"""Tests for Task 7.9 — Git state surfaced to the Main Agent (summary-only).

Coverage:
  - _latest_git_state_context: '' for GENERAL / no git activity; compact
    branch/commit/PR/diff-stat for the newest run with Git state; never the
    raw diff.
  - _mode_guidance_section folds the Git block into @review / @debug only.

Run directly:
    python backend/tests/test_git_context.py
"""

from __future__ import annotations

import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager  # noqa: E402
import execution.run_store as run_store  # noqa: E402
import orchestrator  # noqa: E402
from execution.models import RunRecord, RunStatus  # noqa: E402


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

    def add_run(self, pid, run_id, **fields):
        run_store.init_run_dir(pid, run_id)
        rec = RunRecord(run_id=run_id, project_id=pid, task_title="t", status=RunStatus.COMPLETED, **fields)
        run_store.write_run_json(pid, run_id, rec)


def _run(test_body):
    layout = _TempLayout()
    try:
        test_body(layout)
    finally:
        layout.cleanup()


def test_no_git_activity_returns_empty():
    def body(layout):
        layout.add_run("p", "run-1")  # plain run, no git fields
        assert orchestrator._latest_git_state_context("p") == ""

    _run(body)


def test_general_returns_empty():
    assert orchestrator._latest_git_state_context(orchestrator.GENERAL_PROJECT_ID) == ""


def test_compact_git_state():
    def body(layout):
        layout.add_run(
            "p", "20240101-000001-aaaaaaaa",
            branch="feature/x", commit_sha="abcdef1234567890", pushed=True,
            pr_url="https://github.com/o/r/pull/5", pr_number=5,
            diff_stat="3 files changed, 20 insertions(+)",
        )
        out = orchestrator._latest_git_state_context("p")
        assert "feature/x" in out
        assert "abcdef123456" in out  # short sha
        assert "pull/5" in out
        assert "3 files changed" in out
        # never the raw diff
        assert "diff --git" not in out

    _run(body)


def test_newest_run_with_git_wins():
    def body(layout):
        layout.add_run("p", "20240101-000001-aaaaaaaa", commit_sha="oldoldoldold")
        layout.add_run("p", "20240202-000002-bbbbbbbb", commit_sha="newnewnewnew", branch="main")
        out = orchestrator._latest_git_state_context("p")
        assert "newnewnewnew" in out and "oldoldold" not in out

    _run(body)


def test_mode_guidance_includes_git_for_review_and_debug():
    def body(layout):
        layout.add_run("p", "run-1", commit_sha="abc123def456", branch="main")
        ctx = SimpleNamespace(project_id="p")
        review = orchestrator._mode_guidance_section(ctx, "review")
        assert "Project Ops (Git/GitHub) state" in review
        assert "abc123def456" in review
        # plan mode does NOT fold in git state
        plan = orchestrator._mode_guidance_section(ctx, "plan")
        assert "Project Ops (Git/GitHub) state" not in plan

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
