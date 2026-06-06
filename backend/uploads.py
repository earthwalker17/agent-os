"""Task 07.0 — chat attachment storage.

Handles files uploaded alongside a chat message. Two storage scopes:

- **chat-only** — kept under ``chat_uploads/{conversation_id}/`` at the repo
  root (sibling to ``projects/`` / ``execution_workspaces/``). Attached to the
  message but invisible to the Coding Agent. Works for project *and* GENERAL
  conversations.
- **workspace** — additionally copied into the project's execution workspace
  under ``repo/uploads/`` so the Coding Agent can see the file. Routed through
  ``ProjectSandbox.resolve_repo_path`` so the destination can never escape the
  repo. Only meaningful in project conversations; ignored for GENERAL.

Filenames are sanitized (basename only, safe charset, allow-listed extension)
and de-duplicated per directory, so an upload never overwrites an existing file
and a hostile name can't traverse out of the target directory. This module
deliberately knows nothing about HTTP — it takes raw bytes so it can be unit
tested without a TestClient.
"""

from __future__ import annotations

import mimetypes
import re
from datetime import datetime
from pathlib import Path

from execution.manager import (
    get_execution_root,
    get_project_execution_dir,
    init_execution_workspace,
)
from execution.sandbox import ProjectSandbox

# Allow-listed extensions per the task: common images + a few document types.
# Anything outside this set is rejected before a byte touches disk.
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_DOC_EXTENSIONS = {".txt", ".md", ".pdf", ".doc", ".docx"}
ALLOWED_EXTENSIONS = _IMAGE_EXTENSIONS | _DOC_EXTENSIONS

# Per-file size cap. Generous enough for screenshots / PDFs, small enough that a
# single multipart request can't exhaust memory or disk on a local machine.
MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB

# Where chat-only attachments live, mirroring the repo-relative layout used by
# the rest of the system. Derived from the execution root's parent so tests that
# monkeypatch ``_EXECUTION_ROOT`` redirect this too.
_CHAT_UPLOADS_DIRNAME = "chat_uploads"

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class UploadError(ValueError):
    """Raised when an uploaded file is rejected (bad name, type, or size)."""


def get_chat_uploads_root() -> Path:
    """Top-level directory holding all chat-only attachments."""
    return get_execution_root().parent / _CHAT_UPLOADS_DIRNAME


def get_chat_uploads_dir(conversation_id: str) -> Path:
    """Per-conversation chat-only attachment directory.

    ``conversation_id`` is sanitized to a bare token so it can't be used to
    traverse out of the uploads root.
    """
    token = _UNSAFE_CHARS.sub("_", (conversation_id or "").strip()) or "unknown"
    return get_chat_uploads_root() / token


def sanitize_filename(name: str) -> str:
    """Return a safe basename for ``name`` or raise ``UploadError``.

    Strips any directory components, collapses unsafe characters, and enforces
    the extension allow-list. The result is always a non-empty ``stem.ext``
    with a lowercase, allow-listed extension.
    """
    if not name or not isinstance(name, str):
        raise UploadError("filename is required")

    # Drop any path the client may have sent (both separators), keep the leaf.
    leaf = name.replace("\\", "/").split("/")[-1].strip()
    if not leaf or leaf in (".", ".."):
        raise UploadError(f"invalid filename: {name!r}")

    suffix = Path(leaf).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise UploadError(
            f"file type {suffix or '(none)'!r} is not allowed: {name!r}"
        )

    stem = Path(leaf).stem
    safe_stem = _UNSAFE_CHARS.sub("_", stem).strip("._-")
    if not safe_stem:
        safe_stem = "file"
    # Bound the stem length so a pathological name can't blow past OS limits.
    safe_stem = safe_stem[:120]
    return f"{safe_stem}{suffix}"


def _unique_path(directory: Path, filename: str) -> Path:
    """Return a non-colliding path inside ``directory`` for ``filename``.

    If the name is free, use it as-is. Otherwise insert a short timestamp
    suffix before the extension, and—on the unlikely chance that still
    collides—an incrementing counter.
    """
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = directory / f"{stem}-{stamp}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{stamp}-{counter}{suffix}"
        counter += 1
    return candidate


def _guess_mime(filename: str, provided: str | None) -> str:
    if provided and provided != "application/octet-stream":
        return provided
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or provided or "application/octet-stream"


def save_chat_attachment(
    *,
    conversation_id: str,
    project_id: str,
    is_general: bool,
    original_filename: str,
    data: bytes,
    content_type: str | None = None,
    add_to_workspace: bool = False,
) -> dict:
    """Persist one uploaded file and return its metadata.

    Always writes the chat-only copy. When ``add_to_workspace`` is set and the
    conversation belongs to a real project (not GENERAL), additionally copies
    the bytes into ``repo/uploads/`` via the sandbox. Raises ``UploadError`` on
    a rejected name/type or an oversize file.

    The returned dict carries everything the message metadata needs to render
    and re-serve the attachment: original + stored filenames, MIME type, size,
    the chat-relative reference, the workspace-relative path (if copied), and
    the effective scope.
    """
    if len(data) > MAX_FILE_BYTES:
        raise UploadError(
            f"file {original_filename!r} is too large "
            f"({len(data)} bytes > {MAX_FILE_BYTES} limit)"
        )

    stored_filename = sanitize_filename(original_filename)

    # 1. Chat-only copy (always).
    chat_dir = get_chat_uploads_dir(conversation_id)
    chat_path = _unique_path(chat_dir, stored_filename)
    chat_path.write_bytes(data)
    # The stored name may have been de-duplicated; keep them in sync so the
    # serving endpoint and the workspace copy use the resolved leaf.
    stored_filename = chat_path.name

    # 2. Optional workspace copy — project conversations only.
    workspace_path: str | None = None
    added_to_workspace = False
    if add_to_workspace and not is_general:
        # Ensure the workspace + repo/ exist before resolving a path into them.
        init_execution_workspace(project_id)
        sandbox = ProjectSandbox(project_id)
        uploads_dir = sandbox.resolve_repo_path("uploads")
        dest = _unique_path(uploads_dir, stored_filename)
        dest.write_bytes(data)
        # Confirm the resolved destination is still inside the repo sandbox.
        sandbox.resolve_repo_path(f"uploads/{dest.name}")
        workspace_path = f"repo/uploads/{dest.name}"
        added_to_workspace = True

    return {
        "original_filename": original_filename,
        "stored_filename": stored_filename,
        "mime_type": _guess_mime(stored_filename, content_type),
        "size": len(data),
        "scope": "workspace" if added_to_workspace else "chat",
        "added_to_workspace": added_to_workspace,
        "chat_path": f"{_CHAT_UPLOADS_DIRNAME}/{get_chat_uploads_dir(conversation_id).name}/{stored_filename}",
        "workspace_path": workspace_path,
    }


def resolve_chat_attachment(conversation_id: str, stored_filename: str) -> Path | None:
    """Resolve a stored chat attachment to an on-disk path, or ``None``.

    ``stored_filename`` is reduced to a bare leaf and re-joined under the
    conversation's uploads dir, so a traversal attempt can't escape it. Returns
    ``None`` when the file does not exist.
    """
    leaf = (stored_filename or "").replace("\\", "/").split("/")[-1].strip()
    if not leaf or leaf in (".", ".."):
        return None
    base = get_chat_uploads_dir(conversation_id).resolve()
    target = (base / leaf).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    if not target.exists() or not target.is_file():
        return None
    return target
