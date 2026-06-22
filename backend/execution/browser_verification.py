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

import json
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
from .models import (
    BrowserPageCapture,
    BrowserVerificationResult,
    RunRecord,
    RunStatus,
)
from .sandbox import ProjectSandbox, SandboxViolation


log = logging.getLogger(__name__)


# Same posture as command verification — small preview, never the full
# server log.
_BROWSER_OUTPUT_PREVIEW_CHARS = 4000

# Default lifecycle timeouts. The whole pipeline is meant to be a
# lightweight smoke check, not a full browser test suite.
# Bumped 30 -> 60: a moderately complex app's FIRST `npm run dev` does esbuild
# dep pre-bundling, and on Windows real-time AV scanning of node_modules can push
# the first HTTP bind past 30 s on a cold boot. The early-exit-on-process-death
# guard in _wait_for_url_or_exit still fails fast if the server actually dies, so
# a larger ceiling only costs wall-clock on a genuinely slow-but-alive boot.
DEFAULT_READINESS_TIMEOUT_SECONDS = 60
DEFAULT_SCREENSHOT_TIMEOUT_SECONDS = 20
DEFAULT_TERMINATE_GRACE_SECONDS = 5
READINESS_POLL_INTERVAL_SECONDS = 0.5

# Multi-page capture (readiness + multi-view upgrade). The capture pipeline
# screenshots the entry URL plus a small, bounded number of discovered
# navigation targets (tabs / route links / nav buttons). Kept conservative and
# local-first — this is a smoke review, not a full crawl.
MAX_BROWSER_PAGES = 4
# Per-page in-browser readiness budget: how long the capture script waits for a
# page to actually RENDER (DOM populated, loading indicators gone, content
# settled) before screenshotting. Separate from DEFAULT_READINESS_TIMEOUT_SECONDS,
# which is the HTTP-reachability gate for the dev server. On timeout the page is
# captured anyway and marked ``readiness="unconfirmed"`` (a slow-but-alive app is
# never failed outright; the AI visual judgment is the second line of defense
# against a spinner-only capture).
DEFAULT_PAGE_READINESS_TIMEOUT_SECONDS = 12

# Task 06.2C — defaults for the user-triggered browser verification flow.
# Agent OS's own frontend runs on 5173, so the verified app must use a
# different port to avoid a conflict. These are used only when the project
# has no ``## Browser Verification`` block in TASK.md (advanced users can
# still override via that block — see ``run_ui_browser_verification``).
DEFAULT_DEV_HOST = "127.0.0.1"
DEFAULT_DEV_PORT = 5174
# --strictPort makes Vite fail loudly if 5174 is taken instead of silently
# bumping to 5175 — which would desync from the hardwired DEFAULT_DEV_URL and
# either screenshot a stale/foreign server or hang the readiness gate.
DEFAULT_DEV_COMMAND = (
    f"npm run dev -- --host {DEFAULT_DEV_HOST} --port {DEFAULT_DEV_PORT} --strictPort"
)
DEFAULT_DEV_URL = f"http://{DEFAULT_DEV_HOST}:{DEFAULT_DEV_PORT}"
# Frontend dependency install runs before the dev server starts. npm install
# on a cold cache can take a while, so the cap is generous compared to the
# readiness/screenshot timeouts.
DEFAULT_INSTALL_COMMAND = "npm install"
# Cold install ceiling. A cold Vite+React(+Tailwind/Express) install on Windows
# with real-time AV scanning can exceed 300 s and time out — reporting a
# spurious "install failed" on a fresh tree (the runner's *inferred* install was
# already bumped to 600 s for this reason). Match it here so the UI-triggered
# flow is just as resilient on a cold cache. Warm installs return in seconds.
DEFAULT_INSTALL_TIMEOUT_SECONDS = 600


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
        # Explicit UTF-8 so the _StreamDrainer's readline() can't raise
        # UnicodeDecodeError on Vite's box-drawing/emoji output under a non-UTF-8
        # machine codec (cp936/cp1252). A drainer crash would silently stop
        # draining the pipe and re-introduce the Windows startup deadlock this
        # whole mechanism exists to prevent.
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        # New process group on Windows so children can be terminated as a
        # group; shell=True is needed for shell-style command strings.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        return subprocess.Popen(command, shell=True, **kwargs)
    # POSIX: detach into its own process group via setsid.
    kwargs["preexec_fn"] = os.setsid  # type: ignore[assignment]
    return subprocess.Popen(command, shell=True, **kwargs)


