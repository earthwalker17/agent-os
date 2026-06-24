"""Phase 6 — Main-Agent confirmable recovery assessment.

When a Coding Agent run reaches a NON-green terminal state, this module asks a
small model-judged call to interpret the outcome and recommend a single bounded
next step. It is the Main Agent's "run supervisor / failure-recovery" role made
concrete, while preserving every invariant:

  - **Diagnostic + confirmable, never auto-dispatch.** ``assess_run`` only
    *describes* a recommended next step (and, when a follow-up run is the
    recommendation, drafts a self-contained task card). It NEVER starts a run.
    A run only ever starts from an explicit `@code` or the user clicking
    "OK, run this" on a proposed plan. The UI turns a ``needs_recovery``
    assessment into a confirmable pending card through the existing flow.

  - **Best-effort.** Mirrors ``memory_reconciliation``: a top-level guard so an
    assessment failure NEVER fails the run; the outcome (including errors) is
    captured onto ``RunRecord.recovery_assessment`` and persisted to run.json.

  - **Compact inputs only.** The judge sees the run's status, summary,
    files/commands/blockers, the rendered result.md (truncated), and the
    verification / browser / visual signals already on the record. No raw event
    logs, no diffs, no repo contents.

  - **Quiet on green.** Genuinely-successful runs (completed, verification not
    failed, visual review not failed) are skipped outright — no LLM call, no
    persisted assessment.

  - **Idempotent.** A record that already carries a ``recovery_assessment`` is
    returned unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from llm import chat as llm_chat

from . import run_store
from .models import RunRecord, RunStatus, RecoveryAssessment


log = logging.getLogger(__name__)


# Non-green terminal statuses always worth a look. CANCELLED is excluded (no
# settled outcome) — and the runner only calls this from ``_finalize``, never
# from the cancelled-finalize path, so cancelled runs never reach here anyway.
_NON_GREEN_STATUSES = {RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.BLOCKED}

_VALID_VERDICTS = {"ok", "needs_recovery", "exhausted"}
_VALID_ACTIONS = {"inspect", "repair", "split", "reverify", "report"}
# Actions that imply a follow-up Coding Agent run (and thus a task card).
_RUN_ACTIONS = {"repair", "split", "reverify"}

_MAX_RESULT_MD_CHARS = 4000
_MAX_SUMMARY_CHARS = 1600
_MAX_LIST_ITEM_CHARS = 240
_MAX_LIST_ENTRIES = 20
_MAX_OUTPUT_PREVIEW_CHARS = 1200


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _trim_list(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in (items or [])[:_MAX_LIST_ENTRIES]:
        out.append(_truncate(str(item).strip(), _MAX_LIST_ITEM_CHARS))
    return out


def _needs_assessment(record: RunRecord) -> bool:
    """True for a non-green outcome worth assessing.

    Covers the three non-green statuses plus the two signals a ``completed``
    status can hide: a failed command/browser verification and a failed AI
    visual review (which is diagnostic-only and never downgrades status).
    """
    if record.status in _NON_GREEN_STATUSES:
        return True
    if record.verification and record.verification.status == "failed":
        return True
    if record.browser_verification and record.browser_verification.status == "failed":
        return True
    if record.visual_review and record.visual_review.status == "failed":
        return True
    return False


# ---------- prompt ----------

_SYSTEM_PROMPT = """\
You are the run-recovery subsystem of Agent OS, acting for the Main Agent.

A Coding Agent run inside a project's sandboxed repo/ workspace did NOT come back
fully green. Your job: read a compact summary of the run and recommend ONE bounded
next step for the user to confirm. You do NOT start runs — you only advise.

## Recommended actions (choose exactly one)
- "inspect": the cause is unclear; the Main Agent should read a specific file or
  two (read-only) before deciding.
- "repair": there is a concrete fix; a small follow-up coding run should make it.
- "split": the task was too big; break it into a smaller, well-scoped follow-up.
- "reverify": the work may actually be fine; re-run verification / re-check.
- "report": automatic progress looks exhausted; just report honestly to the user.

