from __future__ import annotations

import pytest

from ff7r_agent.navigation_time_budget import (
    summarize_navigation_attempt,
    summarize_navigation_time_budget,
)


def _event(t: float, name: str, **extra):
    return {"event": name, "t": t, **extra}


def test_time_budget_is_mutually_exclusive_and_tracks_control_overhead():
    result = {
        "state": "ARRIVED",
        "reason": "follower_arrived",
        "movement_cycles": 2,
        "timeline": [
            _event(0.0, "movement_setpoint_issued", command_wall_duration_seconds=0.0,
                   supervision={"deadline": 2.0, "lease_seconds": 2.0, "sprint": True}),
            _event(0.5, "waiting_for_next_telemetry"),
            _event(2.0, "actuator_neutralized", reason="sprint_to_pulse_braking"),
            _event(2.6, "sprint_braking_sample"),
            _event(3.0, "follower_resumed_after_sprint_braking"),
            _event(3.0, "waiting_for_next_telemetry"),
            _event(3.4, "fresh_next_telemetry_acquired"),
            _event(3.6, "movement_pulse_completed", command_wall_duration_seconds=0.2),
            _event(3.6, "actuator_neutralized", reason="movement_pulse_completed"),
            _event(3.6, "waiting_for_next_telemetry"),
            _event(4.0, "post_command_boundary_ambiguity_retry"),
            _event(4.3, "post_command_observation", observed_displacement_m=1.25),
            _event(4.5, "control_cycle", follower_state="ARRIVED"),
            _event(5.0, "post_arrival_confirmation_complete"),
        ],
    }

    summary = summarize_navigation_attempt(result)

    assert summary["analyzed"] is True
    assert summary["total_seconds"] == pytest.approx(5.0)
    durations = summary["durations_seconds"]
    assert durations["commanded_motion"] == pytest.approx(2.2)
    assert durations["neutral_sprint_braking"] == pytest.approx(1.0)
    assert durations["neutral_telemetry_wait"] == pytest.approx(0.8)
    assert durations["neutral_corridor_reacquisition"] == pytest.approx(0.3)
    assert durations["neutral_arrival_confirmation"] == pytest.approx(0.5)
    assert durations["neutral_other"] == pytest.approx(0.2)
    assert sum(durations.values()) == pytest.approx(summary["total_seconds"])
    assert summary["accounting_residual_seconds"] == pytest.approx(0.0)
    assert summary["observed_displacement_m"] == pytest.approx(1.25)
    assert summary["counters"]["supervised_setpoints"] == 1
    assert summary["counters"]["pulse_commands"] == 1
    assert summary["counters"]["sprint_braking_episodes"] == 1
    assert summary["counters"]["corridor_reacquisition_retries"] == 1


def test_supervised_interval_is_truncated_by_explicit_neutralization():
    result = {
        "state": "STOPPED",
        "movement_cycles": 1,
        "timeline": [
            _event(10.0, "movement_setpoint_issued", command_wall_duration_seconds=0.0,
                   supervision={"deadline": 20.0, "lease_seconds": 10.0, "sprint": False}),
            _event(12.0, "actuator_neutralized", reason="readiness_loss_during_following"),
            _event(13.0, "readiness_reacquired_route_state"),
        ],
    }

    summary = summarize_navigation_attempt(result)

    assert summary["total_seconds"] == pytest.approx(3.0)
    assert summary["durations_seconds"]["commanded_motion"] == pytest.approx(2.0)
    assert summary["durations_seconds"]["neutral_readiness_recovery"] == pytest.approx(1.0)


def test_aggregate_exposes_time_outside_adapter_attempts():
    attempt = {
        "state": "ARRIVED",
        "movement_cycles": 1,
        "timeline": [
            _event(0.0, "movement_pulse_completed", command_wall_duration_seconds=0.2),
            _event(0.0, "actuator_neutralized", reason="movement_pulse_completed"),
            _event(1.0, "control_cycle", follower_state="ARRIVED"),
        ],
    }

    summary = summarize_navigation_time_budget([attempt, attempt], overall_elapsed_seconds=5.0)

    assert summary["attempt_count"] == 2
    assert summary["analyzed_attempt_count"] == 2
    assert summary["adapter_attempt_seconds"] == pytest.approx(2.4)
    assert summary["outside_adapter_attempts_seconds"] == pytest.approx(2.6)
    assert summary["movement_cycles"] == 2


def test_no_command_attempt_is_reported_without_fabricating_a_window():
    summary = summarize_navigation_attempt({
        "state": "STOPPED",
        "movement_cycles": 0,
        "timeline": [_event(1.0, "actuator_neutralized", reason="preflight_failed")],
    })

    assert summary == {
        "analyzed": False,
        "reason": "no_movement_command_intervals",
        "state": "STOPPED",
        "movement_cycles": 0,
    }
