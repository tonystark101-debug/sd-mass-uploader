"""SQL for the AI troubleshooter's audit log."""

from __future__ import annotations

from src.core.timeutil import utc_now
from src.database.db import Database


class AiAuditRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, ai_session_id: str, kind: str, tool: str | None = None,
            detail: str | None = None, verdict: str | None = None,
            decided_by: str | None = None) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO ai_audit (ai_session_id, at, kind, tool, detail, verdict, decided_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ai_session_id, utc_now(), kind, tool, (detail or "")[:8000], verdict,
                 decided_by),
            )

    def for_session(self, ai_session_id: str) -> list[dict]:
        rows = self._db.query("SELECT * FROM ai_audit WHERE ai_session_id=? ORDER BY row_id",
                              (ai_session_id,))
        return [dict(row) for row in rows]
