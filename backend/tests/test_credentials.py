"""Tests for Task 7.5 — central credential accessor (credentials.py).

Coverage:
  - env fallback, global-file store, project store, and the project > global >
    env resolution order.
  - status() exposes presence/metadata only — never the token value.
  - set / update-metadata / delete round-trips on the gitignored store.
  - redact() strips exact stored token values + common token shapes.
  - project-id sanitization rejects traversal.

Run directly:
    python backend/tests/test_credentials.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import credentials  # noqa: E402


class _Sandbox:
    """Redirect the credential store to a temp dir + clear env tokens."""

    ENV_VARS = credentials._GITHUB_ENV_VARS

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

        self._env_backup = {k: os.environ.get(k) for k in self.ENV_VARS}
        for k in self.ENV_VARS:
            os.environ.pop(k, None)

    def set_env(self, var: str, value: str) -> None:
        import os

        os.environ[var] = value

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
    sb = _Sandbox()
    try:
        test_body(sb)
    finally:
        sb.cleanup()


# ---------- resolution order ----------


def test_no_credential():
    def body(sb):
        assert credentials.get_github_token("proj") is None
        st = credentials.status("proj")
        assert st["configured"] is False and st["source"] == "none"

    _run(body)


def test_env_fallback():
    def body(sb):
        sb.set_env("GITHUB_TOKEN", "env-tok-123")
        assert credentials.get_github_token("proj") == "env-tok-123"
        st = credentials.status("proj")
        assert st["configured"] and st["source"] == "env"

    _run(body)


def test_project_overrides_global_and_env():
    def body(sb):
        sb.set_env("GITHUB_TOKEN", "env-tok")
        credentials.set_github_credential(None, token="global-tok", scope="global")
        credentials.set_github_credential("proj", token="proj-tok", scope="project")
        # project wins for that project
        assert credentials.get_github_token("proj") == "proj-tok"
        assert credentials.status("proj")["source"] == "project"
        # a different project sees the global file (overrides env)
        assert credentials.get_github_token("other") == "global-tok"
        assert credentials.status("other")["source"] == "global_file"

    _run(body)


# ---------- no leakage ----------


def test_status_never_leaks_token():
    def body(sb):
        credentials.set_github_credential("proj", token="topsecret-xyz", login="octocat")
        st = credentials.status("proj")
        blob = json.dumps(st)
        assert "topsecret-xyz" not in blob
        assert "token" not in st
        assert st["login"] == "octocat"

    _run(body)


# ---------- writers ----------


def test_update_metadata_and_delete():
    def body(sb):
        credentials.set_github_credential("proj", token="t1")
        credentials.update_github_metadata("proj", login="me", default_remote="me/repo")
        st = credentials.status("proj")
        assert st["login"] == "me" and st["default_remote"] == "me/repo"
        credentials.delete_github_credential("proj")
        assert credentials.get_github_token("proj") is None

    _run(body)


# ---------- redaction ----------


def test_redact_known_and_patterns():
    def body(sb):
        credentials.set_github_credential("proj", token="stored-secret-aaa")
        text = (
            "remote add origin https://stored-secret-aaa@github.com/x/y\n"
            "token ghp_0123456789abcdefghij0123\n"
            "PASSWORD=hunter2\n"
            "nothing to see here\n"
        )
        out = credentials.redact(text, "proj")
        assert "stored-secret-aaa" not in out
        assert "ghp_0123456789" not in out
        assert "PASSWORD=[REDACTED]" in out
        assert "nothing to see here" in out

    _run(body)


def test_redact_handles_empty():
    def body(sb):
        assert credentials.redact("") == ""
        assert credentials.redact(None) is None

    _run(body)


# ---------- safety ----------


def test_safe_project_id_rejects_traversal():
    def body(sb):
        for bad in ("..", ".", "", "../../etc"):
            try:
                credentials._safe_project_id(bad)
                if bad not in ("../../etc",):
                    assert False, f"expected rejection for {bad!r}"
            except ValueError:
                pass
        # traversal chars are sanitized to underscores, not allowed through
        assert "/" not in credentials._safe_project_id("a/b/c")
        assert "\\" not in credentials._safe_project_id("a\\b")

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
