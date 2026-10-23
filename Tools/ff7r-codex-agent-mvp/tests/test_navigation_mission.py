import asyncio
import copy
import json

import pytest

from ff7r_agent.navigation_mission import (
    CHADLEY_GOAL, MissionConfig, MissionError, NavigationMission,
    resolve_goal, route_metrics, unwrap, verify_arrival,
)


def observation(sequence=10, x=0):
    return {"sequence": sequence, "running": True, "map_id": "EndGame", "telemetry": {"live": True}, "player": {"actor_id": "Cloud-1", "position": {"x": x, "y": 0, "z": 65}}}


def goal():
    return {"landmark_id": CHADLEY_GOAL, "map_id": "EndGame", "position": {"x": 1000, "y": 0, "z": 65}}


class Runtime:
    def __init__(self):
        self.calls = []
        self.active_lease = False
        self.sequence = 10
        self.run_state = "ARRIVED"
        self.run_reason = "final_waypoint_reached"
        self.preflight = True
        self.running_polls = 1
        self.time = 0
        self.fail_cleanup = False
        self.end_x = 1000
        self.transport_failure = False
        self.route_started = False
        self.client_id = None
        self.game_pid = 6932
        self.stale_pid = None  # a durable context naming a process that has gone

    async def sleep(self, seconds):
        self.time += seconds

    async def call(self, op, args):
        self.calls.append((op, args))
        result = {}
        if op == "get_runtime_identity":
            result = {"git_commit_sha": "accepted", "worker_pid": 123}
        elif op == "refresh_lifecycle_attachment":
            self.stale_pid = None
        elif op == "get_lifecycle_status":
            result = {"run_context": {"pid": self.stale_pid or self.game_pid, "run_id": "game-run", "executable_sha256": "verified"}}
        elif op == "wait_for_settled_gameplay":
            result = {"ok": True}
        elif op == "get_session_health":
            result = {"process_exists": True, "telemetry_live": True}
        elif op == "observe_game":
            self.sequence += 1
            result = observation(self.sequence, self.end_x if self.route_started else 0)
        elif op == "get_navigation_context":
            result = {"map_id": "EndGame", "known_landmarks": [goal()]}
        elif op == "get_control_session_status":
            result = {"active": self.active_lease, "client_id": self.client_id}
        elif op == "open_control_session":
            self.client_id = args["client_id"]
            self.active_lease = True
            result = {"active": True, "client_id": self.client_id}
        elif op == "capture_game_window":
            result = {"ok": True, "image_path": "captured.bmp", "pid": self.game_pid}
        elif op == "start_live_route_adapter":
            self.route_started = True
            self.dry_run = args["dry_run"]
            if self.transport_failure:
                raise OSError("response lost")
            result = {"ok": True, "job_id": "route-1", "state": "running"}
        elif op == "get_live_route_adapter_status":
            self.running_polls -= 1
            result = {"ok": True, "job_id": "route-1", "state": "running"}
            if self.running_polls <= 0:
                result.update(state="completed", response={"ok": True, "id": 42, "result": {
                    "preflight": {"movement_authorized": self.preflight, "reason": "unreachable"},
                    "run": {"state": "STOPPED" if self.dry_run else self.run_state, "reason": "control_cycle_bound_reached" if self.dry_run else self.run_reason, "movement_cycles": 0 if self.dry_run else 1, "timeline": []},
                }})
        elif op == "stop_all_input" and self.fail_cleanup:
            return {"ok": False, "error": "broker_unreachable"}
        return {"ok": True, "id": len(self.calls), "result": result}


def run(runtime, tmp_path, *, execute=True, identity_ok=True, timeout=20, capture_evidence=True):
    mission = NavigationMission(runtime.call, tmp_path / "run", identity_check=lambda *args: {"ok": identity_ok}, config=MissionConfig(execute=execute, timeout_seconds=timeout, capture_evidence=capture_evidence), clock=lambda: runtime.time, sleep=runtime.sleep, progress=lambda _: None)
    return asyncio.run(mission.run())


def test_identity_refusal_is_not_recoverable(tmp_path):
    runtime = Runtime()
    result = run(runtime, tmp_path, identity_ok=False)
    assert result["recovery_refused"] is True
    assert not any(op == "start_live_route_adapter" for op, _ in runtime.calls)


def test_identity_check_exception_is_not_recoverable(tmp_path):
    runtime = Runtime()
    def wrong_head(*_):
        raise MissionError("feature-validation HEAD mismatch")
    mission = NavigationMission(runtime.call, tmp_path / "run", identity_check=wrong_head,
        config=MissionConfig(execute=True), clock=lambda: runtime.time, sleep=runtime.sleep)
    result = asyncio.run(mission.run())
    assert result["recovery_refused"] is True
    assert not runtime.route_started


def test_route_rpc_refusal_is_not_recoverable(tmp_path):
    runtime = Runtime()
    original = runtime.call
    async def refused(op, args):
        if op == "start_live_route_adapter":
            return {"ok": False, "error": "SafetyError: input stop is latched"}
        return await original(op, args)
    runtime.call = refused
    assert run(runtime, tmp_path)["recovery_refused"] is True


