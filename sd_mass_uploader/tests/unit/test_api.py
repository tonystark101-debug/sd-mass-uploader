"""The station API: auth, host check, reads, commands, WebSocket stream, server and client."""

from __future__ import annotations

import os
import socket
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.api.app import ApiServices, create_app
from src.api.auth import load_or_create_token, read_token, token_matches
from src.api.client import ApiUnavailable, StationClient, route
from src.api.server import ApiServer
from src.core.enums import CardState
from tests.unit.test_station import METADATA, StationHarness

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def harness(tmp_path):
    h = StationHarness(tmp_path)
    yield h
    h.close()


@pytest.fixture
def client(harness):
    services = ApiServices(
        harness.station, TOKEN, lambda: {"pid": 1, "running": True},
        harness.sessions.recent,
        lambda sid: {"session_id": sid} if harness.sessions.get(sid) else None,
    )
    with TestClient(create_app(services), base_url="http://127.0.0.1") as test_client:
        yield test_client


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ------------------------------------------------------------------ security

def test_health_is_open_but_everything_else_needs_the_token(client):
    assert client.get("/api/v1/health").json()["status"] == "ok"
    assert client.get("/api/v1/state").status_code == 401
    assert client.get("/api/v1/state", headers={"Authorization": "Bearer nope"}).status_code \
        == 401
    assert client.post("/api/v1/session/pause-all").status_code == 401
    assert client.get("/api/v1/state", headers=AUTH).status_code == 200


def test_non_loopback_host_header_is_rejected(client):
    """Blocks DNS rebinding: a web page cannot reach the API through its own domain."""
    response = client.get("/api/v1/health", headers={"Host": "evil.example.com"})
    assert response.status_code == 400


def test_token_comparison():
    assert token_matches("abc", "abc")
    assert not token_matches("abc", "abd") and not token_matches("abc", None)
    assert not token_matches("abc", "")


def test_token_file_is_created_once_and_private(tmp_path):
    path = tmp_path / "station" / "api-token"
    first = load_or_create_token(path)
    assert len(first) >= 40 and load_or_create_token(path) == first
    assert read_token(path) == first
    assert read_token(tmp_path / "missing") is None
    if os.name == "posix":
        assert path.stat().st_mode & 0o007 == 0  # No access for other users.


# ------------------------------------------------------------------ reads and commands

def test_session_and_card_flow_over_http(client, harness):
    assert client.post("/api/v1/session/pause-all", headers=AUTH).status_code == 404
    response = client.post("/api/v1/session", headers=AUTH,
                           json={"metadata": METADATA, "auto_upload": False})
    assert response.status_code == 201
    session_id = response.json()["session_id"]
    again = client.post("/api/v1/session", headers=AUTH, json={"metadata": METADATA})
    assert again.status_code == 409 and "already open" in again.json()["detail"]

    harness.card("A", 3)
    harness.wait_states(A=CardState.READY)
    state = client.get("/api/v1/state", headers=AUTH).json()
    assert state["session"]["session_id"] == session_id
    assert state["counters"]["ready"] == 1 and state["daemon"]["pid"] == 1
    assert [card["card_id"] for card in client.get("/api/v1/cards", headers=AUTH).json()] \
        == ["sim-A"]

    detail = client.get("/api/v1/cards/sim-A", headers=AUTH).json()
    assert detail["card"]["state"] == "READY" and len(detail["files"]) == 3
    assert detail["file_status_counts"] == {"PENDING": 3}
    page = client.get("/api/v1/cards/sim-A?status=PENDING&limit=2", headers=AUTH).json()
    assert len(page["files"]) == 2
    assert client.get("/api/v1/cards/sim-A?status=BOGUS", headers=AUTH).status_code == 422
    assert client.get("/api/v1/cards/nope", headers=AUTH).status_code == 404
    assert client.post("/api/v1/cards/nope/pause", headers=AUTH).status_code == 404

    assert client.post("/api/v1/cards/sim-A/pause", headers=AUTH).json() == {"ok": True}
    assert client.post("/api/v1/cards/sim-A/start", headers=AUTH).status_code == 200
    harness.wait_states(A=CardState.PAUSED)
    closing = client.post("/api/v1/session/close", headers=AUTH, json={})
    assert closing.status_code == 409 and "not finished" in closing.json()["detail"]
    client.post("/api/v1/cards/sim-A/resume", headers=AUTH)
    harness.wait_states(A=CardState.UPLOADED)
    assert client.post("/api/v1/session/close", headers=AUTH, json={}).json() \
        == {"status": "SUCCESS"}

    sessions = client.get("/api/v1/sessions", headers=AUTH).json()
    assert sessions[0]["session_id"] == session_id
    assert client.get(f"/api/v1/sessions/{session_id}/report", headers=AUTH).status_code == 200
    assert client.get("/api/v1/sessions/nope/report", headers=AUTH).status_code == 404
    assert client.get("/api/v1/hardware", headers=AUTH).status_code == 404


