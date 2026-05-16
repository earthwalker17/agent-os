"""LLM-based semantic delegation judge for project chats.

This module replaces the rule-based `looks_like_code_request` heuristic with a
small Claude call that classifies a non-`@code` user message into one of three
buckets:

  - ``dispatch_suggested`` — the user is asking for code/file changes inside
    the project's ``repo/`` workspace. The chat endpoint should respond with a
    non-executing nudge that proposes an ``@code`` task card.
  - ``discussion`` — ordinary planning, architecture, explanation, or status
    Q&A. The orchestrator handles it normally.
  - ``memory_only`` — the user is asking for project memory (STATUS.md /
    TASK_QUEUE.md / DECISIONS.md / RESEARCH.md / PROJECT.md) to be updated,
    but is not asking for changes inside ``repo/``. The orchestrator handles
    it normally — the existing memory writeback pipeline picks it up.

Design constraints (from Task 05.9):
  - `@code` remains the only path that actually dispatches a CodingAgentRunner
    run. This judge never dispatches — it can only nudge.
  - The judge MUST be robust to non-English imperatives ("继续做这个修复") and
    to anaphoric follow-ups ("do that", "apply the plan") that depend on the
    previous assistant turn.
  - Discussion-style phrasing ("explain", "what's the status", "update the
    status section") MUST NOT be misread as dispatch.
  - On any LLM failure (network, JSON parse, validation), fall back safely to
    the existing conservative regex heuristic. A failed judge must not block
    chat and must never trigger execution.

Cost / latency: this is a third LLM call per non-`@code` turn (chat response
+ memory judge + delegation judge). The prompt and ``max_tokens`` are kept
deliberately small.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from llm import chat as llm_chat

from .delegation_intent import (
    derive_task_card,
    looks_like_code_request,
    render_suggestion as render_heuristic_suggestion,
)


log = logging.getLogger(__name__)


# Path to project memory, mirrored from orchestrator.py to avoid a circular
# import — the orchestrator imports execution, not the other way around.
_PROJECTS_DIR = Path(__file__).resolve().parent.parent.parent / "projects"


DECISION_DISPATCH = "dispatch_suggested"
DECISION_DISCUSSION = "discussion"
DECISION_MEMORY_ONLY = "memory_only"
VALID_DECISIONS = {DECISION_DISPATCH, DECISION_DISCUSSION, DECISION_MEMORY_ONLY}


@dataclass
class DelegationDecision:
    """Structured result from the delegation judge.

    ``proposed_task_card``, ``display_plan`` and ``title`` are only meaningful
    when ``decision == "dispatch_suggested"``; for the other two decisions
    they are the empty string.

    ``source`` is informational — it distinguishes a real LLM judgment ("llm")
    from a fallback path ("heuristic_fallback", "general_workspace", "empty_input").
    """

    decision: str
    confidence: float
    reason: str
    proposed_task_card: str
    source: str
    display_plan: str = ""
    title: str = ""

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "confidence": self.confidence,
            "reason": self.reason,
            "proposed_task_card": self.proposed_task_card,
            "display_plan": self.display_plan,
            "title": self.title,
            "source": self.source,
        }


# ---------- prompt construction ----------

_JUDGE_SYSTEM_PROMPT = """\
You are the delegation classifier for Agent OS, a local-first project cockpit.

Each non-`@code` user message in a project chat must be classified into ONE of:

- "dispatch_suggested": The user is asking for code or file changes to be made
  inside the project's bounded `repo/` workspace by the Coding Agent. This
  includes anaphoric follow-ups ("do that", "go ahead", "apply the plan",
  "implement what we just discussed", "继续做这个修复", "按刚才方案执行")
  *when* the most recent assistant turn proposed concrete code work.
- "discussion": Ordinary conversation — planning, architecture, design Q&A,
  status questions, ideation, brainstorming, explanations. The user is
  talking with the orchestrator, not requesting code execution.
- "memory_only": The user is asking for project memory files to be updated
  (STATUS.md, TASK_QUEUE.md, DECISIONS.md, RESEARCH.md, PROJECT.md) but is
  NOT asking for changes inside `repo/`.

Critical rules:
1. Only return "dispatch_suggested" when the user clearly wants code, files,
   tests, or build artifacts inside `repo/` to be MODIFIED. New-feature
   imperatives ("implement X", "fix the bug", "add a button", "refactor Y",
   "wire up the endpoint", "port this to TypeScript") qualify.
2. "Update the status section", "note this decision", "record this in
   research", "log this finding" are memory_only — NEVER dispatch.
3. "Explain", "what about", "how should we", "why does", "summarize",
   "describe", "what's the plan" are discussion — NEVER dispatch.
4. Follow-up resolution: if the previous assistant turn outlined concrete
   code work (proposed code changes, listed files to modify, sketched an
   implementation), then short follow-ups like "do it", "yes, go ahead",
   "apply that", "let's do this" classify as dispatch_suggested. If the
   previous assistant turn was just discussion, the same words are
   discussion.
5. Be conservative. When you cannot tell, prefer "discussion". The user can
   always explicitly type `@code` to force a dispatch.
