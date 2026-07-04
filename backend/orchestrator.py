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
from typing import Callable, Optional

from llm import chat as llm_chat
import memory_engine

log = logging.getLogger(__name__)


# Task 06.1 — bounded on-demand file inspection.
# The orchestrator gives the LLM an "inspect_request" channel when the
# project has an execution workspace. The loop caps the number of
# inspections per turn so a runaway model can't dump the repo into context.
MAX_INSPECTIONS_PER_TURN = 3

MEMORY_DIR = Path(__file__).resolve().parent.parent / "memory"
PROJECTS_DIR = Path(__file__).resolve().parent.parent / "projects"

GLOBAL_MEMORY_FILES = ["USER.md", "WORKSTYLE.md", "SOUL.md", "MEMORY.md"]
PROJECT_MEMORY_FILES = [
    "PROJECT.md", "STATUS.md", "TASK_QUEUE.md", "DECISIONS.md", "RESEARCH.md",
]

# Files that may be auto-written by the memory update pipeline.
# SOUL.md is explicitly excluded — it is read-only. The authoritative policy
# sets live in ``memory_engine`` (shared with post-run reconciliation); these
# aliases keep the orchestrator's parsers reading from a single source of truth.
WRITABLE_GLOBAL_FILES = memory_engine.WRITABLE_GLOBAL
WRITABLE_PROJECT_FILES = memory_engine.WRITABLE_PROJECT


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
# Context Loader v2 (Phase 6.1) — keep the main-agent prompt compact as memory
# grows. Applied ONLY to the project-memory sections of the main orchestration
# system prompt; the delegation + reconciliation snapshots are separate,
# already-bounded code paths and are intentionally untouched. SOUL.md never
# routes through here (it is loaded full + first).
# ---------------------------------------------------------------------------

# Per-file char budget in the main prompt. Below this a file is byte-identical,
# so small/young projects (and tiny test fixtures) are unaffected.
_CONTEXT_FILE_CHAR_CAP = 2400
# Newest entries kept when an archive-style section is trimmed.
_KEEP_RECENT_ITEMS = 8
# "Current working state" sections — never trimmed (the live, high-signal part).
# STATUS.md / PROJECT.md are kept whole; within TASK_QUEUE.md these stay whole.
_CURRENT_STATE_SECTIONS = {"In Progress", "Up Next"}


def _trim_section_body(buf: list[str]) -> list[str]:
    """Keep the most-recent ``_KEEP_RECENT_ITEMS`` non-empty lines (the tail —
    newest, since memory is append-mostly) with a leading elision note."""
    content_lines = [ln for ln in buf if ln.strip()]
    if len(content_lines) <= _KEEP_RECENT_ITEMS:
        return buf
    elided = len(content_lines) - _KEEP_RECENT_ITEMS
    kept: list[str] = []
    count = 0
    for ln in reversed(buf):
        kept.append(ln)
        if ln.strip():
            count += 1
        if count >= _KEEP_RECENT_ITEMS:
            break
    kept.reverse()
    note = f"_({elided} older entr{'y' if elided == 1 else 'ies'} elided)_"
    return ["", note, ""] + kept


def _compact_memory(filename: str, content: str) -> str:
    """Compact a project-memory file for the main prompt when it grows large.

    STATUS.md (current state) and PROJECT.md (identity/scope) are returned whole.
    For the append-growth files (TASK_QUEUE.md / DECISIONS.md / RESEARCH.md),
    once the file exceeds ``_CONTEXT_FILE_CHAR_CAP`` each ``##`` section's body is
    trimmed to its newest entries — except the live ``In Progress`` / ``Up Next``
    sections, which stay whole. Below the cap the content is byte-identical.
    """
    if filename in ("STATUS.md", "PROJECT.md"):
        return content
    if not content or len(content) <= _CONTEXT_FILE_CHAR_CAP:
        return content

    lines = content.split("\n")
    out: list[str] = []
    section_name: Optional[str] = None
    buf: list[str] = []

    def _flush() -> None:
        if section_name is not None and section_name not in _CURRENT_STATE_SECTIONS:
            out.extend(_trim_section_body(buf))
        else:
            out.extend(buf)

    for line in lines:
        if line.startswith("## "):
            _flush()
            buf = []
            section_name = line[3:].strip()
            out.append(line)
        elif section_name is None:
            out.append(line)  # preamble (title + intro) stays
        else:
            buf.append(line)
    _flush()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Context assembly — builds system prompt + messages for the LLM
