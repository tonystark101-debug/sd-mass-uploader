"""AI troubleshooter: policy, tools, approvals, audit, API and CLI. No real API calls."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.ai.agent import FALLBACK_BETA, AiTroubleshooter, AiUnavailable
from src.ai.policy import Verdict, assert_command_allowed, review_command
from src.ai.tools import DIAGNOSTICS, Toolbox
from src.api.app import ApiServices, create_app
from src.cli import station_commands
from src.core.enums import CardState
from src.core.exceptions import ReadOnlyViolation
from src.database.ai_repository import AiAuditRepository
from src.platform.process import CommandResult, run_reviewed
from src.station.station import StationNotFound
from tests.unit.test_station import METADATA, StationHarness

# ------------------------------------------------------------------ policy

BLOCKED = [
    "", "ls | head", "ls; rm x", "ls && ls", "cat x > y", "echo $(id)", "ls `id`", "sleep 1 &",
    "mkfs.vfat /dev/sdb1", "mkfs -t exfat /dev/sdb1", "fsck /dev/sdb1", "fsck.exfat -y /dev/sdb1",
    "e2fsck -p /dev/sdb1", "dd if=/dev/zero of=/dev/sdb", "wipefs -a /dev/sdb", "parted /dev/sdb",
    "fdisk /dev/sdb", "mount -o rw /dev/sdb1 /mnt", "umount /media/sdu/CARD", "eject /dev/sdb",
    "shred /dev/sdb", "hdparm -r0 /dev/sdb", "blkdiscard /dev/sdb", "chattr -i /media/sdu/X/a",
    "sudo fsck /dev/sdb1", "sudo -u root dd if=/dev/zero of=/dev/sdb", "env A=1 mkfs.vfat x",
    "timeout 5 wipefs /dev/sdb", "nice -n 5 rm -rf /media/sdu/CARD", "nohup parted /dev/sdb",
    "rm /media/sdu/CARD/a.bin", "mv /media/sdu/CARD/a /tmp/a", "touch /media/sdu/CARD/x",
    "chmod 777 /run/media/sdu/CARD", "cp /tmp/x /mnt/card/x", "rm -rf /home/op/sim/CARD01",
    "truncate -s0 /media/x/y", "find /media/sdu -delete", "find /media/sdu -exec rm {} ;",
    "bash -c ls", "sh", "python3 -c print(1)", "perl -e 1", "awk {print} x", "xargs rm",
    "ssh host", "vim /etc/x", "sudo bash", "udisksctl unmount -b /dev/sdb1",
    "udisksctl mount -b /dev/sdb1", "udisksctl power-off -b /dev/sdb", "smartctl -t long /dev/sdb",
    "cat /etc/sd-mass-uploader/credentials.json", "cat data/station/api-token",
    "cat ~/.config/gcloud/application_default_credentials.json", "env", "printenv",
    "sudo env", "cat /proc/1/environ", "cat /etc/shadow", "sudo dd",
]
NEEDS_APPROVAL = [
    "lsusb -t", "ip addr", "systemctl restart udisks2", "sudo systemctl restart sdu-daemon",
    "ls -la /media/sdu/CARD", "stat /media/sdu/CARD/a.bin", "sha256sum /media/sdu/CARD/a.bin",
    "blkid /dev/sdb1", "lsblk /dev/sdb", "find /media/sdu/CARD -name *.enc",
    "udisksctl status", "ping -c 3 storage.googleapis.com", "journalctl -u udisks2 -n 50",
    "smartctl -i /dev/sdb", "df -h", "ls /home/op/sim/CARD01",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_policy_blocks_card_writes_shells_and_secrets(command):
    review = review_command(command, ["/home/op/sim/CARD01"])
    assert review.verdict is Verdict.BLOCKED, (command, review.reason)


@pytest.mark.parametrize("command", NEEDS_APPROVAL)
def test_policy_lets_other_commands_wait_for_approval(command):
    review = review_command(command, ["/home/op/sim/CARD01"])
    assert review.verdict is Verdict.NEEDS_APPROVAL, (command, review.reason)
    assert review.argv


def test_runner_checks_policy_before_starting_anything():
    with pytest.raises(ReadOnlyViolation):
        run_reviewed(["mkfs.vfat", "/dev/sdb1"], assert_command_allowed)
    with pytest.raises(ReadOnlyViolation):
        assert_command_allowed(["rm", "/media/sdu/CARD/a"])


def test_every_fixed_diagnostic_passes_the_policy():
    for argv in DIAGNOSTICS.values():
        assert_command_allowed(argv)


# ------------------------------------------------------------------ fakes

class FakeRunner:
    def __init__(self, returncode=0, stdout="ok\n"):
        self.calls = []
        self.returncode, self.stdout = returncode, stdout

    def __call__(self, argv, check, timeout=60):
        check(argv)  # The real runner always calls the policy first.
        self.calls.append(argv)
        return CommandResult(self.returncode, self.stdout, "")


class Gate:
    def __init__(self, answer=True):
        self.answer = answer
        self.requests = []

    def request_approval(self, kind, summary, reason):
        self.requests.append((kind, summary))
        return self.answer


def text(value):
    return SimpleNamespace(type="text", text=value)


def tool(name, args, call_id=None):
    return SimpleNamespace(type="tool_use", id=call_id or f"call-{name}", name=name, input=args)


def response(*blocks, stop=None, tokens=100):
    stop = stop or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    usage = SimpleNamespace(input_tokens=tokens, output_tokens=10, cache_read_input_tokens=0,
                            cache_creation_input_tokens=None)
    return SimpleNamespace(content=list(blocks), stop_reason=stop, usage=usage)


class FakeClaude:
    """Stands in for anthropic.Anthropic(): returns scripted responses, records each request."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.lock = threading.Lock()
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        with self.lock:
            self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
            item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def harness(tmp_path):
    h = StationHarness(tmp_path)
    h.station.open_session({**METADATA, "operator_name": "Asha Rao", "place_of_recording":
                            "Hampi"})
    yield h
    h.close()


