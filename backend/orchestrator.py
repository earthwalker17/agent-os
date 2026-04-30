"""
Orchestration layer for Agent OS.

Loads global + project memory, assembles context for the LLM,
and produces project-aware responses via Claude API.

Two-step flow per chat turn:
  1. Main response — conversational reply to the user
  2. Memory judgment — structured decision about what project knowledge to persist

Memory policy:
  - SOUL.md is READ-ONLY — loaded every turn as system-level identity anchor,
    never auto-written or modified by the orchestration pipeline.
  - All other global/project memory files may participate in automatic writeback
    when the agent decides there is new durable knowledge worth persisting.
"""

import json
import logging
from pathlib import Path
from dataclasses import dataclass, field

from llm import chat as llm_chat

log = logging.getLogger(__name__)

MEMORY_DIR = Path(__file__).resolve().parent.parent / "memory"
PROJECTS_DIR = Path(__file__).resolve().parent.parent / "projects"

GLOBAL_MEMORY_FILES = ["USER.md", "WORKSTYLE.md", "SOUL.md", "MEMORY.md"]
PROJECT_MEMORY_FILES = [
    "PROJECT.md", "STATUS.md", "TASK_QUEUE.md", "DECISIONS.md", "RESEARCH.md",
]

# Files that may be auto-written by the memory update pipeline.
# SOUL.md is explicitly excluded — it is read-only.
WRITABLE_GLOBAL_FILES: set[str] = {"USER.md", "WORKSTYLE.md", "MEMORY.md"}
WRITABLE_PROJECT_FILES: set[str] = {
    "PROJECT.md", "STATUS.md", "TASK_QUEUE.md", "DECISIONS.md", "RESEARCH.md",
}


GENERAL_PROJECT_ID = "__GENERAL__"


@dataclass
class MemoryContext:
    """All loaded memory for a single orchestration call."""
    # Global
    user: str
    workstyle: str
    soul: str
    global_memory: str
    # Project (empty for GENERAL workspace)
    project: str
    status: str
    task_queue: str
    decisions: str
    research: str
    # Derived
    project_name: str
    project_id: str
    # Conversation history (list of {role, content} dicts)
    history: list[dict] = field(default_factory=list)


