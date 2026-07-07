"""DB-level smoke test for the Task 05.9.5 pending_executions schema.

Uses a temporary on-disk SQLite file (the module's DB_PATH is monkey-patched
before init_db()) so the developer's real agent_os.db is untouched. Covers:

  - init_db creates pending_executions + adds messages.metadata column
  - create_pending_execution → get_pending_execution round-trip
  - update_pending_execution_plan increments revision_count and refuses
    once status moves away from 'pending'
  - mark_pending_execution_dispatched stamps run_id + flips status

Run directly:
    python backend/tests/test_pending_execution_db.py
"""

from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _fresh_db_module():
    """Reload database.py against a temp DB_PATH so each test is isolated."""
    import database  # noqa: WPS433

    tmpdir = tempfile.mkdtemp(prefix="agentos-pe-")
    database.DB_PATH = Path(tmpdir) / "agent_os.db"
    importlib.reload(database)
    database.DB_PATH = Path(tmpdir) / "agent_os.db"
    database.init_db()
    return database


def test_pending_lifecycle():
    db = _fresh_db_module()

    conv = db.create_conversation("agent-os", "test conv")
    user_msg = db.add_message(conv["id"], "user", "add /healthcheck please")

    pending = db.create_pending_execution(
        project_id="agent-os",
        conversation_id=conv["id"],
        source_message_id=user_msg["id"],
        title="Add /healthcheck",
        display_plan="I'll add /healthcheck.",
        task_card="Add a /healthcheck endpoint to backend/main.py.",
    )
    assert pending["status"] == "pending"
    assert pending["revision_count"] == 0
    assert pending["run_id"] is None

    fetched = db.get_pending_execution(pending["id"])
    assert fetched is not None
    assert fetched["title"] == "Add /healthcheck"

    # Revise — bumps revision_count, keeps status pending.
    ok = db.update_pending_execution_plan(
        pending["id"],
        title="Add typed /healthcheck",
        display_plan="Now with a Pydantic model.",
        task_card="Add a typed /healthcheck.",
    )
    assert ok is True
    revised = db.get_pending_execution(pending["id"])
    assert revised["revision_count"] == 1
    assert revised["title"] == "Add typed /healthcheck"
    assert revised["status"] == "pending"

    # Dispatch — flips status, stores run id.
    marked = db.mark_pending_execution_dispatched(pending["id"], "20260515-100000-deadbeef")
    assert marked is True
    after = db.get_pending_execution(pending["id"])
    assert after["status"] == "dispatched"
    assert after["run_id"] == "20260515-100000-deadbeef"

    # Re-revising a dispatched plan must fail.
    ok2 = db.update_pending_execution_plan(
        pending["id"],
        title="should not stick",
        display_plan="x",
        task_card="x",
    )
    assert ok2 is False
    again = db.get_pending_execution(pending["id"])
    assert again["title"] == "Add typed /healthcheck"  # unchanged

    # Re-dispatching a dispatched plan must fail.
    marked2 = db.mark_pending_execution_dispatched(pending["id"], "20260515-100001-cafebabe")
    assert marked2 is False


def test_claim_pending_is_atomic_single_winner():
    """T1.5: claim_pending_execution transitions pending->dispatching for exactly
    one caller; a second claim (concurrent confirm) loses. The winner can then
    finalize to dispatched; a revert restores pending on dispatch failure."""
    db = _fresh_db_module()
    conv = db.create_conversation("agent-os", "c")
    pending = db.create_pending_execution(
        project_id="agent-os", conversation_id=conv["id"], source_message_id=None,
        title="t", display_plan="p", task_card="c",
    )
    pid = pending["id"]

    # First claim wins; the row is now 'dispatching'.
    assert db.claim_pending_execution(pid) is True
    assert db.get_pending_execution(pid)["status"] == "dispatching"
    # A second confirm racing the same plan loses the claim (no second dispatch).
    assert db.claim_pending_execution(pid) is False

    # The winner finalizes the claimed row to dispatched (accepts 'dispatching').
    assert db.mark_pending_execution_dispatched(pid, "20260701-000000-run00001") is True
    assert db.get_pending_execution(pid)["status"] == "dispatched"
    assert db.get_pending_execution(pid)["run_id"] == "20260701-000000-run00001"


