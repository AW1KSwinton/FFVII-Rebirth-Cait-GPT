from __future__ import annotations

from dataclasses import replace
import math

import pytest

from ff7r_agent.dynamic_obstacles import (
    ConflictClassification,
    DynamicObstacleObservation,
    DynamicObstacleSnapshot,
    DynamicObstacleTracker,
    ObstacleEvidenceState,
    StaticLearningDynamicState,
    ObstacleGeometry,
    Point3M,
    TrackerConfig,
    analyze_route_obstacle,
    analyze_route_obstacles,
    assess_static_learning_dynamic_evidence,
    snapshot_from_game_state,
)
from ff7r_agent.models import Character, GameState, ObservedActor, Vec3


ROUTE = (Point3M(0.0, 0.0, 0.0), Point3M(10.0, 0.0, 0.0))
RADIUS = ObstacleGeometry(0.5, "synthetic_test_route182_radius")


def point(x: float, y: float, z: float = 0.0) -> Point3M:
    return Point3M(x, y, z)


def observation(
    actor_id: str,
    position: Point3M,
    *,
    sequence: int = 1,
    sampled_at_ms: int = 1_000,
    velocity: Point3M | None = None,
    confidence: float | None = None,
    sample_count: int | None = None,
    age_seconds: float = 0.0,
) -> DynamicObstacleObservation:
    cloud = point(0.0, 0.0, 0.0)
    relative = position - cloud
    if velocity is None:
        confidence = 0.0
        sample_count = 0
    else:
        confidence = 0.5 if confidence is None else confidence
        sample_count = 1 if sample_count is None else sample_count
    return DynamicObstacleObservation(
        actor_id=actor_id,
        base_sequence=sequence,
        sampled_at_ms=sampled_at_ms,
        cloud_position_m=cloud,
        actor_position_m=position,
        relative_position_m=relative,
        horizontal_distance_m=relative.horizontal_length(),
        vertical_separation_m=abs(relative.z),
        velocity_mps=velocity,
        velocity_confidence=confidence,
        velocity_sample_count=sample_count,
        observation_age_seconds=age_seconds,
        source_identity="synthetic-test",
    )


def snapshot(
    sequence: int,
    sampled_at_ms: int,
    *items: DynamicObstacleObservation,
    complete: bool = True,
    rejection_reason_codes: tuple[str, ...] = (),
) -> DynamicObstacleSnapshot:
    return DynamicObstacleSnapshot(
        sequence,
        sampled_at_ms,
        tuple(items),
        "synthetic-test",
        complete=complete,
        observed_actor_count=len(items),
        accepted_actor_count=len(items),
        rejected_actor_count=0,
        rejection_reason_codes=rejection_reason_codes,
    )


def rebuilt_snapshot(
    parent: DynamicObstacleSnapshot,
    items: tuple[DynamicObstacleObservation, ...],
    *,
    complete: bool,
    observed_actor_count: int,
    rejected_actor_count: int,
    rejection_reason_codes: tuple[str, ...],
) -> DynamicObstacleSnapshot:
    return DynamicObstacleSnapshot(
        base_sequence=parent.base_sequence,
        sampled_at_ms=parent.sampled_at_ms,
        observations=items,
        source_identity=parent.source_identity,
        complete=complete,
        observed_actor_count=observed_actor_count,
        accepted_actor_count=len(items),
        rejected_actor_count=rejected_actor_count,
        rejection_reason_codes=rejection_reason_codes,
    )


def analyze(item: DynamicObstacleObservation, *, route=ROUTE, progress=0.0, speed=2.0, horizon=5.0, radius=RADIUS, vertical=0.5):
    return analyze_route_obstacle(
        item.cloud_position_m,
        route,
        route_progress_m=progress,
        obstacle=item,
        geometry=radius,
        cloud_speed_mps=speed,
        horizon_seconds=horizon,
        vertical_clearance_m=vertical,
    )


def test_stationary_actor_directly_on_route_is_conflict():
    result = analyze(observation("actor-on-route", point(4.0, 0.0)))

    assert result.classification == ConflictClassification.CONFLICT
    assert result.reason_code == "predicted_route_corridor_conflict"
    assert result.nearest_segment_index == 0
    assert result.time_to_conflict_seconds is not None


