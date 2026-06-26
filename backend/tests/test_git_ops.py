"""Tests for Task 7.2 — sandboxed local Git operations (git_ops).

Runs against real `git` in a temp workspace. Coverage:

  - ensure_repo: idempotent init, .gitignore + identity, HEAD always exists.
  - create_checkpoint: out-of-branch tagged snapshot, branch HEAD not advanced.
  - capture_diff: shows new untracked files vs the checkpoint, redacts secrets,
    bounds size.
  - commit: refuses secret-looking files, commits the safe set, nothing-to-commit.
  - create_branch: creates + switches.
  - rollback: clean-tree and dirty-tree restore to the pre-run state.
  - redaction + secret-detection units.

Run directly:
    python backend/tests/test_git_ops.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager  # noqa: E402
import execution.git_ops as git_ops  # noqa: E402
from execution.tool_runtime import ToolRuntime  # noqa: E402


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

    def repo(self, project_id: str = "proj") -> Path:
        r = self.execution_dir / project_id / "repo"
        r.mkdir(parents=True, exist_ok=True)
        return r


def _run(test_body):
    layout = _TempLayout()
    try:
        test_body(layout)
    finally:
        layout.cleanup()


# ---------- units ----------


def test_redact_tokens():
    assert "[REDACTED]" in git_ops._redact("TOKEN=ghp_0123456789abcdefghij0123")
    assert "ghp_0123456789" not in git_ops._redact("x ghp_0123456789abcdefghij0123 y")
    assert "[REDACTED]" in git_ops._redact("api_key: AKIA0123456789ABCDEF")
    # non-secret text passes through
    assert git_ops._redact("hello world") == "hello world"


def test_looks_secret():
    assert git_ops._looks_secret(".env")
    assert git_ops._looks_secret("config/.env.production")
    assert git_ops._looks_secret("server.key")
    assert git_ops._looks_secret("secrets.json")
    assert not git_ops._looks_secret(".env.example")
    assert not git_ops._looks_secret("src/app.py")


# ---------- ensure_repo ----------


def test_ensure_repo_idempotent():
    def body(layout: _TempLayout):
        repo = layout.repo()
        st1 = git_ops.ensure_repo("proj")
        assert st1.is_repo
        assert st1.branch == "main"
        assert st1.head
        assert (repo / ".git").exists()
        assert (repo / ".gitignore").exists()
        head1 = st1.head
        st2 = git_ops.ensure_repo("proj")  # second call no-ops
        assert st2.is_repo
        assert st2.head == head1

    _run(body)


def test_ensure_repo_commits_preexisting_files():
    def body(layout: _TempLayout):
        repo = layout.repo()
        (repo / "readme.md").write_text("hi\n", encoding="utf-8")
        git_ops.ensure_repo("proj")
        rt = ToolRuntime("proj")
        tracked = rt.run_git(["ls-files"]).output
        assert "readme.md" in tracked

    _run(body)


# ---------- checkpoint ----------


def test_checkpoint_does_not_advance_branch():
    def body(layout: _TempLayout):
        layout.repo()
        st = git_ops.ensure_repo("proj")
        rt = ToolRuntime("proj")
        head_before = st.head
        (layout.repo() / "a.txt").write_text("A\n", encoding="utf-8")
        ck = git_ops.create_checkpoint("proj", "run-1", runtime=rt)
        assert ck.created
        assert ck.ref and ck.base_commit == head_before
        # branch HEAD unchanged — checkpoint is out-of-branch
        assert git_ops._head_sha(rt) == head_before
        # tag exists
        assert rt.run_git(["rev-parse", "--verify", ck.tag]).success

    _run(body)


# ---------- diff ----------


def test_capture_diff_shows_new_files_and_redacts():
    def body(layout: _TempLayout):
        repo = layout.repo()
        git_ops.ensure_repo("proj")
        ck = git_ops.create_checkpoint("proj", "run-1")
        # no changes since checkpoint
        d0 = git_ops.capture_diff("proj", ck.ref)
        assert d0.captured and not d0.files
        # add a new file containing a secret
        (repo / "notes.txt").write_text("TOKEN=ghp_0123456789abcdefghij0123\n", encoding="utf-8")
        d1 = git_ops.capture_diff("proj", ck.ref)
        assert d1.captured
        assert "notes.txt" in d1.files
        assert "[REDACTED]" in d1.diff_text
        assert "ghp_0123456789" not in d1.diff_text

    _run(body)


def test_capture_diff_truncates():
    def body(layout: _TempLayout):
        repo = layout.repo()
        git_ops.ensure_repo("proj")
        ck = git_ops.create_checkpoint("proj", "run-1")
        (repo / "big.txt").write_text("x\n" * 5000, encoding="utf-8")
        d = git_ops.capture_diff("proj", ck.ref, max_chars=200)
        assert d.captured and d.truncated
        assert len(d.diff_text) <= 200 + 80

    _run(body)


# ---------- commit ----------


def test_commit_refuses_secret_files():
    def body(layout: _TempLayout):
        repo = layout.repo()
        git_ops.ensure_repo("proj")
        (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
        (repo / "secrets.json").write_text('{"token":"abc"}\n', encoding="utf-8")
        res = git_ops.commit("proj", "add app")
        assert res.committed and res.sha
        assert "app.py" in res.files
        assert "secrets.json" in res.refused
        rt = ToolRuntime("proj")
        tracked = rt.run_git(["ls-files"]).output
        assert "app.py" in tracked
        assert "secrets.json" not in tracked

    _run(body)


def test_commit_multiline_message():
    # Real (LLM-generated) commit messages have a subject + body.
    def body(layout: _TempLayout):
        repo = layout.repo()
        git_ops.ensure_repo("proj")
        (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
        msg = "Add app entrypoint\n\n- create app.py\n- prints a greeting"
        res = git_ops.commit("proj", msg)
        assert res.committed and res.sha
        body_out = ToolRuntime("proj").run_git(["log", "-1", "--pretty=%B"]).output
        assert "Add app entrypoint" in body_out and "create app.py" in body_out

    _run(body)


def test_commit_nothing_to_commit():
    def body(layout: _TempLayout):
        layout.repo()
        git_ops.ensure_repo("proj")
        res = git_ops.commit("proj", "noop")
        assert not res.committed
        assert "nothing to commit" in (res.error or "")

    _run(body)


def test_create_branch():
    def body(layout: _TempLayout):
        layout.repo()
        git_ops.ensure_repo("proj")
        ok, err = git_ops.create_branch("proj", "feature/x")
        assert ok, err
        assert git_ops.current_branch("proj") == "feature/x"
        # switching back to an existing branch
        ok2, _ = git_ops.create_branch("proj", "main")
        assert ok2
        assert git_ops.current_branch("proj") == "main"

    _run(body)


# ---------- rollback ----------


def test_rollback_clean_tree_removes_run_files():
    def body(layout: _TempLayout):
        repo = layout.repo()
        git_ops.ensure_repo("proj")
        ck = git_ops.create_checkpoint("proj", "run-1")  # pre-run = empty
        # run writes files
        (repo / "a.txt").write_text("A\n", encoding="utf-8")
        (repo / "b.txt").write_text("B\n", encoding="utf-8")
        res = git_ops.rollback("proj", base_commit=ck.base_commit, checkpoint_ref=ck.ref)
        assert res.rolled_back
        assert not (repo / "a.txt").exists()
        assert not (repo / "b.txt").exists()

    _run(body)


def test_rollback_dirty_tree_restores_prerun_edits():
    def body(layout: _TempLayout):
        repo = layout.repo()
        git_ops.ensure_repo("proj")
        (repo / "a.txt").write_text("v1\n", encoding="utf-8")
        git_ops.commit("proj", "add a")  # a.txt tracked at v1
        # pre-run uncommitted edit
        (repo / "a.txt").write_text("v2-prerun\n", encoding="utf-8")
        ck = git_ops.create_checkpoint("proj", "run-1")
        # run mutates a.txt and adds b.txt
        (repo / "a.txt").write_text("v3-run\n", encoding="utf-8")
        (repo / "b.txt").write_text("B\n", encoding="utf-8")
        res = git_ops.rollback("proj", base_commit=ck.base_commit, checkpoint_ref=ck.ref)
        assert res.rolled_back
        assert (repo / "a.txt").read_text(encoding="utf-8") == "v2-prerun\n"
        assert not (repo / "b.txt").exists()

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
