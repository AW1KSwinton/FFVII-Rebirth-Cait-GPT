from __future__ import annotations

from pathlib import Path

import pytest

from ff7r_agent import map_query
from ff7r_agent.service import AgentService, SafetyError

SOURCE = Path(__file__).parents[2] / "ff7r-telemetry-exporter/Base/Scripts/map_query.lua"
HARNESS = r"""
function array() return {} end
function safe(fn, default) local ok,v=pcall(fn); if ok and v ~= nil then return v else return default end end
function valid(v) return type(v)=='table' and v.valid ~= false end
function text(v) if v==nil then return nil end return tostring(v) end
function vec3(v) if type(v)=='table' and v.X then return {x=v.X,y=v.Y,z=v.Z} end return nil end
function class_name(o) return o.class end
objects={}
function FindAllOf(name) return objects[name] end
function actor(id, class, x, y, z)
 local a={id=id, class=class, X=x, Y=y, Z=z}
 function a:K2_GetActorLocation() return {X=self.X,Y=self.Y,Z=self.Z} end
 function a:GetFullName() return self.class..' /Game/World.'..self.id end
 return a
end
"""


def lua_block():
    lupa = pytest.importorskip("lupa.lua54")
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(HARNESS)
    source = SOURCE.read_text(encoding="utf-8")
    block = source[source.index("local ACTORS_NEAR_RADIUS_CM"):source.index("-- Every sequence-playing object")]
    return lua, lua.execute(block + "\nreturn actors_near")


def test_actors_within_the_radius_are_listed_nearest_first():
    lua, actors_near = lua_block()
    lua.execute("""objects.Actor = {actor('Gas_02','BP_MakoGas_C',1500,0,0), actor('Far','BP_Rock_C',9000,0,0),
                                   actor('Gas_01','BP_MakoGas_C',500,0,0), actor('Default__X','BP_MakoGas_C',10,0,0)}""")
    result = actors_near(lua.table(x=0, y=0, z=0))
    ids = [result.actors[i].id for i in range(1, len(result.actors) + 1)]
    assert [i.split(".")[-1] for i in ids] == ["Gas_01", "Gas_02"] and result.scanned == 4
    assert actors_near(None).reason == "player_position_unavailable"


def test_the_service_filters_by_word_and_radius(monkeypatch):
    response = {"actors_near": {"scanned": 5000, "truncated": False, "actors": [
        {"class": "BP_MakoGas_C", "id": "a.Gas_01", "distance_cm": 500, "position": {"x": 1, "y": 2, "z": 3}},
        {"class": "StaticMeshActor", "id": "a.Pipe", "distance_cm": 800, "position": {"x": 0, "y": 0, "z": 0}},
        {"class": "BP_MakoGas_C", "id": "a.Gas_02", "distance_cm": 5500, "position": {"x": 0, "y": 0, "z": 0}}]}}
    monkeypatch.setattr(map_query, "request_snapshot", lambda *a, **k: (response, {}))
    result = AgentService.scan_nearby_actors(object(), radius_m=40, contains="mako")
    assert result["ok"] and result["count"] == 1 and result["actors"][0]["id"] == "a.Gas_01"
    assert result["classes"] == {"BP_MakoGas_C": 1}
    with pytest.raises(SafetyError):
        AgentService.scan_nearby_actors(object(), radius_m=200)


def test_an_older_exporter_says_so(monkeypatch):
    monkeypatch.setattr(map_query, "request_snapshot", lambda *a, **k: ({"texts": []}, {}))
    assert AgentService.scan_nearby_actors(object())["reason"] == "actor_scan_unsupported_by_exporter"
