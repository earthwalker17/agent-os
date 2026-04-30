"""Per-project sandbox for the execution tool runtime.

Every tool invocation must go through `ProjectSandbox` to:

- resolve repo-relative paths and reject anything that escapes
  `execution_workspaces/{project_id}/repo/`
- reject reads/writes targeting sensitive filenames (`.env`, `*.key`, etc.)
- reject obviously dangerous shell commands

The sandbox is intentionally minimal — it is the single chokepoint, not a
full shell parser. New rules belong here so every tool inherits them.
"""

from __future__ import annotations

import re
from pathlib import Path

from .manager import get_project_execution_dir


class SandboxViolation(Exception):
    """Raised when a tool tries to escape the project sandbox."""


_SENSITIVE_BASENAMES = {".env", ".env.local", ".env.production"}
_SENSITIVE_SUFFIXES = (".key", ".pem")

_BLOCKED_COMMAND_PATTERNS = (
    "rm -rf /",
    "rm -rf *",
    "del /s",
    "format",
    "shutdown",
    "reboot",
    "curl | bash",
    "wget | bash",
    "invoke-webrequest",
    "iex",
    "powershell -enc",
    "scp",
    "ssh",
    "git push",
)

# The literal "curl | bash" / "wget | bash" substrings above only match if no
# URL sits between the fetcher and the pipe, which is rarely how the attack
# is actually written. This regex catches the real pattern: any fetcher
# piped into a fresh shell.
_PIPE_TO_SHELL_REGEX = re.compile(
    r"\b(curl|wget|iwr|invoke-webrequest)\b[^|]*\|\s*(bash|sh|zsh|powershell|pwsh)\b",
    re.IGNORECASE,
)


class ProjectSandbox:
    def __init__(self, project_id: str):
        self.project_id = project_id

    @property
    def workspace_dir(self) -> Path:
        return get_project_execution_dir(self.project_id)

    @property
    def repo_dir(self) -> Path:
        return self.workspace_dir / "repo"

    def resolve_repo_path(self, relative_path: str) -> Path:
        if relative_path is None or not isinstance(relative_path, str):
            raise SandboxViolation("path must be a non-empty string")

        normalized = relative_path.replace("\\", "/").strip()
        if not normalized:
            normalized = "."

        candidate = Path(normalized)

        if candidate.is_absolute():
            raise SandboxViolation(f"absolute paths are not allowed: {relative_path!r}")

        if ".." in candidate.parts:
            raise SandboxViolation(
                f"path traversal '..' is not allowed: {relative_path!r}"
            )

        for part in candidate.parts:
            lowered_part = part.lower()
            if part in _SENSITIVE_BASENAMES:
                raise SandboxViolation(
                    f"sensitive file is not accessible: {relative_path!r}"
                )
            if lowered_part.endswith(_SENSITIVE_SUFFIXES):
                raise SandboxViolation(
                    f"sensitive file is not accessible: {relative_path!r}"
                )
            if part == ".ssh":
                raise SandboxViolation(
                    f"sensitive path is not accessible: {relative_path!r}"
                )

        if ".git/config" in normalized.lower():
            raise SandboxViolation(
                f"sensitive path is not accessible: {relative_path!r}"
            )

        repo = self.repo_dir.resolve()
        target = (repo / candidate).resolve()
        try:
            target.relative_to(repo)
        except ValueError:
            raise SandboxViolation(
                f"path escapes repo sandbox: {relative_path!r}"
            )
        return target

    def validate_command(self, command: str) -> None:
        if command is None or not isinstance(command, str) or not command.strip():
            raise SandboxViolation("command must be a non-empty string")

        lowered = command.lower()
        for pattern in _BLOCKED_COMMAND_PATTERNS:
            if pattern in lowered:
                raise SandboxViolation(
                    f"command rejected by sandbox policy (matched {pattern!r})"
                )
        if _PIPE_TO_SHELL_REGEX.search(command):
            raise SandboxViolation(
                "command rejected by sandbox policy (piping a fetcher into a shell)"
            )
