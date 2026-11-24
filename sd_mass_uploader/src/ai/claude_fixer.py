"""Claude Code fixes station problems by itself, while the uploads carry on.

The station keeps a live list of problems (src/advisor). When one lasts longer than
`claude_fix.wait_seconds` and only a machine can fix it (not "which device recorded this card?"),
Claude Code, the `claude` program installed on the station and logged in to the operator's
subscription, is started in the background with the problem, the station's state and terminal
and sudo access. Its steps are printed in the station's terminal and published as station
events, so the console's Activity list shows them too. The station never waits for it.

Every command Claude tries first passes `guard()`, run by Claude Code itself as a PreToolUse
hook. It refuses anything that could destroy footage or stop the uploads: writing, formatting
or repartitioning a card, mounting one read-write, deleting from the bucket, changing Google
Cloud access, stopping the station, killing processes, rebooting. A guard on command text is a
strong net, not a cage, so the same rules are also in Claude's instructions.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.settings import ClaudeFixSettings
from src.platform.process import start_claude

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}
CARD_PATHS = r"(/media/|/run/media/|/mnt/)"
_END = r"[^|;&\n]*"  # The rest of one command, up to a pipe, separator or newline.

# (pattern, why it is refused). Searched anywhere in the command, so `sudo`, `bash -c "..."`
# and full paths such as /sbin/mkfs.vfat are caught too.
_REFUSED: list[tuple[re.Pattern, str]] = [(re.compile(pattern, re.IGNORECASE), why) for
                                          pattern, why in [
    (r"\b(mkfs(\.\w+)?|mkdosfs|mkexfatfs|mkntfs|mkswap|wipefs|fdisk|sfdisk|cfdisk|gdisk|sgdisk|"
     r"parted|partprobe|blkdiscard|shred|hdparm|sg_format|mlabel|fatlabel|exfatlabel|dosfslabel|"
     r"tune2fs|e2label|cryptsetup|mdadm|zpool|lvcreate|vgcreate|pvcreate)\b",
     "it formats, partitions or relabels disks; footage on a card must never change"),
    (r"\bdd\b" + _END + r"\bof=", "dd writes to a disk or file"),
    (r"\bbadblocks\b" + _END + r"\s-\w*[wn]", "a write test erases a card"),
    (r"\b(dosfsck|exfatfsck|fsck\.\w+|e2fsck|fsck)\b(?!" + _END + r"\s-n\b)",
     "fsck can change a card; only `fsck -n` (look, change nothing) is allowed"),
    (r"\b(mount|udisksctl)\b" + _END + r"(\brw\b|remount)",
     "cards are only ever opened read-only"),
    (r"(>>?|\btee\b" + _END + r")\s*/dev/(sd|mmcblk|nvme|disk/|hd|vd)",
     "writing to a disk device"),
    (r"\b(rm|mv|cp|rsync|touch|mkdir|rmdir|truncate|tee|chmod|chown|chattr|ln|install|unzip|"
     r"tar|7z|sed|perl|setfattr|fatattr)\b" + _END + CARD_PATHS,
     "it could change a card's files; cards are read-only (look with ls, cat, stat, find)"),
    (r">>?\s*" + CARD_PATHS, "writing onto a card"),
    (r"\b(gsutil|gcloud\s+(alpha\s+|beta\s+)?storage)\b" + _END +
     r"\s(rm|rb|mv|cp|rsync|acl|iam|defacl|lifecycle|retention|objects\s+update|"
     r"buckets\s+(update|delete))\b",
     "only the station uploads to the bucket, and nothing is ever deleted there"),
    (r"\bgcloud\b" + _END + r"\b(iam|projects|organizations|config\s+set|auth\s+(revoke|login)|"
     r"services\s+disable|instances\s+delete|secrets|kms)\b",
     "Google Cloud access and settings are not changed from the station"),
    (r"\bsystemctl\b" + _END + r"\b(stop|kill|disable|mask|reboot|poweroff|halt|suspend|"
     r"hibernate|isolate)\b", "it would stop the station or the computer"),
    (r"\b(reboot|shutdown|poweroff|halt)\b|\binit\s+[06]\b", "it would stop the computer"),
    (r"\b(pkill|killall|kill|xkill)\b",
     "it could stop the uploads; report a stuck process instead"),
    (r"\b(main\.py|sdu)\b" + _END + r"\b(stop|close|rollback)\b",
     "it would end the upload session"),
    (r"\bnmcli\b" + _END + r"\b(networking\s+off|radio\s+\w+\s+off|down|delete)\b",
     "it could cut the internet lines the uploads use"),
    (r"\b(ifdown|ifconfig\s+\S+\s+down)\b|\bip\s+link\s+set\b" + _END + r"\bdown\b",
     "it could cut the internet lines the uploads use"),
    (r"\brm\b" + _END + r"(\.db\b|/database|/var/lib/sd-mass-uploader|/etc/sd-mass-uploader)",
     "the station's database and settings are never deleted"),
    (r"\bsqlite3\b" + _END + r"\b(delete|drop|update|insert|replace|alter|vacuum)\b",
     "the station's database is only read"),
    (r"\brm\s+-\w*r\w*\s+(/|/\*|~|/home|/etc|/usr|/var)(\s|$)", "it deletes the system"),
    (r"\bgit\s+(push|reset\s+--hard|clean|checkout\s+--)\b", "the uploader's code is not changed"),
]]


def guard(command: str) -> str | None:
    """Why a command is refused, or None if Claude may run it."""
    for pattern, why in _REFUSED:
        if pattern.search(command or ""):
            return why
    return None


def guard_file(path: str, app_dir: Path) -> str | None:
    """Claude's Write/Edit tools: never onto a card, a device, the database or the code."""
    text = str(path or "")
    if re.search(CARD_PATHS, text) or text.startswith("/dev/"):
        return "cards and devices are read-only"
    if text.endswith(".db") or "/var/lib/sd-mass-uploader" in text:
        return "the station's database is only read"
    try:
        resolved = Path(text).resolve()
        if (app_dir / "src") in resolved.parents or resolved == app_dir / "main.py":
            return "the uploader's own code is not changed"
    except OSError:
        pass
    return None


