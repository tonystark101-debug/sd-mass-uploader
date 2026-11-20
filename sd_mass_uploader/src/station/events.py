"""In-process event bus. The station publishes; the API and UI subscribe (Phase 2)."""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.core.timeutil import utc_now

logger = logging.getLogger(__name__)

Subscriber = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    kind: str  # For example "card_inserted", "card_state", "problem".
    data: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=utc_now)
    sequence: int = 0


class EventBus:
    """Thread-safe publish/subscribe with a short history for late subscribers."""

    def __init__(self, history: int = 500) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Subscriber] = []
        self._recent: deque[Event] = deque(maxlen=history)
        self._sequence = 0

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return unsubscribe

    def publish(self, kind: str, **data: Any) -> Event:
        with self._lock:
            self._sequence += 1
            event = Event(kind, data, sequence=self._sequence)
            self._recent.append(event)
            subscribers = list(self._subscribers)
        logger.info("event %s %s", kind, data)
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:  # noqa: BLE001 - one bad subscriber must not stop the station
                logger.exception("Event subscriber failed for %s", kind)
        return event

    def recent(self, after: int = 0) -> list[Event]:
        with self._lock:
            return [event for event in self._recent if event.sequence > after]
