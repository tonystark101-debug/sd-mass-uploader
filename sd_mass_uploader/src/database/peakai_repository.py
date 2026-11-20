"""SQL for PeakAI card placement, _info files and dashboard reports."""

from __future__ import annotations

import json

from src.core.enums import TaskStatus
from src.core.timeutil import utc_now
from src.database.db import Database


class PeakAiRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- Card placement

    def save_card(self, session_id: str, card_id: str, serial: str | None, plan: dict,
                  held_reason: str | None) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO peakai_cards (session_id, card_id, serial, plan_json, held_reason,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (session_id, card_id) DO"
                " UPDATE SET serial=excluded.serial, plan_json=excluded.plan_json,"
                " held_reason=excluded.held_reason, updated_at=excluded.updated_at",
                (session_id, card_id, serial, json.dumps(plan, ensure_ascii=False),
                 held_reason, utc_now()),
            )

    def cards(self, session_id: str) -> list[dict]:
        rows = self._db.query("SELECT * FROM peakai_cards WHERE session_id=? ORDER BY card_id",
                              (session_id,))
        return [{**dict(row), "plan": json.loads(row["plan_json"])} for row in rows]

    def card(self, session_id: str, card_id: str) -> dict | None:
        row = self._db.query_one(
            "SELECT * FROM peakai_cards WHERE session_id=? AND card_id=?", (session_id, card_id))
        return {**dict(row), "plan": json.loads(row["plan_json"])} if row else None

    def folder_counts(self, session_id: str, card_id: str, folder: str) -> dict[str, int]:
        """Files of one card part (all objects under its folder) by status."""
        rows = self._db.query(
            "SELECT status, COUNT(*) AS files FROM upload_tasks WHERE session_id=? AND card_id=?"
            " AND substr(object_name, 1, ?)=? GROUP BY status",
            (session_id, card_id, len(folder), folder),
        )
        return {row["status"]: row["files"] for row in rows}

    # --- _info files

    def info(self, session_id: str, card_id: str, pass_name: str) -> dict | None:
        row = self._db.query_one(
            "SELECT * FROM peakai_info WHERE session_id=? AND card_id=? AND pass_name=?",
            (session_id, card_id, pass_name))
        return dict(row) if row else None

    def infos(self, session_id: str) -> list[dict]:
        rows = self._db.query("SELECT * FROM peakai_info WHERE session_id=?", (session_id,))
        return [dict(row) for row in rows]

    def record_info(self, session_id: str, card_id: str, pass_name: str,
                    object_name: str | None, error: str | None, next_try: float = 0) -> None:
        written = utc_now() if object_name and not error else None
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO peakai_info (session_id, card_id, pass_name, object_name, written_at,"
                " last_error, next_try) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (session_id, card_id, pass_name) DO UPDATE SET"
                " object_name=COALESCE(excluded.object_name, object_name),"
                " written_at=COALESCE(excluded.written_at, written_at),"
                " last_error=excluded.last_error, next_try=excluded.next_try",
                (session_id, card_id, pass_name, object_name, written, error, next_try),
            )

    # --- Dashboard reports

    def report(self, session_id: str, pass_name: str) -> dict | None:
        row = self._db.query_one(
            "SELECT * FROM peakai_reports WHERE session_id=? AND pass_name=?",
            (session_id, pass_name))
        return dict(row) if row else None

    def reports(self, session_id: str) -> list[dict]:
        rows = self._db.query("SELECT * FROM peakai_reports WHERE session_id=? ORDER BY"
                              " pass_name", (session_id,))
        return [dict(row) for row in rows]

    def record_report(self, session_id: str, pass_name: str, status: str | None,
                      payload: dict, response: str | None, error: str | None,
                      next_try: float = 0) -> None:
        sent = utc_now() if error is None else None
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO peakai_reports (session_id, pass_name, status, payload_json,"
                " response_text, sent_at, last_error, next_try) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (session_id, pass_name) DO UPDATE SET"
                " status=COALESCE(excluded.status, status),"
                " payload_json=CASE WHEN excluded.last_error IS NULL THEN excluded.payload_json"
                " ELSE payload_json END,"
                " response_text=COALESCE(excluded.response_text, response_text),"
                " sent_at=COALESCE(excluded.sent_at, sent_at),"
                " last_error=excluded.last_error, next_try=excluded.next_try",
                (session_id, pass_name, status, json.dumps(payload, sort_keys=True),
                 response, sent, error, next_try),
            )

    def clear_retry_delays(self, session_id: str) -> None:
        with self._db.transaction() as tx:
            tx.execute("UPDATE peakai_reports SET next_try=0 WHERE session_id=?", (session_id,))
            tx.execute("UPDATE peakai_info SET next_try=0 WHERE session_id=?", (session_id,))

    def verified_status(self) -> str:
        return TaskStatus.VERIFIED.value
