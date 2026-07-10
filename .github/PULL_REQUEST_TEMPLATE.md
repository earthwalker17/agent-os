<!--
Thanks for contributing to Agent OS! Please fill this out so reviewers can
move quickly. Keep changes small and bounded (see CONTRIBUTING.md).
PRs are squash-merged, so the PR title becomes the commit on `main` — write it
in the imperative mood (e.g. "Add X", "Fix Y").
-->

## Summary

<!-- What does this PR do, and why? -->

## Related issue

<!-- e.g. Closes #123 -->
Closes #

## Type of change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that changes existing behavior)
- [ ] Docs / tooling / CI only

## How was this tested?

<!-- Describe what you ran. -->

- [ ] `cd backend && python -m pytest tests -q` passes
- [ ] `cd frontend && npm run build` passes
- [ ] Added or updated tests near the affected feature

## Checklist

- [ ] The change is small and bounded — it touches the files the task names and avoids unrelated refactors/renames.
- [ ] I preserved existing behavior unless the change is the point of the PR.
- [ ] I respected the invariants in [`ARCHITECTURE.md` §9](https://github.com/earthwalker17/agent-os/blob/main/ARCHITECTURE.md) and the agent roles / policies in [`CLAUDE.md`](https://github.com/earthwalker17/agent-os/blob/main/CLAUDE.md) — the sandbox boundary, memory policy, execution policy, and approval gates are intact.
- [ ] No secrets, `.env` values, credential-store files, generated artifacts, or dependency directories are included in the diff.
- [ ] I updated the docs per the [documentation policy](https://github.com/earthwalker17/agent-os/blob/main/ROADMAP.md) and added a `CHANGELOG.md` entry under `Unreleased` if the change is user-visible.
