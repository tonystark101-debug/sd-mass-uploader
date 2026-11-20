"""Upload one file. Runs in a pool thread and never touches SQLite: it returns an outcome."""

from __future__ import annotations

import logging
import mimetypes
import os
import threading
import time
from dataclasses import dataclass, field

from src.core.enums import DuplicatePolicy, VerificationStatus
from src.core.exceptions import (
    ObjectExistsError,
    SourceChangedError,
    SourceDisconnectedError,
    SourceReadError,
    VerificationMismatch,
)
from src.core.models import RemoteObject
from src.core.settings import UploadSettings
from src.core.timeutil import utc_now
from src.database.repositories import Task
from src.platform.paths import open_source_readonly
from src.uploader.card_locator import CardLocator
from src.uploader.gcs_client import ObjectStore
from src.uploader.progress import ProgressTracker
from src.verification.checksums import HashingReader, crc32c_of_file
from src.verification.cloud_verifier import compare

logger = logging.getLogger(__name__)


@dataclass
class UploadContext:
    settings: UploadSettings
    store: ObjectStore
    locator: CardLocator
    progress: ProgressTracker
    cancel: threading.Event
    metadata: dict[str, str] = field(default_factory=dict)
    compute_sha256: bool = False
    overwrite_reason: str | None = None


@dataclass
class TaskOutcome:
    """A finished attempt, or `error` set if it failed."""

    task: Task
    verified: bool = False
    object_name: str | None = None
    remote: RemoteObject | None = None
    local_crc32c: str | None = None
    local_sha256: str | None = None
    verification: str | None = None
    note: str | None = None
    bytes_read: int = 0
    bytes_counted: int = 0
    started_at: str | None = None
    seconds: float = 0.0
    error: Exception | None = None


def _stat_source(path: str, task: Task, card_id: str, locator: CardLocator) -> os.stat_result:
    try:
        info = os.stat(path)
    except OSError as exc:
        if not locator.is_present(card_id):
            raise SourceDisconnectedError(f"{card_id} disappeared: {exc}") from exc
        raise SourceChangedError(f"Source file missing: {task.relative_path}") from exc
    if info.st_size != task.size_bytes or (
        task.mtime_ns is not None and info.st_mtime_ns != task.mtime_ns
    ):
        raise SourceChangedError(f"Source changed after scan: {task.relative_path}")
    return info


def _with_dup_suffix(object_name: str, generation: int) -> str:
    folder, _, name = object_name.rpartition("/")
    stem, extension = os.path.splitext(name)
    return f"{folder}/{stem}__dup-{generation}{extension}"


def _object_metadata(task: Task, ctx: UploadContext) -> dict[str, str]:
    values = dict(ctx.metadata)
    values.update(session_id=task.session_id, card_id=task.card_id)
    if task.mtime_ns is not None:
        values["source_mtime_ns"] = str(task.mtime_ns)
    return values