def _read(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def load_global_memory() -> dict[str, str]:
    """Load the three writable global memory files (for viewer/editor API)."""
    return {
        "USER.md": _read(MEMORY_DIR / "USER.md"),
        "WORKSTYLE.md": _read(MEMORY_DIR / "WORKSTYLE.md"),
        "MEMORY.md": _read(MEMORY_DIR / "MEMORY.md"),
    }


def load_memory(project_id: str, history: list[dict] | None = None) -> MemoryContext:
    """Load global and project memory files into a single context object."""

    # Global memory
    user = _read(MEMORY_DIR / "USER.md")
    workstyle = _read(MEMORY_DIR / "WORKSTYLE.md")
    soul = _read(MEMORY_DIR / "SOUL.md")
    global_memory = _read(MEMORY_DIR / "MEMORY.md")

    # Project memory (empty for GENERAL workspace)
    project = status = task_queue = decisions = research = ""
    project_name = project_id

    if project_id != GENERAL_PROJECT_ID:
        project_path = PROJECTS_DIR / project_id
        project = _read(project_path / "PROJECT.md")
        status = _read(project_path / "STATUS.md")
        task_queue = _read(project_path / "TASK_QUEUE.md")
        decisions = _read(project_path / "DECISIONS.md")
        research = _read(project_path / "RESEARCH.md")

        # Extract project name from first line of PROJECT.md
        if project:
            first_line = project.split("\n")[0]
            if first_line.startswith("# "):
                project_name = first_line[2:].strip()

    return MemoryContext(
        user=user,
        workstyle=workstyle,
        soul=soul,
        global_memory=global_memory,
        project=project,
        status=status,
        task_queue=task_queue,
        decisions=decisions,
        research=research,
        project_name=project_name,
        project_id=project_id,
        history=history or [],
    )


# ---------------------------------------------------------------------------
# Context assembly — builds system prompt + messages for the LLM
# ---------------------------------------------------------------------------

def _build_system_prompt(ctx: MemoryContext) -> str:
    """
    Assemble the system prompt from memory context.

    Structure:
      1. SOUL.md — core identity (always first, always present)
      2. Global memory — user profile, workstyle, cross-project notes
      3. Project memory — all project files
      4. Behavioral rules for memory and honesty
    """
    sections: list[str] = []

    # 1. SOUL.md as the identity anchor
    if ctx.soul:
        sections.append(ctx.soul)

    # 2. Global memory
    global_parts: list[str] = []
    if ctx.user:
        global_parts.append(f"## User Profile\n{ctx.user}")
    if ctx.workstyle:
        global_parts.append(f"## Workstyle Preferences\n{ctx.workstyle}")
    if ctx.global_memory:
        global_parts.append(f"## Cross-Project Memory\n{ctx.global_memory}")
    if global_parts:
        sections.append("---\n\n# Global Context\n\n" + "\n\n".join(global_parts))

    # 3. Project memory
    project_parts: list[str] = []
    if ctx.project:
        project_parts.append(f"## PROJECT.md\n{ctx.project}")
    if ctx.status:
        project_parts.append(f"## STATUS.md\n{ctx.status}")
    if ctx.task_queue:
        project_parts.append(f"## TASK_QUEUE.md\n{ctx.task_queue}")
    if ctx.decisions:
        project_parts.append(f"## DECISIONS.md\n{ctx.decisions}")
    if ctx.research:
        project_parts.append(f"## RESEARCH.md\n{ctx.research}")
    if project_parts:
        sections.append(
            f"---\n\n# Current Project: {ctx.project_name}\n\n"
            + "\n\n".join(project_parts)
        )

    # 4. Behavioral rules about memory and honesty
    if ctx.project_id == GENERAL_PROJECT_ID:
        memory_rule = (
            "## Memory\n"
            "You own global memory maintenance directly. After this conversation turn, "
            "the system will separately ask you to judge whether any global memory files "
            "should be updated based on what was discussed. You do not need to describe "
            "or announce memory updates in your response — the system handles this.\n"
            "Writable global files: USER.md (user profile), WORKSTYLE.md (collaboration preferences), "
            "MEMORY.md (cross-project notes)."
        )
    else:
        memory_rule = (
            "## Memory\n"
            "You own project memory maintenance directly. After this conversation turn, "
            "the system will separately ask you to judge whether any project memory files "
            "should be updated based on what was discussed. You do not need to describe "
            "or announce memory updates in your response — the system handles this."
        )

    sections.append(
        "---\n\n# Agent Behavioral Rules\n\n"
        + memory_rule + "\n\n"
        "## Honesty\n"
        "- Never claim you updated a memory file in your chat response. "
        "Memory writes happen in a separate backend step that you do not control from chat.\n"
        "- Never claim you delegated work to Claude Code or any execution agent. "
        "No delegation path exists yet. If the user asks for code execution, "
        "acknowledge that delegation is not yet built and help with planning instead.\n"
        "- Never pretend an action happened that did not actually happen.\n\n"
        "## SOUL.md\n"
        "SOUL.md is read-only. It defines your identity. Never suggest modifying it."
    )

    return "\n\n".join(sections)


def _build_messages(ctx: MemoryContext, current_message: str) -> list[dict]:
    """
    Build the messages array from conversation history + current message.

    The current user message is always the last entry.
    """
    messages: list[dict] = []

    for msg in ctx.history:
        messages.append({"role": msg["role"], "content": msg["content"]})

    # If history doesn't already end with the current message, add it
    if not messages or messages[-1].get("content") != current_message:
        messages.append({"role": "user", "content": current_message})

    # Ensure messages alternate properly and start with user
    # (Anthropic API requires this)
    cleaned: list[dict] = []
    for msg in messages:
        if cleaned and cleaned[-1]["role"] == msg["role"]:
            cleaned[-1]["content"] += "\n\n" + msg["content"]
        else:
            cleaned.append(msg)

    if cleaned and cleaned[0]["role"] != "user":
        cleaned = cleaned[1:]

    return cleaned


# ---------------------------------------------------------------------------
# Main orchestration entry point
# ---------------------------------------------------------------------------

def orchestrate(project_id: str, message: str, history: list[dict] | None = None) -> str:
    """
    Main orchestration entry point.

    Loads memory, assembles context, calls the LLM, and returns the response.
    SOUL.md is always loaded as the system-level identity anchor.
    """
    ctx = load_memory(project_id, history=history)
    system_prompt = _build_system_prompt(ctx)
    messages = _build_messages(ctx, message)

    return llm_chat(system=system_prompt, messages=messages)


# ---------------------------------------------------------------------------
# Memory judgment — LLM-driven semantic writeback
# ---------------------------------------------------------------------------

_MEMORY_JUDGE_SYSTEM = """\
You are the memory maintenance subsystem of Agent OS.

Your job: examine the latest conversation turn and the current project memory files, \
then decide whether any memory files should be updated with new durable project knowledge.

## File purposes
- PROJECT.md: project definition, vision, scope, target user, tech stack
- STATUS.md: current phase, latest milestone, what works, what's next
- TASK_QUEUE.md: actionable task tracking (In Progress / Up Next / Done sections with checkboxes)
- DECISIONS.md: important project decisions with rationale
- RESEARCH.md: useful findings, external references, technical notes

## Rules
1. Only propose updates when there is genuinely new durable project knowledge — \
not just conversation chatter.
2. Write clean, structured markdown that fits the file's existing format. \
Do NOT dump raw conversation text. Summarize and structure the knowledge.
3. SOUL.md is read-only. Never include it in updates.
4. Use action "append" to add new content to the end of a section. \
Use action "replace" to overwrite a section with updated content.
5. For TASK_QUEUE.md, use checkbox format: "- [ ] task" for open, "- [x] task" for done.
6. Keep updates concise. One clear update per file — don't repeat existing content.
7. If nothing worth persisting happened in this turn, return an empty array.
8. The "section" field must match an existing ## heading in the file, or a new heading will be created.

## Response format
Return ONLY a JSON array. No markdown fencing, no explanation. Examples:

No updates needed:
[]

One update:
[{"filename": "DECISIONS.md", "section": "Decisions", "content": "- Chose FastAPI over Flask for async support and auto-generated docs", "action": "append"}]

Multiple updates:
[{"filename": "STATUS.md", "section": "Current Phase", "content": "Implementation", "action": "replace"}, {"filename": "TASK_QUEUE.md", "section": "Up Next", "content": "- [ ] Set up CI/CD pipeline", "action": "append"}]
"""


def judge_memory_updates(
    ctx: MemoryContext,
    user_message: str,
    assistant_response: str,
) -> list[dict]:
    """
    LLM-driven memory judgment step.

    Given the current memory state, the latest user message, and the assistant's
    response, ask the LLM to decide what (if any) memory updates should be written.

    Returns a list of {filename, section, content, action} dicts.
    """
    # Build a concise view of current memory for the judge
    memory_snapshot = []
    for name, content in [
        ("PROJECT.md", ctx.project),
        ("STATUS.md", ctx.status),
        ("TASK_QUEUE.md", ctx.task_queue),
        ("DECISIONS.md", ctx.decisions),
        ("RESEARCH.md", ctx.research),
    ]:
        if content:
            memory_snapshot.append(f"### {name}\n{content}")
        else:
            memory_snapshot.append(f"### {name}\n(empty)")

    user_prompt = (
        "## Current Project Memory\n\n"
        + "\n\n".join(memory_snapshot)
        + "\n\n---\n\n"
        "## Latest Conversation Turn\n\n"
        f"**User:** {user_message}\n\n"
        f"**Assistant:** {assistant_response}\n\n"
        "---\n\n"
        "Based on this turn, return a JSON array of memory updates (or [] if none needed)."
    )

    raw = llm_chat(
        system=_MEMORY_JUDGE_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        max_tokens=1024,
    )

    return _parse_memory_updates(raw)


def _parse_memory_updates(raw: str) -> list[dict]:
    """
    Parse the LLM's JSON response into a list of update dicts.
    Handles common LLM output quirks (markdown fencing, extra text).
    """
    text = raw.strip()

    # Strip markdown code fences if present
    if "```" in text:
        # Find content between first ``` and last ```
        first = text.find("```")
        last = text.rfind("```")
        if first != last:
            inner = text[first:last]
            # Remove the opening ``` line (may include language tag like ```json)
            first_newline = inner.find("\n")
            if first_newline != -1:
                text = inner[first_newline + 1:].strip()
            else:
                text = ""

    if not text:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Memory judge returned invalid JSON: %s", text[:200])
        return []

    if not isinstance(parsed, list):
        log.warning("Memory judge returned non-list: %s", type(parsed))
        return []

    # Validate each entry
    valid: list[dict] = []
    required_keys = {"filename", "section", "content", "action"}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        if not required_keys.issubset(entry.keys()):
            continue
        if entry["action"] not in ("append", "replace"):
            continue
        if entry["filename"] not in WRITABLE_PROJECT_FILES:
            continue
        valid.append({
            "filename": entry["filename"],
            "section": entry["section"],
            "content": entry["content"],
            "action": entry["action"],
        })

    return valid


def apply_memory_updates(project_id: str, updates: list[dict]) -> list[dict]:
    """
    Apply a list of proposed memory updates through the policy filter.

    Returns only the updates that were successfully applied.
    """
    applied: list[dict] = []
    for update in updates:
        ok = apply_memory_update(
            project_id,
            update["filename"],
            update["section"],
            update["content"],
            update["action"],
        )
        if ok:
            applied.append({**update, "applied": True})
        else:
            log.warning("Memory update rejected: %s/%s", update["filename"], update["section"])
    return applied


def apply_memory_update(project_id: str, filename: str, section: str, content: str, action: str) -> bool:
    """
    Apply a single memory update to a project file.

    Policy enforcement:
      - SOUL.md is never writable (global read-only file).
      - Only files in WRITABLE_PROJECT_FILES are accepted.

    Supports 'append' (add to section) and 'replace' (overwrite section).
    Returns True if the update was applied.
    """
    if filename not in WRITABLE_PROJECT_FILES:
        return False

    filepath = PROJECTS_DIR / project_id / filename
    if not filepath.parent.exists():
        return False

    current = ""
    if filepath.exists():
        current = filepath.read_text(encoding="utf-8")

    if action == "append":
        if current and not current.endswith("\n"):
            current += "\n"
        current += content + "\n"
        filepath.write_text(current, encoding="utf-8")
        return True

    elif action == "replace":
        lines = current.split("\n")
        new_lines = []
        in_section = False
        replaced = False
        for line in lines:
            if line.startswith(f"## {section}"):
                new_lines.append(line)
                new_lines.append(content)
                in_section = True
                replaced = True
                continue
            if in_section and line.startswith("## "):
                in_section = False
            if not in_section:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"\n## {section}")
            new_lines.append(content)
        filepath.write_text("\n".join(new_lines), encoding="utf-8")
        return True

    return False


