from __future__ import annotations

import sqlite3


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  email            TEXT NOT NULL UNIQUE,
  display_name     TEXT,

  absence_user_id  TEXT UNIQUE,
  absence_email    TEXT,
  absence_seen_at  TEXT,

  threecx_user_id  INTEGER UNIQUE,
  threecx_email    TEXT,
  extension        TEXT,
  threecx_seen_at  TEXT,

  is_active        INTEGER NOT NULL DEFAULT 1,

  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active);
CREATE INDEX IF NOT EXISTS idx_users_extension ON users(extension);
CREATE INDEX IF NOT EXISTS idx_users_absence_user_id ON users(absence_user_id);
CREATE INDEX IF NOT EXISTS idx_users_threecx_user_id ON users(threecx_user_id);

CREATE TABLE IF NOT EXISTS state (
  user_id                 INTEGER PRIMARY KEY,

  absence_status          TEXT,
  absence_status_at       TEXT,

  desired_threecx_profile TEXT,
  applied_threecx_profile TEXT,
  threecx_applied_at      TEXT,

  last_sync_at            TEXT,

  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_state_last_sync ON state(last_sync_at);

CREATE TABLE IF NOT EXISTS runs (
  name          TEXT PRIMARY KEY,
  last_ok_at    TEXT,
  last_error_at TEXT,
  last_error    TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        TEXT NOT NULL,

  user_id   INTEGER,
  email     TEXT,

  source    TEXT NOT NULL,
  kind      TEXT NOT NULL,

  old_value TEXT,
  new_value TEXT,
  message   TEXT,

  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_user_id ON events(user_id);
"""


def apply_pragmas(con: sqlite3.Connection) -> None:
    """
    Pragmas are safe to apply repeatedly.
    WAL may fail on read-only filesystems; callers may catch OperationalError if needed.
    """
    con.execute("PRAGMA foreign_keys=ON;")
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")


def _validate_identifier(name: str) -> None:
    """
    SQLite does not support binding table names as parameters.
    Keep this check strict; it's only used for internal table names.
    """
    if not name or any(c in name for c in " \t\n\r;\"'`\\"):
        raise ValueError(f"Invalid SQLite identifier: {name!r}")


def column_exists(con: sqlite3.Connection, table: str, column: str) -> bool:
    _validate_identifier(table)
    rows = con.execute(f"PRAGMA table_info({table});").fetchall()
    return any(r[1] == column for r in rows)  # (cid, name, type, notnull, dflt_value, pk)


def add_column_if_missing(
    con: sqlite3.Connection,
    table: str,
    column: str,
    ddl_fragment: str,
    *,
    backfill_sql: str | None = None,
) -> None:
    if column_exists(con, table, column):
        return
    _validate_identifier(table)
    con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_fragment};")
    if backfill_sql:
        con.execute(backfill_sql)


def apply_schema(con: sqlite3.Connection) -> None:
    """
    Create/update the database schema and apply minimal migrations.
    Safe to run repeatedly at startup.
    """
    # Pragmas: WAL is desirable but might fail in some environments.
    try:
        apply_pragmas(con)
    except sqlite3.OperationalError:
        con.execute("PRAGMA foreign_keys=ON;")
        con.execute("PRAGMA synchronous=NORMAL;")

    con.executescript(SCHEMA_SQL)

    # Backwards-compatible migrations:
    # - older events schema used "at" instead of "ts"
    # - older events schema used "details" instead of "message"
    add_column_if_missing(
        con,
        "events",
        "ts",
        "TEXT",
        backfill_sql="UPDATE events SET ts = at WHERE ts IS NULL AND at IS NOT NULL;"
        if column_exists(con, "events", "at")
        else None,
    )
    add_column_if_missing(
        con,
        "events",
        "message",
        "TEXT",
        backfill_sql="UPDATE events SET message = details WHERE message IS NULL AND details IS NOT NULL;"
        if column_exists(con, "events", "details")
        else None,
    )
