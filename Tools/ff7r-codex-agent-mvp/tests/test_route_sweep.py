import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ff7r_agent.navigation_values import MissionError
from ff7r_agent.route_sweep import classify_hit, request_route_sweep

EXPORTER = Path(__file__).resolve().parents[2] / "ff7r-telemetry-exporter" / "Base"


def test_classifies_floors_characters_pushables_and_fixed_props():
    assert classify_hit({"normal_z": 0.95, "simulating_physics": False}) == "walkable"
    villager = {"normal_z": 0.0, "actor": "MB2300_00_KalmCitizen00_C /Game/x.MB2300_00_KalmCitizen00_2",
                "component": "EndCharacterRootComponent /Game/x.MB2300.Root"}
    assert classify_hit(villager) == "npc"
    aerith = {"normal_z": 0, "actor": "PC0003_00_Aerith_Standard_C /Game/x.PC0003_00_Aerith_Standard_2",
              "component": "EndCharacterRootComponent /Game/x.PC0003.Root", "mobility": 2}
    assert classify_hit(aerith) == "party"
    assert classify_hit({"normal_z": 0.1, "simulating_physics": True}) == "pushable"
    assert classify_hit({"normal_z": 0.0, "simulating_physics": False}) == "fixed"
    assert classify_hit({}) == "fixed"  # unknown collision never counts as passable


def fake_exporter(directory: Path, respond):
    """Answer one route-sweep.request the way route_sweep.lua would."""
    def run():
        request = directory / "route-sweep.request"
        for _ in range(200):
            if request.exists():
                lines = request.read_text(encoding="ascii").splitlines()
                header = lines[0].split("|")
                payload = respond(header, [tuple(map(float, line.split(","))) for line in lines[1:]])
                payload.update(request_id=header[0], source="ue4ss_route_sweep")
                (directory / "route-sweep.json").write_text(json.dumps(payload), encoding="utf-8")
                return
            time.sleep(0.01)
    thread = threading.Thread(target=run)
    thread.start()
    return thread


def service_for(tmp_path):
    return SimpleNamespace(telemetry=SimpleNamespace(path=tmp_path / "state.json"), _sleeper=time.sleep)


def test_round_trip_lists_only_blockers_to_route_around(tmp_path):
    seen = {}

    def respond(header, points):
        seen.update(header=header, points=points)
        return {"status": "ok", "segments": [
            {"index": 0, "complete": True, "hits": [
                {"actor": "BP_Chair", "simulating_physics": True, "normal_z": 0.0},
                {"actor": "BP_Wagon", "simulating_physics": False, "normal_z": 0.0},
            ]},
            {"index": 1, "complete": True, "hits": []},
        ]}

    thread = fake_exporter(tmp_path, respond)
    result = request_route_sweep(service_for(tmp_path), [
        {"x": 0, "y": 0, "z": 10}, {"x": 1000, "y": 0, "z": 10}, {"x": 1000, "y": 500, "z": 10}])
    thread.join()
    assert seen["header"][2:] == ["34", "88", "30", "channel:0", "45"]
    assert seen["points"][1] == (1000.0, 0.0, 10.0)
    assert [b["actor"] for b in result["blockers"]] == ["BP_Wagon"]
    assert result["segments"][0]["hits"][0]["kind"] == "pushable"
    assert result["clear"] is False
    assert not (tmp_path / "route-sweep.request").exists()


def test_a_failed_sweep_is_an_error_not_a_clear_route(tmp_path):
    thread = fake_exporter(tmp_path, lambda header, points: {"status": "error", "error": "boom"})
    with pytest.raises(MissionError, match="route_sweep_failed:boom"):
        request_route_sweep(service_for(tmp_path), [{"x": 0, "y": 0, "z": 0}, {"x": 1, "y": 0, "z": 0}])
    thread.join()


