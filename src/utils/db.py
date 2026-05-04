"""SQLite tracker for processed videos and pipeline state.

Tracks:
- Every reel we've discovered/downloaded (prevents duplicates)
- Upload status and YouTube video IDs
- Creator blocklist (opted-out creators)
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = _PROJECT_ROOT / "data" / "tracker.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type     TEXT NOT NULL,          -- 'instagram' | 'pexels' | 'pixabay'
    source_url      TEXT NOT NULL UNIQUE,
    creator_handle  TEXT,
    creator_url     TEXT,
    caption         TEXT,
    hashtags        TEXT,
    duration_sec    REAL,
    file_hash       TEXT,                   -- sha256 of downloaded file
    local_path      TEXT,
    niche           TEXT,
    discovered_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- pipeline state
    analysis_json   TEXT,
    script_text     TEXT,
    voiceover_path  TEXT,
    edited_path     TEXT,
    youtube_id      TEXT,
    youtube_url     TEXT,
    status          TEXT DEFAULT 'discovered',
                    -- discovered | downloaded | analyzed | scripted
                    -- | voiced | edited | uploaded | failed | skipped
    error           TEXT,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_hash   ON videos(file_hash);

CREATE TABLE IF NOT EXISTS blocklist (
    creator_handle  TEXT PRIMARY KEY,
    reason          TEXT,
    added_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS upload_schedule (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        INTEGER NOT NULL REFERENCES videos(id),
    scheduled_for   TIMESTAMP NOT NULL,
    posted          INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv_state (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class Tracker:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------------- videos ----------------

    def has_url(self, source_url: str) -> bool:
        with self.connect() as c:
            row = c.execute(
                "SELECT 1 FROM videos WHERE source_url = ?", (source_url,)
            ).fetchone()
            return row is not None

    def has_hash(self, file_hash: str) -> bool:
        with self.connect() as c:
            row = c.execute(
                "SELECT 1 FROM videos WHERE file_hash = ?", (file_hash,)
            ).fetchone()
            return row is not None

    def insert_discovery(
        self,
        source_type: str,
        source_url: str,
        creator_handle: Optional[str],
        creator_url: Optional[str],
        caption: Optional[str],
        hashtags: Optional[str],
        niche: Optional[str],
    ) -> int:
        with self.connect() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO videos
                   (source_type, source_url, creator_handle, creator_url,
                    caption, hashtags, niche, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered')""",
                (source_type, source_url, creator_handle, creator_url,
                 caption, hashtags, niche),
            )
            if cur.lastrowid:
                return cur.lastrowid
            row = c.execute(
                "SELECT id FROM videos WHERE source_url = ?", (source_url,)
            ).fetchone()
            return int(row["id"])

    def update(self, video_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = datetime.utcnow().isoformat()
        keys = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [video_id]
        with self.connect() as c:
            c.execute(f"UPDATE videos SET {keys} WHERE id = ?", values)

    def get(self, video_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM videos WHERE id = ?", (video_id,)
            ).fetchone()

    def by_status(self, status: str, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as c:
            return list(
                c.execute(
                    "SELECT * FROM videos WHERE status = ? ORDER BY id ASC LIMIT ?",
                    (status, limit),
                )
            )

    # ---------------- blocklist ----------------

    def is_blocked(self, creator_handle: str) -> bool:
        if not creator_handle:
            return False
        with self.connect() as c:
            row = c.execute(
                "SELECT 1 FROM blocklist WHERE creator_handle = ?",
                (creator_handle.lower(),),
            ).fetchone()
            return row is not None

    def block_creator(self, creator_handle: str, reason: str = "") -> None:
        with self.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO blocklist (creator_handle, reason) VALUES (?, ?)",
                (creator_handle.lower(), reason),
            )

    # ---------------- schedule ----------------

    def add_schedule(self, video_id: int, when: datetime) -> None:
        with self.connect() as c:
            c.execute(
                "INSERT INTO upload_schedule (video_id, scheduled_for) VALUES (?, ?)",
                (video_id, when.isoformat()),
            )

    def due_uploads(self, now: datetime) -> list[sqlite3.Row]:
        with self.connect() as c:
            return list(
                c.execute(
                    """SELECT s.id AS schedule_id, v.*
                       FROM upload_schedule s
                       JOIN videos v ON v.id = s.video_id
                       WHERE s.posted = 0 AND s.scheduled_for <= ?
                       ORDER BY s.scheduled_for ASC""",
                    (now.isoformat(),),
                )
            )

    def mark_posted(self, schedule_id: int) -> None:
        with self.connect() as c:
            c.execute(
                "UPDATE upload_schedule SET posted = 1 WHERE id = ?",
                (schedule_id,),
            )

    # ---------------- key-value state ----------------

    def kv_get(self, key: str) -> Optional[str]:
        with self.connect() as c:
            row = c.execute(
                "SELECT value FROM kv_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self.connect() as c:
            c.execute(
                "INSERT INTO kv_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, value, datetime.utcnow().isoformat()),
            )
