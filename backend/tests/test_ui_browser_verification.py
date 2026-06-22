"""Tests for Task 06.2C — user-triggered browser verification flow.

Coverage:

  - ``run_ui_browser_verification``:
      * default dev command + URL (port 5174) when TASK.md has no block
      * an explicit ``## Browser Verification`` block is honored (advanced
        users keep control — no regression of 06.2B's parser path)
      * missing ``package.json`` is handled gracefully (install skipped,
        flow still proceeds)
      * successful install + dev server + screenshot => passed, install
        status passed, screenshot on disk
      * ``npm install`` failure prevents dev-server startup and records a
        clear failure (no screenshot, starter never called)
      * an installer that crashes is recorded as a failed install
  - ``apply_ui_browser_verification_to_record``: status recompute matching
    06.2B (completed -> partial on fail) plus retry restore semantics.
  - end-to-end artifact write (mirrors the endpoint): run.json + result.md
    are updated and ``rerender_result_md`` preserves the original summary.

Playwright and a real ``npm`` are never invoked — the process starter,
screenshot runner, dependency installer, and readiness probe are all
stubbed.

Run directly:
    python backend/tests/test_ui_browser_verification.py
"""

from __future__ import annotations

import json as _json
import os
import sys
import tempfile
import threading
from pathlib import Path

# Make backend/ importable when running this file directly.
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.browser_verification as bv  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution.browser_verification import (  # noqa: E402
    DEFAULT_DEV_COMMAND,
    DEFAULT_DEV_URL,
    apply_ui_browser_verification_to_record,
    run_ui_browser_verification,
)
from execution.visual_judge import run_visual_review  # noqa: E402
from execution.models import (  # noqa: E402
    BrowserVerificationResult,
    RunRecord,
    RunStatus,
)


_VISION_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
)


class _VisionEnv:
    """Set exactly the given vision API keys (clearing the rest) for a block."""

    def __init__(self, **keys: str) -> None:
        self._keys = keys
        self._prev: dict[str, str | None] = {}

    def __enter__(self) -> "_VisionEnv":
        for k in _VISION_ENV_KEYS:
            self._prev[k] = os.environ.get(k)
            os.environ.pop(k, None)
        for k, v in self._keys.items():
            os.environ[k] = v
        return self

    def __exit__(self, *exc) -> None:
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _vision_stub(verdict: str = "passed"):
    def caller(system, prompt, images, model=None, max_tokens=2048):
        return _json.dumps(
            {
                "verdict": verdict,
                "headline": f"{verdict} verdict",
                "reasoning": "stub reasoning",
                "evidence": ["a thing"],
            }
        )

    return caller


# ---------- harness ----------


