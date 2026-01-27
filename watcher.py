#!/usr/bin/env python3
from __future__ import annotations

import os
import time
import socket
import subprocess
from typing import Optional

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))
FAILOVER_AFTER = int(os.getenv("FAILOVER_AFTER", "60"))
FAILBACK_AFTER = int(os.getenv("FAILBACK_AFTER", "600"))

ROLE = os.getenv("ROLE", "").strip().lower()
PRIMARY_HOST = os.getenv("PRIMARY_HOST", "").strip()
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "49317"))

ACTIVE_FILE = "/tmp/presence_active"


def log(msg: str) -> None:
    print(f"[watcher] {msg}", flush=True)


def tcp_check(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def is_active() -> bool:
    return os.path.exists(ACTIVE_FILE)


def mark_active(active: bool) -> None:
    if active:
        with open(ACTIVE_FILE, "w") as f:
            f.write("active\n")
    else:
        try:
            os.remove(ACTIVE_FILE)
        except FileNotFoundError:
            pass


class ManagedProcs:
    def __init__(self) -> None:
        self.health: Optional[subprocess.Popen] = None
        self.presence: Optional[subprocess.Popen] = None

    def start_health(self, env: dict) -> None:
        if self.health and self.health.poll() is None:
            return
        log("starting health.py")
        self.health = subprocess.Popen(["python", "health.py"], env=env)

    def start_presence(self, env: dict) -> None:
        # refresh mappings once before starting the sync loop
        log("running presence_sync.py")
        subprocess.run(["python", "presence_sync.py"], check=False, env=env)

        if self.presence and self.presence.poll() is None:
            return
        log("starting presence.py")
        self.presence = subprocess.Popen(["python", "presence.py"], env=env)

    def stop_all(self) -> None:
        for name, p in [("presence.py", self.presence), ("health.py", self.health)]:
            if not p or p.poll() is not None:
                continue
            log(f"stopping {name}")
            try:
                p.terminate()
                p.wait(timeout=10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self.presence = None
        self.health = None


def active_env() -> dict:
    # When a backup takes over, child processes must behave like primary.
    env = os.environ.copy()
    env["ROLE"] = "primary"
    return env


def main() -> None:
    log(f"ROLE={ROLE}")

    procs = ManagedProcs()

    # Primary: always active
    if ROLE == "primary":
        env = active_env()
        procs.start_health(env)
        procs.start_presence(env)
        while True:
            time.sleep(3600)

    # Backup: needs a primary to check
    if not PRIMARY_HOST:
        log("ERROR: PRIMARY_HOST not set (backup mode)")
        while True:
            time.sleep(60)

    down_since: Optional[float] = None
    up_since: Optional[float] = None

    while True:
        alive = tcp_check(PRIMARY_HOST, HEALTH_PORT)
        now = time.time()

        if alive:
            down_since = None
            up_since = up_since or now

            if is_active() and (now - up_since) >= FAILBACK_AFTER:
                log("primary stable again → failback to standby")
                procs.stop_all()
                mark_active(False)

        else:
            up_since = None
            down_since = down_since or now

            if (not is_active()) and (now - down_since) >= FAILOVER_AFTER:
                log("primary down → taking over")
                env = active_env()
                procs.start_health(env)
                procs.start_presence(env)
                mark_active(True)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
