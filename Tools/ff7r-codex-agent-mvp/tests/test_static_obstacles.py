from __future__ import annotations

from dataclasses import replace
import json
import pytest

from ff7r_agent.detour_route import (
    DetourLink,
    DetourPolygon,
    DetourPortal,
    DetourTopologyGraph,
    PolygonReference,
    _distance_m,
    _portal_from_link,
)
from ff7r_agent.models import Vec3
from ff7r_agent.dynamic_obstacles import (
    DynamicObstacleStaticEvidence,
    DynamicObstacleObservation,
    DynamicObstacleSnapshot,
    Point3M,
    StaticLearningDynamicState,
    assess_static_learning_dynamic_evidence,
)
from ff7r_agent.static_obstacles import (
    NavigationSidecarLookahead,
    ObstacleEvidenceState,
    StaticObstacle,
    StaticObstacleDurableIdentity,
    StaticObstacleOverlay,
    StaticObstaclePlanningError,
    StaticObstacleScope,
    StaticObstacleStore,
    StaticObstaclePromotionEvidence,
    _distance_point_to_segment_m,
    _point_in_convex_polygon,
    _segment_authorized_corridor,
    plan_with_static_obstacles,
    route_obstacle_hits,
)


def _polygon(reference: int, vertices: tuple[Vec3, ...], links=()):
    return DetourPolygon(
        reference=PolygonReference.make(1, reference, 0),
        tile_x=reference,
        tile_y=0,
        layer=0,
        vertices=vertices,
        flags=1,
        area=0,
        poly_type=0,
        links=tuple(links),
        identity_source="test",
    )


def _portal(
    source: int,
    target: int,
    left: Vec3,
    right: Vec3,
    edge: int = 0,
    target_edge: int | None = None,
):
    return DetourPortal(source, target, target, edge, edge if target_edge is None else target_edge, left, right, 128)


def _single_polygon_graph() -> DetourTopologyGraph:
    polygon = _polygon(
        1,
        (
            Vec3(0, 0, 0),
            Vec3(3000, 0, 0),
            Vec3(3000, 2000, 0),
            Vec3(0, 2000, 0),
        ),
    )
    return DetourTopologyGraph({polygon.reference.value: polygon}, {}, snapshot_diagnostics={})


def _corridor_graph() -> tuple[DetourTopologyGraph, int, int]:
    ref_values = {index: PolygonReference.make(1, index, 0).value for index in (1, 2, 3, 4)}
    a = _polygon(
        1,
        (Vec3(0, 0, 0), Vec3(1000, 0, 0), Vec3(1000, 1000, 0), Vec3(0, 1000, 0)),
        (DetourLink(ref_values[2], 1, 0, 128, 0, 0), DetourLink(ref_values[4], 2, 0, 128, 0, 0)),
    )
    b = _polygon(
        2,
        (Vec3(1000, 0, 0), Vec3(2000, 0, 0), Vec3(2000, 1000, 0), Vec3(1000, 1000, 0)),
        (DetourLink(ref_values[3], 1, 0, 128, 0, 0),),
    )
    c = _polygon(
        3,
        (Vec3(2000, 0, 0), Vec3(3000, 0, 0), Vec3(3000, 1000, 0), Vec3(2000, 1000, 0)),
    )
    d = _polygon(
        4,
        (Vec3(0, 1000, 0), Vec3(3000, 1000, 0), Vec3(3000, 2000, 0), Vec3(0, 2000, 0)),
        (DetourLink(ref_values[3], 0, 0, 128, 0, 0),),
    )
    refs = {item.reference.value: item for item in (a, b, c, d)}
    adjacency = {
        a.reference.value: (
            _portal(a.reference.value, b.reference.value, Vec3(1000, 0, 0), Vec3(1000, 1000, 0), edge=1, target_edge=3),
            _portal(a.reference.value, d.reference.value, Vec3(0, 1000, 0), Vec3(1000, 1000, 0), edge=2, target_edge=0),
        ),
        b.reference.value: (_portal(b.reference.value, c.reference.value, Vec3(2000, 0, 0), Vec3(2000, 1000, 0), edge=1, target_edge=3),),
        d.reference.value: (_portal(d.reference.value, c.reference.value, Vec3(2500, 1000, 0), Vec3(3000, 1000, 0), edge=0, target_edge=2),),
        c.reference.value: (),
    }
    # Portal-link validation in the shipping planner proves the target ref
    # and source edge from the source polygon's link record.
    return DetourTopologyGraph(refs, adjacency, snapshot_diagnostics={}), a.reference.value, c.reference.value


def _portal_endpoint_detour_graph() -> DetourTopologyGraph:
    """Build a bent two-polygon corridor whose funnel waypoint is a portal endpoint."""

    source_vertices = (
        Vec3(0, 0, 0),
        Vec3(1000, 0, 0),
        Vec3(1000, 1000, 0),
        Vec3(0, 1000, 0),
    )
    target_vertices = (
        Vec3(1000, 800, 0),
        Vec3(2000, 800, 0),
        Vec3(2000, 1800, 0),
        Vec3(1000, 1800, 0),
    )
    target = _polygon(12, target_vertices)
    source = _polygon(
        11,
        source_vertices,
        (DetourLink(target.reference.value, 1, 0, 128, 0, 0),),
    )
    portal = _portal(
        source.reference.value,
        target.reference.value,
        Vec3(1000, 800, 0),
        Vec3(1000, 1000, 0),
        edge=1,
        target_edge=3,
    )
    return DetourTopologyGraph(
        {source.reference.value: source, target.reference.value: target},
        {source.reference.value: (portal,), target.reference.value: ()},
        snapshot_diagnostics={},
    )


def _post_corner_detour_graph() -> DetourTopologyGraph:
    """Build the route-17-shaped corridor with two funnel corners before a hit."""

    ref_values = {
        index: PolygonReference.make(1, index, 0).value
        for index in range(21, 27)
    }
    vertices = (
        (
            Vec3(-10, -800, 380),
            Vec3(-70, -770, 410),
            Vec3(-70, -605, 410),
        ),
        (
            Vec3(-10, -800, 380),
            Vec3(-70, -605, 410),
            Vec3(-25, -560, 410),
            Vec3(920, -560, 380),
            Vec3(920, -770, 380),
            Vec3(890, -1010, 380),
        ),
        (
            Vec3(-25, -560, 410),
            Vec3(5, -440, 420),
            Vec3(80, -230, 390),
            Vec3(920, -560, 380),
        ),
        (
            Vec3(920, -560, 380),
            Vec3(80, -230, 390),
            Vec3(95, -140, 390),
        ),
        (
            Vec3(95, -140, 390),
            Vec3(80, 55, 410),
            Vec3(80, 430, 420),
            Vec3(830, 430, 380),
            Vec3(920, 355, 400),
            Vec3(920, -560, 380),
        ),
        (
            Vec3(920, 595, 390),
            Vec3(905, 430, 390),
            Vec3(800, 430, 380),
            Vec3(200, 985, 410),
            Vec3(200, 1135, 410),
            Vec3(920, 1420, 420),
        ),
    )
    portal_data = (
        (2, 0, Vec3(-70, -605, 410), Vec3(-10, -800, 380), 128),
        (2, 3, Vec3(-25, -560, 410), Vec3(920, -560, 380), 6),
        (2, 0, Vec3(80, -230, 390), Vec3(920, -560, 380), 128),
        (2, 5, Vec3(95, -140, 390), Vec3(920, -560, 380), 128),
        (2, 1, Vec3(497.6470588, 430, 381.72549), Vec3(830, 430, 380), 6),
    )
    polygons = {
        ref_values[index]: _polygon(
            index,
            vertices[index - 21],
            (
                DetourLink(
                    ref_values[index + 1],
                    2,
                    0xFFFFFFFF,
                    128,
                    0,
                    255,
                ),
            ) if index < 26 else (),
        )
        for index in range(21, 27)
    }
    adjacency = {reference: () for reference in ref_values.values()}
    for index, (source_edge, target_edge, left, right, side) in enumerate(portal_data):
        source = ref_values[index + 21]
        target = ref_values[index + 22]
        adjacency[source] = (
            _portal(
                source,
                target,
                left,
                right,
                edge=source_edge,
                target_edge=target_edge,
            ),
        )
    return DetourTopologyGraph(polygons, adjacency, snapshot_diagnostics={})


def _scope(name: str = "run-1") -> StaticObstacleScope:
    return StaticObstacleScope(name, "world", "map", "topology")


def _obstacle(scope: StaticObstacleScope, *, center: Vec3, shape: str = "circle", half_length_m: float = 0.0):
    return StaticObstacle(
        obstacle_id=f"fixture-{scope.session_id}-{center.x}-{center.y}",
        center=center,
        radius_m=0.45,
        cloud_clearance_m=0.65,
        uncertainty_margin_m=0.15,
        shape=shape,
        half_length_m=half_length_m,
        heading_degrees=90.0,
        confidence=0.95,
        evidence_count=2,
        first_sequence=1,
        last_sequence=2,
        first_t=1.0,
        last_t=2.0,
        provenance=("fixture",),
    )


def test_tree_like_blocker_inside_walkable_polygon_gets_local_detour():
    graph = _single_polygon_graph()
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    obstacle = _obstacle(scope, center=Vec3(1500, 1000, 0))
    overlay.admit_obstacle(scope=scope, obstacle=obstacle)

    plan = plan_with_static_obstacles(graph, Vec3(300, 1000, 0), Vec3(2700, 1000, 0), overlay, now_t=2.0)

    assert plan.status == "complete"
    assert plan.diagnostics["static_obstacle_resolution"] == "corridor_local_detour"
    assert plan.waypoints[0] == Vec3(300, 1000, 0)
    assert plan.waypoints[-1] == Vec3(2700, 1000, 0)
    assert not route_obstacle_hits(plan.waypoints, obstacle)
    assert any(point.y != 1000 for point in plan.waypoints[1:-1])



def test_no_progress_learning_promotes_only_after_bounded_confirmation():
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    kwargs = dict(
        scope=scope,
        route_plan_sha256="ROUTE",
        route=(Vec3(300, 1000, 0), Vec3(2700, 1000, 0)),
        position_before=Vec3(300, 1000, 0),
        position_after=Vec3(300, 1000, 0),
        along_route_before_m=0.0,
        along_route_after_m=0.0,
        commanded_bearing_degrees=0.0,
        command_duration_ms=250,
        telemetry_fresh=True,
    )
    first = overlay.learn_from_movement(sequence=1, sampled_t=1.0, **kwargs)
    second = overlay.learn_from_movement(sequence=2, sampled_t=2.0, **kwargs)
    third = overlay.learn_from_movement(sequence=3, sampled_t=3.0, **kwargs)

    assert first.state == ObstacleEvidenceState.CANDIDATE
    assert second.state == ObstacleEvidenceState.CANDIDATE
    assert third.state == ObstacleEvidenceState.READY
    assert third.obstacle is not None
    assert "dynamic_evidence_absent" in third.obstacle.provenance


def test_post_divergence_capsule_confirmation_requires_two_distinct_samples():
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    kwargs = dict(
        scope=scope,
        route_plan_sha256="ROUTE",
        route=(Vec3(300, 1000, 0), Vec3(2700, 1000, 0)),
        position_before=Vec3(300, 1000, 0),
        position_after=Vec3(300, 1000, 0),
        along_route_before_m=0.0,
        along_route_after_m=0.0,
        commanded_bearing_degrees=0.0,
        command_duration_ms=250,
        telemetry_fresh=True,
        query_status="blocked",
        query_provenance="post_divergence_capsule_confirmation",
        query_verified=True,
        capsule_radius_m=0.42,
        blocking_distance_m=1.0,
        probe_world_bearing_degrees=0.0,
        dynamic_evidence=DynamicObstacleStaticEvidence(
            StaticLearningDynamicState.CLEAR_COMPLETE,
            "no_dynamic_actor_in_intended_movement_envelope",
            source_identity="synthetic",
            base_sequence=10,
        ),
        require_independent_confirmation=True,
    )
    first = overlay.learn_from_movement(sequence=10, sampled_t=10.0, **kwargs)
    duplicate = overlay.learn_from_movement(sequence=10, sampled_t=10.1, **kwargs)
    second = overlay.learn_from_movement(sequence=11, sampled_t=11.0, **kwargs)

    assert first.state == ObstacleEvidenceState.CANDIDATE
    assert first.evidence_count == 1
    assert duplicate.reason_code == "static_obstacle_duplicate_evidence"
    assert duplicate.evidence_count == 1
    assert second.state == ObstacleEvidenceState.READY
    assert second.obstacle is not None
    assert second.obstacle.evidence_count == 2


