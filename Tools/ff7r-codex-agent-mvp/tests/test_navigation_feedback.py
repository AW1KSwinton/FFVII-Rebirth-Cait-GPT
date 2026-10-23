import json
from dataclasses import replace
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

from ff7r_agent.detour_route import DetourPolygon, DetourPortal, DetourTopologyGraph, PolygonReference
from ff7r_agent.live_route_adapter import AgentServiceRenderEvidenceSource, LiveAdapterConfig, PlanAuthorization, _render_evidence_confirms_gameplay
from ff7r_agent.models import Vec3
from ff7r_agent.safety import WindowsGameEnvironment, game_process_snapshot
from test_live_route_adapter import make_adapter, make_observation, make_telemetry_backed_capture, FakeCaptureService


def recorded_transition():
    data = json.loads((Path(__file__).parent / "fixtures/observed-two-portal-transition.json").read_text())
    polygons = {}
    for raw in data["polygons"]:
        ref = PolygonReference.from_value(raw["ref"]["value"])
        polygons[ref.value] = DetourPolygon(ref, raw["tile_x"], raw["tile_y"], raw["layer"],
            tuple(Vec3(**v) for v in raw["vertices"]), raw["flags"], raw["area"], raw["type"], (), raw["identity_source"])
    adjacency = {}
    for raw in data["portals"]:
        portal = DetourPortal(raw["from_ref"], raw["to_ref"], raw["link_ref"], raw["source_edge"], raw["target_edge"],
            Vec3(**raw["left"]), Vec3(**raw["right"]), raw["side"])
        adjacency.setdefault(portal.from_reference, []).append(portal)
    graph = DetourTopologyGraph(polygons, adjacency, snapshot_diagnostics={})
    adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(route_mode="cached_topology"))
    plan = data["plan"]
    adapter.route_plan = plan
    adapter.topology_session = replace(adapter.topology.topology, graph=graph)
    adapter.authorization = PlanAuthorization(session_id="test", world_id="EndGame", map_id="EndGame",
        topology_fingerprint="test", route_plan_sha256="test", start_ref=plan["start_ref"], end_ref=plan["end_ref"],
        resolved_start=Vec3(**plan["resolved_start"]), resolved_end=Vec3(**plan["resolved_end"]),
        created_monotonic=0, route_mode="cached_topology")
    adapter._route_local_corridor_index = data["start_index"]
    adapter._route_local_corridor_ref = plan["polygon_corridor"][data["start_index"]]
    def sample(point):
        point = Vec3(**point)
        global_resolution = graph.resolve_point(point)
        return {"position": vars(point), "metrics": adapter._authorized_route_metrics(point),
            "corridor_resolution": graph.resolve_point_in_corridor(point, plan["polygon_corridor"], global_resolution=global_resolution)}
    return adapter, sample(data["previous"]), sample(data["current"])


def test_recorded_two_portal_crossing_is_accepted_with_full_interval_proof():
    adapter, previous, current = recorded_transition()
    result = adapter._route_local_sample_assessment(previous, current)
    assert result["safe"], result
    assert result["accepted_index"] == 9
    assert len(result["transition"]["crossings"]) == 2
    assert result["transition"]["interval_containment_proven"]


@pytest.mark.parametrize("fault", ["missing_portal", "changed_plan_portal", "outside_interval", "backwards", "overlap", "too_far"])
def test_multi_portal_proof_rejects_invalid_or_unbounded_crossings(fault):
    adapter, previous, current = recorded_transition()
    graph = adapter.topology_session.graph
    start = adapter._route_local_corridor_ref
    middle = adapter.route_plan["polygon_corridor"][8]
    if fault == "missing_portal":
        graph.adjacency[start] = ()
    elif fault == "changed_plan_portal":
        next(p for p in adapter.route_plan["portals"] if p["from_ref"] == start)["left"]["x"] += 100
    elif fault == "outside_interval":
        # Keep endpoints and portal intersections unchanged, but introduce a
        # hole in the traversed interval. Endpoint-only checks would accept.
        poly = graph.polygons[middle]
        graph.polygons[middle] = replace(poly, vertices=tuple(replace(v, x=v.x+1000) for v in poly.vertices))
    elif fault == "backwards":
        current["metrics"]["along_route_m"] = previous["metrics"]["along_route_m"] - 1
    elif fault == "overlap":
        resolution = current["corridor_resolution"]
        extra = replace(resolution.candidates[0], reference=middle, contained=True)
        current["corridor_resolution"] = replace(resolution, candidates=(*resolution.candidates, extra))
    elif fault == "too_far":
        previous["position"]["x"] -= 1000
    assert adapter._authorized_multi_portal_evidence(previous, current) is None


