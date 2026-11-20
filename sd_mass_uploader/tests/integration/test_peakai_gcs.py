"""The real Google client, with PeakAI pass tokens, against the local storage protocol server.

Proves each object goes up with the token of its own pass, and that a token storage refuses
(401) is replaced. It does not prove how PeakAI's real tokens or bucket behave.
"""

from __future__ import annotations

import io

import pytest

from src.core.settings import PeakAiSettings, UploadSettings
from src.peakai.keeper import PassKeeper
from src.peakai.pack import read_pack
from src.peakai.store import PeakAiStore
from src.verification.checksums import HashingReader
from tests.integration.fake_gcs_server import FakeGcsServer
from tests.unit.peakai_fakes import KEY, FakeTokenService, make_pack


@pytest.fixture
def server(monkeypatch):
    with FakeGcsServer() as fake:
        monkeypatch.setenv("STORAGE_EMULATOR_HOST", fake.url)
        yield fake


@pytest.fixture
def store(tmp_path):
    pack = read_pack(make_pack(tmp_path / "pack.zip")).settings
    service = FakeTokenService()
    keeper = PassKeeper(pack, PeakAiSettings(), lambda: KEY, http=service)
    return PeakAiStore(pack, keeper, UploadSettings(chunk_size_mib=0.25,
                                                    request_timeout_seconds=10)), keeper


def put(store, name, data):
    reader = HashingReader(io.BytesIO(data))
    return store.upload_stream(name, reader, len(data), None, {}, 0)


def test_each_pass_uses_its_own_token(server, store):
    peak, _ = store
    put(peak, "gilabs/garage/bijapur/2026-11-12/card-6AF7F78B/data/a.mcap", b"g" * 100)
    put(peak, "sieve/mono/garage/bijapur/2026-11-12/card-6AF7F78B/DVR/VIDEO/a.mp4", b"s" * 100)
    tokens = [header for header in server.state.auth_headers if header]
    assert "Bearer tok-gilabs-1" in tokens and "Bearer tok-sieve-2" in tokens
    remote = peak.get_object("gilabs/garage/bijapur/2026-11-12/card-6AF7F78B/data/a.mcap")
    assert remote.size == 100
    peak.upload_bytes("gilabs/garage/bijapur/2026-11-12/card-6AF7F78B/_info/m_1.json", b"{}",
                      "application/json", 0)


def test_large_file_resumable_upload_keeps_its_pass_token(server, store):
    peak, _ = store
    data = b"x" * (9 * 1024 * 1024)
    remote = put(peak, "buildai/stereo/garage/bijapur/2026-11-12/card-1/rec.enc", data)
    assert remote.size == len(data) and server.state.chunk_requests > 1
    used = {header for header in server.state.auth_headers if header}
    assert used == {"Bearer tok-buildai-1"}


def test_extra_refreshes_never_waste_a_token(server, store):
    """The auth library may refresh whenever it likes; only a real 401 replaces a token."""
    peak, keeper = store
    put(peak, "sieve/mono/a/b/2026-11-12/card-1/x.mp4", b"1")
    credentials = peak._credentials["sieve"]
    for _ in range(5):
        credentials.refresh(None)
    put(peak, "sieve/mono/a/b/2026-11-12/card-1/y.mp4", b"2")
    assert {h for h in server.state.auth_headers if h} == {"Bearer tok-sieve-1"}


def test_rejected_token_is_replaced_before_the_next_use(server, store):
    peak, keeper = store
    put(peak, "gilabs/a/b/2026-11-12/card-1/x.bin", b"1")
    keeper.reject("gilabs", "tok-gilabs-1")
    peak._stores.clear()  # A new client, as after a 401 the next attempt would see.
    put(peak, "gilabs/a/b/2026-11-12/card-1/y.bin", b"2")
    assert server.state.auth_headers[-1] == "Bearer tok-gilabs-2"
