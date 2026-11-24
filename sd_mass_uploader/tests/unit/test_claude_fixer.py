"""Claude Code fixes station problems by itself; the guard keeps footage and uploads safe."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.ai.claude_fixer import (
    APP_DIR,
    ClaudeFixer,
    FixContext,
    build_prompt,
    guard,
    run_guard_hook,
)
from src.core.exceptions import ReadOnlyViolation
from src.core.settings import ClaudeFixSettings
from src.platform.process import probe, start_claude

REFUSED = [
    "sudo mkfs.vfat /dev/sdb1", "/sbin/mkfs.exfat -n CARD /dev/sdc1", "sudo wipefs -a /dev/sdd",
    "sudo dd if=/dev/zero of=/dev/sdb bs=1M", "sudo fdisk /dev/sdb", "parted /dev/sdb mklabel gpt",
    "sudo fsck.vfat -a /dev/sdb1", "sudo dosfsck -w /dev/sdb1", "fsck -y /dev/sdc1",
    "sudo mount -o remount,rw /media/sdu/CARD", "udisksctl mount -b /dev/sdb1 -o rw",
    "rm -rf /media/sdu/CARD/DCIM", "mv /run/media/op/CARD/a.mp4 /tmp/",
    "echo x > /media/sdu/CARD/test", "sudo tee /dev/sdb < img",
    "gsutil rm -r gs://peak-lake-raw/gilabs/", "gcloud storage rm gs://b/x",
    "gcloud storage cp a gs://b/a", "gcloud projects add-iam-policy-binding p --member x",
    "sudo systemctl stop sdu-daemon", "sudo systemctl restart sdu-daemon && systemctl kill x",
    "sudo reboot", "sudo shutdown -h now", "pkill -f main.py", "kill -9 1234",
    "python main.py station close --force", "sdu stop",
    "nmcli networking off", "sudo ip link set enx001122 down", "sudo ifdown eth0",
    "rm /var/lib/sd-mass-uploader/database/uploader.db",
    "sqlite3 data/database/uploader.db 'DELETE FROM sessions'",
    'bash -c "sudo mkfs.vfat /dev/sdb1"', "sudo rm -rf /",
]

ALLOWED = [
    "lsblk -o NAME,TRAN,SIZE,RM,FSTYPE,MOUNTPOINTS", "lsusb -t", "sudo -n dmesg | tail -100",
    "udisksctl status", "sudo -n udisksctl mount -b /dev/sdc1 -o ro,noatime --no-user-interaction",
    "ls -la /media/sdu/CARD", "cat /media/sdu/CARD/session.json", "stat /run/media/op/CARD",
    "sudo -n fsck.vfat -n /dev/sdb1", "journalctl -u sdu-daemon -n 200",
    "python main.py station status", "python main.py station retry card-1A2B",
    "python main.py check", "sudo -n apt-get install -y exfatprogs",
    "sudo -n tee /etc/polkit-1/rules.d/50-sd-mass-uploader.rules < rules",
    "sudo -n udevadm control --reload && sudo -n udevadm trigger --subsystem-match=block",
    "nmcli con up 'Wired connection 2'", "gsutil ls gs://peak-lake-raw/gilabs/",
    "ip -br addr", "df -h", "sqlite3 data/database/uploader.db 'SELECT count(*) FROM files'",
    "systemctl status sdu-daemon",
]


@pytest.mark.parametrize("command", REFUSED)
def test_the_guard_refuses_anything_that_could_lose_footage_or_stop_uploads(command):
    assert guard(command), command


@pytest.mark.parametrize("command", ALLOWED)
def test_the_guard_lets_diagnosis_and_safe_fixes_through(command):
    assert guard(command) is None, (command, guard(command))


def test_hook_refuses_file_edits_on_cards_and_code_and_fails_closed():
    def hook(tool, **data):
        return run_guard_hook(json.dumps({"tool_name": tool, "tool_input": data}), APP_DIR)

    assert hook("Bash", command="lsusb -t") == (0, "")
    code, message = hook("Bash", command="sudo mkfs.vfat /dev/sdb1")
    assert code == 2 and "Refused by the station's guard" in message
    assert hook("Write", file_path="/media/sdu/CARD/x.txt")[0] == 2
    assert hook("Edit", file_path=str(APP_DIR / "src" / "uploader" / "worker.py"))[0] == 2
    assert hook("Write", file_path="/etc/polkit-1/rules.d/50-x.rules")[0] == 0
    assert run_guard_hook("not json", APP_DIR)[0] == 2


def test_the_real_hook_command_refuses_before_any_config_loads(tmp_path):
    request = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "sudo wipefs -a /dev/sdb"}})
    done = subprocess.run([sys.executable, str(APP_DIR / "main.py"), "fixer", "guard"],
                          input=request, capture_output=True, text=True, cwd=tmp_path,
                          timeout=60)
    assert done.returncode == 2 and "Refused" in done.stderr
    ok = json.dumps({"tool_name": "Bash", "tool_input": {"command": "lsblk"}})
    done = subprocess.run([sys.executable, str(APP_DIR / "main.py"), "fixer", "guard"],
                          input=ok, capture_output=True, text=True, cwd=tmp_path, timeout=60)
    assert done.returncode == 0


def test_only_claude_code_in_print_mode_with_the_guard_may_start():
    with pytest.raises(ReadOnlyViolation):
        start_claude(["bash", "-p", "x", "--settings", "s"], ".")
    with pytest.raises(ReadOnlyViolation):
        start_claude(["claude", "do things"], ".")
    with pytest.raises(ReadOnlyViolation):
        start_claude(["claude", "-p", "x"], ".")  # No guard settings.
    with pytest.raises(ReadOnlyViolation):
        probe(["sudo", "rm", "-rf", "/"])


# ------------------------------------------------------------------ when Claude is started

class FakeClaude:
    """Claude Code's stream-json output for one short session."""

    def __init__(self, argv, cwd):
        self.argv, self.cwd = argv, cwd
        report = ("FIXED: yes\nCAUSE: the polkit rule did not allow system mounts\n"
                  "WHAT I DID: - added filesystem-mount-system\nFOR A PERSON: nothing")
        events = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Looking at the readers."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "lsblk"}}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "python main.py check"}}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": report},
        ]
        self.stdout = iter(json.dumps(event) + "\n" for event in events)
        self.terminated = False

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True


