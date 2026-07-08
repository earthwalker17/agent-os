"""Task 06.1 — bounded on-demand file inspection for the main agent.

The main agent must be able to inspect specific files inside a project's
execution workspace ``repo/`` directory **on demand** — for example when the
user asks "what's in backend/main.py?" or "review the README the agent just
generated." It must NOT auto-inject repo contents into every chat turn.

This module is the bounded, project-scoped tool surface the main agent uses
for those inspections. Every operation routes through ``ToolRuntime`` (which
in turn routes through ``ProjectSandbox``), so:

  - absolute host paths are rejected,
  - ``..`` traversal is rejected,
  - access to other projects' workspaces is rejected,
  - sensitive files (``.env``, ``*.key``, ``.ssh/*``) are rejected.

The wrappers in this module add:

  - GENERAL-workspace rejection (no execution workspace exists there),
  - missing-workspace rejection (clear message instead of an opaque path
    error),
  - tighter response caps than the raw ``ToolRuntime`` (the Coding Agent's
    ``read_file`` caps at 20 000 chars; for chat-context inspection we cap
    tighter so the orchestrator's transcript stays compact),
  - a single normalized return shape (``InspectionResult``) the orchestrator
    can format into its loop transcript without re-implementing the
    formatter for each call.

Read-only by design: ``write_file`` / ``append_file`` / ``run_shell`` are
intentionally not exposed here — main-agent inspection is observation,
not action.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from .manager import get_execution_workspace
from .tool_models import ToolResult
from .tool_runtime import ToolRuntime


log = logging.getLogger(__name__)


GENERAL_PROJECT_ID = "__GENERAL__"


# ----- chat-context caps. Tighter than ``ToolRuntime`` because inspection
# results live in the orchestrator's transcript, which is also carrying
# project memory and conversation history.

INSPECT_MAX_READ_CHARS = 8000
INSPECT_MAX_LIST_ENTRIES = 150
INSPECT_MAX_SEARCH_HITS = 30


# ``retrieve`` (Phase 10.2) is the bounded local-RAG tool — memory + run
# history + repo map as one compact cited bundle (see local_rag.py).
VALID_TOOLS = {"list_files", "read_file", "search_files", "retrieve"}


# ---------- data shape ----------


@dataclass
class InspectionResult:
    """Normalized output for one inspection call.

    ``kind`` is the requested tool name. ``ok`` is True for sandboxed
    success, False for any failure (sandbox violation, missing file,
    GENERAL workspace, etc.). The text content is whatever the user/agent
    needs to see — the orchestrator will inline it into the LLM transcript.
    """

    ok: bool
    kind: str
    path: str = ""
    query: str = ""
    content: str = ""
    truncated: bool = False
    note: str = ""
    error: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "path": self.path,
            "query": self.query,
            "content": self.content,
            "truncated": self.truncated,
            "note": self.note,
            "error": self.error,
            "metadata": self.metadata,
        }


# ---------- workspace checks ----------


def _reject_general_or_missing(
    project_id: str, kind: str, path: str = "", query: str = ""
) -> Optional[InspectionResult]:
    """Return an InspectionResult if the project cannot be inspected; else None."""
    if project_id == GENERAL_PROJECT_ID:
        return InspectionResult(
            ok=False,
            kind=kind,
            path=path,
            query=query,
            error="The GENERAL workspace has no execution workspace; file inspection is unavailable here.",
        )
    if get_execution_workspace(project_id) is None:
        return InspectionResult(
            ok=False,
            kind=kind,
            path=path,
            query=query,
            error=(
                f"Execution workspace not initialized for project {project_id!r}. "
                "Initialize it before requesting file inspection."
            ),
        )
    return None


# ---------- public inspection API ----------


def list_repo_files(project_id: str, path: str = ".") -> InspectionResult:
    """List entries under ``repo/{path}``. Bounded to ``INSPECT_MAX_LIST_ENTRIES``."""
    pre = _reject_general_or_missing(project_id, "list_files", path=path)
    if pre is not None:
        return pre
    raw: ToolResult = ToolRuntime(project_id).list_files(path)
    if not raw.success:
        return InspectionResult(
            ok=False,
            kind="list_files",
            path=path,
            error=raw.error or "list_files failed",
        )

    entries = list(raw.metadata.get("entries") or [])
    truncated_inspect = False
    if len(entries) > INSPECT_MAX_LIST_ENTRIES:
        entries = entries[:INSPECT_MAX_LIST_ENTRIES]
        truncated_inspect = True
    rendered_lines: list[str] = []
    for e in entries:
        tag = "[d]" if e.get("type") == "dir" else "[f]"
        size = e.get("size")
        size_part = f"  ({size}b)" if isinstance(size, int) else ""
        rendered_lines.append(f"{tag} {e.get('name', '')}{size_part}")
    content = "\n".join(rendered_lines) or "(empty directory)"

    truncated = bool(raw.metadata.get("truncated")) or truncated_inspect
    note = ""
    if truncated:
        note = f"Listing truncated at {INSPECT_MAX_LIST_ENTRIES} entries."

    return InspectionResult(
        ok=True,
        kind="list_files",
        path=path,
        content=content,
        truncated=truncated,
        note=note,
        metadata={"entry_count": len(entries)},
    )


def read_repo_file(project_id: str, path: str) -> InspectionResult:
    """Read ``repo/{path}`` as UTF-8 text. Bounded to ``INSPECT_MAX_READ_CHARS``."""
    pre = _reject_general_or_missing(project_id, "read_file", path=path)
    if pre is not None:
        return pre
    if not path or not isinstance(path, str) or not path.strip():
        return InspectionResult(
            ok=False,
            kind="read_file",
            path=path or "",
            error="path is required",
        )
    raw: ToolResult = ToolRuntime(project_id).read_file(path)
    if not raw.success:
        return InspectionResult(
            ok=False,
            kind="read_file",
            path=path,
            error=raw.error or "read_file failed",
        )

    text = raw.output or ""
    runtime_truncated = bool(raw.metadata.get("truncated"))
    truncated = runtime_truncated
    if len(text) > INSPECT_MAX_READ_CHARS:
        text = text[:INSPECT_MAX_READ_CHARS]
        truncated = True

    note = ""
    if truncated:
        full_chars = raw.metadata.get("char_count")
        if isinstance(full_chars, int):
            note = (
                f"File truncated to {len(text)} of {full_chars} chars "
                f"for chat-context limits."
            )
        else:
            note = f"File truncated to {len(text)} chars for chat-context limits."

    return InspectionResult(
        ok=True,
        kind="read_file",
        path=path,
        content=text,
        truncated=truncated,
        note=note,
        metadata={
            "char_count": raw.metadata.get("char_count"),
            "bytes": raw.metadata.get("bytes"),
        },
    )


def search_repo_files(
    project_id: str, query: str, path: str = "."
) -> InspectionResult:
    """Substring-search files under ``repo/{path}``. Bounded to ``INSPECT_MAX_SEARCH_HITS``."""
    pre = _reject_general_or_missing(project_id, "search_files", path=path, query=query)
    if pre is not None:
        return pre
    if not query or not isinstance(query, str) or not query.strip():
        return InspectionResult(
            ok=False,
            kind="search_files",
            path=path,
            query=query or "",
            error="query is required",
        )
    raw: ToolResult = ToolRuntime(project_id).search_files(query, path)
    if not raw.success:
        return InspectionResult(
            ok=False,
            kind="search_files",
            path=path,
            query=query,
            error=raw.error or "search_files failed",
        )

    hits = list(raw.metadata.get("hits") or [])
    truncated_inspect = False
    if len(hits) > INSPECT_MAX_SEARCH_HITS:
        hits = hits[:INSPECT_MAX_SEARCH_HITS]
        truncated_inspect = True
    content = "\n".join(
        f"{h.get('path', '')}:{h.get('line', '')}: {h.get('snippet', '')}" for h in hits
    ) or "(no matches)"

    truncated = bool(raw.metadata.get("truncated")) or truncated_inspect
    note = ""
    if truncated:
        note = f"Results truncated at {INSPECT_MAX_SEARCH_HITS} hits."

    return InspectionResult(
        ok=True,
        kind="search_files",
        path=path,
        query=query,
        content=content,
        truncated=truncated,
        note=note,
        metadata={"hit_count": len(hits)},
    )


# ---------- orchestrator-loop helpers ----------


_INSPECT_REQUEST_KEY = "inspect_request"


def parse_inspect_request(raw: str) -> Optional[dict]:
    """If ``raw`` carries a ``{"inspect_request": {...}}`` directive, return
    the inner dict; otherwise return None (treat the raw output as a text
    answer).

    The primary parse is strict — the directive as the ENTIRE output
    (tolerant of whitespace and a single ``` fence) — so ordinary prose never
    mis-fires. As a fallback, a directive EMBEDDED in narration is extracted
    via a balanced-brace scan: models sometimes preface the JSON with
    commentary, and dropping such a request silently (a) never executes it
    and (b) leaks the raw protocol text into the visible chat answer
    (observed live in the pre-launch E2E). A rare misfire on a quoted
    example is cheap — the channel is read-only and budget-capped.
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # Strip a fence if the LLM wrapped the JSON.
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    parsed: Optional[dict] = None
    if text.startswith("{") and text.endswith("}"):
        try:
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                parsed = loaded
        except json.JSONDecodeError:
            parsed = None
    if parsed is None or not isinstance(parsed.get(_INSPECT_REQUEST_KEY), dict):
        parsed = extract_embedded_request(raw, _INSPECT_REQUEST_KEY)
    if not isinstance(parsed, dict):
        return None
    request = parsed.get(_INSPECT_REQUEST_KEY)
    if not isinstance(request, dict):
        return None
    tool = request.get("tool")
    if tool not in VALID_TOOLS:
        return None
    return request


def extract_embedded_request(raw: str, key: str) -> Optional[dict]:
    """Find the first ``{"<key>": {...}}`` JSON object embedded anywhere in
    ``raw`` (balanced-brace scan, string-literal aware) and return the parsed
    OUTER dict, or ``None``.

    Shared by the inspect and research channels. A bare *mention* of the key
    without a well-formed JSON object spanning it returns ``None``.
    """
    if not raw or not isinstance(raw, str):
        return None
    marker = f'"{key}"'
    pos = raw.find(marker)
    while pos != -1:
        # Try the nearest "{" before the marker, then progressively earlier
        # ones, requiring the balanced object to span the marker itself.
        start = raw.rfind("{", 0, pos + 1)
        while start != -1:
            obj = _scan_balanced_object(raw, start)
            if obj is not None and start + len(obj) > pos:
                try:
                    parsed = json.loads(obj)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict) and isinstance(parsed.get(key), dict):
                    return parsed
            start = raw.rfind("{", 0, start)
        pos = raw.find(marker, pos + len(marker))
    return None


def _scan_balanced_object(text: str, start: int) -> Optional[str]:
    """Return the balanced ``{...}`` substring beginning at ``start``,
    honoring JSON string literals and escapes; ``None`` if unbalanced."""
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def execute_inspect_request(project_id: str, request: dict) -> InspectionResult:
    """Dispatch a parsed ``inspect_request`` dict to the right inspect function.

    Tolerant of missing/empty arguments — returns an ``ok=False`` result with
    a clear error message rather than raising. The orchestrator feeds that
    error back to the LLM, which can then ask the user for clarification or
    try a different path.
    """
    tool = str(request.get("tool", "")).strip()
    if tool == "list_files":
        path = str(request.get("path") or ".").strip() or "."
        return list_repo_files(project_id, path)
    if tool == "read_file":
        path = str(request.get("path") or "").strip()
        return read_repo_file(project_id, path)
    if tool == "search_files":
        query = str(request.get("query") or "").strip()
        path = str(request.get("path") or ".").strip() or "."
        return search_repo_files(project_id, query, path)
    if tool == "retrieve":
        # Phase 10.2 — bounded local RAG over memory + run history + repo.
        # Lazy import keeps the inspect <-> local_rag edge one-directional.
        from . import local_rag

        query = str(request.get("query") or "").strip()
        raw_kinds = request.get("kinds")
        kinds = [str(k) for k in raw_kinds] if isinstance(raw_kinds, list) else None
        result = local_rag.retrieve(project_id, query, kinds=kinds)
        return InspectionResult(
            ok=result.ok,
            kind="retrieve",
            query=query,
            content=result.to_text(),
            truncated=result.truncated,
            note=result.note,
            error=result.error,
        )
    return InspectionResult(
        ok=False,
        kind=tool or "unknown",
        error=f"unknown inspection tool: {tool!r}",
    )


def format_result_for_llm(result: InspectionResult) -> str:
    """Render an InspectionResult into a compact, role-appropriate block for
    inclusion in the orchestrator's LLM transcript.

    The format intentionally labels what came from real inspection so the
    model can distinguish "verified by file read" from "inferred from memory"
    in its eventual reply to the user.
    """
    header_bits: list[str] = [f"tool={result.kind}", f"ok={'true' if result.ok else 'false'}"]
    if result.path:
        header_bits.append(f"path={result.path}")
    if result.query:
        header_bits.append(f"query={result.query!r}")
    if result.truncated:
        header_bits.append("truncated=true")
    header = "INSPECTION RESULT: " + ", ".join(header_bits)

    body_parts: list[str] = [header]
    if result.error:
        body_parts.append(f"error: {result.error}")
    if result.note:
        body_parts.append(f"note: {result.note}")
    if result.content:
        body_parts.append("---")
        body_parts.append(result.content)
    return "\n".join(body_parts)


def inspect_system_prompt_section() -> str:
    """Return the system-prompt fragment that describes the inspect channel.

    Injected into the orchestrator system prompt ONLY for non-GENERAL
    projects whose execution workspace has been initialized — so chats
    without an inspectable workspace never get told about a channel they
    can't use.
    """
    return (
        "## File Inspection\n"
        "You can inspect specific files inside this project's execution workspace "
        "`repo/` directory ON DEMAND when the user's question genuinely requires "
        "looking at a file's contents. Do NOT inspect files for routine planning, "
        "discussion, or status questions — those should be answered from memory.\n"
        "\n"
        "When you need to inspect a file, respond with EXACTLY a JSON object of "
        "this shape and NOTHING ELSE (no prose, no markdown fence, no greeting):\n"
        "\n"
        "  {\"inspect_request\": {\"tool\": \"list_files\", \"path\": \".\"}}\n"
        "  {\"inspect_request\": {\"tool\": \"read_file\", \"path\": \"path/relative/to/repo\"}}\n"
        "  {\"inspect_request\": {\"tool\": \"search_files\", \"query\": \"...\", \"path\": \".\"}}\n"
        "  {\"inspect_request\": {\"tool\": \"retrieve\", \"query\": \"...\"}}\n"
        "\n"
        "The `retrieve` tool is bounded LOCAL retrieval: it returns compact, cited "
        "evidence from THIS project's memory (status/decisions/research/lessons), its "
        "recent run history (plans/failures/fixes), and a repo map. Prefer it when the "
        "answer may already live in the project's own memory or past runs. (Optional "
        "`\"kinds\": [\"memory\",\"runs\",\"repo\"]` narrows the sources.)\n"
        "\n"
        "Rules:\n"
        "- Paths are relative to `repo/`. Never use absolute paths. Never use `..`.\n"
        "- You may issue at most a few inspections per turn; the system enforces a hard cap.\n"
        "- When you're ready to answer the user, respond with NORMAL TEXT (no JSON).\n"
        "- In your final reply, clearly distinguish what you knew from memory "
        "vs. what you verified by reading files.\n"
        "- If the user's request is ambiguous, ask a short clarifying question "
        "INSTEAD of inspecting random files. Listing the directory is appropriate "
        "when the user names a file you can't locate.\n"
    )
