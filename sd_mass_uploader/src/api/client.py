"""A small client for the station API, on the standard library only (the CLI stays light)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote

from src.api.auth import read_token
from src.core.settings import Settings


class ApiUnavailable(Exception):
    """The station API could not be reached (not running, or this user lacks the token)."""


def console_link(settings: Settings) -> str | None:
    """The console address with the sign-in token after '#', or None if this user may not
    read the token. The fragment never reaches the server or its logs."""
    api = settings.api
    token = read_token(settings.resolve(api.token_file))
    if token is None:
        return None
    host = f"[{api.host}]" if ":" in api.host else api.host
    return f"http://{host}:{api.port}/#token={token}"


class StationClient:
    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    @classmethod
    def from_settings(cls, settings: Settings, timeout: float = 15.0) -> StationClient | None:
        api = settings.api
        if not api.enabled:
            return None
        token = read_token(settings.resolve(api.token_file))
        if token is None:
            return None
        host = f"[{api.host}]" if ":" in api.host else api.host
        return cls(f"http://{host}:{api.port}", token, timeout)

    def request(self, method: str, path: str, body: dict | None = None,
                timeout: float | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self._base}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._timeout) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"null")
            except ValueError:
                payload = None
            return exc.code, payload
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ApiUnavailable(str(exc)) from exc

    def state(self) -> dict:
        status, payload = self.request("GET", "/api/v1/state")
        if status != 200:
            raise ApiUnavailable(f"HTTP {status}: {payload}")
        return payload


# How each station command maps onto the API: (method, path, body builder).
def route(command: str, args: dict) -> tuple[str, str, dict | None]:
    card = quote(str(args.get("card_id", "")), safe="")
    simple = {
        "pause_all": "/api/v1/session/pause-all",
        "resume_all": "/api/v1/session/resume-all",
        "start_all": "/api/v1/session/start-all",
        "resume_engine": "/api/v1/engine/resume",
        "retry_reports": "/api/v1/destination/retry-reports",
    }
    if command in simple:
        return "POST", simple[command], None
    if command == "open":
        return "POST", "/api/v1/session", {"metadata": args.get("metadata") or {},
                                           "auto_upload": args.get("auto_upload")}
    if command == "close":
        return "POST", "/api/v1/session/close", {"force": bool(args.get("force"))}
    if command == "auto_upload":
        return "POST", "/api/v1/session/auto-upload", {"enabled": bool(args.get("enabled"))}
    if command == "place":
        return "POST", f"/api/v1/cards/{card}/place", {"device": args.get("device"),
                                                       "skip": bool(args.get("skip"))}
    if command in {"pause", "resume", "start", "confirm", "retry"}:
        return "POST", f"/api/v1/cards/{card}/{command}", None
    raise ValueError(f"Unknown command: {command}")