@pytest.mark.parametrize("raw_label", ["main_menu", "title_prompt"])
def test_brightness_only_menu_candidate_needs_measured_grounded_gameplay(raw_label):
    capture = make_telemetry_backed_capture(raw_label=raw_label)
    capture["startup_frame"]["classification_basis"] = "regional_brightness_heuristic"
    source = AgentServiceRenderEvidenceSource(FakeCaptureService(capture))
    observation = make_observation(position_reference="upright_capsule_bottom", grounded=True)
    evidence = source.observe(observation)
    assert evidence.ready
    # Preflight deliberately reacquires an advancing observation AFTER capture.
    # Reusing the same bound render proof must not require a future screenshot.
    assert _render_evidence_confirms_gameplay(replace(observation, sequence=capture["telemetry_sequence"]+1), evidence)
    for changes in ({"grounded": False}, {"grounded": None}, {"position_reference": "actor_origin"},
                    {"in_menu": True}, {"loading": True}, {"process_valid": False},
                    {"telemetry_age_seconds": .6}, {"sequence": capture["telemetry_sequence"]+1}):
        assert not source.observe(replace(observation, **changes)).ready
    capture["startup_frame"]["classification_basis"] = "recognized_menu_text"
    assert not source.observe(observation).ready


def test_process_snapshot_is_scoped_nested_and_returns_copies():
    first, second = WindowsGameEnvironment(), WindowsGameEnvironment()
    with patch.object(WindowsGameEnvironment, "_scan_game_processes", side_effect=[{1: "game"}, {2: "game"}, {3: "game"}]) as scan:
        with game_process_snapshot():
            first.game_process_ids().clear()
            with game_process_snapshot():
                assert second.game_process_ids() == {1: "game"}
            assert scan.call_count == 1
        assert second.game_process_ids() == {2: "game"}
        assert first.game_process_ids() == {3: "game"}


def test_process_snapshot_cleans_up_after_exception_and_isolates_threads():
    env = WindowsGameEnvironment()
    with patch.object(WindowsGameEnvironment, "_scan_game_processes", return_value={1: "game"}) as scan:
        with pytest.raises(RuntimeError), game_process_snapshot():
            env.game_process_ids()
            thread = Thread(target=env.game_process_ids)
            thread.start()
            thread.join()
            assert scan.call_count == 2
            raise RuntimeError("exit")
        env.game_process_ids()
        assert scan.call_count == 3


def test_airborne_feedback_waits_for_landing_and_replaces_old_corridor():
    observations = [make_observation(sequence=n, position_reference="upright_capsule_bottom", grounded=True)
                    for n in (1, 2, 3)]
    observations += [make_observation(sequence=n, position_reference="upright_capsule_bottom", grounded=False,
                                     controllable=False, position=Vec3(0, 300, 900)) for n in (4, 5)]
    observations += [make_observation(sequence=n, position_reference="upright_capsule_bottom", grounded=True,
                                     position=Vec3(0, 300, 0)) for n in (6, 7, 8)]
    adapter, _, actuator, _ = make_adapter(observations=observations,
        config=LiveAdapterConfig(route_mode="cached_topology", max_cycles=2, post_arrival_samples=0),
        route_target=Vec3(500, 0, 0))
    assert adapter.preflight()["movement_authorized"]
    old_hash = adapter.authorization.route_plan_sha256
    result = adapter.run(dry_run=False, max_cycles=2)
    events = result["timeline"]
    assert len(actuator.commands) == 2, result["reason"]
    assert any(e.get("event") == "post_command_airborne_observation" for e in events)
    assert any(e.get("event") == "readiness_replanned_and_independently_validated" for e in events)
    assert adapter.authorization.route_plan_sha256 != old_hash
    commands = [e for e in events if e.get("event") == "non_neutral_command_imminent"]
    assert commands[0]["sequence"] == 3
    assert commands[1]["sequence"] >= 7
    assert commands[1]["route_plan_sha256"] == adapter.authorization.route_plan_sha256
    assert adapter._route_local_corridor_index == 0
    assert actuator.is_neutral()


