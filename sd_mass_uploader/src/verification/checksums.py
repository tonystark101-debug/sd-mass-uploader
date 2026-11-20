"""CRC32C (what GCS stores) and optional SHA-256, computed while the file is read."""

from __future__ import annotations

import base64
import hashlib
import threading
from collections.abc import Callable
from typing import BinaryIO

import google_crc32c

from src.core.exceptions import SourceDisconnectedError, SourceReadError, StopRequested
from src.platform.paths import open_source_readonly

BLOCK_SIZE = 1024 * 1024


def encode_crc32c(digest: bytes) -> str:
    """GCS reports CRC32C as base64 of the big-endian 4 bytes."""
    return base64.b64encode(digest).decode("ascii")


def crc32c_of_bytes(data: bytes) -> str:
    return encode_crc32c(google_crc32c.Checksum(data).digest())


def crc32c_of_file(path: str) -> str:
    """Read a whole source file. Used when comparing against an existing remote object."""
    checksum = google_crc32c.Checksum()
    with open_source_readonly(path) as handle:
        while block := handle.read(BLOCK_SIZE):
            checksum.update(block)
    return encode_crc32c(checksum.digest())


class HashingReader:
    """Read-only stream handed to the GCS client.

    The client may rewind to resend a chunk, so hashes and progress cover each byte once, at
    its first read. Read failures become SourceReadError, which is deliberately not an OSError:
    otherwise the client would take a dying card for a network fault and retry it.
    """

    def __init__(self, handle: BinaryIO, on_bytes: Callable[[int], None] | None = None,
                 cancel: threading.Event | None = None, sha256: bool = False,
                 mount_check: Callable[[], bool] | None = None) -> None:
        self._handle = handle
        self._on_bytes = on_bytes
        self._cancel = cancel
        self._mount_check = mount_check
        self._crc = google_crc32c.Checksum()
        self._sha = hashlib.sha256() if sha256 else None
        self._position = 0
        self.high_water = 0
        self.bytes_read = 0  # Includes resends, so it measures real transfer work.

    def read(self, size: int = -1) -> bytes:
        if self._cancel is not None and self._cancel.is_set():
            raise StopRequested("Upload interrupted by hard stop")
        try:
            data = self._handle.read(size)
        except OSError as exc:
            if self._mount_check is not None and not self._mount_check():
                raise SourceDisconnectedError(f"Source card disappeared: {exc}") from exc
            raise SourceReadError(f"Source read failed: {exc}") from exc
        start, end = self._position, self._position + len(data)
        if end > self.high_water:
            fresh = data[self.high_water - start:]
            self._crc.update(fresh)
            if self._sha is not None:
                self._sha.update(fresh)
            self.high_water = end
            if self._on_bytes is not None:
                self._on_bytes(len(fresh))
        self._position = end
        self.bytes_read += len(data)
        return data

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = 0) -> int:
        self._position = self._handle.seek(offset, whence)
        return self._position

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    @property
    def crc32c(self) -> str:
        return encode_crc32c(self._crc.digest())

    @property
    def sha256(self) -> str | None:
        return self._sha.hexdigest() if self._sha is not None else None
