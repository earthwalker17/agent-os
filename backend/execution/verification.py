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

import logging
import re
import time
from typing import Optional

from .manager import read_task_state
from .models import VerificationResult
from .tool_runtime import ToolRuntime


log = logging.getLogger(__name__)


# Keep the per-run preview compact. Cause-of-failure context is usually in
# the first / last few hundred lines of stderr — 4 000 chars is enough to
# diagnose a typical test failure without bloating run.json.
_VERIFY_OUTPUT_PREVIEW_CHARS = 4000

# Default timeout. Verification is meant to be a tight check (typecheck,
# focused test suite), not a full integration build.
DEFAULT_VERIFY_TIMEOUT_SECONDS = 120


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


def run_verification(
    project_id: str,
    *,
    runtime: Optional[ToolRuntime] = None,
    task_md_override: Optional[str] = None,
    timeout_seconds: int = DEFAULT_VERIFY_TIMEOUT_SECONDS,
) -> VerificationResult:
    """Run the project's configured verify command, if any.

    Returns a fully populated :class:`VerificationResult`. Never raises.
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
        return "## Verification\nstatus: **skipped** _(no verify command configured)_\n"

    lines: list[str] = ["## Verification"]
    lines.append(f"- **Status**: {verification.status}")
    if verification.command:
        lines.append(f"- **Command**: `{verification.command}`")
    if verification.exit_code is not None:
        lines.append(f"- **Exit code**: {verification.exit_code}")
    if verification.duration_ms is not None:
        lines.append(f"- **Duration**: {verification.duration_ms} ms")
    if verification.output_preview:
        lines.append("")
        lines.append("```")
        lines.append(verification.output_preview)
        lines.append("```")
    return "\n".join(lines) + "\n"
