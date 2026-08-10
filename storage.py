"""SQLite persistence for cached messages and their edit history."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id         INTEGER NOT NULL,
    msg_id          INTEGER NOT NULL,
    is_channel      INTEGER NOT NULL DEFAULT 0,
    chat_title      TEXT,
    sender_id       INTEGER,
    sender_name     TEXT,
    text            TEXT,
    media_path      TEXT,
    media_type      TEXT,
    date            INTEGER NOT NULL,
    deleted         INTEGER NOT NULL DEFAULT 0,
    media_size      INTEGER,
    reply_to_msg_id INTEGER,
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_id ON messages(msg_id);
CREATE INDEX IF NOT EXISTS idx_date   ON messages(date);

CREATE TABLE IF NOT EXISTS edits (
    chat_id  INTEGER,
    msg_id   INTEGER,
    old_text TEXT,
    new_text TEXT,
    date     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_edits_msg ON edits(chat_id, msg_id);

CREATE TABLE IF NOT EXISTS watch_chats (
    chat_id  INTEGER PRIMARY KEY,
    label    TEXT,
    added_at INTEGER NOT NULL
);
"""

_COLUMNS = (
    "chat_id, msg_id, is_channel, chat_title, sender_id, sender_name, "
    "text, media_path, media_type, date, deleted, media_size, reply_to_msg_id"
)


@dataclass
class StoredMessage:
    chat_id: int
    msg_id: int
    is_channel: bool
    chat_title: str | None
    sender_id: int | None
    sender_name: str | None
    text: str | None
    media_path: str | None
    media_type: str | None
    date: int
    deleted: bool = False
    media_size: int | None = None
    reply_to_msg_id: int | None = None


