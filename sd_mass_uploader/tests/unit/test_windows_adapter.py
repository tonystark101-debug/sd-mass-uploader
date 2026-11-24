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
    # No drive letter, but formatted and on a USB reader: read through its volume path.
    assert by_serial["SERIAL_C"] == DeviceStatus.ACTIVE
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


def test_card_without_a_drive_letter_is_read_through_its_volume_path(snapshot):
    volume = next(v for v in snapshot.volumes if v.volume_label == "CAM_C")
    assert volume.is_mounted is True
    assert volume.mount_point == "\\\\?\\Volume{card-c}\\"
    assert volume.drive_letter is None


def test_internal_or_unformatted_volume_without_letter_is_never_opened():
    payload = copy.deepcopy(PAYLOAD)
    payload["partitions"] += [
        {"DiskNumber": 0, "PartitionNumber": 4, "Size": 1_000_000, "DriveLetter": "",
         "AccessPaths": ["\\\\?\\Volume{recovery}\\"]},
        {"DiskNumber": 3, "PartitionNumber": 2, "Size": 1_000_000, "DriveLetter": "",
         "AccessPaths": ["\\\\?\\Volume{raw-part}\\"]},
    ]
    payload["volumes"] += [
        {"DeviceID": "\\\\?\\Volume{recovery}\\", "DriveLetter": None, "Label": "Recovery",
         "FileSystem": "NTFS", "Capacity": 1_000_000, "FreeSpace": 1, "SerialNumber": 7,
         "DriveType": 3, "Name": "\\\\?\\Volume{recovery}\\"},
        {"DeviceID": "\\\\?\\Volume{raw-part}\\", "DriveLetter": None, "Label": None,
         "FileSystem": None, "Capacity": None, "FreeSpace": None, "SerialNumber": None,
         "DriveType": 2, "Name": "\\\\?\\Volume{raw-part}\\"},
    ]
    raw = normalize_windows_inventory(payload, HUBS, PORTS, NOW)
    by_id = {v.raw["device_id"]: v for v in raw.volumes}
    assert by_id["\\\\?\\Volume{recovery}\\"].is_mounted is False  # Internal disk.
    assert by_id["\\\\?\\Volume{raw-part}\\"].is_mounted is False  # No file system.

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


# --- Cards behind hubs: the device tree is read natively, never one slow query per device ---

def _without_tree(payload):
    """The inventory script's output: since the hub fix it carries no per-device properties."""
    payload = copy.deepcopy(payload)
    tree = payload.pop("pnp_properties")
    return payload, tree


def test_collect_reads_the_device_tree_natively():
    payload, tree = _without_tree(PAYLOAD)
    asked = []

    def tree_reader(ids):
        asked.append(ids)
        return tree

    runner = _Runner(CommandResult(0, json.dumps(payload), ""))
    adapter = WindowsAdapter(runner, hub_reader=lambda: (HUBS, PORTS, []), clock=lambda: NOW,
                             tree_reader=tree_reader)
    snapshot = correlate(adapter.collect(), NOW)
    assert len(runner.calls) == 1  # One PowerShell call, however many readers.
    assert "USBSTOR\\DISK&VEN_GENERIC&PROD_STORAGE\\A&0" in asked[0]
    linked = [d for d in snapshot.storage_devices if d.reader_id]
    assert len(linked) == 3


class _Sequence:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def run(self, argv, timeout=60):
        self.calls.append((argv, timeout))
        return self.results.pop(0)


def test_powershell_is_the_fallback_when_the_tree_cannot_be_read():
    payload, tree = _without_tree(PAYLOAD)

    def broken(_ids):
        raise OSError("cfgmgr32 missing")

    runner = _Sequence(CommandResult(0, json.dumps(payload), ""),
                       CommandResult(0, json.dumps({"errors": [], "pnp_properties": tree}), ""))
    adapter = WindowsAdapter(runner, hub_reader=lambda: (HUBS, PORTS, []), clock=lambda: NOW,
                             tree_reader=broken)
    snapshot = correlate(adapter.collect(), NOW)
    assert len(runner.calls) == 2
    assert runner.calls[1][1] >= 60 + 4 * 9  # The time limit grows with the device count.
    assert len([d for d in snapshot.storage_devices if d.reader_id]) == 3


