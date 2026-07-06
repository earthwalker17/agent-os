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
| `STATUS.md`      | Current phase, latest milestone, what works, what's next, and the `## Task Queue` board (Completed / In Progress / Next). |
| `DECISIONS.md`   | Important decisions and their rationale, over time.           |
| `RESEARCH.md`    | Research findings, external references, technical notes.       |
| `LESSONS.md`     | Durable, reusable project lessons from builds, failures, fixes, reviews, deployments, and decisions. |

These five files are seeded automatically when a project is created, then
evolve through chat (policy-filtered memory writeback) and post-run
reconciliation. (Before Phase 10.2 the task board lived in a separate
`TASK_QUEUE.md`; it is now a section inside `STATUS.md`, and legacy files are
migrated automatically on startup.)

## What's committed vs. private

Real project folders (current and future) are **gitignored** — your projects
stay on your machine. This repo ships sanitized example templates of the five
files so the layout is documented:

- `PROJECT.example.md`
- `STATUS.example.md`
- `DECISIONS.example.md`
- `RESEARCH.example.md`
- `LESSONS.example.md`

In a real install these live together inside one `projects/{project_id}/`
folder (one set per project), not flat at the root — they're flattened here
only so they can be committed as examples without shipping a fake project.
