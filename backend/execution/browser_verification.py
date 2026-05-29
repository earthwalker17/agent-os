"""Task 06.2B — opt-in browser-based verification for Coding Agent runs.

After the command verification step finishes, this module optionally
spins up a project-defined dev server, waits for a configured URL to
become reachable, drives a headless browser to that URL, captures a
single screenshot, and tears the server back down.

Design constraints (mirror 06.2A's posture):

  - **Opt-in.** No ``## Browser Verification`` block in ``TASK.md`` =>
    ``BrowserVerificationResult(enabled=False, status='skipped')``. We do
    NOT auto-spin a server for backend-only projects.
  - **Sandbox-respecting.** The dev server command is validated through
    ``ProjectSandbox.validate_command``; the subprocess is launched with
    ``cwd=`` the project's ``repo/`` directory. Unsafe commands are
    recorded as ``failed`` with the sandbox reason in ``output_preview``.
  - **Bounded lifecycle.** Start -> wait-for-readiness -> screenshot ->
    terminate. We never leave a zombie. Termination uses ``terminate()``
    first, then ``kill()`` after a short grace period, and on Unix-y
    platforms we put the child in its own process group so we can also
    take down any sub-processes it spawned (typical for ``npm run dev``).
  - **Bounded output.** ``output_preview`` is capped to a few KB so it
    stays safe to round-trip into ``run.json`` and ``result.md``.
  - **Best-effort.** Any exception inside this module is converted into a
    ``BrowserVerificationResult(status='failed', ...)`` — browser
    verification must never crash background finalization.
  - **No browser engine assumption.** Playwright is loaded lazily; if
    the import fails the module records a clean ``failed`` result
    instead of crashing.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .manager import read_task_state
from .models import BrowserVerificationResult
from .sandbox import ProjectSandbox, SandboxViolation


log = logging.getLogger(__name__)


# Same posture as command verification — small preview, never the full
# server log.
_BROWSER_OUTPUT_PREVIEW_CHARS = 4000

# Default lifecycle timeouts. The whole pipeline is meant to be a
# lightweight smoke check, not a full browser test suite.
DEFAULT_READINESS_TIMEOUT_SECONDS = 30
DEFAULT_SCREENSHOT_TIMEOUT_SECONDS = 20
DEFAULT_TERMINATE_GRACE_SECONDS = 5
READINESS_POLL_INTERVAL_SECONDS = 0.5


# Match the heading row, then capture everything until the next top-level
# heading (or end of file). Same shape as 06.2A's verification parser.
_BROWSER_SECTION_REGEX = re.compile(
    r"^##\s+Browser\s+Verification\s*$(?P<body>.*?)(?=^##\s+|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

# A single fenced block inside the browser-verification section.
_BASH_FENCE_REGEX = re.compile(
    r"```(?:bash|sh|shell|console)?\s*\n(?P<body>.*?)```",
    re.DOTALL | re.IGNORECASE,
)

# ``url: http://127.0.0.1:5173`` or ``URL=http://...``.
_URL_LINE_REGEX = re.compile(
    r"^\s*url\s*[:=]\s*(?P<url>\S+)\s*$",
    re.IGNORECASE,
)


@dataclass
class BrowserVerificationConfig:
    """Parsed ``## Browser Verification`` block."""

    command: str
    url: str


# ---------- parser ----------


