# CLAUDE.md

Operating guide for Claude Code and any other coding agent working on this
repository. This file is intentionally short and phase-independent. For the
system's shape (files, pipelines, invariants) read `ARCHITECTURE.md`; for the
current project status, task log, and roadmap read `ROADMAP.md`. The public
README is deliberately kept brief and should not be used as a status file.

## 1. Project Mission

Agent OS is a lightweight **local-first project cockpit** — a small Agent
Operating System for managing multiple long-term projects through a single
web chat surface. It combines project-scoped conversations, structured
markdown memory, an orchestration layer, and a bounded execution layer that
can hand work off to a Coding Agent inside a sandboxed workspace. The goal
is not to build a general-purpose assistant or a heavyweight agent platform;
it is to give a single builder a clear, controllable place to plan, decide,
and execute project work.

## 2. Core Architecture Principles

- **Local-first.** Filesystem + SQLite + FastAPI + React. No cloud services,
  no queues, no external infra dependencies for the MVP.
- **Project isolation.** Each project has its own conversations, memory
  files, and execution workspace. Crossings are deliberate and bounded.
- **Structured markdown memory.** Important state lives in readable,
  editable `.md` files, not buried in chat history.
- **Main agent = brain.** Planner / memory steward / orchestrator. Does
  not edit code under `repo/`.
- **Coding Agent = hands.** Bounded executor inside the project's
  `execution_workspaces/{project_id}/repo/`. Does not edit project memory
  or other projects' workspaces.
- **All execution goes through `ProjectSandbox` + `ToolRuntime`.** No raw
  `os` / `pathlib` access to repo paths, no raw `subprocess`. Every tool
  call routes through `resolve_repo_path()` or `validate_command()`.

## 3. Agent Roles

### Main agent (orchestrator)
**Does:**
- Holds project conversations and produces planning / explanation /
  recommendation responses.
- Loads global + project memory and assembles context.
- Decides through a separate semantic-judge call whether memory files
  should be updated.
- Delegates execution work to the Coding Agent (today: only via the
  explicit `@code` trigger).
- Consumes Coding Agent run summaries when reasoning about progress.

**Does NOT:**
- Edit code under any project's `repo/`.
- Run shell commands directly.
- Auto-inject repo contents or diffs into its context (see §6).
- Auto-dispatch a Coding Agent run from inferred intent.

### Coding Agent (executor)
**Does:**
- Runs bounded JSON tool loops inside one project's execution workspace.
- Edits files under `repo/` via `ToolRuntime` (`list_files`, `read_file`,
  `write_file`, `append_file`, `search_files`, `run_shell`).
- Updates the per-project `TASK.md` and produces a concise `result.md` +
  `run.json` per run.

**Does NOT:**
- Touch other projects' workspaces.
- Edit project memory (`projects/{id}/*.md`) or global memory (`memory/*.md`).
- Bypass the sandbox: no `os`, `pathlib`, or `subprocess` direct calls
  against repo paths.
- Run destructive shell commands (`rm -rf`, `git push --force`,
  `git reset --hard`, etc.) without explicit confirmation.

## 4. Memory Policy

- **Global memory** lives in `memory/`: `USER.md`, `WORKSTYLE.md`,
  `SOUL.md`, `MEMORY.md`.
- **Project memory** lives in `projects/{project_id}/`: `PROJECT.md`,
  `STATUS.md`, `TASK_QUEUE.md`, `DECISIONS.md`, `RESEARCH.md`.
- `SOUL.md` is **read-only and hidden**. It is loaded as the identity
  anchor on every turn and is never shown in any UI, never auto-written,
  never included in any write path.
- All other global / project memory files participate in **policy-filtered
  semantic memory writeback**: after each non-delegated chat turn, a second
  LLM call examines the latest exchange + current memory and proposes
  structured JSON updates (`{filename, section, content, action}`). The
  backend validates each proposal against the appropriate writable-file set
  before touching disk.
- Memory writes are **clean structured markdown**, not raw conversation
  text or log dumps. Keep entries concise; this layer is meant to stay
  readable by humans.

## 5. Execution Policy

- **Two trigger paths actually start a Coding Agent run, both explicit.**
  (a) `@code <task>` in a project chat dispatches a run immediately.
  (b) An inferred-intent **pending execution plan** dispatches only when
  the user clicks "OK, run this" on the chat bubble — which calls
  `POST /api/projects/{id}/execution/pending/{pid}/confirm` and routes
  through the same `BackgroundRunManager.dispatch()` as `@code`.
  Nothing else dispatches runs.
- **Implicit delegation is model-judged.** Each non-`@code` project-chat
  message goes through `judge_delegation()` — a small Claude call that
  classifies the message into `dispatch_suggested` / `discussion` /
  `memory_only` using recent conversation + a compact project-memory
  snapshot. A `dispatch_suggested` decision creates a `pending_executions`
  row holding the display plan + full task card; the assistant message
  is a project-manager-tone summary with **OK, run this** / **Revise
  plan** buttons. **No auto-dispatch ever occurs from inferred intent.**
- **Revising a pending plan** runs a separate small LLM call that
  rewrites display_plan + task_card in place (status stays `pending`);
  the user must still click OK to dispatch. Revision failures fall back
  to appending the instruction verbatim; revising a dispatched plan is
  rejected.
