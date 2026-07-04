"""Tests for the runner-diagnostics + startup-sweep fixes.

Two narrow regressions are covered here:

  1. **Observed tool activity falls back into files_changed / commands_run**
     when the LLM loop exhausts its step budget before emitting a
     ``final`` action. Real-world failure mode: the agent spent all
     its steps inspecting + tweaking, never said ``final``, and the
     run report claimed no files were touched even though several
     were written. The fix records successful write_file/append_file
     paths and run_shell commands locally, and uses them as a
     fallback when the final action's lists are absent/empty.

  2. **``run_store.sweep_stuck_runs()`` rewrites stuck runs.** Runs get
     stuck in ``running`` when the server process exits mid-loop —
     the BackgroundRunManager's in-process handler can't promote them
     because it died with the process. The sweep is meant to run at
     startup and reconcile orphans. Tests cover: stuck->failed,
     non-running statuses untouched, corrupt run.json skipped without
     raising, result.md backfilled only when missing.

Run directly:
    python backend/tests/test_runner_diagnostics.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

# Make backend/ importable when running this file directly.
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager  # noqa: E402
import execution.memory_reconciliation as mr  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution.models import RunRecord, RunStatus, TaskSpec  # noqa: E402
from execution.runner import CodingAgentRunner, MAX_STEPS  # noqa: E402


# ---------- harness ----------


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

    def init_workspace(
        self,
        project_id: str,
        *,
        task_md_body: str = "# TASK\n",
    ) -> Path:
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

    def write_run(
        self,
        project_id: str,
        run_id: str,
        *,
        status: str,
        with_result_md: bool = False,
        corrupt: bool = False,
        extra: dict | None = None,
    ) -> Path:
        ws_dir = self.execution_dir / project_id
        (ws_dir / "runs").mkdir(parents=True, exist_ok=True)
        run_dir = ws_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        run_json = run_dir / "run.json"
        if corrupt:
            run_json.write_text("{not actually json", encoding="utf-8")
        else:
            payload = {
                "run_id": run_id,
                "project_id": project_id,
                "task_title": "t",
                "status": status,
                "created_at": "2026-01-01T00:00:00",
                "completed_at": None,
                "files_changed": [],
                "commands_run": [],
                "blockers": [],
            }
            if extra:
                payload.update(extra)
            run_json.write_text(json.dumps(payload), encoding="utf-8")
        (run_dir / "events.jsonl").touch()
        if with_result_md:
            (run_dir / "result.md").write_text("# pre-existing\n", encoding="utf-8")
        return run_dir


def _run(test_body):
    layout = _TempLayout()
    try:
        test_body(layout)
    finally:
        layout.cleanup()


# ---------- runner observed-activity fallback ----------


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


def test_max_steps_is_at_least_twelve():
    # Scaffolding tasks routinely need 5-8 write_file steps; the budget
    # should be comfortably above that or this regression returns.
    assert MAX_STEPS >= 12


def test_observed_writes_surface_when_budget_exhausts():
    """The agent writes several files but never finalizes — those files
    should still show up in files_changed."""

    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        layout.init_workspace("agent-os")

        responses: list[str] = []
        # Emit MAX_STEPS write_file calls and never a `final` — forces
        # the budget-exhaustion code path.
        for i in range(MAX_STEPS):
            responses.append(_tool_call("write_file", path=f"f{i}.txt", content=f"x{i}"))
        # One extra in case the runner asks again (it won't, but be safe).
        responses.append(_tool_call("write_file", path="extra.txt", content="x"))

        caller = _stub_llm_caller(responses)
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, caller)
        try:
            summary = CodingAgentRunner("agent-os").run_task(
                TaskSpec(title="t", task_card="do", created_by="test"),
            )
        finally:
            _unpatch_llm(monkey)

        # Loop exhausted -> status failed, but files_changed must reflect
        # what actually happened.
        assert summary.status == "failed"
        assert any("step budget exhausted" in b for b in summary.blockers)
        # We should see every file we wrote (deduplicated, ordered).
        assert summary.files_changed[0] == "f0.txt"
        assert summary.files_changed[-1] == f"f{MAX_STEPS - 1}.txt"
        assert len(summary.files_changed) == MAX_STEPS
        # Run.json on disk should match.
        raw = run_store.read_run_json("agent-os", summary.run_id)
        assert raw is not None
        record = RunRecord(**raw)
        assert record.status == RunStatus.FAILED
        assert len(record.files_changed) == MAX_STEPS

    _run(body)


def test_final_action_lists_take_precedence_over_observed():
    """When the agent supplies a final action with non-empty lists, those
    win over the runner's observed-activity fallback."""

    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        layout.init_workspace("agent-os")

        responses = [
            _tool_call("write_file", path="observed.txt", content="o"),
            json.dumps({
                "action": "final",
                "status": "completed",
                "summary": "ok",
                "files_changed": ["explicit.txt"],
                "commands_run": ["echo hi"],
                "blockers": [],
                "task_md_update": "",
            }),
        ]
        caller = _stub_llm_caller(responses)
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, caller)
        try:
            summary = CodingAgentRunner("agent-os").run_task(
                TaskSpec(title="t", task_card="do", created_by="test"),
            )
        finally:
            _unpatch_llm(monkey)

        # Final action's list is authoritative even though "observed.txt"
        # was also written.
        assert summary.status == "completed"
        assert summary.files_changed == ["explicit.txt"]
        assert summary.commands_run == ["echo hi"]

    _run(body)