def test_capsule_fan_hit_beside_motion_does_not_block_clear_steering():
    from ff7r_agent.static_obstacles import StaticObstacleOverlay
    import math
    overlay = StaticObstacleOverlay()
    origin = Vec3(0, 0, 0)
    def end(degrees):
        return Vec3(300*math.cos(math.radians(degrees)), 300*math.sin(math.radians(degrees)), 0)
    args = dict(probe_bearing_degrees=86.822105, blocking_distance_m=1.8, capsule_radius_m=.35)
    assert overlay.capsule_hit_intersects_segment(origin, end(86.822105), **args)
    assert not overlay.capsule_hit_intersects_segment(origin, end(124.533761), **args)
    # Rotation-invariant and mirrored cases, including close contacts which
    # inflate across both directions and must remain blocking.
    for angle in (0, 90, 180, 270):
        for sign in (-1, 1):
            assert not overlay.capsule_hit_intersects_segment(origin, end(angle+sign*40),
                probe_bearing_degrees=angle, blocking_distance_m=1.8, capsule_radius_m=.35)
            assert overlay.capsule_hit_intersects_segment(origin, end(angle+sign*40),
                probe_bearing_degrees=angle, blocking_distance_m=.05, capsule_radius_m=.35)


@pytest.mark.parametrize("lost_sequence", [4, 5])
def test_readiness_loss_during_deferred_static_lookahead_recovers_without_replan(lost_sequence):
    observations = []
    for sequence in range(1, 10):
        raw = {}
        if sequence <= 3:
            raw = {"navigation": {"schema_version": 1, "source": "ue4ss_line_trace_multi_height_capsule",
                "base_sequence": sequence, "directions": {"forward": {"sample_sequence": sequence,
                "clear_distance_m": 1.0, "capsule_probe": {"available": True, "fresh": True,
                "sample_sequence": sequence, "blocking_hit": True, "clear_distance_m": 1.0, "radius_m": .42}}}}}
        observations.append(make_observation(sequence=sequence, raw=raw,
            position_reference="upright_capsule_bottom", grounded=True,
            controllable=sequence != lost_sequence, telemetry_gameplay_ready=sequence != lost_sequence))
    adapter, _, actuator, _ = make_adapter(observations=observations)
    from ff7r_agent.static_obstacles import NavigationSidecarLookahead
    adapter.lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=2, maximum_attempts_per_plan=2)
    assert adapter.preflight()["movement_authorized"]
    result = adapter.run(dry_run=False, max_cycles=3)
    events = result["timeline"]
    assert not any(e.get("event") == "static_obstacle_replanned_and_reauthorised" for e in events)
    assert any(e.get("event") in {"readiness_reacquired_route_state", "preflight_pose_settling"}
               for e in events), result["reason"]
    assert any(e.get("event") == "static_obstacle_precommand_blocker_deferred" for e in events)
    assert any(e.get("event") == "static_obstacle_short_pulse_not_evidence" for e in events)
    commands = [e for e in events if e.get("event") == "non_neutral_command_imminent"]
    assert commands and min(e["sequence"] for e in commands) >= 3
    assert actuator.is_neutral()


