import copy

from ff7r_agent.detour_route import DetourTopologyGraph, Vec3
from ff7r_agent.door_links import find_door_chain, islands
from test_topology_cache import topology_payload


def two_rooms():
    """Two 9.6 m rooms either side of an 80 cm doorway gap, not linked."""
    payload = copy.deepcopy(topology_payload())
    tile = payload["tiles"][0]
    tile["vertices"] = [
        {"x": -40.0, "y": 0.0, "z": 1000.0}, {"x": -1000.0, "y": 0.0, "z": 1000.0},
        {"x": -1000.0, "y": 0.0, "z": -1000.0}, {"x": -40.0, "y": 0.0, "z": -1000.0},
        {"x": 1000.0, "y": 0.0, "z": 1000.0}, {"x": 40.0, "y": 0.0, "z": 1000.0},
        {"x": 40.0, "y": 0.0, "z": -1000.0}, {"x": 1000.0, "y": 0.0, "z": -1000.0},
    ]
    first = tile["polygons"][0]
    second = {**copy.deepcopy(first), "index": 1, "vertex_indices": [4, 5, 6, 7]}
    tile["polygons"] = [first, second]
    for key in ("poly_count", "polygon_count"):
        if key in tile:
            tile[key] = 2
    payload["total_vertices"] = 8
    return DetourTopologyGraph.from_dict(payload)


def test_a_gap_with_a_door_in_it_joins_the_rooms():
    graph = two_rooms()
    (a, b) = sorted(graph.polygons, key=lambda reference: graph.polygons[reference].centroid.x)
    assert islands(graph)[a] != islands(graph)[b]
    swept = []

    def sweep(points):
        swept.append(points)
        return {"segments": [{"index": 0, "hits": [{"kind": "door", "actor": "BG0012_00_Door1Knob_Standard_C x",
                                                   "actor_bounds": {"origin": {"x": 0.0, "y": 0.0, "z": 100.0}}}]}]}

    start = graph.polygons[a].centroid
    (door,) = find_door_chain(graph, sweep, start, b, a)
    assert door["actor"].startswith("BG0012") and door["centre"] == {"x": 0.0, "y": 0.0, "z": 100.0}
    # The front is on Cloud's side of the door, the far point beyond it.
    assert (door["front"]["x"] - 0.0) * (start.x - 0.0) > 0
    assert (door["beyond"]["x"] - 0.0) * (start.x - 0.0) < 0
    assert len(swept) == 1


def test_a_gap_that_is_only_a_wall_is_not_a_door():
    graph = two_rooms()
    (a, b) = sorted(graph.polygons, key=lambda reference: graph.polygons[reference].centroid.x)
    wall = {"segments": [{"index": 0, "hits": [{"kind": "fixed", "actor": "CollisionAssetActor x"}]}]}
    assert find_door_chain(graph, lambda points: wall, graph.polygons[a].centroid, b, a) == []


def test_a_gap_the_capsule_crosses_cleanly_is_an_open_way_through():
    # Live at Nibelheim: after Cloud had been in to Dale, the room's door was
    # left open and the sweep across its doorway hit nothing.
    graph = two_rooms()
    (a, b) = sorted(graph.polygons, key=lambda reference: graph.polygons[reference].centroid.x)
    clear = {"segments": [{"index": 0, "complete": True, "hits": []}]}
    (link,) = find_door_chain(graph, lambda points: clear, graph.polygons[a].centroid, b, a)
    assert link["open"] and link["actor"] is None


def test_a_found_door_is_recorded_and_later_known_without_a_sweep(tmp_path):
    from ff7r_agent.door_links import DoorLinks

    graph = two_rooms()
    (a, b) = sorted(graph.polygons, key=lambda reference: graph.polygons[reference].centroid.x)
    door_hit = {"segments": [{"index": 0, "hits": [{"kind": "door", "actor": "BG0002_00_Door1Knob_Standard_C x"}]}]}
    store = DoorLinks(tmp_path / "door-links.json")
    find_door_chain(graph, lambda points: door_hit, graph.polygons[a].centroid, b, a,
                    record=lambda link: store.record("capture", link))
    # Left open, the doorway sweeps like a wall; the recorded link still joins
    # the rooms, whichever way Cloud goes.
    wall = {"segments": [{"index": 0, "complete": True, "hits": [{"kind": "ledge"}, {"kind": "fixed"}]}]}
    reloaded = DoorLinks(tmp_path / "door-links.json")
    (back,) = find_door_chain(graph, lambda points: wall, graph.polygons[b].centroid, a, b,
                              known=reloaded.for_topology("capture"))
    assert back["actor"].startswith("BG0002") and not back["open"]
    assert (back["front"]["x"] - 0.0) * (graph.polygons[b].centroid.x - 0.0) > 0
    assert reloaded.for_topology("another capture") == []
