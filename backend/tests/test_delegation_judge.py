"""Lightweight tests for the Task 05.9 LLM delegation judge.

These tests stub the LLM caller — no Anthropic API key required. They cover
the core acceptance scenarios from the task brief:

  - direct English coding request           → dispatch_suggested
  - Chinese coding request                  → dispatch_suggested
  - anaphoric follow-up "do that"           → dispatch_suggested (LLM judges)
  - ordinary architecture discussion        → discussion
  - memory-only update request              → memory_only
  - GENERAL workspace                       → judge skipped (no LLM call)
  - empty input                             → discussion (no LLM call)
  - LLM exception                           → heuristic fallback
  - malformed JSON                          → heuristic fallback
  - invalid decision value                  → heuristic fallback
  - dispatch decision has task_card override

Plus:
  - is_code_delegation() still recognizes `@code …` exactly as before.

Run directly:
    python backend/tests/test_delegation_judge.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make backend/ importable when running this file directly.
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from execution.delegation_judge import (  # noqa: E402
    DECISION_DISCUSSION,
    DECISION_DISPATCH,
    DECISION_MEMORY_ONLY,
    DelegationDecision,
    judge_delegation,
    render_dispatch_suggestion,
)
from execution.chat_delegation import is_code_delegation  # noqa: E402


def _make_caller(payload):
    """Return a stub llm_chat that records its inputs and returns ``payload``.

    ``payload`` may be:
      - a string (returned directly)
      - an Exception class or instance (raised when called)
    """
    calls: list[dict] = []

    def caller(system, messages, max_tokens=None, **kwargs):
        calls.append({"system": system, "messages": messages, "max_tokens": max_tokens})
        if isinstance(payload, BaseException) or (
            isinstance(payload, type) and issubclass(payload, BaseException)
        ):
            raise payload if not isinstance(payload, type) else payload("stub failure")
        return payload

    caller.calls = calls  # type: ignore[attr-defined]
    return caller


# ---------- core decisions ----------


def test_direct_english_coding_request_dispatches():
    caller = _make_caller(json.dumps({
        "decision": "dispatch_suggested",
        "confidence": 0.92,
        "reason": "Direct imperative asking for a new endpoint.",
        "proposed_task_card": "Add a /healthcheck endpoint to backend/main.py returning 200 OK.",
    }))
    result = judge_delegation(
        project_id="agent-os",
        project_name="Agent OS",
        user_message="Please add a /healthcheck endpoint to the backend.",
        history=[],
        is_general=False,
        llm_caller=caller,
    )
    assert result.decision == DECISION_DISPATCH
    assert result.proposed_task_card.startswith("Add a /healthcheck")
    assert result.source == "llm"
    assert len(caller.calls) == 1


def test_chinese_coding_request_dispatches():
    caller = _make_caller(json.dumps({
        "decision": "dispatch_suggested",
        "confidence": 0.88,
        "reason": "Non-English imperative asking to fix a bug.",
        "proposed_task_card": "Fix the login redirect bug in frontend/src/components/Login.tsx.",
    }))
    result = judge_delegation(
        project_id="agent-os",
        project_name="Agent OS",
        user_message="继续做这个修复,把登录跳转的 bug 修掉",
        history=[],
        is_general=False,
        llm_caller=caller,
    )
    assert result.decision == DECISION_DISPATCH
    assert "Fix the login redirect" in result.proposed_task_card


def test_anaphoric_followup_dispatches_when_judge_resolves():
    caller = _make_caller(json.dumps({
        "decision": "dispatch_suggested",
        "confidence": 0.78,
        "reason": "'Do that' refers to the implementation plan from the previous assistant turn.",
        "proposed_task_card": "Refactor sandbox.py to extract path validation into a helper.",
    }))
    history = [
        {"role": "user", "content": "Should we refactor sandbox.py?"},
        {
            "role": "assistant",
            "content": (
                "Yes — extracting the path-validation logic into a helper would "
                "make the rules easier to test. I'd pull it into a `_resolve_path` "
                "function and have `resolve_repo_path` delegate to it."
            ),
        },
        {"role": "user", "content": "do that"},
    ]
    result = judge_delegation(
        project_id="agent-os",
        project_name="Agent OS",
        user_message="do that",
        history=history,
        is_general=False,
        llm_caller=caller,
    )
    assert result.decision == DECISION_DISPATCH
    assert "sandbox.py" in result.proposed_task_card


def test_architecture_discussion_stays_discussion():
    caller = _make_caller(json.dumps({
        "decision": "discussion",
        "confidence": 0.95,
        "reason": "Architecture question with no code change request.",
        "proposed_task_card": "",
    }))
    result = judge_delegation(
        project_id="agent-os",
        project_name="Agent OS",
        user_message="Why did we choose ThreadPoolExecutor over Celery here?",
        history=[],
        is_general=False,
        llm_caller=caller,
    )
    assert result.decision == DECISION_DISCUSSION
    assert result.proposed_task_card == ""


def test_memory_only_request_classified_as_memory_only():
    caller = _make_caller(json.dumps({
        "decision": "memory_only",
        "confidence": 0.9,
        "reason": "User is asking to update the STATUS.md file, not the repo.",
        "proposed_task_card": "",
    }))
    result = judge_delegation(
        project_id="agent-os",
        project_name="Agent OS",
        user_message="Update the status in the explanation to say Phase 3 is done.",
        history=[],
        is_general=False,
        llm_caller=caller,
    )
    assert result.decision == DECISION_MEMORY_ONLY
    # Schema invariant: task card must be empty for non-dispatch decisions
    # even if the model returned one.
    assert result.proposed_task_card == ""


# ---------- short-circuits that skip the LLM ----------


def test_general_workspace_skips_llm():
    caller = _make_caller("should not be called")
    result = judge_delegation(
        project_id="__GENERAL__",
        project_name=None,
        user_message="implement an entire backend please",
        history=[],
        is_general=True,
        llm_caller=caller,
    )
    assert result.decision == DECISION_DISCUSSION
    assert result.source == "general_workspace"
    assert len(caller.calls) == 0


def test_empty_message_skips_llm():
    caller = _make_caller("should not be called")
    result = judge_delegation(
        project_id="agent-os",
        project_name="Agent OS",
        user_message="   ",
        history=[],
        is_general=False,
        llm_caller=caller,
    )
    assert result.decision == DECISION_DISCUSSION
    assert result.source == "empty_input"
    assert len(caller.calls) == 0


# ---------- fallback paths ----------


def test_llm_exception_falls_back_to_heuristic_dispatch():
    caller = _make_caller(RuntimeError)
    # Strong verb + code-context token → heuristic flags as code-shaped.
    result = judge_delegation(
        project_id="agent-os",
        project_name="Agent OS",
        user_message="please fix the bug in the login button",
        history=[],
        is_general=False,
        llm_caller=caller,
    )
    assert result.decision == DECISION_DISPATCH
    assert result.source == "heuristic_fallback"
    assert "fix the bug" in result.proposed_task_card.lower()


def test_llm_exception_falls_back_to_heuristic_discussion():
    caller = _make_caller(RuntimeError)
    # Discussion-style phrasing — heuristic should NOT trigger.
    result = judge_delegation(
        project_id="agent-os",
        project_name="Agent OS",
        user_message="what do you think about our overall architecture?",
        history=[],
        is_general=False,
        llm_caller=caller,
    )
    assert result.decision == DECISION_DISCUSSION
    assert result.source == "heuristic_fallback"


def test_malformed_json_falls_back():
    caller = _make_caller("not actually json {{{")
    result = judge_delegation(
        project_id="agent-os",
        project_name="Agent OS",
        user_message="what do you think about our overall architecture?",
        history=[],
        is_general=False,
        llm_caller=caller,
    )
    assert result.source == "heuristic_fallback"
    assert result.decision == DECISION_DISCUSSION


def test_invalid_decision_value_falls_back():
    caller = _make_caller(json.dumps({
        "decision": "yolo_dispatch",
        "confidence": 0.99,
        "reason": "...",
        "proposed_task_card": "drop the prod DB",
    }))
    result = judge_delegation(
        project_id="agent-os",
        project_name="Agent OS",
        user_message="what do you think about our architecture?",
        history=[],
        is_general=False,
        llm_caller=caller,
    )
    assert result.source == "heuristic_fallback"
    assert result.decision == DECISION_DISCUSSION
    assert "drop the prod DB" not in result.proposed_task_card


def test_judge_tolerates_code_fenced_json():
    caller = _make_caller(
        "```json\n"
        + json.dumps({
            "decision": "discussion",
            "confidence": 0.6,
            "reason": "Question, not a request.",
            "proposed_task_card": "",
        })
        + "\n```"
    )
    result = judge_delegation(
        project_id="agent-os",
        project_name="Agent OS",
        user_message="what's the plan for run-result reconciliation?",
        history=[],
        is_general=False,
        llm_caller=caller,
    )
    assert result.source == "llm"
    assert result.decision == DECISION_DISCUSSION


# ---------- rendering ----------


def test_render_dispatch_suggestion_uses_llm_task_card():
    decision = DelegationDecision(
        decision=DECISION_DISPATCH,
        confidence=0.9,
        reason="...",
        proposed_task_card="Refactor sandbox.py: extract _resolve_path helper.",
        source="llm",
    )
    rendered = render_dispatch_suggestion(decision, "do that")
    assert "Refactor sandbox.py" in rendered
    assert "@code Refactor sandbox.py" in rendered
    # The raw "do that" should not leak into the @code command.
    assert "@code do that" not in rendered


def test_render_dispatch_suggestion_falls_back_to_message_when_no_card():
    decision = DelegationDecision(
        decision=DECISION_DISPATCH,
        confidence=0.5,
        reason="...",
        proposed_task_card="",
        source="llm",
    )
    rendered = render_dispatch_suggestion(decision, "please fix the login button bug")
    # derive_task_card strips "please ", so the @code command shouldn't include it.
    assert "@code fix the login button bug" in rendered


# ---------- @code explicit path is untouched ----------


def test_explicit_at_code_still_recognized():
    assert is_code_delegation("@code add a /healthcheck endpoint")
    assert is_code_delegation("@code: refactor sandbox.py")
    assert is_code_delegation("@code")
    # `@coder` / `@codereview` must NOT match.
    assert not is_code_delegation("@coder please do X")
    assert not is_code_delegation("@codereview the PR")
    assert not is_code_delegation("please add a /healthcheck endpoint")


# ---------- runner ----------


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
