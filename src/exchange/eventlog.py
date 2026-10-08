"""Append-only event log on SQLite. The log is the source of truth; the
engine's in-memory state is a deterministic fold over it.

Exactly one process may hold a log open: the connection keeps SQLite's
exclusive file lock for its lifetime, so a competing engine fails at startup
rather than silently forking the market."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterator
from uuid import uuid4


class EventType(StrEnum):
    ORDER_SUBMITTED = "order_submitted"
    ORDER_CANCELLED = "order_cancelled"


@dataclass(frozen=True)
class Event:
    seq: int
    type_: EventType
    data: dict[str, Any]


class EventLog:
    def __init__(self, path: str) -> None:
        # check_same_thread=False: the app serializes all access on one event
        # loop, but test clients drive that loop from a worker thread.
        # timeout=1.0: the only lock contention possible is a second engine
        # opening the same log, and that should fail fast, not wait out the
        # default five seconds.
        self._conn = sqlite3.connect(path, timeout=1.0, check_same_thread=False)
        try:
            # Once this connection writes, it keeps the file lock until it
            # closes: the one-writer-per-log invariant, enforced by the OS.
            self._conn.execute("PRAGMA locking_mode=EXCLUSIVE")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " type TEXT NOT NULL,"
                " data TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                " key TEXT PRIMARY KEY,"
                " value TEXT NOT NULL)"
            )
            # A write statement even when the row already exists, so it pins
            # the exclusive lock at startup: a competing engine fails here,
            # not on its first order.
            self._conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('instance_id', ?)",
                (str(uuid4()),),
            )
            self._conn.commit()
            (self.instance_id,) = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'instance_id'"
            ).fetchone()
        except Exception:
            self._conn.close()
            raise

    def append(self, type_: EventType, data: dict[str, Any]) -> Event:
        cursor = self._conn.execute(
            "INSERT INTO events (type, data) VALUES (?, ?)", (type_, json.dumps(data))
        )
        self._conn.commit()
        return Event(
            seq=cursor.lastrowid if cursor.lastrowid is not None else 0,
            type_=type_, data=data
        )

    def replay(self) -> Iterator[Event]:
        rows = self._conn.execute("SELECT id, type, data FROM events ORDER BY id")
        for seq, type_, data in rows:
            yield Event(seq=seq, type_=EventType(type_), data=json.loads(data))

    def close(self) -> None:
        self._conn.close()
