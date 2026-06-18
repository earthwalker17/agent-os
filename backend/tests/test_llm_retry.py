"""Tests for llm.chat transient-failure retry (autonomy hardening).

A single mid-run "Connection error" used to kill an entire Coding Agent task
(and cascade its dependents to ``skipped``) — exactly how the Aegis Launch
Control build lost task ``t2``. ``llm.chat`` now retries *transient* failures
with bounded backoff and leaves *deterministic* failures (bad key, 400/401/404,
unknown provider) un-retried.

Coverage:
  - ``_is_transient`` classification (transient vs permanent markers).
  - retry-then-succeed: a transient failure is retried and the later success
    is returned.
  - permanent failures are not retried.
  - a persistently-transient failure exhausts the retry budget and re-raises.

Run directly:
    python backend/tests/test_llm_retry.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import llm  # noqa: E402
import providers  # noqa: E402


class _Patched:
    """Swap providers.complete + neutralize backoff sleep for the duration."""

    def __init__(self, complete_fn):
        self._complete_fn = complete_fn

    def __enter__(self):
        self._orig_complete = providers.complete
        self._orig_sleep = llm.time.sleep
        providers.complete = self._complete_fn  # type: ignore[assignment]
        llm.time.sleep = lambda *_a, **_k: None  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        providers.complete = self._orig_complete  # type: ignore[assignment]
        llm.time.sleep = self._orig_sleep  # type: ignore[assignment]
        return False


def test_is_transient_classification():
    assert llm._is_transient(Exception("Connection error.")) is True
    assert llm._is_transient(Exception("network error calling x: timed out")) is True
    assert llm._is_transient(Exception("HTTP 529 from api: overloaded")) is True
    assert llm._is_transient(Exception("HTTP 503 service unavailable")) is True
    assert llm._is_transient(Exception("rate limit exceeded")) is True
    # Deterministic — must NOT retry.
    assert llm._is_transient(Exception("HTTP 400 invalid_request")) is False
    assert llm._is_transient(Exception("HTTP 401 unauthorized")) is False
    assert llm._is_transient(Exception("provider 'gpt' is not available")) is False
    assert llm._is_transient(Exception("api key missing")) is False
    # Unrecognized -> treated as permanent (conservative).
    assert llm._is_transient(Exception("something weird happened")) is False


def test_transient_retried_then_succeeds():
    calls = {"n": 0}

    def fake_complete(*_a, **_k) -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise providers.ProviderError(
                "network error calling https://api: Connection error"
            )
        return "ok"

    with _Patched(fake_complete):
        out = llm.chat("sys", [{"role": "user", "content": "hi"}], provider="claude")
    assert out == "ok"
    assert calls["n"] == 3  # two failures retried, third succeeded


def test_permanent_not_retried():
    calls = {"n": 0}

    def fake_complete(*_a, **_k) -> str:
        calls["n"] += 1
        raise providers.ProviderError("HTTP 401 from api: unauthorized")

    raised = False
    with _Patched(fake_complete):
        try:
            llm.chat("sys", [{"role": "user", "content": "hi"}], provider="claude")
        except providers.ProviderError:
            raised = True
    assert raised
    assert calls["n"] == 1  # no retry on a deterministic failure


def test_transient_exhausts_then_raises():
    calls = {"n": 0}

    def fake_complete(*_a, **_k) -> str:
        calls["n"] += 1
        raise providers.ProviderError("Connection error.")

    raised = False
    with _Patched(fake_complete):
        try:
            llm.chat("sys", [{"role": "user", "content": "hi"}], provider="claude")
        except providers.ProviderError:
            raised = True
    assert raised
    # First attempt + _MAX_RETRIES retries.
    assert calls["n"] == llm._MAX_RETRIES + 1


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