def test_stationary_actor_laterally_clear_is_no_conflict():
    result = analyze(observation("actor-clear", point(4.0, 2.0)))

    assert result.classification == ConflictClassification.NO_CONFLICT
    assert result.reason_code.startswith("lateral_clearance")


def test_actor_behind_cloud_is_not_forward_conflict():
    result = analyze(observation("actor-behind", point(1.0, 0.0)), progress=3.0)

    assert result.classification == ConflictClassification.NO_CONFLICT
    assert result.reason_code == "behind_route_progress"
    assert result.longitudinal_distance_ahead_m == pytest.approx(-2.0)


def test_actor_ahead_but_vertically_separated_is_not_ground_conflict():
    result = analyze(observation("actor-above", point(4.0, 0.0, 2.0)), vertical=0.5)

    assert result.classification == ConflictClassification.NO_CONFLICT
    assert result.reason_code.startswith("vertical_separation")
    assert result.vertical_separation_m == pytest.approx(2.0)


def test_moving_actor_crossing_route_is_predicted_conflict():
    item = observation("crossing", point(0.0, 2.0), velocity=point(1.0, -1.0))

    result = analyze(item, speed=1.0, horizon=4.0)

    assert result.classification == ConflictClassification.CONFLICT
    assert result.time_to_conflict_seconds == pytest.approx(1.5, abs=1.0e-5)


def test_moving_actor_leaving_route_conflict_clears():
    item = observation("leaving", point(2.0, 2.0), velocity=point(0.0, 2.0), sampled_at_ms=2_000)

    result = analyze(item, progress=1.0, speed=1.0, horizon=2.0)

    assert result.classification == ConflictClassification.NO_CONFLICT
    assert result.reason_code.startswith("lateral_clearance")


def test_stale_actor_is_unknown_and_not_authorizable():
    result = analyze(observation("stale", point(4.0, 0.0), age_seconds=1.0))

    assert result.classification == ConflictClassification.UNKNOWN
    assert result.reason_code == "stale_observation"
    assert result.authorizable is False


def test_parent_observation_source_identity_mismatch_is_rejected():
    item = observation("identity-mismatch", point(2.0, 2.0))

    with pytest.raises(ValueError, match="source identity"):
        DynamicObstacleSnapshot(
            base_sequence=1,
            sampled_at_ms=1_000,
            observations=(item,),
            source_identity="another-producer",
            complete=True,
            observed_actor_count=1,
            accepted_actor_count=1,
            rejected_actor_count=0,
        )


def test_game_state_missing_position_is_retained_as_incomplete_evidence():
    cloud = Character("Cloud", 100, 100, 10, 10, position=Vec3(0.0, 0.0, 0.0))
    state = GameState(
        schema_version=1,
        sequence=11,
        timestamp_utc="2026-08-23T12:00:00Z",
        running=True,
        paused=False,
        in_menu=False,
        in_combat=False,
        location="test",
        objective="test",
        player=cloud,
        nearby_actors=(
            ObservedActor("valid", kind="character", position=Vec3(100.0, 200.0, 0.0)),
            ObservedActor("missing", kind="character", position=None),
        ),
    )

    result = snapshot_from_game_state(state)

    assert result.complete is False
    assert result.observed_actor_count == 2
    assert result.accepted_actor_count == 1
    assert result.rejected_actor_count == 1
    assert result.rejection_reason_codes == (
        "actor_position_unavailable",
        "producer_completeness_unproven",
    )


