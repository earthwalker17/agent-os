from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

import os
import shutil
import stat
import re
import time
import mimetypes

import memory_engine

# Load .env before anything that needs ANTHROPIC_API_KEY
load_dotenv(Path(__file__).resolve().parent / ".env")

from database import (
    init_db, create_conversation, list_conversations, get_conversation,
    list_messages, add_message, update_conversation_title, delete_conversation,
    delete_conversations_for_project, rename_project_conversations,
    create_pending_execution, get_pending_execution,
    update_pending_execution_plan, mark_pending_execution_dispatched,
    claim_pending_execution, revert_pending_execution_to_pending,
    reconcile_stuck_pending_executions,
)
from orchestrator import (
    orchestrate, load_memory, judge_memory_updates, apply_memory_updates, apply_memory_update,
    judge_global_memory_updates, apply_global_memory_updates, apply_global_memory_update,
    judge_memory_intake, apply_memory_decision,
    load_global_memory, GENERAL_PROJECT_ID, WRITABLE_GLOBAL_FILES,
)
from execution import (
    init_execution_workspace,
    get_execution_workspace,
    get_project_execution_dir,
    read_task_state,
    update_task_state,
    ToolRuntime,
    get_default_manager,
    shutdown_default_manager,
    is_code_delegation,
    handle_code_delegation,
    parse_mode_command,
    GENERAL_REJECTION_MESSAGE,
    judge_delegation,
    DECISION_DISPATCH,
    DECISION_MEMORY_ONLY,
    PendingExecutionView,
    serialize_pending,
    revise_pending_plan,
    render_pending_chat_body,
    render_revised_chat_body,
    derive_title_from_card,
    STATUS_PENDING,
    STATUS_DISPATCHED,
)
from execution.tool_models import (
    ListFilesRequest,
    ReadFileRequest,
    WriteFileRequest,
    AppendFileRequest,
    SearchFilesRequest,
    RunShellRequest,
)
from execution.models import TaskSpec, RunRecord, RunStatus, TaskStatus
from execution import run_store
from execution.browser_verification import (
    run_ui_browser_verification,
    apply_ui_browser_verification_to_record,
    DEFAULT_DEV_COMMAND,
    DEFAULT_DEV_URL,
)
from execution.visual_judge import run_visual_review
from execution.recovery import assess_run
from execution.recovery_matrix import classify_failure, contract_for
from execution import preview
from execution import git_ops, github_connector
from execution import app_env
from execution import vercel_connector, ops_ledger, supabase_connector, stripe_connector
from execution.inspect import (
    list_repo_files,
    read_repo_file,
    search_repo_files,
)
from uploads import (
    save_chat_attachment,
    resolve_chat_attachment,
    UploadError,
    ALLOWED_EXTENSIONS,
)
import providers
import credentials
import agents_registry
import skills_store

app = FastAPI(title="Agent OS Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PROJECTS_DIR = Path(__file__).resolve().parent.parent / "projects"

# Phase 10.2 — TASK_QUEUE.md merged into STATUS.md (## Task Queue); LESSONS.md added.
MEMORY_FILES = ["PROJECT.md", "STATUS.md", "DECISIONS.md", "RESEARCH.md", "LESSONS.md"]


@app.on_event("startup")
def startup():
    init_db()
    # Sweep any run.json still marked `running` from a prior process — they
    # belong to a backend instance that exited mid-loop and would otherwise
    # stay stuck forever. Best-effort: never block startup.
    try:
        swept = run_store.sweep_stuck_runs()
        if swept:
            print(f"[startup] marked {len(swept)} stuck run(s) as failed: {swept}")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] sweep_stuck_runs failed: {type(exc).__name__}: {exc}")

    # Companion to the above: a crash during the post-status verify/browser tail
    # leaves a run with a TERMINAL status but a lingering local transient state
    # (e.g. verification_state='verifying'), which sweep_stuck_runs skips (it only
    # touches `running` runs). Without this, the UI poll gates spin forever on a
    # finished run. Clears only the local gates — never deploy/external (the
    # reconciler below owns those). Best-effort.
    try:
        cleared = run_store.sweep_terminal_transient_states()
        if cleared:
            print(f"[startup] cleared leaked transient state on {len(cleared)} run(s): {cleared}")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] sweep_terminal_transient_states failed: {type(exc).__name__}: {exc}")

    # A pending plan stranded mid-confirm (process died between the atomic claim
    # and the dispatched-mark) is stuck in the intermediate 'dispatching' state,
    # where its "OK, run this" button dead-ends. Revert those to 'pending' so the
    # plan is confirmable again (any run that WAS dispatched is swept to failed
    # above, so re-confirming is a clean retry). Best-effort.
    try:
        reverted = reconcile_stuck_pending_executions()
        if reverted:
            print(f"[startup] reverted {reverted} stranded pending plan(s) to 'pending'")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] reconcile_stuck_pending_executions failed: {type(exc).__name__}: {exc}")

    # Phase 8 — reconcile runs left mid external action (deploy/migration) by a
    # prior process: query the provider for the true state, then clear the transient
    # sub-status. NEVER auto-retries an external action that may have partially
    # applied — it records a "verify remote state" blocker instead. Best-effort.
    try:
        recon = reconcile_stuck_external_actions()
        if recon:
            print(f"[startup] reconciled {len(recon)} stuck external action(s): {recon}")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] reconcile_stuck_external_actions failed: {type(exc).__name__}: {exc}")

    # Phase 6 — one-time additive migration: backfill the canonical memory
    # sections for projects created before the structured-memory upgrade so the
    # intake judge + reconciliation always have stable sections to target. Pure
    # backfill (never rewrites existing content); best-effort, never blocks.
    try:
        if PROJECTS_DIR.exists():
            for pdir in PROJECTS_DIR.iterdir():
                if not pdir.is_dir():
                    continue
                name = pdir.name
                pmd = pdir / "PROJECT.md"
                if pmd.exists():
                    first = pmd.read_text(encoding="utf-8").split("\n", 1)[0]
                    if first.startswith("# "):
                        name = first[2:].strip() or name
                memory_engine.ensure_memory_scaffold(pdir, name)
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] memory scaffold migration failed: {type(exc).__name__}: {exc}")


@app.on_event("shutdown")
def shutdown():
    # Best-effort: tear down the background run executor so the process exits
    # cleanly. In-flight runs are not awaited — their artifacts may end in an
    # inconsistent state if the server is killed mid-run.
    shutdown_default_manager(wait=False)
    # Task 06.2D — stop any managed preview dev servers so we don't orphan
    # long-lived node processes when the backend exits.
    try:
        preview.shutdown_all_previews()
    except Exception as exc:  # noqa: BLE001
        print(f"[shutdown] shutdown_all_previews failed: {type(exc).__name__}: {exc}")


# --- Project endpoints ---

@app.get("/api/projects")
def api_list_projects():
    if not PROJECTS_DIR.exists():
        return []
    projects = sorted(d.name for d in PROJECTS_DIR.iterdir() if d.is_dir())
    return projects


@app.get("/api/projects/{project_id}/context")
def api_get_project_context(project_id: str):
    project_path = PROJECTS_DIR / project_id
    if not project_path.exists() or not project_path.is_dir():
        raise HTTPException(status_code=404, detail="Project not found")

    context = {}
    for filename in MEMORY_FILES:
        filepath = project_path / filename
        if filepath.exists():
            context[filename] = filepath.read_text(encoding="utf-8")
        else:
            context[filename] = ""
    return context


DEFAULT_MEMORY_CONTENT = {
    "PROJECT.md": "# {name}\n\n## Vision\n(describe the project vision here)\n\n## Scope\n- (list key scope items)\n\n## Target User\n(who is this for?)\n\n## Tech Stack\n- (list technologies)\n",
    # Phase 10.2 — the task board is now a ## Task Queue section inside STATUS.md
    # (### Completed / ### In Progress / ### Next), no longer a standalone file.
    "STATUS.md": "# Status: {name}\n\n## Current Phase\nPlanning\n\n## Latest Milestone\nProject created\n\n## What Works\n- Project folder initialized\n\n## Next Up\n- Define project scope and goals\n\n## Task Queue\n\n### Completed\n\n- [x] Project created\n\n### In Progress\n\n- [ ] Define project scope and requirements\n\n### Next\n\n- [ ] Set up initial project structure\n",
    "DECISIONS.md": "# Decisions: {name}\n\n## Decisions\n(record important project decisions and their rationale here)\n",
    "RESEARCH.md": "# Research: {name}\n\n## Findings\n(record research findings, external references, and technical notes here)\n",
    "LESSONS.md": "# Lessons: {name}\n\n## Lessons\n(durable project lessons from builds, failures, fixes, reviews, deployments, and decisions are captured here)\n",
}


def _validate_project_name(name: str) -> str | None:
    """Return error message if name is invalid, None if valid."""
    if not name or not name.strip():
        return "Project name cannot be empty"
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9 _-]*$', name.strip()):
        return "Project name must start with a letter or number and contain only letters, numbers, spaces, hyphens, or underscores"
    if len(name.strip()) > 60:
        return "Project name must be 60 characters or less"
    return None


class CreateProjectRequest(BaseModel):
    name: str


@app.post("/api/projects")
def api_create_project(req: CreateProjectRequest):
    error = _validate_project_name(req.name)
    if error:
        raise HTTPException(status_code=400, detail=error)

    project_id = req.name.strip().replace(" ", "-").lower()
    project_path = PROJECTS_DIR / project_id
    if project_path.exists():
        raise HTTPException(status_code=409, detail="A project with this name already exists")

    project_path.mkdir(parents=True)
    display_name = req.name.strip()
    for filename, template in DEFAULT_MEMORY_CONTENT.items():
        content = template.replace("{name}", display_name)
        (project_path / filename).write_text(content, encoding="utf-8")

    # Phase 6 — backfill any canonical memory section the templates don't cover
    # (idempotent; templates already include them, so normally a no-op).
    memory_engine.ensure_memory_scaffold(project_path, display_name)

    return {"project_id": project_id, "name": display_name}


class RenameProjectRequest(BaseModel):
    new_name: str


@app.patch("/api/projects/{project_id}")
def api_rename_project(project_id: str, req: RenameProjectRequest):
    error = _validate_project_name(req.new_name)
    if error:
        raise HTTPException(status_code=400, detail=error)

    project_path = PROJECTS_DIR / project_id
    if not project_path.exists() or not project_path.is_dir():
        raise HTTPException(status_code=404, detail="Project not found")

    new_id = req.new_name.strip().replace(" ", "-").lower()
    new_path = PROJECTS_DIR / new_id

    if new_id != project_id and new_path.exists():
        raise HTTPException(status_code=409, detail="A project with this name already exists")

    if new_id != project_id:
        project_path.rename(new_path)
        rename_project_conversations(project_id, new_id)

    # Update the title in PROJECT.md
    project_md = new_path / "PROJECT.md"
    if project_md.exists():
        content = project_md.read_text(encoding="utf-8")
        lines = content.split("\n")
        if lines and lines[0].startswith("# "):
            lines[0] = f"# {req.new_name.strip()}"
            project_md.write_text("\n".join(lines), encoding="utf-8")

    return {"project_id": new_id, "name": req.new_name.strip()}


def _force_writable_and_retry(func, path, _exc_info):
    """``shutil.rmtree`` callback that strips read-only attrs and retries.

    On Windows, ``node_modules/`` typically contains read-only pack files
    and bin entries that vanilla ``rmtree`` refuses to delete. The
    callback chmod's the offender to writable and re-invokes the failing
    operation, which clears the common failure case without swallowing
    real errors (truly undeletable files re-raise on the retry).
    """
    try:
        os.chmod(path, stat.S_IWRITE)
    except Exception:  # noqa: BLE001
        pass
    func(path)


def _rmtree_force(path: Path) -> None:
    """Remove ``path`` recursively, handling Windows read-only files.

    Python 3.12+ deprecated ``onerror`` in favor of ``onexc``; we use
    ``onexc`` when available and fall back to ``onerror`` otherwise so
    the same code works on older interpreters.
    """
    if not path.exists():
        return
    try:
        shutil.rmtree(path, onexc=_force_writable_and_retry)  # type: ignore[call-arg]
    except TypeError:
        shutil.rmtree(path, onerror=_force_writable_and_retry)


@app.get("/api/projects/{project_id}/workspace-status")
def api_project_workspace_status(project_id: str):
    """Report whether an execution workspace exists on disk for this project.

    Used by the delete-project confirmation modal to decide whether to offer
    the "Delete its workspace too" checkbox — there's no point showing it for
    a backend-only project that never had a workspace materialized.
    """
    _require_project(project_id)
    return {"exists": get_project_execution_dir(project_id).exists()}


@app.delete("/api/projects/{project_id}")
def api_delete_project(project_id: str, delete_workspace: bool = False):
    project_path = PROJECTS_DIR / project_id
    if not project_path.exists() or not project_path.is_dir():
        raise HTTPException(status_code=404, detail="Project not found")

    # Order: tear down DB rows (FK-aware via delete_conversations_for_project)
    # first, then the memory dir, then (optionally) the execution workspace.
    # The workspace is removed last because it's the most likely to fail on
    # Windows when a dev server, file watcher, or editor still has a handle
    # open — we want the DB and memory state cleaned even if the workspace
    # removal raises.
    delete_conversations_for_project(project_id)
    try:
        _rmtree_force(project_path)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Failed to remove project directory {project_path}: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

    # The execution workspace holds the project's repo/ (its actual codebase).
    # Only remove it when the caller explicitly opts in (delete_workspace=true)
    # so a user can drop the project's conversations + memory while keeping the
    # sandboxed codebase on disk for later use.
    workspace_dir = get_project_execution_dir(project_id)
    workspace_error: str | None = None
    if delete_workspace and workspace_dir.exists():
        try:
            _rmtree_force(workspace_dir)
        except OSError as exc:
            # The project itself is gone; treat the workspace as a partial
            # cleanup failure (e.g., a dev server still holding files open)
            # and report it without re-creating the project entry.
            workspace_error = (
                f"Failed to remove execution workspace {workspace_dir}: "
                f"{type(exc).__name__}: {exc}"
            )

    if workspace_error:
        return {"status": "partial", "warning": workspace_error}
    if not delete_workspace and workspace_dir.exists():
        return {"status": "ok", "workspace_kept": True}
    return {"status": "ok"}


class UpdateFileRequest(BaseModel):
    filename: str
    content: str


@app.post("/api/projects/{project_id}/update-file")
def api_update_file(project_id: str, req: UpdateFileRequest):
    project_path = PROJECTS_DIR / project_id
    if not project_path.exists() or not project_path.is_dir():
        raise HTTPException(status_code=404, detail="Project not found")

    if req.filename not in MEMORY_FILES:
        raise HTTPException(status_code=400, detail="Invalid filename")

    filepath = project_path / req.filename
    filepath.write_text(req.content, encoding="utf-8")
    return {"status": "ok"}


# --- Conversation endpoints ---

class CreateConversationRequest(BaseModel):
    title: str = ""


@app.get("/api/projects/{project_id}/conversations")
def api_list_conversations(project_id: str):
    project_path = PROJECTS_DIR / project_id
    if not project_path.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    return list_conversations(project_id)


@app.post("/api/projects/{project_id}/conversations")
def api_create_conversation(project_id: str, req: CreateConversationRequest):
    project_path = PROJECTS_DIR / project_id
    if not project_path.exists():
        raise HTTPException(status_code=404, detail="Project not found")
    title = req.title or f"Conversation {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    return create_conversation(project_id, title)


