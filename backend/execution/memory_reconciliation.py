"""Task 06.0 — post-run memory reconciliation.

When a Coding Agent run reaches a terminal state, this module decides whether
project memory should be updated to reflect the outcome of the run.

Design constraints:

  - **Compact inputs only.** The reconciliation judge sees the run's
    ``ResultSummary`` (status, summary, files_changed, commands_run, blockers),
    the rendered ``result.md`` (truncated), and a compact snapshot of the
    current project memory. It does NOT see ``events.jsonl``, raw diffs, or
    full repo contents.

  - **Bounded write surface.** The reconciler may only propose updates to
    ``STATUS.md`` / ``TASK_QUEUE.md`` / ``DECISIONS.md`` / ``RESEARCH.md``.
    ``PROJECT.md``, all global memory, ``SOUL.md``, and repo source files are
    out of scope. Writes route through the existing policy-filtered
    ``apply_memory_update`` so the writeback layer also rejects anything else.

  - **Best-effort.** Reconciliation failure (LLM error, JSON parse error,
    disk error) must NEVER fail the run itself. Errors are recorded in the
    ``RunRecord``'s reconciliation fields and the run still finalizes
    normally.

  - **Quiet on noisy runs.** Read-only inspection runs and failed runs with
    no useful output skip reconciliation outright — no LLM call.

  - **Idempotent per run.** A record that already has reconciliation
    metadata is skipped, so duplicate appends cannot occur from the same
    run reaching this code twice.

This module deliberately does not import from ``orchestrator.py`` to avoid a
circular-import: ``orchestrator`` imports parts of ``execution`` indirectly.
It re-implements the small subset it needs (writable-file set, snapshot
loader, ``apply_memory_update``-equivalent) on top of the same on-disk
markdown layout.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from llm import chat as llm_chat

from . import run_store
from .models import RunRecord, ResultSummary, RunStatus


log = logging.getLogger(__name__)


_PROJECTS_DIR = Path(__file__).resolve().parent.parent.parent / "projects"


# Reconciliation may only write to these four files. Notice ``PROJECT.md`` is
# intentionally excluded — project definition shouldn't be rewritten by a
# Coding Agent run summary.
RECONCILIATION_WRITABLE_FILES: set[str] = {
    "STATUS.md",
    "TASK_QUEUE.md",
    "DECISIONS.md",
    "RESEARCH.md",
}

TERMINAL_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.PARTIAL,
    RunStatus.BLOCKED,
    RunStatus.FAILED,
}

# Tags used in ``RunRecord.memory_reconciliation``. Kept short and stable so
# the frontend / future analytics can branch on them.
TAG_APPLIED = "applied"
TAG_SKIPPED_NON_TERMINAL = "skipped_non_terminal"
TAG_SKIPPED_ALREADY_RECONCILED = "skipped_already_reconciled"
TAG_SKIPPED_READ_ONLY = "skipped_read_only"
TAG_SKIPPED_FAILED_NOISY = "skipped_failed_noisy"
TAG_SKIPPED_JUDGE_NO_UPDATE = "skipped_judge_no_update"
TAG_SKIPPED_NO_VALID_UPDATES = "skipped_no_valid_updates"
TAG_ERROR = "error"


# Truncation caps for the reconciliation prompt — kept tight; the brief
# requires compact inputs, not raw dumps.
_MAX_RESULT_MD_CHARS = 4000
_MAX_TASK_CARD_CHARS = 1600
_MAX_MEMORY_FILE_CHARS = 1200
_MAX_SUMMARY_CHARS = 1600
_MAX_LIST_ITEM_CHARS = 240
_MAX_LIST_ENTRIES = 20


# ---------- public data structures ----------


@dataclass
class ReconciliationUpdate:
    """One proposed memory update from the reconciliation judge."""

    filename: str
    section: str
    content: str
    action: str  # "append" or "replace"

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "section": self.section,
            "content": self.content,
            "action": self.action,
        }


@dataclass
class ReconciliationDecision:
    """Structured decision returned by the reconciliation judge."""

    should_update: bool
    reason: str
    updates: list[ReconciliationUpdate] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "should_update": self.should_update,
            "reason": self.reason,
            "updates": [u.to_dict() for u in self.updates],
        }


@dataclass
class ReconciliationOutcome:
    """Result of running the full reconciliation pipeline for one run."""

    tag: str
    reconciled: bool
    reason: str
    applied: list[dict] = field(default_factory=list)
    error: Optional[str] = None


# ---------- skip rules ----------


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _trim_list(items: list[str]) -> list[str]:
    bounded: list[str] = []
    for item in items[:_MAX_LIST_ENTRIES]:
        if not isinstance(item, str):
            item = str(item)
        bounded.append(_truncate(item.strip(), _MAX_LIST_ITEM_CHARS))
    return bounded


def _is_read_only_run(record: RunRecord, summary_text: str) -> bool:
    """True for inspection-only runs where memory shouldn't be touched.

    Heuristic: status is completed or partial, the run made no file changes,
    has no blockers, and (defensively) emitted no real summary text.
    Read-only inspection runs are common when a user asks the Coding Agent to
    look something up — they shouldn't leave a trail in project memory.
    """
    if record.status not in (RunStatus.COMPLETED, RunStatus.PARTIAL):
        return False
    if record.files_changed:
        return False
    if record.blockers:
        return False
    if summary_text and len(summary_text.strip()) >= 32:
        # A meaningful summary may still be worth reconciling (e.g. a research
        # finding the agent wrote up) — let the judge decide.
        return False
    return True


def _is_failed_noisy_run(record: RunRecord, summary_text: str) -> bool:
    """True for failed/blocked runs that produced nothing actionable.

    Failed runs that DID change files or DID produce a meaningful blocker
    description are still worth reconciling — they belong in DECISIONS.md or
    RESEARCH.md as a "this didn't work" note. But a generic crash with no
    useful artifacts shouldn't pollute memory.
    """
    if record.status not in (RunStatus.FAILED, RunStatus.BLOCKED):
        return False
    if record.files_changed:
        return False
    # A non-trivial blocker note or summary is informative — keep it.
    longest_blocker = max((len(b) for b in record.blockers), default=0)
    if longest_blocker >= 60:
        return False
    if summary_text and len(summary_text.strip()) >= 60:
        return False
    return True


# ---------- memory snapshot loader ----------


def _load_project_memory_snapshot(project_id: str) -> dict[str, str]:
    """Load the four writable target files. Missing files come back as ''.

    Truncated per-file so the prompt stays bounded.
    """
    snapshot: dict[str, str] = {}
    project_path = _PROJECTS_DIR / project_id
    if not project_path.exists():
        return {name: "" for name in RECONCILIATION_WRITABLE_FILES}
    for filename in RECONCILIATION_WRITABLE_FILES:
        fpath = project_path / filename
        if fpath.exists():
            try:
                text = fpath.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
        else:
            text = ""
        snapshot[filename] = _truncate(text, _MAX_MEMORY_FILE_CHARS)
    return snapshot


# ---------- prompt construction ----------


_RECONCILIATION_SYSTEM_PROMPT = """\
You are the post-run memory reconciliation subsystem of Agent OS.

