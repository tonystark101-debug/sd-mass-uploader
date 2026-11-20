"""Derive what the operator sees for each card, and the station counters, from stored facts."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from src.core.enums import CardPhase, CardState, TaskStatus
from src.database.station_repository import CardCounts

# Cards in these states need a person to do something.
FAILED_STATES = frozenset({
    CardState.FAILED, CardState.UPLOADED_WITH_ISSUES, CardState.NEEDS_ATTENTION,
})


def derive_state(phase: CardPhase, present: bool, paused: bool,
                 counts: CardCounts) -> CardState:
    """One card's state. The database stays the only source of truth for progress."""
    if phase is CardPhase.NEEDS_CONFIRMATION:
        return CardState.NEEDS_CONFIRMATION if present else CardState.REMOVED
    if phase is CardPhase.SCAN_FAILED:
        return CardState.NEEDS_ATTENTION
    if phase is CardPhase.NEEDS_DECISION:
        return CardState.NEEDS_DECISION if present else CardState.REMOVED
    if phase is CardPhase.SCANNING:
        return CardState.SCANNING if present else CardState.REMOVED

    total = counts.total_files()
    if total == 0:
        return CardState.NEEDS_ATTENTION if counts.blocking_scan_errors else CardState.NO_FILES
    active = counts.get(TaskStatus.ACTIVE)
    waiting = counts.get(TaskStatus.PENDING) + counts.get(TaskStatus.UNCERTAIN)
    # Files lost to a removal resume when the card returns, so they are unfinished while it is out.
    if not present and (active or waiting or counts.lost_files):
        return CardState.REMOVED
    if active:
        return CardState.PAUSING if paused else CardState.UPLOADING
    if waiting:
        if paused:
            return CardState.PAUSED
        return CardState.READY if phase is CardPhase.READY else CardState.QUEUED

    verified = counts.get(TaskStatus.VERIFIED)
    if verified == total and not counts.blocking_scan_errors:
        return CardState.UPLOADED
    if verified:
        return CardState.UPLOADED_WITH_ISSUES
    return CardState.FAILED


@dataclass
class StationCounters:
    """The numbers at the top of the console. Each has one exact meaning."""

    inserted: int = 0  # Physically present now.
    total: int = 0  # Joined this session, present or not.
    scanning: int = 0
    ready: int = 0  # Waiting for the operator to start them.
    queued: int = 0
    uploading: int = 0  # Includes cards that are pausing.
    paused: int = 0
    uploaded: int = 0  # Every file verified: safe to remove.
    failed: int = 0  # Failed, finished with issues, or need attention.
    removed: int = 0  # Taken out before finishing.
    needs_confirmation: int = 0
    needs_decision: int = 0  # Waiting for a person to say which device recorded some files.
    no_files: int = 0


def count_cards(states: list[tuple[CardState, bool]]) -> StationCounters:
    """`states` holds (state, present) for every card in the session."""
    by_state = Counter(state for state, _ in states)
    return StationCounters(
        inserted=sum(1 for _, present in states if present),
        total=len(states),
        scanning=by_state[CardState.SCANNING],
        ready=by_state[CardState.READY],
        queued=by_state[CardState.QUEUED],
        uploading=by_state[CardState.UPLOADING] + by_state[CardState.PAUSING],
        paused=by_state[CardState.PAUSED],
        uploaded=by_state[CardState.UPLOADED],
        failed=sum(by_state[state] for state in FAILED_STATES),
        removed=by_state[CardState.REMOVED],
        needs_confirmation=by_state[CardState.NEEDS_CONFIRMATION],
        needs_decision=by_state[CardState.NEEDS_DECISION],
        no_files=by_state[CardState.NO_FILES],
    )
