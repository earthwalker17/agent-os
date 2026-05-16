# Agent OS — Roadmap & Implementation Status

> **For future Claude Code / ChatGPT sessions:** read `CLAUDE.md` first
> (stable operating rules), then this file (current state and next
> steps). `README.md` is the public-facing landing page — it is
> deliberately concise and does not carry detailed task history.

This document is the long-form record of where Agent OS is, what's
landed, and what's coming next. It can grow over time; the public README
should not.

---

## Documentation Policy

Three layers, each with a distinct job:

| File          | Audience                       | Tone / Scope                                   |
|---------------|--------------------------------|------------------------------------------------|
| `README.md`   | Public visitors on GitHub      | Short, presentable. What it is, why, setup.    |
| `ROADMAP.md`  | The builder + future AI sessions | Detailed status, task log, constraints, plans.  |
| `CLAUDE.md`   | Any coding agent working here  | Stable operating rules. Phase-independent.     |

Never duplicate task history across these three. When a task lands:

- README gets a 1–2 line bump in **Current status** (only if user-visible).
- ROADMAP gets the full entry under **Phase 3 — Execution Layer**.
- CLAUDE.md only changes if the constitutional rules changed.

---

## Phase 1 & 2 — Summary (Complete)

These were landed before the execution layer began. They are stable.

- **Workspace + project/conversation management.** Local filesystem
  layout (`memory/`, `projects/{id}/`), FastAPI backend, React frontend
  with three-column layout (project list / chat / context). Project
  create / rename / delete, multiple conversations per project,
  auto-titling from the first message.
- **Global + project markdown memory.** `SOUL.md`, `USER.md`,
  `WORKSTYLE.md`, `MEMORY.md` for global; `PROJECT.md`, `STATUS.md`,
  `TASK_QUEUE.md`, `DECISIONS.md`, `RESEARCH.md` per project. Editable
  from the UI; `SOUL.md` is read-only.
- **LLM-based orchestration.** Full conversation history + memory
  context per turn via the Anthropic SDK in `llm.py`. Context assembly
  in `orchestrator.py`.
- **Two-step semantic memory writeback.** After each chat turn, a
  second LLM call examines the exchange + current memory and proposes
  structured JSON updates. Updates are policy-filtered before disk
  writes. `SOUL.md` is always excluded.

---

## Phase 3 — Execution Layer (Complete through 06.1)

### 05.1 — Execution workspace foundation
Per-project workspaces under `execution_workspaces/{project_id}/`:
`repo/`, `runs/`, `logs/`, `AGENT.md`, `TASK.md`. Idempotent init —
missing folders are created but existing `AGENT.md` / `TASK.md` are
preserved.

### 05.2 — ProjectSandbox + ToolRuntime
Single chokepoint for every tool call. `ProjectSandbox` validates
paths (no `..`, no absolute paths, no `.env` / `*.key` / `.ssh`, no
escape from the project's `repo/`) and shell commands (block-list +
`fetch | shell` regex). `ToolRuntime` exposes `list_files`,
`read_file`, `write_file`, `append_file`, `search_files`, `run_shell`
with output caps (20 000 chars per read, 500 entries per listing,
200 search hits).

### 05.3 — CodingAgentRunner
LLM-driven bounded JSON tool loop (max 8 steps). Per-run artifacts in
`runs/{run_id}/`: `task_card.md`, `events.jsonl`, `run.json`,
`result.md`. Single correction retry on malformed JSON; protocol
errors promote the run to `failed`.

### 05.4 — `@code` chat trigger
Project chat messages starting with `@code <task>` dispatch the runner
synchronously and return a placeholder reply. GENERAL workspace
rejects.

### 05.5 — Runs panel + RunDetailModal
Read-only right-column panel listing runs (newest first). Clicking a
run opens `RunDetailModal` showing `run.json` + rendered `result.md`.

### 05.6A — BackgroundRunManager
`ThreadPoolExecutor`-based dispatcher. `@code` and
`POST /execution/runs` return immediately with the placeholder
`RunRecord` (status=`running`); the runner finalizes off-thread. If
the background thread crashes, the manager flips status to `failed`,
appends the error as a blocker, and writes an emergency `result.md`
so the run never gets stuck in `running`.

### 05.6B — Runs panel auto-refresh
The panel polls every 2s while runs are `running`, stops when idle,
shows a pulsing "N running" indicator. After `@code` send, the panel
reloads immediately for instant feedback.

### 05.7 — "View Run" chat affordance
Chat bubbles containing a run id render a **View Run** button that
opens the same `RunDetailModal` as the Runs panel. Linked via
message metadata.

### 05.8 — Heuristic delegation suggestion (fallback)
Conservative rule-based detector (`delegation_intent.py`) for
code-shaped messages. Currently only used as a fallback when the
05.9 LLM judge fails; `@code` remains the canonical explicit trigger.

