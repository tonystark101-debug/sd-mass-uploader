"""Storage for PeakAI: each object goes up with the token of the pass whose folder holds it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, BinaryIO

import google.auth.credentials

from src.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    GcsPermissionError,
    PassDeniedError,
    PeakAiKeyError,
    TransientUploadError,
)
from src.core.models import RemoteObject
from src.core.settings import GoogleCloudSettings, UploadSettings
from src.peakai.keeper import PassKeeper
from src.peakai.pack import PackSettings
from src.uploader.gcs_client import GcsObjectStore

_PROJECT = "peakai-uploader"  # Only object calls are made, so no project is needed.


class PassCredentials(google.auth.credentials.Credentials):
    """Google credentials backed by the pass keeper.

    `refresh` only asks the keeper for its current token, so the auth library may call it as
    often as it likes. A token storage refused (401) is reported to the keeper by PeakAiStore,
    which sees the actual answer; the next attempt then gets a new token.
    """

    def __init__(self, keeper: PassKeeper, pass_name: str) -> None:
        super().__init__()
        self._keeper = keeper
        self._pass = pass_name

    def refresh(self, request: Any) -> None:
        token, expires = self._keeper.token(self._pass)
        self.token = token
        self.expiry = datetime.fromtimestamp(expires, UTC).replace(tzinfo=None)


def default_client(credentials: PassCredentials) -> Any:
    from google.cloud import storage

    return storage.Client(project=_PROJECT, credentials=credentials)


class PeakAiStore:
    """The ObjectStore for a PeakAI pack. Objects outside every pass folder are refused."""

    def __init__(self, pack: PackSettings, keeper: PassKeeper, upload: UploadSettings,
                 client_factory: Callable[[PassCredentials], Any] = default_client) -> None:
        self._pack = pack
        self._keeper = keeper
        self._upload = upload
        self._client_factory = client_factory
        self._stores: dict[str, GcsObjectStore] = {}
        self._credentials: dict[str, PassCredentials] = {}

    def pass_for(self, name: str) -> str:
        matches = [rule for rule in self._pack.passes.values() if name.startswith(rule.folder)]
        if not matches:
            raise ConfigurationError(f"{name} is not inside any PeakAI pass folder")
        return max(matches, key=lambda rule: len(rule.folder)).name

    def _store(self, pass_name: str) -> GcsObjectStore:
        store = self._stores.get(pass_name)
        if store is None:
            credentials = PassCredentials(self._keeper, pass_name)
            self._credentials[pass_name] = credentials
            client = self._client_factory(credentials)
            store = GcsObjectStore(GoogleCloudSettings(_PROJECT, self._pack.bucket),
                                   self._upload, client=client)
            self._stores[pass_name] = store
        return store

    def _call(self, name: str, method: str, *args: Any) -> Any:
        pass_name = self.pass_for(name)
        try:
            return getattr(self._store(pass_name), method)(name, *args)
        except GcsPermissionError as exc:
            raise PassDeniedError(
                f"Storage refused the {pass_name} pass for {name} (HTTP 403): wrong pass for "
                f"this card, or a pass this laptop does not have. {exc}") from exc
        except PeakAiKeyError:
            raise
        except AuthenticationError as exc:
            # Storage refused this pass's token (401): never use it again; retry with a new one.
            credentials = self._credentials.get(pass_name)
            if credentials is not None and credentials.token:
                self._keeper.reject(pass_name, credentials.token)
                credentials.token = None
            raise TransientUploadError(f"Upload token for {pass_name} was refused (401); a new "
                                       f"one will be fetched. {exc}") from exc

    def validate_access(self) -> None:
        self._keeper.ensure_ready()

    def get_object(self, name: str) -> RemoteObject | None:
        return self._call(name, "get_object")

    def upload_stream(self, name: str, stream: BinaryIO, size: int, content_type: str | None,
                      metadata: dict[str, str], if_generation_match: int) -> RemoteObject:
        return self._call(name, "upload_stream", stream, size, content_type, metadata,
                          if_generation_match)

    def upload_bytes(self, name: str, data: bytes, content_type: str,
                     if_generation_match: int) -> RemoteObject:
        return self._call(name, "upload_bytes", data, content_type, if_generation_match)
