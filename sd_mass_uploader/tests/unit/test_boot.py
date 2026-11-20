"""Boot and kiosk: systemd readiness and watchdog, and the unit, kiosk and desktop files."""

from __future__ import annotations

import os
import shlex
import socket
import sys
import threading
from pathlib import Path

import pytest

import src.station.daemon as daemon_module
from src.cli.commands import build_parser
from src.cli.wiring import App
from src.station.daemon import Daemon
from src.station.systemd import SystemdNotifier, status_line

ROOT = Path(__file__).resolve().parents[2]
LINUX = ROOT / "packaging" / "linux"


class Recorder:
    def __init__(self, fail: bool = False):
        self.messages: list[str] = []
        self.fail = fail

    def __call__(self, address, payload):
        if self.fail:
            raise OSError("socket gone")
        self.messages.append(payload.decode())


def test_notifier_is_silent_outside_systemd():
    recorder = Recorder()
    notifier = SystemdNotifier(environ={}, sender=recorder)
    notifier.ready()
    notifier.watchdog("x")
    assert not notifier.enabled and recorder.messages == [] and notifier.watchdog_seconds is None


def test_notifier_messages_and_failure():
    recorder = Recorder()
    notifier = SystemdNotifier({"NOTIFY_SOCKET": "@sd", "WATCHDOG_USEC": "120000000"}, recorder)
    assert notifier.watchdog_seconds == 120
    notifier.ready("Station running")
    notifier.watchdog("3 uploading")
    notifier.stopping()
    assert recorder.messages == ["READY=1\nSTATUS=Station running",
                                 "WATCHDOG=1\nSTATUS=3 uploading",
                                 "STOPPING=1\nSTATUS=Finishing files in progress"]
    broken = SystemdNotifier({"NOTIFY_SOCKET": "/run/x"}, Recorder(fail=True))
    broken.ready()  # Reporting to systemd must never stop the station.
    assert not broken.enabled


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or os.name != "posix",
                    reason="Unix datagram sockets only")
def test_notifier_real_socket(tmp_path):
    path = str(tmp_path / "notify")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(path)
        SystemdNotifier({"NOTIFY_SOCKET": path}).ready("ok")
        assert server.recv(1024) == b"READY=1\nSTATUS=ok"


def test_status_line():
    assert status_line({"session": None}) == "No session open"
    line = status_line({"session": {"session_id": "S1"},
                        "counters": {"inserted": 3, "uploading": 1, "uploaded": 2, "failed": 0},
                        "problems": [{}]})
    assert line == "Session S1: 3 inserted, 1 uploading, 2 uploaded, 0 failed, 1 problem(s)"


def _daemon(tmp_path) -> Daemon:
    config = tmp_path / "config.yaml"
    config.write_text("station:\n  poll_seconds: 0.2\n  simulator_dir: sim\n"
                      "api:\n  enabled: false\n", encoding="utf-8")
    (tmp_path / "sim").mkdir()
    return Daemon(App.open(str(config)))


def _run(daemon: Daemon, seconds: float) -> None:
    thread = threading.Thread(target=daemon.run, kwargs={"max_seconds": seconds})
    thread.start()
    thread.join(60)


def test_daemon_reports_ready_watchdog_and_stopping(tmp_path):
    daemon = _daemon(tmp_path)
    recorder = Recorder()
    daemon.systemd = SystemdNotifier({"NOTIFY_SOCKET": "@x", "WATCHDOG_USEC": "2000000"},
                                     recorder)
    _run(daemon, 3.0)
    kinds = [message.split("\n")[0] for message in recorder.messages]
    assert kinds[0] == "READY=1" and kinds[-1] == "STOPPING=1"
    assert "WATCHDOG=1" in kinds
    assert "STATUS=No session open" in recorder.messages[0]


def test_stuck_loop_stops_feeding_the_watchdog(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_module, "STUCK_AFTER_SECONDS", -1)  # Every pass counts as stuck.
    daemon = _daemon(tmp_path)
    recorder = Recorder()
    daemon.systemd = SystemdNotifier({"NOTIFY_SOCKET": "@x", "WATCHDOG_USEC": "2000000"},
                                     recorder)
    _run(daemon, 2.5)
    assert not any(message.startswith("WATCHDOG=1") for message in recorder.messages)


# ------------------------------------------------------------------ unit and desktop files

def _exec_args(text: str, key: str) -> list[str]:
    line = next(line for line in text.splitlines() if line.startswith(key + "="))
    argv = shlex.split(line.split("=", 1)[1])
    return argv[argv.index(next(a for a in argv if a.endswith("main.py"))) + 1:]


@pytest.mark.parametrize(("name", "key"), [
    ("sdu-daemon.service", "ExecStart"),
    ("kiosk/sdu-kiosk.service", "ExecStart"),
    ("sd-uploader-console.desktop", "Exec"),
    ("sd-uploader-indicator.desktop", "Exec"),
])
def test_every_installed_command_is_a_real_cli_command(name, key):
    args = build_parser().parse_args(_exec_args((LINUX / name).read_text(), key))
    assert args.config == "/etc/sd-mass-uploader/config.yaml"


def test_service_and_kiosk_settings():
    service = (LINUX / "sdu-daemon.service").read_text()
    for line in ("Type=notify", "WatchdogSec=120", "Restart=always", "KillSignal=SIGTERM",
                 "TimeoutStopSec=600", "User=sdu", "ProtectSystem=strict"):
        assert line in service
    kiosk = (LINUX / "kiosk" / "sdu-kiosk.service").read_text()
    for line in ("User=sdu-kiosk", "TTYPath=/dev/tty1", "Conflicts=getty@tty1.service",
                 "Restart=always", "cage -d -s --", "--window --fullscreen"):
        assert line in kiosk
    script = (ROOT / "scripts" / "ubuntu" / "kiosk.sh").read_text()
    assert "Type 'yes'" in script and "previous-target" in script
    assert "set-default \"$previous\"" in script
    assert script.startswith("#!/usr/bin/env bash") and "set -euo pipefail" in script


def test_kiosk_window_options_parse():
    args = build_parser().parse_args(["desktop", "--window", "--fullscreen"])
    assert args.window and args.fullscreen and sys.executable
