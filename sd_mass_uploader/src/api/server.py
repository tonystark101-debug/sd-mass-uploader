"""Run the API with Uvicorn in background threads inside the daemon."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI

logger = logging.getLogger(__name__)

_START_TIMEOUT_SECONDS = 10.0


class ApiServer:
    """One Uvicorn server per address (TCP loopback, and a Unix socket on Linux if set)."""

    def __init__(self, app: FastAPI, host: str, port: int, unix_socket: str = "") -> None:
        self._configs = [uvicorn.Config(app, host=host, port=port, log_level="warning",
                                        access_log=False, lifespan="off", ws="websockets-sansio")]
        self.addresses = [f"http://{host}:{port}"]
        if unix_socket and os.name == "posix":
            Path(unix_socket).parent.mkdir(parents=True, exist_ok=True)
            self._configs.append(uvicorn.Config(app, uds=unix_socket, log_level="warning",
                                                access_log=False, lifespan="off",
                                                ws="websockets-sansio"))
            self.addresses.append(f"unix:{unix_socket}")
        self._servers: list[uvicorn.Server] = []
        self._threads: list[threading.Thread] = []
        self.error: str | None = None

    def start(self) -> bool:
        """Start every listener. Returns False (with `error` set) if one could not start."""
        for config, address in zip(self._configs, self.addresses, strict=True):
            server = uvicorn.Server(config)
            thread = threading.Thread(target=self._serve, args=(server, address),
                                      name=f"api {address}", daemon=True)
            self._servers.append(server)
            self._threads.append(thread)
            thread.start()
            deadline = time.monotonic() + _START_TIMEOUT_SECONDS
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            if not server.started:
                self.error = self.error or f"API could not listen on {address}"
                logger.error(self.error)
                return False
            logger.info("API listening on %s", address)
        return True

    def _serve(self, server: uvicorn.Server, address: str) -> None:
        try:
            server.run()  # Uvicorn installs no signal handlers outside the main thread.
        except SystemExit:  # Uvicorn exits this way when the port is taken.
            self.error = f"API could not listen on {address} (address in use?)"
        except Exception as exc:  # noqa: BLE001 - reported; the station keeps uploading
            self.error = f"API stopped on {address}: {exc}"
            logger.exception("API server failed")

    def stop(self, timeout: float = 5.0) -> None:
        for server in self._servers:
            server.should_exit = True
        for thread in self._threads:
            thread.join(timeout)