def _taskkill_tree(proc: subprocess.Popen) -> None:
    """Windows-only: reap a process AND its descendants via ``taskkill /T``.

    With ``shell=True`` the Popen handle is cmd.exe; ``proc.kill()`` terminates
    only that shell, orphaning the ``npm.cmd`` -> node/Vite children that keep
    the dev port (5174) bound. ``taskkill /F /T`` walks the whole tree by pid.
    Falls back to ``proc.kill()`` if taskkill is unavailable. Never raises.
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


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
        # Escalate. On Windows a plain proc.kill() only terminates the cmd.exe
        # shell and orphans the node/Vite child (leaking port 5174); taskkill /T
        # reaps the whole tree.
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                _taskkill_tree(proc)
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


def _raise_for_playwright_exit(returncode: int, stdout: str, stderr: str) -> None:
    """Translate a non-zero Playwright subprocess exit into an actionable error.

    Shared by the single-screenshot primitive and the multi-page capture
    pipeline so both surface the same operator guidance. Parses the structured
    ``{"error": ..., "message": ...}`` blob the inline scripts write to stderr
    and maps the documented exit codes (2 = playwright missing, 3 = chromium
    missing) to install instructions; anything else becomes a generic failure.
    """
    stderr = (stderr or "").strip()
    stdout = (stdout or "").strip()
    tag = ""
    message = ""
    try:
        payload_out = json.loads(stderr)
        tag = str(payload_out.get("error") or "")
        message = str(payload_out.get("message") or "").strip()
    except Exception:  # noqa: BLE001
        message = stderr or stdout

    if tag == "playwright_not_installed" or returncode == 2:
        raise RuntimeError(
            "Playwright is not installed in the backend environment. "
            "Run: pip install playwright && python -m playwright install chromium"
            + (f"\n\nDetails: {message}" if message else "")
        )
    if tag == "chromium_not_installed" or returncode == 3:
        raise RuntimeError(
            "Playwright/Chromium is not available in the backend environment. "
            "Run: python -m playwright install chromium"
            + (f"\n\nDetails: {message}" if message else "")
        )
    raise RuntimeError(
        f"Playwright screenshot failed (exit {returncode})"
        + (f": {message}" if message else "")
    )


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
            encoding="utf-8",
            errors="replace",
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

    # Translate the documented exit codes into actionable operator guidance.
    _raise_for_playwright_exit(
        completed.returncode, completed.stdout or "", completed.stderr or ""
    )


# ---------- multi-page readiness-gated capture ----------


# Signature: (url, screenshots_dir, *, max_pages, readiness_timeout_seconds,
#             screenshot_timeout_seconds) -> list[BrowserPageCapture].
# Captures the entry URL plus a bounded set of discovered navigation targets,
# each only after the page has actually rendered. Raises on failure; tests can
# swap in a stub. The default implementation drives Playwright.
PageCaptureRunner = Callable[..., "list[BrowserPageCapture]"]


# Marker that delimits the JSON manifest on the capture subprocess's stdout, so
# the parent can find it even if Playwright/Chromium prints stray warnings.
_CAPTURE_MANIFEST_MARKER = "__AGENTOS_CAPTURE_MANIFEST__"


# Inline script run in a dedicated Python subprocess (same Windows
# SelectorEventLoop workaround as the single-screenshot primitive). For each
# page it (1) navigates, (2) waits for a *rendered* state — DOM populated,
# loading indicators gone, body text settled — rather than just the load event,
# then (3) screenshots. After the entry page it discovers a few same-origin
# navigation targets (tabs / route links / nav buttons) and captures each.
#
# Communication contract:
#   args via sys.argv[1] as JSON: {url, screenshots_dir, max_pages,
#       readiness_timeout_ms, nav_timeout_ms, primary_name}
#   exit 0 = success; stdout carries  <marker><json manifest list>
#   exit 2 = playwright not importable
#   exit 3 = chromium not installed
#   exit 4 = capture failed
# stderr carries a JSON {"error","message"} blob on the error exits.
_PLAYWRIGHT_CAPTURE_SCRIPT = (
    r'''
import json
import os
import sys
import time

_MARKER = "''' + _CAPTURE_MANIFEST_MARKER + r'''"

args = json.loads(sys.argv[1])
url = args["url"]
screenshots_dir = args["screenshots_dir"]
max_pages = max(1, int(args["max_pages"]))
readiness_timeout_ms = int(args["readiness_timeout_ms"])
nav_timeout_ms = int(args["nav_timeout_ms"])
primary_name = args.get("primary_name", "browser.png")

os.makedirs(screenshots_dir, exist_ok=True)

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:
    sys.stderr.write(json.dumps({
        "error": "playwright_not_installed",
        "message": f"{type(exc).__name__}: {exc}",
    }))
    sys.exit(2)

READINESS_JS = """
() => {
  const body = document.body;
  const bodyText = (body && body.innerText || "").trim();
  const root = document.querySelector("#root, #app, main, [data-reactroot]") || body;
  const rootChildren = root ? root.querySelectorAll("*").length : 0;
  const sel = '[class*="spinner" i],[class*="loading" i],[class*="skeleton" i],[aria-busy="true"]';
  let visibleLoaders = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none" && cs.opacity !== "0") visibleLoaders++;
  }
  const lower = bodyText.toLowerCase();
  const loadingOnly = bodyText.length > 0 && bodyText.length <= 40 && (lower.indexOf("loading") === 0 || lower.indexOf("please wait") >= 0);
  return {textLen: bodyText.length, rootChildren: rootChildren, visibleLoaders: visibleLoaders, loadingOnly: loadingOnly, title: document.title || ""};
}
"""

NAV_DISCOVER_JS = """
(maxTargets) => {
  const out = [];
  const seen = new Set();
  const norm = (s) => (s || "").replace(/\\s+/g, " ").trim().slice(0, 60);
  const push = (kind, label, href) => {
    label = norm(label);
    if (!label) return;
    const key = label.toLowerCase();
    if (seen.has(key)) return;
    if (out.length >= maxTargets) return;
    seen.add(key);
    out.push({kind: kind, label: label, href: href || ""});
  };
  for (const el of document.querySelectorAll('[role="tab"]')) push("tab", el.innerText || el.getAttribute("aria-label"), "");
  for (const a of document.querySelectorAll('nav a[href], header a[href], a[href^="/"], a[href^="#/"], a[href^="./"]')) {
    let abs;
    try { abs = new URL(a.getAttribute("href"), location.href); } catch (e) { continue; }
    if (abs.origin !== location.origin) continue;
    if (abs.href === location.href) continue;
    push("link", a.innerText || a.getAttribute("aria-label"), abs.href);
  }
  for (const b of document.querySelectorAll('nav button, header button, [class*="tab" i] button')) push("button", b.innerText || b.getAttribute("aria-label"), "");
  return out.slice(0, maxTargets);
}
"""

def wait_ready(page):
    try:
        page.wait_for_load_state("networkidle", timeout=min(readiness_timeout_ms, 5000))
    except Exception:
        pass
    deadline = time.monotonic() + readiness_timeout_ms / 1000.0
    last_len = -1
    stable_ready = 0
    title = ""
    while time.monotonic() < deadline:
        try:
            s = page.evaluate(READINESS_JS)
        except Exception:
            time.sleep(0.3)
            continue
        title = s.get("title") or title
        rendered = (
            (not s.get("loadingOnly"))
            and s.get("rootChildren", 0) >= 5
            and s.get("textLen", 0) >= 30
            and s.get("visibleLoaders", 0) == 0
        )
        cur = s.get("textLen", 0)
        stable = last_len >= 0 and abs(cur - last_len) <= max(5, int(0.02 * max(cur, 1)))
        last_len = cur
        if rendered and stable:
            stable_ready += 1
            if stable_ready >= 2:
                time.sleep(0.4)
                return "confirmed", title
        else:
            stable_ready = 0
        time.sleep(0.4)
    time.sleep(0.3)
    return "unconfirmed", title

manifest = []
try:
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:
            text = str(exc) or repr(exc)
            tag = "capture_failed"
            if ("Executable doesn't exist" in text or "playwright install" in text or ("BrowserType.launch" in text and "chromium" in text.lower())):
                tag = "chromium_not_installed"
            sys.stderr.write(json.dumps({"error": tag, "message": text}))
            sys.exit(3 if tag == "chromium_not_installed" else 4)
        try:
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
            readiness, title = wait_ready(page)
            page.screenshot(path=os.path.join(screenshots_dir, primary_name), full_page=False)
            manifest.append({"file": primary_name, "url": page.url, "label": (title or "Home"), "title": title, "readiness": readiness, "nav_kind": "primary"})
            try:
                targets = page.evaluate(NAV_DISCOVER_JS, max(0, max_pages - 1)) or []
            except Exception:
                targets = []
            idx = 1
            for t in targets:
                if len(manifest) >= max_pages:
                    break
                try:
                    if t.get("kind") == "link" and t.get("href"):
                        page.goto(t["href"], wait_until="domcontentloaded", timeout=nav_timeout_ms)
                    else:
                        label = t.get("label") or ""
                        clicked = False
                        for role in ("tab", "button", "link"):
                            try:
                                loc = page.get_by_role(role, name=label)
                                if loc.count() > 0:
                                    loc.first.click(timeout=4000)
                                    clicked = True
                                    break
                            except Exception:
                                continue
                        if not clicked:
                            try:
                                page.get_by_text(label, exact=False).first.click(timeout=4000)
                                clicked = True
                            except Exception:
                                clicked = False
                        if not clicked:
                            continue
                    readiness, title = wait_ready(page)
                    idx += 1
                    fname = "page-%02d.png" % idx
                    page.screenshot(path=os.path.join(screenshots_dir, fname), full_page=False)
                    manifest.append({"file": fname, "url": page.url, "label": (t.get("label") or title or fname), "title": title, "readiness": readiness, "nav_kind": t.get("kind") or "link"})
                except Exception:
                    continue
        finally:
            browser.close()
except SystemExit:
    raise
except Exception as exc:
    text = str(exc) or repr(exc)
    sys.stderr.write(json.dumps({"error": "capture_failed", "message": f"{type(exc).__name__}: {text}"}))
    sys.exit(4)

if not manifest:
    sys.stderr.write(json.dumps({"error": "capture_failed", "message": "no pages captured"}))
    sys.exit(4)

sys.stdout.write(_MARKER + json.dumps(manifest))
'''
)


def _default_playwright_capture(
    url: str,
    screenshots_dir: Path,
    *,
    max_pages: int = MAX_BROWSER_PAGES,
    readiness_timeout_seconds: int = DEFAULT_PAGE_READINESS_TIMEOUT_SECONDS,
    screenshot_timeout_seconds: int = DEFAULT_SCREENSHOT_TIMEOUT_SECONDS,
) -> list[BrowserPageCapture]:
    """Capture the entry URL + a few rendered navigation targets via Playwright.

    Runs in a fresh Python subprocess (Windows asyncio workaround). Returns one
    :class:`BrowserPageCapture` per captured page (primary first, keeping the
    ``screenshots/browser.png`` name). Raises ``RuntimeError`` on any failure so
    the caller records a clean ``failed`` browser verification.
    """
    screenshots_dir = Path(screenshots_dir)
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "url": url,
            "screenshots_dir": str(screenshots_dir),
            "max_pages": max(1, int(max_pages)),
            "readiness_timeout_ms": max(1, int(readiness_timeout_seconds)) * 1000,
            "nav_timeout_ms": max(1, int(screenshot_timeout_seconds)) * 1000,
            "primary_name": "browser.png",
        }
    )
    # Generous outer cap: readiness + nav budget per page, plus headroom, so a
    # hung subprocess can't pin the verification phase forever.
    pages = max(1, int(max_pages))
    outer_timeout = max(
        30, (readiness_timeout_seconds + screenshot_timeout_seconds) * pages + 30
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PLAYWRIGHT_CAPTURE_SCRIPT, payload],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=outer_timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "could not spawn Python subprocess for Playwright capture: "
            f"{_format_exception(exc)}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Playwright capture subprocess timed out after {outer_timeout}s"
        ) from exc

    if completed.returncode != 0:
        _raise_for_playwright_exit(
            completed.returncode, completed.stdout or "", completed.stderr or ""
        )

    out = completed.stdout or ""
    marker_at = out.rfind(_CAPTURE_MANIFEST_MARKER)
    if marker_at < 0:
        raise RuntimeError("Playwright capture produced no manifest")
    try:
        raw = json.loads(out[marker_at + len(_CAPTURE_MANIFEST_MARKER):].strip() or "[]")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not parse capture manifest: {_format_exception(exc)}")

    captures: list[BrowserPageCapture] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        fname = str(entry.get("file") or "").strip()
        if not fname:
            continue
        captures.append(
            BrowserPageCapture(
                path=f"screenshots/{fname}",
                url=str(entry.get("url") or ""),
                label=str(entry.get("label") or ""),
                title=str(entry.get("title") or ""),
                readiness=str(entry.get("readiness") or "unknown"),
                nav_kind=str(entry.get("nav_kind") or ""),
            )
        )
    if not captures:
        raise RuntimeError("Playwright capture returned no pages")
    return captures


def _legacy_single_capture_adapter(
    screenshot_runner: BrowserScreenshotRunner,
) -> PageCaptureRunner:
    """Wrap a legacy single-screenshot runner as a one-page capture runner.

    Lets callers (and the existing test suite) keep passing the simple
    ``(url, path, timeout) -> None`` ``screenshot_runner`` while the rest of the
    pipeline speaks the multi-page manifest shape. Produces exactly the legacy
    ``screenshots/browser.png`` so ``screenshot_path`` stays byte-compatible.
    """

    def capture(
        url: str,
        screenshots_dir: Path,
        *,
        max_pages: int = 1,
        readiness_timeout_seconds: int = 0,
        screenshot_timeout_seconds: int = DEFAULT_SCREENSHOT_TIMEOUT_SECONDS,
    ) -> list[BrowserPageCapture]:
        screenshots_dir = Path(screenshots_dir)
        primary = screenshots_dir / "browser.png"
        screenshot_runner(url, primary, screenshot_timeout_seconds)
        return [
            BrowserPageCapture(
                path="screenshots/browser.png",
                url=url,
                label="Home",
                readiness="unknown",
                nav_kind="primary",
            )
        ]

    return capture


# ---------- public entry point ----------


# Task 06.2D — optional keep-alive handoff. When supplied and the
# verification passes, the still-running dev server (plus its stdout/stderr
# drainers) is handed to this callback instead of being torn down, so the
# preview URL stays usable. Signature:
#   (proc, stdout_drainer, stderr_drainer, command, url) -> bool
# Returning True means the callback now owns the process (skip teardown);
# False (or any exception) falls back to the normal teardown.
KeepAliveRegistrar = Callable[
    [subprocess.Popen, Optional["_StreamDrainer"], Optional["_StreamDrainer"], str, str],
    bool,
]


def _core_browser_verification(
    project_id: str,
    config: BrowserVerificationConfig,
    *,
    run_dir: Path,
    start: float,
    readiness_timeout_seconds: int,
    screenshot_timeout_seconds: int,
    terminate_grace_seconds: int,
    screenshot_runner: Optional[BrowserScreenshotRunner],
    process_starter: Optional[Callable[[str, Path], subprocess.Popen]],
    keep_alive_registrar: Optional[KeepAliveRegistrar] = None,
    page_capture_runner: Optional[PageCaptureRunner] = None,
    max_pages: int = MAX_BROWSER_PAGES,
    page_readiness_timeout_seconds: int = DEFAULT_PAGE_READINESS_TIMEOUT_SECONDS,
) -> BrowserVerificationResult:
    """Shared dev-server -> readiness -> screenshot -> teardown lifecycle.

    Both the TASK.md-driven runner path (:func:`run_browser_verification`)
    and the user-triggered UI path (:func:`run_ui_browser_verification`)
    funnel through here so the lifecycle and its failure handling live in
    one place. ``start`` is the caller's ``time.perf_counter()`` reference
    so ``duration_ms`` reflects the full operation. Unexpected exceptions
    propagate to the caller, which is responsible for the outer
    crash-to-``failed`` guard.

    When ``keep_alive_registrar`` is supplied and the verification passes, the
    still-running dev server is handed off to that callback instead of being
    torn down (Task 06.2D persistent preview) — the ``finally`` teardown is
    skipped only when the callback confirms ownership.
    """
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
    # Resolve the page capturer. An explicit ``page_capture_runner`` wins; else a
    # legacy single-screenshot ``screenshot_runner`` (used by the existing test
    # suite) is wrapped to the one-page manifest shape; else the default
    # multi-page, readiness-gated Playwright capture runs.
    if page_capture_runner is not None:
        capturer = page_capture_runner
    elif screenshot_runner is not None:
        capturer = _legacy_single_capture_adapter(screenshot_runner)
    else:
        capturer = _default_playwright_capture

    proc: Optional[subprocess.Popen] = None
    stdout_drainer: Optional[_StreamDrainer] = None
    stderr_drainer: Optional[_StreamDrainer] = None
    extra_msg = ""
    screenshot_rel_path: Optional[str] = None
    status = "failed"
    kept_alive = False
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
        try:
            captures = capturer(
                config.url,
                screenshots_dir,
                max_pages=max_pages,
                readiness_timeout_seconds=page_readiness_timeout_seconds,
                screenshot_timeout_seconds=screenshot_timeout_seconds,
            )
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

        primary = captures[0] if captures else None
        primary_abs = (run_dir / primary.path) if primary is not None else None
        if primary is None or primary_abs is None or not primary_abs.exists():
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

        screenshot_rel_path = primary.path
        status = "passed"
        page_count = len(captures)
        extra_msg = (
            f"captured {page_count} page(s); primary at {screenshot_rel_path} "
            f"(readiness: {primary.readiness})"
        )
        # Task 06.2D — hand the still-running dev server off to the preview
        # layer instead of tearing it down, so the captured URL stays live.
        # Only the UI flow supplies a registrar; the runner path leaves it
        # ``None`` and the server is torn down as before.
        if keep_alive_registrar is not None and proc is not None:
            try:
                if keep_alive_registrar(
                    proc, stdout_drainer, stderr_drainer, config.command, config.url
                ):
                    kept_alive = True
            except Exception:  # noqa: BLE001
                kept_alive = False
        return BrowserVerificationResult(
            enabled=True,
            command=config.command,
            url=config.url,
            status=status,
            screenshot_path=screenshot_rel_path,
            output_preview=_build_output_preview("", "", extra_msg),
            duration_ms=int((time.perf_counter() - start) * 1000),
            pages=captures,
            readiness=primary.readiness,
        )
    finally:
        # Always tear the server down, even if we hit an unexpected
        # exception path — unless it was handed off for keep-alive preview.
        # Never leave a zombie.
        if not kept_alive and proc is not None and proc.poll() is None:
            _terminate_dev_server(proc, terminate_grace_seconds)


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
    page_capture_runner: Optional[PageCaptureRunner] = None,
    max_pages: int = MAX_BROWSER_PAGES,
) -> BrowserVerificationResult:
    """Run the project's configured browser verification, if any.

    ``run_dir`` is the run's artifact directory (e.g.
    ``execution_workspaces/{pid}/runs/{rid}/``); screenshots are written
    under ``run_dir / 'screenshots/'``. Captures the entry URL plus a few
    discovered navigation targets, each only after the page has rendered.
    Never raises.

    Skips (``enabled=False``) when TASK.md has no ``## Browser Verification``
    block — this is the post-run runner path, which must NOT auto-spin a
    server for backend-only projects. The user-triggered flow that falls
    back to a default dev command lives in
    :func:`run_ui_browser_verification`.

    ``screenshot_runner`` / ``page_capture_runner`` and ``process_starter``
    are seams for tests so the lifecycle can be exercised without Playwright
    or a real dev server.
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
        return _core_browser_verification(
            project_id,
            config,
            run_dir=run_dir,
            start=start,
            readiness_timeout_seconds=readiness_timeout_seconds,
            screenshot_timeout_seconds=screenshot_timeout_seconds,
            terminate_grace_seconds=terminate_grace_seconds,
            screenshot_runner=screenshot_runner,
            process_starter=process_starter,
            page_capture_runner=page_capture_runner,
            max_pages=max_pages,
        )
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


