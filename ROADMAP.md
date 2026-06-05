# Agent OS — Roadmap & Implementation Status

> **For future Claude Code / ChatGPT sessions:** read `CLAUDE.md` first
> (stable operating rules), then this file (current state and next
> steps). `README.md` is the public-facing landing page — it is
> deliberately concise and does not carry detailed task history.

This document is the long-form record of where Agent OS is, what's
landed, and what's coming next. It can grow over time; the public README
should not.

---

## Documentation Policy

Three layers, each with a distinct job:

| File          | Audience                       | Tone / Scope                                   |
|---------------|--------------------------------|------------------------------------------------|
| `README.md`   | Public visitors on GitHub      | Short, presentable. What it is, why, setup.    |
| `ROADMAP.md`  | The builder + future AI sessions | Detailed status, task log, constraints, plans.  |
| `CLAUDE.md`   | Any coding agent working here  | Stable operating rules. Phase-independent.     |

Never duplicate task history across these three. When a task lands:

- README gets a 1–2 line bump in **Current status** (only if user-visible).
- ROADMAP gets the full entry under **Phase 3 — Execution Layer**.
- CLAUDE.md only changes if the constitutional rules changed.

---

## Phase 1 & 2 — Summary (Complete)

These were landed before the execution layer began. They are stable.

- **Workspace + project/conversation management.** Local filesystem
  layout (`memory/`, `projects/{id}/`), FastAPI backend, React frontend
  with three-column layout (project list / chat / context). Project
  create / rename / delete, multiple conversations per project,
  auto-titling from the first message.
- **Global + project markdown memory.** `SOUL.md`, `USER.md`,
  `WORKSTYLE.md`, `MEMORY.md` for global; `PROJECT.md`, `STATUS.md`,
  `TASK_QUEUE.md`, `DECISIONS.md`, `RESEARCH.md` per project. Editable
  from the UI; `SOUL.md` is read-only.
- **LLM-based orchestration.** Full conversation history + memory
  context per turn via the Anthropic SDK in `llm.py`. Context assembly
  in `orchestrator.py`.
- **Two-step semantic memory writeback.** After each chat turn, a
  second LLM call examines the exchange + current memory and proposes
  structured JSON updates. Updates are policy-filtered before disk
  writes. `SOUL.md` is always excluded.

---

## Phase 3 — Execution Layer (Complete through 06.2C)

### 05.1 — Execution workspace foundation
Per-project workspaces under `execution_workspaces/{project_id}/`:
`repo/`, `runs/`, `logs/`, `AGENT.md`, `TASK.md`. Idempotent init —
missing folders are created but existing `AGENT.md` / `TASK.md` are
preserved.

### 05.2 — ProjectSandbox + ToolRuntime
Single chokepoint for every tool call. `ProjectSandbox` validates
paths (no `..`, no absolute paths, no `.env` / `*.key` / `.ssh`, no
escape from the project's `repo/`) and shell commands (block-list +
`fetch | shell` regex). `ToolRuntime` exposes `list_files`,
`read_file`, `write_file`, `append_file`, `search_files`, `run_shell`
with output caps (20 000 chars per read, 500 entries per listing,
200 search hits).

### 05.3 — CodingAgentRunner
LLM-driven bounded JSON tool loop (max 8 steps). Per-run artifacts in
`runs/{run_id}/`: `task_card.md`, `events.jsonl`, `run.json`,
`result.md`. Single correction retry on malformed JSON; protocol
errors promote the run to `failed`.

### 05.4 — `@code` chat trigger
Project chat messages starting with `@code <task>` dispatch the runner
synchronously and return a placeholder reply. GENERAL workspace
rejects.

### 05.5 — Runs panel + RunDetailModal
Read-only right-column panel listing runs (newest first). Clicking a
run opens `RunDetailModal` showing `run.json` + rendered `result.md`.

### 05.6A — BackgroundRunManager
`ThreadPoolExecutor`-based dispatcher. `@code` and
`POST /execution/runs` return immediately with the placeholder
`RunRecord` (status=`running`); the runner finalizes off-thread. If
the background thread crashes, the manager flips status to `failed`,
appends the error as a blocker, and writes an emergency `result.md`
so the run never gets stuck in `running`.

### 05.6B — Runs panel auto-refresh
The panel polls every 2s while runs are `running`, stops when idle,
shows a pulsing "N running" indicator. After `@code` send, the panel
reloads immediately for instant feedback.

