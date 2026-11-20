"""Source-file access. Every read of source media goes through open_source_readonly."""

from __future__ import annotations

import errno
import os
import sys
from typing import BinaryIO

IS_WINDOWS = sys.platform == "win32"

# Windows errors meaning the device went away: not ready, device removed, and so on.
_WINDOWS_GONE = {3, 21, 31, 55, 433, 1006, 1167}
_POSIX_GONE = {errno.ENODEV, errno.ENXIO, errno.EIO, getattr(errno, "ENOMEDIUM", errno.ENODEV)}


def to_extended_path(path: str) -> str:
    r"""Add Windows' \\?\ prefix so paths over 260 characters work. No-op elsewhere."""
    if not IS_WINDOWS or path.startswith("\\\\?\\"):
        return path
    full = os.path.abspath(path)
    if full.startswith("\\\\"):
        return "\\\\?\\UNC\\" + full[2:]
    return "\\\\?\\" + full


def open_source_readonly(path: str) -> BinaryIO:
    """Open a source file for reading only, keeping its access time where the OS allows."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    target = to_extended_path(path)
    try:
        descriptor = os.open(target, flags | noatime)
    except PermissionError:
        if not noatime:
            raise
        descriptor = os.open(target, flags)  # O_NOATIME needs file ownership.
    return os.fdopen(descriptor, "rb")


def is_device_gone_error(exc: OSError) -> bool:
    """True if this error suggests the device itself vanished, not just one file."""
    if getattr(exc, "winerror", None) in _WINDOWS_GONE:
        return True
    return exc.errno in _POSIX_GONE


def mount_is_present(mount_point: str) -> bool:
    try:
        return os.path.isdir(to_extended_path(mount_point))
    except OSError:
        return False
