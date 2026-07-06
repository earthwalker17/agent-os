# Definition-of-Done Checklist
> When a coding task is actually finished.

Run these checks in order; any failure means the task is not done, only progressed.

1. **Acceptance criteria met** — every criterion on the task card is demonstrably true, not "should work".
2. **Verified by execution** — the feature was actually run or the tests actually executed; a clean-looking diff is not evidence.
3. **No regressions** — existing tests still pass; adjacent features still behave as before.
4. **Errors handled** — bad input, empty state, and failure paths do something sane, not crash silently.
5. **Scope respected** — nothing outside the card was changed; no drive-by refactors or renames.
6. **Leftovers removed** — no debug prints, commented-out code, dead files, or TODOs masquerading as done.
7. **Result explainable** — the summary states what changed, how it was verified, and what was NOT verified.

Caveat: "partial" is an honest, acceptable outcome — report remaining gaps as explicit blockers rather than stretching "done".
