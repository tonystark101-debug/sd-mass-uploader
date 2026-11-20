"""The daemon loop, state file, single-instance lock and the station/simulate CLI."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from src.cli.commands import main
from src.cli.station_commands import render_station
from src.cli.wiring import App
from src.core.exceptions import ConfigurationError
from src.station.daemon import Daemon, SingleInstance, read_state

CONFIG = """
google_cloud:
  project_id: "REPLACE_WITH_PROJECT_ID"
  bucket_name: "REPLACE_WITH_BUCKET_NAME"
station:
  poll_seconds: 0.2
  simulator_dir: "sim"
api:
  port: {port}
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG.format(port=free_port()), encoding="utf-8")
    (tmp_path / "session.json").write_text(json.dumps({
        "activity_name": "Survey", "recording_date": "2026-11-19", "project_name": "Alpha",
        "place_of_recording": "Pune", "operator_name": "Op",
    }), encoding="utf-8")
    return path


def test_simulate_cli_creates_removes_and_inserts(config, tmp_path, capsys):
    base = ["--config", str(config), "simulate"]
    assert main([*base, "create", "A", "--files", "2", "--size-kib", "1"]) == 0
    assert len(list((tmp_path / "sim" / "A" / "recordings").iterdir())) == 2
    assert main([*base, "remove", "A"]) == 0
    assert (tmp_path / "sim" / "A.ejected").is_dir()
    assert main([*base, "insert", "A"]) == 0
    assert main([*base, "insert", "A"]) == 2  # Not removed: a clear error, not a crash.


def test_daemon_scans_cards_and_reports_gcs_problem_in_state(config, tmp_path):
    main(["--config", str(config), "simulate", "create", "A", "--files", "2", "--size-kib", "1"])
    app = App.open(str(config))
    daemon = Daemon(app, metadata_file=str(tmp_path / "session.json"))
    thread = threading.Thread(target=daemon.run, kwargs={"max_seconds": 30})
    thread.start()
    try:
        state_path = tmp_path / "data" / "station" / "state.json"
        deadline = time.monotonic() + 15
        state = None
        while time.monotonic() < deadline:
            state = read_state(state_path)
            if state and state["cards"] and state["cards"][0]["state"] == "QUEUED":
                break
            time.sleep(0.1)
        assert state and state["cards"][0]["state"] == "QUEUED", state
        assert state["counters"]["inserted"] == 1
        assert "google_cloud" in (state["engine"]["store_error"] or "")
        assert state["daemon"]["running"] is True

        with pytest.raises(ConfigurationError, match="already running"):
            SingleInstance(tmp_path / "data" / "station" / "station.lock").acquire()

        text = render_station(state, 0.2)
        assert "Inserted 1" in text and "sim-A" in text and "(E-CONFIG)" in text
    finally:
        daemon.request_stop()
        thread.join(30)
    assert read_state(tmp_path / "data" / "station" / "state.json")["daemon"]["running"] is False


def test_station_cli_sends_commands_to_the_running_daemon(config, tmp_path, capsys):
    app = App.open(str(config))
    daemon = Daemon(app)
    thread = threading.Thread(target=daemon.run, kwargs={"max_seconds": 30})
    thread.start()
    try:
        base = ["--config", str(config), "station"]
        assert main([*base, "open", "--metadata-file", str(tmp_path / "session.json")]) == 0
        assert "ok: SESSION_" in capsys.readouterr().out
        assert main([*base, "pause-all"]) == 0
        assert main([*base, "pause", "sim-missing"]) == 1
        assert "Unknown card" in capsys.readouterr().out
        time.sleep(0.5)
        assert main([*base, "status"]) == 0
        assert "ALL PAUSED" in capsys.readouterr().out
        assert main([*base, "close", "--wait", "20"]) == 0
    finally:
        daemon.request_stop()
        thread.join(30)


def test_status_without_daemon_and_stale_state(config, tmp_path, capsys):
    assert main(["--config", str(config), "station", "status"]) == 1
    old = {"updated_at": "2026-01-01T00:00:00+00:00", "daemon": {"running": True},
           "engine": {}, "session": None, "issues": []}
    assert "NOT RESPONDING" in render_station(old, 2)
