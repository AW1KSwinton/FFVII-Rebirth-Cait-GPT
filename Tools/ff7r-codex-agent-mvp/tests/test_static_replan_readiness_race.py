from __future__ import annotations

from test_live_route_adapter import make_adapter, make_observation


def test_readiness_failure_does_not_promote_unverified_static_obstacle():
    observations = [
        make_observation(sequence=index, controllable=index != 6)
        for index in range(1, 20)
    ]
    adapter, _, actuator, _ = make_adapter(observations=observations)
    assert adapter.preflight()["movement_authorized"]

    result = adapter.run(dry_run=False, max_cycles=4)

    interceptions = [
        item for item in result["timeline"]
        if item.get("event") == "static_obstacle_replan_intercepted_readiness_failure"
    ]
    assert interceptions == []
    assert any(item.get("event") == "readiness_recovery_sample" for item in result["timeline"])
    assert not any(item.get("event") == "static_obstacle_settled_confirmation" for item in result["timeline"])
    assert not any(item.get("event") == "static_obstacle_replanned_and_reauthorised" for item in result["timeline"])
    assert all(
        item.get("event") == "static_obstacle_short_pulse_not_evidence"
        for item in result["timeline"]
        if item.get("event", "").startswith("static_obstacle_")
        and item.get("event") != "static_obstacle_scope_bound"
    )
    assert not adapter.static_obstacles.planning_snapshot(now_t=10).obstacles
    assert len(actuator.commands) == 4
    assert actuator.neutral
