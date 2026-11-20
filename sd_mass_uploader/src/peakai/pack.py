"""Import PeakAI's context pack (the zip) without running anything in it.

The pack is PeakAI's manual for this laptop. It holds the rules (README.md,
SITE_CHAT_INSTRUCTIONS.md), their bash tools (kept only as reference) and the laptop key.
Importing reads it, checks it, shows a report, and only on confirmation:
- writes the laptop key to `peakai.key_file` (owner-only),
- saves the documents and the parsed settings under `peakai.pack_dir`,
- keeps the previous pack so `pack rollback` can restore it.
The key is never printed, logged or returned.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from src.core.exceptions import ConfigurationError
from src.core.timeutil import utc_now

PROFILE = "peakai-v2"
_MAX_MEMBERS = 200
_MAX_TOTAL_BYTES = 5 * 1024 * 1024
_KEY_SHAPE = re.compile(r"^[A-Za-z0-9_\-.]{20,200}$")
DOCUMENTS = ("README.md", "SITE_CHAT_INSTRUCTIONS.md")
REFERENCE_SCRIPTS = ("install.sh", "peakai-tokens.sh", "peakai-report.sh")

# Phrases the station's device rules (src/peakai/devices.py) were written against. If PeakAI
# changes them, the import stops rather than upload with outdated rules.
_PATH_RULE = "<pass folder>/<activity>/<city>/<upload day>/card-<SERIAL>/"
_DEVICE_MARKERS = ("ego_*.mcap", "GILABS-", "GKENC1", "-left.mp4.enc", "video.h265",
                   "video_live_", "DVR/VIDEO/", "Android/")


@dataclass
class PassRule:
    name: str
    folder: str  # e.g. "buildai/stereo/"
    description: str


@dataclass
class PackSettings:
    """Everything the station uses from the pack. No secrets."""

    profile: str
    version: str
    dated: str
    bucket: str
    token_url: str
    passes: dict[str, PassRule]
    activities: list[str]
    source_name: str
    source_sha256: str
    imported_at: str = ""
    documents: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        data = asdict(self)
        return json.dumps(data, indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> PackSettings:
        data = json.loads(text)
        data["passes"] = {name: PassRule(**rule) for name, rule in data["passes"].items()}
        return cls(**data)

    def folder_of(self, pass_name: str) -> str:
        return self.passes[pass_name].folder


@dataclass
class ImportReport:
    settings: PackSettings | None
    key_ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    live_passes: list[str] | None = None  # From the token service, when it was reachable.
    files: dict[str, str] = field(default_factory=dict)  # Kept documents: name -> text.
    key: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return not self.errors and self.settings is not None and self.key_ok

    def lines(self) -> list[str]:
        """The report shown to the operator. Never includes the key."""
        out = []
        s = self.settings
        if s:
            out += [f"Pack: {s.source_name}  ({s.profile}, v{s.version}, {s.dated})",
                    f"Bucket: gs://{s.bucket}",
                    f"Token service: {s.token_url}",
                    "Passes in the rules:"]
            out += [f"  {rule.name:<11} -> {rule.folder:<16} {rule.description}"
                    for rule in s.passes.values()]
            out.append(f"Activities: {', '.join(s.activities)}")
            out.append(f"Folder layout: gs://{s.bucket}/{_PATH_RULE}<card as is>")
        out.append("Laptop key: found, will be stored privately" if self.key_ok
                   else "Laptop key: missing or not valid")
        if self.live_passes is not None:
            out.append("This laptop may use now: " + (", ".join(self.live_passes) or "(none)"))
        out += [f"WARNING: {text}" for text in self.warnings]
        out += [f"ERROR: {text}" for text in self.errors]
        return out


def _member(names: dict[str, str], zf: zipfile.ZipFile, base: str) -> str | None:
    path = names.get(base)
    if path is None:
        return None
    return zf.read(path).decode("utf-8", errors="replace")


def read_pack(zip_path: str | Path) -> ImportReport:
    """Read and check a pack. Changes nothing on disk."""
    path = Path(zip_path)
    try:
        raw = path.read_bytes()
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        return ImportReport(None, False, [f"Cannot open {path}: {exc}"])
    with zf:
        infos = zf.infolist()
        errors: list[str] = []
        if len(infos) > _MAX_MEMBERS or sum(i.file_size for i in infos) > _MAX_TOTAL_BYTES:
            return ImportReport(None, False, ["The zip is too large to be a context pack"])
        names: dict[str, str] = {}
        for info in infos:
            parts = PurePosixPath(info.filename.replace("\\", "/")).parts
            if info.filename.startswith(("/", "\\")) or ".." in parts or ":" in info.filename:
                return ImportReport(None, False, [f"Unsafe path in zip: {info.filename!r}"])
            if not info.is_dir():
                names.setdefault(parts[-1], info.filename)
        readme = _member(names, zf, "README.md")
        site = _member(names, zf, "SITE_CHAT_INSTRUCTIONS.md")
        tokens_sh = _member(names, zf, "peakai-tokens.sh")
        key_text = _member(names, zf, "laptop_key.txt")
        files = {name: text for name in (*DOCUMENTS, *REFERENCE_SCRIPTS)
                 if (text := _member(names, zf, name)) is not None}

    for name, text in (("README.md", readme), ("SITE_CHAT_INSTRUCTIONS.md", site),
                       ("peakai-tokens.sh", tokens_sh), ("laptop_key.txt", key_text)):
        if text is None:
            errors.append(f"{name} is missing from the zip")
    if errors:
        return ImportReport(None, False, errors)

    key = key_text.strip()
    key_ok = bool(_KEY_SHAPE.fullmatch(key)) and len(key_text.strip().splitlines()) == 1
    report = ImportReport(None, key_ok, files=files, key=key if key_ok else "")
    if not key_ok:
        report.errors.append("laptop_key.txt does not look like a laptop key")

    version = re.search(r"\(v(\d+),\s*([^)]+)\)", readme)
    url = re.search(r"PEAKAI_TOKEN_URL:-(https://[A-Za-z0-9.\-/]+)\}", tokens_sh)
    bucket = re.search(r"Folder in `gs://([a-z0-9._-]+)/`", readme)
    passes = {}
    row = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|\s*`([a-z0-9_\-/]+/)`\s*\|\s*(.+?)\s*\|\s*$",
                     re.M)
    for match in row.finditer(readme):
        name, folder, description = match.groups()
        passes[name] = PassRule(name, folder, description)
    activity_line = re.search(r"\*\*Activity:\*\*(.+?)(?:\n\n|\n-)", site, re.S)
    activities = re.findall(r"`([a-z0-9-]+)`", activity_line.group(1)) if activity_line else []

    if not version:
        report.errors.append("README.md has no version line like '(v2, 12 Nov 2026)'")
    if not url:
        report.errors.append("No token service address in peakai-tokens.sh")
    if not bucket:
        report.errors.append("README.md does not name the bucket")
    if not passes:
        report.errors.append("README.md has no pass table")
    if not activities:
        report.errors.append("SITE_CHAT_INSTRUCTIONS.md has no activity list")
    if _PATH_RULE not in site:
        report.errors.append("The folder layout in SITE_CHAT_INSTRUCTIONS.md is not the one "
                             "this station supports; it needs updating for this pack")
    missing = [marker for marker in _DEVICE_MARKERS if marker not in site]
    if missing:
        report.errors.append("The device rules changed (" + ", ".join(missing) + " not found); "
                             "the station's device rules must be updated first")
    for name, rule in passes.items():
        if not re.fullmatch(r"[a-z0-9-]+(/[a-z0-9-]+)*/", rule.folder):
            report.errors.append(f"Pass {name} has an unusual folder {rule.folder!r}")
    if version and version.group(1) != "2":
        report.errors.append(f"Pack version v{version.group(1)} is not supported (only v2)")
    if report.errors:
        return report
    report.settings = PackSettings(
        profile=PROFILE, version=version.group(1), dated=version.group(2).strip(),
        bucket=bucket.group(1), token_url=url.group(1).rstrip("/"), passes=passes,
        activities=activities, source_name=path.name,
        source_sha256=hashlib.sha256(raw).hexdigest(), documents=sorted(files),
    )
    return report


# ---------------------------------------------------------------------- apply and rollback

def write_private_bytes(path: Path, data: bytes) -> None:
    """Create or replace a file readable by this user only (atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


def discard_upload(path: Path) -> None:
    """Delete a pack zip uploaded through the console (it holds the laptop key)."""
    if path.name == "upload.zip":
        path.unlink(missing_ok=True)


def _write_private(path: Path, text: str) -> None:
    write_private_bytes(path, text.encode("utf-8"))


def apply_pack(report: ImportReport, pack_dir: Path, key_file: Path) -> PackSettings:
    """Install a checked pack. The previous one is kept for rollback."""
    if not report.ok:
        raise ConfigurationError("The pack has errors; nothing was changed")
    settings = report.settings
    settings.imported_at = utc_now()
    pack_dir.mkdir(parents=True, exist_ok=True)
    active, previous = pack_dir / "active", pack_dir / "previous"
    if active.exists():
        if previous.exists():
            shutil.rmtree(previous)
        os.replace(active, previous)
        if key_file.exists():
            os.replace(key_file, key_file.with_name(key_file.name + ".previous"))
    staging = pack_dir / "staging"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "documents").mkdir(parents=True)
    for name, text in report.files.items():
        # Scripts are kept as .txt: reference for people and the AI, never executed.
        target = name if name.endswith(".md") else name + ".txt"
        (staging / "documents" / target).write_text(text, encoding="utf-8")
    (staging / "pack.json").write_text(settings.to_json(), encoding="utf-8")
    os.replace(staging, active)
    _write_private(key_file, report.key + "\n")
    return settings


