"""Tests for Phase 10 — agents/skills HTTP endpoints + the @search chat flow.

Network is faked everywhere: the LLM caller is stubbed at the orchestrator
seam, and ``execution.research.execute_research_request`` is monkeypatched in
the chat tests — no web, no search key, no real memory writes.

Run directly:
    python backend/tests/test_agents_endpoints.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import agents_registry  # noqa: E402
import database  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
import execution.research as research_mod  # noqa: E402
import llm  # noqa: E402
import memory_engine  # noqa: E402
import orchestrator  # noqa: E402
import skills_store  # noqa: E402
from execution.delegation_judge import DelegationDecision  # noqa: E402
from execution.research import ResearchResult  # noqa: E402


class _Env:
    """Tempdir harness: skills live in a seeded copy so writes never touch
    the committed skills/ content."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.skills_dir = root / "skills"
        self.skills_dir.mkdir()
        self._restore: list = []
        self._save(skills_store, "SKILLS_DIR", self.skills_dir)
        self._save(llm, "chat", lambda system, messages, **kw: "msg")
        self.client = TestClient(main.app)

    def _save(self, obj, attr, value):
        self._restore.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def seed_skill(self, agent_id: str, skill_id: str, content: str):
        path = self.skills_dir / agent_id / f"{skill_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def cleanup(self):
        for obj, attr, val in reversed(self._restore):
            setattr(obj, attr, val)
        self.tmp.cleanup()


def _run(test_body):
    env = _Env()
    try:
        test_body(env)
    finally:
        env.cleanup()


# ---------- GET /api/agents ----------


def test_list_agents_shape():
    def body(env):
        r = env.client.get("/api/agents")
        assert r.status_code == 200
        agents = r.json()["agents"]
        assert len(agents) == len(agents_registry.AGENTS)
        by_id = {a["id"]: a for a in agents}
        researcher = by_id["researcher"]
        assert researcher["command"] == "@search"
        assert researcher["aliases"] == ["@research"]
        assert researcher["capabilities"]["searches_web"] is True
        assert researcher["capabilities"]["requires_confirmation"] is True
        # skill refs only — bodies are fetched on demand
        assert all("content" not in s for s in researcher["skills"])
        assert by_id["growth_launch"]["status"] == "planned"
        assert by_id["coder"]["command"] == "@code"
        assert by_id["deploy_ops"]["command"] == ""

    _run(body)


# ---------- skill read / write ----------


def test_skill_read_returns_registry_metadata_and_body():
    def body(env):
        env.seed_skill("planner", "task-breakdown-checklist", "# T\n> d\n\nsteps")
        r = env.client.get("/api/agents/planner/skills/task-breakdown-checklist")
        assert r.status_code == 200
        data = r.json()
        assert data["agent_id"] == "planner"
        assert data["skill_id"] == "task-breakdown-checklist"
        assert data["title"] == "Task Breakdown Checklist"
        assert data["content"] == "# T\n> d\n\nsteps"

    _run(body)


def test_skill_read_unknown_pair_is_404():
    def body(env):
        assert env.client.get("/api/agents/planner/skills/nope").status_code == 404
        assert env.client.get("/api/agents/nope/skills/task-breakdown-checklist").status_code == 404
        # real skill id, wrong owner
        assert env.client.get("/api/agents/planner/skills/code-review-rubric").status_code == 404

    _run(body)


def test_skill_write_roundtrip():
    def body(env):
        r = env.client.post(
            "/api/agents/reviewer/skills/code-review-rubric",
            json={"content": "# Code Review Rubric\n> d\n\n- edited by user"},
        )
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
        r2 = env.client.get("/api/agents/reviewer/skills/code-review-rubric")
        assert "- edited by user" in r2.json()["content"]

    _run(body)


def test_skill_write_validation():
    def body(env):
        assert env.client.post(
            "/api/agents/reviewer/skills/nope", json={"content": "x"}
        ).status_code == 404
        assert env.client.post(
            "/api/agents/reviewer/skills/code-review-rubric", json={"content": "   "}
        ).status_code == 400
        oversize = "x" * (skills_store.MAX_SKILL_CHARS + 1)
        assert env.client.post(
            "/api/agents/reviewer/skills/code-review-rubric", json={"content": oversize}
        ).status_code == 400

    _run(body)


# ---------- the @search chat flow ----------


