"""Derive the open problems from recorded facts, and track when each opens and is resolved.

Problems are never stored as the source of truth. They are recomputed from the database and the
station's live state, so a problem disappears as soon as its cause does (a card is put back, a
retry succeeds, the sign-in is renewed). The table `station_problems` only keeps the history.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from src.advisor.catalog import (
    CATALOG,
    FIX_LABELS,
    SEVERITY_ORDER,
    FixAction,
    code_for_error,
    code_for_scan_kind,
)
from src.core.enums import CardState, TaskStatus
from src.station.events import EventBus

SLOW_LINK_MBPS = 5000.0  # Below USB 3 (5 Gbps).
LOW_DISK_BYTES = 1024 ** 3
LOW_DISK_SHARE = 0.05
_STORE_CODES = {
    "AuthenticationError": "E-GCS-AUTH",
    "GcsPermissionError": "E-GCS-PERMISSION",
    "ConfigurationError": "E-CONFIG",
}


@dataclass
class Problem:
    id: str
    code: str
    severity: str
    title: str
    message: str
    steps: list[str]
    action: dict | None  # {"kind": FixAction, "label": str, "card_id": str | None}
    card_id: str | None = None
    files: int = 0
    detail: str | None = None  # Technical detail for support.
    opened_at: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass
class AdvisorFacts:
    """Everything the advisor looks at. Built by the station; easy to construct in tests."""

    session_id: str | None
    cards: list[Any]  # CardView-like: card_id, label, state, present, reason, files_*.
    error_groups: list[dict] = field(default_factory=list)
    scan_groups: list[dict] = field(default_factory=list)
    engine_error: tuple[str | None, str] | None = None  # (exception class, message)
    store_error: tuple[str | None, str] | None = None
    issues: list[Any] = field(default_factory=list)  # SourceIssue-like: code, message, subject.
    disk_free: tuple[int, int] | None = None  # (free bytes, total bytes)
    max_retries: int = 5
    extra: list[dict] = field(default_factory=list)  # From the destination profile.


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{value} B"


def _speed(mbps: float) -> str:
    return f"{mbps / 1000:g} Gbps" if mbps >= 1000 else f"{mbps:g} Mbps"


class _Builder:
    def __init__(self) -> None:
        self.problems: dict[str, Problem] = {}

    def add(self, code: str, card: Any | None = None, files: int = 0, detail: str | None = None,
            subject: str | None = None, **values: object) -> None:
        spec = CATALOG[code]
        card_id = card.card_id if card is not None else None
        key = f"{code}:{card_id or subject or 'station'}"
        existing = self.problems.get(key)
        if existing is not None:  # Same problem on the same card: add up the files.
            existing.files += files
            files = existing.files
            detail = existing.detail or detail
        name = (f"{card.label} ({card.card_id})" if card is not None and card.label
                else card_id or "")
        title, message = spec.render(card=name, files=files or "", detail=detail, **values)
        action = None
        kind = spec.action
        if kind is FixAction.RETRY_CARD and card_id is None:
            kind = FixAction.RESUME_ENGINE  # Station-wide: the only retry is the uploader.
        if kind is not FixAction.NONE:
            action = {"kind": kind.value, "label": FIX_LABELS[kind], "card_id": card_id}
        self.problems[key] = Problem(
            id=key, code=code, severity=spec.severity.value, title=title, message=message,
            steps=list(spec.steps), action=action, card_id=card_id, files=files, detail=detail,
            tags=list(spec.tags),
        )


def evaluate(facts: AdvisorFacts) -> list[Problem]:
    """The problems open right now, most severe first."""
    build = _Builder()
    cards = {card.card_id: card for card in facts.cards}

    # Station-wide: uploader stopped, cloud unreachable, devices, disk.
    engine_code = None
    if facts.engine_error:
        error_class, message = facts.engine_error
        engine_code = code_for_error(error_class)
        build.add(engine_code, detail=message)
    if facts.store_error:
        error_class, message = facts.store_error
        code = _STORE_CODES.get(error_class or "", "E-GCS-UNREACHABLE")
        if code != engine_code:
            build.add(code, detail=message)
    for issue in facts.issues:
        if issue.code in CATALOG:
            build.add(issue.code, detail=issue.message, subject=issue.subject or issue.message)
    if facts.disk_free:
        free, total = facts.disk_free
        if free < LOW_DISK_BYTES or (total and free / total < LOW_DISK_SHARE):
            build.add("W-DISK-LOW", free=_human_bytes(free))

    # Cards.
    for card in facts.cards:
        if card.state is CardState.REMOVED:
            build.add("E-CARD-REMOVED", card, files=max(0, card.files_total - card.files_done))
        elif card.state is CardState.NEEDS_CONFIRMATION:
            build.add("W-CARD-CONFIRM", card, detail=card.reason or "it needs confirmation")
        elif card.state is CardState.NEEDS_ATTENTION and card.files_total == 0 \
                and card.reason:
            build.add("E-CARD-UNREADABLE", card, detail=card.reason)
        speed = getattr(card, "link_speed_mbps", None)
        if card.present and speed and speed < SLOW_LINK_MBPS:
            build.add("W-USB-SLOW", card, speed=_speed(speed))

    retrying_files, retrying_attempt, retrying_detail = 0, 0, None
    for group in facts.error_groups:
        card = cards.get(group["card_id"])
        status, error_class = group["status"], group["error_class"] or None
        if status == TaskStatus.PENDING.value:  # Backing off after a network error.
            retrying_files += group["files"]
            retrying_attempt = max(retrying_attempt, group["attempts"] or 0)
            retrying_detail = retrying_detail or group["sample"]
            continue
        if status == TaskStatus.UNCERTAIN.value:
            code = "W-UNCERTAIN"
        elif error_class == "SourceDisconnectedError":
            if card is None or not card.present:
                continue  # E-CARD-REMOVED already says it, with the right count.
            code = "E-FILE-READ"  # Still listed, but it stopped answering.
        elif status == TaskStatus.CONFLICT.value and error_class is None:
            code = "E-CONFLICT"
        else:
            code = code_for_error(error_class)
        build.add(code, card, files=group["files"], detail=group["sample"])
    if retrying_files:
        build.add("W-NETWORK-RETRYING", files=retrying_files, detail=retrying_detail,
                  attempt=retrying_attempt, max=facts.max_retries + 1)

    for group in facts.scan_groups:
        card = cards.get(group["card_id"])
        if card is None:
            continue
        build.add(code_for_scan_kind(group["kind"]), card, files=group["count"],
                  detail=group["sample"])

    for item in facts.extra:  # e.g. PeakAI: pass refused, report failed, card held.
        if item["code"] in CATALOG:
            build.add(item["code"], cards.get(item.get("card_id") or ""),
                      detail=item.get("detail"), subject=item.get("subject"),
                      **item.get("values", {}))

    return sorted(build.problems.values(),
                  key=lambda item: (SEVERITY_ORDER[item.severity], item.code, item.id))


class ProblemTracker:
    """Records when problems open and resolve, and announces each change on the event bus."""

    def __init__(self, repo, bus: EventBus) -> None:
        self._repo = repo
        self._bus = bus
        self._open: dict[str, dict] | None = None  # problem id -> history row

    def update(self, session_id: str | None, problems: list[Problem]) -> list[Problem]:
        if self._open is None:
            self._open = {row["problem_id"]: row for row in self._repo.open_problems()}
        current = {problem.id: problem for problem in problems}
        for problem_id in list(self._open):
            if problem_id not in current:
                row = self._open.pop(problem_id)
                self._repo.resolve_problem(row["row_id"])
                self._bus.publish("problem_resolved", id=problem_id, code=row["code"],
                                  card_id=row["card_id"])
        for problem in problems:
            row = self._open.get(problem.id)
            if row is None:
                row = self._repo.open_problem(session_id or "", problem)
                self._open[problem.id] = row
                self._bus.publish("problem_opened", id=problem.id, code=problem.code,
                                  severity=problem.severity, title=problem.title,
                                  card_id=problem.card_id)
            problem.opened_at = row["opened_at"]
        return problems


def as_dicts(problems: list[Problem]) -> list[dict]:
    return [asdict(problem) for problem in problems]
