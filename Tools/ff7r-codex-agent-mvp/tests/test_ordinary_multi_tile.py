from dataclasses import replace

import pytest

from ff7r_agent.detour_route import DetourLink, DetourPolygon, DetourPortal, DetourTopologyGraph, PolygonReference
from ff7r_agent.live_route_adapter import LiveAdapterConfig
from ff7r_agent.models import Vec3
from ff7r_agent.ordinary_multi_tile import (
    OrdinaryMultiTileRouteError,
    derive_multi_tile_cycle_budget,
    validate_ordinary_multi_tile_route,
)


def _tile_graph():
    refs = [
        PolygonReference.make(1, 1, 0),
        PolygonReference.make(1, 1, 1),
        PolygonReference.make(1, 2, 0),
        PolygonReference.make(1, 2, 1),
    ]

    def polygon(ref, left, right, links=()):
        return DetourPolygon(
            reference=ref,
            tile_x=0 if ref.tile_index == 1 else 1,
            tile_y=0,
            layer=0,
            vertices=(
                Vec3(left, 0.0, 0.0), Vec3(right, 0.0, 0.0),
                Vec3(right, 1000.0, 0.0), Vec3(left, 1000.0, 0.0),
            ),
            flags=1,
            area=49,
            poly_type=0,
            links=tuple(links),
            identity_source="native_pool",
        )

    links = [
        DetourLink(refs[1].value, 1, 0, 128, 0, 255),
        DetourLink(refs[2].value, 1, 0, 0, 0, 255),
        DetourLink(refs[3].value, 1, 0, 128, 0, 255),
    ]
    polygons = {
        refs[0].value: polygon(refs[0], 0.0, 1000.0, [links[0]]),
        refs[1].value: polygon(refs[1], 1000.0, 2000.0, [links[1]]),
        refs[2].value: polygon(refs[2], 2000.0, 3000.0, [links[2]]),
        refs[3].value: polygon(refs[3], 3000.0, 4000.0),
    }
    adjacency = {
        refs[0].value: (DetourPortal(refs[0].value, refs[1].value, refs[1].value, 1, 3, Vec3(1000, 0, 0), Vec3(1000, 1000, 0), 128),),
        refs[1].value: (DetourPortal(refs[1].value, refs[2].value, refs[2].value, 1, 3, Vec3(2000, 0, 0), Vec3(2000, 1000, 0), 0),),
        refs[2].value: (DetourPortal(refs[2].value, refs[3].value, refs[3].value, 1, 3, Vec3(3000, 0, 0), Vec3(3000, 1000, 0), 128),),
    }
    return DetourTopologyGraph(polygons, adjacency, snapshot_diagnostics={}), refs


def _plan():
    graph, refs = _tile_graph()
    start = Vec3(500.0, 500.0, 0.0)
    end = graph.polygons[refs[-1].value].centroid
    return graph, refs, start, graph.plan(start, end, max_snap_distance_m=0.75, max_vertical_separation_m=1.0)


def _three_tile_graph():
    graph, refs = _tile_graph()
    third = PolygonReference.make(1, 3, 0)
    graph.polygons[third.value] = DetourPolygon(
        reference=third,
        tile_x=2,
        tile_y=0,
        layer=0,
        vertices=(
            Vec3(4000.0, 0.0, 0.0), Vec3(5000.0, 0.0, 0.0),
            Vec3(5000.0, 1000.0, 0.0), Vec3(4000.0, 1000.0, 0.0),
        ),
        flags=1,
        area=49,
        poly_type=0,
        links=(),
        identity_source="native_pool",
    )
    graph.polygons[refs[3].value] = replace(
        graph.polygons[refs[3].value],
        links=(DetourLink(third.value, 1, 0, 0, 0, 255),),
    )
    graph.adjacency[refs[3].value] = (
        DetourPortal(refs[3].value, third.value, third.value, 1, 3, Vec3(4000, 0, 0), Vec3(4000, 1000, 0), 0),
    )
    return graph, [*refs, third]


def test_route_mode_is_explicit_and_same_tile_defaults_remain_unchanged():
    assert LiveAdapterConfig().route_mode == "same_polygon"
    assert LiveAdapterConfig(route_mode="ordinary_multi_polygon").route_mode == "ordinary_multi_polygon"
    assert LiveAdapterConfig(route_mode="ordinary_multi_tile").route_mode == "ordinary_multi_tile"
    with pytest.raises(ValueError):
        LiveAdapterConfig(route_mode="ordinary_multi_polygon_cross_tile")


