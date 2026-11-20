"""Create a session: scan the selected cards and queue one task per file."""

from __future__ import annotations

import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.core.enums import SessionStatus
from src.core.exceptions import MetadataValidationError
from src.core.models import Card, ScanSummary
from src.core.settings import Settings
from src.database.repositories import ScanRepository, SessionRepository
from src.inventory.inventory_service import ScanService
from src.metadata.validation import SessionMetadata, object_name_for, session_prefix

logger = logging.getLogger(__name__)

_TASK_BATCH = 500
_MAX_SCAN_THREADS = 8


def new_session_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"SESSION_{stamp}_{secrets.token_hex(3)}"


@dataclass
class SessionPlan:
    session_id: str
    base_prefix: str
    scans: list[ScanSummary] = field(default_factory=list)
    planned_files: int = 0
    planned_bytes: int = 0
    rejected_paths: int = 0
    sample_objects: list[str] = field(default_factory=list)


class SessionPlanner:
    def __init__(self, settings: Settings, sessions: SessionRepository,
                 scans: ScanRepository) -> None:
        self._settings = settings
        self._sessions = sessions
        self._scans = scans
        self._scan_service = ScanService(scans, settings.file_scanning)

    def plan(self, metadata: SessionMetadata, cards: list[Card]) -> SessionPlan:
        """Always rescan: an older listing cannot be trusted for what is on the card now."""
        session_id = new_session_id()
        prefix = session_prefix(self._settings.google_cloud.root_prefix, metadata.project_name,
                                metadata.recording_date, session_id)
        self._sessions.create(session_id, prefix, metadata.as_flat_dict())
        for card in cards:
            self._sessions.add_card(session_id, card.card_id, card.mount_point, card.fingerprint,
                                    card.volume_label, card.reader_model,
                                    (card.fingerprint or "").startswith("folder:"))
        self._sessions.set_status(session_id, SessionStatus.SCANNING)

        plan = SessionPlan(session_id, prefix)
        workers = min(_MAX_SCAN_THREADS, max(1, len(cards)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan") as pool:
            plan.scans = list(pool.map(
                lambda card: self._scan_service.scan_card(card.card_id, card.mount_point,
                                                          session_id),
                cards,
            ))
        for summary in plan.scans:
            self.queue_card(plan, summary)
        self._sessions.set_status(session_id, SessionStatus.READY)
        return plan

    def queue_card(self, plan: SessionPlan, summary: ScanSummary) -> None:
        """Queue one task per readable file of a finished scan, adding to the plan's totals."""
        batch: list[tuple] = []
        for row in self._scans.iter_readable_files(summary.scan_id):
            try:
                name = object_name_for(plan.base_prefix, summary.card_id, row["relative_path"])
            except MetadataValidationError as exc:
                self._scans.add_errors(summary.scan_id,
                                       [(row["relative_path"], "UNSAFE_OBJECT_NAME", str(exc))])
                plan.rejected_paths += 1
                continue
            batch.append((plan.session_id, row["file_id"], summary.card_id, row["relative_path"],
                          name, row["size_bytes"], row["mtime_ns"]))
            plan.planned_files += 1
            plan.planned_bytes += row["size_bytes"]
            if len(plan.sample_objects) < 3:
                plan.sample_objects.append(name)
            if len(batch) >= _TASK_BATCH:
                self._sessions.add_tasks(batch)
                batch = []
        if batch:
            self._sessions.add_tasks(batch)
