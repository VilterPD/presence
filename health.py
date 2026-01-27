#!/usr/bin/env python3
from __future__ import annotations

import os
import socket


def main() -> None:
    host = os.getenv("HEALTH_BIND", "0.0.0.0")
    port = int(os.getenv("HEALTH_PORT", "49317"))

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(50)

    print(f"[health] listening on {host}:{port}", flush=True)

    while True:
        conn, _addr = srv.accept()
        try:
            conn.sendall(b"ok\n")
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
