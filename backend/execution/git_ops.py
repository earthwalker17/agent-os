"""Phase 7 — sandboxed local Git operations (Project Ops layer).

Every Git command here routes through ``ToolRuntime.run_git`` (the single Git
executor): ``shell=False`` argv, sandbox-validated, output-bounded, with a
hardened non-interactive environment. This module never imports ``subprocess``,
``os.system``, ``pathlib`` writes against the repo tree, or GitPython — the
sandbox boundary is absolute.

Design constraints (constitutional):

- **No history pollution.** A pre-run *checkpoint* is captured as an
  *out-of-branch* commit object (`write-tree` + `commit-tree` against a throwaway
  index via ``GIT_INDEX_FILE``) and tagged, so the working branch is untouched
  and the eventual user commit is the only commit on the branch.
- **Secrets never enter a diff / commit.** ``capture_diff`` runs its output
  through ``_redact`` before returning; ``commit`` refuses to stage
  secret-looking files even if they slipped past ``.gitignore``.
- **Best-effort.** Functions return structured results and never raise into the
  run loop; a Git failure is reported, not thrown.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import manager
from .tool_runtime import ToolRuntime

log = logging.getLogger(__name__)

CHECKPOINT_TAG_PREFIX = "agentos-checkpoint-"
_DIFF_MAX_CHARS = 60_000
_DEFAULT_BRANCH = "main"

# Files we never stage into a commit, regardless of .gitignore. Defense in depth
# behind the .gitignore ensure step.
_SECRET_BASENAMES = {
    "id_rsa", "id_ed25519", "credentials.json", "secrets.json", ".npmrc",
    ".netrc",
}
_SECRET_SUFFIXES = (".key", ".pem", ".pfx", ".p12", ".keystore")


# ---------- redaction ----------

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_KV_SECRET_RE = re.compile(
    r"(?im)^(\s*[A-Z0-9_]*"
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|APIKEY|ACCESS[_-]?KEY|"
    r"PRIVATE[_-]?KEY|CLIENT[_-]?SECRET)"
    r"[A-Z0-9_]*\s*[=:]\s*)\S+"
)
_TOKEN_RES = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
)


def _redact(text: str) -> str:
    """Best-effort scrub of common secret shapes before a diff/stat is stored
    or shown. Pattern-based; the credential store's own value-scrub (7.5) layers
    on top where available."""
    if not text:
        return text
    out = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    out = _KV_SECRET_RE.sub(r"\1[REDACTED]", out)
    for pat in _TOKEN_RES:
        out = pat.sub("[REDACTED]", out)
    return out


# ---------- result shapes ----------


@dataclass
class GitStatus:
    is_repo: bool = False
    branch: Optional[str] = None
    dirty: bool = False
    untracked: int = 0
    modified: int = 0
    staged: int = 0
    head: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "is_repo": self.is_repo,
            "branch": self.branch,
            "dirty": self.dirty,
            "untracked": self.untracked,
            "modified": self.modified,
            "staged": self.staged,
            "head": self.head,
            "error": self.error,
        }


@dataclass
class CheckpointResult:
    created: bool = False
    ref: Optional[str] = None        # the checkpoint commit sha (out-of-branch)
    base_commit: Optional[str] = None  # branch HEAD at checkpoint time
    tag: Optional[str] = None
    error: Optional[str] = None


@dataclass
class DiffResult:
    captured: bool = False
    diff_text: str = ""              # redacted, bounded
    stat: str = ""                   # redacted diff-stat summary
    files: list[str] = field(default_factory=list)
    truncated: bool = False
    error: Optional[str] = None


@dataclass
class CommitResult:
    committed: bool = False
    sha: Optional[str] = None
    branch: Optional[str] = None
    message: str = ""
    files: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)  # secret-looking, not staged
    error: Optional[str] = None


@dataclass
class RollbackResult:
    rolled_back: bool = False
    target: Optional[str] = None
    error: Optional[str] = None


# ---------- internals ----------


def _repo_dir(project_id: str) -> Path:
    return manager.get_project_execution_dir(project_id) / "repo"


def _is_repo(project_id: str) -> bool:
    return (_repo_dir(project_id) / ".git").exists()


def _temp_index_path(project_id: str) -> Path:
    # Lives in the workspace dir (sibling of repo/), never inside the tree.
    return manager.get_project_execution_dir(project_id) / ".agentos_checkpoint.index"


def _rm(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _short(res) -> str:
    msg = (res.error or res.output or "").strip()
    if not msg:
        msg = f"git failed (exit {res.metadata.get('exit_code')})"
    return msg[:400]


def _head_sha(rt: ToolRuntime) -> Optional[str]:
    res = rt.run_git(["rev-parse", "HEAD"])
    if res.success and res.output.strip():
        return res.output.strip()
    return None


def _looks_secret(path: str) -> bool:
    base = path.replace("\\", "/").split("/")[-1]
    low = base.lower()
    if low in _SECRET_BASENAMES:
        return True
    if low.endswith(_SECRET_SUFFIXES):
        return True
    if low == ".env" or (low.startswith(".env") and not low.endswith(".example")):
        return True
    # Phase 8 — the Supabase CLI persists linked-project state (and can persist
    # secrets) under repo/supabase/{.temp,.branches,config.toml}; never stage them.
    norm = path.replace("\\", "/").lower()
    if "supabase/.temp/" in norm or "supabase/.branches/" in norm:
        return True
    if norm.endswith("supabase/config.toml"):
        return True
    return False


def _parse_status_paths(porcelain: str) -> list[str]:
    """Parse `git status --porcelain` lines into repo-relative paths.

    Handles renames (`R  old -> new` → new). quotepath is disabled in
    ensure_repo so non-ASCII paths are not octal-escaped/quoted.
    """
    paths: list[str] = []
    for line in (porcelain or "").splitlines():
        if len(line) < 4:
            continue
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip().strip('"')
        if rest:
            paths.append(rest)
    return paths


_DEFAULT_GITIGNORE = """\
# Agent OS — default ignore. Keeps secrets and heavy build output out of Git.
node_modules/
dist/
build/
.next/
out/
coverage/
.venv/
venv/
__pycache__/
*.pyc
*.log
# Secrets — never commit these.
.env
.env.*
!.env.example
*.key
*.pem
*.pfx
*.p12
id_rsa
.netrc
# Supabase CLI local state (may hold linked-project secrets)
supabase/.temp/
supabase/.branches/
"""


def _ensure_gitignore(repo: Path) -> None:
    gi = repo / ".gitignore"
    if not gi.exists():
        try:
            gi.write_text(_DEFAULT_GITIGNORE, encoding="utf-8")
        except OSError:
            pass


def _configure(rt: ToolRuntime) -> None:
    # Local, repo-scoped config. Identity lets Agent OS author checkpoint/commit
    # objects; the rest make Git deterministic + non-interactive.
    for key, val in (
        ("user.email", "agent-os@local"),
        ("user.name", "Agent OS"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", "false"),
        ("core.quotepath", "false"),
        ("advice.detachedHead", "false"),
        ("gc.auto", "0"),
    ):
        rt.run_git(["config", key, val])


def _ensure_initial_commit(rt: ToolRuntime) -> None:
    if _head_sha(rt) is None:
        rt.run_git(["add", "-A"])
        rt.run_git(["commit", "-m", "Initial commit (Agent OS)", "--allow-empty"])


# ---------- public API ----------


def ensure_repo(project_id: str, *, runtime: Optional[ToolRuntime] = None) -> GitStatus:
    """Idempotently make ``repo/`` a usable git repo: ``git init`` if needed, a
    safe ``.gitignore``, repo-scoped identity/config, and at least one commit so
    HEAD always exists. Never raises."""
    rt = runtime or ToolRuntime(project_id)
    repo = _repo_dir(project_id)
    try:
        if not (repo / ".git").exists():
            init = rt.run_git(["init", "-b", _DEFAULT_BRANCH])
            if not init.success:
                init = rt.run_git(["init"])
                if not init.success:
                    return GitStatus(is_repo=False, error=_short(init))
        _configure(rt)
        _ensure_gitignore(repo)
        _ensure_initial_commit(rt)
        return git_status(project_id, runtime=rt)
    except Exception as e:  # noqa: BLE001
        log.warning("ensure_repo failed for %s: %r", project_id, e)
        return GitStatus(is_repo=False, error=f"{type(e).__name__}: {e}")


def git_status(project_id: str, *, runtime: Optional[ToolRuntime] = None) -> GitStatus:
    rt = runtime or ToolRuntime(project_id)
    if not _is_repo(project_id):
        return GitStatus(is_repo=False)
    branch = current_branch(project_id, runtime=rt)
    head = _head_sha(rt)
    porcelain = rt.run_git(["status", "--porcelain"])
    untracked = modified = staged = 0
    if porcelain.success:
        for line in porcelain.output.splitlines():
            if not line.strip():
                continue
            x, y = line[0], line[1]
            if x == "?" and y == "?":
                untracked += 1
                continue
            if x not in (" ", "?"):
                staged += 1
            if y not in (" ", "?"):
                modified += 1
    return GitStatus(
        is_repo=True,
        branch=branch,
        dirty=(untracked + modified + staged) > 0,
        untracked=untracked,
        modified=modified,
        staged=staged,
        head=head,
    )


def current_branch(project_id: str, *, runtime: Optional[ToolRuntime] = None) -> Optional[str]:
    rt = runtime or ToolRuntime(project_id)
    res = rt.run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    if res.success:
        name = res.output.strip()
        return name or None
    return None


# Field/record separators that cannot appear in a commit subject/author, so we
# can split a `git log` line safely without a delimiter collision.
_LOG_FS = "\x1f"  # between fields
_LOG_RS = "\x1e"  # between records
_MAX_LOG_COMMITS = 200


def list_commits(
    project_id: str, *, limit: int = 50, runtime: Optional[ToolRuntime] = None
) -> list[dict]:
    """Read-only commit history for the project repo, newest first.

    Mirrors ``git_status`` (single sandboxed ``run_git`` executor, output already
    hard-bounded there). Each commit's author + subject is run through
    ``_redact`` before it leaves the module. ``limit`` is clamped to
    ``_MAX_LOG_COMMITS``. Never raises — a non-repo or a git failure yields ``[]``.
    """
    rt = runtime or ToolRuntime(project_id)
    if not _is_repo(project_id):
        return []
    n = max(1, min(int(limit or 0) or 50, _MAX_LOG_COMMITS))
    pretty = _LOG_FS.join(["%H", "%h", "%an", "%ad", "%s"]) + _LOG_RS
    res = rt.run_git(
        ["log", f"-n{n}", "--date=iso", f"--pretty=format:{pretty}"]
    )
    if not res.success:
        return []
    commits: list[dict] = []
    for record in res.output.split(_LOG_RS):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split(_LOG_FS)
        if len(parts) < 5:
            continue
        full, short, author, date, subject = parts[0], parts[1], parts[2], parts[3], parts[4]
        commits.append(
            {
                "hash": full.strip(),
                "short": short.strip(),
                "author": _redact(author.strip()),
                "date": date.strip(),
                "subject": _redact(subject.strip()),
            }
        )
    return commits


def create_checkpoint(
    project_id: str, label: str, *, runtime: Optional[ToolRuntime] = None
) -> CheckpointResult:
    """Capture the current repo state as a restorable, *out-of-branch* commit
    object (tagged ``agentos-checkpoint-<label>``). The working branch is never
    advanced; the snapshot includes the full working tree (gitignore-respecting)
    via a throwaway index, so a dirty tree is captured without disturbing it."""
    rt = runtime or ToolRuntime(project_id)
    st = ensure_repo(project_id, runtime=rt)
    if not st.is_repo:
        return CheckpointResult(error=st.error or "not a git repo")
    base = _head_sha(rt)
    idx = _temp_index_path(project_id)
    env = {"GIT_INDEX_FILE": str(idx)}
    try:
        add = rt.run_git(["add", "-A"], env_extra=env)
        if not add.success:
            return CheckpointResult(base_commit=base, error=_short(add))
        tree = rt.run_git(["write-tree"], env_extra=env)
        if not tree.success or not tree.output.strip():
            return CheckpointResult(base_commit=base, error=_short(tree))
        tree_sha = tree.output.strip()
        ct_args = ["commit-tree", tree_sha, "-m", f"Agent OS checkpoint: {label}"]
        if base:
            ct_args = ["commit-tree", tree_sha, "-p", base, "-m",
                       f"Agent OS checkpoint: {label}"]
        ct = rt.run_git(ct_args, env_extra=env)
        if not ct.success or not ct.output.strip():
            return CheckpointResult(base_commit=base, error=_short(ct))
        ckpt = ct.output.strip()
        tag = f"{CHECKPOINT_TAG_PREFIX}{label}"
        rt.run_git(["tag", "-f", tag, ckpt])  # best-effort GC anchor
        return CheckpointResult(created=True, ref=ckpt, base_commit=base, tag=tag)
    except Exception as e:  # noqa: BLE001
        log.warning("create_checkpoint failed for %s: %r", project_id, e)
        return CheckpointResult(base_commit=base, error=f"{type(e).__name__}: {e}")
    finally:
        _rm(idx)


def capture_diff(
    project_id: str,
    since_ref: str,
    *,
    runtime: Optional[ToolRuntime] = None,
    redactor: Optional[Callable[[str], str]] = None,
    max_chars: int = _DIFF_MAX_CHARS,
) -> DiffResult:
    """Diff the full current working tree (incl. new untracked files) against
    ``since_ref`` (typically a checkpoint commit). Output is redacted and
    bounded. Uses a throwaway index so the real index is untouched."""
    rt = runtime or ToolRuntime(project_id)
    if not _is_repo(project_id):
        return DiffResult(error="not a git repo")
    redact = redactor or _redact
    idx = _temp_index_path(project_id)
    env = {"GIT_INDEX_FILE": str(idx)}
    try:
        add = rt.run_git(["add", "-A"], env_extra=env)
        if not add.success:
            return DiffResult(error=_short(add))
        names = rt.run_git(["diff", "--cached", "--name-only", since_ref], env_extra=env)
        stat = rt.run_git(["diff", "--cached", "--stat", since_ref], env_extra=env)
        patch = rt.run_git(["diff", "--cached", since_ref], env_extra=env, timeout_seconds=90)
        files = [l.strip() for l in (names.output or "").splitlines() if l.strip()]
        text = redact(patch.output or "")
        truncated = False
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [diff truncated at {max_chars} chars] ..."
            truncated = True
        return DiffResult(
            captured=True,
            diff_text=text,
            stat=redact((stat.output or "").strip()),
            files=files,
            truncated=truncated,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("capture_diff failed for %s: %r", project_id, e)
        return DiffResult(error=f"{type(e).__name__}: {e}")
    finally:
        _rm(idx)


def list_changes(project_id: str, *, runtime: Optional[ToolRuntime] = None) -> list[str]:
    """Repo-relative paths with uncommitted changes (gitignore-respecting)."""
    rt = runtime or ToolRuntime(project_id)
    res = rt.run_git(["status", "--porcelain"])
    return _parse_status_paths(res.output) if res.success else []


def partition_changes(
    project_id: str, *, runtime: Optional[ToolRuntime] = None
) -> tuple[list[str], list[str]]:
    """Split changed paths into (committable, refused-as-secret)."""
    paths = list_changes(project_id, runtime=runtime)
    safe = [p for p in paths if not _looks_secret(p)]
    refused = [p for p in paths if _looks_secret(p)]
    return safe, refused


def create_branch(
    project_id: str, name: str, *, runtime: Optional[ToolRuntime] = None
) -> tuple[bool, str]:
    """Switch to ``name``, creating it if it doesn't exist. Returns (ok, error)."""
    rt = runtime or ToolRuntime(project_id)
    ensure_repo(project_id, runtime=rt)
    exists = rt.run_git(["rev-parse", "--verify", name])
    if exists.success:
        res = rt.run_git(["checkout", name])
    else:
        res = rt.run_git(["checkout", "-b", name])
    return (res.success, "" if res.success else _short(res))