def parse_browser_verification(task_md_text: str) -> Optional[BrowserVerificationConfig]:
    """Extract dev-server command + URL from TASK.md.

    Returns ``None`` when:
      - no ``## Browser Verification`` heading,
      - no fenced block (or no recognizable body),
      - no command line,
      - no ``url:`` line.

    Otherwise returns the first non-empty, non-comment command line plus
    the first ``url:`` declaration.
    """
    if not task_md_text:
        return None
    section_match = _BROWSER_SECTION_REGEX.search(task_md_text)
    if section_match is None:
        return None
    section_body = section_match.group("body")
    if not section_body:
        return None

    fence_match = _BASH_FENCE_REGEX.search(section_body)
    candidate_lines = (
        fence_match.group("body").splitlines()
        if fence_match is not None
        else section_body.splitlines()
    )

    command: Optional[str] = None
    url: Optional[str] = None

    for raw_line in candidate_lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        url_match = _URL_LINE_REGEX.match(line)
        if url_match is not None:
            if url is None:
                url = url_match.group("url").strip()
            continue
        # First non-comment, non-url line is the dev-server command.
        if command is None:
            command = line

    if not command or not url:
        return None
    return BrowserVerificationConfig(command=command, url=url)


# ---------- helpers ----------


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated, +{len(text) - limit} chars]"


class _StreamDrainer:
    """Continuously drain a child stream into a bounded in-memory buffer.

    A daemon thread loops on ``readline()`` so the OS pipe buffer can never
    fill up. This is the difference between ``npm run dev`` actually
    starting on Windows and silently deadlocking: the default Windows pipe
    buffer is small enough (~4-8 KB) that Vite's startup output (dep
    pre-bundling, deprecation warnings) fills it before Vite binds its
    listening socket. With ``stdout`` blocked, Vite blocks too, never
    ``listen()``s, and our URL polling times out at 30s with no clue why.

    The buffer is capped at ``max_chars``; once full, additional output
    is silently dropped so memory and ``run.json`` stay bounded.
    """

    def __init__(self, stream, max_chars: int) -> None:
        self._stream = stream
        self._max_chars = max_chars
        self._chunks: list[str] = []
        self._len = 0
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        if stream is not None:
            self._thread = threading.Thread(
                target=self._run,
                name="browser-verification-drain",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                line = self._stream.readline()
                if not line:
                    break
                with self._lock:
                    if self._len >= self._max_chars:
                        continue
                    room = self._max_chars - self._len
                    chunk = line if len(line) <= room else line[:room]
                    self._chunks.append(chunk)
                    self._len += len(chunk)
        except Exception:  # noqa: BLE001
            # Never let drainer errors crash the verification pipeline.
            pass

    def snapshot(self) -> str:
        # Give the reader a brief moment to flush anything it just saw
        # before we snapshot — important when the dev server died or we
        # just terminated it, since the pipe may still have queued bytes.
        if self._thread is not None:
            self._thread.join(timeout=0.2)
        with self._lock:
            return "".join(self._chunks)


def _build_output_preview(stdout: str, stderr: str, extra: str = "") -> str:
    parts: list[str] = []
    if stdout:
        parts.append("--- stdout ---\n" + stdout.strip())
    if stderr:
        parts.append("--- stderr ---\n" + stderr.strip())
    if extra:
        parts.append(extra.strip())
    combined = "\n\n".join(p for p in parts if p).strip()
    return _truncate(combined, _BROWSER_OUTPUT_PREVIEW_CHARS)


def _wait_for_url(url: str, timeout_seconds: int) -> tuple[bool, str]:
    """Poll ``url`` until it returns any HTTP response or timeout.

    We don't care about the status code — any response means the server
    accepted the connection, which is the only thing this readiness gate
    is checking. Any 4xx/5xx page is still a page the screenshot can
    capture.

    Returns ``(ready, reason)`` where ``reason`` is empty on success and
    a short human-readable string on timeout.
    """
    deadline = time.monotonic() + max(1, timeout_seconds)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2) as _:
                return True, ""
        except urllib.error.HTTPError:
            # Any HTTP response means the socket accepted the request.
            return True, ""
        except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(READINESS_POLL_INTERVAL_SECONDS)
    return False, last_error or "no response before timeout"


