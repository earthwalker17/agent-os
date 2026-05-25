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
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .manager import get_project_execution_dir
from .models import RunRecord
from .verification import render_verification_section


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
        f"## Notes for Main Agent\n{notes.strip() or '_(none)_'}\n"
    )