def make_ai(harness, *script, runner=None, **ai_options):
    for key, value in ai_options.items():
        setattr(harness.settings.ai, key, value)
    runner = runner or FakeRunner()
    toolbox = Toolbox(harness.station, harness.settings, runner)
    fake = FakeClaude(*script)
    ai = AiTroubleshooter(harness.settings, harness.station, AiAuditRepository(harness.db),
                          client_factory=lambda: fake, toolbox=toolbox)
    return ai, fake, runner


def wait_status(ai, session_id, *statuses, timeout=30):  # First anthropic import is slow.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = ai.get(session_id)
        if view["status"] in statuses:
            return view
        time.sleep(0.02)
    raise AssertionError(f"status stayed {ai.get(session_id)['status']}")


# ------------------------------------------------------------------ toolbox

def test_state_tool_hides_personal_details(harness):
    toolbox = Toolbox(harness.station, harness.settings, FakeRunner())
    result = toolbox.execute("get_station_state", {}, Gate())
    assert "Asha Rao" not in result.content and "Hampi" not in result.content
    assert "[redacted]" in result.content and "Survey" in result.content


def test_blocked_command_is_never_offered_for_approval(harness):
    runner, gate = FakeRunner(), Gate()
    result = Toolbox(harness.station, harness.settings, runner).execute(
        "run_command", {"command": "mkfs.vfat /dev/sdb1", "reason": "repair"}, gate)
    assert result.is_error and result.verdict == "blocked" and "cannot be approved" in \
        result.content
    assert gate.requests == [] and runner.calls == []


def test_command_runs_only_after_approval(harness):
    runner = FakeRunner(stdout="active (running)\n")
    toolbox = Toolbox(harness.station, harness.settings, runner)
    declined = toolbox.execute("run_command", {"command": "systemctl restart udisks2",
                                               "reason": "x"}, Gate(answer=False))
    assert declined.verdict == "declined" and runner.calls == []
    gate = Gate()
    approved = toolbox.execute("run_command", {"command": "systemctl restart udisks2",
                                               "reason": "restart the disk service"}, gate)
    assert gate.requests == [("command", "systemctl restart udisks2")]
    assert runner.calls == [["systemctl", "restart", "udisks2"]]
    assert approved.verdict == "approved" and "active (running)" in approved.content


