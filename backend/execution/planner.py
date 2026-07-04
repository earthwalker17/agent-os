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
from .roles import get_role, normalize_role_id


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

# A task id is used verbatim as a filesystem path segment for its Phase 9 patch
# workspace (``patches/{run_id}/{task_id}/``), so it MUST be a safe single
# segment. The LLM emits ids freely, so anything with a path separator, ``..``,
# or exotic characters is rejected at parse time and replaced with ``t{idx}``
# (see ``_safe_task_id``) — closing an out-of-sandbox-write path escape.
_SAFE_TASK_ID_REGEX = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def is_safe_task_id(tid: str) -> bool:
    """Whether ``tid`` is a safe single path segment (no separators / traversal)."""
    if not isinstance(tid, str) or not _SAFE_TASK_ID_REGEX.match(tid):
        return False
    return tid not in (".", "..")


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
        # Reject unsafe ids (path separators / traversal / exotic chars) — the id
        # becomes a filesystem path segment for the task's patch workspace, so an
        # unsanitized id could escape the sandbox. Unsafe / empty / duplicate ids
        # fall back to the positional ``t{idx}``.
        if not is_safe_task_id(tid) or tid in used_ids:
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
        # Phase 9 — team fields, parsed tolerantly. Unknown/absent role falls
        # back to the coder; ``parallel`` accepts a bool or a truthy string.
        role = normalize_role_id(raw.get("role"))
        parallel_raw = raw.get("parallel", raw.get("parallel_safe", False))
        if isinstance(parallel_raw, bool):
            parallel_safe = parallel_raw
        else:
            parallel_safe = str(parallel_raw).strip().lower() in ("true", "1", "yes")
        tasks.append(
            ExecutionTask(
                id=tid,
                title=title,
                description=description,
                depends_on=depends_on,
                status=TaskStatus.PENDING,
                role=role,
                parallel_safe=parallel_safe,
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


def _dependency_provides_output(dep: ExecutionTask) -> bool:
    """True if a dependency produced something a dependent can build on.

    A COMPLETED dependency obviously qualifies. A FAILED dependency that still
    wrote files (e.g. a scaffold task that laid down package.json + config but
    exhausted its step budget before emitting ``final``) also qualifies — its
    output is on disk and a dependent can use it, possibly after the post-run
    repair pass. A SKIPPED dependency never ran, so it provides nothing.
    """
    if dep.status == TaskStatus.COMPLETED:
        return True
    if dep.status == TaskStatus.FAILED and dep.files_changed:
        return True
    return False


def dependency_failed(
    task: ExecutionTask, by_id: dict[str, ExecutionTask]
) -> Optional[str]:
    """Return a skip reason when a task cannot proceed, else None.

    Progress-aware (hardening): a dependent is skipped only when NONE of its
    dependencies produced usable output. If at least one dependency completed —
    or failed but left files on disk — the task RUNS even though other
    dependencies failed/were skipped; the runner hands the incomplete ones to
    the task as a degraded-dependency note (see :func:`degraded_dependencies`)
    so it can compensate (e.g. inline minimal data). This stops one brittle
    task from collapsing an entire multi-task run through the skip cascade — the
    failure mode that skipped six/seven downstream tasks in the Aegis build
    after a single upstream task failed.

    Returns ``None`` when there are no dependencies or at least one provides
    output; otherwise a reason naming the unsatisfied dependencies.
    """
    deps = [by_id[d] for d in task.depends_on if d in by_id]
    if not deps:
        return None
    # Only resolved (terminal) dependencies can block. A dependency still
    # PENDING/RUNNING is not-yet-decided and must not block — this matches the
    # pre-hardening behavior and preserves topological_order's cycle-remainder
    # guarantee (a task left in a dependency cycle is still run, not silently
    # skipped). On the normal forward path deps are always terminal here.
    terminal = [
        d
        for d in deps
        if d.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SKIPPED)
    ]
    if not terminal:
        return None
    if any(_dependency_provides_output(d) for d in terminal):
        return None
    return "; ".join(
        f"dependency {d.id!r} did not complete ({d.status.value})" for d in terminal
    )


def degraded_dependencies(
    task: ExecutionTask, by_id: dict[str, ExecutionTask]
) -> list[str]:
    """Short notes for dependencies that didn't cleanly complete but didn't block.

    Only meaningful for a task that IS going to run (``dependency_failed``
    returned ``None``). Each entry is a human-readable description the runner
    folds into the task-unit prompt so the agent knows which upstream outputs
    may be missing or incomplete and can work around them.
    """
    notes: list[str] = []
    for dep in (by_id[d] for d in task.depends_on if d in by_id):
        if dep.status == TaskStatus.COMPLETED:
            continue
        if dep.status == TaskStatus.FAILED and dep.files_changed:
            state = "incomplete (wrote some files but did not finish)"
        else:
            state = dep.status.value
        notes.append(f"{dep.id} ({dep.title}): {state}")
    return notes


# ---------- Phase 9 — team scheduling helpers ----------

# Bounded parallelism: at most this many agents run concurrently inside one
# team run. Deliberately small — correctness and auditability over maximum
# parallelism (BLUEPRINT Pillar 3); the per-run scheduler chunks larger waves.
MAX_PARALLEL_AGENTS = 3


def compute_waves(
    tasks: list[ExecutionTask],
) -> tuple[list[list[ExecutionTask]], set[str]]:
    """Layer the task graph into topological waves (Kahn layering).

    Wave N contains every task whose dependencies all sit in waves < N, so
    tasks within one wave never depend on each other. Returns
    ``(waves, cyclic_ids)`` — tasks caught in a dependency cycle are appended
    as one final wave in original order and reported in ``cyclic_ids`` so the
    scheduler can force them sequential (a cycle means the planner's ordering
    intent is unknowable; running them concurrently would be a guess).
    Dependency ids no task provides are ignored (already pruned by
    ``plan_from_dict``).
    """
    by_id = {t.id: t for t in tasks}
    deps: dict[str, set[str]] = {
        t.id: {d for d in t.depends_on if d in by_id and d != t.id} for t in tasks
    }
    order_index = {t.id: i for i, t in enumerate(tasks)}

    waves: list[list[ExecutionTask]] = []
    placed: set[str] = set()
    remaining = [t.id for t in tasks]
    while remaining:
        ready = [tid for tid in remaining if deps[tid] <= placed]
        if not ready:
            break  # cycle remainder
        ready.sort(key=order_index.get)
        waves.append([by_id[tid] for tid in ready])
        placed.update(ready)
        remaining = [tid for tid in remaining if tid not in placed]

    cyclic: set[str] = set(remaining)
    if remaining:
        remaining.sort(key=order_index.get)
        waves.append([by_id[tid] for tid in remaining])
    return waves, cyclic


def task_parallel_eligible(task: ExecutionTask) -> bool:
    """Whether a task may run concurrently with siblings in its wave.

    Read-only roles (reviewer / inspector) are inherently parallel-safe —
    they cannot mutate anything. Write tasks are eligible only when the
    planner explicitly marked them ``parallel_safe`` (conservative default:
    sequential).
    """
    if get_role(task.role).read_only:
        return True
    return bool(task.parallel_safe)


def plan_is_team_eligible(plan: ExecutionPlan) -> bool:
    """Whether the runner should use the team (wave / parallel) path.

    Conservative gate: only an LLM-planned multi-task plan where at least one
    wave holds two or more runnable, parallel-eligible tasks. Everything else
    keeps the legacy sequential loop byte-identical.
    """
    if plan.mode != "planned":
        return False
    runnable = [t for t in plan.tasks if t.status != TaskStatus.SKIPPED]
    if len(runnable) < 2:
        return False
    waves, cyclic = compute_waves(runnable)
    for wave in waves:
        eligible = [
            t for t in wave if t.id not in cyclic and task_parallel_eligible(t)
        ]
        if len(eligible) >= 2:
            return True
    return False


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
