import hashlib
import os
import subprocess
import sys
import tracemalloc

import pytest

from src.core.enums import ScanStatus
from src.core.exceptions import SourceDisconnectedError
from src.core.settings import FileScanningSettings
from src.database.db import Database
from src.database.repositories import ScanRepository
from src.inventory.file_scanner import (
    ExcludedEntry,
    FileScanner,
    ScannedDirectory,
    ScannedFile,
    ScanProblem,
)
from src.inventory.inventory_service import ScanService
from src.platform.paths import to_extended_path


def make(root, tree):
    for relative, data in tree.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def scan(root, **options):
    return list(FileScanner(FileScanningSettings(**options)).walk(str(root)))


def service():
    return ScanService(ScanRepository(Database(":memory:")), FileScanningSettings())


def test_counts_and_bytes(tmp_path):
    make(tmp_path, {"DCIM/100/a.MP4": b"12345", "DCIM/b.jpg": b"123", "c": b"", "d.mp4": b"1"})
    summary = service().scan_card("card", str(tmp_path))
    assert summary.file_count == 4
    assert summary.total_bytes == 9
    assert summary.dir_count == 2
    assert summary.by_extension == {".mp4": 2, ".jpg": 1, "(none)": 1}
    assert summary.status == ScanStatus.COMPLETE


def test_relative_paths_use_forward_slashes(tmp_path):
    make(tmp_path, {"a/b/c.txt": b"x"})
    files = [e for e in scan(tmp_path) if isinstance(e, ScannedFile)]
    assert files[0].relative_path == "a/b/c.txt"


def test_unicode_names(tmp_path):
    make(tmp_path, {"वीडियो/फ़ाइल १.mp4": b"x", "日本語.txt": b"y", "emoji😀.txt": b"z"})
    summary = service().scan_card("card", str(tmp_path))
    assert summary.file_count == 3 and summary.error_count == 0


def test_path_beyond_260_characters(tmp_path):
    relative = "/".join(["d" * 50] * 7) + "/long.mp4"
    assert len(str(tmp_path / relative)) > 300
    path = to_extended_path(os.path.join(str(tmp_path), *relative.split("/")))
    os.makedirs(os.path.dirname(path))
    with open(path, "wb") as handle:
        handle.write(b"data")
    summary = service().scan_card("card", str(tmp_path))
    assert summary.file_count == 1 and summary.total_bytes == 4 and summary.error_count == 0


def test_excluded_system_folders_are_counted_not_silent(tmp_path):
    make(tmp_path, {"System Volume Information/x": b"x", "keep.txt": b"y"})
    events = scan(tmp_path, exclude_dirs=["System Volume Information"])
    assert sum(isinstance(e, ExcludedEntry) for e in events) == 1
    assert [e.relative_path for e in events if isinstance(e, ScannedFile)] == ["keep.txt"]


def test_hidden_files_kept_by_default_and_optional_to_skip(tmp_path):
    make(tmp_path, {".hidden": b"x", "seen": b"y"})
    assert sum(isinstance(e, ScannedFile) for e in scan(tmp_path)) == 2
    assert sum(isinstance(e, ScannedFile) for e in scan(tmp_path, ignore_hidden_files=True)) == 1


def _try_symlink(link, target, **kwargs):
    if sys.platform == "win32":  # Junctions are reparse points and need no special rights.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True,
                       capture_output=True)
    else:
        os.symlink(target, link, **kwargs)


def test_links_are_not_followed_by_default(tmp_path):
    outside = tmp_path / "outside"
    make(outside, {"secret.txt": b"x"})
    card = tmp_path / "card"
    make(card, {"a.txt": b"y"})
    _try_symlink(card / "link", outside, target_is_directory=True)
    events = scan(card)
    assert [e.relative_path for e in events if isinstance(e, ScannedFile)] == ["a.txt"]
    assert any(isinstance(e, ScanProblem) and e.kind == "LINK_SKIPPED" for e in events)


def test_followed_link_cannot_leave_volume(tmp_path):
    outside = tmp_path / "outside"
    make(outside, {"secret.txt": b"x"})
    card = tmp_path / "card"
    make(card, {"a.txt": b"y"})
    _try_symlink(card / "link", outside, target_is_directory=True)
    events = scan(card, follow_symlinks=True)
    assert not any(isinstance(e, ScannedFile) and "secret" in e.relative_path for e in events)
    assert any(isinstance(e, ScanProblem) and e.kind == "LINK_OUTSIDE_VOLUME" for e in events)


def test_permission_error_is_per_path(tmp_path, monkeypatch):
    make(tmp_path, {"good/a.txt": b"x", "bad/b.txt": b"y"})
    real = os.scandir

    def guarded(path):
        if str(path).endswith("bad"):
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr("src.inventory.file_scanner.os.scandir", guarded)
    summary = service().scan_card("card", str(tmp_path))
    assert summary.file_count == 1
    assert summary.unreadable_count == 1
    assert summary.status == ScanStatus.COMPLETE_WITH_ERRORS


def test_card_removed_during_scan(tmp_path, monkeypatch):
    make(tmp_path, {"a/x.txt": b"x"})
    real = os.scandir
    calls = []

    def vanish(path):
        calls.append(path)
        if len(calls) > 1:
            raise OSError(21, "not ready")
        return real(path)

    monkeypatch.setattr("src.inventory.file_scanner.os.scandir", vanish)
    monkeypatch.setattr("src.inventory.file_scanner.mount_is_present", lambda _: False)
    summary = service().scan_card("card", str(tmp_path))
    assert summary.status == ScanStatus.DISCONNECTED
    assert summary.error_count == 1


def test_walk_raises_when_mount_is_gone(tmp_path):
    with pytest.raises(SourceDisconnectedError):
        list(FileScanner(FileScanningSettings()).walk(str(tmp_path / "gone")))


def test_sha256_only_when_asked(tmp_path):
    make(tmp_path, {"a": b"abc"})
    plain = FileScanner(FileScanningSettings()).walk(str(tmp_path))
    assert next(e for e in plain if isinstance(e, ScannedFile)).sha256 is None
    hashed = FileScanner(FileScanningSettings(), hash_files=True).walk(str(tmp_path))
    assert next(e for e in hashed if isinstance(e, ScannedFile)).sha256 == \
        hashlib.sha256(b"abc").hexdigest()


def test_walk_is_streaming(tmp_path):
    make(tmp_path, {f"d{i}/f.txt": b"x" for i in range(5)})
    walker = FileScanner(FileScanningSettings()).walk(str(tmp_path))
    assert isinstance(next(walker), ScannedDirectory | ScannedFile)  # Yields before finishing.


def test_large_tree_memory_stays_bounded(tmp_path):
    for folder in range(40):
        directory = tmp_path / f"d{folder}"
        directory.mkdir()
        for name in range(50):
            (directory / f"file_{name}.bin").touch()
    tracemalloc.start()
    count = sum(1 for _ in FileScanner(FileScanningSettings()).walk(str(tmp_path)))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert count == 2040
    assert peak < 2 * 1024 * 1024


def test_scan_changes_nothing(tmp_path):
    make(tmp_path, {"a/b.mp4": b"x" * 1000, "c.txt": b"y"})
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    service().scan_card("card", str(tmp_path))
    after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after
