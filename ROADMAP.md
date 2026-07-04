# Agent OS — Roadmap & Implementation Status

> **Read order for a new session:** `CLAUDE.md` (stable rules) →
> `ARCHITECTURE.md` (the system's current shape — files, pipelines, invariants,
> lessons) → this file (how it got here + what's next). `README.md` is the
> public landing page.

A compressed evolution log: what landed, in what order, plus current constraints
and next steps. **How each piece works now lives in `ARCHITECTURE.md`** — this
file records the history and doesn't duplicate the module/pipeline detail. Phase
numbers are historical labels, newest work last.

---

## Documentation Policy

Four docs, each with one job. Never duplicate content across them.

| File | Audience | Job |
|------|----------|-----|
| `README.md` | Public visitors | Short pitch + setup. Bump only on user-visible change. |
| `ARCHITECTURE.md` | Any agent picking up the repo | Current shape: files, pipelines, invariants, lessons. |
| `ROADMAP.md` | The builder + future sessions | Evolution log, constraints, next steps. |
| `CLAUDE.md` | Any coding agent here | Stable operating rules. Phase-independent. |

When a task lands: README gets a 1–2 line bump (if user-visible); ROADMAP gets a
compressed history entry; ARCHITECTURE is updated if a module / pipeline /
invariant / lesson changed; CLAUDE.md only if a constitutional rule changed.

---

## Phase 1 & 2 — Foundation (stable)

Workspace + project/conversation management (local FS layout, FastAPI backend,
React three-column UI, CRUD + auto-titling). Markdown memory — global (`SOUL`,
`USER`, `WORKSTYLE`, `MEMORY`) + per-project (`PROJECT`, `STATUS`, `TASK_QUEUE`,
`DECISIONS`, `RESEARCH`); UI-editable; `SOUL.md` read-only + hidden, loaded every
turn. LLM orchestration with semantic memory writeback (a second LLM call
proposes structured updates that the backend policy-filters before writing).

## Phase 3 — Execution Layer (complete through 06.2E)

The arc: a sandboxed Coding Agent to delegate to → make delegation safe +
explicit → surface runs in the UI → close the loop with verification + preview.

- **Sandboxed execution.** Per-project `execution_workspaces/{id}/`;
  `ProjectSandbox` + `ToolRuntime` as the single chokepoint (six file/shell tools
  with output caps); `CodingAgentRunner` drives a bounded JSON tool loop emitting
  run artifacts.
- **Triggering + surfacing.** `@code <task>` dispatches (rejected in GENERAL);
  `BackgroundRunManager` returns a `running` record and finalizes off-thread
  (crash → `failed`). Runs panel + `RunDetailModal`.
- **Safe, explicit delegation.** An LLM **delegation judge** classifies each
  non-`@code` message and only *proposes* (heuristic fallback); a
  `dispatch_suggested` becomes a **confirmable pending plan** — only a user click
  dispatches.
- **Closing the loop.** Best-effort post-run **memory reconciliation**;
  main-agent **on-demand file inspection** (bounded, no auto-injection);
  automatic **command verification** (manual block or inferred) + bounded repair
  gating `completed`; **browser verification** (chat-first: install → dev server
  on 5174 → screenshot → handed to a per-project preview registry).

## Phase 5 — Execution Orchestration (plan → tasks → execute)

Upgraded the Coding Agent from a flat loop into a phased, inspectable run.
Additive — the run record, role separation, sandbox chokepoint, and
verification/browser tail are unchanged.

- **Plan → tasks → finalize.** `looks_complex` gates cost (simple cards skip the
  planner, byte-identical); complex cards run a read-only inspection loop → an
  `ExecutionPlan` (`plan.json`), always falling back to a single task on failure.
  The runner executes units in topological order, skips dependents of failed
  deps, aggregates a status. Single-threaded (`depends_on` left room for
  Phase 9's parallelism).
- **Live timeline + run control.** Settled `RunTimeline` + a live phase badge /
  task checklist; **Cancel** (cooperative → terminal `cancelled`) and **Retry**
  (fresh linked run). Polling only (no SSE).
- **Real-time trace + progressive metrics.** Live `RunTrace` modal streams the
  activity thread (raw `llm_response` dropped); counts climb live during a run.
- **Browser verification v2 + AI visual judgment.** Render-readiness-gated,
  multi-page capture; a vision model gives a diagnostic-only verdict
  (`visual_review.json`), skipping gracefully without a vision model.
- **Hardening (Windows + autonomous build "Aegis").** Two adversarial-review +
  real-build passes hardened the layer; builds now complete autonomously
  end-to-end (the committed **Aegis Launch Control** showcase). Durable gotchas →
  `ARCHITECTURE.md §10`.

## Phase 4 — Interface & UX

- **07.0 — Multi-modal composer.** Auto-growing textarea (`Ctrl/Cmd+Enter`),
  voice dictation, file attachments with "add to workspace" (sanitized + deduped;
  storage + UX only, no RAG). New dep: `python-multipart`.
- **07.1–07.3 — Provider Registry 2.0.** Six providers (Claude / GPT / Gemini /
  DeepSeek / Kimi / GLM), key-presence availability, a per-provider model registry
  with per-model `vision` flags + env-overridable defaults (Claude
  `claude-opus-4-8`), capability gating (image upload + visual judgment only for
  vision-capable selections). Anthropic via SDK, others via `urllib` (no new
  deps). Header provider selector + a compact `ModelPicker`. Light/Dark theme via
  `data-theme` + CSS variables. *Out of scope:* per-project model memory, provider
  fallback, cost tracking, streaming, image generation.

## Phase 6 / 6.1 — Main Agent Orchestration & Memory v2

Rebalanced toward the Main Agent. Additive; every invariant unchanged. Recovery
is confirmable-only (no auto-dispatch, except the scoped recovery budget below).

- **Memory Engine v2.** New `memory_engine.py` owns the single atomic markdown
  write path; `orchestrator` + `memory_reconciliation` delegate to it (killed the
  duplicated writers). Idempotent `ensure_memory_scaffold`.
- **Memory Intake v2.** One structured `judge_memory_intake → MemoryDecision`
  every meaningful turn; the reason is surfaced to the UI.
- **Intent Router v2.** The judge emits a richer informational `intent`; new
  deterministic mode commands `@plan`/`@design`/`@debug`/`@review`/`@inspect`/
  `@memory` shape the response (a non-dispatch intent also routes to the matching
  mode). None dispatch.
- **Confirmable recovery.** `recovery.assess_run` interprets a non-green run and
  recommends one bounded step (`RecoveryAssessment`), surfaced as a confirmable
  pending card — the user still clicks OK.
- **Context Loader v2 (6.1).** `_compact_memory` keeps the main prompt compact as
  memory grows (STATUS/PROJECT/SOUL whole; append-growth files tail-trimmed over a
  threshold; byte-identical below it).
- **User-approved recovery budget (6.1).** Confirming a plan can grant a bounded
  auto-recovery allowance (none/1/2): a non-green run auto-dispatches ONE linked,
  audited recovery run (clean-finalize only, decrementing budget, hard cap 2,
  idempotent). Clamped at the confirm endpoint — the explicit-approval boundary.
  Inferred intent still never runs code.

## Phase 7 — Project Ops & GitHub Lifecycle

Turned Agent OS into a **project-lifecycle** system: a finished run becomes a
traceable, user-approved Git/GitHub delivery — checkpoint → reviewed diff →
commit → branch/push → PR — every external/destructive step behind an approval
gate. Additive; invariants preserved. (BLUEPRINT Pillar 1.)

- One Git executor (`ToolRuntime.run_git`); `run_shell` strengthened to block
  destructive Git; `run_git` is not an agent tool.
- `git_ops` (checkpoint, redacted diff, secret-refusing commit, gated rollback);
  `credentials.py` as the single secret reader (token → git only via
  `GIT_ASKPASS`); `github_connector` (REST, no `gh` CLI).
- Explicit two-phase External Action Contracts for commit/push/PR/rollback;
  `GitOpsPanel` drives them. The brain sees a compact git-state summary in
  `@review`/`@debug`, never the raw diff.
- **Validated live end-to-end:** build → verify → checkpoint → commit → branch →
  push (main + feature) → **PR #1** → rollback. Caught + fixed one real bug
  (`validate_git` rejected newlines, breaking multi-line commit messages; now only
  NUL is rejected). Secret-leak audit clean.

## Phase 8 — Production Path (validated live)

Moved what Agent OS can deliver from local preview to shipping a minimal real
SaaS through one golden path: **Vercel (deploy) + Supabase (Postgres/Auth/
migrations) + Stripe (test-mode checkout/webhooks)**. Built on the Phase 7 rails
(two-phase contracts, `credentials.py` sole secret reader, sandboxed executors,
best-effort run linkage). Additive; invariants preserved. (BLUEPRINT Pillar 2.)

- **Credential spine + app-env registry.** `credentials.py` generalized to a
  provider registry (`github|vercel|supabase|stripe`) with a Stripe live-gate and
  hardened redaction; `app_env.py` holds the built app's env vars (presence-only).
- **Connectors + contracts.** `vercel_connector` (async deploy: confirm returns
  immediately, finalizes off-thread polling `READY`, writes `deployment.json`/
  `deploy.log` + an `OPS.md` ledger entry via the one deterministic `ops_ledger`
  writer). `supabase_connector` + `run_supabase` (scrubbed-env CLI executor;
  destructive-by-subcommand gating; migration preview/apply contracts).
  `stripe_connector` (form-encoded, per-request test-gate; provision + webhook
  register, the returned `whsec_` stored but never echoed).
- **Startup reconciliation (8.7).** `reconcile_stuck_external_actions` clears a
  crash-left transient deploy state by querying the provider — never auto-retries
  a partially-applied external action.
- **Validated live end-to-end (8.8).** Drove the full path on `agent-os-phase8-e2e`:
  the Coding Agent built **"CloudNotes"** (Next.js 14 SaaS, Supabase Auth/RLS +
  Stripe test checkout + webhook) from an empty repo → build → commit → push →
  **Vercel production deploy (READY)** → Supabase `link` + migration
  (`db push --linked`, verified live) → Stripe test Product+Price → deployed
  webhook → env pushed. Proven at the live URL: a signed
  `checkout.session.completed` → deployed webhook → 200 + a row persisted in
  Supabase (full payment→persistence loop). Secret-leak audit clean. Caught +
  fixed real bugs (Vercel rejects a Sensitive var targeting `development`; the
  generated app needed `vercel.json {"framework":"nextjs"}`; a follow-up migration
  added a diverged column). Operational findings (not code-changed): cold Next.js
  `npm install` can exceed the 600 s `run_shell` ceiling; the connectors don't
  retry transient network errors (`deployment.json` is the source of truth).

## Phase 9 — Agent Teams & Parallel Execution

Turned the single-threaded execution layer into a **team runtime**: a complex
task decomposes into role-assigned units, independent work runs in parallel in
isolated patch workspaces, a deterministic integration step merges the outputs
(conflicts surfaced, never silent), and the existing verification tail becomes a
**global gate over the integrated tree**. Additive — the sequential/simple paths
are byte-identical (all pre-existing tests unchanged); every invariant holds.
(BLUEPRINT Pillar 3. Module/pipeline detail: `ARCHITECTURE.md §5–§9`.)

- **Role registry** (`roles.py`) — execution roles coder (default, empty overlay)
  / reviewer / inspector (read-only, deliverable = findings); system stages
  integrator / verifier; the chat `@`-mode ↔ role map. Enforced `allowed_tools`;
  unknown role → coder.
- **Isolated patch workspaces** (`patch_workspace.py`) — `PatchToolRuntime`:
  overlay writes, fall-through reads, blocked shell/git/supabase, all paths via
  the new `sandbox.resolve_under`; per-task `manifest.json`.
- **Team planning** — plan schema gains per-task `role`/`parallel` (conservative
  rules); `compute_waves` (Kahn layering, cycles forced sequential) +
  `plan_is_team_eligible` (the gate: an LLM-planned plan with a wave of ≥2
  parallel-eligible tasks).
- **Deterministic integration** (`integration.py`) — per-wave, first-writer-wins,
  identical content de-dupes, conflicting content + apply-errors surfaced and cap
  the run at `partial`; detail in `integration.json`.
- **Wave scheduler** (`runner._run_execution_phase_team`) — bounded parallel pool
  (`MAX_PARALLEL_AGENTS=3`, never the shared dispatch pool), coders in patch
  workspaces + read-only roles on the shared repo with tools enforced in-loop;
  the coordinator is the sole run.json/plan.json writer (workers append
  `task_id`-tagged, per-run-locked events); read-only findings flow into
  later-wave prompts.
- **Team trace** — new events (`team_execution_started`, `wave_started`,
  `integration_*`), transient `integration_state`, wave-grouped checklist with
  role chips, a Team Integration detail section, and interleaving-safe Live-Trace
  pairing. result.md gains role/wave + `## Integration` (sequential runs
  byte-identical).

**Validated live end-to-end.** Drove a real team run on `phase9-team-e2e` with
live Claude: "build a dependency-free `textkit` package (3 independent modules +
wiring + review)" planned as a **5-task team run**. **Wave 1** ran three coders
**concurrently in isolated patch workspaces** (each overlay held only its own
file — isolation proven); **integration** applied all three, 0 conflicts;
**wave 2** wired `__init__.py`; **wave 3** ran the **reviewer**. The **global
gate** (`compileall` over the integrated tree) passed → `completed`, and the real
package imports and handles every edge case the planner flagged. Team-trace
events + patch manifests all present; secret-leak audit clean. Conflict/
apply-error handling (surface + blocker + cap at `partial`) is proven
deterministically by `test_integration` / `test_team_runner` (the live golden
path is disjoint, as correct decomposition should be). **Adversarial review
before the E2E caught + fixed 6 real defects:** a critical **task-id path-escape**
(unsanitized id as a patch-dir segment → sanitized at plan parse + a layout
containment guard), a widened **`.git` write guard** (was `.git/config`-only),
**integration apply-errors now degrade status**, the crash handler now **clears
every transient sub-status**, and the **cyclic-wave dependency gate** re-checks
just-in-time to match sequential skip semantics.

---

## Current Constraints

- **Explicit dispatch only.** `@code` runs immediately; inferred intent only ever
  produces a confirmable pending plan (the judge / confirm endpoint reject
  GENERAL). See `ARCHITECTURE.md §9` / `CLAUDE.md §5`.
- **Execution is wave-scheduled with bounded parallelism.** A team-eligible plan
  runs ≤3 concurrent units/wave (write tasks isolated + integrated); everything
  else runs the legacy single-threaded loop byte-identical. No unbounded spawning;
  parallel units never run shell; conflicts/apply-errors cap a run at `partial`.
- **No streaming** on `/api/chat` — full response in one shot.
- **Verification.** Automatic command verification + bounded repair gates
  `completed`; an opt-in `## Browser Verification` block (or the chat button) adds
  a render-gated multi-page capture + a diagnostic-only visual judgment (skips
  without a vision model).
- **3–4 LLM calls per non-`@code` turn** (delegation judge + optional inspection +
  chat response + memory judge).
- **Main agent never auto-reads repo contents** — only the bounded inspection loop
  (max 3 reads/turn).
- **Single-user, single-process.** No auth, no shared deploy.

---

## Test Coverage

Backend tests live under `backend/tests/`, stub `llm.chat` (no API key needed),
and are each runnable standalone (`python tests/<file>.py`). **604 total**;
`npm run build` (tsc + vite) green.

| File | Tests | Covers |
|------|------:|--------|
| `test_delegation_judge` | 15 | judge decisions, fallbacks, parsing |
| `test_pending_execution` (+`_db`) | 17 / 6 | serialization, revision LLM, renderers; SQLite lifecycle + FK cleanup |
| `test_memory_reconciliation` | 26 | parser, skip rules, e2e pipeline |
| `test_inspect` | 29 | sandbox, parser, orchestrator loop |
| `test_verification` (+`_inference`) | 21 / 23 | parser, runner integration; inference, multi-command, pytest probe, repair |
| `test_browser_verification` | 29 | lifecycle, drainer, Playwright, multi-page capture |
| `test_runner_diagnostics` | 9 | observed activity, `sweep_stuck_runs` |
| `test_ui_browser_verification` | 15 | UI flow + visual review (diagnostic-only + skip) |
| `test_preview` | 12 | preview registry, `deps_installed` |
| `test_chat_first_endpoints` | 3 | browser-verify sub-status, preview start/stop |
| `test_uploads` | 14 | sanitize/dedup/storage, workspace copy |
| `test_providers` | 40 | six-provider registry, model/vision gating, env aliases, chat routing |
| `test_planner` | 28 | heuristic gate, plan parse/fallback, graph |
| `test_runner_planning` | 7 | plan→tasks integration: decompose, skip, gate |
| `test_run_control` | 16 | events/task-card readers, cancel/retry, orphan guard, `since` cursor |
| `test_llm_retry` | 4 | transient-vs-permanent LLM error classification |
| `test_autonomy_hardening` | 11 | dependency skip, productive continuation, iterative repair |
| `test_live_metrics` | 5 | progressive run-level + per-task metrics |
| `test_visual_judge` | 16 | gate/skip, JSON parse, never-raises, model resolution |
| `test_memory_engine` | 13 | apply_update policy/atomic/dedup; scaffold; write-path |
| `test_memory_intake` | 10 | structured intake judge: parse, filter, reason, apply |
| `test_intent_router` | 18 | mode `@`-command parser, `intent`, mode→prompt shaping |
| `test_recovery` (+`_budget`) | 8 / 9 | assess_run non-green/idempotent; auto-recover gating/cap/decrement |
| `test_context_loader` | 8 | `_compact_memory` identity/trim behavior |
| `test_sandbox_git` (+`_supabase`) | 12 / 4 | run_shell block, validate_git/_supabase allow/deny + gating + scrubbed env |
| `test_git_ops` | 12 | ensure_repo/checkpoint/diff+redact/commit-refusal/branch/rollback |
| `test_git_store` | 12 | git/deploy RunRecord round-trip, artifacts, result.md sections |
| `test_checkpoint_diff` | 4 | dispatch checkpoint + inherit, finalize diff capture |
| `test_credentials` | 16 | multi-provider registry, redaction hardening, Stripe live-gate |
| `test_github_connector` | 11 | remote parse/tokenless, status validate, push token-in-env, PR REST |
| `test_git_endpoints` | 10 | status/diff/commit/push/PR/rollback/credentials routes |
| `test_git_context` | 5 | `_latest_git_state_context` summary-only + mode folding |
| `test_app_env` | 5 | app-env set/list/delete presence-only, value reader, redaction |
| `test_vercel_connector` (+`_endpoints`) | 11 / 8 | token-in-header, redacted, deploy/get/list/promote/env, url normalize; routes + stuck-action reconcile |
| `test_ops_ledger` | 5 | OPS.md ledger write + no-leak + idempotency + judge-write rejection |
| `test_phase8_invariants` | 2 | orchestrator imports no connector; OPS.md excluded from judge sets |
| `test_supabase_connector` | 5 | migration apply secrets-in-env/destructive, redaction, Docker-missing |
| `test_stripe_connector` | 7 | form-encoding+Idempotency-Key, test/livemode gate, webhook-secret-never-returned |
| `test_roles` | 11 | role registry contracts, tool sets, mode↔role map, coder fallback |
| `test_patch_workspace` | 13 | overlay read/write/append/list/search, `.git` guard, unsafe-id containment, blocked executors |
| `test_team_planner` | 18 | role/parallel parsing, wave layering, cycle handling, eligibility gate, id sanitization |
| `test_integration` | 7 | clean apply, dedupe, first-writer-wins conflict, sandbox re-validation, failed-task output |
| `test_team_runner` | 9 | end-to-end team path: parallel-overlap proof, isolation+integration, conflicts/apply-errors→partial, read-only bounce, findings flow, cancel mid-wave, sequential gate, one-unit-crash survival |

---

## Recommended Next Steps

**Next up.**
- **Auto-repair on a failed visual verdict** — feed a `failed` verdict + evidence
  back as one bounded repair pass (reuse `_verify_with_repair`), then re-capture +
  re-judge once. Capped + opt-in.
- **Per-view targeting** — let a `## Browser Verification` block list explicit
  views/paths for apps whose nav isn't auto-discoverable.

**After that.**
- **Streaming** (SSE on `/api/chat`) → `llm.py`, `orchestrator.py`, the chat
  endpoint, `ChatPanel.tsx`; then a **run event stream** replacing 2s polling.
- **Cost / latency** — the 3–4 LLM calls/turn is the lever (cache the judge, merge
  the memory judge into the main response, gate inspection).

**Longer-term, not committed.** Deeper team parallelism (LLM-assisted conflict
resolution beyond first-writer-wins; per-view preview servers per parallel unit);
hard cancellation that kills the in-flight subprocess; cross-project memory
linking; multi-user / shared deploy (needs auth + per-user workspaces + a
different DB story).

---

## How to use this document

1. Read `CLAUDE.md` (rules) → `ARCHITECTURE.md` (current shape) → the most recent
   entries here for what just landed.
2. Don't re-litigate decisions recorded above without new information.
3. When a task lands, update the right doc(s) per the Documentation Policy.
