from __future__ import annotations

import json
from pathlib import Path
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]

# This behavior test intentionally uses the real Lua VM. It is run with the
# optional `lupa` test dependency; ordinary development installs may omit it.
LuaRuntime = pytest.importorskip("lupa.lua54").LuaRuntime


SOURCE_PATH = PROJECT_ROOT / "Tools/ff7r-navigation-probe/Scripts/navmesh_path.lua"
MARKER_PATH = r"C:\CaitGPT\Telemetry\navigation-ai-path-observation.enable"
SAMPLE_PATH = r"C:\CaitGPT\Telemetry\state.sample"
OBSERVATION_PATH = r"C:\CaitGPT\Telemetry\navigation-ai-path-observation.json"
OBSERVATION_TEMP_PATH = OBSERVATION_PATH + ".tmp"
ENABLE_TOKEN = "FF7R_AI_PATH_OBSERVATION_ENABLE_V1"
GAMEPLAY_SAMPLE = "1|2026-09-26T00:00:00Z|false|0||0,0,0,0|0,0,0|"
PATH_MARKER_PATH = r"C:\CaitGPT\Telemetry\navigation-path-diagnostic.enable"
PATH_TOKEN = "FF7R_PATH_DIAGNOSTIC_ENABLE_V1"
TARGET_PATH = r"C:\CaitGPT\Telemetry\navigation-target.json"
PATH_OUTPUT = r"C:\CaitGPT\Telemetry\navigation-path.json"
PATH_STATUS = r"C:\CaitGPT\Telemetry\navigation-path-status.json"
STALE_TARGET = '{"target_id":"staleChadley","position":{"x":-33374,"y":-211948,"z":3996}}'


LUA_TEST_HARNESS = r"""
mock_files = {}
mock_calls = {
    find_all = 0,
    static_find = 0,
    get_player_controller = 0,
    observation_writes = 0,
    marker_read_limit = 0,
    path_marker_read_limit = 0,
    path_status_writes = 0,
    path_output_removes = 0,
}

require = function(name)
    if name == "object_refs" then
        return {
            GetPlayerController = function()
                mock_calls.get_player_controller = mock_calls.get_player_controller + 1
                return nil
            end,
        }
    end
    return {}
end

FindAllOf = function(class_name)
    mock_calls.find_all = mock_calls.find_all + 1
    return {}
end

StaticFindObject = function(path)
    mock_calls.static_find = mock_calls.static_find + 1
    return {
        IsValid = function(self) return true end,
        GetFullName = function(self) return path end,
    }
end

io.open = function(path, mode)
    if mode == "rb" then
        local content = mock_files[path]
        if content == nil then return nil end
        return {
            read = function(self, format)
                if path == "C:\\CaitGPT\\Telemetry\\navigation-ai-path-observation.enable"
                   and type(format) == "number" then
                    mock_calls.marker_read_limit = format
                end
                if path == "C:\\CaitGPT\\Telemetry\\navigation-path-diagnostic.enable"
                   and type(format) == "number" then
                    mock_calls.path_marker_read_limit = format
                end
                if format == "*a" then return content end
                if format == "*l" then return content end
                if type(format) == "number" then return content:sub(1, format) end
                error("unsupported mocked read format")
            end,
            close = function(self) return true end,
        }
    elseif mode == "wb" then
        local chunks = {}
        return {
            write = function(self, value)
                chunks[#chunks + 1] = value
                return self
            end,
            flush = function(self) return true end,
            close = function(self)
                mock_files[path] = table.concat(chunks)
                if path == "C:\\CaitGPT\\Telemetry\\navigation-ai-path-observation.json.tmp" then
                    mock_calls.observation_writes = mock_calls.observation_writes + 1
                end
                if path == "C:\\CaitGPT\\Telemetry\\navigation-path-status.json.tmp" then
                    mock_calls.path_status_writes = mock_calls.path_status_writes + 1
                end
                return true
            end,
        }
    end
    return nil
end

os.remove = function(path)
    if path == "C:\\CaitGPT\\Telemetry\\navigation-path.json" then
        mock_calls.path_output_removes = mock_calls.path_output_removes + 1
    end
    mock_files[path] = nil
    return true
end

os.rename = function(source, destination)
    if mock_files[source] == nil then return nil, "mock source missing" end
    mock_files[destination] = mock_files[source]
    mock_files[source] = nil
    return true
end

os.date = function(format)
    return "2026-09-26T00:00:00Z"
end

os.clock = function()
    return 0
end
"""


def _new_runtime(
    *,
    marker: str | None = None,
    path_marker: str | None = None,
    target: str | None = None,
    sample: str | None = GAMEPLAY_SAMPLE,
) -> LuaRuntime:
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(LUA_TEST_HARNESS)
    files = lua.globals().mock_files
    if marker is not None:
        files[MARKER_PATH] = marker
    if path_marker is not None:
        files[PATH_MARKER_PATH] = path_marker
    if target is not None:
        files[TARGET_PATH] = target
    if sample is not None:
        files[SAMPLE_PATH] = sample
    lua.execute(SOURCE_PATH.read_text(encoding="utf-8"))
    return lua


