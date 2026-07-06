"""Tests for Phase 10 — the web-search adapter (execution/web_search.py).

No network: the urllib opener is injected. The API key is stored in a
tempdir-backed credential store so redaction is exercised for real.

Run:  python backend/tests/test_web_search.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
from email.message import Message
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import credentials  # noqa: E402
from execution import web_search  # noqa: E402
from execution.web_search import SearchError, search_web  # noqa: E402


KEY = "tvly_test_key_9f8e7d6c"


class _Env:
    """Tempdir credential store + cleared search env fallbacks."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._restore = [
            (credentials, "_CRED_DIR", credentials._CRED_DIR),
            (credentials, "_PROJECTS_DIR", credentials._PROJECTS_DIR),
            (credentials, "_GLOBAL_FILE", credentials._GLOBAL_FILE),
        ]
        credentials._CRED_DIR = root / "credentials"
        credentials._PROJECTS_DIR = root / "credentials" / "projects"
        credentials._GLOBAL_FILE = root / "credentials" / "global.json"
        self._env_backup = {
            k: os.environ.pop(k, None)
            for k in credentials._PROVIDERS["search"]["env_vars"]
        }

    def store_key(self, project_id="proj", **extra_fields):
        credentials.set_credential(
            "search", project_id, fields={"api_key": KEY, **extra_fields}
        )

    def cleanup(self):
        for obj, attr, val in self._restore:
            setattr(obj, attr, val)
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


class _FakeResp:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_opener(result):
    calls: list = []

    def opener(req, timeout=None):
        calls.append(req)
        if isinstance(result, Exception):
            raise result
        return result

    return opener, calls


def _run(test_body):
    env = _Env()
    try:
        test_body(env)
    finally:
        env.cleanup()


# ---------- request shape ----------


def test_tavily_request_shape_key_in_header_only():
    def body(env):
        env.store_key()
        resp = _FakeResp(json.dumps({"results": []}))
        opener, calls = _make_opener(resp)
        out = search_web("fastapi streaming", project_id="proj", max_results=5, opener=opener)
        assert out == []
        assert len(calls) == 1
        req = calls[0]
        assert req.full_url == "https://api.tavily.com/search"
        assert KEY not in req.full_url
        auth = req.headers.get("Authorization")
        assert auth == f"Bearer {KEY}"
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["query"] == "fastapi streaming"
        assert payload["max_results"] == 5
        assert payload["include_answer"] is False

    _run(body)


def test_max_results_clamped_to_tavily_range():
    def body(env):
        env.store_key()
        opener, calls = _make_opener(_FakeResp(json.dumps({"results": []})))
        search_web("q", project_id="proj", max_results=50, opener=opener)
        assert json.loads(calls[0].data)["max_results"] == 10

    _run(body)


# ---------- response parsing ----------


def test_results_parsed_and_snippet_capped():
    def body(env):
        env.store_key()
        long_content = "x" * 1000
        resp = _FakeResp(json.dumps({
            "results": [
                {"title": "Doc", "url": "https://docs.python.org/3/", "content": long_content},
                {"title": "", "url": "https://example.com/a", "content": "short"},
                {"url": "", "content": "no url — dropped"},
                "not-a-dict",
            ]
        }))
        opener, _ = _make_opener(resp)
        out = search_web("q", project_id="proj", opener=opener)
        assert len(out) == 2
        assert out[0]["title"] == "Doc"
        assert len(out[0]["snippet"]) <= web_search.MAX_SNIPPET_CHARS + 1  # + ellipsis
        # missing title falls back to the url
        assert out[1]["title"] == "https://example.com/a"

    _run(body)


def test_non_json_and_missing_results_raise():
    def body(env):
        env.store_key()
        opener, _ = _make_opener(_FakeResp("<html>gateway error</html>"))
        try:
            search_web("q", project_id="proj", opener=opener)
            raise RuntimeError("expected SearchError for non-JSON")
        except SearchError:
            pass
        opener2, _ = _make_opener(_FakeResp(json.dumps({"answer": "no results key"})))
        try:
            search_web("q", project_id="proj", opener=opener2)
            raise RuntimeError("expected SearchError for missing results")
        except SearchError:
            pass

    _run(body)


# ---------- errors are redacted ----------


def test_http_error_is_search_error_with_key_redacted():
    def body(env):
        env.store_key()
        hdrs = Message()
        err = urllib.error.HTTPError(
            "https://api.tavily.com/search", 401, "Unauthorized", hdrs, None
        )
        # simulate a provider echoing the key back in the error body
        err.read = lambda: f'{{"detail": "bad key {KEY}"}}'.encode("utf-8")  # type: ignore[attr-defined]
        opener, _ = _make_opener(err)
        try:
            search_web("q", project_id="proj", opener=opener)
            raise RuntimeError("expected SearchError")
        except SearchError as e:
            msg = str(e)
            assert "401" in msg
            assert KEY not in msg

    _run(body)


def test_network_error_is_search_error():
    def body(env):
        env.store_key()
        opener, _ = _make_opener(urllib.error.URLError("connection refused"))
        try:
            search_web("q", project_id="proj", opener=opener)
            raise RuntimeError("expected SearchError")
        except SearchError as e:
            assert "network" in str(e).lower()

    _run(body)


# ---------- configuration gates ----------


def test_no_key_raises_config_hint():
    def body(env):
        opener, calls = _make_opener(_FakeResp("{}"))
        try:
            search_web("q", project_id="proj", opener=opener)
            raise RuntimeError("expected SearchError without a key")
        except SearchError as e:
            assert "TAVILY_API_KEY" in str(e)
        assert calls == []  # nothing sent

    _run(body)


def test_unknown_engine_rejected_cleanly():
    def body(env):
        env.store_key(engine="brave")
        opener, calls = _make_opener(_FakeResp("{}"))
        try:
            search_web("q", project_id="proj", opener=opener)
            raise RuntimeError("expected SearchError for unimplemented engine")
        except SearchError as e:
            assert "brave" in str(e).lower()
        assert calls == []

    _run(body)


def test_empty_query_rejected():
    def body(env):
        env.store_key()
        try:
            search_web("   ", project_id="proj")
            raise RuntimeError("expected SearchError for empty query")
        except SearchError:
            pass

    _run(body)


# ---------- standalone runner ----------


def _run_all() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    total = sum(1 for n, f in globals().items() if n.startswith("test_") and callable(f))
    if failures:
        print(f"\n{failures} of {total} tests failed.")
        return 1
    print(f"\nAll {total} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
