"""Windows adapter. Read-only CIM/storage queries, normalized into the common models."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from src.core.enums import AccessStatus, SpeedConfidence
from src.core.exceptions import DeviceDetectionError
from src.core.models import DetectionIssue, RawInventory, StorageDevice, UsbDevice, Volume
from src.device_manager.base import stable_id, usb_capability_from_bcd
from src.device_manager.usb_topology import (
    WindowsPort,
    enumerate_windows_hubs,
    hub_instance_id,
    volume_disk_number,
    windows_speed_mbps,
)
from src.platform.process import CommandRunner, SubprocessRunner, run_powershell

logger = logging.getLogger(__name__)

SPEED_SOURCE = "windows:IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX"
_MASS_STORAGE_SERVICES = {"usbstor", "uaspstor"}
_EXTERNAL_MEDIA = re.compile(r"removable", re.IGNORECASE)

INVENTORY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$r = [ordered]@{ errors = @() }
function Section($name, [scriptblock]$body) {
  try { $r[$name] = @(& $body) }
  catch { $r.errors += @{ section = $name; message = "$($_.Exception.Message)" }; $r[$name] = @() }
}
Section 'disk_drives' { Get-CimInstance Win32_DiskDrive | Select-Object Index, DeviceID, Model,
  InterfaceType, MediaType, MediaLoaded, Size, PNPDeviceID, SerialNumber, Status }
Section 'disks' { Get-Disk | Select-Object Number, FriendlyName, SerialNumber, Size, IsOffline,
  IsReadOnly, @{n='BusType';e={"$($_.BusType)"}},
  @{n='OperationalStatus';e={"$($_.OperationalStatus)"}} }
Section 'partitions' { Get-Partition | Select-Object DiskNumber, PartitionNumber, Size, AccessPaths,
  @{n='DriveLetter';e={"$($_.DriveLetter)".Trim([char]0)}} }
Section 'volumes' { Get-CimInstance Win32_Volume | Select-Object DeviceID, DriveLetter, Label,
  FileSystem, Capacity, FreeSpace, SerialNumber, DriveType, Name }
Section 'usb_pnp' { Get-CimInstance Win32_PnPEntity -Filter "DeviceID LIKE 'USB\\%'" |
  Select-Object DeviceID, Name, Manufacturer, Service, CompatibleID, Status }
Section 'pnp_properties' {
  $ids = @($r['usb_pnp'] | ForEach-Object { $_.DeviceID }) +
         @($r['disk_drives'] | ForEach-Object { $_.PNPDeviceID })
  $ids = @($ids | Where-Object { $_ } | Sort-Object -Unique)
  # One at a time: passing -InstanceId an array repeats some devices and drops others.
  foreach ($id in $ids) {
    try {
      Get-PnpDeviceProperty -InstanceId $id -KeyName DEVPKEY_Device_Parent, DEVPKEY_Device_Driver |
        Select-Object InstanceId, KeyName, Data
    } catch {
      $r.errors += @{ section = "pnp_properties:$id"; message = "$($_.Exception.Message)" }
    }
  }
}
ConvertTo-Json -InputObject $r -Depth 5 -Compress
"""


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


def _usb_identity(instance_id: str | None) -> tuple[str | None, str | None]:
    """Serial is the last instance segment, unless Windows generated it (contains '&')."""
    if not instance_id:
        return None, None
    parts = instance_id.split("\\")
    if len(parts) < 3 or "&" in parts[2]:
        return None, None
    return parts[2], parts[1]