def test_lost_route_start_response_is_not_recoverable(tmp_path):
    runtime = Runtime()
    runtime.transport_failure = True
    assert run(runtime, tmp_path)["recovery_refused"] is True


def test_navigation_obstacle_remains_recoverable(tmp_path):
    runtime = Runtime()
    runtime.preflight = False
    result = run(runtime, tmp_path)
    assert result["reason"] == "unreachable"
    assert result["recovery_refused"] is False


def test_a_restarted_game_is_rebound_before_the_route(tmp_path):
    # Live 2026-10-05: the durable context still named the previous day's
    # game process, and the route stopped at capture_process_mismatch.
    runtime = Runtime()
    runtime.stale_pid = 24976
    result = run(runtime, tmp_path)
    assert result["ok"] and result["state"] == "ARRIVED"
    assert result["game_binding"]["pid"] == runtime.game_pid


def test_a_bridge_left_on_an_older_commit_rebinds_before_its_identity_is_read(tmp_path):
    # The bridge rebinds to the checkout on its first live operation, so the
    # identity is read after one (live 2026-09-30: runtime_identity_mismatch).
    runtime = Runtime()
    commit = {"sha": "older"}
    original = runtime.call
    async def rebinding(op, args):
        if op == "get_runtime_identity":
            runtime.calls.append((op, args))
            return {"ok": True, "id": 1, "result": {"git_commit_sha": commit["sha"], "worker_pid": 123}}
        if op == "refresh_lifecycle_attachment":
            commit["sha"] = "checkout"
        return await original(op, args)
    runtime.call = rebinding
    mission = NavigationMission(runtime.call, tmp_path / "run",
        identity_check=lambda identity, *rest: {"ok": identity.get("git_commit_sha") == "checkout"},
        config=MissionConfig(execute=True, timeout_seconds=20), clock=lambda: runtime.time,
        sleep=runtime.sleep, progress=lambda _: None)
    result = asyncio.run(mission.run())
    assert result["ok"] and result["state"] == "ARRIVED"


def test_arrival_mission_and_cleanup(tmp_path):
    runtime = Runtime()
    result = run(runtime, tmp_path)
    assert result["ok"] and result["state"] == "ARRIVED"
    operations = [op for op, _ in runtime.calls]
    assert operations[:5] == ["refresh_lifecycle_attachment", "get_runtime_identity", "get_lifecycle_status",
                              "get_session_health", "observe_game"]
    arguments = next(args for op, args in runtime.calls if op == "start_live_route_adapter")
    assert arguments["route_target_position"] == goal()["position"]
    assert arguments["route_mode"] == "cached_topology"
    assert arguments["normal_stick_magnitude"] == 1.0
    assert arguments["control_period_seconds"] == 0.3
    assert operations[-2:] == ["stop_all_input", "close_control_session"]
    assert not set(operations) & {"move_stick", "travel_vector_chunk", "launch_game"}
    assert json.loads((tmp_path / "run" / "result.json").read_text())["arrival"]["confirmed"]
    assert "result.json" in json.loads((tmp_path / "run" / "sha256.json").read_text())


def test_dry_run_never_claims_arrival(tmp_path):
    result = run(Runtime(), tmp_path, execute=False)
    assert result["state"] == "PREFLIGHT_PASSED"
    assert not result["movement_attempted"]
    assert "arrival" not in result


def test_identity_failure_has_no_session_or_input_mutation(tmp_path):
    runtime = Runtime()
    result = run(runtime, tmp_path, identity_ok=False)
    assert not result["ok"]
    assert "open_control_session" not in [op for op, _ in runtime.calls]
    assert result["cleanup"] == {}


def test_an_open_session_is_borrowed_not_closed(tmp_path):
    # It is open for the life of the game (the agent's, or startup's); a
    # mission that closed it would latch the stop behind everything after it.
    runtime = Runtime()
    runtime.active_lease = True
    result = run(runtime, tmp_path)
    assert result["ok"] and result["control_session"] == "borrowed"
    operations = [op for op, _ in runtime.calls]
    assert not {"open_control_session", "close_control_session", "stop_all_input"} & set(operations)


def test_a_session_lent_by_the_caller_is_used_and_left_open(tmp_path):
    # #193: an NPC approach lends its session to each route. Closing it would
    # latch the stop, and the approach's next action would be refused.
    runtime = Runtime()
    runtime.active_lease, runtime.client_id = True, "approach-1"
    mission = NavigationMission(runtime.call, tmp_path / "run", identity_check=lambda *args: {"ok": True},
                                config=MissionConfig(execute=True, timeout_seconds=20), clock=lambda: runtime.time,
                                sleep=runtime.sleep, progress=lambda _: None, client_id="approach-1")
    result = asyncio.run(mission.run())
    assert result["ok"] and result["state"] == "ARRIVED" and result["control_session"] == "adopted"
    operations = [op for op, _ in runtime.calls]
    assert not {"open_control_session", "close_control_session", "stop_all_input"} & set(operations)
    assert result["cleanup"] == {"session": "left_open"}


