import math

import pytest

from ff7r_agent.detour_route import (
    DetourLink,
    DetourPolygon,
    DetourPortal,
    DetourTopologyGraph,
    PolygonReference,
)
from ff7r_agent.models import Vec3
from ff7r_agent.time_aware_route import (
    TraversalTimeModel,
    evaluate_corridor_time,
    estimate_polyline_seconds,
    search_time_aware_corridor,
)
from unittest.mock import patch


def _polygon(tile_index: int, center: tuple[float, float]) -> DetourPolygon:
    x, y = center
    reference = PolygonReference.make(1, tile_index, 0)
    return DetourPolygon(
        reference=reference,
        tile_x=tile_index,
        tile_y=0,
        layer=0,
        vertices=(
            Vec3(x - 50.0, y - 50.0, 0.0),
            Vec3(x + 50.0, y - 50.0, 0.0),
            Vec3(x + 50.0, y + 50.0, 0.0),
            Vec3(x - 50.0, y + 50.0, 0.0),
        ),
        flags=1,
        area=0,
        poly_type=0,
        links=(),
        identity_source="test",
    )


def _portal(source: DetourPolygon, target: DetourPolygon, edge: int) -> DetourPortal:
    dx = target.centroid.x - source.centroid.x
    dy = target.centroid.y - source.centroid.y
    length = math.hypot(dx, dy)
    normal_x, normal_y = -dy / length, dx / length
    midpoint = Vec3(
        (source.centroid.x + target.centroid.x) * 0.5,
        (source.centroid.y + target.centroid.y) * 0.5,
        0.0,
    )
    return DetourPortal(
        from_reference=source.reference.value,
        to_reference=target.reference.value,
        link_reference=target.reference.value,
        source_edge=edge,
        target_edge=0,
        left=Vec3(midpoint.x + normal_x * 100.0, midpoint.y + normal_y * 100.0, midpoint.z),
        right=Vec3(midpoint.x - normal_x * 100.0, midpoint.y - normal_y * 100.0, midpoint.z),
        side=128,
    )


def _two_route_graph():
    # Short route: 310 m with four 90-degree turns.
    # Smooth route: ~319.3 m with two ~24-degree turns.
    centers = {
        "s": (0.0, 0.0),
        "a": (8000.0, 0.0),
        "b": (8000.0, 500.0),
        "c": (16000.0, 500.0),
        "d": (16000.0, 0.0),
        "x": (10000.0, -4500.0),
        "y": (20000.0, -4500.0),
        "e": (30000.0, 0.0),
    }
    polygons = {
        name: _polygon(index + 1, center)
        for index, (name, center) in enumerate(centers.items())
    }
    adjacency = {polygon.reference.value: [] for polygon in polygons.values()}

    def connect(first: str, second: str) -> None:
        source = polygons[first]
        target = polygons[second]
        adjacency[source.reference.value].append(_portal(source, target, len(adjacency[source.reference.value])))

    for first, second in (("s", "a"), ("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")):
        connect(first, second)
    for first, second in (("s", "x"), ("x", "y"), ("y", "e")):
        connect(first, second)

    polygons = {
        name: DetourPolygon(
            reference=polygon.reference,
            tile_x=polygon.tile_x,
            tile_y=polygon.tile_y,
            layer=polygon.layer,
            vertices=polygon.vertices,
            flags=polygon.flags,
            area=polygon.area,
            poly_type=polygon.poly_type,
            links=tuple(
                DetourLink(portal.to_reference, portal.source_edge, 0, 128, 0, 255)
                for portal in adjacency[polygon.reference.value]
            ),
            identity_source=polygon.identity_source,
        )
        for name, polygon in polygons.items()
    }
    graph = DetourTopologyGraph(
        {polygon.reference.value: polygon for polygon in polygons.values()},
        {reference: tuple(portals) for reference, portals in adjacency.items()},
        snapshot_diagnostics={"source": "unit_test"},
    )
    return graph, polygons


