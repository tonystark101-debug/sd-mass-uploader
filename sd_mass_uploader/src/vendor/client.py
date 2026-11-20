"""Talks to the vendor platform's station access (src/station.js on vendor.byndfrntr.com).

The station has its own key, which can only look up a packet, say an upload started, and say it
finished. It is read from `vendor_platform.key_file` for every call (so a new key works at once)
and is never logged. The platform never sends phone numbers back.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from src.core.exceptions import ConfigurationError, TransientUploadError, UploaderError
from src.core.settings import VendorPlatformSettings
from src.peakai.keeper import Http, urllib_http

# The permanent code: 5 letters, 4 digits, a check letter. A packet adds when it was sent.
CODE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class VendorPlatformError(UploaderError):
    """The vendor platform said no (unknown code, typo, bad key). The message is its answer."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class VendorPlatformUnreachable(TransientUploadError):
    """No answer from the vendor platform (no internet, or the site is down)."""


def normalize(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())


def check_letter(nine: str) -> str:
    """The same weighted sum as the platform (src/shipments.js checkLetter)."""
    total = 0
    for index, char in enumerate(nine[:9]):
        value = int(char) if char.isdigit() else _LETTERS.index(char) + 10
        total += (index + 1) * value
    return _LETTERS[total % 26]


def check_code(raw: str) -> str:
    """Catch a mistyped code before asking the platform. Returns it without spaces or dashes."""
    text = normalize(raw)
    code = text[:10]
    if not CODE.match(code):
        raise VendorPlatformError("A packet code is 5 letters, 4 digits and a letter, for "
                                  "example FVGPA3451K. Check what is written on the packet.")
    if check_letter(code) != code[9]:
        raise VendorPlatformError("This code has a typo: check each letter and digit on the "
                                  "packet.")
    rest = text[10:]
    if rest and not re.fullmatch(r"\d{10}\d{0,2}", rest):
        raise VendorPlatformError("After the code comes the date and time it was sent, for "
                                  "example FVGPA3451K-061126-1430.")
    return text


def read_station_key(path: Path) -> str:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError(f"This station's vendor platform key is not readable at "
                                 f"{path}. Ask the office for it.") from exc
    if len(key) < 24:
        raise ConfigurationError(f"The vendor platform key in {path} is too short or empty")
    return key


class VendorPlatform:
    def __init__(self, settings: VendorPlatformSettings, key_loader: Callable[[], str],
                 http: Http = urllib_http) -> None:
        self._settings = settings
        self._key_loader = key_loader
        self._http = http

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        key = self._key_loader()
        url = self._settings.url.rstrip("/") + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        code, raw = self._http(method, url, headers, data, self._settings.timeout_seconds)
        if code == 0:
            raise VendorPlatformUnreachable("The vendor platform did not answer. Check the "
                                            "internet connection, then try again.")
        try:
            answer = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            answer = {}
        if code == 200 and isinstance(answer, dict):
            return answer
        message = answer.get("error") if isinstance(answer, dict) else None
        if code >= 500 and code != 503:
            raise VendorPlatformUnreachable(f"The vendor platform had a problem (HTTP {code}). "
                                            "Try again in a minute.")
        if code == 401:
            message = f"{message or 'The key was refused'}. Ask the office for this station's key."
        raise VendorPlatformError(message or f"The vendor platform answered HTTP {code}", code)

    def lookup(self, raw: str) -> dict:
        """{"packet": {...}} or, when a code has several packets to upload, {"packets": [...]}."""
        ref = check_code(raw)
        return self._call("GET", f"/api/station/packets/{urllib.parse.quote(ref)}")

    def start(self, ref: str, cards_detected: int, cards_counted: int, note: str = "") -> dict:
        return self._call("POST", f"/api/station/packets/{urllib.parse.quote(normalize(ref))}"
                          "/start", {"cards_detected": cards_detected,
                                     "cards_counted": cards_counted, "note": note})

    def uploaded(self, ref: str, hours: float, cards_uploaded: int, files: int, bytes_: int,
                 note: str = "") -> dict:
        return self._call("POST", f"/api/station/packets/{urllib.parse.quote(normalize(ref))}"
                          "/uploaded", {"hours": round(hours, 2), "cards_uploaded": cards_uploaded,
                                        "files": files, "bytes": bytes_, "note": note})
