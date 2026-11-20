"""The pass keeper: the station's own version of PeakAI's `peakai-tokens.sh` (decision D11).

Same protocol and timings as PeakAI's script:
- every 5 minutes, ask the token service which passes this laptop may use (`/v1/passes`);
- keep one token per pass, replaced when under 35 minutes are left (`/v1/token?format=pass`);
- after a failed fetch, keep the current token and try again in 1 minute;
- a pass PeakAI took away is dropped at once;
- a token that storage rejected (401) is never handed out again.
The laptop key is sent only in the `X-PeakAI-Key` header. Neither it nor any token is ever
logged, returned by `status()` or shown anywhere.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from src.core.exceptions import (
    ConfigurationError,
    PassDeniedError,
    PeakAiKeyError,
    PeakAiServiceError,
    UploaderError,
)
from src.core.settings import PeakAiSettings
from src.peakai.pack import PackSettings

logger = logging.getLogger(__name__)

LEGACY_PASSES = ("mono", "stereo")
Http = Callable[[str, str, dict, bytes | None, float], tuple[int, bytes]]


def urllib_http(method: str, url: str, headers: dict, body: bytes | None,
                timeout: float) -> tuple[int, bytes]:
    """Status 0 means no answer at all (no internet, DNS, timeout), like curl's 000."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, b""


@dataclass
class PassToken:
    name: str
    token: str = field(repr=False)
    expires_unix: int
    folder: str | None
    device: str | None


@dataclass
class PassState:
    last_error: str | None = None
    last_error_class: str | None = None
    fetched_at: float | None = None


