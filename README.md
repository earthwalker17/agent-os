# Agent OS

A lightweight **local-first project cockpit** — manage multiple long-term
projects through a single web chat surface with structured memory and a
bounded execution layer that can delegate code work to a sandboxed Coding
Agent.

## What is Agent OS?

Agent OS is a small **Agent Operating System** built for one builder running
multiple projects. It combines:

- **Project-scoped conversations** — each project has its own chat history,
  isolated from other projects.
- **Structured markdown memory** — durable project state lives in readable,
  editable `.md` files, not buried in chat scrollback.
- **A thin orchestration layer** — the main agent assembles context from
  memory, produces planning / explanation replies, and decides via a
  separate semantic-judge call whether memory files should be updated.
- **A bounded execution layer** — a sandboxed Coding Agent runs inside a
  per-project workspace under `execution_workspaces/{project_id}/repo/`,
  dispatched explicitly by the user via `@code …`.
- **A verification surface** — a Runs panel + per-run detail modal show
  status, files changed, commands run, blockers, and `result.md`.

## Why this exists

Existing tools each solve a piece of the workflow but not the whole loop:

- **ChatGPT** is great for discussion and planning but can't operate local
  code or persist project-shaped memory.
- **Claude Code** is great at executing inside a repo but isn't ideal as
  the long-term *conversation surface* for a project.
- **General agent platforms** are too broad and not optimized for a
  multi-project workflow.

Agent OS combines the parts that matter for project work:
ChatGPT-like conversation per project + Claude Code-like execution power on
demand + readable local memory files. It stays lightweight on purpose.

## Current MVP Status

**Phase 1–2 complete:** workspace, project/conversation management, global
and project memory, LLM-based orchestration, two-step semantic memory
writeback.

**Phase 3 complete through Task 05.8:**

- **05.1** Execution workspace foundation (`repo/`, `runs/`, `logs/`,
  `AGENT.md`, `TASK.md`, idempotent init).
- **05.2** `ProjectSandbox` + `ToolRuntime` with bounded `list_files`,
  `read_file`, `write_file`, `append_file`, `search_files`, `run_shell`.
- **05.3** LLM-driven `CodingAgentRunner` with a bounded JSON tool loop and
  per-run artifacts.
- **05.4** `@code` chat trigger dispatches the runner; GENERAL workspace
  rejects.
- **05.5** Read-only Runs panel in the right column + run-detail modal.
- **05.6A** `BackgroundRunManager` (ThreadPoolExecutor) — `@code` and
  `POST /execution/runs` return immediately; runner finalizes off-thread;
  background crashes promote to `failed`.
- **05.6B** Runs panel polls every 2s while runs are `running`, stops when
  idle, shows a pulsing "N running" indicator. After `@code` send, the
  panel reloads immediately.
- **05.7** Chat bubbles containing a run id render a "View Run" button
  that opens the same `RunDetailModal` as the Runs panel.
- **05.8** Conservative rule-based implicit-delegation **suggestion**
  layer (MVP fallback only — `@code` remains the only execution trigger).

## Architecture Overview

```
┌────────────┐    ┌────────────────┐    ┌────────────────┐
│  Frontend  │ ←→ │  FastAPI       │ ←→ │  Anthropic API │
│  (React)   │    │  /api/*        │    │  (Claude)      │
└────────────┘    └────────────────┘    └────────────────┘
                         │
        ┌────────────────┼────────────────────────┐
        │                │                        │
        ▼                ▼                        ▼
   memory/         projects/{id}/        execution_workspaces/{id}/
   (global .md)    (project .md)         ├─ repo/    ← Coding Agent
                                         ├─ runs/    ← per-run artifacts
                                         ├─ logs/
                                         ├─ AGENT.md
                                         └─ TASK.md
```

- **Frontend** — React + Vite + TypeScript. Three-column layout
  (project list / chat / context + runs).
- **Backend orchestrator** — Python + FastAPI. `orchestrator.py` assembles
  context from memory; `llm.py` wraps the Anthropic SDK; memory writeback
  is a second LLM call gated by a policy filter.
- **Memory layer** — pure markdown files on disk. Global memory in
  `memory/`; project memory in `projects/{id}/`.
- **Execution layer** — `backend/execution/` contains the sandbox, tool
  runtime, runner, background dispatch manager, run store, and chat
  delegation bridge.
- **Run artifacts** — `execution_workspaces/{id}/runs/{run_id}/` holds
  `task_card.md`, `events.jsonl`, `run.json`, `result.md` for each run.

## Key Features

- **Project / conversation management** — create, rename, delete projects;
  multiple conversations per project; auto-titling from first message.
