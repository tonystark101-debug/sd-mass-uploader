"""All SQL lives here. Other modules use these functions, never the connection."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from typing import Any

from src.core.enums import (
    AccessStatus,
    DeviceStatus,
    ReaderKind,
    SessionStatus,
    SpeedConfidence,
    TaskStatus,
)
from src.core.models import (
    DetectionIssue,
    InventorySnapshot,
    Reader,
    ScanSummary,
    StorageDevice,
    UsbDevice,
    Volume,
)
from src.core.timeutil import utc_now
from src.database.db import Database

ACTIVE_SESSION_STATES = (SessionStatus.UPLOADING, SessionStatus.STOPPED, SessionStatus.READY)

# Station cards that are paused, removed or not yet released hold back their tasks.
# Sessions started from the CLI have no station_cards rows, so nothing is held for them.
_NOT_HELD = (
    " AND card_id NOT IN (SELECT card_id FROM station_cards"
    " WHERE session_id=upload_tasks.session_id"
    " AND (phase<>'QUEUED' OR present=0 OR paused=1))"
)


def _record(item: Any) -> str:
    return json.dumps(asdict(item), default=str, ensure_ascii=False)


class InventoryRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def save(self, snapshot: InventorySnapshot) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO device_snapshots VALUES (?, ?, ?, ?, ?)",
                (snapshot.snapshot_id, snapshot.captured_at, snapshot.platform, snapshot.hostname,
                 json.dumps([asdict(issue) for issue in snapshot.issues], ensure_ascii=False)),
            )
            sid = snapshot.snapshot_id
            tx.executemany(
                "INSERT INTO usb_devices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(sid, d.topology_id, d.parent_topology_id, d.vendor_id, d.product_id,
                  d.product_name, int(d.is_hub), d.negotiated_speed_mbps, d.speed_source,
                  d.speed_confidence.value, _record(d)) for d in snapshot.usb_devices],
            )
            tx.executemany(
                "INSERT INTO readers VALUES (?, ?, ?, ?, ?)",
                [(sid, r.reader_id, r.model, r.status.value, _record(r))
                 for r in snapshot.readers],
            )
            tx.executemany(
                "INSERT INTO storage_devices VALUES (?, ?, ?, ?, ?, ?)",
                [(sid, s.storage_id, s.reader_id, s.status.value, s.capacity_bytes, _record(s))
                 for s in snapshot.storage_devices],
            )
            tx.executemany(
                "INSERT INTO volumes VALUES (?, ?, ?, ?, ?, ?)",
                [(sid, v.volume_id, v.storage_id, v.mount_point, v.access_status.value, _record(v))
                 for v in snapshot.volumes],
            )

    def latest(self) -> InventorySnapshot | None:
        row = self._db.query_one(
            "SELECT * FROM device_snapshots ORDER BY captured_at DESC, rowid DESC LIMIT 1"
        )
        if row is None:
            return None
        sid = row["snapshot_id"]

        def load(table: str) -> list[dict]:
            rows = self._db.query(f"SELECT record_json FROM {table} WHERE snapshot_id = ?", (sid,))
            return [json.loads(item["record_json"]) for item in rows]

        return InventorySnapshot(
            snapshot_id=sid,
            captured_at=row["captured_at"],
            platform=row["platform"],
            hostname=row["hostname"],
            usb_devices=[_usb(item) for item in load("usb_devices")],
            readers=[_reader(item) for item in load("readers")],
            storage_devices=[_storage(item) for item in load("storage_devices")],
            volumes=[_volume(item) for item in load("volumes")],
            issues=[DetectionIssue(**item) for item in json.loads(row["issues_json"])],
        )


def _usb(data: dict) -> UsbDevice:
    data["speed_confidence"] = SpeedConfidence(data["speed_confidence"])
    return UsbDevice(**data)


def _reader(data: dict) -> Reader:
    data["status"] = DeviceStatus(data["status"])
    data["kind"] = ReaderKind(data["kind"])
    return Reader(**data)


def _storage(data: dict) -> StorageDevice:
    data["status"] = DeviceStatus(data["status"])
    return StorageDevice(**data)


def _volume(data: dict) -> Volume:
    data["access_status"] = AccessStatus(data["access_status"])
    return Volume(**data)


class ScanRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def start(self, summary: ScanSummary, session_id: str | None) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO scans (scan_id, session_id, card_id, mount_point, started_at, status)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (summary.scan_id, session_id, summary.card_id, summary.mount_point, utc_now(),
                 summary.status),
            )

    def add_files(self, scan_id: str, rows: Iterable[tuple]) -> None:
        with self._db.transaction() as tx:
            tx.executemany(
                "INSERT INTO files (scan_id, relative_path, file_name, extension, size_bytes,"
                " mtime_ns, readable, sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(scan_id, *row) for row in rows],
            )

    def add_errors(self, scan_id: str, rows: Iterable[tuple[str, str, str]]) -> None:
        with self._db.transaction() as tx:
            tx.executemany(
                "INSERT INTO scan_errors VALUES (?, ?, ?, ?)",
                [(scan_id, *row) for row in rows],
            )

    def finish(self, summary: ScanSummary) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "UPDATE scans SET finished_at=?, status=?, file_count=?, total_bytes=?,"
                " dir_count=?, unreadable_count=?, excluded_count=?, error_count=?,"
                " by_extension_json=? WHERE scan_id=?",
                (utc_now(), summary.status, summary.file_count, summary.total_bytes,
                 summary.dir_count, summary.unreadable_count, summary.excluded_count,
                 summary.error_count, json.dumps(summary.by_extension, ensure_ascii=False),
                 summary.scan_id),
            )

    def for_session(self, session_id: str) -> list[dict]:
        rows = self._db.query("SELECT * FROM scans WHERE session_id=? ORDER BY card_id",
                              (session_id,))
        return [dict(row) for row in rows]

    def errors_for_session(self, session_id: str) -> Iterator[dict]:
        rows = self._db.iterate(
            "SELECT e.rowid AS rowid, s.card_id, e.path, e.kind, e.message FROM scan_errors e"
            " JOIN scans s ON s.scan_id = e.scan_id WHERE s.session_id = ?",
            (session_id,), key="e.rowid",
        )
        return (dict(row) for row in rows)

    def iter_readable_files(self, scan_id: str) -> Iterator[dict]:
        rows = self._db.iterate(
            "SELECT * FROM files WHERE scan_id = ? AND readable = 1", (scan_id,), key="file_id"
        )
        return (dict(row) for row in rows)


@dataclass
class Task:
    task_id: int
    session_id: str
    card_id: str
    relative_path: str
    object_name: str
    size_bytes: int
    mtime_ns: int | None
    status: str
    attempts: int
    local_crc32c: str | None = None
    remote_generation: int | None = None


def _task(row: Any) -> Task:
    return Task(
        task_id=row["task_id"], session_id=row["session_id"], card_id=row["card_id"],
        relative_path=row["relative_path"], object_name=row["object_name"],
        size_bytes=row["size_bytes"], mtime_ns=row["mtime_ns"], status=row["status"],
        attempts=row["attempts"], local_crc32c=row["local_crc32c"],
        remote_generation=row["remote_generation"],
    )


class SessionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- Sessions ---

    def create(self, session_id: str, base_prefix: str, metadata: dict[str, str]) -> None:
        now = utc_now()
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO sessions (session_id, status, created_at, updated_at, base_prefix)"
                " VALUES (?, ?, ?, ?, ?)",
                (session_id, SessionStatus.CREATED.value, now, now, base_prefix),
            )
            tx.executemany(
                "INSERT INTO session_metadata VALUES (?, ?, ?)",
                [(session_id, key, value) for key, value in metadata.items()],
            )

    def get(self, session_id: str) -> dict | None:
        row = self._db.query_one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        return dict(row) if row else None

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM sessions ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]

    def latest_id(self) -> str | None:
        row = self._db.query_one(
            "SELECT session_id FROM sessions ORDER BY created_at DESC, rowid DESC LIMIT 1"
        )
        return row["session_id"] if row else None

    def metadata(self, session_id: str) -> dict[str, str]:
        rows = self._db.query(
            "SELECT key, value FROM session_metadata WHERE session_id=?", (session_id,)
        )
        return {row["key"]: row["value"] for row in rows}

    def set_status(self, session_id: str, status: SessionStatus,
                   error: str | None = None) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "UPDATE sessions SET status=?, updated_at=?, last_error=COALESCE(?, last_error)"
                " WHERE session_id=?",
                (status.value, utc_now(), error, session_id),
            )

    def set_error(self, session_id: str, message: str) -> None:
        with self._db.transaction() as tx:
            tx.execute("UPDATE sessions SET last_error=?, updated_at=? WHERE session_id=?",
                       (message, utc_now(), session_id))

    def incomplete(self) -> list[dict]:
        marks = ",".join("?" for _ in ACTIVE_SESSION_STATES)
        rows = self._db.query(
            f"SELECT * FROM sessions WHERE status IN ({marks}) ORDER BY created_at",
            tuple(state.value for state in ACTIVE_SESSION_STATES),
        )
        return [dict(row) for row in rows]

    def claim(self, session_id: str, pid: int) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "UPDATE sessions SET owner_pid=?, heartbeat_at=?, stop_requested=0"
                " WHERE session_id=?",
                (pid, utc_now(), session_id),
            )

    def heartbeat(self, session_id: str) -> None:
        with self._db.transaction() as tx:
            tx.execute("UPDATE sessions SET heartbeat_at=? WHERE session_id=?",
                       (utc_now(), session_id))

    def release(self, session_id: str, seconds: float) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "UPDATE sessions SET owner_pid=NULL, heartbeat_at=NULL,"
                " upload_seconds=upload_seconds+? WHERE session_id=?",
                (seconds, session_id),
            )

    def request_stop(self, session_id: str) -> None:
        with self._db.transaction() as tx:
            tx.execute("UPDATE sessions SET stop_requested=1 WHERE session_id=?", (session_id,))

    def stop_requested(self, session_id: str) -> bool:
        row = self._db.query_one("SELECT stop_requested FROM sessions WHERE session_id=?",
                                 (session_id,))
        return bool(row and row["stop_requested"])

    # --- Cards ---

    def add_card(self, session_id: str, card_id: str, mount_point: str, fingerprint: str | None,
                 label: str | None, reader_model: str | None, is_folder: bool) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO session_cards VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, card_id, mount_point, fingerprint, label, reader_model,
                 int(is_folder)),
            )

    def cards(self, session_id: str) -> list[dict]:
        rows = self._db.query("SELECT * FROM session_cards WHERE session_id=? ORDER BY card_id",
                              (session_id,))
        return [dict(row) for row in rows]

    # --- Tasks ---

    def add_tasks(self, rows: Iterable[tuple]) -> None:
        now = utc_now()
        with self._db.transaction() as tx:
            tx.executemany(
                "INSERT INTO upload_tasks (session_id, file_id, card_id, relative_path,"
                " object_name, size_bytes, mtime_ns, status, updated_at)"
                f" VALUES (?, ?, ?, ?, ?, ?, ?, '{TaskStatus.PENDING.value}', ?)",
                [(*row, now) for row in rows],
            )

    def next_ready(self, session_id: str, limit: int) -> list[Task]:
        """Claim the next tasks whose backoff has passed, marking them ACTIVE atomically."""
        with self._db.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM upload_tasks WHERE session_id=? AND status=? AND next_attempt_at<=?"
                + _NOT_HELD + " ORDER BY task_id LIMIT ?",
                (session_id, TaskStatus.PENDING.value, time.time(), limit),
            ).fetchall()
            tx.executemany(
                "UPDATE upload_tasks SET status=?, attempts=attempts+1, updated_at=?"
                " WHERE task_id=?",
                [(TaskStatus.ACTIVE.value, utc_now(), row["task_id"]) for row in rows],
            )
        tasks = [_task(row) for row in rows]
        for task in tasks:
            task.attempts += 1
        return tasks

    def update_task(self, task_id: int, status: TaskStatus, **fields: Any) -> None:
        allowed = {"attempts", "next_attempt_at", "local_crc32c", "local_sha256",
                   "remote_generation", "error_class", "last_error", "uploaded_at", "object_name"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown task fields: {sorted(unknown)}")
        columns = ["status=?", "updated_at=?"] + [f"{name}=?" for name in fields]
        with self._db.transaction() as tx:
            tx.execute(
                f"UPDATE upload_tasks SET {', '.join(columns)} WHERE task_id=?",
                (status.value, utc_now(), *fields.values(), task_id),
            )

    def fail_card_pending(self, session_id: str, card_id: str, message: str) -> int:
        with self._db.transaction() as tx:
            cursor = tx.execute(
                "UPDATE upload_tasks SET status=?, error_class='SourceDisconnectedError',"
                " last_error=?, updated_at=? WHERE session_id=? AND card_id=? AND status=?",
                (TaskStatus.FAILED.value, message, utc_now(), session_id, card_id,
                 TaskStatus.PENDING.value),
            )
            return cursor.rowcount

    def tasks_with_status(self, session_id: str, statuses: Iterable[TaskStatus]
                          ) -> Iterator[Task]:
        values = [status.value for status in statuses]
        marks = ",".join("?" for _ in values)
        rows = self._db.iterate(
            f"SELECT * FROM upload_tasks WHERE session_id=? AND status IN ({marks})",
            (session_id, *values), key="task_id",
        )
        return (_task(row) for row in rows)

    def iter_task_rows(self, session_id: str) -> Iterator[dict]:
        rows = self._db.iterate(
            "SELECT * FROM upload_tasks WHERE session_id=?", (session_id,), key="task_id"
        )
        return (dict(row) for row in rows)

    def requeue(self, session_id: str, statuses: Iterable[TaskStatus]) -> int:
        """Return tasks to PENDING with a fresh retry budget. Conflicts are never included."""
        values = [status.value for status in statuses]
        marks = ",".join("?" for _ in values)
        with self._db.transaction() as tx:
            cursor = tx.execute(
                f"UPDATE upload_tasks SET status=?, attempts=0, next_attempt_at=0, updated_at=?"
                f" WHERE session_id=? AND status IN ({marks})",
                (TaskStatus.PENDING.value, utc_now(), session_id, *values),
            )
            return cursor.rowcount

    def mark_interrupted(self, session_id: str) -> int:
        """ACTIVE tasks after a crash: nobody knows if they finished, so they are UNCERTAIN."""
        with self._db.transaction() as tx:
            cursor = tx.execute(
                "UPDATE upload_tasks SET status=?, updated_at=? WHERE session_id=? AND status=?",
                (TaskStatus.UNCERTAIN.value, utc_now(), session_id, TaskStatus.ACTIVE.value),
            )
            return cursor.rowcount

    def status_counts(self, session_id: str) -> dict[str, dict[str, int]]:
        rows = self._db.query(
            "SELECT status, COUNT(*) AS files, COALESCE(SUM(size_bytes), 0) AS bytes"
            " FROM upload_tasks WHERE session_id=? GROUP BY status",
            (session_id,),
        )
        return {row["status"]: {"files": row["files"], "bytes": row["bytes"]} for row in rows}

    def next_delay(self, session_id: str) -> float | None:
        """Seconds until the next backed-off task is ready, or None if nothing is pending."""
        row = self._db.query_one(
            "SELECT MIN(next_attempt_at) AS due FROM upload_tasks WHERE session_id=? AND status=?"
            + _NOT_HELD,
            (session_id, TaskStatus.PENDING.value),
        )
        if row is None or row["due"] is None:
            return None
        return max(0.0, row["due"] - time.time())

    # --- Attempts and verification ---

    def add_attempt(self, task_id: int, started_at: str, seconds: float, bytes_read: int,
                    outcome: str, error_class: str | None, message: str | None) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO upload_attempts (task_id, started_at, duration_seconds, bytes_read,"
                " outcome, error_class, error_message) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, started_at, seconds, bytes_read, outcome, error_class, message),
            )

    def add_verification(self, task_id: int, source_size: int, remote_size: int | None,
                         local_crc32c: str | None, remote_crc32c: str | None, status: str,
                         method: str) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO verification_results (task_id, verified_at, source_size, remote_size,"
                " local_crc32c, remote_crc32c, status, method) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, utc_now(), source_size, remote_size, local_crc32c, remote_crc32c, status,
                 method),
            )

    def attempt_totals(self, session_id: str) -> dict[str, int]:
        row = self._db.query_one(
            "SELECT COUNT(*) AS attempts, COALESCE(SUM(a.bytes_read), 0) AS bytes,"
            " COALESCE(SUM(a.outcome = 'FAILED'), 0) AS failures FROM upload_attempts a"
            " JOIN upload_tasks t ON t.task_id = a.task_id WHERE t.session_id = ?",
            (session_id,),
        )
        return dict(row)
