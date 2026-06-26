# Agent OS — Long-Term Blueprint

> Strategic direction for Agent OS after Phase 5 and Phase 6.
>
> This document is intended to sit in the project root as `BLUEPRINT.md`.
> It should be read after `CLAUDE.md`, `ARCHITECTURE.md`, and `ROADMAP.md`.
> Unlike `ROADMAP.md`, this file is not an implementation log. It records the
> long-term product and architecture direction so future sessions do not drift.

_Last updated: 2026-06-25_

---

## 0. Why this document exists

Agent OS has already passed two important thresholds:

1. **Phase 5 upgraded the execution layer.** The Coding Agent moved from a flat
   bounded loop into a phased run: planning, task decomposition, execution,
   verification, repair, browser preview, run artifacts, live trace, and recovery
   signals.
2. **Phase 6 / 6.1 upgraded the Main Agent layer.** The system now has Memory
   Engine v2, structured memory intake, richer intent routing, deterministic
   `@` modes, recovery assessment, and a user-approved recovery budget.

The next challenge is not simply "make the Coding Agent more powerful." The
next challenge is to turn Agent OS from a **local demo builder** into a
**local-first AI project operating system** that can manage real projects over
time: versioning, external services, production deployment, recovery, research,
skills, launch assets, and long-term maintenance.

This file defines the strategic blueprint. It is intentionally less detailed
than specific tasks. Future implementation sessions should still create clear plans and bounded tasks.

---

## 1. Target identity

Agent OS should evolve into a:

> **Local-first AI Project Operating System: an end-to-end project cockpit for
> planning, building, verifying, versioning, deploying, maintaining, and
> launching real software projects while preserving user control and auditability.**

This is broader than an AI coding assistant, but narrower than an uncontrolled
general automation platform.

The durable advantages should remain:

- **Local-first control.** Prefer filesystem, SQLite, FastAPI, React, local
  workspaces, and simple process management unless a concrete need justifies
  heavier infrastructure.
- **Clear agent boundaries.** The Main Agent is the brain: planner, memory
  steward, orchestrator, reviewer. The Coding Agent is the hands: bounded repo
  executor inside one project workspace.
- **Auditable execution.** Runs should leave durable artifacts: task card, plan,
  events, result, verification, screenshots, visual review, recovery assessment,
  and eventually Git / deployment records.
- **Explicit authority.** High-impact actions must be gated by dry-run previews,
  user-visible contracts, and confirmation. Inferred intent should not silently
  mutate code, external platforms, money flows, production data, or public posts.

---

## 2. Non-negotiable system principles

These principles extend the current architecture rather than replacing it.

### 2.1 Keep the sandbox boundary

All repo path access and shell execution must continue to route through the
sandbox / tool runtime boundary. New project operations must not introduce
parallel raw shell paths.

Future Git, deploy, API, and asset-generation capabilities should become
first-class tool layers with validation, audit records, and explicit permission
models.

### 2.2 Keep role separation

The Main Agent should not directly edit `repo/` or run shell commands. It may
inspect bounded files, draft plans, propose execution contracts, review diffs,
coordinate recovery, and summarize outcomes.

The Coding Agent should not write project memory, global memory, or other
project workspaces. It should produce artifacts that memory reconciliation can
review and persist through the controlled memory engine.

### 2.3 Contract-first external actions

Any action that affects external systems should be represented as an
**External Action Contract** before execution.

Examples:

- Git push
- GitHub PR creation or merge
- Vercel production deployment
- Supabase migration
- Stripe webhook / checkout setup
- domain or environment-variable changes
- social media publishing
- asset export to third-party platforms

A contract should show:

- action type
- target platform
- requested permission
- files / resources affected
- dry-run result when available
- rollback or recovery path when available
- user confirmation requirement
- audit record location

### 2.4 Audit before autonomy