A Coding Agent has just finished a bounded run inside a project's sandboxed
`repo/` workspace. Your job: examine a compact summary of that run plus the
current state of the project's memory files, then decide whether any of the
four writable project memory files should be updated to reflect the outcome.

## Files you can write
- STATUS.md: current phase, latest milestone, what works, what's next.
- TASK_QUEUE.md: actionable task tracking with checkboxes (In Progress / Up
  Next / Done sections; `- [ ]` and `- [x]`).
- DECISIONS.md: important project decisions with rationale (including "we
  tried X and it didn't work" notes from failed runs).
- RESEARCH.md: useful findings, external references, technical notes.

## Files you MUST NOT write
- PROJECT.md (project definition — out of scope for run reconciliation).
- SOUL.md or any global memory file.
- Any source file under repo/.

## Rules
1. Be conservative. Most runs do NOT need a memory update. Only propose
   updates when the run produced genuinely new durable knowledge that
   belongs in one of the four files above.
2. Write clean, structured markdown that fits the file's existing format.
   Do NOT dump the run's task card, result.md, or raw logs into the file.
   Summarize and structure.
3. Avoid duplicates: if the current memory snapshot already records this
   outcome, return should_update=false. Do not re-append the same
   information.
4. Read-only / inspection-only runs (no files changed, no blockers, no
   meaningful summary) should usually return should_update=false.
5. Failed runs with no actionable signal should usually return
   should_update=false. Failed runs WITH a meaningful blocker or
   "what we learned" finding may produce one short DECISIONS.md or
   RESEARCH.md entry.
6. Use action "append" for new content, "replace" to overwrite a section.
   For TASK_QUEUE.md use `- [ ]` / `- [x]` checkbox format.
7. Keep each update concise: one update per file, no repetition.
8. The "section" field must name an existing `##` heading in the file, or
   the writer will create that heading.

## Response format
Return ONLY a single JSON object. No markdown fences, no explanation.

Schema:
{
  "should_update": true | false,
  "reason": "one short sentence justifying the decision",
  "updates": [
    {
      "file": "STATUS.md" | "TASK_QUEUE.md" | "DECISIONS.md" | "RESEARCH.md",
      "section": "name of existing or new ## heading",
      "content": "clean markdown to write into that section",
      "action": "append" | "replace"
    }
  ]
}

When should_update is false, "updates" must be an empty array.
"""


def _build_user_prompt(
    project_id: str,
    record: RunRecord,
    summary_text: str,
    task_card: str,
    result_md_text: str,
    memory_snapshot: dict[str, str],
) -> str:
    files_changed = _trim_list(record.files_changed)
    commands_run = _trim_list(record.commands_run)
    blockers = _trim_list(record.blockers)

    def _render_list(label: str, items: list[str]) -> str:
        if not items:
            return f"### {label}\n_(none)_"
        return f"### {label}\n" + "\n".join(f"- {x}" for x in items)

    memory_block_parts: list[str] = []
    for name in ("STATUS.md", "TASK_QUEUE.md", "DECISIONS.md", "RESEARCH.md"):
        body = memory_snapshot.get(name, "") or "(empty)"
        memory_block_parts.append(f"### {name}\n{body}")
    memory_block = "\n\n".join(memory_block_parts)

    return (
        f"## Run\n"
        f"- run_id: `{record.run_id}`\n"
        f"- project_id: `{project_id}`\n"
        f"- status: `{record.status.value}`\n"
        f"- task_title: {record.task_title or '(untitled)'}\n\n"
        f"## Task Card\n{_truncate(task_card.strip(), _MAX_TASK_CARD_CHARS) or '_(none)_'}\n\n"
        f"## Compact Run Outcome\n"
        f"{_render_list('Files Changed', files_changed)}\n\n"
        f"{_render_list('Commands Run', commands_run)}\n\n"
        f"{_render_list('Blockers / Errors', blockers)}\n\n"
        f"### Summary\n{_truncate(summary_text.strip(), _MAX_SUMMARY_CHARS) or '_(no summary)_'}\n\n"
        f"## Rendered result.md\n{_truncate(result_md_text, _MAX_RESULT_MD_CHARS) or '_(no result.md)_'}\n\n"
        f"---\n\n## Current Project Memory (writable targets)\n\n{memory_block}\n\n"
        f"---\n\nDecide whether to update memory based on this run. "
        f"Return ONLY the JSON object."
    )


# ---------- parsing ----------


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if "```" not in t:
        return t
    first = t.find("```")
    last = t.rfind("```")
    if first == last:
        return t
    inner = t[first:last]
    newline = inner.find("\n")
    if newline == -1:
        return ""
    return inner[newline + 1 :].strip()


def _parse_decision(raw: str) -> Optional[ReconciliationDecision]:
    """Parse the judge's JSON object. Returns None on malformed input."""
    text = _strip_code_fence(raw)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Reconciliation judge returned invalid JSON: %s", text[:200])
        return None
    if not isinstance(parsed, dict):
        log.warning("Reconciliation judge returned non-object: %s", type(parsed))
        return None

    should_update = bool(parsed.get("should_update", False))
    reason = str(parsed.get("reason", "")).strip()
    raw_updates = parsed.get("updates", []) or []
    if not isinstance(raw_updates, list):
        raw_updates = []

    updates: list[ReconciliationUpdate] = []
    for entry in raw_updates:
        if not isinstance(entry, dict):
            continue
        # Tolerate both "file" (per task brief schema) and "filename" (the
        # older memory-writeback schema) — same field, two names in the
        # surrounding codebase.
        filename = str(entry.get("file") or entry.get("filename") or "").strip()
        section = str(entry.get("section") or "").strip()
        content = str(entry.get("content") or "")
        action = str(entry.get("action") or "append").strip().lower()
        if filename not in RECONCILIATION_WRITABLE_FILES:
            continue
        if action not in ("append", "replace"):
            action = "append"
        if not section:
            section = _default_section_for(filename)
        if not content.strip():
            continue
        updates.append(
            ReconciliationUpdate(
                filename=filename,
                section=section,
                content=content,
                action=action,
            )
        )

    if not updates:
        # Coerce: if the judge said "update" but produced nothing valid, treat
        # as a no-op decision so the caller can record a clean skip tag.
        should_update = False

    return ReconciliationDecision(
        should_update=should_update,
        reason=reason,
        updates=updates,
    )


def _default_section_for(filename: str) -> str:
    return {
        "STATUS.md": "What Works",
        "TASK_QUEUE.md": "Done",
        "DECISIONS.md": "Decisions",
        "RESEARCH.md": "Findings",
    }.get(filename, "Notes")


# ---------- judge entry point ----------


def judge_run_memory_reconciliation(
    project_id: str,
    record: RunRecord,
    summary_text: str,
    task_card: str,
    result_md_text: str,
    *,
    memory_snapshot: Optional[dict[str, str]] = None,
    llm_caller: Optional[Callable] = None,
) -> Optional[ReconciliationDecision]:
    """Run the reconciliation judge LLM call.

    Returns ``None`` if the LLM call fails or the response can't be parsed —
    the caller treats that as "skip with an error tag", not as a fatal
    failure.

    ``llm_caller`` is an optional injection seam for tests; the default is
    the live LLM.
    """
    snapshot = memory_snapshot if memory_snapshot is not None else _load_project_memory_snapshot(project_id)
    user_prompt = _build_user_prompt(
        project_id=project_id,
        record=record,
        summary_text=summary_text,
        task_card=task_card,
        result_md_text=result_md_text,
        memory_snapshot=snapshot,
    )
    caller = llm_caller or llm_chat
    try:
        raw = caller(
            system=_RECONCILIATION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=1024,
        )
    except Exception as exc:
        log.warning("Reconciliation judge LLM call failed: %s", exc)
        return None
    return _parse_decision(raw)


# ---------- write path ----------


def _apply_update(project_id: str, update: ReconciliationUpdate) -> bool:
    """Apply one update against the on-disk project memory file.

    Behaves the same as ``orchestrator.apply_memory_update`` but enforces the
    tighter ``RECONCILIATION_WRITABLE_FILES`` allow-list. Re-implemented here
    to avoid a circular import.
    """
    if update.filename not in RECONCILIATION_WRITABLE_FILES:
        return False
    project_path = _PROJECTS_DIR / project_id
    if not project_path.exists():
        return False
    filepath = project_path / update.filename
    current = ""
    if filepath.exists():
        try:
            current = filepath.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Reconciliation: could not read %s: %s", filepath, exc)
            return False

    content = update.content
    if update.action == "append":
        if current and not current.endswith("\n"):
            current += "\n"
        # Cheap dedup: if the exact content body is already present, skip the
        # write to avoid duplicate noisy entries when a run is reconciled twice
        # in pathological cases.
        if content.strip() and content.strip() in current:
            return False
        current += content + ("\n" if not content.endswith("\n") else "")
        try:
            filepath.write_text(current, encoding="utf-8")
        except OSError as exc:
            log.warning("Reconciliation: could not write %s: %s", filepath, exc)
            return False
        return True

    if update.action == "replace":
        lines = current.split("\n")
        new_lines: list[str] = []
        in_section = False
        replaced = False
        for line in lines:
            if line.startswith(f"## {update.section}"):
                new_lines.append(line)
                new_lines.append(content)
                in_section = True
                replaced = True
                continue
            if in_section and line.startswith("## "):
                in_section = False
            if not in_section:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"\n## {update.section}")
            new_lines.append(content)
        try:
            filepath.write_text("\n".join(new_lines), encoding="utf-8")
        except OSError as exc:
            log.warning("Reconciliation: could not write %s: %s", filepath, exc)
            return False
        return True

    return False


