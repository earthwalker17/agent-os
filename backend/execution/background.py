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

import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Lock

from . import run_store
from .manager import get_execution_workspace
from .memory_reconciliation import reconcile_run_memory
from .models import RunRecord, RunStatus, TaskSpec
from .runner import CodingAgentRunner


DEFAULT_MAX_WORKERS = 4


class BackgroundRunManager:
    """Owns a single thread pool for running CodingAgentRunner.run_task off-thread.

    Stateless beyond the executor — the run record itself is persisted to disk
    by `run_store`, so the manager doesn't need to track in-flight runs.
    """

    def __init__(self, max_workers: int = DEFAULT_MAX_WORKERS):
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="coding-agent-run",
        )

    def dispatch(self, project_id: str, task: TaskSpec) -> RunRecord:
        """Create the run record + artifacts immediately, then run in the background.

        Returns the placeholder RunRecord (status=RUNNING). The caller can hand
        this back to the user (e.g. as an `@code` chat reply) without waiting
        for the LLM loop. Raises FileNotFoundError if the workspace doesn't
        exist — caller is responsible for ensuring it does.
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
        )
        run_store.write_run_json(project_id, run_id, record)
        run_store.append_event(
            project_id,
            run_id,
            {
                "type": "run_dispatched",
                "title": task.title,
                "created_by": task.created_by,
            },
        )

        self._executor.submit(self._execute, project_id, run_id, task)
        return record

    def shutdown(self, wait: bool = False) -> None:
        """Tear down the executor. Safe to call multiple times."""
        self._executor.shutdown(wait=wait)

    # ---------- internals ----------

    def _execute(self, project_id: str, run_id: str, task: TaskSpec) -> None:
        try:
            CodingAgentRunner(project_id).run_task(task, run_id=run_id)
        except Exception as exc:
            self._mark_failed(project_id, run_id, task, exc)

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
