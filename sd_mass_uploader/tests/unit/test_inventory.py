import dataclasses

from src.core.enums import AccessStatus, DeviceStatus, ReaderKind
from src.core.models import InventorySnapshot, Reader, StorageDevice, UsbDevice, Volume
from src.database.db import Database
from src.database.repositories import InventoryRepository
from src.device_manager.correlation import mark_disconnected, reader_status, storage_status
from src.inventory.inventory_service import find_cards, refresh_inventory
from src.inventory.volume_scanner import probe_access, probe_volumes

NOW = "2026-11-06T00:00:00+00:00"


def snapshot_with(volumes, storage, readers=(), usb=()):
    return InventorySnapshot("s", NOW, "test", usb_devices=list(usb), readers=list(readers),
                             storage_devices=list(storage), volumes=list(volumes))


def card_setup(serial="1A2B-3C4D", kind=ReaderKind.CARD_READER, bus="USB", status=None):
    storage = StorageDevice("sto1", reader_id="r1", bus_type=bus, serial_number=None,
                            status=status or DeviceStatus.ACTIVE)
    volume = Volume("v1", "sto1", mount_point="F:\\", is_mounted=True, fs_serial=serial,
                    access_status=AccessStatus.OK)
    reader = Reader("r1", kind=kind, model="Card Reader", topology_id="u1")
    usb = UsbDevice("u1", negotiated_speed_mbps=5000)
    return volume, storage, reader, usb


def test_confirmed_card():
    volume, storage, reader, usb = card_setup()
    card = find_cards(snapshot_with([volume], [storage], [reader], [usb]))[0]
    assert card.card_id == "card-1A2B-3C4D"
    assert card.fingerprint == "1A2B-3C4D"
    assert card.negotiated_speed_mbps == 5000
    assert not card.is_ambiguous


def test_card_id_never_uses_drive_letter():
    volume, storage, reader, usb = card_setup()
    first = find_cards(snapshot_with([volume], [storage], [reader], [usb]))[0].card_id
    volume.mount_point = "G:\\"
    second = find_cards(snapshot_with([volume], [storage], [reader], [usb]))[0].card_id
    assert first == second


def test_internal_disk_is_never_a_card():
    volume, storage, reader, usb = card_setup(bus="NVME")
    assert find_cards(snapshot_with([volume], [storage], [reader], [usb])) == []


def test_volume_with_unproven_owner_is_never_a_card():
    volume = Volume("v1", None, mount_point="C:\\", is_mounted=True,
                    access_status=AccessStatus.OK)
    assert find_cards(snapshot_with([volume], [])) == []


def test_usb_drive_needs_confirmation():
    volume, storage, reader, usb = card_setup(kind=ReaderKind.USB_STORAGE_UNCONFIRMED)
    card = find_cards(snapshot_with([volume], [storage], [reader], [usb]))[0]
    assert card.is_ambiguous and "USB drive" in card.ambiguity[0]


def test_cloned_cards_stay_distinct_and_need_confirmation():
    volume, storage, reader, usb = card_setup()
    twin = dataclasses.replace(volume, volume_id="v2", storage_id="sto2", mount_point="G:\\")
    other = dataclasses.replace(storage, storage_id="sto2")
    cards = find_cards(snapshot_with([volume, twin], [storage, other], [reader], [usb]))
    assert cards[0].card_id != cards[1].card_id
    assert cards[1].is_ambiguous


def test_no_serial_means_ambiguous():
    volume, storage, reader, usb = card_setup(serial=None)
    card = find_cards(snapshot_with([volume], [storage], [reader], [usb]))[0]
    assert card.fingerprint is None and card.is_ambiguous


def test_inaccessible_storage_is_not_offered():
    volume, storage, reader, usb = card_setup(status=DeviceStatus.INACCESSIBLE)
    assert find_cards(snapshot_with([volume], [storage], [reader], [usb])) == []


def test_status_rules():
    device = StorageDevice("s")
    mounted = Volume("v", is_mounted=True, mount_point="X", access_status=AccessStatus.DENIED)
    assert storage_status(device, [mounted]) == DeviceStatus.INACCESSIBLE
    mounted.access_status = AccessStatus.ERROR
    assert storage_status(device, [mounted]) == DeviceStatus.ERROR
    assert storage_status(device, []) == DeviceStatus.UNKNOWN
    assert storage_status(StorageDevice("s", capacity_bytes=5), []) == \
        DeviceStatus.UNMOUNTED_STORAGE
    assert storage_status(StorageDevice("s", media_present=False), []) == \
        DeviceStatus.EMPTY_READER


def test_multi_lun_reader_shows_active_slot():
    assert reader_status([DeviceStatus.EMPTY_READER, DeviceStatus.ACTIVE]) == DeviceStatus.ACTIVE
    assert reader_status([DeviceStatus.EMPTY_READER] * 2) == DeviceStatus.EMPTY_READER
    assert reader_status([]) == DeviceStatus.READER_PRESENT_SLOT_UNKNOWN


def test_probe_reports_missing_mount_as_error(tmp_path):
    assert probe_access(str(tmp_path)) == AccessStatus.OK
    assert probe_access(str(tmp_path / "gone")) == AccessStatus.ERROR


def test_probe_hung_card_times_out(monkeypatch):
    import threading

    from src.inventory import volume_scanner

    release = threading.Event()
    monkeypatch.setattr(volume_scanner, "probe_access", lambda _: release.wait(5))
    volume = Volume("v", mount_point="X", is_mounted=True)
    probe_volumes([volume], timeout=0.05)
    release.set()
    assert volume.access_status == AccessStatus.ERROR


class _Adapter:
    def __init__(self, raw):
        self.raw = raw

    def collect(self):
        return dataclasses.replace(self.raw)


def test_removed_device_becomes_disconnected_then_drops_off():
    from src.core.models import RawInventory

    db = Database(":memory:")
    repo = InventoryRepository(db)
    usb = UsbDevice("u1", is_mass_storage=True, port_path="1", vendor_id="0bda")
    present = RawInventory("test", [usb], [StorageDevice("sto", usb_topology_id="u1")], [])
    refresh_inventory(_Adapter(present), repo)
    gone = refresh_inventory(_Adapter(RawInventory("test")), repo)
    assert [r.status for r in gone.readers] == [DeviceStatus.DISCONNECTED]
    assert [s.status for s in gone.storage_devices] == [DeviceStatus.DISCONNECTED]
    later = refresh_inventory(_Adapter(RawInventory("test")), repo)
    assert later.readers == [] and later.storage_devices == []


def test_snapshot_round_trip():
    db = Database(":memory:")
    repo = InventoryRepository(db)
    volume, storage, reader, usb = card_setup()
    original = snapshot_with([volume], [storage], [reader], [usb])
    repo.save(original)
    loaded = repo.latest()
    assert loaded.readers[0].kind == ReaderKind.CARD_READER
    assert loaded.volumes[0].access_status == AccessStatus.OK
    assert loaded.storage_devices[0].status == DeviceStatus.ACTIVE
    assert loaded.usb_devices[0].negotiated_speed_mbps == 5000


def test_mark_disconnected_with_no_history_is_harmless():
    current = snapshot_with([], [])
    mark_disconnected(current, None)
    assert current.readers == []
