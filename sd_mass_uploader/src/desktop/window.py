"""The console in its own window (pywebview on WebKitGTK), or the browser as a fallback."""

from __future__ import annotations

import logging
import sys
import webbrowser
from typing import Any

logger = logging.getLogger(__name__)

TITLE = "SD Uploader"


def webview_available() -> bool:
    try:
        import webview  # noqa: F401
    except ImportError:
        return False
    return True


def open_console(link: str, fullscreen: bool = False, webview_module: Any = None) -> str:
    """Show the console. Returns "window" or "browser". Blocks while a window is open."""
    module = webview_module
    if module is None:
        try:
            import webview as module
        except ImportError:
            module = None
    if module is not None:
        module.create_window(TITLE, link, width=1440, height=900, min_size=(900, 600),
                             fullscreen=fullscreen, confirm_close=False,
                             text_select=True)
        # WebKitGTK on Linux; elsewhere pywebview picks the platform's own engine.
        module.start(gui="gtk" if sys.platform.startswith("linux") else None)
        return "window"
    logger.info("pywebview is not installed; opening the console in the browser")
    if not webbrowser.open(link):
        raise RuntimeError("No browser could be opened")
    return "browser"