def test_station_actions_follow_the_setting_and_confirm_always_asks(harness):
    harness.card("A", 1)
    harness.wait_states(A=CardState.UPLOADED)
    toolbox = Toolbox(harness.station, harness.settings, FakeRunner())
    gate = Gate()
    assert not toolbox.execute("pause_card", {"card_id": "sim-A"}, gate).is_error
    assert gate.requests == []  # auto_station_actions is on by default.
    harness.settings.ai.auto_station_actions = False
    result = toolbox.execute("resume_card", {"card_id": "sim-A"}, Gate(answer=False))
    assert result.verdict == "declined"
    gate = Gate(answer=False)
    toolbox.execute("confirm_card", {"card_id": "sim-A"}, gate)
    assert gate.requests[0][0] == "confirm_card"
    missing = toolbox.execute("retry_card", {"card_id": "nope"}, Gate())
    assert missing.is_error and "Unknown card" in missing.content


def test_diagnostics_and_log_reading(harness):
    runner = FakeRunner(stdout="Bus 02.Port 1: Dev 1, Class=root_hub, 10000M\n")
    toolbox = Toolbox(harness.station, harness.settings, runner)
    result = toolbox.execute("run_diagnostic", {"name": "usb_tree"}, Gate())
    assert runner.calls == [["lsusb", "-t"]] and "10000M" in result.content
    assert toolbox.execute("run_diagnostic", {"name": "rm"}, Gate()).is_error

    log = harness.settings.resolve(harness.settings.logging.directory)
    log.mkdir(parents=True, exist_ok=True)
    (log / "uploader.log").write_text("a ok\nb ERROR card by Asha Rao\nc ERROR again\n",
                                      encoding="utf-8")
    tail = toolbox.execute("read_station_log", {"lines": 5, "contains": "ERROR"}, Gate())
    assert tail.content.splitlines() == ["b ERROR card by [redacted]", "c ERROR again"]
    assert toolbox.execute("nope", {}, Gate()).is_error


# ------------------------------------------------------------------ agent loop

