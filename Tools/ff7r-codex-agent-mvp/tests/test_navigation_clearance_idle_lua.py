"""Exercise the installed clearance Lua with fake files and engine calls."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

LuaRuntime = pytest.importorskip("lupa.lua54").LuaRuntime
SOURCE = Path(__file__).resolve().parents[3] / "Tools/ff7r-navigation-probe/Scripts/main.lua"
ROOT = "C:\\CaitGPT\\Telemetry\\"
NOW = int(datetime(2026, 10, 8, 20, tzinfo=timezone.utc).timestamp())

HARNESS = r'''
files = {}
calls = {controller=0, line=0, capsule=0}
local valid = function() return true end
local pawn = {IsValid=valid, K2_GetActorLocation=function() return {X=100,Y=200,Z=300} end}
local camera = {IsValid=valid, GetCameraRotation=function() return {Yaw=0} end}
local controller = {IsValid=valid, Pawn=pawn, PlayerCameraManager=camera}
local system = {IsValid=valid,
    LineTraceSingle=function() calls.line=calls.line+1; return false end,
    CapsuleTraceSingle=function() calls.capsule=calls.capsule+1; return false end}
local mathlib = {IsValid=valid,
    GetForwardVector=function() return {X=1,Y=0,Z=0} end,
    GetRightVector=function() return {X=0,Y=1,Z=0} end}
require = function(name)
    assert(name == "object_refs")
    return {GetPlayerController=function() calls.controller=calls.controller+1; return controller end,
        GetKismetSystemLibrary=function() return system end,
        GetKismetMathLibrary=function() return mathlib end}
end
io.open = function(path, mode)
    if mode == "rb" then
        if files[path] == nil then return nil end
        return {read=function(_, amount) return files[path]:sub(1, amount) end, close=function() end}
    end
    local chunks = {}
    return {write=function(_, value) chunks[#chunks+1]=value; return true end,
        flush=function() return true end,
        close=function() files[path]=table.concat(chunks); return true end}
end
os.remove = function(path) files[path]=nil; return true end
os.rename = function(source, target) files[target]=files[source]; files[source]=nil; return true end
os.time = function() return test_now end
'''


def sample(*, paused="0", menu="0", position="100,200,300,0,0,0", map_id="EndGame", actor="Pawn", age=0):
    stamp = datetime.fromtimestamp(NOW-age, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "|".join(["10", stamp, "0", "0", "", position, map_id, "", "0", paused,
                     "", menu, "", "0", actor, "0,0,0", ""]) + "\n"


@pytest.mark.parametrize("line,reason", [
    (None, "state_sample_unavailable"), ("1|broken", "state_sample_unavailable"),
    (sample(paused="1"), "paused_or_menu"), (sample(menu="1"), "paused_or_menu"),
    (sample(paused="-1"), "paused_or_menu"), (sample(menu="-1"), "paused_or_menu"),
    (sample(position=""), "player_not_loaded"), (sample(map_id=""), "player_not_loaded"),
    (sample(actor=""), "player_not_loaded"), (sample(age=3), "state_sample_stale"),
    (sample(age=-2), "state_sample_stale"), ("x"*16385, "state_sample_unavailable"),
], ids=["missing", "malformed", "paused", "menu", "pause-unknown", "menu-unknown",
        "no-pose", "no-map", "no-actor", "stale", "future", "oversized"])
def test_idle_boundaries_do_no_engine_work_and_resume_in_field(line, reason):
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.globals().test_now = NOW
    lua.execute(HARNESS)
    lua.globals().files[ROOT+"state.sample"] = line
    lua.globals().files[ROOT+"navigation.json"] = "stale clearance"
    lua.execute(SOURCE.read_text(encoding="utf-8"))
    for _ in range(4):
        lua.globals().FF7RReadOnlyTick()
    assert dict(lua.globals().calls.items()) == {"controller": 0, "line": 0, "capsule": 0}
    assert lua.globals().files[ROOT+"navigation.json"] is None
    health = json.loads(lua.globals().files[ROOT+"navigation-health.json"])
    assert health["state"] == "idle" and health["idle_reason"] == reason
    assert health["capture_failures"] == 0

    lua.globals().files[ROOT+"state.sample"] = sample()
    lua.globals().FF7RReadOnlyTick()
    assert dict(lua.globals().calls.items()) == {"controller": 1, "line": 12, "capsule": 1}
    result = json.loads(lua.globals().files[ROOT+"navigation.json"])
    assert result["base_sequence"] == 10
