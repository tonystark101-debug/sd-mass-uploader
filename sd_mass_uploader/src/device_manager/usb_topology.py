"""USB topology: Windows hub IOCTLs and Linux sysfs / `lsusb -t`.

Negotiated speed comes from the operating system's view of a specific connection,
never from a product name. If it cannot be read, it stays unknown.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import struct
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# --- Windows ---------------------------------------------------------------------------------

GUID_DEVINTERFACE_USB_HUB = "{f18a0e88-c30c-11d0-8815-00a0c906bed8}"

IOCTL_USB_GET_NODE_INFORMATION = 0x220408
IOCTL_USB_GET_NODE_CONNECTION_NAME = 0x220414
IOCTL_USB_GET_NODE_CONNECTION_DRIVERKEY_NAME = 0x220420
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX = 0x220448
IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2 = 0x22045C
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080

DEVICE_CONNECTED = 1
_SPEED_MBPS = {0: 1.5, 1: 12.0, 2: 480.0, 3: 5000.0}
# ConnectionIndex, descriptor, config value, speed, is-hub, address, pipes, status. Packed.
_CONNECTION_EX = struct.Struct("<I18sBBBHII")
_DESCRIPTOR = struct.Struct("<BBHBBBBHHHBBBB")

FLAG_OPERATING_SUPERSPEED = 0x1
FLAG_OPERATING_SUPERSPEED_PLUS = 0x4


@dataclass
class WindowsPort:
    """One occupied downstream port of a hub, as the hub driver reports it."""

    hub_path: str
    port: int
    vendor_id: str
    product_id: str
    bcd_usb: int
    speed_code: int
    is_hub: bool
    v2_flags: int | None = None
    driver_key: str | None = None
    child_hub_path: str | None = None


def windows_speed_mbps(speed_code: int, v2_flags: int | None) -> float | None:
    """Map the driver's speed code to Mbps. V2 flags are the only way to see SuperSpeed+."""
    if v2_flags is not None and v2_flags & FLAG_OPERATING_SUPERSPEED_PLUS:
        return 10000.0  # Lowest SuperSpeedPlus rate; the API does not say how far above it.
    if v2_flags is not None and v2_flags & FLAG_OPERATING_SUPERSPEED:
        return 5000.0
    return _SPEED_MBPS.get(speed_code)


def parse_connection_info(buffer: bytes) -> dict | None:
    """Decode USB_NODE_CONNECTION_INFORMATION_EX. Returns None if nothing is connected."""
    (index, descriptor, _config, speed, is_hub, _addr, _pipes, status) = _CONNECTION_EX.unpack_from(
        buffer
    )
    if status != DEVICE_CONNECTED:
        return None
    fields = _DESCRIPTOR.unpack(descriptor)
    return {
        "port": index,
        "bcd_usb": fields[2],
        "vendor_id": f"{fields[7]:04x}",
        "product_id": f"{fields[8]:04x}",
        "speed_code": speed,
        "is_hub": bool(is_hub),
    }


def parse_name_buffer(buffer: bytes) -> str | None:
    """Decode the name returned by the CONNECTION_NAME and DRIVERKEY_NAME IOCTLs."""
    actual = struct.unpack_from("<I", buffer, 4)[0]
    if actual <= 8:
        return None
    text = buffer[8 : min(actual, len(buffer))].decode("utf-16-le", errors="ignore")
    return text.split("\0", 1)[0] or None


def hub_instance_id(hub_path: str) -> str:
    r"""Convert \\?\USB#ROOT_HUB30#5&2d8e&0&0#{guid} to the instance USB\ROOT_HUB30\5&2d8e&0&0."""
    body = hub_path.removeprefix("\\\\?\\").removeprefix("\\\\.\\")
    return "\\".join(body.split("#")[:3]).upper()


class _HubHandle:
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    INVALID = ctypes.c_void_p(-1).value

    def __init__(self, path: str) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        kernel32.DeviceIoControl.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32 = kernel32
        # Hub driver requires write access to the *hub control interface* for these queries.
        # That interface is not storage; no IOCTL used here changes hub or device state.
        self._handle = kernel32.CreateFileW(
            path, self.GENERIC_WRITE, self.FILE_SHARE_WRITE, None, self.OPEN_EXISTING, 0, None
        )
        if self._handle in (self.INVALID, None):
            raise OSError(ctypes.get_last_error(), f"Cannot open hub {path}")

    def __enter__(self) -> _HubHandle:
        return self

    def __exit__(self, *_: object) -> None:
        self._kernel32.CloseHandle(self._handle)

    def ioctl(self, code: int, payload: bytes, out_size: int) -> bytes | None:
        buffer = ctypes.create_string_buffer(payload, max(out_size, len(payload)))
        returned = ctypes.c_uint32(0)
        succeeded = self._kernel32.DeviceIoControl(
            self._handle, code, buffer, len(payload), buffer, len(buffer),
            ctypes.byref(returned), None,
        )
        return buffer.raw if succeeded else None


