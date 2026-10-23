from pathlib import Path


REPO_ROOT = Path(__file__).parents[3]
PROBE = REPO_ROOT / "Tools" / "ff7r-navigation-probe" / "Scripts" / "custom_ai_state.lua"
INSTALLER = REPO_ROOT / "Tools" / "ff7r-navigation-probe" / "Install-CustomAIStateProbe.ps1"
DISABLER = REPO_ROOT / "Tools" / "ff7r-navigation-probe" / "Disable-CustomAIStateProbe.ps1"


def test_custom_ai_probe_is_read_only_and_bounded():
    source = PROBE.read_text(encoding="utf-8")
    assert "SAMPLE_PERIOD_MS = 3000" in source
    assert "MAX_ATTEMPTS = 120" in source
    assert "MAX_NPC_SCAN = 96" in source
    assert 'TARGET_ACTOR_CLASS = "NP0114_00_BlackMantle_Standard_C"' in source
    assert 'FindAllOf("EndCharacter")' not in source
    assert 'movement_sent = false' in source
    assert 'ai_movement_request_sent = false' in source
    for forbidden in ("MoveToLocation", "TestMoveTo", "RequestAction", "SetValueAs"):
        assert forbidden not in source


def test_custom_ai_probe_targets_rebirth_state_and_generic_path_surfaces():
    source = PROBE.read_text(encoding="utf-8")
    for required in (
        "GetAIMoveMode",
        "GetAIActionState",
        "GetCurrentAIDefinitionTag",
        "GetMoveToMultiTargetName",
        "GetNextWarpPosition",
        "GetCurrentPath",
        "GetCurrentPathPoints",
        "GetCurrentPathIndex",
        "GetPathDestination",
        "EndAIMoveComponent",
        "EndAIActionComponent",
        "BlackboardAsset",
        "EndMobRouteSpline",
    ):
        assert required in source


def test_custom_ai_probe_preserves_differential_and_authored_route_evidence():
    source = PROBE.read_text(encoding="utf-8")
    assert "changed_fields" in source
    assert "authored_route_objects" in source
    assert "GetNumberOfSplinePoints" in source
    assert "GetLocationAtSplinePoint" in source
    assert "generic_unreal_path" in source
    assert "table.unpack({...})" not in source
    assert "local full_name = object_name(value)" not in source


def test_custom_ai_probe_installer_refuses_live_game_and_creates_backup():
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "Get-Process -Name 'ff7rebirth_', 'ff7rebirth'" in installer
    assert "FFVII Rebirth is still running; close it before deployment." in installer
    assert "DisabledRuntimeBackups" in installer


def test_custom_ai_probe_has_guarded_disable_path():
    disabler = DISABLER.read_text(encoding="utf-8")
    assert "Get-Process -Name 'ff7rebirth_', 'ff7rebirth'" in disabler
    assert "FFVII Rebirth is still running; close it before disabling the probe." in disabler
    assert "FF7RCustomAIStateProbe : 0" in disabler
    assert "DisabledRuntimeBackups" in disabler
