"""Phase 8 — project-scoped app-environment registry.

The variables the BUILT app needs at runtime (``DATABASE_URL``,
``NEXT_PUBLIC_SUPABASE_URL``, ``STRIPE_SECRET_KEY``, ``STRIPE_WEBHOOK_SECRET``,
…) — DISTINCT from the Agent OS connector tokens that live in
``credentials.py``. These are later pushed to Vercel via the env-set contract.

Stored gitignored under ``credentials/env/{id}.json`` (the ``credentials/`` tree
is already excluded). This module is **presence-only**: it never returns a
value. The single value reader is ``credentials.get_env_value`` so
``credentials.py`` stays the sole secret reader; secret values are registered
with ``credentials.redact`` so a CLI/Vercel echo of e.g. a ``DATABASE_URL`` is
scrubbed everywhere.

The store directory is derived from ``credentials._CRED_DIR`` at call time (not
captured at import), so a test that redirects the credential store also
redirects this registry.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, List, Optional

import credentials

log = logging.getLogger(__name__)

_VALID_TARGETS = ("production", "preview", "development")


def _env_file(project_id: str) -> Path:
    return credentials._CRED_DIR / "env" / f"{credentials._safe_project_id(project_id)}.json"


def _read(project_id: str) -> dict:
    """Raw store read (key -> {value, targets, secret}). Internal — the only
    value egress is ``credentials.get_env_value``."""
    try:
        return credentials._read_json(_env_file(project_id))
    except ValueError:
        return {}


def _entry_presence(key: str, entry: dict) -> dict:
    return {
        "key": key,
        "targets": [t for t in entry.get("targets", []) if t in _VALID_TARGETS] or list(_VALID_TARGETS),
        "secret": bool(entry.get("secret", True)),
        "is_set": bool(str(entry.get("value") or "").strip()),
    }


def set_env_var(
    project_id: str,
    key: str,
    value: str,
    *,
    targets: Optional[Iterable[str]] = None,
    secret: bool = True,
) -> dict:
    """Store one app-env var. Returns its presence entry (never the value)."""
    key = (key or "").strip()
    if not key:
        raise ValueError("env var key is required")
    sval = "" if value is None else str(value)
    if not sval.strip():
        raise ValueError("env var value is required")
    tlist = [t for t in (targets or _VALID_TARGETS) if t in _VALID_TARGETS] or list(_VALID_TARGETS)
    data = _read(project_id)
    data[key] = {"value": sval, "targets": tlist, "secret": bool(secret)}
    credentials._atomic_write(_env_file(project_id), data)
    return _entry_presence(key, data[key])


def delete_env_var(project_id: str, key: str) -> bool:
    data = _read(project_id)
    if key in data:
        data.pop(key, None)
        credentials._atomic_write(_env_file(project_id), data)
        return True
    return False


def list_env(project_id: str) -> List[dict]:
    """Presence-only listing — key + targets + secret flag + is_set. NEVER a value."""
    data = _read(project_id)
    return [_entry_presence(k, v) for k, v in sorted(data.items())]


def iter_secret_values(project_id: Optional[str]) -> List[str]:
    """Every stored value flagged ``secret`` for a project. Used ONLY by
    ``credentials.redact`` (via the registered source) so app-env secrets are
    exact-matched out of any egress string. Not a general value accessor."""
    if not project_id:
        return []
    out: List[str] = []
    for v in _read(project_id).values():
        if v.get("secret", True):
            val = v.get("value")
            if val and str(val).strip():
                out.append(str(val).strip())
    return out


# Register with the credential redactor so a leaked app-env secret value (e.g. a
# DATABASE_URL echoed by a CLI) is scrubbed from logs/events/result.md/UI.
credentials.register_secret_source(iter_secret_values)
