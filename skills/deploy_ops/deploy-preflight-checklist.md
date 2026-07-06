# Deploy Preflight Checklist
> What to verify before anything leaves the machine.

1. State the delivery in one sentence: what ships, to which environment, and why now.
2. Confirm the working tree is clean — no stray edits, debug prints, or untracked files riding along.
3. Diff exactly what will ship against what is live; read every changed file name, not just the count.
4. Run the build and test suite locally; a red or skipped check blocks the delivery.
5. Verify config and env vars per target environment; never let test keys or localhost URLs reach production.
6. Scan the diff for secrets, tokens, and credentials — nothing sensitive in code, history, or logs.
7. Check migrations: reversible, ordered, and safe to run against current production data.
8. Write the rollback step BEFORE shipping — know the exact command or revert target.
9. Present the plan for explicit confirmation; ship only what was approved, nothing bundled in.

If any check is uncertain, stop and downgrade to a preview — an unshipped delivery costs minutes, a bad one costs the evening.
