"""Get every plugged-in card ready before the terminal commands list them (Linux).

The always-on station mounts new cards by itself on every poll. The terminal commands (`check`,
`scan`, `upload`) used to take one snapshot and list only what Ubuntu had already opened, so
cards Ubuntu did not open on its own (readers behind a hub, or a station run from a terminal
with no desktop session) were silently missing, and so were readers still starting up: a hub of
16 readers takes several seconds to appear. This waits for the readers to settle, opens new
cards read-only exactly as the station does, and explains every reader that is not a card.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from src.core.enums import DeviceStatus
from src.core.exceptions import UploaderError
from src.core.models import InventorySnapshot
from src.core.settings import StationSettings
from src.device_manager.base import PlatformAdapter
from src.device_manager.detector import detect
from src.inventory.inventory_service import EXTERNAL_BUSES, find_cards
from src.platform.process import CommandRunner, SubprocessRunner
from src.station.sources import ReadOnlyMounter, SourceIssue

SETTLE_COMMAND = ["udevadm", "settle", "--timeout=10"]


def settle(runner: CommandRunner) -> None:
    """Wait until the kernel has finished announcing new devices. Best effort."""
    try:
        runner.run(SETTLE_COMMAND, timeout=15)
    except UploaderError:
        pass


def _signature(snapshot: InventorySnapshot) -> tuple:
    external = sorted(device.storage_id for device in snapshot.storage_devices
                      if (device.bus_type or "").upper() in EXTERNAL_BUSES)
    return tuple(external), tuple(sorted(card.card_id for card in find_cards(snapshot)))


def _starting(snapshot: InventorySnapshot) -> int:
    """Readers USB already shows but Linux has not yet made a disk for (still starting up)."""
    with_disk = {device.usb_topology_id for device in snapshot.storage_devices}
    return sum(1 for item in snapshot.usb_devices
               if item.is_mass_storage and not item.is_hub and item.topology_id not in with_disk)


def ready_snapshot(adapter: PlatformAdapter, station: StationSettings,
                   runner: CommandRunner | None = None, wait_seconds: float = 20.0,
                   say: Callable[[str], None] = lambda _text: None,
                   clock: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], None] = time.sleep,
                   linux: bool | None = None,
                   ) -> tuple[InventorySnapshot, list[SourceIssue]]:
    """Detect, open new cards read-only, and repeat until nothing changes (at most
    `wait_seconds`). Returns the final snapshot and the cards that could not be opened."""
    runner = runner or SubprocessRunner()
    linux = os.name == "posix" if linux is None else linux
    mounter = ReadOnlyMounter(station, runner) if linux and station.auto_mount else None
    if linux:
        settle(runner)
    deadline = clock() + wait_seconds
    previous = None
    problems: dict[str, SourceIssue] = {}
    while True:
        snapshot = detect(adapter)
        attempted: list[str] = []
        if mounter is not None:
            attempted, issues = mounter.mount_new(snapshot)
            for issue in issues:
                problems[issue.subject or issue.message] = issue
            if attempted:
                say(f"Opening {len(attempted)} card(s) read-only…")
        signature = _signature(snapshot)
        starting = _starting(snapshot)
        if not attempted and signature == previous and not starting:
            break
        if clock() >= deadline:
            break
        if starting and not attempted:
            say(f"Waiting for {starting} card reader(s) to finish starting…")
        previous = None if attempted else signature
        sleep(1.0 if attempted else 2.0)
        if attempted and linux:
            settle(runner)
    still = {path for path, _fs in mounter.candidates(snapshot)} if mounter else set()
    return snapshot, [issue for key, issue in problems.items() if key in still]


# ------------------------------------------------------------------ why a reader is not a card

@dataclass
class ReaderLine:
    port: str  # USB port path, e.g. 2-1.4.3: the 3rd slot of a hub on port 4 of a hub on 1.
    name: str
    device: str  # /dev/sdc, or "" when Linux made no disk for the reader.
    verdict: str  # "card" or why not.

    @property
    def is_card(self) -> bool:
        return self.verdict == "card"


def explain(snapshot: InventorySnapshot, mount_problems: list[SourceIssue] = ()
            ) -> list[ReaderLine]:
    """One line per card reader and external disk: a card, or the reason it is not one."""
    cards = {card.mount_point for card in find_cards(snapshot)}
    usb = {device.topology_id: device for device in snapshot.usb_devices}
    readers = {reader.reader_id: reader for reader in snapshot.readers}
    failed = {issue.subject: issue.message for issue in mount_problems if issue.subject}
    volumes: dict[str | None, list] = {}
    for volume in snapshot.volumes:
        volumes.setdefault(volume.storage_id, []).append(volume)
    lines: list[ReaderLine] = []
    seen_topology: set[str] = set()
    for device in snapshot.storage_devices:
        if (device.bus_type or "").upper() not in EXTERNAL_BUSES:
            continue
        if device.status == DeviceStatus.DISCONNECTED:
            continue
        reader = readers.get(device.reader_id or "")
        topology = usb.get(device.usb_topology_id or "")
        if device.usb_topology_id:
            seen_topology.add(device.usb_topology_id)
        port = (topology.port_path if topology else None) or "?"
        name = (reader.model if reader else None) or device.model or "unnamed"
        path = device.platform_device_id or ""
        own = volumes.get(device.storage_id, [])
        verdict = _verdict(device, own, cards, failed)
        lines.append(ReaderLine(port, name, path, verdict))
    for item in snapshot.usb_devices:
        if item.is_hub or not item.is_mass_storage or item.topology_id in seen_topology:
            continue
        lines.append(ReaderLine(item.port_path or "?", item.product_name or "USB storage", "",
                                "seen on USB, but Linux made no disk for it: usually too little "
                                "power (use a powered hub) or a faulty reader; see "
                                "`sudo dmesg | tail -50`"))
    return sorted(lines, key=lambda line: line.port)


def _verdict(device, volumes, cards: set[str], failed: dict[str, str]) -> str:
    if any(volume.mount_point in cards for volume in volumes):
        return "card"
    if device.media_present is False:
        return "no card in this reader"
    for volume in volumes:
        path = (volume.raw or {}).get("path")
        if path in failed:
            return f"could not be opened: {failed[path]}"
    if not volumes:
        return "card has no readable file system (blank, or not FAT/exFAT)"
    mounted = [volume for volume in volumes if volume.is_mounted]
    if not mounted:
        filesystems = ", ".join(sorted({v.filesystem or "unknown" for v in volumes}))
        return f"not opened yet (file system {filesystems})"
    return "opened, but its files cannot be read: reseat the card or try another reader"


def render_explanation(lines: list[ReaderLine]) -> str:
    if not lines:
        return "No card readers or USB disks found. Is the hub plugged in and powered?"
    cards = sum(line.is_card for line in lines)
    text = [f"Card readers: {len(lines)}   Cards ready: {cards}"]
    for line in lines:
        mark = "OK " if line.is_card else "-- "
        device = f" {line.device}" if line.device else ""
        text.append(f"  {mark}port {line.port:<12} {line.name[:28]:<28}{device}: {line.verdict}")
    return "\n".join(text)
