from __future__ import annotations

import json
from pathlib import Path
import unittest

from route_following_simulator import (
    ExecutionState,
    SimulatorConfig,
    RouteFollower,
    replay_file,
    validate_route_plan,
)


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "fixtures"


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def replay(name: str):
    return replay_file(FIXTURES / "replays" / f"{name}.json")


def events(timeline, event_name: str):
    return [item for item in timeline if item["event"] == event_name]


class SimulatorTests(unittest.TestCase):
    def test_complete_planner_shape_is_consumed_without_production_imports(self):
        plan = load_json(FIXTURES / "route_plan_example.json")
        normalized = validate_route_plan(plan)

        self.assertEqual(normalized.start_ref, 1001)
        self.assertEqual(normalized.end_ref, 1004)
        self.assertEqual(normalized.polygon_corridor, (1001, 1002, 1003, 1004))
        self.assertEqual(len(normalized.portals), 2)
        self.assertEqual(normalized.waypoints[-1].x, 1000.0)
        self.assertFalse(plan["movement_sent"])

    def test_required_synthetic_scenarios(self):
        scenarios = (
            ("perfect_traversal", ExecutionState.ARRIVED.value, "destination_reached"),
            ("noisy_success", ExecutionState.ARRIVED.value, "destination_reached"),
            ("already_at_first_waypoint", ExecutionState.ARRIVED.value, "waypoint_reached"),
            ("overshoot_waypoint", ExecutionState.ARRIVED.value, "waypoint_overshot"),
            ("stationary_stall", ExecutionState.STALLED.value, "stall_detected"),
            ("wrong_direction", ExecutionState.STALLED.value, "stall_detected"),
            ("large_deviation", ExecutionState.REPLAN_REQUIRED.value, "replan_required"),
            ("pause_then_recover", ExecutionState.ARRIVED.value, "destination_reached"),
            ("waypoint_oscillation", ExecutionState.STALLED.value, "oscillation_detected"),
            ("invalid_telemetry", ExecutionState.FAILED.value, "telemetry_invalid"),
            ("external_invalidation", ExecutionState.REPLAN_REQUIRED.value, "plan_invalidated"),
            ("final_arrival", ExecutionState.ARRIVED.value, "destination_reached"),
        )
        for fixture, terminal_state, required_event in scenarios:
            with self.subTest(fixture=fixture):
                timeline = replay(fixture)
                self.assertEqual(timeline[-1]["state"], terminal_state)
                self.assertTrue(events(timeline, required_event))

    def test_sharp_corner_pass_through_consumes_only_the_missed_intermediate_waypoint(self):
        timeline = replay("sharp_corner_pass_through")
        self.assertEqual(timeline[-1]["state"], ExecutionState.ARRIVED.value)
        passed = events(timeline, "waypoint_pass_through_proven")
        self.assertEqual([item["passed_waypoint_index"] for item in passed], [1])
        self.assertEqual(passed[0]["hard_capture_radius_m"], 0.5)
        self.assertGreater(passed[0]["distance_to_waypoint_m"], 0.5)
        update = [item for item in events(timeline, "telemetry_update") if item["t"] == 2.0][-1]
        self.assertEqual(update["waypoint_index"], 2)
        self.assertGreater(update["desired_heading_deg"], 89.0)
        self.assertLess(update["desired_heading_deg"], 92.0)
        self.assertEqual(
            [item["waypoint_index"] for item in timeline if item["event"] == "telemetry_update"],
            [1, 1, 2, 3, 3],
        )

    def test_sharp_corner_pass_through_rejects_pre_corner_lateral_and_backward_samples(self):
        plan = load_json(FIXTURES / "route_plan_example.json")
        plan["requested_end"] = {"x": 1000, "y": 500, "z": 0}
        plan["resolved_end"] = {"x": 1000, "y": 500, "z": 0}
        plan["waypoints"] = [
            {"x": 0, "y": 0, "z": 0},
            {"x": 500, "y": 0, "z": 0},
            {"x": 500, "y": 500, "z": 0},
            {"x": 1000, "y": 500, "z": 0},
        ]
        plan["total_estimated_path_length_m"] = 15

        before_corner = RouteFollower(plan)
        before_corner.on_telemetry(
            {"t": 0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        before_corner.on_telemetry(
            {"t": 0.5, "position": {"x": 400, "y": 0, "z": 0}, "heading": 0}
        )
        events_before = before_corner.on_telemetry(
            {"t": 1, "position": {"x": 450, "y": -30, "z": 0}, "heading": 0}
        )
        self.assertEqual(before_corner.current_waypoint_index, 1)
        self.assertFalse(events(events_before, "waypoint_pass_through_proven"))

        lateral = RouteFollower(plan)
        lateral.on_telemetry(
            {"t": 0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        lateral.on_telemetry(
            {"t": 0.5, "position": {"x": 400, "y": 0, "z": 0}, "heading": 0}
        )
        events_lateral = lateral.on_telemetry(
            {"t": 1, "position": {"x": 650, "y": 200, "z": 0}, "heading": 45}
        )
        self.assertEqual(lateral.current_waypoint_index, 1)
        self.assertFalse(events(events_lateral, "waypoint_pass_through_proven"))

        backwards = RouteFollower(plan)
        backwards.on_telemetry(
            {"t": 0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        backwards.on_telemetry(
            {"t": 0.5, "position": {"x": 500, "y": 100, "z": 0}, "heading": 90}
        )
        events_backwards = backwards.on_telemetry(
            {"t": 1, "position": {"x": 500, "y": 60, "z": 0}, "heading": 270}
        )
        self.assertEqual(backwards.current_waypoint_index, 1)
        self.assertFalse(events(events_backwards, "waypoint_pass_through_proven"))

    def test_pass_through_requires_one_bounded_adjacent_corner_crossing(self):
        plan = load_json(FIXTURES / "route_plan_example.json")
        plan["requested_end"] = {"x": 1000, "y": 500, "z": 0}
        plan["resolved_end"] = {"x": 1000, "y": 500, "z": 0}
        plan["waypoints"] = [
            {"x": 0, "y": 0, "z": 0},
            {"x": 500, "y": 0, "z": 0},
            {"x": 500, "y": 500, "z": 0},
            {"x": 1000, "y": 500, "z": 0},
        ]
        plan["total_estimated_path_length_m"] = 15

        follower = RouteFollower(plan)
        follower.on_telemetry(
            {"t": 0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        follower.on_telemetry(
            {"t": 1, "position": {"x": 400, "y": 0, "z": 0}, "heading": 0}
        )
        too_far = follower.on_telemetry(
            {"t": 2, "position": {"x": 500, "y": 400, "z": 0}, "heading": 90}
        )
        self.assertEqual(follower.current_waypoint_index, 1)
        self.assertFalse(events(too_far, "waypoint_pass_through_proven"))

        follower = RouteFollower(plan)
        follower.on_telemetry(
            {"t": 0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        follower.on_telemetry(
            {"t": 1, "position": {"x": 400, "y": 0, "z": 0}, "heading": 0}
        )
        bounded = follower.on_telemetry(
            {"t": 2, "position": {"x": 505, "y": 80, "z": 0}, "heading": 60}
        )
        proof = events(bounded, "waypoint_pass_through_proven")
        self.assertEqual(len(proof), 1)
        self.assertEqual(proof[0]["authorized_route_segment_indices"], [0, 1])
        self.assertLessEqual(proof[0]["outgoing_projection_t"], 1.0)

    def test_pass_through_never_applies_to_final_destination(self):
        plan = load_json(FIXTURES / "route_plan_example.json")
        follower = RouteFollower(plan)
        follower.on_telemetry(
            {"t": 0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        timeline = follower.on_telemetry(
            {"t": 1, "position": {"x": 1080, "y": 0, "z": 0}, "heading": 0}
        )
        self.assertFalse(events(timeline, "waypoint_pass_through_proven"))
        self.assertNotEqual(timeline[-1]["state"], ExecutionState.ARRIVED.value)

    def test_waypoint_indices_are_monotonic_and_overshoot_is_safe(self):
        timeline = replay("overshoot_waypoint")
        indices = [item["waypoint_index"] for item in timeline if item["waypoint_index"] is not None]

        self.assertEqual(indices, sorted(indices))
        overshoot = events(timeline, "waypoint_overshot")
        self.assertEqual(overshoot[0]["overshot_waypoint_index"], 1)
        self.assertEqual(overshoot[0]["waypoint_index"], 1)
        self.assertTrue(
            any(item["active_waypoint_index"] == 2 for item in events(timeline, "waypoint_advanced"))
        )

    def test_already_satisfied_start_advances_without_regression(self):
        timeline = replay("already_at_first_waypoint")
        reached = events(timeline, "waypoint_reached")
        self.assertEqual(reached[0]["reached_waypoint_index"], 0)
        self.assertEqual(reached[-1]["reached_waypoint_index"], 2)
        self.assertEqual(
            [item["waypoint_index"] for item in timeline if item["event"] == "telemetry_update"],
            [1, 2, 2],
        )

    def test_route_deviation_uses_active_route_geometry(self):
        timeline = replay("large_deviation")
        deviation = events(timeline, "route_deviation_detected")
        self.assertTrue(deviation)
        self.assertGreaterEqual(deviation[-1]["cross_track_error_m"], 8.0)
        self.assertEqual(timeline[-1]["state"], ExecutionState.REPLAN_REQUIRED.value)

    def test_final_arrival_requires_final_radius_and_never_uses_overshoot(self):
        exact = replay("final_arrival")
        near = replay("final_near_arrival")
        self.assertEqual(exact[-1]["state"], ExecutionState.ARRIVED.value)
        self.assertEqual(near[-1]["state"], ExecutionState.ARRIVED.value)
        self.assertLessEqual(near[-1]["distance_to_final_destination_m"], 0.75)

        for fixture in ("final_slight_overshoot", "final_large_overshoot"):
            with self.subTest(fixture=fixture):
                timeline = replay(fixture)
                self.assertNotIn(ExecutionState.ARRIVED.value, [item["state"] for item in timeline])
                self.assertFalse(events(timeline, "destination_reached"))
                self.assertEqual(timeline[-1]["state"], ExecutionState.FOLLOWING.value)


    def test_stationary_and_wrong_direction_require_elapsed_time_and_no_progress(self):
        for fixture in ("stationary_stall", "wrong_direction"):
            with self.subTest(fixture=fixture):
                timeline = replay(fixture)
                stall = events(timeline, "stall_detected")[-1]
                self.assertEqual(stall["stall_reason"], "no_meaningful_progress")
                self.assertGreaterEqual(stall["elapsed_since_meaningful_progress_s"], 5.0)

    def test_slow_progress_and_temporary_pause_recover_before_stall(self):
        timeline = replay("slow_legitimate_progress")
        self.assertFalse(events(timeline, "stall_detected"))
        self.assertTrue(events(timeline, "waypoint_reached"))

        recovered = replay("pause_then_recover")
        self.assertFalse(events(recovered, "stall_detected"))
        self.assertEqual(recovered[-1]["state"], ExecutionState.ARRIVED.value)

    def test_oscillation_is_detected_without_switching_waypoints(self):
        timeline = replay("waypoint_oscillation")
        detected = events(timeline, "oscillation_detected")
        self.assertEqual(detected[-1]["stall_reason"], "waypoint_oscillation")
        self.assertGreaterEqual(detected[-1]["oscillation_crossings"], 3)
        self.assertEqual(max(item["waypoint_index"] for item in timeline), 1)

    def test_oscillation_episode_resets_after_meaningful_recovery(self):
        timeline = replay_file(
            FIXTURES / "replays" / "oscillation_recovery.json",
            config=SimulatorConfig(waypoint_arrival_radius_m=0.1),
        )
        detected = events(timeline, "oscillation_detected")
        self.assertEqual(len(detected), 2)
        self.assertEqual([item["t"] for item in detected], [5.0, 10.0])
        self.assertEqual([item["waypoint_index"] for item in detected], [1, 1])
        self.assertEqual(detected[0]["state"], ExecutionState.STALLED.value)
        self.assertEqual(detected[1]["state"], ExecutionState.STALLED.value)
        recovered = events(timeline, "stall_recovered")
        self.assertEqual([item["t"] for item in recovered], [6.0])
        self.assertEqual(
            [item["state"] for item in recovered],
            [ExecutionState.FOLLOWING.value],
        )
        self.assertEqual(timeline[-1]["state"], ExecutionState.STALLED.value)
        self.assertEqual(max(item["waypoint_index"] for item in timeline), 1)

    def test_expired_oscillation_crossings_do_not_remain_cumulative(self):
        timeline = replay_file(
            FIXTURES / "replays" / "oscillation_window_expiry.json",
            config=SimulatorConfig(oscillation_window_s=3.0),
        )
        self.assertFalse(events(timeline, "oscillation_detected"))
        self.assertFalse(events(timeline, "stall_detected"))
        self.assertEqual(timeline[-1]["state"], ExecutionState.FOLLOWING.value)

    def test_heading_and_speed_diagnostics_are_non_actuating(self):
        plan = load_json(FIXTURES / "route_plan_example.json")
        follower = RouteFollower(plan)
        follower.on_telemetry(
            {"t": 0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        timeline = follower.on_telemetry(
            {"t": 1, "position": {"x": 200, "y": 0, "z": 0}, "heading": 90}
        )
        update = events(timeline, "telemetry_update")[-1]
        self.assertEqual(update["desired_heading_deg"], 0.0)
        self.assertEqual(update["heading_error_deg"], -90.0)
        self.assertEqual(update["turn_direction"], "left")
        self.assertEqual(update["suggested_speed_band"], "normal")
        self.assertFalse(any(key in update for key in ("key", "gamepad", "movement_command", "input")))

    def test_safety_pause_is_excluded_from_active_stall_budget(self):
        plan = load_json(FIXTURES / "route_plan_example.json")
        follower = RouteFollower(plan)
        follower.on_telemetry(
            {"t": 0.0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        follower.on_telemetry(
            {"t": 2.0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )

        follower.pause_for_safety(2.0)
        follower.resume_after_safety_pause(12.0)

        before_stall = follower.on_telemetry(
            {"t": 14.0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        self.assertFalse(events(before_stall, "stall_detected"))
        update = events(before_stall, "telemetry_update")[-1]
        self.assertAlmostEqual(update["elapsed_since_meaningful_progress_s"], 4.0)

        after_stall = follower.on_telemetry(
            {"t": 15.1, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        stall = events(after_stall, "stall_detected")[-1]
        self.assertGreaterEqual(stall["elapsed_since_meaningful_progress_s"], 5.0)

    def test_meaningful_progress_after_safety_pause_resets_active_stall_clock(self):
        plan = load_json(FIXTURES / "route_plan_example.json")
        follower = RouteFollower(plan)
        follower.on_telemetry(
            {"t": 0.0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        follower.on_telemetry(
            {"t": 2.0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        follower.pause_for_safety(2.0)
        follower.resume_after_safety_pause(12.0)

        progressed = follower.on_telemetry(
            {"t": 13.0, "position": {"x": 100, "y": 0, "z": 0}, "heading": 0}
        )
        self.assertFalse(events(progressed, "stall_detected"))
        accounting = follower.progress_accounting(13.0)
        self.assertEqual(accounting["last_meaningful_progress_t"], 13.0)
        self.assertEqual(accounting["active_stall_elapsed_seconds"], 0.0)

        later = follower.on_telemetry(
            {"t": 17.5, "position": {"x": 100, "y": 0, "z": 0}, "heading": 0}
        )
        self.assertFalse(events(later, "stall_detected"))

    def test_first_waypoint_accepts_bounded_vertical_start_discrepancy(self):
        plan = load_json(FIXTURES / "route_plan_example.json")
        follower = RouteFollower(plan)
        timeline = follower.on_telemetry(
            {"t": 0, "position": {"x": 0, "y": 0, "z": 94}, "heading": 125}
        )
        self.assertTrue(any(
            item.get("event") == "waypoint_reached" and item.get("reached_waypoint_index") == 0
            for item in timeline
        ))
        update = [item for item in timeline if item["event"] == "telemetry_update"][-1]
        self.assertEqual(update["waypoint_index"], 1)
        self.assertEqual(update["desired_heading_deg"], 0.0)

    def test_later_waypoint_accepts_floor_to_actor_vertical_offset_without_widening_horizontal_radius(self):
        plan = load_json(FIXTURES / "route_plan_example.json")
        follower = RouteFollower(plan)
        follower.on_telemetry(
            {"t": 0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        timeline = follower.on_telemetry(
            {"t": 1, "position": {"x": 500, "y": 0, "z": 140}, "heading": 0}
        )
        self.assertTrue(any(
            item.get("event") == "waypoint_reached" and item.get("reached_waypoint_index") == 1
            for item in timeline
        ))

        outside_horizontal_radius = follower.on_telemetry(
            {"t": 2, "position": {"x": 560, "y": 0, "z": 140}, "heading": 0}
        )
        self.assertFalse(any(
            item.get("event") == "waypoint_reached" and item.get("reached_waypoint_index") == 2
            for item in outside_horizontal_radius
        ))

    def test_final_arrival_uses_horizontal_radius_and_vertical_tolerance(self):
        plan = load_json(FIXTURES / "route_plan_example.json")

        accepted = RouteFollower(plan)
        accepted.on_telemetry(
            {"t": 0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        accepted_timeline = accepted.on_telemetry(
            {"t": 1, "position": {"x": 1000, "y": 0, "z": 140}, "heading": 0}
        )
        self.assertEqual(accepted_timeline[-1]["state"], ExecutionState.ARRIVED.value)

        outside_horizontal = RouteFollower(plan)
        outside_horizontal.on_telemetry(
            {"t": 0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        horizontal_timeline = outside_horizontal.on_telemetry(
            {"t": 1, "position": {"x": 1076, "y": 0, "z": 140}, "heading": 0}
        )
        self.assertNotEqual(horizontal_timeline[-1]["state"], ExecutionState.ARRIVED.value)

        outside_vertical = RouteFollower(plan)
        outside_vertical.on_telemetry(
            {"t": 0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0}
        )
        vertical_timeline = outside_vertical.on_telemetry(
            {"t": 1, "position": {"x": 1000, "y": 0, "z": 151}, "heading": 0}
        )
        self.assertNotEqual(vertical_timeline[-1]["state"], ExecutionState.ARRIVED.value)

    def test_unreal_yaw_zero_targeting_positive_y_is_right(self):
        timeline = replay("heading_yaw_convention")
        update = [item for item in events(timeline, "telemetry_update") if item["t"] == 1.0][-1]
        self.assertEqual(update["desired_heading_deg"], 90.0)
        self.assertEqual(update["heading_error_deg"], 90.0)
        self.assertEqual(update["turn_direction"], "right")

    def test_folded_later_leg_cannot_prove_active_progress_or_hide_deviation(self):
        replay_path = FIXTURES / "replays" / "folded_parallel_legs.json"
        timeline = replay_file(replay_path)
        update = [item for item in events(timeline, "telemetry_update") if item["t"] == 1.0][-1]
        self.assertIsNone(update["route_progress_fraction"])
        self.assertGreaterEqual(update["cross_track_error_m"], 10.0)
        self.assertTrue(events(timeline, "route_deviation_detected"))
        self.assertEqual(timeline[-1]["state"], ExecutionState.REPLAN_REQUIRED.value)

        payload = load_json(replay_path)
        follower = RouteFollower(
            payload["route_plan"],
            SimulatorConfig(deviation_confirmations=99, stall_timeout_s=5.0),
        )
        timeline_without_deviation_replan = []
        for sample in payload["telemetry"]:
            timeline_without_deviation_replan.extend(follower.on_telemetry(sample))
        self.assertTrue(events(timeline_without_deviation_replan, "stall_detected"))
        self.assertEqual(
            timeline_without_deviation_replan[-1]["state"], ExecutionState.STALLED.value
        )

    def test_close_waypoints_are_advanced_monotonically(self):
        timeline = replay("close_waypoints")
        self.assertEqual(timeline[-1]["state"], ExecutionState.ARRIVED.value)
        self.assertEqual(
            [item["reached_waypoint_index"] for item in events(timeline, "waypoint_reached")],
            [0, 1, 2],
        )

    def test_external_plan_invalidation_events_require_replan(self):
        plan = load_json(FIXTURES / "route_plan_example.json")
        for event_name in ("topology_changed", "plan_stale", "polygon_ref_invalid", "world_changed"):
            with self.subTest(event_name=event_name):
                follower = RouteFollower(plan)
                follower.on_telemetry({"t": 0, "position": {"x": 0, "y": 0, "z": 0}, "heading": 0})
                event = follower.process_external_event(event_name, 1.0)
                self.assertEqual(event["state"], ExecutionState.REPLAN_REQUIRED.value)
                self.assertEqual(event["invalidation_event"], event_name)

    def test_invalid_route_plan_fails_closed(self):
        timeline = replay("invalid_route_plan")
        self.assertEqual(timeline[-1]["state"], ExecutionState.FAILED.value)
        self.assertIn("movement_sent_must_be_false", timeline[-1]["issues"])

    def test_invalid_nonfinite_telemetry_fails_closed(self):
        timeline = replay("invalid_telemetry")
        failure = events(timeline, "telemetry_invalid")[-1]
        self.assertEqual(failure["state"], ExecutionState.FAILED.value)
        self.assertEqual(failure["telemetry_error"], "heading_non_finite")

    def test_timestamp_regression_is_invalid(self):
        timeline = replay("timestamp_regression")
        self.assertEqual(timeline[-1]["state"], ExecutionState.FAILED.value)
        self.assertEqual(timeline[-1]["telemetry_error"], "timestamp_regressed")


if __name__ == "__main__":
    unittest.main()
