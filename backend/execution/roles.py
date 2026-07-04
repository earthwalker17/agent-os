"""Agent role registry for the team runtime (Phase 9).

A *role* is a contract, not a process: a system-prompt overlay, a tool
permission set, and an expected output shape for one specialized agent inside
a run. The registry is deliberately small — Phase 9 ships the minimum solid
set needed for team execution (coder / reviewer / inspector) plus the system
stages (integrator / verifier) and the chat-facing role contracts that the
existing ``@`` modes map onto. Future roles (research / deploy / release /
launch) get added here when a concrete consumer exists, not speculatively.

Three kinds of entry:

- **Execution roles** (``executes_in_runs=True``) — assignable to an
  :class:`~.models.ExecutionTask` by the planner and executed by the runner's
  per-task tool loop. Their ``allowed_tools`` set is ENFORCED in the loop
  (bounced like the planning loop's read-only gate), not prompt-only.
- **System stages** (``executes_in_runs=False``, no tools) — deterministic
  runner stages (integration, global verification) that appear in the team
  trace under a stable role id but never run an LLM tool loop.
- **Chat-facing roles** (``executes_in_runs=False``) — the Main-Agent ``@``
  modes' role contracts (`@plan` → planner, `@debug` → debugger, …). They
  document the mode ↔ role correspondence for traceability; the chat-side
  mode prompts themselves stay in ``orchestrator._MODE_GUIDANCE`` (the
  orchestrator must not import the execution layer at module load).

Pure data + lookups — no filesystem, no LLM, no imports from the runner — so
it is trivially unit-testable and safe to import from anywhere in the
execution layer.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# Tool names as dispatched by the runner (see runner._dispatch_tool).
_ALL_AGENT_TOOLS = frozenset(
    {"list_files", "read_file", "write_file", "append_file", "search_files", "run_shell"}
)
_READ_ONLY_TOOLS = frozenset({"list_files", "read_file", "search_files"})
# Parallel coder tasks run inside an isolated patch workspace where run_shell
# is unavailable (verification runs globally after integration); the runtime
# blocks it regardless, but the role contract documents the reduced set.
_PATCH_WRITE_TOOLS = frozenset(
    {"list_files", "read_file", "write_file", "append_file", "search_files"}
)


class AgentRole(BaseModel):
    """One role contract: prompt overlay + permissions + expected output."""

    id: str
    title: str
    # One-line summary for the plan prompt / UI chips.
    summary: str = ""
    # System-prompt overlay block appended to the Coding Agent system prompt
    # for task units running under this role. Empty for the default coder so
    # the existing prompt stays byte-identical.
    prompt: str = ""
    # Read-only roles may never mutate the workspace; enforced in the loop.
    read_only: bool = False
    # Tools this role may call in its task-unit loop (enforced, not advisory).
    allowed_tools: frozenset[str] = Field(default=_ALL_AGENT_TOOLS)
    # Whether the planner may assign this role to an ExecutionTask.
    executes_in_runs: bool = True
    # The chat `@` mode this role corresponds to, if any (traceability only).
    mode: str = ""

    model_config = {"frozen": True}


_REVIEWER_PROMPT = """\
You are acting as the REVIEW AGENT for this task. You may only READ the
workspace (list_files / read_file / search_files) — you cannot write files or
run commands. Your deliverable is your `final` action:
- `summary`: a concise, concrete review — what you checked, what is correct,
  and every real problem you found (file + what is wrong + why it matters).
