# `execution_workspaces/` — Project Execution Workspaces

This folder holds the **sandboxed execution workspace** for each project:
`execution_workspaces/{project_id}/`. It is where the **Coding Agent** (the
"hands") actually does work — editing code, running bounded shell commands, and
recording what it did. The main agent (the "brain") never edits anything here
directly; it only reads concise run summaries.

Everything the Coding Agent does is routed through `ProjectSandbox` +
`ToolRuntime`, which confine it to this project's workspace (no `..`, no
absolute paths, no touching other projects, no destructive commands without
confirmation).

## Layout of each `execution_workspaces/{project_id}/` folder

```
execution_workspaces/{project_id}/
├─ repo/        ← the project's actual codebase; the Coding Agent edits here
├─ runs/        ← per-run artifacts (one subfolder per run)
│   └─ {run_id}/
│       ├─ task_card.md     ← the task the agent was given
│       ├─ events.jsonl     ← step-by-step event log for the run
│       ├─ run.json         ← structured run record (status, files, commands…)
│       ├─ result.md        ← human-readable result summary
│       └─ screenshots/     ← optional browser-verification screenshot(s)
├─ logs/        ← runtime logs
├─ AGENT.md     ← who the Coding Agent is and how it must operate (seeded once)
└─ TASK.md      ← current objective, queue, progress, verification config
```

`AGENT.md` and `TASK.md` are seeded once at workspace init and then belong to
the project — they are **not** overwritten on subsequent inits. `TASK.md` also
carries the optional `## Verification` and `## Browser Verification` blocks
that drive the post-run command / headless-browser smoke checks.

## What's committed vs. private

Real workspaces (including `repo/`, `node_modules/`, run artifacts, and logs)
are **gitignored** — your code and generated artifacts stay local. This repo
ships sanitized templates of the two seeded files so the layout is documented:

- `AGENT.example.md` — the default `AGENT.md` template
- `TASK.example.md` — the default `TASK.md` template (with the verification
  blocks)

In a real install these live inside one `execution_workspaces/{project_id}/`
folder; they're at the root here only so they can be committed as examples
without shipping a real workspace. The `{project_id}` / `{name}` placeholders
are filled in automatically when a workspace is initialized.