def _tick(lua: LuaRuntime, count: int = 1) -> None:
    tick = lua.globals().FF7RReadOnlyTick
    for _ in range(count):
        tick()


def _observation(lua: LuaRuntime) -> dict[str, object]:
    raw = lua.globals().mock_files[OBSERVATION_PATH]
    assert raw is not None
    return json.loads(raw)


def _path_status(lua: LuaRuntime) -> dict[str, object]:
    raw = lua.globals().mock_files[PATH_STATUS]
    assert raw is not None
    return json.loads(raw)


def test_ai_path_scan_stays_idle_for_missing_empty_and_invalid_marker() -> None:
    for marker in (None, "", "not-the-versioned-token", ENABLE_TOKEN + "\n", ENABLE_TOKEN + ("x" * 64)):
        lua = _new_runtime(marker=marker)

        _tick(lua, 3)

        calls = lua.globals().mock_calls
        assert calls.find_all == 0
        assert calls.static_find == 0
        assert calls.observation_writes == 0
        # Request reads are bounded to one byte beyond the 64-byte cap.
        assert calls.marker_read_limit == (0 if marker is None else 65)


def test_versioned_marker_opts_in_scan_removal_stops_and_removal_rearms() -> None:
    lua = _new_runtime()
    files = lua.globals().mock_files
    calls = lua.globals().mock_calls

    _tick(lua)
    assert calls.find_all == 0
    assert calls.static_find == 0

    files[MARKER_PATH] = ENABLE_TOKEN
    _tick(lua)
    assert calls.find_all > 0
    assert calls.static_find > 0
    assert _observation(lua)["attempt"] == 1

    prior_find_all = calls.find_all
    files[MARKER_PATH] = None
    _tick(lua)
    assert calls.find_all == prior_find_all
    stopped = _observation(lua)
    assert stopped["status"] == "stopped"
    assert stopped["reason_code"] == "operator_request_removed_or_invalid"

    files[MARKER_PATH] = ENABLE_TOKEN
    _tick(lua)
    assert calls.find_all > prior_find_all
    assert _observation(lua)["attempt"] == 1


def test_attempt_cap_does_not_repeat_until_marker_is_removed() -> None:
    lua = _new_runtime(marker=ENABLE_TOKEN, sample=None)
    files = lua.globals().mock_files
    calls = lua.globals().mock_calls

    _tick(lua, 600)
    completed = _observation(lua)
    assert completed["attempt"] == 600
    assert completed["max_attempts"] == 600
    assert completed["status"] == "inconclusive"
    writes_at_cap = calls.observation_writes
    assert writes_at_cap == 600

    _tick(lua)
    assert calls.observation_writes == writes_at_cap
    assert _observation(lua) == completed

    files[MARKER_PATH] = None
    _tick(lua)
    files[MARKER_PATH] = ENABLE_TOKEN
    _tick(lua)
    restarted = _observation(lua)
    assert restarted["attempt"] == 1
    assert restarted["status"] == "waiting_for_gameplay"


def test_legacy_target_stays_idle_without_versioned_path_request() -> None:
    invalid_markers = (None, "", "invalid", PATH_TOKEN + "\n", PATH_TOKEN + ("x" * 64))
    for marker in invalid_markers:
        lua = _new_runtime(path_marker=marker, target=STALE_TARGET)
        files = lua.globals().mock_files
        files[PATH_OUTPUT] = '{"navmesh_path":[{"x":1}]}'

        _tick(lua, 3)

        calls = lua.globals().mock_calls
        assert calls.get_player_controller == 0
        assert calls.static_find == 0
        assert calls.find_all == 0
        assert files[PATH_OUTPUT] is None
        status = _path_status(lua)
        assert status["status"] == "disabled"
        assert status["reason_code"] == "diagnostic_request_not_enabled"
        assert calls.path_status_writes == 1
        assert calls.path_output_removes == 1
        assert calls.path_marker_read_limit == (0 if marker is None else 65)


def test_path_diagnostic_is_one_shot_until_marker_removal_then_rearms() -> None:
    lua = _new_runtime(target=STALE_TARGET)
    files = lua.globals().mock_files
    calls = lua.globals().mock_calls
    files[PATH_MARKER_PATH] = PATH_TOKEN

    _tick(lua, 3)

    assert calls.get_player_controller == 1
    assert calls.static_find == 0
    assert calls.find_all == 0
    assert _path_status(lua)["status"] == "waiting_for_gameplay"
    assert calls.path_status_writes == 1

    files[PATH_OUTPUT] = '{"navmesh_path":[{"x":1}]}'
    files[PATH_MARKER_PATH] = None
    _tick(lua)
    assert calls.get_player_controller == 1
    assert files[PATH_OUTPUT] is None
    stopped = _path_status(lua)
    assert stopped["status"] == "stopped"
    assert stopped["reason_code"] == "diagnostic_request_removed_or_invalid"
    assert calls.path_status_writes == 2

    _tick(lua)
    assert calls.path_status_writes == 2
    assert calls.path_output_removes == 2

    files[PATH_MARKER_PATH] = PATH_TOKEN
    _tick(lua)
    assert calls.get_player_controller == 2
    assert calls.path_status_writes == 3
    assert _path_status(lua)["status"] == "waiting_for_gameplay"
