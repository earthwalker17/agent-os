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

    The agent's ``final`` action can only declare one of the four "natural"
    terminal values (``completed`` / ``partial`` / ``blocked`` / ``failed``);
    RUNNING is used while a run is in flight. ``CANCELLED`` is a fifth terminal
    value the *runner* sets directly when a user cancels an active run — it is
    never agent-settable (see ``runner._ALLOWED_STATUS``) and is intentionally
    excluded from memory reconciliation (an aborted run has no settled outcome
    worth writing to memory — see ``memory_reconciliation.TERMINAL_STATUSES``).
    """

    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


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


class TaskStatus(str, Enum):
    """Lifecycle status for a single task unit inside a run's execution plan.

    Distinct from :class:`RunStatus` (which is the run as a whole). A task is
    ``PENDING`` until the runner picks it up, ``RUNNING`` while its bounded
    tool loop is in flight, then one of the three terminal values. ``SKIPPED``
    is used when a task is not attempted because one of its declared
    dependencies failed.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ExecutionTask(BaseModel):
    """One unit of work in a run's execution plan (Phase 5).

    The planner breaks a complex task card into an ordered, optionally
    dependency-linked list of these. The runner executes them one at a time
    (single-threaded this phase) and mutates each task's ``status`` + result
    fields in place as it goes, so a poll of run.json shows live progress.

    ``depends_on`` references other tasks by ``id``; it is honored for ordering
    and skip-on-failure today and leaves room for future parallel execution.
    """

    id: str
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = Field(default_factory=list)
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    commands_run: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    steps_used: int = 0


class ExecutionPlan(BaseModel):
    """A run's persisted plan + task graph (Phase 5).

    Produced by the planning phase before implementation begins and persisted
    both inside :class:`RunRecord` (run.json) and as a standalone ``plan.json``
    run artifact. ``mode`` records how the plan was formed:

      - ``"planned"`` — an LLM planning loop decomposed the task card.
      - ``"simple"`` — the task card looked simple; a single task covering the
        whole card was created without an LLM planning call.
      - ``"fallback"`` — planning was attempted but could not produce a usable
        plan (parse failure, LLM unavailable, zero/over-cap/cyclic tasks), so a
        single-task plan covering the whole card was substituted.

    ``tasks`` carries the live task statuses, mutated in place during execution.
    """

    goal: str = ""
    analysis: str = ""
    risks: list[str] = Field(default_factory=list)
    tasks: list[ExecutionTask] = Field(default_factory=list)
    mode: str = "simple"  # "planned" | "simple" | "fallback"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class VerificationCommandResult(BaseModel):
    """Outcome of a single verification command (Task 06.2E).

    A run may verify with more than one command (e.g. a full-stack project
    runs both ``python -m pytest`` and ``npm run build``). Each command's
    individual outcome is captured here; the aggregate lives on the parent
    :class:`VerificationResult`.

    ``kind`` is a coarse label for the command's role —
    ``"install"`` / ``"build"`` / ``"test"`` / ``"syntax"`` / ``"manual"`` —
    so the UI can explain *why* a command ran. ``status`` is
    ``"passed"`` (exit 0), ``"failed"`` (non-zero exit or sandbox/runtime
    error), or ``"skipped"`` (not run because an earlier command in the
    chain already failed).
    """

    command: str
    kind: str = "manual"
    status: str = "skipped"  # "passed" | "failed" | "skipped"
    exit_code: Optional[int] = None
    output_preview: str = ""
    duration_ms: Optional[int] = None


