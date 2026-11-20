"""Normalized models shared by both platforms. Unknown values stay None, never guessed."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from src.core.enums import AccessStatus, DeviceStatus, ReaderKind, SpeedConfidence


@dataclass
class UsbDevice:
    """One node of the USB tree, as reported by the operating system."""

    topology_id: str
    parent_topology_id: str | None = None
    device_path: str | None = None
    vendor_id: str | None = None
    product_id: str | None = None
    manufacturer: str | None = None
    product_name: str | None = None
    serial_number: str | None = None
    port_path: str | None = None
    is_hub: bool = False
    is_mass_storage: bool | None = None
    advertised_capability: str | None = None
    negotiated_speed_mbps: float | None = None
    speed_source: str = "UNKNOWN"
    speed_confidence: SpeedConfidence = SpeedConfidence.NONE
    last_seen_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Reader:
    reader_id: str
    platform_device_id: str | None = None
    vendor_id: str | None = None
    product_id: str | None = None
    serial_number: str | None = None
    model: str | None = None
    topology_path: str | None = None
    topology_id: str | None = None
    kind: ReaderKind = ReaderKind.USB_STORAGE_UNCONFIRMED
    status: DeviceStatus = DeviceStatus.UNKNOWN
    last_seen_at: str | None = None


@dataclass
class StorageDevice:
    storage_id: str
    platform_device_id: str | None = None
    reader_id: str | None = None
    usb_topology_id: str | None = None
    model: str | None = None
    serial_number: str | None = None
    capacity_bytes: int | None = None
    removable: bool | None = None
    read_only: bool | None = None
    bus_type: str | None = None
    media_present: bool | None = None  # False only when the OS confirms no medium.
    status: DeviceStatus = DeviceStatus.UNKNOWN
    correlation_evidence: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Volume:
    volume_id: str
    storage_id: str | None = None
    partition_id: str | None = None
    mount_point: str | None = None
    drive_letter: str | None = None
    filesystem: str | None = None
    volume_label: str | None = None
    fs_serial: str | None = None  # FAT/exFAT serial or UUID; survives drive-letter changes.
    capacity_bytes: int | None = None
    free_bytes: int | None = None
    is_mounted: bool = False
    access_status: AccessStatus = AccessStatus.UNKNOWN
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DetectionIssue:
    """A non-fatal problem found while taking inventory."""

    scope: str
    message: str
    severity: str = "WARNING"


@dataclass
class RawInventory:
    """What a platform adapter returns, before correlation decides statuses."""

    platform: str
    usb_devices: list[UsbDevice] = field(default_factory=list)
    storage_devices: list[StorageDevice] = field(default_factory=list)
    volumes: list[Volume] = field(default_factory=list)
    issues: list[DetectionIssue] = field(default_factory=list)


@dataclass
class InventorySnapshot:
    snapshot_id: str
    captured_at: str
    platform: str
    hostname: str | None = None
    usb_devices: list[UsbDevice] = field(default_factory=list)
    readers: list[Reader] = field(default_factory=list)
    storage_devices: list[StorageDevice] = field(default_factory=list)
    volumes: list[Volume] = field(default_factory=list)
    issues: list[DetectionIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Card:
    """A mounted volume that could be uploaded, plus why it should or should not be trusted."""

    card_id: str
    mount_point: str
    fingerprint: str | None  # Matches the same card again after a replug; None if unavailable.
    volume_label: str | None = None
    filesystem: str | None = None
    capacity_bytes: int | None = None
    free_bytes: int | None = None
    reader_model: str | None = None
    negotiated_speed_mbps: float | None = None
    ambiguity: list[str] = field(default_factory=list)  # Non-empty: operator must confirm.

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.ambiguity)


@dataclass
class RemoteObject:
    name: str
    size: int
    crc32c: str | None
    generation: int
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class ScanSummary:
    """Aggregates for one card. Figures are counted from the walk, never estimated."""

    scan_id: str
    card_id: str
    mount_point: str
    file_count: int = 0
    total_bytes: int = 0
    dir_count: int = 0
    unreadable_count: int = 0
    excluded_count: int = 0
    error_count: int = 0
    by_extension: dict[str, int] = field(default_factory=dict)
    status: str = "RUNNING"
