"""Phase 8 — Vercel connector (Production Path external delivery).

Vercel is reached entirely over its REST API via ``urllib`` (no new dependency,
no CLI for the deploy/redeploy/rollback/env path — mirrors
``github_connector`` + ``providers.py``). The access token rides ONLY in the
``Authorization: Bearer`` header — never argv, never a log, never an artifact.
Every string returned to a caller is run through ``credentials.redact(text,
project_id)`` first.

The connector reads the token through ``credentials.get_token("vercel", …)``
(the only value accessor) and never returns, stores, or logs the value itself.
It never auto-creates a Vercel project: the project must pre-exist and be linked
(``project_id`` / ``org_id`` stored as non-secret connector metadata); a missing
link surfaces as a clear error, not a silent provision.

Docs verified: create deployment ``POST /v13/deployments`` (always sends
``name``; ``gitSource``+``ref`` for a git-connected project; ``deploymentId`` to
redeploy), poll ``GET /v13/deployments/{idOrUrl}`` (``readyState`` ∈
QUEUED/INITIALIZING/BUILDING/READY/ERROR/CANCELED), env
``POST /v10/projects/{id}/env?upsert=true`` (``type:"sensitive"`` write-only),
``DELETE /v9/projects/{id}/env/{envId}``, rollback via promote
``POST /v10/projects/{id}/promote/{deploymentId}``.
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

log = logging.getLogger(__name__)

_API_BASE = "https://api.vercel.com"
_HTTP_TIMEOUT = 30

READY_STATES = ("READY",)
TERMINAL_STATES = ("READY", "ERROR", "CANCELED", "DELETED")


class ConnectorError(Exception):
    """Network / API failure talking to Vercel."""


@dataclass
class VercelStatus:
    configured: bool = False
    connected: bool = False
    scope: str = "none"
    source: str = "none"
    username: Optional[str] = None
    org_id: Optional[str] = None
    project_id: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "provider": "vercel",
            "configured": self.configured,
            "connected": self.connected,
            "scope": self.scope,
            "source": self.source,
            "username": self.username,
            "org_id": self.org_id,
            "project_id": self.project_id,
            "linked": bool(self.project_id),
            "error": self.error,
        }


@dataclass
class DeploymentResult:
    ok: bool = False
    deployment_id: Optional[str] = None
    url: Optional[str] = None
    ready_state: Optional[str] = None
    target: Optional[str] = None
    error: Optional[str] = None


@dataclass
class EnvVarResult:
    ok: bool = False
    env_id: Optional[str] = None
    key: Optional[str] = None
    error: Optional[str] = None


# ---------- REST helper ----------


def _vercel_api(
    method: str,
    path: str,
    token: str,
    payload: Optional[dict] = None,
    *,
    query: Optional[dict] = None,
    base: str = _API_BASE,
    opener: Optional[Callable] = None,
) -> tuple[int, dict]:
    """Call the Vercel REST API. Returns (status_code, parsed_json). The token
    rides in the Authorization header only. ``opener`` is injectable for tests."""
    url = base + path
    if query:
        clean = {k: v for k, v in query.items() if v}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
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
            parsed = {"error": {"message": raw[:300]}}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise ConnectorError(f"network error calling Vercel: {exc.reason}")
    if not body:
        return code, {}
    try:
        return code, json.loads(body)
    except json.JSONDecodeError:
        raise ConnectorError(f"non-JSON response from Vercel: {body[:200]}")


def _err_message(data: dict, code: int) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    return str(data.get("message", f"HTTP {code}"))


def _team_query(project_id: Optional[str]) -> dict:
    """Vercel scopes team resources via ``?teamId=``. We store the team/org id as
    non-secret connector metadata (``org_id``)."""
    org = credentials.get_metadata("vercel", "org_id", project_id)
    return {"teamId": org} if org else {}


def normalize_url(url: Optional[str]) -> Optional[str]:
    """Return a bare ``scheme://host/path`` URL. Vercel returns ``url`` as a bare
    host (``app-xyz.vercel.app``); add https. Strips any query/fragment (e.g. a
    ``?x-vercel-protection-bypass=<secret>`` token) so it is safe to surface."""
    if not url:
        return None
    u = url.strip()
    if not u:
        return None
    if not u.startswith("http://") and not u.startswith("https://"):
        u = "https://" + u
    u = u.split("?", 1)[0].split("#", 1)[0]
    return u.rstrip("/")


# ---------- operations ----------


def status(project_id: Optional[str] = None, *, opener: Optional[Callable] = None) -> VercelStatus:
    """Presence + live connectivity (validates the token via ``GET /v2/user``)
    + linked-project metadata. Never returns the token value."""
    cred = credentials.status(project_id, "vercel")
    st = VercelStatus(
        configured=cred["configured"],
        scope=cred["scope"],
        source=cred["source"],
        org_id=credentials.get_metadata("vercel", "org_id", project_id),
        project_id=credentials.get_metadata("vercel", "project_id", project_id),
    )
    if not cred["configured"]:
        return st
    token = credentials.get_token("vercel", project_id)
    try:
        code, data = _vercel_api("GET", "/v2/user", token, opener=opener)
    except ConnectorError as e:
        st.error = credentials.redact(str(e), project_id)
        return st
    if code == 200:
        st.connected = True
        user = data.get("user") if isinstance(data.get("user"), dict) else data
        st.username = user.get("username") or user.get("name")
        if st.username:
            credentials.update_metadata("vercel", project_id, {"username": st.username})
    else:
        st.error = credentials.redact(_err_message(data, code), project_id)
    return st


def get_project_link(project_id: str, *, opener: Optional[Callable] = None) -> Optional[dict]:
    """The project's connected-git `link` (type/org/repo/repoId/productionBranch).
    Authoritative source for building a gitSource deploy. ``None`` if unlinked /
    unreachable."""
    token = credentials.get_token("vercel", project_id)
    proj = credentials.get_metadata("vercel", "project_id", project_id)
    if not token or not proj:
        return None
    try:
        code, data = _vercel_api(
            "GET",
            f"/v9/projects/{urllib.parse.quote(proj, safe='')}",
            token,
            query=_team_query(project_id),
            opener=opener,
        )
    except ConnectorError:
        return None
    link = data.get("link") if isinstance(data, dict) else None
    return link if isinstance(link, dict) else None


def _git_source(project_id: str, git_ref: str, *, opener: Optional[Callable] = None) -> Optional[dict]:
    """Build the `gitSource` body for a git-connected project. Vercel requires
    `repoId` (preferred) or `org`+`repo` — a bare `{type, ref}` is rejected."""
    link = get_project_link(project_id, opener=opener)
    if not link:
        return None
    if link.get("repoId") is not None:
        return {"type": "github", "ref": git_ref, "repoId": link["repoId"]}
    if link.get("org") and link.get("repo"):
        return {"type": "github", "ref": git_ref, "org": link["org"], "repo": link["repo"]}
    return None


def create_deployment(
    project_id: str,
    *,
    name: str,
    target: str = "preview",
    git_ref: Optional[str] = None,
    deployment_id: Optional[str] = None,
    with_latest_commit: bool = False,
    opener: Optional[Callable] = None,
) -> DeploymentResult:
    """Create a Vercel deployment for a git-connected project. ``git_ref``
    deploys that branch via gitSource (repoId resolved from the project link);
    ``deployment_id`` redeploys an existing deployment. ``target`` is sent only
    for production (preview is Vercel's default when omitted). Returns
    ids/url/readyState — no secret ever rides argv/log."""
    token = credentials.get_token("vercel", project_id)
    if not token:
        return DeploymentResult(error="no Vercel token configured")
    payload: dict = {"name": name}
    if target and target != "preview":
        payload["target"] = target  # 'production'/'staging'; preview is the default
    proj = credentials.get_metadata("vercel", "project_id", project_id)
    if proj:
        payload["project"] = proj  # overrides `name`
    if deployment_id:
        payload["deploymentId"] = deployment_id
        payload["withLatestCommit"] = bool(with_latest_commit)
    elif git_ref:
        gs = _git_source(project_id, git_ref, opener=opener)
        if gs is None:
            return DeploymentResult(
                error="could not resolve the Vercel project's connected git repo (link it first)"
            )
        payload["gitSource"] = gs
    # forceNew: always a fresh build; skipAutoDetectionConfirmation: don't 400 on
    # framework auto-detection in an automated deploy.
    query = {"forceNew": "1", "skipAutoDetectionConfirmation": "1", **_team_query(project_id)}
    try:
        code, data = _vercel_api(
            "POST", "/v13/deployments", token, payload, query=query, opener=opener
        )
    except ConnectorError as e:
        return DeploymentResult(error=credentials.redact(str(e), project_id))
    if code in (200, 201, 202):
        return DeploymentResult(
            ok=True,
            deployment_id=data.get("id") or data.get("uid"),
            url=normalize_url(data.get("url")),
            ready_state=data.get("readyState") or data.get("status"),
            target=target,
        )
    return DeploymentResult(error=credentials.redact(_err_message(data, code), project_id))


def get_deployment(
    project_id: str, deployment_id: str, *, opener: Optional[Callable] = None
) -> DeploymentResult:
    """Poll a deployment's state. ``ready_state`` reaches ``READY`` on success."""
    token = credentials.get_token("vercel", project_id)
    if not token:
        return DeploymentResult(error="no Vercel token configured")
    try:
        code, data = _vercel_api(
            "GET",
            f"/v13/deployments/{urllib.parse.quote(deployment_id, safe='')}",
            token,
            query=_team_query(project_id),
            opener=opener,
        )
    except ConnectorError as e:
        return DeploymentResult(error=credentials.redact(str(e), project_id))
    if code == 200:
        return DeploymentResult(
            ok=True,
            deployment_id=data.get("id") or data.get("uid") or deployment_id,
            url=normalize_url(data.get("url")),
            ready_state=data.get("readyState") or data.get("status"),
            target=(data.get("target") if isinstance(data.get("target"), str) else None),
        )
    return DeploymentResult(error=credentials.redact(_err_message(data, code), project_id))


def list_deployments(
    project_id: str, *, limit: int = 20, opener: Optional[Callable] = None
) -> tuple[List[dict], Optional[str]]:
    """List recent deployments for the linked project (for the redeploy/rollback
    target picker). Returns (deployments, error). Each entry is compact metadata
    (id / url / state / target / created) — never a secret."""
    token = credentials.get_token("vercel", project_id)
    if not token:
        return [], "no Vercel token configured"
    proj = credentials.get_metadata("vercel", "project_id", project_id)
    query = {"limit": str(limit), "projectId": proj, **_team_query(project_id)}
    try:
        code, data = _vercel_api("GET", "/v6/deployments", token, query=query, opener=opener)
    except ConnectorError as e:
        return [], credentials.redact(str(e), project_id)
    if code != 200:
        return [], credentials.redact(_err_message(data, code), project_id)
    out: List[dict] = []
    for d in data.get("deployments", []) or []:
        out.append(
            {
                "deployment_id": d.get("uid") or d.get("id"),
                "url": normalize_url(d.get("url")),
                "ready_state": d.get("readyState") or d.get("state"),
                "target": d.get("target"),
                "created": d.get("created") or d.get("createdAt"),
            }
        )
    return out, None


def promote_deployment(
    project_id: str, deployment_id: str, *, opener: Optional[Callable] = None
) -> DeploymentResult:
    """Roll back production by promoting a previous deployment (instant rollback,
    no rebuild). Free-tier reaches only the immediately previous prod deployment."""
    token = credentials.get_token("vercel", project_id)
    if not token:
        return DeploymentResult(error="no Vercel token configured")
    proj = credentials.get_metadata("vercel", "project_id", project_id)
    if not proj:
        return DeploymentResult(error="no Vercel project linked")
    try:
        code, data = _vercel_api(
            "POST",
            f"/v10/projects/{urllib.parse.quote(proj, safe='')}/promote/"
            f"{urllib.parse.quote(deployment_id, safe='')}",
            token,
            payload={},
            query=_team_query(project_id),
            opener=opener,
        )
    except ConnectorError as e:
        return DeploymentResult(error=credentials.redact(str(e), project_id))
    if code in (200, 201, 202):
        return DeploymentResult(ok=True, deployment_id=deployment_id, target="production")
    return DeploymentResult(error=credentials.redact(_err_message(data, code), project_id))


def set_env_var(
    project_id: str,
    key: str,
    value: str,
    *,
    targets: List[str],
    var_type: str = "sensitive",
    opener: Optional[Callable] = None,
) -> EnvVarResult:
    """Upsert a project env var on Vercel. ``value`` is read by the CALLER from
    the app-env registry via ``credentials.get_env_value`` and passed here at
    action time only — it never enters a contract/log/artifact. Secret vars MUST
    use ``var_type="sensitive"`` (write-only, not readable back)."""
    token = credentials.get_token("vercel", project_id)
    if not token:
        return EnvVarResult(error="no Vercel token configured")
    proj = credentials.get_metadata("vercel", "project_id", project_id)
    if not proj:
        return EnvVarResult(error="no Vercel project linked")
    if not value:
        return EnvVarResult(error="no value to push (set it in the env registry first)")
    target_list = list(targets)
    if var_type == "sensitive":
        # Vercel rejects a Sensitive env var that targets "development"
        # ("You cannot set a Sensitive Environment Variable's target to
        # development."). Restrict sensitive vars to production/preview so a
        # secret app-env var (which is forced to type="sensitive") still pushes.
        target_list = [t for t in target_list if t != "development"] or ["production", "preview"]
    payload = {"key": key, "value": value, "type": var_type, "target": target_list}
    try:
        code, data = _vercel_api(
            "POST",
            f"/v10/projects/{urllib.parse.quote(proj, safe='')}/env",
            token,
            payload,
            query={"upsert": "true", **_team_query(project_id)},
            opener=opener,
        )
    except ConnectorError as e:
        return EnvVarResult(error=credentials.redact(str(e), project_id))
    if code in (200, 201):
        created = data.get("created") if isinstance(data.get("created"), dict) else data
        return EnvVarResult(ok=True, key=key, env_id=(created or {}).get("id"))
    return EnvVarResult(error=credentials.redact(_err_message(data, code), project_id))
