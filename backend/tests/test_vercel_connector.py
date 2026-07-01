"""Tests for Phase 8 — Vercel connector (execution/vercel_connector.py).

All network is faked via an injected ``opener`` (no real HTTP). Coverage:
  - token rides ONLY in the Authorization header (never argv/url/body).
  - error strings are redacted (a stored token in an error message is scrubbed).
  - create / get / list / promote / set_env_var happy paths + no-token guard.
  - normalize_url strips a protection-bypass query.
  - status is presence-only (validates via /v2/user, never returns the token).

Run directly:
    python backend/tests/test_vercel_connector.py
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import credentials  # noqa: E402
from execution import vercel_connector as vc  # noqa: E402


class _Sandbox:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = (credentials._CRED_DIR, credentials._PROJECTS_DIR, credentials._GLOBAL_FILE)
        credentials._CRED_DIR = root / "credentials"
        credentials._PROJECTS_DIR = credentials._CRED_DIR / "projects"
        credentials._GLOBAL_FILE = credentials._CRED_DIR / "global.json"
        import os

        self._env_backup = {k: os.environ.get(k) for k in vc_env_vars()}
        for k in vc_env_vars():
            os.environ.pop(k, None)

    def cleanup(self) -> None:
        import os

        (credentials._CRED_DIR, credentials._PROJECTS_DIR, credentials._GLOBAL_FILE) = self._prev
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


def vc_env_vars():
    return credentials._PROVIDERS["vercel"]["env_vars"]


def _run(test_body):
    sb = _Sandbox()
    try:
        test_body(sb)
    finally:
        sb.cleanup()


class _FakeResp:
    def __init__(self, code: int, body):
        self._code = code
        self._body = body.encode("utf-8") if isinstance(body, str) else body

    def read(self):
        return self._body

    def getcode(self):
        return self._code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_DEFAULT_LINK = {"type": "github", "org": "o", "repo": "r", "repoId": 12345}


def _ok_opener(code: int, payload: dict, link: dict | None = _DEFAULT_LINK):
    """Returns ``payload`` for the main call; for the GET /v9/projects/{id} link
    lookup (which create_deployment now makes first) returns a project with
    ``link`` so gitSource can be built. ``captured`` reflects the LAST request."""
    captured: dict = {}

    def opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["data"] = req.data.decode("utf-8") if req.data else None
        captured["method"] = req.get_method()
        if "/v9/projects/" in req.full_url and req.get_method() == "GET":
            return _FakeResp(200, json.dumps({"name": "p", "link": link}))
        return _FakeResp(code, json.dumps(payload))

    opener.captured = captured
    return opener


def _http_error_opener(code: int, payload: dict):
    def opener(req, timeout=None):
        # the project-link GET succeeds so create_deployment reaches the POST,
        # where the error is raised (so we exercise POST-error redaction).
        if "/v9/projects/" in req.full_url and req.get_method() == "GET":
            return _FakeResp(200, json.dumps({"name": "p", "link": _DEFAULT_LINK}))
        raise urllib.error.HTTPError(
            req.full_url, code, "err", {}, io.BytesIO(json.dumps(payload).encode("utf-8"))
        )

    return opener


# ---------- token handling ----------


def test_token_only_in_authorization_header():
    def body(sb):
        credentials.set_credential("vercel", "proj", fields={"token": "VercelSecretTok123"})
        credentials.update_metadata("vercel", "proj", {"project_id": "prj_1"})
        op = _ok_opener(200, {"id": "dpl_1", "url": "app.vercel.app", "readyState": "READY"})
        res = vc.create_deployment("proj", name="proj", target="preview", git_ref="main", opener=op)
        assert res.ok and res.deployment_id == "dpl_1"
        # token present in the Authorization header only
        assert op.captured["headers"]["authorization"] == "Bearer VercelSecretTok123"
        # NOT in the URL or the request body
        assert "VercelSecretTok123" not in op.captured["url"]
        assert "VercelSecretTok123" not in (op.captured["data"] or "")
        # gitSource carries the resolved repoId (not a bare ref); preview omits target;
        # forceNew + skipAutoDetectionConfirmation are set
        sent = json.loads(op.captured["data"])
        assert sent["gitSource"] == {"type": "github", "ref": "main", "repoId": 12345}
        assert "target" not in sent  # preview is the default (omitted)
        assert "forceNew=1" in op.captured["url"] and "skipAutoDetectionConfirmation=1" in op.captured["url"]

    _run(body)


def test_deploy_unlinked_project_errors_clearly():
    def body(sb):
        credentials.set_credential("vercel", "proj", fields={"token": "t"})
        credentials.update_metadata("vercel", "proj", {"project_id": "prj_1"})
        op = _ok_opener(200, {"id": "dpl_1", "readyState": "READY"}, link=None)
        res = vc.create_deployment("proj", name="proj", target="preview", git_ref="main", opener=op)
        assert not res.ok and "connected git repo" in (res.error or "")

    _run(body)


def test_production_target_is_sent():
    def body(sb):
        credentials.set_credential("vercel", "proj", fields={"token": "t"})
        credentials.update_metadata("vercel", "proj", {"project_id": "prj_1"})
        op = _ok_opener(200, {"id": "dpl_p", "url": "p.vercel.app", "readyState": "READY"})
        vc.create_deployment("proj", name="proj", target="production", git_ref="main", opener=op)
        assert json.loads(op.captured["data"])["target"] == "production"

    _run(body)


def test_no_token_returns_error():
    def body(sb):
        res = vc.create_deployment("proj", name="proj", target="preview", git_ref="main")
        assert not res.ok and "no Vercel token" in (res.error or "")

    _run(body)


def test_error_string_is_redacted():
    def body(sb):
        credentials.set_credential("vercel", "proj", fields={"token": "LeakyTok999"})
        credentials.update_metadata("vercel", "proj", {"project_id": "prj_1"})
        # the API echoes the token back in an error message (worst case)
        op = _http_error_opener(401, {"error": {"message": "invalid token LeakyTok999"}})
        res = vc.create_deployment("proj", name="proj", target="preview", git_ref="main", opener=op)
        assert not res.ok
        assert "LeakyTok999" not in (res.error or "")
        assert "[REDACTED]" in (res.error or "")

    _run(body)


# ---------- url normalization ----------


def test_normalize_url_strips_bypass_token():
    assert vc.normalize_url("app-xyz.vercel.app") == "https://app-xyz.vercel.app"
    assert (
        vc.normalize_url("https://app.vercel.app/path?x-vercel-protection-bypass=SEKRET#frag")
        == "https://app.vercel.app/path"
    )
    assert vc.normalize_url(None) is None
    assert vc.normalize_url("") is None


# ---------- operations ----------


def test_get_deployment_reports_state():
    def body(sb):
        credentials.set_credential("vercel", "proj", fields={"token": "t"})
        op = _ok_opener(200, {"id": "dpl_9", "url": "a.vercel.app", "readyState": "BUILDING"})
        res = vc.get_deployment("proj", "dpl_9", opener=op)
        assert res.ok and res.ready_state == "BUILDING" and res.url == "https://a.vercel.app"

    _run(body)


def test_list_deployments():
    def body(sb):
        credentials.set_credential("vercel", "proj", fields={"token": "t"})
        credentials.update_metadata("vercel", "proj", {"project_id": "prj_1"})
        op = _ok_opener(200, {"deployments": [
            {"uid": "dpl_a", "url": "a.vercel.app", "readyState": "READY", "target": "production"},
            {"uid": "dpl_b", "url": "b.vercel.app", "state": "READY"},
        ]})
        deps, err = vc.list_deployments("proj", opener=op)
        assert err is None and len(deps) == 2
        assert deps[0]["deployment_id"] == "dpl_a" and deps[0]["url"] == "https://a.vercel.app"

    _run(body)


def test_promote_requires_linked_project():
    def body(sb):
        credentials.set_credential("vercel", "proj", fields={"token": "t"})
        res = vc.promote_deployment("proj", "dpl_old")  # no project linked
        assert not res.ok and "project linked" in (res.error or "")
        credentials.update_metadata("vercel", "proj", {"project_id": "prj_1"})
        op = _ok_opener(200, {"ok": True})
        res2 = vc.promote_deployment("proj", "dpl_old", opener=op)
        assert res2.ok and res2.deployment_id == "dpl_old"

    _run(body)


def test_set_env_var_sensitive_type_in_body():
    def body(sb):
        credentials.set_credential("vercel", "proj", fields={"token": "t"})
        credentials.update_metadata("vercel", "proj", {"project_id": "prj_1"})
        op = _ok_opener(201, {"created": {"id": "env_1"}})
        res = vc.set_env_var(
            "proj", "DATABASE_URL", "postgres://secret", targets=["production"], var_type="sensitive", opener=op
        )
        assert res.ok and res.env_id == "env_1"
        sent = json.loads(op.captured["data"])
        assert sent["type"] == "sensitive" and sent["key"] == "DATABASE_URL"
        assert "upsert=true" in op.captured["url"]

    _run(body)


def test_set_env_var_sensitive_drops_development_target():
    """Vercel rejects a Sensitive env var that targets ``development``
    ("You cannot set a Sensitive Environment Variable's target to development").
    A sensitive var requested for all three targets must be sent as
    production/preview only."""
    def body(sb):
        credentials.set_credential("vercel", "proj", fields={"token": "t"})
        credentials.update_metadata("vercel", "proj", {"project_id": "prj_1"})
        op = _ok_opener(201, {"created": {"id": "env_2"}})
        res = vc.set_env_var(
            "proj", "STRIPE_SECRET_KEY", "sk_test_x",
            targets=["production", "preview", "development"], var_type="sensitive", opener=op,
        )
        assert res.ok
        sent = json.loads(op.captured["data"])
        assert "development" not in sent["target"]
        assert set(sent["target"]) == {"production", "preview"}

    _run(body)


def test_set_env_var_plain_keeps_all_targets():
    """A non-sensitive (plain) var keeps every requested target, including
    ``development`` — the drop is scoped to sensitive vars only."""
    def body(sb):
        credentials.set_credential("vercel", "proj", fields={"token": "t"})
        credentials.update_metadata("vercel", "proj", {"project_id": "prj_1"})
        op = _ok_opener(201, {"created": {"id": "env_3"}})
        res = vc.set_env_var(
            "proj", "NEXT_PUBLIC_X", "v",
            targets=["production", "preview", "development"], var_type="plain", opener=op,
        )
        assert res.ok
        sent = json.loads(op.captured["data"])
        assert set(sent["target"]) == {"production", "preview", "development"}

    _run(body)


def test_status_presence_only():
    def body(sb):
        credentials.set_credential("vercel", "proj", fields={"token": "TopSecretVercel"})
        op = _ok_opener(200, {"user": {"username": "octo"}})
        st = vc.status("proj", opener=op)
        d = st.to_dict()
        assert d["configured"] and d["connected"] and d["username"] == "octo"
        assert "TopSecretVercel" not in json.dumps(d)
        assert "token" not in d

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