def test_preflight_waits_for_residual_grounded_motion_before_binding_route():
    positions = [0, 25, 50, 65, 70, 70]
    observations = [make_observation(sequence=i+1, position_reference="upright_capsule_bottom",
        grounded=True, position=Vec3(x, 0, 0), velocity_cm_s=Vec3(0, 0, 0))
        for i, x in enumerate(positions)]
    adapter, _, actuator, _ = make_adapter(observations=observations)
    report = adapter.preflight()
    assert report["movement_authorized"], report
    assert adapter.authorization.resolved_start.x == 70
    samples = [e for e in report["timeline"] if e.get("event") == "preflight_pose_settling"]
    assert not samples[0]["stable"] and samples[-1]["stable"]
    assert not actuator.commands and actuator.is_neutral()


@pytest.mark.parametrize("fault", ["moving", "repeated_sequence", "process_changed", "lease_lost"])
def test_preflight_settling_refuses_unstable_or_unowned_state(fault):
    observations = [make_observation(sequence=i+1, position_reference="upright_capsule_bottom",
        grounded=True, position=Vec3(i*30, 0, 0)) for i in range(5)]
    if fault == "repeated_sequence":
        observations[2:] = [observations[1]] * 3
    if fault == "process_changed":
        observations[2] = replace(observations[2], process_id=456)
    if fault == "lease_lost":
        observations[2] = replace(observations[2], lease_active=False)
    adapter, _, actuator, _ = make_adapter(observations=observations)
    report = adapter.preflight()
    assert not report["movement_authorized"]
    assert not actuator.commands and actuator.is_neutral()


def test_changed_start_retry_never_masks_other_gate_failures():
    from ff7r_agent.runtime_commands import _classify_navigation_route_result
    prefix = "final_pre_movement_gate_failed:"
    for reason in ("route_start_polygon_mismatch", "route_start_horizontal_mismatch;route_start_polygon_mismatch"):
        assert _classify_navigation_route_result({"reason": prefix+reason}) == "recoverable_route_divergence"
        assert _classify_navigation_route_result({"reason": prefix+reason+";movement_lease_missing"}) == "terminal_route_failure"
    assert _classify_navigation_route_result({"reason": prefix+"route_start_polygon_unresolvable"}) == "terminal_route_failure"


def test_readiness_proof_invalidated_after_settling_is_rebuilt_within_same_deadline():
    from test_live_route_adapter import FakeClock, TimedSettlingTelemetry
    clock = FakeClock()
    observations = [make_observation(sequence=n) for n in (1,2,3)]
    observations += [make_observation(sequence=n, controllable=False, telemetry_gameplay_ready=False) for n in (4,5)]
    observations += [make_observation(sequence=6, monotonic_t=clock.value+1.05)]
    telemetry = TimedSettlingTelemetry(observations, clock, wait_duration=.5)
    adapter, _, actuator, _ = make_adapter(telemetry=telemetry, clock=clock)
    assert adapter.preflight()["movement_authorized"]
    started = clock.value
    result = adapter.run(dry_run=False, max_cycles=2)
    assert len(telemetry.settle_calls) == 2, result["reason"]
    assert len(actuator.commands) == 2
    assert clock.value-started < adapter.config.readiness_recovery_timeout_seconds
    commands = [e for e in result["timeline"] if e.get("event") == "non_neutral_command_imminent"]
    assert [e["sequence"] for e in commands] == [3,6]
    assert actuator.is_neutral()


def test_exhausted_short_readiness_wait_gets_bounded_fresh_preflight():
    from ff7r_agent.runtime_commands import _run_navigation_objective, _classify_navigation_route_result
    for reason in ("readiness_recovery_timeout", "readiness_recovery_post_settle_observation_unavailable"):
        calls=[]
        def attempt():
            calls.append(1)
            return {"preflight":{"movement_authorized":True},"run":{
                "state":"STOPPED" if len(calls)==1 else "ARRIVED",
                "reason":reason if len(calls)==1 else "follower_arrived", "movement_cycles":0}}
        result = _run_navigation_objective(attempt,dry_run=False,objective_id="any-goal",target_position=None,max_recovery_attempts=1)
        assert len(calls)==2 and result["objective_state"]=="NAVIGATION_OBJECTIVE_COMPLETED"
    assert _classify_navigation_route_result({"reason":"actor_identity_changed"}) == "terminal_route_failure"
    assert _classify_navigation_route_result({"reason":"movement_lease_missing"}) == "terminal_route_failure"


