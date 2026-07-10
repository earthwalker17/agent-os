# Contributing to Agent OS

Thanks for your interest in Agent OS. This is a **local-first AI project
cockpit** — the harness that wraps a coding model with the memory, sandbox,
verification, and approval gates it needs to finish real work reliably. That
framing shapes everything below: contributions are welcome, but they have to
respect the boundaries that make the system trustworthy.

Before you start, please skim:

- [`README.md`](./README.md) — what Agent OS is and how to run it.
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — the system's current shape: files,
  pipelines, and the invariants you must not break.
- [`CLAUDE.md`](./CLAUDE.md) — the operating constitution (sandbox boundary,
  memory policy, execution policy, context hygiene). It applies to human and
  AI contributors alike.
- [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md) — how we work together.

## Guiding principles

Agent OS is opinionated on purpose. A change is a good fit when it strengthens
one of these; it is a poor fit when it erodes one:

- **Local-first.** Filesystem + SQLite + FastAPI + React. No cloud services,
  queues, or external infrastructure for the core. Prefer the simple local
  option (ThreadPoolExecutor over Celery, SQLite over Postgres, polling over
  SSE) until there is a concrete reason to do otherwise.
- **Sandboxed execution.** Every repo path and shell command routes through
  the single `ProjectSandbox` → `ToolRuntime` chokepoint. No raw
  `os` / `pathlib` / `subprocess` access to repo paths, ever.
- **Explicit approval gates.** Nothing pushes code, mutates an external system,
  or spends money from inferred intent. Delivery, deploys, migrations, and
  payments are two-phase preview → confirm contracts.
- **Human control.** The user stays in the loop. Features that remove a
  decision point, auto-inject repo contents into the main agent, or let an
  agent write outside its lane will be declined regardless of quality.

If you're unsure whether an idea fits, open a
[Discussion](https://github.com/earthwalker17/agent-os/discussions) or a
[feature request](https://github.com/earthwalker17/agent-os/issues/new/choose)
before writing code.

## Development setup

Requirements: **Git**, **Python 3.10+**, **Node.js 18+** (CI builds on Node 20).

```bash
# Backend
cd backend
pip install -r requirements.txt
python -m playwright install chromium   # only needed for browser verification
cp .env.example .env                    # add at least one provider key to run the app

# Frontend (second terminal)
cd frontend
npm install
npm run dev
```

The full quick start (including the one-command Windows installer) lives in the
[README](./README.md#quick-start). You do **not** need any API keys to run the
test suite — only to run the app itself.

## Running the checks

Both of these run in CI on every pull request, so run them locally first:

```bash
# Backend — the full suite, no API key needed (llm.chat is stubbed everywhere)
cd backend
pip install -r requirements-dev.txt
python -m pytest tests -q
python tests/test_sandbox_git.py     # any file is also runnable standalone

# Frontend — the production build (tsc + vite)
cd frontend
npm ci
npm run build
```

## Making a change

- **Keep changes small and bounded.** Touch the files the task names; propose
  refactors separately. No silent renames, surprise API changes, or drive-by
  cleanup of unrelated modules.
- **Preserve existing behavior unless the change is the point.** Sequential and
  single-task execution paths are expected to stay byte-identical where the
  docs say so.
- **Match the surrounding style** of the file you edit — indentation, import
  order, naming, comment density.
- **Add tests near the affected feature.** Tests live in per-feature files
  under `backend/tests/`, are standalone-runnable, and stub the LLM caller.
  Add or update the ones your change affects.
- **Respect the invariants** in [`ARCHITECTURE.md` §9](./ARCHITECTURE.md) and
  the roles in [`CLAUDE.md` §3](./CLAUDE.md). The sandbox boundary, memory
  policy, and execution policy are constitutional, not negotiable per task.
- **Follow the documentation policy.** Five docs, each with one job (see
  [`ROADMAP.md`](./ROADMAP.md)): update the README only on a user-visible
  change, record history in `ROADMAP.md`, update `ARCHITECTURE.md` if a
  module / pipeline / invariant changed, and never duplicate content across
  docs. User-facing changes also get a `CHANGELOG.md` entry under `Unreleased`.

## Pull request process

1. Fork the repo and create a topic branch from `main`.
2. Make your change with tests, and run the backend suite + frontend build.
3. Open a pull request against `main` and fill out the
   [PR template](./.github/PULL_REQUEST_TEMPLATE.md). Link the issue it closes.
4. CI (`backend-tests` and `frontend-build`) must pass, conversations must be
   resolved, and history is kept linear — PRs are **squash-merged**, so your
   commit messages inside the branch are for reviewers; the squash title is
   what lands on `main`. Write it in the imperative mood
   (e.g. "Add X", "Fix Y").
5. A maintainer reviews and merges. The branch is deleted automatically.

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](./LICENSE), the same license that covers this project.

## Reporting bugs and security issues

- **Bugs and feature requests:** use the
  [issue templates](https://github.com/earthwalker17/agent-os/issues/new/choose).
  Always redact API keys, tokens, and other secrets from logs and screenshots.
- **Security vulnerabilities:** do **not** open a public issue. Follow
  [`SECURITY.md`](./SECURITY.md) and report privately through GitHub's
  Private Vulnerability Reporting.

Thanks for helping keep the model on a leash. 🧭
