"""Tests for Task 06.0 — post-run memory reconciliation.

These tests stub the LLM caller and use a temporary projects/ +
execution_workspaces/ layout, so no Anthropic API key is required.

Coverage:

  - parser:  good JSON, fenced JSON, malformed JSON, non-object, accepts both
             "file" and "filename" keys, filters disallowed files.
  - skip rules: read-only inspection runs, failed-noisy runs, non-terminal
                status, already-reconciled idempotency.
  - judge: happy path returning a structured decision.
  - end-to-end pipeline: applied / skipped tags + run.json fields written
                back / dedup against existing memory content.

Run directly:
    python backend/tests/test_memory_reconciliation.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Make backend/ importable when running this file directly.
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Patch the on-disk roots BEFORE importing the modules that capture them at
# import time. We point both `_PROJECTS_DIR` (in memory_reconciliation) and
# the execution manager's `_EXECUTION_ROOT` at temp paths so writes can't
# touch the real repo state.
import execution.memory_reconciliation as mr  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution.models import RunRecord, RunStatus  # noqa: E402
from execution.memory_reconciliation import (  # noqa: E402
    RECONCILIATION_WRITABLE_FILES,
    ReconciliationDecision,
    ReconciliationUpdate,
    TAG_APPLIED,
    TAG_ERROR,
    TAG_SKIPPED_ALREADY_RECONCILED,
    TAG_SKIPPED_FAILED_NOISY,
    TAG_SKIPPED_JUDGE_NO_UPDATE,
    TAG_SKIPPED_NO_VALID_UPDATES,
    TAG_SKIPPED_NON_TERMINAL,
    TAG_SKIPPED_READ_ONLY,
    _is_failed_noisy_run,
    _is_read_only_run,
    _parse_decision,
    judge_run_memory_reconciliation,
    reconcile_run_memory,
)


# ---------- harness ----------


class _TempLayout:
    """Set up temporary projects/ + execution_workspaces/ and patch globals."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects_dir = root / "projects"
        self.execution_dir = root / "execution_workspaces"
        self.projects_dir.mkdir()
        self.execution_dir.mkdir()
        self._prev_projects = mr._PROJECTS_DIR
        self._prev_execution = exec_manager._EXECUTION_ROOT
        mr._PROJECTS_DIR = self.projects_dir
        exec_manager._EXECUTION_ROOT = self.execution_dir

    def cleanup(self) -> None:
        mr._PROJECTS_DIR = self._prev_projects
        exec_manager._EXECUTION_ROOT = self._prev_execution
        self.tmp.cleanup()

    def make_project(self, project_id: str, files: dict[str, str] | None = None) -> None:
        path = self.projects_dir / project_id
        path.mkdir(parents=True, exist_ok=True)
        for name in ("PROJECT.md", "STATUS.md", "TASK_QUEUE.md", "DECISIONS.md", "RESEARCH.md"):
            (path / name).write_text((files or {}).get(name, ""), encoding="utf-8")

    def make_run(
        self,
        project_id: str,
        run_id: str,
        record: RunRecord,
        result_md: str = "",
        task_card: str = "Do a thing",
    ) -> None:
        run_store.init_run_dir(project_id, run_id)
        run_store.write_task_card(project_id, run_id, record.task_title, task_card)
        run_store.write_run_json(project_id, run_id, record)
        if result_md:
            run_store.write_result_md(project_id, run_id, result_md)

    def read_memory(self, project_id: str, filename: str) -> str:
        path = self.projects_dir / project_id / filename
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def read_run_json(self, project_id: str, run_id: str) -> dict:
        return run_store.read_run_json(project_id, run_id) or {}


def _make_caller(payload):
    """Return a stub LLM caller that records inputs and returns ``payload``."""
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


