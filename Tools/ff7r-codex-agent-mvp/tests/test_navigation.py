from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from ff7r_agent.models import GameState, Interactable, RelativePosition, Rotation, Vec3
from ff7r_agent.navigation import (
    KnownWorldStore,
    Landmark,
    Route,
    Waypoint,
    diagnose_navigation_stall,
    decide_navigation_step,
    navigation_candidates,
    plan_route,
)
from ff7r_agent.input import SimulationInput
from ff7r_agent.service import AgentService
from ff7r_agent.static_obstacles import NavigationSidecarLookahead, ObstacleEvidenceState
from ff7r_agent.telemetry import JsonFileTelemetry


def test_human_recordings_are_not_runtime_navigation_inputs():
    navigation_source = (
        Path(__file__).parents[1] / "src" / "ff7r_agent" / "navigation.py"
    ).read_text(encoding="utf-8")
    assert "HumanTraversalRecorder" not in navigation_source
    assert "human_recording" not in navigation_source
    assert "human-history" not in navigation_source


def snapshot():
    return {
        "schema_version": 1,
        "sequence": 10,
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "game": {
            "running": True,
            "map_id": "EndGame",
            "location": "Nibelheim",
        },
        "player": {
            "name": "Cloud", "hp": 10, "max_hp": 10,
            "mp": 2, "max_mp": 2,
            "position": {"x": 0, "y": 0, "z": 0},
            "rotation": {"pitch": 0, "yaw": 0, "roll": 0},
        },
        "camera": {
            "rotation": {"pitch": 2, "yaw": 15, "roll": 0},
        },
        "nearby_interactables": [
            {
                "id": "chadley",
                "kind": "npc",
                "distance": 6.0,
                "usable": True,
                "label": "Chadley",
                "position": {"x": 600, "y": 0, "z": 0},
            },
        ],
        "nearby_actors": [
            {
                "id": "guard",
                "kind": "npc",
                "name": "Village Guard",
                "distance": 8.0,
                "position": {"x": 0, "y": 800, "z": 0},
            },
        ],
    }


def test_camera_and_generic_spatial_lists_are_parsed():
    state = GameState.from_dict(snapshot())
    assert state.camera_rotation.yaw == 15
    assert [item.id for item in state.nearby_interactables] == ["chadley"]
    assert state.nearby_interactables[0].relative.horizontal_distance == pytest.approx(6.0)
    assert state.nearby_actors[0].relative.bearing_degrees == pytest.approx(90.0)


def test_navigation_candidates_prioritise_interactables_and_deduplicate():
    state = GameState.from_dict(snapshot())
    candidates = navigation_candidates(state)
    assert [item.target_id for item in candidates] == ["chadley", "guard"]
    assert candidates[0].label == "Chadley"


def test_target_position_gets_relative_data_at_navigation_time():
    value = snapshot()
    value["target"] = {
        "id": "selected-npc",
        "distance": 6.0,
        "position": {"x": 0, "y": 600, "z": 0},
    }
    state = GameState.from_dict(value)
    selected = next(
        item for item in navigation_candidates(state)
        if item.target_id == "selected-npc"
    )
    assert selected.relative is not None
    assert selected.relative.heading_error_degrees == pytest.approx(90.0)


def test_navigation_decision_turns_before_moving():
    target = navigation_candidates(GameState.from_dict(snapshot()))[0]
    decision = decide_navigation_step(target)
    assert decision.outcome == "move_forward"
    assert decision.movement_y == 1.0

    misaligned = target.__class__(
        **{
            **target.__dict__,
            "relative": RelativePosition(6, 0, 0, 6, 0, 25),
        }
    )
    decision = decide_navigation_step(misaligned)
    assert decision.outcome == "turn_camera"
    assert decision.movement_y == 0.0
    assert decision.camera_correction_degrees == 25


