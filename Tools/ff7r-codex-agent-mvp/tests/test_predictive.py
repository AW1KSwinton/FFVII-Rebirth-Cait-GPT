from ff7r_agent.models import Vec3
from ff7r_agent.predictive import (
    HeadingCandidate, HeadingBlocker, MotionSample, choose_heading,
    estimate_velocity, plan_motion_hold, replay_navigation_metrics,
    stopping_buffer_m,
)


def test_velocity_estimate_uses_robust_horizontal_motion():
    result = estimate_velocity([
        MotionSample(0.0, Vec3(0, 0, 0), 1),
        MotionSample(1.0, Vec3(100, 0, 0), 2),
        MotionSample(2.0, Vec3(300, 0, 0), 3),
        MotionSample(3.0, Vec3(500, 0, 0), 4),
    ])
    assert result.speed_mps == 2.0
    assert result.bearing_degrees == 0.0
    assert result.confidence == "high"


def test_motion_plan_stops_before_clearance():
    result = plan_motion_hold(5.0, 8.0, 2.0, maximum_ms=5000)
    assert result.outcome == "planned"
    assert result.stopping_buffer_m == stopping_buffer_m(2.0)
    assert result.planned_distance_m == 5.0
    assert result.hold_ms == 2500


def test_motion_plan_refuses_unknown_or_unsafe_clearance():
    assert plan_motion_hold(5.0, None, 2.0).outcome == "perception_required"
    assert plan_motion_hold(5.0, 1.0, 2.0).outcome == "blocked"


def test_heading_choice_prefers_route_progress_and_quarantines_failed_heading():
    result = choose_heading(
        [HeadingCandidate("forward", 0.0, 12.0, 12.0), HeadingCandidate("right", 90.0, 6.0, 4.0)],
        target_bearing_degrees=0.0,
        stopping_buffer_m=1.0,
        current_position=Vec3(0, 0, 0),
        recent_blockers=[HeadingBlocker(position=Vec3(100, 0, 0), bearing_degrees=0.0, direction="forward")],
    )
    assert result.direction == "right"
    assert {item["direction"]: item for item in result.ranked}["forward"]["blocked_by_history"] is True


def test_replay_metrics_expose_progress_and_recovery():
    result = replay_navigation_metrics([
        {"action": "travel_direction_chunk", "direction": "forward", "horizontal_displacement": 4.0, "actual_hold_ms": 2000},
        {"action": "travel_direction_chunk", "direction": "forward", "horizontal_displacement": 0.0, "actual_hold_ms": 2000},
        {"action": "travel_direction_chunk", "direction": "backward", "horizontal_displacement": 2.0, "actual_hold_ms": 1000},
    ])
    assert result["movement_action_count"] == 3
    assert result["progressed_movement_count"] == 2
    assert result["collision_recovery_count"] == 1
    assert result["stationary_hold_seconds"] == 2.0