### 05.7 — "View Run" chat affordance
Chat bubbles containing a run id render a **View Run** button that
opens the same `RunDetailModal` as the Runs panel. Linked via
message metadata.

### 05.8 — Heuristic delegation suggestion (fallback)
Conservative rule-based detector (`delegation_intent.py`) for
code-shaped messages. Currently only used as a fallback when the
05.9 LLM judge fails; `@code` remains the canonical explicit trigger.

### 05.9 — LLM semantic delegation judge
Each non-`@code` project-chat message goes through `judge_delegation()`
— a small Claude call (`max_tokens=384`) that classifies into
`dispatch_suggested` / `discussion` / `memory_only` using recent
conversation + a compact project-memory snapshot. Robust to
non-English imperatives and anaphoric follow-ups ("do that").
Judge failures fall back safely to the 05.8 heuristic. Never
dispatches — only proposes.

### 05.9.5 — Confirmable execution plans
When the judge returns `dispatch_suggested`, the assistant replies in
project-manager tone with a short execution plan and stores the full
Coding Agent task card internally as a **pending execution**. The
chat bubble exposes two buttons — **OK, run this** (dispatches the
stored task card via the same path as `@code`) and **Revise plan**
(enters a sticky revise mode; the next chat send rewrites the plan
+ task card in place via a single small LLM call). The full task
card is available behind an inspect toggle but is not dumped into
the chat by default. Inferred coding intent never auto-runs.

### 06.0 — Run-result memory reconciliation
When a Coding Agent run reaches a terminal state
(`completed` / `partial` / `blocked` / `failed`), a small
model-judged reconciliation call examines a **compact** view of the
run — the `ResultSummary`, rendered `result.md`, files changed,
commands run, and blockers — together with a snapshot of the four
writable target memory files, and decides whether to update
`STATUS.md`, `TASK_QUEUE.md`, `DECISIONS.md`, or `RESEARCH.md`.
`PROJECT.md`, global memory, `SOUL.md`, and repo source files are
out of scope. Read-only inspection runs (no files changed, no
blockers, no actionable summary) and noisy failures (no files
changed, no informative blocker) are skipped before the LLM call.
Each run is reconciled at most once — the outcome is recorded on
`RunRecord` (`memory_reconciled` / `memory_reconciliation` /
`memory_reconciliation_error`) and as a `memory_reconciled` event
in `events.jsonl`. Reconciliation NEVER prevents a run from
completing.

### 06.2A — Command verification MVP
After a Coding Agent run finishes its normal final action, the runner now
optionally executes a single project-defined verify command and records
the outcome on `RunRecord.verification` (`enabled`, `command`, `status`
`= passed | failed | skipped`, `exit_code`, `output_preview`,
`duration_ms`). The verify command lives in a `## Verification` fenced
bash block inside each project's `execution_workspaces/{id}/TASK.md`
(seeded as a commented-out example by the workspace template). The
runner snapshots TASK.md before applying the agent's task_md_update so
the agent can't accidentally clobber the verification config. The
command is dispatched through the existing `ToolRuntime.run_shell` path,
which means the same sandbox block-list applies — unsafe commands are
recorded as `failed` with the sandbox reason in `output_preview` rather
than executed. A failing verification downgrades a `completed` run to
`partial` and appends a `verification failed: <cmd>` blocker; runs that
finalized as `failed` / `blocked` / `partial` keep their status either
way. Verification output appears as a new `## Verification` section in
`result.md` and as a styled block in `RunDetailModal`. Verification
failure NEVER fails the run itself — exceptions are converted into a
`failed` status with the error in the preview. Browser-based / dev-server
verification is intentionally deferred — no Playwright, no headless
browser, no screenshot capture in this slice.

### 06.2B.1 — Runner diagnostics + stuck-run sweep (06.2B follow-up)
Smoke-testing 06.2B surfaced four small but real problems in the
runner. All fixed together as a single targeted patch:

1. **Step budget too tight for scaffolding.** `MAX_STEPS` was 8, but a
   "scaffold a tiny frontend project" run could need 5–8 `write_file`
   steps after one or two `list_files` calls and still leave room for
   a `final` action. Bumped to 16 — still bounded, generous enough for
   the realistic worst case.
