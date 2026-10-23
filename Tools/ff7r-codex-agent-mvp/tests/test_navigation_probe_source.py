from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "relative_path",
    (
        Path("Tools/ff7r-navigation-probe/Scripts/main.lua"),
        Path("Tools/ff7r-navigation-probe/Scripts/navmesh_path.lua"),
    ),
)
def test_probe_callbacks_use_persistent_scheduler(relative_path: Path) -> None:
    source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")

    assert "function FF7RReadOnlyTick()" in source
    assert "return false" in source


def test_local_probe_publishes_surface_normal_walkability_evidence() -> None:
    source = (
        PROJECT_ROOT / "Tools/ff7r-navigation-probe/Scripts/main.lua"
    ).read_text(encoding="utf-8")

    assert "ImpactNormal" in source
    assert "WALKABLE_NORMAL_Z_MIN" in source
    assert "blocking_hit" in source
    assert "walkable_surface_hit" in source
    assert "hit_slope_degrees" in source
    assert "walkable_surface_candidate" in source


def test_local_probe_contains_round_robin_capsule_sweep() -> None:
    source = (
        PROJECT_ROOT / "Tools/ff7r-navigation-probe/Scripts/main.lua"
    ).read_text(encoding="utf-8")

    assert "CapsuleTraceSingle" in source
    assert "SAMPLE_PERIOD_MS = 500" in source
    assert "CAPSULE_PROBE_DIRECTIONS" in source
    assert "capsule_direction_index" in source
    assert "CAPSULE_PROBE_INTERVAL_SECONDS" in source
    assert "capsule_probe_cycle" in source
    assert "capsule_probe_direction" in source
    assert "run_capsule_probe" in source
    assert "capsule_probe" in source
    assert "capsule_trace_unavailable" in source
    assert "hit.Distance" in source
    assert "For a capsule sweep" in source
    assert "CAPSULE_START_LIFT_CM" in source
    assert "CAPSULE_TRACE_DISTANCE_CM = 4000.0" in source
    assert "corridor_confirmed" in source


def test_local_probe_retains_failure_and_health_evidence() -> None:
    source = (PROJECT_ROOT / "Tools/ff7r-navigation-probe/Scripts/main.lua").read_text(encoding="utf-8")
    entry = source[source.index("function FF7RReadOnlyTick()") :]
    assert "pcall(write_probe)" in entry
    assert 'record_capture_failure("write_probe_exception"' in entry
    assert "pcall(function() maybe_write_health(false) end)" in entry
    assert "ExecuteInGameThread(" not in source
    assert "navigation-health.json" in source
    assert "navigation_output_write_failed" in source


def test_optional_navmesh_probe_is_slow_and_diagnostic():
    source = (
        PROJECT_ROOT / "Tools/ff7r-navigation-probe/Scripts/navmesh_path.lua"
    ).read_text(encoding="utf-8")

    assert "SAMPLE_PERIOD_MS = 2000" in source
    assert "navigation-path-status.json" in source
    assert "waiting_for_target" in source
    assert "path_ready" in source
    assert "unsupported" in source
    assert "FindPathToLocationSynchronously" in source
    assert "NavigationSystemBase:FindPathToLocationSynchronously" in source
    assert "Default__NavigationSystemV1" in source
    assert "ProjectPointToNavigation" in source
    assert "NavigationRaycast" in source
    assert "navigation_class" in source
    assert "function_object(context, table.unpack(call_args))" in source
    assert ":CallFunction(function_object" in source
    assert "path_function(world, pawn:GetActorLocation()" not in source
    assert "start_projection_failed" in source
    assert "target_projection_failed" in source
    assert "path_endpoint_mismatch" in source
    assert "producer_schema_version" in source
    navmesh_source = (
        PROJECT_ROOT / "Tools/ff7r-navigation-probe/Scripts/navmesh_path.lua"
    ).read_text(encoding="utf-8")
    assert "state.sample fields are" in navmesh_source
    assert "local pose = line:match" in navmesh_source
    assert "([%-]*)([%d%.]+)" in navmesh_source
    assert "navigation-ai-path-observation.enable" in navmesh_source
    assert 'AI_PATH_OBSERVATION_ENABLE_TOKEN = "FF7R_AI_PATH_OBSERVATION_ENABLE_V1"' in navmesh_source
    assert "run_ai_path_observation_if_requested()" in navmesh_source
    assert 'PATH_DIAGNOSTIC_ENABLE_TOKEN = "FF7R_PATH_DIAGNOSTIC_ENABLE_V1"' in navmesh_source
    assert "navigation-path-diagnostic.enable" in navmesh_source
    assert "read_exact_request_marker(" in navmesh_source
    tick = navmesh_source[navmesh_source.index("function FF7RReadOnlyTick()") :]
    assert "run_ai_path_observation_if_requested()" in tick
    assert "    run_ai_path_observation()" not in tick
    assert "run_path_diagnostic_if_requested()" in tick
    assert "    run_path_diagnostic()" not in tick


