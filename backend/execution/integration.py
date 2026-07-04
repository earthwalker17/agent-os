"""Deterministic integration of patch workspaces into the shared repo (Phase 9).

After a parallel wave settles, each write task's overlay is applied back into
the shared ``repo/`` tree — in plan order, through the base ``ToolRuntime``
(so the sandbox re-validates every path on the way in). Conflicts are
detected, decided deterministically, and surfaced loudly:

- Two tasks wrote the same path with **identical** content → applied once,
  no conflict (independent tasks converging on the same boilerplate is fine).
- Two tasks wrote the same path with **different** content → the first task
  in plan order wins; the later task's version is NOT applied, an
  :class:`~.models.IntegrationConflict` is recorded, and a blocker lands on
  the losing task. The losing version stays fully inspectable in its patch
  workspace. A run with conflicts can never finish better than ``partial``.

No LLM, no heuristics, no silent overwrites — a narrow, reliable merge path.
The runner owns event emission and artifact persistence; this module is pure
"given settled units + a runtime, apply and report".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from .models import ExecutionTask, IntegrationConflict
from .patch_workspace import collect_patch_files, get_overlay_root

log = logging.getLogger(__name__)


@dataclass
class WaveIntegration:
    """Outcome of integrating one wave's patch workspaces."""

    wave: int
    applied: list[str] = field(default_factory=list)  # repo-relative paths
    conflicts: list[IntegrationConflict] = field(default_factory=list)
    per_task: dict[str, list[str]] = field(default_factory=dict)  # task_id -> applied
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "wave": self.wave,
            "applied": list(self.applied),
            "conflicts": [c.model_dump() for c in self.conflicts],
            "per_task": {k: list(v) for k, v in self.per_task.items()},
            "errors": list(self.errors),
        }


def integrate_wave(
    project_id: str,
    run_id: str,
    wave_no: int,
    units: list[ExecutionTask],
    runtime,
) -> WaveIntegration:
    """Apply the given (settled, plan-ordered) patch-workspace units' overlays
    into the shared repo via ``runtime`` (the base ``ToolRuntime``).

    Every unit's overlay is applied regardless of its final status — matching
    sequential semantics, where a task's writes land in the repo as they
    happen even if the task later fails (dependents may build on partial
    output; see ``planner._dependency_provides_output``). Per-file failures
    are recorded as errors, never raised.
    """
    result = WaveIntegration(wave=wave_no)

    # normcased path -> (original_rel, task_id, content) of the applied
    # (first-writer) version. Keyed via ``os.path.normcase`` so collision
    # detection matches the actual filesystem: on Windows/macOS it folds case
    # (``src/App.ts`` and ``src/app.ts`` are the SAME on-disk file, so keying on
    # exact case would miss the collision and let the second writer silently
    # overwrite the first with no conflict); on a case-sensitive POSIX FS it is a
    # no-op, so genuinely distinct files still both apply cleanly.
    applied_versions: dict[str, tuple[str, str, str]] = {}

    for unit in units:
        overlay = get_overlay_root(project_id, run_id, unit.id)
        files = collect_patch_files(overlay)
        result.per_task.setdefault(unit.id, [])
        for rel in files:
            try:
                content = (overlay / rel).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                result.errors.append(
                    f"{unit.id}: could not read patch file {rel!r}: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            key = os.path.normcase(rel)
            prior = applied_versions.get(key)
            if prior is not None:
                prior_rel, prior_task, prior_content = prior
                if prior_content == content and prior_rel == rel:
                    # Identical output from independent tasks — already applied.
                    result.per_task[unit.id].append(rel)
                    continue
                conflict = IntegrationConflict(
                    path=rel,
                    applied_task=prior_task,
                    rejected_task=unit.id,
                    wave=wave_no,
                )
                result.conflicts.append(conflict)
                if prior_rel != rel:
                    # Same on-disk file reached via different-case paths — the
                    # kind of collision a case-sensitive exact-match key misses.
                    blocker = (
                        f"integration conflict: {rel!r} collides case-insensitively "
                        f"with {prior_rel!r} written by {prior_task!r}; this task's "
                        f"version was not applied (see its patch workspace)"
                    )
                else:
                    blocker = (
                        f"integration conflict: {rel!r} was also written by "
                        f"{prior_task!r}; this task's version was not applied "
                        f"(see its patch workspace)"
                    )
                if blocker not in unit.blockers:
                    unit.blockers.append(blocker)
                continue

            tool_result = runtime.write_file(rel, content)
            if not tool_result.success:
                result.errors.append(
                    f"{unit.id}: applying {rel!r} failed: {tool_result.error}"
                )
                continue
            applied_versions[key] = (rel, unit.id, content)
            result.applied.append(rel)
            result.per_task[unit.id].append(rel)

    return result
