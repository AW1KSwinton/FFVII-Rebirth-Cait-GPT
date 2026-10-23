from __future__ import annotations

import math

from ff7r_agent.time_aware_calibration import (
    CalibrationSample,
    analyze_calibration_samples,
    summarize_calibration_runs,
)


def _samples(speeds: list[float], yaws: list[float] | None = None, dt: float = 0.1) -> list[CalibrationSample]:
    yaws = yaws or [0.0] * len(speeds)
    assert len(yaws) == len(speeds)
    result: list[CalibrationSample] = []
    x_cm = 0.0
    for index, (speed, yaw) in enumerate(zip(speeds, yaws)):
        if index:
            previous_speed = speeds[index - 1]
            previous_yaw = math.radians(yaws[index - 1])
            x_cm += previous_speed * math.cos(previous_yaw) * dt * 100.0
        result.append(
            CalibrationSample(
                sequence=index + 1,
                t=index * dt,
                timestamp_utc=f"2026-09-11T16:00:{index:02d}Z",
                x_cm=x_cm,
                y_cm=0.0,
                z_cm=0.0,
                yaw_degrees=yaw,
                vx_cm_s=speed * 100.0,
                vy_cm_s=0.0,
                vz_cm_s=0.0,
                grounded=True,
                movement_mode=1,
            )
        )
    return result


def test_straight_sprint_extracts_speed_acceleration_and_braking() -> None:
    samples = _samples([0.0, 1.0, 3.0, 5.0, 7.0, 8.2, 8.4, 8.4, 8.4, 7.0, 5.0, 2.0, 0.5])
    summary = analyze_calibration_samples(samples, scenario="straight-sprint")

    assert summary["peak_speed_m_s"] == 8.4
    assert summary["steady_speed_m_s"] >= 8.2
    assert summary["acceleration"]["seconds_to_90pct_steady"] is not None
    assert summary["braking"]["seconds_90pct_to_0_6_m_s"] is not None
    assert summary["turn"]["turn_detected"] == 0
    assert summary["grounded_fraction"] == 1.0


def test_turn_run_measures_angle_retention_and_time_loss() -> None:
    speeds = [8.0, 8.0, 8.0, 8.0, 8.0, 7.8, 7.0, 6.0, 5.0, 6.0, 7.4, 8.0, 8.0, 8.0]
    yaws = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 10.0, 30.0, 60.0, 90.0, 90.0, 90.0, 90.0, 90.0]
    summary = analyze_calibration_samples(
        _samples(speeds, yaws), scenario="turn-90", target_turn_degrees=90.0
    )
    turn = summary["turn"]

    assert turn["turn_detected"] == 1
    assert 85.0 <= turn["observed_turn_degrees"] <= 95.0
    assert turn["entry_speed_m_s"] >= 7.5
    assert 0.5 <= turn["minimum_speed_retention"] < 1.0
    assert turn["equivalent_time_loss_seconds"] >= 0.0
    assert turn["time_loss_seconds_per_radian"] >= 0.0
    assert abs(turn["target_angle_error_degrees"]) <= 5.0


def test_duplicate_sequences_are_ignored() -> None:
    samples = _samples([0.0, 2.0, 4.0, 6.0, 8.0, 8.0])
    duplicate = samples[2]
    summary = analyze_calibration_samples(samples + [duplicate], scenario="straight-sprint")
    assert summary["sample_count"] == len(samples)


def test_summary_produces_non_authoritative_model_suggestions() -> None:
    straight = analyze_calibration_samples(
        _samples([0.0, 2.0, 4.0, 6.0, 8.0, 8.0, 8.0]), scenario="straight-sprint"
    )
    turn = analyze_calibration_samples(
        _samples(
            [8.0, 8.0, 8.0, 7.5, 6.0, 5.0, 6.5, 8.0, 8.0],
            [0.0, 0.0, 0.0, 10.0, 35.0, 65.0, 90.0, 90.0, 90.0],
        ),
        scenario="turn-90",
        target_turn_degrees=90.0,
    )
    combined = summarize_calibration_runs([{"summary": straight}, {"summary": turn}])

    assert combined["straight_run_count"] == 1
    assert combined["turn_run_count"] == 1
    assert combined["suggested_model"]["straight_speed_m_s"] is not None
    assert combined["suggested_model"]["turn_penalty_s_per_rad"] is not None
    assert combined["suggested_model"]["narrow_portal_penalty_s_per_m"] is None