def test_revert_pending_releases_claim():
    """A claimed plan whose dispatch failed reverts to pending so the button
    doesn't dead-end."""
    db = _fresh_db_module()
    conv = db.create_conversation("agent-os", "c")
    pending = db.create_pending_execution(
        project_id="agent-os", conversation_id=conv["id"], source_message_id=None,
        title="t", display_plan="p", task_card="c",
    )
    pid = pending["id"]
    assert db.claim_pending_execution(pid) is True
    assert db.revert_pending_execution_to_pending(pid) is True
    assert db.get_pending_execution(pid)["status"] == "pending"
    # Can be claimed again after the revert.
    assert db.claim_pending_execution(pid) is True
    # Revert only applies to a 'dispatching' row — a dispatched one is untouched.
    db.mark_pending_execution_dispatched(pid, "run-x")
    assert db.revert_pending_execution_to_pending(pid) is False


def test_reconcile_reverts_stranded_dispatching_rows():
    """A crash between claim and mark leaves a row stuck in 'dispatching' — the
    startup reconciler must revert it to 'pending' so the OK button doesn't
    dead-end (any dispatched run is separately swept to failed)."""
    db = _fresh_db_module()
    conv = db.create_conversation("agent-os", "c")
    p1 = db.create_pending_execution(
        project_id="agent-os", conversation_id=conv["id"], source_message_id=None,
        title="t1", display_plan="p", task_card="c")["id"]
    p2 = db.create_pending_execution(
        project_id="agent-os", conversation_id=conv["id"], source_message_id=None,
        title="t2", display_plan="p", task_card="c")["id"]
    p3 = db.create_pending_execution(
        project_id="agent-os", conversation_id=conv["id"], source_message_id=None,
        title="t3", display_plan="p", task_card="c")["id"]
    # p1 stranded mid-confirm; p2 pending; p3 already dispatched.
    db.claim_pending_execution(p1)  # -> 'dispatching'
    db.claim_pending_execution(p3); db.mark_pending_execution_dispatched(p3, "run-x")

    n = db.reconcile_stuck_pending_executions()
    assert n == 1  # only p1 was 'dispatching'
    assert db.get_pending_execution(p1)["status"] == "pending"   # reverted -> confirmable
    assert db.get_pending_execution(p2)["status"] == "pending"   # untouched
    assert db.get_pending_execution(p3)["status"] == "dispatched"  # untouched
    # The reverted plan can be claimed + confirmed again (clean retry).
    assert db.claim_pending_execution(p1) is True


def test_message_metadata_roundtrip():
    db = _fresh_db_module()
    conv = db.create_conversation("agent-os", "test conv")
    msg = db.add_message(
        conv["id"], "assistant", "hello",
        metadata={"pending_execution_id": "abc123"},
    )
    assert msg["metadata"] == {"pending_execution_id": "abc123"}

    msgs = db.list_messages(conv["id"])
    assert len(msgs) == 1
    assert msgs[0]["metadata"] == {"pending_execution_id": "abc123"}

    fetched = db.get_message(msg["id"])
    assert fetched is not None
    assert fetched["metadata"] == {"pending_execution_id": "abc123"}


def test_message_without_metadata_returns_none_metadata():
    db = _fresh_db_module()
    conv = db.create_conversation("agent-os", "test conv")
    db.add_message(conv["id"], "user", "no metadata here")
    msgs = db.list_messages(conv["id"])
    assert msgs[0]["metadata"] is None


def test_get_pending_returns_none_for_unknown_id():
    db = _fresh_db_module()
    assert db.get_pending_execution("does-not-exist") is None


