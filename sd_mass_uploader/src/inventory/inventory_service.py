"""Use cases: refresh inventory, find uploadable cards, record file scans."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from src.core.enums import AccessStatus, DeviceStatus, ReaderKind, ScanStatus
from src.core.exceptions import SourceDisconnectedError
from src.core.models import Card, InventorySnapshot, ScanSummary, StorageDevice
from src.core.settings import FileScanningSettings
from src.database.repositories import InventoryRepository, ScanRepository
from src.device_manager.base import PlatformAdapter, stable_id
from src.device_manager.correlation import mark_disconnected
from src.device_manager.detector import detect
from src.inventory.file_scanner import (
    ExcludedEntry,
    FileScanner,
    ScannedDirectory,
    ScannedFile,
    ScanProblem,
)

EXTERNAL_BUSES = {"USB", "SD", "MMC"}
_BATCH = 500
_UNSAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
# Reported, but they are decisions rather than faults.
_INFORMATIONAL_KINDS = {"LINK_SKIPPED"}


def refresh_inventory(adapter: PlatformAdapter, repository: InventoryRepository
                      ) -> InventorySnapshot:
    snapshot = detect(adapter)
    mark_disconnected(snapshot, repository.latest())
    repository.save(snapshot)
    return snapshot


def _card_token(text: str) -> str:
    return _UNSAFE_ID.sub("-", text).strip("-._") or "unnamed"


def _is_external(device: StorageDevice) -> bool:
    return (device.bus_type or "").upper() in EXTERNAL_BUSES


def find_cards(snapshot: InventorySnapshot) -> list[Card]:
    """Accessible volumes on external buses. Internal disks can never be selected."""
    storage = {device.storage_id: device for device in snapshot.storage_devices}
    readers = {reader.reader_id: reader for reader in snapshot.readers}
    usb = {device.topology_id: device for device in snapshot.usb_devices}
    cards: list[Card] = []
    used: dict[str, int] = {}
    for volume in snapshot.volumes:
        if not volume.is_mounted or volume.access_status != AccessStatus.OK:
            continue
        device = storage.get(volume.storage_id or "")
        if device is None:
            continue  # Without proven ownership it could be an internal disk.
        if not _is_external(device) or device.status != DeviceStatus.ACTIVE:
            continue
        reader = readers.get(device.reader_id or "")
        topology = usb.get(reader.topology_id or "") if reader else None
        ambiguity = []
        if reader is None:
            ambiguity.append("not linked to a USB reader")
        elif reader.kind != ReaderKind.CARD_READER:
            ambiguity.append("may be a USB drive rather than a card reader")
        fingerprint = volume.fs_serial or device.serial_number
        if not fingerprint:
            ambiguity.append("no volume serial, so the card cannot be re-identified")
        token = _card_token(volume.fs_serial or device.serial_number or volume.volume_id)
        card_id = f"card-{token}"
        used[card_id] = used.get(card_id, 0) + 1
        if used[card_id] > 1:
            ambiguity.append(f"same serial as another card ({token}); likely a cloned card")
            card_id = f"{card_id}-{used[card_id]}"
        cards.append(
            Card(
                card_id=card_id,
                mount_point=volume.mount_point or "",
                fingerprint=fingerprint,
                volume_label=volume.volume_label,
                filesystem=volume.filesystem,
                capacity_bytes=volume.capacity_bytes,
                free_bytes=volume.free_bytes,
                reader_model=reader.model if reader else device.model,
                negotiated_speed_mbps=topology.negotiated_speed_mbps if topology else None,
                ambiguity=ambiguity,
            )
        )
    return cards


@dataclass
class _Buffers:
    files: list[tuple] = field(default_factory=list)
    errors: list[tuple[str, str, str]] = field(default_factory=list)


class ScanService:
    def __init__(self, repository: ScanRepository, settings: FileScanningSettings) -> None:
        self._repository = repository
        self._settings = settings

    def scan_card(self, card_id: str, mount_point: str, session_id: str | None = None,
                  hash_files: bool = False) -> ScanSummary:
        """Walk one card, writing rows in batches. One failing card never affects another."""
        summary = ScanSummary(f"scan-{uuid.uuid4().hex[:16]}", card_id, mount_point)
        self._repository.start(summary, session_id)
        buffers = _Buffers()
        scanner = FileScanner(self._settings, hash_files)
        try:
            for event in scanner.walk(mount_point):
                self._apply(event, summary, buffers)
                if len(buffers.files) + len(buffers.errors) >= _BATCH:
                    self._flush(summary.scan_id, buffers)
            summary.status = (ScanStatus.COMPLETE_WITH_ERRORS if summary.error_count
                              else ScanStatus.COMPLETE).value
        except SourceDisconnectedError as exc:
            buffers.errors.append((mount_point, "CARD_DISCONNECTED", str(exc)))
            summary.error_count += 1
            summary.status = ScanStatus.DISCONNECTED.value
        self._flush(summary.scan_id, buffers)
        self._repository.finish(summary)
        return summary

    def _flush(self, scan_id: str, buffers: _Buffers) -> None:
        if buffers.files:
            self._repository.add_files(scan_id, buffers.files)
            buffers.files = []
        if buffers.errors:
            self._repository.add_errors(scan_id, buffers.errors)
            buffers.errors = []

    @staticmethod
    def _apply(event: object, summary: ScanSummary, buffers: _Buffers) -> None:
        if isinstance(event, ScannedFile):
            summary.file_count += 1
            summary.total_bytes += event.size_bytes
            key = event.extension or "(none)"
            summary.by_extension[key] = summary.by_extension.get(key, 0) + 1
            buffers.files.append((event.relative_path, event.file_name, event.extension,
                                  event.size_bytes, event.mtime_ns, 1, event.sha256))
        elif isinstance(event, ScannedDirectory):
            summary.dir_count += 1
        elif isinstance(event, ExcludedEntry):
            summary.excluded_count += 1
        elif isinstance(event, ScanProblem):
            buffers.errors.append((event.path, event.kind, event.message))
            summary.error_count += 1
            if event.kind not in _INFORMATIONAL_KINDS:
                summary.unreadable_count += 1


def folder_card(path: str, card_id: str | None = None) -> Card:
    """Treat a folder as a card, for test files or when a card has to be chosen by hand."""
    import os

    full = os.path.abspath(path)
    token = _card_token(os.path.basename(full.rstrip("\\/")))
    return Card(
        card_id=card_id or f"folder-{token}-{stable_id('x', full)[2:8]}",
        mount_point=full,
        fingerprint=f"folder:{full}",
        volume_label=None,
        reader_model="(folder)",
    )
