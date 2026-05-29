"""Tests for Task 06.2B — opt-in browser-based verification.

Coverage:

  - parser: missing section, no fence, missing url, missing command,
    picks first uncommented command + first url, tolerates case-
    insensitive heading.
  - run_browser_verification: skipped when no config, success path with
    mocked process + screenshot runner, dev-server-fails path (process
    dies before url is reachable), unreachable url path, sandbox-rejected
    command, screenshot runner failure, cleanup always runs.
  - render_browser_verification_section: produces an informative block
    for each status.
  - runner integration: completed run with a failing browser verification
    is downgraded to ``partial``, run.json + result.md both include the
    browser verification payload, and an already-failed run keeps its
    status.

Playwright is NOT imported. The default screenshot runner is replaced
with a stub for every test that exercises the lifecycle.

Run directly:
    python backend/tests/test_browser_verification.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

# Make backend/ importable when running this file directly.
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.browser_verification as bv  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
import execution.memory_reconciliation as mr  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution.browser_verification import (  # noqa: E402
    BrowserVerificationConfig,
    parse_browser_verification,
    render_browser_verification_section,
    run_browser_verification,
)
from execution.models import (  # noqa: E402
    BrowserVerificationResult,
    RunRecord,
    RunStatus,
    TaskSpec,
)
from execution.runner import CodingAgentRunner  # noqa: E402


# ---------- harness ----------


class _TempLayout:
    """Temporary execution_workspaces/ + projects/ layout."""

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

    def make_run_dir(self, project_id: str, run_id: str) -> Path:
        d = self.execution_dir / project_id / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d


def _run(test_body):
    layout = _TempLayout()
    try:
        test_body(layout)
    finally:
        layout.cleanup()


# ---------- fake process / fake screenshot runner ----------


class _FakeProc:
    """Minimal subprocess.Popen-compatible stub for tests.

    ``exits_after`` simulates an early-exit dev server (returns the given
    return code on poll() after the first call). Otherwise stays
    "running" until ``terminate()`` or ``kill()`` is called.
    """

    def __init__(
        self,
        *,
        exits_after: int | None = None,
        stdout: str = "",
        stderr: str = "",
        return_code: int = 0,
    ) -> None:
        self.pid = 12345
        self.returncode: int | None = None
        self._poll_count = 0
        self._exits_after = exits_after
        self._return_code = return_code
        self.terminated = False
        self.killed = False
        # Use a simple in-memory stream so .read() works.
        from io import StringIO
        self.stdout = StringIO(stdout)
        self.stderr = StringIO(stderr)
        self._wait_event = threading.Event()

    def poll(self) -> int | None:
        if self._exits_after is not None:
            self._poll_count += 1
            if self._poll_count >= self._exits_after:
                self.returncode = self._return_code
                return self._return_code
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._return_code if self.returncode is None else self.returncode
        self._wait_event.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = self._return_code if self.returncode is None else self.returncode
        self._wait_event.set()

    def send_signal(self, _sig) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        self._wait_event.wait(timeout=timeout if timeout is not None else 0.01)
        if self.returncode is None:
            raise __import__("subprocess").TimeoutExpired(cmd="fake", timeout=timeout or 0.01)
        return self.returncode


def _fake_starter_factory(proc: _FakeProc):
    def starter(_cmd, _cwd):
        return proc
    return starter


def _writing_screenshot_runner(_url, output_path: Path, _timeout):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"\x89PNG\r\n\x1a\nstub")


def _failing_screenshot_runner(_url, _output_path, _timeout):
    raise RuntimeError("simulated screenshot failure")


def _always_ready(_url: str, _timeout: int) -> tuple[bool, str]:
    return True, ""


def _never_ready(_url: str, _timeout: int) -> tuple[bool, str]:
    return False, "connection refused"


# ---------- parser tests ----------


def test_parser_no_section_returns_none():
    text = "# TASK\n\n## Other\nbody\n"
    assert parse_browser_verification(text) is None


def test_parser_no_fence_no_command_returns_none():
    text = "## Browser Verification\n\n_(nothing here)_\n"
    assert parse_browser_verification(text) is None


def test_parser_missing_url_returns_none():
    text = (
        "## Browser Verification\n\n"
        "```bash\n"
        "npm run dev -- --host 127.0.0.1\n"
        "```\n"
    )
    assert parse_browser_verification(text) is None


def test_parser_missing_command_returns_none():
    text = (
        "## Browser Verification\n\n"
        "```bash\n"
        "url: http://127.0.0.1:5173\n"
        "```\n"
    )
    assert parse_browser_verification(text) is None


def test_parser_picks_first_uncommented_command_and_url():
    text = (
        "## Browser Verification\n\n"
        "```bash\n"
        "# commented out\n"
        "npm run dev -- --host 127.0.0.1\n"
        "url: http://127.0.0.1:5173\n"
        "```\n"
    )
    cfg = parse_browser_verification(text)
    assert cfg is not None
    assert cfg.command == "npm run dev -- --host 127.0.0.1"
    assert cfg.url == "http://127.0.0.1:5173"


def test_parser_case_insensitive_heading_and_url():
    text = (
        "## browser verification\n\n"
        "```bash\n"
        "npm run dev\n"
        "URL: http://localhost:3000\n"
        "```\n"
    )
    cfg = parse_browser_verification(text)
    assert cfg is not None
    assert cfg.url == "http://localhost:3000"


def test_parser_tolerates_no_fence():
    text = (
        "## Browser Verification\n\n"
        "npm run dev\n"
        "url: http://127.0.0.1:5173\n"
    )
    cfg = parse_browser_verification(text)
    assert cfg is not None
    assert cfg.command == "npm run dev"
    assert cfg.url == "http://127.0.0.1:5173"


def test_parser_stops_at_next_h2():
    text = (
        "## Browser Verification\n\n"
        "```bash\nnpm run dev\nurl: http://127.0.0.1:5173\n```\n\n"
        "## Other\n```bash\nrm -rf /\nurl: http://evil\n```\n"
    )
    cfg = parse_browser_verification(text)
    assert cfg is not None
    assert cfg.command == "npm run dev"


# ---------- run_browser_verification tests ----------


def test_skipped_when_no_config():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", task_md_body="# TASK\n")
        run_dir = layout.make_run_dir("agent-os", "r1")
        result = run_browser_verification("agent-os", run_dir=run_dir)
        assert result.enabled is False
        assert result.status == "skipped"
        assert result.command is None
        assert result.url is None
        assert result.screenshot_path is None

    _run(body)


def test_success_path_records_passed_and_screenshot(monkeypatch_helper=None):
    """Happy path: dev server starts, URL becomes ready, screenshot is written."""

    def body(layout: _TempLayout):
        task_md = (
            "## Browser Verification\n\n"
            "```bash\n"
            "npm run dev\n"
            "url: http://127.0.0.1:5173\n"
            "```\n"
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        run_dir = layout.make_run_dir("agent-os", "r1")

        proc = _FakeProc()
        # Patch the readiness probe so we don't actually open a socket.
        prev_wait = bv._wait_for_url
        bv._wait_for_url = _always_ready  # type: ignore[assignment]
        try:
            result = run_browser_verification(
                "agent-os",
                run_dir=run_dir,
                process_starter=_fake_starter_factory(proc),
                screenshot_runner=_writing_screenshot_runner,
            )
        finally:
            bv._wait_for_url = prev_wait  # type: ignore[assignment]

        assert result.enabled is True
        assert result.status == "passed"
        assert result.command == "npm run dev"
        assert result.url == "http://127.0.0.1:5173"
        assert result.screenshot_path == "screenshots/browser.png"
        assert (run_dir / "screenshots" / "browser.png").exists()
        # Server was torn down.
        assert proc.terminated or proc.killed

    _run(body)


def test_unreachable_url_records_failed_and_cleans_up():
    def body(layout: _TempLayout):
        task_md = (
            "## Browser Verification\n\n"
            "```bash\nnpm run dev\nurl: http://127.0.0.1:5173\n```\n"
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        run_dir = layout.make_run_dir("agent-os", "r1")

        proc = _FakeProc()
        prev_wait = bv._wait_for_url
        bv._wait_for_url = _never_ready  # type: ignore[assignment]
        try:
            result = run_browser_verification(
                "agent-os",
                run_dir=run_dir,
                process_starter=_fake_starter_factory(proc),
                screenshot_runner=_writing_screenshot_runner,
                readiness_timeout_seconds=1,
            )
        finally:
            bv._wait_for_url = prev_wait  # type: ignore[assignment]

        assert result.status == "failed"
        assert result.screenshot_path is None
        assert "did not become reachable" in result.output_preview
        # Cleanup ran.
        assert proc.terminated or proc.killed

    _run(body)


def test_dev_server_crash_recorded_as_failed():
    def body(layout: _TempLayout):
        task_md = (
            "## Browser Verification\n\n"
            "```bash\nnpm run dev\nurl: http://127.0.0.1:5173\n```\n"
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        run_dir = layout.make_run_dir("agent-os", "r1")

        proc = _FakeProc(
            exits_after=1,
            stderr="port already in use",
            return_code=1,
        )
        prev_wait = bv._wait_for_url
        bv._wait_for_url = _never_ready  # type: ignore[assignment]
        try:
            result = run_browser_verification(
                "agent-os",
                run_dir=run_dir,
                process_starter=_fake_starter_factory(proc),
                screenshot_runner=_writing_screenshot_runner,
                readiness_timeout_seconds=1,
            )
        finally:
            bv._wait_for_url = prev_wait  # type: ignore[assignment]

        assert result.status == "failed"
        assert (
            "exited before url was reachable" in result.output_preview
            or "did not become reachable" in result.output_preview
        )

    _run(body)


def test_drainer_surfaces_server_output_on_unreachable_url():
    """When the dev server is still alive but the URL never responds, we
    must surface what the server printed — that's the only diagnostic
    that tells the operator whether vite is mid-startup, blocked on a
    prompt, or hitting a config error. Regression guard for the
    Windows-pipe-buffer-deadlock fix.
    """

    def body(layout: _TempLayout):
        task_md = (
            "## Browser Verification\n\n"
            "```bash\nnpm run dev\nurl: http://127.0.0.1:5173\n```\n"
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        run_dir = layout.make_run_dir("agent-os", "r1")

        # Process stays running (no exits_after) but prints noisy startup
        # logs — the drainer must pull them off the pipe even though the
        # caller never reads `.stdout` / `.stderr` directly.
        proc = _FakeProc(
            stdout="vite optimizing deps\n[error] some plugin yelled\n",
            stderr="Browserslist: caniuse-lite is outdated\n",
        )
        prev_wait = bv._wait_for_url
        bv._wait_for_url = _never_ready  # type: ignore[assignment]
        try:
            result = run_browser_verification(
                "agent-os",
                run_dir=run_dir,
                process_starter=_fake_starter_factory(proc),
                screenshot_runner=_writing_screenshot_runner,
                readiness_timeout_seconds=1,
            )
        finally:
            bv._wait_for_url = prev_wait  # type: ignore[assignment]

        assert result.status == "failed"
        # Drained stdout/stderr should both appear in the preview so the
        # operator can see what vite actually printed.
        assert "vite optimizing deps" in result.output_preview
        assert "Browserslist: caniuse-lite" in result.output_preview
        # Diagnostic phase is now explicit in the message.
        assert "did not become reachable" in result.output_preview
        assert "dev server still running" in result.output_preview

    _run(body)


def test_sandbox_rejected_command_recorded_as_failed():
    def body(layout: _TempLayout):
        task_md = (
            "## Browser Verification\n\n"
            "```bash\nrm -rf /\nurl: http://127.0.0.1:5173\n```\n"
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        run_dir = layout.make_run_dir("agent-os", "r1")
        result = run_browser_verification(
            "agent-os",
            run_dir=run_dir,
            screenshot_runner=_writing_screenshot_runner,
        )
        assert result.enabled is True
        assert result.status == "failed"
        assert "sandbox" in result.output_preview.lower()
        assert result.screenshot_path is None

    _run(body)


def test_screenshot_failure_recorded_and_cleanup_runs():
    def body(layout: _TempLayout):
        task_md = (
            "## Browser Verification\n\n"
            "```bash\nnpm run dev\nurl: http://127.0.0.1:5173\n```\n"
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        run_dir = layout.make_run_dir("agent-os", "r1")

        proc = _FakeProc()
        prev_wait = bv._wait_for_url
        bv._wait_for_url = _always_ready  # type: ignore[assignment]
        try:
            result = run_browser_verification(
                "agent-os",
                run_dir=run_dir,
                process_starter=_fake_starter_factory(proc),
                screenshot_runner=_failing_screenshot_runner,
            )
        finally:
            bv._wait_for_url = prev_wait  # type: ignore[assignment]

        assert result.status == "failed"
        assert "simulated screenshot failure" in result.output_preview
        # Cleanup still ran even though screenshot failed.
        assert proc.terminated or proc.killed
        assert result.screenshot_path is None

    _run(body)


def test_screenshot_messageless_exception_does_not_render_blank_colon():
    """A messageless exception (the shape of ``NotImplementedError()``
    raised by Windows ``SelectorEventLoop`` subprocess methods) must still
    produce an informative preview — not a dangling ``ExceptionType:``
    with no content after the colon.
    """

    def body(layout: _TempLayout):
        task_md = (
            "## Browser Verification\n\n"
            "```bash\nnpm run dev\nurl: http://127.0.0.1:5173\n```\n"
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        run_dir = layout.make_run_dir("agent-os", "r1")

        def messageless_runner(_url, _output_path, _timeout):
            raise NotImplementedError()  # no message, asyncio-on-Windows style

        proc = _FakeProc()
        prev_wait = bv._wait_for_url
        bv._wait_for_url = _always_ready  # type: ignore[assignment]
        try:
            result = run_browser_verification(
                "agent-os",
                run_dir=run_dir,
                process_starter=_fake_starter_factory(proc),
                screenshot_runner=messageless_runner,
            )
        finally:
            bv._wait_for_url = prev_wait  # type: ignore[assignment]

        assert result.status == "failed"
        # The class name must appear, and the trailing dangling ":\n" or
        # ": " (with nothing after) must NOT appear.
        assert "NotImplementedError" in result.output_preview
        assert "screenshot capture failed" in result.output_preview
        # Must NOT end with "NotImplementedError:" — repr() form has "()" instead.
        assert "NotImplementedError()" in result.output_preview

    _run(body)


def test_default_runner_clean_error_when_playwright_missing():
    """When the default screenshot runner can't import playwright, it
    must surface a clear, actionable diagnostic — not a blank or
    cryptic stack trace.
    """
    import execution.browser_verification as bv_mod

    # Force the inline subprocess script to fail with the "not installed"
    # signal by pointing sys.executable at a Python that has no playwright.
    # We achieve that hermetically by running the script with a -c that
    # short-circuits the import, but the simpler path is to monkey-patch
    # ``_PLAYWRIGHT_SCREENSHOT_SCRIPT`` to a stub that exits 2 with a
    # structured error blob.
    prev_script = bv_mod._PLAYWRIGHT_SCREENSHOT_SCRIPT
    bv_mod._PLAYWRIGHT_SCREENSHOT_SCRIPT = (
        "import sys; sys.stderr.write("
        "'{\"error\": \"playwright_not_installed\", \"message\": "
        "\"No module named playwright\"}'); sys.exit(2)"
    )
    try:
        try:
            bv_mod._default_playwright_screenshot(
                "http://127.0.0.1:5173",
                Path(tempfile.mkdtemp()) / "out.png",
                timeout_seconds=5,
            )
        except RuntimeError as exc:
            msg = str(exc)
            assert "Playwright is not installed" in msg
            assert "pip install playwright" in msg
            assert "playwright install chromium" in msg
        else:
            raise AssertionError("expected RuntimeError, got none")
    finally:
        bv_mod._PLAYWRIGHT_SCREENSHOT_SCRIPT = prev_script


def test_default_runner_clean_error_when_chromium_missing():
    """When Playwright is installed but Chromium isn't, the surfaced
    message must point the operator at ``playwright install chromium``.
    """
    import execution.browser_verification as bv_mod

    prev_script = bv_mod._PLAYWRIGHT_SCREENSHOT_SCRIPT
    bv_mod._PLAYWRIGHT_SCREENSHOT_SCRIPT = (
        "import sys; sys.stderr.write("
        "'{\"error\": \"chromium_not_installed\", \"message\": "
        "\"Executable doesnt exist at /tmp/.cache/ms-playwright/...\"}'); "
        "sys.exit(3)"
    )
    try:
        try:
            bv_mod._default_playwright_screenshot(
                "http://127.0.0.1:5173",
                Path(tempfile.mkdtemp()) / "out.png",
                timeout_seconds=5,
            )
        except RuntimeError as exc:
            msg = str(exc)
            assert "Chromium is not available" in msg or "Playwright/Chromium" in msg
            assert "playwright install chromium" in msg
        else:
            raise AssertionError("expected RuntimeError, got none")
    finally:
        bv_mod._PLAYWRIGHT_SCREENSHOT_SCRIPT = prev_script


def test_process_start_failure_recorded_as_failed():
    def body(layout: _TempLayout):
        task_md = (
            "## Browser Verification\n\n"
            "```bash\nnpm run dev\nurl: http://127.0.0.1:5173\n```\n"
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        run_dir = layout.make_run_dir("agent-os", "r1")

        def starter(_cmd, _cwd):
            raise FileNotFoundError("npm not found")

        result = run_browser_verification(
            "agent-os",
            run_dir=run_dir,
            process_starter=starter,
            screenshot_runner=_writing_screenshot_runner,
        )
        assert result.status == "failed"
        assert "failed to start dev server" in result.output_preview
        assert "npm not found" in result.output_preview

    _run(body)


# ---------- render tests ----------


def test_render_section_none_run():
    text = render_browser_verification_section(None)
    assert "Browser Verification" in text
    assert "not run" in text


def test_render_section_disabled():
    r = BrowserVerificationResult(enabled=False, status="skipped")
    text = render_browser_verification_section(r)
    assert "skipped" in text
    assert "no browser verification" in text.lower()


def test_render_section_passed_includes_command_url_screenshot():
    r = BrowserVerificationResult(
        enabled=True,
        command="npm run dev",
        url="http://127.0.0.1:5173",
        status="passed",
        screenshot_path="screenshots/browser.png",
        output_preview="screenshot captured",
        duration_ms=1234,
    )
    text = render_browser_verification_section(r)
    assert "passed" in text
    assert "npm run dev" in text
    assert "127.0.0.1:5173" in text
    assert "screenshots/browser.png" in text
    assert "1234 ms" in text


# ---------- runner integration ----------


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


def _patch_browser_verification_to(stub):
    """Swap ``run_browser_verification`` used by the runner. Returns the
    previous symbol so callers can restore it."""
    import execution.runner as runner_mod
    prev = runner_mod.run_browser_verification
    runner_mod.run_browser_verification = stub  # type: ignore[assignment]
    return prev


def _restore_browser_verification(prev):
    import execution.runner as runner_mod
    runner_mod.run_browser_verification = prev  # type: ignore[assignment]


def test_runner_downgrades_completed_to_partial_on_browser_fail():
    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        task_md = (
            "# Task\n\n"
            "## Browser Verification\n\n"
            "```bash\nnpm run dev\nurl: http://127.0.0.1:5173\n```\n"
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
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

        def stub_browser(*_args, **_kwargs):
            return BrowserVerificationResult(
                enabled=True,
                command="npm run dev",
                url="http://127.0.0.1:5173",
                status="failed",
                screenshot_path=None,
                output_preview="url did not become reachable",
                duration_ms=500,
            )

        prev = _patch_browser_verification_to(stub_browser)
        try:
            summary = CodingAgentRunner("agent-os").run_task(
                TaskSpec(title="t", task_card="do it", created_by="test"),
            )
        finally:
            _unpatch_llm(monkey)
            _restore_browser_verification(prev)

        assert summary.status == "partial"
        assert summary.browser_verification is not None
        assert summary.browser_verification.status == "failed"
        assert any("browser verification failed" in b for b in summary.blockers)

        # run.json should also reflect partial + the browser payload.
        raw = run_store.read_run_json("agent-os", summary.run_id)
        assert raw is not None
        record = RunRecord(**raw)
        assert record.status == RunStatus.PARTIAL
        assert record.browser_verification is not None
        assert record.browser_verification.status == "failed"

        # result.md should include the Browser Verification section.
        result_md = run_store.read_result_md("agent-os", summary.run_id) or ""
        assert "## Browser Verification" in result_md
        assert "failed" in result_md

    _run(body)


def test_runner_completed_with_no_browser_config_stays_completed():
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
        assert summary.browser_verification is not None
        assert summary.browser_verification.enabled is False
        assert summary.browser_verification.status == "skipped"

        result_md = run_store.read_result_md("agent-os", summary.run_id) or ""
        assert "## Browser Verification" in result_md
        assert "skipped" in result_md.lower()

    _run(body)


def test_runner_failed_run_keeps_failed_even_when_browser_passes():
    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        task_md = (
            "## Browser Verification\n\n"
            "```bash\nnpm run dev\nurl: http://127.0.0.1:5173\n```\n"
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

        def stub_browser(*_args, **_kwargs):
            return BrowserVerificationResult(
                enabled=True,
                command="npm run dev",
                url="http://127.0.0.1:5173",
                status="passed",
                screenshot_path="screenshots/browser.png",
                output_preview="ok",
                duration_ms=400,
            )

        prev = _patch_browser_verification_to(stub_browser)
        try:
            summary = CodingAgentRunner("agent-os").run_task(
                TaskSpec(title="t", task_card="do it", created_by="test"),
            )
        finally:
            _unpatch_llm(monkey)
            _restore_browser_verification(prev)

        assert summary.status == "failed"
        assert summary.browser_verification is not None
        assert summary.browser_verification.status == "passed"

    _run(body)


def test_runner_completed_with_passing_browser_stays_completed_and_records_payload():
    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        task_md = (
            "## Browser Verification\n\n"
            "```bash\nnpm run dev\nurl: http://127.0.0.1:5173\n```\n"
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

        def stub_browser(*_args, **_kwargs):
            return BrowserVerificationResult(
                enabled=True,
                command="npm run dev",
                url="http://127.0.0.1:5173",
                status="passed",
                screenshot_path="screenshots/browser.png",
                output_preview="ok",
                duration_ms=400,
            )

        prev = _patch_browser_verification_to(stub_browser)
        try:
            summary = CodingAgentRunner("agent-os").run_task(
                TaskSpec(title="t", task_card="do it", created_by="test"),
            )
        finally:
            _unpatch_llm(monkey)
            _restore_browser_verification(prev)

        assert summary.status == "completed"
        assert summary.browser_verification is not None
        assert summary.browser_verification.status == "passed"
        assert summary.browser_verification.screenshot_path == "screenshots/browser.png"

        raw = run_store.read_run_json("agent-os", summary.run_id)
        assert raw is not None
        record = RunRecord(**raw)
        assert record.browser_verification is not None
        assert record.browser_verification.status == "passed"
        assert record.browser_verification.screenshot_path == "screenshots/browser.png"

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