def test_incomplete_snapshot_with_all_known_actors_clear_is_unknown():
    parent = snapshot_from_game_state(GameState(
        schema_version=1,
        sequence=12,
        timestamp_utc="2026-08-23T12:00:00Z",
        running=True,
        paused=False,
        in_menu=False,
        in_combat=False,
        location="test",
        objective="test",
        player=Character("Cloud", 100, 100, 10, 10, position=Vec3(0.0, 0.0, 0.0)),
        nearby_actors=(
            ObservedActor("valid-clear", kind="character", position=Vec3(100.0, 200.0, 0.0)),
            ObservedActor("missing", kind="character", position=None),
        ),
    ))
    represented = replace(
        parent.observations[0],
        velocity_mps=point(0.0, 0.0, 0.0),
        velocity_confidence=0.5,
        velocity_sample_count=1,
    )
    partial = rebuilt_snapshot(
        parent,
        (represented,),
        complete=False,
        observed_actor_count=2,
        rejected_actor_count=1,
        rejection_reason_codes=("actor_position_unavailable", "producer_completeness_unproven"),
    )

    result = analyze_route_obstacles(
        point(0.0, 0.0),
        ROUTE,
        route_progress_m=0.0,
        obstacles=((represented, RADIUS),),
        cloud_speed_mps=1.0,
        horizon_seconds=1.0,
        vertical_clearance_m=0.5,
        snapshot=partial,
    )

    assert result.classification == ConflictClassification.UNKNOWN
    assert result.reason_code == "incomplete_obstacle_snapshot"
    assert result.authorizable is False


def test_incomplete_snapshot_with_definite_conflict_can_surface_stop_signal():
    parent = snapshot_from_game_state(GameState(
        schema_version=1,
        sequence=13,
        timestamp_utc="2026-08-23T12:00:00Z",
        running=True,
        paused=False,
        in_menu=False,
        in_combat=False,
        location="test",
        objective="test",
        player=Character("Cloud", 100, 100, 10, 10, position=Vec3(0.0, 0.0, 0.0)),
        nearby_actors=(
            ObservedActor("valid-conflict", kind="character", position=Vec3(100.0, 0.0, 0.0)),
            ObservedActor("missing", kind="character", position=None),
        ),
    ))
    represented = replace(
        parent.observations[0],
        velocity_mps=point(0.0, 0.0, 0.0),
        velocity_confidence=0.5,
        velocity_sample_count=1,
    )
    partial = rebuilt_snapshot(
        parent,
        (represented,),
        complete=False,
        observed_actor_count=2,
        rejected_actor_count=1,
        rejection_reason_codes=("actor_position_unavailable", "producer_completeness_unproven"),
    )

    result = analyze_route_obstacles(
        point(0.0, 0.0),
        ROUTE,
        route_progress_m=0.0,
        obstacles=((represented, RADIUS),),
        cloud_speed_mps=1.0,
        horizon_seconds=1.0,
        vertical_clearance_m=0.5,
        snapshot=partial,
    )

    assert result.classification == ConflictClassification.CONFLICT
    assert result.reason_code == "nearest_relevant_conflict"


def test_empty_explicitly_incomplete_snapshot_is_unknown():
    incomplete = DynamicObstacleSnapshot(
        base_sequence=20,
        sampled_at_ms=2_000,
        observations=(),
        source_identity="synthetic-test",
        complete=False,
        observed_actor_count=0,
        accepted_actor_count=0,
        rejected_actor_count=0,
        rejection_reason_codes=("producer_completeness_unproven",),
    )

    result = analyze_route_obstacles(
        point(0.0, 0.0),
        ROUTE,
        route_progress_m=0.0,
        obstacles=(),
        cloud_speed_mps=1.0,
        horizon_seconds=1.0,
        snapshot=incomplete,
    )

    assert result.classification == ConflictClassification.UNKNOWN
    assert result.reason_code == "incomplete_obstacle_snapshot"
    assert result.authorizable is False


def test_complete_empty_snapshot_is_distinct_and_authorizable_clear():
    complete_empty = DynamicObstacleSnapshot(
        base_sequence=21,
        sampled_at_ms=2_100,
        observations=(),
        source_identity="future-complete-producer",
        complete=True,
        observed_actor_count=0,
        accepted_actor_count=0,
        rejected_actor_count=0,
    )

    result = analyze_route_obstacles(
        point(0.0, 0.0),
        ROUTE,
        route_progress_m=0.0,
        obstacles=(),
        cloud_speed_mps=1.0,
        horizon_seconds=1.0,
        snapshot=complete_empty,
    )

    assert result.classification == ConflictClassification.NO_CONFLICT
    assert result.reason_code == "complete_no_relevant_actors"
    assert result.authorizable is True


