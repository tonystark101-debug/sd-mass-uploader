"""SQLite connection. One shared connection, serialized by a lock, in WAL mode."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from src.core.exceptions import DatabaseError

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        try:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
            self._connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        except (sqlite3.Error, OSError) as exc:
            raise DatabaseError(f"Could not open database {self.path}: {exc}") from exc

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on any error."""
        with self._lock:
            try:
                with self._connection:
                    yield self._connection
            except sqlite3.Error as exc:
                raise DatabaseError(str(exc)) from exc

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            try:
                return self._connection.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise DatabaseError(str(exc)) from exc

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def iterate(self, sql: str, params: tuple = (), page_size: int = 1000,
                key: str = "rowid") -> Iterator[sqlite3.Row]:
        """Stream a large result in keyset pages, so reports use bounded memory."""
        last = -1
        while True:
            rows = self.query(f"{sql} AND {key} > ? ORDER BY {key} LIMIT ?",
                              (*params, last, page_size))
            if not rows:
                return
            yield from rows
            last = rows[-1][key.rsplit(".", 1)[-1]]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