# ---------------------------------------------------------------------------

_MODE_GUIDANCE = {
    "plan": (
        "## This turn: PLANNING\n"
        "The user invoked `@plan`. Take a planning posture: turn the request into "
        "a concrete, sequenced set of next steps, call out dependencies and risks, "
        "and recommend what to do first. Do not write code — propose the plan. If a "
        "step warrants the Coding Agent, describe it so the user can dispatch it."
    ),
    "design": (
        "## This turn: DESIGN\n"
        "The user invoked `@design`. Focus on product / architecture / UX design: "
        "explore the shape of the solution, weigh tradeoffs, and recommend a "
        "structure. Stay at the design level; don't write implementation code."
    ),
    "debug": (
        "## This turn: DEBUG / RECOVERY\n"
        "The user invoked `@debug`. Help diagnose what went wrong and propose the "
        "next bounded step (inspect a specific file, a focused repair task, or "
        "splitting the work). Use the file-inspection channel when you need to see "
        "actual code. Recommend a concrete fix the user can dispatch — don't claim "
        "you fixed anything yourself."
    ),
    "review": (
        "## This turn: REVIEW\n"
        "The user invoked `@review`. Take a code-review / retrospective posture: "
        "examine what exists, assess quality and risks, and summarize findings with "
        "concrete recommendations. Use the file-inspection channel to ground your "
        "review in the real code rather than guessing."
    ),
    "inspect": (
        "## This turn: INSPECT\n"
        "The user invoked `@inspect`. They want to understand specific code/files. "
        "Use the bounded file-inspection channel to read what's relevant and answer "
        "precisely from what you actually read — never invent file contents."
    ),
    "memory": (
        "## This turn: MEMORY\n"
        "The user invoked `@memory`. They want project knowledge captured. Confirm "
        "what you understood and how you'd record it; the system persists durable "
        "memory in a separate step — don't claim you wrote files yourself."
    ),
    "docs": (
        "## This turn: DOCS\n"
        "This turn is about documentation. Help draft or improve clear, accurate "
        "docs (README, guides, comments-level explanations). Stay at the writing "
        "level; if doc files in the repo need editing, that's a Coding Agent task "
        "the user can dispatch — propose it, don't claim you wrote files."
    ),
    "research": (
        "## This turn: RESEARCH\n"
        "This turn is about research / investigation. Lay out options, references, "
        "and tradeoffs clearly so the finding can be recorded in RESEARCH.md. Be "
        "honest about what is and isn't known; don't fabricate sources."
    ),
}


def _mode_guidance_section(ctx: MemoryContext, mode: Optional[str]) -> str:
    """Return a small mode-specific guidance block for the system prompt.

    For ``debug``, also fold in a compact summary of the project's latest
    non-green run (read-only, summary-level only — never repo contents) so the
    Main Agent can reason about the failure. Best-effort; never raises.
    """
    if not mode:
        return ""
    block = _MODE_GUIDANCE.get(mode, "")
    if not block:
        return ""
    if mode == "debug":
        run_ctx = _latest_nongreen_run_context(ctx.project_id)
        if run_ctx:
            block += "\n\n### Latest non-green run\n" + run_ctx
    if mode in ("review", "debug"):
        git_ctx = _latest_git_state_context(ctx.project_id)
        if git_ctx:
            block += "\n\n### Project Ops (Git/GitHub) state\n" + git_ctx
    return "---\n\n# Mode\n\n" + block


