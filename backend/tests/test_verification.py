"""Tests for Task 06.2A — command-based verification MVP.

Coverage:

  - parser: missing section, empty fence, all-commented fence, returns
    the first uncommented line, and tolerates a non-fenced section body.
  - run_verification: disabled (skipped), passing command, failing command,
    sandbox rejection (unsafe command), crash safety.
  - render_verification_section: produces an informative block for each
    of the three statuses.
  - runner integration: completed run with a failing verify command is
    downgraded to ``partial``, run.json + result.md both include the
    verification payload, and an already-failed run keeps its status.

Run directly:
    python backend/tests/test_verification.py
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
import execution.runner as runner_mod  # noqa: E402
import execution.run_store as run_store  # noqa: E402
import execution.verification as verification_mod  # noqa: E402
from execution.models import (  # noqa: E402
    RunRecord,
    RunStatus,
    TaskSpec,
    VerificationResult,
)
from execution.runner import CodingAgentRunner  # noqa: E402
from execution.verification import (  # noqa: E402
    parse_verify_command,
    render_verification_section,
    run_verification,
)


# ---------- harness ----------


class _TempLayout:
    """Temporary execution_workspaces/ + projects/ layout with patched globals."""

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


# ---------- parser tests ----------


def test_parser_no_section_returns_none():
    text = "# TASK\n\n## Other\nbody\n"
    assert parse_verify_command(text) is None


def test_parser_empty_fence_returns_none():
    text = "## Verification\n\n```bash\n```\n"
    assert parse_verify_command(text) is None


def test_parser_all_comments_returns_none():
    text = (
        "## Verification\n\n"
        "```bash\n"
        "# leave commented to skip\n"
        "# python -m pytest\n"
        "```\n"
    )
    assert parse_verify_command(text) is None


def test_parser_picks_first_uncommented_line():
    text = (
        "## Verification\n\n"
        "```bash\n"
        "# python -m pytest tests/old\n"
        "python -m pytest tests/new\n"
        "echo something else\n"
        "```\n"
    )
    assert parse_verify_command(text) == "python -m pytest tests/new"


def test_parser_tolerates_no_fence():
    text = (
        "## Verification\n\n"
        "verify_command: npm test --silent\n"
    )
    # Falls back to the first non-comment line of the section body.
    assert parse_verify_command(text) == "verify_command: npm test --silent"


def test_parser_handles_case_insensitive_heading():
    text = "## verification\n\n```bash\nls\n```\n"
    assert parse_verify_command(text) == "ls"


def test_parser_stops_at_next_h2():
    text = (
        "## Verification\n\n"
        "```bash\n"
        "python -m pytest\n"
        "```\n\n"
        "## Something Else\n```bash\nrm -rf /\n```\n"
    )
    assert parse_verify_command(text) == "python -m pytest"


# ---------- run_verification tests ----------


def test_run_verification_skipped_when_no_command():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", task_md_body="# TASK\n")
        v = run_verification("agent-os")
        assert v.enabled is False
        assert v.status == "skipped"
        assert v.command is None
        assert v.exit_code is None

    _run(body)


def test_run_verification_passed_for_zero_exit_command():
    def body(layout: _TempLayout):
        # `python -c "import sys; sys.exit(0)"` runs cross-platform.
        task_md = (
            "## Verification\n\n"
            '```bash\npython -c "import sys; sys.exit(0)"\n```\n'
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        v = run_verification("agent-os")
        assert v.enabled is True
        assert v.status == "passed"
        assert v.exit_code == 0
        assert v.command == 'python -c "import sys; sys.exit(0)"'

    _run(body)


def test_run_verification_failed_for_nonzero_exit_command():
    def body(layout: _TempLayout):
        task_md = (
            "## Verification\n\n"
            '```bash\npython -c "import sys; sys.exit(3)"\n```\n'
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        v = run_verification("agent-os")
        assert v.enabled is True
        assert v.status == "failed"
        assert v.exit_code == 3

    _run(body)


def test_run_verification_blocks_unsafe_command():
    def body(layout: _TempLayout):
        # ``rm -rf /`` is in the sandbox block-list. Verification must
        # surface that as a failure, not run anything.
        task_md = "## Verification\n\n```bash\nrm -rf /\n```\n"
        layout.init_workspace("agent-os", task_md_body=task_md)
        v = run_verification("agent-os")
        assert v.enabled is True
        assert v.status == "failed"
        assert v.exit_code is None
        assert "sandbox" in v.output_preview.lower() or "rejected" in v.output_preview.lower()

    _run(body)


def test_run_verification_truncates_long_output():
    def body(layout: _TempLayout):
        # Emit a large stdout block, then exit 0. Built via python so we
        # don't have to embed 8 kB of X's in the shell command itself.
        task_md = (
            "## Verification\n\n"
            "```bash\n"
            "python -c \"print('X' * 8000)\"\n"
            "```\n"
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        v = run_verification("agent-os")
        assert v.enabled is True
        # The preview should be bounded — either explicitly truncated, or
        # well under twice the cap if the runtime trimmed it upstream.
        assert "truncated" in v.output_preview.lower() or len(v.output_preview) <= 6000

    _run(body)


def test_run_verification_crash_safe(monkeypatch_target_runtime=None):
    """A crashing ToolRuntime must not raise out of run_verification."""

    class _BoomRuntime:
        def run_shell(self, *_args, **_kwargs):
            raise RuntimeError("simulated runtime crash")

    def body(layout: _TempLayout):
        task_md = "## Verification\n\n```bash\nls\n```\n"
        layout.init_workspace("agent-os", task_md_body=task_md)
        v = run_verification("agent-os", runtime=_BoomRuntime())
        assert v.enabled is True
        assert v.status == "failed"
        assert "simulated runtime crash" in v.output_preview

    _run(body)


# ---------- render_verification_section tests ----------


def test_render_section_none_run():
    text = render_verification_section(None)
    assert "Verification" in text
    assert "not run" in text


def test_render_section_disabled():
    v = VerificationResult(enabled=False, status="skipped")
    text = render_verification_section(v)
    assert "skipped" in text
    assert "no verify command configured" in text


def test_render_section_passed_includes_command_and_exit():
    v = VerificationResult(
        enabled=True,
        command="python -m pytest",
        status="passed",
        exit_code=0,
        output_preview="ok",
        duration_ms=42,
    )
    text = render_verification_section(v)
    assert "passed" in text
    assert "python -m pytest" in text
    assert "Exit code" in text
    assert "42 ms" in text


# ---------- runner integration ----------


def _stub_llm_caller(responses: list[str]):
    seq = list(responses)

    def caller(*_args, **_kwargs) -> str:
        if not seq:
            raise AssertionError("LLM caller ran out of stub responses")
        return seq.pop(0)

    return caller


def _patch_llm(monkey: dict[str, Any], caller):
    """Patch llm.chat used by the runner and reconciler."""
    import llm

    monkey["llm_chat"] = llm.chat
    llm.chat = caller  # type: ignore[assignment]


def _unpatch_llm(monkey: dict[str, Any]):
    import llm

    if "llm_chat" in monkey:
        llm.chat = monkey["llm_chat"]


def test_runner_downgrades_completed_to_partial_on_verify_fail():
    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        task_md = (
            "# Task\n\n"
            "## Verification\n\n"
            '```bash\npython -c "import sys; sys.exit(1)"\n```\n'
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        # Runner's final action: completed. We expect verification to flip
        # it to partial.
        final_action = json.dumps({
            "action": "final",
            "status": "completed",
            "summary": "did the thing",
            "files_changed": ["repo/x.py"],
            "commands_run": [],
            "blockers": [],
            "task_md_update": "",
        })
        caller = _stub_llm_caller([final_action])
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, caller)
        try:
            summary = CodingAgentRunner("agent-os").run_task(
                TaskSpec(title="t", task_card="do it", created_by="test"),
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "partial"
        assert summary.verification is not None
        assert summary.verification.status == "failed"
        assert summary.verification.exit_code == 1
        assert any("verification failed" in b for b in summary.blockers)

        # run.json should also reflect the partial + verification payload.
        raw = run_store.read_run_json("agent-os", summary.run_id)
        assert raw is not None
        record = RunRecord(**raw)
        assert record.status == RunStatus.PARTIAL
        assert record.verification is not None
        assert record.verification.status == "failed"

        # result.md should include the Verification section.
        result_md = run_store.read_result_md("agent-os", summary.run_id) or ""
        assert "## Verification" in result_md
        assert "failed" in result_md

    _run(body)


def test_runner_completed_with_no_verify_stays_completed():
    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        layout.init_workspace("agent-os", task_md_body="# TASK\n")
        final_action = json.dumps({
            "action": "final",
            "status": "completed",
            "summary": "did the thing",
            "files_changed": [],
            "commands_run": [],
            "blockers": [],
            "task_md_update": "",
        })
        caller = _stub_llm_caller([final_action])
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, caller)
        try:
            summary = CodingAgentRunner("agent-os").run_task(
                TaskSpec(title="t", task_card="do it", created_by="test"),
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "completed"
        assert summary.verification is not None
        assert summary.verification.enabled is False
        assert summary.verification.status == "skipped"

        result_md = run_store.read_result_md("agent-os", summary.run_id) or ""
        assert "## Verification" in result_md
        assert "skipped" in result_md

    _run(body)


def test_runner_failed_run_keeps_failed_with_verify_skipped():
    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        # Configure a passing verify command — but since the run itself
        # finalizes as 'failed', the status must NOT be promoted upward.
        task_md = (
            "## Verification\n\n"
            '```bash\npython -c "import sys; sys.exit(0)"\n```\n'
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        final_action = json.dumps({
            "action": "final",
            "status": "failed",
            "summary": "didn't work",
            "files_changed": [],
            "commands_run": [],
            "blockers": ["something went wrong"],
            "task_md_update": "",
        })
        caller = _stub_llm_caller([final_action])
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, caller)
        try:
            summary = CodingAgentRunner("agent-os").run_task(
                TaskSpec(title="t", task_card="do it", created_by="test"),
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "failed"
        assert summary.verification is not None
        assert summary.verification.status == "passed"
        # The failed status takes precedence over a passing verification.

    _run(body)


def test_runner_completed_with_passing_verify_stays_completed():
    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        task_md = (
            "## Verification\n\n"
            '```bash\npython -c "import sys; sys.exit(0)"\n```\n'
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        final_action = json.dumps({
            "action": "final",
            "status": "completed",
            "summary": "did the thing",
            "files_changed": [],
            "commands_run": [],
            "blockers": [],
            "task_md_update": "",
        })
        caller = _stub_llm_caller([final_action])
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, caller)
        try:
            summary = CodingAgentRunner("agent-os").run_task(
                TaskSpec(title="t", task_card="do it", created_by="test"),
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "completed"
        assert summary.verification is not None
        assert summary.verification.status == "passed"
        assert summary.verification.exit_code == 0

    _run(body)


def test_runner_records_unsafe_verify_as_failed_and_downgrades():
    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        task_md = "## Verification\n\n```bash\nrm -rf /\n```\n"
        layout.init_workspace("agent-os", task_md_body=task_md)
        final_action = json.dumps({
            "action": "final",
            "status": "completed",
            "summary": "did the thing",
            "files_changed": [],
            "commands_run": [],
            "blockers": [],
            "task_md_update": "",
        })
        caller = _stub_llm_caller([final_action])
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, caller)
        try:
            summary = CodingAgentRunner("agent-os").run_task(
                TaskSpec(title="t", task_card="do it", created_by="test"),
            )
        finally:
            _unpatch_llm(monkey)

        # Unsafe verify command is recorded as failed, which downgrades
        # the completed run to partial.
        assert summary.status == "partial"
        assert summary.verification is not None
        assert summary.verification.status == "failed"
        assert summary.verification.exit_code is None
        assert "sandbox" in summary.verification.output_preview.lower() or \
            "rejected" in summary.verification.output_preview.lower()

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