def _record(
    *,
    status: RunStatus = RunStatus.COMPLETED,
    files_changed: list[str] | None = None,
    blockers: list[str] | None = None,
    commands_run: list[str] | None = None,
) -> RunRecord:
    return RunRecord(
        run_id="20260516-000000-deadbeef",
        project_id="agent-os",
        task_title="Test task",
        status=status,
        files_changed=files_changed or [],
        blockers=blockers or [],
        commands_run=commands_run or [],
    )


# ---------- parser tests ----------


def test_parse_decision_happy_path():
    raw = json.dumps({
        "should_update": True,
        "reason": "added /healthcheck",
        "updates": [
            {
                "file": "STATUS.md",
                "section": "What Works",
                "content": "- /healthcheck endpoint",
                "action": "append",
            }
        ],
    })
    decision = _parse_decision(raw)
    assert decision is not None
    assert decision.should_update is True
    assert len(decision.updates) == 1
    assert decision.updates[0].filename == "STATUS.md"
    assert decision.updates[0].action == "append"


def test_parse_decision_accepts_filename_alias():
    raw = json.dumps({
        "should_update": True,
        "reason": "ok",
        "updates": [{
            "filename": "DECISIONS.md",
            "section": "Decisions",
            "content": "- chose A over B",
            "action": "append",
        }],
    })
    decision = _parse_decision(raw)
    assert decision is not None
    assert decision.updates[0].filename == "DECISIONS.md"


def test_parse_decision_filters_disallowed_files():
    raw = json.dumps({
        "should_update": True,
        "reason": "ok",
        "updates": [
            {"file": "SOUL.md", "section": "x", "content": "evil", "action": "append"},
            {"file": "USER.md", "section": "x", "content": "evil", "action": "append"},
            {"file": "PROJECT.md", "section": "x", "content": "evil", "action": "append"},
            {"file": "repo/main.py", "section": "x", "content": "evil", "action": "append"},
            {"file": "STATUS.md", "section": "x", "content": "ok", "action": "append"},
        ],
    })
    decision = _parse_decision(raw)
    assert decision is not None
    assert len(decision.updates) == 1
    assert decision.updates[0].filename == "STATUS.md"


def test_parse_decision_tolerates_code_fence():
    fenced = "```json\n" + json.dumps({
        "should_update": False,
        "reason": "nothing to do",
        "updates": [],
    }) + "\n```"
    decision = _parse_decision(fenced)
    assert decision is not None
    assert decision.should_update is False
    assert decision.updates == []


def test_parse_decision_malformed_json_returns_none():
    assert _parse_decision("not json {{{") is None


def test_parse_decision_non_object_returns_none():
    assert _parse_decision(json.dumps(["just", "a", "list"])) is None


def test_parse_decision_coerces_should_update_when_no_valid_updates():
    raw = json.dumps({
        "should_update": True,
        "reason": "tried",
        "updates": [
            {"file": "SOUL.md", "section": "x", "content": "evil", "action": "append"},
        ],
    })
    decision = _parse_decision(raw)
    assert decision is not None
    # All updates filtered out -> should_update flips to False so caller can
    # record a clean "no_valid_updates" skip tag.
    assert decision.should_update is False
    assert decision.updates == []


def test_parse_decision_defaults_action_to_append():
    raw = json.dumps({
        "should_update": True,
        "reason": "ok",
        "updates": [
            {"file": "STATUS.md", "section": "x", "content": "hi", "action": "weird"},
        ],
    })
    decision = _parse_decision(raw)
    assert decision is not None
    assert decision.updates[0].action == "append"


def test_parse_decision_skips_empty_content():
    raw = json.dumps({
        "should_update": True,
        "reason": "ok",
        "updates": [
            {"file": "STATUS.md", "section": "x", "content": "   ", "action": "append"},
        ],
    })
    decision = _parse_decision(raw)
    assert decision is not None
    assert decision.updates == []


# ---------- skip-rule tests ----------


