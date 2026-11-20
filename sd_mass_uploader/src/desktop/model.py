"""What the panel indicator shows and which events become notifications. Pure logic, no GUI.

The GTK layer (indicator.py, notify.py) only draws what these functions decide, so the
behaviour is tested here on any machine.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

ICON_OK = "sdu-ok"  # Session open, nothing needs a person.
ICON_BUSY = "sdu-busy"  # Uploading.
ICON_PROBLEM = "sdu-problem"  # A critical or error problem is open.
ICON_OFFLINE = "sdu-offline"  # The station is not answering.
ICON_IDLE = "sdu-idle"  # Running, no session.


@dataclass(frozen=True)
class MenuItem:
    label: str
    action: str | None = None  # None: information only (shown greyed out).


@dataclass
class IndicatorModel:
    icon: str
    label: str  # Short text beside the icon, e.g. "2↑ 5✓ 1✕".
    tooltip: str
    menu: list[MenuItem | None] = field(default_factory=list)  # None draws a separator.


def indicator_model(state: dict | None, connection: str) -> IndicatorModel:
    """`connection` is "live", "offline" or "forbidden" (no permission to read the token)."""
    common = [None, MenuItem("Open console", "open_console")]
    if connection == "forbidden":
        return IndicatorModel(ICON_OFFLINE, "", "SD Uploader: this user may not control the "
                              "station", [MenuItem("Not allowed to read the station token"),
                                          *common, MenuItem("Quit indicator", "quit")])
    if connection != "live" or state is None:
        return IndicatorModel(ICON_OFFLINE, "!", "SD Uploader: station not answering",
                              [MenuItem("Station is not answering"), *common,
                               MenuItem("Quit indicator", "quit")])
    problems = state.get("problems") or []
    urgent = [p for p in problems if p.get("severity") in ("critical", "error")]
    session = state.get("session")
    if session is None:
        return IndicatorModel(
            ICON_PROBLEM if urgent else ICON_IDLE, "", "SD Uploader: no session open",
            [MenuItem("No session open"), *common, None, MenuItem("Quit indicator", "quit")])
    c = state.get("counters") or {}
    needs_you = c.get("needs_confirmation", 0) + c.get("needs_decision", 0)
    parts = []
    if c.get("uploading"):
        parts.append(f"{c['uploading']}↑")
    parts.append(f"{c.get('uploaded', 0)}✓")
    if c.get("failed"):
        parts.append(f"{c['failed']}✕")
    if needs_you:
        parts.append(f"{needs_you}?")
    icon = ICON_PROBLEM if urgent else ICON_BUSY if c.get("uploading") else ICON_OK
    progress = state.get("progress") or {}
    total, done = progress.get("bytes_total") or 0, progress.get("bytes_done") or 0
    share = 100 if total and done >= total else (min(99, int(100 * done / total)) if total else 0)
    menu: list[MenuItem | None] = [
        MenuItem(f"Inserted {c.get('inserted', 0)} · Uploading {c.get('uploading', 0)} · "
                 f"Uploaded {c.get('uploaded', 0)}"),
        MenuItem(f"Failed {c.get('failed', 0)} · Paused {c.get('paused', 0)} · "
                 f"Removed {c.get('removed', 0)} · Needs you {needs_you}"),
        MenuItem(f"{share}% of {_size(total)}" if total else "Nothing queued yet"),
    ]
    if problems:
        top = (urgent or problems)[0]
        more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
        menu += [None, MenuItem(f"⚠ {top.get('title', 'Problem')}{more}", "open_console")]
    menu += common
    menu.append(MenuItem("Resume all", "resume_all") if session.get("paused_all")
                else MenuItem("Pause all", "pause_all"))
    menu += [None, MenuItem("Quit indicator", "quit")]
    tooltip = (f"SD Uploader: {c.get('uploaded', 0)} of {c.get('total', 0)} cards uploaded"
               + (f", {len(urgent)} problem(s)" if urgent else ""))
    return IndicatorModel(icon, " ".join(parts), tooltip, menu)


def _size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{value} B"


# ---------------------------------------------------------------------- notifications

@dataclass(frozen=True)
class Notification:
    key: str  # Same key within the quiet period: shown once.
    title: str
    body: str
    urgency: str = "normal"  # "low", "normal" or "critical"


class NotificationPolicy:
    """Calm by default: only what needs a person, cards that are safe to remove, and the end
    of a session. Repeats of the same thing within `quiet_seconds` are dropped, and several
    cards finishing together become one notification."""

    def __init__(self, quiet_seconds: float = 120.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._quiet = quiet_seconds
        self._clock = clock
        self._shown: dict[str, float] = {}

    def decide(self, events: list[dict], state: dict | None = None) -> list[Notification]:
        cards = {card["card_id"]: card for card in (state or {}).get("cards") or []}
        found: list[Notification] = []
        finished: list[str] = []
        for event in events:
            kind, data = event.get("kind"), event.get("data") or {}
            if kind == "problem_opened" and data.get("severity") in ("critical", "error"):
                critical = data.get("severity") == "critical"
                found.append(Notification(
                    f"problem:{data.get('id')}",
                    ("Uploads stopped: " if critical else "Needs you: ") + str(data.get("title")),
                    "Open the console to see what to do.", "critical" if critical else "normal"))
            elif kind == "card_state" and data.get("state") == "UPLOADED":
                card = cards.get(str(data.get("card_id")), {})
                finished.append(str(card.get("label") or data.get("card_id")))
            elif kind == "session_closed":
                found.append(Notification(f"closed:{data.get('session_id')}", "Session ended",
                                          f"Result: {data.get('status')}.", "low"))
        if len(finished) == 1:
            found.append(Notification(f"uploaded:{finished[0]}", f"{finished[0]} uploaded",
                                      "Every file is verified. It is safe to remove.", "low"))
        elif finished:
            found.append(Notification(f"uploaded:{','.join(sorted(finished))}",
                                      f"{len(finished)} cards uploaded",
                                      ", ".join(finished[:6]) + ": safe to remove.", "low"))
        return [item for item in found if self._fresh(item.key)]

    def _fresh(self, key: str) -> bool:
        now = self._clock()
        last = self._shown.get(key)
        if last is not None and now - last < self._quiet:
            return False
        self._shown[key] = now
        return True
