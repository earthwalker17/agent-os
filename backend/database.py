"""
SQLite-based persistence for conversations, messages, and pending executions.
"""

import json
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
    """Create tables if they don't exist, and run idempotent migrations.

    Migration strategy: tables are created via ``CREATE TABLE IF NOT EXISTS``
    with the full latest schema; new columns added to existing tables are
    introduced via ``ALTER TABLE`` guarded by a column-presence check. This
    keeps the local-first developer DB upgrading cleanly without a real
    migration framework.
    """
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
                metadata TEXT,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );

            CREATE TABLE IF NOT EXISTS pending_executions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                source_message_id TEXT,
                title TEXT NOT NULL,
                display_plan TEXT NOT NULL,
                task_card TEXT NOT NULL,
                status TEXT NOT NULL,
                run_id TEXT,
                revision_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                recovery_of TEXT,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );

            CREATE INDEX IF NOT EXISTS idx_conv_project
                ON conversations(project_id);
            CREATE INDEX IF NOT EXISTS idx_msg_conv
                ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_pending_conv
                ON pending_executions(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_pending_project
                ON pending_executions(project_id);
        """)
        # In-place migrations for existing DBs created before these columns
        # existed. PRAGMA table_info returns one row per column; we add only
        # what's missing.
        existing_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "metadata" not in existing_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN metadata TEXT")
        # Phase 11 — pending recovery plans carry the parent run id so the
        # confirm endpoint can thread recovery lineage (recovery_of /
        # recovered_by / inherited checkpoint) through dispatch.
        pending_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(pending_executions)").fetchall()
        }
        if "recovery_of" not in pending_cols:
            conn.execute("ALTER TABLE pending_executions ADD COLUMN recovery_of TEXT")


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


def add_message(
    conversation_id: str,
    role: str,
    content: str,
    metadata: dict | None = None,
) -> dict:
    msg_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    metadata_json = json.dumps(metadata) if metadata else None
    with get_db() as conn:
        conn.execute(
            "INSERT INTO messages (id, conversation_id, role, content, timestamp, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, conversation_id, role, content, now, metadata_json),
        )
    touch_conversation(conversation_id)
    return {
        "id": msg_id,
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "timestamp": now,
        "metadata": metadata or None,
    }


def _row_to_message(row: sqlite3.Row) -> dict:
    raw_meta = row["metadata"] if "metadata" in row.keys() else None
    parsed_meta = None
    if raw_meta:
        try:
            parsed_meta = json.loads(raw_meta)
        except (TypeError, ValueError):
            parsed_meta = None
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "role": row["role"],
        "content": row["content"],
        "timestamp": row["timestamp"],
        "metadata": parsed_meta,
    }


def list_messages(conversation_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, conversation_id, role, content, timestamp, metadata "
            "FROM messages WHERE conversation_id = ? ORDER BY timestamp ASC",
            (conversation_id,),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def get_message(message_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, conversation_id, role, content, timestamp, metadata "
            "FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    return _row_to_message(row) if row else None


def delete_conversation(conv_id: str):
    # Order matters: ``pending_executions`` and ``messages`` both have an
    # FK on ``conversations(id)`` with PRAGMA foreign_keys=ON, so the
    # parent row can't be removed until every child is gone. Skipping the
    # ``pending_executions`` clear was a real bug — any conversation that
    # had ever held a confirmable plan became permanently undeletable.
    with get_db() as conn:
        conn.execute("DELETE FROM pending_executions WHERE conversation_id = ?", (conv_id,))
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))


def delete_conversations_for_project(project_id: str):
    """Delete all conversations, messages, and pending executions for a project."""
    with get_db() as conn:
        # Clear pending executions first (FK -> conversations.id).
        conn.execute(
            "DELETE FROM pending_executions WHERE project_id = ?", (project_id,)
        )
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
        conn.execute(
            "UPDATE pending_executions SET project_id = ? WHERE project_id = ?",
            (new_project_id, old_project_id),
        )


# ---------- pending executions (Task 05.9.5) ----------
#
# A pending_execution is a confirmable plan produced by the LLM delegation
# judge. It lives in SQLite — not in run.json — because no run has been
# dispatched yet. It's just the *intent* to run: the rendered display plan
# the user reads, the full task card the Coding Agent will read once
# dispatched, and a status lifecycle:
#
#   pending     — created by the judge, waiting for user confirmation
#   dispatched  — user clicked OK; run_id is now populated
#   cancelled   — superseded or explicitly dropped (reserved for future use)
#
# Revisions update the same row in place — display_plan / task_card / title
# are overwritten and revision_count bumps. Status stays "pending" until
# the user confirms.


def create_pending_execution(
    *,
    project_id: str,
    conversation_id: str,
    source_message_id: str | None,
    title: str,
    display_plan: str,
    task_card: str,
    recovery_of: str | None = None,
) -> dict:
    pending_id = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO pending_executions "
            "(id, project_id, conversation_id, source_message_id, title, "
            " display_plan, task_card, status, run_id, revision_count, "
            " created_at, updated_at, recovery_of) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, 0, ?, ?, ?)",
            (
                pending_id, project_id, conversation_id, source_message_id,
                title, display_plan, task_card, now, now, recovery_of,
            ),
        )
    return get_pending_execution(pending_id)  # type: ignore[return-value]


def get_pending_execution(pending_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, project_id, conversation_id, source_message_id, title, "
            "       display_plan, task_card, status, run_id, revision_count, "
            "       created_at, updated_at, recovery_of "
            "FROM pending_executions WHERE id = ?",
            (pending_id,),
        ).fetchone()
    return dict(row) if row else None


def update_pending_execution_plan(
    pending_id: str,
    *,
    title: str,
    display_plan: str,
    task_card: str,
) -> bool:
    """Apply a revised plan to an existing pending execution.

    Returns False if the row doesn't exist or its status is not 'pending'.
    Revising a dispatched/cancelled plan is disallowed — the caller should
    create a fresh pending instead.
    """
    now = datetime.now().isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE pending_executions "
            "SET title = ?, display_plan = ?, task_card = ?, "
            "    revision_count = revision_count + 1, updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (title, display_plan, task_card, now, pending_id),
        )
        return cursor.rowcount > 0


def claim_pending_execution(pending_id: str) -> bool:
    """Atomically transition a pending row 'pending' -> 'dispatching'.

    Returns True for exactly ONE caller when several confirm requests race the
    same pending plan (a guarded ``UPDATE ... WHERE status='pending'`` — SQLite
    serializes the write, so the rowcount is 1 for the winner and 0 for every
    loser). Only the winner should call ``dispatch()``; the losers get a 409.
    This is the atomic guard that stops a double-click from launching two Coding
    Agent runs against the same live ``repo/`` tree.
    """
    now = datetime.now().isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE pending_executions "
            "SET status = 'dispatching', updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now, pending_id),
        )
        return cursor.rowcount > 0


def revert_pending_execution_to_pending(pending_id: str) -> bool:
    """Undo a claim (dispatching -> pending) when the dispatch itself failed, so
    the plan's 'OK, run this' button doesn't dead-end on a transient error."""
    now = datetime.now().isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE pending_executions "
            "SET status = 'pending', updated_at = ? "
            "WHERE id = ? AND status = 'dispatching'",
            (now, pending_id),
        )
        return cursor.rowcount > 0


def reconcile_stuck_pending_executions() -> int:
    """At startup, revert any pending row stranded in the intermediate
    'dispatching' state back to 'pending'.

    'dispatching' is a momentary in-process claim between
    ``claim_pending_execution`` and ``mark_pending_execution_dispatched``; if a
    row is still 'dispatching' at process start, the process that claimed it died
    mid-confirm. Reverting to 'pending' re-enables the confirmable plan so the
    'OK, run this' button never dead-ends. Safe against a crash that occurred
    *after* dispatch: that run is itself swept to ``failed`` by
    ``sweep_stuck_runs``, so re-confirming is a clean retry (never a duplicate
    live run). Returns the number of rows reverted."""
    now = datetime.now().isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE pending_executions "
            "SET status = 'pending', updated_at = ? "
            "WHERE status = 'dispatching'",
            (now,),
        )
        return cursor.rowcount


def mark_pending_execution_dispatched(pending_id: str, run_id: str) -> bool:
    now = datetime.now().isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE pending_executions "
            "SET status = 'dispatched', run_id = ?, updated_at = ? "
            # Accept either 'pending' (legacy direct path) or 'dispatching' (the
            # atomic-claim path) so the winner of a claim can finalize the row.
            "WHERE id = ? AND status IN ('pending', 'dispatching')",
            (run_id, now, pending_id),
        )
        return cursor.rowcount > 0
