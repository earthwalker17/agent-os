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

## Phase 4 — Interface & UX

### 07.0 — Multi-modal chat composer
Replaced the single-line chat input with a modern composer (`ChatPanel.tsx`
+ `uploads.py`):
- **Auto-growing multiline textarea.** Grows to a 200px cap then scrolls.
  `Enter` inserts a newline; **`Ctrl`/`Cmd`+`Enter`** sends. Sent user
  messages render with `white-space: pre-wrap`, so line breaks / blank lines /
  indentation survive round-trip.
- **Voice input** (left of Send) via the Web Speech API — feature-detected
  (`SpeechRecognition` / `webkitSpeechRecognition`), pulsing record state,
  transcript **appended live** (never auto-sent), disabled with a tooltip on
  unsupported browsers. Runs **continuous + interim** so text streams in as you
  speak and listening continues through pauses until you click stop (re-click
  ends immediately via an optimistic UI flip + `abort()`); ref-mirrored state
  avoids stale-closure races in the recognition callbacks. Chrome's transient
  `"network"` errors (it streams audio to a remote service) are auto-retried up
  to twice before surfacing a clear message. Minimal ambient types live in
  `frontend/src/speech.d.ts` (the API isn't in this TS release's `lib.dom`).
- **File upload** via a `+` button: native picker, allow-listed types
  (images + `.txt`/`.md`/`.pdf`/`.doc`/`.docx`), removable chips shown before
  send. A message can carry text, files, or both (empty-text-with-files
  synthesizes a short note for the orchestrator/judge/memory calls).
- **"Add to workspace too"** toggle — project conversations only (GENERAL has
  no workspace). Off → chat-only; on → also copied into `repo/uploads/`. Each
  sent attachment shows a **chat only** / **chat + workspace** badge.
- **Backend.** `POST /api/chat/upload` (multipart: `conversation_id`,
  `add_to_workspace`, `files`) returns per-file metadata (original + stored
  name, MIME, size, scope, workspace path). The owning project is derived from
  the conversation, never trusted from the client. Filenames are sanitized
  (basename only, safe charset, allow-listed ext) and de-duplicated per
  directory; the workspace copy routes through `ProjectSandbox.resolve_repo_path`
  so it can't escape `repo/`. Chat-only files live under
  `chat_uploads/{conversation_id}/` (gitignored) and are re-served read-only via
  `GET /api/conversations/{id}/attachments/{name}`. Attachment metadata rides on
  the user message so chips re-hydrate on reload. New dep: `python-multipart`.
  No document parsing / RAG — upload + storage + UX plumbing only.

### 07.1 — Pluggable model-provider selection
Extended the Anthropic-only setup into a small provider layer (`providers.py`
+ `llm.py` delegate):
- **Four providers** — Claude / GPT / Gemini / DeepSeek, in a stable registry
  (label, key env var, default model). Only Anthropic uses an SDK (already a
  dep); GPT, Gemini, and DeepSeek are called over plain HTTPS via `urllib`
  (OpenAI shape for GPT + DeepSeek; `generateContent` for Gemini) so **no new
  Python dependencies**. Calls are lazy — an unused provider never runs.
