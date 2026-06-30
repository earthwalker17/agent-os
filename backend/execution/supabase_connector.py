"""Phase 8 — Supabase connector (Production Path: Postgres / migrations / Auth).

Supabase is reached two ways, both leak-proof:

- **Migrations / local stack / link** go through the sandboxed CLI executor
  ``ToolRuntime.run_supabase`` (the analogue of ``run_git``). Secrets ride ONLY
  in the executor's ``env_extra`` (``SUPABASE_ACCESS_TOKEN`` /
  ``SUPABASE_DB_PASSWORD``) — never argv, never ``supabase/config.toml``. The CLI
  runs with a scrubbed env (Agent OS's own provider keys are withheld). Every
  returned stdout/stderr is ``credentials.redact``-ed BEFORE it reaches a
  caller / artifact (the connector redacts, not the run_store).
- **Status / project metadata / Auth config** use the Management API over
  ``urllib`` (Bearer ``sbp_…`` access token, no new dependency).

Docker note: ``db push --dry-run`` (the migration preview's required part) and
``db push`` (apply) are Docker-optional; ``db diff`` needs a Docker shadow DB, so
it is best-effort enrichment only. A missing/stopped Docker daemon surfaces as a
clear blocker, never an opaque failure.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional

import credentials

from .tool_runtime import ToolRuntime

log = logging.getLogger(__name__)

_API_BASE = "https://api.supabase.com"
_HTTP_TIMEOUT = 30


class ConnectorError(Exception):
    """Network / API failure talking to Supabase."""


@dataclass
class SupabaseStatus:
    configured: bool = False
    connected: bool = False
    scope: str = "none"
    source: str = "none"
    project_ref: Optional[str] = None
    url: Optional[str] = None
    has_db_password: bool = False
    has_service_role: bool = False
    docker_required_for: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "provider": "supabase",
            "configured": self.configured,
            "connected": self.connected,
            "scope": self.scope,
            "source": self.source,
            "project_ref": self.project_ref,
            "url": self.url,
            "linked": bool(self.project_ref),
            "has_db_password": self.has_db_password,
            "has_service_role": self.has_service_role,
            "error": self.error,
        }


@dataclass
class CliResult:
    ok: bool = False
    output: str = ""           # already redacted
    error: Optional[str] = None  # already redacted
    docker_missing: bool = False
    not_installed: bool = False


def _supabase_api(
    method: str,
    path: str,
    token: str,
    payload: Optional[dict] = None,
    *,
    opener: Optional[Callable] = None,
) -> tuple[int, dict]:
    """Call the Supabase Management API. Token in the Authorization header only."""
    url = _API_BASE + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
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
        raise ConnectorError(f"network error calling Supabase: {exc.reason}")
    if not body:
        return code, {}
    try:
        return code, json.loads(body)
    except json.JSONDecodeError:
        raise ConnectorError(f"non-JSON response from Supabase: {body[:200]}")


def _secret_env(project_id: str) -> dict:
    """The CLI's secret env: access token + DB password (never argv/config/logs)."""
    env: dict = {}
    tok = credentials.get_token("supabase", project_id)
    if tok:
        env["SUPABASE_ACCESS_TOKEN"] = tok
    pw = credentials.get_secret("supabase", "db_password", project_id)
    if pw:
        env["SUPABASE_DB_PASSWORD"] = pw
    return env


def _looks_docker_missing(text: str) -> bool:
    t = (text or "").lower()
    return "docker" in t and any(
        s in t for s in ("daemon", "not running", "cannot connect", "is not running", "no such host")
    )


def _redact(text: str, project_id: str) -> str:
    return credentials.redact(text or "", project_id)


def _run(
    project_id: str,
    argv: List[str],
    *,
    allow_destructive: bool = False,
    with_secrets: bool = True,
    runtime: Optional[ToolRuntime] = None,
    timeout_seconds: int = 180,
) -> CliResult:
    """Run a supabase CLI op via the sandboxed executor and return a REDACTED
    result. Secrets are injected via env_extra only."""
    rt = runtime or ToolRuntime(project_id)
    env_extra = _secret_env(project_id) if with_secrets else None
    res = rt.run_supabase(
        argv, allow_destructive=allow_destructive, env_extra=env_extra, timeout_seconds=timeout_seconds
    )
    out = _redact(res.output, project_id)
    err = _redact(res.error or "", project_id)
    not_installed = bool((res.metadata or {}).get("not_installed"))
    docker = _looks_docker_missing(res.output) or _looks_docker_missing(res.error or "")
    return CliResult(ok=res.success, output=out, error=err or None, docker_missing=docker, not_installed=not_installed)


# ---------- operations ----------


def status(project_id: str, *, opener: Optional[Callable] = None) -> SupabaseStatus:
    """Presence + live connectivity (validates the access token via the
    Management API) + linked-project metadata. Never returns a secret value."""
    cred = credentials.status(project_id, "supabase")
    ref = credentials.get_metadata("supabase", "project_ref", project_id)
    st = SupabaseStatus(
        configured=cred["configured"],
        scope=cred["scope"],
        source=cred["source"],
        project_ref=ref,
        url=credentials.get_metadata("supabase", "url", project_id),
        has_db_password=bool(cred.get("secret_fields", {}).get("db_password")),
        has_service_role=bool(cred.get("secret_fields", {}).get("service_role")),
    )
    if not cred["configured"]:
        return st
    token = credentials.get_token("supabase", project_id)
    path = f"/v1/projects/{urllib.parse.quote(ref, safe='')}" if ref else "/v1/projects"
    try:
        code, data = _supabase_api("GET", path, token, opener=opener)
    except ConnectorError as e:
        st.error = _redact(str(e), project_id)
        return st
    if code == 200:
        st.connected = True
    elif code in (401, 403):
        st.error = "Supabase access token rejected (check the token)"
    else:
        msg = data.get("message") if isinstance(data, dict) else None
        st.error = _redact(str(msg or f"HTTP {code}"), project_id)
    return st


def link(project_id: str, project_ref: str, *, runtime: Optional[ToolRuntime] = None) -> CliResult:
    """Bind the local repo to a hosted Supabase project (`supabase link`). The DB
    password rides in env only; the ref is non-secret."""
    if not project_ref:
        return CliResult(error="project_ref is required")
    return _run(project_id, ["link", "--project-ref", project_ref], runtime=runtime)


def migration_preview(project_id: str, *, runtime: Optional[ToolRuntime] = None) -> CliResult:
    """The REQUIRED preview: `db push --dry-run --linked` lists the pending
    migrations that WOULD be applied — no DB mutation, Docker-optional."""
    return _run(project_id, ["db", "push", "--dry-run", "--linked"], runtime=runtime)


def migration_diff(project_id: str, *, runtime: Optional[ToolRuntime] = None) -> CliResult:
    """Best-effort exact-SQL enrichment via `db diff --linked` (needs Docker).
    Caller treats a Docker-missing result as a soft note, not a hard blocker."""
    return _run(project_id, ["db", "diff", "--linked", "--schema", "public"], runtime=runtime)


def migration_apply(
    project_id: str, *, include_seed: bool = False, runtime: Optional[ToolRuntime] = None
) -> CliResult:
    """Apply pending migrations to the LINKED remote DB (`db push --linked`).
    Destructive/external — only ever called from a user-confirmed contract."""
    argv = ["db", "push", "--linked"]
    if include_seed:
        argv.append("--include-seed")
    return _run(project_id, argv, allow_destructive=True, runtime=runtime, timeout_seconds=300)
