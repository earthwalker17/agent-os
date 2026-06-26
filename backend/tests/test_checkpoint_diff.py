"""Tests for Task 7.4 — pre-run checkpoint (dispatch) + post-run diff (finalize).

Coverage:
  - BackgroundRunManager.dispatch creates an out-of-branch checkpoint and stamps
    pre_run_checkpoint / base_commit / checkpoint_tag / branch on run.json, with
    a checkpoint_created event (runner stubbed so no LLM loop runs).
  - inherit_checkpoint reuses a parent's anchor and does NOT mint a new one
    (recovery-chain anchoring).
  - runner._capture_post_run_diff writes diff.patch, stamps diff_stat +
    head_commit, is idempotent, and no-ops without a checkpoint.

Run directly:
    python backend/tests/test_checkpoint_diff.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager  # noqa: E402
import execution.background as background  # noqa: E402
import execution.git_ops as git_ops  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution.models import RunRecord, RunStatus, TaskSpec  # noqa: E402
from execution.runner import CodingAgentRunner  # noqa: E402


class _TempLayout:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.execution_dir = Path(self.tmp.name) / "execution_workspaces"
        self.execution_dir.mkdir()
        self._prev = exec_manager._EXECUTION_ROOT
        exec_manager._EXECUTION_ROOT = self.execution_dir

    def cleanup(self) -> None:
        exec_manager._EXECUTION_ROOT = self._prev
        self.tmp.cleanup()

    def repo(self, project_id: str = "proj") -> Path:
        return self.execution_dir / project_id / "repo"


def _run(test_body):
    layout = _TempLayout()
    try:
        test_body(layout)
    finally:
        layout.cleanup()


class _StubRunner:
    """Stand-in for CodingAgentRunner so dispatch never runs the LLM loop."""

    def __init__(self, project_id, model=None):
        self.project_id = project_id

    def run_task(self, task, run_id=None, cancel_event=None):
        return None


def _with_stub_runner(fn):
    prev = background.CodingAgentRunner
    background.CodingAgentRunner = _StubRunner
    try:
        return fn()
    finally:
        background.CodingAgentRunner = prev


# ---------- dispatch checkpoint ----------


def test_dispatch_creates_checkpoint():
    def body(layout: _TempLayout):
        exec_manager.init_execution_workspace("proj", "Proj")

        def go():
            mgr = background.BackgroundRunManager(max_workers=1)
            rec = mgr.dispatch("proj", TaskSpec(title="t", task_card="do x"))
            mgr.shutdown(wait=True)
            return rec

        rec = _with_stub_runner(go)
        raw = run_store.read_run_json("proj", rec.run_id)
        assert raw["pre_run_checkpoint"]
        assert raw["base_commit"]
        assert raw["checkpoint_tag"].startswith("agentos-checkpoint-")
        assert raw["branch"] == "main"
        events = run_store.read_events("proj", rec.run_id)
        assert any(e.get("type") == "checkpoint_created" for e in events)

    _run(body)


def test_dispatch_inherits_checkpoint():
    def body(layout: _TempLayout):
        exec_manager.init_execution_workspace("proj", "Proj")

        def go():
            mgr = background.BackgroundRunManager(max_workers=1)
            rec = mgr.dispatch(
                "proj",
                TaskSpec(title="t", task_card="do x"),
                inherit_checkpoint={
                    "ref": "REF123",
                    "base": "BASE456",
                    "tag": "agentos-checkpoint-parent",
                    "branch": "feature/y",
                },
            )
            mgr.shutdown(wait=True)
            return rec

        rec = _with_stub_runner(go)
        raw = run_store.read_run_json("proj", rec.run_id)
        assert raw["pre_run_checkpoint"] == "REF123"
        assert raw["base_commit"] == "BASE456"
        assert raw["branch"] == "feature/y"
        # inherited: no new checkpoint minted
        events = run_store.read_events("proj", rec.run_id)
        assert not any(e.get("type") == "checkpoint_created" for e in events)

    _run(body)


# ---------- post-run diff ----------


def test_capture_post_run_diff():
    def body(layout: _TempLayout):
        exec_manager.init_execution_workspace("proj", "Proj")
        git_ops.ensure_repo("proj")
        ck = git_ops.create_checkpoint("proj", "run-1")
        run_store.init_run_dir("proj", "run-1")
        (layout.repo() / "feature.py").write_text("print('new')\n", encoding="utf-8")

        runner = CodingAgentRunner("proj")
        rec = RunRecord(
            run_id="run-1",
            project_id="proj",
            task_title="t",
            status=RunStatus.COMPLETED,
            pre_run_checkpoint=ck.ref,
            base_commit=ck.base_commit,
        )
        runner._capture_post_run_diff("run-1", rec)
        assert rec.diff_stat
        assert rec.head_commit
        patch = run_store.read_diff_patch("proj", "run-1")
        assert patch and "feature.py" in patch

        # idempotent — second call leaves diff_stat unchanged
        prev = rec.diff_stat
        runner._capture_post_run_diff("run-1", rec)
        assert rec.diff_stat == prev

    _run(body)


def test_capture_diff_noop_without_checkpoint():
    def body(layout: _TempLayout):
        exec_manager.init_execution_workspace("proj", "Proj")
        runner = CodingAgentRunner("proj")
        rec = RunRecord(
            run_id="run-x", project_id="proj", task_title="t", status=RunStatus.COMPLETED
        )
        runner._capture_post_run_diff("run-x", rec)
        assert rec.diff_stat is None

    _run(body)


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
