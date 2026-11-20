"""Where a session's cards are now. Drive letters can change, so identity decides."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

from src.core.exceptions import SourceDisconnectedError
from src.core.models import Card
from src.platform.paths import mount_is_present, to_extended_path


@dataclass
class CardLocation:
    card_id: str
    mount_point: str | None
    problem: str | None = None


class CardLocator:
    """Resolves a task's source path, refusing to read from a card it cannot identify."""

    def __init__(self, locations: dict[str, CardLocation]) -> None:
        self._locations = locations  # Shared on purpose: callers may update it.
        self._lock = threading.Lock()

    def set(self, card_id: str, mount_point: str) -> None:
        """The card is attached here now (station mode: it can move between slots)."""
        with self._lock:
            self._locations[card_id] = CardLocation(card_id, mount_point)

    def remove(self, card_id: str, reason: str) -> None:
        """Reads of this card fail as disconnected until it is set again."""
        with self._lock:
            self._locations[card_id] = CardLocation(card_id, None, reason)

    def mount_of(self, card_id: str) -> str:
        with self._lock:
            location = self._locations.get(card_id)
        if location is None or location.mount_point is None:
            reason = location.problem if location else "unknown card"
            raise SourceDisconnectedError(f"{card_id}: {reason}")
        return location.mount_point

    def path_for(self, card_id: str, relative_path: str) -> str:
        return to_extended_path(
            os.path.join(self.mount_of(card_id), *relative_path.split("/"))
        )

    def is_present(self, card_id: str) -> bool:
        try:
            return mount_is_present(self.mount_of(card_id))
        except SourceDisconnectedError:
            return False


def locate_fresh(cards: list[dict]) -> CardLocator:
    """Just scanned in this run, so the recorded mount points are current."""
    return CardLocator({
        row["card_id"]: CardLocation(row["card_id"], row["mount_point"]) for row in cards
    })


def locate_after_restart(cards: list[dict], present: list[Card]) -> CardLocator:
    """Match each session card to what is attached now, by fingerprint only."""
    locations: dict[str, CardLocation] = {}
    for row in cards:
        card_id, fingerprint = row["card_id"], row["fingerprint"]
        if row["is_folder"]:
            found = row["mount_point"] if os.path.isdir(row["mount_point"]) else None
            locations[card_id] = CardLocation(card_id, found, None if found else "folder missing")
            continue
        if not fingerprint:
            locations[card_id] = CardLocation(card_id, None,
                                              "no fingerprint; cannot prove it is the same card")
            continue
        matches = [card for card in present if card.fingerprint == fingerprint]
        if len(matches) == 1:
            locations[card_id] = CardLocation(card_id, matches[0].mount_point)
        elif matches:
            locations[card_id] = CardLocation(card_id, None,
                                              "several attached cards share this serial")
        else:
            locations[card_id] = CardLocation(card_id, None, "card is not attached")
    return CardLocator(locations)
