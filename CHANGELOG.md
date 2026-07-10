# Changelog

All notable changes to Agent OS are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For the full engineering evolution log — every phase, constraint, and lesson —
see [`ROADMAP.md`](./ROADMAP.md).

## [Unreleased]

### Added
- Community & open-source hardening: `CONTRIBUTING.md`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), this changelog, issue /
  pull-request templates, `CODEOWNERS`, a GitHub Actions CI workflow
  (`backend-tests` + `frontend-build`), Dependabot, and CodeQL scanning.

## [1.0.0] - 2026-07-10

First public release. Agent OS is a local-first AI project cockpit — the
harness that turns a coding model into a reliable software agent: `LLM +
Harness = Agent`.

### Added

- **Local-first cockpit.** Filesystem + SQLite + FastAPI + React three-column
  UI with light/dark themes, project-scoped conversations, a new-conversation
  landing state, and a multi-modal composer (voice, attachments, `@`-command
  palette).
- **Two-agent split.** A **Main Agent** (planner / memory steward /
  orchestrator that never edits `repo/` or runs shell) and a sandboxed
  **Coding Agent** (a bounded executor confined to one project's workspace),
  communicating only through summaries.
- **Structured markdown memory.** A single atomic, policy-filtered write path;
  a per-turn intake judge that proposes updates; post-run reconciliation.
  `SOUL.md` is read-only to every agent/LLM path and editable only by the user.
- **Capability-aware provider registry.** Six providers — Claude, GPT, Gemini,
  DeepSeek, Kimi, and Zhipu GLM — with key-presence availability and per-model
  vision flags.
- **Sandbox chokepoint.** Every repo path and shell command routes through
  `ProjectSandbox` → `ToolRuntime`; six bounded file/shell tools; destructive
  shell and Git are blocked.
- **Phased run lifecycle.** Plan → execute (solo, sequential, or parallel agent
  teams in isolated patch workspaces) → command verification with a bounded
  iterative repair loop → interactive, declarative browser verification with
  per-step screenshots → diagnostic AI visual review → memory reconciliation.
- **Verification as ground truth.** A run is `completed` only after a real
  build/test passes — never on the model's say-so.
- **Typed recovery.** A Recovery Matrix classifies every failure, packages
  redacted evidence, and honors a user-granted, contract-clamped auto-recovery
  budget (≤2).
- **Audited delivery & production connectors.** Two-phase preview → confirm
  contracts for Git commit / push / PR / rollback and for Vercel, Supabase, and
  Stripe (test-mode), all recorded in a deterministic `OPS.md` ledger. Tokens
  reach Git only via a push-time `GIT_ASKPASS` env.
- **Grant-gated research, RAG & skills.** An `@search` web-research channel
  (SSRF-screened, allowlisted, cited, egress-guarded), bounded local retrieval
  over a project's own memory/runs/repo, and user-curated editable skill files.
- **Credential hygiene.** A single secret reader (`credentials.py`),
  presence-only status, exact-value + pattern redaction on every artifact, and
  a Stripe live-mode gate.
- **Windows one-command installer** (`install.ps1`) and launcher (`start.ps1`).
- **820+ backend tests** across 59 standalone-runnable files, each stubbing the
  LLM caller so the whole suite runs with no API key; `npm run build` kept
  green alongside.
- **Pulseboard showcase.** A production feedback/roadmap SaaS taken from an
  empty repo to a live, paying Vercel + Supabase + Stripe-test deployment,
  driven entirely through Agent OS itself.

[Unreleased]: https://github.com/earthwalker17/agent-os/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/earthwalker17/agent-os/releases/tag/v1.0.0
