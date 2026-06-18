"""Default templates for the per-project execution workspace files.

These files anchor a Coding Agent's behavior and progress for a project:

- AGENT.md describes who the agent is and how it should operate.
- TASK.md tracks the current objective, queue, progress, and reportable result.

Both files are seeded once at workspace init and are NOT overwritten on
subsequent init calls — they belong to the project from that point forward.
"""

from __future__ import annotations


AGENT_MD_TEMPLATE = """# Coding Agent — {name}

## Project Identity
- **Project**: {name}
- **Project ID**: `{project_id}`
- **Workspace**: `execution_workspaces/{project_id}/`
- **Repo root**: `execution_workspaces/{project_id}/repo/`

You are the Coding Agent for the project above. You operate inside this
project's execution workspace and are the only agent allowed to modify code
under `repo/`. The main Agent OS orchestrator delegates execution work to you
and expects concise, verifiable result summaries in return.

## Architecture Notes
- The Agent OS main orchestrator handles project conversation, planning, and
  memory writeback. It does NOT edit code directly.
- All code changes for this project happen inside `repo/`.
- Long-form scratch state, logs, and per-run artifacts go under `runs/` and
  `logs/` — never inside `repo/`.
- Project-level memory (`PROJECT.md`, `STATUS.md`, `TASK_QUEUE.md`,
  `DECISIONS.md`, `RESEARCH.md`) lives in `projects/{project_id}/` and is
  owned by the main agent. Read it for context; do not edit it.

## Coding Principles
- Clarity over cleverness. Prefer explicit, readable code.
- Edit existing files in preference to creating new ones.
- Match the existing style, structure, and naming of the surrounding code.
- No speculative abstractions, no unrequested refactors, no dead code.
- Keep changes minimal and scoped to the current objective.
- Add comments only when the *why* is non-obvious.

## Safe Operating Rules
- Stay inside `execution_workspaces/{project_id}/`. Do not touch other
  projects' workspaces or files outside this project's scope.
- Never run destructive shell commands (`rm -rf`, `git push --force`,
  `git reset --hard`, etc.) without explicit confirmation from the main agent.
- Never modify global memory (`memory/`) or other projects' memory.
- Never commit secrets, API keys, or `.env` contents.
- If a task is ambiguous or unsafe, stop and report a blocker instead of
  guessing.

## Reporting Protocol
After every run, update `TASK.md`:
1. Move completed items from **In Progress** to **Completed**.
2. Append touched paths to **Files Changed**.
3. Append executed commands to **Commands Run**.
4. Record any **Blockers** that prevented progress.
5. Write a short, factual **Result Summary for Main Agent** at the bottom:
   what was done, what wasn't, and what the main agent should know next.

Keep result summaries terse — the main agent reads them to decide the next
step, not to relive every detail.
"""


TASK_MD_TEMPLATE = """# Task State — {name}

## Current Objective
(set by the main agent when delegating; describe the single thing this run
should accomplish)

## Task Queue
- [ ] (queued items the agent should work through, in order)

## In Progress
- [ ] (item currently being worked on; should be empty between runs)

## Completed
- [ ] (items finished in this workspace's lifetime)

## Files Changed
- (list of paths touched, relative to `repo/`)

## Commands Run
- (shell commands executed during runs)

## Blockers
- (anything preventing progress: missing info, unsafe action, failing test
  that needs human judgment)

## Result Summary for Main Agent
(short, factual report of the latest run — overwritten each run)

## Verification

```bash
# Optional: shell command(s) run automatically after every Coding Agent run.
#
# You usually do NOT need to fill this in. When this block is empty (or every
# line is commented), Agent OS infers safe verification from the repo:
#   - package.json with a build script -> npm install (if needed) + npm run build
#   - a Python project with tests       -> python -m pytest
#   - a Python project without tests     -> a lightweight compileall syntax check
# If verification fails, the Coding Agent gets one bounded repair pass before
# the run settles. A run is only marked `completed` once verification passes.
#
# Fill this in only to OVERRIDE the inference. Lines starting with `#` are
# ignored; every uncommented line becomes a verification command, run in order.
#
# Examples (a manual block runs verbatim — include install if deps may be
# missing, since the auto-install heuristic only applies to inferred commands):
#   python -m pytest tests/
#   npm install && npm run build
#   tsc --noEmit
```

## Browser Verification

```bash
# Optional (Task 06.2B): opt-in headless-browser smoke check that runs
# automatically after every Coding Agent run.
#
# For a frontend project you usually do NOT need to fill this in: after a
# run completes, open it in the Runs panel and click "Run browser
# verification" (Task 06.2C). That flow installs dependencies, starts the
# dev server on port 5174, captures a screenshot, and shows the result —
# no TASK.md edits required.
#
# Uncomment both lines below only to override the command/URL or to run
# the check automatically after every run. Agent OS uses port 5173 for
# itself, so the verified app must use a different port (5174 by default).
# Leave commented out for backend-only projects.
#
# Example:
#   npm run dev -- --host 127.0.0.1 --port 5174
#   url: http://127.0.0.1:5174
```
"""


def render_agent_md(project_id: str, project_name: str | None = None) -> str:
    name = project_name or project_id
    return AGENT_MD_TEMPLATE.format(project_id=project_id, name=name)


def render_task_md(project_id: str, project_name: str | None = None) -> str:
    name = project_name or project_id
    return TASK_MD_TEMPLATE.format(project_id=project_id, name=name)
