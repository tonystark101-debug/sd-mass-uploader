"""Runs the real google-cloud-storage client against the local protocol server."""

from __future__ import annotations

import io
import os

import pytest

from src.core.exceptions import (
    GcsPermissionError,
    ObjectExistsError,
    TransientUploadError,
)
from src.core.settings import GoogleCloudSettings, UploadSettings
from src.uploader.gcs_client import GcsObjectStore
from src.verification.checksums import HashingReader, crc32c_of_bytes
from tests.integration.fake_gcs_server import FakeGcsServer


@pytest.fixture
def server(monkeypatch):
    with FakeGcsServer() as fake:
        monkeypatch.setenv("STORAGE_EMULATOR_HOST", fake.url)
        yield fake


def make_store(chunk_mib: float = 0.25) -> GcsObjectStore:
    return GcsObjectStore(GoogleCloudSettings("test-project", "test-bucket", "uploads"),
                          UploadSettings(chunk_size_mib=chunk_mib, request_timeout_seconds=10))


def upload(store, name, data, generation=0):
    reader = HashingReader(io.BytesIO(data))
    remote = store.upload_stream(name, reader, len(data), "video/mp4", {"a": "b"}, generation)
    return remote, reader


def test_small_file_multipart_with_real_client(server):
    data = b"hello world" * 100
    remote, reader = upload(make_store(), "x/small.mp4", data)
    assert remote.size == len(data)
    assert remote.crc32c == crc32c_of_bytes(data) == reader.crc32c
    assert server.state.chunk_requests == 0
    assert server.state.objects["x/small.mp4"]["record"]["metadata"] == {"a": "b"}


def test_unicode_object_name_round_trips(server):
    store = make_store()
    upload(store, "x/वीडियो फ़ाइल.mp4", b"abc")
    assert store.get_object("x/वीडियो फ़ाइल.mp4").size == 3


def test_empty_file(server):
    remote, _ = upload(make_store(), "x/empty.bin", b"")
    assert remote.size == 0


def test_large_file_uses_resumable_chunks(server):
    data = os.urandom(9 * 1024 * 1024)
    remote, reader = upload(make_store(chunk_mib=1), "x/large.bin", data)
    assert server.state.chunk_requests >= 9
    assert remote.crc32c == crc32c_of_bytes(data) == reader.crc32c
    assert server.state.objects["x/large.bin"]["data"] == data


def test_resumable_chunk_failure_recovers_without_double_hashing(server):
    data = os.urandom(10 * 1024 * 1024)
    store = make_store(chunk_mib=2)
    server.state.fail_on_chunk = {3}  # Fails mid-file, after two chunks were accepted.
    reader = HashingReader(io.BytesIO(data))
    remote = store.upload_stream("x/flaky.bin", reader, len(data), None, {}, 0)
    assert server.state.chunk_requests == 6  # Five chunks plus the one resend.
    assert server.state.objects["x/flaky.bin"]["data"] == data
    assert reader.high_water == len(data)  # Each byte is hashed and counted once.
    assert remote.crc32c == reader.crc32c == crc32c_of_bytes(data)


def test_precondition_blocks_overwrite_multipart(server):
    store = make_store()
    upload(store, "x/a.bin", b"first")
    with pytest.raises(ObjectExistsError):
        upload(store, "x/a.bin", b"second")
    assert server.state.objects["x/a.bin"]["data"] == b"first"


def test_precondition_blocks_overwrite_resumable(server):
    store = make_store(chunk_mib=1)
    upload(store, "x/b.bin", b"first")
    with pytest.raises(ObjectExistsError):
        upload(store, "x/b.bin", os.urandom(9 * 1024 * 1024))
    assert server.state.objects["x/b.bin"]["data"] == b"first"


def test_generation_match_allows_explicit_overwrite(server):
    store = make_store()
    first, _ = upload(store, "x/c.bin", b"first")
    upload(store, "x/c.bin", b"second", generation=first.generation)
    assert server.state.objects["x/c.bin"]["data"] == b"second"


def test_missing_object_is_none(server):
    assert make_store().get_object("nope") is None


def test_access_check_passes_and_fails(server):
    make_store().validate_access()
    server.state.denied_permissions = {"storage.objects.create"}
    with pytest.raises(GcsPermissionError, match="storage.objects.create"):
        make_store().validate_access()


def test_persistent_503_becomes_transient_error(server, monkeypatch):
    # Switch off the client's own retry, so the error reaches our classifier.
    from google.cloud import storage

    store = make_store()
    original = storage.Blob.upload_from_file

    def no_retry(self, *args, **kwargs):
        kwargs["retry"] = None
        return original(self, *args, **kwargs)

    monkeypatch.setattr(storage.Blob, "upload_from_file", no_retry)
    server.state.fail_next = [503]
    with pytest.raises(TransientUploadError):
        upload(store, "x/down.bin", b"data")


def test_upload_bytes(server):
    remote = make_store().upload_bytes("x/m.json", b"{}", "application/json", 0)
    assert remote.size == 2
