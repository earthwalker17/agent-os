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

## Phase 9.1 — Post-Phase-9 Hardening Pass (execution-layer pressure test)

A dedicated hardening pass over the execution / scheduling / orchestration /
delegation / recovery / project-management stack after Phase 9. An 8-agent
read-only adversarial review swept every subsystem; the load-bearing findings
were re-verified against the code and fixed in place (bounded, behavior-
preserving). Additive — every Phase 5–9 invariant holds; **635 backend tests**
pass (~33 new regressions), `npm run build` green. Full driver + evidence in the
session; no new feature layer.

- **Sandbox chokepoint (constitutional).** `resolve_under` now normalizes each
  path component the way Windows resolves it (trailing dots/spaces stripped) so a
  `".git."` / `".env."` / `"server.pem."` can no longer bypass the `.git`/
  sensitive-name guard and hit the real target (RCE via `.git/hooks`); Windows
  **reserved device names** (`NUL/CON/PRN/AUX/COM1-9/LPT1-9`) are now screened
  (they resolve to devices, not files → phantom "applied" writes).
- **Role enforcement everywhere.** The legacy sequential multi-task loop now
  applies the role overlay + enforced `allowed_tools` for read-only roles — a
  `reviewer`/`inspector` on a non-team-eligible plan (single-task waves) no longer
  runs as a full coder with write/shell against the live repo.
- **Transient-state hygiene.** `sweep_stuck_runs` clears **all six** transient
  `*_state` fields on a stuck-`running` run; a new **`sweep_terminal_transient_states`**
  clears a leaked in-progress state on an already-*terminal* run (e.g. a crash
  during the post-status verify tail left `verification_state='verifying'` forever),
  while **preserving** a settled `browser_verification_state` (`passed`/`failed`).
  (The over-aggressive first cut of this sweep — which wiped a settled
  `browser_verification_state` — was caught by a live backend-restart during the
  E2E and fixed.)
- **Concurrency / lost-update.** New per-`(project,run)` **`run_store.mutate_run_json`**
  lock (mirrors the events lock) closes unsynchronized read-modify-write races:
  the cancel endpoint no longer reverts a run that finalized during its TOCTOU
  window; browser-verify folds its result onto a fresh record (won't clobber a
  concurrent commit); the Vercel deploy confirm claims atomically (two confirms →
  one deployment) and the off-thread finalizer preserves a concurrent commit's
  fields. A DB-atomic **`claim_pending_execution`** makes double-confirm dispatch
  exactly one run (invariant: no two units write the same live tree).
- **Integration correctness.** Conflict detection keys on `os.path.normcase`, so
  a case-only collision (`src/App.ts` vs `src/app.ts`) on Windows/macOS surfaces
  as a conflict instead of one task silently overwriting the other. Clean waves
  now **reclaim** their patch-overlay file copies (keeping `manifest.json`) —
  bounded disk.
- **Secret hygiene.** The Gemini key moved from the request URL to the
  `x-goog-api-key` header (URLs are echoed into error messages/logs); `_safe_url`
  redacts any stray `key=`/`api_key=` query secret before it reaches a
  `ProviderError`.
- **Robustness.** Per-request LLM timeout (env-overridable) so a hung provider
  call can't wedge a parallel wave; `compileall` excludes the full skip-dir set
  (`.venv`/`venv`/`dist`/`build`/…), not just `node_modules`; Windows dev-server
  teardown reaps the whole tree (`taskkill /F /T`) on the happy path + a pre-start
  5174-in-use guard; `memory_engine` uses exact-heading replace + block-equality
  append dedup (no more clobbering `## Decisions Archive` or dropping a distinct
  short entry); SVG attachments serve as `attachment` + `nosniff` (stored-XSS);
  URL path-segment validation (`_safe_id` + a run_store structural guard) blocks
  `..`/separator traversal on `project_id`/`run_id`.
- **Observability.** The main-agent `@debug`/`@review` context now surfaces a
  team run's wave/role/integration shape (metadata only); the Run Detail modal
  adopts the events since-cursor; the poll gates gain a watchdog cap; the Live
  Trace pairs tool_call↔tool_result via an O(N) index (the old 200-event forward
  scan left interleaved parallel-unit calls stuck "running").
