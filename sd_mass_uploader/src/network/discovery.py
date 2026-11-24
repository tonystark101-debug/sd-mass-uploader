"""Find every network connection that can reach the internet on its own.

A connection counts as a line when it is up, has a usable IPv4 address and has its own default
route (gateway). The answer is a list of `FoundLine`; an empty list means "none found", and a
failed query raises, so a broken query can never read as "every line was unplugged".

- Linux: `ip -j` (iproute2, always on Ubuntu). Each line is pinned by network device.
- Windows: PowerShell `Get-NetIPConfiguration`. Each line is pinned by its local address, which
  Windows sends only through the adapter that owns it (strong-host model, the default).
- Other systems: not supported; the computer's default connection is used.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from src.core.exceptions import DeviceDetectionError, UploaderError
from src.platform.process import CommandRunner, SubprocessRunner, run_powershell


class DiscoveryError(UploaderError):
    """The system could not be asked which connections exist."""


@dataclass(frozen=True)
class FoundLine:
    key: str  # Stable identity: device name (Linux) or adapter GUID (Windows).
    name: str  # What the operator sees: "enp3s0", "Ethernet 2", "Wi-Fi".
    # "Ethernet", "Ethernet (USB)" (a LAN cable in a USB adapter or a hub's LAN port), "Wi-Fi",
    # "USB" (a phone sharing its connection), or "Other".
    kind: str
    ip: str
    gateway: str
    device: str | None = None  # Linux: bind to this device (SO_BINDTODEVICE).
    warning: str | None = None  # Something that stops pinning from working reliably.


def supported() -> bool:
    return sys.platform.startswith("linux") or sys.platform == "win32"


def discover(exclude: list[str], runner: CommandRunner | None = None) -> list[FoundLine]:
    runner = runner or SubprocessRunner()
    if sys.platform.startswith("linux"):
        found = _discover_linux(runner)
    elif sys.platform == "win32":
        found = _discover_windows(runner)
    else:
        return []
    return [line for line in found if not _excluded(line.name, exclude)]


def _excluded(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name.lower(), pattern.lower()) for pattern in patterns)


def _usable_ip(ip: str | None) -> bool:
    return bool(ip) and not ip.startswith(("169.254.", "127.", "0."))


# ---------------------------------------------------------------------------------- Linux

def _run_json(runner: CommandRunner, command: list[str]) -> list:
    try:
        done = runner.run(command, timeout=15)
    except DeviceDetectionError as exc:
        raise DiscoveryError(str(exc)) from exc
    if done.returncode != 0:
        raise DiscoveryError(f"{' '.join(command)} failed: {done.stderr.strip()[:300]}")
    text = done.stdout.strip()
    data = json.loads(text) if text else []
    return data if isinstance(data, list) else [data]


_PHONE_DRIVERS = {"rndis_host", "ipheth"}  # Android and iPhone connection sharing.


def _linux_kind(device: str) -> str:
    base = f"/sys/class/net/{device}"
    if os.path.exists(f"{base}/wireless") or os.path.exists(f"{base}/phy80211"):
        return "Wi-Fi"
    try:
        if "/usb" in os.path.realpath(f"{base}/device"):
            driver = os.path.basename(os.path.realpath(f"{base}/device/driver"))
            return "USB" if driver in _PHONE_DRIVERS else "Ethernet (USB)"
    except OSError:
        pass
    return "Ethernet" if os.path.exists(f"{base}/device") else "Other"


def _rp_filter(device: str) -> int:
    """Reverse-path filter: 1 (strict) drops replies on a second line; 0 and 2 are fine."""
    values = []
    for scope in ("all", device):
        try:
            text = Path(f"/proc/sys/net/ipv4/conf/{scope}/rp_filter").read_text()
            values.append(int(text.strip() or 0))
        except (OSError, ValueError):
            continue
    return max(values, default=0)


def parse_linux(routes: list, addresses: list) -> list[tuple[str, str, str]]:
    """(device, ip, gateway) for each device with its own default route. Pure, for tests."""
    gateways: dict[str, str] = {}
    for route in routes:
        device, gateway = route.get("dev"), route.get("gateway")
        if not device or not gateway or "linkdown" in (route.get("flags") or []):
            continue
        gateways.setdefault(device, gateway)  # Lowest metric first: `ip` lists it first.
    found = []
    for item in addresses:
        device = item.get("ifname")
        if device not in gateways or item.get("operstate") == "DOWN":
            continue
        ips = [info.get("local") for info in item.get("addr_info") or []
               if info.get("family") == "inet" and _usable_ip(info.get("local"))]
        if ips:
            found.append((device, ips[0], gateways[device]))
    return found


def _discover_linux(runner: CommandRunner) -> list[FoundLine]:
    routes = _run_json(runner, ["ip", "-j", "-4", "route", "show", "default"])
    addresses = _run_json(runner, ["ip", "-j", "-4", "addr", "show"])
    lines = []
    for device, ip, gateway in parse_linux(routes, addresses):
        warning = None
        if _rp_filter(device) == 1:
            warning = (f"strict reverse-path filtering (rp_filter=1) on {device} can drop the "
                       "replies of a second line; set net.ipv4.conf.all.rp_filter=2")
        lines.append(FoundLine(device, device, _linux_kind(device), ip, gateway,
                               device=device, warning=warning))
    return lines


# -------------------------------------------------------------------------------- Windows

_WINDOWS_QUERY = (
    "Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and "
    "$_.NetAdapter.Status -eq 'Up' } | ForEach-Object { "
    "$a = @($_.IPv4Address | Where-Object { $_.IPAddress -notlike '169.254.*' })[0]; "
    "$i = Get-NetIPInterface -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 "
    "-ErrorAction SilentlyContinue; "
    "[pscustomobject]@{ alias=$_.InterfaceAlias; guid=[string]$_.NetAdapter.InterfaceGuid; "
    "ip=$a.IPAddress; gateway=@($_.IPv4DefaultGateway.NextHop)[0]; "
    "desc=[string]$_.NetAdapter.InterfaceDescription; "
    "media=[string]$_.NetAdapter.PhysicalMediaType; weak=[string]$i.WeakHostSend } } "
    "| ConvertTo-Json -Depth 3 -Compress"
)


def parse_windows(rows: list) -> list[FoundLine]:
    """Pure, for tests."""
    lines = []
    for row in rows:
        ip = row.get("ip")
        if not _usable_ip(ip) or not row.get("gateway"):
            continue
        text = f"{row.get('media', '')} {row.get('desc', '')}".lower()
        if "802.11" in text or "wireless" in text or "wi-fi" in text:
            kind = "Wi-Fi"
        elif "ndis" in text or "usb" in text:
            # A LAN cable in a USB adapter or a hub's LAN port, or a phone sharing its connection.
            phone = any(word in text for word in ("rndis", "remote ndis", "internet sharing",
                                                  "iphone", "apple mobile", "android"))
            wired = any(word in text for word in ("ethernet", "gbe", "gigabit", "lan", "802.3"))
            kind = "Ethernet (USB)" if wired and not phone else "USB"
        else:
            kind = "Ethernet"
        warning = None
        if row.get("weak") == "Enabled":
            warning = ("WeakHostSend is on for this adapter, so Windows may send its uploads "
                       "through another adapter")
        name = row.get("alias") or ip
        lines.append(FoundLine(row.get("guid") or name, name, kind, ip, row["gateway"],
                               warning=warning))
    return lines


def _discover_windows(runner: CommandRunner) -> list[FoundLine]:
    try:
        done = run_powershell(runner, _WINDOWS_QUERY, timeout=60)
    except DeviceDetectionError as exc:
        raise DiscoveryError(str(exc)) from exc
    if done.returncode != 0:
        raise DiscoveryError(f"PowerShell failed: {done.stderr.strip()[:300]}")
    text = done.stdout.strip()
    rows = json.loads(text) if text else []
    return parse_windows(rows if isinstance(rows, list) else [rows])
