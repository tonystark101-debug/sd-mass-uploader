"""Upload engine failure paths, using an in-memory store and real files in temp folders."""

from __future__ import annotations

import os
import threading

import pytest

from src.core.enums import DuplicatePolicy, SessionStatus, TaskStatus
from src.core.exceptions import (
    AuthenticationError,
    GcsPermissionError,
    ObjectExistsError,
    TransientUploadError,
)
from src.core.models import RemoteObject
from src.core.settings import Settings
from src.database.db import Database
from src.database.repositories import ScanRepository, SessionRepository
from src.inventory.inventory_service import folder_card
from src.metadata.session import SessionPlanner
from src.metadata.validation import SessionMetadata
from src.reporting.report_generator import ReportGenerator
from src.uploader.card_locator import CardLocation, CardLocator, locate_after_restart, locate_fresh
from src.uploader.finalize import SessionFinalizer, decide_status
from src.uploader.recovery import ensure_not_running, reconcile
from src.uploader.upload_manager import RunControl, UploadManager
from src.verification.checksums import crc32c_of_bytes
from src.verification.cloud_verifier import CloudVerifier


class FakeStore:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.generation = 10
        self.errors: list[Exception] = []
        self.before_upload = None
        self.after_store_raise: list[Exception] = []
        self.corrupt = False
        self.max_concurrent = 0
        self._active = 0
        self._lock = threading.Lock()
        self.uploads = 0

    def validate_access(self):
        pass

    def _remote(self, name):
        item = self.objects[name]
        return RemoteObject(name, len(item["data"]), crc32c_of_bytes(item["data"]),
                            item["generation"], item["metadata"])

    def get_object(self, name):
        return self._remote(name) if name in self.objects else None

    def _put(self, name, data, metadata, match):
        with self._lock:
            current = self.objects.get(name)
            if match != (current["generation"] if current else 0):
                raise ObjectExistsError(name)
            self.generation += 1
            stored = b"!" + data[1:] if self.corrupt and data else data
            self.objects[name] = {"data": stored, "generation": self.generation,
                                  "metadata": metadata}

    def upload_stream(self, name, stream, size, content_type, metadata, if_generation_match):
        with self._lock:
            self.uploads += 1
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
            error = self.errors.pop(0) if self.errors else None
        try:
            if error:
                raise error
            if self.before_upload:
                self.before_upload(name)
            data = b""
            while block := stream.read(1024):
                data += block
            self._put(name, data, metadata, if_generation_match)
            if self.after_store_raise:
                raise self.after_store_raise.pop(0)
            return self._remote(name)
        finally:
            with self._lock:
                self._active -= 1

    def upload_bytes(self, name, data, content_type, if_generation_match):
        self._put(name, data, {}, if_generation_match)
        return self._remote(name)


class Harness:
    def __init__(self, tmp_path, **upload_options):
        self.settings = Settings(base_dir=tmp_path)
        self.settings.google_cloud.bucket_name = "b"
        self.settings.upload.backoff_initial_seconds = 0.001
        self.settings.upload.backoff_max_seconds = 0.002
        for key, value in upload_options.items():
            setattr(self.settings.upload, key, value)
        self.db = Database(":memory:")
        self.sessions = SessionRepository(self.db)
        self.scans = ScanRepository(self.db)
        self.store = FakeStore()
        self.tmp = tmp_path
        self.metadata = SessionMetadata("Survey", "2026-11-06", "Alpha", "Pune", "Op", "")

    def card(self, name, files):
        folder = self.tmp / name
        folder.mkdir(exist_ok=True)
        for relative, data in files.items():
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return folder_card(str(folder), card_id=name)

    def plan(self, *cards):
        return SessionPlanner(self.settings, self.sessions, self.scans).plan(self.metadata,
                                                                             list(cards))

    def run(self, plan, control=None, locator=None, reason=None):
        locator = locator or locate_fresh(self.sessions.cards(plan.session_id))
        manager = UploadManager(self.settings, self.sessions, self.store)
        return manager.run(plan.session_id, locator, control, reason)

    def finish(self, result):
        reports = ReportGenerator(self.sessions, self.scans, self.tmp / "reports")
        return SessionFinalizer(self.sessions, self.scans, self.store, reports).finalize(result)

    def counts(self, plan):
        return {k: v["files"] for k, v in self.sessions.status_counts(plan.session_id).items()}

    def rows(self, plan):
        return list(self.sessions.iter_task_rows(plan.session_id))


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def test_all_files_verified_with_recorded_checksums(h):
    plan = h.plan(h.card("A", {"x/a.bin": b"aaa", "b.bin": b"bb"}))
    h.run(plan)
    assert h.counts(plan) == {"VERIFIED": 2}
    for row in h.rows(plan):
        assert row["local_crc32c"] == h.store._remote(row["object_name"]).crc32c
        assert row["remote_generation"]