# ---------- dependency install (Task 06.2C) ----------


# Signature: (command, cwd) -> (exit_code, combined_output). Tests can swap
# in a stub so a real ``npm install`` never runs in the suite.
DependencyInstaller = Callable[[str, Path], "tuple[int, str]"]


def _default_dependency_installer(
    command: str, cwd: Path, timeout_seconds: int
) -> "tuple[int, str]":
    """Run a dependency-install command in ``cwd`` and capture its output.

    Returns ``(exit_code, combined_output)``. A timeout is reported as a
    non-zero exit with an explanatory line so the caller treats it as a
    failed install (and skips the screenshot) rather than crashing.
    """
    # Popen (not subprocess.run) + explicit UTF-8 so a timeout can reap the whole
    # npm/node tree on Windows (a half-finished install left writing into
    # node_modules corrupts the next install) and Vite/npm UTF-8 output decodes
    # cleanly under a non-UTF-8 machine codec.
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        return 1, f"dependency install could not start: {_format_exception(exc)}"
    try:
        out, err = proc.communicate(timeout=max(1, int(timeout_seconds)))
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            _taskkill_tree(proc)
        else:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        return 1, f"dependency install timed out after {timeout_seconds}s"
    except Exception as exc:  # noqa: BLE001
        return 1, f"dependency install crashed: {_format_exception(exc)}"
    return proc.returncode, _build_output_preview(out or "", err or "")


