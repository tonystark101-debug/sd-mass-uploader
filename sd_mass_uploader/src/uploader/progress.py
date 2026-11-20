"""Thread-safe upload progress. Throughput comes from measured bytes and elapsed time."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

_WINDOW_SECONDS = 10.0


@dataclass
class ProgressView:
    files_pending: int
    files_active: int
    files_successful: int
    files_failed: int
    bytes_pending: int
    bytes_uploaded: int
    retries: int
    elapsed_seconds: float
    current_mb_per_s: float
    average_mb_per_s: float


class ProgressTracker:
    def __init__(self, files_total: int, bytes_total: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._started = clock()
        self._files_total = files_total
        self._bytes_total = bytes_total
        self._active = self._ok = self._failed = self._retries = 0
        self._bytes = 0
        self._samples: deque[tuple[float, int]] = deque()

    def add_planned(self, files: int, size: int) -> None:
        """More work joined the run, for example a newly inserted card."""
        with self._lock:
            self._files_total += files
            self._bytes_total += size

    def add_bytes(self, count: int) -> None:
        with self._lock:
            self._bytes += count
            now = self._clock()
            self._samples.append((now, count))
            while self._samples and now - self._samples[0][0] > _WINDOW_SECONDS:
                self._samples.popleft()

    def rewind_bytes(self, count: int) -> None:
        """Undo the count for an attempt that will be sent again."""
        with self._lock:
            self._bytes -= count

    def started(self) -> None:
        with self._lock:
            self._active += 1

    def finished(self, success: bool) -> None:
        with self._lock:
            self._active -= 1
            if success:
                self._ok += 1
            else:
                self._failed += 1

    def released(self) -> None:
        """The file went back in the queue."""
        with self._lock:
            self._active -= 1

    def retried(self) -> None:
        with self._lock:
            self._retries += 1

    def view(self) -> ProgressView:
        with self._lock:
            now = self._clock()
            elapsed = max(now - self._started, 1e-9)
            recent = sum(c for t, c in self._samples if now - t <= _WINDOW_SECONDS)
            window = min(elapsed, _WINDOW_SECONDS)
            return ProgressView(
                files_pending=max(
                    0, self._files_total - self._ok - self._failed - self._active),
                files_active=self._active,
                files_successful=self._ok,
                files_failed=self._failed,
                bytes_pending=max(0, self._bytes_total - self._bytes),
                bytes_uploaded=self._bytes,
                retries=self._retries,
                elapsed_seconds=elapsed,
                current_mb_per_s=recent / window / 1_000_000,
                average_mb_per_s=self._bytes / elapsed / 1_000_000,
            )
