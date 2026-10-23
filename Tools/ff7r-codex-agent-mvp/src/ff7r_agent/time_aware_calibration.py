"""Read-only live calibration analysis for traversal-time-aware routing.

The recorder that feeds this module only observes existing telemetry. It never
requests or sends movement. These helpers turn normalized locomotion samples
into conservative measurements that can later replace the provisional timing
coefficients used by :mod:`ff7r_agent.time_aware_route`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import statistics
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class CalibrationSample:
    sequence: int
    t: float
    timestamp_utc: str
    x_cm: float
    y_cm: float
    z_cm: float
    yaw_degrees: float
    vx_cm_s: float
    vy_cm_s: float
    vz_cm_s: float
    grounded: bool | None
    movement_mode: int | None

    @property
    def horizontal_speed_m_s(self) -> float:
        return math.hypot(self.vx_cm_s, self.vy_cm_s) / 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "t": self.t,
            "timestamp_utc": self.timestamp_utc,
            "position_cm": {"x": self.x_cm, "y": self.y_cm, "z": self.z_cm},
            "yaw_degrees": self.yaw_degrees,
            "velocity_cm_s": {"x": self.vx_cm_s, "y": self.vy_cm_s, "z": self.vz_cm_s},
            "horizontal_speed_m_s": self.horizontal_speed_m_s,
            "grounded": self.grounded,
            "movement_mode": self.movement_mode,
        }


def _finite(value: Any) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("calibration values must be finite")
    return result


def sample_from_mapping(value: Mapping[str, Any]) -> CalibrationSample:
    position = value.get("position_cm")
    velocity = value.get("velocity_cm_s")
    if not isinstance(position, Mapping) or not isinstance(velocity, Mapping):
        raise ValueError("calibration sample requires position_cm and velocity_cm_s")
    return CalibrationSample(
        sequence=int(value["sequence"]),
        t=_finite(value["t"]),
        timestamp_utc=str(value.get("timestamp_utc") or ""),
        x_cm=_finite(position["x"]),
        y_cm=_finite(position["y"]),
        z_cm=_finite(position["z"]),
        yaw_degrees=_finite(value["yaw_degrees"]),
        vx_cm_s=_finite(velocity["x"]),
        vy_cm_s=_finite(velocity["y"]),
        vz_cm_s=_finite(velocity["z"]),
        grounded=value.get("grounded") if isinstance(value.get("grounded"), bool) else None,
        movement_mode=(
            int(value["movement_mode"])
            if value.get("movement_mode") is not None and not isinstance(value.get("movement_mode"), bool)
            else None
        ),
    )


def sample_from_game_state(state: Any, *, captured_monotonic: float) -> CalibrationSample:
    player = getattr(state, "player", None)
    locomotion = getattr(player, "locomotion", None) if player is not None else None
    rotation = getattr(player, "rotation", None) if player is not None else None
    if player is None or locomotion is None or rotation is None:
        raise ValueError("live calibration requires player rotation and locomotion telemetry")
    position = locomotion.feet_position
    velocity = locomotion.velocity_cm_s
    return CalibrationSample(
        sequence=int(state.sequence),
        t=_finite(captured_monotonic),
        timestamp_utc=str(state.timestamp_utc),
        x_cm=_finite(position.x),
        y_cm=_finite(position.y),
        z_cm=_finite(position.z),
        yaw_degrees=_finite(rotation.yaw),
        vx_cm_s=_finite(velocity.x),
        vy_cm_s=_finite(velocity.y),
        vz_cm_s=_finite(velocity.z),
        grounded=locomotion.grounded,
        movement_mode=locomotion.movement_mode,
    )


def _ordered_unique(samples: Iterable[CalibrationSample]) -> list[CalibrationSample]:
    by_sequence: dict[int, CalibrationSample] = {}
    for sample in samples:
        by_sequence.setdefault(sample.sequence, sample)
    result = sorted(by_sequence.values(), key=lambda item: (item.t, item.sequence))
    if len(result) < 3:
        raise ValueError("calibration requires at least three advancing telemetry samples")
    for first, second in zip(result, result[1:]):
        if second.t <= first.t:
            raise ValueError("calibration sample times must advance")
    return result


def _horizontal_distance_m(first: CalibrationSample, second: CalibrationSample) -> float:
    return math.hypot(second.x_cm - first.x_cm, second.y_cm - first.y_cm) / 100.0


def _shortest_angle_delta_degrees(first: float, second: float) -> float:
    return (second - first + 180.0) % 360.0 - 180.0


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _distance_between(samples: Sequence[CalibrationSample], start: int, end: int) -> float:
    return sum(
        _horizontal_distance_m(first, second)
        for first, second in zip(samples[start:end], samples[start + 1 : end + 1])
    )


def _steady_speed(speeds: Sequence[float]) -> float:
    positive = sorted((value for value in speeds if value > 0.5), reverse=True)
    if not positive:
        return 0.0
    count = max(1, math.ceil(len(positive) * 0.20))
    return float(statistics.median(positive[:count]))


def _acceleration_metrics(samples: Sequence[CalibrationSample], steady_speed: float) -> dict[str, float | None]:
    if steady_speed <= 0.0:
        return {"seconds_to_90pct_steady": None, "distance_to_90pct_steady_m": None}
    threshold = steady_speed * 0.90
    start = next((i for i, sample in enumerate(samples) if sample.horizontal_speed_m_s <= max(0.75, steady_speed * 0.20)), 0)
    reached = next((i for i in range(start, len(samples)) if samples[i].horizontal_speed_m_s >= threshold), None)
    if reached is None or reached <= start:
        return {"seconds_to_90pct_steady": None, "distance_to_90pct_steady_m": None}
    return {
        "seconds_to_90pct_steady": samples[reached].t - samples[start].t,
        "distance_to_90pct_steady_m": _distance_between(samples, start, reached),
    }


def _braking_metrics(samples: Sequence[CalibrationSample], steady_speed: float) -> dict[str, float | None]:
    if steady_speed <= 0.0:
        return {"seconds_90pct_to_0_6_m_s": None, "distance_90pct_to_0_6_m_s": None}
    peak_index = max(range(len(samples)), key=lambda index: samples[index].horizontal_speed_m_s)
    start = next(
        (i for i in range(peak_index + 1, len(samples)) if samples[i].horizontal_speed_m_s <= steady_speed * 0.90),
        None,
    )
    if start is None:
        return {"seconds_90pct_to_0_6_m_s": None, "distance_90pct_to_0_6_m_s": None}
    end = next((i for i in range(start + 1, len(samples)) if samples[i].horizontal_speed_m_s <= 0.60), None)
    if end is None:
        return {"seconds_90pct_to_0_6_m_s": None, "distance_90pct_to_0_6_m_s": None}
    return {
        "seconds_90pct_to_0_6_m_s": samples[end].t - samples[start].t,
        "distance_90pct_to_0_6_m_s": _distance_between(samples, start, end),
    }


def _turn_metrics(samples: Sequence[CalibrationSample]) -> dict[str, float | int | None]:
    deltas = [
        _shortest_angle_delta_degrees(first.yaw_degrees, second.yaw_degrees)
        for first, second in zip(samples, samples[1:])
    ]
    rates = [abs(delta) / (samples[i + 1].t - samples[i].t) for i, delta in enumerate(deltas)]
    active = [i for i, rate in enumerate(rates) if rate >= 20.0 and abs(deltas[i]) >= 1.0]
    if not active:
        return {
            "turn_detected": 0,
            "observed_turn_degrees": None,
            "turn_duration_seconds": None,
            "entry_speed_m_s": None,
            "minimum_turn_speed_m_s": None,
            "exit_speed_m_s": None,
            "minimum_speed_retention": None,
            "exit_speed_retention": None,
            "equivalent_time_loss_seconds": None,
            "time_loss_seconds_per_radian": None,
        }
    start_edge, end_edge = min(active), max(active)
    start = start_edge
    end = min(len(samples) - 1, end_edge + 1)
    observed_turn = abs(sum(deltas[start_edge : end_edge + 1]))
    if observed_turn < 15.0:
        return {**_turn_metrics_without_detection(), "observed_turn_degrees": observed_turn}

    before = [sample.horizontal_speed_m_s for sample in samples[max(0, start - 5) : start]]
    during = [sample.horizontal_speed_m_s for sample in samples[start : end + 1]]
    after = [sample.horizontal_speed_m_s for sample in samples[end + 1 : min(len(samples), end + 6)]]
    entry = _median(before) or samples[start].horizontal_speed_m_s
    minimum = min(during) if during else None
    exit_speed = _median(after) if after else samples[end].horizontal_speed_m_s

    recovery_end = end
    if entry > 0.5:
        for index in range(end + 1, len(samples)):
            recovery_end = index
            if samples[index].horizontal_speed_m_s >= entry * 0.90:
                break
            if samples[index].t - samples[end].t >= 2.0:
                break
    elapsed = samples[recovery_end].t - samples[start].t
    distance = _distance_between(samples, start, recovery_end) if recovery_end > start else 0.0
    equivalent_loss = None
    per_radian = None
    if entry > 0.5 and elapsed > 0.0:
        equivalent_loss = max(0.0, elapsed - distance / entry)
        radians = math.radians(observed_turn)
        if radians > 1e-9:
            per_radian = equivalent_loss / radians

    return {
        "turn_detected": 1,
        "turn_start_sample_index": start,
        "turn_end_sample_index": end,
        "recovery_end_sample_index": recovery_end,
        "observed_turn_degrees": observed_turn,
        "turn_duration_seconds": samples[end].t - samples[start].t,
        "entry_speed_m_s": entry,
        "minimum_turn_speed_m_s": minimum,
        "exit_speed_m_s": exit_speed,
        "minimum_speed_retention": (minimum / entry if minimum is not None and entry > 0.0 else None),
        "exit_speed_retention": (exit_speed / entry if exit_speed is not None and entry > 0.0 else None),
        "equivalent_time_loss_seconds": equivalent_loss,
        "time_loss_seconds_per_radian": per_radian,
    }


def _turn_metrics_without_detection() -> dict[str, float | int | None]:
    return {
        "turn_detected": 0,
        "observed_turn_degrees": None,
        "turn_duration_seconds": None,
        "entry_speed_m_s": None,
        "minimum_turn_speed_m_s": None,
        "exit_speed_m_s": None,
        "minimum_speed_retention": None,
        "exit_speed_retention": None,
        "equivalent_time_loss_seconds": None,
        "time_loss_seconds_per_radian": None,
    }


def analyze_calibration_samples(
    samples: Iterable[CalibrationSample],
    *,
    scenario: str,
    target_turn_degrees: float | None = None,
) -> dict[str, Any]:
    ordered = _ordered_unique(samples)
    speeds = [sample.horizontal_speed_m_s for sample in ordered]
    grounded_known = [sample.grounded for sample in ordered if sample.grounded is not None]
    duration = ordered[-1].t - ordered[0].t
    distance = _distance_between(ordered, 0, len(ordered) - 1)
    steady = _steady_speed(speeds)
    result: dict[str, Any] = {
        "scenario": scenario,
        "target_turn_degrees": target_turn_degrees,
        "sample_count": len(ordered),
        "duration_seconds": duration,
        "horizontal_distance_m": distance,
        "peak_speed_m_s": max(speeds),
        "steady_speed_m_s": steady,
        "mean_moving_speed_m_s": (
            statistics.fmean(value for value in speeds if value > 0.5)
            if any(value > 0.5 for value in speeds)
            else 0.0
        ),
        "grounded_fraction": (
            sum(1 for value in grounded_known if value is True) / len(grounded_known)
            if grounded_known else None
        ),
        "acceleration": _acceleration_metrics(ordered, steady),
        "braking": _braking_metrics(ordered, steady),
        "turn": _turn_metrics(ordered),
    }
    if target_turn_degrees is not None and result["turn"].get("observed_turn_degrees") is not None:
        result["turn"]["target_angle_error_degrees"] = (
            float(result["turn"]["observed_turn_degrees"]) - float(target_turn_degrees)
        )
    return result


def summarize_calibration_runs(payloads: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    runs = list(payloads)
    summaries = [run.get("summary") for run in runs if isinstance(run.get("summary"), Mapping)]
    straight_speeds = [
        float(summary["steady_speed_m_s"])
        for summary in summaries
        if str(summary.get("scenario") or "").startswith("straight")
        and isinstance(summary.get("steady_speed_m_s"), (int, float))
        and float(summary["steady_speed_m_s"]) > 0.0
    ]
    turn_coefficients: list[float] = []
    turn_records: list[dict[str, Any]] = []
    for summary in summaries:
        turn = summary.get("turn")
        if not isinstance(turn, Mapping) or not turn.get("turn_detected"):
            continue
        coefficient = turn.get("time_loss_seconds_per_radian")
        if isinstance(coefficient, (int, float)) and math.isfinite(float(coefficient)):
            turn_coefficients.append(float(coefficient))
        turn_records.append({
            "scenario": summary.get("scenario"),
            "target_turn_degrees": summary.get("target_turn_degrees"),
            "observed_turn_degrees": turn.get("observed_turn_degrees"),
            "entry_speed_m_s": turn.get("entry_speed_m_s"),
            "minimum_speed_retention": turn.get("minimum_speed_retention"),
            "exit_speed_retention": turn.get("exit_speed_retention"),
            "equivalent_time_loss_seconds": turn.get("equivalent_time_loss_seconds"),
            "time_loss_seconds_per_radian": coefficient,
        })

    suggested_speed = statistics.median(straight_speeds) if straight_speeds else None
    suggested_turn = statistics.median(turn_coefficients) if turn_coefficients else None
    return {
        "run_count": len(runs),
        "straight_run_count": len(straight_speeds),
        "turn_run_count": len(turn_records),
        "suggested_model": {
            "straight_speed_m_s": suggested_speed,
            "turn_penalty_s_per_rad": suggested_turn,
            "narrow_portal_threshold_m": None,
            "narrow_portal_penalty_s_per_m": None,
        },
        "turn_records": turn_records,
        "limitations": [
            "suggestions are calibration evidence, not authority to change live route selection",
            "portal-width coefficients remain uncalibrated",
            "slope and collision effects are not estimated by this first pass",
        ],
    }
