"""Tests for Task 9.3 — team-aware planning (planner role/parallel parsing,
wave computation, team-eligibility gate).

Run:  python backend/tests/test_team_planner.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from execution.models import ExecutionPlan, ExecutionTask, TaskSpec, TaskStatus  # noqa: E402
from execution.planner import (  # noqa: E402
    compute_waves,
    is_safe_task_id,
    plan_from_dict,
    plan_is_team_eligible,
    task_parallel_eligible,
)


def _task(tid: str, depends_on=None, *, role="coder", parallel=False, status=TaskStatus.PENDING):
    return ExecutionTask(
        id=tid,
        title=f"Task {tid}",
        description="d",
        depends_on=depends_on or [],
        role=role,
        parallel_safe=parallel,
        status=status,
    )


_SPEC = TaskSpec(title="t", task_card="card", created_by="test")


# ---------- role / parallel parsing (plan_from_dict) ----------


def test_plan_parses_role_and_parallel_fields():
    plan = plan_from_dict(
        {
            "goal": "g",
            "tasks": [
                {"id": "t1", "title": "A", "role": "coder", "parallel": True},
                {"id": "t2", "title": "B", "role": "reviewer"},
                {"id": "t3", "title": "C", "parallel": "true"},
            ],
        },
        _SPEC,
    )
    t1, t2, t3 = plan.tasks
    assert t1.role == "coder" and t1.parallel_safe is True
    assert t2.role == "reviewer" and t2.parallel_safe is False
    assert t3.role == "coder" and t3.parallel_safe is True  # string coercion


def test_plan_defaults_and_unknown_role_fall_back_to_coder():
    plan = plan_from_dict(
        {
            "tasks": [
                {"id": "t1", "title": "A"},
                {"id": "t2", "title": "B", "role": "growth_wizard", "parallel": "nope"},
            ]
        },
        _SPEC,
    )
    assert plan.tasks[0].role == "coder" and plan.tasks[0].parallel_safe is False
    assert plan.tasks[1].role == "coder" and plan.tasks[1].parallel_safe is False


def test_plan_execution_mode_defaults_sequential():
    plan = plan_from_dict({"tasks": [{"id": "t1", "title": "A"}]}, _SPEC)
    assert plan.execution_mode == "sequential"


# ---------- task-id sanitization (sandbox-escape guard) ----------


def test_is_safe_task_id_rejects_path_tricks():
    assert is_safe_task_id("t1")
    assert is_safe_task_id("setup-config.v2")
    assert not is_safe_task_id("../evil")
    assert not is_safe_task_id("a/b")
    assert not is_safe_task_id("a\\b")
    assert not is_safe_task_id("..")
    assert not is_safe_task_id(".")
    assert not is_safe_task_id("")
    assert not is_safe_task_id("x" * 65)  # over the length cap


def test_plan_replaces_unsafe_task_ids_with_positional_ids():
    plan = plan_from_dict(
        {
            "tasks": [
                {"id": "../../../../tmp/pwn", "title": "A", "parallel": True},
                {"id": "ok_task", "title": "B"},
                {"id": "a/b/c", "title": "C"},
            ]
        },
        _SPEC,
    )
    ids = [t.id for t in plan.tasks]
    # The unsafe ids are replaced by safe positional ones; the safe id survives.
    assert all("/" not in i and ".." not in i for i in ids)
    assert ids[0] == "t1" and ids[1] == "ok_task" and ids[2] == "t3"


# ---------- compute_waves ----------


def test_waves_layer_by_dependencies():
    tasks = [
        _task("t1"),
        _task("t2"),
        _task("t3", ["t1", "t2"]),
        _task("t4", ["t3"]),
    ]
    waves, cyclic = compute_waves(tasks)
    assert [[t.id for t in w] for w in waves] == [["t1", "t2"], ["t3"], ["t4"]]
    assert cyclic == set()


def test_waves_preserve_plan_order_within_a_wave():
    tasks = [_task("b"), _task("a"), _task("c", ["b"])]
    waves, _ = compute_waves(tasks)
    assert [t.id for t in waves[0]] == ["b", "a"]  # original order, not alphabetical


def test_waves_cycle_remainder_is_final_wave_and_flagged():
    tasks = [_task("t1"), _task("t2", ["t3"]), _task("t3", ["t2"])]
    waves, cyclic = compute_waves(tasks)
    assert [t.id for t in waves[0]] == ["t1"]
    assert {t.id for t in waves[-1]} == {"t2", "t3"}
    assert cyclic == {"t2", "t3"}


def test_waves_empty_input():
    waves, cyclic = compute_waves([])
    assert waves == [] and cyclic == set()


# ---------- parallel eligibility ----------


def test_read_only_roles_are_always_parallel_eligible():
    assert task_parallel_eligible(_task("t1", role="reviewer", parallel=False))
    assert task_parallel_eligible(_task("t2", role="inspector", parallel=False))


def test_coder_requires_explicit_parallel_flag():
    assert not task_parallel_eligible(_task("t1", role="coder", parallel=False))
    assert task_parallel_eligible(_task("t2", role="coder", parallel=True))


# ---------- team-eligibility gate ----------


def _planned(tasks) -> ExecutionPlan:
    return ExecutionPlan(goal="g", tasks=tasks, mode="planned")


def test_team_eligible_when_a_wave_has_two_parallel_tasks():
    plan = _planned([_task("t1", parallel=True), _task("t2", parallel=True)])
    assert plan_is_team_eligible(plan)


def test_not_eligible_for_simple_or_fallback_plans():
    for mode in ("simple", "fallback"):
        plan = ExecutionPlan(
            goal="g",
            tasks=[_task("t1", parallel=True), _task("t2", parallel=True)],
            mode=mode,
        )
        assert not plan_is_team_eligible(plan), mode


def test_not_eligible_when_all_sequential():
    plan = _planned([_task("t1"), _task("t2", ["t1"]), _task("t3", ["t2"])])
    assert not plan_is_team_eligible(plan)


def test_not_eligible_with_one_parallel_task_per_wave():
    # t1 parallel-safe but alone in wave 1; t2 depends on it.
    plan = _planned([_task("t1", parallel=True), _task("t2", ["t1"], parallel=True)])
    assert not plan_is_team_eligible(plan)


def test_read_only_pair_makes_a_plan_eligible():
    plan = _planned([_task("t1", role="reviewer"), _task("t2", role="inspector")])
    assert plan_is_team_eligible(plan)


def test_skipped_tasks_do_not_count_toward_eligibility():
    plan = _planned(
        [
            _task("t1", parallel=True, status=TaskStatus.SKIPPED),
            _task("t2", parallel=True),
        ]
    )
    assert not plan_is_team_eligible(plan)


def test_cyclic_tasks_are_not_eligible_even_if_marked_parallel():
    plan = _planned(
        [
            _task("t1", ["t2"], parallel=True),
            _task("t2", ["t1"], parallel=True),
        ]
    )
    assert not plan_is_team_eligible(plan)


# ---------- standalone runner ----------


def _run_all() -> int:
    failures = 0
    names = [n for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name in names:
        fn = globals()[name]
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"\n{failures} of {len(names)} tests failed.")
        return 1
    print(f"\nAll {len(names)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
