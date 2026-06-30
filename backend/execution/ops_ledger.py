"""Phase 8 — the project OPS ledger writer.

The ONE writer of ``projects/{id}/OPS.md`` (deployment / env / migration /
webhook records). Every external-action confirm-execute path calls
``append_ops_entry``; it builds the entry from ids / URLs / key-NAMES only
(never a value) and runs the whole block through ``credentials.redact`` at the
call site — so there is no duplicated no-leak logic and no secret can reach the
ledger. ``OPS.md`` is deliberately excluded from the chat-judge + reconciler
writable sets (``memory_engine``), so no LLM can paraphrase or fabricate
deployment facts; it is written only here, deterministically.

It also appends a redacted audit line to
``execution_workspaces/{id}/ops/events.jsonl`` so project-scoped external
actions (env-set / link / webhook) that aren't tied to a build run still leave
a durable trace.

OPS.md is an append-only ledger under a single ``## Ledger`` heading (each entry
is a self-describing ``###`` block with an id line for grep + dedup). We avoid a
replace-based "current pointer" because ``memory_engine.apply_update`` appends to
end-of-file, which would let later entries bleed into a replaced section's body.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import credentials
import memory_engine

from .manager import get_project_execution_dir

log = logging.getLogger(__name__)

_PROJECTS_DIR = Path(__file__).resolve().parent.parent.parent / "projects"

# The action kinds the ledger records (informational tag in each entry header).
KINDS = ("deploy", "redeploy", "rollback", "env_set", "migration", "link", "webhook")


def _project_dir(project_id: str) -> Path:
    return _PROJECTS_DIR / project_id


def append_ops_entry(
    project_id: str,
    kind: str,
    title: str,
    fields: dict,
    *,
    timestamp: str,
    dedup_key: Optional[str] = None,
) -> bool:
    """Append one OPS.md ledger entry (and a project ops-events line).

    ``fields`` must already be ids / URLs / key-NAMES — never a value. The whole
    rendered block is redacted defensively. ``dedup_key`` (e.g. a deployment id)
    makes a confirm-retry idempotent: if it already appears in OPS.md the write
    is skipped. Never raises — a ledger failure must not fail an external action.
    Returns ``True`` iff OPS.md was written.
    """
    try:
        base = _project_dir(project_id)
        if not base.exists():
            # The endpoint validates the project first; if the dir is somehow
            # absent, don't fabricate one — just record the audit event.
            _append_ops_event(project_id, kind, title, fields, timestamp)
            return False
        # Ensure OPS.md + its canonical section exist (idempotent, additive).
        memory_engine.ensure_memory_scaffold(base, project_id)

        if dedup_key:
            try:
                existing = (base / "OPS.md").read_text(encoding="utf-8")
            except OSError:
                existing = ""
            if str(dedup_key) in existing:
                _append_ops_event(project_id, kind, title, fields, timestamp)
                return False

        lines = [f"### {timestamp} · {kind} — {title}"]
        for k, v in fields.items():
            if v is None:
                continue
            sval = str(v).strip()
            if sval:
                lines.append(f"- {k}: {sval}")
        content = credentials.redact("\n".join(lines), project_id)

        ok = memory_engine.apply_update(
            base_dir=base,
            allow=memory_engine.OPS_WRITABLE,
            filename="OPS.md",
            section="Ledger",
            content=content,
            action="append",
        )
        _append_ops_event(project_id, kind, title, fields, timestamp)
        return ok
    except Exception as exc:  # noqa: BLE001 — best-effort; never fail the action
        log.warning("ops_ledger: could not append entry for %s: %s", project_id, exc)
        return False


def _append_ops_event(project_id: str, kind: str, title: str, fields: dict, timestamp: str) -> None:
    """Append a redacted JSON audit line to the project ops-events log."""
    try:
        d = get_project_execution_dir(project_id) / "ops"
        d.mkdir(parents=True, exist_ok=True)
        obj = {"ts": timestamp, "kind": kind, "title": title}
        for k, v in fields.items():
            if v is not None and str(v).strip():
                obj[k] = str(v)
        line = credentials.redact(json.dumps(obj, default=str), project_id)
        with open(d / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        log.warning("ops_ledger: could not write ops event for %s: %s", project_id, exc)


def read_ops_events(project_id: str) -> list[dict]:
    """Read the project ops-events audit log (parsed). Read-only helper."""
    path = get_project_execution_dir(project_id) / "ops" / "events.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out