# ---------- public pipeline ----------


def reconcile_run_memory(
    project_id: str,
    run_id: str,
    *,
    summary_override: Optional[str] = None,
    llm_caller: Optional[Callable] = None,
) -> ReconciliationOutcome:
    """Top-level reconciliation entry point.

    Loads the persisted ``RunRecord`` for ``(project_id, run_id)``, applies
    skip rules, calls the judge, applies any allowed updates, and writes
    the reconciliation metadata back to ``run.json``.

    This function NEVER raises. All errors are captured into the returned
    ``ReconciliationOutcome`` and into the ``RunRecord``'s reconciliation
    fields so the caller (runner finalize / background manager) can stay
    focused on its own success path.
    """
    try:
        return _reconcile_inner(
            project_id=project_id,
            run_id=run_id,
            summary_override=summary_override,
            llm_caller=llm_caller,
        )
    except Exception as exc:
        log.exception("Reconciliation pipeline crashed for run %s", run_id)
        outcome = ReconciliationOutcome(
            tag=TAG_ERROR,
            reconciled=False,
            reason=f"reconciliation pipeline crashed: {type(exc).__name__}: {exc}",
            error=f"{type(exc).__name__}: {exc}",
        )
        _persist_outcome(project_id, run_id, outcome)
        return outcome