- `blockers`: one entry per genuine defect that should block acceptance.
Leave `files_changed` and `commands_run` empty. Do not propose to fix things
yourself — later tasks or the user act on your findings."""

_INSPECTOR_PROMPT = """\
You are acting as the INSPECTOR AGENT for this task. You may only READ the
workspace (list_files / read_file / search_files) — you cannot write files or
run commands. Your deliverable is your `final` action's `summary`: the
concrete facts you gathered (file layout, key interfaces, existing patterns,
constraints), stated precisely so later tasks can build on them without
re-reading everything. Leave `files_changed` and `commands_run` empty."""

_PATCH_CODER_PROMPT = """\
You are working inside an ISOLATED PATCH WORKSPACE: your reads see the shared
repo, but every file you write lands in your own private overlay, which Agent
OS integrates into the shared repo after this task (conflicts are detected —
do not touch files that belong to other tasks in the plan). `run_shell` is
unavailable here: do not try to install dependencies, build, or run tests —
global verification runs automatically after integration. Spend your steps
writing complete files for THIS task only."""


# ---------- the registry ----------

_ROLES: dict[str, AgentRole] = {
    role.id: role
    for role in (
        # -- execution roles (planner-assignable) --
        AgentRole(
            id="coder",
            title="Coding Agent",
            summary="Writes code and files for one task unit.",
            prompt="",  # empty overlay -> existing system prompt byte-identical
            read_only=False,
            allowed_tools=_ALL_AGENT_TOOLS,
            executes_in_runs=True,
        ),
        AgentRole(
            id="reviewer",
            title="Review Agent",
            summary="Reads the workspace and reports defects; writes nothing.",
            prompt=_REVIEWER_PROMPT,
            read_only=True,
            allowed_tools=_READ_ONLY_TOOLS,
            executes_in_runs=True,
            mode="review",
        ),
        AgentRole(
            id="inspector",
            title="Inspector Agent",
            summary="Gathers facts about the workspace read-only for later tasks.",
            prompt=_INSPECTOR_PROMPT,
            read_only=True,
            allowed_tools=_READ_ONLY_TOOLS,
            executes_in_runs=True,
            mode="inspect",
        ),
        # -- system stages (deterministic runner stages, trace labels only) --
        AgentRole(
            id="integrator",
            title="Integration Agent",
            summary="Deterministic merge of patch workspaces into the shared repo.",
            read_only=False,
            allowed_tools=frozenset(),
            executes_in_runs=False,
        ),
        AgentRole(
            id="verifier",
            title="Verification Agent",
            summary="Global verification gate over the integrated result.",
            read_only=True,
            allowed_tools=frozenset(),
            executes_in_runs=False,
        ),
        # -- chat-facing role contracts (the `@` modes; traceability only) --
        AgentRole(
            id="planner",
            title="Planner / PM Agent",
            summary="Turns a request into a sequenced, dependency-aware plan.",
            read_only=True,
            allowed_tools=frozenset(),
            executes_in_runs=False,
            mode="plan",
        ),
        AgentRole(
            id="designer",
            title="Design Agent",
            summary="Product / architecture / UX shaping at design level.",
            read_only=True,
            allowed_tools=frozenset(),
            executes_in_runs=False,
            mode="design",
        ),
        AgentRole(
            id="debugger",
            title="Debug / Recovery Agent",
            summary="Diagnoses a non-green outcome and proposes one bounded step.",
            read_only=True,
            allowed_tools=frozenset(),
            executes_in_runs=False,
            mode="debug",
        ),
        AgentRole(
            id="memory_steward",
            title="Memory Steward",
            summary="Curates structured markdown memory through the memory engine.",
            read_only=True,
            allowed_tools=frozenset(),
            executes_in_runs=False,
            mode="memory",
        ),
    )
}

DEFAULT_ROLE = "coder"

# Role ids the planner may assign to a task (validated in plan parsing).
EXECUTION_ROLES = frozenset(r.id for r in _ROLES.values() if r.executes_in_runs)

# Chat `@` mode -> role id (documents the Phase 9 mode ↔ role mapping).
ROLE_FOR_MODE = {r.mode: r.id for r in _ROLES.values() if r.mode}


def get_role(role_id: str | None) -> AgentRole:
    """Look up a role by id; unknown / empty ids fall back to the coder.

    The fallback keeps the runner robust against a planner emitting a role
    the registry doesn't know — the task still executes, as a plain coder.
    """
    if not role_id:
        return _ROLES[DEFAULT_ROLE]
    return _ROLES.get(str(role_id).strip().lower(), _ROLES[DEFAULT_ROLE])


def normalize_role_id(role_id: str | None) -> str:
    """Map an arbitrary planner-emitted role string onto a known execution
    role id (unknown -> ``coder``). Used by plan parsing so plan.json always
    carries a registry-known role."""
    rid = str(role_id or "").strip().lower()
    return rid if rid in EXECUTION_ROLES else DEFAULT_ROLE


def patch_coder_prompt() -> str:
    """The isolation overlay appended for coder tasks running in a patch
    workspace (parallel write tasks). Kept here so the contract lives with
    the role definitions."""
    return _PATCH_CODER_PROMPT


def allowed_tools_for(role_id: str, *, in_patch_workspace: bool = False) -> frozenset[str]:
    """The enforced tool set for a task unit running under ``role_id``.

    A coder inside an isolated patch workspace loses ``run_shell`` (the
    runtime blocks it too — this keeps loop-level enforcement and the runtime
    guard in agreement).
    """
    role = get_role(role_id)
    if role.read_only:
        return _READ_ONLY_TOOLS
    if in_patch_workspace:
        return _PATCH_WRITE_TOOLS
    return role.allowed_tools