def test_route_plan_prefers_recorded_waypoint_and_marks_map_scale_unknown():
    state = GameState.from_dict(snapshot())
    target = navigation_candidates(state)[0]
    route = Route(
        "upper-square", "EndGame",
        (Waypoint("path-junction", "EndGame", Vec3(0, 300, 0)),),
    )
    plan = plan_route(state, target, [route])
    assert plan.path_preference == "known_route"
    assert plan.waypoint_ids == ("path-junction",)
    assert plan.expected_distance_m == pytest.approx(9.708, rel=1e-3)
    assert plan.straight_line_distance_m == pytest.approx(6.0)
    assert plan.map_scale_status == "unavailable"


def test_stall_diagnosis_prioritises_direct_reverse_then_lateral_escape():
    state = GameState.from_dict(snapshot())
    diagnosis = diagnose_navigation_stall(state, displacement_m=0.0)
    assert diagnosis.primary_action == "move_backward"
    assert [item["key"] for item in diagnosis.ranked_actions[:3]] == ["s", "a", "d"]
    assert diagnosis.requires_screenshot is True
    assert diagnosis.requires_map is True


def test_known_world_store_round_trips_landmarks_and_routes(tmp_path):
    store = KnownWorldStore(tmp_path / "known-world.json")
    landmark = Landmark(
        "nibelheim-chadley", "Chadley", "npc", "EndGame",
        Vec3(600, 0, 0), heading_yaw=90,
        evidence_refs=("screenshot.png",),
    )
    route = Route(
        "nibelheim-upper-square", "EndGame",
        (Waypoint("square-entry", "EndGame", Vec3(0, 0, 0)),),
    )
    store.upsert_landmark(landmark)
    store.upsert_route(route)
    assert store.list_landmarks("EndGame") == [landmark]
    assert store.list_routes("EndGame") == [route]


def test_supplemental_telemetry_is_sequence_bound(tmp_path):
    base = tmp_path / "state.json"
    sidecar = tmp_path / "surroundings.json"
    base.write_text(json.dumps(snapshot()), encoding="utf-8")
    sidecar.write_text(json.dumps({
        "base_sequence": 10,
        "nearby_actors": [{
            "id": "npc-sidecar", "kind": "npc", "distance": 4.0,
            "position": {"x": 400, "y": 0, "z": 0},
        }],
    }), encoding="utf-8")
    state = JsonFileTelemetry(base, supplemental_path=sidecar).observe()
    assert [item.id for item in state.nearby_actors] == ["npc-sidecar"]

    sidecar.write_text(json.dumps({
        "base_sequence": 9,
        "nearby_actors": [{"id": "stale", "distance": 1.0}],
    }), encoding="utf-8")
    state = JsonFileTelemetry(base, supplemental_path=sidecar).observe()
    assert [item.id for item in state.nearby_actors] == ["guard"]


def test_trusted_navigation_sidecar_is_sequence_skew_bound(tmp_path):
    base = tmp_path / "state.json"
    navigation = tmp_path / "navigation.json"
    value = snapshot()
    base.write_text(json.dumps(value), encoding="utf-8")
    navigation.write_text(json.dumps({
        "base_sequence": 153,
        "timestamp_utc": value["timestamp_utc"],
        "source": "ue4ss_line_trace_multi_height_capsule",
        "directions": {"forward": {"clear_distance_m": 12.0}},
    }), encoding="utf-8")
    state = JsonFileTelemetry(base, navigation_path=navigation).observe()
    assert state.navigation["directions"]["forward"]["clear_distance_m"] == 12.0


def test_exact_navigation_sidecar_remains_valid_for_any_source(tmp_path):
    base = tmp_path / "state.json"
    navigation = tmp_path / "navigation.json"
    value = snapshot()
    base.write_text(json.dumps(value), encoding="utf-8")
    navigation.write_text(json.dumps({
        "schema_version": 1,
        "base_sequence": 10,
        "source": "arbitrary_probe",
        "directions": {"forward": {"clear_distance_m": 12.0}},
    }), encoding="utf-8")

    state = JsonFileTelemetry(base, navigation_path=navigation).observe()

    assert state.navigation["directions"]["forward"]["clear_distance_m"] == 12.0
    assert state.navigation_diagnostics["reason"] == "navigation_exact_sequence_match"


