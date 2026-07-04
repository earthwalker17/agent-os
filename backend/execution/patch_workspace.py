"""Isolated patch workspaces for parallel write tasks (Phase 9).

A patch workspace gives one write-producing task unit a private overlay so
concurrent tasks can never race on the shared ``repo/`` tree:

    execution_workspaces/{project_id}/patches/{run_id}/{task_id}/
        workspace/      — the overlay tree (every file the task wrote)
        manifest.json   — audit artifact: role, status, files, blockers

Reads fall through to the shared repo (the task sees the real project);
writes always land in the overlay. After the wave settles, the deterministic
integration step (``integration.py``) applies each overlay back into the
shared repo through the base ``ToolRuntime`` — conflicts are detected and
surfaced, never silently overwritten.

``PatchToolRuntime`` extends ``ToolRuntime`` but resolves every path through
``ProjectSandbox.resolve_under`` — the overlay inherits the exact same
validation rules (no absolute paths / ``..`` / sensitive files / escapes) as
the repo itself, so the sandbox chokepoint invariant holds. ``run_shell`` /
``run_git`` / ``run_supabase`` are hard-blocked inside a patch workspace:
parallel write tasks produce files; commands run globally (verification runs
after integration, on the integrated tree).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .manager import get_project_execution_dir
from .sandbox import SandboxViolation
from .tool_models import ToolResult
from .tool_runtime import (
    ToolRuntime,
    _MAX_LIST_ENTRIES,
    _MAX_READ_CHARS,
    _MAX_SEARCH_HITS,
    _MAX_SNIPPET_CHARS,
    _SKIP_DIR_NAMES,
    _err,
    _truncate,
)


_BLOCKED_IN_PATCH = (
    "{tool} is not available in an isolated patch workspace — parallel tasks "
    "only read the repo and write files; Agent OS runs verification globally "
    "after your files are integrated. Write the files this task needs, then "
    "emit final."
)


# ---------- layout helpers ----------


def _safe_segment(value: str, kind: str) -> str:
    """Return ``value`` iff it is a single safe path segment, else raise.

    Defense in depth: ``task_id`` is normally sanitized at plan parse
    (``planner.is_safe_task_id``), but the patch-workspace layout is the
    filesystem chokepoint, so it independently refuses any id / run id that
    contains a path separator or ``..`` before it can be joined into a path
    that escapes ``patches/``. ``run_id`` is server-generated (safe by
    construction); this guards against a future caller regression too.
    """
    if (
        not isinstance(value, str)
        or not value
        or value in (".", "..")
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise SandboxViolation(f"unsafe {kind} for patch workspace: {value!r}")
    return value


def get_patches_dir(project_id: str, run_id: str) -> Path:
    """Root of a run's patch workspaces (``patches/{run_id}/``)."""
    return get_project_execution_dir(project_id) / "patches" / _safe_segment(run_id, "run_id")


def get_patch_dir(project_id: str, run_id: str, task_id: str) -> Path:
    return get_patches_dir(project_id, run_id) / _safe_segment(task_id, "task_id")


def get_overlay_root(project_id: str, run_id: str, task_id: str) -> Path:
    """The overlay tree the task writes into (``.../{task_id}/workspace/``).

    Kept one level below the patch dir so the sibling ``manifest.json`` can
    never be mistaken for task output at integration time.
    """
    return get_patch_dir(project_id, run_id, task_id) / "workspace"


def init_patch_workspace(project_id: str, run_id: str, task_id: str) -> Path:
    """Create (idempotently) and return the overlay root for one task."""
    overlay = get_overlay_root(project_id, run_id, task_id)
    overlay.mkdir(parents=True, exist_ok=True)
    return overlay


def collect_patch_files(overlay_root: Path) -> list[str]:
    """Every file in the overlay as sorted repo-relative POSIX paths."""
    if not overlay_root.exists() or not overlay_root.is_dir():
        return []
    files: list[str] = []
    for path in overlay_root.rglob("*"):
        if path.is_file():
            files.append(path.relative_to(overlay_root).as_posix())
    return sorted(files)


def write_patch_manifest(
    project_id: str, run_id: str, task_id: str, manifest: dict
) -> None:
    """Persist the per-task audit manifest (atomic; sibling of workspace/)."""
    patch_dir = get_patch_dir(project_id, run_id, task_id)
    patch_dir.mkdir(parents=True, exist_ok=True)
    path = patch_dir / "manifest.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def read_patch_manifest(project_id: str, run_id: str, task_id: str) -> dict | None:
    path = get_patch_dir(project_id, run_id, task_id) / "manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# ---------- the overlay tool runtime ----------


