"""Measure read speed and upload speed on the real setup, to choose workers and chunk size."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from src.core.models import Card
from src.core.settings import FileScanningSettings
from src.inventory.file_scanner import FileScanner, ScannedFile
from src.platform.paths import open_source_readonly

_BLOCK = 1024 * 1024


@dataclass
class ReadResult:
    card_id: str
    bytes_read: int
    seconds: float
    errors: int

    @property
    def mb_per_s(self) -> float:
        return self.bytes_read / self.seconds / 1_000_000 if self.seconds else 0.0


def _read_card(card: Card, settings: FileScanningSettings, limit: int) -> ReadResult:
    total = errors = 0
    started = time.monotonic()
    for event in FileScanner(settings).walk(card.mount_point):
        if not isinstance(event, ScannedFile):
            continue
        path = os.path.join(card.mount_point, *event.relative_path.split("/"))
        try:
            with open_source_readonly(path) as handle:
                while total < limit and (block := handle.read(_BLOCK)):
                    total += len(block)
        except OSError:
            errors += 1
        if total >= limit:
            break
    return ReadResult(card.card_id, total, time.monotonic() - started, errors)


def measure_read(cards: list[Card], settings: FileScanningSettings,
                 limit_bytes: int) -> list[ReadResult]:
    """Read every card at once, so shared hub and controller limits show up in the result."""
    with ThreadPoolExecutor(max_workers=max(1, len(cards))) as pool:
        return list(pool.map(lambda card: _read_card(card, settings, limit_bytes), cards))
