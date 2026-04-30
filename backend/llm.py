"""
Thin LLM wrapper for Agent OS.

Handles Anthropic API calls with a clean interface.
All context assembly happens in orchestrator.py — this module
only knows how to send messages and return text.
"""

import os
from anthropic import Anthropic

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. "
                "Add it to backend/.env or export it in your shell."
            )
        _client = Anthropic(api_key=api_key)
    return _client


def chat(
    system: str,
    messages: list[dict],
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 2048,
) -> str:
    """Send a chat request and return the assistant's text response."""
    client = _get_client()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    # Extract text from content blocks
    parts = []
    for block in response.content:
        if block.type == "text":
            parts.append(block.text)
    return "\n".join(parts)
