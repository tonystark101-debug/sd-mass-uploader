"""After an interruption, compare local records with GCS before doing anything else."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from src.core.enums import TaskStatus, VerificationStatus
from src.core.exceptions import ConfigurationError
from src.core.timeutil import age_seconds
from src.database.repositories import SessionRepository
from src.uploader.card_locator import CardLocator
from src.uploader.gcs_client import ObjectStore
from src.verification.checksums import crc32c_of_file
from src.verification.cloud_verifier import compare

logger = logging.getLogger(__name__)

# Heartbeats arrive every 10s, so a much older one means the owner is gone.
_OWNER_ALIVE_SECONDS = 60


@dataclass
class ReconcileResult:
    interrupted: int = 0
    requeued: int = 0
    confirmed: int = 0
    conflicts: int = 0
    still_uncertain: int = 0


def ensure_not_running(sessions: SessionRepository, session_id: str) -> None:
    session = sessions.get(session_id)
    if session is None:
        raise ConfigurationError(f"Unknown session: {session_id}")
    beat = session["heartbeat_at"]
    if beat and age_seconds(beat) < _OWNER_ALIVE_SECONDS:
        raise ConfigurationError(
            f"Session {session_id} looks active (process {session['owner_pid']}). "
            "Stop it first, or wait a minute if that process has died."
        )


def reconcile(sessions: SessionRepository, store: ObjectStore, locator: CardLocator,
              session_id: str) -> ReconcileResult:
    """Settle every interrupted task. An interrupted upload may or may not have finished."""
    ensure_not_running(sessions, session_id)
    result = ReconcileResult(interrupted=sessions.mark_interrupted(session_id))
    tasks = list(sessions.tasks_with_status(session_id, [TaskStatus.UNCERTAIN]))
    return settle_uncertain(sessions, store, locator, tasks, result)


def settle_uncertain(sessions: SessionRepository, store: ObjectStore, locator: CardLocator,
                     tasks: list, result: ReconcileResult | None = None) -> ReconcileResult:
    """Decide each UNCERTAIN task against GCS. The caller must ensure no worker holds them."""
    result = result or ReconcileResult()
    for task in tasks:
        remote = store.get_object(task.object_name)
        if remote is None:
            sessions.update_task(task.task_id, TaskStatus.PENDING)
            result.requeued += 1
            continue
        local = _local_checksum(task, locator)
        if local is None:
            sessions.update_task(task.task_id, TaskStatus.UNCERTAIN,
                                 last_error="Remote object exists; source needed to confirm it")
            result.still_uncertain += 1
            continue
        status = compare(task.size_bytes, local, remote)
        sessions.add_verification(task.task_id, task.size_bytes, remote.size, local, remote.crc32c,
                                  status.value, "recovery-source-crc32c")
        if status is VerificationStatus.MATCH:
            sessions.update_task(task.task_id, TaskStatus.VERIFIED, local_crc32c=local,
                                 remote_generation=remote.generation, last_error=None)
            result.confirmed += 1
        else:
            sessions.update_task(task.task_id, TaskStatus.CONFLICT, error_class=status.value,
                                 last_error=f"{status.value} during recovery")
            result.conflicts += 1
    return result


def _local_checksum(task, locator: CardLocator) -> str | None:
    if task.local_crc32c:
        return task.local_crc32c
    try:
        path = locator.path_for(task.card_id, task.relative_path)
        info = os.stat(path)
        if info.st_size != task.size_bytes or info.st_mtime_ns != task.mtime_ns:
            return None
        return crc32c_of_file(path)
    except Exception as exc:  # noqa: BLE001 - unavailable source just leaves the task uncertain
        logger.info("Source unavailable for %s: %s", task.relative_path, exc)
        return None