def test_reroute_around_learned_blocker_does_not_retry_same_polyline():
    graph = _single_polygon_graph()
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    for sequence in (1, 2, 3):
        result = overlay.learn_from_movement(
            scope=scope,
            route_plan_sha256="ROUTE",
            route=(Vec3(300, 1000, 0), Vec3(2700, 1000, 0)),
            position_before=Vec3(300, 1000, 0),
            position_after=Vec3(300, 1000, 0),
            along_route_before_m=0.0,
            along_route_after_m=0.0,
            commanded_bearing_degrees=0.0,
            command_duration_ms=250,
            sequence=sequence,
            sampled_t=float(sequence),
        )
    assert result.state == ObstacleEvidenceState.READY
    plan = plan_with_static_obstacles(graph, Vec3(300, 1000, 0), Vec3(2700, 1000, 0), overlay, now_t=2.0)
    assert plan.diagnostics["static_obstacle_resolution"] == "corridor_local_detour"
    assert plan.waypoints != (Vec3(300, 1000, 0), Vec3(2700, 1000, 0))


def test_long_fence_blocks_portal_and_escalates_to_global_route():
    graph, start_ref, end_ref = _corridor_graph()
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    overlay.admit_obstacle(
        scope=scope,
        obstacle=_obstacle(scope, center=Vec3(2000, 500, 0), shape="capsule", half_length_m=0.5),
    )
    base = graph.plan(Vec3(500, 500, 0), Vec3(2500, 500, 0))
    plan = plan_with_static_obstacles(graph, Vec3(500, 500, 0), Vec3(2500, 500, 0), overlay, now_t=2.0)

    middle_ref = next(reference for reference in graph.polygons if reference not in {start_ref, end_ref})
    assert base.polygon_corridor == (start_ref, middle_ref, end_ref)
    assert plan.status == "complete"
    assert plan.diagnostics["static_obstacle_resolution"] == "global_replan_still_blocked" or plan.polygon_corridor != base.polygon_corridor
    assert plan.polygon_corridor != base.polygon_corridor


def test_blocker_inside_intermediate_corridor_polygon_uses_local_detour():
    graph, _, _ = _corridor_graph()
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    obstacle = _obstacle(scope, center=Vec3(1500, 500, 0))
    overlay.admit_obstacle(scope=scope, obstacle=obstacle)

    plan = plan_with_static_obstacles(graph, Vec3(500, 500, 0), Vec3(2500, 500, 0), overlay, now_t=2.0)

    assert plan.status == "complete"
    assert plan.diagnostics["static_obstacle_resolution"] == "corridor_local_detour"
    assert not route_obstacle_hits(plan.waypoints, obstacle)


def test_effective_blocker_radius_includes_cloud_clearance_and_uncertainty():
    scope = _scope()
    obstacle = _obstacle(scope, center=Vec3(0, 0, 0))
    assert obstacle.effective_radius_m == pytest.approx(1.25)
    assert obstacle.effective_radius_m > obstacle.radius_m


def test_scope_change_clears_old_blockers_and_mismatched_learning():
    overlay = StaticObstacleOverlay()
    first = _scope("run-1")
    second = _scope("run-2")
    overlay.bind_scope(first)
    overlay.admit_obstacle(scope=first, obstacle=_obstacle(first, center=Vec3(100, 100, 0)))
    overlay.bind_scope(second)

    assert overlay.snapshot().obstacles == ()
    result = overlay.learn_from_movement(
        scope=first,
        route_plan_sha256="ROUTE",
        route=(Vec3(0, 0, 0), Vec3(1000, 0, 0)),
        position_before=Vec3(0, 0, 0),
        position_after=Vec3(0, 0, 0),
        along_route_before_m=0.0,
        along_route_after_m=0.0,
        commanded_bearing_degrees=0.0,
        command_duration_ms=200,
        sequence=1,
        sampled_t=1.0,
    )
    assert result.state == ObstacleEvidenceState.UNKNOWN
    assert result.reason_code == "static_obstacle_scope_mismatch"


def test_stale_evidence_fails_closed_for_planning():
    overlay = StaticObstacleOverlay(maximum_evidence_age_seconds=1.0)
    scope = _scope()
    overlay.bind_scope(scope)
    overlay.admit_obstacle(scope=scope, obstacle=_obstacle(scope, center=Vec3(100, 100, 0)))
    with pytest.raises(StaticObstaclePlanningError, match="stale_static_obstacle_evidence"):
        overlay.planning_snapshot(now_t=10.0)


def test_uncertain_telemetry_and_lookahead_do_not_authorize_blocker():
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    result = overlay.learn_from_movement(
        scope=scope,
        route_plan_sha256="ROUTE",
        route=(Vec3(0, 0, 0), Vec3(1000, 0, 0)),
        position_before=Vec3(0, 0, 0),
        position_after=Vec3(0, 0, 0),
        along_route_before_m=0.0,
        along_route_after_m=0.0,
        commanded_bearing_degrees=0.0,
        command_duration_ms=200,
        sequence=1,
        sampled_t=1.0,
        telemetry_fresh=False,
    )
    assert result.state == ObstacleEvidenceState.UNKNOWN
    assert overlay.snapshot().obstacles == ()

    lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=1)
    raw = {"navigation": {"schema_version": 1, "source": "ue4ss_line_trace_multi_height_capsule", "base_sequence": 1, "directions": {"forward": {"sample_sequence": 99, "clear_distance_m": 1.0}}}}
    decision = lookahead.inspect(raw, sequence=1, route_plan_sha256="ROUTE", desired_world_bearing_degrees=0.0, camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True)
    assert decision.state == ObstacleEvidenceState.UNKNOWN
    assert decision.reason_code == "lookahead_direction_sequence_mismatch"


def test_unobstructed_navigation_is_byte_for_byte_unchanged():
    graph = _single_polygon_graph()
    overlay = StaticObstacleOverlay()
    overlay.bind_scope(_scope())
    base = graph.plan(Vec3(300, 1000, 0), Vec3(2700, 1000, 0))
    overlaid = plan_with_static_obstacles(graph, Vec3(300, 1000, 0), Vec3(2700, 1000, 0), overlay, now_t=2.0)
    assert overlaid.to_dict() == base.to_dict()


def test_lookahead_is_exact_sequence_and_bounded_to_one_query_per_plan():
    lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=1)
    raw = {
        "navigation": {
            "schema_version": 1,
            "source": "ue4ss_line_trace_multi_height_capsule",
            "base_sequence": 7,
            "directions": {
                "forward": {
                    "sample_sequence": 7,
                    "hit": True,
                    "clear_distance_m": 1.0,
                    "capsule_probe": {
                        "available": True,
                        "fresh": True,
                        "sample_sequence": 7,
                        "blocking_hit": True,
                        "clear_distance_m": 1.0,
                        "radius_m": 0.42,
                    },
                }
            },
        }
    }
    first = lookahead.inspect(raw, sequence=7, route_plan_sha256="ROUTE", desired_world_bearing_degrees=0.0, camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True)
    second = lookahead.inspect(raw, sequence=7, route_plan_sha256="ROUTE", desired_world_bearing_degrees=0.0, camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True)
    assert first.state == ObstacleEvidenceState.READY
    assert first.query_verified is True
    assert first.reason_code == "lookahead_capsule_blocked"
    assert second.reason_code == "lookahead_budget_exhausted"


def _capsule_sidecar(sequence: int, *, base_sequence: int | None = None, direction: str = "forward") -> dict:
    return {
        "navigation": {
            "schema_version": 1,
            "source": "ue4ss_line_trace_multi_height_capsule",
            "base_sequence": sequence if base_sequence is None else base_sequence,
            "directions": {
                direction: {
                    "sample_sequence": sequence,
                    "clear_distance_m": 2.0,
                    "capsule_probe": {
                        "available": True,
                        "fresh": True,
                        "sample_sequence": sequence,
                        "blocking_hit": True,
                        "clear_distance_m": 2.0,
                        "radius_m": 0.42,
                    },
                },
            },
        },
    }


def test_lookahead_invalid_attempt_does_not_consume_usable_probe_budget():
    lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=1, maximum_attempts_per_plan=3)
    missing = lookahead.inspect(
        None, sequence=1, route_plan_sha256="budget", desired_world_bearing_degrees=0.0,
        camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True,
    )
    wrong_sequence = lookahead.inspect(
        _capsule_sidecar(99, base_sequence=2), sequence=2, route_plan_sha256="budget",
        desired_world_bearing_degrees=0.0, camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True,
    )
    valid = lookahead.inspect(
        _capsule_sidecar(3), sequence=3, route_plan_sha256="budget", desired_world_bearing_degrees=0.0,
        camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True,
    )
    assert missing.reason_code == "lookahead_navigation_payload_missing"
    assert wrong_sequence.reason_code == "lookahead_direction_sequence_mismatch"
    assert valid.reason_code == "lookahead_capsule_blocked"
    status = lookahead.budget_status("budget")
    assert status["used_attempts"] == 3
    assert status["maximum_attempts"] == 3
    assert status["used_usable_queries"] == 1
    assert status["maximum_usable_queries"] == 1


def test_lookahead_hard_attempt_budget_stops_repeated_invalid_polling():
    lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=2, maximum_attempts_per_plan=2)
    for sequence in (1, 2):
        decision = lookahead.inspect(
            {"navigation": {"schema_version": 99}}, sequence=sequence, route_plan_sha256="hard",
            desired_world_bearing_degrees=0.0, camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True,
        )
        assert decision.reason_code == "lookahead_schema_invalid"
    exhausted = lookahead.inspect(
        _capsule_sidecar(3), sequence=3, route_plan_sha256="hard", desired_world_bearing_degrees=0.0,
        camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True,
    )
    assert exhausted.reason_code == "lookahead_budget_exhausted"
    status = lookahead.budget_status("hard")
    assert status["used_attempts"] == status["maximum_attempts"] == 2
    assert status["used_usable_queries"] == 0


def test_two_valid_lookahead_samples_exhaust_only_usable_probe_budget():
    lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=2, maximum_attempts_per_plan=5)
    for sequence in (1, 2):
        decision = lookahead.inspect(
            _capsule_sidecar(sequence), sequence=sequence, route_plan_sha256="two",
            desired_world_bearing_degrees=0.0, camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True,
        )
        assert decision.query_verified is True
    third = lookahead.inspect(
        _capsule_sidecar(3), sequence=3, route_plan_sha256="two", desired_world_bearing_degrees=0.0,
        camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True,
    )
    assert third.reason_code == "lookahead_budget_exhausted"
    status = lookahead.budget_status("two")
    assert status["used_attempts"] == 2
    assert status["used_usable_queries"] == status["maximum_usable_queries"] == 2


def test_route_plan_identity_digest_changes_when_overlay_changes():
    graph = _single_polygon_graph()
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    before = overlay.planning_snapshot(now_t=2.0).digest()
    overlay.admit_obstacle(scope=scope, obstacle=_obstacle(scope, center=Vec3(1500, 1000, 0)))
    after = overlay.planning_snapshot(now_t=2.0).digest()
    assert before != after
    assert after == overlay.planning_snapshot(now_t=2.0).digest()


def test_stale_obstacle_does_not_poison_fresh_obstacle_in_same_scope():
    overlay = StaticObstacleOverlay(maximum_evidence_age_seconds=2.0)
    scope = _scope()
    overlay.bind_scope(scope)
    stale = _obstacle(scope, center=Vec3(800, 800, 0))
    fresh = _obstacle(scope, center=Vec3(1500, 1000, 0))
    overlay.admit_obstacle(scope=scope, obstacle=stale)
    overlay.admit_obstacle(scope=scope, obstacle=fresh)
    overlay._obstacles[stale.obstacle_id] = StaticObstacle(**{**stale.__dict__, "last_t": 1.0})
    overlay._obstacles[fresh.obstacle_id] = StaticObstacle(**{**fresh.__dict__, "last_t": 9.0})

    snapshot = overlay.planning_snapshot(now_t=10.0)

    assert snapshot.state == ObstacleEvidenceState.READY
    assert [item.obstacle_id for item in snapshot.obstacles] == [fresh.obstacle_id]
    assert snapshot.stale_obstacle_ids == (stale.obstacle_id,)


