# Agent OS — Roadmap & Implementation Status

> **Read order for a new session:** `CLAUDE.md` (stable rules) →
> `ARCHITECTURE.md` (the system's shape — files, pipelines, invariants) → this
> file (how it got here + what's next). `README.md` is the public landing page.

A compressed evolution log: what landed, in what order, plus current constraints
and next steps. Deep implementation detail and the hard-won gotchas live in
`ARCHITECTURE.md` (see its §10 "Lessons worth keeping"); this file does not
duplicate them. Phase numbers are historical labels — sections are grouped by
theme (foundation → execution → interface), newest work last.

---

## Documentation Policy

Four docs, each with one job. Never duplicate content across them.

| File              | Audience                      | Job                                            |
|-------------------|-------------------------------|------------------------------------------------|
| `README.md`       | Public visitors on GitHub     | Short pitch + setup. Bump only on user-visible change. |
| `ARCHITECTURE.md` | Any agent picking up the repo | The whole picture: files, pipelines, invariants, lessons. |
| `ROADMAP.md`      | The builder + future sessions | Evolution log, constraints, next steps.        |
| `CLAUDE.md`       | Any coding agent here         | Stable operating rules. Phase-independent.     |

When a task lands: README gets a 1–2 line bump (only if user-visible); ROADMAP
gets a compressed entry under the right phase; ARCHITECTURE is updated if a
module / pipeline / invariant / lesson changed; CLAUDE.md only if a
constitutional rule changed.

---

## Phase 1 & 2 — Foundation (stable, unchanged since)

- **Workspace + project/conversation management.** Local filesystem layout
  (`memory/`, `projects/{id}/`), FastAPI backend, React three-column UI
  (projects / chat / context). Project + conversation CRUD, auto-titling.
- **Markdown memory.** Global (`SOUL.md`, `USER.md`, `WORKSTYLE.md`, `MEMORY.md`)
  + per-project (`PROJECT.md`, `STATUS.md`, `TASK_QUEUE.md`, `DECISIONS.md`,
  `RESEARCH.md`). UI-editable; `SOUL.md` is read-only + hidden, loaded every turn.
- **LLM orchestration + semantic memory writeback.** Each turn assembles memory
  context (`orchestrator.py` + `llm.py`); a second LLM call proposes structured
  JSON memory updates that the backend policy-filters before writing
  (`SOUL.md` always excluded).

---

## Phase 3 — Execution Layer (complete through 06.2E)

The arc: a sandboxed Coding Agent the main agent can delegate to → make
delegation safe + explicit → surface runs in the UI → close the loop with
automatic verification and a live preview.

- **Sandboxed execution (05.1–05.3).** Per-project `execution_workspaces/{id}/`
  (`repo/`, `runs/`, `logs/`, `AGENT.md`, `TASK.md`), idempotent init.
  **`ProjectSandbox` + `ToolRuntime` are the single chokepoint** — path +
  command validation; six file/shell tools with output caps; nothing touches
  repo paths or the shell except through here. `CodingAgentRunner` drives a
  bounded JSON tool loop, emitting `task_card.md` / `events.jsonl` / `run.json` /
  `result.md`.
- **Triggering + surfacing (05.4–05.7).** `@code <task>` dispatches a run
  (rejected in GENERAL). `BackgroundRunManager` (`ThreadPoolExecutor`) returns a
  `running` record immediately and finalizes off-thread; a crashed worker →
  `failed`. Runs panel (newest first, polls 2s while active) + `RunDetailModal`;
  a run-id in chat renders a "View Run" button.
- **Safe, explicit delegation (05.8–05.9.5).** A small LLM **delegation judge**
  classifies each non-`@code` message (`dispatch_suggested` / `discussion` /
  `memory_only`) and **never dispatches — only proposes**; a rule-based
  heuristic is the fallback. `dispatch_suggested` → a **confirmable pending
  plan** (PM-tone reply + **OK, run this** / **Revise plan**); only a user click
  dispatches (same path as `@code`).
- **Closing the loop (06.0–06.2E).** Terminal runs run a bounded, best-effort
  **memory reconciliation** (`STATUS`/`TASK_QUEUE`/`DECISIONS`/`RESEARCH` only,
  at most once, never fails the run). The main agent can **inspect specific
  `repo/` files on demand** (bounded read-only loop, max 3/turn — no
  auto-injection). **Command verification is automatic + multi-command**: a
  manual `## Verification` block else inferred (`npm install`+`build`, `pytest`
  iff importable, else a `compileall` syntax check); failures get a **bounded
  iterative repair pass**; `completed` requires a pass (or safe skip).
  **Browser verification** evolved MVP → user-triggered → **chat-first**: a run
  posts a running note then a summary with **Run browser verification**, which
  installs deps, starts a dev server on **port 5174**, captures a screenshot, and
  hands the server to a per-project **preview registry** so the URL stays live
  (Runs panel **Start / Stop preview**).
- **Housekeeping.** Delete-path FK cleanup + Windows-safe workspace `rmtree`
  (opt-in via "Delete its workspace too"); `.gitignore` ships public
  `*.example.md` templates while keeping private contents out.

---

## Phase 5 — Execution Orchestration (plan → tasks → execute)

Upgraded the Coding Agent from one flat loop into a phased, inspectable
orchestration. Additive — run record, role separation, sandbox chokepoint,
explicit-dispatch contract, and the verification/browser/preview/reconciliation
tail are unchanged.

- **Plan → tasks → finalize.** A cheap pure heuristic (`looks_complex`) gates
  cost: simple cards skip the planner (legacy path, byte-identical); complex
  cards run a **bounded read-only inspection loop** ending in a `plan` action.
  The plan (`ExecutionPlan` of `ExecutionTask` units with `depends_on`, persisted
  as `plan.json`) **always falls back to a single task** on any failure. The
  runner executes units in topological order, skips dependents of failed deps,
  records per-task status/files/commands/blockers, and aggregates a run status.
  Single-threaded — `depends_on` leaves room for future parallel execution.
  Richer `events.jsonl` (`phase` tags + plan/task events); `GET …/runs/{id}/plan`;
  a read-only "Plan & Tasks" UI block. Design refs (architecture only):
  OpenHarness / openclaude / opencode.

- **Live timeline + run control.** `GET …/runs/{id}/events` (tolerant parse,
  optional `since` cursor) feeds a **settled** `RunTimeline` (start/settle pairs
  collapsed, no stale spinners) and a live **phase badge + task checklist** in
  the chat card. **Cancel** → cooperative at step boundaries → terminal
  `cancelled` (runner-set only, never reconciled); orphaned runs finalized by the
  endpoint under a re-read guard. **Retry** → a fresh **linked** run
  (`retry_of` / `retried_by`), terminal-only, explicit. Polling only (no SSE).

- **Real-time trace + progressive metrics.** The runner flushes observed
  file/command activity to `run.json` *during* a run (counts climb live; finalize
  still owns the authoritative lists). A dedicated **Live Trace** modal
  (`RunTrace.tsx`) streams the chronological activity thread (raw `llm_response`
  dropped — no chain-of-thought), polling the `since` cursor.

- **Browser verification readiness + multi-page + AI visual judgment.** Capture
  now **waits for a genuine render** (networkidle + DOM-populated + body-text
  stability, not just `load`) and marks `readiness="unconfirmed"` on timeout
  rather than failing. It **discovers + captures a few views** (tabs / route
  links / nav buttons; `MAX_BROWSER_PAGES=4`; stable `browser.png` + `page-NN.png`).
  Then a vision model runs an **AI visual judgment** over the screenshots
  (`passed`/`warning`/`failed`/`inconclusive` + concise rationale, persisted as
  `visual_review.json`). **Diagnostic-only** — never changes run status — and
  **skips gracefully** without a vision-capable model. Vision support landed in
  `providers.py` (`complete_vision`) + `llm.chat_vision`.

- **Hardening passes (Windows readiness + autonomous-build "Aegis").** Two
  adversarial review + real-build passes hardened the execution layer
  (UTF-8 subprocess decoding + whole-tree kill, atomic artifact writes, planning
  fidelity, transient-LLM retry, progress-aware dependency skip, iterative
  repair, truthful-completion prompt rules, big-file write budget,
  previewable-architecture guidance). Outcome: builds now complete autonomously
  end-to-end (the committed **Aegis Launch Control** showcase). The durable
  gotchas from these passes are recorded in **`ARCHITECTURE.md §10`**.

---

## Phase 4 — Interface & UX (newest)

- **07.0 — Multi-modal composer** (`ChatPanel.tsx` + `uploads.py`). Auto-growing
  multiline textarea (`Enter` = newline, `Ctrl/Cmd+Enter` = send), Web Speech
  voice dictation, and file attachments (images + `.txt`/`.md`/`.pdf`/`.doc`/
  `.docx`) with an "add to workspace too" toggle. `POST /api/chat/upload`
  sanitizes + de-dupes files (chat-only, or copied into `repo/uploads/` via the
  sandbox); metadata rides the message so chips re-hydrate. Storage + UX only —
  no parsing / RAG. New dep: `python-multipart`.
- **07.1 — Pluggable model providers** (`providers.py`). Claude / GPT / Gemini /
  DeepSeek in a key-presence registry; Anthropic via SDK, the rest via `urllib`
  (no new deps). `GET /api/providers` + a `provider` field on `/api/chat` route
  the main response; a header dropdown lists providers (missing-key → disabled).
- **07.2 — Light theme.** A `:root[data-theme='light']` token block + a Dark/Light
  switcher (top-right header), persisted to `localStorage`; the whole UI
  re-themes via cascading CSS variables.
- **07.3 — Provider Registry 2.0** (six providers, model picker, vision gating).
  Upgraded 07.1 into a **capability-aware provider/model registry**:
  - **Six providers** — added **Kimi (Moonshot)** and **Zhipu GLM** (both
    OpenAI-compatible HTTPS; base URLs env-overridable for region hosts;
    `ZAI_API_KEY` / `KIMI_API_KEY` / `GEMINI_API_KEY` accepted as aliases).
  - **Per-provider model registry** with a per-model `vision` flag and an
    env-overridable default. Default ids refreshed to current, **doc-verified**
    models — Claude **Opus 4.8**, GPT **5.5**, Gemini **3.5 Flash**, DeepSeek
    **V4 Flash**, Kimi **K2.6**, GLM **5.2** (the Claude bump also lifts internal
    subsystem calls; override via `AGENT_OS_CLAUDE_MODEL`).
  - **Capability gating.** Chat image upload is offered only for vision-capable
    selections; the AI browser visual judgment prefers the selected vision-capable
    model, falls back to any available vision model, and skips otherwise.
    `/api/chat` validates the provider/model combo (accepting env-pinned
    defaults); `orchestrate(..., model=)` threads the selection.
  - **UI.** Provider selector stays in the header (07.1 behavior preserved); a
    compact upward-opening **model picker** (`ModelPicker.tsx`) sits above the
    composer with vision/text tags.
  - **Out of scope (per task):** per-project model memory, provider fallback,
    cost tracking, streaming, image generation.

---

## Phase 6 — Main Agent Orchestration & Memory v2

Rebalanced the system back toward the **Main Agent** (the execution layer had
outgrown it). Additive — every constitutional invariant is unchanged: SOUL.md
read-only, explicit-dispatch-only, no repo auto-injection, best-effort post-run
steps. Recovery is **confirmable-only** (no auto-dispatch).

- **Memory Engine v2.** New leaf module `memory_engine.py` (stdlib-only) owns the
  single markdown write path: a policy-filtered, **atomic** (temp + `os.replace`),
  OSError-guarded, append-deduped `apply_update(base_dir, allow=…)`, shared
  canonical section names + default-section map, and the structured
  `MemoryDecision`. `orchestrator.apply_memory_update` / `apply_global_memory_update`
  and `memory_reconciliation._apply_update` now all delegate to it (killed the
  duplicated, non-atomic writers). `ensure_memory_scaffold()` idempotently
  backfills missing project-memory files/sections (at project-create + a one-time
  startup migration; never from `load_memory`).
- **Memory Intake v2.** The two bare-array chat-turn judges collapsed into one
  structured `judge_memory_intake(scope, …) -> MemoryDecision` (should_update +
  reason + updates) that runs every meaningful turn; the **reason** is surfaced to
  the UI. Fixed an active bug: the main-agent system prompt no longer claims
  "no delegation path exists yet" (it shipped phases ago).
- **Intent Router v2.** The delegation judge now also emits a richer `intent`
  label (planning / design / build / debug / inspect / memory / docs /
  retrospective / research / discussion) — informational, routing still keys off
  the 3 dispatch decisions. New deterministic mode commands `@plan` / `@design` /
  `@debug` / `@review` / `@inspect` / `@memory` shape the response via
  `orchestrate(mode=…)` (e.g. `@debug` folds in the latest non-green run summary;
  `@inspect`/`@review` nudge the bounded inspection channel). **None dispatch** —
  only `@code` / confirm do.
- **Confirmable recovery.** New `execution/recovery.py` `assess_run()` (best-effort,
  never raises, mirrors reconciliation skip rules) interprets a non-green terminal
  run — `partial`/`failed`/`blocked`, plus the signals a `completed` status hides
  (failed command/browser verification, failed visual review) — and recommends
  one bounded next step (`inspect`/`repair`/`split`/`reverify`/`report`). Persisted
  to `RunRecord.recovery_assessment`; triggered in `runner._finalize` after
  reconciliation. A `needs_recovery` assessment becomes a **confirmable pending
  card** via `POST …/runs/{id}/propose-recovery` (reuses the existing pending-plan
  dispatch — the user still clicks **OK, run this**).
- **UI.** Run cards show a memory-reconciliation line + a "Next steps" recovery
  block with a **Run suggested fix** button; chat turns carry an intent badge +
  a "🧠 Memory updated — <reason>" chip (both persisted on the message so they
  survive reload). `types.ts` gained the 3 already-shipped reconciliation fields +
  `recovery_assessment`.

## Current Constraints

- **Explicit dispatch only.** `@code <task>` runs immediately; inferred coding
  intent only ever produces a **confirmable pending plan** — a user click (OK,
  run this) is the sole path to a run. The judge / confirm endpoint reject
  GENERAL. (Invariant; see `ARCHITECTURE.md §9` / `CLAUDE.md §5`.)
- **No streaming** on `/api/chat` — full response in one shot.
- **Verification.** Automatic command verification (manual block or inferred) +
  bounded repair gates `completed`; an opt-in `## Browser Verification` block (or
  the chat button) adds a render-gated, multi-page headless capture + a
  **diagnostic-only** AI visual judgment that **skips without a vision-capable
  model**.
- **Up to 3–4 LLM calls per non-`@code` turn** (delegation judge + optional
  inspection + chat response + memory judge); repair adds a bounded loop only on
  a failed `completed` run.
- **Main agent never auto-reads repo contents** — only the bounded inspection
  loop (max 3 reads/turn).
- **Execution is single-threaded** in topological order; `depends_on` is recorded
  but not yet parallelized.
- **Single-user, single-process.** No auth, no shared deploy.

---

## Test Coverage

Backend tests live under `backend/tests/`, stub the LLM caller (no API key
needed), and are each runnable standalone (`python tests/<file>.py`).

| File                              | Tests | Covers                                                  |
|-----------------------------------|------:|---------------------------------------------------------|
| `test_delegation_judge.py`        |    15 | 05.9 judge decisions, fallbacks, parsing                |
| `test_pending_execution.py`       |    17 | 05.9.5 serialization, revision LLM, renderers           |
| `test_pending_execution_db.py`    |     6 | 05.9.5 SQLite lifecycle + delete-path FK cleanup        |
| `test_memory_reconciliation.py`   |    26 | 06.0 parser, skip rules, e2e pipeline                   |
| `test_inspect.py`                 |    29 | 06.1 sandbox, parser, orchestrator loop                 |
| `test_verification.py`            |    21 | 06.2A parser, runner integration, sandbox path          |
| `test_verification_inference.py`  |    23 | 06.2E inference, multi-command, pytest probe, repair    |
| `test_browser_verification.py`    |    29 | 06.2B lifecycle, drainer, Playwright; multi-page capture |
| `test_runner_diagnostics.py`      |     9 | observed activity, `sweep_stuck_runs`                   |
| `test_ui_browser_verification.py` |    15 | 06.2C UI flow + visual review (diagnostic-only + skip)  |
| `test_preview.py`                 |    12 | 06.2D preview registry; 06.2E `deps_installed`          |
| `test_chat_first_endpoints.py`    |     3 | 06.2D browser-verify sub-status, preview start/stop     |
| `test_uploads.py`                 |    14 | 07.0 sanitize/dedup/storage, workspace copy, upload     |
| `test_providers.py`               |    40 | 07.1 + Registry 2.0: six-provider order, model registry + `is_known_model`, Kimi/GLM dispatch + vision, capability gating, env aliases/base-URL override, chat routing |
| `test_planner.py`                 |    28 | Phase 5 heuristic gate, plan parse/fallback, graph       |
| `test_runner_planning.py`         |     7 | Phase 5 plan→tasks integration: decompose, skip, gate    |
| `test_run_control.py`             |    16 | events/task-card readers, cancel/retry, orphan guard, `since` cursor |
| `test_llm_retry.py`               |     4 | transient-vs-permanent LLM error classification + retry  |
| `test_autonomy_hardening.py`      |    11 | dependency skip, productive continuation, iterative repair |
| `test_live_metrics.py`            |     5 | progressive run-level + per-task metrics                 |
| `test_visual_judge.py`            |    16 | gate/skip rules, JSON parse, never-raises, image cap; selected-model resolution + fallback/skip |
| `test_memory_engine.py`           |    13 | Phase 6 apply_update policy/atomic/dedup/replace; scaffold; orchestrator write-path |
| `test_memory_intake.py`           |    10 | Phase 6 structured intake judge: parse, policy filter, reason, no-op, apply |
| `test_intent_router.py`           |    14 | Phase 6 mode `@`-command parser, `intent` field, mode→prompt shaping |
| `test_recovery.py`                |     8 | Phase 6 assess_run: non-green/green/cancelled/idempotent/visual-failed, never-raises |
| **Total**                         | **391** |                                                        |

Frontend: `npm run build` (tsc + vite) green.

---

## Recommended Next Steps

**Next up.**
- **Auto-repair on a failed visual verdict.** Visual judgment is diagnostic-only
  today; feed a `failed` verdict + evidence back as one bounded repair pass (reuse
  the `_verify_with_repair` shape), then re-capture + re-judge once. Tightly
  capped + opt-in.
- **Per-view targeting.** Let a `## Browser Verification` block list explicit
  views/paths to capture for apps whose nav isn't auto-discoverable.

**After that.**
- **Streaming** (SSE on `/api/chat`) → touches `llm.py`, `orchestrator.py`, the
  chat endpoint, `ChatPanel.tsx`.
- **Run event stream** — replace 2s polling with a per-project SSE stream (shares
  plumbing with streaming).
- **Cost / latency** — the 3–4 LLM calls/turn is the lever (cache the judge, merge
  the memory judge into the main response via structured output, gate inspection).

**Longer-term, not committed.** Parallel / subagent task execution (`depends_on`
is recorded but the runner is single-threaded); hard cancellation that kills the
in-flight subprocess (today's is cooperative); cross-project memory linking;
multi-user / shared deploy (needs auth + per-user workspaces + a different DB
story).

---

## How to use this document

1. Read `CLAUDE.md` (rules) → `ARCHITECTURE.md` (current shape) → the most recent
   entries here for what just landed.
2. Don't re-litigate decisions recorded above without new information.
3. When a task lands, update the right doc(s) per the Documentation Policy.