class _TempLayout:
    """Temporary execution_workspaces/ layout."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.execution_dir = root / "execution_workspaces"
        self.execution_dir.mkdir()
        self._prev_execution = exec_manager._EXECUTION_ROOT
        exec_manager._EXECUTION_ROOT = self.execution_dir

    def cleanup(self) -> None:
        exec_manager._EXECUTION_ROOT = self._prev_execution
        self.tmp.cleanup()

    def init_workspace(
        self,
        project_id: str,
        *,
        task_md_body: str = "# TASK\n",
        with_package_json: bool = True,
    ) -> Path:
        ws_dir = self.execution_dir / project_id
        repo_dir = ws_dir / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "AGENT.md").write_text("# AGENT\n", encoding="utf-8")
        (ws_dir / "TASK.md").write_text(task_md_body, encoding="utf-8")
        (ws_dir / "runs").mkdir(exist_ok=True)
        (ws_dir / "logs").mkdir(exist_ok=True)
        if with_package_json:
            (repo_dir / "package.json").write_text(
                '{"name": "demo", "scripts": {"dev": "vite"}}\n', encoding="utf-8"
            )
        return repo_dir

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


# ---------- fakes ----------


class _FakeProc:
    """Minimal subprocess.Popen-compatible stub."""

    def __init__(self, *, stdout: str = "", stderr: str = "") -> None:
        from io import StringIO

        self.pid = 4242
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.stdout = StringIO(stdout)
        self.stderr = StringIO(stderr)
        self._wait_event = threading.Event()

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0 if self.returncode is None else self.returncode
        self._wait_event.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = 0 if self.returncode is None else self.returncode
        self._wait_event.set()

    def send_signal(self, _sig) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        self._wait_event.wait(timeout=timeout if timeout is not None else 0.01)
        if self.returncode is None:
            raise __import__("subprocess").TimeoutExpired(cmd="fake", timeout=timeout or 0.01)
        return self.returncode


def _writing_screenshot_runner(_url, output_path: Path, _timeout):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"\x89PNG\r\n\x1a\nstub")


def _always_ready(_url: str, _timeout: int) -> tuple[bool, str]:
    return True, ""


def _ok_installer(_cmd, _cwd) -> tuple[int, str]:
    return 0, "added 12 packages"


def _patch_ready():
    prev = bv._wait_for_url
    bv._wait_for_url = _always_ready  # type: ignore[assignment]
    return prev


def _restore_ready(prev):
    bv._wait_for_url = prev  # type: ignore[assignment]


# ---------- run_ui_browser_verification ----------


def test_default_url_and_port_when_no_task_block():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", task_md_body="# TASK\n")
        run_dir = layout.make_run_dir("agent-os", "r1")

        captured: dict = {}

        def starter(cmd, cwd):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            return _FakeProc()

        prev = _patch_ready()
        try:
            result = run_ui_browser_verification(
                "agent-os",
                run_dir=run_dir,
                process_starter=starter,
                screenshot_runner=_writing_screenshot_runner,
                dependency_installer=_ok_installer,
            )
        finally:
            _restore_ready(prev)

        assert result.status == "passed"
        assert result.url == DEFAULT_DEV_URL == "http://127.0.0.1:5174"
        assert result.command == DEFAULT_DEV_COMMAND
        assert "--port 5174" in result.command
        assert "5173" not in (result.url or "")
        # The dev server was started with the default command.
        assert captured["cmd"] == DEFAULT_DEV_COMMAND
        assert result.install_status == "passed"
        assert result.screenshot_path == "screenshots/browser.png"
        assert (run_dir / "screenshots" / "browser.png").exists()

    _run(body)


def test_task_md_block_is_honored_over_default():
    def body(layout: _TempLayout):
        task_md = (
            "## Browser Verification\n\n"
            "```bash\n"
            "npm run dev -- --host 127.0.0.1 --port 4000\n"
            "url: http://127.0.0.1:4000\n"
            "```\n"
        )
        layout.init_workspace("agent-os", task_md_body=task_md)
        run_dir = layout.make_run_dir("agent-os", "r1")

        captured: dict = {}

        def starter(cmd, cwd):
            captured["cmd"] = cmd
            return _FakeProc()

        prev = _patch_ready()
        try:
            result = run_ui_browser_verification(
                "agent-os",
                run_dir=run_dir,
                process_starter=starter,
                screenshot_runner=_writing_screenshot_runner,
                dependency_installer=_ok_installer,
            )
        finally:
            _restore_ready(prev)

        assert result.status == "passed"
        assert result.url == "http://127.0.0.1:4000"
        assert captured["cmd"] == "npm run dev -- --host 127.0.0.1 --port 4000"

    _run(body)


def test_missing_package_json_skips_install_and_still_runs():
    def body(layout: _TempLayout):
        layout.init_workspace(
            "agent-os", task_md_body="# TASK\n", with_package_json=False
        )
        run_dir = layout.make_run_dir("agent-os", "r1")

        def installer_must_not_run(_cmd, _cwd):
            raise AssertionError("installer must not be called without package.json")

        prev = _patch_ready()
        try:
            result = run_ui_browser_verification(
                "agent-os",
                run_dir=run_dir,
                process_starter=lambda _c, _w: _FakeProc(),
                screenshot_runner=_writing_screenshot_runner,
                dependency_installer=installer_must_not_run,
            )
        finally:
            _restore_ready(prev)

        assert result.install_status == "skipped"
        assert result.install_command is None
        # Flow still proceeds to the (mocked) browser verification.
        assert result.status == "passed"

    _run(body)


def test_install_failure_skips_dev_server_and_records_clear_failure():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", task_md_body="# TASK\n")
        run_dir = layout.make_run_dir("agent-os", "r1")

        def failing_installer(_cmd, _cwd):
            return 1, "npm ERR! code ELIFECYCLE\nnpm ERR! build failed"

        def starter_must_not_run(_cmd, _cwd):
            raise AssertionError("dev server must not start after a failed install")

        result = run_ui_browser_verification(
            "agent-os",
            run_dir=run_dir,
            process_starter=starter_must_not_run,
            screenshot_runner=_writing_screenshot_runner,
            dependency_installer=failing_installer,
        )

        assert result.status == "failed"
        assert result.install_status == "failed"
        assert result.screenshot_path is None
        assert not (run_dir / "screenshots" / "browser.png").exists()
        assert "dependency install failed" in result.output_preview
        assert "npm ERR!" in result.install_output_preview

    _run(body)


def test_install_crash_recorded_as_failed():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", task_md_body="# TASK\n")
        run_dir = layout.make_run_dir("agent-os", "r1")

        def crashing_installer(_cmd, _cwd):
            raise RuntimeError("installer blew up")

        result = run_ui_browser_verification(
            "agent-os",
            run_dir=run_dir,
            process_starter=lambda _c, _w: _FakeProc(),
            screenshot_runner=_writing_screenshot_runner,
            dependency_installer=crashing_installer,
        )

        assert result.status == "failed"
        assert result.install_status == "failed"
        assert "installer blew up" in result.install_output_preview
        assert result.screenshot_path is None

    _run(body)


def test_successful_install_dev_server_screenshot_records_passed():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", task_md_body="# TASK\n")
        run_dir = layout.make_run_dir("agent-os", "r1")

        install_calls: list = []

        def installer(cmd, cwd):
            install_calls.append((cmd, cwd))
            return 0, "added 50 packages in 3s"

        proc = _FakeProc()
        prev = _patch_ready()
        try:
            result = run_ui_browser_verification(
                "agent-os",
                run_dir=run_dir,
                process_starter=lambda _c, _w: proc,
                screenshot_runner=_writing_screenshot_runner,
                dependency_installer=installer,
            )
        finally:
            _restore_ready(prev)

        assert result.status == "passed"
        assert result.install_status == "passed"
        assert result.install_command == "npm install"
        assert install_calls and install_calls[0][0] == "npm install"
        assert result.screenshot_path == "screenshots/browser.png"
        assert (run_dir / "screenshots" / "browser.png").exists()
        # Server was torn down.
        assert proc.terminated or proc.killed

    _run(body)


# ---------- apply_ui_browser_verification_to_record ----------


def _bv(status: str, command: str = "npm run dev -- --host 127.0.0.1 --port 5174"):
    return BrowserVerificationResult(
        enabled=True,
        command=command,
        url="http://127.0.0.1:5174",
        status=status,
        screenshot_path="screenshots/browser.png" if status == "passed" else None,
        output_preview="",
        duration_ms=100,
        install_command="npm install",
        install_status="passed",
    )


def _record(status: RunStatus, blockers=None):
    return RunRecord(
        run_id="r1",
        project_id="agent-os",
        task_title="t",
        status=status,
        blockers=list(blockers or []),
    )


def test_completed_downgraded_to_partial_on_browser_fail():
    rec = _record(RunStatus.COMPLETED)
    apply_ui_browser_verification_to_record(rec, _bv("failed"))
    assert rec.status == RunStatus.PARTIAL
    assert any(b.startswith("browser verification failed:") for b in rec.blockers)


def test_completed_stays_completed_on_browser_pass():
    rec = _record(RunStatus.COMPLETED)
    apply_ui_browser_verification_to_record(rec, _bv("passed"))
    assert rec.status == RunStatus.COMPLETED
    assert rec.blockers == []


def test_partial_restored_to_completed_on_pass_when_no_other_blockers():
    # Partial only because a prior browser verification failed.
    rec = _record(
        RunStatus.PARTIAL,
        blockers=["browser verification failed: npm run dev -- --port 5174"],
    )
    apply_ui_browser_verification_to_record(rec, _bv("passed"))
    assert rec.status == RunStatus.COMPLETED
    assert rec.blockers == []


def test_partial_with_other_blocker_stays_partial_on_pass():
    rec = _record(
        RunStatus.PARTIAL,
        blockers=["verification failed: pytest"],  # command-verification blocker
    )
    apply_ui_browser_verification_to_record(rec, _bv("passed"))
    assert rec.status == RunStatus.PARTIAL
    assert rec.blockers == ["verification failed: pytest"]


def test_repeated_failure_does_not_duplicate_blocker():
    rec = _record(RunStatus.COMPLETED)
    apply_ui_browser_verification_to_record(rec, _bv("failed"))
    apply_ui_browser_verification_to_record(rec, _bv("failed"))
    browser_blockers = [
        b for b in rec.blockers if b.startswith("browser verification failed:")
    ]
    assert len(browser_blockers) == 1


# ---------- end-to-end artifact write (mirrors the endpoint) ----------


def test_endpoint_flow_writes_artifacts_and_preserves_summary():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", task_md_body="# TASK\n")
        run_id = "20260604-000000-abcd1234"
        run_dir = layout.make_run_dir("agent-os", run_id)

        # Seed a completed run + its result.md, as the runner would.
        record = RunRecord(
            run_id=run_id,
            project_id="agent-os",
            task_title="Scaffold a vite app",
            status=RunStatus.COMPLETED,
            files_changed=["repo/index.html"],
        )
        run_store.write_run_json("agent-os", run_id, record)
        run_store.write_result_md(
            "agent-os",
            run_id,
            run_store.render_result_md(record, "Created a minimal Vite app."),
        )

        prev = _patch_ready()
        try:
            result = run_ui_browser_verification(
                "agent-os",
                run_dir=run_dir,
                process_starter=lambda _c, _w: _FakeProc(),
                screenshot_runner=_writing_screenshot_runner,
                dependency_installer=_ok_installer,
            )
        finally:
            _restore_ready(prev)

        apply_ui_browser_verification_to_record(record, result)
        run_store.write_run_json("agent-os", run_id, record)
        run_store.rerender_result_md("agent-os", run_id, record)

        # run.json reflects the passed browser verification.
        raw = run_store.read_run_json("agent-os", run_id)
        assert raw is not None
        reloaded = RunRecord(**raw)
        assert reloaded.status == RunStatus.COMPLETED
        assert reloaded.browser_verification is not None
        assert reloaded.browser_verification.status == "passed"
        assert reloaded.browser_verification.install_status == "passed"

        # result.md was re-rendered: keeps the summary, gains the browser
        # verification block with install status.
        result_md = run_store.read_result_md("agent-os", run_id) or ""
        assert "Created a minimal Vite app." in result_md
        assert "## Browser Verification" in result_md
        assert "passed" in result_md
        assert "Dependency install" in result_md

    _run(body)


# ---------- visual review integration (mirrors the endpoint) ----------


def _verified_run(layout: _TempLayout, run_id: str):
    """Run a passing UI browser verification and return (record, result, run_dir)."""
    layout.init_workspace("agent-os", task_md_body="# TASK\n")
    run_dir = layout.make_run_dir("agent-os", run_id)
    record = RunRecord(
        run_id=run_id,
        project_id="agent-os",
        task_title="Build a dashboard",
        status=RunStatus.COMPLETED,
        summary="built it",
    )
    prev = _patch_ready()
    try:
        result = run_ui_browser_verification(
            "agent-os",
            run_dir=run_dir,
            process_starter=lambda _c, _w: _FakeProc(),
            screenshot_runner=_writing_screenshot_runner,
            dependency_installer=_ok_installer,
        )
    finally:
        _restore_ready(prev)
    apply_ui_browser_verification_to_record(record, result)
    return record, result, run_dir


def test_visual_review_runs_on_pass_without_changing_status():
    def body(layout: _TempLayout):
        record, result, run_dir = _verified_run(layout, "r-vr1")
        assert result.status == "passed"
        with _VisionEnv(ANTHROPIC_API_KEY="test"):
            record.visual_review = run_visual_review(
                "agent-os",
                "r-vr1",
                task_card="build a dashboard",
                summary=record.summary,
                browser_result=result,
                run_dir=run_dir,
                vision_caller=_vision_stub("passed"),
            )
        assert record.visual_review is not None
        assert record.visual_review.status == "passed"
        # Diagnostic-only: the run status is untouched by the verdict.
        assert record.status == RunStatus.COMPLETED
        assert (run_dir / "visual_review.json").exists()

    _run(body)


def test_visual_review_failed_verdict_does_not_downgrade():
    def body(layout: _TempLayout):
        record, result, run_dir = _verified_run(layout, "r-vr2")
        with _VisionEnv(ANTHROPIC_API_KEY="test"):
            record.visual_review = run_visual_review(
                "agent-os",
                "r-vr2",
                task_card="build a dashboard",
                summary=record.summary,
                browser_result=result,
                run_dir=run_dir,
                vision_caller=_vision_stub("failed"),
            )
        assert record.visual_review.status == "failed"
        # A failed visual verdict must NOT downgrade the run or add a blocker.
        assert record.status == RunStatus.COMPLETED
        assert not any("visual" in b.lower() for b in record.blockers)

    _run(body)


def test_visual_review_skips_without_vision_key():
    def body(layout: _TempLayout):
        record, result, run_dir = _verified_run(layout, "r-vr3")
        with _VisionEnv():  # no vision keys configured
            review = run_visual_review(
                "agent-os",
                "r-vr3",
                task_card="build a dashboard",
                summary=record.summary,
                browser_result=result,
                run_dir=run_dir,
                vision_caller=_vision_stub("passed"),
            )
        assert review.status == "skipped"
        assert review.skipped_reason
        assert record.status == RunStatus.COMPLETED

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
