# Agent OS — Roadmap & Implementation Status

> **For future Claude Code / ChatGPT sessions:** read `CLAUDE.md` first
> (stable operating rules), then `ARCHITECTURE.md` (the whole picture —
> files, pipelines, invariants), then this file (how the project got here
> and what's next). `README.md` is the public landing page and is
> deliberately short.

This is the long-form evolution record of Agent OS: what landed, in what
order, and the notes worth keeping. It is a changelog with memory — each
entry is compressed to its essence plus any lesson that still matters.

---

## Documentation Policy

Four docs, each with one job. Never duplicate content across them.

| File              | Audience                         | Job                                                    |
|-------------------|----------------------------------|--------------------------------------------------------|
| `README.md`       | Public visitors on GitHub        | Short pitch + setup. Bump only on user-visible change. |
| `ARCHITECTURE.md` | Any agent picking up the repo     | The whole picture: files, pipelines, invariants.       |
| `ROADMAP.md`      | The builder + future AI sessions  | Evolution log, constraints, next steps.                |
| `CLAUDE.md`       | Any coding agent working here     | Stable operating rules. Phase-independent.             |

When a task lands: README gets a 1–2 line bump (only if user-visible);
ROADMAP gets a compressed entry under the right phase; ARCHITECTURE is
updated if a module/pipeline/invariant changed; CLAUDE.md changes only if a
constitutional rule changed.

---

## Phase 1 & 2 — Foundation (complete, stable)

Landed before the execution layer; unchanged since.

- **Workspace + project/conversation management.** Local filesystem layout
  (`memory/`, `projects/{id}/`), FastAPI backend, React three-column UI
  (projects / chat / context). Project + conversation CRUD, multiple
  conversations per project, auto-titling from the first message.
- **Global + project markdown memory.** Global: `SOUL.md`, `USER.md`,
  `WORKSTYLE.md`, `MEMORY.md`. Per-project: `PROJECT.md`, `STATUS.md`,
  `TASK_QUEUE.md`, `DECISIONS.md`, `RESEARCH.md`. UI-editable; `SOUL.md` is
  read-only + hidden, loaded every turn as the identity anchor.
- **LLM orchestration.** Full conversation history + assembled memory context
  per turn via the Anthropic SDK (`llm.py` + `orchestrator.py`).
- **Two-step semantic memory writeback.** After each chat turn a second LLM
  call proposes structured JSON memory updates; the backend policy-filters
  them before writing. `SOUL.md` always excluded.

---

## Phase 3 — Execution Layer (complete through 06.2E)

The arc: build a sandboxed Coding Agent that the main agent can delegate to,
make delegation safe and explicit, surface runs in the UI, then close the
loop with automatic verification and a live preview.

### Foundation (05.1–05.3): sandboxed execution
- **05.1 — Workspace foundation.** Per-project `execution_workspaces/{id}/`
  (`repo/`, `runs/`, `logs/`, `AGENT.md`, `TASK.md`). Idempotent init —
  existing `AGENT.md` / `TASK.md` are never clobbered.
- **05.2 — ProjectSandbox + ToolRuntime.** The single chokepoint for every
  tool call. Sandbox validates paths (no `..`, no absolute, no
  `.env`/`*.key`/`.ssh`/`.git/config`, no escape from `repo/`) and commands
  (block-list + `fetcher | shell` regex). ToolRuntime exposes
  `list_files` / `read_file` / `write_file` / `append_file` / `search_files` /
  `run_shell` with output caps. **Invariant: nothing touches repo paths or the
  shell except through here.**
- **05.3 — CodingAgentRunner.** LLM-driven bounded JSON tool loop. Per-run
  artifacts: `task_card.md`, `events.jsonl`, `run.json`, `result.md`. One
  JSON-correction retry; protocol errors → `failed`.

### Triggering + surfacing runs (05.4–05.7)
- **05.4 — `@code` trigger.** `@code <task>` in a project chat dispatches a run
  immediately. GENERAL workspace rejects.
- **05.5 — Runs panel + RunDetailModal.** Read-only run list (newest first) +
  per-run detail view (`run.json` + rendered `result.md`).
- **05.6A — BackgroundRunManager.** `ThreadPoolExecutor` dispatcher; POST
  returns a `running` `RunRecord` immediately, runner finalizes off-thread; a
  crashed worker is flipped to `failed` with an emergency `result.md`.
- **05.6B — Runs panel auto-refresh.** Polls every 2s while runs are active,
  stops when idle.
- **05.7 — "View Run" chat affordance.** A run-id in a chat bubble renders a
  button that opens the same modal.

### Safe, explicit delegation (05.8–05.9.5)
- **05.8 — Heuristic delegation detector** (`delegation_intent.py`).
  Conservative rule-based classifier; now only a **fallback**.
- **05.9 — LLM delegation judge** (`delegation_judge.py`). Small Claude call
  classifies each non-`@code` message into
  `dispatch_suggested` / `discussion` / `memory_only`. Robust to non-English
  imperatives and anaphora ("do that"). Falls back to 05.8 on failure.
  **Never dispatches — only proposes.**
- **05.9.5 — Confirmable execution plans** (`pending_execution.py`). A
  `dispatch_suggested` decision stores the full task card as a pending row and
  replies in PM tone with **OK, run this** / **Revise plan** buttons. Only a
  user click dispatches (same path as `@code`). Revise rewrites the plan in
  place via one LLM call; it still requires an explicit OK.

### Closing the loop (06.0–06.2E)
- **06.0 — Run-result memory reconciliation** (`memory_reconciliation.py`).
  When a run reaches a terminal state, a bounded model-judged call may update
  `STATUS.md` / `TASK_QUEUE.md` / `DECISIONS.md` / `RESEARCH.md` from a
  **compact** view (ResultSummary + rendered `result.md`). `PROJECT.md`,
  global memory, `SOUL.md`, and repo files are out of scope. Read-only /
  noisy-failure runs skip the LLM call. At most once per run. **Never fails
  the run.**
- **06.1 — Main-agent file inspection** (`inspect.py`). The main agent can
  read specific `repo/` files on demand through a bounded read-only loop
  (max 3 inspections/turn, tighter caps) driven by `{"inspect_request": …}`
  JSON inside `orchestrate()`. Enabled only for initialized non-GENERAL
  workspaces. **No auto-injection of repo contents — by design.**
- **06.2A — Command verification MVP** (`verification.py`). After a run, an
  optional single `## Verification` command from `TASK.md` runs through the
  sandbox; a failing check downgrades `completed` → `partial`. TASK.md is
  snapshotted pre-update so the agent can't clobber the verify config.
- **06.2B — Browser verification MVP** (`browser_verification.py`). Opt-in
  `## Browser Verification` block (dev-server command + `url:`): spin up the
  server, poll for readiness, drive a headless Playwright Chromium to capture
  one screenshot, tear down. Failing check downgrades `completed` → `partial`.
  Three hard-won follow-ups:
  - **06.2B.1** — bumped step budget; runner now reports *observed* file/command
    activity when the budget exhausts; `sweep_stuck_runs()` at startup rescues
    runs left `running` by a server restart.
  - **06.2B.2** — fixed a Windows pipe-deadlock: an unread `stdout`/`stderr` PIPE
    filled the OS buffer and vite never `listen()`ed. Fix: bounded background
    `_StreamDrainer` threads + fast-fail on early dev-server exit.
  - **06.2B.3** — Playwright now runs in a **fresh Python subprocess**: its sync
    API on a worker thread hit Windows `SelectorEventLoop`'s messageless
    `NotImplementedError`. Structured exit codes → actionable operator messages.
- **06.2C — User-triggered browser verification.** `POST …/browser-verify`
  verifies an existing `completed`/`partial` run with no TASK.md editing:
  `npm install` first, default Vite command on port **5174** (Agent OS uses
  5173), result written back into the run artifacts with retry-aware status
  recompute.
- **06.2D — Chat-first run workflow + persistent preview** (`preview.py`).
  Moved the whole build→verify→preview loop into the chat thread:
  `RunChatCard` owns the in-chat lifecycle (running note → completion summary →
  **Run browser verification** → live preview URL + screenshot). A passing
  user-triggered verification **hands the dev server off** to a process-local
  preview registry (one per project) so the URL stays live; Runs panel gained
  **Start / Stop preview**. Modal demoted to a detailed inspection view.
- **06.2E — Automatic command verification + bounded repair.** Verification is
  now automatic and multi-command. `plan_verification()` uses a manual
  `## Verification` block if present (now multi-line), else **infers** from the
  repo: `npm install` (when `node_modules` absent) + `npm run build`;
  `python -m pytest` **only when pytest is importable** (probed via the same
  shell — else fall back to a `compileall` syntax check that excludes
  `node_modules`). Commands run in order, stop at first failure. A failure on
  an otherwise-`completed` run gets **one bounded repair pass** (agent re-edits
  with the failing output as context, then re-verify); a run is `completed`
  only once verification passes (or is safely skipped). `RunRecord`
  `verification_state` (`verifying`/`repairing`) drives chat + Runs-panel
  phases; the chat **Run browser verification** button appears only after
  command verification is clean; **Start preview** enables as soon as
  `node_modules` exists (`/preview/status.deps_installed`). `MAX_STEPS` 16→24.

### Housekeeping (non-phase, landed alongside 06.2B/C)
- **Delete-path fixes.** Conversation delete clears the `pending_executions` FK
  child first; project delete can remove `execution_workspaces/{id}/` via a
  Windows-safe `rmtree`; non-OK deletes surface in the UI.
- **Opt-in workspace deletion.** Project delete keeps `repo/` unless the user
  ticks "Delete its workspace too"; `GET …/workspace-status` reports presence.
- **Public example templates.** `.gitignore` commits `README.md` +
  `*.example.md` explainers under `memory/`, `projects/`,
  `execution_workspaces/` while ignoring private contents.

---

## Execution-trigger contract (invariant)

- **`@code <task>`** in a project chat starts a run immediately; the task card
  is the literal text after `@code`.
- **Inferred coding intent** (LLM judge) only creates a **confirmable pending
  plan** — the user must click **OK, run this** to dispatch. **Revise plan**
  never dispatches; the next send rewrites the plan in place.
- **Only user confirmation dispatches.** There is no path from
  `dispatch_suggested` to a run that bypasses an explicit user action. The
  judge does not run in GENERAL; the confirm endpoint rejects GENERAL.

---

## Current Constraints

- **No streaming** on `/api/chat` — full response in one shot.
- **Automatic command verification + opt-in browser verification.** After each
  run, command verification runs (manual block or inferred); a failure gets one
  bounded repair pass; `completed` requires a pass (or safe skip). An opt-in
  `## Browser Verification` block additionally captures a headless screenshot.
  The user-facing loop lives in chat (06.2D); a managed preview server
  (one per project, sandbox-validated, torn down on shutdown) keeps the URL
  live. **No AI visual judgment yet** — screenshots are stored, not analyzed.
- **Up to 3–4 LLM calls per non-`@code` chat turn** — delegation judge +
  (optional inspection iterations) + chat response + memory judge. A repair
  pass adds a bounded loop only when verification fails on a `completed` run.
- **Main agent never auto-reads repo contents** — only via the bounded 06.1
  inspection loop (max 3 reads/turn).
- **Single-user, single-process.** No auth, no shared deploy.

---

## Test Coverage

Backend tests live under `backend/tests/` and stub the LLM caller, so no
Anthropic key is needed. Each file is runnable standalone.

| File                              | Tests | Covers                                                       |
|-----------------------------------|------:|--------------------------------------------------------------|
| `test_delegation_judge.py`        |    15 | 05.9 judge: decisions, fallbacks, parsing                    |
| `test_pending_execution.py`       |    17 | 05.9.5 serialization, revision LLM, renderers                |
| `test_pending_execution_db.py`    |     6 | 05.9.5 SQLite lifecycle + delete-path FK cleanup             |
| `test_memory_reconciliation.py`   |    26 | 06.0 parser, skip rules, e2e pipeline                        |
| `test_inspect.py`                 |    29 | 06.1 sandbox, parser, orchestrator loop                      |
| `test_verification.py`            |    21 | 06.2A parser, runner integration, sandbox path               |
| `test_verification_inference.py`  |    23 | 06.2E inference, multi-command, pytest probe/fallback, repair |
| `test_browser_verification.py`    |    26 | 06.2B parser, lifecycle, drainer, Playwright diagnostics     |
| `test_runner_diagnostics.py`      |     9 | 06.2B.1 observed activity, sweep_stuck_runs                  |
| `test_ui_browser_verification.py` |    12 | 06.2C UI flow: default port, install step, status recompute  |
| `test_preview.py`                 |    12 | 06.2D preview registry; 06.2E `deps_installed`               |
| `test_chat_first_endpoints.py`    |     3 | 06.2D HTTP: browser-verify sub-status, preview start/stop    |
| **Total**                         | **199** |                                                            |

Run all (from `backend/`): `python tests/<file>.py` for each row above.

---

## Recommended Next Steps

### Next up: AI-assisted visual review
Browser verification captures a screenshot but doesn't judge it. Add a small
model-judged pass over the screenshot + task card + `result.md` producing a
"looks right / wrong / can't tell" verdict. Open questions: third post-run step
vs. folding into 06.0; cost-gate to only `completed`+passing-browser runs; how
to surface the verdict in the UI.

### After that
- **Streaming responses** (SSE on `/api/chat`) — touches `llm.py`,
  `orchestrator.py`, the chat endpoint, `ChatPanel.tsx`.
- **Run event stream** — replace 2s polling with a per-project SSE stream of
  status/verification transitions. Shares plumbing with streaming.
- **Cost / latency** — the 3–4 LLM calls/turn is the lever: cache the judge on
  idempotent messages, merge the memory judge into the main response via
  structured output, gate the inspection loop behind a heuristic.

### Longer-term, not committed
Run cancellation; run retry ("rerun this task card"); cross-project memory
linking; multi-user / shared deploy (needs auth + per-user workspaces + a
different DB story).

---

## How to use this document

1. Read `CLAUDE.md` — the rules of engagement.
2. Read `ARCHITECTURE.md` — the current shape of the system.
3. Skim the most recent Phase 3 entries here for what just landed.
4. Propose your task; don't re-litigate decisions recorded above without new
   information. When it lands, update the right doc(s) per the policy table.
