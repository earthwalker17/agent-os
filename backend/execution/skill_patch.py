"""Suggested skill patch — the review-first self-improvement step (Phase 10.2).

After a GREEN (``completed``) run, a small bounded judge looks at the run's
outcome and decides whether it revealed a *reusable* method / checklist / repair
pattern / implementation lesson worth folding into one of the built-in skills.
If so it produces a :class:`~.models.SkillPatchProposal` — a PROPOSAL only:

  - It never writes a skill file. The proposal (target agent + skill, rationale,
    evidence, and a before/after content pair) is stored on ``run.json`` for the
    UI to render Apply / Reject / Edit.
  - Applying routes through ``skills_store.write_skill`` — the ONE skill write
    path — with the user's (possibly edited) content. Rejecting just marks it.
  - Patches are **append-style**: the judge proposes an ``addition`` block that
    is appended to the existing skill, so no existing content is ever lost.
  - Targets are restricted to skills already in ``agents_registry`` — there is
    NO autonomous creation of new skills and NO global promotion.

Best-effort and idempotent (one proposal per run); NEVER raises into finalize.
Mirrors ``recovery.assess_run``'s structure.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

import agents_registry
import skills_store
from llm import chat as llm_chat

from . import run_store
from .models import RunRecord, RunStatus, SkillPatchProposal

log = logging.getLogger(__name__)

_MAX_RESULT_MD_CHARS = 3000
_MAX_ADDITION_CHARS = 1200


def _catalog() -> str:
    """Compact list of patchable skills for the judge to choose from."""
    lines: list[str] = []
    for agent in agents_registry.AGENTS:
        if agent.status != agents_registry.STATUS_ACTIVE or not agent.skills:
            continue
        for ref in agent.skills:
            lines.append(f"- {agent.id}/{ref.id}: {ref.title} — {ref.description}")
    return "\n".join(lines)


_SYSTEM_PROMPT = """\
You are the skill-improvement subsystem of Agent OS. A Coding Agent just
finished a SUCCESSFUL run. Your job: decide whether the run revealed a
GENERALIZABLE, REUSABLE lesson — a method, checklist step, repair pattern, or
implementation gotcha — that should be folded into one of the existing built-in
skills so future runs benefit.

A skill is a reusable METHOD / CHECKLIST / RUBRIC / TEMPLATE — never an
executable tool, never project-specific trivia. Only propose a patch when the
lesson is durable and would help on OTHER projects/tasks. Most runs need NO
patch — prefer should_patch=false.

You may only patch a skill from this catalog (choose one target):
{catalog}

If you propose a patch, write an "addition": a short markdown block (a new
checklist item, a "### Gotcha" note, a rubric row — a few lines, under ~1000
chars) to APPEND to the chosen skill. Do not rewrite the skill; only add.