def _latest_nongreen_run_context(project_id: str) -> str:
    """Compact summary of the most recent partial/failed/blocked run, or ''.

    Summary-level only (status, title, summary, blockers) — no repo contents,
    no raw logs. Lazily imports the execution layer and swallows all errors.
    """
    if project_id == GENERAL_PROJECT_ID:
        return ""
    try:
        from execution import run_store
        runs = run_store.list_runs(project_id)
    except Exception:  # noqa: BLE001
        return ""
    nongreen = {"partial", "failed", "blocked"}
    for rec in runs or []:  # list_runs is newest-first
        status = str(rec.get("status", "")).lower()
        if status not in nongreen:
            continue
        title = str(rec.get("task_title", "") or "(untitled)")
        summary = str(rec.get("summary", "") or "").strip()
        blockers = rec.get("blockers") or []
        lines = [f"- **{title}** — status `{status}`"]
        if summary:
            lines.append(f"  - summary: {summary[:400]}")
        # Phase 9 — when this was a TEAM run, surface the wave/integration
        # picture so the main agent (project manager) can reason about what the
        # team actually did rather than seeing a flat status. Metadata only
        # (counts + conflicts), never repo contents (§6 context hygiene).
        team_line = _team_run_context(rec)
        if team_line:
            lines.append(team_line)
        for b in blockers[:5]:
            lines.append(f"  - blocker: {str(b)[:200]}")
        return "\n".join(lines)
    return ""


def _team_run_context(rec: dict) -> str:
    """One compact line describing a team run's wave/integration shape, or ''.

    Reads only the metadata already on the run record (``plan.execution_mode``,
    ``plan.tasks`` roles/waves, ``integration`` counts) — never repo contents.
    Empty for a sequential run, so the PM context is unchanged for those.
    """
    plan = rec.get("plan") or {}
    integ = rec.get("integration") or {}
    is_team = plan.get("execution_mode") == "team" or integ.get("enabled")
    if not is_team:
        return ""
    tasks = plan.get("tasks") or []
    waves = sorted({t.get("wave") for t in tasks if t.get("wave") is not None})
    roles: dict[str, int] = {}
    for t in tasks:
        r = str(t.get("role") or "coder")
        roles[r] = roles.get(r, 0) + 1
    role_str = ", ".join(f"{n}×{r}" for r, n in roles.items())
    applied = len(integ.get("files_applied") or [])
    conflicts = integ.get("conflicts") or []
    parts = [
        f"team run: {len(tasks)} task(s) across {len(waves)} wave(s)",
    ]
    if role_str:
        parts.append(f"roles {role_str}")
    parts.append(f"{applied} file(s) integrated")
    if conflicts:
        paths = ", ".join(str(c.get("path")) for c in conflicts[:3])
        parts.append(f"{len(conflicts)} integration conflict(s) [{paths}]")
    else:
        parts.append("no integration conflicts")
    return "  - " + "; ".join(parts)


def _latest_git_state_context(project_id: str) -> str:
    """Phase 7 — compact Git/GitHub delivery state for the most recent run that
    has any (branch / commit / PR / diff-stat), or ''.

    Metadata only (§6 context hygiene): branch, short commit sha, PR url, and a
    one-line diff-stat — NEVER the raw diff. The full diff is reachable only via
    the bounded ``/diff`` endpoint on a concrete reason. Lazily imports the
    execution layer; swallows all errors.
    """
    if project_id == GENERAL_PROJECT_ID:
        return ""
    try:
        from execution import run_store

        runs = run_store.list_runs(project_id)
    except Exception:  # noqa: BLE001
        return ""
    for rec in runs or []:  # newest-first
        commit = rec.get("commit_sha")
        pr_url = rec.get("pr_url")
        diff_stat = rec.get("diff_stat")
        branch = rec.get("branch")
        if not any((commit, pr_url, diff_stat, rec.get("pushed"))):
            continue
        title = str(rec.get("task_title", "") or "(untitled)")
        lines = [f"- **{title}**"]
        if branch:
            lines.append(f"  - branch: `{branch}`")
        if diff_stat:
            lines.append(f"  - diff: {str(diff_stat)[:200]}")
        if commit:
            lines.append(f"  - commit: `{str(commit)[:12]}`")
        if rec.get("pushed"):
            lines.append("  - pushed: yes")
        if pr_url:
            num = rec.get("pr_number")
            lines.append(f"  - PR{f' #{num}' if num else ''}: {pr_url}")
        return "\n".join(lines)
    return ""