class _Attempt:
    def __init__(self, task: Task, ctx: UploadContext) -> None:
        self.task = task
        self.ctx = ctx
        self.path = ctx.locator.path_for(task.card_id, task.relative_path)
        self.outcome = TaskOutcome(task, object_name=task.object_name)

    def run(self) -> None:
        task, ctx = self.task, self.ctx
        _stat_source(self.path, task, task.card_id, ctx.locator)
        generation = self._resolve_destination(self.outcome.object_name)
        if self.outcome.verified:
            return
        try:
            remote = self._transfer(generation)
        except ObjectExistsError:
            # Usually an earlier attempt finished but its response was lost.
            existing = ctx.store.get_object(self.outcome.object_name)
            if existing is None:
                raise
            self._accept_identical_or_raise(existing)
            return
        _stat_source(self.path, task, task.card_id, ctx.locator)  # Did it change while reading?
        self._finish(remote)

    def _resolve_destination(self, name: str) -> int:
        """Apply the duplicate policy and return the required generation precondition."""
        ctx = self.ctx
        existing = ctx.store.get_object(name)
        if existing is None:
            return 0
        if self._matches_source(existing):
            self._accept(existing, "already present with identical content")
            return 0
        if ctx.overwrite_reason:
            logger.warning("Overwriting %s (generation %s): %s", name, existing.generation,
                           ctx.overwrite_reason)
            return existing.generation
        if ctx.settings.duplicate_policy is DuplicatePolicy.NEW_PATH:
            self.outcome.object_name = _with_dup_suffix(name, existing.generation)
            return self._resolve_destination(self.outcome.object_name)
        raise VerificationMismatch(f"Different object already at {name}")

    def _matches_source(self, existing: RemoteObject) -> bool:
        if existing.size != self.task.size_bytes:
            return False
        try:
            self.outcome.local_crc32c = crc32c_of_file(self.path)
        except OSError as exc:
            raise SourceReadError(f"Cannot read source: {exc}") from exc
        return self.outcome.local_crc32c == existing.crc32c

    def _accept(self, remote: RemoteObject, note: str) -> None:
        self.outcome.remote = remote
        self.outcome.verified = True
        self.outcome.verification = VerificationStatus.MATCH.value
        self.outcome.note = note

    def _accept_identical_or_raise(self, existing: RemoteObject) -> None:
        if not self._matches_source(existing):
            raise VerificationMismatch(f"Different object at {existing.name}")
        self._accept(existing, "confirmed after an interrupted response")

    def _transfer(self, generation: int) -> RemoteObject:
        task, ctx = self.task, self.ctx
        outcome = self.outcome

        def count(number: int) -> None:
            outcome.bytes_counted += number
            ctx.progress.add_bytes(number)

        content_type = mimetypes.guess_type(task.relative_path)[0]
        try:
            handle = open_source_readonly(self.path)
        except OSError as exc:
            if not ctx.locator.is_present(task.card_id):
                raise SourceDisconnectedError(f"{task.card_id} disappeared: {exc}") from exc
            raise SourceReadError(f"Cannot open source: {exc}") from exc
        with handle:
            reader = HashingReader(handle, count, ctx.cancel, ctx.compute_sha256,
                                   lambda: ctx.locator.is_present(task.card_id))
            try:
                remote = ctx.store.upload_stream(
                    outcome.object_name, reader, task.size_bytes, content_type,
                    _object_metadata(task, ctx), generation,
                )
            finally:
                outcome.bytes_read = reader.bytes_read
        if reader.high_water != task.size_bytes:
            raise SourceChangedError(
                f"Read {reader.high_water} of {task.size_bytes} bytes: {task.relative_path}"
            )
        outcome.local_crc32c = reader.crc32c
        outcome.local_sha256 = reader.sha256
        return remote

    def _finish(self, remote: RemoteObject) -> None:
        """Success requires the remote object to match what was actually read."""
        ctx, outcome = self.ctx, self.outcome
        outcome.remote = remote
        if not ctx.settings.verify_after_upload:
            outcome.note = "verification skipped by configuration"
            return
        fresh = ctx.store.get_object(outcome.object_name)
        status = compare(self.task.size_bytes, outcome.local_crc32c, fresh)
        outcome.verification = status.value
        if status is not VerificationStatus.MATCH:
            raise VerificationMismatch(f"{status.value} for {outcome.object_name}")
        outcome.remote = fresh
        outcome.verified = True


def upload_task(task: Task, ctx: UploadContext) -> TaskOutcome:
    """Never raises: failures are returned so one file cannot disturb the others."""
    outcome = TaskOutcome(task)
    started_at, started = utc_now(), time.perf_counter()
    try:
        attempt = _Attempt(task, ctx)
        outcome = attempt.outcome
        attempt.run()
    except Exception as exc:  # noqa: BLE001 - isolation boundary; classified by the manager
        outcome.error = exc
    outcome.started_at = started_at
    outcome.seconds = time.perf_counter() - started
    return outcome
