"""Tests for Phase 10.2 — minimal local RAG (execution/local_rag.py).

Bounded retrieval over project memory + run history + repo map, plus the
inspect-channel ``retrieve`` tool and the ``/retrieve`` HTTP endpoint. No
network, no LLM.

Run:  python backend/tests/test_local_rag.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution import local_rag  # noqa: E402
from execution.inspect import execute_inspect_request, parse_inspect_request  # noqa: E402
from execution.models import RunRecord, RunStatus  # noqa: E402


class _Layout:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects_dir = root / "projects"
        self.execution_dir = root / "execution_workspaces"
        self.projects_dir.mkdir()
        self.execution_dir.mkdir()
        self._restore = [
            (local_rag, "_PROJECTS_DIR", local_rag._PROJECTS_DIR),
            (exec_manager, "_EXECUTION_ROOT", exec_manager._EXECUTION_ROOT),
            (main, "PROJECTS_DIR", main.PROJECTS_DIR),
        ]
        local_rag._PROJECTS_DIR = self.projects_dir
        exec_manager._EXECUTION_ROOT = self.execution_dir
        main.PROJECTS_DIR = self.projects_dir

    def project(self, pid, memory: dict[str, str] | None = None):
        p = self.projects_dir / pid
        p.mkdir(parents=True, exist_ok=True)
        for name, body in (memory or {}).items():
            (p / name).write_text(body, encoding="utf-8")
        return p

    def workspace(self, pid, repo_files: dict[str, str] | None = None):
        ws = self.execution_dir / pid
        repo = ws / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        (ws / "AGENT.md").write_text("# AGENT\n", encoding="utf-8")
        (ws / "TASK.md").write_text("# TASK\n", encoding="utf-8")
        (ws / "runs").mkdir(exist_ok=True)
        (ws / "logs").mkdir(exist_ok=True)
        for rel, body in (repo_files or {}).items():
            f = repo / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body, encoding="utf-8")
        return ws

    def run(self, pid, run_id, **fields):
        rec = RunRecord(run_id=run_id, project_id=pid, task_title=fields.pop("task_title", "t"),
                        status=fields.pop("status", RunStatus.COMPLETED), **fields)
        run_store.init_run_dir(pid, run_id)
        run_store.write_run_json(pid, run_id, rec)

    def cleanup(self):
        for obj, attr, val in self._restore:
            setattr(obj, attr, val)
        self.tmp.cleanup()


def _run(body):
    lay = _Layout()
    try:
        body(lay)
    finally:
        lay.cleanup()


# ---------- memory retrieval ----------


def test_search_memory_returns_scored_sections():
    def body(lay):
        lay.project("p", {
            "STATUS.md": "# Status\n\n## What Works\n- login works\n\n## Task Queue\n### In Progress\n- [ ] add rate limiting to the API\n",
            "DECISIONS.md": "# Decisions\n\n## Decisions\n- Chose SlowAPI for rate limiting\n",
            "RESEARCH.md": "# Research\n\n## Findings\n- unrelated note about colors\n",
        })
        hits = local_rag.search_memory("p", "rate limiting")
        srcs = [h.source for h in hits]
        assert any("DECISIONS.md" in s for s in srcs)
        assert any("STATUS.md#Task Queue" in s for s in srcs)
        # unrelated section not surfaced
        assert not any("RESEARCH.md" in s for s in srcs)

    _run(body)


def test_search_memory_caps_hits():
    def body(lay):
        big = "# Status\n" + "".join(
            f"\n## Sec{i}\n- rate limiting matters here {i}\n" for i in range(20)
        )
        lay.project("p", {"STATUS.md": big})
        hits = local_rag.search_memory("p", "rate limiting")
        assert len(hits) <= local_rag.RAG_MAX_MEMORY_HITS

    _run(body)


# ---------- run history ----------


def test_recent_runs_compact_and_query_preferred():
    def body(lay):
        lay.workspace("p")
        lay.run("p", "20240101-000000-aaaaaaaa", task_title="build login",
                summary="added login form", status=RunStatus.COMPLETED)
        lay.run("p", "20240102-000000-bbbbbbbb", task_title="add rate limiting",
                summary="wired SlowAPI middleware", status=RunStatus.PARTIAL,
                blockers=["tests failed"])
        hits = local_rag.recent_runs("p", "rate limiting")
        assert hits[0].title == "add rate limiting"  # query-preferred first
        assert "SlowAPI" in hits[0].snippet
        assert "blocker: tests failed" in hits[0].snippet

    _run(body)


def test_recent_runs_empty_without_workspace():
    def body(lay):
        lay.project("p")
        assert local_rag.recent_runs("p", "anything") == []

    _run(body)


# ---------- repo map ----------


def test_repo_map_lists_tree_and_summarizes_matching_files():
    def body(lay):
        lay.project("p")
        lay.workspace("p", {
            "src/ratelimit.py": "def limit():\n    # token bucket\n    return True\n",
            "README.md": "# Project\nsome docs\n",
        })
        hits = local_rag.repo_map("p", "ratelimit")
        assert hits[0].source == "repo:tree"
        assert any(h.source == "repo:src/ratelimit.py" for h in hits)

    _run(body)


def test_repo_map_never_exposes_git_or_env():
    def body(lay):
        lay.project("p")
        ws = lay.workspace("p", {"app.py": "x=1\n"})
        # plant sensitive files the sandbox must never surface
        (ws / "repo" / ".env").write_text("TAVILY_API_KEY=tvly_secret\n", encoding="utf-8")
        hits = local_rag.repo_map("p", "env")
        blob = "\n".join(h.snippet for h in hits)
        assert "tvly_secret" not in blob
        assert ".env" not in blob

    _run(body)


def test_repo_map_filters_broad_credential_files():
    def body(lay):
        lay.project("p")
        ws = lay.workspace("p", {"app.py": "x=1\n"})
        # credential files the sandbox's narrow set does NOT block, but retrieval must
        for name, secret in (
            (".npmrc", "//registry.npmjs.org/:_authToken=npm_SECRET123"),
            ("credentials.json", '{"key": "SECRET_JSON"}'),
            ("id_rsa", "-----BEGIN PRIVATE KEY-----\nSECRET_RSA"),
        ):
            (ws / "repo" / name).write_text(secret + "\n", encoding="utf-8")
        hits = local_rag.repo_map("p", "npmrc credentials rsa token key")
        blob = "\n".join(h.snippet + " " + h.title for h in hits)
        for marker in ("npm_SECRET123", "SECRET_JSON", "SECRET_RSA", "npmrc", "credentials.json", "id_rsa"):
            assert marker not in blob, marker

    _run(body)


def test_retrieve_redacts_secret_shaped_values_in_hits():
    def body(lay):
        # a memory file that (wrongly) contains a secret-shaped value; a
        # `NAME_KEY=value` line is caught by credentials.redact's KV pattern
        # (no real-provider prefix — keeps the fake literal off secret scanners).
        lay.project("p", {
            "RESEARCH.md": "# R\n\n## Findings\nAPI_KEY=PROBEsecretVALUE0123456789\n",
        })
        lay.workspace("p")
        res = local_rag.retrieve("p", "key findings", kinds=["memory"])
        blob = "\n".join(h.snippet for h in res.hits)
        assert "PROBEsecretVALUE0123456789" not in blob

    _run(body)


# ---------- retrieve orchestration ----------


def test_retrieve_general_and_unknown_rejected():
    def body(lay):
        assert local_rag.retrieve("__GENERAL__", "x").ok is False
        assert local_rag.retrieve("nope", "x").ok is False

    _run(body)


def test_retrieve_combines_sources_and_respects_kinds():
    def body(lay):
        lay.project("p", {"STATUS.md": "# S\n\n## Task Queue\n- [ ] rate limiting\n"})
        lay.workspace("p", {"rl.py": "# rate limiting\n"})
        lay.run("p", "20240101-000000-aaaaaaaa", task_title="rate limiting work",
                summary="did rate limiting")
        full = local_rag.retrieve("p", "rate limiting")
        assert full.ok
        kinds = {h.source.split(":")[0] for h in full.hits}
        assert {"memory", "run", "repo"} & kinds
        # kinds filter: memory only
        mem_only = local_rag.retrieve("p", "rate limiting", kinds=["memory"])
        assert all(h.source.startswith("memory:") for h in mem_only.hits)

    _run(body)


def test_retrieve_enforces_total_char_budget():
    def body(lay):
        # many fat sections so the budget trips
        big = "# S\n" + "".join(
            f"\n## Sec{i}\n{'rate limiting ' * 60}\n" for i in range(10)
        )
        lay.project("p", {"STATUS.md": big})
        res = local_rag.retrieve("p", "rate limiting", kinds=["memory"])
        assert res.ok
        total = sum(len(h.snippet) + len(h.title) + len(h.source) for h in res.hits)
        assert total <= local_rag.RAG_MAX_TOTAL_CHARS

    _run(body)


# ---------- inspect-channel tool ----------


def test_retrieve_is_a_valid_inspect_tool():
    def body(lay):
        lay.project("p", {"STATUS.md": "# S\n\n## Task Queue\n- [ ] rate limiting\n"})
        lay.workspace("p")
        req = parse_inspect_request('{"inspect_request": {"tool": "retrieve", "query": "rate limiting"}}')
        assert req is not None and req["tool"] == "retrieve"
        result = execute_inspect_request("p", req)
        assert result.ok
        assert result.kind == "retrieve"
        assert "LOCAL RETRIEVAL" in result.content

    _run(body)


# ---------- HTTP endpoint ----------


def test_retrieve_endpoint():
    def body(lay):
        lay.project("p", {"STATUS.md": "# S\n\n## Task Queue\n- [ ] rate limiting\n"})
        lay.workspace("p")
        client = TestClient(main.app)
        r = client.post("/api/projects/p/retrieve", json={"query": "rate limiting"})
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert any("rate limiting" in h["snippet"].lower() for h in data["hits"])
        # unknown project -> 404 from the guard
        assert client.post("/api/projects/nope/retrieve", json={"query": "x"}).status_code == 404

    _run(body)


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
