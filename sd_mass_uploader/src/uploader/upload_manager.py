"""Bounded concurrent uploads. This thread alone writes state; workers only return outcomes."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from src.core.enums import SessionStatus, TaskStatus
from src.core.exceptions import LineLostError, SourceDisconnectedError
from src.core.settings import Settings
from src.core.timeutil import utc_now
from src.database.repositories import SessionRepository, Task
from src.network.lines import LinePool
from src.uploader.card_locator import CardLocator
from src.uploader.gcs_client import ObjectStore
from src.uploader.progress import ProgressTracker, ProgressView
from src.uploader.retry_policy import Disposition, backoff_delay, classify
from src.uploader.worker import TaskOutcome, UploadContext, upload_task

logger = logging.getLogger(__name__)

_TICK_SECONDS = 0.5
_HEARTBEAT_SECONDS = 10.0
_MAX_THREADS = 64  # Threads start only as needed; the live limit is the lines' capacity.


@dataclass
class RunControl:
    """`stop`: finish files in progress, start no more. `cancel`: also abort those files."""

    stop: threading.Event = field(default_factory=threading.Event)
    cancel: threading.Event = field(default_factory=threading.Event)


@dataclass
class RunResult:
    session_id: str
    stopped: bool = False
    abort_reason: str | None = None
    abort_error: str | None = None  # Exception class behind abort_reason.
    lost_cards: set[str] = field(default_factory=set)
    elapsed_seconds: float = 0.0


class UploadManager:
    def __init__(self, settings: Settings, sessions: SessionRepository, store: ObjectStore,
                 on_tick: Callable[[ProgressView], None] | None = None,
                 lines: LinePool | None = None) -> None:
        self._settings = settings
        self._sessions = sessions
        self._store = store
        self._on_tick = on_tick
        self._lines = lines

    def run(self, session_id: str, locator: CardLocator, control: RunControl | None = None,
            overwrite_reason: str | None = None,
            keep_alive: Callable[[], bool] | None = None,
            progress: ProgressTracker | None = None) -> RunResult:
        """Upload until the queue is empty, or, with `keep_alive`, until it returns False.

        The station uses `keep_alive` so new cards can join a running session, and passes its
        own `progress` so it can grow the totals as cards arrive.
        """
        control = control or RunControl()
        if progress is None:
            counts = self._sessions.status_counts(session_id).get(TaskStatus.PENDING.value, {})
            progress = ProgressTracker(counts.get("files", 0), counts.get("bytes", 0))
        metadata = self._object_metadata(session_id)
        context = UploadContext(
            settings=self._settings.upload,
            store=self._store,
            locator=locator,
            progress=progress,
            cancel=control.cancel,
            metadata=metadata,
            compute_sha256=self._settings.file_scanning.calculate_sha256,
            overwrite_reason=overwrite_reason,
            lines=self._lines,
        )
        result = RunResult(session_id)
        self._sessions.claim(session_id, os.getpid())
        self._sessions.set_status(session_id, SessionStatus.UPLOADING)
        started = time.perf_counter()  # The default clock can tick only every 15 ms on Windows.
        try:
            self._loop(session_id, context, control, result, keep_alive)
        finally:
            result.elapsed_seconds = time.perf_counter() - started
            self._sessions.release(session_id, result.elapsed_seconds)
        result.stopped = control.stop.is_set() and result.abort_reason is None
        return result

    def _object_metadata(self, session_id: str) -> dict[str, str]:
        values = self._sessions.metadata(session_id)
        allowed = self._settings.upload.object_metadata_fields
        return {key: values[key] for key in allowed if key in values}

    def _loop(self, session_id: str, context: UploadContext, control: RunControl,
              result: RunResult, keep_alive: Callable[[], bool] | None = None) -> None:
        in_flight: dict[Future[TaskOutcome], Task] = {}
        last_beat = time.monotonic()
        threads = _MAX_THREADS if self._lines is not None else self._settings.upload.max_workers
        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="upload") as pool:
            while True:
                if self._sessions.stop_requested(session_id):
                    control.stop.set()
                # With several lines the limit follows them: a line plugged in adds its slots.
                workers = (self._lines.capacity() if self._lines is not None
                           else self._settings.upload.max_workers)
                halting = control.stop.is_set() or result.abort_reason is not None
                if not halting and len(in_flight) < workers:
                    free = workers - len(in_flight)
                    for task in self._sessions.next_ready(session_id, free):
                        context.progress.started()
                        future = pool.submit(upload_task, task, context)
                        in_flight[future] = task
                if not in_flight:
                    if halting:
                        return
                    delay = self._sessions.next_delay(session_id)
                    if delay is None:
                        if keep_alive is None or not keep_alive():
                            return
                        delay = _TICK_SECONDS  # Idle: wait for new cards or a resume.
                    control.stop.wait(min(max(delay, 0.01), _TICK_SECONDS))
                else:
                    done, _ = wait(in_flight, timeout=_TICK_SECONDS,
                                   return_when=FIRST_COMPLETED)
                    for future in done:
                        del in_flight[future]
                        self._record(future.result(), context, result)
                if self._on_tick:
                    self._on_tick(context.progress.view())
                if time.monotonic() - last_beat > _HEARTBEAT_SECONDS:
                    self._sessions.heartbeat(session_id)
                    last_beat = time.monotonic()

    def _record(self, outcome: TaskOutcome, context: UploadContext, result: RunResult) -> None:
        task = outcome.task
        started = outcome.started_at or utc_now()
        if outcome.error is None:
            self._record_success(outcome, context)
            self._sessions.add_attempt(task.task_id, started, outcome.seconds, outcome.bytes_read,
                                       "OK", None, None)
            return
        error = outcome.error
        name = type(error).__name__
        message = str(error)[:1000]
        self._sessions.add_attempt(task.task_id, started, outcome.seconds, outcome.bytes_read,
                                   "FAILED", name, message)
        logger.warning("%s attempt %s failed: %s: %s", task.relative_path, task.attempts, name,
                       message)
        progress = context.progress
        progress.rewind_bytes(outcome.bytes_counted)
        if isinstance(error, LineLostError):
            # Its line was unplugged: not the file's fault. Straight back in the queue, no retry
            # used, and another line picks it up.
            self._sessions.update_task(task.task_id, TaskStatus.PENDING, error_class=name,
                                       last_error=message, attempts=task.attempts - 1)
            progress.released()
            return
        disposition = classify(error)
        if disposition is Disposition.RETRY and task.attempts <= self._settings.upload.max_retries:
            upload = self._settings.upload
            delay = backoff_delay(task.attempts, upload.backoff_initial_seconds,
                                  upload.backoff_max_seconds)
            self._sessions.update_task(task.task_id, TaskStatus.PENDING, error_class=name,
                                       last_error=message, next_attempt_at=time.time() + delay)
            progress.retried()
            progress.released()
        elif disposition is Disposition.STOPPED:
            self._sessions.update_task(task.task_id, TaskStatus.PENDING,
                                       attempts=task.attempts - 1)
            progress.released()
        elif disposition is Disposition.ABORT_SESSION:
            self._sessions.update_task(task.task_id, TaskStatus.PENDING, error_class=name,
                                       last_error=message, attempts=task.attempts - 1)
            progress.released()
            if result.abort_reason is None:
                result.abort_reason, result.abort_error = message, name
        else:
            self._sessions.update_task(task.task_id, _FINAL_STATUS.get(disposition,
                                                                       TaskStatus.FAILED),
                                       error_class=name, last_error=message)
            progress.finished(False)
            if isinstance(error, SourceDisconnectedError):
                self._lose_card(task, message, context, result)

    def _record_success(self, outcome: TaskOutcome, context: UploadContext) -> None:
        task, remote = outcome.task, outcome.remote
        status = TaskStatus.VERIFIED if outcome.verified else TaskStatus.UPLOADED_UNVERIFIED
        self._sessions.update_task(
            task.task_id, status,
            object_name=outcome.object_name,
            local_crc32c=outcome.local_crc32c,
            local_sha256=outcome.local_sha256,
            remote_generation=remote.generation if remote else None,
            uploaded_at=utc_now(),
            error_class=None,
            last_error=outcome.note,
        )
        if outcome.verification and remote is not None:
            self._sessions.add_verification(
                task.task_id, task.size_bytes, remote.size, outcome.local_crc32c, remote.crc32c,
                outcome.verification, "post-upload-crc32c",
            )
        context.progress.finished(True)

    def _lose_card(self, task: Task, message: str, context: UploadContext,
                   result: RunResult) -> None:
        """Stop this card's queue; other cards carry on.

        Not skipped for a card already lost: in station mode a card can return and be lost again.
        A repeat call finds nothing pending and changes nothing.
        """
        result.lost_cards.add(task.card_id)
        count = self._sessions.fail_card_pending(task.session_id, task.card_id, message)
        logger.error("Card %s lost; %s queued files failed", task.card_id, count)
        for _ in range(count):
            context.progress.started()
            context.progress.finished(False)


_FINAL_STATUS = {
    Disposition.FAIL_PERMANENT: TaskStatus.FAILED_PERMANENT,
    Disposition.CONFLICT: TaskStatus.CONFLICT,
}