def _two_hubs_of_sixteen():
    """A laptop with two powered hubs, each holding 16 single-slot readers, each with a card.
    Windows has run out of drive letters after 20 cards."""
    from src.device_manager.usb_topology import WindowsPort

    guid = "#{f18a0e88-c30c-11d0-8815-00a0c906bed8}"
    root = f"\\\\?\\USB#ROOT_HUB30#5&ROOT&0&0{guid}"
    hubs, ports = [root], []
    payload = {"errors": [], "usb_pnp": [
        {"DeviceID": "USB\\ROOT_HUB30\\5&ROOT&0&0", "Name": "USB Root Hub (USB 3.0)",
         "Service": "USBHUB3", "CompatibleID": None, "Status": "OK"}],
        "disk_drives": [], "disks": [], "partitions": [], "volumes": []}
    tree = []
    index = 0
    for h in (1, 2):
        hub = f"\\\\?\\USB#VID_2109&PID_0817#6&HUB{h}&0&{h}{guid}"
        hubs.append(hub)
        ports.append(WindowsPort(root, h, "2109", "0817", 0x0320, 3, True, 0x3,
                                 f"{{hub}}\\000{h}", hub))
        hub_id = f"USB\\VID_2109&PID_0817\\6&HUB{h}&0&{h}"
        payload["usb_pnp"].append({"DeviceID": hub_id, "Name": "Generic USB Hub",
                                   "Service": "USBHUB3", "CompatibleID": ["USB\\Class_09"]})
        tree.append({"InstanceId": hub_id, "KeyName": "DEVPKEY_Device_Driver",
                     "Data": f"{{hub}}\\000{h}"})
        for slot in range(1, 17):
            index += 1
            serial = f"RDR{index:02d}"
            reader = f"USB\\VID_05E3&PID_0749\\{serial}"
            driver = f"{{usb}}\\{index:04d}"
            disk = f"USBSTOR\\DISK&VEN_GENERIC&PROD_SD_READER\\{serial}&0"
            ports.append(WindowsPort(hub, slot, "05e3", "0749", 0x0300, 3, False, 0x3, driver))
            payload["usb_pnp"].append({"DeviceID": reader, "Name": "USB Mass Storage Device",
                                       "Service": "USBSTOR", "CompatibleID": ["USB\\Class_08"]})
            tree += [{"InstanceId": reader, "KeyName": "DEVPKEY_Device_Driver", "Data": driver},
                     {"InstanceId": disk, "KeyName": "DEVPKEY_Device_Parent", "Data": reader}]
            payload["disk_drives"].append({
                "Index": index, "Model": "Generic SD Card Reader USB Device",
                "InterfaceType": "USB", "MediaType": "Removable Media", "MediaLoaded": True,
                "Size": 64_000_000_000, "PNPDeviceID": disk, "Status": "OK"})
            payload["disks"].append({"Number": index, "Size": 64_000_000_000, "BusType": "USB",
                                     "OperationalStatus": "Online"})
            letter = chr(ord("E") + index - 1) if index <= 20 else ""
            volume = f"\\\\?\\Volume{{card-{index:02d}}}\\"
            payload["partitions"].append({
                "DiskNumber": index, "PartitionNumber": 1, "DriveLetter": letter,
                "AccessPaths": ([f"{letter}:\\"] if letter else []) + [volume]})
            payload["volumes"].append({
                "DeviceID": volume, "DriveLetter": f"{letter}:" if letter else None,
                "Label": f"CARD{index:02d}", "FileSystem": "exFAT", "Capacity": 63_000_000_000,
                "FreeSpace": 1_000_000, "SerialNumber": 0x1000 + index, "DriveType": 2,
                "Name": f"{letter}:\\" if letter else volume})
    return payload, hubs, ports, tree


def test_two_hubs_of_sixteen_readers_give_thirty_two_cards():
    from src.inventory.inventory_service import find_cards

    payload, hubs, ports, tree = _two_hubs_of_sixteen()
    adapter = WindowsAdapter(_Runner(CommandResult(0, json.dumps(payload), "")),
                             hub_reader=lambda: (hubs, ports, []), clock=lambda: NOW,
                             tree_reader=lambda _ids: tree)
    raw = adapter.collect()
    for volume in raw.volumes:
        if volume.is_mounted:
            volume.access_status = AccessStatus.OK
    snapshot = correlate(raw, NOW)
    assert len([d for d in snapshot.usb_devices if d.is_hub]) == 3  # Root + both hubs.
    assert len(snapshot.readers) == 32
    deep = [r for r in snapshot.readers if r.topology_path.count("/") == 2]
    assert len(deep) == 32  # Every reader is seen behind its hub: root/hub/slot.
    cards = find_cards(snapshot)
    assert len(cards) == 32
    lettered = [c for c in cards if c.mount_point.endswith(":\\")]
    assert len(lettered) == 20  # The other 12 are read through their volume paths.
    assert all(c.negotiated_speed_mbps == 5000.0 for c in cards)
