"""Tests for Task 7.6 — GitHub connector (github_connector.py).

No real network / no real push. Coverage:
  - parse_remote_url across URL shapes.
  - ensure_remote points at a TOKENLESS URL (add vs set-url).
  - status() validates via an injected API stub (connected / error / no-token).
  - push_branch: the token is injected ONLY via env (GIT_ASKPASS +
    AGENT_OS_GIT_PASSWORD), NEVER in the git argv; a token echoed in git's
    error output is redacted in the returned message.
  - create_pull_request: success + API-error + network-error paths.

Run directly:
    python backend/tests/test_github_connector.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import credentials  # noqa: E402
from execution import github_connector as gc  # noqa: E402
from execution.tool_models import ToolResult  # noqa: E402


class _CredSandbox:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = (
            credentials._CRED_DIR,
            credentials._PROJECTS_DIR,
            credentials._GLOBAL_FILE,
        )
        credentials._CRED_DIR = root / "credentials"
        credentials._PROJECTS_DIR = credentials._CRED_DIR / "projects"
        credentials._GLOBAL_FILE = credentials._CRED_DIR / "global.json"
        import os

        self._env_backup = {k: os.environ.get(k) for k in credentials._GITHUB_ENV_VARS}
        for k in credentials._GITHUB_ENV_VARS:
            os.environ.pop(k, None)

    def cleanup(self) -> None:
        import os

        (
            credentials._CRED_DIR,
            credentials._PROJECTS_DIR,
            credentials._GLOBAL_FILE,
        ) = self._prev
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


def _run(test_body):
    sb = _CredSandbox()
    try:
        test_body(sb)
    finally:
        sb.cleanup()


class FakeRuntime:
    def __init__(self, result_fn=None):
        self.calls = []
        self._fn = result_fn

    def run_git(self, args, *, allow_destructive=False, env_extra=None, timeout_seconds=60):
        args = list(args)
        self.calls.append({"args": args, "env_extra": dict(env_extra or {})})
        if self._fn is not None:
            return self._fn(args, env_extra)
        return ToolResult(
            success=True, tool_name="run_git", output="",
            metadata={"command": "git " + " ".join(args), "args": args},
        )


# ---------- parse / remote ----------


def test_parse_remote_url():
    assert gc.parse_remote_url("https://github.com/octocat/Hello-World.git") == ("octocat", "Hello-World")
    assert gc.parse_remote_url("git@github.com:octocat/Hello-World.git") == ("octocat", "Hello-World")
    assert gc.parse_remote_url("https://github.com/octocat/Hello-World") == ("octocat", "Hello-World")
    assert gc.parse_remote_url("https://example.com/x/y") is None


def test_ensure_remote_tokenless_add():
    def body(_):
        def fn(args, env):
            if args[:2] == ["remote", "get-url"]:
                return ToolResult(success=False, tool_name="run_git", error="no such remote")
            return ToolResult(success=True, tool_name="run_git", output="")
        rt = FakeRuntime(fn)
        ok, url = gc.ensure_remote("proj", "octocat", "Hello-World", runtime=rt)
        assert ok
        assert url == "https://github.com/octocat/Hello-World.git"
        add_calls = [c for c in rt.calls if c["args"][:2] == ["remote", "add"]]
        assert add_calls and add_calls[0]["args"][-1] == url
        # tokenless — no '@' / credentials in the URL
        assert "@" not in url

    _run(body)


# ---------- status ----------


def test_status_connected():
    def body(_):
        credentials.set_github_credential("proj", token="ghp_xxxxxxxxxxxxxxxxxxxx")
        def api(method, path, token, payload=None):
            assert path == "/user"
            return 200, {"login": "octocat"}
        st = gc.status("proj", api=api)
        assert st.configured and st.connected and st.login == "octocat"
        assert "ghp_xxxx" not in st.to_dict().__str__()

    _run(body)


def test_status_no_token():
    def body(_):
        st = gc.status("proj")
        assert not st.configured and not st.connected

    _run(body)


def test_status_api_error():
    def body(_):
        credentials.set_github_credential("proj", token="ghp_xxxxxxxxxxxxxxxxxxxx")
        def api(method, path, token, payload=None):
            return 401, {"message": "Bad credentials"}
        st = gc.status("proj", api=api)
        assert st.configured and not st.connected
        assert "Bad credentials" in (st.error or "")

    _run(body)


# ---------- push ----------


def test_push_token_only_in_env_never_argv():
    def body(_):
        token = "ghp_secretpushtoken1234567890"
        credentials.set_github_credential("proj", token=token)
        rt = FakeRuntime()
        res = gc.push_branch("proj", "feature/x", runtime=rt)
        assert res.ok
        call = rt.calls[-1]
        # argv carries no secret
        assert token not in " ".join(call["args"])
        assert call["args"] == ["push", "-u", "origin", "feature/x"]
        # token only in env, via askpass
        assert call["env_extra"]["AGENT_OS_GIT_PASSWORD"] == token
        assert call["env_extra"]["GIT_ASKPASS"]
        assert call["env_extra"]["GIT_TERMINAL_PROMPT"] == "0"

    _run(body)


def test_push_failure_redacts_token():
    def body(_):
        token = "ghp_secretpushtoken1234567890"
        credentials.set_github_credential("proj", token=token)
        def fn(args, env):
            return ToolResult(
                success=False, tool_name="run_git",
                error=f"fatal: could not read Password: {token}",
            )
        rt = FakeRuntime(fn)
        res = gc.push_branch("proj", "feature/x", runtime=rt)
        assert not res.ok
        assert token not in (res.error or "")

    _run(body)


def test_push_no_token():
    def body(_):
        res = gc.push_branch("proj", "feature/x", runtime=FakeRuntime())
        assert not res.ok and "no GitHub token" in res.error

    _run(body)


# ---------- pull request ----------


def test_create_pr_success():
    def body(_):
        credentials.set_github_credential("proj", token="ghp_xxxxxxxxxxxxxxxxxxxx")
        def api(method, path, token, payload=None):
            assert method == "POST" and "/pulls" in path
            assert payload["head"] == "feature/x" and payload["base"] == "main"
            return 201, {"html_url": "https://github.com/o/r/pull/9", "number": 9}
        res = gc.create_pull_request(
            "proj", owner="o", repo="r", head="feature/x", base="main",
            title="Add x", body="body", api=api,
        )
        assert res.ok and res.number == 9
        assert res.url.endswith("/pull/9")

    _run(body)


def test_create_pr_api_error():
    def body(_):
        credentials.set_github_credential("proj", token="ghp_xxxxxxxxxxxxxxxxxxxx")
        def api(method, path, token, payload=None):
            return 422, {"message": "Validation Failed", "errors": [{"message": "exists"}]}
        res = gc.create_pull_request(
            "proj", owner="o", repo="r", head="f", base="main", title="t", api=api
        )
        assert not res.ok and "Validation Failed" in res.error

    _run(body)


def test_create_pr_network_error():
    def body(_):
        credentials.set_github_credential("proj", token="ghp_xxxxxxxxxxxxxxxxxxxx")
        def api(method, path, token, payload=None):
            raise gc.ConnectorError("network error calling GitHub: timed out")
        res = gc.create_pull_request(
            "proj", owner="o", repo="r", head="f", base="main", title="t", api=api
        )
        assert not res.ok and "network error" in res.error

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