def test_rejects_unknown_queries(tmp_path):
    with pytest.raises(MissionError, match="query_invalid"):
        request_route_sweep(service_for(tmp_path), [{"x": 0, "y": 0, "z": 0}, {"x": 1, "y": 0, "z": 0}],
                            query="channel:2|x")


def test_rejects_routes_without_segments(tmp_path):
    with pytest.raises(MissionError, match="point_count"):
        request_route_sweep(service_for(tmp_path), [{"x": 0, "y": 0, "z": 0}])


def test_lua_sweep_output_is_classified_by_the_python_consumer():
    lupa = pytest.importorskip("lupa.lua54")
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.globals().arg = runtime.table(str(EXPORTER / "Scripts" / "route_sweep.lua"),
                                          str(EXPORTER / "Scripts" / "map_query.lua"))
    payload = json.loads(runtime.eval(f'loadfile([[{EXPORTER / "tests" / "route_sweep_regression.lua"}]])')())
    kinds = [classify_hit(hit) for hit in payload["segments"][0]["hits"]]
    assert kinds == ["pushable", "fixed"]


def test_a_long_route_is_swept_in_chunks_with_whole_route_segment_indices(tmp_path):
    # Live: a Kalm route from the plaza to Zahira had more points than one
    # request and its collision audit failed with route_sweep_point_count_invalid.
    requests = []

    def serve():
        request = tmp_path / "route-sweep.request"
        answered = set()
        for _ in range(1000):
            if request.exists():
                lines = request.read_text(encoding="ascii").splitlines()
                header = lines[0].split("|")
                if header[0] not in answered:
                    answered.add(header[0])
                    points = lines[1:]
                    requests.append(len(points))
                    segments = [{"index": i, "complete": True, "hits": []} for i in range(len(points) - 1)]
                    if len(requests) == 2:
                        segments[0]["hits"] = [{"actor": "BP_Wagon", "simulating_physics": False, "normal_z": 0.0}]
                    (tmp_path / "route-sweep.json").write_text(json.dumps(
                        {"request_id": header[0], "source": "ue4ss_route_sweep", "status": "ok",
                         "segments": segments}), encoding="utf-8")
                    if len(requests) == 2:
                        return
            time.sleep(0.005)

    thread = threading.Thread(target=serve)
    thread.start()
    route = [{"x": 100.0 * i, "y": 0, "z": 0} for i in range(200)]
    result = request_route_sweep(service_for(tmp_path), route)
    thread.join()
    assert requests == [128, 73] and result["chunks"] == 2
    assert [segment["index"] for segment in result["segments"]] == list(range(199))
    assert [blocker["segment"] for blocker in result["blockers"]] == [127]


def _floor_payload(query, hits, *, missing=0, sequence=10):
    return {"status": "ok", "source": "ue4ss_route_sweep", "request_id": query,
            "map_id": "EndGame", "source_sequence": sequence,
            "capsule": {"radius_cm": 34.0, "half_height_cm": 88.0, "lift_cm": 30.0},
            "query": {"profile": "EndRootCollision"} if query == "profile" else {"channel": 0},
            "floor_missing": missing,
            "segments": [{"index": 0, "complete": True, "hits": hits}]}


def test_profile_confirms_visibility_stair_gap_but_keeps_actor_contact(monkeypatch):
    from copy import deepcopy
    from ff7r_agent import route_sweep
    ledge = {"ledge": True, "rise_cm": 105.0, "impact": {"x": 50, "y": 0, "z": 105}}
    wall = {"actor": "BP_Wall", "normal_z": 0.0, "impact": {"x": 70, "y": 0, "z": 0}}
    primary = _floor_payload("channel:0", [ledge, wall], missing=8)
    profile = _floor_payload("profile", [], sequence=20)
    requests = []

    def sweep(service, route, **options):
        requests.append((route, options))
        return deepcopy(profile if options["query"] == "profile" else primary)

    monkeypatch.setattr(route_sweep, "_sweep_chunk", sweep)
    result = request_route_sweep(None, (p for p in ({"x": 0, "y": 0, "z": 0}, {"x": 100, "y": 0, "z": 105})))
    assert result["floor_profile_confirmation"]["verified"] is True
    assert result["segments"][0]["hits"][0]["ledge"] is True  # Preserve the raw Visibility report.
    assert result["segments"][0]["hits"][0]["kind"] == "walkable"
    assert result["floor_missing"] == 8
    assert [hit["actor"] for hit in result["blockers"]] == ["BP_Wall"]
    assert result["clear"] is False
    assert requests[0][0] == requests[1][0]
    assert {k: v for k, v in requests[0][1].items() if k != "query"} == {k: v for k, v in requests[1][1].items() if k != "query"}