@app.get("/api/conversations/{conversation_id}/messages")
def api_list_messages(conversation_id: str):
    conv = get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return list_messages(conversation_id)


@app.patch("/api/conversations/{conversation_id}")
def api_update_conversation(conversation_id: str, req: CreateConversationRequest):
    conv = get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if req.title:
        update_conversation_title(conversation_id, req.title)
    return get_conversation(conversation_id)


@app.delete("/api/conversations/{conversation_id}")
def api_delete_conversation(conversation_id: str):
    conv = get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    delete_conversation(conversation_id)
    return {"status": "ok"}


# --- GENERAL workspace conversation endpoints ---

@app.get("/api/general/conversations")
def api_list_general_conversations():
    return list_conversations(GENERAL_PROJECT_ID)


@app.post("/api/general/conversations")
def api_create_general_conversation(req: CreateConversationRequest):
    title = req.title or f"Conversation {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    return create_conversation(GENERAL_PROJECT_ID, title)


# --- Global memory endpoints ---

MEMORY_DIR = Path(__file__).resolve().parent.parent / "memory"

WRITABLE_GLOBAL_FILE_LIST = ["USER.md", "WORKSTYLE.md", "MEMORY.md"]

# Files a USER may edit through the Global Memory modal. SOUL.md is included
# here — and ONLY here — so the user (never any LLM/judge path) can edit it:
# `WRITABLE_GLOBAL_FILES` (the auto-writeback allow-list) still excludes SOUL.md,
# so the Main Agent, the intake judge, and post-run reconciliation remain unable
# to write it. This one manual endpoint is its sole write path.
USER_EDITABLE_GLOBAL_FILES = [*WRITABLE_GLOBAL_FILES, "SOUL.md"]


@app.get("/api/global-memory")
def api_get_global_memory():
    """Return the global memory files for the viewer/editor (SOUL.md included,
    shown read-only-to-the-agent + user-editable)."""
    return load_global_memory()


class UpdateGlobalFileRequest(BaseModel):
    filename: str
    content: str


@app.post("/api/global-memory/update-file")
def api_update_global_file(req: UpdateGlobalFileRequest):
    """Manually update a global memory file (the sole SOUL.md write path).

    Atomic (temp sibling + ``os.replace``): these files — SOUL.md especially — are
    read on every chat turn as the identity anchor, so a plain truncate-then-write
    would let a concurrent turn observe an empty / half-written file.
    """
    if req.filename not in USER_EDITABLE_GLOBAL_FILES:
        raise HTTPException(status_code=400, detail="Invalid filename or file is read-only")
    memory_engine._atomic_write(MEMORY_DIR / req.filename, req.content)
    return {"status": "ok"}


# --- Model provider endpoints (Task 07.1) ---


@app.get("/api/providers")
def api_list_model_providers():
    """Report provider availability for the UI selector.

    Returns all four providers (always visible) with an ``available`` flag
    derived purely from API-key presence, plus the resolved default provider
    the frontend should pre-select.
    """
    return {
        "providers": providers.list_providers(),
        "default": providers.default_provider(),
    }


# --- Agent registry & skills endpoints (Phase 10) ---


@app.get("/api/agents")
def api_list_agents():
    """The agent profile registry for the Agents browser + composer autocomplete.

    Global, read-only, structured data (agents_registry) — skill bodies are
    NOT included; the UI fetches one on demand via the skill read endpoint.
    """
    return {"agents": agents_registry.agents_for_ui()}


@app.get("/api/agents/{agent_id}/skills/{skill_id}")
def api_read_agent_skill(agent_id: str, skill_id: str):
    """Read one built-in skill's markdown body (registry-validated pair)."""
    ref = agents_registry.skill_ref(agent_id, skill_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="Unknown agent or skill")
    return {
        "agent_id": agent_id,
        "skill_id": ref.id,
        "title": ref.title,
        "description": ref.description,
        "content": skills_store.read_skill(agent_id, ref.id),
    }


class UpdateSkillRequest(BaseModel):
    content: str


@app.post("/api/agents/{agent_id}/skills/{skill_id}")
def api_update_agent_skill(agent_id: str, skill_id: str, req: UpdateSkillRequest):
    """Persist a manual skill edit (the ONLY write path for skill files —
    there is no LLM/autonomous route to this content)."""
    if agents_registry.skill_ref(agent_id, skill_id) is None:
        raise HTTPException(status_code=404, detail="Unknown agent or skill")
    try:
        skills_store.write_skill(agent_id, skill_id, req.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok"}


# --- Chat endpoint ---

class ChatRequest(BaseModel):
    conversation_id: str
    message: str
    # When the user clicked "Revise plan" on a pending execution and is now
    # typing revision instructions, the frontend echoes the pending id here
    # so the backend routes to the revision flow instead of the orchestrator.
    revise_pending_id: str | None = None
    # Task 07.0 — attachment metadata returned by ``/api/chat/upload`` for files
    # the user attached to this message. Stored verbatim on the user message so
    # the chat re-renders the file chips (and their chat/workspace scope) on
    # reload. Empty for ordinary text-only turns.
    attachments: list[dict] = []
    # Task 07.1 — selected model provider id (claude / gpt / gemini / deepseek /
    # kimi / glm). None falls back to the default provider (Claude when
    # available). An unknown or unavailable provider yields a clean 400.
    provider: str | None = None
    # Provider Registry 2.0 — selected model id within that provider. None falls
    # back to the provider's default model. An unknown provider/model combo
    # yields a clean 400.
    model: str | None = None


class ChatResponse(BaseModel):
    role: str
    content: str
    timestamp: str
    memory_updated: bool = False
    memory_updates: list[dict] = []
    # Phase 6 — the intake judge's one-sentence reason for the memory decision,
    # surfaced as a subtle "Memory updated — <reason>" chip in chat. "" when no
    # memory was written (or no reason given).
    memory_reason: str = ""
    # Phase 6 — the turn's classified intent (planning / design / build / debug /
    # inspect / memory / docs / retrospective / research / discussion) or the
    # explicit `@`-command mode. "" when unclassified. Drives a UI mode badge.
    intent: str = ""
    # Populated when the assistant message has a confirmable execution plan
    # attached (Task 05.9.5). The frontend keys off `status` to decide
    # whether to render the OK/Revise buttons under the message.
    pending_execution: dict | None = None
    # Echo of the assistant message id so the frontend can correlate the
    # rendered message with the persisted row (used for re-rendering on
    # reload via message metadata).
    message_id: str | None = None
    # Task 06.2D — when this chat turn dispatched a Coding Agent run (a `@code`
    # message), echo the run id so the frontend attaches the chat-first run
    # follow-up card (live status + browser-verification controls) to the
    # assistant message. ``None`` for ordinary chat turns.
    run_id: str | None = None
    # Task 06.1 — when the orchestrator inspected repo files to answer
    # the user, surface that list so the UI can clearly distinguish
    # answers grounded in memory from answers grounded in file reads.
    inspected_files: list[dict] = []
    # Phase 10 — the research actions (search / fetch) an explicit `@search`
    # turn performed, for the sources chip + audit trail. [] otherwise.
    research_sources: list[dict] = []
    # Phase 10 — set when the judge labeled an ordinary message `research`:
    # a {command, query} proposal the UI offers to pre-fill as `@search …`.
    # Sending that message is the user's explicit network grant — the
    # suggestion itself never triggers any network access.
    research_suggestion: dict | None = None


# Phase 6.1 — map a judged intent label (delegation_judge.INTENT_LABELS) to an
# orchestration mode string (orchestrator._MODE_GUIDANCE). Labels with no distinct
# workflow (discussion, build) map to None → no mode block. Note the label
# "retrospective" maps to the "review" mode and "planning" maps to "plan".
_INTENT_TO_MODE: dict[str, str] = {
    "planning": "plan",
    "design": "design",
    "debug": "debug",
    "inspect": "inspect",
    "retrospective": "review",
    "memory": "memory",
    "docs": "docs",
    "research": "research",
}


@app.post("/api/chat", response_model=ChatResponse)
def api_chat(req: ChatRequest):
    conv = get_conversation(req.conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    project_id = conv["project_id"]
    is_general = project_id == GENERAL_PROJECT_ID

    # Task 07.0 — a turn may carry text, attachments, or both. Reject only the
    # truly empty case (no text and no files). When text is empty but files are
    # attached, we synthesize a short note so the orchestrator/judge/memory
    # calls have something coherent to reason about; the stored user message
    # keeps the literal (possibly empty) text and renders chips from metadata.
    if not req.message.strip() and not req.attachments:
        raise HTTPException(status_code=400, detail="Message or an attachment is required")

    # Task 07.1 — resolve + validate the model provider for this turn. Done up
    # front so every path (chat, @code, revise) returns a clean error on a bad
    # provider; only the orchestrated chat response is actually routed to it.
    provider_id = (req.provider or "").strip() or providers.default_provider()
    if not providers.is_known(provider_id):
        raise HTTPException(
            status_code=400, detail=f"Unknown model provider: {provider_id!r}"
        )
    if not providers.is_available(provider_id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Model provider '{providers.label(provider_id)}' is not available "
                "— its API key is not configured on the server."
            ),
        )
    # Provider Registry 2.0 — validate the selected model within the provider.
    # None falls back to the provider's default; an unknown combo is a clean 400.
    model_id = (req.model or "").strip() or None
    if model_id is not None and not providers.is_known_model(provider_id, model_id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown model {model_id!r} for provider "
                f"'{providers.label(provider_id)}'."
            ),
        )

    attachment_note = ""
    if req.attachments:
        names = ", ".join(
            str(a.get("original_filename") or "file") for a in req.attachments
        )
        in_workspace = any(a.get("added_to_workspace") for a in req.attachments)
        attachment_note = (
            f"[Attached {len(req.attachments)} file(s): {names}"
            + ("; also added to the project workspace" if in_workspace else "")
            + "]"
        )
    effective_message = req.message.strip() or attachment_note

    # Persist user message. We capture its id so the pending-execution row
    # can link back to the message that triggered it. Task 07.0 — attachment
    # metadata rides along in the message metadata so the chat re-renders the
    # file chips on reload.
    user_meta = {"attachments": req.attachments} if req.attachments else None
    user_msg = add_message(req.conversation_id, "user", req.message, metadata=user_meta)

    # Load conversation history for orchestration context
    messages = list_messages(req.conversation_id)

    # --- Revise pending execution plan (Task 05.9.5) ---
    # When the user clicked "Revise plan" and is now sending revision
    # instructions, the frontend echoes the pending id back. We route to the
    # revision LLM call instead of the orchestrator, rewrite the plan in
    # place, and persist a new assistant message that points to the same
    # pending row (now with the revised display_plan + task_card).
    if req.revise_pending_id:
        return _handle_revise_pending(
            req=req,
            messages=messages,
            user_message_id=user_msg["id"],
            is_general=is_general,
        )

    # --- @code delegation short-circuit (Task 05.4) ---
    # When the user prefixes a project chat with `@code`, hand the message off
    # to CodingAgentRunner instead of the chat orchestrator. Memory writeback
    # is skipped: the runner already updates TASK.md, and project memory
    # judgment shouldn't be triggered by tool-style delegation requests.
    if is_code_delegation(req.message):
        run_id: str | None = None
        if is_general:
            response_content = GENERAL_REJECTION_MESSAGE
        else:
            project_name = _project_display_name(project_id)
            response_content, run_id = handle_code_delegation(
                project_id, project_name, req.message
            )
        # Attach the run id to the message metadata so the chat-first run
        # follow-up card re-hydrates after a reload.
        meta = {"run_id": run_id} if run_id else None
        assistant_msg = add_message(
            req.conversation_id, "assistant", response_content, metadata=meta
        )
        if len([m for m in messages if m["role"] == "user"]) <= 1:
            title = req.message[:60] + ("..." if len(req.message) > 60 else "")
            update_conversation_title(req.conversation_id, title)
        return ChatResponse(
            role="assistant",
            content=response_content,
            timestamp=assistant_msg["timestamp"],
            memory_updated=False,
            memory_updates=[],
            message_id=assistant_msg["id"],
            run_id=run_id,
        )

    history = [{"role": m["role"], "content": m["content"]} for m in messages]

    # --- Intent Router v2 (Phase 6) ---
    # An explicit mode `@`-command (@plan / @design / @debug / @review /
    # @inspect / @memory) sets the orchestration mode directly and skips the
    # delegation judge (none of these dispatch). Otherwise, in project chats the
    # LLM judge classifies the message: on `dispatch_suggested` we create a
    # confirmable pending plan (the only inferred path toward a run, still
    # gated on a user click). The richer `intent` label is informational — it
    # hints the memory-intake judge and drives UI badges, never routing.
    mode_command, _mode_body = parse_mode_command(req.message)
    turn_intent: str = mode_command or ""

    # Phase 10 — the explicit `@search`/`@research` command is the per-turn
    # NETWORK GRANT for the bounded research channel. Nothing else sets this:
    # a judge-labeled `research` intent routes to the research mode below but
    # never enables web access. GENERAL is allowed (user decision) — but a
    # GENERAL research turn skips memory intake entirely (see below).
    research_enabled = mode_command == "research"

    if not is_general and mode_command is None:
        project_name = _project_display_name(project_id)
        decision = judge_delegation(
            project_id=project_id,
            project_name=project_name,
            user_message=effective_message,
            history=history,
            is_general=False,
        )
        if decision.decision == DECISION_DISPATCH:
            return _handle_dispatch_suggested(
                req=req,
                project_id=project_id,
                source_message_id=user_msg["id"],
                decision=decision,
                messages=messages,
            )
        turn_intent = getattr(decision, "intent", "") or (
            "memory" if decision.decision == DECISION_MEMORY_ONLY else "discussion"
        )

    # Phase 6.1 — Intent → workflow routing. An explicit `@`-command (mode_command)
    # always wins; otherwise the judged intent label routes to the matching
    # orchestration mode so e.g. a natural debugging question folds in the latest
    # non-green run. Still conservative: this only shapes the response, never
    # dispatches (dispatch already early-returned above).
    turn_mode = mode_command or _INTENT_TO_MODE.get(turn_intent)

    # Generate orchestration response (with optional on-demand file inspection).
    # Task 07.1 — route the main response to the selected provider; Provider
    # Registry 2.0 — pin the selected model within that provider. Phase 6 —
    # ``mode`` shapes the system prompt for `@`-commands and (6.1) routed intents.
    response_content, inspected_files, research_sources = orchestrate(
        project_id, effective_message, history=history,
        provider=provider_id, model=model_id, mode=turn_mode,
        research_enabled=research_enabled,
    )

    # Memory judgment (Phase 6): one structured intake decision per turn, scoped
    # global vs project. Carries a reason the UI can surface; never fails the turn.
    # Done before persisting the assistant message so its outcome (intent +
    # memory reason) can ride in the message metadata and survive a reload.
    # Phase 10 — a GENERAL `@search` turn skips memory intake entirely: research
    # findings belong in a project's RESEARCH.md, not in global memory.
    applied: list[dict] = []
    memory_reason = ""
    if not (research_enabled and is_general):
        ctx = load_memory(project_id)
        scope = "global" if is_general else "project"
        mem_decision = judge_memory_intake(
            scope, ctx, effective_message, response_content, intent=turn_intent or None
        )
        applied = apply_memory_decision(
            mem_decision, scope, project_id=None if is_general else project_id
        )
        memory_reason = mem_decision.reason if applied else ""

    # Persist assistant reply. Include inspection list (06.1) + the Phase 6
    # intent / memory-reason so the chat history re-renders the badges/chips on
    # reload (these are not on the wire-only ChatResponse otherwise).
    assistant_meta: dict = {}
    if inspected_files:
        assistant_meta["inspected_files"] = inspected_files
    # Phase 10 — persist the research audit trail + the semantic suggestion the
    # same way (metadata survives reload, mirroring inspected_files).
    if research_sources:
        assistant_meta["research_sources"] = research_sources
    research_suggestion: dict | None = None
    if turn_intent == "research" and mode_command is None and not is_general:
        research_suggestion = {
            "command": "@search",
            "query": effective_message[:300],
        }
        assistant_meta["research_suggestion"] = research_suggestion
    if turn_intent:
        assistant_meta["intent"] = turn_intent
    if memory_reason:
        assistant_meta["memory_reason"] = memory_reason
    # Phase 6.1 — content-stripped audit list (file / section only) so the chat
    # "Memory updated" chip can expand to show exactly what changed.
    if applied:
        assistant_meta["memory_applied"] = [
            {"filename": u.get("filename"), "section": u.get("section")} for u in applied
        ]
    assistant_msg = add_message(
        req.conversation_id, "assistant", response_content,
        metadata=assistant_meta or None,
    )

    # Auto-title: if this is the first user message, set conversation title from it
    if len([m for m in messages if m["role"] == "user"]) <= 1:
        title = effective_message[:60] + ("..." if len(effective_message) > 60 else "")
        update_conversation_title(req.conversation_id, title)

    return ChatResponse(
        role="assistant",
        content=response_content,
        timestamp=assistant_msg["timestamp"],
        memory_updated=len(applied) > 0,
        memory_updates=applied,
        memory_reason=memory_reason,
        intent=turn_intent,
        message_id=assistant_msg["id"],
        inspected_files=inspected_files,
        research_sources=research_sources,
        research_suggestion=research_suggestion,
    )