def run_guard_hook(stdin_text: str, app_dir: Path) -> tuple[int, str]:
    """Claude Code's PreToolUse hook: (exit code, message). 2 refuses the tool call and tells
    Claude why; 0 lets it run."""
    try:
        event = json.loads(stdin_text or "{}")
    except ValueError:
        return 2, "Refused: the guard could not read this request."
    tool = event.get("tool_name") or ""
    data = event.get("tool_input") or {}
    if tool == "Bash":
        why = guard(str(data.get("command") or ""))
    elif tool in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        why = guard_file(str(data.get("file_path") or data.get("notebook_path") or ""), app_dir)
    else:
        why = None
    if why:
        return 2, (f"Refused by the station's guard: {why}. Find another way that does not do "
                   "this, or stop and say what a person must do.")
    return 0, ""


APP_DIR = Path(__file__).resolve().parents[2]


def find_claude(command: str) -> str:
    """The Claude Code program. The installer puts it in ~/.local/bin, which a service or a
    non-login shell may not have on its PATH; on Windows, npm's `claude.cmd` stands in front of
    the real claude.exe, which is what can be started directly."""
    found = shutil.which(command)
    if found and found.lower().endswith(".cmd"):
        exe = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"
        if (exe / "claude.exe").exists():
            return str(exe / "claude.exe")
    if found:
        return found
    if Path(command).name == command:
        for folder in (Path.home() / ".local" / "bin", Path.home() / ".claude" / "local",
                       Path("/usr/local/bin"), Path("/opt/claude-code/bin")):
            if (folder / command).exists():
                return str(folder / command)
    return command


def guard_main() -> int:
    """`main.py fixer guard`: Claude Code's hook. Any failure refuses (exit 2), never allows."""
    try:
        code, message = run_guard_hook(sys.stdin.read(), APP_DIR)
    except Exception as exc:  # noqa: BLE001 - fail closed
        code, message = 2, f"Refused: the station's guard failed ({exc})."
    if message:
        print(message, file=sys.stderr)
    return code