def test_preflight_residual_pose_wait_rebuilds_invalidated_readiness():
    from test_live_route_adapter import FakeClock, TimedSettlingTelemetry
    clock = FakeClock()
    observations = [make_observation(sequence=i+1, monotonic_t=t,
        position_reference="upright_capsule_bottom", grounded=True,
        position=Vec3(x, 0, 0), controllable=ready, telemetry_gameplay_ready=ready)
        for i, (x, ready, t) in enumerate([
            (0, True, 10), (30, True, 10), (70, False, 10),
            (70, False, 10.5), (70, True, 11), (70, True, 11)])]
    telemetry = TimedSettlingTelemetry(observations, clock, wait_duration=.5)
    adapter, _, actuator, _ = make_adapter(telemetry=telemetry, clock=clock)
    report = adapter.preflight()
    assert report["movement_authorized"], report["reason"]
    assert len(telemetry.settle_calls) == 2
    samples = [e for e in report["timeline"] if e.get("event") == "preflight_pose_settling"]
    assert [e["sequence"] for e in samples if e["stable"]] == [6]
    assert adapter.authorization.resolved_start.x == 70
    assert clock.value - 10 < adapter.config.readiness_recovery_timeout_seconds
    assert not actuator.commands and actuator.is_neutral()


@pytest.mark.parametrize("fault", ["process", "lease", "actor", "deadline"])
def test_preflight_pose_readiness_wait_keeps_identity_and_time_bounds(fault):
    from test_live_route_adapter import FakeClock, TimedSettlingTelemetry
    clock = FakeClock()
    observations = [make_observation(sequence=i+1, position_reference="upright_capsule_bottom",
        grounded=True, position=Vec3(x, 0, 0)) for i,x in enumerate([0,30,70])]
    changes = {"controllable": False, "telemetry_gameplay_ready": False}
    if fault == "process": changes["process_id"] = 456
    if fault == "lease": changes["lease_active"] = False
    if fault == "actor": changes["actor_id"] = "different-actor"
    observations[-1] = replace(observations[-1], **changes)
    telemetry = TimedSettlingTelemetry(observations, clock, wait_duration=.5)
    adapter, _, actuator, _ = make_adapter(telemetry=telemetry, clock=clock)
    report = adapter.preflight()
    assert not report["movement_authorized"]
    assert not actuator.commands and actuator.is_neutral()
    assert clock.value - 10 <= adapter.config.readiness_recovery_timeout_seconds + 1e-6
    if fault != "deadline": assert not telemetry.settle_calls


@pytest.mark.parametrize("fault", [None, "stale_forever", "actor_change", "wrong_process"])
def test_preflight_reacquires_stale_render_without_reusing_its_authority(fault):
    observations = [make_observation(sequence=i+1, position_reference="upright_capsule_bottom",grounded=True)
                    for i in range(6)]
    if fault == "actor_change": observations[1] = replace(observations[1],actor_id="changed")
    class Capture:
        calls = 0
        def capture_game_window(self):
            self.calls += 1
            capture = make_telemetry_backed_capture(telemetry_age_seconds=2.5 if self.calls==1 or fault=="stale_forever" else .1)
            capture["telemetry_sequence"] = self.calls
            if fault == "wrong_process": capture["pid"] = 456
            return capture
    capture = Capture()
    adapter,_,actuator,_ = make_adapter(observations=observations,render=AgentServiceRenderEvidenceSource(capture))
    result = adapter.preflight()
    assert result["movement_authorized"] == (fault is None), result["reason"]
    assert capture.calls == (2 if fault is None else 3 if fault=="stale_forever" else 1)
    assert not actuator.commands and actuator.is_neutral()


