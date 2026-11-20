"""The station: one open session that cards join as they are inserted, uploaded in the background.

Threads:
- The daemon loop calls `update_presence` and `handle_commands`.
- A small pool scans cards and settles interrupted files, so the loop never blocks.
- One engine thread runs `UploadManager` in keep-alive mode while a session is open.
SQLite holds every fact; this class keeps only what the OS tells it right now (which cards are in).
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

from src.advisor.advisor import AdvisorFacts, ProblemTracker, as_dicts, evaluate
from src.core.enums import CardPhase, CardState, ScanStatus, SessionStatus, TaskStatus
from src.core.exceptions import ConfigurationError, MetadataValidationError, UploaderError
from src.core.models import Card, ScanSummary
from src.core.settings import Settings
from src.core.timeutil import age_seconds, utc_now
from src.database.repositories import ScanRepository, SessionRepository
from src.database.station_repository import CardCounts, StationRepository, card_from_json
from src.inventory.inventory_service import ScanService
from src.station.card_state import StationCounters, count_cards, derive_state
from src.station.events import EventBus
from src.station.profile import GenericProfile, QueueOutcome, UploadProfile
from src.station.sources import PollResult
from src.uploader.card_locator import CardLocator
from src.uploader.finalize import SessionFinalizer
from src.uploader.gcs_client import ObjectStore
from src.uploader.progress import ProgressTracker
from src.uploader.recovery import settle_uncertain
from src.uploader.upload_manager import RunControl, RunResult, UploadManager

logger = logging.getLogger(__name__)

_OWNER_ALIVE_SECONDS = 60
_STORE_RETRY_SECONDS = 30.0
_SCAN_THREADS = 4
_DISK_CHECK_SECONDS = 30.0

# A card in one of these states still has work to do before the session can close cleanly.
UNFINISHED_STATES = frozenset({
    CardState.NEEDS_CONFIRMATION, CardState.SCANNING, CardState.NEEDS_DECISION,
    CardState.READY, CardState.QUEUED,
    CardState.UPLOADING, CardState.PAUSING, CardState.PAUSED, CardState.REMOVED,
})


class StationError(UploaderError):
    """A station command that cannot be carried out, with a reason the operator can act on."""


class StationNotFound(StationError):
    """The session or card named in a command does not exist."""


@dataclass
class CardView:
    card_id: str
    label: str | None
    state: CardState
    present: bool
    paused: bool
    reason: str | None
    files_done: int
    files_total: int
    bytes_done: int
    bytes_total: int
    files_failed: int
    mount_point: str | None
    reader_model: str | None
    filesystem: str | None
    capacity_bytes: int | None
    link_speed_mbps: float | None
    inserted_at: str


@dataclass
class StationView:
    updated_at: str
    session: dict | None
    counters: StationCounters
    cards: list[CardView]
    progress: dict[str, Any]
    engine: dict[str, Any]
    issues: list[dict] = field(default_factory=list)  # Raw device issues (also in problems).
    waiting_cards: list[dict] = field(default_factory=list)  # Inserted, but no session is open.
    problems: list[dict] = field(default_factory=list)  # Plain-language, most severe first.
    destination: dict = field(default_factory=dict)  # Where uploads go (profile's view).

    def to_dict(self) -> dict:
        return asdict(self)


def _owner_alive(pid: int | None) -> bool:
    """Is the process that last uploaded this session still running? After a crash or a power
    cut it is not, so the restarted station can take over at once instead of waiting a minute."""
    if not pid or pid == os.getpid():
        return False
    if os.name != "posix":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _windows_process_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False  # No such process (or it is gone).
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class Station:
    def __init__(self, settings: Settings, sessions: SessionRepository, scans: ScanRepository,
                 station: StationRepository, store_factory: Callable[[], ObjectStore],
                 finalizer_factory: Callable[[ObjectStore | None], SessionFinalizer],
                 bus: EventBus | None = None, profile: UploadProfile | None = None) -> None:
        self._settings = settings
        self.profile: UploadProfile = profile or GenericProfile(settings, sessions, scans)
        self._housekeeping = False
        self._sessions = sessions
        self._scans = scans
        self._repo = station
        self._store_factory = store_factory
        self._finalizer_factory = finalizer_factory
        self.bus = bus or EventBus()
        self._lock = threading.RLock()
        self._scan_service = ScanService(scans, settings.file_scanning)
        self._scan_pool = ThreadPoolExecutor(max_workers=_SCAN_THREADS,
                                             thread_name_prefix="station-scan")
        self._locator = CardLocator({})
        self._present: dict[str, Card] = {}  # Session card_id -> where it is now.
        self._waiting: dict[str, Card] = {}  # Inserted with no session open.
        self._issues: list = []  # SourceIssue from the latest poll.
        self._tracker = ProblemTracker(station, self.bus)
        self._engine_error_class: str | None = None
        self._store_error_class: str | None = None
        self._disk: tuple[float, tuple[int, int] | None] = (-1e9, None)
        self._last_states: dict[str, CardState] = {}
        self._store: ObjectStore | None = None
        self._store_error: str | None = None
        self._store_failed_at = -1e9
        self._engine: threading.Thread | None = None
        self._control = RunControl()
        self._progress = ProgressTracker(0, 0)
        self._engine_problem: str | None = None
        self._closing = False
        self._busy_scans: set[str] = set()

    # ------------------------------------------------------------------ lifecycle

    def restore(self) -> str | None:
        """After a restart: files that were mid-upload are uncertain, and nothing is known to be
        inserted until the first poll. Returns the open session ID, if any."""
        session = self._repo.current()
        if session is None:
            return None
        session_id = session["session_id"]
        interrupted = self._sessions.mark_interrupted(session_id)
        for scan in self._scans.for_session(session_id):
            if scan["status"] == ScanStatus.RUNNING.value:
                try:  # Cut short by the restart; the card is scanned again when seen.
                    self._repo.discard_scan(scan["scan_id"])
                except ValueError:
                    logger.warning("Interrupted scan %s already has queued files",
                                   scan["scan_id"])
        for row in self._repo.cards(session_id):
            if row["present"]:
                self._repo.set_present(session_id, row["card_id"], False)
            if row["phase"] == CardPhase.SCANNING.value:
                # The scan died with the old process; it restarts when the card is seen.
                self._repo.set_phase(session_id, row["card_id"], CardPhase.SCANNING,
                                     "scan interrupted; it restarts when the card is seen")
        logger.info("Restored station session %s (%s interrupted files)", session_id,
                    interrupted)
        self.bus.publish("session_restored", session_id=session_id, interrupted=interrupted)
        return session_id

    def shutdown(self, timeout: float | None = None) -> None:
        """Finish the files in flight, then stop. Nothing is marked done that is not."""
        self._control.stop.set()
        engine = self._engine
        if engine is not None:
            engine.join(timeout)
        self._scan_pool.shutdown(wait=True, cancel_futures=True)

    # ------------------------------------------------------------------ session commands

    def open_session(self, metadata: dict, auto_upload: bool | None = None) -> str:
        with self._lock:
            if self._repo.current() is not None:
                raise StationError("A session is already open. End it before starting another.")
            try:
                created = self.profile.create_session(metadata)
            except MetadataValidationError as exc:
                raise StationError(f"Session details are not valid: {exc}") from exc
            auto = self._settings.station.auto_upload if auto_upload is None else auto_upload
            self._repo.open_session(created.session_id, auto)
            self._closing = False
            self._engine_problem = None
            self._engine_error_class = None
            self._control = RunControl()
            self._progress = ProgressTracker(0, 0)
            self.bus.publish("session_opened", session_id=created.session_id,
                             prefix=created.prefix, auto_upload=auto, notes=created.notes)
            waiting = list(self._waiting.values())
            self._waiting.clear()
        if waiting:  # Cards already inserted join the new session.
            self.update_presence(PollResult(waiting + list(self._present.values())))
        return created.session_id

    def close_session(self, force: bool = False) -> str:
        """End intake. Unfinished cards block this unless `force` is given."""
        with self._lock:
            session = self._require_session()
            session_id = session["session_id"]
            unfinished = [view.card_id for view in self._card_views(session_id)
                          if view.state in UNFINISHED_STATES]
            if unfinished and not force:
                raise StationError(
                    f"{len(unfinished)} card(s) are not finished: {', '.join(unfinished[:5])}. "
                    "Finish or remove them, or end the session with force."
                )
            self._closing = True
            self._control.stop.set()
            engine = self._engine
        if engine is not None:
            engine.join()
        with self._lock:
            status = self._finalize(session_id, stopped=bool(unfinished))
            self._repo.close_session(session_id)
            self._present.clear()
            self._last_states.clear()
            self._locator = CardLocator({})
            self._engine = None
            self._closing = False
            self.bus.publish("session_closed", session_id=session_id, status=status.value)
            return status.value

    def _finalize(self, session_id: str, stopped: bool) -> SessionStatus:
        store = self._get_store()
        try:
            finalizer = self._finalizer_factory(store)
            status, _ = finalizer.finalize(RunResult(session_id, stopped=stopped))
            return status
        except UploaderError as exc:
            logger.error("Could not finalize %s: %s", session_id, exc)
            self._sessions.set_status(session_id, SessionStatus.STOPPED, str(exc))
            return SessionStatus.STOPPED

    # ------------------------------------------------------------------ card commands

    def pause_card(self, card_id: str) -> None:
        self._set_paused(card_id, True)

    def resume_card(self, card_id: str) -> None:
        self._set_paused(card_id, False)

    def _set_paused(self, card_id: str, paused: bool) -> None:
        with self._lock:
            session_id = self._require_card(card_id)["session_id"]
            self._repo.set_paused(session_id, card_id, paused)
            self.bus.publish("card_paused" if paused else "card_resumed", card_id=card_id)

    def pause_all(self) -> None:
        self._set_paused_all(True)

    def resume_all(self) -> None:
        self._set_paused_all(False)

    def _set_paused_all(self, paused: bool) -> None:
        with self._lock:
            session_id = self._require_session()["session_id"]
            self._repo.set_paused_all(session_id, paused)
            self.bus.publish("paused_all" if paused else "resumed_all", session_id=session_id)

    def start_card(self, card_id: str) -> None:
        """Release a READY card to the uploader (when automatic upload is off)."""
        with self._lock:
            row = self._require_card(card_id)
            if row["phase"] != CardPhase.READY.value:
                raise StationError(f"{card_id} is not waiting to start")
            self._repo.set_phase(row["session_id"], card_id, CardPhase.QUEUED)
            self.bus.publish("card_started", card_id=card_id)

    def start_all(self) -> int:
        with self._lock:
            session_id = self._require_session()["session_id"]
            ready = [row for row in self._repo.cards(session_id)
                     if row["phase"] == CardPhase.READY.value]
            for row in ready:
                self._repo.set_phase(session_id, row["card_id"], CardPhase.QUEUED)
            return len(ready)

    def confirm_card(self, card_id: str) -> None:
        """The operator says this device really is a card to upload."""
        with self._lock:
            row = self._require_card(card_id)
            if row["phase"] != CardPhase.NEEDS_CONFIRMATION.value:
                raise StationError(f"{card_id} does not need confirmation")
            if not row["present"]:
                raise StationError(f"{card_id} is not inserted")
            card = self._present[card_id]
            self._sessions.add_card(row["session_id"], card_id, card.mount_point,
                                    card.fingerprint, card.volume_label, card.reader_model,
                                    False)
            self._repo.set_phase(row["session_id"], card_id, CardPhase.SCANNING)
            self._submit_scan(row["session_id"], card_id, card)

    def retry_card(self, card_id: str) -> int:
        """Queue the card's failed files again. Conflicts are left for a separate decision."""
        with self._lock:
            row = self._require_card(card_id)
            session_id = row["session_id"]
            if row["phase"] == CardPhase.NEEDS_DECISION.value:
                self._decide(row, {})  # Check again, e.g. after PeakAI added a pass.
                return 0
            if row["phase"] == CardPhase.SCAN_FAILED.value:
                if not row["present"]:
                    raise StationError(f"{card_id} is not inserted")
                self._repo.set_phase(session_id, card_id, CardPhase.SCANNING)
                self._submit_scan(session_id, card_id, self._present[card_id])
                return 0
            count = self._repo.requeue_card_failed(session_id, card_id)
            self.bus.publish("card_retry", card_id=card_id, files=count)
            return count

    def place_card(self, card_id: str, device: str | None = None, skip: bool = False) -> str:
        """Answer a held card: which device recorded its unplaced files, or skip them."""
        if not device and not skip:
            raise StationError("Choose the device that recorded the files, or skip them")
        with self._lock:
            row = self._require_card(card_id)
            if row["phase"] != CardPhase.NEEDS_DECISION.value:
                raise StationError(f"{card_id} is not waiting for a decision")
            return self._decide(row, {"device": device, "skip": skip})

    def _decide(self, row: dict, decision: dict) -> str:
        session_id, card_id = row["session_id"], row["card_id"]
        outcome = self.profile.decide(session_id, self._present.get(card_id), card_id, decision)
        self._apply_outcome(session_id, card_id, outcome, None)
        return "still waiting: " + outcome.held_reason if outcome.held_reason else "placed"

    def retry_reports(self) -> None:
        with self._lock:
            self.profile.retry_reports(self._require_session()["session_id"])

    def set_auto_upload(self, enabled: bool) -> None:
        with self._lock:
            self._repo.set_auto_upload(self._require_session()["session_id"], enabled)

    def resume_engine(self) -> None:
        """Try uploading again after a problem that stopped it (sign-in, permission, config)."""
        with self._lock:
            self._engine_problem = self._engine_error_class = None
            self._store_error = self._store_error_class = None
            self._store_failed_at = -1e9

    def _require_session(self) -> dict:
        session = self._repo.current()
        if session is None:
            raise StationNotFound("No session is open")
        return session

    def _require_card(self, card_id: str) -> dict:
        session = self._require_session()
        row = self._repo.card(session["session_id"], card_id)
        if row is None:
            raise StationNotFound(f"Unknown card: {card_id}")
        return row

    # ------------------------------------------------------------------ presence

    def update_presence(self, poll: PollResult) -> None:
        """Reconcile what is inserted now with what the session knows."""
        with self._lock:
            self._issues = list(poll.issues)
            session = self._repo.current()
            if session is None:
                self._waiting = {card.card_id: card for card in poll.cards}
                return
            session_id = session["session_id"]
            rows = {row["card_id"]: row for row in self._repo.cards(session_id)}
            by_fingerprint: dict[str, str] = {}
            for card_id, row in rows.items():
                fingerprint = card_from_json(row["card_json"]).fingerprint
                if fingerprint:
                    by_fingerprint.setdefault(fingerprint, card_id)
            by_mount = {card.mount_point: card_id for card_id, card in self._present.items()}
            repeats = Counter(card.fingerprint for card in poll.cards if card.fingerprint)

            seen: set[str] = set()
            for card in poll.cards:
                duplicate = bool(card.fingerprint and repeats[card.fingerprint] > 1)
                known = None if duplicate else by_fingerprint.get(card.fingerprint or "")
                if known is None and not card.fingerprint:
                    known = by_mount.get(card.mount_point)  # Unidentifiable: same slot only.
                if known is not None and known not in seen:
                    seen.add(known)
                    self._seen_known(session_id, rows[known], card)
                else:
                    seen.add(self._admit(session, card, rows, duplicate))
            for card_id, row in rows.items():
                if row["present"] and card_id not in seen:
                    self._removed(session_id, card_id)
            self._publish_state_changes(session_id)

    def _seen_known(self, session_id: str, row: dict, card: Card) -> None:
        card_id = row["card_id"]
        moved = card_id in self._present and self._present[card_id].mount_point != card.mount_point
        self._present[card_id] = card
        self._locator.set(card_id, card.mount_point)
        if row["present"] and not moved:
            return
        self._repo.set_present(session_id, card_id, True, card)
        if not row["present"]:
            self.bus.publish("card_returned", card_id=card_id, mount_point=card.mount_point)
            if row["phase"] == CardPhase.SCANNING.value:
                self._submit_scan(session_id, card_id, card)
            else:
                requeued = self._repo.requeue_lost(session_id, card_id)
                self._scan_pool.submit(self._settle_card, session_id, card_id)
                if requeued:
                    self.bus.publish("card_resumed_after_removal", card_id=card_id,
                                     files=requeued)

    def _admit(self, session: dict, card: Card, rows: dict[str, dict], duplicate: bool) -> str:
        session_id = session["session_id"]
        card_id, number = card.card_id, 1
        while card_id in rows:  # Another card already used this ID in the session.
            number += 1
            card_id = f"{card.card_id}-{number}"
        if card_id != card.card_id:
            card = Card(**{**asdict(card), "card_id": card_id})
        reasons = list(card.ambiguity)
        if duplicate and not any("serial" in reason for reason in reasons):
            reasons.append("another inserted card reports the same serial")
        phase = CardPhase.NEEDS_CONFIRMATION if reasons else CardPhase.SCANNING
        self._repo.add_card(session_id, card, phase, bool(session["paused_all"]),
                            "; ".join(reasons) or None)
        rows[card_id] = self._repo.card(session_id, card_id)
        self._present[card_id] = card
        self._locator.set(card_id, card.mount_point)
        self.bus.publish("card_inserted", card_id=card_id, label=card.volume_label,
                         mount_point=card.mount_point, needs_confirmation=bool(reasons))
        if phase is CardPhase.SCANNING:
            self._sessions.add_card(session_id, card_id, card.mount_point, card.fingerprint,
                                    card.volume_label, card.reader_model, False)
            self._submit_scan(session_id, card_id, card)
        return card_id

    def _removed(self, session_id: str, card_id: str) -> None:
        self._repo.set_present(session_id, card_id, False)
        self._present.pop(card_id, None)
        self._locator.remove(card_id, "card was removed")
        self.bus.publish("card_removed", card_id=card_id)

    # ------------------------------------------------------------------ scanning

    def _submit_scan(self, session_id: str, card_id: str, card: Card) -> None:
        if card_id in self._busy_scans:
            return
        self._busy_scans.add(card_id)
        self._scan_pool.submit(self._scan, session_id, card_id, card)

    def _scan(self, session_id: str, card_id: str, card: Card) -> None:
        try:
            self._scan_inner(session_id, card_id, card)
        except Exception as exc:  # noqa: BLE001 - one card's failure must not stop the station
            logger.exception("Scan of %s failed", card_id)
            self._repo.set_phase(session_id, card_id, CardPhase.SCAN_FAILED, f"Scan failed: {exc}")
        finally:
            with self._lock:
                self._busy_scans.discard(card_id)
                self._publish_state_changes(session_id)

    def _scan_inner(self, session_id: str, card_id: str, card: Card) -> None:
        self.bus.publish("card_scanning", card_id=card_id)
        summary = self._scan_service.scan_card(card_id, card.mount_point, session_id)
        if summary.status == ScanStatus.DISCONNECTED.value:
            self._repo.discard_scan(summary.scan_id)  # Partial listings are never uploaded.
            with self._lock:
                still_in = card_id in self._present
            if still_in:  # The card stopped answering but is still listed: needs a person.
                self._repo.set_phase(session_id, card_id, CardPhase.SCAN_FAILED,
                                     "the card stopped responding during the scan")
            else:
                self._repo.set_phase(session_id, card_id, CardPhase.SCANNING,
                                     "removed during the scan; put it back to scan again")
            return
        if summary.status == ScanStatus.FAILED.value:
            self._repo.set_phase(session_id, card_id, CardPhase.SCAN_FAILED,
                                 "the card could not be read")
            return
        outcome = self.profile.queue_card(session_id, card, summary)
        self._apply_outcome(session_id, card_id, outcome, summary)

    def _apply_outcome(self, session_id: str, card_id: str, outcome: QueueOutcome,
                       summary: ScanSummary | None) -> None:
        with self._lock:
            row = self._repo.card(session_id, card_id)
            if outcome.held_reason:
                self._repo.set_phase(session_id, card_id, CardPhase.NEEDS_DECISION,
                                     outcome.held_reason)
                self.bus.publish("card_needs_decision", card_id=card_id,
                                 reason=outcome.held_reason)
                return
            current = self._repo.current()
            auto = bool(current and current["auto_upload"])
            self._progress.add_planned(outcome.planned_files, outcome.planned_bytes)
            unreadable = summary.unreadable_count if summary else 0
            note = None
            if unreadable or outcome.rejected:
                note = (f"{unreadable + outcome.rejected} file(s) could not be "
                        "read or named safely; see the card's errors")
            self._repo.set_phase(session_id, card_id,
                                 CardPhase.QUEUED if auto else CardPhase.READY, note)
            if row and not row["present"]:
                self._locator.remove(card_id, "card was removed")
            self.bus.publish("card_scanned", card_id=card_id, files=outcome.planned_files,
                             bytes=outcome.planned_bytes,
                             errors=summary.error_count if summary else 0)

    def _settle_card(self, session_id: str, card_id: str) -> None:
        """A returning card can prove whether its interrupted files reached GCS."""
        tasks = [task for task in self._sessions.tasks_with_status(session_id,
                                                                    [TaskStatus.UNCERTAIN])
                 if task.card_id == card_id]
        if not tasks:
            return
        store = self._get_store()
        if store is None:
            return  # Tried again the next time the card returns, or by `retry`.
        try:
            result = settle_uncertain(self._sessions, store, self._locator, tasks)
            self.bus.publish("card_settled", card_id=card_id, **asdict(result))
        except UploaderError as exc:
            logger.warning("Could not settle %s: %s", card_id, exc)

    # ------------------------------------------------------------------ engine

    def _get_store(self) -> ObjectStore | None:
        with self._lock:
            if self._store is not None:
                return self._store
            if time.monotonic() - self._store_failed_at < _STORE_RETRY_SECONDS:
                return None
            try:
                store = self._store_factory()
                store.validate_access()
            except UploaderError as exc:
                self._store_error = f"{type(exc).__name__}: {exc}"
                self._store_error_class = type(exc).__name__
                self._store_failed_at = time.monotonic()
                return None
            self._store, self._store_error, self._store_error_class = store, None, None
            return store

    def ensure_engine(self) -> None:
        """Start the uploader when a session is open and it is not already running."""
        with self._lock:
            session = self._repo.current()
            if session is not None and not self._housekeeping \
                    and not self._control.stop.is_set():
                self._housekeeping = True  # _info files and reports, off the daemon's thread.
                self._scan_pool.submit(self._run_housekeeping, session["session_id"])
            if session is None or self._closing or self._engine_problem:
                return
            if self._engine is not None and self._engine.is_alive():
                return
            if self._control.stop.is_set():
                return  # Shutting down.
            session_id = session["session_id"]
            record = self._sessions.get(session_id)
            beat = record["heartbeat_at"] if record else None
            if beat and age_seconds(beat) < _OWNER_ALIVE_SECONDS \
                    and _owner_alive(record["owner_pid"]):
                self._engine_problem = (f"Another process ({record['owner_pid']}) is uploading "
                                        "this session")
                self._engine_error_class = "SessionBusy"
                return
        store = self._get_store()
        if store is None:
            return
        with self._lock:
            pending = self._sessions.status_counts(session_id).get(TaskStatus.PENDING.value, {})
            if self._progress.view().files_successful == 0 and pending:
                self._progress = ProgressTracker(pending.get("files", 0), pending.get("bytes", 0))
            self._engine = threading.Thread(target=self._run_engine, args=(session_id, store),
                                            name="station-engine", daemon=True)
            self._engine.start()

    def _run_housekeeping(self, session_id: str) -> None:
        try:
            self.profile.housekeeping(session_id)
        except Exception:  # noqa: BLE001 - reported in the log; tried again next tick
            logger.exception("Housekeeping failed")
        finally:
            self._housekeeping = False

    def _run_engine(self, session_id: str, store: ObjectStore) -> None:
        manager = UploadManager(self._settings, self._sessions, store)
        self.bus.publish("engine_started", session_id=session_id)
        try:
            result = manager.run(session_id, self._locator, self._control,
                                 keep_alive=self._keep_alive, progress=self._progress)
        except UploaderError as exc:
            result = RunResult(session_id, abort_reason=str(exc),
                               abort_error=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - reported, and the station keeps running
            logger.exception("Upload engine crashed")
            result = RunResult(session_id, abort_reason=f"Internal error: {exc}",
                               abort_error=type(exc).__name__)
        with self._lock:
            if result.abort_reason:
                self._engine_problem = result.abort_reason
                self._engine_error_class = result.abort_error
                self._sessions.set_error(session_id, result.abort_reason)
            self.bus.publish("engine_stopped", session_id=session_id,
                             reason=result.abort_reason)

    def _keep_alive(self) -> bool:
        return not self._closing and not self._control.stop.is_set()

    # ------------------------------------------------------------------ commands from the queue

    def handle_commands(self) -> None:
        for command in self._repo.take_commands():
            try:
                result = self.execute(command["command"], command["args"])
            except (StationError, ConfigurationError) as exc:
                result = f"refused: {exc}"
            except Exception as exc:  # noqa: BLE001 - a bad command must not stop the station
                logger.exception("Command %s failed", command["command"])
                result = f"error: {exc}"
            self._repo.finish_command(command["command_id"], result)

    def execute(self, name: str, args: dict) -> str:
        """Run one command. Used by the command queue now and by the API later."""
        card = args.get("card_id")
        actions: dict[str, Callable[[], object]] = {
            "open": lambda: self.open_session(args.get("metadata") or {},
                                              args.get("auto_upload")),
            "close": lambda: self.close_session(bool(args.get("force"))),
            "pause": lambda: self.pause_card(card),
            "resume": lambda: self.resume_card(card),
            "pause_all": self.pause_all,
            "resume_all": self.resume_all,
            "start": lambda: self.start_card(card),
            "start_all": self.start_all,
            "confirm": lambda: self.confirm_card(card),
            "retry": lambda: self.retry_card(card),
            "auto_upload": lambda: self.set_auto_upload(bool(args.get("enabled"))),
            "resume_engine": self.resume_engine,
            "place": lambda: self.place_card(card, args.get("device"), bool(args.get("skip"))),
            "retry_reports": self.retry_reports,
        }
        if name not in actions:
            raise StationError(f"Unknown command: {name}")
        if name in {"pause", "resume", "start", "confirm", "retry", "place"} and not card:
            raise StationError(f"{name} needs a card ID")
        value = actions[name]()
        return f"ok{'' if value is None else f': {value}'}"

    # ------------------------------------------------------------------ views

    def _card_views(self, session_id: str) -> list[CardView]:
        counts = self._repo.card_counts(session_id)
        views = []
        for row in self._repo.cards(session_id):
            card = card_from_json(row["card_json"])
            item = counts.get(row["card_id"], CardCounts())
            state = derive_state(CardPhase(row["phase"]), bool(row["present"]),
                                 bool(row["paused"]), item)
            done_statuses = (TaskStatus.VERIFIED.value, TaskStatus.UPLOADED_UNVERIFIED.value)
            failed = sum(item.files.get(status.value, 0) for status in (
                TaskStatus.FAILED, TaskStatus.FAILED_PERMANENT, TaskStatus.CONFLICT))
            views.append(CardView(
                card_id=row["card_id"],
                label=card.volume_label,
                state=state,
                present=bool(row["present"]),
                paused=bool(row["paused"]),
                reason=row["reason"],
                files_done=sum(item.files.get(status, 0) for status in done_statuses),
                files_total=item.total_files(),
                bytes_done=sum(item.bytes.get(status, 0) for status in done_statuses),
                bytes_total=item.total_bytes(),
                files_failed=failed,
                mount_point=card.mount_point if row["present"] else None,
                reader_model=card.reader_model,
                filesystem=card.filesystem,
                capacity_bytes=card.capacity_bytes,
                link_speed_mbps=card.negotiated_speed_mbps,
                inserted_at=row["inserted_at"],
            ))
        return views

    def _publish_state_changes(self, session_id: str) -> None:
        for view in self._card_views(session_id):
            if self._last_states.get(view.card_id) != view.state:
                self._last_states[view.card_id] = view.state
                self.bus.publish("card_state", card_id=view.card_id, state=view.state.value,
                                 reason=view.reason)

    def check_cloud(self) -> dict:
        """Try the bucket now, ignoring the retry delay. For the AI troubleshooter and setup."""
        try:
            store = self._store_factory()
            store.validate_access()
        except UploaderError as exc:
            return {"ok": False, "error_class": type(exc).__name__, "error": str(exc)}
        return {"ok": True}

    def card_detail(self, card_id: str, status: str | None = None, limit: int = 200,
                    offset: int = 0) -> dict:
        """One card for the console's drawer: its view, files and scan errors."""
        with self._lock:
            row = self._require_card(card_id)
            session_id = row["session_id"]
            view = next(item for item in self._card_views(session_id)
                        if item.card_id == card_id)
        return {
            "card": asdict(view),
            "files": self._repo.card_files(session_id, card_id, status, limit, offset),
            "file_status_counts": self._repo.card_counts(session_id).get(
                card_id, CardCounts()).files,
            "scan_errors": self._repo.card_scan_errors(session_id, card_id, limit=200),
        }

    def recent_events(self, after: int = 0) -> list[dict]:
        return [asdict(event) for event in self.bus.recent(after)]

    def view(self) -> StationView:
        with self._lock:
            session = self._repo.current()
            engine = {
                "running": bool(self._engine and self._engine.is_alive()),
                "problem": self._engine_problem,
                "store_error": self._store_error,
                "workers": self._settings.upload.max_workers,
            }
            progress_view = self._progress.view()
            issues = [asdict(issue) for issue in self._issues]
            waiting = [asdict(card) for card in self._waiting.values()]
            if session is None:
                problems = self._problems(None, [])
                return StationView(utc_now(), None, StationCounters(), [], {}, engine,
                                   issues, waiting, problems, self.profile.view(None))
            session_id = session["session_id"]
            self._publish_state_changes(session_id)
            cards = self._card_views(session_id)
            counters = count_cards([(card.state, card.present) for card in cards])
            bytes_total = sum(card.bytes_total for card in cards)
            bytes_done = sum(card.bytes_done for card in cards)
            speed = progress_view.current_mb_per_s
            remaining = max(0, bytes_total - bytes_done)
            progress = {
                "files_total": sum(card.files_total for card in cards),
                "files_done": sum(card.files_done for card in cards),
                "files_failed": sum(card.files_failed for card in cards),
                "files_active": progress_view.files_active,
                "bytes_total": bytes_total,
                "bytes_done": bytes_done,
                "current_mb_per_s": round(speed, 2),
                "average_mb_per_s": round(progress_view.average_mb_per_s, 2),
                "retries": progress_view.retries,
                "eta_seconds": round(remaining / (speed * 1_000_000)) if speed > 0.01 else None,
            }
            record = self._sessions.get(session_id) or {}
            info = {
                "session_id": session_id,
                "status": record.get("status"),
                "opened_at": session["opened_at"],
                "auto_upload": bool(session["auto_upload"]),
                "paused_all": bool(session["paused_all"]),
                "base_prefix": record.get("base_prefix"),
                "metadata": self._sessions.metadata(session_id),
            }
            problems = self._problems(session_id, cards)
            return StationView(utc_now(), info, counters, cards, progress, engine, issues,
                               waiting, problems, self.profile.view(session_id))

    def _problems(self, session_id: str | None, cards: list[CardView]) -> list[dict]:
        facts = AdvisorFacts(
            session_id=session_id,
            cards=cards,
            error_groups=self._repo.error_groups(session_id) if session_id else [],
            scan_groups=self._repo.scan_error_groups(session_id) if session_id else [],
            engine_error=((self._engine_error_class, self._engine_problem)
                          if self._engine_problem else None),
            store_error=((self._store_error_class,
                          self._store_error.removeprefix(f"{self._store_error_class}: "))
                         if self._store_error and session_id else None),
            issues=list(self._issues),
            disk_free=self._disk_free(),
            max_retries=self._settings.upload.max_retries,
            extra=self.profile.problems(session_id),
        )
        return as_dicts(self._tracker.update(session_id, evaluate(facts)))

    def _disk_free(self) -> tuple[int, int] | None:
        checked, value = self._disk
        if time.monotonic() - checked < _DISK_CHECK_SECONDS:
            return value
        try:
            usage = shutil.disk_usage(self._settings.resolve(self._settings.database.path).parent)
            value = (usage.free, usage.total)
        except OSError:
            value = None
        self._disk = (time.monotonic(), value)
        return value

    def problem_history(self, limit: int = 100) -> list[dict]:
        return self._repo.problem_history(limit)
