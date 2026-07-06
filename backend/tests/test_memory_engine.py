"""Tests for memory_engine — the shared markdown memory write core (Phase 6).

Standalone script, run directly:  python tests/test_memory_engine.py

memory_engine.apply_update takes ``base_dir`` explicitly, so these tests need no
module-root patching — just a tempfile.TemporaryDirectory(). The orchestrator
wrapper tests patch orchestrator.PROJECTS_DIR / MEMORY_DIR before exercising the
public apply_memory_update / apply_global_memory_update wrappers (the chat-turn
write path, previously untested).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import memory_engine
from memory_engine import (
    WRITABLE_PROJECT,
    WRITABLE_GLOBAL,
    RECONCILIATION_WRITABLE,
    apply_update,
    ensure_memory_scaffold,
)


# ---------- apply_update: policy ----------

def test_rejects_file_not_in_allow():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        ok = apply_update(
            base, allow=WRITABLE_PROJECT, filename="EVIL.md",
            section="X", content="boom", action="append",
        )
        assert ok is False
        assert not (base / "EVIL.md").exists()


def test_rejects_soul_md_everywhere():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        # SOUL.md is in none of the allow-lists.
        for allow in (WRITABLE_PROJECT, WRITABLE_GLOBAL, RECONCILIATION_WRITABLE):
            assert apply_update(
                base, allow=allow, filename="SOUL.md",
                section="Identity", content="hacked", action="replace",
            ) is False
        assert not (base / "SOUL.md").exists()


def test_reconciliation_allow_excludes_project_md():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        assert apply_update(
            base, allow=RECONCILIATION_WRITABLE, filename="PROJECT.md",
            section="Vision", content="nope", action="replace",
        ) is False


def test_missing_base_dir_returns_false():
    base = Path(tempfile.gettempdir()) / "agentos-does-not-exist-xyz"
    assert apply_update(
        base, allow=WRITABLE_PROJECT, filename="STATUS.md",
        section="What Works", content="x", action="append",
    ) is False


# ---------- apply_update: append ----------

def test_append_creates_and_writes():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        ok = apply_update(
            base, allow=WRITABLE_PROJECT, filename="DECISIONS.md",
            section="Decisions", content="- Chose SQLite", action="append",
        )
        assert ok is True
        text = (base / "DECISIONS.md").read_text(encoding="utf-8")
        assert "- Chose SQLite" in text
        assert text.endswith("\n")


def test_append_dedup_returns_false():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "RESEARCH.md").write_text("# Research\n\n## Findings\n- A known fact\n", encoding="utf-8")
        # Exact body already present -> skip the write.
        ok = apply_update(
            base, allow=WRITABLE_PROJECT, filename="RESEARCH.md",
            section="Findings", content="- A known fact", action="append",
        )
        assert ok is False


def test_append_new_short_entry_that_is_a_substring_still_writes():
    """T2.5: a distinct new entry must NOT be dropped just because its text is a
    substring of existing content (the old raw-substring dedup lost writes)."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "STATUS.md").write_text(
            "# Status\n\n## Task Queue\n- Add /healthcheck endpoint with a Pydantic model\n",
            encoding="utf-8",
        )
        # "- [x] Add /healthcheck" is a substring of the existing line but is a
        # genuinely new, distinct entry — it must be appended, not rejected.
        ok = apply_update(
            base, allow=WRITABLE_PROJECT, filename="STATUS.md",
            section="Task Queue", content="- [x] Add /healthcheck", action="append",
        )
        assert ok is True
        text = (base / "STATUS.md").read_text(encoding="utf-8")
        assert "- [x] Add /healthcheck" in text
        # An EXACT repeat of that same line is still deduped.
        ok2 = apply_update(
            base, allow=WRITABLE_PROJECT, filename="STATUS.md",
            section="Task Queue", content="- [x] Add /healthcheck", action="append",
        )
        assert ok2 is False


# ---------- apply_update: replace ----------


