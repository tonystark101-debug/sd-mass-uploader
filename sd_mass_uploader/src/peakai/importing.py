"""Check a pack zip and, unless offline, ask the token service which passes this laptop has.

Shared by `main.py pack import` and the console's Setup screen. Changes nothing on disk.
"""

from __future__ import annotations

from pathlib import Path

from src.core.settings import PeakAiSettings
from src.peakai.keeper import PassKeeper
from src.peakai.pack import ImportReport, read_pack


def check_pack(zip_path: str | Path, settings: PeakAiSettings,
               offline: bool = False) -> ImportReport:
    report = read_pack(zip_path)
    if report.ok and not offline:
        keeper = PassKeeper(report.settings, settings, lambda: report.key)
        try:
            report.live_passes = keeper.refresh_passes()
            unknown = [name for name in report.live_passes
                       if name not in report.settings.passes and name not in ("mono", "stereo")]
            if unknown:
                report.warnings.append("The token service offers passes the rules do not "
                                       "describe: " + ", ".join(unknown))
            if keeper.legacy_only():
                report.warnings.append("This laptop still has only the old mono/stereo "
                                       "passes; PeakAI has not switched it yet")
        except Exception as exc:  # noqa: BLE001 - reported; importing offline is allowed
            report.warnings.append("Could not ask the token service which passes this "
                                   f"laptop has: {exc}")
    if report.ok and not settings.vendor_code:
        report.warnings.append("peakai.vendor_code is not set in config.yaml; sessions cannot "
                               "start until it is (the `vendor` value in _info files)")
    return report


def report_summary(report: ImportReport) -> dict:
    """For the console. Never contains the key."""
    settings = report.settings
    return {
        "ok": report.ok,
        "lines": report.lines(),
        "errors": report.errors,
        "warnings": report.warnings,
        "live_passes": report.live_passes,
        "pack": None if settings is None else {
            "name": settings.source_name, "version": settings.version, "dated": settings.dated,
            "bucket": settings.bucket, "token_url": settings.token_url,
            "passes": {name: {"folder": rule.folder, "description": rule.description}
                       for name, rule in settings.passes.items()},
            "activities": settings.activities,
        },
    }
