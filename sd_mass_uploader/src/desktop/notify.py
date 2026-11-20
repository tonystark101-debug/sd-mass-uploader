"""Desktop notifications: libnotify through PyGObject on Linux, otherwise the log only."""

from __future__ import annotations

import logging
from typing import Protocol

from src.desktop.model import Notification

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def show(self, notification: Notification) -> None: ...


class LogNotifier:
    def __init__(self) -> None:
        self.shown: list[Notification] = []

    def show(self, notification: Notification) -> None:
        self.shown.append(notification)
        logger.info("Notification: %s: %s", notification.title, notification.body)


class GiNotifier:
    """freedesktop notifications (GNOME, Xfce, KDE) via libnotify."""

    def __init__(self) -> None:
        import gi

        gi.require_version("Notify", "0.7")
        from gi.repository import Notify

        Notify.init("SD Uploader")
        self._notify = Notify
        self._urgency = {"low": Notify.Urgency.LOW, "normal": Notify.Urgency.NORMAL,
                         "critical": Notify.Urgency.CRITICAL}

    def show(self, notification: Notification) -> None:
        item = self._notify.Notification.new(notification.title, notification.body,
                                             "sd-uploader")
        item.set_urgency(self._urgency.get(notification.urgency, self._urgency["normal"]))
        try:
            item.show()
        except Exception as exc:  # noqa: BLE001 - a missing notification daemon is not fatal
            logger.warning("Could not show a notification: %s", exc)


def create_notifier() -> Notifier:
    try:
        return GiNotifier()
    except (ImportError, ValueError, AttributeError) as exc:
        logger.info("Desktop notifications unavailable (%s); logging them instead", exc)
        return LogNotifier()