@pytest.mark.parametrize("fault", [None, "process", "actor", "lease", "world", "deadline", "moving", "repeated"])
def test_static_replan_rebuilds_readiness_and_settles_before_reauthorizing(fault):
    from test_live_route_adapter import FakeClock, TimedSettlingTelemetry
    clock = FakeClock()
    observations = [make_observation(sequence=n, position_reference="upright_capsule_bottom",
        grounded=True) for n in (1, 2, 3)]
    # The post-obstacle observation is fresh and grounded, but its service
    # readiness proof was invalidated by a stale sample between reads.
    observations += [make_observation(sequence=4, position_reference="upright_capsule_bottom",
        grounded=True, controllable=False, telemetry_gameplay_ready=False)]
    observations += [make_observation(sequence=n, monotonic_t=10.5,
        position_reference="upright_capsule_bottom", grounded=True) for n in (5, 6, 7, 8)]
    changes = {"process": {"process_id": 456}, "actor": {"actor_id": "other"},
        "lease": {"lease_active": False}, "world": {"map_id": "other"}}
    if fault in changes:
        observations[4] = replace(observations[4], **changes[fault])
    elif fault == "deadline":
        observations[4:] = [replace(o, controllable=False, telemetry_gameplay_ready=False)
                            for o in observations[4:]]
    elif fault == "moving":
        observations[4:] = [replace(o, position=Vec3((i+1)*30, 0, 0))
                            for i, o in enumerate(observations[4:])]
    elif fault == "repeated":
        observations[4:] = [observations[3]] * 4
    telemetry = TimedSettlingTelemetry(observations, clock, wait_duration=.5)
    adapter, _, actuator, _ = make_adapter(telemetry=telemetry, clock=clock)
    assert adapter.preflight()["movement_authorized"]
    previous = adapter._require_observation()
    started = clock.value
    result = adapter._replan_for_static_obstacle(previous)
    assert not actuator.commands and actuator.is_neutral()
    assert clock.value - started <= adapter.config.readiness_recovery_timeout_seconds + 1e-6
    replans = [e for e in adapter.timeline if e.get("event") == "static_obstacle_replanned_and_reauthorised"]
    if fault is None:
        assert result is not None
        assert result.sequence == 6
        assert len(telemetry.settle_calls) == 1
        assert replans and replans[-1]["sequence"] == 6
        assert adapter.authorization_proof is not None
        assert adapter.validator_result["valid"]
    else:
        assert result is None
        assert not replans



def test_short_pulse_is_not_static_obstacle_evidence():
    adapter, _, actuator, _ = make_adapter()
    assert adapter.preflight()["movement_authorized"]
    pending = {
        "position_before": {"x": 0.0, "y": 0.0, "z": 0.0},
        "along_route_before_m": 0.0,
        "command_duration_ms": 150,
        "magnitude": 0.5,
        "desired_world_heading_deg": 0.0,
        "sequence": 10,
    }
    adapter._learn_static_obstacle_from_post_command(
        make_observation(sequence=11, monotonic_t=11.0),
        pending,
        {"along_route_m": 0.0},
        0.0,
    )
    assert adapter._static_no_progress_streak == 0
    assert adapter._static_last_learning is None
    assert any(e["event"] == "static_obstacle_short_pulse_not_evidence" for e in adapter.timeline)
    assert not actuator.commands


