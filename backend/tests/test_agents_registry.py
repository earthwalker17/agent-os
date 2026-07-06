"""Tests for Phase 10 — the agent profile registry (agents_registry.py).

This file is also THE cross-registry sync test: profile commands/modes must
stay in lock-step with ``chat_delegation.MODE_COMMANDS`` and
``roles.ROLE_FOR_MODE`` (the registry links by string, not import — see the
module docstring).

Run:  python backend/tests/test_agents_registry.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from agents_registry import (  # noqa: E402
    AGENTS,
    STATUS_ACTIVE,
    STATUS_PLANNED,
    agents_for_ui,
    command_entries,
    get_agent,
    is_valid_slug,
    skill_ref,
)
from execution.chat_delegation import MODE_COMMANDS  # noqa: E402
from execution.roles import ROLE_FOR_MODE  # noqa: E402


# ---------- registry shape ----------


def test_ids_are_unique_valid_slugs():
    ids = [a.id for a in AGENTS]
    assert len(ids) == len(set(ids))
    for aid in ids:
        assert is_valid_slug(aid), aid


def test_commands_and_aliases_are_globally_unique():
    seen: set[str] = set()
    for agent in AGENTS:
        for cmd in (agent.command, *agent.aliases):
            if not cmd:
                continue
            assert cmd.startswith("@"), cmd
            assert cmd not in seen, f"duplicate command {cmd}"
            seen.add(cmd)


def test_skill_ids_are_valid_slugs_and_unique_per_agent():
    for agent in AGENTS:
        sids = [s.id for s in agent.skills]
        assert len(sids) == len(set(sids)), agent.id
        for sid in sids:
            assert is_valid_slug(sid), f"{agent.id}/{sid}"
            ref = agent.skills[sids.index(sid)]
            assert ref.title.strip(), f"{agent.id}/{sid}"
            assert ref.description.strip(), f"{agent.id}/{sid}"


def test_expected_ten_profiles_present():
    assert {a.id for a in AGENTS} == {
        "planner", "designer", "debugger", "reviewer", "inspector",
        "memory_steward", "researcher", "coder", "deploy_ops", "growth_launch",
    }


def test_every_active_profile_has_the_browser_facing_fields():
    for agent in AGENTS:
        if agent.status != STATUS_ACTIVE:
            continue
        assert agent.introduction.strip(), agent.id
        assert agent.approval_boundary.strip(), agent.id
        assert agent.use_cases, agent.id
        assert agent.responsibilities, agent.id
        assert agent.tool_categories, agent.id
        assert agent.skills, agent.id


# ---------- cross-registry sync (the load-bearing test) ----------


def test_every_mode_command_maps_to_exactly_one_active_profile():
    for cmd, mode in MODE_COMMANDS.items():
        owners = [
            a for a in AGENTS
            if a.status == STATUS_ACTIVE and cmd in (a.command, *a.aliases)
        ]
        assert len(owners) == 1, f"{cmd} owned by {[a.id for a in owners]}"
        assert owners[0].mode == mode, f"{cmd}: profile mode {owners[0].mode!r} != {mode!r}"


def test_every_profile_mode_is_a_real_mode_command():
    modes = set(MODE_COMMANDS.values())
    for agent in AGENTS:
        if agent.mode:
            assert agent.mode in modes, f"{agent.id} mode {agent.mode!r} has no @ command"


def test_profile_role_linkage_matches_role_for_mode():
    for agent in AGENTS:
        # A profile that carries a mode MUST carry a role_id (else the linkage
        # check below is vacuous), and it must match ROLE_FOR_MODE exactly.
        if agent.mode:
            assert agent.role_id, f"{agent.id} has a mode but no role_id"
            assert ROLE_FOR_MODE.get(agent.mode) == agent.role_id, agent.id


def test_coder_is_command_without_mode():
    coder = get_agent("coder")
    assert coder is not None
    assert coder.command == "@code"
    # @code dispatches (chat_delegation.is_code_delegation), it is not a mode.
    assert coder.mode == ""
    assert "@code" not in MODE_COMMANDS


def test_researcher_has_search_command_and_research_alias():
    researcher = get_agent("researcher")
    assert researcher is not None
    assert researcher.command == "@search"
    assert researcher.aliases == ("@research",)
    assert researcher.mode == "research"
    assert researcher.role_id == "researcher"


# ---------- capability sanity ----------


def test_capability_flags_are_honest():
    for agent in AGENTS:
        caps = agent.capabilities
        assert caps.searches_web == (agent.id == "researcher"), agent.id
        assert caps.writes_repo == (agent.id == "coder"), agent.id
        assert caps.dispatches_runs == (agent.id == "coder"), agent.id
        assert caps.deploys == (agent.id == "deploy_ops"), agent.id
        # read_only means "changes nothing anywhere" — incompatible with any
        # write/deploy/dispatch flag.
        if caps.read_only:
            assert not (
                caps.writes_memory or caps.writes_repo or caps.deploys
                or caps.dispatches_runs or caps.searches_web
            ), agent.id


def test_network_and_external_capable_agents_require_confirmation():
    assert get_agent("researcher").capabilities.requires_confirmation
    assert get_agent("deploy_ops").capabilities.requires_confirmation


def test_growth_launch_is_planned_and_inert():
    agent = get_agent("growth_launch")
    assert agent is not None
    assert agent.status == STATUS_PLANNED
    assert agent.command == ""
    assert agent.aliases == ()
    assert agent.skills == ()
    caps = agent.capabilities
    assert caps.read_only
    assert not any((
        caps.writes_memory, caps.writes_repo, caps.searches_web,
        caps.deploys, caps.dispatches_runs,
    ))


# ---------- lookups ----------


def test_get_agent_is_strict_and_normalizing():
    assert get_agent("planner").id == "planner"
    assert get_agent("  Planner ").id == "planner"
    assert get_agent("nope") is None
    assert get_agent("") is None
    assert get_agent(None) is None


def test_skill_ref_resolves_only_registered_pairs():
    assert skill_ref("planner", "task-breakdown-checklist") is not None
    assert skill_ref("planner", "TASK-BREAKDOWN-CHECKLIST") is not None
    assert skill_ref("planner", "code-review-rubric") is None  # reviewer's skill
    assert skill_ref("nope", "task-breakdown-checklist") is None
    assert skill_ref("planner", "../../etc/passwd") is None


def test_agents_for_ui_is_json_ready():
    dumped = agents_for_ui()
    assert len(dumped) == len(AGENTS)
    researcher = next(d for d in dumped if d["id"] == "researcher")
    # tuples must serialize as lists for the wire.
    assert isinstance(researcher["aliases"], list)
    assert isinstance(researcher["skills"], list)
    assert researcher["capabilities"]["searches_web"] is True


def test_command_entries_expand_aliases_and_skip_commandless():
    entries = command_entries()
    commands = [e["command"] for e in entries]
    assert len(commands) == len(set(commands))
    for cmd in MODE_COMMANDS:
        assert cmd in commands
    assert "@code" in commands
    alias_row = next(e for e in entries if e["command"] == "@research")
    assert alias_row["is_alias"] and alias_row["agent_id"] == "researcher"
    agent_ids = {e["agent_id"] for e in entries}
    assert "deploy_ops" not in agent_ids
    assert "growth_launch" not in agent_ids


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
