# Interface Consistency Checklist
> Checks that keep product structure and UI coherent.

1. Naming: one concept, one name — check that the same thing isn't called "task" here and "job" there across UI, API, and docs.
2. Verbs: the same action (create, delete, confirm, cancel) looks and behaves the same everywhere; destructive actions always confirm the same way.
3. Navigation: every screen answers "where am I, how did I get here, how do I get back"; hierarchy in the UI mirrors the mental model, not the code layout.
4. State display: loading, empty, error, and success states exist for every list and form, and use the same patterns.
5. Defaults: forms and settings default to the safe, common choice; danger is opt-in.
6. Feedback: every user action produces a visible result or acknowledgment within a beat; long work shows progress.
7. API symmetry: endpoints for similar resources share shape, error format, and pagination.

Run this pass whenever a new surface is added — drift compounds; fix names first, they anchor everything else.
