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
