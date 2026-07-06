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
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Writable-file policy sets (callers pass the one that applies)
# ---------------------------------------------------------------------------

# Global memory files the chat-turn intake judge may write in GENERAL chats.
WRITABLE_GLOBAL: frozenset[str] = frozenset({"USER.md", "WORKSTYLE.md", "MEMORY.md"})

# Project memory files the chat-turn intake judge may write in a project chat.
# Phase 10.2 — TASK_QUEUE.md was merged into STATUS.md's ``## Task Queue``
# section (no longer a standalone file); LESSONS.md was added for durable
# project lessons the Main Agent captures from builds/failures/fixes/research.
WRITABLE_PROJECT: frozenset[str] = frozenset(
    {"PROJECT.md", "STATUS.md", "DECISIONS.md", "RESEARCH.md", "LESSONS.md"}
)

# Post-run reconciliation writes a tighter set — PROJECT.md is intentionally
# excluded (a Coding Agent run summary shouldn't rewrite the project definition).
RECONCILIATION_WRITABLE: frozenset[str] = frozenset(
    {"STATUS.md", "DECISIONS.md", "RESEARCH.md", "LESSONS.md"}
)

# Phase 8 — the deployment ledger. Written ONLY by the deterministic
# ``ops_ledger`` (deploy/env/migration/webhook confirm-execute paths), never by
# an LLM. It is deliberately absent from ``WRITABLE_PROJECT`` /
# ``RECONCILIATION_WRITABLE`` (so the chat-judge + reconciler can never touch it)
# and from ``DEFAULT_SECTION`` (so no judge parser can default-target it). A
# single-edit footgun is thereby avoided: enabling LLM writes to OPS.md would
# require adding it to a judge allow-set, which we never do.
OPS_WRITABLE: frozenset[str] = frozenset({"OPS.md"})


# ---------------------------------------------------------------------------
# Canonical section headings — shared so templates / scaffold / prompts /
# reconciliation's default-section map cannot diverge.
# ---------------------------------------------------------------------------

# Ordered list of the stable ``##`` headings each project memory file carries.
# STATUS.md now ends with a ``## Task Queue`` section that carries the
# ``### Completed`` / ``### In Progress`` / ``### Next`` board (Phase 10.2 —
# the former TASK_QUEUE.md). It is intentionally LAST so a ``replace`` of the
# section (the engine's ``##``-level granularity) rewrites the whole board
# without touching the status prose above it.
CANONICAL_SECTIONS: dict[str, list[str]] = {
    "PROJECT.md": ["Vision", "Scope", "Target User", "Tech Stack"],
    "STATUS.md": ["Current Phase", "Latest Milestone", "What Works", "Next Up", "Task Queue"],
    "DECISIONS.md": ["Decisions"],
    "RESEARCH.md": ["Findings"],
    # Phase 10.2 — durable, project-specific lessons the Main Agent learns from
    # builds, failures, fixes, reviews, deployments, research, and user
    # decisions. Writable by the intake judge + post-run reconciliation; a
    # target for skill-patch evidence. Never written by the Coding Agent.
    "LESSONS.md": ["Lessons"],
    # Phase 8 — the deployment ledger. One append-only section; entries are
    # self-describing ``###`` blocks written by ``ops_ledger``. Scaffolded here
    # (so the section anchor exists) but intentionally NOT in DEFAULT_SECTION or
    # any judge writable set.
    "OPS.md": ["Ledger"],
}

# Where a write with no/unknown section should land, per file. Used by the
# reconciliation judge parser and as a safe default for the intake judge.
DEFAULT_SECTION: dict[str, str] = {
    "PROJECT.md": "Scope",
    "STATUS.md": "What Works",
    "DECISIONS.md": "Decisions",
    "RESEARCH.md": "Findings",
    "LESSONS.md": "Lessons",
}

# The ``## Task Queue`` board inside STATUS.md. Its three ``###`` subsections
# are the merge target of the former TASK_QUEUE.md (see migration below).
TASK_QUEUE_SECTION = "Task Queue"
TASK_QUEUE_SUBSECTIONS = ("Completed", "In Progress", "Next")


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

