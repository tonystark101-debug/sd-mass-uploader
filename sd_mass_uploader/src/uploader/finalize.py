"""Decide the session's status from recorded facts, then write manifests and reports."""

from __future__ import annotations

import json
import logging

from src.core.enums import SessionStatus, TaskStatus
from src.core.exceptions import UploaderError
from src.database.repositories import ScanRepository, SessionRepository
from src.reporting.report_generator import ReportGenerator, ReportPaths, count_blocking_errors
from src.uploader.gcs_client import ObjectStore
from src.uploader.upload_manager import RunResult

logger = logging.getLogger(__name__)


def decide_status(counts: dict[str, dict[str, int]], blocking_scan_errors: int,
                  stopped: bool, aborted: bool) -> SessionStatus:
    def files(status: TaskStatus) -> int:
        return counts.get(status.value, {}).get("files", 0)

    total = sum(item["files"] for item in counts.values())
    verified = files(TaskStatus.VERIFIED)
    unfinished = files(TaskStatus.PENDING) + files(TaskStatus.UNCERTAIN)
    if stopped and unfinished:
        return SessionStatus.STOPPED
    if aborted:
        return SessionStatus.PARTIAL_SUCCESS if verified else SessionStatus.FAILED
    if verified == total and not blocking_scan_errors:
        return SessionStatus.SUCCESS
    if verified:
        return SessionStatus.PARTIAL_SUCCESS
    return SessionStatus.FAILED


class SessionFinalizer:
    def __init__(self, sessions: SessionRepository, scans: ScanRepository, store: ObjectStore,
                 reports: ReportGenerator) -> None:
        self._sessions = sessions
        self._scans = scans
        self._store = store
        self._reports = reports

    def finalize(self, result: RunResult) -> tuple[SessionStatus, ReportPaths]:
        session_id = result.session_id
        counts = self._sessions.status_counts(session_id)
        blocking = count_blocking_errors(self._scans, session_id)
        status = decide_status(counts, blocking, result.stopped, result.abort_reason is not None)
        self._sessions.set_status(session_id, status, result.abort_reason)
        if status is not SessionStatus.STOPPED and not result.abort_reason:
            self.write_manifests(session_id)
        return status, self._reports.write(session_id)

    def write_manifests(self, session_id: str) -> None:
        """A failed manifest is reported, and never changes the files' verified state."""
        session = self._sessions.get(session_id)
        prefix = f"{session['base_prefix']}/manifests"
        documents = self._reports.manifests(session_id)
        for name, document in documents.items():
            object_name = f"{prefix}/{name}"
            body = json.dumps(document, indent=2, ensure_ascii=False).encode("utf-8")
            try:
                existing = self._store.get_object(object_name)
                generation = existing.generation if existing else 0
                self._store.upload_bytes(object_name, body, "application/json", generation)
            except UploaderError as exc:
                logger.error("Could not write manifest %s: %s", object_name, exc)
                self._sessions.set_error(session_id, f"Manifest not written: {exc}")

    def refresh_after_verify(self, session_id: str) -> tuple[SessionStatus, ReportPaths]:
        """Verification can change the outcome, so decide the status again."""
        stopped = self._sessions.get(session_id)["status"] == SessionStatus.STOPPED
        return self.finalize(RunResult(session_id, stopped=stopped))
