"""Off-mesh traversal links in the planner, the validator and the report."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ff7r_agent import offmesh_links as om
from ff7r_agent.detour_route import DetourTopologyGraph
from ff7r_agent.models import Vec3
from ff7r_agent.static_obstacles import (
    StaticObstacle,
    StaticObstacleOverlay,
    StaticObstacleScope,
    plan_with_static_obstacles,
)

from offmesh_fixtures import (
    GIRDER_GOAL,
    GIRDER_START,
    connection,
    girder_payload,
    pads_payload,
    ref,
)

A_MIDDLE = Vec3(100.0, 200.0, 0.0)
B_MIDDLE = Vec3(850.0, 200.0, 0.0)


def two_pads(*connections, **kwargs):
    return DetourTopologyGraph.from_dict(pads_payload(list(connections), **kwargs))


STEP = connection((350.0, 200.0, 0.0), (650.0, 200.0, 0.0))


# -- classification -----------------------------------------------------------


@pytest.mark.parametrize(
    ("horizontal_cm", "vertical_cm", "expected"),
    [
        (300.0, 0.0, om.CLASS_STEP_OVER),
        (600.0, 50.0, om.CLASS_STEP_OVER),
        (600.0, -50.0, om.CLASS_STEP_OVER),
        (601.0, 0.0, om.CLASS_LONG_SPAN),
        (300.0, -51.0, om.CLASS_STEP_DOWN),
        (300.0, -150.0, om.CLASS_STEP_DOWN),
        (300.0, -151.0, om.CLASS_DROP),
        (900.0, -80.0, om.CLASS_DROP),
        (300.0, 51.0, om.CLASS_STEP_UP),
        (300.0, 150.0, om.CLASS_STEP_UP),
        (300.0, 151.0, om.CLASS_CLIMB),
        (900.0, 80.0, om.CLASS_CLIMB),
    ],
)
def test_links_are_classified_by_length_and_rise(horizontal_cm, vertical_cm, expected):
    assert om.classify_link(horizontal_cm, vertical_cm) == expected


def test_steps_drops_and_the_live_crossed_gimmicks_are_enabled_and_only_gimmicks_verified():
    assert om.DEFAULT_TRAVERSAL_POLICY.enabled_classes == frozenset(
        {om.CLASS_STEP_OVER, om.CLASS_STEP_DOWN, om.CLASS_STEP_UP, om.CLASS_DROP,
         om.CLASS_LADDER, om.CLASS_CRACK, om.CLASS_CROUCH})
    assert om.CLASS_LONG_SPAN not in om.DEFAULT_TRAVERSAL_POLICY.enabled_classes
    assert om.DEFAULT_TRAVERSAL_POLICY.allow_reverse_of_two_way is False
    # Crossed live at Kalm, 2026-10-14; capture links are still unverified.
    assert om.VERIFIED_LINK_CLASSES == frozenset(om.GIMMICK_LINK_CLASSES)
    assert om.CLASS_CLIMB not in om.DEFAULT_TRAVERSAL_POLICY.enabled_classes
    with pytest.raises(ValueError):
        om.TraversalPolicy(enabled_classes=frozenset({"teleport"}))


# -- the planner --------------------------------------------------------------


def test_a_step_over_joins_two_islands_and_the_route_names_it():
    graph = two_pads(STEP)
    plan = graph.plan(A_MIDDLE, B_MIDDLE)
    assert plan.status == "complete", plan.reason_code
    assert plan.polygon_corridor == (ref(0), ref(1))
    (segment,) = plan.traversal_segments
    assert segment["link_class"] == "step_over"
    assert segment["direction"] == "start_to_end"
    assert (segment["from_ref"], segment["to_ref"]) == (ref(0), ref(1))
    assert segment["start"] == {"x": 350.0, "y": 200.0, "z": 0.0}
    assert segment["end"] == {"x": 650.0, "y": 200.0, "z": 0.0}
    assert segment["live_verified"] is False
    # Departure and landing points are consecutive waypoints of the route.
    start, end = segment["start_waypoint_index"], segment["end_waypoint_index"]
    assert end == start + 1
    assert (plan.waypoints[start].x, plan.waypoints[start].y) == (350.0, 200.0)
    assert (plan.waypoints[end].x, plan.waypoints[end].y) == (650.0, 200.0)
    assert plan.waypoints[0].x == pytest.approx(100.0) and plan.waypoints[-1].x == pytest.approx(850.0)
    # The portal says it is not an ordinary one.
    crossing = plan.portals[1].to_dict()
    assert crossing["kind"] == "traversal" and crossing["traversal_class"] == "step_over"
    assert crossing["left"] == segment["start"] and crossing["right"] == segment["end"]
    assert plan.validation["traversal_count"] == 1 and plan.validation["traversal_links_proven"] is True
    assert plan.validation["portal_links_proven"] is True
    # The route's length is the walked polyline, crossing included.
    assert plan.total_estimated_path_length_m == pytest.approx(7.5)


def test_the_plan_flags_the_unverified_link_class_before_any_movement():
    plan = two_pads(STEP).plan(A_MIDDLE, B_MIDDLE)
    flags = plan.diagnostics["traversal"]
    assert flags["unverified_classes"] == ["step_over"]
    assert flags["requires_live_verification"] is True
    assert flags["by_class"] == {"step_over": 1}
    payload = plan.to_dict()
    assert payload["traversal_segments"][0]["link_class"] == "step_over"
    json.dumps(payload)


def test_a_route_without_a_crossing_serialises_as_before():
    graph = two_pads(STEP)
    plan = graph.plan(Vec3(50.0, 50.0, 0.0), Vec3(350.0, 350.0, 0.0))
    assert plan.status == "complete" and plan.traversal_segments == ()
    assert "traversal_segments" not in plan.to_dict()
    assert "traversal" not in plan.diagnostics
    assert all("kind" not in portal for portal in plan.to_dict()["portals"])


def test_a_one_way_link_is_not_walked_backwards():
    graph = two_pads(STEP)
    plan = graph.plan(B_MIDDLE, A_MIDDLE)
    assert plan.status == "failed" and plan.reason_code == "no_linked_polygon_corridor"
    # Even with everything allowed: nothing says the link goes both ways.
    assert plan.diagnostics["traversal_hint"]["reachable_with_excluded_links"] is False
    assert graph.plan(B_MIDDLE, A_MIDDLE, traversal_policy=om.EVERYTHING_POLICY).status == "failed"


def test_the_reverse_of_a_two_way_link_is_off_unless_the_policy_allows_it():
    graph = two_pads(connection((350.0, 200.0, 0.0), (650.0, 200.0, 0.0), flags=192))
    assert graph.off_mesh_links[0].two_way_claimed is True
    forward = graph.plan(A_MIDDLE, B_MIDDLE)
    assert forward.status == "complete"
    backward = graph.plan(B_MIDDLE, A_MIDDLE)
    assert backward.status == "failed"
    assert backward.diagnostics["traversal_hint"]["uses_two_way_reverse"] is True
    allowed = om.TraversalPolicy(allow_reverse_of_two_way=True)
    reverse = graph.plan(B_MIDDLE, A_MIDDLE, traversal_policy=allowed)
    assert reverse.status == "complete"
    (segment,) = reverse.traversal_segments
    assert segment["direction"] == "end_to_start"
    assert (segment["from_ref"], segment["to_ref"]) == (ref(1), ref(0))
    assert segment["start"] == {"x": 650.0, "y": 200.0, "z": 0.0}


@pytest.mark.parametrize(
    ("end", "rise", "link_class"),
    [
        ((650.0, 200.0, 100.0), 100.0, om.CLASS_STEP_UP),
        ((650.0, 200.0, -100.0), -100.0, om.CLASS_STEP_DOWN),
        ((650.0, 200.0, -300.0), -300.0, om.CLASS_DROP),
    ],
)
def test_steps_and_drops_are_routed_by_default(end, rise, link_class):
    """On 2026-10-21: the Mt. Nibel caves go down by drops."""
    graph = two_pads(connection((350.0, 200.0, 0.0), end), rise_cm=(0.0, rise))
    assert graph.off_mesh_links[0].link_class == link_class
    plan = graph.plan(A_MIDDLE, Vec3(850.0, 200.0, rise))
    assert plan.status == "complete"
    assert [item["link_class"] for item in plan.traversal_segments] == [link_class]


def test_a_failed_link_is_left_out_of_the_plan():
    graph = two_pads(connection((350.0, 200.0, 0.0), (650.0, 200.0, -300.0)), rise_cm=(0.0, -300.0))
    link = graph.off_mesh_links[0]
    excluded = om.TraversalPolicy(excluded_links=frozenset({link.owner_reference}))
    plan = graph.plan(A_MIDDLE, Vec3(850.0, 200.0, -300.0), traversal_policy=excluded)
    assert plan.status == "failed" and plan.reason_code == "no_linked_polygon_corridor"
    assert excluded.to_dict()["excluded_links"] == [link.owner_reference]
    assert "excluded_links" not in om.DEFAULT_TRAVERSAL_POLICY.to_dict()


@pytest.mark.parametrize(
    ("end", "rise", "link_class"),
    [
        ((650.0, 200.0, 300.0), 300.0, om.CLASS_CLIMB),
    ],
)
def test_large_drops_and_climbs_stay_out_of_routes_until_verified(end, rise, link_class):
    graph = two_pads(connection((350.0, 200.0, 0.0), end), rise_cm=(0.0, rise))
    assert graph.off_mesh_links[0].link_class == link_class
    goal = Vec3(850.0, 200.0, rise)
    plan = graph.plan(A_MIDDLE, goal)
    assert plan.status == "failed" and plan.reason_code == "no_linked_polygon_corridor"
    hint = plan.diagnostics["traversal_hint"]
    assert hint["reachable_with_excluded_links"] is True
    assert hint["classes_needed"] == [link_class]
    # Allowed explicitly, the same graph does route over it.
    enabled = om.TraversalPolicy(enabled_classes=frozenset({link_class}))
    assert graph.plan(A_MIDDLE, goal, traversal_policy=enabled).status == "complete"


def test_a_long_level_link_is_a_long_span_and_excluded():
    graph = two_pads(connection((350.0, 200.0, 0.0), (1250.0, 200.0, 0.0)), gap_cm=800.0)
    assert graph.off_mesh_links[0].link_class == om.CLASS_LONG_SPAN
    plan = graph.plan(A_MIDDLE, Vec3(1400.0, 200.0, 0.0))
    assert plan.status == "failed"
    assert plan.diagnostics["traversal_hint"]["classes_needed"] == [om.CLASS_LONG_SPAN]


def test_a_disabled_policy_never_crosses():
    graph = two_pads(STEP)
    plan = graph.plan(A_MIDDLE, B_MIDDLE, traversal_policy=om.DISABLED_TRAVERSAL_POLICY)
    assert plan.status == "failed"
    assert plan.diagnostics["traversal_hint"]["classes_needed"] == ["step_over"]


def test_the_cheapest_of_parallel_links_is_chosen():
    long_link = connection((300.0, 380.0, 0.0), (620.0, 380.0, 0.0))
    short_link = connection((390.0, 200.0, 0.0), (610.0, 200.0, 0.0))
    plan = two_pads(long_link, short_link).plan(A_MIDDLE, B_MIDDLE)
    (segment,) = plan.traversal_segments
    assert segment["horizontal_length_m"] == pytest.approx(2.2)


def test_the_time_aware_objective_crosses_too():
    graph = two_pads(STEP)
    plan = graph.plan(A_MIDDLE, B_MIDDLE, routing_objective="travel_time")
    assert plan.status == "complete" and len(plan.traversal_segments) == 1
    off = graph.plan(A_MIDDLE, B_MIDDLE, routing_objective="travel_time",
                     traversal_policy=om.DISABLED_TRAVERSAL_POLICY)
    assert off.status == "failed"


def test_connections_that_land_nowhere_are_counted_and_left_out():
    graph = two_pads(
        STEP,
        connection((350.0, 300.0, 0.0), (650.0, 300.0, 0.0), end_link=False),
        connection((350.0, 100.0, 0.0), (650.0, 100.0, 0.0), start_link=False),
        # Landing links name pad B but the end point lies 3 m beyond it.
        connection((350.0, 250.0, 0.0), (1300.0, 250.0, 0.0)),
        # Shorter than a stride's worth of anything.
        connection((395.0, 150.0, 0.0), (402.0, 150.0, 0.0), from_pad=0, to_pad=0),
    )
    summary = graph.snapshot_diagnostics["off_mesh"]
    assert summary["serialized"] == 5 and summary["linked"] == 1
    assert summary["landing_unresolved"] == 2
    assert summary["landing_geometry_mismatch"] == 1 and summary["same_polygon"] == 1
    assert len(graph.off_mesh_links) == 1


def test_ordinary_adjacency_and_islands_are_untouched_by_links():
    graph = two_pads(STEP)
    assert graph.adjacency == {}
    assert sorted(set(graph.ground_islands().values())) == [0, 1]
    assert graph.snapshot_diagnostics["traversal_links"]["islands_joined_by_default_policy"] == 1


# -- the live failure ---------------------------------------------------------


def test_the_girder_fixture_now_has_a_corridor_from_the_start_island_to_the_next():
    graph = DetourTopologyGraph.from_dict(girder_payload())
    start, goal = Vec3(*GIRDER_START), Vec3(*GIRDER_GOAL)
    islands = graph.ground_islands()
    start_ref = graph.resolve_point(start).selected_reference
    goal_ref = graph.resolve_point(goal).selected_reference
    assert islands[start_ref] != islands[goal_ref]
    plan = graph.plan(start, goal)
    assert plan.status == "complete", plan.reason_code
    (segment,) = plan.traversal_segments
    assert segment["link_class"] == "step_over"
    assert segment["horizontal_length_m"] < 6.0 and abs(segment["vertical_delta_m"]) <= 0.5
    assert plan.waypoints[segment["start_waypoint_index"]].y < plan.waypoints[segment["end_waypoint_index"]].y
    assert plan.start_ref == start_ref and plan.end_ref == goal_ref
    # Without links the same query was, and is, refused.
    off = graph.plan(start, goal, traversal_policy=om.DISABLED_TRAVERSAL_POLICY)
    assert off.status == "failed" and off.reason_code == "no_linked_polygon_corridor"
    assert off.diagnostics["traversal_hint"]["classes_needed"] == ["step_over"]


def test_large_height_links_in_the_fixture_connect_islands_by_step_downs():
    graph = DetourTopologyGraph.from_dict(girder_payload())
    islands = graph.ground_islands()
    # Island 2 (the higher floor) is left only by a step down of 1.1 m.
    upper = Vec3(4378.0, 65702.0, 152.0)
    lower = Vec3(4188.0, 66010.0, -5.0)
    assert islands[graph.resolve_point(upper).selected_reference] == 2
    assert islands[graph.resolve_point(lower).selected_reference] == 0
    # Step downs are routed by default since 2026-10-21.
    plan = graph.plan(upper, lower)
    assert plan.status == "complete"
    assert [item["link_class"] for item in plan.traversal_segments] == ["step_down"]
    step_overs_only = om.TraversalPolicy(enabled_classes=frozenset({om.CLASS_STEP_OVER}))
    refused = graph.plan(upper, lower, traversal_policy=step_overs_only)
    assert refused.status == "failed"
    hint = refused.diagnostics["traversal_hint"]
    assert hint["reachable_with_excluded_links"] is True and hint["classes_needed"] == ["step_down"]


def test_the_fixture_counts_match_what_the_capture_shows():
    graph = DetourTopologyGraph.from_dict(girder_payload())
    connections = graph.snapshot_diagnostics["off_mesh"]
    assert connections["serialized"] == 75 and connections["linked"] == 64
    assert connections["landing_unresolved"] == 4 and connections["landing_polygon_not_in_graph"] == 7
    classes = {}
    for link in graph.off_mesh_links:
        classes[link.link_class] = classes.get(link.link_class, 0) + 1
    assert classes == {"step_over": 27, "step_up": 14, "drop": 9, "step_down": 7, "climb": 5, "long_span": 2}
    # Flags 64 is one way, 192 two way: the capture's 1,544 linked connections
    # split 1,331 / 213; here the same two values are all there is.
    assert {link.flags for link in graph.off_mesh_links} == {64, 192}


# -- the static obstacle overlay ----------------------------------------------


def overlay_with(*obstacles):
    overlay = StaticObstacleOverlay()
    scope = StaticObstacleScope("session", "EndGame", "EndGame", "F" * 64)
    overlay.bind_scope(scope)
    for obstacle in obstacles:
        overlay.admit_obstacle(scope=scope, obstacle=obstacle, reason_code="test")
    return overlay


def test_overlay_planning_keeps_the_crossing_and_blocks_a_crossing_with_an_obstacle():
    graph = two_pads(STEP)
    clear = plan_with_static_obstacles(graph, A_MIDDLE, B_MIDDLE, overlay_with(), now_t=1.0)
    assert clear.status == "complete" and len(clear.traversal_segments) == 1
    on_link = StaticObstacle(obstacle_id="girder", center=Vec3(500.0, 200.0, 0.0), radius_m=0.6, evidence_count=1)
    blocked = plan_with_static_obstacles(graph, A_MIDDLE, B_MIDDLE, overlay_with(on_link), now_t=1.0)
    assert blocked.status == "failed"
    assert not blocked.traversal_segments


def test_an_obstacle_beside_the_link_is_gridded_round_and_the_crossing_is_kept():
    from ff7r_agent.static_obstacles import route_obstacle_hits

    graph = two_pads(STEP)
    on_the_way = StaticObstacle(obstacle_id="crate", center=Vec3(160.0, 200.0, 0.0), radius_m=0.3, evidence_count=1)
    plan = plan_with_static_obstacles(
        graph, Vec3(30.0, 200.0, 0.0), B_MIDDLE, overlay_with(on_the_way), now_t=1.0,
        max_snap_distance_m=0.75, minimum_edge_clearance_m=0.0,
    )
    assert plan.status == "complete", plan.reason_code
    assert not route_obstacle_hits(plan.waypoints, on_the_way)
    assert len(plan.waypoints) > 4
    (segment,) = plan.traversal_segments
    ((_, index),) = om.locate_traversal_waypoints(plan.waypoints, plan.traversal_segments)
    assert index == segment["start_waypoint_index"]
    assert plan.validation["traversal_count"] == 1
    assert plan.diagnostics["traversal"]["count"] == 1


def test_joined_legs_keep_every_crossing_in_order():
    from ff7r_agent.grid_detour import plan_via_points

    graph = two_pads(STEP)
    base = graph.plan(A_MIDDLE, B_MIDDLE)
    joined = plan_via_points(graph, base, [Vec3(200.0, 300.0, 0.0)])
    assert joined is not None and joined.status == "complete"
    (segment,) = joined.traversal_segments
    assert segment["start_waypoint_index"] == len(joined.waypoints) - 3 or segment["end_waypoint_index"] < len(joined.waypoints)
    ((_, index),) = om.locate_traversal_waypoints(joined.waypoints, joined.traversal_segments)
    assert index == segment["start_waypoint_index"]


def test_overlay_planning_can_be_told_to_leave_links_alone():
    graph = two_pads(STEP)
    plan = plan_with_static_obstacles(
        graph, A_MIDDLE, B_MIDDLE, overlay_with(), now_t=1.0,
        traversal_policy=om.DISABLED_TRAVERSAL_POLICY,
    )
    assert plan.status == "failed"


# -- locating a crossing in a plan ---------------------------------------------


def test_a_crossing_is_found_between_its_two_consecutive_waypoints():
    plan = two_pads(STEP).plan(A_MIDDLE, B_MIDDLE)
    ((segment, index),) = om.locate_traversal_waypoints(plan.waypoints, plan.traversal_segments)
    assert index == segment["start_waypoint_index"]


def test_a_waypoint_inserted_inside_a_crossing_makes_it_unlocatable():
    plan = two_pads(STEP).plan(A_MIDDLE, B_MIDDLE)
    waypoints = list(plan.waypoints)
    waypoints.insert(plan.traversal_segments[0]["end_waypoint_index"], Vec3(500.0, 200.0, 0.0))
    with pytest.raises(ValueError, match="traversal_waypoints_not_found"):
        om.locate_traversal_waypoints(waypoints, plan.traversal_segments)


# -- the report ---------------------------------------------------------------


def test_the_report_lists_islands_and_the_links_bridging_them_by_class():
    graph = DetourTopologyGraph.from_dict(girder_payload())
    report = om.build_traversal_report(graph, source="fixture")
    assert report["schema"] == om.REPORT_SCHEMA
    assert report["islands"]["count"] == 8
    assert report["links"]["by_class"]["step_over"] == 27
    assert report["verified_classes"] == sorted(om.GIMMICK_LINK_CLASSES)
    pair = next(item for item in report["bridges"]["listed"]
                if (item["from_island"], item["to_island"]) == (0, 1))
    assert pair["links_by_class"] == {"step_over": 4}
    bridging = {}
    for item in report["bridges"]["listed"]:
        for name, count in item["links_by_class"].items():
            bridging[name] = bridging.get(name, 0) + count
    assert bridging == graph.snapshot_diagnostics["traversal_links"]["bridging_links_by_class"]
    default = report["connectivity_by_policy"]["step_over_forward"]
    everything = report["connectivity_by_policy"]["all_classes_forward"]
    assert everything["weak_groups"] <= default["weak_groups"] < report["islands"]["count"]
    assert everything["strong_groups"] <= default["strong_groups"]
    json.dumps(report)


def test_the_summary_in_planner_diagnostics_matches_the_report():
    graph = DetourTopologyGraph.from_dict(girder_payload())
    summary = graph.snapshot_diagnostics["traversal_links"]
    assert summary["links"] == 64 and summary["islands"] == 8
    assert summary["unverified_classes"] == sorted(summary["links_by_class"])
    assert summary["islands_joined_by_default_policy"] == 3


def test_the_report_is_written_beside_the_capture_and_never_raises(tmp_path):
    graph = two_pads(STEP)
    capture = tmp_path / "geometry.json"
    capture.write_text("{}", encoding="utf-8")
    written = om.write_traversal_report(graph, capture)
    assert written == tmp_path / "geometry.traversal-report.json"
    assert json.loads(written.read_text(encoding="utf-8"))["links"]["count"] == 1
    assert om.write_traversal_report(graph, tmp_path / "missing-dir" / "geometry.json") is None
    no_links = DetourTopologyGraph.from_dict(pads_payload([]))
    assert om.write_traversal_report(no_links, tmp_path / "plain.json") is None
    assert not (tmp_path / "plain.traversal-report.json").exists()


def test_the_report_command_line_writes_beside_the_capture(tmp_path, capsys):
    capture = tmp_path / "geom.json"
    capture.write_text(json.dumps(girder_payload()), encoding="utf-8")
    assert om.main([str(capture)]) == 0
    assert (tmp_path / "geom.traversal-report.json").is_file()
    assert json.loads(capsys.readouterr().out)["islands"] == 8


# -- the independent validator -------------------------------------------------

VALIDATOR_PATH = Path(__file__).resolve().parents[2] / "ff7r-navigation-validation" / "navigation_validation.py"


@pytest.fixture(scope="module")
def validator_module():
    spec = importlib.util.spec_from_file_location("ff7r_test_navigation_validation", VALIDATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def girder_oracle(validator_module, tmp_path_factory):
    path = tmp_path_factory.mktemp("oracle") / "girder.json"
    path.write_text(json.dumps(girder_payload()), encoding="utf-8")
    return validator_module.RoutePlanValidator(validator_module.TopologyAnalyzer(path))


@pytest.fixture(scope="module")
def girder_plan():
    graph = DetourTopologyGraph.from_dict(girder_payload())
    return graph.plan(Vec3(*GIRDER_START), Vec3(*GIRDER_GOAL)).to_dict()


def test_the_oracle_accepts_the_planned_crossing(girder_oracle, girder_plan):
    result = girder_oracle.validate(girder_plan)
    assert result["valid"] is True, result["errors"]


def mutate(plan, change):
    copy = json.loads(json.dumps(plan))
    change(copy)
    return copy


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p["traversal_segments"][0].update(link_class="drop"), "class disagrees"),
        (lambda p: p["traversal_segments"][0].update(direction="end_to_start"), "backwards"),
        (lambda p: p.update(traversal_segments=[]), "segment count disagrees"),
        (lambda p: p["traversal_segments"][0].update(link_ref=1), "no single off-mesh connection"),
        (lambda p: p["traversal_segments"][0]["start"].update(x=p["traversal_segments"][0]["start"]["x"] + 50.0),
         "points disagree"),
        (lambda p: p["waypoints"].insert(2, {"x": 4400.0, "y": 67000.0, "z": -100.0}),
         "consecutive waypoints"),
        (lambda p: p["portals"][1].update(traversal_class="drop"), "portal disagrees"),
    ],
)
def test_the_oracle_rejects_a_crossing_that_does_not_match_the_data(girder_oracle, girder_plan, change, message):
    result = girder_oracle.validate(mutate(girder_plan, change))
    assert result["valid"] is False
    assert any(message in error for error in result["errors"]), result["errors"]


def test_the_oracle_accepts_a_step_down_route(validator_module, girder_oracle):
    """The checker keeps its own copy of the classes; steps and drops since 2026-10-21."""
    graph = DetourTopologyGraph.from_dict(girder_payload())
    upper, lower = Vec3(4378.0, 65702.0, 152.0), Vec3(4188.0, 66010.0, -5.0)
    plan = graph.plan(upper, lower).to_dict()
    assert [item["link_class"] for item in plan["traversal_segments"]] == ["step_down"]
    assert not any("is not allowed in a route" in error for error in girder_oracle.validate(plan)["errors"])
    assert validator_module.TRAVERSAL_ALLOWED_CLASSES == (
        om.DEFAULT_TRAVERSAL_POLICY.enabled_classes - set(om.GIMMICK_LINK_CLASSES))


def test_the_oracle_rejects_a_route_over_a_class_it_does_not_allow(validator_module, tmp_path):
    payload = pads_payload([connection((350.0, 200.0, 0.0), (650.0, 200.0, 300.0))], rise_cm=(0.0, 300.0))
    path = tmp_path / "pads.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    oracle = validator_module.RoutePlanValidator(validator_module.TopologyAnalyzer(path))
    plan = DetourTopologyGraph.from_dict(payload).plan(
        A_MIDDLE, Vec3(850.0, 200.0, 300.0), traversal_policy=om.EVERYTHING_POLICY).to_dict()
    assert [item["link_class"] for item in plan["traversal_segments"]] == ["climb"]
    errors = oracle.validate(plan)["errors"]
    assert any("is not allowed in a route" in error for error in errors), errors


def test_the_oracle_rejects_the_reverse_of_a_two_way_link_until_it_is_verified(validator_module, tmp_path):
    payload = pads_payload([connection((350.0, 200.0, 0.0), (650.0, 200.0, 0.0), flags=192)])
    path = tmp_path / "pads.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    oracle = validator_module.RoutePlanValidator(validator_module.TopologyAnalyzer(path))
    graph = DetourTopologyGraph.from_dict(payload)
    forward = graph.plan(A_MIDDLE, B_MIDDLE).to_dict()
    assert forward["traversal_segments"][0]["direction"] == "start_to_end"
    reverse = graph.plan(B_MIDDLE, A_MIDDLE, traversal_policy=om.TraversalPolicy(allow_reverse_of_two_way=True)).to_dict()
    errors = oracle.validate(reverse)["errors"]
    assert any("backwards" in error for error in errors), errors
