# CLAUDE.md

## Project
Agent OS MVP

## Current Phase
**Phase 3 — Execution Layer** (Tasks 05.1 + 05.2 + 05.3 + 05.4 + 05.5 complete).
Phases 1–2 (workspace, memory, orchestration, semantic writeback) are complete.
Phase 3 introduces per-project execution workspaces under `execution_workspaces/{project_id}/` so a Coding Agent can be delegated work against the project's `repo/`.
- Task 05.1 — workspace/data foundation: `repo/`, `runs/`, `logs/`, `AGENT.md`, `TASK.md`, idempotent init.
- Task 05.2 — sandbox + tool runtime: `ProjectSandbox` and `ToolRuntime` provide bounded `list_files`, `read_file`, `write_file`, `append_file`, `search_files`, `run_shell` operations restricted to the project's `repo/` directory.
- Task 05.3 — LLM-driven Coding Agent runner: `CodingAgentRunner` runs a bounded JSON tool loop against a `TaskSpec`, writes `task_card.md` / `events.jsonl` / `run.json` / `result.md` under `runs/{run_id}/`, and updates `TASK.md` at the end. Exposed via `POST /api/projects/{id}/execution/runs` (synchronous).
- Task 05.4 — main-chat delegation: `/api/chat` recognizes `@code …` in project conversations as an explicit delegation trigger and hands the message off to `CodingAgentRunner`, returning a structured run summary as the assistant reply. GENERAL workspace rejects `@code` cleanly. Memory writeback is skipped for delegated turns (the runner owns `TASK.md`).
- Task 05.5 — frontend run surface: a read-only Runs panel in the right column for project workspaces (hidden in GENERAL). Lists runs newest-first with status badges; clicking a row opens a modal with run record + rendered `result.md`. Manual refresh button. No new backend code — purely UI on top of the existing four `GET /execution/runs[…]` endpoints.

**All execution-layer tools must go through `ProjectSandbox` validation.** The Coding Agent runner is never allowed to call `os` / `pathlib` against repo paths, or `subprocess` directly — it must use `ToolRuntime`, which routes paths through `resolve_repo_path()` and shell commands through `validate_command()`. The chat layer must NOT call `ToolRuntime` or filesystem APIs directly either: any code-execution path from chat must go through `CodingAgentRunner` (currently via the `@code` helper in `execution/chat_delegation.py`).

**Still synchronous.** A `@code` turn blocks the chat request until the run finishes. There is no background queue, run-aware chat bubble, or implicit delegation intent yet — the runs panel surfaces results once they exist, but the user has to click refresh after a long `@code` turn.

## Mission
Build a lightweight local-first Agent Operating System for project development.

This system is designed for a single user to build and manage multiple long-term projects through:
- a dedicated web chat interface
- project-scoped conversations
- structured markdown memory
- a central orchestration layer
- delegated execution through coding agents and tools
- browser-based inspection and verification

The goal is not to build a general-purpose assistant, social chatbot, or a full OpenClaw replacement.
The goal is to build a focused Project OS for AI-assisted product development workflow.

---

## Product Positioning
This product sits between ChatGPT, Claude Code, and OpenClaw:

- ChatGPT is strong at discussion, planning, and high-level product thinking, but cannot directly operate local code/runtime.
- Claude Code is strong at execution, but not ideal as the main long-term project conversation surface.
- OpenClaw has tool access and local agency, but is too heavy, too broad, and not optimized for project-separated development workflow.

Agent OS should combine the best parts:
- ChatGPT-like project-separated conversation
- Claude Code-like execution power through delegation
- OpenClaw-inspired local control, memory files, and tool orchestration

But it should remain lighter, clearer, and more workflow-centered than a general-purpose agent platform.

---

## MVP Scope
The MVP should support:

1. Multi-project workspace
   - separate projects
   - isolated chat context
   - clear project switching

2. Explicit memory system
   - global memory files
   - project memory files
   - readable and editable markdown state

3. Orchestration layer
   - understand user intent in project context
   - load memory and current state
   - produce structured guidance and next steps
   - prepare work for execution agents

4. Delegated execution flow
   - hand off coding work to external execution agents or tools
   - receive concise result summaries
   - avoid unnecessary low-level context pollution

5. Verification loop
   - inspect outputs through browser testing, tool feedback, or state checks
   - help determine whether work is actually done

---

## Non-Goals
For the MVP, do not optimize for:
- multi-user collaboration
- cloud deployment
- social/chat platform integrations
- autonomous always-on background behavior
- plugin ecosystems
- heavy visual polish
- large-scale infrastructure

This is a local-first, workflow-first system for one builder.

---

## Core Principles

### 1. Workflow-first
The system exists to move projects forward.

### 2. Project isolation
Each project should have distinct chat, memory, and state.

### 3. Explicit memory
Important project state should live in readable files, not only hidden chat history.

### 4. Thin orchestration
Keep the central agent layer clear and lightweight.
Do not overbuild speculative architecture.

### 5. Delegated execution
The main agent should coordinate execution, not become a monolithic coding worker.

### 6. Verifiable progress
The system should prefer inspected results over assumed success.

### 7. Local control
Prefer simple, debuggable, local-first architecture.

---

## Memory Rules

### Global memory
- `USER.md` → who the user is
- `WORKSTYLE.md` → collaboration preferences
- `SOUL.md` → primary agent identity and operating philosophy
- `MEMORY.md` → cross-project persistent notes

### Project memory
- `PROJECT.md` → project definition
- `STATUS.md` → current state and milestone
- `TASK_QUEUE.md` → next actions
- `DECISIONS.md` → important choices and rationale
- `RESEARCH.md` → external findings and technical notes

Memory should remain readable, editable, and operationally useful.

### Execution workspace (Phase 3)
Per-project Coding Agent workspaces live at `execution_workspaces/{project_id}/`:

- `repo/` — working code tree the Coding Agent edits (no other agent edits here)
- `runs/` — per-run artifacts
- `logs/` — per-run logs
- `AGENT.md` — agent identity, principles, safe-operating rules (project-owned, never auto-overwritten)
- `TASK.md` — current objective, queue, in-progress, completed, files changed, commands run, blockers, result summary

The main orchestrator does NOT edit code under `repo/`. It delegates to the Coding Agent via the workspace.

---

## Recommended Stack
- Frontend: React + Vite + TypeScript
- Backend: Python + FastAPI
- Storage: local filesystem first, SQLite only when needed
- Communication: simple HTTP first

---

## Coding Standards
- keep code clear and modular
- prefer explicitness over abstraction
- avoid premature complexity
- make file and API boundaries easy to understand
- write code that supports future delegation and verification flows

---

## UI Expectations
Main layout:
- left: project list
- center: chat / orchestration surface
- right: project memory / state panel

The UI should feel like a practical internal tool for project operation.

---

## What to Optimize For
When working in this repository, prioritize:
- clarity
- continuity
- controllability
- readable shared state
- low-friction local usage
- smooth path toward delegated execution and verification