def test_read_only_run_is_skipped():
    record = _record(status=RunStatus.COMPLETED, files_changed=[], blockers=[])
    assert _is_read_only_run(record, summary_text="") is True


def test_completed_with_files_is_not_read_only():
    record = _record(status=RunStatus.COMPLETED, files_changed=["repo/main.py"])
    assert _is_read_only_run(record, summary_text="") is False


def test_completed_with_long_summary_is_not_read_only():
    record = _record(status=RunStatus.COMPLETED, files_changed=[], blockers=[])
    long_summary = "We discovered that the bug only repros under PowerShell." * 2
    assert _is_read_only_run(record, summary_text=long_summary) is False


def test_failed_with_no_blocker_is_noisy():
    record = _record(status=RunStatus.FAILED, files_changed=[], blockers=["x"])
    assert _is_failed_noisy_run(record, summary_text="") is True


def test_failed_with_long_blocker_is_not_noisy():
    record = _record(
        status=RunStatus.FAILED,
        files_changed=[],
        blockers=["Pydantic v2 model_dump rejects unknown kwargs; need to patch llm.py."],
    )
    assert _is_failed_noisy_run(record, summary_text="") is False


def test_failed_with_long_summary_is_not_noisy():
    record = _record(status=RunStatus.FAILED, files_changed=[], blockers=["x"])
    summary = "Tried switching the build to esbuild but the project relies on Vite plugins."
    assert _is_failed_noisy_run(record, summary_text=summary) is False


def test_completed_status_is_not_failed_noisy():
    record = _record(status=RunStatus.COMPLETED, files_changed=[], blockers=[])
    assert _is_failed_noisy_run(record, summary_text="") is False


# ---------- judge tests ----------


def test_judge_returns_structured_decision_on_good_payload():
    record = _record(files_changed=["repo/main.py"])
    caller = _make_caller(json.dumps({
        "should_update": True,
        "reason": "added /healthcheck",
        "updates": [{
            "file": "STATUS.md",
            "section": "What Works",
            "content": "- /healthcheck",
            "action": "append",
        }],
    }))
    decision = judge_run_memory_reconciliation(
        project_id="agent-os",
        record=record,
        summary_text="added /healthcheck endpoint",
        task_card="add a healthcheck endpoint",
        result_md_text="",
        memory_snapshot={k: "" for k in RECONCILIATION_WRITABLE_FILES},
        llm_caller=caller,
    )
    assert decision is not None
    assert decision.should_update is True
    assert decision.updates[0].filename == "STATUS.md"
    assert len(caller.calls) == 1


def test_judge_returns_none_on_llm_exception():
    record = _record(files_changed=["repo/main.py"])
    caller = _make_caller(RuntimeError)
    decision = judge_run_memory_reconciliation(
        project_id="agent-os",
        record=record,
        summary_text="x",
        task_card="x",
        result_md_text="",
        memory_snapshot={k: "" for k in RECONCILIATION_WRITABLE_FILES},
        llm_caller=caller,
    )
    assert decision is None


# ---------- end-to-end pipeline tests ----------


def _run_e2e(test_body):
    layout = _TempLayout()
    try:
        test_body(layout)
    finally:
        layout.cleanup()


