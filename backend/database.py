"""
SQLite-based persistence for conversations and messages.
"""

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).resolve().parent / "agent_os.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );

            CREATE INDEX IF NOT EXISTS idx_conv_project
                ON conversations(project_id);
            CREATE INDEX IF NOT EXISTS idx_msg_conv
                ON messages(conversation_id);
        """)


def create_conversation(project_id: str, title: str) -> dict:
    conv_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO conversations (id, project_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (conv_id, project_id, title, now, now),
        )
    return {"id": conv_id, "project_id": project_id, "title": title, "created_at": now, "updated_at": now}


def list_conversations(project_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, project_id, title, created_at, updated_at FROM conversations WHERE project_id = ? ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conv_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, project_id, title, created_at, updated_at FROM conversations WHERE id = ?",
            (conv_id,),
        ).fetchone()
    return dict(row) if row else None


def update_conversation_title(conv_id: str, title: str):
    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, conv_id),
        )


def touch_conversation(conv_id: str):
    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id))


def add_message(conversation_id: str, role: str, content: str) -> dict:
    msg_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO messages (id, conversation_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (msg_id, conversation_id, role, content, now),
        )
    touch_conversation(conversation_id)
    return {"id": msg_id, "conversation_id": conversation_id, "role": role, "content": content, "timestamp": now}


def list_messages(conversation_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, conversation_id, role, content, timestamp FROM messages WHERE conversation_id = ? ORDER BY timestamp ASC",
            (conversation_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_conversation(conv_id: str):
    with get_db() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))


def delete_conversations_for_project(project_id: str):
    """Delete all conversations and their messages for a project."""
    with get_db() as conn:
        conv_ids = conn.execute(
            "SELECT id FROM conversations WHERE project_id = ?", (project_id,)
        ).fetchall()
        for row in conv_ids:
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (row["id"],))
        conn.execute("DELETE FROM conversations WHERE project_id = ?", (project_id,))


def rename_project_conversations(old_project_id: str, new_project_id: str):
    """Update project_id for all conversations when a project is renamed."""
    with get_db() as conn:
        conn.execute(
            "UPDATE conversations SET project_id = ? WHERE project_id = ?",
            (new_project_id, old_project_id),
        )