- **Validated live on real Windows through Agent OS itself.** Team build (5-task
  plan, 3 parallel coders in isolated patch workspaces → clean integration →
  reviewer wave → verification gate → `completed`, all transient states clear,
  overlays trimmed, project memory reconciled); PM delegation loop (judge →
  pending plan → confirm+recovery-budget → run); double-confirm race (→ [200,409],
  one run); cancel mid-run (→ clean `cancelled`, no stuck state); Git golden path
  (commit + push live; PR blocked by the PAT's scope, surfaced cleanly);
  connector contract previews (deploy/migration) + GENERAL rejection; crash
  recovery (kill mid-run → restart → swept to `failed`, all transient cleared);
  **secret-leak audit clean** (7 tracked secret values, 101 artifact/memory files,
  zero leaks).
- **Adversarial self-review caught + fixed 4 regressions in the hardening diff
  itself** (an 8-agent find→verify pass over the highest-risk changes, plus a
  live backend-restart during the E2E): the terminal-state sweep wiped a *settled*
  `browser_verification_state='passed'` (now clears only `'running'`); the
  `compileall -x` exclusion used unanchored substrings so `src/distribution/`
  matched `dist` and was silently un-checked (replaced with an exact-dir-name
  `py_compile` walk); the pending atomic-claim could strand a plan in
  `'dispatching'` on a crash mid-confirm (added a startup revert-to-`pending`
  reconciler); and a malformed `run_id` returned HTTP 500 instead of a clean 404
  (readers treat a guard-rejected id as absent). One flagged item (per-request
  timeout wired only to the Anthropic client, not the finite-timeout urllib path)
  was verified a non-issue.

## Phase 10 — Research / RAG / Skills (increment 1: discovery + skills + safe @search)

Made the agent system **discoverable**, gave agents a minimal **built-in
skills** foundation, and added a **safe, approval-first web-research channel**.
Additive — every invariant holds, and the explicit-command boundary now also
governs network access. (BLUEPRINT Pillar 4. Module/pipeline detail:
`ARCHITECTURE.md §5–§7.J`.)

- **Agent profile registry** (`agents_registry.py`, top-level leaf) — 10
  structured profiles (command/aliases, mode↔role linkage by string,
  introduction / use cases / responsibilities / tool categories, capability
  badges, approval boundary, skill index) behind `GET /api/agents`; consumed by
  both the composer autocomplete and the Agents browser, so agent descriptions
  live in one place. `roles.py` gains the `researcher` chat contract
  (`ROLE_FOR_MODE["research"]`); a sync test pins MODE_COMMANDS ↔ profiles ↔
  ROLE_FOR_MODE.
- **Composer `@` discovery** — typing `@` opens an upward autocomplete
  (`CommandMenu`, ModelPicker pattern): command + agent name + one-line
  description + capability badges (read-only / web / dispatches run / asks
  first); ↑/↓/Enter/Tab/Esc keyboard nav that only intercepts while the menu is
  open (Ctrl/Cmd+Enter send untouched).
- **Agents browser** — an "Agents" button atop the sidebar opens a two-pane
  modal (list + full contract detail) rendered from the registry; per-agent
  skills are readable and manually editable in place.
- **Skills foundation** — 18 committed markdown skills under
  `skills/{agent}/{skill}.md` (method / checklist / rubric / template — never
  executable tools); `skills_store.py` is the only read/write path
  (registry-validated pair before any path build, atomic, 20k cap) and folds a
  chat mode's skills into its guidance block (900/skill, 2000 total). The only
  writer is the user's Save in the UI — no autonomous skill generation
  (post-run "suggested skill patch" stays a planned follow-up).
- **Safe `@search` / `@research`** — one research mode, two spellings, added to
  `MODE_COMMANDS`; the explicit command is the per-turn **network grant**.
  `orchestrate()` becomes a combined inspect/research loop (independent budgets
  3 / 4, 16k research-char cap, forced-text tail) returning
  `(text, inspected_files, research_sources)`. `research.py` mirrors
  `inspect.py` (strict JSON protocol, `ok=False` results, never raises):
  `web_search` returns snippets only (Tavily adapter in `web_search.py`; key
  header-only via the new `search` credentials provider; Brave slot
  documented); `fetch_url` is SSRF-screened (scheme/port/userinfo guards,
  public-DNS enforcement incl. v4-mapped v6, hop-by-hop redirect re-screening),
  domain-allowlisted (user-pasted URLs bypass only the allowlist), tag-stripped,
  capped, and framed as untrusted evidence. A `redact()`-diff egress guard
  refuses any outbound query/URL carrying a credential-shaped value. GENERAL
  `@search` is allowed but skips memory intake (findings belong in project
  RESEARCH.md, which the ordinary intake judge already handles for projects).
- **Semantic proposal stays approval-first** — a judge-labeled `research`
  intent produces a `research_suggestion` ({command, query}) rendered as a
  "Run @search" chip that only pre-fills the composer; sending it is the grant.
  Executed research actions ride `ChatResponse.research_sources` and persist in
  message metadata (🔎 sources chip, reload-safe — the `inspected_files`
  pattern).
- **Not in this increment (deliberate):** MCP anything; autonomous skill
  generation/promotion; a separate research-cache artifact (RESEARCH.md + the
  persisted source metadata are the durable record); Brave adapter; local RAG
  indexes (repo/run-history retrieval); research runs via the Coding Agent.
- **Tests:** +87 across 6 new files (registry sync/capabilities 17, skills
  store/folding 12, research SSRF/allowlist/redirects/egress 31, web-search
  adapter 9, combined-loop orchestration 10, agents/skills/chat endpoints 8),
  plus mechanical updates (`test_roles`, `test_inspect`, `test_providers`
  3-tuple unpacks). `npm run build` green.
- **Adversarial review pass.** A multi-agent find→verify review over the
  increment (self-verified where the panel was interrupted) caught + fixed:
  the research fetcher could raise on a mid-read network error (broke the
  never-raises contract); an egress-refused URL persisted its secret-shaped
  value unredacted into `research_sources` (now redacted in `_fetch_fail`); the
  live chat turn dropped `research_sources`/`research_suggestion` from message
  metadata so chips only appeared after reload (now carried on the live turn);
  a failed `fetch_url` source rendered a model-controlled URL as an unvalidated
  `<a href>` (now http(s)-only); voice dictation left the `@`-menu on a stale
  caret; the composer offered mid-message `@`-commands the backend ignores (now
  start-of-message only); and the docs overstated the egress guard (reworded:
  it blocks credential-shaped values, not arbitrary memory/repo text).

## Phase 10.2 — Live research validation, skill patches, local RAG, memory upgrade

The second Phase 10 increment: validated the research channel live, added the
review-first self-improvement step, a minimal local retrieval layer, and
upgraded the project-memory scaffold. Additive; every invariant holds.
(BLUEPRINT Pillar 4 + 5.)

- **Live Tavily validation.** Drove real `@search` turns end-to-end through a
  running backend (real Claude + real Tavily on `backend/.env`): the `web_search`
  (Tavily) and `fetch_url` tools both returned real, cited, bounded results;
  `research_sources` rode the response and persisted in message metadata
  (reload-safe); a natural-language research request stayed approval-first (zero
  network, only a `@search` suggestion chip).
- **Suggested skill patch (review-first)** — `skill_patch.py`: after a GREEN
  run, a cheaply-gated best-effort judge (mirrors `recovery.assess_run`; skips
  trivial/read-only runs before any LLM call) may PROPOSE an **append-style**
  skill refinement onto `run.json` (`SkillPatchProposal`: target agent/skill,
  rationale, run evidence, before/after content). The Run Detail modal shows an
  Apply / Edit / Reject card; **Apply is the only write path** and routes
  through `skills_store.write_skill` (existing content never lost, registry-
  validated). No autonomous skill creation or global promotion.
- **Minimal local RAG** — `local_rag.py`: bounded, keyword-based (not vector)
  retrieval over project memory (scored `##` sections), recent run history
  (`run_store` summaries), and a sandbox-safe repo map (two-level walk via
  `inspect`, sensitive names filtered so `.env`/`.git`/`*.key` never surface).
  Per-source + per-turn char caps, never raises; exposed as the Main Agent's
  `retrieve` inspection tool and `POST /api/projects/{id}/retrieve`. No vector
  DB, no full-repo injection.
- **Project memory upgrade** — the former standalone `TASK_QUEUE.md` is now a
  `## Task Queue` section inside `STATUS.md` (`### Completed / ### In Progress /
  ### Next`); `memory_engine.migrate_task_queue_into_status` folds legacy files
  in on scaffold (non-destructive, idempotent, mapped subsections) and startup
  migration handles existing projects. New `LESSONS.md` (durable Main-Agent
  lessons) joins the writable + reconciliation sets and compaction (bounded
  tail); it's UI-visible/editable and never written by the Coding Agent. All
  intake/reconciliation/judge prompts, writable sets, the `/context` endpoint,
  `ContextPanel`, and the example templates updated in lock-step.
- **Tests:** +40 (`test_local_rag` 13, `test_skill_patch` 13, `test_memory_engine`
  +9 migration/LESSONS/append-fixes, plus MemoryContext/target-file updates across
  `test_memory_intake` / `test_intent_router`). Full backend sweep + `npm run
  build` green.
- **Adversarial review pass** (5-dimension find→verify, 3-vote panels) caught +
  fixed 5 real defects: migration silently dropped legacy items placed ABOVE the
  first heading before deleting the file (now preserved under "Other
  (migrated)"); `apply_update` append landed at EOF — i.e. inside the new
  trailing Task Queue board — for a named section (now **section-aware**: append
  lands inside the named `##` section, EOF fallback only when the section is
  absent); local RAG could surface the name/content of credential files the
  sandbox's narrow set misses — `.npmrc` / `id_rsa` / `credentials.json` / `*.p12`
  (retrieval now uses a **broader** credential filter than the write-guard set);
  retrieval hits weren't redacted (now every hit passes `credentials.redact`);
  and a skill-patch Apply wrote the propose-time snapshot, clobbering edits made
  since (Apply now **re-bases** the addition onto the live skill, and an explicit
  user edit still writes verbatim). The 3 findings that failed verification
  (0/3 votes) were correctly rejected.

## Phase 11 — Long-horizon Self-healing + Interactive Browser Verification (increment 1)

Upgraded recovery from "one generic repair after failure" into a **typed,
evidence-driven, auditable repair loop**, and browser verification from passive
screenshots into a **bounded, declarative interaction layer** — deliberately
NOT a computer-use platform. Additive; every invariant holds and the Phase 6.1
budget contract is preserved byte-for-byte. (BLUEPRINT Pillar 5. Module/
pipeline detail: `ARCHITECTURE.md §5 / §7.E / §7.E2 / §8 / §9`.)

- **Recovery Matrix foundation** (`recovery_matrix.py`, new leaf) — 8 frozen
  typed contracts (accepted evidence, verification method, max attempts,
  auto-eligibility, child-budget cap, confirmation boundary, audit note) + a
  deterministic first-match classifier (`classify_failure` → type, reason,
  `auto_ok`) + a bounded `redact()`-ed evidence builder (≤1800 chars).
  Environment failures (missing Playwright/Chromium, occupied port) classify
  `runtime` with `auto_ok=False` — a Coding Agent can't fix operator tooling,
  so they never burn a budget. `assess_run` computes the classification
  pre-LLM (strong prior; judge's type validated, `classified_by` audits which
  won; survives LLM failure) and appends the evidence block to run-action
  follow-up cards — **recovery children stopped being evidence-starved** on
  both dispatch paths.
- **Interactive browser verification** — the `## Browser Verification` block
  gains `### Views` (routes auto-discovery misses) + `### Flow: <name>`
  subsections (fixed vocabulary `goto/click/fill/submit/expect_text/screenshot`,
  ≤6 views / ≤2 flows / ≤10 steps, same-origin only; proven backward-compatible
  in both directions — the `##`-section regex never matches `###`, and
  HTML-comment examples are stripped). The one Playwright capture subprocess
  now collects **console errors / page errors / local-origin network failures**
  (bounded, `[page-label]`-prefixed, redacted in-parent), captures explicit
  views first (overall cap 8), and executes flows with per-step screenshots +
  a final-state capture that rides the existing gallery (`nav_kind="step"`).
  Credential-shaped fill values / sensitive input targets are **refused in the
  parent process** (never serialized to the browser; `value_masked` only; a
  refused flow is loud but never fails the run and is never auto-repaired). A
  failed **declared** flow fails the verification (→ `partial`); console
  errors alone never flip status — they are classification evidence and ride
  the visual judge's new bounded "Runtime signals" prompt block.
- **Bounded visual repair loop — via the existing recovery machinery, no new
  loop.** `_maybe_auto_recover` now also fires on a `completed` run whose
  visual verdict failed (closing a code-vs-constitution gap: CLAUDE.md already
  defined non-green as including a failed visual review), gated by the
  contract (`auto_eligible` + `auto_ok`) and clamped by it: a visual/runtime
  child inherits budget 0 — exactly one repair pass, whose own tail re-runs
  verification → browser → visual judge (before/after verdicts = parent vs
  child, linked by lineage). Confirm-only types (integration / deployment /
  database / docs_memory) never auto-dispatch; the generic non-green fallback
  (`product`) preserves the Phase 6.1 behavior exactly.
- **Manual recovery lineage fixed** — pending recovery plans persist
  `recovery_of` (nullable column, PRAGMA-migrated); the confirm endpoint
  threads `recovery_of` / `orchestration_round+1` / the parent's checkpoint,
  clamps the budget by the contract, 409s (releasing the claim) when the
  parent already has a recovery, and claims `recovered_by` under the per-run
  `mutate_run_json` lock + a `manual_recovery_dispatched` event. The UI
  browser-verify endpoint now also runs a best-effort `assess_run` on a failed
  outcome (previously "Run suggested fix" dead-ended 409 for UI-discovered
  failures).
- **Artifacts + UI** — run.json gains `flows` / `console_errors` /
  `network_failures` on the browser result and `recovery_type` /
  `classified_by` on the assessment (all defaulted; old records round-trip;
  legacy result.md renders byte-identical). RunChatCard: per-flow summary line,
  console/network counts, recovery-type chip. RunDetailModal: per-step list
  with errors + step-screenshot links, collapsible console/network evidence,
  type chip. No new polling, no timeline changes.
- **Validated live end-to-end on real Windows** (aegis-launch-control, real
  Claude + real Playwright/Chromium through a running backend). Happy path:
  the declared `### Views` route captured (`view-02.png`, `nav_kind="view"`),
  the `simulator-smoke` flow clicked the Simulator tab, asserted
  `SELECT SCENARIO`, and screenshotted per step + final state; a planted
  `console.error` probe AND the app's real backend-down failures (Express on
  3001 never started under `npm run dev`) were captured with page labels —
  and console noise correctly did NOT fail the passing verification (visual
  judge: passed). Failure path: a sabotaged `expect_text` failed the flow →
  verification `failed` → run `partial` → typed assessment whose judge used
  the console/network evidence to diagnose the REAL root cause (missing
  backend → `runtime`, `classified_by=judge`, deviating from the `visual`
  hint with justification) with the evidence block on the card. Manual
  recovery: propose → pending plan carried `recovery_of` → confirm with
  budget 2 → child dispatched with lineage + evidence-rich task card, budget
  **contract-clamped 2→0**, parent `recovered_by` claimed +
  `manual_recovery_dispatched` event; a second proposal 409'd; the child
  cancelled cleanly (no wedged state). Repair→re-verify: fixing the flow and
  re-verifying restored the run `partial → completed` with a fresh passing
  visual verdict (before/after). A mid-E2E port-in-use collision (the
  keep-alive preview) was itself correctly classified `runtime` as an
  environment failure. Backend restart: sweeps left zero wedged states and
  preserved the settled `browser_verification_state`. The showcase workspace
  was restored to its committed state afterwards.
- **Adversarial review pass** (6-dimension find→verify, 3-lens panels; run
  twice after session-limit losses) confirmed 1 real defect + surfaced 4
  verified-inline near-misses, all fixed: the capture subprocess truncated
  console/network/step-error text at 300 chars BEFORE the parent's exact-match
  redaction — a stored secret straddling the cut (e.g. a ~230-char Supabase
  `service_role` JWT in a failed local API URL) would leak partially into
  run.json/result.md/prompts/UI (child cap raised to 2000; the parent already
  redacts-then-truncates); the evidence builder had the same
  truncate-before-redact ordering per field (now redacts each field first);
  a bare `\bmigration\b` blocker regex misclassified ordinary app runs as
  confirm-only `database`, silently vetoing a user-approved budget (narrowed
  to supabase/db-push signatures); incidental console noise on a rendered app
  rerouted the generic Phase 6.1 fallback to a tighter contract (console rule
  now requires unconfirmed readiness); duplicate flow names could mask a
  failure via the by-name merge (parser dedupes); an all-None inherited
  checkpoint suppressed a manual recovery child's fresh rollback anchor
  (inherit only when the parent has one).

---

## UI Polish Pass (pre-Phase 12)

Frontend-only visual/UX overhaul; **no behavior change** (verified by build +
Playwright sweep of both themes + a behavior-preservation review pass).

- **Design system.** `App.css` rewritten around one token layer (colors, type,
  radii, shadows, motion) with a full light-theme token set — graphite surfaces,
  periwinkle-indigo working accent, crimson demoted to danger-only, and a
  tokenized status spectrum (ok/warn/run/blocked/cancel) that is readable in
  both themes. Signature: telemetry-style small-caps mono for every eyebrow /
  section label / status badge (`--font-mono`); humanist UI face for
  conversation. Themed scrollbars, `:focus-visible` rings, `prefers-reduced-
  motion` respected, `color-scheme` per theme. All pre-polish class names kept
  (selector diff clean); only the header `provider-select`/`theme-select` rules
  dropped with their elements.
- **Settings modal** (`SettingsModal.tsx`). The chat-header provider/theme
  dropdowns moved to a Settings modal opened from a pinned sidebar-footer
  button; adds a model select driven by the same App state as the composer
  picker. Identical handlers, options ("— no key" disabled entries), and
  persistence (theme in `agentos-theme`; provider/model still session-only).
- **Collapsible left sidebar.** Animated grid collapse (264px → 56px icon rail:
  expand / Agents / Global memory / Settings) with the component kept mounted so
  expand/selection state survives; preference persisted (`agentos-sidebar-
  collapsed`). Center column gains the freed width.
- **Component polish.** Inline SVG icon set (`icons.tsx`, no dependency);
  restyled buttons/badges/cards/modals (overlay blur + entrance animation);
  structured empty states; chat column width capped for readability; intent
  badges tinted per judged intent; `ChatPanel` still threads `selectedProvider`
  to run cards for verification calls.

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
  without a vision model). Phase 11: the block may declare views + bounded
  interaction flows (≤2×10 steps, same-origin, no credential input) — a failed
  declared flow fails the verification; console/network evidence is recorded,
  never status-flipping.