def test_map_surface_probe_is_paused_only_and_bounded():
    source = (
        PROJECT_ROOT / "Tools/ff7r-map-surface-probe/Scripts/main.lua"
    ).read_text(encoding="utf-8")

    assert "SAMPLE_PERIOD_MS = 2000" in source
    assert "MAX_BROAD_WIDGETS = 192" in source
    assert "if not live or not running or not paused" in source
    assert "MAX_EXACT_INSTANCES = 64" in source
    assert "context_progress < 2" in source
    assert "pending_sequence" in source
    assert "must not perform telemetry file I/O" in source
    assert "scan_policy=paused_only_exact_classes" in source
    assert "return true" in source
    assert '"paused"%s*:%s*true' in source


def test_ai_path_gate_deployer_is_read_only_by_default_and_one_file_only():
    source = (
        PROJECT_ROOT / "Tools/ff7r-navigation-probe/Deploy-AIPathObservationGate.ps1"
    ).read_text(encoding="utf-8")

    assert "FF7RNavmeshPathProbe\\scripts\\main.lua" in source
    assert "navmesh_path.lua" in source
    assert "Get-Process -Name 'ff7rebirth_', 'ff7rebirth'" in source
    assert "if (-not $Apply)" in source
    assert "writes_performed = $false" in source
    assert "[System.IO.File]::Replace($stagedPath, $destinationPath, $null)" in source
    assert "restored_after_failure" in source
    assert "AIPathObservationGate-before-{0}-{1}" in source
    assert "Get-Sha256 $backupPath" in source
    assert "Cannot safely restore while Rebirth or its launcher is running" in source
    assert "expectedInstalledSha256" in source
    assert "expectedSourceSha256" in source
    lua_source = (
        PROJECT_ROOT / "Tools/ff7r-navigation-probe/Scripts/navmesh_path.lua"
    ).read_bytes()
    expected_source_hash = hashlib.sha256(lua_source).hexdigest().upper()
    assert f"$expectedSourceSha256 = '{expected_source_hash}'" in source
    assert "object_refs_ready = $objectRefsMatches" in source
    assert "use Install-PersistentSamplers.ps1 for the paired deployment" in source
    assert "Fresh object lookup module changed after preflight" in source



def test_native_json_writer_retries_rename_on_sharing_violation():
    source = (
        PROJECT_ROOT / "Tools/ff7r-navigation-probe/Native/src/dllmain.cpp"
    ).read_text(encoding="utf-8")
    start = source.index("bool WriteJsonFile(")
    writer = source[start:source.index("bool WriteReport(", start)]

    assert "constexpr int MoveFileRetryAttempts = 40;" in source
    assert "constexpr DWORD MoveFileRetrySleepMs = 25;" in source
    assert "for (int attempt = 0; attempt < MoveFileRetryAttempts; ++attempt)" in writer
    assert "MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH" in writer
    assert "GetLastError()" in writer
    assert "ERROR_SHARING_VIOLATION" in writer
    assert "ERROR_ACCESS_DENIED" in writer
    assert "Sleep(MoveFileRetrySleepMs);" in writer
    # A failed rename keeps the .tmp, where the harness reads a stranded terminal status.
    assert "DeleteFileW" not in writer