@pytest.mark.parametrize("progress_kind", ["displacement", "route_progress", "invalid"])
def test_adapter_progress_resets_only_unverified_blocker_accumulation(progress_kind):
    adapter, _, actuator, _ = make_adapter()
    assert adapter.preflight()["movement_authorized"]
    pending = {"position_before": {"x":0.,"y":0.,"z":0.}, "along_route_before_m":0.,
               "command_duration_ms":300, "magnitude":.6, "desired_world_heading_deg":0.}
    for sequence in (10,11):
        adapter._learn_static_obstacle_from_post_command(make_observation(sequence=sequence,monotonic_t=float(sequence)),
            {**pending,"sequence":sequence-1}, {"along_route_m":0.}, 0.)
    assert adapter._static_last_learning["evidence_count"] == 2
    displacement, along = {"displacement":(.3,0.), "route_progress":(0.,.3), "invalid":(float("nan"),float("nan"))}[progress_kind]
    adapter._learn_static_obstacle_from_post_command(make_observation(sequence=12,monotonic_t=12.),
        {**pending,"sequence":11}, {"along_route_m":along}, displacement)
    adapter._learn_static_obstacle_from_post_command(make_observation(sequence=13,monotonic_t=13.),
        {**pending,"sequence":12}, {"along_route_m":0.}, 0.)
    if progress_kind == "invalid":
        assert adapter._static_pending_discovery is None
        assert any(e["event"] == "static_obstacle_candidate_deferred_unverified" for e in adapter.timeline)
    else:
        assert adapter._static_pending_discovery is None
        assert adapter._static_last_learning["evidence_count"] == 1
        assert any(e["event"] == "static_obstacle_candidates_reset_for_progress" for e in adapter.timeline)
    assert not actuator.commands


@pytest.mark.parametrize("later_progress", [False, True])
def test_repeated_feedback_for_one_command_cannot_confirm_a_static_obstacle(later_progress):
    adapter, _, actuator, _ = make_adapter()
    assert adapter.preflight()["movement_authorized"]
    pending = {"position_before": {"x":0.,"y":0.,"z":0.}, "along_route_before_m":0.,
        "command_duration_ms":300, "magnitude":.8, "desired_world_heading_deg":0.}
    # Recorded command/sample relationships from the 0.8 live stop, expressed
    # in local geometry: 21860 was sampled twice before command 21865.
    for command, sequence in ((21860,21861),(21860,21865),(21865,21867)):
        adapter._learn_static_obstacle_from_post_command(
            make_observation(sequence=sequence,monotonic_t=float(sequence)),
            {**pending,"sequence":command}, {"along_route_m":0.}, 0.)
    assert adapter._static_pending_discovery is None
    assert adapter._static_last_learning["evidence_count"] == 2
    assert adapter._static_no_progress_streak == 2
    if later_progress:
        adapter._learn_static_obstacle_from_post_command(
            make_observation(sequence=21868,monotonic_t=21868.,position=Vec3(30.,0.,0.)),
            {**pending,"sequence":21865}, {"along_route_m":.3}, .3)
    adapter._learn_static_obstacle_from_post_command(
        make_observation(sequence=21870,monotonic_t=21870.),
        {**pending,"sequence":21869}, {"along_route_m":0.}, 0.)
    assert adapter._static_pending_discovery is None
    if not later_progress:
        assert any(e["event"] == "static_obstacle_candidate_deferred_unverified" for e in adapter.timeline)
    assert adapter._static_last_learning["evidence_count"] == (1 if later_progress else 3)
    assert not actuator.commands


def test_late_replay_or_missing_command_identity_does_not_add_blocker_evidence():
    adapter, _, actuator, _ = make_adapter()
    assert adapter.preflight()["movement_authorized"]
    pending = {"position_before": {"x":0.,"y":0.,"z":0.}, "along_route_before_m":0.,
        "command_duration_ms":300, "magnitude":.8, "desired_world_heading_deg":0.}
    for sequence, command in enumerate((100,101,100,None,True), start=102):
        adapter._learn_static_obstacle_from_post_command(
            make_observation(sequence=sequence,monotonic_t=float(sequence)),
            {**pending,"sequence":command}, {"along_route_m":0.}, 0.)
    assert adapter._static_pending_discovery is None
    assert adapter._static_last_learning["evidence_count"] == 2
    assert adapter._static_no_progress_streak == 2
    assert not actuator.commands
