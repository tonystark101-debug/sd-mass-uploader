"""Session reports. Every figure is computed from recorded measurements."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.enums import TaskStatus
from src.core.timeutil import utc_now
from src.database.repositories import ScanRepository, SessionRepository

_NOT_BLOCKING = {"LINK_SKIPPED", "NOT_PROJECT_FOOTAGE", "SKIPPED_BY_OPERATOR"}
_CSV_COLUMNS = ["card_id", "relative_path", "object_name", "size_bytes", "status", "attempts",
                "local_crc32c", "local_sha256", "remote_generation", "uploaded_at", "error_class",
                "last_error"]


def count_blocking_errors(scans: ScanRepository, session_id: str) -> int:
    """Files that were never queued, so the session cannot claim complete success."""
    return sum(1 for error in scans.errors_for_session(session_id)
               if error["kind"] not in _NOT_BLOCKING)


@dataclass
class ReportPaths:
    json_path: Path | None
    csv_path: Path | None
    errors_path: Path


class ReportGenerator:
    def __init__(self, sessions: SessionRepository, scans: ScanRepository, directory: Path,
                 write_json: bool = True, write_csv: bool = True) -> None:
        self._sessions = sessions
        self._scans = scans
        self._directory = directory
        self._write_json = write_json
        self._write_csv = write_csv

    def build(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        counts = self._sessions.status_counts(session_id)
        scans = self._scans.for_session(session_id)
        totals = self._sessions.attempt_totals(session_id)

        def files(status: TaskStatus) -> int:
            return counts.get(status.value, {}).get("files", 0)

        def size(status: TaskStatus) -> int:
            return counts.get(status.value, {}).get("bytes", 0)

        failed = sum(files(s) for s in (TaskStatus.FAILED, TaskStatus.FAILED_PERMANENT))
        elapsed = session["upload_seconds"]
        transferred = totals["bytes"]
        return {
            "session_id": session_id,
            "status": session["status"],
            "generated_at": utc_now(),
            "created_at": session["created_at"],
            "destination_prefix": session["base_prefix"],
            "last_error": session["last_error"],
            "metadata": self._sessions.metadata(session_id),
            "cards_discovered": len(self._sessions.cards(session_id)),
            "cards_scanned": len(scans),
            "cards_with_errors": sum(1 for scan in scans if scan["error_count"]),
            "scan_errors": count_blocking_errors(self._scans, session_id),
            "files_discovered": sum(scan["file_count"] for scan in scans),
            "files_verified": files(TaskStatus.VERIFIED),
            "files_uploaded_unverified": files(TaskStatus.UPLOADED_UNVERIFIED),
            "files_failed": failed,
            "files_conflict": files(TaskStatus.CONFLICT),
            "files_uncertain": files(TaskStatus.UNCERTAIN),
            "files_pending": files(TaskStatus.PENDING),
            "source_bytes": sum(scan["total_bytes"] for scan in scans),
            "verified_bytes": size(TaskStatus.VERIFIED),
            "transferred_bytes": transferred,
            "failed_attempts": totals["failures"],
            "elapsed_seconds": round(elapsed, 3),
            "average_mb_per_s": round(transferred / elapsed / 1_000_000, 3) if elapsed else None,
            "average_mib_per_s": round(transferred / elapsed / 1_048_576, 3) if elapsed else None,
            "task_counts": {key: value["files"] for key, value in counts.items()},
        }

    def write(self, session_id: str) -> ReportPaths:
        folder = self._directory / session_id
        folder.mkdir(parents=True, exist_ok=True)
        json_path = folder / "report.json" if self._write_json else None
        if json_path:
            document = self.build(session_id)
            document["cards"] = self._cards(session_id)
            json_path.write_text(json.dumps(document, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
        csv_path = folder / "files.csv" if self._write_csv else None
        if csv_path:
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, _CSV_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(self._sessions.iter_task_rows(session_id))
        errors_path = folder / "errors.log"
        with open(errors_path, "w", encoding="utf-8") as handle:
            for error in self._scans.errors_for_session(session_id):
                handle.write(f"SCAN {error['card_id']} {error['kind']} {error['path']}: "
                             f"{error['message']}\n")
            for row in self._sessions.iter_task_rows(session_id):
                if row["status"] not in (TaskStatus.VERIFIED, TaskStatus.PENDING):
                    handle.write(f"UPLOAD {row['card_id']} {row['status']} "
                                 f"{row['relative_path']}: {row['error_class']}: "
                                 f"{row['last_error']}\n")
        return ReportPaths(json_path, csv_path, errors_path)

    def _cards(self, session_id: str) -> list[dict]:
        scans = {scan["card_id"]: scan for scan in self._scans.for_session(session_id)}
        result = []
        for card in self._sessions.cards(session_id):
            scan = scans.get(card["card_id"], {})
            result.append({
                "card_id": card["card_id"],
                "volume_label": card["volume_label"],
                "reader_model": card["reader_model"],
                "scan_status": scan.get("status"),
                "file_count": scan.get("file_count"),
                "total_bytes": scan.get("total_bytes"),
                "dir_count": scan.get("dir_count"),
                "unreadable_count": scan.get("unreadable_count"),
                "error_count": scan.get("error_count"),
                "by_extension": json.loads(scan.get("by_extension_json") or "{}"),
            })
        return result

    def manifests(self, session_id: str) -> dict[str, dict]:
        """The documents stored beside the uploaded files."""
        session = self.build(session_id)
        files = [
            {key: row[key] for key in ("card_id", "relative_path", "object_name", "size_bytes",
                                       "local_crc32c", "local_sha256", "status")}
            for row in self._sessions.iter_task_rows(session_id)
        ]
        return {
            "session-manifest.json": session,
            "card-inventory.json": {
                "session_id": session_id,
                "cards": self._cards(session_id),
                "files": files,
            },
        }