def _build_system_prompt(
    ctx: MemoryContext, *, inspection_enabled: bool = False, mode: Optional[str] = None
) -> str:
    """
    Assemble the system prompt from memory context.

    Structure:
      1. SOUL.md — core identity (always first, always present)
      2. Global memory — user profile, workstyle, cross-project notes
      3. Project memory — all project files
      4. Behavioral rules for memory and honesty
      5. (optional) Mode guidance — when an `@`-command set an orchestration mode
      6. (optional) File inspection channel — only when ``inspection_enabled``
         is True (project has an execution workspace and is not GENERAL)
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

    # 3. Project memory (Phase 6.1 — compacted for the main prompt as it grows;
    #    STATUS.md / PROJECT.md kept whole, archive sections trimmed to newest).
    project_parts: list[str] = []
    if ctx.project:
        project_parts.append(f"## PROJECT.md\n{_compact_memory('PROJECT.md', ctx.project)}")
    if ctx.status:
        project_parts.append(f"## STATUS.md\n{_compact_memory('STATUS.md', ctx.status)}")
    if ctx.task_queue:
        project_parts.append(f"## TASK_QUEUE.md\n{_compact_memory('TASK_QUEUE.md', ctx.task_queue)}")
    if ctx.decisions:
        project_parts.append(f"## DECISIONS.md\n{_compact_memory('DECISIONS.md', ctx.decisions)}")
    if ctx.research:
        project_parts.append(f"## RESEARCH.md\n{_compact_memory('RESEARCH.md', ctx.research)}")
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
        "## Delegation\n"
        "You CAN delegate code/file work to the Coding Agent, which runs inside "
        "this project's sandboxed `repo/` workspace. There are two explicit paths: "
        "the user types `@code <task>` (runs immediately), or you propose a plan and "
        "the user clicks **OK, run this** on it. You never start a run yourself from "
        "inferred intent — you propose, the user confirms. When a coding task is "
        "warranted, help shape a crisp task and let that confirmable flow handle it.\n"
        "- Do not claim a run has started, finished, or changed files unless a run "
        "actually reported that outcome. Proposing a plan is not the same as running it.\n\n"
        "## Honesty\n"
        "- Never claim you updated a memory file in your chat response. "
        "Memory writes happen in a separate backend step that you do not control from chat.\n"
        "- Never pretend an action happened that did not actually happen. "
        "Distinguish planning from completion.\n\n"
        "## SOUL.md\n"
        "SOUL.md is read-only. It defines your identity. Never suggest modifying it."
    )

    # 5. Mode guidance (from an explicit `@`-command this turn).
    mode_section = _mode_guidance_section(ctx, mode)
    if mode_section:
        sections.append(mode_section)

    if inspection_enabled:
        # Imported lazily to avoid pulling execution-layer code into GENERAL
        # chats or callers that don't need inspection.
        from execution.inspect import inspect_system_prompt_section
        sections.append("---\n\n# Available Channels\n\n" + inspect_system_prompt_section())

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

def orchestrate(
    project_id: str,
    message: str,
    history: list[dict] | None = None,
    *,
    llm_caller: Optional[Callable] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    mode: Optional[str] = None,
) -> tuple[str, list[dict]]:
    """
    Main orchestration entry point.

    Loads memory, assembles context, runs a bounded chat-with-inspection loop,
    and returns ``(text_response, inspected_files)``. SOUL.md is always loaded
    as the system-level identity anchor.

    The inspection loop is enabled only for non-GENERAL projects that have
    an initialized execution workspace. Inside the loop the LLM may emit
    ``{"inspect_request": {...}}`` JSON to read a file from ``repo/`` via
    the sandboxed inspect API. The loop caps at
    ``MAX_INSPECTIONS_PER_TURN`` so a runaway model cannot dump the repo
    into chat context.

    For GENERAL chats or projects without an execution workspace, behavior
    is unchanged: a single LLM call producing text, with
    ``inspected_files == []``.

    ``llm_caller`` is an optional injection seam for tests; the default is
    the live LLM. ``provider`` selects the model provider for the main
    response (Task 07.1); ``model`` optionally pins a specific model within
    that provider (Provider Registry 2.0) — when omitted the provider's
    default model is used. Both are ignored when ``llm_caller`` is supplied.

    ``mode`` (Phase 6) is an optional orchestration mode set by an explicit
    `@`-command (``plan``/``design``/``debug``/``review``/``inspect``/``memory``);
    it appends a small guidance block to the system prompt and never changes
    routing or dispatch.
    """
    ctx = load_memory(project_id, history=history)
    inspection_enabled = _inspection_enabled_for(project_id)
    # Bind the selected provider + model to the default caller so every LLM
    # call in the response loop routes to it. Tests pass ``llm_caller`` to
    # bypass entirely.
    caller = llm_caller or (
        lambda **kwargs: llm_chat(provider=provider, model=model, **kwargs)
    )

    if not inspection_enabled:
        system_prompt = _build_system_prompt(ctx, inspection_enabled=False, mode=mode)
        messages = _build_messages(ctx, message)
        text = caller(system=system_prompt, messages=messages)
        return text, []

    # ---- bounded inspection loop ----
    # Imported here to keep the GENERAL/no-workspace path free of execution
    # imports.
    from execution.inspect import (
        execute_inspect_request,
        format_result_for_llm,
        parse_inspect_request,
    )

    base_system_prompt = _build_system_prompt(ctx, inspection_enabled=True, mode=mode)
    messages = _build_messages(ctx, message)
    inspected_files: list[dict] = []

    for step in range(MAX_INSPECTIONS_PER_TURN + 1):
        # On the final allowed iteration, force a text answer by dropping the
        # inspection guidance from the system prompt. This is what we hand
        # the model after it has exhausted the inspection budget.
        force_text = step == MAX_INSPECTIONS_PER_TURN
        system_prompt = (
            _build_system_prompt(ctx, inspection_enabled=False, mode=mode)
            if force_text
            else base_system_prompt
        )

        raw = caller(system=system_prompt, messages=messages)
        if force_text:
            return raw, inspected_files

        request = parse_inspect_request(raw)
        if request is None:
            # Plain text answer — done.
            return raw, inspected_files

        # Run the inspection, record it, feed the result back into the
        # transcript, and let the model decide whether to keep inspecting or
        # answer.
        result = execute_inspect_request(project_id, request)
        inspected_files.append({
            "tool": result.kind,
            "path": result.path,
            "query": result.query,
            "ok": result.ok,
            "truncated": result.truncated,
            "error": result.error or None,
        })

        # Persist the model's request and the inspection result in the
        # transcript so it can build on what it just learned.
        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": format_result_for_llm(result),
        })

    # The for-loop returns once it hits ``force_text``; this line is
    # unreachable but kept defensively.
    return "", inspected_files


def _inspection_enabled_for(project_id: str) -> bool:
    """Return True iff the project has an initialized execution workspace.

    GENERAL has no execution workspace by definition. Other projects only
    qualify once ``/api/projects/{id}/execution/init`` has been run.
    """
    if project_id == GENERAL_PROJECT_ID:
        return False
    # Lazy import — orchestrator must not import execution at module load
    # time (avoids any chance of a circular import).
    try:
        from execution import get_execution_workspace
    except Exception:  # pragma: no cover - defensive
        return False
    try:
        return get_execution_workspace(project_id) is not None
    except Exception:  # pragma: no cover - defensive
        return False


# ---------------------------------------------------------------------------
# Memory judgment — LLM-driven semantic writeback
# ---------------------------------------------------------------------------

# Phase 6 — structured memory intake. The judge now returns a single reasoned
# object ({should_update, reason, updates[]}) instead of a bare array, so the
# decision carries a justification the UI can surface and is inspectable/testable
# in the same shape as post-run reconciliation.

_INTAKE_RULES = """\
## Rules
1. Only propose updates when there is genuinely new durable knowledge — not just \
conversation chatter. Most turns need NO update; prefer should_update=false.
2. Write clean, structured markdown that fits the file's existing format. Do NOT \
dump raw conversation text. Summarize and structure the knowledge.
3. SOUL.md is read-only. Never include it in updates.
4. Use action "append" to add content to a section, "replace" to overwrite a \
section. For TASK_QUEUE.md use checkbox format ("- [ ]" open, "- [x]" done).
5. Keep each update concise: one clear update per file, no repetition of content \
already present in the snapshot.
6. The "section" field should match an existing ## heading in the file (a new \
heading is created if it doesn't exist).

