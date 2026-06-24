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


# ---------- apply_update: replace ----------

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
