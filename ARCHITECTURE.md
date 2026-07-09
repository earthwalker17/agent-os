# Agent OS — Architecture

The current shape of the system — files, pipelines, invariants, lessons. This doc describes
**what the system is now**; the history of how it got here lives in `ROADMAP.md`. Pair with
`CLAUDE.md` (rules).

---

## 1. What it is

A **local-first project cockpit** — one web chat surface for many long-term projects:
project-scoped conversations, structured markdown memory, orchestration, and a sandboxed
execution layer. Two agents, clean separation:
- **Main agent = brain.** Planner / memory steward / orchestrator. Talks to the user, loads
  memory, decides delegation. **Never edits `repo/` or runs shell.**
- **Coding Agent = hands.** Bounded executor inside one project's
  `execution_workspaces/{id}/repo/`. **Never edits memory or other projects.**

## 2. Core principles

- **Local-first.** Filesystem + SQLite + FastAPI + React. No cloud infra, no queues;
  ThreadPoolExecutor over Celery, polling over SSE — until there's a concrete reason to swap.
- **Project isolation.** Each project owns its conversations, memory, and workspace; crossings are deliberate and bounded.
- **Structured markdown memory.** Durable state lives in readable `.md` files.
- **One sandbox chokepoint.** Every repo path + shell command routes through `ProjectSandbox`
  → `ToolRuntime`; no raw `os`/`pathlib`/`subprocess` on repo paths elsewhere.
- **No auto-injection of repo contents** into the main agent's context — bounded on-demand reads only.
- **Explicit execution only.** Inferred coding intent never auto-runs; a human click (or `@code`) is required.

## 3. High-level shape

```
┌────────────┐    ┌────────────────┐    ┌──────────────────────────┐
│  Frontend  │ ←→ │  FastAPI       │ ←→ │  LLM providers: Claude / │
│  (React)   │/api│  (main.py)     │    │  GPT / Gemini / DeepSeek │
└────────────┘    └───────┬────────┘    │  / Kimi / GLM            │
        ┌─────────────────┼───────────┐ └──────────────────────────┘
        ▼                 ▼           ▼
   memory/          projects/{id}/   execution_workspaces/{id}/
   (global .md)     (project .md)    repo/ · runs/ · patches/ · logs/ · AGENT.md · TASK.md
```

Frontend dev (Vite, 5173) proxies `/api` → backend (8000); verified preview apps use **5174**.

## 4. Directory layout

