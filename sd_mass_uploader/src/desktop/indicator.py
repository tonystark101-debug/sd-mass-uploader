"""The panel indicator (Linux): an icon with counts in the top or bottom panel, a menu, and
notifications. AyatanaAppIndicator works on Xfce (Kali) and on Ubuntu GNOME, which ships the
AppIndicator extension. Without it, only notifications run.

All decisions come from model.py; this module only draws them.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from src.desktop.feed import FeedUpdate, StationFeed
from src.desktop.model import IndicatorModel, NotificationPolicy, indicator_model
from src.desktop.notify import Notifier

logger = logging.getLogger(__name__)

ICON_DIR = Path(__file__).with_name("icons")


def load_appindicator():
    """The AppIndicator module, or None. Ayatana first (current Ubuntu, Debian, Kali)."""
    import gi

    for name in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(name, "0.1")
            module = __import__("gi.repository", fromlist=[name])
            return getattr(module, name)
        except (ValueError, ImportError, AttributeError):
            continue
    return None


class Indicator:
    def __init__(self, feed: StationFeed, notifier: Notifier, open_console: Callable[[], None],
                 command: Callable[[str], None]) -> None:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib, Gtk

        self._gtk, self._glib = Gtk, GLib
        self._feed = feed
        self._notifier = notifier
        self._open_console = open_console
        self._command = command
        self._policy = NotificationPolicy()
        self._stop = threading.Event()
        self._last: IndicatorModel | None = None
        app_indicator = load_appindicator()
        self._indicator = None
        if app_indicator is not None:
            self._indicator = app_indicator.Indicator.new(
                "sd-uploader", "sdu-offline",
                app_indicator.IndicatorCategory.APPLICATION_STATUS)
            self._indicator.set_icon_theme_path(str(ICON_DIR))
            self._indicator.set_status(app_indicator.IndicatorStatus.ACTIVE)
            self._indicator.set_title("SD Uploader")
        else:
            logger.warning("No AppIndicator support: notifications only. On GNOME, enable the "
                           "AppIndicator extension to see the panel icon")

    def run(self) -> int:
        thread = threading.Thread(target=self._feed.run, args=(self._stop, self._deliver),
                                  name="desktop-feed", daemon=True)
        thread.start()
        try:
            self._gtk.main()
        finally:
            self._stop.set()
        return 0

    def _deliver(self, update: FeedUpdate) -> None:  # Feed thread: hand over to GTK.
        self._glib.idle_add(self._apply, update)

    def _apply(self, update: FeedUpdate) -> bool:
        model = indicator_model(update.state, update.connection)
        if self._indicator is not None and model != self._last:
            self._indicator.set_icon_full(model.icon, model.tooltip)
            self._indicator.set_label(model.label, "88↑ 88✓ 88✕ 8?")
            self._indicator.set_menu(self._menu(model))
            self._last = model
        for notification in self._policy.decide(update.events, update.state):
            self._notifier.show(notification)
        return False  # Run once.

    def _menu(self, model: IndicatorModel):
        Gtk = self._gtk
        menu = Gtk.Menu()
        for item in model.menu:
            if item is None:
                menu.append(Gtk.SeparatorMenuItem())
                continue
            entry = Gtk.MenuItem(label=item.label)
            if item.action is None:
                entry.set_sensitive(False)
            else:
                entry.connect("activate", lambda _widget, action=item.action: self._act(action))
            menu.append(entry)
        menu.show_all()
        return menu

    def _act(self, action: str) -> None:
        if action == "quit":
            self._gtk.main_quit()
        elif action == "open_console":
            self._open_console()
        else:  # pause_all / resume_all: off the GTK thread.
            threading.Thread(target=self._command, args=(action,), daemon=True).start()


def run_indicator(feed: StationFeed, notifier: Notifier, open_console: Callable[[], None],
                  command: Callable[[str], None]) -> int:
    if not sys.platform.startswith("linux"):
        print("The panel indicator runs on Linux desktops only.")
        return 2
    try:
        indicator = Indicator(feed, notifier, open_console, command)
    except (ImportError, ValueError) as exc:
        print(f"GTK is not available ({exc}). Install python3-gi and gir1.2-gtk-3.0.")
        return 2
    return indicator.run()