- **Global / project memory** — `SOUL.md` (read-only identity anchor),
  `USER.md`, `WORKSTYLE.md`, `MEMORY.md` for global state; `PROJECT.md`,
  `STATUS.md`, `TASK_QUEUE.md`, `DECISIONS.md`, `RESEARCH.md` per project.
  Editable from the UI; never auto-overwritten without policy filtering.
- **LLM-based orchestration** — full conversation history + memory context
  per turn. Anthropic Claude via the official SDK.
- **Semantic memory writeback** — agent-judged, structured-JSON updates,
  policy-filtered before disk writes. SOUL.md is always excluded.
- **Coding Agent execution** — bounded tool loop with sandbox-validated
  file and shell operations, running in a background thread.
- **Runs panel + run-detail modal** — auto-refreshing while runs are in
  flight, openable from the panel or from a chat bubble's "View Run"
  button.

## How Execution Works

1. **User types `@code <task>`** in a project chat. (`@code` is currently
   the **only** trigger that actually dispatches a run.)
2. **`BackgroundRunManager.dispatch()`** allocates a run id, creates the
   run directory, writes the initial `run.json` (status=`running`) and
   `task_card.md`, and submits `CodingAgentRunner.run_task(...)` to a
   thread pool. The HTTP request returns the placeholder record immediately.
3. **`CodingAgentRunner`** loads `AGENT.md` + `TASK.md`, builds the system
   prompt, and runs a bounded JSON tool loop (max 8 steps). Each step is
   a `tool_call` or a `final` action; `tool_call` routes through
   `ToolRuntime` which validates every path and shell command via
   `ProjectSandbox`.
4. **Run finalizes** — the runner writes a final `run.json`, updates the
   project's `TASK.md` (overwrite if the model provided one, else append a
   short auto-summary), and writes `result.md`.
5. **Chat bubble & Runs panel** — the assistant's `@code` reply contains
   the run id; a "View Run" button opens `RunDetailModal`. Meanwhile the
   Runs panel polls every 2s while anything is `running` and stops when
   idle.

If a background thread crashes before the runner finalizes, the manager
flips status to `failed`, appends the error as a blocker, and writes an
emergency `result.md` so the run never gets stuck in `running`.

## Current Constraints

- **No streaming** on `/api/chat` — full response is returned in one shot.
- **No browser-based verification loop** — the Coding Agent runs shell
  commands but does not yet open the rendered UI for visual checks.
- **Implicit delegation is a rule-based MVP fallback**, not a final
  semantic judge. It can flag obvious coding requests; it cannot reason
  about conversation context or non-English imperatives.
- **No run-result memory reconciliation yet** — when a run finalizes, the
  runner updates `TASK.md` and `result.md`, but `STATUS.md` /
  `TASK_QUEUE.md` / `DECISIONS.md` / `RESEARCH.md` are not automatically
  updated from the run summary.
- **Main agent does not auto-read repo contents** — by design. The main
  agent works from compact run metadata + `result.md`. Specific changed
  files are read on demand only.
- **Two LLM calls per chat turn** — chat response + memory judge. Trades
  latency for semantic correctness.
- **Single-user, single-process.** No multi-user auth, no shared deploy
  story.

## Intended Next Architecture / Roadmap

These three pillars are **not** solved by the current 05.8 heuristic and
form the next stretch of work:

1. **Task 05.9 — LLM-based semantic delegation judge.** Replace the
   keyword/regex detector with a model-judged call that sees recent
   conversation, project memory, and the user message. Returns a
   structured decision (`dispatch_suggested` / `discussion` / `memory_only`)
   plus an optional task card. `@code` stays the only execution trigger.
2. **Task 06.0 — Run-result memory reconciliation.** When a run reaches a
   terminal state, a model-judged reconciliation call examines the
   `ResultSummary` + `result.md` and proposes structured updates to
   `STATUS.md` / `TASK_QUEUE.md` / `DECISIONS.md` / `RESEARCH.md`. Reuses
   the existing policy-filtered writeback pipeline. Clean structured
   markdown only, never raw log dumps.
3. **Task 06.1 — On-demand file inspection.** Give the main agent a
   bounded path to pull specific files from `repo/` into a chat turn when
   the user asks for review/debug — through `ToolRuntime`, only with an
   explicit reason, never auto-injected.

Subsequent items:

- **Task 06.2 — Verification loop.** Browser-based or tool-based output
  inspection for the runner itself.
- **Streaming responses.** SSE on `/api/chat` for longer replies.
- **Run event stream.** Replace 2s polling with a per-project SSE stream
  of status transitions and event-log appends.

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 18+
- An Anthropic API key

### Backend

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add ANTHROPIC_API_KEY
uvicorn main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>.

