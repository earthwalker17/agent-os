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
from datetime import datetime
from pathlib import Path
from typing import Any

import llm

from .manager import get_execution_workspace, read_task_state, update_task_state
from .models import RunRecord, RunStatus, ResultSummary, TaskSpec
from .memory_reconciliation import reconcile_run_memory
from .prompts import (
    build_correction_prompt,
    build_initial_user_prompt,
    build_system_prompt,
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
from .verification import run_verification
from .browser_verification import run_browser_verification


# Bumped from 8 -> 16: scaffolding tasks (write package.json + index.html +
# 2-3 src files + finalize) routinely need 5-8 write_file steps after any
# exploration, which left no room for a final action under the old budget.
# Verification + browser verification run AFTER the loop, so the agent
# shouldn't spend steps running dev servers or test suites — see prompts.py.
MAX_STEPS = 16

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
        task_md = read_task_state(self.project_id) or ""

        system_prompt = build_system_prompt(agent_md, self.project_id, MAX_STEPS)
        messages: list[dict[str, str]] = [
            {
                "role": "user",
                "content": build_initial_user_prompt(task.title, task.task_card, task_md),
            }
        ]

        final_action: dict | None = None
        loop_failed_reason: str | None = None

        for step in range(1, MAX_STEPS + 1):
            try:
                action = self._llm_step(run_id, step, system_prompt, messages)
            except _LLMProtocolError as e:
                loop_failed_reason = f"LLM protocol error after retry: {e}"
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {"type": "run_failed", "step": step, "error": loop_failed_reason},
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
                        "status": action.get("status"),
                    },
                )
                break

            if action.get("action") == "tool_call":
                tool_result, dispatched = self._dispatch_tool(run_id, step, action)
                self._observe_tool_result(action, tool_result)
                run_store.append_event(
                    self.project_id,
                    run_id,
                    {
                        "type": "tool_result",
                        "step": step,
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
                {"type": "run_failed", "step": step, "error": loop_failed_reason},
            )
            break
        else:
            # Loop exhausted without final
            loop_failed_reason = f"step budget exhausted after {MAX_STEPS} steps"
            run_store.append_event(
                self.project_id,
                run_id,
                {"type": "run_failed", "step": MAX_STEPS, "error": loop_failed_reason},
            )

        return self._finalize(run_id, record, task, final_action, loop_failed_reason)

    # ---------- internals ----------

    def _llm_step(
        self,
        run_id: str,
        step: int,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> dict:
        """One LLM call with a single correction retry. Mutates `messages` in place."""
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
                "preview": _truncate(raw, _EVENT_PREVIEW_CHARS),
            },
        )

        try:
            action = _parse_action(raw)
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
                    "retry": True,
                    "preview": _truncate(retry_raw, _EVENT_PREVIEW_CHARS),
                },
            )
            action = _parse_action(retry_raw)
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

    def _dispatch_tool(self, run_id: str, step: int, action: dict) -> tuple[ToolResult, dict]:
        tool_name = action.get("tool_name") or ""
        arguments = action.get("arguments") or {}
        run_store.append_event(
            self.project_id,
            run_id,
            {
                "type": "tool_call",
                "step": step,
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

        # Task 06.2A — optional verification. Runs after the agent's
        # normal final action; never raises. A failing verify command
        # downgrades a `completed` run to `partial`; non-completed
        # statuses (failed/blocked/partial) are preserved either way.
        verification = run_verification(
            self.project_id,
            runtime=self.runtime,
            task_md_override=pre_update_task_md,
        )
        record.verification = verification
        if (
            verification.enabled
            and verification.status == "failed"
            and record.status == RunStatus.COMPLETED
        ):
            record.status = RunStatus.PARTIAL
            blocker_msg = f"verification failed: {verification.command or '(unknown command)'}"
            if blocker_msg not in record.blockers:
                record.blockers.append(blocker_msg)
        run_store.append_event(
            self.project_id,
            run_id,
            {
                "type": "verification",
                "enabled": verification.enabled,
                "status": verification.status,
                "command": verification.command,
                "exit_code": verification.exit_code,
                "duration_ms": verification.duration_ms,
            },
        )

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
        )


# ---------- helpers ----------


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


def _parse_action(raw: str) -> dict:
    """Tolerant JSON parse: accept fenced or wrapped output, but require an action key."""
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
    if parsed.get("action") not in {"tool_call", "final"}:
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
