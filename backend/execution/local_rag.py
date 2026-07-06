"""Minimal local retrieval for the Main Agent (Phase 10.2).

A small, bounded, keyword-based retrieval layer — NOT a vector database and
NOT a full semantic index. It answers "what does this project already know
about X?" from three local, safe sources:

  1. **Project memory** — PROJECT/STATUS/DECISIONS/RESEARCH/LESSONS.md, scored
     per ``##`` section so the agent gets the relevant section, not the whole
     file.
  2. **Run history** — compact summaries of recent runs (status, title,
     summary, blockers) from ``run_store`` — the plans/failures/fixes record.
  3. **Repo map** — a bounded file tree plus lightweight summaries (headings /
     first lines) of a few relevant files, via the sandboxed ``inspect``
     readers (so ``.git`` / ``.env`` / credential files are never read).

Design constraints (all enforced here):
- **Bounded + inspect-like + auditable.** Per-source and per-turn char caps;
  every result is a compact, cited bundle, never a full-file/-repo dump.
- **No secrets.** Memory files carry no credentials; runs are already-redacted
  summaries; repo reads go through the sandbox (which rejects sensitive names).
- **Reuse sandbox/path safety.** Repo access delegates to ``inspect`` (which
  routes through ``ProjectSandbox`` / ``ToolRuntime``).
- **Never raises** into the caller — failures degrade to ``ok=False``.

Exposed to the Main Agent as a ``retrieve`` tool in the inspection channel and
as ``POST /api/projects/{id}/retrieve`` (audit/UI surface).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import credentials
from . import run_store
from . import sandbox as _sandbox

log = logging.getLogger(__name__)

# Retrieval reads arbitrary repo files by keyword, so its credential filter is
# BROADER than the sandbox's write-guard set: common credential-bearing files
# the sandbox doesn't classify (``.npmrc``/``id_rsa``/``credentials.json``/…)
# must never have their NAME or CONTENT surfaced in a retrieval hit.
_RAG_SENSITIVE_BASENAMES = frozenset({
    ".git", ".env", ".npmrc", ".pypirc", ".netrc", "_netrc", ".htpasswd",
    "credentials", "credentials.json", "credential", "secrets", "secrets.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".dockercfg",
})
_RAG_SENSITIVE_SUFFIXES = (
    ".key", ".pem", ".pfx", ".p12", ".p8", ".keystore", ".jks", ".crt", ".cer",
    ".ppk", ".asc", ".gpg",
)
# Substrings that mark a filename as credential-bearing regardless of extension.
_RAG_SENSITIVE_SUBSTRINGS = ("secret", "credential", "password", "id_rsa", "private_key", "privatekey")


def _is_sensitive_name(name: str) -> bool:
    """Defensive filter so a repo map never surfaces the NAME or CONTENT of a
    credential/ignored file. Broader than the sandbox set (the sandbox guards
    writes; retrieval must guard reads of the whole credential-file family),
    and Windows-normalized (trailing dots/spaces) to match sandbox behavior."""
    low = (name or "").strip().lower().rstrip(" .")
    if not low:
        return True
    if low in _RAG_SENSITIVE_BASENAMES or low in _sandbox._SENSITIVE_BASENAMES:
        return True
    if low.startswith(".env"):
        return True
    if low.endswith(_RAG_SENSITIVE_SUFFIXES) or low.endswith(_sandbox._SENSITIVE_SUFFIXES):
        return True
    return any(s in low for s in _RAG_SENSITIVE_SUBSTRINGS)

GENERAL_PROJECT_ID = "__GENERAL__"

# Repo-root projects/ (execution/local_rag.py -> execution/ -> backend/ ->
# repo root). Patchable for tests, mirroring the other project-memory readers.
_PROJECTS_DIR = Path(__file__).resolve().parent.parent.parent / "projects"

_MEMORY_FILES = ("PROJECT.md", "STATUS.md", "DECISIONS.md", "RESEARCH.md", "LESSONS.md")

# Caps (bounded evidence, never a dump).
RAG_MAX_MEMORY_HITS = 6
RAG_SNIPPET_CHARS = 500
RAG_MAX_RUNS = 5
RAG_MAX_REPO_FILES = 25
RAG_REPO_SUMMARY_LINES = 8
RAG_MAX_TOTAL_CHARS = 6000

VALID_KINDS = ("memory", "runs", "repo")

# Heavy / generated dirs the repo map should not descend into (noise + cost).
_SKIP_DIRS = frozenset({
    "node_modules", ".venv", "venv", "dist", "build", ".git", "__pycache__",
    ".next", ".cache", "coverage", ".pytest_cache", "target",
})

# Very common tokens carry no retrieval signal.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "into", "how", "what",
    "why", "when", "where", "does", "did", "are", "was", "were", "have", "has",
    "can", "could", "should", "would", "will", "about", "use", "using", "add",
    "make", "get", "set", "our", "your", "you", "there", "then", "than",
})


@dataclass
class RetrievalHit:
    source: str  # "memory:STATUS.md#Task Queue" | "run:<id>" | "repo:path"
    title: str
    snippet: str

    def to_dict(self) -> dict:
        return {"source": self.source, "title": self.title, "snippet": self.snippet}


@dataclass
class RetrievalResult:
    ok: bool
    query: str = ""
    hits: list[RetrievalHit] = field(default_factory=list)
    truncated: bool = False
    note: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "query": self.query,
            "hits": [h.to_dict() for h in self.hits],
            "truncated": self.truncated,
            "note": self.note,
            "error": self.error,
        }

    def to_text(self) -> str:
        """Compact, cited bundle for the LLM transcript."""
        if not self.ok:
            return f"LOCAL RETRIEVAL: ok=false\nerror: {self.error}"
        header = f"LOCAL RETRIEVAL: query={self.query!r}, hits={len(self.hits)}"
        if self.truncated:
            header += ", truncated=true"
        parts = [header]
        if self.note:
            parts.append(f"note: {self.note}")
        for h in self.hits:
            parts.append(f"\n[{h.source}] {h.title}\n{h.snippet}")
        if not self.hits:
            parts.append("(no local matches)")
        return "\n".join(parts)


# ---------- scoring ----------


def _tokens(query: str) -> list[str]:
    out: list[str] = []
    for raw in (query or "").lower().replace("/", " ").replace("_", " ").split():
        w = "".join(ch for ch in raw if ch.isalnum())
        if len(w) >= 3 and w not in _STOPWORDS and w not in out:
            out.append(w)
    return out


def _score(text: str, tokens: list[str]) -> int:
    if not tokens:
        return 0
    low = text.lower()
    return sum(low.count(t) for t in tokens)


def _snippet(text: str, tokens: list[str]) -> str:
    """A bounded window around the first token hit (or the head of the text)."""
    body = text.strip()
    if len(body) <= RAG_SNIPPET_CHARS:
        return body
    low = body.lower()
    pos = -1
    for t in tokens:
        i = low.find(t)
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        return body[:RAG_SNIPPET_CHARS].rstrip() + "…"
    start = max(0, pos - RAG_SNIPPET_CHARS // 3)
    end = min(len(body), start + RAG_SNIPPET_CHARS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return prefix + body[start:end].strip() + suffix


def _split_sections(text: str) -> list[tuple[str, str]]:
    """``(## heading, body)`` pairs; a leading preamble is kept under ``(intro)``."""
    sections: list[tuple[str, str]] = []
    heading = "(intro)"
    buf: list[str] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            if buf and "".join(buf).strip():
                sections.append((heading, "\n".join(buf).strip()))
            heading = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if buf and "".join(buf).strip():
        sections.append((heading, "\n".join(buf).strip()))
    return sections


# ---------- sources ----------


def search_memory(project_id: str, query: str) -> list[RetrievalHit]:
    """Top-scoring project-memory sections for the query."""
    tokens = _tokens(query)
    project_path = _PROJECTS_DIR / project_id
    scored: list[tuple[int, RetrievalHit]] = []
    for filename in _MEMORY_FILES:
        fpath = project_path / filename
        try:
            text = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
        except OSError:
            continue
        if not text.strip():
            continue
        for heading, body in _split_sections(text):
            score = _score(body, tokens) or _score(heading, tokens)
            # With no query tokens, surface the live status board / lessons.
            if not tokens and filename in ("STATUS.md", "LESSONS.md"):
                score = 1
            if score <= 0:
                continue
            scored.append((score, RetrievalHit(
                source=f"memory:{filename}#{heading}",
                title=f"{filename} › {heading}",
                snippet=_snippet(body, tokens),
            )))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [hit for _, hit in scored[:RAG_MAX_MEMORY_HITS]]


def recent_runs(project_id: str, query: str = "") -> list[RetrievalHit]:
    """Compact summaries of recent runs (newest first), query-preferred."""
    tokens = _tokens(query)
    try:
        runs = run_store.list_runs(project_id) or []
    except Exception:  # noqa: BLE001
        return []
    scored: list[tuple[int, int, RetrievalHit]] = []
    for idx, rec in enumerate(runs):
        title = str(rec.get("task_title") or "(untitled)")
        status = str(rec.get("status") or "?")
        summary = str(rec.get("summary") or "").strip()
        blockers = rec.get("blockers") or []
        files_n = len(rec.get("files_changed") or [])
        lines = [f"status={status}, files_changed={files_n}"]
        if summary:
            lines.append(_snippet(summary, tokens))
        for b in blockers[:3]:
            lines.append(f"blocker: {str(b)[:160]}")
        hit = RetrievalHit(
            source=f"run:{rec.get('run_id', '?')}",
            title=title,
            snippet="\n".join(lines),
        )
        score = _score(title + " " + summary, tokens)
        # newest-first tiebreak (negative idx) so recency wins with no query.
        scored.append((score, -idx, hit))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [hit for _, _, hit in scored[:RAG_MAX_RUNS]]


def _parse_entry(line: str) -> tuple[Optional[str], str]:
    """Parse one ``list_repo_files`` line (``[d] name`` / ``[f] name  (123b)``)."""
    s = line.strip()
    if s.startswith("[d]"):
        return "dir", s[3:].strip()
    if s.startswith("[f]"):
        rest = s[3:].strip()
        if rest.endswith("b)") and "(" in rest:
            rest = rest[:rest.rfind("(")].strip()
        return "file", rest
    return None, ""


def _walk_repo(inspect_mod, project_id: str) -> list[str]:
    """A bounded two-level file walk (relative paths), sandbox-safe."""
    files: list[str] = []
    root = inspect_mod.list_repo_files(project_id, ".")
    if not root.ok:
        return files
    dirs: list[str] = []
    for line in root.content.split("\n"):
        kind, name = _parse_entry(line)
        if not name or _is_sensitive_name(name):
            continue
        if kind == "file":
            files.append(name)
        elif kind == "dir" and name.lower() not in _SKIP_DIRS:
            dirs.append(name)
    # descend one level into a few directories
    for d in dirs[:8]:
        if len(files) >= RAG_MAX_REPO_FILES * 2:
            break
        sub = inspect_mod.list_repo_files(project_id, d)
        if not sub.ok:
            continue
        for line in sub.content.split("\n"):
            kind, name = _parse_entry(line)
            if kind == "file" and name and not _is_sensitive_name(name):
                files.append(f"{d}/{name}")
    return files


def repo_map(project_id: str, query: str = "") -> list[RetrievalHit]:
    """A bounded repo file map + lightweight summaries of a few relevant files.

    Delegates to the sandboxed ``inspect`` readers, so ``.git`` / ``.env`` /
    credential files are never surfaced.
    """
    # Lazy import breaks the inspect <-> local_rag cycle (inspect dispatches
    # a ``retrieve`` tool to us).
    from . import inspect as inspect_mod

    tokens = _tokens(query)
    files = _walk_repo(inspect_mod, project_id)
    if not files:
        return []

    tree = "\n".join(files[:RAG_MAX_REPO_FILES])
    hits: list[RetrievalHit] = [RetrievalHit(
        source="repo:tree",
        title="repo file map (top entries)",
        snippet=tree + ("\n…" if len(files) > RAG_MAX_REPO_FILES else ""),
    )]

    # Summarize a few files whose PATH matches the query (nothing without a
    # query signal — the map alone is the answer then).
    if tokens:
        ranked = sorted(files, key=lambda f: _score(f, tokens), reverse=True)
        picks = [f for f in ranked if _score(f, tokens) > 0][:3]
    else:
        picks = []
    for rel in picks:
        res = inspect_mod.read_repo_file(project_id, rel)
        if not res.ok or not res.content:
            continue
        head = "\n".join(res.content.split("\n")[:RAG_REPO_SUMMARY_LINES])
        hits.append(RetrievalHit(
            source=f"repo:{rel}",
            title=f"{rel} (first {RAG_REPO_SUMMARY_LINES} lines)",
            snippet=head[:RAG_SNIPPET_CHARS],
        ))
    return hits


# ---------- orchestration ----------


def retrieve(
    project_id: str,
    query: str,
    *,
    kinds: Optional[list[str]] = None,
) -> RetrievalResult:
    """Bounded local retrieval over memory + runs + repo. Never raises."""
    if project_id == GENERAL_PROJECT_ID or not project_id:
        return RetrievalResult(
            ok=False, query=query or "",
            error="local retrieval is project-scoped (no GENERAL workspace)",
        )
    if not _PROJECTS_DIR.exists() or not (_PROJECTS_DIR / project_id).exists():
        return RetrievalResult(ok=False, query=query or "", error="unknown project")

    wanted = [k for k in (kinds or VALID_KINDS) if k in VALID_KINDS] or list(VALID_KINDS)
    q = (query or "").strip()

    collected: list[RetrievalHit] = []
    try:
        if "memory" in wanted:
            collected += search_memory(project_id, q)
        if "runs" in wanted:
            collected += recent_runs(project_id, q)
        if "repo" in wanted:
            collected += repo_map(project_id, q)
    except Exception as exc:  # noqa: BLE001 — never raise into a chat turn
        log.warning("local_rag.retrieve failed for %s: %s", project_id, exc)
        return RetrievalResult(ok=False, query=q, error=f"retrieval error: {exc}")

    # Defense-in-depth egress guard: scrub any credential-shaped value from
    # every surfaced snippet/title before it leaves the module (run summaries or
    # repo file heads could carry one even though memory files shouldn't).
    for h in collected:
        h.snippet = credentials.redact(h.snippet, project_id)
        h.title = credentials.redact(h.title, project_id)

    # Enforce the per-turn total-char budget across all hits.
    hits: list[RetrievalHit] = []
    total = 0
    truncated = False
    for h in collected:
        cost = len(h.snippet) + len(h.title) + len(h.source)
        if total + cost > RAG_MAX_TOTAL_CHARS:
            truncated = True
            break
        hits.append(h)
        total += cost

    note = "retrieval truncated to fit the char budget" if truncated else ""
    return RetrievalResult(ok=True, query=q, hits=hits, truncated=truncated, note=note)
