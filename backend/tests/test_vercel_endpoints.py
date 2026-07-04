"""Tests for Phase 8 — Vercel + connector/env HTTP endpoints (main.py).

Network is faked: the Vercel connector functions are monkeypatched, and the
background deploy finalize is run synchronously via a fake manager. Coverage:
  - generic /credentials/{provider} (+ /connectors) presence-only routes;
    Stripe live-key refusal through the route.
  - /env registry routes never echo a value.
  - vercel deploy: preview -> confirm (async finalize stamps the run + OPS.md);
    no-token guard; value never appears in an env-set contract.

Run directly:
    python backend/tests/test_vercel_endpoints.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import credentials  # noqa: E402
import llm  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
import execution.run_store as run_store  # noqa: E402
from execution import ops_ledger  # noqa: E402
from execution import vercel_connector as vc  # noqa: E402
from execution.models import RunRecord, RunStatus  # noqa: E402


class _FakeManager:
    """Runs submitted callables synchronously (so the deploy finalize completes
    before the test asserts)."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return None


class _Env:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects_dir = root / "projects"
        self.execution_dir = root / "execution_workspaces"
        self.projects_dir.mkdir()
        self.execution_dir.mkdir()
        self._restore: list = []
        self._save(main, "PROJECTS_DIR", self.projects_dir)
        self._save(exec_manager, "_EXECUTION_ROOT", self.execution_dir)
        self._save(ops_ledger, "_PROJECTS_DIR", self.projects_dir)
        self._save(credentials, "_CRED_DIR", root / "credentials")
        self._save(credentials, "_PROJECTS_DIR", root / "credentials" / "projects")
        self._save(credentials, "_GLOBAL_FILE", root / "credentials" / "global.json")
        self._save(main, "get_default_manager", lambda: _FakeManager())
        self._save(llm, "chat", lambda system, messages, **kw: "msg")
        # Clear every provider's env fallbacks so a real .env token doesn't make
        # a provider appear configured in the temp store.
        import os

        self._env_vars = tuple(
            dict.fromkeys(v for cfg in credentials._PROVIDERS.values() for v in cfg["env_vars"])
        )
        self._env_backup = {k: os.environ.get(k) for k in self._env_vars}
        for k in self._env_vars:
            os.environ.pop(k, None)
        self.client = TestClient(main.app)

    def _save(self, obj, attr, value):
        self._restore.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def patch_vc(self, **fns):
        for name, fn in fns.items():
            self._save(vc, name, fn)

    def make_project(self, pid: str):
        (self.projects_dir / pid).mkdir(parents=True, exist_ok=True)
        (self.projects_dir / pid / "PROJECT.md").write_text(f"# {pid}\n", encoding="utf-8")
        ws = self.execution_dir / pid
        (ws / "repo").mkdir(parents=True, exist_ok=True)
        (ws / "runs").mkdir(exist_ok=True)
        (ws / "logs").mkdir(exist_ok=True)

    def make_run(self, pid, run_id="run-1"):
        rec = RunRecord(
            run_id=run_id, project_id=pid, task_title="task", status=RunStatus.COMPLETED,
            summary="did things", branch="main", commit_sha="abc1234", pushed=True,
        )
        run_store.init_run_dir(pid, run_id)
        run_store.write_run_json(pid, run_id, rec)
        return rec

    def cleanup(self):
        import os

        for obj, attr, val in reversed(self._restore):
            setattr(obj, attr, val)
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


def _run(test_body):
    env = _Env()
    try:
        test_body(env)
    finally:
        env.cleanup()


# ---------- generic credentials / connectors ----------


def test_connector_credentials_routes():
    def body(env):
        env.make_project("p")
        # store a Vercel token via the generic route
        r = env.client.post("/api/projects/p/credentials/vercel", json={"fields": {"token": "vrc_tok_123"}})
        assert r.status_code == 200
        assert r.json()["configured"] is True
        assert "vrc_tok_123" not in r.text  # value never echoed
        # status_all lists every provider
        allc = env.client.get("/api/projects/p/connectors").json()
        assert set(allc) == set(credentials.PROVIDERS)
        assert allc["vercel"]["configured"] is True
        assert allc["github"]["configured"] is False
        # unknown provider -> 404
        assert env.client.get("/api/projects/p/credentials/bogus").status_code == 404

    _run(body)


def test_stripe_live_key_refused_via_route():
    def body(env):
        env.make_project("p")
        r = env.client.post(
            "/api/projects/p/credentials/stripe", json={"fields": {"secret_key": "sk_live_nope123"}}
        )
        assert r.status_code == 400
        # with the explicit allow_live opt-in it is accepted
        r2 = env.client.post(
            "/api/projects/p/credentials/stripe",
            json={"fields": {"secret_key": "sk_live_nope123"}, "allow_live": True},
        )
        assert r2.status_code == 200

    _run(body)


