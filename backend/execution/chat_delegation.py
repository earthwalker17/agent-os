"""Bridge from /api/chat to the Coding Agent runner.

When a project chat message starts with `@code`, the chat endpoint hands the
message off to this module. The helper:

1. Strips the `@code` prefix to form the task card.
2. Lazily ensures the project's execution workspace exists.
3. Dispatches the run to `BackgroundRunManager` — returns immediately.
4. Renders a `running` placeholder as the assistant chat reply.

The chat endpoint is responsible for persisting the user/assistant messages
and conversation auto-title — this module only produces the assistant text.
Run completion happens off-thread; the user reads the final result via the
Runs panel (or by polling `GET /execution/runs/{id}`).
"""

from __future__ import annotations

from .background import get_default_manager
from .manager import init_execution_workspace
from .models import RunRecord, TaskSpec


CODE_TRIGGER = "@code"

GENERAL_REJECTION_MESSAGE = (
    "Coding delegation is only available inside a project workspace."
)

EMPTY_TASK_MESSAGE = (
    "## Coding Delegation Error\n\n"
    "`@code` requires a task description. Try:\n\n"
    "```\n"
    "@code Create hello.py that prints 'Hello from Agent OS' and run it.\n"
    "```"
)


def is_code_delegation(message: str) -> bool:
    if not isinstance(message, str):
        return False
    stripped = message.lstrip()
    if not stripped.lower().startswith(CODE_TRIGGER):
        return False
    rest = stripped[len(CODE_TRIGGER) :]
    # `@coder`, `@codereview`, etc. should NOT trigger delegation — only
    # `@code` followed by whitespace, end-of-string, or punctuation does.
    return rest == "" or rest[0].isspace() or rest[0] in ":-,"


# Phase 6 — additional explicit chat commands. Unlike `@code` (which dispatches
# a run), these only SHAPE the Main Agent's chat response via an orchestration
# "mode" string. NONE of them dispatch anything — execution still requires
# `@code` or clicking "OK, run this" on a proposed plan.
# Phase 10 — `@search` / `@research` (one mode, two spellings): besides shaping
# the response, the explicit command is the per-turn grant that lets the chat
# endpoint enable the bounded research channel (see main.py). The command being
# explicit is the approval boundary — inferred `research` intent routes to the
# same mode but NEVER enables network access.
MODE_COMMANDS: dict[str, str] = {
    "@plan": "plan",
    "@design": "design",
    "@debug": "debug",
    "@review": "review",
    "@inspect": "inspect",
    "@memory": "memory",
    "@search": "research",
    "@research": "research",
}


def parse_mode_command(message: str) -> tuple[str | None, str]:
    """Detect a leading mode `@`-command.

    Returns ``(mode, body)`` when ``message`` starts with one of
    ``MODE_COMMANDS`` followed by whitespace / end-of-string / punctuation
    (the same guard ``is_code_delegation`` uses, so ``@reviewer`` does not match
    ``@review``); ``body`` is the message with the command prefix stripped.
    Returns ``(None, message)`` otherwise. Never raises.
    """
    if not isinstance(message, str):
        return None, message
    stripped = message.lstrip()
    low = stripped.lower()
    for cmd, mode in MODE_COMMANDS.items():
        if low.startswith(cmd):
            rest = stripped[len(cmd):]
            if rest == "" or rest[0].isspace() or rest[0] in ":-,":
                return mode, rest.lstrip(" :-,\t\n").strip()
    return None, message


def extract_task_card(message: str) -> str:
    stripped = message.lstrip()
    return stripped[len(CODE_TRIGGER) :].lstrip(" :-,\t\n").strip()


def handle_code_delegation(
    project_id: str,
    project_name: str | None,
    user_message: str,
) -> tuple[str, str | None]:
    """Dispatch the Coding Agent for a `@code …` chat message.

    Returns ``(reply_markdown, run_id)`` where ``reply_markdown`` is the
    immediate assistant-facing message (a natural "Coding Agent is running"
    note while the run executes in the background) and ``run_id`` is the
    dispatched run's id (``None`` on the empty-task / dispatch-error paths so
    the chat-first run follow-up card is not attached). Pre-condition: caller
    has confirmed the project is not the GENERAL workspace and the message
    starts with `@code`.
    """
    task_card = extract_task_card(user_message)
    if not task_card:
        return EMPTY_TASK_MESSAGE, None

    # Idempotent — creates folders and seeds AGENT.md/TASK.md only if missing.
    init_execution_workspace(project_id, project_name)

    title = _derive_title(task_card)
    spec = TaskSpec(title=title, task_card=task_card, created_by="chat")

    try:
        record = get_default_manager().dispatch(project_id, spec)
    except Exception as e:
        return _format_dispatch_error(e), None

    return _format_dispatch_placeholder(record), record.run_id


# ---------- helpers ----------


def _derive_title(task_card: str) -> str:
    first_line = task_card.split("\n", 1)[0].strip()
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    return first_line or "Untitled coding task"


def _format_dispatch_placeholder(record: RunRecord) -> str:
    """Natural chat-first placeholder (Task 06.2D).

    The structured run-id / endpoint card is gone — the live run status, the
    completion summary, and the browser-verification controls are rendered by
    the chat-level run follow-up card, which keys off the message's ``run_id``
    metadata. This text is just the conversational lead-in.
    """
    return (
        f"**Coding Agent is running** — _{record.task_title}_.\n\n"
        f"I'll update this thread when the first build pass finishes."
    )


def _format_dispatch_error(exc: Exception) -> str:
    return (
        "## Coding Agent Dispatch Failed\n\n"
        f"The run could not be queued — `{type(exc).__name__}`:\n\n"
        f"> {exc}\n\n"
        "No run was created. Check backend logs for details."
    )
