"""Tests for Phase 5 — the pure planning layer (execution/planner.py).

Coverage:
  - looks_complex: trivial cards (incl. the legacy runner cards "do"/"do it")
    classify simple; long / bulleted / numbered / multi-sentence / multi-line
    cards classify complex. (Guards the heuristic gate that keeps the legacy
    runner tests' LLM call counts intact.)
  - parse_plan / plan_from_dict: valid, fenced, prose-embedded, malformed,
    zero-task, id assignment, dangling-dependency pruning, MAX_TASKS overflow.
  - topological_order: dependency ordering, stable independent order, cycle-safe.
  - dependency_failed / task_status_from_final / aggregate_run_status.

Run directly:
    python backend/tests/test_planner.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from execution.models import ExecutionTask, TaskSpec, TaskStatus  # noqa: E402
from execution.planner import (  # noqa: E402
    MAX_TASKS,
    PlanParseError,
    aggregate_run_status,
    dependency_failed,
    fallback_plan,
    looks_complex,
    parse_plan,
    plan_from_dict,
    task_status_from_final,
    topological_order,
)


def _task(tid: str, title: str = "T", **over) -> dict:
    body = {"id": tid, "title": title, "description": "d", "depends_on": []}
    body.update(over)
    return body


# ---------- looks_complex ----------


def test_looks_complex_trivial_cards_are_simple():
    assert looks_complex("do") is False
    assert looks_complex("do it") is False
    assert looks_complex("do the thing") is False
    assert looks_complex("fix the typo in README") is False
    assert looks_complex("") is False
    assert looks_complex("   ") is False


def test_looks_complex_long_card():
    assert looks_complex("x " * 200) is True


def test_looks_complex_bulleted_card():
    assert looks_complex("Build it:\n- add config\n- add helper") is True


def test_looks_complex_numbered_card():
    assert looks_complex("Steps:\n1. scaffold\n2. wire it up") is True


def test_looks_complex_multi_sentence_card():
    assert looks_complex("Add the model. Then the endpoint. Then a test.") is True


def test_looks_complex_multi_line_card():
    assert looks_complex("alpha\nbeta\ngamma\ndelta") is True


def test_looks_complex_terse_clause_enumerated_card():
    # A realistic one-line full-stack dispatch: single sentence, no terminal '.',
    # under 240 chars, but enumerates several deliverables via commas/conjunctions.
    # Must classify complex so Phase 5 planning engages (regression guard for the
    # 'terse LaunchBoard card silently skips decomposition' thin-spot).
    card = (
        "Build a LaunchBoard planning dashboard with multiple views, local CRUD, "
        "filtering and grouping, and a responsive frontend"
    )
    assert len(card) < 240  # not caught by the char-length rule
    assert "\n" not in card  # not caught by the multi-line rule
    assert looks_complex(card) is True


def test_looks_complex_short_two_part_card_stays_simple():
    # One comma / one conjunction is not enough to trip the clause heuristic, so a
    # genuinely small two-part card still runs the legacy single loop.
    assert looks_complex("add a login form and a logout button") is False
    assert looks_complex("rename the helper, then export it") is False


# ---------- fallback_plan ----------


def test_fallback_plan_single_task():
    plan = fallback_plan(TaskSpec(title="My task", task_card="do the work"))
    assert plan.mode == "simple"
    assert len(plan.tasks) == 1
    assert plan.tasks[0].id == "t1"
    assert plan.tasks[0].description == "do the work"
    assert plan.tasks[0].status == TaskStatus.PENDING
    assert plan.goal == "My task"


def test_fallback_plan_mode_override():
    plan = fallback_plan(TaskSpec(title="t", task_card="c"), mode="fallback")
    assert plan.mode == "fallback"


# ---------- parse_plan / plan_from_dict ----------


def test_parse_plan_valid():
    raw = json.dumps(
        {
            "action": "plan",
            "goal": "ship it",
            "analysis": "empty repo",
            "risks": ["none"],
            "tasks": [_task("t1", "A"), _task("t2", "B", depends_on=["t1"])],
        }
    )
    plan = parse_plan(raw, TaskSpec(title="t", task_card="c"))
    assert plan.mode == "planned"
    assert plan.goal == "ship it"
    assert [t.id for t in plan.tasks] == ["t1", "t2"]
    assert plan.tasks[1].depends_on == ["t1"]


def test_parse_plan_fenced():
    raw = "```json\n" + json.dumps({"tasks": [_task("t1", "A")]}) + "\n```"
    plan = parse_plan(raw, TaskSpec(title="t", task_card="c"))
    assert len(plan.tasks) == 1


def test_parse_plan_embedded_in_prose():
    raw = "Here is the plan: " + json.dumps({"tasks": [_task("t1", "A")]}) + " thanks!"
    plan = parse_plan(raw, TaskSpec(title="t", task_card="c"))
    assert len(plan.tasks) == 1


def test_parse_plan_malformed_raises():
    try:
        parse_plan("not json at all", TaskSpec(title="t", task_card="c"))
    except PlanParseError:
        return
    raise AssertionError("expected PlanParseError")


def test_parse_plan_zero_tasks_raises():
    try:
        parse_plan(json.dumps({"tasks": []}), TaskSpec(title="t", task_card="c"))
    except PlanParseError:
        return
    raise AssertionError("expected PlanParseError")


def test_plan_from_dict_assigns_missing_ids():
    data = {"tasks": [{"title": "A"}, {"title": "B"}]}
    plan = plan_from_dict(data, TaskSpec(title="t", task_card="c"))
    assert [t.id for t in plan.tasks] == ["t1", "t2"]


def test_plan_from_dict_prunes_dangling_dependencies():
    data = {"tasks": [_task("t1", "A", depends_on=["ghost", "t1"])]}
    plan = plan_from_dict(data, TaskSpec(title="t", task_card="c"))
    # "ghost" (no such task) and the self-ref "t1" are both dropped.
    assert plan.tasks[0].depends_on == []


def test_plan_from_dict_overflow_marked_skipped():
    data = {"tasks": [_task(f"t{i}", f"T{i}") for i in range(1, MAX_TASKS + 3)]}
    plan = plan_from_dict(data, TaskSpec(title="t", task_card="c"))
    assert len(plan.tasks) == MAX_TASKS + 2
    # First MAX_TASKS executable, the rest pre-skipped with a blocker.
    for t in plan.tasks[:MAX_TASKS]:
        assert t.status == TaskStatus.PENDING
    for t in plan.tasks[MAX_TASKS:]:
        assert t.status == TaskStatus.SKIPPED
        assert t.blockers and "cap" in t.blockers[0]


# ---------- topological_order ----------


def _et(tid: str, deps=None, status=TaskStatus.PENDING) -> ExecutionTask:
    return ExecutionTask(id=tid, title=tid, depends_on=deps or [], status=status)


def test_topological_order_respects_dependencies():
    tasks = [_et("t2", deps=["t1"]), _et("t1"), _et("t3", deps=["t2"])]
    order = [t.id for t in topological_order(tasks)]
    assert order.index("t1") < order.index("t2") < order.index("t3")


def test_topological_order_stable_for_independent():
    tasks = [_et("t1"), _et("t2"), _et("t3")]
    assert [t.id for t in topological_order(tasks)] == ["t1", "t2", "t3"]


def test_topological_order_cycle_safe():
    tasks = [_et("t1", deps=["t2"]), _et("t2", deps=["t1"])]
    order = topological_order(tasks)
    # No hang, and every task is still returned exactly once.
    assert sorted(t.id for t in order) == ["t1", "t2"]


# ---------- dependency_failed ----------


def test_dependency_failed_detects_failed_dep():
    t1 = _et("t1", status=TaskStatus.FAILED)
    t2 = _et("t2", deps=["t1"])
    by_id = {"t1": t1, "t2": t2}
    reason = dependency_failed(t2, by_id)
    assert reason is not None and "t1" in reason


def test_dependency_failed_none_when_deps_ok():
    t1 = _et("t1", status=TaskStatus.COMPLETED)
    t2 = _et("t2", deps=["t1"])
    by_id = {"t1": t1, "t2": t2}
    assert dependency_failed(t2, by_id) is None


# ---------- task_status_from_final ----------


def test_task_status_from_final_mapping():
    assert task_status_from_final("completed") == TaskStatus.COMPLETED
    assert task_status_from_final("partial") == TaskStatus.FAILED
    assert task_status_from_final("blocked") == TaskStatus.FAILED
    assert task_status_from_final("failed") == TaskStatus.FAILED


# ---------- aggregate_run_status ----------


def test_aggregate_all_completed():
    tasks = [_et("t1", status=TaskStatus.COMPLETED), _et("t2", status=TaskStatus.COMPLETED)]
    status, summary, blockers = aggregate_run_status(tasks)
    assert status == "completed"
    assert blockers == []


def test_aggregate_single_completed():
    status, _, _ = aggregate_run_status([_et("t1", status=TaskStatus.COMPLETED)])
    assert status == "completed"


def test_aggregate_mixed_is_partial():
    tasks = [_et("t1", status=TaskStatus.COMPLETED), _et("t2", status=TaskStatus.FAILED)]
    status, _, blockers = aggregate_run_status(tasks)
    assert status == "partial"


def test_aggregate_none_completed_is_failed():
    tasks = [_et("t1", status=TaskStatus.FAILED), _et("t2", status=TaskStatus.SKIPPED)]
    status, _, _ = aggregate_run_status(tasks)
    assert status == "failed"


def _run_all() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed: list[str] = []
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed.append(fn.__name__)
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append(fn.__name__)
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print()
    if failed:
        print(f"{len(failed)} test(s) failed: {', '.join(failed)}")
        return 1
    print(f"All {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
