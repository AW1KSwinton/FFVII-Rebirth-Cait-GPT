from datetime import datetime, timezone
import inspect
from pathlib import Path

import pytest

from ff7r_agent.evidence_fusion import (
    cross_track_distance_m,
    fresh_capsule_probe,
    fuse_direction_evidence,
)
from ff7r_agent.models import GameState, Vec3
from ff7r_agent.input import SimulationInput
from ff7r_agent.service import (
    AgentService,
    _bounded_capsule_clearance_m,
    _navigation_obstacle_within_horizon,
    _verified_corridor_clearance_m,
)
from ff7r_agent.telemetry import SimulationTelemetry


def state_with_directions(directions):
    return GameState.from_dict({
        "schema_version": 1,
        "sequence": 4,
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "game": {"running": True, "map_id": "Square", "location": "Town"},
        "player": {
            "name": "Cloud", "hp": 10, "max_hp": 10, "mp": 2, "max_mp": 2,
            "position": {"x": 0, "y": 0, "z": 0},
            "rotation": {"pitch": 0, "yaw": 0, "roll": 0},
        },
        "camera": {"rotation": {"pitch": 0, "yaw": 0, "roll": 0}},
        "navigation": {
            "source": "ue4ss_line_trace_multi_height",
            "directions": directions,
        },
    })


def test_goal_vector_is_only_a_local_corridor_tiebreaker():
    state = state_with_directions({
        "forward": {"clear_distance_m": 2.0, "confidence": "high"},
        "right": {"clear_distance_m": 10.0, "confidence": "high"},
    })

    decision = fuse_direction_evidence(state, goal_bearing_degrees=90.0)

    assert decision.outcome == "local_corridor_selected"
    assert decision.selected_direction == "right"
    assert decision.route_available is False
    assert decision.target_vector_policy == "low_confidence_goal_tiebreaker_with_local_clearance"
    assert any("goal_vector_tiebreaker" in item.sources for item in decision.candidates)


def test_complete_route_leg_beats_final_goal_vector():
    state = state_with_directions({
        "forward": {"clear_distance_m": 11.0, "confidence": "high"},
        "right": {"clear_distance_m": 12.0, "confidence": "high"},
    })

    decision = fuse_direction_evidence(
        state,
        route_bearing_degrees=0.0,
        goal_bearing_degrees=90.0,
        route_source="ue4ss_navmesh_path",
    )

    assert decision.outcome == "route_leg_selected"
    assert decision.selected_direction == "forward"
    assert decision.route_available is True
    assert any("route_leg_bearing" in item.sources for item in decision.candidates)


def test_route_leg_does_not_accept_unrelated_clear_heading_as_detour():
    state = state_with_directions({
        "forward": {"clear_distance_m": 0.5, "confidence": "high"},
        "right": {"clear_distance_m": 12.0, "confidence": "high"},
    })

    decision = fuse_direction_evidence(
        state,
        route_bearing_degrees=0.0,
        route_source="ue4ss_navmesh_path",
    )

    assert decision.selected_direction is None
    assert decision.outcome == "route_deviation_replan"
    assert any("route-leg deviation budget" in item.reason for item in decision.candidates)


def test_recent_no_progress_heading_is_excluded():
    state = state_with_directions({
        "forward": {"clear_distance_m": 12.0, "confidence": "high"},
        "right": {"clear_distance_m": 8.0, "confidence": "high"},
    })

    decision = fuse_direction_evidence(
        state,
        goal_bearing_degrees=0.0,
        blockers=[{"bearing_degrees": 0.0}],
    )

    assert decision.selected_direction == "right"
    forward = next(item for item in decision.candidates if item.direction == "forward")
    assert forward.eligible is False
    assert "recent no-progress" in forward.reason


def test_line_trace_hit_cannot_authorize_forward_dead_end():
    state = state_with_directions({
        "forward": {"clear_distance_m": 3.8, "hit": True, "confidence": "high"},
        "right": {"clear_distance_m": 12.0, "hit": False, "confidence": "high"},
    })

    decision = fuse_direction_evidence(state, goal_bearing_degrees=0.0)

    assert decision.selected_direction == "right"
    forward = next(item for item in decision.candidates if item.direction == "forward")
    assert forward.eligible is False
    assert "line-trace hit blocks" in forward.reason


def test_fresh_capsule_clearance_can_override_line_hit():
    state = state_with_directions({
        "forward": {
            "clear_distance_m": 3.8,
            "hit": True,
            "confidence": "high",
            "capsule_probe": {
                "available": True,
                "fresh": True,
                "sample_sequence": 10,
                "hit": True,
                "blocking_hit": False,
            },
            "walkable_surface_hit": True,
            "surface_class": "walkable_surface_candidate",
        },
        "right": {"clear_distance_m": 12.0, "hit": False, "confidence": "high"},
    })
    state.navigation["base_sequence"] = 10

    decision = fuse_direction_evidence(state, goal_bearing_degrees=0.0)

    assert decision.selected_direction == "forward"
    forward = next(item for item in decision.candidates if item.direction == "forward")
    assert forward.eligible is True
    assert "capsule_sweep" in forward.sources


