"""Tests for Phase 10.2 — the suggested skill patch flow (execution/skill_patch.py).

The LLM is stubbed, skills live in a tempdir copy, and runs live in a temp
workspace. Covers: green-only gating, proposal build (append-style), unknown
target rejection, idempotency, never-raises, apply (through skills_store) with
and without an edit, reject, and the HTTP endpoints.

Run:  python backend/tests/test_skill_patch.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import skills_store  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution import skill_patch  # noqa: E402
from execution.models import RunRecord, RunStatus  # noqa: E402


class _Env:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.execution_dir = root / "execution_workspaces"
        self.skills_dir = root / "skills"
        self.projects_dir = root / "projects"
        for d in (self.execution_dir, self.skills_dir, self.projects_dir):
            d.mkdir()
        self._restore = [
            (exec_manager, "_EXECUTION_ROOT", exec_manager._EXECUTION_ROOT),
            (skills_store, "SKILLS_DIR", skills_store.SKILLS_DIR),
            (main, "PROJECTS_DIR", main.PROJECTS_DIR),
        ]
        exec_manager._EXECUTION_ROOT = self.execution_dir
        skills_store.SKILLS_DIR = self.skills_dir
        main.PROJECTS_DIR = self.projects_dir

    def workspace(self, pid="p"):
        ws = self.execution_dir / pid
        (ws / "repo").mkdir(parents=True, exist_ok=True)
        (ws / "runs").mkdir(exist_ok=True)
        (ws / "logs").mkdir(exist_ok=True)
        (self.projects_dir / pid).mkdir(parents=True, exist_ok=True)

    def run(self, pid, run_id, status=RunStatus.COMPLETED, **fields):
        rec = RunRecord(run_id=run_id, project_id=pid,
                        task_title=fields.pop("task_title", "build feature"),
                        status=status, **fields)
        run_store.init_run_dir(pid, run_id)
        run_store.write_run_json(pid, run_id, rec)

    def seed_skill(self, agent_id, skill_id, content):
        p = self.skills_dir / agent_id / f"{skill_id}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def cleanup(self):
        for obj, attr, val in self._restore:
            setattr(obj, attr, val)
        self.tmp.cleanup()


def _run(body):
    env = _Env()
    try:
        body(env)
    finally:
        env.cleanup()


def _caller(payload):
    def caller(system, messages, **kw):
        return json.dumps(payload) if isinstance(payload, dict) else payload
    return caller


_GOOD = {
    "should_patch": True,
    "target_agent_id": "coder",
    "target_skill_id": "definition-of-done-checklist",
    "rationale": "verifying the build before finalizing catches phantom output",
    "evidence": "this run's build failed until deps were installed",
    "addition": "- [ ] Run the real build/verify before claiming done.",
}


# ---------- propose ----------


def test_skips_non_green_runs():
    def body(env):
        env.workspace()
        env.run("p", "r1", status=RunStatus.PARTIAL)
        out = skill_patch.propose_skill_patch("p", "r1", llm_caller=_caller(_GOOD))
        assert out is None
        rec = RunRecord(**run_store.read_run_json("p", "r1"))
        assert rec.skill_patch is None  # never even called the judge

    _run(body)


def test_builds_append_style_proposal():
    def body(env):
        env.workspace()
        env.seed_skill("coder", "definition-of-done-checklist",
                       "# Definition-of-Done Checklist\n> when a task is done\n\n- [ ] tests pass")
        env.run("p", "r1", files_changed=["app.py"], summary="built the thing")
        out = skill_patch.propose_skill_patch("p", "r1", llm_caller=_caller(_GOOD))
        assert out is not None and out.proposed is True
        assert out.target_agent_id == "coder"
        assert out.target_skill_id == "definition-of-done-checklist"
        # append-style: existing content preserved, addition appended
        assert out.current_content.startswith("# Definition-of-Done Checklist")
        assert "- [ ] tests pass" in out.proposed_content
        assert "Run the real build/verify" in out.proposed_content
        assert out.status == "proposed"
        # persisted on run.json
        rec = RunRecord(**run_store.read_run_json("p", "r1"))
        assert rec.skill_patch.proposed is True

    _run(body)


def test_no_patch_records_quiet_no_proposal():
    def body(env):
        env.workspace()
        # substantive run (files changed) so the judge path is reached
        env.run("p", "r1", files_changed=["a.py"], summary="did a thing worth judging")
        out = skill_patch.propose_skill_patch(
            "p", "r1", llm_caller=_caller({"should_patch": False})
        )
        assert out is not None and out.proposed is False
        # recorded so we don't re-judge
        rec = RunRecord(**run_store.read_run_json("p", "r1"))
        assert rec.skill_patch is not None and rec.skill_patch.proposed is False

    _run(body)


def test_trivial_run_skipped_before_llm():
    def body(env):
        env.workspace()
        env.run("p", "r1", summary="ok")  # no files, short summary

        def boom(system, messages, **kw):
            raise AssertionError("LLM must not be called for a trivial run")

        out = skill_patch.propose_skill_patch("p", "r1", llm_caller=boom)
        assert out is None  # skipped before the judge
        assert RunRecord(**run_store.read_run_json("p", "r1")).skill_patch is None

    _run(body)


def test_unknown_target_skill_is_rejected():
    def body(env):
        env.workspace()
        env.run("p", "r1", files_changed=["app.py"], summary="did work")
        bad = dict(_GOOD, target_skill_id="not-a-real-skill")
        out = skill_patch.propose_skill_patch("p", "r1", llm_caller=_caller(bad))
        assert out.proposed is False
        assert "unknown skill" in (out.error or "")

    _run(body)


def test_agent_qualified_skill_id_is_normalized():
    """Pre-launch E2E regression: the judge returned the skill id
    agent-qualified ("coder/definition-of-done-checklist"), and the raw
    registry lookup rejected a legitimate proposal."""

    def body(env):
        env.workspace()
        env.seed_skill("coder", "definition-of-done-checklist",
                       "# Definition-of-Done Checklist\n> when a task is done\n\n- [ ] tests pass")
        env.run("p", "r1", files_changed=["app.py"], summary="did work")
        for qualified in ("coder/definition-of-done-checklist",
                          "skills/coder/definition-of-done-checklist"):
            env.run("p", f"r-{hash(qualified) & 0xffff}", files_changed=["app.py"],
                    summary="did work")
            payload = dict(_GOOD, target_skill_id=qualified)
            out = skill_patch.propose_skill_patch(
                "p", f"r-{hash(qualified) & 0xffff}", llm_caller=_caller(payload)
            )
            assert out is not None and out.proposed is True, (qualified, out.error)
            assert out.target_skill_id == "definition-of-done-checklist"
        # a genuinely foreign prefix is still rejected
        env.run("p", "r-foreign", files_changed=["app.py"], summary="did work")
        bad = dict(_GOOD, target_skill_id="reviewer/code-review-rubric")
        out = skill_patch.propose_skill_patch("p", "r-foreign", llm_caller=_caller(bad))
        assert out.proposed is False and "unknown skill" in (out.error or "")

    _run(body)


def test_idempotent_once_per_run():
    def body(env):
        env.workspace()
        env.seed_skill("coder", "definition-of-done-checklist", "# X\n> y\n\n- [ ] a")
        env.run("p", "r1", files_changed=["app.py"], summary="did work")
        first = skill_patch.propose_skill_patch("p", "r1", llm_caller=_caller(_GOOD))
        # a second call with a DIFFERENT payload must not re-judge
        second = skill_patch.propose_skill_patch(
            "p", "r1", llm_caller=_caller({"should_patch": False})
        )
        assert second.proposed == first.proposed is True

    _run(body)


def test_never_raises_on_bad_llm():
    def body(env):
        env.workspace()
        env.run("p", "r1", files_changed=["app.py"], summary="did work")

        def boom(system, messages, **kw):
            raise RuntimeError("llm down")

        out = skill_patch.propose_skill_patch("p", "r1", llm_caller=boom)
        assert out is not None and out.proposed is False
        assert "RuntimeError" in (out.error or "")

    _run(body)


# ---------- apply / reject ----------


def test_apply_writes_through_skills_store():
    def body(env):
        env.workspace()
        env.seed_skill("coder", "definition-of-done-checklist", "# X\n> y\n\n- [ ] a")
        env.run("p", "r1", files_changed=["app.py"], summary="did work")
        skill_patch.propose_skill_patch("p", "r1", llm_caller=_caller(_GOOD))
        updated = skill_patch.apply_skill_patch("p", "r1")
        assert updated.status == "applied"
        # the skill file now contains the addition
        content = skills_store.read_skill("coder", "definition-of-done-checklist")
        assert "Run the real build/verify" in content
        # re-apply is rejected
        try:
            skill_patch.apply_skill_patch("p", "r1")
            raise AssertionError("expected ValueError on re-apply")
        except ValueError:
            pass

    _run(body)


def test_apply_rebases_onto_live_content_preserving_intervening_edits():
    # A skill edited AFTER the proposal (via another Apply or the Agents editor)
    # must not be clobbered by the stale propose-time snapshot.
    def body(env):
        env.workspace()
        env.seed_skill("coder", "definition-of-done-checklist", "# X\n> y\n\n- [ ] a")
        env.run("p", "r1", files_changed=["app.py"], summary="did work")
        skill_patch.propose_skill_patch("p", "r1", llm_caller=_caller(_GOOD))
        # someone edits the skill in the meantime
        skills_store.write_skill("coder", "definition-of-done-checklist",
                                 "# X\n> y\n\n- [ ] a\n- [ ] INTERVENING EDIT")
        skill_patch.apply_skill_patch("p", "r1")  # un-edited Apply -> rebased
        final = skills_store.read_skill("coder", "definition-of-done-checklist")
        assert "INTERVENING EDIT" in final           # not clobbered
        assert "Run the real build/verify" in final  # addition still applied

    _run(body)


def test_apply_honors_user_edit():
    def body(env):
        env.workspace()
        env.seed_skill("coder", "definition-of-done-checklist", "# X\n> y\n\n- [ ] a")
        env.run("p", "r1", files_changed=["app.py"], summary="did work")
        skill_patch.propose_skill_patch("p", "r1", llm_caller=_caller(_GOOD))
        edited = "# X\n> y\n\n- [ ] a\n- [ ] user edited this line"
        skill_patch.apply_skill_patch("p", "r1", content_override=edited)
        assert skills_store.read_skill("coder", "definition-of-done-checklist") == edited

    _run(body)


def test_apply_without_proposal_errors():
    def body(env):
        env.workspace()
        env.run("p", "r1", files_changed=["app.py"], summary="did work")  # never proposed
        try:
            skill_patch.apply_skill_patch("p", "r1")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    _run(body)


def test_reject_marks_rejected_and_writes_nothing():
    def body(env):
        env.workspace()
        env.seed_skill("coder", "definition-of-done-checklist", "# X\n> y\n\n- [ ] a")
        env.run("p", "r1", files_changed=["app.py"], summary="did work")
        skill_patch.propose_skill_patch("p", "r1", llm_caller=_caller(_GOOD))
        updated = skill_patch.reject_skill_patch("p", "r1")
        assert updated.status == "rejected"
        # skill file untouched
        assert skills_store.read_skill("coder", "definition-of-done-checklist") == "# X\n> y\n\n- [ ] a"

    _run(body)


# ---------- endpoints ----------


def test_apply_reject_endpoints():
    def body(env):
        env.workspace()
        env.seed_skill("coder", "definition-of-done-checklist", "# X\n> y\n\n- [ ] a")
        env.run("p", "r1", files_changed=["app.py"], summary="did work")
        skill_patch.propose_skill_patch("p", "r1", llm_caller=_caller(_GOOD))
        client = TestClient(main.app)
        r = client.post("/api/projects/p/execution/runs/r1/skill-patch/apply", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "applied"
        # reject after apply -> the proposal is applied, reject still flips status
        # (endpoint is permissive); a run with no proposal -> 400
        env.run("p", "r2", summary="x")
        r2 = client.post("/api/projects/p/execution/runs/r2/skill-patch/reject", json={})
        assert r2.status_code == 400

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
