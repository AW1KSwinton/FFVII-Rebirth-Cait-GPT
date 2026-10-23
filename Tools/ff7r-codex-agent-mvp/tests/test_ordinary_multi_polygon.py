from dataclasses import replace
from types import SimpleNamespace

import pytest

from ff7r_agent.detour_route import (
    DetourLink,
    DetourPolygon,
    DetourPortal,
    DetourTopologyGraph,
    PolygonReference,
)
from ff7r_agent.live_route_adapter import LiveAdapterConfig, LiveRouteAdapter, PlanAuthorization
from ff7r_agent.models import Vec3
from ff7r_agent.ordinary_multi_polygon import (
    OrdinaryMultiPolygonRouteError,
    select_ordinary_multi_polygon_destination,
    validate_ordinary_multi_polygon_route,
)


def _polygon(reference, left, right, *, poly_type=0):
    vertices = (
        Vec3(left, 0.0, 0.0),
        Vec3(right, 0.0, 0.0),
        Vec3(right, 500.0, 0.0),
        Vec3(left, 500.0, 0.0),
    )
    return DetourPolygon(
        reference=reference,
        tile_x=1,
        tile_y=1,
        layer=0,
        vertices=vertices,
        flags=1,
        area=0,
        poly_type=poly_type,
        links=(),
        identity_source="test",
    )


def _multi_graph(*, non_ground_ref=None, remove_second_portal=False, tile2=False):
    refs = [PolygonReference.make(1, 1, index) for index in range(3)]
    polygons = {
        refs[0].value: _polygon(refs[0], 0.0, 600.0),
        refs[1].value: _polygon(refs[1], 600.0, 1200.0, poly_type=1 if non_ground_ref == refs[1].value else 0),
        refs[2].value: _polygon(refs[2], 1200.0, 1800.0),
    }
    if tile2:
        old_ref = refs[2].value
        new_ref = PolygonReference.make(1, 2, 2)
        polygons[new_ref.value] = replace(polygons.pop(old_ref), reference=new_ref)
        refs[2] = new_ref
    polygons[refs[0].value] = replace(
        polygons[refs[0].value],
        links=(DetourLink(refs[1].value, 1, 0, 128, 0, 255),),
    )
    if not remove_second_portal:
        polygons[refs[1].value] = replace(
            polygons[refs[1].value],
            links=(DetourLink(refs[2].value, 1, 0, 128, 0, 255),),
        )
    portal12 = DetourPortal(
        refs[0].value, refs[1].value, refs[1].value, 1, 3,
        Vec3(600.0, 500.0, 0.0), Vec3(600.0, 0.0, 0.0), 128,
    )
    adjacency = {refs[0].value: (portal12,)}
    if not remove_second_portal:
        adjacency[refs[1].value] = (
            DetourPortal(
                refs[1].value, refs[2].value, refs[2].value, 1, 3,
                Vec3(1200.0, 500.0, 0.0), Vec3(1200.0, 0.0, 0.0), 128,
            ),
        )
    return DetourTopologyGraph(polygons, adjacency, snapshot_diagnostics={}), refs


def _plan(graph, refs):
    start = Vec3(300.0, 250.0, 0.0)
    return graph.plan(start, graph.polygons[refs[2].value].centroid, max_snap_distance_m=0.75, max_vertical_separation_m=1.0)


def test_multi_polygon_selection_is_explicit_deterministic_and_same_tile():
    graph, refs = _multi_graph()
    start = Vec3(300.0, 250.0, 0.0)
    selection = select_ordinary_multi_polygon_destination(graph, start, refs[0].value)
    _, _, plan, candidates = selection
    repeat = select_ordinary_multi_polygon_destination(graph, start, refs[0].value)

    assert LiveAdapterConfig().route_mode == "same_polygon"
    assert LiveAdapterConfig(route_mode="ordinary_multi_polygon").route_mode == "ordinary_multi_polygon"
    assert LiveAdapterConfig(destination_min_distance_m=10.0, destination_max_distance_m=15.0).route_mode == "same_polygon"
    assert plan.status == "complete"
    assert len(plan.polygon_corridor) >= 2
    assert plan.validation["tile_crossings"] == 0
    assert 10.0 <= plan.total_estimated_path_length_m <= 15.0
    assert plan.end_ref == repeat[2].end_ref
    assert any(item.get("selected") for item in candidates)


