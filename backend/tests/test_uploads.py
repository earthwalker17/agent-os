"""Tests for Task 07.0 — chat attachment upload + storage.

Two layers:

- ``uploads`` module unit tests: filename sanitization (path stripping,
  traversal, allow-list, charset), per-directory de-duplication, chat-only
  storage, optional workspace copy via the sandbox, and safe re-resolution of
  a stored attachment path.
- HTTP tests over the FastAPI surface: ``POST /api/chat/upload`` (chat-only,
  add-to-workspace, GENERAL ignores the toggle, bad type rejected) and the
  attachment-serving GET endpoint.

All filesystem state is redirected into a temp dir by monkeypatching the
execution root (which both the workspace manager and the uploads module derive
their paths from). No LLM, no network.

Run directly:
    python backend/tests/test_uploads.py
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import uploads  # noqa: E402
import database  # noqa: E402
import execution.manager as exec_manager  # noqa: E402
from database import create_conversation  # noqa: E402
from orchestrator import GENERAL_PROJECT_ID  # noqa: E402


class _Env:
    """Temp-dir harness that redirects projects + execution roots + the DB.

    The DB path is monkeypatched to a temp file before ``init_db`` so the
    developer's real ``agent_os.db`` is never written to.
    """

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.projects_dir = root / "projects"
        self.execution_dir = root / "execution_workspaces"
        self.projects_dir.mkdir()
        self.execution_dir.mkdir()
        self._prev_projects = main.PROJECTS_DIR
        self._prev_exec = exec_manager._EXECUTION_ROOT
        self._prev_db = database.DB_PATH
        main.PROJECTS_DIR = self.projects_dir
        exec_manager._EXECUTION_ROOT = self.execution_dir
        database.DB_PATH = root / "agent_os.db"
        database.init_db()
        # The chat_uploads root is derived from execution root's parent.
        self.client = TestClient(main.app)

    def cleanup(self) -> None:
        main.PROJECTS_DIR = self._prev_projects
        exec_manager._EXECUTION_ROOT = self._prev_exec
        database.DB_PATH = self._prev_db
        self.tmp.cleanup()

    def make_project(self, pid: str) -> None:
        (self.projects_dir / pid).mkdir(parents=True, exist_ok=True)
        (self.projects_dir / pid / "PROJECT.md").write_text(f"# {pid}\n", encoding="utf-8")


def _run(body):
    env = _Env()
    try:
        body(env)
    finally:
        env.cleanup()


# ---------- unit: sanitize_filename ----------


def test_sanitize_strips_path_and_traversal():
    assert uploads.sanitize_filename("../../etc/passwd.txt") == "passwd.txt"
    assert uploads.sanitize_filename("a/b/c.png") == "c.png"
    assert uploads.sanitize_filename("a\\b\\c.md") == "c.md"


def test_sanitize_collapses_unsafe_chars():
    assert uploads.sanitize_filename("my report (v2).pdf") == "my_report_v2.pdf"


def test_sanitize_rejects_disallowed_extension():
    for bad in ("evil.exe", "script.sh", "noextension", "archive.zip"):
        try:
            uploads.sanitize_filename(bad)
            raise AssertionError(f"expected rejection for {bad!r}")
        except uploads.UploadError:
            pass


def test_sanitize_accepts_all_allowed_extensions():
    for ext in uploads.ALLOWED_EXTENSIONS:
        assert uploads.sanitize_filename(f"file{ext}") == f"file{ext}"


# ---------- unit: dedup + storage ----------


def test_chat_only_storage_and_dedup():
    def body(env: _Env):
        m1 = uploads.save_chat_attachment(
            conversation_id="conv1",
            project_id="p1",
            is_general=False,
            original_filename="note.txt",
            data=b"hello",
            content_type="text/plain",
            add_to_workspace=False,
        )
        assert m1["stored_filename"] == "note.txt"
        assert m1["added_to_workspace"] is False
        assert m1["scope"] == "chat"
        assert m1["size"] == 5
        assert m1["workspace_path"] is None
        # Second upload of the same name must NOT overwrite the first.
        m2 = uploads.save_chat_attachment(
            conversation_id="conv1",
            project_id="p1",
            is_general=False,
            original_filename="note.txt",
            data=b"world!!",
            content_type="text/plain",
            add_to_workspace=False,
        )
        assert m2["stored_filename"] != "note.txt"
        chat_dir = uploads.get_chat_uploads_dir("conv1")
        assert (chat_dir / m1["stored_filename"]).read_bytes() == b"hello"
        assert (chat_dir / m2["stored_filename"]).read_bytes() == b"world!!"

    _run(body)


def test_workspace_copy_lands_in_repo_uploads():
    def body(env: _Env):
        env.make_project("p1")
        meta = uploads.save_chat_attachment(
            conversation_id="conv1",
            project_id="p1",
            is_general=False,
            original_filename="diagram.png",
            data=b"\x89PNG",
            content_type="image/png",
            add_to_workspace=True,
        )
        assert meta["added_to_workspace"] is True
        assert meta["scope"] == "workspace"
        assert meta["workspace_path"] == "repo/uploads/diagram.png"
        repo_copy = env.execution_dir / "p1" / "repo" / "uploads" / "diagram.png"
        assert repo_copy.read_bytes() == b"\x89PNG"
        # Chat-only copy also exists.
        chat_copy = uploads.get_chat_uploads_dir("conv1") / meta["stored_filename"]
        assert chat_copy.exists()

    _run(body)


def test_workspace_flag_ignored_for_general():
    def body(env: _Env):
        meta = uploads.save_chat_attachment(
            conversation_id="convg",
            project_id=GENERAL_PROJECT_ID,
            is_general=True,
            original_filename="note.md",
            data=b"# hi",
            content_type="text/markdown",
            add_to_workspace=True,
        )
        assert meta["added_to_workspace"] is False
        assert meta["workspace_path"] is None

    _run(body)


def test_oversize_rejected():
    def body(env: _Env):
        big = b"x" * (uploads.MAX_FILE_BYTES + 1)
        try:
            uploads.save_chat_attachment(
                conversation_id="conv1",
                project_id="p1",
                is_general=False,
                original_filename="big.txt",
                data=big,
                add_to_workspace=False,
            )
            raise AssertionError("expected oversize rejection")
        except uploads.UploadError:
            pass

    _run(body)


def test_resolve_chat_attachment_blocks_traversal():
    def body(env: _Env):
        uploads.save_chat_attachment(
            conversation_id="conv1",
            project_id="p1",
            is_general=False,
            original_filename="ok.txt",
            data=b"data",
            add_to_workspace=False,
        )
        assert uploads.resolve_chat_attachment("conv1", "ok.txt") is not None
        # Traversal attempts resolve to the leaf only → not found, never escapes.
        assert uploads.resolve_chat_attachment("conv1", "../../secret.txt") is None
        assert uploads.resolve_chat_attachment("conv1", "missing.txt") is None

    _run(body)


# ---------- HTTP: /api/chat/upload ----------


def _upload(env: _Env, conv_id: str, files, *, add_to_workspace=False):
    data = {"conversation_id": conv_id, "add_to_workspace": str(add_to_workspace).lower()}
    return env.client.post("/api/chat/upload", data=data, files=files)


def test_http_upload_chat_only():
    def body(env: _Env):
        env.make_project("p1")
        conv = create_conversation("p1", "c")
        res = _upload(
            env,
            conv["id"],
            [("files", ("hello.txt", io.BytesIO(b"hi there"), "text/plain"))],
        )
        assert res.status_code == 200, res.text
        j = res.json()
        assert j["added_to_workspace"] is False
        att = j["attachments"][0]
        assert att["original_filename"] == "hello.txt"
        assert att["added_to_workspace"] is False
        # Serving endpoint returns the bytes.
        got = env.client.get(
            f"/api/conversations/{conv['id']}/attachments/{att['stored_filename']}"
        )
        assert got.status_code == 200
        assert got.content == b"hi there"

    _run(body)


def test_http_upload_to_workspace():
    def body(env: _Env):
        env.make_project("p1")
        conv = create_conversation("p1", "c")
        res = _upload(
            env,
            conv["id"],
            [("files", ("img.png", io.BytesIO(b"\x89PNG"), "image/png"))],
            add_to_workspace=True,
        )
        assert res.status_code == 200, res.text
        j = res.json()
        assert j["added_to_workspace"] is True
        att = j["attachments"][0]
        assert att["workspace_path"] == "repo/uploads/img.png"
        assert (env.execution_dir / "p1" / "repo" / "uploads" / "img.png").exists()

    _run(body)


def test_http_upload_general_ignores_workspace_flag():
    def body(env: _Env):
        conv = create_conversation(GENERAL_PROJECT_ID, "c")
        res = _upload(
            env,
            conv["id"],
            [("files", ("note.md", io.BytesIO(b"# x"), "text/markdown"))],
            add_to_workspace=True,
        )
        assert res.status_code == 200, res.text
        assert res.json()["added_to_workspace"] is False

    _run(body)


def test_http_upload_rejects_bad_type():
    def body(env: _Env):
        env.make_project("p1")
        conv = create_conversation("p1", "c")
        res = _upload(
            env,
            conv["id"],
            [("files", ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream"))],
        )
        assert res.status_code == 400

    _run(body)


def test_http_upload_unknown_conversation_404():
    def body(env: _Env):
        res = _upload(
            env,
            "does-not-exist",
            [("files", ("a.txt", io.BytesIO(b"x"), "text/plain"))],
        )
        assert res.status_code == 404

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