def rollback_pack(pack_dir: Path, key_file: Path) -> PackSettings:
    previous = pack_dir / "previous"
    if not previous.exists():
        raise ConfigurationError("There is no previous pack to go back to")
    active = pack_dir / "active"
    swap = pack_dir / "swap"
    if swap.exists():
        shutil.rmtree(swap)
    if active.exists():
        os.replace(active, swap)
    os.replace(previous, active)
    if swap.exists():
        os.replace(swap, previous)
    old_key = key_file.with_name(key_file.name + ".previous")
    if old_key.exists():
        current = key_file.with_name(key_file.name + ".swap")
        if key_file.exists():
            os.replace(key_file, current)
        os.replace(old_key, key_file)
        if current.exists():
            os.replace(current, old_key)
    return load_active(pack_dir)


def load_active(pack_dir: Path) -> PackSettings | None:
    path = pack_dir / "active" / "pack.json"
    if not path.exists():
        return None
    try:
        return PackSettings.from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ConfigurationError(f"The installed pack is damaged ({path}): {exc}") from exc


def read_document(pack_dir: Path, name: str) -> str | None:
    """A kept document, by file name, for the console and the AI troubleshooter."""
    folder = pack_dir / "active" / "documents"
    target = (folder / name).resolve()
    if target.parent != folder.resolve() or not target.is_file():
        return None
    return target.read_text(encoding="utf-8")


def read_key(key_file: Path) -> str:
    try:
        key = key_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigurationError(f"The PeakAI laptop key is not readable at {key_file}") from exc
    if not key:
        raise ConfigurationError(f"The PeakAI laptop key file {key_file} is empty")
    return key