```
Agent OS/
├─ CLAUDE.md / ROADMAP.md / ARCHITECTURE.md / BLUEPRINT.md / README.md (+ README.png hero)
├─ install.ps1 / start.ps1      Windows installer + launcher
├─ memory/                      global: SOUL.md (read-only anchor), USER/WORKSTYLE/MEMORY.md
├─ skills/{agent_id}/{skill}.md committed skill markdown (edited only by the user via the UI)
├─ projects/{id}/               PROJECT/STATUS(+`## Task Queue`)/DECISIONS/RESEARCH/LESSONS.md (+ OPS.md ledger)
├─ execution_workspaces/{id}/
│  ├─ repo/                     working code tree (Coding Agent's sandbox); repo/uploads/ for chat files
│  ├─ runs/{run_id}/            task_card.md · events.jsonl · run.json · plan.json · result.md ·
│  │                            integration.json · diff.patch · deployment.json · deploy.log ·
│  │                            visual_review.json · screenshots/
│  ├─ patches/{run_id}/{task}/  isolated patch workspace: workspace/ overlay + manifest.json
│  └─ logs/  AGENT.md  TASK.md
├─ chat_uploads/{conversation}/ chat-only attachments (gitignored)
├─ backend/                     FastAPI + execution layer (Python)
└─ frontend/                    React + TypeScript (Vite)
```

`SOUL.md` is committed; other global/project memory files are gitignored (only `*.example.md`
templates + README explainers ship), and execution workspaces are likewise private except their
README + example templates. `agent_os.db` (SQLite, WAL) holds conversations, messages, `pending_executions`.

## 5. Backend modules

### Top level (`backend/`)
| File | Purpose |
|------|---------|
| `main.py` / `database.py` | FastAPI app + every HTTP endpoint (projects, conversations, chat, memory, execution, inspection, verification, preview, run control, Git/deploy/migration contracts); SQLite persistence (conversations, messages, `pending_executions`). Startup: stuck-run + stuck-external-action sweeps, memory-scaffold migration. |
| `orchestrator.py` | The chat brain: loads SOUL + memory (compacted — STATUS/PROJECT/SOUL whole, append-growth files tail-trimmed), assembles context (+ optional `@`-mode block with that mode's skills folded in), produces the reply, runs the bounded inspection/research loop, then the memory-intake judge. Imports no connector/executor (test-asserted). |
| `agents_registry.py` / `skills_store.py` | Leaf agent-profile registry behind `GET /api/agents` (10 frozen profiles: command/aliases, mode↔role linkage by string, capability badges, approval boundary, skill index — presentation + contract, not permissions); the ONLY read/write path for `skills/{agent}/{skill}.md` (registry-validated, atomic, 20k cap; written only by the user's Save in the UI) + the bounded `skills_prompt_block(mode)` fold-in. |
| `memory_engine.py` | Stdlib-only single markdown write path `apply_update` (policy-filtered, atomic temp+`os.replace`, append-deduped, robust section replace), writable-file sets, `MemoryDecision`, idempotent scaffold incl. the legacy `TASK_QUEUE.md` → `STATUS.md ## Task Queue` migration. `OPS.md` is scaffolded but excluded from every writable set. |
| `llm.py` / `providers.py` | Thin `chat`/`chat_vision` entry with shared transient-retry, over a capability-aware model registry — Claude / GPT / Gemini / DeepSeek / Kimi / GLM: key-presence availability (+ env aliases), Claude-first default, per-model `vision` flags, env-overridable default model/base-URL. Anthropic via SDK; others via OpenAI-compatible/`generateContent` `urllib` (no new deps). |
| `uploads.py` | Chat attachment storage: sanitize + allow-list + dedup, chat-only under `chat_uploads/{conv}/`, optional workspace copy via `ProjectSandbox`. |
| `credentials.py` | The ONLY reader of secret values: provider registry (`github\|vercel\|supabase\|stripe`), typed secret/non-secret getters, presence-only `status`, sole app-env value reader (`get_env_value`), Stripe live-gate at the store boundary. `redact()` covers known secret patterns + every stored token. Store under `credentials/` (gitignored). |

### Execution layer (`backend/execution/`)
| File | Purpose |
|------|---------|
| `manager.py` / `models.py` / `tool_models.py` | Workspace filesystem layout + idempotent init + `read/update_task_state`; pydantic run/plan/verification/recovery/integration models (see §8); `ToolResult` + per-tool request models. |
| `sandbox.py` | `ProjectSandbox` — **the boundary**. `resolve_under(root, rel)` validates every path (absolute/`..`/sensitive-name/`.git`/escape rejection); `validate_command` / `validate_git` / `validate_supabase` (allow-list + destructive-by-subcommand gating). |
| `tool_runtime.py` | `ToolRuntime` — the six sandboxed file/shell tools with output caps, plus typed executors `run_git` / `run_supabase` (`shell=False` argv; NOT agent tools; `run_supabase` gets a scrubbed allow-listed env). |
| `patch_workspace.py` | Isolated patch workspaces for team runs: reads fall through overlay→repo, writes land under `patches/`, `run_shell`/`run_git`/`run_supabase` blocked; per-task `manifest.json` audit artifact. |
| `templates.py` / `prompts.py` | Default `AGENT.md`/`TASK.md` seeds; Coding Agent system + step/correction/repair/continuation/plan prompts, with optional role overlay (empty = byte-identical to legacy). |
| `planner.py` / `roles.py` | Pure planning: `looks_complex` gate, tolerant plan parsing, `fallback_plan`; graph helpers (`topological_order`, `compute_waves` via Kahn layering, cycle remainder forced sequential), team eligibility, `MAX_PARALLEL_AGENTS = 3`. Agent role registry: prompt overlay + enforced `allowed_tools` + read-only flag — execution roles `coder`/`reviewer`/`inspector`, system stages `integrator`/`verifier`, chat `@`-mode ↔ role map; unknown role → coder. |
| `integration.py` | Deterministic per-wave merge for team runs: overlays apply into the shared repo through the base `ToolRuntime`, plan order; identical content de-dupes, conflicts → first-writer-wins + `IntegrationConflict` + blocker. No LLM. |
| `runner.py` | `CodingAgentRunner`: phased run (plan → execute → finalize) with verification + iterative repair, cooperative cancel at step boundaries, progressive live metrics. Team path: wave scheduling on a dedicated per-batch pool, per-unit runtime + enforced tools, per-wave `integrate_wave`; the coordinator is the sole run.json/plan.json writer. |
| `background.py` | `BackgroundRunManager`: thread-pool dispatch; crash → `failed` (clears every transient sub-status); per-run cancel-`Event` registry; linked retry/recovery dispatch. `_maybe_auto_recover` (clean-finalize only) auto-dispatches ONE linked recovery run when a user-approved budget remains, gated by the Recovery Matrix contract. |
| `chat_delegation.py` / `delegation_intent.py` / `delegation_judge.py` | `@code` trigger + deterministic mode `@`-commands (`@plan`/`@design`/`@debug`/`@review`/`@inspect`/`@memory`/`@search`/`@research`) — they shape the response, never dispatch; `@search`/`@research` additionally grant the research channel for that turn. Primary LLM semantic delegation judge (3 decisions + an informational `intent` label) with a heuristic fallback detector. |
| `pending_execution.py` | Confirmable execution plans (store / render / revise); a recovery proposal carries `recovery_of` so the confirm endpoint threads full lineage. |
| `memory_reconciliation.py` | Post-run bounded reconciliation judge: writes STATUS/DECISIONS/RESEARCH via `memory_engine`; skip rules; once-only. |
| `recovery.py` / `recovery_matrix.py` | Best-effort `assess_run`: interprets a non-green terminal run, recommends ONE bounded next step (confirmable handoff — never auto-dispatches). The matrix is a leaf module (no LLM/IO): frozen per-type contracts (build/runtime/visual auto-eligible under budget with child-budget caps; integration/deployment/database/product/docs_memory confirm-only), deterministic `classify_failure` run pre-LLM as a strong prior (validated fallback, audited via `classified_by`; `auto_ok=False` marks environment failures — missing Playwright, occupied port), and a ≤1800-char redacted evidence builder for the follow-up task card. |
| `inspect.py` / `local_rag.py` | Main-agent on-demand, read-only inspection (bounded, max 3/turn), incl. the `retrieve` tool: minimal keyword-based (not vector) retrieval — bounded, cited evidence from project memory, recent run history, and a sandbox-safe repo map (sensitive names filtered). Per-source + per-turn caps; never raises. Also `POST …/retrieve`. |
| `skill_patch.py` | Review-first suggested skill patch: a best-effort, green-run-only judge PROPOSES an append-style refinement on run.json; applied ONLY through `skills_store.write_skill` on explicit user action. Never creates skills or promotes globally. |
| `research.py` / `web_search.py` | The bounded research channel (mirrors `inspect.py`): strict `{"research_request": …}` protocol, two tools — `web_search` (Tavily adapter; key from `credentials.get_token("search")` header-only; snippets only) and `fetch_url` (bounded tag-stripped extract). SSRF screening (scheme/port/userinfo, public-DNS enforcement, redirect re-screening), domain allowlist (user-pasted URLs bypass only that), `redact()`-diff egress guard, per-fetch 6k + per-turn 16k caps, untrusted-content framing. Never raises into the loop. |
| `verification.py` / `browser_verification.py` | Command verification: `plan_verification` (manual `## Verification` block else inferred), `run_verification_specs`, render. Browser lifecycle: dev server → HTTP readiness → render-readiness-gated multi-page Playwright capture → teardown. The `## Browser Verification` block declares `### Views` / `### Flow:` subsections (fixed vocabulary `goto/click/fill/submit/expect_text/screenshot`; caps 6 views / 2 flows / 10 steps); the capture subprocess collects bounded redacted console/network evidence and executes flows with per-step screenshots. Credential-shaped fill values refused in-parent. A failed **declared** flow fails the verification; console errors and policy refusals never do. |
| `visual_judge.py` / `preview.py` | AI visual judgment over screenshots (`run_visual_review` → verdict + rationale, `visual_review.json`); resolves a vision-capable model, skips gracefully otherwise; folds a bounded runtime-signals block into the prompt. Diagnostic-only. Plus managed long-lived preview dev servers (one per project). |
| `git_ops.py` / `github_connector.py` / `_git_askpass.sh` | Sandboxed local Git via `run_git` (`ensure_repo`, tagged out-of-branch checkpoints, redacted bounded `capture_diff`, secret-refusing `commit`, `create_branch`, gated `rollback`, read-only `list_commits` behind `GET /git/log` — redacted + bounded; never raises into the run loop) + GitHub via REST/`urllib` (no `gh` CLI): `status`, tokenless `ensure_remote`, `push_branch`, `create_pull_request` — token injected ONLY via the generated `GIT_ASKPASS` env, never argv/`.git/config`. Output redacted. |
| `app_env.py` / `vercel_connector.py` / `supabase_connector.py` / `stripe_connector.py` | Project app-env registry (`credentials/env/{id}.json`: the BUILT app's env vars, presence-only; secret values register into the redactor) + REST-over-`urllib` connectors. Vercel: `status`, `create_deployment`, `promote_deployment` (rollback), `set_env_var` — token in the `Authorization` header only. Supabase: Management REST `status` + sandboxed CLI (`link` / migration preview `db push --dry-run` / gated apply `--linked`); secrets ride `run_supabase` `env_extra` only. Stripe test-mode: form-encoded REST + `Idempotency-Key`, per-request test-gate; `provision_price`, `register_webhook` (returned `whsec_` stored, never echoed). Every returned string redacted. |
| `ops_ledger.py` | The ONE writer of `projects/{id}/OPS.md`: deterministic, ids/URLs/key-NAMES only, redacted at the call site, idempotent on `dedup_key`. |

`tests/` mirrors these per feature; all stub `llm.chat` (no API key needed).

## 6. Frontend modules (`frontend/src/`)

| File | Purpose |
|------|---------|
| `main.tsx` / `App.tsx` / `types.ts` | Entry + three-column layout + top-level state; theme via `data-theme` on `<html>`, persisted; shared TS types mirroring backend models. |
| `components/ProjectList.tsx` / `ContextPanel.tsx` / `RunsSection.tsx` | Left column (Agents & Skills button, a flat "Conversations" section for the GENERAL workspace, projects + conversations + CRUD, footer Settings gear); right column grouped into labeled sections (Project Memory files, Integrations, Workspace env registry, Links, Runs — the runs list auto-polls while active, live files/cmds counts, Start/Stop preview, per-row Trace). |
| `components/IntegrationsPanel.tsx` (+ `GitPanel.tsx` / `GitHubModal.tsx`) | Right-column Integrations: one card per provider (GitHub / Vercel / Supabase / Stripe) showing a brand mark + live connection status (fetched from the four validated `/…/status` endpoints). Clicking a card opens that provider's OWN central modal — GitHub → `GitHubModal` (`GitPanel`'s repo + working-tree body plus an expandable, read-only git-history view from `/git/log`), the others → `ConnectorsModal` locked to that single provider. Presentation only; every underlying endpoint/contract unchanged. |
| `components/AgentsModal.tsx` / `CommandMenu.tsx` | Agents browser over `GET /api/agents` (command/capability chips, full contract detail, per-agent skills editable in place) + composer `@`-command autocomplete derived from the same registry. |
| `components/ChatPanel.tsx` (+ `ModelPicker.tsx`) | Center: header, message thread, and a shared multi-modal composer (auto-grow textarea, file upload with "add to workspace", voice, `@`-autocomplete, in-row model picker, arrow send). When a workspace is selected with no conversation open, renders a landing state (time-of-day greeting, quick `@`-chips, per-project Recent Runs) whose first send creates the conversation then transitions into the thread. Renders `RunChatCard` on `run_id` messages, research-sources + suggestion chips; the per-provider model picker gates image upload by the model's `vision` flag. |
| `components/RunChatCard.tsx` | In-chat run lifecycle: live phase badge + wave-grouped task checklist with role chips → build/verify phases → summary → browser verification (preview URL, screenshot gallery, AI visual verdict) → integration summary → Git/deploy/migration/Stripe panels; Cancel / Retry / Live trace. |
| `components/RunDetailModal.tsx` / `RunTimeline.tsx` / `RunTrace.tsx` (+ `runEventUtils.ts`) | Detailed inspection (per-command verification, browser + visual review, Plan & Tasks, Team Integration, Recovery, memory reconciliation, `result.md`); settled-milestone timeline (start/settle pairs collapsed); live Trace modal (`tool_call`+`tool_result` paired, interleaving-safe via `task_id`; polls `…/events?since=`). |
| `components/GitOpsPanel / DeployOpsPanel / MigrationPanel / StripePanel / ExternalActionPanel` | Two-phase contract drivers (preview→confirm) for commit/push/PR/rollback, deploy/redeploy/rollback, Supabase link/migrate, Stripe provision/webhook. |
| `components/EditModal / ConfirmDialog / GlobalMemoryModal / ConnectorsModal / EnvRegistryPanel` | Memory editing, confirmations, global-memory viewer (SOUL.md leads, tagged read-only-to-the-agent but user-editable); connector setup (Vercel/Supabase/Stripe — write-only, presence-only display, Stripe TEST badge, optional `singleProvider` lock) + app-env registry with per-key "Push to Vercel". |

## 7. Key pipelines

### A. Chat turn (non-`@code`) — `orchestrator.orchestrate()`
Load SOUL + global + compacted project memory → assemble context (+ an optional `@`-mode block
that shapes the response, never dispatching) → intent routing (an explicit mode command skips
the delegation judge; `dispatch_suggested` creates a pending plan — never a run; judge failure →
heuristic fallback) → main response, which may emit `{"inspect_request": …}` (bounded repo
reads) and, on an explicit research grant, `{"research_request": …}` (§J) → memory-intake judge
(policy-filtered atomic writes via `memory_engine`; skipped for GENERAL research turns). 3–4
LLM calls/turn; the user-selected model serves only the main response.

### B. Delegation → run
`@code` → `chat_delegation` → `BackgroundRunManager.dispatch()`; inferred intent → pending plan
→ **OK** click → `…/execution/pending/{id}/confirm` → the same `dispatch()`. **No other path runs.**

### C. Coding Agent run — `CodingAgentRunner.run_task()`
Init run dir + `run.json` (`running`); load `AGENT.md`/`TASK.md`. **Plan:** `looks_complex`
gates — simple cards skip the LLM; complex cards run a bounded read-only inspection loop ending
in an `ExecutionPlan` (`plan.json`; failure → single-task fallback).
**Execute**, most conservative route first: *single-task* (bounded loop, `MAX_STEPS=24`);
*sequential multi-task* (topological order, `MAX_TASK_STEPS=20`, dependents of failed deps
skipped); *team* (only when `plan_is_team_eligible`) — topological **waves**, parallel-eligible
units on a dedicated bounded pool (≤3), coders in isolated patch workspaces, read-only
reviewer/inspector on the shared repo (§9); the coordinator settles units, **integrates** each
wave (first-writer-wins; conflicts + apply-errors surfaced, capped at `partial`), then runs
remaining tasks sequentially. **Finalize** status/summary/lists; update `TASK.md` (snapshot
first); a best-effort tail (never fails finalization) runs §D over the *integrated* tree, §E,
memory reconciliation, and recovery assessment; write `run.json`/`plan.json`/`result.md`.

### D. Command verification + repair — `verification.py` + `runner._verify_with_repair`
A manual `## Verification` block wins; else infer (`npm install`+`build`; `pytest` iff
importable, else `compileall`); else skip. Specs run in order, stopping at the first failure;
a failed run that produced files enters a bounded iterative repair loop (`MAX_REPAIR_ATTEMPTS`):
pre-read the erroring files, re-edit (`run_shell` blocked), re-verify — until green, a
no-change pass, or the cap. Still failing → `completed` downgrades to `partial` + a blocker.

### E. Browser preview + visual review — `browser_verification` + `visual_judge` + `preview`
`POST …/browser-verify`: `npm install` → dev server (5174) → poll → render-readiness-gated
multi-page Playwright capture (entry + declared `### Views` + a few discovered; readiness
timeout → captured anyway, marked `unconfirmed`) → declared interaction flows with per-step
screenshots, console/network evidence redacted in-parent. Pass → the server hands off to
`preview.py` (Start/Stop in the Runs panel); a failed flow → `failed`, no handoff. A port
pre-flight aborts when a FOREIGN server holds the dev port (it would be screenshot as this
app) — but the project's OWN keep-alive preview is stopped automatically and verification
proceeds. The same request sends screenshots + task context + runtime signals to a vision
model → structured verdict (diagnostic-only; skips without one).

### E2. Recovery Matrix — typed, evidence-driven repair
`recovery_matrix.classify_failure` runs deterministically before the `assess_run` LLM call, and
the follow-up task card carries a bounded redacted evidence block for the recovery child. The
child's own tail re-runs verification → browser → visual judge (before/after linked by
`recovery_of`/`recovered_by`). The auto path also fires on a `completed` run whose visual
verdict failed — only for contract-`auto_eligible` types classified `auto_ok` (environment
failures never auto-repair), the child's budget clamped by the contract cap (visual/runtime → 0
= one repair pass). The manual path threads the same lineage; a parent with a recovery already 409s.