# ---------------------------------------------------------------------------
# Global memory judgment — LLM-driven semantic writeback for GENERAL chats
# ---------------------------------------------------------------------------

_GLOBAL_MEMORY_JUDGE_SYSTEM = """\
You are the global memory maintenance subsystem of Agent OS.

Your job: examine the latest conversation turn and the current global memory files, \
then decide whether any global memory files should be updated with new durable knowledge.

## File purposes
- USER.md: durable user profile — identity, role, long-term goals, stable personal context
- WORKSTYLE.md: collaboration preferences — response style, communication preferences, working habits
- MEMORY.md: cross-project notes — recurring lessons, meta-level ongoing context, reusable knowledge

## Rules
1. Only propose updates when there is genuinely new durable knowledge — \
not just conversation chatter.
2. Write clean, structured markdown that fits the file's existing format. \
Do NOT dump raw conversation text. Summarize and structure the knowledge.
3. SOUL.md is read-only. Never include it in updates.
4. Use action "append" to add new content to the end of a section. \
Use action "replace" to overwrite a section with updated content.
5. Keep updates concise. One clear update per file — don't repeat existing content.
6. If nothing worth persisting happened in this turn, return an empty array.
7. The "section" field must match an existing ## heading in the file, or a new heading will be created.

## Response format
Return ONLY a JSON array. No markdown fencing, no explanation. Examples:

No updates needed:
[]

One update:
[{"filename": "USER.md", "section": "Role", "content": "Senior backend engineer at Acme Corp", "action": "replace"}]

Multiple updates:
[{"filename": "WORKSTYLE.md", "section": "Response Style", "content": "- Prefers concise answers with code examples", "action": "append"}, {"filename": "MEMORY.md", "section": "Lessons Learned", "content": "- Always check database indexes before optimizing queries", "action": "append"}]
"""


