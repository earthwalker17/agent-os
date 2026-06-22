"""Task 07.1 — pluggable model-provider layer.

A small registry mapping a provider id (``claude`` / ``gpt`` / ``gemini`` /
``deepseek``) to:

- a human label,
- the env var holding its API key,
- a default model (overridable via env so it's easy to bump later),
- a ``complete()`` implementation that turns ``(system, messages)`` into text.

**Availability is purely key-presence:** a provider is available iff its API
key env var is set. The UI shows all four providers; unavailable ones render
disabled.

Only Anthropic uses an SDK (already a dependency). GPT, Gemini, and DeepSeek
are called over plain HTTPS via ``urllib`` so the project gains **no new Python
dependencies** — consistent with the local-first, minimal-deps philosophy. Each
HTTP/SDK call is lazy, so a provider you never use never runs.

This module owns no context assembly: callers pass a ``system`` string plus a
list of ``{role, content}`` messages (roles ``"user"`` / ``"assistant"``),
exactly like ``llm.chat``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

PROVIDER_CLAUDE = "claude"
PROVIDER_GPT = "gpt"
PROVIDER_GEMINI = "gemini"
PROVIDER_DEEPSEEK = "deepseek"

# Stable UI order; also the preference order for the default provider. Claude
# is first so the pre-07.1 Anthropic-only default is preserved.
PROVIDER_ORDER = [PROVIDER_CLAUDE, PROVIDER_GPT, PROVIDER_GEMINI, PROVIDER_DEEPSEEK]

# Providers that accept image input (for the AI visual-judgment pass over
# browser-verification screenshots). DeepSeek's chat model is text-only, so it
# is excluded. Availability is still key-presence — a provider here is only
# usable for vision when its API key is set.
VISION_PROVIDERS = {PROVIDER_CLAUDE, PROVIDER_GPT, PROVIDER_GEMINI}

_LABELS = {
    PROVIDER_CLAUDE: "Claude",
    PROVIDER_GPT: "GPT",
    PROVIDER_GEMINI: "Gemini",
    PROVIDER_DEEPSEEK: "DeepSeek",
}

_ENV_KEYS = {
    PROVIDER_CLAUDE: "ANTHROPIC_API_KEY",
    PROVIDER_GPT: "OPENAI_API_KEY",
    PROVIDER_GEMINI: "GOOGLE_API_KEY",
    PROVIDER_DEEPSEEK: "DEEPSEEK_API_KEY",
}

# Default models — each overridable via its own env var so they're trivial to
# change without touching code. Kept to widely-available, sensible defaults.
_MODEL_ENV = {
    PROVIDER_CLAUDE: "AGENT_OS_CLAUDE_MODEL",
    PROVIDER_GPT: "AGENT_OS_OPENAI_MODEL",
    PROVIDER_GEMINI: "AGENT_OS_GEMINI_MODEL",
    PROVIDER_DEEPSEEK: "AGENT_OS_DEEPSEEK_MODEL",
}

_MODEL_DEFAULTS = {
    # Current stable Sonnet. The previous pinned id (claude-sonnet-4-20250514)
    # now returns 404 not_found from the API, which broke every Coding Agent /
    # orchestrator call on a fresh install. Override per-deployment with
    # AGENT_OS_CLAUDE_MODEL.
    PROVIDER_CLAUDE: "claude-sonnet-4-5",
    PROVIDER_GPT: "gpt-4o",
    PROVIDER_GEMINI: "gemini-1.5-flash",
    PROVIDER_DEEPSEEK: "deepseek-chat",
}

_HTTP_TIMEOUT = 120


class ProviderError(RuntimeError):
    """Raised when a provider id is unknown / unavailable or a call fails."""


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def is_known(provider_id: str) -> bool:
    return provider_id in _LABELS


def label(provider_id: str) -> str:
    return _LABELS.get(provider_id, provider_id)


def _api_key(provider_id: str) -> str | None:
    env = _ENV_KEYS.get(provider_id)
    return os.environ.get(env) if env else None


def is_available(provider_id: str) -> bool:
    """A provider is available iff its API key env var is set."""
    return bool(_api_key(provider_id))


def default_model(provider_id: str) -> str:
    return os.environ.get(_MODEL_ENV[provider_id], _MODEL_DEFAULTS[provider_id])


def default_provider() -> str:
    """First available provider in preference order (Claude preferred).

    Falls back to Claude when nothing is configured so callers surface a clean,
    consistent "missing key" error rather than something opaque.
    """
    for pid in PROVIDER_ORDER:
        if is_available(pid):
            return pid
    return PROVIDER_CLAUDE


def is_vision_capable(provider_id: str) -> bool:
    """True iff ``provider_id`` accepts image input (text+image messages)."""
    return provider_id in VISION_PROVIDERS


def vision_available() -> bool:
    """True iff at least one vision-capable provider has its API key set."""
    return any(is_available(pid) for pid in PROVIDER_ORDER if is_vision_capable(pid))


def default_vision_provider() -> str | None:
    """First available *vision-capable* provider in preference order.

    Returns ``None`` when no vision-capable provider has a key configured, so
    callers can skip the visual-judgment pass gracefully (rather than surfacing
    an opaque error). Claude is preferred, mirroring :func:`default_provider`.
    """
    for pid in PROVIDER_ORDER:
        if is_vision_capable(pid) and is_available(pid):
            return pid
    return None


def list_providers() -> list[dict]:
    """UI-facing availability snapshot for all four providers, in stable order."""
    return [
        {
            "id": pid,
            "label": _LABELS[pid],
            "available": is_available(pid),
            "default_model": default_model(pid),
        }
        for pid in PROVIDER_ORDER
    ]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def complete(
    provider_id: str,
    system: str,
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 2048,
) -> str:
    """Route a completion to ``provider_id`` and return the assistant text.

    Raises ``ProviderError`` for an unknown or unavailable provider; lower-level
    call failures (HTTP/network/parse) also surface as ``ProviderError``.
    """
    if not is_known(provider_id):
        raise ProviderError(f"unknown provider: {provider_id!r}")
    if not is_available(provider_id):
        raise ProviderError(
            f"provider {provider_id!r} is not available — set {_ENV_KEYS[provider_id]}"
        )
    chosen = model or default_model(provider_id)

    if provider_id == PROVIDER_CLAUDE:
        return _complete_anthropic(system, messages, chosen, max_tokens)
    if provider_id == PROVIDER_GPT:
        return _complete_openai_compatible(
            "https://api.openai.com/v1/chat/completions",
            _api_key(provider_id) or "", system, messages, chosen, max_tokens,
        )
    if provider_id == PROVIDER_DEEPSEEK:
        return _complete_openai_compatible(
            "https://api.deepseek.com/chat/completions",
            _api_key(provider_id) or "", system, messages, chosen, max_tokens,
        )
    if provider_id == PROVIDER_GEMINI:
        return _complete_gemini(
            _api_key(provider_id) or "", system, messages, chosen, max_tokens
        )
    raise ProviderError(f"unhandled provider: {provider_id!r}")  # pragma: no cover


def complete_vision(
    provider_id: str,
    system: str,
    prompt: str,
    images: list[tuple[str, str]],
    model: str | None = None,
    max_tokens: int = 2048,
) -> str:
    """Route a single-turn text+image completion to ``provider_id``.

    ``images`` is a list of ``(media_type, base64_data)`` tuples (e.g.
    ``("image/png", "<base64>")``). The image blocks are sent before the text
    ``prompt`` in one user turn — the shape vision models expect. Returns the
    assistant text.

    Raises ``ProviderError`` for an unknown / unavailable / non-vision provider;
    lower-level call failures (HTTP/network/parse) also surface as
    ``ProviderError``.
    """
    if not is_known(provider_id):
        raise ProviderError(f"unknown provider: {provider_id!r}")
    if not is_vision_capable(provider_id):
        raise ProviderError(f"provider {provider_id!r} does not support image input")
    if not is_available(provider_id):
        raise ProviderError(
            f"provider {provider_id!r} is not available — set {_ENV_KEYS[provider_id]}"
        )
    chosen = model or default_model(provider_id)

    if provider_id == PROVIDER_CLAUDE:
        return _complete_anthropic_vision(system, prompt, images, chosen, max_tokens)
    if provider_id == PROVIDER_GPT:
        return _complete_openai_vision(
            "https://api.openai.com/v1/chat/completions",
            _api_key(provider_id) or "", system, prompt, images, chosen, max_tokens,
        )
    if provider_id == PROVIDER_GEMINI:
        return _complete_gemini_vision(
            _api_key(provider_id) or "", system, prompt, images, chosen, max_tokens
        )
    raise ProviderError(f"unhandled vision provider: {provider_id!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

_anthropic_client = None


def _complete_anthropic(system, messages, model, max_tokens) -> str:
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic

        _anthropic_client = Anthropic(api_key=_api_key(PROVIDER_CLAUDE))
    response = _anthropic_client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(parts)


def _complete_anthropic_vision(system, prompt, images, model, max_tokens) -> str:
    """Anthropic SDK vision call — image content blocks + a text block."""
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic

        _anthropic_client = Anthropic(api_key=_api_key(PROVIDER_CLAUDE))
    content: list[dict] = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
        for media_type, data in images
    ]
    content.append({"type": "text", "text": prompt})
    response = _anthropic_client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(parts)


def _complete_openai_vision(
    url, api_key, system, prompt, images, model, max_tokens
) -> str:
    """OpenAI Chat Completions vision shape — text + ``image_url`` data URLs."""
    content: list[dict] = [{"type": "text", "text": prompt}]
    for media_type, data in images:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{data}"},
            }
        )
    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": content})
    payload = {"model": model, "messages": msgs, "max_tokens": max_tokens}
    data = _http_post_json(url, {"Authorization": f"Bearer {api_key}"}, payload)
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise ProviderError(f"unexpected response shape from {url}: {str(data)[:200]}")


def _complete_gemini_vision(api_key, system, prompt, images, model, max_tokens) -> str:
    """Google Gemini ``generateContent`` vision shape — inline_data + text."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    parts: list[dict] = [
        {"inline_data": {"mime_type": media_type, "data": data}}
        for media_type, data in images
    ]
    parts.append({"text": prompt})
    payload: dict = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if system:
        payload["system_instruction"] = {"parts": [{"text": system}]}
    data = _http_post_json(url, {}, payload)
    try:
        out_parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in out_parts)
    except (KeyError, IndexError, TypeError):
        raise ProviderError(f"unexpected Gemini response: {str(data)[:200]}")


