"""Confirmable pending execution plans (Task 05.9.5).

When the LLM delegation judge returns ``dispatch_suggested``, the chat layer
does NOT immediately dispatch a Coding Agent run and does NOT dump a raw
``@code`` task card into the chat. Instead it:

  1. Persists a ``pending_execution`` row in SQLite with the rendered
     display_plan (what the user reads) and the full task_card (what the
     Coding Agent will read on confirmation).
  2. Posts a natural project-manager-style assistant message linking the
     pending plan via message metadata.
  3. Lets the user confirm (→ dispatch the run) or revise (→ overwrite the
     plan + task_card in place).

Direct ``@code …`` keeps its 05.4 path — it bypasses this module entirely.

This module exposes:

  - ``PendingExecutionView`` — the shape returned to the chat API.
  - ``serialize_pending`` — DB row → API shape.
  - ``revise_pending_plan`` — small LLM call that rewrites display_plan +
    task_card given user revision instructions, with a heuristic fallback.
  - ``MAX_TITLE_LEN`` / display defaults — shared with main.py.

It deliberately does not import from ``main.py``; the chat endpoint owns
the orchestration. ``database.py`` owns persistence; this module owns the
LLM-touching revision logic and the API-shape adapter.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from llm import chat as llm_chat


log = logging.getLogger(__name__)


# Lifecycle. Kept here so consumers don't reach into raw status strings.
STATUS_PENDING = "pending"
STATUS_DISPATCHED = "dispatched"
STATUS_CANCELLED = "cancelled"

MAX_TITLE_LEN = 80


# ---------- API shape ----------


@dataclass
class PendingExecutionView:
    """The pending-execution shape returned in chat API responses.

    Mirrors the SQLite row but flattens / renames a couple of fields for the
    frontend. The frontend keys off ``status`` to decide whether to render
    the OK/Revise buttons (``pending``) or a "run started" indicator
    (``dispatched``).
    """

    pending_execution_id: str
    project_id: str
    conversation_id: str
    title: str
    display_plan: str
    task_card: str
    status: str
    run_id: Optional[str]
    revision_count: int
    created_at: str
    updated_at: str
    # Phase 11 — set when this plan is a recovery proposal for a specific
    # failed run; the confirm endpoint threads it into dispatch lineage.
    recovery_of: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "pending_execution_id": self.pending_execution_id,
            "project_id": self.project_id,
            "conversation_id": self.conversation_id,
            "title": self.title,
            "display_plan": self.display_plan,
            "task_card": self.task_card,
            "status": self.status,
            "run_id": self.run_id,
            "revision_count": self.revision_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "recovery_of": self.recovery_of,
        }


def serialize_pending(row: dict) -> PendingExecutionView:
    """Adapt a raw ``pending_executions`` row dict to the API view."""
    return PendingExecutionView(
        pending_execution_id=row["id"],
        project_id=row["project_id"],
        conversation_id=row["conversation_id"],
        title=row["title"],
        display_plan=row["display_plan"],
        task_card=row["task_card"],
        status=row["status"],
        run_id=row.get("run_id"),
        revision_count=row.get("revision_count", 0) or 0,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        recovery_of=row.get("recovery_of"),
    )


# ---------- chat-message rendering ----------


PENDING_FOOTER = (
    "\n\n_I won't start this until you confirm._ "
    "Click **OK, run this** to dispatch, or **Revise plan** to adjust the "
    "approach first."
)

REVISED_PREFIX = (
    "Updated the plan based on your feedback."
)


def render_pending_chat_body(plan: PendingExecutionView) -> str:
    """Format the initial assistant message body for a brand-new pending plan.

    The display_plan is already project-manager-tone markdown; we just append
    a short footer telling the user how to confirm. Keeping the prose
    minimal here on purpose — heavy templating would defeat the goal of
    sounding like a normal PM reply rather than a sterile task card dump.
    """
    body = plan.display_plan.strip()
    if not body:
        body = (
            f"Proposed work: **{plan.title}**.\n\n"
            "I haven't started this yet — confirm before I dispatch."
        )
    return body + PENDING_FOOTER


def render_revised_chat_body(
    plan: PendingExecutionView,
    change_summary: str,
) -> str:
    """Format the assistant message body after a successful revision."""
    summary = (change_summary or "").strip()
    parts = [REVISED_PREFIX]
    if summary:
        parts.append(f"**What changed:** {summary}")
    parts.append("**Revised plan:**\n\n" + plan.display_plan.strip())
    return "\n\n".join(parts) + PENDING_FOOTER


# ---------- revision LLM call ----------


_REVISE_SYSTEM_PROMPT = """\
You are the planning subsystem of Agent OS. The user is revising a pending
Coding Agent execution plan BEFORE it runs.

You receive:
  - the current plan (what the user previously saw)
  - the current task card (what the Coding Agent will read on confirmation)
  - the user's revision instructions

Your job: produce a *new* plan + task card that incorporates the revision,
plus a short summary of what changed. Keep the project-manager tone for the
plan; keep the task card a self-contained imperative for the Coding Agent.

