from __future__ import annotations

import json
from pathlib import Path

from ff7r_agent.mcp_runtime_server import stop_all_input
from ff7r_agent.runtime_bridge import RuntimeSupervisor
from ff7r_agent.runtime_commands import (
    LIFECYCLE_COMMANDS,
    SERVICE_ALIASES,
    SESSION_COMMANDS,
    SPECIAL_SERVICE_COMMANDS,
    STARTUP_COMMANDS,
)
from ff7r_agent.service import AgentService


MANIFEST = Path(__file__).parents[3] / "ProjectKnowledge" / "Current" / "canonical-project-state.json"


def test_golden_manifest_matches_allowlisted_runtime_operations() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    accepted = manifest["accepted_operations"]

    assert "canonical_commit_sha" not in manifest
    assert accepted["service_aliases"] == sorted(SERVICE_ALIASES)
    assert accepted["lifecycle"] == sorted(LIFECYCLE_COMMANDS)
    assert accepted["startup"] == sorted(STARTUP_COMMANDS)
    assert accepted["session"] == sorted(SESSION_COMMANDS)
    assert accepted["special"] == sorted(SPECIAL_SERVICE_COMMANDS)


def test_every_service_alias_targets_a_real_service_method() -> None:
    for operation, target in SERVICE_ALIASES.items():
        assert callable(getattr(AgentService, target, None)), (operation, target)


def test_dedicated_mcp_wrappers_target_valid_allowlisted_operations() -> None:
    wrappers = json.loads(MANIFEST.read_text(encoding="utf-8"))["accepted_operations"]["dedicated_mcp_wrappers"]
    assert wrappers["get_runtime_identity"] == "identity"
    assert wrappers["get_bootstrap_verification"] == "bootstrap_verification"
    assert callable(getattr(RuntimeSupervisor, wrappers["get_runtime_identity"], None))
    assert callable(getattr(RuntimeSupervisor, wrappers["get_bootstrap_verification"], None))
    assert wrappers["stop_all_input"] in SERVICE_ALIASES
    assert SERVICE_ALIASES[wrappers["stop_all_input"]] == "stop"
    assert callable(stop_all_input)
