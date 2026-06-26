"""Phase 7 — GitHub connector (Project Ops external delivery).

GitHub is reached two ways, both leak-proof:

- **Push** uses git over HTTPS. The token is injected ONLY into the subprocess
  environment via ``GIT_ASKPASS`` (a tiny generated helper that echoes the token
  from an env var). It never appears in the argv (which ``run_git`` logs to
  events/run.json), never in ``.git/config`` (the remote URL is tokenless), and
  the git error output is redacted before it is returned.
- **Pull requests / token validation** use the GitHub REST API over ``urllib``
  (no new dependency; mirrors ``providers.py``). The token rides in the
  ``Authorization`` header, never logged.

The connector reads the token through ``credentials.get_github_token`` (the only
value accessor) and never returns, stores, or logs the value itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import credentials

from .tool_runtime import ToolRuntime

log = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
_HTTP_TIMEOUT = 30
_PUSH_TIMEOUT = 180

# The GIT_ASKPASS helper lives in the gitignored credentials/ dir (outside the
# repo). It carries NO secret — it only echoes values from the environment the
# connector sets per-push. Written with LF endings + an exec bit so git's
# shebang handling runs it on POSIX and Git-for-Windows alike.
_ASKPASS_BODY = (
    "#!/bin/sh\n"
    'case "$1" in\n'
    "  *[Uu]sername*) printf '%s' \"${AGENT_OS_GIT_USERNAME:-x-access-token}\" ;;\n"
    '  *) printf \'%s\' "${AGENT_OS_GIT_PASSWORD}" ;;\n'
    "esac\n"
)


class ConnectorError(Exception):
    """Network / API failure talking to GitHub."""


@dataclass
class PushResult:
    ok: bool = False
    branch: Optional[str] = None
    remote: Optional[str] = None
    error: Optional[str] = None


@dataclass
class PullRequestResult:
    ok: bool = False
    url: Optional[str] = None
    number: Optional[int] = None
    error: Optional[str] = None


@dataclass
class ConnectorStatus:
    configured: bool = False
    connected: bool = False
    scope: str = "none"
    source: str = "none"
    login: Optional[str] = None
    default_remote: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "provider": "github",
            "configured": self.configured,
            "connected": self.connected,
            "scope": self.scope,
            "source": self.source,
            "login": self.login,
            "default_remote": self.default_remote,
            "error": self.error,
        }


# ---------- REST helper ----------


def _github_api(
    method: str,
    path: str,
    token: str,
    payload: Optional[dict] = None,
    *,
    base: str = _API_BASE,
    opener: Optional[Callable] = None,
) -> tuple[int, dict]:
    """Call the GitHub REST API. Returns (status_code, parsed_json). The token
    rides in the Authorization header only. ``opener`` is injectable for tests."""
    url = base + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "Agent-OS")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    _open = opener or urllib.request.urlopen
    try:
        with _open(req, timeout=_HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            code = resp.getcode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"message": raw[:300]}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise ConnectorError(f"network error calling GitHub: {exc.reason}")
    if not body:
        return code, {}
    try:
        return code, json.loads(body)
    except json.JSONDecodeError:
        raise ConnectorError(f"non-JSON response from GitHub: {body[:200]}")


# ---------- remote parsing ----------

_REMOTE_RE = re.compile(
    r"github\.com[:/]+(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?\s*$",
    re.IGNORECASE,
)


def parse_remote_url(url: str) -> Optional[tuple[str, str]]:
    if not url:
        return None
    m = _REMOTE_RE.search(url.strip())
    if not m:
        return None
    return m.group("owner"), m.group("repo")


def get_remote(project_id: str, *, remote: str = "origin", runtime: Optional[ToolRuntime] = None) -> Optional[tuple[str, str]]:
    rt = runtime or ToolRuntime(project_id)
    res = rt.run_git(["remote", "get-url", remote])
    if not res.success:
        return None
    return parse_remote_url(res.output.strip())


def ensure_remote(
    project_id: str,
    owner: str,
    repo: str,
    *,
    remote: str = "origin",
    runtime: Optional[ToolRuntime] = None,
) -> tuple[bool, str]:
    """Point ``remote`` at a TOKENLESS GitHub URL (idempotent). Returns
    (ok, url-or-error). The token is never written to .git/config."""
    rt = runtime or ToolRuntime(project_id)
    repo = repo[:-4] if repo.endswith(".git") else repo
    url = f"https://github.com/{owner}/{repo}.git"
    existing = rt.run_git(["remote", "get-url", remote])
    if existing.success:
        res = rt.run_git(["remote", "set-url", remote, url])
    else:
        res = rt.run_git(["remote", "add", remote, url])
    if not res.success:
        return False, credentials.redact((res.error or res.output or "").strip(), project_id)
    return True, url


# ---------- askpass ----------


def _askpass_path() -> Path:
    return credentials._CRED_DIR / "_git_askpass.sh"


def _ensure_askpass_script() -> str:
    path = _askpass_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write with LF endings (newline="") so the shebang survives on Windows.
    if not path.exists() or path.read_text(encoding="utf-8") != _ASKPASS_BODY:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(_ASKPASS_BODY)
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
    return str(path)


# ---------- operations ----------


def status(project_id: Optional[str] = None, *, api: Optional[Callable] = None) -> ConnectorStatus:
    """Presence + live connectivity. Validates the token via ``GET /user`` when
    one is configured. Never returns the token value."""
    cred = credentials.status(project_id)
    st = ConnectorStatus(
        configured=cred["configured"],
        scope=cred["scope"],
        source=cred["source"],
        login=cred.get("login"),
        default_remote=cred.get("default_remote"),
    )
    if not cred["configured"]:
        return st
    token = credentials.get_github_token(project_id)
    try:
        code, data = (api or _github_api)("GET", "/user", token)
    except ConnectorError as e:
        st.error = credentials.redact(str(e), project_id)
        return st
    if code == 200:
        st.connected = True
        st.login = data.get("login")
        if st.login:
            credentials.update_github_metadata(project_id, login=st.login)
    else:
        st.error = credentials.redact(str(data.get("message", f"HTTP {code}")), project_id)
    return st


def push_branch(
    project_id: str,
    branch: str,
    *,
    remote: str = "origin",
    set_upstream: bool = True,
    runtime: Optional[ToolRuntime] = None,
    token: Optional[str] = None,
) -> PushResult:
    """Push ``branch`` to ``remote`` using a token injected ONLY via the
    environment (GIT_ASKPASS). The argv carries no secret; the remote is
    tokenless; git's output is redacted before return."""
    rt = runtime or ToolRuntime(project_id)
    token = token or credentials.get_github_token(project_id)
    if not token:
        return PushResult(error="no GitHub token configured")
    env = {
        "GIT_ASKPASS": _ensure_askpass_script(),
        "AGENT_OS_GIT_USERNAME": "x-access-token",
        "AGENT_OS_GIT_PASSWORD": token,
        "GIT_TERMINAL_PROMPT": "0",
    }
    args = ["push"]
    if set_upstream:
        args.append("-u")
    args += [remote, branch]
    res = rt.run_git(args, env_extra=env, timeout_seconds=_PUSH_TIMEOUT)
    if not res.success:
        return PushResult(
            error=credentials.redact((res.error or res.output or "push failed").strip(), project_id)
        )
    return PushResult(ok=True, branch=branch, remote=remote)


def create_pull_request(
    project_id: str,
    *,
    owner: str,
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str = "",
    draft: bool = False,
    token: Optional[str] = None,
    api: Optional[Callable] = None,
) -> PullRequestResult:
    """Open a PR via the REST API. The token rides in the Authorization header.
    ``api`` is injectable for tests."""
    token = token or credentials.get_github_token(project_id)
    if not token:
        return PullRequestResult(error="no GitHub token configured")
    payload = {"title": title, "head": head, "base": base, "body": body, "draft": draft}
    try:
        code, data = (api or _github_api)(
            "POST", f"/repos/{owner}/{repo}/pulls", token, payload
        )
    except ConnectorError as e:
        return PullRequestResult(error=credentials.redact(str(e), project_id))
    if code in (200, 201):
        return PullRequestResult(
            ok=True, url=data.get("html_url"), number=data.get("number")
        )
    msg = data.get("message", f"HTTP {code}")
    errs = data.get("errors")
    if errs:
        msg = f"{msg}: {errs}"
    return PullRequestResult(error=credentials.redact(str(msg), project_id))
