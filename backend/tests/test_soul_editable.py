"""SOUL.md is user-editable through the Global Memory modal, yet remains
read-only to every LLM/judge writeback path.

Verifies the constitutional split introduced in the Public UI pass:
  - GET /api/global-memory includes SOUL.md (so the UI can show it).
  - POST /api/global-memory/update-file writes SOUL.md (the user's manual path).
  - orchestrator.apply_global_memory_update STILL refuses SOUL.md (the judge /
    reconciliation path is unchanged — SOUL.md is in no auto-writeback allow-list).

Run directly:
    python backend/tests/test_soul_editable.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import orchestrator  # noqa: E402


class _Env:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = Path(self.tmp.name) / "memory"
        self.mem.mkdir()
        for name, body in (
            ("SOUL.md", "# Soul\nOriginal identity.\n"),
            ("USER.md", "# User\n"),
            ("WORKSTYLE.md", "# Workstyle\n"),
            ("MEMORY.md", "# Memory\n"),
        ):
            (self.mem / name).write_text(body, encoding="utf-8")
        self._prev_main = main.MEMORY_DIR
        self._prev_orch = orchestrator.MEMORY_DIR
        main.MEMORY_DIR = self.mem
        orchestrator.MEMORY_DIR = self.mem
        self.client = TestClient(main.app)

    def cleanup(self) -> None:
        main.MEMORY_DIR = self._prev_main
        orchestrator.MEMORY_DIR = self._prev_orch
        self.tmp.cleanup()


def _run(body):
    env = _Env()
    try:
        body(env)
    finally:
        env.cleanup()


def test_global_memory_get_includes_soul():
    def body(env):
        r = env.client.get("/api/global-memory")
        assert r.status_code == 200
        data = r.json()
        assert "SOUL.md" in data
        assert "Original identity" in data["SOUL.md"]

    _run(body)


def test_user_can_edit_soul_via_endpoint():
    def body(env):
        r = env.client.post(
            "/api/global-memory/update-file",
            json={"filename": "SOUL.md", "content": "# Soul\nEdited by the user.\n"},
        )
        assert r.status_code == 200
        assert "Edited by the user" in (env.mem / "SOUL.md").read_text(encoding="utf-8")

    _run(body)


def test_judge_path_still_refuses_soul():
    def body(env):
        # The auto-writeback wrapper must reject SOUL.md (not in any allow-list)
        # and must not alter the file on disk.
        before = (env.mem / "SOUL.md").read_text(encoding="utf-8")
        ok = orchestrator.apply_global_memory_update(
            "SOUL.md", "Identity", "pwned", "replace"
        )
        assert ok is False
        assert (env.mem / "SOUL.md").read_text(encoding="utf-8") == before

    _run(body)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all soul-editable tests passed")
