# `projects/` — Per-Project Memory

Each project in Agent OS gets its own folder here: `projects/{project_id}/`.
A project folder holds that project's **structured markdown memory** — the
planning and status files the main agent loads when you chat about the project.
This is distinct from the project's *code*, which lives in a sandboxed
execution workspace under `execution_workspaces/{project_id}/` (see that
folder's README).

The main agent owns these files (planner / memory steward). The Coding Agent
never edits them.

## Files in each `projects/{project_id}/` folder

| File             | Purpose                                                       |
|------------------|---------------------------------------------------------------|
| `PROJECT.md`     | The stable project definition: vision, scope, target user, tech stack. |
| `STATUS.md`      | Current phase, latest milestone, what works, what's next.     |
| `TASK_QUEUE.md`  | In Progress / Up Next / Done checklist for the project.        |
| `DECISIONS.md`   | Important decisions and their rationale, over time.           |
| `RESEARCH.md`    | Research findings, external references, technical notes.       |

These five files are seeded automatically when a project is created, then
evolve through chat (policy-filtered memory writeback) and post-run
reconciliation.

## What's committed vs. private

Real project folders (current and future) are **gitignored** — your projects
stay on your machine. This repo ships sanitized example templates of the five
files so the layout is documented:

- `PROJECT.example.md`
- `STATUS.example.md`
- `TASK_QUEUE.example.md`
- `DECISIONS.example.md`
- `RESEARCH.example.md`

In a real install these live together inside one `projects/{project_id}/`
folder (one set per project), not flat at the root — they're flattened here
only so they can be committed as examples without shipping a fake project.
