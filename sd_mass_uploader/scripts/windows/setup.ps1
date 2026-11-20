# Creates .venv and installs pinned dependencies. Safe to run again.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..\..')
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv venv --allow-existing --python 3.12 .venv
    uv pip install --python .venv\Scripts\python.exe -r requirements-dev.txt
} else {
    py -3.12 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
}
.\.venv\Scripts\python.exe --version
Write-Host 'Ready. Next: edit config.yaml, then run: python main.py check'
