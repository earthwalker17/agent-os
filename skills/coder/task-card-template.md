# Task Card Template
> What a well-formed task card gives the Coding Agent.

Fill in every section before dispatching; a vague card produces a vague run.

1. **Goal** — one sentence: the user-visible outcome, not the code change.
2. **Context** — 2-3 bullets: what exists now, why this is needed, relevant prior decisions.
3. **Scope** — the specific files, modules, or features to touch. Name them.
4. **Out of scope** — what must NOT change (APIs, styling, unrelated modules).
5. **Acceptance criteria** — 2-5 observable checks, each verifiable by running or inspecting the result ("clicking X shows Y", "test suite passes").
6. **Constraints** — stack, dependencies allowed, performance or compatibility limits.
7. **Verification** — the exact commands or manual steps that prove it works.

## Quality bar
- One task card = one coherent change; split anything bigger.
- Criteria are testable, never "improve" or "clean up".
- If you cannot write acceptance criteria, the task is not ready to dispatch.
