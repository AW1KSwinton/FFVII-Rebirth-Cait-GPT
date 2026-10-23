from pathlib import Path


REPO_ROOT = Path(__file__).parents[3]
PROBE = REPO_ROOT / "Tools" / "ff7r-navigation-probe" / "Scripts" / "custom_nav_api_probe.lua"


def test_custom_nav_api_probe_is_one_shot_read_only_and_separate_from_production_route():
    source = PROBE.read_text(encoding="utf-8")
    executable_lines = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("--")
    )

    assert "navigation-custom-api.enable" in source
    assert "os.remove(MARKER_PATH)" in source
    assert "SAMPLE_PERIOD_MS = 1000" in source
    assert "navigation-custom-api.json" in source
    assert "navigation-path.json" not in executable_lines
    assert "movement_sent = false" in source
    assert "ai_movement_request_sent = false" in source
    assert "function FF7RReadOnlyTick()" in source
    assert "LoopAsync(" not in source
    for forbidden in ("MoveToLocation", "TestMoveTo", "FindAllOf", "SetValueAs"):
        assert forbidden not in executable_lines


def test_custom_nav_api_probe_preserves_exact_static_signatures_and_controlled_queries():
    source = PROBE.read_text(encoding="utf-8")
    for required in (
        "/Script/EndGame.Default__EndNavAPI",
        "/Script/EndGame.EndNavAPI:GetAgentPropety",
        "/Script/EndGame.EndNavAPI:FindNearestNavMeshPoint",
        "/Script/EndGame.EndNavAPI:FindNearestNavMeshPointAgent",
        "FNavAgentProperties GetAgentPropety(EEndNavLayerType InLayerType)",
        "FVector& OutLocation",
        "agent_properties",
        "current",
        "nearby_x_5m",
        "high_invalid_20m",
        "filter_type = 0",
        "modifier_name = \"None\"",
    ):
        assert required in source


def test_custom_nav_api_probe_fails_closed_and_keeps_invocation_errors():
    source = PROBE.read_text(encoding="utf-8")
    assert "ufunction_call_with_context" in source
    assert "context_call_function" in source
    assert "CallFunction fallback:" in source
    assert "unhandled_probe_error" in source
    assert "status = \"probe_error\"" in source
    assert "parameter_metadata = \"not_called_on_live_ufunction_proxy\"" in source


def test_custom_nav_api_probe_disables_nonreturning_live_projection_by_default():
    source = PROBE.read_text(encoding="utf-8")
    assert "LIVE_REFLECTED_PROJECTION_CALLS_ENABLED = false" in source
    assert "reflected_projection_invocation_disabled_after_nonreturning_call" in source
    assert "last_checkpoint = \"query_begin=current\"" in source
