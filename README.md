# Agent OS

A lightweight **local-first project cockpit** — manage multiple
long-term projects through a single web chat surface, with structured
markdown memory and a bounded execution layer that can delegate code
work to a sandboxed Coding Agent.

> Detailed implementation status, task history, and the roadmap live in
> [`ROADMAP.md`](./ROADMAP.md); the system's shape (files, pipelines,
> invariants) is in [`ARCHITECTURE.md`](./ARCHITECTURE.md). The stable
> operating guide for any agent working on this repo is
> [`CLAUDE.md`](./CLAUDE.md).

## What is Agent OS?

Agent OS is a small **Agent Operating System** built for one builder
running multiple projects. It combines:

- **Project-scoped conversations** — each project has its own chat
  history, isolated from other projects.
- **Structured markdown memory** — durable project state lives in
  readable, editable `.md` files, not buried in chat scrollback.
- **A thin orchestration layer** — the main agent assembles context
  from memory, produces planning / explanation replies, and decides via
  a separate semantic-judge call whether memory files should be updated.
- **A bounded execution layer** — a sandboxed Coding Agent runs inside
  a per-project workspace under `execution_workspaces/{project_id}/repo/`,
  dispatched explicitly by the user via `@code …` or by confirming a
  model-proposed plan. Complex tasks are first **planned and broken into a
  tracked task graph**, then executed task-by-task with per-task status.
- **A verification surface** — a Runs panel and per-run detail modal
  show status, files changed, commands run, blockers, `result.md`, and a
  settled execution timeline, with **Cancel** and **Retry** controls for
  long-running or failed runs. Metrics update **live** as the agent works,
  and a dedicated **Live Trace** shows a real-time chronological thread of
  every file edit, command, and verification step.

## Showcase: a full-stack app Agent OS built by itself

[**Aegis Launch Control**](./execution_workspaces/aegis-launch-control/SHOWCASE.md)
is a mission-control planning dashboard (**React + TypeScript + Vite + Tailwind**
front end + a lightweight **Express** API) that the Agent OS Coding Agent built
**autonomously, from an empty repository**, in response to a single
natural-language task card. No human wrote any of the app code or fixed its
build. Agent OS planned the work into an **8-task dependency graph**, executed it
task-by-task, and verified the result with a real `npm install` + `npm run build`
(passed). All 8 tasks completed with zero blockers.

[![Aegis Launch Control dashboard](./execution_workspaces/aegis-launch-control/runs/20260619-044436-e65d2e61/screenshots/browser.png)](./execution_workspaces/aegis-launch-control/SHOWCASE.md)

Notably, this run was driven by **Claude Sonnet 4.5** — a mid-tier model, not
the strongest available — so it's a useful *lower bound* on what the system can
produce.

The screenshot above is **Agent OS's own automated browser-verification capture**
of the running app — the upgraded verification waits for the app to actually
render, walks its tabs, and an AI visual judgment confirmed the result looks
correct. The full generated source plus the complete run evidence — the task
card, the plan, a chronological log of every tool call, the build log, and the
multi-page browser-verification captures + visual-review verdict — are committed
under
[`execution_workspaces/aegis-launch-control/`](./execution_workspaces/aegis-launch-control/)
as a public, replayable example. See its
[SHOWCASE.md](./execution_workspaces/aegis-launch-control/SHOWCASE.md) for the
details. Every other project and workspace stays private.

## Why this exists

Existing tools each solve a piece of the workflow but not the whole loop:

- **ChatGPT** is great for discussion and planning but can't operate
  local code or persist project-shaped memory.
- **Claude Code** is great at executing inside a repo but isn't ideal
  as the long-term *conversation surface* for a project.
- **General agent platforms** are too broad and not optimized for a
  multi-project workflow.

Agent OS combines the parts that matter for project work:
ChatGPT-like conversation per project + Claude Code-like execution
power on demand + readable local memory files. It stays lightweight
on purpose.

## Current status