def test_custom_metadata_fields_are_accepted(client):
    """The same JSON as session.example.json, including its nested custom fields."""
    body = {"metadata": {**METADATA, "custom": {"camera_group": "A"}}}
    assert client.post("/api/v1/session", headers=AUTH, json=body).status_code == 201


def test_invalid_metadata_is_refused_with_the_reason(client):
    response = client.post("/api/v1/session", headers=AUTH,
                           json={"metadata": {"activity_name": "x"}})
    assert response.status_code == 409 and "not valid" in response.json()["detail"]
    assert client.post("/api/v1/session", headers=AUTH, json={}).status_code == 422


def test_events_endpoint_returns_events_after_a_sequence(client):
    client.post("/api/v1/session", headers=AUTH, json={"metadata": METADATA})
    events = client.get("/api/v1/events", headers=AUTH).json()
    assert events[-1]["kind"] == "session_opened"
    later = client.get(f"/api/v1/events?after={events[-1]['sequence']}", headers=AUTH).json()
    assert later == []


def test_openapi_documents_the_endpoints(client):
    paths = client.get("/api/openapi.json").json()["paths"]
    assert "/api/v1/cards/{card_id}/pause" in paths and "/api/v1/state" in paths


# ------------------------------------------------------------------ WebSocket

def test_websocket_requires_auth_message(client):
    with client.websocket_connect("ws://127.0.0.1/api/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4401


def test_websocket_streams_state_and_events(client, harness):
    with client.websocket_connect("ws://127.0.0.1/api/v1/ws") as ws:
        ws.send_json({"type": "auth", "token": TOKEN})
        first = ws.receive_json()
        assert first["type"] == "state" and first["state"]["session"] is None
        client.post("/api/v1/session", headers=AUTH, json={"metadata": METADATA})
        kinds = []
        deadline = time.monotonic() + 5
        while "session_opened" not in kinds and time.monotonic() < deadline:
            message = ws.receive_json()
            if message["type"] == "event":
                kinds.append(message["event"]["kind"])
        assert "session_opened" in kinds
        while True:  # A periodic state follows within a second.
            message = ws.receive_json()
            if message["type"] == "state":
                assert message["state"]["session"] is not None
                break


# ------------------------------------------------------------------ server and client

def test_real_server_and_cli_client(harness):
    port = free_port()
    services = ApiServices(harness.station, TOKEN)
    server = ApiServer(create_app(services), "127.0.0.1", port)
    assert server.start(), server.error
    try:
        client = StationClient(f"http://127.0.0.1:{port}", TOKEN)
        assert client.state()["session"] is None
        method, path, body = route("open", {"metadata": METADATA})
        status, payload = client.request(method, path, body)
        assert status == 201 and payload["session_id"].startswith("SESSION_")
        status, payload = client.request(*route("pause", {"card_id": "a b"}))
        assert status == 404 and "a b" in payload["detail"]
        bad = StationClient(f"http://127.0.0.1:{port}", "wrong")
        assert bad.request("GET", "/api/v1/state")[0] == 401

        second = ApiServer(create_app(services), "127.0.0.1", port)
        assert not second.start() and "could not listen" in second.error
    finally:
        server.stop()
    with pytest.raises(ApiUnavailable):
        StationClient(f"http://127.0.0.1:{port}", TOKEN, timeout=2).state()


def test_route_covers_every_station_command():
    for command in ("pause_all", "resume_all", "start_all", "resume_engine", "close",
                    "auto_upload", "pause", "resume", "start", "confirm", "retry"):
        method, path, _ = route(command, {"card_id": "sim-A"})
        assert method == "POST" and path.startswith("/api/v1/")
    with pytest.raises(ValueError):
        route("format", {})