# ------------------------------------------------------------------ what Claude is told

RULES = """You are fixing an SD card upload station while it is uploading. Hard rules (a guard \
also enforces them; when it refuses, find another way or stop and report):
1. SD cards and card readers are read-only, always. Never format, partition, relabel, repair \
(fsck without -n), write to, or mount read-write any card, and never change a file on one.
2. Never delete, move or overwrite anything in Google Cloud Storage, and never change Google \
Cloud access, projects or settings. Only the station uploads.
3. The uploads must keep running. Never stop, restart or kill the station or any process, \
never end the session, never reboot, never take an internet line down.
4. Never change the uploader's code, its database or its read-only safety settings.
5. Reset or rebind a USB reader only if no card in it is uploading right now.
6. Use `sudo -n` (never wait for a password). If sudo needs a password, say so and stop.
7. Never guess. Diagnose first, make the smallest fix, then check it worked."""


@dataclass
class FixContext:
    """Where things are on this station, for the prompt."""

    python: str
    main: str
    config: str | None
    logs: str
    service: str = "sdu-daemon"

    def station_command(self, words: str) -> str:
        config = f" --config {self.config}" if self.config else ""
        return f"{self.python} {self.main}{config} {words}"


def build_prompt(problems: list[dict], state: dict, context: FixContext) -> str:
    lines = ["The upload station has a problem that has lasted a while. Fix it if a machine can, "
             "without interrupting the uploads in progress.", "", "Problem(s):"]
    for item in problems:
        lines.append(f"- [{item.get('severity')}] {item.get('code')}: {item.get('title')}")
        if item.get("message"):
            lines.append(f"  {item['message']}")
        if item.get("card_id"):
            lines.append(f"  Card: {item['card_id']}")
        if item.get("detail"):
            lines.append(f"  Detail: {str(item['detail'])[:800]}")
        for step in item.get("steps") or []:
            lines.append(f"  Suggested: {step}")
    counters = state.get("counters") or {}
    if counters:
        lines += ["", "Cards now: " + ", ".join(f"{k}={v}" for k, v in counters.items() if v)]
    for card in (state.get("cards") or [])[:40]:
        reason = f" ({card.get('reason')})" if card.get("reason") else ""
        lines.append(f"- {card.get('card_id')}: {card.get('state')}{reason}")
    engine = state.get("engine") or {}
    if engine.get("problem") or engine.get("store_error"):
        lines.append(f"Uploader: {engine.get('problem') or engine.get('store_error')}")
    network = state.get("network") or {}
    for line in network.get("lines") or []:
        lines.append(f"Internet line {line.get('name')} ({line.get('kind')}): {line.get('state')}"
                     f" {line.get('note') or ''}".rstrip())
    lines += [
        "", "Useful here:",
        f"- Station state: {context.station_command('station status')}",
        f"- Every card reader and why it is or is not a card: {context.station_command('check')}",
        f"- Internet lines: {context.station_command('lines')}",
        f"- Retry or resume a card: {context.station_command('station retry CARD_ID')} / "
        f"{context.station_command('station resume CARD_ID')}",
        f"- Every problem code and its fix: {context.station_command('problems')}",
        f"- Logs: {context.logs}, `journalctl -u {context.service} -n 200`, `sudo -n dmesg | "
        "tail -100`",
        "- Hardware: lsblk -o NAME,TRAN,SIZE,RM,FSTYPE,MOUNTPOINTS, lsusb -t, udisksctl status",
        "", "Finish with this report, nothing after it:",
        "FIXED: yes / no / partly",
        "CAUSE: one sentence",
        "WHAT I DID: short bullet points",
        "FOR A PERSON: what someone must still do (power, cable, a broken card), or 'nothing'",
    ]
    return "\n".join(lines)


def guard_settings(hook_command: str) -> dict:
    return {"hooks": {"PreToolUse": [{"matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
                                      "hooks": [{"type": "command", "command": hook_command}]}]}}


