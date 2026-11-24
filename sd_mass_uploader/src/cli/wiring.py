"""Builds the object graph once, so commands stay short."""

from __future__ import annotations

import os
from functools import cached_property

from src.core.exceptions import ConfigurationError, DatabaseError
from src.core.models import RemoteObject
from src.core.settings import Settings, load_settings
from src.database.db import Database
from src.database.repositories import InventoryRepository, ScanRepository, SessionRepository
from src.device_manager.base import PlatformAdapter
from src.device_manager.detector import create_adapter
from src.inventory.inventory_service import ScanService
from src.network import discovery
from src.network.lines import LinePool
from src.reporting.report_generator import ReportGenerator
from src.uploader.finalize import SessionFinalizer
from src.uploader.gcs_client import GcsObjectStore, ObjectStore


class App:
    """The object graph. The database opens on first use, so commands that only talk to the
    running station (console, desktop, station, problems) work for operators who may not
    read the station's database."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @classmethod
    def open(cls, config_path: str) -> App:
        return cls(load_settings(config_path))

    @cached_property
    def db(self) -> Database:
        try:
            return Database(self.settings.resolve(self.settings.database.path))
        except DatabaseError as exc:
            raise DatabaseError(f"{exc}. On an installed station, run this as the station "
                                "user: sudo -u sdu sdu <command>") from exc

    @cached_property
    def inventory(self) -> InventoryRepository:
        return InventoryRepository(self.db)

    @cached_property
    def scans(self) -> ScanRepository:
        return ScanRepository(self.db)

    @cached_property
    def sessions(self) -> SessionRepository:
        return SessionRepository(self.db)

    def adapter(self) -> PlatformAdapter:
        return create_adapter(self.settings.device_detection)

    def ready_inventory(self, say=print):
        """Every plugged-in card, ready: readers settled, new cards opened read-only (Linux),
        saved as the latest inventory. Returns (snapshot, cards that could not be opened)."""
        from src.device_manager.correlation import mark_disconnected
        from src.inventory.readiness import ready_snapshot

        snapshot, problems = ready_snapshot(self.adapter(), self.settings.station, say=say)
        mark_disconnected(snapshot, self.inventory.latest())
        self.inventory.save(snapshot)
        return snapshot, problems

    def scan_service(self) -> ScanService:
        return ScanService(self.scans, self.settings.file_scanning)

    def lines(self, on_event=None) -> LinePool | None:
        """The internet-line pool, or None when uploads use the default connection only.

        Off when network.enabled is false, on systems without line support, and against a
        local storage emulator (STORAGE_EMULATOR_HOST), which no internet line can reach.
        """
        network = self.settings.network
        emulator = bool(os.environ.get("STORAGE_EMULATOR_HOST"))
        if not network.enabled or not discovery.supported() or emulator:
            return None
        return LinePool(network, fallback_workers=self.settings.upload.max_workers,
                        on_event=on_event)

    def store(self) -> ObjectStore:
        self.settings.require_gcs()
        return GcsObjectStore(self.settings.google_cloud, self.settings.upload)

    def reports(self) -> ReportGenerator:
        reporting = self.settings.reporting
        directory = self.settings.resolve(reporting.directory)
        return ReportGenerator(self.sessions, self.scans, directory, reporting.json, reporting.csv)

    def finalizer(self, store: ObjectStore) -> SessionFinalizer:
        return SessionFinalizer(self.sessions, self.scans, store, self.reports())

    def finalizer_or_offline(self, store: ObjectStore | None) -> SessionFinalizer:
        """Reports are still written when GCS cannot be reached; manifests are reported missing."""
        return self.finalizer(store if store is not None else OfflineStore())


class OfflineStore:
    """Stands in for GCS when it is unreachable. Every call fails with a clear reason."""

    def _fail(self, *_: object, **__: object) -> RemoteObject:
        raise ConfigurationError("Google Cloud Storage is not available")

    validate_access = get_object = upload_stream = upload_bytes = _fail
