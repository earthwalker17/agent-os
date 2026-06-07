"""Tests for Phase 5 — the planning + multi-task execution runner integration.

Coverage:
  - simple card → single-task plan (mode="simple"), no extra LLM planning call,
    legacy behavior preserved.
  - complex card → LLM plan decomposed into multiple tasks; per-task statuses
    recorded; plan.json + result.md task-by-task section written.
  - planner failure (unusable plan) → graceful single-task fallback; run still
    finalizes.
  - dependency-failed task → skipped with a blocker (no LLM call for it).
  - multi-task all-completed → "completed", so command verification still gates
    the run and can downgrade it to "partial".
  - single-task path passes the agent's task_md_update through (regression).

Run directly:
    python backend/tests/test_runner_planning.py
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
from execution.manager import read_task_state  # noqa: E402
from execution.models import RunRecord, RunStatus, TaskSpec, TaskStatus  # noqa: E402
from execution.runner import CodingAgentRunner  # noqa: E402


# ---------- harness (mirrors test_verification_inference.py) ----------


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


def _ptask(tid: str, title: str, description: str = "d", depends_on=None) -> dict:
    return {"id": tid, "title": title, "description": description, "depends_on": depends_on or []}


def _plan(tasks: list[dict], goal: str = "goal", analysis: str = "a", risks=None) -> str:
    return json.dumps(
        {"action": "plan", "goal": goal, "analysis": analysis, "risks": risks or [], "tasks": tasks}
    )


_COMPLEX_CARD = (
    "Build the feature:\n"
    "- create a config file\n"
    "- create a helper module\n"
    "- wire them together"
)


# ---------- tests ----------


def test_simple_card_skips_planner():
    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        monkey: dict[str, Any] = {}
        # Exactly one response: if the planner had made an LLM call, the stub
        # would raise "ran out of stub responses" and the run would fail.
        _patch_llm(monkey, _stub_llm_caller([_final("completed", files_changed=["x.txt"])]))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card="do it", created_by="test")
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "completed"
        assert summary.plan is not None
        assert summary.plan.mode == "simple"
        assert len(summary.plan.tasks) == 1
        # plan.json artifact persisted.
        plan_raw = run_store.read_plan_json("p", summary.run_id)
        assert plan_raw is not None
        assert plan_raw["mode"] == "simple"

    _run(body)


def test_complex_card_decomposes_into_tasks():
    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        responses = [
            _plan([_ptask("t1", "Create config"), _ptask("t2", "Wire it")]),
            _final("completed", summary="config done", files_changed=["config.txt"]),
            _final("completed", summary="wired", files_changed=["main.txt"]),
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card=_COMPLEX_CARD, created_by="test")
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "completed"
        assert summary.plan is not None
        assert summary.plan.mode == "planned"
        assert [t.id for t in summary.plan.tasks] == ["t1", "t2"]
        assert all(t.status == TaskStatus.COMPLETED for t in summary.plan.tasks)

        raw = run_store.read_run_json("p", summary.run_id)
        record = RunRecord(**raw)
        assert record.plan is not None
        assert len(record.plan.tasks) == 2
        assert set(record.files_changed) == {"config.txt", "main.txt"}

        result_md = run_store.read_result_md("p", summary.run_id) or ""
        assert "## Execution Plan" in result_md
        assert "## Tasks" in result_md
        assert "Create config" in result_md
        assert "Wire it" in result_md

    _run(body)


def test_planner_failure_falls_back_to_single_task():
    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        # First planning response is a plan with zero tasks -> PlanParseError ->
        # fallback single-task plan. Then the single-task loop completes.
        responses = [
            _plan([]),
            _final("completed", files_changed=["x.txt"]),
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card=_COMPLEX_CARD, created_by="test")
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "completed"
        assert summary.plan is not None
        assert summary.plan.mode == "fallback"
        assert len(summary.plan.tasks) == 1

    _run(body)


def test_dependency_failed_task_is_skipped():
    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        responses = [
            _plan([_ptask("t1", "Make base"), _ptask("t2", "Build on base", depends_on=["t1"])]),
            _final("failed", summary="base broke", blockers=["could not create base"]),
            # No response for t2 — it must be skipped without an LLM call.
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card=_COMPLEX_CARD, created_by="test")
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.plan is not None
        by_id = {t.id: t for t in summary.plan.tasks}
        assert by_id["t1"].status == TaskStatus.FAILED
        assert by_id["t2"].status == TaskStatus.SKIPPED
        assert any("t1" in b for b in by_id["t2"].blockers)
        # No task completed -> run failed.
        assert summary.status == "failed"

    _run(body)


def test_multitask_all_completed_then_verification_downgrades():
    def body(layout: _TempLayout):
        layout.make_project("p")
        # Manual verification block that always fails, so an all-completed
        # multi-task run is still gated by command verification.
        task_md = (
            "# Task\n\n"
            "## Verification\n\n"
            '```bash\npython -c "import sys; sys.exit(1)"\n```\n'
        )
        layout.init_workspace("p", task_md_body=task_md)
        responses = [
            _plan([_ptask("t1", "Part one"), _ptask("t2", "Part two")]),
            _final("completed", summary="one"),
            _final("completed", summary="two"),
            # No repair responses -> repair pass runs out -> stays failed -> partial.
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card=_COMPLEX_CARD, created_by="test")
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "partial"
        assert summary.verification is not None
        assert summary.verification.status == "failed"
        # Tasks themselves completed — the downgrade comes from verification,
        # demonstrating aggregation (all-completed) still gates on verification.
        assert summary.plan is not None
        assert all(t.status == TaskStatus.COMPLETED for t in summary.plan.tasks)

    _run(body)


def test_single_task_task_md_update_passthrough():
    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        new_task_md = "# Rewritten TASK\n\nthe agent overwrote this\n"
        responses = [
            _final("completed", files_changed=["x.txt"], task_md_update=new_task_md),
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card="do it", created_by="test")
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "completed"
        # The single-task path must pass the agent's task_md_update through.
        task_md = read_task_state("p") or ""
        assert "the agent overwrote this" in task_md

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
