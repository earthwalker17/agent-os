"""Tests for Task 9.4 — deterministic patch integration (execution/integration.py).

Run:  python backend/tests/test_integration.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager  # noqa: E402
from execution.integration import integrate_wave  # noqa: E402
from execution.models import ExecutionTask, TaskStatus  # noqa: E402
from execution.patch_workspace import PatchToolRuntime, init_patch_workspace  # noqa: E402
from execution.tool_runtime import ToolRuntime  # noqa: E402


_PID = "integproj"
_RUN = "run1"


class _TempLayout:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._old_exec_root = exec_manager._EXECUTION_ROOT
        exec_manager._EXECUTION_ROOT = root / "execution_workspaces"

    def init_workspace(self, project_id: str = _PID) -> Path:
        base = exec_manager._EXECUTION_ROOT / project_id
        repo = base / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        (base / "runs").mkdir(exist_ok=True)
        return repo

    def cleanup(self):
        exec_manager._EXECUTION_ROOT = self._old_exec_root
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass


def _run(body):
    layout = _TempLayout()
    try:
        body(layout)
    finally:
        layout.cleanup()


def _unit(tid: str, status=TaskStatus.COMPLETED) -> ExecutionTask:
    return ExecutionTask(
        id=tid, title=f"Task {tid}", status=status, role="coder",
        parallel_safe=True, workspace="patch",
    )


def _write_patch(task_id: str, files: dict[str, str]) -> None:
    overlay = init_patch_workspace(_PID, _RUN, task_id)
    rt = PatchToolRuntime(_PID, overlay)
    for rel, content in files.items():
        res = rt.write_file(rel, content)
        assert res.success, res.error


# ---------- clean apply ----------


def test_disjoint_patches_apply_cleanly():
    def body(layout):
        repo = layout.init_workspace()
        _write_patch("t1", {"src/a.ts": "a\n"})
        _write_patch("t2", {"src/b.ts": "b\n", "docs/b.md": "docs\n"})
        u1, u2 = _unit("t1"), _unit("t2")
        result = integrate_wave(_PID, _RUN, 1, [u1, u2], ToolRuntime(_PID))
        assert sorted(result.applied) == ["docs/b.md", "src/a.ts", "src/b.ts"]
        assert result.conflicts == [] and result.errors == []
        assert (repo / "src" / "a.ts").read_text(encoding="utf-8") == "a\n"
        assert (repo / "docs" / "b.md").read_text(encoding="utf-8") == "docs\n"
        assert result.per_task["t1"] == ["src/a.ts"]
        assert sorted(result.per_task["t2"]) == ["docs/b.md", "src/b.ts"]

    _run(body)


def test_identical_content_from_two_tasks_is_not_a_conflict():
    def body(layout):
        repo = layout.init_workspace()
        _write_patch("t1", {"shared/config.json": '{"x": 1}\n'})
        _write_patch("t2", {"shared/config.json": '{"x": 1}\n'})
        u1, u2 = _unit("t1"), _unit("t2")
        result = integrate_wave(_PID, _RUN, 1, [u1, u2], ToolRuntime(_PID))
        assert result.conflicts == []
        assert result.applied == ["shared/config.json"]
        # Both tasks are credited with the shared file.
        assert result.per_task["t1"] == ["shared/config.json"]
        assert result.per_task["t2"] == ["shared/config.json"]
        assert (repo / "shared" / "config.json").exists()

    _run(body)


# ---------- conflicts ----------


def test_conflicting_content_first_writer_wins_and_is_surfaced():
    def body(layout):
        repo = layout.init_workspace()
        _write_patch("t1", {"src/app.ts": "version A\n"})
        _write_patch("t2", {"src/app.ts": "version B\n"})
        u1, u2 = _unit("t1"), _unit("t2")
        result = integrate_wave(_PID, _RUN, 2, [u1, u2], ToolRuntime(_PID))
        # First in plan order wins; the loser is surfaced, never silently applied.
        assert (repo / "src" / "app.ts").read_text(encoding="utf-8") == "version A\n"
        assert len(result.conflicts) == 1
        c = result.conflicts[0]
        assert c.path == "src/app.ts"
        assert c.applied_task == "t1" and c.rejected_task == "t2"
        assert c.wave == 2
        # A blocker landed on the losing task.
        assert any("integration conflict" in b for b in u2.blockers)
        assert not any("integration conflict" in b for b in u1.blockers)

    _run(body)


def test_failed_task_output_still_integrates_matching_sequential_semantics():
    def body(layout):
        repo = layout.init_workspace()
        # Sequentially, a failed task's writes are already on disk; the overlay
        # model must not silently drop them.
        _write_patch("t1", {"partial.txt": "partial output\n"})
        u1 = _unit("t1", status=TaskStatus.FAILED)
        result = integrate_wave(_PID, _RUN, 1, [u1], ToolRuntime(_PID))
        assert result.applied == ["partial.txt"]
        assert (repo / "partial.txt").exists()

    _run(body)


# ---------- case-insensitive collision (T1.6) ----------


def test_case_only_path_collision_is_detected_not_silently_overwritten():
    """On a case-insensitive filesystem (Windows/macOS) two tasks writing paths
    that differ ONLY in case target the SAME on-disk file. That must surface as a
    conflict (first-writer-wins), never a silent overwrite. On a case-sensitive
    FS the two are genuinely distinct files and both apply cleanly."""
    import os

    def body(layout):
        repo = layout.init_workspace()
        _write_patch("t1", {"src/App.ts": "version A\n"})
        _write_patch("t2", {"src/app.ts": "version B\n"})
        u1, u2 = _unit("t1"), _unit("t2")
        result = integrate_wave(_PID, _RUN, 1, [u1, u2], ToolRuntime(_PID))

        case_insensitive = os.path.normcase("A") == os.path.normcase("a")
        if case_insensitive:
            # Same file: first writer wins, the collision is surfaced + blocked.
            assert len(result.conflicts) == 1, result.conflicts
            c = result.conflicts[0]
            assert c.applied_task == "t1" and c.rejected_task == "t2"
            assert result.applied == ["src/App.ts"]
            assert any("conflict" in b for b in u2.blockers)
            # The winner's content is what's on disk (not silently clobbered).
            on_disk = list((repo / "src").glob("*.ts"))
            assert len(on_disk) == 1
            assert on_disk[0].read_text(encoding="utf-8") == "version A\n"
        else:
            # Distinct files on a case-sensitive FS — both apply, no conflict.
            assert result.conflicts == []
            assert sorted(result.applied) == ["src/App.ts", "src/app.ts"]

    _run(body)


# ---------- safety ----------


def test_apply_routes_through_the_sandbox():
    def body(layout):
        layout.init_workspace()
        # Force a sensitive path into the overlay by writing to disk directly
        # (bypassing PatchToolRuntime, as a hostile/buggy writer would).
        overlay = init_patch_workspace(_PID, _RUN, "t1")
        bad = overlay / ".env"
        bad.write_text("SECRET=1", encoding="utf-8")
        u1 = _unit("t1")
        result = integrate_wave(_PID, _RUN, 1, [u1], ToolRuntime(_PID))
        # The base ToolRuntime re-validates on the way in and refuses it.
        assert result.applied == []
        assert len(result.errors) == 1 and ".env" in result.errors[0]
        repo = exec_manager._EXECUTION_ROOT / _PID / "repo"
        assert not (repo / ".env").exists()

    _run(body)


def test_missing_or_empty_overlay_is_a_no_op():
    def body(layout):
        layout.init_workspace()
        u1 = _unit("t9")  # never had a patch workspace
        result = integrate_wave(_PID, _RUN, 1, [u1], ToolRuntime(_PID))
        assert result.applied == [] and result.conflicts == [] and result.errors == []
        assert result.per_task == {"t9": []}

    _run(body)


def test_to_dict_shape():
    def body(layout):
        layout.init_workspace()
        _write_patch("t1", {"x.txt": "x"})
        result = integrate_wave(_PID, _RUN, 3, [_unit("t1")], ToolRuntime(_PID))
        d = result.to_dict()
        assert d["wave"] == 3
        assert d["applied"] == ["x.txt"]
        assert d["conflicts"] == [] and d["errors"] == []
        assert d["per_task"] == {"t1": ["x.txt"]}

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
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"\n{failures} of {len(names)} tests failed.")
        return 1
    print(f"\nAll {len(names)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