def test_valid_one_crossing_route_passes_and_recomputes_tile_crossings():
    graph, refs, start, plan = _plan()
    resolved_start, resolved_end, evidence = validate_ordinary_multi_tile_route(
        plan.to_dict(), graph, start, start_ref=refs[0].value,
    )
    assert resolved_start == plan.resolved_start
    assert resolved_end == plan.resolved_end
    assert evidence["tile_crossings"] == 1
    assert evidence["tile_identities"][0]["tile_index"] != evidence["tile_identities"][2]["tile_index"]
    assert evidence["transition_portals"][1]["portal"]["side"] == 0


def test_valid_bounded_two_crossing_route_passes_and_one_crossing_limit_rejects():
    graph, refs = _three_tile_graph()
    start = Vec3(500.0, 500.0, 0.0)
    end = graph.polygons[refs[-1].value].centroid
    plan = graph.plan(
        start, end, max_snap_distance_m=0.75, max_vertical_separation_m=1.0,
        max_tile_crossings=2,
    )
    _, _, evidence = validate_ordinary_multi_tile_route(
        plan.to_dict(), graph, start, start_ref=refs[0].value, max_tile_crossings=2,
    )
    assert evidence["tile_crossings"] == 2
    assert [item["from_tile"]["tile_index"] for item in evidence["transition_portals"] if item["tile_crossing"]] == [1, 2]
    with pytest.raises(OrdinaryMultiTileRouteError, match="too_many_tile_crossings"):
        validate_ordinary_multi_tile_route(
            plan.to_dict(), graph, start, start_ref=refs[0].value, max_tile_crossings=1,
        )


def test_planner_crossing_limit_is_a_real_search_constraint():
    graph, refs = _three_tile_graph()
    start = Vec3(500.0, 500.0, 0.0)
    end = graph.polygons[refs[-1].value].centroid
    blocked = graph.plan(
        start, end, max_snap_distance_m=0.75, max_vertical_separation_m=1.0,
        max_tile_crossings=1,
    )
    assert blocked.status == "failed"
    assert blocked.reason_code == "no_linked_polygon_corridor"


def test_zero_crossing_and_short_or_long_routes_fail():
    graph, refs, start, plan = _plan()
    zero = plan.to_dict()
    zero["polygon_corridor"] = [refs[0].value, refs[1].value]
    zero["end_ref"] = refs[1].value
    zero["resolved_end"] = {"x": 1500.0, "y": 500.0, "z": 0.0}
    zero["portals"] = zero["portals"][:2]
    zero["validation"] = {**zero["validation"], "tile_crossings": 0}
    with pytest.raises(OrdinaryMultiTileRouteError, match="requires_tile_crossing"):
        validate_ordinary_multi_tile_route(zero, graph, start, start_ref=refs[0].value)

    for length, expected in ((29.9, "route_length_outside_bound"), (50.1, "route_length_outside_bound")):
        mutated = plan.to_dict()
        mutated["total_estimated_path_length_m"] = length
        with pytest.raises(OrdinaryMultiTileRouteError, match=expected):
            validate_ordinary_multi_tile_route(mutated, graph, start, start_ref=refs[0].value)


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda p, r: {**p, "validation": {**p["validation"], "tile_crossings": 0}}, "tile_crossing_evidence_mismatch"),
        (lambda p, r: {**p, "portals": [*p["portals"][:2], {**p["portals"][2], "side": 128}, p["portals"][3]]}, "cross_tile_internal_side"),
        (lambda p, r: {**p, "portals": [*p["portals"][:2], {**p["portals"][2], "left": {"x": 2100, "y": 0, "z": 0}}, p["portals"][3]]}, "unresolved_or_dynamic_portal"),
        (lambda p, r: {**p, "polygon_corridor": [p["polygon_corridor"][0], p["polygon_corridor"][1], p["polygon_corridor"][1], p["polygon_corridor"][3]]}, "repeats_polygon"),
        (lambda p, r: {**p, "resolved_end": {"x": 3000, "y": 500, "z": 0}}, "endpoint_ref_unresolvable"),
    ],
)
def test_multi_tile_validator_rejects_unsafe_mutations(mutation, expected):
    graph, refs, start, plan = _plan()
    mutated = mutation(plan.to_dict(), refs)
    with pytest.raises(OrdinaryMultiTileRouteError, match=expected):
        validate_ordinary_multi_tile_route(mutated, graph, start, start_ref=refs[0].value)


def test_dynamic_link_and_missing_adjacency_fail_closed():
    graph, refs, start, plan = _plan()
    source = graph.polygons[refs[1].value]
    graph.polygons[refs[1].value] = replace(
        source,
        links=(DetourLink(refs[2].value, 1, 0, 0x50, 0, 255),),
    )
    with pytest.raises(OrdinaryMultiTileRouteError, match="unresolved_or_dynamic_portal"):
        validate_ordinary_multi_tile_route(plan.to_dict(), graph, start, start_ref=refs[0].value)


