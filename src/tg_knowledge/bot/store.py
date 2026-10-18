from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

import numpy as np

from ..knowledge import QUERY_STOP_WORDS, normalize, split_text, tokens


class Store:
    def __init__(self, root: Path, *, readonly: bool = False):
        if readonly:
            self.db = sqlite3.connect((root / "bot.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
        else:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.db = sqlite3.connect(root / "bot.sqlite3")
        self.db.row_factory = sqlite3.Row
        if readonly:
            return
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY, type TEXT NOT NULL, title TEXT NOT NULL,
                username TEXT, enabled INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER NOT NULL, id INTEGER NOT NULL, date TEXT NOT NULL,
                text TEXT NOT NULL, raw_json TEXT NOT NULL, digest TEXT NOT NULL,
                document_status TEXT NOT NULL, PRIMARY KEY(chat_id,id)
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                text TEXT NOT NULL, locator TEXT NOT NULL, date TEXT NOT NULL,
                vector BLOB, vector_model TEXT,
                FOREIGN KEY(chat_id,message_id) REFERENCES messages(chat_id,id)
            );
            CREATE INDEX IF NOT EXISTS chunks_chat ON chunks(chat_id,message_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(tokens);
            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, error TEXT
            );
            CREATE TABLE IF NOT EXISTS throttle (key TEXT PRIMARY KEY, until REAL NOT NULL);
        """)

    def close(self):
        self.db.close()

    def get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, value))

    def enqueue(self, updates: list[dict]):
        # Commit the durable inbox and next offset together, before acknowledging Telegram.
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO inbox(id,payload) VALUES (?,?)",
                [(u["update_id"], json.dumps(u, ensure_ascii=False)) for u in updates],
            )
            if updates:
                offset = max(int(self.get("offset", "0")), max(u["update_id"] for u in updates) + 1)
                self.db.execute(
                    "INSERT OR REPLACE INTO state VALUES ('offset',?)", (str(offset),)
                )

    def next_update(self):
        return self.db.execute(
            "SELECT * FROM inbox WHERE status='pending' AND next_attempt<=? ORDER BY id LIMIT 1",
            (time.time(),),
        ).fetchone()

    def finish(self, update_id: int):
        with self.db:
            # Archive messages separately; don't retain a second copy of every update forever.
            self.db.execute("DELETE FROM inbox WHERE id=?", (update_id,))

    def fail(self, update_id: int, error: str, retry_after: float = 0):
        with self.db:
            self.db.execute(
                "UPDATE inbox SET attempts=attempts+1, error=?, "
                "status=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END, "
                "next_attempt=? WHERE id=?",
                (error, time.time() + max(retry_after, 30), update_id),
            )

    def register(self, chat: dict):
        with self.db:
            self.db.execute(
                "INSERT INTO chats(id,type,title,username) VALUES (?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET type=excluded.type,title=excluded.title,"
                "username=excluded.username",
                (chat["id"], chat["type"], chat.get("title", ""), chat.get("username")),
            )

    def chat(self, chat_id: int):
        return self.db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()

    def enable(self, chat_id: int, enabled: bool):
        with self.db:
            self.db.execute("UPDATE chats SET enabled=? WHERE id=?", (int(enabled), chat_id))

    def rate_limit(self, key: str, seconds: int = 15) -> bool:
        now = time.time()
        row = self.db.execute("SELECT until FROM throttle WHERE key=?", (key,)).fetchone()
        if row and row[0] > now:
            return False
        with self.db:
            self.db.execute("DELETE FROM throttle WHERE until<?", (now,))
            self.db.execute("INSERT OR REPLACE INTO throttle VALUES (?,?)", (key, now + seconds))
        return True

    def replace_message(
        self, chat_id: int, message_id: int, date: str, text: str, raw: dict,
        sections: list[tuple[str, str]], document_status: str = "none",
    ):
        digest = hashlib.sha256(json.dumps(sections, ensure_ascii=False).encode()).hexdigest()
        previous = self.db.execute(
            "SELECT digest,raw_json FROM messages WHERE chat_id=? AND id=?", (chat_id, message_id)
        ).fetchone()
        if previous and json.loads(previous["raw_json"]).get("_update_id", -1) > raw.get("_update_id", -1):
            return
        with self.db:
            self.db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?,?) ON CONFLICT(chat_id,id) DO UPDATE "
                "SET date=excluded.date,text=excluded.text,raw_json=excluded.raw_json,"
                "digest=excluded.digest,document_status=excluded.document_status",
                (chat_id, message_id, date, text, json.dumps(raw, ensure_ascii=False),
                 digest, document_status),
            )
            if previous and previous[0] == digest:
                return
            self.db.execute(
                "DELETE FROM chunks_fts WHERE rowid IN "
                "(SELECT id FROM chunks WHERE chat_id=? AND message_id=?)", (chat_id, message_id)
            )
            self.db.execute(
                "DELETE FROM chunks WHERE chat_id=? AND message_id=?", (chat_id, message_id)
            )
            for locator, body in sections:
                for part in split_text(normalize(body), size=1400, overlap=150):
                    cursor = self.db.execute(
                        "INSERT INTO chunks(chat_id,message_id,text,locator,date) VALUES (?,?,?,?,?)",
                        (chat_id, message_id, part, locator, date),
                    )
                    self.db.execute(
                        "INSERT INTO chunks_fts(rowid,tokens) VALUES (?,?)",
                        (cursor.lastrowid, tokens(part)),
                    )

    def pending_vectors(self, model: str, limit: int = 32):
        return self.db.execute(
            "SELECT c.id,c.text FROM chunks c JOIN chats t ON t.id=c.chat_id "
            "WHERE t.enabled=1 AND (c.vector IS NULL OR c.vector_model IS NOT ?) "
            "ORDER BY c.id LIMIT ?", (model, limit),
        ).fetchall()

    def save_vectors(self, rows, vectors: list[list[float]], model: str):
        with self.db:
            for row, vector in zip(rows, vectors, strict=True):
                self.db.execute(
                    "UPDATE chunks SET vector=?,vector_model=? WHERE id=? AND text=?",
                    (np.asarray(vector, dtype="<f4").tobytes(), model, row["id"], row["text"]),
                )

    def search(self, chat_id: int, question: str, vector: list[float], model: str, limit=6):
        terms = [t for t in dict.fromkeys(tokens(question).split()) if t not in QUERY_STOP_WORDS]
        rankings = []
        if terms:
            match = " OR ".join('"' + t + '"' for t in terms[:80])
            rankings.append([row[0] for row in self.db.execute(
                "SELECT c.id FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid "
                "WHERE chunks_fts MATCH ? AND c.chat_id=? ORDER BY bm25(chunks_fts) LIMIT 50",
                (match, chat_id),
            )])
        query = np.asarray(vector, dtype="<f4")
        norm = float(np.linalg.norm(query))
        semantic = []
        if norm:
            query /= norm
            for row in self.db.execute(
                "SELECT id,vector FROM chunks WHERE chat_id=? AND vector_model=? "
                "AND vector IS NOT NULL", (chat_id, model),
            ):
                candidate = np.frombuffer(row["vector"], dtype="<f4")
                if candidate.shape == query.shape:
                    score = float(candidate @ query) / (float(np.linalg.norm(candidate)) or 1)
                    if score >= 0.25:
                        semantic.append((score, row["id"]))
            rankings.append([i for _, i in sorted(semantic, reverse=True)[:50]])
        fused = Counter()
        for ranking in rankings:
            for rank, chunk_id in enumerate(ranking, 1):
                fused[chunk_id] += 1 / (60 + rank)
        hits, seen = [], set()
        for chunk_id, _ in fused.most_common():
            row = self.db.execute(
                "SELECT * FROM chunks WHERE id=? AND chat_id=?", (chunk_id, chat_id)
            ).fetchone()
            # Keep several useful pages from a document, but avoid near-identical chunks.
            key = (row["message_id"], row["locator"])
            if key in seen:
                continue
            seen.add(key)
            hits.append(dict(row))
            if len(hits) >= limit:
                break
        return hits

    def context(self, chat_id: int, message_id: int) -> list[dict]:
        row = self.db.execute(
            "SELECT raw_json FROM messages WHERE chat_id=? AND id=?", (chat_id, message_id)
        ).fetchone()
        if not row:
            return []
        raw = json.loads(row[0])
        reply = raw.get("reply_to_message", {}).get("message_id")
        candidates = self.db.execute(
            "SELECT m.* FROM messages m WHERE m.chat_id=? AND m.id!=? AND "
            "(m.id=? OR m.id IN (SELECT id FROM messages WHERE chat_id=? AND id<? "
            "ORDER BY id DESC LIMIT 2)) AND EXISTS (SELECT 1 FROM chunks c "
            "WHERE c.chat_id=m.chat_id AND c.message_id=m.id) ORDER BY m.id DESC LIMIT 3",
            (chat_id, message_id, reply, chat_id, message_id),
        ).fetchall()
        return [
            {"message_id": r["id"], "date": r["date"], "text": r["text"][:1000],
             "locator": "Контекст сообщения"}
            for r in candidates
            if r["text"] and json.loads(r["raw_json"]).get("message_thread_id") == raw.get("message_thread_id")
        ][:2]

    def stats(self, chat_id: int | None = None) -> dict:
        where = " WHERE chat_id=?" if chat_id is not None else ""
        params = (chat_id,) if chat_id is not None else ()
        return {
            "messages": self.db.execute("SELECT COUNT(*) FROM messages" + where, params).fetchone()[0],
            "chunks": self.db.execute("SELECT COUNT(*) FROM chunks" + where, params).fetchone()[0],
            "embedded": self.db.execute(
                "SELECT COUNT(*) FROM chunks" + where +
                (" AND" if where else " WHERE") + " vector IS NOT NULL", params
            ).fetchone()[0],
            "documents": dict(self.db.execute(
                "SELECT document_status,COUNT(*) FROM messages" + where + " GROUP BY document_status",
                params,
            ).fetchall()),
            "inbox": dict(self.db.execute(
                "SELECT status,COUNT(*) FROM inbox GROUP BY status"
            ).fetchall()) if chat_id is None else None,
            "last_poll": self.get("last_poll") if chat_id is None else None,
        }