class VerificationResult(BaseModel):
    """Outcome of the post-run command verification (Task 06.2A + 06.2E).

    ``enabled`` is False when no safe verify command was configured or could
    be inferred. When ``enabled`` is True, ``status`` distinguishes
    ``"passed"`` (every command exited 0), ``"failed"`` (any command failed
    or hit a sandbox/runtime error), and ``"skipped"``.

    Task 06.2E adds three fields:

      - ``mode`` — how the commands were chosen: ``"manual"`` (an explicit
        ``## Verification`` block in TASK.md), ``"inferred"`` (derived from
        the repo contents), or ``"skipped"`` (nothing safe to run).
      - ``commands`` — the per-command results. For backward compatibility
        the top-level ``command`` / ``exit_code`` / ``output_preview`` /
        ``duration_ms`` fields mirror the *aggregate* (the first failing
        command when failed, otherwise the last command).
      - ``repair_attempts`` — how many bounded repair passes the runner made
        after an initial failure (0 or 1 for this task).
    """

    enabled: bool = False
    command: Optional[str] = None
    status: str = "skipped"  # "passed" | "failed" | "skipped"
    exit_code: Optional[int] = None
    output_preview: str = ""
    duration_ms: Optional[int] = None
    # Task 06.2E.
    mode: str = "manual"  # "manual" | "inferred" | "skipped"
    commands: list[VerificationCommandResult] = Field(default_factory=list)
    repair_attempts: int = 0