6. Non-English imperatives count — judge intent, not language.

When you choose "dispatch_suggested", produce three extra fields:

1. ``title`` — a short noun-phrase label (max ~60 chars) for the run, like
   a PR title. Examples: "Add /healthcheck endpoint", "Fix login redirect
   bug", "Refactor sandbox.py path validation".

2. ``display_plan`` — what the project manager says to the user before
   asking for confirmation. Natural project-manager tone, NOT a sterile
   task-card template. Briefly restate your understanding, list the
   concrete steps the Coding Agent will take (2-5 bullet items),
   call out any files or commands. The user reads this and decides
   whether to confirm. Keep it tight — maybe 4-12 lines of markdown.

3. ``proposed_task_card`` — the imperative task card handed to the
   Coding Agent on confirmation. Self-contained (the agent will not see
   chat history). Strip politeness, resolve anaphora ("do that" → the
   concrete task from the previous assistant turn). Should be 1-6 short
   sentences or a short bulleted list.

For "discussion" and "memory_only", these three fields must be empty
strings.

Return ONLY a single JSON object. No markdown fences, no commentary.

Schema:
{
  "decision": "dispatch_suggested" | "discussion" | "memory_only",
  "confidence": 0.0-1.0,
  "reason": "one short sentence justifying the decision",
  "title": "short run title (empty unless dispatch_suggested)",
  "display_plan": "project-manager-tone markdown plan (empty unless dispatch_suggested)",
  "proposed_task_card": "imperative task card (empty unless dispatch_suggested)"
}
"""


# Window of recent conversation passed to the judge. Anaphoric follow-ups
# usually only need the immediately preceding assistant turn; we include a
# bit more to be safe but keep the prompt tight.
_MAX_HISTORY_TURNS = 6

# Cap each historical message; long planning replies in particular can be
# enormous and they aren't needed verbatim for intent classification.
_MAX_HISTORY_CHAR_LEN = 1600

# Per-memory-file cap when building the project snapshot.
_MAX_MEMORY_FILE_CHAR_LEN = 800


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _build_history_block(
    history: list[dict],
    current_message: str,
) -> str:
    """Render the trailing ``_MAX_HISTORY_TURNS`` of conversation as plain text.

    Excludes the trailing duplicate of the current user message if present
    (the caller will inject it separately in the user prompt).
    """
    if not history:
        return "(no prior conversation)"

    trimmed = history[-_MAX_HISTORY_TURNS:]
    if (
        trimmed
        and trimmed[-1].get("role") == "user"
        and trimmed[-1].get("content", "").strip() == current_message.strip()
    ):
        trimmed = trimmed[:-1]

    if not trimmed:
        return "(no prior conversation)"

    lines: list[str] = []
    for msg in trimmed:
        role = msg.get("role", "user").upper()
        content = _truncate(str(msg.get("content", "")), _MAX_HISTORY_CHAR_LEN)
        lines.append(f"[{role}] {content}")
    return "\n\n".join(lines)


def _load_memory_snapshot(project_id: str) -> str:
    """Return a small compact snapshot of project memory for the judge.

    We do not load all five files at full size — the judge mainly uses
    STATUS.md and TASK_QUEUE.md to resolve "continue" / "do that" follow-ups.
    PROJECT.md gives the project flavor in case the user message refers to
    the system being built.
    """
    project_path = _PROJECTS_DIR / project_id
    if not project_path.exists():
        return "(no project memory available)"

    parts: list[str] = []
    for filename in ("PROJECT.md", "STATUS.md", "TASK_QUEUE.md"):
        fpath = project_path / filename
        if not fpath.exists():
            continue
        try:
            content = fpath.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not content:
            continue
        parts.append(f"### {filename}\n{_truncate(content, _MAX_MEMORY_FILE_CHAR_LEN)}")

    if not parts:
        return "(project memory is empty)"
    return "\n\n".join(parts)


def _build_user_prompt(
    project_name: Optional[str],
    project_id: str,
    history: list[dict],
    current_message: str,
) -> str:
    name = project_name or project_id
    return (
        f"## Project: {name}\n\n"
        "## Compact Project Memory\n\n"
        f"{_load_memory_snapshot(project_id)}\n\n"
        "---\n\n"
        "## Recent Conversation\n\n"
        f"{_build_history_block(history, current_message)}\n\n"
        "---\n\n"
        "## Current User Message\n\n"
        f"{current_message}\n\n"
        "---\n\n"
        "Classify the current user message. Return ONLY the JSON object."
    )


# ---------- parsing ----------


def _strip_code_fence(text: str) -> str:
    """Tolerate models that wrap their JSON object in ``` fences."""
    t = text.strip()
    if "```" not in t:
        return t
    first = t.find("```")
    last = t.rfind("```")
    if first == last:
        return t
    inner = t[first:last]
    newline = inner.find("\n")
    if newline == -1:
        return ""
    return inner[newline + 1 :].strip()


