"""Tests for Task 06.2D — managed preview server layer + keep-alive handoff.

Coverage:

  - ``start_preview``:
      * returns a running URL only after readiness succeeds
      * a dev server that exits early is torn down and reported as failed
        (no live URL leaks to the UI)
      * a duplicate start while one is already running does NOT spawn a
        second process (``already_running``)
      * sandbox-rejected command fails cleanly without starting anything
  - ``stop_preview``: terminates the managed server and reports stopped
  - ``get_preview_status``: reports running/stopped accurately, reaps a
    dead entry, and surfaces ``has_package_json``
  - ``adopt_preview``: takes ownership; a second adopt while alive is rejected
  - ``shutdown_all_previews``: terminates every managed server
  - keep-alive handoff: a passing ``run_ui_browser_verification`` whose
    registrar adopts the server leaves the dev server RUNNING (not torn down);
    a failed install never reaches the registrar.

Real ``npm`` / Playwright / sockets are never touched — the process starter,
screenshot runner, dependency installer, and readiness probe are stubbed.

Run directly:
    python backend/tests/test_preview.py
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.browser_verification as bv  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
import execution.preview as preview  # noqa: E402
from execution.browser_verification import (  # noqa: E402
    DEFAULT_DEV_URL,
    run_ui_browser_verification,
)


# ---------- harness ----------


class _TempLayout:
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
        # Ensure no registry leakage between tests.
        preview._registry.clear()
        preview._starting.clear()

    def init_workspace(self, project_id: str, *, with_package_json: bool = True) -> Path:
        ws_dir = self.execution_dir / project_id
        repo_dir = ws_dir / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "AGENT.md").write_text("# AGENT\n", encoding="utf-8")
        (ws_dir / "TASK.md").write_text("# TASK\n", encoding="utf-8")
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


class _FakeProc:
    """Minimal subprocess.Popen-compatible stub."""

    def __init__(self, *, stdout: str = "", stderr: str = "", returncode=None) -> None:
        from io import StringIO

        self.pid = 4242
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.stdout = StringIO(stdout)
        self.stderr = StringIO(stderr)
        self._wait_event = threading.Event()
        if returncode is not None:
            self._wait_event.set()

    def poll(self):
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


def _always_ready(_url: str, _timeout: int):
    return True, ""


def _patch_ready():
    prev = bv._wait_for_url
    bv._wait_for_url = _always_ready  # type: ignore[assignment]
    return prev


def _restore_ready(prev):
    bv._wait_for_url = prev  # type: ignore[assignment]


def _writing_screenshot_runner(_url, output_path: Path, _timeout):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"\x89PNG\r\n\x1a\nstub")


def _ok_installer(_cmd, _cwd):
    return 0, "added 12 packages"


# ---------- start_preview ----------


def test_start_preview_returns_url_after_readiness():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        proc = _FakeProc()
        prev = _patch_ready()
        try:
            status = preview.start_preview(
                "p1", process_starter=lambda _c, _w: proc
            )
        finally:
            _restore_ready(prev)
        assert status["ok"] is True
        assert status["running"] is True
        assert status["url"] == DEFAULT_DEV_URL
        # Reflected by a follow-up status call too.
        assert preview.get_preview_status("p1")["running"] is True

    _run(body)


def test_start_preview_early_exit_is_failure_no_live_url():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        # Process already exited (port conflict / missing dep simulation).
        proc = _FakeProc(returncode=1)
        status = preview.start_preview("p1", process_starter=lambda _c, _w: proc)
        assert status["ok"] is False
        assert status["running"] is False
        assert status["url"] is None
        # Nothing registered.
        assert preview.get_preview_status("p1")["running"] is False

    _run(body)


def test_duplicate_start_does_not_spawn_second_process():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        starts: list = []

        def starter(cmd, cwd):
            starts.append(cmd)
            return _FakeProc()

        prev = _patch_ready()
        try:
            first = preview.start_preview("p1", process_starter=starter)
            second = preview.start_preview("p1", process_starter=starter)
        finally:
            _restore_ready(prev)
        assert first["running"] is True
        assert second["running"] is True
        assert second.get("already_running") is True
        # Only one process was ever started.
        assert len(starts) == 1

    _run(body)


def test_start_preview_sandbox_rejects_bad_command():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")

        def starter_must_not_run(_c, _w):
            raise AssertionError("starter must not run for a rejected command")

        status = preview.start_preview(
            "p1", command="npm run dev && git push", process_starter=starter_must_not_run
        )
        assert status["ok"] is False
        assert "sandbox" in status["error"].lower()

    _run(body)


# ---------- stop / status ----------


def test_stop_preview_terminates_and_reports_stopped():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        proc = _FakeProc()
        prev = _patch_ready()
        try:
            preview.start_preview("p1", process_starter=lambda _c, _w: proc)
        finally:
            _restore_ready(prev)
        assert preview.get_preview_status("p1")["running"] is True
        stopped = preview.stop_preview("p1")
        assert stopped["stopped"] is True
        assert stopped["running"] is False
        assert proc.terminated or proc.killed
        assert preview.get_preview_status("p1")["running"] is False

    _run(body)


def test_status_reaps_dead_entry():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        proc = _FakeProc()
        prev = _patch_ready()
        try:
            preview.start_preview("p1", process_starter=lambda _c, _w: proc)
        finally:
            _restore_ready(prev)
        # Process dies on its own.
        proc.returncode = 0
        status = preview.get_preview_status("p1")
        assert status["running"] is False
        assert "p1" not in preview._registry

    _run(body)


def test_status_reports_has_package_json():
    def body(layout: _TempLayout):
        layout.init_workspace("with_pkg", with_package_json=True)
        layout.init_workspace("no_pkg", with_package_json=False)
        assert preview.get_preview_status("with_pkg")["has_package_json"] is True
        assert preview.get_preview_status("no_pkg")["has_package_json"] is False

    _run(body)


# ---------- adopt / shutdown ----------


def test_adopt_then_second_adopt_rejected():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        proc1 = _FakeProc()
        ok1 = preview.adopt_preview("p1", proc1, None, None, "npm run dev", DEFAULT_DEV_URL)
        assert ok1 is True
        assert preview.get_preview_status("p1")["running"] is True
        proc2 = _FakeProc()
        ok2 = preview.adopt_preview("p1", proc2, None, None, "npm run dev", DEFAULT_DEV_URL)
        assert ok2 is False

    _run(body)


def test_shutdown_all_previews_terminates_everything():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        layout.init_workspace("p2")
        proc1, proc2 = _FakeProc(), _FakeProc()
        prev = _patch_ready()
        try:
            preview.start_preview("p1", process_starter=lambda _c, _w: proc1)
            preview.start_preview("p2", process_starter=lambda _c, _w: proc2)
        finally:
            _restore_ready(prev)
        preview.shutdown_all_previews()
        assert proc1.terminated or proc1.killed
        assert proc2.terminated or proc2.killed
        assert preview.get_preview_status("p1")["running"] is False
        assert preview.get_preview_status("p2")["running"] is False

    _run(body)


# ---------- keep-alive handoff via run_ui_browser_verification ----------


def test_keep_alive_registrar_keeps_dev_server_running():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        run_dir = layout.make_run_dir("p1", "r1")
        proc = _FakeProc()

        def registrar(p, sd, ed, command, url):
            return preview.adopt_preview("p1", p, sd, ed, command, url)

        prev = _patch_ready()
        try:
            result = run_ui_browser_verification(
                "p1",
                run_dir=run_dir,
                process_starter=lambda _c, _w: proc,
                screenshot_runner=_writing_screenshot_runner,
                dependency_installer=_ok_installer,
                keep_alive_registrar=registrar,
            )
        finally:
            _restore_ready(prev)

        assert result.status == "passed"
        # The dev server was NOT torn down — it's adopted and still alive.
        assert not proc.terminated and not proc.killed
        assert proc.poll() is None
        assert preview.get_preview_status("p1")["running"] is True
        # Cleanup leaves nothing running.
        preview.stop_preview("p1")
        assert proc.terminated or proc.killed

    _run(body)


def test_failed_install_never_reaches_registrar():
    def body(layout: _TempLayout):
        layout.init_workspace("p1")
        run_dir = layout.make_run_dir("p1", "r1")

        def failing_installer(_cmd, _cwd):
            return 1, "npm ERR! boom"

        def registrar_must_not_run(*_a):
            raise AssertionError("registrar must not run after a failed install")

        result = run_ui_browser_verification(
            "p1",
            run_dir=run_dir,
            process_starter=lambda _c, _w: _FakeProc(),
            screenshot_runner=_writing_screenshot_runner,
            dependency_installer=failing_installer,
            keep_alive_registrar=registrar_must_not_run,
        )
        assert result.status == "failed"
        assert preview.get_preview_status("p1")["running"] is False

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
