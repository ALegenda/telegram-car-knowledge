from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from .store import Store


def readonly(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def import_archive(store: Store, root: Path, chat_id: int) -> int:
    """Read the original export/index without modifying or relabelling its chat."""
    with readonly(root / "archive.sqlite3") as archive:
        archive.row_factory = sqlite3.Row
        row = archive.execute("SELECT value FROM metadata WHERE key='chat'").fetchone()
        chat = json.loads(row[0]) if row else {}
        if int(chat.get("id", 0)) != chat_id:
            raise ValueError("ID исходного архива не совпадает с целевым чатом; импорт отменён.")
        store.register({
            "id": chat_id, "type": "supergroup" if str(chat_id).startswith("-100") else "group",
            "title": chat.get("title", ""), "username": chat.get("username"),
        })
        passages = defaultdict(list)
        index_path = root / "knowledge" / "index.sqlite3"
        if index_path.is_file():
            with readonly(index_path) as index:
                index.row_factory = sqlite3.Row
                for chunk in index.execute("SELECT * FROM chunks ORDER BY id"):
                    sources = json.loads(chunk["sources_json"])
                    anchor = next(s["message_id"] for s in sources if s["role"] == "message")
                    passages[anchor].append(("Архив, " + chunk["topic"][:60], chunk["text"]))
        count = 0
        for message in archive.execute("SELECT * FROM messages ORDER BY id"):
            # Keep newer Bot API copies if the archive is imported a second time.
            old = store.db.execute(
                "SELECT raw_json FROM messages WHERE chat_id=? AND id=?", (chat_id, message["id"])
            ).fetchone()
            if old and json.loads(old[0]).get("_update_id") is not None:
                continue
            sections = passages.get(message["id"]) or (
                [("Архив, сообщение", message["text"])] if message["text"] else []
            )
            store.replace_message(
                chat_id, message["id"], message["date"], message["text"],
                {"imported": True, "original": json.loads(message["raw_json"])}, sections,
                "imported_index" if index_path.is_file() else "imported_text_only",
            )
            count += 1
    return count
