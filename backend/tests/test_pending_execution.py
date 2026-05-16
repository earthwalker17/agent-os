"""Tests for the Task 05.9.5 confirmable execution plan layer.

Covers:
  - serialize_pending adapter shape
  - revise_pending_plan happy path with stubbed LLM
  - revise_pending_plan fallback on LLM exception
  - revise_pending_plan fallback on malformed JSON
  - revise_pending_plan handles empty instructions
  - render_pending_chat_body / render_revised_chat_body produce confirm-aware text
  - derive_title_from_card truncates long titles
  - delegation judge passes display_plan + title through

Run directly:
    python backend/tests/test_pending_execution.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from execution.delegation_judge import judge_delegation  # noqa: E402
from execution.pending_execution import (  # noqa: E402
    PendingExecutionView,
    derive_title_from_card,
    render_pending_chat_body,
    render_revised_chat_body,
    revise_pending_plan,
    serialize_pending,
)


def _make_view(**overrides) -> PendingExecutionView:
    defaults = {
        "pending_execution_id": "abc123",
        "project_id": "agent-os",
        "conversation_id": "convXYZ",
        "title": "Add /healthcheck endpoint",
        "display_plan": (
            "I'll add a `/healthcheck` endpoint to `backend/main.py` that "
            "returns 200 OK with a JSON `{status: 'ok'}` body."
        ),
        "task_card": (
            "Add a /healthcheck endpoint to backend/main.py that returns "
            "{status: 'ok'} with HTTP 200."
        ),
        "status": "pending",
        "run_id": None,
        "revision_count": 0,
        "created_at": "2026-05-15T10:00:00",
        "updated_at": "2026-05-15T10:00:00",
    }
    defaults.update(overrides)
    return PendingExecutionView(**defaults)


def _make_caller(payload):
    def caller(system, messages, max_tokens=None, **kwargs):
        if isinstance(payload, type) and issubclass(payload, BaseException):
            raise payload("stub failure")
        return payload
    return caller


# ---------- serialize_pending ----------


def test_serialize_pending_maps_id_to_pending_execution_id():
    row = {
        "id": "p1",
        "project_id": "p",
        "conversation_id": "c",
        "title": "t",
        "display_plan": "plan",
        "task_card": "card",
        "status": "pending",
        "run_id": None,
        "revision_count": 0,
        "created_at": "2026-05-15T10:00:00",
        "updated_at": "2026-05-15T10:00:00",
    }
    view = serialize_pending(row)
    assert view.pending_execution_id == "p1"
    assert view.title == "t"
    assert view.status == "pending"
    assert view.revision_count == 0


def test_serialize_pending_tolerates_missing_optional_fields():
    # revision_count present as None must coerce to 0, not crash.
    row = {
        "id": "p1",
        "project_id": "p",
        "conversation_id": "c",
        "title": "t",
        "display_plan": "plan",
        "task_card": "card",
        "status": "pending",
        "run_id": None,
        "revision_count": None,
        "created_at": "2026-05-15T10:00:00",
        "updated_at": "2026-05-15T10:00:00",
    }
    view = serialize_pending(row)
    assert view.revision_count == 0


# ---------- revise_pending_plan ----------


def test_revise_pending_plan_happy_path():
    view = _make_view()
    caller = _make_caller(json.dumps({
        "title": "Add /healthcheck (typed)",
        "display_plan": "I'll add a typed `/healthcheck` route returning `{status: 'ok'}`.",
        "task_card": "Add a typed /healthcheck endpoint returning a Pydantic model.",
        "change_summary": "Switched to a typed response model.",
    }))
    result = revise_pending_plan(view, "use a Pydantic response model", llm_caller=caller)
    assert result.source == "llm"
    assert result.title.startswith("Add /healthcheck")
    assert "Pydantic" in result.task_card
    assert "Switched to a typed" in result.change_summary


def test_revise_pending_plan_falls_back_on_exception():
    view = _make_view()
    result = revise_pending_plan(
        view, "use a Pydantic model", llm_caller=_make_caller(RuntimeError),
    )
    assert result.source == "heuristic_fallback"
    # Heuristic keeps existing plan and appends the instruction verbatim.
    assert "Pydantic" in result.task_card
    assert "Pydantic" in result.display_plan
    assert "Revision LLM unavailable" in result.change_summary


def test_revise_pending_plan_falls_back_on_malformed_json():
    view = _make_view()
    result = revise_pending_plan(
        view, "use a Pydantic model", llm_caller=_make_caller("not json {{{"),
    )
    assert result.source == "heuristic_fallback"


def test_revise_pending_plan_no_op_on_empty_instructions():
    view = _make_view()
    result = revise_pending_plan(view, "   ", llm_caller=_make_caller("should not be called"))
    assert result.source == "heuristic_fallback"
    assert result.display_plan == view.display_plan
    assert result.task_card == view.task_card
    assert "No revision instructions" in result.change_summary


def test_revise_pending_plan_truncates_long_title():
    view = _make_view()
    long_title = "x" * 200
    caller = _make_caller(json.dumps({
        "title": long_title,
        "display_plan": "plan",
        "task_card": "card",
        "change_summary": "ok",
    }))
    result = revise_pending_plan(view, "rename it", llm_caller=caller)
    assert len(result.title) <= 80
    assert result.title.endswith("...")


def test_revise_pending_plan_uses_old_fields_when_llm_returns_empty():
    view = _make_view()
    caller = _make_caller(json.dumps({
        "title": "",
        "display_plan": "",
        "task_card": "",
        "change_summary": "",
    }))
    result = revise_pending_plan(view, "tweak it", llm_caller=caller)
    # Empty LLM outputs must NOT clobber the existing plan.
    assert result.title == view.title
    assert result.display_plan == view.display_plan
    assert result.task_card == view.task_card


# ---------- render_pending_chat_body / render_revised_chat_body ----------


def test_render_pending_chat_body_includes_confirm_instructions():
    view = _make_view()
    body = render_pending_chat_body(view)
    assert "/healthcheck" in body
    assert "OK, run this" in body
    assert "Revise plan" in body


def test_render_pending_chat_body_handles_empty_display_plan():
    view = _make_view(display_plan="")
    body = render_pending_chat_body(view)
    assert view.title in body
    assert "confirm" in body.lower()


def test_render_revised_chat_body_shows_change_summary_and_new_plan():
    view = _make_view(
        display_plan="The revised plan goes here.",
        revision_count=1,
    )
    body = render_revised_chat_body(view, "Switched to TypeScript.")
    assert "Updated the plan based on your feedback" in body
    assert "Switched to TypeScript" in body
    assert "The revised plan goes here" in body
    assert "OK, run this" in body


def test_render_revised_chat_body_handles_empty_change_summary():
    view = _make_view()
    body = render_revised_chat_body(view, "")
    # Should still produce a usable message without crashing on empty summary.
    assert "Updated the plan" in body
    assert "What changed" not in body  # omitted when summary is empty


# ---------- derive_title_from_card ----------


def test_derive_title_from_card_uses_first_line():
    title = derive_title_from_card("Add a /healthcheck endpoint\nReturn 200 OK")
    assert title == "Add a /healthcheck endpoint"


def test_derive_title_from_card_truncates_long_first_line():
    title = derive_title_from_card("x" * 200)
    assert len(title) <= 80
    assert title.endswith("...")


def test_derive_title_from_card_fallback_on_empty():
    assert derive_title_from_card("") == "Coding task"
    assert derive_title_from_card("   \n  ") == "Coding task"


# ---------- delegation judge: display_plan + title round-trip ----------


def test_judge_passes_display_plan_and_title_through():
    payload = json.dumps({
        "decision": "dispatch_suggested",
        "confidence": 0.9,
        "reason": "Direct request",
        "title": "Add healthcheck endpoint",
        "display_plan": "I'll add /healthcheck returning {status: 'ok'}.\n\n- step 1\n- step 2",
        "proposed_task_card": "Add /healthcheck to backend/main.py.",
    })
    decision = judge_delegation(
        project_id="agent-os",
        project_name="Agent OS",
        user_message="add a healthcheck endpoint",
        history=[],
        is_general=False,
        llm_caller=lambda **kwargs: payload,
    )
    assert decision.decision == "dispatch_suggested"
    assert decision.title == "Add healthcheck endpoint"
    assert "step 1" in decision.display_plan
    assert decision.proposed_task_card.startswith("Add /healthcheck")


def test_judge_blanks_display_plan_for_non_dispatch_decisions():
    payload = json.dumps({
        "decision": "discussion",
        "confidence": 0.95,
        "reason": "Q&A",
        "title": "should be cleared",
        "display_plan": "should be cleared",
        "proposed_task_card": "should be cleared",
    })
    decision = judge_delegation(
        project_id="agent-os",
        project_name="Agent OS",
        user_message="why did we pick FastAPI?",
        history=[],
        is_general=False,
        llm_caller=lambda **kwargs: payload,
    )
    assert decision.decision == "discussion"
    assert decision.title == ""
    assert decision.display_plan == ""
    assert decision.proposed_task_card == ""


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
