from fastapi import FastAPI, HTTPException
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

# Load .env before anything that needs ANTHROPIC_API_KEY
load_dotenv(Path(__file__).resolve().parent / ".env")

from database import (
    init_db, create_conversation, list_conversations, get_conversation,
    list_messages, add_message, update_conversation_title, delete_conversation,
    delete_conversations_for_project, rename_project_conversations,
    create_pending_execution, get_pending_execution,
    update_pending_execution_plan, mark_pending_execution_dispatched,
)
from orchestrator import (
    orchestrate, load_memory, judge_memory_updates, apply_memory_updates, apply_memory_update,
    judge_global_memory_updates, apply_global_memory_updates, apply_global_memory_update,
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
    GENERAL_REJECTION_MESSAGE,
    judge_delegation,
    DECISION_DISPATCH,
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
from execution.models import TaskSpec
from execution import run_store
from execution.inspect import (
    list_repo_files,
    read_repo_file,
    search_repo_files,
)

app = FastAPI(title="Agent OS Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PROJECTS_DIR = Path(__file__).resolve().parent.parent / "projects"

MEMORY_FILES = ["PROJECT.md", "STATUS.md", "TASK_QUEUE.md", "DECISIONS.md", "RESEARCH.md"]


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


@app.on_event("shutdown")
def shutdown():
    # Best-effort: tear down the background run executor so the process exits
    # cleanly. In-flight runs are not awaited — their artifacts may end in an
    # inconsistent state if the server is killed mid-run.
    shutdown_default_manager(wait=False)


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
    "STATUS.md": "# Status: {name}\n\n## Current Phase\nPlanning\n\n## Latest Milestone\nProject created\n\n## What Works\n- Project folder initialized\n\n## Next Up\n- Define project scope and goals\n",
    "TASK_QUEUE.md": "# Task Queue: {name}\n\n## In Progress\n- [ ] Define project scope and requirements\n\n## Up Next\n- [ ] Set up initial project structure\n\n## Done\n- [x] Project created\n",
    "DECISIONS.md": "# Decisions: {name}\n\n(record important project decisions and their rationale here)\n",
    "RESEARCH.md": "# Research: {name}\n\n(record research findings, external references, and technical notes here)\n",
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


@app.get("/api/global-memory")
def api_get_global_memory():
    """Return the 3 writable global memory files (SOUL.md excluded)."""
    return load_global_memory()


class UpdateGlobalFileRequest(BaseModel):
    filename: str
    content: str


@app.post("/api/global-memory/update-file")
def api_update_global_file(req: UpdateGlobalFileRequest):
    """Manually update a global memory file."""
    if req.filename not in WRITABLE_GLOBAL_FILES:
        raise HTTPException(status_code=400, detail="Invalid filename or file is read-only")
    filepath = MEMORY_DIR / req.filename
    filepath.write_text(req.content, encoding="utf-8")
    return {"status": "ok"}


# --- Chat endpoint ---

class ChatRequest(BaseModel):
    conversation_id: str
    message: str
    # When the user clicked "Revise plan" on a pending execution and is now
    # typing revision instructions, the frontend echoes the pending id here
    # so the backend routes to the revision flow instead of the orchestrator.
    revise_pending_id: str | None = None


class ChatResponse(BaseModel):
    role: str
    content: str
    timestamp: str
    memory_updated: bool = False
    memory_updates: list[dict] = []
    # Populated when the assistant message has a confirmable execution plan
    # attached (Task 05.9.5). The frontend keys off `status` to decide
    # whether to render the OK/Revise buttons under the message.
    pending_execution: dict | None = None
    # Echo of the assistant message id so the frontend can correlate the
    # rendered message with the persisted row (used for re-rendering on
    # reload via message metadata).
    message_id: str | None = None
    # Task 06.1 — when the orchestrator inspected repo files to answer
    # the user, surface that list so the UI can clearly distinguish
    # answers grounded in memory from answers grounded in file reads.
    inspected_files: list[dict] = []


@app.post("/api/chat", response_model=ChatResponse)
def api_chat(req: ChatRequest):
    conv = get_conversation(req.conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    project_id = conv["project_id"]
    is_general = project_id == GENERAL_PROJECT_ID

    # Persist user message. We capture its id so the pending-execution row
    # can link back to the message that triggered it.
    user_msg = add_message(req.conversation_id, "user", req.message)

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
        if is_general:
            response_content = GENERAL_REJECTION_MESSAGE
        else:
            project_name = _project_display_name(project_id)
            response_content = handle_code_delegation(
                project_id, project_name, req.message
            )
        assistant_msg = add_message(req.conversation_id, "assistant", response_content)
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
        )

    history = [{"role": m["role"], "content": m["content"]} for m in messages]

    # --- Implicit delegation judge → confirmable plan (Task 05.9 + 05.9.5) ---
    # In project chats only, the LLM judge classifies the message. On
    # `dispatch_suggested` we create a pending execution plan, persist a
    # natural project-manager-style assistant message linked to it, and let
    # the user confirm or revise via UI buttons. `@code` and the confirm
    # endpoint are still the only paths that actually dispatch a run. The
    # GENERAL workspace has no execution workspace, so the judge is skipped.
    if not is_general:
        project_name = _project_display_name(project_id)
        decision = judge_delegation(
            project_id=project_id,
            project_name=project_name,
            user_message=req.message,
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

    # Generate orchestration response (with optional on-demand file inspection).
    response_content, inspected_files = orchestrate(
        project_id, req.message, history=history
    )

    # Persist assistant reply. When inspections happened, include them in the
    # message metadata so the chat history reflects how the answer was built.
    inspect_metadata = {"inspected_files": inspected_files} if inspected_files else None
    assistant_msg = add_message(
        req.conversation_id, "assistant", response_content, metadata=inspect_metadata
    )

    # Memory judgment: route to global or project writeback
    ctx = load_memory(project_id)
    if is_general:
        proposed = judge_global_memory_updates(ctx, req.message, response_content)
        applied = apply_global_memory_updates(proposed)
    else:
        proposed = judge_memory_updates(ctx, req.message, response_content)
        applied = apply_memory_updates(project_id, proposed)

    # Auto-title: if this is the first user message, set conversation title from it
    if len([m for m in messages if m["role"] == "user"]) <= 1:
        title = req.message[:60] + ("..." if len(req.message) > 60 else "")
        update_conversation_title(req.conversation_id, title)

    return ChatResponse(
        role="assistant",
        content=response_content,
        timestamp=assistant_msg["timestamp"],
        memory_updated=len(applied) > 0,
        memory_updates=applied,
        message_id=assistant_msg["id"],
        inspected_files=inspected_files,
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
    metadata = {"pending_execution_id": plan.pending_execution_id}
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


def _require_project(project_id: str) -> Path:
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


@app.get("/api/projects/{project_id}/execution/runs/{run_id}/screenshot")
def api_get_run_screenshot(project_id: str, run_id: str):
    """Serve the browser verification screenshot for a run (Task 06.2B).

    Only ``screenshots/browser.png`` inside the run's artifact dir is
    served; the path is constructed server-side so there's no caller
    input that could escape the artifact directory.
    """
    _require_workspace(project_id)
    run_dir = run_store.get_run_dir(project_id, run_id)
    screenshot = run_dir / "screenshots" / "browser.png"
    if not screenshot.exists() or not screenshot.is_file():
        raise HTTPException(status_code=404, detail="screenshot not found")
    return FileResponse(str(screenshot), media_type="image/png")


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


@app.post("/api/projects/{project_id}/execution/pending/{pending_id}/confirm")
def api_confirm_pending_execution(project_id: str, pending_id: str):
    """Dispatch the stored task card via the same path as `@code`.

    Validates project + workspace, ensures the pending plan exists, is still
    in 'pending' state, and belongs to this project; then submits to the
    background run manager and marks the pending row as dispatched with the
    new run id. A short assistant message is appended to the chat so the
    user sees confirmation inline (matching the `@code` placeholder UX).
    """
    if project_id == GENERAL_PROJECT_ID:
        raise HTTPException(
            status_code=400,
            detail="Pending execution plans are not available in the GENERAL workspace.",
        )
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

    plan = serialize_pending(row)
    spec = TaskSpec(
        title=plan.title,
        task_card=plan.task_card,
        created_by="pending_confirm",
    )
    record = get_default_manager().dispatch(project_id, spec)

    marked = mark_pending_execution_dispatched(pending_id, record.run_id)
    if not marked:
        # Lost a race with a concurrent confirm; the run is already in flight
        # but our pending row didn't update. Surface clearly rather than
        # silently leaving the row in an inconsistent state.
        raise HTTPException(
            status_code=409,
            detail=(
                "Run was dispatched but the pending plan changed state "
                "concurrently. Check the Runs panel."
            ),
        )

    # Drop an inline confirmation message into the chat so the user can see
    # the new run id without scanning the runs panel.
    confirmation_body = (
        "## Coding Agent Run Started\n\n"
        f"**Run ID:** `{record.run_id}`\n"
        f"**Status:** running\n"
        f"**Task:** {record.task_title}\n\n"
        "The Coding Agent is working on this in the background. Open the "
        "**Runs** panel on the right to track progress."
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