Agent OS may become increasingly autonomous, but it should become increasingly
auditable at the same time. Every new autonomous loop should leave evidence:
what it saw, what it changed, why it changed it, how it verified the change, and
what it refused to do.

### 2.5 Prefer narrow golden paths before broad plugin coverage

Do not try to support every platform at once. Prefer a small set of
well-integrated, high-value paths:

- Git / GitHub for version control and collaboration
- Vercel for web deployment
- Supabase for database / auth / migrations
- Stripe test mode for payments
- Canva / HyperFrames later for launch assets
- selected social platforms later for draft generation and optional publishing

---

## 3. Six long-term development pillars

## Pillar 1 — Project Ops Layer

### Goal

Move Agent OS from "writes files into a workspace" to "manages project history
and delivery state."

### Why it matters

A real long-term project needs checkpoints, branches, commits, diffs, PRs,
rollback, and release records. Without this layer, Agent OS can generate code
but cannot safely maintain it over weeks or months.

### Direction

Introduce a Project Ops layer that covers:

- Git initialization and repository state detection
- status, diff, branch, commit, tag, revert, and rollback
- pre-run checkpoint creation
- post-run diff review
- generated commit messages
- run-to-commit linkage
- GitHub remote setup
- branch push
- Pull Request creation
- PR review summary
- merge readiness checks
- release notes

Git operations should be exposed as audited operations, not casual unrestricted
shell commands. GitHub operations should use an explicit connector with scoped
credentials and confirmation gates.

### Strategic completion marker

Agent OS can take a completed, verified run, show the diff, create a commit,
push it to a branch, and create a GitHub Pull Request after user approval.

### Reference notes

- GitHub REST API overview: https://docs.github.com/rest
- GitHub REST API Pull Requests endpoints: https://docs.github.com/rest/reference/pulls
- GitHub Pull Requests endpoint details: https://docs.github.com/en/rest/pulls/pulls

---

## Pillar 2 — Real Backend & Deployment Layer

### Goal

Move what Agent OS can build from preview-only local demos to production-like full-stack
delivery.

### Why it matters

A local preview is not a real product. Real projects need databases,
authentication, backend services, environment variables, migrations, deployments,
webhooks, logs, and rollback.

### Direction

Start with one modern SaaS golden path:

- frontend: Vite / React or Next.js
- backend/API: Node, FastAPI, or framework-native routes
- database/auth: Supabase
- payment sandbox: Stripe test mode
- deployment: Vercel preview and production flows
- secrets/env: project-scoped secret registry
- verification: local build + deployed smoke test + browser capture
- memory: deployment record, environment notes, and operational decisions

Do not start by supporting every backend platform. First make one path reliable.

### External Action Contract requirements

Deployment and backend actions should require special contracts:

- environment variable creation/update
- database migration
- webhook endpoint creation
- production deployment
- domain binding
- payment flow setup
- rollback

### Strategic completion marker

Agent OS can build and deploy a minimal real SaaS with database, auth, Stripe
test checkout, preview deployment, environment variables, and a recorded
deployment state.

### Reference notes

- Vercel REST API: https://vercel.com/docs/rest-api
- Vercel deployments: https://vercel.com/docs/deployments
- Vercel environment variables: https://vercel.com/docs/environment-variables
- Supabase local development: https://supabase.com/docs/guides/local-development
- Supabase local development with schema migrations: https://supabase.com/docs/guides/local-development/overview
- Supabase database migrations: https://supabase.com/docs/guides/deployment/database-migrations
- Supabase CLI reference: https://supabase.com/docs/reference/cli/introduction
- Stripe API reference: https://docs.stripe.com/api
- Stripe webhooks: https://docs.stripe.com/webhooks
- Stripe webhook endpoints API: https://docs.stripe.com/api/webhook_endpoints

---

## Pillar 3 — Agent Teams Runtime

### Goal

Move from a single-threaded Coding Agent to coordinated multi-agent execution.

### Why it matters