def test_fresh_capsule_clearance_overrides_a_farther_line_hit():
    state = state_with_directions({
        "forward": {
            "clear_distance_m": 10.0,
            "hit": True,
            "confidence": "high",
            "capsule_probe": {
                "available": True,
                "fresh": True,
                "sample_sequence": 13,
                "hit": False,
                "blocking_hit": False,
                "corridor_confirmed": True,
                "clear_distance_m": 6.0,
            },
        },
        "right": {"clear_distance_m": 12.0, "hit": False, "confidence": "high"},
    })
    state.navigation["base_sequence"] = 13

    decision = fuse_direction_evidence(state, goal_bearing_degrees=0.0)

    assert decision.selected_direction == "forward"
    forward = next(item for item in decision.candidates if item.direction == "forward")
    assert forward.eligible is True


def test_capsule_floor_hit_does_not_erase_a_blocking_line_trace():
    state = state_with_directions({
        "forward": {
            "clear_distance_m": 3.8,
            "hit": True,
            "blocking_hit": True,
            "walkable_surface_hit": False,
            "surface_class": "blocking_or_unknown",
            "capsule_probe": {
                "available": True,
                "fresh": True,
                "sample_sequence": 12,
                "hit": True,
                "blocking_hit": False,
                "corridor_confirmed": False,
            },
        },
        "right": {"clear_distance_m": 12.0, "hit": False, "confidence": "high"},
    })
    state.navigation["base_sequence"] = 12

    decision = fuse_direction_evidence(state, goal_bearing_degrees=0.0)

    assert decision.selected_direction == "right"
    forward = next(item for item in decision.candidates if item.direction == "forward")
    assert forward.eligible is False
    assert "line-trace hit blocks" in forward.reason


def test_visual_reviewed_walkable_capsule_allows_short_detour_over_line_hit():
    state = state_with_directions({
        "forward": {"clear_distance_m": 12.0, "hit": False, "confidence": "high"},
        "left": {
            "clear_distance_m": 6.6,
            "hit": True,
            "walkable_surface_hit": False,
            "surface_class": "blocking_or_unknown",
            "confidence": "high",
            "capsule_probe": {
                "available": True,
                "fresh": True,
                "sample_sequence": 4,
                "hit": True,
                "blocking_hit": False,
                "walkable_surface_hit": True,
                "corridor_confirmed": False,
                "clear_distance_m": 5.4,
            },
        },
        "right": {"clear_distance_m": 12.0, "hit": False, "confidence": "high"},
    })
    state.navigation["base_sequence"] = 4

    decision = fuse_direction_evidence(
        state,
        route_bearing_degrees=-90.0,
        route_source="visual_survey",
        visual_review_bearing_degrees=-90.0,
    )

    assert decision.selected_direction == "left"
    left = next(item for item in decision.candidates if item.direction == "left")
    assert left.eligible is True
    assert "visual_review_capsule_override" in left.sources


def test_fresh_capsule_block_overrides_line_clearance():
    state = state_with_directions({
        "forward": {
            "clear_distance_m": 12.0,
            "hit": False,
            "confidence": "high",
            "capsule_probe": {
                "available": True,
                "fresh": True,
                "sample_sequence": 11,
                "hit": True,
                "blocking_hit": True,
            },
        },
        "right": {"clear_distance_m": 12.0, "hit": False, "confidence": "high"},
    })
    state.navigation["base_sequence"] = 11

    decision = fuse_direction_evidence(state, goal_bearing_degrees=0.0)

    assert decision.selected_direction == "right"
    forward = next(item for item in decision.candidates if item.direction == "forward")
    assert forward.eligible is False
    assert "capsule" in forward.reason


def test_distant_capsule_endpoint_does_not_veto_bounded_short_segment():
    state = state_with_directions({
        "forward": {
            "clear_distance_m": 12.0,
            "hit": False,
            "confidence": "high",
            "capsule_probe": {
                "available": True,
                "fresh": True,
                "sample_sequence": 21,
                "hit": True,
                "blocking_hit": True,
                "clear_distance_m": 23.5,
            },
        },
        "right": {"clear_distance_m": 12.0, "hit": False, "confidence": "high"},
    })
    state.navigation["base_sequence"] = 21

    decision = fuse_direction_evidence(
        state,
        goal_bearing_degrees=0.0,
        movement_horizon_m=2.25,
    )

    assert decision.selected_direction == "forward"
    forward = next(item for item in decision.candidates if item.direction == "forward")
    assert forward.eligible is True
    assert forward.reason == "locally clear candidate"


