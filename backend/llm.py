"""
Thin LLM wrapper for Agent OS.

A single `chat(system, messages, ...)` entry point. As of Task 07.1 it
delegates to the pluggable provider layer (`providers.py`) so the same call
can route to Claude / GPT / Gemini / DeepSeek. All context assembly still
happens in the callers (orchestrator + execution modules) — this module only
knows how to send messages and return text.

Backward compatibility: callers that don't pass `provider` get the default
provider (Claude when `ANTHROPIC_API_KEY` is set, otherwise the first
available), so existing Anthropic-only behavior is unchanged.

Transient-failure resilience: a single network blip used to kill an entire
Coding Agent task — the runner's per-task loop treats any `llm.chat` exception
as a fatal protocol error, which then cascades its dependents to ``skipped``
(this is exactly how the Aegis Launch Control build lost task ``t2`` to a
mid-run "Connection error"). ``chat`` now retries *transient* failures
(connection drops, timeouts, 429/5xx/overloaded) with bounded exponential
backoff before giving up. Deterministic failures (bad key, 400/401/403/404,
unknown provider) are NOT retried — re-sending them would only waste time.
Retries are configurable via ``AGENT_OS_LLM_MAX_RETRIES`` /
``AGENT_OS_LLM_RETRY_BASE_DELAY`` and add latency only on the rare failure
path; the happy path is unchanged.
"""

from __future__ import annotations

import logging
import os
import time

import providers


log = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


# Number of *extra* attempts after the first one (so total tries = retries + 1).
# 3 retries over exponential backoff spans a long enough window (≈ base*(1+2+4))
# to ride out a brief connectivity hiccup without stalling a run for minutes.
_MAX_RETRIES = max(0, _int_env("AGENT_OS_LLM_MAX_RETRIES", 3))
# Base backoff delay in seconds; doubled each retry, capped at _MAX_BACKOFF.
_RETRY_BASE_DELAY = max(0.0, _float_env("AGENT_OS_LLM_RETRY_BASE_DELAY", 1.5))
_MAX_BACKOFF = 20.0

# Substrings (matched case-insensitively against ``str(exc)`` and the exception
# class name) that mark a failure as worth retrying. Kept deliberately broad on
# the transient side and narrow on the deterministic side.
_TRANSIENT_MARKERS = (
    "connection error",
    "connection aborted",
    "connection reset",
    "connection refused",
    "network error",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "service unavailable",
    "overloaded",
    "rate limit",
    "too many requests",
    "remote disconnected",
    "broken pipe",
    "eof occurred",
    "ssl",
    " 429",
    " 500",
    " 502",
    " 503",
    " 504",
    " 529",
    "apiconnectionerror",
    "apitimeouterror",
    "internalservererror",
    "ratelimiterror",
)

# Substrings that mark a failure as deterministic — never retry these, even if a
# transient marker also matches (a "401 ... connection" style message stays a
# hard auth failure). Checked first.
_PERMANENT_MARKERS = (
    "api key",
    "unauthorized",
    "authentication",
    "permission",
    "forbidden",
    "not_found",
    "not found",
    "unknown provider",
    "is not available",
    "invalid_request",
    "invalid request",
    " 400",
    " 401",
    " 403",
    " 404",
)


def _is_transient(exc: Exception) -> bool:
    """Heuristically classify whether ``exc`` is worth retrying.

    Conservative: only failures matching a transient marker (and not a
    permanent one) are retried. Anything unrecognized is treated as permanent
    so we never loop on a deterministic error.
    """
    haystack = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in haystack for marker in _PERMANENT_MARKERS):
        return False
    return any(marker in haystack for marker in _TRANSIENT_MARKERS)


def chat(
    system: str,
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 2048,
    provider: str | None = None,
) -> str:
    """Send a chat request and return the assistant's text response.

    ``provider`` is a provider id (``claude`` / ``gpt`` / ``gemini`` /
    ``deepseek``); when omitted, the default provider is used. ``model`` is an
    optional per-call override — when omitted, the provider's default model is
    used. Raises ``providers.ProviderError`` if the provider is unknown or its
    API key is not configured.

    Transient failures (see ``_is_transient``) are retried up to
    ``_MAX_RETRIES`` times with exponential backoff; the final failure is
    re-raised unchanged so callers see the original exception type/message.
    """
    provider_id = provider or providers.default_provider()

    attempt = 0
    while True:
        try:
            return providers.complete(
                provider_id, system, messages, model=model, max_tokens=max_tokens
            )
        except Exception as exc:  # noqa: BLE001 — re-raised below if not retryable
            if attempt >= _MAX_RETRIES or not _is_transient(exc):
                raise
            delay = min(_RETRY_BASE_DELAY * (2 ** attempt), _MAX_BACKOFF)
            log.warning(
                "llm.chat transient failure (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1,
                _MAX_RETRIES + 1,
                delay,
                exc,
            )
            if delay > 0:
                time.sleep(delay)
            attempt += 1
