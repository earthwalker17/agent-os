"""LLM-driven Coding Agent runner.

Loop shape (kept deliberately small):

    runner.run_task(task)
        -> init run dir + run.json + task_card.md
        -> load AGENT.md + TASK.md
        -> build system prompt + initial user message
        -> for step in 1..MAX_STEPS:
              call LLM (with one JSON-correction retry)
              parse action
              if action == final: break
              if action == tool_call: dispatch to ToolRuntime, append result
        -> overwrite TASK.md (if final.task_md_update non-empty)
           else append a small auto-summary
        -> write result.md + finalize run.json
        -> return ResultSummary

Every repo operation goes through `ToolRuntime`. The runner never imports
`subprocess`, `shutil`, or touches paths under `repo/` directly.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import llm

from .manager import get_execution_workspace, read_task_state, update_task_state
from .models import (
    ExecutionPlan,
    ExecutionTask,
    IntegrationResult,
    RunRecord,
    RunStatus,
    ResultSummary,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from .memory_reconciliation import reconcile_run_memory
from .recovery import assess_run
from .integration import integrate_wave
from .patch_workspace import (
    PatchToolRuntime,
    collect_patch_files,
    get_overlay_root,
    init_patch_workspace,
    write_patch_manifest,
)
from .planner import (
    MAX_PARALLEL_AGENTS,
    MAX_TASKS,
    PlanParseError,
    aggregate_run_status,
    compute_waves,
    degraded_dependencies,
    dependency_failed,
    fallback_plan,
    looks_complex,
    plan_from_dict,
    plan_is_team_eligible,
    task_parallel_eligible,
    task_status_from_final,
    topological_order,
)
from .roles import allowed_tools_for, get_role, patch_coder_prompt
from .prompts import (
    build_continuation_prompt,
    build_correction_prompt,
    build_initial_user_prompt,
    build_plan_system_prompt,
    build_plan_user_prompt,
    build_repair_user_prompt,
    build_system_prompt,
    build_task_unit_user_prompt,
    build_tool_result_prompt,
)
from . import run_store
from .tool_runtime import ToolRuntime
from .tool_models import (
    AppendFileRequest,
    ListFilesRequest,
    ReadFileRequest,
    RunShellRequest,
    SearchFilesRequest,
    ToolResult,
    WriteFileRequest,
)
from .verification import plan_verification, run_verification_specs
from .browser_verification import run_browser_verification
from .visual_judge import run_visual_review


log = logging.getLogger(__name__)


# Bumped from 16 -> 24 (Task 06.2E): a minimal full-stack scaffold (backend
# files + a Vite frontend: package.json, vite config, index.html, a couple of
# src files) can need a dozen-plus write_file steps after any exploration, and
# still leave room for a final action. Verification + repair + browser
# verification all run AFTER the loop, so the agent shouldn't spend steps
# running dev servers or test suites — see prompts.py.
MAX_STEPS = 24

# Bounded budget for ONE post-verification repair pass (Task 06.2E). Repairs
# are targeted fixes informed by the failing command output. Bumped 10 -> 18:
# a pass against subtle cross-file type errors spends several steps reading the
# erroring file + the component/type interfaces it references BEFORE it can
# write; at 12 an investigation-heavy pass exhausted its budget on reads/searches
# without ever writing a fix (mutations=0), which the iterative loop reads as
# "no progress" and stops. 18 leaves comfortable room to read AND write.
MAX_REPAIR_STEPS = 18

# Maximum number of repair→re-verify cycles (autonomy hardening). A single pass
# is rarely enough for a real multi-file build: cross-file type/import errors
# surface in waves, so the first pass fixes most and a re-verify reveals
# stragglers (or a fix exposes a downstream error). Bumped 3 -> 5: a freshly
# generated multi-section app can start with errors spread across ~15-20 files
# (cross-file type drift + strict-tsconfig unused-var/import noise); repair
# converges monotonically (~6-9 files fixed per pass) but at 3 attempts it ran
# one straggler short of green. Each pass is bounded by MAX_REPAIR_STEPS, and the
# loop stops early as soon as the build passes or a pass changes nothing, so the
# extra ceiling only costs time on builds that are actually still converging.
MAX_REPAIR_ATTEMPTS = 5

# Phase 5 — planning + multi-task execution budgets. Kept deliberately small:
# the point of Phase 5 is better STRUCTURE (plan -> per-task loops), not a
# bigger flat budget. The single-task (simple) path still uses MAX_STEPS so its
# behavior — and its tests — are unchanged.
#
# - MAX_PLAN_STEPS: read-only inspection steps the planner may take before it
#   must emit a plan (or we fall back).
# - MAX_TASK_STEPS: per-task tool-loop budget in the multi-task path. Each task
#   is a focused unit, so it needs far fewer steps than a whole monolithic run.
MAX_PLAN_STEPS = 6
# Bumped 12 -> 16 -> 20: a frontend-heavy task unit (several view components + a
# state store + filter/group utils + styles) or a from-scratch scaffold writes
# a dozen-plus files at one write_file per step, plus a little exploration and
# the final action. At 16 the Aegis scaffold task burned all its steps writing
# config files and never reached `final`, so it was marked FAILED. 20 gives
# realistic headroom; the productive-continuation safety net below covers the
# rare unit that still needs more while it is actively writing files.
MAX_TASK_STEPS = 20

# Productive-continuation safety net for a per-task unit (multi-task path only).
# When a task exhausts its step budget WITHOUT finalizing but is still making
# progress (it wrote at least one new file since the last checkpoint), grant it
# one more bounded budget so a genuinely heavy unit can finish instead of dying
# one step short and cascading its dependents. This is "budget extends only for
# productive work", not a bigger flat loop: a unit that stops writing files gets
# no extension. The single-task (simple/fallback) path passes
# max_continuations=0, preserving its legacy exhaustion behavior + tests.
MAX_TASK_CONTINUATIONS = 1
TASK_CONTINUATION_STEPS = MAX_TASK_STEPS

# Output-token budget for every Coding Agent LLM call. The agent emits whole
# files inline as JSON (the write_file ``content`` argument), so a too-small
# budget truncates the file mid-string — the JSON action then fails to parse and
# the task fails (the default 2048 truncated even a moderate component; the
# original fix raised it to 8192). A rich seed-data module is much bigger than a
# component, and at 8192 the Aegis "create seed data" task overflowed and failed
# with "response is not valid JSON" on EVERY attempt. 16384 (~60 KB of file
# content) comfortably covers a large data module plus the JSON envelope; the
# prompt also tells the agent to split a truly huge file across write_file +
# append_file. Sonnet supports far more, so this stays well within model limits.
CODING_AGENT_MAX_TOKENS = 16384

# Tools the planner is allowed to call — strictly read-only. Writing / shell
# happen only in the execution phase. Enforced in the plan loop, not just in
# the prompt.
_PLAN_READONLY_TOOLS = {"list_files", "read_file", "search_files"}

# The LLM transcript can grow; cap any single tool-result payload that we
# round-trip back into the LLM. ToolRuntime already caps individual outputs at
# 20 000 chars, but we want a tighter per-step cap so the loop can take 8
# steps without blowing context.
_MAX_TOOL_OUTPUT_FOR_LLM = 4000

# events.jsonl preview cap — keep the log skimmable, full payloads stay only
# in the live LLM transcript.
_EVENT_PREVIEW_CHARS = 500

# Statuses the *agent* may declare in its ``final`` action. Deliberately does
# NOT include "cancelled": cancellation is a user/runner concern, never
# something the model can claim about itself (see RunStatus.CANCELLED).
_ALLOWED_STATUS = {"completed", "partial", "blocked", "failed"}


class _LLMProtocolError(Exception):
    """Raised when the LLM returns something we cannot parse into an action."""


class _RunCancelled(Exception):
    """Raised internally at a step boundary when a user has requested cancel.

    Caught in :meth:`CodingAgentRunner.run_task` and routed to
    :meth:`_finalize_cancelled`. Raised only from loop tops (never inside
    ``_llm_step`` / ``_dispatch_tool``, whose broad ``except`` clauses would
    otherwise swallow it).
    """


class _UnitContext:
    """Per-task-unit execution state for parallel (team) units (Phase 9).

    The runner instance is stateful (observed lists, the live-metrics record,
    ``_active_unit``) and those fields assume ONE active unit at a time. A
    parallel unit therefore carries its own runtime + observation lists in
    one of these; the coordinator thread merges them into the run-scoped
    state as each unit settles, and remains the sole writer of run.json /
    plan.json. ``ctx=None`` everywhere means "sequential unit" and preserves
    the legacy self-backed behavior byte-identically.

    ``allowed_tools`` (when set) is enforced in the tool loop — a call
    outside the set is bounced back to the model with an explanation, the
    same mechanism the planning loop uses for its read-only gate.
    """

    def __init__(
        self,
        runtime,
        *,
        task_id: str = "",
        role: str = "",
        allowed_tools: frozenset[str] | None = None,
    ):
        self.runtime = runtime
        self.task_id = task_id
        self.role = role
        self.allowed_tools = allowed_tools
        self.observed_files: list[str] = []
        self.observed_cmds: list[str] = []
        self.write_ops: int = 0

    def event_extra(self) -> dict:
        """Extra fields stamped onto this unit's events so a concurrent trace
        can attribute every tool call / LLM step to its task + role."""
        extra: dict = {}
        if self.task_id:
            extra["task_id"] = self.task_id
        if self.role:
            extra["role"] = self.role
        return extra


# ---------- public entry point ----------


class CodingAgentRunner:
    def __init__(self, project_id: str, model: str | None = None):
        self.project_id = project_id
        self.runtime = ToolRuntime(project_id)
        self.model = model  # None -> let llm.chat use its default
        # Diagnostic fallback: track what the agent actually accomplished so
        # files_changed / commands_run still reflect reality when the loop
        # exhausts its step budget before emitting a final action. The
        # final action's lists take precedence when supplied; these
        # ordered, deduplicated lists are used only when the final lists
        # are absent or empty.
        self._observed_files_changed: list[str] = []
        self._observed_commands_run: list[str] = []
        # Total successful write/append operations (NOT deduplicated). The
        # productive-continuation check keys off this, not the deduplicated path
        # list above: a polish/refactor task that OVERWRITES existing files makes
        # real progress without adding new unique paths, so a unique-path delta
        # would wrongly read as "no progress" and deny it a continuation.
        self._observed_write_ops: int = 0
        # AGENT.md text, captured during run_task for the repair pass.
        self._agent_md: str = ""
        # Run control — cooperative cancellation. ``None`` (the default for the
        # stand-alone synchronous path + every existing test) means the cancel
        # checkpoints are inert and the loop is byte-identical to before.
        self._cancel_event: threading.Event | None = None
        # Live observability — progressive run metrics. The active run's record +
        # id, captured in ``run_task`` so ``_persist_live_metrics`` can rewrite
        # run.json (and plan.json for the active task) as files/commands
        # accumulate, instead of only at finalize. ``_active_unit`` + its base
        # offsets attribute the live delta to the task currently executing.
        # All default to inert so the synchronous path + tests are unchanged.
        self._live_run_id: str | None = None
        self._live_record: RunRecord | None = None
        self._active_unit: ExecutionTask | None = None
        self._active_unit_base_f: int = 0
        self._active_unit_base_c: int = 0

    def run_task(
        self,
        task: TaskSpec,
        run_id: str | None = None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ResultSummary:
        """Execute the Coding Agent loop and finalize artifacts.

        If `run_id` is provided, the caller (e.g. BackgroundRunManager) is
        expected to have already created the run directory, task_card.md, and
        an initial run.json with status=running. The runner reuses that record
        instead of allocating a new one. Otherwise the runner does the
        allocation itself (stand-alone synchronous path).
        """
        self._cancel_event = cancel_event

        ws = get_execution_workspace(self.project_id)
        if ws is None:
            raise FileNotFoundError(
                f"Execution workspace not initialized for project {self.project_id!r}"
            )

        if run_id is None:
            run_id = run_store.new_run_id()
            run_store.init_run_dir(self.project_id, run_id)
            run_store.write_task_card(self.project_id, run_id, task.title, task.task_card)
            record = RunRecord(
                run_id=run_id,
                project_id=self.project_id,
                task_title=task.title,
                status=RunStatus.RUNNING,
            )
            run_store.write_run_json(self.project_id, run_id, record)
        else:
            existing = run_store.read_run_json(self.project_id, run_id)
            if existing is not None:
                try:
                    record = RunRecord(**existing)
                except Exception:
                    record = RunRecord(
                        run_id=run_id,
                        project_id=self.project_id,
                        task_title=task.title,
                        status=RunStatus.RUNNING,
                    )
                    run_store.write_run_json(self.project_id, run_id, record)
            else:
                record = RunRecord(
                    run_id=run_id,
                    project_id=self.project_id,
                    task_title=task.title,
                    status=RunStatus.RUNNING,
                )
                run_store.init_run_dir(self.project_id, run_id)
                run_store.write_task_card(self.project_id, run_id, task.title, task.task_card)
                run_store.write_run_json(self.project_id, run_id, record)

        # Capture the active record + id so live tool activity can be flushed to
        # run.json progressively (see _persist_live_metrics). Finalize still owns
        # the authoritative file/command lists; these writes only make the
        # in-progress counts climb for pollers.
        self._live_run_id = run_id
        self._live_record = record

        run_store.append_event(
            self.project_id,
            run_id,
            {
                "type": "run_started",
                "title": task.title,
                "created_by": task.created_by,
            },
        )

        agent_md = Path(ws.agent_md).read_text(encoding="utf-8") if Path(ws.agent_md).exists() else ""
        # Stashed for the planning + per-task + repair passes, which each build
        # their own system prompt.
        self._agent_md = agent_md
        task_md = read_task_state(self.project_id) or ""

        # Phase 5: plan -> execute (task-by-task) -> finalize. The run record
        # stays RUNNING through planning + execution; only _finalize sets a
        # terminal status, so the background crash handler + startup sweep keep
        # working. For a simple task card the plan is a single task and the
        # execution phase runs the original bounded loop verbatim.
        #
        # Run control: a cancel requested mid-planning or mid-execution raises
        # _RunCancelled at the next step boundary and short-circuits to a
        # dedicated cancelled-finalize. The wrap deliberately EXCLUDES
        # _finalize — once a run is producing its terminal result we let
        # verification/browser/reconcile finish rather than abort half-way.
        try:
            plan = self._run_plan_phase(run_id, record, task, task_md)
            final_action, loop_failed_reason = self._run_execution_phase(
                run_id, record, task, plan, task_md
            )
        except _RunCancelled:
            return self._finalize_cancelled(run_id, record, task)

        return self._finalize(run_id, record, task, final_action, loop_failed_reason)

    def _cancelled(self) -> bool:
        """True when a user has requested this run be cancelled."""
        return self._cancel_event is not None and self._cancel_event.is_set()

    # ---------- planning phase (Phase 5) ----------

    def _run_plan_phase(
        self, run_id: str, record: RunRecord, task: TaskSpec, task_md: str
    ) -> ExecutionPlan:
        """Produce + persist the run's execution plan.

        Simple task cards skip the planner entirely (no LLM call) and get a
        single-task plan — this is what keeps the legacy behavior + tests
        intact. Complex cards run a bounded read-only planning loop; any
        failure falls back to a single-task plan. The record stays RUNNING.
        """
        if not looks_complex(task.task_card):
            plan = fallback_plan(task, mode="simple")
        else:
            run_store.append_event(
                self.project_id,
                run_id,
                {"type": "plan_started", "created_by": task.created_by},
            )
            try:
                plan = self._plan_loop(run_id, task, task_md)
            except _RunCancelled:
                # A cancel during planning must propagate to the cancelled
                # finalize, never be swallowed into a fallback plan.
                raise
            except Exception as exc:  # noqa: BLE001 — planning never fails a run
                log.exception("Planning crashed for run %s", run_id)
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {"type": "plan_failed", "error": f"{type(exc).__name__}: {exc}"},
                )
                plan = fallback_plan(task, mode="fallback")

        record.plan = plan
        run_store.write_plan_json(self.project_id, run_id, plan)
        run_store.write_run_json(self.project_id, run_id, record)
        run_store.append_event(
            self.project_id,
            run_id,
            {
                "type": "plan_ready",
                "goal": plan.goal,
                "mode": plan.mode,
                "task_count": len(plan.tasks),
                "tasks": [
                    {"id": t.id, "title": t.title, "depends_on": t.depends_on}
                    for t in plan.tasks
                ],
                "risks": plan.risks,
            },
        )
        return plan

    def _plan_loop(self, run_id: str, task: TaskSpec, task_md: str) -> ExecutionPlan:
        """Bounded read-only inspection loop that ends in a ``plan`` action.

        Returns a parsed :class:`ExecutionPlan`, or a fallback plan when the
        budget is exhausted / the model misbehaves. Restricted to read-only
        tools (enforced here, not just in the prompt).
        """
        system_prompt = build_plan_system_prompt(
            self._agent_md, self.project_id, MAX_PLAN_STEPS, MAX_TASKS
        )
        messages: list[dict[str, str]] = [
            {"role": "user", "content": build_plan_user_prompt(task.title, task.task_card, task_md)}
        ]
        allowed = frozenset({"tool_call", "plan"})

        for step in range(1, MAX_PLAN_STEPS + 1):
            if self._cancelled():
                raise _RunCancelled()
            try:
                action = self._llm_step(
                    run_id, step, system_prompt, messages,
                    allowed_actions=allowed, phase="planning",
                )
            except _LLMProtocolError as exc:
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {"type": "plan_failed", "step": step, "error": str(exc)},
                )
                return fallback_plan(task, mode="fallback")

            act = action.get("action")
            if act == "plan":
                try:
                    return plan_from_dict(action, task)
                except PlanParseError as exc:
                    run_store.append_event(
                        self.project_id,
                        run_id,
                        {"type": "plan_failed", "step": step, "error": str(exc)},
                    )
                    return fallback_plan(task, mode="fallback")

            if act == "tool_call":
                tool_name = (action.get("tool_name") or "").strip()
                if tool_name not in _PLAN_READONLY_TOOLS:
                    # Read-only gate: bounce write/append/run_shell back to the
                    # model instead of executing them during planning.
                    msg = (
                        f"Tool {tool_name!r} is not allowed during planning. Use only "
                        "list_files / read_file / search_files, or emit the plan."
                    )
                    run_store.append_event(
                        self.project_id,
                        run_id,
                        {
                            "type": "tool_result",
                            "step": step,
                            "phase": "planning",
                            "tool_name": tool_name or "unknown",
                            "success": False,
                            "error": msg,
                        },
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": build_tool_result_prompt(
                                tool_name or "unknown", False, msg
                            ),
                        }
                    )
                    continue
                tool_result, dispatched = self._dispatch_tool(
                    run_id, step, action, phase="planning"
                )
                # Read-only — nothing to record in the diagnostic activity lists.
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {
                        "type": "tool_result",
                        "step": step,
                        "phase": "planning",
                        "tool_name": dispatched["tool_name"],
                        "success": tool_result.success,
                        "preview": _truncate(tool_result.output, _EVENT_PREVIEW_CHARS),
                        "error": _truncate(tool_result.error, _EVENT_PREVIEW_CHARS),
                    },
                )
                messages.append(
                    {
                        "role": "user",
                        "content": build_tool_result_prompt(
                            dispatched["tool_name"],
                            tool_result.success,
                            _format_tool_result_for_llm(tool_result),
                        ),
                    }
                )
                continue

            # Shouldn't happen (allowed set enforced) — fall back defensively.
            return fallback_plan(task, mode="fallback")

        run_store.append_event(
            self.project_id,
            run_id,
            {"type": "plan_failed", "step": MAX_PLAN_STEPS, "error": "planning budget exhausted"},
        )
        return fallback_plan(task, mode="fallback")

    # ---------- execution phase (Phase 5) ----------

    def _run_execution_phase(
        self,
        run_id: str,
        record: RunRecord,
        task: TaskSpec,
        plan: ExecutionPlan,
        task_md: str,
    ) -> tuple[dict | None, str | None]:
        """Execute the plan's tasks and return a (final_action, fail_reason).

        Single-task plans run the original bounded loop verbatim and pass the
        agent's raw final action (including ``task_md_update``) straight through
        to ``_finalize`` — so legacy semantics, verification gating, and tests
        are unchanged. Multi-task plans run each task in topological order
        (skipping tasks whose dependency failed) and synthesize an aggregate
        final action.
        """
        tasks = plan.tasks

        # --- single-task (simple / fallback) path: identical to the legacy loop
        if len(tasks) == 1 and tasks[0].status != TaskStatus.SKIPPED:
            unit = tasks[0]
            unit.status = TaskStatus.RUNNING
            system_prompt = build_system_prompt(self._agent_md, self.project_id, MAX_STEPS)
            initial = build_initial_user_prompt(task.title, task.task_card, task_md)
            before_f = len(self._observed_files_changed)
            before_c = len(self._observed_commands_run)
            self._begin_unit(unit, before_f, before_c)
            try:
                final_action, loop_failed_reason = self._run_task_unit(
                    run_id, system_prompt, initial, MAX_STEPS, phase="execution"
                )
            finally:
                self._active_unit = None
            self._apply_unit_result(unit, final_action, loop_failed_reason, before_f, before_c)
            # Pass the raw final action through unchanged (incl. task_md_update).
            return final_action, loop_failed_reason

        # --- team path (Phase 9): wave scheduling + bounded parallel execution
        # + patch-workspace integration. Conservative gate — only LLM-planned
        # multi-task plans with a wave of >= 2 parallel-eligible tasks; every
        # other plan keeps the sequential loop below byte-identical.
        if plan_is_team_eligible(plan):
            return self._run_execution_phase_team(run_id, record, task, plan, task_md)

        # --- multi-task path
        by_id = {t.id: t for t in tasks}
        for idx, unit in enumerate(topological_order(tasks), start=1):
            if self._cancelled():
                raise _RunCancelled()
            if unit.status == TaskStatus.SKIPPED:
                # Pre-skipped (e.g. MAX_TASKS overflow) — surface, don't run.
                reason = unit.blockers[0] if unit.blockers else "skipped"
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {"type": "task_status", "task_id": unit.id, "status": "skipped", "reason": reason},
                )
                continue

            dep_reason = dependency_failed(unit, by_id)
            if dep_reason:
                unit.status = TaskStatus.SKIPPED
                if dep_reason not in unit.blockers:
                    unit.blockers.append(dep_reason)
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {"type": "task_status", "task_id": unit.id, "status": "skipped", "reason": dep_reason},
                )
                self._persist_progress(run_id, record, plan)
                continue

            unit.status = TaskStatus.RUNNING
            run_store.append_event(
                self.project_id,
                run_id,
                {"type": "task_started", "task_id": unit.id, "title": unit.title},
            )
            self._persist_progress(run_id, record, plan)

            # Surface any dependency that didn't cleanly complete but didn't
            # block this task (progress-aware skip) so the agent can compensate.
            degraded = degraded_dependencies(unit, by_id)
            system_prompt = build_system_prompt(self._agent_md, self.project_id, MAX_TASK_STEPS)
            initial = build_task_unit_user_prompt(
                goal=plan.goal,
                task_no=idx,
                task_total=len(tasks),
                task_id=unit.id,
                title=unit.title,
                description=unit.description,
                plan_outline=_render_plan_outline(tasks, current_id=unit.id),
                prior_context=_render_prior_context(tasks),
                task_md=task_md,
                degraded_dependencies=degraded,
            )
            before_f = len(self._observed_files_changed)
            before_c = len(self._observed_commands_run)
            self._begin_unit(unit, before_f, before_c)
            try:
                final_action, loop_failed_reason = self._run_task_unit(
                    run_id,
                    system_prompt,
                    initial,
                    MAX_TASK_STEPS,
                    phase="execution",
                    max_continuations=MAX_TASK_CONTINUATIONS,
                    continuation_steps=TASK_CONTINUATION_STEPS,
                )
            finally:
                self._active_unit = None
            self._apply_unit_result(unit, final_action, loop_failed_reason, before_f, before_c)
            run_store.append_event(
                self.project_id,
                run_id,
                {
                    "type": "task_status",
                    "task_id": unit.id,
                    "status": unit.status.value,
                    "summary": _truncate(unit.summary, 200),
                    "files_changed": unit.files_changed,
                    "commands_run": unit.commands_run,
                    "blockers": unit.blockers,
                },
            )
            self._persist_progress(run_id, record, plan)

        status, summary, blockers = aggregate_run_status(tasks)
        synthetic = {
            "action": "final",
            "status": status,
            "summary": summary,
            "files_changed": _dedup([f for t in tasks for f in t.files_changed]),
            "commands_run": _dedup([c for t in tasks for c in t.commands_run]),
            "blockers": blockers,
            # The runner owns TASK.md across a multi-task run (auto-summary);
            # never honor a per-task TASK.md overwrite.
            "task_md_update": "",
        }
        return synthetic, None

    # ---------- team execution phase (Phase 9) ----------

    def _run_execution_phase_team(
        self,
        run_id: str,
        record: RunRecord,
        task: TaskSpec,
        plan: ExecutionPlan,
        task_md: str,
    ) -> tuple[dict | None, str | None]:
        """Wave-scheduled team execution with bounded parallelism (Phase 9).

        The plan's task graph is layered into topological *waves* (tasks in
        one wave never depend on each other). Within a wave, parallel-eligible
        tasks run concurrently on a dedicated bounded pool — coders inside
        isolated patch workspaces, read-only roles (reviewer / inspector)
        against the shared repo — then the wave's patches are integrated into
        the shared repo deterministically (conflicts surfaced, never silent),
        and any remaining wave tasks run sequentially in the main workspace
        with the full tool set. The coordinator (this thread) is the ONLY
        writer of run.json / plan.json / integration state; workers only
        append (locked) attributed events.

        The global verification gate is unchanged: the synthetic aggregate
        final action flows into ``_finalize``, whose command/browser/visual
        verification now runs over the *integrated* tree — a team run can
        only be ``completed`` when the integrated result passes. Unresolved
        integration conflicts cap the aggregate at ``partial``.
        """
        tasks = plan.tasks
        by_id = {t.id: t for t in tasks}
        idx_map = {t.id: i for i, t in enumerate(tasks, start=1)}
        total = len(tasks)

        # Surface pre-skipped (plan-cap overflow) tasks, mirroring the
        # sequential path.
        for unit in tasks:
            if unit.status == TaskStatus.SKIPPED:
                reason = unit.blockers[0] if unit.blockers else "skipped"
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {"type": "task_status", "task_id": unit.id, "status": "skipped", "reason": reason},
                )

        runnable = [t for t in tasks if t.status != TaskStatus.SKIPPED]
        waves, cyclic = compute_waves(runnable)
        plan.execution_mode = "team"
        for wave_no, wave in enumerate(waves, start=1):
            for t in wave:
                t.wave = wave_no

        integration_agg = IntegrationResult(enabled=True)
        record.integration = integration_agg
        wave_details: list[dict] = []
        # Files that failed to APPLY at integration (distinct from a conflict —
        # here NO version landed anywhere). Tracked so the aggregate never
        # finishes green with silently dropped work.
        integration_errors: list[str] = []
        self._persist_progress(run_id, record, plan)
        run_store.append_event(
            self.project_id,
            run_id,
            {
                "type": "team_execution_started",
                "waves": [[t.id for t in w] for w in waves],
                "max_parallel": MAX_PARALLEL_AGENTS,
            },
        )

        for wave_no, wave in enumerate(waves, start=1):
            if self._cancelled():
                raise _RunCancelled()

            # Dependency gate for this wave (deps settled in earlier waves).
            ready: list[ExecutionTask] = []
            for unit in wave:
                dep_reason = dependency_failed(unit, by_id)
                if dep_reason:
                    unit.status = TaskStatus.SKIPPED
                    if dep_reason not in unit.blockers:
                        unit.blockers.append(dep_reason)
                    run_store.append_event(
                        self.project_id,
                        run_id,
                        {
                            "type": "task_status",
                            "task_id": unit.id,
                            "status": "skipped",
                            "reason": dep_reason,
                            "role": unit.role,
                            "wave": unit.wave,
                        },
                    )
                    self._persist_progress(run_id, record, plan)
                    continue
                ready.append(unit)
            if not ready:
                continue

            batch = [
                u for u in ready if u.id not in cyclic and task_parallel_eligible(u)
            ]
            if len(batch) < 2:
                batch = []  # a lone task gains nothing from a pool — run it inline
            rest = [u for u in ready if u not in batch]

            if batch:
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {
                        "type": "wave_started",
                        "wave": wave_no,
                        "parallel": [u.id for u in batch],
                        "sequential": [u.id for u in rest],
                    },
                )
                self._run_parallel_batch(
                    run_id, record, plan, batch, idx_map, total, task_md, by_id
                )

                # Integration: apply the wave's patch workspaces into the
                # shared repo before any dependent work runs.
                patch_units = [u for u in batch if u.workspace == "patch"]
                if patch_units:
                    record.integration_state = "integrating"
                    self._persist_progress(run_id, record, plan)
                    run_store.append_event(
                        self.project_id,
                        run_id,
                        {
                            "type": "integration_started",
                            "wave": wave_no,
                            "tasks": [u.id for u in patch_units],
                        },
                    )
                    wave_integ = integrate_wave(
                        self.project_id, run_id, wave_no, patch_units, self.runtime
                    )
                    integration_agg.waves += 1
                    for p in wave_integ.applied:
                        if p not in integration_agg.files_applied:
                            integration_agg.files_applied.append(p)
                    integration_agg.conflicts.extend(wave_integ.conflicts)
                    if wave_integ.errors:
                        integration_errors.extend(wave_integ.errors)
                        note = f"{len(wave_integ.errors)} file(s) failed to apply"
                        integration_agg.notes = (
                            f"{integration_agg.notes}; {note}".strip("; ")
                            if integration_agg.notes
                            else note
                        )
                    wave_details.append(wave_integ.to_dict())
                    try:
                        run_store.write_integration_json(
                            self.project_id, run_id, {"waves": wave_details}
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("integration.json write failed for run %s", run_id)
                    for c in wave_integ.conflicts:
                        run_store.append_event(
                            self.project_id,
                            run_id,
                            {
                                "type": "integration_conflict",
                                "wave": wave_no,
                                "path": c.path,
                                "applied_task": c.applied_task,
                                "rejected_task": c.rejected_task,
                            },
                        )
                    run_store.append_event(
                        self.project_id,
                        run_id,
                        {
                            "type": "integration_completed",
                            "wave": wave_no,
                            "applied": len(wave_integ.applied),
                            "conflicts": len(wave_integ.conflicts),
                            "errors": len(wave_integ.errors),
                        },
                    )
                    record.integration_state = None
                    self._persist_progress(run_id, record, plan)
                if self._cancelled():
                    raise _RunCancelled()

            for unit in rest:
                if self._cancelled():
                    raise _RunCancelled()
                # Re-check dependencies just-in-time (not only at wave entry): a
                # cyclic-wave sibling may have just failed without output, in
                # which case this task must skip exactly as the sequential path
                # would — the wave-entry gate saw the sibling still PENDING.
                dep_reason = dependency_failed(unit, by_id)
                if dep_reason:
                    unit.status = TaskStatus.SKIPPED
                    if dep_reason not in unit.blockers:
                        unit.blockers.append(dep_reason)
                    run_store.append_event(
                        self.project_id,
                        run_id,
                        {
                            "type": "task_status",
                            "task_id": unit.id,
                            "status": "skipped",
                            "reason": dep_reason,
                            "role": unit.role,
                            "wave": unit.wave,
                        },
                    )
                    self._persist_progress(run_id, record, plan)
                    continue
                self._run_wave_unit_sequential(
                    run_id, record, plan, unit, idx_map[unit.id], total, task_md, by_id
                )

        status, summary, blockers = aggregate_run_status(tasks)
        summary += (
            f" Team execution: {len(waves)} wave(s), "
            f"{integration_agg.waves} integrated, "
            f"{len(integration_agg.files_applied)} file(s) applied, "
            f"{len(integration_agg.conflicts)} conflict(s)."
        )
        if integration_agg.conflicts or integration_errors:
            # A conflicted OR error-hit integration is never silently green — an
            # apply error means NO version of a file landed, which is strictly
            # worse than a conflict, so it degrades status the same way.
            if status == "completed":
                status = "partial"
            for c in integration_agg.conflicts:
                line = (
                    f"integration conflict on {c.path!r}: applied {c.applied_task}, "
                    f"rejected {c.rejected_task} (wave {c.wave})"
                )
                if line not in blockers:
                    blockers.append(line)
            for err in integration_errors:
                line = f"integration failed to apply a file: {err}"
                if line not in blockers:
                    blockers.append(line)
        synthetic = {
            "action": "final",
            "status": status,
            "summary": summary,
            "files_changed": _dedup([f for t in tasks for f in t.files_changed]),
            "commands_run": _dedup([c for t in tasks for c in t.commands_run]),
            "blockers": blockers,
            "task_md_update": "",
        }
        return synthetic, None

    def _run_parallel_batch(
        self,
        run_id: str,
        record: RunRecord,
        plan: ExecutionPlan,
        batch: list[ExecutionTask],
        idx_map: dict[str, int],
        total: int,
        task_md: str,
        by_id: dict[str, ExecutionTask],
    ) -> None:
        """Run one wave's parallel-eligible units concurrently (bounded).

        A dedicated per-batch pool (NEVER the shared BackgroundRunManager
        pool — a run occupying a shared worker that then blocks on futures in
        the same pool can deadlock it). Prompts are snapshotted on the
        coordinator thread before submission so workers never read mutating
        plan state; each unit gets its own `_UnitContext` (runtime + activity
        lists + enforced tool set); the coordinator settles results with
        ``as_completed`` and remains the sole run.json/plan.json writer.
        """
        contexts: dict[str, _UnitContext] = {}
        prompts: dict[str, tuple[str, str]] = {}

        for unit in batch:
            role = get_role(unit.role)
            if role.read_only:
                unit.workspace = "main"
                runtime = self.runtime  # ToolRuntime is stateless; reads are safe
            else:
                unit.workspace = "patch"
                overlay = init_patch_workspace(self.project_id, run_id, unit.id)
                runtime = PatchToolRuntime(self.project_id, overlay)
            contexts[unit.id] = _UnitContext(
                runtime,
                task_id=unit.id,
                role=unit.role,
                allowed_tools=allowed_tools_for(
                    unit.role, in_patch_workspace=(unit.workspace == "patch")
                ),
            )
            unit.status = TaskStatus.RUNNING
            run_store.append_event(
                self.project_id,
                run_id,
                {
                    "type": "task_started",
                    "task_id": unit.id,
                    "title": unit.title,
                    "role": unit.role,
                    "wave": unit.wave,
                    "workspace": unit.workspace,
                    "parallel": True,
                },
            )

        # Snapshot prompts AFTER all batch units are marked RUNNING so each
        # unit's plan outline shows its siblings in flight.
        for unit in batch:
            role = get_role(unit.role)
            degraded = degraded_dependencies(unit, by_id)
            system_prompt = build_system_prompt(
                self._agent_md, self.project_id, MAX_TASK_STEPS, role_block=role.prompt
            )
            role_note = patch_coder_prompt() if unit.workspace == "patch" else ""
            initial = build_task_unit_user_prompt(
                goal=plan.goal,
                task_no=idx_map[unit.id],
                task_total=total,
                task_id=unit.id,
                title=unit.title,
                description=unit.description,
                plan_outline=_render_plan_outline(plan.tasks, current_id=unit.id),
                prior_context=_render_prior_context(plan.tasks),
                task_md=task_md,
                degraded_dependencies=degraded,
                role_note=role_note,
            )
            prompts[unit.id] = (system_prompt, initial)

        self._persist_progress(run_id, record, plan)

        cancelled = False
        with ThreadPoolExecutor(
            max_workers=min(MAX_PARALLEL_AGENTS, len(batch)),
            thread_name_prefix=f"team-{run_id[-8:]}",
        ) as pool:
            futures = {
                pool.submit(
                    self._run_parallel_unit,
                    run_id,
                    prompts[unit.id][0],
                    prompts[unit.id][1],
                    contexts[unit.id],
                ): unit
                for unit in batch
            }
            for fut in as_completed(futures):
                unit = futures[fut]
                ctx = contexts[unit.id]
                try:
                    final_action, fail_reason, unit_cancelled = fut.result()
                except Exception as exc:  # noqa: BLE001 — defensive; worker catches its own
                    final_action, fail_reason, unit_cancelled = (
                        None,
                        f"parallel unit crashed: {type(exc).__name__}: {exc}",
                        False,
                    )
                if unit_cancelled:
                    # Leave the unit RUNNING — _finalize_cancelled settles every
                    # in-flight task to SKIPPED with a clear reason.
                    cancelled = True
                    continue
                self._apply_unit_result_from_lists(
                    unit,
                    final_action,
                    fail_reason,
                    list(ctx.observed_files),
                    list(ctx.observed_cmds),
                )
                # Merge the unit's activity into the run-scoped observation
                # lists (coordinator thread only) + live record counts.
                for p in ctx.observed_files:
                    if p not in self._observed_files_changed:
                        self._observed_files_changed.append(p)
                for c in ctx.observed_cmds:
                    if c not in self._observed_commands_run:
                        self._observed_commands_run.append(c)
                self._observed_write_ops += ctx.write_ops
                if record.status == RunStatus.RUNNING:
                    record.files_changed = list(self._observed_files_changed)
                    record.commands_run = list(self._observed_commands_run)
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {
                        "type": "task_status",
                        "task_id": unit.id,
                        "status": unit.status.value,
                        "summary": _truncate(unit.summary, 200),
                        "files_changed": unit.files_changed,
                        "commands_run": unit.commands_run,
                        "blockers": unit.blockers,
                        "role": unit.role,
                        "wave": unit.wave,
                        "workspace": unit.workspace,
                    },
                )
                if unit.workspace == "patch":
                    try:
                        overlay = get_overlay_root(self.project_id, run_id, unit.id)
                        write_patch_manifest(
                            self.project_id,
                            run_id,
                            unit.id,
                            {
                                "run_id": run_id,
                                "task_id": unit.id,
                                "title": unit.title,
                                "role": unit.role,
                                "wave": unit.wave,
                                "status": unit.status.value,
                                "summary": unit.summary,
                                "files": collect_patch_files(overlay),
                                "commands_run": unit.commands_run,
                                "blockers": unit.blockers,
                            },
                        )
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "patch manifest write failed for %s/%s", run_id, unit.id
                        )
                self._persist_progress(run_id, record, plan)

        if cancelled or self._cancelled():
            raise _RunCancelled()

    def _run_parallel_unit(
        self,
        run_id: str,
        system_prompt: str,
        initial_user_prompt: str,
        ctx: _UnitContext,
    ) -> tuple[dict | None, str | None, bool]:
        """One parallel unit, executed on a pool worker thread.

        Fully self-contained via ``ctx`` and never raises: exceptions fold
        into a failed outcome (one crashing agent must not kill the run), and
        a cooperative cancel returns a ``cancelled`` marker for the
        coordinator to act on. Returns ``(final_action, fail_reason,
        cancelled)``.
        """
        try:
            final_action, fail_reason = self._run_task_unit(
                run_id,
                system_prompt,
                initial_user_prompt,
                MAX_TASK_STEPS,
                phase="execution",
                max_continuations=MAX_TASK_CONTINUATIONS,
                continuation_steps=TASK_CONTINUATION_STEPS,
                ctx=ctx,
            )
            return final_action, fail_reason, False
        except _RunCancelled:
            return None, "run cancelled by user", True
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "Parallel unit %s crashed for run %s", ctx.task_id or "?", run_id
            )
            return None, f"parallel unit crashed: {type(exc).__name__}: {exc}", False

    def _run_wave_unit_sequential(
        self,
        run_id: str,
        record: RunRecord,
        plan: ExecutionPlan,
        unit: ExecutionTask,
        task_no: int,
        total: int,
        task_md: str,
        by_id: dict[str, ExecutionTask],
    ) -> None:
        """Run one team-wave task sequentially in the main workspace.

        Mirrors the legacy multi-task loop body (full tool set for coders,
        live metrics attribution), plus role awareness: a read-only role gets
        its tool set enforced through a `_UnitContext` even though it runs on
        the coordinator thread.
        """
        unit.status = TaskStatus.RUNNING
        unit.workspace = "main"
        run_store.append_event(
            self.project_id,
            run_id,
            {
                "type": "task_started",
                "task_id": unit.id,
                "title": unit.title,
                "role": unit.role,
                "wave": unit.wave,
                "workspace": unit.workspace,
            },
        )
        self._persist_progress(run_id, record, plan)

        role = get_role(unit.role)
        degraded = degraded_dependencies(unit, by_id)
        system_prompt = build_system_prompt(
            self._agent_md, self.project_id, MAX_TASK_STEPS, role_block=role.prompt
        )
        initial = build_task_unit_user_prompt(
            goal=plan.goal,
            task_no=task_no,
            task_total=total,
            task_id=unit.id,
            title=unit.title,
            description=unit.description,
            plan_outline=_render_plan_outline(plan.tasks, current_id=unit.id),
            prior_context=_render_prior_context(plan.tasks),
            task_md=task_md,
            degraded_dependencies=degraded,
        )
        before_f = len(self._observed_files_changed)
        before_c = len(self._observed_commands_run)
        self._begin_unit(unit, before_f, before_c)
        try:
            if role.read_only:
                ctx = _UnitContext(
                    self.runtime,
                    task_id=unit.id,
                    role=unit.role,
                    allowed_tools=allowed_tools_for(unit.role),
                )
                final_action, fail_reason = self._run_task_unit(
                    run_id,
                    system_prompt,
                    initial,
                    MAX_TASK_STEPS,
                    phase="execution",
                    max_continuations=MAX_TASK_CONTINUATIONS,
                    continuation_steps=TASK_CONTINUATION_STEPS,
                    ctx=ctx,
                )
                unit_files = list(ctx.observed_files)
                unit_cmds = list(ctx.observed_cmds)
            else:
                final_action, fail_reason = self._run_task_unit(
                    run_id,
                    system_prompt,
                    initial,
                    MAX_TASK_STEPS,
                    phase="execution",
                    max_continuations=MAX_TASK_CONTINUATIONS,
                    continuation_steps=TASK_CONTINUATION_STEPS,
                    event_extra={"task_id": unit.id, "role": unit.role},
                )
                unit_files = self._observed_files_changed[before_f:]
                unit_cmds = self._observed_commands_run[before_c:]
        finally:
            self._active_unit = None
        self._apply_unit_result_from_lists(
            unit, final_action, fail_reason, unit_files, unit_cmds
        )
        run_store.append_event(
            self.project_id,
            run_id,
            {
                "type": "task_status",
                "task_id": unit.id,
                "status": unit.status.value,
                "summary": _truncate(unit.summary, 200),
                "files_changed": unit.files_changed,
                "commands_run": unit.commands_run,
                "blockers": unit.blockers,
                "role": unit.role,
                "wave": unit.wave,
                "workspace": unit.workspace,
            },
        )
        self._persist_progress(run_id, record, plan)

    def _run_task_unit(
        self,
        run_id: str,
        system_prompt: str,
        initial_user_prompt: str,
        max_steps: int,
        *,
        phase: str = "execution",
        max_continuations: int = 0,
        continuation_steps: int | None = None,
        ctx: _UnitContext | None = None,
        event_extra: dict | None = None,
    ) -> tuple[dict | None, str | None]:
        """One bounded JSON tool loop. Returns (final_action, fail_reason).

        This is the original single-loop body, extracted so the simple path and
        every multi-task unit run identical machinery. Uses a fresh ``messages``
        list (per-task isolation). Observed file/command activity accumulates
        into the run-scoped ``self._observed_*`` lists (the diagnostic fallback
        + repair-delta in ``_finalize`` depend on run-scope).

        Productive continuation (hardening): if the step budget is exhausted
        WITHOUT a ``final`` action but the agent wrote at least one new file
        since the last checkpoint, the budget is extended by
        ``continuation_steps`` (up to ``max_continuations`` times) with a short
        "finish up" nudge — so a heavy-but-progressing unit (e.g. a from-scratch
        scaffold) can finish instead of dying one step short and cascading its
        dependents. A unit that has stopped producing files gets no extension.
        ``max_continuations=0`` (the default, used by the single-task path)
        preserves the legacy exhaustion behavior + its regression tests.

        Phase 9: a non-``None`` ``ctx`` makes this unit self-contained for
        parallel execution — tools dispatch through ``ctx.runtime``, activity
        accumulates in ``ctx``'s lists, ``ctx.allowed_tools`` is enforced,
        events carry the task id/role, and the loop never writes run.json
        (the coordinator owns all record writes).
        """
        messages: list[dict[str, str]] = [{"role": "user", "content": initial_user_prompt}]
        final_action: dict | None = None
        loop_failed_reason: str | None = None
        # Event attribution: a parallel unit derives it from its ctx; a
        # sequential team unit passes it explicitly (``event_extra``) so the
        # team trace attributes EVERY unit's activity. Legacy callers pass
        # neither and their events stay byte-identical.
        if ctx is not None:
            event_extra = ctx.event_extra()
        elif event_extra:
            event_extra = dict(event_extra)
        else:
            event_extra = None

        def _writes_so_far() -> int:
            return ctx.write_ops if ctx is not None else self._observed_write_ops

        cont_steps = continuation_steps if continuation_steps is not None else max_steps
        budget = max_steps
        continuations_used = 0
        # Progress is measured in successful WRITE OPERATIONS (incl. overwrites),
        # not unique file paths — a polish/refactor unit that rewrites existing
        # files is making progress even though it adds no new paths.
        writes_at_checkpoint = _writes_so_far()
        step = 0

        while True:
            step += 1
            if step > budget:
                # Budget exhausted without a final action. Grant a bounded
                # continuation only while the unit is still productive.
                progressed = _writes_so_far() > writes_at_checkpoint
                if continuations_used < max_continuations and progressed:
                    continuations_used += 1
                    writes_at_checkpoint = _writes_so_far()
                    budget += cont_steps
                    run_store.append_event(
                        self.project_id,
                        run_id,
                        {
                            "type": "task_continued",
                            "step": step - 1,
                            "phase": phase,
                            "granted_steps": cont_steps,
                            "continuation": continuations_used,
                            **(event_extra or {}),
                        },
                    )
                    messages.append(
                        {"role": "user", "content": build_continuation_prompt(cont_steps)}
                    )
                    # fall through and run another step under the extended budget
                else:
                    loop_failed_reason = f"step budget exhausted after {budget} steps"
                    run_store.append_event(
                        self.project_id,
                        run_id,
                        {"type": "run_failed", "step": budget, "phase": phase, "error": loop_failed_reason, **(event_extra or {})},
                    )
                    break

            if self._cancelled():
                raise _RunCancelled()
            try:
                action = self._llm_step(
                    run_id, step, system_prompt, messages, phase=phase, event_extra=event_extra
                )
            except _LLMProtocolError as e:
                loop_failed_reason = f"LLM protocol error after retry: {e}"
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {"type": "run_failed", "step": step, "phase": phase, "error": loop_failed_reason, **(event_extra or {})},
                )
                break

            if action.get("action") == "final":
                final_action = action
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {
                        "type": "run_completed",
                        "step": step,
                        "phase": phase,
                        "status": action.get("status"),
                        **(event_extra or {}),
                    },
                )
                break

            if action.get("action") == "tool_call":
                # Cheap extra checkpoint: honor a cancel that arrived during the
                # (blocking) LLM call before we spend a tool dispatch on it.
                if self._cancelled():
                    raise _RunCancelled()
                # Phase 9 — role tool gate (enforced, not prompt-only): bounce a
                # call outside the unit's allowed set back to the model, the
                # same way the planning loop guards its read-only tools.
                if ctx is not None and ctx.allowed_tools is not None:
                    gate_tool = (action.get("tool_name") or "").strip()
                    if gate_tool not in ctx.allowed_tools:
                        msg = (
                            f"Tool {gate_tool!r} is not available to the "
                            f"{ctx.role or 'assigned'} role for this task. Allowed tools: "
                            f"{', '.join(sorted(ctx.allowed_tools))}. Adapt your approach "
                            "or finalize."
                        )
                        run_store.append_event(
                            self.project_id,
                            run_id,
                            {
                                "type": "tool_result",
                                "step": step,
                                "phase": phase,
                                "tool_name": gate_tool or "unknown",
                                "success": False,
                                "error": msg,
                                **(event_extra or {}),
                            },
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": build_tool_result_prompt(
                                    gate_tool or "unknown", False, msg
                                ),
                            }
                        )
                        continue
                tool_result, dispatched = self._dispatch_tool(
                    run_id, step, action, phase=phase, ctx=ctx, event_extra=event_extra
                )
                self._observe_tool_result(action, tool_result, ctx=ctx)
                # Flush progressive metrics so pollers see counts climb live.
                # Parallel units skip this — the coordinator owns run.json.
                if ctx is None:
                    self._persist_live_metrics()
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {
                        "type": "tool_result",
                        "step": step,
                        "phase": phase,
                        "tool_name": dispatched["tool_name"],
                        "success": tool_result.success,
                        "preview": _truncate(tool_result.output, _EVENT_PREVIEW_CHARS),
                        "error": _truncate(tool_result.error, _EVENT_PREVIEW_CHARS),
                        **(event_extra or {}),
                    },
                )
                # Feed result back into the conversation
                tool_payload = _format_tool_result_for_llm(tool_result)
                messages.append(
                    {
                        "role": "user",
                        "content": build_tool_result_prompt(
                            dispatched["tool_name"], tool_result.success, tool_payload
                        ),
                    }
                )
                continue

            # Unknown / malformed action shape — treat as protocol error.
            loop_failed_reason = f"unknown action shape: {action!r}"
            run_store.append_event(
                self.project_id,
                run_id,
                {"type": "run_failed", "step": step, "phase": phase, "error": loop_failed_reason, **(event_extra or {})},
            )
            break

        return final_action, loop_failed_reason

    def _apply_unit_result(
        self,
        unit: "ExecutionTask",
        final_action: dict | None,
        loop_failed_reason: str | None,
        before_f: int,
        before_c: int,
    ) -> None:
        """Record one task unit's outcome onto its :class:`ExecutionTask`.

        Per-task file/command attribution comes from a snapshot delta of the
        run-scoped observed lists (the lists themselves are NOT reset per task).
        The agent's explicit final-action lists win when present.
        """
        self._apply_unit_result_from_lists(
            unit,
            final_action,
            loop_failed_reason,
            self._observed_files_changed[before_f:],
            self._observed_commands_run[before_c:],
        )

    def _apply_unit_result_from_lists(
        self,
        unit: "ExecutionTask",
        final_action: dict | None,
        loop_failed_reason: str | None,
        unit_files: list[str],
        unit_cmds: list[str],
    ) -> None:
        """Core of :meth:`_apply_unit_result` with explicit activity lists —
        parallel units pass their own ``_UnitContext`` lists (Phase 9)."""
        if final_action is not None:
            raw_status = final_action.get("status", "")
            if raw_status not in _ALLOWED_STATUS:
                raw_status = "failed"
            unit.status = task_status_from_final(raw_status)
            unit.summary = str(final_action.get("summary", "")).strip()
            fa_files = _coerce_str_list(final_action.get("files_changed"))
            fa_cmds = _coerce_str_list(final_action.get("commands_run"))
            unit.files_changed = fa_files or unit_files
            unit.commands_run = fa_cmds or unit_cmds
            blockers = _coerce_str_list(final_action.get("blockers"))
            if raw_status != "completed" and not blockers:
                blockers = [f"task ended with status {raw_status}"]
            unit.blockers = blockers
        else:
            unit.status = TaskStatus.FAILED
            unit.summary = loop_failed_reason or "task did not finalize"
            unit.files_changed = unit_files
            unit.commands_run = unit_cmds
            unit.blockers = [loop_failed_reason] if loop_failed_reason else ["did not finalize"]

    def _persist_progress(
        self, run_id: str, record: RunRecord, plan: ExecutionPlan
    ) -> None:
        """Write run.json + plan.json mid-execution so polls see live progress."""
        record.plan = plan
        run_store.write_plan_json(self.project_id, run_id, plan)
        run_store.write_run_json(self.project_id, run_id, record)

    def _persist_live_metrics(self) -> None:
        """Flush observed file/command activity to run.json mid-execution.

        Called after every side-effecting tool result so the right-side Runs
        panel, chat card, and Live Trace see files/commands counts climb *during*
        a run instead of snapping to the final values at finalize. Only the
        observed (progressive) lists are written here; ``_finalize`` still sets
        the authoritative file/command lists (the agent's ``final`` action for a
        single task, the multi-task aggregation otherwise), so run semantics and
        every existing test are unchanged.

        Bounded + safe:
        - No-op unless a run is active and still ``RUNNING`` — so it never
          clobbers a settled / cancelled record, and never fires during the
          post-terminal repair pass.
        - Writes only when the deduped observed lists actually changed
          (``_observe_tool_result`` appends only *new* paths/commands), so this
          costs at most one run.json write per new file or command, not one per
          tool call.
        - When a task unit is executing, also attributes the live delta to that
          unit and rewrites plan.json, so a long task's per-task progress shows
          before its terminal ``task_status``.
        - Appends no events (the granular tool_call/tool_result events already
          carry the per-op detail); this only updates the structured counts.
        """
        record = self._live_record
        run_id = self._live_run_id
        if record is None or run_id is None:
            return
        if record.status != RunStatus.RUNNING:
            return

        changed = False
        if record.files_changed != self._observed_files_changed:
            record.files_changed = list(self._observed_files_changed)
            changed = True
        if record.commands_run != self._observed_commands_run:
            record.commands_run = list(self._observed_commands_run)
            changed = True

        plan_changed = False
        unit = self._active_unit
        if unit is not None:
            unit_files = self._observed_files_changed[self._active_unit_base_f:]
            unit_cmds = self._observed_commands_run[self._active_unit_base_c:]
            if unit.files_changed != unit_files:
                unit.files_changed = list(unit_files)
                plan_changed = True
            if unit.commands_run != unit_cmds:
                unit.commands_run = list(unit_cmds)
                plan_changed = True

        if not (changed or plan_changed):
            return
        if plan_changed and record.plan is not None:
            run_store.write_plan_json(self.project_id, run_id, record.plan)
        run_store.write_run_json(self.project_id, run_id, record)

    def _begin_unit(self, unit: "ExecutionTask", before_f: int, before_c: int) -> None:
        """Mark ``unit`` as the live-attribution target for _persist_live_metrics."""
        self._active_unit = unit
        self._active_unit_base_f = before_f
        self._active_unit_base_c = before_c

    # ---------- internals ----------

    def _llm_step(
        self,
        run_id: str,
        step: int,
        system_prompt: str,
        messages: list[dict[str, str]],
        *,
        allowed_actions: frozenset[str] = frozenset({"tool_call", "final"}),
        phase: str = "execution",
        event_extra: dict | None = None,
    ) -> dict:
        """One LLM call with a single correction retry. Mutates `messages` in place.

        ``allowed_actions`` constrains which ``action`` values the parser
        accepts — the execution/repair loops allow ``tool_call`` / ``final``;
        the planning loop allows ``tool_call`` / ``plan``. This keeps the
        ``plan`` action from ever being accepted by the execution loop.
        ``event_extra`` (Phase 9) stamps parallel-unit attribution fields
        (task_id / role) onto the emitted events.
        """
        kwargs: dict[str, Any] = {
            "system": system_prompt,
            "messages": messages,
            "max_tokens": CODING_AGENT_MAX_TOKENS,
        }
        if self.model:
            kwargs["model"] = self.model

        try:
            raw = llm.chat(**kwargs)
        except Exception as e:
            raise _LLMProtocolError(f"LLM call failed: {e}")

        run_store.append_event(
            self.project_id,
            run_id,
            {
                "type": "llm_response",
                "step": step,
                "phase": phase,
                "preview": _truncate(raw, _EVENT_PREVIEW_CHARS),
                **(event_extra or {}),
            },
        )

        try:
            action = _parse_action(raw, allowed_actions)
        except _LLMProtocolError as parse_err:
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {"role": "user", "content": build_correction_prompt(str(parse_err))}
            )
            try:
                retry_raw = llm.chat(**kwargs)
            except Exception as e:
                raise _LLMProtocolError(f"LLM retry call failed: {e}")
            run_store.append_event(
                self.project_id,
                run_id,
                {
                    "type": "llm_response",
                    "step": step,
                    "phase": phase,
                    "retry": True,
                    "preview": _truncate(retry_raw, _EVENT_PREVIEW_CHARS),
                    **(event_extra or {}),
                },
            )
            action = _parse_action(retry_raw, allowed_actions)
            messages.append({"role": "assistant", "content": retry_raw})
            return action

        messages.append({"role": "assistant", "content": raw})
        return action

    def _observe_tool_result(
        self, action: dict, result: ToolResult, *, ctx: _UnitContext | None = None
    ) -> None:
        """Record successful side-effecting tool calls for the diagnostic fallback.

        We only count operations that actually changed state: write_file,
        append_file, and run_shell (when sandbox-accepted). Reads are
        ignored. Lists stay ordered and deduplicated. A parallel unit's
        activity accumulates in its own ``ctx`` (merged by the coordinator
        when the unit settles) instead of the shared run-scoped lists.
        """
        if not result.success:
            return
        if ctx is not None:
            files, cmds = ctx.observed_files, ctx.observed_cmds
        else:
            files, cmds = self._observed_files_changed, self._observed_commands_run
        tool_name = (action.get("tool_name") or "").strip()
        arguments = action.get("arguments") or {}
        if tool_name in {"write_file", "append_file"}:
            # Count every successful write (incl. overwrites) for the
            # continuation progress signal.
            if ctx is not None:
                ctx.write_ops += 1
            else:
                self._observed_write_ops += 1
            path = arguments.get("path") if isinstance(arguments, dict) else None
            if isinstance(path, str) and path and path not in files:
                files.append(path)
        elif tool_name == "run_shell":
            command = arguments.get("command") if isinstance(arguments, dict) else None
            if isinstance(command, str) and command and command not in cmds:
                cmds.append(command)

    def _dispatch_tool(
        self,
        run_id: str,
        step: int,
        action: dict,
        *,
        phase: str = "execution",
        ctx: _UnitContext | None = None,
        event_extra: dict | None = None,
    ) -> tuple[ToolResult, dict]:
        tool_name = action.get("tool_name") or ""
        arguments = action.get("arguments") or {}
        runtime = ctx.runtime if ctx is not None else self.runtime
        if event_extra is None and ctx is not None:
            event_extra = ctx.event_extra()
        run_store.append_event(
            self.project_id,
            run_id,
            {
                "type": "tool_call",
                "step": step,
                "phase": phase,
                "tool_name": tool_name,
                "arguments": _bound_args(arguments),
                "reason": _truncate(str(action.get("reason", "")), 200),
                **(event_extra or {}),
            },
        )

        try:
            if tool_name == "list_files":
                args = ListFilesRequest(**arguments)
                return runtime.list_files(args.path), {"tool_name": tool_name}
            if tool_name == "read_file":
                args = ReadFileRequest(**arguments)
                return runtime.read_file(args.path), {"tool_name": tool_name}
            if tool_name == "write_file":
                args = WriteFileRequest(**arguments)
                return runtime.write_file(args.path, args.content), {"tool_name": tool_name}
            if tool_name == "append_file":
                args = AppendFileRequest(**arguments)
                return runtime.append_file(args.path, args.content), {"tool_name": tool_name}
            if tool_name == "search_files":
                args = SearchFilesRequest(**arguments)
                return runtime.search_files(args.query, args.path), {"tool_name": tool_name}
            if tool_name == "run_shell":
                args = RunShellRequest(**arguments)
                return runtime.run_shell(args.command, args.timeout_seconds), {"tool_name": tool_name}
        except Exception as e:
            return (
                ToolResult(
                    success=False,
                    tool_name=tool_name or "unknown",
                    error=f"tool argument error: {type(e).__name__}: {e}",
                ),
                {"tool_name": tool_name},
            )

        return (
            ToolResult(
                success=False,
                tool_name=tool_name or "unknown",
                error=f"unknown tool: {tool_name!r}",
            ),
            {"tool_name": tool_name or "unknown"},
        )

    def _finalize_cancelled(
        self, run_id: str, record: RunRecord, task: TaskSpec
    ) -> ResultSummary:
        """Finalize a run a user cancelled mid-flight (run control).

        Sets a clear terminal ``cancelled`` status + artifacts and stops — no
        command/browser verification and no memory reconciliation, because an
        aborted run has no settled outcome to verify or record. Re-reads the
        on-disk status first and only writes when it is still ``running``, so it
        can never clobber a record another writer (the cancel endpoint's orphan
        path, or a racing finalize) already settled.
        """
        on_disk = run_store.read_run_json(self.project_id, run_id)
        result_path = str(run_store.get_run_dir(self.project_id, run_id) / "result.md")
        if on_disk is not None and on_disk.get("status") != RunStatus.RUNNING.value:
            # Already finalized by someone else — respect it, don't overwrite.
            return ResultSummary(
                run_id=run_id,
                status=str(on_disk.get("status") or RunStatus.CANCELLED.value),
                summary=str(on_disk.get("summary") or ""),
                files_changed=_coerce_str_list(on_disk.get("files_changed")),
                commands_run=_coerce_str_list(on_disk.get("commands_run")),
                blockers=_coerce_str_list(on_disk.get("blockers")),
                result_path=result_path,
            )

        summary = "Run cancelled by user."
        record.status = RunStatus.CANCELLED
        record.completed_at = datetime.utcnow()
        record.summary = summary
        # Surface whatever the agent managed to do before the cancel landed.
        if not record.files_changed and self._observed_files_changed:
            record.files_changed = list(self._observed_files_changed)
        if not record.commands_run and self._observed_commands_run:
            record.commands_run = list(self._observed_commands_run)
        blocker = "run cancelled by user"
        if blocker not in record.blockers:
            record.blockers = list(record.blockers) + [blocker]
        # Settle transient sub-status + the cancel-request flag.
        record.cancel_requested = False
        record.verification_state = None
        record.integration_state = None
        # Mark any in-flight task skipped so the task graph reads cleanly.
        if record.plan is not None:
            for unit in record.plan.tasks:
                if unit.status == TaskStatus.RUNNING:
                    unit.status = TaskStatus.SKIPPED
                    if "run cancelled" not in unit.blockers:
                        unit.blockers.append("run cancelled")
            run_store.write_plan_json(self.project_id, run_id, record.plan)

        run_store.write_run_json(self.project_id, run_id, record)
        result_md = run_store.render_result_md(
            record, summary, notes="Run cancelled by user before it finalized."
        )
        run_store.write_result_md(self.project_id, run_id, result_md)
        run_store.append_event(
            self.project_id,
            run_id,
            {"type": "run_cancelled", "reason": "user requested cancellation"},
        )
        return ResultSummary(
            run_id=run_id,
            status=record.status.value,
            summary=summary,
            files_changed=record.files_changed,
            commands_run=record.commands_run,
            blockers=record.blockers,
            result_path=result_path,
            plan=record.plan,
        )

    def _finalize(
        self,
        run_id: str,
        record: RunRecord,
        task: TaskSpec,
        final_action: dict | None,
        loop_failed_reason: str | None,
    ) -> ResultSummary:
        if final_action is not None:
            status_str = final_action.get("status", "")
            if status_str not in _ALLOWED_STATUS:
                status_str = "failed"
            summary = str(final_action.get("summary", "")).strip()
            files_changed = _coerce_str_list(final_action.get("files_changed"))
            commands_run = _coerce_str_list(final_action.get("commands_run"))
            blockers = _coerce_str_list(final_action.get("blockers"))
            task_md_update = str(final_action.get("task_md_update", "")).strip()
        else:
            status_str = "failed"
            summary = loop_failed_reason or "run did not finalize"
            files_changed = []
            commands_run = []
            blockers = [loop_failed_reason] if loop_failed_reason else ["did not finalize"]
            task_md_update = ""

        # Diagnostic fallback: if the agent didn't supply file/command
        # lists (typical when the loop exhausted its budget before
        # emitting `final`), surface what actually happened so the run
        # is still inspectable. The final action's explicit lists always
        # win when present.
        if not files_changed and self._observed_files_changed:
            files_changed = list(self._observed_files_changed)
        if not commands_run and self._observed_commands_run:
            commands_run = list(self._observed_commands_run)

        record.status = RunStatus(status_str)
        record.completed_at = datetime.utcnow()
        record.files_changed = files_changed
        record.commands_run = commands_run
        record.blockers = blockers
        # Task 06.2D — persist the summary on the record so the chat-first run
        # follow-up card can render a natural completion message from run.json.
        record.summary = summary

        # Snapshot the pre-update TASK.md so the verify command is read
        # from the project's persistent config, even when the agent's
        # final action rewrites TASK.md and accidentally drops the
        # ## Verification section.
        pre_update_task_md = read_task_state(self.project_id) or ""

        # TASK.md update — only execution-layer file we touch.
        if task_md_update:
            update_task_state(self.project_id, task_md_update + ("\n" if not task_md_update.endswith("\n") else ""))
        else:
            existing = read_task_state(self.project_id) or ""
            stamp = record.completed_at.strftime("%Y-%m-%dT%H:%M:%SZ") if record.completed_at else ""
            auto = (
                f"\n\n---\n\n"
                f"### Run {record.run_id} — {record.status.value} ({stamp})\n"
                f"**Task:** {task.title}\n\n"
                f"{summary or '(no summary)'}\n"
            )
            update_task_state(self.project_id, existing + auto)

        # Task 06.2A + 06.2E — automatic command verification with one bounded
        # repair pass. Runs after the agent's normal final action; never
        # raises. Verification commands are taken from a manual ``##
        # Verification`` block when present, otherwise inferred from the repo.
        # A failing verification on an otherwise-`completed` run triggers a
        # single repair pass; if it still fails the run is downgraded to
        # `partial`. Non-completed statuses are preserved either way.
        observed_before_verify = set(self._observed_files_changed)
        verification = self._verify_with_repair(
            run_id, record, task, pre_update_task_md
        )
        record.verification = verification
        # The repair pass may have written new files; reflect those (and only
        # those) so the run record + result.md stay accurate without clobbering
        # the agent's explicit final-action file list.
        for path in self._observed_files_changed:
            if path not in observed_before_verify and path not in record.files_changed:
                record.files_changed.append(path)
        if verification.enabled and verification.status == "failed":
            # A `completed` run that fails verification is downgraded to
            # `partial`; a run that was already `partial` stays `partial`. Either
            # way, record the failing command as a blocker so the run report is
            # honest about why the build/check didn't pass.
            if record.status == RunStatus.COMPLETED:
                record.status = RunStatus.PARTIAL
            if record.status == RunStatus.PARTIAL:
                blocker_msg = f"verification failed: {verification.command or '(unknown command)'}"
                if blocker_msg not in record.blockers:
                    record.blockers.append(blocker_msg)
        # Settle the transient verification sub-status now that the phase is done.
        record.verification_state = None
        run_store.append_event(
            self.project_id,
            run_id,
            {
                "type": "verification",
                "enabled": verification.enabled,
                "status": verification.status,
                "mode": verification.mode,
                "command": verification.command,
                "exit_code": verification.exit_code,
                "repair_attempts": verification.repair_attempts,
                "duration_ms": verification.duration_ms,
            },
        )
        # Persist the settled verification result + cleared sub-status before
        # the (potentially slow) automatic browser verification runs, so a poll
        # during that window doesn't see a stale "verifying" sub-status.
        run_store.write_run_json(self.project_id, run_id, record)

        # Task 06.2B — optional browser verification. Same posture as
        # command verification: runs after the agent's final action,
        # never raises, and a failing browser verification downgrades a
        # ``completed`` run to ``partial``. Skipped (no config) is a
        # no-op for status. Failures are tolerated for already-failed/
        # blocked/partial runs.
        try:
            browser_verification = run_browser_verification(
                self.project_id,
                run_dir=run_store.get_run_dir(self.project_id, run_id),
                task_md_override=pre_update_task_md,
            )
        except Exception as exc:  # noqa: BLE001
            # run_browser_verification already swallows its own exceptions,
            # but keep an outer guard so a programmer error here never
            # leaks into background finalization.
            from .models import BrowserVerificationResult as _BVR
            browser_verification = _BVR(
                enabled=True,
                command=None,
                url=None,
                status="failed",
                screenshot_path=None,
                output_preview=(
                    f"browser verification crashed: {type(exc).__name__}: {exc}"
                ),
                duration_ms=None,
            )
        record.browser_verification = browser_verification
        if (
            browser_verification.enabled
            and browser_verification.status == "failed"
            and record.status == RunStatus.COMPLETED
        ):
            record.status = RunStatus.PARTIAL
            cmd_label = browser_verification.command or "(unknown command)"
            blocker_msg = f"browser verification failed: {cmd_label}"
            if blocker_msg not in record.blockers:
                record.blockers.append(blocker_msg)
        run_store.append_event(
            self.project_id,
            run_id,
            {
                "type": "browser_verification",
                "enabled": browser_verification.enabled,
                "status": browser_verification.status,
                "command": browser_verification.command,
                "url": browser_verification.url,
                "screenshot_path": browser_verification.screenshot_path,
                "duration_ms": browser_verification.duration_ms,
                "pages": len(browser_verification.pages),
                "readiness": browser_verification.readiness,
            },
        )

        # AI visual judgment over the captured screenshots. Diagnostic-only:
        # best-effort, never raises, and never changes ``record.status``. Runs
        # only on a passing browser verification with screenshots; skips
        # gracefully (with a reason) when no vision provider key is configured.
        if (
            browser_verification.enabled
            and browser_verification.status == "passed"
            and browser_verification.pages
        ):
            try:
                record.visual_review = run_visual_review(
                    self.project_id,
                    run_id,
                    task_card=task.task_card,
                    summary=summary,
                    browser_result=browser_verification,
                    run_dir=run_store.get_run_dir(self.project_id, run_id),
                )
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {
                        "type": "visual_review",
                        "status": record.visual_review.status,
                        "headline": record.visual_review.headline,
                        "provider": record.visual_review.provider,
                        "url": browser_verification.url,
                    },
                )
            except Exception:  # noqa: BLE001 — visual review never fails the run
                log.exception("Visual review wiring failed for run %s", run_id)

        # Phase 7 — capture the post-run diff against the pre-run checkpoint.
        # Best-effort + read-only: it NEVER auto-commits or pushes (those are
        # explicit, user-confirmed actions) and never fails finalization. The
        # diff text is redacted + bounded and stored as a per-run artifact; only
        # compact metadata (diff_stat / head_commit) lands on the record.
        self._capture_post_run_diff(run_id, record)

        run_store.write_run_json(self.project_id, run_id, record)
        # Re-write plan.json with the settled per-task statuses (Phase 5). For a
        # single-task plan, sync the lone task's status to the run's terminal
        # status so run.json's embedded plan + plan.json never contradict the
        # top-level status — the per-task `task_status_from_final` mapping
        # collapses partial/blocked to FAILED, and a verification downgrade can
        # move the run to PARTIAL after the task was marked COMPLETED.
        if record.plan is not None:
            if len(record.plan.tasks) == 1:
                record.plan.tasks[0].status = (
                    TaskStatus.COMPLETED
                    if record.status == RunStatus.COMPLETED
                    else TaskStatus.FAILED
                )
            run_store.write_plan_json(self.project_id, run_id, record.plan)

        notes = ""
        if loop_failed_reason and final_action is None:
            notes = f"Run did not produce a final action: {loop_failed_reason}"
        result_md = run_store.render_result_md(record, summary, notes=notes)
        run_store.write_result_md(self.project_id, run_id, result_md)
        result_path = str(run_store.get_run_dir(self.project_id, run_id) / "result.md")

        # Task 06.0 — bounded post-run memory reconciliation. Best-effort:
        # any failure here is captured into run.json's reconciliation fields
        # and swallowed so the run itself still finalizes cleanly.
        try:
            reconcile_run_memory(
                self.project_id,
                run_id,
                summary_override=summary,
            )
        except Exception:  # noqa: BLE001
            # `reconcile_run_memory` already swallows its own exceptions; this
            # extra guard exists only to absolutely guarantee the runner never
            # leaks a reconciliation failure to its callers.
            pass

        # Phase 6 — Main-Agent recovery assessment for a non-green run. Runs
        # after reconciliation so memory reflects the outcome first. Best-effort
        # and diagnostic-only: it persists a `recovery_assessment` onto run.json
        # for the UI / Main Agent but NEVER dispatches a follow-up run and NEVER
        # fails finalization. Green runs are skipped inside `assess_run`.
        try:
            assess_run(self.project_id, run_id)
        except Exception:  # noqa: BLE001
            pass

        return ResultSummary(
            run_id=run_id,
            status=record.status.value,
            summary=summary,
            files_changed=record.files_changed,
            commands_run=commands_run,
            blockers=record.blockers,
            result_path=result_path,
            verification=verification,
            browser_verification=browser_verification,
            visual_review=record.visual_review,
            plan=record.plan,
            commit_sha=record.commit_sha,
            branch=record.branch,
            pr_url=record.pr_url,
            diff_stat=record.diff_stat,
        )

    # ---------- post-run diff capture (Phase 7) ----------

    def _capture_post_run_diff(self, run_id: str, record: RunRecord) -> None:
        """Capture the run's diff against its pre-run checkpoint, redacted and
        bounded, into the ``diff.patch`` artifact; stamp ``head_commit`` +
        ``diff_stat`` onto the record. No-op when no checkpoint was created (e.g.
        git unavailable) or already captured. Best-effort — never raises."""
        if not record.pre_run_checkpoint or record.diff_stat is not None:
            return
        try:
            from . import git_ops  # lazy import

            # Strongest egress guard: strip exact stored token values (project +
            # global + env) AND common token shapes from the diff at capture time.
            try:
                import credentials as _credentials

                redactor = lambda t: _credentials.redact(t, self.project_id)  # noqa: E731
            except Exception:  # noqa: BLE001 — fall back to git_ops' pattern scrub
                redactor = None

            record.head_commit = git_ops._head_sha(self.runtime)
            diff = git_ops.capture_diff(
                self.project_id,
                record.pre_run_checkpoint,
                runtime=self.runtime,
                redactor=redactor,
            )
            if not diff.captured:
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {"type": "diff_skipped", "error": (diff.error or "")[:200]},
                )
                return
            run_store.write_diff_patch(self.project_id, run_id, diff.diff_text)
            if diff.stat:
                record.diff_stat = diff.stat.splitlines()[-1].strip()[:300]
            elif diff.files:
                record.diff_stat = f"{len(diff.files)} file(s) changed"
            else:
                record.diff_stat = "no changes"
            run_store.append_event(
                self.project_id,
                run_id,
                {
                    "type": "diff_captured",
                    "files": len(diff.files),
                    "truncated": diff.truncated,
                    "stat": record.diff_stat,
                },
            )
        except Exception:  # noqa: BLE001
            log.exception("Post-run diff capture failed for run %s", run_id)

    # ---------- verification + repair (Task 06.2E) ----------

    def _verify_with_repair(
        self,
        run_id: str,
        record: RunRecord,
        task: TaskSpec,
        pre_update_task_md: str,
    ) -> VerificationResult:
        """Run command verification, with bounded iterative repair on failure.

        Up to ``MAX_REPAIR_ATTEMPTS`` repair→re-verify cycles run while the build
        still fails and the run produced output worth fixing. Returns the final
        :class:`VerificationResult`. Never raises — any unexpected error is folded
        into a ``failed`` aggregate so background finalization is never
        interrupted.
        """
        try:
            mode, specs = plan_verification(
                self.project_id, pre_update_task_md, runtime=self.runtime
            )
        except Exception:  # noqa: BLE001
            return VerificationResult(enabled=False, status="skipped", mode="skipped")

        if not specs:
            return VerificationResult(enabled=False, status="skipped", mode="skipped")

        # Signal the "verifying" phase so a concurrent poll sees the run is
        # still busy (the agent loop has finished, but the run isn't settled).
        record.verification_state = "verifying"
        run_store.write_run_json(self.project_id, run_id, record)
        run_store.append_event(
            self.project_id,
            run_id,
            {"type": "verification_started", "mode": mode, "commands": len(specs)},
        )

        verification = run_verification_specs(
            self.project_id, specs, mode=mode, runtime=self.runtime
        )

        # Attempt a bounded repair when verification failed AND the run produced
        # real output worth fixing. Originally this fired only for `completed`
        # runs; extended to `partial` runs too (hardening): a multi-task run can
        # land `partial` because one task didn't finish, yet still carry a fixable
        # build error (e.g. a TypeScript export conflict — TS2484) that blocks the
        # whole app from building. Repairing it makes the build green so browser
        # verification / preview can proceed. `blocked` / `failed` runs and runs
        # with no files changed (nothing usable to repair) are left alone.
        repairable = record.status in (RunStatus.COMPLETED, RunStatus.PARTIAL) and bool(
            record.files_changed
        )
        if not (verification.status == "failed" and repairable):
            return verification

        # Iterative repair (hardening): a single repair pass is rarely enough for
        # a real multi-file build, where type/import errors surface in waves — the
        # first pass fixes most, a re-verify reveals a few stragglers (or a fix
        # exposes a downstream error). Loop up to MAX_REPAIR_ATTEMPTS times:
        # repair against the LATEST failing output, re-verify, repeat until it
        # passes, a pass changes nothing / the LLM is unavailable, or the budget
        # is spent. Each pass is itself bounded (MAX_REPAIR_STEPS), so total work
        # stays tightly capped. ``repair_attempts`` records the count for the UI.
        attempts = 0
        while (
            verification.status == "failed"
            and repairable
            and attempts < MAX_REPAIR_ATTEMPTS
        ):
            record.verification_state = "repairing"
            run_store.write_run_json(self.project_id, run_id, record)
            run_store.append_event(
                self.project_id,
                run_id,
                {
                    "type": "verification_repair_started",
                    "attempt": attempts + 1,
                    "command": verification.command,
                },
            )

            repaired = self._run_repair_pass(run_id, verification)
            if not repaired:
                # Repair could not run (LLM unavailable) or changed nothing —
                # further passes would be identical, so stop. Record at least one
                # attempt so the UI/report shows repair was tried.
                verification.repair_attempts = max(attempts, 1)
                break

            attempts += 1
            verification = run_verification_specs(
                self.project_id, specs, mode=mode, runtime=self.runtime, repair_attempts=attempts
            )
            run_store.append_event(
                self.project_id,
                run_id,
                {
                    "type": "verification_reverified",
                    "attempt": attempts,
                    "status": verification.status,
                    "command": verification.command,
                },
            )

        return verification

    def _collect_repair_file_context(self, verification: VerificationResult) -> str:
        """Pre-read the files named in the failing output for the repair prompt.

        Inlining the current contents lets the repair agent rewrite them
        immediately rather than burning its bounded step budget on reads/searches
        (an unguarded repair agent spent all its steps investigating and wrote
        zero fixes). Only fully-readable (non-truncated) files are inlined — a
        truncated file is skipped so the agent never rewrites a partial file and
        drops content. Bounded in file count + total size.
        """
        paths = _extract_error_file_paths(verification)
        blocks: list[str] = []
        total = 0
        for path in paths[:_MAX_REPAIR_CONTEXT_FILES]:
            try:
                res = self.runtime.read_file(path)
            except Exception:  # noqa: BLE001
                continue
            if not res.success or not res.output:
                continue
            meta = res.metadata or {}
            if meta.get("truncated"):
                continue
            content = res.output
            if total + len(content) > _MAX_REPAIR_CONTEXT_CHARS:
                break
            total += len(content)
            blocks.append(f"### {path}\n```\n{content}\n```")
        return "\n\n".join(blocks)

    def _run_repair_pass(self, run_id: str, verification: VerificationResult) -> bool:
        """Give the Coding Agent one bounded pass to fix a failed verification.

        Returns True when the repair pass applied at least one successful file
        mutation (so a re-verify is worthwhile) — including overwriting an
        existing file, which is the common repair shape. Returns False when the
        LLM is unavailable or the agent changed nothing. Never raises.
        """
        try:
            failures = _format_verification_failures(verification)
            files_context = self._collect_repair_file_context(verification)
            system_prompt = build_system_prompt(
                self._agent_md, self.project_id, MAX_REPAIR_STEPS
            )
            messages: list[dict[str, str]] = [
                {
                    "role": "user",
                    "content": build_repair_user_prompt(
                        failures, MAX_REPAIR_STEPS, files_context
                    ),
                }
            ]
            mutations = 0

            for step in range(1, MAX_REPAIR_STEPS + 1):
                try:
                    action = self._llm_step(run_id, step, system_prompt, messages, phase="repair")
                except _LLMProtocolError as e:
                    run_store.append_event(
                        self.project_id,
                        run_id,
                        {"type": "verification_repair_failed", "step": step, "error": str(e)},
                    )
                    break

                if action.get("action") == "final":
                    break

                if action.get("action") == "tool_call":
                    tool_name = (action.get("tool_name") or "").strip()
                    if tool_name == "run_shell":
                        # Hard-block run_shell during repair (enforced, not just
                        # prompted): verification reruns automatically after this
                        # pass, so a repair must spend its bounded budget EDITING
                        # files. In practice an unguarded repair agent burns all
                        # its steps re-running `tsc` / `npm run build` to "check"
                        # and writes zero fixes — the pass then looks like it made
                        # no progress and the iterative loop gives up early.
                        msg = (
                            "run_shell is disabled during the repair pass — Agent OS "
                            "reruns verification automatically after you finalize. Do "
                            "not run build/test commands. Read the file(s) named in the "
                            "errors and write the corrected versions, then emit final."
                        )
                        run_store.append_event(
                            self.project_id,
                            run_id,
                            {
                                "type": "tool_result",
                                "step": step,
                                "phase": "repair",
                                "tool_name": "run_shell",
                                "success": False,
                                "error": msg,
                            },
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": build_tool_result_prompt("run_shell", False, msg),
                            }
                        )
                        continue
                    tool_result, dispatched = self._dispatch_tool(run_id, step, action, phase="repair")
                    self._observe_tool_result(action, tool_result)
                    if tool_result.success and dispatched["tool_name"] in {
                        "write_file",
                        "append_file",
                    }:
                        mutations += 1
                    tool_payload = _format_tool_result_for_llm(tool_result)
                    messages.append(
                        {
                            "role": "user",
                            "content": build_tool_result_prompt(
                                dispatched["tool_name"], tool_result.success, tool_payload
                            ),
                        }
                    )
                    continue

                # Unknown action shape — stop the repair pass.
                break

            return mutations > 0
        except Exception:  # noqa: BLE001
            log.exception("Repair pass crashed for run %s", run_id)
            return False


# ---------- helpers ----------


# Matches a repo-relative file path with an extension followed by a line marker
# in compiler/test output: ``src/pages/Dashboard.tsx(181,11)`` (TS) or
# ``backend/app.py:12`` (Python/most tools). Captures the path.
_ERROR_FILE_PATH_REGEX = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_./\-]*\.[A-Za-z0-9]+)[(:]\d+")
# Bound how much pre-read file context we inject into a repair prompt. 10 files
# (was 6): a multi-file build error often names a dozen+ files, and pre-reading
# more of them lets a single repair pass rewrite more in one go — fewer passes to
# converge.
_MAX_REPAIR_CONTEXT_FILES = 10
_MAX_REPAIR_CONTEXT_CHARS = 16_000


def _extract_error_file_paths(verification: VerificationResult) -> list[str]:
    """Pull repo-relative file paths out of the failing verification output.

    Ordered + de-duplicated. Used to pre-read the offending files into the
    repair prompt so the agent can rewrite them without hunting.
    """
    text = _format_verification_failures(verification)
    paths: list[str] = []
    for m in _ERROR_FILE_PATH_REGEX.finditer(text):
        p = m.group(1).replace("\\", "/").lstrip("./")
        if p and p not in paths:
            paths.append(p)
    return paths


def _format_verification_failures(verification: VerificationResult) -> str:
    """Render the failing verification command(s) + output for the repair prompt."""
    failed = [c for c in verification.commands if c.status == "failed"]
    if not failed:
        # Fall back to the aggregate single command (older shape).
        cmd = verification.command or "(unknown command)"
        return f"$ {cmd}\n{verification.output_preview or '(no output captured)'}"
    blocks: list[str] = []
    for cmd in failed:
        head = f"$ {cmd.command}"
        if cmd.exit_code is not None:
            head += f"   (exit {cmd.exit_code})"
        blocks.append(head + "\n" + (cmd.output_preview or "(no output captured)"))
    return "\n\n".join(blocks)


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [truncated, +{len(text) - limit} chars]"


def _format_tool_result_for_llm(result: ToolResult) -> str:
    """Compact, bounded representation of a ToolResult for the LLM transcript."""
    body = {
        "success": result.success,
        "tool_name": result.tool_name,
        "output": _truncate(result.output, _MAX_TOOL_OUTPUT_FOR_LLM),
        "error": _truncate(result.error, _MAX_TOOL_OUTPUT_FOR_LLM),
        "metadata": result.metadata,
    }
    return json.dumps(body, ensure_ascii=False, indent=2)


def _bound_args(arguments: Any) -> Any:
    """Bound argument values for safe inclusion in events.jsonl.

    `content` fields can be huge; replace with a length marker.
    """
    if not isinstance(arguments, dict):
        return arguments
    out = {}
    for k, v in arguments.items():
        if k == "content" and isinstance(v, str) and len(v) > 200:
            out[k] = f"<{len(v)} chars>"
        elif isinstance(v, str) and len(v) > 500:
            out[k] = _truncate(v, 500)
        else:
            out[k] = v
    return out


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if isinstance(x, (str, int, float))]


def _dedup(items: list[str]) -> list[str]:
    """Order-preserving de-duplication."""
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _render_plan_outline(tasks: list["ExecutionTask"], current_id: str) -> str:
    """Compact plan overview for a task unit's prompt (marks the current task)."""
    lines: list[str] = []
    for t in tasks:
        marker = "->" if t.id == current_id else "- "
        dep = f" (after {', '.join(t.depends_on)})" if t.depends_on else ""
        lines.append(f"{marker} [{t.status.value}] {t.id}: {t.title}{dep}")
    return "\n".join(lines)