def test_pipeline_applies_update_and_persists_outcome():
    def body(layout: _TempLayout):
        layout.make_project("agent-os", {
            "STATUS.md": "# Status\n\n## What Works\n- baseline\n",
            "TASK_QUEUE.md": "# Tasks\n",
            "DECISIONS.md": "# Decisions\n",
            "RESEARCH.md": "# Research\n",
        })
        record = _record(
            status=RunStatus.COMPLETED,
            files_changed=["repo/backend/main.py"],
        )
        layout.make_run(
            "agent-os",
            record.run_id,
            record,
            result_md="# Run Result\n\n## Summary\nAdded /healthcheck endpoint.\n",
            task_card="add a healthcheck",
        )
        caller = _make_caller(json.dumps({
            "should_update": True,
            "reason": "Run added a new endpoint, STATUS update warranted.",
            "updates": [{
                "file": "STATUS.md",
                "section": "What Works",
                "content": "- /healthcheck endpoint added",
                "action": "append",
            }],
        }))
        outcome = reconcile_run_memory(
            "agent-os",
            record.run_id,
            summary_override="Added /healthcheck endpoint.",
            llm_caller=caller,
        )
        assert outcome.tag == TAG_APPLIED
        assert outcome.reconciled is True
        assert len(outcome.applied) == 1

        # STATUS.md was actually appended to.
        status_text = layout.read_memory("agent-os", "STATUS.md")
        assert "/healthcheck endpoint added" in status_text

        # run.json carries the new fields.
        record_dict = layout.read_run_json("agent-os", record.run_id)
        assert record_dict["memory_reconciled"] is True
        assert record_dict["memory_reconciliation"] == TAG_APPLIED
        assert record_dict["memory_reconciliation_error"] is None

    _run_e2e(body)


def test_pipeline_skips_read_only_run_without_llm_call():
    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        record = _record(
            status=RunStatus.COMPLETED,
            files_changed=[],
            blockers=[],
        )
        layout.make_run(
            "agent-os",
            record.run_id,
            record,
            result_md="# Run Result\n\n## Summary\n_(no summary)_\n",
        )
        caller = _make_caller("should not be called")
        outcome = reconcile_run_memory(
            "agent-os",
            record.run_id,
            summary_override="",
            llm_caller=caller,
        )
        assert outcome.tag == TAG_SKIPPED_READ_ONLY
        assert outcome.reconciled is False
        assert len(caller.calls) == 0
        record_dict = layout.read_run_json("agent-os", record.run_id)
        assert record_dict["memory_reconciled"] is False
        assert record_dict["memory_reconciliation"] == TAG_SKIPPED_READ_ONLY

    _run_e2e(body)


def test_pipeline_skips_failed_noisy_run():
    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        record = _record(
            status=RunStatus.FAILED,
            files_changed=[],
            blockers=["oops"],
        )
        layout.make_run("agent-os", record.run_id, record, result_md="")
        caller = _make_caller("should not be called")
        outcome = reconcile_run_memory(
            "agent-os",
            record.run_id,
            summary_override="",
            llm_caller=caller,
        )
        assert outcome.tag == TAG_SKIPPED_FAILED_NOISY
        assert len(caller.calls) == 0

    _run_e2e(body)


def test_pipeline_idempotent_against_double_call():
    def body(layout: _TempLayout):
        layout.make_project("agent-os", {
            "STATUS.md": "# Status\n\n## What Works\n- baseline\n",
            "TASK_QUEUE.md": "",
            "DECISIONS.md": "",
            "RESEARCH.md": "",
        })
        record = _record(
            status=RunStatus.COMPLETED,
            files_changed=["repo/main.py"],
        )
        layout.make_run("agent-os", record.run_id, record, result_md="# Run Result\n\n## Summary\nAdded /healthcheck.\n")
        caller = _make_caller(json.dumps({
            "should_update": True,
            "reason": "ok",
            "updates": [{
                "file": "STATUS.md",
                "section": "What Works",
                "content": "- /healthcheck added",
                "action": "append",
            }],
        }))
        first = reconcile_run_memory(
            "agent-os",
            record.run_id,
            summary_override="Added /healthcheck.",
            llm_caller=caller,
        )
        assert first.tag == TAG_APPLIED

        # Second call must skip without invoking the LLM.
        second_caller = _make_caller("should not be called")
        second = reconcile_run_memory(
            "agent-os",
            record.run_id,
            summary_override="Added /healthcheck.",
            llm_caller=second_caller,
        )
        assert second.tag == TAG_SKIPPED_ALREADY_RECONCILED
        assert len(second_caller.calls) == 0

        # STATUS.md only got one append, not two.
        status_text = layout.read_memory("agent-os", "STATUS.md")
        assert status_text.count("/healthcheck added") == 1

    _run_e2e(body)


