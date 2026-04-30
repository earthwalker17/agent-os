"""Project-scoped tool runtime for the future Coding Agent.

Every operation goes through `ProjectSandbox` first. The runtime never
touches the filesystem or shell directly — it asks the sandbox to resolve
paths and validate commands, and only then performs the bounded operation.

Output sizes (file reads, shell stdout/stderr, search hits, directory
listings) are capped so a runaway tool call cannot blow up the agent's
context window or the response payload.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

from .sandbox import ProjectSandbox, SandboxViolation
from .tool_models import ToolResult


_MAX_READ_CHARS = 20_000
_MAX_SHELL_OUTPUT_CHARS = 20_000
_MAX_LIST_ENTRIES = 500
_MAX_SEARCH_HITS = 200
_MAX_SNIPPET_CHARS = 200

_SKIP_DIR_NAMES = {".git", "node_modules", ".venv", "__pycache__"}


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _err(tool: str, exc: Exception) -> ToolResult:
    return ToolResult(
        success=False,
        tool_name=tool,
        error=f"{type(exc).__name__}: {exc}",
    )


class ToolRuntime:
    def __init__(self, project_id: str):
        self.project_id = project_id
        self.sandbox = ProjectSandbox(project_id)

    # --- file tools ---

    def list_files(self, path: str = ".") -> ToolResult:
        try:
            target = self.sandbox.resolve_repo_path(path)
            if not target.exists():
                return ToolResult(
                    success=False,
                    tool_name="list_files",
                    error=f"path does not exist: {path!r}",
                )
            if not target.is_dir():
                return ToolResult(
                    success=False,
                    tool_name="list_files",
                    error=f"path is not a directory: {path!r}",
                )

            entries: list[dict] = []
            children = sorted(
                target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
            truncated = False
            for child in children:
                if len(entries) >= _MAX_LIST_ENTRIES:
                    truncated = True
                    break
                is_dir = child.is_dir()
                entries.append(
                    {
                        "name": child.name,
                        "type": "dir" if is_dir else "file",
                        "size": None if is_dir else child.stat().st_size,
                    }
                )

            output_lines = []
            for e in entries:
                tag = "[d]" if e["type"] == "dir" else "[f]"
                size = f"  ({e['size']}b)" if e["size"] is not None else ""
                output_lines.append(f"{tag} {e['name']}{size}")

            return ToolResult(
                success=True,
                tool_name="list_files",
                output="\n".join(output_lines) or "(empty directory)",
                metadata={
                    "path": path,
                    "entry_count": len(entries),
                    "entries": entries,
                    "truncated": truncated,
                },
            )
        except SandboxViolation as e:
            return _err("list_files", e)
        except Exception as e:
            return _err("list_files", e)

    def read_file(self, path: str) -> ToolResult:
        try:
            target = self.sandbox.resolve_repo_path(path)
            if not target.exists() or not target.is_file():
                return ToolResult(
                    success=False,
                    tool_name="read_file",
                    error=f"file not found: {path!r}",
                )
            try:
                raw = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return ToolResult(
                    success=False,
                    tool_name="read_file",
                    error="file is not valid UTF-8 text",
                )
            content, truncated = _truncate(raw, _MAX_READ_CHARS)
            return ToolResult(
                success=True,
                tool_name="read_file",
                output=content,
                metadata={
                    "path": path,
                    "bytes": target.stat().st_size,
                    "char_count": len(raw),
                    "truncated": truncated,
                    "truncation_limit": _MAX_READ_CHARS,
                },
            )
        except SandboxViolation as e:
            return _err("read_file", e)
        except Exception as e:
            return _err("read_file", e)

    def write_file(self, path: str, content: str) -> ToolResult:
        try:
            target = self.sandbox.resolve_repo_path(path)
            if target == self.sandbox.repo_dir.resolve():
                return ToolResult(
                    success=False,
                    tool_name="write_file",
                    error="cannot write to repo root itself",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return ToolResult(
                success=True,
                tool_name="write_file",
                output=f"wrote {len(content)} chars to {path}",
                metadata={
                    "path": path,
                    "bytes": target.stat().st_size,
                },
            )
        except SandboxViolation as e:
            return _err("write_file", e)
        except Exception as e:
            return _err("write_file", e)

    def append_file(self, path: str, content: str) -> ToolResult:
        try:
            target = self.sandbox.resolve_repo_path(path)
            if target == self.sandbox.repo_dir.resolve():
                return ToolResult(
                    success=False,
                    tool_name="append_file",
                    error="cannot append to repo root itself",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as f:
                f.write(content)
            return ToolResult(
                success=True,
                tool_name="append_file",
                output=f"appended {len(content)} chars to {path}",
                metadata={
                    "path": path,
                    "bytes": target.stat().st_size,
                },
            )
        except SandboxViolation as e:
            return _err("append_file", e)
        except Exception as e:
            return _err("append_file", e)

    def search_files(self, query: str, path: str = ".") -> ToolResult:
        try:
            if not query:
                return ToolResult(
                    success=False,
                    tool_name="search_files",
                    error="query is empty",
                )
            target = self.sandbox.resolve_repo_path(path)
            if not target.exists():
                return ToolResult(
                    success=False,
                    tool_name="search_files",
                    error=f"path does not exist: {path!r}",
                )

            repo = self.sandbox.repo_dir.resolve()
            hits: list[dict] = []
            scanned = 0
            skipped_binary = 0
            truncated = False

            for fpath in self._walk_files(target):
                if len(hits) >= _MAX_SEARCH_HITS:
                    truncated = True
                    break
                try:
                    text = fpath.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    skipped_binary += 1
                    continue
                scanned += 1
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if query in line:
                        rel = fpath.relative_to(repo).as_posix()
                        snippet = line.strip()
                        if len(snippet) > _MAX_SNIPPET_CHARS:
                            snippet = snippet[:_MAX_SNIPPET_CHARS] + "..."
                        hits.append({"path": rel, "line": lineno, "snippet": snippet})
                        if len(hits) >= _MAX_SEARCH_HITS:
                            truncated = True
                            break

            output = (
                "\n".join(f"{h['path']}:{h['line']}: {h['snippet']}" for h in hits)
                or "(no matches)"
            )
            return ToolResult(
                success=True,
                tool_name="search_files",
                output=output,
                metadata={
                    "query": query,
                    "path": path,
                    "hit_count": len(hits),
                    "scanned_files": scanned,
                    "skipped_binary": skipped_binary,
                    "truncated": truncated,
                    "hits": hits,
                },
            )
        except SandboxViolation as e:
            return _err("search_files", e)
        except Exception as e:
            return _err("search_files", e)

    def _walk_files(self, root: Path) -> Iterable[Path]:
        if root.is_file():
            yield root
            return
        if not root.is_dir():
            return
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir():
                if child.name in _SKIP_DIR_NAMES or child.name.startswith("."):
                    continue
                yield from self._walk_files(child)
            elif child.is_file():
                yield child

    # --- shell tool ---

    def run_shell(self, command: str, timeout_seconds: int = 30) -> ToolResult:
        try:
            self.sandbox.validate_command(command)
        except SandboxViolation as e:
            return _err("run_shell", e)

        repo = self.sandbox.repo_dir
        if not repo.exists() or not repo.is_dir():
            return ToolResult(
                success=False,
                tool_name="run_shell",
                error=f"repo dir does not exist: {repo}",
            )

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=max(1, int(timeout_seconds)),
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                tool_name="run_shell",
                error=f"command timed out after {timeout_seconds}s",
                metadata={
                    "command": command,
                    "cwd": str(repo),
                    "timeout": True,
                    "timeout_seconds": timeout_seconds,
                },
            )
        except Exception as e:
            return _err("run_shell", e)

        stdout, stdout_truncated = _truncate(proc.stdout or "", _MAX_SHELL_OUTPUT_CHARS)
        stderr, stderr_truncated = _truncate(proc.stderr or "", _MAX_SHELL_OUTPUT_CHARS)
        return ToolResult(
            success=proc.returncode == 0,
            tool_name="run_shell",
            output=stdout,
            error=stderr,
            metadata={
                "command": command,
                "cwd": str(repo),
                "exit_code": proc.returncode,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            },
        )
