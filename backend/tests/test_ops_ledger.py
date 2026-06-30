"""Tests for Phase 8 — OPS ledger writer (execution/ops_ledger.py) + OPS.md policy.

Coverage:
  - append_ops_entry writes projects/{id}/OPS.md under ## Ledger + a project
    ops-events audit line.
  - no secret VALUE ever lands in OPS.md (redacted at the call site).
  - idempotent on a repeated dedup_key.
  - OPS.md memory policy: scaffolded, but absent from every judge writable set
    and from DEFAULT_SECTION (so no LLM can ever write/fabricate it).

Run directly:
    python backend/tests/test_ops_ledger.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import credentials  # noqa: E402
import memory_engine  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
from execution import ops_ledger  # noqa: E402


class _Sandbox:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects_dir = root / "projects"
        self.execution_dir = root / "execution_workspaces"
        self.projects_dir.mkdir()
        self.execution_dir.mkdir()
        (self.projects_dir / "proj").mkdir()
        self._prev_ledger_projects = ops_ledger._PROJECTS_DIR
        self._prev_exec = exec_manager._EXECUTION_ROOT
        self._prev_cred = (credentials._CRED_DIR, credentials._PROJECTS_DIR, credentials._GLOBAL_FILE)
        ops_ledger._PROJECTS_DIR = self.projects_dir
        exec_manager._EXECUTION_ROOT = self.execution_dir
        credentials._CRED_DIR = root / "credentials"
        credentials._PROJECTS_DIR = credentials._CRED_DIR / "projects"
        credentials._GLOBAL_FILE = credentials._CRED_DIR / "global.json"

    def ops_md(self) -> str:
        p = self.projects_dir / "proj" / "OPS.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def cleanup(self) -> None:
        ops_ledger._PROJECTS_DIR = self._prev_ledger_projects
        exec_manager._EXECUTION_ROOT = self._prev_exec
        (credentials._CRED_DIR, credentials._PROJECTS_DIR, credentials._GLOBAL_FILE) = self._prev_cred
        self.tmp.cleanup()


def _run(test_body):
    sb = _Sandbox()
    try:
        test_body(sb)
    finally:
        sb.cleanup()


def test_append_writes_ledger_and_event():
    def body(sb):
        ok = ops_ledger.append_ops_entry(
            "proj", "deploy", "Vercel deploy (preview)",
            {"target": "vercel:preview", "deployment_id": "dpl_abc", "preview_url": "https://x.vercel.app"},
            timestamp="2026-06-30T14:02Z", dedup_key="dpl_abc",
        )
        assert ok is True
        md = sb.ops_md()
        assert "## Ledger" in md
        assert "deploy — Vercel deploy (preview)" in md
        assert "dpl_abc" in md and "https://x.vercel.app" in md
        events = ops_ledger.read_ops_events("proj")
        assert events and events[0]["deployment_id"] == "dpl_abc"

    _run(body)


def test_no_secret_value_lands_in_ledger():
    def body(sb):
        # a secret stored for the project; if it somehow appears in a field it
        # must be redacted at the call site.
        credentials.set_credential("stripe", "proj", fields={"webhook_secret": "whsec_supersecret123456"})
        ops_ledger.append_ops_entry(
            "proj", "webhook", "Stripe webhook registered",
            {"endpoint_id": "we_1", "leaked": "whsec_supersecret123456"},
            timestamp="2026-06-30T14:03Z",
        )
        md = sb.ops_md()
        assert "we_1" in md
        assert "whsec_supersecret123456" not in md  # redacted
        # the ops-events audit line is redacted too
        import json
        raw = (sb.execution_dir / "proj" / "ops" / "events.jsonl").read_text(encoding="utf-8")
        assert "whsec_supersecret123456" not in raw

    _run(body)


def test_idempotent_on_dedup_key():
    def body(sb):
        first = ops_ledger.append_ops_entry(
            "proj", "deploy", "Deploy", {"deployment_id": "dpl_same"},
            timestamp="2026-06-30T14:04Z", dedup_key="dpl_same",
        )
        second = ops_ledger.append_ops_entry(
            "proj", "deploy", "Deploy again", {"deployment_id": "dpl_same"},
            timestamp="2026-06-30T14:05Z", dedup_key="dpl_same",
        )
        assert first is True and second is False
        assert sb.ops_md().count("dpl_same") == 1

    _run(body)


def test_ops_md_memory_policy():
    # The ledger file must be unwritable by any LLM judge path.
    assert "OPS.md" not in memory_engine.WRITABLE_PROJECT
    assert "OPS.md" not in memory_engine.WRITABLE_GLOBAL
    assert "OPS.md" not in memory_engine.RECONCILIATION_WRITABLE
    assert "OPS.md" not in memory_engine.DEFAULT_SECTION
    # ...but it IS scaffolded + in its own deterministic writable set.
    assert "OPS.md" in memory_engine.CANONICAL_SECTIONS
    assert memory_engine.OPS_WRITABLE == frozenset({"OPS.md"})


def test_ops_md_rejects_judge_write():
    def body(sb):
        base = sb.projects_dir / "proj"
        memory_engine.ensure_memory_scaffold(base, "proj")
        # a reconciliation-style write (its allow-set) must NOT touch OPS.md
        wrote = memory_engine.apply_update(
            base_dir=base, allow=memory_engine.RECONCILIATION_WRITABLE,
            filename="OPS.md", section="Ledger", content="hacked", action="append",
        )
        assert wrote is False
        assert "hacked" not in sb.ops_md()

    _run(body)


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