def test_topology_companion_promotes_across_sessions_and_rejects_other_identity(tmp_path):
    identity = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY", content_hashes=("ARTIFACT",))
    store = StaticObstacleStore(artifact_path=tmp_path / "static-obstacles.json")
    first_scope = _scope("run-1")
    kwargs = dict(
        route_plan_sha256="ROUTE",
        route=(Vec3(300, 1000, 0), Vec3(2700, 1000, 0)),
        position_before=Vec3(300, 1000, 0),
        position_after=Vec3(300, 1000, 0),
        along_route_before_m=0.0,
        along_route_after_m=0.0,
        commanded_bearing_degrees=0.0,
        command_duration_ms=250,
        telemetry_fresh=True,
    )
    first = StaticObstacleOverlay(store=store)
    first.bind_scope(first_scope, durable_identity=identity)
    first.learn_from_movement(scope=first_scope, sequence=1, sampled_t=1.0, **kwargs)
    first.learn_from_movement(scope=first_scope, sequence=2, sampled_t=2.0, **kwargs)
    learned = first.learn_from_movement(scope=first_scope, sequence=3, sampled_t=3.0, **kwargs)
    assert learned.obstacle is not None and learned.obstacle.persistence == "session"

    second_scope = _scope("run-2")
    second = StaticObstacleOverlay(store=store)
    second.bind_scope(second_scope, durable_identity=identity)
    second.learn_from_movement(scope=second_scope, sequence=3, sampled_t=3.0, **kwargs)
    second.learn_from_movement(scope=second_scope, sequence=4, sampled_t=4.0, **kwargs)
    promoted = second.learn_from_movement(scope=second_scope, sequence=5, sampled_t=5.0, **kwargs)
    assert promoted.obstacle is not None and promoted.obstacle.persistence == "durable"

    restored = StaticObstacleStore(artifact_path=tmp_path / "static-obstacles.json")
    unrelated = StaticObstacleOverlay(store=restored)
    unrelated.bind_scope(_scope("run-3"), durable_identity=StaticObstacleDurableIdentity("BUILD", "other-map", "TOPOLOGY"))
    assert unrelated.snapshot().obstacles == ()
    matching = StaticObstacleOverlay(store=restored)
    matching.bind_scope(_scope("run-3"), durable_identity=identity)
    assert len(matching.snapshot().obstacles) == 1
    assert matching.snapshot().obstacles[0].persistence == "durable"


def _ledger_obstacle(obstacle_id: str, center: Vec3) -> StaticObstacle:
    return StaticObstacle(
        obstacle_id=obstacle_id,
        center=center,
        radius_m=0.45,
        cloud_clearance_m=0.65,
        uncertainty_margin_m=0.15,
        confidence=0.95,
        evidence_count=2,
        first_sequence=1,
        last_sequence=2,
        first_t=1.0,
        last_t=2.0,
        provenance=("runtime_capsule_blocked",),
    )


def test_confirmation_ledger_survives_topology_switch_and_restart_without_authorizing_geometry(tmp_path):
    identity_a = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY-A", content_hashes=("A",))
    identity_b = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY-B", content_hashes=("B",))
    root = tmp_path / "companions"
    obstacle = _ledger_obstacle("cell-a", Vec3(1500, 1000, 0))

    service = StaticObstacleStore(artifact_root=root, minimum_durable_sessions=2)
    service.bind_identity(identity_a)
    service.record(scope=_scope("session-a-1"), identity=identity_a, obstacle=obstacle)
    assert service.durable_obstacles(identity_a) == ()
    ledger_path = root / f"{identity_a.key()}.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(payload["confirmation_ledger"]) == 1

    # A→B→A must reload A's non-authorizing ledger rather than dropping it.
    service.bind_identity(identity_b)
    assert service.durable_obstacles(identity_b) == ()
    service.bind_identity(identity_a)
    assert service.durable_obstacles(identity_a) == ()
    assert service.load_for_scope(_scope("session-a-2"), identity_a) == ()

    # A process/service restart still sees the first independent session.
    restarted = StaticObstacleStore(artifact_root=root, minimum_durable_sessions=2)
    restarted.bind_identity(identity_a)
    restarted.record(scope=_scope("session-a-2"), identity=identity_a, obstacle=obstacle)
    promoted = restarted.durable_obstacles(identity_a)
    assert len(promoted) == 1
    assert promoted[0].persistence == "durable"
    assert promoted[0].promotion_reason == "independent_session_confirmations"

    # Topology B never inherits A's ledger and confirmation-only entries are
    # absent from planning snapshots/digests until promotion.
    restarted.bind_identity(identity_b)
    assert restarted.load_for_scope(_scope("session-b"), identity_b) == ()


def test_confirmation_ledger_does_not_count_same_session_and_is_bounded(tmp_path):
    identity = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY")
    path = tmp_path / "companion.json"
    store = StaticObstacleStore(
        artifact_path=path,
        minimum_durable_sessions=3,
        max_confirmation_records=2,
        max_confirmation_sessions=3,
    )
    obstacle = _ledger_obstacle("same-cell", Vec3(1000, 1000, 0))
    same_scope = _scope("same-session")
    store.record(scope=same_scope, identity=identity, obstacle=obstacle)
    store.record(scope=same_scope, identity=identity, obstacle=obstacle)
    assert len(store._confirmations[(identity.key(), obstacle.obstacle_id)]) == 1
    assert store.artifact_dict()["next_confirmation_generation"] == 1
    for index in range(5):
        store.record(
            scope=_scope(f"independent-{index}"),
            identity=identity,
            obstacle=_ledger_obstacle(f"cell-{index}", Vec3(1000 + index * 100, 1000, 0)),
        )
    assert len(store._confirmations) <= 2
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert len(persisted["confirmation_ledger"]) <= 2


def test_confirmation_generation_survives_restart_and_prunes_newest_records(tmp_path):
    identity = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY")
    path = tmp_path / "companion.json"
    store = StaticObstacleStore(
        artifact_path=path,
        minimum_durable_sessions=4,
        max_confirmation_records=3,
    )
    for index in range(2):
        store.record(
            scope=_scope(f"before-{index}"),
            identity=identity,
            obstacle=_ledger_obstacle(f"cell-{index}", Vec3(1000 + index * 100, 1000, 0)),
        )
    before = json.loads(path.read_text(encoding="utf-8"))
    assert before["next_confirmation_generation"] == 2
    before_generations = {
        item["obstacle_id"]: item["last_seen_generation"]
        for item in before["confirmation_ledger"]
    }

    restarted = StaticObstacleStore(
        artifact_path=path,
        minimum_durable_sessions=4,
        max_confirmation_records=3,
    )
    restarted.record(
        scope=_scope("after-restart"),
        identity=identity,
        obstacle=_ledger_obstacle("cell-2", Vec3(1200, 1000, 0)),
    )
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["next_confirmation_generation"] == 3
    assert after["confirmation_ledger"][-1]["last_seen_generation"] in {1, 2, 3}
    after_generations = {
        item["obstacle_id"]: item["last_seen_generation"]
        for item in after["confirmation_ledger"]
    }
    assert after_generations["cell-2"] > max(before_generations.values())

    pruned = StaticObstacleStore(
        artifact_path=path,
        minimum_durable_sessions=4,
        max_confirmation_records=2,
    )
    retained = {
        item["obstacle_id"]: item["last_seen_generation"]
        for item in pruned.artifact_dict()["confirmation_ledger"]
    }
    assert set(retained) == {"cell-1", "cell-2"}
    assert min(retained.values()) == 2
    persisted_after_prune = json.loads(path.read_text(encoding="utf-8"))
    assert {item["obstacle_id"] for item in persisted_after_prune["confirmation_ledger"]} == {"cell-1", "cell-2"}


def test_topology_companions_keep_independent_confirmation_generations(tmp_path):
    identity_a = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY-A")
    identity_b = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY-B")
    root = tmp_path / "companions"
    store = StaticObstacleStore(artifact_root=root, minimum_durable_sessions=3)
    store.bind_identity(identity_a)
    store.record(
        scope=_scope("a-1"), identity=identity_a,
        obstacle=_ledger_obstacle("cell-a", Vec3(1000, 1000, 0)),
    )
    store.bind_identity(identity_b)
    store.record(
        scope=_scope("b-1"), identity=identity_b,
        obstacle=_ledger_obstacle("cell-b", Vec3(1000, 1000, 0)),
    )
    artifact_a = json.loads((root / f"{identity_a.key()}.json").read_text(encoding="utf-8"))
    artifact_b = json.loads((root / f"{identity_b.key()}.json").read_text(encoding="utf-8"))
    assert artifact_a["next_confirmation_generation"] == 1
    assert artifact_b["next_confirmation_generation"] == 1


def test_legacy_confirmation_ordering_migrates_without_using_monotonic_timestamps(tmp_path):
    identity = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY")
    path = tmp_path / "legacy.json"
    store = StaticObstacleStore(artifact_path=path, minimum_durable_sessions=4)
    for index in range(2):
        store.record(
            scope=_scope(f"legacy-{index}"), identity=identity,
            obstacle=_ledger_obstacle(f"legacy-cell-{index}", Vec3(1000 + index * 100, 1000, 0)),
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("next_confirmation_generation")
    for entry in payload["confirmation_ledger"]:
        entry.pop("last_seen_generation")
        entry["last_seen"] = 48231.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = StaticObstacleStore(artifact_path=path, minimum_durable_sessions=4)
    assert restored.load_error is None
    assert restored.artifact_dict()["next_confirmation_generation"] == 2
    restored.record(
        scope=_scope("legacy-new"), identity=identity,
        obstacle=_ledger_obstacle("legacy-cell-2", Vec3(1200, 1000, 0)),
    )
    assert restored.artifact_dict()["next_confirmation_generation"] == 3


def test_corrupt_confirmation_generation_fails_closed_without_discarding_durable_geometry(tmp_path):
    identity = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY")
    path = tmp_path / "corrupt-ordering.json"
    obstacle = _ledger_obstacle("durable-cell", Vec3(1200, 1000, 0))
    store = StaticObstacleStore(artifact_path=path, minimum_durable_sessions=2)
    store.record(scope=_scope("one"), identity=identity, obstacle=obstacle)
    store.record(scope=_scope("two"), identity=identity, obstacle=obstacle)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["next_confirmation_generation"] = "not-an-integer"
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = StaticObstacleStore(artifact_path=path, minimum_durable_sessions=2)
    assert restored.load_error == "confirmation_ledger_ordering_invalid"
    assert len(restored.durable_obstacles(identity)) == 1
    new_obstacle = _ledger_obstacle("new-cell", Vec3(1600, 1000, 0))
    restored.record(scope=_scope("three"), identity=identity, obstacle=new_obstacle)
    restored.record(scope=_scope("four"), identity=identity, obstacle=new_obstacle)
    assert not any(item.obstacle_id == "new-cell" for item in restored.durable_obstacles(identity))


def test_corrupt_confirmation_ledger_preserves_valid_durable_geometry_but_blocks_promotion(tmp_path):
    identity = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY")
    path = tmp_path / "companion.json"
    obstacle = _ledger_obstacle("durable-cell", Vec3(1200, 1000, 0))
    store = StaticObstacleStore(artifact_path=path, minimum_durable_sessions=2)
    store.record(scope=_scope("one"), identity=identity, obstacle=obstacle)
    store.record(scope=_scope("two"), identity=identity, obstacle=obstacle)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["confirmation_ledger"] = [{"corrupt": True}]
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = StaticObstacleStore(artifact_path=path, minimum_durable_sessions=2)
    assert restored.load_error == "confirmation_ledger_invalid"
    assert len(restored.durable_obstacles(identity)) == 1
    new_obstacle = _ledger_obstacle("new-cell", Vec3(1600, 1000, 0))
    restored.record(scope=_scope("three"), identity=identity, obstacle=new_obstacle)
    restored.record(scope=_scope("four"), identity=identity, obstacle=new_obstacle)
    assert not any(item.obstacle_id == "new-cell" for item in restored.durable_obstacles(identity))


def test_capsule_distance_positions_blocker_and_line_only_is_not_confirmation():
    scope = _scope()
    overlay = StaticObstacleOverlay()
    overlay.bind_scope(scope)
    lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=2)
    line_raw = {
        "navigation": {
            "schema_version": 1,
            "source": "ue4ss_line_trace_multi_height_capsule",
            "base_sequence": 4,
            "directions": {"forward": {"sample_sequence": 4, "hit": True, "clear_distance_m": 1.0}},
        }
    }
    line = lookahead.inspect(line_raw, sequence=4, route_plan_sha256="ROUTE", desired_world_bearing_degrees=0.0, camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True)
    assert line.state == ObstacleEvidenceState.CANDIDATE
    assert line.query_verified is False

    capsule_raw = {
        "navigation": {
            "schema_version": 1,
            "source": "ue4ss_line_trace_multi_height_capsule",
            "base_sequence": 5,
            "directions": {"forward": {
                "sample_sequence": 5,
                "clear_distance_m": 2.5,
                "capsule_probe": {"available": True, "fresh": True, "sample_sequence": 5, "blocking_hit": True, "clear_distance_m": 2.5, "radius_m": 0.42},
            }},
        }
    }
    capsule = lookahead.inspect(capsule_raw, sequence=5, route_plan_sha256="ROUTE", desired_world_bearing_degrees=0.0, camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True)
    result = overlay.learn_from_movement(
        scope=scope,
        route_plan_sha256="ROUTE",
        route=(Vec3(0, 0, 0), Vec3(1000, 0, 0)),
        position_before=Vec3(0, 0, 0),
        position_after=Vec3(0, 0, 0),
        along_route_before_m=0.0,
        along_route_after_m=0.0,
        commanded_bearing_degrees=0.0,
        command_duration_ms=200,
        sequence=5,
        sampled_t=5.0,
        query_status="blocked",
        query_provenance="runtime_capsule_blocked",
        query_verified=capsule.query_verified,
        capsule_radius_m=capsule.capsule_radius_m,
        blocking_distance_m=capsule.clear_distance_m,
        probe_world_bearing_degrees=capsule.probe_world_bearing_degrees,
    )
    assert capsule.query_verified is True
    assert result.obstacle is not None
    assert result.obstacle.center.x == pytest.approx(347.0)


