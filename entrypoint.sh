#!/usr/bin/env sh
set -eu

# Logs sofort
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

# Optional: DB initialisieren (safe to run)
if [ "${INIT_DB:-1}" = "1" ]; then
  echo "[entrypoint] init_db.py (DB_PATH=${DB_PATH:-/data/presence.db})"
  python init_db.py || true
fi

echo "[entrypoint] starting watcher.py (ROLE=${ROLE:-})"
exec python watcher.py