## Verdict
- "needs_recovery": a concrete next step (inspect / repair / split / reverify) is
  worth doing.
- "exhausted": repeated attempts or hard blockers mean a human should step in;
  pair with action "report".
- "ok": the run is actually fine despite the signal (rare).

## Rules
1. Be concrete and honest. Diagnose what actually went wrong from the evidence.
2. When the action is repair / split / reverify, write ``follow_up_task_card`` as
   a SELF-CONTAINED imperative task for the Coding Agent (it won't see chat
   history): what to do and how to know it's fixed. Keep it tight (1-6 sentences
   or a short bulleted list). For inspect / report leave it an empty string.
3. Do not claim anything has been fixed. You are proposing, not doing.

## Response format
Return ONLY a single JSON object. No markdown fences, no commentary.
{
  "verdict": "needs_recovery" | "exhausted" | "ok",
  "diagnosis": "one or two sentences on what went wrong",
  "recommended_action": "inspect" | "repair" | "split" | "reverify" | "report",
  "follow_up_task_card": "imperative task card, or empty string",
  "rationale": "one sentence on why this is the right next step"
}
"""


def _build_user_prompt(record: RunRecord, result_md_text: str) -> str:
    files = _trim_list(record.files_changed)
    commands = _trim_list(record.commands_run)
    blockers = _trim_list(record.blockers)

    def _render(label: str, items: list[str]) -> str:
        if not items:
            return f"### {label}\n_(none)_"
        return f"### {label}\n" + "\n".join(f"- {x}" for x in items)

    signals: list[str] = []
    if record.verification:
        v = record.verification
        signals.append(
            f"- command verification: {v.status}"
            + (f" (command `{v.command}`)" if v.command else "")
            + (f" — {_truncate(v.output_preview, _MAX_OUTPUT_PREVIEW_CHARS)}" if v.output_preview else "")
        )
    if record.browser_verification and record.browser_verification.enabled:
        b = record.browser_verification
        signals.append(
            f"- browser verification: {b.status}"
            + (f" — {_truncate(b.output_preview, _MAX_OUTPUT_PREVIEW_CHARS)}" if b.output_preview else "")
        )
    if record.visual_review and record.visual_review.enabled:
        vr = record.visual_review
        signals.append(
            f"- visual review: {vr.status}"
            + (f" — {vr.headline}" if vr.headline else "")
            + (f" {_truncate(vr.reasoning, _MAX_OUTPUT_PREVIEW_CHARS)}" if vr.reasoning else "")
        )
    signals_block = "\n".join(signals) if signals else "_(no extra verification signals)_"

    return (
        f"## Run\n"
        f"- run_id: `{record.run_id}`\n"
        f"- status: `{record.status.value}`\n"
        f"- task_title: {record.task_title or '(untitled)'}\n\n"
        f"## Compact Outcome\n"
        f"{_render('Files Changed', files)}\n\n"
        f"{_render('Commands Run', commands)}\n\n"
        f"{_render('Blockers / Errors', blockers)}\n\n"
        f"### Verification Signals\n{signals_block}\n\n"
        f"### Summary\n{_truncate(record.summary.strip(), _MAX_SUMMARY_CHARS) or '_(no summary)_'}\n\n"
        f"## Rendered result.md\n{_truncate(result_md_text, _MAX_RESULT_MD_CHARS) or '_(no result.md)_'}\n\n"
        f"---\n\nAssess this run and recommend ONE bounded next step. Return ONLY the JSON object."
    )


def _strip_code_fence(text: str) -> str:
    t = (text or "").strip()
    if "```" not in t:
        return t
    first = t.find("```")
    last = t.rfind("```")
    if first == last:
        return t
    inner = t[first:last]
    nl = inner.find("\n")
    return inner[nl + 1:].strip() if nl != -1 else ""


def _parse_assessment(raw: str) -> Optional[RecoveryAssessment]:
    text = _strip_code_fence(raw)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Recovery judge returned invalid JSON: %s", text[:200])
        return None
    if not isinstance(parsed, dict):
        return None

    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict not in _VALID_VERDICTS:
        verdict = "needs_recovery"
    action = str(parsed.get("recommended_action", "")).strip().lower()
    if action not in _VALID_ACTIONS:
        action = "report"
    diagnosis = str(parsed.get("diagnosis", "")).strip()
    rationale = str(parsed.get("rationale", "")).strip()
    task_card = str(parsed.get("follow_up_task_card", "")).strip()
    # A task card only makes sense for run-type actions.
    if action not in _RUN_ACTIONS:
        task_card = ""
    # If the judge recommends a run action but gave no card, it can't be a
    # confirmable run — downgrade to a report.
    if action in _RUN_ACTIONS and not task_card:
        action = "report"
        if verdict == "needs_recovery":
            verdict = "exhausted"

    return RecoveryAssessment(
        assessed=True,
        verdict=verdict,
        diagnosis=diagnosis,
        recommended_action=action,
        follow_up_task_card=task_card,
        rationale=rationale,
    )


def assess_run(
    project_id: str,
    run_id: str,
    *,
    llm_caller: Optional[Callable] = None,
) -> Optional[RecoveryAssessment]:
    """Assess a terminal run and persist a ``RecoveryAssessment`` if non-green.

    Returns the assessment (or ``None`` when skipped). NEVER raises — all errors
    are captured onto the record's ``recovery_assessment.error`` and swallowed so
    the run stays in its terminal state.
    """
    try:
        return _assess_inner(project_id, run_id, llm_caller)
    except Exception as exc:  # noqa: BLE001
        log.exception("Recovery assessment crashed for run %s", run_id)
        try:
            _persist(project_id, run_id, RecoveryAssessment(
                assessed=False, verdict="ok",
                error=f"{type(exc).__name__}: {exc}",
            ))
        except Exception:  # noqa: BLE001
            pass
        return None


def _assess_inner(
    project_id: str, run_id: str, llm_caller: Optional[Callable]
) -> Optional[RecoveryAssessment]:
    raw = run_store.read_run_json(project_id, run_id)
    if raw is None:
        return None
    try:
        record = RunRecord(**raw)
    except Exception:  # noqa: BLE001
        return None

    # Idempotent: already assessed.
    if record.recovery_assessment is not None:
        return record.recovery_assessment

    # Only non-running, non-cancelled runs; quiet on green.
    if record.status in (RunStatus.RUNNING, RunStatus.CANCELLED):
        return None
    if not _needs_assessment(record):
        return None

    result_md = run_store.read_result_md(project_id, run_id) or ""
    caller = llm_caller or llm_chat
    try:
        out = caller(
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_prompt(record, result_md)}],
            max_tokens=900,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Recovery judge LLM call failed: %s", exc)
        assessment = RecoveryAssessment(
            assessed=False, verdict="ok", error=f"{type(exc).__name__}: {exc}"
        )
        _persist(project_id, run_id, assessment)
        return assessment

    assessment = _parse_assessment(out)
    if assessment is None:
        assessment = RecoveryAssessment(
            assessed=False, verdict="ok", error="judge returned malformed output"
        )
    _persist(project_id, run_id, assessment)
    return assessment


def _persist(project_id: str, run_id: str, assessment: RecoveryAssessment) -> None:
    """Read-modify-write the assessment onto run.json + emit an event."""
    raw = run_store.read_run_json(project_id, run_id)
    if raw is None:
        return
    try:
        record = RunRecord(**raw)
    except Exception:  # noqa: BLE001
        return
    record.recovery_assessment = assessment
    run_store.write_run_json(project_id, run_id, record)
    run_store.append_event(
        project_id,
        run_id,
        {
            "type": "recovery_assessment",
            "assessed": assessment.assessed,
            "verdict": assessment.verdict,
            "recommended_action": assessment.recommended_action,
            "has_task_card": bool(assessment.follow_up_task_card),
            "error": _truncate(assessment.error or "", 240),
        },
    )