# --- Confirmable execution plan helpers (Task 05.9.5) ---


def _handle_dispatch_suggested(
    *,
    req: ChatRequest,
    project_id: str,
    source_message_id: str,
    decision,
    messages: list[dict],
) -> ChatResponse:
    """Persist a new pending execution plan and return the assistant reply.

    Memory writeback is skipped — the user hasn't actually agreed to do the
    work yet. Auto-title behaves the same as the other short-circuit paths.
    """
    task_card = (decision.proposed_task_card or "").strip()
    if not task_card:
        # The judge said "dispatch" but didn't give us a task card. That
        # shouldn't happen with the current prompt, but be defensive — fall
        # back to the user's literal message.
        task_card = req.message.strip()

    title = (decision.title or "").strip() or derive_title_from_card(task_card)
    display_plan = (decision.display_plan or "").strip()
    if not display_plan:
        # Defensive fallback: synthesize a minimal plan so the UX still works.
        display_plan = (
            "I read this as a Coding Agent task. Here's what I'd hand off:\n\n"
            f"> {task_card}\n\n"
            "Confirm to dispatch, or revise the plan first."
        )

    pending_row = create_pending_execution(
        project_id=project_id,
        conversation_id=req.conversation_id,
        source_message_id=source_message_id,
        title=title,
        display_plan=display_plan,
        task_card=task_card,
    )
    plan = serialize_pending(pending_row)
    body = render_pending_chat_body(plan)
    metadata = {
        "pending_execution_id": plan.pending_execution_id,
        "intent": getattr(decision, "intent", "") or "build",
    }
    assistant_msg = add_message(
        req.conversation_id, "assistant", body, metadata=metadata
    )

    if len([m for m in messages if m["role"] == "user"]) <= 1:
        auto_title = req.message[:60] + ("..." if len(req.message) > 60 else "")
        update_conversation_title(req.conversation_id, auto_title)

    return ChatResponse(
        role="assistant",
        content=body,
        timestamp=assistant_msg["timestamp"],
        memory_updated=False,
        memory_updates=[],
        pending_execution=plan.to_dict(),
        message_id=assistant_msg["id"],
    )


def _handle_revise_pending(
    *,
    req: ChatRequest,
    messages: list[dict],
    user_message_id: str,
    is_general: bool,
) -> ChatResponse:
    """Apply revision instructions to an existing pending plan.

    Fails safely (HTTPException) on: missing pending id, stale pending id,
    pending row that's already dispatched/cancelled, GENERAL workspace (no
    execution workspace exists there), or empty revision instructions.
    """
    if is_general:
        raise HTTPException(
            status_code=400,
            detail="Pending execution plans are not available in the GENERAL workspace.",
        )

    pending_row = get_pending_execution(req.revise_pending_id or "")
    if not pending_row:
        raise HTTPException(
            status_code=404,
            detail="Pending execution plan not found — it may have been dispatched or expired.",
        )
    if pending_row["status"] != STATUS_PENDING:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Pending execution plan is {pending_row['status']!r}; "
                "revisions are only allowed while it is still pending."
            ),
        )
    if pending_row["conversation_id"] != req.conversation_id:
        raise HTTPException(
            status_code=400,
            detail="Pending execution plan does not belong to this conversation.",
        )

    instructions = (req.message or "").strip()
    if not instructions:
        raise HTTPException(
            status_code=400,
            detail="Revision instructions are empty.",
        )

    current = serialize_pending(pending_row)
    revision = revise_pending_plan(current, instructions)

    ok = update_pending_execution_plan(
        current.pending_execution_id,
        title=revision.title,
        display_plan=revision.display_plan,
        task_card=revision.task_card,
    )
    if not ok:
        # Race: pending was dispatched between read and write.
        raise HTTPException(
            status_code=409,
            detail="Pending execution plan changed state during revision; please refresh.",
        )

    refreshed_row = get_pending_execution(current.pending_execution_id)
    refreshed = serialize_pending(refreshed_row)  # type: ignore[arg-type]
    body = render_revised_chat_body(refreshed, revision.change_summary)
    metadata = {"pending_execution_id": refreshed.pending_execution_id}
    assistant_msg = add_message(
        req.conversation_id, "assistant", body, metadata=metadata
    )

    if len([m for m in messages if m["role"] == "user"]) <= 1:
        auto_title = req.message[:60] + ("..." if len(req.message) > 60 else "")
        update_conversation_title(req.conversation_id, auto_title)

    return ChatResponse(
        role="assistant",
        content=body,
        timestamp=assistant_msg["timestamp"],
        memory_updated=False,
        memory_updates=[],
        pending_execution=refreshed.to_dict(),
        message_id=assistant_msg["id"],
    )


# --- Chat attachment upload endpoints (Task 07.0) ---


@app.post("/api/chat/upload")
async def api_chat_upload(
    conversation_id: str = Form(...),
    add_to_workspace: bool = Form(False),
    files: list[UploadFile] = File(...),
):
    """Upload one or more attachments for a chat message (multipart form data).

    The owning project is derived from the conversation (never trusted from the
    client). Every file is stored chat-only; when ``add_to_workspace`` is set
    and the conversation belongs to a real project, each is additionally copied
    into ``repo/uploads/`` via the sandbox. Returns per-file metadata the
    frontend echoes back on the subsequent ``/api/chat`` send.

    A rejected file (bad name/type or oversize) fails the whole request with a
    400 so the user fixes it before sending — partial uploads would be
    confusing in the composer.
    """
    conv = get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    project_id = conv["project_id"]
    is_general = project_id == GENERAL_PROJECT_ID
    # "Add to workspace too" is only meaningful in project conversations — the
    # GENERAL workspace has no execution workspace to copy into.
    effective_add = bool(add_to_workspace) and not is_general

    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    attachments: list[dict] = []
    for upload in files:
        data = await upload.read()
        try:
            meta = save_chat_attachment(
                conversation_id=conversation_id,
                project_id=project_id,
                is_general=is_general,
                original_filename=upload.filename or "file",
                data=data,
                content_type=upload.content_type,
                add_to_workspace=effective_add,
            )
        except UploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        attachments.append(meta)

    return {"attachments": attachments, "added_to_workspace": effective_add}


# Raster image types that are safe to serve inline (they can't execute script as
# a top-level document). Everything else — notably image/svg+xml — is forced to
# download so an uploaded active document can't run in the backend origin.
_INLINE_SAFE_IMAGE_MIMES = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
}


@app.get("/api/conversations/{conversation_id}/attachments/{stored_filename}")
def api_get_chat_attachment(conversation_id: str, stored_filename: str):
    """Serve a previously uploaded chat-only attachment for inline preview.

    The filename is reduced to a bare leaf and re-resolved under the
    conversation's uploads dir, so the path can't escape it.
    """
    conv = get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    path = resolve_chat_attachment(conversation_id, stored_filename)
    if path is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "application/octet-stream"
    # Security: only raster images are safe to render inline in the backend
    # origin. An SVG (or any active type) can carry a <script> that executes as a
    # top-level document — stored XSS at localhost:8000, same origin as every
    # unauthenticated run/credential endpoint. Force such types to DOWNLOAD
    # (Content-Disposition: attachment) and always send nosniff so the browser
    # can't MIME-sniff a non-image into HTML. Inline <img> previews are
    # unaffected (scripts in an SVG loaded as an image never run).
    headers = {"X-Content-Type-Options": "nosniff"}
    if mime in _INLINE_SAFE_IMAGE_MIMES:
        return FileResponse(str(path), media_type=mime, headers=headers)
    return FileResponse(
        str(path),
        media_type=mime,
        filename=path.name,
        content_disposition_type="attachment",
        headers=headers,
    )


# --- Memory update endpoints ---

class MemoryUpdateRequest(BaseModel):
    filename: str
    section: str
    content: str
    action: str  # "append" or "replace"


@app.post("/api/projects/{project_id}/memory-update")
def api_memory_update(project_id: str, req: MemoryUpdateRequest):
    project_path = PROJECTS_DIR / project_id
    if not project_path.exists():
        raise HTTPException(status_code=404, detail="Project not found")

    if req.action not in ("append", "replace"):
        raise HTTPException(status_code=400, detail="Action must be 'append' or 'replace'")

    success = apply_memory_update(project_id, req.filename, req.section, req.content, req.action)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to apply memory update")
    return {"status": "ok"}


@app.post("/api/conversations/{conversation_id}/extract-updates")
def api_extract_updates(conversation_id: str):
    """Extract potential memory updates from a conversation using LLM judgment."""
    conv = get_conversation(conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msgs = list_messages(conversation_id)
    if len(msgs) < 2:
        return {"updates": [], "project_id": conv["project_id"]}

    # Use the last user message and last assistant response
    last_user = ""
    last_assistant = ""
    for m in reversed(msgs):
        if m["role"] == "assistant" and not last_assistant:
            last_assistant = m["content"]
        elif m["role"] == "user" and not last_user:
            last_user = m["content"]
        if last_user and last_assistant:
            break

    ctx = load_memory(conv["project_id"])
    updates = judge_memory_updates(ctx, last_user, last_assistant)
    return {"updates": updates, "project_id": conv["project_id"]}


# --- Execution workspace endpoints (Phase 3 foundation) ---

def _project_display_name(project_id: str) -> str | None:
    """Derive a human project name from PROJECT.md's first H1, fallback to id."""
    project_md = PROJECTS_DIR / project_id / "PROJECT.md"
    if project_md.exists():
        first_line = project_md.read_text(encoding="utf-8").split("\n", 1)[0].strip()
        if first_line.startswith("# "):
            return first_line[2:].strip() or None
    return None


# Path-segment guard for ids that arrive as URL path parameters (project_id,
# run_id, pending_id, …). Only ``api_create_project`` validates the id charset
# at creation; every other route takes the id as a raw segment and joins it into
# a filesystem path. A single ``..`` / ``.`` segment (reachable via a decoded
# ``%2e%2e``) would otherwise resolve above the intended root — worst case
# ``api_delete_project`` rmtree'ing the parent tree. Ids we mint are lowercase
# slugs / ``YYYYMMDD-HHMMSS-<hex>`` run ids / hex pending ids, plus the reserved
# ``__GENERAL__``; all match this charset. Reject anything else at the boundary.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_id(value: str, *, kind: str = "id") -> str:
    """Return ``value`` if it is a safe single path segment, else HTTP 400.

    Rejects empties, ``.``/``..``, path separators, and any character outside
    ``[A-Za-z0-9._-]`` so the id can never traverse out of its intended root.
    """
    if not isinstance(value, str) or value in ("", ".", "..") or not _SAFE_ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"invalid {kind}")
    return value


def _require_project(project_id: str) -> Path:
    _safe_id(project_id, kind="project id")
    project_path = PROJECTS_DIR / project_id
    if not project_path.exists() or not project_path.is_dir():
        raise HTTPException(status_code=404, detail="Project not found")
    return project_path


@app.post("/api/projects/{project_id}/execution/init")
def api_execution_init(project_id: str):
    _require_project(project_id)
    name = _project_display_name(project_id)
    workspace = init_execution_workspace(project_id, name)
    return workspace.model_dump()