def _list_hub_interfaces() -> list[str]:
    """Ask the Configuration Manager for every present USB hub interface."""

    class Guid(ctypes.Structure):
        _fields_ = [
            ("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16),
            ("d", ctypes.c_ubyte * 8),
        ]

    hex_text = GUID_DEVINTERFACE_USB_HUB.strip("{}").replace("-", "")
    raw = bytes.fromhex(hex_text)
    guid = Guid(
        int(hex_text[:8], 16), int(hex_text[8:12], 16), int(hex_text[12:16], 16),
        (ctypes.c_ubyte * 8)(*raw[8:]),
    )
    cfgmgr = ctypes.WinDLL("cfgmgr32")
    length = ctypes.c_uint32(0)
    status = cfgmgr.CM_Get_Device_Interface_List_SizeW(ctypes.byref(length), ctypes.byref(guid),
                                                       None, 0)
    if status != 0:
        raise OSError(f"CM_Get_Device_Interface_List_Size failed: {status}")
    buffer = ctypes.create_unicode_buffer(length.value)
    status = cfgmgr.CM_Get_Device_Interface_ListW(ctypes.byref(guid), None, buffer, length, 0)
    if status != 0:
        raise OSError(f"CM_Get_Device_Interface_List failed: {status}")
    return [item for item in ctypes.wstring_at(buffer, length.value).split("\0") if item]


def _read_hub_ports(path: str) -> list[WindowsPort]:
    ports: list[WindowsPort] = []
    with _HubHandle(path) as hub:
        info = hub.ioctl(IOCTL_USB_GET_NODE_INFORMATION, b"\0" * 76, 76)
        if info is None:
            raise OSError(ctypes.get_last_error(), f"Hub information unavailable: {path}")
        for port in range(1, info[6] + 1):
            request = struct.pack("<I", port)
            raw = hub.ioctl(IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX, request + b"\0" * 508,
                            512)
            parsed = parse_connection_info(raw) if raw else None
            if parsed is None:
                continue
            v2 = hub.ioctl(
                IOCTL_USB_GET_NODE_CONNECTION_INFORMATION_EX_V2,
                struct.pack("<IIII", port, 16, 0x7, 0), 16,
            )
            driver = hub.ioctl(IOCTL_USB_GET_NODE_CONNECTION_DRIVERKEY_NAME,
                               request + b"\0" * 1020, 1024)
            child = None
            if parsed["is_hub"]:
                name = hub.ioctl(IOCTL_USB_GET_NODE_CONNECTION_NAME, request + b"\0" * 1020, 1024)
                child_name = parse_name_buffer(name) if name else None
                child = f"\\\\?\\{child_name}" if child_name else None
            ports.append(
                WindowsPort(
                    hub_path=path,
                    port=port,
                    vendor_id=parsed["vendor_id"],
                    product_id=parsed["product_id"],
                    bcd_usb=parsed["bcd_usb"],
                    speed_code=parsed["speed_code"],
                    is_hub=parsed["is_hub"],
                    v2_flags=struct.unpack_from("<I", v2, 12)[0] if v2 else None,
                    driver_key=parse_name_buffer(driver) if driver else None,
                    child_hub_path=child,
                )
            )
    return ports


def volume_disk_number(drive_letter: str) -> tuple[int, int] | None:
    """Ask the storage driver which (disk, partition) holds a drive letter.

    Needed for cards formatted with no partition table, which Get-Partition can miss. The volume
    is opened with zero access rights: it can answer this query, but cannot read or write data.
    """
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    file_share_read_write = 0x3
    handle = kernel32.CreateFileW(f"\\\\.\\{drive_letter}:", 0, file_share_read_write, None,
                                  _HubHandle.OPEN_EXISTING, 0, None)
    if handle in (_HubHandle.INVALID, None):
        return None
    try:
        output = ctypes.create_string_buffer(12)
        returned = ctypes.c_uint32(0)
        succeeded = kernel32.DeviceIoControl(handle, IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 0,
                                             output, 12, ctypes.byref(returned), None)
        if not succeeded:
            return None
        _device_type, disk, partition = struct.unpack("<III", output.raw)
        return disk, partition
    finally:
        kernel32.CloseHandle(handle)


def enumerate_windows_hubs() -> tuple[list[str], list[WindowsPort], list[str]]:
    """Return (hub paths, occupied ports, per-hub errors). One bad hub never stops the rest."""
    if sys.platform != "win32":
        return [], [], ["Windows USB APIs are not available on this platform"]
    hubs = _list_hub_interfaces()
    ports: list[WindowsPort] = []
    errors: list[str] = []
    for hub in hubs:
        try:
            ports.extend(_read_hub_ports(hub))
        except OSError as exc:
            errors.append(f"{hub}: {exc}")
    return hubs, ports, errors


# --- Linux -----------------------------------------------------------------------------------

@dataclass
class SysfsUsbDevice:
    name: str  # Kernel name such as 2-1.3, which encodes bus and port chain.
    attrs: dict[str, str]
    interface_classes: list[str]


_USB_DEVICE_NAME = re.compile(r"^(usb\d+|\d+-\d+(\.\d+)*)$")
_USB_PATH_PART = re.compile(r"^\d+-\d+(\.\d+)*$")
_ATTRS = ("idVendor", "idProduct", "manufacturer", "product", "serial", "speed", "version",
          "bDeviceClass", "busnum", "devpath")


def read_sysfs_usb(root: str = "/sys/bus/usb/devices") -> Iterator[SysfsUsbDevice]:
    """Read every USB device from sysfs. A device may vanish mid-read; it is skipped if so."""
    base = Path(root)
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return
    for name in entries:
        if not _USB_DEVICE_NAME.match(name):
            continue
        attrs = {}
        try:
            for attr in _ATTRS:
                path = base / name / attr
                if path.exists():
                    attrs[attr] = path.read_text(encoding="utf-8", errors="replace").strip()
            classes = []
            for child in sorted(os.listdir(base / name)):
                class_file = base / name / child / "bInterfaceClass"
                if child.startswith(f"{name}:") and class_file.exists():
                    classes.append(class_file.read_text(encoding="ascii").strip().lower())
        except OSError as exc:
            logger.warning("USB device %s disappeared during read: %s", name, exc)
            continue
        yield SysfsUsbDevice(name, attrs, classes)


def sysfs_parent_name(name: str) -> str | None:
    """2-1.3.2 -> 2-1.3 -> 2-1 -> usb2 -> None."""
    if name.startswith("usb"):
        return None
    if "." in name:
        return name.rsplit(".", 1)[0]
    return f"usb{name.split('-', 1)[0]}"


def usb_device_from_block_path(resolved_path: str) -> str | None:
    """Find the deepest USB device in a resolved /sys/block/<name> path."""
    found = None
    for part in resolved_path.replace("\\", "/").split("/"):
        if _USB_PATH_PART.match(part):
            found = part
    return found


_LSUSB_LINE = re.compile(
    r"Port\s+(?P<port>\d+):\s*Dev\s+(?P<dev>\d+),.*?Class=(?P<cls>[^,]*),"
    r".*?,\s*(?P<speed>[\d.]+)(?P<unit>[MG])"
)
_LSUSB_BUS = re.compile(r"Bus\s+(?P<bus>\d+)\.Port\s+\d+:\s*Dev\s+\d+")


def parse_lsusb_tree(text: str) -> dict[str, dict]:
    """Parse `lsusb -t` into {'2-1.3': {...}}; the fallback when sysfs is unavailable."""
    devices: dict[str, dict] = {}
    stack: list[tuple[int, str]] = []
    bus = None
    for line in text.splitlines():
        bus_match = _LSUSB_BUS.search(line) if line.lstrip().startswith("/:") else None
        if bus_match:
            bus = int(bus_match["bus"])
            stack = []
            continue
        match = _LSUSB_LINE.search(line)
        if not match or bus is None or "Port" not in line:
            continue
        depth = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= depth:
            stack.pop()
        port = match["port"]
        name = f"{stack[-1][1]}.{port}" if stack else f"{bus}-{port}"
        stack.append((depth, name))
        speed = float(match["speed"]) * (1000 if match["unit"] == "G" else 1)
        entry = devices.setdefault(name, {"speed_mbps": speed, "classes": []})
        entry["classes"].append(match["cls"].strip().lower())
    return devices