# Belt and braces: Claude Code refuses these itself, before the hook is even asked.
DISALLOWED_TOOLS = ",".join([
    "Bash(mkfs:*)", "Bash(sudo mkfs:*)", "Bash(dd:*)", "Bash(sudo dd:*)", "Bash(wipefs:*)",
    "Bash(sudo wipefs:*)", "Bash(fdisk:*)", "Bash(sudo fdisk:*)", "Bash(parted:*)",
    "Bash(sudo parted:*)", "Bash(gsutil rm:*)", "Bash(gcloud storage rm:*)", "Bash(reboot:*)",
    "Bash(sudo reboot:*)", "Bash(shutdown:*)", "Bash(sudo shutdown:*)", "Bash(pkill:*)",
    "Bash(sudo pkill:*)", "Bash(killall:*)", "Bash(sudo killall:*)",
])


# ------------------------------------------------------------------ the fixer

@dataclass
class FixRecord:
    started_at: str
    codes: list[str]
    titles: list[str]
    finished_at: str | None = None
    fixed: str | None = None  # "yes", "no", "partly", or None if it did not finish.
    report: str = ""
    commands: int = 0
    error: str | None = None
    log_file: str | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


Launcher = Callable[[list[str], str], Any]


@dataclass
class _Seen:
    first: float
    problem: dict = field(default_factory=dict)


