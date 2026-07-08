# Agent OS — Roadmap & Evolution Log

> **Read order for a new session:** `CLAUDE.md` (stable rules) →
> `ARCHITECTURE.md` (the system's current shape) → this file (how it got here
> + what's next). `README.md` is the public landing page.

A chronological evolution log: what landed, plus current constraints and next
steps. How the system works now lives in `ARCHITECTURE.md`, not here.

---

## Documentation Policy

Five docs, each with one job — never duplicate content across them.

| File | Audience | Job |
|------|----------|-----|
| `README.md` | Public visitors | Short pitch + setup. Bump only on user-visible change. |
| `ARCHITECTURE.md` | Any agent picking up the repo | Current shape: files, pipelines, invariants, lessons. |
| `ROADMAP.md` | The builder + future sessions | Evolution log, constraints, next steps. |
| `BLUEPRINT.md` | The builder + future sessions | Long-term identity, non-negotiable principles, next direction. |
| `CLAUDE.md` | Any coding agent here | Stable operating rules. Phase-independent. |

---

## Phase 1–2 — Foundation

The local-first cockpit skeleton: workspace, projects, conversations, memory,
one orchestrated chat loop.

- Local filesystem + SQLite; FastAPI backend; React three-column UI;
  project/conversation CRUD with auto-titling.
- Structured markdown memory, UI-editable: global (`SOUL`, `USER`,
  `WORKSTYLE`, `MEMORY`) + per-project (`PROJECT`, `STATUS`, `TASK_QUEUE`,
  `DECISIONS`, `RESEARCH`); `SOUL.md` read-only + hidden, loaded every turn.
- Semantic memory writeback: a second, policy-filtered LLM call proposes
  structured updates; the backend validates them before writing.

## Phase 3 — Execution Layer

A sandboxed Coding Agent to delegate to, made safe and explicit, surfaced in
the UI, with the loop closed by verification and preview.

- `ProjectSandbox` + `ToolRuntime` as the single chokepoint: a bounded JSON
  tool loop (six capped file/shell tools) with per-run artifacts.
- `@code <task>` dispatches (rejected in GENERAL); runs finalize off-thread
  (crash → `failed`); Runs panel + run detail modal in the UI.
- The LLM delegation judge only ever *proposes* — a confirmable pending plan
  dispatched only by a user click, with a rule-based heuristic fallback.
- Closing the loop: post-run memory reconciliation, bounded on-demand file
  inspection (never auto-injected), command verification + repair gating
  `completed`, a first browser verification with per-project previews.

## Phase 4 — Interface & UX

Composer and provider upgrades on the chat surface.

- Multi-modal composer: auto-growing textarea (`Ctrl/Cmd+Enter`), voice
  dictation, file attachments with "add to workspace" (sanitized + deduped).
- Provider Registry 2.0: six providers (Claude / GPT / Gemini / DeepSeek /
  Kimi / GLM), key-presence availability, per-model `vision` flags gating
  image upload + visual judgment; Anthropic via SDK, others via `urllib`.
- Provider selector + model picker; light/dark theme via CSS variables.

## Phase 5 — Execution Orchestration (plan → tasks → execute)

Upgraded the Coding Agent from a flat loop into a phased, inspectable run —
additive; run record, role separation, sandbox, verification tail unchanged.

- Plan → tasks → finalize: a complexity gate keeps simple cards
  byte-identical; complex cards plan via a read-only inspection loop, falling
  back to a single task; dependency-ordered units with failure skips.
- Live timeline + task checklist, cooperative Cancel, linked Retry, a
  real-time trace modal, progressive live metrics (polling only).
- Browser verification v2: render-gated multi-page capture + a
  diagnostic-only AI visual judgment (skips without a vision model).
- Hardened on Windows via adversarial-review and real-build passes; builds
  complete autonomously end to end (the **Aegis Launch Control** showcase).

## Phase 6 / 6.1 — Main Agent Orchestration & Memory v2

Rebalanced toward the Main Agent; recovery stays confirmable-only except the
scoped, user-granted budget below.

- Memory v2: one engine owns the single atomic markdown write path (duplicate
  writers removed); one structured intake judgment per meaningful turn, its
  reason surfaced to the UI; context compaction as memory grows.
- Intent Router v2: deterministic mode commands (`@plan` `@design` `@debug`
  `@review` `@inspect` `@memory`) shape the response — none dispatch a run.
- Confirmable recovery: a non-green run yields one bounded proposed step as a
  confirmable pending card; confirming can also grant an auto-recovery budget
  (none/1/2): hard cap 2, idempotent, clamped at the confirm endpoint — the
  only auto-dispatch path. Inferred intent never runs code.

## Phase 7 — Project Ops & GitHub Lifecycle

A finished run becomes a traceable, user-approved Git/GitHub delivery, every
external or destructive step behind an approval gate. (BLUEPRINT Pillar 1.)

- One Git executor (`run_git`; `run_shell` blocks destructive Git):
  checkpoint, redacted diff, secret-refusing commit, gated rollback; GitHub
  over REST; `credentials.py`-held tokens reach git only via `GIT_ASKPASS`.
- Two-phase External Action Contracts for commit / push / PR / rollback; the
  Main Agent sees a compact git-state summary, never the raw diff.
- Validated live end to end: build → verify → checkpoint → commit → branch →
  push → PR → rollback, secret-leak audit clean; the pass caught and fixed
  one real bug (commit-message validation rejected newlines).

## Phase 8 — Production Path

One golden path from local preview to a shipped real SaaS — Vercel + Supabase
+ Stripe (test-mode) — behind two-phase contracts. (BLUEPRINT Pillar 2.)

- Credential spine: a multi-provider secret registry (Stripe live-gate,
  hardened redaction) + a presence-only registry for the built app's env vars.
- Connectors: Vercel async deploy + `OPS.md` ledger (sole deterministic
  writer); a sandboxed scrubbed-env Supabase CLI executor; Stripe
  provisioning (webhook secret never echoed); crash-left deploys reconciled
  at startup, never auto-retried.
- Validated live end to end: built **CloudNotes** (Next.js + Supabase
  Auth/RLS + Stripe test checkout) from an empty repo → Vercel production
  deploy → live DB migration → a signed `checkout.session.completed` hit the
  deployed webhook and persisted a row; audit clean, three real bugs fixed.

## Phase 9 — Agent Teams & Parallel Execution

The execution layer became a team runtime; sequential paths stay
byte-identical. (BLUEPRINT Pillar 3.)

- Role registry with enforced `allowed_tools`; isolated overlay patch
  workspaces (blocked shell/git); wave-layered planning + a conservative
  eligibility gate; a ≤3-unit scheduler, coordinator as sole writer.
- Deterministic integration: first-writer-wins per wave; conflicts and
  apply-errors surfaced — never silent — capping the run at `partial`.
- Validated live end to end: a 5-task team run with live Claude ran three
  coders concurrently in isolated workspaces (isolation proven), integrated
  zero-conflict to a passing global gate → `completed`; audit clean.
- An adversarial review pass caught and fixed 6 real defects (a task-id path
  escape and a too-narrow `.git` write guard most notable).

## Phase 9.1 — Execution-Layer Hardening Pass

A pressure test of the whole execution stack after Phase 9: a multi-agent
adversarial sweep, findings re-verified against the code and fixed in place.

- Sandbox: Windows path normalization closed `.git`/sensitive-name bypasses;
  device names screened; sequential-loop role enforcement.
- Concurrency: a per-run record lock closes read-modify-write races; an
  atomic claim makes double-confirm dispatch exactly one run; state sweeps
  clear stuck/terminal runs while preserving settled verdicts.
- Validated live on real Windows through Agent OS itself — team build, PM
  delegation loop, confirm races, mid-run cancel, Git path, crash recovery;
  secret-leak audit clean (7 tracked secrets, 101 files, zero leaks).
- An adversarial self-review of the hardening diff itself caught and fixed 4
  regressions (a sweep that wiped settled browser verdicts most notable).

## Phase 10 — Research, Skills & Agent Discovery

Agent discovery, a skills foundation, a safe approval-first web-research
channel; the explicit command now gates the network. (BLUEPRINT Pillar 4.)

- Agent profile registry (10 profiles) behind one API — the single source for
  the composer `@` autocomplete and a two-pane Agents browser.
- Skills: 18 committed markdown files behind one registry-validated
  read/write path, folded into mode guidance; only the user's Save writes.
- Safe `@search`/`@research`: the explicit command is the per-turn network
  grant; snippets-only search (Tavily), SSRF-screened + allowlisted + capped
  fetches, an egress guard refusing credential-shaped values; semantic
  research intent only yields a suggestion chip — sending it is the grant.
- An adversarial review pass caught and fixed 7 real defects (a fetcher that
  could raise mid-read and an unredacted egress-refused URL most notable).

## Phase 10.2 — Live Research Validation, Skill Patches, Local RAG

Research validated live, review-first self-improvement, minimal local
retrieval, an upgraded project-memory scaffold. (BLUEPRINT Pillars 4–5.)

- Validated live (real Claude + Tavily): bounded, cited results persisted in
  message metadata; a natural-language request stayed approval-first.
- Suggested skill patch: after a green run a gated judge may *propose* an
  append-style refinement; the user's Apply is the only write path.
- Minimal local RAG: bounded, redacted keyword retrieval over project memory,
  run history, a sandbox-safe repo map — no full-repo injection.
- Memory upgrade: `TASK_QUEUE.md` folded into a `## Task Queue` board in
  `STATUS.md` (idempotent migration); new `LESSONS.md` (Main-Agent-only).
- An adversarial review pass caught and fixed 5 real defects (a migration
  dropping pre-heading content and unredacted retrieval hits most notable).

## Phase 11 — Recovery Matrix & Interactive Browser Verification

Typed, evidence-driven, auditable recovery; browser verification became a
bounded, declarative interaction layer — not computer use. (Pillar 5.)

- Recovery Matrix: 8 frozen typed contracts, a deterministic classifier, a
  bounded redacted evidence builder — computed before the LLM, attached to
  every recovery task card; environment failures never burn a budget.
- Declared interactions: views + flows (fixed vocabulary, ≤6 views / ≤2
  flows / ≤10 steps, same-origin); console/network evidence redacted;
  credential-shaped fill values refused in-parent; a failed flow fails it.
- Repair loops: a `completed` run with a failed visual verdict is now
  recover-eligible; a visual/runtime child gets one re-verified pass;
  confirm-only types never auto-dispatch; manual recovery threads lineage +
  contract-clamped budgets.
- Validated live on real Windows (real Claude + Playwright): declared flows
  ran with per-step screenshots; console/network evidence never failed a
  passing verification; a sabotaged flow → `partial` → a typed assessment
  finding the true root cause; repair → re-verify restored `completed`.
- An adversarial review pass fixed 1 real defect + 4 near-misses
  (truncate-before-redact orderings that could leak a stored secret).

## UI Polish Pass

Frontend-only visual/UX overhaul; no behavior change (verified by build, a
Playwright sweep of both themes, and a behavior-preservation review pass).

- One design-token layer with a full light theme, a status spectrum readable
  in both themes, and small-caps mono labels; all pre-polish class names kept.
- Settings modal (provider/theme/model moved from the header); collapsible
  sidebar (icon rail, state preserved); inline SVG icons; restyled
  components; structured empty states; capped chat width.

## Pre-Launch Hardening & Release (v1.0)

The final pre-launch pass: a full repo audit, one definitive production E2E
driven through Agent OS itself, public-facing docs, and a Windows one-command
installer.

- **The Pulseboard showcase E2E.** A product-feedback & roadmap SaaS
  (Next.js 14 App Router + Supabase Postgres/Auth/RLS + Stripe test-mode
  subscriptions) taken from an empty repo to a live production deployment
  through chat alone: judged plan → confirmable dispatch → multi-run build
  (surviving a mid-run API-credit outage and a backend kill — both swept
  clean) → interactive browser verification (declared `submit-feedback` flow,
  per-step screenshots) → an AI-visual-review-caught rendering bug fixed and
  re-verified to `passed` → a typed `runtime` recovery repairing a broken dev
  command (budget-clamped, linked lineage) → commit/push → Vercel production
  deploy → Supabase migration (tables + RLS verified live) → Stripe
  provisioning + signed webhook → **a real checkout on the live site flipping
  `profiles.plan` to `pro` in the live database**. Live at
  agent-os-sample-project.vercel.app.
- **The E2E caught and fixed 4 real defects**, each with a regression test:
  `credentials.status()` hid project-scoped fields whenever the primary token
  resolved from env; an inspect/research directive embedded in narration was
  silently dropped — never executed, with the raw protocol text (and fabricated
  "results") leaking into the visible reply (both parsers now extract embedded
  requests); the skill-patch judge's agent-qualified skill ids were rejected
  instead of normalized; and a project's own keep-alive preview holding the
  dev port failed the next verification's pre-flight (the guard now stops the
  project's OWN managed preview and proceeds — foreign listeners still fail).
- **Post-release fixes.** A transient network failure during the deploy
  READY-poll no longer discards the created deployment's identity from the run
  record (it orphaned a live deployment from the UI and redeploy/rollback);
  the Links panel falls back to Vercel's deployment list when no run carries a
  URL; and the composer `@`-command menu no longer inherits the Send button's
  accent background (unreadable options).
- **Release hardening.** `backend/.env.example` now documents every global
  token the code honors (model providers, Tavily, GitHub/Vercel/Supabase/
  Stripe); a one-command Windows installer (`install.ps1`) + launcher
  (`start.ps1`); README rewritten as a public landing page around the
  Pulseboard showcase; ARCHITECTURE/ROADMAP compressed; BLUEPRINT reduced to
  the open Phase 12 + future directions; CLAUDE.md compressed to a <200-line
  constitution; the retired demo workspaces (Aegis Launch Control, the
  Phase 8/9 E2Es) removed from the repo and the local cockpit; secret audit
  clean.

---

## Current Constraints

- **Explicit dispatch only.** `@code` runs immediately; inferred intent only
  produces a confirmable pending plan (GENERAL rejected).
- **Bounded parallelism.** Team plans run ≤3 concurrent units per wave;
  others stay single-threaded byte-identical; parallel units never run shell.
- **Verification gates status.** Command verification + bounded repair gate
  `completed`; browser flows are opt-in + bounded (≤2×10 steps, same-origin,
  no credential input); a failed flow fails it, console evidence never does.
- **Recovery is typed and contract-bounded.** Auto-recovery (budget ≤2,
  idempotent) also needs an auto-eligible contract; visual/runtime children
  get one pass; confirm-only types + environment failures never auto-run.
- **No streaming** on `/api/chat`; 3–4 LLM calls per non-`@code` turn.
- **Main agent never auto-reads repo contents** — only the bounded inspection
  loop (max 3 reads per turn).
- **Web access only on an explicit grant** — ≤4 requests, 16k chars, SSRF +
  allowlist screening per `@search` turn; user URLs bypass only the allowlist.
- **Single-user, single-process.** No auth, no shared deploy.

---

## Test Coverage

As of this release the backend suite is **826 tests across 59 files** under
`backend/tests/`: every file is standalone-runnable (`python tests/<file>.py`),
`llm.chat` is stubbed (no API key needed), and the whole suite runs with
`python -m pytest backend/tests`. Coverage spans the sandbox/executor
chokepoint, the delegation → pending-plan → confirm pipeline, the memory
engine, the team runtime, and the research/recovery layers (SSRF/egress
matrix, browser-interaction grammar). `npm run build` (tsc + vite) is kept
green alongside the suite.

---

## Recommended Next Steps

**Phase 12 — Launch / Growth Studio** — scoped as Pillar 6 in `BLUEPRINT.md`.

**Queued increments.**
- Skill-patch surfacing in chat + new-skill targets behind explicit review;
  RAG ranking + run-diff retrieval; Brave adapter; extensible fetch allowlist.
- Phase 11 increment 2: deployment/database repair automation behind existing
  contracts; failure-pattern memory in `LESSONS.md`; run-health over lineage.
- Streaming (SSE on `/api/chat`), then a run event stream replacing polling;
  cost/latency work on the 3–4 LLM calls per turn.

**Longer-term, not committed.** Deeper team parallelism; hard cancellation of
in-flight subprocesses; cross-project memory linking; multi-user deploy.

---

## How to use this document

1. Read `CLAUDE.md` → `ARCHITECTURE.md` → the newest entries here.
2. Don't re-litigate decisions recorded above without new information.
3. When a task lands, update the right doc(s) per the Documentation Policy.