def test_replace_does_not_clobber_prefix_overlapping_sibling_section():
    """T2.5: targeting 'Decisions' must not overwrite a '## Decisions Archive'
    sibling when the exact '## Decisions' heading is absent — it should create
    the exact heading instead of latching onto the prefix match."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "DECISIONS.md").write_text(
            "# Decisions\n\n## Decisions Archive\n- historical choice that must survive\n",
            encoding="utf-8",
        )
        ok = apply_update(
            base, allow=WRITABLE_PROJECT, filename="DECISIONS.md",
            section="Decisions", content="- new decision", action="replace",
        )
        assert ok is True
        text = (base / "DECISIONS.md").read_text(encoding="utf-8")
        # The archive heading + its body survived intact.
        assert "## Decisions Archive" in text
        assert "- historical choice that must survive" in text
        # A distinct exact '## Decisions' heading was created with the new body.
        assert "## Decisions\n- new decision" in text




def test_replace_existing_section_overwrites_body():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "STATUS.md").write_text(
            "# Status\n\n## Current Phase\nPlanning\n\n## What Works\n- old\n",
            encoding="utf-8",
        )
        ok = apply_update(
            base, allow=WRITABLE_PROJECT, filename="STATUS.md",
            section="Current Phase", content="Implementation", action="replace",
        )
        assert ok is True
        text = (base / "STATUS.md").read_text(encoding="utf-8")
        assert "Implementation" in text
        assert "Planning" not in text
        # Other sections untouched.
        assert "## What Works" in text and "- old" in text


def test_replace_missing_section_creates_heading():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "STATUS.md").write_text("# Status\n\n## Current Phase\nPlanning\n", encoding="utf-8")
        ok = apply_update(
            base, allow=WRITABLE_PROJECT, filename="STATUS.md",
            section="Next Up", content="- ship it", action="replace",
        )
        assert ok is True
        text = (base / "STATUS.md").read_text(encoding="utf-8")
        assert "## Next Up" in text and "- ship it" in text


def test_atomic_write_leaves_no_tmp_sibling():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        apply_update(
            base, allow=WRITABLE_PROJECT, filename="STATUS.md",
            section="What Works", content="- done", action="append",
        )
        # The temp sibling used by the atomic write must be gone.
        assert not (base / "STATUS.md.tmp").exists()
        assert (base / "STATUS.md").exists()


# ---------- ensure_memory_scaffold ----------

def test_scaffold_creates_all_files_with_canonical_sections():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "proj"
        touched = ensure_memory_scaffold(base, "My Project")
        assert set(touched) == set(memory_engine.CANONICAL_SECTIONS.keys())
        status = (base / "STATUS.md").read_text(encoding="utf-8")
        for section in memory_engine.CANONICAL_SECTIONS["STATUS.md"]:
            assert f"## {section}" in status
        assert status.startswith("# Status: My Project")


def test_scaffold_is_idempotent_and_additive():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "proj"
        base.mkdir(parents=True)
        # A partial STATUS.md missing some canonical sections + custom content.
        (base / "STATUS.md").write_text(
            "# Status: Existing\n\n## Current Phase\nDeep in it\n", encoding="utf-8"
        )
        first = ensure_memory_scaffold(base, "Existing")
        assert "STATUS.md" in first
        status = (base / "STATUS.md").read_text(encoding="utf-8")
        # Existing content preserved...
        assert "Deep in it" in status
        # ...missing sections backfilled.
        assert "## What Works" in status and "## Next Up" in status
        # Second call is a no-op for already-complete files.
        second = ensure_memory_scaffold(base, "Existing")
        assert "STATUS.md" not in second


# ---------- Phase 10.2 — TASK_QUEUE -> STATUS merge + LESSONS.md ----------

def test_task_queue_no_longer_a_writable_file_or_canonical():
    assert "TASK_QUEUE.md" not in memory_engine.WRITABLE_PROJECT
    assert "TASK_QUEUE.md" not in memory_engine.RECONCILIATION_WRITABLE
    assert "TASK_QUEUE.md" not in memory_engine.CANONICAL_SECTIONS
    assert "Task Queue" in memory_engine.CANONICAL_SECTIONS["STATUS.md"]


def test_lessons_is_writable_and_scaffolded():
    assert "LESSONS.md" in memory_engine.WRITABLE_PROJECT
    assert "LESSONS.md" in memory_engine.RECONCILIATION_WRITABLE
    assert memory_engine.CANONICAL_SECTIONS["LESSONS.md"] == ["Lessons"]
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "proj"
        ensure_memory_scaffold(base, "P")
        lessons = (base / "LESSONS.md").read_text(encoding="utf-8")
        assert "## Lessons" in lessons and lessons.startswith("# Lessons: P")


def test_scaffold_status_carries_task_queue_board():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "proj"
        ensure_memory_scaffold(base, "P")
        status = (base / "STATUS.md").read_text(encoding="utf-8")
        assert "## Task Queue" in status
        for sub in ("### Completed", "### In Progress", "### Next"):
            assert sub in status
        # the standalone file is not created
        assert not (base / "TASK_QUEUE.md").exists()


def test_migration_folds_legacy_task_queue_into_status_and_removes_it():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "proj"
        base.mkdir(parents=True)
        (base / "STATUS.md").write_text(
            "# Status: Legacy\n\n## Current Phase\nBuild\n", encoding="utf-8"
        )
        (base / "TASK_QUEUE.md").write_text(
            "# Task Queue: Legacy\n\n## In Progress\n- [ ] wire the API\n\n"
            "## Up Next\n- [ ] add tests\n\n## Done\n- [x] scaffold\n",
            encoding="utf-8",
        )
        migrated = memory_engine.migrate_task_queue_into_status(base)
        assert migrated is True
        status = (base / "STATUS.md").read_text(encoding="utf-8")
        # legacy content preserved, mapped to the new subsections
        assert "## Task Queue" in status
        assert "- [ ] wire the API" in status   # In Progress
        assert "- [ ] add tests" in status      # Up Next -> Next
        assert "- [x] scaffold" in status       # Done -> Completed
        assert "### Completed" in status and "### Next" in status
        # status prose above is untouched
        assert "## Current Phase" in status and "Build" in status
        # standalone file removed
        assert not (base / "TASK_QUEUE.md").exists()
        # idempotent: a second call is a no-op
        assert memory_engine.migrate_task_queue_into_status(base) is False


def test_migration_preserves_preamble_items_before_first_heading():
    # Hand-edited TASK_QUEUE.md with an item ABOVE the first ## heading must not
    # be lost when the file is folded in and then removed.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "proj"
        base.mkdir(parents=True)
        (base / "STATUS.md").write_text("# Status\n\n## Current Phase\nGo\n", encoding="utf-8")
        (base / "TASK_QUEUE.md").write_text(
            "# Task Queue: P\n- [ ] PREAMBLE ITEM before any heading\n\n"
            "## In Progress\n- [ ] wire it\n",
            encoding="utf-8",
        )
        assert memory_engine.migrate_task_queue_into_status(base) is True
        status = (base / "STATUS.md").read_text(encoding="utf-8")
        assert "PREAMBLE ITEM before any heading" in status  # not dropped
        assert "- [ ] wire it" in status
        assert not (base / "TASK_QUEUE.md").exists()


def test_append_is_section_aware_not_end_of_file():
    # A Phase 10.2 regression: STATUS.md ends with the ## Task Queue board, so a
    # blind EOF append would misfile a write inside the board. Append must land
    # inside the NAMED section instead.
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "STATUS.md").write_text(
            "# Status\n\n## What Works\n- login works\n\n## Task Queue\n"
            "### Completed\n- [x] scaffold\n\n### In Progress\n\n### Next\n",
            encoding="utf-8",
        )
        ok = apply_update(
            base, allow=WRITABLE_PROJECT, filename="STATUS.md",
            section="What Works", content="- OAuth works", action="append",
        )
        assert ok is True
        text = (base / "STATUS.md").read_text(encoding="utf-8")
        # the new bullet is under What Works, BEFORE the Task Queue board
        wanted_pos = text.index("- OAuth works")
        board_pos = text.index("## Task Queue")
        assert wanted_pos < board_pos, "append landed inside the Task Queue board"
        # the board is intact
        assert "- [x] scaffold" in text


def test_append_falls_back_to_eof_for_single_section_file():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "RESEARCH.md").write_text("# Research\n\n## Findings\n- a\n", encoding="utf-8")
        ok = apply_update(
            base, allow=WRITABLE_PROJECT, filename="RESEARCH.md",
            section="Findings", content="- b", action="append",
        )
        assert ok is True
        assert "- b" in (base / "RESEARCH.md").read_text(encoding="utf-8")


def test_migration_is_noop_without_legacy_file():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "proj"
        base.mkdir(parents=True)
        (base / "STATUS.md").write_text("# Status\n", encoding="utf-8")
        assert memory_engine.migrate_task_queue_into_status(base) is False


def test_scaffold_runs_migration_first():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d) / "proj"
        base.mkdir(parents=True)
        (base / "TASK_QUEUE.md").write_text(
            "# Task Queue\n\n## Done\n- [x] legacy item\n", encoding="utf-8"
        )
        ensure_memory_scaffold(base, "P")
        status = (base / "STATUS.md").read_text(encoding="utf-8")
        assert "- [x] legacy item" in status
        assert not (base / "TASK_QUEUE.md").exists()
        # no empty duplicate Task Queue section
        assert status.count("## Task Queue") == 1


# ---------- orchestrator wrappers (the previously-untested chat-turn write path) ----------

def test_orchestrator_apply_memory_update_writes_and_rejects_soul():
    import orchestrator
    with tempfile.TemporaryDirectory() as d:
        projects = Path(d)
        (projects / "demo").mkdir()
        prev = orchestrator.PROJECTS_DIR
        orchestrator.PROJECTS_DIR = projects
        try:
            assert orchestrator.apply_memory_update(
                "demo", "STATUS.md", "What Works", "- it builds", "append"
            ) is True
            assert "- it builds" in (projects / "demo" / "STATUS.md").read_text(encoding="utf-8")
            # SOUL.md is never writable.
            assert orchestrator.apply_memory_update(
                "demo", "SOUL.md", "Identity", "pwned", "replace"
            ) is False
            assert not (projects / "demo" / "SOUL.md").exists()
        finally:
            orchestrator.PROJECTS_DIR = prev


def test_orchestrator_apply_global_memory_update_writes_and_rejects_project_file():
    import orchestrator
    with tempfile.TemporaryDirectory() as d:
        mem = Path(d)
        prev = orchestrator.MEMORY_DIR
        orchestrator.MEMORY_DIR = mem
        try:
            assert orchestrator.apply_global_memory_update(
                "USER.md", "Role", "Builder", "append"
            ) is True
            assert "Builder" in (mem / "USER.md").read_text(encoding="utf-8")
            # A project file is not a writable global file.
            assert orchestrator.apply_global_memory_update(
                "STATUS.md", "What Works", "x", "append"
            ) is False
        finally:
            orchestrator.MEMORY_DIR = prev


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
