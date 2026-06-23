"""Tests for AI visual judgment (``execution/visual_judge.py``).

Coverage:
  - gate: skipped when browser verification didn't pass / no screenshots /
    no vision-capable provider key configured.
  - happy path: a valid JSON verdict is parsed; evidence + per-page notes are
    captured; ``visual_review.json`` is persisted; provider/model recorded.
  - tolerance: fenced JSON is parsed; non-JSON output → ``inconclusive``;
    a caller exception → graceful ``skipped`` (never raises).
  - bounds: at most ``MAX_REVIEW_IMAGES`` images are sent to the model.
  - render: the result.md section renders for passed + skipped.

No real model call is made — a stub ``vision_caller`` is injected. Vision
provider availability is controlled by setting/clearing API-key env vars.

Run directly:
    python backend/tests/test_visual_judge.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.visual_judge as vj  # noqa: E402
from execution.models import BrowserPageCapture, BrowserVerificationResult  # noqa: E402
from execution.visual_judge import (  # noqa: E402
    render_visual_review_section,
    run_visual_review,
)


_VISION_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    # Provider Registry 2.0 — the new providers + accepted env aliases. Clearing
    # them keeps the "no vision key" gate deterministic on a dev box that has a
    # Kimi/GLM/Gemini key set.
    "MOONSHOT_API_KEY",
    "ZHIPUAI_API_KEY",
    "GEMINI_API_KEY",
    "KIMI_API_KEY",
    "ZAI_API_KEY",
)


class _Env:
    """Context-manager that sets exactly the given vision keys (clears the rest)."""

    def __init__(self, **keys: str) -> None:
        self._keys = keys
        self._prev: dict[str, str | None] = {}

    def __enter__(self) -> "_Env":
        for k in _VISION_ENV_KEYS:
            self._prev[k] = os.environ.get(k)
            os.environ.pop(k, None)
        for k, v in self._keys.items():
            os.environ[k] = v
        return self

    def __exit__(self, *exc) -> None:
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _passed_result(pages: list[BrowserPageCapture]) -> BrowserVerificationResult:
    return BrowserVerificationResult(
        enabled=True,
        command="npm run dev",
        url="http://127.0.0.1:5174",
        status="passed",
        screenshot_path=pages[0].path if pages else None,
        pages=pages,
        readiness="confirmed",
    )


def _write_pages(run_dir: Path, n: int) -> list[BrowserPageCapture]:
    shots = run_dir / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    pages: list[BrowserPageCapture] = []
    for i in range(n):
        name = "browser.png" if i == 0 else f"page-{i + 1:02d}.png"
        (shots / name).write_bytes(b"\x89PNG\r\n\x1a\nstub")
        pages.append(
            BrowserPageCapture(
                path=f"screenshots/{name}",
                label="Home" if i == 0 else f"View {i + 1}",
                readiness="confirmed",
            )
        )
    return pages


def _stub_caller(response: str, captured: dict | None = None):
    def caller(system, prompt, images, model=None, provider=None, max_tokens=2048):
        if captured is not None:
            captured["images"] = images
            captured["prompt"] = prompt
            captured["called"] = True
            captured["model"] = model
            captured["provider"] = provider
        return response

    return caller


# ---------- gate tests ----------


def test_skipped_when_no_vision_provider():
    with _Env():  # no keys at all
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            pages = _write_pages(run_dir, 1)
            captured = {"called": False}
            result = run_visual_review(
                "p",
                "r",
                task_card="build a dashboard",
                summary="done",
                browser_result=_passed_result(pages),
                run_dir=run_dir,
                vision_caller=_stub_caller("{}", captured),
            )
    assert result.status == "skipped"
    assert "vision" in result.skipped_reason.lower()
    assert captured["called"] is False  # model never called


def test_skipped_when_browser_not_passed():
    with _Env(ANTHROPIC_API_KEY="test"):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            pages = _write_pages(run_dir, 1)
            bad = _passed_result(pages)
            bad.status = "failed"
            result = run_visual_review(
                "p", "r", task_card="x", summary="x",
                browser_result=bad, run_dir=run_dir,
                vision_caller=_stub_caller("{}"),
            )
    assert result.status == "skipped"
    assert "pass" in result.skipped_reason.lower()


def test_skipped_when_no_screenshots():
    with _Env(ANTHROPIC_API_KEY="test"):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            empty = BrowserVerificationResult(
                enabled=True, status="passed", screenshot_path=None, pages=[]
            )
            result = run_visual_review(
                "p", "r", task_card="x", summary="x",
                browser_result=empty, run_dir=run_dir,
                vision_caller=_stub_caller("{}"),
            )
    assert result.status == "skipped"
    assert "screenshot" in result.skipped_reason.lower()


def test_skipped_when_browser_result_none():
    with _Env(ANTHROPIC_API_KEY="test"):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_visual_review(
                "p", "r", task_card="x", summary="x",
                browser_result=None, run_dir=Path(tmp),
                vision_caller=_stub_caller("{}"),
            )
    assert result.status == "skipped"


# ---------- verdict parsing ----------


def test_passed_verdict_parsed_and_persisted():
    response = json.dumps(
        {
            "verdict": "passed",
            "headline": "Dashboard renders fully.",
            "reasoning": "Header, metrics, and charts are all visible.",
            "evidence": ["Visible nav bar", "Populated metric cards"],
            "pages": [{"label": "Home", "verdict": "passed", "note": "ok"}],
        }
    )
    with _Env(ANTHROPIC_API_KEY="test"):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            pages = _write_pages(run_dir, 2)
            result = run_visual_review(
                "p", "r", task_card="build a dashboard", summary="done",
                browser_result=_passed_result(pages), run_dir=run_dir,
                vision_caller=_stub_caller(response),
            )
            assert result.status == "passed"
            assert result.headline.startswith("Dashboard")
            assert len(result.evidence) == 2
            assert result.enabled is True
            assert result.provider == "claude"
            assert result.model  # default model recorded
            assert len(result.pages) == 1
            # persisted artifact
            saved = json.loads((run_dir / "visual_review.json").read_text(encoding="utf-8"))
            assert saved["status"] == "passed"


def test_fenced_json_is_parsed():
    response = "```json\n" + json.dumps({"verdict": "warning", "headline": "rough"}) + "\n```"
    with _Env(ANTHROPIC_API_KEY="test"):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            pages = _write_pages(run_dir, 1)
            result = run_visual_review(
                "p", "r", task_card="x", summary="x",
                browser_result=_passed_result(pages), run_dir=run_dir,
                vision_caller=_stub_caller(response),
            )
    assert result.status == "warning"


def test_non_json_output_is_inconclusive():
    with _Env(ANTHROPIC_API_KEY="test"):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            pages = _write_pages(run_dir, 1)
            result = run_visual_review(
                "p", "r", task_card="x", summary="x",
                browser_result=_passed_result(pages), run_dir=run_dir,
                vision_caller=_stub_caller("The app looks great to me!"),
            )
    assert result.status == "inconclusive"


def test_unknown_verdict_normalizes_to_inconclusive():
    response = json.dumps({"verdict": "maybe", "headline": "unsure"})
    with _Env(ANTHROPIC_API_KEY="test"):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            pages = _write_pages(run_dir, 1)
            result = run_visual_review(
                "p", "r", task_card="x", summary="x",
                browser_result=_passed_result(pages), run_dir=run_dir,
                vision_caller=_stub_caller(response),
            )
    assert result.status == "inconclusive"


def test_caller_exception_skips_gracefully():
    def boom(system, prompt, images, model=None, provider=None, max_tokens=2048):
        raise RuntimeError("vision provider exploded")

    with _Env(ANTHROPIC_API_KEY="test"):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            pages = _write_pages(run_dir, 1)
            result = run_visual_review(
                "p", "r", task_card="x", summary="x",
                browser_result=_passed_result(pages), run_dir=run_dir,
                vision_caller=boom,
            )
    assert result.status == "skipped"
    assert "failed" in result.skipped_reason.lower()


def test_image_count_is_capped():
    captured: dict = {}
    response = json.dumps({"verdict": "passed", "headline": "ok"})
    with _Env(ANTHROPIC_API_KEY="test"):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            pages = _write_pages(run_dir, 6)  # more than the cap
            run_visual_review(
                "p", "r", task_card="x", summary="x",
                browser_result=_passed_result(pages), run_dir=run_dir,
                vision_caller=_stub_caller(response, captured),
            )
    assert captured["called"] is True
    assert len(captured["images"]) <= vj.MAX_REVIEW_IMAGES
    # each image is a (media_type, base64) tuple
    media, data = captured["images"][0]
    assert media == "image/png"
    assert isinstance(data, str) and data


def test_fallback_to_screenshot_path_without_pages():
    """An older result with only ``screenshot_path`` (no pages) is still reviewed."""
    response = json.dumps({"verdict": "passed", "headline": "ok"})
    captured: dict = {}
    with _Env(ANTHROPIC_API_KEY="test"):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_pages(run_dir, 1)  # writes screenshots/browser.png
            legacy = BrowserVerificationResult(
                enabled=True, status="passed",
                screenshot_path="screenshots/browser.png", pages=[],
            )
            result = run_visual_review(
                "p", "r", task_card="x", summary="x",
                browser_result=legacy, run_dir=run_dir,
                vision_caller=_stub_caller(response, captured),
            )
    assert result.status == "passed"
    assert len(captured["images"]) == 1


# ---------- model selection / capability gating ----------


def test_selected_vision_model_is_used():
    """A vision-capable selected provider/model is passed straight to the caller."""
    captured: dict = {}
    response = json.dumps({"verdict": "passed", "headline": "ok"})
    with _Env(ANTHROPIC_API_KEY="test"):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            pages = _write_pages(run_dir, 1)
            result = run_visual_review(
                "p", "r", task_card="x", summary="x",
                browser_result=_passed_result(pages), run_dir=run_dir,
                provider="claude", model="claude-sonnet-4-6",
                vision_caller=_stub_caller(response, captured),
            )
    assert captured["provider"] == "claude"
    assert captured["model"] == "claude-sonnet-4-6"
    assert result.provider == "claude"
    assert result.model == "claude-sonnet-4-6"


def test_text_selected_model_falls_back_to_provider_vision_model():
    """A text-only selected model resolves to that provider's vision model."""
    captured: dict = {}
    response = json.dumps({"verdict": "passed", "headline": "ok"})
    with _Env(ZHIPUAI_API_KEY="z"):  # GLM default (glm-5.2) is text-only
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            pages = _write_pages(run_dir, 1)
            result = run_visual_review(
                "p", "r", task_card="x", summary="x",
                browser_result=_passed_result(pages), run_dir=run_dir,
                provider="glm", model="glm-5.2",
                vision_caller=_stub_caller(response, captured),
            )
    assert captured["provider"] == "glm"
    assert captured["model"] == "glm-5v-turbo"  # GLM's vision model
    assert result.status == "passed"


