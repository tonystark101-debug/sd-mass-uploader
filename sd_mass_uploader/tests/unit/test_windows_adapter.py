import copy
import json
import struct
import sys

import pytest

from src.core.enums import AccessStatus, DeviceStatus, ReaderKind, SpeedConfidence
from src.device_manager.correlation import correlate
from src.device_manager.usb_topology import (
    hub_instance_id,
    parse_connection_info,
    parse_name_buffer,
    volume_disk_number,
    windows_speed_mbps,
)
from src.device_manager.windows import (
    INVENTORY_SCRIPT,
    SPEED_SOURCE,
    WindowsAdapter,
    normalize_windows_inventory,
)
from src.platform.process import CommandResult
from tests.fixtures.windows_payload import HUBS, PAYLOAD, PORTS

NOW = "2026-11-06T00:00:00+00:00"


def inventory(payload=None, hubs=HUBS, ports=PORTS):
    raw = normalize_windows_inventory(payload or copy.deepcopy(PAYLOAD), hubs, ports, NOW)
    for volume in raw.volumes:
        if volume.is_mounted:
            volume.access_status = AccessStatus.OK
    return raw


@pytest.fixture
def snapshot():
    return correlate(inventory(), NOW)


def test_hubs_are_not_counted_as_readers(snapshot):
    hubs = [d for d in snapshot.usb_devices if d.is_hub]
    assert len(hubs) == 2
    assert len(snapshot.readers) == 4
    assert not {r.topology_id for r in snapshot.readers} & {h.topology_id for h in hubs}


def test_topology_is_nested_with_port_paths(snapshot):
    by_id = {d.topology_id: d for d in snapshot.usb_devices}
    leaf = next(d for d in snapshot.usb_devices if d.serial_number == "SERIAL_D")
    assert leaf.port_path == "root:5&AAA&0&0/1/4"
    parent = by_id[leaf.parent_topology_id]
    assert parent.product_name == "Generic SuperSpeed USB Hub"
    assert by_id[parent.parent_topology_id].product_name == "USB Root Hub (USB 3.0)"


def test_reader_status_for_each_physical_case(snapshot):
    by_serial = {r.serial_number: r.status for r in snapshot.readers}
    assert by_serial["SERIAL_A"] == DeviceStatus.ACTIVE
    assert by_serial["SERIAL_B"] == DeviceStatus.EMPTY_READER
    assert by_serial["SERIAL_C"] == DeviceStatus.UNMOUNTED_STORAGE
    assert by_serial["SERIAL_D"] == DeviceStatus.READER_PRESENT_SLOT_UNKNOWN


def test_usb3_reader_negotiating_usb2_is_reported_honestly(snapshot):
    slow = next(d for d in snapshot.usb_devices if d.serial_number == "SERIAL_C")
    assert slow.advertised_capability == "USB 3.0"
    assert slow.negotiated_speed_mbps == 480
    assert slow.speed_source == SPEED_SOURCE
    assert slow.speed_confidence == SpeedConfidence.HIGH


def test_generic_names_do_not_claim_card_reader(snapshot):
    assert all(r.kind == ReaderKind.USB_STORAGE_UNCONFIRMED for r in snapshot.readers)


def test_named_card_reader_is_recognized():
    payload = copy.deepcopy(PAYLOAD)
    payload["usb_pnp"][2]["Name"] = "Realtek USB3.0 Card Reader"
    reader = next(r for r in correlate(inventory(payload), NOW).readers
                  if r.serial_number == "SERIAL_A")
    assert reader.kind == ReaderKind.CARD_READER


def test_volumes_link_through_access_paths_and_serial_format(snapshot):
    volume = next(v for v in snapshot.volumes if v.volume_label == "CAM_A")
    storage = next(s for s in snapshot.storage_devices if s.storage_id == volume.storage_id)
    assert storage.bus_type == "USB"
    assert storage.reader_id is not None
    assert volume.drive_letter == "F"
    assert volume.fs_serial == "1A2B-3C4D"


def test_card_without_partition_table_links_through_driver_query():
    payload = copy.deepcopy(PAYLOAD)
    payload["partitions"] = [p for p in payload["partitions"] if p["DiskNumber"] != 1]
    lookup = {"F": (1, 0)}.get
    raw = normalize_windows_inventory(payload, HUBS, PORTS, NOW, lookup)
    volume = next(v for v in raw.volumes if v.volume_label == "CAM_A")
    assert volume.storage_id is not None
    assert next(s for s in raw.storage_devices if s.storage_id == volume.storage_id).bus_type == \
        "USB"


def test_unresolved_volume_stays_unlinked():
    payload = copy.deepcopy(PAYLOAD)
    payload["partitions"] = [p for p in payload["partitions"] if p["DiskNumber"] != 1]
    raw = normalize_windows_inventory(payload, HUBS, PORTS, NOW)
    assert next(v for v in raw.volumes if v.volume_label == "CAM_A").storage_id is None


def test_volume_without_letter_is_unmounted(snapshot):
    volume = next(v for v in snapshot.volumes if v.volume_label == "CAM_C")
    assert volume.is_mounted is False
    assert volume.access_status == AccessStatus.NOT_MOUNTED


def test_internal_nvme_is_never_a_reader(snapshot):
    nvme = next(s for s in snapshot.storage_devices if s.bus_type == "NVME")
    assert nvme.reader_id is None and nvme.removable is False


