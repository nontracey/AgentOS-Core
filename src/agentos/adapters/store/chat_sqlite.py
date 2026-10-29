"""Conversation persistence in its own SQLite file beside the core database."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    subject TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    preset TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    pinned INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(tenant, subject, updated_at);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    thinking TEXT NOT NULL DEFAULT '',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'complete',
    stats_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    superseded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, seq);
CREATE TABLE IF NOT EXISTS presets (
    id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    subject TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ChatStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(_SCHEMA)

    # ------------------------------------------------------------ conversations
    @staticmethod
    def _conversation(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "title": row["title"], "model": row["model"], "preset": row["preset"],
                "system_prompt": row["system_prompt"], "params": json.loads(row["params_json"] or "{}"),
                "pinned": bool(row["pinned"]), "archived": bool(row["archived"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def create_conversation(self, tenant: str, subject: str, *, title: str = "", model: str = "",
                            preset: str = "", system_prompt: str = "",
                            params: dict[str, Any] | None = None) -> dict[str, Any]:
        cid = uuid.uuid4().hex
        stamp = now_iso()
        with self._lock:
            self._db.execute(
                "INSERT INTO conversations(id, tenant, subject, title, model, preset, system_prompt, params_json,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid, tenant, subject, title, model, preset, system_prompt,
                 json.dumps(params or {}, ensure_ascii=False), stamp, stamp))
        found = self.get_conversation(tenant, subject, cid)
        assert found is not None
        return found

    def get_conversation(self, tenant: str, subject: str, cid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM conversations WHERE id=? AND tenant=? AND subject=?",
                                   (cid, tenant, subject)).fetchone()
        return self._conversation(row) if row else None

    def list_conversations(self, tenant: str, subject: str, *, archived: bool = False,
                           query: str = "", limit: int = 200) -> list[dict[str, Any]]:
        sql = ("SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id AND m.superseded=0)"
               " AS message_count FROM conversations c WHERE c.tenant=? AND c.subject=? AND c.archived=?")
        args: list[Any] = [tenant, subject, int(archived)]
        if query:
            sql += (" AND (c.title LIKE ? OR EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id=c.id"
                    " AND m.superseded=0 AND m.content LIKE ?))")
            args += [f"%{query}%", f"%{query}%"]
        sql += " ORDER BY c.pinned DESC, c.updated_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [{**self._conversation(r), "message_count": int(r["message_count"])} for r in rows]

    def update_conversation(self, tenant: str, subject: str, cid: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"title", "model", "preset", "system_prompt", "pinned", "archived"}
        sets: list[str] = []
        args: list[Any] = []
        for key, value in fields.items():
            if key == "params":
                sets.append("params_json=?")
                args.append(json.dumps(value or {}, ensure_ascii=False))
            elif key in allowed:
                sets.append(f"{key}=?")
                args.append(int(value) if key in {"pinned", "archived"} else str(value))
        sets.append("updated_at=?")
        args.append(now_iso())
        with self._lock:
            self._db.execute(f"UPDATE conversations SET {', '.join(sets)} WHERE id=? AND tenant=? AND subject=?",
                             (*args, cid, tenant, subject))
        return self.get_conversation(tenant, subject, cid)

    def touch(self, cid: str) -> None:
        with self._lock:
            self._db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now_iso(), cid))

    def delete_conversation(self, tenant: str, subject: str, cid: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM conversations WHERE id=? AND tenant=? AND subject=?",
                                   (cid, tenant, subject))
            if cur.rowcount:
                self._db.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        return bool(cur.rowcount)

    # ------------------------------------------------------------ messages
    @staticmethod
    def _message(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "conversation_id": row["conversation_id"], "seq": row["seq"],
                "role": row["role"], "content": row["content"], "thinking": row["thinking"],
                "attachments": json.loads(row["attachments_json"] or "[]"), "model": row["model"],
                "status": row["status"], "stats": json.loads(row["stats_json"] or "{}"), "error": row["error"],
                "superseded": bool(row["superseded"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def add_message(self, cid: str, role: str, *, content: str = "", attachments: list[dict[str, Any]] | None = None,
                    model: str = "", status: str = "complete") -> dict[str, Any]:
        mid = uuid.uuid4().hex
        stamp = now_iso()
        with self._lock:
            seq = int(self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM messages WHERE conversation_id=?",
                                       (cid,)).fetchone()[0]) + 1
            self._db.execute(
                "INSERT INTO messages(id, conversation_id, seq, role, content, attachments_json, model, status,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (mid, cid, seq, role, content, json.dumps(attachments or [], ensure_ascii=False), model, status,
                 stamp, stamp))
            self._db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (stamp, cid))
        found = self.get_message(mid)
        assert found is not None
        return found

    def get_message(self, mid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        return self._message(row) if row else None

    def list_messages(self, cid: str, *, include_superseded: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE conversation_id=?"
        if not include_superseded:
            sql += " AND superseded=0"
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY seq", (cid,)).fetchall()
        return [self._message(r) for r in rows]

    def update_message(self, mid: str, **fields: Any) -> None:
        mapping = {"content": "content", "thinking": "thinking", "status": "status", "error": "error",
                   "model": "model"}
        sets: list[str] = []
        args: list[Any] = []
        for key, value in fields.items():
            if key == "stats":
                sets.append("stats_json=?")
                args.append(json.dumps(value or {}, ensure_ascii=False))
            elif key in mapping:
                sets.append(f"{mapping[key]}=?")
                args.append(str(value))
        if not sets:
            return
        sets.append("updated_at=?")
        args.append(now_iso())
        with self._lock:
            self._db.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", (*args, mid))

    def supersede_from(self, cid: str, seq: int) -> None:
        """Hide messages from `seq` onward (an edited question or a regenerated answer)."""
        with self._lock:
            self._db.execute("UPDATE messages SET superseded=1 WHERE conversation_id=? AND seq>=?", (cid, seq))

    def mark_interrupted(self) -> int:
        """Generations cut off by a restart keep their partial text with a visible status."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE messages SET status='interrupted', updated_at=? WHERE status IN ('pending','streaming')",
                (now_iso(),))
        return int(cur.rowcount)

    # ------------------------------------------------------------ presets
    def list_presets(self, tenant: str, subject: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM presets WHERE tenant=? AND subject=? ORDER BY created_at",
                                    (tenant, subject)).fetchall()
        return [{"id": r["id"], "name": r["name"], "description": r["description"],
                 "system_prompt": r["system_prompt"], "builtin": False} for r in rows]

    def save_preset(self, tenant: str, subject: str, *, name: str, system_prompt: str,
                    description: str = "", preset_id: str = "") -> dict[str, Any]:
        pid = preset_id or "custom-" + uuid.uuid4().hex[:10]
        with self._lock:
            self._db.execute(
                "INSERT INTO presets(id, tenant, subject, name, description, system_prompt, created_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                " description=excluded.description, system_prompt=excluded.system_prompt",
                (pid, tenant, subject, name, description, system_prompt, now_iso()))
        return {"id": pid, "name": name, "description": description, "system_prompt": system_prompt,
                "builtin": False}

    def delete_preset(self, tenant: str, subject: str, preset_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM presets WHERE id=? AND tenant=? AND subject=?",
                             (preset_id, tenant, subject))

    def close(self) -> None:
        with self._lock:
            self._db.close()
