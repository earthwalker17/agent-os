# Agent OS — Long-Term Blueprint

> Strategic direction for Agent OS. Read after `CLAUDE.md`, `ARCHITECTURE.md`,
> and `ROADMAP.md`. Unlike `ROADMAP.md` (the evolution log), this file records
> the durable product identity, the non-negotiable principles that govern any
> future work, and what comes next.

_Last updated: 2026-07-08 — Phases 1–11 are delivered; the original Phase 7–11
plans this document once carried are now history and live in `ROADMAP.md`._

---

## 0. Why this document exists

Agent OS grew phase by phase from a chat cockpit into a full build → verify →
preview → deliver → deploy → recover pipeline. With Phases 1–11 complete, five
of the six long-term pillars this blueprint originally defined are built and
validated live. This file keeps future sessions honest about two things: the
**identity** Agent OS must not drift from, and the **remaining direction**
(Phase 12 and beyond) so new work extends the system instead of re-inventing it.

---

## 1. Target identity

> **A local-first AI Project Operating System: an end-to-end project cockpit
> for planning, building, verifying, versioning, deploying, maintaining, and
> launching real software projects while preserving user control and
> auditability.**

Broader than an AI coding assistant, narrower than an uncontrolled general
automation platform. The durable advantages:

- **Local-first control.** Filesystem, SQLite, FastAPI, React, local workspaces,
  simple process management — heavier infrastructure only on concrete need.
- **Clear agent boundaries.** The Main Agent is the brain: planner, memory
  steward, orchestrator, reviewer. The Coding Agent is the hands: a bounded
  repo executor inside one project workspace.
- **Auditable execution.** Every run leaves durable artifacts: task card, plan,
  events, result, verification, screenshots, visual review, recovery
  assessment, Git and deployment records.
- **Explicit authority.** High-impact actions are gated by dry-run previews,
  user-visible contracts, and confirmation. Inferred intent never silently
  mutates code, external platforms, money flows, production data, or public
  posts.

---

## 2. Non-negotiable system principles

These govern all future work. They extend the current architecture rather than
replacing it (the concrete invariants live in `ARCHITECTURE.md §9`).

### 2.1 Keep the sandbox boundary
All repo path access and shell execution route through the sandbox / tool
runtime chokepoint. New capabilities must not introduce parallel raw-shell
paths; they become first-class tool layers with validation, audit records, and
explicit permission models.

### 2.2 Keep role separation
The Main Agent never edits `repo/` or runs shell commands; it inspects bounded
files, drafts plans, proposes contracts, reviews diffs, coordinates recovery,
and summarizes outcomes. The Coding Agent never writes project or global
memory; its artifacts flow back through reconciliation and the controlled
memory engine.

### 2.3 Contract-first external actions
Anything that affects an external system — Git push, PR, deployment, migration,
webhook/payment setup, domain or env changes, social publishing, asset export —
is an **External Action Contract**: action type, target platform, requested
permission, affected resources, dry-run result where available, rollback path
where available, explicit user confirmation, and an audit record.

### 2.4 Audit before autonomy
Agent OS may become more autonomous only as it becomes more auditable. Every
new autonomous loop must leave evidence: what it saw, what it changed, why, how
it verified the change, and what it refused to do.

### 2.5 Narrow golden paths before broad plugin coverage
Prefer a small set of well-integrated, high-value paths (Git/GitHub, Vercel,
Supabase, Stripe test mode — delivered; Canva/HyperFrames and selected social
platforms later) over shallow support for every platform.

---

## 3. The six pillars — status

| Pillar | Theme | Status |
|--------|-------|--------|
| 1 | **Project Ops Layer** — audited Git/GitHub lifecycle: checkpoint, diff, commit, branch, push, PR, rollback, run↔commit↔PR linkage | ✅ Delivered (Phase 7 — `ROADMAP.md`, `ARCHITECTURE.md §7.H`) |
| 2 | **Real Backend & Deployment** — the Vercel + Supabase + Stripe-test golden path: env/secret registry, deploy/migration/webhook contracts, validated live with a real SaaS | ✅ Delivered (Phase 8 — `ARCHITECTURE.md §7.I`) |
| 3 | **Agent Teams Runtime** — role registry, wave-scheduled parallel execution in isolated patch workspaces, deterministic integration, global verification gate | ✅ Delivered (Phase 9 — `ARCHITECTURE.md §5`) |
| 4 | **Research, RAG & Skills** — grant-gated web research, local RAG over memory/runs/repo, curated skills with review-first skill patches | ✅ Delivered (Phase 10 — `ARCHITECTURE.md §7.J`) |
| 5 | **Long-horizon Recovery & Self-healing** — typed Recovery Matrix, evidence-driven repair, interactive browser verification, bounded auto-recovery | ✅ Delivered (Phase 11 — `ARCHITECTURE.md §7.E2`) |
| 6 | **Launch & Growth Studio** — turn shipped projects into explainable, shareable, launchable ones | ⏳ Open (Phase 12, below) |