def _build_usb(payload: dict, hubs: list[str], ports: list[WindowsPort], now: str
               ) -> tuple[list[UsbDevice], dict[str, str]]:
    """Build the USB tree. Returns the devices and {instance id: topology id}."""
    pnp = {row["DeviceID"].upper(): row for row in payload.get("usb_pnp", [])}
    driver_to_instance: dict[str, str] = {}
    parents: dict[str, str] = {}
    for prop in payload.get("pnp_properties", []):
        data, instance = _text(prop.get("Data")), prop["InstanceId"].upper()
        if not data:
            continue
        if prop["KeyName"] == "DEVPKEY_Device_Driver":
            driver_to_instance[data.lower()] = instance
        elif prop["KeyName"] == "DEVPKEY_Device_Parent":
            parents[instance] = data.upper()

    devices: list[UsbDevice] = []
    instance_to_topology: dict[str, str] = {}
    hub_to_topology: dict[str, str] = {}
    hub_to_port_path: dict[str, str] = {}

    downstream = {port.child_hub_path.upper() for port in ports if port.child_hub_path}
    for hub in hubs:
        if hub.upper() in downstream:
            continue  # An external hub, represented below by the port it occupies.
        instance = hub_instance_id(hub)
        row = pnp.get(instance, {})
        topology_id = stable_id("usb", instance)
        short = instance.rsplit("\\", 1)[-1]
        hub_to_topology[hub.upper()] = topology_id
        hub_to_port_path[hub.upper()] = f"root:{short}"
        instance_to_topology[instance] = topology_id
        devices.append(
            UsbDevice(
                topology_id=topology_id,
                device_path=hub,
                product_name=_text(row.get("Name")),
                manufacturer=_text(row.get("Manufacturer")),
                port_path=f"root:{short}",
                is_hub=True,
                is_mass_storage=False,
                last_seen_at=now,
            )
        )

    pending = list(ports)
    while pending:  # Parents first, because a port's path builds on its hub's path.
        ready = [p for p in pending if p.hub_path.upper() in hub_to_topology]
        if not ready:
            for port in pending:
                logger.warning("USB port with unknown parent hub: %s", port.hub_path)
            break
        pending = [p for p in pending if p.hub_path.upper() not in hub_to_topology]
        for port in ready:
            hub_key = port.hub_path.upper()
            instance = driver_to_instance.get((port.driver_key or "").lower())
            row = pnp.get(instance or "", {})
            port_path = f"{hub_to_port_path[hub_key]}/{port.port}"
            topology_id = stable_id("usb", port_path, port.vendor_id, port.product_id)
            speed = windows_speed_mbps(port.speed_code, port.v2_flags)
            serial, _ = _usb_identity(instance)
            devices.append(
                UsbDevice(
                    topology_id=topology_id,
                    parent_topology_id=hub_to_topology[hub_key],
                    device_path=instance,
                    vendor_id=port.vendor_id,
                    product_id=port.product_id,
                    manufacturer=_text(row.get("Manufacturer")),
                    product_name=_text(row.get("Name")),
                    serial_number=serial,
                    port_path=port_path,
                    is_hub=port.is_hub,
                    is_mass_storage=False if port.is_hub else _is_mass_storage(row),
                    advertised_capability=usb_capability_from_bcd(port.bcd_usb),
                    negotiated_speed_mbps=speed,
                    speed_source=SPEED_SOURCE if speed else "UNKNOWN",
                    speed_confidence=SpeedConfidence.HIGH if speed else SpeedConfidence.NONE,
                    last_seen_at=now,
                    raw={"speed_code": port.speed_code, "v2_flags": port.v2_flags,
                         "port": port.port},
                )
            )
            if instance:
                instance_to_topology[instance] = topology_id
            if port.child_hub_path:
                hub_to_topology[port.child_hub_path.upper()] = topology_id
                hub_to_port_path[port.child_hub_path.upper()] = port_path

    _mark_interface_parents(payload, parents, instance_to_topology, devices)
    return devices, _with_ancestors(parents, instance_to_topology)


def _is_mass_storage(row: dict) -> bool | None:
    if not row:
        return None
    if (_text(row.get("Service")) or "").lower() in _MASS_STORAGE_SERVICES:
        return True
    compatible = row.get("CompatibleID") or []
    if isinstance(compatible, str):
        compatible = [compatible]
    # Composite devices expose this on a child interface, handled by the caller.
    return any("class_08" in str(item).lower() for item in compatible)