def test_concurrency_is_bounded_and_configurable(tmp_path):
    h = Harness(tmp_path, max_workers=3)
    barrier = threading.Barrier(3, timeout=5)
    h.store.before_upload = lambda _: barrier.wait()
    plan = h.plan(h.card("A", {f"{i}.bin": b"x" for i in range(9)}))
    h.run(plan)
    assert h.counts(plan) == {"VERIFIED": 9}
    assert h.store.max_concurrent == 3


def test_many_cards_are_not_limited_to_twenty(h):
    cards = [h.card(f"C{i}", {"a.bin": b"x"}) for i in range(25)]
    plan = h.plan(*cards)
    h.run(plan)
    assert h.counts(plan) == {"VERIFIED": 25}


def test_transient_error_retries_then_succeeds(h):
    h.store.errors = [TransientUploadError("reset"), TransientUploadError("reset")]
    plan = h.plan(h.card("A", {"a.bin": b"data"}))
    h.run(plan)
    assert h.counts(plan) == {"VERIFIED": 1}
    assert h.sessions.attempt_totals(plan.session_id)["failures"] == 2


def test_retries_stop_at_the_limit(tmp_path):
    h = Harness(tmp_path, max_retries=2)
    h.store.errors = [TransientUploadError("x") for _ in range(10)]
    plan = h.plan(h.card("A", {"a.bin": b"data"}))
    h.run(plan)
    assert h.counts(plan) == {"FAILED": 1}
    assert h.sessions.attempt_totals(plan.session_id)["attempts"] == 3


@pytest.mark.parametrize("error", [AuthenticationError("bad"), GcsPermissionError("no")])
def test_auth_failures_abort_without_retrying_everything(h, error):
    h.settings.upload.max_workers = 1
    h.store.errors = [error]
    plan = h.plan(h.card("A", {f"{i}.bin": b"x" for i in range(20)}))
    result = h.run(plan)
    assert result.abort_reason
    assert h.store.uploads == 1
    assert h.counts(plan) == {"PENDING": 20}
    status, _ = h.finish(result)
    assert status is SessionStatus.FAILED


def test_lost_response_is_accepted_after_checksum_match(h):
    h.store.after_store_raise = [TransientUploadError("response lost")]
    plan = h.plan(h.card("A", {"a.bin": b"data"}))
    h.run(plan)
    assert h.counts(plan) == {"VERIFIED": 1}
    assert "identical" in h.rows(plan)[0]["last_error"]
    assert len(h.store.objects) == 1


def test_different_existing_object_is_a_conflict_and_is_kept(h):
    plan = h.plan(h.card("A", {"a.bin": b"new"}))
    name = h.rows(plan)[0]["object_name"]
    h.store._put(name, b"old content", {}, 0)
    h.run(plan)
    assert h.counts(plan) == {"CONFLICT": 1}
    assert h.store.objects[name]["data"] == b"old content"


def test_same_size_different_content_is_still_a_conflict(h):
    plan = h.plan(h.card("A", {"a.bin": b"new"}))
    name = h.rows(plan)[0]["object_name"]
    h.store._put(name, b"old", {}, 0)
    h.run(plan)
    assert h.counts(plan) == {"CONFLICT": 1}


def test_new_path_policy_keeps_both(tmp_path):
    h = Harness(tmp_path, duplicate_policy=DuplicatePolicy.NEW_PATH)
    plan = h.plan(h.card("A", {"a.bin": b"new"}))
    name = h.rows(plan)[0]["object_name"]
    h.store._put(name, b"old content", {}, 0)
    h.run(plan)
    row = h.rows(plan)[0]
    assert row["status"] == "VERIFIED"
    assert row["object_name"] != name and row["object_name"].endswith(".bin")
    assert h.store.objects[name]["data"] == b"old content"
    assert h.store.objects[row["object_name"]]["data"] == b"new"


