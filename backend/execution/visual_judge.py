"""AI visual judgment over browser-verification screenshots.

After a passing browser verification, this module asks a vision-capable model
to look at the captured page(s) plus the task context and judge whether the
generated app actually *looks usable* — loaded, visually coherent, relevant to
the task, and free of obvious broken states (spinner-only, blank page, error
overlay, missing content, wrong route). The HTTP-reachability gate and the
readiness-gated capture prove the server is up and the DOM rendered; this is
the layer that catches the rest.

Design posture mirrors command/browser verification and memory reconciliation:

  - **Diagnostic-only.** The verdict NEVER changes a run's status, never
    downgrades ``completed`` → ``partial``, and never adds blockers. It is an
    advisory signal surfaced alongside browser verification.
  - **Skips gracefully.** When no vision-capable provider key is configured (or
    there are no screenshots), it returns ``status="skipped"`` with a clear
    ``skipped_reason`` instead of failing the run.
  - **Best-effort.** Any exception is converted into an ``inconclusive`` /
    ``skipped`` result — visual judgment must never crash background
    finalization or the user-triggered verification endpoint.
  - **No chain-of-thought.** Only a concise, user-facing rationale is stored;
    the prompt explicitly forbids exposing reasoning steps.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional

import llm
import providers

from .models import BrowserVerificationResult, VisualReviewResult


log = logging.getLogger(__name__)


# Keep the request bounded: a smoke review needs only the captured views, and
# more images cost more tokens for little extra signal.
MAX_REVIEW_IMAGES = 4
# Skip any single screenshot larger than this so a runaway full-page capture
# can't blow past the provider's per-image limit (≈5 MB on the Anthropic API).
_MAX_IMAGE_BYTES = 4_500_000
_VISION_MAX_TOKENS = 1024
_VALID_VERDICTS = {"passed", "warning", "failed", "inconclusive"}

# Signature: (system, prompt, images, model=None, max_tokens=...) -> str, where
# ``images`` is a list of ``(media_type, base64_data)`` tuples. Defaults to
# ``llm.chat_vision``; tests swap in a stub so no real API call is made.
VisionCaller = Callable[..., str]


_SYSTEM_PROMPT = (
    "You are a meticulous QA reviewer. You are shown one or more screenshots of "
    "a web application that was just built, plus the task it was supposed to "
    "implement. Judge ONLY what is visible in the screenshots. Decide whether "
    "the app appears actually usable: fully loaded (not a loading spinner or "
    "skeleton), visually coherent, relevant to the task, and free of obvious "
    "broken states (blank page, error overlay/stack trace, missing content, or "
    "an obviously wrong route). Do not assume functionality you cannot see. "
    "Respond with ONLY a single JSON object and nothing else — no markdown "
    "fences, no commentary, no step-by-step reasoning."
)


def _build_user_prompt(task_card: str, summary: str, pages: list) -> str:
    page_lines = []
    for i, page in enumerate(pages, start=1):
        label = getattr(page, "label", "") or getattr(page, "path", "") or f"page {i}"
        readiness = getattr(page, "readiness", "") or "unknown"
        page_lines.append(f"  {i}. {label} (render readiness: {readiness})")
    pages_block = "\n".join(page_lines) or "  1. (entry page)"
    return (
        "# Task the app was built to satisfy\n"
        f"{(task_card or '(no task card)').strip()[:2000]}\n\n"
        "# Build summary\n"
        f"{(summary or '(no summary)').strip()[:1000]}\n\n"
        "# Screenshots (in order)\n"
        f"{pages_block}\n\n"
        "# Output contract\n"
        "Return EXACTLY this JSON shape:\n"
        "{\n"
        '  "verdict": "passed | warning | failed | inconclusive",\n'
        '  "headline": "<one short sentence>",\n'
        '  "reasoning": "<2-4 sentences, user-facing, no chain-of-thought>",\n'
        '  "evidence": ["<concrete thing you saw>", ...],\n'
        '  "pages": [{"label": "<page label>", "verdict": "passed|warning|failed|inconclusive", "note": "<short>"}]\n'
        "}\n\n"
        "Verdict guidance: 'passed' = looks loaded, coherent, and on-task; "
        "'warning' = renders but has visible rough edges (minor layout/empty "
        "sections); 'failed' = spinner-only, blank, error overlay, or clearly "
        "wrong/irrelevant; 'inconclusive' = you genuinely cannot tell."
    )


def _normalize_verdict(value: object) -> str:
    v = str(value or "").strip().lower()
    if v in _VALID_VERDICTS:
        return v
    if v in ("pass", "ok", "good"):
        return "passed"
    if v in ("fail", "broken", "error"):
        return "failed"
    if v in ("warn",):
        return "warning"
    return "inconclusive"


def _extract_json_object(text: str) -> Optional[dict]:
    """Tolerant parse: accept fenced/wrapped output, return the first JSON object."""
    if not text or not text.strip():
        return None
    body = text.strip()
    if body.startswith("```"):
        nl = body.find("\n")
        if nl != -1:
            body = body[nl + 1:]
        if body.endswith("```"):
            body = body[:-3]
        body = body.strip()
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced { ... } block.
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(body):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    parsed = json.loads(body[start:i + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _load_images(
    run_dir: Path, browser_result: BrowserVerificationResult
) -> list[tuple[str, str, str]]:
    """Read screenshots → list of ``(label, media_type, base64_data)`` (bounded)."""
    pages = list(browser_result.pages or [])
    if not pages and browser_result.screenshot_path:
        # Older result without a pages manifest — fall back to the primary path.
        from .models import BrowserPageCapture

        pages = [
            BrowserPageCapture(path=browser_result.screenshot_path, label="Home")
        ]
    images: list[tuple[str, str, str]] = []
    for page in pages[:MAX_REVIEW_IMAGES]:
        rel = getattr(page, "path", "") or ""
        if not rel:
            continue
        abs_path = run_dir / rel
        try:
            if not abs_path.exists() or not abs_path.is_file():
                continue
            data = abs_path.read_bytes()
        except Exception:  # noqa: BLE001
            continue
        if not data or len(data) > _MAX_IMAGE_BYTES:
            continue
        media_type = "image/png" if abs_path.suffix.lower() == ".png" else "image/jpeg"
        label = getattr(page, "label", "") or abs_path.name
        images.append((label, media_type, base64.standard_b64encode(data).decode("ascii")))
    return images


def _skipped(reason: str, duration_ms: Optional[int] = None) -> VisualReviewResult:
    return VisualReviewResult(
        enabled=True, status="skipped", skipped_reason=reason, duration_ms=duration_ms
    )


def run_visual_review(
    project_id: str,
    run_id: str,
    *,
    task_card: str,
    summary: str,
    browser_result: Optional[BrowserVerificationResult],
    run_dir: Path,
    vision_caller: Optional[VisionCaller] = None,
) -> VisualReviewResult:
    """Judge the captured screenshots with a vision model. Never raises.

    Returns ``status="skipped"`` (with ``skipped_reason``) when there is nothing
    to judge or no vision-capable provider is configured. Otherwise returns the
    model's verdict (``passed`` / ``warning`` / ``failed`` / ``inconclusive``).
    Persists ``visual_review.json`` next to the run's other artifacts.
    """
    start = time.perf_counter()
    try:
        if browser_result is None or not browser_result.enabled:
            return _skipped("browser verification did not run")
        if browser_result.status != "passed":
            return _skipped("browser verification did not pass; nothing to review")

        provider_id = providers.default_vision_provider()
        if not provider_id:
            return _skipped(
                "no vision-capable model provider configured "
                "(set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY)"
            )

        images = _load_images(run_dir, browser_result)
        if not images:
            return _skipped("no readable screenshots to review")

        caller = vision_caller or llm.chat_vision
        prompt = _build_user_prompt(task_card, summary, list(browser_result.pages or []))
        image_payload = [(media_type, data) for (_label, media_type, data) in images]

        try:
            raw = caller(
                _SYSTEM_PROMPT,
                prompt,
                image_payload,
                max_tokens=_VISION_MAX_TOKENS,
            )
        except Exception as exc:  # noqa: BLE001 — graceful skip, never fail the run
            log.warning("Visual review LLM call failed for %s/%s: %s", project_id, run_id, exc)
            return _skipped(f"visual review model call failed: {type(exc).__name__}")

        parsed = _extract_json_object(raw)
        duration_ms = int((time.perf_counter() - start) * 1000)
        if parsed is None:
            result = VisualReviewResult(
                enabled=True,
                status="inconclusive",
                headline="Could not parse the visual review response.",
                reasoning="The vision model did not return a usable verdict.",
                provider=provider_id,
                model=providers.default_model(provider_id),
                duration_ms=duration_ms,
            )
        else:
            evidence = parsed.get("evidence")
            evidence_list = (
                [str(e) for e in evidence][:8] if isinstance(evidence, list) else []
            )
            pages_field = parsed.get("pages")
            page_notes = []
            if isinstance(pages_field, list):
                for p in pages_field[:MAX_REVIEW_IMAGES]:
                    if isinstance(p, dict):
                        page_notes.append(
                            {
                                "label": str(p.get("label") or ""),
                                "verdict": _normalize_verdict(p.get("verdict")),
                                "note": str(p.get("note") or "")[:300],
                            }
                        )
            result = VisualReviewResult(
                enabled=True,
                status=_normalize_verdict(parsed.get("verdict")),
                headline=str(parsed.get("headline") or "")[:300],
                reasoning=str(parsed.get("reasoning") or "")[:1200],
                evidence=evidence_list,
                pages=page_notes,
                provider=provider_id,
                model=providers.default_model(provider_id),
                duration_ms=duration_ms,
            )

        _persist(run_dir, result)
        return result
    except Exception as exc:  # noqa: BLE001 — absolute outer guard
        log.exception("Visual review crashed for %s/%s", project_id, run_id)
        return _skipped(
            f"visual review crashed: {type(exc).__name__}",
            duration_ms=int((time.perf_counter() - start) * 1000),
        )


def _persist(run_dir: Path, result: VisualReviewResult) -> None:
    """Write ``visual_review.json`` alongside the run's other artifacts."""
    try:
        path = Path(run_dir) / "visual_review.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        import os

        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — persistence is best-effort
        pass


def render_visual_review_section(result: Optional[VisualReviewResult]) -> str:
    """Render a Markdown ``## Visual Review`` block for ``result.md``.

    Returns ``""`` when no review was attempted, so result.md for runs without
    visual review stays byte-identical to the legacy output.
    """
    if result is None or not result.enabled:
        return ""
    lines: list[str] = ["## Visual Review"]
    lines.append(f"- **Verdict**: {result.status}")
    if result.status == "skipped" and result.skipped_reason:
        lines.append(f"- **Skipped**: {result.skipped_reason}")
    if result.headline:
        lines.append(f"- **Headline**: {result.headline}")
    if result.provider:
        model = f" / {result.model}" if result.model else ""
        lines.append(f"- **Reviewed by**: {result.provider}{model}")
    if result.reasoning:
        lines.append("")
        lines.append(result.reasoning)
    if result.evidence:
        lines.append("")
        lines.append("Evidence:")
        lines.extend(f"- {e}" for e in result.evidence)
    return "\n".join(lines) + "\n"
