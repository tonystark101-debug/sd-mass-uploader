"""Station mode: cards arriving and leaving a long-running session, with an in-memory store."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from src.core.enums import AccessStatus, CardPhase, CardState, SessionStatus, TaskStatus
from src.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ReadOnlyViolation,
    SourceDisconnectedError,
)
from src.core.models import Card, InventorySnapshot, StorageDevice, Volume
from src.core.settings import Settings, StationSettings
from src.database.db import Database
from src.database.repositories import ScanRepository, SessionRepository
from src.database.station_repository import CardCounts, StationRepository
from src.platform.process import CommandResult, assert_read_only
from src.reporting.report_generator import ReportGenerator
from src.station.card_state import count_cards, derive_state
from src.station.sources import PollResult, ReadOnlyMounter, SimulatorSource
from src.station.station import Station, StationError
from src.uploader.finalize import SessionFinalizer
from tests.unit.test_upload_engine import FakeStore

METADATA = {
    "activity_name": "Survey",
    "recording_date": "2026-11-19",
    "project_name": "Alpha",
    "place_of_recording": "Pune",
    "operator_name": "Op",
}


class StationHarness:
    def __init__(self, tmp_path: Path, db: Database | None = None, store: FakeStore | None = None,
                 **upload_options):
        self.tmp = tmp_path
        self.settings = Settings(base_dir=tmp_path)
        self.settings.google_cloud.bucket_name = "b"
        self.settings.upload.backoff_initial_seconds = 0.001
        self.settings.upload.backoff_max_seconds = 0.002
        for key, value in upload_options.items():
            setattr(self.settings.upload, key, value)
        self.db = db or Database(tmp_path / "station.db")
        self.sessions = SessionRepository(self.db)
        self.scans = ScanRepository(self.db)
        self.repo = StationRepository(self.db)
        self.store = store or FakeStore()
        self.root = tmp_path / "sim"
        self.root.mkdir(exist_ok=True)
        self.source = SimulatorSource(self.root)
        reports = ReportGenerator(self.sessions, self.scans, tmp_path / "reports")
        self.station = Station(
            self.settings, self.sessions, self.scans, self.repo, lambda: self.store,
            lambda store: SessionFinalizer(self.sessions, self.scans, store, reports),
        )
        self.extra: list[Card] = []

    def card(self, name: str, count: int = 3, size: int = 2000) -> None:
        folder = self.root / name
        (folder / "rec").mkdir(parents=True)
        for number in range(count):
            (folder / "rec" / f"f{number}.bin").write_bytes(os.urandom(size))

    def remove(self, name: str) -> None:
        os.replace(self.root / name, self.root / f"{name}.ejected")

    def insert(self, name: str) -> None:
        os.replace(self.root / f"{name}.ejected", self.root / name)

    def tick(self) -> None:
        poll = self.source.poll()
        poll.cards.extend(self.extra)
        self.station.update_presence(poll)
        self.station.handle_commands()
        self.station.ensure_engine()

    def states(self) -> dict[str, CardState]:
        return {card.card_id: card.state for card in self.station.view().cards}

    def wait_for(self, predicate, timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.tick()
            if predicate():
                return
            time.sleep(0.05)
        raise AssertionError(f"Timed out; states: {self.states()}; "
                             f"engine: {self.station.view().engine}")

    def wait_states(self, **expected: CardState):
        wanted = {f"sim-{name}": state for name, state in expected.items()}
        self.wait_for(lambda: all(self.states().get(k) == v for k, v in wanted.items()))

    def close(self):
        self.station.shutdown(timeout=10)


@pytest.fixture
def harness(tmp_path):
    h = StationHarness(tmp_path)
    yield h
    h.close()


# ------------------------------------------------------------------ state derivation

def _counts(**files: int) -> CardCounts:
    return CardCounts(files={key.upper(): value for key, value in files.items()},
                      bytes={key.upper(): value * 10 for key, value in files.items()})


@pytest.mark.parametrize(("phase", "present", "paused", "counts", "expected"), [
    (CardPhase.SCANNING, True, False, _counts(), CardState.SCANNING),
    (CardPhase.SCANNING, False, False, _counts(), CardState.REMOVED),
    (CardPhase.NEEDS_CONFIRMATION, True, False, _counts(), CardState.NEEDS_CONFIRMATION),
    (CardPhase.SCAN_FAILED, True, False, _counts(), CardState.NEEDS_ATTENTION),
    (CardPhase.QUEUED, True, False, _counts(), CardState.NO_FILES),
    (CardPhase.READY, True, False, _counts(pending=3), CardState.READY),
    (CardPhase.QUEUED, True, False, _counts(pending=3), CardState.QUEUED),
    (CardPhase.QUEUED, True, False, _counts(active=1, pending=2), CardState.UPLOADING),
    (CardPhase.QUEUED, True, True, _counts(active=1, pending=2), CardState.PAUSING),
    (CardPhase.QUEUED, True, True, _counts(pending=2, verified=1), CardState.PAUSED),
    (CardPhase.QUEUED, False, False, _counts(pending=2, verified=1), CardState.REMOVED),
    (CardPhase.QUEUED, False, False, _counts(verified=3), CardState.UPLOADED),
    (CardPhase.QUEUED, True, False, _counts(verified=3), CardState.UPLOADED),
    (CardPhase.QUEUED, True, False, _counts(verified=2, conflict=1),
     CardState.UPLOADED_WITH_ISSUES),
    (CardPhase.QUEUED, True, False, _counts(failed=3), CardState.FAILED),
])
def test_derive_state(phase, present, paused, counts, expected):
    assert derive_state(phase, present, paused, counts) is expected


def test_lost_files_mean_removed_while_out_and_scan_errors_block_uploaded():
    lost = _counts(verified=2, failed=1)
    lost.lost_files = 1
    assert derive_state(CardPhase.QUEUED, False, False, lost) is CardState.REMOVED
    blocked = _counts(verified=3)
    blocked.blocking_scan_errors = 1
    assert derive_state(CardPhase.QUEUED, True, False, blocked) \
        is CardState.UPLOADED_WITH_ISSUES


def test_counters_have_exact_meanings():
    counters = count_cards([
        (CardState.UPLOADED, True), (CardState.UPLOADED, False), (CardState.UPLOADING, True),
        (CardState.PAUSING, True), (CardState.PAUSED, True), (CardState.FAILED, True),
        (CardState.NEEDS_ATTENTION, True), (CardState.UPLOADED_WITH_ISSUES, False),
        (CardState.REMOVED, False), (CardState.QUEUED, True),
    ])
    assert (counters.inserted, counters.total) == (7, 10)
    assert (counters.uploaded, counters.uploading, counters.paused) == (2, 2, 1)
    assert (counters.failed, counters.removed, counters.queued) == (3, 1, 1)


# ------------------------------------------------------------------ station flows

def test_cards_join_and_upload_then_session_closes_successfully(harness):
    session_id = harness.station.open_session(METADATA)
    harness.card("A", 3)
    harness.card("B", 2)
    harness.wait_states(A=CardState.UPLOADED, B=CardState.UPLOADED)
    view = harness.station.view()
    assert view.counters.uploaded == 2 and view.counters.inserted == 2
    assert view.progress["files_done"] == 5 and view.progress["bytes_done"] == 10000
    assert len(harness.store.objects) == 5
    assert all("/sim-A/rec/" in name or "/sim-B/rec/" in name for name in harness.store.objects)
    assert harness.station.close_session() == SessionStatus.SUCCESS.value
    assert harness.repo.current() is None
    assert harness.sessions.get(session_id)["status"] == SessionStatus.SUCCESS.value
    assert any("/manifests/" in name for name in harness.store.objects)


def test_card_inserted_before_a_session_waits_then_joins(harness):
    harness.card("A", 2)
    harness.tick()
    view = harness.station.view()
    assert view.session is None and len(view.waiting_cards) == 1
    harness.station.open_session(METADATA)
    harness.wait_states(A=CardState.UPLOADED)


def test_manual_start_and_per_card_pause(harness):
    harness.station.open_session(METADATA, auto_upload=False)
    harness.card("A", 2)
    harness.card("B", 2)
    harness.wait_states(A=CardState.READY, B=CardState.READY)
    assert harness.store.uploads == 0
    harness.station.pause_card("sim-A")
    assert harness.station.start_all() == 2
    harness.wait_states(A=CardState.PAUSED, B=CardState.UPLOADED)
    assert harness.station.view().counters.paused == 1
    assert not any("/sim-A/" in name for name in harness.store.objects)
    harness.station.resume_card("sim-A")
    harness.wait_states(A=CardState.UPLOADED)


def test_pause_all_applies_to_cards_inserted_later(harness):
    harness.station.open_session(METADATA)
    harness.station.pause_all()
    harness.card("A", 2)
    harness.wait_states(A=CardState.PAUSED)
    time.sleep(0.3)
    assert harness.store.uploads == 0
    harness.station.resume_all()
    harness.wait_states(A=CardState.UPLOADED)


def test_card_removed_before_upload_resumes_when_put_back(harness):
    harness.station.open_session(METADATA, auto_upload=False)
    harness.card("A", 3)
    harness.wait_states(A=CardState.READY)
    harness.remove("A")
    harness.wait_states(A=CardState.REMOVED)
    view = harness.station.view()
    assert (view.counters.removed, view.counters.inserted) == (1, 0)
    harness.station.start_card("sim-A")
    time.sleep(0.3)
    harness.tick()
    assert harness.store.uploads == 0  # Held while it is out.
    harness.insert("A")
    harness.wait_states(A=CardState.UPLOADED)


def test_card_lost_mid_upload_resumes_its_files_when_reinserted(tmp_path):
    h = StationHarness(tmp_path, max_workers=1)
    try:
        h.store.errors = [SourceDisconnectedError("device gone")]
        h.station.open_session(METADATA)
        h.card("A", 3)
        h.wait_for(lambda: h.repo.card_counts(
            h.repo.current()["session_id"]).get("sim-A", CardCounts()).lost_files == 3)
        h.remove("A")
        h.wait_states(A=CardState.REMOVED)
        h.insert("A")
        h.wait_states(A=CardState.UPLOADED)
        assert len(h.store.objects) == 3
    finally:
        h.close()


def test_device_that_may_not_be_a_card_waits_for_confirmation(harness):
    folder = harness.tmp / "stick"
    (folder / "x").mkdir(parents=True)
    (folder / "x" / "a.bin").write_bytes(b"data")
    harness.extra = [Card("card-STICK", str(folder), "STICK", "STICK",
                          ambiguity=["may be a USB drive rather than a card reader"])]
    harness.station.open_session(METADATA)
    harness.wait_for(lambda: harness.states().get("card-STICK") is CardState.NEEDS_CONFIRMATION)
    assert harness.station.view().counters.needs_confirmation == 1
    assert harness.store.uploads == 0
    harness.station.confirm_card("card-STICK")
    harness.wait_for(lambda: harness.states().get("card-STICK") is CardState.UPLOADED)


def test_cards_sharing_a_serial_both_need_confirmation(harness):
    folders = []
    for name in ("c1", "c2"):
        folder = harness.tmp / name
        folder.mkdir()
        (folder / "a.bin").write_bytes(name.encode())
        folders.append(folder)
    harness.extra = [Card("card-X", str(folders[0]), "SERIAL"),
                     Card("card-X-2", str(folders[1]), "SERIAL")]
    harness.station.open_session(METADATA)
    harness.wait_for(lambda: len(harness.states()) == 2)
    assert set(harness.states().values()) == {CardState.NEEDS_CONFIRMATION}


def test_close_with_unfinished_cards_needs_force(harness):
    harness.station.open_session(METADATA, auto_upload=False)
    harness.card("A", 2)
    harness.wait_states(A=CardState.READY)
    with pytest.raises(StationError, match="not finished"):
        harness.station.close_session()
    assert harness.station.close_session(force=True) == SessionStatus.STOPPED.value


def test_only_one_session_and_metadata_is_validated(harness):
    with pytest.raises(StationError, match="not valid"):
        harness.station.open_session({"activity_name": "x"})
    harness.station.open_session(METADATA)
    with pytest.raises(StationError, match="already open"):
        harness.station.open_session(METADATA)


def test_sign_in_failure_stops_uploads_until_resumed(harness):
    harness.store.errors = [AuthenticationError("token expired")]
    harness.station.open_session(METADATA)
    harness.card("A", 2)
    harness.wait_for(lambda: harness.station.view().engine["problem"] is not None)
    assert "token expired" in harness.station.view().engine["problem"]
    harness.station.resume_engine()
    harness.wait_states(A=CardState.UPLOADED)


def test_restart_settles_interrupted_files_and_continues(tmp_path):
    first = StationHarness(tmp_path)
    session_id = first.station.open_session(METADATA, auto_upload=False)
    first.card("A", 3)
    first.wait_states(A=CardState.READY)
    first.close()
    task = next(first.sessions.tasks_with_status(session_id, [TaskStatus.PENDING]))
    first.sessions.update_task(task.task_id, TaskStatus.ACTIVE)  # Mid-upload at the "crash".

    second = StationHarness(tmp_path, db=first.db, store=first.store)
    try:
        assert second.station.restore() == session_id
        assert second.repo.card(session_id, "sim-A")["present"] == 0
        second.wait_for(lambda: second.sessions.status_counts(session_id).get(
            TaskStatus.UNCERTAIN.value) is None and second.repo.card(session_id, "sim-A")[
            "present"] == 1)
        second.station.start_card("sim-A")
        second.wait_states(A=CardState.UPLOADED)
        assert len(second.store.objects) == 3
    finally:
        second.close()


def test_command_queue_round_trip(harness):
    harness.station.open_session(METADATA)
    ok = harness.repo.enqueue("pause_all")
    bad = harness.repo.enqueue("format_card", {"card_id": "x"})
    missing = harness.repo.enqueue("pause")
    harness.station.handle_commands()
    assert harness.repo.command(ok)["result"] == "ok"
    assert harness.repo.command(bad)["result"].startswith("refused: Unknown command")
    assert "needs a card ID" in harness.repo.command(missing)["result"]
    assert harness.repo.current()["paused_all"] == 1
    assert harness.repo.take_commands() == []


# ------------------------------------------------------------------ read-only mounting

def test_only_read_only_udisks_mounts_are_allowed():
    good = ["udisksctl", "mount", "--block-device", "/dev/sdb1", "--options",
            "ro,noatime,nosuid,nodev,noexec", "--no-user-interaction"]
    assert_read_only(good)
    for bad in (
        ["udisksctl", "unmount", "--block-device", "/dev/sdb1"],
        ["udisksctl", "power-off", "--block-device", "/dev/sdb"],
        [*good[:5], "rw,noatime", good[6]],
        [*good[:5], "noatime", good[6]],
        [*good[:5], "ro,rw", good[6]],
        [*good[:3], "sdb1", *good[4:]],
        [*good, "--extra"],
    ):
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(bad)


def test_station_settings_reject_writable_mount_options(tmp_path):
    from src.core.settings import validate

    settings = Settings(base_dir=tmp_path)
    settings.station.mount_options = "rw,noatime"
    with pytest.raises(ConfigurationError, match="must include ro"):
        validate(settings)


class RecordingRunner:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def run(self, argv, timeout=60):
        assert_read_only(argv)
        self.calls.append(argv)
        return CommandResult(self.returncode, "Mounted /dev/sdb1 at /media/sdu/CARD",
                             "" if self.returncode == 0 else "Error mounting: busy")


def _snapshot(bus="USB", fs="exfat", mounted=False, media=True):
    device = StorageDevice("sto-1", bus_type=bus, media_present=media)
    volume = Volume("vol-1", storage_id="sto-1", filesystem=fs, is_mounted=mounted,
                    mount_point="/media/x" if mounted else None,
                    access_status=AccessStatus.OK if mounted else AccessStatus.NOT_MOUNTED,
                    raw={"path": "/dev/sdb1"})
    return InventorySnapshot("snap", "now", "linux", storage_devices=[device], volumes=[volume])


def test_mounter_mounts_external_cards_read_only():
    runner = RecordingRunner()
    mounter = ReadOnlyMounter(StationSettings(), runner)
    attempted, problems = mounter.mount_new(_snapshot())
    assert attempted == ["/dev/sdb1"] and problems == []
    assert runner.calls[0][5].split(",")[0] == "ro"


@pytest.mark.parametrize("snapshot", [
    _snapshot(bus="SATA"), _snapshot(mounted=True), _snapshot(media=False), _snapshot(bus=None),
])
def test_mounter_ignores_internal_mounted_or_empty_devices(snapshot):
    runner = RecordingRunner()
    assert ReadOnlyMounter(StationSettings(), runner).mount_new(snapshot) == ([], [])
    assert runner.calls == []


def test_mounter_skips_other_filesystems_and_limits_retries():
    runner = RecordingRunner(returncode=1)
    clock = [0.0]
    mounter = ReadOnlyMounter(StationSettings(), runner, clock=lambda: clock[0])
    _, problems = mounter.mount_new(_snapshot(fs="ext4"))
    assert runner.calls == [] and "not opened automatically" in problems[0].message
    assert problems[0].code == "W-FS-UNSUPPORTED"
    for _ in range(5):
        _, problems = mounter.mount_new(_snapshot())
        clock[0] += 61
    assert len(runner.calls) == 3
    assert "after 3 tries" in problems[0].message and problems[0].code == "E-MOUNT-FAILED"


def test_simulator_lists_inserted_folders_only(tmp_path):
    for name in ("A", "B.ejected", ".hidden"):
        (tmp_path / name).mkdir()
    (tmp_path / "file.txt").write_text("x")
    cards = SimulatorSource(tmp_path).poll().cards
    assert [card.card_id for card in cards] == ["sim-A"]
    assert cards[0].fingerprint == "sim:A"
    assert SimulatorSource(tmp_path / "missing").poll().issues


def test_poll_result_defaults():
    assert PollResult([]).issues == [] and PollResult([]).mounting == []