class Storage:
    """One connection, one thread — the whole app runs in a single event loop."""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database already existed on disk —
        CREATE TABLE IF NOT EXISTS in SCHEMA never touches an existing table."""
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(messages)")}
        if "media_size" not in cols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN media_size INTEGER")
        if "reply_to_msg_id" not in cols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN reply_to_msg_id INTEGER")

    # An overwrite refreshes the content columns only. deleted, media_path and
    # media_size describe what we already know and did about the message, and
    # the incoming row never carries them — a plain INSERT OR REPLACE would
    # reset the flag and drop the downloaded file reference. That matters
    # because catch_up replays NewMessage updates after a reconnect: a replay
    # of an already-reported deletion would resurrect the row and the next
    # reconcile pass would report the same deletion again.
    _UPSERT_SUFFIX = """
        ON CONFLICT(chat_id, msg_id) DO UPDATE SET
            is_channel      = excluded.is_channel,
            chat_title      = excluded.chat_title,
            sender_id       = excluded.sender_id,
            sender_name     = excluded.sender_name,
            text            = excluded.text,
            media_type      = excluded.media_type,
            date            = excluded.date,
            reply_to_msg_id = excluded.reply_to_msg_id
        WHERE messages.deleted = 0
    """

    def save_message(self, msg: StoredMessage, *, overwrite: bool = True) -> None:
        suffix = self._UPSERT_SUFFIX if overwrite else "ON CONFLICT DO NOTHING"
        self.conn.execute(
            f"INSERT INTO messages ({_COLUMNS}) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) {suffix}",
            (
                msg.chat_id,
                msg.msg_id,
                int(msg.is_channel),
                msg.chat_title,
                msg.sender_id,
                msg.sender_name,
                msg.text,
                msg.media_path,
                msg.media_type,
                msg.date,
                int(msg.deleted),
                msg.media_size,
                msg.reply_to_msg_id,
            ),
        )
        self.conn.commit()

    def find_message(self, chat_id: int | None, msg_id: int) -> StoredMessage | None:
        """Look up a message.

        Deletion events only carry chat_id for channels and supergroups. When it
        is None the message came from a DM or a basic group, where msg_id is a
        single account-wide sequence — so the is_channel flag is what keeps a
        channel message with the same id from being reported by mistake.
        """
        if chat_id is not None:
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM messages WHERE chat_id = ? AND msg_id = ?",
                (chat_id, msg_id),
            ).fetchone()
        else:
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM messages "
                "WHERE msg_id = ? AND is_channel = 0 "
                "ORDER BY date DESC LIMIT 1",
                (msg_id,),
            ).fetchone()
        return self._to_message(row)

    def mark_deleted(self, chat_id: int, msg_id: int) -> None:
        self.conn.execute(
            "UPDATE messages SET deleted = 1 WHERE chat_id = ? AND msg_id = ?",
            (chat_id, msg_id),
        )
        self.conn.commit()

    def update_media_path(
        self, chat_id: int, msg_id: int, media_path: str, media_size: int | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE messages SET media_path = ?, media_size = ? "
            "WHERE chat_id = ? AND msg_id = ?",
            (media_path, media_size, chat_id, msg_id),
        )
        self.conn.commit()

    def set_media_size(self, chat_id: int, msg_id: int, media_size: int) -> None:
        self.conn.execute(
            "UPDATE messages SET media_size = ? WHERE chat_id = ? AND msg_id = ?",
            (media_size, chat_id, msg_id),
        )
        self.conn.commit()

    def rows_missing_media_size(self) -> list[tuple[int, int, str]]:
        """(chat_id, msg_id, media_path) for rows downloaded before media_size
        was tracked — used to backfill it once from the files already on disk."""
        rows = self.conn.execute(
            "SELECT chat_id, msg_id, media_path FROM messages "
            "WHERE media_path IS NOT NULL AND media_size IS NULL"
        ).fetchall()
        return [(r["chat_id"], r["msg_id"], r["media_path"]) for r in rows]

    def media_total_bytes(self) -> int:
        return self.conn.execute(
            "SELECT COALESCE(SUM(media_size), 0) FROM messages WHERE media_path IS NOT NULL"
        ).fetchone()[0]

    def evict_oldest_media(self, target_bytes: int) -> list[str]:
        """Drop the media reference (oldest message first) from rows until the
        tracked total is back under target_bytes. Message rows and their text
        stay — only media_path/media_size are cleared. Returns the freed file
        paths for the caller to unlink from disk."""
        total = self.media_total_bytes()
        if total <= target_bytes:
            return []
        rows = self.conn.execute(
            "SELECT chat_id, msg_id, media_path, media_size FROM messages "
            "WHERE media_path IS NOT NULL ORDER BY date ASC"
        ).fetchall()
        paths: list[str] = []
        for row in rows:
            if total <= target_bytes:
                break
            paths.append(row["media_path"])
            total -= row["media_size"] or 0
            self.conn.execute(
                "UPDATE messages SET media_path = NULL, media_size = NULL "
                "WHERE chat_id = ? AND msg_id = ?",
                (row["chat_id"], row["msg_id"]),
            )
        self.conn.commit()
        return paths

    def alive_ids(self, chat_id: int, since_ts: int) -> list[int]:
        """Ids this chat is still believed to hold, for reconciliation against
        Telegram — anything missing there was deleted without an update."""
        rows = self.conn.execute(
            "SELECT msg_id FROM messages "
            "WHERE chat_id = ? AND deleted = 0 AND date >= ? ORDER BY msg_id ASC",
            (chat_id, since_ts),
        ).fetchall()
        return [r["msg_id"] for r in rows]

    def recent_chats(self, since_ts: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT chat_id FROM messages WHERE deleted = 0 AND date >= ? "
            "GROUP BY chat_id ORDER BY MAX(date) DESC",
            (since_ts,),
        ).fetchall()
        return [r["chat_id"] for r in rows]

    def update_text(self, chat_id: int, msg_id: int, new_text: str | None) -> None:
        self.conn.execute(
            "UPDATE messages SET text = ? WHERE chat_id = ? AND msg_id = ?",
            (new_text, chat_id, msg_id),
        )
        self.conn.commit()

    def add_edit(
        self,
        chat_id: int,
        msg_id: int,
        old_text: str | None,
        new_text: str | None,
        date: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO edits (chat_id, msg_id, old_text, new_text, date) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, msg_id, old_text, new_text, date),
        )
        self.conn.commit()

    def get_edits(
        self, chat_id: int, msg_id: int
    ) -> list[tuple[str | None, str | None, int]]:
        rows = self.conn.execute(
            "SELECT old_text, new_text, date FROM edits "
            "WHERE chat_id = ? AND msg_id = ? ORDER BY date ASC, rowid ASC",
            (chat_id, msg_id),
        ).fetchall()
        return [(r["old_text"], r["new_text"], r["date"]) for r in rows]

    def add_watch(self, chat_id: int, label: str | None, added_at: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO watch_chats (chat_id, label, added_at) "
            "VALUES (?, ?, ?)",
            (chat_id, label, added_at),
        )
        self.conn.commit()

    def remove_watch(self, chat_id: int) -> None:
        self.conn.execute("DELETE FROM watch_chats WHERE chat_id = ?", (chat_id,))
        self.conn.commit()

    def list_watch(self) -> list[tuple[int, str | None, int]]:
        rows = self.conn.execute(
            "SELECT chat_id, label, added_at FROM watch_chats ORDER BY added_at ASC"
        ).fetchall()
        return [(r["chat_id"], r["label"], r["added_at"]) for r in rows]

    def purge_older_than(self, cutoff_ts: int) -> tuple[int, list[str]]:
        """Drop rows older than cutoff_ts, returning (rows purged, media paths to unlink)."""
        rows = self.conn.execute(
            "SELECT media_path FROM messages WHERE date < ? AND media_path IS NOT NULL",
            (cutoff_ts,),
        ).fetchall()
        paths = [r["media_path"] for r in rows]
        row_count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE date < ?", (cutoff_ts,)
        ).fetchone()["n"]
        self.conn.execute(
            "DELETE FROM edits WHERE (chat_id, msg_id) IN "
            "(SELECT chat_id, msg_id FROM messages WHERE date < ?)",
            (cutoff_ts,),
        )
        self.conn.execute("DELETE FROM messages WHERE date < ?", (cutoff_ts,))
        self.conn.commit()
        return row_count, paths

    def _purge_conditions(
        self, cutoff_ts: int | None, chat_id: int | None
    ) -> tuple[str, list[int]]:
        """Build the WHERE conditions shared by count_purge_candidates and purge.
        cutoff_ts is a lower bound — /purge <N> targets rows from the last N
        days (date >= cutoff), not rows older than it.
        Requiring at least one filter keeps a bare (None, None) call from ever
        matching the whole table."""
        conditions = []
        params: list[int] = []
        if cutoff_ts is not None:
            conditions.append("date >= ?")
            params.append(cutoff_ts)
        if chat_id is not None:
            conditions.append("chat_id = ?")
            params.append(chat_id)
        if not conditions:
            raise ValueError("cutoff_ts and chat_id cannot both be None")
        return " AND ".join(conditions), params

    def count_purge_candidates(
        self, cutoff_ts: int | None, chat_id: int | None
    ) -> tuple[int, int]:
        """Return (message_count, media_count) matching the scope, for a
        /purge dry-run report."""
        where, params = self._purge_conditions(cutoff_ts, chat_id)
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {where}", params
        ).fetchone()[0]
        media = self.conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {where} AND media_path IS NOT NULL",
            params,
        ).fetchone()[0]
        return total, media

    def known_media_paths(self) -> set[str]:
        """All media_path values still referenced by any row, across every
        chat. Used to find orphan files on disk — media whose row was
        already deleted (by a prior purge/retention pass) but whose file
        was never swept, because that sweep only ever touched paths pulled
        from the rows it was deleting."""
        rows = self.conn.execute(
            "SELECT media_path FROM messages WHERE media_path IS NOT NULL"
        ).fetchall()
        return {r["media_path"] for r in rows}

    def purge(self, cutoff_ts: int | None, chat_id: int | None) -> list[str]:
        """Drop rows matching the scope, returning media paths to unlink."""
        where, params = self._purge_conditions(cutoff_ts, chat_id)
        rows = self.conn.execute(
            f"SELECT media_path FROM messages WHERE {where} AND media_path IS NOT NULL",
            params,
        ).fetchall()
        paths = [r["media_path"] for r in rows]
        self.conn.execute(
            "DELETE FROM edits WHERE (chat_id, msg_id) IN "
            f"(SELECT chat_id, msg_id FROM messages WHERE {where})",
            params,
        )
        self.conn.execute(f"DELETE FROM messages WHERE {where}", params)
        self.conn.commit()
        return paths

    def stats(self, top_n: int = 10) -> dict:
        """Snapshot for /stats: counts, date range, and the busiest chats.
        Disk sizes (DB file, media directory) are not SQLite's business — the
        caller stats those paths itself."""
        total_messages = self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        deleted_messages = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE deleted = 1"
        ).fetchone()[0]
        total_edits = self.conn.execute("SELECT COUNT(*) FROM edits").fetchone()[0]
        distinct_chats = self.conn.execute(
            "SELECT COUNT(DISTINCT chat_id) FROM messages"
        ).fetchone()[0]
        watched_chats = self.conn.execute("SELECT COUNT(*) FROM watch_chats").fetchone()[0]
        oldest, newest = self.conn.execute(
            "SELECT MIN(date), MAX(date) FROM messages"
        ).fetchone()
        top_rows = self.conn.execute(
            "SELECT chat_id, COUNT(*) AS cnt FROM messages "
            "GROUP BY chat_id ORDER BY cnt DESC LIMIT ?",
            (top_n,),
        ).fetchall()
        return {
            "total_messages": total_messages,
            "deleted_messages": deleted_messages,
            "total_edits": total_edits,
            "distinct_chats": distinct_chats,
            "watched_chats": watched_chats,
            "oldest_date": oldest,
            "newest_date": newest,
            "top_chats": [(r["chat_id"], r["cnt"]) for r in top_rows],
        }

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _to_message(row: sqlite3.Row | None) -> StoredMessage | None:
        if row is None:
            return None
        return StoredMessage(
            chat_id=row["chat_id"],
            msg_id=row["msg_id"],
            is_channel=bool(row["is_channel"]),
            chat_title=row["chat_title"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            text=row["text"],
            media_path=row["media_path"],
            media_type=row["media_type"],
            date=row["date"],
            deleted=bool(row["deleted"]),
            media_size=row["media_size"],
            reply_to_msg_id=row["reply_to_msg_id"],
        )