def _trusted_navigation(base_value, *, sequence, timestamp=None, source="ue4ss_line_trace_multi_height_capsule", **extra):
    payload = {
        "schema_version": 1,
        "base_sequence": sequence,
        "timestamp_utc": timestamp or base_value["timestamp_utc"],
        "source": source,
        "directions": {"forward": {"clear_distance_m": 2.0}},
    }
    payload.update(extra)
    return payload


def test_trusted_navigation_live_observed_568_skew_uses_timestamp_binding(tmp_path):
    base = tmp_path / "state.json"
    navigation = tmp_path / "navigation.json"
    value = snapshot()
    base.write_text(json.dumps(value), encoding="utf-8")
    navigation.write_text(json.dumps(_trusted_navigation(value, sequence=578)), encoding="utf-8")

    state = JsonFileTelemetry(base, navigation_path=navigation).observe()

    assert state.navigation is not None
    assert state.navigation["base_sequence"] == 578
    assert state.navigation["_ff7r_navigation_binding"] == {
        "mode": "trusted_timestamp",
        "source": "ue4ss_line_trace_multi_height_capsule",
        "observed_sequence": 10,
        "sidecar_sequence": 578,
    }
    assert state.navigation_diagnostics["reason"] == "navigation_trusted_timestamp_match"
    assert state.navigation_diagnostics["sequence_delta"] == 568


def test_trusted_navigation_large_sequence_skew_is_allowed_only_by_timestamp(tmp_path):
    base = tmp_path / "state.json"
    navigation = tmp_path / "navigation.json"
    value = snapshot()
    base.write_text(json.dumps(value), encoding="utf-8")
    navigation.write_text(json.dumps(_trusted_navigation(value, sequence=100000)), encoding="utf-8")

    state = JsonFileTelemetry(base, navigation_path=navigation).observe()

    assert state.navigation is not None
    assert state.navigation_diagnostics["reason"] == "navigation_trusted_timestamp_match"


@pytest.mark.parametrize(
    ("timestamp", "reason"),
    [
        ("2025-12-31T23:59:58Z", "navigation_timestamp_stale"),
        ("2026-01-01T00:00:04Z", "navigation_timestamp_future"),
        ("not-a-timestamp", "navigation_timestamp_invalid"),
        (None, "navigation_timestamp_missing"),
    ],
)
def test_trusted_navigation_rejects_stale_future_or_invalid_timestamp(tmp_path, timestamp, reason):
    base = tmp_path / "state.json"
    navigation = tmp_path / "navigation.json"
    value = snapshot()
    value["timestamp_utc"] = "2026-01-01T00:00:01Z"
    base.write_text(json.dumps(value), encoding="utf-8")
    payload = _trusted_navigation(value, sequence=578, timestamp=timestamp)
    if timestamp is None:
        payload.pop("timestamp_utc")
    navigation.write_text(json.dumps(payload), encoding="utf-8")

    state = JsonFileTelemetry(base, navigation_path=navigation).observe()

    assert state.navigation is None
    assert state.navigation_diagnostics["reason"] == reason


def test_untrusted_navigation_with_live_observed_skew_is_rejected(tmp_path):
    base = tmp_path / "state.json"
    navigation = tmp_path / "navigation.json"
    value = snapshot()
    base.write_text(json.dumps(value), encoding="utf-8")
    navigation.write_text(json.dumps(_trusted_navigation(value, sequence=578, source="arbitrary_probe")), encoding="utf-8")

    state = JsonFileTelemetry(base, navigation_path=navigation).observe()

    assert state.navigation is None
    assert state.navigation_diagnostics["reason"] == "navigation_untrusted_source"


