"""Tests for the Phase 6 structured memory intake judge (orchestrator.py).

Standalone:  python tests/test_memory_intake.py

The judge takes an injected ``llm_caller`` so no API key / network is needed.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import orchestrator
from orchestrator import (
    judge_memory_intake,
    apply_memory_decision,
    judge_memory_updates,
)
from orchestrator import MemoryContext


def _ctx() -> MemoryContext:
    return MemoryContext(
        user="", workstyle="", soul="", global_memory="",
        project="# Demo", status="# Status: Demo\n\n## What Works\n- nothing yet\n",
        task_queue="", decisions="", research="",
        project_name="Demo", project_id="demo", history=[],
    )


def _caller(payload):
    """Stub llm_chat returning ``payload`` (str) or raising it (Exception)."""
    def caller(system, messages, max_tokens=None, **kwargs):
        if isinstance(payload, BaseException):
            raise payload
        return payload
    return caller


# ---------- parsing ----------

def test_structured_object_is_parsed_with_reason():
    payload = json.dumps({
        "should_update": True,
        "reason": "Recorded the chosen datastore.",
        "updates": [
            {"filename": "DECISIONS.md", "section": "Decisions",
             "content": "- Chose SQLite", "action": "append"}
        ],
    })
    decision = judge_memory_intake("project", _ctx(), "use sqlite", "ok", llm_caller=_caller(payload))
    assert decision.should_update is True
    assert decision.reason == "Recorded the chosen datastore."
    assert len(decision.updates) == 1
    assert decision.updates[0].filename == "DECISIONS.md"


def test_legacy_bare_array_is_tolerated():
    payload = json.dumps([
        {"filename": "STATUS.md", "section": "What Works", "content": "- builds", "action": "append"}
    ])
    decision = judge_memory_intake("project", _ctx(), "msg", "resp", llm_caller=_caller(payload))
    assert decision.should_update is True
    assert decision.updates[0].filename == "STATUS.md"


def test_empty_decision_when_nothing_to_update():
    payload = json.dumps({"should_update": False, "reason": "Just chatter.", "updates": []})
    decision = judge_memory_intake("project", _ctx(), "hi", "hello", llm_caller=_caller(payload))
    assert decision.should_update is False
    assert decision.updates == []


def test_non_writable_file_is_filtered_out():
    payload = json.dumps({
        "should_update": True, "reason": "x",
        "updates": [
            {"filename": "SOUL.md", "section": "Identity", "content": "pwned", "action": "replace"},
            {"filename": "STATUS.md", "section": "What Works", "content": "- ok", "action": "append"},
        ],
    })
    decision = judge_memory_intake("project", _ctx(), "m", "r", llm_caller=_caller(payload))
    files = [u.filename for u in decision.updates]
    assert "SOUL.md" not in files
    assert "STATUS.md" in files


def test_global_scope_rejects_project_files():
    payload = json.dumps({
        "should_update": True, "reason": "x",
        "updates": [
            {"filename": "STATUS.md", "section": "What Works", "content": "- ok", "action": "append"},
            {"filename": "USER.md", "section": "Role", "content": "Builder", "action": "append"},
        ],
    })
    decision = judge_memory_intake("global", _ctx(), "m", "r", llm_caller=_caller(payload))
    files = [u.filename for u in decision.updates]
    assert files == ["USER.md"]


def test_malformed_json_is_a_noop():
    decision = judge_memory_intake("project", _ctx(), "m", "r", llm_caller=_caller("not json at all"))
    assert decision.should_update is False
    assert decision.updates == []


def test_llm_exception_is_a_noop():
    decision = judge_memory_intake(
        "project", _ctx(), "m", "r", llm_caller=_caller(RuntimeError("boom"))
    )
    assert decision.should_update is False


def test_code_fenced_json_is_tolerated():
    payload = "```json\n" + json.dumps({
        "should_update": True, "reason": "r",
        "updates": [{"filename": "RESEARCH.md", "section": "Findings", "content": "- a", "action": "append"}],
    }) + "\n```"
    decision = judge_memory_intake("project", _ctx(), "m", "r", llm_caller=_caller(payload))
    assert decision.updates and decision.updates[0].filename == "RESEARCH.md"


# ---------- apply ----------

def test_apply_memory_decision_writes_and_reports():
    payload = json.dumps({
        "should_update": True, "reason": "r",
        "updates": [{"filename": "STATUS.md", "section": "What Works", "content": "- it builds", "action": "append"}],
    })
    decision = judge_memory_intake("project", _ctx(), "m", "r", llm_caller=_caller(payload))
    with tempfile.TemporaryDirectory() as d:
        projects = Path(d)
        (projects / "demo").mkdir()
        prev = orchestrator.PROJECTS_DIR
        orchestrator.PROJECTS_DIR = projects
        try:
            applied = apply_memory_decision(decision, "project", project_id="demo")
            assert len(applied) == 1
            assert applied[0]["applied"] is True
            assert "- it builds" in (projects / "demo" / "STATUS.md").read_text(encoding="utf-8")
        finally:
            orchestrator.PROJECTS_DIR = prev


# ---------- back-compat wrapper ----------

def test_judge_memory_updates_wrapper_returns_bare_dicts():
    payload = json.dumps({
        "should_update": True, "reason": "r",
        "updates": [{"filename": "STATUS.md", "section": "What Works", "content": "- x", "action": "append"}],
    })
    # The wrapper uses the live llm_chat; patch it on the module for this call.
    import llm
    prev = llm.chat
    orch_prev = orchestrator.llm_chat
    llm.chat = _caller(payload)
    orchestrator.llm_chat = _caller(payload)
    try:
        updates = judge_memory_updates(_ctx(), "m", "r")
        assert isinstance(updates, list)
        assert updates[0]["filename"] == "STATUS.md"
        assert set(updates[0].keys()) == {"filename", "section", "content", "action"}
    finally:
        llm.chat = prev
        orchestrator.llm_chat = orch_prev


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