def test_multi_polygon_validator_proves_every_transition_and_endpoint():
    graph, refs = _multi_graph()
    plan = _plan(graph, refs)
    start = Vec3(300.0, 250.0, 0.0)
    resolved_start, resolved_end, evidence = validate_ordinary_multi_polygon_route(
        plan.to_dict(), graph, start, start_ref=refs[0].value,
    )
    assert resolved_start == plan.resolved_start
    assert resolved_end == plan.resolved_end
    assert evidence["polygon_count"] == 3
    assert len(evidence["transition_portals"]) == 2
    assert evidence["tile_crossings"] == 0


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda plan: {**plan, "polygon_corridor": [plan["start_ref"]]}, "requires_two_polygons"),
        (lambda plan: {**plan, "validation": {**plan["validation"], "tile_crossings": 1}}, "tile_crossing_rejected"),
        (lambda plan: {**plan, "portals": [{**plan["portals"][0]}, {**plan["portals"][1], "side": 1}, {**plan["portals"][2]}]}, "special_transition"),
    ],
)
def test_multi_polygon_validator_rejects_unsafe_corridor_mutations(mutation, expected):
    graph, refs = _multi_graph()
    plan = mutation(_plan(graph, refs).to_dict())
    with pytest.raises(OrdinaryMultiPolygonRouteError, match=expected):
        validate_ordinary_multi_polygon_route(plan, graph, Vec3(300.0, 250.0, 0.0), start_ref=refs[0].value)


def test_multi_polygon_validator_rejects_missing_adjacency_non_ground_and_edge_clearance():
    graph, refs = _multi_graph()
    plan = _plan(graph, refs).to_dict()
    graph.adjacency.pop(refs[1].value, None)
    with pytest.raises(OrdinaryMultiPolygonRouteError, match="unresolved_portal"):
        validate_ordinary_multi_polygon_route(plan, graph, Vec3(300.0, 250.0, 0.0), start_ref=refs[0].value)

    graph, refs = _multi_graph(non_ground_ref=PolygonReference.make(1, 1, 1).value)
    plan = _plan(graph, refs).to_dict()
    with pytest.raises(OrdinaryMultiPolygonRouteError, match="non_ground_polygon"):
        validate_ordinary_multi_polygon_route(plan, graph, Vec3(300.0, 250.0, 0.0), start_ref=refs[0].value)

    graph, refs = _multi_graph()
    plan = _plan(graph, refs).to_dict()
    with pytest.raises(OrdinaryMultiPolygonRouteError, match="edge_clearance"):
        validate_ordinary_multi_polygon_route(
            plan, graph, Vec3(300.0, 250.0, 0.0), start_ref=refs[0].value,
            endpoint_edge_clearance_m=3.0,
        )


def test_multi_polygon_validator_rejects_portal_geometry_that_is_not_the_admitted_portal():
    graph, refs = _multi_graph()
    plan = _plan(graph, refs).to_dict()
    plan["portals"][1]["left"]["x"] += 100.0
    with pytest.raises(OrdinaryMultiPolygonRouteError, match="portal_geometry_mismatch"):
        validate_ordinary_multi_polygon_route(plan, graph, Vec3(300.0, 250.0, 0.0), start_ref=refs[0].value)


def test_multi_polygon_validator_rejects_tile_crossing_stage():
    graph, refs = _multi_graph(tile2=True)
    # Rebuild the last portal's target reference for the changed tile identity.
    last = refs[2].value
    first = refs[1].value
    graph.polygons[first] = replace(
        graph.polygons[first], links=(DetourLink(last, 1, 0, 128, 0, 255),)
    )
    graph.adjacency[first] = (
        DetourPortal(first, last, last, 1, 3, Vec3(1200.0, 500.0, 0.0), Vec3(1200.0, 0.0, 0.0), 128),
    )
    plan = graph.plan(Vec3(300.0, 250.0, 0.0), graph.polygons[last].centroid, max_snap_distance_m=0.75, max_vertical_separation_m=1.0)
    with pytest.raises(OrdinaryMultiPolygonRouteError, match="tile_crossing"):
        validate_ordinary_multi_polygon_route(plan.to_dict(), graph, Vec3(300.0, 250.0, 0.0), start_ref=refs[0].value)


def test_route_metrics_follow_bent_authorized_polyline_not_start_end_chord():
    adapter = LiveRouteAdapter(object(), object(), object(), lambda plan: object(), object(), config=LiveAdapterConfig())
    adapter.route_plan = {
        "waypoints": [
            {"x": 0.0, "y": 0.0, "z": 0.0},
            {"x": 1000.0, "y": 0.0, "z": 0.0},
            {"x": 1000.0, "y": 1000.0, "z": 0.0},
        ]
    }
    adapter.authorization = PlanAuthorization(
        session_id="run", world_id="EndGame", map_id="EndGame",
        topology_fingerprint="topology", route_plan_sha256="plan",
        start_ref=1, end_ref=2, resolved_start=Vec3(0, 0, 0),
        resolved_end=Vec3(1000, 1000, 0), created_monotonic=0.0,
    )
    adapter.follower = SimpleNamespace(current_waypoint_index=1)
    metrics = adapter._authorized_route_metrics(Vec3(1000.0, 500.0, 0.0))
    assert metrics is not None
    assert metrics["cross_track_m"] == 0.0
    assert metrics["along_route_m"] == 15.0
    assert metrics["remaining_route_distance_m"] == 5.0
