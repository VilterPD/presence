# db.py
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import List, Optional

from schema import apply_schema


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class DB:
    """
    SQLite DB wrapper.

    Schema is defined centrally in schema.py (single source of truth).
    This class only performs queries and small helper operations.
    """

    def __init__(self, path: str):
        self.path = path

    # -------------------------
    # Connection handling
    # -------------------------
    @contextmanager
    def connect(self) -> sqlite3.Connection:
        parent = os.path.dirname(self.path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # -------------------------
    # Schema
    # -------------------------
    def ensure_schema(self) -> None:
        """
        Ensure DB schema exists and is up to date.
        Delegates entirely to schema.py.
        Safe to call repeatedly.
        """
        with self.connect() as con:
            apply_schema(con)

    # -------------------------
    # runs bookkeeping
    # -------------------------
    def run_ok(self, name: str, when: Optional[str] = None) -> None:
        when = when or utc_now_iso()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO runs(name, last_ok_at, last_error_at, last_error)
                VALUES(?, ?, NULL, NULL)
                ON CONFLICT(name) DO UPDATE SET
                  last_ok_at=excluded.last_ok_at,
                  last_error_at=NULL,
                  last_error=NULL
                """,
                (name, when),
            )

    def run_error(self, name: str, error: str, when: Optional[str] = None) -> None:
        when = when or utc_now_iso()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO runs(name, last_ok_at, last_error_at, last_error)
                VALUES(?, NULL, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  last_error_at=excluded.last_error_at,
                  last_error=excluded.last_error
                """,
                (name, when, error),
            )

    # -------------------------
    # events audit
    # -------------------------
    def log_event(
        self,
        source: str,
        kind: str,
        *,
        user_id: Optional[int] = None,
        email: Optional[str] = None,
        old_value: Optional[str] = None,
        new_value: Optional[str] = None,
        message: Optional[str] = None,
        ts: Optional[str] = None,
    ) -> None:
        ts = ts or utc_now_iso()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO events(
                    ts, user_id, email, source, kind, old_value, new_value, message
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (ts, user_id, email, source, kind, old_value, new_value, message),
            )

    # -------------------------
    # users upsert / lookup
    # -------------------------
    def upsert_user(
        self,
        *,
        email: str,
        display_name: Optional[str] = None,
        is_active: int = 1,
        absence_user_id: Optional[str] = None,
        absence_email: Optional[str] = None,
        absence_seen_at: Optional[str] = None,
        threecx_user_id: Optional[int] = None,
        threecx_email: Optional[str] = None,
        extension: Optional[str] = None,
        threecx_seen_at: Optional[str] = None,
    ) -> int:
        """
        Upsert user by canonical email.
        Returns users.id.
        """
        now = utc_now_iso()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO users(
                    email, display_name,
                    absence_user_id, absence_email, absence_seen_at,
                    threecx_user_id, threecx_email, extension, threecx_seen_at,
                    is_active, created_at, updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(email) DO UPDATE SET
                    display_name=COALESCE(excluded.display_name, users.display_name),

                    absence_user_id=COALESCE(excluded.absence_user_id, users.absence_user_id),
                    absence_email=COALESCE(excluded.absence_email, users.absence_email),
                    absence_seen_at=COALESCE(excluded.absence_seen_at, users.absence_seen_at),

                    threecx_user_id=COALESCE(excluded.threecx_user_id, users.threecx_user_id),
                    threecx_email=COALESCE(excluded.threecx_email, users.threecx_email),
                    extension=COALESCE(excluded.extension, users.extension),
                    threecx_seen_at=COALESCE(excluded.threecx_seen_at, users.threecx_seen_at),

                    is_active=excluded.is_active,
                    updated_at=excluded.updated_at
                """,
                (
                    email,
                    display_name,
                    absence_user_id,
                    absence_email,
                    absence_seen_at,
                    threecx_user_id,
                    threecx_email,
                    extension,
                    threecx_seen_at,
                    int(is_active),
                    now,
                    now,
                ),
            )

            row = con.execute(
                "SELECT id FROM users WHERE email=?",
                (email,),
            ).fetchone()

            return int(row["id"])

    def get_user_by_email(self, email: str) -> Optional[sqlite3.Row]:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM users WHERE email=?",
                (email,),
            ).fetchone()

    def get_user_by_extension(self, extension: str) -> Optional[sqlite3.Row]:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM users WHERE extension=?",
                (extension,),
            ).fetchone()

    def list_active_users(self) -> List[sqlite3.Row]:
        with self.connect() as con:
            return con.execute(
                """
                SELECT *
                FROM users
                WHERE is_active=1
                ORDER BY email COLLATE NOCASE
                """
            ).fetchall()

    def list_users_ready_for_sync(self) -> List[sqlite3.Row]:
        """
        Users that have enough data to apply a 3CX profile.
        """
        with self.connect() as con:
            return con.execute(
                """
                SELECT *
                FROM users
                WHERE is_active=1
                  AND email IS NOT NULL AND email!=''
                  AND (
                        threecx_user_id IS NOT NULL
                        OR (extension IS NOT NULL AND extension!='')
                  )
                ORDER BY email COLLATE NOCASE
                """
            ).fetchall()

    # -------------------------
    # state helpers
    # -------------------------
    def ensure_state_row(self, con: sqlite3.Connection, user_id: int) -> None:
        con.execute(
            """
            INSERT INTO state(user_id)
            VALUES(?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id,),
        )

    def get_state(self, user_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM state WHERE user_id=?",
                (user_id,),
            ).fetchone()

    def get_last_applied_profile(self, user_id: int) -> Optional[str]:
        with self.connect() as con:
            row = con.execute(
                "SELECT applied_threecx_profile FROM state WHERE user_id=?",
                (user_id,),
            ).fetchone()
            return row["applied_threecx_profile"] if row else None

    def set_absence_status(self, user_id: int, status: str, at: Optional[str] = None) -> None:
        at = at or utc_now_iso()
        with self.connect() as con:
            self.ensure_state_row(con, user_id)
            con.execute(
                """
                UPDATE state
                SET absence_status=?,
                    absence_status_at=?,
                    last_sync_at=?
                WHERE user_id=?
                """,
                (status, at, utc_now_iso(), user_id),
            )

    def set_desired_profile(self, user_id: int, profile: str) -> None:
        with self.connect() as con:
            self.ensure_state_row(con, user_id)
            con.execute(
                """
                UPDATE state
                SET desired_threecx_profile=?,
                    last_sync_at=?
                WHERE user_id=?
                """,
                (profile, utc_now_iso(), user_id),
            )

    def mark_applied_profile(
        self,
        user_id: int,
        profile: str,
        applied_at: Optional[str] = None,
    ) -> None:
        applied_at = applied_at or utc_now_iso()
        with self.connect() as con:
            self.ensure_state_row(con, user_id)
            con.execute(
                """
                UPDATE state
                SET applied_threecx_profile=?,
                    threecx_applied_at=?,
                    last_sync_at=?
                WHERE user_id=?
                """,
                (profile, applied_at, utc_now_iso(), user_id),
            )

    # -------------------------
    # convenience stamps
    # -------------------------
    def touch_absence_seen(self, email: str, seen_at: Optional[str] = None) -> None:
        seen_at = seen_at or utc_now_iso()
        with self.connect() as con:
            con.execute(
                """
                UPDATE users
                SET absence_seen_at=?,
                    updated_at=?
                WHERE email=?
                """,
                (seen_at, utc_now_iso(), email),
            )

    def touch_threecx_seen(self, email: str, seen_at: Optional[str] = None) -> None:
        seen_at = seen_at or utc_now_iso()
        with self.connect() as con:
            con.execute(
                """
                UPDATE users
                SET threecx_seen_at=?,
                    updated_at=?
                WHERE email=?
                """,
                (seen_at, utc_now_iso(), email),
            )
