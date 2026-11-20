"""Desktop companion: indicator contents, notification rules, the feed, the window and its guard.

The GTK drawing itself runs only on Linux; everything it shows is decided by the code tested here.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from src.api.client import ApiUnavailable
from src.cli.commands import main
from src.core.exceptions import ReadOnlyViolation
from src.desktop.feed import StationFeed
from src.desktop.model import (
    ICON_BUSY,
    ICON_IDLE,
    ICON_OFFLINE,
    ICON_OK,
    ICON_PROBLEM,
    NotificationPolicy,
    indicator_model,
)
from src.desktop.window import open_console
from src.platform import process


def state(**counters):
    base = {"inserted": 0, "total": 0, "uploading": 0, "uploaded": 0, "failed": 0,
            "paused": 0, "removed": 0, "needs_confirmation": 0, "needs_decision": 0}
    return {
        "session": {"session_id": "S1", "paused_all": counters.pop("paused_all", False)},
        "counters": {**base, **counters},
        "progress": {"bytes_total": 1000, "bytes_done": 996},
        "problems": [],
        "cards": [],
        "daemon": {"started_at": "t0"},
    }


# ------------------------------------------------------------------ indicator

def test_indicator_when_offline_forbidden_or_idle():
    offline = indicator_model(None, "offline")
    assert offline.icon == ICON_OFFLINE and "not answering" in offline.tooltip
    assert [item.action for item in offline.menu if item] == [None, "open_console", "quit"]
    forbidden = indicator_model(None, "forbidden")
    assert "may not control" in forbidden.tooltip
    idle = indicator_model({"session": None, "problems": []}, "live")
    assert idle.icon == ICON_IDLE and idle.label == ""


def test_indicator_counts_problems_and_actions():
    current = state(inserted=6, total=8, uploading=2, uploaded=5, failed=1, needs_decision=1)
    current["problems"] = [
        {"severity": "warning", "title": "Which device recorded this card?"},
        {"severity": "error", "title": "Card removed before it finished"},
    ]
    model = indicator_model(current, "live")
    assert model.label == "2↑ 5✓ 1✕ 1?"
    assert model.icon == ICON_PROBLEM and "1 problem(s)" in model.tooltip
    labels = [item.label for item in model.menu if item]
    assert "99% of 1.0 KB" in labels  # Rounded down: 100% only when finished.
    assert "⚠ Card removed before it finished (+1 more)" in labels
    assert "Pause all" in labels and "Resume all" not in labels
    paused = indicator_model(state(uploaded=1, paused_all=True), "live")
    assert "Resume all" in [item.label for item in paused.menu if item]
    assert paused.icon == ICON_OK and paused.label == "1✓"
    assert indicator_model(state(uploading=1), "live").icon == ICON_BUSY


# ------------------------------------------------------------------ notifications

class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_only_what_needs_a_person_is_notified():
    policy = NotificationPolicy(clock=Clock())
    shown = policy.decide([
        {"kind": "problem_opened", "data": {"id": "a", "severity": "critical",
                                            "title": "Google sign-in is not valid"}},
        {"kind": "problem_opened", "data": {"id": "b", "severity": "warning", "title": "x"}},
        {"kind": "problem_opened", "data": {"id": "c", "severity": "error",
                                            "title": "Card removed before it finished"}},
        {"kind": "card_inserted", "data": {"card_id": "x"}},
        {"kind": "card_state", "data": {"card_id": "x", "state": "UPLOADING"}},
    ])
    assert [(n.title, n.urgency) for n in shown] == [
        ("Uploads stopped: Google sign-in is not valid", "critical"),
        ("Needs you: Card removed before it finished", "normal")]


def test_finished_cards_are_grouped_and_repeats_dropped():
    clock = Clock()
    policy = NotificationPolicy(quiet_seconds=60, clock=clock)
    one = policy.decide([{"kind": "card_state", "data": {"card_id": "sim-A", "state": "UPLOADED"}}],
                        {"cards": [{"card_id": "sim-A", "label": "CARD01"}]})
    assert [n.title for n in one] == ["CARD01 uploaded"] and "safe to remove" in one[0].body
    many = policy.decide([{"kind": "card_state", "data": {"card_id": c, "state": "UPLOADED"}}
                          for c in ("b", "c", "d")])
    assert [n.title for n in many] == ["3 cards uploaded"]
    problem = {"kind": "problem_opened", "data": {"id": "p", "severity": "error", "title": "t"}}
    assert len(policy.decide([problem])) == 1
    assert policy.decide([problem]) == []  # Same problem again within the quiet period.
    clock.now = 61
    assert len(policy.decide([problem])) == 1
    closed = policy.decide([{"kind": "session_closed", "data": {"session_id": "S",
                                                                "status": "SUCCESS"}}])
    assert closed[0].title == "Session ended" and closed[0].urgency == "low"


# ------------------------------------------------------------------ feed

class ScriptedClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.paths = []

    def request(self, method, path, body=None, timeout=None):
        self.paths.append(path)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_feed_skips_old_news_and_handles_restarts():
    old = [{"kind": "card_inserted", "sequence": 1}, {"kind": "card_state", "sequence": 2}]
    new = [{"kind": "problem_opened", "sequence": 3}]
    restarted = {**state(), "daemon": {"started_at": "t1"}}
    client = ScriptedClient([(200, state()), (200, old), (200, state()), (200, new),
                             (200, restarted), (200, [{"kind": "x", "sequence": 1}]),
                             (200, restarted), (200, [])])
    feed = StationFeed(lambda: client)
    first = feed.poll()
    assert first.connection == "live" and first.events == []  # Before we started: skipped.
    assert feed.poll().events == new
    assert client.paths[3] == "/api/v1/events?after=2"
    assert feed.poll().events == []  # The station restarted: its old events are skipped too.
    feed.poll()
    assert client.paths[-1] == "/api/v1/events?after=1"


def test_feed_connection_states():
    assert StationFeed(lambda: None).poll().connection == "forbidden"
    assert StationFeed(lambda: ScriptedClient([(401, {})])).poll().connection == "forbidden"
    assert StationFeed(lambda: ScriptedClient([ApiUnavailable("down")])).poll().connection \
        == "offline"
    assert StationFeed(lambda: ScriptedClient([(500, {})])).poll().connection == "offline"


# ------------------------------------------------------------------ window and launcher

def test_window_uses_webview_or_falls_back_to_browser(monkeypatch):
    calls = []
    fake = SimpleNamespace(create_window=lambda *a, **k: calls.append(("create", a, k)),
                           start=lambda **k: calls.append(("start", k)))
    assert open_console("http://127.0.0.1:1/#token=x", fullscreen=True, webview_module=fake) \
        == "window"
    assert calls[0][1] == ("SD Uploader", "http://127.0.0.1:1/#token=x")
    assert calls[0][2]["fullscreen"] is True
    assert calls[1][1]["gui"] == ("gtk" if sys.platform.startswith("linux") else None)

    import builtins

    real_import = builtins.__import__

    def no_webview(name, *args, **kwargs):
        if name == "webview":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    opened = []
    monkeypatch.setattr(builtins, "__import__", no_webview)
    monkeypatch.setattr("webbrowser.open", lambda link: opened.append(link) or True)
    assert open_console("http://x/#token=y") == "browser" and opened == ["http://x/#token=y"]


def test_only_the_console_window_can_be_launched(monkeypatch):
    started = []
    monkeypatch.setattr(process.subprocess, "Popen", lambda argv, **k: started.append(argv))
    good = [sys.executable, "/opt/app/main.py", "--config", "/etc/c.yaml", "desktop", "--window"]
    process.launch_console_window(good)
    process.launch_console_window([sys.executable, "/opt/app/main.py", "desktop", "--window"])
    assert len(started) == 2
    for bad in (
        ["/bin/sh", "/opt/app/main.py", "desktop", "--window"],
        [sys.executable, "/opt/app/evil.py", "desktop", "--window"],
        [sys.executable, "/opt/app/main.py", "daemon"],
        [sys.executable, "/opt/app/main.py", "--yes", "x", "desktop", "--window"],
        [sys.executable, "/opt/app/main.py", "--config", "a", "b", "desktop", "--window"],
    ):
        with pytest.raises(ReadOnlyViolation):
            process.launch_console_window(bad)
    assert len(started) == 2


# ------------------------------------------------------------------ CLI

def test_desktop_command(tmp_path, capsys, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("api:\n  port: 18998\n", encoding="utf-8")
    base = ["--config", str(config), "desktop"]
    assert main([*base, "--window"]) == 1
    assert "Cannot read the station's API token" in capsys.readouterr().out
    main([*base, "--check"])
    out = capsys.readouterr().out
    assert "Station API token readable" in out and "Notifications (libnotify)" in out
    monkeypatch.setattr(sys, "platform", "win32")
    assert main(base) == 2
    assert "Linux desktops only" in capsys.readouterr().out
