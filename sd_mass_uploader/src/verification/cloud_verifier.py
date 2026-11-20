"""Compare local state with what GCS really holds. Only like checksums are compared."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from src.core.enums import TaskStatus, VerificationStatus
from src.core.models import RemoteObject
from src.database.repositories import SessionRepository
from src.verification.checksums import crc32c_of_file

logger = logging.getLogger(__name__)


def compare(source_size: int, local_crc32c: str | None,
            remote: RemoteObject | None) -> VerificationStatus:
    if remote is None:
        return VerificationStatus.MISSING
    if remote.size != source_size:
        return VerificationStatus.SIZE_MISMATCH
    if not remote.crc32c or not local_crc32c:
        return VerificationStatus.CHECKSUM_UNAVAILABLE
    if remote.crc32c != local_crc32c:
        return VerificationStatus.CHECKSUM_MISMATCH
    return VerificationStatus.MATCH


@dataclass
class VerifyResult:
    checked: int = 0
    matched: int = 0
    mismatched: int = 0
    missing: int = 0
    reread_from_source: int = 0
    source_unavailable: int = 0


class CloudVerifier:
    def __init__(self, sessions: SessionRepository, store) -> None:
        self._sessions = sessions
        self._store = store

    def verify_session(self, session_id: str, locator=None) -> VerifyResult:
        """Recheck every uploaded object. With a locator, also re-read the source card."""
        result = VerifyResult()
        statuses = (TaskStatus.VERIFIED, TaskStatus.UPLOADED_UNVERIFIED)
        for task in list(self._sessions.tasks_with_status(session_id, statuses)):
            result.checked += 1
            local = task.local_crc32c
            method = "recorded-crc32c"
            if locator is not None:
                reread = self._reread(task, locator, result)
                if reread:
                    local, method = reread, "source-reread-crc32c"
            remote = self._store.get_object(task.object_name)
            status = compare(task.size_bytes, local, remote)
            self._sessions.add_verification(
                task.task_id, task.size_bytes, remote.size if remote else None, local,
                remote.crc32c if remote else None, status.value, method,
            )
            self._apply(task, status, remote, result)
        return result

    @staticmethod
    def _reread(task, locator, result: VerifyResult) -> str | None:
        try:
            path = locator.path_for(task.card_id, task.relative_path)
            info = os.stat(path)
            if info.st_size != task.size_bytes or info.st_mtime_ns != task.mtime_ns:
                result.source_unavailable += 1
                return None
            crc = crc32c_of_file(path)
        except Exception as exc:  # noqa: BLE001 - one unreadable source must not stop the rest
            logger.warning("Cannot re-read %s: %s", task.relative_path, exc)
            result.source_unavailable += 1
            return None
        result.reread_from_source += 1
        return crc

    def _apply(self, task, status: VerificationStatus, remote: RemoteObject | None,
               result: VerifyResult) -> None:
        if status is VerificationStatus.MATCH:
            result.matched += 1
            self._sessions.update_task(task.task_id, TaskStatus.VERIFIED, error_class=None,
                                       last_error=None)
        elif status is VerificationStatus.MISSING:
            result.missing += 1
            self._sessions.update_task(task.task_id, TaskStatus.FAILED,
                                       error_class="MISSING_REMOTE_OBJECT",
                                       last_error=f"Object not found: {task.object_name}")
        else:
            result.mismatched += 1
            self._sessions.update_task(task.task_id, TaskStatus.CONFLICT,
                                       error_class=status.value,
                                       last_error=f"{status.value}: {task.object_name}")
