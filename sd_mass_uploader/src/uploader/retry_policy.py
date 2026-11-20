"""Decide what happens after a failed attempt."""

from __future__ import annotations

import random
from collections.abc import Callable
from enum import Enum

from src.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    GcsPermissionError,
    ObjectExistsError,
    PassDeniedError,
    SourceChangedError,
    SourceDisconnectedError,
    SourceReadError,
    StopRequested,
    TransientUploadError,
    VerificationMismatch,
)


class Disposition(Enum):
    RETRY = "RETRY"  # Back off, then try again.
    FAIL = "FAIL"  # Give up for now; `retry` may try again later.
    FAIL_PERMANENT = "FAIL_PERMANENT"  # Retrying the same thing cannot succeed.
    CONFLICT = "CONFLICT"  # Destination state needs an operator.
    ABORT_SESSION = "ABORT_SESSION"  # Every other file would fail the same way.
    STOPPED = "STOPPED"  # Interrupted by a stop; goes back in the queue.


def classify(exc: Exception) -> Disposition:
    if isinstance(exc, StopRequested):
        return Disposition.STOPPED
    if isinstance(exc, PassDeniedError):
        return Disposition.FAIL  # That card's files only; retried once PeakAI fixes the pass.
    if isinstance(exc, TransientUploadError):
        return Disposition.RETRY
    if isinstance(exc, AuthenticationError | GcsPermissionError | ConfigurationError):
        return Disposition.ABORT_SESSION
    if isinstance(exc, ObjectExistsError | VerificationMismatch):
        return Disposition.CONFLICT
    if isinstance(exc, SourceChangedError):
        return Disposition.FAIL_PERMANENT
    if isinstance(exc, SourceDisconnectedError):
        return Disposition.FAIL  # The card may be plugged back in.
    if isinstance(exc, SourceReadError):
        return Disposition.FAIL
    return Disposition.FAIL_PERMANENT


def backoff_delay(attempt: int, initial: float, maximum: float,
                  rng: Callable[[], float] = random.random) -> float:
    """Exponential backoff with jitter, so workers do not all come back at the same moment."""
    ceiling = min(maximum, initial * (2 ** max(0, attempt - 1)))
    return ceiling / 2 + rng() * ceiling / 2
