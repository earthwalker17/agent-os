"""Phase 8 — constitutional invariant guards (static).

These assert structural properties that must hold for the whole phase, so a
later edit can't silently break them:

  - The Main Agent brain (orchestrator.py) must NOT import any external
    connector / sandbox CLI executor. The connectors are reachable ONLY from the
    user-driven endpoint handlers + the runner — never from the chat loop — so
    inferred chat intent can never reach a deploy/migration/payment path (M6).
  - OPS.md is excluded from every LLM-judge writable set (so deployment facts
    are deterministic, never paraphrased/fabricated by a model).

Run directly:
    python backend/tests/test_phase8_invariants.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import memory_engine  # noqa: E402

_FORBIDDEN_IN_ORCHESTRATOR = (
    "vercel_connector",
    "supabase_connector",
    "stripe_connector",
    "ops_ledger",
    "run_supabase",
    "run_vercel",
    "run_stripe",
)


def test_orchestrator_imports_no_connector():
    src = (_BACKEND / "orchestrator.py").read_text(encoding="utf-8")
    # crude but effective: no import line may reference a connector/executor.
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            for name in _FORBIDDEN_IN_ORCHESTRATOR:
                assert name not in stripped, f"orchestrator must not import {name!r}: {stripped}"
    # also assert the module object never gained the attribute (defensive)
    import orchestrator  # noqa: E402

    for name in _FORBIDDEN_IN_ORCHESTRATOR:
        assert not hasattr(orchestrator, name), f"orchestrator unexpectedly exposes {name}"


def test_ops_md_excluded_from_judge_sets():
    assert "OPS.md" not in memory_engine.WRITABLE_PROJECT
    assert "OPS.md" not in memory_engine.WRITABLE_GLOBAL
    assert "OPS.md" not in memory_engine.RECONCILIATION_WRITABLE
    assert "OPS.md" not in memory_engine.DEFAULT_SECTION


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
