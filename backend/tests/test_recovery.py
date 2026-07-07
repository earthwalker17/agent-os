"""Tests for Phase 6 confirmable recovery assessment (execution/recovery.py).

Standalone:  python tests/test_recovery.py

Patches the execution root to a temp dir at runtime (``get_execution_root`` reads
the module global dynamically), writes a crafted run.json, then drives
``assess_run`` with an injected ``llm_caller`` (no API key / network).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager
from execution import run_store
from execution import recovery
from execution.models import (
    RunRecord, RunStatus, VerificationResult, VisualReviewResult,
)


class _Root:
    """Temp execution root, patched in for the duration of a test."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._prev = exec_manager._EXECUTION_ROOT
        exec_manager._EXECUTION_ROOT = Path(self.tmp.name)

    def cleanup(self):
        exec_manager._EXECUTION_ROOT = self._prev
        self.tmp.cleanup()

    def seed_run(self, record: RunRecord, result_md: str = "") -> None:
        run_store.init_run_dir(record.project_id, record.run_id)
        run_store.write_run_json(record.project_id, record.run_id, record)
        if result_md:
            (run_store.get_run_dir(record.project_id, record.run_id) / "result.md").write_text(
                result_md, encoding="utf-8"
            )


def _record(**over) -> RunRecord:
    base = dict(
        run_id="20260101-000000-deadbeef",
        project_id="demo",
        task_title="Add feature X",
        status=RunStatus.PARTIAL,
        summary="Wrote files but the build failed.",
        files_changed=["src/x.ts"],
        blockers=["verification failed: npm run build"],
    )
    base.update(over)
    return RunRecord(**base)


def _caller(payload, counter=None):
    def caller(system, messages, max_tokens=None, **kwargs):
        if counter is not None:
            counter.append(1)
        if isinstance(payload, BaseException):
            raise payload
        return payload
    return caller


def _needs_recovery_payload(card="Fix the type error in src/x.ts so npm run build passes."):
    return json.dumps({
        "verdict": "needs_recovery",
        "diagnosis": "A TypeScript error breaks the build.",
        "recommended_action": "repair",
        "follow_up_task_card": card,
        "rationale": "A one-file fix should unblock the build.",
    })


def _read_assessment(project_id, run_id):
    raw = run_store.read_run_json(project_id, run_id)
    return RunRecord(**raw).recovery_assessment


# ---------- core ----------

def test_partial_run_persists_needs_recovery():
    root = _Root()
    try:
        rec = _record()
        root.seed_run(rec, result_md="## Summary\nBuild failed.\n")
        out = recovery.assess_run("demo", rec.run_id, llm_caller=_caller(_needs_recovery_payload()))
        assert out is not None and out.assessed is True
        assert out.verdict == "needs_recovery"
        assert out.recommended_action == "repair"
        assert "src/x.ts" in out.follow_up_task_card
        # Persisted to run.json.
        persisted = _read_assessment("demo", rec.run_id)
        assert persisted is not None and persisted.verdict == "needs_recovery"
    finally:
        root.cleanup()


def test_completed_green_run_is_skipped():
    root = _Root()
    try:
        rec = _record(
            status=RunStatus.COMPLETED, blockers=[],
            verification=VerificationResult(enabled=True, status="passed"),
        )
        root.seed_run(rec)
        counter: list[int] = []
        out = recovery.assess_run("demo", rec.run_id, llm_caller=_caller(_needs_recovery_payload(), counter))
        assert out is None
        assert counter == []  # no LLM call
        assert _read_assessment("demo", rec.run_id) is None
    finally:
        root.cleanup()


def test_cancelled_run_is_skipped():
    root = _Root()
    try:
        rec = _record(status=RunStatus.CANCELLED)
        root.seed_run(rec)
        out = recovery.assess_run("demo", rec.run_id, llm_caller=_caller(_needs_recovery_payload()))
        assert out is None
    finally:
        root.cleanup()


def test_completed_but_visual_failed_is_assessed():
    root = _Root()
    try:
        rec = _record(
            status=RunStatus.COMPLETED, blockers=[],
            verification=VerificationResult(enabled=True, status="passed"),
            visual_review=VisualReviewResult(enabled=True, status="failed", headline="Blank page"),
        )
        root.seed_run(rec)
        counter: list[int] = []
        out = recovery.assess_run("demo", rec.run_id, llm_caller=_caller(_needs_recovery_payload(), counter))
        assert out is not None and out.assessed is True
        assert counter == [1]  # the hidden visual signal triggered a real assessment
    finally:
        root.cleanup()


