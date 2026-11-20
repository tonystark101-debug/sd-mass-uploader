"""Select the platform adapter and produce a correlated snapshot."""

from __future__ import annotations

import sys

from src.core.exceptions import DeviceDetectionError
from src.core.models import InventorySnapshot
from src.core.settings import DeviceDetectionSettings
from src.core.timeutil import utc_now
from src.device_manager.base import PlatformAdapter
from src.device_manager.correlation import correlate
from src.inventory.volume_scanner import probe_volumes


def create_adapter(settings: DeviceDetectionSettings) -> PlatformAdapter:
    if sys.platform == "win32":
        from src.device_manager.windows import WindowsAdapter

        return WindowsAdapter(detect_topology=settings.detect_usb_topology)
    if sys.platform.startswith("linux"):
        from src.device_manager.linux import LinuxAdapter

        return LinuxAdapter(detect_topology=settings.detect_usb_topology)
    raise DeviceDetectionError(f"Unsupported platform: {sys.platform}")


def detect(adapter: PlatformAdapter) -> InventorySnapshot:
    """Collect, test real access to each volume, then decide statuses."""
    raw = adapter.collect()
    probe_volumes(raw.volumes)
    return correlate(raw, utc_now())