def _block_already_present(body: str, current: str) -> bool:
    """True when ``body`` is an EXACT repeat of existing content (block or line).

    Dedups a re-append of the same entry (double reconciliation) without the
    false positives of a raw substring test: a distinct new short entry that
    merely happens to be a substring of existing text is NOT considered present.
    """
    b = body.strip()
    if not b:
        return False
    # Exact whole-line match — the common single-line append (task-queue item,
    # status note).
    if b in {ln.strip() for ln in current.split("\n") if ln.strip()}:
        return True
    # Exact multi-line block match (blocks separated by blank lines).
    blocks = [blk.strip() for blk in re.split(r"\n\s*\n", current) if blk.strip()]
    return b in blocks


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
        # Dedup by BLOCK equality, not raw substring containment. A substring
        # test drops a legitimately-new short entry whenever its text happens to
        # appear anywhere in the file (e.g. appending "- [x] Add /healthcheck"
        # when "Add /healthcheck endpoint" already exists) — a silently lost
        # write reported to callers as "rejected". Compare the stripped new block
        # against the stripped existing blocks (split on blank lines) and against
        # the individual existing lines, so an exact repeat is still deduped.
        if body and _block_already_present(body, current):
            return False
        # Section-aware append: when ``section`` names an existing ``## heading``,
        # insert at the END of that section (just before the next ``## `` or EOF)
        # rather than at end-of-file. Before Phase 10.2 an EOF append to a
        # multi-section file (STATUS.md) landed in the trailing prose harmlessly;
        # now STATUS.md ends with the structured ``## Task Queue`` board, so a
        # blind EOF append would misfile the write inside the board. Falls back
        # to EOF append when no/unknown section (preserves single-section files).
        target_heading = f"## {section}" if section else ""
        lines = current.split("\n")
        insert_at = None
        if target_heading and any(ln.rstrip() == target_heading for ln in lines):
            in_section = False
            last_content_idx = None
            for i, ln in enumerate(lines):
                if ln.rstrip() == target_heading:
                    in_section = True
                    last_content_idx = i
                    continue
                if in_section and ln.startswith("## "):
                    break
                if in_section and ln.strip():
                    last_content_idx = i
            if last_content_idx is not None:
                insert_at = last_content_idx + 1
        addition = content.rstrip("\n")
        if insert_at is not None:
            lines[insert_at:insert_at] = [addition]
            new_text = "\n".join(lines)
        else:
            new_text = current
            if new_text and not new_text.endswith("\n"):
                new_text += "\n"
            new_text += content + ("\n" if not content.endswith("\n") else "")
        try:
            _atomic_write(filepath, new_text)
        except OSError as exc:
            log.warning("memory_engine: could not write %s: %s", filepath, exc)
            return False
        return True

    if action == "replace":
        lines = current.split("\n")
        new_lines: list[str] = []
        in_section = False
        replaced = False
        target_heading = f"## {section}"
        for line in lines:
            # Match the heading EXACTLY (ignoring trailing whitespace), not by
            # prefix. A prefix test (``startswith("## Decisions")``) would latch
            # onto a sibling like ``## Decisions Archive`` when the intended
            # ``## Decisions`` is absent, keeping the wrong heading and dropping
            # its body — silently overwriting the wrong section.
            if not replaced and line.rstrip() == target_heading:
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
# The ``## Task Queue`` section seeds the three ``###`` subsections as one
# block (the board lives under a single ``##`` heading so the engine can
# replace it wholesale).
_TASK_QUEUE_SEED = (
    "### Completed\n\n- [x] Project created\n\n"
    "### In Progress\n\n- [ ] Define project scope and requirements\n\n"
    "### Next\n\n- [ ] Set up initial project structure"
)

_SECTION_SEED: dict[tuple[str, str], str] = {
    ("STATUS.md", "Current Phase"): "Planning",
    ("STATUS.md", "Latest Milestone"): "Project created",
    ("STATUS.md", "What Works"): "- Project folder initialized",
    ("STATUS.md", "Next Up"): "- Define project scope and goals",
    ("STATUS.md", "Task Queue"): _TASK_QUEUE_SEED,
    ("LESSONS.md", "Lessons"): "- (Durable project lessons are captured here as work progresses.)",
}


def _title_for(filename: str, project_name: str) -> str:
    return {
        "PROJECT.md": f"# {project_name}",
        "STATUS.md": f"# Status: {project_name}",
        "DECISIONS.md": f"# Decisions: {project_name}",
        "RESEARCH.md": f"# Research: {project_name}",
        "LESSONS.md": f"# Lessons: {project_name}",
        "OPS.md": f"# Ops: {project_name}",
    }.get(filename, f"# {project_name}")


# ---------------------------------------------------------------------------
# Migration: fold a legacy standalone TASK_QUEUE.md into STATUS.md (Phase 10.2)
# ---------------------------------------------------------------------------

# Old TASK_QUEUE.md ``##`` heading -> new ``### `` subsection under Task Queue.
_LEGACY_TQ_MAP = {"done": "Completed", "in progress": "In Progress", "up next": "Next"}