def test_missing_portal_target_edge_and_invalid_corridor_reference_fail_closed():
    graph, refs, start, plan = _plan()
    bad = plan.to_dict()
    bad["portals"][2] = {**bad["portals"][2], "target_edge": 99}
    with pytest.raises(OrdinaryMultiTileRouteError, match="unresolved_or_dynamic_portal"):
        validate_ordinary_multi_tile_route(bad, graph, start, start_ref=refs[0].value)

    bad = plan.to_dict()
    bad["polygon_corridor"] = [refs[0].value, "not-a-ref", refs[2].value, refs[3].value]
    with pytest.raises(OrdinaryMultiTileRouteError, match="corridor_reference_invalid"):
        validate_ordinary_multi_tile_route(bad, graph, start, start_ref=refs[0].value)


def test_plan_identity_and_portal_count_are_required():
    graph, refs, start, plan = _plan()
    bad = plan.to_dict()
    bad["start_ref"] = refs[1].value
    with pytest.raises(OrdinaryMultiTileRouteError, match="start_ref_mismatch"):
        validate_ordinary_multi_tile_route(bad, graph, start, start_ref=refs[0].value)

    bad = plan.to_dict()
    bad["portals"] = bad["portals"][:-1]
    with pytest.raises(OrdinaryMultiTileRouteError, match="portal_count_mismatch"):
        validate_ordinary_multi_tile_route(bad, graph, start, start_ref=refs[0].value)

    graph, refs, start, plan = _plan()
    graph.adjacency.pop(refs[1].value)
    with pytest.raises(OrdinaryMultiTileRouteError, match="unresolved_or_dynamic_portal"):
        validate_ordinary_multi_tile_route(plan.to_dict(), graph, start, start_ref=refs[0].value)


def test_non_ground_and_ambiguous_tile_identity_fail_closed():
    graph, refs, start, plan = _plan()
    graph.polygons[refs[2].value] = replace(graph.polygons[refs[2].value], poly_type=1)
    with pytest.raises(OrdinaryMultiTileRouteError, match="non_ground_polygon"):
        validate_ordinary_multi_tile_route(plan.to_dict(), graph, start, start_ref=refs[0].value)

    graph, refs, start, plan = _plan()
    graph.polygons[refs[2].value] = replace(graph.polygons[refs[2].value], identity_source="unresolved")
    with pytest.raises(OrdinaryMultiTileRouteError, match="ambiguous_tile_identity"):
        validate_ordinary_multi_tile_route(plan.to_dict(), graph, start, start_ref=refs[0].value)


def test_start_snap_vertical_endpoint_clearance_and_crossing_bound_fail():
    graph, refs, start, plan = _plan()
    bad = plan.to_dict()
    bad["start_resolution"] = {**bad["start_resolution"], "horizontal_distance_m": 0.76}
    with pytest.raises(OrdinaryMultiTileRouteError, match="start_snap_exceeds_bound"):
        validate_ordinary_multi_tile_route(bad, graph, start, start_ref=refs[0].value)

    bad = plan.to_dict()
    bad["start_resolution"] = {**bad["start_resolution"], "vertical_distance_m": 1.01}
    with pytest.raises(OrdinaryMultiTileRouteError, match="vertical.*exceeds_bound"):
        validate_ordinary_multi_tile_route(bad, graph, start, start_ref=refs[0].value)

    bad = plan.to_dict()
    bad["resolved_end"] = {"x": 4000.0, "y": 500.0, "z": 0.0}
    with pytest.raises(OrdinaryMultiTileRouteError, match="endpoint_edge_clearance"):
        validate_ordinary_multi_tile_route(bad, graph, start, start_ref=refs[0].value)


def test_cycle_budget_is_length_derived_bounded_and_deterministic():
    assert derive_multi_tile_cycle_budget(30.0) == 192
    assert derive_multi_tile_cycle_budget(50.0) == 312
    assert derive_multi_tile_cycle_budget(50.0, absolute_max_cycles=200) == 200
    assert derive_multi_tile_cycle_budget(1.0) == 40


def test_controller_policy_and_existing_same_tile_mode_are_unchanged():
    config = LiveAdapterConfig(route_mode="ordinary_multi_tile")
    assert config.normal_stick_magnitude == 0.35
    assert config.approach_stick_magnitude == 0.5
    assert config.final_stick_magnitude == 0.5
    assert config.minimum_effective_stick_magnitude == 0.5
    assert config.final_arrival_radius_m == 0.75
    assert config.multi_tile_min_distance_m == 30.0
    assert config.multi_tile_max_distance_m == 50.0
    assert LiveAdapterConfig(route_mode="ordinary_multi_polygon").multi_polygon_min_distance_m == 10.0