def test_a_session_under_another_id_is_borrowed_under_that_id(tmp_path):
    runtime = Runtime()
    runtime.active_lease, runtime.client_id = True, "someone-else"
    mission = NavigationMission(runtime.call, tmp_path / "run", identity_check=lambda *args: {"ok": True},
                                config=MissionConfig(execute=True, timeout_seconds=20), clock=lambda: runtime.time,
                                sleep=runtime.sleep, progress=lambda _: None, client_id="approach-1")
    result = asyncio.run(mission.run())
    assert result["ok"] and result["control_session"] == "borrowed"
    assert mission.session_client_id == "someone-else"
    assert "close_control_session" not in [op for op, _ in runtime.calls]


def test_budget_exhaustion_cancels_job_and_closes_session(tmp_path):
    runtime = Runtime()
    runtime.running_polls = 100
    result = run(runtime, tmp_path, timeout=10)
    assert result["reason"] == "mission_time_budget_exhausted"
    assert [op for op, _ in runtime.calls][-2:] == ["stop_all_input", "close_control_session"]
    assert sum(op == "start_live_route_adapter" for op, _ in runtime.calls) == 1


def test_lost_start_response_is_stopped_not_replayed(tmp_path):
    runtime = Runtime()
    runtime.transport_failure = True
    result = run(runtime, tmp_path)
    assert not result["ok"]
    assert sum(op == "start_live_route_adapter" for op, _ in runtime.calls) == 1
    assert [op for op, _ in runtime.calls][-2:] == ["stop_all_input", "close_control_session"]


@pytest.mark.parametrize("state,reason", [("STOPPED", "route_blocked"), ("REPLAN_REQUIRED", "plan_identity_changed")])
def test_route_refusal_is_terminal_not_blind_retry(tmp_path, state, reason):
    runtime = Runtime()
    runtime.run_state, runtime.run_reason = state, reason
    result = run(runtime, tmp_path)
    assert not result["ok"] and result["reason"] == reason
    assert sum(op == "start_live_route_adapter" for op, _ in runtime.calls) == 1


def test_preflight_refusal_closes_input(tmp_path):
    runtime = Runtime()
    runtime.preflight = False
    assert run(runtime, tmp_path)["reason"] == "unreachable"
    assert [op for op, _ in runtime.calls][-2:] == ["stop_all_input", "close_control_session"]


def test_report_cannot_succeed_when_cleanup_fails(tmp_path):
    runtime = Runtime()
    runtime.fail_cleanup = True
    result = run(runtime, tmp_path)
    assert not result["ok"] and result["state"] == "CLEANUP_FAILED"


def test_follower_arrival_requires_independent_goal_measurement(tmp_path):
    runtime = Runtime()
    runtime.end_x = 800
    result = run(runtime, tmp_path)
    assert not result["ok"] and result["reason"] == "arrival_outside_goal_tolerance"


@pytest.mark.parametrize("mutation,error", [
    (lambda s: s.update(sequence=12), "arrival_sequence_not_advancing"),
    (lambda s: s["telemetry"].update(live=False), "arrival_telemetry_stale"),
    (lambda s: s.update(map_id="Other"), "arrival_identity_changed"),
    (lambda s: s["player"].update(actor_id="Other"), "arrival_identity_changed"),
    (lambda s: s.update(paused=True), "arrival_not_gameplay"),
    (lambda s: s["player"]["position"].update(z=300), "arrival_outside_goal_tolerance"),
])
def test_arrival_evidence_failures(mutation, error):
    samples = [observation(12, 1000), observation(13, 1000)]
    mutation(samples[1])
    with pytest.raises(MissionError, match=error):
        verify_arrival(samples, goal(), observation())


def test_landmark_is_exact_and_map_bound():
    with pytest.raises(MissionError, match="missing_or_ambiguous"):
        resolve_goal({"map_id": "EndGame", "known_landmarks": [goal(), goal()]}, CHADLEY_GOAL)
    with pytest.raises(MissionError, match="map_mismatch"):
        resolve_goal({"map_id": "other", "known_landmarks": [goal()]}, CHADLEY_GOAL)


def test_nested_worker_failure_is_not_transport_success():
    with pytest.raises(MissionError, match="movement_lease_missing"):
        unwrap({"ok": True, "id": 1, "result": {"ok": False, "error": "movement_lease_missing"}})


def test_metrics_deduplicate_observations_and_report_missing_final_sample():
    event = {"event": "post_command_observation", "command_sequence": 10, "position_before": {"x": 0, "y": 0, "z": 0}, "position_after": {"x": 300, "y": 400, "z": 1200}}
    metrics = route_metrics({"run": {"movement_cycles": 2, "timeline": [event, copy.deepcopy(event)]}})
    assert metrics["cumulative_horizontal_distance_m"] == 5
    assert metrics["observed_movement_cycles"] == 1
    assert not metrics["distance_sampling_complete"]


def test_existing_evidence_directory_never_overwritten(tmp_path):
    (tmp_path / "run").mkdir()
    with pytest.raises(FileExistsError):
        run(Runtime(), tmp_path)