def test_overwrite_only_with_a_reason(h):
    plan = h.plan(h.card("A", {"a.bin": b"new"}))
    name = h.rows(plan)[0]["object_name"]
    h.store._put(name, b"old content", {}, 0)
    h.run(plan, reason="operator approved re-shoot")
    assert h.store.objects[name]["data"] == b"new"


def test_corrupted_object_is_never_verified(h):
    h.store.corrupt = True
    plan = h.plan(h.card("A", {"a.bin": b"data"}))
    h.run(plan)
    assert h.counts(plan) == {"CONFLICT": 1}


def test_unverified_mode_does_not_count_as_success(tmp_path):
    h = Harness(tmp_path, verify_after_upload=False)
    plan = h.plan(h.card("A", {"a.bin": b"data"}))
    status, _ = h.finish(h.run(plan))
    assert h.counts(plan) == {"UPLOADED_UNVERIFIED": 1}
    assert status is SessionStatus.FAILED


def test_file_changed_after_scan_is_refused(h):
    card = h.card("A", {"a.bin": b"data"})
    plan = h.plan(card)
    (h.tmp / "A" / "a.bin").write_bytes(b"different length")
    h.run(plan)
    assert h.counts(plan) == {"FAILED_PERMANENT": 1}
    assert h.store.objects == {}


def test_file_deleted_after_scan_is_refused(h):
    plan = h.plan(h.card("A", {"a.bin": b"data"}))
    os.unlink(h.tmp / "A" / "a.bin")
    h.run(plan)
    assert h.counts(plan) == {"FAILED_PERMANENT": 1}


def test_card_pulled_fails_that_card_and_others_continue(tmp_path):
    h = Harness(tmp_path, max_workers=1)
    good = h.card("GOOD", {f"{i}.bin": b"g" for i in range(3)})
    bad = h.card("BAD", {f"{i}.bin": b"b" for i in range(3)})
    plan = h.plan(bad, good)
    locations = {
        "GOOD": CardLocation("GOOD", good.mount_point),
        "BAD": CardLocation("BAD", bad.mount_point),
    }
    locator = CardLocator(locations)

    class VanishingStream(FakeStore):
        def upload_stream(self, name, stream, *args):
            if "/BAD/" in name and not hasattr(self, "pulled"):
                self.pulled = True
                locations["BAD"] = CardLocation("BAD", None, "card pulled")
            return super().upload_stream(name, stream, *args)

    h.store = VanishingStream()
    result = h.run(plan, locator=locator)
    assert result.lost_cards == {"BAD"}
    rows = h.rows(plan)
    assert all(r["status"] == "VERIFIED" for r in rows if r["card_id"] == "GOOD")
    assert sum(r["status"] == "VERIFIED" for r in rows if r["card_id"] == "BAD") <= 1
    assert any(r["status"] == "FAILED" and r["card_id"] == "BAD" for r in rows)
    status, _ = h.finish(result)
    assert status is SessionStatus.PARTIAL_SUCCESS


def test_read_error_mid_file_is_not_retried_as_network_fault(h, monkeypatch):
    class DyingFile:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _):
            raise OSError(1167, "device not connected")

        def seek(self, *_):
            return 0

    monkeypatch.setattr("src.uploader.worker.open_source_readonly", lambda _: DyingFile())
    plan = h.plan(h.card("A", {"a.bin": b"data"}))
    h.run(plan)
    assert h.counts(plan) == {"FAILED": 1}
    assert h.store.objects == {}


def test_graceful_stop_leaves_rest_pending_and_retry_finishes(tmp_path):
    h = Harness(tmp_path, max_workers=1)
    plan = h.plan(h.card("A", {f"{i}.bin": b"x" for i in range(5)}))
    control = RunControl()
    h.store.before_upload = lambda _: control.stop.set()
    result = h.run(plan, control)
    assert result.stopped
    assert h.counts(plan)["VERIFIED"] == 1
    assert h.counts(plan)["PENDING"] == 4
    status, _ = h.finish(result)
    assert status is SessionStatus.STOPPED
    assert h.store.objects.keys() == {r["object_name"] for r in h.rows(plan)
                                      if r["status"] == "VERIFIED"}  # No manifest yet.
    h.store.before_upload = None
    h.run(plan)
    assert h.counts(plan) == {"VERIFIED": 5}