- **Heuristic fallback.** The 05.8 rule-based detector
  (`delegation_intent.py`) remains as a safe fallback. If the judge call
  fails (network, malformed JSON, invalid decision value), the chat
  endpoint falls back to the heuristic — never blocks chat, never
  triggers execution.
- **Coding Agent runs in `execution_workspaces/{project_id}/repo/`.** Run
  artifacts live under `execution_workspaces/{project_id}/runs/{run_id}/`
  (`task_card.md`, `events.jsonl`, `run.json`, `result.md`).
- **Runs are background.** Both `@code` and `POST /api/projects/{id}/execution/runs`
  return an initial `RunRecord` (status=`running`) immediately; finalization
  happens in a worker thread. Crashes promote to `failed` with the error
  captured as a blocker.
- **Main agent sees run summaries by default.** Not full repo contents,
  not full diffs, not raw event logs.
- **Post-run memory reconciliation is bounded.** When a run reaches a
  terminal state (`completed` / `partial` / `blocked` / `failed`), a
  small model-judged call may update `STATUS.md` / `TASK_QUEUE.md` /
  `DECISIONS.md` / `RESEARCH.md` from the compact `ResultSummary` +
  rendered `result.md`. `PROJECT.md`, global memory, `SOUL.md`, and repo
  files are out of scope. Read-only inspection runs and noisy failures
  skip the LLM call. Each run reconciles at most once.
  Reconciliation failure NEVER fails the run.
- **Phase 6 — mode commands + confirmable recovery.** Besides `@code`, the
  main agent understands deterministic mode `@`-commands (`@plan`, `@design`,
  `@debug`, `@review`, `@inspect`, `@memory`) that only **shape the chat
  response** — they never dispatch a run. When a run ends non-green (partial /
  failed / blocked, or a failed verification / visual review), a best-effort
  `recovery.assess_run` proposes one bounded next step; a `needs_recovery`
  assessment becomes a **confirmable pending plan** (the user still clicks
  "OK, run this"). **Phase 6.1 scoped exception:** when the user *explicitly
  confirms* an execution contract they may grant a bounded **recovery budget**
  (none / 1 / 2); a non-green run with remaining budget auto-dispatches that many
  bounded, linked, audited recovery runs (hard cap 2, idempotent, clamped at the
  confirm endpoint). This is the ONLY auto-dispatch path and is authorized by the
  user's explicit prior approval — inferred intent still never runs code, and a
  crashed run never auto-recovers.
- **Phase 7 — Project Ops (Git/GitHub).** Git is audited delivery, not raw shell:
  all Git routes through the one executor `ToolRuntime.run_git` (not an agent tool;
  `run_shell` still blocks `git push` + destructive Git). Pre-run checkpoint + post-run
  diff are best-effort, never auto-commit/push. Commit / push / PR / rollback are
  explicit preview→confirm contracts — no inferred-intent Git, no Git auto-dispatch.
  GitHub tokens (`credentials.py`) reach git only via the push-time `env`
  (`GIT_ASKPASS`, tokenless remote) — never argv/`.git/config`/logs/memory/UI.
  (Details: `ARCHITECTURE.md §7.H`.)

## 6. Context Hygiene

- **Do NOT auto-inject repo contents or code diffs into the main agent's
  context.** That would blow up token budgets and conflate project
  knowledge with implementation detail.
- The main agent's default view of a run is the compact metadata:
  `status`, `task_title`, `summary`, `files_changed`, `commands_run`,
  `blockers`, plus the rendered `result.md`.
- Reading specific changed files into context is **on-demand only**,
  driven by a concrete reason: reviewing a change, debugging a regression,
  or answering a user question about a specific file. Use the bounded
  `execution.inspect` API (`list_repo_files` / `read_repo_file` /
  `search_repo_files`) — read-only wrappers over `ToolRuntime` with
  tighter caps for chat context. Do not invent new filesystem paths.
  The orchestrator's chat loop already opens this channel to the main
  agent through `{"inspect_request": {...}}` JSON (max 3 inspections per
  turn).
- Keep crossings between "summaries + memory" (main agent) and "files in
  `repo/`" (Coding Agent) deliberate and bounded.

## 7. Working Style for Claude Code

- **Make small bounded changes.** Touch the files the task names; don't
  fan out into unrelated refactors. If a refactor would help, propose it
  separately.
- **Preserve existing behavior unless asked.** No silent renames, no
  surprise API changes, no "while I was in there" cleanup of unrelated
  modules.
- **Update `ROADMAP.md` when project state changes.** Detailed task
  status, new constraints, and roadmap shifts belong there. The public
  `README.md` only needs a short bump if the change is user-visible.
  Do NOT duplicate progress into `CLAUDE.md` — this file is the stable
  operating guide.
- **Summarize changed files and verification steps** at the end of every
  task. Mention what you ran (typecheck, build, smoke test) and what you
  did not run.
- **Prefer simple local-first solutions.** ThreadPoolExecutor over Celery,
  SQLite over Postgres, polling over SSE — until there is a concrete
  reason to swap. Don't introduce dependencies you don't need.
- **Match the existing style and structure** of the file you're editing.
  Match indentation, import order, naming conventions, and comment density.
- **Follow §3, §4, §5, §6** without exception. The sandbox boundary,
  memory policy, and context hygiene rules are constitutional, not
  negotiable per task.
