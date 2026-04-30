"""Filesystem manager for project-scoped execution workspaces.

A workspace lives at `execution_workspaces/{project_id}/` (sibling to
`projects/`, `memory/`, `backend/`, `frontend/`) and contains:

    repo/      — the working code tree the Coding Agent edits
    runs/      — per-run artifacts
    logs/      — per-run logs
    AGENT.md   — agent identity, principles, safe-operating rules
    TASK.md    — current objective, queue, progress, result summary

Init is idempotent: missing folders are created, but existing AGENT.md and
TASK.md are left untouched so the project's accumulated state is never lost.
"""

from __future__ import annotations

from pathlib import Path

from .models import ExecutionWorkspace
from .templates import render_agent_md, render_task_md


# Repo root = parent of backend/ (sibling to projects/, memory/, frontend/)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_EXECUTION_ROOT = _REPO_ROOT / "execution_workspaces"


def get_execution_root() -> Path:
    """Return the absolute path to the top-level execution_workspaces directory."""
    return _EXECUTION_ROOT


def get_project_execution_dir(project_id: str) -> Path:
    """Return the absolute path to a project's execution workspace directory.

    Does not create the directory or check existence.
    """
    return _EXECUTION_ROOT / project_id


def _build_workspace_model(project_id: str, exists: bool) -> ExecutionWorkspace:
    base = get_project_execution_dir(project_id)
    return ExecutionWorkspace(
        project_id=project_id,
        root=str(base),
        repo_dir=str(base / "repo"),
        runs_dir=str(base / "runs"),
        logs_dir=str(base / "logs"),
        agent_md=str(base / "AGENT.md"),
        task_md=str(base / "TASK.md"),
        exists=exists,
    )


def init_execution_workspace(
    project_id: str, project_name: str | None = None
) -> ExecutionWorkspace:
    """Create the workspace folder layout for a project, idempotently.

    - Creates `execution_workspaces/{project_id}/` and `repo/`, `runs/`, `logs/`
      if they don't already exist.
    - Seeds `AGENT.md` and `TASK.md` from templates only if they don't exist.
    - Returns workspace metadata.
    """
    base = get_project_execution_dir(project_id)
    base.mkdir(parents=True, exist_ok=True)

    for sub in ("repo", "runs", "logs"):
        (base / sub).mkdir(exist_ok=True)

    agent_md = base / "AGENT.md"
    if not agent_md.exists():
        agent_md.write_text(render_agent_md(project_id, project_name), encoding="utf-8")

    task_md = base / "TASK.md"
    if not task_md.exists():
        task_md.write_text(render_task_md(project_id, project_name), encoding="utf-8")

    return _build_workspace_model(project_id, exists=True)


def get_execution_workspace(project_id: str) -> ExecutionWorkspace | None:
    """Return workspace metadata if the workspace exists, else None."""
    base = get_project_execution_dir(project_id)
    if not base.exists() or not base.is_dir():
        return None
    return _build_workspace_model(project_id, exists=True)


def read_task_state(project_id: str) -> str | None:
    """Read the current TASK.md content for a project. Returns None if missing."""
    task_md = get_project_execution_dir(project_id) / "TASK.md"
    if not task_md.exists():
        return None
    return task_md.read_text(encoding="utf-8")


def update_task_state(project_id: str, content: str) -> bool:
    """Overwrite TASK.md for a project. Returns True on success.

    Requires the workspace to already exist; will not silently create one.
    """
    base = get_project_execution_dir(project_id)
    if not base.exists() or not base.is_dir():
        return False
    (base / "TASK.md").write_text(content, encoding="utf-8")
    return True