## Response format
Return ONLY a single JSON object. No markdown fences, no commentary.

Schema:
{
  "should_update": true | false,
  "reason": "one short sentence: what you're recording and why, or why nothing",
  "updates": [
    {"filename": "<one of the writable files>", "section": "## heading name",
     "content": "clean markdown", "action": "append" | "replace"}
  ]
}

When should_update is false, "updates" must be an empty array.
"""

_INTAKE_SYSTEM_PROJECT = (
    "You are the project-memory intake subsystem of Agent OS.\n\n"
    "Examine the latest conversation turn and the current project memory, then "
    "decide whether any project memory file should be updated with new durable "
    "project knowledge.\n\n"
    "## Writable files\n"
    "- PROJECT.md: project definition, vision, scope, target user, tech stack\n"
    "- STATUS.md: current phase, latest milestone, what works, what's next\n"
    "- TASK_QUEUE.md: actionable task tracking (In Progress / Up Next / Done)\n"
    "- DECISIONS.md: important project decisions with rationale\n"
    "- RESEARCH.md: useful findings, external references, technical notes\n\n"
    + _INTAKE_RULES
)

_INTAKE_SYSTEM_GLOBAL = (
    "You are the global-memory intake subsystem of Agent OS.\n\n"
    "Examine the latest conversation turn and the current global memory, then "
    "decide whether any global memory file should be updated with new durable "
    "knowledge.\n\n"
    "## Writable files\n"
    "- USER.md: durable user profile — identity, role, long-term goals\n"
    "- WORKSTYLE.md: collaboration/communication preferences, working habits\n"
    "- MEMORY.md: cross-project notes — recurring lessons, reusable knowledge\n\n"
    + _INTAKE_RULES
)


def _intake_snapshot(ctx: MemoryContext, scope: str) -> list[tuple[str, str]]:
    if scope == "global":
        return [
            ("USER.md", ctx.user),
            ("WORKSTYLE.md", ctx.workstyle),
            ("MEMORY.md", ctx.global_memory),
        ]
    return [
        ("PROJECT.md", ctx.project),
        ("STATUS.md", ctx.status),
        ("TASK_QUEUE.md", ctx.task_queue),
        ("DECISIONS.md", ctx.decisions),
        ("RESEARCH.md", ctx.research),
    ]


def judge_memory_intake(
    scope: str,
    ctx: MemoryContext,
    user_message: str,
    assistant_response: str,
    *,
    intent: Optional[str] = None,
    llm_caller: Optional[Callable] = None,
) -> "memory_engine.MemoryDecision":
    """Structured per-turn memory intake judgment (Phase 6).

    Runs after the assistant response for every meaningful turn, regardless of
    whether the message was a coding request. Returns a reasoned
    :class:`memory_engine.MemoryDecision` (policy-filtered to ``scope``'s writable
    set). ``scope`` is ``"project"`` or ``"global"``. ``intent`` is an optional
    hint from the intent router. Never raises — on any LLM/parse error it returns
    a no-op decision so a chat turn never fails on memory writeback.
    """
    allow = WRITABLE_GLOBAL_FILES if scope == "global" else WRITABLE_PROJECT_FILES
    system = _INTAKE_SYSTEM_GLOBAL if scope == "global" else _INTAKE_SYSTEM_PROJECT
    caller = llm_caller or llm_chat

    snapshot = []
    for name, content in _intake_snapshot(ctx, scope):
        snapshot.append(f"### {name}\n{content if content else '(empty)'}")

    intent_line = f"The intent router classified this turn as: {intent}.\n\n" if intent else ""
    user_prompt = (
        f"## Current {'Global' if scope == 'global' else 'Project'} Memory\n\n"
        + "\n\n".join(snapshot)
        + "\n\n---\n\n"
        f"{intent_line}## Latest Conversation Turn\n\n"
        f"**User:** {user_message}\n\n"
        f"**Assistant:** {assistant_response}\n\n"
        "---\n\n"
        "Decide whether to update memory based on this turn. Return ONLY the JSON object."
    )

    try:
        raw = caller(
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001 — memory writeback must never fail a turn
        log.warning("Memory intake judge LLM call failed: %s", exc)
        return memory_engine.MemoryDecision(should_update=False, reason="", updates=[])

    return _parse_memory_decision(raw, allow)


def _parse_memory_decision(raw: str, allow) -> "memory_engine.MemoryDecision":
    """Parse the intake judge's JSON object into a policy-filtered decision.

    Tolerant of markdown fences and of the legacy bare-array shape (older prompts
    / fallbacks may still emit ``[{...}]``).
    """
    text = (raw or "").strip()
    if "```" in text:
        first = text.find("```")
        last = text.rfind("```")
        if first != last:
            inner = text[first:last]
            nl = inner.find("\n")
            text = inner[nl + 1:].strip() if nl != -1 else ""

    if not text:
        return memory_engine.MemoryDecision(should_update=False, reason="", updates=[])

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Memory intake judge returned invalid JSON: %s", text[:200])
        return memory_engine.MemoryDecision(should_update=False, reason="", updates=[])

    # Accept both the structured object and a legacy bare array of updates.
    if isinstance(parsed, list):
        raw_updates = parsed
        reason = ""
        should_update = bool(raw_updates)
    elif isinstance(parsed, dict):
        raw_updates = parsed.get("updates", []) or []
        reason = str(parsed.get("reason", "")).strip()
        should_update = bool(parsed.get("should_update", False))
        if not isinstance(raw_updates, list):
            raw_updates = []
    else:
        log.warning("Memory intake judge returned unexpected type: %s", type(parsed))
        return memory_engine.MemoryDecision(should_update=False, reason="", updates=[])

    updates: list[memory_engine.MemoryUpdateSpec] = []
    for entry in raw_updates:
        if not isinstance(entry, dict):
            continue
        filename = str(entry.get("filename") or entry.get("file") or "").strip()
        section = str(entry.get("section") or "").strip()
        content = str(entry.get("content") or "")
        action = str(entry.get("action") or "append").strip().lower()
        if filename not in allow:
            continue
        if action not in ("append", "replace"):
            action = "append"
        if not section:
            section = memory_engine.DEFAULT_SECTION.get(filename, "Notes")
        if not content.strip():
            continue
        updates.append(
            memory_engine.MemoryUpdateSpec(
                filename=filename, section=section, content=content,
                action=action, category=str(entry.get("category") or ""),
            )
        )

    if not updates:
        should_update = False
    return memory_engine.MemoryDecision(should_update=should_update, reason=reason, updates=updates)


def apply_memory_decision(
    decision: "memory_engine.MemoryDecision",
    scope: str,
    project_id: Optional[str] = None,
) -> list[dict]:
    """Apply a structured ``MemoryDecision`` through the policy-filtered writers.

    Returns the list of updates actually written (each with ``applied=True``).
    """
    applied: list[dict] = []
    for u in decision.updates:
        if scope == "global":
            ok = apply_global_memory_update(u.filename, u.section, u.content, u.action)
        else:
            ok = apply_memory_update(project_id, u.filename, u.section, u.content, u.action)
        if ok:
            applied.append({**u.to_dict(), "applied": True})
        else:
            log.warning("Memory update rejected: %s/%s", u.filename, u.section)
    return applied


def judge_memory_updates(
    ctx: MemoryContext,
    user_message: str,
    assistant_response: str,
) -> list[dict]:
    """Back-compat wrapper: project-scope intake → bare list of update dicts.

    Used by the ``/extract-updates`` endpoint, which only needs the proposed
    updates (not the reason).
    """
    decision = judge_memory_intake("project", ctx, user_message, assistant_response)
    return [
        {"filename": u.filename, "section": u.section, "content": u.content, "action": u.action}
        for u in decision.updates
    ]


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

    Delegates the actual write to ``memory_engine.apply_update`` (atomic write,
    OSError-guarded, append-dedup, robust section replace). Returns True if the
    update was applied.
    """
    return memory_engine.apply_update(
        PROJECTS_DIR / project_id,
        allow=WRITABLE_PROJECT_FILES,
        filename=filename,
        section=section,
        content=content,
        action=action,
    )


# ---------------------------------------------------------------------------
# Global memory judgment — thin wrapper over the unified intake judge
# ---------------------------------------------------------------------------


def judge_global_memory_updates(
    ctx: MemoryContext,
    user_message: str,
    assistant_response: str,
) -> list[dict]:
    """Back-compat wrapper: global-scope intake → bare list of update dicts."""
    decision = judge_memory_intake("global", ctx, user_message, assistant_response)
    return [
        {"filename": u.filename, "section": u.section, "content": u.content, "action": u.action}
        for u in decision.updates
    ]


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
    Delegates the write to ``memory_engine.apply_update``.
    """
    return memory_engine.apply_update(
        MEMORY_DIR,
        allow=WRITABLE_GLOBAL_FILES,
        filename=filename,
        section=section,
        content=content,
        action=action,
    )
