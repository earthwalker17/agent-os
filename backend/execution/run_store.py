"""Filesystem layer for per-run artifacts.

Layout under `execution_workspaces/{project_id}/runs/{run_id}/`:

    task_card.md   — the original task card (input)
    events.jsonl   — append-only event log (one JSON object per line)
    run.json       — structured RunRecord serialization
    result.md      — human-readable result summary

This module only knows about reading/writing those four files. The runner
owns the loop logic; the API endpoints query through this store.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .manager import get_execution_root, get_project_execution_dir
from .models import RunRecord, RunStatus
from .verification import render_verification_section
from .browser_verification import render_browser_verification_section


def new_run_id() -> str:
    """Sortable timestamped run id with a short random suffix."""
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def get_runs_dir(project_id: str) -> Path:
    return get_project_execution_dir(project_id) / "runs"


def get_run_dir(project_id: str, run_id: str) -> Path:
    return get_runs_dir(project_id) / run_id


def init_run_dir(project_id: str, run_id: str) -> Path:
    run_dir = get_run_dir(project_id, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    # Touch events.jsonl so consumers can stream it before the first event.
    (run_dir / "events.jsonl").touch(exist_ok=True)
    return run_dir


def write_task_card(project_id: str, run_id: str, title: str, task_card: str) -> None:
    text = f"# {title}\n\n{task_card.strip()}\n"
    (get_run_dir(project_id, run_id) / "task_card.md").write_text(text, encoding="utf-8")


def append_event(project_id: str, run_id: str, event: dict[str, Any]) -> None:
    """Append one JSON event line to events.jsonl.

    Caller is responsible for bounding the size of `event` — never dump full
    file contents or full stdout/stderr here.
    """
    payload = dict(event)
    payload.setdefault("timestamp", datetime.utcnow().isoformat() + "Z")
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with (get_run_dir(project_id, run_id) / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_run_json(project_id: str, run_id: str, record: RunRecord) -> None:
    path = get_run_dir(project_id, run_id) / "run.json"
    path.write_text(record.model_dump_json(indent=2), encoding="utf-8")


def read_run_json(project_id: str, run_id: str) -> dict | None:
    path = get_run_dir(project_id, run_id) / "run.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_result_md(project_id: str, run_id: str, content: str) -> None:
    (get_run_dir(project_id, run_id) / "result.md").write_text(content, encoding="utf-8")


def read_result_md(project_id: str, run_id: str) -> str | None:
    path = get_run_dir(project_id, run_id) / "result.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def list_runs(project_id: str) -> list[dict]:
    """Return run records for a project, newest first.

    Each entry is the raw run.json dict; runs missing run.json are skipped.
    """
    runs_dir = get_runs_dir(project_id)
    if not runs_dir.exists():
        return []
    entries = []
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        record = read_run_json(project_id, child.name)
        if record is None:
            entries.append({"run_id": child.name, "status": "unknown"})
        else:
            entries.append(record)
    entries.sort(key=lambda r: r.get("run_id", ""), reverse=True)
    return entries


def sweep_stuck_runs() -> list[str]:
    """Promote any run.json still marked ``running`` to ``failed``.

    Runs become "stuck running" when the backend process exits (server
    restart, crash, machine reboot) while a Coding Agent loop is still
    in flight: the worker thread dies with the process, so the
    ``BackgroundRunManager``'s in-process crash-handler never gets to
    flip the status.

    This sweep is meant to be called once at process startup. It walks
    every run record under ``execution_workspaces/*/runs/*/`` and, for
    each one whose status is still ``running``, rewrites it to
    ``failed`` with an "interrupted" blocker, sets ``completed_at`` to
    now, appends a ``run_interrupted`` event, and writes a minimal
    ``result.md`` so the UI doesn't show a spinner forever.

    Returns the list of ``"{project_id}/{run_id}"`` strings that were
    rewritten — useful for logging at startup. Best-effort: never
    raises; corrupt or unreadable run.json entries are skipped.
    """
    swept: list[str] = []
    root = get_execution_root()
    if not root.exists() or not root.is_dir():
        return swept
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        runs_dir = project_dir / "runs"
        if not runs_dir.exists() or not runs_dir.is_dir():
            continue
        for run_dir in runs_dir.iterdir():
            if not run_dir.is_dir():
                continue
            run_json_path = run_dir / "run.json"
            if not run_json_path.exists():
                continue
            try:
                raw = json.loads(run_json_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if raw.get("status") != RunStatus.RUNNING.value:
                continue
            try:
                record = RunRecord(**raw)
            except Exception:  # noqa: BLE001
                # Fall back to a minimal record if validation fails.
                record = RunRecord(
                    run_id=raw.get("run_id") or run_dir.name,
                    project_id=raw.get("project_id") or project_dir.name,
                    task_title=str(raw.get("task_title") or ""),
                )
            record.status = RunStatus.FAILED
            record.completed_at = datetime.utcnow()
            interrupted_msg = "run interrupted before finalize (server restart or crash)"
            if interrupted_msg not in record.blockers:
                record.blockers = list(record.blockers) + [interrupted_msg]
            try:
                write_run_json(project_dir.name, run_dir.name, record)
                append_event(
                    project_dir.name,
                    run_dir.name,
                    {"type": "run_interrupted", "reason": interrupted_msg},
                )
                # Only seed result.md if the runner never got that far.
                if not (run_dir / "result.md").exists():
                    write_result_md(
                        project_dir.name,
                        run_dir.name,
                        render_result_md(
                            record,
                            "Run did not finalize before the backend process exited.",
                            notes=interrupted_msg,
                        ),
                    )
                swept.append(f"{project_dir.name}/{run_dir.name}")
            except Exception:  # noqa: BLE001
                continue
    return swept


_PLACEHOLDER_VALUES = {"_(no summary provided)_", "_(none)_"}


def _extract_md_section(text: str, name: str) -> str:
    """Return the body of a ``## {name}`` section from rendered result.md.

    Captures everything between the heading and the next ``## `` heading (or
    EOF). Returns ``""`` when the section is absent or holds only a
    placeholder marker. Used to preserve the human-written Summary / Notes
    when re-rendering result.md after a post-hoc verification step.
    """
    if not text:
        return ""
    match = re.search(
        rf"^## {re.escape(name)}\n(.*?)(?=^## |\Z)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    if match is None:
        return ""
    body = match.group(1).strip()
    return "" if body in _PLACEHOLDER_VALUES else body


def rerender_result_md(project_id: str, run_id: str, record: RunRecord) -> None:
    """Regenerate result.md from ``record`` while preserving Summary / Notes.

    The runner is the normal author of result.md; this is for endpoints that
    update a finalized run in place (e.g. Task 06.2C's user-triggered browser
    verification) and need the rendered verification blocks refreshed without
    losing the original summary text.
    """
    existing = read_result_md(project_id, run_id) or ""
    summary = _extract_md_section(existing, "Summary")
    notes = _extract_md_section(existing, "Notes for Main Agent")
    write_result_md(project_id, run_id, render_result_md(record, summary, notes=notes))


def render_result_md(record: RunRecord, summary: str, notes: str = "") -> str:
    def _bullets(items: list[str]) -> str:
        return "\n".join(f"- {x}" for x in items) if items else "_(none)_"

    return (
        f"# Run Result\n\n"
        f"## Status\n{record.status.value}\n\n"
        f"## Summary\n{summary.strip() or '_(no summary provided)_'}\n\n"
        f"## Files Changed\n{_bullets(record.files_changed)}\n\n"
        f"## Commands Run\n{_bullets(record.commands_run)}\n\n"
        f"## Blockers\n{_bullets(record.blockers)}\n\n"
        f"{render_verification_section(record.verification)}\n"
        f"{render_browser_verification_section(record.browser_verification)}\n"
        f"## Notes for Main Agent\n{notes.strip() or '_(none)_'}\n"
    )
