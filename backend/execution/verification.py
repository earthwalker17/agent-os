"""Task 06.2A — command-based verification for Coding Agent runs.

After the runner finalizes a normal Coding Agent run, this module optionally
executes a single project-defined shell command and records the outcome on
the ``RunRecord`` and inside ``result.md``.

Design constraints (kept tight on purpose):

  - **Optional.** No verify command => status ``skipped``. Never blocks the
    run from completing.
  - **One command.** First uncommented, non-empty line under the
    ``## Verification`` heading in the project's ``TASK.md``.
  - **Sandboxed.** Routed through ``ToolRuntime.run_shell``, which routes
    through ``ProjectSandbox.validate_command``. Same block-list as every
    other shell call. Unsafe commands are recorded as ``failed`` with the
    sandbox reason in ``output_preview``.
  - **Bounded output.** The captured stdout/stderr is concatenated, header-
    labeled, and truncated to ``_VERIFY_OUTPUT_PREVIEW_CHARS`` so it stays
    safe to round-trip into ``result.md`` and ``run.json``.
  - **Best-effort.** Any exception inside this module is converted into a
    ``VerificationResult(status='failed', ...)`` — verification must never
    crash background finalization.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .manager import read_task_state
from .models import VerificationCommandResult, VerificationResult
from .tool_runtime import ToolRuntime


log = logging.getLogger(__name__)


# Keep the per-run preview compact. Cause-of-failure context is usually in
# the first / last few hundred lines of stderr — 4 000 chars is enough to
# diagnose a typical test failure without bloating run.json.
_VERIFY_OUTPUT_PREVIEW_CHARS = 4000

# Default timeout. Verification is meant to be a tight check (typecheck,
# focused test suite), not a full integration build.
DEFAULT_VERIFY_TIMEOUT_SECONDS = 120

# Per-kind timeouts for inferred commands. ``npm install`` on a cold cache
# is slow, so it gets a generous budget; a build sits between that and a
# focused test run; a syntax check is fast.
#
# Install gets the full sandbox ceiling (``ToolRuntime`` clamps run_shell to
# 600 s): a cold ``npm install`` for a real Vite + React + TypeScript +
# Tailwind app on Windows (with Defender scanning every extracted file) can
# run well past 300 s — the Aegis Launch Control build timed out at 300 s on
# the first attempt, marking the whole run failed and leaving node_modules
# half-populated. 600 s gives that install room to finish on a cold machine
# while still bounding worst-case latency.
_INSTALL_TIMEOUT_SECONDS = 600
_BUILD_TIMEOUT_SECONDS = 300
_TEST_TIMEOUT_SECONDS = 180
_SYNTAX_TIMEOUT_SECONDS = 60

# Directory names we never walk when sniffing the repo for inference.
_SKIP_DIR_NAMES = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}


# Match the heading row, then capture everything until the next top-level
# heading (or end of file).
_VERIFICATION_SECTION_REGEX = re.compile(
    r"^##\s+Verification\s*$(?P<body>.*?)(?=^##\s+|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

# Match a single bash fence inside the verification section.
_BASH_FENCE_REGEX = re.compile(
    r"```(?:bash|sh|shell|console)?\s*\n(?P<body>.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def parse_verify_command(task_md_text: str) -> Optional[str]:
    """Extract a single verify command from TASK.md.

    Returns ``None`` when:
      - the file has no ``## Verification`` section,
      - the section has no fenced block,
      - every non-empty line in the block is a comment.

    Otherwise returns the first non-empty, non-comment line, stripped.
    """
    if not task_md_text:
        return None
    section_match = _VERIFICATION_SECTION_REGEX.search(task_md_text)
    if section_match is None:
        return None
    section_body = section_match.group("body")
    if not section_body:
        return None

    fence_match = _BASH_FENCE_REGEX.search(section_body)
    if fence_match is None:
        # Tolerate the no-fence variant: take the first non-comment line of
        # the section body itself.
        candidate_lines = section_body.splitlines()
    else:
        candidate_lines = fence_match.group("body").splitlines()

    for raw_line in candidate_lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        return line
    return None


def parse_verify_commands(task_md_text: str) -> list[str]:
    """Extract *all* uncommented verify commands from TASK.md (Task 06.2E).

    Same section/fence rules as :func:`parse_verify_command`, but returns
    every non-empty, non-comment line in order so a manual block can declare
    more than one command (e.g. ``python -m pytest`` then ``npm run build``).
    Returns ``[]`` when nothing is configured.
    """
    if not task_md_text:
        return []
    section_match = _VERIFICATION_SECTION_REGEX.search(task_md_text)
    if section_match is None:
        return []
    section_body = section_match.group("body")
    if not section_body:
        return []

    fence_match = _BASH_FENCE_REGEX.search(section_body)
    candidate_lines = (
        fence_match.group("body").splitlines()
        if fence_match is not None
        else section_body.splitlines()
    )

    commands: list[str] = []
    for raw_line in candidate_lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        commands.append(line)
    return commands


# ---------- automatic inference (Task 06.2E) ----------


@dataclass
class VerifyCommandSpec:
    """A single planned verification command + how to run it."""

    command: str
    kind: str  # "install" | "build" | "test" | "syntax" | "manual"
    timeout_seconds: int = DEFAULT_VERIFY_TIMEOUT_SECONDS


def _iter_repo_files(repo_dir: Path):
    """Yield files under ``repo_dir``, skipping vendored / build dirs."""
    if not repo_dir.exists() or not repo_dir.is_dir():
        return
    stack = [repo_dir]
    while stack:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if child.name in _SKIP_DIR_NAMES or child.name.startswith("."):
                    continue
                stack.append(child)
            elif child.is_file():
                yield child


def _read_package_json(repo_dir: Path) -> Optional[dict]:
    pkg = repo_dir / "package.json"
    if not pkg.exists() or not pkg.is_file():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _has_python_tests(repo_dir: Path) -> bool:
    """True when the repo looks like it has a runnable pytest suite."""
    for marker in ("conftest.py", "pytest.ini", "tox.ini"):
        if (repo_dir / marker).exists():
            return True
    if (repo_dir / "tests").is_dir() or (repo_dir / "test").is_dir():
        return True
    for fpath in _iter_repo_files(repo_dir):
        name = fpath.name
        if name.startswith("test_") and name.endswith(".py"):
            return True
        if name.endswith("_test.py"):
            return True
    return False


def _has_python_sources(repo_dir: Path) -> bool:
    for fpath in _iter_repo_files(repo_dir):
        if fpath.suffix == ".py":
            return True
    return False


def _syntax_check_spec() -> VerifyCommandSpec:
    """A lightweight, always-available Python syntax check.

    ``compileall`` runs from the repo cwd, so ``node_modules`` (which can hold
    vendored, sometimes Python-2-only ``.py`` files for a full-stack repo) is
    excluded via ``-x`` so it can't derail an otherwise-clean backend.
    """
    return VerifyCommandSpec(
        command="python -m compileall -q -x node_modules .",
        kind="syntax",
        timeout_seconds=_SYNTAX_TIMEOUT_SECONDS,
    )


def infer_verification_specs(
    repo_dir: Path, *, pytest_available: bool = True
) -> list[VerifyCommandSpec]:
    """Infer safe verification commands from the repo contents (Task 06.2E).

    Cases handled (a full-stack repo can match more than one):

      - ``package.json`` with a ``build`` script -> ``npm install`` (only when
        ``node_modules`` is absent) followed by ``npm run build``.
      - Python project with tests -> ``python -m pytest`` **when pytest is
        importable in the verification shell**; otherwise a lightweight
        ``compileall`` syntax check, because running pytest without the runner
        installed would fail every time (no virtualenv is managed for the
        project).
      - Python project without tests -> the ``compileall`` syntax check.

    ``pytest_available`` is supplied by :func:`plan_verification` after probing
    the actual shell; it defaults to ``True`` so the pure inference stays easy
    to reason about in isolation.

    Returns ``[]`` when nothing safe can be inferred (the caller records the
    verification as ``skipped``). The order is frontend-build first, then the
    backend check, so a failing install short-circuits before the Python check.
    """
    specs: list[VerifyCommandSpec] = []

    pkg = _read_package_json(repo_dir)
    if pkg is not None:
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        if isinstance(scripts, dict) and scripts.get("build"):
            if not (repo_dir / "node_modules").is_dir():
                specs.append(
                    VerifyCommandSpec(
                        command="npm install",
                        kind="install",
                        timeout_seconds=_INSTALL_TIMEOUT_SECONDS,
                    )
                )
            specs.append(
                VerifyCommandSpec(
                    command="npm run build",
                    kind="build",
                    timeout_seconds=_BUILD_TIMEOUT_SECONDS,
                )
            )

    if _has_python_tests(repo_dir):
        if pytest_available:
            specs.append(
                VerifyCommandSpec(
                    command="python -m pytest",
                    kind="test",
                    timeout_seconds=_TEST_TIMEOUT_SECONDS,
                )
            )
        else:
            # Tests exist but the runner isn't installed — fall back to a
            # syntax check so verification still provides value and can pass.
            specs.append(_syntax_check_spec())
    elif _has_python_sources(repo_dir):
        specs.append(_syntax_check_spec())

    return specs


def _pytest_importable(runtime: ToolRuntime) -> bool:
    """Probe whether ``pytest`` can be imported in the verification shell.

    Uses the same sandboxed ``run_shell`` path (and therefore the same
    ``python`` on PATH) that the verify command itself would use, so the
    answer matches reality. Conservative: any error / non-zero exit is treated
    as "not available".
    """
    try:
        result = runtime.run_shell('python -c "import pytest"', timeout_seconds=30)
    except Exception:  # noqa: BLE001
        return False
    metadata = result.metadata or {}
    return bool(result.success and metadata.get("exit_code") == 0)


def plan_verification(
    project_id: str,
    task_md_text: str,
    *,
    runtime: Optional[ToolRuntime] = None,
) -> tuple[str, list[VerifyCommandSpec]]:
    """Decide how to verify a run: manual override, inference, or skip.

    Returns ``(mode, specs)`` where ``mode`` is ``"manual"`` /
    ``"inferred"`` / ``"skipped"``. A manual ``## Verification`` block always
    wins; otherwise we infer from the repo. When the inferred plan would run
    pytest, we first probe (through ``runtime``) that pytest is importable —
    if it isn't, inference falls back to a syntax check instead of a command
    that's certain to fail. ``specs`` is empty for ``"skipped"``.
    """
    manual = parse_verify_commands(task_md_text)
    if manual:
        return "manual", [
            VerifyCommandSpec(
                command=cmd, kind="manual", timeout_seconds=DEFAULT_VERIFY_TIMEOUT_SECONDS
            )
            for cmd in manual
        ]
    rt = runtime or ToolRuntime(project_id)
    repo_dir = rt.sandbox.repo_dir
    # Only pay for the pytest probe when the repo actually has tests to run.
    pytest_available = True
    if _has_python_tests(repo_dir):
        pytest_available = _pytest_importable(rt)
    inferred = infer_verification_specs(repo_dir, pytest_available=pytest_available)
    if inferred:
        return "inferred", inferred
    return "skipped", []


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated, +{len(text) - limit} chars]"


def _build_output_preview(stdout: str, stderr: str) -> str:
    parts: list[str] = []
    if stdout:
        parts.append("--- stdout ---\n" + stdout.strip())
    if stderr:
        parts.append("--- stderr ---\n" + stderr.strip())
    combined = "\n\n".join(parts).strip()
    return _truncate(combined, _VERIFY_OUTPUT_PREVIEW_CHARS)


def _run_single_spec(
    spec: VerifyCommandSpec, runtime: ToolRuntime
) -> VerificationCommandResult:
    """Run one spec through the sandboxed shell. Never raises."""
    start = time.perf_counter()
    try:
        result = runtime.run_shell(spec.command, timeout_seconds=spec.timeout_seconds)
    except Exception as exc:  # noqa: BLE001
        return VerificationCommandResult(
            command=spec.command,
            kind=spec.kind,
            status="failed",
            exit_code=None,
            output_preview=_truncate(
                f"verification command crashed: {type(exc).__name__}: {exc}",
                _VERIFY_OUTPUT_PREVIEW_CHARS,
            ),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
    duration_ms = int((time.perf_counter() - start) * 1000)
    metadata = result.metadata or {}
    exit_code = metadata.get("exit_code")
    timed_out = bool(metadata.get("timeout"))

    if not result.success and exit_code is None:
        reason = (result.error or "verification command failed").strip()
        if timed_out:
            reason = f"verification command timed out after {spec.timeout_seconds}s"
        return VerificationCommandResult(
            command=spec.command,
            kind=spec.kind,
            status="failed",
            exit_code=None,
            output_preview=_truncate(reason, _VERIFY_OUTPUT_PREVIEW_CHARS),
            duration_ms=duration_ms,
        )

    preview = _build_output_preview(result.output or "", result.error or "")
    status = "passed" if (result.success and exit_code == 0) else "failed"
    return VerificationCommandResult(
        command=spec.command,
        kind=spec.kind,
        status=status,
        exit_code=exit_code if isinstance(exit_code, int) else None,
        output_preview=preview,
        duration_ms=duration_ms,
    )


def _aggregate(
    mode: str,
    commands: list[VerificationCommandResult],
    repair_attempts: int = 0,
) -> VerificationResult:
    """Fold per-command results into the aggregate VerificationResult.

    The top-level ``command`` / ``exit_code`` / ``output_preview`` /
    ``duration_ms`` mirror the first failing command when any failed,
    otherwise the last command that ran — so the existing single-command
    consumers (and 06.2A tests) keep seeing meaningful values.
    """
    enabled = bool(commands)
    failing = next((c for c in commands if c.status == "failed"), None)
    representative = failing or (commands[-1] if commands else None)
    status = "failed" if failing is not None else ("passed" if commands else "skipped")
    total_duration = sum(c.duration_ms or 0 for c in commands) or None
    return VerificationResult(
        enabled=enabled,
        command=representative.command if representative else None,
        status=status,
        exit_code=representative.exit_code if representative else None,
        output_preview=representative.output_preview if representative else "",
        duration_ms=total_duration,
        mode=mode if enabled else "skipped",
        commands=commands,
        repair_attempts=repair_attempts,
    )


def run_verification_specs(
    project_id: str,
    specs: list[VerifyCommandSpec],
    *,
    mode: str,
    runtime: Optional[ToolRuntime] = None,
    repair_attempts: int = 0,
) -> VerificationResult:
    """Run a list of verification specs in order (Task 06.2E). Never raises.

    Stops at the first failing command — the remaining commands are recorded
    as ``skipped`` so the report shows what didn't get a chance to run (e.g.
    ``npm run build`` is skipped when ``npm install`` fails). Returns the
    aggregate :class:`VerificationResult`.
    """
    if not specs:
        return VerificationResult(
            enabled=False,
            command=None,
            status="skipped",
            mode="skipped",
            commands=[],
            repair_attempts=repair_attempts,
        )
    try:
        rt = runtime or ToolRuntime(project_id)
        results: list[VerificationCommandResult] = []
        failed = False
        for spec in specs:
            if failed:
                results.append(
                    VerificationCommandResult(
                        command=spec.command, kind=spec.kind, status="skipped"
                    )
                )
                continue
            outcome = _run_single_spec(spec, rt)
            results.append(outcome)
            if outcome.status == "failed":
                failed = True
        return _aggregate(mode, results, repair_attempts=repair_attempts)
    except Exception as exc:  # noqa: BLE001
        log.exception("Verification specs crashed for project %s", project_id)
        return VerificationResult(
            enabled=True,
            command=None,
            status="failed",
            output_preview=f"verification crashed: {type(exc).__name__}: {exc}",
            mode=mode,
            commands=[],
            repair_attempts=repair_attempts,
        )


def run_verification(
    project_id: str,
    *,
    runtime: Optional[ToolRuntime] = None,
    task_md_override: Optional[str] = None,
    timeout_seconds: int = DEFAULT_VERIFY_TIMEOUT_SECONDS,
) -> VerificationResult:
    """Run the project's configured verify command, if any.

    Returns a fully populated :class:`VerificationResult`. Never raises.

    Retained from Task 06.2A for the single-command path and its unit tests;
    the runner now uses :func:`plan_verification` + :func:`run_verification_specs`
    for the inference + multi-command + repair flow.
    """
    try:
        task_md = task_md_override if task_md_override is not None else (read_task_state(project_id) or "")
        command = parse_verify_command(task_md)
        if not command:
            return VerificationResult(
                enabled=False,
                command=None,
                status="skipped",
                exit_code=None,
                output_preview="",
                duration_ms=None,
            )

        rt = runtime or ToolRuntime(project_id)
        start = time.perf_counter()
        result = rt.run_shell(command, timeout_seconds=timeout_seconds)
        duration_ms = int((time.perf_counter() - start) * 1000)

        metadata = result.metadata or {}
        exit_code = metadata.get("exit_code")
        timed_out = bool(metadata.get("timeout"))

        if not result.success and exit_code is None:
            # Sandbox rejection, timeout, or other runtime error — surface
            # the reason in the preview field so the UI can show it.
            reason = (result.error or "verification command failed").strip()
            if timed_out:
                reason = f"verification command timed out after {timeout_seconds}s"
            return VerificationResult(
                enabled=True,
                command=command,
                status="failed",
                exit_code=None,
                output_preview=_truncate(reason, _VERIFY_OUTPUT_PREVIEW_CHARS),
                duration_ms=duration_ms,
            )

        preview = _build_output_preview(result.output or "", result.error or "")
        status = "passed" if (result.success and exit_code == 0) else "failed"
        return VerificationResult(
            enabled=True,
            command=command,
            status=status,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            output_preview=preview,
            duration_ms=duration_ms,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Verification crashed for project %s", project_id)
        return VerificationResult(
            enabled=True,
            command=None,
            status="failed",
            exit_code=None,
            output_preview=f"verification crashed: {type(exc).__name__}: {exc}",
            duration_ms=None,
        )


def render_verification_section(verification: Optional[VerificationResult]) -> str:
    """Render a Markdown ``## Verification`` block for ``result.md``.

    Always emits the section so reviewers can tell verification was
    considered — even when it was skipped.
    """
    if verification is None:
        return "## Verification\n_(not run)_\n"

    if not verification.enabled:
        reason = (
            "no safe verify command could be inferred"
            if verification.mode == "skipped"
            else "no verify command configured"
        )
        return f"## Verification\nstatus: **skipped** _({reason})_\n"

    lines: list[str] = ["## Verification"]
    lines.append(f"- **Status**: {verification.status}")
    if verification.mode:
        lines.append(f"- **Mode**: {verification.mode}")
    if verification.repair_attempts:
        lines.append(f"- **Repair attempts**: {verification.repair_attempts}")
    if verification.duration_ms is not None:
        lines.append(f"- **Duration**: {verification.duration_ms} ms")

    # Per-command breakdown (Task 06.2E). Fall back to the aggregate single
    # command for older / single-command results that carry no `commands`.
    if verification.commands:
        lines.append("")
        lines.append("Commands:")
        for cmd in verification.commands:
            bits = [f"`{cmd.command}`", f"({cmd.kind})", f"**{cmd.status}**"]
            if cmd.exit_code is not None:
                bits.append(f"exit {cmd.exit_code}")
            lines.append("- " + " — ".join(bits))
        # Show the output of the representative (first failing) command.
        failing = next((c for c in verification.commands if c.status == "failed"), None)
        preview = failing.output_preview if failing else verification.output_preview
        if preview:
            lines.append("")
            lines.append("```")
            lines.append(preview)
            lines.append("```")
    else:
        if verification.command:
            lines.append(f"- **Command**: `{verification.command}`")
        if verification.exit_code is not None:
            lines.append(f"- **Exit code**: {verification.exit_code}")
        if verification.output_preview:
            lines.append("")
            lines.append("```")
            lines.append(verification.output_preview)
            lines.append("```")
    return "\n".join(lines) + "\n"