def _parse_decision(raw: str) -> Optional[DelegationDecision]:
    """Parse the judge's JSON object. Returns None on any malformed input."""
    text = _strip_code_fence(raw)
    if not text:
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Delegation judge returned invalid JSON: %s", text[:200])
        return None

    if not isinstance(parsed, dict):
        log.warning("Delegation judge returned non-object: %s", type(parsed))
        return None

    decision = parsed.get("decision")
    if decision not in VALID_DECISIONS:
        log.warning("Delegation judge returned invalid decision: %r", decision)
        return None

    confidence_raw = parsed.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    reason = str(parsed.get("reason", "")).strip()
    task_card = str(parsed.get("proposed_task_card", "")).strip()
    display_plan = str(parsed.get("display_plan", "")).strip()
    title = str(parsed.get("title", "")).strip()

    # Enforce the schema invariant: dispatch-only fields are empty for
    # other decisions, regardless of what the model emitted.
    if decision != DECISION_DISPATCH:
        task_card = ""
        display_plan = ""
        title = ""

    return DelegationDecision(
        decision=decision,
        confidence=confidence,
        reason=reason,
        proposed_task_card=task_card,
        display_plan=display_plan,
        title=title,
        source="llm",
    )


# ---------- fallback ----------


def _fallback_from_heuristic(message: str) -> DelegationDecision:
    """Conservative regex-based fallback for when the LLM judge fails.

    This is intentionally simple: if the existing heuristic flags the message
    as code-shaped, we propose dispatch with a stripped task card; otherwise
    we classify it as discussion and let the orchestrator handle it. The
    heuristic does not detect memory_only intent — that's fine because the
    orchestrator's memory writeback step handles those cases anyway.

    The fallback ``display_plan`` is intentionally minimal — the LLM judge
    is the one that produces a real project-manager-tone plan. Here we just
    echo the task card so the user can still confirm/revise meaningfully.
    """
    if looks_like_code_request(message):
        task_card = derive_task_card(message) or message.strip()
        title = task_card.split("\n", 1)[0].strip()[:60] or "Coding task"
        display_plan = (
            "I read this as a request for the Coding Agent. "
            "Here's what I'd hand off:\n\n"
            f"> {task_card}\n\n"
            "Confirm to dispatch, or revise the plan first."
        )
        return DelegationDecision(
            decision=DECISION_DISPATCH,
            confidence=0.5,
            reason="LLM judge unavailable; conservative regex heuristic flagged this as code-shaped.",
            proposed_task_card=task_card,
            display_plan=display_plan,
            title=title,
            source="heuristic_fallback",
        )
    return DelegationDecision(
        decision=DECISION_DISCUSSION,
        confidence=0.5,
        reason="LLM judge unavailable; conservative regex heuristic saw no code-shaped imperative.",
        proposed_task_card="",
        display_plan="",
        title="",
        source="heuristic_fallback",
    )


# ---------- public entry point ----------


def judge_delegation(
    project_id: str,
    project_name: Optional[str],
    user_message: str,
    history: list[dict],
    *,
    is_general: bool = False,
    llm_caller=None,
) -> DelegationDecision:
    """Classify a non-`@code` project-chat message.

    ``llm_caller`` is an optional injection seam — pass a function with the
    same signature as ``llm.chat`` to bypass the real Anthropic call during
    tests. The default is the live LLM.

    Never raises. Any internal error becomes a heuristic fallback.
    """
    text = (user_message or "").strip()
    if not text:
        return DelegationDecision(
            decision=DECISION_DISCUSSION,
            confidence=1.0,
            reason="Empty message — nothing to dispatch.",
            proposed_task_card="",
            source="empty_input",
        )

    if is_general:
        # `@code` is rejected in GENERAL by chat_delegation already; the
        # implicit nudge has no business firing there either.
        return DelegationDecision(
            decision=DECISION_DISCUSSION,
            confidence=1.0,
            reason="GENERAL workspace does not have an execution workspace.",
            proposed_task_card="",
            source="general_workspace",
        )

    caller = llm_caller or llm_chat
    user_prompt = _build_user_prompt(project_name, project_id, history, text)

    try:
        raw = caller(
            system=_JUDGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=384,
        )
    except Exception as exc:  # network, auth, rate-limit, etc.
        log.warning("Delegation judge LLM call failed: %s", exc)
        return _fallback_from_heuristic(text)

    parsed = _parse_decision(raw)
    if parsed is None:
        return _fallback_from_heuristic(text)
    return parsed


# ---------- suggestion rendering ----------


def render_dispatch_suggestion(
    decision: DelegationDecision,
    original_message: str,
) -> str:
    """Render the assistant-facing markdown nudge for a dispatch_suggested decision.

    Prefers the LLM-proposed task card; falls back to the heuristic-derived
    card if the judge returned an empty one (defensive — the parser already
    accepts empty strings).
    """
    task_card = decision.proposed_task_card.strip()
    if not task_card:
        task_card = derive_task_card(original_message) or original_message.strip()

    # Reuse the existing heuristic renderer so the UX is identical to 05.8.
    # We just hand it a synthesized "message" that already contains the
    # imperative task card, so the rendered `@code` command matches.
    return render_heuristic_suggestion(task_card)
