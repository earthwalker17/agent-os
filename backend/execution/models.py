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


class VerificationResult(BaseModel):
    """Outcome of the optional post-run verify command (Task 06.2A).

    ``enabled`` is False when the project did not configure a verify
    command (or it failed to parse). When ``enabled`` is True, ``status``
    distinguishes ``"passed"`` (exit 0), ``"failed"`` (non-zero exit or
    sandbox/runtime error), and ``"skipped"`` (recorded as a no-op for
    completeness — e.g. ``enabled`` but the command was rejected before
    execution and we want the UI to surface the reason).
    """

    enabled: bool = False
    command: Optional[str] = None
    status: str = "skipped"  # "passed" | "failed" | "skipped"
    exit_code: Optional[int] = None
    output_preview: str = ""
    duration_ms: Optional[int] = None


class BrowserVerificationResult(BaseModel):
    """Outcome of the optional post-run browser verification (Task 06.2B).

    Kept separate from :class:`VerificationResult` because the lifecycle
    is meaningfully different — there's a long-lived dev server,
    URL-readiness wait, a headless browser, and a screenshot artifact
    on disk — and folding it in would conflate two distinct features.

    ``enabled`` is False when the project did not configure a
    ``## Browser Verification`` block in ``TASK.md``. When enabled,
    ``status`` is ``"passed"`` (server started, URL reachable, screenshot
    captured), ``"failed"`` (anything went wrong end-to-end), or
    ``"skipped"`` (recorded for completeness; rarely used).

    ``screenshot_path`` is the run-relative path (e.g.
    ``screenshots/browser.png``) so the UI can build a fetch URL without
    leaking absolute filesystem paths.
    """

    enabled: bool = False
    command: Optional[str] = None
    url: Optional[str] = None
    status: str = "skipped"  # "passed" | "failed" | "skipped"
    screenshot_path: Optional[str] = None
    output_preview: str = ""
    duration_ms: Optional[int] = None
    # Task 06.2C — optional dependency-install step for the UI-triggered
    # flow. ``None`` for the TASK.md-driven runner path (06.2B), which does
    # not install anything. For the user-triggered flow, ``install_status``
    # is ``"passed"`` (install succeeded), ``"failed"`` (install failed —
    # browser screenshot capture is skipped), or ``"skipped"`` (no
    # ``package.json`` in the repo, so nothing to install).
    install_command: Optional[str] = None
    install_status: Optional[str] = None  # "passed" | "failed" | "skipped"
    install_output_preview: str = ""


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
    # Task 06.2D — the concise human-readable run summary the agent emitted in
    # its ``final`` action (mirrors ``ResultSummary.summary`` and the ``##
    # Summary`` section of result.md). Surfaced here so the chat-first run
    # follow-up card can render a natural completion message straight from
    # run.json without re-parsing result.md. ``""`` for older records.
    summary: str = ""
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
    # Task 06.2A — optional post-run command verification. ``None`` for
    # runs that finished before verification was introduced, or that did
    # not reach the finalize step where verification runs.
    verification: Optional[VerificationResult] = None
    # Task 06.2B — optional post-run browser verification. ``None`` for
    # runs that finished before browser verification was introduced.
    # When the project has no ``## Browser Verification`` block, this is
    # populated with ``enabled=False, status="skipped"`` so the UI can
    # still display a consistent block.
    browser_verification: Optional[BrowserVerificationResult] = None
    # Task 06.2D — transient sub-status for the user-triggered browser
    # verification flow. ``None`` when no UI verification has been requested,
    # ``"running"`` while install + dev server + screenshot is in flight (the
    # endpoint writes this before the blocking work so a concurrent Runs-panel
    # poll can tell the run is active again), then the terminal browser
    # verification status (``"passed"`` / ``"failed"``) once it settles. The
    # frontend treats only ``"running"`` as in-progress.
    browser_verification_state: Optional[str] = None


class ResultSummary(BaseModel):
    """Concise summary returned to the main agent after a run completes."""

    run_id: str
    status: str
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    commands_run: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    result_path: Optional[str] = None
    verification: Optional[VerificationResult] = None
    browser_verification: Optional[BrowserVerificationResult] = None
