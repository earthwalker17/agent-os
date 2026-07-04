"""Pluggable model-provider layer (Task 07.1 → Provider Registry 2.0).

A small **capability-aware** registry mapping a provider id
(``claude`` / ``gpt`` / ``gemini`` / ``deepseek`` / ``kimi`` / ``glm``) to:

- a human label,
- the env var holding its API key (with optional accepted aliases),
- a **list of selectable models**, each tagged with capability metadata
  (today: ``vision`` — does it accept image input),
- a default model (the first in its list; overridable via env so it's easy to
  bump later),
- a ``complete()`` text dispatcher and a ``complete_vision()`` text+image
  dispatcher.

**Availability is purely key-presence:** a provider is available iff its API
key env var (or an accepted alias) is set. The UI shows all six providers;
unavailable ones render disabled.

Only Anthropic uses an SDK (already a dependency). GPT, DeepSeek, Kimi, and GLM
are OpenAI-compatible HTTPS endpoints; Gemini uses ``generateContent``. All are
called over plain ``urllib`` so the project gains **no new Python
dependencies** — consistent with the local-first, minimal-deps philosophy.
Each HTTP/SDK call is lazy, so a provider you never use never runs.

**Capability gating.** ``vision`` is a per-*model* flag, not a per-provider one:
e.g. DeepSeek's chat models are text-only, GLM's ``glm-5.2`` is text-only while
``glm-5v-turbo`` accepts images, and Kimi's ``kimi-k2.7-code`` is text-only
while ``kimi-k2.6`` accepts images. Image-related features (chat image upload,
AI visual judgment over browser screenshots) gate on the *selected* model's
``vision`` flag via :func:`is_vision_capable` / :func:`model_is_vision`.

This module owns no context assembly: callers pass a ``system`` string plus a
list of ``{role, content}`` messages (roles ``"user"`` / ``"assistant"``),
exactly like ``llm.chat``.

Model ids were verified against each provider's official API docs / live model
list as of 2026-06; they are env-overridable so a deployment can pin or bump a
model without a code change.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

PROVIDER_CLAUDE = "claude"
PROVIDER_GPT = "gpt"
PROVIDER_GEMINI = "gemini"
PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_KIMI = "kimi"
PROVIDER_GLM = "glm"

# Stable UI order; also the preference order for the default provider. Claude
# is first so the pre-07.1 Anthropic-only default is preserved.
PROVIDER_ORDER = [
    PROVIDER_CLAUDE,
    PROVIDER_GPT,
    PROVIDER_GEMINI,
    PROVIDER_DEEPSEEK,
    PROVIDER_KIMI,
    PROVIDER_GLM,
]

_LABELS = {
    PROVIDER_CLAUDE: "Claude",
    PROVIDER_GPT: "GPT",
    PROVIDER_GEMINI: "Gemini",
    PROVIDER_DEEPSEEK: "DeepSeek",
    PROVIDER_KIMI: "Kimi",
    PROVIDER_GLM: "GLM",
}

# Primary API-key env var per provider (shown in "missing key" errors).
_ENV_KEYS = {
    PROVIDER_CLAUDE: "ANTHROPIC_API_KEY",
    PROVIDER_GPT: "OPENAI_API_KEY",
    PROVIDER_GEMINI: "GOOGLE_API_KEY",
    PROVIDER_DEEPSEEK: "DEEPSEEK_API_KEY",
    PROVIDER_KIMI: "MOONSHOT_API_KEY",
    PROVIDER_GLM: "ZHIPUAI_API_KEY",
}

# Additional accepted env-var names per provider, checked after the primary.
# Kimi/Moonshot and Zhipu/Z.ai are known by more than one convention; accept
# the common aliases so an existing key works without renaming.
_ENV_KEY_ALIASES = {
    PROVIDER_GEMINI: ("GEMINI_API_KEY",),
    PROVIDER_KIMI: ("KIMI_API_KEY",),
    PROVIDER_GLM: ("ZAI_API_KEY",),
}

# Default-model override env var per provider.
_MODEL_ENV = {
    PROVIDER_CLAUDE: "AGENT_OS_CLAUDE_MODEL",
    PROVIDER_GPT: "AGENT_OS_OPENAI_MODEL",
    PROVIDER_GEMINI: "AGENT_OS_GEMINI_MODEL",
    PROVIDER_DEEPSEEK: "AGENT_OS_DEEPSEEK_MODEL",
    PROVIDER_KIMI: "AGENT_OS_KIMI_MODEL",
    PROVIDER_GLM: "AGENT_OS_GLM_MODEL",
}

# Per-provider model registry. Each entry is ``(model_id, label, vision)``.
# The FIRST entry is the provider's default model (also env-overridable). Model
# ids are the exact strings the provider's API expects. ``vision`` marks whether
# the model accepts image input (gates chat image upload + AI visual judgment).
_MODELS = {
    PROVIDER_CLAUDE: [
        ("claude-opus-4-8", "Claude Opus 4.8", True),
        ("claude-sonnet-4-6", "Claude Sonnet 4.6", True),
        ("claude-haiku-4-5", "Claude Haiku 4.5", True),
        ("claude-fable-5", "Claude Fable 5", True),
        ("claude-opus-4-7", "Claude Opus 4.7", True),
    ],
    PROVIDER_GPT: [
        ("gpt-5.5", "GPT-5.5", True),
        ("gpt-5.4", "GPT-5.4", True),
        ("gpt-5.4-mini", "GPT-5.4 mini", True),
        ("gpt-5.2", "GPT-5.2", True),
        ("gpt-4.1", "GPT-4.1", True),
    ],
    PROVIDER_GEMINI: [
        ("gemini-3.5-flash", "Gemini 3.5 Flash", True),
        ("gemini-3.1-pro-preview", "Gemini 3.1 Pro (Preview)", True),
        ("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite", True),
        ("gemini-2.5-flash", "Gemini 2.5 Flash", True),
    ],
    PROVIDER_DEEPSEEK: [
        # DeepSeek's API is text-only as of 2026-06 (no image content type).
        ("deepseek-v4-flash", "DeepSeek V4 Flash", False),
        ("deepseek-v4-pro", "DeepSeek V4 Pro", False),
        ("deepseek-chat", "DeepSeek Chat", False),
    ],
    PROVIDER_KIMI: [
        ("kimi-k2.6", "Kimi K2.6", True),
        ("kimi-k2.7-code", "Kimi K2.7 Code", False),
        ("kimi-k2.5", "Kimi K2.5", True),
        ("moonshot-v1-128k-vision-preview", "Moonshot v1 128K (Vision)", True),
        ("moonshot-v1-128k", "Moonshot v1 128K", False),
    ],
    PROVIDER_GLM: [
        ("glm-5.2", "GLM-5.2", False),
        ("glm-5v-turbo", "GLM-5V-Turbo", True),
        ("glm-4.6", "GLM-4.6", False),
        ("glm-4.5v", "GLM-4.5V", True),
        ("glm-4.5-air", "GLM-4.5-Air", False),
    ],
}

# Default model per provider = first registry entry, overridable via env.
_MODEL_DEFAULTS = {pid: _MODELS[pid][0][0] for pid in PROVIDER_ORDER}

# Chat-completions endpoint for the OpenAI-compatible HTTPS providers. The base
# URL is env-overridable (Kimi/GLM publish region-specific hosts — e.g.
# api.moonshot.cn / open.bigmodel.cn — so a China-region key can repoint
# without code changes).
_OPENAI_COMPAT_URLS = {
    PROVIDER_GPT: "https://api.openai.com/v1/chat/completions",
    PROVIDER_DEEPSEEK: "https://api.deepseek.com/chat/completions",
    PROVIDER_KIMI: "https://api.moonshot.ai/v1/chat/completions",
    PROVIDER_GLM: "https://api.z.ai/api/paas/v4/chat/completions",
}

_BASE_URL_ENV = {
    PROVIDER_GPT: "AGENT_OS_OPENAI_BASE_URL",
    PROVIDER_DEEPSEEK: "AGENT_OS_DEEPSEEK_BASE_URL",
    PROVIDER_KIMI: "AGENT_OS_KIMI_BASE_URL",
    PROVIDER_GLM: "AGENT_OS_GLM_BASE_URL",
}

# Providers whose request/response wire shape is OpenAI Chat Completions.
_OPENAI_COMPAT = {PROVIDER_GPT, PROVIDER_DEEPSEEK, PROVIDER_KIMI, PROVIDER_GLM}

_HTTP_TIMEOUT = 120


def _llm_timeout() -> float:
    """Per-request LLM timeout (seconds), env-overridable.

    A bounded timeout matters for the team runtime: parallel wave units run on a
    dedicated pool and the coordinator blocks on ``as_completed`` with no timeout,
    so one unit whose provider call hangs would stall the whole wave (and defer a
    user cancel) for as long as the client waits. A finite timeout lets a stalled
    call fail into ``llm.chat``'s transient-retry/backoff instead of wedging. Kept
    generous — a real completion at max tokens can take a while.
    """
    raw = os.environ.get("AGENT_OS_LLM_TIMEOUT")
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 300.0


class ProviderError(RuntimeError):
    """Raised when a provider/model is unknown / unavailable or a call fails."""


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def is_known(provider_id: str) -> bool:
    return provider_id in _LABELS


def label(provider_id: str) -> str:
    return _LABELS.get(provider_id, provider_id)


def _api_key(provider_id: str) -> str | None:
    primary = _ENV_KEYS.get(provider_id)
    if primary and os.environ.get(primary):
        return os.environ.get(primary)
    for alias in _ENV_KEY_ALIASES.get(provider_id, ()):  # accepted alternates
        if os.environ.get(alias):
            return os.environ.get(alias)
    return None


def is_available(provider_id: str) -> bool:
    """A provider is available iff its API key env var (or an alias) is set."""
    return bool(_api_key(provider_id))


def _models(provider_id: str) -> list[tuple[str, str, bool]]:
    return _MODELS.get(provider_id, [])


def _model_entry(provider_id: str, model: str) -> tuple[str, str, bool] | None:
    for entry in _models(provider_id):
        if entry[0] == model:
            return entry
    return None


def list_models(provider_id: str) -> list[dict]:
    """Capability-tagged model options for ``provider_id`` (stable order)."""
    return [
        {"id": mid, "label": mlabel, "vision": vision}
        for (mid, mlabel, vision) in _models(provider_id)
    ]


def is_known_model(provider_id: str, model: str) -> bool:
    """True iff ``model`` is a valid option for ``provider_id``.

    Accepts any model in the provider's registry **plus** the provider's
    resolved default model — which may be pinned off-registry via
    ``AGENT_OS_{...}_MODEL``. Without this, an env-overridden default would be
    surfaced by ``/api/providers``, echoed back by the frontend, and then
    rejected by the chat endpoint's validation — a 400 on every turn.
    """
    if _model_entry(provider_id, model) is not None:
        return True
    return provider_id in _MODELS and model == default_model(provider_id)


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


def model_is_vision(provider_id: str, model: str) -> bool:
    """True iff the specific ``(provider_id, model)`` accepts image input.

    Unknown / env-overridden models that aren't in the registry are treated as
    non-vision (conservative — image features stay off rather than failing).
    """
    entry = _model_entry(provider_id, model)
    return bool(entry and entry[2])


def is_vision_capable(provider_id: str, model: str | None = None) -> bool:
    """Whether ``provider_id`` (optionally a specific ``model``) does vision.

    With ``model`` given, returns that model's ``vision`` flag. With ``model``
    omitted, returns whether the provider has *any* vision-capable model — used
    for provider-level availability checks (``vision_available`` /
    ``default_vision_provider``).
    """
    if model is None:
        return any(vision for (_id, _lbl, vision) in _models(provider_id))
    return model_is_vision(provider_id, model)


def default_vision_model(provider_id: str) -> str | None:
    """A vision-capable model for ``provider_id`` (its default if vision, else
    the first vision-capable option), or ``None`` if the provider has none."""
    dm = default_model(provider_id)
    if model_is_vision(provider_id, dm):
        return dm
    for (mid, _lbl, vision) in _models(provider_id):
        if vision:
            return mid
    return None


def vision_available() -> bool:
    """True iff at least one provider has a vision-capable model + a key set."""
    return any(
        is_available(pid) for pid in PROVIDER_ORDER if is_vision_capable(pid)
    )


def default_vision_provider() -> str | None:
    """First available provider (preference order) that has a vision model.

    Returns ``None`` when no vision-capable provider has a key configured, so
    callers can skip the visual-judgment pass gracefully (rather than surfacing
    an opaque error). Claude is preferred, mirroring :func:`default_provider`.
    """
    for pid in PROVIDER_ORDER:
        if is_vision_capable(pid) and is_available(pid):
            return pid
    return None


def list_providers() -> list[dict]:
    """UI-facing availability snapshot for all providers, in stable order.

    Each entry carries the provider's availability, default model, and its
    capability-tagged ``models`` so the frontend can drive the per-provider
    model picker and gate image-related controls by the selected model.
    """
    return [
        {
            "id": pid,
            "label": _LABELS[pid],
            "available": is_available(pid),
            "default_model": default_model(pid),
            "models": list_models(pid),
        }
        for pid in PROVIDER_ORDER
    ]


def _chat_url(provider_id: str) -> str:
    env = _BASE_URL_ENV.get(provider_id)
    override = (os.environ.get(env, "").strip() if env else "")
    return override or _OPENAI_COMPAT_URLS[provider_id]


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
    if provider_id in _OPENAI_COMPAT:
        return _complete_openai_compatible(
            _chat_url(provider_id),
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
    """Route a single-turn text+image completion to ``provider_id`` / ``model``.

    ``images`` is a list of ``(media_type, base64_data)`` tuples (e.g.
    ``("image/png", "<base64>")``). The image blocks are sent before the text
    ``prompt`` in one user turn — the shape vision models expect. Returns the
    assistant text.

    Raises ``ProviderError`` for an unknown / unavailable provider or a
    non-vision *model* (the capability is checked against the resolved model,
    not just the provider); lower-level call failures also surface as
    ``ProviderError``.
    """
    if not is_known(provider_id):
        raise ProviderError(f"unknown provider: {provider_id!r}")
    chosen = model or default_model(provider_id)
    if not is_vision_capable(provider_id, chosen):
        raise ProviderError(
            f"model {chosen!r} of provider {provider_id!r} does not support image input"
        )
    if not is_available(provider_id):
        raise ProviderError(
            f"provider {provider_id!r} is not available — set {_ENV_KEYS[provider_id]}"
        )

    if provider_id == PROVIDER_CLAUDE:
        return _complete_anthropic_vision(system, prompt, images, chosen, max_tokens)
    if provider_id in _OPENAI_COMPAT:  # gpt / kimi / glm (deepseek has no vision model)
        return _complete_openai_vision(
            _chat_url(provider_id),
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

        _anthropic_client = Anthropic(api_key=_api_key(PROVIDER_CLAUDE), timeout=_llm_timeout())
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

        _anthropic_client = Anthropic(api_key=_api_key(PROVIDER_CLAUDE), timeout=_llm_timeout())
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
    """OpenAI Chat Completions vision shape — text + ``image_url`` data URLs.

    Shared by GPT, Kimi (Moonshot), and GLM (Z.ai) — all OpenAI-compatible.
    """
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
    # Key in the header, not the query string (see _complete_gemini) — keeps the
    # live key out of URL-bearing error messages / logs.
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
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
    data = _http_post_json(url, {"x-goog-api-key": api_key}, payload)
    try:
        out_parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in out_parts)
    except (KeyError, IndexError, TypeError):
        raise ProviderError(f"unexpected Gemini response: {str(data)[:200]}")


def _safe_url(url: str) -> str:
    """Strip a ``key=`` / ``api_key=`` query secret from a URL before it is
    echoed into an error message (defense in depth — providers now send keys as
    headers, but a stray query secret must never reach a log/traceback)."""
    return re.sub(r"(?i)([?&](?:key|api_key)=)[^&\s]+", r"\1[REDACTED]", url)


def _http_post_json(url: str, headers: dict, payload: dict) -> dict:
    """POST ``payload`` as JSON and return the parsed JSON response.

    Network/HTTP/parse failures raise ``ProviderError`` with a bounded preview
    of the upstream body so the chat endpoint can surface a clean message. The
    URL is redacted (``_safe_url``) before it enters any error message so an API
    key can never leak through a query string.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    safe = _safe_url(url)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise ProviderError(f"HTTP {exc.code} from {safe}: {detail}")
    except urllib.error.URLError as exc:
        raise ProviderError(f"network error calling {safe}: {exc.reason}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise ProviderError(f"non-JSON response from {safe}: {body[:200]}")


def _complete_openai_compatible(
    url, api_key, system, messages, model, max_tokens
) -> str:
    """OpenAI Chat Completions shape — used by GPT, DeepSeek, Kimi, and GLM."""
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
    # The API key rides the ``x-goog-api-key`` HEADER, never the query string —
    # ``_http_post_json`` echoes the URL into every error message (and thus into
    # logs / tracebacks), so a ``?key=`` URL would leak the live key on any
    # transient Gemini failure. Headers are never echoed.
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
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
    data = _http_post_json(url, {"x-goog-api-key": api_key}, payload)
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        raise ProviderError(f"unexpected Gemini response: {str(data)[:200]}")
