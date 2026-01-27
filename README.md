# Presence Sync (experimental)

This project synchronizes employee presence states between absence.io (time tracking / absence management) and 3CX (PBX presence profiles).

It is designed to run continuously in a Docker container and supports a primary / backup failover setup.

Status: Experimental  
The project is in active use but still evolving. Interfaces, configuration and behaviour may change.

---

## Overview

The application consists of three logical parts:

### 1. Mapping refresh (presence_sync.py)
- Runs once on startup and can be executed manually.
- Fetches users from absence.io and 3CX.
- Matches users by canonical email address.
- Stores a unified mapping in a local SQLite database.

### 2. Presence loop (presence.py)
- Runs continuously.
- Polls absence.io for current working state.
- Translates absence state into a 3CX presence profile.
- Applies the profile via the 3CX XAPI.
- Uses different polling intervals for day/night operation.

### 3. Failover watcher (watcher.py)
- Enables active/standby operation.
- Backup instance monitors the primary via a TCP health port.
- Automatically takes over if the primary becomes unreachable.
- Automatically fails back after the primary is stable again.

A small health server (health.py) exposes a TCP port used exclusively for failover checks.

---

## Supported systems

Currently implemented:

- absence.io
  - Hawk authentication
  - User list
  - Time tracking status (work / break / off)

- 3CX
  - OAuth2 client credentials
  - XAPI user endpoints
  - Presence profile switching

The architecture is intentionally modular.  
Additional systems or providers can be integrated by adding new clients and mapping logic.

---

## Data storage

- SQLite database (default: /data/presence.db)
- Schema includes:
  - users (identity + mapping)
  - state (last known / applied presence)
  - runs (job bookkeeping)
  - events (audit trail)

The schema is idempotent and safe to initialize on startup.

---

## Docker image

vilter/presence:latest  
or a pinned version such as vilter/presence:0.1.2

---

## Configuration

Configuration is done exclusively via environment variables.

### Required variables

ROLE  
primary or backup

DB_PATH  
Path to the SQLite database (inside the container)

absence.io:
- ABSENCE_BASE_URL
- ABSENCE_API_ID
- ABSENCE_API_KEY

3CX:
- THREECX_PBX_FQDN
- THREECX_CLIENT_ID
- THREECX_CLIENT_SECRET

---

### Failover (backup only)

PRIMARY_HOST  
IP or hostname of the primary instance

HEALTH_PORT  
TCP port exposed by the primary (default: 49317)

---

### Polling behaviour

- DAY_START_HOUR
- DAY_END_HOUR
- INTERVAL_DAY_SECONDS
- INTERVAL_NIGHT_SECONDS

---

### Optional

- EXCLUDE_EMAILS
- EXCLUDE_EXTENSIONS
- SYNC_DRY_RUN

---

## Primary vs Backup behaviour

Primary:
- Runs health server
- Runs presence loop continuously
- Accepts failover checks from backup

Backup:
- Does nothing while primary is reachable
- Monitors the primary via TCP health port
- Takes over automatically if the primary is unreachable
- Fails back automatically once the primary is stable again

Both instances use the same container image.  
Only environment variables differ.

---

## API usage considerations

The application performs regular polling against absence.io and 3CX.
Typical intervals (for example once per minute during office hours) are within normal background usage for both systems.

---

## Limitations

- Only absence.io and 3CX are currently supported.
- SQLite is used by design; no external database support yet.
- No built-in rate limiting beyond polling intervals.
- No encryption or authentication on the health port (intended for private networks/tailscale/wireguard only).

---

Internal or private use only. No guarantees are provided regarding correctness or availability.
