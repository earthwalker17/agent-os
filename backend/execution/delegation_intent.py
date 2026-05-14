"""Conservative implicit-delegation detection for project chats.

This module decides whether a non-`@code` user message in a project workspace
*reads like* a coding-task request — "please implement X", "fix this bug",
"add a button". When it does, the chat layer can short-circuit the normal
orchestrator response and instead nudge the user toward an explicit `@code`
dispatch with a proposed task card.

Detection rules (intentionally narrow, biased toward false negatives):

1. Lowercase + trim the message.
2. Look for an **action verb** that appears at the start of the message OR
   immediately after a punctuation boundary OR after a small set of leading
   phrases ("please", "can you", "could you", "let's", "and", "then", "now",
   "go ahead and", "i'd like you to", "i want you to", "i need you to").
3. **Strong verbs** trigger on their own: `implement`, `refactor`, `debug`,
   `patch`, `port`, `migrate`, `rewrite`, `fix`, `wire up`, `hook up`.
4. **Conditional verbs** (more ambiguous in conversation) also require a
   **code-context token** somewhere in the message: `add`, `modify`, `change`,
   `write`, `build`, `create`, `remove`, `delete`, `rename`, `update`, `edit`,
   `set up`.
5. Code-context tokens include things like `test`, `tests`, `function`,
   `endpoint`, `component`, `file`, `frontend`, `backend`, `ui`, `api`,
   `button`, `page`, `module`, `the code`, `this bug`, `the bug`, `a bug`,
   `the error`, `this error`, `the script`, `python`, `react`, `css`, `html`.
6. Discussion-style framing ("explain", "describe", "summarize", "what",
   "why", "how do/does", "should we") never triggers on its own — the rule
   set deliberately excludes them from the verb list. A message like
   "explain this error and fix it" still triggers because of the "and fix"
   clause boundary.

We do NOT run an LLM judge here. A lightweight rule pass keeps the implicit
path cheap and predictable; the user retains full control via `@code`.

This module produces a *suggestion message* — it never dispatches the run.
The chat endpoint persists the suggestion as the assistant reply and skips
memory writeback (the user message hasn't produced durable project knowledge
yet — they still need to confirm with `@code`).
"""

from __future__ import annotations

import re


# ---------- verb / context inventories ----------

_STRONG_VERBS = {
    "implement", "refactor", "debug", "patch", "port", "migrate",
    "rewrite", "fix",
}

# Strong multi-word verbs (matched as phrases, not single tokens).
_STRONG_PHRASES = {
    "wire up", "hook up",
}

_CONDITIONAL_VERBS = {
    "add", "modify", "change", "write", "build", "create",
    "remove", "delete", "rename", "update", "edit",
}

_CONDITIONAL_PHRASES = {
    "set up",
}

# Words that, when present anywhere in the message, promote a conditional
# verb into a trigger. Matched with word boundaries so short tokens like
# "ui" don't fire inside words like "build" / "guide".
_CONTEXT_WORDS = (
    "test", "tests", "function", "functions", "endpoint", "endpoints",
    "component", "components", "module", "modules",
    "frontend", "backend", "ui", "api", "button", "buttons",
    "page", "pages", "feature", "features", "script", "scripts",
    "class", "classes", "method", "methods",
    "python", "react", "typescript", "javascript", "css", "html",
    "readme",
)

# Multi-word context phrases — already long enough that substring matching
# is collision-free.
_CONTEXT_PHRASES = (
    "the file", "this file", "the files", "the code", "the script",
    "the bug", "this bug", "a bug", "the error", "this error", "an error",
    "the api", "an api",
)

# File extensions — start with "." so substring matching is safe.
_CONTEXT_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".md")

# Permitted prefix phrases between the verb and the start (or a previous
# punctuation/conjunction boundary). Order matters only for longest-match
# safety in the regex below.
_LEAD_PREFIXES = [
    "please",
    "can you",
    "could you",
    "would you",
    "will you",
    "let's", "lets",
    "go ahead and",
    "i'd like you to", "id like you to",
    "i would like you to",
    "i want you to",
    "i need you to",
    "now",
    "then",
    "and then",
    "and",
]


