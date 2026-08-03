"""SQLite persistence for cached messages and their edit history."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    is_channel  INTEGER NOT NULL DEFAULT 0,
    chat_title  TEXT,
    sender_id   INTEGER,
    sender_name TEXT,
    text        TEXT,
    media_path  TEXT,
    media_type  TEXT,
    date        INTEGER NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0,
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
    "text, media_path, media_type, date, deleted"
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
        self.conn.commit()

    def save_message(self, msg: StoredMessage, *, overwrite: bool = True) -> None:
        verb = "REPLACE" if overwrite else "IGNORE"
        self.conn.execute(
            f"INSERT OR {verb} INTO messages ({_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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

    def purge_older_than(self, cutoff_ts: int) -> list[str]:
        """Drop rows older than cutoff_ts, returning media paths to unlink."""
        rows = self.conn.execute(
            "SELECT media_path FROM messages WHERE date < ? AND media_path IS NOT NULL",
            (cutoff_ts,),
        ).fetchall()
        paths = [r["media_path"] for r in rows]
        self.conn.execute(
            "DELETE FROM edits WHERE (chat_id, msg_id) IN "
            "(SELECT chat_id, msg_id FROM messages WHERE date < ?)",
            (cutoff_ts,),
        )
        self.conn.execute("DELETE FROM messages WHERE date < ?", (cutoff_ts,))
        self.conn.commit()
        return paths

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
        )
