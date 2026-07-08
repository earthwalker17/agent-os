"""Tests for Task 06.1 — on-demand file inspection.

Two surfaces under test:

  1. ``execution.inspect`` — the public list/read/search wrappers, the
     ``inspect_request`` parser, and the LLM-transcript formatter.
  2. ``orchestrator.orchestrate`` — the bounded inspection loop, with a
     stubbed LLM caller that exercises both "no inspection needed" and
     multi-step inspect→answer flows.

Both surfaces run against a temporary filesystem layout so no real
``execution_workspaces/`` or ``projects/`` directories are touched.

Run directly:
    python backend/tests/test_inspect.py
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

import execution.manager as exec_manager  # noqa: E402
import execution.inspect as inspect_mod  # noqa: E402
import orchestrator  # noqa: E402
from execution.inspect import (  # noqa: E402
    INSPECT_MAX_LIST_ENTRIES,
    INSPECT_MAX_READ_CHARS,
    InspectionResult,
    execute_inspect_request,
    format_result_for_llm,
    list_repo_files,
    parse_inspect_request,
    read_repo_file,
    search_repo_files,
)


# ---------- harness ----------


class _TempLayout:
    """Temporary execution_workspaces/ + projects/ + memory/ layout."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.execution_dir = root / "execution_workspaces"
        self.projects_dir = root / "projects"
        self.memory_dir = root / "memory"
        self.execution_dir.mkdir()
        self.projects_dir.mkdir()
        self.memory_dir.mkdir()

        self._prev_execution_root = exec_manager._EXECUTION_ROOT
        self._prev_orch_memory_dir = orchestrator.MEMORY_DIR
        self._prev_orch_projects_dir = orchestrator.PROJECTS_DIR
        exec_manager._EXECUTION_ROOT = self.execution_dir
        orchestrator.MEMORY_DIR = self.memory_dir
        orchestrator.PROJECTS_DIR = self.projects_dir

    def cleanup(self) -> None:
        exec_manager._EXECUTION_ROOT = self._prev_execution_root
        orchestrator.MEMORY_DIR = self._prev_orch_memory_dir
        orchestrator.PROJECTS_DIR = self._prev_orch_projects_dir
        self.tmp.cleanup()

    def make_project(self, project_id: str, files: dict[str, str] | None = None) -> Path:
        path = self.projects_dir / project_id
        path.mkdir(parents=True, exist_ok=True)
        defaults = {
            "PROJECT.md": f"# {project_id}\n",
            "STATUS.md": "",
            "TASK_QUEUE.md": "",
            "DECISIONS.md": "",
            "RESEARCH.md": "",
        }
        defaults.update(files or {})
        for name, body in defaults.items():
            (path / name).write_text(body, encoding="utf-8")
        return path

    def init_workspace(self, project_id: str, repo_files: dict[str, str]) -> Path:
        """Create a minimal execution workspace with the given repo files."""
        ws_dir = self.execution_dir / project_id
        repo_dir = ws_dir / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        # Required workspace marker files
        (ws_dir / "AGENT.md").write_text("# AGENT\n", encoding="utf-8")
        (ws_dir / "TASK.md").write_text("# TASK\n", encoding="utf-8")
        (ws_dir / "runs").mkdir(exist_ok=True)
        (ws_dir / "logs").mkdir(exist_ok=True)
        for rel_path, body in repo_files.items():
            target = repo_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        return repo_dir


def _run(test_body):
    layout = _TempLayout()
    try:
        test_body(layout)
    finally:
        layout.cleanup()


# ---------- parser tests ----------


def test_parser_accepts_well_formed_read_request():
    raw = json.dumps({"inspect_request": {"tool": "read_file", "path": "src/main.py"}})
    parsed = parse_inspect_request(raw)
    assert parsed == {"tool": "read_file", "path": "src/main.py"}


def test_parser_tolerates_code_fence():
    raw = "```json\n" + json.dumps({
        "inspect_request": {"tool": "list_files", "path": "."},
    }) + "\n```"
    parsed = parse_inspect_request(raw)
    assert parsed is not None
    assert parsed["tool"] == "list_files"


def test_parser_rejects_unknown_tool():
    raw = json.dumps({"inspect_request": {"tool": "rm_rf", "path": "/"}})
    assert parse_inspect_request(raw) is None


