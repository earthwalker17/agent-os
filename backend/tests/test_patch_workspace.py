"""Tests for Task 9.2 — isolated patch workspaces (execution/patch_workspace.py).

Covers the overlay ToolRuntime (reads fall through to the shared repo, writes
land in the overlay, merged listings/search, append seeding, blocked
executors) and the sandbox rules applying identically inside the overlay.

Run:  python backend/tests/test_patch_workspace.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager  # noqa: E402
from execution.patch_workspace import (  # noqa: E402
    PatchToolRuntime,
    collect_patch_files,
    get_overlay_root,
    init_patch_workspace,
    read_patch_manifest,
    write_patch_manifest,
)


_PID = "patchproj"


class _TempLayout:
    """Temp execution root + a seeded repo, monkeypatching the module root."""

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
        (base / "logs").mkdir(exist_ok=True)
        (base / "AGENT.md").write_text("# Agent", encoding="utf-8")
        (base / "TASK.md").write_text("# Task", encoding="utf-8")
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


def _make_runtime(layout: _TempLayout, run_id: str = "run1", task_id: str = "t1"):
    layout.init_workspace()
    overlay = init_patch_workspace(_PID, run_id, task_id)
    return PatchToolRuntime(_PID, overlay), overlay


# ---------- writes land in the overlay, never the shared repo ----------


def test_write_goes_to_overlay_not_repo():
    def body(layout):
        rt, overlay = _make_runtime(layout)
        res = rt.write_file("src/app.py", "print('hi')\n")
        assert res.success, res.error
        assert (overlay / "src" / "app.py").exists()
        repo = exec_manager._EXECUTION_ROOT / _PID / "repo"
        assert not (repo / "src" / "app.py").exists()

    _run(body)


def test_read_falls_through_to_base_repo():
    def body(layout):
        repo = layout.init_workspace()
        (repo / "base.txt").write_text("base content", encoding="utf-8")
        overlay = init_patch_workspace(_PID, "run1", "t1")
        rt = PatchToolRuntime(_PID, overlay)
        res = rt.read_file("base.txt")
        assert res.success and res.output == "base content"

    _run(body)


def test_overlay_shadows_base_on_read():
    def body(layout):
        repo = layout.init_workspace()
        (repo / "f.txt").write_text("old", encoding="utf-8")
        overlay = init_patch_workspace(_PID, "run1", "t1")
        rt = PatchToolRuntime(_PID, overlay)
        assert rt.write_file("f.txt", "new").success
        res = rt.read_file("f.txt")
        assert res.success and res.output == "new"
        # The base copy is untouched.
        assert (repo / "f.txt").read_text(encoding="utf-8") == "old"

    _run(body)


def test_append_seeds_overlay_with_base_content_first():
    def body(layout):
        repo = layout.init_workspace()
        (repo / "notes.md").write_text("line1\n", encoding="utf-8")
        overlay = init_patch_workspace(_PID, "run1", "t1")
        rt = PatchToolRuntime(_PID, overlay)
        res = rt.append_file("notes.md", "line2\n")
        assert res.success, res.error
        assert (overlay / "notes.md").read_text(encoding="utf-8") == "line1\nline2\n"
        assert (repo / "notes.md").read_text(encoding="utf-8") == "line1\n"

    _run(body)


# ---------- merged views ----------


def test_list_files_merges_base_and_overlay():
    def body(layout):
        repo = layout.init_workspace()
        (repo / "a.txt").write_text("a", encoding="utf-8")
        overlay = init_patch_workspace(_PID, "run1", "t1")
        rt = PatchToolRuntime(_PID, overlay)
        assert rt.write_file("b.txt", "b").success
        res = rt.list_files(".")
        assert res.success
        names = {e["name"] for e in res.metadata["entries"]}
        assert {"a.txt", "b.txt"} <= names

    _run(body)


def test_search_finds_overlay_and_unshadowed_base_hits():
    def body(layout):
        repo = layout.init_workspace()
        (repo / "one.py").write_text("needle_base = 1\n", encoding="utf-8")
        (repo / "two.py").write_text("needle_shadowed = 1\n", encoding="utf-8")
        overlay = init_patch_workspace(_PID, "run1", "t1")
        rt = PatchToolRuntime(_PID, overlay)
        # Shadow two.py WITHOUT the needle; add a new overlay file WITH one.
        assert rt.write_file("two.py", "clean = 1\n").success
        assert rt.write_file("three.py", "needle_overlay = 1\n").success
        res = rt.search_files("needle")
        assert res.success
        paths = {h["path"] for h in res.metadata["hits"]}
        assert "one.py" in paths        # unshadowed base hit
        assert "three.py" in paths      # overlay hit
        assert "two.py" not in paths    # shadowed base copy must not leak

    _run(body)


# ---------- sandbox rules hold inside the overlay ----------


def test_overlay_rejects_escape_and_sensitive_paths():
    def body(layout):
        rt, _ = _make_runtime(layout)
        assert not rt.write_file("../outside.txt", "x").success
        assert not rt.write_file(".env", "SECRET=1").success
        assert not rt.write_file("keys/server.pem", "k").success
        assert not rt.read_file("../../etc/passwd").success

    _run(body)


def test_overlay_rejects_dot_git_paths():
    # A repo write must never reach .git — not just .git/config. Writing
    # .git/hooks/pre-commit would run arbitrary code at the next commit.
    def body(layout):
        rt, _ = _make_runtime(layout)
        assert not rt.write_file(".git/hooks/pre-commit", "#!/bin/sh\necho pwn").success
        assert not rt.write_file(".git/config", "[x]").success
        assert not rt.read_file(".git/HEAD").success

    _run(body)


def test_patch_dir_rejects_unsafe_task_id_segment():
    # Defense in depth: even if a caller bypassed plan-parse sanitization, the
    # filesystem layout helper refuses a task id that would escape patches/.
    from execution.patch_workspace import get_patch_dir
    from execution.sandbox import SandboxViolation

    def body(layout):
        layout.init_workspace()
        for bad in ("../../pwn", "a/b", "..", "with\\sep"):
            try:
                get_patch_dir(_PID, "run1", bad)
                raise AssertionError(f"expected rejection for {bad!r}")
            except SandboxViolation:
                pass
        # A safe id still resolves.
        assert get_patch_dir(_PID, "run1", "t1").name == "t1"

    _run(body)


# ---------- executors are blocked ----------


def test_run_shell_git_supabase_blocked_in_patch_workspace():
    def body(layout):
        rt, _ = _make_runtime(layout)
        shell = rt.run_shell("echo hi")
        assert not shell.success and "patch workspace" in (shell.error or "")
        git = rt.run_git(["status"])
        assert not git.success and "patch workspace" in (git.error or "")
        sup = rt.run_supabase(["status"])
        assert not sup.success and "patch workspace" in (sup.error or "")

    _run(body)


# ---------- layout + manifest helpers ----------


def test_collect_patch_files_returns_sorted_relative_posix_paths():
    def body(layout):
        rt, overlay = _make_runtime(layout)
        assert rt.write_file("src/b.ts", "b").success
        assert rt.write_file("a.md", "a").success
        assert collect_patch_files(overlay) == ["a.md", "src/b.ts"]

    _run(body)


def test_manifest_round_trip_sits_outside_the_overlay():
    def body(layout):
        layout.init_workspace()
        overlay = init_patch_workspace(_PID, "run1", "t1")
        write_patch_manifest(_PID, "run1", "t1", {"task_id": "t1", "status": "completed"})
        got = read_patch_manifest(_PID, "run1", "t1")
        assert got == {"task_id": "t1", "status": "completed"}
        # The manifest must never be collected as task output.
        assert collect_patch_files(overlay) == []
        assert get_overlay_root(_PID, "run1", "t1") == overlay

    _run(body)


def test_init_patch_workspace_is_idempotent():
    def body(layout):
        layout.init_workspace()
        one = init_patch_workspace(_PID, "run1", "t1")
        two = init_patch_workspace(_PID, "run1", "t1")
        assert one == two and one.exists()

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
