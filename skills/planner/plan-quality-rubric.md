# Plan Quality Rubric
> What a plan must cover before it is worth executing.

Score each criterion; a plan is dispatch-ready only when all are at least "good".

- **Outcome clarity** — good: the end state is stated as observable behavior, not activity ("users can reset passwords", not "work on auth").
- **Bounded tasks** — good: every task names its scope and out-of-scope; none is open-ended or multi-goal.
- **Sequencing** — good: dependencies are explicit; nothing waits on an undefined earlier result; parallelizable work is marked.
- **Verifiability** — good: each task has a done-check a reviewer could run without asking the author.
- **Risk placement** — good: unknowns and irreversible steps are surfaced early, with a fallback or decision point named.
- **Right size** — good: 3–7 tasks; the first is startable today with no missing inputs.
- **Assumptions logged** — good: guesses about environment, data, or intent are written down, not silent.

If any criterion is weak, revise the plan before recommending execution.
