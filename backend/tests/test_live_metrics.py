"""Tests for progressive (live) run metrics.

Before this change, ``RunRecord.files_changed`` / ``commands_run`` were written
to run.json only at finalize, so the Runs panel + chat card + Live Trace showed
``0 files / 0 cmds`` for the whole run and only snapped to the real numbers on
completion. The runner now flushes observed file/command activity to run.json
*during* execution (``_persist_live_metrics``), while finalize still owns the
authoritative lists.

Covered here:
  1. **Run-level progressive metrics** — a single-task run records growing
     files_changed counts in run.json WHILE the run is still ``running``
     (proven by snapshotting every write_run_json call).
  2. **_persist_live_metrics direct behavior** — updates disk while running,
     no-ops once the record is terminal.
  3. **Per-task live attribution** — the executing unit's files/commands +
     plan.json reflect the live delta before the task's terminal task_status.

Run directly:
    python backend/tests/test_live_metrics.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

# Make backend/ importable when running this file directly.
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager  # noqa: E402
import execution.memory_reconciliation as mr  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution.models import (  # noqa: E402
    ExecutionPlan,
    ExecutionTask,
    RunRecord,
    RunStatus,
    TaskSpec,
    TaskStatus,
)
from execution.runner import CodingAgentRunner  # noqa: E402


# ---------- harness (mirrors test_runner_diagnostics) ----------


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


def _tool_call(name: str, **arguments) -> str:
    return json.dumps({
        "action": "tool_call",
        "tool_name": name,
        "arguments": arguments,
        "reason": "test",
    })


def _final(**over) -> str:
    body = {
        "action": "final",
        "status": "completed",
        "summary": "ok",
        "files_changed": [],
        "commands_run": [],
        "blockers": [],
        "task_md_update": "",
    }
    body.update(over)
    return json.dumps(body)


# ---------- 1. run-level progressive metrics ----------


def test_run_level_metrics_climb_while_running():
    """run.json reflects growing files_changed BEFORE the run finalizes."""

    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")

        # Capture (status, files_count) at every run.json write so we can prove
        # the count grew while the run was still RUNNING (not only at finalize).
        snapshots: list[tuple[str, int]] = []
        real_write = run_store.write_run_json

        def spy(project_id, run_id, record):
            snapshots.append((record.status.value, len(record.files_changed)))
            return real_write(project_id, run_id, record)

        responses = [
            _tool_call("write_file", path="a.txt", content="a"),
            _tool_call("write_file", path="b.txt", content="b"),
            _final(files_changed=["a.txt", "b.txt"]),
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        run_store.write_run_json = spy  # type: ignore[assignment]
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card="do", created_by="test"),
            )
        finally:
            run_store.write_run_json = real_write  # type: ignore[assignment]
            _unpatch_llm(monkey)

        # The decisive assertions: files_changed reached 1 then 2 while the
        # record was still RUNNING — i.e. live, before any terminal finalize.
        assert ("running", 1) in snapshots, snapshots
        assert ("running", 2) in snapshots, snapshots
        # And the final on-disk record carries the authoritative two files.
        raw = run_store.read_run_json("p", summary.run_id)
        assert raw is not None
        assert sorted(raw["files_changed"]) == ["a.txt", "b.txt"]

    _run(body)


def test_commands_climb_while_running():
    """run_shell commands also surface live in run.json."""

    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")

        cmd_snaps: list[tuple[str, int]] = []
        real_write = run_store.write_run_json

        def spy(project_id, run_id, record):
            cmd_snaps.append((record.status.value, len(record.commands_run)))
            return real_write(project_id, run_id, record)

        responses = [
            _tool_call("run_shell", command="echo hello"),
            _final(commands_run=["echo hello"]),
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        run_store.write_run_json = spy  # type: ignore[assignment]
        try:
            CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card="do", created_by="test"),
            )
        finally:
            run_store.write_run_json = real_write  # type: ignore[assignment]
            _unpatch_llm(monkey)

        assert ("running", 1) in cmd_snaps, cmd_snaps

    _run(body)


# ---------- 2. _persist_live_metrics direct behavior ----------


def test_persist_live_metrics_updates_disk_while_running():
    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        run_store.init_run_dir("p", "r1")
        record = RunRecord(run_id="r1", project_id="p", task_title="t", status=RunStatus.RUNNING)
        run_store.write_run_json("p", "r1", record)

        runner = CodingAgentRunner("p")
        runner._live_run_id = "r1"
        runner._live_record = record
        runner._observed_files_changed = ["x.txt", "y.txt"]
        runner._observed_commands_run = ["echo hi"]
        runner._persist_live_metrics()

        raw = run_store.read_run_json("p", "r1")
        assert raw["files_changed"] == ["x.txt", "y.txt"]
        assert raw["commands_run"] == ["echo hi"]

    _run(body)


def test_persist_live_metrics_noops_when_terminal():
    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        run_store.init_run_dir("p", "r1")
        record = RunRecord(run_id="r1", project_id="p", task_title="t", status=RunStatus.RUNNING)
        runner = CodingAgentRunner("p")
        runner._live_run_id = "r1"
        runner._live_record = record
        runner._observed_files_changed = ["x.txt"]
        runner._persist_live_metrics()
        assert run_store.read_run_json("p", "r1")["files_changed"] == ["x.txt"]

        # Once terminal, further activity must NOT be flushed (never clobber a
        # settled record).
        record.status = RunStatus.COMPLETED
        runner._observed_files_changed.append("z.txt")
        runner._persist_live_metrics()
        assert run_store.read_run_json("p", "r1")["files_changed"] == ["x.txt"]

    _run(body)


# ---------- 3. per-task live attribution ----------


def test_active_unit_attribution_writes_plan_json():
    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        run_store.init_run_dir("p", "r1")
        plan = ExecutionPlan(
            mode="planned",
            tasks=[
                ExecutionTask(id="t1", title="one", status=TaskStatus.COMPLETED, files_changed=["t1a.txt"]),
                ExecutionTask(id="t2", title="two", status=TaskStatus.RUNNING),
            ],
        )
        record = RunRecord(
            run_id="r1", project_id="p", task_title="t",
            status=RunStatus.RUNNING, plan=plan,
        )
        run_store.write_run_json("p", "r1", record)
        run_store.write_plan_json("p", "r1", plan)

        runner = CodingAgentRunner("p")
        runner._live_run_id = "r1"
        runner._live_record = record
        # t1 already wrote t1a.txt; t2 (the active unit) starts at offset 1.
        runner._observed_files_changed = ["t1a.txt", "t2a.txt", "t2b.txt"]
        runner._begin_unit(plan.tasks[1], before_f=1, before_c=0)
        runner._persist_live_metrics()

        # Run-level shows everything; the active unit shows only its own delta.
        run_raw = run_store.read_run_json("p", "r1")
        assert run_raw["files_changed"] == ["t1a.txt", "t2a.txt", "t2b.txt"]
        plan_raw = run_store.read_plan_json("p", "r1")
        assert plan_raw["tasks"][1]["files_changed"] == ["t2a.txt", "t2b.txt"]
        # The earlier task's record is untouched.
        assert plan_raw["tasks"][0]["files_changed"] == ["t1a.txt"]

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