def test_zero_penalty_shadow_matches_shortest_distance_choice():
    graph, polygons = _two_route_graph()
    baseline = graph._astar(
        polygons["s"].reference.value,
        polygons["e"].reference.value,
        max_expansions=100,
    )
    assert baseline is not None
    baseline_corridor, _, baseline_diagnostics = baseline
    assert baseline_corridor == tuple(polygons[name].reference.value for name in ("s", "a", "b", "c", "d", "e"))
    assert baseline_diagnostics["graph_cost_m"] == pytest.approx(310.0)

    shadow = search_time_aware_corridor(
        graph,
        polygons["s"].reference.value,
        polygons["e"].reference.value,
        model=TraversalTimeModel(
            straight_speed_m_s=8.4,
            turn_penalty_s_per_rad=0.0,
            narrow_portal_threshold_m=0.0,
            narrow_portal_penalty_s_per_m=0.0,
        ),
        max_expansions=100,
    )
    assert shadow.ok
    assert shadow.polygon_corridor == baseline_corridor
    assert shadow.graph_distance_m == pytest.approx(310.0)


def test_time_penalty_prefers_longer_smoother_route():
    graph, polygons = _two_route_graph()
    shadow = search_time_aware_corridor(
        graph,
        polygons["s"].reference.value,
        polygons["e"].reference.value,
        model=TraversalTimeModel(
            straight_speed_m_s=8.4,
            turn_penalty_s_per_rad=1.0,
            narrow_portal_threshold_m=0.0,
            narrow_portal_penalty_s_per_m=0.0,
        ),
        max_expansions=100,
    )
    assert shadow.ok
    assert shadow.polygon_corridor == tuple(polygons[name].reference.value for name in ("s", "x", "y", "e"))
    assert shadow.graph_distance_m > 310.0
    assert shadow.graph_distance_m == pytest.approx(319.31712199)
    assert shadow.max_turn_degrees < 30.0
    assert shadow.turn_penalty_seconds < math.pi
    assert shadow.to_dict()["movement_sent"] is False
    assert shadow.to_dict()["authoritative"] is False
    for first, portal in zip(shadow.polygon_corridor, shadow.transitions):
        assert portal in graph.adjacency[first]


def test_blocked_transitions_remain_excluded():
    graph, polygons = _two_route_graph()
    shadow = search_time_aware_corridor(
        graph,
        polygons["s"].reference.value,
        polygons["e"].reference.value,
        model=TraversalTimeModel(turn_penalty_s_per_rad=1.0),
        blocked_transitions={(polygons["x"].reference.value, polygons["y"].reference.value)},
        max_expansions=100,
    )
    assert shadow.ok
    assert polygons["x"].reference.value not in shadow.polygon_corridor
    assert shadow.polygon_corridor == tuple(polygons[name].reference.value for name in ("s", "a", "b", "c", "d", "e"))


def test_existing_corridor_evaluation_requires_validated_portals():
    graph, polygons = _two_route_graph()
    baseline = graph._astar(
        polygons["s"].reference.value,
        polygons["e"].reference.value,
        max_expansions=100,
    )
    assert baseline is not None
    corridor, transitions, _ = baseline
    evaluated = evaluate_corridor_time(graph, corridor, transitions)
    assert evaluated.ok

    forged = DetourPortal(
        from_reference=corridor[0],
        to_reference=corridor[1],
        link_reference=999,
        source_edge=99,
        target_edge=99,
        left=Vec3(0.0, 0.0, 0.0),
        right=Vec3(1.0, 0.0, 0.0),
        side=128,
    )
    rejected = evaluate_corridor_time(graph, corridor, (forged, *transitions[1:]))
    assert rejected.status == "failed"
    assert rejected.reason_code == "corridor_transition_not_in_validated_graph"


def test_model_validation_and_search_bounds_fail_closed():
    with pytest.raises(ValueError):
        TraversalTimeModel(straight_speed_m_s=0.0)
    with pytest.raises(ValueError):
        TraversalTimeModel(turn_penalty_s_per_rad=-0.1)
    with pytest.raises(ValueError):
        TraversalTimeModel(straight_speed_m_s="fast")
    graph, polygons = _two_route_graph()
    result = search_time_aware_corridor(
        graph,
        polygons["s"].reference.value,
        polygons["e"].reference.value,
        max_expansions=0,
    )
    assert result.status == "failed"
    assert result.reason_code == "invalid_search_bound"


def test_polyline_time_estimator_rejects_invalid_parameters_and_coordinates():
    with pytest.raises(ValueError):
        estimate_polyline_seconds((Vec3(0.0, 0.0, 0.0), Vec3(100.0, 0.0, 0.0)), speed_m_s="fast")
    with pytest.raises(ValueError):
        estimate_polyline_seconds((Vec3(0.0, 0.0, 0.0), Vec3(math.nan, 0.0, 0.0)))


