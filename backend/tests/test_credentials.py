"""Tests for Task 7.5 — central credential accessor (credentials.py).

Coverage:
  - env fallback, global-file store, project store, and the project > global >
    env resolution order.
  - status() exposes presence/metadata only — never the token value.
  - set / update-metadata / delete round-trips on the gitignored store.
  - redact() strips exact stored token values + common token shapes.
  - project-id sanitization rejects traversal.

Run directly:
    python backend/tests/test_credentials.py
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


class _Sandbox:
    """Redirect the credential store to a temp dir + clear env tokens."""

    # Clear every provider's env fallbacks so the temp store is authoritative.
    ENV_VARS = tuple(
        dict.fromkeys(v for cfg in credentials._PROVIDERS.values() for v in cfg["env_vars"])
    )

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._prev = (
            credentials._CRED_DIR,
            credentials._PROJECTS_DIR,
            credentials._GLOBAL_FILE,
        )
        credentials._CRED_DIR = root / "credentials"
        credentials._PROJECTS_DIR = credentials._CRED_DIR / "projects"
        credentials._GLOBAL_FILE = credentials._CRED_DIR / "global.json"
        import os

        self._env_backup = {k: os.environ.get(k) for k in self.ENV_VARS}
        for k in self.ENV_VARS:
            os.environ.pop(k, None)

    def set_env(self, var: str, value: str) -> None:
        import os

        os.environ[var] = value

    def cleanup(self) -> None:
        import os

        (
            credentials._CRED_DIR,
            credentials._PROJECTS_DIR,
            credentials._GLOBAL_FILE,
        ) = self._prev
        for k, v in self._env_backup.items():
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


# ---------- resolution order ----------


def test_no_credential():
    def body(sb):
        assert credentials.get_github_token("proj") is None
        st = credentials.status("proj")
        assert st["configured"] is False and st["source"] == "none"

    _run(body)


def test_env_fallback():
    def body(sb):
        sb.set_env("GITHUB_TOKEN", "env-tok-123")
        assert credentials.get_github_token("proj") == "env-tok-123"
        st = credentials.status("proj")
        assert st["configured"] and st["source"] == "env"

    _run(body)


def test_project_overrides_global_and_env():
    def body(sb):
        sb.set_env("GITHUB_TOKEN", "env-tok")
        credentials.set_github_credential(None, token="global-tok", scope="global")
        credentials.set_github_credential("proj", token="proj-tok", scope="project")
        # project wins for that project
        assert credentials.get_github_token("proj") == "proj-tok"
        assert credentials.status("proj")["source"] == "project"
        # a different project sees the global file (overrides env)
        assert credentials.get_github_token("other") == "global-tok"
        assert credentials.status("other")["source"] == "global_file"

    _run(body)


# ---------- no leakage ----------


def test_status_never_leaks_token():
    def body(sb):
        credentials.set_github_credential("proj", token="topsecret-xyz", login="octocat")
        st = credentials.status("proj")
        blob = json.dumps(st)
        assert "topsecret-xyz" not in blob
        assert "token" not in st
        assert st["login"] == "octocat"

    _run(body)


# ---------- writers ----------


def test_update_metadata_and_delete():
    def body(sb):
        credentials.set_github_credential("proj", token="t1")
        credentials.update_github_metadata("proj", login="me", default_remote="me/repo")
        st = credentials.status("proj")
        assert st["login"] == "me" and st["default_remote"] == "me/repo"
        credentials.delete_github_credential("proj")
        assert credentials.get_github_token("proj") is None

    _run(body)


# ---------- redaction ----------


def test_redact_known_and_patterns():
    def body(sb):
        credentials.set_github_credential("proj", token="stored-secret-aaa")
        text = (
            "remote add origin https://stored-secret-aaa@github.com/x/y\n"
            "token ghp_0123456789abcdefghij0123\n"
            "PASSWORD=hunter2\n"
            "nothing to see here\n"
        )
        out = credentials.redact(text, "proj")
        assert "stored-secret-aaa" not in out
        assert "ghp_0123456789" not in out
        assert "PASSWORD=[REDACTED]" in out
        assert "nothing to see here" in out

    _run(body)


def test_redact_handles_empty():
    def body(sb):
        assert credentials.redact("") == ""
        assert credentials.redact(None) is None

    _run(body)


# ---------- safety ----------


def test_safe_project_id_rejects_traversal():
    def body(sb):
        for bad in ("..", ".", "", "../../etc"):
            try:
                credentials._safe_project_id(bad)
                if bad not in ("../../etc",):
                    assert False, f"expected rejection for {bad!r}"
            except ValueError:
                pass
        # traversal chars are sanitized to underscores, not allowed through
        assert "/" not in credentials._safe_project_id("a/b/c")
        assert "\\" not in credentials._safe_project_id("a\\b")

    _run(body)


# ---------- Phase 8: multi-provider registry ----------


def test_provider_round_trip_vercel():
    def body(sb):
        credentials.set_credential("vercel", "proj", fields={"token": "vrc_opaque_24charstoken"})
        assert credentials.get_token("vercel", "proj") == "vrc_opaque_24charstoken"
        st = credentials.status("proj", "vercel")
        assert st["provider"] == "vercel" and st["configured"] is True
        assert st["secret_fields"]["token"] is True
        assert "vrc_opaque_24charstoken" not in json.dumps(st)
        # github is independent + still unset for this project
        assert credentials.status("proj", "github")["configured"] is False
        credentials.delete_credential("vercel", "proj")
        assert credentials.get_token("vercel", "proj") is None

    _run(body)


def test_vercel_env_fallback_and_metadata():
    def body(sb):
        sb.set_env("VERCEL_TOKEN", "env-vrc-tok")
        assert credentials.get_token("vercel", "proj") == "env-vrc-tok"
        assert credentials.status("proj", "vercel")["source"] == "env"
        # metadata is non-secret and round-trips via update_metadata (needs a stored token)
        credentials.set_credential("vercel", "proj", fields={"token": "stored-vrc"})
        credentials.update_metadata("vercel", "proj", {"org_id": "team_123", "project_id": "prj_abc"})
        st = credentials.status("proj", "vercel")
        assert st["org_id"] == "team_123" and st["project_id"] == "prj_abc"
        assert credentials.get_metadata("vercel", "project_id", "proj") == "prj_abc"

    _run(body)


def test_supabase_multi_secret_and_public_anon():
    def body(sb):
        credentials.set_credential(
            "supabase",
            "proj",
            fields={
                "access_token": "sbp_pat_0123456789abcdef0123",
                "db_password": "sup3r-secret-pw",
                "service_role": "eyJhbG.service.role.secret",
                "project_ref": "abcdefabcdef",
                "anon_key": "eyJhbG.anon.public",
                "url": "https://abcdefabcdef.supabase.co",
            },
        )
        # every secret field resolves through get_secret
        assert credentials.get_secret("supabase", "db_password", "proj") == "sup3r-secret-pw"
        assert credentials.get_secret("supabase", "service_role", "proj") == "eyJhbG.service.role.secret"
        assert credentials.get_token("supabase", "proj") == "sbp_pat_0123456789abcdef0123"
        # public anon + url are metadata, not secrets
        assert credentials.get_metadata("supabase", "anon_key", "proj") == "eyJhbG.anon.public"
        # status never leaks any secret value but shows presence + public metadata
        st = credentials.status("proj", "supabase")
        blob = json.dumps(st)
        for secret in ("sbp_pat_0123456789abcdef0123", "sup3r-secret-pw", "eyJhbG.service.role.secret"):
            assert secret not in blob
        assert st["secret_fields"] == {"access_token": True, "db_password": True, "service_role": True}
        assert st["project_ref"] == "abcdefabcdef"
        assert st["anon_key"] == "eyJhbG.anon.public"

    _run(body)


def test_stripe_live_key_refused_test_key_ok():
    def body(sb):
        # a live key is refused at the store boundary (default-deny)
        try:
            credentials.set_credential("stripe", "proj", fields={"secret_key": "sk_live_abc123abc123"})
            assert False, "expected live-key refusal"
        except ValueError:
            pass
        # an explicit override stores it
        credentials.set_credential(
            "stripe", "proj", fields={"secret_key": "sk_live_abc123abc123"}, allow_live=True
        )
        assert credentials.get_token("stripe", "proj") == "sk_live_abc123abc123"
        # a test key is accepted normally; webhook secret stored alongside
        credentials.set_credential(
            "stripe",
            "proj",
            fields={"secret_key": "sk_test_xyz789xyz789", "webhook_secret": "whsec_localabc123abc"},
        )
        assert credentials.get_secret("stripe", "webhook_secret", "proj") == "whsec_localabc123abc"
        st = credentials.status("proj", "stripe")
        assert st["secret_fields"] == {"secret_key": True, "webhook_secret": True}
        assert "sk_test_xyz789xyz789" not in json.dumps(st)

    _run(body)


def test_status_all_lists_every_provider():
    def body(sb):
        credentials.set_credential("vercel", "proj", fields={"token": "vrc-tok"})
        allst = credentials.status_all("proj")
        assert set(allst) == set(credentials._PROVIDERS)
        assert allst["vercel"]["configured"] is True
        assert allst["github"]["configured"] is False

    _run(body)


def test_unknown_provider_rejected():
    def body(sb):
        for fn in (
            lambda: credentials.get_token("bogus", "proj"),
            lambda: credentials.status("proj", "bogus"),
            lambda: credentials.set_credential("bogus", "proj", fields={"token": "x"}),
        ):
            try:
                fn()
                assert False, "expected unknown-provider rejection"
            except ValueError:
                pass

    _run(body)


# ---------- Phase 8: redaction of new shapes ----------


def test_redact_phase8_shapes():
    def body(sb):
        # an opaque Vercel token (no shape) is caught by exact stored-value match
        credentials.set_credential("vercel", "proj", fields={"token": "OpaqueVercelTokenNoShape24"})
        # a secret service_role JWT is caught by exact value; the public anon JWT is NOT
        credentials.set_credential(
            "supabase",
            "proj",
            fields={"service_role": "eyJsecretSERVICErolejwtvalue", "anon_key": "eyJpublicANONjwtvalue"},
        )
        text = (
            "deploy: token=OpaqueVercelTokenNoShape24 used\n"
            "STRIPE_SECRET_KEY=sk_test_abcdefghijklmnop\n"
            "sig whsec_aabbccddeeff00112233\n"
            "supabase login sbp_aabbccddeeff0011223344\n"
            "DATABASE_URL=postgresql://postgres:My-DB-Pass99@db.abc.supabase.co:5432/postgres\n"
            "service eyJsecretSERVICErolejwtvalue here\n"
            "anon key eyJpublicANONjwtvalue stays\n"
            "publishable pk_test_visiblepublishable keeps\n"
        )
        out = credentials.redact(text, "proj")
        assert "OpaqueVercelTokenNoShape24" not in out
        assert "sk_test_abcdefghijklmnop" not in out
        assert "whsec_aabbccddeeff00112233" not in out
        assert "sbp_aabbccddeeff0011223344" not in out
        assert "My-DB-Pass99" not in out and "@db.abc.supabase.co" in out  # password gone, host kept
        assert "eyJsecretSERVICErolejwtvalue" not in out  # secret service_role redacted
        # public values survive (they ship to clients; redacting them hides nothing + breaks debugging)
        assert "eyJpublicANONjwtvalue" in out
        assert "pk_test_visiblepublishable" in out

    _run(body)


def test_redact_extra_secret_source():
    def body(sb):
        snapshot = list(credentials._EXTRA_SECRET_SOURCES)
        captured = {"v": "AppEnvSecretValue123"}
        credentials.register_secret_source(lambda pid: [captured["v"]] if pid == "proj" else [])
        try:
            out = credentials.redact("env DATABASE_URL was AppEnvSecretValue123 here", "proj")
            assert "AppEnvSecretValue123" not in out
            # a different project does not see it
            out2 = credentials.redact("AppEnvSecretValue123", "other")
            assert "AppEnvSecretValue123" in out2
        finally:
            credentials._EXTRA_SECRET_SOURCES[:] = snapshot

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
