"""Tell systemd how the station is doing (sd_notify), without any extra library.

With `Type=notify` and `WatchdogSec=` in sdu-daemon.service:
- READY=1 after start-up, so systemd knows the station really works;
- WATCHDOG=1 on every loop, so a station that hangs is restarted automatically;
- STATUS= with the counts, shown by `systemctl status sdu-daemon`;
- STOPPING=1 while files in progress finish.
Outside systemd (no NOTIFY_SOCKET) every call does nothing.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Callable

logger = logging.getLogger(__name__)


def _send_datagram(address: str, message: bytes) -> None:
    target = "\0" + address[1:] if address.startswith("@") else address  # Abstract namespace.
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:  # type: ignore[attr-defined]
        sock.connect(target)
        sock.sendall(message)


class SystemdNotifier:
    def __init__(self, environ: dict[str, str] | None = None,
                 sender: Callable[[str, bytes], None] | None = None) -> None:
        env = os.environ if environ is None else environ
        self._address = env.get("NOTIFY_SOCKET") or None
        self._sender = sender or _send_datagram
        usec = env.get("WATCHDOG_USEC", "")
        self.watchdog_seconds = int(usec) / 1_000_000 if usec.isdigit() else None
        self._failed = False

    @property
    def enabled(self) -> bool:
        return self._address is not None and not self._failed

    def _notify(self, *fields: str) -> None:
        if not self.enabled:
            return
        try:
            self._sender(self._address, "\n".join(fields).encode("utf-8"))
        except OSError as exc:
            self._failed = True  # Never let reporting to systemd stop the station.
            logger.warning("systemd notification failed: %s", exc)

    def ready(self, status: str = "Station running") -> None:
        self._notify("READY=1", f"STATUS={status}")

    def watchdog(self, status: str | None = None) -> None:
        self._notify("WATCHDOG=1", *([f"STATUS={status}"] if status else []))

    def stopping(self) -> None:
        self._notify("STOPPING=1", "STATUS=Finishing files in progress")


def status_line(state: dict) -> str:
    """One line for `systemctl status`."""
    session = state.get("session")
    if session is None:
        return "No session open"
    c = state.get("counters") or {}
    problems = len(state.get("problems") or [])
    return (f"Session {session.get('session_id')}: {c.get('inserted', 0)} inserted, "
            f"{c.get('uploading', 0)} uploading, {c.get('uploaded', 0)} uploaded, "
            f"{c.get('failed', 0)} failed, {problems} problem(s)")
