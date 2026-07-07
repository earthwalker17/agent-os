"""Tests for Phase 11 — lightweight interactive browser verification.

Standalone:  python tests/test_browser_interactions.py

Covers the Views/Flow grammar in ``## Browser Verification`` (backward
compatibility both directions), the parent-side credential fill guard, the
CaptureOutcome plumbing through ``_core_browser_verification`` (flow-failure
status folding, refused flows, legacy capturer adapters, evidence redaction),
the dict-manifest parsing of the default Playwright capture, and the
result.md render additions. Playwright is never imported.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from io import StringIO
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import execution.browser_verification as bv  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
from execution.browser_verification import (  # noqa: E402
    CaptureOutcome,
    FlowSpec,
    FlowStepSpec,
    ViewTarget,
    _screen_flows,
    parse_browser_verification,
    render_browser_verification_section,
    run_browser_verification,
)
from execution.models import (  # noqa: E402
    BrowserFlowResult,
    BrowserFlowStep,
    BrowserPageCapture,
    BrowserVerificationResult,
)
from execution.templates import render_task_md  # noqa: E402


# ---------- harness ----------


class _TempLayout:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.execution_dir = root / "execution_workspaces"
        self.execution_dir.mkdir()
        self._prev = exec_manager._EXECUTION_ROOT
        exec_manager._EXECUTION_ROOT = self.execution_dir

    def cleanup(self) -> None:
        exec_manager._EXECUTION_ROOT = self._prev
        self.tmp.cleanup()

    def init_workspace(self, project_id: str, *, task_md_body: str = "# TASK\n") -> Path:
        ws_dir = self.execution_dir / project_id
        repo_dir = ws_dir / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "AGENT.md").write_text("# AGENT\n", encoding="utf-8")
        (ws_dir / "TASK.md").write_text(task_md_body, encoding="utf-8")
        (ws_dir / "runs").mkdir(exist_ok=True)
        return repo_dir

    def make_run_dir(self, project_id: str, run_id: str) -> Path:
        d = self.execution_dir / project_id / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d


def _run(test_body):
    layout = _TempLayout()
    try:
        test_body(layout)
    finally:
        layout.cleanup()


class _FakeProc:
    def __init__(self) -> None:
        self.pid = 12345
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.stdout = StringIO("")
        self.stderr = StringIO("")
        self._wait_event = threading.Event()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        self._wait_event.set()

    def kill(self):
        self.killed = True
        self.returncode = 0
        self._wait_event.set()

    def send_signal(self, _sig):
        self.terminate()

    def wait(self, timeout=None):
        self._wait_event.wait(timeout=timeout if timeout is not None else 0.01)
        if self.returncode is None:
            raise __import__("subprocess").TimeoutExpired(cmd="fake", timeout=timeout or 0.01)
        return self.returncode


def _starter(proc):
    return lambda _cmd, _cwd: proc


def _always_ready(_url, _timeout):
    return True, "ok"


_TASK_MD_WITH_FLOWS = (
    "## Browser Verification\n\n"
    "```bash\nnpm run dev\nurl: http://127.0.0.1:5173\n```\n\n"
    "### Views\n"
    "- /settings\n\n"
    "### Flow: smoke\n"
    "- goto /\n"
    '- click "Add"\n'
    '- expect_text "Added"\n'
)


def _interactive_runner(
    *,
    fail_last_step: bool = False,
    console: list | None = None,
    network: list | None = None,
    seen: dict | None = None,
):
    """A modern capture stub: accepts views/flows, writes real PNG stubs."""

    def capture(
        url,
        screenshots_dir,
        *,
        max_pages=4,
        readiness_timeout_seconds=0,
        screenshot_timeout_seconds=0,
        views=None,
        flows=None,
    ):
        if seen is not None:
            seen["views"] = list(views or [])
            seen["flows"] = list(flows or [])
            seen["max_pages"] = max_pages
        screenshots_dir = Path(screenshots_dir)
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        (screenshots_dir / "browser.png").write_bytes(b"\x89PNGstub")
        pages = [
            BrowserPageCapture(
                path="screenshots/browser.png", label="Home",
                readiness="confirmed", nav_kind="primary",
            )
        ]
        for i, v in enumerate(views or [], start=2):
            fname = "view-%02d.png" % i
            (screenshots_dir / fname).write_bytes(b"\x89PNGstub")
            pages.append(
                BrowserPageCapture(
                    path=f"screenshots/{fname}", label=(v.label or v.path),
                    readiness="confirmed", nav_kind="view",
                )
            )
        flow_results = []
        for f in flows or []:
            steps = []
            failed = False
            for j, s in enumerate(f.steps):
                status = "passed"
                error = ""
                if failed:
                    status = "skipped"
                elif fail_last_step and j == len(f.steps) - 1:
                    status, error, failed = "failed", "element not found", True
                steps.append(
                    BrowserFlowStep(
                        action=s.action, target=s.target,
                        value_masked=("***" if s.value else ""),
                        status=status, error=error,
                    )
                )
            flow_results.append(
                BrowserFlowResult(
                    name=f.name,
                    status=("failed" if fail_last_step else "passed"),
                    steps=steps,
                )
            )
        return CaptureOutcome(
            pages=pages,
            flows=flow_results,
            console_errors=list(console or []),
            network_failures=list(network or []),
        )

    return capture


def _verify(layout, task_md, capturer, project="agent-os"):
    layout.init_workspace(project, task_md_body=task_md)
    run_dir = layout.make_run_dir(project, "r1")
    proc = _FakeProc()
    prev_wait = bv._wait_for_url
    bv._wait_for_url = _always_ready  # type: ignore[assignment]
    try:
        result = run_browser_verification(
            project,
            run_dir=run_dir,
            process_starter=_starter(proc),
            page_capture_runner=capturer,
        )
    finally:
        bv._wait_for_url = prev_wait  # type: ignore[assignment]
    return result, proc, run_dir


# ---------- parser: grammar + backward compatibility ----------


def test_parser_legacy_block_unchanged():
    task_md = (
        "## Browser Verification\n\n"
        "```bash\n# comment\nnpm run dev -- --port 5174\nurl: http://127.0.0.1:5174\n```\n"
    )
    cfg = parse_browser_verification(task_md)
    assert cfg is not None
    assert cfg.command == "npm run dev -- --port 5174"
    assert cfg.url == "http://127.0.0.1:5174"
    assert cfg.views == [] and cfg.flows == []


def test_parser_views_and_flows_grammar():
    cfg = parse_browser_verification(_TASK_MD_WITH_FLOWS)
    assert cfg is not None
    assert [v.path for v in cfg.views] == ["/settings"]
    assert len(cfg.flows) == 1
    flow = cfg.flows[0]
    assert flow.name == "smoke"
    assert [(s.action, s.target) for s in flow.steps] == [
        ("goto", "/"), ("click", "Add"), ("expect_text", "Added"),
    ]


def test_parser_view_labels_and_invalid_entries():
    task_md = (
        "## Browser Verification\n\n"
        "```bash\nnpm run dev\nurl: http://x:1\n```\n\n"
        "### Views\n"
        "- /reports — Reports page\n"
        "- notaview\n"
        "- http://evil.example.com/x\n"
        "- /two words\n"
    )
    cfg = parse_browser_verification(task_md)
    assert [(v.path, v.label) for v in cfg.views] == [("/reports", "Reports page")]


def test_parser_caps_enforced():
    views = "\n".join(f"- /v{i}" for i in range(10))
    flows = ""
    for i in range(4):
        steps = "\n".join(f'- click "b{j}"' for j in range(15))
        flows += f"### Flow: f{i}\n{steps}\n\n"
    task_md = (
        "## Browser Verification\n\n"
        "```bash\nnpm run dev\nurl: http://x:1\n```\n\n"
        f"### Views\n{views}\n\n{flows}"
    )
    cfg = parse_browser_verification(task_md)
    assert len(cfg.views) == bv.MAX_EXPLICIT_VIEWS
    assert len(cfg.flows) == bv.MAX_FLOWS
    assert all(len(f.steps) == bv.MAX_FLOW_STEPS for f in cfg.flows)


def test_parser_malformed_steps_skipped_and_empty_flow_dropped():
    task_md = (
        "## Browser Verification\n\n"
        "```bash\nnpm run dev\nurl: http://x:1\n```\n\n"
        "### Flow: good\n"
        '- click "Add"\n'
        '- dance "badly"\n'
        "- fill onlyonearg\n"
        "### Flow: hollow\n"
        "- explode\n"
    )
    cfg = parse_browser_verification(task_md)
    assert [f.name for f in cfg.flows] == ["good"]
    assert [(s.action, s.target) for s in cfg.flows[0].steps] == [("click", "Add")]


def test_parser_duplicate_flow_names_deduped():
    task_md = (
        "## Browser Verification\n\n"
        "```bash\nnpm run dev\nurl: http://x:1\n```\n\n"
        "### Flow: smoke\n"
        '- click "Add"\n'
        "### Flow: smoke\n"
        '- click "Other"\n'
    )
    cfg = parse_browser_verification(task_md)
    assert [f.name for f in cfg.flows] == ["smoke"]
    # The FIRST declaration wins; the duplicate collects nothing.
    assert [(s.action, s.target) for s in cfg.flows[0].steps] == [("click", "Add")]


def test_parser_next_section_terminates_subsections():
    task_md = (
        _TASK_MD_WITH_FLOWS + "\n## Something Else\n- goto /not-a-step\n"
    )
    cfg = parse_browser_verification(task_md)
    assert len(cfg.flows) == 1
    assert len(cfg.flows[0].steps) == 3


def test_parser_fenceless_block_with_subsections():
    task_md = (
        "## Browser Verification\n"
        "npm run dev\n"
        "url: http://127.0.0.1:5173\n\n"
        "### Views\n- /a\n"
    )
    cfg = parse_browser_verification(task_md)
    assert cfg.command == "npm run dev"
    assert [v.path for v in cfg.views] == ["/a"]


def test_template_comment_example_is_inert():
    # The shipped TASK.md template documents Views/Flows inside an HTML
    # comment; uncommenting only the command/url must not activate them.
    t = render_task_md("p1")
    assert parse_browser_verification(t) is None
    t2 = t.replace(
        "#   npm run dev -- --host 127.0.0.1 --port 5174",
        "npm run dev -- --host 127.0.0.1 --port 5174",
    ).replace("#   url: http://127.0.0.1:5174", "url: http://127.0.0.1:5174")
    cfg = parse_browser_verification(t2)
    assert cfg is not None
    assert cfg.views == [] and cfg.flows == []


# ---------- credential fill guard ----------


def test_fill_credential_shaped_value_refused_without_echo():
    flows = [FlowSpec(name="pay", steps=[
        FlowStepSpec("fill", "Name", "sk_test_51HAbCdEfGhIjKlMnOp"),
        FlowStepSpec("click", "Save"),
    ])]
    executable, refused = _screen_flows(flows, "proj")
    assert executable == []
    assert refused["pay"].status == "refused"
    assert refused["pay"].steps[0].status == "refused"
    assert refused["pay"].steps[1].status == "skipped"
    assert "sk_test_51HAbCdEfGhIjKlMnOp" not in json.dumps(
        [s.model_dump() for s in refused["pay"].steps]
    )


def test_fill_sensitive_target_refused():
    for target in ("Password", "API key", "Card number"):
        flows = [FlowSpec(name="f", steps=[FlowStepSpec("fill", target, "hello")])]
        executable, refused = _screen_flows(flows, "proj")
        assert executable == [] and refused["f"].status == "refused", target


def test_plain_fill_passes_screening():
    flows = [FlowSpec(name="ok", steps=[FlowStepSpec("fill", "Title", "My note")])]
    executable, refused = _screen_flows(flows, "proj")
    assert [f.name for f in executable] == ["ok"] and refused == {}


# ---------- core plumbing ----------


def test_flows_views_and_evidence_recorded_on_result():
    def body(layout):
        seen: dict = {}
        result, proc, _ = _verify(
            layout, _TASK_MD_WITH_FLOWS,
            _interactive_runner(console=["[Home] console.error: boom"],
                                network=["[Home] HTTP 500 GET /api"], seen=seen),
        )
        assert result.status == "passed"
        # Views threaded through + page budget enlarged for them.
        assert [v.path for v in seen["views"]] == ["/settings"]
        assert seen["max_pages"] == bv.MAX_BROWSER_PAGES + 1
        assert [f.name for f in seen["flows"]] == ["smoke"]
        assert [f.status for f in result.flows] == ["passed"]
        assert result.console_errors == ["[Home] console.error: boom"]
        assert result.network_failures == ["[Home] HTTP 500 GET /api"]
        assert any(p.nav_kind == "view" for p in result.pages)
        assert proc.terminated or proc.killed
    _run(body)


def test_failed_flow_fails_verification():
    def body(layout):
        result, proc, _ = _verify(
            layout, _TASK_MD_WITH_FLOWS, _interactive_runner(fail_last_step=True)
        )
        assert result.status == "failed"
        assert "smoke" in result.output_preview
        assert "expect_text Added" in result.output_preview
        # Evidence still recorded on a failed result.
        assert [f.status for f in result.flows] == ["failed"]
        assert proc.terminated or proc.killed
    _run(body)


def test_console_errors_alone_do_not_fail():
    def body(layout):
        result, _, _ = _verify(
            layout, _TASK_MD_WITH_FLOWS,
            _interactive_runner(console=["[Home] console.error: noisy lib"]),
        )
        assert result.status == "passed"
    _run(body)


def test_refused_flow_does_not_fail_verification():
    task_md = (
        "## Browser Verification\n\n"
        "```bash\nnpm run dev\nurl: http://127.0.0.1:5173\n```\n\n"
        "### Flow: login\n"
        '- fill "Password" "hunter2"\n'
        '- click "Sign in"\n'
    )

    def body(layout):
        seen: dict = {}
        result, _, _ = _verify(layout, task_md, _interactive_runner(seen=seen))
        assert result.status == "passed"
        # The refused flow never reached the capturer…
        assert seen["flows"] == []
        # …but is surfaced loudly on the result.
        assert [f.status for f in result.flows] == ["refused"]
        assert "hunter2" not in json.dumps(result.model_dump())
    _run(body)


def test_legacy_capturer_with_declared_flows_marks_them_skipped():
    def legacy(url, screenshots_dir, *, max_pages=4,
               readiness_timeout_seconds=0, screenshot_timeout_seconds=0):
        screenshots_dir = Path(screenshots_dir)
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        (screenshots_dir / "browser.png").write_bytes(b"\x89PNGstub")
        return [BrowserPageCapture(path="screenshots/browser.png", label="Home",
                                   readiness="confirmed", nav_kind="primary")]

    def body(layout):
        result, _, _ = _verify(layout, _TASK_MD_WITH_FLOWS, legacy)
        assert result.status == "passed"
        assert [f.status for f in result.flows] == ["skipped"]
        assert all(s.status == "skipped" for s in result.flows[0].steps)
    _run(body)


def test_evidence_strings_are_redacted_on_result():
    def body(layout):
        result, _, _ = _verify(
            layout, _TASK_MD_WITH_FLOWS,
            _interactive_runner(
                console=["[Home] leaked sk_test_51HAbCdEfGhIjKlMnOp value"]
            ),
        )
        assert result.status == "passed"
        assert "sk_test_51HAbCdEfGhIjKlMnOp" not in json.dumps(result.model_dump())
        assert any("[REDACTED]" in e for e in result.console_errors)
    _run(body)


# ---------- default Playwright capture: dict manifest parsing ----------


def test_default_capture_parses_dict_manifest():
    class _Completed:
        returncode = 0
        stderr = ""

        def __init__(self, stdout):
            self.stdout = stdout

    captured_cmd: dict = {}

    def fake_run(cmd, **kwargs):
        captured_cmd["payload"] = json.loads(cmd[3])
        sdir = Path(captured_cmd["payload"]["screenshots_dir"])
        (sdir / "browser.png").write_bytes(b"\x89PNGstub")
        (sdir / "flow-01-step-01.png").write_bytes(b"\x89PNGstub")
        manifest = {
            "pages": [
                {"file": "browser.png", "url": "http://x", "label": "Home",
                 "title": "T", "readiness": "confirmed", "nav_kind": "primary"},
            ],
            "flows": [
                {"name": "smoke", "status": "passed", "steps": [
                    {"action": "click", "target": "Add", "value_masked": "",
                     "status": "passed", "error": "", "screenshot": "flow-01-step-01.png"},
                ]},
            ],
            "console_errors": ["[Home] console.error: x"],
            "network_failures": [],
        }
        return _Completed(bv._CAPTURE_MANIFEST_MARKER + json.dumps(manifest))

    tmp = tempfile.TemporaryDirectory()
    prev_run = bv.subprocess.run
    bv.subprocess.run = fake_run  # type: ignore[assignment]
    try:
        outcome = bv._default_playwright_capture(
            "http://127.0.0.1:5173",
            Path(tmp.name),
            views=[ViewTarget(path="/a", label="A")],
            flows=[FlowSpec(name="smoke", steps=[FlowStepSpec("click", "Add")])],
        )
    finally:
        bv.subprocess.run = prev_run  # type: ignore[assignment]
        tmp.cleanup()

    assert isinstance(outcome, CaptureOutcome)
    assert outcome.pages[0].path == "screenshots/browser.png"
    assert outcome.flows[0].name == "smoke"
    assert outcome.flows[0].steps[0].screenshot == "screenshots/flow-01-step-01.png"
    assert outcome.console_errors == ["[Home] console.error: x"]
    # Payload carried the declared views/flows (values included pre-screened).
    assert captured_cmd["payload"]["views"] == [{"path": "/a", "label": "A"}]
    assert captured_cmd["payload"]["flows"][0]["name"] == "smoke"


def test_capture_script_is_valid_python():
    compile(bv._PLAYWRIGHT_CAPTURE_SCRIPT, "<capture-script>", "exec")


# ---------- render ----------


def test_render_section_includes_flows_and_evidence():
    result = BrowserVerificationResult(
        enabled=True, status="failed", command="npm run dev", url="http://x:1",
        flows=[BrowserFlowResult(name="smoke", status="failed", steps=[
            BrowserFlowStep(action="click", target="Add", status="failed", error="timeout"),
        ])],
        console_errors=["[Home] console.error: boom"],
        network_failures=["[Home] HTTP 500 GET /api"],
    )
    text = render_browser_verification_section(result)
    assert "Flow `smoke`" in text and "failed" in text
    assert "click Add" in text and "timeout" in text
    assert "Console errors" in text and "boom" in text
    assert "Network failures" in text


def test_render_section_byte_identical_without_new_fields():
    legacy = BrowserVerificationResult(
        enabled=True, status="passed", command="npm run dev",
        url="http://x:1", screenshot_path="screenshots/browser.png",
        duration_ms=1200, readiness="confirmed",
        pages=[BrowserPageCapture(path="screenshots/browser.png", label="Home",
                                  readiness="confirmed", nav_kind="primary")],
    )
    text = render_browser_verification_section(legacy)
    assert "Flow" not in text
    assert "Console errors" not in text
    assert "Network failures" not in text


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
