# Agent OS MVP

A lightweight local-first Agent Operating System for managing multiple projects through a dedicated web interface with project-scoped conversations and structured markdown memory.

## Quick Start

### Prerequisites
- Python 3.10+
- Node.js 18+

### Backend

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
uvicorn main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 in your browser.

## Structure

```
agent-os/
├── frontend/              # React + Vite + TypeScript
│   └── src/
│       ├── App.tsx
│       └── components/
│           ├── ProjectList.tsx    # Sidebar with project/conversation tree
│           ├── ChatPanel.tsx      # Chat interface
│           ├── ContextPanel.tsx   # Memory file viewer
│           ├── EditModal.tsx      # Full-screen memory file editor
│           ├── GlobalMemoryModal.tsx  # Global memory viewer/editor modal
│           ├── ConfirmDialog.tsx  # Deletion confirmation
│           ├── RunsSection.tsx    # Coding Agent runs list (right column)
│           └── RunDetailModal.tsx # Run detail + result.md viewer
├── backend/               # Python + FastAPI
│   ├── main.py            # API endpoints
│   ├── orchestrator.py    # LLM-based orchestration layer
│   ├── llm.py             # Anthropic API wrapper
│   ├── database.py        # SQLite persistence for conversations
│   ├── execution/         # Phase 3 execution-layer foundation
│   │   ├── manager.py     # Workspace filesystem operations
│   │   ├── models.py      # ExecutionWorkspace, TaskSpec, RunRecord, etc.
│   │   ├── templates.py   # AGENT.md and TASK.md defaults
│   │   ├── sandbox.py     # ProjectSandbox: path + command validation
│   │   ├── tool_models.py # ToolResult + tool request models
│   │   ├── tool_runtime.py # ToolRuntime: list/read/write/append/search/shell
│   │   ├── prompts.py     # Coding Agent system prompt + per-step prompts
│   │   ├── run_store.py   # Per-run artifact reader/writer (runs/{run_id}/)
│   │   ├── runner.py      # CodingAgentRunner: bounded LLM tool loop
│   │   └── chat_delegation.py # `@code` chat trigger -> runner bridge
│   ├── .env               # API key (not committed)
│   └── .env.example       # Template for .env
├── memory/                # Global markdown memory files
├── projects/              # Per-project markdown files
├── execution_workspaces/  # Per-project Coding Agent workspaces (Phase 3)
│   └── {project_id}/
│       ├── repo/          # Working code tree
│       ├── runs/          # Per-run artifacts
│       ├── logs/          # Per-run logs
│       ├── AGENT.md       # Coding Agent identity & rules
│       └── TASK.md        # Current objective & progress
└── CLAUDE.md              # Project instructions
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/projects` | List all project IDs |
| POST | `/api/projects` | Create a new project |
| PATCH | `/api/projects/{id}` | Rename a project |
| DELETE | `/api/projects/{id}` | Delete a project and all its conversations |
| GET | `/api/projects/{id}/context` | Get markdown files for a project |
| POST | `/api/projects/{id}/update-file` | Update a project memory file |
| GET | `/api/projects/{id}/conversations` | List conversations for a project |
| POST | `/api/projects/{id}/conversations` | Create a new conversation |
| GET | `/api/conversations/{id}/messages` | Get messages for a conversation |
| PATCH | `/api/conversations/{id}` | Update conversation title |
| DELETE | `/api/conversations/{id}` | Delete a conversation |
| POST | `/api/chat` | Send a message in a conversation |
| POST | `/api/projects/{id}/memory-update` | Apply a structured memory update |
| POST | `/api/conversations/{id}/extract-updates` | Extract memory updates from conversation |
| GET | `/api/general/conversations` | List GENERAL workspace conversations |
| POST | `/api/general/conversations` | Create a GENERAL workspace conversation |
| GET | `/api/global-memory` | Get writable global memory files (USER.md, WORKSTYLE.md, MEMORY.md) |
| POST | `/api/global-memory/update-file` | Manually update a global memory file |
| POST | `/api/projects/{id}/execution/init` | Initialize the project's execution workspace (idempotent) |
| GET | `/api/projects/{id}/execution/workspace` | Get execution workspace metadata |
| GET | `/api/projects/{id}/execution/task-state` | Read TASK.md for the project |
| POST | `/api/projects/{id}/execution/task-state` | Overwrite TASK.md for the project |
| POST | `/api/projects/{id}/execution/tools/list-files` | List files under a repo-relative path |
| POST | `/api/projects/{id}/execution/tools/read-file` | Read a UTF-8 text file from the repo |
| POST | `/api/projects/{id}/execution/tools/write-file` | Write a UTF-8 text file (creates parent dirs) |
| POST | `/api/projects/{id}/execution/tools/append-file` | Append UTF-8 text to a repo file |
| POST | `/api/projects/{id}/execution/tools/search-files` | Recursive substring search under a repo path |
| POST | `/api/projects/{id}/execution/tools/run-shell` | Run a shell command with cwd=repo, timeout, sandbox policy |
| POST | `/api/projects/{id}/execution/runs` | Create + run a Coding Agent task synchronously, returns ResultSummary |
| GET | `/api/projects/{id}/execution/runs` | List run records (newest first) |
| GET | `/api/projects/{id}/execution/runs/{run_id}` | Get a single run's run.json |
| GET | `/api/projects/{id}/execution/runs/{run_id}/result` | Get the rendered result.md |

