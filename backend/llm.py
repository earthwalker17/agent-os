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
"""

import providers


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
    """
    provider_id = provider or providers.default_provider()
    return providers.complete(
        provider_id, system, messages, model=model, max_tokens=max_tokens
    )
