"""The interface every platform adapter implements."""

from __future__ import annotations

import hashlib
from typing import Protocol

from src.core.models import RawInventory


class PlatformAdapter(Protocol):
    def collect(self) -> RawInventory:
        """Gather a read-only inventory. Per-device failures become issues, not exceptions."""
        ...


def stable_id(prefix: str, *parts: object) -> str:
    """Build an ID from identifying facts. Never from a drive letter or mount point."""
    text = "|".join("" if part is None else str(part) for part in parts)
    return f"{prefix}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}"


def usb_capability_from_bcd(bcd: int | None) -> str | None:
    """Turn a descriptor's bcdUSB (for example 0x0320) into 'USB 3.2'. It is capability only."""
    if not bcd:
        return None
    major, minor = (bcd >> 8) & 0xFF, (bcd >> 4) & 0xF
    return f"USB {major:x}.{minor:x}"
