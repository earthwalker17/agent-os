# Agent OS — Architecture

The current shape of the system: what each file does, how the pieces fit, and
the invariants that must not break. This doc describes **what the system is
now** — the phase-by-phase history of how it got here lives in `ROADMAP.md`
(don't duplicate it here). Pair with `CLAUDE.md` (rules).

---

## 1. What it is

A **local-first project cockpit** — a single web chat surface for running many
long-term projects. It combines project-scoped conversations, structured
markdown memory, an orchestration layer, and a sandboxed execution layer that
hands work to a **Coding Agent** inside a per-project workspace.

Two agents, clean separation:
- **Main agent = brain.** Planner / memory steward / orchestrator. Talks to the
  user, loads memory, decides delegation. **Never edits `repo/` or runs shell.**
- **Coding Agent = hands.** Bounded executor inside one project's
  `execution_workspaces/{id}/repo/`. Edits code via tools. **Never edits memory
  or other projects.**

## 2. Core principles

- **Local-first.** Filesystem + SQLite + FastAPI + React. No cloud infra, no
  queues. ThreadPoolExecutor over Celery, SQLite over Postgres, polling over SSE
  — until there's a concrete reason to swap.
- **Project isolation.** Each project owns its conversations, memory, and
  workspace. Crossings are deliberate and bounded.
- **Structured markdown memory.** Durable state lives in readable `.md` files.
- **One sandbox chokepoint.** Every repo path + shell command routes through
  `ProjectSandbox` → `ToolRuntime`. No raw `os`/`pathlib`/`subprocess` on repo
  paths elsewhere.
- **No auto-injection of repo contents** into the main agent's context — bounded
  on-demand reads only.
- **Explicit execution only.** Inferred coding intent never auto-runs; a human
  click (or `@code`) is always required.

## 3. High-level shape

```
┌────────────┐    ┌────────────────┐    ┌────────────────┐
│  Frontend  │ ←→ │  FastAPI       │ ←→ │  Anthropic API │
│  (React)   │/api│  (main.py)     │    │  (Claude)      │
└────────────┘    └───────┬────────┘    └────────────────┘
        ┌─────────────────┼──────────────────────────┐
        ▼                 ▼                          ▼
   memory/          projects/{id}/         execution_workspaces/{id}/
   (global .md)     (project .md)          repo/ · runs/ · patches/ · logs/
        └── orchestrator ─┘                AGENT.md · TASK.md
```

Frontend dev (Vite, 5173) proxies `/api` → backend (8000). Verified preview apps
use **5174** to avoid colliding with Agent OS itself.

## 4. Directory layout

```
Agent OS/
├─ CLAUDE.md / ROADMAP.md / ARCHITECTURE.md / BLUEPRINT.md / README.md   ← docs
├─ memory/                      global: SOUL.md (read-only anchor), USER/WORKSTYLE/MEMORY.md
├─ skills/{agent_id}/{skill}.md built-in skill markdown (committed; user-editable + suggested-patch-applied via UI only)
├─ projects/{id}/               per-project: PROJECT/STATUS(+`## Task Queue`)/DECISIONS/RESEARCH/LESSONS.md (+ OPS.md ledger)
├─ execution_workspaces/{id}/
│  ├─ repo/                     working code tree (Coding Agent's sandbox); repo/uploads/ for chat files
│  ├─ runs/{run_id}/            task_card.md · events.jsonl · run.json · plan.json · result.md
│  │                            · integration.json · diff.patch · deployment.json · deploy.log
│  │                            · visual_review.json · screenshots/
│  ├─ patches/{run_id}/{task}/  isolated patch workspace: workspace/ overlay + manifest.json
│  └─ logs/  AGENT.md  TASK.md
├─ chat_uploads/{conversation}/ chat-only attachments (gitignored)
├─ backend/                     FastAPI + execution layer (Python)
└─ frontend/                    React + TypeScript (Vite)
```

`SOUL.md` is committed; other global/project files are gitignored (only
`*.example.md` explainers are public). `agent_os.db` (SQLite, WAL) holds
conversations, messages, `pending_executions`.

---

## 5. Backend modules

### Top level (`backend/`)
| File | Purpose |
|------|---------|
| `main.py` | FastAPI app + every HTTP endpoint (projects, conversations, chat, memory, execution, inspection, verification, preview, run control, Git/deploy/migration contracts). Startup: `sweep_stuck_runs` + `reconcile_stuck_external_actions` + memory-scaffold migration. |
| `orchestrator.py` | The chat brain. Loads SOUL + memory, **compacts** project memory (`_compact_memory`: STATUS/PROJECT/SOUL whole, append-growth files tail-trimmed over a threshold), assembles context (+ optional `@`-mode/intent block with the mode's skills folded in), produces the reply, runs the bounded inspection/research loop (independent budgets; research only on an explicit grant), then the structured memory-intake judge (`judge_memory_intake → MemoryDecision`). Imports no connector/executor (asserted by a test). |
| `agents_registry.py` | Leaf module (pydantic only): the agent profile registry behind `GET /api/agents` — 10 frozen `AgentProfile`s (command/aliases, mode↔role linkage BY STRING, introduction/use-cases/responsibilities, `AgentCapabilities` badges, approval boundary, `SkillRef` index). Presentation + contract, not permissions; sync with `MODE_COMMANDS`/`ROLE_FOR_MODE` is test-asserted. |
| `skills_store.py` | Leaf module: the ONLY read/write path for `skills/{agent}/{skill}.md` (registry-validated pair before any path build, slug re-check, atomic write, 20k cap; writes come only from the user's Save in the UI) + `skills_prompt_block(mode)` — the bounded (900/skill, 2000 total) fold-in for chat-mode guidance. Never raises into a turn. |
| `memory_engine.py` | Stdlib-only leaf: the single markdown write path `apply_update(base_dir, allow=…)` (policy-filtered, atomic temp+`os.replace`, append-deduped, robust section replace), writable-file sets, `MemoryDecision`, idempotent `ensure_memory_scaffold`. Phase 10.2 — the task board is a `## Task Queue` section inside STATUS.md (was standalone TASK_QUEUE.md; `migrate_task_queue_into_status` folds legacy files in on scaffold, non-destructive + idempotent); `LESSONS.md` added to the writable/reconciliation sets. `OPS.md` is scaffolded but excluded from every writable set (only `ops_ledger` writes it). |
| `llm.py` | Thin entry point: `chat(system, messages, model?, provider?) -> str` + `chat_vision(…)`. Delegates to `providers.py`; shared transient-retry. |
| `providers.py` | Capability-aware model registry — Claude / GPT / Gemini / DeepSeek / Kimi / GLM. Key-presence availability (+ env aliases), Claude-first default, per-model `vision` flags, env-overridable default model/base-URL, `is_known_model`, `complete()` + vision-gated `complete_vision()`. Anthropic via SDK; others via OpenAI-compatible/`generateContent` `urllib` (no new deps). |
| `database.py` | SQLite persistence: conversations, messages, `pending_executions`. |
| `uploads.py` | Chat attachment storage: sanitize + allow-list + dedup, chat-only under `chat_uploads/{conv}/`, optional workspace copy via `ProjectSandbox`. HTTP-agnostic. |
| `credentials.py` | The ONLY reader of secret values. Provider registry (`github\|vercel\|supabase\|stripe`), typed secret/non-secret fields: `get_token`/`get_secret`/`get_metadata`/`status`/`set_credential`/`delete_credential` + `get_env_value` (sole app-env value reader). Stripe live-gate at the store boundary. `redact()` covers `sk_`/`rk_`/`whsec_`/`sbp_` + Postgres conn-string passwords + every stored token (`service_role` by exact value, not shape — the anon JWT is public). Store under `credentials/` (gitignored). |

### Execution layer (`backend/execution/`)
| File | Purpose |
|------|---------|
| `manager.py` | Workspace filesystem layout + idempotent init; `read/update_task_state`. |
| `models.py` | Pydantic: `RunRecord`, `RunStatus`, `TaskSpec`, `ResultSummary`, `VerificationResult`, `BrowserVerificationResult`, `VisualReviewResult`, `RecoveryAssessment`, `ExecutionPlan`/`ExecutionTask`/`TaskStatus`, `IntegrationResult`/`IntegrationConflict`. See §8. |
| `templates.py` | Default `AGENT.md` / `TASK.md` seeds (incl. verification-block docs). |
| `sandbox.py` | `ProjectSandbox` — **the boundary**. `resolve_under(root, rel)` validates a path (absolute/`..`/sensitive-name/`.git`/escape rejection) against any root; `resolve_repo_path` delegates. `validate_command`, `validate_git`, `validate_supabase` (allow-list + destructive-by-subcommand gating). |
| `tool_models.py` | `ToolResult` + per-tool request models. |
| `tool_runtime.py` | `ToolRuntime` — the six sandboxed file/shell tools with output caps, plus typed executors `run_git` and `run_supabase` (`shell=False` argv; NOT agent tools; `run_supabase` gets a scrubbed allow-listed env so Agent OS keys never reach the third-party CLI). |
| `patch_workspace.py` | Isolated patch workspaces (team runs): `PatchToolRuntime` — reads fall through overlay→repo, writes land in `patches/{run}/{task}/workspace/`, merged list/search, `append_file` seeds from base, `run_shell`/`run_git`/`run_supabase` blocked. All paths via `sandbox.resolve_under`. Per-task `manifest.json` audit artifact; `_safe_segment` guards the task-id path component. |
| `prompts.py` | Coding Agent system prompt + per-step/correction/repair/continuation/plan/task-unit prompts. `build_system_prompt` takes an optional role overlay; `build_task_unit_user_prompt` an optional role note (empty = byte-identical to legacy). |
| `planner.py` | Pure planning: `looks_complex` gate, tolerant `parse_plan`/`plan_from_dict` (with role/`parallel` parsing + task-id sanitization), `fallback_plan`; graph helpers `topological_order`, `dependency_failed`, `aggregate_run_status`; team helpers `compute_waves` (Kahn layering, cycle remainder forced sequential), `task_parallel_eligible`, `plan_is_team_eligible`. `MAX_PARALLEL_AGENTS = 3`. |
| `roles.py` | Agent role registry: `AgentRole` = prompt overlay + enforced `allowed_tools` + read-only flag. Execution roles `coder`/`reviewer`/`inspector`; system stages `integrator`/`verifier` (trace labels); chat `@`-mode ↔ role map. Pure data; unknown role → coder. |
| `integration.py` | Deterministic per-wave merge (team runs): overlays apply into the shared repo through the base `ToolRuntime` (sandbox re-validates), plan order; identical content de-dupes, conflicting content → first-writer-wins + `IntegrationConflict` + blocker; apply errors surfaced. No LLM. |
| `runner.py` | `CodingAgentRunner`: phased run (plan → execute → finalize) with verification + iterative repair, cooperative cancel (`cancel_event` at step boundaries → `_finalize_cancelled`), progressive live metrics. **Team path** `_run_execution_phase_team` (gated by `plan_is_team_eligible`): wave scheduling, `_run_parallel_batch` on a dedicated per-batch pool (never the shared manager pool), per-unit `_UnitContext` (own runtime + observed lists + enforced tools), per-wave `integrate_wave`, conflict/error-capped aggregate; coordinator is the sole run.json/plan.json writer. |
| `background.py` | `BackgroundRunManager`: thread-pool dispatch; crash → `failed` (clears every transient sub-status); per-run cancel-`Event` registry (`request_cancel`); `dispatch(…, retry_of/recovery_of/recovery_budget/orchestration_round)`; `submit` (off-thread finalize). `_maybe_auto_recover` (clean-finalize only) auto-dispatches ONE linked recovery run when a user-approved budget remains. Phase 11 — also fires on a `completed` run whose visual verdict failed; enforces the Recovery Matrix contract (`auto_eligible` + classification `auto_ok`; confirm-only types and environment failures never auto-dispatch) and clamps the child's onward budget by the contract cap (visual/runtime → 0: one repair pass, one natural re-verify). |
| `chat_delegation.py` | `@code` trigger + deterministic mode `@`-commands (`parse_mode_command`: `@plan`/`@design`/`@debug`/`@review`/`@inspect`/`@memory`/`@search`/`@research` — shape the response, never dispatch; the explicit `@search`/`@research` additionally grants the research channel for that turn). |
| `delegation_intent.py` / `delegation_judge.py` | Heuristic fallback detector; LLM semantic delegation judge (primary — 3 decisions + a richer informational `intent` label). |
| `pending_execution.py` | Confirmable execution plans (store / render / revise). Phase 11 — a recovery proposal carries `recovery_of` (nullable SQLite column, PRAGMA-migrated) so the confirm endpoint threads full lineage. |
| `memory_reconciliation.py` | Post-run bounded reconciliation judge (writes STATUS/TASK_QUEUE/DECISIONS/RESEARCH via `memory_engine`; skip rules; once-only). |
| `recovery.py` | Best-effort `assess_run`: interprets a non-green terminal run, recommends one bounded next step (`RecoveryAssessment`). Phase 11 — computes the deterministic `recovery_matrix` classification pre-LLM (rides the prompt as a strong prior; validates/falls back the judge's `recovery_type`, audited via `classified_by`), and appends the bounded redacted evidence block to a run-action `follow_up_task_card` so recovery children are no longer evidence-starved. Confirmable handoff only — never auto-dispatches. |
| `recovery_matrix.py` | Phase 11 leaf module (models + credentials only; no LLM/IO): the typed Recovery Matrix. Frozen `RECOVERY_CONTRACTS` (8 types — build/runtime/visual auto-eligible under budget with per-type child-budget caps; integration/deployment/database/product*/docs_memory confirm-only; *product's non-green fallback stays auto-eligible to preserve the Phase 6.1 budget contract), deterministic first-match `classify_failure(record) -> (type, reason, auto_ok)` (`auto_ok=False` marks environment failures — missing Playwright/Chromium, occupied port — that a Coding Agent can't fix), and `build_recovery_evidence` (≤1800 chars, every line through `credentials.redact`). |
| `inspect.py` | Main-agent on-demand, read-only file inspection (bounded, max 3/turn). Phase 10.2 — also dispatches the `retrieve` local-RAG tool. |
| `local_rag.py` | Minimal, keyword-based (not vector) local retrieval (Phase 10.2): bounded, cited evidence from project memory (scored `##` sections), recent run history (`run_store` summaries), and a sandbox-safe repo map (two-level walk via `inspect`, sensitive names filtered). Per-source + per-turn char caps; never raises. Exposed as the `retrieve` inspection tool + `POST …/retrieve`. |
| `skill_patch.py` | Review-first suggested skill patch (Phase 10.2): a best-effort, green-run-only, cheaply-gated judge (mirrors `recovery.assess_run`) that PROPOSES an append-style skill refinement (`SkillPatchProposal` on run.json) — target agent/skill, rationale, run evidence, before/after content. `apply_skill_patch` writes ONLY through `skills_store.write_skill` on explicit user action (Apply, optionally edited); `reject_skill_patch` writes nothing. Never creates skills or promotes globally. |
| `research.py` | The bounded research channel (Phase 10, mirrors inspect.py): strict `{"research_request": …}` protocol, two tools (`web_search` snippets-only, `fetch_url` bounded tag-stripped extract), SSRF screening (scheme/port/userinfo, public-DNS enforcement incl. v4-mapped v6, hop-by-hop redirect re-screening), `DEFAULT_ALLOWED_DOMAINS` allowlist (user-pasted URLs bypass only that), `redact()`-diff egress guard on every outbound query/URL, per-fetch 6k + per-turn 16k char caps, untrusted-content framing. Never raises into the loop. |
| `web_search.py` | Search-engine adapter (Tavily v1; Brave slot documented): `search_web → [{title,url,snippet}]`, key from `credentials.get_token("search")` in a header ONLY, errors redacted, injectable opener. No key → clear config error (URL fetch stays keyless). |
| `verification.py` | Command verification: `plan_verification` (manual `## Verification` block else inferred), `run_verification_specs`, render. |
| `browser_verification.py` | Browser lifecycle: dev server → HTTP readiness → render-readiness-gated multi-page Playwright capture (`BrowserPageCapture[]`) → teardown; legacy single-capture adapter keeps `screenshots/browser.png`. Phase 11 — the `## Browser Verification` block gains `### Views` / `### Flow:` subsections (fixed vocabulary `goto/click/fill/submit/expect_text/screenshot`; caps 6 views / 2 flows / 10 steps; HTML-comment examples inert); the capture subprocess collects console errors / page errors / local-origin network failures (bounded, redacted), captures explicit views first (overall cap 8), and executes flows with per-step screenshots + a final-state capture (`nav_kind="step"` rides the existing gallery). `CaptureOutcome` normalizes legacy list-returning capturers. `_screen_flows` refuses credential-shaped fill values / sensitive targets in-parent (never serialized). A failed **declared** flow fails the verification; console errors and policy refusals never do. |
| `visual_judge.py` | AI visual judgment over screenshots (`run_visual_review` → verdict + rationale, `visual_review.json`). Resolves a vision-capable model (selected, else any available), skips gracefully otherwise. Diagnostic-only. Phase 11 — the prompt folds in a bounded "Runtime signals" text block (console/network/flow-step evidence, ≤1200 chars) as supporting signal. |
| `preview.py` | Managed long-lived preview dev servers (one per project). |
| `git_ops.py` | Sandboxed local Git via `run_git`: `ensure_repo`, `create_checkpoint` (out-of-branch tagged snapshot), `capture_diff` (redacted + bounded), secret-refusing `commit`, `create_branch`, gated `rollback`. Never raises into the run loop. |
| `github_connector.py` / `_git_askpass.sh` | GitHub via REST/`urllib` (no `gh` CLI): `status`, tokenless `ensure_remote`, `push_branch`, `create_pull_request` — token injected ONLY via the generated `GIT_ASKPASS` env, never argv/`.git/config`. Output redacted. |
| `app_env.py` | Project app-env registry (`credentials/env/{id}.json`): the BUILT app's env vars, presence-only (value reader is `credentials.get_env_value`); secret values register into the redactor. |
| `vercel_connector.py` | Vercel REST-over-`urllib`: `status`, `create_deployment` (gitSource), `get/list_deployments`, `promote_deployment` (rollback), `set_env_var`. Token in the `Authorization` header only; every returned string redacted; `normalize_url` strips a protection-bypass query. |
| `supabase_connector.py` | Supabase: Management REST (`status`) + sandboxed CLI (`link` / migration preview = `db push --dry-run` / apply = `db push --linked`, gated). Secrets ride `run_supabase` `env_extra` only; CLI stdout/stderr redacted before any artifact; Docker-missing → clear blocker. |
| `stripe_connector.py` | Stripe test-mode: form-encoded REST + `Idempotency-Key`, per-request test-gate. `provision_price`, `register_webhook` (returned `whsec_` stored, never echoed), `delete_webhook`, `status`, `local_webhook_command`. |
| `ops_ledger.py` | The ONE writer of `projects/{id}/OPS.md`: deterministic, ids/URLs/key-NAMES only, redacted at the call site, idempotent on `dedup_key`. |

`tests/` mirrors these per feature; all stub `llm.chat` (no API key needed).

## 6. Frontend modules (`frontend/src/`)

| File | Purpose |
|------|---------|
| `main.tsx` / `App.tsx` | Entry + three-column layout + all top-level state. Theme via `data-theme` on `<html>`, persisted to `localStorage`. |
| `types.ts` | Shared TS types mirroring backend models. |
| `components/ProjectList.tsx` | Left column: Agents button (top) + projects + conversations + CRUD. |
| `components/AgentsModal.tsx` | Agents browser: two-pane modal over `GET /api/agents` — agent list with command/capability chips, full contract detail (introduction, use cases, responsibilities, tools, approval boundary), per-agent skills readable/editable in place (Save → the skill-update endpoint). |
| `components/CommandMenu.tsx` | Composer `@`-command autocomplete: upward popover (ModelPicker pattern) with command + name + one-line description + capability badges; rows derived from the agent registry (`buildCommandEntries`, aliases expanded). |
| `components/ChatPanel.tsx` | Center: header (provider + theme selectors) + message thread + multi-modal composer (auto-grow textarea, `Ctrl/Cmd+Enter`, file upload with chips + "add to workspace", voice, `@`-command autocomplete with caret-token detection + ↑/↓/Enter/Tab/Esc). Renders `RunChatCard` on `run_id` messages, research-sources 🔎 chip + "Run @search" suggestion chip from message metadata; `ModelPicker` above the composer gates image upload by the selected model's `vision` flag. |
| `components/ModelPicker.tsx` | Compact per-provider model dropdown with vision/text tags. |
| `components/ContextPanel.tsx` | Right column: editable project memory files + `RunsSection` + env registry. |
| `components/RunsSection.tsx` | Runs list (auto-polls while active; live files/cmds counts) + Start/Stop preview + per-row Trace. |
| `components/RunChatCard.tsx` | In-chat run lifecycle: live phase badge + wave-grouped task checklist with role chips → build/verify phases → completion summary → browser verification (preview URL + multi-page screenshot gallery + AI visual verdict) → integration summary → Git/deploy/migration/Stripe panels; Cancel / Retry / Live trace. |
| `components/RunDetailModal.tsx` | Detailed inspection: per-command verification, browser + visual review, Plan & Tasks (per-task role/wave/workspace), Team Integration section, settled Timeline, Recovery, memory reconciliation, `result.md`. |
| `components/RunTimeline.tsx` | Settled-milestone timeline: collapses each step's start/settle pair into one row (plan / task / verification / repair / browser / wave / integration); settles dangling "running" once terminal. |
| `components/RunTrace.tsx` | Live Trace modal: granular chronological thread, `tool_call`+`tool_result` paired (interleaving-safe for parallel units via `task_id`), raw `llm_response` dropped. Polls `…/events?since=`. |
| `components/runEventUtils.ts` | Shared event-render helpers (`kindFor`, `str`, `clockTime`). |
| `components/EditModal / ConfirmDialog / GlobalMemoryModal` | Memory editing, confirmations, global-memory viewer. |
| `components/GitOpsPanel / DeployOpsPanel / MigrationPanel / StripePanel / ExternalActionPanel` | Two-phase contract drivers (preview→confirm) in the run card / detail modal for commit/push/PR/rollback, deploy/redeploy/rollback, Supabase link/migrate, Stripe provision/webhook. |
| `components/ConnectorsModal / ConnectorModal / EnvRegistryPanel` | Connector setup (write-only, presence-only display, Stripe TEST badge) + app-env registry with per-key "Push to Vercel". |

---

## 7. Key pipelines

### A. Chat turn (non-`@code`) — `orchestrator.orchestrate()`
1. Load SOUL + global + project memory; assemble context (+ optional `@`-mode
   block — with the mode's built-in skills folded in, capped — that shapes the
   response without dispatching).
2. **Intent router.** An explicit mode `@`-command sets the mode + skips the
   judge. Else the delegation judge classifies (`dispatch_suggested` /
   `discussion` / `memory_only`) + emits an informational `intent`; a
   `dispatch_suggested` creates a pending plan (no run); on failure → heuristic
   fallback. A non-dispatch intent also routes to the matching `orchestrate(mode=…)`.
3. **Main response** LLM call. May emit `{"inspect_request": …}` to read a repo
   file via `inspect.py` (max 3/turn), each result fed back before the next.
   On an explicit `@search`/`@research` turn the same loop also accepts
   `{"research_request": …}` (§J) with its own independent budget.
4. **Memory-intake judge** proposes updates; the backend policy-filters (SOUL +
   non-writable excluded) and writes atomically via `memory_engine`. The
   decision `reason` + turn `intent` ride on the message metadata (UI chip/badge).
   A GENERAL research turn skips this step entirely (findings belong in a
   project's RESEARCH.md, not global memory).

→ 3–4 LLM calls/turn. The request carries a `provider`/`model` (validated,
400 on unknown); the main response routes to it, internal subsystem calls use
the default provider + model.

### B. Delegation → run
`@code` → `chat_delegation` → `BackgroundRunManager.dispatch()`. Inferred intent
→ pending plan → user clicks **OK** → `…/execution/pending/{id}/confirm` → same
`dispatch()`. **No other path runs.**

### C. Coding Agent run — `CodingAgentRunner.run_task()`
1. Init run dir + `run.json` (`running`); load `AGENT.md`/`TASK.md`.
2. **Plan phase.** `looks_complex` gates: simple cards skip the LLM (single-task
   plan); complex cards run a bounded read-only inspection loop
   (`MAX_PLAN_STEPS=6`, enforced) ending in a `plan` action → `ExecutionPlan`.
   Any failure → single-task fallback. Persisted to `plan.json`.
3. **Execution phase** (three routes, most conservative first):
   - *Single-task* — the original bounded loop verbatim (`MAX_STEPS=24`), passing
     the agent's `final` straight through.
   - *Sequential multi-task* — each task in topological order
     (`MAX_TASK_STEPS=20`), skip dependents of failed deps, mutate per-task
     state in place, aggregate a run status.
   - *Team* (only when `plan_is_team_eligible` — an LLM-planned plan with a wave
     of ≥2 parallel-eligible tasks): the graph layers into topological **waves**;
     within a wave, parallel-eligible units run concurrently on a dedicated
     bounded pool (≤3) — coders in isolated **patch workspaces** (`run_shell`
     blocked), read-only reviewer/inspector against the shared repo with tools
     enforced in-loop. The coordinator settles units as they finish (sole
     run.json/plan.json writer; workers append `task_id`-tagged events), then
     **integrates** the wave's overlays (`integrate_wave`; first-writer-wins,
     conflicts + apply-errors surfaced and capped at `partial`), then runs the
     wave's remaining tasks sequentially. Read-only findings flow into
     later-wave prompts.
4. **Finalize:** status/summary/lists; update `TASK.md` (snapshot first).
5–8. **Best-effort tail** (never fails finalization): command verification +
   repair (§D) — over the *integrated* tree for team runs, so `completed` means
   the integrated result passed; optional browser verification (§E); memory
   reconciliation; recovery assessment.
9. Write `run.json`/`plan.json`/`result.md`; return `ResultSummary`.

Events carry a `phase` tag + plan/task/wave/integration events.

### D. Command verification + repair — `verification.py` + `runner._verify_with_repair`
1. `plan_verification`: manual `## Verification` block wins; else infer
   (`npm install`+`build`; `pytest` iff importable, else `compileall`); else skip.
2. Run specs in order, **stop at first failure**.
3. A failed `completed`/`partial` run that produced files → bounded iterative
   repair loop (`MAX_REPAIR_ATTEMPTS`): pre-read the erroring files, re-edit
   (`run_shell` blocked), re-verify; stop when green, a pass changes nothing, or
   the cap hits.
4. Still failing → a `completed` run downgrades to `partial` + a
   `verification failed:` blocker.

### E. Browser preview + visual review — `browser_verification` + `visual_judge` + `preview`
**Run browser verification** (`POST …/browser-verify`): `npm install` → dev
server (5174) → poll URL → render-readiness-gated multi-page Playwright capture
(entry page + explicit `### Views` first + a few discovered views; a readiness
timeout captures anyway, marked `unconfirmed`) → **declared interaction flows**
(Phase 11: bounded `### Flow:` steps executed in the same capture subprocess —
click/fill/submit/expect_text with per-step screenshots; console/pageerror/
local-network evidence collected throughout, redacted in-parent). On pass the
server is handed to `preview.py` (Start/Stop from the Runs panel); a
flow-failed verification is `failed` (no handoff) and, from the UI endpoint,
gets a best-effort typed `assess_run`. Same request: `run_visual_review` sends
the screenshots + task context + runtime signals to a vision model → structured
verdict (diagnostic-only; skips without a vision model). Screenshots served via
`…/screenshot?name=`.

### E2. Recovery Matrix — typed, evidence-driven repair (Phase 11)
Every assessment is typed: `recovery_matrix.classify_failure` runs
deterministically before the `assess_run` LLM call (strong prior; validated
fallback; `classified_by` audits which won), and a run-action
`follow_up_task_card` carries a bounded redacted evidence block (screenshot
artifact names, failing route, visual verdict, console/network errors, failed
flow steps, verification tail) so the recovery child sees the concrete failure.
Repair reuses the EXISTING recovery-run machinery — no new loop: the child's
own tail re-runs verification → browser → visual judge (before/after = parent
vs child `visual_review`, linked by `recovery_of`/`recovered_by`). The
auto-path (`_maybe_auto_recover`) additionally fires on a `completed` run whose
visual verdict failed, but only for contract-`auto_eligible` types
(build/runtime/visual + the non-green product fallback) with classification
`auto_ok` (environment failures — missing Playwright, occupied port — never
auto-repair), and clamps the child's onward budget by the contract cap
(visual/runtime → 0 = exactly one repair pass). The manual path
(propose-recovery → pending row with `recovery_of` → confirm) threads the same
lineage: `recovery_of`, `orchestration_round+1`, inherited checkpoint, contract-
clamped budget, parent `recovered_by` claim + `manual_recovery_dispatched`
event; a parent that already has a recovery 409s.

### F. Chat attachment upload — `uploads.py`
Composer sends files to `POST /api/chat/upload` before the chat send; each is
sanitized + de-duped chat-only, and (if "add to workspace") copied into
`repo/uploads/` via the sandbox. Metadata rides the `/api/chat` body so chips
re-hydrate on reload.

### G. Run control — timeline + cancel + retry
`GET …/runs/{id}/events` (optional `since` cursor) feeds the settled
`RunTimeline` and the live `RunTrace` (raw `llm_response` dropped). The chat card
derives a phase badge + task checklist from `run.json`; progressive metrics make
counts climb live. **Cancel** (only `running`): sets `cancel_requested`, signals
the runner via a per-run `Event`; cooperative at step boundaries →
`_finalize_cancelled`; an orphaned run is finalized by the endpoint under a
re-read guard. **Retry** (terminal only): re-reads `task_card.md`, dispatches a
fresh linked run (`retry_of`/`retried_by`).

### H. Project Ops — Git/GitHub (`git_ops` + `github_connector`)
One Git executor (`ToolRuntime.run_git`, allow-list + destructive gating; the
agent's `run_shell` blocks Git and never reaches `run_git`). Dispatch captures a
best-effort pre-run checkpoint; finalize captures a redacted post-run
`diff.patch`; neither auto-commits. Delivery is explicit two-phase contracts
(`…/git/commit|git/push|github/pr|git/rollback`, External Action Contract on
`confirm:false`, execute on `confirm:true`). The token reaches git only via
`GIT_ASKPASS` env. The brain sees only a compact git-state summary in
`@review`/`@debug`, never the raw diff.

### I. Production Path — Vercel/Supabase/Stripe (`*_connector` + `ops_ledger`)
Same two-phase contract shape as §H, over the `credentials.py` registry + the
`app_env` registry. Deploy/redeploy/rollback are run-scoped and **async** (confirm
stamps a transient `deploy_state`, returns immediately, finalizes off-thread via
`submit` polling Vercel to `READY`, then writes `deployment.json`/`deploy.log` +
an `OPS.md` ledger entry). Supabase adds migration/link contracts (apply =
`db push --linked`, destructive); Stripe adds test-mode checkout-provision +
webhook-register. Every mutating route rejects GENERAL; every secret reaches a
provider only via header/exec-env, never a contract/log/artifact/UI. Startup
`reconcile_stuck_external_actions` clears a crash-left transient state by
querying the provider (never auto-retries).

### J. Research channel — `research.py` + `web_search.py` (Phase 10)
The explicit `@search`/`@research` command is the **per-turn network grant**
(`main.py` sets `research_enabled`; a judge-labeled `research` intent instead
yields a `research_suggestion` chip that pre-fills the command — Send is the
approval). Granted turns run the combined loop in §A with two tools:
`web_search` (Tavily adapter; key header-only from the `search` credential
provider; snippets only) and `fetch_url` (SSRF-screened, allowlist-gated GET →
tag-stripped extract, per-fetch/per-turn caps, framed as untrusted evidence).
User-pasted URLs bypass only the allowlist; cross-host redirects lose that
privilege. Every executed action lands in `research_sources` (ChatResponse +
persisted message metadata → the 🔎 chip), and the ordinary memory-intake
judge distills durable findings into `RESEARCH.md ▸ Findings` (skipped for
GENERAL). The registry behind discovery: `agents_registry` (`GET /api/agents`)
drives both the composer autocomplete and the Agents browser; `skills_store`
folds a mode's committed skill markdown into its guidance block, and skill
files are written ONLY by the user's Save in the UI.

## 8. Core data model — `RunRecord` (serialized as `run.json`)

Identity/outcome: `run_id`, `project_id`, `task_title`, `status`
(`running`/`completed`/`partial`/`blocked`/`failed`/`cancelled`), `summary`,
`files_changed`, `commands_run`, `blockers`. All `Optional` fields below default
so old records round-trip; **no secret ever appears on the record.**

- **Verification** — `verification` (`VerificationResult`: `mode`,
  `commands[]` breakdown, `repair_attempts`), transient `verification_state`.
- **Browser/visual** — `browser_verification` (with `pages: BrowserPageCapture[]`
  + `readiness`; Phase 11 adds bounded redacted `console_errors[]` /
  `network_failures[]` + `flows: BrowserFlowResult[]` of per-step outcomes —
  fill values never stored, `value_masked` only), `browser_verification_state`,
  diagnostic-only `visual_review` (also `visual_review.json`).
- **Plan** — `plan` (`ExecutionPlan` of `ExecutionTask` units, also `plan.json`).
  Team fields on the task: `role`, `parallel_safe`, `wave`, `workspace`
  (`main`|`patch`); on the plan: `execution_mode` (`sequential`|`team`).
- **Integration (team)** — `integration` (compact `IntegrationResult`: waves,
  applied files, conflicts; full detail in `integration.json` + patch
  manifests), transient `integration_state`.
- **Memory** — `memory_reconciled` / `memory_reconciliation` (tag) / `_reason` /
  `_error`.
- **Recovery** — `recovery_assessment` (`RecoveryAssessment`; Phase 11 adds
  `recovery_type` + `classified_by`); budget lineage `recovery_budget` /
  `recovery_of` / `recovered_by` / `orchestration_round` (`recovered_by` set
  once → one recovery per parent, claimed on BOTH the auto and the manual
  confirm path).
- **Run control** — `cancel_requested`, `retry_of` / `retried_by`.
- **Git (Project Ops)** — `pre_run_checkpoint` / `checkpoint_tag` / `base_commit`
  (rollback anchor), `head_commit`, `branch`, `commit_sha`, `pushed`, `pr_url` /
  `pr_number`, `diff_stat` (raw diff stays in `diff.patch`), transient `git_state`.
- **Deploy** — `deployment_id` / `deployment_url` (normalized) /
  `deployment_target`, transient `deploy_state` / `external_state`. Project-level
  provisioning facts (Stripe/Supabase ids, env key names) live in `OPS.md`, not here.

Transient `*_state` fields mirror `verification_state` and are **never** a
`RunStatus`; every settle path clears the in-progress values so a crash can't
wedge the UI poll gates: `_finalize`/`_finalize_cancelled`/the crash handler
clear them in-process, and at startup two sweeps back them cross-process —
`sweep_stuck_runs` (rewrites a still-`running` run to `failed`, clearing all six)
and `sweep_terminal_transient_states` (clears a leaked in-progress value on an
already-*terminal* run — e.g. a crash during the post-status verify tail).
**Exception:** `browser_verification_state` also holds a *settled* result
(`passed`/`failed`), so the terminal sweep clears only its transient `running`.
Endpoints/finalizers that mutate a live run's record do it through the
per-`(project,run)` `run_store.mutate_run_json` lock (atomic reads never lose an
update). `ResultSummary` is the compact main-agent view (summary + lists +
verification/plan + commit/branch/PR/diff-stat + deployment metadata).

Status semantics: **`completed` only after verification passes (or safe skip)**;
files-written-but-failed is `partial`; `skipped` only when nothing safe can run.
`files_changed`/`commands_run` climb progressively during a run (atomic
rewrites) and are overwritten with the authoritative lists at finalize.

## 9. Invariants (do not break)

- **Sandbox boundary.** All repo paths + shell commands go through
  `ProjectSandbox` / `ToolRuntime`. No raw `os`/`pathlib`/`subprocess` on repo
  paths elsewhere. Bounded output previews everywhere.
- **Agent roles.** Main agent never edits `repo/` or runs shell. Coding Agent
  never edits memory (`projects/{id}/*.md`, `memory/*.md`) or other workspaces.
- **`SOUL.md`** is read-only + hidden — never shown, auto-written, or in any
  write path.
- **Explicit dispatch only.** No inferred-intent auto-run. Retry is an explicit
  click creating a new linked run. *Scoped exception:* a user-approved **recovery
  budget** set when explicitly confirming a contract authorizes ≤2 bounded,
  linked, audited auto-recovery runs (clamped at the confirm endpoint,
  idempotent via `recovered_by`, never from inferred intent or a crash).
  Phase 11 tightens, never loosens, this boundary: the Recovery Matrix contract
  gates auto-recovery by failure type (confirm-only types and environment
  failures never auto-dispatch; a visual/runtime repair child gets budget 0 —
  one pass), while the generic non-green case preserves the Phase 6.1 behavior
  exactly.
- **Interactive browser verification is declarative + bounded.** Flows come
  only from the committed `## Browser Verification` block (fixed action
  vocabulary, ≤2 flows × ≤10 steps, ≤6 explicit views, overall page cap 8) and
  run only against the local dev-server origin — no arbitrary browsing, no
  external sites, no exploration. Credential-shaped fill values / sensitive
  input targets are refused in the parent process (never serialized to the
  browser, never stored — `value_masked` only), a refused flow never fails the
  run and is never auto-repaired, and console/network evidence is bounded +
  redacted before persistence. A failed **declared** flow fails the
  verification; console errors alone never do.
- **Best-effort post-run steps** (verification / browser / reconciliation /
  checkpoint / diff / integration) never crash finalization and never leave a
  run stuck in `running`.
- **No auto-injection of repo contents** into the main agent's context.
- **Git is audited delivery, not shell.** All Git via `ToolRuntime.run_git`
  (`validate_git` + gating); `run_git` is not an agent tool. Commit/push/PR/
  rollback are explicit two-phase contracts — no inferred-intent Git. Credentials
  reach git only via the `GIT_ASKPASS` env (tokenless remote), never argv/
  `.git/config`/commits/logs/events/memory/prompts/UI. `credentials.py` is the
  sole secret reader; `status` is presence-only.
- **Team execution is bounded, isolated, explicitly integrated.** Parallelism
  only via the wave scheduler (≤`MAX_PARALLEL_AGENTS`=3, never the shared
  dispatch pool, no unbounded spawning). No two units write the same live tree —
  parallel writers use isolated patch workspaces (same `resolve_under` rules;
  `run_shell`/`run_git`/`run_supabase` blocked) and reach the repo only through
  the deterministic integration step (conflicts + apply-errors surfaced, never
  silent; a conflicted/error run is at best `partial`). Read-only tool sets are
  enforced in-loop. The coordinator is the sole run.json/plan.json writer;
  parallel events go through the per-run append lock. `completed` only when the
  **integrated** tree passes verification.
- **External connectors are contract-first + secret-clean.** Vercel / Supabase /
  Stripe go through two-phase preview→confirm contracts and the single secret
  reader; a token reaches a connector only via a header or exec-time env, never
  argv/logs/artifacts/UI; every returned string is redacted with `project_id`. No
  inferred-intent deploy/migration/payment; `orchestrator.py` imports no
  connector (asserted). Stripe stays TEST-only; `OPS.md` is written only by
  `ops_ledger`, never an LLM.
- **Web research is grant-gated, bounded, and egress-clean.** The Main Agent
  reaches the web ONLY through the research channel, enabled per-turn by an
  explicit `@search`/`@research` command — inferred intent never triggers
  network access (it only suggests the command). Fetches are SSRF-screened +
  domain-allowlisted (user-pasted URLs bypass only the allowlist), results are
  bounded cited extracts (never raw page dumps) framed as untrusted, and every
  action is audited in `research_sources`. The `redact()`-diff egress guard
  refuses any outbound query/URL carrying a **credential-shaped** value (stored
  secrets + known patterns); it does not (and is not relied on to) scrub
  arbitrary memory/repo text — that stays off the wire because it is never
  auto-injected and the channel prompt forbids sending it. Skills are curated
  committed markdown with exactly one writer — the user's explicit Save; no
  LLM/autonomous path writes a skill file.

## 10. Lessons worth keeping (mostly Windows + subprocess)

- **Unread PIPEs deadlock dev servers.** A child with `stdout/stderr=PIPE` nobody
  drains fills the OS buffer (~4–8 KB on Windows) and blocks before `listen()`.
  Drain with bounded background threads (`_StreamDrainer`).
- **Playwright sync API on a worker thread fails on Windows** (a *messageless*
  `NotImplementedError` from `SelectorEventLoop`). Run Playwright in a fresh
  subprocess; use `repr(exc)` for empty messages.
- **`python` on PATH ≠ a venv with your deps.** Probe (`python -c "import
  pytest"`) before inferring `pytest`, else fall back to a syntax check.
- **`compileall .` walks `node_modules`.** Exclude it (`-x node_modules`).
- **Server restarts orphan `running` runs.** `sweep_stuck_runs()` at startup
  rescues them to `failed`.
- **Snapshot `TASK.md` before applying the agent's update** so `task_md_update`
  can't clobber the verification config.
- **`subprocess(text=True)` decodes with the machine codec, not UTF-8.** On a
  non-UTF-8 Windows box, npm/Vite's UTF-8 output raises `UnicodeDecodeError` and
  silently loses output / kills a drainer thread. Always pass
  `encoding="utf-8", errors="replace"` to every capturing `Popen`/`run`.
- **A timeout kills only the direct child.** With `shell=True` the child is
  `cmd.exe`; `npm`/`node` grandchildren orphan, holding ports (5173/5174) + file
  locks. Reap the tree: `taskkill /F /T /PID` (Windows), `killpg` (POSIX).
- **Non-atomic JSON writes tear under polling.** run.json/plan.json are rewritten
  many times/run while the UI polls every 2 s; write a `.tmp` sibling then
  `os.replace` (`run_store._atomic_write_text`).
- **The agent writes whole files inline as JSON, so cap output high.** A default
  2048/8192 truncates a real component/seed module mid-string → unparseable
  action → task fails. `CODING_AGENT_MAX_TOKENS = 16384`; the prompt tells the
  agent to split a huge file across `write_file` + `append_file`.
- **Pinned model ids go stale** (`claude-sonnet-4-20250514` now 404s). Defaults
  are current, doc-verified ids (Claude → `claude-opus-4-8`), each env-overridable
  (`AGENT_OS_{CLAUDE,…}_MODEL`). Re-verify against docs when bumping.
- **A transient LLM blip must not kill a task.** `llm.chat` retries transient
  errors (connection/timeout/429/5xx) with backoff; deterministic ones (auth/4xx)
  are not retried.
- **The agent can hallucinate completion** (finalized after writing only
  `package.json`, fabricating the rest). Command verification (the real build) is
  the ground truth; the prompt forbids claiming unwritten files.
- **One repair pass isn't enough for a real build.** Cross-file type/import errors
  surface in waves; repair is an iterative loop that pre-reads erroring files and
  blocks `run_shell` (an unguarded repair agent burned every step re-running
  `tsc`).
- **Preview starts only the frontend.** Browser verification runs `npm run dev`
  alone; the agent is told to bundle seed data so the preview renders populated
  (never a stuck "Loading…").
- **"Progress" for a continuation = write ops, not unique paths.** A polish pass
  that overwrites files makes progress without new paths; the budget-extension
  check counts successful writes.
- **Windows strips trailing dots/spaces from each path component.** `".git."` /
  `".env."` / `"server.pem."` open the *real* `.git`/`.env`, so a raw
  equality/suffix guard is bypassable — `sandbox.resolve_under` normalizes each
  component (`rstrip(" .")`) before the sensitive-name/`.git` checks. It also
  screens **reserved device names** (`NUL/CON/PRN/AUX/COM1-9/LPT1-9`): they open a
  device, so a "write" succeeds against nothing and the run claims phantom output.
- **`browser_verification_state` is BOTH transient and settled.** `'running'` is
  an in-progress gate; `'passed'`/`'failed'` are the real outcome. A blanket
  "clear every transient `*_state` on a terminal run" sweep wipes the settled
  result — the terminal-run sweep (`sweep_terminal_transient_states`) must clear
  only `'running'` for that field (while clearing any non-null
  `verification_state`/`integration_state`/`git_state`, which are purely
  in-progress). A live backend-restart during E2E caught the over-broad first cut.
- **`verification_state` is set AFTER the status is already terminal.** The
  verify/browser tail runs post-status, so a crash there leaves `completed` +
  `verification_state='verifying'` — which `sweep_stuck_runs` skips (not
  `running`). Needs a companion terminal-run sweep, or the UI poll-gate spins
  forever. (The startup sweep pair is the cross-process backstop; the in-process
  crash handler clears all six directly.)
- **`os.path.replace` is atomic but not a lock.** run.json is read-modify-written
  by many endpoints + off-thread finalizers; atomic writes stop *torn reads* but
  not *lost updates*. Contended mutators (cancel / browser-verify / deploy-confirm
  / deploy-finalizer) go through the per-`(project,run)` `run_store.mutate_run_json`
  lock; the check-then-act deploy guard and pending-confirm dispatch became atomic
  claims (`_claim_deploy` under the lock; DB `claim_pending_execution`) so a
  double-click can't launch two runs/deployments.
- **Case-insensitive filesystems need `os.path.normcase` for "same file".** Two
  parallel patch tasks writing `src/App.ts` and `src/app.ts` map to ONE on-disk
  file on Windows/macOS; keying integration on the exact case misses the collision
  and one task silently overwrites the other. Key on `normcase` (a no-op on
  case-sensitive POSIX, so distinct files still both apply).
- **A key in a request URL leaks into error messages.** `_http_post_json` echoes
  the URL into every `ProviderError` (→ logs/tracebacks); the Gemini key rides the
  `x-goog-api-key` header instead, and `_safe_url` redacts any stray `key=` query
  secret defensively.
- **Truncate-then-redact leaks secrets; always redact first.** Exact-value
  redaction (`credentials.redact`) fails on a secret sliced mid-value by an
  earlier truncation — e.g. a ~230-char Supabase `service_role` JWT in a failed
  local API URL cut at a 300-char console-entry cap. Any bounded evidence path
  (capture-subprocess entries, recovery-evidence fields) must apply the
  generous transport cap first, then redact, then apply the small display
  truncation — never truncate small before redacting. (Caught by the Phase 11
  adversarial review.)

## 11. Starting a future session

1. Read `CLAUDE.md`, this file, then the newest `ROADMAP.md` entries for what
   just landed.
2. Match the per-feature module + per-feature test-file convention.
3. Keep changes bounded to the files the task names; propose refactors separately.
4. When done: run the relevant `backend/tests/<file>.py`, note what you ran (and
   didn't), and update the right doc(s) per the policy table in `ROADMAP.md`.