def judge_global_memory_updates(
    ctx: MemoryContext,
    user_message: str,
    assistant_response: str,
) -> list[dict]:
    """
    LLM-driven global memory judgment step for GENERAL conversations.

    Returns a list of {filename, section, content, action} dicts for global files only.
    """
    memory_snapshot = []
    for name, content in [
        ("USER.md", ctx.user),
        ("WORKSTYLE.md", ctx.workstyle),
        ("MEMORY.md", ctx.global_memory),
    ]:
        if content:
            memory_snapshot.append(f"### {name}\n{content}")
        else:
            memory_snapshot.append(f"### {name}\n(empty)")

    user_prompt = (
        "## Current Global Memory\n\n"
        + "\n\n".join(memory_snapshot)
        + "\n\n---\n\n"
        "## Latest Conversation Turn\n\n"
        f"**User:** {user_message}\n\n"
        f"**Assistant:** {assistant_response}\n\n"
        "---\n\n"
        "Based on this turn, return a JSON array of global memory updates (or [] if none needed)."
    )

    raw = llm_chat(
        system=_GLOBAL_MEMORY_JUDGE_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        max_tokens=1024,
    )

    return _parse_global_memory_updates(raw)


def _parse_global_memory_updates(raw: str) -> list[dict]:
    """Parse global memory judge response, filtering to writable global files only."""
    text = raw.strip()

    if "```" in text:
        first = text.find("```")
        last = text.rfind("```")
        if first != last:
            inner = text[first:last]
            first_newline = inner.find("\n")
            if first_newline != -1:
                text = inner[first_newline + 1:].strip()
            else:
                text = ""

    if not text:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Global memory judge returned invalid JSON: %s", text[:200])
        return []

    if not isinstance(parsed, list):
        log.warning("Global memory judge returned non-list: %s", type(parsed))
        return []

    valid: list[dict] = []
    required_keys = {"filename", "section", "content", "action"}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        if not required_keys.issubset(entry.keys()):
            continue
        if entry["action"] not in ("append", "replace"):
            continue
        if entry["filename"] not in WRITABLE_GLOBAL_FILES:
            continue
        valid.append({
            "filename": entry["filename"],
            "section": entry["section"],
            "content": entry["content"],
            "action": entry["action"],
        })

    return valid


