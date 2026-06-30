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
│  │                            result.md, visual_review.json,
│  │                            screenshots/browser.png (+ page-02.png …)
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
| `orchestrator.py` | The chat brain. Loads SOUL + memory, **compacts** project memory for the prompt (Phase 6.1 `_compact_memory` — STATUS/PROJECT/SOUL whole, archive sections tail-trimmed over a threshold), assembles context (incl. an optional **mode** block from an `@`-command or a routed intent), produces the reply, runs the bounded inspection loop, then the structured **memory-intake** judge (`judge_memory_intake → MemoryDecision`). |
| `memory_engine.py`| **Phase 6** — stdlib-only leaf module: the single markdown memory write path (`apply_update(base_dir, allow=…)` — policy-filtered, atomic, OSError-guarded, append-deduped, robust section replace), shared writable-file sets + `CANONICAL_SECTIONS` / `DEFAULT_SECTION`, the structured `MemoryDecision`, and idempotent `ensure_memory_scaffold`. Imported by both `orchestrator` and `execution.memory_reconciliation` (de-duped the old writers). **Phase 8** adds `OPS_WRITABLE` + scaffolds `OPS.md` (a `## Ledger` section) but deliberately keeps it OUT of `WRITABLE_PROJECT` / `RECONCILIATION_WRITABLE` / `DEFAULT_SECTION` — so only the deterministic `ops_ledger` writes it, never an LLM judge. |
| `llm.py`          | Thin LLM entry point: `chat(system, messages, model?, provider?) -> str` + `chat_vision(system, prompt, images, …)` for image input. Delegates to `providers.py` (07.1); shared transient-retry; no context assembly — callers own that. |
| `providers.py`    | **Provider Registry 2.0** — capability-aware model providers (Claude / GPT / Gemini / DeepSeek / Kimi / GLM). Key-presence availability (+ accepted env aliases, e.g. `ZAI_API_KEY` for GLM), default-provider preference (Claude first), a per-provider **model registry** with per-model `vision` flags, env-overridable default model + base URL, model validation (`is_known_model`), a `complete()` dispatcher, and a `complete_vision()` text+image dispatcher gated on the **selected model's** vision flag (`is_vision_capable`/`model_is_vision`/`default_vision_model`/`vision_available`/`default_vision_provider`). Anthropic via SDK; GPT/DeepSeek/Kimi/GLM via OpenAI-compatible `urllib` HTTPS; Gemini via `generateContent` (no new deps). |
| `database.py`     | SQLite persistence: conversations, messages, `pending_executions`. |
| `uploads.py`      | Task 07.0 — chat attachment storage: filename sanitization + allow-list, per-dir dedup, chat-only storage under `chat_uploads/{conv}/`, optional workspace copy via `ProjectSandbox`. HTTP-agnostic (takes bytes). |
| `credentials.py`  | **Phase 7 + Phase 8** — central credential accessor (the only reader of secret values). **Phase 8** generalized it to a provider registry (`github | vercel | supabase | stripe`) with typed secret + non-secret fields: generic `get_token` / `get_secret` / `get_metadata` / `status` / `status_all` / `set_credential` / `update_metadata` / `delete_credential` (the `*_github_*` helpers stay as aliases) + `get_env_value` (the sole app-env value reader). **Stripe live-gate** at the store boundary (non-`*_test_` key refused unless `allow_live`). `redact()` widened (`sk_`/`rk_`/`whsec_`/`sbp_` shapes + a Postgres connection-string password regex + `_all_known_tokens` over every provider + a `register_secret_source` hook for app-env values) — **no bare-JWT regex** (anon is a public JWT; `service_role` is redacted by exact value, classified by field name). Store under `credentials/` (gitignored). |