def test_env_routes_never_echo_value():
    def body(env):
        env.make_project("p")
        r = env.client.post(
            "/api/projects/p/env",
            json={"key": "DATABASE_URL", "value": "postgres://u:p@h/db", "secret": True},
        )
        assert r.status_code == 200
        assert "postgres://u:p@h/db" not in r.text
        listing = env.client.get("/api/projects/p/env").json()["vars"]
        assert any(v["key"] == "DATABASE_URL" and v["is_set"] for v in listing)
        assert "postgres://u:p@h/db" not in json.dumps(listing)
        # delete
        d = env.client.delete("/api/projects/p/env/DATABASE_URL")
        assert d.status_code == 200 and d.json()["removed"] is True

    _run(body)


# ---------- vercel deploy ----------


def _linked_status(*a, **k):
    return vc.VercelStatus(configured=True, connected=True, project_id="prj_1", org_id="team_1")


def test_deploy_preview_then_confirm_stamps_run_and_ledger():
    def body(env):
        env.make_project("p")
        env.make_run("p")
        credentials.set_credential("vercel", "p", fields={"token": "t"})
        credentials.update_metadata("vercel", "p", {"project_id": "prj_1", "org_id": "team_1"})
        env.patch_vc(
            status=_linked_status,
            create_deployment=lambda *a, **k: vc.DeploymentResult(
                ok=True, deployment_id="dpl_x", url="https://app-xyz.vercel.app", ready_state="READY"
            ),
        )
        # preview
        pv = env.client.post("/api/projects/p/execution/runs/run-1/vercel/deploy", json={"confirm": False})
        assert pv.status_code == 200
        c = pv.json()
        assert c["applied"] is False and c["contract"]["action"] == "deploy"
        assert c["contract"]["target"] == "preview" and c["contract"]["token_configured"] is True
        # confirm (finalize runs synchronously via the fake manager)
        cf = env.client.post("/api/projects/p/execution/runs/run-1/vercel/deploy", json={"confirm": True})
        assert cf.status_code == 200 and cf.json()["applied"] is True
        raw = run_store.read_run_json("p", "run-1")
        assert raw["deployment_id"] == "dpl_x"
        assert raw["deployment_url"] == "https://app-xyz.vercel.app"
        assert raw["deploy_state"] is None  # transient cleared
        ops = (env.projects_dir / "p" / "OPS.md").read_text(encoding="utf-8")
        assert "dpl_x" in ops and "## Ledger" in ops

    _run(body)


def test_concurrent_double_confirm_deploys_only_once():
    """T2.1: two confirms for one run must not both launch a Vercel deployment.
    With a deferred finalizer (deploy_state stays 'deploying'), the second
    confirm has to be rejected by the atomic per-run claim, and
    create_deployment is invoked exactly once."""
    class _DeferManager:
        def __init__(self):
            self.queued = []

        def submit(self, fn, *args, **kwargs):
            self.queued.append((fn, args, kwargs))
            return None

    def body(env):
        env.make_project("p")
        env.make_run("p")
        credentials.set_credential("vercel", "p", fields={"token": "t"})
        credentials.update_metadata("vercel", "p", {"project_id": "prj_1", "org_id": "team_1"})
        calls = {"n": 0}

        def _create(*a, **k):
            calls["n"] += 1
            return vc.DeploymentResult(
                ok=True, deployment_id="dpl_x", url="https://app.vercel.app", ready_state="READY"
            )

        env.patch_vc(status=_linked_status, create_deployment=_create,
                     get_deployment=lambda *a, **k: vc.DeploymentResult(
                         ok=True, deployment_id="dpl_x", url="https://app.vercel.app", ready_state="READY"))
        defer = _DeferManager()
        env._save(main, "get_default_manager", lambda: defer)

        # First confirm claims the deploy (deploy_state='deploying'); finalizer
        # is queued, NOT run, so the transient state is still set.
        cf1 = env.client.post("/api/projects/p/execution/runs/run-1/vercel/deploy", json={"confirm": True})
        assert cf1.status_code == 200 and cf1.json()["applied"] is True
        assert (run_store.read_run_json("p", "run-1") or {})["deploy_state"] == "deploying"

        # Second confirm, before the finalizer runs, must be rejected by the claim.
        cf2 = env.client.post("/api/projects/p/execution/runs/run-1/vercel/deploy", json={"confirm": True})
        assert cf2.status_code == 409, cf2.text

        # Exactly one finalizer was queued; run it and confirm one deployment.
        assert len(defer.queued) == 1
        fn, args, kwargs = defer.queued[0]
        fn(*args, **kwargs)
        assert calls["n"] == 1
        raw = run_store.read_run_json("p", "run-1")
        assert raw["deployment_id"] == "dpl_x" and raw["deploy_state"] is None

    _run(body)


