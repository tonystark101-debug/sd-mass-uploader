"""The API token: random, created once, readable only by the station's user (and its group)."""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from src.core.exceptions import ConfigurationError


def load_or_create_token(path: Path) -> str:
    """Return the token in `path`, creating it with owner/group-only permissions if missing."""
    try:
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ConfigurationError(f"Cannot read API token {path}: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    except FileExistsError:  # Another process created it first; use theirs.
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token)
    return token


def read_token(path: Path) -> str | None:
    """For clients: the token, or None if this user may not read it."""
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def token_matches(expected: str, supplied: str | None) -> bool:
    return bool(supplied) and hmac.compare_digest(expected.encode(), supplied.encode())