def test_dynamic_actor_evidence_is_not_converted_to_static_blocker():
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    result = overlay.learn_from_movement(
        scope=scope,
        route_plan_sha256="ROUTE",
        route=(Vec3(0, 0, 0), Vec3(1000, 0, 0)),
        position_before=Vec3(0, 0, 0),
        position_after=Vec3(0, 0, 0),
        along_route_before_m=0.0,
        along_route_after_m=0.0,
        commanded_bearing_degrees=0.0,
        command_duration_ms=200,
        sequence=1,
        sampled_t=1.0,
        dynamic_evidence=DynamicObstacleStaticEvidence(
            StaticLearningDynamicState.BLOCKING,
            "current_dynamic_actor_in_intended_movement_envelope",
            source_identity="synthetic",
            base_sequence=1,
            sampled_at_ms=1000,
            blocking_actor_ids=("actor-1",),
        ),
    )
    assert result.reason_code == "dynamic_actor_evidence_not_static"
    assert overlay.snapshot().obstacles == ()


def test_visibility_detour_checks_all_committed_obstacles_when_local_offsets_are_blocked():
    graph = _single_polygon_graph()
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    obstacles = tuple(
        _obstacle(scope, center=Vec3(1500, y, 0))
        for y in (1000, 850, 1150)
    )
    for obstacle in obstacles:
        overlay.admit_obstacle(scope=scope, obstacle=obstacle)

    plan = plan_with_static_obstacles(graph, Vec3(300, 1000, 0), Vec3(2700, 1000, 0), overlay, now_t=2.0)

    assert plan.status == "complete"
    assert plan.reason_code == "route_plan_complete"
    assert plan.diagnostics["static_obstacle_resolution"] == "same_polygon_visibility_detour"
    assert len(plan.waypoints) == 4
    for obstacle in obstacles:
        assert not route_obstacle_hits(plan.waypoints, obstacle)


def _dynamic_snapshot(
    *,
    position: Point3M | None = None,
    complete: bool = False,
    age_seconds: float = 0.0,
    sequence: int = 1,
) -> DynamicObstacleSnapshot:
    observations = ()
    if position is not None:
        relative = position - Point3M(0.0, 0.0, 0.0)
        observations = (
            DynamicObstacleObservation(
                actor_id="actor-1",
                base_sequence=sequence,
                sampled_at_ms=1_000,
                cloud_position_m=Point3M(0.0, 0.0, 0.0),
                actor_position_m=position,
                relative_position_m=relative,
                horizontal_distance_m=relative.horizontal_length(),
                vertical_separation_m=abs(relative.z),
                observation_age_seconds=age_seconds,
                source_identity="typed-test",
            ),
        )
    return DynamicObstacleSnapshot(
        base_sequence=sequence,
        sampled_at_ms=1_000,
        observations=observations,
        source_identity="typed-test",
        complete=complete,
        observed_actor_count=len(observations),
        accepted_actor_count=len(observations),
        rejected_actor_count=0,
        rejection_reason_codes=() if complete else (("producer_completeness_unproven",) if not observations else ("producer_completeness_unproven",)),
    )


def _movement_learning_kwargs(scope: StaticObstacleScope) -> dict[str, object]:
    return {
        "scope": scope,
        "route_plan_sha256": "ROUTE",
        "route": (Vec3(0, 0, 0), Vec3(3000, 0, 0)),
        "position_before": Vec3(0, 0, 0),
        "position_after": Vec3(0, 0, 0),
        "along_route_before_m": 0.0,
        "along_route_after_m": 0.0,
        "commanded_bearing_degrees": 0.0,
        "command_duration_ms": 250,
        "telemetry_fresh": True,
    }


def test_typed_dynamic_blocker_prevents_static_persistence():
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    evidence = assess_static_learning_dynamic_evidence(
        _dynamic_snapshot(position=Point3M(1.0, 0.0, 0.0)),
        cloud_position_m=Point3M(0.0, 0.0, 0.0),
        intended_bearing_degrees=0.0,
    )
    assert evidence.blocks_static_persistence
    result = overlay.learn_from_movement(dynamic_evidence=evidence, sequence=1, sampled_t=1.0, **_movement_learning_kwargs(scope))
    assert result.reason_code == "dynamic_actor_evidence_not_static"
    assert overlay.snapshot().obstacles == ()


def test_complete_actor_snapshot_allows_stronger_static_learning_path():
    overlay = StaticObstacleOverlay(minimum_confirmations=2)
    scope = _scope()
    overlay.bind_scope(scope)
    evidence = assess_static_learning_dynamic_evidence(
        _dynamic_snapshot(complete=True),
        cloud_position_m=Point3M(0.0, 0.0, 0.0),
        intended_bearing_degrees=0.0,
    )
    assert evidence.state == StaticLearningDynamicState.CLEAR_COMPLETE
    first = overlay.learn_from_movement(dynamic_evidence=evidence, sequence=1, sampled_t=1.0, **_movement_learning_kwargs(scope))
    second = overlay.learn_from_movement(dynamic_evidence=evidence, sequence=2, sampled_t=2.0, **_movement_learning_kwargs(scope))
    assert first.state == ObstacleEvidenceState.CANDIDATE
    assert second.state == ObstacleEvidenceState.READY
    assert second.obstacle is not None
    assert "dynamic_evidence_clear_complete" in second.obstacle.provenance


def test_incomplete_stale_and_absent_dynamic_evidence_remain_explicit_but_can_confirm():
    scope = _scope()
    for evidence in (
        assess_static_learning_dynamic_evidence(
            _dynamic_snapshot(), cloud_position_m=Point3M(0.0, 0.0, 0.0), intended_bearing_degrees=0.0,
        ),
        assess_static_learning_dynamic_evidence(
            _dynamic_snapshot(position=Point3M(2.0, 2.0, 0.0), age_seconds=2.0),
            cloud_position_m=Point3M(0.0, 0.0, 0.0), intended_bearing_degrees=0.0,
        ),
        assess_static_learning_dynamic_evidence(
            None, cloud_position_m=Point3M(0.0, 0.0, 0.0), intended_bearing_degrees=0.0,
        ),
    ):
        overlay = StaticObstacleOverlay(minimum_confirmations=2)
        overlay.bind_scope(scope)
        assert evidence.state in {StaticLearningDynamicState.INCOMPLETE, StaticLearningDynamicState.STALE, StaticLearningDynamicState.ABSENT}
        first = overlay.learn_from_movement(dynamic_evidence=evidence, sequence=1, sampled_t=1.0, **_movement_learning_kwargs(scope))
        second = overlay.learn_from_movement(dynamic_evidence=evidence, sequence=2, sampled_t=2.0, **_movement_learning_kwargs(scope))
        third = overlay.learn_from_movement(dynamic_evidence=evidence, sequence=3, sampled_t=3.0, **_movement_learning_kwargs(scope))
        assert first.state == ObstacleEvidenceState.CANDIDATE
        assert second.state == ObstacleEvidenceState.CANDIDATE
        assert third.state == ObstacleEvidenceState.READY


def test_dynamic_actor_disappearing_does_not_leave_durable_static_obstacle(tmp_path):
    identity = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY")
    store = StaticObstacleStore(artifact_path=tmp_path / "companion.json")
    scope = _scope()
    overlay = StaticObstacleOverlay(minimum_confirmations=2, store=store)
    overlay.bind_scope(scope, durable_identity=identity)
    dynamic = assess_static_learning_dynamic_evidence(
        _dynamic_snapshot(position=Point3M(1.0, 0.0, 0.0)), cloud_position_m=Point3M(0.0, 0.0, 0.0), intended_bearing_degrees=0.0,
    )
    assert overlay.learn_from_movement(dynamic_evidence=dynamic, sequence=1, sampled_t=1.0, **_movement_learning_kwargs(scope)).obstacle is None
    clear = assess_static_learning_dynamic_evidence(
        _dynamic_snapshot(complete=True), cloud_position_m=Point3M(0.0, 0.0, 0.0), intended_bearing_degrees=0.0,
    )
    overlay.learn_from_movement(dynamic_evidence=clear, sequence=2, sampled_t=2.0, **_movement_learning_kwargs(scope))
    result = overlay.learn_from_movement(dynamic_evidence=clear, sequence=3, sampled_t=3.0, **_movement_learning_kwargs(scope))
    assert result.obstacle is not None and result.obstacle.persistence == "session"
    assert store.durable_obstacles(identity) == ()


def test_one_trusted_capsule_hit_is_session_local(tmp_path):
    identity = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY")
    store = StaticObstacleStore(artifact_path=tmp_path / "companion.json")
    scope = _scope()
    overlay = StaticObstacleOverlay(store=store)
    overlay.bind_scope(scope, durable_identity=identity)
    result = overlay.learn_from_movement(
        dynamic_evidence=assess_static_learning_dynamic_evidence(None, cloud_position_m=Point3M(0.0, 0.0, 0.0), intended_bearing_degrees=0.0),
        probe_world_bearing_degrees=0.0,
        query_status="blocked",
        query_verified=True,
        capsule_radius_m=0.42,
        blocking_distance_m=2.0,
        query_provenance="runtime_capsule_blocked",
        sequence=1,
        sampled_t=1.0,
        **_movement_learning_kwargs(scope),
    )
    assert result.obstacle is not None and result.obstacle.persistence == "session"
    assert store.durable_obstacles(identity) == ()


def test_offline_static_corroboration_can_promote_with_reason(tmp_path):
    identity = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY")
    store = StaticObstacleStore(artifact_path=tmp_path / "companion.json")
    scope = _scope()
    overlay = StaticObstacleOverlay(store=store)
    overlay.bind_scope(scope, durable_identity=identity)
    evidence = StaticObstaclePromotionEvidence("offline_static", "trusted offline collision corroboration", "asset:tree-01")
    overlay.admit_obstacle(scope=scope, obstacle=_obstacle(scope, center=Vec3(1000, 1000, 0)), promotion_evidence=evidence)
    durable = store.durable_obstacles(identity)
    assert len(durable) == 1
    assert durable[0].promotion_reason == "trusted offline collision corroboration"
    assert "offline_static" in durable[0].promotion_evidence


def test_default_identity_companion_is_separate_and_reloadable(tmp_path):
    root = tmp_path / "static-obstacles"
    identity = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY", content_hashes=("A",))
    other = StaticObstacleDurableIdentity("BUILD", "map", "OTHER", content_hashes=("A",))
    store = StaticObstacleStore(artifact_root=root)
    first = StaticObstacleOverlay(store=store)
    first.bind_scope(_scope("run-1"), durable_identity=identity)
    first.admit_obstacle(
        scope=_scope("run-1"),
        obstacle=_obstacle(_scope("run-1"), center=Vec3(1000, 1000, 0)),
        promotion_evidence=StaticObstaclePromotionEvidence("offline_static", "offline asset", "asset:1"),
    )
    assert (root / f"{identity.key()}.json").is_file()
    restored = StaticObstacleStore(artifact_root=root)
    matching = StaticObstacleOverlay(store=restored)
    matching.bind_scope(_scope("run-2"), durable_identity=identity)
    unrelated = StaticObstacleOverlay(store=restored)
    unrelated.bind_scope(_scope("run-2"), durable_identity=other)
    assert len(matching.snapshot().obstacles) == 1
    assert unrelated.snapshot().obstacles == ()


def test_service_owned_store_shares_session_obstacle_across_adapter_scopes():
    store = StaticObstacleStore()
    scope = _scope("shared")
    first = StaticObstacleOverlay(store=store)
    first.bind_scope(scope)
    first.admit_obstacle(scope=scope, obstacle=_obstacle(scope, center=Vec3(1000, 1000, 0)))
    second = StaticObstacleOverlay(store=store)
    second.bind_scope(scope)
    assert len(second.snapshot().obstacles) == 1
    assert second.snapshot().obstacles[0].persistence == "session"


