"""Data models for the execution layer.

Kept intentionally minimal. New fields are added only when a concrete consumer
needs them (avoid speculative shape changes — every field shows up in run.json
or in the runner's loop, not 'just in case').
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    """Lifecycle / final status for a Coding Agent run.

    The four terminal values match the JSON contract the agent emits in its
    `final` action. RUNNING is used while a run is in flight.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


class ExecutionWorkspace(BaseModel):
    """Filesystem layout metadata for a project's execution workspace."""

    project_id: str
    root: str
    repo_dir: str
    runs_dir: str
    logs_dir: str
    agent_md: str
    task_md: str
    exists: bool = True


class TaskSpec(BaseModel):
    """A task card handed to the Coding Agent."""

    title: str
    task_card: str
    created_by: str = "manual"


class RunRecord(BaseModel):
    """Persistent metadata for a single agent run (serialized as run.json)."""

    run_id: str
    project_id: str
    task_title: str
    status: RunStatus = RunStatus.RUNNING
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    files_changed: list[str] = Field(default_factory=list)
    commands_run: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    # Task 06.0 — post-run memory reconciliation metadata. ``None`` means
    # reconciliation has not run yet for this record (e.g. a record loaded
    # from an older run). ``True``/``False`` reflect whether the reconciler
    # actually applied memory updates. ``memory_reconciliation`` is a short
    # status tag ("applied", "skipped_read_only", "skipped_failed_noisy",
    # "skipped_judge_no_update", "skipped_already_reconciled", "error", ...);
    # ``memory_reconciliation_error`` carries the error string if the
    # reconciler crashed. Reconciler is best-effort — see
    # ``execution/memory_reconciliation.py``.
    memory_reconciled: Optional[bool] = None
    memory_reconciliation: Optional[str] = None
    memory_reconciliation_error: Optional[str] = None


class ResultSummary(BaseModel):
    """Concise summary returned to the main agent after a run completes."""

    run_id: str
    status: str
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    commands_run: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    result_path: Optional[str] = None