def _wait_for_url_or_exit(
    url: str,
    timeout_seconds: int,
    proc: Optional[subprocess.Popen],
) -> tuple[bool, str, bool]:
    """Wait for ``url`` while also fast-failing on dev-server exit.

    Calls :func:`_wait_for_url` (the patchable surface used by tests) in
    short windows so we can re-check ``proc.poll()`` between probes. This
    avoids burning the full readiness timeout on a server that already
    died (port conflict, syntax error, missing dep).

    Returns ``(ready, reason, exited_early)``.
    """
    deadline = time.monotonic() + max(1, timeout_seconds)
    last_reason = ""
    # Each inner attempt window is short so we re-check proc.poll() often
    # enough to fail fast on early exits, but long enough that on success
    # we don't busy-loop.
    attempt_window = 3
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return (
                False,
                f"dev server exited with code {proc.returncode}",
                True,
            )
        remaining = max(1, int(deadline - time.monotonic()))
        window = min(remaining, attempt_window)
        ready, reason = _wait_for_url(url, window)
        if ready:
            return True, "", False
        if reason:
            last_reason = reason
        # Guard against a stubbed _wait_for_url that returns instantly —
        # without this we'd spin until the deadline.
        time.sleep(0.05)
    return False, last_reason or "no response before timeout", False


def _start_dev_server(command: str, cwd: Path) -> subprocess.Popen:
    """Start the dev server with ``cwd`` set to the project's repo dir.

    On POSIX we put the child in its own process group so termination can
    reach any sub-processes (``npm run dev`` typically forks). On Windows
    we use ``CREATE_NEW_PROCESS_GROUP`` to enable a clean ``CTRL_BREAK``
    teardown if needed.
    """
    kwargs: dict = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "nt":
        # New process group on Windows so children can be terminated as a
        # group; shell=True is needed for shell-style command strings.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        return subprocess.Popen(command, shell=True, **kwargs)
    # POSIX: detach into its own process group via setsid.
    kwargs["preexec_fn"] = os.setsid  # type: ignore[assignment]
    return subprocess.Popen(command, shell=True, **kwargs)


def _terminate_dev_server(proc: subprocess.Popen, grace_seconds: int) -> None:
    """Tear the dev server down. Never raises."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                proc.terminate()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                proc.terminate()
        try:
            proc.wait(timeout=max(1, grace_seconds))
            return
        except subprocess.TimeoutExpired:
            pass
        # Escalate.
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        # Absolutely never let cleanup raise out of here.
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


# ---------- screenshot runner ----------


# Signature: (url, output_path, timeout_seconds) -> None.
# Raises on failure. Returns nothing on success. The default
# implementation uses Playwright; tests can swap in a stub.
BrowserScreenshotRunner = Callable[[str, Path, int], None]


def _format_exception(exc: BaseException) -> str:
    """Render an exception for human display.

    ``str(exc)`` is empty for many built-in exceptions raised with no
    argument — most notably ``NotImplementedError()`` raised by asyncio's
    ``SelectorEventLoop`` subprocess methods on Windows. Falling back to
    ``repr(exc)`` keeps the preview informative instead of showing
    ``NotImplementedError:`` followed by nothing.
    """
    msg = str(exc).strip()
    if msg:
        return f"{type(exc).__name__}: {msg}"
    return repr(exc)


# Inline script run in a dedicated Python subprocess so Playwright always
# executes on the main thread of a fresh interpreter. Without this,
# Playwright's sync API — invoked from a ``BackgroundRunManager`` worker
# thread — trips Windows' asyncio limitation where ``SelectorEventLoop``
# subprocess methods raise a messageless ``NotImplementedError()``, which
# is exactly the failure we observed before this change.
#
# Communication contract:
#   args via ``sys.argv[1]`` as a JSON dict: ``{url, output_path, timeout_ms}``
#   exit 0 = success (file written)
#   exit 2 = playwright module not importable
#   exit 3 = chromium binary not installed
#   exit 4 = screenshot capture failed for any other reason
# stderr always carries a JSON ``{"error": "<tag>", "message": "..."}`` line
# so the caller can translate exit codes into clean error messages.
_PLAYWRIGHT_SCREENSHOT_SCRIPT = r"""
import json
import sys