def test_historical_session_and_confirmation_storage_is_bounded():
    store = StaticObstacleStore(max_session_scopes=2, max_confirmation_records=2)
    identity = StaticObstacleDurableIdentity("BUILD", "map", "TOPOLOGY")
    for index in range(6):
        scope = _scope(f"run-{index}")
        store.record(scope=scope, identity=None, obstacle=_obstacle(scope, center=Vec3(100 + index * 100, 100, 0)))
    assert len(store._sessions) <= 2
    for index in range(6):
        scope = _scope(f"confirm-{index}")
        obstacle = _obstacle(scope, center=Vec3(100 + index * 100, 500, 0))
        store.record(scope=scope, identity=identity, obstacle=obstacle)
    assert len(store._confirmations) <= 2


def test_capsule_hit_uses_selected_probe_bearing_and_preserves_route_intent():
    lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=1)
    raw = {
        "navigation": {
            "schema_version": 1,
            "source": "ue4ss_line_trace_multi_height_capsule",
            "base_sequence": 8,
            "directions": {
                "right": {
                    "sample_sequence": 8,
                    "clear_distance_m": 2.5,
                    "capsule_probe": {
                        "available": True, "fresh": True, "sample_sequence": 8,
                        "blocking_hit": True, "clear_distance_m": 2.5, "radius_m": 0.42,
                    },
                },
            },
        },
    }
    decision = lookahead.inspect(
        raw, sequence=8, route_plan_sha256="probe-route", desired_world_bearing_degrees=80.0,
        camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True,
    )
    assert decision.selected_direction == "right"
    assert decision.probe_world_bearing_degrees == pytest.approx(90.0)
    scope = _scope("probe")
    overlay = StaticObstacleOverlay()
    overlay.bind_scope(scope)
    result = overlay.learn_from_movement(
        scope=scope,
        route_plan_sha256="probe-route",
        route=(Vec3(0, 0, 0), Vec3(1000, 0, 0)),
        position_before=Vec3(0, 0, 0), position_after=Vec3(0, 0, 0),
        along_route_before_m=0.0, along_route_after_m=0.0,
        commanded_bearing_degrees=80.0, probe_world_bearing_degrees=decision.probe_world_bearing_degrees,
        command_duration_ms=200, sequence=8, sampled_t=8.0,
        query_status="blocked", query_provenance="runtime_capsule_blocked", query_verified=True,
        capsule_radius_m=decision.capsule_radius_m,
        blocking_distance_m=decision.clear_distance_m,
    )
    assert result.obstacle is not None
    assert result.obstacle.center.x == pytest.approx(0.0)
    assert result.obstacle.center.y == pytest.approx(347.0)
    assert result.obstacle.route_bearing_degrees == pytest.approx(80.0)
    assert result.obstacle.probe_bearing_degrees == pytest.approx(90.0)


def test_runtime_contacts_merge_into_bounded_aligned_capsule_barrier():
    overlay = StaticObstacleOverlay(minimum_confirmations=2)
    scope = _scope("fence")
    overlay.bind_scope(scope)
    results = []
    sequence = 1
    for along_m in (8.0, 10.0, 11.5):
        for _ in range(3):
            results.append(overlay.learn_from_movement(
                scope=scope,
                route_plan_sha256="fence-route",
                route=(Vec3(0, 0, 0), Vec3(3000, 0, 0)),
                position_before=Vec3(along_m * 100.0, 0, 0), position_after=Vec3(along_m * 100.0, 0, 0),
                along_route_before_m=along_m, along_route_after_m=along_m,
                commanded_bearing_degrees=0.0, command_duration_ms=250,
                sequence=sequence, sampled_t=sequence,
            ))
            sequence += 1
    barriers = [item for item in overlay.snapshot().obstacles if item.shape == "capsule"]
    assert barriers
    assert barriers[0].half_length_m >= 1.5
    assert "runtime_barrier_merge" in barriers[0].provenance


def test_nearby_unaligned_runtime_contacts_do_not_fake_a_wall():
    overlay = StaticObstacleOverlay(minimum_confirmations=2)
    scope = _scope("trees")
    overlay.bind_scope(scope)
    sequence = 1
    for along_m, lateral_m in ((8.0, 0.0), (10.0, 0.8), (11.5, -0.8)):
        for _ in range(3):
            overlay.learn_from_movement(
                scope=scope,
                route_plan_sha256="trees-route",
                route=(Vec3(0, lateral_m * 100.0, 0), Vec3(3000, lateral_m * 100.0, 0)),
                position_before=Vec3(along_m * 100.0, lateral_m * 100.0, 0),
                position_after=Vec3(along_m * 100.0, lateral_m * 100.0, 0),
                along_route_before_m=along_m, along_route_after_m=along_m,
                commanded_bearing_degrees=0.0, command_duration_ms=250,
                sequence=sequence, sampled_t=sequence,
            )
            sequence += 1
    assert not any(item.shape == "capsule" for item in overlay.snapshot().obstacles)


def test_convex_corridor_validates_segment_clearance_not_only_route_endpoints():
    graph = _single_polygon_graph()
    polygon = next(iter(graph.polygons.values()))
    first, second = Vec3(300, 300, 0), Vec3(2700, 300, 0)
    assert _point_in_convex_polygon(first, polygon)
    assert _point_in_convex_polygon(second, polygon)
    assert not _segment_authorized_corridor(
        first,
        second,
        graph=graph,
        corridor_refs=(polygon.reference.value,),
        portals=(),
        minimum_edge_clearance_m=4.0,
        max_vertical_separation_m=20.0,
    )


def test_corner_edge_blocker_has_no_unproven_local_escape():
    narrow = _polygon(
        1,
        (Vec3(0, 0, 0), Vec3(3000, 0, 0), Vec3(3000, 200, 0), Vec3(0, 200, 0)),
    )
    graph = DetourTopologyGraph({narrow.reference.value: narrow}, {}, snapshot_diagnostics={})
    overlay = StaticObstacleOverlay()
    scope = _scope("narrow")
    overlay.bind_scope(scope)
    overlay.admit_obstacle(scope=scope, obstacle=_obstacle(scope, center=Vec3(1500, 100, 0)))
    plan = plan_with_static_obstacles(graph, Vec3(300, 100, 0), Vec3(2700, 100, 0), overlay, now_t=2.0)
    assert plan.status == "failed"
    assert plan.reason_code == "static_obstacle_local_detour_unproven"


def test_oversized_obstacle_has_no_proven_local_bypass():
    graph = _single_polygon_graph()
    scope = _scope("oversized")
    obstacle = StaticObstacle(
        obstacle_id="oversized-fixture",
        center=Vec3(1500, 1000, 0),
        radius_m=10.0,
        cloud_clearance_m=0.65,
        uncertainty_margin_m=0.25,
        shape="circle",
        half_length_m=0.0,
        heading_degrees=0.0,
        confidence=0.99,
        evidence_count=3,
        first_sequence=1,
        last_sequence=3,
        first_t=1.0,
        last_t=3.0,
        provenance=("ambiguous_geometry_bound",),
    )
    overlay = StaticObstacleOverlay()
    overlay.bind_scope(scope)
    overlay.admit_obstacle(scope=scope, obstacle=obstacle)

    plan = plan_with_static_obstacles(
        graph,
        Vec3(300, 1000, 0),
        Vec3(2700, 1000, 0),
        overlay,
        now_t=2.0,
    )

    assert plan.status == "failed"
    assert plan.reason_code == "static_obstacle_local_detour_unproven"


def test_multi_polygon_detour_does_not_accept_a_segment_outside_authorized_corridor():
    graph, start_ref, end_ref = _corridor_graph()
    overlay = StaticObstacleOverlay()
    scope = _scope("outside")
    overlay.bind_scope(scope)
    overlay.admit_obstacle(scope=scope, obstacle=_obstacle(scope, center=Vec3(1500, 500, 0)))
    plan = plan_with_static_obstacles(graph, Vec3(500, 500, 0), Vec3(2500, 500, 0), overlay, now_t=2.0)
    assert plan.status == "complete"
    assert plan.polygon_corridor[0] == start_ref
    assert plan.polygon_corridor[-1] == end_ref
    assert all(
        any(_point_in_convex_polygon(point, graph.polygons[reference]) for reference in plan.polygon_corridor)
        for point in plan.waypoints
    )


def _actual_clipped_portal_fixture(bmin: int = 64, bmax: int = 192):
    source = _polygon(
        10,
        (Vec3(0, 0, 0), Vec3(1000, 0, 0), Vec3(1000, 1000, 0), Vec3(0, 1000, 0)),
    )
    target = _polygon(
        11,
        (Vec3(1000, 0, 0), Vec3(2000, 0, 0), Vec3(2000, 1000, 0), Vec3(1000, 1000, 0)),
    )
    link = DetourLink(
        reference=target.reference.value,
        edge=1,
        next_index=0xFFFFFFFF,
        side=2,
        bmin=bmin,
        bmax=bmax,
    )
    portal = _portal_from_link(source, target, link, tolerance_cm=1.0)
    assert portal is not None
    graph = DetourTopologyGraph(
        {source.reference.value: source, target.reference.value: target},
        {source.reference.value: (portal,), target.reference.value: ()},
        snapshot_diagnostics={},
    )
    return graph, source.reference.value, target.reference.value, portal


def _offset_target_clipped_portal_parts():
    source = _polygon(
        20,
        (Vec3(0, 0, 0), Vec3(1000, 0, 0), Vec3(1000, 1000, 0), Vec3(0, 1000, 0)),
    )
    # The source-link lower endpoint is about Y=250.98 cm.  Starting the
    # target edge at Y=254 cm makes that endpoint genuinely ~3 cm outside
    # the finite target segment while remaining inside the canonical 5 cm
    # Detour portal-association tolerance.
    target = _polygon(
        21,
        (Vec3(1000, 254, 0), Vec3(2000, 254, 0), Vec3(2000, 757, 0), Vec3(1000, 757, 0)),
    )
    link = DetourLink(
        reference=target.reference.value,
        edge=1,
        next_index=0xFFFFFFFF,
        side=2,
        bmin=64,
        bmax=192,
    )
    return source, target, link


def _offset_target_clipped_portal_fixture():
    """Build a canonical clipped portal whose target edge is only approximately coincident."""

    source, target, link = _offset_target_clipped_portal_parts()
    portal = _portal_from_link(source, target, link, tolerance_cm=5.0)
    assert portal is not None
    assert portal.source_edge == 1
    assert portal.target_edge == 3
    graph = DetourTopologyGraph(
        {source.reference.value: source, target.reference.value: target},
        {source.reference.value: (portal,), target.reference.value: ()},
        snapshot_diagnostics={},
        portal_tolerance_cm=5.0,
    )
    return graph, source.reference.value, target.reference.value, portal


def test_actual_detour_full_edge_portal_is_authorized():
    graph, source_ref, target_ref, portal = _actual_clipped_portal_fixture(0, 255)
    assert _segment_authorized_corridor(
        Vec3(500, 500, 0), Vec3(1500, 500, 0),
        graph=graph, corridor_refs=(source_ref, target_ref), portals=(portal,),
        minimum_edge_clearance_m=0.10, max_vertical_separation_m=20.0,
    )


def test_actual_detour_clipped_middle_portal_is_authorized_only_inside_interval():
    graph, source_ref, target_ref, portal = _actual_clipped_portal_fixture()
    assert _segment_authorized_corridor(
        Vec3(500, 500, 0), Vec3(1500, 500, 0),
        graph=graph, corridor_refs=(source_ref, target_ref), portals=(portal,),
        minimum_edge_clearance_m=0.10, max_vertical_separation_m=20.0,
    )
    assert not _segment_authorized_corridor(
        Vec3(500, 100, 0), Vec3(1500, 100, 0),
        graph=graph, corridor_refs=(source_ref, target_ref), portals=(portal,),
        minimum_edge_clearance_m=0.10, max_vertical_separation_m=20.0,
    )


def test_actual_detour_wrong_directed_clipped_portal_is_rejected():
    graph, source_ref, target_ref, portal = _actual_clipped_portal_fixture()
    wrong_direction = DetourPortal(
        target_ref, source_ref, portal.link_reference, portal.source_edge, portal.target_edge,
        portal.left, portal.right, portal.side,
    )
    assert not _segment_authorized_corridor(
        Vec3(500, 500, 0), Vec3(1500, 500, 0),
        graph=graph, corridor_refs=(source_ref, target_ref), portals=(wrong_direction,),
        minimum_edge_clearance_m=0.10, max_vertical_separation_m=20.0,
    )


def test_adjacent_clipped_portal_cannot_authorize_the_expected_transition():
    graph, source_ref, target_ref, portal = _actual_clipped_portal_fixture()
    _, _, _, adjacent = _actual_clipped_portal_fixture(0, 32)
    adjacent = DetourPortal(
        source_ref, target_ref + 999, adjacent.link_reference, adjacent.source_edge,
        adjacent.target_edge, adjacent.left, adjacent.right, adjacent.side,
    )
    assert not _segment_authorized_corridor(
        Vec3(500, 100, 0), Vec3(1500, 100, 0),
        graph=graph, corridor_refs=(source_ref, target_ref), portals=(portal, adjacent),
        minimum_edge_clearance_m=0.10, max_vertical_separation_m=20.0,
    )