def _parse_md_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into ``(heading, body)`` pairs.

    Any content BEFORE the first ``## `` heading (excluding a single leading
    ``# `` H1 title line) is preserved under the sentinel heading
    ``"(preamble)"`` so a caller that migrates the file can keep hand-edited
    items placed above the first heading. Bodies are ``strip("\\n")``-ed.
    """
    sections: list[tuple[str, str]] = []
    heading = "(preamble)"
    buf: list[str] = []
    saw_h1 = False
    for line in text.split("\n"):
        if line.startswith("## "):
            body = "\n".join(buf).strip("\n")
            if body:
                sections.append((heading, body))
            heading = line[3:].strip()
            buf = []
        elif heading == "(preamble)" and not saw_h1 and line.startswith("# "):
            saw_h1 = True  # drop exactly one leading H1 title
        else:
            buf.append(line)
    body = "\n".join(buf).strip("\n")
    if body:
        sections.append((heading, body))
    return sections


def _build_task_queue_from_legacy(tq_text: str) -> str:
    """Render a legacy TASK_QUEUE.md body as a ``## Task Queue`` section block
    (``### Completed`` / ``### In Progress`` / ``### Next``), preserving every
    item. Unmapped legacy headings are kept under a ``### Other`` subsection so
    nothing is lost."""
    by_new: dict[str, list[str]] = {name: [] for name in TASK_QUEUE_SUBSECTIONS}
    other: list[str] = []
    for heading, body in _parse_md_sections(tq_text):
        new_name = _LEGACY_TQ_MAP.get(heading.strip().lower())
        target = by_new[new_name] if new_name else other
        if body.strip():
            target.append(body.rstrip())
    parts: list[str] = []
    for name in TASK_QUEUE_SUBSECTIONS:
        parts.append(f"### {name}\n")
        if by_new[name]:
            parts.append("\n".join(by_new[name]))
        parts.append("")
    if other:
        parts.append("### Other (migrated)\n")
        parts.append("\n".join(other))
        parts.append("")
    return "\n".join(parts).strip("\n")


def migrate_task_queue_into_status(base_dir: Path) -> bool:
    """Fold a legacy standalone ``TASK_QUEUE.md`` into ``STATUS.md``'s
    ``## Task Queue`` section, then remove the standalone file. Idempotent
    (no-op once TASK_QUEUE.md is gone) and non-destructive (every legacy item
    is preserved). Returns True iff a migration was performed.

    Runs BEFORE ``ensure_memory_scaffold`` so the scaffold sees the merged
    section already present and doesn't seed an empty duplicate.
    """
    tq_path = base_dir / "TASK_QUEUE.md"
    if not tq_path.exists():
        return False
    try:
        tq_text = tq_path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk error
        log.warning("migrate_task_queue: could not read %s: %s", tq_path, exc)
        return False

    status_path = base_dir / "STATUS.md"
    try:
        status_text = status_path.read_text(encoding="utf-8") if status_path.exists() else ""
    except OSError as exc:  # pragma: no cover - disk error
        log.warning("migrate_task_queue: could not read %s: %s", status_path, exc)
        return False
    if not status_text.strip():
        status_text = f"# Status\n"

    board = _build_task_queue_from_legacy(tq_text)
    if "## Task Queue" in status_text:
        # STATUS already has the board (partial prior migration) — append the
        # legacy items at EOF under a dated-free "migrated" marker so nothing is
        # lost, rather than clobbering the existing board.
        if not status_text.endswith("\n"):
            status_text += "\n"
        status_text += "\n### Migrated from TASK_QUEUE.md\n" + board + "\n"
    else:
        if not status_text.endswith("\n"):
            status_text += "\n"
        status_text += f"\n## {TASK_QUEUE_SECTION}\n" + board + "\n"

    try:
        _atomic_write(status_path, status_text)
    except OSError as exc:  # pragma: no cover - disk error
        log.warning("migrate_task_queue: could not write %s: %s", status_path, exc)
        return False
    # Content is safely in STATUS.md now — drop the standalone file.
    try:
        tq_path.unlink()
    except OSError as exc:  # pragma: no cover - disk error
        log.warning("migrate_task_queue: migrated but could not remove %s: %s", tq_path, exc)
    return True


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

    # Phase 10.2 — fold any legacy standalone TASK_QUEUE.md into STATUS.md
    # BEFORE seeding canonical sections, so the scaffold sees the merged
    # ``## Task Queue`` already present (no empty duplicate). Idempotent.
    touched: list[str] = []
    if migrate_task_queue_into_status(base_dir):
        touched.append("STATUS.md")

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
