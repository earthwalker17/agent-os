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
from datetime import datetime
from pathlib import Path
from typing import Any

import llm

from .manager import get_execution_workspace, read_task_state, update_task_state
from .models import (
    ExecutionPlan,
    ExecutionTask,
    RunRecord,
    RunStatus,
    ResultSummary,
    TaskSpec,
    TaskStatus,
    VerificationResult,
)
from .memory_reconciliation import reconcile_run_memory
from .planner import (
    MAX_TASKS,
    PlanParseError,
    aggregate_run_status,
    dependency_failed,
    fallback_plan,
    looks_complex,
    plan_from_dict,
    task_status_from_final,
    topological_order,
)
from .prompts import (
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


log = logging.getLogger(__name__)


# Bumped from 16 -> 24 (Task 06.2E): a minimal full-stack scaffold (backend
# files + a Vite frontend: package.json, vite config, index.html, a couple of
# src files) can need a dozen-plus write_file steps after any exploration, and
# still leave room for a final action. Verification + repair + browser
# verification all run AFTER the loop, so the agent shouldn't spend steps
# running dev servers or test suites — see prompts.py.
MAX_STEPS = 24

# Bounded budget for the single post-verification repair pass (Task 06.2E).
# Repairs are targeted fixes informed by the failing command output, so they
# need far fewer steps than the initial build.
MAX_REPAIR_STEPS = 10

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
MAX_TASK_STEPS = 12

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

_ALLOWED_STATUS = {"completed", "partial", "blocked", "failed"}


class _LLMProtocolError(Exception):
    """Raised when the LLM returns something we cannot parse into an action."""


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
        # AGENT.md text, captured during run_task for the repair pass.
        self._agent_md: str = ""

    def run_task(self, task: TaskSpec, run_id: str | None = None) -> ResultSummary:
        """Execute the Coding Agent loop and finalize artifacts.

        If `run_id` is provided, the caller (e.g. BackgroundRunManager) is
        expected to have already created the run directory, task_card.md, and
        an initial run.json with status=running. The runner reuses that record
        instead of allocating a new one. Otherwise the runner does the
        allocation itself (stand-alone synchronous path).
        """
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
        plan = self._run_plan_phase(run_id, record, task, task_md)
        final_action, loop_failed_reason = self._run_execution_phase(
            run_id, record, task, plan, task_md
        )

        return self._finalize(run_id, record, task, final_action, loop_failed_reason)

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
            final_action, loop_failed_reason = self._run_task_unit(
                run_id, system_prompt, initial, MAX_STEPS, phase="execution"
            )
            self._apply_unit_result(unit, final_action, loop_failed_reason, before_f, before_c)
            # Pass the raw final action through unchanged (incl. task_md_update).
            return final_action, loop_failed_reason

        # --- multi-task path
        by_id = {t.id: t for t in tasks}
        for idx, unit in enumerate(topological_order(tasks), start=1):
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
            )
            before_f = len(self._observed_files_changed)
            before_c = len(self._observed_commands_run)
            final_action, loop_failed_reason = self._run_task_unit(
                run_id, system_prompt, initial, MAX_TASK_STEPS, phase="execution"
            )
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

    def _run_task_unit(
        self,
        run_id: str,
        system_prompt: str,
        initial_user_prompt: str,
        max_steps: int,
        *,
        phase: str = "execution",
    ) -> tuple[dict | None, str | None]:
        """One bounded JSON tool loop. Returns (final_action, fail_reason).

        This is the original single-loop body, extracted so the simple path and
        every multi-task unit run identical machinery. Uses a fresh ``messages``
        list (per-task isolation). Observed file/command activity accumulates
        into the run-scoped ``self._observed_*`` lists (the diagnostic fallback
        + repair-delta in ``_finalize`` depend on run-scope).
        """
        messages: list[dict[str, str]] = [{"role": "user", "content": initial_user_prompt}]
        final_action: dict | None = None
        loop_failed_reason: str | None = None

        for step in range(1, max_steps + 1):
            try:
                action = self._llm_step(run_id, step, system_prompt, messages, phase=phase)
            except _LLMProtocolError as e:
                loop_failed_reason = f"LLM protocol error after retry: {e}"
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {"type": "run_failed", "step": step, "phase": phase, "error": loop_failed_reason},
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
                    },
                )
                break

            if action.get("action") == "tool_call":
                tool_result, dispatched = self._dispatch_tool(run_id, step, action, phase=phase)
                self._observe_tool_result(action, tool_result)
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
                {"type": "run_failed", "step": step, "phase": phase, "error": loop_failed_reason},
            )
            break
        else:
            # Loop exhausted without final
            loop_failed_reason = f"step budget exhausted after {max_steps} steps"
            run_store.append_event(
                self.project_id,
                run_id,
                {"type": "run_failed", "step": max_steps, "phase": phase, "error": loop_failed_reason},
            )

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
        unit_files = self._observed_files_changed[before_f:]
        unit_cmds = self._observed_commands_run[before_c:]
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
    ) -> dict:
        """One LLM call with a single correction retry. Mutates `messages` in place.

        ``allowed_actions`` constrains which ``action`` values the parser
        accepts — the execution/repair loops allow ``tool_call`` / ``final``;
        the planning loop allows ``tool_call`` / ``plan``. This keeps the
        ``plan`` action from ever being accepted by the execution loop.
        """
        kwargs: dict[str, Any] = {"system": system_prompt, "messages": messages}
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
                },
            )
            action = _parse_action(retry_raw, allowed_actions)
            messages.append({"role": "assistant", "content": retry_raw})
            return action

        messages.append({"role": "assistant", "content": raw})
        return action

    def _observe_tool_result(self, action: dict, result: ToolResult) -> None:
        """Record successful side-effecting tool calls for the diagnostic fallback.

        We only count operations that actually changed state: write_file,
        append_file, and run_shell (when sandbox-accepted). Reads are
        ignored. Lists stay ordered and deduplicated.
        """
        if not result.success:
            return
        tool_name = (action.get("tool_name") or "").strip()
        arguments = action.get("arguments") or {}
        if tool_name in {"write_file", "append_file"}:
            path = arguments.get("path") if isinstance(arguments, dict) else None
            if isinstance(path, str) and path and path not in self._observed_files_changed:
                self._observed_files_changed.append(path)
        elif tool_name == "run_shell":
            command = arguments.get("command") if isinstance(arguments, dict) else None
            if isinstance(command, str) and command and command not in self._observed_commands_run:
                self._observed_commands_run.append(command)

    def _dispatch_tool(
        self, run_id: str, step: int, action: dict, *, phase: str = "execution"
    ) -> tuple[ToolResult, dict]:
        tool_name = action.get("tool_name") or ""
        arguments = action.get("arguments") or {}
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
            },
        )

        try:
            if tool_name == "list_files":
                args = ListFilesRequest(**arguments)
                return self.runtime.list_files(args.path), {"tool_name": tool_name}
            if tool_name == "read_file":
                args = ReadFileRequest(**arguments)
                return self.runtime.read_file(args.path), {"tool_name": tool_name}
            if tool_name == "write_file":
                args = WriteFileRequest(**arguments)
                return self.runtime.write_file(args.path, args.content), {"tool_name": tool_name}
            if tool_name == "append_file":
                args = AppendFileRequest(**arguments)
                return self.runtime.append_file(args.path, args.content), {"tool_name": tool_name}
            if tool_name == "search_files":
                args = SearchFilesRequest(**arguments)
                return self.runtime.search_files(args.query, args.path), {"tool_name": tool_name}
            if tool_name == "run_shell":
                args = RunShellRequest(**arguments)
                return self.runtime.run_shell(args.command, args.timeout_seconds), {"tool_name": tool_name}
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
        if (
            verification.enabled
            and verification.status == "failed"
            and record.status == RunStatus.COMPLETED
        ):
            record.status = RunStatus.PARTIAL
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
            },
        )

        run_store.write_run_json(self.project_id, run_id, record)
        # Re-write plan.json with the settled per-task statuses (Phase 5).
        if record.plan is not None:
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
            plan=record.plan,
        )

    # ---------- verification + repair (Task 06.2E) ----------

    def _verify_with_repair(
        self,
        run_id: str,
        record: RunRecord,
        task: TaskSpec,
        pre_update_task_md: str,
    ) -> VerificationResult:
        """Run command verification, with one bounded repair pass on failure.

        Returns the final :class:`VerificationResult`. Never raises — any
        unexpected error is folded into a ``failed`` aggregate so background
        finalization is never interrupted.
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

        # Only attempt a repair when the run would otherwise be `completed` and
        # verification actually failed. Other terminal statuses are left alone.
        if not (verification.status == "failed" and record.status == RunStatus.COMPLETED):
            return verification

        record.verification_state = "repairing"
        run_store.write_run_json(self.project_id, run_id, record)
        run_store.append_event(
            self.project_id,
            run_id,
            {"type": "verification_repair_started", "command": verification.command},
        )

        repaired = self._run_repair_pass(run_id, verification)
        if not repaired:
            # Repair could not run (e.g. LLM unavailable) or changed nothing —
            # keep the original failed verification, just record the attempt.
            verification.repair_attempts = 1
            return verification

        # Re-run the same specs after the repair pass.
        reverified = run_verification_specs(
            self.project_id, specs, mode=mode, runtime=self.runtime, repair_attempts=1
        )
        run_store.append_event(
            self.project_id,
            run_id,
            {
                "type": "verification_reverified",
                "status": reverified.status,
                "command": reverified.command,
            },
        )
        return reverified

    def _run_repair_pass(self, run_id: str, verification: VerificationResult) -> bool:
        """Give the Coding Agent one bounded pass to fix a failed verification.

        Returns True when the repair pass applied at least one successful file
        mutation (so a re-verify is worthwhile) — including overwriting an
        existing file, which is the common repair shape. Returns False when the
        LLM is unavailable or the agent changed nothing. Never raises.
        """
        try:
            failures = _format_verification_failures(verification)
            system_prompt = build_system_prompt(
                self._agent_md, self.project_id, MAX_REPAIR_STEPS
            )
            messages: list[dict[str, str]] = [
                {"role": "user", "content": build_repair_user_prompt(failures, MAX_REPAIR_STEPS)}
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
    """Summarize what earlier tasks already wrote, for the next task's prompt."""
    files = _dedup(
        [
            f
            for t in tasks
            if t.status == TaskStatus.COMPLETED
            for f in t.files_changed
        ]
    )
    if not files:
        return "(nothing written yet)"
    shown = files[:40]
    body = "\n".join(f"- {f}" for f in shown)
    if len(files) > len(shown):
        body += f"\n- … (+{len(files) - len(shown)} more)"
    return "Files written by earlier tasks:\n" + body


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
