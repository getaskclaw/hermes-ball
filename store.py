"""SQLite store: one row per chat thread. No transcripts — only ball/since/promise/note."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

SCHEMA = """CREATE TABLE IF NOT EXISTS threads (
    key TEXT PRIMARY KEY,
    ball TEXT NOT NULL CHECK (ball IN ('ai','human')),
    since TEXT NOT NULL,
    promise TEXT,
    note TEXT NOT NULL DEFAULT '',
    updated TEXT NOT NULL,
    label TEXT
)"""
COLS = ("key", "ball", "since", "promise", "note", "updated", "label")
NOTE_MAX = 200


def now_iso(precise: bool = False) -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds" if precise else "seconds")


def _default_connect() -> sqlite3.Connection:
    from plugins.plugin_storage import plugin_db  # Hermes host: <home>/plugin-data/ball/data.db (WAL)
    return plugin_db("ball")


class Store:
    def __init__(self, connect: Optional[Callable[[], sqlite3.Connection]] = None):
        self._connect = connect or _default_connect
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            conn.execute(SCHEMA)
            have = {c[1] for c in conn.execute("PRAGMA table_info(threads)")}
            if "label" not in have:  # migrate pre-label DBs in place; never drops data
                conn.execute("ALTER TABLE threads ADD COLUMN label TEXT")
            conn.commit()
            self._conn = conn
        return self._conn

    def set(self, key: str, ball: str, *, promise: Optional[str] = None, note: str = "",
            since: Optional[str] = None, label: Optional[str] = None) -> dict:
        """Upsert. ``since`` is kept when the ball does not change hands (unless given).
        ``label`` (human-readable thread name) is kept when not given."""
        if ball not in ("ai", "human"):
            raise ValueError(f"bad ball {ball!r}")
        note = (note or "").replace("\n", " ").strip()[:NOTE_MAX]
        promise = promise[:NOTE_MAX] if promise else None
        label = (label or "").replace("\n", " ").strip()[:NOTE_MAX] or None
        ts = now_iso(precise=True)
        with self._lock:
            db = self._db()
            cur = self.get(key)
            if since is None:
                since = cur["since"] if cur and cur["ball"] == ball else now_iso()
            if label is None and cur:
                label = cur.get("label")
            db.execute(
                "INSERT INTO threads(key,ball,since,promise,note,updated,label) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET ball=excluded.ball, since=excluded.since, "
                "promise=excluded.promise, note=excluded.note, updated=excluded.updated, label=excluded.label",
                (key, ball, since, promise, note, ts, label))
            db.commit()
            return {"key": key, "ball": ball, "since": since, "promise": promise, "note": note, "updated": ts,
                    "label": label}

    def set_if(self, key: str, expected_updated: str, ball: str, **kw) -> Optional[dict]:
        """Atomic compare-and-set: write only if the row's ``updated`` still equals the stamp."""
        with self._lock:
            cur = self.get(key)
            if not cur or cur["updated"] != expected_updated:
                return None
            return self.set(key, ball, **kw)

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            row = self._db().execute(
                "SELECT key,ball,since,promise,note,updated,label FROM threads WHERE key=?", (key,)).fetchone()
        return _row(row) if row else None

    def all(self) -> list[dict]:
        with self._lock:
            rows = self._db().execute("SELECT key,ball,since,promise,note,updated,label FROM threads").fetchall()
        return [_row(r) for r in rows]

    def delete(self, key: str) -> bool:
        with self._lock:
            cur = self._db().execute("DELETE FROM threads WHERE key=?", (key,))
            self._db().commit()
            return cur.rowcount > 0


def _row(r) -> dict:
    return dict(zip(COLS, tuple(r)))