def test_deadline_includes_setup_before_any_lease_or_input(tmp_path):
    runtime = Runtime()
    original = runtime.call
    async def slow_setup(op, args):
        value = await original(op, args)
        if op == "get_bootstrap_verification":
            runtime.time += 21
        return value
    runtime.call = slow_setup
    result = run(runtime, tmp_path)
    assert result["reason"] == "mission_time_budget_exhausted"
    assert not runtime.route_started and runtime.client_id is None


@pytest.mark.parametrize("own", [True, False])
def test_lost_open_response_only_cleans_up_correlated_lease(tmp_path, own):
    runtime = Runtime()
    original = runtime.call
    async def lose_open(op, args):
        value = await original(op, args)
        if op == "open_control_session":
            if not own:
                runtime.client_id = "a-different-task"
            raise OSError("session response lost")
        return value
    runtime.call = lose_open
    result = run(runtime, tmp_path)
    assert not result["ok"] and not runtime.route_started
    operations = [op for op, _ in runtime.calls]
    assert operations.count("open_control_session") == 1
    assert ("stop_all_input" in operations) == own
    assert result["cleanup"]["session_reconciliation"]["owned"] == own


@pytest.mark.parametrize("capture_evidence", [True, False])
def test_mission_rejects_process_change_even_with_matching_actor_and_position(tmp_path, capture_evidence):
    runtime = Runtime()
    original = runtime.call
    async def changed_process(op, args):
        if op == "get_lifecycle_status" and runtime.route_started:
            runtime.game_pid = 9999
        return await original(op, args)
    runtime.call = changed_process
    assert run(runtime, tmp_path, capture_evidence=capture_evidence)["reason"] == "arrival_game_process_changed"


def test_telemetry_only_arrival_keeps_distinct_samples_and_binding_checks(tmp_path):
    runtime = Runtime()
    original = runtime.call
    arrival_times, completion_times = [], []
    async def timed(op, args):
        result = await original(op, args)
        if op == "get_live_route_adapter_status":
            completion_times.append(runtime.time)
        if op == "observe_game" and runtime.route_started:
            arrival_times.append(runtime.time)
        return result
    runtime.call = timed
    result = run(runtime, tmp_path, capture_evidence=False)
    assert result["ok"] and result["arrival"]["confirmed"]
    assert arrival_times == pytest.approx([completion_times[-1] + 0.3, completion_times[-1] + 0.6])
    assert len({sample["sequence"] for sample in result["arrival"]["samples"]}) == 2
    operations = [op for op, _ in runtime.calls]
    assert "capture_game_window" not in operations
    for gate in ("refresh_lifecycle_attachment", "get_lifecycle_status", "get_session_health",
                 "wait_for_settled_gameplay", "get_control_session_status", "stop_all_input"):
        assert gate in operations


@pytest.mark.parametrize("fault,reason", [
    ("stale", "arrival_telemetry_stale"),
    ("actor", "arrival_identity_changed"),
    ("sequence", "arrival_sequence_not_advancing"),
])
def test_telemetry_only_arrival_still_refuses_invalid_evidence(tmp_path, fault, reason):
    runtime = Runtime()
    original = runtime.call
    async def invalid(op, args):
        value = await original(op, args)
        if op == "observe_game" and runtime.route_started:
            state = value["result"]
            if fault == "stale":
                state["telemetry"]["live"] = False
            elif fault == "actor":
                state["player"]["actor_id"] = "different-actor"
            else:
                state["sequence"] = 10
        return value
    runtime.call = invalid
    result = run(runtime, tmp_path, capture_evidence=False)
    assert not result["ok"] and result["reason"] == reason
    assert "stop_all_input" in [op for op, _ in runtime.calls]


def test_metrics_include_all_attempts_and_latest_feedback_once():
    def event(sequence, end):
        return {"event": "post_command_observation", "command_sequence": 10, "sequence": sequence,
            "position_before": {"x": 0, "y": 0, "z": 0}, "position_after": {"x": end, "y": 0, "z": 0}}
    attempts = [{"run": {"movement_cycles": 1, "timeline": [event(11, 100), event(12, 150)]}},
                {"run": {"movement_cycles": 1, "timeline": [event(11, 200)]}}]
    metrics = route_metrics({"route_attempts": attempts})
    assert metrics["movement_cycles"] == metrics["observed_movement_cycles"] == 2
    assert metrics["cumulative_horizontal_distance_m"] == 3.5


def test_coordinate_mission_uses_same_generic_route_and_map_gate(tmp_path):
    runtime = Runtime()
    config = MissionConfig(execute=False, target_position=(1500, 200, 65), target_map_id="EndGame",
                           topology_cache_manifest="admitted/manifest.json")
    mission = NavigationMission(runtime.call, tmp_path/"run", identity_check=lambda *args: {"ok": True},
        config=config, clock=lambda:runtime.time, sleep=runtime.sleep, progress=lambda _:None)
    result = asyncio.run(mission.run())
    assert result["ok"] and result["goal_id"] is None
    arguments = next(a for op,a in runtime.calls if op == "start_live_route_adapter")
    assert arguments["route_target_position"] == {"x":1500,"y":200,"z":65}
    assert arguments["topology_cache_manifest_path"] == "admitted/manifest.json"
    assert "get_navigation_context" not in [op for op,_ in runtime.calls]