def _mark_interface_parents(payload: dict, parents: dict[str, str],
                            instance_to_topology: dict[str, str],
                            devices: list[UsbDevice]) -> None:
    """A composite device is mass storage if one of its interfaces is."""
    by_topology = {device.topology_id: device for device in devices}
    for row in payload.get("usb_pnp", []):
        if not _is_mass_storage(row):
            continue
        topology = _ascend(row["DeviceID"].upper(), parents, instance_to_topology)
        if topology and not by_topology[topology].is_hub:
            by_topology[topology].is_mass_storage = True


def _ascend(instance: str, parents: dict[str, str], known: dict[str, str]) -> str | None:
    seen: set[str] = set()
    current: str | None = instance
    while current and current not in seen:
        if current in known:
            return known[current]
        seen.add(current)
        current = parents.get(current)
    return None


def _with_ancestors(parents: dict[str, str], known: dict[str, str]) -> dict[str, str]:
    """Extend {instance: topology} to descendants such as composite interfaces and disks."""
    resolved = dict(known)
    for instance in parents:
        topology = _ascend(instance, parents, known)
        if topology:
            resolved[instance] = topology
    return resolved


def _media_present(drive: dict, disk: dict) -> bool | None:
    """False only with positive OS evidence: MediaLoaded false, or status 'No Media'."""
    if (_text(disk.get("OperationalStatus")) or "").lower() == "no media":
        return False
    loaded = drive.get("MediaLoaded")
    if loaded is False and not drive.get("Size"):
        return False
    if loaded is True or drive.get("Size"):
        return True
    return None


def _build_storage(payload: dict, instance_to_topology: dict[str, str]) -> list[StorageDevice]:
    disks = {row["Number"]: row for row in payload.get("disks", [])}
    result: list[StorageDevice] = []
    for drive in payload.get("disk_drives", []):
        disk = disks.get(drive.get("Index"), {})
        pnp_id = _text(drive.get("PNPDeviceID"))
        topology = instance_to_topology.get((pnp_id or "").upper())
        media = _text(drive.get("MediaType")) or ""
        bus = _text(disk.get("BusType")) or _text(drive.get("InterfaceType"))
        result.append(
            StorageDevice(
                storage_id=stable_id("sto", pnp_id),
                platform_device_id=pnp_id,
                usb_topology_id=topology,
                model=_text(drive.get("Model")),
                serial_number=_text(drive.get("SerialNumber")) or _text(disk.get("SerialNumber")),
                capacity_bytes=drive.get("Size") or disk.get("Size") or None,
                removable=True if _EXTERNAL_MEDIA.search(media) else (
                    False if "fixed" in media.lower() else None),
                read_only=disk.get("IsReadOnly"),
                bus_type=bus.upper() if bus else None,
                media_present=_media_present(drive, disk),
                correlation_evidence="PnP parent chain to USB port" if topology else None,
                raw={"disk_number": drive.get("Index"), "interface": drive.get("InterfaceType"),
                     "media_type": drive.get("MediaType"), "status": drive.get("Status"),
                     "operational_status": disk.get("OperationalStatus")},
            )
        )
    return result


def _format_serial(value: Any) -> str | None:
    """Show Win32_Volume's integer serial the way FAT/exFAT tools print it: ABCD-1234."""
    if not isinstance(value, int) or value == 0:
        return None
    return f"{(value >> 16) & 0xFFFF:04X}-{value & 0xFFFF:04X}"


DiskLookup = Callable[[str], tuple[int, int] | None]