---

## 4. Phase 12 — Launch & Growth Studio (the open pillar)

### Goal
Help real projects become explainable, shareable, and launchable. Writing code
and deploying a product are not the end of a project: a builder also needs
diagrams, screenshots, demos, release notes, and launch materials. Agent OS
already captures browser screenshots, visual reviews, run artifacts, and
deployment records — the Launch Studio reuses that evidence.

### Direction
A project asset studio that generates, from run artifacts and project memory:

- architecture / system-map / product-flow diagrams
- screenshot gallery curation
- demo scripts and demo-video planning (HTML-to-video experiments)
- release notes and build-in-public timeline summaries
- README / landing-page polish
- platform-specific launch drafts (LinkedIn, X, Reddit, Instagram)
- a release checklist

Start with **draft generation and exported assets**. No automatic publishing
until platform permissions, preview, confirmation, and audit logs are mature —
publishing is an External Action Contract like any other.

### Possible integrations
Canva (editable diagrams/exports), HyperFrames (HTML-native demo videos),
LinkedIn / X / Reddit / Instagram (draft-first, publish behind contracts).

### Completion marker
After a verified milestone, Agent OS can generate a launch kit — README polish,
architecture diagram, screenshot set, demo script, platform post drafts, and a
release checklist — from committed artifacts, with nothing auto-published.

### Out of scope initially
Automatic public posting, engagement automation, scraping private communities,
paid-ad management.

---

## 5. Beyond Phase 12 — candidate directions

Not committed, roughly ordered by leverage:

- **Streaming + live run events.** SSE on `/api/chat`, then a run event stream
  replacing 2s polling (`llm.py`, `orchestrator.py`, chat endpoint, UI).
- **Cost / latency.** The 3–4 LLM calls per chat turn are the lever: cache the
  delegation judge, merge the memory judge into the main response, gate
  inspection more aggressively.
- **Research/RAG increments.** A second search engine (Brave) behind the same
  adapter seam; user-extensible fetch allowlists; RAG ranking beyond substring
  scoring; run-diff retrieval; skill patches that can propose NEW skills behind
  explicit review.
- **Deeper recovery automation.** Deployment/database repair behind their
  existing explicit contracts; failure-pattern memory (repeated recovery types
  distilled into `LESSONS.md`); a run-health view over recovery lineage chains.
- **Deeper team parallelism.** LLM-assisted conflict resolution beyond
  first-writer-wins; per-unit preview servers; hard cancellation that kills
  in-flight subprocesses.
- **MCP adapter.** MCP is a useful compatibility layer, not the core security
  model: Agent OS keeps its own connector registry and permission model first,
  then supports *approved* MCP servers through an adapter. Unknown MCP servers
  never get broad tool access by default.
- **Multi-user / shared deploy.** Needs auth, per-user workspaces, and a
  different database story — only when the single-builder model is outgrown.

---

## 6. What not to do

Do not convert Agent OS into an uncontrolled general automation system:

- no agents silently pushing, deploying, changing platform settings, or
  publishing posts
- no arbitrary MCP tools with broad permissions
- no unbounded RAG injection into the Main Agent context
- no auto-generated skills that change behavior without review
- no cloud infrastructure before the local-first model is exhausted
- no flashy plugins before the Git / deploy / recovery foundations they depend on

---

## 7. One-line strategic summary

Phases 1–11 made Agent OS capable of building, verifying, remembering,
delivering, deploying, coordinating agent teams, researching, and self-healing;
what remains is making the projects it ships **explainable and launchable** —
without ever giving up the leash.
