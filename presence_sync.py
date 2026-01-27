#!/usr/bin/env python3
"""
presence_sync.py  (Mapping Refresh)

Purpose
-------
Run this 1–2 times per day to refresh the *mapping table* (Absence <-> 3CX) in SQLite.

What it does:
- Fetches user lists from Absence (Hawk auth) and 3CX (XAPI OAuth client credentials).
- Matches users by canonical email (lowercased).
- Upserts a unified "users" record into SQLite, including:
    - absence_user_id / absence_email / absence_seen_at
    - threecx_user_id / threecx_email / extension / threecx_seen_at
    - display_name (best-effort)
- Writes a short run/event log to the DB.

What it does NOT do:
- It does NOT change 3CX presence/profile.
  That is handled by the runtime sync loop (presence.py).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional

import httpx
from mohawk import Sender

from db import DB, utc_now_iso


# -----------------------------
# Config helpers
# -----------------------------
def env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise SystemExit(f"[FATAL] Missing required env var: {name}")
    return v


def env(name: str, default: str) -> str:
    v = os.getenv(name, default)
    return (v or "").strip()


def env_int(name: str, default: int) -> int:
    v = os.getenv(name, "")
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        raise SystemExit(f"[FATAL] Env var {name} must be int, got: {v!r}")


def norm_email(s: Optional[str]) -> str:
    return (s or "").strip().lower()


# -----------------------------
# Absence client (Hawk)
# -----------------------------
class AbsenceClient:
    """
    Absence API uses Hawk. Hawk signs the request content, so we must use a deterministic JSON encoding.
    """

    def __init__(self, base_url: str, api_id: str, api_key: str, timeout_s: int = 20):
        self.base_url = base_url.rstrip("/")
        self.api_id = api_id
        self.api_key = api_key
        self.timeout = httpx.Timeout(timeout_s)

    def _hawk_header(self, url: str, method: str, content: bytes, content_type: str) -> str:
        sender = Sender(
            credentials={"id": self.api_id, "key": self.api_key, "algorithm": "sha256"},
            url=url,
            method=method.upper(),
            content=content,
            content_type=content_type,
        )
        return sender.request_header

    def post_json(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"

        # Deterministic encoding for Hawk signature
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self._hawk_header(url, "POST", content=body, content_type="application/json"),
        }

        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(url, headers=headers, content=body)
            r.raise_for_status()
            return r.json()

    def list_users(self, limit: int = 100) -> List[dict]:
        """
        Absence v2 listing:
          POST /api/v2/users { skip, limit, filter }

        We fetch all users page-by-page.
        """
        all_rows: List[dict] = []
        skip = 0

        while True:
            payload = {"skip": skip, "limit": limit, "filter": {}}
            data = self.post_json("/api/v2/users", payload)

            rows = data.get("data") or []
            all_rows.extend(rows)

            # Some implementations provide "count" for the page; fall back to page size.
            page_count = int(data.get("count") or len(rows))
            if page_count < limit:
                break

            skip += limit
            if skip > 10000:  # safety valve
                break

        return all_rows


# -----------------------------
# 3CX XAPI client (OAuth token)
# -----------------------------
class ThreeCXClient:

    def __init__(self, pbx_fqdn: str, client_id: str, client_secret: str, timeout_s: int = 20):
        self.pbx = pbx_fqdn.strip()
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = httpx.Timeout(timeout_s)

    def token(self) -> str:
        url = f"https://{self.pbx}/connect/token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }

        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=data,
                auth=(self.client_id, self.client_secret),
            )
            r.raise_for_status()
            j = r.json()

        tok = j.get("access_token") or ""
        if not tok:
            raise RuntimeError(f"3CX token response missing access_token: {j}")
        return tok

    def xapi_get(self, token: str, path_and_query: str) -> dict:
        url = f"https://{self.pbx}{path_and_query}"
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            r.raise_for_status()
            return r.json()

    def list_users(self, token: str) -> List[dict]:
        """
        OData limit: $top max 100. Paginate using $skip.
        """
        all_rows: List[dict] = []
        top = 100
        skip = 0

        while True:
            q = (
                "/xapi/v1/Users"
                "?$select=Id,Number,EmailAddress,FirstName,LastName,DisplayName"
                f"&$top={top}&$skip={skip}"
            )
            j = self.xapi_get(token, q)
            rows = j.get("value") or []
            all_rows.extend(rows)

            if len(rows) < top:
                break

            skip += top
            if skip > 10000:
                break

        return all_rows


# -----------------------------
# Mapping helpers
# -----------------------------
def build_absence_index(users: List[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for u in users:
        email = norm_email(u.get("email"))
        if email:
            out[email] = u
    return out


def build_threecx_index(users: List[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for u in users:
        email = norm_email(u.get("EmailAddress"))
        if email:
            out[email] = u
    return out


def display_name_from_parts(first: str, last: str, fallback: str = "") -> str:
    first = (first or "").strip()
    last = (last or "").strip()
    full = (first + " " + last).strip()
    return full or (fallback or "").strip()


# -----------------------------
# Main
# -----------------------------
def main() -> int:
    role = os.getenv("ROLE", "").strip().lower()
    if role in ("backup", "secondary", "standby"):
        print("[INFO] ROLE indicates standby mode; exiting.")
        return 0

    # Config
    db_path = env("DB_PATH", "/data/presence.db")
    timeout_s = env_int("HTTP_TIMEOUT_SECONDS", 20)

    absence_base = env("ABSENCE_BASE_URL", "https://app.absence.io")
    absence_id = env_required("ABSENCE_API_ID")
    absence_key = env_required("ABSENCE_API_KEY")

    pbx = env_required("THREECX_PBX_FQDN")
    threecx_id = env_required("THREECX_CLIENT_ID")
    threecx_secret = env_required("THREECX_CLIENT_SECRET")

    # DB (schema is idempotent)
    db = DB(db_path)
    db.ensure_schema()

    run_name = "mapping_refresh"
    db.log_event(source="sync", kind="mapping_refresh", message="started", ts=utc_now_iso())

    print("[INFO] presence_sync (mapping refresh) started")
    print(f"[INFO] DB_PATH={db_path}")
    print(f"[INFO] Absence={absence_base}")
    print(f"[INFO] 3CX={pbx}")

    try:
        # 1) Absence users
        a = AbsenceClient(absence_base, absence_id, absence_key, timeout_s=timeout_s)
        absence_users = a.list_users(limit=100)
        absence_idx = build_absence_index(absence_users)
        print(f"[OK] Absence users fetched: total={len(absence_users)} with_email={len(absence_idx)}")

        # 2) 3CX users
        t = ThreeCXClient(pbx, threecx_id, threecx_secret, timeout_s=timeout_s)
        token = t.token()
        threecx_users = t.list_users(token)
        threecx_idx = build_threecx_index(threecx_users)
        print(f"[OK] 3CX users fetched: total={len(threecx_users)} with_email={len(threecx_idx)}")

        # 3) Merge by email (union)
        all_emails = sorted(set(absence_idx.keys()) | set(threecx_idx.keys()))
        print(f"[INFO] union emails: {len(all_emails)}")

        now = utc_now_iso()
        upserted = 0
        linked_absence = 0
        linked_threecx = 0

        for email in all_emails:
            au = absence_idx.get(email)
            tu = threecx_idx.get(email)

            # Display name preference:
            #  - 3CX First/Last (or DisplayName)
            #  - Absence firstName/lastName (or name)
            #  - email
            disp = ""
            if tu:
                disp = display_name_from_parts(
                    tu.get("FirstName") or "",
                    tu.get("LastName") or "",
                    fallback=tu.get("DisplayName") or "",
                )
            if not disp and au:
                disp = display_name_from_parts(
                    au.get("firstName") or "",
                    au.get("lastName") or "",
                    fallback=au.get("name") or "",
                )
            if not disp:
                disp = email

            # Absence fields
            absence_user_id = None
            absence_email = None
            absence_seen_at = None
            if au:
                linked_absence += 1
                absence_user_id = str(au.get("_id") or au.get("id") or "") or None
                absence_email = au.get("email") or email
                absence_seen_at = now

            # 3CX fields
            threecx_user_id = None
            threecx_email = None
            extension = None
            threecx_seen_at = None
            if tu:
                linked_threecx += 1
                try:
                    threecx_user_id = int(tu.get("Id")) if tu.get("Id") is not None else None
                except Exception:
                    threecx_user_id = None

                threecx_email = tu.get("EmailAddress") or email
                extension = str(tu.get("Number") or "").strip() or None
                threecx_seen_at = now

            db.upsert_user(
                email=email,
                display_name=disp,
                is_active=1,
                absence_user_id=absence_user_id,
                absence_email=absence_email,
                absence_seen_at=absence_seen_at,
                threecx_user_id=threecx_user_id,
                threecx_email=threecx_email,
                extension=extension,
                threecx_seen_at=threecx_seen_at,
            )
            upserted += 1

        db.run_ok(run_name, when=utc_now_iso())
        db.log_event(
            source="sync",
            kind="mapping_refresh",
            message=f"completed: upserted={upserted} linked_absence={linked_absence} linked_threecx={linked_threecx}",
        )

        print("[OK] mapping refresh completed")
        print(f"[INFO] upserted:       {upserted}")
        print(f"[INFO] linked absence: {linked_absence}")
        print(f"[INFO] linked 3cx:     {linked_threecx}")

        # Compact report: who is ready for presence.py
        rows = db.list_users_ready_for_sync()
        print(f"[INFO] users_ready_for_sync: {len(rows)}")
        if rows:
            print("---- READY (email -> ext / threecx_user_id / absence_user_id) ----")
            for r in rows:
                print(
                    f"{r['email']:<35} -> ext={str(r['extension'] or '-'):>4}  "
                    f"3cx_id={str(r['threecx_user_id'] or '-'):>4}  "
                    f"abs_id={str(r['absence_user_id'] or '-')}"
                )

        print("[OK] presence_sync finished successfully.")
        return 0

    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        db.run_error(run_name, err, when=utc_now_iso())
        db.log_event(source="sync", kind="error", message=err)
        print(f"[ERROR] mapping refresh failed: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