def test_failed_writes_are_not_observed():
    """A write_file that the sandbox rejects must not be counted as an
    observed file change."""

    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        layout.init_workspace("agent-os")

        responses: list[str] = []
        responses.append(_tool_call("write_file", path="ok.txt", content="ok"))
        # Sandbox will reject the next one (absolute path).
        responses.append(_tool_call("write_file", path="/etc/passwd", content="bad"))
        # Fill the rest with no-op reads of a file we just wrote.
        for _ in range(MAX_STEPS - 2):
            responses.append(_tool_call("read_file", path="ok.txt"))
        responses.append(_tool_call("read_file", path="ok.txt"))

        caller = _stub_llm_caller(responses)
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, caller)
        try:
            summary = CodingAgentRunner("agent-os").run_task(
                TaskSpec(title="t", task_card="do", created_by="test"),
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "failed"
        assert summary.files_changed == ["ok.txt"]
        assert "/etc/passwd" not in summary.files_changed

    _run(body)


# ---------- sweep_stuck_runs ----------


def test_sweep_promotes_stuck_running_to_failed():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        layout.write_run("p1", "r1", status="running")

        swept = run_store.sweep_stuck_runs()
        assert swept == ["p1/r1"]

        raw = run_store.read_run_json("p1", "r1")
        assert raw is not None
        assert raw["status"] == "failed"
        assert any("interrupted" in b for b in raw["blockers"])
        # completed_at gets stamped so the UI no longer shows a spinner.
        assert raw["completed_at"] is not None
        # result.md was backfilled (it was missing before the sweep).
        result_md = run_store.read_result_md("p1", "r1") or ""
        assert "did not finalize" in result_md or "interrupted" in result_md

    _run(body)


def test_sweep_leaves_terminal_runs_untouched():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        layout.write_run("p1", "completed-1", status="completed")
        layout.write_run("p1", "failed-1", status="failed")
        layout.write_run("p1", "partial-1", status="partial")
        layout.write_run("p1", "running-1", status="running")

        swept = run_store.sweep_stuck_runs()
        assert swept == ["p1/running-1"]

        # Other statuses unchanged.
        assert (run_store.read_run_json("p1", "completed-1") or {}).get("status") == "completed"
        assert (run_store.read_run_json("p1", "failed-1") or {}).get("status") == "failed"
        assert (run_store.read_run_json("p1", "partial-1") or {}).get("status") == "partial"

    _run(body)


def test_sweep_does_not_overwrite_existing_result_md():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        layout.write_run("p1", "r1", status="running", with_result_md=True)

        run_store.sweep_stuck_runs()
        result_md = run_store.read_result_md("p1", "r1") or ""
        # Pre-existing content is preserved — the sweep only backfills
        # when result.md was missing.
        assert "pre-existing" in result_md

    _run(body)


def test_sweep_skips_corrupt_run_json_without_raising():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        layout.write_run("p1", "good", status="running")
        layout.write_run("p1", "bad", status="running", corrupt=True)

        # Must not raise even though one run.json is unreadable.
        swept = run_store.sweep_stuck_runs()
        # The good one was swept; the corrupt one was skipped.
        assert "p1/good" in swept
        assert "p1/bad" not in swept

    _run(body)


def test_sweep_handles_missing_execution_root():
    def body(layout: _TempLayout):
        # Don't init any workspace at all. The execution root exists but
        # is empty; the sweep should just return an empty list.
        swept = run_store.sweep_stuck_runs()
        assert swept == []

    _run(body)


# ---------- transient sub-state clearing (T1.3) ----------

_ALL_TRANSIENT = {
    "verification_state": "verifying",
    "browser_verification_state": "running",
    "integration_state": "integrating",
    "git_state": "committing",
    "deploy_state": "deploying",
    "external_state": "migrating",
}


def test_sweep_clears_all_transient_states_on_stuck_running():
    """A crash mid-phase can leave ANY of the six transient states set on a
    still-`running` run. The startup sweep must clear them all, not just
    integration_state, or the UI poll gates spin forever after restart."""
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        layout.write_run("p1", "r1", status="running", extra=dict(_ALL_TRANSIENT))

        swept = run_store.sweep_stuck_runs()
        assert swept == ["p1/r1"]

        raw = run_store.read_run_json("p1", "r1") or {}
        assert raw["status"] == "failed"
        for field in _ALL_TRANSIENT:
            assert raw.get(field) is None, f"{field} not cleared by sweep"

    _run(body)


def test_terminal_sweep_clears_leaked_verification_state():
    """verification_state / browser_verification_state are set AFTER the run's
    status goes terminal (the verify tail), so a crash there leaves a completed
    run with a lingering local transient state that sweep_stuck_runs skips.
    sweep_terminal_transient_states must clear the LOCAL gates — but never
    deploy_state/external_state (the external reconciler owns those)."""
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        layout.write_run(
            "p1", "done-1", status="completed",
            extra={
                "verification_state": "verifying",
                "browser_verification_state": "running",
                "integration_state": "integrating",
                "git_state": "pushing",
                # A real half-applied external action must be left for the
                # provider-querying reconciler, NOT cleared here.
                "deploy_state": "deploying",
                "external_state": "migrating",
            },
        )
        # A run with no leaked local state must be left alone (not "fixed").
        layout.write_run("p1", "clean-1", status="completed")

        fixed = run_store.sweep_terminal_transient_states()
        assert "p1/done-1" in fixed
        assert "p1/clean-1" not in fixed

        raw = run_store.read_run_json("p1", "done-1") or {}
        assert raw["status"] == "completed"  # status untouched
        assert raw.get("verification_state") is None
        assert raw.get("browser_verification_state") is None
        assert raw.get("integration_state") is None
        assert raw.get("git_state") is None
        # deploy/external deliberately preserved for reconcile_stuck_external_actions.
        assert raw.get("deploy_state") == "deploying"
        assert raw.get("external_state") == "migrating"

    _run(body)


def test_terminal_sweep_leaves_running_runs_untouched():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        layout.write_run("p1", "run-1", status="running",
                         extra={"verification_state": "verifying"})
        fixed = run_store.sweep_terminal_transient_states()
        assert fixed == []  # running runs are sweep_stuck_runs' job
        assert (run_store.read_run_json("p1", "run-1") or {}).get("status") == "running"

    _run(body)


def test_terminal_sweep_preserves_settled_browser_verification_state():
    """browser_verification_state holds a SETTLED result ('passed'/'failed') on a
    terminal run — the sweep must NOT wipe it (only its transient 'running')."""
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        # A completed run whose browser verification settled 'passed', with a
        # genuinely leaked verification_state alongside.
        layout.write_run(
            "p1", "done-1", status="completed",
            extra={"browser_verification_state": "passed",
                   "verification_state": "verifying"},
        )
        # A completed run whose browser verification is a leaked 'running'.
        layout.write_run(
            "p1", "leaked-1", status="completed",
            extra={"browser_verification_state": "running"},
        )
        fixed = run_store.sweep_terminal_transient_states()
        assert "p1/done-1" in fixed and "p1/leaked-1" in fixed

        done = run_store.read_run_json("p1", "done-1") or {}
        # Settled 'passed' preserved; the leaked verifying cleared.
        assert done.get("browser_verification_state") == "passed"
        assert done.get("verification_state") is None

        leaked = run_store.read_run_json("p1", "leaked-1") or {}
        assert leaked.get("browser_verification_state") is None  # transient cleared

    _run(body)


def test_terminal_sweep_ignores_run_with_only_settled_browser_state():
    """A terminal run whose ONLY set field is a settled 'passed' must be left
    entirely untouched (not even rewritten)."""
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        layout.write_run("p1", "ok-1", status="completed",
                         extra={"browser_verification_state": "failed"})
        fixed = run_store.sweep_terminal_transient_states()
        assert "p1/ok-1" not in fixed
        assert (run_store.read_run_json("p1", "ok-1") or {}).get("browser_verification_state") == "failed"

    _run(body)


# ---------- runner ----------


def _run_all() -> int:
    tests = [
        v for k, v in globals().items()
        if k.startswith("test_") and callable(v)
    ]
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