def test_parser_extracts_request_embedded_in_prose():
    # Pre-launch E2E regression: models sometimes narrate before/after the
    # JSON directive. A silently dropped request never executes and the raw
    # protocol text leaks into the visible answer — so an embedded,
    # well-formed directive now parses.
    raw = "Sure, let me look at that:\n" + json.dumps({
        "inspect_request": {"tool": "read_file", "path": "x"},
    })
    assert parse_inspect_request(raw) == {"tool": "read_file", "path": "x"}
    # ...including with trailing narration after the object.
    raw2 = (
        "I need the entry point first.\n\n"
        '{"inspect_request": {"tool": "read_file", "path": "src/main.py"}}\n\n'
        "Then I will summarize it."
    )
    assert parse_inspect_request(raw2) == {"tool": "read_file", "path": "src/main.py"}


def test_parser_embedded_still_rejects_mentions_and_bad_tools():
    # A bare mention of the key without a spanning JSON object stays inert.
    assert parse_inspect_request('the inspect_request channel takes a "tool" field') is None
    assert parse_inspect_request('use "inspect_request" to read files') is None
    # An embedded directive with an unknown tool is still rejected.
    raw = "Trying:\n" + json.dumps({"inspect_request": {"tool": "rm_rf", "path": "/"}})
    assert parse_inspect_request(raw) is None
    # Malformed JSON around the key is rejected.
    assert parse_inspect_request('{"inspect_request": {"tool": "read_file", }') is None


def test_parser_rejects_empty_and_text():
    assert parse_inspect_request("") is None
    assert parse_inspect_request("just a normal answer") is None
    assert parse_inspect_request("{not json}") is None


def test_parser_rejects_non_object_root():
    assert parse_inspect_request(json.dumps(["nope"])) is None


# ---------- workspace-rejection tests ----------


def test_general_workspace_rejected_for_all_three_tools():
    def body(_layout: _TempLayout):
        list_r = list_repo_files("__GENERAL__")
        assert list_r.ok is False
        assert "GENERAL" in list_r.error

        read_r = read_repo_file("__GENERAL__", "x")
        assert read_r.ok is False
        assert "GENERAL" in read_r.error

        search_r = search_repo_files("__GENERAL__", "q")
        assert search_r.ok is False
        assert "GENERAL" in search_r.error

    _run(body)


def test_missing_workspace_rejected():
    def body(_layout: _TempLayout):
        result = list_repo_files("no-such-project")
        assert result.ok is False
        assert "not initialized" in result.error.lower() or "workspace" in result.error.lower()

    _run(body)


# ---------- list_repo_files tests ----------


def test_list_repo_files_returns_entries():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", {
            "README.md": "# hello\n",
            "src/main.py": "print('hi')\n",
        })
        result = list_repo_files("agent-os", ".")
        assert result.ok is True
        assert "[d] src" in result.content
        assert "[f] README.md" in result.content

    _run(body)


def test_list_repo_files_caps_entries():
    def body(layout: _TempLayout):
        big = {f"file_{i:03d}.txt": "x" for i in range(INSPECT_MAX_LIST_ENTRIES + 25)}
        layout.init_workspace("agent-os", big)
        result = list_repo_files("agent-os", ".")
        assert result.ok is True
        line_count = len([ln for ln in result.content.split("\n") if ln.startswith("[f]")])
        assert line_count <= INSPECT_MAX_LIST_ENTRIES
        assert result.truncated is True

    _run(body)


# ---------- read_repo_file tests ----------


def test_read_repo_file_returns_content():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", {"src/main.py": "print('hi')\n"})
        result = read_repo_file("agent-os", "src/main.py")
        assert result.ok is True
        assert "print('hi')" in result.content
        assert result.truncated is False

    _run(body)


def test_read_repo_file_truncates_large_files():
    def body(layout: _TempLayout):
        body_text = "x" * (INSPECT_MAX_READ_CHARS + 500)
        layout.init_workspace("agent-os", {"big.txt": body_text})
        result = read_repo_file("agent-os", "big.txt")
        assert result.ok is True
        assert len(result.content) == INSPECT_MAX_READ_CHARS
        assert result.truncated is True
        assert result.note  # has a human note about the truncation

    _run(body)


def test_read_repo_file_rejects_path_traversal():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", {"x.txt": "ok"})
        result = read_repo_file("agent-os", "../../../etc/passwd")
        assert result.ok is False
        assert "traversal" in result.error.lower() or "not allowed" in result.error.lower()

    _run(body)


def test_read_repo_file_rejects_absolute_path():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", {"x.txt": "ok"})
        result = read_repo_file("agent-os", "/etc/passwd")
        assert result.ok is False

    _run(body)


