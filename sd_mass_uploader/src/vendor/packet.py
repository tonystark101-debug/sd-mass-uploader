"""A vendor platform packet becomes the session's details, and its card counts are checked.

The operator answers two things: how many cards they put in, and the packet's code. Everything
else (activity, city, vendor, site, recording days, declared hours, device) comes from the packet.
The cards' own contents still decide each card's pass; the packet's device is a cross-check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.core.settings import VendorPlatformSettings


def slug(text: str) -> str:
    """'Phone repair' -> 'phone-repair'; what the lake's folder names use."""
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def session_metadata(packet: dict, settings: VendorPlatformSettings) -> dict:
    """The PeakAI session details for a packet (src/peakai/session.py validate_session)."""
    city = slug(packet.get("city"))
    city = {slug(k): slug(v) for k, v in settings.city_aliases.items()}.get(city, city)
    details = {
        "activity": slug(packet.get("environment")),
        "city": city,
        "vendor_name": str(packet.get("vendor") or "").strip(),
        "site": str(packet.get("site") or "").strip(),
        "packet": packet["ref"],
        "packet_device": str(packet.get("device_kind") or "").strip(),
    }
    if packet.get("recorded_from"):
        details["recorded_from"] = packet["recorded_from"]
    if packet.get("recorded_to"):
        details["recorded_to"] = packet["recorded_to"]
    if packet.get("hours_declared"):
        details["video_hours"] = packet["hours_declared"]
    return {key: value for key, value in details.items() if value not in ("", None)}


def packet_cards(packet: dict) -> int | None:
    """What the packet should hold: the manager's count when received, else the vendor's."""
    received = packet.get("cards_received")
    return received if isinstance(received, int) else packet.get("cards_sent")


@dataclass
class CountCheck:
    detected: int  # Cards the station sees plugged in.
    counted: int  # Cards the operator says they put in.
    expected: int | None  # Cards the packet should hold.
    problems: list[str] = field(default_factory=list)  # Each one needs a reason to go on.

    @property
    def ok(self) -> bool:
        return not self.problems


def check_counts(detected: int, counted: int, packet: dict | None) -> CountCheck:
    check = CountCheck(detected, counted, packet_cards(packet) if packet else None)
    if counted != detected:
        missing = counted - detected
        check.problems.append(
            f"You put in {counted} card(s) but the station sees {detected}. "
            + (f"{missing} card(s) are not showing: check each reader's light and push the "
               "card in again." if missing > 0 else
               f"{-missing} more than you counted are plugged in: is a USB stick or another "
               "packet's card in?"))
    if check.expected is not None and counted != check.expected:
        who = "the manager counted" if packet.get("cards_received") is not None \
            else "the vendor sent"
        check.problems.append(f"The packet says {who} {check.expected} card(s); you counted "
                              f"{counted}.")
    if packet and packet.get("uploaded"):
        up = packet["uploaded"]
        check.problems.append(f"This packet was already uploaded on {up.get('at')} "
                              f"({up.get('hours')} h, {up.get('station') or 'a station'}).")
    return check


def expected_devices(packet_device: str, settings: VendorPlatformSettings) -> list[str] | None:
    """Devices a card from this packet may hold; None when the type is not checked."""
    for kind, devices in settings.device_kinds.items():
        if kind.strip().lower() == (packet_device or "").strip().lower():
            return list(devices)
    return None