def _reconcile_inner(
    project_id: str,
    run_id: str,
    summary_override: Optional[str],
    llm_caller: Optional[Callable],
) -> ReconciliationOutcome:
    raw_record = run_store.read_run_json(project_id, run_id)
    if raw_record is None:
        return ReconciliationOutcome(
            tag=TAG_ERROR,
            reconciled=False,
            reason="run.json not found",
            error="run.json missing",
        )
    try:
        record = RunRecord(**raw_record)
    except Exception as exc:
        return ReconciliationOutcome(
            tag=TAG_ERROR,
            reconciled=False,
            reason=f"could not parse run.json: {exc}",
            error=f"{type(exc).__name__}: {exc}",
        )

    if record.status not in TERMINAL_STATUSES:
        outcome = ReconciliationOutcome(
            tag=TAG_SKIPPED_NON_TERMINAL,
            reconciled=False,
            reason=f"status is {record.status.value!r}, not terminal",
        )
        return outcome

    if record.memory_reconciled is not None or record.memory_reconciliation:
        return ReconciliationOutcome(
            tag=TAG_SKIPPED_ALREADY_RECONCILED,
            reconciled=bool(record.memory_reconciled),
            reason="already reconciled",
        )

    result_md_text = run_store.read_result_md(project_id, run_id) or ""
    summary_text = (summary_override or "").strip() or _extract_summary_from_result_md(result_md_text)
    task_card = _read_task_card(project_id, run_id)

    if _is_read_only_run(record, summary_text):
        outcome = ReconciliationOutcome(
            tag=TAG_SKIPPED_READ_ONLY,
            reconciled=False,
            reason="run made no file changes and produced no actionable summary",
        )
        _persist_outcome(project_id, run_id, outcome)
        return outcome

    if _is_failed_noisy_run(record, summary_text):
        outcome = ReconciliationOutcome(
            tag=TAG_SKIPPED_FAILED_NOISY,
            reconciled=False,
            reason="run failed/blocked without an actionable signal",
        )
        _persist_outcome(project_id, run_id, outcome)
        return outcome

    decision = judge_run_memory_reconciliation(
        project_id=project_id,
        record=record,
        summary_text=summary_text,
        task_card=task_card,
        result_md_text=result_md_text,
        llm_caller=llm_caller,
    )
    if decision is None:
        outcome = ReconciliationOutcome(
            tag=TAG_ERROR,
            reconciled=False,
            reason="reconciliation judge returned no usable decision",
            error="judge failed or returned malformed JSON",
        )
        _persist_outcome(project_id, run_id, outcome)
        return outcome

    if not decision.should_update or not decision.updates:
        outcome = ReconciliationOutcome(
            tag=TAG_SKIPPED_JUDGE_NO_UPDATE,
            reconciled=False,
            reason=decision.reason or "judge declined to update memory",
        )
        _persist_outcome(project_id, run_id, outcome)
        return outcome

    applied: list[dict] = []
    for update in decision.updates:
        ok = _apply_update(project_id, update)
        if ok:
            applied.append({**update.to_dict(), "applied": True})

    if not applied:
        outcome = ReconciliationOutcome(
            tag=TAG_SKIPPED_NO_VALID_UPDATES,
            reconciled=False,
            reason="no updates passed the policy filter / dedup check",
        )
        _persist_outcome(project_id, run_id, outcome)
        return outcome

    outcome = ReconciliationOutcome(
        tag=TAG_APPLIED,
        reconciled=True,
        reason=decision.reason or "applied reconciliation updates",
        applied=applied,
    )
    _persist_outcome(project_id, run_id, outcome)
    return outcome