@pytest.mark.parametrize(
    ("base_extra", "navigation_extra", "reason"),
    [
        ({}, {"map_id": "OtherMap"}, "navigation_map_mismatch"),
        ({"world_id": "world-a"}, {"world_id": "world-b"}, "navigation_world_mismatch"),
        ({"session_id": "session-a"}, {"session_id": "session-b"}, "navigation_session_mismatch"),
    ],
)
def test_trusted_navigation_rejects_world_or_session_identity_mismatch(
    tmp_path, base_extra, navigation_extra, reason
):
    base = tmp_path / "state.json"
    navigation = tmp_path / "navigation.json"
    value = snapshot()
    value.update(base_extra)
    base.write_text(json.dumps(value), encoding="utf-8")
    navigation.write_text(
        json.dumps(_trusted_navigation(value, sequence=578, **navigation_extra)),
        encoding="utf-8",
    )

    state = JsonFileTelemetry(base, navigation_path=navigation).observe()

    assert state.navigation is None
    assert state.navigation_diagnostics["reason"] == reason


def test_accepted_trusted_navigation_reaches_capsule_lookahead(tmp_path):
    base = tmp_path / "state.json"
    navigation = tmp_path / "navigation.json"
    value = snapshot()
    value["camera"]["rotation"]["yaw"] = 0.0
    base.write_text(json.dumps(value), encoding="utf-8")
    payload = _trusted_navigation(value, sequence=578)
    payload["directions"] = {
        "forward": {
            "sample_sequence": 578,
            "clear_distance_m": 2.0,
            "hit": True,
            "capsule_probe": {
                "available": True,
                "fresh": True,
                "sample_sequence": 578,
                "blocking_hit": True,
                "clear_distance_m": 2.0,
                    "radius_m": 0.42,
            },
        }
    }
    navigation.write_text(json.dumps(payload), encoding="utf-8")

    state = JsonFileTelemetry(base, navigation_path=navigation).observe()
    lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=1)
    decision = lookahead.inspect(
        state.to_dict(), sequence=10, route_plan_sha256="trusted-568",
        desired_world_bearing_degrees=0.0, camera_world_yaw_degrees=0.0,
        horizon_m=3.0, force=True,
    )

    assert state.navigation is not None
    assert decision.state == ObstacleEvidenceState.READY
    assert decision.reason_code == "lookahead_capsule_blocked"


def test_rejected_navigation_cannot_reach_capsule_lookahead(tmp_path):
    base = tmp_path / "state.json"
    navigation = tmp_path / "navigation.json"
    value = snapshot()
    base.write_text(json.dumps(value), encoding="utf-8")
    navigation.write_text(
        json.dumps(_trusted_navigation(value, sequence=578, timestamp="not-a-timestamp")),
        encoding="utf-8",
    )

    state = JsonFileTelemetry(base, navigation_path=navigation).observe()
    lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=1)
    decision = lookahead.inspect(
        state.to_dict(), sequence=10, route_plan_sha256="rejected-568",
        desired_world_bearing_degrees=0.0, camera_world_yaw_degrees=0.0,
        horizon_m=3.0, force=True,
    )

    assert state.navigation is None
    assert decision.state == ObstacleEvidenceState.UNKNOWN
    assert decision.reason_code == "lookahead_source_untrusted"


def test_global_route_sidecar_requires_exact_sequence(tmp_path):
    base = tmp_path / "state.json"
    route = tmp_path / "navigation-path.json"
    base.write_text(json.dumps(snapshot()), encoding="utf-8")
    route.write_text(json.dumps({
        "base_sequence": 9,
        "source": "ue4ss_navmesh_path",
        "target_id": "chadley",
        "path_complete": True,
        "navmesh_path": [{"x": 0, "y": 0, "z": 0}, {"x": 600, "y": 0, "z": 0}],
    }), encoding="utf-8")
    state = JsonFileTelemetry(base, navigation_route_path=route).observe()
    assert state.navigation is None