Current task decomposition records dependencies, but execution remains
topological and single-threaded. That is reliable, but inefficient for larger
projects. Real full-stack work can often be split into frontend, backend,
database, tests, docs, research, and review.

### Direction

Do not start by letting many agents write to the same repo at the same time.
Parallelism must be based on isolation and integration.

Recommended evolution:

1. **Parallel read-only agents.**
   Research, review, architecture, and planning agents can inspect docs, repo
   structure, logs, and prior runs without writing files.
2. **Parallel patch agents.**
   Subagents write in isolated patch workspaces, git worktrees, or branch-like
   staging areas. They do not directly mutate the main workspace.
3. **Integration Agent.**
   A dedicated agent merges patches, resolves conflicts, and prepares the final
   integrated diff.
4. **Verification Agent.**
   A dedicated stage runs global verification, browser checks, visual review,
   and deployment smoke tests.
5. **Release Agent.**
   A later agent prepares commit, PR, release notes, deployment, and launch kit.

### Agent role model

Future `@` commands should map naturally to agents and skills:

- `@plan` → Planner / PM Agent
- `@design` → Design Agent
- `@debug` → Debug / Recovery Agent
- `@review` → Review Agent
- `@inspect` → Inspector Agent
- `@memory` → Memory Steward
- `@research` → Research Agent
- `@deploy` → Deploy Agent
- `@launch` → Growth / Launch Agent

Not every role needs to become a separate process immediately. The important
step is to define role-specific prompts, skills, tools, permissions, and output
contracts.

### Strategic completion marker

Agent OS can decompose a complex build into low-coupling subprojects, run safe
subagents in parallel, merge their outputs, and pass a final global verification
gate.

---

## Pillar 4 — Research, RAG & Skills Layer

### Goal

Give Agent OS safe, selective access to external knowledge and a way to convert
repeated successful patterns into reusable skills.

### Why it matters

A real project builder cannot rely only on model intelligence. It needs to read
project docs, inspect source code, access user-approved URLs, search targeted
references, learn from open-source examples, and preserve reusable know-how.

### Direction

Start with narrow, local-first RAG:

- project memory index: `PROJECT.md`, `STATUS.md`, `DECISIONS.md`, `RESEARCH.md`
- repo index: file tree, symbols, summaries, recent diffs
- run index: prior plans, failures, fixes, verification outcomes
- user-approved URL reader
- domain-allowlisted web search
- GitHub reference reader for README / architecture / selected files
- research cache written to project memory or a bounded local cache

Avoid automatically injecting large repo contents or arbitrary web pages into
the Main Agent context. Retrieval should be targeted, cited, bounded, and
auditable.

### Skills model

A useful distinction:

- **Tool:** an executable capability, such as shell, GitHub API, Vercel API,
  web fetch, image generator, or video renderer.
- **Skill:** a reusable method, recipe, checklist, prompt pattern, evaluation
  rubric, or implementation guide.
- **Agent:** a role that combines goals, skills, tools, permissions, and output
  contracts.

Example skill families:

- frontend app scaffold
- design system setup
- Supabase schema and RLS setup
- Stripe checkout test flow
- Vercel deployment checklist
- PR review rubric
- visual repair rubric
- launch post generator
- architecture diagram generator
- demo video script generator

### Self-improvement boundary

Skills can eventually be suggested or generated by Agent OS, but they should not
silently modify core behavior. A safe path:

1. curated built-in skills
2. project-level suggested skills
3. user-reviewed skill patches
4. tested skill activation
5. optional global promotion

### MCP position

MCP is a useful compatibility layer, not the core security model.

Agent OS should first define its own connector registry and permission model.
Then it can support approved MCP servers through an adapter. Unknown MCP servers
should not be granted broad tool access by default.

### Reference notes

