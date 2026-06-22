"""Tests for Task 07.1 — pluggable model-provider selection.

Two layers:

- ``providers`` module: key-presence availability, default-provider preference
  order, the UI snapshot, completion dispatch to the right backend, and clean
  errors for unknown / unavailable providers. OpenAI/DeepSeek/Gemini response
  parsing is exercised by stubbing the HTTP layer; Anthropic dispatch is
  exercised by stubbing the SDK call. No network, no API keys needed.
- HTTP surface: ``GET /api/providers`` reflects env, and ``POST /api/chat``
  validates the provider (unknown → 400, unavailable → 400) and routes the
  orchestrated response to the selected provider.

Env vars are saved + restored around each test so the developer's real keys
aren't disturbed.

Run directly:
    python backend/tests/test_providers.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import providers  # noqa: E402
import llm  # noqa: E402
import orchestrator  # noqa: E402

_ALL_KEYS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
]


class _Keys:
    """Context manager that sets exactly the given provider keys, clearing the rest."""

    def __init__(self, **present: str) -> None:
        self._present = present
        self._saved: dict[str, str | None] = {}
        # Also save the model-override envs in case a test sets them.
        self._model_envs = list(providers._MODEL_ENV.values())

    def __enter__(self):
        for k in _ALL_KEYS + self._model_envs:
            self._saved[k] = os.environ.get(k)
            os.environ.pop(k, None)
        for k, v in self._present.items():
            os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------- availability + defaults ----------


def test_availability_follows_keys():
    with _Keys(OPENAI_API_KEY="sk-x"):
        assert providers.is_available("gpt") is True
        assert providers.is_available("claude") is False
        assert providers.is_available("gemini") is False
        assert providers.is_available("deepseek") is False


def test_default_prefers_claude():
    with _Keys(ANTHROPIC_API_KEY="a", OPENAI_API_KEY="b"):
        assert providers.default_provider() == "claude"


def test_default_first_available_when_no_claude():
    with _Keys(OPENAI_API_KEY="b", GOOGLE_API_KEY="c"):
        # PROVIDER_ORDER is claude, gpt, gemini, deepseek → gpt wins.
        assert providers.default_provider() == "gpt"


def test_default_falls_back_to_claude_when_none():
    with _Keys():
        assert providers.default_provider() == "claude"


def test_list_providers_shape_and_order():
    with _Keys(ANTHROPIC_API_KEY="a"):
        listed = providers.list_providers()
        assert [p["id"] for p in listed] == ["claude", "gpt", "gemini", "deepseek"]
        by_id = {p["id"]: p for p in listed}
        assert by_id["claude"]["available"] is True
        assert by_id["gpt"]["available"] is False
        # default_model is exposed + overridable via env.
        assert by_id["claude"]["default_model"]


def test_model_override_via_env():
    with _Keys(OPENAI_API_KEY="x"):
        os.environ["AGENT_OS_OPENAI_MODEL"] = "gpt-4o-mini"
        assert providers.default_model("gpt") == "gpt-4o-mini"


# ---------- dispatch + errors ----------


def test_complete_unknown_provider_raises():
    with _Keys(ANTHROPIC_API_KEY="a"):
        try:
            providers.complete("grok", "sys", [{"role": "user", "content": "hi"}])
            raise AssertionError("expected ProviderError")
        except providers.ProviderError:
            pass


def test_complete_unavailable_provider_raises():
    with _Keys(ANTHROPIC_API_KEY="a"):
        try:
            providers.complete("gpt", "sys", [{"role": "user", "content": "hi"}])
            raise AssertionError("expected ProviderError")
        except providers.ProviderError:
            pass


def test_complete_routes_to_anthropic():
    with _Keys(ANTHROPIC_API_KEY="a"):
        captured = {}

        def fake_anthropic(system, messages, model, max_tokens):
            captured["model"] = model
            return "claude says hi"

        prev = providers._complete_anthropic
        providers._complete_anthropic = fake_anthropic
        try:
            out = providers.complete(
                "claude", "sys", [{"role": "user", "content": "hi"}]
            )
        finally:
            providers._complete_anthropic = prev
        assert out == "claude says hi"
        assert captured["model"] == providers.default_model("claude")


def test_openai_compatible_parsing():
    with _Keys(OPENAI_API_KEY="sk-x"):
        prev = providers._http_post_json
        providers._http_post_json = lambda url, headers, payload: {
            "choices": [{"message": {"content": "gpt reply"}}]
        }
        try:
            out = providers.complete("gpt", "sys", [{"role": "user", "content": "hi"}])
        finally:
            providers._http_post_json = prev
        assert out == "gpt reply"


def test_deepseek_uses_openai_shape():
    with _Keys(DEEPSEEK_API_KEY="ds-x"):
        seen = {}

        def fake_post(url, headers, payload):
            seen["url"] = url
            seen["model"] = payload["model"]
            return {"choices": [{"message": {"content": "deepseek reply"}}]}

        prev = providers._http_post_json
        providers._http_post_json = fake_post
        try:
            out = providers.complete("deepseek", "sys", [{"role": "user", "content": "hi"}])
        finally:
            providers._http_post_json = prev
        assert out == "deepseek reply"
        assert "deepseek.com" in seen["url"]
        assert seen["model"] == providers.default_model("deepseek")


def test_gemini_parsing_and_role_mapping():
    with _Keys(GOOGLE_API_KEY="g-x"):
        seen = {}

        def fake_post(url, headers, payload):
            seen["payload"] = payload
            return {"candidates": [{"content": {"parts": [{"text": "gemini reply"}]}}]}

        prev = providers._http_post_json
        providers._http_post_json = fake_post
        try:
            out = providers.complete(
                "gemini",
                "sys",
                [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "yo"},
                    {"role": "user", "content": "again"},
                ],
            )
        finally:
            providers._http_post_json = prev
        assert out == "gemini reply"
        roles = [c["role"] for c in seen["payload"]["contents"]]
        assert roles == ["user", "model", "user"]  # assistant -> model
        assert seen["payload"]["system_instruction"]["parts"][0]["text"] == "sys"


def test_http_error_surfaces_as_provider_error():
    with _Keys(OPENAI_API_KEY="sk-x"):
        def boom(url, headers, payload):
            raise providers.ProviderError("HTTP 401 from openai: bad key")

        prev = providers._http_post_json
        providers._http_post_json = boom
        try:
            providers.complete("gpt", "sys", [{"role": "user", "content": "hi"}])
            raise AssertionError("expected ProviderError")
        except providers.ProviderError:
            pass
        finally:
            providers._http_post_json = prev


# ---------- vision (AI visual judgment) ----------


def test_vision_capability_flags():
    assert providers.is_vision_capable("claude") is True
    assert providers.is_vision_capable("gpt") is True
    assert providers.is_vision_capable("gemini") is True
    # DeepSeek's chat model is text-only.
    assert providers.is_vision_capable("deepseek") is False


def test_vision_available_and_default_provider():
    with _Keys(ANTHROPIC_API_KEY="a"):
        assert providers.vision_available() is True
        assert providers.default_vision_provider() == "claude"
    with _Keys(OPENAI_API_KEY="b", GOOGLE_API_KEY="c"):
        assert providers.default_vision_provider() == "gpt"  # preference order
    with _Keys(DEEPSEEK_API_KEY="d"):  # only a non-vision provider
        assert providers.vision_available() is False
        assert providers.default_vision_provider() is None
    with _Keys():
        assert providers.vision_available() is False
        assert providers.default_vision_provider() is None


def test_complete_vision_rejects_non_vision_provider():
    with _Keys(DEEPSEEK_API_KEY="d"):
        try:
            providers.complete_vision("deepseek", "sys", "hi", [("image/png", "AAAA")])
            raise AssertionError("expected ProviderError")
        except providers.ProviderError:
            pass


def test_complete_vision_unavailable_raises():
    with _Keys(ANTHROPIC_API_KEY="a"):  # gpt has no key
        try:
            providers.complete_vision("gpt", "sys", "hi", [("image/png", "AAAA")])
            raise AssertionError("expected ProviderError")
        except providers.ProviderError:
            pass


def test_complete_vision_routes_to_anthropic():
    with _Keys(ANTHROPIC_API_KEY="a"):
        captured = {}

        def fake(system, prompt, images, model, max_tokens):
            captured["images"] = images
            captured["model"] = model
            return "claude vision reply"

        prev = providers._complete_anthropic_vision
        providers._complete_anthropic_vision = fake
        try:
            out = providers.complete_vision(
                "claude", "sys", "describe", [("image/png", "AAAA"), ("image/png", "BBBB")]
            )
        finally:
            providers._complete_anthropic_vision = prev
        assert out == "claude vision reply"
        assert len(captured["images"]) == 2
        assert captured["model"] == providers.default_model("claude")


def test_complete_vision_openai_shape_has_image_url():
    with _Keys(OPENAI_API_KEY="sk-x"):
        seen = {}

        def fake_post(url, headers, payload):
            seen["payload"] = payload
            return {"choices": [{"message": {"content": "gpt vision reply"}}]}

        prev = providers._http_post_json
        providers._http_post_json = fake_post
        try:
            out = providers.complete_vision(
                "gpt", "sys", "describe", [("image/png", "AAAA")]
            )
        finally:
            providers._http_post_json = prev
        assert out == "gpt vision reply"
        content = seen["payload"]["messages"][-1]["content"]
        kinds = [c["type"] for c in content]
        assert "image_url" in kinds
        img = next(c for c in content if c["type"] == "image_url")
        assert img["image_url"]["url"].startswith("data:image/png;base64,AAAA")


def test_complete_vision_gemini_shape_has_inline_data():
    with _Keys(GOOGLE_API_KEY="g-x"):
        seen = {}

        def fake_post(url, headers, payload):
            seen["payload"] = payload
            return {"candidates": [{"content": {"parts": [{"text": "gemini vision reply"}]}}]}

        prev = providers._http_post_json
        providers._http_post_json = fake_post
        try:
            out = providers.complete_vision(
                "gemini", "sys", "describe", [("image/png", "AAAA")]
            )
        finally:
            providers._http_post_json = prev
        assert out == "gemini vision reply"
        parts = seen["payload"]["contents"][0]["parts"]
        assert any("inline_data" in p for p in parts)
        inline = next(p for p in parts if "inline_data" in p)
        assert inline["inline_data"]["mime_type"] == "image/png"
        assert inline["inline_data"]["data"] == "AAAA"


def test_chat_vision_uses_default_vision_provider():
    with _Keys(OPENAI_API_KEY="sk-x"):  # gpt is vision-capable
        captured = {}

        def fake_complete_vision(provider_id, system, prompt, images, model=None, max_tokens=2048):
            captured["provider"] = provider_id
            return "ok"

        prev = providers.complete_vision
        providers.complete_vision = fake_complete_vision
        try:
            out = llm.chat_vision("sys", "describe", [("image/png", "AAAA")])
        finally:
            providers.complete_vision = prev
        assert out == "ok"
        assert captured["provider"] == "gpt"


def test_chat_vision_raises_when_no_vision_provider():
    with _Keys(DEEPSEEK_API_KEY="d"):  # no vision-capable key
        try:
            llm.chat_vision("sys", "describe", [("image/png", "AAAA")])
            raise AssertionError("expected ProviderError")
        except providers.ProviderError:
            pass


# ---------- llm.chat delegation ----------


def test_llm_chat_uses_default_provider():
    with _Keys(OPENAI_API_KEY="sk-x"):
        captured = {}

        def fake_complete(provider_id, system, messages, model=None, max_tokens=2048):
            captured["provider"] = provider_id
            return "ok"

        prev = providers.complete
        providers.complete = fake_complete
        try:
            out = llm.chat("sys", [{"role": "user", "content": "hi"}])
        finally:
            providers.complete = prev
        assert out == "ok"
        assert captured["provider"] == "gpt"  # only OpenAI configured


def test_orchestrate_routes_selected_provider():
    # GENERAL skips the inspection loop → exactly one provider call.
    captured = {}

    def fake_complete(provider_id, system, messages, model=None, max_tokens=2048):
        captured.setdefault("providers", []).append(provider_id)
        return "routed reply"

    prev = providers.complete
    providers.complete = fake_complete
    try:
        text, inspected = orchestrator.orchestrate(
            orchestrator.GENERAL_PROJECT_ID, "hello", history=[], provider="gpt"
        )
    finally:
        providers.complete = prev
    assert text == "routed reply"
    assert inspected == []
    assert captured["providers"] == ["gpt"]


# ---------- HTTP surface ----------


def _make_env():
    import main  # noqa: WPS433
    import execution.manager as exec_manager  # noqa: WPS433
    import database  # noqa: WPS433

    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    projects_dir = root / "projects"
    execution_dir = root / "execution_workspaces"
    projects_dir.mkdir()
    execution_dir.mkdir()
    saved = {
        "projects": main.PROJECTS_DIR,
        "exec": exec_manager._EXECUTION_ROOT,
        "db": database.DB_PATH,
    }
    main.PROJECTS_DIR = projects_dir
    exec_manager._EXECUTION_ROOT = execution_dir
    database.DB_PATH = root / "agent_os.db"
    database.init_db()
    client = TestClient(main.app)
    return tmp, client, saved, projects_dir, main, database


def _restore_env(tmp, saved, main, exec_manager_db):
    import execution.manager as exec_manager  # noqa: WPS433
    import database  # noqa: WPS433

    main.PROJECTS_DIR = saved["projects"]
    exec_manager._EXECUTION_ROOT = saved["exec"]
    database.DB_PATH = saved["db"]
    tmp.cleanup()


def test_http_providers_endpoint_reflects_env():
    with _Keys(ANTHROPIC_API_KEY="a"):
        tmp, client, saved, _pd, main, database = _make_env()
        try:
            res = client.get("/api/providers")
            assert res.status_code == 200
            j = res.json()
            assert j["default"] == "claude"
            ids = {p["id"]: p["available"] for p in j["providers"]}
            assert ids == {
                "claude": True,
                "gpt": False,
                "gemini": False,
                "deepseek": False,
            }
        finally:
            _restore_env(tmp, saved, main, database)


def test_http_chat_unknown_provider_400():
    with _Keys(ANTHROPIC_API_KEY="a"):
        tmp, client, saved, _pd, main, database = _make_env()
        try:
            conv = database.create_conversation(orchestrator.GENERAL_PROJECT_ID, "c")
            res = client.post(
                "/api/chat",
                json={"conversation_id": conv["id"], "message": "hi", "provider": "grok"},
            )
            assert res.status_code == 400
        finally:
            _restore_env(tmp, saved, main, database)


def test_http_chat_unavailable_provider_400():
    with _Keys(ANTHROPIC_API_KEY="a"):  # gpt has no key
        tmp, client, saved, _pd, main, database = _make_env()
        try:
            conv = database.create_conversation(orchestrator.GENERAL_PROJECT_ID, "c")
            res = client.post(
                "/api/chat",
                json={"conversation_id": conv["id"], "message": "hi", "provider": "gpt"},
            )
            assert res.status_code == 400
        finally:
            _restore_env(tmp, saved, main, database)


def test_http_chat_routes_to_selected_provider():
    with _Keys(ANTHROPIC_API_KEY="a", OPENAI_API_KEY="b"):
        tmp, client, saved, _pd, main, database = _make_env()
        seen = {}

        def fake_complete(provider_id, system, messages, model=None, max_tokens=2048):
            seen.setdefault("calls", []).append(provider_id)
            # The memory-judge system prompt identifies itself as a "subsystem";
            # the main chat prompt does not. Return [] for the judge so memory
            # writeback is a no-op, and the routed reply for the main call.
            return "[]" if "subsystem" in system else "main reply"

        prev = providers.complete
        providers.complete = fake_complete
        try:
            conv = database.create_conversation(orchestrator.GENERAL_PROJECT_ID, "c")
            res = client.post(
                "/api/chat",
                json={"conversation_id": conv["id"], "message": "hi", "provider": "gpt"},
            )
            assert res.status_code == 200, res.text
            assert res.json()["content"] == "main reply"
            # The main orchestrated response routed to gpt.
            assert "gpt" in seen["calls"]
        finally:
            providers.complete = prev
            _restore_env(tmp, saved, main, database)


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
            import traceback

            traceback.print_exc()
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
