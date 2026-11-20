#!/usr/bin/env bash
# Creates .venv and installs pinned dependencies. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")/../.."
command -v lsblk >/dev/null || { echo "lsblk is required: sudo apt install util-linux"; exit 1; }
command -v lsusb >/dev/null || echo "Note: lsusb not found. Optional: sudo apt install usbutils"
if command -v uv >/dev/null; then
    uv venv --allow-existing --python 3.12 .venv
    uv pip install --python .venv/bin/python -r requirements-dev.txt
elif command -v python3.12 >/dev/null; then
    python3.12 -m venv .venv || {
        echo "Could not create .venv. Try: sudo apt install python3.12-venv"; exit 1; }
    .venv/bin/python -m pip install -r requirements-dev.txt
else
    echo "Python 3.12 not found. Install uv, then run this again:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi
.venv/bin/python --version
echo "Ready. Next: edit config.yaml, then run: python main.py check"