def test_route_plan_consumes_complete_runtime_path_and_rejects_direct_fallback():
    value = snapshot()
    value["navigation"] = {
        "target_id": "chadley",
        "source": "ue4ss_navmesh_path",
        "path_complete": True,
        "navmesh_path": [{"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 300, "z": 0}, {"x": 600, "y": 0, "z": 0}],
    }
    state = GameState.from_dict(value)
    target = navigation_candidates(state)[0]
    plan = plan_route(state, target)
    assert plan.path_preference == "navmesh_path"
    assert plan.route_source == "ue4ss_navmesh_path"
    assert plan.waypoint_ids == ("path-2",)
    assert plan.legs[0]["from_position"] == {"x": 0, "y": 0, "z": 0}
    assert plan.legs[0]["to_position"] == {"x": 0, "y": 300, "z": 0}

    value["navigation"] = None
    target_only = navigation_candidates(GameState.from_dict(value))[0]
    assert plan_route(GameState.from_dict(value), target_only).path_preference == "route_required"


def test_partial_runtime_path_is_rejected():
    value = snapshot()
    value["navigation"] = {
        "target_id": "chadley",
        "source": "ue4ss_navmesh_path",
        "path_complete": False,
        "partial": True,
        "navmesh_path": [{"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 300, "z": 0}],
    }
    state = GameState.from_dict(value)
    plan = plan_route(state, navigation_candidates(state)[0])
    assert plan.path_preference == "route_required"
    assert plan.route_source == "unavailable"


def test_runtime_path_with_wrong_target_id_is_rejected():
    value = snapshot()
    value["navigation"] = {
        "target_id": "different-target",
        "source": "ue4ss_navmesh_path",
        "path_complete": True,
        "navmesh_path": [{"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 300, "z": 0}],
    }
    state = GameState.from_dict(value)
    assert plan_route(state, navigation_candidates(state)[0]).path_preference == "route_required"


def test_runtime_path_with_non_finite_point_is_rejected():
    value = snapshot()
    value["navigation"] = {
        "target_id": "chadley",
        "source": "ue4ss_navmesh_path",
        "path_complete": True,
        "navmesh_path": [
            {"x": 0, "y": 0, "z": 0},
            {"x": "NaN", "y": 300, "z": 0},
            {"x": 600, "y": 0, "z": 0},
        ],
    }
    state = GameState.from_dict(value)
    assert plan_route(state, navigation_candidates(state)[0]).path_preference == "route_required"


def test_runtime_path_trims_to_current_position_and_preserves_next_turn():
    value = snapshot()
    value["player"]["position"] = {"x": 0, "y": 250, "z": 0}
    value["navigation"] = {
        "target_id": "chadley",
        "source": "ue4ss_navmesh_path",
        "path_complete": True,
        "navmesh_path": [
            {"x": 0, "y": 0, "z": 0},
            {"x": 0, "y": 500, "z": 0},
            {"x": 500, "y": 500, "z": 0},
            {"x": 600, "y": 0, "z": 0},
        ],
    }
    state = GameState.from_dict(value)
    plan = plan_route(state, navigation_candidates(state)[0])
    assert plan.path_preference == "navmesh_path"
    assert plan.waypoint_ids == ("path-2", "path-3")
    assert plan.legs[0]["bearing_degrees"] == pytest.approx(90.0)
    assert plan.legs[1]["bearing_degrees"] != plan.legs[0]["bearing_degrees"]


def test_route_evidence_disappearance_fails_closed():
    value = snapshot()
    value["navigation"] = {
        "target_id": "chadley",
        "source": "ue4ss_navmesh_path",
        "path_complete": False,
    }
    state = GameState.from_dict(value)
    plan = plan_route(state, navigation_candidates(state)[0])
    assert plan.path_preference == "route_required"
    assert plan.route_source == "unavailable"


def test_route_plan_can_consume_occupancy_grid_astar():
    value = snapshot()
    value["navigation"] = {
        "target_id": "chadley",
        "source": "ue4ss_occupancy_grid",
        "occupancy_grid": {
            "cell_size_m": 1.0,
            "origin": {"x": 0, "y": 0, "z": 0},
            "width": 7,
            "height": 4,
            "blocked": [[1, 0], [1, 1], [1, 2]],
        },
    }
    state = GameState.from_dict(value)
    plan = plan_route(state, navigation_candidates(state)[0])
    assert plan.path_preference == "occupancy_grid_astar"
    assert plan.route_source == "occupancy_grid_astar"
    assert plan.expected_distance_m is not None and plan.expected_distance_m > plan.straight_line_distance_m


def test_service_context_and_landmark_recording_are_read_only_to_game(tmp_path):
    base = tmp_path / "state.json"
    base.write_text(json.dumps(snapshot()), encoding="utf-8")
    store = KnownWorldStore(tmp_path / "known-world.json")
    service = AgentService(
        JsonFileTelemetry(base), SimulationInput(), known_world_store=store
    )
    context = service.navigation_context(target_id="chadley")
    assert context["selected_target_id"] == "chadley"
    assert context["decision"]["outcome"] == "move_forward"
    saved = service.remember_landmark(
        "nibelheim-chadley", "Chadley", target_id="chadley",
        evidence_ref="upper-square.png",
    )
    assert saved["ok"] is True
    assert store.list_landmarks("EndGame")[0].position == Vec3(600, 0, 0)
    assert store.list_landmarks("EndGame")[0].metadata["target_id"] == "chadley"


def test_service_resolves_known_landmark_when_target_is_not_nearby(tmp_path):
    base = tmp_path / "state.json"
    value = snapshot()
    value["nearby_interactables"] = []
    value["nearby_actors"] = []
    base.write_text(json.dumps(value), encoding="utf-8")
    store = KnownWorldStore(tmp_path / "known-world.json")
    store.upsert_landmark(Landmark(
        "known-bench", "Known bench", "bench", "EndGame", Vec3(600, 0, 0),
        interaction_position=Vec3(600, 0, 0),
    ))
    service = AgentService(
        JsonFileTelemetry(base), SimulationInput(), known_world_store=store
    )

    context = service.navigation_context(target_id="known-bench")

    assert context["candidates"][0]["target_id"] == "known-bench"
    assert context["candidates"][0]["metadata"]["known_landmark"] is True
    assert context["selected_target_id"] == "known-bench"
    assert context["decision"]["outcome"] == "move_forward"
    assert context["decision"]["distance_m"] == pytest.approx(6.0)


def test_service_does_not_use_persisted_routes_at_runtime_by_default(tmp_path):
    base = tmp_path / "state.json"
    base.write_text(json.dumps(snapshot()), encoding="utf-8")
    store = KnownWorldStore(tmp_path / "known-world.json")
    store.upsert_route(Route(
        "historical-route", "EndGame",
        (Waypoint("historical-junction", "EndGame", Vec3(0, 300, 0)),),
    ))
    service = AgentService(
        JsonFileTelemetry(base), SimulationInput(), known_world_store=store
    )

    result = service.plan_navigation_route(target_id="chadley")

    assert result["route_plan"]["path_preference"] == "route_required"
    assert result["route_plan"]["route_source"] == "unavailable"


def test_service_can_explicitly_opt_in_to_persisted_routes(tmp_path):
    base = tmp_path / "state.json"
    base.write_text(json.dumps(snapshot()), encoding="utf-8")
    store = KnownWorldStore(tmp_path / "known-world.json")
    store.upsert_route(Route(
        "historical-route", "EndGame",
        (Waypoint("historical-junction", "EndGame", Vec3(0, 300, 0)),),
    ))
    service = AgentService(
        JsonFileTelemetry(base), SimulationInput(), known_world_store=store,
        use_recorded_routes=True,
    )

    result = service.plan_navigation_route(target_id="chadley")

    assert result["route_plan"]["path_preference"] == "known_route"
    assert result["route_plan"]["route_source"] == "known_world"