Return ONLY a single JSON object, no fences, no commentary:
{{
  "should_patch": true | false,
  "target_agent_id": "<agent id from the catalog>",
  "target_skill_id": "<skill id from the catalog>",
  "rationale": "one sentence: what reusable lesson and why it belongs there",
  "evidence": "one sentence grounding it in THIS run's outcome",
  "addition": "markdown block to append to the skill"
}}
When should_patch is false, the other fields may be empty strings.
"""


def _build_user_prompt(record: RunRecord, result_md: str) -> str:
    parts = [
        f"Run title: {record.task_title}",
        f"Status: {record.status.value if hasattr(record.status, 'value') else record.status}",
    ]
    if record.summary:
        parts.append(f"Summary: {record.summary}")
    if record.files_changed:
        parts.append("Files changed: " + ", ".join(record.files_changed[:15]))
    if record.commands_run:
        parts.append("Commands run: " + ", ".join(record.commands_run[:10]))
    if result_md:
        parts.append("--- result.md (truncated) ---\n" + result_md[:_MAX_RESULT_MD_CHARS])
    return "\n".join(parts)


def _strip_code_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _parse(out: str) -> Optional[dict]:
    t = _strip_code_fence(out)
    if not (t.startswith("{") and t.endswith("}")):
        return None
    try:
        parsed = json.loads(t)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def propose_skill_patch(
    project_id: str,
    run_id: str,
    *,
    summary_override: Optional[str] = None,
    llm_caller: Optional[Callable] = None,
) -> Optional[SkillPatchProposal]:
    """Propose (never apply) a skill patch from a green run. Never raises."""
    try:
        return _propose_inner(project_id, run_id, summary_override, llm_caller)
    except Exception as exc:  # noqa: BLE001
        log.exception("Skill-patch proposal crashed for run %s", run_id)
        try:
            _persist(project_id, run_id, SkillPatchProposal(
                proposed=False, error=f"{type(exc).__name__}: {exc}"
            ))
        except Exception:  # noqa: BLE001
            pass
        return None


def _propose_inner(
    project_id: str, run_id: str,
    summary_override: Optional[str], llm_caller: Optional[Callable],
) -> Optional[SkillPatchProposal]:
    raw = run_store.read_run_json(project_id, run_id)
    if raw is None:
        return None
    try:
        record = RunRecord(**raw)
    except Exception:  # noqa: BLE001
        return None

    # Idempotent: one proposal per run.
    if record.skill_patch is not None:
        return record.skill_patch
    # Only GREEN runs (a reusable lesson from a verified success).
    if record.status != RunStatus.COMPLETED:
        return None
    if summary_override and not record.summary:
        record.summary = summary_override
    # Cheap gate before spending an LLM call: a run that changed no files and
    # has no substantive summary is a read-only/inspection/trivial run and
    # almost never yields a reusable skill lesson.
    if not record.files_changed and len((record.summary or "").strip()) < 40:
        return None

    result_md = run_store.read_result_md(project_id, run_id) or ""
    caller = llm_caller or llm_chat
    system = _SYSTEM_PROMPT.format(catalog=_catalog())
    try:
        out = caller(
            system=system,
            messages=[{"role": "user", "content": _build_user_prompt(record, result_md)}],
            max_tokens=900,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Skill-patch judge LLM call failed: %s", exc)
        proposal = SkillPatchProposal(proposed=False, error=f"{type(exc).__name__}: {exc}")
        _persist(project_id, run_id, proposal)
        return proposal

    parsed = _parse(out)
    if not parsed or not parsed.get("should_patch"):
        # Nothing to propose — record a quiet "no proposal" so we don't re-judge.
        proposal = SkillPatchProposal(proposed=False)
        _persist(project_id, run_id, proposal)
        return proposal

    proposal = _build_proposal(parsed)
    _persist(project_id, run_id, proposal)
    return proposal


def _build_proposal(parsed: dict) -> SkillPatchProposal:
    agent_id = str(parsed.get("target_agent_id") or "").strip().lower()
    skill_id = str(parsed.get("target_skill_id") or "").strip().lower()
    # Judges sometimes return the skill id agent-qualified ("coder/skill-name"
    # or "skills/coder/skill-name") — normalize to the bare skill id before
    # the registry lookup instead of rejecting a legitimate proposal.
    if "/" in skill_id:
        parts = [p for p in skill_id.split("/") if p and p != "skills"]
        if len(parts) >= 2 and parts[-2] == agent_id:
            skill_id = parts[-1]
    ref = agents_registry.skill_ref(agent_id, skill_id)
    if ref is None:
        return SkillPatchProposal(
            proposed=False,
            error=f"judge targeted an unknown skill: {agent_id}/{skill_id}",
        )
    addition = str(parsed.get("addition") or "").strip()
    if not addition:
        return SkillPatchProposal(proposed=False, error="judge proposed an empty addition")
    if len(addition) > _MAX_ADDITION_CHARS:
        addition = addition[:_MAX_ADDITION_CHARS].rstrip() + "\n…"

    current = skills_store.read_skill(agent_id, ref.id)
    base = current.rstrip() if current.strip() else f"# {ref.title}\n> {ref.description}"
    proposed_content = base + "\n\n" + addition + "\n"
    return SkillPatchProposal(
        proposed=True,
        target_agent_id=agent_id,
        target_skill_id=ref.id,
        target_skill_title=ref.title,
        rationale=str(parsed.get("rationale") or "").strip()[:400],
        evidence=str(parsed.get("evidence") or "").strip()[:400],
        current_content=current,
        proposed_content=proposed_content,
        addition=addition,
        status="proposed",
    )


def _rebased_content(proposal: SkillPatchProposal) -> str:
    """Append the proposal's addition onto the LIVE skill content (so edits made
    between propose and apply are preserved, not clobbered by the stale
    snapshot). Falls back to the snapshot when the live content matches or the
    addition isn't recoverable."""
    live = skills_store.read_skill(proposal.target_agent_id, proposal.target_skill_id)
    addition = proposal.addition
    if not addition:
        # Legacy proposal without a stored addition — safest is the snapshot.
        return proposal.proposed_content
    if addition.strip() and addition.strip() in live:
        return live  # already applied; don't duplicate
    base = live.rstrip() if live.strip() else proposal.current_content.rstrip()
    return base + "\n\n" + addition + "\n"


