"""Background dispatch for CodingAgentRunner.

The runner itself is synchronous: `run_task()` blocks until the LLM tool loop
finishes. That's fine when the caller is a CLI or an HTTP request that's
willing to wait, but it pins the `/api/chat` connection open for the entire
run — easily minutes for a non-trivial task.

`BackgroundRunManager` decouples *dispatching* a run from *executing* it:

    manager.dispatch(project_id, task)
        -> pre-creates run dir + task_card.md + run.json (status=running)
        -> submits CodingAgentRunner.run_task to a ThreadPoolExecutor
        -> returns the placeholder RunRecord immediately

The submitted thread runs the LLM loop and finalizes run.json/result.md in
place; `GET /execution/runs/{id}` reflects status changes as they happen. If
the background execution crashes, the manager flips status from "running" to
"failed", records the error as a blocker, and writes an emergency result.md
so the run never gets stuck in "running" forever.

Single-process, local-first: no Celery, no Redis, no queue service. The
executor lives for the lifetime of the FastAPI process.
"""

from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event, Lock

from . import run_store
from .manager import get_execution_workspace
from .memory_reconciliation import reconcile_run_memory
from .models import RunRecord, RunStatus, TaskSpec
from .recovery_matrix import classify_failure, contract_for
from .runner import CodingAgentRunner


log = logging.getLogger(__name__)

DEFAULT_MAX_WORKERS = 4

# Phase 6.1 — hard cap on auto-recovery chain depth (belt-and-suspenders next to
# the decrementing budget). At most this many auto-dispatched recovery runs can
# follow an original.
RECOVERY_HARD_CAP = 2