def run_ui_browser_verification(
    project_id: str,
    *,
    run_dir: Path,
    task_md_override: Optional[str] = None,
    install_timeout_seconds: int = DEFAULT_INSTALL_TIMEOUT_SECONDS,
    readiness_timeout_seconds: int = DEFAULT_READINESS_TIMEOUT_SECONDS,
    screenshot_timeout_seconds: int = DEFAULT_SCREENSHOT_TIMEOUT_SECONDS,
    terminate_grace_seconds: int = DEFAULT_TERMINATE_GRACE_SECONDS,
    screenshot_runner: Optional[BrowserScreenshotRunner] = None,
    process_starter: Optional[Callable[[str, Path], subprocess.Popen]] = None,
    dependency_installer: Optional[DependencyInstaller] = None,
    keep_alive_registrar: Optional[KeepAliveRegistrar] = None,
    page_capture_runner: Optional[PageCaptureRunner] = None,
    max_pages: int = MAX_BROWSER_PAGES,
) -> BrowserVerificationResult:
    """User-triggered browser verification (Task 06.2C). Never raises.

    Unlike :func:`run_browser_verification`, this works even when the
    project has no ``## Browser Verification`` block: it falls back to a
    default Vite dev command on port ``5174`` (Agent OS itself uses 5173).
    If a block IS present it is honored, so advanced users keep control of
    the command and URL.

    Before starting the dev server, if the repo contains ``package.json``
    the configured install command (``npm install``) is run first. A failed
    install short-circuits the flow: no dev server is started, no screenshot
    is captured, and the result records the install failure as a clear
    blocker. The dev-server command and the install command both go through
    ``ProjectSandbox.validate_command``.
    """
    start = time.perf_counter()
    try:
        task_md = (
            task_md_override
            if task_md_override is not None
            else (read_task_state(project_id) or "")
        )
        # Honor an explicit TASK.md block; otherwise fall back to the
        # default dev command + URL on the non-conflicting port.
        config = parse_browser_verification(task_md) or BrowserVerificationConfig(
            command=DEFAULT_DEV_COMMAND,
            url=DEFAULT_DEV_URL,
        )

        sandbox = ProjectSandbox(project_id)
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

        # ---- dependency install ----
        install_command: Optional[str] = None
        install_status: str = "skipped"
        install_output: str = ""
        if (repo_dir / "package.json").exists():
            install_command = DEFAULT_INSTALL_COMMAND
            try:
                sandbox.validate_command(install_command)
            except SandboxViolation as exc:
                return BrowserVerificationResult(
                    enabled=True,
                    command=config.command,
                    url=config.url,
                    status="failed",
                    screenshot_path=None,
                    output_preview=_truncate(
                        f"sandbox rejected install command: {exc}",
                        _BROWSER_OUTPUT_PREVIEW_CHARS,
                    ),
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    install_command=install_command,
                    install_status="failed",
                    install_output_preview=_truncate(
                        f"sandbox rejected install command: {exc}",
                        _BROWSER_OUTPUT_PREVIEW_CHARS,
                    ),
                )
            installer = dependency_installer or (
                lambda cmd, cwd: _default_dependency_installer(
                    cmd, cwd, install_timeout_seconds
                )
            )
            try:
                code, install_output = installer(install_command, repo_dir)
            except Exception as exc:  # noqa: BLE001
                code = 1
                install_output = (
                    f"dependency install crashed: {_format_exception(exc)}"
                )
            if code != 0:
                # Do NOT attempt to start the dev server / capture a
                # screenshot when deps failed to install.
                return BrowserVerificationResult(
                    enabled=True,
                    command=config.command,
                    url=config.url,
                    status="failed",
                    screenshot_path=None,
                    output_preview=_truncate(
                        f"dependency install failed (exit {code}); "
                        "skipping dev server start and screenshot capture.",
                        _BROWSER_OUTPUT_PREVIEW_CHARS,
                    ),
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    install_command=install_command,
                    install_status="failed",
                    install_output_preview=_truncate(
                        install_output, _BROWSER_OUTPUT_PREVIEW_CHARS
                    ),
                )
            install_status = "passed"

        # ---- dev server -> readiness -> multi-page capture -> teardown ----
        result = _core_browser_verification(
            project_id,
            config,
            run_dir=run_dir,
            start=start,
            readiness_timeout_seconds=readiness_timeout_seconds,
            screenshot_timeout_seconds=screenshot_timeout_seconds,
            terminate_grace_seconds=terminate_grace_seconds,
            screenshot_runner=screenshot_runner,
            process_starter=process_starter,
            keep_alive_registrar=keep_alive_registrar,
            page_capture_runner=page_capture_runner,
            max_pages=max_pages,
        )
        result.install_command = install_command
        result.install_status = install_status
        result.install_output_preview = _truncate(
            install_output, _BROWSER_OUTPUT_PREVIEW_CHARS
        )
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "UI browser verification crashed for project %s", project_id
        )
        return BrowserVerificationResult(
            enabled=True,
            command=None,
            url=None,
            status="failed",
            screenshot_path=None,
            output_preview=f"browser verification crashed: {_format_exception(exc)}",
            duration_ms=int((time.perf_counter() - start) * 1000),
        )


