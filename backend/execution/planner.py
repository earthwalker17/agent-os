"""Planning layer for the Coding Agent runner (Phase 5).

Pure logic + parsing, deliberately free of any runner / LLM / filesystem state
so it is trivially unit-testable. The runner owns the bounded planning *loop*
(it already has the LLM-step + tool-dispatch machinery); this module owns:

  - ``looks_complex`` — the cheap, pure heuristic gate that decides whether a
    task card is worth an LLM planning pass at all. Simple cards skip planning
    entirely and run the existing single tool loop (no extra LLM call), which
    is what keeps the legacy runner behavior + tests intact.
  - ``parse_plan`` / ``plan_from_dict`` — tolerant JSON → :class:`ExecutionPlan`
    normalization (id assignment, dependency validation, task cap).
  - ``fallback_plan`` — a single-task plan covering the whole card, used both
    for simple cards and as the safety net when planning fails.
  - graph helpers — ``topological_order`` (cycle-safe), ``dependency_failed``,
    and ``aggregate_run_status`` for turning per-task outcomes into a run-level
    status.

Nothing here touches ``repo/`` or the shell; the sandbox chokepoint lives in
the runner's tool dispatch.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .models import ExecutionPlan, ExecutionTask, TaskSpec, TaskStatus


# Upper bound on tasks in a single plan. The planning prompt asks for a small
# number; anything beyond this is clamped (overflow tasks are marked SKIPPED,
# not silently dropped — see ``plan_from_dict``) so a runaway plan can't fan
# out unbounded work.
MAX_TASKS = 8

# Heuristic thresholds for ``looks_complex`` — see the function docstring.
_COMPLEX_CHAR_THRESHOLD = 240
_COMPLEX_LINE_THRESHOLD = 4
_COMPLEX_LIST_ITEM_THRESHOLD = 2
_COMPLEX_SENTENCE_THRESHOLD = 3
# A single sentence can still enumerate many deliverables via commas /
# conjunctions; >= this many such clauses (each with real content) reads as
# multi-part work worth planning.
_COMPLEX_CLAUSE_THRESHOLD = 3

_LIST_ITEM_REGEX = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", re.MULTILINE)
_SENTENCE_SPLIT_REGEX = re.compile(r"[.!?]+")
_CLAUSE_SPLIT_REGEX = re.compile(r",|\band\b|&|;", re.IGNORECASE)


class PlanParseError(Exception):
    """Raised when an LLM planning response cannot be turned into a usable plan."""


def looks_complex(task_card: str) -> bool:
    """Cheap, pure heuristic: is this task card worth an LLM planning pass?

    Returns ``True`` for cards that look like they describe multiple
    deliverables / steps, ``False`` for short, single-shot instructions. This
    is intentionally conservative — a false negative just means a card runs the
    existing single tool loop (today's behavior), so misclassifying borderline
    cards as "simple" is the safe direction.

    A card is "complex" when ANY of these hold:
      - it is long (>= ``_COMPLEX_CHAR_THRESHOLD`` chars), or
      - it has several non-empty lines (>= ``_COMPLEX_LINE_THRESHOLD``), or
      - it enumerates work as a list (>= ``_COMPLEX_LIST_ITEM_THRESHOLD``
        bullet/numbered items), or
      - it reads as several sentences (>= ``_COMPLEX_SENTENCE_THRESHOLD``).

    Must classify trivial cards like ``"do"`` / ``"do it"`` as simple.
    """
    if not task_card:
        return False
    text = task_card.strip()
    if not text:
        return False

    if len(text) >= _COMPLEX_CHAR_THRESHOLD:
        return True

    non_empty_lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(non_empty_lines) >= _COMPLEX_LINE_THRESHOLD:
        return True

    if len(_LIST_ITEM_REGEX.findall(text)) >= _COMPLEX_LIST_ITEM_THRESHOLD:
        return True

    sentences = [s for s in _SENTENCE_SPLIT_REGEX.split(text) if s.strip()]
    if len(sentences) >= _COMPLEX_SENTENCE_THRESHOLD:
        return True

    # A single comma/conjunction-enumerated sentence (no terminal '.') like
    # "build a dashboard with multiple views, local CRUD, filtering and grouping,
    # and a responsive frontend" describes multi-part work but slips past every
    # check above. Count clauses with real content (>= 2 words each) so a terse
    # one-line full-stack card still trips planning. A false positive only costs
    # one cheap planning call (which can still fall back to a single task); the
    # ">= 2 words" filter keeps trivial cards ("do it") simple.
    clauses = [c for c in _CLAUSE_SPLIT_REGEX.split(text) if len(c.split()) >= 2]
    if len(clauses) >= _COMPLEX_CLAUSE_THRESHOLD:
        return True

    return False


def fallback_plan(task: TaskSpec, mode: str = "simple") -> ExecutionPlan:
    """A single-task plan covering the whole task card.

    Used both for heuristically-simple cards (``mode="simple"``) and as the
    safety net when LLM planning fails (``mode="fallback"``). The single task
    runs the existing tool loop verbatim, so this preserves legacy behavior.
    """
    return ExecutionPlan(
        goal=task.title.strip() or "Execute the task",
        analysis="",
        risks=[],
        tasks=[
            ExecutionTask(
                id="t1",
                title=task.title.strip() or "Task",
                description=task.task_card.strip(),
                status=TaskStatus.PENDING,
            )
        ],
        mode=mode,
    )


def plan_from_dict(data: dict[str, Any], task: TaskSpec) -> ExecutionPlan:
    """Normalize a parsed plan object into an :class:`ExecutionPlan`.

    Assigns stable ids, drops dangling ``depends_on`` references, and clamps to
    ``MAX_TASKS`` (overflow tasks are kept but marked SKIPPED so the artifact
    stays honest about what was dropped). Raises :class:`PlanParseError` when
    there is nothing usable (no tasks).
    """
    if not isinstance(data, dict):
        raise PlanParseError("plan is not a JSON object")

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanParseError("plan has no tasks")

    tasks: list[ExecutionTask] = []
    used_ids: set[str] = set()
    for idx, raw in enumerate(raw_tasks, start=1):
        if not isinstance(raw, dict):
            continue
        tid = str(raw.get("id") or "").strip()
        if not tid or tid in used_ids:
            tid = f"t{idx}"
        # Guard against a generated id colliding with a later explicit one.
        while tid in used_ids:
            tid = f"{tid}_{idx}"
        used_ids.add(tid)
        title = str(raw.get("title") or "").strip() or f"Task {idx}"
        description = str(raw.get("description") or "").strip()
        depends_on = [
            str(d).strip()
            for d in (raw.get("depends_on") or [])
            if isinstance(d, (str, int)) and str(d).strip()
        ]
        tasks.append(
            ExecutionTask(
                id=tid,
                title=title,
                description=description,
                depends_on=depends_on,
                status=TaskStatus.PENDING,
            )
        )

    if not tasks:
        raise PlanParseError("plan tasks could not be parsed")

    # Drop dangling dependency references (an id that no task provides).
    valid_ids = {t.id for t in tasks}
    for t in tasks:
        t.depends_on = [d for d in t.depends_on if d in valid_ids and d != t.id]

    # Clamp to MAX_TASKS: keep the first N executable, mark the rest skipped so
    # the plan artifact records what was dropped rather than hiding it.
    if len(tasks) > MAX_TASKS:
        for overflow in tasks[MAX_TASKS:]:
            overflow.status = TaskStatus.SKIPPED
            overflow.blockers = [f"dropped: plan exceeded the {MAX_TASKS}-task cap"]

    risks = [
        str(r).strip()
        for r in (data.get("risks") or [])
        if isinstance(r, (str, int)) and str(r).strip()
    ]

    return ExecutionPlan(
        goal=str(data.get("goal") or task.title or "").strip(),
        analysis=str(data.get("analysis") or "").strip(),
        risks=risks,
        tasks=tasks,
        mode="planned",
    )


def parse_plan(raw: str, task: TaskSpec) -> ExecutionPlan:
    """Tolerant parse of an LLM planning response into an ExecutionPlan.

    Accepts a bare JSON object, a ```json fenced block, or a plan object
    embedded in surrounding prose. Raises :class:`PlanParseError` on any
    failure so the caller can fall back to a single-task plan.
    """
    if not raw or not raw.strip():
        raise PlanParseError("empty planning response")

    text = raw.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        snippet = _extract_first_json_object(text)
        if snippet is None:
            raise PlanParseError("planning response is not valid JSON")
        try:
            parsed = json.loads(snippet)
        except json.JSONDecodeError as e:
            raise PlanParseError(f"planning response is not valid JSON: {e}")

    if not isinstance(parsed, dict):
        raise PlanParseError("planning response JSON is not an object")
    return plan_from_dict(parsed, task)


# ---------- graph helpers ----------


def topological_order(tasks: list[ExecutionTask]) -> list[ExecutionTask]:
    """Return tasks ordered so dependencies precede dependents (Kahn's).

    Cycle-safe: any tasks left in a dependency cycle are appended in their
    original order rather than dropped or looped on. Dependency ids that no
    task provides are ignored (already pruned by ``plan_from_dict``, but this
    stays robust if called directly).
    """
    by_id = {t.id: t for t in tasks}
    indeg: dict[str, int] = {}
    dependents: dict[str, list[str]] = {t.id: [] for t in tasks}
    for t in tasks:
        deps = [d for d in t.depends_on if d in by_id and d != t.id]
        indeg[t.id] = len(deps)
        for d in deps:
            dependents[d].append(t.id)

    # Preserve original order among ready tasks for stable, predictable runs.
    order_index = {t.id: i for i, t in enumerate(tasks)}
    ready = sorted([t.id for t in tasks if indeg[t.id] == 0], key=order_index.get)
    ordered_ids: list[str] = []
    while ready:
        tid = ready.pop(0)
        ordered_ids.append(tid)
        for dep_id in dependents[tid]:
            indeg[dep_id] -= 1
            if indeg[dep_id] == 0:
                # Insert keeping original-order stability.
                ready.append(dep_id)
                ready.sort(key=order_index.get)

    if len(ordered_ids) < len(tasks):
        # Cycle remainder — append leftovers in original order.
        seen = set(ordered_ids)
        for t in tasks:
            if t.id not in seen:
                ordered_ids.append(t.id)

    return [by_id[tid] for tid in ordered_ids]


def dependency_failed(
    task: ExecutionTask, by_id: dict[str, ExecutionTask]
) -> Optional[str]:
    """Return a reason string if any dependency failed/was skipped, else None."""
    for dep_id in task.depends_on:
        dep = by_id.get(dep_id)
        if dep is None:
            continue
        if dep.status in (TaskStatus.FAILED, TaskStatus.SKIPPED):
            return f"dependency {dep_id!r} did not complete ({dep.status.value})"
    return None


def task_status_from_final(final_status: str) -> TaskStatus:
    """Map a per-task `final` action status onto a TaskStatus.

    A task counts as COMPLETED only when its loop reported ``completed``;
    every other terminal value (``partial`` / ``blocked`` / ``failed``) maps to
    FAILED, with the detail preserved in the task's blockers.
    """
    return TaskStatus.COMPLETED if final_status == "completed" else TaskStatus.FAILED


def aggregate_run_status(tasks: list[ExecutionTask]) -> tuple[str, str, list[str]]:
    """Collapse per-task outcomes into a run-level (status, summary, blockers).

    Mapping (multi-task path only — the single-task path passes the agent's raw
    final status straight through so legacy semantics + verification gating are
    untouched):

      - every task COMPLETED              -> ``"completed"`` (so command
        verification still gates the run and can downgrade to ``partial``)
      - some COMPLETED, some not          -> ``"partial"``
      - none COMPLETED                    -> ``"failed"``

    A non-``completed`` aggregate does NOT receive the command-verification
    repair pass (that pass only fires on a ``completed`` run); this is intended
    — a run that already failed tasks should not be auto-repaired into green.
    """
    total = len(tasks)
    completed = [t for t in tasks if t.status == TaskStatus.COMPLETED]
    failed = [t for t in tasks if t.status == TaskStatus.FAILED]
    skipped = [t for t in tasks if t.status == TaskStatus.SKIPPED]

    if total and len(completed) == total:
        status = "completed"
    elif completed:
        status = "partial"
    else:
        status = "failed"

    summary = (
        f"Executed {total} planned task{'s' if total != 1 else ''}: "
        f"{len(completed)} completed, {len(failed)} failed, {len(skipped)} skipped."
    )

    blockers: list[str] = []
    for t in tasks:
        if t.status in (TaskStatus.FAILED, TaskStatus.SKIPPED):
            for b in t.blockers:
                line = f"{t.id} ({t.title}): {b}"
                if line not in blockers:
                    blockers.append(line)
    return status, summary, blockers


def _extract_first_json_object(text: str) -> str | None:
    """Return the first balanced ``{ ... }`` object in ``text`` (quote-aware)."""
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : i + 1]
    return None