## Repository Structure

```
agent-os/
├── frontend/                       # React + Vite + TypeScript
│   └── src/
│       ├── App.tsx
│       └── components/
│           ├── ProjectList.tsx     # sidebar
│           ├── ChatPanel.tsx       # chat + "View Run" affordance
│           ├── ContextPanel.tsx    # memory files + Runs section
│           ├── EditModal.tsx
│           ├── GlobalMemoryModal.tsx
│           ├── ConfirmDialog.tsx
│           ├── RunsSection.tsx     # auto-refreshing runs list
│           └── RunDetailModal.tsx  # run.json + result.md viewer
├── backend/                        # Python + FastAPI
│   ├── main.py                     # API endpoints
│   ├── orchestrator.py             # context assembly + memory judge
│   ├── llm.py                      # Anthropic SDK wrapper
│   ├── database.py                 # SQLite (conversations + messages)
│   └── execution/
│       ├── manager.py              # workspace filesystem
│       ├── models.py               # ExecutionWorkspace / RunRecord / etc.
│       ├── templates.py            # AGENT.md / TASK.md defaults
│       ├── sandbox.py              # ProjectSandbox: path + command checks
│       ├── tool_models.py          # ToolResult + tool request models
│       ├── tool_runtime.py         # ToolRuntime: file + shell tools
│       ├── prompts.py              # Coding Agent prompts
│       ├── run_store.py            # per-run artifact reader/writer
│       ├── runner.py               # CodingAgentRunner
│       ├── background.py           # BackgroundRunManager
│       ├── chat_delegation.py      # @code chat trigger
│       └── delegation_intent.py    # implicit-delegation heuristic (MVP)
├── memory/                         # global markdown memory
├── projects/                       # per-project markdown memory
├── execution_workspaces/           # per-project Coding Agent workspaces
├── README.md                       # this file
└── CLAUDE.md                       # operating guide for coding agents
```

## API Endpoints (Summary)

Project + memory:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/projects` | List project ids |
| POST | `/api/projects` | Create project |
| PATCH | `/api/projects/{id}` | Rename project |
| DELETE | `/api/projects/{id}` | Delete project + conversations |
| GET | `/api/projects/{id}/context` | Read project memory files |
| POST | `/api/projects/{id}/update-file` | Update a project memory file |
| GET | `/api/global-memory` | Read writable global memory (no SOUL.md) |
| POST | `/api/global-memory/update-file` | Update a global memory file |

Conversations + chat:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/projects/{id}/conversations` | List conversations |
| POST | `/api/projects/{id}/conversations` | Create conversation |
| GET | `/api/general/conversations` | List GENERAL conversations |
| POST | `/api/general/conversations` | Create GENERAL conversation |
| GET | `/api/conversations/{id}/messages` | List messages |
| PATCH | `/api/conversations/{id}` | Rename conversation |
| DELETE | `/api/conversations/{id}` | Delete conversation |
| POST | `/api/chat` | Send a chat message (runs orchestrator, memory writeback, or @code dispatch) |

Execution:

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/projects/{id}/execution/init` | Initialize execution workspace |
| GET | `/api/projects/{id}/execution/workspace` | Workspace metadata |
| GET / POST | `/api/projects/{id}/execution/task-state` | Read / write `TASK.md` |
| POST | `/api/projects/{id}/execution/tools/*` | Manual tool runtime endpoints (list/read/write/append/search/shell) |
| POST | `/api/projects/{id}/execution/runs` | Dispatch a run (returns placeholder immediately) |
| GET | `/api/projects/{id}/execution/runs` | List runs (newest first) |
| GET | `/api/projects/{id}/execution/runs/{run_id}` | Get a run's `run.json` |
| GET | `/api/projects/{id}/execution/runs/{run_id}/result` | Get a run's `result.md` |

## Design Philosophy

- **Brain vs. hands.** The main agent plans and remembers; the Coding
  Agent executes. They communicate through summaries, not by sharing
  context. This is the single most important architectural rule.
- **Memory as a structured context layer.** Knowledge worth remembering
  belongs in named markdown files with stable sections, not in hidden
  conversation buffers. Memory writes are model-proposed but
  policy-filtered before they touch disk.
- **Local-first trusted execution.** Everything that runs runs on the
  user's machine, in a project-scoped sandbox, surfaced through a UI the
  user can read and refresh. No cloud queues, no opaque background
  workers, no implicit cross-project access.
- **Explicit trust boundary before automation.** `@code` is the only
  execution trigger today. Implicit delegation surfaces a *suggestion*,
  never a run. Auto-dispatch waits until the semantic delegation judge
  (Task 05.9) is built and trusted.