class PatchToolRuntime(ToolRuntime):
    """A ``ToolRuntime`` whose writes land in an isolated overlay.

    Reads see overlay-over-repo (the overlay shadows the shared tree);
    listings and searches merge both views. Every path — overlay or base —
    still resolves through ``ProjectSandbox.resolve_under``, so the overlay
    enforces the identical sandbox rules.
    """

    def __init__(self, project_id: str, overlay_root: Path):
        super().__init__(project_id)
        self.overlay_root = overlay_root

    # -- resolution helpers --

    def _overlay_path(self, path: str) -> Path:
        return self.sandbox.resolve_under(self.overlay_root, path)

    def _base_path(self, path: str) -> Path:
        return self.sandbox.resolve_repo_path(path)

    def _overlay_rel_files(self) -> set[str]:
        """Relative POSIX paths of every file currently in the overlay."""
        return set(collect_patch_files(self.overlay_root))

    # -- file tools --

    def read_file(self, path: str) -> ToolResult:
        try:
            target = self._overlay_path(path)
        except SandboxViolation as e:
            return _err("read_file", e)
        if target.exists() and target.is_file():
            try:
                raw = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return ToolResult(
                    success=False,
                    tool_name="read_file",
                    error="file is not valid UTF-8 text",
                )
            except Exception as e:  # noqa: BLE001
                return _err("read_file", e)
            content, truncated = _truncate(raw, _MAX_READ_CHARS)
            return ToolResult(
                success=True,
                tool_name="read_file",
                output=content,
                metadata={
                    "path": path,
                    "bytes": target.stat().st_size,
                    "char_count": len(raw),
                    "truncated": truncated,
                    "truncation_limit": _MAX_READ_CHARS,
                    "workspace": "patch",
                },
            )
        # Fall through to the shared repo.
        return super().read_file(path)

    def write_file(self, path: str, content: str) -> ToolResult:
        try:
            target = self._overlay_path(path)
            if target == self.overlay_root.resolve():
                return ToolResult(
                    success=False,
                    tool_name="write_file",
                    error="cannot write to repo root itself",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return ToolResult(
                success=True,
                tool_name="write_file",
                output=f"wrote {len(content)} chars to {path}",
                metadata={
                    "path": path,
                    "bytes": target.stat().st_size,
                    "workspace": "patch",
                },
            )
        except SandboxViolation as e:
            return _err("write_file", e)
        except Exception as e:  # noqa: BLE001
            return _err("write_file", e)

    def append_file(self, path: str, content: str) -> ToolResult:
        try:
            target = self._overlay_path(path)
            if target == self.overlay_root.resolve():
                return ToolResult(
                    success=False,
                    tool_name="append_file",
                    error="cannot append to repo root itself",
                )
            if not target.exists():
                # Layer over the shared repo: seed the overlay copy with the
                # base content so an append never silently drops it.
                base = self._base_path(path)
                if base.exists() and base.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(
                        base.read_text(encoding="utf-8"), encoding="utf-8"
                    )
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as f:
                f.write(content)
            return ToolResult(
                success=True,
                tool_name="append_file",
                output=f"appended {len(content)} chars to {path}",
                metadata={
                    "path": path,
                    "bytes": target.stat().st_size,
                    "workspace": "patch",
                },
            )
        except SandboxViolation as e:
            return _err("append_file", e)
        except UnicodeDecodeError:
            return ToolResult(
                success=False,
                tool_name="append_file",
                error="existing file is not valid UTF-8 text",
            )
        except Exception as e:  # noqa: BLE001
            return _err("append_file", e)

    def list_files(self, path: str = ".") -> ToolResult:
        try:
            overlay_target = self._overlay_path(path)
            base_target = self._base_path(path)

            if not overlay_target.exists() and not base_target.exists():
                return ToolResult(
                    success=False,
                    tool_name="list_files",
                    error=f"path does not exist: {path!r}",
                )
            for tgt in (overlay_target, base_target):
                if tgt.exists() and not tgt.is_dir():
                    return ToolResult(
                        success=False,
                        tool_name="list_files",
                        error=f"path is not a directory: {path!r}",
                    )

            # Merge: base entries first, overlay entries win on name collision.
            merged: dict[str, dict] = {}
            for root in (base_target, overlay_target):
                if not root.exists() or not root.is_dir():
                    continue
                for child in root.iterdir():
                    is_dir = child.is_dir()
                    merged[child.name] = {
                        "name": child.name,
                        "type": "dir" if is_dir else "file",
                        "size": None if is_dir else child.stat().st_size,
                    }

            children = sorted(
                merged.values(), key=lambda e: (e["type"] != "dir", e["name"].lower())
            )
            truncated = len(children) > _MAX_LIST_ENTRIES
            entries = children[:_MAX_LIST_ENTRIES]

            output_lines = []
            for e in entries:
                tag = "[d]" if e["type"] == "dir" else "[f]"
                size = f"  ({e['size']}b)" if e["size"] is not None else ""
                output_lines.append(f"{tag} {e['name']}{size}")

            return ToolResult(
                success=True,
                tool_name="list_files",
                output="\n".join(output_lines) or "(empty directory)",
                metadata={
                    "path": path,
                    "entry_count": len(entries),
                    "entries": entries,
                    "truncated": truncated,
                    "workspace": "patch",
                },
            )
        except SandboxViolation as e:
            return _err("list_files", e)
        except Exception as e:  # noqa: BLE001
            return _err("list_files", e)

    def search_files(self, query: str, path: str = ".") -> ToolResult:
        try:
            if not query:
                return ToolResult(
                    success=False,
                    tool_name="search_files",
                    error="query is empty",
                )
            overlay_target = self._overlay_path(path)
            base_target = self._base_path(path)
            if not overlay_target.exists() and not base_target.exists():
                return ToolResult(
                    success=False,
                    tool_name="search_files",
                    error=f"path does not exist: {path!r}",
                )

            overlay_root = self.overlay_root.resolve()
            base_root = self.sandbox.repo_dir.resolve()
            shadowed = self._overlay_rel_files()

            hits: list[dict] = []
            scanned = 0
            skipped_binary = 0
            truncated = False

            def _scan(root: Path, start: Path, skip_shadowed: bool) -> bool:
                """Returns True when the hit cap was reached."""
                nonlocal scanned, skipped_binary, truncated
                if not start.exists():
                    return False
                for fpath in self._walk_files(start):
                    rel = fpath.relative_to(root).as_posix()
                    if skip_shadowed and rel in shadowed:
                        continue
                    if len(hits) >= _MAX_SEARCH_HITS:
                        truncated = True
                        return True
                    try:
                        text = fpath.read_text(encoding="utf-8")
                    except (UnicodeDecodeError, OSError):
                        skipped_binary += 1
                        continue
                    scanned += 1
                    for lineno, line in enumerate(text.splitlines(), start=1):
                        if query in line:
                            snippet = line.strip()
                            if len(snippet) > _MAX_SNIPPET_CHARS:
                                snippet = snippet[:_MAX_SNIPPET_CHARS] + "..."
                            hits.append(
                                {"path": rel, "line": lineno, "snippet": snippet}
                            )
                            if len(hits) >= _MAX_SEARCH_HITS:
                                truncated = True
                                return True
                return False

            # Overlay first (it shadows the base), then unshadowed base files.
            capped = _scan(overlay_root, overlay_target, skip_shadowed=False)
            if not capped:
                _scan(base_root, base_target, skip_shadowed=True)

            output = (
                "\n".join(f"{h['path']}:{h['line']}: {h['snippet']}" for h in hits)
                or "(no matches)"
            )
            return ToolResult(
                success=True,
                tool_name="search_files",
                output=output,
                metadata={
                    "query": query,
                    "path": path,
                    "hit_count": len(hits),
                    "scanned_files": scanned,
                    "skipped_binary": skipped_binary,
                    "truncated": truncated,
                    "hits": hits,
                    "workspace": "patch",
                },
            )
        except SandboxViolation as e:
            return _err("search_files", e)
        except Exception as e:  # noqa: BLE001
            return _err("search_files", e)

    # -- blocked executors --

    def run_shell(self, command: str, timeout_seconds: int = 30) -> ToolResult:
        return ToolResult(
            success=False,
            tool_name="run_shell",
            error=_BLOCKED_IN_PATCH.format(tool="run_shell"),
        )

    def run_git(self, args, **kwargs) -> ToolResult:
        return ToolResult(
            success=False,
            tool_name="run_git",
            error=_BLOCKED_IN_PATCH.format(tool="run_git"),
        )

    def run_supabase(self, args, **kwargs) -> ToolResult:
        return ToolResult(
            success=False,
            tool_name="run_supabase",
            error=_BLOCKED_IN_PATCH.format(tool="run_supabase"),
        )