def test_mission_reacquires_transient_startup_readiness_before_opening_session(tmp_path):
    runtime = Runtime(); call = runtime.call
    async def transient(op,args):
        response = await call(op,args)
        if op == "get_session_health" and sum(name==op for name,_ in runtime.calls)==1:
            response["result"]["telemetry_live"] = False
        return response
    runtime.call = transient
    result = run(runtime,tmp_path)
    assert result["ok"]
    names = [name for name,_ in runtime.calls]
    assert names.index("wait_for_settled_gameplay") < names.index("open_control_session")


@pytest.mark.parametrize("fault", ["readiness", "process"])
def test_mission_setup_reacquisition_does_not_authorize_failed_or_changed_game(tmp_path,fault):
    runtime = Runtime(); call = runtime.call
    async def fail(op,args):
        response = await call(op,args)
        if op == "wait_for_settled_gameplay":
            if fault == "readiness": response["result"] = {"ok":False,"reason":"not_ready"}
            else: runtime.game_pid += 1
        return response
    runtime.call = fail
    result = run(runtime,tmp_path)
    assert not result["ok"]
    assert not any(name=="open_control_session" for name,_ in runtime.calls)



def drift_runtime(*, correct=True, fault=None):
    runtime = Runtime()
    runtime.end_x = 915  # 85 cm from the target, inside the correction bound.
    runtime.starts = 0
    original = runtime.call

    async def call(op, args):
        if op == "start_live_route_adapter":
            runtime.starts += 1
            if runtime.starts == 2:
                runtime.running_polls = 1
                if fault == "lost_start":
                    raise OSError("correction start response lost")
                if correct:
                    runtime.end_x = 1000
                if fault == "route_refused":
                    runtime.run_state, runtime.run_reason = "STOPPED", "new_route_blocked"
        if op == "get_control_session_status" and runtime.route_started and fault == "lease_changed":
            runtime.client_id = "another-task"
        result = await original(op, args)
        if op == "observe_game" and runtime.route_started and fault == "actor_changed" and runtime.sequence % 2:
            result["result"]["player"]["actor_id"] = "another-actor"
        return result

    runtime.call = call
    return runtime


def test_small_arrival_drift_is_corrected_locally_and_metrics_include_both_routes(tmp_path):
    runtime = drift_runtime()
    result = run(runtime, tmp_path)
    assert result["ok"] and result["arrival"]["confirmed"]
    assert runtime.starts == 2
    starts = [args for op, args in runtime.calls if op == "start_live_route_adapter"]
    assert starts[1]["route_target_position"] == starts[0]["route_target_position"]
    assert starts[1]["max_cycles"] == 32
    assert starts[1]["cached_topology_wall_clock_seconds"] <= 20
    assert starts[1]["normal_stick_magnitude"] == 1.0
    assert result["metrics"]["movement_cycles"] == 2
    assert result["arrival_correction"]["attempts"] == 1


def test_persistent_arrival_drift_never_starts_a_third_route(tmp_path):
    runtime = drift_runtime(correct=False)
    result = run(runtime, tmp_path)
    assert runtime.starts == 2 and not result["ok"]
    assert result["reason"] == "arrival_outside_goal_tolerance"


@pytest.mark.parametrize("fault,reason,starts", [
    ("actor_changed", "arrival_identity_changed", 1),
    ("lease_changed", "arrival_correction_session_changed", 1),
    ("route_refused", "new_route_blocked", 2),
    ("lost_start", "correction start response lost", 2),
])
def test_arrival_correction_does_not_bypass_gates_or_replay_uncertain_start(tmp_path, fault, reason, starts):
    runtime = drift_runtime(fault=fault)
    result = run(runtime, tmp_path)
    assert not result["ok"] and result["reason"] == reason
    assert runtime.starts == starts
    if fault == "lease_changed":
        assert "stop_all_input" not in [op for op, args in runtime.calls]
    else:
        assert [op for op, args in runtime.calls][-2:] == ["stop_all_input", "close_control_session"]


def test_arrival_correction_cannot_exceed_the_original_command_budget(tmp_path):
    runtime = drift_runtime()
    mission = NavigationMission(runtime.call, tmp_path / "run", identity_check=lambda *args: {"ok":True},
        config=MissionConfig(execute=True,max_cycles=1,timeout_seconds=20),clock=lambda:runtime.time,
        sleep=runtime.sleep,progress=lambda _:None)
    result = asyncio.run(mission.run())
    assert not result["ok"] and runtime.starts == 1
    assert result["reason"] == "arrival_outside_goal_tolerance"