def _persist(project_id: str, run_id: str, proposal: SkillPatchProposal) -> None:
    """Read-modify-write the proposal onto run.json (+ event)."""
    def _apply(rec: RunRecord) -> RunRecord:
        rec.skill_patch = proposal
        return rec

    try:
        run_store.mutate_run_json(project_id, run_id, _apply)
    except Exception:  # noqa: BLE001
        # Fall back to a plain write if the lock helper isn't usable here.
        raw = run_store.read_run_json(project_id, run_id)
        if raw is None:
            return
        try:
            record = RunRecord(**raw)
        except Exception:  # noqa: BLE001
            return
        record.skill_patch = proposal
        run_store.write_run_json(project_id, run_id, record)

    run_store.append_event(
        project_id,
        run_id,
        {
            "type": "skill_patch_proposed",
            "proposed": proposal.proposed,
            "target": f"{proposal.target_agent_id}/{proposal.target_skill_id}"
            if proposal.proposed else "",
            "error": (proposal.error or "")[:240],
        },
    )


# ---------- apply / reject (user-triggered, via the endpoints) ----------


def apply_skill_patch(
    project_id: str, run_id: str, *, content_override: Optional[str] = None
) -> SkillPatchProposal:
    """Apply the run's proposed skill patch through ``skills_store.write_skill``
    (the sole skill write path). ``content_override`` lets the user edit the
    proposed content before applying. Raises ValueError on any invalid state."""
    raw = run_store.read_run_json(project_id, run_id)
    if raw is None:
        raise ValueError("run not found")
    record = RunRecord(**raw)
    proposal = record.skill_patch
    if proposal is None or not proposal.proposed:
        raise ValueError("no skill patch proposed for this run")
    if proposal.status == "applied":
        raise ValueError("skill patch already applied")

    # An explicit user edit wins verbatim (they reviewed the diff and chose it).
    # An un-edited Apply RE-BASES the addition onto the live skill so any edit or
    # other patch applied since the proposal is preserved, not clobbered by the
    # propose-time snapshot.
    content = content_override if content_override is not None else _rebased_content(proposal)
    # The single skill write path (registry-validated pair, atomic, capped).
    skills_store.write_skill(proposal.target_agent_id, proposal.target_skill_id, content)

    updated = proposal.model_copy(update={"status": "applied", "proposed_content": content})

    def _apply(rec: RunRecord) -> RunRecord:
        rec.skill_patch = updated
        return rec

    run_store.mutate_run_json(project_id, run_id, _apply)
    run_store.append_event(project_id, run_id, {
        "type": "skill_patch_applied",
        "target": f"{updated.target_agent_id}/{updated.target_skill_id}",
    })
    return updated


def reject_skill_patch(project_id: str, run_id: str) -> SkillPatchProposal:
    """Mark the run's proposed skill patch as rejected (writes nothing)."""
    raw = run_store.read_run_json(project_id, run_id)
    if raw is None:
        raise ValueError("run not found")
    record = RunRecord(**raw)
    proposal = record.skill_patch
    if proposal is None or not proposal.proposed:
        raise ValueError("no skill patch proposed for this run")
    updated = proposal.model_copy(update={"status": "rejected"})

    def _apply(rec: RunRecord) -> RunRecord:
        rec.skill_patch = updated
        return rec

    run_store.mutate_run_json(project_id, run_id, _apply)
    run_store.append_event(project_id, run_id, {
        "type": "skill_patch_rejected",
        "target": f"{updated.target_agent_id}/{updated.target_skill_id}",
    })
    return updated