def test_clipped_portal_crossing_too_close_to_lower_closed_edge_is_rejected():
    graph, source_ref, target_ref, portal = _actual_clipped_portal_fixture()
    assert not _segment_authorized_corridor(
        Vec3(500, 300, 0), Vec3(1500, 240, 0),
        graph=graph, corridor_refs=(source_ref, target_ref), portals=(portal,),
        minimum_edge_clearance_m=0.50, max_vertical_separation_m=20.0,
    )


def test_clipped_portal_crossing_too_close_to_upper_closed_edge_is_rejected():
    graph, source_ref, target_ref, portal = _actual_clipped_portal_fixture()
    assert not _segment_authorized_corridor(
        Vec3(500, 700, 0), Vec3(1500, 760, 0),
        graph=graph, corridor_refs=(source_ref, target_ref), portals=(portal,),
        minimum_edge_clearance_m=0.50, max_vertical_separation_m=20.0,
    )


def test_portal_touching_edge_start_or_end_retains_clearance_on_remaining_edge():
    start_graph, start_ref, start_target_ref, start_portal = _actual_clipped_portal_fixture(0, 128)
    assert _segment_authorized_corridor(
        Vec3(500, 250, 0), Vec3(1500, 250, 0),
        graph=start_graph, corridor_refs=(start_ref, start_target_ref), portals=(start_portal,),
        minimum_edge_clearance_m=0.50, max_vertical_separation_m=20.0,
    )
    assert not _segment_authorized_corridor(
        Vec3(500, 450, 0), Vec3(1500, 490, 0),
        graph=start_graph, corridor_refs=(start_ref, start_target_ref), portals=(start_portal,),
        minimum_edge_clearance_m=0.50, max_vertical_separation_m=20.0,
    )

    end_graph, end_ref, end_target_ref, end_portal = _actual_clipped_portal_fixture(127, 255)
    assert not _segment_authorized_corridor(
        Vec3(500, 550, 0), Vec3(1500, 510, 0),
        graph=end_graph, corridor_refs=(end_ref, end_target_ref), portals=(end_portal,),
        minimum_edge_clearance_m=0.50, max_vertical_separation_m=20.0,
    )


def test_canonical_offset_target_edge_uses_graph_tolerance_and_exact_portal_interval():
    graph, source_ref, target_ref, portal = _offset_target_clipped_portal_fixture()
    assert _segment_authorized_corridor(
        Vec3(500, 500, 0), Vec3(1500, 500, 0),
        graph=graph, corridor_refs=(source_ref, target_ref), portals=(portal,),
        minimum_edge_clearance_m=0.10, max_vertical_separation_m=20.0,
    )
    assert not _segment_authorized_corridor(
        Vec3(500, 100, 0), Vec3(1500, 100, 0),
        graph=graph, corridor_refs=(source_ref, target_ref), portals=(portal,),
        minimum_edge_clearance_m=0.10, max_vertical_separation_m=20.0,
    )


def test_canonical_offset_target_edge_keeps_closed_portion_clearance():
    graph, source_ref, target_ref, portal = _offset_target_clipped_portal_fixture()
    # The crossing is inside left/right, but roughly 4 cm from the lower
    # closed portion of the canonical source/target portal edges.
    assert not _segment_authorized_corridor(
        Vec3(500, 255, 0), Vec3(1500, 255, 0),
        graph=graph, corridor_refs=(source_ref, target_ref), portals=(portal,),
        minimum_edge_clearance_m=0.05, max_vertical_separation_m=20.0,
    )
    assert not _segment_authorized_corridor(
        Vec3(500, 752, 0), Vec3(1500, 752, 0),
        graph=graph, corridor_refs=(source_ref, target_ref), portals=(portal,),
        minimum_edge_clearance_m=0.05, max_vertical_separation_m=20.0,
    )


def test_offset_target_portal_requires_canonical_five_centimeter_tolerance():
    source, target, link = _offset_target_clipped_portal_parts()
    tight_portal = _portal_from_link(source, target, link, tolerance_cm=1.0)
    assert tight_portal is None or tight_portal.target_edge < 0

    portal = _portal_from_link(source, target, link, tolerance_cm=5.0)
    assert portal is not None
    assert portal.source_edge == link.edge
    assert portal.target_edge == 3
    target_edge_first, target_edge_second = target.edge(portal.target_edge)
    endpoint_distances_cm = tuple(
        100.0 * _distance_point_to_segment_m(endpoint, target_edge_first, target_edge_second)[0]
        for endpoint in (portal.left, portal.right)
    )
    assert any(1.0 < distance < 5.0 for distance in endpoint_distances_cm)


def test_canonical_missing_target_edge_fails_closed():
    graph, source_ref, target_ref, portal = _offset_target_clipped_portal_fixture()
    invalid_target_edge = DetourPortal(
        portal.from_reference,
        portal.to_reference,
        portal.link_reference,
        portal.source_edge,
        -1,
        portal.left,
        portal.right,
        portal.side,
    )
    assert not _segment_authorized_corridor(
        Vec3(500, 500, 0), Vec3(1500, 500, 0),
        graph=graph, corridor_refs=(source_ref, target_ref), portals=(invalid_target_edge,),
        minimum_edge_clearance_m=0.10, max_vertical_separation_m=20.0,
    )


def test_canonical_missing_source_edge_fails_closed():
    graph, source_ref, target_ref, portal = _offset_target_clipped_portal_fixture()
    invalid_source_edge = DetourPortal(
        portal.from_reference,
        portal.to_reference,
        portal.link_reference,
        -1,
        portal.target_edge,
        portal.left,
        portal.right,
        portal.side,
    )
    assert not _segment_authorized_corridor(
        Vec3(500, 500, 0), Vec3(1500, 500, 0),
        graph=graph, corridor_refs=(source_ref, target_ref), portals=(invalid_source_edge,),
        minimum_edge_clearance_m=0.10, max_vertical_separation_m=20.0,
    )


def test_portal_endpoint_local_detour_rejoins_inside_authorized_corridor():
    graph = _portal_endpoint_detour_graph()
    start = Vec3(500, 300, 0)
    target = Vec3(1500, 1700, 0)
    base = graph.plan(start, target)
    assert base.status == "complete"
    assert base.waypoints == (start, base.portals[1].left, target)
    assert route_obstacle_hits(
        base.waypoints,
        _obstacle(_scope("portal-endpoint"), center=Vec3(750, 550, 0)),
    ) == (0,)

    scope = _scope("portal-endpoint")
    overlay = StaticObstacleOverlay()
    overlay.bind_scope(scope)
    overlay.admit_obstacle(
        scope=scope,
        obstacle=_obstacle(scope, center=Vec3(750, 550, 0)),
    )

    plan = plan_with_static_obstacles(graph, start, target, overlay, now_t=2.0)
    # Before the bounded rejoin fallback, this production-shaped geometry
    # returned static_obstacle_local_detour_unproven because the funnel corner
    # made the fixed three-segment candidate unprovable.
    assert plan.status == "complete"
    assert plan.reason_code == "route_plan_complete"
    assert plan.diagnostics["static_obstacle_resolution"] == "corridor_local_detour"
    assert plan.polygon_corridor == base.polygon_corridor
    assert plan.portals == base.portals
    assert plan.waypoints[0] == start
    assert plan.waypoints[-1] == target
    assert route_obstacle_hits(plan.waypoints, overlay.snapshot().obstacles[0]) == ()
    assert plan.validation["static_obstacle_overlay_validated"] is True
    assert plan.validation["movement_sent"] is False


def test_post_corner_local_detour_departure_keeps_base_subsegment_and_proves_new_links():
    graph = _post_corner_detour_graph()
    start = Vec3(-33.519531, -773.171875, 397.324707)
    target = Vec3(700, 1100, 400)
    scope = _scope("post-corner")
    obstacle = StaticObstacle(
        obstacle_id="route17-fixture",
        center=Vec3(-14.009757, -421.781666, 397.324707),
        radius_m=0.55,
        cloud_clearance_m=0.65,
        uncertainty_margin_m=0.1,
        shape="circle",
        half_length_m=0.0,
        heading_degrees=0.0,
        confidence=0.96,
        evidence_count=1,
        first_sequence=1,
        last_sequence=1,
        first_t=1.0,
        last_t=1.0,
        provenance=("runtime_capsule_blocked",),
    )
    base = graph.plan(start, target)
    assert base.status == "complete"
    assert route_obstacle_hits(base.waypoints, obstacle)
    # Correct funnel tightening removes the historical unnecessary corners.

    overlay = StaticObstacleOverlay()
    overlay.bind_scope(scope)
    overlay.admit_obstacle(scope=scope, obstacle=obstacle)

    plan = plan_with_static_obstacles(graph, start, target, overlay, now_t=2.0)
    assert plan.status == "complete"
    assert plan.diagnostics["static_obstacle_resolution"] == "corridor_local_detour"
    assert plan.polygon_corridor == base.polygon_corridor
    assert plan.portals == base.portals
    assert plan.waypoints[0] == base.waypoints[0]
    assert plan.waypoints[-1] == base.waypoints[-1]
    assert plan.waypoints[1] != base.waypoints[1]
    assert plan.waypoints != base.waypoints
    departure_distance_m, departure_fraction = _distance_point_to_segment_m(
        plan.waypoints[1],
        base.waypoints[0],
        base.waypoints[1],
    )
    assert departure_distance_m == pytest.approx(0.0)
    assert 0.0 < departure_fraction < 1.0
    assert all(
        _segment_authorized_corridor(
            first,
            second,
            graph=graph,
            corridor_refs=base.polygon_corridor,
            portals=base.portals[1:-1],
            minimum_edge_clearance_m=0.2,
            max_vertical_separation_m=20.0,
        )
        for first, second in zip(plan.waypoints[1:4], plan.waypoints[2:5])
    )
    assert route_obstacle_hits(plan.waypoints, obstacle) == ()
    assert plan.total_estimated_path_length_m == pytest.approx(sum(
        _distance_m(first, second)
        for first, second in zip(plan.waypoints, plan.waypoints[1:])
    ))
    assert plan.validation["static_obstacle_overlay_validated"] is True
    assert plan.validation["movement_sent"] is False


