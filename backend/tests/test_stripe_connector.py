"""Tests for Phase 8 — Stripe connector (execution/stripe_connector.py).

Network is faked via an injected ``opener``. Coverage:
  - form-encoding (NOT JSON) with bracket notation + Idempotency-Key header.
  - test-mode gate: a non-test key and a livemode:true response are refused.
  - provision Product+Price returns price_id.
  - register webhook stores the whsec_ via credentials but NEVER returns it;
    GET-then-create is idempotent.
  - status is presence-only.

Run directly:
    python backend/tests/test_stripe_connector.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import credentials  # noqa: E402
from execution import stripe_connector as sc  # noqa: E402


class _Sandbox:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = (credentials._CRED_DIR, credentials._PROJECTS_DIR, credentials._GLOBAL_FILE)
        credentials._CRED_DIR = root / "credentials"
        credentials._PROJECTS_DIR = credentials._CRED_DIR / "projects"
        credentials._GLOBAL_FILE = credentials._CRED_DIR / "global.json"
        import os

        self._eb = {k: os.environ.get(k) for k in credentials._PROVIDERS["stripe"]["env_vars"]}
        for k in credentials._PROVIDERS["stripe"]["env_vars"]:
            os.environ.pop(k, None)

    def cleanup(self) -> None:
        import os

        (credentials._CRED_DIR, credentials._PROJECTS_DIR, credentials._GLOBAL_FILE) = self._prev
        for k, v in self._eb.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


def _run(test_body):
    sb = _Sandbox()
    try:
        test_body(sb)
    finally:
        sb.cleanup()


class _Resp:
    def __init__(self, code, body):
        self._c, self._b = code, json.dumps(body).encode()

    def read(self):
        return self._b

    def getcode(self):
        return self._c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _router(routes):
    """routes: list of (method, path_substr, code, payload). Captures the LAST request."""
    cap: dict = {}

    def opener(req, timeout=None):
        cap["url"] = req.full_url
        cap["method"] = req.get_method()
        cap["headers"] = {k.lower(): v for k, v in req.header_items()}
        cap["data"] = req.data.decode("utf-8") if req.data else None
        for method, sub, code, payload in routes:
            if req.get_method() == method and sub in req.full_url:
                return _Resp(code, payload)
        return _Resp(404, {"error": {"message": "no route"}})

    opener.cap = cap
    return opener


def _test_key():
    credentials.set_credential("stripe", "p", fields={"secret_key": "sk_test_abcdef123456"})


# ---------- encoding + gate ----------


def test_form_encode_bracket_notation():
    enc = sc._form_encode({
        "url": "https://x/api",
        "enabled_events": ["checkout.session.completed", "invoice.paid"],
        "line_items": [{"price": "price_1", "quantity": 1}],
        "active": True,
    })
    assert "enabled_events%5B%5D=checkout.session.completed" in enc  # enabled_events[]=
    assert "line_items%5B0%5D%5Bprice%5D=price_1" in enc             # line_items[0][price]=
    assert "active=true" in enc


def test_is_test_key_and_live_refused():
    assert sc.is_test_key("sk_test_x") and not sc.is_test_key("sk_live_x")
    try:
        sc._stripe_api("GET", "/v1/account", "sk_live_nope")
        assert False, "expected live-key refusal"
    except sc.LiveModeRefused:
        pass


def test_livemode_response_refused():
    def body(sb):
        _test_key()
        op = _router([("POST", "/v1/products", 200, {"id": "prod_1", "livemode": True})])
        res = sc.provision_price("p", name="X", unit_amount=1000, opener=op)
        assert not res.ok and "live" in (res.error or "").lower()

    _run(body)


# ---------- provisioning ----------


def test_provision_price_form_encoded_with_idempotency():
    def body(sb):
        _test_key()
        op = _router([
            ("POST", "/v1/products", 200, {"id": "prod_1", "livemode": False}),
            ("POST", "/v1/prices", 200, {"id": "price_1", "livemode": False}),
        ])
        res = sc.provision_price("p", name="Pro", unit_amount=1500, currency="usd", opener=op)
        assert res.ok and res.price_id == "price_1" and res.product_id == "prod_1"
        # last call (prices) is form-encoded + carries an Idempotency-Key + the token in the header
        assert op.cap["headers"]["content-type"] == "application/x-www-form-urlencoded"
        assert "idempotency-key" in op.cap["headers"]
        assert op.cap["headers"]["authorization"] == "Bearer sk_test_abcdef123456"
        assert "unit_amount=1500" in op.cap["data"]
        assert "sk_test_abcdef123456" not in (op.cap["data"] or "")  # key not in body

    _run(body)


# ---------- webhooks ----------


def test_register_webhook_stores_secret_never_returns():
    def body(sb):
        _test_key()
        op = _router([
            ("GET", "/v1/webhook_endpoints", 200, {"data": []}),
            ("POST", "/v1/webhook_endpoints", 200,
             {"id": "we_1", "secret": "whsec_supersecret", "enabled_events": ["checkout.session.completed"],
              "livemode": False}),
        ])
        res = sc.register_webhook("p", "https://app.vercel.app/api/stripe/webhook", opener=op)
        assert res.ok and res.endpoint_id == "we_1" and res.secret_stored is True
        # whsec_ must NOT be in the returned object...
        assert "whsec_supersecret" not in json.dumps(res.__dict__)
        # ...but IS stored for later use
        assert credentials.get_secret("stripe", "webhook_secret", "p") == "whsec_supersecret"

    _run(body)


def test_register_webhook_idempotent_reuse():
    def body(sb):
        _test_key()
        op = _router([
            ("GET", "/v1/webhook_endpoints", 200,
             {"data": [{"id": "we_existing", "url": "https://app/api/stripe/webhook",
                        "enabled_events": ["checkout.session.completed"]}]}),
        ])
        res = sc.register_webhook("p", "https://app/api/stripe/webhook", opener=op)
        assert res.ok and res.endpoint_id == "we_existing" and res.secret_stored is False

    _run(body)


def test_status_presence_only():
    def body(sb):
        _test_key()
        op = _router([("GET", "/v1/account", 200, {"id": "acct_1", "livemode": False})])
        st = sc.status("p", opener=op).to_dict()
        assert st["configured"] and st["connected"] and st["mode"] == "test"
        assert st["account"] == "acct_1"
        assert "sk_test_abcdef123456" not in json.dumps(st)

    _run(body)


def _run_all() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed: list[str] = []
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed.append(fn.__name__)
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append(fn.__name__)
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print()
    if failed:
        print(f"{len(failed)} test(s) failed: {', '.join(failed)}")
        return 1
    print(f"All {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