def commit(
    project_id: str,
    message: str,
    *,
    paths: Optional[list[str]] = None,
    allow_empty: bool = False,
    runtime: Optional[ToolRuntime] = None,
) -> CommitResult:
    """Stage and commit. Secret-looking files are *refused* (never staged) even
    if .gitignore missed them — the durable guarantee behind ``_ensure_gitignore``.
    """
    rt = runtime or ToolRuntime(project_id)
    st = ensure_repo(project_id, runtime=rt)
    if not st.is_repo:
        return CommitResult(message=message, error=st.error or "not a git repo")

    porcelain = rt.run_git(["status", "--porcelain"])
    changed = _parse_status_paths(porcelain.output) if porcelain.success else []
    refused = [p for p in changed if _looks_secret(p)]
    safe = [p for p in changed if not _looks_secret(p)]
    if paths is not None:
        req = set(paths)
        safe = [p for p in safe if p in req]

    if not safe and not allow_empty:
        return CommitResult(
            branch=st.branch, message=message, refused=refused,
            error="nothing to commit",
        )
    if safe:
        add = rt.run_git(["add", "--", *safe])
        if not add.success:
            return CommitResult(
                branch=st.branch, message=message, refused=refused,
                error=_short(add),
            )
    cargs = ["commit", "-m", message]
    if allow_empty:
        cargs.append("--allow-empty")
    c = rt.run_git(cargs)
    if not c.success:
        return CommitResult(
            branch=st.branch, message=message, refused=refused, error=_short(c)
        )
    return CommitResult(
        committed=True,
        sha=_head_sha(rt),
        branch=current_branch(project_id, runtime=rt),
        message=message,
        files=safe,
        refused=refused,
    )


def rollback(
    project_id: str,
    *,
    base_commit: str,
    checkpoint_ref: Optional[str] = None,
    runtime: Optional[ToolRuntime] = None,
) -> RollbackResult:
    """Restore the repo to its pre-run state (destructive — caller must have a
    user confirmation). Resets the branch to ``base_commit``, removes the run's
    untracked files, then re-applies the checkpoint snapshot's working-tree edits
    (if any). All steps pass ``allow_destructive=True``."""
    rt = runtime or ToolRuntime(project_id)
    if not _is_repo(project_id):
        return RollbackResult(error="not a git repo")
    reset = rt.run_git(["reset", "--hard", base_commit], allow_destructive=True)
    if not reset.success:
        return RollbackResult(target=base_commit, error=_short(reset))
    rt.run_git(["clean", "-fd"], allow_destructive=True)  # remove run's new files
    if checkpoint_ref and checkpoint_ref != base_commit:
        # bring back any pre-run *uncommitted* edits captured by the snapshot
        rt.run_git(["checkout", checkpoint_ref, "--", "."], allow_destructive=True)
    return RollbackResult(rolled_back=True, target=base_commit)
