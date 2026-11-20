"""`main.py daemon`: the always-on station process (run by systemd on Linux)."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import IO, Any

from src.core.exceptions import ConfigurationError, UploaderError
from src.core.timeutil import utc_now
from src.database.station_repository import StationRepository
from src.metadata.templates import load_metadata_file
from src.station.events import EventBus
from src.station.sources import PollResult, SourceIssue, UdevWaker, create_source
from src.station.station import Station, StationError
from src.station.systemd import SystemdNotifier, status_line

# The watchdog is fed only while the main loop finishes a pass within this time. A pass can
# legitimately wait on the network for a while; a station stuck longer than this is restarted.
STUCK_AFTER_SECONDS = 300

logger = logging.getLogger(__name__)


def build_profile(app):
    """The PeakAI profile when a pack is imported; otherwise the generic destination."""
    from src.database.peakai_repository import PeakAiRepository
    from src.peakai.keeper import PassKeeper
    from src.peakai.pack import load_active, read_key
    from src.peakai.profile import PeakAiProfile
    from src.peakai.store import PeakAiStore

    settings = app.settings
    pack = load_active(settings.resolve(settings.peakai.pack_dir))
    if pack is None:
        return None, app.store
    key_path = settings.resolve(settings.peakai.key_file)

    def key_loader() -> str:
        return read_key(key_path)

    keeper = PassKeeper(pack, settings.peakai, key_loader)
    store = PeakAiStore(pack, keeper, settings.upload)
    profile = PeakAiProfile(settings, pack, keeper, store, app.sessions, app.scans,
                            PeakAiRepository(app.db), key_loader)
    logger.info("PeakAI pack %s (v%s) is active: uploads go to gs://%s", pack.source_name,
                pack.version, pack.bucket)
    return profile, profile.store


class SingleInstance:
    """Holds an OS file lock so two stations can never drive the same database."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a+", encoding="utf-8")  # noqa: SIM115 - held for the run
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            raise ConfigurationError(
                f"Another station is already running (lock {self._path})") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def write_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic replace, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def read_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Daemon:
    def __init__(self, app, simulate_dir: str | None = None,
                 metadata_file: str | None = None) -> None:
        self._app = app
        settings = app.settings
        if simulate_dir:
            settings.station.simulator_dir = str(Path(simulate_dir).resolve())
        self._settings = settings
        self._metadata_file = metadata_file
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.bus = EventBus()
        self.keeper = None
        profile, store_factory = build_profile(app)
        if profile is not None:
            self.keeper = profile.keeper
        self.station = Station(settings, app.sessions, app.scans, StationRepository(app.db),
                               store_factory, app.finalizer_or_offline, self.bus,
                               profile=profile)
        self._keeper_stop = threading.Event()
        self._source = create_source(settings.station, app.adapter, app.inventory,
                                     settings.base_dir)
        self._state_path = settings.resolve(settings.station.state_file)
        self._lock = SingleInstance(settings.resolve(settings.station.lock_file))
        self._started_at = utc_now()
        # Every station event wakes the loop, so the state file follows changes quickly.
        self.bus.subscribe(lambda _event: self._wake.set())
        self.api = None
        self.api_error: str | None = None
        self.systemd = SystemdNotifier()
        self._last_pass = time.monotonic()
        self._status = "Starting"

    def request_stop(self, *_: object) -> None:
        self._stop.set()
        self._wake.set()

    def run(self, max_seconds: float | None = None) -> int:
        self._lock.acquire()
        waker = UdevWaker(self._wake)
        try:
            self.station.restore()
            self._open_from_file()
            self._start_api()
            if self.keeper is not None:  # Keeps every pass's token fresh, like peakai-tokens.sh.
                threading.Thread(target=self.keeper.run, args=(self._keeper_stop,),
                                 name="peakai-keeper", daemon=True).start()
            if not self._settings.station.simulator_dir:
                waker.start()
            self._install_signals()
            deadline = time.monotonic() + max_seconds if max_seconds else None
            logger.info("Station running (pid %s)", os.getpid())
            self.tick()
            self.systemd.ready(self._status)
            if self.systemd.watchdog_seconds:
                threading.Thread(target=self._feed_watchdog, name="systemd-watchdog",
                                 daemon=True).start()
            while not self._stop.is_set():
                if deadline and time.monotonic() >= deadline:
                    break
                self._wake.wait(self._settings.station.poll_seconds)
                self._wake.clear()
                if self._stop.is_set():
                    break
                self.tick()
        finally:
            self.systemd.stopping()
            self._keeper_stop.set()
            waker.stop()
            if self.api is not None:
                self.api.stop()
            logger.info("Station stopping: finishing files in progress")
            self.station.shutdown()
            self._write_state(stopped=True)
            self._lock.release()
        return 0

    def tick(self) -> None:
        try:
            poll = self._source.poll()
        except UploaderError as exc:
            poll = PollResult([], [SourceIssue("W-DETECTION", f"Device detection failed: {exc}")])
        try:
            self.station.update_presence(poll)
            self.station.handle_commands()
            self.station.ensure_engine()
        except UploaderError as exc:
            logger.error("Station tick failed: %s", exc)
        state = self._write_state()
        self._status = status_line(state)
        self._last_pass = time.monotonic()

    def _feed_watchdog(self) -> None:
        """Ping systemd at half its watchdog interval, but only while the loop is alive."""
        interval = max(1.0, (self.systemd.watchdog_seconds or 60) / 2)
        while not self._stop.wait(interval):
            if time.monotonic() - self._last_pass < STUCK_AFTER_SECONDS:
                self.systemd.watchdog(self._status)
            else:
                logger.error("Station loop has not finished a pass for %ss; letting systemd "
                             "restart it", STUCK_AFTER_SECONDS)

    def _open_from_file(self) -> None:
        if not self._metadata_file or self.station.view().session is not None:
            return
        try:
            session_id = self.station.open_session(load_metadata_file(self._metadata_file))
            logger.info("Opened session %s from %s", session_id, self._metadata_file)
        except StationError as exc:
            raise ConfigurationError(str(exc)) from exc

    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        signal.signal(signal.SIGINT, self.request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self.request_stop)

    def daemon_info(self, stopped: bool = False) -> dict:
        api = self._settings.api
        return {
            "pid": os.getpid(),
            "started_at": self._started_at,
            "running": not stopped,
            "source": "simulator" if self._settings.station.simulator_dir else "devices",
            "poll_seconds": self._settings.station.poll_seconds,
            "api": {
                "enabled": api.enabled,
                "addresses": self.api.addresses if self.api else [],
                "error": self.api_error or (self.api.error if self.api else None),
            },
        }

    def _start_api(self) -> None:
        settings = self._settings.api
        if not settings.enabled:
            return
        from src.api.app import ApiServices, create_app  # Only the daemon needs the web stack.
        from src.api.auth import load_or_create_token
        from src.api.server import ApiServer

        app = self._app
        token = load_or_create_token(self._settings.resolve(settings.token_file))

        def hardware() -> dict | None:
            snapshot = app.inventory.latest()
            return asdict(snapshot) if snapshot else None

        def report(session_id: str) -> dict | None:
            return app.reports().build(session_id) if app.sessions.get(session_id) else None

        from src.ai.agent import AiTroubleshooter
        from src.database.ai_repository import AiAuditRepository

        ai = AiTroubleshooter(self._settings, self.station, AiAuditRepository(app.db))
        services = ApiServices(self.station, token, self.daemon_info, app.sessions.recent,
                               report, hardware, ai=ai, settings=self._settings)
        socket = settings.unix_socket
        self.api = ApiServer(create_app(services), settings.host, settings.port,
                             str(self._settings.resolve(socket)) if socket else "")
        if not self.api.start():
            self.api_error = self.api.error  # Uploads continue; the CLI falls back to the DB.

    def _write_state(self, stopped: bool = False) -> dict:
        state = self.station.view().to_dict()
        state["daemon"] = self.daemon_info(stopped)
        try:
            write_state(self._state_path, state)
        except OSError as exc:
            logger.warning("Could not write %s: %s", self._state_path, exc)
        return state
