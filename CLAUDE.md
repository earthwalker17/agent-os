# CLAUDE.md

Operating guide for Claude Code and any other coding agent working on this
repository. This file is intentionally short, stable, and phase-independent.
For the system's shape (files, pipelines, invariants) read `ARCHITECTURE.md`;
for the project's history and status read `ROADMAP.md`; for long-term direction
read `BLUEPRINT.md`. The public `README.md` is a landing page, not a status file.

## 1. Project Mission

Agent OS is a lightweight **local-first project cockpit** — a small Agent
Operating System for managing multiple long-term projects through a single web
chat surface. It combines project-scoped conversations, structured markdown
memory, an orchestration layer, and a bounded execution layer that hands work
to a Coding Agent inside a sandboxed workspace, then delivers the result
through audited Git/GitHub and deployment contracts. The goal is not a
general-purpose assistant or a heavyweight agent platform; it is a clear,
controllable place for a single builder to plan, decide, and execute project
work.

## 2. Core Architecture Principles

- **Local-first.** Filesystem + SQLite + FastAPI + React. No cloud services,
  no queues, no external infra dependencies for the core.
- **Project isolation.** Each project has its own conversations, memory files,
  and execution workspace. Crossings are deliberate and bounded.
- **Structured markdown memory.** Important state lives in readable, editable
  `.md` files, not buried in chat history.
- **Main agent = brain.** Planner / memory steward / orchestrator. Does not
  edit code under `repo/`.
- **Coding Agent = hands.** Bounded executor inside the project's
  `execution_workspaces/{project_id}/repo/`. Does not edit project memory or
  other projects' workspaces.
- **All execution goes through `ProjectSandbox` + `ToolRuntime`.** No raw
  `os` / `pathlib` access to repo paths, no raw `subprocess`. Every tool call
  routes through `resolve_repo_path()` / `resolve_under()` or
  `validate_command()`.

## 3. Agent Roles

### Main agent (orchestrator)
**Does:** hold project conversations; load global + project memory and
assemble context; judge (via separate LLM calls) memory updates and delegation
intent; delegate execution to the Coding Agent; consume run summaries; read
specific repo files on demand through the bounded inspection channel; run
grant-gated web research.

**Does NOT:** edit code under any `repo/`; run shell commands; auto-inject
repo contents or diffs into its context; auto-dispatch a run from inferred
intent.

### Coding Agent (executor)
**Does:** run bounded JSON tool loops inside one project's workspace; edit
files under `repo/` via `ToolRuntime` (`list_files`, `read_file`,
`write_file`, `append_file`, `search_files`, `run_shell`); update the
per-project `TASK.md`; produce a concise `result.md` + `run.json` per run.

**Does NOT:** touch other projects' workspaces; edit project memory
(`projects/{id}/*.md`) or global memory (`memory/*.md`); bypass the sandbox;
run destructive shell commands (`rm -rf`, `git push --force`,
`git reset --hard`, …) — Git and Supabase CLIs are separate audited executors,
not agent tools.

## 4. Memory Policy

- **Global memory** lives in `memory/`: `USER.md`, `WORKSTYLE.md`, `SOUL.md`,
  `MEMORY.md`.
- **Project memory** lives in `projects/{project_id}/`: `PROJECT.md`,
  `STATUS.md` (carries the `## Task Queue` board — Completed / In Progress /
  Next), `DECISIONS.md`, `RESEARCH.md`, `LESSONS.md` (durable Main-Agent
  lessons; never written by the Coding Agent). `OPS.md` is the deployment
  ledger, written only by the deterministic `ops_ledger` — never by an LLM.
- `SOUL.md` is **read-only to every agent/LLM path**: loaded as the identity
  anchor every turn, never auto-written, never in any judge/reconciliation
  writeback allow-list. It is shown at the top of the Global Memory modal and
  editable **only by the user**, through the one explicit manual
  `/global-memory/update-file` endpoint — the sole SOUL.md write path.
- All other global/project memory files participate in **policy-filtered
  semantic writeback**: after each non-delegated turn a judge call proposes
  structured JSON updates (`{filename, section, content, action}`); the
  backend validates each against the writable-file set before touching disk.
  All writes go through the single atomic write path in `memory_engine.py`.
- Memory writes are **clean structured markdown**, not conversation dumps.
  Keep entries concise; this layer stays human-readable.

## 5. Execution Policy

- **Two trigger paths start a run, both explicit.** (a) `@code <task>`
  dispatches immediately. (b) A model-judged **pending execution plan**
  dispatches only when the user clicks "OK, run this" (the confirm endpoint).
  Nothing else dispatches runs; inferred intent never auto-runs code.
- **Implicit delegation is model-judged** (`judge_delegation`), with the
  rule-based detector as a fallback that never blocks chat and never triggers
  execution. Deterministic mode `@`-commands (`@plan`, `@design`, `@debug`,
  `@review`, `@inspect`, `@memory`) only shape the chat response.
