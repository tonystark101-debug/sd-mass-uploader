"""The whole program as an operator runs it, in a separate process, with folders as cards."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.integration.fake_gcs_server import FakeGcsServer

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def server():
    with FakeGcsServer() as fake:
        yield fake


@pytest.fixture
def workspace(tmp_path):
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    config["google_cloud"].update(project_id="test-project", bucket_name="test-bucket")
    config["upload"].update(backoff_initial_seconds=0.01, backoff_max_seconds=0.02, max_workers=3)
    config["database"]["path"] = str(tmp_path / "state" / "uploader.db")
    config["reporting"]["directory"] = str(tmp_path / "reports")
    config["logging"]["directory"] = str(tmp_path / "logs")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    meta = tmp_path / "session.json"
    meta.write_text(json.dumps({
        "activity_name": "Field Survey", "place_of_recording": "Bengaluru",
        "recording_date": "2026-11-06", "operator_name": "Operator 01",
        "project_name": "Project Alpha", "description": "Test",
    }), encoding="utf-8")
    return tmp_path


def make_card(root: Path, name: str, files: dict[str, bytes]) -> Path:
    card = root / name
    for relative, data in files.items():
        target = card / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return card


def run(server, workspace, *args):
    env = {**os.environ, "STORAGE_EMULATOR_HOST": server.url, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--config", str(workspace / "config.yaml"), *args],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120, cwd=ROOT,
        stdin=subprocess.DEVNULL,
    )


def upload(server, workspace, *cards):
    sources = [item for card in cards for item in ("--source", str(card))]
    return run(server, workspace, "upload", "--metadata-file", str(workspace / "session.json"),
               "--yes", *sources)


def only_report(workspace) -> dict:
    reports = list((workspace / "reports").glob("SESSION_*/report.json"))
    assert len(reports) == 1
    return json.loads(reports[0].read_text(encoding="utf-8"))


def snapshot(folder: Path) -> dict[str, tuple]:
    return {
        str(path): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in folder.rglob("*") if path.is_file()
    }


def test_two_cards_upload_verified_and_sources_untouched(server, workspace):
    big = os.urandom(9 * 1024 * 1024)
    card_a = make_card(workspace, "cardA", {"DCIM/100/a.mp4": b"alpha" * 500, "वीडियो.mp4": b"h"})
    card_b = make_card(workspace, "cardB", {"DCIM/b.bin": big, "empty.txt": b""})
    before = {**snapshot(card_a), **snapshot(card_b)}

    done = upload(server, workspace, card_a, card_b)

    assert done.returncode == 0, done.stdout + done.stderr
    report = only_report(workspace)
    assert report["status"] == "SUCCESS"
    assert report["files_verified"] == report["files_discovered"] == 4
    assert report["source_bytes"] == len(big) + 2501
    assert report["average_mb_per_s"] > 0
    names = list(server.state.objects)
    assert any(n.startswith("uploads/Project_Alpha/2026/11/06/SESSION_") and
               n.endswith("DCIM/100/a.mp4") for n in names)
    assert any(n.endswith("/manifests/session-manifest.json") for n in names)
    assert any(n.endswith("/manifests/card-inventory.json") for n in names)
    assert {**snapshot(card_a), **snapshot(card_b)} == before

    rows = list(csv.DictReader(open(next((workspace / "reports").glob("*/files.csv")),
                                    encoding="utf-8-sig")))
    assert {row["status"] for row in rows} == {"VERIFIED"}
    assert all(row["local_crc32c"] for row in rows)


def test_operator_name_stays_out_of_object_metadata(server, workspace):
    card = make_card(workspace, "card", {"a.txt": b"x"})
    assert upload(server, workspace, card).returncode == 0
    item = next(v for k, v in server.state.objects.items() if k.endswith("a.txt"))
    metadata = item["record"]["metadata"]
    assert metadata["project_name"] == "Project Alpha"
    assert "operator_name" not in metadata


def test_transient_failures_are_retried(server, workspace):
    server.state.fail_next = [503, 503]
    card = make_card(workspace, "card", {"a.txt": b"one", "b.txt": b"two"})
    done = upload(server, workspace, card)
    assert done.returncode == 0, done.stdout + done.stderr
    assert only_report(workspace)["status"] == "SUCCESS"


def test_lost_response_is_confirmed_not_duplicated(server, workspace):
    server.state.lose_next_response = 1
    card = make_card(workspace, "card", {"a.txt": b"content"})
    done = upload(server, workspace, card)
    assert done.returncode == 0, done.stdout + done.stderr
    report = only_report(workspace)
    assert report["files_verified"] == 1


def test_corruption_in_transit_is_not_marked_successful(server, workspace):
    server.state.corrupt_next = 1
    card = make_card(workspace, "card", {"a.bin": b"z" * 1000})
    done = upload(server, workspace, card)
    assert done.returncode == 1
    report = only_report(workspace)
    assert report["status"] == "FAILED"
    assert report["files_verified"] == 0
    assert report["files_conflict"] == 1


def test_permission_denied_stops_before_any_upload(server, workspace):
    server.state.denied_permissions = {"storage.objects.create"}
    card = make_card(workspace, "card", {"a.txt": b"x"})
    done = upload(server, workspace, card)
    assert done.returncode == 2
    assert "storage.objects.create" in done.stderr
    assert server.state.upload_calls == 0


def test_retry_finishes_a_stopped_session(server, workspace):
    card = make_card(workspace, "card", {f"f{i}.txt": b"x" * 10 for i in range(5)})
    server.state.fail_next = [403]  # An authorization failure aborts the session.
    first = upload(server, workspace, card)
    assert first.returncode == 1
    assert only_report(workspace)["files_verified"] < 5  # Files already in flight may finish.

    second = run(server, workspace, "retry")

    assert second.returncode == 0, second.stdout + second.stderr
    assert only_report(workspace)["status"] == "SUCCESS"
    assert only_report(workspace)["files_verified"] == 5


def test_verify_detects_a_deleted_object(server, workspace):
    card = make_card(workspace, "card", {"a.txt": b"x", "b.txt": b"y"})
    assert upload(server, workspace, card).returncode == 0
    victim = next(k for k in server.state.objects if k.endswith("a.txt"))
    del server.state.objects[victim]

    done = run(server, workspace, "verify")

    assert done.returncode == 1
    assert "missing: 1" in done.stdout
    assert only_report(workspace)["status"] == "PARTIAL_SUCCESS"


def test_deep_verify_rereads_folder_cards(server, workspace):
    card = make_card(workspace, "card", {"a.txt": b"x"})
    assert upload(server, workspace, card).returncode == 0
    # Folder cards need no hardware query, but detection still runs; it must not disturb this.
    done = run(server, workspace, "verify", "--deep")
    assert done.returncode == 0, done.stdout + done.stderr


def test_placeholder_bucket_is_refused(server, workspace):
    config = yaml.safe_load((workspace / "config.yaml").read_text(encoding="utf-8"))
    config["google_cloud"]["bucket_name"] = "REPLACE_WITH_BUCKET_NAME"
    (workspace / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    done = upload(server, workspace, make_card(workspace, "card", {"a": b"x"}))
    assert done.returncode == 2
    assert "bucket_name" in done.stderr


def test_gcs_test_command(server, workspace):
    done = run(server, workspace, "gcs-test")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PASSED" in done.stdout
    assert any("_connectivity_test" in name for name in server.state.objects)
