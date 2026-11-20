"""Physical acceptance checks. Run only with real readers attached:

    python -m pytest tests/hardware -m hardware -s --override-ini addopts=""

Set EXPECT_READERS to the number of readers connected. These tests only read.
Passing mocks proves nothing about hardware; passing these on each OS does.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from src.core.enums import DeviceStatus
from src.core.settings import DeviceDetectionSettings, FileScanningSettings
from src.device_manager.detector import create_adapter, detect
from src.inventory.file_scanner import FileScanner, ScannedFile
from src.inventory.inventory_service import find_cards

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def snapshot():
    return detect(create_adapter(DeviceDetectionSettings()))


def test_expected_reader_count(snapshot):
    expected = os.environ.get("EXPECT_READERS")
    if not expected:
        pytest.skip("Set EXPECT_READERS to the number of connected readers")
    assert len(snapshot.readers) == int(expected), [r.model for r in snapshot.readers]


def test_hubs_are_separate_from_readers(snapshot):
    hubs = {d.topology_id for d in snapshot.usb_devices if d.is_hub}
    assert not hubs & {r.topology_id for r in snapshot.readers}


def test_every_reader_reports_a_link_speed_or_says_unknown(snapshot):
    usb = {d.topology_id: d for d in snapshot.usb_devices}
    for reader in snapshot.readers:
        link = usb[reader.topology_id]
        print(reader.model, reader.status, link.negotiated_speed_mbps, link.speed_source)
        if link.negotiated_speed_mbps is None:
            assert link.speed_source == "UNKNOWN"


def test_no_internal_disk_is_offered(snapshot):
    storage = {s.storage_id: s for s in snapshot.storage_devices}
    mounts = {c.mount_point for c in find_cards(snapshot)}
    for volume in snapshot.volumes:
        device = storage.get(volume.storage_id or "")
        if volume.mount_point in mounts:
            assert device is not None and device.bus_type in {"USB", "SD", "MMC"}


def _digest(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def test_scanning_a_real_card_changes_nothing(snapshot):
    cards = find_cards(snapshot)
    if not cards:
        pytest.skip("No card mounted")
    root = cards[0].mount_point
    events = FileScanner(FileScanningSettings()).walk(root)
    sample = [Path(root, e.relative_path) for e in events
              if isinstance(e, ScannedFile) and e.size_bytes < 50_000_000][:20]
    before = {path: _digest(path) for path in sample}
    list(FileScanner(FileScanningSettings()).walk(root))
    assert {path: _digest(path) for path in sample} == before


def test_empty_status_is_never_a_guess(snapshot):
    for storage in snapshot.storage_devices:
        if storage.status == DeviceStatus.EMPTY_READER:
            assert storage.media_present is False
