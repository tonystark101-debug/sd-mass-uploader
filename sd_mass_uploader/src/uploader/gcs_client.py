"""The only module that touches the Google client. Everything above uses ObjectStore."""

from __future__ import annotations

import logging
from typing import Any, BinaryIO, Protocol

from src.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    GcsPermissionError,
    ObjectExistsError,
    PermanentUploadError,
    TransientUploadError,
    UploaderError,
    VerificationMismatch,
)
from src.core.models import RemoteObject
from src.core.settings import GoogleCloudSettings, UploadSettings

logger = logging.getLogger(__name__)

REQUIRED_PERMISSIONS = ["storage.objects.create", "storage.objects.get"]
_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
_CONNECT_TIMEOUT = 30


class ObjectStore(Protocol):
    def validate_access(self) -> None: ...

    def get_object(self, name: str) -> RemoteObject | None: ...

    def upload_stream(self, name: str, stream: BinaryIO, size: int, content_type: str | None,
                      metadata: dict[str, str], if_generation_match: int) -> RemoteObject: ...

    def upload_bytes(self, name: str, data: bytes, content_type: str,
                     if_generation_match: int) -> RemoteObject: ...


def translate_error(exc: Exception, context: str) -> UploaderError:
    """Map a client exception to a typed error. Decides what is safe to retry."""
    from google.api_core import exceptions as api
    from google.auth import exceptions as auth
    from google.cloud.storage.exceptions import DataCorruption, InvalidResponse

    message = f"{context}: {exc}"
    if isinstance(exc, DataCorruption):
        return VerificationMismatch(message)
    if isinstance(exc, api.PreconditionFailed):
        return ObjectExistsError(message)
    if isinstance(exc, api.Unauthorized):
        return AuthenticationError(message)
    if isinstance(exc, auth.TransportError):
        return TransientUploadError(message)
    if isinstance(exc, auth.GoogleAuthError):
        return AuthenticationError(f"{context}: Google credentials rejected or missing")
    if isinstance(exc, api.Forbidden):
        return GcsPermissionError(message)
    if isinstance(exc, api.NotFound):
        return ConfigurationError(message)
    if isinstance(exc, api.GoogleAPICallError):
        code = getattr(exc, "code", None)
        if isinstance(code, int) and code in _TRANSIENT_STATUS:
            return TransientUploadError(message)
        return PermanentUploadError(message)
    if isinstance(exc, InvalidResponse):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 412:
            return ObjectExistsError(message)
        if status == 401:
            return AuthenticationError(message)
        if status == 403:
            return GcsPermissionError(message)
        if status in _TRANSIENT_STATUS:
            return TransientUploadError(message)
        return PermanentUploadError(message)
    return TransientUploadError(message)  # Connection resets and timeouts land here.


def _translatable() -> tuple[type[BaseException], ...]:
    import requests
    from google.api_core.exceptions import GoogleAPIError
    from google.auth.exceptions import GoogleAuthError
    from google.cloud.storage.exceptions import DataCorruption, InvalidResponse

    return (GoogleAPIError, GoogleAuthError, DataCorruption, InvalidResponse,
            requests.RequestException, ConnectionError, TimeoutError)


class GcsObjectStore:
    def __init__(self, cloud: GoogleCloudSettings, upload: UploadSettings,
                 client: Any = None) -> None:
        self._cloud = cloud
        self._upload = upload
        self._client = client
        self._bucket: Any = None
        self._errors = _translatable()

    @property
    def _timeout(self) -> tuple[float, float]:
        return (_CONNECT_TIMEOUT, self._upload.request_timeout_seconds)

    def _get_bucket(self) -> Any:
        if self._bucket is None:
            from google.cloud import storage

            try:
                self._client = self._client or storage.Client(project=self._cloud.project_id)
            except self._errors as exc:
                raise translate_error(exc, "Creating Google client") from exc
            self._bucket = self._client.bucket(self._cloud.bucket_name)
        return self._bucket

    def validate_access(self) -> None:
        """Confirm the bucket exists and this identity can write and read objects."""
        bucket = self._get_bucket()
        try:
            granted = bucket.test_iam_permissions(REQUIRED_PERMISSIONS)
        except self._errors as exc:
            raise translate_error(exc, f"Checking bucket {self._cloud.bucket_name}") from exc
        missing = sorted(set(REQUIRED_PERMISSIONS) - set(granted))
        if missing:
            raise GcsPermissionError(
                f"Missing on bucket {self._cloud.bucket_name}: {', '.join(missing)}"
            )

    @staticmethod
    def _to_remote(blob: Any) -> RemoteObject:
        return RemoteObject(
            name=blob.name,
            size=int(blob.size),
            crc32c=blob.crc32c,
            generation=int(blob.generation),
            metadata=dict(blob.metadata or {}),
        )

    def get_object(self, name: str) -> RemoteObject | None:
        try:
            blob = self._get_bucket().get_blob(name, timeout=self._timeout)
        except self._errors as exc:
            raise translate_error(exc, f"Reading metadata of {name}") from exc
        return self._to_remote(blob) if blob is not None else None

    def upload_stream(self, name: str, stream: BinaryIO, size: int, content_type: str | None,
                      metadata: dict[str, str], if_generation_match: int) -> RemoteObject:
        """Upload exactly `size` bytes. A generation of 0 means "only if it does not exist".

        Above 8 MiB the client uses a resumable upload in chunks of chunk_size_bytes and
        retries each chunk from the last offset GCS confirmed.
        """
        blob = self._get_bucket().blob(name, chunk_size=self._upload.chunk_size_bytes)
        blob.metadata = metadata or None
        try:
            blob.upload_from_file(
                stream,
                rewind=False,
                size=size,
                content_type=content_type,
                if_generation_match=if_generation_match,
                checksum="crc32c",
                timeout=self._timeout,
            )
        except self._errors as exc:
            raise translate_error(exc, f"Uploading {name}") from exc
        return self._to_remote(blob)

    def upload_bytes(self, name: str, data: bytes, content_type: str,
                     if_generation_match: int) -> RemoteObject:
        blob = self._get_bucket().blob(name)
        try:
            blob.upload_from_string(
                data,
                content_type=content_type,
                if_generation_match=if_generation_match,
                checksum="crc32c",
                timeout=self._timeout,
            )
        except self._errors as exc:
            raise translate_error(exc, f"Uploading {name}") from exc
        return self._to_remote(blob)
