# Task Breakdown Checklist
> How to split a request into bounded, dispatchable tasks.

1. Restate the goal in one sentence; note the user-visible outcome.
2. List hard constraints (deadlines, stack, no-touch areas, budgets).
3. Split the work into tasks that each: fit one focused session, touch a small named surface area, and have a single verifiable result.
4. For each task write: title, intent (why), scope (what changes), out-of-scope (what must not change), and a concrete done-check.
5. Order tasks by dependency; mark which can run in parallel and which block others.
6. Put risky or unknown-heavy tasks first as thin spikes so failures surface early.
7. Attach a verification step to every task (test, build, manual check) — never "done when written".
8. Flag tasks needing a human decision or credentials before dispatch.

Caveats: prefer 3–7 tasks; if a task has "and" in its title, split it; if two tasks always ship together, merge them.