def _build_volumes(payload: dict, storage: list[StorageDevice],
                   disk_lookup: DiskLookup) -> list[Volume]:
    disk_to_storage = {item.raw.get("disk_number"): item.storage_id for item in storage}
    access_path_owner: dict[str, tuple[int, int]] = {}
    letter_owner: dict[str, tuple[int, int]] = {}
    for part in payload.get("partitions", []):
        owner = (part.get("DiskNumber"), part.get("PartitionNumber"))
        for path in part.get("AccessPaths") or []:
            access_path_owner[str(path).lower()] = owner
        if _text(part.get("DriveLetter")):
            letter_owner[_text(part["DriveLetter"]).upper()] = owner

    result: list[Volume] = []
    for row in payload.get("volumes", []):
        device_id = _text(row.get("DeviceID")) or ""
        letter = (_text(row.get("DriveLetter")) or "").rstrip(":").upper() or None
        owner = access_path_owner.get(device_id.lower()) or letter_owner.get(letter or "")
        if owner is None and letter:
            owner = disk_lookup(letter)  # Cards with no partition table.
        name = _text(row.get("Name"))
        mount = name if name and not name.startswith("\\\\?\\") else None
        result.append(
            Volume(
                volume_id=stable_id("vol", device_id),
                storage_id=disk_to_storage.get(owner[0]) if owner else None,
                partition_id=f"{owner[0]}:{owner[1]}" if owner else None,
                mount_point=mount,
                drive_letter=letter,
                filesystem=_text(row.get("FileSystem")),
                volume_label=_text(row.get("Label")),
                fs_serial=_format_serial(row.get("SerialNumber")),
                capacity_bytes=row.get("Capacity"),
                free_bytes=row.get("FreeSpace"),
                is_mounted=mount is not None,
                access_status=AccessStatus.UNKNOWN if mount else AccessStatus.NOT_MOUNTED,
                raw={"device_id": device_id, "drive_type": row.get("DriveType")},
            )
        )
    return result


def normalize_windows_inventory(payload: dict, hubs: list[str], ports: list[WindowsPort],
                                now: str, disk_lookup: DiskLookup = lambda _: None
                                ) -> RawInventory:
    """Pure conversion of OS output into normalized records. Tested with recorded fixtures."""
    inventory = RawInventory(platform="windows")
    for error in payload.get("errors", []):
        inventory.issues.append(
            DetectionIssue(f"windows:{error.get('section')}", str(error.get("message")))
        )
    usb, instance_to_topology = _build_usb(payload, hubs, ports, now)
    inventory.usb_devices = usb
    inventory.storage_devices = _build_storage(payload, instance_to_topology)
    inventory.volumes = _build_volumes(payload, inventory.storage_devices, disk_lookup)
    return inventory


HubReader = Callable[[], tuple[list[str], list[WindowsPort], list[str]]]


class WindowsAdapter:
    def __init__(self, runner: CommandRunner | None = None,
                 hub_reader: HubReader = enumerate_windows_hubs,
                 detect_topology: bool = True, clock: Callable[[], str] | None = None) -> None:
        from src.core.timeutil import utc_now

        self._runner = runner or SubprocessRunner()
        self._hub_reader = hub_reader
        self._detect_topology = detect_topology
        self._clock = clock or utc_now

    def _query(self) -> tuple[dict, list[DetectionIssue]]:
        try:
            done = run_powershell(self._runner, INVENTORY_SCRIPT, timeout=90)
        except DeviceDetectionError as exc:
            return {}, [DetectionIssue("windows:powershell", str(exc), "ERROR")]
        if done.returncode != 0:
            return {}, [DetectionIssue("windows:powershell", done.stderr.strip()[:500], "ERROR")]
        try:
            return json.loads(done.stdout), []
        except json.JSONDecodeError as exc:
            return {}, [DetectionIssue("windows:powershell", f"Bad JSON: {exc}", "ERROR")]

    def collect(self) -> RawInventory:
        payload, issues = self._query()
        hubs: list[str] = []
        ports: list[WindowsPort] = []
        if self._detect_topology:
            try:
                hubs, ports, errors = self._hub_reader()
                issues.extend(DetectionIssue("windows:usb-topology", e) for e in errors)
            except OSError as exc:
                issues.append(DetectionIssue(
                    "windows:usb-topology", f"Negotiated speed unavailable: {exc}"))
        inventory = normalize_windows_inventory(payload, hubs, ports, self._clock(),
                                                volume_disk_number)
        inventory.issues[:0] = issues
        return inventory
