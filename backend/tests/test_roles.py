"""Tests for Task 9.1 — the agent role registry (execution/roles.py).

Run:  python backend/tests/test_roles.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from execution.roles import (  # noqa: E402
    DEFAULT_ROLE,
    EXECUTION_ROLES,
    ROLE_FOR_MODE,
    allowed_tools_for,
    get_role,
    normalize_role_id,
    patch_coder_prompt,
)


# ---------- registry contracts ----------


def test_execution_roles_are_the_expected_minimum_set():
    assert EXECUTION_ROLES == {"coder", "reviewer", "inspector"}


def test_default_role_is_coder_with_full_tools_and_empty_overlay():
    role = get_role(DEFAULT_ROLE)
    assert role.id == "coder"
    assert not role.read_only
    assert role.executes_in_runs
    # Empty overlay keeps the existing system prompt byte-identical.
    assert role.prompt == ""
    assert role.allowed_tools == {
        "list_files", "read_file", "write_file", "append_file", "search_files", "run_shell",
    }


def test_reviewer_and_inspector_are_read_only_with_read_tools():
    for rid in ("reviewer", "inspector"):
        role = get_role(rid)
        assert role.read_only, rid
        assert role.executes_in_runs, rid
        assert role.allowed_tools == {"list_files", "read_file", "search_files"}, rid
        assert role.prompt.strip(), f"{rid} must carry a role-contract prompt"


def test_system_stage_roles_do_not_execute_in_runs():
    for rid in ("integrator", "verifier"):
        role = get_role(rid)
        # get_role falls back to coder for non-execution lookups only when the
        # id is unknown — integrator/verifier ARE known, just not assignable.
        assert role.id == rid
        assert not role.executes_in_runs
        assert rid not in EXECUTION_ROLES


def test_unknown_role_falls_back_to_coder():
    assert get_role("growth_hacker").id == "coder"
    assert get_role("").id == "coder"
    assert get_role(None).id == "coder"


# ---------- normalization (plan parsing) ----------


def test_normalize_role_id_accepts_known_execution_roles():
    assert normalize_role_id("reviewer") == "reviewer"
    assert normalize_role_id("  Inspector ") == "inspector"
    assert normalize_role_id("CODER") == "coder"


def test_normalize_role_id_rejects_non_execution_and_unknown():
    # System stages and chat-facing roles are not planner-assignable.
    assert normalize_role_id("integrator") == "coder"
    assert normalize_role_id("verifier") == "coder"
    assert normalize_role_id("planner") == "coder"
    assert normalize_role_id("banana") == "coder"
    assert normalize_role_id(None) == "coder"


# ---------- mode <-> role traceability ----------


def test_mode_mapping_covers_the_at_commands():
    assert ROLE_FOR_MODE["plan"] == "planner"
    assert ROLE_FOR_MODE["design"] == "designer"
    assert ROLE_FOR_MODE["debug"] == "debugger"
    assert ROLE_FOR_MODE["review"] == "reviewer"
    assert ROLE_FOR_MODE["inspect"] == "inspector"
    assert ROLE_FOR_MODE["memory"] == "memory_steward"


# ---------- enforced tool sets ----------


def test_allowed_tools_for_read_only_roles_is_read_set():
    assert allowed_tools_for("reviewer") == {"list_files", "read_file", "search_files"}
    assert allowed_tools_for("inspector") == {"list_files", "read_file", "search_files"}


def test_allowed_tools_for_coder_in_patch_workspace_drops_run_shell():
    full = allowed_tools_for("coder")
    patched = allowed_tools_for("coder", in_patch_workspace=True)
    assert "run_shell" in full
    assert "run_shell" not in patched
    assert patched == full - {"run_shell"}


def test_patch_coder_prompt_mentions_isolation_and_no_shell():
    text = patch_coder_prompt().lower()
    assert "patch workspace" in text
    assert "run_shell" in text


# ---------- standalone runner ----------


def _run_all() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    total = sum(1 for n, f in globals().items() if n.startswith("test_") and callable(f))
    if failures:
        print(f"\n{failures} of {total} tests failed.")
        return 1
    print(f"\nAll {total} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
