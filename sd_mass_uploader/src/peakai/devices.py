"""Which device recorded each part of a card, so it goes to the right PeakAI pass.

The rules come from SITE_CHAT_INSTRUCTIONS.md section 2 (v2). "The pass comes from the files
on the card, not from what the person says." Anything that matches no rule is left unplaced and
the card waits for a person: the station never guesses a pass.

Files are only listed and, for a few small files, read (session.json, the first bytes of an
encrypted recording, an MP4's header). Nothing on the card is changed.
"""

from __future__ import annotations

import fnmatch
import json
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import BinaryIO

GILABS = "GI Labs EGO1GS"
BUILDAI = "BuildAI stereo (encrypted)"
PHONE_RIG = "Phone stereo rig"
E2 = "E2 stereo"
STEREO_HAT = "Stereo hat"
HEAD_CAMERA = "Head camera"
DEVICE_NAMES = (GILABS, BUILDAI, PHONE_RIG, E2, STEREO_HAT, HEAD_CAMERA)

# Device -> passes it may use, in order of preference.
DEVICE_PASSES = {
    GILABS: ("gilabs",),
    BUILDAI: ("buildai",),
    PHONE_RIG: ("ots-stereo",),
    E2: ("ots-stereo",),
    STEREO_HAT: ("ots-stereo",),
    HEAD_CAMERA: ("sieve", "ots-mono"),  # Sieve if this laptop has it, else ots-mono.
}
STEREO_PASSES = {"gilabs", "buildai", "ots-stereo"}  # `camera` in _info: stereo; else mono.

# Not ours, or not footage: never uploaded, always reported.
_EXCLUDED_PREFIXES = {
    "android/": "phone app data (Android/), not project footage",
    ".trashes/": "system folder", ".spotlight-v100/": "system folder",
    ".fseventsd/": "system folder", "lost.dir/": "system folder",
}
_MIN_PLAUSIBLE = datetime(2015, 1, 1, tzinfo=UTC)
_MAX_PROBES = 3000

Reader = Callable[[str, int], bytes | None]  # (relative path, max bytes) -> bytes, or None.
Opener = Callable[[str], BinaryIO | None]  # Read-only handle for MP4 probing, or None.


@dataclass
class DeviceGroup:
    device: str
    prefixes: list[str]  # Folders on the card that belong to this device, e.g. ["data/"].
    files: list[str] = field(default_factory=list)
    bytes: int = 0
    pass_name: str | None = None
    hours: float | None = None
    recorded_from: str | None = None
    recorded_to: str | None = None


@dataclass
class Classification:
    groups: list[DeviceGroup]
    excluded: list[tuple[str, str]]  # (path, why)
    unplaced: list[str]
    notes: list[str] = field(default_factory=list)

    def main_group(self) -> DeviceGroup | None:
        return max(self.groups, key=lambda group: group.bytes) if self.groups else None


def _match(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path.lower(), pattern.lower())


def _top(path: str, depth: int) -> str:
    return "/".join(path.split("/")[:depth]) + "/"


def classify(files: list[tuple[str, int]], read: Reader) -> Classification:
    """`files`: (relative path with "/", size). Root files go with the main device."""
    notes: list[str] = []
    groups: list[DeviceGroup] = []

    # GI Labs EGO1GS: data/session_*/ego_*.mcap with a session.json whose hostname is GILABS-*.
    sessions = [path for path, _ in files if _match(path, "data/session_*/session.json")]
    mcaps = [path for path, _ in files if _match(path, "data/session_*/ego_*.mcap")]
    if mcaps:
        hosts = []
        for path in sessions[:20]:
            try:
                hosts.append(str(json.loads(read(path, 65536) or b"{}").get("hostname", "")))
            except (ValueError, AttributeError):
                continue
        if any(host.startswith("GILABS-") for host in hosts):
            groups.append(DeviceGroup(GILABS, ["data/"]))
        else:
            notes.append("data/ has GI Labs-style .mcap files, but no session.json with a "
                         "GILABS- hostname")

    # E2 stereo: recordings/ego_*/metadata.json + video_live_*.mp4 (checked before BuildAI,
    # which also lives in recordings/).
    e2_dirs = sorted({_top(path, 2) for path, _ in files
                      if _match(path, "recordings/ego_*/metadata.json")})
    e2_dirs = [d for d in e2_dirs
               if any(_match(path, d + "video_live_*.mp4") for path, _ in files)]
    if e2_dirs:
        groups.append(DeviceGroup(E2, e2_dirs))

    # BuildAI stereo (encrypted): recordings/*-sn<serial>-*-left.mp4.enc or .imu.enc, GKENC1.
    candidates = [path for path, _ in files
                  if _match(path, "recordings/*-sn*-*-left.mp4.enc")
                  or _match(path, "recordings/*.imu.enc")]
    candidates = [path for path in candidates if path.count("/") == 1]
    if candidates:
        if any((read(path, 6) or b"") == b"GKENC1" for path in candidates[:5]):
            groups.append(DeviceGroup(BUILDAI, ["recordings/"]))
        else:
            notes.append("recordings/ has BuildAI-style names, but the files do not start "
                         "with GKENC1")

    # Phone stereo rig: Stereo/recordings/stereo_*/chunk_*/video.h265.
    if any(_match(path, "stereo/recordings/stereo_*/chunk_*/video.h265") for path, _ in files):
        groups.append(DeviceGroup(PHONE_RIG, ["Stereo/"]))

    # Head camera: DVR/VIDEO/*.mp4.
    if any(_match(path, "dvr/video/*.mp4") for path, _ in files):
        groups.append(DeviceGroup(HEAD_CAMERA, ["DVR/"]))

    result = Classification(groups, [], [], notes)
    assign_files(files, result)
    return result


