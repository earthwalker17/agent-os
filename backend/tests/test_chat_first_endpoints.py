"""Tests for Task 06.2D — chat-first run endpoints (HTTP level).

Exercises the FastAPI surface with stubbed verification so no real npm /
Playwright / sockets run:

  - ``POST .../runs/{id}/browser-verify``:
      * rejects a non-terminal run with 409
      * flips ``browser_verification_state`` to ``"running"`` BEFORE the
        blocking verification (a concurrent poll would see the run active
        again), then settles it to the terminal status
      * keeps the screenshot artifact path on the returned record
  - ``/preview/start`` + ``/preview/status`` + ``/preview/stop``:
      * start reports a running URL, status agrees, duplicate start does not
        spawn a second process, stop terminates it

Run directly:
    python backend/tests/test_chat_first_endpoints.py
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
import execution.preview as preview  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution.models import (  # noqa: E402
    BrowserVerificationResult,
    RunRecord,
    RunStatus,
)


class _Env:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects_dir = root / "projects"
        self.execution_dir = root / "execution_workspaces"
        self.projects_dir.mkdir()
        self.execution_dir.mkdir()
        self._prev_projects = main.PROJECTS_DIR
        self._prev_exec = exec_manager._EXECUTION_ROOT
        main.PROJECTS_DIR = self.projects_dir
        exec_manager._EXECUTION_ROOT = self.execution_dir
        self.client = TestClient(main.app)

    def cleanup(self) -> None:
        main.PROJECTS_DIR = self._prev_projects
        exec_manager._EXECUTION_ROOT = self._prev_exec
        preview._registry.clear()
        preview._starting.clear()
        self.tmp.cleanup()

    def make_project(self, pid: str, *, with_package_json: bool = True) -> None:
        (self.projects_dir / pid).mkdir(parents=True, exist_ok=True)
        (self.projects_dir / pid / "PROJECT.md").write_text(f"# {pid}\n", encoding="utf-8")
        ws = self.execution_dir / pid
        repo = ws / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        (ws / "AGENT.md").write_text("# A\n", encoding="utf-8")
        (ws / "TASK.md").write_text("# T\n", encoding="utf-8")
        (ws / "runs").mkdir(exist_ok=True)
        (ws / "logs").mkdir(exist_ok=True)
        if with_package_json:
            (repo / "package.json").write_text('{"name":"d"}\n', encoding="utf-8")

    def seed_completed_run(self, pid: str, run_id: str) -> None:
        run_store.init_run_dir(pid, run_id)
        rec = RunRecord(
            run_id=run_id,
            project_id=pid,
            task_title="Scaffold a vite app",
            status=RunStatus.COMPLETED,
            files_changed=["repo/index.html"],
            summary="Created a minimal Vite app.",
        )
        run_store.write_run_json(pid, run_id, rec)
        run_store.write_result_md(pid, run_id, run_store.render_result_md(rec, rec.summary))


class _FakeProc:
    def __init__(self) -> None:
        from io import StringIO

        self.pid = 99
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.stdout = StringIO("")
        self.stderr = StringIO("")
        self._e = threading.Event()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        self._e.set()

    def kill(self):
        self.killed = True
        self.returncode = 0
        self._e.set()

    def send_signal(self, _s):
        self.terminate()

    def wait(self, timeout=None):
        self._e.wait(timeout or 0.01)
        if self.returncode is None:
            raise __import__("subprocess").TimeoutExpired(cmd="f", timeout=timeout or 0.01)
        return self.returncode


def _run(body):
    env = _Env()
    try:
        body(env)
    finally:
        env.cleanup()


# ---------- browser-verify endpoint ----------


def test_browser_verify_rejects_non_terminal_run():
    def body(env: _Env):
        env.make_project("p1")
        run_store.init_run_dir("p1", "r1")
        rec = RunRecord(run_id="r1", project_id="p1", task_title="t", status=RunStatus.RUNNING)
        run_store.write_run_json("p1", "r1", rec)
        res = env.client.post("/api/projects/p1/execution/runs/r1/browser-verify")
        assert res.status_code == 409

    _run(body)


def test_browser_verify_marks_running_then_settles_and_keeps_screenshot():
    def body(env: _Env):
        env.make_project("p1")
        env.seed_completed_run("p1", "r1")

        seen_state: dict = {}

        def fake_verify(project_id, *, run_dir, keep_alive_registrar=None):
            # The endpoint must have flipped the sub-status to "running"
            # BEFORE calling us (so a concurrent poll sees the run active).
            raw = run_store.read_run_json(project_id, "r1")
            seen_state["state_during"] = raw.get("browser_verification_state")
            # Write a screenshot artifact like the real runner would.
            shot = Path(run_dir) / "screenshots" / "browser.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            shot.write_bytes(b"\x89PNG\r\n\x1a\nstub")
            return BrowserVerificationResult(
                enabled=True,
                command="npm run dev -- --host 127.0.0.1 --port 5174",
                url="http://127.0.0.1:5174",
                status="passed",
                screenshot_path="screenshots/browser.png",
                install_command="npm install",
                install_status="passed",
                duration_ms=120,
            )

        prev = main.run_ui_browser_verification
        main.run_ui_browser_verification = fake_verify
        try:
            res = env.client.post("/api/projects/p1/execution/runs/r1/browser-verify")
        finally:
            main.run_ui_browser_verification = prev

        assert res.status_code == 200
        body_json = res.json()
        # During the blocking call the run was marked verifying.
        assert seen_state["state_during"] == "running"
        # After it settles, the sub-status is the terminal verification status.
        assert body_json["browser_verification_state"] == "passed"
        assert body_json["status"] == "completed"
        # Artifact path survives on the record (modal still references it).
        assert body_json["browser_verification"]["screenshot_path"] == "screenshots/browser.png"
        # And the file is actually on disk + served by the screenshot endpoint.
        shot = env.client.get("/api/projects/p1/execution/runs/r1/screenshot")
        assert shot.status_code == 200

    _run(body)


# ---------- preview endpoints ----------


def test_preview_start_status_duplicate_stop():
    def body(env: _Env):
        env.make_project("p1")
        procs: list = []

        def starter(cmd, cwd):
            p = _FakeProc()
            procs.append(p)
            return p

        # readiness probe: patch browser_verification._wait_for_url
        import execution.browser_verification as bv

        prev_ready = bv._wait_for_url
        bv._wait_for_url = lambda _u, _t: (True, "")  # type: ignore[assignment]
        prev_starter = preview._start_dev_server
        preview._start_dev_server = starter  # type: ignore[assignment]
        try:
            start = env.client.post("/api/projects/p1/preview/start", json={})
            assert start.status_code == 200
            sj = start.json()
            assert sj["running"] is True
            assert sj["url"] == "http://127.0.0.1:5174"

            status = env.client.get("/api/projects/p1/preview/status").json()
            assert status["running"] is True
            assert status["has_package_json"] is True

            # Duplicate start: no second process.
            dup = env.client.post("/api/projects/p1/preview/start", json={}).json()
            assert dup["running"] is True
            assert len(procs) == 1

            stop = env.client.post("/api/projects/p1/preview/stop").json()
            assert stop["running"] is False
            assert procs[0].terminated or procs[0].killed
            assert env.client.get("/api/projects/p1/preview/status").json()["running"] is False
        finally:
            bv._wait_for_url = prev_ready  # type: ignore[assignment]
            preview._start_dev_server = prev_starter  # type: ignore[assignment]

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
            import traceback

            traceback.print_exc()
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