def test_bounded_capsule_clearance_requires_distance_beyond_short_horizon():
    capsule = {
        "available": True,
        "fresh": True,
        "hit": True,
        "blocking_hit": True,
        "clear_distance_m": 23.5,
    }

    assert _bounded_capsule_clearance_m(capsule, 2.25) == pytest.approx(23.5)
    assert _bounded_capsule_clearance_m(capsule, 23.5) is None
    assert _bounded_capsule_clearance_m(None, 2.25) is None


def test_capsule_sequence_mismatch_cannot_override_line_trace():
    state = state_with_directions({
        "forward": {
            "clear_distance_m": 3.8,
            "hit": True,
            "capsule_probe": {
                "available": True,
                "fresh": True,
                "sample_sequence": 9,
                "hit": True,
                "blocking_hit": False,
            },
        },
    })
    state.navigation["base_sequence"] = 10

    forward = state.navigation["directions"]["forward"]
    assert fresh_capsule_probe(state.navigation, forward) is None
    decision = fuse_direction_evidence(state, goal_bearing_degrees=0.0)
    candidate = next(item for item in decision.candidates if item.direction == "forward")
    assert candidate.eligible is False


def test_no_clearance_returns_perception_required():
    state = state_with_directions({"forward": {"confidence": "unknown"}})

    decision = fuse_direction_evidence(state, goal_bearing_degrees=0.0)

    assert decision.outcome == "blocked_or_uncertain"
    assert decision.selected_direction is None


def test_distant_line_trace_hit_does_not_invalidate_short_segment():
    navigation = {
        "base_sequence": 10,
        "directions": {},
    }
    distant_hit = {
        "clear_distance_m": 8.5,
        "hit": True,
        "blocking_hit": True,
    }
    near_hit = {
        "clear_distance_m": 2.0,
        "hit": True,
        "blocking_hit": True,
    }

    assert _navigation_obstacle_within_horizon(navigation, distant_hit, 3.0) is False
    assert _navigation_obstacle_within_horizon(navigation, near_hit, 3.0) is True


def test_long_hold_is_bounded_by_capsule_horizon_not_line_trace_horizon():
    capsule = {
        "available": True,
        "fresh": True,
        "hit": False,
        "corridor_confirmed": True,
        "clear_distance_m": 6.0,
        "sample_sequence": 4,
    }

    assert _verified_corridor_clearance_m(capsule, "line_trace_surface_normal", 40.0) == 6.0


def test_walkable_capsule_surface_hit_authorizes_bounded_hold():
    capsule = {
        "available": True,
        "fresh": True,
        "hit": True,
        "blocking_hit": False,
        "walkable_surface_hit": True,
        "corridor_confirmed": False,
        "clear_distance_m": 10.5,
        "sample_sequence": 5,
    }

    assert _verified_corridor_clearance_m(
        capsule, "line_trace_surface_normal", 40.0
    ) == pytest.approx(10.5)


def test_long_walkability_probe_can_authorize_long_hold():
    assert _verified_corridor_clearance_m(
        None,
        "navmesh_corridor",
        40.0,
    ) == 40.0


def test_cross_track_distance_uses_horizontal_route_segment():
    assert cross_track_distance_m(
        Vec3(500, 300, 999), Vec3(0, 0, 0), Vec3(1000, 0, 0)
    ) == pytest.approx(3.0)


def test_coordinator_module_has_no_human_recording_dependency():
    source = (Path(__file__).parents[1] / "src" / "ff7r_agent" / "evidence_fusion.py").read_text(encoding="utf-8")
    assert "HumanTraversal" not in source
    assert "human_recording" not in source
    assert "human-history" not in source


def test_coordinator_requires_explicit_visual_review_before_moving_from_frame():
    parameters = inspect.signature(AgentService.navigate_next_segment).parameters
    assert "visual_reviewed" in parameters
    assert parameters["visual_reviewed"].default is False
    assert "visual_review" in parameters
    assert parameters["visual_review"].default is None


def test_camera_survey_fails_closed_when_telemetry_yaw_does_not_change():
    service = AgentService(SimulationTelemetry(), SimulationInput())

    result = service.survey_camera(60.0, 80)

    assert result["ok"] is False
    assert result["observed_turn_degrees"] is None
    assert result["reason"] == "camera yaw telemetry is unavailable for survey verification"


