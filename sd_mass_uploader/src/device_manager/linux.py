"""Ubuntu adapter. Reads lsblk, sysfs and lsusb; never mounts, repairs or partitions."""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable
from typing import Any

from src.core.enums import AccessStatus, SpeedConfidence
from src.core.exceptions import DeviceDetectionError
from src.core.models import DetectionIssue, RawInventory, StorageDevice, UsbDevice, Volume
from src.core.timeutil import utc_now
from src.device_manager.base import stable_id
from src.device_manager.usb_topology import (
    SysfsUsbDevice,
    parse_lsusb_tree,
    read_sysfs_usb,
    sysfs_parent_name,
    usb_device_from_block_path,
)
from src.platform.process import CommandRunner, SubprocessRunner

logger = logging.getLogger(__name__)

_LSBLK_COLUMNS = "NAME,PATH,TYPE,TRAN,SIZE,RM,RO,FSTYPE,LABEL,UUID,{mount},MODEL,SERIAL"
_SKIPPED_TYPES = {"loop", "rom", "ram", "zram"}
SPEED_SOURCE_SYSFS = "linux:sysfs:speed"
SPEED_SOURCE_LSUSB = "linux:lsusb-t"


def _flag(value: Any) -> bool | None:
    """lsblk versions print booleans as true, 1 or "1"."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true"}


def _size(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mounts(node: dict) -> list[str]:
    mounts = node.get("mountpoints")
    if mounts is None:
        mounts = [node.get("mountpoint")]
    return [m for m in mounts if m]


def usb_from_sysfs(devices: list[SysfsUsbDevice], now: str) -> list[UsbDevice]:
    names = {device.name for device in devices}
    result = []
    for device in devices:
        attrs = device.attrs
        speed = None
        if attrs.get("speed", "").replace(".", "", 1).isdigit():
            speed = float(attrs["speed"])
        parent = sysfs_parent_name(device.name)
        is_hub = attrs.get("bDeviceClass", "").lower() == "09" or "09" in device.interface_classes
        version = attrs.get("version")
        result.append(
            UsbDevice(
                topology_id=device.name,
                parent_topology_id=parent if parent in names else None,
                device_path=f"/sys/bus/usb/devices/{device.name}",
                vendor_id=attrs.get("idVendor"),
                product_id=attrs.get("idProduct"),
                manufacturer=attrs.get("manufacturer"),
                product_name=attrs.get("product"),
                serial_number=attrs.get("serial"),
                port_path=device.name,
                is_hub=is_hub,
                is_mass_storage=False if is_hub else "08" in device.interface_classes,
                advertised_capability=f"USB {version}" if version else None,
                negotiated_speed_mbps=speed,
                speed_source=SPEED_SOURCE_SYSFS if speed else "UNKNOWN",
                speed_confidence=SpeedConfidence.HIGH if speed else SpeedConfidence.NONE,
                last_seen_at=now,
                raw={"interface_classes": device.interface_classes},
            )
        )
    return result


def usb_from_lsusb(tree: dict[str, dict], now: str) -> list[UsbDevice]:
    """Fallback: topology and speed only, so its confidence is lower than sysfs."""
    result = []
    for name, entry in tree.items():
        parent = sysfs_parent_name(name)
        classes = entry["classes"]
        is_hub = any("hub" in item for item in classes)
        result.append(
            UsbDevice(
                topology_id=name,
                parent_topology_id=parent if parent in tree else None,
                port_path=name,
                is_hub=is_hub,
                is_mass_storage=False if is_hub else any("storage" in c for c in classes),
                negotiated_speed_mbps=entry["speed_mbps"],
                speed_source=SPEED_SOURCE_LSUSB,
                speed_confidence=SpeedConfidence.MEDIUM,
                last_seen_at=now,
            )
        )
    return result


def _volume_from_node(node: dict, storage_id: str, usage: Callable[[str], Any]) -> Volume:
    mounts = _mounts(node)
    mount = mounts[0] if mounts else None
    capacity = _size(node.get("size"))
    free = None
    if mount:
        try:
            stats = usage(mount)
            capacity, free = stats.total, stats.free
        except OSError as exc:
            logger.warning("Could not read usage for %s: %s", mount, exc)
    identity = node.get("uuid") or node.get("path") or node.get("name")
    return Volume(
        volume_id=stable_id("vol", identity),
        storage_id=storage_id,
        partition_id=node.get("name"),
        mount_point=mount,
        filesystem=node.get("fstype"),
        volume_label=node.get("label"),
        fs_serial=node.get("uuid"),
        capacity_bytes=capacity,
        free_bytes=free,
        is_mounted=mount is not None,
        access_status=AccessStatus.UNKNOWN if mount else AccessStatus.NOT_MOUNTED,
        raw={"path": node.get("path"), "mounts": mounts},
    )


def normalize_lsblk(payload: dict, block_usb: Callable[[str], str | None],
                    usage: Callable[[str], Any] = shutil.disk_usage
                    ) -> tuple[list[StorageDevice], list[Volume]]:
    """Pure conversion of `lsblk -J` into normalized storage and volumes."""
    storage: list[StorageDevice] = []
    volumes: list[Volume] = []
    for node in payload.get("blockdevices", []):
        if node.get("type") in _SKIPPED_TYPES:
            continue
        name = node.get("name") or ""
        removable, size = _flag(node.get("rm")), _size(node.get("size"))
        topology = block_usb(name)
        bus = (node.get("tran") or ("mmc" if name.startswith("mmcblk") else "") or "").upper()
        # A removable device reporting zero sectors is the kernel saying "no medium".
        if size:
            media: bool | None = True
        elif size == 0 and removable:
            media = False
        else:
            media = None
        device = StorageDevice(
            storage_id=stable_id("sto", node.get("path"), node.get("serial"), topology),
            platform_device_id=node.get("path"),
            usb_topology_id=topology,
            model=(node.get("model") or "").strip() or None,
            serial_number=(node.get("serial") or "").strip() or None,
            capacity_bytes=size or None,
            removable=removable,
            read_only=_flag(node.get("ro")),
            bus_type=bus or None,
            media_present=media,
            correlation_evidence="sysfs block device path" if topology else None,
            raw={"name": name},
        )
        storage.append(device)
        children = node.get("children") or []
        if node.get("fstype"):  # "Superfloppy" card: a filesystem directly on the disk.
            volumes.append(_volume_from_node(node, device.storage_id, usage))
        for child in children:
            if child.get("fstype") or _mounts(child):
                volumes.append(_volume_from_node(child, device.storage_id, usage))
    return storage, volumes


class LinuxAdapter:
    def __init__(self, runner: CommandRunner | None = None,
                 sysfs_usb: Callable[[], list[SysfsUsbDevice]] | None = None,
                 block_usb: Callable[[str], str | None] | None = None,
                 detect_topology: bool = True, clock: Callable[[], str] = utc_now) -> None:
        self._runner = runner or SubprocessRunner()
        self._sysfs_usb = sysfs_usb or (lambda: list(read_sysfs_usb()))
        self._block_usb = block_usb or self._block_to_usb
        self._detect_topology = detect_topology
        self._clock = clock

    @staticmethod
    def _block_to_usb(name: str) -> str | None:
        try:
            return usb_device_from_block_path(os.path.realpath(f"/sys/block/{name}"))
        except OSError:
            return None

    def _lsblk(self) -> tuple[dict, list[DetectionIssue]]:
        last = "lsblk produced no output"
        # util-linux before 2.37 has no MOUNTPOINTS column, so fall back to MOUNTPOINT.
        for mount_column in ("MOUNTPOINTS", "MOUNTPOINT"):
            columns = _LSBLK_COLUMNS.format(mount=mount_column)
            # -a: without it, lsblk hides the zero-size devices that are empty readers.
            try:
                done = self._runner.run(["lsblk", "-J", "-b", "-a", "-o", columns], timeout=30)
            except DeviceDetectionError as exc:
                return {}, [DetectionIssue("linux:lsblk", str(exc), "ERROR")]
            if done.returncode == 0:
                try:
                    return json.loads(done.stdout), []
                except json.JSONDecodeError as exc:
                    return {}, [DetectionIssue("linux:lsblk", f"Bad JSON: {exc}", "ERROR")]
            last = done.stderr.strip()[:500]
        return {}, [DetectionIssue("linux:lsblk", last, "ERROR")]

    def _usb(self, now: str) -> tuple[list[UsbDevice], list[DetectionIssue]]:
        if not self._detect_topology:
            return [], []
        devices = self._sysfs_usb()
        if devices:
            return usb_from_sysfs(devices, now), []
        try:
            done = self._runner.run(["lsusb", "-t"], timeout=30)
        except DeviceDetectionError as exc:
            return [], [DetectionIssue("linux:usb-topology", f"Speed unavailable: {exc}")]
        if done.returncode != 0:
            return [], [DetectionIssue("linux:usb-topology", done.stderr.strip()[:500])]
        return usb_from_lsusb(parse_lsusb_tree(done.stdout), now), [
            DetectionIssue("linux:usb-topology", "sysfs empty; using lsusb -t fallback")
        ]

    def collect(self) -> RawInventory:
        now = self._clock()
        inventory = RawInventory(platform="linux")
        inventory.usb_devices, usb_issues = self._usb(now)
        payload, block_issues = self._lsblk()
        inventory.issues = usb_issues + block_issues
        inventory.storage_devices, inventory.volumes = normalize_lsblk(payload, self._block_usb)
        return inventory
