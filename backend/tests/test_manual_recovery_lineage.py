"""Tests for Phase 11 — manual recovery lineage through the confirm endpoint.

Standalone:  python tests/test_manual_recovery_lineage.py

Before Phase 11, a manually-confirmed recovery run (propose-recovery →
pending plan → "OK, run this") was dispatched as an ordinary run: no
``recovery_of`` link, no inherited checkpoint, and the parent's
``recovered_by`` never set. These tests drive the real FastAPI confirm
endpoint (dispatch stubbed) and assert the lineage now threads through —
plus the contract clamp and the one-recovery-per-parent 409.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402
import main  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution.models import (  # noqa: E402
    RecoveryAssessment,
    RunRecord,
    RunStatus,
    VisualReviewResult,
)


class _Env:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects_dir = root / "projects"
        self.execution_dir = root / "execution_workspaces"
        self.projects_dir.mkdir()
        self.execution_dir.mkdir()
        self._prev_projects = main.PROJECTS_DIR
        self._prev_exec = exec_manager._EXECUTION_ROOT
        self._prev_db = database.DB_PATH
        main.PROJECTS_DIR = self.projects_dir
        exec_manager._EXECUTION_ROOT = self.execution_dir
        database.DB_PATH = root / "agent_os.db"
        database.init_db()
        # Stub the background manager so no real Coding Agent run executes.
        self.dispatch_calls: list[dict] = []
        env = self

        class _FakeManager:
            def dispatch(self, project_id, task, **kw):
                env.dispatch_calls.append(
                    {"project_id": project_id, "task": task, **kw}
                )
                child = RunRecord(
                    run_id="20260707-000000-child001",
                    project_id=project_id,
                    task_title=task.title,
                    status=RunStatus.RUNNING,
                )
                run_store.init_run_dir(project_id, child.run_id)
                run_store.write_run_json(project_id, child.run_id, child)
                return child

        self._prev_mgr = main.get_default_manager
        main.get_default_manager = lambda: _FakeManager()  # type: ignore[assignment]
        self.client = TestClient(main.app)

    def cleanup(self) -> None:
        main.PROJECTS_DIR = self._prev_projects
        exec_manager._EXECUTION_ROOT = self._prev_exec
        main.get_default_manager = self._prev_mgr  # type: ignore[assignment]
        database.DB_PATH = self._prev_db
        self.tmp.cleanup()

    def make_project(self, pid: str) -> None:
        (self.projects_dir / pid).mkdir(parents=True, exist_ok=True)
        (self.projects_dir / pid / "PROJECT.md").write_text(f"# {pid}\n", encoding="utf-8")
        ws = self.execution_dir / pid
        (ws / "repo").mkdir(parents=True, exist_ok=True)
        (ws / "AGENT.md").write_text("# A\n", encoding="utf-8")
        (ws / "TASK.md").write_text("# T\n", encoding="utf-8")
        (ws / "runs").mkdir(exist_ok=True)
        (ws / "logs").mkdir(exist_ok=True)

    def seed_parent(self, pid: str, run_id: str, **over) -> RunRecord:
        base = dict(
            run_id=run_id,
            project_id=pid,
            task_title="Build dashboard",
            status=RunStatus.COMPLETED,
            visual_review=VisualReviewResult(
                enabled=True, status="failed", headline="Blank page"
            ),
            recovery_assessment=RecoveryAssessment(
                assessed=True, verdict="needs_recovery",
                recommended_action="repair",
                follow_up_task_card="Fix the blank dashboard route.",
                recovery_type="visual", classified_by="rules",
            ),
            pre_run_checkpoint="refs/agentos/ck1",
            base_commit="abc1234",
            checkpoint_tag="agentos/ck1",
            branch="main",
            orchestration_round=0,
        )
        base.update(over)
        rec = RunRecord(**base)
        run_store.init_run_dir(pid, run_id)
        run_store.write_run_json(pid, run_id, rec)
        return rec

    def make_recovery_pending(self, pid: str, parent_run_id: str) -> str:
        conv = database.create_conversation(pid, "recovery conv")
        row = database.create_pending_execution(
            project_id=pid,
            conversation_id=conv["id"],
            source_message_id=None,
            title="Fix the blank dashboard route",
            display_plan="plan",
            task_card="Fix the blank dashboard route.\n\n## Evidence from failed run\n- x",
            recovery_of=parent_run_id,
        )
        return row["id"]


PARENT = "20260707-000000-parent01"


def test_confirm_threads_recovery_lineage():
    env = _Env()
    try:
        env.make_project("demo")
        env.seed_parent("demo", PARENT)
        pid = env.make_recovery_pending("demo", PARENT)

        resp = env.client.post(
            f"/api/projects/demo/execution/pending/{pid}/confirm",
            json={"recovery_budget": 2},
        )
        assert resp.status_code == 200, resp.text
        assert len(env.dispatch_calls) == 1
        kw = env.dispatch_calls[0]
        assert kw["recovery_of"] == PARENT
        assert kw["orchestration_round"] == 1
        assert kw["inherit_checkpoint"]["ref"] == "refs/agentos/ck1"
        assert kw["inherit_checkpoint"]["branch"] == "main"
        # Visual contract clamps the child's onward budget to 0 even though
        # the user picked 2 — one bounded repair pass, then re-verify only.
        assert kw["recovery_budget"] == 0

        parent = RunRecord(**run_store.read_run_json("demo", PARENT))
        assert parent.recovered_by == "20260707-000000-child001"
        events = run_store.read_events("demo", PARENT)
        manual = [e for e in events if e.get("type") == "manual_recovery_dispatched"]
        assert len(manual) == 1
        assert manual[0]["child_run_id"] == "20260707-000000-child001"
        assert manual[0]["recovery_type"] == "visual"
    finally:
        env.cleanup()


def test_confirm_409_when_parent_already_recovered():
    env = _Env()
    try:
        env.make_project("demo")
        env.seed_parent("demo", PARENT, recovered_by="some-earlier-child")
        pid = env.make_recovery_pending("demo", PARENT)

        resp = env.client.post(
            f"/api/projects/demo/execution/pending/{pid}/confirm",
            json={"recovery_budget": 1},
        )
        assert resp.status_code == 409
        assert env.dispatch_calls == []
        # The claim was released so the row doesn't dead-end.
        row = database.get_pending_execution(pid)
        assert row["status"] == "pending"
    finally:
        env.cleanup()


def test_confirm_plain_plan_unchanged():
    """A non-recovery pending plan dispatches exactly as before Phase 11."""
    env = _Env()
    try:
        env.make_project("demo")
        conv = database.create_conversation("demo", "c")
        row = database.create_pending_execution(
            project_id="demo", conversation_id=conv["id"], source_message_id=None,
            title="Add feature", display_plan="p", task_card="Do it.",
        )
        resp = env.client.post(
            f"/api/projects/demo/execution/pending/{row['id']}/confirm",
            json={"recovery_budget": 2},
        )
        assert resp.status_code == 200, resp.text
        kw = env.dispatch_calls[0]
        assert kw.get("recovery_of") is None
        assert kw["recovery_budget"] == 2  # user clamp only, no contract clamp
        assert "orchestration_round" not in kw
    finally:
        env.cleanup()


def test_confirm_without_parent_checkpoint_lets_child_anchor_fresh():
    """A parent with no pre-run checkpoint must not suppress the child's own
    fresh checkpoint via an all-None inherit."""
    env = _Env()
    try:
        env.make_project("demo")
        env.seed_parent(
            "demo", PARENT,
            pre_run_checkpoint=None, base_commit=None,
            checkpoint_tag=None, branch=None,
        )
        pid = env.make_recovery_pending("demo", PARENT)
        resp = env.client.post(
            f"/api/projects/demo/execution/pending/{pid}/confirm",
            json={"recovery_budget": 1},
        )
        assert resp.status_code == 200, resp.text
        kw = env.dispatch_calls[0]
        assert kw["recovery_of"] == PARENT  # lineage still threads
        assert "inherit_checkpoint" not in kw  # fresh anchor allowed
    finally:
        env.cleanup()


def test_confirm_with_missing_parent_degrades_to_plain_dispatch():
    env = _Env()
    try:
        env.make_project("demo")
        pid = env.make_recovery_pending("demo", "20260707-000000-gone0000")
        resp = env.client.post(
            f"/api/projects/demo/execution/pending/{pid}/confirm",
            json={"recovery_budget": 1},
        )
        assert resp.status_code == 200, resp.text
        kw = env.dispatch_calls[0]
        assert kw.get("recovery_of") is None  # defensive degrade
        assert kw["recovery_budget"] == 1
    finally:
        env.cleanup()


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
