"""Validate operator metadata and build safe object names."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

from src.core.exceptions import MetadataValidationError
from src.core.settings import MetadataSettings

MAX_FIELD_LENGTH = 256
MAX_OBJECT_NAME_BYTES = 1024
RESERVED_PREFIX = ".well-known/acme-challenge/"


@dataclass
class SessionMetadata:
    activity_name: str
    recording_date: str
    project_name: str
    place_of_recording: str = ""
    operator_name: str = ""
    description: str = ""
    custom: dict[str, str] | None = None

    def as_flat_dict(self) -> dict[str, str]:
        values = {
            "activity_name": self.activity_name,
            "place_of_recording": self.place_of_recording,
            "recording_date": self.recording_date,
            "operator_name": self.operator_name,
            "project_name": self.project_name,
            "description": self.description,
        }
        values.update({f"custom.{key}": value for key, value in (self.custom or {}).items()})
        return {key: value for key, value in values.items() if value}


def has_control_characters(text: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in text)


def _clean(field: str, value: object, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise MetadataValidationError(f"{field} must be text")
    text = value.strip()
    if required and not text:
        raise MetadataValidationError(f"{field} is required")
    if has_control_characters(text):
        raise MetadataValidationError(f"{field} contains control characters")
    if len(text) > MAX_FIELD_LENGTH:
        raise MetadataValidationError(f"{field} is longer than {MAX_FIELD_LENGTH} characters")
    return text


def _clean_date(value: object) -> str:
    text = _clean("recording_date", value, True)
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise MetadataValidationError("recording_date must be a real date, YYYY-MM-DD") from exc


def validate_metadata(raw: dict, settings: MetadataSettings) -> SessionMetadata:
    known = {"activity_name", "place_of_recording", "recording_date", "operator_name",
             "project_name", "description", "custom"}
    unknown = set(raw) - known
    if unknown:
        raise MetadataValidationError(f"Unknown metadata field(s): {sorted(unknown)}")
    custom_raw = raw.get("custom") or {}
    if not isinstance(custom_raw, dict):
        raise MetadataValidationError("custom must be a mapping of names to text")
    custom = {}
    for key, value in custom_raw.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(key)):
            raise MetadataValidationError(f"Invalid custom field name: {key!r}")
        custom[str(key)] = _clean(f"custom.{key}", value, False)
    metadata = SessionMetadata(
        activity_name=_clean("activity_name", raw.get("activity_name"), True),
        recording_date=_clean_date(raw.get("recording_date")),
        project_name=_clean("project_name", raw.get("project_name"), True),
        place_of_recording=_clean("place_of_recording", raw.get("place_of_recording"),
                                  settings.require_place_of_recording),
        operator_name=_clean("operator_name", raw.get("operator_name"),
                             settings.require_operator_name),
        description=_clean("description", raw.get("description"), False),
        custom=custom or None,
    )
    safe_path_segment(metadata.project_name)
    return metadata


def safe_path_segment(value: str) -> str:
    """Reduce operator text to one path segment. '../x' can only ever become 'x'."""
    # Letters, digits and combining marks stay: Indic vowel signs are marks, not punctuation.
    kept = [ch if ch in "._-" or unicodedata.category(ch)[0] in "LMN" else "_"
            for ch in unicodedata.normalize("NFC", value.strip())]
    segment = re.sub(r"_+", "_", "".join(kept)).strip("._")
    if not segment:
        raise MetadataValidationError(f"{value!r} has no usable characters for a path")
    return segment


def session_prefix(root_prefix: str, project_name: str, recording_date: str,
                   session_id: str) -> str:
    """Build PROJECT/YYYY/MM/DD/SESSION_ID below the configured root."""
    day = date.fromisoformat(recording_date)
    parts = [safe_path_segment(part) for part in root_prefix.split("/") if part]
    parts += [safe_path_segment(project_name), f"{day.year:04d}", f"{day.month:02d}",
              f"{day.day:02d}", safe_path_segment(session_id)]
    return "/".join(parts)


def object_name_for(prefix: str, card_id: str, relative_path: str) -> str:
    """Join a source path under the card folder, or raise if GCS could not store it safely.

    Source names are kept exactly, so the hierarchy is preserved. They are refused rather than
    rewritten, because changing a name quietly could make two files collide.
    """
    relative = relative_path.replace("\\", "/")
    segments = relative.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise MetadataValidationError(f"Unsafe source path: {relative_path!r}")
    if has_control_characters(relative):
        raise MetadataValidationError(f"Control characters in source path: {relative_path!r}")
    name = f"{prefix}/{safe_path_segment(card_id)}/{relative}"
    if name.startswith(RESERVED_PREFIX):
        raise MetadataValidationError(f"Reserved object prefix: {name!r}")
    if len(name.encode("utf-8")) > MAX_OBJECT_NAME_BYTES:
        raise MetadataValidationError(f"Object name exceeds 1024 bytes: {relative_path!r}")
    return name
