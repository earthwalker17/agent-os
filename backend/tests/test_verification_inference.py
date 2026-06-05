"""Tests for Task 06.2E — automatic command verification + repair loop.

Coverage:

  - parse_verify_commands: returns every uncommented line (multi-command).
  - infer_verification_specs: package.json build (with/without node_modules),
    python tests, python-without-tests syntax check, full-stack, and the
    nothing-to-do case.
  - plan_verification: manual override beats inference; inferred; skipped.
  - run_verification_specs: multi-command, stop-at-first-failure, all-pass.
  - render_verification_section: multi-command breakdown + mode.
  - runner integration: inferred verification passes -> completed; an inferred
    failure that the repair pass fixes -> completed; a failure the repair pass
    can't fix -> partial; repair unavailable (LLM out) -> partial.

Run directly:
    python backend/tests/test_verification_inference.py
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
from execution.models import (  # noqa: E402
    RunRecord,
    RunStatus,
    TaskSpec,
    VerificationCommandResult,
    VerificationResult,
)
from execution.runner import CodingAgentRunner  # noqa: E402
from execution.verification import (  # noqa: E402
    infer_verification_specs,
    parse_verify_commands,
    plan_verification,
    render_verification_section,
    run_verification_specs,
)


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
        repo_files: dict[str, str] | None = None,
    ) -> Path:
        ws_dir = self.execution_dir / project_id
        repo_dir = ws_dir / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "AGENT.md").write_text("# AGENT\n", encoding="utf-8")
        (ws_dir / "TASK.md").write_text(task_md_body, encoding="utf-8")
        (ws_dir / "runs").mkdir(exist_ok=True)
        (ws_dir / "logs").mkdir(exist_ok=True)
        for rel, body in (repo_files or {}).items():
            target = repo_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
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


# ---------- parse_verify_commands ----------


def test_parse_verify_commands_multiple():
    text = (
        "## Verification\n\n"
        "```bash\n"
        "# a comment\n"
        "python -m pytest\n"
        "npm run build\n"
        "```\n"
    )
    assert parse_verify_commands(text) == ["python -m pytest", "npm run build"]


def test_parse_verify_commands_empty():
    assert parse_verify_commands("# TASK\n## Other\nx\n") == []
    assert parse_verify_commands("## Verification\n\n```bash\n# all commented\n```\n") == []


# ---------- infer_verification_specs ----------


def test_infer_package_build_includes_install_without_node_modules(tmp_repo=None):
    def body(layout: _TempLayout):
        repo = layout.init_workspace(
            "p", repo_files={"package.json": json.dumps({"scripts": {"build": "vite build"}})}
        )
        specs = infer_verification_specs(repo)
        cmds = [s.command for s in specs]
        assert cmds == ["npm install", "npm run build"]
        assert specs[0].kind == "install"
        assert specs[1].kind == "build"

    _run(body)


def test_infer_package_build_skips_install_with_node_modules():
    def body(layout: _TempLayout):
        repo = layout.init_workspace(
            "p",
            repo_files={
                "package.json": json.dumps({"scripts": {"build": "vite build"}}),
                "node_modules/.keep": "",
            },
        )
        specs = infer_verification_specs(repo)
        assert [s.command for s in specs] == ["npm run build"]

    _run(body)


def test_infer_package_without_build_script_yields_nothing():
    def body(layout: _TempLayout):
        repo = layout.init_workspace(
            "p", repo_files={"package.json": json.dumps({"scripts": {"start": "node x"}})}
        )
        assert infer_verification_specs(repo) == []

    _run(body)


def test_infer_python_tests():
    def body(layout: _TempLayout):
        repo = layout.init_workspace(
            "p", repo_files={"app.py": "x = 1\n", "tests/test_app.py": "def test_x():\n    assert True\n"}
        )
        specs = infer_verification_specs(repo)
        assert [s.command for s in specs] == ["python -m pytest"]
        assert specs[0].kind == "test"

    _run(body)


def test_infer_python_without_tests_syntax_check():
    def body(layout: _TempLayout):
        repo = layout.init_workspace("p", repo_files={"app.py": "x = 1\n"})
        specs = infer_verification_specs(repo)
        assert len(specs) == 1
        assert specs[0].kind == "syntax"
        assert "compileall" in specs[0].command
        # node_modules is excluded so a full-stack repo's vendored .py files
        # can't derail the syntax check.
        assert "node_modules" in specs[0].command

    _run(body)


def test_infer_python_tests_without_pytest_falls_back_to_syntax():
    def body(layout: _TempLayout):
        repo = layout.init_workspace(
            "p", repo_files={"app.py": "x=1\n", "tests/test_app.py": "def test_x():\n    assert True\n"}
        )
        specs = infer_verification_specs(repo, pytest_available=False)
        assert len(specs) == 1
        assert specs[0].kind == "syntax"

    _run(body)


def test_infer_fullstack_runs_both():
    def body(layout: _TempLayout):
        repo = layout.init_workspace(
            "p",
            repo_files={
                "package.json": json.dumps({"scripts": {"build": "vite build"}}),
                "backend/app.py": "x = 1\n",
                "backend/tests/test_app.py": "def test_x():\n    assert True\n",
            },
        )
        specs = infer_verification_specs(repo)
        kinds = [s.kind for s in specs]
        assert "build" in kinds
        assert "test" in kinds

    _run(body)


def test_infer_nothing_for_empty_repo():
    def body(layout: _TempLayout):
        repo = layout.init_workspace("p")
        assert infer_verification_specs(repo) == []

    _run(body)


# ---------- plan_verification ----------


def test_plan_manual_overrides_inference():
    def body(layout: _TempLayout):
        task_md = "## Verification\n\n```bash\necho manual\n```\n"
        layout.init_workspace("p", task_md_body=task_md, repo_files={"app.py": "x=1\n"})
        mode, specs = plan_verification("p", task_md)
        assert mode == "manual"
        assert [s.command for s in specs] == ["echo manual"]

    _run(body)


def test_plan_inferred_when_no_manual_block():
    def body(layout: _TempLayout):
        layout.init_workspace("p", repo_files={"app.py": "x=1\n"})
        mode, specs = plan_verification("p", "# TASK\n")
        assert mode == "inferred"
        assert specs and specs[0].kind == "syntax"

    _run(body)


def test_plan_skipped_when_nothing():
    def body(layout: _TempLayout):
        layout.init_workspace("p")
        mode, specs = plan_verification("p", "# TASK\n")
        assert mode == "skipped"
        assert specs == []

    _run(body)


class _FakeRuntime:
    """Minimal ToolRuntime stand-in for probing pytest availability."""

    def __init__(self, repo_dir: Path, pytest_exit: int) -> None:
        class _S:
            pass

        self.sandbox = _S()
        self.sandbox.repo_dir = repo_dir  # type: ignore[attr-defined]
        self._pytest_exit = pytest_exit
        self.calls: list[str] = []

    def run_shell(self, command: str, timeout_seconds: int = 30):
        from execution.tool_models import ToolResult

        self.calls.append(command)
        return ToolResult(
            success=self._pytest_exit == 0,
            tool_name="run_shell",
            metadata={"exit_code": self._pytest_exit},
        )


def test_plan_falls_back_to_syntax_when_pytest_unavailable():
    def body(layout: _TempLayout):
        repo = layout.init_workspace(
            "p", repo_files={"backend/app.py": "x=1\n", "backend/tests/test_app.py": "def test_x():\n    assert True\n"}
        )
        rt = _FakeRuntime(repo, pytest_exit=1)  # import pytest -> fails
        mode, specs = plan_verification("p", "# TASK\n", runtime=rt)
        assert mode == "inferred"
        assert [s.kind for s in specs] == ["syntax"]
        # The probe was actually run through the shell.
        assert any("import pytest" in c for c in rt.calls)

    _run(body)


def test_plan_uses_pytest_when_available():
    def body(layout: _TempLayout):
        repo = layout.init_workspace(
            "p", repo_files={"app.py": "x=1\n", "tests/test_app.py": "def test_x():\n    assert True\n"}
        )
        rt = _FakeRuntime(repo, pytest_exit=0)  # import pytest -> ok
        mode, specs = plan_verification("p", "# TASK\n", runtime=rt)
        assert mode == "inferred"
        assert [s.kind for s in specs] == ["test"]

    _run(body)


# ---------- run_verification_specs ----------


def test_run_specs_stops_at_first_failure():
    def body(layout: _TempLayout):
        layout.init_workspace("p")
        from execution.verification import VerifyCommandSpec
        from execution.tool_runtime import ToolRuntime

        specs = [
            VerifyCommandSpec('python -c "import sys; sys.exit(1)"', "test", 30),
            VerifyCommandSpec('python -c "import sys; sys.exit(0)"', "build", 30),
        ]
        v = run_verification_specs("p", specs, mode="inferred", runtime=ToolRuntime("p"))
        assert v.status == "failed"
        assert v.mode == "inferred"
        assert len(v.commands) == 2
        assert v.commands[0].status == "failed"
        assert v.commands[1].status == "skipped"  # never ran
        # Aggregate reflects the first failing command.
        assert v.exit_code == 1

    _run(body)


def test_run_specs_all_pass():
    def body(layout: _TempLayout):
        layout.init_workspace("p")
        from execution.verification import VerifyCommandSpec
        from execution.tool_runtime import ToolRuntime

        specs = [
            VerifyCommandSpec('python -c "import sys; sys.exit(0)"', "syntax", 30),
            VerifyCommandSpec('python -c "print(1)"', "test", 30),
        ]
        v = run_verification_specs("p", specs, mode="inferred", runtime=ToolRuntime("p"))
        assert v.status == "passed"
        assert all(c.status == "passed" for c in v.commands)

    _run(body)


# ---------- render ----------


def test_render_multi_command_section():
    v = VerificationResult(
        enabled=True,
        status="failed",
        mode="inferred",
        repair_attempts=1,
        commands=[
            VerificationCommandResult(command="npm install", kind="install", status="passed", exit_code=0),
            VerificationCommandResult(
                command="npm run build", kind="build", status="failed", exit_code=2,
                output_preview="TS2304: cannot find name",
            ),
        ],
    )
    text = render_verification_section(v)
    assert "Mode**: inferred" in text
    assert "Repair attempts**: 1" in text
    assert "npm install" in text
    assert "npm run build" in text
    assert "TS2304" in text


def test_render_skipped_inferred_reason():
    v = VerificationResult(enabled=False, status="skipped", mode="skipped")
    text = render_verification_section(v)
    assert "skipped" in text
    assert "inferred" in text


# ---------- runner integration ----------


def test_runner_inferred_passing_completes():
    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        responses = [
            _tool_call("write_file", path="app.py", content="x = 1\n"),
            _final("completed"),
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(TaskSpec(title="t", task_card="do", created_by="test"))
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "completed"
        assert summary.verification is not None
        assert summary.verification.mode == "inferred"
        assert summary.verification.status == "passed"
        assert summary.verification.commands[0].kind == "syntax"

    _run(body)


def test_runner_inferred_failure_repaired_completes():
    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        bad = "def f(:\n    pass\n"   # syntax error
        good = "def f():\n    pass\n"
        responses = [
            _tool_call("write_file", path="app.py", content=bad),
            _final("completed"),
            # repair pass:
            _tool_call("write_file", path="app.py", content=good),
            _final("completed"),
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(TaskSpec(title="t", task_card="do", created_by="test"))
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "completed"
        assert summary.verification is not None
        assert summary.verification.status == "passed"
        assert summary.verification.repair_attempts == 1

        raw = run_store.read_run_json("p", summary.run_id)
        record = RunRecord(**raw)
        assert record.status == RunStatus.COMPLETED
        assert record.verification_state is None

    _run(body)


def test_runner_inferred_failure_repair_fails_partial():
    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        bad = "def f(:\n    pass\n"
        still_bad = "def g(:\n    pass\n"
        responses = [
            _tool_call("write_file", path="app.py", content=bad),
            _final("completed"),
            # repair pass writes something, but it's still broken:
            _tool_call("write_file", path="app.py", content=still_bad),
            _final("completed"),
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(TaskSpec(title="t", task_card="do", created_by="test"))
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "partial"
        assert summary.verification is not None
        assert summary.verification.status == "failed"
        assert summary.verification.repair_attempts == 1
        assert any("verification failed" in b for b in summary.blockers)

    _run(body)


def test_runner_repair_unavailable_keeps_partial():
    """When the LLM is unavailable for the repair pass, the run still settles
    as partial (no crash, no infinite loop)."""

    def body(layout: _TempLayout):
        layout.make_project("p")
        layout.init_workspace("p")
        bad = "def f(:\n    pass\n"
        # Only enough responses for the build pass; the repair pass's first
        # llm.chat call raises (out of responses), which the runner treats as
        # "repair unavailable".
        responses = [
            _tool_call("write_file", path="app.py", content=bad),
            _final("completed"),
        ]
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller(responses))
        try:
            summary = CodingAgentRunner("p").run_task(TaskSpec(title="t", task_card="do", created_by="test"))
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "partial"
        assert summary.verification.status == "failed"
        assert summary.verification.repair_attempts == 1

    _run(body)


# ---------- runner ----------


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
