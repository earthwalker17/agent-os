"""Tests for Phase 10 — the orchestrator's combined inspect/research loop.

The LLM caller is scripted and ``execution.research.execute_research_request``
is monkeypatched, so no network and no real search key are ever involved.

Run:  python backend/tests/test_research_orchestration.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.manager as exec_manager  # noqa: E402
import execution.research as research_mod  # noqa: E402
import orchestrator  # noqa: E402
from execution.research import ResearchResult  # noqa: E402


# ---------- harness (mirrors test_inspect.py) ----------


class _TempLayout:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.execution_dir = root / "execution_workspaces"
        self.projects_dir = root / "projects"
        self.memory_dir = root / "memory"
        self.execution_dir.mkdir()
        self.projects_dir.mkdir()
        self.memory_dir.mkdir()
        for name in ("USER.md", "WORKSTYLE.md", "SOUL.md", "MEMORY.md"):
            (self.memory_dir / name).write_text("", encoding="utf-8")

        self._prev = (
            exec_manager._EXECUTION_ROOT,
            orchestrator.MEMORY_DIR,
            orchestrator.PROJECTS_DIR,
        )
        exec_manager._EXECUTION_ROOT = self.execution_dir
        orchestrator.MEMORY_DIR = self.memory_dir
        orchestrator.PROJECTS_DIR = self.projects_dir

    def cleanup(self) -> None:
        (
            exec_manager._EXECUTION_ROOT,
            orchestrator.MEMORY_DIR,
            orchestrator.PROJECTS_DIR,
        ) = self._prev
        self.tmp.cleanup()

    def make_project(self, project_id: str) -> Path:
        path = self.projects_dir / project_id
        path.mkdir(parents=True, exist_ok=True)
        for name in ("PROJECT.md", "STATUS.md", "TASK_QUEUE.md", "DECISIONS.md", "RESEARCH.md"):
            (path / name).write_text("", encoding="utf-8")
        return path

    def init_workspace(self, project_id: str, repo_files: dict[str, str]) -> Path:
        ws_dir = self.execution_dir / project_id
        repo_dir = ws_dir / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
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


def _make_caller(responses):
    seq = list(responses)
    calls: list[dict] = []

    def caller(system, messages, max_tokens=None, **kwargs):
        calls.append({"system": system, "messages": list(messages)})
        if not seq:
            raise AssertionError("LLM caller ran out of stub responses")
        return seq.pop(0)

    caller.calls = calls  # type: ignore[attr-defined]
    return caller


class _FakeResearch:
    """Monkeypatches execute_research_request; records every call."""

    def __init__(self, content="fetched text", ok=True):
        self.calls: list[dict] = []
        self.content = content
        self.ok = ok
        self._real = research_mod.execute_research_request

    def __enter__(self):
        def fake(project_id, request, *, user_urls=None):
            self.calls.append({
                "project_id": project_id, "request": dict(request),
                "user_urls": list(user_urls or []),
            })
            tool = str(request.get("tool", ""))
            return ResearchResult(
                ok=self.ok, kind=tool or "web_search",
                query=str(request.get("query") or ""),
                url=str(request.get("url") or ""),
                content=self.content if self.ok else "",
                error="" if self.ok else "boom",
            )

        research_mod.execute_research_request = fake  # type: ignore[assignment]
        return self

    def __exit__(self, *a):
        research_mod.execute_research_request = self._real
        return False


def _req(tool="web_search", **kw):
    return json.dumps({"research_request": {"tool": tool, **kw}})


# ---------- tests ----------


def test_research_only_loop_runs_without_workspace():
    def body(layout):
        layout.make_project("proj")  # NO execution workspace
        caller = _make_caller([
            _req(query="fastapi streaming"),
            _req(tool="fetch_url", url="https://docs.python.org/3/"),
            "Findings with **Sources**.",
        ])
        with _FakeResearch() as fake:
            text, inspected, sources = orchestrator.orchestrate(
                "proj", "@search fastapi streaming best practice",
                history=[], llm_caller=caller, mode="research", research_enabled=True,
            )
        assert text == "Findings with **Sources**."
        assert inspected == []
        assert len(sources) == 2
        assert sources[0]["tool"] == "web_search"
        assert sources[1]["tool"] == "fetch_url"
        assert len(fake.calls) == 2
        # granted prompt names the channel; inspect channel absent (no workspace)
        assert "Web Research (granted for this turn)" in caller.calls[0]["system"]
        assert "inspect_request" not in caller.calls[0]["system"]
        # the research results were fed back into the transcript
        transcript = caller.calls[2]["messages"]
        feedback = [m for m in transcript if m["role"] == "user" and "RESEARCH RESULT" in m["content"]]
        assert len(feedback) == 2

    _run(body)


def test_general_chat_gets_research_only_loop():
    def body(layout):
        caller = _make_caller([_req(query="q"), "General answer."])
        with _FakeResearch() as fake:
            text, inspected, sources = orchestrator.orchestrate(
                "__GENERAL__", "@search q", history=[],
                llm_caller=caller, mode="research", research_enabled=True,
            )
        assert text == "General answer."
        assert len(sources) == 1
        assert fake.calls[0]["project_id"] == "__GENERAL__"
        assert "Web Research (granted for this turn)" in caller.calls[0]["system"]
        assert "inspect_request" not in caller.calls[0]["system"]

    _run(body)


def test_request_budget_enforced_then_forced_text():
    def body(layout):
        layout.make_project("proj")
        cap = research_mod.MAX_RESEARCH_REQUESTS_PER_TURN
        caller = _make_caller([_req(query=f"q{i}") for i in range(cap)] + ["Final answer."])
        with _FakeResearch(content="x") as fake:
            text, _insp, sources = orchestrator.orchestrate(
                "proj", "@search deep dive", history=[],
                llm_caller=caller, mode="research", research_enabled=True,
            )
        assert text == "Final answer."
        assert len(sources) == cap
        assert len(fake.calls) == cap
        # the final call was the forced-text iteration: channel guidance dropped
        assert "## Web Research" not in caller.calls[-1]["system"]

    _run(body)


def test_char_budget_stops_execution_before_request_cap():
    def body(layout):
        layout.make_project("proj")
        big = "A" * (research_mod.RESEARCH_MAX_TOTAL_CHARS + 1)
        caller = _make_caller([
            _req(query="q1"),
            _req(query="q2"),
            "Answer from what I have.",
        ])
        with _FakeResearch(content=big) as fake:
            text, _insp, sources = orchestrator.orchestrate(
                "proj", "@search big topic", history=[],
                llm_caller=caller, mode="research", research_enabled=True,
            )
        assert text == "Answer from what I have."
        assert len(sources) == 1          # q2 was refused, not executed
        assert len(fake.calls) == 1
        transcript = caller.calls[2]["messages"]
        assert any("budget exhausted" in m["content"] for m in transcript if m["role"] == "user")

    _run(body)


def test_mixed_inspect_and_research_budgets_are_independent():
    def body(layout):
        layout.make_project("proj")
        layout.init_workspace("proj", {"README.md": "# hello repo\n"})
        caller = _make_caller([
            json.dumps({"inspect_request": {"tool": "read_file", "path": "README.md"}}),
            _req(query="how do others do this"),
            "Combined answer.",
        ])
        with _FakeResearch() as fake:
            text, inspected, sources = orchestrator.orchestrate(
                "proj", "@search compare our README to best practice", history=[],
                llm_caller=caller, mode="research", research_enabled=True,
            )
        assert text == "Combined answer."
        assert len(inspected) == 1 and inspected[0]["tool"] == "read_file"
        assert len(sources) == 1
        assert len(fake.calls) == 1
        # both channels documented in the granted prompt
        assert "inspect_request" in caller.calls[0]["system"]
        assert "Web Research (granted for this turn)" in caller.calls[0]["system"]

    _run(body)


def test_ungranted_turn_has_no_research_channel():
    def body(layout):
        layout.make_project("proj")
        layout.init_workspace("proj", {"README.md": "# r\n"})
        caller = _make_caller(["Plain answer."])
        text, _insp, sources = orchestrator.orchestrate(
            "proj", "what's the best practice here?", history=[],
            llm_caller=caller, mode="research", research_enabled=False,
        )
        assert text == "Plain answer."
        assert sources == []
        system = caller.calls[0]["system"]
        assert "## Web Research" not in system
        assert "research_request" not in system
        # the ungranted research mode explicitly says there is no web access
        assert "NO web access" in system

    _run(body)


def test_user_urls_flow_into_execute_calls():
    def body(layout):
        layout.make_project("proj")
        caller = _make_caller([
            _req(tool="fetch_url", url="https://my.example/spec"),
            "Done.",
        ])
        with _FakeResearch() as fake:
            orchestrator.orchestrate(
                "proj", "@search summarize https://my.example/spec please",
                history=[], llm_caller=caller, mode="research", research_enabled=True,
            )
        assert fake.calls[0]["user_urls"] == ["https://my.example/spec"]
        # pre-approved URLs are named in the granted prompt
        assert "https://my.example/spec" in caller.calls[0]["system"]

    _run(body)


def test_forced_text_tail_returns_final_even_if_model_keeps_emitting_json():
    # A model that NEVER stops emitting research JSON must still terminate: the
    # forced-text tail returns whatever the last call produced (no infinite loop).
    def body(layout):
        layout.make_project("proj")
        cap = research_mod.MAX_RESEARCH_REQUESTS_PER_TURN
        # emit JSON on every single call, including the forced-text iteration
        caller = _make_caller([_req(query=f"q{i}") for i in range(cap + 5)])
        with _FakeResearch() as fake:
            text, _insp, sources = orchestrator.orchestrate(
                "proj", "@search never stops", history=[],
                llm_caller=caller, mode="research", research_enabled=True,
            )
        # terminates; the loop ran a bounded number of LLM calls (cap+1)
        assert len(caller.calls) == cap + 1
        # only `cap` requests actually executed (budget), rest were refused
        assert len(fake.calls) == cap
        # the final (forced-text) call's raw JSON is returned as the answer
        assert text.startswith("{")

    _run(body)


def test_inspect_over_budget_yields_synthetic_result_without_research_leak():
    # Inspect budget exhausted while research budget remains: further
    # inspect_requests get a synthetic "budget exhausted" result and the loop
    # keeps going (research still available).
    def body(layout):
        layout.make_project("proj")
        layout.init_workspace("proj", {"a.py": "x=1\n"})
        insp = json.dumps({"inspect_request": {"tool": "read_file", "path": "a.py"}})
        # 3 real inspections + a 4th over-budget inspect + a research call + text
        caller = _make_caller([insp, insp, insp, insp, _req(query="q"), "Done."])
        with _FakeResearch() as fake:
            text, inspected, sources = orchestrator.orchestrate(
                "proj", "@search compare", history=[],
                llm_caller=caller, mode="research", research_enabled=True,
            )
        assert text == "Done."
        # only 3 inspections recorded (the 4th was refused, not executed)
        assert len(inspected) == 3
        assert len(sources) == 1
        # the over-budget inspect got the synthetic message in the transcript
        transcript = caller.calls[-1]["messages"]
        assert any(
            "inspection budget exhausted" in m["content"]
            for m in transcript if m["role"] == "user"
        )

    _run(body)


def test_failed_research_result_recorded_and_loop_continues():
    def body(layout):
        layout.make_project("proj")
        caller = _make_caller([_req(query="q"), "Answered despite failure."])
        with _FakeResearch(ok=False) as fake:
            text, _insp, sources = orchestrator.orchestrate(
                "proj", "@search q", history=[],
                llm_caller=caller, mode="research", research_enabled=True,
            )
        assert text == "Answered despite failure."
        assert len(sources) == 1
        assert sources[0]["ok"] is False
        assert sources[0]["error"] == "boom"
        assert len(fake.calls) == 1

    _run(body)


# ---------- standalone runner ----------


def _run_all() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    total = sum(1 for n, f in globals().items() if n.startswith("test_") and callable(f))
    if failures:
        print(f"\n{failures} of {total} tests failed.")
        return 1
    print(f"\nAll {total} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