@pytest.mark.parametrize("feature", [True, False])
def test_mission_explicit_speed_trial_is_feature_bound_and_forwarded(tmp_path, feature):
    runtime = Runtime()
    async def rpc(op, args):
        value = await runtime.call(op, args)
        if op == "get_runtime_identity":
            value["result"]["deployment_profile"] = "FEATURE_VALIDATION" if feature else "CANONICAL"
        return value
    mission = NavigationMission(rpc, tmp_path / "trial", identity_check=lambda *args: {"ok": True},
        config=MissionConfig(execute=True, normal_stick_magnitude=0.8),
        clock=lambda: runtime.time, sleep=runtime.sleep, progress=lambda _: None)
    result = asyncio.run(mission.run())
    if feature:
        assert result["ok"]
        args = next(args for op, args in runtime.calls if op == "start_live_route_adapter")
        assert args["normal_stick_magnitude"] == 0.8
        assert args["controller_policy_profile"] == "normal_0_8_trial"
        assert args["final_stick_magnitude"] == 0.5
        assert result["controller_policy"]["normal_stick_magnitude"] == 0.8
    else:
        assert result["reason"] == "live_controller_trial_requires_feature_runtime"
        assert not any(op in ("open_control_session", "start_live_route_adapter") for op, _ in runtime.calls)


def test_lazy_executable_digest_is_bound_before_any_movement(tmp_path):
    runtime = Runtime()
    original = runtime.call
    digest = None
    async def call(op, args):
        nonlocal digest
        result = await original(op, args)
        if op == "get_lifecycle_status":
            result["result"]["run_context"]["executable_sha256"] = digest
        elif op == "verify_executable_identity":
            digest = "A" * 64
            result["result"] = {"verified": True, "sha256": digest}
        elif op == "start_live_route_adapter":
            assert digest == "A" * 64
        return result
    runtime.call = call
    result = run(runtime, tmp_path)
    assert result["ok"]
    assert result["game_binding"]["executable_sha256"] == "A" * 64


def test_unverified_lazy_digest_never_starts_navigation(tmp_path):
    runtime = Runtime()
    original = runtime.call
    async def call(op, args):
        result = await original(op, args)
        if op == "get_lifecycle_status":
            result["result"]["run_context"]["executable_sha256"] = None
        return result
    runtime.call = call
    result = run(runtime, tmp_path)
    assert result["reason"] == "executable_identity_not_verified"
    assert not runtime.route_started


@pytest.mark.parametrize("arrival_target", [None, (1000.0, 200.0, 88.0)])
def test_camera_option_is_explicitly_passed_to_route_runtime(tmp_path, arrival_target):
    runtime = Runtime()
    mission = NavigationMission(runtime.call, tmp_path / "camera", identity_check=lambda *a: {"ok": True},
        config=MissionConfig(execute=True, camera_follow_enabled=True,
                             camera_follow_arrival_target=arrival_target), clock=lambda: runtime.time,
        sleep=runtime.sleep, progress=lambda _: None)
    assert asyncio.run(mission.run())["ok"]
    args = next(args for op, args in runtime.calls if op == "start_live_route_adapter")
    assert args["camera_follow_enabled"] is True
    if arrival_target is None:
        assert "camera_follow_arrival_target" not in args
    else:
        assert args["camera_follow_arrival_target"] == arrival_target


def test_evidence_bytes_match_the_streamed_pretty_json_and_sha256_file(tmp_path):
    import hashlib
    import os
    runtime = Runtime()
    assert run(runtime, tmp_path)["ok"]
    hashes = json.loads((tmp_path / "run" / "sha256.json").read_text())
    files = sorted(path.name for path in (tmp_path / "run").glob("*.json") if path.name != "sha256.json")
    assert sorted(hashes) == files
    for name in files:
        data = (tmp_path / "run" / name).read_bytes()
        assert hashes[name] == hashlib.sha256(data).hexdigest()
        # The former text-mode json.dump layout, byte for byte.
        legacy = tmp_path / "legacy.json"
        with legacy.open("w", encoding="utf-8") as stream:
            json.dump(json.loads(data), stream, indent=2, allow_nan=False)
            stream.write("\n")
        assert data == legacy.read_bytes()
        assert os.linesep.encode() in data
        legacy.unlink()


@pytest.mark.parametrize("poll_seconds,expected", [
    (2.0, [0.25, 0.5, 1.0, 2.0, 2.0]),
    (0.4, [0.25, 0.4, 0.4, 0.4, 0.4]),
])
def test_status_polls_ramp_up_then_hold_the_configured_interval(tmp_path, poll_seconds, expected):
    runtime = Runtime()
    runtime.running_polls = 5
    sleeps = []
    async def sleep(seconds):
        sleeps.append(seconds)
        runtime.time += seconds
    mission = NavigationMission(runtime.call, tmp_path / "run", identity_check=lambda *a: {"ok": True},
        config=MissionConfig(execute=True, timeout_seconds=60, poll_seconds=poll_seconds),
        clock=lambda: runtime.time, sleep=sleep, progress=lambda _: None)
    assert asyncio.run(mission.run())["ok"]
    assert sleeps[:5] == expected
    assert sum(op == "get_live_route_adapter_status" for op, _ in runtime.calls) == 5


def rich_identity(**changes):
    identity = {"pid": 22980, "worker_generation": 1, "worker_pid": 3240, "git_commit_sha": "c0ffee",
                "git_dirty": False, "deployment_profile": "FEATURE_VALIDATION",
                "bridge_identity": {"pid": 22980, "process_creation_token": 7},
                "worker_identity": {"pid": 11424, "git_commit_sha": "c0ffee"},
                "bridge_status": {"generation": 1, "worker_pid": 3240, "started_at": 100.0, "last_restart_at": None,
                                  "gamepad_broker": {"supervised_sequence": -1}},
                "gamepad_broker_identity": {"supervised_sequence": -1}}
    identity.update(changes)
    return identity