Hard rules:
- Never invent files or commands that the user did not ask for and were not
  in the previous plan.
- If the revision says "scrap that, do X instead", REPLACE the plan — don't
  just append.
- If the revision is vague or unactionable, keep the existing plan but note
  the ambiguity in change_summary so the user can clarify.
- Keep the same project context. Do not change the project's tech stack
  based on a passing comment.

Return ONLY a single JSON object. No markdown fences, no commentary.

Schema:
{
  "title": "short noun-phrase run title, max ~60 chars",
  "display_plan": "project-manager-tone markdown plan, 4-12 lines",
  "task_card": "self-contained imperative task card for the Coding Agent",
  "change_summary": "one short sentence describing what changed vs the prior plan"
}
"""


@dataclass
class PlanRevision:
    """In-memory result of a successful or fallback revision pass."""

    title: str
    display_plan: str
    task_card: str
    change_summary: str
    source: str  # "llm" or "heuristic_fallback"


def _heuristic_revision(
    current: PendingExecutionView,
    revision_instructions: str,
) -> PlanRevision:
    """Safe fallback when the revision LLM call fails.

    We DON'T silently keep the old plan — that would hide the failure from
    the user. Instead we append the revision verbatim to both the plan and
    the task card and tell the user the model judge couldn't refine it.
    """
    instructions = revision_instructions.strip()
    new_card = (
        current.task_card.rstrip()
        + "\n\nRevision: "
        + instructions
    )
    new_plan = (
        current.display_plan.rstrip()
        + "\n\n**Additional instruction:** "
        + instructions
    )
    return PlanRevision(
        title=current.title,
        display_plan=new_plan,
        task_card=new_card,
        change_summary=(
            "Revision LLM unavailable — appended your instruction to the "
            "existing plan and task card verbatim."
        ),
        source="heuristic_fallback",
    )


def _strip_code_fence(text: str) -> str:
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


def _parse_revision(raw: str) -> Optional[dict]:
    text = _strip_code_fence(raw)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Revision judge returned invalid JSON: %s", text[:200])
        return None
    if not isinstance(parsed, dict):
        return None
    required = {"title", "display_plan", "task_card", "change_summary"}
    if not required.issubset(parsed.keys()):
        return None
    return parsed


def revise_pending_plan(
    current: PendingExecutionView,
    revision_instructions: str,
    *,
    llm_caller=None,
) -> PlanRevision:
    """Run the revision LLM call. Never raises; falls back to heuristic.

    ``llm_caller`` is an optional injection seam for tests — defaults to the
    live Anthropic client via ``llm.chat``.
    """
    instructions = (revision_instructions or "").strip()
    if not instructions:
        # Nothing to revise. Treat as a no-op fallback that simply tells the
        # user we didn't see any instructions, but keeps the plan stable.
        return PlanRevision(
            title=current.title,
            display_plan=current.display_plan,
            task_card=current.task_card,
            change_summary="No revision instructions provided — plan unchanged.",
            source="heuristic_fallback",
        )

    caller = llm_caller or llm_chat
    user_prompt = (
        "## Current plan (what the user saw)\n\n"
        f"{current.display_plan}\n\n"
        "## Current task card (what the Coding Agent will read)\n\n"
        f"{current.task_card}\n\n"
        "## Current title\n\n"
        f"{current.title}\n\n"
        "## User's revision instructions\n\n"
        f"{instructions}\n\n"
        "Return ONLY the JSON object described in the system prompt."
    )

    try:
        raw = caller(
            system=_REVISE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=768,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Revision LLM call failed: %s", exc)
        return _heuristic_revision(current, instructions)

    parsed = _parse_revision(raw)
    if parsed is None:
        return _heuristic_revision(current, instructions)

    title = str(parsed["title"]).strip() or current.title
    if len(title) > MAX_TITLE_LEN:
        title = title[: MAX_TITLE_LEN - 3].rstrip() + "..."
    display_plan = str(parsed["display_plan"]).strip() or current.display_plan
    task_card = str(parsed["task_card"]).strip() or current.task_card
    change_summary = str(parsed["change_summary"]).strip() or "Plan revised."

    return PlanRevision(
        title=title,
        display_plan=display_plan,
        task_card=task_card,
        change_summary=change_summary,
        source="llm",
    )


# ---------- helpers used by the chat endpoint ----------


def derive_title_from_card(task_card: str, fallback: str = "Coding task") -> str:
    """Best-effort title derivation when the judge didn't provide one.

    Used in the fallback heuristic path and as a defensive belt for the LLM
    path. Mirrors chat_delegation._derive_title's behaviour so the two paths
    produce equivalent run titles.
    """
    first_line = (task_card or "").split("\n", 1)[0].strip()
    if not first_line:
        return fallback
    if len(first_line) > MAX_TITLE_LEN:
        return first_line[: MAX_TITLE_LEN - 3].rstrip() + "..."
    return first_line