def test_read_repo_file_rejects_env_file():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", {".env": "SECRET=xyz"})
        result = read_repo_file("agent-os", ".env")
        assert result.ok is False
        assert "sensitive" in result.error.lower() or "not accessible" in result.error.lower()

    _run(body)


def test_read_repo_file_rejects_missing_file():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", {"x.txt": "ok"})
        result = read_repo_file("agent-os", "nope.py")
        assert result.ok is False

    _run(body)


def test_read_repo_file_rejects_cross_project_access():
    def body(layout: _TempLayout):
        # Two separate workspaces. project-a should not see project-b's files.
        layout.init_workspace("project-a", {"public.md": "ok"})
        layout.init_workspace("project-b", {"secret.md": "private"})
        result = read_repo_file("project-a", "../../project-b/repo/secret.md")
        assert result.ok is False

    _run(body)


# ---------- search_repo_files tests ----------


def test_search_repo_files_finds_matches():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", {
            "src/main.py": "def healthcheck():\n    return 'ok'\n",
            "README.md": "# repo\n",
        })
        result = search_repo_files("agent-os", "healthcheck")
        assert result.ok is True
        assert "src/main.py" in result.content
        assert "healthcheck" in result.content

    _run(body)


def test_search_repo_files_rejects_empty_query():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", {"x.txt": "ok"})
        result = search_repo_files("agent-os", "")
        assert result.ok is False

    _run(body)


# ---------- execute + format ----------


def test_execute_dispatches_correct_tool():
    def body(layout: _TempLayout):
        layout.init_workspace("agent-os", {"src/main.py": "hello"})
        r = execute_inspect_request("agent-os", {"tool": "read_file", "path": "src/main.py"})
        assert r.ok is True and "hello" in r.content

        r2 = execute_inspect_request("agent-os", {"tool": "list_files", "path": "."})
        assert r2.ok is True and "[d] src" in r2.content

        r3 = execute_inspect_request("agent-os", {
            "tool": "search_files", "query": "hello", "path": "."
        })
        assert r3.ok is True and "hello" in r3.content

    _run(body)


def test_execute_handles_unknown_tool():
    result = execute_inspect_request("agent-os", {"tool": "evil"})
    assert result.ok is False
    assert "unknown" in result.error.lower()


def test_format_result_renders_compact_block():
    r = InspectionResult(
        ok=True,
        kind="read_file",
        path="x.py",
        content="print(1)",
        truncated=False,
    )
    text = format_result_for_llm(r)
    assert "INSPECTION RESULT" in text
    assert "tool=read_file" in text
    assert "path=x.py" in text
    assert "print(1)" in text


def test_format_result_includes_error_for_failures():
    r = InspectionResult(ok=False, kind="read_file", path="x", error="not found")
    text = format_result_for_llm(r)
    assert "ok=false" in text
    assert "not found" in text


# ---------- orchestrate() loop tests ----------


def _make_caller(responses):
    """Stub llm_chat that returns ``responses[i]`` on the i-th call."""
    seq = list(responses)
    calls: list[dict] = []

    def caller(system, messages, max_tokens=None, **kwargs):
        calls.append({"system": system, "messages": list(messages), "max_tokens": max_tokens})
        if not seq:
            raise AssertionError("LLM caller ran out of stub responses")
        return seq.pop(0)

    caller.calls = calls  # type: ignore[attr-defined]
    return caller


def _seed_minimal_memory(layout: _TempLayout):
    """Write empty global memory files so orchestrator.load_memory doesn't fail."""
    (layout.memory_dir / "USER.md").write_text("", encoding="utf-8")
    (layout.memory_dir / "WORKSTYLE.md").write_text("", encoding="utf-8")
    (layout.memory_dir / "SOUL.md").write_text("", encoding="utf-8")
    (layout.memory_dir / "MEMORY.md").write_text("", encoding="utf-8")


def test_orchestrate_no_workspace_returns_text_directly():
    def body(layout: _TempLayout):
        _seed_minimal_memory(layout)
        layout.make_project("no-ws")
        # No workspace initialized → no inspection loop.
        caller = _make_caller(["Hello — answer from memory."])
        text, inspected, _research = orchestrator.orchestrate(
            "no-ws", "What's the project status?", history=[], llm_caller=caller,
        )
        assert text == "Hello — answer from memory."
        assert inspected == []
        # Inspection guidance should NOT be in the system prompt for this turn.
        assert "inspect_request" not in caller.calls[0]["system"]

    _run(body)


