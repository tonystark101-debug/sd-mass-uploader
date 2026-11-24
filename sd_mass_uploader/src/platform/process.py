"""The only place that launches OS programs.

Inventory uses a read-only allowlist. The AI troubleshooter uses `run_reviewed`, which applies
its command policy (no card writes, no shells) immediately before each process starts.
"""

from __future__ import annotations

import base64
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePath
from typing import Protocol

from src.core.exceptions import DeviceDetectionError, ReadOnlyViolation

ALLOWED_PROGRAMS = frozenset({"powershell", "lsblk", "lsusb", "findmnt", "udisksctl", "ip",
                              "udevadm"})

# udevadm may only wait for new devices to finish appearing. Exactly this.
UDEVADM_SETTLE = ("settle", "--timeout=10")

# `ip` (iproute2) may only list routes and addresses, to find the internet lines. Exactly these.
IP_READ_ONLY_COMMANDS = (
    ("-j", "-4", "route", "show", "default"),
    ("-j", "-4", "addr", "show"),
)

# udisksctl may only mount, and only read-only. Unmount, power-off and every other verb are refused.
READ_ONLY_MOUNT_OPTION = "ro"

# Backstop for inline PowerShell. The scripts here only read, and this keeps it that way.
_FORBIDDEN_POWERSHELL = re.compile(
    r"\b(Format-|Clear-Disk|Initialize-Disk|Set-Disk|Set-Partition|Set-Volume|Remove-|"
    r"New-Partition|Resize-Partition|Repair-Volume|Add-PartitionAccessPath|"
    r"Mount-|Dismount-|diskpart|chkdsk|Move-Item|Set-Content|Out-File|Invoke-Expression)",
    re.IGNORECASE,
)


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, argv: list[str], timeout: float = 60) -> CommandResult: ...


def assert_read_only(argv: list[str]) -> None:
    """Refuse anything outside the allowlist before a process can start."""
    if not argv:
        raise ReadOnlyViolation("Empty command")
    program = PurePath(argv[0]).stem.lower()
    if program not in ALLOWED_PROGRAMS:
        raise ReadOnlyViolation(f"Command is not on the read-only allowlist: {argv[0]}")
    if program == "udisksctl":
        _assert_read_only_mount(argv)
    if program == "udevadm" and tuple(argv[1:]) != UDEVADM_SETTLE:
        raise ReadOnlyViolation(f"Only `udevadm settle` is allowed: {argv}")
    if program == "ip" and tuple(argv[1:]) not in IP_READ_ONLY_COMMANDS:
        raise ReadOnlyViolation(f"Only listing routes and addresses is allowed: {argv}")


def _assert_read_only_mount(argv: list[str]) -> None:
    """Exactly: udisksctl mount --block-device DEV --options OPTS --no-user-interaction."""
    shape = (len(argv) == 7 and argv[1] == "mount" and argv[2] == "--block-device"
             and argv[4] == "--options" and argv[6] == "--no-user-interaction")
    if not shape:
        raise ReadOnlyViolation(f"Only a read-only udisksctl mount is allowed: {argv}")
    if not argv[3].startswith("/dev/"):
        raise ReadOnlyViolation(f"Not a block device: {argv[3]}")
    options = argv[5].split(",")
    if READ_ONLY_MOUNT_OPTION not in options or "rw" in options:
        raise ReadOnlyViolation(f"Mount options must include ro and not rw: {argv[5]}")


class SubprocessRunner:
    """Runs argument lists (never a shell), with a timeout and captured stderr."""

    def run(self, argv: list[str], timeout: float = 60) -> CommandResult:
        assert_read_only(argv)
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise DeviceDetectionError(f"Program not found: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise DeviceDetectionError(f"{argv[0]} timed out after {timeout}s") from exc
        except OSError as exc:
            raise DeviceDetectionError(f"Could not run {argv[0]}: {exc}") from exc
        return CommandResult(
            done.returncode,
            done.stdout.decode("utf-8", errors="replace"),
            done.stderr.decode("utf-8", errors="replace"),
        )


def run_powershell(runner: CommandRunner, script: str, timeout: float = 60) -> CommandResult:
    """Run a read-only script. It is base64-encoded so no quoting can alter it."""
    match = _FORBIDDEN_POWERSHELL.search(script)
    if match:
        raise ReadOnlyViolation(f"Script contains forbidden command: {match.group(0)}")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    return runner.run(argv, timeout=timeout)


def launch_console_window(argv: list[str]) -> None:
    """Start the console window as its own process (from the panel indicator's menu).

    Only this exact shape is allowed: <this Python> <.../main.py> [--config X] desktop --window.
    """
    import os
    import sys

    if not argv or os.path.abspath(argv[0]) != os.path.abspath(sys.executable):
        raise ReadOnlyViolation("Only this program's own Python may be started")
    if len(argv) < 4 or not argv[1].endswith("main.py") or argv[-2:] != ["desktop", "--window"]:
        raise ReadOnlyViolation(f"Not a console window command: {argv}")
    middle = argv[2:-2]
    if middle and (len(middle) != 2 or middle[0] != "--config"):
        raise ReadOnlyViolation(f"Unexpected arguments: {middle}")
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,  # noqa: S603
                     stderr=subprocess.DEVNULL, start_new_session=True)


def start_claude(argv: list[str], cwd: str) -> subprocess.Popen:
    """Start Claude Code in print mode for the problem fixer (src/ai/claude_fixer.py).

    Only this shape: <claude program> -p <prompt> ... --settings <file>. The settings file holds
    the guard hook that every command Claude tries must pass. Never through a shell. Output is
    one JSON event per line on stdout.
    """
    if not argv or PurePath(argv[0]).name not in {"claude", "claude.exe", "claude.cmd"}:
        raise ReadOnlyViolation(f"Only Claude Code may be started here: {argv[:1]}")
    if len(argv) < 3 or argv[1] != "-p" or "--settings" not in argv:
        raise ReadOnlyViolation("Claude Code must run in print mode with the guard settings")
    return subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,  # noqa: S603
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", bufsize=1,
                            start_new_session=True)


def probe(argv: list[str], timeout: float = 30) -> CommandResult | None:
    """For `fixer check` only: is Claude Code installed, and can sudo run without a password?

    Exactly two shapes: <claude> --version, and sudo -n true. None if the program is missing.
    """
    claude = PurePath(argv[0]).name in {"claude", "claude.exe", "claude.cmd"} \
        and argv[1:] == ["--version"]
    if not (claude or argv == ["sudo", "-n", "true"]):
        raise ReadOnlyViolation(f"Not a fixer check: {argv}")
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return CommandResult(done.returncode, done.stdout.decode("utf-8", "replace"),
                         done.stderr.decode("utf-8", "replace"))


def run_reviewed(argv: list[str], check: Callable[[list[str]], None],
                 timeout: float = 60, max_output: int = 20_000) -> CommandResult:
    """Run a command proposed by the AI troubleshooter and approved by an operator.

    `check` (src/ai/policy.assert_command_allowed) runs here, immediately before the process
    starts, so nothing can reach the OS without passing it. Never through a shell.
    """
    check(argv)
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise DeviceDetectionError(f"Program not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeviceDetectionError(f"{argv[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise DeviceDetectionError(f"Could not run {argv[0]}: {exc}") from exc
    return CommandResult(
        done.returncode,
        done.stdout.decode("utf-8", errors="replace")[-max_output:],
        done.stderr.decode("utf-8", errors="replace")[-max_output:],
    )