def test_hard_stop_never_completes_a_partial_file(tmp_path):
    h = Harness(tmp_path, max_workers=1)
    plan = h.plan(h.card("A", {"a.bin": b"x"}))
    control = RunControl()
    h.store.before_upload = lambda _: (control.stop.set(), control.cancel.set())
    h.run(plan, control)
    assert h.counts(plan) == {"PENDING": 1}
    assert h.store.objects == {}


def test_stop_command_flag_is_seen_by_running_session(tmp_path):
    h = Harness(tmp_path, max_workers=1)
    plan = h.plan(h.card("A", {f"{i}.bin": b"x" for i in range(4)}))
    h.store.before_upload = lambda _: h.sessions.request_stop(plan.session_id)
    assert h.run(plan).stopped


# --- Recovery ---

def crash_mid_upload(h, plan):
    """Leave the database as a killed process would: claimed tasks, stale heartbeat."""
    return h.sessions.next_ready(plan.session_id, 10)


def test_restart_finds_finished_upload_without_resending(h):
    card = h.card("A", {"done.bin": b"finished", "todo.bin": b"never sent"})
    plan = h.plan(card)
    tasks = crash_mid_upload(h, plan)
    done = next(t for t in tasks if t.relative_path == "done.bin")
    h.store._put(done.object_name, b"finished", {}, 0)
    locator = locate_after_restart(h.sessions.cards(plan.session_id), [])

    result = reconcile(h.sessions, h.store, locator, plan.session_id)

    assert result.interrupted == 2 and result.confirmed == 1 and result.requeued == 1
    h.run(plan, locator=locator)
    assert h.counts(plan) == {"VERIFIED": 2}
    assert h.store.uploads == 1


def test_restart_with_different_remote_object_needs_an_operator(h):
    plan = h.plan(h.card("A", {"a.bin": b"source"}))
    task = crash_mid_upload(h, plan)[0]
    h.store._put(task.object_name, b"unrelated", {}, 0)
    locator = locate_after_restart(h.sessions.cards(plan.session_id), [])
    result = reconcile(h.sessions, h.store, locator, plan.session_id)
    assert result.conflicts == 1
    assert h.counts(plan) == {"CONFLICT": 1}


def test_restart_with_missing_card_stays_uncertain(h):
    plan = h.plan(h.card("A", {"a.bin": b"source"}))
    task = crash_mid_upload(h, plan)[0]
    h.store._put(task.object_name, b"source", {}, 0)
    locator = CardLocator({"A": CardLocation("A", None, "not attached")})
    result = reconcile(h.sessions, h.store, locator, plan.session_id)
    assert result.still_uncertain == 1
    assert h.counts(plan) == {"UNCERTAIN": 1}


def test_active_session_cannot_be_reconciled(h):
    from src.core.exceptions import ConfigurationError

    plan = h.plan(h.card("A", {"a.bin": b"x"}))
    h.sessions.claim(plan.session_id, 1234)
    with pytest.raises(ConfigurationError, match="looks active"):
        ensure_not_running(h.sessions, plan.session_id)


def _cards(fingerprint, mount):
    from src.core.models import Card

    return Card("x", mount, fingerprint)


def test_card_found_again_under_a_new_letter():
    rows = [{"card_id": "c", "fingerprint": "AAAA-1111", "mount_point": "F:\\", "is_folder": 0}]
    locator = locate_after_restart(rows, [_cards("AAAA-1111", "H:\\")])
    assert locator.mount_of("c") == "H:\\"


def test_wrong_card_in_same_slot_is_refused():
    from src.core.exceptions import SourceDisconnectedError

    rows = [{"card_id": "c", "fingerprint": "AAAA-1111", "mount_point": "F:\\", "is_folder": 0}]
    locator = locate_after_restart(rows, [_cards("BBBB-2222", "F:\\")])
    with pytest.raises(SourceDisconnectedError):
        locator.mount_of("c")


def test_two_cards_with_same_serial_are_ambiguous():
    rows = [{"card_id": "c", "fingerprint": "AAAA-1111", "mount_point": "F:\\", "is_folder": 0}]
    locator = locate_after_restart(rows, [_cards("AAAA-1111", "H:\\"), _cards("AAAA-1111", "I:\\")])
    assert not locator.is_present("c")