# ---------- helpers ----------


def _read_task_card(project_id: str, run_id: str) -> str:
    """Read ``task_card.md`` from the run directory. Returns '' if missing."""
    path = run_store.get_run_dir(project_id, run_id) / "task_card.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _extract_summary_from_result_md(result_md_text: str) -> str:
    """Pull the ``## Summary`` body out of a rendered result.md.

    The runner builds result.md from a fixed template, so a section-by-section
    extract is more useful to the judge than the full markdown blob with
    headings.
    """
    if not result_md_text:
        return ""
    marker = "## Summary"
    idx = result_md_text.find(marker)
    if idx == -1:
        return ""
    rest = result_md_text[idx + len(marker) :].lstrip("\n")
    next_idx = rest.find("\n## ")
    section = rest if next_idx == -1 else rest[:next_idx]
    return section.strip()


def _persist_outcome(project_id: str, run_id: str, outcome: ReconciliationOutcome) -> None:
    """Write the reconciliation metadata back into run.json.

    Reads-modifies-writes the on-disk record so we don't clobber any fields
    written by the runner between finalize and reconciliation. Best-effort:
    a write failure here is logged and swallowed so the run itself stays
    in its terminal state.
    """
    try:
        raw = run_store.read_run_json(project_id, run_id)
        if raw is None:
            return
        try:
            record = RunRecord(**raw)
        except Exception:
            return
        record.memory_reconciled = outcome.reconciled
        record.memory_reconciliation = outcome.tag
        record.memory_reconciliation_error = outcome.error
        run_store.write_run_json(project_id, run_id, record)
        run_store.append_event(
            project_id,
            run_id,
            {
                "type": "memory_reconciled",
                "tag": outcome.tag,
                "reconciled": outcome.reconciled,
                "reason": _truncate(outcome.reason, 240),
                "applied_count": len(outcome.applied),
                "error": _truncate(outcome.error or "", 240),
            },
        )
    except Exception as exc:
        log.warning("Could not persist reconciliation outcome for %s: %s", run_id, exc)