### 05.9 — LLM semantic delegation judge
Each non-`@code` project-chat message goes through `judge_delegation()`
— a small Claude call (`max_tokens=384`) that classifies into
`dispatch_suggested` / `discussion` / `memory_only` using recent
conversation + a compact project-memory snapshot. Robust to
non-English imperatives and anaphoric follow-ups ("do that").
Judge failures fall back safely to the 05.8 heuristic. Never
dispatches — only proposes.

### 05.9.5 — Confirmable execution plans
When the judge returns `dispatch_suggested`, the assistant replies in
project-manager tone with a short execution plan and stores the full
Coding Agent task card internally as a **pending execution**. The
chat bubble exposes two buttons — **OK, run this** (dispatches the
stored task card via the same path as `@code`) and **Revise plan**
(enters a sticky revise mode; the next chat send rewrites the plan
+ task card in place via a single small LLM call). The full task
card is available behind an inspect toggle but is not dumped into
the chat by default. Inferred coding intent never auto-runs.

### 06.0 — Run-result memory reconciliation
When a Coding Agent run reaches a terminal state
(`completed` / `partial` / `blocked` / `failed`), a small
model-judged reconciliation call examines a **compact** view of the
run — the `ResultSummary`, rendered `result.md`, files changed,
commands run, and blockers — together with a snapshot of the four
writable target memory files, and decides whether to update
`STATUS.md`, `TASK_QUEUE.md`, `DECISIONS.md`, or `RESEARCH.md`.
`PROJECT.md`, global memory, `SOUL.md`, and repo source files are
out of scope. Read-only inspection runs (no files changed, no
blockers, no actionable summary) and noisy failures (no files
changed, no informative blocker) are skipped before the LLM call.
Each run is reconciled at most once — the outcome is recorded on
`RunRecord` (`memory_reconciled` / `memory_reconciliation` /
`memory_reconciliation_error`) and as a `memory_reconciled` event
in `events.jsonl`. Reconciliation NEVER prevents a run from
completing.

### 06.1 — On-demand main-agent file inspection
The main agent now has a bounded, sandboxed path to inspect specific
files inside a project's execution workspace `repo/` directory **only
when the user's question requires it**. Implemented as a tiny tool
loop inside `orchestrate()` (max 3 inspections per turn): the LLM
may emit `{"inspect_request": {"tool":
"list_files"|"read_file"|"search_files", ...}}` JSON to request an
inspection. The orchestrator executes it through `execution.inspect`
(which routes through `ProjectSandbox` + `ToolRuntime`), and the
result is fed back as a labeled `INSPECTION RESULT` block before the
next iteration. The loop is enabled **only** for non-GENERAL projects
whose execution workspace has been initialized. Read-only — no
`write_file` / `append_file` / `run_shell` on this surface. Caps:
8000 chars per file, 150 entries per listing, 30 search hits. Path
traversal, absolute paths, sensitive files, and cross-project
access are rejected by the sandbox. `ChatResponse.inspected_files`
surfaces which files were read.

---

## Current Architecture

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

### Module layout (`backend/execution/`)

| File                       | Purpose                                            |
|----------------------------|----------------------------------------------------|
| `manager.py`               | Workspace filesystem                               |
| `models.py`                | `ExecutionWorkspace` / `RunRecord` / `TaskSpec` / `ResultSummary` |
| `templates.py`             | `AGENT.md` / `TASK.md` defaults                    |
| `sandbox.py`               | `ProjectSandbox`: path + command validation        |
| `tool_models.py`           | `ToolResult` + request models                      |
| `tool_runtime.py`          | `ToolRuntime`: file + shell tools                  |
| `prompts.py`               | Coding Agent system + tool-result prompts          |
| `run_store.py`             | Per-run artifact reader/writer                     |
| `runner.py`                | `CodingAgentRunner` JSON tool loop                 |
| `background.py`            | `BackgroundRunManager` (thread pool)               |
| `chat_delegation.py`       | `@code` chat trigger                               |
| `delegation_intent.py`     | Heuristic implicit-delegation (fallback)           |
| `delegation_judge.py`      | LLM semantic delegation judge (05.9)               |
| `pending_execution.py`     | Confirmable execution plans (05.9.5)               |
| `memory_reconciliation.py` | Post-run memory reconciliation (06.0)              |
| `inspect.py`               | Main-agent file inspection (06.1)                  |

### Execution-trigger contract

- **`@code`** in a project chat starts a `CodingAgentRunner` run
  **immediately** — the task card is the user's literal text after
  `@code`.
- **Inferred coding intent** (detected by the LLM delegation judge)
  creates a **confirmable pending execution plan** instead of running
  anything. The assistant replies with the plan; the user must click
  **OK, run this** to dispatch.
- **Revise plan** does not dispatch. The next chat send is treated as
  revision instructions; the pending plan + task card are rewritten
  in place via a small LLM call. The new plan is shown with the same
  two buttons.
