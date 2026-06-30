"""Tests for Phase 8 — app-env registry (execution/app_env.py).

Coverage:
  - set / list / delete round-trip on the gitignored credentials/env store.
  - list_env is presence-only (key + targets + secret + is_set, NEVER a value).
  - credentials.get_env_value is the single value reader.
  - secret app-env values are exact-matched out by credentials.redact; public
    (secret=False) values are not auto-redacted.
  - key/value validation.

Run directly:
    python backend/tests/test_app_env.py
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
from execution import app_env  # noqa: E402


class _Sandbox:
    """Redirect the credential store (and thus the env store) to a temp dir."""

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

    def cleanup(self) -> None:
        (
            credentials._CRED_DIR,
            credentials._PROJECTS_DIR,
            credentials._GLOBAL_FILE,
        ) = self._prev
        self.tmp.cleanup()


def _run(test_body):
    sb = _Sandbox()
    try:
        test_body(sb)
    finally:
        sb.cleanup()


def test_set_list_delete():
    def body(sb):
        app_env.set_env_var(
            "proj", "DATABASE_URL", "postgres://u:p@h/db", targets=["production", "preview"]
        )
        app_env.set_env_var("proj", "NEXT_PUBLIC_SUPABASE_URL", "https://x.supabase.co", secret=False)
        entries = app_env.list_env("proj")
        assert {e["key"] for e in entries} == {"DATABASE_URL", "NEXT_PUBLIC_SUPABASE_URL"}
        db = next(e for e in entries if e["key"] == "DATABASE_URL")
        assert db["secret"] is True and db["is_set"] is True
        assert set(db["targets"]) == {"production", "preview"}
        pub = next(e for e in entries if e["key"] == "NEXT_PUBLIC_SUPABASE_URL")
        assert pub["secret"] is False
        assert app_env.delete_env_var("proj", "DATABASE_URL") is True
        assert {e["key"] for e in app_env.list_env("proj")} == {"NEXT_PUBLIC_SUPABASE_URL"}
        assert app_env.delete_env_var("proj", "DATABASE_URL") is False

    _run(body)


def test_list_never_returns_value():
    def body(sb):
        app_env.set_env_var("proj", "SECRET_X", "top-secret-value-123")
        listing = app_env.list_env("proj")
        blob = json.dumps(listing)
        assert "top-secret-value-123" not in blob
        for e in listing:
            assert "value" not in e

    _run(body)


def test_get_env_value_is_the_reader():
    def body(sb):
        app_env.set_env_var("proj", "STRIPE_SECRET_KEY", "sk_test_appenvvalue123")
        assert credentials.get_env_value("proj", "STRIPE_SECRET_KEY") == "sk_test_appenvvalue123"
        assert credentials.get_env_value("proj", "MISSING") is None
        assert credentials.get_env_value("other", "STRIPE_SECRET_KEY") is None

    _run(body)


def test_secret_values_are_redacted_public_are_not():
    def body(sb):
        app_env.set_env_var("proj", "DATABASE_URL", "postgres://u:p@h/db?marker=ZZZsecret", secret=True)
        app_env.set_env_var("proj", "NEXT_PUBLIC_URL", "https://public.example.test", secret=False)
        out = credentials.redact("connecting postgres://u:p@h/db?marker=ZZZsecret now", "proj")
        assert "ZZZsecret" not in out  # the whole secret value is exact-matched out
        out2 = credentials.redact("ui shows https://public.example.test here", "proj")
        assert "https://public.example.test" in out2  # public value not auto-redacted

    _run(body)


def test_validation_rejects_empty():
    def body(sb):
        for key, val in (("", "v"), ("K", ""), ("K", "   ")):
            try:
                app_env.set_env_var("proj", key, val)
                assert False, f"expected ValueError for {key!r}/{val!r}"
            except ValueError:
                pass

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
