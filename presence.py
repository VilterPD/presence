#!/usr/bin/env python3
"""
presence.py (Status Sync)

Purpose
- Runs continuously and keeps 3CX user profiles in sync with Absence.io timetracking status.
- Requires mappings to be present in SQLite (run presence_sync.py periodically).

High-level flow (every poll interval):
1) Read "ready" users from DB (have email + absence_user_id + threecx_user_id and active).
2) Fetch Absence timetracking status map (absence_user_id -> status).
3) Map Absence status -> desired 3CX profile (e.g. work -> Available, else -> Away).
4) If desired differs from last applied profile, PATCH 3CX Users(id) CurrentProfileName.
5) Write status + applied info to DB state + events.

Environment variables
Required:
- DB_PATH (default: /data/presence.db)

Absence (Hawk):
- ABSENCE_BASE_URL (default: https://app.absence.io)
- ABSENCE_API_ID
- ABSENCE_API_KEY

3CX (OAuth Service Principal):
- THREECX_PBX_FQDN
- THREECX_CLIENT_ID
- THREECX_CLIENT_SECRET

Profiles:
- THREECX_PROFILE_AVAILABLE (default: Available)
- THREECX_PROFILE_AWAY (default: Away)

Polling:
- DAY_START_HOUR (default: 7)       # local time hour
- DAY_END_HOUR (default: 20)        # local time hour
- INTERVAL_DAY_SECONDS (default: 60)
- INTERVAL_NIGHT_SECONDS (default: 600)

Exclusions (comma-separated):
- EXCLUDE_EMAILS
- EXCLUDE_EXTENSIONS

Other:
- SYNC_DRY_RUN (true/false; default: false)
- ROLE=backup|secondary|standby -> exit immediately (standby mode)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Set

import httpx
from mohawk import Sender

from db import DB, utc_now_iso


# ----------------------------
# helpers
# ----------------------------
def norm_email(s: str) -> str:
    return (s or "").strip().lower()


def parse_csv_set(raw: str) -> Set[str]:
    if not raw:
        return set()
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


# ----------------------------
# config
# ----------------------------
@dataclass(frozen=True)
class Config:
    db_path: str

    absence_base_url: str
    absence_api_id: str
    absence_api_key: str

    threecx_pbx_fqdn: str
    threecx_client_id: str
    threecx_client_secret: str

    profile_available: str
    profile_away: str

    day_start_hour: int
    day_end_hour: int
    interval_day_seconds: int
    interval_night_seconds: int

    exclude_emails: Set[str]
    exclude_extensions: Set[str]

    dry_run: bool


def load_config() -> Config:
    def getenv(name: str, default: str = "") -> str:
        return os.getenv(name, default).strip()

    def getenv_required(name: str) -> str:
        v = getenv(name, "")
        if not v:
            raise SystemExit(f"[FATAL] Missing required env var: {name}")
        return v

    def getenv_int(name: str, default: int) -> int:
        v = getenv(name, str(default))
        try:
            return int(v)
        except Exception:
            return default

    def getenv_bool(name: str, default: bool = False) -> bool:
        v = getenv(name, str(default)).lower()
        return v in ("1", "true", "yes", "y", "on")

    day_start = clamp_int(getenv_int("DAY_START_HOUR", 7), 0, 23)
    day_end = clamp_int(getenv_int("DAY_END_HOUR", 20), 0, 23)

    return Config(
        db_path=getenv("DB_PATH", "/data/presence.db"),
        absence_base_url=getenv("ABSENCE_BASE_URL", "https://app.absence.io"),
        absence_api_id=getenv_required("ABSENCE_API_ID"),
        absence_api_key=getenv_required("ABSENCE_API_KEY"),
        threecx_pbx_fqdn=getenv_required("THREECX_PBX_FQDN"),
        threecx_client_id=getenv_required("THREECX_CLIENT_ID"),
        threecx_client_secret=getenv_required("THREECX_CLIENT_SECRET"),
        profile_available=getenv("THREECX_PROFILE_AVAILABLE", "Available"),
        profile_away=getenv("THREECX_PROFILE_AWAY", "Away"),
        day_start_hour=day_start,
        day_end_hour=day_end,
        interval_day_seconds=max(5, getenv_int("INTERVAL_DAY_SECONDS", 60)),
        interval_night_seconds=max(10, getenv_int("INTERVAL_NIGHT_SECONDS", 600)),
        exclude_emails=parse_csv_set(getenv("EXCLUDE_EMAILS", "")),
        exclude_extensions=parse_csv_set(getenv("EXCLUDE_EXTENSIONS", "")),
        dry_run=getenv_bool("SYNC_DRY_RUN", False),
    )


def current_interval_seconds(cfg: Config) -> int:
    """
    Returns polling interval depending on local time.

    Semantics:
      - If start < end: "day" is [start, end) e.g. 07..20
      - If start > end: wraps midnight e.g. 20..07
      - If equal: always-day.
    """
    local_hour = datetime.now().hour
    s = cfg.day_start_hour
    e = cfg.day_end_hour

    if s == e:
        return cfg.interval_day_seconds

    if s < e:
        is_day = (local_hour >= s) and (local_hour < e)
    else:
        is_day = (local_hour >= s) or (local_hour < e)

    return cfg.interval_day_seconds if is_day else cfg.interval_night_seconds


# ----------------------------
# Absence.io client (Hawk)
# ----------------------------
class AbsenceClient:
    def __init__(self, base_url: str, api_id: str, api_key: str, timeout_s: int = 30):
        self.base = base_url.rstrip("/")
        self.api_id = api_id
        self.api_key = api_key
        self.timeout = httpx.Timeout(timeout_s)

    def _hawk_headers(self, method: str, url: str, content: bytes, content_type: str) -> Dict[str, str]:
        sender = Sender(
            credentials={"id": self.api_id, "key": self.api_key, "algorithm": "sha256"},
            url=url,
            method=method.upper(),
            content=content,
            content_type=content_type,
        )
        return {"Authorization": sender.request_header}

    def fetch_timetracking_status(self) -> Dict[str, str]:
        """
        Returns: absence_user_id -> status (work/off/break/...)
        Endpoint: POST /api/timetracking/status
        """
        url = f"{self.base}/api/timetracking/status"
        payload = {"skip": 0, "limit": 200}
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **self._hawk_headers("POST", url, content=body, content_type="application/json"),
        }

        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(url, headers=headers, content=body)
            r.raise_for_status()
            j = r.json()

        data = j.get("data", []) if isinstance(j, dict) else []
        out: Dict[str, str] = {}
        for row in data:
            uid = str(row.get("_id") or "").strip()
            st = str(row.get("status") or "").strip()
            if uid:
                out[uid] = st
        return out


def absence_status_to_profile(abs_status: str, profile_available: str, profile_away: str) -> str:
    """
    Mapping:
      - work  -> Available
      - break -> Away
      - off/other -> Away
    """
    s = (abs_status or "").strip().lower()
    return profile_available if s == "work" else profile_away


# ----------------------------
# 3CX XAPI client (OAuth)
# ----------------------------
class ThreeCXClient:
    def __init__(self, pbx_fqdn: str, client_id: str, client_secret: str, timeout_s: int = 30):
        self.base = f"https://{pbx_fqdn.strip()}"
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = httpx.Timeout(timeout_s)
        self._token: Optional[str] = None
        self._token_expiry_ts: float = 0.0

    def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expiry_ts - 5:
            return self._token

        url = f"{self.base}/connect/token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }

        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                auth=(self.client_id, self.client_secret),
            )
            r.raise_for_status()
            j = r.json()

        token = j.get("access_token") or ""
        if not token:
            raise RuntimeError(f"3CX token response missing access_token: {j}")

        expires_in = int(j.get("expires_in", 60))
        self._token = token
        self._token_expiry_ts = now + expires_in
        return token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}", "Accept": "application/json"}

    def set_user_profile(self, threecx_user_id: int, profile_name: str) -> None:
        """
        PATCH /xapi/v1/Users({id}) { "CurrentProfileName": "<profile>" }
        """
        url = f"{self.base}/xapi/v1/Users({int(threecx_user_id)})"
        payload = {"CurrentProfileName": profile_name}

        with httpx.Client(timeout=self.timeout) as client:
            r = client.patch(url, headers={**self._headers(), "Content-Type": "application/json"}, json=payload)
            r.raise_for_status()


# ----------------------------
# main loop
# ----------------------------
def main() -> None:
    role = os.getenv("ROLE", "").strip().lower()
    if role in ("backup", "secondary", "standby"):
        print("[INFO] ROLE indicates standby mode; exiting.")
        return

    cfg = load_config()
    db = DB(cfg.db_path)
    # Ensure schema exists (idempotent). If you only want init_db.py, this is still harmless.
    db.ensure_schema()

    absence = AbsenceClient(cfg.absence_base_url, cfg.absence_api_id, cfg.absence_api_key)
    threecx = ThreeCXClient(cfg.threecx_pbx_fqdn, cfg.threecx_client_id, cfg.threecx_client_secret)

    print("[INFO] presence.py started")
    print(f"[INFO] DB_PATH={cfg.db_path}")
    print(f"[INFO] PBX={cfg.threecx_pbx_fqdn} dry_run={cfg.dry_run}")
    print(
        f"[INFO] polling: day={cfg.day_start_hour:02d}:00..{cfg.day_end_hour:02d}:00 "
        f"interval_day={cfg.interval_day_seconds}s interval_night={cfg.interval_night_seconds}s"
    )
    if cfg.exclude_emails:
        print(f"[INFO] EXCLUDE_EMAILS={sorted(cfg.exclude_emails)}")
    if cfg.exclude_extensions:
        print(f"[INFO] EXCLUDE_EXTENSIONS={sorted(cfg.exclude_extensions)}")

    run_name = "presence_poll"

    while True:
        tick_started = time.time()
        interval = current_interval_seconds(cfg)

        changed = 0
        skipped = 0
        errors = 0

        try:
            users = db.list_users_ready_for_sync()
            if not users:
                print("[WARN] No users ready for sync (run presence_sync.py first).")
                db.run_ok(run_name, when=utc_now_iso())
                time.sleep(interval)
                continue

            status_map = absence.fetch_timetracking_status()

            for u in users:
                user_id = int(u["id"])
                email = norm_email(u["email"] or "")
                ext = str(u["extension"] or "").strip()
                absence_user_id = str(u["absence_user_id"] or "").strip()
                threecx_user_id = u["threecx_user_id"]

                # Exclusions
                if email and email in cfg.exclude_emails:
                    skipped += 1
                    continue
                if ext and ext.lower() in cfg.exclude_extensions:
                    skipped += 1
                    continue

                # Must have both IDs for this sync strategy
                if not absence_user_id or threecx_user_id is None:
                    skipped += 1
                    continue

                abs_status = status_map.get(absence_user_id, "off")
                desired_profile = absence_status_to_profile(abs_status, cfg.profile_available, cfg.profile_away)

                # Store observed status + desired profile (even if we don't change anything)
                db.set_absence_status(user_id, abs_status)
                db.set_desired_profile(user_id, desired_profile)

                last_applied = db.get_last_applied_profile(user_id)
                if last_applied == desired_profile:
                    db.log_event(
                        source="sync",
                        kind="noop",
                        user_id=user_id,
                        email=email,
                        old_value=last_applied,
                        new_value=desired_profile,
                        message="desired profile equals last applied",
                    )
                    skipped += 1
                    continue

                if cfg.dry_run:
                    print(f"[DRY] {email:40} ext={ext:>4}  {abs_status:>5} -> {desired_profile}")
                    db.mark_applied_profile(user_id, desired_profile)
                    db.log_event(
                        source="sync",
                        kind="dry_run",
                        user_id=user_id,
                        email=email,
                        old_value=last_applied,
                        new_value=desired_profile,
                        message="dry run; not calling 3CX",
                    )
                    changed += 1
                    continue

                try:
                    threecx.set_user_profile(int(threecx_user_id), desired_profile)
                    print(f"[OK]  {email:40} ext={ext:>4}  {abs_status:>5} -> {desired_profile}")

                    db.mark_applied_profile(user_id, desired_profile)
                    db.log_event(
                        source="3cx",
                        kind="profile_set",
                        user_id=user_id,
                        email=email,
                        old_value=last_applied,
                        new_value=desired_profile,
                        message="profile updated in 3CX",
                    )
                    changed += 1
                except Exception as e:
                    errors += 1
                    db.log_event(
                        source="3cx",
                        kind="error",
                        user_id=user_id,
                        email=email,
                        old_value=last_applied,
                        new_value=desired_profile,
                        message=f"failed to set profile: {type(e).__name__}: {e}",
                    )
                    print(f"[ERR] {email:40} ext={ext:>4}  failed to set {desired_profile}: {e}")

            db.run_ok(run_name, when=utc_now_iso())

            dt = time.time() - tick_started
            print(
                f"[INFO] tick done in {dt:.2f}s | interval={interval}s | "
                f"changed={changed} skipped={skipped} errors={errors}"
            )

        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            db.run_error(run_name, err, when=utc_now_iso())
            db.log_event(source="sync", kind="error", message=f"tick failed: {err}")
            print(f"[ERROR] tick failed: {err}")

        elapsed = time.time() - tick_started
        sleep_for = max(1, interval - int(elapsed))
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