def test_idempotent_second_call_does_not_rejudge():
    root = _Root()
    try:
        rec = _record()
        root.seed_run(rec)
        counter: list[int] = []
        first = recovery.assess_run("demo", rec.run_id, llm_caller=_caller(_needs_recovery_payload(), counter))
        assert first is not None and counter == [1]
        second = recovery.assess_run("demo", rec.run_id, llm_caller=_caller(_needs_recovery_payload(), counter))
        assert second is not None and second.verdict == "needs_recovery"
        assert counter == [1]  # not called again
    finally:
        root.cleanup()


def test_run_action_without_task_card_downgrades_to_report():
    root = _Root()
    try:
        rec = _record()
        root.seed_run(rec)
        payload = json.dumps({
            "verdict": "needs_recovery",
            "diagnosis": "Unclear.",
            "recommended_action": "repair",
            "follow_up_task_card": "",
            "rationale": "x",
        })
        out = recovery.assess_run("demo", rec.run_id, llm_caller=_caller(payload))
        assert out.recommended_action == "report"
        assert out.follow_up_task_card == ""
        assert out.verdict == "exhausted"
    finally:
        root.cleanup()


def test_llm_exception_records_error_but_never_raises():
    root = _Root()
    try:
        rec = _record()
        root.seed_run(rec)
        out = recovery.assess_run("demo", rec.run_id, llm_caller=_caller(RuntimeError("boom")))
        assert out is not None and out.assessed is False
        assert out.error and "boom" in out.error
    finally:
        root.cleanup()


def test_malformed_json_is_recorded_as_error():
    root = _Root()
    try:
        rec = _record()
        root.seed_run(rec)
        out = recovery.assess_run("demo", rec.run_id, llm_caller=_caller("not json"))
        assert out is not None and out.assessed is False
        assert out.error
    finally:
        root.cleanup()


# ---------- Phase 11: Recovery Matrix classification + evidence ----------

def _payload_with_type(recovery_type, action="repair", card="Fix src/x.ts."):
    return json.dumps({
        "verdict": "needs_recovery",
        "diagnosis": "d",
        "recommended_action": action,
        "recovery_type": recovery_type,
        "follow_up_task_card": card if action in ("repair", "split", "reverify") else "",
        "rationale": "r",
    })


def test_valid_judge_recovery_type_is_kept():
    root = _Root()
    try:
        rec = _record()
        root.seed_run(rec)
        out = recovery.assess_run(
            "demo", rec.run_id, llm_caller=_caller(_payload_with_type("visual"))
        )
        assert out.recovery_type == "visual"
        assert out.classified_by == "judge"
    finally:
        root.cleanup()


def test_invalid_judge_recovery_type_falls_back_to_rules():
    root = _Root()
    try:
        rec = _record(
            verification=VerificationResult(enabled=True, status="failed"),
        )
        root.seed_run(rec)
        out = recovery.assess_run(
            "demo", rec.run_id, llm_caller=_caller(_payload_with_type("nonsense"))
        )
        assert out.recovery_type == "build"  # deterministic classification
        assert out.classified_by == "rules"
    finally:
        root.cleanup()


def test_legacy_payload_without_type_falls_back_to_rules():
    root = _Root()
    try:
        rec = _record()
        root.seed_run(rec)
        out = recovery.assess_run(
            "demo", rec.run_id, llm_caller=_caller(_needs_recovery_payload())
        )
        assert out.recovery_type == "product"  # non-green fallback
        assert out.classified_by == "rules"
    finally:
        root.cleanup()


def test_evidence_appended_to_run_action_card():
    root = _Root()
    try:
        rec = _record(
            verification=VerificationResult(
                enabled=True, status="failed", command="npm run build",
                output_preview="error TS2304: Cannot find name 'foo'",
            ),
        )
        root.seed_run(rec)
        out = recovery.assess_run(
            "demo", rec.run_id, llm_caller=_caller(_needs_recovery_payload())
        )
        assert "## Evidence from failed run" in out.follow_up_task_card
        assert "TS2304" in out.follow_up_task_card
        # Persisted card carries the evidence too (both dispatch paths read it).
        persisted = _read_assessment("demo", rec.run_id)
        assert "## Evidence from failed run" in persisted.follow_up_task_card
    finally:
        root.cleanup()


def test_no_evidence_for_report_action():
    root = _Root()
    try:
        rec = _record()
        root.seed_run(rec)
        out = recovery.assess_run(
            "demo", rec.run_id,
            llm_caller=_caller(_payload_with_type("product", action="report")),
        )
        assert out.follow_up_task_card == ""
    finally:
        root.cleanup()


def test_llm_failure_still_carries_deterministic_type():
    root = _Root()
    try:
        rec = _record(
            verification=VerificationResult(enabled=True, status="failed"),
        )
        root.seed_run(rec)
        out = recovery.assess_run("demo", rec.run_id, llm_caller=_caller(RuntimeError("boom")))
        assert out.assessed is False
        assert out.recovery_type == "build"
        assert out.classified_by == "rules"
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
