from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

import shutil
import re

# Load .env before anything that needs ANTHROPIC_API_KEY
load_dotenv(Path(__file__).resolve().parent / ".env")

from database import init_db, create_conversation, list_conversations, get_conversation, list_messages, add_message, update_conversation_title, delete_conversation, delete_conversations_for_project, rename_project_conversations
from orchestrator import (
    orchestrate, load_memory, judge_memory_updates, apply_memory_updates, apply_memory_update,
    judge_global_memory_updates, apply_global_memory_updates, apply_global_memory_update,
    load_global_memory, GENERAL_PROJECT_ID, WRITABLE_GLOBAL_FILES,
)
from execution import (
    init_execution_workspace,
    get_execution_workspace,
    read_task_state,
    update_task_state,
    ToolRuntime,
    get_default_manager,
    shutdown_default_manager,
    is_code_delegation,
    handle_code_delegation,
    GENERAL_REJECTION_MESSAGE,
    looks_like_code_request,
    render_suggestion,
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


@app.delete("/api/projects/{project_id}")
def api_delete_project(project_id: str):
    project_path = PROJECTS_DIR / project_id
    if not project_path.exists() or not project_path.is_dir():
        raise HTTPException(status_code=404, detail="Project not found")

    delete_conversations_for_project(project_id)
    shutil.rmtree(project_path)
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


class ChatResponse(BaseModel):
    role: str
    content: str
    timestamp: str
    memory_updated: bool = False
    memory_updates: list[dict] = []


@app.post("/api/chat", response_model=ChatResponse)
def api_chat(req: ChatRequest):
    conv = get_conversation(req.conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    project_id = conv["project_id"]
    is_general = project_id == GENERAL_PROJECT_ID

    # Persist user message
    add_message(req.conversation_id, "user", req.message)

    # Load conversation history for orchestration context
    messages = list_messages(req.conversation_id)

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
        )

    # --- Implicit delegation suggestion (Task 05.8) ---
    # In project chats only, if the message reads like a coding request, reply
    # with a non-executing suggestion that proposes an `@code` task card.
    # @code remains the only actual execution trigger; the user must confirm.
    # Memory writeback is skipped — the message hasn't produced durable
    # project knowledge yet (the user still needs to dispatch).
    if not is_general and looks_like_code_request(req.message):
        response_content = render_suggestion(req.message)
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
        )

    history = [{"role": m["role"], "content": m["content"]} for m in messages]

    # Generate orchestration response
    response_content = orchestrate(project_id, req.message, history=history)

    # Persist assistant reply
    assistant_msg = add_message(req.conversation_id, "assistant", response_content)

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