def _http_post_json(url: str, headers: dict, payload: dict) -> dict:
    """POST ``payload`` as JSON and return the parsed JSON response.

    Network/HTTP/parse failures raise ``ProviderError`` with a bounded preview
    of the upstream body so the chat endpoint can surface a clean message.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise ProviderError(f"HTTP {exc.code} from {url}: {detail}")
    except urllib.error.URLError as exc:
        raise ProviderError(f"network error calling {url}: {exc.reason}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise ProviderError(f"non-JSON response from {url}: {body[:200]}")


def _complete_openai_compatible(
    url, api_key, system, messages, model, max_tokens
) -> str:
    """OpenAI Chat Completions shape — used by both OpenAI and DeepSeek."""
    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})
    for m in messages:
        msgs.append({"role": m["role"], "content": m["content"]})
    payload = {"model": model, "messages": msgs, "max_tokens": max_tokens}
    data = _http_post_json(url, {"Authorization": f"Bearer {api_key}"}, payload)
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise ProviderError(f"unexpected response shape from {url}: {str(data)[:200]}")


def _complete_gemini(api_key, system, messages, model, max_tokens) -> str:
    """Google Gemini ``generateContent`` REST shape."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    contents: list[dict] = []
    for m in messages:
        # Gemini uses "model" for the assistant role; everything else is "user".
        role = "model" if m["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    payload: dict = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if system:
        payload["system_instruction"] = {"parts": [{"text": system}]}
    data = _http_post_json(url, {}, payload)
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        raise ProviderError(f"unexpected Gemini response: {str(data)[:200]}")
