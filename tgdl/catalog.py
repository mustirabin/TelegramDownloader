"""SQLite catalog of every detected file: download queue state + library index."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

ACTIVE = ("queued", "downloading")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_chat TEXT NOT NULL,          -- chat holding the file: channel id or username or user id
    file_msg_id INTEGER NOT NULL,
    via_chat_id INTEGER,              -- private chat where we saw the message / link
    via_msg_id INTEGER,
    via_sender TEXT,
    link_url TEXT,
    document_id INTEGER,
    original_name TEXT,
    caption TEXT,
    mime TEXT,
    size INTEGER,
    kind TEXT,                        -- episode | movie | unsorted
    title TEXT,
    season INTEGER,
    episode INTEGER,
    year INTEGER,
    rel_path TEXT,                    -- path inside the library once done
    status TEXT NOT NULL DEFAULT 'queued',  -- queued|downloading|done|failed|cancelled|skipped
    error TEXT,
    bytes_done INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    UNIQUE(file_chat, file_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_doc ON items(document_id);
"""

COLUMNS = (
    "file_chat", "file_msg_id", "via_chat_id", "via_msg_id", "via_sender", "link_url", "document_id",
    "original_name", "caption", "mime", "size", "kind", "title", "season", "episode", "year", "rel_path",
    "status", "error", "bytes_done", "attempts", "completed_at",
)


class Catalog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def add(self, file_chat, file_msg_id: int, **fields) -> tuple[dict, bool]:
        """Insert a new item; returns (item, created). Existing (chat,msg) pairs are returned untouched."""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM items WHERE file_chat=? AND file_msg_id=?", (str(file_chat), file_msg_id)
            ).fetchone()
            if row:
                return dict(row), False
            now = time.time()
            data = {k: v for k, v in fields.items() if k in COLUMNS}
            cols = ["file_chat", "file_msg_id", "created_at", "updated_at", *data]
            vals = [str(file_chat), file_msg_id, now, now, *data.values()]
            cur = self.db.execute(
                f"INSERT INTO items ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals
            )
            self.db.commit()
            return self.get(cur.lastrowid), True

    def get(self, item_id: int) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            return dict(row) if row else None

    def update(self, item_id: int, **fields) -> dict | None:
        data = {k: v for k, v in fields.items() if k in COLUMNS}
        if not data:
            return self.get(item_id)
        with self._lock:
            sets = ",".join(f"{k}=?" for k in data) + ",updated_at=?"
            self.db.execute(f"UPDATE items SET {sets} WHERE id=?", (*data.values(), time.time(), item_id))
            self.db.commit()
        return self.get(item_id)

    def delete(self, item_id: int) -> None:
        with self._lock:
            self.db.execute("DELETE FROM items WHERE id=?", (item_id,))
            self.db.commit()

    def find_done_document(self, document_id: int, exclude_id: int) -> dict | None:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM items WHERE document_id=? AND status='done' AND id<>? LIMIT 1",
                (document_id, exclude_id),
            ).fetchone()
            return dict(row) if row else None

    def list(self, statuses: tuple[str, ...] | None = None, limit: int = 500) -> list[dict]:
        with self._lock:
            if statuses:
                q = f"SELECT * FROM items WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY id DESC LIMIT ?"
                rows = self.db.execute(q, (*statuses, limit)).fetchall()
            else:
                rows = self.db.execute("SELECT * FROM items ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def pending_ids(self) -> list[int]:
        with self._lock:
            rows = self.db.execute(
                f"SELECT id FROM items WHERE status IN ({','.join('?' * len(ACTIVE))}) ORDER BY id", ACTIVE
            ).fetchall()
            return [r["id"] for r in rows]

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.db.execute("SELECT status, COUNT(*) n FROM items GROUP BY status").fetchall()
            return {r["status"]: r["n"] for r in rows}