### Execution layer (`backend/execution/`)
| File                       | Purpose                                                                |
|----------------------------|------------------------------------------------------------------------|
| `manager.py`               | Workspace filesystem layout + idempotent init; `read/update_task_state`. |
| `models.py`                | Pydantic models: `RunRecord`, `RunStatus`, `TaskSpec`, `ResultSummary`, `VerificationResult` (+ `VerificationCommandResult`), `BrowserVerificationResult`; **Phase 5** `ExecutionPlan` / `ExecutionTask` / `TaskStatus`. |
| `templates.py`             | Default `AGENT.md` / `TASK.md` seeds (incl. the verification block docs). |
| `sandbox.py`               | `ProjectSandbox`: path + command validation. **The boundary.** Phase 7 `validate_git` + **Phase 8** `validate_supabase` (allow-list + `_is_destructive_supabase`, destructive-by-subcommand). |
| `tool_models.py`           | `ToolResult` + per-tool request models.                                 |
| `tool_runtime.py`          | `ToolRuntime`: the six sandboxed file/shell tools with output caps; Phase 7 `run_git` + **Phase 8** `run_supabase` (typed CLI executor with a **scrubbed allow-listed env** — Agent OS's own keys are withheld from the third-party CLI). |
| `prompts.py`               | Coding Agent system prompt + per-step / correction / **repair** prompts; **Phase 5** planning + per-task-unit prompts. |
| `planner.py`               | **Phase 5** — pure planning layer: `looks_complex` heuristic gate, tolerant `parse_plan`, `fallback_plan`, task-graph helpers (`topological_order` cycle-safe, `dependency_failed`, `aggregate_run_status`). |
| `run_store.py`             | Per-run artifact reader/writer; `render_result_md`; `sweep_stuck_runs`; **Phase 5** `write/read_plan_json` + result.md task section; **run control** `read_events` (timeline) + `read_task_card` (retry source). |
| `runner.py`                | `CodingAgentRunner`: **Phase 5 phased run** — plan phase → per-task execution loops → finalize, **verification + repair** orchestration. **Run control:** cooperative `cancel_event` checked at step boundaries → `_finalize_cancelled` (terminal `cancelled`, no verify/reconcile). **Live metrics:** `_persist_live_metrics` flushes observed files/commands (+ the active task's delta) to run.json/plan.json after each side-effecting tool result, so counts climb during a run; finalize still owns the authoritative lists. |
| `background.py`            | `BackgroundRunManager`: thread-pool dispatch; crash → `failed`; **run control** per-run cancel-`Event` registry (`request_cancel`) + `dispatch(..., retry_of=, recovery_of=, recovery_budget=, orchestration_round=)`. **Phase 6.1** `_maybe_auto_recover` (clean-finalize only) auto-dispatches ONE linked recovery run when a user-approved budget remains. |
| `chat_delegation.py`       | `@code` trigger handling + **Phase 6** deterministic mode `@`-commands (`parse_mode_command`: `@plan`/`@design`/`@debug`/`@review`/`@inspect`/`@memory` — shape the response, never dispatch). |
| `delegation_intent.py`     | Heuristic implicit-delegation detector (fallback only).                 |
| `delegation_judge.py`      | LLM semantic delegation judge (the primary classifier) + **Phase 6** richer `intent` label (informational; routing still keys off the 3 decisions). |
| `pending_execution.py`     | Confirmable execution plans (store / render / revise).                  |
| `memory_reconciliation.py` | Post-run bounded memory reconciliation judge (writes via `memory_engine`). |
| `recovery.py`              | **Phase 6** — best-effort `assess_run`: interprets a non-green terminal run and recommends one bounded next step (`RecoveryAssessment` on the record). Confirmable handoff only — never auto-dispatches. |
| `inspect.py`               | Main-agent on-demand, read-only file inspection (06.1).                 |
| `verification.py`          | Command verification: parse, **infer** (`plan_verification`), run specs, render. |
| `browser_verification.py`  | Browser verification lifecycle (dev server → HTTP readiness → **render-readiness-gated, multi-page** Playwright capture → teardown) + UI flow. Captures the entry URL plus a few discovered views (`BrowserPageCapture[]`); a legacy single-capture adapter keeps the `screenshots/browser.png` contract. |
| `visual_judge.py`          | AI visual judgment over the captured screenshots (`run_visual_review`): vision-model verdict (`passed`/`warning`/`failed`/`inconclusive`) + concise rationale/evidence, persisted as `visual_review.json`. Resolves a vision-capable `(provider, model)` — preferring the user's **selected** one, else any available vision model — and **skips gracefully** when none is configured. **Diagnostic-only**, best-effort. |
| `preview.py`               | Managed long-lived preview dev servers (one per project).               |
| `git_ops.py`               | **Phase 7** — sandboxed local Git ops, all via `ToolRuntime.run_git`: `ensure_repo` (lazy `git init` + safe `.gitignore` + identity + initial commit), `git_status`, `current_branch`, `create_checkpoint` (out-of-branch snapshot via `write-tree`/`commit-tree` on a throwaway index → tagged), `capture_diff` (redacted + bounded, includes new files), `commit` (secret-refusing stage), `partition_changes`, `create_branch`, `rollback` (gated). Never raises into the run loop. |
| `github_connector.py`      | **Phase 7** — GitHub via REST/`urllib` (no `gh` CLI, no new deps): `status` (validate token via `GET /user`), `ensure_remote` (tokenless URL), `push_branch` (token injected ONLY via `GIT_ASKPASS` env — never argv/`.git/config`), `create_pull_request` (`POST /pulls`). Output is redacted; the token never enters a logged surface. |
| `_git_askpass.sh`          | Runtime-generated (gitignored, under `credentials/`) `GIT_ASKPASS` helper — echoes the token from an env var the connector sets per-push. Contains no secret; keeps the token out of argv and `.git/config`. |
| `app_env.py`               | **Phase 8** — project-scoped app-env registry (`credentials/env/{id}.json`): the BUILT app's env vars (DATABASE_URL / NEXT_PUBLIC_* / STRIPE_*), DISTINCT from connector tokens. Presence-only `set/delete/list_env` (never a value); the single value reader is `credentials.get_env_value`; secret values register into the redactor. |
| `vercel_connector.py`      | **Phase 8** — Vercel REST-over-`urllib` (no CLI for deploy/redeploy/rollback/env): `status` (whoami), `create_deployment` (gitSource), `get_deployment` (poll `readyState`), `list_deployments`, `promote_deployment` (rollback), `set_env_var`. Token in the `Authorization` header ONLY; every returned string `credentials.redact`-ed with `project_id`; `normalize_url` strips a protection-bypass query; never auto-creates a project. |
| `ops_ledger.py`            | **Phase 8** — the ONE writer of `projects/{id}/OPS.md` (`append_ops_entry`): deterministic, ids/URLs/key-NAMES only, redacted at the call site, idempotent on a `dedup_key`; also appends a redacted line to `execution_workspaces/{id}/ops/events.jsonl`. OPS.md is scaffolded but excluded from every LLM-judge writable set. |
| `supabase_connector.py`    | **Phase 8** — Supabase: Management REST (`status`, Bearer `sbp_`) + sandboxed CLI (`link` / `migration_preview` = `db push --dry-run`, Docker-optional / `migration_diff` = best-effort Docker-gated / `migration_apply` = `db push --linked`, `allow_destructive`). Secrets ride `run_supabase` `env_extra` only; the connector **redacts CLI stdout/stderr before any artifact** (H7); Docker-not-running → a clear blocker. |
| `stripe_connector.py`      | **Phase 8** — Stripe test-mode: REST over urllib but **form-encoded** (`x-www-form-urlencoded` + bracket notation; NOT JSON) with an `Idempotency-Key` on creates. **Per-request test gate** (non-`*_test_` key / `livemode:true` response refused). `provision_price` (Product+Price → `price_id`), `register_webhook` (GET-then-create; the returned `whsec_` is stored via `credentials` + **never** echoed), `delete_webhook`, `status`, `local_webhook_command`. The built app's checkout-session-create + signature-verify are app-runtime (reads its own `process.env`). |

`tests/` mirrors these per feature; all stub `llm.chat` so no API key is needed.

---

## 6. Frontend modules (`frontend/src/`)

| File                         | Purpose                                                              |
|------------------------------|---------------------------------------------------------------------|
| `main.tsx` / `App.tsx`       | Entry + three-column layout and all top-level state (projects, conversations, messages, context, modals, model provider, color theme). Theme (07.2) is applied via `data-theme` on `<html>` and persisted to `localStorage`. |
| `types.ts`                   | Shared TS types mirroring backend models (+ `ModelInfo` / `ProviderInfo.models` for the model picker). |
| `components/ProjectList.tsx` | Left column: projects + conversations + create/rename/delete.       |
| `components/ChatPanel.tsx`   | Center column: header (provider selector top-left 07.1, theme selector top-right 07.2) + message thread + the **multi-modal composer** (07.0 — auto-growing textarea, `Ctrl/Cmd+Enter` send, `+` file upload with chips + "add to workspace too", Web Speech voice button); renders `RunChatCard` on messages carrying a `run_id` and attachment chips on messages carrying `metadata.attachments`. **Provider Registry 2.0:** a compact upward-opening `ModelPicker` sits above the composer; chat image upload is gated by the selected model's `vision` flag (text-only models drop/refuse images with a clear note). |
| `components/ModelPicker.tsx` | Provider Registry 2.0 — compact, upward-opening per-provider model dropdown (right of the composer). Trigger shows the selected model + a vision/text cue; the popover lists the provider's models, each tagged image-capable or text-only. |
| `components/ContextPanel.tsx`| Right column: project memory files (editable) + `RunsSection`.       |
| `components/RunsSection.tsx` | Runs list (auto-polls while active; **live files/cmds counts**) + Start/Stop **preview** control + per-row **Trace** button. |
| `components/RunChatCard.tsx` | The in-chat run lifecycle: **live phase badge + multi-task checklist** → build progress → verification phases → completion summary → **Run browser verification** → live preview URL + **multi-page screenshot gallery** (lightbox w/ prev-next) + **AI visual-judgment verdict** (reachable / captured / judged shown as distinct signals); **Cancel** (active) / **Retry** (terminal) / **Live trace** controls. |
| `components/RunDetailModal.tsx` | Detailed run inspection: per-command verification, browser status (+ per-page screenshot list + readiness), **Visual Review** block, **Plan & Tasks**, **event Timeline** (settled milestones, polls while active), `result.md`; **Cancel / Retry / Open live trace** controls. |
| `components/RunTimeline.tsx` | Read-only **settled milestone** timeline: collapses each logical step's start/settle event pair (plan / each task / verification / each repair attempt / browser) into one row showing its terminal status; a `runActive` prop settles any dangling "running" once the run is terminal. |
| `components/RunTrace.tsx` | **Live Trace modal** — a lightweight vertical chronological thread of all run activity (planning, every file read/write/append/search, shell command, task start/finish, verification, repair, browser, cancel/retry). Pairs `tool_call`+`tool_result` into one row; drops raw `llm_response` (no chain-of-thought). Polls `…/events?since=` + run.json while active, auto-scrolls; replayable after the run. |
| `components/runEventUtils.ts` | Shared event-rendering helpers (`kindFor` status→palette, `str`, `clockTime`) used by `RunTimeline` + `RunTrace`. |
| `components/EditModal.tsx` / `ConfirmDialog.tsx` / `GlobalMemoryModal.tsx` | Memory editing, confirmations, global-memory viewer. |
| `components/ConnectorsModal.tsx` (08) | Multi-provider connector setup (Vercel / Supabase / Stripe) over the generic `/credentials/{provider}` routes; write-only inputs, presence-only display, Stripe TEST badge + `allow_live` opt-in. (GitHub keeps its validating `ConnectorModal.tsx`.) |
| `components/EnvRegistryPanel.tsx` (08) | Project app-env registry in `ContextPanel` — write-only values, presence list, per-key "Push to Vercel" (the env-set contract). |
| `components/ExternalActionPanel.tsx` (08) | Generic two-phase External-Action contract preview block (external/destructive/TEST tags + confirm gate); shared driver for the deploy panel. |
| `components/DeployOpsPanel.tsx` (08) | Production Path controls in the run chat card — deploy / redeploy / rollback contracts (preview→confirm), last-deploy URL/state badges, polls while a deploy is in flight. |
| `components/MigrationPanel.tsx` (08) | Supabase controls in the run chat card — link + apply-migrations contracts (preview shows the `db push --dry-run` pending list + best-effort SQL diff; confirm applies to the linked DB). |
| `components/StripePanel.tsx` (08) | Stripe TEST-mode controls in the run chat card — provision a test Product+Price (→ `price_id`), register the deployed webhook endpoint (signing secret stored, never shown), and the `stripe listen` local-test command. |

---

## 7. Key pipelines

### A. Chat turn (non-`@code`) — `orchestrator.orchestrate()`
1. Load `SOUL.md` + global + project memory; assemble context (Phase 6: plus an
   optional `@`-command **mode** block — `@plan`/`@debug`/`@inspect`/… — that
   shapes the response without dispatching).
2. **Intent Router v2.** An explicit mode `@`-command sets the mode + skips the
   judge. Otherwise the **delegation judge** (`delegation_judge`) classifies the
   message (`dispatch_suggested` / `discussion` / `memory_only`) and emits a
   richer `intent` label. On `dispatch_suggested` → create a pending plan (no
   run). On failure → fall back to the `delegation_intent` heuristic. **Phase
   6.1:** a non-dispatch judged intent also routes to the matching
   `orchestrate(mode=…)` (`_INTENT_TO_MODE`) so the richer labels shape the
   response, not just a UI badge.
3. **Main response** LLM call. May emit `{"inspect_request": …}` to read a repo
   file via `inspect.py` (max 3/turn), each result fed back before the next.
4. **Memory-intake judge** (Phase 6: `judge_memory_intake → MemoryDecision`,
   one structured reasoned object) proposes updates; the backend policy-filters
   them (SOUL + non-writable files excluded) and writes them atomically via
   `memory_engine`. The decision's `reason` + the turn `intent` ride on the
   assistant message metadata (UI chip + badge, survive reload).

→ Up to 3–4 LLM calls per turn.

**Provider routing (07.1 + Provider Registry 2.0).** The chat request carries a
`provider` id (`claude`/`gpt`/`gemini`/`deepseek`/`kimi`/`glm`) and an optional
`model`; the endpoint validates both (unknown/unavailable provider or unknown
provider/model combo → 400) and the **main response** (step 3) routes to them via
`orchestrate(..., provider=, model=)`. Internal subsystem calls (judge,
delegation, Coding Agent) use the default provider + its default model.
Availability is key-presence; see `providers.py`.

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
   order (`MAX_TASK_STEPS = 16` each), skips tasks whose dependency failed
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
8. **Recovery assessment** (Phase 6) — `recovery.assess_run`, best-effort, only
   for a non-green outcome; persists `recovery_assessment` for the Main Agent /
   UI. Never auto-dispatches.
9. Write `run.json` + `plan.json` + `result.md`; return `ResultSummary`.

Steps 5–8 are **best-effort: an exception there never fails finalization.** The
plan phase is read-only and never fails the run — it falls back to a single task.
Events now carry a `phase` tag (`planning`/`execution`/`repair`) plus
`plan_started`/`plan_ready`/`plan_failed`/`task_started`/`task_status`.

### D. Command verification + repair — `verification.py` + `runner._verify_with_repair`
1. `plan_verification()`: manual `## Verification` block wins (multi-line);
   else **infer** from repo — `npm install`(+build), `pytest` *iff importable*
   else `compileall` syntax check; else `skipped`.
2. Write `verification_state = "verifying"`; run specs in order, **stop at first
   failure**.
3. If a `completed` **or `partial`** run that produced files failed verification
   → `verification_state = "repairing"`, run a **bounded iterative repair loop**
   (`MAX_REPAIR_ATTEMPTS`): each pass pre-reads the files named in the errors into
   the prompt, re-edits them (`run_shell` is blocked during repair), then
   **re-verifies**; repeat until green, a pass changes nothing, or the cap is hit.
4. Pass → keep status; still failing → a `completed` run downgrades to `partial`,
   and either way a `verification failed:` blocker is recorded. Clear
   `verification_state`.

### E. Browser preview + visual review — `browser_verification.py` + `visual_judge.py` + `preview.py`
User clicks **Run browser verification** → `POST …/browser-verify`:
`npm install` → dev server (port 5174) → poll URL (HTTP reachability) →
**render-readiness-gated, multi-page Playwright capture** (the entry page plus a
few discovered views: tabs / route links / nav buttons; each captured only after
the DOM has populated and settled, never on a `"Loading…"` spinner; on a
readiness timeout it's captured anyway and marked `unconfirmed`). On pass, the
still-running server is **handed off to `preview.py`** so the URL stays live
(Start/Stop from the Runs panel; torn down on backend shutdown). Then — in the
**same request** — `visual_judge.run_visual_review` sends the screenshots + task
context to a vision model for a structured verdict; the UI forwards the user's
**selected provider/model** so the judge prefers a vision-capable selection
(falling back to any available vision model). It's **diagnostic-only**
(never changes run status) and **skips gracefully** without a vision-capable model.
Screenshots are served by name via `GET …/runs/{id}/screenshot?name=`; the
verdict persists on `RunRecord.visual_review` + `visual_review.json`. The runner's
own configured-block path (pipeline C step 6) runs the same capture + review.

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
`events.jsonl` (optional `since=<index>` cursor → `{events: tail, total}` for
cheap incremental polling; no `since` returns all, the legacy shape). The run
detail modal renders a **settled** `RunTimeline` (start/settle pairs collapsed to
one row per logical step) and polls it (with run.json) while the run is active;
a dedicated **Live Trace modal** (`RunTrace`) renders the granular chronological
thread (every file op + command, with `tool_call`+`tool_result` paired and raw
`llm_response` dropped — no chain-of-thought) and polls with the `since` cursor.
The chat card derives a live **phase badge** + **multi-task checklist** straight
from `run.json` (no extra fetch). **Progressive metrics:** the runner rewrites
run.json after each side-effecting tool result, so the Runs panel + chat card
files/cmds counts climb live instead of only at finalize.
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

### H. Project Ops — Git/GitHub delivery (Phase 7) — `git_ops` + `github_connector`
Turns a finished run into an audited, user-approved Git/GitHub delivery. **One
Git executor:** every command goes through `ToolRuntime.run_git` (`shell=False`
argv, `ProjectSandbox.validate_git` allow-list + destructive gating); the agent's
`run_shell` still blocks `git push` + destructive Git and never reaches `run_git`.
**Checkpoint (dispatch):** `background.dispatch` best-effort `git_ops.ensure_repo`
+ `create_checkpoint` stamps `pre_run_checkpoint`/`base_commit`/`checkpoint_tag`/
`branch` on the record before the first `write_run_json`; a recovery child inherits
the parent's anchor. **Diff (finalize):** `runner._finalize` best-effort
`capture_diff` against the checkpoint → redacted, bounded `diff.patch` artifact +
`diff_stat`/`head_commit` on the record (never auto-commits). **Delivery
(explicit, two-phase):** `POST …/runs/{id}/git/commit|git/push|github/pr|git/rollback`
each return an **External Action Contract** on `confirm:false` and execute only on
`confirm:true`; `GET …/runs/{id}/diff` serves the patch on demand,
`GET …/projects/{id}/git/status` the live tree, `…/credentials/github` +
`…/github/connector` the (presence-only) token/connection state. Push/PR are
external, rollback destructive — all gated; commit refuses secret-looking files;
the token reaches git only via `GIT_ASKPASS` env. Frontend: `GitOpsPanel` (in the
run chat card + detail modal) drives the contracts; `ConnectorModal` (from the Runs
panel) enters the token; the brain sees only a compact `_latest_git_state_context`
in `@review`/`@debug` (never the raw diff).

---

### I. Production Path — Vercel deploy (Phase 8) — `vercel_connector` + `ops_ledger`
Extends Project Ops from "delivered to GitHub" to "deployed". Same contract
shape as §H. **Connectors** (`credentials.py` provider registry) hold Vercel /
Supabase / Stripe tokens with the GitHub no-leak guarantees; an **app-env
registry** (`app_env.py`) holds the built app's env vars (presence-only; the one
value reader is `credentials.get_env_value`). **Deploy/redeploy/rollback** are
run-scoped two-phase contracts (`POST …/runs/{id}/vercel/deploy|redeploy|rollback`,
`confirm:false`→preview / `confirm:true`→execute); **env-set/status/deployments**
are project-scoped; every mutating route rejects GENERAL. A deploy is **async**:
confirm stamps a transient `deploy_state`, returns immediately, and finalizes
off-thread (`BackgroundRunManager.submit` polls Vercel to `READY`) → stamps
`deployment_url`/`id`/`target`, writes `deployment.json` + redacted `deploy.log`,
re-renders `result.md`, and appends a redacted `OPS.md` ledger entry. Secrets
reach Vercel only via the `Authorization` header / an env push value read once at
action time — never a contract / event / log / artifact / the UI. **Supabase**
(`supabase_connector` + `run_supabase`) adds migration/link contracts (the apply
is `db push --linked`, destructive, confirmed); **Stripe** (`stripe_connector`,
form-encoded + test-gated) adds checkout-provision + webhook-register contracts.
The minimal golden path — connect → build → verify → commit → push → deploy →
migrate → test checkout/webhook — is code-complete. **Startup reconciliation
(8.7):** `reconcile_stuck_external_actions` clears a transient `deploy_state`/
`external_state` left by a crash, querying the provider for the true state and
recording a "verify remote state" blocker rather than auto-retrying.

## 8. Core data model — `RunRecord` (serialized as `run.json`)

`run_id`, `project_id`, `task_title`, `status` (`running`/`completed`/
`partial`/`blocked`/`failed`/**`cancelled`**), `summary`, `files_changed`,
`commands_run`, `blockers`; verification fields (`verification`,
`verification_state`), browser fields (`browser_verification` — now carrying a
`pages: BrowserPageCapture[]` manifest + a `readiness` outcome — plus
`browser_verification_state`), the diagnostic-only `visual_review`
(`VisualReviewResult`, also written standalone as `visual_review.json`),
memory-reconciliation fields, the **Phase 5**
`plan` field (an `ExecutionPlan` of `ExecutionTask` units, also written
standalone as `plan.json`), and **run-control** fields `cancel_requested`
(transient — set while a cancel is pending, status still `running`) +
`retry_of` / `retried_by` (links a retry to its origin). `cancelled` is a
terminal status the runner sets directly on user cancel — never agent-settable,
and excluded from memory reconciliation. **Phase 6** adds `recovery_assessment`
(a `RecoveryAssessment`: verdict / diagnosis / recommended_action /
follow_up_task_card) — populated by `recovery.assess_run` for non-green runs,
`None` for green/older ones. **Phase 6.1** adds `memory_reconciliation_reason`
(audit) and the recovery-budget lineage `recovery_budget` / `recovery_of` /
`recovered_by` / `orchestration_round` (`recovered_by` set once → at most one
recovery per parent). `VerificationResult` carries `mode`
(`manual`/`inferred`/`skipped`), a `commands[]` breakdown, and `repair_attempts`;
its legacy top-level `command`/`exit_code`/`output_preview` mirror the aggregate.
**Phase 7** adds the Project Ops linkage (all `Optional`, defaulted so old records
round-trip): `pre_run_checkpoint` / `checkpoint_tag` / `base_commit` (the rollback
anchor), `head_commit`, `branch`, `commit_sha`, `pushed`, `pr_url` / `pr_number`,
`diff_stat` (compact — the raw diff stays in `diff.patch`), and the transient
`git_state` sub-status (`checkpointing`/`committing`/`pushing`/`opening_pr`/
`rolling_back`; mirrors `verification_state`, never a `RunStatus` value).
`ResultSummary` gains compact `commit_sha`/`branch`/`pr_url`/`diff_stat` (metadata
only). **Phase 8** adds run-scoped deploy linkage (all `Optional`/defaulted):
`deployment_id` / `deployment_url` (normalized, bypass-token stripped) /
`deployment_target`, plus the transient sub-statuses `deploy_state` /
`external_state` (mirror `git_state`; never a `RunStatus`). `ResultSummary` gains
`deployment_url`/`deployment_target`. Project-level provisioning facts
(Stripe/Supabase ids, env key names) live in the `OPS.md` ledger, NOT on the
record. **No secret ever appears on either model.**

Status semantics: **`completed` only after verification passes (or safe skip);**
files-written-but-verification-failed is `partial`; `skipped` is acceptable only
when nothing safe can run. `files_changed` / `commands_run` update
**progressively** during a run (the runner rewrites run.json — atomically — as
new files/commands are observed) and are overwritten with the authoritative
lists at finalize, so a poll mid-run sees live counts without changing the final
values.

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
  **Phase 6.1 scoped extension:** a user-approved **recovery budget** set when
  *explicitly confirming* an execution contract authorizes up to N (≤2) bounded,
  linked, audited auto-recovery runs. This is the only auto-dispatch path; it is
  gated on the user's explicit prior approval (clamped at the confirm endpoint),
  capped, idempotent (`recovered_by`, one recovery per parent), and never fires
  from inferred intent or from a crash path.
- **Best-effort post-run steps.** Verification / browser / reconciliation /
  checkpoint / diff capture never crash finalization and never get a run stuck in
  `running`.
- **No auto-injection of repo contents** into the main agent's context.
- **Git is audited delivery, not shell (Phase 7).** All Git routes through the
  single executor `ToolRuntime.run_git` (`validate_git` + destructive gating); no
  raw `subprocess`/`os`/GitPython on repo paths, and `run_git` is not an agent
  tool. Commit/push/PR/rollback are explicit two-phase confirm contracts — **no
  inferred-intent Git, no Git auto-dispatch.** Credentials reach git only via the
  `GIT_ASKPASS` subprocess env (tokenless remote) and never touch argv,
  `.git/config`, commits, logs, events, memory, prompts, or the UI; `credentials.py`
  is the sole secret reader and `status` is presence-only.
- **External connectors are contract-first + secret-clean (Phase 8).** Vercel /
  Supabase / Stripe go through the same two-phase preview→confirm contracts and
  the single secret reader (`credentials.py`); a token reaches a connector only
  via an `Authorization` header or an exec-time env, never argv/logs/artifacts/UI;
  every returned string is redacted with the `project_id`. No inferred-intent
  deploy/migration/payment; `orchestrator.py` imports no connector/executor
  (asserted by a test). Stripe stays TEST-only behind a per-request executor gate.
  `OPS.md` is written only by the deterministic `ops_ledger`, never by an LLM.

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
- **`subprocess(text=True)` decodes with the machine codec, not UTF-8.** On a
  non-UTF-8 Windows box (e.g. cp936/GBK, cp1252) capturing npm/Vite's UTF-8
  output (box-drawing, ✓, ➜, emoji) raises `UnicodeDecodeError` — silently losing
  captured output, or killing a `_StreamDrainer` reader thread and re-introducing
  the pipe-deadlock. Always pass `encoding="utf-8", errors="replace"` to every
  `subprocess.run`/`Popen` that captures child output.
- **A timeout/teardown kills only the direct child.** With `shell=True` the
  child is `cmd.exe`; `npm`/`node` grandchildren survive and orphan, holding ports
  (5173/5174) and file locks. Reap the whole tree: `taskkill /F /T /PID` on
  Windows (`run_shell`, dev-server teardown, installer), `killpg` on POSIX.
- **Non-atomic JSON writes tear under polling.** The runner rewrites
  `run.json`/`plan.json` many times per run while the UI polls every 2 s;
  truncate-then-write leaves a window where a reader sees an empty/partial file
  (transient 404/flicker). Write to a `.tmp` sibling then `os.replace` (atomic on
  the same NTFS volume) — see `run_store._atomic_write_text`.
- **The agent writes whole files inline as JSON, so cap output high.** A
  `write_file` of a real component overflows a 2048-token default and truncates
  the JSON mid-string → the action won't parse → the task fails. A rich seed-data
  module is bigger still and failed every time at 8192, so the Coding Agent loop
  uses `CODING_AGENT_MAX_TOKENS = 16384` and the prompt tells the agent to split a
  very large file across `write_file` + `append_file`.
- **Pinned model ids go stale.** `claude-sonnet-4-20250514` now 404s; a dead
  default model breaks every LLM call. Provider Registry 2.0 refreshed every
  provider's default to a current API-available id (Claude → `claude-opus-4-8`),
  each overridable via env (`AGENT_OS_{CLAUDE,OPENAI,GEMINI,DEEPSEEK,KIMI,GLM}_MODEL`).
  Re-verify model ids against official docs when bumping — don't trust memory.
- **A transient LLM blip must not kill a task.** One mid-run "Connection error"
  used to fail a task and cascade its dependents to `skipped`. `llm.chat` retries
  transient errors (connection/timeout/429/5xx) with backoff; deterministic ones
  (auth/4xx) are not retried.
- **The agent can hallucinate completion.** It once finalized a scaffold task as
  `completed` after writing only `package.json`, fabricating the rest in
  `files_changed`. Command verification (the real `npm run build`) is the ground
  truth that catches this; the prompt also forbids claiming unwritten files.
- **One repair pass isn't enough for a real build.** Multi-file builds shed
  type/import errors in waves; repair is now an iterative loop that pre-reads the
  erroring files and blocks `run_shell` (an unguarded repair agent spent every
  step re-running `tsc` and wrote no fix). It recovered even a hallucinated,
  scaffold-missing repo to a green build.
- **Preview starts only the frontend.** Browser verification runs `npm run dev`
  alone — an app fetching from a separately-launched backend screenshots as a
  stuck "Loading…". The agent is told to bundle seed data in the frontend so the
  preview renders populated.
- **"Progress" for a continuation = write ops, not unique paths.** A polish pass
  that overwrites existing files makes progress without adding new paths; the
  budget-extension check counts successful writes, not deduplicated path count.

---

## 11. Starting a future session

1. Read `CLAUDE.md`, this file, then the latest Phase 5 entries in `ROADMAP.md`.
2. Match the existing per-feature module + per-feature test-file convention.
3. Keep changes small and bounded to the files the task names; propose refactors
   separately.
4. When done: run the relevant `backend/tests/<file>.py`, note what you ran (and
   didn't), and update the right doc(s) per the policy table in `ROADMAP.md`.
