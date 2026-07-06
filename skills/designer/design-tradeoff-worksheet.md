# Design Tradeoff Worksheet
> A structured way to compare candidate designs honestly.

1. State the problem in one sentence, plus the constraint that hurts most (time, complexity budget, team size, compatibility).
2. List 2–4 candidate designs. Always include the "boring" baseline: do less, or extend what exists.
3. For each candidate, fill the same row of criteria: implementation effort, operational complexity, failure modes, migration/rollback cost, how hard it is to change later.
4. Name what each candidate makes easy AND what it makes hard — a design with no listed downside is under-analyzed, not perfect.
5. Identify the reversible vs. irreversible parts. Prefer designs whose risky bets are reversible.
6. Pick one, write a 2–3 sentence rationale, and record the trigger that would reopen the decision ("revisit if X").

## Caveats
- Weight criteria before scoring, or the comparison quietly favors your first idea.
- If two options score close, choose the simpler one and move on.
