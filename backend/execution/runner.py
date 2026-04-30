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


MAX_STEPS = 8

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

    def run_task(self, task: TaskSpec) -> ResultSummary:
        ws = get_execution_workspace(self.project_id)
        if ws is None:
            raise FileNotFoundError(
                f"Execution workspace not initialized for project {self.project_id!r}"
            )

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

        record.status = RunStatus(status_str)
        record.completed_at = datetime.utcnow()
        record.files_changed = files_changed
        record.commands_run = commands_run
        record.blockers = blockers
        run_store.write_run_json(self.project_id, run_id, record)

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

        notes = ""
        if loop_failed_reason and final_action is None:
            notes = f"Run did not produce a final action: {loop_failed_reason}"
        result_md = run_store.render_result_md(record, summary, notes=notes)
        run_store.write_result_md(self.project_id, run_id, result_md)
        result_path = str(run_store.get_run_dir(self.project_id, run_id) / "result.md")

        return ResultSummary(
            run_id=run_id,
            status=record.status.value,
            summary=summary,
            files_changed=files_changed,
            commands_run=commands_run,
            blockers=blockers,
            result_path=result_path,
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