def test_orchestrate_general_workspace_skips_inspection():
    def body(layout: _TempLayout):
        _seed_minimal_memory(layout)
        caller = _make_caller(["Reply from GENERAL."])
        text, inspected, _research = orchestrator.orchestrate(
            "__GENERAL__", "What's up?", history=[], llm_caller=caller,
        )
        assert text == "Reply from GENERAL."
        assert inspected == []
        assert "inspect_request" not in caller.calls[0]["system"]

    _run(body)


def test_orchestrate_with_workspace_no_inspection_needed():
    def body(layout: _TempLayout):
        _seed_minimal_memory(layout)
        layout.make_project("agent-os")
        layout.init_workspace("agent-os", {"README.md": "# repo\n"})
        # LLM answers directly — no inspect_request emitted.
        caller = _make_caller(["I can answer this without inspecting any files."])
        text, inspected, _research = orchestrator.orchestrate(
            "agent-os", "Summarize the project plan.", history=[], llm_caller=caller,
        )
        assert text.startswith("I can answer this")
        assert inspected == []
        assert len(caller.calls) == 1
        # System prompt SHOULD include the inspection guidance for projects with workspaces.
        assert "inspect_request" in caller.calls[0]["system"]

    _run(body)


def test_orchestrate_inspect_then_answer():
    def body(layout: _TempLayout):
        _seed_minimal_memory(layout)
        layout.make_project("agent-os")
        layout.init_workspace("agent-os", {"src/main.py": "def healthcheck():\n    pass\n"})
        caller = _make_caller([
            json.dumps({"inspect_request": {"tool": "read_file", "path": "src/main.py"}}),
            "I read src/main.py and found a healthcheck function.",
        ])
        text, inspected, _research = orchestrator.orchestrate(
            "agent-os",
            "What's in src/main.py?",
            history=[],
            llm_caller=caller,
        )
        assert "healthcheck function" in text
        assert len(inspected) == 1
        assert inspected[0]["tool"] == "read_file"
        assert inspected[0]["path"] == "src/main.py"
        assert inspected[0]["ok"] is True
        assert len(caller.calls) == 2
        # Second call must have the inspection result in the transcript.
        second_msgs = caller.calls[1]["messages"]
        assert any("INSPECTION RESULT" in m["content"] for m in second_msgs)

    _run(body)


def test_orchestrate_caps_at_max_inspections():
    def body(layout: _TempLayout):
        _seed_minimal_memory(layout)
        layout.make_project("agent-os")
        layout.init_workspace("agent-os", {"a.txt": "a", "b.txt": "b", "c.txt": "c"})
        # Model keeps asking for inspections forever. The loop must force a
        # text answer after MAX_INSPECTIONS_PER_TURN inspections.
        infinite_request = json.dumps(
            {"inspect_request": {"tool": "read_file", "path": "a.txt"}}
        )
        caller = _make_caller([
            infinite_request,  # step 0  → run inspection #1
            infinite_request,  # step 1  → run inspection #2
            infinite_request,  # step 2  → run inspection #3
            "Final answer forced after cap.",  # step 3 (force_text)
        ])
        text, inspected, _research = orchestrator.orchestrate(
            "agent-os", "tell me about a.txt", history=[], llm_caller=caller,
        )
        assert text == "Final answer forced after cap."
        assert len(inspected) == orchestrator.MAX_INSPECTIONS_PER_TURN
        # Forced-text turn must NOT include the inspection channel in the system prompt.
        assert "inspect_request" not in caller.calls[-1]["system"]

    _run(body)


def test_orchestrate_records_failed_inspection_and_lets_model_recover():
    def body(layout: _TempLayout):
        _seed_minimal_memory(layout)
        layout.make_project("agent-os")
        layout.init_workspace("agent-os", {"src/main.py": "ok"})
        caller = _make_caller([
            json.dumps({"inspect_request": {"tool": "read_file", "path": "../../etc/passwd"}}),
            "I couldn't read that path because it escapes the sandbox.",
        ])
        text, inspected, _research = orchestrator.orchestrate(
            "agent-os",
            "Read /etc/passwd",
            history=[],
            llm_caller=caller,
        )
        assert "couldn't read" in text.lower() or "sandbox" in text.lower()
        assert len(inspected) == 1
        assert inspected[0]["ok"] is False
        assert inspected[0]["error"]

    _run(body)


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