@pytest.mark.parametrize("fault", ["floor_missing", "incomplete", "map_changed", "capsule_changed",
                                   "fallback_channel", "real_ledge", "wall", "stale", "bad_index", "timeout"])
def test_ambiguous_floor_confirmation_keeps_visibility_ledge(monkeypatch, fault):
    from copy import deepcopy
    from ff7r_agent import route_sweep
    ledge = {"ledge": True, "rise_cm": 105.0, "impact": {"x": 50, "y": 0, "z": 105}}
    primary = _floor_payload("channel:0", [ledge], missing=8)
    profile = _floor_payload("profile", [], sequence=20)
    if fault == "floor_missing": profile["floor_missing"] = 1
    if fault == "incomplete": profile["segments"][0]["complete"] = False
    if fault == "map_changed": profile["map_id"] = "DifferentMap"
    if fault == "capsule_changed": profile["capsule"]["radius_cm"] = 1.0
    if fault == "fallback_channel": profile["query"] = {"channel": 0}
    if fault == "real_ledge": profile["segments"][0]["hits"] = [deepcopy(ledge)]
    if fault == "wall": profile["segments"][0]["hits"] = [{"actor": "BP_Wall", "normal_z": 0}]
    if fault == "stale": profile["source_sequence"] = 9
    if fault == "bad_index": profile["segments"][0]["index"] = 2

    def sweep(service, route, **options):
        if options["query"] == "profile" and fault == "timeout":
            raise MissionError("route_sweep_timeout")
        return deepcopy(profile if options["query"] == "profile" else primary)

    monkeypatch.setattr(route_sweep, "_sweep_chunk", sweep)
    result = request_route_sweep(None, [{"x": 0, "y": 0, "z": 0}, {"x": 100, "y": 0, "z": 105}])
    assert result["floor_profile_confirmation"]["verified"] is False
    assert result["blockers"][0]["kind"] == "ledge"
    assert result["clear"] is False


@pytest.mark.parametrize("changed", ["query", "map", "capsule", "sequence"])
def test_floor_confirmation_checks_every_chunk_binding(monkeypatch, changed):
    from ff7r_agent import route_sweep
    calls = {"channel:0": 0, "profile": 0}

    def sweep(service, route, **options):
        query = options["query"]
        calls[query] += 1
        result = _floor_payload(query, [], sequence=(10 if query == "channel:0" else 20) + calls[query])
        result["segments"] = [{"index": i, "complete": True, "hits": []} for i in range(len(route) - 1)]
        if query == "channel:0" and calls[query] == 1:
            result["segments"][0]["hits"] = [{"ledge": True, "rise_cm": 105}]
        if query == "profile" and calls[query] == 2:
            if changed == "query": result["query"] = {"channel": 0}
            if changed == "map": result["map_id"] = "DifferentMap"
            if changed == "capsule": result["capsule"]["radius_cm"] = 1.0
            if changed == "sequence": result["source_sequence"] = 9
        return result

    monkeypatch.setattr(route_sweep, "_sweep_chunk", sweep)
    result = request_route_sweep(None, [{"x": i * 100, "y": 0, "z": 0} for i in range(200)])
    assert calls == {"channel:0": 2, "profile": 2}
    assert result["floor_profile_confirmation"]["verified"] is False
    assert result["blockers"][0]["kind"] == "ledge"
