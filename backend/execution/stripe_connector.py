"""Phase 8 — Stripe connector (Production Path: test-mode checkout + webhooks).

Stripe is reached over its REST API via ``urllib``. Two things differ from the
GitHub/Vercel connectors and are easy to get wrong:

- **Stripe is form-encoded, NOT JSON.** Requests use
  ``application/x-www-form-urlencoded`` with bracket notation for nested/array
  params (``enabled_events[]=...``, ``line_items[0][price]=...``); responses are
  JSON. A JSON body 400s on every call.
- **Idempotency.** Mutating creates carry an ``Idempotency-Key`` header so a
  double-confirm / retry can't create duplicate Products / Prices / endpoints.

Constitutional test-mode gate (per request, at the executor boundary — no
import-time route theater): the secret key MUST be ``sk_test_`` / ``rk_test_``
and every response must have ``livemode == false``; otherwise the call is
refused. The webhook signing secret (``whsec_``) returned at endpoint creation is
stored via ``credentials`` and **never** copied into a contract / event / log.
The key is read through ``credentials.get_token('stripe', …)`` and rides only in
the ``Authorization`` header.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional

import credentials

log = logging.getLogger(__name__)

_API_BASE = "https://api.stripe.com"
_HTTP_TIMEOUT = 30

_TEST_KEY_RE = re.compile(r"^(?:sk|rk)_test_")


class ConnectorError(Exception):
    """Network / API failure talking to Stripe."""


class LiveModeRefused(ConnectorError):
    """A non-test key or a livemode response was seen — refused by the test gate."""


@dataclass
class StripeStatus:
    configured: bool = False
    connected: bool = False
    scope: str = "none"
    source: str = "none"
    mode: str = "test"
    has_webhook_secret: bool = False
    account: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "provider": "stripe",
            "configured": self.configured,
            "connected": self.connected,
            "scope": self.scope,
            "source": self.source,
            "mode": self.mode,
            "has_webhook_secret": self.has_webhook_secret,
            "account": self.account,
            "error": self.error,
        }


@dataclass
class ProvisionResult:
    ok: bool = False
    product_id: Optional[str] = None
    price_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class WebhookResult:
    ok: bool = False
    endpoint_id: Optional[str] = None
    url: Optional[str] = None
    events: Optional[List[str]] = None
    secret_stored: bool = False  # whsec_ stored via credentials; never returned
    error: Optional[str] = None


def _form_encode(params: dict) -> str:
    """Encode params the Stripe way: x-www-form-urlencoded with bracket notation
    for nested dicts (``a[b]=c``) and arrays (scalars → ``a[]=v``; objects →
    ``a[0][k]=v``). Booleans → 'true'/'false'."""
    pairs: list[tuple[str, str]] = []

    def add(key: str, val) -> None:
        if isinstance(val, dict):
            for k, v in val.items():
                add(f"{key}[{k}]", v)
        elif isinstance(val, (list, tuple)):
            for i, v in enumerate(val):
                if isinstance(v, (dict, list, tuple)):
                    add(f"{key}[{i}]", v)
                else:
                    pairs.append((f"{key}[]", str(v)))
        elif isinstance(val, bool):
            pairs.append((key, "true" if val else "false"))
        elif val is not None:
            pairs.append((key, str(val)))

    for k, v in params.items():
        add(k, v)
    return urllib.parse.urlencode(pairs)


def is_test_key(key: Optional[str]) -> bool:
    return bool(key and _TEST_KEY_RE.match(key))


def _stripe_api(
    method: str,
    path: str,
    key: str,
    params: Optional[dict] = None,
    *,
    idempotency_key: Optional[str] = None,
    opener: Optional[Callable] = None,
) -> tuple[int, dict]:
    """Call the Stripe REST API (form-encoded). Token in the Authorization header
    only. Asserts the response is test-mode."""
    if not is_test_key(key):
        raise LiveModeRefused("refusing to use a non-test Stripe key (Phase 8 is test-mode only)")
    url = _API_BASE + path
    data = _form_encode(params).encode("utf-8") if params is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("User-Agent", "Agent-OS")
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if idempotency_key:
        req.add_header("Idempotency-Key", idempotency_key)
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
        raise ConnectorError(f"network error calling Stripe: {exc.reason}")
    parsed = json.loads(body) if body else {}
    # livemode gate: any successful object asserting livemode:true is refused.
    if isinstance(parsed, dict) and parsed.get("livemode") is True:
        raise LiveModeRefused("Stripe returned a live-mode object; refusing (test-mode only)")
    return code, parsed


def _err_message(data: dict, code: int) -> str:
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    return str((data or {}).get("message", f"HTTP {code}"))


def _key(project_id: str) -> Optional[str]:
    return credentials.get_token("stripe", project_id)


# ---------- operations ----------


def status(project_id: str, *, opener: Optional[Callable] = None) -> StripeStatus:
    """Presence + live connectivity (validates the key via GET /v1/account).
    Always test-mode; never returns a secret."""
    cred = credentials.status(project_id, "stripe")
    st = StripeStatus(
        configured=cred["configured"],
        scope=cred["scope"],
        source=cred["source"],
        has_webhook_secret=bool(cred.get("secret_fields", {}).get("webhook_secret")),
    )
    if not cred["configured"]:
        return st
    key = _key(project_id)
    if not is_test_key(key):
        st.error = "configured Stripe key is not a test key (test-mode only)"
        return st
    try:
        code, data = _stripe_api("GET", "/v1/account", key, opener=opener)
    except ConnectorError as e:
        st.error = credentials.redact(str(e), project_id)
        return st
    if code == 200:
        st.connected = True
        st.account = data.get("id")
    elif code in (401, 403):
        st.error = "Stripe key rejected"
    else:
        st.error = credentials.redact(_err_message(data, code), project_id)
    return st


def provision_price(
    project_id: str,
    *,
    name: str,
    unit_amount: int,
    currency: str = "usd",
    recurring_interval: Optional[str] = None,
    opener: Optional[Callable] = None,
) -> ProvisionResult:
    """Create a test Product + Price (idempotent via a derived key). Returns the
    price id (for the app's checkout env). Session creation is app-runtime, not
    this contract."""
    key = _key(project_id)
    if not is_test_key(key):
        return ProvisionResult(error="no test Stripe key configured")
    try:
        code, prod = _stripe_api(
            "POST", "/v1/products", key, {"name": name},
            idempotency_key=f"agentos-{project_id}-product-{name}", opener=opener,
        )
        if code not in (200, 201):
            return ProvisionResult(error=credentials.redact(_err_message(prod, code), project_id))
        product_id = prod.get("id")
        price_params: dict = {"product": product_id, "unit_amount": unit_amount, "currency": currency}
        if recurring_interval:
            price_params["recurring"] = {"interval": recurring_interval}
        code, price = _stripe_api(
            "POST", "/v1/prices", key, price_params,
            idempotency_key=f"agentos-{project_id}-price-{product_id}-{unit_amount}", opener=opener,
        )
        if code not in (200, 201):
            return ProvisionResult(product_id=product_id, error=credentials.redact(_err_message(price, code), project_id))
        return ProvisionResult(ok=True, product_id=product_id, price_id=price.get("id"))
    except ConnectorError as e:
        return ProvisionResult(error=credentials.redact(str(e), project_id))


def _find_webhook(key: str, url: str, *, opener: Optional[Callable] = None) -> Optional[dict]:
    code, data = _stripe_api("GET", "/v1/webhook_endpoints?limit=100", key, opener=opener)
    if code != 200:
        return None
    for ep in data.get("data", []) or []:
        if ep.get("url") == url:
            return ep
    return None


def register_webhook(
    project_id: str,
    url: str,
    enabled_events: Optional[List[str]] = None,
    *,
    opener: Optional[Callable] = None,
) -> WebhookResult:
    """Register (or reuse) a deployed webhook endpoint. The returned ``whsec_`` is
    stored via credentials and NEVER echoed — only the endpoint id/url/events come
    back. GET-then-create keeps it idempotent."""
    key = _key(project_id)
    if not is_test_key(key):
        return WebhookResult(error="no test Stripe key configured")
    events = enabled_events or ["checkout.session.completed"]
    try:
        existing = _find_webhook(key, url, opener=opener)
        if existing:
            # already registered — reuse (Stripe only returns whsec_ at creation,
            # so we cannot re-store it; the user keeps the one from first creation).
            return WebhookResult(
                ok=True, endpoint_id=existing.get("id"), url=url,
                events=existing.get("enabled_events"), secret_stored=False,
            )
        code, data = _stripe_api(
            "POST", "/v1/webhook_endpoints", key,
            {"url": url, "enabled_events": events},
            idempotency_key=f"agentos-{project_id}-webhook-{url}", opener=opener,
        )
        if code not in (200, 201):
            return WebhookResult(error=credentials.redact(_err_message(data, code), project_id))
        secret = data.get("secret")  # whsec_… — store, never return
        stored = False
        if secret:
            try:
                credentials.set_credential("stripe", project_id, fields={"webhook_secret": secret})
                stored = True
            except Exception:  # noqa: BLE001
                log.warning("could not store stripe webhook secret")
        return WebhookResult(
            ok=True, endpoint_id=data.get("id"), url=url,
            events=data.get("enabled_events") or events, secret_stored=stored,
        )
    except ConnectorError as e:
        return WebhookResult(error=credentials.redact(str(e), project_id))


def delete_webhook(project_id: str, endpoint_id: str, *, opener: Optional[Callable] = None) -> WebhookResult:
    key = _key(project_id)
    if not is_test_key(key):
        return WebhookResult(error="no test Stripe key configured")
    try:
        code, data = _stripe_api(
            "DELETE", f"/v1/webhook_endpoints/{urllib.parse.quote(endpoint_id, safe='')}", key, opener=opener
        )
    except ConnectorError as e:
        return WebhookResult(error=credentials.redact(str(e), project_id))
    if code == 200:
        return WebhookResult(ok=True, endpoint_id=endpoint_id)
    return WebhookResult(error=credentials.redact(_err_message(data, code), project_id))


def local_webhook_command(port: int = 5174, path: str = "/api/stripe/webhook") -> dict:
    """Read-only: the exact `stripe listen` + `stripe trigger` commands the user
    runs locally (the local whsec_ it prints goes in the app's own .env, distinct
    from the deployed endpoint secret)."""
    return {
        "listen": f"stripe listen --forward-to localhost:{port}{path}",
        "trigger": "stripe trigger checkout.session.completed",
        "note": "The whsec_ printed by `stripe listen` is for LOCAL testing only — put it in the app's local .env.",
    }