2. **Empty `files_changed` / `commands_run` when the budget exhausts.**
   The runner derived those lists exclusively from the agent's `final`
   action, so a run that ran out of steps after writing several files
   reported "no files changed" even though several were on disk. The
   runner now tracks successful `write_file` / `append_file` paths and
   accepted `run_shell` commands locally and surfaces them as a
   diagnostic fallback when the final action's lists are absent or
   empty. The final action's explicit lists still win when supplied.
3. **System prompt encouraged "verify by running" inside the loop.**
   The agent was spending its budget on `npm run dev` and similar
   long-running commands. The prompt now says explicitly that command
   and browser verification are automatic post-loop steps, so the
   agent must not spin up dev servers or run full test suites inside
   the loop, and must avoid `read_file` / `list_files` on paths it
   plans to overwrite (`write_file` overwrites unconditionally).
4. **Stuck-`running` runs from a server restart.** When the FastAPI
   process exited mid-loop (crash, reload, machine reboot), the
   in-process `BackgroundRunManager` crash-handler died with it and
   the orphaned run stayed in `running` forever. Added
   `run_store.sweep_stuck_runs()` and wired it into the FastAPI
   startup hook — any run.json with `status="running"` at startup is
   promoted to `failed` with an "interrupted" blocker, `completed_at`
   stamped, a `run_interrupted` event appended, and `result.md`
   backfilled if missing.

### 06.2B — Browser verification MVP
Opt-in headless-browser smoke check that runs after the 06.2A command
verification. A project that ships a frontend may add a
`## Browser Verification` block to its
`execution_workspaces/{id}/TASK.md` with a dev-server command and a
`url:` line. After every Coding Agent run, if the block is present,
the runner spins the dev server up through a sandbox-validated
subprocess in the project's `repo/` directory, polls the URL until it
becomes reachable (default 30s), drives a headless Playwright Chromium
browser to that URL, captures a single screenshot to
`execution_workspaces/{id}/runs/{run_id}/screenshots/browser.png`, and
tears the server back down (SIGTERM → SIGKILL on POSIX, CTRL_BREAK →
kill on Windows, with the child started in its own process group on
POSIX). The result lands on `RunRecord.browser_verification`
(`enabled`, `command`, `url`, `status = passed | failed | skipped`,
`screenshot_path`, `output_preview`, `duration_ms`). A failing browser
verification downgrades a `completed` run to `partial` and appends a
`browser verification failed: <cmd>` blocker, matching 06.2A's
behavior. Backend-only projects (no block in TASK.md) skip browser
verification entirely. The screenshot is served back through
`GET /api/projects/{id}/execution/runs/{run_id}/screenshot` and
rendered inline in `RunDetailModal`. Browser verification NEVER
crashes background finalization — any exception is captured as a
`failed` status with the reason in `output_preview`. Lifecycle
constraints are deliberately tight: short readiness timeout, bounded
output preview, screenshot stays inside the run artifact directory,
and the dev-server command goes through `ProjectSandbox.validate_command`
(same block-list as command verification). AI visual judgment,
multi-page browser flows, and streaming run telemetry are
intentionally deferred. **Manual smoke test note:** Playwright is
imported lazily; install it with `pip install playwright` plus
`playwright install chromium` in the backend venv before running
against a real project. CI/unit tests mock the screenshot runner and
process starter, so no browser binaries are needed for the test
suite.

### 06.2B.2 — Browser-verification pipe-deadlock fix (06.2B follow-up)
Smoke-testing 06.2B on Windows surfaced a reproducible deadlock: every
real run finished as `partial` with `url did not become reachable
within 30s`, even after the same `npm run dev -- --host 127.0.0.1 --port
5174` command worked when launched manually from the same `repo/`
directory. Root cause: the dev server was started with
`stdout=PIPE, stderr=PIPE` but nothing was reading the pipes. The
default Windows pipe buffer (~4-8 KB) fills up during vite's startup
output (dep pre-bundling logs, deprecation warnings) before vite ever
calls `listen()` on its port, so the child blocks on its next `print()`
and the URL never becomes reachable. The manual run worked because the
user's terminal was draining the pipes for free. Fix:

1. **Bounded background drainer threads.** A new `_StreamDrainer` reads
   `proc.stdout` and `proc.stderr` line-by-line in daemon threads with a
   bounded in-memory buffer (`_BROWSER_OUTPUT_PREVIEW_CHARS * 2`). The OS
   pipe buffer can no longer fill up regardless of how chatty the dev
   server is, so vite reaches its `listen()` call normally.
