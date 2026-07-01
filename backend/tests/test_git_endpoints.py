"""Tests for Task 7.7 — Project Ops HTTP endpoints (main.py).

Exercises the FastAPI surface. Network (GitHub REST) is stubbed; the commit /
rollback / diff paths run against real git; push is validated both end-to-end
against a LOCAL BARE remote (real git push) and through the endpoint with a
stubbed connector.

Coverage:
  - GET /git/status, GET /runs/{id}/diff
  - commit: two-phase preview/confirm, secret refusal, run.json stamping
  - push: endpoint confirm gate + a real push to a local bare remote
  - PR: requires a prior push (409), then opens via stubbed REST
  - rollback: confirm gate restores the pre-run state
  - credentials: set/get/delete (presence only), works in GENERAL

Run directly:
    python backend/tests/test_git_endpoints.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import credentials  # noqa: E402
import llm  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
import execution.git_ops as git_ops  # noqa: E402
import execution.github_connector as github_connector  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution.tool_runtime import ToolRuntime  # noqa: E402
from execution.models import RunRecord, RunStatus  # noqa: E402


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
        self._prev_cred = (credentials._CRED_DIR, credentials._PROJECTS_DIR, credentials._GLOBAL_FILE)
        self._prev_chat = llm.chat
        main.PROJECTS_DIR = self.projects_dir
        exec_manager._EXECUTION_ROOT = self.execution_dir
        credentials._CRED_DIR = root / "credentials"
        credentials._PROJECTS_DIR = credentials._CRED_DIR / "projects"
        credentials._GLOBAL_FILE = credentials._CRED_DIR / "global.json"
        import os

        self._env_backup = {k: os.environ.get(k) for k in credentials._GITHUB_ENV_VARS}
        for k in credentials._GITHUB_ENV_VARS:
            os.environ.pop(k, None)
        llm.chat = lambda system, messages, **kw: "Add feature"
        self.client = TestClient(main.app)

    def cleanup(self) -> None:
        import os

        main.PROJECTS_DIR = self._prev_projects
        exec_manager._EXECUTION_ROOT = self._prev_exec
        (credentials._CRED_DIR, credentials._PROJECTS_DIR, credentials._GLOBAL_FILE) = self._prev_cred
        llm.chat = self._prev_chat
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def make_project(self, pid: str) -> Path:
        (self.projects_dir / pid).mkdir(parents=True, exist_ok=True)
        (self.projects_dir / pid / "PROJECT.md").write_text(f"# {pid}\n", encoding="utf-8")
        ws = self.execution_dir / pid
        (ws / "repo").mkdir(parents=True, exist_ok=True)
        (ws / "AGENT.md").write_text("# A\n", encoding="utf-8")
        (ws / "TASK.md").write_text("# T\n", encoding="utf-8")
        (ws / "runs").mkdir(exist_ok=True)
        (ws / "logs").mkdir(exist_ok=True)
        return ws / "repo"

    def make_run(self, pid, run_id="run-1", files=None):
        git_ops.ensure_repo(pid)
        rec = RunRecord(
            run_id=run_id, project_id=pid, task_title="task", status=RunStatus.COMPLETED,
            summary="did things",
        )
        ck = git_ops.create_checkpoint(pid, run_id)
        rec.pre_run_checkpoint = ck.ref
        rec.base_commit = ck.base_commit
        rec.checkpoint_tag = ck.tag
        rec.branch = git_ops.current_branch(pid)
        repo = self.execution_dir / pid / "repo"
        for name, content in (files or {"app.py": "print('hi')\n"}).items():
            (repo / name).write_text(content, encoding="utf-8")
        run_store.init_run_dir(pid, run_id)
        run_store.write_run_json(pid, run_id, rec)
        return rec


def _run(test_body):
    env = _Env()
    try:
        test_body(env)
    finally:
        env.cleanup()


# ---------- status / diff ----------


def test_git_status_endpoint():
    def body(env):
        env.make_project("p")
        env.make_run("p")
        r = env.client.get("/api/projects/p/git/status")
        assert r.status_code == 200
        assert r.json()["is_repo"] and r.json()["branch"] == "main"

    _run(body)


def test_diff_endpoint():
    def body(env):
        env.make_project("p")
        env.make_run("p")
        run_store.write_diff_patch("p", "run-1", "diff --git a/app.py b/app.py\n+print('hi')\n")
        r = env.client.get("/api/projects/p/execution/runs/run-1/diff")
        assert r.status_code == 200 and r.json()["available"]
        assert "app.py" in r.json()["diff"]

    _run(body)


# ---------- commit ----------


def test_commit_preview_then_confirm():
    def body(env):
        env.make_project("p")
        env.make_run("p")
        # preview (no confirm) — generates a message, lists the file
        pv = env.client.post("/api/projects/p/execution/runs/run-1/git/commit", json={"confirm": False})
        assert pv.status_code == 200
        c = pv.json()
        assert c["applied"] is False
        assert c["contract"]["action"] == "commit"
        assert "app.py" in c["contract"]["files"]
        assert c["contract"]["message"]
        # confirm
        cf = env.client.post(
            "/api/projects/p/execution/runs/run-1/git/commit",
            json={"confirm": True, "message": "Add app"},
        )
        assert cf.status_code == 200 and cf.json()["applied"]
        sha = cf.json()["commit_sha"]
        assert sha
        raw = run_store.read_run_json("p", "run-1")
        assert raw["commit_sha"] == sha
        tracked = ToolRuntime("p").run_git(["ls-files"]).output
        assert "app.py" in tracked

    _run(body)


def test_commit_refuses_secret():
    def body(env):
        env.make_project("p")
        env.make_run("p", files={"app.py": "x\n", "secrets.json": '{"t":"x"}\n'})
        cf = env.client.post(
            "/api/projects/p/execution/runs/run-1/git/commit",
            json={"confirm": True, "message": "Add app"},
        )
        assert cf.status_code == 200
        assert "secrets.json" in cf.json()["refused"]
        tracked = ToolRuntime("p").run_git(["ls-files"]).output
        assert "app.py" in tracked and "secrets.json" not in tracked

    _run(body)


# ---------- push ----------


def test_push_endpoint_with_stubbed_connector():
    def body(env):
        env.make_project("p")
        env.make_run("p")
        env.client.post("/api/projects/p/execution/runs/run-1/git/commit", json={"confirm": True, "message": "c"})
        credentials.set_github_credential("p", token="ghp_dummdummdummdummdummy")
        prev_get, prev_push = github_connector.get_remote, github_connector.push_branch
        github_connector.get_remote = lambda pid, **kw: ("octocat", "demo")
        github_connector.push_branch = lambda pid, branch, **kw: github_connector.PushResult(ok=True, branch=branch, remote="origin")
        try:
            r = env.client.post("/api/projects/p/execution/runs/run-1/git/push", json={"confirm": True})
            assert r.status_code == 200 and r.json()["applied"]
            assert run_store.read_run_json("p", "run-1")["pushed"] is True
        finally:
            github_connector.get_remote, github_connector.push_branch = prev_get, prev_push

    _run(body)


def test_real_push_to_local_bare_remote():
    def body(env):
        repo = env.make_project("p")
        env.make_run("p")
        rt = ToolRuntime("p")
        git_ops.commit("p", "initial app")
        # a real bare remote on disk (test infra may use subprocess directly)
        bare = Path(env.tmp.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True)
        rt.run_git(["remote", "add", "origin", str(bare)])
        credentials.set_github_credential("p", token="dummy-not-needed-for-file-remote")
        res = github_connector.push_branch("p", "main", runtime=rt)
        assert res.ok, res.error
        check = subprocess.run(
            ["git", "--git-dir", str(bare), "rev-parse", "refs/heads/main"],
            capture_output=True, text=True,
        )
        assert check.returncode == 0  # branch landed on the remote

    _run(body)


# ---------- pull request ----------


def test_pr_requires_push_then_opens():
    def body(env):
        env.make_project("p")
        rec = env.make_run("p")
        env.client.post("/api/projects/p/execution/runs/run-1/git/commit", json={"confirm": True, "message": "c"})
        credentials.set_github_credential("p", token="ghp_dummdummdummdummdummy")
        prev_get, prev_pr = github_connector.get_remote, github_connector.create_pull_request
        github_connector.get_remote = lambda pid, **kw: ("octocat", "demo")
        github_connector.create_pull_request = lambda pid, **kw: github_connector.PullRequestResult(
            ok=True, url="https://github.com/octocat/demo/pull/4", number=4
        )
        try:
            # not pushed yet → 409
            r1 = env.client.post("/api/projects/p/execution/runs/run-1/github/pr", json={"confirm": True})
            assert r1.status_code == 409
            # mark pushed, then it opens
            raw = run_store.read_run_json("p", "run-1")
            rec2 = RunRecord(**raw)
            rec2.pushed = True
            run_store.write_run_json("p", "run-1", rec2)
            r2 = env.client.post("/api/projects/p/execution/runs/run-1/github/pr", json={"confirm": True})
            assert r2.status_code == 200 and r2.json()["applied"]
            assert run_store.read_run_json("p", "run-1")["pr_number"] == 4
        finally:
            github_connector.get_remote, github_connector.create_pull_request = prev_get, prev_pr

    _run(body)


# ---------- rollback ----------


def test_rollback_endpoint():
    def body(env):
        repo = env.make_project("p")
        env.make_run("p")  # checkpoint captured before run files were written
        assert (repo / "app.py").exists()
        # preview
        pv = env.client.post("/api/projects/p/execution/runs/run-1/git/rollback", json={"confirm": False})
        assert pv.status_code == 200 and pv.json()["applied"] is False
        assert pv.json()["contract"]["destructive"] is True
        # confirm — restores pre-run state (app.py was written after the checkpoint)
        cf = env.client.post("/api/projects/p/execution/runs/run-1/git/rollback", json={"confirm": True})
        assert cf.status_code == 200 and cf.json()["applied"]
        assert not (repo / "app.py").exists()

    _run(body)


# ---------- credentials ----------


def test_credentials_endpoints():
    def body(env):
        env.make_project("p")
        prev_api = github_connector._github_api
        github_connector._github_api = lambda method, path, token, payload=None, **kw: (200, {"login": "tester"})
        try:
            r = env.client.post("/api/projects/p/credentials/github", json={"token": "ghp_abcabcabcabcabcabcab"})
            assert r.status_code == 200
            assert r.json()["configured"] and r.json()["login"] == "tester"
            # GET never leaks the token
            g = env.client.get("/api/projects/p/credentials/github")
            assert g.status_code == 200 and "token" not in g.json()
            assert "ghp_abcabc" not in g.text
            # DELETE
            d = env.client.delete("/api/projects/p/credentials/github")
            assert d.status_code == 200 and d.json()["configured"] is False
        finally:
            github_connector._github_api = prev_api

    _run(body)


def test_credentials_work_in_general():
    def body(env):
        # GENERAL has no project dir, but credential endpoints still work (global scope)
        r = env.client.get("/api/projects/__GENERAL__/credentials/github")
        assert r.status_code == 200
        assert r.json()["configured"] is False

    _run(body)


# ---------- github repo target (owner/repo) ----------


def test_github_repo_endpoint_set_get():
    def body(env):
        env.make_project("p")
        # empty initially
        g0 = env.client.get("/api/projects/p/github/repo")
        assert g0.status_code == 200 and g0.json()["repo"] is None
        # accepts a full URL (with .git) and normalizes to owner/repo
        s = env.client.post("/api/projects/p/github/repo", json={"repo_url": "https://github.com/octo/demo.git"})
        assert s.status_code == 200
        assert s.json()["repo"] == "octo/demo"
        assert s.json()["url"] == "https://github.com/octo/demo"
        # persists + reads back
        g1 = env.client.get("/api/projects/p/github/repo")
        assert g1.json()["repo"] == "octo/demo"
        # a bare owner/repo is accepted too
        s2 = env.client.post("/api/projects/p/github/repo", json={"repo_url": "acme/widgets"})
        assert s2.json()["repo"] == "acme/widgets"
        # garbage is rejected
        bad = env.client.post("/api/projects/p/github/repo", json={"repo_url": "not-a-repo"})
        assert bad.status_code == 400

    _run(body)


def test_push_resolves_stored_repo_target():
    def body(env):
        env.make_project("p")
        env.make_run("p")
        env.client.post("/api/projects/p/execution/runs/run-1/git/commit", json={"confirm": True, "message": "c"})
        credentials.set_github_credential("p", token="ghp_dummdummdummdummdummy")
        # store the project's repo target (no git remote, no owner/repo in the push body)
        env.client.post("/api/projects/p/github/repo", json={"repo_url": "octo/demo"})
        prev_get, prev_ensure, prev_push = (
            github_connector.get_remote,
            github_connector.ensure_remote,
            github_connector.push_branch,
        )
        github_connector.get_remote = lambda pid, **kw: None
        github_connector.ensure_remote = lambda pid, owner, repo, **kw: (True, f"{owner}/{repo}")
        github_connector.push_branch = lambda pid, branch, **kw: github_connector.PushResult(
            ok=True, branch=branch, remote="origin"
        )
        try:
            # preview: target resolved from the stored repo (no owner/repo supplied)
            pv = env.client.post("/api/projects/p/execution/runs/run-1/git/push", json={"confirm": False})
            assert pv.status_code == 200
            assert pv.json()["contract"]["target"] == "octo/demo"
            # confirm: push succeeds against the stored target
            cf = env.client.post("/api/projects/p/execution/runs/run-1/git/push", json={"confirm": True})
            assert cf.status_code == 200 and cf.json()["applied"]
            assert cf.json()["contract"]["target"] == "octo/demo"
            # the stored default_remote persists (drives the clickable repo link)
            assert credentials.get_metadata("github", "default_remote", "p") == "octo/demo"
        finally:
            (
                github_connector.get_remote,
                github_connector.ensure_remote,
                github_connector.push_branch,
            ) = (prev_get, prev_ensure, prev_push)

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
