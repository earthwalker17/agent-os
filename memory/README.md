# `memory/` — Global Memory

This folder holds **Agent OS's global, cross-project memory** — the small set
of markdown files the main agent loads on every turn to stay grounded in who
the user is, how they like to work, and what's happening across all projects.
It is global on purpose: project-specific state lives under `projects/{id}/`
instead (see `projects/README.md`).

Memory here is **structured, human-readable markdown** — not raw chat logs.
After most chat turns a second LLM call proposes concise, policy-filtered
updates to these files. The files stay short and editable by hand.

## Files

| File          | Purpose                                                        | Written by                |
|---------------|----------------------------------------------------------------|---------------------------|
| `SOUL.md`     | The agent's identity anchor — role, philosophy, behavioral rules. **Read-only, hidden in the UI, never auto-written.** | Human only |
| `USER.md`     | Who the user is: name, role, focus.                            | Human + memory writeback  |
| `WORKSTYLE.md`| How the user likes to collaborate, communicate, and build.     | Human + memory writeback  |
| `MEMORY.md`   | Cross-project memory: active projects + durable learnings.     | Human + memory writeback  |

`SOUL.md` is special: it is loaded as the identity anchor on every turn, is
never shown in any UI, and is never part of any write path. It contains no
private data, so it **is** committed to this repo as a real, working example.

## What's committed vs. private

To protect privacy, the real `USER.md`, `WORKSTYLE.md`, and `MEMORY.md` are
**gitignored**. This repo ships sanitized templates instead:

- `USER.example.md`
- `WORKSTYLE.example.md`
- `MEMORY.example.md`

Copy each `*.example.md` to the same name without `.example` (e.g.
`USER.example.md` → `USER.md`) and fill in your own details to get started.
`SOUL.md` is committed as-is and can be used directly.
