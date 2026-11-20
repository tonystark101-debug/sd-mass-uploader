"""The station's local API: REST for reads and commands, one WebSocket for live updates.

Security: it listens on loopback (and optionally a Unix socket) only. Every request except
/health needs the token from `api.token_file`. The Host header must be a loopback name, which
blocks DNS-rebinding attacks from web pages in a local browser.

WebSocket protocol (/api/v1/ws):
  client -> {"type": "auth", "token": "..."}   within 5 seconds, or the socket is closed (4401)
  server -> {"type": "state", "state": {...}}  at once, then every second
  server -> {"type": "event", "event": {...}}  for every station event as it happens
  server -> {"type": "resync"}                 events were dropped; the next state is complete
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketDisconnect

from src.advisor.catalog import CATALOG, FIX_LABELS, FixAction
from src.api.auth import token_matches
from src.core.enums import TaskStatus
from src.core.exceptions import UploaderError
from src.station.events import Event
from src.station.station import Station, StationError, StationNotFound

logger = logging.getLogger(__name__)

API_VERSION = "1"
LOOPBACK_HOST_NAMES = ["127.0.0.1", "localhost", "::1", "[::1]"]
_AUTH_TIMEOUT_SECONDS = 5.0
_STATE_INTERVAL_SECONDS = 1.0
_QUEUE_LIMIT = 1000
_MAX_PACK_BYTES = 5 * 1024 * 1024
CONSOLE_DIR = Path(__file__).resolve().parents[1] / "web_dist"  # Built from web/ by Vite.

# The console is served from here only. No inline scripts, no third-party anything, no framing.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


@dataclass
class ApiServices:
    """What the API can reach. Callables keep it independent of the daemon and the CLI app."""

    station: Station
    token: str
    daemon_info: Callable[[], dict] = dict
    list_sessions: Callable[[int], list[dict]] = lambda _limit: []
    session_report: Callable[[str], dict | None] = lambda _session_id: None
    hardware: Callable[[], dict | None] = lambda: None
    allowed_hosts: list[str] = field(default_factory=lambda: list(LOOPBACK_HOST_NAMES))
    ai: Any = None  # AiTroubleshooter, when available.
    settings: Any = None  # Settings, for the Setup screen (non-secret parts only).


# ------------------------------------------------------------------ request bodies

class OpenSessionRequest(BaseModel):
    metadata: dict[str, Any] = Field(
        description="activity_name, recording_date (YYYY-MM-DD), project_name, "
                    "place_of_recording, operator_name, description, custom (object)")
    auto_upload: bool | None = Field(
        None, description="Upload each card as soon as it is scanned (default from config)")


class CloseSessionRequest(BaseModel):
    force: bool = Field(False, description="End even with unfinished cards; they can be "
                                           "finished later with `retry`")


class AutoUploadRequest(BaseModel):
    enabled: bool


class PlaceCardRequest(BaseModel):
    device: str | None = Field(None, description="The device that recorded the unplaced files")
    skip: bool = Field(False, description="Do not upload the unplaced files")


class AiStartRequest(BaseModel):
    problem_id: str | None = Field(None, description="An open problem's id, from /problems")
    question: str | None = Field(None, max_length=4000, description="What the operator asks")
    operator: str | None = Field(None, max_length=100)


class AiMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    operator: str | None = Field(None, max_length=100)


class AiDecisionRequest(BaseModel):
    approve: bool
    operator: str | None = Field(None, max_length=100, description="Who decided, for the audit")


# ------------------------------------------------------------------ app

def state_payload(services: ApiServices) -> dict[str, Any]:
    state = services.station.view().to_dict()
    state["daemon"] = services.daemon_info()
    return state


def create_app(services: ApiServices) -> FastAPI:
    station = services.station
    app = FastAPI(
        title="SD Mass Uploader station API",
        version=API_VERSION,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        description="Local control and live status for the SD card upload station. "
                    "Send `Authorization: Bearer <token>`; the token is in `api.token_file`.",
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=services.allowed_hosts)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            if name == "Cache-Control" and request.url.path.startswith("/assets/"):
                continue  # Hashed file names: safe to cache.
            response.headers.setdefault(name, value)
        return response

    def require_token(authorization: str | None = Header(None)) -> None:
        supplied = None
        if authorization and authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not token_matches(services.token, supplied):
            raise HTTPException(status_code=401, detail="Missing or wrong API token",
                                headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(StationNotFound)
    async def _not_found(_request, exc: StationNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(StationError)
    async def _refused(_request, exc: StationError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(UploaderError)
    async def _failed(_request, exc: UploaderError) -> JSONResponse:
        return JSONResponse(status_code=500,
                            content={"detail": f"{type(exc).__name__}: {exc}"})

    @app.get("/api/v1/health", tags=["status"])
    def health() -> dict:
        """No token needed: lets a client tell "not running" from "not allowed"."""
        return {"status": "ok", "api_version": API_VERSION}

    auth = [Depends(require_token)]

    # --- reads

    @app.get("/api/v1/state", tags=["status"], dependencies=auth)
    def get_state() -> dict:
        """Everything the dashboard shows: session, counters, cards, progress, engine, issues."""
        return state_payload(services)

    @app.get("/api/v1/cards", tags=["cards"], dependencies=auth)
    def list_cards() -> list[dict]:
        return [asdict(card) for card in station.view().cards]

    @app.get("/api/v1/cards/{card_id}", tags=["cards"], dependencies=auth)
    def card_detail(card_id: str,
                    status: TaskStatus | None = Query(None, description="Only this file status"),
                    limit: int = Query(200, ge=1, le=1000),
                    offset: int = Query(0, ge=0)) -> dict:
        """One card with a page of its files (problems first) and its scan errors."""
        return station.card_detail(card_id, status.value if status else None, limit, offset)

    @app.get("/api/v1/events", tags=["status"], dependencies=auth)
    def events(after: int = Query(0, ge=0, description="Last sequence number seen")) -> list:
        """Recent events, for clients that cannot use the WebSocket."""
        return station.recent_events(after)

    @app.get("/api/v1/problems", tags=["problems"], dependencies=auth)
    def problems(history: int = Query(0, ge=0, le=500,
                                      description="Also return this many past problems")) -> dict:
        """Open problems, most severe first, each with steps and a fix action."""
        result: dict[str, Any] = {"open": station.view().problems}
        if history:
            result["history"] = station.problem_history(history)
        return result

    @app.get("/api/v1/problems/catalog", tags=["problems"], dependencies=auth)
    def problem_catalog() -> list[dict]:
        """Every problem the station can report: the help pages."""
        return [_spec_dict(spec) for spec in CATALOG.values()]

    @app.get("/api/v1/problems/catalog/{code}", tags=["problems"], dependencies=auth)
    def problem_help(code: str) -> dict:
        spec = CATALOG.get(code)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"Unknown problem code: {code}")
        return _spec_dict(spec)

    @app.get("/api/v1/hardware", tags=["status"], dependencies=auth)
    def hardware() -> dict:
        """The latest saved device inventory: hubs, readers, link speeds, volumes."""
        snapshot = services.hardware()
        if snapshot is None:
            raise HTTPException(status_code=404, detail="No device inventory saved yet")
        return snapshot

    @app.get("/api/v1/sessions", tags=["history"], dependencies=auth)
    def sessions(limit: int = Query(50, ge=1, le=500)) -> list[dict]:
        return services.list_sessions(limit)

    @app.get("/api/v1/sessions/{session_id}/report", tags=["history"], dependencies=auth)
    def session_report(session_id: str) -> dict:
        report = services.session_report(session_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"Unknown session: {session_id}")
        return report

    # --- session commands

    @app.post("/api/v1/session", tags=["session"], dependencies=auth, status_code=201)
    def open_session(body: OpenSessionRequest) -> dict:
        """Open a session. Cards already inserted, and every card inserted later, join it."""
        return {"session_id": station.open_session(body.metadata, body.auto_upload)}

    @app.post("/api/v1/session/close", tags=["session"], dependencies=auth)
    def close_session(body: CloseSessionRequest) -> dict:
        """Waits for files in progress, then writes reports and manifests."""
        return {"status": station.close_session(body.force)}

    @app.post("/api/v1/session/pause-all", tags=["session"], dependencies=auth)
    def pause_all() -> dict:
        station.pause_all()
        return {"ok": True}

    @app.post("/api/v1/session/resume-all", tags=["session"], dependencies=auth)
    def resume_all() -> dict:
        station.resume_all()
        return {"ok": True}

    @app.post("/api/v1/session/start-all", tags=["session"], dependencies=auth)
    def start_all() -> dict:
        return {"started": station.start_all()}

    @app.post("/api/v1/session/auto-upload", tags=["session"], dependencies=auth)
    def auto_upload(body: AutoUploadRequest) -> dict:
        station.set_auto_upload(body.enabled)
        return {"ok": True}

    @app.post("/api/v1/engine/resume", tags=["session"], dependencies=auth)
    def resume_engine() -> dict:
        """After fixing what stopped uploads (sign-in, permission, settings)."""
        station.resume_engine()
        return {"ok": True}

    # --- card commands

    @app.post("/api/v1/cards/{card_id}/pause", tags=["cards"], dependencies=auth)
    def pause(card_id: str) -> dict:
        station.pause_card(card_id)
        return {"ok": True}

    @app.post("/api/v1/cards/{card_id}/resume", tags=["cards"], dependencies=auth)
    def resume(card_id: str) -> dict:
        station.resume_card(card_id)
        return {"ok": True}

    @app.post("/api/v1/cards/{card_id}/start", tags=["cards"], dependencies=auth)
    def start(card_id: str) -> dict:
        station.start_card(card_id)
        return {"ok": True}

    @app.post("/api/v1/cards/{card_id}/confirm", tags=["cards"], dependencies=auth)
    def confirm(card_id: str) -> dict:
        station.confirm_card(card_id)
        return {"ok": True}

    @app.post("/api/v1/cards/{card_id}/place", tags=["cards"], dependencies=auth)
    def place(card_id: str, body: PlaceCardRequest) -> dict:
        """PeakAI: say which device recorded a held card's unplaced files, or skip them."""
        return {"result": station.place_card(card_id, body.device, body.skip)}

    @app.get("/api/v1/destination", tags=["status"], dependencies=auth)
    def destination() -> dict:
        """Where uploads go. PeakAI: pack, passes and token time left, dashboard entries,
        _info files and how each card was placed. Never a token or key."""
        view = station.view()
        return view.destination

    @app.post("/api/v1/destination/retry-reports", tags=["session"], dependencies=auth)
    def retry_reports() -> dict:
        """PeakAI: send dashboard entries and _info files that failed, now."""
        station.retry_reports()
        return {"ok": True}

    @app.post("/api/v1/cards/{card_id}/retry", tags=["cards"], dependencies=auth)
    def retry(card_id: str) -> dict:
        return {"requeued_files": station.retry_card(card_id)}

    # --- AI troubleshooter

    def require_ai():
        if services.ai is None:
            raise HTTPException(status_code=503, detail="The AI troubleshooter is not available")
        return services.ai

    @app.get("/api/v1/ai", tags=["ai"], dependencies=auth)
    def ai_info() -> dict:
        if services.ai is None:
            return {"enabled": False, "sessions": []}
        return services.ai.info()

    @app.post("/api/v1/ai/sessions", tags=["ai"], dependencies=auth, status_code=201)
    def ai_start(body: AiStartRequest) -> dict:
        """Ask the AI about a problem (or a question). It diagnoses on its own; station actions
        follow `ai.auto_station_actions`; every shell command waits for an approval."""
        ai = require_ai()
        return {"ai_session_id": ai.start(body.problem_id, body.question, body.operator)}

    @app.get("/api/v1/ai/sessions/{ai_session_id}", tags=["ai"], dependencies=auth)
    def ai_session(ai_session_id: str) -> dict:
        return require_ai().get(ai_session_id)

    @app.post("/api/v1/ai/sessions/{ai_session_id}/messages", tags=["ai"], dependencies=auth)
    def ai_message(ai_session_id: str, body: AiMessageRequest) -> dict:
        require_ai().send(ai_session_id, body.text, body.operator)
        return {"ok": True}

    @app.post("/api/v1/ai/sessions/{ai_session_id}/approvals/{approval_id}", tags=["ai"],
              dependencies=auth)
    def ai_decide(ai_session_id: str, approval_id: str, body: AiDecisionRequest) -> dict:
        """Approve or decline the exact command or action the AI is waiting on."""
        require_ai().decide(ai_session_id, approval_id, body.approve, body.operator)
        return {"ok": True}

    @app.post("/api/v1/ai/sessions/{ai_session_id}/stop", tags=["ai"], dependencies=auth)
    def ai_stop(ai_session_id: str) -> dict:
        require_ai().stop(ai_session_id)
        return {"ok": True}

    # --- settings and the PeakAI pack (Setup screen)

    def require_settings():
        if services.settings is None:
            raise HTTPException(status_code=503, detail="Settings are not available here")
        return services.settings

    @app.get("/api/v1/settings", tags=["setup"], dependencies=auth)
    def settings_view() -> dict:
        """What the station is set to. No paths to secrets, no keys."""
        s = require_settings()
        return {
            "station": {"auto_upload": s.station.auto_upload,
                        "poll_seconds": s.station.poll_seconds,
                        "auto_mount": s.station.auto_mount,
                        "simulator": bool(s.station.simulator_dir)},
            "upload": {"max_workers": s.upload.max_workers,
                       "chunk_size_mib": s.upload.chunk_size_mib,
                       "max_retries": s.upload.max_retries,
                       "verify_after_upload": s.upload.verify_after_upload,
                       "duplicate_policy": s.upload.duplicate_policy.value},
            "ai": {"enabled": s.ai.enabled, "model": s.ai.model, "effort": s.ai.effort,
                   "auto_station_actions": s.ai.auto_station_actions,
                   "redact_personal": s.ai.redact_personal,
                   "key_present": bool(os.environ.get("ANTHROPIC_API_KEY"))},
            "peakai": {"vendor_code": s.peakai.vendor_code,
                       "machine_name": s.peakai.machine_name or None},
            "api": {"host": s.api.host, "port": s.api.port},
        }

    from src.peakai.pack import discard_upload

    def _upload_path() -> Path:
        s = require_settings()
        return s.resolve(s.peakai.pack_dir) / "upload.zip"

    @app.post("/api/v1/pack/check", tags=["setup"], dependencies=auth)
    async def pack_check(request: Request, offline: bool = False) -> dict:
        """Send a pack zip as the request body. It is checked and kept (privately) until it is
        applied or cancelled. Nothing changes yet. The key is never returned."""
        from src.peakai.importing import check_pack, report_summary
        from src.peakai.pack import write_private_bytes

        body = await request.body()
        if not body or len(body) > _MAX_PACK_BYTES:
            raise HTTPException(status_code=400, detail="Send the pack zip (at most 5 MB)")
        path = _upload_path()
        await run_in_threadpool(write_private_bytes, path, body)
        report = await run_in_threadpool(check_pack, path, require_settings().peakai, offline)
        if not report.ok:
            discard_upload(path)
        return report_summary(report)

    @app.post("/api/v1/pack/apply", tags=["setup"], dependencies=auth)
    def pack_apply() -> dict:
        """Install the pack that was checked. Restart the station to use it."""
        from src.peakai.importing import check_pack
        from src.peakai.pack import apply_pack

        s = require_settings()
        path = _upload_path()
        if not path.exists():
            raise HTTPException(status_code=409, detail="Check a pack first")
        try:
            report = check_pack(path, s.peakai, offline=True)
            if not report.ok:
                raise HTTPException(status_code=409, detail="; ".join(report.errors))
            pack = apply_pack(report, s.resolve(s.peakai.pack_dir), s.resolve(s.peakai.key_file))
        finally:
            discard_upload(path)  # The upload holds the key: never keep it.
        return {"applied": pack.source_name, "restart_needed": True}

    @app.post("/api/v1/pack/cancel", tags=["setup"], dependencies=auth)
    def pack_cancel() -> dict:
        discard_upload(_upload_path())
        return {"ok": True}

    # --- live updates

    @app.websocket("/api/v1/ws")
    async def live(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            message = await asyncio.wait_for(websocket.receive_text(), _AUTH_TIMEOUT_SECONDS)
            supplied = json.loads(message).get("token") if message.startswith("{") else None
        except (TimeoutError, ValueError, WebSocketDisconnect):
            supplied = None
        if not token_matches(services.token, supplied):
            with contextlib.suppress(Exception):
                await websocket.close(code=4401, reason="Missing or wrong API token")
            return
        await _stream(websocket, services)

    _mount_console(app)
    return app


def _mount_console(app: FastAPI) -> None:
    """The Operator Console: static files built from web/. Login is by a link from
    `main.py console`; the token is in the link's #fragment, which browsers never send."""
    index = CONSOLE_DIR / "index.html"
    if (CONSOLE_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=CONSOLE_DIR / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def console():
        if index.exists():
            return FileResponse(index, media_type="text/html")
        return HTMLResponse("<h1>Console not built</h1><p>Run <code>npm run build</code> in "
                            "the web folder.</p>", status_code=503)

    for name in ("favicon.svg",):
        target = CONSOLE_DIR / name
        if target.exists():
            app.add_api_route(f"/{name}", lambda target=target: FileResponse(target),
                              include_in_schema=False)


def _spec_dict(spec) -> dict:
    return {
        "code": spec.code,
        "severity": spec.severity.value,
        "title": spec.title,
        "message": spec.message,
        "steps": list(spec.steps),
        "action": None if spec.action is FixAction.NONE else {
            "kind": spec.action.value, "label": FIX_LABELS[spec.action]},
        "card_scoped": spec.card_scoped,
        "tags": list(spec.tags),
    }


async def _stream(websocket: WebSocket, services: ApiServices) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=_QUEUE_LIMIT)
    overflowed = False

    def push(event: Event) -> None:  # Called from station threads.
        def put() -> None:
            nonlocal overflowed
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                overflowed = True
        loop.call_soon_threadsafe(put)

    unsubscribe = services.station.bus.subscribe(push)
    try:
        await websocket.send_json({"type": "state",
                                   "state": await run_in_threadpool(state_payload, services)})
        next_state = loop.time() + _STATE_INTERVAL_SECONDS
        while True:
            timeout = max(0.0, next_state - loop.time())
            try:
                event = await asyncio.wait_for(queue.get(), timeout)
            except TimeoutError:
                event = None
            if event is not None:
                await websocket.send_json({"type": "event", "event": asdict(event)})
            if overflowed:
                overflowed = False
                await websocket.send_json({"type": "resync"})
                next_state = 0.0
            if loop.time() >= next_state:
                state = await run_in_threadpool(state_payload, services)
                await websocket.send_json({"type": "state", "state": state})
                next_state = loop.time() + _STATE_INTERVAL_SECONDS
    except (WebSocketDisconnect, RuntimeError):
        pass  # Client went away.
    finally:
        unsubscribe()
