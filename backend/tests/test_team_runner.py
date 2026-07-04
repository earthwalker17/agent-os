"""Tests for Task 9.5 — the team execution path (wave scheduling, parallel
units in isolated patch workspaces, deterministic integration, read-only role
enforcement, conflicts, cancellation, and the sequential-gate regression).

Parallel units call the LLM concurrently, so the classic pop-a-list stub is
nondeterministic here. These tests use a THREAD-SAFE, PROMPT-KEYED stub: the
planning call is recognized by its system prompt, and each task unit's calls
are matched by the ``This task (tN)`` marker in its initial user prompt, each
task consuming its own response sequence under a lock.

Run:  python backend/tests/test_team_runner.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager  # noqa: E402
import execution.memory_reconciliation as mr  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution.models import RunStatus, TaskSpec, TaskStatus  # noqa: E402
from execution.patch_workspace import get_overlay_root, read_patch_manifest  # noqa: E402
from execution.runner import CodingAgentRunner  # noqa: E402


_PID = "teamproj"


# ---------- harness (mirrors test_runner_planning.py) ----------


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
        try:
            self.tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def init_workspace(self, project_id: str = _PID) -> Path:
        ws_dir = self.execution_dir / project_id
        repo_dir = ws_dir / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "AGENT.md").write_text("# AGENT\n", encoding="utf-8")
        (ws_dir / "TASK.md").write_text("# TASK\n", encoding="utf-8")
        (ws_dir / "runs").mkdir(exist_ok=True)
        (ws_dir / "logs").mkdir(exist_ok=True)
        return repo_dir

    def make_project(self, project_id: str = _PID) -> None:
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


def _patch_llm(monkey: dict[str, Any], caller):
    import llm

    monkey["llm_chat"] = llm.chat
    llm.chat = caller  # type: ignore[assignment]


def _unpatch_llm(monkey: dict[str, Any]):
    import llm

    if "llm_chat" in monkey:
        llm.chat = monkey["llm_chat"]


def _keyed_caller(
    plan_json: str,
    task_responses: dict[str, list[str]],
    *,
    on_serve=None,
):
    """Thread-safe, prompt-keyed LLM stub for concurrent task units.

    - The planning call (its system prompt says "PLANNING phase") returns
      ``plan_json`` once.
    - A task-unit call is matched by the ``This task (tN)`` marker in its
      user messages and pops the next response from that task's own sequence.
    - Any other call (memory reconciliation / recovery judges) raises — those
      subsystems are best-effort and swallow the error, mirroring the
      exhausted-stub convention of the existing suites.
    - ``on_serve(task_id, index, prompt_text)`` (optional) observes each
      served task response — used to assert prompt contents / drive cancels.
    """
    lock = threading.Lock()
    state = {"plan_served": False}
    counts: dict[str, int] = {}

    def caller(*args, **kwargs) -> str:
        system = kwargs.get("system") or (args[0] if args else "") or ""
        messages = kwargs.get("messages") or (args[1] if len(args) > 1 else []) or []
        if "PLANNING phase" in system:
            with lock:
                if state["plan_served"]:
                    raise AssertionError("planner called twice")
                state["plan_served"] = True
            return plan_json
        text = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "user"
        )
        for tid, seq in task_responses.items():
            if f"This task ({tid})" in text:
                with lock:
                    idx = counts.get(tid, 0)
                    if idx >= len(seq):
                        raise AssertionError(f"stub responses for {tid} exhausted")
                    counts[tid] = idx + 1
                if on_serve is not None:
                    on_serve(tid, idx, text)
                return seq[idx]
        raise AssertionError("no stub response matched this call")

    return caller


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


def _tool(tool_name: str, **arguments) -> str:
    return json.dumps(
        {"action": "tool_call", "tool_name": tool_name, "arguments": arguments, "reason": "r"}
    )


def _ptask(tid: str, title: str, *, role: str = "coder", parallel: bool = False, depends_on=None) -> dict:
    return {
        "id": tid,
        "title": title,
        "description": f"do {tid}",
        "depends_on": depends_on or [],
        "role": role,
        "parallel": parallel,
    }


def _plan(tasks: list[dict], goal: str = "team goal") -> str:
    return json.dumps(
        {"action": "plan", "goal": goal, "analysis": "a", "risks": [], "tasks": tasks}
    )


_COMPLEX_CARD = (
    "Build the feature:\n"
    "- create module A\n"
    "- create module B\n"
    "- wire them together"
)


def _dispatch(layout: _TempLayout, caller) -> Any:
    layout.init_workspace()
    layout.make_project()
    monkey: dict[str, Any] = {}
    _patch_llm(monkey, caller)
    try:
        return CodingAgentRunner(_PID).run_task(
            TaskSpec(title="team task", task_card=_COMPLEX_CARD, created_by="test")
        )
    finally:
        _unpatch_llm(monkey)


def _events(run_id: str) -> list[dict]:
    return run_store.read_events(_PID, run_id)


def _event_types(run_id: str) -> list[str]:
    return [e.get("type") for e in _events(run_id)]


# ---------- the golden team path ----------


def test_parallel_wave_executes_isolates_and_integrates():
    def body(layout):
        caller = _keyed_caller(
            _plan(
                [
                    _ptask("t1", "Module A", parallel=True),
                    _ptask("t2", "Module B", parallel=True),
                    _ptask("t3", "Wire together", depends_on=["t1", "t2"]),
                ]
            ),
            {
                "t1": [
                    _tool("write_file", path="src/a.txt", content="module A\n"),
                    _final(files_changed=["src/a.txt"], summary="A done"),
                ],
                "t2": [
                    _tool("write_file", path="src/b.txt", content="module B\n"),
                    _final(files_changed=["src/b.txt"], summary="B done"),
                ],
                "t3": [
                    _tool("write_file", path="src/c.txt", content="wired\n"),
                    _final(files_changed=["src/c.txt"], summary="wired"),
                ],
            },
        )
        summary = _dispatch(layout, caller)
        assert summary.status == "completed", summary.blockers
        run_id = summary.run_id

        # The team path was taken and waves were stamped.
        plan = summary.plan
        assert plan is not None and plan.execution_mode == "team"
        by_id = {t.id: t for t in plan.tasks}
        assert by_id["t1"].wave == 1 and by_id["t2"].wave == 1 and by_id["t3"].wave == 2
        assert by_id["t1"].workspace == "patch" and by_id["t2"].workspace == "patch"
        assert by_id["t3"].workspace == "main"
        assert all(t.status == TaskStatus.COMPLETED for t in plan.tasks)

        # Parallel units wrote to their overlays; integration applied to repo.
        repo = layout.execution_dir / _PID / "repo"
        assert (repo / "src" / "a.txt").read_text(encoding="utf-8") == "module A\n"
        assert (repo / "src" / "b.txt").read_text(encoding="utf-8") == "module B\n"
        assert (repo / "src" / "c.txt").read_text(encoding="utf-8") == "wired\n"
        assert (get_overlay_root(_PID, run_id, "t1") / "src" / "a.txt").exists()
        manifest = read_patch_manifest(_PID, run_id, "t1")
        assert manifest and manifest["files"] == ["src/a.txt"]
        assert manifest["role"] == "coder" and manifest["status"] == "completed"

        # Record carries the integration aggregate; integration.json exists.
        raw = run_store.read_run_json(_PID, run_id)
        integ = raw.get("integration")
        assert integ and integ["enabled"] is True
        assert sorted(integ["files_applied"]) == ["src/a.txt", "src/b.txt"]
        assert integ["conflicts"] == []
        assert raw.get("integration_state") is None
        detail = run_store.read_integration_json(_PID, run_id)
        assert detail and len(detail["waves"]) == 1

        # Team trace events.
        types = _event_types(run_id)
        assert "team_execution_started" in types
        assert "wave_started" in types
        assert "integration_started" in types and "integration_completed" in types
        started = [e for e in _events(run_id) if e.get("type") == "task_started"]
        parallel_started = [e for e in started if e.get("parallel") is True]
        assert {e["task_id"] for e in parallel_started} == {"t1", "t2"}
        assert all(e.get("role") == "coder" for e in started)
        # Parallel tool events carry task attribution.
        tool_calls = [e for e in _events(run_id) if e.get("type") == "tool_call" and e.get("phase") == "execution"]
        assert all(e.get("task_id") in {"t1", "t2", "t3"} for e in tool_calls)

        # result.md shows the team annotations + integration section.
        result_md = run_store.read_result_md(_PID, run_id)
        assert "role: coder" in result_md and "wave 1" in result_md
        assert "## Integration" in result_md

    _run(body)


def test_parallel_units_actually_overlap():
    """Both wave-1 units must be in flight concurrently (real parallelism)."""

    def body(layout):
        barrier = threading.Barrier(2, timeout=30)
        overlapped = {"ok": False}

        def on_serve(tid: str, idx: int, _text: str):
            # First LLM call of each parallel unit meets at a barrier — if the
            # units ran sequentially this would deadlock (30s timeout -> error).
            if idx == 0 and tid in ("t1", "t2"):
                barrier.wait()
                overlapped["ok"] = True

        caller = _keyed_caller(
            _plan(
                [
                    _ptask("t1", "A", parallel=True),
                    _ptask("t2", "B", parallel=True),
                ]
            ),
            {
                "t1": [_final(summary="A done")],
                "t2": [_final(summary="B done")],
            },
            on_serve=on_serve,
        )
        summary = _dispatch(layout, caller)
        assert summary.status == "completed"
        assert overlapped["ok"], "wave-1 units never overlapped"

    _run(body)


# ---------- conflicts ----------


def test_conflicting_parallel_writes_surface_and_cap_at_partial():
    def body(layout):
        caller = _keyed_caller(
            _plan(
                [
                    _ptask("t1", "A", parallel=True),
                    _ptask("t2", "B", parallel=True),
                ]
            ),
            {
                "t1": [
                    _tool("write_file", path="src/app.txt", content="version A\n"),
                    _final(files_changed=["src/app.txt"]),
                ],
                "t2": [
                    _tool("write_file", path="src/app.txt", content="version B\n"),
                    _final(files_changed=["src/app.txt"]),
                ],
            },
        )
        summary = _dispatch(layout, caller)
        # Both tasks completed, but the conflicted integration caps at partial.
        assert summary.status == "partial"
        assert any("integration conflict" in b for b in summary.blockers)

        repo = layout.execution_dir / _PID / "repo"
        assert (repo / "src" / "app.txt").read_text(encoding="utf-8") == "version A\n"

        raw = run_store.read_run_json(_PID, summary.run_id)
        conflicts = raw["integration"]["conflicts"]
        assert len(conflicts) == 1
        assert conflicts[0]["path"] == "src/app.txt"
        assert conflicts[0]["applied_task"] == "t1"
        assert conflicts[0]["rejected_task"] == "t2"
        assert "integration_conflict" in _event_types(summary.run_id)
        # The losing task carries the blocker; its full version stays inspectable.
        by_id = {t.id: t for t in summary.plan.tasks}
        assert any("integration conflict" in b for b in by_id["t2"].blockers)
        assert (
            get_overlay_root(_PID, summary.run_id, "t2") / "src" / "app.txt"
        ).read_text(encoding="utf-8") == "version B\n"

    _run(body)


# ---------- integration apply-errors degrade status (never silent) ----------


def test_integration_apply_error_caps_at_partial_and_blocks():
    def body(layout):
        repo = layout.init_workspace()
        # A regular file named 'src' — a parallel task writing 'src/x.txt' will
        # fail to APPLY at integration (can't mkdir a dir where a file exists).
        (repo / "src").write_text("i am a file, not a dir\n", encoding="utf-8")
        caller = _keyed_caller(
            _plan(
                [
                    _ptask("t1", "A", parallel=True),
                    _ptask("t2", "B", parallel=True),
                ]
            ),
            {
                "t1": [
                    _tool("write_file", path="src/x.txt", content="x\n"),
                    _final(files_changed=["src/x.txt"]),
                ],
                "t2": [
                    _tool("write_file", path="other.txt", content="ok\n"),
                    _final(files_changed=["other.txt"]),
                ],
            },
        )
        summary = _dispatch(layout, caller)
        # Both tasks completed, but the dropped file must NOT leave the run green.
        assert summary.status == "partial", summary.status
        assert any("failed to apply" in b for b in summary.blockers)
        # The healthy sibling still integrated.
        assert (repo / "other.txt").exists()

    _run(body)


# ---------- read-only role enforcement ----------


def test_read_only_roles_run_parallel_and_writes_are_bounced():
    def body(layout):
        repo = layout.init_workspace()
        (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
        caller = _keyed_caller(
            _plan(
                [
                    _ptask("t1", "Review the code", role="reviewer"),
                    _ptask("t2", "Inspect the layout", role="inspector"),
                ]
            ),
            {
                "t1": [
                    # A reviewer trying to write must be bounced, not executed.
                    _tool("write_file", path="hack.txt", content="nope"),
                    _tool("read_file", path="code.py"),
                    _final(summary="review finding: x is fine", blockers=[]),
                ],
                "t2": [
                    _tool("list_files", path="."),
                    _final(summary="layout inspected"),
                ],
            },
        )
        summary = _dispatch(layout, caller)
        assert summary.status == "completed", summary.blockers
        # Read-only units ran in the main workspace — no patch dirs at all.
        assert not (layout.execution_dir / _PID / "patches").exists()
        # The bounced write never landed anywhere.
        assert not (repo / "hack.txt").exists()
        bounced = [
            e
            for e in _events(summary.run_id)
            if e.get("type") == "tool_result"
            and e.get("success") is False
            and "reviewer role" in str(e.get("error", ""))
        ]
        assert bounced, "reviewer write_file was not bounced"
        by_id = {t.id: t for t in summary.plan.tasks}
        assert by_id["t1"].workspace == "main" and by_id["t1"].wave == 1
        # No integration happened (nothing to merge), and that's honest.
        raw = run_store.read_run_json(_PID, summary.run_id)
        assert raw["integration"]["enabled"] is True
        assert raw["integration"]["files_applied"] == []

    _run(body)


# ---------- findings flow into later waves ----------


def test_reviewer_findings_reach_later_wave_prompts():
    def body(layout):
        seen = {"marker": False}

        def on_serve(tid: str, idx: int, text: str):
            if tid == "t2" and "FINDING_MARKER_XYZ" in text:
                seen["marker"] = True

        caller = _keyed_caller(
            _plan(
                [
                    _ptask("t1", "Inspect", role="inspector"),
                    _ptask("t1b", "Survey", role="reviewer"),
                    _ptask("t2", "Build on findings", depends_on=["t1"]),
                ]
            ),
            {
                "t1": [_final(summary="FINDING_MARKER_XYZ: use pattern Q")],
                "t1b": [_final(summary="looks fine")],
                "t2": [_final(summary="built")],
            },
            on_serve=on_serve,
        )
        summary = _dispatch(layout, caller)
        assert summary.status == "completed", summary.blockers
        assert seen["marker"], "inspector findings did not reach the wave-2 prompt"

    _run(body)


# ---------- cancellation ----------


def test_cancel_during_parallel_wave_finalizes_cancelled():
    def body(layout):
        evt = threading.Event()

        def on_serve(tid: str, idx: int, _text: str):
            # Request cancellation as the first parallel unit serves its first
            # response — units observe it at their next step boundary.
            evt.set()

        caller = _keyed_caller(
            _plan(
                [
                    _ptask("t1", "A", parallel=True),
                    _ptask("t2", "B", parallel=True),
                ]
            ),
            {
                # Tool-call first so each unit has a next step boundary at
                # which to observe the cancel.
                "t1": [
                    _tool("list_files", path="."),
                    _final(summary="A done"),
                ],
                "t2": [
                    _tool("list_files", path="."),
                    _final(summary="B done"),
                ],
            },
            on_serve=on_serve,
        )
        layout.init_workspace()
        layout.make_project()
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, caller)
        try:
            summary = CodingAgentRunner(_PID).run_task(
                TaskSpec(title="team task", task_card=_COMPLEX_CARD, created_by="test"),
                cancel_event=evt,
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "cancelled"
        raw = run_store.read_run_json(_PID, summary.run_id)
        assert raw["status"] == RunStatus.CANCELLED.value
        assert raw.get("integration_state") is None
        # No task may be left perpetually running.
        plan = run_store.read_plan_json(_PID, summary.run_id)
        assert all(t["status"] != "running" for t in plan["tasks"])
        assert "run_cancelled" in _event_types(summary.run_id)

    _run(body)


# ---------- the conservative gate (regression) ----------


def test_sequential_plans_do_not_take_the_team_path():
    def body(layout):
        caller = _keyed_caller(
            _plan(
                [
                    _ptask("t1", "First"),
                    _ptask("t2", "Second", depends_on=["t1"]),
                ]
            ),
            {
                "t1": [_final(summary="one")],
                "t2": [_final(summary="two")],
            },
        )
        summary = _dispatch(layout, caller)
        assert summary.status == "completed"
        assert summary.plan.execution_mode == "sequential"
        # No team artifacts for a sequential run.
        assert not (layout.execution_dir / _PID / "patches").exists()
        raw = run_store.read_run_json(_PID, summary.run_id)
        assert raw.get("integration") is None
        types = _event_types(summary.run_id)
        assert "team_execution_started" not in types
        assert "wave_started" not in types

    _run(body)


# ---------- one crashing unit must not kill the run ----------


def test_one_failed_parallel_unit_yields_partial_not_crash():
    def body(layout):
        caller = _keyed_caller(
            _plan(
                [
                    _ptask("t1", "A", parallel=True),
                    _ptask("t2", "B", parallel=True),
                ]
            ),
            {
                "t1": [
                    _tool("write_file", path="a.txt", content="A"),
                    _final(files_changed=["a.txt"]),
                ],
                # t2's stub is EMPTY -> its first LLM call raises inside the
                # worker -> protocol error -> the unit fails, the run survives.
                "t2": [],
            },
        )
        summary = _dispatch(layout, caller)
        assert summary.status == "partial"
        by_id = {t.id: t for t in summary.plan.tasks}
        assert by_id["t1"].status == TaskStatus.COMPLETED
        assert by_id["t2"].status == TaskStatus.FAILED
        # t1's output still integrated.
        repo = layout.execution_dir / _PID / "repo"
        assert (repo / "a.txt").exists()

    _run(body)


# ---------- standalone runner ----------


def _run_all() -> int:
    failures = 0
    names = [n for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name in names:
        fn = globals()[name]
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback

            traceback.print_exc()
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"\n{failures} of {len(names)} tests failed.")
        return 1
    print(f"\nAll {len(names)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
