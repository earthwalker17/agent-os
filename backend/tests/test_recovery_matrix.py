"""Tests for the Phase 11 Recovery Matrix (execution/recovery_matrix.py).

Standalone:  python tests/test_recovery_matrix.py

Pure-function module — no temp roots, no LLM stubs needed. Covers the frozen
contract registry, the deterministic classification ladder (priority order +
the auto_ok environment guard), and the bounded/redacted evidence builder.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import credentials
from execution.recovery_matrix import (
    EVIDENCE_MAX_CHARS,
    RECOVERY_CONTRACTS,
    RECOVERY_TYPES,
    build_recovery_evidence,
    classify_failure,
    contract_for,
)
from execution.models import (
    BrowserFlowResult,
    BrowserFlowStep,
    BrowserPageCapture,
    BrowserVerificationResult,
    IntegrationConflict,
    IntegrationResult,
    RunRecord,
    RunStatus,
    VerificationCommandResult,
    VerificationResult,
    VisualReviewResult,
)


def _record(**over) -> RunRecord:
    base = dict(
        run_id="20260101-000000-cafebabe",
        project_id="demo",
        task_title="Build the dashboard",
        status=RunStatus.PARTIAL,
        summary="Wrote files.",
        files_changed=["src/App.tsx"],
        blockers=[],
    )
    base.update(over)
    return RunRecord(**base)


def _failed_browser(**over) -> BrowserVerificationResult:
    base = dict(enabled=True, status="failed", url="http://127.0.0.1:5174")
    base.update(over)
    return BrowserVerificationResult(**base)


# ---------- contract registry ----------

def test_contracts_cover_every_type_and_are_frozen():
    assert set(RECOVERY_CONTRACTS.keys()) == set(RECOVERY_TYPES)
    c = RECOVERY_CONTRACTS["visual"]
    try:
        c.max_attempts = 99  # type: ignore[misc]
        assert False, "contract should be frozen"
    except dataclasses.FrozenInstanceError:
        pass
    # Near-term automated paths only; external/semantic types stay confirm-only.
    assert RECOVERY_CONTRACTS["build"].auto_eligible is True
    assert RECOVERY_CONTRACTS["runtime"].auto_eligible is True
    assert RECOVERY_CONTRACTS["visual"].auto_eligible is True
    for t in ("integration", "deployment", "database", "docs_memory"):
        assert RECOVERY_CONTRACTS[t].auto_eligible is False, t
    # Visual/runtime repairs are single bounded passes.
    assert RECOVERY_CONTRACTS["visual"].child_budget_cap == 0
    assert RECOVERY_CONTRACTS["runtime"].child_budget_cap == 0
    # The generic fallback preserves the Phase 6.1 budget contract (child
    # budget was `min(budget,2) - 1` <= 1 before Phase 11).
    assert RECOVERY_CONTRACTS["product"].auto_eligible is True
    assert RECOVERY_CONTRACTS["product"].child_budget_cap == 1


def test_contract_for_unknown_type_falls_back_to_product():
    assert contract_for("nonsense").recovery_type == "product"
    assert contract_for("").recovery_type == "product"


# ---------- classification ladder ----------

def test_integration_conflicts_win_over_everything():
    rec = _record(
        integration=IntegrationResult(
            enabled=True,
            waves=1,
            conflicts=[IntegrationConflict(path="a.ts", applied_task="t1", rejected_task="t2")],
        ),
        verification=VerificationResult(enabled=True, status="failed"),
        browser_verification=_failed_browser(),
    )
    cls = classify_failure(rec)
    assert cls.recovery_type == "integration"
    assert cls.auto_ok is False


def test_deployment_signature():
    rec = _record(
        deployment_id="dpl_123",
        blockers=["deployment failed: Vercel build error"],
    )
    assert classify_failure(rec).recovery_type == "deployment"


def test_database_signature():
    rec = _record(blockers=["supabase db push failed: column mismatch"])
    assert classify_failure(rec).recovery_type == "database"


def test_bare_migration_mention_is_not_database():
    """A blocker merely mentioning a migration file must not veto the user's
    recovery budget by misclassifying `database` (confirm-only)."""
    cls = classify_failure(
        _record(blockers=["ran out of steps while writing the migration file"])
    )
    assert cls.recovery_type == "product"
    assert cls.auto_ok is True


def test_env_failure_is_runtime_but_never_auto():
    for preview in (
        "playwright not installed — run: python -m playwright install chromium",
        "chromium not installed",
        "port 5174 is already in use — stop the running preview",
        "could not spawn Python subprocess for Playwright capture",
    ):
        rec = _record(browser_verification=_failed_browser(output_preview=preview))
        cls = classify_failure(rec)
        assert cls.recovery_type == "runtime", preview
        assert cls.auto_ok is False, preview


def test_server_failure_is_runtime_and_auto_ok():
    rec = _record(
        browser_verification=_failed_browser(
            output_preview="dev server exited before url was reachable (exit 1)"
        )
    )
    cls = classify_failure(rec)
    assert cls.recovery_type == "runtime"
    assert cls.auto_ok is True


def test_install_failure_is_runtime():
    rec = _record(browser_verification=_failed_browser(install_status="failed"))
    assert classify_failure(rec).recovery_type == "runtime"


def test_failed_flow_is_visual():
    rec = _record(
        browser_verification=_failed_browser(
            flows=[
                BrowserFlowResult(
                    name="smoke",
                    status="failed",
                    steps=[BrowserFlowStep(action="click", target="Save", status="failed", error="timeout")],
                )
            ]
        )
    )
    cls = classify_failure(rec)
    assert cls.recovery_type == "visual"
    assert cls.auto_ok is True


def test_failed_visual_verdict_is_visual():
    rec = _record(
        status=RunStatus.COMPLETED,
        visual_review=VisualReviewResult(enabled=True, status="failed", headline="Blank page"),
    )
    assert classify_failure(rec).recovery_type == "visual"


def test_verification_failure_is_build():
    rec = _record(
        verification=VerificationResult(
            enabled=True,
            status="failed",
            commands=[VerificationCommandResult(command="npm run build", kind="build", status="failed")],
        )
    )
    cls = classify_failure(rec)
    assert cls.recovery_type == "build"
    assert cls.auto_ok is True


def test_unrendered_page_with_console_errors_is_runtime():
    rec = _record(
        browser_verification=BrowserVerificationResult(
            enabled=True, status="passed", readiness="unconfirmed",
            console_errors=["[Home] pageerror: boom"],
        )
    )
    assert classify_failure(rec).recovery_type == "runtime"


def test_incidental_console_noise_keeps_product_fallback():
    """A rendered app with stray console errors on an incomplete run must NOT
    reroute the generic fallback (rule 10 carries the Phase 6.1 contract)."""
    rec = _record(
        blockers=["ran out of steps"],
        browser_verification=BrowserVerificationResult(
            enabled=True, status="passed", readiness="confirmed",
            console_errors=["[Home] console.error: noisy lib"],
        ),
    )
    cls = classify_failure(rec)
    assert cls.recovery_type == "product"
    assert cls.auto_ok is True


def test_non_green_fallback_is_product_and_auto_ok():
    # The canonical Phase 6.1 case: incomplete work, no technical signature.
    cls = classify_failure(_record(blockers=["ran out of steps"]))
    assert cls.recovery_type == "product"
    assert cls.auto_ok is True


def test_green_fallback_is_product_but_not_auto():
    cls = classify_failure(_record(status=RunStatus.COMPLETED))
    assert cls.recovery_type == "product"
    assert cls.auto_ok is False


# ---------- evidence builder ----------

def _rich_record() -> RunRecord:
    return _record(
        browser_verification=_failed_browser(
            output_preview="declared flow 'smoke' failed at step [click Save]",
            pages=[
                BrowserPageCapture(path="screenshots/browser.png", label="Home"),
                BrowserPageCapture(path="screenshots/view-02.png", label="/settings"),
            ],
            console_errors=["[Home] console.error: boom"],
            network_failures=["[Home] HTTP 500 GET http://127.0.0.1:5174/api/x"],
            flows=[
                BrowserFlowResult(
                    name="smoke",
                    status="failed",
                    steps=[
                        BrowserFlowStep(action="click", target="Save", status="failed", error="timeout 4000ms"),
                    ],
                )
            ],
        ),
        visual_review=VisualReviewResult(
            enabled=True, status="failed", headline="Save flow broken",
            evidence=["button unresponsive"],
        ),
        verification=VerificationResult(
            enabled=True, status="failed", command="npm run build",
            output_preview="error TS2304: Cannot find name 'foo'",
        ),
    )


def test_evidence_is_bounded_and_names_artifacts():
    text = build_recovery_evidence(_rich_record(), project_id=None)
    assert len(text) <= EVIDENCE_MAX_CHARS
    assert "screenshots/browser.png" in text
    assert "screenshots/view-02.png" in text
    assert "smoke" in text and "click Save" in text
    assert "console.error: boom" in text
    assert "HTTP 500" in text
    assert "TS2304" in text
    assert "Save flow broken" in text
    # Guidance lines are always present.
    assert "Browser Verification" in text and "repo/" in text


def test_evidence_is_redacted():
    rec = _rich_record()
    rec.browser_verification.console_errors = [
        "[Home] console.error: leaked sk_test_51HAbCdEfGhIjKlMnOpQrStUv key"
    ]
    text = build_recovery_evidence(rec, project_id=None)
    assert "sk_test_51HAbCdEfGhIjKlMnOpQrStUv" not in text
    assert "[REDACTED]" in text


def test_evidence_redacts_before_per_line_truncation():
    """A secret straddling a per-line truncation cut must not leak a partial
    value — fields are redacted BEFORE they are truncated."""
    secret = "sk_test_51" + "A" * 60
    rec = _rich_record()
    # Place the secret so the 600-char verification tail cut would slice it.
    rec.verification.output_preview = "x" * 580 + " " + secret + " trailing"
    text = build_recovery_evidence(rec, project_id=None)
    assert secret not in text
    # No partial fragment of the secret either (the tell-tale prefix + run of A's).
    assert "sk_test_51AAAA" not in text


def test_evidence_truncates_past_cap():
    rec = _rich_record()
    rec.browser_verification.console_errors = [
        f"[Home] console.error: very long line {i} " + "x" * 190 for i in range(20)
    ]
    rec.verification.output_preview = "y" * 5000
    text = build_recovery_evidence(rec, project_id=None)
    assert len(text) <= EVIDENCE_MAX_CHARS


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