def apply_global_memory_updates(updates: list[dict]) -> list[dict]:
    """Apply a list of proposed global memory updates through the policy filter."""
    applied: list[dict] = []
    for update in updates:
        ok = apply_global_memory_update(
            update["filename"],
            update["section"],
            update["content"],
            update["action"],
        )
        if ok:
            applied.append({**update, "applied": True})
        else:
            log.warning("Global memory update rejected: %s/%s", update["filename"], update["section"])
    return applied


def apply_global_memory_update(filename: str, section: str, content: str, action: str) -> bool:
    """
    Apply a single memory update to a global memory file.

    Policy: only WRITABLE_GLOBAL_FILES are accepted. SOUL.md is never writable.
    """
    if filename not in WRITABLE_GLOBAL_FILES:
        return False

    filepath = MEMORY_DIR / filename
    current = ""
    if filepath.exists():
        current = filepath.read_text(encoding="utf-8")

    if action == "append":
        if current and not current.endswith("\n"):
            current += "\n"
        current += content + "\n"
        filepath.write_text(current, encoding="utf-8")
        return True

    elif action == "replace":
        lines = current.split("\n")
        new_lines = []
        in_section = False
        replaced = False
        for line in lines:
            if line.startswith(f"## {section}"):
                new_lines.append(line)
                new_lines.append(content)
                in_section = True
                replaced = True
                continue
            if in_section and line.startswith("## "):
                in_section = False
            if not in_section:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"\n## {section}")
            new_lines.append(content)
        filepath.write_text("\n".join(new_lines), encoding="utf-8")
        return True

    return False
