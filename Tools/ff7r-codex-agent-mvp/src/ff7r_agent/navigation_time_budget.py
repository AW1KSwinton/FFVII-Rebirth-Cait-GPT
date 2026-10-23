"""Offline wall-clock accounting for recorded live navigation timelines.

The live adapter already records the events required for this analysis.  This
module deliberately post-processes those events rather than adding timing work
to the control loop.  Commanded-motion intervals override neutral-state labels,
so the headline categories are mutually exclusive and sum to the analyzed run
window.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping, Sequence


_TIME_KEYS = ("t", "monotonic_t")
_PHASES = (
    "commanded_motion",
    "neutral_telemetry_wait",
    "neutral_sprint_braking",
    "neutral_readiness_recovery",
    "neutral_corridor_reacquisition",
    "neutral_replanning",
    "neutral_arrival_confirmation",
    "neutral_other",
)


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _event_time(event: Mapping[str, Any]) -> float | None:
    for key in _TIME_KEYS:
        value = _finite_float(event.get(key))
        if value is not None:
            return value
    return None


@dataclass(frozen=True)
class _Interval:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def _merge_intervals(intervals: Iterable[_Interval]) -> list[_Interval]:
    ordered = sorted(
        (item for item in intervals if item.end > item.start),
        key=lambda item: (item.start, item.end),
    )
    merged: list[_Interval] = []
    for item in ordered:
        if not merged or item.start > merged[-1].end:
            merged.append(item)
            continue
        previous = merged[-1]
        merged[-1] = _Interval(previous.start, max(previous.end, item.end))
    return merged


def _overlap_seconds(interval: _Interval, blockers: Sequence[_Interval]) -> float:
    total = 0.0
    for blocker in blockers:
        if blocker.end <= interval.start:
            continue
        if blocker.start >= interval.end:
            break
        total += max(0.0, min(interval.end, blocker.end) - max(interval.start, blocker.start))
    return total


def _first_neutral_at_or_after(neutral_times: Sequence[float], start: float) -> float | None:
    for value in neutral_times:
        if value >= start:
            return value
    return None


def _command_intervals(events: Sequence[tuple[float, int, Mapping[str, Any]]]) -> list[_Interval]:
    neutral_times = [
        t for t, _, event in events if event.get("event") == "actuator_neutralized"
    ]
    intervals: list[_Interval] = []
    for t, _, event in events:
        name = event.get("event")
        wall_duration = max(0.0, _finite_float(event.get("command_wall_duration_seconds")) or 0.0)
        if name == "movement_pulse_completed":
            intervals.append(_Interval(t - wall_duration, t))
            continue
        if name != "movement_setpoint_issued":
            continue
        supervision = event.get("supervision")
        if not isinstance(supervision, Mapping):
            continue
        start = t - wall_duration
        deadline = _finite_float(supervision.get("deadline"))
        lease = _finite_float(supervision.get("lease_seconds"))
        end = deadline if deadline is not None else (t + max(0.0, lease or 0.0))
        neutral = _first_neutral_at_or_after(neutral_times, start)
        if neutral is not None:
            end = min(end, neutral)
        if end > start:
            intervals.append(_Interval(start, end))
    return _merge_intervals(intervals)


def _phase_after_event(current: str, event: Mapping[str, Any]) -> str:
    name = str(event.get("event") or "")
    reason = str(event.get("reason") or "")

    if name == "actuator_neutralized":
        if reason == "sprint_to_pulse_braking":
            return "neutral_sprint_braking"
        if "readiness" in reason or reason == "telemetry_advancement_timeout":
            return "neutral_readiness_recovery"
        if "replan" in reason or "static_obstacle" in reason:
            return "neutral_replanning"
        return current if current.startswith("neutral_") else "neutral_other"

    if name in {
        "readiness_loss_triggered",
        "telemetry_advancement_timeout_recovery_started",
    } or name.startswith("follower_paused_for_readiness"):
        return "neutral_readiness_recovery"
    if name in {
        "readiness_reacquired_route_state",
        "readiness_recovery_failed",
    } or name.startswith("follower_resumed_after_readiness"):
        return "neutral_other"

    if name == "post_command_boundary_ambiguity_retry":
        return "neutral_corridor_reacquisition"
    if name in {
        "post_command_boundary_ambiguity_retry_failed",
        "post_command_corridor_reacquisition_rejected",
        "post_command_observation",
    } and current == "neutral_corridor_reacquisition":
        return "neutral_other"

    if name.startswith("follower_paused_for_sprint_braking"):
        return "neutral_sprint_braking"
    if name.startswith("follower_resumed_after_sprint_braking") or name == "sprint_low_speed_pulse_handoff_proven":
        return "neutral_other"

    if name in {
        "static_obstacle_replan_intercepted_readiness_failure",
        "static_obstacle_replan_started",
    }:
        return "neutral_replanning"
    if name in {
        "static_obstacle_replanned_and_reauthorised",
        "static_obstacle_replan_failed",
    }:
        return "neutral_other"

    if name == "control_cycle" and str(event.get("follower_state") or "") == "ARRIVED":
        return "neutral_arrival_confirmation"
    if name == "destination_reached_after_readiness":
        return "neutral_arrival_confirmation"

    if name == "waiting_for_next_telemetry" and current == "neutral_other":
        return "neutral_telemetry_wait"
    if name in {"fresh_next_telemetry_acquired", "fresh_telemetry_observation_acquired"} and current == "neutral_telemetry_wait":
        return "neutral_other"

    return current


def _extract_results(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        timeline = payload.get("timeline")
        if isinstance(timeline, list):
            return [payload]
        attempts = payload.get("attempts")
        if isinstance(attempts, list):
            results: list[Mapping[str, Any]] = []
            for attempt in attempts:
                results.extend(_extract_results(attempt))
            return results
    if isinstance(payload, list):
        results = []
        for item in payload:
            results.extend(_extract_results(item))
        return results
    raise ValueError("navigation result must contain a timeline or attempts")


def summarize_navigation_attempt(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return mutually exclusive wall-clock categories for one adapter result."""
    raw_timeline = result.get("timeline")
    if not isinstance(raw_timeline, list):
        raise ValueError("navigation result timeline must be a list")
    events: list[tuple[float, int, Mapping[str, Any]]] = []
    for index, raw in enumerate(raw_timeline):
        if not isinstance(raw, Mapping):
            continue
        t = _event_time(raw)
        if t is not None:
            events.append((t, index, raw))
    events.sort(key=lambda item: (item[0], item[1]))

    commands = _command_intervals(events)
    if not commands:
        return {
            "analyzed": False,
            "reason": "no_movement_command_intervals",
            "state": result.get("state"),
            "movement_cycles": int(result.get("movement_cycles") or 0),
        }

    start = commands[0].start
    end_candidates = [t for t, _, _ in events if t >= start]
    end = max(end_candidates) if end_candidates else commands[-1].end
    end = max(end, commands[-1].end)
    if end <= start:
        return {
            "analyzed": False,
            "reason": "non_positive_run_window",
            "state": result.get("state"),
            "movement_cycles": int(result.get("movement_cycles") or 0),
        }

    phase_intervals: list[tuple[str, _Interval]] = []
    current = "neutral_other"
    cursor = start
    for t, _, event in events:
        if t < start:
            continue
        bounded_t = min(max(t, start), end)
        if bounded_t > cursor:
            phase_intervals.append((current, _Interval(cursor, bounded_t)))
        current = _phase_after_event(current, event)
        cursor = bounded_t
        if cursor >= end:
            break
    if cursor < end:
        phase_intervals.append((current, _Interval(cursor, end)))

    durations = {phase: 0.0 for phase in _PHASES}
    durations["commanded_motion"] = sum(item.duration for item in commands)
    for phase, interval in phase_intervals:
        if phase == "commanded_motion":
            continue
        durations.setdefault(phase, 0.0)
        durations[phase] += max(0.0, interval.duration - _overlap_seconds(interval, commands))

    total = end - start
    accounted = sum(durations.values())
    # Floating point clipping/merging can leave tiny residuals. Attribute only
    # numerical dust to neutral_other; a larger mismatch is surfaced explicitly.
    residual = total - accounted
    if abs(residual) <= 1.0e-6:
        durations["neutral_other"] += residual
        residual = 0.0

    pulse_count = sum(1 for _, _, event in events if event.get("event") == "movement_pulse_completed")
    supervised_count = sum(1 for _, _, event in events if event.get("event") == "movement_setpoint_issued")
    sprint_setpoint_count = sum(
        1
        for _, _, event in events
        if event.get("event") == "movement_setpoint_issued"
        and isinstance(event.get("supervision"), Mapping)
        and event["supervision"].get("sprint") is True
    )
    observed_displacement = sum(
        max(0.0, _finite_float(event.get("observed_displacement_m")) or 0.0)
        for _, _, event in events
        if event.get("event") == "post_command_observation"
    )
    counters = {
        "pulse_commands": pulse_count,
        "supervised_setpoints": supervised_count,
        "sprint_setpoints": sprint_setpoint_count,
        "sprint_braking_episodes": sum(
            1
            for _, _, event in events
            if event.get("event") == "actuator_neutralized"
            and event.get("reason") == "sprint_to_pulse_braking"
        ),
        "readiness_recovery_starts": sum(
            1
            for _, _, event in events
            if event.get("event") in {"readiness_loss_triggered", "telemetry_advancement_timeout_recovery_started"}
        ),
        "corridor_reacquisition_retries": sum(
            1 for _, _, event in events if event.get("event") == "post_command_boundary_ambiguity_retry"
        ),
        "static_replans": sum(
            1 for _, _, event in events if event.get("event") == "static_obstacle_replanned_and_reauthorised"
        ),
    }
    percentages = {
        phase: (seconds / total * 100.0 if total > 0.0 else 0.0)
        for phase, seconds in durations.items()
    }
    return {
        "analyzed": True,
        "state": result.get("state"),
        "reason": result.get("reason"),
        "movement_cycles": int(result.get("movement_cycles") or 0),
        "window_start_monotonic": start,
        "window_end_monotonic": end,
        "total_seconds": total,
        "durations_seconds": durations,
        "percentages": percentages,
        "accounting_residual_seconds": residual,
        "observed_displacement_m": observed_displacement,
        "counters": counters,
    }