def test_unavailable_topology_leaves_speed_unknown_and_no_links():
    snapshot = correlate(inventory(hubs=[], ports=[]), NOW)
    assert snapshot.readers == []
    assert snapshot.usb_devices == []
    assert all(s.reader_id is None for s in snapshot.storage_devices)


def test_composite_reader_found_through_interface_child():
    payload = copy.deepcopy(PAYLOAD)
    payload["usb_pnp"][2].update(Service="usbccgp", CompatibleID=["USB\\COMPOSITE"])
    interface = "USB\\VID_0BDA&PID_0307&MI_00\\7&1&0&0000"
    payload["usb_pnp"].append({"DeviceID": interface, "Name": "USB Mass Storage Device",
                               "Service": "USBSTOR", "CompatibleID": ["USB\\Class_08"],
                               "Manufacturer": None, "Status": "OK"})
    payload["pnp_properties"].append({"InstanceId": interface, "KeyName": "DEVPKEY_Device_Parent",
                                      "Data": "USB\\VID_0BDA&PID_0307\\SERIAL_A"})
    device = next(d for d in inventory(payload).usb_devices if d.serial_number == "SERIAL_A")
    assert device.is_mass_storage is True


def test_generated_instance_id_is_not_a_serial():
    payload = copy.deepcopy(PAYLOAD)
    device = next(d for d in inventory(payload).usb_devices if d.vendor_id == "05e3")
    assert device.serial_number is None


class _Runner:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def run(self, argv, timeout=60):
        self.calls.append(argv)
        if self.error:
            raise self.error
        return self.result


def test_adapter_records_powershell_failure_without_crashing():
    adapter = WindowsAdapter(_Runner(CommandResult(1, "", "Access denied")),
                             hub_reader=lambda: ([], [], []), clock=lambda: NOW)
    raw = adapter.collect()
    assert raw.storage_devices == []
    assert raw.issues[0].severity == "ERROR" and "Access denied" in raw.issues[0].message


def test_adapter_records_bad_json():
    adapter = WindowsAdapter(_Runner(CommandResult(0, "not json", "")),
                             hub_reader=lambda: ([], [], []), clock=lambda: NOW)
    assert "Bad JSON" in adapter.collect().issues[0].message


def test_one_section_failure_is_a_per_section_issue():
    payload = {**copy.deepcopy(PAYLOAD),
               "errors": [{"section": "disks", "message": "Storage module missing"}]}
    adapter = WindowsAdapter(_Runner(CommandResult(0, json.dumps(payload), "")),
                             hub_reader=lambda: (HUBS, PORTS, []), clock=lambda: NOW)
    raw = adapter.collect()
    assert any("Storage module" in issue.message for issue in raw.issues)
    assert raw.storage_devices


def test_hub_failure_keeps_inventory():
    def broken():
        raise OSError("cfgmgr32 failed")

    adapter = WindowsAdapter(_Runner(CommandResult(0, json.dumps(PAYLOAD), "")),
                             hub_reader=broken, clock=lambda: NOW)
    raw = adapter.collect()
    assert raw.storage_devices
    assert any("speed unavailable" in i.message.lower() for i in raw.issues)


# --- IOCTL decoding ---

def _connection(status=1, speed=3, is_hub=0, bcd=0x0300, vid=0x0BDA, pid=0x0307):
    descriptor = struct.pack("<BBHBBBBHHHBBBB", 18, 1, bcd, 0, 0, 0, 9, vid, pid, 0x100, 1, 2, 3, 1)
    return struct.pack("<I18sBBBHII", 4, descriptor, 1, speed, is_hub, 5, 2, status) + b"\0" * 100


def test_parse_connection_info():
    assert parse_connection_info(_connection()) == {
        "port": 4, "bcd_usb": 0x0300, "vendor_id": "0bda", "product_id": "0307",
        "speed_code": 3, "is_hub": False,
    }


def test_empty_port_is_none():
    assert parse_connection_info(_connection(status=0)) is None


@pytest.mark.parametrize("code,flags,expected", [
    (0, None, 1.5), (1, 0, 12), (2, 0, 480), (3, None, 5000),
    (2, 0x1, 5000), (3, 0x5, 10000), (9, None, None),
])
def test_speed_mapping(code, flags, expected):
    assert windows_speed_mbps(code, flags) == expected


def test_parse_name_buffer():
    text = "{usb}\\0001".encode("utf-16-le") + b"\0\0"
    buffer = struct.pack("<II", 1, 8 + len(text)) + text + b"\0" * 20
    assert parse_name_buffer(buffer) == "{usb}\\0001"


def test_hub_instance_id():
    assert hub_instance_id(HUBS[0]) == "USB\\ROOT_HUB30\\5&AAA&0&0"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_real_driver_query_for_system_drive():
    disk, partition = volume_disk_number("C")
    assert disk >= 0 and partition >= 1
    assert volume_disk_number("Q") is None or volume_disk_number("Q")[0] >= 0


def test_inventory_script_passes_safety_gate():
    from src.platform.process import run_powershell

    runner = _Runner(CommandResult(0, "{}", ""))
    run_powershell(runner, INVENTORY_SCRIPT)
    assert runner.calls[0][0] == "powershell.exe"
    assert "-EncodedCommand" in runner.calls[0]
