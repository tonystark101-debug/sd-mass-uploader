"""Reusable metadata: load from a JSON file, save the current answers as a template."""

from __future__ import annotations

import json
from pathlib import Path

from src.core.exceptions import MetadataValidationError


def load_metadata_file(path: str | Path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise MetadataValidationError(f"Cannot read metadata file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MetadataValidationError(f"Metadata file is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MetadataValidationError("Metadata file must contain a JSON object")
    return data


def save_metadata_file(path: str | Path, values: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(values, indent=2, ensure_ascii=False), encoding="utf-8")
