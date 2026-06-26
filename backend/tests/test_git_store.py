"""Tests for Task 7.3 — RunRecord/ResultSummary Git linkage + run_store diff
artifact + result.md Project Ops section.

Coverage:
  - old run.json (no git keys) round-trips into RunRecord with defaulted fields.
  - new git fields serialize and survive a model_dump_json round-trip.
  - write/read_diff_patch round-trip + size bound.
  - render_result_md: non-git run stays free of a Project Ops section; a run
    with git linkage renders branch/commit/PR metadata (no raw diff/secret).

Run directly:
    python backend/tests/test_git_store.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution.models import ResultSummary, RunRecord, RunStatus  # noqa: E402


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


def _run(test_body):
    layout = _TempLayout()
    try:
        test_body(layout)
    finally:
        layout.cleanup()


# ---------- round-trip ----------


def test_old_record_round_trips():
    old = {
        "run_id": "r1",
        "project_id": "p",
        "task_title": "t",
        "status": "completed",
    }
    rec = RunRecord(**old)
    assert rec.pre_run_checkpoint is None
    assert rec.commit_sha is None
    assert rec.pushed is False
    assert rec.pr_number is None
    assert rec.git_state is None


def test_new_git_fields_serialize():
    rec = RunRecord(
        run_id="r1",
        project_id="p",
        task_title="t",
        status=RunStatus.COMPLETED,
        pre_run_checkpoint="abc123",
        base_commit="base456",
        branch="feature/x",
        commit_sha="def789",
        pushed=True,
        pr_url="https://github.com/o/r/pull/3",
        pr_number=3,
        diff_stat="2 files changed, 10 insertions(+)",
        git_state=None,
    )
    blob = rec.model_dump_json()
    rt = RunRecord(**json.loads(blob))
    assert rt.commit_sha == "def789"
    assert rt.pr_number == 3
    assert rt.pushed is True


def test_result_summary_git_fields():
    rs = ResultSummary(run_id="r", status="completed")
    assert rs.commit_sha is None and rs.branch is None and rs.pr_url is None


# ---------- diff artifact ----------


def test_diff_patch_round_trip():
    def body(_):
        run_store.init_run_dir("p", "r1")
        run_store.write_diff_patch("p", "r1", "diff --git a/x b/x\n+hello\n")
        got = run_store.read_diff_patch("p", "r1")
        assert got and "hello" in got
        assert run_store.read_diff_patch("p", "missing") is None

    _run(body)


def test_diff_patch_bounded():
    def body(_):
        run_store.init_run_dir("p", "r2")
        run_store.write_diff_patch("p", "r2", "x" * (run_store._DIFF_PATCH_MAX_CHARS + 5000))
        got = run_store.read_diff_patch("p", "r2")
        assert "truncated" in got
        assert len(got) <= run_store._DIFF_PATCH_MAX_CHARS + 100

    _run(body)


# ---------- result.md rendering ----------


def test_result_md_no_git_section_for_plain_run():
    rec = RunRecord(run_id="r", project_id="p", task_title="t", status=RunStatus.COMPLETED)
    md = run_store.render_result_md(rec, "did stuff")
    assert "## Project Ops" not in md
    assert "## Status" in md and "## Notes for Main Agent" in md


def test_result_md_renders_git_section():
    rec = RunRecord(
        run_id="r",
        project_id="p",
        task_title="t",
        status=RunStatus.COMPLETED,
        branch="feature/x",
        base_commit="base456789abc",
        pre_run_checkpoint="ckpt123456789",
        checkpoint_tag="agentos-checkpoint-r",
        diff_stat="2 files changed",
        commit_sha="commitabc123456",
        pushed=True,
        pr_url="https://github.com/o/r/pull/7",
        pr_number=7,
    )
    md = run_store.render_result_md(rec, "did stuff")
    assert "## Project Ops" in md
    assert "feature/x" in md
    assert "pull request (#7)" in md
    assert "https://github.com/o/r/pull/7" in md
    # short shas only, no raw diff text
    assert "diff --git" not in md


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
