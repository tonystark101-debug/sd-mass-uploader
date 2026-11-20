"""Build the Debian package: sd-mass-uploader_<version>_amd64.deb

    python scripts/build_deb.py                     # Full build with bundled Python packages
    python scripts/build_deb.py --skip-wheels       # Layout only (quick check, not installable)

Works on Linux and Windows: the .deb (an `ar` archive of two tar files) is written directly, so
ownership (root) and permissions are exact. Needs `uv` (to resolve the Linux dependency lock)
and a Python with pip (to download the Linux wheels); stations then install with no internet.
Run `npm run build` in web/ first: the console is shipped prebuilt.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEBIAN = ROOT / "packaging" / "debian"
LINUX = ROOT / "packaging" / "linux"
APP = "opt/sd-mass-uploader"
PYTHONS = ("3.12", "3.13")  # Ubuntu 24.04 LTS, Kali Rolling.
LINUX_PLATFORM = "x86_64-manylinux_2_28"
PIP_PLATFORMS = ("manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64",
                 "manylinux_2_5_x86_64", "manylinux1_x86_64", "linux_x86_64")
DEFAULT_MAINTAINER = "SD Uploader maintainers <noreply@example.invalid>"
EXECUTABLE = {"usr/bin/sdu", f"{APP}/scripts/ubuntu/kiosk.sh", f"{APP}/main.py"}
CONFFILES = ["/etc/sd-mass-uploader/config.yaml",
             "/etc/xdg/autostart/sd-uploader-indicator.desktop"]


def version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version = "([^"]+)"', text, re.M).group(1)


# ---------------------------------------------------------------------- wheels

def build_wheelhouse(out: Path, pip_python: str) -> None:
    """Exact Linux locks per Python version, then their Linux wheels."""
    for python in PYTHONS:
        tag = "cp" + python.replace(".", "")
        target = out / tag
        target.mkdir(parents=True, exist_ok=True)
        lock = target / "requirements.txt"
        subprocess.run(["uv", "pip", "compile", str(ROOT / "requirements.txt"), "--quiet",
                        "--python-platform", LINUX_PLATFORM, "--python-version", python,
                        "--no-header", "--no-annotate", "-o", str(lock)], check=True)
        pins = [line.strip() for line in lock.read_text().splitlines()
                if line.strip() and not line.startswith("#")]
        for pin in pins:
            _download(pin, python, tag, target, pip_python)
        print(f"{tag}: {len(pins)} packages")


def _download(pin: str, python: str, tag: str, target: Path, pip_python: str) -> None:
    command = [pip_python, "-m", "pip", "download", "--quiet", "--no-deps",
               "--only-binary=:all:", "--python-version", python, "--implementation", "cp",
               "--abi", tag, "--abi", "abi3", "--abi", "none", "-d", str(target), pin]
    for platform in PIP_PLATFORMS:
        command[command.index("--python-version"):command.index("--python-version")] = [
            "--platform", platform]
    if subprocess.run(command, capture_output=True).returncode == 0:
        return
    # No wheel published (pure-Python source only): build one; it must be platform-free.
    before = set(target.iterdir())
    subprocess.run([pip_python, "-m", "pip", "wheel", "--quiet", "--no-deps", "-w",
                    str(target), pin], check=True)
    for path in set(target.iterdir()) - before:
        if not path.name.endswith("-none-any.whl"):
            path.unlink()
            raise SystemExit(f"{pin} has no Linux wheel and is not pure Python")


# ---------------------------------------------------------------------- staging

def stage(tree: Path, wheels: Path | None) -> None:
    if tree.exists():
        shutil.rmtree(tree)
    app = tree / APP
    copy = shutil.copy2

    def put(source: Path, destination: str) -> None:
        target = tree / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        copy(source, target)

    if not (ROOT / "src" / "web_dist" / "index.html").exists():
        raise SystemExit("Build the console first: cd web && npm run build")
    shutil.copytree(ROOT / "src", app / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "*.pyc"))
    for name in ("main.py", "requirements.txt", "README.md"):
        put(ROOT / name, f"{APP}/{name}")
    put(ROOT / "docs" / "PROBLEMS.md", f"{APP}/docs/PROBLEMS.md")
    put(ROOT / "docs" / "HARDWARE_ACCEPTANCE.md", f"{APP}/docs/HARDWARE_ACCEPTANCE.md")
    put(ROOT / "scripts" / "ubuntu" / "kiosk.sh", f"{APP}/scripts/ubuntu/kiosk.sh")
    put(LINUX / "kiosk" / "sdu-kiosk.service", f"{APP}/packaging/linux/kiosk/sdu-kiosk.service")
    if wheels is not None:
        shutil.copytree(wheels, app / "wheels")
    put(DEBIAN / "config.yaml", "etc/sd-mass-uploader/config.yaml")
    put(DEBIAN / "sdu", "usr/bin/sdu")
    put(LINUX / "sdu-daemon.service", "lib/systemd/system/sdu-daemon.service")
    put(LINUX / "50-sd-mass-uploader.rules", "usr/share/polkit-1/rules.d/50-sd-mass-uploader.rules")
    put(LINUX / "sd-uploader-console.desktop",
        "usr/share/applications/sd-uploader-console.desktop")
    put(LINUX / "sd-uploader-indicator.desktop",
        "etc/xdg/autostart/sd-uploader-indicator.desktop")
    put(LINUX / "icons" / "sd-uploader.svg",
        "usr/share/icons/hicolor/scalable/apps/sd-uploader.svg")
    put(ROOT / "README.md", "usr/share/doc/sd-mass-uploader/README.md")


# ---------------------------------------------------------------------- the .deb itself

def _mode(relative: str, is_dir: bool) -> int:
    if is_dir:
        return 0o755
    return 0o755 if relative in EXECUTABLE else 0o644


def _tar(entries: list[tuple[str, bytes | None, int]], mtime: int) -> bytes:
    """entries: (path, data or None for a directory, mode). Everything owned by root."""
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=mtime) as raw, \
            tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.mode, info.mtime = mode, mtime
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _ar(members: list[tuple[str, bytes]], mtime: int) -> bytes:
    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, data in members:
        header = (f"{name:<16}{mtime:<12}{0:<6}{0:<6}{0o100644:<8o}{len(data):<10}`\n")
        out.write(header.encode("ascii"))
        out.write(data)
        if len(data) % 2:
            out.write(b"\n")
    return out.getvalue()


def write_deb(tree: Path, out: Path, maintainer: str, mtime: int) -> Path:
    data_entries: list[tuple[str, bytes | None, int]] = []
    md5sums: list[str] = []
    installed = 0
    for path in sorted(tree.rglob("*")):
        relative = path.relative_to(tree).as_posix()
        if path.is_dir():
            data_entries.append((f"./{relative}/", None, _mode(relative, True)))
            continue
        content = path.read_bytes()
        installed += len(content)
        data_entries.append((f"./{relative}", content, _mode(relative, False)))
        if f"/{relative}" not in CONFFILES:
            md5sums.append(f"{hashlib.md5(content).hexdigest()}  {relative}")
    control = (DEBIAN / "control.in").read_text(encoding="utf-8").format(
        version=f"{version()}-1", maintainer=maintainer,
        installed_kb=max(1, installed // 1024))
    control_entries: list[tuple[str, bytes | None, int]] = [
        ("./", None, 0o755),
        ("./control", control.encode(), 0o644),
        ("./conffiles", ("\n".join(CONFFILES) + "\n").encode(), 0o644),
        ("./md5sums", ("\n".join(md5sums) + "\n").encode(), 0o644),
    ]
    for script in ("postinst", "prerm", "postrm"):
        control_entries.append((f"./{script}", (DEBIAN / script).read_bytes().replace(
            b"\r\n", b"\n"), 0o755))
    package = _ar([("debian-binary", b"2.0\n"),
                   ("control.tar.gz", _tar(control_entries, mtime)),
                   ("data.tar.gz", _tar([("./", None, 0o755), *data_entries], mtime))], mtime)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"sd-mass-uploader_{version()}-1_amd64.deb"
    target.write_bytes(package)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-wheels", action="store_true",
                        help="Leave out the Python packages (not installable; for checks)")
    parser.add_argument("--out", default=str(ROOT / "dist"))
    parser.add_argument("--maintainer", default=DEFAULT_MAINTAINER)
    parser.add_argument("--pip-python", default=sys.executable,
                        help="A Python with pip, used to download the Linux wheels")
    args = parser.parse_args(argv)
    build = ROOT / "build" / "deb"
    mtime = int(os.environ.get("SOURCE_DATE_EPOCH", time.time()))
    wheels = None
    if not args.skip_wheels:
        wheels = build / "wheels"
        if wheels.exists():
            shutil.rmtree(wheels)
        build_wheelhouse(wheels, args.pip_python)
    stage(build / "root", wheels)
    target = write_deb(build / "root", Path(args.out), args.maintainer, mtime)
    print(f"Built {target} ({target.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
