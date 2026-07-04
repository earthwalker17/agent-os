"""Filesystem layer for per-run artifacts.

Layout under `execution_workspaces/{project_id}/runs/{run_id}/`:

    task_card.md   — the original task card (input)
    events.jsonl   — append-only event log (one JSON object per line)
    run.json       — structured RunRecord serialization
    result.md      — human-readable result summary
    plan.json      — Phase 5 execution plan / task graph
    integration.json — Phase 9 team-run integration detail (read on demand)
    diff.patch     — Phase 7 post-run diff (redacted, read on demand)
    deployment.json— Phase 8 redacted deploy contract + result (read on demand)
    deploy.log     — Phase 8 redacted build/CLI log (bounded, read on demand)

This module only knows about reading/writing those files. The runner owns the
loop logic; the API endpoints query through this store.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .manager import get_execution_root, get_project_execution_dir
from .models import ExecutionPlan, RunRecord, RunStatus
from .verification import render_verification_section
from .browser_verification import render_browser_verification_section
from .visual_judge import render_visual_review_section


def new_run_id() -> str:
    """Sortable timestamped run id with a short random suffix."""
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp sibling + ``os.replace``).

    The runner rewrites run.json / plan.json many times during a multi-task run
    (once per task boundary + each verification phase) while the UI polls every
    2 s. A plain truncate-then-write leaves a window where a reader sees an
    empty / half-written file — on Windows this surfaces as a transient 404 /
    'unknown' status. ``os.replace`` is atomic on the same volume (NTFS
    included), so a concurrent reader always sees either the old or the new
    complete file, never a torn one.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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


def read_task_card(project_id: str, run_id: str) -> tuple[str, str]:
    """Return ``(title, body)`` parsed from task_card.md.

    Inverse of :func:`write_task_card`, which writes ``# {title}\\n\\n{body}\\n``.
    Parses by splitting off only the first line (so ``#`` inside the title or
    blank lines inside the body survive) — used by the retry endpoint to
    reconstruct the original task card for a fresh run. Returns ``("", "")``
    when the file is absent.
    """
    path = get_run_dir(project_id, run_id) / "task_card.md"
    if not path.exists():
        return "", ""
    text = path.read_text(encoding="utf-8")
    first, _, rest = text.partition("\n")
    title = first[2:] if first.startswith("# ") else first
    return title.strip(), rest.strip("\n")


# Phase 9 — per-run append locks. A team run's parallel task units all append
# to the SAME events.jsonl from different threads; bare `open("a")` appends can
# tear a line if a write splits across syscalls, so every append takes the
# run's lock. Negligible cost for the single-writer (sequential) case. The
# registry grows one small Lock per run touched in this process — bounded in
# practice and reclaimed on restart.
_EVENT_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_EVENT_LOCKS_GUARD = threading.Lock()


def _event_lock(project_id: str, run_id: str) -> threading.Lock:
    with _EVENT_LOCKS_GUARD:
        return _EVENT_LOCKS.setdefault((project_id, run_id), threading.Lock())


def append_event(project_id: str, run_id: str, event: dict[str, Any]) -> None:
    """Append one JSON event line to events.jsonl (thread-safe per run).

    Caller is responsible for bounding the size of `event` — never dump full
    file contents or full stdout/stderr here.
    """
    payload = dict(event)
    payload.setdefault("timestamp", datetime.utcnow().isoformat() + "Z")
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _event_lock(project_id, run_id):
        with (get_run_dir(project_id, run_id) / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def read_events(project_id: str, run_id: str) -> list[dict]:
    """Return the run's events in file (chronological) order.

    Reads ``events.jsonl`` one line at a time and JSON-parses each; malformed
    or empty lines are skipped (the log is append-only and a crash could leave
    a half-written final line). Returns ``[]`` when the file is absent. Each
    event carries at least ``type`` and ``timestamp`` (stamped by
    :func:`append_event`).
    """
    path = get_run_dir(project_id, run_id) / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    return events


def write_run_json(project_id: str, run_id: str, record: RunRecord) -> None:
    path = get_run_dir(project_id, run_id) / "run.json"
    _atomic_write_text(path, record.model_dump_json(indent=2))


def read_run_json(project_id: str, run_id: str) -> dict | None:
    path = get_run_dir(project_id, run_id) / "run.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_plan_json(project_id: str, run_id: str, plan: ExecutionPlan) -> None:
    """Persist the run's execution plan as a standalone ``plan.json`` artifact.

    The plan is also embedded in run.json (``RunRecord.plan``); this dedicated
    artifact gives a clean, machine-readable task-graph view that the runner
    re-writes as task statuses settle (Phase 5).
    """
    path = get_run_dir(project_id, run_id) / "plan.json"
    _atomic_write_text(path, plan.model_dump_json(indent=2))


def read_plan_json(project_id: str, run_id: str) -> dict | None:
    path = get_run_dir(project_id, run_id) / "plan.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_result_md(project_id: str, run_id: str, content: str) -> None:
    _atomic_write_text(get_run_dir(project_id, run_id) / "result.md", content)


def read_result_md(project_id: str, run_id: str) -> str | None:
    path = get_run_dir(project_id, run_id) / "result.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


# Phase 7 — the post-run diff is stored as a standalone artifact (read on
# demand via the bounded ``/diff`` endpoint), never inlined into run.json /
# result.md / the main-agent context (§6 context hygiene). The text is already
# redacted + bounded by ``git_ops.capture_diff`` before it reaches here.
_DIFF_PATCH_MAX_CHARS = 200_000


def write_diff_patch(project_id: str, run_id: str, content: str) -> None:
    text = content or ""
    if len(text) > _DIFF_PATCH_MAX_CHARS:
        text = text[:_DIFF_PATCH_MAX_CHARS] + "\n... [diff.patch truncated] ...\n"
    _atomic_write_text(get_run_dir(project_id, run_id) / "diff.patch", text)


def read_diff_patch(project_id: str, run_id: str) -> str | None:
    path = get_run_dir(project_id, run_id) / "diff.patch"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


# Phase 8 — deployment artifacts. ``deployment.json`` holds the redacted deploy
# contract + result (deployment id/url/target/timestamps); ``deploy.log`` holds
# the bounded raw build/CLI log. Both are read on demand (never inlined into
# run.json / result.md / the main-agent context, §6). The connector MUST redact
# any string (``credentials.redact(text, project_id)``) BEFORE it reaches here.
_DEPLOY_LOG_MAX_CHARS = 200_000


def write_deployment_json(project_id: str, run_id: str, data: dict) -> None:
    path = get_run_dir(project_id, run_id) / "deployment.json"
    _atomic_write_text(path, json.dumps(data, indent=2, default=str))


def read_deployment_json(project_id: str, run_id: str) -> dict | None:
    path = get_run_dir(project_id, run_id) / "deployment.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# Phase 9 — the team run's integration detail (per-wave applied files,
# conflicts, per-task decisions). The compact aggregate lives on
# ``RunRecord.integration``; this artifact holds the full breakdown, read on
# demand (never inlined into the main-agent context, §6).


def write_integration_json(project_id: str, run_id: str, data: dict) -> None:
    path = get_run_dir(project_id, run_id) / "integration.json"
    _atomic_write_text(path, json.dumps(data, indent=2, default=str))


def read_integration_json(project_id: str, run_id: str) -> dict | None:
    path = get_run_dir(project_id, run_id) / "integration.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_deploy_log(project_id: str, run_id: str, content: str) -> None:
    text = content or ""
    if len(text) > _DEPLOY_LOG_MAX_CHARS:
        text = text[:_DEPLOY_LOG_MAX_CHARS] + "\n... [deploy.log truncated] ...\n"
    _atomic_write_text(get_run_dir(project_id, run_id) / "deploy.log", text)


def read_deploy_log(project_id: str, run_id: str) -> str | None:
    path = get_run_dir(project_id, run_id) / "deploy.log"
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
            # Phase 9 — settle the transient integration sub-status so a crash
            # mid-integration can't leave the UI poll gates reading the run as
            # active forever.
            record.integration_state = None
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


def _render_plan_section(plan: Optional[ExecutionPlan]) -> str:
    """Render the Execution Plan + per-task summary for a multi-task run.

    Returns ``""`` for runs with no plan or a single-task (simple/fallback)
    plan, so result.md for simple runs stays byte-identical to the legacy
    output. Multi-task runs get a readable task-by-task execution summary.
    """
    if plan is None or len(plan.tasks) <= 1:
        return ""
    lines: list[str] = ["## Execution Plan"]
    if plan.goal:
        lines.append(f"**Goal:** {plan.goal}")
    if plan.analysis:
        lines.append("")
        lines.append(plan.analysis)
    if plan.risks:
        lines.append("")
        lines.append("**Risks:**")
        lines.extend(f"- {r}" for r in plan.risks)
    lines.append("")
    lines.append("## Tasks")
    # Phase 9 — team runs annotate each task with its role / wave / workspace
    # so the trace reads at a glance; sequential runs render the legacy line
    # byte-identical.
    team = getattr(plan, "execution_mode", "sequential") == "team"
    for i, t in enumerate(plan.tasks, start=1):
        line = f"{i}. [{t.status.value}] {t.id} — {t.title}"
        if team:
            tags = [f"role: {t.role}"]
            if t.wave is not None:
                tags.append(f"wave {t.wave}")
            if t.workspace == "patch":
                tags.append("patch workspace")
            line += f" ({', '.join(tags)})"
        lines.append(line)
        if t.summary:
            lines.append(f"   - {t.summary}")
        if t.files_changed:
            lines.append(f"   - files: {', '.join(t.files_changed)}")
        if t.commands_run:
            lines.append(f"   - commands: {', '.join(t.commands_run)}")
        if t.blockers:
            lines.append(f"   - blockers: {', '.join(t.blockers)}")
    return "\n".join(lines) + "\n\n"


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
        f"{_visual_review_block(record.visual_review)}"
        f"{_render_plan_section(record.plan)}"
        f"{_integration_section(record)}"
        f"{_git_section(record)}"
        f"{_deployment_section(record)}"
        f"## Notes for Main Agent\n{notes.strip() or '_(none)_'}\n"
    )


def _integration_section(record: RunRecord) -> str:
    """Render the Phase 9 team-integration section for result.md.

    Returns ``""`` for sequential runs (no ``integration`` on the record), so
    legacy result.md output stays byte-identical. Compact aggregate only —
    the per-wave / per-task breakdown lives in ``integration.json``.
    """
    integ = record.integration
    if integ is None or not integ.enabled:
        return ""
    lines: list[str] = ["## Integration"]
    lines.append(f"- waves: {integ.waves}")
    lines.append(f"- files applied: {len(integ.files_applied)}")
    if integ.conflicts:
        lines.append(f"- conflicts: {len(integ.conflicts)}")
        for c in integ.conflicts:
            lines.append(
                f"  - `{c.path}` — applied {c.applied_task}, rejected {c.rejected_task} (wave {c.wave})"
            )
    else:
        lines.append("- conflicts: none")
    if integ.notes:
        lines.append(f"- notes: {integ.notes}")
    return "\n".join(lines) + "\n\n"


def _git_section(record: RunRecord) -> str:
    """Render the Phase 7 Project Ops (Git/GitHub) section for result.md.

    Returns ``""`` when the run has no Git activity, so result.md for non-git
    runs stays byte-identical to the legacy output (same discipline as
    ``_render_plan_section`` / ``_visual_review_block``). Metadata only — never
    the raw diff, never a secret.
    """
    if not any(
        (
            record.pre_run_checkpoint,
            record.commit_sha,
            record.diff_stat,
            record.pr_url,
            record.pushed,
        )
    ):
        return ""
    lines: list[str] = ["## Project Ops"]
    if record.branch:
        lines.append(f"- branch: `{record.branch}`")
    if record.base_commit:
        lines.append(f"- pre-run base: `{record.base_commit[:12]}`")
    if record.pre_run_checkpoint:
        tag = f" (tag `{record.checkpoint_tag}`)" if record.checkpoint_tag else ""
        lines.append(f"- checkpoint: `{record.pre_run_checkpoint[:12]}`{tag}")
    if record.diff_stat:
        lines.append(f"- diff: {record.diff_stat}")
    if record.commit_sha:
        lines.append(f"- commit: `{record.commit_sha[:12]}`")
    if record.pushed:
        lines.append("- pushed: yes")
    if record.pr_url:
        num = f" (#{record.pr_number})" if record.pr_number else ""
        lines.append(f"- pull request{num}: {record.pr_url}")
    return "\n".join(lines) + "\n\n"


def _deployment_section(record: RunRecord) -> str:
    """Render the Phase 8 Deployment section for result.md.

    Returns ``""`` when the run has no deployment, so result.md for non-deploy
    runs stays byte-identical to the legacy output (same empty-when-absent
    discipline as ``_git_section``). Metadata only — never a secret, never the
    raw build log (that lives in ``deploy.log``).
    """
    if not any((record.deployment_id, record.deployment_url, record.deployment_target)):
        return ""
    lines: list[str] = ["## Deployment"]
    if record.deployment_target:
        lines.append(f"- target: `vercel:{record.deployment_target}`")
    if record.deployment_url:
        lines.append(f"- url: {record.deployment_url}")
    if record.deployment_id:
        lines.append(f"- deployment: `{record.deployment_id}`")
    return "\n".join(lines) + "\n\n"


def _visual_review_block(visual_review) -> str:
    """Render the optional Visual Review section with surrounding spacing.

    Returns ``""`` when no review was attempted (so result.md for runs without
    visual review stays byte-identical to the legacy output), else the section
    followed by a blank line so it reads cleanly between the browser block and
    the plan/notes sections.
    """
    section = render_visual_review_section(visual_review)
    return f"{section}\n" if section else ""
