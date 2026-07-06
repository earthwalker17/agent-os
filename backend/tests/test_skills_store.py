"""Tests for Phase 10 — skill storage + prompt folding (skills_store.py).

Also holds the registry↔disk drift test: every SkillRef in agents_registry
must resolve to a committed file under skills/.

Run:  python backend/tests/test_skills_store.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import agents_registry  # noqa: E402
import skills_store  # noqa: E402
from agents_registry import STATUS_ACTIVE, SkillRef  # noqa: E402


def _with_temp_skills_dir(fn):
    """Run ``fn(tmp_path)`` with skills_store.SKILLS_DIR pointed at a tempdir."""
    old = skills_store.SKILLS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        skills_store.SKILLS_DIR = Path(tmp)
        try:
            fn(Path(tmp))
        finally:
            skills_store.SKILLS_DIR = old


def _seed(tmp: Path, agent_id: str, skill_id: str, content: str) -> None:
    path = tmp / agent_id / f"{skill_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------- read / write ----------


def test_read_registered_skill_returns_content():
    def body(tmp: Path):
        _seed(tmp, "planner", "task-breakdown-checklist", "# T\n> d\n\nsteps")
        assert skills_store.read_skill("planner", "task-breakdown-checklist") == (
            "# T\n> d\n\nsteps"
        )

    _with_temp_skills_dir(body)


def test_read_missing_file_returns_empty_not_error():
    def body(tmp: Path):
        assert skills_store.read_skill("planner", "plan-quality-rubric") == ""

    _with_temp_skills_dir(body)


def test_unknown_pair_rejected_on_read_and_write():
    def body(tmp: Path):
        for fn, args in (
            (skills_store.read_skill, ("planner", "nope")),
            (skills_store.read_skill, ("nope", "task-breakdown-checklist")),
            # a real skill id, but owned by another agent
            (skills_store.read_skill, ("planner", "code-review-rubric")),
            (skills_store.write_skill, ("planner", "nope", "content")),
            (skills_store.write_skill, ("planner", "../../etc/passwd", "x")),
        ):
            try:
                fn(*args)
                raise RuntimeError(f"expected ValueError for {args}")
            except ValueError:
                pass

    _with_temp_skills_dir(body)


def test_write_roundtrip_is_atomic_and_creates_dirs():
    def body(tmp: Path):
        skills_store.write_skill("reviewer", "code-review-rubric", "# R\n> d\n\nnew body")
        assert skills_store.read_skill("reviewer", "code-review-rubric") == (
            "# R\n> d\n\nnew body"
        )
        # no stray temp siblings left behind
        leftovers = [p for p in (tmp / "reviewer").iterdir() if p.suffix == ".tmp"]
        assert leftovers == []

    _with_temp_skills_dir(body)


def test_write_rejects_empty_and_oversize():
    def body(tmp: Path):
        for bad in ("", "   \n", None):
            try:
                skills_store.write_skill("planner", "plan-quality-rubric", bad)  # type: ignore[arg-type]
                raise RuntimeError("expected ValueError for empty content")
            except ValueError:
                pass
        try:
            skills_store.write_skill(
                "planner", "plan-quality-rubric", "x" * (skills_store.MAX_SKILL_CHARS + 1)
            )
            raise RuntimeError("expected ValueError for oversize content")
        except ValueError:
            pass

    _with_temp_skills_dir(body)


def test_malicious_registry_ref_still_rejected_by_slug_guard():
    """Defense in depth: even if a SkillRef somehow carried a path-shaped id,
    the slug re-check refuses to build a path from it."""

    def body(tmp: Path):
        real = agents_registry.skill_ref
        agents_registry.skill_ref = lambda a, s: SkillRef(  # type: ignore[assignment]
            id="../evil", title="t", description="d"
        )
        try:
            try:
                skills_store.read_skill("planner", "anything")
                raise RuntimeError("expected ValueError for path-shaped skill id")
            except ValueError:
                pass
        finally:
            agents_registry.skill_ref = real

    _with_temp_skills_dir(body)


# ---------- prompt folding ----------


def test_prompt_block_includes_titles_and_bodies():
    def body(tmp: Path):
        _seed(tmp, "planner", "task-breakdown-checklist", "- split by seams")
        _seed(tmp, "planner", "plan-quality-rubric", "- has risks section")
        block = skills_store.skills_prompt_block("plan")
        assert "### Skill: Task Breakdown Checklist" in block
        assert "### Skill: Plan Quality Rubric" in block
        assert "- split by seams" in block
        assert "- has risks section" in block

    _with_temp_skills_dir(body)


def test_prompt_block_caps_per_skill_and_total():
    def body(tmp: Path):
        _seed(tmp, "planner", "task-breakdown-checklist", "A" * 5000)
        _seed(tmp, "planner", "plan-quality-rubric", "B" * 5000)
        block = skills_store.skills_prompt_block("plan")
        # generous margin over the cap for headings + truncation notes
        assert len(block) <= skills_store.SKILL_PROMPT_CAP + 100
        assert "…(truncated)" in block
        # the second skill still gets space (per-skill cap, not first-wins)
        assert "B" in block

    _with_temp_skills_dir(body)


def test_prompt_block_empty_for_modeless_unknown_and_missing():
    def body(tmp: Path):
        assert skills_store.skills_prompt_block(None) == ""
        assert skills_store.skills_prompt_block("") == ""
        assert skills_store.skills_prompt_block("no-such-mode") == ""
        # active mode, but nothing on disk
        assert skills_store.skills_prompt_block("plan") == ""

    _with_temp_skills_dir(body)


def test_prompt_block_never_raises():
    def body(tmp: Path):
        real = skills_store.read_skill
        skills_store.read_skill = lambda a, s: (_ for _ in ()).throw(OSError("disk"))  # type: ignore[assignment]
        try:
            assert skills_store.skills_prompt_block("plan") == ""
        finally:
            skills_store.read_skill = real

    _with_temp_skills_dir(body)


def test_orchestrator_mode_section_folds_skills():
    def body(tmp: Path):
        _seed(tmp, "planner", "task-breakdown-checklist", "- split by seams")
        import orchestrator

        ctx = orchestrator.MemoryContext(
            user="", workstyle="", soul="", global_memory="",
            project="", status="", decisions="", research="", lessons="",
            project_name="X", project_id="proj-x",
        )
        section = orchestrator._mode_guidance_section(ctx, "plan")
        assert "### Skill: Task Breakdown Checklist" in section
        # skills never appear without a mode
        assert orchestrator._mode_guidance_section(ctx, None) == ""

    _with_temp_skills_dir(body)


# ---------- registry <-> disk drift ----------


def test_every_registered_skill_file_is_committed():
    for agent in agents_registry.AGENTS:
        if agent.status != STATUS_ACTIVE:
            continue
        for ref in agent.skills:
            path = skills_store.SKILLS_DIR / agent.id / f"{ref.id}.md"
            assert path.is_file(), f"missing skill file: {path}"
            text = path.read_text(encoding="utf-8")
            assert text.startswith(f"# {ref.title}"), path
            assert len(text) <= skills_store.MAX_SKILL_CHARS, path


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