2. **Server output surfaced on every failure path.** Drained
   stdout/stderr is now included in `output_preview` for the
   url-unreachable, dev-server-crashed, screenshot-failed, and
   missing-screenshot branches — not just the previous
   process-already-died case. Future failures will tell us whether vite
   printed a port-conflict error, hung in dep optimization, or simply
   never bound the socket.
3. **Fast-fail on dev-server early exit.** New `_wait_for_url_or_exit`
   wrapper polls `proc.poll()` in between URL probes, so a dev server
   that dies at second 1 (port conflict, missing dep) no longer burns
   the full 30s readiness budget before being reported as failed.
4. **Categorical phase label in the failure message.** The
   url-unreachable branch now says `(dev server still running; last
   probe: …)` so the operator can distinguish "server crashed" from
   "server up but not responding" at a glance.

No public API changes; the `process_starter` and `screenshot_runner`
test seams keep the same signatures. New regression test
`test_drainer_surfaces_server_output_on_unreachable_url` locks in the
behavior: a still-running process that produces noisy stdout/stderr
must have its output appear in `result.output_preview` even though the
caller never reads `proc.stdout`/`proc.stderr` directly.

### 06.2B.3 — Playwright on worker thread fix (06.2B follow-up)
With 06.2B.2 in place, the dev server now reaches its `ready` state
reliably and URL polling succeeds — but the very next phase failed
with a bare ``screenshot capture failed: NotImplementedError:`` (no
message at all). Root cause: ``CodingAgentRunner.run_task`` runs inside
a ``BackgroundRunManager`` worker thread, and Playwright's sync API
ends up invoking ``asyncio.create_subprocess_exec`` to spawn its
Node.js driver. On Windows in a non-main thread, that lands on
``SelectorEventLoop``'s subprocess methods, which raise a *messageless*
``NotImplementedError()`` — and ``f"{exc}"`` collapses that to an empty
string, which is why the preview had nothing after the colon. Fix:

1. **Playwright now runs in a Python subprocess.** The default screenshot
   runner shells out to ``sys.executable -c <inline script>`` so
   Playwright always executes on the main thread of a fresh interpreter
   with the platform-default asyncio policy. Side-steps the
   thread/event-loop interaction entirely. ~200-500 ms overhead is
   trivial for a smoke check that already takes seconds.
