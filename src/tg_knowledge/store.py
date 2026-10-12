from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .media import media_skip_reason


def json_text(value: Any) -> str:
    def fallback(obj: Any) -> Any:
        if isinstance(obj, bytes):
            import base64

            return {"__bytes__": base64.b64encode(obj).decode("ascii")}
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        raise TypeError(f"Unsupported type: {type(obj).__name__}")

    return json.dumps(value, ensure_ascii=False, default=fallback)


class Archive:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(root / "archive.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, topic_id INTEGER, date TEXT NOT NULL,
                edit_date TEXT, sender_id INTEGER, sender_name TEXT, sender_username TEXT,
                reply_id INTEGER, text TEXT NOT NULL, service TEXT,
                media_type TEXT, media_json TEXT, raw_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_topic ON messages(topic_id, id);
            CREATE TABLE IF NOT EXISTS attachments (
                message_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
                path TEXT, size INTEGER, error TEXT
            );
        """)

    def __enter__(self) -> Archive:
        return self

    def __exit__(self, *args: object) -> None:
        self.db.close()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json_text(value)))
        self.db.commit()

    def topic(self, topic_id: int, title: str, raw: Any = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO topics VALUES (?,?,?)",
            (topic_id, title, json_text(raw or {})),
        )

    def save_message(self, record: dict) -> None:
        self.db.execute(
            """
            DELETE FROM attachments WHERE message_id=? AND EXISTS (
                SELECT 1 FROM messages WHERE id=? AND
                (media_json IS NOT ? OR media_type IS NOT ?)
            )
        """,
            (record["id"], record["id"], record["media_json"], record["media_type"]),
        )
        keys = list(record)
        self.db.execute(
            f"INSERT INTO messages ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)}) "
            "ON CONFLICT(id) DO UPDATE SET "
            + ",".join(f"{key}=excluded.{key}" for key in keys if key != "id"),
            list(record.values()),
        )

    def attachment(self, message_id: int, status: str, path=None, size=None, error=None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO attachments VALUES (?,?,?,?,?)",
            (message_id, status, path, size, error),
        )
        self.db.commit()

    def stats(self) -> dict:
        media_total = self.db.execute("""
            SELECT COUNT(*) FROM messages
            WHERE media_type IN ('MessageMediaPhoto','MessageMediaDocument')
        """).fetchone()[0]
        attachment_counts = dict(
            self.db.execute("""
                SELECT a.status,COUNT(*) FROM attachments a JOIN messages m ON m.id=a.message_id
                WHERE m.media_type IN ('MessageMediaPhoto','MessageMediaDocument') GROUP BY a.status
            """).fetchall()
        )
        downloaded = attachment_counts.get("downloaded", 0)
        policy = self.get("media_policy", "all")
        max_mb = self.get("max_media_mb")
        selected, excluded = Counter(), Counter()
        selected_total = 0
        for row in self.db.execute("""
            SELECT m.media_type,m.media_json,a.status FROM messages m
            LEFT JOIN attachments a ON a.message_id=m.id
            WHERE m.media_type IN ('MessageMediaPhoto','MessageMediaDocument')
        """):
            reason = media_skip_reason(
                row["media_type"], json.loads(row["media_json"] or "{}"), policy, max_mb
            )
            if policy == "source" and row["status"] == "discarded":
                reason = "processed_no_useful_text"
            if reason:
                excluded[reason] += 1
            else:
                selected_total += 1
                if row["status"]:
                    selected[row["status"]] += 1
        selected_downloaded = selected.get("downloaded", 0)
        return {
            "chat": self.get("chat"),
            "messages": self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "topics": self.db.execute("SELECT COUNT(*) FROM topics").fetchone()[0],
            "history_complete": self.get("history_complete", False),
            "last_sync": self.get("last_sync"),
            "attachments": attachment_counts,
            "media_total": media_total,
            "media_pending": max(0, media_total - downloaded),
            "media_complete": media_total == downloaded,
            "media_selection": {
                "policy": policy,
                "total": selected_total,
                "downloaded": selected_downloaded,
                "pending": max(0, selected_total - selected_downloaded),
                "complete": selected_total == selected_downloaded,
                "attachments": dict(selected),
                "excluded": sum(excluded.values()),
                "excluded_reasons": dict(excluded),
            },
            "media_processing": self.get("media_processing"),
            "streams": self.get("streams", {}),
        }
