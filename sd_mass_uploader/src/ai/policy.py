"""Which shell commands the AI troubleshooter may run (decision D9: tiered, with approval).

Every command the AI proposes is model output, so it is treated as untrusted:
- One program at a time, run without a shell. Pipes, redirects and chaining are refused.
- Programs that can run hidden commands (shells, interpreters, xargs) are refused.
- Anything that could write to a card is refused, and approval cannot override that:
  formatting, repair, partitioning, raw writes, mounting, and any non-read-only program that
  names a card's mount point or a disk device.
- Credentials and secrets are never read or printed.
Everything else needs the operator to approve that exact command.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum


class Verdict(StrEnum):
    NEEDS_APPROVAL = "needs_approval"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class CommandReview:
    verdict: Verdict
    reason: str
    argv: list[str] = field(default_factory=list)


_SHELL_OPERATORS = ("|", "&", ";", "`", "$(", ">", "<", "\n", "\r")

# Programs that only pass control to another program. Checked through to the real one.
_WRAPPERS = {"sudo", "doas", "pkexec", "env", "nohup", "timeout", "nice", "ionice", "stdbuf",
             "time", "command", "exec", "chrt", "taskset", "setsid", "unbuffer"}
_WRAPPER_OPTIONS_WITH_VALUE = {"-u", "-g", "-n", "-c", "-p", "-t", "-s", "-k", "-C", "-h",
                               "-U", "-r", "--user", "--group", "--signal", "--kill-after"}

# Programs that can run arbitrary hidden commands, so no review of the visible text is enough.
_INTERPRETERS = re.compile(
    r"^(sh|bash|dash|zsh|ksh|mksh|fish|csh|tcsh|busybox|toybox|python[\d.]*|pypy[\d.]*|perl[\d.]*"
    r"|ruby[\d.]*|node|nodejs|deno|bun|php[\d.]*|lua[\d.]*|tclsh|wish|awk|gawk|mawk|nawk|xargs"
    r"|eval|source|script|expect|parallel|watch|su|screen|tmux|at|batch|crontab|systemd-run"
    r"|ssh|scp|sftp|nc|ncat|netcat|socat|telnet|vi|vim|nvim|nano|emacs|ed|less|more|man)$"
)

# Programs that write to disks or file systems. Never run, approved or not.
_CARD_WRITERS = re.compile(
    r"^(mkfs(\..+)?|mkdosfs|mkexfatfs|mke2fs|mkntfs|mkswap|fsck(\..+)?|e2fsck|dosfsck|exfatfsck"
    r"|fsck_exfat|ntfsfix|dd|ddrescue|wipefs|shred|parted|partprobe|fdisk|sfdisk|sgdisk|gdisk"
    r"|cfdisk|gparted|badblocks|hdparm|sdparm|blkdiscard|fstrim|mount|umount|eject|losetup"
    r"|cryptsetup|fatlabel|exfatlabel|dosfslabel|mlabel|e2label|tune2fs|resize2fs|ntfslabel"
    r"|ntfsresize|debugfs|testdisk|sg_.+|blockdev|zerofree|chattr|setfattr)$"
)

# Programs that only read. Allowed to name a card path (still after approval).
_READ_ONLY_TOOLS = {"ls", "stat", "cat", "head", "tail", "file", "md5sum", "sha1sum",
                    "sha256sum", "sha512sum", "b2sum", "cksum", "du", "df", "findmnt", "lsblk",
                    "blkid", "wc", "hexdump", "xxd", "od", "find", "tree", "exiftool",
                    "ffprobe", "mediainfo", "grep", "diff", "cmp", "readlink", "realpath",
                    "smartctl", "udevadm", "lsof"}
# Read-only tools allowed to name a disk device (/dev/sd*, /dev/mmcblk*).
_DEVICE_READERS = {"lsblk", "blkid", "stat", "file", "ls", "udevadm", "smartctl", "lsof",
                   "findmnt", "readlink"}
_FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0",
                 "-fprintf", "-fls"}
_UDISKS_READS = {"status", "info", "dump", "monitor", "help"}
_SMARTCTL_WRITES = {"-s", "--smart", "-o", "--offlineauto", "-S", "--saveauto", "-t",
                    "--test", "-X", "--abort", "--set"}

_SECRET = re.compile(
    r"(credentials\.json|api-token|ai\.env|application_default_credentials|\.config/gcloud"
    r"|laptop_key|_token\.txt|PEAKAI_PASS_|PeakAI-Uploader/tokens|/secrets/|X-PeakAI-Key"
    r"|/etc/shadow|/etc/sudoers|id_rsa|id_ed25519|\.pem$|\.key$|ANTHROPIC_API_KEY|/environ)",
    re.IGNORECASE,
)
_SECRET_PROGRAMS = {"printenv", "env", "set", "declare", "export", "gcloud", "keyctl"}

_DEVICE_PATH = re.compile(r"^/dev/(sd[a-z]+\d*|mmcblk\d+(p\d+)?|disk/.+|nvme\d+n\d+(p\d+)?"
                          r"|hd[a-z]+\d*|vd[a-z]+\d*|loop\d+|dm-\d+|mapper/.+|sg\d+)$")
CARD_ROOTS_DEFAULT = ("/media/", "/run/media/", "/mnt/")


def _program(token: str) -> str:
    return os.path.basename(token).lower()


def _unwrap(argv: list[str]) -> list[str]:
    """Skip sudo/env/timeout/... so the real program is checked."""
    rest = list(argv)
    while rest and _program(rest[0]) in _WRAPPERS:
        wrapper = _program(rest.pop(0))
        positional_to_skip = 1 if wrapper in {"timeout", "chrt", "taskset"} else 0
        while rest:
            token = rest[0]
            if token in _WRAPPER_OPTIONS_WITH_VALUE:
                rest = rest[2:]
            elif token.startswith("-"):
                rest = rest[1:]
            elif wrapper == "env" and "=" in token:
                rest = rest[1:]
            elif positional_to_skip:
                rest, positional_to_skip = rest[1:], positional_to_skip - 1
            else:
                break
        if not rest and wrapper == "env":
            return ["printenv"]  # A bare `env` prints the environment, which holds secrets.
    return rest


def _paths(argv: Iterable[str]) -> list[str]:
    found = []
    for token in argv:
        value = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
        if value.startswith("/"):
            found.append(posixpath.normpath(value))
    return found


def _under(path: str, roots: Iterable[str]) -> bool:
    for root in roots:
        root = posixpath.normpath(root)
        if path == root or path.startswith(root.rstrip("/") + "/"):
            return True
    return False


def review_command(command: str, card_mounts: Iterable[str] = ()) -> CommandReview:
    """Decide whether a proposed command is blocked or may run after operator approval."""
    text = (command or "").strip()
    if not text:
        return CommandReview(Verdict.BLOCKED, "Empty command")
    if any(operator in text for operator in _SHELL_OPERATORS):
        return CommandReview(Verdict.BLOCKED, "Run one program at a time: pipes, redirects, "
                                              "background jobs and chaining are not allowed")
    try:
        argv = shlex.split(text)
    except ValueError as exc:
        return CommandReview(Verdict.BLOCKED, f"Could not parse the command: {exc}")
    if any(_SECRET.search(token) for token in argv):
        return CommandReview(Verdict.BLOCKED, "Commands may not read or print credentials")
    real = _unwrap(argv)
    if not real:
        return CommandReview(Verdict.BLOCKED, "No program to run")
    program = _program(real[0])
    if program in _SECRET_PROGRAMS:
        return CommandReview(Verdict.BLOCKED, "Commands may not print the environment or "
                                              "credentials")
    if _INTERPRETERS.match(program):
        return CommandReview(Verdict.BLOCKED, f"{program} can run hidden commands; run the "
                                              "program you need directly")
    if _CARD_WRITERS.match(program):
        return CommandReview(Verdict.BLOCKED, f"{program} can change disks or cards. Cards are "
                                              "read-only; this is never allowed")
    if program == "udisksctl" and (len(real) < 2 or real[1] not in _UDISKS_READS):
        return CommandReview(Verdict.BLOCKED, "Only read-only udisksctl commands are allowed; "
                                              "the station mounts cards itself")
    if program == "find" and any(token in _FIND_ACTIONS for token in real):
        return CommandReview(Verdict.BLOCKED, "find may only list files, not run or delete")
    if program == "smartctl" and any(token.split("=")[0] in _SMARTCTL_WRITES for token in real):
        return CommandReview(Verdict.BLOCKED, "smartctl may only read")
    roots = [*CARD_ROOTS_DEFAULT, *(mount for mount in card_mounts if mount)]
    for path in _paths(real[1:]):
        if _DEVICE_PATH.match(path) and program not in _DEVICE_READERS:
            return CommandReview(Verdict.BLOCKED, f"{program} may not use the disk device "
                                                  f"{path}")
        if _under(path, roots) and program not in _READ_ONLY_TOOLS:
            return CommandReview(Verdict.BLOCKED, f"{program} may not touch {path}: it is on a "
                                                  "card, and cards are read-only")
    return CommandReview(Verdict.NEEDS_APPROVAL,
                         "Runs as the station's user after you approve this exact command",
                         argv)


def assert_command_allowed(argv: list[str], card_mounts: Iterable[str] = ()) -> None:
    """The last check, inside the runner, just before a process starts."""
    from src.core.exceptions import ReadOnlyViolation

    review = review_command(shlex.join(argv), card_mounts)
    if review.verdict is Verdict.BLOCKED:
        raise ReadOnlyViolation(review.reason)
