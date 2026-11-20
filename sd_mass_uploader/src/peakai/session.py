"""PeakAI session details, folder names and the `_info` file (SITE_CHAT_INSTRUCTIONS.md 1, 3, 5)."""

from __future__ import annotations

import hashlib
import re
import socket
from datetime import date, datetime, timedelta, timezone

from src.core.exceptions import MetadataValidationError
from src.metadata.validation import has_control_characters, object_name_for
from src.peakai.devices import STEREO_PASSES
from src.peakai.pack import PackSettings

IST = timezone(timedelta(hours=5, minutes=30), "IST")  # India has no daylight saving.
_SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_PHONE = re.compile(r"(?:\+?\d[\s\-().]*){10,}")  # Ten or more digits, however written.
_FIELDS = ("activity", "city", "vendor_name", "site", "batch", "recorded_from", "recorded_to",
           "video_hours", "city_unknown_confirmed", "packet", "packet_device")
# A vendor platform packet: its code and when it was sent (src/vendor), e.g. FVGPA3451K-061126-1430.
_PACKET = re.compile(r"^[A-Z]{5}\d{4}[A-Z](-\d{6}-\d{4}(-\d{1,2})?)?$")


def now_ist() -> datetime:
    return datetime.now(IST)


def upload_day(now: datetime | None = None) -> str:
    """The day you upload, India time: the date folder in the path."""
    return (now or now_ist()).astimezone(IST).date().isoformat()


def contains_phone_number(text: str) -> bool:
    return bool(_PHONE.search(text or ""))


def _text(raw: dict, name: str, required: bool, limit: int = 120) -> str:
    value = raw.get(name)
    value = "" if value is None else str(value).strip()
    if required and not value:
        raise MetadataValidationError(f"{name} is required. Ask the person; never guess")
    if len(value) > limit or has_control_characters(value):
        raise MetadataValidationError(f"{name} is too long or has control characters")
    if contains_phone_number(value):
        raise MetadataValidationError(f"{name} looks like it holds a phone number. Phone "
                                      "numbers are not allowed anywhere")
    return value


def _day(raw: dict, name: str) -> str:
    value = _text(raw, name, False, 10)
    if not value:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise MetadataValidationError(f"{name} must be a date like 2026-11-08") from exc


def validate_session(raw: dict, pack: PackSettings,
                     now: datetime | None = None) -> tuple[dict[str, str], list[str]]:
    """Check the operator's answers. Returns the stored details and notes to tell them."""
    unknown = set(raw) - set(_FIELDS)
    if unknown:
        raise MetadataValidationError(f"Unknown field(s): {sorted(unknown)}. PeakAI sessions "
                                      f"use: {', '.join(_FIELDS[:8])}")
    notes = []
    activity = _text(raw, "activity", True).lower()
    if not _SLUG.fullmatch(activity):
        raise MetadataValidationError("activity must be lowercase words joined by '-', for "
                                      "example motor-repair")
    if activity not in pack.activities:
        notes.append(f"'{activity}' is a new activity (not in PeakAI's list); it was added")
    city = _text(raw, "city", True).lower()
    if not _SLUG.fullmatch(city):
        raise MetadataValidationError("city must be the lowercase town name, for example "
                                      "bijapur")
    if city == "unknown" and raw.get("city_unknown_confirmed") is not True:
        raise MetadataValidationError("Ask the person where it was recorded. Use 'unknown' "
                                      "only if nobody knows, and confirm that")
    if city == "unknown":
        notes.append("The city is recorded as 'unknown'. Tell the person")
    details = {
        "profile": "peakai",
        "activity": activity,
        "city": city,
        "vendor_name": _text(raw, "vendor_name", True),
        "site": _text(raw, "site", False),
        "batch": _text(raw, "batch", False, 60),
        "upload_date": upload_day(now),
        "recorded_from": _day(raw, "recorded_from"),
        "recorded_to": _day(raw, "recorded_to"),
        "video_hours": "",
        # Not run through the phone-number check: a packet reference is digits by design.
        "packet": str(raw.get("packet") or "").strip().upper(),
        "packet_device": _text(raw, "packet_device", False, 60),
    }
    if details["packet"] and not _PACKET.fullmatch(details["packet"]):
        raise MetadataValidationError("packet must be a vendor platform packet reference like "
                                      "FVGPA3451K-061126-1430")
    if details["recorded_from"] and details["recorded_to"] \
            and details["recorded_from"] > details["recorded_to"]:
        raise MetadataValidationError("recorded_from is after recorded_to")
    hours = raw.get("video_hours")
    if hours not in (None, ""):
        try:
            value = float(hours)
        except (TypeError, ValueError) as exc:
            raise MetadataValidationError("video_hours must be a number like 26.4") from exc
        if not 0 <= value < 100_000:
            raise MetadataValidationError("video_hours is out of range")
        details["video_hours"] = f"{value:g}"
    return {key: value for key, value in details.items() if value != ""}, notes


