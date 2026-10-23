import pytest

from ff7r_agent.detour_route import DetourTopologyGraph, Vec3
from ff7r_agent.grid_detour import join_plans
from ff7r_agent.static_obstacles import (
    StaticObstacle, StaticObstacleOverlay, StaticObstacleScope, plan_with_static_obstacles, route_obstacle_hits,
)
from test_topology_cache import topology_payload


def plaza():
    """One 20 m square polygon: the open-ground case the overlay could not detour in."""
    return DetourTopologyGraph.from_dict(topology_payload())


def overlay_with(*obstacles):
    overlay = StaticObstacleOverlay()
    overlay.bind_scope(StaticObstacleScope("session", "EndGame", "EndGame", "F" * 64))
    for obstacle in obstacles:
        overlay.admit_obstacle(scope=overlay.scope, obstacle=obstacle)
    return overlay


def obstacle(identifier, x, y, radius):
    return StaticObstacle(obstacle_id=identifier, center=Vec3(x, y, 0.0), radius_m=radius,
                          cloud_clearance_m=0.45, uncertainty_margin_m=0.1, evidence_count=1)


def test_detours_round_an_obstacle_inside_one_large_polygon(monkeypatch):
    from ff7r_agent import static_obstacles

    # The older strategies are tried first; with them all failing (live: the
    # crowd round the Golden Plumes wagon), the grid is the last resort.
    for name in ("_local_detour", "_same_polygon_visibility_detour", "_portal_contour_detour"):
        monkeypatch.setattr(static_obstacles, name, lambda *a, **k: None)
    graph = plaza()
    # Cloud's feet read a little above the navmesh floor.
    start, end = Vec3(-800.0, 0.0, 30.0), Vec3(800.0, 0.0, 0.0)
    crowd = [obstacle("wagon", 0.0, 0.0, 1.0), obstacle("villager", 150.0, -250.0, 0.3)]
    plan = plan_with_static_obstacles(graph, start, end, overlay_with(*crowd), max_snap_distance_m=0.75,
                                      max_vertical_separation_m=1.0)
    assert plan.status == "complete"
    assert plan.diagnostics["static_obstacle_resolution"] == "grid_detour"
    assert plan.diagnostics["legacy_failure"] == "static_obstacle_local_detour_unproven"
    # The adapter binds the plan to Cloud's observed position exactly.
    assert plan.requested_start == start and plan.requested_end == end
    assert not any(route_obstacle_hits(plan.waypoints, item) for item in crowd)
    assert plan.validation["transition_portals_valid"] and len(plan.portals) == len(plan.polygon_corridor) + 1
    assert 16.0 < plan.total_estimated_path_length_m < 22.0
    spacing = [((b.x - a.x) ** 2 + (b.y - a.y) ** 2) ** 0.5 for a, b in zip(plan.waypoints, plan.waypoints[1:])]
    assert min(spacing) >= 39.0


def test_obstacles_far_apart_are_gridded_separately_and_joined(monkeypatch):
    # Live 2026-10-06 at Cosmo Canyon: a tent at the start and contacts by
    # Ka'dina 130 m on made one band no search could cross; each alone passed.
    from ff7r_agent import grid_detour, static_obstacles

    for name in ("_local_detour", "_same_polygon_visibility_detour", "_portal_contour_detour"):
        monkeypatch.setattr(static_obstacles, name, lambda *a, **k: None)
    monkeypatch.setattr(grid_detour, "STRETCH_MARGIN_CM", 100.0)
    start, end = Vec3(-800.0, 0.0, 0.0), Vec3(800.0, 0.0, 0.0)
    far_apart = [obstacle("tent", -450.0, 0.0, 0.4), obstacle("table", 450.0, 0.0, 0.4)]
    seen = []
    original = grid_detour._stretches
    monkeypatch.setattr(grid_detour, "_stretches", lambda spans, total: seen.append(original(spans, total)) or seen[-1])
    plan = plan_with_static_obstacles(plaza(), start, end, overlay_with(*far_apart), max_snap_distance_m=0.75,
                                      max_vertical_separation_m=1.0)
    assert len(seen[-1]) == 2
    assert plan.status == "complete" and plan.diagnostics["static_obstacle_resolution"] == "grid_detour"
    assert not any(route_obstacle_hits(plan.waypoints, item) for item in far_apart)
    assert plan.requested_start == start and plan.requested_end == end


