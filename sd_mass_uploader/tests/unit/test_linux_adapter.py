"""Linux adapter against recorded-format fixtures. Not yet run on a real Ubuntu machine."""

import json
import sys
from types import SimpleNamespace

import pytest

from src.core.enums import AccessStatus, DeviceStatus, SpeedConfidence
from src.device_manager.correlation import correlate
from src.device_manager.linux import (
    SPEED_SOURCE_SYSFS,
    LinuxAdapter,
    normalize_lsblk,
    usb_from_sysfs,
)
from src.device_manager.usb_topology import (
    SysfsUsbDevice,
    parse_lsusb_tree,
    read_sysfs_usb,
    sysfs_parent_name,
    usb_device_from_block_path,
)
from src.platform.process import CommandResult

NOW = "2026-11-06T00:00:00+00:00"

LSBLK = {"blockdevices": [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk", "tran": "nvme",
     "size": 512110190592, "rm": False, "ro": False, "fstype": None, "label": None,
     "uuid": None, "mountpoints": [None],
     "model": "Samsung SSD", "serial": "S1",
     "children": [{"name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "type": "part",
                   "size": 512000000000, "fstype": "ext4", "uuid": "root-uuid", "label": None,
                   "mountpoints": ["/"]}]},
    {"name": "loop0", "path": "/dev/loop0", "type": "loop", "size": 4096, "rm": False},
    {"name": "sdb", "path": "/dev/sdb", "type": "disk", "tran": "usb", "size": 63000000000,
     "rm": "1", "ro": "0", "fstype": None, "mountpoints": [None], "model": "SD Card Reader ",
     "serial": "000000001", "children": [
         {"name": "sdb1", "path": "/dev/sdb1", "type": "part", "size": 62900000000,
          "fstype": "exfat", "uuid": "1A2B-3C4D", "label": "CAM_A",
          "mountpoints": ["/media/op/CAM_A"]}]},
    {"name": "sdc", "path": "/dev/sdc", "type": "disk", "tran": "usb", "size": 0, "rm": True,
     "ro": False, "fstype": None, "mountpoints": [None], "model": "SD Card Reader",
     "serial": "000000001"},
    {"name": "sdd", "path": "/dev/sdd", "type": "disk", "tran": "usb", "size": 31000000000,
     "rm": True, "ro": False, "fstype": "vfat", "uuid": "AAAA-BBBB", "label": "FLOPPY",
     "mountpoints": [None], "model": None, "serial": None},
    {"name": "mmcblk0", "path": "/dev/mmcblk0", "type": "disk", "tran": None, "size": 8000000000,
     "rm": False, "ro": False, "fstype": None, "mountpoints": [None], "children": [
         {"name": "mmcblk0p1", "path": "/dev/mmcblk0p1", "type": "part", "size": 7900000000,
          "fstype": "vfat", "uuid": "1111-2222", "label": "BUILTIN",
          "mountpoint": "/media/op/BUILTIN"}]},
]}

BLOCK_USB = {"sdb": "2-1.1", "sdc": "2-1.2", "sdd": "2-1.3"}


def sysfs_device(name, cls="00", classes=("08",), speed="5000", version="3.00", **extra):
    attrs = {"idVendor": "0bda", "idProduct": "0307", "speed": speed, "version": version,
             "bDeviceClass": cls, "product": "USB3.0 Card Reader", **extra}
    return SysfsUsbDevice(name, attrs, list(classes))


SYSFS = [
    sysfs_device("usb2", cls="09", classes=("09",), product="xHCI Host Controller"),
    sysfs_device("2-1", cls="09", classes=("09",), product="USB3.0 Hub"),
    sysfs_device("2-1.1", serial="000000001"),
    sysfs_device("2-1.2", serial="000000001", speed="480"),
    sysfs_device("2-1.3", serial="000000001"),
    sysfs_device("2-1.4", speed=""),  # Reader with no block device, so slot state is unknown.
]


def usage(_):
    return SimpleNamespace(total=62900000000, free=1000000)


def build():
    storage, volumes = normalize_lsblk(LSBLK, BLOCK_USB.get, usage)
    for volume in volumes:
        if volume.is_mounted:
            volume.access_status = AccessStatus.OK
    from src.core.models import RawInventory

    raw = RawInventory("linux", usb_from_sysfs(SYSFS, NOW), storage, volumes)
    return correlate(raw, NOW)


def test_hubs_excluded_and_each_reader_found():
    snapshot = build()
    assert sum(d.is_hub for d in snapshot.usb_devices) == 2
    assert len(snapshot.readers) == 4


def test_statuses_match_evidence():
    by_path = {r.topology_path: r.status for r in build().readers}
    assert by_path["2-1.1"] == DeviceStatus.ACTIVE
    assert by_path["2-1.2"] == DeviceStatus.EMPTY_READER
    assert by_path["2-1.3"] == DeviceStatus.UNMOUNTED_STORAGE
    assert by_path["2-1.4"] == DeviceStatus.READER_PRESENT_SLOT_UNKNOWN


def test_missing_from_lsblk_is_not_called_empty():
    assert not any(r.status == DeviceStatus.EMPTY_READER and r.topology_path == "2-1.4"
                   for r in build().readers)


def test_speed_comes_from_sysfs():
    device = next(d for d in build().usb_devices if d.port_path == "2-1.2")
    assert device.negotiated_speed_mbps == 480
    assert device.advertised_capability == "USB 3.00"
    assert device.speed_source == SPEED_SOURCE_SYSFS
    assert device.speed_confidence == SpeedConfidence.HIGH