def test_camera_survey_does_not_resend_after_motion_without_settle():
    class DelayedSettlingTelemetry:
        def __init__(self):
            self.sequence = 0
            self.yaw = 0.0
            self.after_input = False
            self.samples_after_input = 0

        def observe(self):
            self.sequence += 1
            if self.after_input:
                samples = (20.0, 40.0, 56.0, 57.0, 55.0, 57.0, 55.0, 57.0)
                self.yaw = samples[min(self.samples_after_input, len(samples) - 1)]
                self.samples_after_input += 1
            return state_with_camera_yaw(self.yaw, self.sequence)

    class CameraInput:
        is_real = False

        def __init__(self, telemetry):
            self.telemetry = telemetry
            self.look_calls = 0

        def look(self, _x, _y, _milliseconds):
            self.look_calls += 1
            self.telemetry.after_input = True

        def release_all(self):
            return None

    telemetry = DelayedSettlingTelemetry()
    input_backend = CameraInput(telemetry)
    clock = iter([float(index) * 0.2 for index in range(100)])
    service = AgentService(
        telemetry,
        input_backend,
        monotonic=lambda: next(clock),
        sleeper=lambda _seconds: None,
    )

    result = service.survey_camera(60.0, 80)

    assert result["ok"] is True
    assert input_backend.look_calls == 1
    assert result["observed_turn_degrees"] == pytest.approx(57.0)
    assert result["observations"][0]["verification_mode"] == "advanced_yaw_without_settle"


def state_with_camera_yaw(yaw, sequence=4):
    return GameState.from_dict({
        "schema_version": 1,
        "sequence": sequence,
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "game": {"running": True, "map_id": "Square", "location": "Town"},
        "player": {
            "name": "Cloud", "hp": 10, "max_hp": 10, "mp": 2, "max_mp": 2,
            "position": {"x": 0, "y": 0, "z": 0},
            "rotation": {"pitch": 0, "yaw": 0, "roll": 0},
        },
        "camera": {"rotation": {"pitch": 0, "yaw": yaw, "roll": 0}},
        "navigation": {"source": "simulation", "directions": {}},
    })


def test_structured_visual_review_rejects_dead_end_and_accepts_matching_frame(tmp_path):
    frame = tmp_path / "frame.bmp"
    frame.write_bytes(b"not-a-real-bmp-but-a-present-evidence-file")

    rejected, reason = AgentService._validate_visual_review({
        "evidence_path": str(frame),
        "corridor_clear": True,
        "route_continuity": True,
        "dead_end": True,
        "reviewed_bearing_degrees": 0.0,
        "camera_bearing_degrees": 0.0,
    }, 0.0)
    assert rejected is False
    assert "dead end" in reason

    accepted, reason = AgentService._validate_visual_review({
        "evidence_path": str(frame),
        "corridor_clear": True,
        "route_continuity": True,
        "dead_end": False,
        "reviewed_bearing_degrees": 20.0,
        "camera_bearing_degrees": 0.0,
    }, 0.0)
    assert accepted is True
    assert reason == "visual corridor review accepted"

    mismatched, reason = AgentService._validate_visual_review({
        "evidence_path": str(frame),
        "corridor_clear": True,
        "route_continuity": True,
        "dead_end": False,
        "reviewed_bearing_degrees": 15.0,
        "camera_bearing_degrees": 105.0,
    }, 15.0)
    assert mismatched is False
    assert "frame bearing" in reason


def test_coordinator_vetoes_movement_until_captured_frame_is_reviewed():
    class OneStateTelemetry:
        def observe(self):
            return state_with_directions({
                "forward": {"clear_distance_m": 12.0, "confidence": "high"},
            })

    service = AgentService(OneStateTelemetry(), SimulationInput())
    service.capture_game_window = lambda: {"ok": True, "image_path": "frame.bmp"}

    result = service.navigate_next_segment(
        target_position={"x": 100.0, "y": 0.0, "z": 0.0},
        survey=False,
        capture_screenshot=True,
    )

    assert result["decision"] == "visual_review_required"
    assert result["movement"] is None
    assert result["replan_required"] is True


def test_coordinator_captures_visual_evidence_when_all_corridors_are_blocked():
    class BlockedTelemetry:
        def observe(self):
            return state_with_directions({
                "forward": {"confidence": "unknown"},
                "backward": {"confidence": "unknown"},
                "left": {"confidence": "unknown"},
                "right": {"confidence": "unknown"},
            })

    service = AgentService(BlockedTelemetry(), SimulationInput())
    captured = []

    def capture():
        captured.append(True)
        return {"ok": True, "image_path": "blocker.bmp"}

    service.capture_game_window = capture

    result = service.navigate_next_segment(
        target_position={"x": 100.0, "y": 0.0, "z": 0.0},
        route_bearing_degrees=0.0,
        survey=False,
    )

    assert result["movement"] is None
    assert result["blocker_review"]["required"] is True
    assert result["blocker_review"]["detour_authorized"] is False
    assert result["visual_evidence"]["image_path"] == "blocker.bmp"
    assert captured == [True]
