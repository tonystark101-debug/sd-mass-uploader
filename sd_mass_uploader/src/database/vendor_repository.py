"""The vendor platform packet behind each session (table vendor_packets)."""

from __future__ import annotations

import json

from src.core.timeutil import utc_now
from src.database.db import Database


class VendorPacketRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def save(self, session_id: str, packet: dict, detected: int, counted: int,
             note: str) -> None:
        with self._db.transaction() as tx:
            tx.execute(
                "INSERT INTO vendor_packets (session_id, ref, packet_json, cards_detected,"
                " cards_counted, note) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, packet["ref"], json.dumps(packet, ensure_ascii=False), detected,
                 counted, note))

    def get(self, session_id: str) -> dict | None:
        row = self._db.query_one("SELECT * FROM vendor_packets WHERE session_id=?",
                                 (session_id,))
        if row is None:
            return None
        item = dict(row)
        item["packet"] = json.loads(item.pop("packet_json"))
        item["uploaded"] = json.loads(item.pop("uploaded_json")) if item["uploaded_json"] \
            else None
        return item

    def started(self, session_id: str, error: str | None, next_try: float = 0) -> None:
        with self._db.transaction() as tx:
            tx.execute("UPDATE vendor_packets SET started_at=COALESCE(started_at, ?),"
                       " last_error=?, next_try=? WHERE session_id=?",
                       (None if error else utc_now(), error, next_try, session_id))

    def uploaded(self, session_id: str, report: dict, error: str | None,
                 next_try: float = 0) -> None:
        with self._db.transaction() as tx:
            if error:
                tx.execute("UPDATE vendor_packets SET last_error=?, next_try=? WHERE"
                           " session_id=?", (error, next_try, session_id))
            else:
                tx.execute("UPDATE vendor_packets SET uploaded_at=?, uploaded_json=?,"
                           " last_error=NULL, next_try=0 WHERE session_id=?",
                           (utc_now(), json.dumps(report), session_id))

    def clear_delay(self, session_id: str) -> None:
        with self._db.transaction() as tx:
            tx.execute("UPDATE vendor_packets SET next_try=0 WHERE session_id=?", (session_id,))
