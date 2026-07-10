# Security Policy

Agent OS takes security seriously — credential hygiene, a single sandbox
chokepoint, and human-in-the-loop delivery are constitutional parts of the
design. If you find a way to break any of them, we want to hear about it
privately.

## Supported versions

Agent OS is developed on a rolling basis; fixes land on `main` and ship in the
next tagged release. Only the latest release and `main` are supported.

| Version | Supported |
|---------|-----------|
| `main` / latest `1.x` release | ✅ |
| Older tags | ❌ |

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub Issues,
Discussions, or pull requests.**

Report privately through **GitHub Private Vulnerability Reporting**:

1. Go to the repository's **[Security tab](https://github.com/earthwalker17/agent-os/security)**.
2. Click **"Report a vulnerability"** to open a private advisory
   ([direct link](https://github.com/earthwalker17/agent-os/security/advisories/new)).
3. Describe the issue with enough detail for us to reproduce it.

This keeps the report confidential between you and the maintainers until a fix
is available.

### What to include

- A description of the vulnerability and its impact.
- Step-by-step instructions to reproduce it.
- The affected component (e.g. the sandbox, a connector, the research channel,
  credential handling) and any relevant configuration.
- Your environment: OS, Python version, Node version, and which model providers
  or connectors were configured.

**Redact all secrets** — API keys, tokens, `.env` contents, credential-store
files — from anything you attach. If a secret was exposed as part of the
vulnerability, say so without pasting the value.

### What to expect

- We aim to acknowledge a report within **5 business days**.
- We'll confirm the issue, work on a fix, and keep you updated on progress.
- With your permission, we'll credit you when the fix is released.

Because Agent OS is a **single-user, local-first** tool with no shared
deployment or authentication layer, please keep that threat model in mind:
the highest-value reports concern the boundaries that are meant to hold even on
a trusted machine — sandbox escapes (raw filesystem/shell access outside
`ProjectSandbox`), secret leakage into prompts / logs / artifacts / Git / the
UI, bypasses of the explicit approval gates for delivery/deploys/payments, or
SSRF / egress bypasses in the `@search` research channel.

## Scope

In scope: the code in this repository (`backend/`, `frontend/`, installer
scripts). Out of scope: vulnerabilities in third-party model providers or
connector services (Vercel, Supabase, Stripe, GitHub) themselves — report those
to the respective vendor — and issues that require an already-compromised host.