- **Availability = key presence.** `is_available()` is true iff the provider's
  env var (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` /
  `DEEPSEEK_API_KEY`) is set. `default_provider()` picks the first available in
  order, **Claude preferred** (preserves pre-07.1 behavior). Default models are
  overridable via `AGENT_OS_{CLAUDE,OPENAI,GEMINI,DEEPSEEK}_MODEL`.
- **`GET /api/providers`** returns all four with an `available` flag + the
  resolved default for the UI. **`/api/chat`** gained a `provider` field;
  it's validated up front (unknown / unavailable → clean 400) and the
  **orchestrated main response** routes to it via `orchestrate(..., provider=)`.
  Internal subsystem calls (memory judge, delegation judge, Coding Agent) stay
  on the default provider — minimal scope, no per-call routing.
- **UI.** A dropdown top-left in the chat header lists all four; missing-key
  providers render disabled (`— no key`). Selection lives in `App` state,
  defaults to the backend's preferred-available provider, and ships with every
  chat request. Existing Claude-only setups are unchanged.
- **Out of scope (per task):** per-project model memory, provider fallback,
  cost tracking, advanced settings, streaming, provider-specific UI tuning.

### 07.2 — Light theme + theme switcher
Added a second color theme (frontend-only):
- **Theme tokens.** The dark palette already lived in CSS variables on `:root`;
  added a `:root[data-theme='light']` block (higher specificity than bare
  `:root`, so it wins when the attribute is set; no attribute → dark default).
  Two previously hard-coded values were variabilized so they flip with the
  theme: bold response text (`--text-strong`) and the elevated code surface
  (`--bg-elev`). Everything else already referenced variables, so the whole UI
  (sidebars, chat, composer, modals, runs panel) re-themes with no per-component
  changes.
- **Switcher.** A Dark/Light dropdown sits **top-right** of the chat header
  (mirroring the provider selector top-left). `App` owns the theme state,
  defaults to dark, persists to `localStorage` (`agentos-theme`), and applies it
  by setting `data-theme` on `<html>` so the variables cascade everywhere
  (modals included). Behavior is otherwise identical to dark mode.

---

## Phase 5 — Execution Orchestration (plan → tasks → execute)

Upgraded the Coding Agent from one flat bounded tool loop into a phased,
inspectable orchestration: **plan → execute task-by-task → finalize**. The
run record, role separation, sandbox chokepoint, explicit-dispatch contract,
and the whole verification / browser / preview / reconciliation tail are
unchanged; the new layer is additive.

- **Planning phase** (`planner.py`, new). A cheap, pure heuristic
  (`looks_complex`) gates the cost: simple task cards skip the planner entirely
  (no extra LLM call — legacy behavior + every existing test preserved), while
  complex cards run a **bounded read-only inspection loop** (`MAX_PLAN_STEPS=6`,
  `list_files`/`read_file`/`search_files` only, enforced not prompt-only) that
  ends in a `plan` action. The plan — goal, analysis, risks, and an ordered set
  of `ExecutionTask` units with `depends_on` — is parsed tolerantly and
  **always falls back to a single-task plan** on any failure (parse error,
  zero/over-cap tasks, planner LLM unavailable). Never fails the run.
- **Structured task graph** (`models.py`). New `ExecutionPlan` / `ExecutionTask`
  / `TaskStatus` (`pending`/`running`/`completed`/`failed`/`skipped`). The plan
  rides on `RunRecord.plan` (so run.json carries it) and is also persisted as a
  standalone `plan.json` artifact, rewritten as task statuses settle.
- **Multi-step execution** (`runner.py`). A single-task plan runs the *original*
  loop verbatim (`MAX_STEPS=24`) and passes the agent's `final` (incl.
  `task_md_update`) straight to `_finalize` — byte-identical legacy path. A
  multi-task plan runs each unit in topological order (`MAX_TASK_STEPS=12`,
  cycle-safe), skips tasks whose dependency failed, records per-task
  status/summary/files/commands/blockers, and aggregates a run status
  (all completed → `completed`, so command verification still gates it;
  mixed → `partial`; none → `failed`). Single-threaded, but `depends_on` leaves
  the door open for future parallel/subagent execution. Observed-activity stays
  run-scoped; per-task attribution uses snapshot deltas.
- **Observability**. Richer `events.jsonl`: a `phase` tag
  (`planning`/`execution`/`repair`) on tool/LLM events plus
  `plan_started` / `plan_ready` / `plan_failed` / `task_started` / `task_status`.
  `result.md` gains an **Execution Plan + per-task** section for multi-task runs
  (single-task result.md is unchanged). New read-only endpoint
  `GET …/runs/{run_id}/plan`. Minimal additive UI: TS types + a read-only
  "Plan & Tasks" block in `RunDetailModal` (Runs panel + chat card untouched).
- **Design references** (architecture only, no code copied): `HKUDS/OpenHarness`,
  `Gitlawb/openclaude`, `anomalyco/opencode` — plan/build split, task tools,
  observable execution. Kept strictly local-first (no queues/cloud).
- **Budgets, not bloat.** We added structure (per-task loops + a small planning
  budget), not a bigger flat `MAX_STEPS`.

### Live Execution Timeline + Run Control
Turned the Phase 5 structured run data into a visible, controllable experience.
All additive — the run lifecycle, sandbox chokepoint, role separation, and
explicit-dispatch contract are unchanged.
- **Live timeline.** New read-only `GET …/runs/{id}/events` returns the parsed
  `events.jsonl` (tolerant `run_store.read_events`). The run detail modal renders
  a curated `RunTimeline` (plan / task / command / verification / browser /
  cancel events → labelled rows) and polls it while the run is active. The chat
  card gained a live **phase badge** (planning → executing → verifying /
  repairing → browser verification → cancelling) and a **multi-task checklist**,
  both derived from `run.json` (no extra fetch).
- **Cancel.** New terminal `RunStatus.CANCELLED` (runner-set only, never
  agent-declarable, never reconciled). `BackgroundRunManager` keeps a per-run
  `threading.Event` registry; `POST …/runs/{id}/cancel` sets `cancel_requested`
  and signals it. The runner checks the flag at each step boundary
  (planning / per-task / pre-tool-dispatch) → `_finalize_cancelled` writes a
  terminal `cancelled` + artifacts and skips verify/browser/reconcile.
  **Cooperative:** an in-flight LLM call or ≤30 s shell finishes first. An
  orphaned `running` run (post-restart, no worker) is finalized by the endpoint
  directly — guarded by a re-read of run.json so it can't clobber a run that
  just settled.
- **Retry.** `POST …/runs/{id}/retry` re-reads the original `task_card.md`
  (`run_store.read_task_card`) and dispatches a fresh **linked** run
  (`retry_of` on the new record, `retried_by` + a `run_retried` event on the
  original). Terminal-only (409 while running); explicit user click — no
  auto-rerun, no hidden dispatch. The chat card surfaces it via a "Retry →
  view new run" affordance; the Runs panel refreshes to show it.
- **New events:** `run_cancel_requested`, `run_cancelled`, `run_retried`.
- **Scope notes:** polling only (no SSE); cancellation is cooperative at step
  boundaries and does not kill the in-flight subprocess (the later readiness pass
  moved `run_shell` to `Popen` for *timeout* tree-kill, but cancel still doesn't
  reach a running command); retry creates a new run rather than mutating history.

### Real-World Readiness Hardening (Windows smoke-test pass)
A readiness review (adversarially verified) + a real Windows full-stack smoke
test — building a "LaunchBoard" planning dashboard end-to-end through the normal
UI/API path — hardened the execution layer. All changes are targeted and
stability-focused; sandbox chokepoint, role separation, explicit-dispatch, and
verification semantics are unchanged.
- **Confirm-path blocker fixed.** `/execution/pending/{id}/confirm` (the
  natural-language **OK, run this** button) now lazily inits the workspace like
  `@code` does — it previously 404'd on a brand-new project, dead-ending the
  pending plan with no run dispatched.
- **Windows subprocess robustness.** Every execution-layer `subprocess` that
  captures output now decodes `encoding="utf-8", errors="replace"` (the default
  machine codec — cp936/GBK on the test box — raised `UnicodeDecodeError` on
  npm/Vite's UTF-8 output, dropping logs or killing a `_StreamDrainer`). `run_shell`,
  the dev-server teardown, and the dependency installer reap the **whole process
  tree** on timeout/teardown (`taskkill /F /T`) so node can't orphan and hold port
  5174; `run_shell` clamps its timeout to `[1, 600] s`.
- **Atomic artifacts.** `run.json` / `plan.json` / `result.md` write via temp
  file + `os.replace` (`run_store._atomic_write_text`), eliminating torn-read
  404/flicker under 2 s UI polling.
- **Coding-agent token budget.** The agent loop uses `max_tokens = 8192` (was the
  `llm.chat` default 2048) so a full file written inline as JSON can't truncate
  mid-string → parse-fail → task-fail.
- **Planning fidelity.** `looks_complex` now also counts comma/conjunction
  clauses, so a terse one-line full-stack card reliably triggers Phase 5
  decomposition instead of one monolithic loop. `MAX_TASK_STEPS` 12 → 16 so a
  frontend-heavy unit doesn't exhaust and cascade dependents to `skipped`.
- **Artifact consistency.** Single-task plans sync `plan.tasks[0].status` to the
  run's terminal status; the orphan-cancel endpoint settles in-flight plan tasks
  to `skipped` and rewrites `plan.json`.
- **Browser/preview.** Readiness timeout 30 → 60 s (cold Vite + Windows AV
  scanning); default dev command gains `--strictPort` (fail loud instead of
  silently moving to 5175 and desyncing the hardwired URL).
- **UI.** The Runs-panel row shows `verifying` during a UI-triggered browser
  verification; the timeline renders `verification_repair_failed` + `run_retried`.
- **Model + deps.** Default Claude model is a current alias (`claude-sonnet-4-5`);
  the previous pinned id `claude-sonnet-4-20250514` now returns 404, which broke
  every LLM call. `playwright` is added to `requirements.txt` (one-time
  `python -m playwright install chromium`).
- **Deferred (documented, low-risk):** events.jsonl append lock, cosmetic
  cancel-vs-runner `run.json` race, cancel-during-finalize no-op, multi-node
  dependency-cycle detection, partial-`node_modules` reinstall heuristic,
  reverify skip-already-passed, the `format`/`ssh` block-list substring
  false-positives (left untouched to avoid weakening the sandbox), and the unused
  `steps_used` field.

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
- **Execution is planned then task-by-task (Phase 5).** Complex runs decompose
  into a persisted task graph and execute single-threaded in topological order;
  simple runs still take the original single-loop path. Planning is read-only
  and best-effort (always falls back to one task). No parallel/subagent
  execution yet — `depends_on` is recorded but the runner is single-threaded.
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
| `test_uploads.py`                 |    14 | 07.0 sanitize/dedup/storage, workspace copy, upload HTTP      |
| `test_providers.py`               |    19 | 07.1 availability, default order, dispatch/parse, chat routing |
| `test_planner.py`                 |    28 | Phase 5 heuristic gate (+ clause heuristic), plan parse/fallback, graph + aggregation |
| `test_runner_planning.py`         |     7 | Phase 5 plan→tasks integration: decompose, skip, fallback, gate, artifact consistency |
| `test_run_control.py`             |    15 | Run control: events/task-card readers, cooperative cancel, cancel/retry endpoints, orphan + race guard, orphan plan-settle, retry-of-cancelled |
| **Total**                         | **282** |                                                            |

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
Parallel / subagent task execution (Phase 5 records `depends_on` but runs
single-threaded); a *streaming* run-event UI (SSE) over the richer
`events.jsonl` to replace the 2 s timeline polling; hard cancellation that kills
the in-flight subprocess (today's cancel is cooperative at step boundaries);
cross-project memory linking; multi-user / shared deploy (needs auth + per-user
workspaces + a different DB story). *(Run cancellation + retry + a polled live
timeline landed — see "Live Execution Timeline + Run Control".)*

---

## How to use this document

1. Read `CLAUDE.md` — the rules of engagement.
2. Read `ARCHITECTURE.md` — the current shape of the system.
3. Skim the most recent Phase 3 entries here for what just landed.
4. Propose your task; don't re-litigate decisions recorded above without new
   information. When it lands, update the right doc(s) per the policy table.