def test_stretches_join_only_where_their_margins_meet():
    from ff7r_agent.grid_detour import STRETCH_MARGIN_CM, _stretches

    near = _stretches([(1000.0, 1100.0), (1100.0 + STRETCH_MARGIN_CM, 1300.0 + STRETCH_MARGIN_CM)], 20_000.0)
    assert len(near) == 1
    far = _stretches([(9000.0, 9100.0), (1000.0, 1100.0)], 20_000.0)
    assert far == [(1000.0 - STRETCH_MARGIN_CM, 1100.0 + STRETCH_MARGIN_CM),
                   (9000.0 - STRETCH_MARGIN_CM, 9100.0 + STRETCH_MARGIN_CM)]
    assert _stretches([(-50.0, 100.0)], 500.0) == [(0.0, 100.0 + STRETCH_MARGIN_CM if 100.0 + STRETCH_MARGIN_CM < 500.0 else 500.0)]


def test_a_clear_route_is_left_alone():
    graph = plaza()
    plan = plan_with_static_obstacles(graph, Vec3(-800.0, 0.0, 0.0), Vec3(800.0, 0.0, 0.0),
                                      overlay_with(obstacle("aside", 0.0, 700.0, 0.3)),
                                      max_snap_distance_m=0.75, max_vertical_separation_m=1.0)
    assert plan.status == "complete" and len(plan.waypoints) == 2


def test_no_way_through_still_fails_closed():
    graph = plaza()
    wall = [obstacle(f"w{i}", 0.0, y, 0.6) for i, y in enumerate(range(-1000, 1001, 150))]
    plan = plan_with_static_obstacles(graph, Vec3(-800.0, 0.0, 0.0), Vec3(800.0, 0.0, 0.0), overlay_with(*wall),
                                      max_snap_distance_m=0.75, max_vertical_separation_m=1.0)
    assert plan.status == "failed" and plan.diagnostics.get("static_obstacle_resolution") != "grid_detour"


def test_joined_legs_keep_one_start_and_end_portal():
    graph = plaza()
    a = graph.plan(Vec3(-800.0, 0.0, 0.0), Vec3(0.0, 500.0, 0.0))
    b = graph.plan(Vec3(0.0, 500.0, 0.0), Vec3(800.0, 0.0, 0.0), start_resolution_override=a.end_resolution)
    joined = join_plans([a, b])
    assert joined is not None
    assert len(joined.portals) == len(joined.polygon_corridor) + 1
    assert joined.waypoints[0] == a.waypoints[0] and joined.waypoints[-1] == b.waypoints[-1]
    assert len(joined.waypoints) == len(a.waypoints) + len(b.waypoints) - 1


def test_joined_length_is_the_3d_polyline_length():
    from dataclasses import replace

    graph = plaza()
    a = graph.plan(Vec3(-800.0, 0.0, 0.0), Vec3(0.0, 500.0, 0.0))
    b = graph.plan(Vec3(0.0, 500.0, 0.0), Vec3(800.0, 0.0, 0.0), start_resolution_override=a.end_resolution)
    # A climb on the second leg, as at Kalm; the validator measures in 3D.
    b = replace(b, waypoints=tuple(Vec3(p.x, p.y, 0.0 if i == 0 else 400.0) for i, p in enumerate(b.waypoints)))
    joined = join_plans([a, b])
    polyline = sum(((q.x - p.x) ** 2 + (q.y - p.y) ** 2 + (q.z - p.z) ** 2) ** 0.5
                   for p, q in zip(joined.waypoints, joined.waypoints[1:])) / 100.0
    assert joined.total_estimated_path_length_m == pytest.approx(polyline, abs=1e-6)