class IdentityRuntime(Runtime):
    def __init__(self, identity):
        super().__init__()
        self.identity = identity
        self.run_id = "game-run"

    async def call(self, op, args):
        value = await super().call(op, args)
        if op == "get_runtime_identity":
            value["result"] = copy.deepcopy(self.identity)
        elif op == "get_lifecycle_status":
            value["result"]["run_context"]["run_id"] = self.run_id
        return value


def approach_missions(tmp_path, runtime, cache, between=None):
    results = []
    for index in range(2):
        if index and between:
            between(runtime)
        runtime.route_started = False
        runtime.active_lease = False
        runtime.running_polls = 1
        mission = NavigationMission(runtime.call, tmp_path / f"route-{index + 1}", identity_check=cache,
            config=MissionConfig(execute=True, timeout_seconds=60), clock=lambda: runtime.time,
            sleep=runtime.sleep, progress=lambda _: None)
        results.append(asyncio.run(mission.run()))
    return results


def test_a_later_route_in_the_approach_reuses_the_matching_recent_verification(tmp_path):
    from ff7r_agent.navigation_mission import VerifiedIdentityCache
    runtime, checks = IdentityRuntime(rich_identity()), []
    def check(identity, bootstrap):
        checks.append(identity)
        return {"ok": True, "profile": "FEATURE_VALIDATION"}
    cache = VerifiedIdentityCache(check, clock=lambda: runtime.time)
    # Volatile broker fields may change between routes without a re-check.
    first, second = approach_missions(tmp_path, runtime, cache,
        between=lambda r: r.identity["gamepad_broker_identity"].update(supervised_sequence=40))
    assert first["ok"] and second["ok"]
    assert len(checks) == 1
    assert sum(op == "get_bootstrap_verification" for op, _ in runtime.calls) == 1
    assert (first["identity_verification"], second["identity_verification"]) == ("full", "reused")
    reused = json.loads((tmp_path / "route-2" / "identity-check.json").read_text())
    assert reused["ok"] and reused["reused"] and reused["verified_by"] == str(tmp_path / "route-1")
    assert reused["verification"] == {"ok": True, "profile": "FEATURE_VALIDATION"}
    assert reused["game_binding"] == {"pid": 6932, "run_id": "game-run", "executable_sha256": "verified"}
    # Every fresh setup gate still ran for the reusing route.
    operations = [op for op, _ in runtime.calls]
    second_ops = operations[operations.index("get_runtime_identity", 1):]
    for gate in ("get_runtime_identity", "get_lifecycle_status", "get_session_health", "observe_game",
                 "wait_for_settled_gameplay", "get_control_session_status", "open_control_session",
                 "capture_game_window"):
        assert gate in second_ops


@pytest.mark.parametrize("change", [
    lambda r: r.identity.update(worker_pid=9999),
    lambda r: r.identity.update(worker_generation=2),
    lambda r: r.identity.update(git_commit_sha="another"),
    lambda r: r.identity.update(git_dirty=True),
    lambda r: r.identity.update(deployment_profile="CANONICAL"),
    lambda r: r.identity["bridge_identity"].update(process_creation_token=8),
    lambda r: r.identity["worker_identity"].update(pid=1),
    lambda r: r.identity["bridge_status"].update(started_at=200.0),
    lambda r: setattr(r, "run_id", "another-game-run"),
    lambda r: setattr(r, "game_pid", 4242),
    lambda r: setattr(r, "time", r.time + 301),
])
def test_any_identity_binding_or_age_change_forces_a_full_verification(tmp_path, change):
    from ff7r_agent.navigation_mission import VerifiedIdentityCache
    runtime, checks = IdentityRuntime(rich_identity()), []
    cache = VerifiedIdentityCache(lambda identity, bootstrap: checks.append(1) or {"ok": True},
                                  clock=lambda: runtime.time)
    results = approach_missions(tmp_path, runtime, cache, between=change)
    assert len(checks) == 2
    assert sum(op == "get_bootstrap_verification" for op, _ in runtime.calls) == 2
    assert results[1]["identity_verification"] == "full"


def test_a_failed_or_unpinned_verification_is_never_reused(tmp_path):
    from ff7r_agent.navigation_mission import VerifiedIdentityCache
    runtime, outcomes = IdentityRuntime(rich_identity()), [False, True]
    cache = VerifiedIdentityCache(lambda identity, bootstrap: {"ok": outcomes.pop(0)}, clock=lambda: runtime.time)
    first, second = approach_missions(tmp_path / "failed", runtime, cache)
    assert first["reason"] == "runtime_identity_mismatch" and second["ok"]
    assert second["identity_verification"] == "full"
    runtime = IdentityRuntime(rich_identity(pid=None))
    cache = VerifiedIdentityCache(lambda identity, bootstrap: {"ok": True}, clock=lambda: runtime.time)
    results = approach_missions(tmp_path / "unpinned", runtime, cache)
    assert [r["identity_verification"] for r in results] == ["full", "full"]


