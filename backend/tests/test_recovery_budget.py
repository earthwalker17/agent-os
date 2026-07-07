"""Tests for the Phase 6.1 user-approved recovery budget (background.py).

Standalone:  python tests/test_recovery_budget.py

Exercises BackgroundRunManager._maybe_auto_recover's gating in isolation by
stubbing the instance's ``dispatch`` so no real Coding Agent run executes. The
gating is the safety-critical surface (it is the only path that auto-dispatches a
run without a per-run click).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager
from execution import run_store
from execution.background import BackgroundRunManager, RECOVERY_HARD_CAP
from execution.models import (
    BrowserVerificationResult,
    RecoveryAssessment,
    RunRecord,
    RunStatus,
    VerificationCommandResult,
    VerificationResult,
    VisualReviewResult,
)


class _Root:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev = exec_manager._EXECUTION_ROOT
        exec_manager._EXECUTION_ROOT = Path(self.tmp.name)

    def cleanup(self):
        exec_manager._EXECUTION_ROOT = self._prev
        self.tmp.cleanup()

    def seed(self, record: RunRecord):
        run_store.init_run_dir(record.project_id, record.run_id)
        run_store.write_run_json(record.project_id, record.run_id, record)


def _assessment(needs=True, card="Fix it with stdlib only.") -> RecoveryAssessment:
    return RecoveryAssessment(
        assessed=True,
        verdict="needs_recovery" if needs else "exhausted",
        diagnosis="x",
        recommended_action="repair" if needs else "report",
        follow_up_task_card=card if needs else "",
        rationale="y",
    )


def _record(**over) -> RunRecord:
    base = dict(
        run_id="20260101-000000-parent00",
        project_id="demo",
        task_title="Build X",
        status=RunStatus.PARTIAL,
        recovery_budget=1,
        orchestration_round=0,
        recovery_assessment=_assessment(),
    )
    base.update(over)
    return RunRecord(**base)


def _mgr_with_spy():
    """A manager whose dispatch is replaced by a non-executing spy."""
    mgr = BackgroundRunManager(max_workers=1)
    calls: list[dict] = []

    def fake_dispatch(project_id, task, **kw):
        calls.append({"project_id": project_id, "task": task, **kw})
        return RunRecord(
            run_id="child-0001", project_id=project_id,
            task_title=task.title, status=RunStatus.RUNNING,
        )

    mgr.dispatch = fake_dispatch  # instance attr shadows the bound method
    mgr.shutdown(wait=False)  # we never use the real executor in these tests
    return mgr, calls


def _read(project_id, run_id):
    return RunRecord(**run_store.read_run_json(project_id, run_id))


# ---------- fires once ----------

def test_auto_recover_fires_on_nongreen_with_budget():
    root = _Root()
    try:
        rec = _record(recovery_budget=2, orchestration_round=0)
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert len(calls) == 1
        kw = calls[0]
        assert kw["recovery_of"] == rec.run_id
        assert kw["recovery_budget"] == 1           # decremented
        assert kw["orchestration_round"] == 1       # +1
        assert kw["task"].created_by == "auto_recovery"
        # Parent claimed.
        assert _read("demo", rec.run_id).recovered_by == "child-0001"
    finally:
        root.cleanup()


# ---------- gating: does NOT fire ----------

def test_no_budget_does_not_recover():
    root = _Root()
    try:
        rec = _record(recovery_budget=0)
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert calls == []
        assert _read("demo", rec.run_id).recovered_by is None
    finally:
        root.cleanup()


def test_green_run_does_not_recover():
    root = _Root()
    try:
        rec = _record(status=RunStatus.COMPLETED, recovery_budget=2)
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert calls == []
    finally:
        root.cleanup()


def test_already_recovered_is_idempotent():
    root = _Root()
    try:
        rec = _record(recovery_budget=2, recovered_by="prev-child")
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert calls == []  # one recovery per parent
    finally:
        root.cleanup()


def test_hard_cap_stops_chain():
    root = _Root()
    try:
        rec = _record(recovery_budget=5, orchestration_round=RECOVERY_HARD_CAP)
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert calls == []  # depth cap beats a large budget
    finally:
        root.cleanup()


def test_non_needs_recovery_verdict_does_not_recover():
    root = _Root()
    try:
        rec = _record(recovery_budget=2, recovery_assessment=_assessment(needs=False))
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert calls == []
    finally:
        root.cleanup()


def test_no_assessment_does_not_recover():
    root = _Root()
    try:
        rec = _record(recovery_budget=2, recovery_assessment=None)
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert calls == []
    finally:
        root.cleanup()


def test_needs_recovery_without_card_does_not_recover():
    root = _Root()
    try:
        ra = RecoveryAssessment(
            assessed=True, verdict="needs_recovery",
            recommended_action="report", follow_up_task_card="",
        )
        rec = _record(recovery_budget=2, recovery_assessment=ra)
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert calls == []
    finally:
        root.cleanup()


def test_child_inherits_decremented_budget_to_zero():
    root = _Root()
    try:
        rec = _record(recovery_budget=1, orchestration_round=0)
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert len(calls) == 1
        assert calls[0]["recovery_budget"] == 0  # child can't recover again
    finally:
        root.cleanup()


# ---------- Phase 11: Recovery Matrix gating ----------

def test_visual_failed_completed_run_auto_recovers_under_budget():
    """A COMPLETED run whose visual verdict failed is recover-eligible (the
    verdict never downgrades status, so the old status-only gate could never
    spend a granted budget on a visually-broken-but-green build)."""
    root = _Root()
    try:
        rec = _record(
            status=RunStatus.COMPLETED,
            recovery_budget=2,
            visual_review=VisualReviewResult(
                enabled=True, status="failed", headline="Blank page"
            ),
        )
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert len(calls) == 1
        # Visual repair is a single bounded pass: contract clamps the child's
        # onward budget to 0 regardless of the remaining user budget.
        assert calls[0]["recovery_budget"] == 0
        assert calls[0]["recovery_of"] == rec.run_id
        assert _read("demo", rec.run_id).recovered_by == "child-0001"
    finally:
        root.cleanup()


def test_visual_warning_completed_run_does_not_recover():
    root = _Root()
    try:
        rec = _record(
            status=RunStatus.COMPLETED,
            recovery_budget=2,
            visual_review=VisualReviewResult(enabled=True, status="warning"),
        )
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert calls == []
    finally:
        root.cleanup()


def test_environment_failure_never_auto_recovers():
    """Missing Playwright/Chromium, occupied port etc. are operator problems —
    a Coding Agent can't fix them, so a granted budget must not be burned."""
    root = _Root()
    try:
        rec = _record(
            recovery_budget=2,
            browser_verification=BrowserVerificationResult(
                enabled=True, status="failed",
                output_preview="chromium not installed — run: python -m playwright install chromium",
            ),
        )
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert calls == []
        assert _read("demo", rec.run_id).recovered_by is None
    finally:
        root.cleanup()


