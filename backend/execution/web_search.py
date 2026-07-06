"""Web-search adapter for the research channel (Phase 10).

One engine behind a tiny seam: ``search_web()`` returns a bounded list of
``{title, url, snippet}`` dicts, never page contents (fetching is a separate,
allowlist-gated step in ``research.py``). Tavily is the v1 engine; a Brave
adapter slots in as one function when needed:

    Brave slot: GET https://api.search.brave.com/res/v1/web/search?q=<q>&count=<n>
    with header ``X-Subscription-Token: <key>``; parse
    ``web.results[].{title,url,description}`` into the same triple.

Security posture (mirrors ``github_connector``):
- plain ``urllib`` — no new dependencies;
- the API key rides ONLY in a request header, never a URL or argv;
- every error string is passed through ``credentials.redact()`` before it can
  reach a log/prompt/UI;
- ``opener`` is injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Callable, Optional

import credentials

log = logging.getLogger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"
_HTTP_TIMEOUT = 30

DEFAULT_ENGINE = "tavily"
MAX_SNIPPET_CHARS = 300


class SearchError(Exception):
    """Search failed — message is safe to surface (already redacted)."""


def search_web(
    query: str,
    *,
    project_id: Optional[str] = None,
    max_results: int = 8,
    opener: Optional[Callable] = None,
) -> list[dict]:
    """Run one bounded web search; return ``[{title, url, snippet}]``.

    Raises :class:`SearchError` on any failure (no key, HTTP error, malformed
    response) — callers turn that into an ``ok=False`` research result.
    """
    q = (query or "").strip()
    if not q:
        raise SearchError("empty search query")

    engine = (credentials.get_metadata("search", "engine", project_id) or DEFAULT_ENGINE).lower()
    api_key = credentials.get_token("search", project_id)
    if not api_key:
        raise SearchError(
            "No search API key configured — set TAVILY_API_KEY (or store an "
            "api_key under the 'search' connector) to enable web search. "
            "Fetching user-provided URLs works without a key."
        )
    if engine == "tavily":
        return _tavily_search(q, api_key, max_results, project_id=project_id, opener=opener)
    raise SearchError(f"unknown search engine {engine!r} — supported: tavily")


def _tavily_search(
    query: str,
    api_key: str,
    max_results: int,
    *,
    project_id: Optional[str] = None,
    opener: Optional[Callable] = None,
) -> list[dict]:
    payload = {
        "query": query,
        "max_results": max(1, min(int(max_results), 10)),
        "search_depth": "basic",
        "include_answer": False,
    }
    req = urllib.request.Request(
        _TAVILY_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # Key in the header ONLY — URLs get echoed into error messages.
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Agent-OS-Research",
        },
        method="POST",
    )
    _open = opener or urllib.request.urlopen
    try:
        with _open(req, timeout=_HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise SearchError(
            credentials.redact(f"search HTTP {e.code}: {detail}", project_id)
        ) from e
    except Exception as e:  # noqa: BLE001 — URLError, timeout, connection reset
        raise SearchError(
            credentials.redact(f"search network error: {e}", project_id)
        ) from e

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise SearchError("search returned a non-JSON response") from e
    raw_results = parsed.get("results")
    if not isinstance(raw_results, list):
        raise SearchError("search response missing 'results'")

    results: list[dict] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        snippet = str(item.get("content") or "").strip()
        if len(snippet) > MAX_SNIPPET_CHARS:
            snippet = snippet[:MAX_SNIPPET_CHARS].rstrip() + "…"
        results.append({
            "title": str(item.get("title") or "").strip() or url,
            "url": url,
            "snippet": snippet,
        })
    return results
