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
    # Destructive Git operations that discard work or rewrite refs. The Coding
    # Agent's free-form `run_shell` must never run these (CLAUDE.md §3). Project
    # Ops performs *confirmed* destructive Git through the separate typed
    # `run_git` surface (`validate_git` + `allow_destructive`), which does NOT
    # consult this block-list — so a user-approved rollback still works while the
    # agent loop stays fenced.
    "git reset --hard",
    "git clean -f",
    "git checkout -- ",
    "git checkout .",
    "git restore .",
    "git branch -d",
)

# Git subcommands the typed `run_git` surface (Project Ops) may invoke. This is
# an allow-list for our own controlled callers (git_ops / the GitHub connector);
# the Coding Agent never reaches run_git. Anything outside this set is rejected.
_GIT_ALLOWED_SUBCOMMANDS = frozenset(
    {
        "init", "status", "diff", "add", "rm", "commit", "branch", "checkout",
        "switch", "restore", "reset", "clean", "stash", "tag", "rev-parse",
        "rev-list", "log", "show", "remote", "fetch", "push", "config",
        "ls-files", "symbolic-ref", "update-ref", "cat-file", "merge-base",
        "show-ref", "write-tree", "commit-tree",
    }
)

# Supabase CLI subcommands the typed `run_supabase` surface (Phase 8 Production
# Path) may invoke. Like `_GIT_ALLOWED_SUBCOMMANDS`, this is an allow-list for our
# own controlled callers (supabase_connector); the Coding Agent never reaches
# run_supabase. The "subcommand" is the FIRST token (`db`, `migration`, `link`,
# `start`, …); destructiveness keys off the first token + its action word.
_SUPABASE_ALLOWED_SUBCOMMANDS = frozenset(
    {
        "init", "login", "link", "start", "stop", "status",
        "db", "migration", "gen", "projects",
    }
)

# The literal "curl | bash" / "wget | bash" substrings above only match if no
# URL sits between the fetcher and the pipe, which is rarely how the attack
# is actually written. This regex catches the real pattern: any fetcher
# piped into a fresh shell.
_PIPE_TO_SHELL_REGEX = re.compile(
    r"\b(curl|wget|iwr|invoke-webrequest)\b[^|]*\|\s*(bash|sh|zsh|powershell|pwsh)\b",
    re.IGNORECASE,
)


def _is_destructive_git(sub: str, rest: list[str]) -> bool:
    """Whether a typed `git <sub> <rest...>` invocation discards work / rewrites
    refs and therefore needs explicit user confirmation (``allow_destructive``).

    Conservative: when in doubt for a known-dangerous subcommand, return True.
    """
    tokens = [t.lower() for t in rest]
    if sub == "reset":
        return "--hard" in tokens
    if sub == "clean":
        # clean removes untracked files; only a dry-run is non-destructive.
        return not any(t in ("-n", "--dry-run") for t in tokens)
    if sub == "push":
        return any(
            t in ("-f", "--force") or t.startswith("--force-with-lease")
            for t in tokens
        )
    if sub == "checkout":
        # `checkout -- <path>` / `checkout .` / `-f` discard working-tree edits.
        return "--" in tokens or "." in tokens or "-f" in tokens or "--force" in tokens
    if sub == "restore":
        # `restore --staged <path>` only unstages (safe); anything touching the
        # worktree discards changes.
        return not ("--staged" in tokens and "--worktree" not in tokens)
    if sub == "switch":
        return "-f" in tokens or "--force" in tokens or "--discard-changes" in tokens
    if sub == "branch":
        return any(t in ("-d", "-D", "--delete") for t in tokens)
    if sub == "update-ref":
        return "-d" in tokens
    return False


