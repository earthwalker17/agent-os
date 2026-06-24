"""Tests for Context Loader v2 (Phase 6.1) — orchestrator._compact_memory.

Standalone:  python tests/test_context_loader.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import orchestrator
from orchestrator import _compact_memory, _CONTEXT_FILE_CHAR_CAP, _KEEP_RECENT_ITEMS


def _big_decisions(n: int) -> str:
    pad = "x" * 90
    lines = "\n".join(f"- Decision {i}: {pad}" for i in range(n))
    return f"# Decisions: Demo\n\n## Decisions\n{lines}\n"


# ---------- below threshold: untouched ----------

def test_small_file_is_byte_identical():
    content = "# Decisions: Demo\n\n## Decisions\n- Chose SQLite\n- Chose FastAPI\n"
    assert _compact_memory("DECISIONS.md", content) == content


def test_empty_is_identity():
    assert _compact_memory("RESEARCH.md", "") == ""


# ---------- current-state / identity files never trimmed ----------

def test_status_never_trimmed_even_when_large():
    big = "# Status: Demo\n\n## What Works\n" + "\n".join(f"- item {i} {'y'*90}" for i in range(40))
    assert len(big) > _CONTEXT_FILE_CHAR_CAP
    assert _compact_memory("STATUS.md", big) == big


def test_project_never_trimmed_even_when_large():
    big = "# Demo\n\n## Scope\n" + "\n".join(f"- scope {i} {'z'*90}" for i in range(40))
    assert len(big) > _CONTEXT_FILE_CHAR_CAP
    assert _compact_memory("PROJECT.md", big) == big


# ---------- archive sections trimmed to newest ----------

def test_large_decisions_trims_to_tail_with_note():
    content = _big_decisions(30)
    assert len(content) > _CONTEXT_FILE_CHAR_CAP
    out = _compact_memory("DECISIONS.md", content)
    # Header preserved.
    assert "## Decisions" in out
    # Newest entry kept, oldest elided.
    assert "Decision 29" in out
    assert "Decision 0:" not in out
    # Elision note present with a count.
    assert "older entries elided" in out
    # Kept roughly the configured number of recent items.
    kept = [ln for ln in out.split("\n") if ln.startswith("- Decision ")]
    assert len(kept) == _KEEP_RECENT_ITEMS
    # And it's actually shorter.
    assert len(out) < len(content)


def test_task_queue_keeps_current_state_trims_done():
    in_progress = "\n".join(f"- [ ] active task {i}" for i in range(3))
    up_next = "\n".join(f"- [ ] queued task {i}" for i in range(3))
    done = "\n".join(f"- [x] done task {i} {'w'*90}" for i in range(30))
    content = (
        f"# Task Queue: Demo\n\n## In Progress\n{in_progress}\n\n"
        f"## Up Next\n{up_next}\n\n## Done\n{done}\n"
    )
    assert len(content) > _CONTEXT_FILE_CHAR_CAP
    out = _compact_memory("TASK_QUEUE.md", content)
    # Current-state sections kept whole.
    for i in range(3):
        assert f"active task {i}" in out
        assert f"queued task {i}" in out
    # Done history trimmed: newest kept, oldest elided.
    assert "done task 29" in out
    assert "done task 0 " not in out
    assert "older entries elided" in out


def test_singular_elision_note_for_one_extra():
    # _KEEP_RECENT_ITEMS + 1 entries -> exactly one elided -> singular "entry".
    n = _KEEP_RECENT_ITEMS + 1
    pad = "x" * 320  # enough that n lines exceed the char cap (one elided)
    lines = "\n".join(f"- Finding {i}: {pad}" for i in range(n))
    content = f"# Research: Demo\n\n## Findings\n{lines}\n"
    assert len(content) > _CONTEXT_FILE_CHAR_CAP
    out = _compact_memory("RESEARCH.md", content)
    assert "1 older entry elided" in out


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
