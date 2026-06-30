"""Tests for Phase 8 — Supabase connector (execution/supabase_connector.py).

The CLI executor + Management API are faked (no real supabase / network).
Coverage:
  - migration_apply runs `db push --linked` with allow_destructive=True and the
    secrets in env_extra (access token + DB password), never in argv.
  - the connector redacts CLI output before returning (DB password scrubbed).
  - a Docker-missing CLI result is detected + surfaced as a soft signal.
  - status validates the access token via the Management API, presence-only.

Run directly:
    python backend/tests/test_supabase_connector.py
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
from execution import supabase_connector as sc  # noqa: E402
from execution.tool_models import ToolResult  # noqa: E402


class _Sandbox:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = (credentials._CRED_DIR, credentials._PROJECTS_DIR, credentials._GLOBAL_FILE)
        credentials._CRED_DIR = root / "credentials"
        credentials._PROJECTS_DIR = credentials._CRED_DIR / "projects"
        credentials._GLOBAL_FILE = credentials._CRED_DIR / "global.json"
        import os

        self._eb = {k: os.environ.get(k) for k in credentials._PROVIDERS["supabase"]["env_vars"]}
        for k in credentials._PROVIDERS["supabase"]["env_vars"]:
            os.environ.pop(k, None)

    def cleanup(self) -> None:
        import os

        (credentials._CRED_DIR, credentials._PROJECTS_DIR, credentials._GLOBAL_FILE) = self._prev
        for k, v in self._eb.items():
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


class _FakeRuntime:
    def __init__(self, result: ToolResult):
        self.result = result
        self.calls: list[dict] = []

    def run_supabase(self, argv, *, allow_destructive=False, env_extra=None, timeout_seconds=180):
        self.calls.append({"argv": list(argv), "allow_destructive": allow_destructive, "env_extra": env_extra})
        return self.result


def _tr(success=True, output="", error="", **md):
    return ToolResult(success=success, tool_name="run_supabase", output=output, error=error, metadata=md)


def _supa_creds():
    credentials.set_credential(
        "supabase", "p",
        fields={"access_token": "sbp_tok0123456789abcdef", "db_password": "supersecretpw"},
    )
    credentials.update_metadata("supabase", "p", {"project_ref": "abcdefabcdef"})


def test_migration_apply_uses_secrets_and_destructive_flag():
    def body(sb):
        _supa_creds()
        rt = _FakeRuntime(_tr(success=True, output="Applying migration 0001_init.sql ... done"))
        res = sc.migration_apply("p", runtime=rt)
        assert res.ok
        call = rt.calls[0]
        assert call["argv"] == ["db", "push", "--linked"]
        assert call["allow_destructive"] is True
        assert call["env_extra"]["SUPABASE_ACCESS_TOKEN"] == "sbp_tok0123456789abcdef"
        assert call["env_extra"]["SUPABASE_DB_PASSWORD"] == "supersecretpw"
        # the password is NOT in argv
        assert "supersecretpw" not in " ".join(call["argv"])

    _run(body)


def test_cli_output_is_redacted():
    def body(sb):
        _supa_creds()
        rt = _FakeRuntime(_tr(success=True, output="connecting postgres://postgres:supersecretpw@db ... ok"))
        res = sc.migration_preview("p", runtime=rt)
        assert "supersecretpw" not in res.output  # redacted at the connector
        # dry-run preview does not need destructive confirmation
        assert rt.calls[0]["argv"] == ["db", "push", "--dry-run", "--linked"]
        assert rt.calls[0]["allow_destructive"] is False

    _run(body)


def test_docker_missing_detected():
    def body(sb):
        _supa_creds()
        rt = _FakeRuntime(_tr(success=False, error="Cannot connect to the Docker daemon. Is docker running?"))
        res = sc.migration_diff("p", runtime=rt)
        assert res.docker_missing is True and res.ok is False

    _run(body)


def test_link_argv_and_password_env():
    def body(sb):
        _supa_creds()
        rt = _FakeRuntime(_tr(success=True, output="Finished supabase link."))
        res = sc.link("p", "abcdefabcdef", runtime=rt)
        assert res.ok and rt.calls[0]["argv"] == ["link", "--project-ref", "abcdefabcdef"]
        assert rt.calls[0]["env_extra"]["SUPABASE_DB_PASSWORD"] == "supersecretpw"

    _run(body)


def test_status_presence_only():
    def body(sb):
        _supa_creds()

        class _Resp:
            def __init__(self, code, body):
                self._c, self._b = code, json.dumps(body).encode()
            def read(self): return self._b
            def getcode(self): return self._c
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(req, timeout=None):
            return _Resp(200, {"id": "abcdefabcdef", "name": "proj"})

        st = sc.status("p", opener=opener).to_dict()
        assert st["configured"] and st["connected"] and st["linked"]
        assert st["project_ref"] == "abcdefabcdef"
        assert st["has_db_password"] is True
        assert "supersecretpw" not in json.dumps(st)
        assert "sbp_tok0123456789abcdef" not in json.dumps(st)

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