class BrowserPageCapture(BaseModel):
    """One captured page/view from a browser-verification run.

    The capture pipeline visits a small, bounded set of pages (the entry
    URL plus a few discovered navigation targets) and records one of these
    per page. ``path`` is the run-relative artifact path (e.g.
    ``screenshots/browser.png``) so the UI can build a fetch URL without
    leaking absolute filesystem paths. The first capture always keeps the
    legacy ``screenshots/browser.png`` name for backward compatibility.

    ``readiness`` records how confident the capture pipeline is that the
    page had actually rendered before the screenshot was taken:
    ``"confirmed"`` (a render signal was observed and the DOM had settled),
    ``"unconfirmed"`` (the readiness wait timed out — captured anyway so a
    slow-but-alive app is never failed outright), or ``"unknown"`` (legacy
    / single-capture path that does not run the readiness loop).

    ``nav_kind`` records how the page was reached: ``"primary"`` (the entry
    URL), ``"link"`` (a same-origin route link), ``"tab"`` (a tab control),
    or ``"button"`` (a navigation button).
    """

    path: str
    url: str = ""
    label: str = ""
    title: str = ""
    readiness: str = "unknown"  # "confirmed" | "unconfirmed" | "unknown"
    nav_kind: str = ""  # "primary" | "link" | "tab" | "button"


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
    ``screenshots/browser.png``) of the *primary* (first) capture so the UI
    can build a fetch URL without leaking absolute filesystem paths. The
    multi-page upgrade adds ``pages`` (every captured view, including the
    primary one) and ``readiness`` (the aggregate readiness of the primary
    capture); ``screenshot_path`` continues to mirror ``pages[0].path`` so
    existing single-screenshot consumers keep working unchanged.
    """

    enabled: bool = False
    command: Optional[str] = None
    url: Optional[str] = None
    status: str = "skipped"  # "passed" | "failed" | "skipped"
    screenshot_path: Optional[str] = None
    output_preview: str = ""
    duration_ms: Optional[int] = None
    # Multi-page capture (readiness + multi-view upgrade). ``pages`` carries
    # every captured view (the primary capture is ``pages[0]`` and keeps the
    # ``screenshots/browser.png`` name); ``readiness`` is the primary
    # capture's readiness outcome. Both default empty/None so older records
    # and the single-capture path round-trip unchanged.
    pages: list[BrowserPageCapture] = Field(default_factory=list)
    readiness: Optional[str] = None
    # Task 06.2C — optional dependency-install step for the UI-triggered
    # flow. ``None`` for the TASK.md-driven runner path (06.2B), which does
    # not install anything. For the user-triggered flow, ``install_status``
    # is ``"passed"`` (install succeeded), ``"failed"`` (install failed —
    # browser screenshot capture is skipped), or ``"skipped"`` (no
    # ``package.json`` in the repo, so nothing to install).
    install_command: Optional[str] = None
    install_status: Optional[str] = None  # "passed" | "failed" | "skipped"
    install_output_preview: str = ""


class VisualReviewResult(BaseModel):
    """Outcome of the optional AI visual judgment over browser screenshots.

    Runs after a passing browser verification, when a vision-capable model
    provider key is configured. A vision model looks at the captured
    page(s) plus the task context and judges whether the app appears
    actually usable — loaded, visually coherent, relevant to the task, and
    free of obvious broken states (spinner-only, blank page, error overlay,
    missing content, wrong route).

    **Diagnostic-only.** This result never changes a run's status, never
    downgrades ``completed`` → ``partial``, and never adds blockers — it is
    a separate, advisory signal surfaced alongside browser verification.

    ``status`` is ``"passed"`` / ``"warning"`` / ``"failed"`` /
    ``"inconclusive"`` (the model's verdict) or ``"skipped"`` (no
    vision-capable provider, no screenshots, or otherwise not run — see
    ``skipped_reason``). ``reasoning`` is a concise, user-facing rationale
    only — never raw chain-of-thought. ``pages`` holds an optional per-page
    breakdown (``{path, label, verdict, note}``).
    """

    enabled: bool = False
    status: str = "skipped"  # passed | warning | failed | inconclusive | skipped
    headline: str = ""
    reasoning: str = ""
    evidence: list[str] = Field(default_factory=list)
    pages: list[dict] = Field(default_factory=list)
    provider: Optional[str] = None
    model: Optional[str] = None
    duration_ms: Optional[int] = None
    skipped_reason: str = ""


class RecoveryAssessment(BaseModel):
    """Main-Agent assessment of a non-green run (Phase 6).

    Produced best-effort by ``execution.recovery.assess_run`` after a run reaches
    a non-green terminal state (``partial`` / ``failed`` / ``blocked``, or a
    ``completed`` run whose command/browser verification or AI visual review
    failed). It interprets the compact run outcome and recommends a single
    bounded next step for the **user to confirm** — it never auto-dispatches a
    run (explicit-dispatch invariant).

    ``verdict``:
      - ``"ok"`` — nothing to recover (rarely persisted; green runs are skipped).
      - ``"needs_recovery"`` — a concrete bounded next step is recommended.
      - ``"exhausted"`` — automatic progress looks exhausted; report to the user.

    ``recommended_action`` is one of ``inspect`` / ``repair`` / ``split`` /
    ``reverify`` / ``report``. ``follow_up_task_card`` is a self-contained
    imperative task card for the Coding Agent — populated only when a follow-up
    run is the recommendation (``repair`` / ``split`` / ``reverify``), empty
    otherwise. ``assessed`` is False on the skip / error paths.
    """

    assessed: bool = False
    verdict: str = "ok"  # ok | needs_recovery | exhausted
    diagnosis: str = ""
    recommended_action: str = "report"  # inspect | repair | split | reverify | report
    follow_up_task_card: str = ""
    rationale: str = ""
    error: Optional[str] = None


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
    # Phase 6.1 — the reconciliation judge's one-sentence reason for the decision
    # (applied or skipped), surfaced in the run card + detail modal so memory
    # changes are auditable. ``None`` for older records / before reconciliation.
    memory_reconciliation_reason: Optional[str] = None
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
    # AI visual judgment over the browser-verification screenshots. ``None``
    # for runs that finished before visual review was introduced, or that
    # never reached a passing browser verification. Diagnostic-only — it is
    # surfaced alongside browser verification but never changes ``status``.
    visual_review: Optional[VisualReviewResult] = None
    # Task 06.2E — transient sub-status for the automatic post-run command
    # verification phase. ``None`` outside that phase; ``"verifying"`` while
    # the inferred/manual commands run, ``"repairing"`` during the one bounded
    # repair pass. The runner clears it once the run settles. The frontend
    # treats these as in-progress so the chat thread + Runs panel show the
    # right phase instead of a premature terminal status.
    verification_state: Optional[str] = None
    # Task 06.2D — transient sub-status for the user-triggered browser
    # verification flow. ``None`` when no UI verification has been requested,
    # ``"running"`` while install + dev server + screenshot is in flight (the
    # endpoint writes this before the blocking work so a concurrent Runs-panel
    # poll can tell the run is active again), then the terminal browser
    # verification status (``"passed"`` / ``"failed"``) once it settles. The
    # frontend treats only ``"running"`` as in-progress.
    browser_verification_state: Optional[str] = None
    # Phase 5 — the run's execution plan + task graph. ``None`` for runs that
    # finished before planning was introduced. Populated for every new run
    # (a trivial single-task plan for simple cards, a decomposed multi-task
    # plan for complex ones); task statuses inside it mutate as execution
    # proceeds, so a poll of run.json reflects live per-task progress.
    plan: Optional[ExecutionPlan] = None
    # Run control — cooperative cancellation. The cancel endpoint sets this
    # ``True`` (and leaves status ``running``) so the UI can show a transient
    # "cancelling" phase while the runner reaches its next step boundary; the
    # runner then flips status to ``cancelled``. ``False`` for normal runs.
    cancel_requested: bool = False
    # Run control — retry linkage. ``retry_of`` is the run_id this run was
    # spawned from (set on a retry's new record); ``retried_by`` is the run_id
    # of the retry spawned from this run (set on the original). Both ``None``
    # for runs never involved in a retry, so old records round-trip unchanged.
    retry_of: Optional[str] = None
    retried_by: Optional[str] = None
    # Phase 6.1 — user-approved bounded recovery. ``recovery_budget`` is the
    # number of remaining auto-recovery attempts (set by the user at confirm time;
    # 0 = none). It is the SINGLE SOURCE OF TRUTH for auto-recovery; a child
    # inherits ``budget - 1``. ``recovery_of`` links a recovery run to the run it
    # was spawned from; ``recovered_by`` is the recovery run spawned from this one
    # (set once — by auto-recovery — so "at most one recovery per parent" holds).
    # ``orchestration_round`` is the chain depth (redundant hard cap). All default
    # 0/None so old records round-trip unchanged.
    recovery_budget: int = 0
    recovery_of: Optional[str] = None
    recovered_by: Optional[str] = None
    orchestration_round: int = 0
    # Phase 6 — Main-Agent recovery assessment for a non-green terminal run.
    # ``None`` until assessed (and for green runs, which are skipped). Best-effort:
    # an assessment failure never fails the run. Populated by
    # ``execution.recovery.assess_run`` from ``runner._finalize``.
    recovery_assessment: Optional[RecoveryAssessment] = None
    # Phase 7 — Project Ops (Git/GitHub) linkage. All default ``None``/``False``
    # so old run.json records round-trip unchanged (mirrors the ``retry_of`` /
    # ``recovery_of`` convention). Scalar refs only — the full diff text lives in
    # the per-run ``diff.patch`` artifact, never on the record (§6 context
    # hygiene). Secrets never appear here.
    #   - ``pre_run_checkpoint``: out-of-branch checkpoint commit sha captured at
    #     dispatch (rollback restores via ``base_commit`` + this snapshot).
    #   - ``checkpoint_tag`` / ``base_commit``: the checkpoint tag name and the
    #     branch HEAD at checkpoint time (the rollback reset target).
    #   - ``head_commit``: branch HEAD observed at finalize (diff anchor).
    #   - ``branch`` / ``commit_sha``: the user-confirmed commit's branch + sha.
    #   - ``pushed`` / ``pr_url`` / ``pr_number``: GitHub delivery state.
    #   - ``diff_stat``: a compact one-line diff summary (no raw diff).
    #   - ``git_state``: transient sub-status while a Git action runs
    #     ("checkpointing"/"committing"/"pushing"/"opening_pr"); ``None`` at rest.
    #     Mirrors ``verification_state``; NOT a ``RunStatus`` value.
    pre_run_checkpoint: Optional[str] = None
    checkpoint_tag: Optional[str] = None
    base_commit: Optional[str] = None
    head_commit: Optional[str] = None
    branch: Optional[str] = None
    commit_sha: Optional[str] = None
    pushed: bool = False
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    diff_stat: Optional[str] = None
    git_state: Optional[str] = None


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
    visual_review: Optional[VisualReviewResult] = None
    plan: Optional[ExecutionPlan] = None
    # Phase 7 — compact Git delivery metadata for the main agent (§6: metadata
    # only, never the raw diff). ``None`` for runs without Git activity.
    commit_sha: Optional[str] = None
    branch: Optional[str] = None
    pr_url: Optional[str] = None
    diff_stat: Optional[str] = None
