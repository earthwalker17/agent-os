# Minimal-Repro Checklist
> How to shrink a failure until the cause is visible.

1. Reproduce the failure once, unchanged, and record the exact trigger: input, command, environment, and observed output.
2. Freeze everything that varies: pin versions, seed randomness, fix timestamps, use a copy of the failing data.
3. Halve the input: delete half the data, steps, or config. If it still fails, keep halving; if it passes, restore the other half and cut there.
4. Remove components one at a time — plugins, flags, middleware, concurrency — retesting after each removal.
5. Replace real dependencies with the simplest stand-in that still fails (stub service, tiny fixture file, single record).
6. Stop when removing anything else makes the failure disappear — what remains is the suspect set.
7. Write down the minimal recipe: smallest input + shortest step list + expected vs. actual result, so anyone can rerun it.

Caveat: if the failure is intermittent, first find a loop or condition that makes it reliable; shrinking a flaky repro produces false conclusions.
