# Code Review Rubric
> What to check, in what order, and what blocks acceptance.

1. Correctness first: does the change do what the task states? Trace one real input end-to-end; name the exact line if behavior diverges.
2. Edge cases: empty input, null/None, zero, negative, unicode, oversized payloads, duplicate calls. Good code handles or explicitly rejects them.
3. Error paths: failures surface with context, resources are released, partial state is not left behind.
4. Contract fit: public signatures, return shapes, and error types match callers' expectations; no silent renames.
5. Tests: new behavior has a test that fails without the change; bug fixes add a regression test.
6. Readability: names say intent, functions stay single-purpose, dead code removed.

Blocking: any correctness, data-loss, or security defect; broken contract; untested new logic. Non-blocking: style, naming taste, optional refactors — report separately, never mixed with defects. Ground every finding in a file and line; if you cannot, mark it as a question, not a defect.