def test_route27_capsule_geometry_keeps_start_outside_overlay_and_replans():
    """Reproduce route-27 with the supplied world pose and capsule sample."""

    from dataclasses import replace
    import math

    start = Vec3(-26349.101562, -226584.09375, 5496.115723)
    learned_center = Vec3(-26362.9380567, -226548.2640752, 5488.355957)
    blocking_distance_m = 0.703128
    capsule_radius_m = 0.42
    route_plan_sha256 = "c3400db019bfb811d92bde02178c56ecfa471a6681eef9309a7f8009fa822d9"
    topology_fingerprint = "D0FA31646CA0DB82A346A924DB346AB8CE02FAE7EC383AD6C456AC80654A566E8"
    bearing_degrees = math.degrees(math.atan2(
        learned_center.y - start.y,
        learned_center.x - start.x,
    ))
    radians = math.radians(bearing_degrees)
    cosine, sine = math.cos(radians), math.sin(radians)

    source_graph, start_ref, end_ref = _corridor_graph()

    def transform(point: Vec3) -> Vec3:
        # Scale the longitudinal axis so the first directed portal is one
        # metre from the production-shaped start, then rotate it to the
        # observed start-to-obstacle bearing.
        longitudinal_cm = point.x * 0.2 - 100.0
        lateral_cm = point.y - 500.0
        return Vec3(
            start.x + longitudinal_cm * cosine - lateral_cm * sine,
            start.y + longitudinal_cm * sine + lateral_cm * cosine,
            start.z,
        )

    graph = DetourTopologyGraph(
        {
            reference: replace(
                polygon,
                vertices=tuple(transform(vertex) for vertex in polygon.vertices),
            )
            for reference, polygon in source_graph.polygons.items()
        },
        {
            reference: tuple(
                replace(portal, left=transform(portal.left), right=transform(portal.right))
                for portal in portals
            )
            for reference, portals in source_graph.adjacency.items()
        },
        snapshot_diagnostics={},
    )
    target = Vec3(
        start.x + 400.0 * cosine,
        start.y + 400.0 * sine,
        start.z,
    )
    base = graph.plan(start, target)
    assert base.status == "complete"
    middle_ref = next(reference for reference in graph.polygons if reference not in {start_ref, end_ref})
    assert base.polygon_corridor == (start_ref, middle_ref, end_ref)

    lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=1)
    raw = {
        "navigation": {
            "schema_version": 1,
            "source": "ue4ss_line_trace_multi_height_capsule",
            "base_sequence": 10534,
            "capsule_radius_m": capsule_radius_m,
            "directions": {
                "forward": {
                    "sample_sequence": 10534,
                    "clear_distance_m": blocking_distance_m,
                    "capsule_probe": {
                        "available": True,
                        "fresh": True,
                        "sample_sequence": 10534,
                        "blocking_hit": True,
                        "clear_distance_m": blocking_distance_m,
                        "radius_m": capsule_radius_m,
                    },
                },
            },
        },
    }
    decision = lookahead.inspect(
        raw,
        sequence=10534,
        route_plan_sha256=route_plan_sha256,
        desired_world_bearing_degrees=bearing_degrees,
        camera_world_yaw_degrees=bearing_degrees,
        horizon_m=3.0,
        force=True,
    )
    assert decision.state == ObstacleEvidenceState.READY
    assert decision.reason_code == "lookahead_capsule_blocked"
    assert decision.capsule_radius_m == pytest.approx(capsule_radius_m)

    scope = StaticObstacleScope(
        "route-27-session",
        "EndGame",
        "EndGame",
        topology_fingerprint,
    )
    overlay = StaticObstacleOverlay()
    overlay.bind_scope(scope)
    position_before = Vec3(
        learned_center.x - blocking_distance_m * 100.0 * cosine,
        learned_center.y - blocking_distance_m * 100.0 * sine,
        start.z,
    )
    learned = overlay.learn_from_movement(
        scope=scope,
        route_plan_sha256=route_plan_sha256,
        route=base.waypoints,
        position_before=position_before,
        position_after=position_before,
        along_route_before_m=0.0,
        along_route_after_m=0.0,
        commanded_bearing_degrees=bearing_degrees,
        command_duration_ms=250,
        sequence=10534,
        sampled_t=10534.0,
        query_status="blocked",
        query_provenance="precommand_capsule",
        query_verified=decision.query_verified,
        blocking_distance_m=decision.clear_distance_m,
        probe_world_bearing_degrees=decision.probe_world_bearing_degrees,
        capsule_radius_m=decision.capsule_radius_m,
    )
    assert learned.state == ObstacleEvidenceState.READY
    assert learned.obstacle is not None
    obstacle = learned.obstacle
    expected_center_distance_m = blocking_distance_m + capsule_radius_m + overlay.probable_radius_m
    assert math.hypot(
        (obstacle.center.x - position_before.x) / 100.0,
        (obstacle.center.y - position_before.y) / 100.0,
    ) == pytest.approx(expected_center_distance_m)
    assert math.hypot(
        (start.x - obstacle.center.x) / 100.0,
        (start.y - obstacle.center.y) / 100.0,
    ) > obstacle.effective_radius_m

    plan = plan_with_static_obstacles(graph, start, target, overlay, now_t=10535.0)
    alternate_ref = next(reference for reference in graph.polygons if reference not in {start_ref, end_ref, middle_ref})
    assert plan.status == "complete"
    assert plan.polygon_corridor == (start_ref, alternate_ref, end_ref)
    assert plan.polygon_corridor != base.polygon_corridor
    assert plan.diagnostics["static_obstacle_resolution"] == "global_replan"
    assert not route_obstacle_hits(plan.waypoints, obstacle)

def test_blocking_capsule_without_radius_fails_closed():
    lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=1)
    raw = _capsule_sidecar(1)
    raw["navigation"]["directions"]["forward"]["capsule_probe"].pop("radius_m")
    decision = lookahead.inspect(
        raw, sequence=1, route_plan_sha256="missing-radius", desired_world_bearing_degrees=0.0,
        camera_world_yaw_degrees=0.0, horizon_m=3.0, force=True,
    )
    assert decision.state == ObstacleEvidenceState.UNKNOWN
    assert decision.reason_code == "lookahead_capsule_geometry_invalid"


def test_persisted_route_local_detour_scopes_vertical_check_to_touched_intervals():
    """The retained live obstacle route must not inherit the goal's elevation."""
    start = Vec3(-26006.6875, -227162.203125, 5424.128906)
    target = Vec3(-33374.386719, -211948.03125, 3996.835205)
    obstacle = StaticObstacle(
        obstacle_id="static-fe5a4b26af6972dd",
        center=Vec3(-26135.092918412094, -227008.86673492115, 5368.222945007819),
        radius_m=0.55,
        cloud_clearance_m=0.65,
        uncertainty_margin_m=0.25,
        confidence=0.99,
        evidence_count=3,
        first_sequence=815,
        last_sequence=820,
        first_t=87817.3164534,
        last_t=87818.6722507,
        provenance=("dynamic_evidence_incomplete", "movement_no_progress"),
        persistence="durable",
    )
    local_ref = PolygonReference.make(1, 98, 0).value
    far_ref = PolygonReference.make(1, 99, 0).value
    local_z = start.z
    local = _polygon(
        98,
        (
            Vec3(-26500, -227600, local_z),
            Vec3(-25700, -227600, local_z),
            Vec3(-25700, -226600, local_z),
            Vec3(-26500, -226600, local_z),
        ),
    )
    far = _polygon(
        99,
        (
            Vec3(-35000, -212500, 3912),
            Vec3(-32500, -212500, 3912),
            Vec3(-32500, -210500, 3912),
            Vec3(-35000, -210500, 3912),
        ),
    )
    graph = DetourTopologyGraph(
        {local_ref: local, far_ref: far},
        {},
        snapshot_diagnostics={},
    )
    next_point = Vec3(
        start.x + 2.0 * (obstacle.center.x - start.x),
        start.y + 2.0 * (obstacle.center.y - start.y),
        local_z,
    )
    seed = _post_corner_detour_graph().plan(
        Vec3(-33.519531, -773.171875, 397.324707),
        Vec3(700, 1100, 400),
    )
    base = replace(
        seed,
        requested_start=start,
        requested_end=target,
        resolved_start=start,
        resolved_end=target,
        start_ref=local_ref,
        end_ref=far_ref,
        polygon_corridor=(local_ref, far_ref),
        portals=(),
        waypoints=(start, next_point, target),
        total_estimated_path_length_m=None,
    )

    class PersistedRouteGraph:
        portal_tolerance_cm = graph.portal_tolerance_cm
        polygons = graph.polygons

        def plan(self, *args, **kwargs):
            return base

    hits = route_obstacle_hits(base.waypoints, obstacle)
    assert hits == (0,)
    assert obstacle.effective_radius_m == pytest.approx(1.45)
    assert any(abs(vertex.z - base.waypoints[0].z) / 100.0 > 1.0 for vertex in far.vertices)
    overlay = StaticObstacleOverlay()
    scope = _scope("run-20260827T220907Z-3d5f6bea")
    overlay.bind_scope(scope)
    overlay.admit_obstacle(scope=scope, obstacle=obstacle)

    plan = plan_with_static_obstacles(
        PersistedRouteGraph(),
        start,
        target,
        overlay,
        now_t=87820.0,
        max_snap_distance_m=0.75,
        max_vertical_separation_m=1.0,
        max_waypoints=256,
        minimum_edge_clearance_m=0.2,
    )

    assert plan.status == "complete"
    assert plan.diagnostics["static_obstacle_resolution"] == "corridor_local_detour"
    assert route_obstacle_hits(plan.waypoints, obstacle) == ()


def test_touched_surface_and_endpoint_vertical_bounds_remain_strict():
    high_surface = _polygon(
        100,
        (
            Vec3(0, 0, 250),
            Vec3(3000, 0, 250),
            Vec3(3000, 2000, 250),
            Vec3(0, 2000, 250),
        ),
    )
    graph = DetourTopologyGraph(
        {high_surface.reference.value: high_surface},
        {},
        snapshot_diagnostics={},
    )
    assert not _segment_authorized_corridor(
        Vec3(300, 1000, 0),
        Vec3(2700, 1000, 0),
        graph=graph,
        corridor_refs=(high_surface.reference.value,),
        portals=(),
        minimum_edge_clearance_m=0.2,
        max_vertical_separation_m=1.0,
    )
    assert not _segment_authorized_corridor(
        Vec3(300, 1000, 0),
        Vec3(2700, 1000, 150),
        graph=graph,
        corridor_refs=(high_surface.reference.value,),
        portals=(),
        minimum_edge_clearance_m=0.2,
        max_vertical_separation_m=1.0,
    )


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("side", [-1, 1])
def test_off_axis_obstacle_has_small_proven_bypass_in_narrow_corridor(angle, side):
    import math
    def point(x, y):
        y *= side
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        return Vec3(1234+x*c-y*s, 5678+x*s+y*c, 0)
    poly = _polygon(99, tuple(point(x,y) for x,y in ((0,-90),(2000,-90),(2000,180),(0,180))))
    graph = DetourTopologyGraph({poly.reference.value: poly}, {}, snapshot_diagnostics={})
    scope = _scope();overlay=StaticObstacleOverlay();overlay.bind_scope(scope)
    obstacle = _obstacle(scope, center=point(1000, 100))
    overlay.admit_obstacle(scope=scope, obstacle=obstacle)
    start,end = point(300,0), point(1700,0)
    assert route_obstacle_hits((start,end), obstacle)
    plan = plan_with_static_obstacles(graph,start,end,overlay,now_t=2)
    assert plan.status == "complete", plan.reason_code
    assert not route_obstacle_hits(plan.waypoints,obstacle)
    assert all(_segment_authorized_corridor(a,b,graph=graph,corridor_refs=plan.polygon_corridor,
        portals=(),minimum_edge_clearance_m=.15,max_vertical_separation_m=1)
        for a,b in zip(plan.waypoints,plan.waypoints[1:]))


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("mirror", [1, -1])
def test_recorded_fence_requires_partial_portal_and_proven_contour(angle, mirror):
    from pathlib import Path
    import dataclasses
    import math
    data = json.loads((Path(__file__).parent / "fixtures/observed-fence-corridors.json").read_text())
    origin = Vec3(**data["start"])
    cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    def transform(raw):
        point = Vec3(**raw)
        x, y = point.x - origin.x, (point.y - origin.y) * mirror
        return Vec3(1234 + x*cosine - y*sine, 5678 + x*sine + y*cosine, point.z)
    polygons = {}
    for raw in data["polygons"]:
        values = dict(raw)
        values["reference"] = PolygonReference.from_value(raw["reference"])
        values["vertices"] = tuple(transform(v) for v in raw["vertices"])
        values["links"] = tuple(DetourLink(**v) for v in raw["links"])
        polygons[raw["reference"]] = DetourPolygon(**values)
    adjacency = {}
    for raw in data["portals"]:
        left, right = transform(raw["left"]), transform(raw["right"])
        if mirror < 0: left, right = right, left
        portal = DetourPortal(raw["from_ref"], raw["to_ref"], raw["link_ref"],
            raw["source_edge"], raw["target_edge"], left, right, raw["side"])
        adjacency.setdefault(portal.from_reference, []).append(portal)
    graph = DetourTopologyGraph(polygons, adjacency, snapshot_diagnostics={},
                               portal_tolerance_cm=data["portal_tolerance_cm"])
    original_polygons, original_portals = dict(graph.polygons), dict(graph.adjacency)
    overlay = StaticObstacleOverlay(); scope = _scope(); overlay.bind_scope(scope)
    obstacles = []
    for raw in data["obstacles"]:
        values = {f.name:raw[f.name] for f in dataclasses.fields(StaticObstacle) if f.name in raw}
        values["center"] = transform(raw["center"])
        item = StaticObstacle(**values); obstacles.append(item)
        overlay.admit_obstacle(scope=scope, obstacle=item)
    start, end = transform(data["start"]), transform(data["end"])
    plan = plan_with_static_obstacles(graph, start, end, overlay, now_t=326627,
        max_snap_distance_m=.5, max_vertical_separation_m=1.5, max_waypoints=512,
        portal_corner_inset_m=.5)
    assert plan.status == "complete", plan.reason_code
    assert plan.diagnostics["static_obstacle_resolution"] == "portal_contour_detour"
    search = plan.diagnostics["contour_search"]
    assert search["nodes"] <= 128 and search["connector_checks"] <= 8192
    first, count = search["first_replaced_waypoint"], search["connector_count"]
    assert count > 1
    assert all(_segment_authorized_corridor(a,b,graph=graph,corridor_refs=plan.polygon_corridor,
        portals=plan.portals[1:-1],minimum_edge_clearance_m=.2,max_vertical_separation_m=1.5)
        for a,b in zip(plan.waypoints[first:first+count],plan.waypoints[first+1:first+count+1]))
    assert all(not route_obstacle_hits(plan.waypoints,item) for item in obstacles)
    assert graph.polygons == original_polygons and graph.adjacency == original_portals
    assert all(portal in graph.adjacency[portal.from_reference] or
        replace(portal, left=portal.right, right=portal.left) in graph.adjacency[portal.from_reference]
        for portal in plan.portals[1:-1])


