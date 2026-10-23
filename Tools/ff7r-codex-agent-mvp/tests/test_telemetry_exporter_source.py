from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[2]
BASE_CPP = ROOT / "ff7r-telemetry-exporter" / "Base" / "src" / "dllmain.cpp"
BASE_LUA = ROOT / "ff7r-telemetry-exporter" / "Base" / "Scripts" / "main.lua"
BASE_INSTALLER = ROOT / "ff7r-telemetry-exporter" / "Install-BaseTelemetryExporter.ps1"
SIDECAR_LUA = ROOT / "ff7r-telemetry-exporter" / "Scripts" / "main.lua"


def test_authoritative_base_source_is_present_and_distinct_from_sidecar() -> None:
    base = BASE_LUA.read_text(encoding="utf-8")
    sidecar = SIDECAR_LUA.read_text(encoding="utf-8")
    assert r'local SAMPLE_PATH = "D:\\D' not in base
    assert r'local SAMPLE_PATH = "C:\\CaitGPT\\Telemetry\\state.sample"' in base
    assert "LoopAsync(" not in base
    assert "ExecuteInGameThread(" not in base
    assert "function FF7RReadOnlyTick()" in base
    assert "GetCharaHP" not in base
    assert "GetHPMax" not in base
    assert r'local SIDECAR_PATH = "C:\\CaitGPT\\Telemetry\\surroundings.json"' in sidecar
    assert base != sidecar


def test_native_source_uses_precise_handoff_timestamp_and_atomic_publication() -> None:
    source = BASE_CPP.read_text(encoding="utf-8") + (BASE_CPP.parent / "sample_format.hpp").read_text(encoding="utf-8")
    assert "%04u-%02u-%02uT%02u:%02u:%02u.%03uZ" in source
    assert "sampled_at_utc = utc_timestamp(handoff_time)" in source
    assert r'json += ",\"timestamp_utc\":\""' in source
    assert r'json += "\",\"sampled_at_utc\":\""' in source
    assert r'json += "\",\"source_timestamp_utc\":\""' in source
    assert r'json += "\",\"published_at_utc\":\""' in source
    assert r'json += "\",\"producer_elapsed_ms\":"' in source
    assert "state.json.tmp" in (ROOT / "ff7r-telemetry-exporter" / "Base" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH" in source
    assert "source_timestamp_is_recent" in source
    assert "2ULL * 10'000'000ULL" in source


def test_native_source_distinguishes_unknown_combat_from_observed_inactive() -> None:
    source = BASE_CPP.read_text(encoding="utf-8")
    assert r'json += ",\"combat_state_observed\":"' in source
    assert 'sample && sample->in_combat.has_value() ? "true" : "false"' in source


def test_base_sampler_timestamp_is_taken_after_gameplay_reads() -> None:
    source = BASE_LUA.read_text(encoding="utf-8")
    query_end = source.index('local locomotion = safe_call("", function() return encode_locomotion(leader_actor) end)')
    assert source.index("local function sample_on_game_thread(generation)") < query_end
    assert query_end < source.index("pcall(entity_poi.sample", query_end)
    assert source.index('os.date("!%Y-%m-%dT%H:%M:%SZ")', query_end) < source.index(
        "local file = io.open(SAMPLE_PATH", query_end
    )
    assert 'type(poi_error) == "string" then source_timestamp = poi_error' in source





def test_base_exporter_health_is_bounded_and_diagnostic_only() -> None:
    source = BASE_LUA.read_text(encoding="utf-8")

    assert 'HEALTH_PATH = "C:\\\\CaitGPT\\\\Telemetry\\\\telemetry-health.json"' in source
    assert "callback_attempts" in source
    assert "capture_successes" in source
    assert "capture_failures" in source
    assert "schedule_failures" in source
    assert "consecutive_failures" in source
    assert "last_success_sequence" in source
    assert "last_failure_reason" in source
    assert "bounded_text" in source
    assert "assert(file:write(payload))" in source
    assert "pcall(function() maybe_write_health(false) end)" in source


def test_base_sample_write_failure_does_not_publish_a_new_sample() -> None:
    source = BASE_LUA.read_text(encoding="utf-8")
    sample_start = source.index("local function sample_on_game_thread(generation)")
    sample_end = source.index("function FF7RReadOnlyTick()", sample_start)
    sample_source = source[sample_start:sample_end]

    assert 'return false, "state_sample_open_failed"' in sample_source
    assert "return false, bounded_text(detail or \"state_sample_write_failed\", 512)" in sample_source
    assert "return true" in sample_source
    assert "record_capture_success()" not in sample_source

    native = BASE_CPP.read_text(encoding="utf-8") + (BASE_CPP.parent / "sample_format.hpp").read_text(encoding="utf-8")
    assert "sample.source_sequence == last_source_sequence" in native
    assert "source_timestamp_is_recent" in native


def test_base_exporter_installer_is_process_guarded_and_backed_up() -> None:
    source = BASE_INSTALLER.read_text(encoding="utf-8")

    assert "Get-Process -Name 'ff7rebirth_', 'ff7rebirth'" in source
    assert "FFVII Rebirth is still running; close it before deployment." in source
    assert "Copy-Item -LiteralPath $modsFile" in source
    assert "Copy-Item -LiteralPath $destinationDir" in source
    assert "[guid]::NewGuid()" in source
    assert "Move-Item -LiteralPath $temporaryScript -Destination $destinationScript -Force" in source
    assert "Get-FileHash -LiteralPath $sourceScript -Algorithm SHA256" in source
    assert "Installed base telemetry script hash did not match" in source






def test_paired_samplers_use_persistent_game_thread_entry_points() -> None:
    paths = [BASE_LUA] + [
        ROOT / "ff7r-navigation-probe" / "Scripts" / name
        for name in ("main.lua", "navmesh_path.lua", "custom_nav_api_probe.lua")
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "function FF7RReadOnlyTick()" in source
        assert "LoopAsync(" not in source
        assert "ExecuteInGameThread(" not in source
    native = (BASE_CPP.parent / "persistent_sampler.hpp").read_text(encoding="utf-8")
    assert "RegisterGameViewportClientTickPreCallback" in native
    assert "RegisterProcessEventPreCallback" in native
    assert "discover_main_thread()" in native
    assert "CloseHandle(snapshot)" in native
    assert "return tied ? 0 : candidate" in native
    assert "state->game_thread.load() != GetCurrentThreadId()" in native
    assert "state->dispatching" in native
    assert "void detach(" in native
    assert "new_thread(" not in native
    assert "game thread" in native