### F. Chat attachment upload — `uploads.py`
Files hit `POST /api/chat/upload` before the chat send — sanitized, de-duped, chat-only,
optionally copied into `repo/uploads/` via the sandbox; metadata rides the `/api/chat` body
(chips re-hydrate on reload).

### G. Run control — timeline + cancel + retry
`GET …/runs/{id}/events` (optional `since` cursor) feeds the settled `RunTimeline` + the live
`RunTrace`; the chat card derives its phase badge + task checklist from `run.json`. **Cancel**
(only `running`): cooperative at step boundaries via a per-run `Event` (orphaned runs finalized
by the endpoint under a re-read guard). **Retry** (terminal only): re-reads `task_card.md` into
a fresh linked run (`retry_of`/`retried_by`).

### H. Project Ops — Git/GitHub (`git_ops` + `github_connector`)
One Git executor (`ToolRuntime.run_git`; the agent's `run_shell` blocks Git). Dispatch captures
a best-effort pre-run checkpoint, finalize a redacted post-run `diff.patch`; neither
auto-commits. Commit / push / PR / rollback are explicit two-phase contracts (preview →
confirm); the token reaches git only via `GIT_ASKPASS`. The brain sees a compact git-state
summary in `@review`/`@debug`, never the raw diff.

### I. Production Path — Vercel/Supabase/Stripe (`*_connector` + `ops_ledger`)
Two-phase contracts as §H, over the `credentials.py` + `app_env` registries. Deploys are
run-scoped and **async**: confirm stamps a transient `deploy_state`, returns immediately,
finalizes off-thread by polling Vercel to `READY`, then writes `deployment.json`/`deploy.log` +
an `OPS.md` ledger entry. Supabase adds link/migration contracts, Stripe test-mode checkout +
webhooks. Every mutating route rejects GENERAL; startup reconciliation clears a crash-left
transient state by querying the provider (never auto-retries).

### J. Research channel — `research.py` + `web_search.py`
The explicit `@search`/`@research` command is the **per-turn network grant** (a judge-labeled
`research` intent only yields a suggestion chip that pre-fills the command — Send is the
approval). Granted turns add `web_search` + `fetch_url` to the §A loop, returning bounded cited
extracts framed as untrusted; user-pasted URLs bypass only the allowlist (cross-host redirects
lose that privilege). Every action is audited in `research_sources`; the memory-intake judge
distills durable findings into `RESEARCH.md ▸ Findings`.

## 8. Core data model — `RunRecord` (serialized as `run.json`)

Identity/outcome: `run_id`, `project_id`, `task_title`, `status` (`running`/`completed`/
`partial`/`blocked`/`failed`/`cancelled`), `summary`, `files_changed`, `commands_run`, `blockers`.
All `Optional` fields below default so old records round-trip; **no secret ever appears on the record.**

- **Verification** — `verification` (mode, per-command breakdown, repair attempts), transient `verification_state`.
- **Browser/visual** — `browser_verification` (`pages`, `readiness`, bounded redacted
  `console_errors[]` / `network_failures[]`, `flows[]` of per-step outcomes — fill values
  never stored, `value_masked` only), `browser_verification_state`, diagnostic-only `visual_review`.
- **Plan** — `plan` (`ExecutionPlan` of `ExecutionTask` units, also `plan.json`); team fields
  per task: `role`, `parallel_safe`, `wave`, `workspace`; per plan: `execution_mode`.
- **Integration (team)** — compact `IntegrationResult` (waves, applied files, conflicts; full
  detail in `integration.json` + patch manifests), transient `integration_state`.
- **Memory / run control** — `memory_reconciled` / `memory_reconciliation` / `_reason` /
  `_error`; `cancel_requested`, `retry_of` / `retried_by`.
- **Recovery** — `recovery_assessment` (incl. `recovery_type` + `classified_by`); lineage
  `recovery_budget` / `recovery_of` / `recovered_by` / `orchestration_round` (`recovered_by`
  set once → one recovery per parent, claimed on both the auto and manual confirm paths).
- **Git** — `pre_run_checkpoint` / `checkpoint_tag` / `base_commit` (rollback anchor),
  `head_commit`, `branch`, `commit_sha`, `pushed`, `pr_url` / `pr_number`, `diff_stat` (raw
  diff stays in `diff.patch`), transient `git_state`.
- **Deploy** — `deployment_id` / `deployment_url` / `deployment_target`, transient
  `deploy_state` / `external_state`; provisioning facts (Stripe/Supabase ids, env key names) live in `OPS.md`, not here.

Transient `*_state` fields are **never** a `RunStatus`, and every settle path clears the
in-progress values so a crash can't wedge the UI poll gates: `_finalize` / `_finalize_cancelled`
/ the crash handler clear them in-process; at startup, `sweep_stuck_runs` (still-`running` →
`failed`, clearing all six) and `sweep_terminal_transient_states` (clears a leaked in-progress
value on an already-*terminal* run) back them cross-process. **Exception:**
`browser_verification_state` also holds a *settled* result (`passed`/`failed`), so the terminal
sweep clears only its transient `running`. Mutators of a live run's record go through the
per-`(project,run)` `run_store.mutate_run_json` lock (atomic reads never lose an update);
`ResultSummary` is the compact main-agent view.

Status semantics: **`completed` only after verification passes (or safe skip)**;
files-written-but-failed is `partial`; `skipped` only when nothing safe can run.
`files_changed`/`commands_run` climb progressively during a run and are overwritten with the
authoritative lists at finalize.

## 9. Invariants (do not break)

- **Sandbox boundary.** All repo paths + shell commands go through `ProjectSandbox` /
  `ToolRuntime` — no raw `os`/`pathlib`/`subprocess` on repo paths elsewhere; bounded output previews everywhere.
- **Agent roles.** Main agent never edits `repo/` or runs shell. Coding Agent never edits
  memory (`projects/{id}/*.md`, `memory/*.md`) or other workspaces.
- **`SOUL.md`** is read-only to every agent/LLM path — never auto-written and in
  no judge/reconciliation writeback allow-list. It is shown + user-editable in the
  Global Memory modal via the one explicit `/global-memory/update-file` endpoint
  (the sole SOUL.md write path); no LLM path can reach it.
- **Explicit dispatch only.** No inferred-intent auto-run; retry is an explicit click
  creating a new linked run. *Scoped exception:* a user-approved **recovery budget**, set when
  explicitly confirming a contract, authorizes ≤2 bounded, linked, audited auto-recovery runs
  (clamped at the confirm endpoint, idempotent via `recovered_by`, never from inferred intent
  or a crash). The Recovery Matrix tightens, never loosens, this boundary: auto-recovery is
  gated by failure type (confirm-only types and environment failures never auto-dispatch; a
  visual/runtime repair child gets budget 0 — one pass); the generic non-green case keeps the
  original budget behavior exactly.
- **Interactive browser verification is declarative + bounded.** Flows come only from the
  committed `## Browser Verification` block (fixed action vocabulary, ≤2 flows × ≤10 steps, ≤6
  explicit views, overall page cap 8), run only against the local dev-server origin — no
  arbitrary browsing, external sites, or exploration. Credential-shaped fill values / sensitive
  input targets are refused in the parent process (never serialized to the browser, never
  stored — `value_masked` only); a refused flow never fails the run and is never auto-repaired;
  console/network evidence is bounded + redacted before persistence. A failed **declared**
  flow fails the verification; console errors alone never do.
- **Best-effort post-run steps** (verification / browser / reconciliation / checkpoint /
  diff / integration) never crash finalization and never leave a run stuck in `running`.
- **No auto-injection of repo contents** into the main agent's context.
- **Git is audited delivery, not shell.** All Git via `ToolRuntime.run_git` (`validate_git` +
  gating); `run_git` is not an agent tool. Commit/push/PR/rollback are explicit two-phase
  contracts — no inferred-intent Git. Credentials reach git only via the `GIT_ASKPASS` env
  (tokenless remote), never argv/`.git/config`/commits/logs/events/memory/prompts/UI.
  `credentials.py` is the sole secret reader; `status` is presence-only.
- **Team execution is bounded, isolated, explicitly integrated.** Parallelism only via the
  wave scheduler (≤`MAX_PARALLEL_AGENTS`=3, never the shared dispatch pool, no unbounded
  spawning). No two units write the same live tree — parallel writers use isolated patch
  workspaces (same `resolve_under` rules; `run_shell`/`run_git`/`run_supabase` blocked) and
  reach the repo only through the deterministic integration step (conflicts + apply-errors
  surfaced, never silent; a conflicted/error run is at best `partial`); read-only tool sets
  are enforced in-loop. The coordinator is the sole run.json/plan.json writer; parallel events
  go through the per-run append lock. `completed` only when the **integrated** tree passes verification.
- **External connectors are contract-first + secret-clean.** Vercel / Supabase / Stripe go
  through two-phase preview→confirm contracts and the single secret reader; a token reaches a
  connector only via a header or exec-time env, never argv/logs/artifacts/UI; every returned
  string is redacted. No inferred-intent deploy/migration/payment; `orchestrator.py` imports no
  connector (asserted); Stripe stays TEST-only; `OPS.md` is written only by `ops_ledger`, never an LLM.
- **Web research is grant-gated, bounded, and egress-clean.** The Main Agent reaches the web
  ONLY through the research channel, enabled per-turn by an explicit `@search`/`@research`
  command — inferred intent never triggers network access (it only suggests the command).
  Fetches are SSRF-screened + domain-allowlisted (user-pasted URLs bypass only the allowlist),
  results are bounded cited extracts (never raw page dumps) framed as untrusted, and every
  action is audited in `research_sources`. The `redact()`-diff egress guard refuses any
  outbound query/URL carrying a **credential-shaped** value (stored secrets + known patterns);
  it does not (and is not relied on to) scrub arbitrary memory/repo text — that stays off the
  wire because it is never auto-injected and the channel prompt forbids sending it. Skills are
  curated committed markdown with exactly one writer — the user's explicit Save; no
  LLM/autonomous path writes a skill file.

## 10. Lessons worth keeping (mostly Windows + subprocess)

- **Unread PIPEs deadlock dev servers.** A child with `stdout/stderr=PIPE` nobody drains
  blocks before `listen()` (~4–8 KB OS buffer on Windows); drain with bounded background threads.
- **Playwright's sync API on a worker thread fails on Windows** (a *messageless*
  `NotImplementedError`). Run it in a fresh subprocess; use `repr(exc)` for empty messages.