- Model Context Protocol introduction: https://modelcontextprotocol.io/docs/getting-started/intro
- MCP tools specification: https://modelcontextprotocol.io/specification/2025-06-18/server/tools
- Anthropic MCP announcement: https://www.anthropic.com/news/model-context-protocol
- MCP tool poisoning / prompt injection risk discussion: https://arxiv.org/abs/2603.22489
- OWASP MCP Tool Poisoning overview: https://owasp.org/www-community/attacks/MCP_Tool_Poisoning

---

## Pillar 5 — Long-horizon Recovery & Self-healing

### Goal

Upgrade recovery from "one suggested fix after failure" into a typed,
evidence-driven, auditable repair system.

### Why it matters

Large projects fail in layers. A TypeScript build error, API mismatch, missing
env var, visual bug, deployment failure, or migration issue each requires
different evidence and repair behavior.

### Direction

Create a Recovery Matrix with different repair types:

- **Build Repair:** compile/type/lint/test failures
- **Runtime Repair:** dev server, port, dependency, startup, env failures
- **Visual Repair:** blank screen, broken layout, loading spinner, missing views
- **Integration Repair:** frontend/backend contract mismatch
- **Database Repair:** migration, seed, RLS, schema mismatch
- **Deployment Repair:** Vercel build, env, domain, route, serverless issues
- **Product Repair:** feature works technically but misses user intent
- **Docs/Memory Repair:** stale docs, misleading status, unrecorded decisions

Each recovery type should define:

- accepted evidence
- allowed tools
- denied tools
- max attempts
- verification command
- user-confirmation boundary
- memory reconciliation behavior

### Visual repair as near-term bridge

The current visual judgment is diagnostic-only. A natural next improvement is
an opt-in visual repair pass:

1. capture screenshots
2. get visual verdict and evidence
3. package screenshot + route + task + console/network clues
4. run one bounded visual repair pass
5. re-capture once
6. re-judge once
7. record outcome

This should remain capped and auditable.

### Strategic completion marker

A non-green run can trigger a typed recovery path that uses the right evidence,
makes bounded changes, re-verifies, and records the recovery lineage.

---

## Pillar 6 — Launch & Growth Studio

### Goal

Help real projects become explainable, shareable, and launchable.

### Why it matters

Writing code and deploying a product are not the end of the project. A builder
also needs diagrams, screenshots, demos, release notes, social posts, and launch
materials. Agent OS already captures browser screenshots and visual reviews; the
Launch Studio should reuse these artifacts.

### Direction

Create a project asset studio with:

- architecture diagram generation
- system map generation
- product flow diagrams
- screenshot gallery curation
- demo script generation
- demo video planning
- HTML-to-video experiments
- launch announcement drafts
- README / landing page polish
- platform-specific content packs
- release notes
- build-in-public timeline summaries

Start with draft generation and exported assets. Do not publish automatically
until platform permissions, preview, confirmation, and audit logs are mature.

### Possible platform/tool integrations

- Canva for editable diagrams, design assets, and exports
- HyperFrames for HTML-native demo videos
- LinkedIn for professional launch posts
- X for short threads and build-in-public updates
- Reddit for community-specific launch drafts
- Instagram for Reels captions / visual launch material

### Strategic completion marker

After a project reaches a verified milestone, Agent OS can generate a launch kit:
README polish, architecture diagram, screenshot set, demo video script, LinkedIn
post, X thread, Reddit post draft, Instagram caption, and release checklist.

### Reference notes

- Canva Connect APIs: https://www.canva.dev/docs/connect/
- Canva Exports API: https://www.canva.dev/docs/connect/api-reference/exports/
- HyperFrames GitHub: https://github.com/heygen-com/hyperframes
- HyperFrames site: https://hyperframes.heygen.com/
- LinkedIn Posts API: https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api
- X API Create/Edit Post: https://docs.x.com/x-api/posts/create-post
- X API Manage Posts: https://docs.x.com/x-api/posts/manage-tweets/introduction
- Reddit API submit endpoint: https://www.reddit.com/dev/api/
- Instagram Graph API overview: https://developers.facebook.com/products/instagram/apis/
- Instagram content publishing: https://developers.facebook.com/documentation/instagram-platform/content-publishing