def test_complete_valid_multi_actor_snapshot_can_authorize_clear():
    first = observation(
        "complete-a",
        point(2.0, 2.0),
        sequence=30,
        sampled_at_ms=3_000,
        velocity=point(0.0, 0.0, 0.0),
    )
    second = observation(
        "complete-b",
        point(4.0, -2.0),
        sequence=30,
        sampled_at_ms=3_000,
        velocity=point(0.0, 0.0, 0.0),
    )
    complete = snapshot(30, 3_000, first, second, complete=True)

    result = analyze_route_obstacles(
        point(0.0, 0.0),
        ROUTE,
        route_progress_m=0.0,
        obstacles=((first, RADIUS), (second, RADIUS)),
        cloud_speed_mps=1.0,
        horizon_seconds=1.0,
        vertical_clearance_m=0.5,
        snapshot=complete,
    )

    assert result.classification == ConflictClassification.NO_CONFLICT
    assert result.reason_code == "all_obstacles_clear_within_horizon"
    assert result.authorizable is True


def test_malformed_actor_cannot_disappear_into_authorizable_clear():
    cloud = Character("Cloud", 100, 100, 10, 10, position=Vec3(0.0, 0.0, 0.0))
    state = GameState(
        schema_version=1,
        sequence=31,
        timestamp_utc="2026-08-23T12:00:00Z",
        running=True,
        paused=False,
        in_menu=False,
        in_combat=False,
        location="test",
        objective="test",
        player=cloud,
        nearby_actors=(
            ObservedActor("valid-clear", kind="character", position=Vec3(100.0, 200.0, 0.0)),
            ObservedActor("malformed", kind="character", position=Vec3(float("nan"), 200.0, 0.0)),
        ),
    )
    partial = snapshot_from_game_state(state)
    represented = replace(
        partial.observations[0],
        velocity_mps=point(0.0, 0.0, 0.0),
        velocity_confidence=0.5,
        velocity_sample_count=1,
    )
    partial = rebuilt_snapshot(
        partial,
        (represented,),
        complete=False,
        observed_actor_count=2,
        rejected_actor_count=1,
        rejection_reason_codes=("actor_position_invalid", "producer_completeness_unproven"),
    )

    result = analyze_route_obstacles(
        point(0.0, 0.0),
        ROUTE,
        route_progress_m=0.0,
        obstacles=((represented, RADIUS),),
        cloud_speed_mps=1.0,
        horizon_seconds=1.0,
        vertical_clearance_m=0.5,
        snapshot=partial,
    )

    assert result.classification == ConflictClassification.UNKNOWN
    assert result.authorizable is False


def test_out_of_order_snapshot_is_rejected():
    tracker = DynamicObstacleTracker()
    first = observation("ordered", point(0.0, 0.0), sequence=2, sampled_at_ms=2_000)
    tracker.update(snapshot(2, 2_000, first))

    result = tracker.update(snapshot(1, 1_000, observation("ordered", point(1.0, 0.0), sequence=1, sampled_at_ms=1_000)))

    assert result.snapshot_accepted is False
    assert result.reason_code == "out_of_order_snapshot"


def test_duplicate_sample_does_not_create_fake_velocity():
    tracker = DynamicObstacleTracker()
    item = observation("duplicate", point(0.0, 0.0), sequence=1, sampled_at_ms=1_000)
    tracker.update(snapshot(1, 1_000, item))

    result = tracker.update(snapshot(1, 1_000, item))
    track = result.tracks[0]

    assert result.snapshot_accepted is False
    assert result.reason_code == "duplicate_snapshot"
    assert track.observation.velocity_mps is None
    assert track.observation.velocity_sample_count == 0


def test_impossible_velocity_is_kept_as_low_confidence_position_only():
    tracker = DynamicObstacleTracker(TrackerConfig(maximum_believable_velocity_mps=5.0))
    tracker.update(snapshot(1, 1_000, observation("teleport", point(0.0, 0.0), sequence=1, sampled_at_ms=1_000)))
    result = tracker.update(snapshot(2, 1_100, observation("teleport", point(100.0, 0.0), sequence=2, sampled_at_ms=1_100)))

    track = result.tracks[0]
    assert track.velocity_evidence == "impossible_velocity"
    assert track.observation.velocity_mps is None
    assert track.observation.velocity_confidence == 0.0