def test_skipped_when_only_text_only_provider_available():
    """A provider with no vision model (DeepSeek) skips with a clear reason."""
    with _Env(DEEPSEEK_API_KEY="d"):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            pages = _write_pages(run_dir, 1)
            captured = {"called": False}
            result = run_visual_review(
                "p", "r", task_card="x", summary="x",
                browser_result=_passed_result(pages), run_dir=run_dir,
                provider="deepseek", model="deepseek-v4-flash",
                vision_caller=_stub_caller("{}", captured),
            )
    assert result.status == "skipped"
    assert captured["called"] is False


# ---------- render ----------


def test_render_section_passed():
    from execution.models import VisualReviewResult

    r = VisualReviewResult(
        enabled=True, status="passed", headline="Looks good",
        reasoning="All sections render.", evidence=["nav present"],
        provider="claude", model="claude-sonnet-4-6",
    )
    text = render_visual_review_section(r)
    assert "## Visual Review" in text
    assert "passed" in text
    assert "Looks good" in text
    assert "nav present" in text


def test_render_section_skipped_and_absent():
    from execution.models import VisualReviewResult

    skipped = VisualReviewResult(
        enabled=True, status="skipped", skipped_reason="no vision key"
    )
    text = render_visual_review_section(skipped)
    assert "skipped" in text.lower()
    assert "no vision key" in text
    # absent / disabled → empty so result.md stays unchanged for old runs
    assert render_visual_review_section(None) == ""


# ---------- runner ----------


def _run_all() -> int:
    tests = [
        v for k, v in globals().items() if k.startswith("test_") and callable(v)
    ]
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
