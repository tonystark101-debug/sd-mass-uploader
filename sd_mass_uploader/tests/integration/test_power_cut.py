"""Power cut: the station process dies mid-upload, starts again, and finishes without duplicates.

Runs the real daemon as its own process (as systemd would) against the local storage protocol
server, and kills it hard, the way a power cut or `kill -9` would.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from src.station.simulator import create_card
from tests.integration.fake_gcs_server import FakeGcsServer

ROOT = Path(__file__).resolve().parents[2]
FILES = 12


def start_station(config: Path, env: dict, metadata: Path | None = None) -> subprocess.Popen:
    argv = [sys.executable, str(ROOT / "main.py"), "--config", str(config), "daemon"]
    if metadata:
        argv += ["--metadata-file", str(metadata)]
    return subprocess.Popen(argv, cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise AssertionError(f"Timed out waiting for {what}")


def card_state(state_file: Path) -> str | None:
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cards = state.get("cards") or []
    return cards[0]["state"] if cards else None


def test_power_cut_mid_upload_resumes_without_duplicates(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        'google_cloud:\n  project_id: "test-project"\n  bucket_name: "test-bucket"\n'
        "upload:\n  max_workers: 2\n  backoff_initial_seconds: 0.1\n"
        "station:\n  poll_seconds: 0.5\n  simulator_dir: sim\napi:\n  enabled: false\n",
        encoding="utf-8")
    metadata = tmp_path / "session.json"
    metadata.write_text(json.dumps({
        "activity_name": "Survey", "recording_date": "2026-11-19", "project_name": "Alpha",
        "place_of_recording": "Pune", "operator_name": "Op"}), encoding="utf-8")
    create_card(tmp_path / "sim", "CARD", FILES, 4096)
    state_file = tmp_path / "data" / "station" / "state.json"

    with FakeGcsServer() as server:
        server.state.upload_delay = 0.8  # Slow enough to cut it off part-way.
        env = {**os.environ, "STORAGE_EMULATOR_HOST": server.url}
        first = start_station(config, env, metadata)
        try:
            wait_for(lambda: len(server.state.objects) >= 3, 90, "the first uploads")
        finally:
            first.kill()  # Power cut: no clean shutdown, files in flight are lost.
            first.wait(30)
        done_before = len(server.state.objects)
        assert done_before < FILES, "the upload finished before the cut; slow it down"
        calls_before = server.state.upload_calls

        server.state.upload_delay = 0.05
        second = start_station(config, env)
        try:
            wait_for(lambda: card_state(state_file) == "UPLOADED", 120,
                     "the restarted station to finish the card")
        finally:
            second.kill()
            second.wait(30)

        names = sorted(name for name in server.state.objects if "/rec_" in name)
        assert len(names) == FILES and len(set(names)) == FILES
        # Only files that were in flight at the cut (at most one per worker) went up twice.
        assert server.state.upload_calls - calls_before <= FILES - done_before + 2

    with sqlite3.connect(tmp_path / "data" / "database" / "uploader.db") as db:
        statuses = dict(db.execute(
            "SELECT status, COUNT(*) FROM upload_tasks GROUP BY status").fetchall())
    assert statuses == {"VERIFIED": FILES}
