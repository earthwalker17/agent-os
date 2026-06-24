"""Tests for Intent Router v2 (Phase 6).

Covers: the deterministic mode `@`-command parser, the `intent` field on the
delegation judge, and the orchestration `mode` system-prompt shaping.

Standalone:  python tests/test_intent_router.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from execution.chat_delegation import parse_mode_command, is_code_delegation
from execution.delegation_judge import (
    judge_delegation,
    DECISION_DISPATCH,
    DECISION_DISCUSSION,
)
import orchestrator
from orchestrator import MemoryContext, _build_system_prompt


# ---------- mode command parser ----------

def test_plan_command_parses_and_strips_prefix():
    mode, body = parse_mode_command("@plan break down the auth work")
    assert mode == "plan"
    assert body == "break down the auth work"


def test_each_mode_command_recognized():
    for cmd, mode in [
        ("@plan x", "plan"), ("@design x", "design"), ("@debug x", "debug"),
        ("@review x", "review"), ("@inspect x", "inspect"), ("@memory x", "memory"),
    ]:
        m, _ = parse_mode_command(cmd)
        assert m == mode, f"{cmd} -> {m}"


def test_reviewer_does_not_match_review():
    # The guard mirrors is_code_delegation: a trailing word char means no match.
    mode, body = parse_mode_command("@reviewer should look at this")
    assert mode is None
    assert body == "@reviewer should look at this"


def test_bare_command_with_no_body():
    mode, body = parse_mode_command("@debug")
    assert mode == "debug"
    assert body == ""


def test_non_command_message_passthrough():
    mode, body = parse_mode_command("how should we structure the API?")
    assert mode is None
    assert body == "how should we structure the API?"


def test_code_command_is_separate_from_mode_commands():
    # @code is handled by is_code_delegation, not parse_mode_command.
    assert is_code_delegation("@code do the thing") is True
    mode, _ = parse_mode_command("@code do the thing")
    assert mode is None


def test_punctuation_separator_is_allowed():
    mode, body = parse_mode_command("@plan: ship the MVP")
    assert mode == "plan"
    assert body == "ship the MVP"


# ---------- delegation judge intent field ----------

def _caller(payload):
    def caller(system, messages, max_tokens=None, **kwargs):
        return payload
    return caller


def test_judge_emits_intent_label():
    payload = json.dumps({
        "decision": "discussion", "confidence": 0.8,
        "reason": "strategic chat", "intent": "planning",
        "title": "", "display_plan": "", "proposed_task_card": "",
    })
    d = judge_delegation("p", "P", "how should we sequence this?", [], llm_caller=_caller(payload))
    assert d.decision == DECISION_DISCUSSION
    assert d.intent == "planning"


def test_dispatch_defaults_intent_to_build_when_missing():
    payload = json.dumps({
        "decision": "dispatch_suggested", "confidence": 0.9, "reason": "code",
        "title": "Add endpoint", "display_plan": "do it", "proposed_task_card": "Add /x",
    })
    d = judge_delegation("p", "P", "add an endpoint", [], llm_caller=_caller(payload))
    assert d.decision == DECISION_DISPATCH
    assert d.intent == "build"


def test_unknown_intent_label_falls_back_to_empty():
    payload = json.dumps({
        "decision": "discussion", "confidence": 0.5, "reason": "x",
        "intent": "banana", "title": "", "display_plan": "", "proposed_task_card": "",
    })
    d = judge_delegation("p", "P", "msg", [], llm_caller=_caller(payload))
    assert d.intent == ""


def test_intent_in_to_dict():
    payload = json.dumps({
        "decision": "memory_only", "confidence": 0.7, "reason": "x",
        "intent": "memory", "title": "", "display_plan": "", "proposed_task_card": "",
    })
    d = judge_delegation("p", "P", "note this", [], llm_caller=_caller(payload))
    assert d.to_dict()["intent"] == "memory"


# ---------- mode shapes the system prompt ----------

def _ctx() -> MemoryContext:
    return MemoryContext(
        user="", workstyle="", soul="# Soul", global_memory="",
        project="# Demo", status="", task_queue="", decisions="", research="",
        project_name="Demo", project_id="__GENERAL__", history=[],
    )


def test_mode_block_present_for_plan():
    prompt = _build_system_prompt(_ctx(), mode="plan")
    assert "# Mode" in prompt
    assert "PLANNING" in prompt


def test_no_mode_block_when_mode_none():
    prompt = _build_system_prompt(_ctx(), mode=None)
    assert "# Mode" not in prompt


def test_unknown_mode_adds_no_block():
    prompt = _build_system_prompt(_ctx(), mode="banana")
    assert "# Mode" not in prompt


def test_docs_and_research_modes_have_guidance():
    for mode in ("docs", "research", "review"):
        prompt = _build_system_prompt(_ctx(), mode=mode)
        assert "# Mode" in prompt, mode


# ---------- intent -> mode routing (Phase 6.1) ----------

def test_intent_to_mode_maps_to_real_guidance_modes():
    import main
    import orchestrator
    # Every routed intent must map to a mode that actually has guidance, else the
    # routing would silently no-op.
    for intent, mode in main._INTENT_TO_MODE.items():
        assert mode in orchestrator._MODE_GUIDANCE, f"{intent} -> {mode}"


def test_retrospective_maps_to_review_and_planning_to_plan():
    import main
    assert main._INTENT_TO_MODE["retrospective"] == "review"
    assert main._INTENT_TO_MODE["planning"] == "plan"


def test_discussion_and_build_intents_have_no_mode():
    import main
    assert main._INTENT_TO_MODE.get("discussion") is None
    assert main._INTENT_TO_MODE.get("build") is None


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
