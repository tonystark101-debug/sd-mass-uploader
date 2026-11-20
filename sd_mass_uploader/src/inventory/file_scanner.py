"""Streaming, read-only directory walk. Yields one result at a time; never builds a tree."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass

from src.core.exceptions import SourceDisconnectedError
from src.core.settings import FileScanningSettings
from src.platform.paths import (
    mount_is_present,
    open_source_readonly,
    to_extended_path,
)

_HASH_BLOCK = 1024 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)


@dataclass
class ScannedFile:
    relative_path: str  # Always with forward slashes.
    file_name: str
    extension: str
    size_bytes: int
    mtime_ns: int
    readable: bool = True
    sha256: str | None = None


@dataclass
class ScanProblem:
    path: str
    kind: str
    message: str


@dataclass
class ScannedDirectory:
    relative_path: str


@dataclass
class ExcludedEntry:
    relative_path: str


ScanEvent = ScannedFile | ScanProblem | ScannedDirectory | ExcludedEntry


def _can_store(text: str) -> bool:
    """Names with undecodable bytes cannot be stored in SQLite or named in GCS."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_link(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _is_hidden(entry: os.DirEntry) -> bool:
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return entry.name.startswith(".") or bool(attributes & _HIDDEN)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open_source_readonly(path) as handle:
        while block := handle.read(_HASH_BLOCK):
            digest.update(block)
    return digest.hexdigest()


class FileScanner:
    """Walks one volume. Raises SourceDisconnectedError if the card vanishes."""

    def __init__(self, settings: FileScanningSettings, hash_files: bool = False) -> None:
        self._settings = settings
        self._hash = hash_files
        self._excluded = {name.casefold() for name in settings.exclude_dirs}

    def walk(self, root: str) -> Iterator[ScanEvent]:
        base = to_extended_path(os.path.abspath(root))
        stack = [""]
        seen_links: set[str] = set()
        while stack:
            relative = stack.pop()
            directory = os.path.join(base, *relative.split("/")) if relative else base
            try:
                with os.scandir(directory) as entries:
                    children = list(entries)  # One directory at a time, not the tree.
            except OSError as exc:
                self._raise_if_gone(root, exc)
                yield ScanProblem(relative or ".", "DIRECTORY_UNREADABLE", str(exc))
                continue
            for entry in children:
                child = f"{relative}/{entry.name}" if relative else entry.name
                yield from self._visit(entry, child, root, base, stack, seen_links)

    def _visit(self, entry: os.DirEntry, relative: str, root: str, base: str, stack: list[str],
               seen_links: set[str]) -> Iterator[ScanEvent]:
        if not _can_store(entry.name):
            yield ScanProblem(ascii(relative), "INVALID_FILENAME", "Name is not valid Unicode")
            return
        try:
            if self._settings.ignore_hidden_files and _is_hidden(entry):
                yield ExcludedEntry(relative)
                return
            if _is_link(entry):
                if not self._settings.follow_symlinks:
                    yield ScanProblem(relative, "LINK_SKIPPED", "Link not followed")
                    return
                if not self._link_inside_volume(entry.path, base, seen_links):
                    yield ScanProblem(relative, "LINK_OUTSIDE_VOLUME", "Link target refused")
                    return
            if entry.is_dir():
                if entry.name.casefold() in self._excluded:
                    yield ExcludedEntry(relative)
                else:
                    yield ScannedDirectory(relative)
                    stack.append(relative)
            elif entry.is_file():
                yield self._file(entry, relative)
            else:
                yield ScanProblem(relative, "NOT_A_REGULAR_FILE", "Special file skipped")
        except OSError as exc:
            self._raise_if_gone(root, exc)
            yield ScanProblem(relative, "ENTRY_UNREADABLE", str(exc))

    @staticmethod
    def _link_inside_volume(path: str, base: str, seen: set[str]) -> bool:
        target = os.path.realpath(path)
        canonical = os.path.realpath(base)
        if os.path.commonpath([target, canonical]) != canonical or target in seen:
            return False
        seen.add(target)
        return True

    def _file(self, entry: os.DirEntry, relative: str) -> ScannedFile | ScanProblem:
        info = entry.stat()
        name = entry.name
        result = ScannedFile(
            relative_path=relative,
            file_name=name,
            extension=os.path.splitext(name)[1].lower(),
            size_bytes=info.st_size,
            mtime_ns=info.st_mtime_ns,
        )
        if self._hash:
            result.sha256 = _sha256(entry.path)
        return result

    @staticmethod
    def _raise_if_gone(root: str, exc: OSError) -> None:
        """One bad file is a per-path error; only a missing mount means the card went away."""
        if not mount_is_present(root):
            raise SourceDisconnectedError(f"Card at {root} disappeared: {exc}") from exc