def test_deploy_finalizer_preserves_concurrent_commit_fields():
    """T2.1: a commit/push landing on the run DURING the deploy poll must not be
    clobbered by the finalizer's write (it folds only deploy-owned fields)."""
    def body(env):
        env.make_project("p")
        env.make_run("p")
        credentials.set_credential("vercel", "p", fields={"token": "t"})
        credentials.update_metadata("vercel", "p", {"project_id": "prj_1", "org_id": "team_1"})

        # get_deployment mutates the on-disk record mid-poll to simulate a user
        # pushing a new commit while the finalizer is polling Vercel.
        def _get(*a, **k):
            def _push(r):
                r.head_commit = "newcommit999"
                r.pushed = True
                return r
            run_store.mutate_run_json("p", "run-1", _push)
            return vc.DeploymentResult(ok=True, deployment_id="dpl_x",
                                       url="https://app.vercel.app", ready_state="READY")

        env.patch_vc(
            status=_linked_status,
            create_deployment=lambda *a, **k: vc.DeploymentResult(
                ok=True, deployment_id="dpl_x", url=None, ready_state="BUILDING"),
            get_deployment=_get,
        )
        cf = env.client.post("/api/projects/p/execution/runs/run-1/vercel/deploy", json={"confirm": True})
        assert cf.status_code == 200
        raw = run_store.read_run_json("p", "run-1")
        # deploy stamped AND the concurrent push survived.
        assert raw["deployment_id"] == "dpl_x"
        assert raw["head_commit"] == "newcommit999" and raw["pushed"] is True

    _run(body)


def test_deploy_requires_token():
    def body(env):
        env.make_project("p")
        env.make_run("p")
        env.patch_vc(status=lambda *a, **k: vc.VercelStatus(configured=False))
        cf = env.client.post("/api/projects/p/execution/runs/run-1/vercel/deploy", json={"confirm": True})
        assert cf.status_code == 400

    _run(body)


def test_env_set_contract_hides_value():
    def body(env):
        env.make_project("p")
        env.client.post(
            "/api/projects/p/env",
            json={"key": "STRIPE_SECRET_KEY", "value": "sk_test_secretvalue", "secret": True},
        )
        env.patch_vc(status=_linked_status)
        pv = env.client.post("/api/projects/p/vercel/env/set", json={"key": "STRIPE_SECRET_KEY", "confirm": False})
        assert pv.status_code == 200
        c = pv.json()["contract"]
        assert c["type"] == "sensitive" and c["value_configured"] is True
        assert "sk_test_secretvalue" not in pv.text  # value never in the contract

    _run(body)


# ---------- 8.7 startup reconciliation of crashed external actions ----------


def test_reconcile_stuck_deploy_confirms_ready():
    def body(env):
        env.make_project("p")
        rec = RunRecord(
            run_id="run-1", project_id="p", task_title="t", status=RunStatus.COMPLETED,
            deploy_state="deploying", external_state="deploying", deployment_id="dpl_x",
        )
        run_store.init_run_dir("p", "run-1")
        run_store.write_run_json("p", "run-1", rec)
        env.patch_vc(get_deployment=lambda *a, **k: vc.DeploymentResult(
            ok=True, deployment_id="dpl_x", url="https://app.vercel.app", ready_state="READY"))
        fixed = main.reconcile_stuck_external_actions()
        assert "run-1" in fixed
        raw = run_store.read_run_json("p", "run-1")
        assert raw["deploy_state"] is None and raw["external_state"] is None
        assert raw["deployment_url"] == "https://app.vercel.app"
        assert not raw["blockers"]  # confirmed READY, no blocker

    _run(body)


def test_reconcile_stuck_migration_adds_verify_blocker():
    def body(env):
        env.make_project("p")
        rec = RunRecord(
            run_id="run-2", project_id="p", task_title="t", status=RunStatus.COMPLETED,
            external_state="migrating",
        )
        run_store.init_run_dir("p", "run-2")
        run_store.write_run_json("p", "run-2", rec)
        fixed = main.reconcile_stuck_external_actions()
        assert "run-2" in fixed
        raw = run_store.read_run_json("p", "run-2")
        assert raw["external_state"] is None
        assert any("partially applied" in b for b in raw["blockers"])

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
