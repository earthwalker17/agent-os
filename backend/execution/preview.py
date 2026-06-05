"""Task 06.2D — managed preview dev-server layer.

A small, process-local registry of long-lived dev servers, one per project.
It exists for two reasons:

  1. **Persistent preview after browser verification.** When a user-triggered
     browser verification passes, the dev server it spun up is handed off here
     (see :func:`adopt_preview`) instead of being torn down, so the captured
     preview URL stays usable.
  2. **Explicit Start/Stop preview from the Runs panel.** The user can start a
     managed dev server for a project and stop it again.

Design constraints mirror ``browser_verification.py``:

  - **Sandbox-respecting.** The dev-server command is validated through
    ``ProjectSandbox.validate_command`` before launch.
  - **No duplicate servers.** At most one managed preview per project; a start
    request while one is already running is a no-op that returns the existing
    status.
  - **Readiness-gated URL.** :func:`start_preview` only reports ``running`` (and
    a usable URL) once the URL actually responds. Early process exit is
    detected and reported instead of hanging the readiness budget.
  - **Bounded output, drained pipes.** stdout/stderr are drained by background
    threads (reusing ``_StreamDrainer``) so the child never deadlocks on a full
    pipe — the same Windows fix learned in 06.2B.
  - **Clean shutdown.** :func:`shutdown_all_previews` tears every managed server
    down; the FastAPI shutdown hook calls it.

This module deliberately reuses the lifecycle helpers in
``browser_verification.py`` rather than duplicating them, and is imported by
that module's callers (not the other way around) to keep the dependency edge
one-directional.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from .browser_verification import (
    DEFAULT_DEV_COMMAND,
    DEFAULT_DEV_URL,
    DEFAULT_READINESS_TIMEOUT_SECONDS,
    DEFAULT_TERMINATE_GRACE_SECONDS,
    _BROWSER_OUTPUT_PREVIEW_CHARS,
    _StreamDrainer,
    _build_output_preview,
    _format_exception,
    _start_dev_server,
    _terminate_dev_server,
    _wait_for_url_or_exit,
)
from .sandbox import ProjectSandbox, SandboxViolation


log = logging.getLogger(__name__)


@dataclass
class PreviewServer:
    """A live managed dev server for one project."""

    project_id: str
    command: str
    url: str
    proc: subprocess.Popen
    stdout_drainer: Optional[_StreamDrainer]
    stderr_drainer: Optional[_StreamDrainer]
    started_at: str


# Process-local registry. Single-user, single-process — no cross-process
# coordination needed (matches the rest of the local-first design).
_registry: dict[str, PreviewServer] = {}
_starting: set[str] = set()
_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _is_alive(server: Optional[PreviewServer]) -> bool:
    return server is not None and server.proc.poll() is None


def _has_package_json(project_id: str) -> bool:
    try:
        return (ProjectSandbox(project_id).repo_dir / "package.json").exists()
    except Exception:  # noqa: BLE001
        return False


def _deps_installed(project_id: str) -> bool:
    """True when ``node_modules`` exists — i.e. the dev server can be started.

    This is the on-disk source of truth for "deps are installed", independent
    of *how* they got there (the 06.2E command-verification ``npm install``, a
    browser verification, or a manual install). The Runs panel uses it to
    enable Start preview as soon as the project is preview-ready.
    """
    try:
        return (ProjectSandbox(project_id).repo_dir / "node_modules").is_dir()
    except Exception:  # noqa: BLE001
        return False


def _status_dict(project_id: str, server: Optional[PreviewServer]) -> dict:
    running = _is_alive(server)
    return {
        "project_id": project_id,
        "running": running,
        "url": server.url if (running and server is not None) else None,
        "command": server.command if (running and server is not None) else None,
        "started_at": server.started_at if (running and server is not None) else None,
        "has_package_json": _has_package_json(project_id),
        "deps_installed": _deps_installed(project_id),
    }


def get_preview_status(project_id: str) -> dict:
    """Return whether a managed preview server is running for ``project_id``.

    Reaps a dead entry (process exited on its own) so the reported state always
    reflects reality.
    """
    with _lock:
        server = _registry.get(project_id)
        if server is not None and not _is_alive(server):
            _registry.pop(project_id, None)
            server = None
        return _status_dict(project_id, server)


def adopt_preview(
    project_id: str,
    proc: subprocess.Popen,
    stdout_drainer: Optional[_StreamDrainer],
    stderr_drainer: Optional[_StreamDrainer],
    command: str,
    url: str,
) -> bool:
    """Take ownership of an already-running dev server (keep-alive handoff).

    Used by the browser-verification keep-alive path: rather than tearing the
    server down after a passing verification, the caller hands it here so the
    preview URL stays live. Returns ``True`` when adopted (caller must NOT
    terminate it), ``False`` when a preview is already running for this project
    (caller should tear its own down to avoid a duplicate).
    """
    with _lock:
        existing = _registry.get(project_id)
        if _is_alive(existing):
            return False
        if existing is not None:
            _registry.pop(project_id, None)
        _registry[project_id] = PreviewServer(
            project_id=project_id,
            command=command,
            url=url,
            proc=proc,
            stdout_drainer=stdout_drainer,
            stderr_drainer=stderr_drainer,
            started_at=_now_iso(),
        )
        return True


def start_preview(
    project_id: str,
    *,
    command: str = DEFAULT_DEV_COMMAND,
    url: str = DEFAULT_DEV_URL,
    readiness_timeout_seconds: int = DEFAULT_READINESS_TIMEOUT_SECONDS,
    process_starter: Optional[Callable[[str, "object"], subprocess.Popen]] = None,
) -> dict:
    """Start a managed dev server for ``project_id`` and wait until reachable.

    Returns a status dict with an extra ``ok`` flag and, on failure, an
    ``error`` (and optional ``output_preview``). Never raises.

    - Validates the command through the sandbox.
    - No-ops (``already_running=True``) when a preview is already up — never
      spawns a duplicate.
    - Only reports ``running=True`` after the URL actually responds; a dev
      server that exits early or never binds is torn down and reported as a
      failure (so the UI never opens a dead URL).
    """
    sandbox = ProjectSandbox(project_id)
    try:
        sandbox.validate_command(command)
    except SandboxViolation as exc:
        return {
            "ok": False,
            "running": False,
            "project_id": project_id,
            "url": None,
            "error": f"sandbox rejected preview command: {exc}",
        }

    repo_dir = sandbox.repo_dir
    if not repo_dir.exists() or not repo_dir.is_dir():
        return {
            "ok": False,
            "running": False,
            "project_id": project_id,
            "url": None,
            "error": f"repo dir does not exist: {repo_dir}",
        }

    with _lock:
        existing = _registry.get(project_id)
        if _is_alive(existing):
            return {"ok": True, "already_running": True, **_status_dict(project_id, existing)}
        if existing is not None:
            _registry.pop(project_id, None)
        if project_id in _starting:
            return {
                "ok": False,
                "running": False,
                "project_id": project_id,
                "url": None,
                "error": "a preview server is already starting for this project",
            }
        _starting.add(project_id)

    try:
        starter = process_starter or _start_dev_server
        try:
            proc = starter(command, repo_dir)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "running": False,
                "project_id": project_id,
                "url": None,
                "error": f"failed to start preview server: {_format_exception(exc)}",
            }

        buf_cap = _BROWSER_OUTPUT_PREVIEW_CHARS * 2
        stdout_drainer = _StreamDrainer(proc.stdout, buf_cap)
        stderr_drainer = _StreamDrainer(proc.stderr, buf_cap)

        ready, reason, exited_early = _wait_for_url_or_exit(
            url, readiness_timeout_seconds, proc
        )
        if not ready:
            stdout_text = stdout_drainer.snapshot()
            stderr_text = stderr_drainer.snapshot()
            if exited_early or proc.poll() is not None:
                extra = f"preview server exited before {url} was reachable (exit {proc.returncode})"
            else:
                extra = (
                    f"preview url {url} did not become reachable within "
                    f"{readiness_timeout_seconds}s (last probe: {reason})"
                )
            _terminate_dev_server(proc, DEFAULT_TERMINATE_GRACE_SECONDS)
            return {
                "ok": False,
                "running": False,
                "project_id": project_id,
                "url": None,
                "error": extra,
                "output_preview": _build_output_preview(stdout_text, stderr_text, extra),
            }

        server = PreviewServer(
            project_id=project_id,
            command=command,
            url=url,
            proc=proc,
            stdout_drainer=stdout_drainer,
            stderr_drainer=stderr_drainer,
            started_at=_now_iso(),
        )
        with _lock:
            _registry[project_id] = server
        return {"ok": True, **_status_dict(project_id, server)}
    finally:
        with _lock:
            _starting.discard(project_id)


def stop_preview(project_id: str) -> dict:
    """Stop the managed preview server for ``project_id``, if any. Never raises."""
    with _lock:
        server = _registry.pop(project_id, None)
    if server is None:
        return {"ok": True, "running": False, "project_id": project_id, "url": None, "stopped": False}
    try:
        if _is_alive(server):
            _terminate_dev_server(server.proc, DEFAULT_TERMINATE_GRACE_SECONDS)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to terminate preview for %s: %s", project_id, exc)
    return {"ok": True, "running": False, "project_id": project_id, "url": None, "stopped": True}


def shutdown_all_previews() -> None:
    """Tear down every managed preview server. Called on backend shutdown."""
    with _lock:
        servers = list(_registry.values())
        _registry.clear()
        _starting.clear()
    for server in servers:
        try:
            if _is_alive(server):
                _terminate_dev_server(server.proc, DEFAULT_TERMINATE_GRACE_SECONDS)
        except Exception:  # noqa: BLE001
            pass
