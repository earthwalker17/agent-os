# Risk Hotspot Checklist
> Where defects cluster: boundaries, state, concurrency, security.

1. Boundaries: check every off-by-one candidate — loop limits, slices, pagination, date ranges, inclusive vs exclusive comparisons.
2. Input edges: validate handling of empty, null, whitespace, max-length, malformed, and duplicate inputs at every external entry point.
3. State transitions: find where state is written twice or read stale; confirm invariants hold after every early return and exception.
4. Concurrency: look for shared mutable state, check-then-act races, missing locks, and non-idempotent retries.
5. Resource lifecycles: files, connections, and handles closed on all paths, including errors.
6. Security: user input reaching queries, shells, paths, or templates unescaped; secrets in logs, errors, or URLs; authz checked on every route, not just the UI.
7. Time and encoding: timezone math, DST, locale, and unicode normalization.

Weight review effort toward code that is new, recently changed, or rarely executed — that is where defects hide; stable hot paths need less scrutiny.
