from __future__ import annotations

from pathlib import Path

from ff7r_agent.live_route_adapter import (
    DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH,
    DEFAULT_TOPOLOGY_PROBE_DLL_PATH,
)
from ff7r_agent.validator_prewarm import default_validated_navigation_resource_key


def test_default_resource_key_matches_runtime_command_defaults():
    assert default_validated_navigation_resource_key() == (
        str(Path(DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH).resolve()),
        str(Path(DEFAULT_TOPOLOGY_PROBE_DLL_PATH).resolve()),
        None,
        None,
        None,
        "False",
    )