args = json.loads(sys.argv[1])
url = args["url"]
output_path = args["output_path"]
timeout_ms = int(args["timeout_ms"])

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:
    sys.stderr.write(json.dumps({
        "error": "playwright_not_installed",
        "message": f"{type(exc).__name__}: {exc}",
    }))
    sys.exit(2)

try:
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:
            text = str(exc) or repr(exc)
            tag = "capture_failed"
            if (
                "Executable doesn't exist" in text
                or "playwright install" in text
                or "BrowserType.launch" in text and "chromium" in text.lower()
            ):
                tag = "chromium_not_installed"
            sys.stderr.write(json.dumps({"error": tag, "message": text}))
            sys.exit(3 if tag == "chromium_not_installed" else 4)
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(url, timeout=timeout_ms)
            page.screenshot(path=output_path, full_page=False)
        finally:
            browser.close()
except SystemExit:
    raise
except Exception as exc:
    text = str(exc) or repr(exc)
    sys.stderr.write(json.dumps({
        "error": "capture_failed",
        "message": f"{type(exc).__name__}: {text}",
    }))
    sys.exit(4)
"""


def _default_playwright_screenshot(url: str, output_path: Path, timeout_seconds: int) -> None:
    """Capture ``url`` to ``output_path`` via a Playwright subprocess.

    Running Playwright in a subprocess (rather than calling
    ``sync_playwright()`` directly) gives it a clean main-thread Python
    interpreter with the platform-default asyncio policy. This is the only
    reliable way to call Playwright's sync API from a
    ``BackgroundRunManager`` worker thread on Windows — calling it inline
    in the worker thread reliably surfaces a messageless
    ``NotImplementedError()`` from Windows ``SelectorEventLoop``'s missing
    subprocess support, with no diagnostic for the operator.
    """
    import json as _json

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _json.dumps(
        {
            "url": url,
            "output_path": str(output_path),
            "timeout_ms": max(1, int(timeout_seconds)) * 1000,
        }
    )
    # Give Playwright a generous outer cap on top of its own timeout so a
    # truly hung subprocess can't pin the verification phase forever.
    outer_timeout = max(15, timeout_seconds + 30)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PLAYWRIGHT_SCREENSHOT_SCRIPT, payload],
            capture_output=True,
            text=True,
            timeout=outer_timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "could not spawn Python subprocess for Playwright screenshot: "
            f"{_format_exception(exc)}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Playwright screenshot subprocess timed out after {outer_timeout}s"
        ) from exc

    if completed.returncode == 0:
        return

    # Try to parse the structured error blob the script writes to stderr.
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    tag = ""
    message = ""
    try:
        payload_out = _json.loads(stderr)
        tag = str(payload_out.get("error") or "")
        message = str(payload_out.get("message") or "").strip()
    except Exception:  # noqa: BLE001
        message = stderr or stdout

    if tag == "playwright_not_installed" or completed.returncode == 2:
        raise RuntimeError(
            "Playwright is not installed in the backend environment. "
            "Run: pip install playwright && python -m playwright install chromium"
            + (f"\n\nDetails: {message}" if message else "")
        )
    if tag == "chromium_not_installed" or completed.returncode == 3:
        raise RuntimeError(
            "Playwright/Chromium is not available in the backend environment. "
            "Run: python -m playwright install chromium"
            + (f"\n\nDetails: {message}" if message else "")
        )
    raise RuntimeError(
        f"Playwright screenshot failed (exit {completed.returncode})"
        + (f": {message}" if message else "")
    )


# ---------- public entry point ----------


def run_browser_verification(
    project_id: str,
    *,
    run_dir: Path,
    task_md_override: Optional[str] = None,
    readiness_timeout_seconds: int = DEFAULT_READINESS_TIMEOUT_SECONDS,
    screenshot_timeout_seconds: int = DEFAULT_SCREENSHOT_TIMEOUT_SECONDS,
    terminate_grace_seconds: int = DEFAULT_TERMINATE_GRACE_SECONDS,
    screenshot_runner: Optional[BrowserScreenshotRunner] = None,
    process_starter: Optional[Callable[[str, Path], subprocess.Popen]] = None,
) -> BrowserVerificationResult:
    """Run the project's configured browser verification, if any.

    ``run_dir`` is the run's artifact directory (e.g.
    ``execution_workspaces/{pid}/runs/{rid}/``); screenshots are written
    under ``run_dir / 'screenshots/'``. Never raises.

    ``screenshot_runner`` and ``process_starter`` are seams for tests
    so the lifecycle can be exercised without Playwright or a real
    dev server.
    """
    start = time.perf_counter()
    try:
        task_md = (
            task_md_override
            if task_md_override is not None
            else (read_task_state(project_id) or "")
        )
        config = parse_browser_verification(task_md)
        if config is None:
            return BrowserVerificationResult(
                enabled=False,
                command=None,
                url=None,
                status="skipped",
                screenshot_path=None,
                output_preview="",
                duration_ms=None,
            )

        sandbox = ProjectSandbox(project_id)
        try:
            sandbox.validate_command(config.command)
        except SandboxViolation as exc:
            return BrowserVerificationResult(
                enabled=True,
                command=config.command,
                url=config.url,
                status="failed",
                screenshot_path=None,
                output_preview=_truncate(
                    f"sandbox rejected dev server command: {exc}",
                    _BROWSER_OUTPUT_PREVIEW_CHARS,
                ),
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        repo_dir = sandbox.repo_dir
        if not repo_dir.exists() or not repo_dir.is_dir():
            return BrowserVerificationResult(
                enabled=True,
                command=config.command,
                url=config.url,
                status="failed",
                screenshot_path=None,
                output_preview=f"repo dir does not exist: {repo_dir}",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )

        starter = process_starter or _start_dev_server
        runner = screenshot_runner or _default_playwright_screenshot

        proc: Optional[subprocess.Popen] = None
        stdout_drainer: Optional[_StreamDrainer] = None
        stderr_drainer: Optional[_StreamDrainer] = None
        extra_msg = ""
        screenshot_rel_path: Optional[str] = None
        status = "failed"
        try:
            try:
                proc = starter(config.command, repo_dir)
            except Exception as exc:  # noqa: BLE001
                return BrowserVerificationResult(
                    enabled=True,
                    command=config.command,
                    url=config.url,
                    status="failed",
                    screenshot_path=None,
                    output_preview=_truncate(
                        f"failed to start dev server: {_format_exception(exc)}",
                        _BROWSER_OUTPUT_PREVIEW_CHARS,
                    ),
                    duration_ms=int((time.perf_counter() - start) * 1000),
                )

            # Start draining stdout/stderr immediately. Without this, the
            # OS pipe buffer fills up during dev-server startup (vite dep
            # pre-bundling is enough to do it on Windows) and the child
            # deadlocks before it can ``listen()`` on its port.
            buf_cap = _BROWSER_OUTPUT_PREVIEW_CHARS * 2
            stdout_drainer = _StreamDrainer(proc.stdout, buf_cap)
            stderr_drainer = _StreamDrainer(proc.stderr, buf_cap)

            ready, reason, exited_early = _wait_for_url_or_exit(
                config.url, readiness_timeout_seconds, proc
            )
            if not ready:
                stdout_text = stdout_drainer.snapshot()
                stderr_text = stderr_drainer.snapshot()
                if exited_early or proc.poll() is not None:
                    extra_msg = (
                        f"dev server exited before url was reachable "
                        f"(exit {proc.returncode}); url={config.url}"
                    )
                else:
                    extra_msg = (
                        f"url {config.url} did not become reachable within "
                        f"{readiness_timeout_seconds}s "
                        f"(dev server still running; last probe: {reason})"
                    )
                return BrowserVerificationResult(
                    enabled=True,
                    command=config.command,
                    url=config.url,
                    status="failed",
                    screenshot_path=None,
                    output_preview=_build_output_preview(
                        stdout_text, stderr_text, extra_msg
                    ),
                    duration_ms=int((time.perf_counter() - start) * 1000),
                )

            screenshots_dir = run_dir / "screenshots"
            screenshot_abs_path = screenshots_dir / "browser.png"
            try:
                runner(config.url, screenshot_abs_path, screenshot_timeout_seconds)
            except Exception as exc:  # noqa: BLE001
                stdout_text = stdout_drainer.snapshot()
                stderr_text = stderr_drainer.snapshot()
                extra_msg = (
                    f"screenshot capture failed: {_format_exception(exc)}"
                )
                return BrowserVerificationResult(
                    enabled=True,
                    command=config.command,
                    url=config.url,
                    status="failed",
                    screenshot_path=None,
                    output_preview=_build_output_preview(
                        stdout_text, stderr_text, extra_msg
                    ),
                    duration_ms=int((time.perf_counter() - start) * 1000),
                )

            if not screenshot_abs_path.exists():
                stdout_text = stdout_drainer.snapshot()
                stderr_text = stderr_drainer.snapshot()
                extra_msg = "screenshot runner returned but file is missing"
                return BrowserVerificationResult(
                    enabled=True,
                    command=config.command,
                    url=config.url,
                    status="failed",
                    screenshot_path=None,
                    output_preview=_build_output_preview(
                        stdout_text, stderr_text, extra_msg
                    ),
                    duration_ms=int((time.perf_counter() - start) * 1000),
                )

            screenshot_rel_path = "screenshots/browser.png"
            status = "passed"
            extra_msg = f"screenshot captured at {screenshot_rel_path}"
            return BrowserVerificationResult(
                enabled=True,
                command=config.command,
                url=config.url,
                status=status,
                screenshot_path=screenshot_rel_path,
                output_preview=_build_output_preview("", "", extra_msg),
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        finally:
            # Always tear the server down, even if we hit an unexpected
            # exception path. Never leave a zombie.
            if proc is not None and proc.poll() is None:
                _terminate_dev_server(proc, terminate_grace_seconds)
    except Exception as exc:  # noqa: BLE001
        log.exception("Browser verification crashed for project %s", project_id)
        return BrowserVerificationResult(
            enabled=True,
            command=None,
            url=None,
            status="failed",
            screenshot_path=None,
            output_preview=f"browser verification crashed: {_format_exception(exc)}",
            duration_ms=int((time.perf_counter() - start) * 1000),
        )


def render_browser_verification_section(
    result: Optional[BrowserVerificationResult],
) -> str:
    """Render a Markdown ``## Browser Verification`` block for ``result.md``.

    Always emits the section so reviewers can tell browser verification
    was considered — even when it was skipped.
    """
    if result is None:
        return "## Browser Verification\n_(not run)_\n"

    if not result.enabled:
        return (
            "## Browser Verification\n"
            "status: **skipped** _(no browser verification configured)_\n"
        )

    lines: list[str] = ["## Browser Verification"]
    lines.append(f"- **Status**: {result.status}")
    if result.command:
        lines.append(f"- **Command**: `{result.command}`")
    if result.url:
        lines.append(f"- **URL**: {result.url}")
    if result.screenshot_path:
        lines.append(f"- **Screenshot**: `{result.screenshot_path}`")
    if result.duration_ms is not None:
        lines.append(f"- **Duration**: {result.duration_ms} ms")
    if result.output_preview:
        lines.append("")
        lines.append("```")
        lines.append(result.output_preview)
        lines.append("```")
    return "\n".join(lines) + "\n"