def apply_ui_browser_verification_to_record(
    record: RunRecord, result: BrowserVerificationResult
) -> RunRecord:
    """Fold a fresh UI-triggered browser verification into a run record.

    Mirrors 06.2B's status rule (a failing browser verification downgrades
    a ``completed`` run to ``partial`` and adds a blocker) and additionally
    supports retry: any prior ``browser verification failed:`` blocker is
    cleared before recomputing, and a now-passing verification restores a
    ``partial`` run to ``completed`` only when no other blockers remain.
    Mutates and returns ``record``.
    """
    record.browser_verification = result
    record.blockers = [
        b
        for b in record.blockers
        if not b.startswith("browser verification failed:")
    ]
    if result.enabled and result.status == "failed":
        if record.status in (RunStatus.COMPLETED, RunStatus.PARTIAL):
            record.status = RunStatus.PARTIAL
            blocker_msg = (
                f"browser verification failed: {result.command or '(unknown command)'}"
            )
            if blocker_msg not in record.blockers:
                record.blockers.append(blocker_msg)
    elif result.enabled and result.status == "passed":
        if record.status == RunStatus.PARTIAL and not record.blockers:
            record.status = RunStatus.COMPLETED
    return record


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
    if result.install_status:
        install_line = f"- **Dependency install**: {result.install_status}"
        if result.install_command:
            install_line += f" (`{result.install_command}`)"
        lines.append(install_line)
    if result.command:
        lines.append(f"- **Command**: `{result.command}`")
    if result.url:
        lines.append(f"- **URL**: {result.url}")
    if result.screenshot_path:
        lines.append(f"- **Screenshot**: `{result.screenshot_path}`")
    if result.readiness:
        lines.append(f"- **Render readiness**: {result.readiness}")
    if result.duration_ms is not None:
        lines.append(f"- **Duration**: {result.duration_ms} ms")
    if result.pages:
        lines.append(f"- **Pages captured**: {len(result.pages)}")
        for page in result.pages:
            label = page.label or page.path
            lines.append(
                f"  - `{page.path}` — {label} ({page.readiness})"
            )
    if result.install_output_preview:
        lines.append("")
        lines.append("Install output:")
        lines.append("```")
        lines.append(result.install_output_preview)
        lines.append("```")
    if result.output_preview:
        lines.append("")
        lines.append("```")
        lines.append(result.output_preview)
        lines.append("```")
    return "\n".join(lines) + "\n"