- **`python` on PATH ≠ a venv with your deps.** Probe importability before inferring `pytest`
  (else fall back to a syntax check), and exclude `node_modules` from `compileall`.
- **Crashes leave wedged state; startup sweeps rescue it.** `sweep_stuck_runs` fails orphaned
  `running` runs; the verify/browser tail runs post-status, so a crash there also needs
  `sweep_terminal_transient_states` — which must clear only `'running'` for
  `browser_verification_state`, whose settled `passed`/`failed` is a real outcome.
- **Snapshot `TASK.md` before applying the agent's update** so it can't clobber the
  verification config.
- **`subprocess(text=True)` decodes with the machine codec, not UTF-8** — npm/Vite output then
  raises `UnicodeDecodeError` and silently vanishes; always pass `encoding="utf-8", errors="replace"`.
- **A timeout kills only the direct child.** With `shell=True`, `npm`/`node` grandchildren
  orphan and hold ports/locks. Reap the tree: `taskkill /F /T` (Windows), `killpg` (POSIX).
- **Atomic writes stop torn reads, not lost updates.** Write `.tmp` + `os.replace` for
  pollers; route contended mutators through the per-`(project,run)` `mutate_run_json` lock;
  make check-then-act guards atomic claims so a double-click can't launch two runs/deployments.