def test_full_conversation_with_an_approved_command(harness):
    ai, fake, runner = make_ai(
        harness,
        response(text("Let me look."), tool("get_station_state", {}, "c1"),
                 tool("run_command", {"command": "systemctl restart udisks2",
                                      "reason": "Restart the disk service"}, "c2")),
        response(text("Fixed: the disk service was stuck. I restarted it. Next: nothing.")),
    )
    session_id = ai.start(question="Cards are not appearing", started_by="op1")
    waiting = wait_status(ai, session_id, "waiting_approval")
    approval = waiting["pending_approval"]
    assert approval["summary"] == "systemctl restart udisks2"
    assert runner.calls == []
    ai.decide(session_id, approval["approval_id"], True, by="op1")
    done = wait_status(ai, session_id, "idle")
    assert runner.calls == [["systemctl", "restart", "udisks2"]]
    kinds = [entry["kind"] for entry in done["transcript"]]
    assert kinds[0] == "operator" and kinds[-1] == "assistant"
    assert "Fixed:" in done["transcript"][-1]["text"]

    first, second = fake.requests
    assert first["model"] == "claude-opus-5-5" and "thinking" not in first
    assert first["betas"] == [FALLBACK_BETA] and first["fallbacks"] == "default"
    assert first["output_config"] == {"effort": "high"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert {t["name"] for t in first["tools"]} >= {"run_command", "get_station_state"}
    results = second["messages"][-1]
    assert results["role"] == "user"
    assert [r["tool_use_id"] for r in results["content"]] == ["c1", "c2"]  # One message.
    assert second["messages"][-2]["content"] is not None  # Assistant content passed back as is.

    audit = [(row["kind"], row["verdict"]) for row in
             AiAuditRepository(harness.db).for_session(session_id)]
    assert ("approval_decided", "approved") in audit
    assert audit[0][0] == "started" and audit[-1] == ("turn_finished", "idle")
    assert done["tokens_used"] == 220


def test_blocked_command_goes_back_to_the_ai_without_asking(harness):
    ai, fake, runner = make_ai(
        harness,
        response(tool("run_command", {"command": "fsck -y /dev/sdb1", "reason": "repair"})),
        response(text("I cannot repair cards. Set this card aside.")),
    )
    session_id = ai.start(question="card broken")
    done = wait_status(ai, session_id, "idle")
    assert runner.calls == [] and done["pending_approval"] is None
    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "safety policy" in result["content"]


def test_decline_stop_and_timeout_never_run_the_command(harness):
    command = response(tool("run_command", {"command": "systemctl restart udisks2",
                                            "reason": "x"}))
    final = response(text("OK, I will not run it."))
    ai, _, runner = make_ai(harness, command, final)
    session_id = ai.start(question="q")
    approval = wait_status(ai, session_id, "waiting_approval")["pending_approval"]
    ai.decide(session_id, approval["approval_id"], False)
    wait_status(ai, session_id, "idle")

    ai2, _, runner2 = make_ai(harness, command, final)
    session_id = ai2.start(question="q")
    wait_status(ai2, session_id, "waiting_approval")
    ai2.stop(session_id)
    assert wait_status(ai2, session_id, "stopped")["status"] == "stopped"

    ai3, _, runner3 = make_ai(harness, command, final, approval_timeout_seconds=0.1)
    session_id = ai3.start(question="q")
    done = wait_status(ai3, session_id, "idle")
    assert any("Expired" in entry["text"] for entry in done["transcript"])
    assert runner.calls == runner2.calls == runner3.calls == []


def test_wrong_or_late_approval_is_refused(harness):
    ai, _, _ = make_ai(harness, response(tool("run_command", {"command": "ls /tmp",
                                                              "reason": "x"})),
                       response(text("done")))
    session_id = ai.start(question="q")
    approval = wait_status(ai, session_id, "waiting_approval")["pending_approval"]
    with pytest.raises(Exception, match="no longer waiting"):
        ai.decide(session_id, "wrong-id", True)
    ai.decide(session_id, approval["approval_id"], True)
    wait_status(ai, session_id, "idle")
    with pytest.raises(Exception, match="no longer waiting"):
        ai.decide(session_id, approval["approval_id"], True)


def test_refusal_budget_and_step_limits(harness):
    ai, _, _ = make_ai(harness, response(stop="refusal"))
    done = wait_status(ai, ai.start(question="q"), "idle")
    assert "declined to continue" in done["transcript"][-1]["text"]

    looping = response(tool("list_problems", {}), tokens=600)
    ai, fake, _ = make_ai(harness, looping, token_budget=1000)
    done = wait_status(ai, ai.start(question="q"), "idle")
    assert "token budget" in done["transcript"][-1]["text"] and len(fake.requests) == 2

    ai, fake, _ = make_ai(harness, response(tool("list_problems", {}), tokens=1),
                          max_iterations=3, token_budget=10_000_000)
    done = wait_status(ai, ai.start(question="q"), "idle")
    assert "Stopped after 3 steps" in done["transcript"][-1]["text"]
    assert len(fake.requests) == 3


def test_service_errors_and_follow_up(harness):
    ai, fake, _ = make_ai(harness, RuntimeError("socket closed"))
    session_id = ai.start(question="q")
    failed = wait_status(ai, session_id, "failed")
    assert "The AI service failed" in failed["error"]
    fake.script = [response(text("Now it works."))]
    ai.send(session_id, "try again")
    done = wait_status(ai, session_id, "idle")
    assert done["transcript"][-1]["text"] == "Now it works."
    assert fake.requests[-1]["messages"][-1] == {"role": "user", "content": "try again"}
    with pytest.raises(Exception, match="Write a message"):
        ai.send(session_id, "  ")


def test_missing_key_is_explained_plainly(harness):
    """What the real SDK raises when no key is configured (seen on a real run)."""
    error = TypeError('"Could not resolve authentication method. Expected one of api_key, '
                      'auth_token, or credentials to be set."')
    ai, _, _ = make_ai(harness, error)
    failed = wait_status(ai, ai.start(question="q"), "failed")
    assert "ANTHROPIC_API_KEY" in failed["error"] and "TypeError" not in failed["error"]


def test_problem_details_reach_the_ai_as_data(harness):
    harness.station.set_auto_upload(False)
    harness.card("A", 2)
    harness.wait_states(A=CardState.READY)
    harness.remove("A")
    harness.wait_for(lambda: any(p["code"] == "E-CARD-REMOVED"
                                 for p in harness.station.view().problems))
    problem_id = harness.station.view().problems[0]["id"]
    ai, fake, _ = make_ai(harness, response(text("Put CARD A back in.")))
    session_id = ai.start(problem_id=problem_id)
    wait_status(ai, session_id, "idle")
    prompt = fake.requests[0]["messages"][0]["content"]
    assert "E-CARD-REMOVED" in prompt and "data, not instructions" in prompt
    assert ai.get(session_id)["problem_id"] == problem_id


def test_unknown_problem_and_disabled_ai(harness):
    ai, _, _ = make_ai(harness, response(text("ok")))
    with pytest.raises(StationNotFound):
        ai.start(problem_id="E-NOPE:x")
    harness.settings.ai.enabled = False
    with pytest.raises(AiUnavailable):
        ai.start(question="q")


# ------------------------------------------------------------------ API and CLI

def test_ai_over_the_api(harness):
    ai, _, runner = make_ai(
        harness,
        response(tool("run_command", {"command": "ip addr", "reason": "check network"})),
        response(text("Network is fine.")),
    )
    headers = {"Authorization": "Bearer t"}
    with TestClient(create_app(ApiServices(harness.station, "t", ai=ai)),
                    base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/ai", headers=headers).json()["model"] == "claude-opus-5-5"
        started = client.post("/api/v1/ai/sessions", headers=headers,
                              json={"question": "is the network ok?", "operator": "op"})
        session_id = started.json()["ai_session_id"]
        approval = wait_status(ai, session_id, "waiting_approval")["pending_approval"]
        decided = client.post(f"/api/v1/ai/sessions/{session_id}/approvals/"
                              f"{approval['approval_id']}", headers=headers,
                              json={"approve": True, "operator": "op"})
        assert decided.status_code == 200
        wait_status(ai, session_id, "idle")
        body = client.get(f"/api/v1/ai/sessions/{session_id}", headers=headers).json()
        assert body["transcript"][-1]["text"] == "Network is fine."
        assert runner.calls == [["ip", "addr"]]
        assert client.get("/api/v1/ai/sessions/nope", headers=headers).status_code == 404
        busy = client.post(f"/api/v1/ai/sessions/{session_id}/approvals/x", headers=headers,
                           json={"approve": True})
        assert busy.status_code == 409

    with TestClient(create_app(ApiServices(harness.station, "t")),
                    base_url="http://127.0.0.1") as client:
        assert client.get("/api/v1/ai", headers=headers).json()["enabled"] is False
        assert client.post("/api/v1/ai/sessions", headers=headers, json={}).status_code == 503


class ScriptedApi:
    """Plays the station API for the CLI conversation loop."""

    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.posts = []

    def request(self, method, path, body=None, timeout=None):
        if method == "POST":
            self.posts.append((path, body))
            return 200, {"ok": True}
        return 200, self.sessions.pop(0) if len(self.sessions) > 1 else self.sessions[0]


def test_cli_conversation_asks_before_running(monkeypatch, capsys):
    entries = [
        {"kind": "operator", "text": "help"},
        {"kind": "tool_call", "text": "Wants to run: ip addr  (check)"},
    ]
    pending = {"approval_id": "a1", "summary": "ip addr", "reason": "check network"}
    sessions = [
        {"status": "waiting_approval", "transcript": entries, "pending_approval": pending},
        {"status": "idle", "pending_approval": None, "transcript": [
            *entries, {"kind": "tool_result", "text": "eth0 UP", "verdict": "approved"},
            {"kind": "assistant", "text": "All good."}]},
    ]
    api = ScriptedApi(sessions)
    answers = iter(["y", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert station_commands._follow(api, "ai-1", "op", no_follow_up=False) == 0
    assert api.posts == [("/api/v1/ai/sessions/ai-1/approvals/a1",
                          {"approve": True, "operator": "op"})]
    out = capsys.readouterr().out
    assert "The AI wants to: ip addr" in out and "AI: All good." in out