## Features

### Task 01 — MVP Skeleton
- 3-column layout: project list, chat panel, context panel
- Project-scoped chat with markdown memory files

### Task 02 — Memory-Aware Orchestration
- Orchestration layer builds project-aware responses from memory files
- Intent detection classifies messages (status, next steps, idea, problem, decision, research)
- Structured responses with current understanding, context, next steps, and risks

### Task 03 — Conversations & Backend Persistence
- Project-scoped conversations with SQLite persistence
- Expandable sidebar with project/conversation hierarchy
- Conversation-aware orchestration using message history
- Memory update mechanism for extracting project knowledge from conversations

### Task 3.5 — Project Management & Memory Editing UX
- **Modal-based memory editing**: Edit memory files in a large centered modal instead of the narrow sidebar
- **Create project**: New Project button in sidebar creates a project with default memory files
- **Rename project**: Rename a project from the sidebar; folder, conversations, and PROJECT.md title are all updated
- **Delete project**: Delete a project and all its conversations with confirmation dialog
- **Delete conversation**: Delete individual conversations with confirmation dialog

### Task 04 — LLM-Based Orchestration Layer
- **Real LLM responses**: `/api/chat` now calls Claude API (via Anthropic SDK) instead of rule-based templates
- **Context assembly**: Each request builds a structured system prompt from SOUL.md + global memory + project memory
- **Conversation continuity**: Full message history is passed to the LLM for multi-turn awareness
- **Memory policy enforcement**:
  - `SOUL.md` is **read-only** — loaded every turn as the system-level identity anchor, never auto-written
  - All other global and project memory files may participate in automatic writeback
- **Thin wrapper**: `llm.py` handles API calls; `orchestrator.py` handles context assembly and policy

### Task 04.5 / 04.6 — Agent-Owned Semantic Memory Writeback
- **LLM-driven memory judgment**: After each chat response, a second LLM call examines the conversation turn and current memory state, then decides whether any files need updating
- **Semantic, not keyword-based**: The agent decides based on meaning — no hardcoded keyword triggers. Natural conversation about project scope, decisions, or progress will update the right files
- **Structured output**: The memory judge returns JSON updates (`{filename, section, content, action}`) that are validated and filtered by the backend before writing to disk
- **Policy-controlled**: Only files in `WRITABLE_PROJECT_FILES` are accepted. SOUL.md is always excluded. The backend — not the LLM — decides what actually gets written
- **Clean markdown**: The memory judge writes structured markdown summaries, not raw conversation dumps
- **No fake actions**: The main agent's system prompt explicitly forbids claiming memory updates or Claude Code delegation. Memory writes happen silently in the backend pipeline
- **Frontend refresh**: Chat response includes `memory_updated: bool`; when true, the context panel auto-refreshes

### Task 05.5 — Frontend Run Surface
- **Read-only Runs panel** in the right column (under the project memory files), visible only when a project is active. The GENERAL workspace continues to show no context panel and therefore no Runs section.
- **Run list**: each row shows task title, status badge (color-coded for `completed` / `partial` / `blocked` / `failed` / `running`), local time (completed_at if present, else created_at), and `files: N  cmds: N` counts. Sorted newest-first.
- **Refresh button** (`↻`) in the section header re-fetches `GET /api/projects/{id}/execution/runs` on demand. After sending a `@code` task in chat, click refresh to surface the new run.
- **Run detail modal**: clicking a run row opens a modal that fetches both the run record (`/runs/{id}`) and the rendered result content (`/runs/{id}/result`) in parallel, then shows: run id, status badge, task title, files changed, commands run, blockers, and the full `result.md` body in a monospace block. Escape or click-outside closes the modal.
- **Empty state**: a project with no runs yet shows `No coding agent runs yet. Use @code in chat to create one.`
- **No new backend code** — purely UI on top of the existing four GET endpoints from Task 05.3.