def _brief(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ")
    return text[:120]


def _service_error(code: int, what: str, body: bytes) -> UploaderError:
    """PeakAI's README table of meanings, as typed errors."""
    if code == 401:
        return PeakAiKeyError(f"{what}: HTTP 401 unknown laptop key. This laptop's key was "
                              "switched off or is wrong; ask PeakAI")
    if code == 403:
        return PassDeniedError(f"{what}: HTTP 403 {_brief(body)}".strip())
    if code == 0:
        return PeakAiServiceError(f"{what}: no answer (HTTP 000, no internet?)")
    if code >= 500:
        return PeakAiServiceError(f"{what}: HTTP {code}, the service or Google did not answer "
                                  "just then")
    return PeakAiServiceError(f"{what}: HTTP {code} {_brief(body)}".strip())


class PassKeeper:
    def __init__(self, pack: PackSettings, settings: PeakAiSettings,
                 key_loader: Callable[[], str], http: Http = urllib_http,
                 clock: Callable[[], float] = time.time) -> None:
        self._pack = pack
        self._settings = settings
        self._key_loader = key_loader
        self._http = http
        self._clock = clock
        self._lock = threading.RLock()
        self._tokens: dict[str, PassToken] = {}
        self._states: dict[str, PassState] = {}
        self._allowed: list[str] | None = None
        self._passes_checked_at: float | None = None
        self._key_error: str | None = None
        self._service_error: str | None = None
        self._rejected: dict[str, str] = {}  # pass -> a token storage refused (401)

    # ------------------------------------------------------------------ requests

    def _get(self, path: str) -> tuple[int, bytes]:
        headers = {"X-PeakAI-Key": self._key_loader(), "Accept": "application/json"}
        return self._http("GET", self._pack.token_url + path, headers, None,
                          self._settings.http_timeout_seconds)

    def refresh_passes(self) -> list[str]:
        """Ask which passes this laptop may use now. Keeps the last list on failure."""
        code, body = self._get("/v1/passes")
        if code == 200:
            try:
                names = [str(item["name"]) for item in json.loads(body)["passes"]]
            except (ValueError, KeyError, TypeError) as exc:
                raise PeakAiServiceError(f"Pass list: unexpected answer ({exc})") from exc
            with self._lock:
                self._allowed = names
                self._passes_checked_at = self._clock()
                self._key_error = self._service_error = None
                for name in list(self._tokens):
                    if name not in self.usable_passes():
                        del self._tokens[name]  # Taken away: never used again.
                        logger.info("PeakAI pass %s is no longer allowed; token removed", name)
            return names
        error = _service_error(code, "Pass list", body)
        with self._lock:
            if isinstance(error, PeakAiKeyError):
                self._key_error = str(error)
            else:
                self._service_error = str(error)
        raise error

    def _fetch(self, name: str) -> PassToken:
        code, body = self._get(f"/v1/token?pass={name}&format=pass")
        if code != 200:
            raise _service_error(code, f"Token for {name}", body)
        fields: dict[str, str] = {}
        for line in body.decode("utf-8", errors="replace").splitlines():
            key, sep, value = line.partition(": ")
            if sep:
                fields[key.strip()] = value.strip()
        token = fields.get("token", "")
        try:
            expires = int(fields.get("expires_unix", ""))
        except ValueError:
            expires = 0
        if not token or expires <= self._clock():
            raise PeakAiServiceError(f"Token for {name}: the answer had no usable token")
        folder = fields.get(f"destination.{name}")
        expected = self._pack.passes[name].folder if name in self._pack.passes else None
        if folder and expected and folder.rstrip("/") + "/" != expected:
            raise PassDeniedError(f"Token for {name} is for folder {folder!r}, but the pack says "
                                  f"{expected!r}. Import PeakAI's latest pack")
        return PassToken(name, token, expires, folder, fields.get("device"))

    # ------------------------------------------------------------------ public

    def usable_passes(self) -> list[str]:
        """New passes first; the old mono/stereo passes only if nothing else is allowed."""
        with self._lock:
            allowed = list(self._allowed or [])
        new = [name for name in allowed if name not in LEGACY_PASSES]
        return [name for name in (new or allowed) if name in self._pack.passes]

    def legacy_only(self) -> bool:
        with self._lock:
            allowed = list(self._allowed or [])
        return bool(allowed) and all(name in LEGACY_PASSES for name in allowed)

    def token(self, name: str) -> tuple[str, int]:
        """A valid token for this pass (renewed first if under 35 minutes are left)."""
        if name not in self.usable_passes():
            if self._allowed is None:
                self.refresh_passes()
            if name not in self.usable_passes():
                raise PassDeniedError(f"This laptop may not use the {name} pass right now")
        with self._lock:
            current = self._tokens.get(name)
            rejected = self._rejected.get(name)
            left = current.expires_unix - self._clock() if current else 0
            fresh_enough = left >= self._settings.refresh_left_seconds
            if current and current.token != rejected and fresh_enough:
                return current.token, current.expires_unix
        state = self._states.setdefault(name, PassState())
        try:
            fresh = self._fetch(name)
        except UploaderError as exc:
            state.last_error, state.last_error_class = str(exc), type(exc).__name__
            if isinstance(exc, PeakAiKeyError):
                self._key_error = str(exc)
            usable = current and current.token != rejected and left > 60
            if usable and not isinstance(exc, PeakAiKeyError | PassDeniedError):
                return current.token, current.expires_unix  # Keep the current one meanwhile.
            raise
        with self._lock:
            self._tokens[name] = fresh
            self._rejected.pop(name, None)
            state.last_error = state.last_error_class = None
            state.fetched_at = self._clock()
        logger.info("PeakAI %s: new token, %d min left", name,
                    (fresh.expires_unix - self._clock()) // 60)
        return fresh.token, fresh.expires_unix

    def reject(self, name: str, token: str) -> None:
        """Storage refused this token (401): fetch a new one before the next use."""
        with self._lock:
            self._rejected[name] = token

    def maintain(self) -> bool:
        """One round, like `peakai-tokens.sh --once`. True when every pass has a token."""
        try:
            self.refresh_passes()
        except UploaderError as exc:
            logger.warning("PeakAI %s", exc)
        ok = True
        for name in self.usable_passes():
            try:
                self.token(name)
            except UploaderError as exc:
                logger.warning("PeakAI %s", exc)
                ok = False
        return ok and bool(self.usable_passes())

    def ensure_ready(self) -> None:
        """Before uploading: the passes are known and at least one has a token."""
        if self._allowed is None:
            self.refresh_passes()
        if self.legacy_only():
            raise ConfigurationError("PeakAI has not switched this laptop to the new passes yet "
                                     "(only the old mono/stereo passes are allowed)")
        names = self.usable_passes()
        if not names:
            raise ConfigurationError("This laptop has no PeakAI passes. Ask PeakAI to add them")
        errors = []
        for name in names:
            try:
                self.token(name)
                return
            except UploaderError as exc:
                errors.append(exc)
        raise errors[0]

    def run(self, stop: threading.Event) -> None:
        """Keep tokens fresh until `stop` is set (the daemon runs this in a thread)."""
        while not stop.is_set():
            ok = self.maintain()
            wait = (self._settings.check_every_seconds if ok
                    else self._settings.retry_every_seconds)
            stop.wait(wait)

    def status(self) -> dict:
        """For the console and the AI. Contains no token and no key."""
        now = self._clock()
        with self._lock:
            passes = []
            for name in self.usable_passes():
                token = self._tokens.get(name)
                state = self._states.get(name, PassState())
                passes.append({
                    "name": name,
                    "folder": self._pack.passes[name].folder,
                    "description": self._pack.passes[name].description,
                    "minutes_left": max(0, int((token.expires_unix - now) // 60)) if token else 0,
                    "has_token": bool(token and token.expires_unix > now),
                    "device": token.device if token else None,
                    "last_error": state.last_error,
                    "last_error_class": state.last_error_class,
                })
            return {
                "allowed": list(self._allowed) if self._allowed is not None else None,
                "passes": passes,
                "legacy_only": self.legacy_only(),
                "checked_at": self._passes_checked_at,
                "key_error": self._key_error,
                "service_error": self._service_error,
            }