def summarize_navigation_time_budget(
    payload: Any,
    *,
    overall_elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    """Aggregate one or more adapter results into a benchmark time budget."""
    attempts = [summarize_navigation_attempt(item) for item in _extract_results(payload)]
    analyzed = [item for item in attempts if item.get("analyzed") is True]
    durations = {phase: 0.0 for phase in _PHASES}
    counters = {
        "pulse_commands": 0,
        "supervised_setpoints": 0,
        "sprint_setpoints": 0,
        "sprint_braking_episodes": 0,
        "readiness_recovery_starts": 0,
        "corridor_reacquisition_retries": 0,
        "static_replans": 0,
    }
    movement_cycles = 0
    observed_displacement = 0.0
    adapter_seconds = 0.0
    for attempt in analyzed:
        adapter_seconds += float(attempt["total_seconds"])
        movement_cycles += int(attempt.get("movement_cycles") or 0)
        observed_displacement += float(attempt.get("observed_displacement_m") or 0.0)
        for phase in _PHASES:
            durations[phase] += float(attempt["durations_seconds"].get(phase, 0.0))
        for key in counters:
            counters[key] += int(attempt["counters"].get(key, 0))

    overall = _finite_float(overall_elapsed_seconds)
    outside = None
    if overall is not None:
        outside = max(0.0, overall - adapter_seconds)
    denominator = overall if overall is not None and overall > 0.0 else adapter_seconds
    percentages = {
        phase: (seconds / denominator * 100.0 if denominator > 0.0 else 0.0)
        for phase, seconds in durations.items()
    }
    if outside is not None:
        percentages["outside_adapter_attempts"] = outside / denominator * 100.0 if denominator > 0.0 else 0.0

    return {
        "attempt_count": len(attempts),
        "analyzed_attempt_count": len(analyzed),
        "adapter_attempt_seconds": adapter_seconds,
        "overall_elapsed_seconds": overall,
        "outside_adapter_attempts_seconds": outside,
        "durations_seconds": durations,
        "percentages_of_selected_total": percentages,
        "movement_cycles": movement_cycles,
        "observed_displacement_m": observed_displacement,
        "counters": counters,
        "attempts": attempts,
        "interpretation": {
            "commanded_motion": "actuator was commanded non-neutral; this is an upper bound on productive movement, not proof that every millisecond advanced toward the goal",
            "neutral_telemetry_wait": "actuator neutral while waiting for the next fresh telemetry sample",
            "outside_adapter_attempts": "benchmark wall time not represented by analyzed adapter run windows, e.g. inter-attempt orchestration/planning gaps",
        },
    }
