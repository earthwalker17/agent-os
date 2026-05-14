# CLAUDE.md

Operating guide for Claude Code and any other coding agent working on this
repository. This file is intentionally short and phase-independent. For the
current project status and roadmap, read `README.md`.

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

- **`@code` is currently the only actual execution trigger.** A project
  chat message prefixed with `@code ` dispatches a background Coding Agent
  run. Nothing else dispatches runs, including the implicit-delegation
  heuristic (see below).
- **Implicit delegation is a rule-based MVP suggestion layer**, not a real
  semantic delegation judge. It can flag obvious coding requests and
  propose an `@code` task card, but it never auto-dispatches and is not
  authoritative. The intended final design is a model-judged detector.
- **Coding Agent runs in `execution_workspaces/{project_id}/repo/`.** Run
  artifacts live under `execution_workspaces/{project_id}/runs/{run_id}/`
  (`task_card.md`, `events.jsonl`, `run.json`, `result.md`).
- **Runs are background.** Both `@code` and `POST /api/projects/{id}/execution/runs`
  return an initial `RunRecord` (status=`running`) immediately; finalization
  happens in a worker thread. Crashes promote to `failed` with the error
  captured as a blocker.
- **Main agent sees run summaries by default.** Not full repo contents,
  not full diffs, not raw event logs.

## 6. Context Hygiene

- **Do NOT auto-inject repo contents or code diffs into the main agent's
  context.** That would blow up token budgets and conflate project
  knowledge with implementation detail.
- The main agent's default view of a run is the compact metadata:
  `status`, `task_title`, `summary`, `files_changed`, `commands_run`,
  `blockers`, plus the rendered `result.md`.
- Reading specific changed files into context is **on-demand only**,
  driven by a concrete reason: reviewing a change, debugging a regression,
  or answering a user question about a specific file. Use the existing
  `ToolRuntime.read_file` / `list_files` / `search_files`; do not invent
  new filesystem paths.
- Keep crossings between "summaries + memory" (main agent) and "files in
  `repo/`" (Coding Agent) deliberate and bounded.

## 7. Working Style for Claude Code

- **Make small bounded changes.** Touch the files the task names; don't
  fan out into unrelated refactors. If a refactor would help, propose it
  separately.
- **Preserve existing behavior unless asked.** No silent renames, no
  surprise API changes, no "while I was in there" cleanup of unrelated
  modules.
- **Update `README.md` when project state changes.** Current status, new
  features, new constraints, and roadmap shifts belong there. Do NOT
  duplicate that progress into `CLAUDE.md` — this file is the stable
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
