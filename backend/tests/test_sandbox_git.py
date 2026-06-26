"""Tests for Task 7.1 — the sandboxed Git primitive.

Coverage:

  - run_shell block-list: the new destructive-git guards (reset --hard,
    clean -f, checkout -- ., branch -d, …) reject through validate_command,
    while the legacy `git push` block stays intact.
  - validate_git allow-list: allowed subcommands pass, unknown ones reject,
    bad arg shapes (non-list, non-str, control chars) reject.
  - validate_git destructive gating: reset --hard / force-push / clean /
    worktree-discarding checkout|restore / branch -d need allow_destructive;
    a non-force push and `restore --staged` are allowed.
  - run_git end-to-end against real git: init/config/add/commit/status work,
    a token in env_extra never lands in metadata, and a destructive command
    is rejected without allow_destructive.

Run directly:
    python backend/tests/test_sandbox_git.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager  # noqa: E402
from execution.sandbox import ProjectSandbox, SandboxViolation  # noqa: E402
from execution.tool_runtime import ToolRuntime  # noqa: E402


# ---------- harness ----------


class _TempLayout:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.execution_dir = Path(self.tmp.name) / "execution_workspaces"
        self.execution_dir.mkdir()
        self._prev = exec_manager._EXECUTION_ROOT
        exec_manager._EXECUTION_ROOT = self.execution_dir

    def cleanup(self) -> None:
        exec_manager._EXECUTION_ROOT = self._prev
        self.tmp.cleanup()

    def init_repo(self, project_id: str) -> Path:
        repo = self.execution_dir / project_id / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        return repo


def _run(test_body):
    layout = _TempLayout()
    try:
        test_body(layout)
    finally:
        layout.cleanup()


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except SandboxViolation:
        return True


# ---------- run_shell block-list (validate_command) ----------


def test_run_shell_blocks_destructive_git():
    sb = ProjectSandbox("p")
    for cmd in (
        "git reset --hard HEAD~1",
        "git reset --HARD origin/main",
        "git clean -fdx",
        "git checkout -- .",
        "git checkout .",
        "git restore .",
        "git branch -d feature",
        "git branch -D feature",
    ):
        assert _raises(lambda c=cmd: sb.validate_command(c)), cmd


def test_run_shell_still_blocks_push():
    sb = ProjectSandbox("p")
    assert _raises(lambda: sb.validate_command("git push origin main"))


def test_run_shell_allows_safe_git():
    sb = ProjectSandbox("p")
    # These are not in the block-list and must pass validate_command.
    sb.validate_command("git status")
    sb.validate_command("git diff")
    sb.validate_command("git log --oneline")


# ---------- validate_git allow-list ----------


def test_validate_git_allows_known_subcommands():
    sb = ProjectSandbox("p")
    for sub in ("status", "diff", "add", "commit", "branch", "log", "rev-parse", "push"):
        sb.validate_git([sub])  # should not raise


def test_validate_git_rejects_unknown_subcommand():
    sb = ProjectSandbox("p")
    assert _raises(lambda: sb.validate_git(["nuke"]))
    assert _raises(lambda: sb.validate_git(["daemon"]))


def test_validate_git_rejects_bad_shapes():
    sb = ProjectSandbox("p")
    assert _raises(lambda: sb.validate_git([]))
    assert _raises(lambda: sb.validate_git("status"))  # not a list
    assert _raises(lambda: sb.validate_git([123]))
    assert _raises(lambda: sb.validate_git(["status\x00evil"]))  # NUL byte


def test_validate_git_allows_multiline_commit_message():
    # A real commit message has a subject + body (newlines). shell=False makes
    # this safe; the args must pass validation.
    sb = ProjectSandbox("p")
    sb.validate_git(["commit", "-m", "Add feature\n\n- bullet one\n- bullet two"])


# ---------- validate_git destructive gating ----------


def test_validate_git_destructive_needs_flag():
    sb = ProjectSandbox("p")
    destructive = [
        ["reset", "--hard", "HEAD"],
        ["push", "--force", "origin", "main"],
        ["push", "-f"],
        ["clean", "-fdx"],
        ["checkout", "--", "."],
        ["checkout", "."],
        ["restore", "src/app.py"],
        ["branch", "-D", "feature"],
    ]
    for argv in destructive:
        assert _raises(lambda a=argv: sb.validate_git(a)), argv
        # allowed when explicitly confirmed
        sb.validate_git(argv, allow_destructive=True)


def test_validate_git_non_destructive_allowed():
    sb = ProjectSandbox("p")
    # plain push (not force) is allowed at the sandbox layer; its external
    # nature is gated at the confirm endpoint, not here.
    sb.validate_git(["push", "-u", "origin", "feature"])
    sb.validate_git(["reset", "--soft", "HEAD~1"])
    sb.validate_git(["restore", "--staged", "file.py"])  # unstage only
    sb.validate_git(["clean", "-n"])  # dry-run


# ---------- run_git end-to-end (real git) ----------


def test_run_git_init_commit_status():
    def body(layout: _TempLayout):
        layout.init_repo("proj")
        rt = ToolRuntime("proj")

        assert rt.run_git(["init"]).success
        assert (layout.execution_dir / "proj" / "repo" / ".git").exists()

        assert rt.run_git(["config", "user.email", "a@b.c"]).success
        assert rt.run_git(["config", "user.name", "Agent OS"]).success
        assert rt.run_git(["config", "commit.gpgsign", "false"]).success

        rt.write_file("hello.txt", "hi\n")
        assert rt.run_git(["add", "-A"]).success
        commit = rt.run_git(["commit", "-m", "init"])
        assert commit.success, commit.error

        status = rt.run_git(["status", "--porcelain"])
        assert status.success
        assert status.output.strip() == ""  # clean tree after commit

    _run(body)


def test_run_git_token_never_in_metadata():
    def body(layout: _TempLayout):
        layout.init_repo("proj")
        rt = ToolRuntime("proj")
        rt.run_git(["init"])
        res = rt.run_git(["status"], env_extra={"AGENT_OS_GIT_ASKPASS_VALUE": "supersecrettoken"})
        blob = (res.metadata.get("command", "") + " ".join(res.metadata.get("args", [])))
        assert "supersecrettoken" not in blob

    _run(body)


def test_run_git_rejects_destructive_without_flag():
    def body(layout: _TempLayout):
        layout.init_repo("proj")
        rt = ToolRuntime("proj")
        rt.run_git(["init"])
        res = rt.run_git(["reset", "--hard"])
        assert not res.success
        assert "destructive" in res.error.lower()

    _run(body)


def test_run_git_rejects_bad_args():
    def body(layout: _TempLayout):
        layout.init_repo("proj")
        rt = ToolRuntime("proj")
        assert not rt.run_git([]).success
        assert not rt.run_git(["bogus-subcommand"]).success

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