def test_pipeline_dedup_against_existing_memory_content():
    def body(layout: _TempLayout):
        layout.make_project("agent-os", {
            "STATUS.md": "# Status\n\n## What Works\n- /healthcheck endpoint added\n",
            "TASK_QUEUE.md": "",
            "DECISIONS.md": "",
            "RESEARCH.md": "",
        })
        record = _record(
            status=RunStatus.COMPLETED,
            files_changed=["repo/main.py"],
        )
        layout.make_run("agent-os", record.run_id, record, result_md="# Run Result\n\n## Summary\nReran the same change.\n")
        # Same payload the file already contains — dedup must reject the append.
        caller = _make_caller(json.dumps({
            "should_update": True,
            "reason": "thought we should note healthcheck",
            "updates": [{
                "file": "STATUS.md",
                "section": "What Works",
                "content": "- /healthcheck endpoint added",
                "action": "append",
            }],
        }))
        outcome = reconcile_run_memory(
            "agent-os",
            record.run_id,
            summary_override="Reran the same change.",
            llm_caller=caller,
        )
        assert outcome.tag == TAG_SKIPPED_NO_VALID_UPDATES
        # STATUS.md unchanged — still one occurrence.
        status_text = layout.read_memory("agent-os", "STATUS.md")
        assert status_text.count("/healthcheck endpoint added") == 1

    _run_e2e(body)


def test_pipeline_marks_error_when_judge_returns_malformed_json():
    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        record = _record(
            status=RunStatus.COMPLETED,
            files_changed=["repo/main.py"],
        )
        layout.make_run("agent-os", record.run_id, record, result_md="# Run Result\n\n## Summary\nWork.\n")
        caller = _make_caller("not json {{")
        outcome = reconcile_run_memory(
            "agent-os",
            record.run_id,
            summary_override="Work.",
            llm_caller=caller,
        )
        assert outcome.tag == TAG_ERROR
        assert outcome.error is not None
        record_dict = layout.read_run_json("agent-os", record.run_id)
        assert record_dict["memory_reconciliation"] == TAG_ERROR
        assert record_dict["memory_reconciliation_error"]

    _run_e2e(body)


def test_pipeline_marks_judge_no_update_when_should_update_false():
    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        record = _record(
            status=RunStatus.COMPLETED,
            files_changed=["repo/main.py"],
        )
        layout.make_run("agent-os", record.run_id, record, result_md="# Run Result\n\n## Summary\nMinor cleanup.\n")
        caller = _make_caller(json.dumps({
            "should_update": False,
            "reason": "Cleanup not worth recording",
            "updates": [],
        }))
        outcome = reconcile_run_memory(
            "agent-os",
            record.run_id,
            summary_override="Minor cleanup.",
            llm_caller=caller,
        )
        assert outcome.tag == TAG_SKIPPED_JUDGE_NO_UPDATE
        record_dict = layout.read_run_json("agent-os", record.run_id)
        assert record_dict["memory_reconciled"] is False
        assert record_dict["memory_reconciliation"] == TAG_SKIPPED_JUDGE_NO_UPDATE

    _run_e2e(body)


def test_pipeline_skips_non_terminal_status():
    def body(layout: _TempLayout):
        layout.make_project("agent-os")
        record = _record(status=RunStatus.RUNNING)
        layout.make_run("agent-os", record.run_id, record)
        caller = _make_caller("should not be called")
        outcome = reconcile_run_memory(
            "agent-os",
            record.run_id,
            summary_override="",
            llm_caller=caller,
        )
        assert outcome.tag == TAG_SKIPPED_NON_TERMINAL
        assert len(caller.calls) == 0

    _run_e2e(body)


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
