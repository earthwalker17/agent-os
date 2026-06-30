"""Central credential accessor (Project Ops + Production Path).

The single place secret VALUES are read. Global tokens come from the
environment (like provider keys); project-scoped tokens live in a gitignored
on-disk store OUTSIDE ``projects/`` and ``memory/`` (so a secret can never be
swept into memory writeback). Resolution order: a **project** record overrides
the **global file**, which overrides the **environment**.

Phase 7 introduced this for GitHub. **Phase 8** generalizes it to a small
provider registry — ``github | vercel | supabase | stripe`` — each with a
primary token plus (for Supabase/Stripe) extra secret fields and some
non-secret metadata. The GitHub-specific helpers remain as thin aliases so
Phase 7 callers are untouched.

Constitutional rules:
- A secret value leaves this module ONLY via the value readers (``get_token`` /
  ``get_secret`` / ``get_github_token`` / ``get_env_value``), consumed by a
  connector's env-injection / header at action time. ``status`` /
  ``status_all`` return presence + non-secret metadata — never a value.
- ``redact`` scrubs known stored secret values + common token shapes (incl. a
  Postgres connection-string password) from any string before it can reach a
  prompt, log, event, result.md, memory, or the UI. The *public* Stripe
  publishable key and the *public* Supabase anon key are deliberately NOT
  redacted (they are meant to ship in client bundles); the secret
  ``service_role`` JWT is caught by exact value, never by JWT shape.
- ``allow_live`` defaults to false everywhere: a non-test Stripe key is refused
  at the store boundary unless the caller explicitly opts in.
- Nothing here writes into the repo, run.json, events.jsonl, or memory. The
  store lives under ``<root>/credentials/`` which ``.gitignore`` excludes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

# <repo-root>/credentials/ — sibling to projects/, memory/, execution_workspaces/.
_ROOT = Path(__file__).resolve().parent.parent
_CRED_DIR = _ROOT / "credentials"
_PROJECTS_DIR = _CRED_DIR / "projects"
_GLOBAL_FILE = _CRED_DIR / "global.json"

# Kept as a module constant (referenced by tests + the github provider config).
_GITHUB_ENV_VARS = ("GITHUB_TOKEN", "AGENT_OS_GITHUB_TOKEN", "GH_TOKEN")


# ---------- provider registry ----------
#
# Each provider declares:
#   env_vars      — env fallbacks for the PRIMARY token field (first non-empty wins)
#   token_field   — the primary secret used as the connector token
#   secret_fields — every field whose value must never egress (redacted by value)
#   meta_fields   — non-secret metadata safe to return in status()
#
# Note: only the primary token has an env fallback; extra secret fields and
# metadata are store-only. Public values (Stripe publishable key, Supabase anon
# key) live in meta_fields — present in status(), never in secret_fields.

_PROVIDERS: dict[str, dict] = {
    "github": {
        "env_vars": _GITHUB_ENV_VARS,
        "token_field": "token",
        "secret_fields": ("token",),
        "meta_fields": ("login", "default_remote"),
    },
    "vercel": {
        "env_vars": ("VERCEL_TOKEN", "AGENT_OS_VERCEL_TOKEN"),
        "token_field": "token",
        "secret_fields": ("token",),
        "meta_fields": ("username", "org_id", "project_id"),
    },
    "supabase": {
        "env_vars": ("SUPABASE_ACCESS_TOKEN", "AGENT_OS_SUPABASE_TOKEN"),
        "token_field": "access_token",
        "secret_fields": ("access_token", "db_password", "service_role"),
        "meta_fields": ("project_ref", "url", "anon_key"),
    },
    "stripe": {
        "env_vars": ("STRIPE_SECRET_KEY", "AGENT_OS_STRIPE_SECRET_KEY"),
        "token_field": "secret_key",
        "secret_fields": ("secret_key", "webhook_secret"),
        "meta_fields": ("account", "publishable_key"),
    },
}

# A Stripe API key (publishable / secret / restricted) must be a TEST key unless
# the caller explicitly allows live. The webhook signing secret (whsec_) has no
# test/live prefix and is exempt from this check.
# Public tuple of registered provider ids (for route validation / UI).
PROVIDERS = tuple(_PROVIDERS.keys())

_STRIPE_KEY_FIELDS = ("secret_key", "publishable_key")
_STRIPE_TEST_KEY_RE = re.compile(r"^(?:pk|sk|rk)_test_")
_STRIPE_API_KEY_RE = re.compile(r"^(?:pk|sk|rk)_(?:test|live)_")


_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_project_id(project_id: str) -> str:
    pid = _SAFE_ID.sub("_", (project_id or "").strip())
    if not pid or pid in (".", ".."):
        raise ValueError("invalid project id")
    return pid


def _provider_cfg(provider: str) -> dict:
    cfg = _PROVIDERS.get(provider)
    if cfg is None:
        raise ValueError(f"unknown provider: {provider!r}")
    return cfg


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


def _env_token(provider: str = "github") -> Optional[str]:
    for var in _provider_cfg(provider)["env_vars"]:
        v = os.environ.get(var)
        if v and v.strip():
            return v.strip()
    return None


def _env_github_token() -> Optional[str]:  # back-compat alias
    return _env_token("github")


def _global_record(provider: str = "github") -> dict:
    return _read_json(_GLOBAL_FILE).get(provider, {}) or {}


def _project_record(project_id: Optional[str], provider: str = "github") -> dict:
    if not project_id:
        return {}
    try:
        return _read_json(_project_file(project_id)).get(provider, {}) or {}
    except ValueError:
        return {}


# ---------- value readers (connector only) ----------


def get_token(provider: str, project_id: Optional[str] = None) -> Optional[str]:
    """Resolve a provider's PRIMARY token (project store → global file → env).
    Returns ``None`` if unset. A value-returning accessor — callers MUST keep
    the result out of logs, argv, commits, and memory."""
    tf = _provider_cfg(provider)["token_field"]
    if project_id:
        tok = _project_record(project_id, provider).get(tf)
        if tok and str(tok).strip():
            return str(tok).strip()
    gtok = _global_record(provider).get(tf)
    if gtok and str(gtok).strip():
        return str(gtok).strip()
    return _env_token(provider)


def get_github_token(project_id: Optional[str] = None) -> Optional[str]:
    """Phase 7 alias — resolve the GitHub token (project → global → env)."""
    return get_token("github", project_id)


def get_secret(provider: str, field: str, project_id: Optional[str] = None) -> Optional[str]:
    """Resolve a specific secret FIELD for a provider (project → global; the
    primary token field additionally falls back to env). Returns ``None`` if
    unset or if ``field`` is not a declared secret of ``provider``."""
    cfg = _provider_cfg(provider)
    if field == cfg["token_field"]:
        return get_token(provider, project_id)
    if field not in cfg["secret_fields"]:
        return None
    if project_id:
        v = _project_record(project_id, provider).get(field)
        if v and str(v).strip():
            return str(v).strip()
    gv = _global_record(provider).get(field)
    if gv and str(gv).strip():
        return str(gv).strip()
    return None


def get_env_value(project_id: str, key: str) -> Optional[str]:
    """The SINGLE reader of an app-env value (so this module stays the only
    place a secret value leaves). Backs the Vercel env-set connector at action
    time; callers MUST keep the result out of logs, argv, and memory. The
    registry itself (``execution.app_env``) is presence-only."""
    if not project_id or not key:
        return None
    try:
        from execution import app_env  # lazy — avoid an import cycle
    except Exception:  # noqa: BLE001
        return None
    entry = app_env._read(project_id).get(key)
    if not entry:
        return None
    val = entry.get("value")
    return str(val) if val is not None and str(val).strip() else None


def get_metadata(provider: str, field: str, project_id: Optional[str] = None) -> Optional[str]:
    """Resolve a NON-secret metadata field (e.g. Vercel ``project_id`` /
    ``org_id``, Supabase ``project_ref`` / ``url`` / ``anon_key``). Safe to log
    — these are not secrets. Returns ``None`` if unset or not a metadata field."""
    cfg = _provider_cfg(provider)
    if field not in cfg["meta_fields"]:
        return None
    if project_id:
        v = _project_record(project_id, provider).get(field)
        if v is not None and str(v).strip():
            return str(v).strip()
    gv = _global_record(provider).get(field)
    if gv is not None and str(gv).strip():
        return str(gv).strip()
    return None


def _token_source(project_id: Optional[str], provider: str = "github") -> str:
    tf = _provider_cfg(provider)["token_field"]
    if project_id and _project_record(project_id, provider).get(tf):
        return "project"
    if _global_record(provider).get(tf):
        return "global_file"
    if _env_token(provider):
        return "env"
    return "none"


# ---------- presence-only status ----------


def status(project_id: Optional[str] = None, provider: str = "github") -> dict:
    """Presence + non-secret metadata for a provider credential. NEVER a value.

    ``provider`` keeps its default of ``"github"`` and is the SECOND positional
    arg, so existing ``status(project_id)`` calls are unchanged.
    """
    cfg = _provider_cfg(provider)
    source = _token_source(project_id, provider)
    configured = source != "none"
    rec = _project_record(project_id, provider) if source == "project" else _global_record(provider)
    out: dict = {
        "provider": provider,
        "configured": configured,
        "source": source,  # project | global_file | env | none
        "scope": "project" if source == "project" else ("global" if configured else "none"),
    }
    # Non-secret metadata (values are safe to surface).
    for mf in cfg["meta_fields"]:
        out[mf] = rec.get(mf)
    # Presence-only map for every secret field (booleans, never values). The
    # primary token reflects the resolved `configured` (which includes env).
    fields: dict[str, bool] = {}
    for f in cfg["secret_fields"]:
        if f == cfg["token_field"]:
            fields[f] = configured
        else:
            fields[f] = bool((rec.get(f) or "").strip())
    out["secret_fields"] = fields
    return out


def status_all(project_id: Optional[str] = None) -> dict:
    """Presence status for every registered provider (no secret values)."""
    return {p: status(project_id, p) for p in _PROVIDERS}


# ---------- writers ----------


def set_credential(
    provider: str,
    project_id: Optional[str],
    *,
    fields: dict,
    scope: str = "project",
    allow_live: bool = False,
) -> dict:
    """Store one or more fields for ``provider`` in the gitignored store.

    ``scope`` is ``"project"`` (per-project file) or ``"global"`` (shared file).
    Empty/blank values are dropped. For Stripe, a non-test API key is refused
    unless ``allow_live`` is set (default-deny). Returns the presence
    ``status`` — never a value.
    """
    _provider_cfg(provider)
    clean: dict[str, str] = {}
    for k, v in (fields or {}).items():
        if v is None:
            continue
        s = str(v).strip()
        if s:
            clean[k] = s
    if not clean:
        raise ValueError("at least one non-empty field is required")
    if provider == "stripe":
        for f in _STRIPE_KEY_FIELDS:
            val = clean.get(f)
            if val and not _STRIPE_TEST_KEY_RE.match(val) and not allow_live:
                raise ValueError(
                    "refusing to store a non-test Stripe key; Phase 8 is test-mode "
                    "only (pass allow_live to override)"
                )
    target_global = scope == "global"
    if not target_global and not project_id:
        raise ValueError("project scope requires a project_id")
    path = _GLOBAL_FILE if target_global else _project_file(project_id)  # type: ignore[arg-type]
    data = _read_json(path)
    data[provider] = {**data.get(provider, {}), **clean}
    _atomic_write(path, data)
    return status(None if target_global else project_id, provider)


def set_github_credential(
    project_id: Optional[str],
    *,
    token: str,
    login: Optional[str] = None,
    default_remote: Optional[str] = None,
    scope: str = "project",
) -> dict:
    """Phase 7 alias — store a GitHub token (+ optional metadata)."""
    fields: dict = {"token": (token or "").strip()}
    if login:
        fields["login"] = login
    if default_remote:
        fields["default_remote"] = default_remote
    return set_credential(
        "github",
        None if scope == "global" else project_id,
        fields=fields,
        scope=scope,
    )


def update_metadata(provider: str, project_id: Optional[str], fields: dict) -> None:
    """Merge non-secret metadata onto an existing stored record for the active
    scope. ``fields`` is a dict (some metadata keys — e.g. Vercel ``project_id``
    — collide with this function's own params, so it is NOT ``**kwargs``).
    No-op for unknown fields or an env-only token (no file to annotate)."""
    cfg = _provider_cfg(provider)
    updates = {
        k: str(v).strip()
        for k, v in (fields or {}).items()
        if k in cfg["meta_fields"] and v and str(v).strip()
    }
    if not updates:
        return
    src = _token_source(project_id, provider)
    try:
        if src == "project" and project_id:
            path = _project_file(project_id)
        elif src == "global_file":
            path = _GLOBAL_FILE
        else:
            return  # env-only or unset — nothing to annotate
        data = _read_json(path)
        data[provider] = {**data.get(provider, {}), **updates}
        _atomic_write(path, data)
    except (OSError, ValueError):
        log.warning("could not update %s metadata for %s", provider, project_id)


def update_github_metadata(
    project_id: Optional[str],
    *,
    login: Optional[str] = None,
    default_remote: Optional[str] = None,
) -> None:
    """Phase 7 alias — merge GitHub login / default_remote metadata."""
    update_metadata("github", project_id, {"login": login, "default_remote": default_remote})


def delete_credential(
    provider: str, project_id: Optional[str] = None, *, scope: str = "project"
) -> dict:
    """Remove a stored provider record. Cannot remove an env-provided token
    (that is the operator's to unset)."""
    _provider_cfg(provider)
    try:
        if scope == "global":
            data = _read_json(_GLOBAL_FILE)
            data.pop(provider, None)
            _atomic_write(_GLOBAL_FILE, data)
        elif project_id:
            path = _project_file(project_id)
            data = _read_json(path)
            data.pop(provider, None)
            _atomic_write(path, data)
    except (OSError, ValueError):
        log.warning("could not delete %s credential for %s", provider, project_id)
    return status(None if scope == "global" else project_id, provider)


def delete_github_credential(project_id: Optional[str] = None, *, scope: str = "project") -> dict:
    """Phase 7 alias — remove a stored GitHub token."""
    return delete_credential("github", project_id, scope=scope)


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
# A Postgres connection string carries the DB password inline
# (postgresql://user:PASSWORD@host). Redact only the password segment so the
# rest stays debuggable. (Catches a CLI-reformatted DATABASE_URL that exact
# value-match might miss.)
_CONN_STRING_RE = re.compile(r"(postgres(?:ql)?://[^:@/\s]+:)([^@/\s]+)(@)")
_TOKEN_RES = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    # Phase 8 provider secret shapes. The PUBLIC `pk_*` publishable key and the
    # PUBLIC Supabase anon JWT are intentionally NOT matched (they ship to
    # clients); the secret `service_role` JWT is redacted by exact value via
    # `_all_known_tokens`, never by JWT shape.
    re.compile(r"\b[sr]k_(?:test|live)_[A-Za-z0-9]{10,}"),  # Stripe secret / restricted key
    re.compile(r"\bwhsec_[A-Za-z0-9]{10,}"),               # Stripe webhook signing secret
    re.compile(r"\bsbp_[A-Za-z0-9]{16,}"),                 # Supabase personal access token
)


def _all_known_tokens(project_id: Optional[str] = None) -> set[str]:
    """Every stored secret VALUE in scope (all providers, env + global + the
    given project), for exact-match redaction of shapeless secrets (the opaque
    Vercel token, the DB password, the service_role JWT). Pass ``project_id`` so
    project-scoped secrets are covered — without it, only env + global are."""
    toks: set[str] = set()
    for provider, cfg in _PROVIDERS.items():
        grec = _global_record(provider)
        prec = _project_record(project_id, provider) if project_id else {}
        for field in cfg["secret_fields"]:
            for rec in (prec, grec):
                val = rec.get(field)
                if val and str(val).strip():
                    toks.add(str(val).strip())
        ev = _env_token(provider)
        if ev:
            toks.add(ev)
    toks.update(_extra_secret_values(project_id))
    return toks


# Other modules (e.g. the app-env registry) register a callable that yields
# additional secret VALUES to exact-match in `redact`. Keeps `credentials.py`
# the single redaction authority without importing those modules here.
_EXTRA_SECRET_SOURCES: list = []


def register_secret_source(fn) -> None:
    """Register ``fn(project_id) -> Iterable[str]`` yielding extra secret values
    to redact (used by the app-env registry). Idempotent on identity."""
    if fn not in _EXTRA_SECRET_SOURCES:
        _EXTRA_SECRET_SOURCES.append(fn)


def _extra_secret_values(project_id: Optional[str]) -> set[str]:
    out: set[str] = set()
    for fn in _EXTRA_SECRET_SOURCES:
        try:
            for v in fn(project_id) or ():
                if v and str(v).strip():
                    out.add(str(v).strip())
        except Exception:  # noqa: BLE001 — a misbehaving source must never break redaction
            log.warning("a registered secret source raised; ignoring it")
    return out


def redact(text: str, project_id: Optional[str] = None) -> str:
    """Scrub known stored secret values + common secret shapes from ``text``.
    The strongest egress guard — call it (with ``project_id``) before any string
    that might contain a credential reaches a log/prompt/diff/UI/artifact."""
    if not text:
        return text
    out = text
    for tok in _all_known_tokens(project_id):
        out = out.replace(tok, "[REDACTED]")
    out = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", out)
    out = _CONN_STRING_RE.sub(r"\1[REDACTED]\3", out)
    out = _KV_SECRET_RE.sub(r"\1[REDACTED]", out)
    for pat in _TOKEN_RES:
        out = pat.sub("[REDACTED]", out)
    return out
