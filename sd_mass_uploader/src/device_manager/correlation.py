"""Turn raw records into readers and statuses. Conservative: no evidence means no link."""

from __future__ import annotations

import re
import socket
import uuid
from collections import Counter

from src.core.enums import AccessStatus, DeviceStatus, ReaderKind
from src.core.models import (
    DetectionIssue,
    InventorySnapshot,
    RawInventory,
    Reader,
    StorageDevice,
    UsbDevice,
    Volume,
)
from src.device_manager.base import stable_id

_CARD_READER_NAME = re.compile(
    r"card[ _-]?reader|cardreader|sd/?mmc|micro[ _-]?sd|multi[ _-]?card|\bsd[xh]c\b|\bcard\b",
    re.IGNORECASE,
)

# When several LUNs share a reader, the reader shows the most useful one.
_READER_PRIORITY = [
    DeviceStatus.ACTIVE,
    DeviceStatus.UNMOUNTED_STORAGE,
    DeviceStatus.INACCESSIBLE,
    DeviceStatus.ERROR,
    DeviceStatus.UNKNOWN,
]


def storage_status(device: StorageDevice, volumes: list[Volume]) -> DeviceStatus:
    if device.media_present is False:
        return DeviceStatus.EMPTY_READER
    mounted = [volume for volume in volumes if volume.is_mounted]
    access = {volume.access_status for volume in mounted}
    if AccessStatus.OK in access:
        return DeviceStatus.ACTIVE
    if AccessStatus.DENIED in access:
        return DeviceStatus.INACCESSIBLE
    if AccessStatus.ERROR in access:
        return DeviceStatus.ERROR
    if mounted:
        return DeviceStatus.UNKNOWN
    if volumes or device.media_present or device.capacity_bytes:
        return DeviceStatus.UNMOUNTED_STORAGE
    return DeviceStatus.UNKNOWN


def reader_status(statuses: list[DeviceStatus]) -> DeviceStatus:
    if not statuses:
        return DeviceStatus.READER_PRESENT_SLOT_UNKNOWN
    if all(status is DeviceStatus.EMPTY_READER for status in statuses):
        return DeviceStatus.EMPTY_READER
    for status in _READER_PRIORITY:
        if status in statuses:
            return status
    return DeviceStatus.UNKNOWN


def _reader_kind(usb: UsbDevice, storage: list[StorageDevice]) -> ReaderKind:
    names = [usb.product_name, usb.manufacturer] + [item.model for item in storage]
    if any(name and _CARD_READER_NAME.search(name) for name in names):
        return ReaderKind.CARD_READER
    return ReaderKind.USB_STORAGE_UNCONFIRMED


def _serial_issues(usb_devices: list[UsbDevice]) -> list[DetectionIssue]:
    counts = Counter(
        (d.vendor_id, d.product_id, d.serial_number) for d in usb_devices if d.serial_number
    )
    return [
        DetectionIssue(
            "correlation",
            f"{count} USB devices share serial {key[2]} ({key[0]}:{key[1]}); "
            "they are told apart by port path",
        )
        for key, count in counts.items()
        if count > 1
    ]


def correlate(raw: RawInventory, now: str) -> InventorySnapshot:
    """Apply the status rules and create reader records. Never counts hubs as readers."""
    issues = list(raw.issues) + _serial_issues(raw.usb_devices)
    by_topology = {device.topology_id: device for device in raw.usb_devices}

    volumes_by_storage: dict[str, list[Volume]] = {}
    for volume in raw.volumes:
        if volume.storage_id:
            volumes_by_storage.setdefault(volume.storage_id, []).append(volume)
        elif volume.is_mounted:
            issues.append(DetectionIssue(
                "correlation", f"Volume {volume.mount_point} has no proven storage device"))

    storage_by_usb: dict[str, list[StorageDevice]] = {}
    for device in raw.storage_devices:
        device.status = storage_status(device, volumes_by_storage.get(device.storage_id, []))
        if device.usb_topology_id in by_topology:
            storage_by_usb.setdefault(device.usb_topology_id, []).append(device)
        elif device.usb_topology_id:
            device.usb_topology_id = None
            device.correlation_evidence = None

    readers: list[Reader] = []
    for usb in raw.usb_devices:
        attached = storage_by_usb.get(usb.topology_id, [])
        if usb.is_hub or not (usb.is_mass_storage or attached):
            continue
        reader = Reader(
            reader_id=stable_id("rdr", usb.vendor_id, usb.product_id, usb.port_path),
            platform_device_id=usb.device_path,
            vendor_id=usb.vendor_id,
            product_id=usb.product_id,
            serial_number=usb.serial_number,
            model=usb.product_name or next((d.model for d in attached if d.model), None),
            topology_path=usb.port_path,
            topology_id=usb.topology_id,
            kind=_reader_kind(usb, attached),
            status=reader_status([device.status for device in attached]),
            last_seen_at=now,
        )
        for device in attached:
            device.reader_id = reader.reader_id
        readers.append(reader)

    return InventorySnapshot(
        snapshot_id=f"snap-{uuid.uuid4().hex[:16]}",
        captured_at=now,
        platform=raw.platform,
        hostname=socket.gethostname(),
        usb_devices=raw.usb_devices,
        readers=readers,
        storage_devices=raw.storage_devices,
        volumes=raw.volumes,
        issues=issues,
    )


def mark_disconnected(snapshot: InventorySnapshot, previous: InventorySnapshot | None) -> None:
    """Keep devices seen last time but missing now, explicitly marked DISCONNECTED."""
    if previous is None:
        return
    readers = {reader.reader_id for reader in snapshot.readers}
    storage = {device.storage_id for device in snapshot.storage_devices}
    for reader in previous.readers:
        if reader.reader_id not in readers and reader.status != DeviceStatus.DISCONNECTED:
            reader.status = DeviceStatus.DISCONNECTED
            snapshot.readers.append(reader)
    for device in previous.storage_devices:
        if device.storage_id not in storage and device.status != DeviceStatus.DISCONNECTED:
            device.status = DeviceStatus.DISCONNECTED
            snapshot.storage_devices.append(device)