---

## 4. Recommended phase sequence

The pillars above are long-term directions. They should not all be built at
once. The recommended sequence is:

1. **Phase 7 — Project Ops & GitHub Lifecycle**
2. **Phase 8 — Production Deployment & Real Backend Connectors**
3. **Phase 9 — Agent Teams & Parallel Execution**
4. **Phase 10 — Research / RAG / Skills System**
5. **Phase 11 — Long-horizon Recovery & Self-improvement**
6. **Phase 12 — Launch / Growth Studio**

This order matters.

Project Ops should come before Agent Teams because parallel subagents need
branches, patches, diffs, rollback, and merge review.

Project Ops should also come before deployment because production-facing changes
need traceable commits and release records.

Deployment should come before Launch Studio because launch assets are more
valuable when they describe a real deployed product, not only a local preview.

---

## 5. Phase summaries

## Phase 7 — Project Ops & GitHub Lifecycle

### Intent

Create the version-control and project-lifecycle foundation.

### Scope themes

- local Git state detection
- safe Git tool wrappers
- pre-run checkpoints
- post-run diff viewer
- commit message generation
- branch creation
- GitHub remote connector
- push with confirmation
- PR creation
- PR status tracking
- run-to-commit / run-to-PR linkage
- rollback from failed run or bad commit

### Out of scope initially

- fully automatic merge
- production deployment
- multi-agent parallel writes
- social publishing
- broad MCP marketplace

### Success signal

A verified run can become a reviewed commit and GitHub PR through an auditable,
user-approved flow.

---

## Phase 8 — Production Deployment & Real Backend Connectors

### Intent

Make Agent OS capable of shipping a minimal real full-stack product.

### Scope themes

- Vercel connector
- Supabase connector / CLI workflow
- Stripe test-mode connector
- local env / secret registry
- deployment contracts
- migration contracts
- webhook setup and local testing
- deployed smoke tests
- deployment records in memory
- rollback / redeploy basics

### Out of scope initially

- every cloud provider
- production payments without explicit hard gates
- automatic domain purchase
- multi-user cloud Agent OS hosting

### Success signal

Agent OS can build and deploy a minimal SaaS with database, auth, Stripe test
flow, preview URL, environment variables, and recorded operational state.

---

## Phase 9 — Agent Teams & Parallel Execution

### Intent

Turn the execution layer into a coordinated team runtime.

### Scope themes

- role registry
- skill registry v1
- task graph scheduler
- parallel read-only agents
- isolated patch workspaces
- integration / merge agent
- conflict handling
- global verification gate
- team trace UI

### Out of scope initially

- unbounded agent spawning
- agents writing concurrently to the same files
- autonomous external actions without contracts
- cloud queue infrastructure unless local threading becomes insufficient

### Success signal

A complex project task can be safely split, partially parallelized, merged,
verified, and recorded.

---

## Phase 10 — Research / RAG / Skills System

### Intent

Give Agent OS controlled external knowledge and reusable implementation memory.

### Scope themes

- local project index
- repo symbol / file summary index
- run history retrieval
- URL reader with user approval
- web search with allowlists
- GitHub reference reader
- research cache
- skill file format
- skill suggestion after successful runs
- user-reviewed skill activation

### Out of scope initially

- unrestricted browsing
- arbitrary MCP server trust
- automatic copying from open-source projects
- silent skill mutation

### Success signal

Agent OS can gather bounded references, cite them, store useful notes, and reuse
them in future runs without flooding the prompt or violating user control.

---

## Phase 11 — Long-horizon Recovery & Self-improvement

### Intent

Make repair loops typed, deeper, and evidence-driven.

### Scope themes

- Recovery Matrix
- visual repair loop
- runtime log repair
- deployment repair
- database repair
- failure-pattern memory
- regression check suggestions
- issue creation from repeated failures
- run health dashboard