def _render_prior_context(tasks: list["ExecutionTask"]) -> str:
    """Summarize what earlier tasks already wrote, for the next task's prompt.

    Phase 9: completed read-only tasks (reviewer / inspector) contribute their
    findings too — that summary IS their deliverable, and later tasks should
    build on it. Sequential all-coder plans render byte-identical to before.
    """
    files = _dedup(
        [
            f
            for t in tasks
            if t.status == TaskStatus.COMPLETED
            for f in t.files_changed
        ]
    )
    findings = [
        t
        for t in tasks
        if t.status == TaskStatus.COMPLETED
        and t.summary
        and get_role(t.role).read_only
    ]
    if not files and not findings:
        return "(nothing written yet)"
    parts: list[str] = []
    if files:
        shown = files[:40]
        body = "\n".join(f"- {f}" for f in shown)
        if len(files) > len(shown):
            body += f"\n- … (+{len(files) - len(shown)} more)"
        parts.append("Files written by earlier tasks:\n" + body)
    if findings:
        body = "\n".join(
            f"- {t.id} ({t.role}): {_truncate(t.summary, 400)}" for t in findings
        )
        parts.append("Findings from earlier read-only tasks:\n" + body)
    return "\n\n".join(parts)


def _parse_action(
    raw: str, allowed: frozenset[str] = frozenset({"tool_call", "final"})
) -> dict:
    """Tolerant JSON parse: accept fenced or wrapped output, but require an action key.

    ``allowed`` restricts which ``action`` values are accepted. Defaults to the
    execution/repair set (``tool_call`` / ``final``); the planning loop passes
    ``{"tool_call", "plan"}`` so the ``plan`` action is only ever valid there.
    """
    if not raw or not raw.strip():
        raise _LLMProtocolError("empty response")

    text = raw.strip()
    # Strip a leading ```json or ``` fence if present.
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()

    parsed: Any
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Fall back to extracting the first balanced { ... } block.
        snippet = _extract_first_json_object(text)
        if snippet is None:
            raise _LLMProtocolError("response is not valid JSON")
        try:
            parsed = json.loads(snippet)
        except json.JSONDecodeError as e:
            raise _LLMProtocolError(f"response is not valid JSON: {e}")

    if not isinstance(parsed, dict):
        raise _LLMProtocolError("response JSON is not an object")
    if parsed.get("action") not in allowed:
        raise _LLMProtocolError(
            f"response missing or unknown 'action' (got {parsed.get('action')!r})"
        )
    return parsed


def _extract_first_json_object(text: str) -> str | None:
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : i + 1]
    return None
