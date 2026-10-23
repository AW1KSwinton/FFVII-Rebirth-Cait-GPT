"""Predictive local-navigation primitives.

The runtime exporter may provide measured clearance through the optional
sequence-bound ``navigation`` telemetry sidecar. This module keeps the
planning math independent from input and UE4SS so it can be replay-tested
without launching the game.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from statistics import median
from typing import Any, Iterable, Sequence

from .models import Vec3


DEFAULT_MOVEMENT_SPEED_MPS = 2.0
DEFAULT_REACTION_SECONDS = 0.25
DEFAULT_BRAKING_SECONDS = 0.35
DEFAULT_CHARACTER_MARGIN_M = 0.65
MIN_MEANINGFUL_DISPLACEMENT_M = 0.10


@dataclass(frozen=True)
class MotionSample:
    """One timestamped horizontal player position in Unreal centimetres."""

    timestamp_s: float
    position: Vec3
    sequence: int | None = None


@dataclass(frozen=True)
class VelocityEstimate:
    speed_mps: float
    bearing_degrees: float | None
    sample_count: int
    elapsed_seconds: float
    confidence: str


@dataclass(frozen=True)
class PlannedMotion:
    requested_distance_m: float
    clear_distance_m: float | None
    speed_mps: float
    stopping_buffer_m: float
    planned_distance_m: float
    hold_ms: int
    confidence: str
    outcome: str
    reason: str


@dataclass(frozen=True)
class HeadingCandidate:
    direction: str
    bearing_degrees: float
    clear_distance_m: float | None
    progress_m: float
    confidence: str = "unknown"
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class HeadingBlocker:
    """A recently failed local heading that must not be immediately retried."""

    position: Vec3
    bearing_degrees: float | None = None
    direction: str | None = None
    map_id: str | None = None
    radius_m: float = 4.0
    heading_window_degrees: float = 55.0
    source: str = "movement_history"
    reason: str = "recent_no_progress"


@dataclass(frozen=True)
class HeadingChoice:
    direction: str | None
    score: float | None
    clear_distance_m: float | None
    reason: str
    confidence: str
    ranked: tuple[dict[str, Any], ...]


def _normalise_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def _distance_m(first: Vec3, second: Vec3) -> float:
    return math.hypot(second.x - first.x, second.y - first.y) / 100.0


def _parse_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


def motion_sample_from_state(state: Any) -> MotionSample | None:
    """Convert a GameState-like object into a motion sample."""
    player = getattr(state, "player", None)
    position = getattr(player, "position", None)
    timestamp = _parse_timestamp(getattr(state, "timestamp_utc", None))
    if position is None or timestamp is None:
        return None
    return MotionSample(timestamp, position, getattr(state, "sequence", None))


def estimate_velocity(samples: Sequence[MotionSample] | Iterable[MotionSample]) -> VelocityEstimate:
    """Estimate robust horizontal speed and recent travel bearing."""
    ordered = sorted(samples, key=lambda item: (item.timestamp_s, item.sequence or 0))
    intervals: list[tuple[float, float, float]] = []
    for previous, current in zip(ordered, ordered[1:]):
        elapsed = current.timestamp_s - previous.timestamp_s
        if elapsed <= 0.02:
            continue
        distance = _distance_m(previous.position, current.position)
        intervals.append((distance / elapsed, elapsed, distance))
    if not intervals:
        return VelocityEstimate(0.0, None, len(ordered), 0.0, "unknown")
    speed = float(median([item[0] for item in intervals]))
    elapsed = sum(item[1] for item in intervals)
    bearing = None
    for previous, current in reversed(list(zip(ordered, ordered[1:]))):
        dx = current.position.x - previous.position.x
        dy = current.position.y - previous.position.y
        if math.hypot(dx, dy) / 100.0 >= MIN_MEANINGFUL_DISPLACEMENT_M:
            bearing = math.degrees(math.atan2(dy, dx))
            break
    confidence = "high" if len(intervals) >= 3 else "calibrating"
    return VelocityEstimate(speed, bearing, len(ordered), elapsed, confidence)


def stopping_buffer_m(
    speed_mps: float,
    *,
    reaction_seconds: float = DEFAULT_REACTION_SECONDS,
    braking_seconds: float = DEFAULT_BRAKING_SECONDS,
    character_margin_m: float = DEFAULT_CHARACTER_MARGIN_M,
) -> float:
    """Return a conservative distance to leave before a measured obstacle."""
    speed = max(0.0, float(speed_mps))
    reaction = max(0.0, float(reaction_seconds))
    braking = max(0.0, float(braking_seconds))
    margin = max(0.1, float(character_margin_m))
    return margin + speed * (reaction + braking)


def plan_motion_hold(
    requested_distance_m: float,
    clear_distance_m: float | None,
    speed_mps: float | None,
    *,
    maximum_ms: int = 5000,
    fallback_speed_mps: float = DEFAULT_MOVEMENT_SPEED_MPS,
    reaction_seconds: float = DEFAULT_REACTION_SECONDS,
    braking_seconds: float = DEFAULT_BRAKING_SECONDS,
    character_margin_m: float = DEFAULT_CHARACTER_MARGIN_M,
) -> PlannedMotion:
    """Plan one hold that ends before the measured clearance boundary."""
    requested = float(requested_distance_m)
    if not math.isfinite(requested) or requested <= 0:
        raise ValueError("requested_distance_m must be positive and finite")
    maximum_ms = int(maximum_ms)
    if not 100 <= maximum_ms <= 20000:
        raise ValueError("maximum_ms must be between 100 and 20000")
    if clear_distance_m is not None:
        clear = float(clear_distance_m)
        if not math.isfinite(clear) or clear < 0:
            raise ValueError("clear_distance_m must be finite and non-negative")
    else:
        clear = None
    measured_speed = float(speed_mps) if speed_mps is not None else 0.0
    if not math.isfinite(measured_speed) or measured_speed < 0:
        raise ValueError("speed_mps must be finite and non-negative")
    speed = measured_speed or max(0.1, float(fallback_speed_mps))
    buffer = stopping_buffer_m(
        speed,
        reaction_seconds=reaction_seconds,
        braking_seconds=braking_seconds,
        character_margin_m=character_margin_m,
    )
    if clear is None:
        return PlannedMotion(requested, None, speed, buffer, 0.0, 0, "unknown", "perception_required", "no forward clearance measurement is available")
    available = clear - buffer
    if available <= 0.0:
        return PlannedMotion(requested, clear, speed, buffer, 0.0, 0, "high", "blocked", "measured clearance is inside the stopping buffer")
    planned = min(requested, available, speed * maximum_ms / 1000.0)
    hold_ms = max(100, min(maximum_ms, int(round(planned / speed * 1000.0))))
    planned = min(planned, speed * hold_ms / 1000.0)
    return PlannedMotion(requested, clear, speed, buffer, planned, hold_ms, "high" if speed_mps is not None else "calibrating", "planned", "hold ends before clearance minus stopping buffer")


def choose_heading(
    candidates: Sequence[HeadingCandidate],
    target_bearing_degrees: float,
    *,
    stopping_buffer_m: float,
    minimum_progress_m: float = 0.25,
    current_position: Vec3 | None = None,
    recent_blockers: Sequence[HeadingBlocker] = (),
) -> HeadingChoice:
    """Rank candidate corridors by route progress, clearance, and history."""
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        clear = candidate.clear_distance_m
        if clear is None:
            ranked.append({"direction": candidate.direction, "score": None, "blocked": True, "reason": "clearance_unknown"})
            continue
        available = clear - max(0.0, float(stopping_buffer_m))
        error = abs(_normalise_degrees(candidate.bearing_degrees - target_bearing_degrees))
        progress = max(0.0, float(candidate.progress_m))
        score = progress * max(0.0, math.cos(math.radians(error)))
        score += min(max(0.0, available), 5.0) * 0.15
        blocked = available <= 0 or progress < minimum_progress_m
        blocker_matches: list[dict[str, Any]] = []
        if current_position is not None:
            for blocker in recent_blockers:
                distance = _distance_m(current_position, blocker.position)
                heading_error = abs(_normalise_degrees(candidate.bearing_degrees - blocker.bearing_degrees)) if blocker.bearing_degrees is not None else None
                same_direction = blocker.direction is not None and _canonical_direction(candidate.direction) == _canonical_direction(blocker.direction)
                if distance <= max(0.1, float(blocker.radius_m)) and (same_direction or (heading_error is not None and heading_error <= max(1.0, float(blocker.heading_window_degrees)))):
                    blocker_matches.append({"source": blocker.source, "reason": blocker.reason, "distance_m": round(distance, 3), "heading_error_degrees": round(heading_error, 3) if heading_error is not None else None})
            if blocker_matches:
                blocked = True
        ranked.append({"direction": candidate.direction, "score": round(score, 4), "clear_distance_m": clear, "available_distance_m": round(available, 4), "heading_error_degrees": round(error, 3), "blocked": blocked, "blocked_by_history": bool(blocker_matches), "history_blockers": blocker_matches, "bearing_degrees": round(candidate.bearing_degrees, 3), "confidence": candidate.confidence, **(candidate.metadata or {})})
    viable = [item for item in ranked if not item.get("blocked") and item.get("score") is not None]
    if not viable:
        return HeadingChoice(None, None, None, "no candidate has safe clearance", "blocked", tuple(ranked))
    best = max(viable, key=lambda item: float(item["score"]))
    reason = "highest progress score among safe corridors"
    if any(item.get("blocked_by_history") for item in ranked):
        reason += "; recently failed headings quarantined"
    return HeadingChoice(str(best["direction"]), float(best["score"]), float(best["clear_distance_m"]), reason, "high", tuple(ranked))


def _canonical_direction(direction: str) -> str:
    return {"w": "forward", "forward": "forward", "s": "backward", "back": "backward", "backward": "backward", "a": "left", "left": "left", "d": "right", "right": "right"}.get(str(direction).strip().lower(), str(direction).strip().lower())


def replay_navigation_metrics(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarise action JSONL for regression tracking."""
    actions = list(records)
    movement = [item for item in actions if item.get("action") in {"move", "move_direction", "travel_forward_chunk", "travel_direction_chunk", "travel_vector_chunk", "probe_forward_movement"}]
    progressed = [item for item in movement if float(item.get("horizontal_displacement") or 0.0) >= MIN_MEANINGFUL_DISPLACEMENT_M]
    recoveries = [item for item in movement if str(item.get("direction", "")).lower() in {"back", "backward", "s", "left", "right", "a", "d"}]
    no_progress = [item for item in movement if float(item.get("horizontal_displacement") or 0.0) < MIN_MEANINGFUL_DISPLACEMENT_M]
    total_distance = sum(float(item.get("horizontal_displacement") or 0.0) for item in movement)
    total_hold_ms = sum(float(item.get("actual_hold_ms") or 0.0) for item in movement)
    stationary_hold_ms = sum(float(item.get("actual_hold_ms") or 0.0) for item in no_progress)
    potential_unnecessary_stops = [item for item in no_progress if str(item.get("release_reason", item.get("outcome", ""))).lower() in {"maximum_hold_elapsed", "pulse_limit_reached"}]
    collision_recoveries = duplicate_directions = history_quarantines = route_required_count = 0
    route_source_counts: dict[str, int] = {}
    previous_direction: str | None = None
    for index, item in enumerate(movement):
        direction = str(item.get("direction") or "forward").lower()
        if direction == previous_direction:
            duplicate_directions += 1
        if direction in {"back", "backward", "s", "left", "right", "a", "d"} and index > 0 and float(movement[index - 1].get("horizontal_displacement") or 0.0) < MIN_MEANINGFUL_DISPLACEMENT_M:
            collision_recoveries += 1
        if item.get("heading_blocked_for_retry"):
            history_quarantines += 1
        predictive = item.get("predictive_plan")
        if isinstance(predictive, dict):
            source = predictive.get("route_source")
            if source:
                route_source_counts[str(source)] = route_source_counts.get(str(source), 0) + 1
            if predictive.get("decision") == "route_required":
                route_required_count += 1
        previous_direction = direction
    route_required_count += sum(1 for item in actions if item.get("decision") == "route_required")
    return {"action_count": len(actions), "movement_action_count": len(movement), "progressed_movement_count": len(progressed), "no_progress_movement_count": len(no_progress), "recovery_action_count": len(recoveries), "collision_recovery_count": collision_recoveries, "potential_unnecessary_stop_count": len(potential_unnecessary_stops), "duplicate_direction_count": duplicate_directions, "history_quarantine_count": history_quarantines, "route_required_count": route_required_count, "route_source_counts": route_source_counts, "total_horizontal_distance_m": round(total_distance, 4), "total_hold_seconds": round(total_hold_ms / 1000.0, 4), "stationary_hold_seconds": round(stationary_hold_ms / 1000.0, 4), "progress_rate": round(len(progressed) / len(movement), 4) if movement else 0.0, "no_progress_rate": round(len(no_progress) / len(movement), 4) if movement else 0.0}