def test_confirm_only_type_never_auto_recovers():
    """Evidence-driven confirm-only types (deployment/database/…) stay manual
    even with budget + a needs_recovery card."""
    root = _Root()
    try:
        rec = _record(
            recovery_budget=2,
            blockers=["supabase db push failed: column mismatch"],
        )
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert calls == []
    finally:
        root.cleanup()


def test_build_type_child_budget_clamped_by_contract():
    root = _Root()
    try:
        rec = _record(
            recovery_budget=2,
            verification=VerificationResult(
                enabled=True, status="failed",
                commands=[VerificationCommandResult(
                    command="npm run build", kind="build", status="failed"
                )],
            ),
        )
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert len(calls) == 1
        # build cap is 1: min(2 - 1, 1) == 1.
        assert calls[0]["recovery_budget"] == 1
    finally:
        root.cleanup()


def test_judge_recovery_type_overrides_rules_for_contract():
    """The assessment's validated recovery_type wins over re-classification
    when resolving the contract."""
    root = _Root()
    try:
        assessment = _assessment()
        assessment.recovery_type = "deployment"  # confirm-only by contract
        rec = _record(recovery_budget=2, recovery_assessment=assessment)
        root.seed(rec)
        mgr, calls = _mgr_with_spy()
        mgr._maybe_auto_recover("demo", rec.run_id)
        assert calls == []
    finally:
        root.cleanup()


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