def test_a_reused_verification_still_refuses_a_capture_of_another_process(tmp_path):
    from ff7r_agent.navigation_mission import VerifiedIdentityCache
    runtime = IdentityRuntime(rich_identity())
    cache = VerifiedIdentityCache(lambda identity, bootstrap: {"ok": True}, clock=lambda: runtime.time)
    original = runtime.call
    async def call(op, args):
        value = await original(op, args)
        # Route 1 captures before and after; route 2's pre-route capture is the third.
        if op == "capture_game_window" and sum(o == "capture_game_window" for o, _ in runtime.calls) > 2:
            value["result"]["pid"] = 1
        return value
    runtime.call = call
    first, second = approach_missions(tmp_path, runtime, cache)
    assert first["ok"] and second["identity_verification"] == "reused"
    assert second["reason"] == "capture_process_mismatch"
    assert sum(op == "start_live_route_adapter" for op, _ in runtime.calls) == 1


def test_feature_identity_check_verifies_the_rebound_runtime(monkeypatch):
    # After a rebind the bridge keeps its original binding at the top level
    # and reports the process that runs operations as active_runtime_identity.
    from ff7r_agent import navigation_mission, runtime_profile
    monkeypatch.setattr(runtime_profile, "validate_checkout", lambda **kwargs: {"commit": kwargs["expected_commit_sha"]})
    monkeypatch.setattr(runtime_profile, "verify_runtime_identity",
                        lambda value, contract, worker_identity=None: {"ok": value.get("git_commit_sha") == contract["commit"]})
    check = navigation_mission.feature_identity_check(".", "feature", "new")
    bootstrap = {"ok": True, "runtime_identity": {"git_commit_sha": "new"}}
    rebound = {"git_commit_sha": "old", "active_runtime_identity": {"git_commit_sha": "new"}}
    assert check(rebound, bootstrap)["ok"]
    assert not check({"git_commit_sha": "old"}, bootstrap)["ok"]


@pytest.mark.parametrize("values", [{"model_id":"gpt-6.1-sol"}, {"reasoning_effort":"xhigh"},
                                    {"model_id":" ","reasoning_effort":"xhigh"}])
def test_navigation_driver_metadata_requires_explicit_nonempty_pair(values):
    with pytest.raises(ValueError, match="model_id and reasoning_effort"):
        MissionConfig(**values)


def test_explicit_navigation_lease_metadata_reaches_input_event(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from ff7r_agent.safety import SessionLease
    from ff7r_agent import input as input_module
    monkeypatch.setenv("LOCALAPPDATA",str(tmp_path))
    monkeypatch.setenv("FF7R_AUTONOMY_RUNTIME_DIR",str(tmp_path/"lease"))
    monkeypatch.setenv("FF7R_OBS_STATE_DIR",str(tmp_path/"obs"))
    monkeypatch.setenv("FF7R_OBS_INPUT_CLIPS","1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST",raising=False)
    lease=SessionLease(runtime_dir=tmp_path/"lease")
    runtime=Runtime();original=runtime.call
    async def rpc(op,args):
        if op=="open_control_session":
            lease.open(args["seconds"],model_id=args["model_id"],reasoning_effort=args["reasoning_effort"])
        if op=="start_live_route_adapter":
            input_module._write_obs_marker(datetime.now(timezone.utc))
        return await original(op,args)
    mission=NavigationMission(rpc,tmp_path/"run",identity_check=lambda *args:{"ok":True},
        config=MissionConfig(execute=True,model_id="gpt-6.1-sol",reasoning_effort="xhigh"),
        clock=lambda:runtime.time,sleep=runtime.sleep,progress=lambda _:None)
    assert asyncio.run(mission.run())["ok"]
    opened=next(args for op,args in runtime.calls if op=="open_control_session")
    assert opened["model_id"]=="gpt-6.1-sol" and opened["reasoning_effort"]=="xhigh"
    files=list((tmp_path/"obs"/"input-events").glob("*.json"))
    assert files and json.loads(files[0].read_text())["driver"]=={"modelId":"gpt-6.1-sol","effort":"xhigh"}


def test_explicit_metadata_never_reclaims_another_active_client(tmp_path):
    # The open session keeps its own attribution: the mission borrows it
    # rather than opening, replacing or closing anything.
    runtime=Runtime();runtime.active_lease=True;runtime.client_id="other-client"
    mission=NavigationMission(runtime.call,tmp_path/"run",identity_check=lambda *args:{"ok":True},
        config=MissionConfig(execute=True,model_id="gpt-6.1-sol",reasoning_effort="xhigh"),
        clock=lambda:runtime.time,sleep=runtime.sleep,progress=lambda _:None)
    result=asyncio.run(mission.run())
    assert result["ok"] and result["control_session"]=="borrowed"
    assert not any(op in {"open_control_session","close_control_session","stop_all_input"}
                   for op,args in runtime.calls)
    assert runtime.client_id=="other-client"


def test_navigation_cli_requires_both_driver_flags():
    from ff7r_agent.navigation_mission import main
    with pytest.raises(SystemExit):
        main(["--model-id","gpt-6.1-sol"])
