"""SQL for station mode: the open session, per-card controls and the command queue."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from src.core.enums import CardPhase, TaskStatus
from src.core.models import Card
from src.core.timeutil import utc_now
from src.database.db import Database

# Scan error kinds that do not stop a card counting as fully uploaded (see reporting).
NOT_BLOCKING_SCAN_ERRORS = ("LINK_SKIPPED", "NOT_PROJECT_FOOTAGE", "SKIPPED_BY_OPERATOR")
LOST_ERROR_CLASS = "SourceDisconnectedError"


@dataclass
class CardCounts:
    """Files and bytes per task status for one card, plus files lost to a card removal."""

    files: dict[str, int] = field(default_factory=dict)
    bytes: dict[str, int] = field(default_factory=dict)
    lost_files: int = 0
    blocking_scan_errors: int = 0

    def total_files(self) -> int:
        return sum(self.files.values())

    def total_bytes(self) -> int:
        return sum(self.bytes.values())

    def get(self, status: TaskStatus) -> int:
        return self.files.get(status.value, 0)


def card_to_json(card: Card) -> str:
    return json.dumps(asdict(card), sort_keys=True)


def card_from_json(text: str) -> Card:
    return Card(**json.loads(text))


class StationRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- Station session ---

    def open_session(self, session_id: str, auto_upload: bool) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO station_sessions (session_id, opened_at, auto_upload)"
                " VALUES (?, ?, ?)",
                (session_id, utc_now(), int(auto_upload)),
            )

    def current(self) -> dict | None:
        """The open station session, if any."""
        row = self._db.query_one(
            "SELECT * FROM station_sessions WHERE closed_at IS NULL ORDER BY opened_at DESC LIMIT 1"
        )
        return dict(row) if row else None

    def close_session(self, session_id: str) -> None:
        with self._db.transaction() as tx:
            tx.execute("UPDATE station_sessions SET closed_at=? WHERE session_id=?",
                       (utc_now(), session_id))

    def set_paused_all(self, session_id: str, paused: bool) -> None:
        with self._db.transaction() as tx:
            tx.execute("UPDATE station_sessions SET paused_all=? WHERE session_id=?",
                       (int(paused), session_id))
            tx.execute("UPDATE station_cards SET paused=?, updated_at=? WHERE session_id=?",
                       (int(paused), utc_now(), session_id))

    def set_auto_upload(self, session_id: str, enabled: bool) -> None:
        with self._db.transaction() as tx:
            tx.execute("UPDATE station_sessions SET auto_upload=? WHERE session_id=?",
                       (int(enabled), session_id))

    # --- Cards ---

    def add_card(self, session_id: str, card: Card, phase: CardPhase, paused: bool,
                 reason: str | None = None) -> None:
        now = utc_now()
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO station_cards (session_id, card_id, phase, present, paused, reason,"
                " card_json, inserted_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
                (session_id, card.card_id, phase.value, int(paused), reason, card_to_json(card),
                 now, now),
            )

    def card(self, session_id: str, card_id: str) -> dict | None:
        row = self._db.query_one(
            "SELECT * FROM station_cards WHERE session_id=? AND card_id=?", (session_id, card_id)
        )
        return dict(row) if row else None

    def cards(self, session_id: str) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM station_cards WHERE session_id=? ORDER BY inserted_at, card_id",
            (session_id,),
        )
        return [dict(row) for row in rows]

    def _update(self, session_id: str, card_id: str, **values: Any) -> None:
        allowed = {"phase", "present", "paused", "reason", "card_json"}
        if set(values) - allowed:
            raise ValueError(f"Unknown station card fields: {sorted(set(values) - allowed)}")
        columns = ", ".join(f"{name}=?" for name in values)
        with self._db.transaction() as tx:
            tx.execute(
                f"UPDATE station_cards SET {columns}, updated_at=?"
                " WHERE session_id=? AND card_id=?",
                (*values.values(), utc_now(), session_id, card_id),
            )

    def set_phase(self, session_id: str, card_id: str, phase: CardPhase,
                  reason: str | None = None) -> None:
        self._update(session_id, card_id, phase=phase.value, reason=reason)

    def set_present(self, session_id: str, card_id: str, present: bool,
                    card: Card | None = None) -> None:
        values: dict[str, Any] = {"present": int(present)}
        if card is not None:
            values["card_json"] = card_to_json(card)
        self._update(session_id, card_id, **values)

    def set_paused(self, session_id: str, card_id: str, paused: bool) -> None:
        self._update(session_id, card_id, paused=int(paused))

    def card_counts(self, session_id: str) -> dict[str, CardCounts]:
        counts: dict[str, CardCounts] = {}
        rows = self._db.query(
            "SELECT card_id, status, COUNT(*) AS files, COALESCE(SUM(size_bytes), 0) AS bytes,"
            " SUM(status=? AND error_class=?) AS lost FROM upload_tasks WHERE session_id=?"
            " GROUP BY card_id, status",
            (TaskStatus.FAILED.value, LOST_ERROR_CLASS, session_id),
        )
        for row in rows:
            item = counts.setdefault(row["card_id"], CardCounts())
            item.files[row["status"]] = row["files"]
            item.bytes[row["status"]] = row["bytes"]
            item.lost_files += row["lost"] or 0
        marks = ",".join("?" for _ in NOT_BLOCKING_SCAN_ERRORS)
        rows = self._db.query(
            "SELECT s.card_id, COUNT(*) AS errors FROM scan_errors e JOIN scans s"
            f" ON s.scan_id = e.scan_id WHERE s.session_id=? AND e.kind NOT IN ({marks})"
            " GROUP BY s.card_id",
            (session_id, *NOT_BLOCKING_SCAN_ERRORS),
        )
        for row in rows:
            counts.setdefault(row["card_id"], CardCounts()).blocking_scan_errors = row["errors"]
        return counts

    def requeue_lost(self, session_id: str, card_id: str) -> int:
        """Files that failed only because the card was taken out go back in the queue."""
        with self._db.transaction() as tx:
            cursor = tx.execute(
                "UPDATE upload_tasks SET status=?, attempts=0, next_attempt_at=0, updated_at=?"
                " WHERE session_id=? AND card_id=? AND status=? AND error_class=?",
                (TaskStatus.PENDING.value, utc_now(), session_id, card_id,
                 TaskStatus.FAILED.value, LOST_ERROR_CLASS),
            )
            return cursor.rowcount

    def requeue_card_failed(self, session_id: str, card_id: str) -> int:
        """Operator retry for one card: FAILED files only. Conflicts need a separate decision."""
        with self._db.transaction() as tx:
            cursor = tx.execute(
                "UPDATE upload_tasks SET status=?, attempts=0, next_attempt_at=0, updated_at=?"
                " WHERE session_id=? AND card_id=? AND status=?",
                (TaskStatus.PENDING.value, utc_now(), session_id, card_id,
                 TaskStatus.FAILED.value),
            )
            return cursor.rowcount

    def card_files(self, session_id: str, card_id: str, status: str | None = None,
                   limit: int = 200, offset: int = 0) -> list[dict]:
        """A page of one card's files with their upload state, problems first."""
        where, params = "session_id=? AND card_id=?", [session_id, card_id]
        if status:
            where += " AND status=?"
            params.append(status)
        rows = self._db.query(
            "SELECT task_id, relative_path, object_name, size_bytes, status, attempts,"
            " error_class, last_error, uploaded_at FROM upload_tasks"
            f" WHERE {where} ORDER BY CASE status WHEN 'VERIFIED' THEN 1 ELSE 0 END, task_id"
            " LIMIT ? OFFSET ?",
            (*params, max(1, min(limit, 1000)), max(0, offset)),
        )
        return [dict(row) for row in rows]

    def error_groups(self, session_id: str) -> list[dict]:
        """Files needing attention, grouped by card, status and error, with one example."""
        rows = self._db.query(
            "SELECT card_id, status, error_class, COUNT(*) AS files, MAX(attempts) AS attempts,"
            " MIN(relative_path || ': ' || COALESCE(last_error, '')) AS sample"
            " FROM upload_tasks WHERE session_id=? AND (status IN (?, ?, ?, ?)"
            " OR (status=? AND error_class=? AND attempts>0))"
            " GROUP BY card_id, status, error_class",
            (session_id, TaskStatus.FAILED.value, TaskStatus.FAILED_PERMANENT.value,
             TaskStatus.CONFLICT.value, TaskStatus.UNCERTAIN.value, TaskStatus.PENDING.value,
             "TransientUploadError"),
        )
        return [dict(row) for row in rows]

    def scan_error_groups(self, session_id: str) -> list[dict]:
        rows = self._db.query(
            "SELECT s.card_id, e.kind, COUNT(*) AS count, MIN(e.path) AS sample"
            " FROM scan_errors e JOIN scans s ON s.scan_id = e.scan_id"
            " WHERE s.session_id=? GROUP BY s.card_id, e.kind",
            (session_id,),
        )
        return [dict(row) for row in rows]

    # --- Problem history

    def open_problems(self) -> list[dict]:
        rows = self._db.query("SELECT * FROM station_problems WHERE resolved_at IS NULL")
        return [dict(row) for row in rows]

    def open_problem(self, session_id: str, problem) -> dict:
        now = utc_now()
        with self._db.transaction() as tx:
            cursor = tx.execute(
                "INSERT INTO station_problems (problem_id, session_id, code, severity, card_id,"
                " title, message, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (problem.id, session_id, problem.code, problem.severity, problem.card_id,
                 problem.title, problem.message[:2000], now),
            )
        return {"row_id": cursor.lastrowid, "problem_id": problem.id, "code": problem.code,
                "card_id": problem.card_id, "opened_at": now}

    def resolve_problem(self, row_id: int) -> None:
        with self._db.transaction() as tx:
            tx.execute("UPDATE station_problems SET resolved_at=? WHERE row_id=?",
                       (utc_now(), row_id))

    def problem_history(self, limit: int = 100) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM station_problems ORDER BY row_id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]

    def card_scan_errors(self, session_id: str, card_id: str, limit: int = 200) -> list[dict]:
        rows = self._db.query(
            "SELECT e.path, e.kind, e.message FROM scan_errors e JOIN scans s"
            " ON s.scan_id = e.scan_id WHERE s.session_id=? AND s.card_id=? LIMIT ?",
            (session_id, card_id, limit),
        )
        return [dict(row) for row in rows]

    def discard_scan(self, scan_id: str) -> None:
        """Forget a scan that was cut short. Safe only before any task was queued from it."""
        with self._db.transaction() as tx:
            used = tx.execute(
                "SELECT 1 FROM upload_tasks t JOIN files f ON f.file_id = t.file_id"
                " WHERE f.scan_id=? LIMIT 1", (scan_id,),
            ).fetchone()
            if used:
                raise ValueError(f"Scan {scan_id} already has queued files")
            tx.execute("DELETE FROM scans WHERE scan_id=?", (scan_id,))

    # --- Command queue ---

    def enqueue(self, command: str, args: dict | None = None) -> int:
        with self._db.transaction() as tx:
            cursor = tx.execute(
                "INSERT INTO station_commands (created_at, command, args_json) VALUES (?, ?, ?)",
                (utc_now(), command, json.dumps(args or {}, sort_keys=True)),
            )
            return int(cursor.lastrowid)

    def take_commands(self, limit: int = 20) -> list[dict]:
        """Claim waiting commands in order. Each is handed out once."""
        with self._db.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM station_commands WHERE taken_at IS NULL ORDER BY command_id LIMIT ?",
                (limit,),
            ).fetchall()
            now = utc_now()
            tx.executemany("UPDATE station_commands SET taken_at=? WHERE command_id=?",
                           [(now, row["command_id"]) for row in rows])
        return [{**dict(row), "args": json.loads(row["args_json"])} for row in rows]

    def finish_command(self, command_id: int, result: str) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "UPDATE station_commands SET finished_at=?, result=? WHERE command_id=?",
                (utc_now(), result[:2000], command_id),
            )

    def command(self, command_id: int) -> dict | None:
        row = self._db.query_one("SELECT * FROM station_commands WHERE command_id=?",
                                 (command_id,))
        return dict(row) if row else None
