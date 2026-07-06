# Memory Intake Rubric
> What deserves memory, what stays in chat, and why.

Score each candidate fact against these criteria; record only when most pass.

1. **Durable** — still true and useful in two weeks. Good: "auth uses JWT with 24h expiry." Bad: "tests are running now."
2. **Decision or commitment** — a choice made, with the why. Good: "chose SQLite over Postgres to stay local-first." Bad: options merely discussed.
3. **Status delta** — moves a milestone, closes or opens a task, or changes a blocker. Good: "payment flow verified end-to-end." Bad: incremental progress inside one session.
4. **Hard-won** — research findings, gotchas, or constraints that were expensive to learn and painful to relearn.
5. **Non-derivable** — cannot be reconstructed from code, docs, or a quick search.

Route it: decisions with rationale → decisions file; state and blockers → status; future work → task queue; findings → research.

Keep in chat only: speculation, small talk, transient errors, anything the next reader would skip. When unsure, prefer one tight sentence in memory over a paragraph — or nothing.
