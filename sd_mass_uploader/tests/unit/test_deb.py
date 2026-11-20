"""The Debian package: structure, ownership, permissions, contents, and the installed settings."""

from __future__ import annotations

import gzip
import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from src.cli.commands import main
from src.core.settings import load_settings

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import build_deb  # noqa: E402


def read_ar(data: bytes) -> list[tuple[str, bytes]]:
    assert data[:8] == b"!<arch>\n"
    members, position = [], 8
    while position < len(data):
        header = data[position:position + 60]
        name, size = header[:16].decode().strip(), int(header[48:58].decode().strip())
        assert header[58:60] == b"`\n"
        assert header[40:48].decode().strip() == "100644"
        body = data[position + 60:position + 60 + size]
        members.append((name, body))
        position += 60 + size + (size % 2)
    return members


@pytest.fixture(scope="module")
def deb(tmp_path_factory):
    out = tmp_path_factory.mktemp("deb")
    if not (ROOT / "src" / "web_dist" / "index.html").exists():
        pytest.skip("console not built")
    build_deb.stage(out / "root", None)
    path = build_deb.write_deb(out / "root", out, build_deb.DEFAULT_MAINTAINER, 1_800_000_000)
    members = dict(read_ar(path.read_bytes()))
    control = tarfile.open(fileobj=io.BytesIO(gzip.decompress(members["control.tar.gz"])))
    data = tarfile.open(fileobj=io.BytesIO(gzip.decompress(members["data.tar.gz"])))
    return path, members, control, data


def test_archive_structure_and_control(deb):
    path, members, control, _ = deb
    assert list(members) == ["debian-binary", "control.tar.gz", "data.tar.gz"]
    assert members["debian-binary"] == b"2.0\n"
    assert path.name == f"sd-mass-uploader_{build_deb.version()}-1_amd64.deb"
    fields = dict(line.split(": ", 1) for line in
                  control.extractfile("./control").read().decode().splitlines()
                  if ": " in line and not line.startswith(" "))
    assert fields["Package"] == "sd-mass-uploader" and fields["Architecture"] == "amd64"
    assert fields["Version"] == f"{build_deb.version()}-1"
    assert "python3 (>= 3.12)" in fields["Depends"] and "udisks2" in fields["Depends"]
    assert int(fields["Installed-Size"]) > 0
    assert "example.invalid" in fields["Maintainer"]  # No personal address baked in.


def test_everything_is_owned_by_root_with_sane_modes(deb):
    _, _, _, data = deb
    members = {item.name: item for item in data.getmembers()}
    for item in members.values():
        assert (item.uid, item.gid, item.uname, item.gname) == (0, 0, "root", "root")
        assert item.mode & 0o022 == 0, item.name  # Nothing group- or world-writable.
    executables = {"./usr/bin/sdu", "./opt/sd-mass-uploader/scripts/ubuntu/kiosk.sh"}
    for name in executables:
        assert members[name].mode == 0o755
    assert members["./etc/sd-mass-uploader/config.yaml"].mode == 0o644
    assert members["./lib/systemd/system/sdu-daemon.service"].mode == 0o644


def test_no_secrets_caches_or_local_station_data(deb):
    _, _, _, data = deb
    names = [item.name for item in data.getmembers()]
    for bad in ("__pycache__", ".pyc", "data/station", "laptop_key", "api-token", ".env",
                "egg-info", "uploader.db", "node_modules"):
        assert not any(bad in name for name in names), bad
    assert "./opt/sd-mass-uploader/src/web_dist/index.html" in names
    assert "./opt/sd-mass-uploader/src/station/daemon.py" in names
    assert "./usr/share/polkit-1/rules.d/50-sd-mass-uploader.rules" in names


def test_conffiles_and_md5sums(deb):
    _, _, control, data = deb
    names = {item.name for item in data.getmembers()}
    conffiles = control.extractfile("./conffiles").read().decode().split()
    assert conffiles == build_deb.CONFFILES
    assert all(f".{path}" in names for path in conffiles)
    for line in control.extractfile("./md5sums").read().decode().splitlines():
        digest, relative = line.split("  ", 1)
        assert hashlib.md5(data.extractfile(f"./{relative}").read()).hexdigest() == digest


def test_maintainer_scripts(deb, tmp_path):
    _, _, control, _ = deb
    shell = shutil.which("sh") or shutil.which("bash")
    for name in ("postinst", "prerm", "postrm"):
        item = control.getmember(f"./{name}")
        body = control.extractfile(item).read()
        assert item.mode == 0o755 and b"\r\n" not in body
        assert body.startswith(b"#!/bin/sh\n") and b"set -e" in body
        if shell:
            script = tmp_path / name
            script.write_bytes(body)
            assert subprocess.run([shell, "-n", str(script)]).returncode == 0
    postinst = control.extractfile("./postinst").read().decode()
    assert "-m 0700 \"$DATA/database\" \"$DATA/secrets\"" in postinst
    assert "--no-index" in postinst and "--system-site-packages" in postinst


def test_installed_config_is_valid_and_absolute(deb, tmp_path):
    _, _, _, data = deb
    config = tmp_path / "config.yaml"
    config.write_bytes(data.extractfile("./etc/sd-mass-uploader/config.yaml").read())
    settings = load_settings(config)
    paths = [settings.database.path, settings.reporting.directory, settings.logging.directory,
             settings.station.state_file, settings.api.token_file, settings.peakai.pack_dir,
             settings.peakai.key_file]
    assert all(path.startswith("/var/") for path in paths)
    assert settings.peakai.key_file.startswith("/var/lib/sd-mass-uploader/secrets/")
    assert settings.peakai.vendor_code == "" and settings.station.simulator_dir == ""


def test_service_and_menu_point_at_installed_paths(deb):
    _, _, _, data = deb
    names = {item.name for item in data.getmembers()}
    assert "./opt/sd-mass-uploader/main.py" in names
    for name in ("./lib/systemd/system/sdu-daemon.service",
                 "./usr/share/applications/sd-uploader-console.desktop",
                 "./etc/xdg/autostart/sd-uploader-indicator.desktop"):
        text = data.extractfile(name).read().decode()
        assert "/opt/sd-mass-uploader/venv/bin/python /opt/sd-mass-uploader/main.py" in text
    wrapper = data.extractfile("./usr/bin/sdu").read().decode()
    assert "/opt/sd-mass-uploader/main.py" in wrapper and "\r" not in wrapper


# ------------------------------------------------------------------ operators without access

def test_operator_commands_work_without_database_or_log_access(tmp_path, capsys):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        f'database:\n  path: "{(blocker / "db" / "uploader.db").as_posix()}"\n'
        f'logging:\n  directory: "{(blocker / "logs").as_posix()}"\n'
        "api:\n  port: 18997\n", encoding="utf-8")
    token = tmp_path / "data" / "station" / "api-token"
    token.parent.mkdir(parents=True)
    token.write_text("tok", encoding="utf-8")
    assert main(["--config", str(config), "console", "--print-link"]) == 0
    assert "#token=tok" in capsys.readouterr().out
    assert main(["--config", str(config), "problems"]) == 0
    code = main(["--config", str(config), "status"])  # Needs the database: a clear refusal.
    assert code == 2
    assert "sudo -u sdu" in capsys.readouterr().err