class ClaudeFixer:
    def __init__(self, settings: ClaudeFixSettings, context: FixContext, log_dir: Path,
                 app_dir: Path, publish: Callable[..., Any] = lambda *_a, **_k: None,
                 launcher: Launcher = start_claude, clock: Callable[[], float] = time.monotonic,
                 say: Callable[[str], None] | None = None) -> None:
        self._settings = settings
        self._context = context
        self._log_dir = log_dir
        self._app_dir = app_dir
        self._publish = publish
        self._launcher = launcher
        self._clock = clock
        self._say = say or self._print
        self._lock = threading.Lock()
        self._seen: dict[str, _Seen] = {}
        self._last_fix: dict[str, float] = {}
        self._starts: deque[float] = deque()
        self._thread: threading.Thread | None = None
        self._process: Any = None
        self._stopping = threading.Event()
        self.history: list[FixRecord] = []

    def _print(self, text: str) -> None:
        if self._settings.show_in_terminal:
            print(f"[claude] {text}", flush=True)
        else:
            logger.info("[claude] %s", text)

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def eligible(self, problem: dict) -> bool:
        rank = SEVERITY_RANK.get(str(problem.get("severity") or "").lower(), 0)
        minimum = SEVERITY_RANK.get(self._settings.min_severity.lower(), 1)
        return rank >= minimum and problem.get("code") not in set(self._settings.skip_codes)

    def observe(self, state: dict) -> FixRecord | None:
        """Called on every station pass. Starts a session when a problem has lasted; returns it."""
        now = self._clock()
        with self._lock:
            current = {str(p.get("id") or p.get("code")): p for p in state.get("problems") or []
                       if self.eligible(p)}
            self._seen = {key: self._seen.get(key, _Seen(now)) for key in current}
            for key, seen in self._seen.items():
                seen.problem = current[key]
            if self.busy or self._stopping.is_set():
                return None
            while self._starts and now - self._starts[0] > 3600:
                self._starts.popleft()
            if len(self._starts) >= self._settings.max_sessions_per_hour:
                return None
            cooldown = self._settings.cooldown_minutes * 60
            due = [seen.problem for seen in self._seen.values()
                   if now - seen.first >= self._settings.wait_seconds
                   and now - self._last_fix.get(seen.problem.get("code"), -1e12) >= cooldown]
            if not due:
                return None
            for problem in due:
                self._last_fix[problem.get("code")] = now
            self._starts.append(now)
            record = FixRecord(_now(), [p.get("code") for p in due], [p.get("title") for p in due])
            self.history.append(record)
            self._thread = threading.Thread(target=self._session, args=(due, state, record),
                                            name="claude-fixer", daemon=True)
            self._thread.start()
            return record

    def run_now(self, problems: list[dict], state: dict | None = None) -> FixRecord:
        """One session in the foreground (`main.py fixer run`)."""
        record = FixRecord(_now(), [p.get("code") for p in problems],
                           [p.get("title") for p in problems])
        self.history.append(record)
        self._session(problems, state or {}, record)
        return record

    def stop(self) -> None:
        self._stopping.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()  # Our own child: Claude Code, never the station.

    # ---------------------------------------------------------------- one session

    def _settings_file(self) -> Path:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        path = self._log_dir / "guard-settings.json"
        hook = f'"{sys.executable}" "{self._context.main}" fixer guard'
        path.write_text(json.dumps(guard_settings(hook), indent=2), encoding="utf-8")
        return path

    def command(self, prompt: str) -> list[str]:
        argv = [find_claude(self._settings.command), "-p", prompt, "--output-format", "stream-json",
                "--verbose", "--max-turns", str(self._settings.max_turns),
                "--permission-mode", "bypassPermissions",
                "--settings", str(self._settings_file()),
                "--append-system-prompt", RULES,
                "--disallowedTools", DISALLOWED_TOOLS]
        if self._settings.model:
            argv += ["--model", self._settings.model]
        return argv

    def _session(self, problems: list[dict], state: dict, record: FixRecord) -> None:
        titles = "; ".join(f"{p.get('code')} {p.get('title')}" for p in problems)
        self._say(f"Working on: {titles}. Uploads continue meanwhile.")
        self._publish("claude_fix_started", codes=record.codes, titles=record.titles)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        log_path = self._log_dir / f"fix-{stamp}-{'_'.join(record.codes)[:60]}.jsonl"
        record.log_file = str(log_path)
        try:
            argv = self.command(build_prompt(problems, state, self._context))
            self._process = self._launcher(argv, str(self._app_dir))
        except Exception as exc:  # noqa: BLE001 - a missing `claude` must not stop the station
            record.error = f"Could not start Claude Code: {exc}"
            self._finish(record)
            return
        timer = threading.Timer(self._settings.timeout_minutes * 60, self._timeout, [record])
        timer.daemon = True
        timer.start()
        try:
            with open(log_path, "a", encoding="utf-8") as log:
                for line in self._process.stdout:
                    log.write(line)
                    self._show(line, record)
            self._process.wait()
        except Exception as exc:  # noqa: BLE001
            record.error = f"Lost Claude Code's output: {exc}"
        finally:
            timer.cancel()
            self._process = None
        self._finish(record)

    def _timeout(self, record: FixRecord) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            record.error = f"Stopped after {self._settings.timeout_minutes:g} minutes"
            process.terminate()

    def _show(self, line: str, record: FixRecord) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            if line.strip():
                self._say(line.rstrip()[:300])
            return
        kind = event.get("type")
        if kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self._say(block["text"].strip()[:600])
                elif block.get("type") == "tool_use":
                    data = block.get("input") or {}
                    shown = data.get("command") or data.get("file_path") or data.get("pattern")
                    text = f"$ {shown}" if block.get("name") == "Bash" else \
                        f"{block.get('name')} {shown or ''}".strip()
                    record.commands += 1
                    self._say(text[:300])
                    self._publish("claude_fix_step", text=text[:200])
        elif kind == "result":
            record.report = str(event.get("result") or "").strip()
            if event.get("is_error") and not record.error:
                record.error = str(event.get("subtype") or "Claude Code reported an error")

    def _finish(self, record: FixRecord) -> None:
        record.finished_at = _now()
        match = re.search(r"FIXED:\s*(yes|no|partly)", record.report, re.IGNORECASE)
        record.fixed = match.group(1).lower() if match else None
        if record.error:
            self._say(f"Stopped: {record.error}")
        if record.report:
            self._say("Report:\n" + record.report)
        self._publish("claude_fix_finished", codes=record.codes, fixed=record.fixed,
                      error=record.error, report=record.report[:2000],
                      commands=record.commands, log=record.log_file)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
