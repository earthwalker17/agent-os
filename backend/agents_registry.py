"""Agent profile registry — the structured data behind agent discovery (Phase 10).

A *profile* is the user-facing contract of one system agent: what it's called,
how it's triggered (`@` command), what it's for, what it may touch, and which
built-in skills it carries. The Agents browser (`GET /api/agents`) and the
composer's `@`-command autocomplete both render from this registry, so agent
descriptions live in exactly one place instead of scattered UI strings.

Layering rules (deliberate):

- **Top-level leaf module** (pydantic only, like ``memory_engine``). The
  orchestrator folds skill text into chat prompts at turn time, and it must
  not import the ``execution`` package at module load — so profiles link to
  ``execution.roles`` entries **by string** ``role_id``/``mode``, and
  ``tests/test_agents_registry.py`` asserts the cross-registry sync
  (MODE_COMMANDS ↔ profile commands ↔ ROLE_FOR_MODE) instead of an import.
- **Profiles are presentation + contract, not permissions.** Enforcement
  stays where it always was: ``roles.allowed_tools_for`` in the runner loop,
  the sandbox, and the explicit-dispatch endpoints. ``AgentCapabilities`` is
  a coarse, honest badge set for the UI — changing it changes nothing about
  what an agent can actually do.
- **Skills are indexed here, stored as markdown.** Each ``SkillRef`` maps to
  the committed file ``skills/{agent_id}/{skill_id}.md`` (read/written only
  through ``skills_store``, which validates against this registry first).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field


# Agent ids and skill ids double as path segments under skills/ — keep them
# strict slugs so no filesystem path is ever built from a looser string.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

STATUS_ACTIVE = "active"
STATUS_PLANNED = "planned"


class AgentCapabilities(BaseModel):
    """Coarse capability flags rendered as badges (UI-facing, not enforced)."""

    # Never changes project state at all (repo, memory, external services).
    read_only: bool = True
    # Its turns feed the structured memory-intake pipeline (memory_engine).
    writes_memory: bool = False
    # Its work lands in repo/ (always via a dispatched, sandboxed run).
    writes_repo: bool = False
    # May use the bounded research channel (explicit @search grant only).
    searches_web: bool = False
    # Participates in the Phase 7/8 preview->confirm delivery contracts.
    deploys: bool = False
    # Its flow can start a Coding Agent run.
    dispatches_runs: bool = False
    # Mutating / external work needs an explicit user act (command or click).
    requires_confirmation: bool = False

    model_config = {"frozen": True}


class SkillRef(BaseModel):
    """Index entry for one built-in skill (body lives in skills/{agent}/{id}.md)."""

    id: str
    title: str
    # One line, shown in skill lists.
    description: str

    model_config = {"frozen": True}


class AgentProfile(BaseModel):
    """One system agent as shown in the Agents browser / composer menu."""

    id: str
    name: str
    # Chat trigger, e.g. "@plan". Empty = contract-driven (no chat command).
    command: str = ""
    aliases: tuple[str, ...] = ()
    # Orchestration mode this command sets (matches MODE_COMMANDS values).
    mode: str = ""
    # execution.roles registry id this profile corresponds to (by string —
    # see module docstring for why this is not an import).
    role_id: str = ""
    status: str = STATUS_ACTIVE
    introduction: str = ""
    use_cases: tuple[str, ...] = ()
    responsibilities: tuple[str, ...] = ()
    # Human-readable tool *categories*, not tool names ("memory (read)", ...).
    tool_categories: tuple[str, ...] = ()
    # One or two sentences: what this agent may never do without the user.
    approval_boundary: str = ""
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    skills: tuple[SkillRef, ...] = ()

    model_config = {"frozen": True}


# ---------- the registry ----------

AGENTS: tuple[AgentProfile, ...] = (
    AgentProfile(
        id="planner",
        name="Planner / PM Agent",
        command="@plan",
        mode="plan",
        role_id="planner",
        introduction=(
            "Turns a request into a sequenced, dependency-aware plan before any "
            "code is written. Thinks in bounded tasks the Coding Agent can take on."
        ),
        use_cases=(
            "Breaking a feature into ordered, bounded tasks",
            "Deciding what to build first and why",
            "Surfacing risks and dependencies before dispatching work",
        ),
        responsibilities=(
            "Decompose the request into concrete, sequenced steps",
            "Call out dependencies, risks, and open questions",
            "Recommend what to hand to the Coding Agent (without dispatching it)",
        ),
        tool_categories=("project memory (read)", "bounded repo inspection"),
        approval_boundary=(
            "Read-only chat mode: shapes this turn's response, never dispatches "
            "a run or edits anything."
        ),
        capabilities=AgentCapabilities(read_only=True),
        skills=(
            SkillRef(
                id="task-breakdown-checklist",
                title="Task Breakdown Checklist",
                description="How to split a request into bounded, dispatchable tasks.",
            ),
            SkillRef(
                id="plan-quality-rubric",
                title="Plan Quality Rubric",
                description="What a plan must cover before it is worth executing.",
            ),
        ),
    ),
    AgentProfile(
        id="designer",
        name="Design Agent",
        command="@design",
        mode="design",
        role_id="designer",
        introduction=(
            "Explores the shape of a solution — product structure, architecture, "
            "interaction — and weighs tradeoffs before implementation starts."
        ),
        use_cases=(
            "Choosing between product or architecture directions",
            "Reviewing an interaction or UI flow for coherence",
            "Sketching the structure of a new feature",
        ),
        responsibilities=(
            "Lay out solution shapes with their tradeoffs",
            "Recommend one structure and say why",
            "Stay at design level — implementation goes to the Coding Agent",
        ),
        tool_categories=("project memory (read)", "bounded repo inspection"),
        approval_boundary=(
            "Read-only chat mode: proposes designs, never writes code or files."
        ),
        capabilities=AgentCapabilities(read_only=True),
        skills=(
            SkillRef(
                id="design-tradeoff-worksheet",
                title="Design Tradeoff Worksheet",
                description="A structured way to compare candidate designs honestly.",
            ),
            SkillRef(
                id="interface-consistency-checklist",
                title="Interface Consistency Checklist",
                description="Checks that keep product structure and UI coherent.",
            ),
        ),
    ),
    AgentProfile(
        id="debugger",
        name="Debug / Recovery Agent",
        command="@debug",
        mode="debug",
        role_id="debugger",
        introduction=(
            "Diagnoses what went wrong — a non-green run, a reported bug, a "
            "confusing failure — and proposes one bounded next step. Sees a "
            "compact summary of the latest non-green run automatically."
        ),
        use_cases=(
            "Understanding why a run ended partial / failed / blocked",
            "Triaging a bug report against the actual code",
            "Choosing the next bounded repair step",
        ),
        responsibilities=(
            "Collect evidence before concluding (runs, files, errors)",
            "Name the most likely cause and the fastest way to confirm it",
            "Propose one bounded fix the user can dispatch",
        ),
        tool_categories=(
            "project memory (read)",
            "bounded repo inspection",
            "run summaries (read)",
        ),
        approval_boundary=(
            "Read-only chat mode: diagnoses and proposes; repairs run only as "
            "user-dispatched (or user-budgeted recovery) runs."
        ),
        capabilities=AgentCapabilities(read_only=True),
        skills=(
            SkillRef(
                id="failure-triage-method",
                title="Failure Triage Method",
                description="Ordered steps from symptom to confirmed cause.",
            ),
            SkillRef(
                id="minimal-repro-checklist",
                title="Minimal-Repro Checklist",
                description="How to shrink a failure until the cause is visible.",
            ),
        ),
    ),
    AgentProfile(
        id="reviewer",
        name="Review Agent",
        command="@review",
        mode="review",
        role_id="reviewer",
        introduction=(
            "Examines what exists — code, runs, project state — and reports "
            "concrete findings. Also runs read-only inside team runs as the "
            "review task unit."
        ),
        use_cases=(
            "Reviewing a finished run before committing or shipping",
            "Assessing quality and risk in part of the codebase",
            "A retrospective pass over recent project work",
        ),
        responsibilities=(
            "Ground every finding in code actually read (never guess)",
            "Separate real defects from taste; say why each matters",
            "Summarize with concrete, actionable recommendations",
        ),
        tool_categories=(
            "project memory (read)",
            "bounded repo inspection",
            "read-only run tools (in team runs)",
        ),
        approval_boundary=(
            "Read-only everywhere: reports findings, never fixes anything itself."
        ),
        capabilities=AgentCapabilities(read_only=True),
        skills=(
            SkillRef(
                id="code-review-rubric",
                title="Code Review Rubric",
                description="What to check, in what order, and what blocks acceptance.",
            ),
            SkillRef(
                id="risk-hotspot-checklist",
                title="Risk Hotspot Checklist",
                description="Where defects cluster: boundaries, state, concurrency, security.",
            ),
        ),
    ),
    AgentProfile(
        id="inspector",
        name="Inspector Agent",
        command="@inspect",
        mode="inspect",
        role_id="inspector",
        introduction=(
            "Reads specific files and structures through the bounded inspection "
            "channel and answers precisely from what it actually read. Also runs "
            "read-only inside team runs as the fact-gathering task unit."
        ),
        use_cases=(
            "Answering \"what does this file / module actually do?\"",
            "Mapping structure before planning a change",
            "Verifying an assumption against the real code",
        ),
        responsibilities=(
            "Read only what the question needs (bounded, capped)",
            "Report facts with file references, never invented content",
            "State clearly what was NOT read",
        ),
        tool_categories=("bounded repo inspection",),
        approval_boundary=(
            "Read-only chat mode with hard caps (max 3 reads per turn); never "
            "auto-injects repo contents."
        ),
        capabilities=AgentCapabilities(read_only=True),
        skills=(
            SkillRef(
                id="codebase-recon-method",
                title="Codebase Recon Method",
                description="An efficient reading order for unfamiliar code.",
            ),
            SkillRef(
                id="evidence-notes-template",
                title="Evidence Notes Template",
                description="How to record findings so later turns can build on them.",
            ),
        ),
    ),
    AgentProfile(
        id="memory_steward",
        name="Memory Steward",
        command="@memory",
        mode="memory",
        role_id="memory_steward",
        introduction=(
            "Captures durable project knowledge into the structured markdown "
            "memory files. The steward decides what is worth keeping; the "
            "memory engine does the actual writing under policy."
        ),
        use_cases=(
            "Recording a decision and its rationale",
            "Updating project status after a milestone",
            "Curating noisy notes into clean memory entries",
        ),
        responsibilities=(
            "Confirm what was understood before it is recorded",
            "Keep entries concise, structured, and human-readable",
            "Never touch SOUL.md or files outside the writable policy sets",
        ),
        tool_categories=("project memory (read)", "memory intake pipeline"),
        approval_boundary=(
            "Writes reach disk only through the policy-filtered memory engine "
            "(SOUL.md and non-writable files are excluded unconditionally)."
        ),
        capabilities=AgentCapabilities(read_only=False, writes_memory=True),
        skills=(
            SkillRef(
                id="memory-intake-rubric",
                title="Memory Intake Rubric",
                description="What deserves memory, what stays in chat, and why.",
            ),
            SkillRef(
                id="memory-hygiene-checklist",
                title="Memory Hygiene Checklist",
                description="Keeping memory files readable as they grow.",
            ),
        ),
    ),
    AgentProfile(
        id="researcher",
        name="Research Agent",
        command="@search",
        aliases=("@research",),
        mode="research",
        role_id="researcher",
        introduction=(
            "Gathers bounded, cited external references through the research "
            "channel: allowlisted web search plus fetching of user-provided "
            "URLs, distilled into concise sourced notes (never raw page dumps)."
        ),
        use_cases=(
            "Finding current best practice for a concrete question",
            "Reading documentation the user linked",
            "Comparing options with sources before a decision",
        ),
        responsibilities=(
            "Search and fetch only within the granted, bounded budget",
            "Cite every claim taken from an external source",
            "Distill findings so they can be recorded in RESEARCH.md",
        ),
        tool_categories=(
            "web search (allowlisted, keyed)",
            "URL fetch (user-provided or allowlisted, SSRF-guarded)",
            "project memory (read)",
        ),
        approval_boundary=(
            "Network access exists only inside a turn the user explicitly "
            "started with @search / @research (or explicitly confirmed); "
            "inferred intent never reaches the network, and no secret, memory, "
            "or repo content is ever sent to a website."
        ),
        capabilities=AgentCapabilities(
            read_only=False,
            writes_memory=True,
            searches_web=True,
            requires_confirmation=True,
        ),
        skills=(
            SkillRef(
                id="source-vetting-rubric",
                title="Source Vetting Rubric",
                description="Which sources to trust, and how much, for what.",
            ),
            SkillRef(
                id="cited-research-notes-template",
                title="Cited Research Notes Template",
                description="The shape of a distilled, sourced research note.",
            ),
        ),
    ),
    AgentProfile(
        id="coder",
        name="Coding Agent",
        command="@code",
        role_id="coder",
        introduction=(
            "The hands: a bounded executor that builds and edits code inside "
            "the project's sandboxed workspace, with planning, verification, "
            "and repair built into every run."
        ),
        use_cases=(
            "Building a feature or scaffolding an app",
            "Applying a bounded fix a chat agent proposed",
            "Any change that must land in repo/ files",
        ),
        responsibilities=(
            "Execute one task card per run, honestly and verifiably",
            "Keep every file and command inside the project sandbox",
            "Report what changed, what ran, and what blocked",
        ),
        tool_categories=("sandboxed file tools", "sandboxed shell (allow-listed)"),
        approval_boundary=(
            "Runs only on an explicit act: `@code` is itself the dispatch, and "
            "inferred coding intent only ever creates a plan the user must "
            "confirm. Never edits project memory."
        ),
        capabilities=AgentCapabilities(
            read_only=False,
            writes_repo=True,
            dispatches_runs=True,
        ),
        skills=(
            SkillRef(
                id="task-card-template",
                title="Task Card Template",
                description="What a well-formed task card gives the Coding Agent.",
            ),
            SkillRef(
                id="definition-of-done-checklist",
                title="Definition-of-Done Checklist",
                description="When a coding task is actually finished.",
            ),
        ),
    ),
    AgentProfile(
        id="deploy_ops",
        name="Deploy / Ops Agent",
        # Contract-driven: no chat command — Git/deploy/migration/payment
        # actions run through the preview->confirm panels on a run.
        command="",
        role_id="",
        introduction=(
            "Delivers finished work to the outside world — Git commits and "
            "pushes, GitHub PRs, Vercel deploys, Supabase migrations, Stripe "
            "test setup — exclusively through preview→confirm contracts."
        ),
        use_cases=(
            "Committing and pushing a verified run",
            "Deploying to Vercel and rolling back if needed",
            "Applying a Supabase migration or provisioning Stripe test mode",
        ),
        responsibilities=(
            "Show exactly what an action will do before it runs",
            "Keep every credential out of logs, prompts, and artifacts",
            "Record delivery facts in the OPS.md ledger",
        ),
        tool_categories=(
            "Git executor (audited, allow-listed)",
            "GitHub / Vercel / Supabase / Stripe connectors (contract-first)",
        ),
        approval_boundary=(
            "Every external or destructive action is a two-phase contract: "
            "preview first, and nothing executes without an explicit confirm "
            "click. No inferred-intent delivery, ever."
        ),
        capabilities=AgentCapabilities(
            read_only=False,
            deploys=True,
            requires_confirmation=True,
        ),
        skills=(
            SkillRef(
                id="deploy-preflight-checklist",
                title="Deploy Preflight Checklist",
                description="What to verify before anything leaves the machine.",
            ),
            SkillRef(
                id="rollback-notes-template",
                title="Rollback Notes Template",
                description="Recording enough to undo a delivery calmly.",
            ),
        ),
    ),
    AgentProfile(
        id="growth_launch",
        name="Growth / Launch Agent",
        status=STATUS_PLANNED,
        introduction=(
            "Planned (Phase 12 — Launch & Growth Studio): launch kits, release "
            "notes, diagrams, and social drafts generated from real project "
            "artifacts. Not available yet."
        ),
        approval_boundary="Not yet available; nothing is ever auto-published.",
        capabilities=AgentCapabilities(read_only=True),
    ),
)

_AGENTS_BY_ID: dict[str, AgentProfile] = {a.id: a for a in AGENTS}


# ---------- lookups ----------


def get_agent(agent_id: str | None) -> AgentProfile | None:
    """Strict lookup — unknown ids return ``None`` (no coder-style fallback:
    registry consumers must not silently show the wrong agent)."""
    if not agent_id:
        return None
    return _AGENTS_BY_ID.get(str(agent_id).strip().lower())


def skill_ref(agent_id: str, skill_id: str) -> SkillRef | None:
    """The registry-membership gate ``skills_store`` validates against before
    any path is built from ``agent_id``/``skill_id``."""
    agent = get_agent(agent_id)
    if agent is None:
        return None
    sid = str(skill_id or "").strip().lower()
    for ref in agent.skills:
        if ref.id == sid:
            return ref
    return None


def agents_for_ui() -> list[dict]:
    """JSON-ready profile list for ``GET /api/agents`` (tuples become lists)."""
    return [a.model_dump(mode="json") for a in AGENTS]


def command_entries() -> list[dict]:
    """Composer autocomplete rows: active profiles with a chat command, with
    alias commands expanded into their own rows."""
    entries: list[dict] = []
    for agent in AGENTS:
        if agent.status != STATUS_ACTIVE or not agent.command:
            continue
        entries.append({"command": agent.command, "agent_id": agent.id, "is_alias": False})
        for alias in agent.aliases:
            entries.append({"command": alias, "agent_id": agent.id, "is_alias": True})
    return entries


def is_valid_slug(value: str) -> bool:
    """Shared slug rule for agent/skill ids (they double as path segments)."""
    return bool(_SLUG_RE.match(value or ""))
