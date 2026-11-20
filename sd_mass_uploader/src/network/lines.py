"""The internet lines this computer has right now, and which one each upload uses.

How it behaves:
- A watcher re-reads the connections every few seconds. A new line is checked with a real
  connection to Google through that line only; when it passes it gets its upload slots at once.
- Load balancing is per file: every working line gets the same number of slots
  (`network.workers_per_line`), and each new file goes to the line with the most free slots
  (ties go to the faster line). Each line therefore carries several uploads at once, which is
  what fills it; adding a line adds its full speed.
- A line that is unplugged, or gets a new address, is dropped at once. Its files stop and go back
  in the queue without using up a retry, and the other lines pick them up.
- Three network failures in a row rest a line for a minute; it is checked again before it is used.
- Two connections that reach the internet with the same public address share one internet line
  (for example two cables into one router). Both are used, and the console says so, because the
  second adds no speed.
- With no working line found (or on a system that is not supported), uploads use the computer's
  default connection exactly as before.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from src.core.exceptions import LineLostError
from src.core.settings import NetworkSettings
from src.network import discovery
from src.network.discovery import DiscoveryError, FoundLine

logger = logging.getLogger(__name__)

_SPEED_WINDOW = 10.0  # Seconds of sent bytes behind "speed now".
_FAILURES_TO_REST = 3
_FORGET_GONE_SECONDS = 120.0
_RECHECK_FAILED_SECONDS = 30.0
_RECHECK_PUBLIC_IP_SECONDS = 300.0

ACTIVE, CHECKING, NO_INTERNET, RESTING, SPARE, GONE = (
    "active", "checking", "no_internet", "resting", "spare", "gone")

Probe = Callable[["Line"], tuple[bool, str | None, str | None]]  # (ok, public ip, error)
Discover = Callable[[], list[FoundLine]]


class Line:
    """One internet connection. Counters are updated from upload threads under `lock`."""

    def __init__(self, found: FoundLine, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.lock = threading.Lock()
        self.key = found.key
        self.name = found.name
        self.kind = found.kind
        self.ip = found.ip
        self.gateway = found.gateway
        self.device = found.device
        self.warning = found.warning
        self.generation = 0  # Bumped when the address changes; older connections stop.
        self.state = CHECKING
        self.note: str | None = None
        self.public_ip: str | None = None
        self.shares_with: str | None = None
        self.active = 0
        self.slots = 0
        self.failures = 0
        self.rest_until = 0.0
        self.checked_at = -1e9
        self.public_checked_at = -1e9
        self.gone_at: float | None = None
        self.files_ok = 0
        self.sent_total = 0
        self._samples: deque[tuple[float, int]] = deque()

    # Called from upload threads (through the pinned socket).
    def check_open(self, generation: int) -> None:
        if self.state == GONE or generation != self.generation:
            raise LineLostError(f"Internet line {self.name} was disconnected")

    def add_sent(self, count: int) -> None:
        now = self._clock()
        with self.lock:
            self.sent_total += count
            self._samples.append((now, count))
            while self._samples and now - self._samples[0][0] > _SPEED_WINDOW:
                self._samples.popleft()

    def bytes_per_second(self) -> float:
        now = self._clock()
        with self.lock:
            recent = sum(count for when, count in self._samples if now - when <= _SPEED_WINDOW)
        return recent / _SPEED_WINDOW

    @property
    def usable(self) -> bool:
        return self.state == ACTIVE


@dataclass
class LineView:
    key: str
    name: str
    kind: str
    ip: str
    gateway: str
    public_ip: str | None
    state: str
    note: str | None
    shares_with: str | None
    uploads: int
    slots: int
    mbit_per_s: float
    mb_per_s: float
    sent_bytes: int
    files_done: int


def default_probe(settings: NetworkSettings, timeout: float = 8.0) -> Probe:
    """Reach Google through the line only; then, optionally, learn its public address."""

    def probe(line: Line) -> tuple[bool, str | None, str | None]:
        from src.network.binding import line_session

        session = line_session(line, pool_maxsize=2)
        try:
            try:
                session.head(settings.probe_url, timeout=(timeout, timeout),
                             allow_redirects=False)
            except Exception as exc:  # noqa: BLE001 - any failure means "this line cannot"
                return False, None, _short(exc)
            public_ip = None
            if settings.public_ip_url:
                try:
                    answer = session.get(settings.public_ip_url, timeout=(timeout, timeout))
                    public_ip = _parse_public_ip(answer.text)
                except Exception:  # noqa: BLE001 - optional; the line still works
                    public_ip = None
            return True, public_ip, None
        finally:
            session.close()

    return probe


def _parse_public_ip(text: str) -> str | None:
    for row in text.splitlines():  # Cloudflare trace: "ip=1.2.3.4"; plain services: "1.2.3.4".
        row = row.strip()
        if row.startswith("ip="):
            return row[3:] or None
    text = text.strip()
    return text if text and len(text) <= 45 and " " not in text else None


def _short(exc: Exception) -> str:
    text = str(exc)
    for marker in ("Errno", "timed out", "refused", "unreachable", "No route", "Name or service"):
        index = text.find(marker)
        if index >= 0:
            return text[index:index + 120]
    return f"{type(exc).__name__}: {text[:120]}"


def _same_address(line: Line) -> bool:
    return bool(line.note) and "same address" in line.note


_local = threading.local()


def current_line() -> Line | None:
    """The line the current upload thread is pinned to, or None for the default connection."""
    return getattr(_local, "line", None)


@contextmanager
def using_line(line: Line | None):
    previous = current_line()
    _local.line = line
    try:
        yield line
    finally:
        _local.line = previous


class LinePool:
    def __init__(self, settings: NetworkSettings, fallback_workers: int = 4,
                 discover: Discover | None = None, probe: Probe | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 on_event: Callable[..., None] | None = None) -> None:
        self._settings = settings
        self._fallback_workers = max(1, fallback_workers)
        self._discover = discover or (lambda: discovery.discover(settings.exclude_interfaces))
        self._probe = probe or default_probe(settings)
        self._clock = clock
        self.on_event = on_event
        self._lock = threading.RLock()
        self._lines: dict[str, Line] = {}
        self._order: list[str] = []  # First seen first: keeps max_lines choices stable.
        self._error: str | None = None
        self._refreshed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._probes = ThreadPoolExecutor(max_workers=4, thread_name_prefix="line-check")

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, name="line-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._probes.shutdown(wait=False, cancel_futures=True)

    def _watch(self) -> None:
        interval = max(1.0, self._settings.rescan_seconds)
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - the watcher must never die
                logger.exception("Checking internet lines failed")
            self._stop.wait(interval)

    # ------------------------------------------------------------------ discovery

    def refresh(self) -> None:
        """One pass: re-read the connections, then check lines that need it (in parallel)."""
        try:
            found = self._discover()
        except DiscoveryError as exc:
            with self._lock:
                self._error = str(exc)
            return  # A failed query proves nothing: keep what we had.
        now = self._clock()
        with self._lock:
            self._error = None
            self._refreshed = True
            self._apply(found, now)
            due = [line for line in self._lines.values() if self._needs_check(line, now)]
            for line in due:
                line.checked_at = now
        if due:
            list(self._probes.map(self._check, due))
        with self._lock:
            self._settle(self._clock())

    def _apply(self, found: list[FoundLine], now: float) -> None:
        seen = set()
        ip_counts: dict[str, int] = {}
        for item in found:
            ip_counts[item.ip] = ip_counts.get(item.ip, 0) + 1
        for item in found:
            seen.add(item.key)
            line = self._lines.get(item.key)
            if line is None or line.state == GONE and line.gone_at is not None:
                if line is None:
                    line = Line(item, self._clock)
                    self._lines[item.key] = line
                    self._order.append(item.key)
                    logger.info("Internet line found: %s (%s, %s)", item.name, item.kind, item.ip)
                else:  # Plugged back in.
                    self._reset(line, item)
                line.state, line.gone_at = CHECKING, None
                self._emit("line_found", line)
            elif (item.ip, item.gateway, item.device) != (line.ip, line.gateway, line.device):
                logger.info("Internet line %s changed address %s -> %s", line.name, line.ip,
                            item.ip)
                self._reset(line, item)
                line.state = CHECKING
                self._emit("line_changed", line)
            line.name, line.kind, line.warning = item.name, item.kind, item.warning
            if ip_counts[item.ip] > 1:
                if line.state != NO_INTERNET or not _same_address(line):
                    line.generation += 1  # Its uploads could leave through either connection.
                line.state, line.note = NO_INTERNET, (
                    f"another connection has the same address {item.ip}, so uploads cannot be "
                    "pinned to this one (two iPhone hotspots both use 172.20.10.x)")
            elif line.state == NO_INTERNET and _same_address(line):
                line.state, line.note = CHECKING, None
        for line in self._lines.values():
            if line.key not in seen and line.state != GONE:
                line.state, line.gone_at, line.note = GONE, now, "unplugged or disconnected"
                line.generation += 1  # Stops its uploads at the next slice.
                logger.warning("Internet line lost: %s", line.name)
                self._emit("line_lost", line)
        for key in [k for k, line in self._lines.items()
                    if line.state == GONE and line.gone_at is not None
                    and now - line.gone_at > _FORGET_GONE_SECONDS]:
            del self._lines[key]
            self._order.remove(key)

    def _reset(self, line: Line, item: FoundLine) -> None:
        line.ip, line.gateway, line.device = item.ip, item.gateway, item.device
        line.generation += 1
        line.public_ip = None
        line.failures, line.rest_until = 0, 0.0
        line.checked_at = line.public_checked_at = -1e9

    def _needs_check(self, line: Line, now: float) -> bool:
        if line.state == CHECKING:
            return True
        if line.state == NO_INTERNET and _same_address(line):
            return False
        if line.state == NO_INTERNET:
            return now - line.checked_at >= _RECHECK_FAILED_SECONDS
        if line.state == RESTING:
            return now >= line.rest_until
        if line.state in (ACTIVE, SPARE):
            return now - line.public_checked_at >= _RECHECK_PUBLIC_IP_SECONDS
        return False

    def _check(self, line: Line) -> None:
        generation = line.generation
        ok, public_ip, error = self._probe(line)
        with self._lock:
            if line.generation != generation or line.state == GONE:
                return  # Changed while checking; the next pass checks it again.
            line.public_checked_at = self._clock()
            if ok:
                was = line.state
                line.public_ip = public_ip or line.public_ip
                line.failures = 0
                if was in (CHECKING, NO_INTERNET, RESTING):
                    line.state, line.note = ACTIVE, None
                    logger.info("Internet line ready: %s (%s, public %s)", line.name, line.ip,
                                line.public_ip or "unknown")
                    self._emit("line_ready", line)
            else:
                line.state = NO_INTERNET
                line.note = f"cannot reach Google through this line: {error}"
                logger.warning("Internet line %s cannot reach Google: %s", line.name, error)
                self._emit("line_no_internet", line)

    def _settle(self, now: float) -> None:
        """Apply max_lines, mark shared lines, and hand out slots."""
        working = [self._lines[key] for key in self._order
                   if self._lines[key].state in (ACTIVE, SPARE)]
        limit = self._settings.max_lines or len(working)
        for index, line in enumerate(working):
            line.state = ACTIVE if index < limit else SPARE
            if line.state == SPARE:
                line.note = f"spare: network.max_lines is {self._settings.max_lines}"
            elif line.note and line.note.startswith("spare"):
                line.note = None
        first_by_public: dict[str, Line] = {}
        for line in working:
            line.shares_with = None
            if line.public_ip:
                owner = first_by_public.setdefault(line.public_ip, line)
                if owner is not line:
                    line.shares_with = owner.name
        for line in self._lines.values():
            line.slots = self._settings.workers_per_line if line.state == ACTIVE else 0

    def _emit(self, kind: str, line: Line) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, line=line.name, kind_of_line=line.kind, ip=line.ip,
                              note=line.note)
            except Exception:  # noqa: BLE001 - a listener must not break the watcher
                logger.exception("Line event listener failed")

    # ------------------------------------------------------------------ uploads

    def usable(self) -> list[Line]:
        with self._lock:
            return [self._lines[key] for key in self._order if self._lines[key].usable]

    def capacity(self) -> int:
        """How many files may upload at once right now."""
        with self._lock:
            lines = [line for line in self._lines.values() if line.usable]
            if not lines:
                return self._fallback_workers
            return min(self._settings.max_total_workers, sum(line.slots for line in lines))

    def acquire(self) -> Line | None:
        """Pick the line for the next file; None means use the default connection."""
        with self._lock:
            lines = [line for line in self._lines.values() if line.usable]
            if not lines:
                return None
            line = min(lines, key=lambda item: (item.active / max(item.slots, 1),
                                                -item.bytes_per_second()))
            line.active += 1
            return line

    def release(self, line: Line | None, error: Exception | None) -> None:
        if line is None:
            return
        from src.core.exceptions import PeakAiServiceError, TransientUploadError

        with self._lock:
            line.active = max(0, line.active - 1)
            if error is None:
                line.failures = 0
                line.files_ok += 1
                return
            network = isinstance(error, TransientUploadError) and not isinstance(
                error, LineLostError | PeakAiServiceError)
            if not network or line.state != ACTIVE:
                return
            line.failures += 1
            if line.failures >= _FAILURES_TO_REST:
                line.state = RESTING
                line.rest_until = self._clock() + self._settings.rest_seconds
                line.note = (f"{_FAILURES_TO_REST} network failures in a row; resting for "
                             f"{self._settings.rest_seconds:g} s, then checked again")
                line.failures = 0
                line.slots = 0
                logger.warning("Internet line %s is resting: %s", line.name, error)
                self._emit("line_resting", line)

    # ------------------------------------------------------------------ views

    def views(self) -> list[LineView]:
        with self._lock:
            lines = [self._lines[key] for key in self._order]
        result = []
        for line in lines:
            rate = 0.0 if line.state == GONE else line.bytes_per_second()  # Gone: nothing now.
            result.append(LineView(
                key=line.key, name=line.name, kind=line.kind, ip=line.ip, gateway=line.gateway,
                public_ip=line.public_ip, state=line.state,
                note=line.note or line.warning, shares_with=line.shares_with,
                uploads=line.active, slots=line.slots,
                mbit_per_s=round(rate * 8 / 1_000_000, 1), mb_per_s=round(rate / 1_000_000, 2),
                sent_bytes=line.sent_total, files_done=line.files_ok))
        return result

    def view(self) -> dict:
        """What the console, the status command and the API show."""
        lines = self.views()
        working = [line for line in lines if line.state == ACTIVE]
        total = sum(line.mbit_per_s for line in lines)
        if not self._settings.enabled:
            mode = "off"
        elif not working:
            mode = "default"
        else:
            mode = "multi" if len(working) > 1 else "single"
        return {
            "enabled": self._settings.enabled,
            "mode": mode,
            "working": len(working),
            "capacity": self.capacity(),
            "total_mbit_per_s": round(total, 1),
            "total_mb_per_s": round(total / 8, 2),
            "error": self._error,
            "lines": [asdict(line) for line in lines],
        }

    def issues(self) -> list[tuple[str, str, str]]:
        """(problem code, message, subject) for the advisor."""
        found = []
        for line in self.views():
            if line.state == NO_INTERNET:
                found.append(("W-LINE-NO-INTERNET", f"{line.name} ({line.kind}, {line.ip}): "
                              f"{line.note}", line.name))
            elif line.state == ACTIVE and line.shares_with:
                found.append(("I-LINE-SHARED", f"{line.name} and {line.shares_with} reach the "
                              f"internet through the same public address {line.public_ip}",
                              line.name))
        return found


def disabled_view() -> dict:
    return {"enabled": False, "mode": "off", "working": 0, "capacity": 0,
            "total_mbit_per_s": 0.0, "total_mb_per_s": 0.0, "error": None, "lines": []}
