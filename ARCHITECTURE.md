# Agent OS — Architecture

A map of the whole system: what each file does, how the pieces fit, and the
invariants that must not be broken. Read this at the start of a session to get
the full picture cheaply. Pair it with `CLAUDE.md` (rules) and `ROADMAP.md`
(how it evolved + what's next).

---

## 1. What it is

Agent OS is a **local-first project cockpit** — a single web chat surface for
running multiple long-term projects. It combines project-scoped conversations,
structured markdown memory, an orchestration layer, and a sandboxed execution
layer that can hand work to a **Coding Agent** inside a per-project workspace.

Two agents, clean separation:
- **Main agent = brain.** Planner / memory steward / orchestrator. Talks to the
  user, loads memory, decides delegation. **Never edits `repo/` code or runs
  shell.**
- **Coding Agent = hands.** Bounded executor inside one project's
  `execution_workspaces/{id}/repo/`. Edits code via tools. **Never edits memory
  or other projects.**

---

## 2. Core principles

- **Local-first.** Filesystem + SQLite + FastAPI + React. No cloud infra, no
  queues. ThreadPoolExecutor over Celery, SQLite over Postgres, polling over
  SSE — until there's a concrete reason to swap.
- **Project isolation.** Each project has its own conversations, memory files,
  and execution workspace. Crossings are deliberate and bounded.
- **Structured markdown memory.** Durable state lives in readable `.md` files,
  not buried in chat history.
- **One sandbox chokepoint.** Every repo path + shell command routes through
  `ProjectSandbox` → `ToolRuntime`. No raw `os`/`pathlib`/`subprocess` against
  repo paths anywhere else.
- **No auto-injection of repo contents** into the main agent's context — it
  reads specific files on demand, bounded.
- **Explicit execution only.** Inferred coding intent never auto-runs; a human
  click (or `@code`) is always required.

---

## 3. High-level shape

```
┌────────────┐    ┌────────────────┐    ┌────────────────┐
│  Frontend  │ ←→ │  FastAPI       │ ←→ │  Anthropic API │
│  (React)   │ /api│  (main.py)     │    │  (Claude)      │
└────────────┘    └───────┬────────┘    └────────────────┘
                          │
        ┌─────────────────┼──────────────────────────┐
        ▼                 ▼                          ▼
   memory/          projects/{id}/         execution_workspaces/{id}/
   (global .md)     (project .md)          ├─ repo/   ← Coding Agent edits
        │                 │                ├─ runs/   ← per-run artifacts
        └── orchestrator ─┘                ├─ logs/
            (chat brain)                   ├─ AGENT.md / TASK.md
                                           └─ (runner + verification + preview)
```

Frontend dev server (Vite, port 5173) proxies `/api` → backend (port 8000).
Verified preview apps use **5174** to avoid colliding with Agent OS itself.

---

## 4. Directory layout

```
Agent OS/
├─ CLAUDE.md / ROADMAP.md / ARCHITECTURE.md / README.md   ← docs (root only)
├─ memory/                      global memory
│  ├─ SOUL.md                   read-only identity anchor (loaded every turn)
│  ├─ USER.md / WORKSTYLE.md / MEMORY.md
├─ projects/{project_id}/       per-project memory
│  └─ PROJECT.md / STATUS.md / TASK_QUEUE.md / DECISIONS.md / RESEARCH.md
├─ execution_workspaces/{project_id}/
│  ├─ repo/                     working code tree (Coding Agent's sandbox)
│  ├─ runs/{run_id}/            task_card.md, events.jsonl, run.json, plan.json,
│  │                            result.md, screenshots/browser.png
│  ├─ logs/  AGENT.md  TASK.md
│  └─ repo/uploads/             chat files copied in via "add to workspace" (07.0)
├─ chat_uploads/{conversation}/ chat-only attachments (07.0, gitignored)
├─ backend/                     FastAPI + execution layer (Python)
└─ frontend/                    React + TypeScript (Vite)
```

`SOUL.md` is committed; other global/project files are gitignored (only
`*.example.md` explainers are public). `agent_os.db` (SQLite, WAL) holds
conversations, messages, and pending executions.

---

## 5. Backend modules

### Top level (`backend/`)
| File              | Purpose                                                                 |
|-------------------|-------------------------------------------------------------------------|
| `main.py`         | FastAPI app + all HTTP endpoints (projects, conversations, chat, memory, execution, inspection, verification, preview, **run control** — events / cancel / retry). Wires everything together. |
| `orchestrator.py` | The chat brain. Loads SOUL + memory, assembles context, produces the reply, runs the bounded inspection loop, then the memory-writeback judge. |
| `llm.py`          | Thin LLM entry point: `chat(system, messages, model?, provider?) -> str`. Delegates to `providers.py` (07.1); no context assembly — callers own that. |
| `providers.py`    | Task 07.1 — pluggable model providers (Claude / GPT / Gemini / DeepSeek). Key-presence availability, default-provider preference (Claude first), per-provider default model (env-overridable), and a `complete()` dispatcher. Anthropic via SDK; the rest via `urllib` HTTPS (no new deps). |
| `database.py`     | SQLite persistence: conversations, messages, `pending_executions`. |
| `uploads.py`      | Task 07.0 — chat attachment storage: filename sanitization + allow-list, per-dir dedup, chat-only storage under `chat_uploads/{conv}/`, optional workspace copy via `ProjectSandbox`. HTTP-agnostic (takes bytes). |

### Execution layer (`backend/execution/`)
| File                       | Purpose                                                                |
|----------------------------|------------------------------------------------------------------------|
| `manager.py`               | Workspace filesystem layout + idempotent init; `read/update_task_state`. |
| `models.py`                | Pydantic models: `RunRecord`, `RunStatus`, `TaskSpec`, `ResultSummary`, `VerificationResult` (+ `VerificationCommandResult`), `BrowserVerificationResult`; **Phase 5** `ExecutionPlan` / `ExecutionTask` / `TaskStatus`. |
| `templates.py`             | Default `AGENT.md` / `TASK.md` seeds (incl. the verification block docs). |
| `sandbox.py`               | `ProjectSandbox`: path + command validation. **The boundary.**          |
| `tool_models.py`           | `ToolResult` + per-tool request models.                                 |
| `tool_runtime.py`          | `ToolRuntime`: the six sandboxed file/shell tools with output caps.     |
| `prompts.py`               | Coding Agent system prompt + per-step / correction / **repair** prompts; **Phase 5** planning + per-task-unit prompts. |
| `planner.py`               | **Phase 5** — pure planning layer: `looks_complex` heuristic gate, tolerant `parse_plan`, `fallback_plan`, task-graph helpers (`topological_order` cycle-safe, `dependency_failed`, `aggregate_run_status`). |
| `run_store.py`             | Per-run artifact reader/writer; `render_result_md`; `sweep_stuck_runs`; **Phase 5** `write/read_plan_json` + result.md task section; **run control** `read_events` (timeline) + `read_task_card` (retry source). |
| `runner.py`                | `CodingAgentRunner`: **Phase 5 phased run** — plan phase → per-task execution loops → finalize, **verification + repair** orchestration. **Run control:** cooperative `cancel_event` checked at step boundaries → `_finalize_cancelled` (terminal `cancelled`, no verify/reconcile). |
| `background.py`            | `BackgroundRunManager`: thread-pool dispatch; crash → `failed`; **run control** per-run cancel-`Event` registry (`request_cancel`) + `dispatch(..., retry_of=)`. |
| `chat_delegation.py`       | `@code` trigger handling.                                               |
| `delegation_intent.py`     | Heuristic implicit-delegation detector (fallback only).                 |
| `delegation_judge.py`      | LLM semantic delegation judge (the primary classifier).                 |
| `pending_execution.py`     | Confirmable execution plans (store / render / revise).                  |
| `memory_reconciliation.py` | Post-run bounded memory reconciliation judge.                           |
| `inspect.py`               | Main-agent on-demand, read-only file inspection (06.1).                 |
| `verification.py`          | Command verification: parse, **infer** (`plan_verification`), run specs, render. |
| `browser_verification.py`  | Browser verification lifecycle (dev server → readiness → Playwright screenshot → teardown) + UI flow. |
| `preview.py`               | Managed long-lived preview dev servers (one per project).               |

`tests/` mirrors these per feature; all stub `llm.chat` so no API key is needed.

---

## 6. Frontend modules (`frontend/src/`)

| File                         | Purpose                                                              |
|------------------------------|---------------------------------------------------------------------|
| `main.tsx` / `App.tsx`       | Entry + three-column layout and all top-level state (projects, conversations, messages, context, modals, model provider, color theme). Theme (07.2) is applied via `data-theme` on `<html>` and persisted to `localStorage`. |
| `types.ts`                   | Shared TS types mirroring backend models.                           |
| `components/ProjectList.tsx` | Left column: projects + conversations + create/rename/delete.       |
| `components/ChatPanel.tsx`   | Center column: header (provider selector top-left 07.1, theme selector top-right 07.2) + message thread + the **multi-modal composer** (07.0 — auto-growing textarea, `Ctrl/Cmd+Enter` send, `+` file upload with chips + "add to workspace too", Web Speech voice button); renders `RunChatCard` on messages carrying a `run_id` and attachment chips on messages carrying `metadata.attachments`. |
| `components/ContextPanel.tsx`| Right column: project memory files (editable) + `RunsSection`.       |
| `components/RunsSection.tsx` | Runs list (auto-polls while active) + Start/Stop **preview** control. |
| `components/RunChatCard.tsx` | The in-chat run lifecycle: **live phase badge + multi-task checklist** → build progress → verification phases → completion summary → **Run browser verification** → live preview URL + screenshot; **Cancel** (active) / **Retry** (terminal) controls. |
| `components/RunDetailModal.tsx` | Detailed run inspection: per-command verification, browser status, **Plan & Tasks**, **event Timeline** (polls while active), `result.md`; **Cancel / Retry** controls. |
| `components/RunTimeline.tsx` | Read-only presentational timeline: maps a curated subset of `events.jsonl` (plan / task / command / verification / cancel) to labelled rows. |
| `components/EditModal.tsx` / `ConfirmDialog.tsx` / `GlobalMemoryModal.tsx` | Memory editing, confirmations, global-memory viewer. |

---

## 7. Key pipelines

### A. Chat turn (non-`@code`) — `orchestrator.orchestrate()`
1. Load `SOUL.md` + global + project memory; assemble context.
2. **Delegation judge** (`delegation_judge`) classifies the message. On
   `dispatch_suggested` → create a pending plan (no run). On failure → fall back
   to the `delegation_intent` heuristic.
3. **Main response** LLM call. May emit `{"inspect_request": …}` to read a repo
   file via `inspect.py` (max 3/turn), each result fed back before the next.
4. **Memory-writeback judge** LLM call proposes structured updates; the backend
   policy-filters them (SOUL + non-writable files excluded) before disk writes.

→ Up to 3–4 LLM calls per turn.

**Provider routing (07.1).** The chat request carries a `provider` id
(`claude`/`gpt`/`gemini`/`deepseek`); the endpoint validates it (unknown /
unavailable → 400) and the **main response** (step 3) routes to it via
`orchestrate(..., provider=)`. Internal subsystem calls (judge, delegation,
Coding Agent) use the default provider. Availability is key-presence; see
`providers.py`.

### B. Delegation → run
`@code` → `chat_delegation` → `BackgroundRunManager.dispatch()`.
Inferred intent → pending plan → user clicks **OK** →
`/execution/pending/{id}/confirm` → same `dispatch()`. **No other path runs.**

### C. Coding Agent run — `CodingAgentRunner.run_task()` (Phase 5: phased)
1. Init run dir + `run.json` (`running`); load `AGENT.md` / `TASK.md`.
2. **Plan phase** (`planner.py`): a cheap, pure heuristic (`looks_complex`)
   gates planning. Simple cards skip the LLM and get a single-task plan; complex
   cards run a bounded **read-only** inspection loop (`MAX_PLAN_STEPS = 6`,
   list/read/search only, enforced — not prompt-only) ending in a `plan` action →
   an `ExecutionPlan` (goal, analysis, risks, ordered `ExecutionTask` units with
   `depends_on`). Any failure (parse/empty/over-cap/LLM-unavailable) → single-task
   fallback. Persisted to `plan.json` + `run.json`; the record stays `running`.
3. **Execution phase**: a single-task plan runs the original bounded loop
   verbatim (`MAX_STEPS = 24`; `tool_call`/`final`; one JSON-correction retry;
   observed-activity fallback) and passes the agent's `final` straight through
   (incl. `task_md_update`). A multi-task plan runs each task in topological
   order (`MAX_TASK_STEPS = 12` each), skips tasks whose dependency failed
   (→ `skipped`), mutates per-task status/summary/files/commands/blockers in
   place (run.json + plan.json rewritten per task for live polling), then
   aggregates a run status (all completed → `completed`; mixed → `partial`;
   none → `failed`).
4. **Finalize**: set status/summary/lists; update `TASK.md` (snapshot pre-update
   copy first).
5. **Command verification + repair** (see D).
6. **Browser verification** (opt-in `## Browser Verification` block) — automatic
   screenshot if configured.
7. **Memory reconciliation** (06.0) — bounded, best-effort, at most once.
8. Write `run.json` + `plan.json` + `result.md`; return `ResultSummary`.

Steps 5–7 are **best-effort: an exception there never fails finalization.** The
plan phase is read-only and never fails the run — it falls back to a single task.
Events now carry a `phase` tag (`planning`/`execution`/`repair`) plus
`plan_started`/`plan_ready`/`plan_failed`/`task_started`/`task_status`.

### D. Command verification + repair — `verification.py` + `runner._verify_with_repair`
1. `plan_verification()`: manual `## Verification` block wins (multi-line);
   else **infer** from repo — `npm install`(+build), `pytest` *iff importable*
   else `compileall` syntax check; else `skipped`.
2. Write `verification_state = "verifying"`; run specs in order, **stop at first
   failure**.
3. If a `completed` run failed verification → `verification_state =
   "repairing"`, run **one** bounded repair pass (agent re-edits with the
   failing output), then **re-verify**.
4. Pass → stay `completed`; still failing → `partial` + a `verification failed:`
   blocker. Clear `verification_state`.

### E. Browser preview — `browser_verification.py` + `preview.py`
User clicks **Run browser verification** → `POST …/browser-verify`:
`npm install` → dev server (port 5174) → poll URL → Playwright screenshot. On
pass, the still-running server is **handed off to `preview.py`** so the URL
stays live (Start/Stop from the Runs panel; torn down on backend shutdown).

### F. Chat attachment upload (07.0) — `uploads.py`
Composer sends files to `POST /api/chat/upload` (multipart) **before** the
chat send. The project is derived from the conversation; each file is
sanitized + de-duped and written chat-only under `chat_uploads/{conv}/`, and —
when "add to workspace too" is set on a project conversation — also copied into
`repo/uploads/` through `ProjectSandbox.resolve_repo_path`. Returned metadata
is echoed back on the `/api/chat` body and stored on the user message, so chips
re-hydrate on reload. Images re-serve read-only via
`GET /api/conversations/{id}/attachments/{name}`.

### G. Run control — live timeline + cancel + retry
Surfaces the structured run data and adds bounded control over active/terminal
runs. **Read-only timeline:** `GET …/runs/{id}/events` returns the parsed
`events.jsonl`; the run detail modal renders a curated `RunTimeline` and polls
it (with run.json) while the run is active. The chat card derives a live **phase
badge** + **multi-task checklist** straight from `run.json` (no extra fetch).
**Cancel** (`POST …/runs/{id}/cancel`, only when `running`): sets
`cancel_requested`, signals the in-flight runner via a per-run `threading.Event`
in `BackgroundRunManager`; the runner checks it at each step boundary and routes
to `_finalize_cancelled` (terminal `cancelled` + artifacts, no
verify/browser/reconcile). If no worker owns the run (post-restart), the
endpoint finalizes the orphan directly — after re-reading run.json to confirm
it's still `running`, so it never clobbers a run that just settled. Cancellation
is **cooperative**: an in-flight LLM call or shell command finishes first.
**Retry** (`POST …/runs/{id}/retry`, only when terminal): re-reads the original
`task_card.md` and dispatches a fresh linked run (`retry_of` on the new record,
`retried_by` on the original) — explicit user action, no auto-rerun.

---

## 8. Core data model — `RunRecord` (serialized as `run.json`)

`run_id`, `project_id`, `task_title`, `status` (`running`/`completed`/
`partial`/`blocked`/`failed`/**`cancelled`**), `summary`, `files_changed`,
`commands_run`, `blockers`; verification fields (`verification`,
`verification_state`), browser fields (`browser_verification`,
`browser_verification_state`), memory-reconciliation fields, the **Phase 5**
`plan` field (an `ExecutionPlan` of `ExecutionTask` units, also written
standalone as `plan.json`), and **run-control** fields `cancel_requested`
(transient — set while a cancel is pending, status still `running`) +
`retry_of` / `retried_by` (links a retry to its origin). `cancelled` is a
terminal status the runner sets directly on user cancel — never agent-settable,
and excluded from memory reconciliation. `VerificationResult` carries `mode`
(`manual`/`inferred`/`skipped`), a `commands[]` breakdown, and `repair_attempts`;
its legacy top-level `command`/`exit_code`/`output_preview` mirror the aggregate.

Status semantics: **`completed` only after verification passes (or safe skip);**
files-written-but-verification-failed is `partial`; `skipped` is acceptable only
when nothing safe can run.

---

## 9. Invariants (do not break)

- **Sandbox boundary.** All repo paths + shell commands go through
  `ProjectSandbox` / `ToolRuntime`. No raw `os`/`pathlib`/`subprocess` on repo
  paths elsewhere. Bounded output previews everywhere.
- **Agent roles.** Main agent never edits `repo/` or runs shell. Coding Agent
  never edits memory (`projects/{id}/*.md`, `memory/*.md`) or other workspaces.
- **`SOUL.md`** is read-only + hidden — never shown, never auto-written, never
  in any write path.
- **Explicit dispatch only.** No inferred-intent auto-run, ever. **Retry** is
  an explicit user click that creates a new linked run — never an auto-rerun.
- **Best-effort post-run steps.** Verification / browser / reconciliation never
  crash finalization and never get a run stuck in `running`.
- **No auto-injection of repo contents** into the main agent's context.

---

## 10. Lessons worth keeping (mostly Windows + subprocess)

- **Unread PIPEs deadlock dev servers.** A child with `stdout/stderr=PIPE` that
  nobody drains fills the OS buffer (~4–8 KB on Windows) and blocks before it
  `listen()`s. Always drain with bounded background threads (`_StreamDrainer`).
- **Playwright sync API on a worker thread fails on Windows.** It calls
  `asyncio.create_subprocess_exec`, which `SelectorEventLoop` doesn't support
  off the main thread → a *messageless* `NotImplementedError`. Run Playwright in
  a fresh Python subprocess; use `repr(exc)` fallback for empty messages.
- **`python` on PATH ≠ a venv with your deps.** Inferring `pytest` blindly fails
  with "No module named pytest". **Probe first** (`python -c "import pytest"`
  through the same shell), then fall back to a syntax check.
- **`compileall .` walks `node_modules`.** Exclude it (`-x node_modules`) so
  vendored / py2 files can't derail a full-stack repo's check.
- **Server restarts orphan `running` runs.** The in-process crash handler dies
  with the process; `sweep_stuck_runs()` at startup rescues them to `failed`.
- **Snapshot `TASK.md` before applying the agent's update** so a `task_md_update`
  can't clobber the verification config.

---

## 11. Starting a future session

1. Read `CLAUDE.md`, this file, then the latest Phase 5 entries in `ROADMAP.md`.
2. Match the existing per-feature module + per-feature test-file convention.
3. Keep changes small and bounded to the files the task names; propose refactors
   separately.
4. When done: run the relevant `backend/tests/<file>.py`, note what you ran (and
   didn't), and update the right doc(s) per the policy table in `ROADMAP.md`.