def test_plan_travel_time_selects_longer_funnel_only_when_model_says_quicker():
    graph, polygons = _two_route_graph()
    start = polygons["s"].centroid
    end = polygons["e"].centroid
    from ff7r_agent import time_aware_route

    validated_candidates = []
    complete_plan = graph._complete_plan

    def capture_candidate(*args, **kwargs):
        candidate = complete_plan(*args, **kwargs)
        validated_candidates.append(candidate)
        return candidate

    with patch.object(graph, "_complete_plan", side_effect=capture_candidate), patch.object(
        time_aware_route, "estimate_polyline_seconds", side_effect=(10.0, 20.0)
    ):
        plan = graph.plan(start, end, routing_objective="travel_time", max_expansions=100)
    assert plan.status == "complete", plan.reason_code
    assert plan.validation["portal_links_proven"]
    assert plan.validation["waypoints_finite"]
    assert plan.diagnostics["routing_objective"] == "travel_time"
    assert plan.diagnostics["selected_candidate"] == "travel_time"
    assert len(validated_candidates) == 2
    assert all(candidate.status == "complete" and candidate.validation["portal_links_proven"]
               for candidate in validated_candidates)
    assert validated_candidates[0].total_estimated_path_length_m > validated_candidates[1].total_estimated_path_length_m

    distance = graph.plan(start, end, max_expansions=100)
    assert distance.status == "complete", distance.reason_code
    assert plan.total_estimated_path_length_m > distance.total_estimated_path_length_m
    assert plan.diagnostics["time_candidate_seconds"] < plan.diagnostics["distance_candidate_seconds"]

    validated_candidates.clear()
    with patch.object(graph, "_complete_plan", side_effect=capture_candidate), patch.object(
        time_aware_route, "estimate_polyline_seconds", side_effect=(20.0, 10.0)
    ):
        slower_time_route = graph.plan(start, end, routing_objective="travel_time", max_expansions=100)
    assert slower_time_route.status == "complete"
    assert slower_time_route.diagnostics["selected_candidate"] == "distance"
    assert slower_time_route.total_estimated_path_length_m == distance.total_estimated_path_length_m


def test_travel_time_search_failure_falls_back_to_validated_distance_route():
    graph, polygons = _two_route_graph()
    start = polygons["s"].centroid
    end = polygons["e"].centroid
    from ff7r_agent import time_aware_route

    with patch.object(time_aware_route, "search_time_aware_corridor") as search:
        search.return_value = time_aware_route.ShadowRouteResult(
            "failed", "shadow_time_route_not_found", (), (), None, None, None,
            None, None, None, None, None, 0, TraversalTimeModel(),
        )
        plan = graph.plan(start, end, routing_objective="travel_time", max_expansions=100)
    assert plan.status == "complete", plan.reason_code
    assert plan.validation["portal_links_proven"]
    assert plan.diagnostics["routing_objective"] == "travel_time"
    assert plan.diagnostics["selected_candidate"] == "distance_fallback"
    assert plan.diagnostics["time_candidate_fallback_reason"] == "time_search_shadow_time_route_not_found"


def test_travel_time_materializes_blocked_transition_generator_for_both_candidates():
    graph, polygons = _two_route_graph()
    start = polygons["s"].centroid
    end = polygons["e"].centroid
    blocked_edge = (polygons["s"].reference.value, polygons["a"].reference.value)
    plan = graph.plan(
        start,
        end,
        routing_objective="travel_time",
        blocked_transitions=(edge for edge in (blocked_edge,)),
        max_expansions=100,
    )
    assert plan.status == "complete", plan.reason_code
    assert blocked_edge not in tuple(zip(plan.polygon_corridor, plan.polygon_corridor[1:]))
    assert plan.diagnostics["routing_objective"] == "travel_time"


def test_plan_rejects_unhashable_or_unknown_routing_objective():
    graph, polygons = _two_route_graph()
    start = polygons["s"].centroid
    end = polygons["e"].centroid
    for value in (None, [], "fastest"):
        with pytest.raises(ValueError):
            graph.plan(start, end, routing_objective=value)


def test_single_polygon_evaluation_requires_a_known_polygon():
    graph, polygons = _two_route_graph()
    valid = evaluate_corridor_time(graph, (polygons["s"].reference.value,), ())
    assert valid.ok
    assert valid.estimated_seconds == 0
    assert not evaluate_corridor_time(graph, (-1,), ()).ok