def test_actor_disappearance_expires_without_indefinite_extrapolation():
    tracker = DynamicObstacleTracker()
    tracker.update(snapshot(1, 1_000, observation("vanished", point(1.0, 0.0), sequence=1, sampled_at_ms=1_000)))
    tracker.update(snapshot(2, 1_500))

    track = tracker.tracks(now_ms=2_600)[0]
    assert track.evidence_state == ObstacleEvidenceState.EXPIRED
    assert tracker.current_obstacles(now_ms=2_600) == ()


def test_multiple_actors_select_earliest_deterministically():
    result = analyze_route_obstacles(
        point(0.0, 0.0),
        ROUTE,
        route_progress_m=0.0,
        obstacles=(
            (observation("later", point(7.0, 0.0)), RADIUS),
            (observation("earlier", point(3.0, 0.0)), RADIUS),
        ),
        cloud_speed_mps=2.0,
        horizon_seconds=5.0,
        vertical_clearance_m=0.5,
    )

    assert result.classification == ConflictClassification.CONFLICT
    assert result.selected is not None
    assert result.selected.actor_id == "earlier"
    assert [item.actor_id for item in result.analyses] == ["earlier", "later"]


def test_route_bend_is_evaluated_after_first_segment():
    bent_route = (point(0.0, 0.0), point(5.0, 0.0), point(5.0, 5.0))
    result = analyze(observation("bend", point(5.0, 2.0)), route=bent_route, speed=2.0, horizon=5.0)

    assert result.classification == ConflictClassification.CONFLICT
    assert result.nearest_segment_index == 1
    assert result.time_to_conflict_seconds is not None


def test_waypoint_boundary_uses_stable_first_segment_tie_break():
    bent_route = (point(0.0, 0.0), point(5.0, 0.0), point(5.0, 5.0))
    result = analyze(
        observation("waypoint", point(5.0, 0.0)),
        route=bent_route,
        progress=4.5,
        speed=1.0,
        horizon=2.0,
    )

    assert result.classification == ConflictClassification.CONFLICT
    assert result.nearest_segment_index == 0
    assert result.along_route_m == pytest.approx(5.0)


def test_nonfinite_position_is_rejected():
    with pytest.raises(ValueError, match="finite"):
        Point3M(float("nan"), 0.0, 0.0)

    with pytest.raises(ValueError, match="finite"):
        observation("bad", point(1.0, 0.0)).__class__(
            actor_id="bad",
            base_sequence=1,
            sampled_at_ms=1_000,
            cloud_position_m=point(0.0, 0.0),
            actor_position_m=point(float("inf"), 0.0),
            relative_position_m=point(float("inf"), 0.0),
            horizontal_distance_m=math.inf,
            vertical_separation_m=0.0,
            source_identity="synthetic-test",
        )


def test_route182_like_moving_character_is_flagged_before_simulated_collision():
    # Conceptual replay only: Cloud starts on route, the character begins at
    # 1.013 m cross-track and 0.24 m vertical separation, then crosses into
    # the route.  No original Aerith trajectory is asserted.
    item = observation(
        "synthetic-route182-character",
        point(2.0, 1.013, 0.24),
        velocity=point(0.0, -0.5, 0.0),
    )

    result = analyze(item, speed=1.0, horizon=3.0, vertical=0.5)

    assert result.classification == ConflictClassification.CONFLICT
    assert result.time_to_conflict_seconds is not None
    assert result.time_to_conflict_seconds < 2.026  # centerline crossing time


def test_no_actor_data_is_unknown_not_route_clear():
    result = analyze_route_obstacles(
        point(0.0, 0.0),
        ROUTE,
        route_progress_m=0.0,
        obstacles=(),
        cloud_speed_mps=2.0,
        horizon_seconds=3.0,
    )

    assert result.classification == ConflictClassification.UNKNOWN
    assert result.reason_code == "no_obstacle_data"
    assert result.authorizable is False


