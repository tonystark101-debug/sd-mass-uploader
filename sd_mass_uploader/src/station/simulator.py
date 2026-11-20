"""Simulated cards for testing without readers. Touches only the simulator folder.

This is the only station module that creates, renames or writes files outside app data; it
exists so the station can be developed and demonstrated on any machine. It is never given a
real card's mount point: the simulator folder comes from `station.simulator_dir` or `--dir`.
"""

from __future__ import annotations

import os
from pathlib import Path

from src.core.exceptions import ConfigurationError
from src.station.sources import SIMULATOR_EJECTED_SUFFIX


def _paths(root: Path, name: str) -> tuple[Path, Path]:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ConfigurationError(f"Not a valid simulated card name: {name!r}")
    return root / name, root / f"{name}{SIMULATOR_EJECTED_SUFFIX}"


def create_card(root: Path, name: str, files: int, size_bytes: int) -> Path:
    inserted, ejected = _paths(root, name)
    if inserted.exists() or ejected.exists():
        raise ConfigurationError(f"Simulated card {name} already exists")
    root.mkdir(parents=True, exist_ok=True)
    building = root / f".{name}.creating"  # Hidden until complete, so no half-card is scanned.
    (building / "recordings").mkdir(parents=True)
    for number in range(files):
        (building / "recordings" / f"rec_{number:04d}.bin").write_bytes(os.urandom(size_bytes))
    os.replace(building, inserted)
    return inserted


def eject_card(root: Path, name: str) -> None:
    inserted, ejected = _paths(root, name)
    if not inserted.exists():
        raise ConfigurationError(f"Simulated card {name} is not inserted")
    os.replace(inserted, ejected)


def insert_card(root: Path, name: str) -> None:
    inserted, ejected = _paths(root, name)
    if not ejected.exists():
        raise ConfigurationError(f"Simulated card {name} is not removed")
    os.replace(ejected, inserted)
