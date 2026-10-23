from __future__ import annotations

from ff7r_agent.runtime_commands import _run_navigation_objective


TARGET = {"x": 10.0, "y": 20.0, "z": 30.0}


def preflight(route_sha: str, *, authorized: bool = True, reason: str = "") -> dict:
    if not authorized:
        return {
            "movement_authorized": False,
            "reason": reason or "live_start_not_inside_current_topology",
            "timeline": [],
        }
    return {
        "movement_authorized": True,
        "reason": "movement_authorized",
        "authorization": {
            "route_plan_sha256": route_sha,
            "session_id": "session-1",
            "world_id": "world-1",
            "map_id": "EndGame",
        },
        "authorization_proof": {
            "route_plan_sha256": route_sha,
            "session_id": "session-1",
            "world_id": "world-1",
            "map_id": "EndGame",
        },
        "static_obstacles": {"digest": "overlay-1"},
        "timeline": [{"event": "immutable_route_authorization_proof_created"}],
    }


def stopped_divergence(*, progress: float | None = None, distance: float | None = None) -> dict:
    timeline = []
    if progress is not None or distance is not None:
        timeline.append({
            "event": "control_cycle",
            "actual_command_sent": True,
            "route_progress": progress,
            "distance_to_final_m": distance,
        })
    return {
        "state": "STOPPED",
        "reason": "post_command_corridor_reacquisition_failed:corridor_membership_not_authorized",
        "movement_authorized": False,
        "movement_cycles": 1,
        "route_plan_sha256": "old-route",
        "timeline": timeline,
    }


def arrived(route_sha: str) -> dict:
    return {
        "state": "ARRIVED",
        "reason": "arrived",
        "movement_authorized": False,
        "movement_cycles": 1,
        "route_plan_sha256": route_sha,
        "timeline": [],
    }


def test_authorized_route_completion_does_not_start_recovery():
    calls = []

    def attempts():
        calls.append(1)
        return {
            "adapter": object(),
            "preflight": preflight("route-71"),
            "run": arrived("route-71"),
        }

    result = _run_navigation_objective(
        attempts,
        dry_run=False,
        objective_id="chadley-objective",
        target_position=TARGET,
    )

    assert len(calls) == 1
    assert result["objective_state"] == "NAVIGATION_OBJECTIVE_COMPLETED"
    assert result["recovery_attempts"] == 0
    assert result["route_attempts"][0]["classification"] == "terminal_route_failure"


def test_route_divergence_relocalizes_and_replans_from_actual_position():
    calls = []

    def attempts():
        index = len(calls)
        calls.append(index)
        if index == 0:
            return {
                "adapter": object(),
                "preflight": preflight("old-route"),
                "run": stopped_divergence(),
            }
        return {
            "adapter": object(),
            "preflight": preflight("new-route-from-current-position"),
            "run": arrived("new-route-from-current-position"),
        }

    result = _run_navigation_objective(
        attempts,
        dry_run=False,
        objective_id="chadley-objective",
        target_position=TARGET,
    )

    assert len(calls) == 2
    assert result["objective_state"] == "NAVIGATION_OBJECTIVE_COMPLETED"
    assert result["route_attempts"][0]["route_identity"]["route_plan_sha256"] == "old-route"
    assert result["route_attempts"][1]["route_identity"]["route_plan_sha256"] == "new-route-from-current-position"
    assert result["route_attempts"][1]["preflight"]["authorization_proof"]["route_plan_sha256"] != "old-route"
    events = [event["event"] for event in result["timeline"]]
    assert events.index("navigation_objective_route_invalidated") < events.index("navigation_objective_recovery_started")
    assert all(event.get("movement_command_sent") is False for event in result["timeline"] if "movement_command_sent" in event)
    assert result["objective_id"] == "chadley-objective"
    assert result["objective_target_position"] == TARGET


def test_unresolved_relocalization_remains_neutral_and_aborts_objective():
    calls = []

    def attempts():
        index = len(calls)
        calls.append(index)
        if index == 0:
            return {"adapter": object(), "preflight": preflight("old-route"), "run": stopped_divergence()}
        return {
            "adapter": object(),
            "preflight": preflight("unused", authorized=False, reason="live_start_not_inside_current_topology"),
            "run": None,
        }

    result = _run_navigation_objective(
        attempts,
        dry_run=False,
        objective_id="chadley-objective",
        target_position=TARGET,
    )

    assert len(calls) == 2
    assert result["objective_state"] == "NAVIGATION_OBJECTIVE_ABORTED"
    assert result["final_run"] is None
    assert result["route_attempts"][1]["movement_commands"] == 0
    assert any(event["event"] == "navigation_objective_aborted_during_preflight" for event in result["timeline"])


def test_recovery_budget_exhaustion_is_bounded_and_distinct_from_safety_stop():
    calls = []

    def attempts():
        calls.append(1)
        return {"adapter": object(), "preflight": preflight(f"route-{len(calls)}"), "run": stopped_divergence()}

    result = _run_navigation_objective(
        attempts,
        dry_run=False,
        objective_id="chadley-objective",
        target_position=TARGET,
        max_recovery_attempts=2,
    )

    assert len(calls) == 3
    assert result["objective_state"] == "NAVIGATION_OBJECTIVE_ABORTED"
    assert result["final_run"]["reason"] == "recovery_budget_exhausted"
    assert result["final_run"]["route_terminal_reason"].startswith("post_command_corridor")
    assert result["recovery_attempts"] == 2
    assert any(event["event"] == "navigation_objective_recovery_budget_exhausted" for event in result["timeline"])


def test_meaningful_progress_resets_local_recovery_streak_but_absolute_bound_remains():
    calls = []

    def attempts():
        index = len(calls)
        calls.append(index)
        if index == 0:
            run = stopped_divergence(progress=0.0, distance=100.0)
            run["timeline"].append({
                "event": "control_cycle",
                "actual_command_sent": True,
                "route_progress": 0.2,
                "distance_to_final_m": 99.0,
            })
        else:
            run = stopped_divergence()
        return {"adapter": object(), "preflight": preflight(f"route-{index}"), "run": run}

    result = _run_navigation_objective(
        attempts,
        dry_run=False,
        objective_id="chadley-objective",
        target_position=TARGET,
        max_recovery_attempts=2,
    )

    assert len(calls) == 3
    assert result["route_attempts"][0]["recovery_budget_reset"] is True
    assert result["route_attempts"][1]["recovery_attempt"] == 1
    assert result["final_run"]["reason"] == "recovery_budget_exhausted"


def test_stale_or_unsafe_preflight_never_starts_recovery_movement():
    calls = []

    def attempts():
        calls.append(1)
        return {
            "adapter": object(),
            "preflight": preflight("none", authorized=False, reason="telemetry_stale"),
            "run": None,
        }

    result = _run_navigation_objective(
        attempts,
        dry_run=False,
        objective_id="chadley-objective",
        target_position=TARGET,
    )

    assert len(calls) == 1
    assert result["objective_state"] == "NAVIGATION_OBJECTIVE_ABORTED"
    assert result["recovery_attempts"] == 0
    assert result["route_attempts"][0]["movement_commands"] == 0