def card_serial(fingerprint: str | None) -> str | None:
    """The card's volume serial as 8 (or 16) uppercase hex digits, e.g. 6AF7F78B (D12)."""
    if not fingerprint:
        return None
    if fingerprint.startswith("sim:"):  # Simulated cards: a stable made-up serial.
        return hashlib.sha1(fingerprint.encode()).hexdigest()[:8].upper()
    if fingerprint.startswith("folder:"):
        return None
    digits = re.sub(r"[-\s]", "", fingerprint)
    if re.fullmatch(r"[0-9A-Fa-f]{8}|[0-9A-Fa-f]{16}", digits):
        return digits.upper()
    return None


def card_folder(pack: PackSettings, pass_name: str, details: dict, serial: str) -> str:
    """<pass folder>/<activity>/<city>/<upload day>/card-<SERIAL>/"""
    return (f"{pack.folder_of(pass_name)}{details['activity']}/{details['city']}/"
            f"{details['upload_date']}/card-{serial}/")


def object_name(folder: str, relative_path: str) -> str:
    """The card exactly as it is, under its card folder. Unsafe names are refused."""
    return object_name_for(folder.rstrip("/").rsplit("/", 1)[0],
                           folder.rstrip("/").rsplit("/", 1)[1], relative_path)


def machine_name(configured: str = "") -> str:
    name = configured or socket.gethostname() or "station"
    return re.sub(r"[^A-Za-z0-9._-]", "-", name)[:60]


def info_object_name(folder: str, machine: str, when: datetime | None = None) -> str:
    stamp = (when or now_ist()).astimezone(IST).strftime("%Y%m%dT%H%M%S")
    return f"{folder}_info/{machine}_{stamp}.json"


def info_document(*, pass_name: str, folder: str, vendor_code: str, details: dict,
                  device: str, serial: str, label: str | None, file_count: int,
                  total_bytes: int, hours: float | None, recorded_from: str | None,
                  recorded_to: str | None, machine: str, when: datetime | None = None) -> dict:
    """The `_info` file of SITE_CHAT_INSTRUCTIONS.md section 5. No phone numbers."""
    written = (when or now_ist()).astimezone(IST).replace(microsecond=0)
    document = {
        "pass": pass_name,
        "folder": folder,
        "vendor": vendor_code,
        "vendor_name": details.get("vendor_name", ""),
        "device": device,
        "camera": "stereo" if pass_name in STEREO_PASSES else "mono",
        "activity": details["activity"],
        "city": details["city"],
        "site": details.get("site", ""),
        "batch": details.get("batch", ""),
        "packet": details.get("packet", ""),  # The vendor platform packet the card came in.
        "upload_date": details["upload_date"],
        "recorded_from": recorded_from or details.get("recorded_from") or None,
        "recorded_to": recorded_to or details.get("recorded_to") or None,
        "card_serial": serial,
        "card_label": label or "",
        "file_count": file_count,
        "total_bytes": total_bytes,
        "video_hours_estimate": hours,
        "written_by": machine,
        "written_at": written.isoformat(),
    }
    for key, value in document.items():
        if isinstance(value, str) and contains_phone_number(value) and key not in (
                "folder", "upload_date", "recorded_from", "recorded_to", "written_at",
                "packet"):
            raise MetadataValidationError(f"_info {key} looks like it holds a phone number")
    return document
