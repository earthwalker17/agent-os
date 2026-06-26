"""Phase 7 — central credential accessor (Project Ops).

The single place secret VALUES are read. Global tokens come from the
environment (like provider keys); project-scoped tokens live in a gitignored
on-disk store OUTSIDE ``projects/`` and ``memory/`` (so a secret can never be
swept into memory writeback). Resolution order: a **project** token overrides
the **global file**, which overrides the **environment**.

Constitutional rules (BREAK-3):
- A secret value leaves this module ONLY via ``get_github_token`` (consumed by
  the GitHub connector's env-injection at push time). ``status`` returns
  presence + non-secret metadata — never the value.
- ``redact`` scrubs known stored token values + common token shapes from any
  string before it can reach a prompt, log, event, result.md, memory, or the UI.
- Nothing here writes into the repo, run.json, events.jsonl, or memory. The
  store lives under ``<root>/credentials/`` which ``.gitignore`` excludes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# <repo-root>/credentials/ — sibling to projects/, memory/, execution_workspaces/.
_ROOT = Path(__file__).resolve().parent.parent
_CRED_DIR = _ROOT / "credentials"
_PROJECTS_DIR = _CRED_DIR / "projects"
_GLOBAL_FILE = _CRED_DIR / "global.json"

_GITHUB_ENV_VARS = ("GITHUB_TOKEN", "AGENT_OS_GITHUB_TOKEN", "GH_TOKEN")

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_project_id(project_id: str) -> str:
    pid = _SAFE_ID.sub("_", (project_id or "").strip())
    if not pid or pid in (".", ".."):
        raise ValueError("invalid project id")
    return pid


def _project_file(project_id: str) -> Path:
    return _PROJECTS_DIR / f"{_safe_project_id(project_id)}.json"


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        log.warning("could not read credential store at %s", path)
    return {}


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------- env / store records ----------


def _env_github_token() -> Optional[str]:
    for var in _GITHUB_ENV_VARS:
        v = os.environ.get(var)
        if v and v.strip():
            return v.strip()
    return None


def _global_record(provider: str = "github") -> dict:
    return _read_json(_GLOBAL_FILE).get(provider, {}) or {}


def _project_record(project_id: Optional[str], provider: str = "github") -> dict:
    if not project_id:
        return {}
    try:
        return _read_json(_project_file(project_id)).get(provider, {}) or {}
    except ValueError:
        return {}


# ---------- value reader (connector only) ----------


def get_github_token(project_id: Optional[str] = None) -> Optional[str]:
    """Resolve the GitHub token for a project (project store → global file →
    env). Returns ``None`` if none is set. The ONLY value-returning accessor;
    callers MUST keep the result out of logs, argv, commits, and memory."""
    if project_id:
        tok = _project_record(project_id, "github").get("token")
        if tok and tok.strip():
            return tok.strip()
    gtok = _global_record("github").get("token")
    if gtok and gtok.strip():
        return gtok.strip()
    return _env_github_token()


def _token_source(project_id: Optional[str]) -> str:
    if project_id and _project_record(project_id, "github").get("token"):
        return "project"
    if _global_record("github").get("token"):
        return "global_file"
    if _env_github_token():
        return "env"
    return "none"


# ---------- presence-only status ----------


def status(project_id: Optional[str] = None) -> dict:
    """Presence + non-secret metadata for the GitHub credential. NEVER the
    token value."""
    source = _token_source(project_id)
    configured = source != "none"
    rec = (
        _project_record(project_id, "github")
        if source == "project"
        else _global_record("github")
    )
    return {
        "provider": "github",
        "configured": configured,
        "source": source,  # project | global_file | env | none
        "scope": "project" if source == "project" else ("global" if configured else "none"),
        "login": rec.get("login"),  # filled by the connector after validation
        "default_remote": rec.get("default_remote"),
    }


# ---------- writers ----------


def set_github_credential(
    project_id: Optional[str],
    *,
    token: str,
    login: Optional[str] = None,
    default_remote: Optional[str] = None,
    scope: str = "project",
) -> dict:
    """Store a GitHub token in the gitignored credential store. ``scope`` is
    ``"project"`` (per-project file) or ``"global"`` (shared file). Returns the
    presence ``status`` — never the value."""
    token = (token or "").strip()
    if not token:
        raise ValueError("token must be non-empty")
    rec: dict = {"token": token}
    if login:
        rec["login"] = login
    if default_remote:
        rec["default_remote"] = default_remote
    if scope == "global":
        data = _read_json(_GLOBAL_FILE)
        data["github"] = {**data.get("github", {}), **rec}
        _atomic_write(_GLOBAL_FILE, data)
    else:
        if not project_id:
            raise ValueError("project scope requires a project_id")
        path = _project_file(project_id)
        data = _read_json(path)
        data["github"] = {**data.get("github", {}), **rec}
        _atomic_write(path, data)
    return status(project_id)


def update_github_metadata(
    project_id: Optional[str],
    *,
    login: Optional[str] = None,
    default_remote: Optional[str] = None,
) -> None:
    """Merge non-secret metadata (login / default_remote) onto an existing
    stored record. No-op if no on-disk token exists for that scope (e.g. an
    env-only token, where there is no file to annotate)."""
    updates = {k: v for k, v in (("login", login), ("default_remote", default_remote)) if v}
    if not updates:
        return
    src = _token_source(project_id)
    try:
        if src == "project" and project_id:
            path = _project_file(project_id)
            data = _read_json(path)
            data["github"] = {**data.get("github", {}), **updates}
            _atomic_write(path, data)
        elif src == "global_file":
            data = _read_json(_GLOBAL_FILE)
            data["github"] = {**data.get("github", {}), **updates}
            _atomic_write(_GLOBAL_FILE, data)
    except (OSError, ValueError):
        log.warning("could not update github metadata for %s", project_id)


def delete_github_credential(project_id: Optional[str] = None, *, scope: str = "project") -> dict:
    """Remove a stored GitHub token. Cannot remove an env-provided token (that
    is the operator's to unset)."""
    try:
        if scope == "global":
            data = _read_json(_GLOBAL_FILE)
            data.pop("github", None)
            _atomic_write(_GLOBAL_FILE, data)
        elif project_id:
            path = _project_file(project_id)
            data = _read_json(path)
            data.pop("github", None)
            _atomic_write(path, data)
    except (OSError, ValueError):
        log.warning("could not delete github credential for %s", project_id)
    return status(project_id)


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


def _all_known_tokens(project_id: Optional[str]) -> set[str]:
    toks: set[str] = set()
    for t in (
        _env_github_token(),
        _global_record("github").get("token"),
        _project_record(project_id, "github").get("token") if project_id else None,
    ):
        if t and t.strip():
            toks.add(t.strip())
    return toks


def redact(text: str, project_id: Optional[str] = None) -> str:
    """Scrub known stored token values + common secret shapes from ``text``.
    The strongest egress guard — call it before any string that might contain a
    credential reaches a log/prompt/diff/UI."""
    if not text:
        return text
    out = text
    for tok in _all_known_tokens(project_id):
        out = out.replace(tok, "[REDACTED]")
    out = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", out)
    out = _KV_SECRET_RE.sub(r"\1[REDACTED]", out)
    for pat in _TOKEN_RES:
        out = pat.sub("[REDACTED]", out)
    return out
