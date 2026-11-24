"""A vendor platform packet becomes the session's details, and its card counts are checked.

The operator types one thing: the code written on the packet. Everything else (activity, city,
vendor, site, recording days, declared hours, device) comes from the vendor platform, and the
cards the station sees are checked against the packet's own count.
The cards' own contents still decide each card's pass; the packet's device is a cross-check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.core.settings import VendorPlatformSettings


def slug(text: str) -> str:
    """'Phone repair' -> 'phone-repair'; what the lake's folder names use."""
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def identity_metadata(packet: dict, settings: VendorPlatformSettings) -> dict:
    """Who recorded it, what work and where: the same for every packet under one code."""
    city = slug(packet.get("city"))
    city = {slug(k): slug(v) for k, v in settings.city_aliases.items()}.get(city, city)
    details = {
        "activity": slug(packet.get("environment")),
        "city": city,
        "vendor_name": str(packet.get("vendor") or "").strip(),
        "site": str(packet.get("site") or "").strip(),
        "packet_device": str(packet.get("device_kind") or "").strip(),
    }
    return {key: value for key, value in details.items() if value not in ("", None)}


def session_metadata(packet: dict, settings: VendorPlatformSettings) -> dict:
    """The PeakAI session details for a packet (src/peakai/session.py validate_session)."""
    details = {**identity_metadata(packet, settings), "packet": packet["ref"]}
    if packet.get("recorded_from"):
        details["recorded_from"] = packet["recorded_from"]
    if packet.get("recorded_to"):
        details["recorded_to"] = packet["recorded_to"]
    if packet.get("hours_declared"):
        details["video_hours"] = packet["hours_declared"]
    return details


def packet_cards(packet: dict) -> int | None:
    """What the packet should hold: the manager's count when received, else the vendor's."""
    received = packet.get("cards_received")
    return received if isinstance(received, int) else packet.get("cards_sent")


@dataclass
class CountCheck:
    detected: int  # Cards the station sees plugged in.
    counted: int  # Cards the operator says they put in (what the station sees if not asked).
    expected: int | None  # Cards the packet should hold.
    problems: list[str] = field(default_factory=list)  # Each one needs a reason to go on.

    @property
    def ok(self) -> bool:
        return not self.problems


def check_counts(detected: int, counted: int | None, packet: dict | None) -> CountCheck:
    """Compare the cards plugged in with the packet. `counted` is the operator's own count, if
    they were asked; without it the station's count is checked against the packet's."""
    check = CountCheck(detected, detected if counted is None else counted,
                       packet_cards(packet) if packet else None)
    who = ("the manager counted" if packet and packet.get("cards_received") is not None
           else "the vendor sent")
    if counted is None:
        if check.expected is not None and detected != check.expected:
            missing = check.expected - detected
            check.problems.append(
                f"The packet holds {check.expected} card(s) ({who} {check.expected}) but the "
                f"station sees {detected}. "
                + (f"{missing} card(s) are not showing: check each reader's light and push "
                   "the card in again." if missing > 0 else
                   f"{-missing} more are plugged in: is a USB stick or another packet's card "
                   "in?"))
    else:
        if counted != detected:
            missing = counted - detected
            check.problems.append(
                f"You put in {counted} card(s) but the station sees {detected}. "
                + (f"{missing} card(s) are not showing: check each reader's light and push "
                   "the card in again." if missing > 0 else
                   f"{-missing} more than you counted are plugged in: is a USB stick or "
                   "another packet's card in?"))
        if check.expected is not None and counted != check.expected:
            check.problems.append(f"The packet says {who} {check.expected} card(s); you "
                                  f"counted {counted}.")
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