- **Only user confirmation dispatches.** No inferred intent ever
  auto-runs. There is no path from `dispatch_suggested` to a Coding
  Agent run that bypasses an explicit user action.
- The delegation judge does not run in the GENERAL workspace; the
  confirm endpoint rejects GENERAL. Pending plans only exist in
  project chats with an execution workspace.

---

## Current Constraints

- **No streaming** on `/api/chat` — full response returned in one shot.
- **No browser-based verification loop** — the Coding Agent runs
  shell commands but does not yet open the rendered UI for visual
  checks. This is the 06.2 target.
- **Up to four LLM calls per non-`@code` project chat turn** —
  delegation judge + (optional inspection loop iterations) + chat
  response + memory judge. The inspection loop is only entered when
  the model emits an `inspect_request`; most chat turns stay at 3
  calls.
- **Main agent does not auto-read repo contents** — by design.
  Specific changed files are read on demand only, through the bounded
  inspection loop in 06.1 (max 3 reads per turn, 8000 chars per
  file).
- **Run-result memory reconciliation is bounded** (06.0). The
  reconciliation judge sees only compact run metadata + rendered
  `result.md` + a compact memory snapshot — never `events.jsonl`,
  full diffs, or full repo contents.
- **Single-user, single-process.** No multi-user auth, no shared
  deploy story.

---

## Test Coverage

The backend tests live under `backend/tests/` and stub the LLM caller
so no Anthropic API key is needed to run them.

| File                                 | Tests | Covers                                          |
|--------------------------------------|------:|-------------------------------------------------|
| `test_delegation_judge.py`           |    15 | 05.9 judge: decisions, fallbacks, parsing       |
| `test_pending_execution.py`          |    17 | 05.9.5 serialization, revision LLM, renderers   |
| `test_pending_execution_db.py`       |     4 | 05.9.5 SQLite lifecycle + metadata roundtrip    |
| `test_memory_reconciliation.py`      |    26 | 06.0 parser, skip rules, e2e pipeline           |
| `test_inspect.py`                    |    29 | 06.1 sandbox, parser, orchestrator loop         |
| **Total**                            |  **91** |                                                 |

Run all:
```bash
cd backend
python tests/test_delegation_judge.py
python tests/test_pending_execution.py
python tests/test_pending_execution_db.py
python tests/test_memory_reconciliation.py
python tests/test_inspect.py
```

---

## Recommended Next Steps

### Next up: Task 06.2 — Verification loop
Give the Coding Agent (or the main agent on demand) a way to verify
that a run produced working software, not just modified files. Two
plausible shapes:

- **Tool-based verification.** The runner's tool budget gets one extra
  step where it executes a project-defined `verify` command (`pytest`,
  `npm test`, `tsc --noEmit`, etc.) and captures the result into
  `run.json` + `result.md`.
- **Browser-based verification.** For UI runs, spawn a headless
  browser (Playwright?) against the dev server, take a screenshot,
  attach it to `result.md`. Only for projects that opt in.

Open questions before starting:
- Where does the per-project verify command live? `AGENT.md`? A new
  `VERIFY.md`? A field in `task_card.md`?
- Should verification failure flip a `completed` run to `partial`?

### After 06.2
- **Streaming responses.** Server-Sent Events on `/api/chat` for
  longer replies. Touches `llm.py`, `orchestrator.py`, the chat
  endpoint, and `ChatPanel.tsx`.
- **Run event stream.** Replace 2s polling on the Runs panel with a
  per-project SSE stream of status transitions and event-log appends.
  Shares plumbing with streaming responses.
- **Improve cost / latency.** The current 3–4 LLM calls per turn is
  the obvious cost lever. Plausible reductions: cache the delegation
  judge on idempotent messages; merge the memory judge into the main
  response with structured output; only run the inspection loop when
  a heuristic gate fires.

### Longer-term, not committed yet
- **Run cancellation** from the Runs panel.
- **Run retry** ("rerun this task card" button).
- **Cross-project memory linking** — explicit links from one
  project's `RESEARCH.md` to another's, surfaced as inspection
  suggestions.
- **Multi-user / shared deploy** would require auth, per-user
  workspaces, and a different DB story. Not on the near-term path.

---

## How to Use This Document

When you (Claude / ChatGPT / a human) sit down to work on Agent OS:

1. Read `CLAUDE.md` — the rules of engagement haven't changed.
2. Read this file's **Current Architecture**, **Current Constraints**,
   and **Recommended Next Steps**.
3. Skim the most recent task entries in **Phase 3** for what just
   landed.
4. Then propose your task. Don't re-litigate decisions already
   recorded above unless you have new information.

When a task lands, update this file (the Phase 3 list, the test
table, and the constraint list) — and only bump the README's
**Current status** line if the change is user-visible on GitHub.
