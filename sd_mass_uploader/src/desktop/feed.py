"""Polls the station API for the desktop companion: state, connection, and new events."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from src.api.client import ApiUnavailable, StationClient


@dataclass
class FeedUpdate:
    state: dict | None
    connection: str  # "live", "offline" or "forbidden"
    events: list[dict] = field(default_factory=list)  # New since the last poll.


class StationFeed:
    """Events that happened before the companion started (or before a station restart) are
    skipped, so nobody is notified about old news."""

    def __init__(self, client_factory: Callable[[], StationClient | None]) -> None:
        self._client_factory = client_factory
        self._after: int | None = None
        self._station_started: str | None = None

    def poll(self) -> FeedUpdate:
        client = self._client_factory()
        if client is None:
            return FeedUpdate(None, "forbidden")
        try:
            status, state = client.request("GET", "/api/v1/state")
            if status == 401:
                return FeedUpdate(None, "forbidden")
            if status != 200 or not isinstance(state, dict):
                return FeedUpdate(None, "offline")
            started = (state.get("daemon") or {}).get("started_at")
            if started != self._station_started:
                self._station_started, self._after = started, None  # Restarted: new numbering.
            status, events = client.request("GET", f"/api/v1/events?after={self._after or 0}")
        except ApiUnavailable:
            return FeedUpdate(None, "offline")
        events = events if status == 200 and isinstance(events, list) else []
        newest = max((event.get("sequence", 0) for event in events), default=self._after or 0)
        fresh = events if self._after is not None else []
        self._after = max(newest, self._after or 0)
        return FeedUpdate(state, "live", fresh)

    def run(self, stop: threading.Event, deliver: Callable[[FeedUpdate], None],
            interval: float = 2.0) -> None:
        while not stop.is_set():
            deliver(self.poll())
            stop.wait(interval)
