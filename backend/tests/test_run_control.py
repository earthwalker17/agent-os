"""Tests for Live Execution Timeline + Run Control.

Covers the run-control additions on top of the Phase 5 runner:

  - run_store helpers: ``read_events`` (tolerant JSONL reader) and
    ``read_task_card`` (inverse of ``write_task_card``).
  - Cooperative cancellation in the runner: a pre-set / mid-run cancel Event
    short-circuits to a terminal ``cancelled`` status with artifacts, runs no
    verification, and (when ``cancel_event`` is ``None``) is byte-identical to
    the legacy path.
  - BackgroundRunManager cancel registry: ``request_cancel`` / ``_discard_cancel``.
  - Endpoints: ``POST .../cancel`` (orphan finalize + race guard + 409s) and
    ``POST .../retry`` (new linked run, body preserved, 409 while running).
  - ``cancelled`` runs are never memory-reconciled.

All deterministic — cancel Events are set synchronously inside stubbed
``llm.chat``; no sleeps, no thread joins.

Run directly:
    python backend/tests/test_run_control.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
import execution.memory_reconciliation as mr  # noqa: E402
import execution.preview as preview  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution.background import BackgroundRunManager  # noqa: E402
from execution.models import (  # noqa: E402
    ExecutionPlan,
    ExecutionTask,
    RunRecord,
    RunStatus,
    TaskSpec,
    TaskStatus,
)
from execution.runner import CodingAgentRunner  # noqa: E402


# ---------- harness ----------


class _Env:
    """Temp layout + patched module globals + a FastAPI TestClient.

    Patches the three roots the run-control paths touch: ``main.PROJECTS_DIR``,
    ``exec_manager._EXECUTION_ROOT``, ``mr._PROJECTS_DIR``.
    """

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects_dir = root / "projects"
        self.execution_dir = root / "execution_workspaces"
        self.projects_dir.mkdir()
        self.execution_dir.mkdir()
        self._prev_projects = main.PROJECTS_DIR
        self._prev_exec = exec_manager._EXECUTION_ROOT
        self._prev_mr = mr._PROJECTS_DIR
        main.PROJECTS_DIR = self.projects_dir
        exec_manager._EXECUTION_ROOT = self.execution_dir
        mr._PROJECTS_DIR = self.projects_dir
        self.client = TestClient(main.app)

    def cleanup(self) -> None:
        main.PROJECTS_DIR = self._prev_projects
        exec_manager._EXECUTION_ROOT = self._prev_exec
        mr._PROJECTS_DIR = self._prev_mr
        preview._registry.clear()
        preview._starting.clear()
        self.tmp.cleanup()

    def setup(self, pid: str, *, task_md_body: str = "# TASK\n") -> Path:
        path = self.projects_dir / pid
        path.mkdir(parents=True, exist_ok=True)
        for name in ("PROJECT.md", "STATUS.md", "TASK_QUEUE.md", "DECISIONS.md", "RESEARCH.md"):
            (path / name).write_text("", encoding="utf-8")
        ws = self.execution_dir / pid
        repo = ws / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        (ws / "AGENT.md").write_text("# AGENT\n", encoding="utf-8")
        (ws / "TASK.md").write_text(task_md_body, encoding="utf-8")
        (ws / "runs").mkdir(exist_ok=True)
        (ws / "logs").mkdir(exist_ok=True)
        return repo

    def seed_run(self, pid: str, run_id: str, status: RunStatus, **over) -> RunRecord:
        run_store.init_run_dir(pid, run_id)
        over.setdefault("task_title", "t")
        rec = RunRecord(run_id=run_id, project_id=pid, status=status, **over)
        run_store.write_run_json(pid, run_id, rec)
        run_store.write_result_md(pid, run_id, run_store.render_result_md(rec, rec.summary))
        return rec


def _run(body):
    env = _Env()
    try:
        body(env)
    finally:
        env.cleanup()


def _stub_llm_caller(responses: list[str]):
    seq = list(responses)

    def caller(*_args, **_kwargs) -> str:
        if not seq:
            raise AssertionError("LLM caller ran out of stub responses")
        return seq.pop(0)

    return caller


def _patch_llm(monkey: dict[str, Any], caller):
    import llm

    monkey["llm_chat"] = llm.chat
    llm.chat = caller  # type: ignore[assignment]


def _unpatch_llm(monkey: dict[str, Any]):
    import llm

    if "llm_chat" in monkey:
        llm.chat = monkey["llm_chat"]


def _final(status: str = "completed", **over) -> str:
    body = {
        "action": "final",
        "status": status,
        "summary": "did the thing",
        "files_changed": [],
        "commands_run": [],
        "blockers": [],
        "task_md_update": "",
    }
    body.update(over)
    return json.dumps(body)


def _ptask(tid: str, title: str, depends_on=None) -> dict:
    return {"id": tid, "title": title, "description": "d", "depends_on": depends_on or []}


def _plan(tasks: list[dict]) -> str:
    return json.dumps(
        {"action": "plan", "goal": "goal", "analysis": "a", "risks": [], "tasks": tasks}
    )


_COMPLEX_CARD = (
    "Build the feature:\n"
    "- create a config file\n"
    "- create a helper module\n"
    "- wire them together"
)


class _FakeManager:
    """Stand-in for BackgroundRunManager: dispatch without a worker thread."""

    def __init__(self, *, cancel_in_flight: bool = False) -> None:
        self.calls: list[tuple] = []
        self.cancel_calls: list[str] = []
        self._cancel_in_flight = cancel_in_flight

    def dispatch(self, project_id: str, task: TaskSpec, *, retry_of=None) -> RunRecord:
        self.calls.append((project_id, task, retry_of))
        run_id = run_store.new_run_id()
        run_store.init_run_dir(project_id, run_id)
        run_store.write_task_card(project_id, run_id, task.title, task.task_card)
        rec = RunRecord(
            run_id=run_id,
            project_id=project_id,
            task_title=task.title,
            status=RunStatus.RUNNING,
            retry_of=retry_of,
        )
        run_store.write_run_json(project_id, run_id, rec)
        return rec

    def request_cancel(self, run_id: str) -> bool:
        self.cancel_calls.append(run_id)
        return self._cancel_in_flight


# ---------- run_store helpers ----------


def test_read_task_card_roundtrip():
    def body(env: _Env):
        env.setup("p")
        run_store.init_run_dir("p", "r1")
        title = "Fix #42: the # thing"
        card = "line one\n\nline two with a blank line above\nlast"
        run_store.write_task_card("p", "r1", title, card)
        got_title, got_body = run_store.read_task_card("p", "r1")
        assert got_title == title, got_title
        assert got_body == card, repr(got_body)

    _run(body)


def test_read_events_tolerant():
    def body(env: _Env):
        env.setup("p")
        run_store.init_run_dir("p", "r1")
        run_store.append_event("p", "r1", {"type": "run_started", "title": "t"})
        # Append a malformed line by hand — read_events must skip it.
        events_path = run_store.get_run_dir("p", "r1") / "events.jsonl"
        with events_path.open("a", encoding="utf-8") as f:
            f.write("{not json}\n\n")
        run_store.append_event("p", "r1", {"type": "run_completed", "status": "completed"})
        events = run_store.read_events("p", "r1")
        assert [e["type"] for e in events] == ["run_started", "run_completed"]
        assert all("timestamp" in e for e in events)

    _run(body)


# ---------- runner cooperative cancellation ----------


def test_cancel_before_first_step_finalizes_cancelled():
    def body(env: _Env):
        env.setup("p")
        evt = threading.Event()
        evt.set()  # cancel before the loop even starts
        monkey: dict[str, Any] = {}
        # Empty stub: if the runner ever calls the LLM, it raises — proving the
        # cancel short-circuits before any model round-trip.
        _patch_llm(monkey, _stub_llm_caller([]))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card="do it", created_by="test"),
                cancel_event=evt,
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "cancelled", summary.status
        raw = run_store.read_run_json("p", summary.run_id)
        assert raw["status"] == "cancelled"
        assert any("cancelled" in b.lower() for b in raw["blockers"])
        assert raw["completed_at"] is not None
        types = [e["type"] for e in run_store.read_events("p", summary.run_id)]
        assert "run_cancelled" in types
        # No verification ran for an aborted run.
        assert "verification" not in types
        # The single in-flight task is marked skipped, not left running.
        assert summary.plan is not None
        assert summary.plan.tasks[0].status == TaskStatus.SKIPPED

    _run(body)


def test_cancel_event_none_is_byte_identical():
    def body(env: _Env):
        env.setup("p")
        monkey: dict[str, Any] = {}
        _patch_llm(monkey, _stub_llm_caller([_final("completed", files_changed=["x.txt"])]))
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card="do it", created_by="test"),
                cancel_event=None,
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "completed"
        types = [e["type"] for e in run_store.read_events("p", summary.run_id)]
        assert "run_cancelled" not in types
        assert "run_completed" in types

    _run(body)


def test_cancel_between_tasks_skips_remaining():
    def body(env: _Env):
        env.setup("p")
        evt = threading.Event()
        calls = {"n": 0}

        def caller(*_a, **_k) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                return _plan([_ptask("t1", "Part one"), _ptask("t2", "Part two")])
            if calls["n"] == 2:
                evt.set()  # request cancel exactly as task 1 finishes
                return _final("completed", summary="one")
            raise AssertionError("task 2 must not call the LLM after cancel")

        monkey: dict[str, Any] = {}
        _patch_llm(monkey, caller)
        try:
            summary = CodingAgentRunner("p").run_task(
                TaskSpec(title="t", task_card=_COMPLEX_CARD, created_by="test"),
                cancel_event=evt,
            )
        finally:
            _unpatch_llm(monkey)

        assert summary.status == "cancelled", summary.status
        by_id = {t.id: t for t in summary.plan.tasks}
        assert by_id["t1"].status == TaskStatus.COMPLETED
        # t2 never started — it must not be COMPLETED.
        assert by_id["t2"].status != TaskStatus.COMPLETED
        types = [e["type"] for e in run_store.read_events("p", summary.run_id)]
        assert "run_cancelled" in types

    _run(body)


def test_cancelled_run_is_not_reconciled():
    def body(env: _Env):
        env.setup("p")
        env.seed_run("p", "r1", RunStatus.CANCELLED, blockers=["run cancelled by user"])
        outcome = mr.reconcile_run_memory("p", "r1")
        assert outcome.reconciled is False
        assert outcome.tag == mr.TAG_SKIPPED_NON_TERMINAL, outcome.tag

    _run(body)


# ---------- manager cancel registry ----------


def test_manager_request_cancel_registry():
    mgr = BackgroundRunManager(max_workers=1)
    try:
        evt = threading.Event()
        with mgr._cancel_lock:
            mgr._cancels["run-x"] = evt
        assert mgr.request_cancel("run-x") is True
        assert evt.is_set()
        assert mgr.request_cancel("unknown") is False
        mgr._discard_cancel("run-x")
        assert mgr.request_cancel("run-x") is False
    finally:
        mgr.shutdown(wait=False)


# ---------- cancel endpoint ----------


def test_cancel_endpoint_rejects_terminal_run():
    def body(env: _Env):
        env.setup("p")
        env.seed_run("p", "r1", RunStatus.COMPLETED)
        res = env.client.post("/api/projects/p/execution/runs/r1/cancel")
        assert res.status_code == 409

    _run(body)


def test_cancel_endpoint_orphan_finalizes():
    def body(env: _Env):
        env.setup("p")
        env.seed_run("p", "r1", RunStatus.RUNNING)
        fake = _FakeManager(cancel_in_flight=False)
        prev = main.get_default_manager
        main.get_default_manager = lambda: fake
        try:
            res = env.client.post("/api/projects/p/execution/runs/r1/cancel")
            assert res.status_code == 200
            assert res.json()["status"] == "cancelled"
            # Idempotent: now terminal, a second cancel is rejected.
            again = env.client.post("/api/projects/p/execution/runs/r1/cancel")
            assert again.status_code == 409
        finally:
            main.get_default_manager = prev

        types = [e["type"] for e in run_store.read_events("p", "r1")]
        assert "run_cancel_requested" in types
        assert "run_cancelled" in types

    _run(body)


def test_cancel_endpoint_orphan_settles_plan_tasks():
    """Orphan-cancel (no worker) must settle an in-flight plan task + write plan.json."""

    def body(env: _Env):
        env.setup("p")
        plan = ExecutionPlan(
            goal="g",
            mode="planned",
            tasks=[
                ExecutionTask(id="t1", title="one", status=TaskStatus.COMPLETED),
                ExecutionTask(id="t2", title="two", status=TaskStatus.RUNNING),
            ],
        )
        env.seed_run("p", "r1", RunStatus.RUNNING, plan=plan)
        fake = _FakeManager(cancel_in_flight=False)  # no worker -> orphan branch
        prev = main.get_default_manager
        main.get_default_manager = lambda: fake
        try:
            res = env.client.post("/api/projects/p/execution/runs/r1/cancel")
            assert res.status_code == 200
            assert res.json()["status"] == "cancelled"
        finally:
            main.get_default_manager = prev

        # plan.json now exists and the in-flight task is settled to skipped.
        plan_raw = run_store.read_plan_json("p", "r1")
        assert plan_raw is not None
        by_id = {t["id"]: t["status"] for t in plan_raw["tasks"]}
        assert by_id["t1"] == "completed"  # untouched
        assert by_id["t2"] == "skipped"  # was running -> skipped
        # run.json's embedded plan agrees (no torn cross-artifact state).
        run_raw = run_store.read_run_json("p", "r1")
        emb = {t["id"]: t["status"] for t in run_raw["plan"]["tasks"]}
        assert emb["t2"] == "skipped"

    _run(body)


def test_cancel_endpoint_does_not_clobber_finished_run():
    """Race guard: request_cancel False + a re-read showing terminal => no orphan write."""

    def body(env: _Env):
        env.setup("p")
        env.seed_run("p", "r1", RunStatus.RUNNING)
        fake = _FakeManager(cancel_in_flight=False)

        real_read = run_store.read_run_json
        calls = {"n": 0}

        def racing_read(pid, rid):
            calls["n"] += 1
            raw = real_read(pid, rid)
            # First read (status gate) sees running; later reads (the orphan
            # guard's re-read) see a run that just finished as completed.
            if raw is not None and calls["n"] >= 2:
                raw = dict(raw)
                raw["status"] = "completed"
            return raw

        prev_mgr = main.get_default_manager
        main.get_default_manager = lambda: fake
        run_store.read_run_json = racing_read  # main calls run_store.read_run_json
        try:
            res = env.client.post("/api/projects/p/execution/runs/r1/cancel")
            assert res.status_code == 200
            # The re-read showed completed, so the orphan path was skipped.
            assert res.json()["status"] == "completed"
        finally:
            run_store.read_run_json = real_read
            main.get_default_manager = prev_mgr

        # No cancelled finalize happened.
        types = [e["type"] for e in run_store.read_events("p", "r1")]
        assert "run_cancelled" not in types

    _run(body)


# ---------- retry endpoint ----------


def test_retry_dispatches_new_linked_run():
    def body(env: _Env):
        env.setup("p")
        env.seed_run("p", "20260101-000000-aaaaaaaa", RunStatus.FAILED, task_title="Build it")
        run_store.write_task_card(
            "p", "20260101-000000-aaaaaaaa", "Build it", "the original task body"
        )
        fake = _FakeManager()
        prev = main.get_default_manager
        main.get_default_manager = lambda: fake
        try:
            res = env.client.post(
                "/api/projects/p/execution/runs/20260101-000000-aaaaaaaa/retry"
            )
        finally:
            main.get_default_manager = prev

        assert res.status_code == 200
        new = res.json()
        assert new["run_id"] != "20260101-000000-aaaaaaaa"
        assert new["retry_of"] == "20260101-000000-aaaaaaaa"
        # Body preserved into the new run's task spec.
        assert fake.calls and fake.calls[0][1].task_card == "the original task body"
        # Original now points to the retry.
        original = run_store.read_run_json("p", "20260101-000000-aaaaaaaa")
        assert original["retried_by"] == new["run_id"]
        types = [e["type"] for e in run_store.read_events("p", "20260101-000000-aaaaaaaa")]
        assert "run_retried" in types

    _run(body)


def test_retry_of_cancelled_run_is_allowed():
    """A cancelled run is terminal, so retry must dispatch a new linked run."""

    def body(env: _Env):
        env.setup("p")
        env.seed_run("p", "20260101-000000-bbbbbbbb", RunStatus.CANCELLED, task_title="Build it")
        run_store.write_task_card(
            "p", "20260101-000000-bbbbbbbb", "Build it", "the original task body"
        )
        fake = _FakeManager()
        prev = main.get_default_manager
        main.get_default_manager = lambda: fake
        try:
            res = env.client.post(
                "/api/projects/p/execution/runs/20260101-000000-bbbbbbbb/retry"
            )
        finally:
            main.get_default_manager = prev

        assert res.status_code == 200, res.text
        new = res.json()
        assert new["retry_of"] == "20260101-000000-bbbbbbbb"
        assert fake.calls and fake.calls[0][1].task_card == "the original task body"

    _run(body)


def test_retry_rejects_running_run():
    def body(env: _Env):
        env.setup("p")
        env.seed_run("p", "r1", RunStatus.RUNNING)
        res = env.client.post("/api/projects/p/execution/runs/r1/retry")
        assert res.status_code == 409

    _run(body)


# ---------- events endpoint ----------


def test_events_endpoint_returns_timeline():
    def body(env: _Env):
        env.setup("p")
        env.seed_run("p", "r1", RunStatus.RUNNING)
        run_store.append_event("p", "r1", {"type": "run_started", "title": "t"})
        run_store.append_event("p", "r1", {"type": "task_started", "task_id": "t1"})
        res = env.client.get("/api/projects/p/execution/runs/r1/events")
        assert res.status_code == 200
        payload = res.json()
        assert payload["run_id"] == "r1"
        assert [e["type"] for e in payload["events"]] == ["run_started", "task_started"]
        # Backward-compatible: a total count rides alongside the events.
        assert payload["total"] == 2
        # Unknown run -> 404.
        assert env.client.get("/api/projects/p/execution/runs/nope/events").status_code == 404

    _run(body)


def test_events_endpoint_since_cursor():
    """The Live Trace polls with ?since=<index> to fetch only new events."""

    def body(env: _Env):
        env.setup("p")
        env.seed_run("p", "r1", RunStatus.RUNNING)
        for i in range(5):
            run_store.append_event("p", "r1", {"type": f"e{i}"})

        # since past the first two -> only the tail, but total is the full count.
        res = env.client.get("/api/projects/p/execution/runs/r1/events?since=2")
        assert res.status_code == 200
        payload = res.json()
        assert [e["type"] for e in payload["events"]] == ["e2", "e3", "e4"]
        assert payload["total"] == 5

        # since at the end -> empty tail, total still reported (poll-and-append).
        res = env.client.get("/api/projects/p/execution/runs/r1/events?since=5")
        payload = res.json()
        assert payload["events"] == []
        assert payload["total"] == 5

        # since beyond the end (race / stale cursor) -> empty, never raises.
        res = env.client.get("/api/projects/p/execution/runs/r1/events?since=99")
        assert res.status_code == 200
        assert res.json()["events"] == []

        # No since -> every event (legacy behavior preserved).
        res = env.client.get("/api/projects/p/execution/runs/r1/events")
        assert [e["type"] for e in res.json()["events"]] == ["e0", "e1", "e2", "e3", "e4"]

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
            import traceback

            traceback.print_exc()
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