def test_delete_conversation_clears_pending_executions():
    """Regression: ``pending_executions`` has an FK on ``conversations.id``
    with ``PRAGMA foreign_keys=ON``. ``delete_conversation`` must clear
    the child rows first, otherwise the parent DELETE raises
    ``IntegrityError`` and any conversation that ever held a confirmable
    plan becomes permanently undeletable from the UI.
    """
    db = _fresh_db_module()
    conv = db.create_conversation("agent-os", "conv with pending")
    db.create_pending_execution(
        project_id="agent-os",
        conversation_id=conv["id"],
        source_message_id=None,
        title="t",
        display_plan="p",
        task_card="c",
    )
    # Sanity: pending row exists.
    with db.get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM pending_executions WHERE conversation_id = ?",
            (conv["id"],),
        ).fetchone()[0]
    assert n == 1

    db.delete_conversation(conv["id"])

    with db.get_db() as conn:
        n_pending = conn.execute(
            "SELECT COUNT(*) FROM pending_executions WHERE conversation_id = ?",
            (conv["id"],),
        ).fetchone()[0]
        n_conv = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE id = ?", (conv["id"],)
        ).fetchone()[0]
    assert n_pending == 0
    assert n_conv == 0


def test_delete_conversations_for_project_clears_pending_executions():
    """Same regression at the project-cascade level: a project with any
    pending plan must be fully deletable, including the FK-referencing
    ``pending_executions`` rows.
    """
    db = _fresh_db_module()
    conv = db.create_conversation("agent-os", "conv with pending")
    db.create_pending_execution(
        project_id="agent-os",
        conversation_id=conv["id"],
        source_message_id=None,
        title="t",
        display_plan="p",
        task_card="c",
    )
    db.delete_conversations_for_project("agent-os")

    with db.get_db() as conn:
        n_pending = conn.execute(
            "SELECT COUNT(*) FROM pending_executions WHERE project_id = ?",
            ("agent-os",),
        ).fetchone()[0]
        n_conv = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE project_id = ?",
            ("agent-os",),
        ).fetchone()[0]
    assert n_pending == 0
    assert n_conv == 0


def test_recovery_of_roundtrip_and_default():
    """Phase 11 — a pending recovery plan stores its parent run id; ordinary
    plans read back None (and the serializer carries the field to the API)."""
    db = _fresh_db_module()
    conv = db.create_conversation("agent-os", "c")
    plain = db.create_pending_execution(
        project_id="agent-os", conversation_id=conv["id"], source_message_id=None,
        title="t", display_plan="p", task_card="c",
    )
    assert plain["recovery_of"] is None

    rec = db.create_pending_execution(
        project_id="agent-os", conversation_id=conv["id"], source_message_id=None,
        title="fix", display_plan="p", task_card="c",
        recovery_of="20260101-000000-parent00",
    )
    assert rec["recovery_of"] == "20260101-000000-parent00"
    assert db.get_pending_execution(rec["id"])["recovery_of"] == "20260101-000000-parent00"

    from execution.pending_execution import serialize_pending
    view = serialize_pending(db.get_pending_execution(rec["id"]))
    assert view.recovery_of == "20260101-000000-parent00"
    assert view.to_dict()["recovery_of"] == "20260101-000000-parent00"


def test_recovery_of_migration_on_legacy_db():
    """Phase 11 — init_db adds the recovery_of column to a pre-existing
    pending_executions table (the messages.metadata migration pattern)."""
    import sqlite3

    import database

    tmpdir = tempfile.mkdtemp(prefix="agentos-mig-")
    legacy_path = Path(tmpdir) / "agent_os.db"
    conn = sqlite3.connect(legacy_path)
    conn.executescript(
        """
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, timestamp TEXT NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        );
        CREATE TABLE pending_executions (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            source_message_id TEXT, title TEXT NOT NULL, display_plan TEXT NOT NULL,
            task_card TEXT NOT NULL, status TEXT NOT NULL, run_id TEXT,
            revision_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        );
        INSERT INTO pending_executions VALUES
            ('legacy1', 'agent-os', 'conv1', NULL, 't', 'p', 'c', 'pending', NULL, 0,
             '2026-01-01', '2026-01-01');
        """
    )
    conn.commit()
    conn.close()

    database.DB_PATH = legacy_path
    importlib.reload(database)
    database.DB_PATH = legacy_path
    database.init_db()

    row = database.get_pending_execution("legacy1")
    assert row is not None
    assert row["recovery_of"] is None  # legacy row reads back cleanly


def _run_all() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed: list[str] = []
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed.append(fn.__name__)
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append(fn.__name__)
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print()
    if failed:
        print(f"{len(failed)} test(s) failed: {', '.join(failed)}")
        return 1
    print(f"All {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