**Phase 1 & 2** (workspace + memory + orchestration + semantic
writeback) are complete. **Phase 3 — Execution Layer** is complete
through **Task 06.2E** (automatic command verification + bounded repair).
The Coding Agent runs sandboxed jobs in the background, the runs panel
auto-refreshes, inferred coding intent is surfaced as a confirmable
plan, terminal runs reconcile back into project memory, the main
agent can inspect specific repo files on demand through a bounded
sandboxed channel, and post-run verification now covers both a
project-defined verify command (06.2A) and an opt-in headless-browser
screenshot of a project-managed dev server (06.2B). The whole
build-and-preview loop now lives in the chat thread (06.2D): a run
posts a natural "running" note, then a completion summary with a
**Run browser verification** button; clicking it installs dependencies,
starts the dev server on port 5174, **waits for the app to actually
render** (not just a loading spinner), captures screenshots of the entry
page **plus a few discovered views/tabs**, and — when a vision-capable
model key is set — runs an **AI visual judgment** that returns a
`passed`/`warning`/`failed`/`inconclusive` verdict over the screenshots
(diagnostic-only; it never changes the run status, and skips gracefully
with no key). It returns a live preview URL + a multi-page thumbnail
gallery inline, and keeps the dev server alive so the URL stays usable. The Runs panel gained **Start / Stop preview**
controls; the run detail modal is now a detailed inspection view.
Command verification is now **automatic** (06.2E): Agent OS infers the
right checks from the repo (`npm run build`, `pytest`, or a syntax
check), gives the Coding Agent one bounded repair pass if they fail, and
marks a run `completed` only after they pass — then offers browser
verification in chat.

**Phase 5 — Execution Orchestration** adds a planning stage and a
structured task graph. A complex task is first inspected (read-only) and
broken into an ordered set of task units with dependencies, persisted as a
`plan.json` artifact; the runner then executes them task-by-task, recording
per-task status / files / commands / blockers and richer run events. Simple
tasks still take the original single-loop path, and verification, browser
verification, preview, and memory reconciliation are unchanged. That structured
data is now **visible and controllable**: the chat run card shows a live phase
badge and task checklist, files/commands counts climb **live** during a run, a
dedicated **Live Trace** modal streams the full chronological activity thread
(opened from the chat card, the Runs panel, or the detail modal), the detail
modal renders a polled timeline that **settles** completed steps (no stale
"running" rows), and runs can be **cancelled** (cooperatively, ending in a clean
`cancelled` state) or **retried** as a new linked run.

**Phase 4 — Interface & UX** has begun with **Task 07.0**: a modern
multi-modal chat composer — auto-growing multiline input (`Enter` for a
newline, `Ctrl`/`Cmd`+`Enter` to send), voice dictation via the Web Speech
API, and file attachments (images, `.txt`/`.md`/`.pdf`/`.doc`/`.docx`) that
can be attached to the chat message and optionally copied into the project's
sandboxed workspace. **Task 07.1** added pluggable **model providers**, since upgraded to a
capability-aware **provider/model registry**: six providers — Claude, GPT,
Gemini, DeepSeek, **Kimi (Moonshot)**, and **Zhipu GLM** — each exposing a
selectable list of current models. The provider is chosen from the chat header
and the specific model from a compact upward-opening picker next to the
composer; a provider is available when its API key is set (missing-key ones show
disabled), and Claude stays the default. Each model carries **capability
metadata** (notably image input), so chat image upload is offered only for
vision-capable models and the AI browser visual judgment runs only when a
vision-capable model is available — skipping gracefully otherwise.
**Task 07.2** added a light color theme alongside the original dark one,
switchable from a dropdown top-right in the chat header and remembered
across reloads.

**Phase 6 — Main Agent Orchestration & Memory v2** strengthens the main agent
itself. Memory writes now flow through one atomic, policy-filtered engine, and
each chat turn makes a single **structured memory-intake decision** that carries
a reason (surfaced as a "🧠 Memory updated" chip). Routing is richer: an
`intent` label plus new explicit commands — `@plan`, `@design`, `@debug`,
`@review`, `@inspect`, `@memory` — shape the response (only `@code` and
confirming a plan ever dispatch a run). And when a run comes back partial,
failed, blocked, or fails verification, the main agent **assesses it and proposes
a bounded next step** ("Run suggested fix") that you confirm with one click —
never auto-run. Run cards now also show whether project memory was reconciled.

**Phase 6.1** polishes this: the recovery assessment now shows in the run detail
modal too; the "Memory updated" chip expands to show exactly which file ›
section changed; long project memory is compacted for the agent's context as it
grows; and when you confirm a plan you can optionally approve a **bounded
auto-recovery budget** (none / 1 / 2) so a non-green run can fix itself once or
twice — capped, linked, and fully audited — without giving up explicit-dispatch
safety.

