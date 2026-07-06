"""Built-in skill storage — the single read/write path for skill markdown (Phase 10).

A skill is a reusable method / checklist / rubric / template, NOT an
executable tool. Bodies live as committed markdown under the repo-root
``skills/{agent_id}/{skill_id}.md`` (they ship with Agent OS, like
``memory/SOUL.md``; user edits through the UI land in the working tree and
show up in ``git diff`` — visible and revertible, never silent).

Rules this module enforces:

- **Registry first, paths second.** Every read/write validates the
  ``(agent_id, skill_id)`` pair against ``agents_registry`` before any path
  is built, and re-checks the slug rule defensively — no filesystem path is
  ever constructed from an unvalidated string.
- **Only user actions write.** The one writer is ``write_skill``, called by
  the skill-update endpoint (a user clicking Save). No LLM path, no
  autonomous skill generation, no self-modification.
- **Prompt folding is bounded.** ``skills_prompt_block(mode)`` folds a chat
  mode's skills into the system prompt with per-skill and total char caps,
  and never raises — a broken skill file can't break a chat turn.

Leaf module: stdlib + ``agents_registry`` only.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import agents_registry

# Repo-root skills/ (backend/skills_store.py -> backend/ -> repo root).
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

# One skill file may not exceed this (UI edits are rejected above it).
MAX_SKILL_CHARS = 20_000
# Total skill text folded into one chat mode's prompt section...
SKILL_PROMPT_CAP = 2_000
# ...with a per-skill slice so a long first skill can't crowd out the second.
_PER_SKILL_CAP = 900


def _resolve_skill_path(agent_id: str, skill_id: str) -> tuple[Path, "agents_registry.SkillRef"]:
    """Validate the pair against the registry, then build the path.

    Raises ``ValueError`` for any unknown pair — the endpoint maps that to
    404. The slug re-check is defensive depth: registry ids are already
    slug-tested, but a path segment is never trusted twice.
    """
    ref = agents_registry.skill_ref(agent_id, skill_id)
    if ref is None:
        raise ValueError(f"unknown skill: {agent_id}/{skill_id}")
    aid = str(agent_id).strip().lower()
    if not agents_registry.is_valid_slug(aid) or not agents_registry.is_valid_slug(ref.id):
        raise ValueError(f"invalid skill identifier: {agent_id}/{skill_id}")
    return SKILLS_DIR / aid / f"{ref.id}.md", ref


def read_skill(agent_id: str, skill_id: str) -> str:
    """Return the skill body, or ``""`` when the file is missing on disk
    (registry↔disk drift is surfaced by tests, not by crashing the UI)."""
    path, _ = _resolve_skill_path(agent_id, skill_id)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_skill(agent_id: str, skill_id: str, content: str) -> None:
    """Persist a user edit atomically (temp sibling + ``os.replace``)."""
    path, _ = _resolve_skill_path(agent_id, skill_id)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("skill content must be a non-empty string")
    if len(content) > MAX_SKILL_CHARS:
        raise ValueError(f"skill content exceeds {MAX_SKILL_CHARS} characters")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def skills_prompt_block(mode: str | None) -> str:
    """The bounded skills fold-in for a chat mode's guidance section.

    Returns ``""`` for empty/unknown modes, modes without an active profile,
    or when nothing readable exists. Never raises.
    """
    if not mode:
        return ""
    try:
        profile = next(
            (
                a for a in agents_registry.AGENTS
                if a.mode == mode and a.status == agents_registry.STATUS_ACTIVE
            ),
            None,
        )
        if profile is None or not profile.skills:
            return ""
        parts: list[str] = []
        for ref in profile.skills:
            try:
                body = read_skill(profile.id, ref.id).strip()
            except Exception:  # noqa: BLE001 — one bad skill never blocks the turn
                continue
            if not body:
                continue
            if len(body) > _PER_SKILL_CAP:
                body = body[:_PER_SKILL_CAP].rstrip() + "\n…(truncated)"
            parts.append(f"### Skill: {ref.title}\n{body}")
        if not parts:
            return ""
        block = "\n\n".join(parts)
        if len(block) > SKILL_PROMPT_CAP:
            block = block[:SKILL_PROMPT_CAP].rstrip() + "\n…(skills truncated)"
        return block
    except Exception:  # noqa: BLE001
        return ""
