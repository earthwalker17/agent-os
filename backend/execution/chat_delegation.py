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


def extract_task_card(message: str) -> str:
    stripped = message.lstrip()
    return stripped[len(CODE_TRIGGER) :].lstrip(" :-,\t\n").strip()


def handle_code_delegation(
    project_id: str,
    project_name: str | None,
    user_message: str,
) -> str:
    """Dispatch the Coding Agent for a `@code …` chat message and return the
    immediate assistant-facing markdown reply (a placeholder while the run
    executes in the background). Pre-condition: caller has confirmed the
    project is not the GENERAL workspace and the message starts with `@code`.
    """
    task_card = extract_task_card(user_message)
    if not task_card:
        return EMPTY_TASK_MESSAGE

    # Idempotent — creates folders and seeds AGENT.md/TASK.md only if missing.
    init_execution_workspace(project_id, project_name)

    title = _derive_title(task_card)
    spec = TaskSpec(title=title, task_card=task_card, created_by="chat")

    try:
        record = get_default_manager().dispatch(project_id, spec)
    except Exception as e:
        return _format_dispatch_error(e)

    return _format_dispatch_placeholder(project_id, record)


# ---------- helpers ----------


def _derive_title(task_card: str) -> str:
    first_line = task_card.split("\n", 1)[0].strip()
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    return first_line or "Untitled coding task"


def _format_dispatch_placeholder(project_id: str, record: RunRecord) -> str:
    return (
        f"## Coding Agent Run Started\n\n"
        f"**Run ID:** `{record.run_id}`\n"
        f"**Status:** running\n"
        f"**Task:** {record.task_title}\n\n"
        f"The Coding Agent is working on this in the background. Open the "
        f"**Runs** panel on the right and hit refresh to see the result when "
        f"it's ready, or fetch:\n\n"
        f"- `/api/projects/{project_id}/execution/runs/{record.run_id}` — run record\n"
        f"- `/api/projects/{project_id}/execution/runs/{record.run_id}/result` — rendered result.md\n"
    )


def _format_dispatch_error(exc: Exception) -> str:
    return (
        "## Coding Agent Dispatch Failed\n\n"
        f"The run could not be queued — `{type(exc).__name__}`:\n\n"
        f"> {exc}\n\n"
        "No run was created. Check backend logs for details."
    )
