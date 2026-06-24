"""Shared memory write core for Agent OS (Phase 6).

A small, dependency-free **leaf module**: it imports only the standard library
(``pathlib`` / ``os`` / ``json`` / ``logging`` / ``dataclasses``) and is imported
*by* both the orchestrator (chat-turn memory writeback) and the execution layer
(post-run reconciliation). It deliberately imports neither ``orchestrator`` nor
``execution`` so the import graph stays a clean diamond with no cycles.

Before Phase 6 the markdown write path was duplicated in two places
(``orchestrator.apply_memory_update`` and
``memory_reconciliation._apply_update``) with subtly different behavior — only
the reconciliation copy had OSError guards and append-dedup, and **neither wrote
atomically**, even though project memory files are read on every chat turn and
written from a background reconciliation thread. This module unifies them:

  - One ``apply_update(base_dir, *, allow, ...)`` that takes the writable-file
    allow-list from the caller (so each caller keeps its own policy — the
    orchestrator writes the five project files, reconciliation only four), with
    OSError guards, append-substring dedup, and an **atomic** write.
  - Canonical section names shared by the project-memory templates, the scaffold,
    the judge prompts, and reconciliation's default-section map, so they cannot
    drift apart.
  - A structured ``MemoryDecision`` the chat-turn intake judge returns, so memory
    writeback carries a reason and is inspectable instead of being a bare array.

``SOUL.md`` is never in any allow-list and therefore never writable here.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Writable-file policy sets (callers pass the one that applies)
# ---------------------------------------------------------------------------

# Global memory files the chat-turn intake judge may write in GENERAL chats.
WRITABLE_GLOBAL: frozenset[str] = frozenset({"USER.md", "WORKSTYLE.md", "MEMORY.md"})

# Project memory files the chat-turn intake judge may write in a project chat.
WRITABLE_PROJECT: frozenset[str] = frozenset(
    {"PROJECT.md", "STATUS.md", "TASK_QUEUE.md", "DECISIONS.md", "RESEARCH.md"}
)

# Post-run reconciliation writes a tighter set — PROJECT.md is intentionally
# excluded (a Coding Agent run summary shouldn't rewrite the project definition).
RECONCILIATION_WRITABLE: frozenset[str] = frozenset(
    {"STATUS.md", "TASK_QUEUE.md", "DECISIONS.md", "RESEARCH.md"}
)


# ---------------------------------------------------------------------------
# Canonical section headings — shared so templates / scaffold / prompts /
# reconciliation's default-section map cannot diverge.
# ---------------------------------------------------------------------------

# Ordered list of the stable ``##`` headings each project memory file carries.
CANONICAL_SECTIONS: dict[str, list[str]] = {
    "PROJECT.md": ["Vision", "Scope", "Target User", "Tech Stack"],
    "STATUS.md": ["Current Phase", "Latest Milestone", "What Works", "Next Up"],
    "TASK_QUEUE.md": ["In Progress", "Up Next", "Done"],
    "DECISIONS.md": ["Decisions"],
    "RESEARCH.md": ["Findings"],
}

# Where a write with no/unknown section should land, per file. Used by the
# reconciliation judge parser and as a safe default for the intake judge.
DEFAULT_SECTION: dict[str, str] = {
    "PROJECT.md": "Scope",
    "STATUS.md": "What Works",
    "TASK_QUEUE.md": "Done",
    "DECISIONS.md": "Decisions",
    "RESEARCH.md": "Findings",
}


# ---------------------------------------------------------------------------
# Structured decision returned by the chat-turn memory intake judge (WS3)
# ---------------------------------------------------------------------------

@dataclass
class MemoryUpdateSpec:
    """One proposed memory write."""

    filename: str
    section: str
    content: str
    action: str  # "append" | "replace"
    category: str = ""  # informational: status | task | decision | research | global

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "section": self.section,
            "content": self.content,
            "action": self.action,
            "category": self.category,
        }


@dataclass
class MemoryDecision:
    """Structured result of the per-turn memory intake judgment.

    ``reason`` is a one-sentence justification surfaced to the UI so a user can
    see *why* memory changed (or didn't). ``updates`` is policy-validated by the
    judge before it reaches the caller, but ``apply_update`` re-checks the
    allow-list defensively.
    """

    should_update: bool
    reason: str
    updates: list[MemoryUpdateSpec] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "should_update": self.should_update,
            "reason": self.reason,
            "updates": [u.to_dict() for u in self.updates],
        }


# ---------------------------------------------------------------------------
# Atomic write (own tiny copy — keeps this module free of execution imports)
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp sibling + ``os.replace``).

    Project memory files are read on every chat turn (``load_memory``) and inside
    the delegation snapshot, while reconciliation can write them from a
    background thread. A plain truncate-then-write leaves a window where a
    concurrent reader sees an empty / half-written file. ``os.replace`` is atomic
    on the same volume (NTFS included), so a reader always sees either the old or
    the new complete file, never a torn one.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# The single write path
# ---------------------------------------------------------------------------

def apply_update(
    base_dir: Path,
    *,
    allow: frozenset[str],
    filename: str,
    section: str,
    content: str,
    action: str,
) -> bool:
    """Apply one markdown memory update under ``base_dir``.

    Policy: ``filename`` must be in ``allow`` (callers pass the set that applies
    to their scope — this is how ``SOUL.md`` and any non-writable file are kept
    out). ``base_dir`` is the directory holding the target file
    (``projects/{id}`` for project memory, ``memory/`` for global memory) — it is
    passed in rather than read from a module global so each caller keeps its own
    patch-point in tests.

    Supports ``"append"`` (add to the file, skipped if the exact content body is
    already present) and ``"replace"`` (overwrite the body of the named ``##``
    section, creating the heading if absent).

    Returns ``True`` iff the file was written. Never raises — disk errors are
    logged and reported as ``False`` so a memory write can never crash a chat
    turn or a run finalize.
    """
    if filename not in allow:
        return False
    if not base_dir.exists():
        return False

    filepath = base_dir / filename
    current = ""
    if filepath.exists():
        try:
            current = filepath.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("memory_engine: could not read %s: %s", filepath, exc)
            return False

    if action == "append":
        body = content.strip()
        # Cheap dedup: don't re-append content already present (guards against a
        # turn / run being reconciled twice producing duplicate entries).
        if body and body in current:
            return False
        if current and not current.endswith("\n"):
            current += "\n"
        current += content + ("\n" if not content.endswith("\n") else "")
        try:
            _atomic_write(filepath, current)
        except OSError as exc:
            log.warning("memory_engine: could not write %s: %s", filepath, exc)
            return False
        return True

    if action == "replace":
        lines = current.split("\n")
        new_lines: list[str] = []
        in_section = False
        replaced = False
        for line in lines:
            if line.startswith(f"## {section}"):
                new_lines.append(line)
                new_lines.append(content)
                in_section = True
                replaced = True
                continue
            if in_section and line.startswith("## "):
                in_section = False
            if not in_section:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"\n## {section}")
            new_lines.append(content)
        try:
            _atomic_write(filepath, "\n".join(new_lines))
        except OSError as exc:
            log.warning("memory_engine: could not write %s: %s", filepath, exc)
            return False
        return True

    return False


# ---------------------------------------------------------------------------
# Idempotent project-memory scaffold (WS2)
# ---------------------------------------------------------------------------

# Default seed bodies for each canonical section, used only to backfill a file
# (or a section) that is entirely missing. Never overwrites existing content.
_SECTION_SEED: dict[tuple[str, str], str] = {
    ("STATUS.md", "Current Phase"): "Planning",
    ("STATUS.md", "Latest Milestone"): "Project created",
    ("STATUS.md", "What Works"): "- Project folder initialized",
    ("STATUS.md", "Next Up"): "- Define project scope and goals",
    ("TASK_QUEUE.md", "In Progress"): "- [ ] Define project scope and requirements",
    ("TASK_QUEUE.md", "Up Next"): "- [ ] Set up initial project structure",
    ("TASK_QUEUE.md", "Done"): "- [x] Project created",
}


def _title_for(filename: str, project_name: str) -> str:
    return {
        "PROJECT.md": f"# {project_name}",
        "STATUS.md": f"# Status: {project_name}",
        "TASK_QUEUE.md": f"# Task Queue: {project_name}",
        "DECISIONS.md": f"# Decisions: {project_name}",
        "RESEARCH.md": f"# Research: {project_name}",
    }.get(filename, f"# {project_name}")


def ensure_memory_scaffold(base_dir: Path, project_name: str) -> list[str]:
    """Idempotently backfill missing project-memory files / canonical sections.

    Creates any of the five project memory files that don't exist and appends any
    canonical ``##`` heading a file is missing, so the structured intake judge and
    post-run reconciliation always have a stable section to target. **Never
    rewrites or removes existing content** — pure additive migration.

    Call this explicitly at project creation / execution-init — NOT from
    ``load_memory`` (which runs on every turn from several code paths, and writing
    from a read path would amplify writes and fight the atomic-write invariant).

    Returns the list of files it touched (for logging / tests).
    """
    if not base_dir.exists():
        try:
            base_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("ensure_memory_scaffold: could not create %s: %s", base_dir, exc)
            return []

    touched: list[str] = []
    for filename, sections in CANONICAL_SECTIONS.items():
        filepath = base_dir / filename
        try:
            current = filepath.read_text(encoding="utf-8") if filepath.exists() else ""
        except OSError:
            current = ""

        original = current
        if not current.strip():
            current = _title_for(filename, project_name) + "\n"

        for section in sections:
            if f"## {section}" not in current:
                seed = _SECTION_SEED.get((filename, section), "")
                if not current.endswith("\n"):
                    current += "\n"
                current += f"\n## {section}\n{seed}\n" if seed else f"\n## {section}\n"

        if current != original:
            try:
                _atomic_write(filepath, current)
                touched.append(filename)
            except OSError as exc:
                log.warning("ensure_memory_scaffold: could not write %s: %s", filepath, exc)
    return touched