@app.get("/api/projects/{project_id}/execution/workspace")
def api_execution_workspace(project_id: str):
    _require_project(project_id)
    workspace = get_execution_workspace(project_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Execution workspace not initialized")
    return workspace.model_dump()


@app.get("/api/projects/{project_id}/execution/task-state")
def api_execution_get_task_state(project_id: str):
    _require_project(project_id)
    content = read_task_state(project_id)
    if content is None:
        raise HTTPException(status_code=404, detail="TASK.md not found — initialize the workspace first")
    return {"project_id": project_id, "content": content}


class UpdateTaskStateRequest(BaseModel):
    content: str


@app.post("/api/projects/{project_id}/execution/task-state")
def api_execution_update_task_state(project_id: str, req: UpdateTaskStateRequest):
    _require_project(project_id)
    ok = update_task_state(project_id, req.content)
    if not ok:
        raise HTTPException(status_code=404, detail="Execution workspace not initialized")
    return {"status": "ok"}


# --- Execution tool runtime endpoints (Phase 3 — Task 05.2) ---
# Manual/local testing endpoints. The future Coding Agent will call the
# ToolRuntime directly inside the backend; these HTTP wrappers exist so the
# sandbox can be exercised end-to-end without an LLM in the loop.


def _require_workspace(project_id: str) -> None:
    _require_project(project_id)
    if get_execution_workspace(project_id) is None:
        raise HTTPException(
            status_code=404,
            detail="Execution workspace not initialized — call /execution/init first",
        )


@app.post("/api/projects/{project_id}/execution/tools/list-files")
def api_tool_list_files(project_id: str, req: ListFilesRequest):
    _require_workspace(project_id)
    return ToolRuntime(project_id).list_files(req.path).model_dump()


@app.post("/api/projects/{project_id}/execution/tools/read-file")
def api_tool_read_file(project_id: str, req: ReadFileRequest):
    _require_workspace(project_id)
    return ToolRuntime(project_id).read_file(req.path).model_dump()


@app.post("/api/projects/{project_id}/execution/tools/write-file")
def api_tool_write_file(project_id: str, req: WriteFileRequest):
    _require_workspace(project_id)
    return ToolRuntime(project_id).write_file(req.path, req.content).model_dump()


@app.post("/api/projects/{project_id}/execution/tools/append-file")
def api_tool_append_file(project_id: str, req: AppendFileRequest):
    _require_workspace(project_id)
    return ToolRuntime(project_id).append_file(req.path, req.content).model_dump()


@app.post("/api/projects/{project_id}/execution/tools/search-files")
def api_tool_search_files(project_id: str, req: SearchFilesRequest):
    _require_workspace(project_id)
    return ToolRuntime(project_id).search_files(req.query, req.path).model_dump()


@app.post("/api/projects/{project_id}/execution/tools/run-shell")
def api_tool_run_shell(project_id: str, req: RunShellRequest):
    _require_workspace(project_id)
    return ToolRuntime(project_id).run_shell(req.command, req.timeout_seconds).model_dump()


# --- Execution agent run endpoints (Phase 3 — Tasks 05.3 + 05.6A) ---
# Runs are dispatched to a background thread pool (see execution/background.py).
# POST returns the placeholder RunRecord (status="running") immediately; the
# GET endpoints below reflect status transitions as the run progresses.


class CreateRunRequest(BaseModel):
    title: str
    task_card: str
    created_by: str = "manual"


@app.post("/api/projects/{project_id}/execution/runs")
def api_create_run(project_id: str, req: CreateRunRequest):
    _require_workspace(project_id)
    if not req.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    if not req.task_card.strip():
        raise HTTPException(status_code=400, detail="task_card is required")

    task = TaskSpec(title=req.title, task_card=req.task_card, created_by=req.created_by)
    record = get_default_manager().dispatch(project_id, task)
    return record.model_dump()


@app.get("/api/projects/{project_id}/execution/runs")
def api_list_runs(project_id: str):
    _require_workspace(project_id)
    return run_store.list_runs(project_id)


@app.get("/api/projects/{project_id}/execution/runs/{run_id}")
def api_get_run(project_id: str, run_id: str):
    _require_workspace(project_id)
    record = run_store.read_run_json(project_id, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run not found")
    return record


@app.get("/api/projects/{project_id}/execution/runs/{run_id}/result")
def api_get_run_result(project_id: str, run_id: str):
    _require_workspace(project_id)
    content = run_store.read_result_md(project_id, run_id)
    if content is None:
        raise HTTPException(status_code=404, detail="result not found")
    return {"run_id": run_id, "content": content}


@app.get("/api/projects/{project_id}/execution/runs/{run_id}/plan")
def api_get_run_plan(project_id: str, run_id: str):
    """Return the run's execution plan + task graph (Phase 5).

    Reads the standalone ``plan.json`` artifact, falling back to the ``plan``
    embedded in run.json. 404 when the run predates planning or has no plan.
    """
    _require_workspace(project_id)
    plan = run_store.read_plan_json(project_id, run_id)
    if plan is None:
        record = run_store.read_run_json(project_id, run_id)
        if record is not None and record.get("plan") is not None:
            plan = record["plan"]
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@app.get("/api/projects/{project_id}/execution/runs/{run_id}/events")
def api_get_run_events(project_id: str, run_id: str, since: int = 0):
    """Return the run's event timeline (run control / live timeline / trace).

    Reads the append-only ``events.jsonl`` and returns the parsed events in
    chronological order. 404 only when the run dir itself is missing; a run
    with no events yet returns an empty list. Drives the Run Detail timeline
    and the Live Trace, both polled alongside run.json while a run is active.

    ``since`` is an optional cursor: an index into the full event list. The
    response returns ``events[since:]`` plus ``total`` (the full count) so a
    live poller can append only new events instead of re-rendering the whole
    log. Omitting ``since`` (or ``since=0``) returns every event — the legacy
    behavior the Run Detail modal relies on.
    """
    _require_workspace(project_id)
    if run_store.read_run_json(project_id, run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    all_events = run_store.read_events(project_id, run_id)
    start = since if since > 0 else 0
    return {
        "run_id": run_id,
        "events": all_events[start:],
        "total": len(all_events),
    }


@app.get("/api/projects/{project_id}/execution/runs/{run_id}/screenshot")
def api_get_run_screenshot(project_id: str, run_id: str, name: str = "browser.png"):
    """Serve a browser-verification screenshot for a run (Task 06.2B + multi-page).

    Defaults to the primary ``screenshots/browser.png`` (the legacy contract).
    The optional ``name`` query param selects an additional captured page
    (e.g. ``page-02.png``); it is validated to a bare ``*.png`` basename inside
    the run's ``screenshots/`` dir, so no caller input can escape the artifact
    directory.
    """
    _require_workspace(project_id)
    # Reject anything that isn't a plain ``*.png`` basename (no traversal, no
    # subdirs, no absolute paths).
    safe_name = os.path.basename(name or "browser.png")
    if (
        safe_name != name
        or not safe_name.lower().endswith(".png")
        or safe_name in (".", "..")
        or "/" in name
        or "\\" in name
    ):
        raise HTTPException(status_code=400, detail="invalid screenshot name")
    run_dir = run_store.get_run_dir(project_id, run_id)
    screenshot = run_dir / "screenshots" / safe_name
    if not screenshot.exists() or not screenshot.is_file():
        raise HTTPException(status_code=404, detail="screenshot not found")
    return FileResponse(str(screenshot), media_type="image/png")


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/cancel")
def api_cancel_run(project_id: str, run_id: str):
    """Request cooperative cancellation of an active run (run control).

    Only a ``running`` run can be cancelled (409 otherwise). Sets
    ``cancel_requested`` so the UI can show a transient "cancelling" phase, then
    signals the in-flight runner, which stops at its next step boundary and
    writes a terminal ``cancelled`` status + artifacts. If no runner is in
    flight in this process (e.g. after a server restart the registry is empty),
    the orphaned record is finalized to ``cancelled`` here so it never stays
    stuck — but only after re-reading run.json to confirm it is still
    ``running`` (guards against clobbering a run that just finished).
    """
    _require_workspace(project_id)
    raw = run_store.read_run_json(project_id, run_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        record = RunRecord(**raw)
    except Exception:
        raise HTTPException(status_code=500, detail="run record is corrupt")
    if record.status != RunStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"only a running run can be cancelled (status={record.status.value})",
        )

    # Stamp cancel_requested ONLY while the run is still `running`, under the
    # per-run lock, re-reading first. Without this guard the endpoint would
    # blindly write a stale RUNNING record — reverting a run that finalized
    # between our first read and this write, and destroying its real result.
    stamped = {"ok": False}

    def _mark(rec: RunRecord):
        if rec.status != RunStatus.RUNNING:
            return rec  # already settled between our read and now — don't revert
        rec.cancel_requested = True
        stamped["ok"] = True
        return rec

    run_store.mutate_run_json(project_id, run_id, _mark)
    if stamped["ok"]:
        run_store.append_event(project_id, run_id, {"type": "run_cancel_requested"})

    in_flight = get_default_manager().request_cancel(run_id)
    if not in_flight:
        # No runner in this process owns the run (e.g. after a restart the cancel
        # registry is empty). Finalize the orphan to `cancelled` — but only if it
        # is genuinely STILL running when we take the lock, so a run that just
        # finalized (or that a live worker is finalizing) is never clobbered.
        settled = {"orphan": None}

        def _finalize_orphan(rec: RunRecord):
            if rec.status != RunStatus.RUNNING:
                return rec  # already terminal — respect it
            rec.status = RunStatus.CANCELLED
            rec.completed_at = datetime.utcnow()
            rec.cancel_requested = False
            run_store._clear_transient_states(rec)
            rec.summary = "Run cancelled by user."
            blocker = "run cancelled by user (no active worker)"
            if blocker not in rec.blockers:
                rec.blockers = list(rec.blockers) + [blocker]
            if rec.plan is not None:
                for unit in rec.plan.tasks:
                    if unit.status == TaskStatus.RUNNING:
                        unit.status = TaskStatus.SKIPPED
                        if "run cancelled" not in unit.blockers:
                            unit.blockers.append("run cancelled")
            settled["orphan"] = rec
            return rec

        final = run_store.mutate_run_json(project_id, run_id, _finalize_orphan)
        orphan = settled["orphan"]
        if orphan is not None:
            # We won the settle — write the dependent artifacts (plan.json /
            # result.md / event) from the record we committed.
            if orphan.plan is not None:
                run_store.write_plan_json(project_id, run_id, orphan.plan)
            run_store.write_result_md(
                project_id,
                run_id,
                run_store.render_result_md(
                    orphan,
                    orphan.summary,
                    notes="Run cancelled by user; no active worker to stop.",
                ),
            )
            run_store.append_event(
                project_id, run_id, {"type": "run_cancelled", "reason": "orphan"}
            )
            return orphan.model_dump()
        # Someone else settled it first — fall through and return the latest.

    # Worker is in flight (or the run already settled) — return the latest record.
    latest = run_store.read_run_json(project_id, run_id)
    return latest if latest is not None else record.model_dump()


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/retry")
def api_retry_run(project_id: str, run_id: str):
    """Retry a terminal run as a new, linked run (run control).

    Reads the original task card and dispatches a fresh run with the same task
    (explicit user action — no auto-rerun). The new run records ``retry_of`` and
    the original records ``retried_by`` so the two are linked; history is never
    mutated beyond that pointer. 409 if the original is still running.
    """
    _require_workspace(project_id)
    raw = run_store.read_run_json(project_id, run_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        original = RunRecord(**raw)
    except Exception:
        raise HTTPException(status_code=500, detail="run record is corrupt")
    if original.status == RunStatus.RUNNING:
        raise HTTPException(
            status_code=409, detail="cannot retry a run that is still running"
        )

    title, body = run_store.read_task_card(project_id, run_id)
    title = title or original.task_title or "Retry"
    if not body.strip():
        raise HTTPException(
            status_code=409, detail="original task card is empty; nothing to retry"
        )

    task = TaskSpec(title=title, task_card=body, created_by="retry")
    new_record = get_default_manager().dispatch(project_id, task, retry_of=run_id)

    # Link the original back to the retry. Re-read to avoid clobbering any
    # concurrent write to the original record.
    fresh = run_store.read_run_json(project_id, run_id)
    if fresh is not None:
        try:
            original = RunRecord(**fresh)
        except Exception:
            pass
    original.retried_by = new_record.run_id
    run_store.write_run_json(project_id, run_id, original)
    run_store.append_event(
        project_id, run_id, {"type": "run_retried", "new_run_id": new_record.run_id}
    )
    return new_record.model_dump()


# --- Suggested skill patch (Phase 10.2) — review-first self-improvement ---
# A green run may PROPOSE a skill refinement (stored on run.json). Applying goes
# through skills_store (the sole skill write path) only on this explicit action.


class ApplySkillPatchRequest(BaseModel):
    # Optional user-edited content; when omitted, the proposal's content is used.
    content: str | None = None


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/skill-patch/apply")
def api_apply_skill_patch(project_id: str, run_id: str, req: ApplySkillPatchRequest):
    _require_workspace(project_id)
    from execution import skill_patch

    try:
        updated = skill_patch.apply_skill_patch(
            project_id, run_id, content_override=req.content
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return updated.model_dump()


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/skill-patch/reject")
def api_reject_skill_patch(project_id: str, run_id: str):
    _require_workspace(project_id)
    from execution import skill_patch

    try:
        updated = skill_patch.reject_skill_patch(project_id, run_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return updated.model_dump()


class ProposeRecoveryRequest(BaseModel):
    conversation_id: str


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/propose-recovery")
def api_propose_recovery(project_id: str, run_id: str, req: ProposeRecoveryRequest):
    """Turn a run's recovery assessment into a confirmable pending plan (Phase 6).

    The Main Agent assessed a non-green run and recommended a follow-up coding
    task. This endpoint materializes that recommendation as a normal
    **confirmable pending execution** (the user still clicks "OK, run this" to
    dispatch — no auto-run) and posts it as an assistant message in the given
    conversation so it renders as a recovery card. Returns the pending plan +
    the new message id.
    """
    _require_workspace(project_id)
    conv = get_conversation(req.conversation_id)
    if not conv or conv["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Conversation not found for this project")

    raw = run_store.read_run_json(project_id, run_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        record = RunRecord(**raw)
    except Exception:
        raise HTTPException(status_code=500, detail="run record is corrupt")

    # Phase 6.1 — one recovery per parent. If an auto-recovery (or a prior manual
    # one) already claimed this run, don't propose another.
    if record.recovered_by is not None:
        raise HTTPException(
            status_code=409,
            detail=f"This run already has a recovery run ({record.recovered_by}).",
        )

    ra = record.recovery_assessment
    if ra is None or not ra.assessed:
        raise HTTPException(status_code=409, detail="run has no recovery assessment")
    task_card = (ra.follow_up_task_card or "").strip()
    if ra.verdict != "needs_recovery" or not task_card:
        raise HTTPException(
            status_code=409,
            detail="recovery assessment did not recommend a confirmable follow-up run",
        )

    title = derive_title_from_card(task_card, fallback=f"Recovery for {record.task_title or run_id}")
    # The card can carry a Phase 11 evidence block — quote only its head in the
    # chat bubble (the full card still rides the pending plan + Revise flow).
    card_quote = task_card if len(task_card) <= 400 else task_card[:397].rstrip() + "..."
    type_line = (
        f"**Failure type:** {ra.recovery_type}\n\n" if ra.recovery_type else ""
    )
    display_plan = (
        f"I looked at the **{record.status.value}** run _{record.task_title or run_id}_ and "
        f"recommend a **{ra.recommended_action}** follow-up.\n\n"
        f"{type_line}"
        f"**Diagnosis:** {ra.diagnosis or '(none)'}\n\n"
        f"**Proposed fix:**\n\n> {card_quote}\n\n"
        f"{ra.rationale or ''}\n\n"
        "Confirm to dispatch this recovery run, or revise the plan first."
    ).strip()

    pending_row = create_pending_execution(
        project_id=project_id,
        conversation_id=req.conversation_id,
        source_message_id=None,
        title=title,
        display_plan=display_plan,
        task_card=task_card,
        recovery_of=run_id,
    )
    plan = serialize_pending(pending_row)
    body = render_pending_chat_body(plan)
    assistant_msg = add_message(
        req.conversation_id, "assistant", body,
        metadata={"pending_execution_id": plan.pending_execution_id, "recovery_of": run_id},
    )
    return {
        "pending_execution": plan.to_dict(),
        "message_id": assistant_msg["id"],
    }


class BrowserVerifyRequest(BaseModel):
    # Provider Registry 2.0 — the user's currently-selected chat model, so the
    # diagnostic AI visual judgment prefers a vision-capable selection (and
    # skips gracefully when neither it nor any provider key supports vision).
    # Both optional: an empty POST body keeps the legacy default-provider path.
    provider: str | None = None
    model: str | None = None


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/browser-verify")
def api_run_browser_verification(
    project_id: str,
    run_id: str,
    req: BrowserVerifyRequest | None = Body(default=None),
):
    """User-triggered browser verification for an existing run (Task 06.2C).

    Reuses the 06.2B browser-verification infrastructure but adds a frontend
    dependency-install step and a sensible default dev command (port 5174),
    so a completed frontend run can be verified from the UI without editing
    TASK.md. Writes the result back into the same run artifacts (run.json,
    result.md, screenshots/) and updates run status consistently — a failing
    verification downgrades a ``completed`` run to ``partial``.

    Runs synchronously: install + dev-server + screenshot takes seconds for a
    small Vite app, and the caller (RunDetailModal) shows a verifying state
    while awaiting the response.
    """
    _require_workspace(project_id)
    raw = run_store.read_run_json(project_id, run_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        record = RunRecord(**raw)
    except Exception:
        raise HTTPException(status_code=500, detail="run record is corrupt")
    if record.status not in (RunStatus.COMPLETED, RunStatus.PARTIAL):
        raise HTTPException(
            status_code=409,
            detail=(
                "browser verification can only run on a completed run "
                f"(status={record.status.value})"
            ),
        )

    # Task 06.2D — mark the run as actively browser-verifying BEFORE the
    # blocking work so a concurrent Runs-panel poll (served on a separate
    # thread) sees the run is active again rather than looking finished/stale.
    # Under the per-run lock so it can't clobber a concurrent commit/deploy write.
    def _mark_running(r: RunRecord):
        r.browser_verification_state = "running"
        return r

    run_store.mutate_run_json(project_id, run_id, _mark_running)
    run_store.append_event(
        project_id, run_id, {"type": "browser_verification_started"}
    )

    # On a passing verification, hand the still-running dev server off to the
    # managed preview layer so the captured URL stays usable (Task 06.2D).
    def _keep_alive(proc, stdout_drainer, stderr_drainer, command, url) -> bool:
        return preview.adopt_preview(
            project_id, proc, stdout_drainer, stderr_drainer, command, url
        )

    result = run_ui_browser_verification(
        project_id,
        run_dir=run_store.get_run_dir(project_id, run_id),
        keep_alive_registrar=_keep_alive,
    )

    # AI visual judgment over the captured screenshots — synchronously, in this
    # same request. Diagnostic-only (never changes run status) and best-effort:
    # skips gracefully (with a reason) when no vision-capable provider key is
    # configured. Runs only on a passing capture with screenshots. Computed from
    # the read-only task context before the locked merge.
    visual_review = None
    if result.status == "passed" and result.pages:
        title, body = run_store.read_task_card(project_id, run_id)
        visual_review = run_visual_review(
            project_id,
            run_id,
            task_card=body or title or record.task_title,
            summary=record.summary,
            browser_result=result,
            run_dir=run_store.get_run_dir(project_id, run_id),
            provider=(req.provider if req else None),
            model=(req.model if req else None),
        )

    # Fold the browser-verification outcome onto a FRESH read of the record under
    # the lock, so a commit/push/deploy that landed during the (multi-second)
    # capture is preserved rather than clobbered by a stale write.
    def _apply_bv(r: RunRecord):
        apply_ui_browser_verification_to_record(r, result)
        r.browser_verification_state = result.status
        if visual_review is not None:
            r.visual_review = visual_review
        return r

    updated = run_store.mutate_run_json(project_id, run_id, _apply_bv)
    if updated is not None:
        record = updated
    run_store.rerender_result_md(project_id, run_id, record)
    run_store.append_event(
        project_id,
        run_id,
        {
            "type": "browser_verification_ui",
            "enabled": result.enabled,
            "status": result.status,
            "command": result.command,
            "url": result.url,
            "install_status": result.install_status,
            "screenshot_path": result.screenshot_path,
            "pages": len(result.pages),
            "readiness": result.readiness,
            "duration_ms": result.duration_ms,
            "flows": [
                {"name": f.name, "status": f.status} for f in result.flows
            ],
            "console_errors": len(result.console_errors),
            "network_failures": len(result.network_failures),
        },
    )
    if record.visual_review is not None:
        run_store.append_event(
            project_id,
            run_id,
            {
                "type": "visual_review",
                "status": record.visual_review.status,
                "headline": record.visual_review.headline,
                "provider": record.visual_review.provider,
                "url": result.url,
            },
        )
    # Phase 11 — a failed UI-triggered browser verification / visual verdict
    # deserves a typed recovery assessment too (the runner-tail assessment
    # only sees the state at finalize). Best-effort + idempotent: assess_run
    # short-circuits when an assessment already exists and NEVER dispatches.
    try:
        if (
            (result.enabled and result.status == "failed")
            or (
                record.visual_review is not None
                and record.visual_review.enabled
                and record.visual_review.status == "failed"
            )
        ):
            assess_run(project_id, run_id)
    except Exception:
        pass
    return record.model_dump()


# --- Managed preview server endpoints (Task 06.2D) ---
# A small process-local layer that keeps a project's dev server alive so the
# preview URL returned after a successful browser verification stays usable,
# and so the Runs panel can explicitly Start / Stop a preview. The dev-server
# command is sandbox-validated; at most one preview runs per project.


@app.get("/api/projects/{project_id}/preview/status")
def api_preview_status(project_id: str):
    _require_workspace(project_id)
    return preview.get_preview_status(project_id)


class StartPreviewRequest(BaseModel):
    # Optional overrides; both default to the standard Vite dev command on the
    # non-conflicting 5174 port (Agent OS itself uses 5173). The command still
    # goes through the sandbox before launch.
    command: str = DEFAULT_DEV_COMMAND
    url: str = DEFAULT_DEV_URL


@app.post("/api/projects/{project_id}/preview/start")
def api_preview_start(project_id: str, req: StartPreviewRequest):
    _require_workspace(project_id)
    status = preview.start_preview(
        project_id, command=req.command, url=req.url
    )
    if not status.get("ok"):
        raise HTTPException(
            status_code=409,
            detail=status.get("error") or "failed to start preview server",
        )
    return status


@app.post("/api/projects/{project_id}/preview/stop")
def api_preview_stop(project_id: str):
    _require_workspace(project_id)
    return preview.stop_preview(project_id)


# --- Project Ops endpoints (Phase 7 — Git / GitHub lifecycle) ---
# Every Git operation routes through git_ops -> ToolRuntime.run_git (the single
# Git executor); GitHub push/PR go through the leak-proof connector. Repo-scoped
# endpoints reject the GENERAL workspace (no repo). Credential endpoints work
# everywhere (global-scope tokens are project-independent). EXTERNAL/DESTRUCTIVE
# actions (push / PR / rollback) and local commit are explicit, two-phase
# preview/confirm contracts — they execute only when the request carries
# ``confirm: true`` (the user's click). Nothing here auto-runs.


def _reject_general_repo_op(project_id: str) -> None:
    if project_id == GENERAL_PROJECT_ID:
        raise HTTPException(
            status_code=400,
            detail="Git operations are not available in the General workspace.",
        )


def _require_project_or_general(project_id: str) -> None:
    # Credential/connector endpoints are about tokens, not the repo, so they work
    # in GENERAL (global-scope) too.
    if project_id != GENERAL_PROJECT_ID:
        _require_project(project_id)


def _load_run_record(project_id: str, run_id: str) -> RunRecord:
    _safe_id(run_id, kind="run id")
    raw = run_store.read_run_json(project_id, run_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        return RunRecord(**raw)
    except Exception:
        raise HTTPException(status_code=500, detail="run record is corrupt")


def _generate_commit_message(project_id: str, record: RunRecord, files: list[str]) -> str:
    """Generate a concise commit message from compact, redacted run metadata
    (never the raw diff, never a secret). Mirrors ``revise_pending_plan``: an
    LLM call with a deterministic heuristic fallback; never raises."""
    base = (record.summary or record.task_title or "").strip().splitlines()
    fallback = (base[0][:72] if base else (f"Update {len(files)} file(s)" if files else "Update"))
    try:
        from llm import chat as llm_chat

        sys_prompt = (
            "You write ONE concise git commit message for a set of code changes. "
            "Output only the message: a <=72-char imperative subject line, optionally "
            "followed by a blank line and 1-3 short bullet points. No code fences, no preamble."
        )
        ctx = credentials.redact(
            f"Task: {record.task_title}\n"
            f"Summary: {record.summary}\n"
            f"Diff stat: {record.diff_stat or '(unknown)'}\n"
            f"Files: {', '.join(files[:30])}\n",
            project_id,
        )
        out = (llm_chat(sys_prompt, [{"role": "user", "content": ctx}]) or "").strip()
        if out.startswith("```"):
            out = out.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return out or fallback
    except Exception:  # noqa: BLE001
        return fallback


def _generate_pr_body(record: RunRecord) -> str:
    parts: list[str] = []
    if record.summary:
        parts.append(record.summary.strip())
    if record.diff_stat:
        parts.append(f"**Changes:** {record.diff_stat}")
    parts.append(f"_Delivered via Agent OS (run `{record.run_id}`)._")
    return "\n\n".join(parts).strip()


# ----- live git + connector status -----


@app.get("/api/projects/{project_id}/git/status")
def api_git_status(project_id: str):
    _require_workspace(project_id)
    _reject_general_repo_op(project_id)
    return git_ops.git_status(project_id).to_dict()


@app.get("/api/projects/{project_id}/git/log")
def api_git_log(project_id: str, limit: int = 50):
    """Read-only commit history (newest first) for the project repo — powers the
    GitHub modal's traceable git-record view. `git log` is sandbox-allow-listed
    and non-destructive; author/subject are redacted, output is bounded."""
    _require_workspace(project_id)
    _reject_general_repo_op(project_id)
    return {"commits": git_ops.list_commits(project_id, limit=limit)}


@app.get("/api/projects/{project_id}/github/connector")
def api_github_connector_status(project_id: str):
    _require_project_or_general(project_id)
    return github_connector.status(project_id).to_dict()


# ----- credentials (presence only; values never echoed) -----


class SetGithubCredentialRequest(BaseModel):
    token: str
    scope: str = "project"  # "project" | "global"
    default_remote: str | None = None  # optional "owner/repo"


@app.get("/api/projects/{project_id}/credentials/github")
def api_get_github_credential(project_id: str):
    _require_project_or_general(project_id)
    return credentials.status(project_id)


@app.post("/api/projects/{project_id}/credentials/github")
def api_set_github_credential(project_id: str, req: SetGithubCredentialRequest):
    _require_project_or_general(project_id)
    token = (req.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="token is required")
    scope = req.scope if req.scope in ("project", "global") else "project"
    try:
        credentials.set_github_credential(
            None if scope == "global" else project_id,
            token=token,
            scope=scope,
            default_remote=req.default_remote,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Validate + capture the login (the response is presence-only, never the token).
    return github_connector.status(project_id).to_dict()


@app.delete("/api/projects/{project_id}/credentials/github")
def api_delete_github_credential(project_id: str, scope: str = "project"):
    _require_project_or_general(project_id)
    scope = scope if scope in ("project", "global") else "project"
    return credentials.delete_github_credential(
        None if scope == "global" else project_id, scope=scope
    )


# ----- project GitHub repo target (owner/repo) -----
# Distinct from the account-level token (which lives in .env): the repo a project
# is pushed to is project-specific, so it is entered in the UI. Stored as the
# github connector's ``default_remote`` metadata; read project-first via
# get_metadata so it works even when the token comes from .env.


def _parse_owner_repo(url: str) -> str:
    """Normalize a GitHub repo reference to ``owner/repo``. Accepts
    ``owner/repo``, ``https://github.com/owner/repo(.git)``, or
    ``git@github.com:owner/repo(.git)``. Raises 400 on anything else."""
    u = (url or "").strip()
    u = re.sub(r"^https?://github\.com/", "", u, flags=re.IGNORECASE)
    u = re.sub(r"^git@github\.com:", "", u, flags=re.IGNORECASE)
    u = re.sub(r"\.git$", "", u, flags=re.IGNORECASE).strip("/")
    parts = [p for p in u.split("/") if p]
    if len(parts) < 2:
        raise HTTPException(
            status_code=400,
            detail="enter a GitHub repo as owner/repo or a github.com URL",
        )
    return f"{parts[0]}/{parts[1]}"


class SetRepoUrlRequest(BaseModel):
    repo_url: str


@app.get("/api/projects/{project_id}/github/repo")
def api_get_github_repo(project_id: str):
    _require_project(project_id)
    remote = credentials.get_metadata("github", "default_remote", project_id)
    return {"repo": remote, "url": f"https://github.com/{remote}" if remote else None}


@app.post("/api/projects/{project_id}/github/repo")
def api_set_github_repo(project_id: str, req: SetRepoUrlRequest):
    _require_project(project_id)
    remote = _parse_owner_repo(req.repo_url)
    credentials.set_credential(
        "github", project_id, fields={"default_remote": remote}, scope="project"
    )
    return {"repo": remote, "url": f"https://github.com/{remote}"}


# ----- generic multi-provider connectors (Phase 8: vercel / supabase / stripe;
# github keeps its own connector-validating routes above) -----
#
# Presence-only everywhere: a token VALUE is never echoed back. These are
# declared AFTER the literal /credentials/github routes so github continues to
# use its validating handlers; the {provider} param routes serve the rest.


class SetConnectorCredentialRequest(BaseModel):
    fields: dict[str, str] = {}
    scope: str = "project"  # "project" | "global"
    allow_live: bool = False  # explicit opt-in to store a non-test Stripe key


def _require_known_provider(provider: str) -> None:
    if provider not in credentials.PROVIDERS:
        raise HTTPException(status_code=404, detail=f"unknown provider: {provider}")


@app.get("/api/projects/{project_id}/connectors")
def api_connectors_status(project_id: str):
    """Presence status for every registered provider (no secret values)."""
    _require_project_or_general(project_id)
    return credentials.status_all(project_id)


@app.get("/api/projects/{project_id}/credentials/{provider}")
def api_get_connector_credential(project_id: str, provider: str):
    _require_project_or_general(project_id)
    _require_known_provider(provider)
    return credentials.status(project_id, provider)


@app.post("/api/projects/{project_id}/credentials/{provider}")
def api_set_connector_credential(project_id: str, provider: str, req: SetConnectorCredentialRequest):
    _require_project_or_general(project_id)
    _require_known_provider(provider)
    scope = req.scope if req.scope in ("project", "global") else "project"
    fields = {k: v for k, v in (req.fields or {}).items() if isinstance(v, str) and v.strip()}
    if not fields:
        raise HTTPException(status_code=400, detail="at least one field value is required")
    try:
        credentials.set_credential(
            provider,
            None if scope == "global" else project_id,
            fields=fields,
            scope=scope,
            allow_live=bool(req.allow_live),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return credentials.status(None if scope == "global" else project_id, provider)


@app.delete("/api/projects/{project_id}/credentials/{provider}")
def api_delete_connector_credential(project_id: str, provider: str, scope: str = "project"):
    _require_project_or_general(project_id)
    _require_known_provider(provider)
    scope = scope if scope in ("project", "global") else "project"
    return credentials.delete_credential(
        provider, None if scope == "global" else project_id, scope=scope
    )


# ----- app-env registry (Phase 8: the BUILT app's env vars; presence-only,
# values never echoed; pushed to Vercel via the env-set contract) -----


class SetEnvVarRequest(BaseModel):
    key: str
    value: str
    targets: list[str] | None = None
    secret: bool = True


@app.get("/api/projects/{project_id}/env")
def api_list_env(project_id: str):
    _require_project(project_id)
    return {"vars": app_env.list_env(project_id)}


@app.post("/api/projects/{project_id}/env")
def api_set_env(project_id: str, req: SetEnvVarRequest):
    _require_project(project_id)
    try:
        entry = app_env.set_env_var(
            project_id, req.key, req.value, targets=req.targets, secret=req.secret
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"entry": entry, "vars": app_env.list_env(project_id)}


@app.delete("/api/projects/{project_id}/env/{key}")
def api_delete_env(project_id: str, key: str):
    _require_project(project_id)
    removed = app_env.delete_env_var(project_id, key)
    return {"removed": removed, "vars": app_env.list_env(project_id)}


# ----- per-run diff (lazy, bounded, redacted at capture time) -----


@app.get("/api/projects/{project_id}/execution/runs/{run_id}/diff")
def api_get_run_diff(project_id: str, run_id: str):
    _require_workspace(project_id)
    _reject_general_repo_op(project_id)
    record = _load_run_record(project_id, run_id)
    patch = run_store.read_diff_patch(project_id, run_id)
    return {
        "run_id": run_id,
        "available": patch is not None,
        "diff_stat": record.diff_stat,
        "diff": patch or "",
    }


# ----- commit (local, explicit, secret-refusing) -----


class GitCommitRequest(BaseModel):
    message: str | None = None
    branch: str | None = None
    confirm: bool = False


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/git/commit")
def api_git_commit(project_id: str, run_id: str, req: GitCommitRequest):
    _require_workspace(project_id)
    _reject_general_repo_op(project_id)
    record = _load_run_record(project_id, run_id)

    status = git_ops.git_status(project_id)
    safe, refused = git_ops.partition_changes(project_id)
    message = (req.message or "").strip() or _generate_commit_message(project_id, record, safe)
    contract = {
        "action": "commit",
        "title": "Create commit",
        "external": False,
        "destructive": False,
        "branch": req.branch or status.branch,
        "files": safe,
        "refused": refused,
        "diff_stat": record.diff_stat,
        "message": message,
        "requires_confirmation": True,
    }
    if not req.confirm:
        return {"contract": contract, "applied": False, "run": record.model_dump()}

    record.git_state = "committing"
    run_store.write_run_json(project_id, run_id, record)
    try:
        if req.branch and req.branch != status.branch:
            ok, err = git_ops.create_branch(project_id, req.branch)
            if not ok:
                raise HTTPException(status_code=409, detail=f"could not create branch: {err}")
        result = git_ops.commit(project_id, message)
        if not result.committed:
            raise HTTPException(status_code=409, detail=result.error or "nothing to commit")
    finally:
        record.git_state = None
        run_store.write_run_json(project_id, run_id, record)

    record.commit_sha = result.sha
    record.branch = result.branch
    record.head_commit = result.sha
    run_store.write_run_json(project_id, run_id, record)
    run_store.rerender_result_md(project_id, run_id, record)
    run_store.append_event(
        project_id,
        run_id,
        {
            "type": "git_commit",
            "sha": (result.sha or "")[:12],
            "branch": result.branch,
            "files": len(result.files),
            "refused": len(result.refused),
        },
    )
    return {
        "contract": {**contract, "branch": result.branch},
        "applied": True,
        "commit_sha": result.sha,
        "refused": result.refused,
        "run": record.model_dump(),
    }


# ----- push (external — requires confirm) -----


class GitPushRequest(BaseModel):
    branch: str | None = None
    remote: str = "origin"
    owner: str | None = None
    repo: str | None = None
    confirm: bool = False


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/git/push")
def api_git_push(project_id: str, run_id: str, req: GitPushRequest):
    _require_workspace(project_id)
    _reject_general_repo_op(project_id)
    record = _load_run_record(project_id, run_id)

    status = git_ops.git_status(project_id)
    branch = req.branch or record.branch or status.branch
    remote_info = github_connector.get_remote(project_id, remote=req.remote)
    owner, repo = req.owner, req.repo
    if not remote_info and not (owner and repo):
        # The project's stored GitHub repo (set from the "GitHub repo" field in
        # the UI, kept as the github connector's default_remote) is the canonical
        # push target — so a push needs no owner/repo re-entry from any path.
        stored = credentials.get_metadata("github", "default_remote", project_id)
        if stored and "/" in stored:
            owner, repo = stored.split("/", 1)
    if not remote_info and not (owner and repo):
        # Lowest-friction fallback (Phase 8): a Vercel-linked project already
        # knows its connected GitHub repo — push to that same repo instead of
        # asking the user to re-specify it. Only consulted on the first push
        # (once origin is set, get_remote resolves it directly).
        link = vercel_connector.get_project_link(project_id)
        if link and link.get("type") == "github" and link.get("org") and link.get("repo"):
            owner, repo = link["org"], link["repo"]
    target = (
        f"{remote_info[0]}/{remote_info[1]}"
        if remote_info
        else (f"{owner}/{repo}" if owner and repo else None)
    )
    cred = credentials.status(project_id)
    contract = {
        "action": "push",
        "title": "Push branch to GitHub",
        "external": True,
        "destructive": False,
        "branch": branch,
        "remote": req.remote,
        "target": target,
        "token_configured": cred["configured"],
        "requires_confirmation": True,
    }
    if not req.confirm:
        return {"contract": contract, "applied": False, "run": record.model_dump()}

    if not branch:
        raise HTTPException(status_code=400, detail="no branch to push")
    if not cred["configured"]:
        raise HTTPException(
            status_code=400, detail="no GitHub token configured — connect GitHub first"
        )
    if not remote_info:
        if not (owner and repo):
            raise HTTPException(
                status_code=400,
                detail="no git remote configured; link a Vercel project, provide owner+repo, or set a remote first",
            )
        ok, info = github_connector.ensure_remote(project_id, owner, repo, remote=req.remote)
        if not ok:
            raise HTTPException(status_code=409, detail=f"could not set remote: {info}")
        target = f"{owner}/{repo}"

    record.git_state = "pushing"
    run_store.write_run_json(project_id, run_id, record)
    push = github_connector.push_branch(project_id, branch, remote=req.remote)
    record.git_state = None
    if not push.ok:
        run_store.write_run_json(project_id, run_id, record)
        raise HTTPException(status_code=502, detail=push.error or "push failed")

    record.pushed = True
    record.branch = branch
    run_store.write_run_json(project_id, run_id, record)
    run_store.rerender_result_md(project_id, run_id, record)
    # Remember where this project pushes so the UI can show a clickable repo link
    # afterward (idempotent; never stores a token).
    if target and "/" in target:
        try:
            credentials.set_credential(
                "github", project_id, fields={"default_remote": target}, scope="project"
            )
        except Exception:  # noqa: BLE001 — best-effort, never fail a push
            pass
    run_store.append_event(
        project_id,
        run_id,
        {"type": "git_push", "branch": branch, "remote": req.remote, "target": target},
    )
    return {"contract": {**contract, "target": target}, "applied": True, "run": record.model_dump()}


# ----- pull request (external — requires confirm) -----


class GitPrRequest(BaseModel):
    title: str | None = None
    body: str | None = None
    base: str = "main"
    head: str | None = None
    confirm: bool = False


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/github/pr")
def api_github_pr(project_id: str, run_id: str, req: GitPrRequest):
    _require_workspace(project_id)
    _reject_general_repo_op(project_id)
    record = _load_run_record(project_id, run_id)

    remote_info = github_connector.get_remote(project_id)
    owner, repo = (remote_info or (None, None))
    head = req.head or record.branch or git_ops.current_branch(project_id)
    base_line = (record.summary or record.task_title or "Agent OS changes").strip().splitlines()
    title = (req.title or "").strip() or (base_line[0][:72] if base_line else "Agent OS changes")
    body_text = (req.body or "").strip() or _generate_pr_body(record)
    contract = {
        "action": "pr",
        "title": "Open pull request",
        "external": True,
        "destructive": False,
        "pr_title": title,
        "base": req.base,
        "head": head,
        "target": f"{owner}/{repo}" if owner else None,
        "pushed": record.pushed,
        "requires_confirmation": True,
    }
    if not req.confirm:
        return {"contract": contract, "applied": False, "run": record.model_dump()}

    if not record.pushed:
        raise HTTPException(status_code=409, detail="push the branch before opening a PR")
    if not (owner and repo):
        raise HTTPException(status_code=400, detail="no GitHub remote configured")
    if not head:
        raise HTTPException(status_code=400, detail="no head branch for the PR")

    record.git_state = "opening_pr"
    run_store.write_run_json(project_id, run_id, record)
    pr = github_connector.create_pull_request(
        project_id, owner=owner, repo=repo, head=head, base=req.base, title=title, body=body_text
    )
    record.git_state = None
    if not pr.ok:
        run_store.write_run_json(project_id, run_id, record)
        raise HTTPException(status_code=502, detail=pr.error or "pull request creation failed")

    record.pr_url = pr.url
    record.pr_number = pr.number
    run_store.write_run_json(project_id, run_id, record)
    run_store.rerender_result_md(project_id, run_id, record)
    run_store.append_event(
        project_id,
        run_id,
        {"type": "github_pr", "url": pr.url, "number": pr.number},
    )
    return {"contract": contract, "applied": True, "run": record.model_dump()}


# ----- rollback (destructive — requires confirm) -----


class GitRollbackRequest(BaseModel):
    confirm: bool = False


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/git/rollback")
def api_git_rollback(project_id: str, run_id: str, req: GitRollbackRequest):
    _require_workspace(project_id)
    _reject_general_repo_op(project_id)
    record = _load_run_record(project_id, run_id)
    if not record.base_commit:
        raise HTTPException(status_code=409, detail="no pre-run checkpoint to roll back to")

    contract = {
        "action": "rollback",
        "title": "Roll back to pre-run checkpoint",
        "external": False,
        "destructive": True,
        "target": record.base_commit[:12],
        "checkpoint": (record.pre_run_checkpoint or "")[:12],
        "summary": "Discards this run's changes and restores the repo to its pre-run state.",
        "requires_confirmation": True,
    }
    if not req.confirm:
        return {"contract": contract, "applied": False, "run": record.model_dump()}

    record.git_state = "rolling_back"
    run_store.write_run_json(project_id, run_id, record)
    result = git_ops.rollback(
        project_id, base_commit=record.base_commit, checkpoint_ref=record.pre_run_checkpoint
    )
    record.git_state = None
    run_store.write_run_json(project_id, run_id, record)
    if not result.rolled_back:
        raise HTTPException(status_code=409, detail=result.error or "rollback failed")
    run_store.append_event(
        project_id,
        run_id,
        {"type": "git_rollback", "target": (record.base_commit or "")[:12]},
    )
    return {"contract": contract, "applied": True, "run": record.model_dump()}


# --- Production Path endpoints (Phase 8 — Vercel deploy lifecycle) ---
# Each external action is a two-phase External Action Contract (mirrors Phase 7
# Git): ``confirm: false`` returns a preview, ``confirm: true`` executes. Deploy
# / redeploy / rollback are RUN-scoped (they ship the commit a run produced —
# the gitSource deploy needs a prior GitHub push). Env-set / status are
# project-scoped. GENERAL is rejected for every mutating action. Secrets reach
# Vercel only via the Authorization header inside the connector; values pushed
# as env vars are read once via ``credentials.get_env_value`` at action time and
# never echoed into a contract / event / log. A deploy finalizes OFF-THREAD
# (a Vercel build is minutes) — the confirm returns immediately with
# ``deploy_state`` set and the UI polls ``vercel/status`` / the run.

_DEPLOY_POLL_INTERVAL = 5
_DEPLOY_POLL_MAX = 300  # seconds — a stuck poll never blocks a worker forever


def _utc_stamp() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%MZ")


def _read_record_safe(project_id: str, run_id: str) -> RunRecord | None:
    raw = run_store.read_run_json(project_id, run_id)
    if raw is None:
        return None
    try:
        return RunRecord(**raw)
    except Exception:  # noqa: BLE001
        return None


def _finalize_vercel_deploy(
    project_id: str,
    run_id: str,
    target: str,
    *,
    git_ref: str | None = None,
    source_deployment_id: str | None = None,
    with_latest_commit: bool = False,
    kind: str = "deploy",
) -> None:
    """Create a Vercel deployment + poll to READY, then stamp the run + OPS
    ledger. Runs on the background pool. Best-effort: never raises (the manager's
    ``submit`` wrapper also guards). Clears the transient ``deploy_state`` in
    every outcome so the run never sticks 'deploying'."""
    # A valid project-name slug; the connector also sends `project` (the id),
    # which overrides `name` on the Vercel API.
    name = project_id
    log_lines = [f"[{_utc_stamp()}] {kind} target={target} ref={git_ref or '-'} src={source_deployment_id or '-'}"]
    if source_deployment_id:
        res = vercel_connector.create_deployment(
            project_id, name=name, target=target,
            deployment_id=source_deployment_id, with_latest_commit=with_latest_commit,
        )
    else:
        res = vercel_connector.create_deployment(project_id, name=name, target=target, git_ref=git_ref)
    dep_id, state, url, error = res.deployment_id, res.ready_state, res.url, res.error
    log_lines.append(f"created: id={dep_id} state={state} url={url or '-'} err={error or '-'}")
    elapsed = 0
    while dep_id and not error and state not in vercel_connector.TERMINAL_STATES and elapsed < _DEPLOY_POLL_MAX:
        time.sleep(_DEPLOY_POLL_INTERVAL)
        elapsed += _DEPLOY_POLL_INTERVAL
        g = vercel_connector.get_deployment(project_id, dep_id)
        if g.ok:
            state = g.ready_state or state
            url = g.url or url
        else:
            error = g.error or error
        log_lines.append(f"poll +{elapsed}s: state={state} url={url or '-'}")

    # Compute the deploy-owned outcome, then fold ONLY those fields onto a fresh
    # record under the per-run lock — so a git commit/push the user made on this
    # run during the (up to 300s) poll is preserved, not clobbered by a stale
    # write. Clearing deploy_state/external_state in every branch keeps the run
    # from sticking 'deploying'.
    blocker_msg: str | None = None
    if dep_id and state == "READY":
        pass
    elif state in ("ERROR", "CANCELED", "DELETED"):
        blocker_msg = credentials.redact(f"Vercel {kind} failed: {error or state}", project_id)
    elif dep_id and error:
        # The deployment was created but a (typically transient) network error
        # broke the READY poll — the deployment itself may still be building or
        # already live on Vercel. Keep its identity on the record: discarding it
        # orphans a real deployment from the run, the Links panel, and
        # redeploy/rollback (observed live when a flaky network killed the poll
        # while the deployment went on to reach READY).
        blocker_msg = credentials.redact(
            f"Vercel {kind} status polling failed ({error}) — the deployment may "
            "still be in progress; verify in the Vercel dashboard",
            project_id,
        )
    elif dep_id:
        # Created but not confirmed READY before the cap — record it, flag verify.
        blocker_msg = f"Vercel {kind} did not reach READY before timeout — verify in the Vercel dashboard"
    else:
        blocker_msg = credentials.redact(f"Vercel {kind} failed: {error or 'no deployment id returned'}", project_id)
    # Stamp the deployment identity whenever one was actually created and did
    # not definitively fail — a poll error must not lose a live deployment. A
    # definitively failed deployment stays unstamped so a fresh deploy on this
    # run isn't blocked by the already-has-a-deployment guard.
    stamp_dep = bool(dep_id and state not in ("ERROR", "CANCELED", "DELETED"))

    def _settle(r: RunRecord):
        r.deploy_state = None
        r.external_state = None
        if stamp_dep:
            r.deployment_id = dep_id
            r.deployment_url = url
            r.deployment_target = target
        if blocker_msg and blocker_msg not in r.blockers:
            r.blockers = list(r.blockers) + [blocker_msg]
        return r

    record = run_store.mutate_run_json(project_id, run_id, _settle)
    if record is None:
        return
    run_store.write_deploy_log(project_id, run_id, credentials.redact("\n".join(log_lines), project_id))
    run_store.write_deployment_json(
        project_id, run_id,
        {
            "kind": kind, "target": target, "deployment_id": record.deployment_id,
            "url": record.deployment_url, "ready_state": state,
            "settled_at": _utc_stamp(), "error": blocker_msg,
        },
    )
    run_store.append_event(
        project_id, run_id,
        {"type": "deploy_settled", "kind": kind, "ready_state": state, "deployment_id": record.deployment_id},
    )
    try:
        run_store.rerender_result_md(project_id, run_id, record)
    except Exception:  # noqa: BLE001
        pass
    if record.deployment_id:
        ops_ledger.append_ops_entry(
            project_id, kind, f"Vercel {kind} ({target})",
            {
                "target": f"vercel:{target}",
                "deployment_id": record.deployment_id,
                "preview_url": record.deployment_url,
                "commit": (record.commit_sha or record.head_commit or None),
                "ready_state": state,
            },
            timestamp=_utc_stamp(),
            dedup_key=record.deployment_id,
        )


def reconcile_stuck_external_actions() -> list[str]:
    """At startup, fix runs left with a transient external sub-status
    (``deploy_state`` / ``external_state``) by a process that died mid-action.

    For a stuck DEPLOY with a known deployment id we query Vercel for the true
    state and stamp the URL if it actually reached READY. Otherwise we clear the
    transient and record a "verify remote state" blocker — we NEVER auto-retry an
    external action that may have partially applied (consistent with "a crashed
    run never auto-recovers"). Best-effort: a failure here never blocks startup.
    """
    fixed: list[str] = []
    try:
        projects = [d.name for d in PROJECTS_DIR.iterdir() if d.is_dir()] if PROJECTS_DIR.exists() else []
    except Exception:  # noqa: BLE001
        return fixed
    for pid in projects:
        try:
            runs = run_store.list_runs(pid)
        except Exception:  # noqa: BLE001
            continue
        for raw in runs:
            if not (raw.get("deploy_state") or raw.get("external_state")):
                continue
            rid = raw.get("run_id")
            rec = _read_record_safe(pid, rid) if rid else None
            if rec is None:
                continue
            note: str | None = None
            if rec.deploy_state and rec.deployment_id:
                g = vercel_connector.get_deployment(pid, rec.deployment_id)
                if g.ok and g.ready_state == "READY":
                    rec.deployment_url = g.url or rec.deployment_url
                    rec.deployment_target = rec.deployment_target or g.target
                else:
                    note = "deploy did not confirm READY before a restart — verify in the Vercel dashboard"
            elif rec.deploy_state:
                note = "a deploy was interrupted by a restart — verify in the Vercel dashboard before retrying"
            elif rec.external_state:
                note = (
                    f"an external action ({rec.external_state}) was interrupted by a restart — it may have "
                    "partially applied; verify the remote state before retrying"
                )
            rec.deploy_state = None
            rec.external_state = None
            if note and note not in rec.blockers:
                rec.blockers = list(rec.blockers) + [note]
            try:
                run_store.write_run_json(pid, rid, rec)
                fixed.append(rid)
            except Exception:  # noqa: BLE001
                pass
    return fixed


@app.get("/api/projects/{project_id}/vercel/status")
def api_vercel_status(project_id: str):
    _require_project_or_general(project_id)
    return vercel_connector.status(project_id).to_dict()


@app.get("/api/projects/{project_id}/vercel/deployments")
def api_vercel_deployments(project_id: str):
    _require_project(project_id)
    deps, err = vercel_connector.list_deployments(project_id)
    return {"deployments": deps, "error": err}


class VercelDeployRequest(BaseModel):
    environment: str = "preview"  # preview | production
    confirm: bool = False


def _claim_deploy(
    project_id: str,
    run_id: str,
    state: str,
    *,
    allow_existing_deployment: bool = False,
) -> dict:
    """Atomically set ``deploy_state``/``external_state`` under the per-run lock.

    Returns ``{"record": RunRecord}`` on a successful claim or
    ``{"error": <message>}`` if a deploy is already in flight (or the run already
    has a deployment and that isn't allowed). Serializing the read-check-set
    stops two concurrent confirms from both launching a real Vercel deployment.
    """
    outcome: dict = {}

    def _apply(r: RunRecord):
        if r.deploy_state:
            outcome["error"] = "a deploy is already in flight for this run"
            return r
        if r.deployment_id and not allow_existing_deployment:
            outcome["error"] = "this run already has a deployment; use redeploy or rollback"
            return r
        r.deploy_state = state
        r.external_state = state
        outcome["record"] = r
        return r

    updated = run_store.mutate_run_json(project_id, run_id, _apply)
    if updated is None and "error" not in outcome:
        outcome["error"] = "run record is unavailable"
    return outcome


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/vercel/deploy")
def api_vercel_deploy(project_id: str, run_id: str, req: VercelDeployRequest):
    _require_workspace(project_id)
    _reject_general_repo_op(project_id)
    record = _load_run_record(project_id, run_id)
    target = req.environment if req.environment in ("preview", "production") else "preview"
    vstatus = vercel_connector.status(project_id)
    git_ref = record.branch or "main"
    contract = {
        "action": "deploy",
        "title": f"Deploy to Vercel ({target})",
        "external": True,
        "destructive": target == "production",
        "target": target,
        "token_configured": vstatus.configured,
        "linked": bool(vstatus.project_id),
        "git_ref": git_ref,
        "commit": (record.commit_sha or record.head_commit or "")[:12] or None,
        "requires_confirmation": True,
    }
    if not req.confirm:
        return {"contract": contract, "applied": False, "run": record.model_dump()}
    if not vstatus.configured:
        raise HTTPException(status_code=400, detail="no Vercel token configured (connect Vercel first)")
    if not vstatus.project_id:
        raise HTTPException(status_code=400, detail="no Vercel project linked (set project_id in the Vercel connector)")
    if not (record.commit_sha or record.head_commit or record.pushed):
        raise HTTPException(status_code=409, detail="push the commit to GitHub before deploying (gitSource deploy)")
    # Atomically claim the deploy under the per-run lock so two concurrent
    # confirms (double-click / retry) can't both pass the guard and launch two
    # real (billable) Vercel deployments. Only the winner submits the finalizer.
    claim = _claim_deploy(project_id, run_id, "deploying")
    if claim.get("error"):
        raise HTTPException(status_code=409, detail=claim["error"])
    record = claim["record"]
    run_store.append_event(project_id, run_id, {"type": "deploy_started", "target": target})
    get_default_manager().submit(
        _finalize_vercel_deploy, project_id, run_id, target, git_ref=git_ref, kind="deploy"
    )
    return {"contract": contract, "applied": True, "async": True, "run": record.model_dump()}


class VercelRedeployRequest(BaseModel):
    deployment_id: str
    with_latest_commit: bool = False
    environment: str = "preview"
    confirm: bool = False


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/vercel/redeploy")
def api_vercel_redeploy(project_id: str, run_id: str, req: VercelRedeployRequest):
    _require_workspace(project_id)
    _reject_general_repo_op(project_id)
    record = _load_run_record(project_id, run_id)
    target = req.environment if req.environment in ("preview", "production") else "preview"
    vstatus = vercel_connector.status(project_id)
    contract = {
        "action": "redeploy",
        "title": f"Redeploy on Vercel ({target})",
        "external": True,
        "destructive": target == "production",
        "target": target,
        "source_deployment_id": req.deployment_id,
        "with_latest_commit": req.with_latest_commit,
        "token_configured": vstatus.configured,
        "linked": bool(vstatus.project_id),
        "requires_confirmation": True,
    }
    if not req.confirm:
        return {"contract": contract, "applied": False, "run": record.model_dump()}
    if not vstatus.configured or not vstatus.project_id:
        raise HTTPException(status_code=400, detail="Vercel not configured/linked")
    # Atomically claim (same race guard as deploy). Redeploy tolerates an
    # existing deployment_id (that's its input), so only deploy_state is checked.
    claim = _claim_deploy(project_id, run_id, "redeploying", allow_existing_deployment=True)
    if claim.get("error"):
        raise HTTPException(status_code=409, detail=claim["error"])
    record = claim["record"]
    run_store.append_event(project_id, run_id, {"type": "redeploy_started", "source": req.deployment_id})
    get_default_manager().submit(
        _finalize_vercel_deploy, project_id, run_id, target,
        source_deployment_id=req.deployment_id, with_latest_commit=req.with_latest_commit, kind="redeploy",
    )
    return {"contract": contract, "applied": True, "async": True, "run": record.model_dump()}


class VercelRollbackRequest(BaseModel):
    target_deployment_id: str
    confirm: bool = False


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/vercel/rollback")
def api_vercel_rollback(project_id: str, run_id: str, req: VercelRollbackRequest):
    _require_workspace(project_id)
    _reject_general_repo_op(project_id)
    record = _load_run_record(project_id, run_id)
    vstatus = vercel_connector.status(project_id)
    contract = {
        "action": "rollback",
        "title": "Roll back Vercel production",
        "external": True,
        "destructive": True,
        "target": req.target_deployment_id,
        "current": record.deployment_id,
        "token_configured": vstatus.configured,
        "linked": bool(vstatus.project_id),
        "summary": "Re-points production traffic to a previous deployment (instant, no rebuild).",
        "requires_confirmation": True,
    }
    if not req.confirm:
        return {"contract": contract, "applied": False, "run": record.model_dump()}
    if not vstatus.configured or not vstatus.project_id:
        raise HTTPException(status_code=400, detail="Vercel not configured/linked")
    record.deploy_state = "rolling_back"
    record.external_state = "rolling_back"
    run_store.write_run_json(project_id, run_id, record)
    res = vercel_connector.promote_deployment(project_id, req.target_deployment_id)
    record.deploy_state = None
    record.external_state = None
    if not res.ok:
        run_store.write_run_json(project_id, run_id, record)
        raise HTTPException(status_code=502, detail=res.error or "rollback failed")
    record.deployment_id = req.target_deployment_id
    record.deployment_target = "production"
    run_store.write_run_json(project_id, run_id, record)
    run_store.rerender_result_md(project_id, run_id, record)
    run_store.append_event(project_id, run_id, {"type": "deploy_rollback", "target": req.target_deployment_id})
    ops_ledger.append_ops_entry(
        project_id, "rollback", "Vercel rollback (production)",
        {"target": "vercel:production", "deployment_id": req.target_deployment_id},
        timestamp=_utc_stamp(),
    )
    return {"contract": contract, "applied": True, "run": record.model_dump()}


class VercelEnvSetRequest(BaseModel):
    key: str
    confirm: bool = False


@app.post("/api/projects/{project_id}/vercel/env/set")
def api_vercel_env_set(project_id: str, req: VercelEnvSetRequest):
    _require_project(project_id)
    entries = {e["key"]: e for e in app_env.list_env(project_id)}
    entry = entries.get(req.key)
    if not entry:
        raise HTTPException(status_code=404, detail=f"env var {req.key} is not in the registry")
    vstatus = vercel_connector.status(project_id)
    # H4: a secret app-env var is FORCED to type "sensitive" (write-only on
    # Vercel, not readable back / not in build logs); only an explicitly public
    # var may be "plain".
    var_type = "sensitive" if entry["secret"] else "plain"
    contract = {
        "action": "env_set",
        "title": f"Push env var {req.key} to Vercel",
        "external": True,
        "destructive": False,
        "key": req.key,
        "targets": entry["targets"],
        "type": var_type,
        "value_configured": entry["is_set"],  # never the value itself
        "token_configured": vstatus.configured,
        "linked": bool(vstatus.project_id),
        "requires_confirmation": True,
    }
    if not req.confirm:
        return {"contract": contract, "applied": False}
    if not vstatus.configured or not vstatus.project_id:
        raise HTTPException(status_code=400, detail="Vercel not configured/linked")
    if not entry["is_set"]:
        raise HTTPException(status_code=400, detail="env var has no value set in the registry")
    value = credentials.get_env_value(project_id, req.key)  # the ONLY value read
    res = vercel_connector.set_env_var(
        project_id, req.key, value or "", targets=entry["targets"], var_type=var_type
    )
    if not res.ok:
        raise HTTPException(status_code=502, detail=res.error or "env var push failed")
    ops_ledger.append_ops_entry(
        project_id, "env_set", f"Pushed {req.key} to Vercel",
        {"key": req.key, "targets": ",".join(entry["targets"]), "type": var_type},
        timestamp=_utc_stamp(),
    )
    return {"contract": {**contract, "env_id": res.env_id}, "applied": True}


# --- Production Path endpoints (Phase 8 — Supabase migrations / link / Auth) ---
# Migrations route through the sandboxed ``run_supabase`` executor (never raw
# subprocess); the access token + DB password reach it via env only. ``db push``
# (apply) is the one destructive/external mutation — a two-phase contract whose
# preview runs ``db push --dry-run`` (Docker-optional) + a best-effort ``db diff``
# (Docker-gated). GENERAL is rejected; Auth provider config stays a manual
# dashboard step for this pass (RLS ships as migration SQL).


@app.get("/api/projects/{project_id}/supabase/status")
def api_supabase_status(project_id: str):
    _require_project_or_general(project_id)
    return supabase_connector.status(project_id).to_dict()


class SupabaseLinkRequest(BaseModel):
    project_ref: str
    confirm: bool = False


@app.post("/api/projects/{project_id}/supabase/link")
def api_supabase_link(project_id: str, req: SupabaseLinkRequest):
    _require_workspace(project_id)
    _reject_general_repo_op(project_id)
    ref = (req.project_ref or "").strip()
    cred = credentials.status(project_id, "supabase")
    contract = {
        "action": "link_project",
        "title": "Link Supabase project",
        "external": True,
        "destructive": False,
        "target": ref,
        "token_configured": cred["configured"],
        "summary": "Binds this repo to the hosted Supabase project; the DB password is used via env (never shown).",
        "requires_confirmation": True,
    }
    if not req.confirm:
        return {"contract": contract, "applied": False}
    if not ref:
        raise HTTPException(status_code=400, detail="project_ref is required")
    if not cred["configured"]:
        raise HTTPException(status_code=400, detail="no Supabase access token configured")
    res = supabase_connector.link(project_id, ref)
    if not res.ok:
        if res.not_installed:
            raise HTTPException(status_code=400, detail="supabase CLI not installed (npm i -g supabase)")
        detail = "Docker is required but not running" if res.docker_missing else (res.error or "link failed")
        raise HTTPException(status_code=502, detail=detail)
    credentials.update_metadata("supabase", project_id, {"project_ref": ref})
    ops_ledger.append_ops_entry(
        project_id, "link", "Supabase project linked", {"project_ref": ref}, timestamp=_utc_stamp()
    )
    return {"contract": contract, "applied": True}


class SupabaseMigrationRequest(BaseModel):
    include_seed: bool = False
    confirm: bool = False


@app.post("/api/projects/{project_id}/execution/runs/{run_id}/supabase/migration")
def api_supabase_migration(project_id: str, run_id: str, req: SupabaseMigrationRequest):
    _require_workspace(project_id)
    _reject_general_repo_op(project_id)
    record = _load_run_record(project_id, run_id)
    cred = credentials.status(project_id, "supabase")
    ref = credentials.get_metadata("supabase", "project_ref", project_id)
    # Preview: dry-run lists the pending migrations (Docker-optional); diff adds
    # the exact SQL when Docker is available (best-effort).
    preview = supabase_connector.migration_preview(project_id)
    diff = supabase_connector.migration_diff(project_id)
    contract = {
        "action": "migration_apply",
        "title": "Apply Supabase migrations (linked DB)",
        "external": True,
        "destructive": True,
        "target": ref,
        "token_configured": cred["configured"],
        "pending": (preview.output[:4000] if preview.ok else (preview.error or "")[:1000]),
        "diff_available": diff.ok,
        "diff": (diff.output[:4000] if diff.ok else None),
        "docker_note": (
            "exact SQL diff unavailable: Docker not running"
            if (diff.docker_missing and not diff.ok) else None
        ),
        "include_seed": req.include_seed,
        "summary": "Applies pending repo/supabase/migrations to the LINKED remote database (forward-only).",
        "requires_confirmation": True,
    }
    if not req.confirm:
        return {"contract": contract, "applied": False, "run": record.model_dump()}
    if not cred["configured"]:
        raise HTTPException(status_code=400, detail="no Supabase access token configured")
    if not ref:
        raise HTTPException(status_code=400, detail="no Supabase project linked (run supabase/link first)")
    record.external_state = "migrating"
    run_store.write_run_json(project_id, run_id, record)
    res = supabase_connector.migration_apply(project_id, include_seed=req.include_seed)
    record.external_state = None
    run_store.write_run_json(project_id, run_id, record)
    if not res.ok:
        if res.not_installed:
            raise HTTPException(status_code=400, detail="supabase CLI not installed (npm i -g supabase)")
        detail = "Docker is required but not running" if res.docker_missing else (res.error or "migration failed")
        raise HTTPException(status_code=502, detail=detail)
    run_store.append_event(project_id, run_id, {"type": "supabase_migration", "target": ref})
    ops_ledger.append_ops_entry(
        project_id, "migration", "Supabase migration applied",
        {"project_ref": ref, "include_seed": req.include_seed},
        timestamp=_utc_stamp(),
    )
    return {"contract": contract, "applied": True, "run": record.model_dump()}


# --- Production Path endpoints (Phase 8 — Stripe test-mode checkout + webhooks) ---
# Provisioning (test Product/Price, deployed webhook endpoint) is contract-first;
# the per-purchase checkout session + signature verification are app-runtime code
# inside the BUILT app (which reads its own process.env, never credentials.py).
# The test-mode gate lives at the connector per request (a live key / livemode
# response is refused) — routes are always mounted (no import-time theater). The
# returned whsec_ is stored + never echoed. GENERAL rejected for mutations.


@app.get("/api/projects/{project_id}/stripe/status")
def api_stripe_status(project_id: str):
    _require_project_or_general(project_id)
    return stripe_connector.status(project_id).to_dict()


@app.get("/api/projects/{project_id}/stripe/webhook/local-command")
def api_stripe_local_command(project_id: str):
    _require_project_or_general(project_id)
    port = 5174
    try:
        pv = preview.get_preview_status(project_id)
        if isinstance(pv, dict) and pv.get("url"):
            m = re.search(r":(\d+)", str(pv.get("url")))
            if m:
                port = int(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return stripe_connector.local_webhook_command(port=port)


class StripeCheckoutTestRequest(BaseModel):
    name: str = "Phase 8 Test Product"
    amount: int = 1000  # smallest currency unit (cents)
    currency: str = "usd"
    mode: str = "payment"  # payment | subscription
    confirm: bool = False


@app.post("/api/projects/{project_id}/stripe/checkout-test")
def api_stripe_checkout_test(project_id: str, req: StripeCheckoutTestRequest):
    _require_project(project_id)
    cred = credentials.status(project_id, "stripe")
    contract = {
        "action": "checkout_test",
        "title": "Provision a Stripe test product + price",
        "external": True,
        "destructive": False,
        "mode": "test",
        "live_gate_passed": True,
        "name": req.name,
        "amount": req.amount,
        "currency": req.currency,
        "checkout_mode": req.mode,
        "token_configured": cred["configured"],
        "requires_confirmation": True,
    }
    if not req.confirm:
        return {"contract": contract, "applied": False}
    if not cred["configured"]:
        raise HTTPException(status_code=400, detail="no Stripe test key configured")
    res = stripe_connector.provision_price(
        project_id,
        name=req.name,
        unit_amount=req.amount,
        currency=req.currency,
        recurring_interval=("month" if req.mode == "subscription" else None),
    )
    if not res.ok:
        raise HTTPException(status_code=502, detail=res.error or "provisioning failed")
    ops_ledger.append_ops_entry(
        project_id, "stripe", f"Stripe test price provisioned ({req.mode})",
        {"product_id": res.product_id, "price_id": res.price_id, "mode": "test"},
        timestamp=_utc_stamp(), dedup_key=res.price_id,
    )
    return {"contract": {**contract, "price_id": res.price_id, "product_id": res.product_id}, "applied": True}


class StripeWebhookRegisterRequest(BaseModel):
    url: str
    enabled_events: list[str] | None = None
    confirm: bool = False


@app.post("/api/projects/{project_id}/stripe/webhook/register")
def api_stripe_webhook_register(project_id: str, req: StripeWebhookRegisterRequest):
    _require_project(project_id)
    cred = credentials.status(project_id, "stripe")
    events = req.enabled_events or ["checkout.session.completed"]
    contract = {
        "action": "webhook_register",
        "title": "Register a Stripe webhook endpoint",
        "external": True,
        "destructive": False,
        "mode": "test",
        "url": req.url,
        "enabled_events": events,
        "livemode": False,
        "token_configured": cred["configured"],
        "summary": "Registers the deployed endpoint; the returned signing secret is stored, never shown.",
        "requires_confirmation": True,
    }
    if not req.confirm:
        return {"contract": contract, "applied": False}
    if not cred["configured"]:
        raise HTTPException(status_code=400, detail="no Stripe test key configured")
    if not (req.url or "").startswith("http"):
        raise HTTPException(status_code=400, detail="a public https endpoint URL is required")
    res = stripe_connector.register_webhook(project_id, req.url, events)
    if not res.ok:
        raise HTTPException(status_code=502, detail=res.error or "webhook registration failed")
    ops_ledger.append_ops_entry(
        project_id, "webhook", "Stripe webhook registered (test)",
        {"endpoint_id": res.endpoint_id, "url": res.url, "events": ",".join(res.events or [])},
        timestamp=_utc_stamp(), dedup_key=res.endpoint_id,
    )
    # endpoint id + secret_stored only — whsec_ is never returned
    return {"contract": {**contract, "endpoint_id": res.endpoint_id, "secret_stored": res.secret_stored}, "applied": True}


@app.delete("/api/projects/{project_id}/stripe/webhook/{endpoint_id}")
def api_stripe_webhook_delete(project_id: str, endpoint_id: str):
    _require_project(project_id)
    res = stripe_connector.delete_webhook(project_id, endpoint_id)
    if not res.ok:
        raise HTTPException(status_code=502, detail=res.error or "delete failed")
    return {"deleted": True, "endpoint_id": endpoint_id}


# --- Main-agent file inspection endpoints (Task 06.1) ---
# These are the on-demand inspection surface for the main agent and any
# frontend UI that wants to surface workspace files alongside chat. Same
# sandbox + ToolRuntime as the Coding Agent uses, but with tighter caps
# and read-only operations only. The orchestrator's bounded chat loop
# (orchestrate() in orchestrator.py) also goes through these wrappers.


class InspectListRequest(BaseModel):
    path: str = "."


class InspectReadRequest(BaseModel):
    path: str


class InspectSearchRequest(BaseModel):
    query: str
    path: str = "."


@app.post("/api/projects/{project_id}/execution/inspect/list")
def api_inspect_list(project_id: str, req: InspectListRequest):
    _require_project(project_id)
    result = list_repo_files(project_id, req.path)
    return result.to_dict()


@app.post("/api/projects/{project_id}/execution/inspect/read")
def api_inspect_read(project_id: str, req: InspectReadRequest):
    _require_project(project_id)
    result = read_repo_file(project_id, req.path)
    return result.to_dict()


@app.post("/api/projects/{project_id}/execution/inspect/search")
def api_inspect_search(project_id: str, req: InspectSearchRequest):
    _require_project(project_id)
    result = search_repo_files(project_id, req.query, req.path)
    return result.to_dict()


# --- Local RAG (Phase 10.2) — bounded local retrieval over memory + runs + repo ---


class RetrieveRequest(BaseModel):
    query: str = ""
    kinds: list[str] | None = None


@app.post("/api/projects/{project_id}/retrieve")
def api_retrieve(project_id: str, req: RetrieveRequest):
    """Bounded, cited local retrieval (project memory + run history + repo map).
    The same engine the Main Agent's ``retrieve`` inspection tool uses — exposed
    for the UI and for auditability."""
    _require_project(project_id)
    from execution import local_rag

    result = local_rag.retrieve(project_id, req.query, kinds=req.kinds)
    return result.to_dict()


# --- Pending execution endpoints (Task 05.9.5) ---
# A pending execution is a confirmable plan produced by the LLM delegation
# judge. Confirming it dispatches a Coding Agent run via the same
# BackgroundRunManager path used by `@code`. The GENERAL workspace cannot
# create or confirm pending plans (no execution workspace exists there).


@app.get("/api/projects/{project_id}/execution/pending/{pending_id}")
def api_get_pending_execution(project_id: str, pending_id: str):
    """Fetch a pending plan. Used by the frontend when re-rendering messages
    after a page reload — the message's metadata carries the pending id and
    the UI keys off the row's current status."""
    _require_project(project_id)
    row = get_pending_execution(pending_id)
    if not row:
        raise HTTPException(status_code=404, detail="pending execution not found")
    if row["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="pending execution not found")
    return serialize_pending(row).to_dict()


class ConfirmExecutionRequest(BaseModel):
    # Phase 6.1 — the user may approve a bounded auto-recovery allowance when
    # confirming an execution contract: 0 (none) / 1 / 2 attempts. Clamped at
    # this boundary; this is the ONLY place a non-zero budget enters the system.
    recovery_budget: int = 0


@app.post("/api/projects/{project_id}/execution/pending/{pending_id}/confirm")
def api_confirm_pending_execution(
    project_id: str,
    pending_id: str,
    req: ConfirmExecutionRequest | None = Body(default=None),
):
    """Dispatch the stored task card via the same path as `@code`.

    Validates project + workspace, ensures the pending plan exists, is still
    in 'pending' state, and belongs to this project; then submits to the
    background run manager and marks the pending row as dispatched with the
    new run id. A short assistant message is appended to the chat so the
    user sees confirmation inline (matching the `@code` placeholder UX).

    Phase 6.1 — an optional ``recovery_budget`` (0/1/2) authorizes that many
    bounded auto-recovery passes if the run comes back non-green.
    """
    if project_id == GENERAL_PROJECT_ID:
        raise HTTPException(
            status_code=400,
            detail="Pending execution plans are not available in the GENERAL workspace.",
        )
    # Lazily initialize the execution workspace (idempotent) so the
    # natural-language confirm path works on a brand-new project — exactly like
    # the `@code` path (chat_delegation.handle_code_delegation). Without this,
    # BackgroundRunManager.dispatch() 404s because the workspace was never
    # created, so the pending plan's "OK, run this" button would dead-end.
    _require_project(project_id)
    init_execution_workspace(project_id, _project_display_name(project_id))
    _require_workspace(project_id)

    row = get_pending_execution(pending_id)
    if not row:
        raise HTTPException(status_code=404, detail="pending execution not found")
    if row["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="pending execution not found")
    if row["status"] != STATUS_PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Pending execution is {row['status']!r}; cannot dispatch.",
        )

    # Atomically claim the plan (pending -> dispatching) BEFORE dispatching, so
    # two concurrent confirms (double-click / two tabs) can't both start a run
    # against the same live repo/ tree. Only the claim winner proceeds; the loser
    # gets a 409 and no run is dispatched for it.
    if not claim_pending_execution(pending_id):
        raise HTTPException(
            status_code=409,
            detail="Pending execution is already being dispatched.",
        )

    plan = serialize_pending(row)
    spec = TaskSpec(
        title=plan.title,
        task_card=plan.task_card,
        created_by="pending_confirm",
    )
    # Clamp the user-approved recovery budget at the trust boundary (0..2).
    recovery_budget = max(0, min(2, (req.recovery_budget if req else 0) or 0))

    # Phase 11 — a recovery plan (propose-recovery) carries its parent run id;
    # thread the lineage through dispatch so a MANUALLY-confirmed recovery run
    # is linked, checkpoint-inherited, and budget-clamped exactly like an
    # auto-dispatched one. Parent missing/corrupt degrades to a normal
    # dispatch (defensive — the plan is still the user's explicit intent).
    dispatch_kwargs: dict = {"recovery_budget": recovery_budget}
    parent_record: RunRecord | None = None
    if plan.recovery_of:
        parent_raw = run_store.read_run_json(project_id, plan.recovery_of)
        if parent_raw is not None:
            try:
                parent_record = RunRecord(**parent_raw)
            except Exception:
                parent_record = None
        if parent_record is not None:
            if parent_record.recovered_by is not None:
                # One recovery per parent — a concurrent auto-recovery (or a
                # second confirmed proposal) already claimed this run.
                revert_pending_execution_to_pending(pending_id)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Run {plan.recovery_of} already has a recovery run "
                        f"({parent_record.recovered_by})."
                    ),
                )
            ra = parent_record.recovery_assessment
            recovery_type = (
                (ra.recovery_type if ra is not None else "")
                or classify_failure(parent_record).recovery_type
            )
            contract = contract_for(recovery_type)
            dispatch_kwargs = {
                # The contract can only TIGHTEN the child's onward budget —
                # e.g. a confirmed visual repair still gets exactly one pass.
                "recovery_budget": min(recovery_budget, contract.child_budget_cap),
                "recovery_of": plan.recovery_of,
                "orchestration_round": parent_record.orchestration_round + 1,
            }
            # Share the parent's rollback anchor — but only when one exists;
            # passing an all-None inherit would suppress the child's own
            # fresh checkpoint.
            if parent_record.pre_run_checkpoint:
                dispatch_kwargs["inherit_checkpoint"] = {
                    "ref": parent_record.pre_run_checkpoint,
                    "base": parent_record.base_commit,
                    "tag": parent_record.checkpoint_tag,
                    "branch": parent_record.branch,
                }
    try:
        record = get_default_manager().dispatch(project_id, spec, **dispatch_kwargs)
    except Exception:
        # Dispatch failed after we claimed — release the claim so the button
        # doesn't dead-end, then surface the failure.
        revert_pending_execution_to_pending(pending_id)
        raise

    # Claim the parent (recovered_by) under the per-run lock so the manual and
    # auto paths share one idempotency story; audited via an event.
    if plan.recovery_of and parent_record is not None:
        def _claim_parent(rec: RunRecord):
            if rec.recovered_by is None:
                rec.recovered_by = record.run_id
            return rec

        try:
            run_store.mutate_run_json(project_id, plan.recovery_of, _claim_parent)
            run_store.append_event(
                project_id,
                plan.recovery_of,
                {
                    "type": "manual_recovery_dispatched",
                    "child_run_id": record.run_id,
                    "recovery_type": (
                        (parent_record.recovery_assessment.recovery_type
                         if parent_record.recovery_assessment is not None else "")
                        or classify_failure(parent_record).recovery_type
                    ),
                    "orchestration_round": parent_record.orchestration_round + 1,
                },
            )
        except Exception:
            # Lineage is audit metadata — a claim/event failure must never
            # fail the dispatch that already happened.
            pass

    marked = mark_pending_execution_dispatched(pending_id, record.run_id)
    if not marked:
        # Should not happen (we hold the 'dispatching' claim), but keep the
        # row honest if it changed underneath us.
        raise HTTPException(
            status_code=409,
            detail=(
                "Run was dispatched but the pending plan changed state "
                "concurrently. Check the Runs panel."
            ),
        )

    # Drop an inline confirmation message into the chat. The live status,
    # completion summary, and browser-verification controls are rendered by
    # the chat-first run follow-up card, which keys off the ``run_id``
    # metadata below — so this is just the conversational lead-in (Task 06.2D).
    confirmation_body = (
        f"**Coding Agent is running** — _{record.task_title}_.\n\n"
        "I'll update this thread when the first build pass finishes."
    )
    add_message(
        plan.conversation_id,
        "assistant",
        confirmation_body,
        metadata={
            "pending_execution_id": plan.pending_execution_id,
            "run_id": record.run_id,
        },
    )

    return {
        "run": record.model_dump(),
        "pending_execution": serialize_pending(
            get_pending_execution(pending_id)  # type: ignore[arg-type]
        ).to_dict(),
    }