# ---------- compiled patterns ----------

def _build_trigger_re() -> re.Pattern[str]:
    verbs = sorted(_STRONG_VERBS | _CONDITIONAL_VERBS, key=len, reverse=True)
    phrases = sorted(_STRONG_PHRASES | _CONDITIONAL_PHRASES, key=len, reverse=True)
    # Phrases first (longer alternations bind first in a regex alternation).
    verb_alt = "|".join(
        re.escape(p) for p in phrases
    ) + "|" + "|".join(re.escape(v) for v in verbs)
    prefix_alt = "|".join(re.escape(p) for p in _LEAD_PREFIXES)

    # The verb may follow:
    #   - the start of the message
    #   - a punctuation boundary (. ? ! ; ,) followed by optional whitespace
    #   - a permitted prefix word/phrase followed by whitespace
    # We capture the matched verb for downstream classification.
    pattern = (
        r"(?:(?<=^)|(?<=[\.\?!;,]\s)|(?<=[\.\?!;,])"
        r"|(?:\b(?:" + prefix_alt + r")\s+))"
        r"(" + verb_alt + r")\b"
    )
    return re.compile(pattern, re.IGNORECASE)


_TRIGGER_RE = _build_trigger_re()

_CONTEXT_WORDS_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _CONTEXT_WORDS) + r")\b",
    re.IGNORECASE,
)


def _has_code_context(lower: str) -> bool:
    if _CONTEXT_WORDS_RE.search(lower):
        return True
    if any(phrase in lower for phrase in _CONTEXT_PHRASES):
        return True
    if any(ext in lower for ext in _CONTEXT_EXTENSIONS):
        return True
    return False


# ---------- public API ----------


def looks_like_code_request(message: str) -> bool:
    """Heuristic: does this project-chat message read like a coding request?

    Returns True when a strong verb is present at a clause boundary, or a
    conditional verb is present alongside a code-context token. Designed
    to under-trigger; ambiguous planning/discussion phrasing returns False.
    """
    if not isinstance(message, str):
        return False
    text = message.strip()
    if not text:
        return False
    lower = text.lower()

    has_context = _has_code_context(lower)

    for match in _TRIGGER_RE.finditer(lower):
        verb = match.group(1).lower()
        if verb in _STRONG_VERBS or verb in _STRONG_PHRASES:
            return True
        if verb in _CONDITIONAL_VERBS or verb in _CONDITIONAL_PHRASES:
            if has_context:
                return True
    return False


def derive_task_card(message: str) -> str:
    """Strip a leading politeness/permission prefix to produce a clean task card.

    The orchestrator-style "please " / "can you " framing isn't useful inside
    a `@code` task card — the Coding Agent reads it as an imperative regardless.
    """
    text = (message or "").strip()
    if not text:
        return ""

    stripped = re.sub(
        r"^(please\s+|can you\s+|could you\s+|would you\s+|will you\s+|"
        r"let'?s\s+|go ahead and\s+|"
        r"i'?d like you to\s+|i would like you to\s+|"
        r"i want you to\s+|i need you to\s+)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return stripped.strip()


def render_suggestion(message: str) -> str:
    """Build the assistant-facing markdown for an implicit-delegation nudge."""
    task_card = derive_task_card(message) or message.strip()
    return (
        "## Looks Like a Coding Agent Task\n\n"
        "This message reads like a code-execution request. The Agent OS "
        "orchestrator doesn't edit project code itself — that work happens "
        "through the Coding Agent, which runs in a sandboxed workspace under "
        "`execution_workspaces/{project}/repo/`.\n\n"
        "**Proposed task card:**\n\n"
        f"> {task_card}\n\n"
        "To dispatch it, send the following as your next message:\n\n"
        "```\n"
        f"@code {task_card}\n"
        "```\n\n"
        "The run will execute in the background; you can track it in the "
        "Runs panel on the right. If this isn't what you meant, reply with a "
        "different phrasing and the regular orchestrator will pick it up."
    )