### Task 05.4 — Main Chat Delegation to Coding Agent
- **`@code` trigger in `/api/chat`**: A project-chat message that begins with `@code ` is routed directly to `CodingAgentRunner` instead of the chat orchestrator. The text after `@code` becomes the task card; the workspace is initialized lazily if missing.
- **Chat reply is a structured run summary**: The assistant response is markdown containing the run id, status, summary, files changed, commands run, blockers, and a reference to `GET /api/projects/{id}/execution/runs/{run_id}/result`. Both the original `@code …` user message and the summary reply are persisted to the conversation.
- **GENERAL workspace rejection**: `@code` in a GENERAL (non-project) conversation returns `Coding delegation is only available inside a project workspace.` No run is created, no workspace is touched.
- **Trigger discipline**: Detection requires `@code` followed by whitespace, end-of-string, or punctuation, so `@coder`, `@codereview`, etc. do NOT trigger delegation. Detection is case-insensitive and ignores leading whitespace.
- **Memory writeback skipped on delegation**: Project- and global-memory judgment are bypassed for `@code` turns — the runner already updates `TASK.md`, and tool-style delegation requests aren't appropriate inputs to the semantic memory judge.
- **Normal chat is unchanged**: Non-`@code` messages flow through the existing orchestrator + memory writeback path exactly as before.
- **Still synchronous**: The chat request blocks until the run finishes. Background queueing and a frontend run panel remain deferred.

### Task 05.3 — LLM-driven Coding Agent Runner
- **Task-card based execution**: `POST /api/projects/{id}/execution/runs` accepts a `{title, task_card}` body and runs the Coding Agent synchronously against the project's execution workspace.
- **Bounded LLM tool loop**: Up to 8 steps per run. Each step asks the LLM for one strict-JSON action (`tool_call` or `final`); the runner dispatches the call, feeds the result back, and continues until `final` or the budget is exhausted. One JSON-correction retry per step.
- **Sandboxed ToolRuntime only**: The agent has access to exactly six tools — `list_files`, `read_file`, `write_file`, `append_file`, `search_files`, `run_shell`. The runner imports neither `os` repo-paths, `pathlib` repo-paths, nor `subprocess`; every operation goes through `ToolRuntime`, which routes through `ProjectSandbox`.
- **Per-run artifacts** under `execution_workspaces/{project_id}/runs/{run_id}/`:
  - `task_card.md` — the original input
  - `events.jsonl` — append-only log of `run_started`, `llm_response`, `tool_call`, `tool_result`, `run_completed`, `run_failed` (payloads previewed/bounded)
  - `run.json` — `RunRecord` (status, timestamps, files_changed, commands_run, blockers)
  - `result.md` — human-readable summary for the main orchestrator
- **TASK.md update**: When the agent's `final` action includes a non-empty `task_md_update`, the runner overwrites `TASK.md` with it. Otherwise it appends a short auto-summary. Project memory and global memory are never touched by the runner.
- **Final status vocabulary**: `completed | partial | blocked | failed`. Sandbox violations surface as `ToolResult.success=false` (not exceptions), so the agent can recover and finalize with `blocked` instead of crashing the run.
- **No main-chat delegation yet**: `/api/chat` is unchanged. Wiring the orchestrator to dispatch task cards into this runner is Task 05.4.
- **No frontend UI yet**: Runs are exercised via the four HTTP endpoints above only.