### Out of scope initially

- unlimited self-repair
- silent production mutation
- self-modifying core instructions without review

### Success signal

Agent OS can identify the class of failure, choose the right recovery path,
execute a bounded repair, reverify, and record the lesson.

---

## Phase 12 — Launch / Growth Studio

### Intent

Help projects become understandable and shareable.

### Scope themes

- architecture diagrams
- product screenshots
- launch posters
- demo scripts
- demo video generation / editing integrations
- platform-specific social drafts
- release notes
- README / landing page polish
- launch checklist
- project narrative memory

### Out of scope initially

- automatic public posting
- influencer-style engagement automation
- scraping private communities
- paid ad management

### Success signal

Agent OS can generate a complete launch kit from project artifacts and memory.

---

## 6. Long-term architecture shape

```text
Agent OS
│
├─ Main Agent Layer
│  ├─ Intent Router
│  ├─ Planner / PM Agent
│  ├─ Memory Steward
│  ├─ Recovery Orchestrator
│  └─ Project Ops Coordinator
│
├─ Agent Teams Layer
│  ├─ Coding Agent
│  ├─ Backend Agent
│  ├─ Design Agent
│  ├─ Review Agent
│  ├─ Deploy Agent
│  ├─ Research Agent
│  └─ Growth Agent
│
├─ Execution Layer
│  ├─ Task Graph
│  ├─ Parallel Scheduler
│  ├─ Patch Workspaces
│  ├─ Integration / Merge
│  ├─ Verification
│  └─ Recovery Loops
│
├─ Project Ops Layer
│  ├─ Git / Branch / Commit
│  ├─ GitHub PR / Issue
│  ├─ Checkpoint / Rollback
│  └─ Release Records
│
├─ External Connector Layer
│  ├─ Vercel
│  ├─ Supabase
│  ├─ Stripe
│  ├─ GitHub
│  ├─ Canva / HyperFrames
│  └─ MCP Adapter
│
├─ Knowledge Layer
│  ├─ Markdown Memory
│  ├─ Project RAG
│  ├─ Research Cache
│  ├─ Skills Registry
│  └─ Lessons / Failure Patterns
│
└─ Launch Studio
   ├─ Diagrams
   ├─ Screenshots
   ├─ Demo Videos
   ├─ Social Posts
   └─ Release Kits
```

---

## 7. Practical guidance for future sessions

When starting a future Agent OS planning or coding session:

1. Read `CLAUDE.md`.
2. Read `ARCHITECTURE.md`.
3. Read `ROADMAP.md`.
4. Read this `BLUEPRINT.md`.
5. Identify which blueprint phase the requested work belongs to.
6. Keep the task bounded.
7. Preserve the existing invariants.
8. Add tests near the affected feature.
9. Update docs according to the existing documentation policy.

When uncertain, prefer the following strategic order:

1. safety and auditability
2. project lifecycle correctness
3. production realism
4. execution efficiency
5. research and skill reuse
6. launch assets and growth workflows

---

## 8. What not to do

Do not convert Agent OS into an uncontrolled general automation system.

Avoid these failure modes:

- agents silently pushing code
- agents silently deploying to production
- agents silently changing external platform settings
- agents silently publishing social posts
- arbitrary MCP tools with broad permissions
- parallel agents conflicting with each other
- huge unbounded RAG injection into the Main Agent context
- auto-generated skills that change behavior without review
- adding cloud infrastructure before the local-first model is exhausted
- optimizing for flashy plugins before Git / deploy / recovery foundations exist

---

## 9. One-line strategic summary

Phase 5 and Phase 6 made Agent OS capable of building, verifying, remembering,
and recovering. The next evolution should make it capable of managing real
project history, shipping production-like full-stack apps, coordinating agent
teams, learning through research and skills, repairing over longer horizons, and
generating the assets needed to launch projects publicly.