class Harness:
    def __init__(self, tmp_path, **settings):
        self.now = 0.0
        self.started = []
        self.events = []
        self.said = []
        self.settings = ClaudeFixSettings(enabled=True, **settings)

        def launch(argv, cwd):
            process = FakeClaude(argv, cwd)
            self.started.append(process)
            return process

        self.fixer = ClaudeFixer(
            self.settings, FixContext("python3", str(APP_DIR / "main.py"), None, "logs"),
            tmp_path / "claude-fixes", APP_DIR,
            publish=lambda kind, **data: self.events.append((kind, data)),
            launcher=launch, clock=lambda: self.now, say=self.said.append)

    def tick(self, problems, seconds=5):
        self.now += seconds
        record = self.fixer.observe({"problems": problems, "counters": {"uploading": 3}})
        if record is not None:
            self.fixer._thread.join(5)
        return record


MOUNT = {"id": "p1", "code": "E-MOUNT-FAILED", "severity": "error",
         "title": "A card could not be opened", "message": "/dev/sdc1: Not authorized"}


def test_a_lasting_problem_starts_one_claude_session_and_reports_back(tmp_path):
    h = Harness(tmp_path, wait_seconds=45)
    assert h.tick([MOUNT], 0) is None  # Just appeared: most problems clear by themselves.
    assert h.tick([MOUNT], 30) is None
    record = h.tick([MOUNT], 20)
    assert record is not None and len(h.started) == 1
    argv = h.started[0].argv
    assert Path(argv[0]).stem == "claude" and argv[1] == "-p"
    assert "E-MOUNT-FAILED" in argv[2]
    assert "--permission-mode" in argv and "bypassPermissions" in argv
    settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert hook.endswith("fixer guard")
    assert "mkfs" in argv[argv.index("--disallowedTools") + 1]
    assert record.fixed == "yes" and record.commands == 2
    assert Path(record.log_file).exists()
    kinds = [kind for kind, _ in h.events]
    assert kinds == ["claude_fix_started", "claude_fix_step", "claude_fix_step",
                     "claude_fix_finished"]
    assert any("Uploads continue" in text for text in h.said)


def test_the_same_problem_waits_out_its_cooldown(tmp_path):
    h = Harness(tmp_path, wait_seconds=0, cooldown_minutes=20)
    assert h.tick([MOUNT]) is not None
    assert h.tick([MOUNT], 60) is None
    assert h.tick([MOUNT], 20 * 60) is not None


def test_problems_only_a_person_can_settle_are_left_alone(tmp_path):
    h = Harness(tmp_path, wait_seconds=0)
    decision = {"id": "d", "code": "W-CARD-DECISION", "severity": "warning",
                "title": "Which device recorded this card?"}
    info = {"id": "i", "code": "I-SKIPPED", "severity": "info", "title": "Skipped"}
    safety = {"id": "s", "code": "E-SAFETY-BLOCK", "severity": "critical", "title": "Blocked"}
    assert h.tick([decision, info, safety]) is None and h.started == []


def test_at_most_a_few_sessions_an_hour(tmp_path):
    h = Harness(tmp_path, wait_seconds=0, cooldown_minutes=0, max_sessions_per_hour=2)
    assert h.tick([MOUNT]) is not None
    assert h.tick([MOUNT]) is not None
    assert h.tick([MOUNT]) is None
    assert h.tick([MOUNT], 3600) is not None


def test_a_missing_claude_never_stops_the_station(tmp_path):
    h = Harness(tmp_path, wait_seconds=0)

    def broken(argv, cwd):
        raise FileNotFoundError("claude")

    h.fixer._launcher = broken
    record = h.tick([MOUNT])
    assert record.error.startswith("Could not start Claude Code")
    assert h.events[-1][0] == "claude_fix_finished"


def test_the_prompt_carries_the_problem_the_state_and_the_rules():
    context = FixContext("python3", "/opt/sdu/main.py", "/etc/sd-mass-uploader/config.yaml",
                         "/var/log/sdu")
    state = {"counters": {"uploading": 2, "failed": 0},
             "cards": [{"card_id": "card-1", "state": "UPLOADING"}],
             "network": {"lines": [{"name": "enx0011", "kind": "Ethernet (USB)",
                                    "state": "active"}]}}
    text = build_prompt([MOUNT], state, context)
    assert "E-MOUNT-FAILED" in text and "card-1: UPLOADING" in text
    assert "--config /etc/sd-mass-uploader/config.yaml check" in text
    assert "Ethernet (USB)" in text and "FIXED: yes / no / partly" in text