### Task 05.2 — Sandbox + Tool Runtime
- **Project-scoped repo root**: Every tool operation is anchored at `execution_workspaces/{project_id}/repo/`. No tool can read or write outside that subtree.
- **Safe file tools**: `list_files`, `read_file`, `write_file`, `append_file`, `search_files` all go through `ProjectSandbox.resolve_repo_path`, which rejects absolute paths, `..` traversal, and sensitive filenames (`.env`, `.env.local`, `.env.production`, `*.key`, `*.pem`, anything containing `.ssh`, anything containing `.git/config`).
- **Shell execution with command policy**: `run_shell` validates the command via `ProjectSandbox.validate_command` (rejects `rm -rf /`, `rm -rf *`, `del /s`, `format`, `shutdown`, `reboot`, `git push`, `ssh`, `scp`, `iex`, `powershell -enc`, `Invoke-WebRequest`, and any fetcher piped into a shell), runs with `cwd` forced to the project's `repo/`, enforces a timeout, and bounds stdout/stderr at 20 000 chars. Exit code is returned in `metadata`.
- **Bounded outputs**: file reads cap at 20 000 chars; directory listings cap at 500 entries; search caps at 200 hits; shell stdout/stderr cap at 20 000 chars each. Truncation flags are returned in `metadata`.
- **Search hygiene**: search prunes hidden directories and `.git`, `node_modules`, `.venv`, `__pycache__`; binary files are skipped via UTF-8 decode failure.
- **Structured results**: every tool returns a `ToolResult` (`success`, `tool_name`, `output`, `error`, `metadata`).
- **No LLM runner yet**: this task only ships the safe tools. Wiring an LLM-driven Coding Agent into these tools is Task 05.3.

### Phase 3 — Execution Layer foundation (Task 05.1)
- **Per-project execution workspaces**: Each project can initialize its own execution workspace at `execution_workspaces/{project_id}/` containing `repo/`, `runs/`, `logs/`, `AGENT.md`, and `TASK.md`
- **Idempotent init**: `POST /api/projects/{id}/execution/init` creates missing folders and seeds `AGENT.md` / `TASK.md` from templates. Existing AGENT.md and TASK.md are never overwritten — accumulated agent state persists across re-inits
- **AGENT.md template**: Anchors the project's Coding Agent with project identity, architecture notes, coding principles, safe operating rules, and a reporting protocol
- **TASK.md template**: Tracks current objective, task queue, in-progress work, completed items, files changed, commands run, blockers, and a result summary for the main agent
- **Backend package**: `backend/execution/` with `models.py` (ExecutionWorkspace, TaskSpec, RunRecord, RunStatus, ResultSummary), `templates.py`, and `manager.py`
- **Read/write task state**: `GET` and `POST /api/projects/{id}/execution/task-state` allow the main agent (and future Coding Agent) to read and update `TASK.md` over HTTP
- **No execution yet**: This task is the data/workspace foundation only. LLM-driven execution, shell tooling, file edits, sandboxing, and frontend UI come in subsequent tasks.

### Task 04.7 — GENERAL Workspace + Global Memory UX + Semantic Global Writeback
- **GENERAL workspace**: A new section above PROJECTS in the sidebar for non-project / cross-project / user-level conversations
- **GENERAL conversations**: Full conversation lifecycle (create, rename, delete) with SQLite persistence, visually distinct from project conversations
- **Global memory loading**: GENERAL chats load all four global memory files (USER.md, WORKSTYLE.md, SOUL.md, MEMORY.md) before every response — no project memory is loaded
- **Global semantic writeback**: GENERAL chats use the same two-step LLM flow as project chats, but the memory judge targets global files instead of project files:
  - USER.md — durable user profile, identity, role, long-term goals
  - WORKSTYLE.md — collaboration preferences, response style, working habits
  - MEMORY.md — cross-project notes, recurring lessons, meta-level context
- **SOUL.md remains hidden and read-only**: Always loaded as the identity anchor, never shown in any UI, never included in any write path
- **Global memory modal**: A "View Global Memories" button at the top of the sidebar opens a centered modal showing the 3 writable global files with inline edit/save/cancel
- **Backend policy**: Global memory writes are policy-filtered just like project writes — only `WRITABLE_GLOBAL_FILES` are accepted, SOUL.md is always rejected

## How It Works

### Project Management
- **Create**: Click "+" in the sidebar header, enter a name. A project folder is created with starter content in all 5 memory files (PROJECT.md, STATUS.md, TASK_QUEUE.md, DECISIONS.md, RESEARCH.md).
- **Rename**: Click the pencil icon on the active project. The folder is renamed, all conversation references are updated, and the PROJECT.md title line is updated.
- **Delete**: Click the "x" icon on the active project. A confirmation dialog appears. On confirm, the project folder and all its conversations are permanently deleted.
- Project IDs are derived from the name (lowercased, spaces to hyphens). Names must be alphanumeric with spaces, hyphens, or underscores, up to 60 characters.