def test_synthetic_radius_provenance_is_explicit_and_not_a_game_constant():
    geometry = ObstacleGeometry(0.4, "synthetic_test_only_route182")

    assert geometry.radius_m == pytest.approx(0.4)
    assert geometry.radius_source.startswith("synthetic_test")
    assert geometry.effective_radius_m == pytest.approx(0.4)


def test_party_position_is_retained_but_never_classified_as_static_blocker():
    cloud = Character("Cloud", 100, 100, 10, 10, position=Vec3(0.0, 0.0, 0.0))
    tifa = Character("Tifa", 100, 100, 10, 10, position=Vec3(100.0, 0.0, 0.0))
    state = GameState(
        schema_version=1, sequence=9, timestamp_utc="2026-08-23T12:00:00Z",
        running=True, paused=False, in_menu=False, in_combat=False,
        location="test", objective="test", player=cloud, party=(tifa,),
    )
    result = snapshot_from_game_state(state)
    assert len(result.observations) == 1
    assert result.observations[0].actor_class == "party"
    evidence = assess_static_learning_dynamic_evidence(
        result, cloud_position_m=Point3M(0.0, 0.0, 0.0), intended_bearing_degrees=0.0,
    )
    assert evidence.blocking_actor_ids == ()
    assert evidence.state != StaticLearningDynamicState.BLOCKING


def test_npc_within_two_metres_is_reported_as_dynamic_blocker():
    cloud = Character("Cloud", 100, 100, 10, 10, position=Vec3(0.0, 0.0, 0.0))
    npc = ObservedActor("npc-1", kind="npc", position=Vec3(100.0, 0.0, 0.0))
    state = GameState(
        schema_version=1, sequence=10, timestamp_utc="2026-08-23T12:00:00Z",
        running=True, paused=False, in_menu=False, in_combat=False,
        location="test", objective="test", player=cloud, nearby_actors=(npc,),
    )
    result = snapshot_from_game_state(state)
    assert result.observations[0].actor_class == "npc"
    evidence = assess_static_learning_dynamic_evidence(
        result, cloud_position_m=Point3M(0.0, 0.0, 0.0), intended_bearing_degrees=0.0,
    )
    assert evidence.state == StaticLearningDynamicState.BLOCKING
    assert evidence.blocking_actor_ids == ("npc-1",)


def test_actor_beyond_two_metres_is_not_used_for_short_blocker_check():
    cloud = Character("Cloud", 100, 100, 10, 10, position=Vec3(0.0, 0.0, 0.0))
    npc = ObservedActor("npc-far", kind="npc", position=Vec3(250.0, 0.0, 0.0))
    state = GameState(
        schema_version=1, sequence=11, timestamp_utc="2026-08-23T12:00:00Z",
        running=True, paused=False, in_menu=False, in_combat=False,
        location="test", objective="test", player=cloud, nearby_actors=(npc,),
    )
    result = snapshot_from_game_state(state)
    evidence = assess_static_learning_dynamic_evidence(
        result, cloud_position_m=Point3M(0.0, 0.0, 0.0), intended_bearing_degrees=0.0,
    )
    assert evidence.state != StaticLearningDynamicState.BLOCKING


def test_existing_game_state_actor_adapter_converts_cm_to_metres():
    cloud = Character(
        "Cloud", 100, 100, 10, 10,
        position=Vec3(0.0, 0.0, 0.0),
    )
    actor = ObservedActor(
        id="sidecar-actor",
        kind="character",
        position=Vec3(100.0, 200.0, 30.0),
    )
    state = GameState(
        schema_version=1,
        sequence=7,
        timestamp_utc="2026-08-23T12:00:00Z",
        running=True,
        paused=False,
        in_menu=False,
        in_combat=False,
        location="test",
        objective="test",
        player=cloud,
        nearby_actors=(actor,),
    )

    result = snapshot_from_game_state(state)

    assert result.observations[0].actor_position_m == point(1.0, 2.0, 0.3)
    assert result.observations[0].relative_position_m == point(1.0, 2.0, 0.3)
    assert result.observations[0].actor_category == "character"