class BackgroundRunManager:
    """Owns a single thread pool for running CodingAgentRunner.run_task off-thread.

    Run records are persisted to disk by `run_store`, so the manager doesn't
    need to track in-flight runs for status. The one thing it *does* track is a
    per-run cancellation `Event` (run control): the cancel endpoint sets it and
    the runner checks it at step boundaries. The registry only holds runs alive
    in *this* process — after a restart it's empty, and the cancel endpoint
    falls back to finalizing an orphaned record directly.
    """

    def __init__(self, max_workers: int = DEFAULT_MAX_WORKERS):
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="coding-agent-run",
        )
        # run_id -> cancellation Event for runs in flight in this process.
        self._cancels: dict[str, Event] = {}
        self._cancel_lock = Lock()

    def submit(self, fn, *args, **kwargs):
        """Run an arbitrary callable on the shared pool (Phase 8: async
        external-action finalize — e.g. polling a Vercel deploy to READY
        off-thread so the confirm request returns immediately). Best-effort: an
        exception in the task is logged, never propagated."""

        def _wrapped():
            try:
                fn(*args, **kwargs)
            except Exception:  # noqa: BLE001
                log.warning("background submit task failed:\n%s", traceback.format_exc())

        return self._executor.submit(_wrapped)

    def dispatch(
        self,
        project_id: str,
        task: TaskSpec,
        *,
        retry_of: str | None = None,
        recovery_of: str | None = None,
        recovery_budget: int = 0,
        orchestration_round: int = 0,
        inherit_checkpoint: dict | None = None,
    ) -> RunRecord:
        """Create the run record + artifacts immediately, then run in the background.

        Returns the placeholder RunRecord (status=RUNNING). The caller can hand
        this back to the user (e.g. as an `@code` chat reply) without waiting
        for the LLM loop. Raises FileNotFoundError if the workspace doesn't
        exist — caller is responsible for ensuring it does. ``retry_of`` links
        this run to the run it was retried from (run control). **Phase 6.1**:
        ``recovery_of`` / ``recovery_budget`` / ``orchestration_round`` carry a
        user-approved bounded recovery contract (a dumb pass-through — the
        clamping + approval check happen at the HTTP boundary).
        """
        ws = get_execution_workspace(project_id)
        if ws is None:
            raise FileNotFoundError(
                f"Execution workspace not initialized for project {project_id!r}"
            )

        run_id = run_store.new_run_id()
        run_store.init_run_dir(project_id, run_id)
        run_store.write_task_card(project_id, run_id, task.title, task.task_card)

        record = RunRecord(
            run_id=run_id,
            project_id=project_id,
            task_title=task.title,
            status=RunStatus.RUNNING,
            retry_of=retry_of,
            recovery_of=recovery_of,
            recovery_budget=max(0, recovery_budget),
            orchestration_round=orchestration_round,
        )

        # Phase 7 — pre-run checkpoint. Best-effort and synchronous (a handful of
        # fast git commands); a failure here NEVER blocks dispatch. A recovery
        # child inherits its parent's checkpoint so the whole chain shares ONE
        # rollback anchor; a fresh run captures a new out-of-branch snapshot.
        try:
            if inherit_checkpoint is not None:
                record.pre_run_checkpoint = inherit_checkpoint.get("ref")
                record.base_commit = inherit_checkpoint.get("base")
                record.checkpoint_tag = inherit_checkpoint.get("tag")
                record.branch = inherit_checkpoint.get("branch")
            else:
                self._create_pre_run_checkpoint(project_id, run_id, record)
        except Exception:  # noqa: BLE001
            log.exception("Pre-run checkpoint wiring failed for run %s", run_id)

        run_store.write_run_json(project_id, run_id, record)
        run_store.append_event(
            project_id,
            run_id,
            {
                "type": "run_dispatched",
                "title": task.title,
                "created_by": task.created_by,
                "recovery_of": recovery_of,
                "recovery_budget": max(0, recovery_budget),
                "orchestration_round": orchestration_round,
            },
        )

        cancel_event = Event()
        with self._cancel_lock:
            self._cancels[run_id] = cancel_event
        self._executor.submit(self._execute, project_id, run_id, task, cancel_event)
        return record

    def request_cancel(self, run_id: str) -> bool:
        """Signal an in-flight run to cancel at its next step boundary.

        Returns ``True`` iff this process is actually running the given run
        (a cancellation Event was registered). ``False`` means the run is not
        in flight here — either it already finished, or the process restarted
        and lost the registry; the caller (the cancel endpoint) then re-reads
        run.json and finalizes an orphaned ``running`` record directly.
        """
        with self._cancel_lock:
            event = self._cancels.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    def _discard_cancel(self, run_id: str) -> None:
        with self._cancel_lock:
            self._cancels.pop(run_id, None)

    def shutdown(self, wait: bool = False) -> None:
        """Tear down the executor. Safe to call multiple times."""
        self._executor.shutdown(wait=wait)

    # ---------- internals ----------

    def _create_pre_run_checkpoint(
        self, project_id: str, run_id: str, record: RunRecord
    ) -> None:
        """Capture an out-of-branch checkpoint of the repo before the run and
        stamp the linkage fields onto ``record`` (persisted by the caller's
        ``write_run_json``). Lazy-imports git_ops to keep the cost off the
        import path. Records an audit event either way."""
        from . import git_ops  # lazy: avoids paying git_ops import on every import

        ck = git_ops.create_checkpoint(project_id, run_id)
        if ck.created:
            record.pre_run_checkpoint = ck.ref
            record.base_commit = ck.base_commit
            record.checkpoint_tag = ck.tag
            record.branch = git_ops.current_branch(project_id)
            run_store.append_event(
                project_id,
                run_id,
                {
                    "type": "checkpoint_created",
                    "ref": (ck.ref or "")[:12],
                    "base": (ck.base_commit or "")[:12],
                    "tag": ck.tag,
                    "branch": record.branch,
                },
            )
        else:
            run_store.append_event(
                project_id,
                run_id,
                {"type": "checkpoint_skipped", "error": (ck.error or "")[:200]},
            )

    def _execute(
        self, project_id: str, run_id: str, task: TaskSpec, cancel_event: Event
    ) -> None:
        try:
            CodingAgentRunner(project_id).run_task(
                task, run_id=run_id, cancel_event=cancel_event
            )
            # Phase 6.1 — only on a CLEAN finalize (a crash routes to
            # _mark_failed, which has no recovery_assessment to act on). Wrapped
            # so a bug in auto-recovery can never flip this (successful) run to
            # failed via the except below.
            try:
                self._maybe_auto_recover(project_id, run_id)
            except Exception:  # noqa: BLE001
                log.exception("Auto-recovery wiring failed for run %s", run_id)
        except Exception as exc:
            self._mark_failed(project_id, run_id, task, exc)
        finally:
            self._discard_cancel(run_id)

    # ---------- auto-recovery (Phase 6.1) ----------

    def _maybe_auto_recover(self, project_id: str, run_id: str) -> None:
        """Auto-dispatch ONE bounded recovery run when the user approved a budget.

        Fires iff the finalized run is non-green, has remaining budget, hasn't
        already been recovered, is under the hard cap, and the Main Agent's
        recovery assessment recommends a concrete follow-up. This is the only
        path that dispatches a run without a per-run user click — and it is
        authorized by the recovery budget the user explicitly set when confirming
        the original execution contract. Audited via events + lineage.
        """
        raw = run_store.read_run_json(project_id, run_id)
        if raw is None:
            return
        try:
            rec = RunRecord(**raw)
        except Exception:  # noqa: BLE001
            return

        # Gates — budget is the source of truth; the rest are guards.
        # Phase 11 — besides the three non-green statuses, a COMPLETED run
        # whose AI visual verdict failed is recover-eligible: the verdict is
        # diagnostic-only (never downgrades status), so without this a granted
        # budget could never repair a visually-broken-but-green build. This
        # matches CLAUDE.md §5's definition of non-green ("… or a failed
        # verification / visual review").
        non_green = rec.status in (RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.BLOCKED)
        visual_failed_green = (
            rec.status == RunStatus.COMPLETED
            and rec.visual_review is not None
            and rec.visual_review.enabled
            and rec.visual_review.status == "failed"
        )
        if not (non_green or visual_failed_green):
            return  # green (or cancelled) — nothing to recover
        if rec.recovery_budget <= 0:
            return  # no user-approved budget
        if rec.orchestration_round >= RECOVERY_HARD_CAP:
            return  # depth cap
        if rec.recovered_by is not None:
            return  # idempotent — already recovered (also blocks the manual path)
        ra = rec.recovery_assessment
        if ra is None or not ra.assessed or ra.verdict != "needs_recovery":
            return
        card = (ra.follow_up_task_card or "").strip()
        if not card:
            return

        # Phase 11 — Recovery Matrix contract enforcement. The contract can
        # only TIGHTEN the user-approved boundary, never loosen it: types that
        # a bounded Coding Agent run can't plausibly fix (integration /
        # deployment / database / product / docs_memory) never auto-dispatch,
        # and environment failures (missing Playwright, occupied port) are
        # excluded even inside an auto-eligible type.
        classification = classify_failure(rec)
        recovery_type = ra.recovery_type or classification.recovery_type
        contract = contract_for(recovery_type)
        if not contract.auto_eligible or not classification.auto_ok:
            return
        # Clamp the child's remaining budget by the contract's cap so e.g. a
        # visual repair is a single bounded pass (child budget 0 = repair once,
        # re-verify once in the child's own tail) regardless of the remaining
        # user budget.
        child_budget = min(rec.recovery_budget - 1, contract.child_budget_cap)

        title = card.split("\n", 1)[0].strip()[:80] or "Auto-recovery"
        child_task = TaskSpec(title=title, task_card=card, created_by="auto_recovery")
        child = self.dispatch(
            project_id,
            child_task,
            recovery_of=run_id,
            recovery_budget=child_budget,
            orchestration_round=rec.orchestration_round + 1,
            # Phase 7 — the recovery chain shares the parent's checkpoint as a
            # single rollback anchor (don't re-anchor mid-chain).
            inherit_checkpoint={
                "ref": rec.pre_run_checkpoint,
                "base": rec.base_commit,
                "tag": rec.checkpoint_tag,
                "branch": rec.branch,
            },
        )

        # Claim the parent (re-read to avoid clobbering a concurrent write).
        fresh = run_store.read_run_json(project_id, run_id)
        if fresh is not None:
            try:
                rec = RunRecord(**fresh)
            except Exception:  # noqa: BLE001
                pass
        rec.recovered_by = child.run_id
        run_store.write_run_json(project_id, run_id, rec)
        run_store.append_event(
            project_id,
            run_id,
            {
                "type": "auto_recovery_dispatched",
                "child_run_id": child.run_id,
                "remaining_budget": max(0, child_budget),
                "recovery_type": recovery_type,
                "orchestration_round": rec.orchestration_round + 1,
            },
        )

    def _mark_failed(
        self,
        project_id: str,
        run_id: str,
        task: TaskSpec,
        exc: Exception,
    ) -> None:
        """Promote a crashed run from RUNNING to FAILED with the error attached.

        Idempotent against the runner having already written a terminal status —
        we only overwrite when the on-disk record is still RUNNING. Otherwise
        the runner reached its own finalize path and we don't want to clobber it.
        """
        error_line = f"background execution crashed: {type(exc).__name__}: {exc}"

        existing = run_store.read_run_json(project_id, run_id)
        if existing is not None:
            try:
                record = RunRecord(**existing)
            except Exception:
                record = RunRecord(
                    run_id=run_id,
                    project_id=project_id,
                    task_title=task.title,
                    status=RunStatus.RUNNING,
                )
        else:
            record = RunRecord(
                run_id=run_id,
                project_id=project_id,
                task_title=task.title,
                status=RunStatus.RUNNING,
            )

        if record.status != RunStatus.RUNNING:
            run_store.append_event(
                project_id,
                run_id,
                {
                    "type": "run_failed_post_finalize",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                },
            )
            return

        record.status = RunStatus.FAILED
        record.completed_at = datetime.utcnow()
        record.blockers = list(record.blockers) + [error_line]
        # Settle every transient sub-status so a crash mid-phase can't leave the
        # UI poll gates treating a terminal run as active forever. Each settle
        # path clears the transient states it can leave behind: this in-process
        # crash handler clears all six; the startup `sweep_stuck_runs` clears all
        # six on still-`running` runs and `sweep_terminal_transient_states` clears
        # the local gates on already-terminal runs; `_finalize_cancelled` and the
        # orphan-cancel endpoint clear theirs.
        run_store._clear_transient_states(record)
        run_store.write_run_json(project_id, run_id, record)
        run_store.append_event(
            project_id,
            run_id,
            {
                "type": "run_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            },
        )

        summary = "Background execution crashed before the run could finalize."
        notes = f"{type(exc).__name__}: {exc}"
        result_md = run_store.render_result_md(record, summary, notes=notes)
        run_store.write_result_md(project_id, run_id, result_md)

        # Task 06.0 — give the reconciler a chance to record this crash as a
        # DECISIONS.md note if the blocker is informative enough. Skip rules
        # will route a generic crash to ``skipped_failed_noisy`` — that's the
        # expected outcome here. Reconciliation is best-effort and cannot
        # affect the failed run state.
        try:
            reconcile_run_memory(project_id, run_id, summary_override=summary)
        except Exception:  # noqa: BLE001
            pass


# ---------- module-level singleton ----------

_default_manager: BackgroundRunManager | None = None
_default_lock = Lock()


def get_default_manager() -> BackgroundRunManager:
    """Return the process-wide BackgroundRunManager, creating it lazily."""
    global _default_manager
    with _default_lock:
        if _default_manager is None:
            _default_manager = BackgroundRunManager()
        return _default_manager


def shutdown_default_manager(wait: bool = False) -> None:
    """Tear down the process-wide manager if it was created."""
    global _default_manager
    with _default_lock:
        if _default_manager is not None:
            _default_manager.shutdown(wait=wait)
            _default_manager = None