2. **Structured error categorization.** The inline script writes a JSON
   blob to stderr — ``{"error": "<tag>", "message": "..."}`` — and uses
   distinct exit codes (2 = playwright missing, 3 = chromium missing, 4 =
   generic capture failure). The caller translates those into actionable
   operator messages (``"Run: pip install playwright && python -m
   playwright install chromium"`` / ``"Run: python -m playwright install
   chromium"``) instead of letting raw stack traces leak into
   ``output_preview``.
3. **Messageless-exception preview formatting.** A new ``_format_exception``
   helper falls back to ``repr(exc)`` when ``str(exc)`` is empty, so a
   bare ``NotImplementedError()`` now renders as
   ``NotImplementedError()`` in the preview — not a dangling
   ``NotImplementedError:`` with nothing after. Wired into the
   dev-server-start, screenshot-failure, and outer-crash paths.

No public API changes; ``screenshot_runner`` and ``process_starter``
test seams keep the same signatures. New regression tests
(``test_screenshot_messageless_exception_does_not_render_blank_colon``,
``test_default_runner_clean_error_when_playwright_missing``,
``test_default_runner_clean_error_when_chromium_missing``) lock in the
diagnostics-clarity contract.

### 06.2C — User-triggered browser verification flow
06.2B made browser verification an *automatic* post-run step gated on a
hand-written `## Browser Verification` block in `TASK.md`. 06.2C turns it
into a **user-triggered UI flow** so a completed frontend run can be
verified with one click — no manual `TASK.md` editing for the happy path.

- **New endpoint.** `POST /api/projects/{id}/execution/runs/{run_id}/browser-verify`
  runs verification against an existing run and writes the result back
  into the same artifacts (`run.json`, `result.md`, `screenshots/`). It
  rejects non-terminal runs (only `completed` / `partial` are eligible)
  with a 409. Runs synchronously — install + dev server + screenshot is
  seconds for a small Vite app, and the modal shows a verifying state
  while it awaits.
- **`run_ui_browser_verification()`** (in `browser_verification.py`)
  reuses 06.2B's dev-server → readiness → screenshot → teardown
  lifecycle (extracted into a shared `_core_browser_verification`) but:
  - falls back to a default Vite command on a **non-conflicting port**
    (`npm run dev -- --host 127.0.0.1 --port 5174`, URL
    `http://127.0.0.1:5174`) when `TASK.md` has no block — Agent OS
    itself uses 5173. An explicit block still wins, so advanced users
    keep control.
  - runs **`npm install` first** when `repo/package.json` exists,
    capturing output. A failed (or crashing) install short-circuits the
    flow: no dev server, no screenshot, a clear `dependency install
    failed` preview. Missing `package.json` => install `skipped`, flow
    proceeds. The install command also goes through
    `ProjectSandbox.validate_command`.
- **Status recompute.** `apply_ui_browser_verification_to_record()`
  mirrors 06.2B (a failing verification downgrades `completed` →
  `partial` + adds a `browser verification failed:` blocker) and adds
  retry semantics: a prior browser blocker is cleared before recomputing,
  and a now-passing verification restores `partial` → `completed` only
  when no other blockers remain.
- **Model fields.** `BrowserVerificationResult` gains optional
  `install_command` / `install_status` / `install_output_preview`
  (`None` on the 06.2B runner path, so no regression).
- **UI.** `RunDetailModal` shows a **Run browser verification** button on
  `completed` / `partial` runs (re-labelled "Re-run…" after a prior
  attempt), with a verifying state and error surfacing; the browser block
  now also renders the dependency-install status + output.
- **Safety preserved.** Commands route through the sandbox; screenshot
  path stays inside the run artifact dir; previews stay bounded; no
  arbitrary frontend-supplied shell. AI visual judgment, multi-page
  flows, and streaming telemetry remain out of scope. `result.md` is
  re-rendered via `run_store.rerender_result_md`, which preserves the
  original summary/notes.

**Manual smoke test note:** as with 06.2B, the real screenshot path needs
`pip install playwright` + `playwright install chromium` in the backend
venv. Unit tests stub the installer, process starter, screenshot runner,
and readiness probe, so no `npm`/browser binaries are needed.

### Housekeeping — deletion cleanup + public example templates (non-phase)
A batch of small maintenance changes landed alongside 06.2B. These are
unrelated to the verification loop, so they are deliberately left
**unnumbered** to keep the next `06.2C` slot reserved for the real
verification-loop milestone. Brief record:

- **Delete-path fixes.** `delete_conversation` /
  `delete_conversations_for_project` now clear the `pending_executions`
  FK child before the parent rows (fixes silently-failing conversation
  deletes; regression-tested in `test_pending_execution_db.py`).
  `api_delete_project` now also removes `execution_workspaces/{id}/`
  via a Windows-safe `rmtree`, and the frontend surfaces non-OK
  delete responses via `alert(...)` instead of silently returning.
- **Opt-in workspace deletion.** `api_delete_project` takes
  `delete_workspace: bool = False`; the codebase under `repo/` is kept
  unless the user ticks "Delete its workspace too" in the confirm
  modal. `GET /api/projects/{id}/workspace-status` reports whether a
  workspace is on disk.
- **Public example templates.** `.gitignore` now ignores each private
  folder's *contents* while committing `README.md` + `*.example.md`
  explainers under `memory/`, `projects/`, and `execution_workspaces/`,
  so the layout is visible on-repo without exposing private data.

### 06.1 — On-demand main-agent file inspection
The main agent now has a bounded, sandboxed path to inspect specific
files inside a project's execution workspace `repo/` directory **only
when the user's question requires it**. Implemented as a tiny tool
loop inside `orchestrate()` (max 3 inspections per turn): the LLM
may emit `{"inspect_request": {"tool":
"list_files"|"read_file"|"search_files", ...}}` JSON to request an
inspection. The orchestrator executes it through `execution.inspect`
(which routes through `ProjectSandbox` + `ToolRuntime`), and the
result is fed back as a labeled `INSPECTION RESULT` block before the
next iteration. The loop is enabled **only** for non-GENERAL projects
whose execution workspace has been initialized. Read-only — no
`write_file` / `append_file` / `run_shell` on this surface. Caps:
8000 chars per file, 150 entries per listing, 30 search hits. Path
traversal, absolute paths, sensitive files, and cross-project
access are rejected by the sandbox. `ChatResponse.inspected_files`
surfaces which files were read.

---

## Current Architecture

```
┌────────────┐    ┌────────────────┐    ┌────────────────┐
│  Frontend  │ ←→ │  FastAPI       │ ←→ │  Anthropic API │
│  (React)   │    │  /api/*        │    │  (Claude)      │
└────────────┘    └────────────────┘    └────────────────┘
                         │
        ┌────────────────┼────────────────────────┐
        │                │                        │
        ▼                ▼                        ▼
   memory/         projects/{id}/        execution_workspaces/{id}/
   (global .md)    (project .md)         ├─ repo/    ← Coding Agent
                                         ├─ runs/    ← per-run artifacts
                                         ├─ logs/
                                         ├─ AGENT.md
                                         └─ TASK.md
```

### Module layout (`backend/execution/`)

| File                       | Purpose                                            |
|----------------------------|----------------------------------------------------|
| `manager.py`               | Workspace filesystem                               |
| `models.py`                | `ExecutionWorkspace` / `RunRecord` / `TaskSpec` / `ResultSummary` |
| `templates.py`             | `AGENT.md` / `TASK.md` defaults                    |
| `sandbox.py`               | `ProjectSandbox`: path + command validation        |
| `tool_models.py`           | `ToolResult` + request models                      |
| `tool_runtime.py`          | `ToolRuntime`: file + shell tools                  |
| `prompts.py`               | Coding Agent system + tool-result prompts          |
| `run_store.py`             | Per-run artifact reader/writer                     |
| `runner.py`                | `CodingAgentRunner` JSON tool loop                 |
| `background.py`            | `BackgroundRunManager` (thread pool)               |
| `chat_delegation.py`       | `@code` chat trigger                               |
| `delegation_intent.py`     | Heuristic implicit-delegation (fallback)           |
| `delegation_judge.py`      | LLM semantic delegation judge (05.9)               |
| `pending_execution.py`     | Confirmable execution plans (05.9.5)               |
| `memory_reconciliation.py` | Post-run memory reconciliation (06.0)              |
| `inspect.py`               | Main-agent file inspection (06.1)                  |
| `verification.py`          | Post-run command verification (06.2A)              |
| `browser_verification.py`  | Post-run browser verification MVP (06.2B)          |

### Execution-trigger contract

- **`@code`** in a project chat starts a `CodingAgentRunner` run
  **immediately** — the task card is the user's literal text after
  `@code`.
- **Inferred coding intent** (detected by the LLM delegation judge)
  creates a **confirmable pending execution plan** instead of running
  anything. The assistant replies with the plan; the user must click
  **OK, run this** to dispatch.
- **Revise plan** does not dispatch. The next chat send is treated as
  revision instructions; the pending plan + task card are rewritten
  in place via a small LLM call. The new plan is shown with the same
  two buttons.
- **Only user confirmation dispatches.** No inferred intent ever
  auto-runs. There is no path from `dispatch_suggested` to a Coding
  Agent run that bypasses an explicit user action.
- The delegation judge does not run in the GENERAL workspace; the
  confirm endpoint rejects GENERAL. Pending plans only exist in
  project chats with an execution workspace.

---

## Current Constraints

- **No streaming** on `/api/chat` — full response returned in one shot.
- **Command verification + opt-in browser verification (06.2A + 06.2B).**
  After each Coding Agent run, an optional project-defined
  `## Verification` shell command runs through the existing sandbox.
  A separate, opt-in `## Browser Verification` block in `TASK.md` can
  additionally spin up a project dev server, wait for a configured
  URL, capture one headless-browser screenshot, and tear the server
  down. Either failing check downgrades a `completed` run to
  `partial`. Backend-only projects (no `## Browser Verification` block)
  skip the browser path entirely. **No AI visual judgment yet** —
  screenshots are stored and rendered, not analyzed. Streaming run
  telemetry and multi-page browser flows are also still future work.
- **Up to four LLM calls per non-`@code` project chat turn** —
  delegation judge + (optional inspection loop iterations) + chat
  response + memory judge. The inspection loop is only entered when
  the model emits an `inspect_request`; most chat turns stay at 3
  calls.
- **Main agent does not auto-read repo contents** — by design.
  Specific changed files are read on demand only, through the bounded
  inspection loop in 06.1 (max 3 reads per turn, 8000 chars per
  file).
- **Run-result memory reconciliation is bounded** (06.0). The
  reconciliation judge sees only compact run metadata + rendered
  `result.md` + a compact memory snapshot — never `events.jsonl`,
  full diffs, or full repo contents.
- **Single-user, single-process.** No multi-user auth, no shared
  deploy story.

---

## Test Coverage

The backend tests live under `backend/tests/` and stub the LLM caller
so no Anthropic API key is needed to run them.

| File                                 | Tests | Covers                                          |
|--------------------------------------|------:|-------------------------------------------------|
| `test_delegation_judge.py`           |    15 | 05.9 judge: decisions, fallbacks, parsing       |
| `test_pending_execution.py`          |    17 | 05.9.5 serialization, revision LLM, renderers   |
| `test_pending_execution_db.py`       |     6 | 05.9.5 SQLite lifecycle + metadata roundtrip + delete-path FK cleanup |
| `test_memory_reconciliation.py`      |    26 | 06.0 parser, skip rules, e2e pipeline           |
| `test_inspect.py`                    |    29 | 06.1 sandbox, parser, orchestrator loop         |
| `test_verification.py`               |    21 | 06.2A parser, runner integration, sandbox path  |
| `test_browser_verification.py`       |    26 | 06.2B parser, lifecycle, runner integration, drainer, Playwright subprocess diagnostics |
| `test_runner_diagnostics.py`         |     9 | 06.2B.1 observed activity, sweep_stuck_runs     |
| `test_ui_browser_verification.py`    |    12 | 06.2C UI-triggered flow: default port 5174, install step, status recompute, artifact write |
| **Total**                            | **161** |                                                 |

Run all:
```bash
cd backend
python tests/test_delegation_judge.py
python tests/test_pending_execution.py
python tests/test_pending_execution_db.py
python tests/test_memory_reconciliation.py
python tests/test_inspect.py
python tests/test_verification.py
python tests/test_browser_verification.py
python tests/test_runner_diagnostics.py
python tests/test_ui_browser_verification.py
```

---

## Recommended Next Steps

### Next up: AI-assisted visual review
Browser verification (06.2B) captures a screenshot but does not judge
it. The natural follow-on is a small model-judged pass over the
captured screenshot + the original task card + the rendered
`result.md`, producing a structured "looks right / looks wrong / can't
tell" verdict with a one-paragraph reason. Open questions:

- Should it be a third post-run step or fold into 06.0's memory
  reconciliation judge?
- Cost gate: only run it on `completed` runs with a passing browser
  verification, to avoid paying for a vision call on every failure?
- How to surface the verdict in `RunDetailModal` — alongside the
  screenshot, or as a separate "Visual review" section?

### After AI visual review
- **Streaming responses.** Server-Sent Events on `/api/chat` for
  longer replies. Touches `llm.py`, `orchestrator.py`, the chat
  endpoint, and `ChatPanel.tsx`.
- **Run event stream.** Replace 2s polling on the Runs panel with a
  per-project SSE stream of status transitions, event-log appends,
  and verification/browser-verification phase transitions. Shares
  plumbing with streaming responses.
- **Improve cost / latency.** The current 3–4 LLM calls per turn is
  the obvious cost lever. Plausible reductions: cache the delegation
  judge on idempotent messages; merge the memory judge into the main
  response with structured output; only run the inspection loop when
  a heuristic gate fires.

### Longer-term, not committed yet
- **Run cancellation** from the Runs panel.
- **Run retry** ("rerun this task card" button).
- **Cross-project memory linking** — explicit links from one
  project's `RESEARCH.md` to another's, surfaced as inspection
  suggestions.
- **Multi-user / shared deploy** would require auth, per-user
  workspaces, and a different DB story. Not on the near-term path.

---

## How to Use This Document

When you (Claude / ChatGPT / a human) sit down to work on Agent OS:

1. Read `CLAUDE.md` — the rules of engagement haven't changed.
2. Read this file's **Current Architecture**, **Current Constraints**,
   and **Recommended Next Steps**.
3. Skim the most recent task entries in **Phase 3** for what just
   landed.
4. Then propose your task. Don't re-litigate decisions already
   recorded above unless you have new information.

When a task lands, update this file (the Phase 3 list, the test
table, and the constraint list) — and only bump the README's
**Current status** line if the change is user-visible on GitHub.
