"""Rotating file logs, with a filter that removes anything shaped like a secret."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.core.settings import Settings

_SECRETS = [
    re.compile(r"ya29\.[\w.-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r'("?(?:private_key|access_token|refresh_token|client_secret)"?\s*[:=]\s*)'
               r'("[^"]*"|\S+)', re.I),
    re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.I),
    re.compile(r"(X-PeakAI-Key:\s*)\S+", re.I),
    re.compile(r"sk-ant-[\w-]{10,}"),
]


def redact(text: str) -> str:
    for pattern in _SECRETS:
        if pattern.groups:
            text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def configure_logging(settings: Settings) -> Path | None:
    """Log to the station's file. A user who may not write there (an operator running
    `sdu console`) logs warnings to the terminal instead."""
    directory = settings.resolve(settings.logging.directory)
    path: Path | None = directory / "uploader.log"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            path, maxBytes=settings.logging.max_bytes,
            backupCount=settings.logging.backup_count, encoding="utf-8")
    except OSError:
        handler, path = logging.StreamHandler(), None
        handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.application.log_level.upper())
    for noisy in ("urllib3", "google", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path