**Phase 7 — Project Ops & GitHub Lifecycle** makes a finished run a traceable,
user-approved delivery: a pre-run **checkpoint** and a redacted post-run **diff**
are captured automatically, then you **review the diff, commit, push a branch, and
open a GitHub PR** — each external/destructive step shown as a contract you confirm
before it runs, and roll back to the checkpoint anytime. Git is an audited delivery
outlet routed through one sandboxed executor, never raw shell; GitHub tokens live in
a gitignored store and reach git only at push time — never in commits, logs, memory,
prompts, or the UI.

Full task log and the next-step plan are in [`ROADMAP.md`](./ROADMAP.md).

## Architecture

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

- **Frontend** — React + Vite + TypeScript. Three-column layout
  (project list / chat / context + runs).
- **Backend orchestrator** — Python + FastAPI. `orchestrator.py`
  assembles context from memory; `llm.py` wraps the Anthropic SDK;
  memory writeback is a second LLM call gated by a policy filter.
- **Memory layer** — pure markdown files on disk. Global memory in
  `memory/`; project memory in `projects/{id}/`.
- **Execution layer** — `backend/execution/` contains the sandbox,
  tool runtime, runner, background dispatch manager, run store,
  delegation judge, pending-execution flow, memory reconciliation,
  on-demand file inspection, and the confirmable **recovery** assessor.

## Design philosophy

- **Brain vs. hands.** The main agent plans and remembers; the Coding
  Agent executes. They communicate through summaries, not by sharing
  context. This is the single most important architectural rule.
- **Memory as a structured context layer.** Knowledge worth remembering
  belongs in named markdown files with stable sections, not in hidden
  conversation buffers. Memory writes are model-proposed but
  policy-filtered before they touch disk.
- **Local-first trusted execution.** Everything that runs runs on the
  user's machine, in a project-scoped sandbox, surfaced through a UI
  the user can read and refresh. No cloud queues, no opaque background
  workers, no implicit cross-project access.
- **Explicit trust boundary before automation.** `@code` and explicit
  user confirmation of a model-proposed plan are the only two paths
  that dispatch a run.

## Quick Start

### Prerequisites
- Python 3.10+
- Node.js 18+
- An Anthropic API key

### Backend
```bash
cd backend
pip install -r requirements.txt
# Browser verification drives a headless Chromium; install it once:
python -m playwright install chromium
cp .env.example .env
# Edit .env and add at least one provider key:
#   ANTHROPIC_API_KEY (Claude), OPENAI_API_KEY (GPT),
#   GOOGLE_API_KEY (Gemini), DEEPSEEK_API_KEY (DeepSeek),
#   MOONSHOT_API_KEY (Kimi), ZHIPUAI_API_KEY (Zhipu GLM)
uvicorn main:app --reload --port 8000
```

### Frontend
```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>.

## Repository layout

```
agent-os/
├── frontend/              # React + Vite + TypeScript UI
├── backend/               # Python + FastAPI
│   ├── main.py            # API endpoints
│   ├── orchestrator.py    # context assembly + memory judge + inspect loop
│   ├── llm.py             # Anthropic SDK wrapper
│   ├── database.py        # SQLite (conversations + messages + pending exec)
│   ├── execution/         # sandbox, runner, judges, reconciliation, inspect
│   └── tests/             # backend test suite (stubbed LLM, no API key needed)
├── memory/                # global markdown memory  (private; ships SOUL.md + *.example.md templates + README)
├── projects/              # per-project markdown memory  (private; ships *.example.md templates + README)
├── execution_workspaces/  # Coding Agent workspaces  (private; ships *.example.md templates + README)
├── README.md              # this file (public landing page)
├── ROADMAP.md             # detailed status + task log + next steps
├── ARCHITECTURE.md        # system shape: files, pipelines, invariants
└── CLAUDE.md              # stable operating guide for coding agents
```

## Running the tests

```bash
cd backend
python tests/test_delegation_judge.py
python tests/test_pending_execution.py
python tests/test_pending_execution_db.py
python tests/test_memory_reconciliation.py
python tests/test_inspect.py
python tests/test_verification.py
python tests/test_verification_inference.py
python tests/test_browser_verification.py
python tests/test_runner_diagnostics.py
python tests/test_uploads.py
python tests/test_providers.py
python tests/test_planner.py
python tests/test_runner_planning.py
python tests/test_run_control.py
python tests/test_memory_engine.py
python tests/test_memory_intake.py
python tests/test_intent_router.py
python tests/test_recovery.py
```

All tests stub the LLM caller, so no Anthropic API key is needed to
run them.
