"""Phase 11 — Recovery Matrix: typed, deterministic failure classification.

Large projects fail in layers — a TypeScript build error, a dev server that
never comes up, a blank page, an integration conflict, a failed deploy — and
each layer needs different evidence and different repair behavior. This module
is the **foundation** for that: a small, frozen registry of recovery
*contracts* (one per failure type) plus a deterministic, rule-based classifier
and a bounded evidence builder.

Deliberately a **leaf module**: no LLM calls, no disk writes, no network.
It only reads the ``RunRecord`` the caller passes in. The LLM-facing side
lives in ``recovery.assess_run`` (which uses the classification as a strong
prior) and the dispatch side in ``background._maybe_auto_recover`` /
``main.api_confirm_pending_execution`` (which enforce the contract's
auto-eligibility and budget caps).

Invariants encoded here:

  - **Typed, not free-form.** Every classification lands in ``RECOVERY_TYPES``;
    unknown signals fall back to ``product`` (never auto-repaired).
  - **Environment problems are never auto-repaired.** A missing Playwright /
    Chromium install, an occupied port, a failed subprocess spawn — a Coding
    Agent cannot fix operator tooling, so those classify as ``runtime`` with
    ``auto_ok=False``.
  - **Evidence is bounded + redacted.** ``build_recovery_evidence`` output is
    capped (``EVIDENCE_MAX_CHARS``) and every line passes
    ``credentials.redact`` before it can reach a task card / prompt / UI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple, Optional

import credentials

from .models import RunRecord, RunStatus


# ---------- types + contracts ----------


RECOVERY_TYPES = (
    "build",
    "runtime",
    "visual",
    "integration",
    "deployment",
    "database",
    "product",
    "docs_memory",
)


@dataclass(frozen=True)
class RecoveryContract:
    """The per-type repair contract: what evidence a repair of this type
    accepts, how it is verified, and how far automation may go.

    ``auto_eligible`` gates the Phase 6.1 budgeted auto-recovery path: only
    types where a bounded Coding Agent run can plausibly fix the failure may
    auto-dispatch (and only under a user-approved budget — the contract can
    tighten the boundary, never loosen it). ``child_budget_cap`` clamps the
    recovery child's own remaining budget so e.g. a visual repair is a single
    bounded pass (repair → natural re-verify in the child's tail) rather than
    a chain. ``confirmation`` documents the user-approval boundary;
    ``always_confirm`` types only ever reach a run through an explicit
    "OK, run this" click.
    """

    recovery_type: str
    description: str
    accepted_evidence: tuple[str, ...]
    verification: str
    max_attempts: int
    auto_eligible: bool
    child_budget_cap: int
    confirmation: str  # "auto_under_budget" | "always_confirm"
    audit_note: str


RECOVERY_CONTRACTS: dict[str, RecoveryContract] = {
    "build": RecoveryContract(
        recovery_type="build",
        description="Compile / type / lint / test / install failures caught by command verification.",
        accepted_evidence=(
            "failing verification command + kind",
            "command output tail (stdout/stderr preview)",
            "files changed by the run",
        ),
        verification="command verification re-runs automatically in the recovery run's tail",
        max_attempts=2,
        auto_eligible=True,
        child_budget_cap=1,
        confirmation="auto_under_budget",
        audit_note="recovery_assessment + lineage on run.json; verification result on the child run",
    ),
    "runtime": RecoveryContract(
        recovery_type="runtime",
        description="Dev server / startup / port / dependency failures and console-error storms.",
        accepted_evidence=(
            "dev-server output tail + readiness probe outcome",
            "install status/output",
            "console errors + network failures",
        ),
        verification="browser verification re-runs automatically in the recovery run's tail",
        max_attempts=1,
        auto_eligible=True,
        child_budget_cap=0,
        confirmation="auto_under_budget",
        audit_note="browser_verification (console/network evidence) + lineage on run.json",
    ),
    "visual": RecoveryContract(
        recovery_type="visual",
        description="Blank screens, broken layout, stuck loading, missing views, failed declared flows.",
        accepted_evidence=(
            "screenshots (artifact names)",
            "AI visual verdict + reasoning + evidence",
            "failed interaction flow steps",
            "console errors + network failures",
        ),
        verification="browser verification + AI visual review re-run in the recovery run's tail",
        max_attempts=1,
        auto_eligible=True,
        child_budget_cap=0,
        confirmation="auto_under_budget",
        audit_note="before/after visual verdicts on parent vs child run, linked by recovery lineage",
    ),
    "integration": RecoveryContract(
        recovery_type="integration",
        description="Team-run integration conflicts or overlay apply errors.",
        accepted_evidence=(
            "integration conflicts list (path + winning/rejected task)",
            "patch-workspace manifests",
        ),
        verification="command verification over the re-integrated tree",
        max_attempts=1,
        auto_eligible=False,
        child_budget_cap=0,
        confirmation="always_confirm",
        audit_note="integration.json + conflicts on run.json",
    ),
    "deployment": RecoveryContract(
        recovery_type="deployment",
        description="Vercel build / env / routing failures on a deployed target.",
        accepted_evidence=(
            "deployment id/url/state",
            "deploy log artifact reference",
        ),
        verification="explicit re-deploy contract (preview -> confirm)",
        max_attempts=1,
        auto_eligible=False,
        child_budget_cap=0,
        confirmation="always_confirm",
        audit_note="deployment.json / deploy.log + OPS.md ledger",
    ),
    "database": RecoveryContract(
        recovery_type="database",
        description="Supabase migration / seed / schema failures.",
        accepted_evidence=(
            "sandboxed supabase CLI output (redacted)",
            "migration file names",
        ),
        verification="explicit migration contract re-run (preview -> confirm)",
        max_attempts=1,
        auto_eligible=False,
        child_budget_cap=0,
        confirmation="always_confirm",
        audit_note="migration contract records + OPS.md ledger",
    ),
    "product": RecoveryContract(
        recovery_type="product",
        description=(
            "The work misses the user's intent, or a non-green run has no "
            "specific technical failure signature (incomplete/blocked work)."
        ),
        accepted_evidence=(
            "task card vs run summary mismatch",
            "blockers without a technical signature",
            "user feedback",
        ),
        verification="the recovery run's own verification tail + user review",
        # This is the generic fallback type, and a NON-GREEN run classified
        # here is the canonical Phase 6.1 auto-recovery case (incomplete work,
        # a repair/split card, an explicit user budget) — auto-eligibility here
        # preserves that constitutional behavior byte-for-byte (the old child
        # budget was `budget - 1` with budget clamped to <= 2, i.e. <= 1 —
        # identical to this cap). ``classify_failure`` still returns
        # ``auto_ok=False`` for green-status product mismatches, so those stay
        # confirm-only.
        max_attempts=1,
        auto_eligible=True,
        child_budget_cap=1,
        confirmation="auto_under_budget",
        audit_note="recovery_assessment diagnosis on run.json",
    ),
    "docs_memory": RecoveryContract(
        recovery_type="docs_memory",
        description="Stale docs / failed memory reconciliation / unrecorded decisions.",
        accepted_evidence=(
            "memory_reconciliation error fields",
        ),
        verification="memory reconciliation re-run",
        max_attempts=1,
        auto_eligible=False,
        child_budget_cap=0,
        confirmation="always_confirm",
        audit_note="memory_reconciliation fields on run.json",
    ),
}


def contract_for(recovery_type: str) -> RecoveryContract:
    """Resolve a contract, falling back to the conservative ``product`` one."""
    return RECOVERY_CONTRACTS.get(recovery_type, RECOVERY_CONTRACTS["product"])


# ---------- deterministic classification ----------


class RecoveryClassification(NamedTuple):
    """Outcome of ``classify_failure``: the matched type, the human-readable
    rule that matched (for audit), and whether a *budgeted auto-repair* is
    sensible at all (``auto_ok=False`` marks environment/operator problems a
    Coding Agent cannot fix — independent of the contract's own gate)."""

    recovery_type: str
    reason: str
    auto_ok: bool


# Environment signatures inside a failed browser verification's output that a
# Coding Agent cannot repair by editing repo/ files.
_ENV_FAILURE_RE = re.compile(
    r"playwright (?:is )?not installed"
    r"|playwright_not_installed"
    r"|chromium (?:is )?not installed"
    r"|chromium_not_installed"
    r"|python -m playwright install"
    r"|already in use"
    r"|could not spawn"
    r"|npm (?:was )?not found",
    re.IGNORECASE,
)

# Server-level runtime signatures: the app's process itself never served.
_SERVER_FAILURE_RE = re.compile(
    r"exited (?:early|with code)"
    r"|did not become reachable"
    r"|dev server (?:failed|exited)"
    r"|install failed",
    re.IGNORECASE,
)

_DEPLOY_SIGNAL_RE = re.compile(r"\bdeploy(?:ment)?\b|\bvercel\b", re.IGNORECASE)
# Deliberately narrow: only the Supabase executor's own signatures. A bare
# "migration" would match an ordinary app-code run whose blocker merely
# mentions writing a migration file — misclassifying it `database`
# (confirm-only) and silently vetoing the user's approved recovery budget.
_DATABASE_SIGNAL_RE = re.compile(r"\bsupabase\b|\bdb push\b", re.IGNORECASE)

_NON_GREEN = {RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.BLOCKED}


def classify_failure(record: RunRecord) -> RecoveryClassification:
    """Deterministically classify a non-green run into a recovery type.

    First-match ladder, most specific first. Pure function of the record —
    no LLM, no I/O — so the same record always classifies the same way.
    """
    bv = record.browser_verification
    browser_failed = bool(bv and bv.enabled and bv.status == "failed")
    blockers_text = " ".join(record.blockers or [])

    # 1. Team integration conflicts / apply errors.
    integ = record.integration
    if integ is not None and integ.enabled and integ.conflicts:
        return RecoveryClassification(
            "integration",
            f"integration produced {len(integ.conflicts)} conflict(s)",
            False,
        )

    # 2. Deployment failures (run-scoped deploy that went bad).
    if record.deployment_id and _DEPLOY_SIGNAL_RE.search(blockers_text):
        return RecoveryClassification(
            "deployment", "deployment id present with a deploy-signal blocker", False
        )

    # 3. Database / migration failures.
    if _DATABASE_SIGNAL_RE.search(blockers_text):
        return RecoveryClassification(
            "database", "blocker carries a supabase/migration signature", False
        )

    # 4-6. Browser verification failures, split by evidence.
    if browser_failed:
        preview = (bv.output_preview or "") + " " + (bv.install_output_preview or "")
        if _ENV_FAILURE_RE.search(preview):
            return RecoveryClassification(
                "runtime",
                "browser verification failed on an environment problem "
                "(missing Playwright/Chromium, occupied port, spawn failure)",
                False,  # operator tooling — never auto-repair
            )
        if bv.install_status == "failed" or _SERVER_FAILURE_RE.search(preview):
            return RecoveryClassification(
                "runtime", "dev server / install never served the app", True
            )
        if any(f.status == "failed" for f in (bv.flows or [])):
            return RecoveryClassification(
                "visual", "a declared interaction flow failed", True
            )
        return RecoveryClassification(
            "visual", "browser verification failed after the server was up", True
        )

    # 7. Failed AI visual verdict (diagnostic-only signal on a green status).
    vr = record.visual_review
    if vr is not None and vr.enabled and vr.status == "failed":
        return RecoveryClassification(
            "visual", "AI visual review returned a failed verdict", True
        )

    # 8. Command verification failures -> build repair.
    v = record.verification
    if v is not None and v.enabled and v.status == "failed":
        kind = "build"
        for cmd in v.commands or []:
            if cmd.status == "failed":
                kind = cmd.kind or "build"
                break
        return RecoveryClassification(
            "build", f"command verification failed (kind: {kind})", True
        )

    # 9. Runtime-log bridge: a page that never confirmably rendered (blank /
    # stuck loading) with console errors on a non-green run. Deliberately
    # gated on unconfirmed readiness — incidental console noise on an
    # otherwise-rendered app must NOT reroute the generic fallback below
    # (rule 10 carries the Phase 6.1 budget contract).
    if (
        record.status in _NON_GREEN
        and bv is not None
        and bv.enabled
        and (bv.console_errors or [])
        and bv.readiness == "unconfirmed"
    ):
        return RecoveryClassification(
            "runtime",
            "page never confirmably rendered and console errors were captured",
            True,
        )

    # 10. Fallback. A NON-GREEN run without a technical signature (incomplete
    # or blocked work) is the canonical Phase 6.1 budgeted auto-recovery case
    # — auto_ok stays True so an explicit user budget keeps working. A green
    # run that lands here (rare: some non-failed advisory signal) has nothing
    # a repair run could provably fix — confirm-only.
    if record.status in _NON_GREEN:
        return RecoveryClassification(
            "product",
            "non-green run without a specific technical failure signature",
            True,
        )
    return RecoveryClassification(
        "product", "no technical failure signature matched", False
    )


# ---------- bounded evidence builder ----------


EVIDENCE_MAX_CHARS = 1800

_MAX_EVIDENCE_SCREENSHOTS = 6
_MAX_EVIDENCE_CONSOLE = 5
_MAX_EVIDENCE_NETWORK = 5
_MAX_EVIDENCE_STEPS = 6
_CONSOLE_LINE_CHARS = 200
_VERIFY_TAIL_CHARS = 600

_GUIDANCE = (
    "Fix the code under repo/. Do not remove or edit the `## Verification` or "
    "`## Browser Verification` blocks in TASK.md — the post-run tail re-runs "
    "them automatically to prove the fix."
)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def build_recovery_evidence(
    record: RunRecord,
    *,
    project_id: Optional[str] = None,
    classification: Optional[RecoveryClassification] = None,
) -> str:
    """Render a bounded, redacted markdown evidence block for a recovery run.

    Appended to the assessment's ``follow_up_task_card`` so the recovery child
    (which never sees chat history or the parent's artifacts) receives the
    concrete failure evidence: what failed, where, and which artifacts prove
    it. Total output is capped at ``EVIDENCE_MAX_CHARS`` and every line passes
    ``credentials.redact`` before leaving this function.

    Free-text fields are redacted BEFORE their per-line truncation (and the
    whole block is redacted again at the end): truncating first could slice a
    stored secret mid-value and defeat exact-match redaction.
    """
    cls = classification or classify_failure(record)

    def _safe(text: str, limit: int) -> str:
        return _truncate(credentials.redact(text or "", project_id), limit)
    lines: list[str] = [
        f"- Failed run: `{record.run_id}` (status: {record.status.value})",
        f"- Recovery type: **{cls.recovery_type}** — {cls.reason}",
    ]

    bv = record.browser_verification
    if bv is not None and bv.enabled:
        if bv.url:
            lines.append(f"- App URL under verification: {bv.url}")
        if bv.status != "passed" and bv.output_preview:
            lines.append(
                f"- Browser verification output: {_safe(bv.output_preview, 300)}"
            )
        shots = [p.path for p in (bv.pages or []) if p.path][:_MAX_EVIDENCE_SCREENSHOTS]
        if shots:
            lines.append(
                "- Screenshots on the failed run (artifacts): " + ", ".join(shots)
            )
        for flow in bv.flows or []:
            if flow.status not in ("failed", "refused"):
                continue
            failed_steps = [s for s in flow.steps if s.status == "failed"]
            for step in failed_steps[:_MAX_EVIDENCE_STEPS]:
                lines.append(
                    f"- Flow `{flow.name}` failed at `{step.action} {step.target}`: "
                    f"{_safe(step.error, 160) or '(no error text)'}"
                )
            if flow.status == "refused":
                lines.append(
                    f"- Flow `{flow.name}` was refused by policy (credential-shaped "
                    "input) — do NOT attempt to work around this."
                )
        for entry in (bv.console_errors or [])[:_MAX_EVIDENCE_CONSOLE]:
            lines.append(f"- Console error: {_safe(entry, _CONSOLE_LINE_CHARS)}")
        for entry in (bv.network_failures or [])[:_MAX_EVIDENCE_NETWORK]:
            lines.append(f"- Network failure: {_safe(entry, _CONSOLE_LINE_CHARS)}")

    vr = record.visual_review
    if vr is not None and vr.enabled and vr.status in ("failed", "warning"):
        lines.append(
            f"- Visual verdict (before repair): **{vr.status}** — "
            f"{_safe(vr.headline or vr.reasoning, 240)}"
        )
        for item in (vr.evidence or [])[:3]:
            lines.append(f"- Visual evidence: {_safe(str(item), 200)}")

    v = record.verification
    if v is not None and v.enabled and v.status == "failed":
        lines.append(
            f"- Failing verification command: `{v.command or '(unknown)'}`"
        )
        if v.output_preview:
            lines.append(
                f"- Verification output tail: {_safe(v.output_preview, _VERIFY_TAIL_CHARS)}"
            )

    lines.append(f"- Guidance: {_GUIDANCE}")

    text = "\n".join(lines)
    text = credentials.redact(text, project_id)
    if len(text) > EVIDENCE_MAX_CHARS:
        text = text[: EVIDENCE_MAX_CHARS - 3].rstrip() + "..."
    return text
