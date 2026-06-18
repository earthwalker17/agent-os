"""Tests for the autonomous-build hardening pass (the Aegis Launch Control fix).

These guard the specific failure modes the Aegis build hit, at the Agent OS
level (not by patching the generated app):

  PLANNER — progress-aware dependency skip:
    A dependent is skipped only when NONE of its dependencies produced usable
    output. A dependency that FAILED but left files on disk (a scaffold that ran
    out of steps) no longer cascades its dependents to ``skipped``.

  RUNNER — productive continuation:
    A multi-task unit that exhausts its step budget WITHOUT finalizing but is
    still writing files gets one bounded continuation so it can finish, instead
    of dying one step short (the scaffold-exhaustion failure). A unit that
    stops producing files gets no extension.

  RUNNER — failed-with-files dependency runs its dependent:
    Mirrors run 1: t1 fails but wrote files; t2 (depends on t1) RUNS instead of
    being skipped, and the run lands ``partial`` rather than collapsing.

  RUNNER — repair on a partial run:
    A multi-task run that lands ``partial`` (one task failed) but has a fixable
    build error still gets the bounded repair pass — the TS-export-conflict
    class of failure the original retry left unrepaired.

Run directly:
    python backend/tests/test_autonomy_hardening.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager  # noqa: E402
import execution.memory_reconciliation as mr  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution.models import ExecutionTask, TaskSpec, TaskStatus  # noqa: E402
from execution.planner import (  # noqa: E402
    degraded_dependencies,
    dependency_failed,
)
from execution.runner import CodingAgentRunner, MAX_TASK_STEPS  # noqa: E402


# ---------- planner: progress-aware dependency skip ----------


def _et(tid, title="T", status=TaskStatus.PENDING, deps=None, files=None) -> ExecutionTask:
    return ExecutionTask(
        id=tid,
        title=title,
        status=status,
        depends_on=deps or [],
        files_changed=files or [],
    )


def test_failed_dep_with_files_does_not_block():
    t1 = _et("t1", "scaffold", status=TaskStatus.FAILED, files=["package.json"])
    t2 = _et("t2", "build on it", deps=["t1"])
    by_id = {"t1": t1, "t2": t2}
    assert dependency_failed(t2, by_id) is None  # proceeds — t1 left files
    notes = degraded_dependencies(t2, by_id)
    assert any("t1" in n for n in notes)
    assert any("incomplete" in n for n in notes)


def test_failed_dep_without_files_still_blocks():
    t1 = _et("t1", "base", status=TaskStatus.FAILED, files=[])
    t2 = _et("t2", "next", deps=["t1"])
    by_id = {"t1": t1, "t2": t2}
    assert dependency_failed(t2, by_id) is not None  # nothing usable upstream


def test_skipped_dep_blocks():
    t1 = _et("t1", "x", status=TaskStatus.SKIPPED)
    t2 = _et("t2", "y", deps=["t1"])
    assert dependency_failed(t2, {"t1": t1, "t2": t2}) is not None


def test_pending_deps_do_not_block_cycle_remainder():
    # topological_order is cycle-safe and still RUNS cycle leftovers. A task
    # whose deps are all still PENDING/RUNNING (the cycle-remainder case) must
    # not be skipped — only resolved (terminal) deps can block.
    t1 = _et("t1", "a", status=TaskStatus.PENDING, deps=["t2"])
    t2 = _et("t2", "b", status=TaskStatus.PENDING, deps=["t1"])
    by_id = {"t1": t1, "t2": t2}
    assert dependency_failed(t1, by_id) is None
    # A completed dep alongside a pending one still proceeds.
    t3 = _et("t3", "c", status=TaskStatus.COMPLETED, files=["x.ts"])
    t4 = _et("t4", "d", status=TaskStatus.RUNNING)
    t5 = _et("t5", "e", deps=["t3", "t4"])
    assert dependency_failed(t5, {"t3": t3, "t4": t4, "t5": t5}) is None


def test_one_good_dep_lets_task_proceed_with_note():
    t1 = _et("t1", "types", status=TaskStatus.COMPLETED, files=["types.ts"])
    t2 = _et("t2", "data", status=TaskStatus.FAILED, files=[])  # failed, empty
    t3 = _et("t3", "view", deps=["t1", "t2"])
    by_id = {"t1": t1, "t2": t2, "t3": t3}
    assert dependency_failed(t3, by_id) is None  # t1 completed -> proceed
    notes = degraded_dependencies(t3, by_id)
    assert any("t2" in n for n in notes)
    assert not any("t1" in n for n in notes)  # completed deps aren't "degraded"


# ---------- runner harness (mirrors test_runner_planning) ----------


class _TempLayout:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.execution_dir = root / "execution_workspaces"
        self.projects_dir = root / "projects"
        self.execution_dir.mkdir()
        self.projects_dir.mkdir()
        self._prev_execution = exec_manager._EXECUTION_ROOT
        self._prev_projects = mr._PROJECTS_DIR
        exec_manager._EXECUTION_ROOT = self.execution_dir
        mr._PROJECTS_DIR = self.projects_dir

    def cleanup(self) -> None:
        exec_manager._EXECUTION_ROOT = self._prev_execution
        mr._PROJECTS_DIR = self._prev_projects
        self.tmp.cleanup()

    def init_workspace(self, project_id: str, *, task_md_body: str = "# TASK\n") -> Path:
        ws_dir = self.execution_dir / project_id
        repo_dir = ws_dir / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "AGENT.md").write_text("# AGENT\n", encoding="utf-8")
        (ws_dir / "TASK.md").write_text(task_md_body, encoding="utf-8")
        (ws_dir / "runs").mkdir(exist_ok=True)
        (ws_dir / "logs").mkdir(exist_ok=True)
        return repo_dir

    def make_project(self, project_id: str) -> None:
        path = self.projects_dir / project_id
        path.mkdir(parents=True, exist_ok=True)
        for name in ("PROJECT.md", "STATUS.md", "TASK_QUEUE.md", "DECISIONS.md", "RESEARCH.md"):
            (path / name).write_text("", encoding="utf-8")


def _run(test_body):
    layout = _TempLayout()
    try:
        test_body(layout)
    finally:
        layout.cleanup()


def _stub_llm_caller(responses: list[str]):
    seq = list(responses)

    def caller(*_args, **_kwargs) -> str:
        if not seq:
            raise AssertionError("LLM caller ran out of stub responses")
        return seq.pop(0)

    return caller


def _patch_llm(monkey: dict[str, Any], caller):
    import llm

    monkey["llm_chat"] = llm.chat
    llm.chat = caller  # type: ignore[assignment]


def _unpatch_llm(monkey: dict[str, Any]):
    import llm

    if "llm_chat" in monkey:
        llm.chat = monkey["llm_chat"]


def _tool_call(tool_name: str, **arguments) -> str:
    return json.dumps(
        {"action": "tool_call", "tool_name": tool_name, "arguments": arguments, "reason": "x"}
    )


def _final(status: str = "completed", **over) -> str:
    body = {
        "action": "final",
        "status": status,
        "summary": "did the thing",
        "files_changed": [],
        "commands_run": [],
        "blockers": [],
        "task_md_update": "",
    }
    body.update(over)
    return json.dumps(body)


def _ptask(tid, title, description="d", depends_on=None) -> dict:
    return {"id": tid, "title": title, "description": description, "depends_on": depends_on or []}


def _plan(tasks, goal="goal") -> str:
    return json.dumps({"action": "plan", "goal": goal, "analysis": "a", "risks": [], "tasks": tasks})


_COMPLEX_CARD = (
    "Build the feature:\n- scaffold the project\n- add data models\n- wire it together"
)


# ---------- runner: productive continuation ----------


def test_productive_unit_gets_continuation_and_finishes():
    """A heavy unit that writes files past its budget gets one continuation and
    finishes — the scaffold-exhaustion fix."""

    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        # t1 writes MAX_TASK_STEPS files (exhausting the budget) then, after the
        # continuation nudge, finalizes. t2 is trivial.
        t1_writes = [
            _tool_call("write_file", path=f"src/f{i}.txt", content=f"x{i}")
            for i in range(MAX_TASK_STEPS)
        ]
        responses = (
            [_plan([_ptask("t1", "scaffold"), _ptask("t2", "wire")])]
            + t1_writes
            + [_final("completed", summary="scaffold done")]  # t1 finishes on continuation
            + [_final("completed", summary="wired")]  # t2
        )
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card=_COMPLEX_CARD, created_by="test")
            )
        finally:
            _unpatch_llm(monkey)

        by_id = {t.id: t for t in summary.plan.tasks}
        assert by_id["t1"].status == TaskStatus.COMPLETED, by_id["t1"].status
        assert by_id["t2"].status == TaskStatus.COMPLETED
        assert summary.status == "completed"
        # A continuation event was emitted for the heavy task.
        events = run_store.read_events("p", summary.run_id)
        assert any(e.get("type") == "task_continued" for e in events)

    _run(body)


def test_overwriting_unit_gets_continuation():
    """A unit that OVERWRITES files already written by an earlier task (a
    polish/refactor pass) makes real progress without adding new unique paths.
    The continuation must still fire — progress is measured in write ops, not
    unique file paths. Regression for the v5 t7 'polish' task that exhausted
    its budget overwriting components and was wrongly denied a continuation."""

    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        # t1 writes shared.tsx (now in observed paths). t2 overwrites shared.tsx
        # MAX_TASK_STEPS times (no NEW path) then, after the continuation,
        # finalizes.
        t2_overwrites = [
            _tool_call("write_file", path="src/shared.tsx", content=f"// v{i}\n")
            for i in range(MAX_TASK_STEPS)
        ]
        responses = (
            [_plan([_ptask("t1", "create shared"), _ptask("t2", "polish shared")])]
            + [_tool_call("write_file", path="src/shared.tsx", content="// v0\n"), _final("completed")]
            + t2_overwrites
            + [_final("completed", summary="polished")]
        )
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card=_COMPLEX_CARD, created_by="test")
            )
        finally:
            _unpatch_llm(monkey)

        by_id = {t.id: t for t in summary.plan.tasks}
        assert by_id["t2"].status == TaskStatus.COMPLETED, by_id["t2"].status
        events = run_store.read_events("p", summary.run_id)
        assert any(e.get("type") == "task_continued" for e in events)

    _run(body)


def test_nonproductive_unit_is_not_continued():
    """A unit that exhausts its budget WITHOUT writing files gets no
    continuation (so we never extend a spinning loop)."""

    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        # t1 does only failing reads (no observed writes) and never finalizes;
        # t2 depends on t1 so it is skipped once t1 fails with no files.
        t1_reads = [
            _tool_call("read_file", path="does_not_exist.txt") for _ in range(MAX_TASK_STEPS)
        ]
        responses = [
            _plan([_ptask("t1", "spin"), _ptask("t2", "after", depends_on=["t1"])])
        ] + t1_reads
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card=_COMPLEX_CARD, created_by="test")
            )
        finally:
            _unpatch_llm(monkey)

        by_id = {t.id: t for t in summary.plan.tasks}
        assert by_id["t1"].status == TaskStatus.FAILED
        assert by_id["t2"].status == TaskStatus.SKIPPED  # empty dep still cascades
        events = run_store.read_events("p", summary.run_id)
        assert not any(e.get("type") == "task_continued" for e in events)

    _run(body)


# ---------- runner: failed-with-files dependency runs its dependent ----------


def test_failed_with_files_dependency_runs_dependent():
    """Mirrors Aegis run 1: t1 fails but wrote files; t2 (depends on t1) RUNS
    instead of being skipped, and the run is partial, not a total collapse."""

    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        responses = [
            _plan([_ptask("t1", "scaffold"), _ptask("t2", "build on it", depends_on=["t1"])]),
            # t1: write a file, then declare failure (incomplete but produced output).
            _tool_call("write_file", path="package.json", content="{}"),
            _final("failed", summary="ran out before finishing", blockers=["incomplete"]),
            # t2 must still run (gets a degraded-dependency note) and completes.
            _tool_call("write_file", path="src/app.tsx", content="export const A = 1\n"),
            _final("completed", summary="built on the scaffold"),
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card=_COMPLEX_CARD, created_by="test")
            )
        finally:
            _unpatch_llm(monkey)

        by_id = {t.id: t for t in summary.plan.tasks}
        assert by_id["t1"].status == TaskStatus.FAILED
        assert "package.json" in by_id["t1"].files_changed
        # The key assertion: t2 RAN (not skipped) because t1 left files on disk.
        assert by_id["t2"].status == TaskStatus.COMPLETED, by_id["t2"].status
        assert summary.status == "partial"
        events = run_store.read_events("p", summary.run_id)
        assert any(
            e.get("type") == "task_started" and e.get("task_id") == "t2" for e in events
        )

    _run(body)


# ---------- runner: repair on a partial run ----------


def test_partial_run_with_build_error_is_repaired():
    """A multi-task run that lands `partial` (one task failed) but carries a
    fixable verification failure still gets the bounded repair pass."""

    def body(layout: _TempLayout):
        layout.make_project("p")
        # Manual verification checks marker.txt == "good".
        task_md = (
            "# Task\n\n## Verification\n\n"
            "```bash\n"
            "python -c \"import sys; sys.exit(0 if open('marker.txt').read().strip()=='good' else 1)\"\n"
            "```\n"
        )
        layout.init_workspace("p", task_md_body=task_md)
        responses = [
            _plan([_ptask("t1", "write marker"), _ptask("t2", "other")]),
            # t1 completes but writes a "bad" marker -> verification will fail.
            _tool_call("write_file", path="marker.txt", content="bad"),
            _final("completed", summary="marker written", files_changed=["marker.txt"]),
            # t2 fails -> run aggregates to `partial`.
            _final("failed", summary="could not do it", blockers=["nope"]),
            # Repair pass (only runs because the gate now includes `partial`):
            _tool_call("write_file", path="marker.txt", content="good"),
            _final("completed", summary="fixed marker"),
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card=_COMPLEX_CARD, created_by="test")
            )
        finally:
            _unpatch_llm(monkey)

        # The run stays `partial` (t2 failed), but the build error was repaired.
        assert summary.status == "partial"
        assert summary.verification is not None
        assert summary.verification.status == "passed", summary.verification.status
        assert summary.verification.repair_attempts == 1

    _run(body)


def test_iterative_repair_converges_over_two_passes():
    """A build error that takes two repair passes to clear (the first pass fixes
    some files, the re-verify reveals a straggler) still ends green — the
    single-pass repair would have left it `partial`."""

    def body(layout: _TempLayout):
        layout.make_project("p")
        # Manual verification: both markers must read "good".
        task_md = (
            "# Task\n\n## Verification\n\n"
            "```bash\n"
            "python -c \"import sys; a=open('a.txt').read().strip(); b=open('b.txt').read().strip(); "
            "sys.exit(0 if a=='good' and b=='good' else 1)\"\n"
            "```\n"
        )
        layout.init_workspace("p", task_md_body=task_md)
        responses = [
            # Build: write both markers bad -> verification fails.
            _tool_call("write_file", path="a.txt", content="bad"),
            _tool_call("write_file", path="b.txt", content="bad"),
            _final("completed", files_changed=["a.txt", "b.txt"]),
            # Repair attempt 1: fix only a.txt -> re-verify still fails (b.txt bad).
            _tool_call("write_file", path="a.txt", content="good"),
            _final("completed"),
            # Repair attempt 2: fix b.txt -> re-verify passes.
            _tool_call("write_file", path="b.txt", content="good"),
            _final("completed"),
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card="do it", created_by="test")
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "completed", summary.status
        assert summary.verification is not None
        assert summary.verification.status == "passed"
        assert summary.verification.repair_attempts == 2  # took two cycles

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