def test_card_without_fingerprint_cannot_resume():
    rows = [{"card_id": "c", "fingerprint": None, "mount_point": "F:\\", "is_folder": 0}]
    assert not locate_after_restart(rows, []).is_present("c")


# --- Verify and status ---

def test_verifier_flags_changed_and_missing_objects(h):
    plan = h.plan(h.card("A", {"a.bin": b"aa", "b.bin": b"bb", "c.bin": b"cc"}))
    h.run(plan)
    rows = {r["relative_path"]: r["object_name"] for r in h.rows(plan)}
    del h.store.objects[rows["a.bin"]]
    h.store.objects[rows["b.bin"]]["data"] = b"XX"
    result = CloudVerifier(h.sessions, h.store).verify_session(plan.session_id)
    assert (result.checked, result.matched, result.missing, result.mismatched) == (3, 1, 1, 1)
    assert h.counts(plan) == {"VERIFIED": 1, "FAILED": 1, "CONFLICT": 1}


def test_deep_verify_rereads_source(h):
    plan = h.plan(h.card("A", {"a.bin": b"aa"}))
    h.run(plan)
    locator = locate_fresh(h.sessions.cards(plan.session_id))
    result = CloudVerifier(h.sessions, h.store).verify_session(plan.session_id, locator)
    assert result.reread_from_source == 1 and result.matched == 1


def test_deep_verify_with_card_gone_still_checks_remote(h):
    plan = h.plan(h.card("A", {"a.bin": b"aa"}))
    h.run(plan)
    locator = CardLocator({"A": CardLocation("A", None, "gone")})
    result = CloudVerifier(h.sessions, h.store).verify_session(plan.session_id, locator)
    assert result.source_unavailable == 1 and result.matched == 1


def counts_of(**statuses):
    return {key: {"files": value, "bytes": 0} for key, value in statuses.items()}


@pytest.mark.parametrize("counts,errors,stopped,aborted,expected", [
    (counts_of(VERIFIED=3), 0, False, False, SessionStatus.SUCCESS),
    (counts_of(VERIFIED=3), 1, False, False, SessionStatus.PARTIAL_SUCCESS),
    (counts_of(VERIFIED=2, FAILED=1), 0, False, False, SessionStatus.PARTIAL_SUCCESS),
    (counts_of(FAILED=3), 0, False, False, SessionStatus.FAILED),
    (counts_of(VERIFIED=1, PENDING=2), 0, True, False, SessionStatus.STOPPED),
    (counts_of(PENDING=2), 0, False, True, SessionStatus.FAILED),
    (counts_of(VERIFIED=1, PENDING=2), 0, False, True, SessionStatus.PARTIAL_SUCCESS),
    (counts_of(VERIFIED=3), 0, True, False, SessionStatus.SUCCESS),
    (counts_of(UPLOADED_UNVERIFIED=1, VERIFIED=1), 0, False, False, SessionStatus.PARTIAL_SUCCESS),
])
def test_session_status_rules(counts, errors, stopped, aborted, expected):
    assert decide_status(counts, errors, stopped, aborted) is expected


def test_scan_error_prevents_success(h):
    plan = h.plan(h.card("A", {"a.bin": b"x"}))
    h.scans.add_errors(plan.scans[0].scan_id, [("bad.bin", "ENTRY_UNREADABLE", "denied")])
    status, _ = h.finish(h.run(plan))
    assert status is SessionStatus.PARTIAL_SUCCESS


def test_report_figures_come_from_measurements(h):
    plan = h.plan(h.card("A", {"a.bin": b"x" * 100}))
    status, paths = h.finish(h.run(plan))
    report = ReportGenerator(h.sessions, h.scans, h.tmp / "reports").build(plan.session_id)
    assert report["status"] == "SUCCESS" == status.value
    assert report["source_bytes"] == report["verified_bytes"] == report["transferred_bytes"] == 100
    assert report["elapsed_seconds"] is not None
    assert report["average_mb_per_s"] > 0
    assert paths.json_path.exists() and paths.csv_path.exists() and paths.errors_path.exists()


def test_task_status_vocabulary_is_stored_as_text(h):
    plan = h.plan(h.card("A", {"a.bin": b"x"}))
    assert h.rows(plan)[0]["status"] == TaskStatus.PENDING.value
