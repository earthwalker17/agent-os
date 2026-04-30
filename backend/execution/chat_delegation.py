"""Bridge from /api/chat to the Coding Agent runner.

When a project chat message starts with `@code`, the chat endpoint hands the
message off to this module. The helper:

1. Strips the `@code` prefix to form the task card.
2. Lazily ensures the project's execution workspace exists.
3. Calls `CodingAgentRunner.run_task()` synchronously.
4. Renders the `ResultSummary` as a markdown chat reply.

The chat endpoint is responsible for persisting the user/assistant messages
and conversation auto-title — this module only produces the assistant text.
"""

from __future__ import annotations

from .manager import init_execution_workspace
from .models import ResultSummary, TaskSpec
from .runner import CodingAgentRunner


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
    """Run the Coding Agent for a `@code …` chat message and return the
    assistant-facing markdown reply. Pre-condition: caller has confirmed the
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
        summary = CodingAgentRunner(project_id).run_task(spec)
    except Exception as e:
        return _format_runner_error(e)

    return _format_summary(project_id, summary)


# ---------- helpers ----------


def _derive_title(task_card: str) -> str:
    first_line = task_card.split("\n", 1)[0].strip()
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    return first_line or "Untitled coding task"


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {x}" for x in items) if items else "None"


def _format_summary(project_id: str, summary: ResultSummary) -> str:
    return (
        f"## Coding Agent Run Complete\n\n"
        f"**Run ID:** `{summary.run_id}`\n"
        f"**Status:** {summary.status}\n\n"
        f"### Summary\n"
        f"{summary.summary.strip() or '_(no summary provided)_'}\n\n"
        f"### Files Changed\n"
        f"{_bullets(summary.files_changed)}\n\n"
        f"### Commands Run\n"
        f"{_bullets(summary.commands_run)}\n\n"
        f"### Blockers\n"
        f"{_bullets(summary.blockers)}\n\n"
        f"You can inspect the full result at:\n"
        f"`/api/projects/{project_id}/execution/runs/{summary.run_id}/result`"
    )


def _format_runner_error(exc: Exception) -> str:
    return (
        "## Coding Agent Run Failed\n\n"
        f"The runner raised `{type(exc).__name__}` before completing:\n\n"
        f"> {exc}\n\n"
        "No run summary is available. Check backend logs for details."
    )