- **Runs are background and observable.** Dispatch returns a `running` record
  immediately; finalization happens off-thread; crashes promote to `failed`.
  Artifacts live under `execution_workspaces/{id}/runs/{run_id}/`
  (`task_card.md`, `plan.json`, `events.jsonl`, `run.json`, `result.md`,
  `diff.patch`, screenshots). The main agent sees compact summaries by
  default — never full repo contents or raw event logs.
- **Verification is the ground truth for "done."** Command verification
  (manual `## Verification` block or inferred build/test) plus a bounded
  iterative repair loop gate `completed`. Browser verification is opt-in and
  **declarative**: the committed `## Browser Verification` block may declare
  views and interaction flows (fixed vocabulary, ≤2 flows × ≤10 steps,
  ≤6 views, same-origin only, credential-shaped fill values refused). A failed
  declared flow fails the verification; console/network evidence informs
  diagnosis but never flips status; the AI visual review stays diagnostic.
- **Recovery is typed and confirmable.** A non-green run (including a
  `completed` run whose visual verdict failed) gets a best-effort typed
  assessment (Recovery Matrix) surfaced as a confirmable plan with a bounded,
  redacted evidence block. **The only auto-dispatch path** is a user-granted
  recovery budget (≤2) set when explicitly confirming a plan — clamped per
  failure type, idempotent, fully linked and audited. Confirm-only failure
  types and environment failures never auto-dispatch; a crashed run never
  auto-recovers.
- **Post-run memory reconciliation is bounded**: a small judged call may
  update `STATUS.md` / `DECISIONS.md` / `RESEARCH.md` from the compact result;
  `PROJECT.md`, global memory, `SOUL.md`, and repo files are out of scope;
  reconciliation failure never fails the run.
- **Git is audited delivery, not shell.** All Git routes through the single
  executor `ToolRuntime.run_git` (not an agent tool; `run_shell` blocks Git).
  Pre-run checkpoint + post-run diff are best-effort and never auto-commit.
  Commit / push / PR / rollback are explicit two-phase preview→confirm
  contracts. Tokens reach git only via the push-time `GIT_ASKPASS` env —
  never argv, `.git/config`, logs, memory, or the UI.
- **External connectors are contract-first.** Vercel / Supabase / Stripe go
  through two-phase contracts, never raw API calls from inferred intent.
  `credentials.py` is the sole secret reader (presence-only status); secrets
  reach a provider only via header or exec-time env. Stripe is TEST-mode only
  by default. The orchestrator imports no connector.
- **Web research is grant-gated.** Network access exists only inside the
  bounded research channel, granted per-turn by an explicit `@search` /
  `@research` command (semantic intent only produces a suggestion chip).
  Fetches are SSRF-screened and domain-allowlisted (user-pasted URLs bypass
  only the allowlist); results are bounded cited extracts; a `redact()`-diff
  egress guard refuses outbound queries carrying credential-shaped values.
- **Skills are user-curated.** `skills/*.md` are committed markdown edited
  only through the UI by the user; a green run may PROPOSE a skill patch, but
  only an explicit user Apply writes it. Local RAG (`retrieve`) is bounded,
  cited, and filters credential-shaped files.

## 6. Context Hygiene

- **Never auto-inject repo contents or code diffs** into the main agent's
  context — token budgets and separation of concerns both forbid it.
- The main agent's default view of a run is the compact metadata (`status`,
  `task_title`, `summary`, `files_changed`, `commands_run`, `blockers`) plus
  the rendered `result.md`.
- Reading changed files into context is **on-demand only**, through the
  bounded read-only inspection channel (max 3 reads per turn), driven by a
  concrete reason: reviewing a change, debugging a regression, answering a
  question about a specific file.
- Keep crossings between "summaries + memory" (main agent) and "files in
  `repo/`" (Coding Agent) deliberate and bounded.

## 7. Working Style for Claude Code

- **Make small bounded changes.** Touch the files the task names; propose
  refactors separately.
- **Preserve existing behavior unless asked.** No silent renames, no surprise
  API changes, no drive-by cleanup of unrelated modules.
- **Follow the documentation policy** (`ROADMAP.md`): when a task lands,
  README gets a bump only if user-visible, ROADMAP gets the history entry,
  ARCHITECTURE is updated if a module/pipeline/invariant/lesson changed, and
  this file changes only when a constitutional rule changes. Never duplicate
  content across docs.
- **Add tests near the affected feature** (per-feature test files under
  `backend/tests/`, standalone-runnable, LLM stubbed) and **summarize what you
  ran and didn't run** at the end of every task.
- **Prefer simple local-first solutions.** ThreadPoolExecutor over Celery,
  SQLite over Postgres, polling over SSE — until there is a concrete reason.
- **Match the existing style** of the file you're editing: indentation, import
  order, naming, comment density.
- **Follow §3–§6 without exception.** The sandbox boundary, memory policy,
  execution policy, and context hygiene are constitutional, not negotiable
  per task.