- **LLM plumbing needs slack.** The agent writes whole files inline as JSON, so cap output high
  (`CODING_AGENT_MAX_TOKENS = 16384`; a low cap truncates mid-string into an unparseable
  action; huge files split across `write_file` + `append_file`). Pinned model ids go stale
  (defaults are doc-verified + env-overridable); `llm.chat` retries transient errors with
  backoff, never deterministic ones.
- **The agent can hallucinate completion, and one repair pass isn't enough.** Command
  verification (the real build) is the ground truth; cross-file errors surface in waves, so
  repair is an iterative loop that pre-reads erroring files and blocks `run_shell`.
- **Preview starts only the frontend** (`npm run dev`); the agent is told to bundle seed data
  so the preview renders populated, never a stuck "Loading…". And continuation "progress" =
  successful write ops, not unique paths — a polish pass overwrites files without adding new ones.
- **Windows filesystems lie about names.** Trailing dots/spaces are stripped (`".git."` opens
  the *real* `.git`), so `resolve_under` normalizes each component before the sensitive-name
  checks and screens reserved device names (`NUL/CON/PRN/AUX/COM1-9/LPT1-9`), which "write"
  against nothing. And names are case-insensitive: parallel patch tasks writing `src/App.ts` /
  `src/app.ts` hit ONE on-disk file — integration keys on `os.path.normcase`.
- **Redact before truncating; keep keys out of URLs.** A secret sliced mid-value by an early
  cap escapes exact-value redaction — apply the generous transport cap, then redact, then the
  small display cut. And a key in a request URL echoes into error messages: ride headers,
  redact stray `key=` queries defensively.
- **A verification-CONFIG repair can't pass its own tail.** The runner snapshots `TASK.md`
  at run start so an agent can never weaken its own gate mid-run — which also means a
  recovery child that *fixes the verification command* still gets re-verified against the
  stale snapshot. That snapshot is load-bearing; the fixed config takes effect on the next
  run or an endpoint-triggered re-verify (which reads live `TASK.md`).