class _ChatEnv:
    """Full chat harness: temp projects/memory/db, scripted orchestrator LLM,
    recorded memory-intake + delegation-judge stubs, faked research executor."""

    def __init__(self, llm_responses: list[str]) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects_dir = root / "projects"
        self.execution_dir = root / "execution_workspaces"
        self.memory_dir = root / "memory"
        for d in (self.projects_dir, self.execution_dir, self.memory_dir):
            d.mkdir()
        for name in ("USER.md", "WORKSTYLE.md", "SOUL.md", "MEMORY.md"):
            (self.memory_dir / name).write_text("", encoding="utf-8")

        self._restore: list = []
        self._save(main, "PROJECTS_DIR", self.projects_dir)
        self._save(exec_manager, "_EXECUTION_ROOT", self.execution_dir)
        self._save(orchestrator, "MEMORY_DIR", self.memory_dir)
        self._save(orchestrator, "PROJECTS_DIR", self.projects_dir)
        self._save(database, "DB_PATH", root / "agent_os.db")
        database.init_db()

        # Claude must look available so the default provider resolves.
        self._key_backup = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "test-key"

        # Scripted main-response LLM at the orchestrator seam.
        self._llm_queue = list(llm_responses)

        def fake_llm(system=None, messages=None, **kw):
            assert self._llm_queue, "orchestrator LLM stub ran out of responses"
            return self._llm_queue.pop(0)

        self._save(orchestrator, "llm_chat", fake_llm)
        self._save(llm, "chat", fake_llm)

        # Recorded memory-intake stub (never writes).
        self.memory_judge_calls: list[dict] = []

        def fake_intake(scope, ctx, user_message, assistant_response, *, intent=None, **kw):
            self.memory_judge_calls.append({"scope": scope, "intent": intent})
            return memory_engine.MemoryDecision(should_update=False, reason="", updates=[])

        self._save(main, "judge_memory_intake", fake_intake)

        # Recorded delegation-judge stub (defaults to plain discussion).
        self.judge_calls: list[str] = []
        self.judge_decision = DelegationDecision(
            decision="discussion", confidence=0.9, reason="", proposed_task_card="",
            source="llm", intent="",
        )

        def fake_judge(**kw):
            self.judge_calls.append(kw.get("user_message", ""))
            return self.judge_decision

        self._save(main, "judge_delegation", fake_judge)

        # Faked research executor (records calls, no network).
        self.research_calls: list[dict] = []

        def fake_research(project_id, request, *, user_urls=None):
            self.research_calls.append({"project_id": project_id, "request": dict(request)})
            return ResearchResult(
                ok=True, kind=str(request.get("tool", "web_search")),
                query=str(request.get("query") or ""), url=str(request.get("url") or ""),
                title="Doc", content="distilled snippet",
            )

        self._save(research_mod, "execute_research_request", fake_research)

        self.client = TestClient(main.app)

    def _save(self, obj, attr, value):
        self._restore.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def make_project(self, pid: str):
        path = self.projects_dir / pid
        path.mkdir(parents=True, exist_ok=True)
        for name in ("PROJECT.md", "STATUS.md", "TASK_QUEUE.md", "DECISIONS.md", "RESEARCH.md"):
            (path / name).write_text("", encoding="utf-8")

    def cleanup(self):
        for obj, attr, val in reversed(self._restore):
            setattr(obj, attr, val)
        if self._key_backup is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._key_backup
        self.tmp.cleanup()


def _search_json(query="what is x"):
    return json.dumps({"research_request": {"tool": "web_search", "query": query}})


def test_chat_general_search_runs_research_but_skips_memory():
    env = _ChatEnv([_search_json(), "Cited answer.\n\n**Sources**: example"])
    try:
        conv = database.create_conversation(orchestrator.GENERAL_PROJECT_ID, "c")
        r = env.client.post("/api/chat", json={
            "conversation_id": conv["id"], "message": "@search what is x",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["intent"] == "research"
        assert len(body["research_sources"]) == 1
        assert body["research_sources"][0]["tool"] == "web_search"
        assert body["memory_updated"] is False
        assert body["research_suggestion"] is None
        # GENERAL research turn: research ran, memory intake did NOT
        assert len(env.research_calls) == 1
        assert env.memory_judge_calls == []
        assert env.judge_calls == []  # explicit command skips the judge

    finally:
        env.cleanup()


def test_chat_project_search_persists_sources_in_metadata():
    env = _ChatEnv([_search_json("best practice"), "Findings with sources."])
    try:
        env.make_project("proj")
        conv = database.create_conversation("proj", "c")
        r = env.client.post("/api/chat", json={
            "conversation_id": conv["id"],
            "message": "@research best practice for x",
        })
        assert r.status_code == 200
        body = r.json()
        assert len(body["research_sources"]) == 1
        # an explicit @research command skips the delegation judge — meaningful
        # here (a PROJECT chat, where the judge WOULD otherwise run).
        assert env.judge_calls == []
        # project turns still run memory intake (project scope)
        assert env.memory_judge_calls == [{"scope": "project", "intent": "research"}]
        # the audit trail survives a reload via message metadata
        msgs = env.client.get(f"/api/conversations/{conv['id']}/messages").json()
        assistant = [m for m in msgs if m["role"] == "assistant"][-1]
        meta = assistant.get("metadata") or {}
        assert len(meta.get("research_sources") or []) == 1
        assert meta["research_sources"][0]["query"] == "best practice"

    finally:
        env.cleanup()


def test_chat_semantic_research_intent_only_suggests():
    env = _ChatEnv(["Here is what I know offline."])
    try:
        env.make_project("proj")
        env.judge_decision = DelegationDecision(
            decision="discussion", confidence=0.8, reason="", proposed_task_card="",
            source="llm", intent="research",
        )
        conv = database.create_conversation("proj", "c")
        message = "find current best practice for auth and update my plan"
        r = env.client.post("/api/chat", json={
            "conversation_id": conv["id"], "message": message,
        })
        assert r.status_code == 200
        body = r.json()
        # proposed, not executed: a suggestion, zero network, zero sources
        assert body["research_suggestion"] == {"command": "@search", "query": message}
        assert body["research_sources"] == []
        assert env.research_calls == []
        assert len(env.judge_calls) == 1
        # persisted for reload
        msgs = env.client.get(f"/api/conversations/{conv['id']}/messages").json()
        assistant = [m for m in msgs if m["role"] == "assistant"][-1]
        assert (assistant.get("metadata") or {}).get("research_suggestion") == {
            "command": "@search", "query": message,
        }

    finally:
        env.cleanup()


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