def assign_files(files: list[tuple[str, int]], result: Classification,
                 catch_all: DeviceGroup | None = None) -> None:
    """Put each file in its device's group by folder (most specific folder wins).

    Root files go with the main device. With `catch_all` (an operator's decision), files that
    match no device go there instead of staying unplaced. Groups left empty are dropped.
    """
    for group in result.groups:
        group.files, group.bytes = [], 0
    result.excluded, result.unplaced = [], []
    roots: list[tuple[str, int]] = []
    for path, size in files:
        lowered = path.lower()
        why = next((reason for prefix, reason in _EXCLUDED_PREFIXES.items()
                    if lowered.startswith(prefix)), None)
        if why is None and path.rsplit("/", 1)[-1].startswith("._"):
            why = "macOS helper file, not footage"
        if why:
            result.excluded.append((path, why))
            continue
        if "/" not in path:
            roots.append((path, size))
            continue
        owner = max(((group, prefix) for group in result.groups for prefix in group.prefixes
                     if lowered.startswith(prefix.lower())),
                    key=lambda item: len(item[1]), default=None)
        target = owner[0] if owner else catch_all
        if target is None:
            result.unplaced.append(path)
        else:
            target.files.append(path)
            target.bytes += size
    if catch_all is not None and catch_all.files and catch_all not in result.groups:
        result.groups.append(catch_all)
    result.groups = [group for group in result.groups if group.files]
    main = result.main_group() or catch_all
    for path, size in roots:  # The card's small root files go with the main device.
        if main is None:
            result.unplaced.append(path)
        else:
            main.files.append(path)
            main.bytes += size
    if main is not None and main.files and main not in result.groups:
        result.groups.append(main)


def choose_passes(classification: Classification, usable: list[str]) -> list[str]:
    """Set each group's pass. Returns problems (a device whose pass this laptop lacks)."""
    problems = []
    for group in classification.groups:
        options = DEVICE_PASSES[group.device]
        group.pass_name = next((name for name in options if name in usable), None)
        if group.pass_name is None:
            problems.append(f"{group.device} needs the {' or '.join(options)} pass, which this "
                            "laptop does not have. Check the device; if it is right, ask PeakAI "
                            "to add the pass")
    return problems


# ---------------------------------------------------------------------- MP4 details (D13)

def _boxes(handle: BinaryIO, start: int, end: int):
    position = start
    while position + 8 <= end:
        handle.seek(position)
        header = handle.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header)
        offset = 8
        if size == 1:
            size = struct.unpack(">Q", handle.read(8))[0]
            offset = 16
        elif size == 0:
            size = end - position
        if size < offset:
            return
        yield kind, position + offset, position + size
        position += size


def probe_mp4(handle: BinaryIO) -> tuple[float, datetime | None] | None:
    """Duration in seconds and creation time from the movie header, or None."""
    handle.seek(0, 2)
    end = handle.tell()
    for kind, body, box_end in _boxes(handle, 0, end):
        if kind != b"moov":
            continue
        for inner, inner_body, _ in _boxes(handle, body, box_end):
            if inner != b"mvhd":
                continue
            handle.seek(inner_body)
            version = handle.read(1)
            handle.read(3)
            if version == b"\x01":
                created, _, scale, duration = struct.unpack(">QQIQ", handle.read(28))
            else:
                created, _, scale, duration = struct.unpack(">IIII", handle.read(16))
            if not scale:
                return None
            when = None
            if created:
                stamp = datetime(1904, 1, 1, tzinfo=UTC).timestamp() + created
                try:
                    when = datetime.fromtimestamp(stamp, UTC)
                except (OverflowError, OSError, ValueError):
                    when = None
            return duration / scale, when
    return None


def detect_details(group: DeviceGroup, open_file: Opener) -> None:
    """Hours and first/last recording day from MP4 headers. Left empty when not knowable
    (encrypted, raw H.265, .mcap): the operator gives them, never a guess."""
    videos = [path for path in group.files if path.lower().endswith(".mp4")][:_MAX_PROBES]
    if not videos:
        return
    seconds, days, probed = 0.0, [], 0
    for path in videos:
        handle = open_file(path)
        if handle is None:
            return
        with handle:
            try:
                found = probe_mp4(handle)
            except (OSError, struct.error):
                found = None
        if found is None:
            return  # One unreadable header: the total would be wrong, so leave it to a person.
        probed += 1
        seconds += found[0]
        if found[1] and found[1] >= _MIN_PLAUSIBLE:
            days.append(found[1].date())
    if probed == len(videos):
        group.hours = round(seconds / 3600, 2)
        if days and len(days) == probed:
            group.recorded_from, group.recorded_to = min(days).isoformat(), max(days).isoformat()
