#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from schema import apply_schema


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> None:
    db_path = os.getenv("DB_PATH", "/data/presence.db")
    db_dir = os.path.dirname(db_path) or "."

    # SQLite creates the DB file if it doesn't exist, but it must be able to write in db_dir.
    os.makedirs(db_dir, exist_ok=True)

    con = sqlite3.connect(db_path, timeout=30)
    try:
        apply_schema(con)

        now = utc_now_iso()
        con.execute(
            """
            INSERT INTO runs(name, last_ok_at) VALUES(?, ?)
            ON CONFLICT(name) DO UPDATE SET last_ok_at=excluded.last_ok_at
            """,
            ("db_initialized", now),
        )

        con.commit()
        print(f"[OK] SQLite schema ready: {db_path}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
