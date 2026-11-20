"""Where the station learns which cards are inserted: real devices, or simulated folders."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from src.core.enums import AccessStatus
from src.core.exceptions import UploaderError
from src.core.models import Card, InventorySnapshot
from src.core.settings import StationSettings
from src.database.repositories import InventoryRepository
from src.device_manager.base import PlatformAdapter
from src.device_manager.detector import detect
from src.inventory.inventory_service import EXTERNAL_BUSES, find_cards
from src.platform.process import CommandRunner, SubprocessRunner

logger = logging.getLogger(__name__)

SIMULATOR_EJECTED_SUFFIX = ".ejected"


@dataclass(frozen=True)
class SourceIssue:
    """A device-level problem. `code` is a problem-catalog code (src/advisor/catalog.py)."""

    code: str
    message: str
    subject: str | None = None  # The device or folder concerned.


@dataclass
class PollResult:
    cards: list[Card]
    issues: list[SourceIssue] = field(default_factory=list)
    mounting: list[str] = field(default_factory=list)  # Devices being mounted right now.


class CardSource(Protocol):
    def poll(self) -> PollResult: ...


class SimulatorSource:
    """Each sub-folder of `root` is a card. Rename it to NAME.ejected to "remove" it.

    For developing and demonstrating the station without readers. The identity comes from the
    folder name, so a card renamed back is recognised as the same card.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def poll(self) -> PollResult:
        if not self._root.is_dir():
            return PollResult([], [SourceIssue("W-SIMULATOR-MISSING",
                                               f"Simulator folder not found: {self._root}",
                                               str(self._root))])
        cards = []
        for entry in sorted(self._root.iterdir(), key=lambda item: item.name):
            if not entry.is_dir() or entry.name.endswith(SIMULATOR_EJECTED_SUFFIX):
                continue
            if entry.name.startswith("."):
                continue
            cards.append(simulated_card(entry))
        return PollResult(cards)


def simulated_card(folder: Path) -> Card:
    name = folder.name
    return Card(
        card_id=f"sim-{name}",
        mount_point=str(folder.resolve()),
        fingerprint=f"sim:{name}",
        volume_label=name,
        filesystem="simulated",
        reader_model="Simulated reader",
    )


class ReadOnlyMounter:
    """Mounts new card partitions read-only through udisks2. It never unmounts or repairs."""

    _RETRY_SECONDS = 60.0
    _MAX_ATTEMPTS = 3

    def __init__(self, settings: StationSettings, runner: CommandRunner | None = None,
                 clock=time.monotonic) -> None:
        self._options = settings.mount_options
        self._filesystems = {name.lower() for name in settings.mount_filesystems}
        self._runner = runner or SubprocessRunner()
        self._clock = clock
        self._attempts: dict[str, tuple[int, float]] = {}

    def candidates(self, snapshot: InventorySnapshot) -> list[tuple[str, str | None]]:
        """(device path, filesystem) of unmounted volumes on removable external buses."""
        storage = {device.storage_id: device for device in snapshot.storage_devices}
        found = []
        for volume in snapshot.volumes:
            if volume.is_mounted or volume.access_status != AccessStatus.NOT_MOUNTED:
                continue
            device = storage.get(volume.storage_id or "")
            if device is None or (device.bus_type or "").upper() not in EXTERNAL_BUSES:
                continue
            if device.media_present is False:
                continue
            path = (volume.raw or {}).get("path")
            if path:
                found.append((path, volume.filesystem))
        return found

    def build_command(self, device_path: str) -> list[str]:
        return ["udisksctl", "mount", "--block-device", device_path, "--options", self._options,
                "--no-user-interaction"]

    def mount_new(self, snapshot: InventorySnapshot) -> tuple[list[str], list[SourceIssue]]:
        """Try to mount each candidate. Returns (devices attempted, problems)."""
        attempted: list[str] = []
        problems: list[SourceIssue] = []
        now = self._clock()
        for path, filesystem in self.candidates(snapshot):
            if (filesystem or "").lower() not in self._filesystems:
                problems.append(SourceIssue(
                    "W-FS-UNSUPPORTED",
                    f"{path}: file system {filesystem or 'unknown'} is not opened automatically "
                    f"(allowed: {', '.join(sorted(self._filesystems))})", path))
                continue
            count, last = self._attempts.get(path, (0, -1e9))
            if count >= self._MAX_ATTEMPTS:
                problems.append(SourceIssue(
                    "E-MOUNT-FAILED", f"{path}: could not be opened read-only after {count} tries",
                    path))
                continue
            if now - last < self._RETRY_SECONDS and count:
                continue
            self._attempts[path] = (count + 1, now)
            attempted.append(path)
            try:
                result = self._runner.run(self.build_command(path), timeout=30)
            except UploaderError as exc:
                problems.append(SourceIssue("E-MOUNT-FAILED", f"{path}: {exc}", path))
                continue
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip().splitlines()
                problems.append(SourceIssue(
                    "E-MOUNT-FAILED", f"{path}: {detail[-1] if detail else 'no output'}", path))
            else:
                logger.info("Mounted %s read-only: %s", path, result.stdout.strip())
                self._attempts.pop(path, None)
        return attempted, problems


class DeviceSource:
    """Real readers, through the platform adapter. Mounts new cards when allowed."""

    def __init__(self, adapter: PlatformAdapter, inventory: InventoryRepository | None,
                 mounter: ReadOnlyMounter | None) -> None:
        self._adapter = adapter
        self._inventory = inventory
        self._mounter = mounter
        self._last_ids: set[str] | None = None

    def poll(self) -> PollResult:
        snapshot = detect(self._adapter)
        issues = [SourceIssue("W-DETECTION", f"{issue.scope}: {issue.message}", issue.scope)
                  for issue in snapshot.issues if issue.severity != "INFO"]
        mounting: list[str] = []
        if self._mounter is not None:
            mounting, problems = self._mounter.mount_new(snapshot)
            issues.extend(problems)
        cards = find_cards(snapshot)
        ids = {card.card_id for card in cards}
        if ids != self._last_ids and self._inventory is not None:
            self._inventory.save(snapshot)  # Only on change, so the history stays small.
        self._last_ids = ids
        return PollResult(cards, issues, mounting)


class UdevWaker:
    """Wakes the station as soon as a block device appears or disappears (Linux, pyudev)."""

    def __init__(self, wake: threading.Event) -> None:
        self._wake = wake
        self._observer = None

    def start(self) -> bool:
        try:
            import pyudev  # Linux only; optional.
        except ImportError:
            logger.info("pyudev not available; relying on polling")
            return False
        try:
            context = pyudev.Context()
            monitor = pyudev.Monitor.from_netlink(context)
            monitor.filter_by(subsystem="block")
            self._observer = pyudev.MonitorObserver(monitor, callback=lambda _: self._wake.set(),
                                                    name="udev-watch")
            self._observer.start()
        except Exception as exc:  # noqa: BLE001 - polling still works without events
            logger.warning("udev monitoring unavailable: %s", exc)
            return False
        return True

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()


def create_source(settings: StationSettings, adapter_factory, inventory: InventoryRepository,
                  base_dir: Path) -> CardSource:
    if settings.simulator_dir:
        root = Path(settings.simulator_dir)
        return SimulatorSource(root if root.is_absolute() else base_dir / root)
    mounter = ReadOnlyMounter(settings) if settings.auto_mount and os.name == "posix" else None
    return DeviceSource(adapter_factory(), inventory, mounter)