def test_portal_subtraction_preserves_unrelated_narrow_edges_and_bounds_union():
    from ff7r_agent.static_obstacles import _free_circle_portal_intervals
    scope = _scope()
    obstacle = _obstacle(scope, center=Vec3(0,0,0))
    portal = _portal(1,2,Vec3(-300,0,0),Vec3(300,0,0))
    intervals = _free_circle_portal_intervals(portal,(obstacle,))
    assert len(intervals) == 2 and intervals[0][0] == 0 and intervals[1][1] == 1
    assert intervals[0][1] < intervals[1][0]
    assert _free_circle_portal_intervals(replace(portal,left=Vec3(900,0,0),right=Vec3(900.1,0,0)),(obstacle,)) == [(0.,1.)]
    assert not _free_circle_portal_intervals(replace(portal,left=Vec3(-20,0,0),right=Vec3(20,0,0)),(obstacle,))
    assert not _free_circle_portal_intervals(replace(portal,left=Vec3(0,0,0),right=Vec3(0,0,0)),(obstacle,))
    assert _free_circle_portal_intervals(replace(portal,left=Vec3(900,0,0),right=Vec3(900,0,0)),(obstacle,)) == [(0.,1.)]
    covers = [replace(obstacle,center=Vec3(x,0,0)) for x in (-200,0,200)]
    assert not _free_circle_portal_intervals(portal,covers)


@pytest.mark.parametrize("fault", ["too_many", "barrier", "blocked_start", "waypoint_bound"])
def test_contour_fallback_refuses_unproven_or_out_of_scope_candidates(fault):
    from ff7r_agent.static_obstacles import _portal_contour_detour
    graph = _single_polygon_graph(); scope = _scope()
    start,end = Vec3(300,1000,0),Vec3(2700,1000,0)
    obstacle = _obstacle(scope,center=Vec3(1500,1000,0))
    obstacles = [obstacle]
    kwargs = {"max_waypoints":256,"max_vertical_separation_m":1.0}
    if fault == "too_many": obstacles *= 9
    if fault == "barrier": obstacles = [replace(obstacle,shape="capsule",half_length_m=2)]
    if fault == "blocked_start": obstacles = [replace(obstacle,center=start)]
    if fault == "waypoint_bound": kwargs["max_waypoints"] = 2
    assert _portal_contour_detour(graph,start,end,obstacles,planner_kwargs=kwargs,
                                  minimum_edge_clearance_m=.2) is None


def _recorded_forest_replan_case(angle=0, mirror=1):
    from pathlib import Path
    import dataclasses
    import math
    data = json.loads((Path(__file__).parent / "fixtures/observed-forest-replan-boundary.json").read_text())
    origin = Vec3(**data["start"])
    cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))

    def transform(raw):
        point = Vec3(**raw)
        x, y = point.x - origin.x, (point.y - origin.y) * mirror
        return Vec3(1234 + x*cosine - y*sine, 5678 + x*sine + y*cosine, point.z)

    polygons = {}
    for raw in data["polygons"]:
        values = dict(raw)
        values["reference"] = PolygonReference.from_value(raw["reference"])
        values["vertices"] = tuple(transform(v) for v in raw["vertices"])
        values["links"] = tuple(DetourLink(**v) for v in raw["links"])
        polygons[raw["reference"]] = DetourPolygon(**values)
    adjacency = {}
    for raw in data["portals"]:
        left, right = transform(raw["left"]), transform(raw["right"])
        if mirror < 0:
            left, right = right, left
        portal = DetourPortal(raw["from_ref"], raw["to_ref"], raw["link_ref"],
            raw["source_edge"], raw["target_edge"], left, right, raw["side"])
        adjacency.setdefault(portal.from_reference, []).append(portal)
    graph = DetourTopologyGraph(polygons, adjacency, snapshot_diagnostics={},
                               portal_tolerance_cm=data["portal_tolerance_cm"])
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    obstacles = []
    for raw in data["obstacles"]:
        values = {f.name: raw[f.name] for f in dataclasses.fields(StaticObstacle) if f.name in raw}
        values["center"] = transform(raw["center"])
        item = StaticObstacle(**values)
        obstacles.append(item)
        overlay.admit_obstacle(scope=scope, obstacle=item)
    return graph, overlay, obstacles, transform(data["start"]), transform(data["end"]), data["planner_kwargs"]


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("mirror", [1, -1])
def test_last_global_replan_is_checked_before_declaring_route_blocked(angle, mirror, monkeypatch):
    graph, overlay, obstacles, start, end, kwargs = _recorded_forest_replan_case(angle, mirror)
    original_polygons, original_portals = dict(graph.polygons), dict(graph.adjacency)
    original_plan, candidates = graph.plan, []

    def track(*args, **values):
        candidate = original_plan(*args, **values)
        candidates.append(candidate)
        return candidate

    monkeypatch.setattr(graph, "plan", track)
    plan = plan_with_static_obstacles(graph, start, end, overlay, now_t=380346, **kwargs)
    # The base and first replacement hit the recorded obstacle. The second
    # replacement is already clear, with no extra search or relaxed geometry.
    assert len(candidates) == 3
    assert all(any(route_obstacle_hits(p.waypoints, o) for o in obstacles) for p in candidates[:2])
    assert plan.status == "complete", plan.reason_code
    assert plan.waypoints == candidates[-1].waypoints
    assert plan.diagnostics["static_obstacle_resolution"] == "global_replan"
    assert all(not route_obstacle_hits(plan.waypoints, o) for o in obstacles)
    assert graph.polygons == original_polygons and graph.adjacency == original_portals
    assert all(portal in graph.adjacency[portal.from_reference] or
        replace(portal, left=portal.right, right=portal.left) in graph.adjacency[portal.from_reference]
        for portal in plan.portals[1:-1])


def test_global_replan_budget_still_refuses_a_blocked_last_candidate(monkeypatch):
    graph, overlay, obstacles, start, end, kwargs = _recorded_forest_replan_case()
    blocked = graph.plan(start, end, **kwargs)
    assert any(route_obstacle_hits(blocked.waypoints, o) for o in obstacles)
    calls = []

    def never_clears(*args, **values):
        calls.append(values)
        return blocked

    monkeypatch.setattr(graph, "plan", never_clears)
    plan = plan_with_static_obstacles(graph, start, end, overlay, now_t=380346, **kwargs)
    # Base plus at most two global replans (the grid fallback's leg plans
    # carry no blocked transitions).
    assert sum("blocked_transitions" in call for call in calls) == 2
    assert plan.status == "failed"
    assert plan.reason_code == "static_obstacle_global_replan_still_blocked"
    assert plan.waypoints == ()
    assert plan.validation["static_obstacle_overlay_validated"] is False



def test_successful_movement_breaks_provisional_blocker_confirmation():
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    base = dict(scope=scope, route_plan_sha256="route", route=(Vec3(0,0,0),Vec3(2000,0,0)),
        position_before=Vec3(0,0,0), position_after=Vec3(0,0,0),
        along_route_before_m=0., along_route_after_m=0., commanded_bearing_degrees=0., command_duration_ms=300)
    for sequence in (1,2):
        assert overlay.learn_from_movement(**base,sequence=sequence,sampled_t=float(sequence)).state == ObstacleEvidenceState.CANDIDATE
    progress = {**base, "position_after": Vec3(30,0,0), "along_route_after_m": .3}
    assert overlay.learn_from_movement(**progress,sequence=3,sampled_t=3.).reason_code == "movement_progress_observed"
    # Two more pauses after actual progress are a new candidate, not the third
    # corroborating observation of the old alleged collision.
    for sequence in (4,5):
        result = overlay.learn_from_movement(**base,sequence=sequence,sampled_t=float(sequence))
        assert result.state == ObstacleEvidenceState.CANDIDATE
    assert not overlay.planning_snapshot(now_t=5).obstacles
    assert overlay.learn_from_movement(**base,sequence=6,sampled_t=6.).state == ObstacleEvidenceState.READY


def test_progress_reset_preserves_committed_obstacles_and_capsule_samples():
    overlay = StaticObstacleOverlay()
    scope = _scope()
    overlay.bind_scope(scope)
    committed = _obstacle(scope, center=Vec3(1500,500,0))
    overlay.admit_obstacle(scope=scope, obstacle=committed)
    base = dict(scope=scope, route_plan_sha256="route", route=(Vec3(0,0,0),Vec3(2000,0,0)),
        position_before=Vec3(0,0,0), position_after=Vec3(0,0,0),
        along_route_before_m=0., along_route_after_m=0., commanded_bearing_degrees=0., command_duration_ms=300,
        query_status="blocked",query_verified=True,blocking_distance_m=1.,capsule_radius_m=.42,
        probe_world_bearing_degrees=0.,require_independent_confirmation=True)
    first = overlay.learn_from_movement(**base,sequence=1,sampled_t=1.)
    assert first.state == ObstacleEvidenceState.CANDIDATE
    digest = overlay.planning_snapshot(now_t=1).digest()
    assert overlay.reset_movement_candidates(scope=scope) == 0
    assert overlay.planning_snapshot(now_t=1).digest() == digest
    second = overlay.learn_from_movement(**base,sequence=2,sampled_t=2.)
    assert second.evidence_count == 2
    assert committed in overlay.planning_snapshot(now_t=2).obstacles


def test_blocked_transitions_skip_only_portals_no_obstacle_reaches():
    """The bounding-box prefilter keeps exactly the portals the full check blocks."""
    import math
    import random
    from types import SimpleNamespace
    from ff7r_agent.static_obstacles import _free_circle_portal_intervals, _fully_blocked_transitions
    scope = _scope()
    generator = random.Random(20260927)
    portals = []
    for index in range(400):
        x, y = generator.uniform(0, 5000), generator.uniform(0, 5000)
        length = generator.choice((0.0, generator.uniform(10, 400)))
        angle = generator.uniform(0, 6.283)
        portals.append(_portal(index, index + 1, Vec3(x, y, 0),
                               Vec3(x + length * math.cos(angle), y + length * math.sin(angle), 0)))
    graph = SimpleNamespace(adjacency={0: tuple(portals)})
    blocked_any = 0
    for _ in range(60):
        obstacles = [replace(_obstacle(scope, center=Vec3(generator.uniform(0, 5000), generator.uniform(0, 5000), 0)),
                             radius_m=generator.uniform(0.05, 3.0))
                     for _ in range(generator.randint(1, 8))]
        expected = {(portal.from_reference, portal.to_reference) for portal in portals
                    if not _free_circle_portal_intervals(portal, obstacles)}
        assert _fully_blocked_transitions(graph, obstacles) == expected
        blocked_any += bool(expected)
    assert blocked_any > 5
    assert _fully_blocked_transitions(graph, ()) == set()


def test_contour_search_resolves_nodes_without_scanning_every_polygon(monkeypatch):
    """Circle nodes round an obstacle by the navmesh edge fall off the mesh;
    resolving them must not scan every polygon (live at Kalm: 32 scans, 25 s
    of one recovery preflight) and must give the same detour as before."""
    from ff7r_agent.static_obstacles import _portal_contour_detour
    graph, _, _ = _corridor_graph()
    # Only the three indexed 10 m squares along the y = 0 edge.
    del graph.polygons[PolygonReference.make(1, 4, 0).value]
    graph.adjacency = {
        key: tuple(portal for portal in value if portal.to_reference in graph.polygons)
        for key, value in graph.adjacency.items() if key in graph.polygons
    }
    graph._build_point_resolution_index()
    assert graph._point_resolution_index_overflow == ()
    scope = _scope()
    obstacles = [replace(_obstacle(scope, center=Vec3(1500, 100, 0)), radius_m=0.8)]
    start, end = Vec3(800, 250, 0), Vec3(2200, 250, 0)
    kwargs = {"max_waypoints": 256, "max_vertical_separation_m": 1.0, "max_snap_distance_m": 0.75}
    scans = []
    original_scan = graph.resolve_point_full_scan

    def counted_scan(*args, **kwargs):
        scans.append(args)
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(graph, "resolve_point_full_scan", counted_scan)
    monkeypatch.setattr(graph, "resolve_point_if_resolvable", graph.resolve_point)
    before = _portal_contour_detour(graph, start, end, obstacles, planner_kwargs=kwargs, minimum_edge_clearance_m=.2)
    assert scans, "the fixture must place contour nodes off the mesh"
    monkeypatch.undo()
    monkeypatch.setattr(graph, "resolve_point_full_scan", lambda *a, **k: pytest.fail("full polygon scan"))
    after = _portal_contour_detour(graph, start, end, obstacles, planner_kwargs=kwargs, minimum_edge_clearance_m=.2)
    assert before is not None and after == before
    assert after.diagnostics["static_obstacle_resolution"] == "portal_contour_detour"
