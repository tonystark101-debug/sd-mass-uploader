"""Ubuntu: cards behind a USB hub are opened and listed by the terminal commands too."""

from __future__ import annotations

import json

import pytest

from src.core.exceptions import ReadOnlyViolation
from src.core.settings import StationSettings
from src.device_manager.linux import LinuxAdapter
from src.device_manager.usb_topology import SysfsUsbDevice
from src.inventory.inventory_service import find_cards
from src.inventory.readiness import explain, ready_snapshot, render_explanation
from src.platform.process import CommandResult, assert_read_only


class Hub:
    """One reader straight in the laptop (1-2) and a hub (1-1) with 16 readers (1-1.1..16).

    The direct card is already open (Ubuntu opened it). The hub's cards are not, and its
    readers appear a few seconds late, the way a hub of readers starts up."""

    def __init__(self, root, late_scans=1, refuse=False, dead=0):
        self.root = root
        self.scans = 0
        self.late_scans = late_scans
        self.refuse = refuse
        self.dead = dead  # Readers on the hub that USB sees but that never become a disk.
        self.mounted = {"/dev/sda1": self._dir("direct")}
        self.calls = []

    def _dir(self, name):
        path = self.root / name
        path.mkdir(exist_ok=True)
        (path / "DCIM").mkdir(exist_ok=True)
        return str(path)

    def disks(self):
        names = [("sda", "1-2")]
        if self.scans > self.late_scans:
            names += [(f"sd{chr(ord('b') + i)}", f"1-1.{i + 1}") for i in range(16 - self.dead)]
        return names

    def sysfs(self):
        devices = [SysfsUsbDevice("usb1", {"bDeviceClass": "09"}, ["09"]),
                   SysfsUsbDevice("1-1", {"bDeviceClass": "09", "product": "USB3.0 Hub",
                                          "speed": "5000"}, ["09"]),
                   SysfsUsbDevice("1-2", {"product": "SD Reader", "speed": "5000"}, ["08"])]
        for i in range(16):
            devices.append(SysfsUsbDevice(f"1-1.{i + 1}", {"product": "SD Reader",
                                                           "speed": "5000"}, ["08"]))
        return devices

    def block_usb(self, name):
        return dict(self.disks()).get(name)

    def run(self, argv, timeout=60):
        assert_read_only(argv)
        self.calls.append(argv)
        if argv[0] == "udevadm":
            return CommandResult(0, "", "")
        if argv[0] == "udisksctl":
            device = argv[3]
            if self.refuse:
                return CommandResult(1, "", "Error mounting: GDBus.Error:org.freedesktop."
                                            "UDisks2.Error.NotAuthorizedCanObtain: Not "
                                            "authorized to perform operation")
            self.mounted[device] = self._dir(device.rsplit("/", 1)[-1])
            return CommandResult(0, f"Mounted {device} at {self.mounted[device]}", "")
        assert argv[0] == "lsblk"
        self.scans += 1
        nodes = []
        for index, (name, _port) in enumerate(self.disks()):
            part = f"/dev/{name}1"
            nodes.append({
                "name": name, "path": f"/dev/{name}", "type": "disk", "tran": "usb",
                "size": 64_000_000_000, "rm": True, "ro": False, "model": "SD Reader",
                "serial": "000000000001",  # Cheap readers share one serial; port tells apart.
                "children": [{"name": f"{name}1", "path": part, "type": "part", "tran": None,
                              "size": 63_000_000_000, "fstype": "exfat", "label": "CARD",
                              "uuid": f"AB{index:02d}-1234",
                              "mountpoints": [self.mounted.get(part)]}]})
        return CommandResult(0, json.dumps({"blockdevices": nodes}), "")


def _ready(hub, **kwargs):
    adapter = LinuxAdapter(runner=hub, sysfs_usb=hub.sysfs, block_usb=hub.block_usb,
                           clock=lambda: "2026-11-24T00:00:00+00:00")
    now = [0.0]
    return ready_snapshot(adapter, StationSettings(), runner=hub, linux=True,
                          clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s),
                          **kwargs)


def test_cards_behind_a_hub_are_opened_and_listed(tmp_path):
    hub = Hub(tmp_path)
    snapshot, problems = _ready(hub)
    cards = find_cards(snapshot)
    assert len(cards) == 17 and problems == []
    mounts = [argv for argv in hub.calls if argv[0] == "udisksctl"]
    assert len(mounts) == 16 and all("ro" in argv[5].split(",") for argv in mounts)
    assert ["udevadm", "settle", "--timeout=10"] in hub.calls
    lines = explain(snapshot)
    assert sum(line.is_card for line in lines) == 17
    assert {line.port for line in lines} >= {"1-2", "1-1.1", "1-1.16"}


def test_readers_that_start_late_are_waited_for(tmp_path):
    hub = Hub(tmp_path, late_scans=3)  # The hub's readers show up on the 4th look.
    snapshot, _ = _ready(hub)
    assert len(find_cards(snapshot)) == 17


def test_a_refused_card_says_why(tmp_path):
    hub = Hub(tmp_path, refuse=True)
    snapshot, problems = _ready(hub)
    assert len(find_cards(snapshot)) == 1  # Only the card Ubuntu had opened.
    assert len(problems) == 16
    text = render_explanation(explain(snapshot, problems))
    assert "Card readers: 17   Cards ready: 1" in text
    assert "Not authorized" in text


def test_a_reader_without_a_disk_is_reported(tmp_path):
    hub = Hub(tmp_path, dead=2)
    snapshot, _ = _ready(hub)
    lines = explain(snapshot)
    dead = [line for line in lines if "made no disk" in line.verdict]
    assert len(dead) == 2 and all(line.port.startswith("1-1.") for line in dead)
    assert sum(line.is_card for line in lines) == 15


def test_udevadm_may_only_settle():
    assert_read_only(["udevadm", "settle", "--timeout=10"])
    for argv in (["udevadm", "trigger"], ["udevadm", "settle"], ["udevadm", "control", "-R"]):
        with pytest.raises(ReadOnlyViolation):
            assert_read_only(argv)
