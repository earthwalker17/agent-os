"""Tests for Phase 8 — Supabase sandbox executor (sandbox + tool_runtime).

Coverage:
  - validate_supabase allow-list + NUL rejection.
  - destructive gating is by SUBCOMMAND (db push w/o --dry-run, db reset,
    migration up --linked, db remote commit, branches delete) — NOT a git-style
    "bare verb is safe" clone; db push --dry-run / migration new / status / db
    diff are non-destructive.
  - run_supabase uses a SCRUBBED env (parent provider secrets absent; env_extra
    present); argv-only command metadata; missing-CLI is a clear error.

Run directly:
    python backend/tests/test_sandbox_supabase.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager  # noqa: E402
import execution.tool_runtime as tool_runtime  # noqa: E402
from execution.sandbox import ProjectSandbox, SandboxViolation  # noqa: E402
from execution.tool_runtime import ToolRuntime  # noqa: E402


def _expect_violation(fn, *a, **k):
    try:
        fn(*a, **k)
        return False
    except SandboxViolation:
        return True


def test_allowlist_and_unknown_rejected():
    sb = ProjectSandbox("p")
    # allowed, non-destructive
    for argv in (["status"], ["link", "--project-ref", "abc"], ["start"], ["migration", "new", "init"],
                 ["db", "diff", "--schema", "public"], ["db", "push", "--dry-run", "--linked"]):
        sb.validate_supabase(argv)  # must not raise
    # unknown subcommand
    assert _expect_violation(sb.validate_supabase, ["secrets", "set", "X=1"])
    assert _expect_violation(sb.validate_supabase, ["functions", "deploy"])
    # malformed
    assert _expect_violation(sb.validate_supabase, [])
    assert _expect_violation(sb.validate_supabase, ["db\x00push"])


def test_destructive_gating_by_subcommand():
    sb = ProjectSandbox("p")
    # destructive without confirmation -> rejected
    for argv in (["db", "push", "--linked"], ["db", "push"], ["db", "reset"],
                 ["db", "remote", "commit"], ["migration", "up", "--linked"],
                 ["branches", "delete", "x"]):
        assert _expect_violation(sb.validate_supabase, argv), f"expected gate for {argv}"
    # same ops pass WITH explicit confirmation
    for argv in (["db", "push", "--linked"], ["db", "reset"], ["migration", "up", "--linked"]):
        sb.validate_supabase(argv, allow_destructive=True)  # must not raise
    # non-destructive variants don't need confirmation
    for argv in (["db", "push", "--dry-run", "--linked"], ["migration", "up"], ["migration", "new", "x"],
                 ["status"], ["db", "diff", "--linked", "--schema", "public"]):
        sb.validate_supabase(argv)  # must not raise (db diff is read-only even --linked)


def test_scrubbed_env_excludes_parent_secrets():
    import os
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-PARENTSECRET"
    os.environ["GITHUB_TOKEN"] = "ghp_PARENTSECRET"
    try:
        env = tool_runtime._scrubbed_cli_env({"SUPABASE_ACCESS_TOKEN": "sbp_x", "SUPABASE_DB_PASSWORD": "pw"})
        assert "ANTHROPIC_API_KEY" not in env  # parent provider secret NOT passed
        assert "GITHUB_TOKEN" not in env
        assert env["SUPABASE_ACCESS_TOKEN"] == "sbp_x" and env["SUPABASE_DB_PASSWORD"] == "pw"
        assert "PATH" in env  # the CLI still gets what it needs to run
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("GITHUB_TOKEN", None)


def test_run_supabase_rejects_destructive_and_handles_missing_cli():
    tmp = tempfile.TemporaryDirectory()
    prev = exec_manager._EXECUTION_ROOT
    exec_manager._EXECUTION_ROOT = Path(tmp.name)
    try:
        rt = ToolRuntime("p")
        # destructive without confirmation -> sandbox error (never spawns a process)
        res = rt.run_supabase(["db", "push", "--linked"])
        assert not res.success and "confirmation" in (res.error or "").lower()
        # an allowed op: either runs or (if the CLI is absent) returns a clear error,
        # and the recorded command is argv-only (no secret).
        res2 = rt.run_supabase(["status"], env_extra={"SUPABASE_ACCESS_TOKEN": "sbp_secret"})
        assert res2.metadata.get("command") == "supabase status"
        assert "sbp_secret" not in str(res2.metadata)
        if not res2.success and res2.metadata.get("not_installed"):
            assert "not found" in (res2.error or "")
    finally:
        exec_manager._EXECUTION_ROOT = prev
        tmp.cleanup()


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