def test_blank_speed_stays_unknown():
    device = next(d for d in build().usb_devices if d.port_path == "2-1.4")
    assert device.negotiated_speed_mbps is None
    assert device.speed_confidence == SpeedConfidence.NONE


def test_duplicate_serials_are_flagged_and_told_apart():
    snapshot = build()
    assert any("share serial" in issue.message for issue in snapshot.issues)
    assert len({r.reader_id for r in snapshot.readers}) == 4


def test_loop_devices_skipped_and_superfloppy_volume_kept():
    snapshot = build()
    assert not any("loop" in (s.platform_device_id or "") for s in snapshot.storage_devices)
    assert any(v.volume_label == "FLOPPY" for v in snapshot.volumes)


def test_mountpoint_singular_and_mmc_bus():
    snapshot = build()
    volume = next(v for v in snapshot.volumes if v.volume_label == "BUILTIN")
    assert volume.mount_point == "/media/op/BUILTIN"
    mmc = next(s for s in snapshot.storage_devices if s.storage_id == volume.storage_id)
    assert mmc.bus_type == "MMC"


def test_lsblk_falls_back_to_old_column():
    class Runner:
        def __init__(self):
            self.calls = []

        def run(self, argv, timeout=60):
            self.calls.append(argv)
            if "MOUNTPOINTS" in argv[-1]:
                return CommandResult(1, "", "unknown column: MOUNTPOINTS")
            return CommandResult(0, json.dumps(LSBLK), "")

    runner = Runner()
    adapter = LinuxAdapter(runner, sysfs_usb=lambda: SYSFS, block_usb=BLOCK_USB.get)
    raw = adapter.collect()
    assert len(runner.calls) == 2
    assert "-a" in runner.calls[0]  # Without -a, lsblk hides empty readers.
    assert raw.storage_devices


def test_lsusb_fallback_when_sysfs_is_empty():
    class Runner:
        def run(self, argv, timeout=60):
            if argv[0] == "lsusb":
                return CommandResult(0, LSUSB_TREE, "")
            return CommandResult(0, json.dumps({"blockdevices": []}), "")

    raw = LinuxAdapter(Runner(), sysfs_usb=list, block_usb=lambda _: None).collect()
    device = next(d for d in raw.usb_devices if d.topology_id == "2-1.2")
    assert device.negotiated_speed_mbps == 480
    assert device.speed_confidence == SpeedConfidence.MEDIUM
    assert any("fallback" in i.message for i in raw.issues)


def test_lsblk_missing_program_is_reported():
    from src.core.exceptions import DeviceDetectionError

    class Runner:
        def run(self, argv, timeout=60):
            raise DeviceDetectionError("Program not found: lsblk")

    raw = LinuxAdapter(Runner(), sysfs_usb=lambda: SYSFS, block_usb=lambda _: None).collect()
    assert raw.issues[0].severity == "ERROR"


LSUSB_TREE = """/:  Bus 02.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/4p, 5000M
    |__ Port 1: Dev 2, If 0, Class=Hub, Driver=hub/4p, 5000M
        |__ Port 1: Dev 3, If 0, Class=Mass Storage, Driver=usb-storage, 5000M
        |__ Port 2: Dev 4, If 0, Class=Mass Storage, Driver=uas, 480M
/:  Bus 01.Port 1: Dev 1, Class=root_hub, Driver=xhci_hcd/12p, 480M
    |__ Port 3: Dev 5, If 0, Class=Wireless, Driver=btusb, 12M
    |__ Port 3: Dev 5, If 1, Class=Wireless, Driver=btusb, 12M
"""


def test_parse_lsusb_tree():
    tree = parse_lsusb_tree(LSUSB_TREE)
    assert tree["2-1"]["speed_mbps"] == 5000
    assert tree["2-1.1"]["classes"] == ["mass storage"]
    assert tree["2-1.2"]["speed_mbps"] == 480
    assert tree["1-3"]["speed_mbps"] == 12


def test_block_path_finds_deepest_usb_device():
    path = ("/sys/devices/pci0000:00/0000:00:14.0/usb2/2-1/2-1.3/2-1.3:1.0/host6/target6:0:0/"
            "6:0:0:0/block/sdb")
    assert usb_device_from_block_path(path) == "2-1.3"
    assert usb_device_from_block_path("/sys/devices/pci0000:00/nvme/nvme0/nvme0n1") is None


def test_parent_names():
    assert sysfs_parent_name("2-1.3.2") == "2-1.3"
    assert sysfs_parent_name("2-1") == "usb2"
    assert sysfs_parent_name("usb2") is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows forbids ':' in folder names")
def test_sysfs_reader_with_fake_tree(tmp_path):
    device = tmp_path / "2-1.3"
    interface = device / "2-1.3:1.0"
    interface.mkdir(parents=True)
    (device / "idVendor").write_text("0bda\n")
    (device / "speed").write_text("5000\n")
    (interface / "bInterfaceClass").write_text("08\n")
    (tmp_path / "2-1.3:1.0").mkdir()  # Interfaces are skipped as devices.
    (tmp_path / "pci_junk").mkdir()
    found = list(read_sysfs_usb(str(tmp_path)))
    assert [d.name for d in found] == ["2-1.3"]
    assert found[0].attrs["speed"] == "5000"
    assert found[0].interface_classes == ["08"]