- **Recovery is typed and contract-bounded.** Every assessment carries a
  Recovery Matrix type; auto-recovery (still budget-gated, ≤2, idempotent)
  additionally requires an auto-eligible contract + `auto_ok` classification,
  and visual/runtime children get exactly one pass. Confirm-only types and
  environment failures never auto-dispatch.
- **3–4 LLM calls per non-`@code` turn** (delegation judge + optional inspection +
  chat response + memory judge).
- **Main agent never auto-reads repo contents** — only the bounded inspection loop
  (max 3 reads/turn).
- **Web access only on an explicit grant.** The research channel runs only on an
  explicit `@search`/`@research` turn (≤4 requests, 16k chars, allowlist +
  SSRF-screened; user-pasted URLs bypass only the allowlist). Search needs a
  Tavily key under the `search` connector; URL fetch works keyless.
- **Single-user, single-process.** No auth, no shared deploy.

---

## Test Coverage

Backend tests live under `backend/tests/`, stub `llm.chat` (no API key needed),
and are each runnable standalone (`python tests/<file>.py`). **821 total**
(Phase 11 added 64: recovery matrix 20, browser interactions 22, manual
recovery lineage 5, recovery-budget matrix gating +6, recovery classification/
evidence +6, pending recovery_of round-trip/migration +2, visual-judge runtime
signals +2, UI browser-verify assess hook +1; Phase 10.2 added 40: local RAG 13, skill patch 13, memory-engine migration/
LESSONS/append +9, plus adversarial-review regressions; Phase 10 added 87 across
the agent registry, skills store, research channel,
web-search adapter, combined orchestration loop, and agents/skills endpoints —
including a post-implementation adversarial-review pass that hardened the
research fetcher's never-raises contract, egress-URL redaction, v4-mapped-IPv6
SSRF, and budget-exhaustion coverage; Phase 9.1 hardening added ~29 regressions
across sandbox device-name/dot guards,
sequential-path role enforcement, terminal transient-state sweeps, cancel/deploy
run.json-lock races, pending double-confirm claim, integration case-collision,
compileall skip-dirs, browser teardown/port-guard, memory-engine matching,
attachment XSS, provider timeout + Gemini-key redaction, and team-run PM context);
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
| `test_memory_engine` | 24 | apply_update policy/atomic/dedup + section-aware append; scaffold; write-path; TASK_QUEUE→STATUS migration (+ preamble preservation) + LESSONS.md |
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
| `test_roles` | 11 | role registry contracts, tool sets, mode↔role map (incl. research), coder fallback |
| `test_agents_registry` | 17 | profile shape/uniqueness, MODE_COMMANDS↔profiles↔ROLE_FOR_MODE sync, capability honesty, command entries |
| `test_skills_store` | 12 | registry-gated read/write, slug guard, caps, prompt folding, registry↔disk drift |
| `test_research` | 31 | protocol parse, SSRF matrix (incl. v4-mapped IPv6), allowlist + user-URL bypass, redirect re-screening + no-Location, caps, extraction, mid-read never-raises, secret egress + refused-URL redaction |
| `test_web_search` | 9 | Tavily request shape (key header-only), parsing, redacted errors, config gates |
| `test_research_orchestration` | 10 | combined loop: budgets, char cap, forced-text tail, inspect-over-budget, GENERAL research-only, ungranted turns, user URLs |
| `test_agents_endpoints` | 8 | /api/agents shape, skill read/write routes, @search chat flow (GENERAL no-memory, metadata persistence, suggestion-only semantic path) |
| `test_local_rag` | 13 | memory/run/repo retrieval, scoring, caps, kinds filter, broad credential-name filtering, hit redaction, inspect tool + /retrieve endpoint |
| `test_skill_patch` | 13 | green-only gating, append-style proposal, unknown-target rejection, idempotency, never-raises, apply-via-skills_store (+ edit, + rebase), reject, endpoints |
| `test_recovery_matrix` | 20 | contract registry frozen/complete, classifier ladder + priority + `auto_ok` env guard + narrow DB regex + readiness-gated console rule, evidence bounds/redact-before-truncate |
| `test_browser_interactions` | 22 | Views/Flow grammar + caps + duplicate-name dedupe + legacy identity + fence-less/HTML-comment edges, credential fill refusal (no echo), flow-failure⇒failed, refused/skipped flows, evidence redaction, dict-manifest parsing, script syntax, render byte-identity |
| `test_manual_recovery_lineage` | 5 | confirm threads recovery_of/round/checkpoint + contract clamp + `recovered_by` claim + event; 409 releases claim; no-checkpoint/plain/missing-parent degrade |
| `test_patch_workspace` | 13 | overlay read/write/append/list/search, `.git` guard, unsafe-id containment, blocked executors |
| `test_team_planner` | 18 | role/parallel parsing, wave layering, cycle handling, eligibility gate, id sanitization |
| `test_integration` | 7 | clean apply, dedupe, first-writer-wins conflict, sandbox re-validation, failed-task output |
| `test_team_runner` | 9 | end-to-end team path: parallel-overlap proof, isolation+integration, conflicts/apply-errors→partial, read-only bounce, findings flow, cancel mid-wave, sequential gate, one-unit-crash survival |

---

## Recommended Next Steps

**Next up (Phase 10 increment 3).**
- **Skill-patch surfacing in chat** — echo a proposed skill patch as a chat
  affordance (today it lives in the Run Detail modal), and allow a patch to
  target a NEW skill (registry-add behind explicit review), extending the
  self-improvement ladder.
- **RAG ranking + run-diff retrieval** — add recent-diff snippets and light
  symbol indexing to `local_rag`; better scoring than substring counts.
- **Brave adapter + allowlist config** — second search engine behind the same
  seam; user-extensible fetch allowlist (per-project or global file).

**Also queued.**
- **Phase 11 increment 2** — deployment/database repair automation behind their
  existing explicit contracts (typed classification already lands); failure-
  pattern memory (LESSONS.md ← repeated recovery types); run-health view over
  recovery lineage chains.

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