def _is_destructive_supabase(sub: str, rest: list[str]) -> bool:
    """Whether a typed `supabase <sub> <rest...>` invocation mutates a REMOTE
    database / discards data and therefore needs explicit user confirmation.

    Unlike git, Supabase's danger is keyed off the SUBCOMMAND, not a flag: a bare
    ``db push`` applies migrations to the linked production DB — the single most
    destructive op in the phase. So this is **default-deny by action**, not a
    git-style "bare verb is safe" clone:

    - ``db push`` (without ``--dry-run``)  → applies migrations to the remote DB
    - ``db reset``                         → drops + recreates a DB (local data loss)
    - ``db remote commit``                 → writes remote migration history
    - ``migration up`` with ``--linked``   → applies to the remote DB
    - ``branches delete``                  → deletes a remote branch
    - any op carrying ``--linked``/``--db-url`` that is NOT a ``--dry-run``
      → treated as remote-targeting, destructive by default
    """
    tokens = [t.lower() for t in rest]
    action = tokens[0] if tokens else ""
    dry_run = "--dry-run" in tokens
    remote_targeted = ("--linked" in tokens) or any(t == "--db-url" or t.startswith("--db-url=") for t in tokens)

    if sub == "db":
        if action in ("diff", "lint", "dump", "test"):
            return False  # read-only inspection (diff needs Docker but mutates nothing)
        if action in ("push", "reset", "pull"):
            # push/pull touch the remote; reset wipes a DB. dry-run push is safe.
            if action == "push" and dry_run:
                return False
            return True
        if action == "remote":
            return True  # `db remote commit` writes remote migration history
        # any other db action that targets a remote is gated by default
        return remote_targeted and not dry_run
    if sub == "migration":
        if action == "up":
            return remote_targeted  # local `migration up` is recoverable via reset
        if action in ("repair", "squash"):
            return remote_targeted
        return False  # `migration new` / `migration list` are safe
    if sub == "branches":
        return action in ("delete", "disable")
    # link/start/stop/status/init/login/gen are not data-destructive; but any
    # other subcommand explicitly targeting a remote is gated by default.
    return remote_targeted and not dry_run


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

    def validate_git(self, args, *, allow_destructive: bool = False) -> None:
        """Validate a typed Git argv (without the leading ``git``).

        Used only by the Project Ops layer (`ToolRuntime.run_git` → git_ops /
        GitHub connector); the Coding Agent never reaches this path. Enforces:

        - a non-empty list of plain strings, no embedded control characters
          (the argv is executed with ``shell=False`` so there is no shell
          interpolation, but we still reject newlines/NULs defensively);
        - an allow-listed subcommand (`_GIT_ALLOWED_SUBCOMMANDS`);
        - destructive subcommands (`reset --hard`, force-push, `clean`,
          worktree-discarding `checkout`/`restore`, branch deletes, …) only when
          ``allow_destructive`` is True — which a caller passes solely from a
          user-confirmed action endpoint (e.g. rollback).

        Note: a non-force ``push`` is allowed here (it is not *locally*
        destructive); its *external* nature is gated separately, at the
        confirm endpoint, never in the sandbox.
        """
        if not isinstance(args, (list, tuple)) or not args:
            raise SandboxViolation("git args must be a non-empty list")
        cleaned: list[str] = []
        for a in args:
            if not isinstance(a, str):
                raise SandboxViolation("git args must all be strings")
            # Only NUL is rejected. Newlines are legitimate in a commit message
            # (subject + body) and, since run_git uses shell=False with an argv
            # list, they carry NO injection risk — each arg reaches git verbatim.
            if "\x00" in a:
                raise SandboxViolation("git args must not contain NUL bytes")
            cleaned.append(a)

        sub = cleaned[0].lower()
        if sub not in _GIT_ALLOWED_SUBCOMMANDS:
            raise SandboxViolation(f"git subcommand not allowed: {sub!r}")
        if _is_destructive_git(sub, cleaned[1:]) and not allow_destructive:
            raise SandboxViolation(
                f"destructive git operation requires explicit confirmation: "
                f"git {sub} ..."
            )

    def validate_supabase(self, args, *, allow_destructive: bool = False) -> None:
        """Validate a typed Supabase CLI argv (without the leading ``supabase``).

        Used only by the Production Path layer (`ToolRuntime.run_supabase` →
        supabase_connector); the Coding Agent never reaches this path. Mirrors
        `validate_git`: a non-empty list of plain strings (no NUL — shell=False
        makes newlines safe), an allow-listed subcommand
        (`_SUPABASE_ALLOWED_SUBCOMMANDS`), and remote-mutating / data-discarding
        actions (`db push`, `db reset`, `migration up --linked`, …) only when
        ``allow_destructive`` is True (passed solely from a user-confirmed action
        endpoint). See `_is_destructive_supabase` — destructiveness is keyed off
        the subcommand, not a flag, because a bare ``db push`` hits the remote DB.
        """
        if not isinstance(args, (list, tuple)) or not args:
            raise SandboxViolation("supabase args must be a non-empty list")
        cleaned: list[str] = []
        for a in args:
            if not isinstance(a, str):
                raise SandboxViolation("supabase args must all be strings")
            if "\x00" in a:
                raise SandboxViolation("supabase args must not contain NUL bytes")
            cleaned.append(a)

        sub = cleaned[0].lower()
        if sub not in _SUPABASE_ALLOWED_SUBCOMMANDS:
            raise SandboxViolation(f"supabase subcommand not allowed: {sub!r}")
        if _is_destructive_supabase(sub, cleaned[1:]) and not allow_destructive:
            raise SandboxViolation(
                f"destructive supabase operation requires explicit confirmation: "
                f"supabase {sub} {' '.join(cleaned[1:2])} ..."
            )
