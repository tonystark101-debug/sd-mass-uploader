"""Where a station's uploads go and how: the seam between the station and a destination.

The generic profile is the original behaviour (one bucket, `<root>/<project>/<date>/<session>/
<card>/...`). The PeakAI profile (src/peakai/profile.py) replaces it when a pack is imported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from src.core.models import Card, ScanSummary
from src.core.settings import Settings
from src.database.repositories import ScanRepository, SessionRepository
from src.metadata.session import SessionPlan, SessionPlanner
from src.metadata.validation import validate_metadata


@dataclass
class SessionCreated:
    session_id: str
    prefix: str
    notes: list[str] = field(default_factory=list)  # Things to tell the operator.


@dataclass
class QueueOutcome:
    planned_files: int = 0
    planned_bytes: int = 0
    rejected: int = 0
    held_reason: str | None = None  # Set: the card waits for a person's decision.


class UploadProfile(Protocol):
    name: str

    def create_session(self, metadata: dict) -> SessionCreated: ...

    def queue_card(self, session_id: str, card: Card, summary: ScanSummary) -> QueueOutcome: ...

    def decide(self, session_id: str, card: Card | None, card_id: str,
               decision: dict) -> QueueOutcome: ...

    def housekeeping(self, session_id: str) -> None: ...

    def retry_reports(self, session_id: str) -> None: ...

    def problems(self, session_id: str | None) -> list[dict]: ...

    def view(self, session_id: str | None) -> dict: ...


class GenericProfile:
    """The original behaviour, unchanged."""

    name = "generic"

    def __init__(self, settings: Settings, sessions: SessionRepository,
                 scans: ScanRepository) -> None:
        self._settings = settings
        self._sessions = sessions
        self._planner = SessionPlanner(settings, sessions, scans)

    def create_session(self, metadata: dict) -> SessionCreated:
        values = validate_metadata(metadata, self._settings.metadata)
        plan = self._planner.plan(values, [])
        return SessionCreated(plan.session_id, plan.base_prefix)

    def queue_card(self, session_id: str, card: Card, summary: ScanSummary) -> QueueOutcome:
        session = self._sessions.get(session_id)
        plan = SessionPlan(session_id, session["base_prefix"])
        self._planner.queue_card(plan, summary)
        return QueueOutcome(plan.planned_files, plan.planned_bytes, plan.rejected_paths)

    def decide(self, session_id: str, card: Card | None, card_id: str,
               decision: dict) -> QueueOutcome:
        from src.station.station import StationError

        raise StationError("Cards never wait for a placement decision in this mode")

    def housekeeping(self, session_id: str) -> None:
        return None

    def retry_reports(self, session_id: str) -> None:
        return None

    def problems(self, session_id: str | None) -> list[dict]:
        return []

    def view(self, session_id: str | None) -> dict:
        return {"profile": self.name, "bucket": self._settings.google_cloud.bucket_name}