### Memory File Editing
- Click "Edit" on any memory file in the right context panel
- A centered modal opens with a full-size textarea for comfortable editing
- Save persists to disk and refreshes the panel; Cancel discards changes
- Escape key also closes the modal

### Conversation Management
- Each project supports multiple conversations
- New conversations auto-title from the first user message
- Delete a conversation by hovering and clicking the "x" button (with confirmation)
- All messages are persisted in SQLite

### Orchestration (Two-Step LLM Flow)
Every `/api/chat` request follows this flow:

1. **Load memory** — read all global files (USER.md, WORKSTYLE.md, SOUL.md, MEMORY.md) and, for project chats, project files (PROJECT.md, STATUS.md, TASK_QUEUE.md, DECISIONS.md, RESEARCH.md). GENERAL chats skip project files.
2. **Assemble system prompt** — SOUL.md comes first as the identity anchor, followed by global context, then project context (if applicable), then behavioral rules
3. **Build messages** — conversation history + current user message, cleaned for API compatibility (alternating roles, starts with user)
4. **LLM call 1: Chat response** — send system prompt + messages to Claude API, get conversational reply
5. **Persist response** — save assistant reply to SQLite
6. **LLM call 2: Memory judgment** — a second LLM call examines the latest turn alongside current memory files. For project chats, the judge targets project files. For GENERAL chats, it targets global files (USER.md, WORKSTYLE.md, MEMORY.md).
7. **Policy filter + apply** — backend validates each proposed update against the appropriate writable file set and writes to disk
8. **Return response** — include `memory_updated: bool` and `memory_updates: list` so the frontend knows to refresh

### Memory Writeback Policy

**Global memory** (written by GENERAL chat writeback):

| File | Auto-writable | Notes |
|------|---------------|-------|
| SOUL.md | **No** (read-only) | Core identity, never modified, never shown in UI |
| USER.md | Yes | User profile, identity, role, long-term goals |
| WORKSTYLE.md | Yes | Collaboration preferences, response style |
| MEMORY.md | Yes | Cross-project notes, recurring lessons |

**Project memory** (written by project chat writeback):

| File | Auto-writable | Notes |
|------|---------------|-------|
| PROJECT.md | Yes | Project definition, vision, scope, tech stack |
| STATUS.md | Yes | Phase, milestone, what works, what's next |
| TASK_QUEUE.md | Yes | Task tracking (In Progress / Up Next / Done) |
| DECISIONS.md | Yes | Decisions with rationale |
| RESEARCH.md | Yes | Findings, references, technical notes |

The memory judge writes clean structured markdown — not raw conversation text. Updates are proposed as JSON (`{filename, section, content, action}`) and filtered by the backend policy layer before disk writes. The manual update API (`/api/projects/{id}/memory-update`) remains available for project files, and `/api/global-memory/update-file` for global files.

## Constraints & Edge Cases

- **Rename collisions**: Renaming to an existing project name is rejected
- **Delete is permanent**: No undo for project or conversation deletion
- **Project ID format**: Names are normalized to lowercase-hyphenated IDs
- **API key required**: Set `ANTHROPIC_API_KEY` in `backend/.env`
- **No streaming**: LLM responses are returned in full (no SSE/streaming yet)
- **Two LLM calls per turn**: Chat response + memory judgment — adds latency but ensures semantic accuracy
- **No delegation**: Cannot hand off work to coding agents yet
- **No verification**: No browser automation or output inspection
- **No token budgeting**: Large memory files + long history may hit context limits

## Recommended Next Steps

1. **Task 05.6 — Background runs + status polling**: Move `CodingAgentRunner.run_task` off the request thread so a long `@code` turn doesn't block the chat connection. Return a placeholder reply immediately with the run id; let the frontend hydrate the summary when the run finishes (poll `GET /runs/{id}` or SSE). Pairs with auto-refresh of the runs panel after a `@code` send.
2. **Task 05.7 — Run-aware chat bubbles**: Parse the run id out of `@code` assistant replies and turn it into a clickable affordance that opens the same run-detail modal — closing the loop between chat and runs panel.
3. **Task 05.8 — Implicit delegation intent**: A lightweight LLM judge that decides when a non-`@code` turn should still hand off to the runner (e.g., "please implement X" inside a project chat), keeping `@code` as the explicit override.
4. **Task 06 — Verification loop**: Browser-based or tool-based output inspection.
5. **Streaming responses**: SSE streaming on `/api/chat` for better UX on longer responses.
