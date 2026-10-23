"""Movement-free RoutePlan execution-state simulation.

This module intentionally has no dependency on the production navigation
package.  It accepts the serialized RoutePlan contract and synthetic
telemetry, then produces deterministic state/diagnostic events.  It never
generates a route and never emits movement commands.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


class ExecutionState(str, Enum):
    IDLE = "IDLE"
    ROUTE_READY = "ROUTE_READY"
    FOLLOWING = "FOLLOWING"
    WAYPOINT_REACHED = "WAYPOINT_REACHED"
    ARRIVED = "ARRIVED"
    STALLED = "STALLED"
    DEVIATED = "DEVIATED"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    FAILED = "FAILED"


class InvalidRoutePlan(ValueError):
    """Raised when a RoutePlan cannot be safely followed."""

    def __init__(self, issues: Sequence[str]):
        self.issues = tuple(str(issue) for issue in issues)
        super().__init__("invalid RoutePlan: " + "; ".join(self.issues))


class ReplayInputError(ValueError):
    """Raised for malformed replay envelopes or external invalidation events."""


class _InvalidTelemetry(ValueError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class Point:
    x: float
    y: float
    z: float

    def to_dict(self) -> dict[str, float]:
        return {"x": _rounded(self.x), "y": _rounded(self.y), "z": _rounded(self.z)}


@dataclass(frozen=True)
class RoutePlan:
    """The small normalized view needed by the follower.

    The original JSON is deliberately retained only as a mapping so callers
    can audit the exact accepted planner fields.  No planner object is
    imported or reconstructed here.
    """

    raw: Mapping[str, Any]
    requested_start: Point
    requested_end: Point
    resolved_start: Point
    resolved_end: Point
    start_ref: int
    end_ref: int
    polygon_corridor: tuple[int, ...]
    portals: tuple[Mapping[str, Any], ...]
    waypoints: tuple[Point, ...]
    total_estimated_path_length_m: float
    validation: Mapping[str, Any]


@dataclass(frozen=True)
class SimulatorConfig:
    """Tolerances are metres unless the field name says otherwise.

    Telemetry heading is Unreal ``FRotator.Yaw``: +X is 0 degrees, +Y is
    +90 degrees, positive signed yaw error means turn right, and negative
    signed yaw error means turn left.
    """

    world_units_per_meter: float = 100.0
    waypoint_arrival_radius_m: float = 0.5
    final_arrival_radius_m: float = 0.75
    first_waypoint_vertical_tolerance_m: float = 1.0
    waypoint_vertical_tolerance_m: float = 1.5
    overshoot_margin_m: float = 0.75
    overshoot_cross_track_m: float = 2.5
    # Intermediate pass-through is deliberately narrower than ordinary route
    # deviation.  It proves one adjacent incoming->outgoing corner crossing;
    # it never creates a new steering target or accepts an arbitrary polygon.
    pass_through_cross_track_m: float = 1.0
    pass_through_min_progress_m: float = 0.1
    pass_through_max_step_m: float = 3.0
    pass_through_min_forward_alignment: float = 0.5
    pass_through_min_turn_degrees: float = 15.0
    deviation_threshold_m: float = 3.0
    deviation_confirmations: int = 2
    stall_timeout_s: float = 5.0
    progress_epsilon_m: float = 0.25
    route_progress_epsilon: float = 0.01
    progress_window_s: float = 10.0
    oscillation_window_s: float = 8.0
    oscillation_band_m: float = 0.25
    oscillation_min_crossings: int = 3
    slow_distance_m: float = 2.0
    heading_slow_error_deg: float = 110.0
    straight_heading_tolerance_deg: float = 5.0

    def __post_init__(self) -> None:
        positive = (
        "world_units_per_meter",
        "waypoint_arrival_radius_m",
        "final_arrival_radius_m",
        "first_waypoint_vertical_tolerance_m",
        "waypoint_vertical_tolerance_m",
        "overshoot_margin_m",
            "overshoot_cross_track_m",
            "pass_through_cross_track_m",
            "pass_through_min_progress_m",
            "pass_through_max_step_m",
            "pass_through_min_turn_degrees",
            "deviation_threshold_m",
            "stall_timeout_s",
            "progress_epsilon_m",
            "route_progress_epsilon",
            "progress_window_s",
            "oscillation_window_s",
            "oscillation_band_m",
            "slow_distance_m",
            "heading_slow_error_deg",
            "straight_heading_tolerance_deg",
        )
        for name in positive:
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        if self.deviation_confirmations < 1:
            raise ValueError("deviation_confirmations must be positive")
        if self.oscillation_min_crossings < 1:
            raise ValueError("oscillation_min_crossings must be positive")
        if not 0.0 < self.pass_through_min_forward_alignment <= 1.0:
            raise ValueError("pass_through_min_forward_alignment must be in (0, 1]")
        if self.pass_through_min_turn_degrees > 180.0:
            raise ValueError("pass_through_min_turn_degrees must not exceed 180 degrees")


_ALLOWED_INVALIDATIONS = frozenset(
    {"topology_changed", "plan_stale", "polygon_ref_invalid", "world_changed"}
)

_ALLOWED_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.IDLE: frozenset({ExecutionState.ROUTE_READY, ExecutionState.FAILED}),
    ExecutionState.ROUTE_READY: frozenset(
        {ExecutionState.FOLLOWING, ExecutionState.REPLAN_REQUIRED, ExecutionState.FAILED}
    ),
    ExecutionState.FOLLOWING: frozenset(
        {
            ExecutionState.WAYPOINT_REACHED,
            ExecutionState.ARRIVED,
            ExecutionState.STALLED,
            ExecutionState.DEVIATED,
            ExecutionState.REPLAN_REQUIRED,
            ExecutionState.FAILED,
        }
    ),
    ExecutionState.WAYPOINT_REACHED: frozenset(
        {
            ExecutionState.FOLLOWING,
            ExecutionState.ARRIVED,
            ExecutionState.REPLAN_REQUIRED,
            ExecutionState.FAILED,
        }
    ),
    ExecutionState.STALLED: frozenset(
        {
            ExecutionState.FOLLOWING,
            ExecutionState.WAYPOINT_REACHED,
            ExecutionState.ARRIVED,
            ExecutionState.DEVIATED,
            ExecutionState.REPLAN_REQUIRED,
            ExecutionState.FAILED,
        }
    ),
    ExecutionState.DEVIATED: frozenset(
        {ExecutionState.REPLAN_REQUIRED, ExecutionState.FAILED}
    ),
    ExecutionState.ARRIVED: frozenset({ExecutionState.REPLAN_REQUIRED, ExecutionState.FAILED}),
    ExecutionState.REPLAN_REQUIRED: frozenset({ExecutionState.FAILED}),
    ExecutionState.FAILED: frozenset(),
}


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _rounded(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _parse_point(value: Any, *, allow_sequence: bool = False) -> Point:
    if isinstance(value, Mapping):
        fields = (value.get("x"), value.get("y"), value.get("z"))
    elif allow_sequence and isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 3:
            raise ValueError("point_sequence_length")
        fields = tuple(value)
    else:
        raise ValueError("point_not_object")
    if not all(_finite(field) for field in fields):
        raise ValueError("point_non_finite")
    return Point(*(float(field) for field in fields))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_route_plan(value: Any) -> RoutePlan:
    """Validate and normalize the accepted serialized planner schema."""

    issues: list[str] = []
    if not isinstance(value, Mapping):
        raise InvalidRoutePlan(("route_plan_not_object",))

    required = (
        "status",
        "reason_code",
        "requested_start",
        "requested_end",
        "resolved_start",
        "resolved_end",
        "start_ref",
        "end_ref",
        "polygon_corridor",
        "portals",
        "waypoints",
        "total_estimated_path_length_m",
        "validation",
        "movement_sent",
    )
    issues.extend(f"missing_{key}" for key in required if key not in value)
    if issues:
        raise InvalidRoutePlan(issues)

    if value.get("status") != "complete":
        issues.append("status_not_complete")
    if value.get("reason_code") != "route_plan_complete":
        issues.append("reason_code_not_route_plan_complete")
    if value.get("movement_sent") is not False:
        issues.append("movement_sent_must_be_false")

    parsed_points: dict[str, Point] = {}
    for key in ("requested_start", "requested_end", "resolved_start", "resolved_end"):
        try:
            parsed_points[key] = _parse_point(value[key])
        except ValueError as exc:
            issues.append(f"{key}_{exc}")

    for key in ("start_ref", "end_ref"):
        if not _is_int(value[key]) or value[key] <= 0:
            issues.append(f"{key}_must_be_positive_integer")

    corridor = value.get("polygon_corridor")
    if not isinstance(corridor, list) or not corridor:
        issues.append("polygon_corridor_must_be_nonempty_list")
    elif any(not _is_int(ref) or ref <= 0 for ref in corridor):
        issues.append("polygon_corridor_contains_invalid_reference")

    portals = value.get("portals")
    if not isinstance(portals, list):
        issues.append("portals_must_be_list")
    elif any(not isinstance(portal, Mapping) for portal in portals):
        issues.append("portals_contains_non_object")

    raw_waypoints = value.get("waypoints")
    waypoints: list[Point] = []
    if not isinstance(raw_waypoints, list) or not raw_waypoints:
        issues.append("waypoints_must_be_nonempty_list")
    else:
        for index, waypoint in enumerate(raw_waypoints):
            try:
                waypoints.append(_parse_point(waypoint))
            except ValueError as exc:
                issues.append(f"waypoint_{index}_{exc}")

    total_length = value.get("total_estimated_path_length_m")
    if not _finite(total_length) or float(total_length) < 0.0:
        issues.append("total_estimated_path_length_m_invalid")

    validation = value.get("validation")
    if not isinstance(validation, Mapping):
        issues.append("validation_must_be_object")
    elif validation.get("movement_sent") is True:
        issues.append("validation_movement_sent_must_not_be_true")

    if "resolved_start" in parsed_points and waypoints:
        if _distance_world(parsed_points["resolved_start"], waypoints[0]) > 2.0:
            issues.append("first_waypoint_does_not_match_resolved_start")
    if "resolved_end" in parsed_points and waypoints:
        if _distance_world(parsed_points["resolved_end"], waypoints[-1]) > 2.0:
            issues.append("last_waypoint_does_not_match_resolved_end")

    if issues:
        raise InvalidRoutePlan(issues)

    return RoutePlan(
        raw=value,
        requested_start=parsed_points["requested_start"],
        requested_end=parsed_points["requested_end"],
        resolved_start=parsed_points["resolved_start"],
        resolved_end=parsed_points["resolved_end"],
        start_ref=int(value["start_ref"]),
        end_ref=int(value["end_ref"]),
        polygon_corridor=tuple(int(ref) for ref in corridor),
        portals=tuple(portal for portal in portals),
        waypoints=tuple(waypoints),
        total_estimated_path_length_m=float(total_length),
        validation=validation,
    )


def _distance_world(first: Point, second: Point) -> float:
    return math.sqrt(
        (second.x - first.x) ** 2
        + (second.y - first.y) ** 2
        + (second.z - first.z) ** 2
    )


def _distance_m(first: Point, second: Point, units_per_meter: float) -> float:
    return _distance_world(first, second) / units_per_meter


def _horizontal_distance_m(first: Point, second: Point, units_per_meter: float) -> float:
    return math.hypot(second.x - first.x, second.y - first.y) / units_per_meter


def _segment_length_m(first: Point, second: Point, units_per_meter: float) -> float:
    return _distance_m(first, second, units_per_meter)


def _project_to_segment(
    point: Point,
    first: Point,
    second: Point,
    units_per_meter: float,
    *,
    clamp: bool = True,
) -> tuple[float, float, float]:
    """Return (t, horizontal cross-track metres, segment metres)."""

    dx = second.x - first.x
    dy = second.y - first.y
    denominator = dx * dx + dy * dy
    if denominator <= 1e-12:
        return 0.0, _horizontal_distance_m(point, first, units_per_meter), 0.0
    raw_t = ((point.x - first.x) * dx + (point.y - first.y) * dy) / denominator
    t = max(0.0, min(1.0, raw_t)) if clamp else raw_t
    projected = Point(first.x + dx * t, first.y + dy * t, first.z + (second.z - first.z) * t)
    return t, _horizontal_distance_m(point, projected, units_per_meter), _segment_length_m(
        first, second, units_per_meter
    )


@dataclass(frozen=True)
class _ActiveRouteGeometry:
    """Geometry anchored to the current leg, never to an arbitrary route leg."""

    active_segment_index: int | None
    bounded_segment_indices: tuple[int, ...]
    route_progress_fraction: float | None
    active_cross_track_m: float | None
    neighbourhood_cross_track_m: float | None


def _active_route_geometry(
    waypoints: Sequence[Point],
    active_waypoint_index: int,
    point: Point,
    units_per_meter: float,
) -> _ActiveRouteGeometry:
    """Project only onto the active leg and a bounded diagnostic neighborhood.

    The incoming segment is authoritative while an actor is approaching an
    active waypoint. At route start, the outgoing segment is authoritative.
    The bounded neighborhood extends at most one segment before and two after
    that anchor; its nearest distance is diagnostic only and never drives
    progress or deviation decisions.
    """

    if len(waypoints) <= 1:
        cross = (
            _horizontal_distance_m(point, waypoints[0], units_per_meter)
            if waypoints
            else None
        )
        return _ActiveRouteGeometry(None, (), 0.0, cross, cross)

    active_segment = 0 if active_waypoint_index <= 0 else min(
        active_waypoint_index - 1, len(waypoints) - 2
    )
    bounded_start = max(0, active_segment - 1)
    bounded_end = min(len(waypoints) - 2, active_segment + 2)
    bounded_indices = tuple(range(bounded_start, bounded_end + 1))

    first = waypoints[active_segment]
    second = waypoints[active_segment + 1]
    active_t, active_cross, active_length = _project_to_segment(
        point, first, second, units_per_meter
    )
    segment_lengths = tuple(
        _segment_length_m(left, right, units_per_meter)
        for left, right in zip(waypoints, waypoints[1:])
    )
    total_length = sum(segment_lengths)
    distance_before = sum(segment_lengths[:active_segment])
    route_fraction = (
        max(0.0, min(1.0, (distance_before + active_length * active_t) / total_length))
        if total_length > 1e-12
        else 0.0
    )
    neighbourhood_cross = min(
        _project_to_segment(
            point,
            waypoints[index],
            waypoints[index + 1],
            units_per_meter,
        )[1]
        for index in bounded_indices
    )
    return _ActiveRouteGeometry(
        active_segment,
        bounded_indices,
        route_fraction,
        active_cross,
        neighbourhood_cross,
    )


def _normalize_heading(value: float) -> float:
    return value % 360.0


def _signed_heading_error(desired: float, actual: float) -> float:
    return (desired - actual + 180.0) % 360.0 - 180.0


class RouteFollower:
    """Deterministic execution-state model for one already-built RoutePlan."""

    def __init__(self, route_plan: Mapping[str, Any], config: SimulatorConfig | None = None):
        self.route = validate_route_plan(route_plan)
        self.config = config or SimulatorConfig()
        self.state = ExecutionState.IDLE
        self.current_waypoint_index = 0
        self._last_t: float | None = None
        self._last_position: Point | None = None
        self._last_heading: float | None = None
        self._last_meaningful_progress_t: float | None = None
        self._safety_pause_started_t: float | None = None
        self._safety_pause_total_s = 0.0
        self._progress_anchor_distance_m: float | None = None
        self._progress_anchor_route_fraction: float | None = None
        self._recent_distances: deque[tuple[float, float]] = deque()
        self._deviation_consecutive = 0
        self._oscillation_signs: deque[tuple[float, int]] = deque()
        self._oscillation_crossings = 0
        self._oscillation_triggered = False
        self._transition(ExecutionState.ROUTE_READY, 0.0, "route_ready_internal")

    @property
    def active_waypoint(self) -> Point | None:
        if self.current_waypoint_index >= len(self.route.waypoints):
            return None
        return self.route.waypoints[self.current_waypoint_index]

    @property
    def is_terminal(self) -> bool:
        return self.state in {
            ExecutionState.ARRIVED,
            ExecutionState.REPLAN_REQUIRED,
            ExecutionState.FAILED,
        }

    def route_ready_event(self, t: float = 0.0) -> dict[str, Any]:
        return self._event(t, "route_ready", reason_code="route_plan_complete")

    def _transition(
        self, new_state: ExecutionState, t: float, event: str, **fields: Any
    ) -> dict[str, Any] | None:
        if new_state == self.state:
            return None
        allowed = _ALLOWED_TRANSITIONS[self.state]
        if new_state not in allowed:
            raise RuntimeError(f"invalid execution transition {self.state.value}->{new_state.value}")
        self.state = new_state
        return self._event(t, event, **fields)

    def _event(self, t: float, event: str, **fields: Any) -> dict[str, Any]:
        metrics = self._metrics()
        result: dict[str, Any] = {
            "t": _rounded(t),
            "event": event,
            "state": self.state.value,
            "waypoint_index": self.current_waypoint_index,
            "distance_m": metrics["distance_m"],
            "distance_to_final_destination_m": metrics["distance_to_final_destination_m"],
            "best_distance_recent_m": metrics["best_distance_recent_m"],
            "elapsed_since_meaningful_progress_s": metrics["elapsed_since_meaningful_progress_s"],
            "route_progress_fraction": metrics["route_progress_fraction"],
            "cross_track_error_m": metrics["cross_track_error_m"],
            "desired_heading_deg": metrics["desired_heading_deg"],
            "heading_error_deg": metrics["heading_error_deg"],
            "turn_direction": metrics["turn_direction"],
            "suggested_speed_band": metrics["suggested_speed_band"],
        }
        result.update(fields)
        return result

    def _metrics(self) -> dict[str, Any]:
        position = self._last_position
        if position is None:
            return {
                "distance_m": None,
                "distance_to_final_destination_m": None,
                "best_distance_recent_m": None,
                "elapsed_since_meaningful_progress_s": None,
                "route_progress_fraction": None,
                "cross_track_error_m": None,
                "desired_heading_deg": None,
                "heading_error_deg": None,
                "turn_direction": "none",
                "suggested_speed_band": "stop",
            }

        target = self.active_waypoint
        distance = (
            _distance_m(position, target, self.config.world_units_per_meter)
            if target is not None
            else None
        )
        final_distance = _distance_m(
            position, self.route.resolved_end, self.config.world_units_per_meter
        )
        geometry = _active_route_geometry(
            self.route.waypoints,
            self.current_waypoint_index,
            position,
            self.config.world_units_per_meter,
        )
        route_fraction = (
            geometry.route_progress_fraction
            if geometry.active_cross_track_m is None
            or geometry.active_cross_track_m <= self.config.deviation_threshold_m
            else None
        )
        if route_fraction is None and len(self.route.waypoints) == 1:
            route_fraction = 1.0 if final_distance <= self.config.final_arrival_radius_m else 0.0
        desired, error, turn = self._heading_diagnostics(position, target)
        elapsed = (
            max(0.0, self._last_t - self._last_meaningful_progress_t)
            if self._last_t is not None and self._last_meaningful_progress_t is not None
            else None
        )
        if self.state in {ExecutionState.ARRIVED, ExecutionState.REPLAN_REQUIRED, ExecutionState.FAILED}:
            speed_band = "stop"
        elif distance is not None and distance <= self._arrival_radius_for(self.current_waypoint_index):
            speed_band = "stop"
        elif distance is not None and (
            distance <= self.config.slow_distance_m
            or (error is not None and abs(error) >= self.config.heading_slow_error_deg)
        ):
            speed_band = "slow"
        else:
            speed_band = "normal"
        return {
            "distance_m": _rounded(distance),
            "distance_to_final_destination_m": _rounded(final_distance),
            "best_distance_recent_m": _rounded(self._best_recent_distance()),
            "elapsed_since_meaningful_progress_s": _rounded(elapsed),
            "route_progress_fraction": _rounded(route_fraction),
            "cross_track_error_m": _rounded(geometry.active_cross_track_m),
            "desired_heading_deg": _rounded(desired),
            "heading_error_deg": _rounded(error),
            "turn_direction": turn,
            "suggested_speed_band": speed_band,
        }

    def progress_accounting(self, t: float | None = None) -> dict[str, Any]:
        """Expose active-movement stall accounting for adapter evidence."""

        current_t = self._last_t if t is None else float(t)
        elapsed = None
        if current_t is not None and self._last_meaningful_progress_t is not None:
            elapsed = max(0.0, current_t - self._last_meaningful_progress_t)
        return {
            "last_meaningful_progress_t": self._last_meaningful_progress_t,
            "active_stall_elapsed_seconds": elapsed,
            "safety_pause_started_t": self._safety_pause_started_t,
            "safety_pause_total_seconds": self._safety_pause_total_s,
        }

    def pause_for_safety(self, t: float) -> dict[str, Any]:
        """Freeze stall time while the adapter is deliberately neutral."""

        pause_t = float(t)
        if self._safety_pause_started_t is None:
            self._safety_pause_started_t = pause_t
        return self.progress_accounting(pause_t)

    def resume_after_safety_pause(self, t: float) -> dict[str, Any]:
        """Remove only the neutral pause duration from active stall time."""

        resume_t = float(t)
        started = self._safety_pause_started_t
        if started is not None:
            paused = max(0.0, resume_t - started)
            self._safety_pause_total_s += paused
            if self._last_meaningful_progress_t is not None:
                self._last_meaningful_progress_t += paused
            self._safety_pause_started_t = None
        return self.progress_accounting(resume_t)

    def _heading_diagnostics(
        self, position: Point, target: Point | None
    ) -> tuple[float | None, float | None, str]:
        """Return Unreal FRotator.Yaw diagnostics.

        Unreal yaw uses +X=0 degrees and +Y=+90 degrees. A positive signed
        desired-minus-actual yaw error means turn right; a negative error
        means turn left.
        """

        if target is None:
            return None, None, "none"
        dx = target.x - position.x
        dy = target.y - position.y
        if math.hypot(dx, dy) <= 1e-9:
            return None, None, "none"
        desired = _normalize_heading(math.degrees(math.atan2(dy, dx)))
        if self._last_heading is None:
            return desired, None, "unknown"
        error = _signed_heading_error(desired, self._last_heading)
        if abs(error) <= self.config.straight_heading_tolerance_deg:
            turn = "straight"
        elif error > 0.0:
            turn = "right"
        else:
            turn = "left"
        return desired, error, turn

    def _arrival_radius_for(self, index: int) -> float:
        return (
            self.config.final_arrival_radius_m
            if index == len(self.route.waypoints) - 1
            else self.config.waypoint_arrival_radius_m
        )

    def _best_recent_distance(self) -> float | None:
        if not self._recent_distances:
            return None
        return min(distance for _, distance in self._recent_distances)

    def _reset_progress(self, t: float, position: Point) -> None:
        target = self.active_waypoint
        distance = (
            _distance_m(position, target, self.config.world_units_per_meter)
            if target is not None
            else 0.0
        )
        geometry = _active_route_geometry(
            self.route.waypoints,
            self.current_waypoint_index,
            position,
            self.config.world_units_per_meter,
        )
        fraction = (
            geometry.route_progress_fraction
            if geometry.active_cross_track_m is None
            or geometry.active_cross_track_m <= self.config.deviation_threshold_m
            else None
        )
        self._last_meaningful_progress_t = t
        self._progress_anchor_distance_m = distance
        self._progress_anchor_route_fraction = fraction
        self._recent_distances.clear()
        self._recent_distances.append((t, distance))
        self._reset_oscillation_episode()

    def _reset_oscillation_episode(self) -> None:
        """Start a new oscillation episode without changing waypoint progress."""

        self._oscillation_signs.clear()
        self._oscillation_crossings = 0
        self._oscillation_triggered = False

    def _is_overshot(
        self,
        position: Point,
        index: int,
        previous_position: Point | None,
    ) -> bool:
        if previous_position is None or index <= 0 or index >= len(self.route.waypoints) - 1:
            return False
        previous = self.route.waypoints[index - 1]
        target = self.route.waypoints[index]
        following = self.route.waypoints[index + 1]
        previous_t, _, previous_segment_length = _project_to_segment(
            previous_position,
            previous,
            target,
            self.config.world_units_per_meter,
            clamp=False,
        )
        projected_t, cross_track, segment_length = _project_to_segment(
            position,
            previous,
            target,
            self.config.world_units_per_meter,
            clamp=False,
        )
        outgoing_t, outgoing_cross_track, outgoing_segment_length = _project_to_segment(
            position,
            target,
            following,
            self.config.world_units_per_meter,
            clamp=False,
        )
        if segment_length <= 1e-9 or previous_segment_length <= 1e-9 or outgoing_segment_length <= 1e-9:
            return False
        overrun_m = (projected_t - 1.0) * segment_length
        step_x = position.x - previous_position.x
        step_y = position.y - previous_position.y
        step_m = math.hypot(step_x, step_y) / self.config.world_units_per_meter
        outgoing_x = (following.x - target.x) / (
            outgoing_segment_length * self.config.world_units_per_meter
        )
        outgoing_y = (following.y - target.y) / (
            outgoing_segment_length * self.config.world_units_per_meter
        )
        forward_alignment = (
            (step_x * outgoing_x + step_y * outgoing_y) / (step_m * self.config.world_units_per_meter)
            if step_m > 1e-9
            else -1.0
        )
        # Keep the established straight-leg overshoot behavior for valid
        # progress samples; the bounded step requirement belongs to the new
        # corner-crossing proof below.
        return (
            0.0 <= previous_t <= 1.0
            and projected_t > 1.0
            and overrun_m >= self.config.overshoot_margin_m
            and cross_track <= self.config.overshoot_cross_track_m
            and 0.0 <= outgoing_t <= 1.0
            and outgoing_cross_track <= self.config.pass_through_cross_track_m
            and forward_alignment >= self.config.pass_through_min_forward_alignment
            # Preserve the established straight-leg overshoot behavior for
            # valid progress samples; bounded step size is specific to the
            # new corner-crossing proof below.
        )

    def _pass_through_proof(
        self,
        previous_position: Point | None,
        position: Point,
        index: int,
    ) -> dict[str, Any] | None:
        """Prove one adjacent-segment intermediate waypoint crossing.

        This is a progress proof, not a corridor expansion.  The previous
        sample must be on the incoming leg and the current sample must be on
        the immediately following leg.  Both samples stay close to their
        route segments, the observed step is bounded and points generally in
        the outgoing direction, and the current actor-origin height remains
        within the existing waypoint vertical envelope.  The live adapter's
        independent corridor/portal authorization remains authoritative before
        and after every movement command.
        """

        if previous_position is None or index <= 0 or index >= len(self.route.waypoints) - 1:
            return None
        previous = self.route.waypoints[index - 1]
        target = self.route.waypoints[index]
        following = self.route.waypoints[index + 1]
        incoming_t, incoming_cross_track, incoming_length = _project_to_segment(
            previous_position,
            previous,
            target,
            self.config.world_units_per_meter,
            clamp=False,
        )
        outgoing_t, outgoing_cross_track, outgoing_length = _project_to_segment(
            position,
            target,
            following,
            self.config.world_units_per_meter,
            clamp=False,
        )
        if incoming_length <= 1e-9 or outgoing_length <= 1e-9:
            return None

        incoming_x = (target.x - previous.x) / (
            incoming_length * self.config.world_units_per_meter
        )
        incoming_y = (target.y - previous.y) / (
            incoming_length * self.config.world_units_per_meter
        )
        outgoing_x = (following.x - target.x) / (
            outgoing_length * self.config.world_units_per_meter
        )
        outgoing_y = (following.y - target.y) / (
            outgoing_length * self.config.world_units_per_meter
        )
        turn_cosine = max(-1.0, min(1.0, incoming_x * outgoing_x + incoming_y * outgoing_y))
        turn_degrees = math.degrees(math.acos(turn_cosine))
        if turn_degrees < self.config.pass_through_min_turn_degrees:
            return None

        step_x = position.x - previous_position.x
        step_y = position.y - previous_position.y
        step_m = math.hypot(step_x, step_y) / self.config.world_units_per_meter
        if step_m <= 1e-9 or step_m > self.config.pass_through_max_step_m:
            return None

        incoming_vertical_m = abs(
            previous_position.z
            - (previous.z + (target.z - previous.z) * max(0.0, min(1.0, incoming_t)))
        ) / self.config.world_units_per_meter
        outgoing_vertical_m = abs(
            position.z
            - (target.z + (following.z - target.z) * max(0.0, min(1.0, outgoing_t)))
        ) / self.config.world_units_per_meter
        if (
            incoming_t < 0.0
            or incoming_t > 1.0
            or outgoing_t <= 0.0
            or outgoing_t > 1.0
            or incoming_cross_track > self.config.pass_through_cross_track_m
            or outgoing_cross_track > self.config.pass_through_cross_track_m
            or incoming_vertical_m > self.config.waypoint_vertical_tolerance_m
            or outgoing_vertical_m > self.config.waypoint_vertical_tolerance_m
        ):
            return None

        forward_alignment = (step_x * outgoing_x + step_y * outgoing_y) / (
            step_m * self.config.world_units_per_meter
        )
        if forward_alignment < self.config.pass_through_min_forward_alignment:
            return None

        segment_lengths = tuple(
            _segment_length_m(left, right, self.config.world_units_per_meter)
            for left, right in zip(self.route.waypoints, self.route.waypoints[1:])
        )
        waypoint_progress_m = sum(segment_lengths[:index])
        previous_progress_m = sum(segment_lengths[: index - 1]) + incoming_t * incoming_length
        current_progress_m = waypoint_progress_m + outgoing_t * outgoing_length
        if current_progress_m <= waypoint_progress_m + self.config.pass_through_min_progress_m:
            return None
        if current_progress_m <= previous_progress_m + self.config.pass_through_min_progress_m:
            return None

        return {
            "method": "adjacent_segment_crossing",
            "passed_waypoint_index": index,
            "hard_capture_radius_m": self._arrival_radius_for(index),
            "distance_to_waypoint_m": _horizontal_distance_m(
                position, target, self.config.world_units_per_meter
            ),
            "incoming_projection_t": incoming_t,
            "outgoing_projection_t": outgoing_t,
            "incoming_cross_track_m": incoming_cross_track,
            "outgoing_cross_track_m": outgoing_cross_track,
            "incoming_vertical_error_m": incoming_vertical_m,
            "outgoing_vertical_error_m": outgoing_vertical_m,
            "step_m": step_m,
            "forward_alignment": forward_alignment,
            "turn_degrees": turn_degrees,
            "previous_route_progress_m": previous_progress_m,
            "current_route_progress_m": current_progress_m,
            "authorized_route_segment_indices": [index - 1, index],
        }

    def _advance_waypoints(
        self,
        t: float,
        position: Point,
        previous_position: Point | None,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while self.current_waypoint_index < len(self.route.waypoints):
            index = self.current_waypoint_index
            target = self.route.waypoints[index]
            distance = _distance_m(position, target, self.config.world_units_per_meter)
            horizontal_distance = _horizontal_distance_m(
                position,
                target,
                self.config.world_units_per_meter,
            )
            vertical_distance = abs(position.z - target.z) / self.config.world_units_per_meter
            vertical_tolerance = (
                self.config.first_waypoint_vertical_tolerance_m
                if index == 0
                else self.config.waypoint_vertical_tolerance_m
            )
            within_radius = (
                horizontal_distance <= self._arrival_radius_for(index)
                and vertical_distance <= vertical_tolerance
            )
            overshot = (
                index < len(self.route.waypoints) - 1
                and not within_radius
                and self._is_overshot(position, index, previous_position)
            )
            pass_through = (
                None
                if within_radius or overshot
                else self._pass_through_proof(previous_position, position, index)
            )
            if not within_radius and not overshot and pass_through is None:
                break

            reason = "within_arrival_radius" if within_radius else "passed_waypoint"
            # Reaching or safely passing a waypoint is meaningful progress for
            # the active leg. Record it before emitting the transition event so
            # the event's best-distance and progress diagnostics include the
            # arrival sample itself.
            self._reset_progress(t, position)
            event = self._transition(
                ExecutionState.WAYPOINT_REACHED,
                t,
                "waypoint_reached",
                reached_waypoint_index=index,
                waypoint_reach_reason=reason,
            )
            if event is not None:
                events.append(event)
            if overshot:
                overshoot_t, _, overshoot_segment_m = _project_to_segment(
                    position,
                    self.route.waypoints[index - 1],
                    target,
                    self.config.world_units_per_meter,
                    clamp=False,
                )
                events.append(
                    self._event(
                        t,
                        "waypoint_overshot",
                        overshot_waypoint_index=index,
                        overshoot_m=_rounded((overshoot_t - 1.0) * overshoot_segment_m),
                    )
                )
            if pass_through is not None:
                events.append(self._event(t, "waypoint_pass_through_proven", **pass_through))
            if index == len(self.route.waypoints) - 1:
                arrived = self._transition(
                    ExecutionState.ARRIVED,
                    t,
                    "destination_reached",
                    reached_waypoint_index=index,
                )
                if arrived is not None:
                    events.append(arrived)
                return events

            self.current_waypoint_index += 1
            self._reset_progress(t, position)
            advanced = self._transition(
                ExecutionState.FOLLOWING,
                t,
                "waypoint_advanced",
                advanced_from_index=index,
                active_waypoint_index=self.current_waypoint_index,
            )
            if advanced is not None:
                events.append(advanced)
            # A single fresh sample may prove one corner crossing, but it must
            # not be used to skip an unobserved chain of intermediate points.
            if pass_through is not None:
                break
        return events

    def commit_traversal_landing(
        self, start_index: int, end_index: int, sample: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Resume one declared crossing after the adapter proves its landing.

        Ordinary pass-through cannot consume the unobserved off-mesh leg.
        Only its two exact consecutive waypoints may be handed back here;
        the caller remains responsible for readiness and polygon containment.
        """
        declared = self.route.raw.get("traversal_segments") or ()
        if (type(start_index) is not int or type(end_index) is not int
                or not 0 <= start_index < end_index < len(self.route.waypoints)
                or end_index != start_index + 1
                or self.current_waypoint_index not in {start_index, end_index}
                or self.state not in {ExecutionState.FOLLOWING, ExecutionState.WAYPOINT_REACHED}
                or not any(isinstance(item, Mapping)
                           and item.get("start_waypoint_index") == start_index
                           and item.get("end_waypoint_index") == end_index
                           and item.get("link_class") == "step_over"
                           and item.get("direction") == "start_to_end" for item in declared)):
            raise ValueError("traversal_follower_handoff_invalid")
        if not isinstance(sample, Mapping) or not _finite(sample.get("t")) or not _finite(sample.get("heading")):
            raise ValueError("traversal_follower_telemetry_invalid")
        t = float(sample["t"])
        position = _parse_point(sample.get("position"), allow_sequence=True)
        heading = _normalize_heading(float(sample["heading"]))
        if self._last_t is not None and t < self._last_t:
            raise ValueError("traversal_follower_timestamp_regressed")
        landing = self.route.waypoints[end_index]
        along, lateral, _ = _project_to_segment(
            position, self.route.waypoints[start_index], landing, self.config.world_units_per_meter, clamp=False)
        distance = _horizontal_distance_m(position, landing, self.config.world_units_per_meter)
        if (not (distance <= 1.0 or (along >= 1.0 and lateral <= 1.5 and distance <= 2.0))
                or abs(position.z - landing.z) / self.config.world_units_per_meter
                > self.config.waypoint_vertical_tolerance_m):
            raise ValueError("traversal_follower_landing_out_of_bounds")
        self.current_waypoint_index = end_index
        self.state = ExecutionState.FOLLOWING
        self._last_t = t
        self._last_position = position
        self._last_heading = heading
        self._reset_progress(t, position)
        return self._event(t, "traversal_follower_resumed", start_waypoint_index=start_index,
                           landing_waypoint_index=end_index)

    def _update_deviation(self, t: float, position: Point) -> list[dict[str, Any]]:
        geometry = _active_route_geometry(
            self.route.waypoints,
            self.current_waypoint_index,
            position,
            self.config.world_units_per_meter,
        )
        cross_track = geometry.active_cross_track_m or 0.0
        if cross_track > self.config.deviation_threshold_m:
            self._deviation_consecutive += 1
        else:
            self._deviation_consecutive = 0
        if self._deviation_consecutive < self.config.deviation_confirmations:
            return []
        if self.state in {
            ExecutionState.ARRIVED,
            ExecutionState.REPLAN_REQUIRED,
            ExecutionState.FAILED,
        }:
            return []
        events: list[dict[str, Any]] = []
        deviated = self._transition(
            ExecutionState.DEVIATED,
            t,
            "route_deviation_detected",
            deviation_confirmations=self._deviation_consecutive,
            deviation_threshold_m=self.config.deviation_threshold_m,
        )
        if deviated is not None:
            events.append(deviated)
        required = self._transition(
            ExecutionState.REPLAN_REQUIRED,
            t,
            "replan_required",
            replan_reason="route_deviation",
        )
        if required is not None:
            events.append(required)
        return events

    def _oscillation_delta(self, position: Point) -> float | None:
        index = self.current_waypoint_index
        if index >= len(self.route.waypoints):
            return None
        target = self.route.waypoints[index]
        if index > 0:
            origin = self.route.waypoints[index - 1]
        elif len(self.route.waypoints) > 1:
            origin = target
            target = self.route.waypoints[1]
        else:
            return None
        dx = target.x - origin.x
        dy = target.y - origin.y
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            return None
        if index == 0:
            waypoint = self.route.waypoints[0]
        else:
            waypoint = self.route.waypoints[index]
        return ((position.x - waypoint.x) * dx + (position.y - waypoint.y) * dy) / (
            length * self.config.world_units_per_meter
        )

    def _update_oscillation(self, t: float, position: Point) -> list[dict[str, Any]]:
        while self._oscillation_signs and (
            t - self._oscillation_signs[0][0] > self.config.oscillation_window_s
        ):
            self._oscillation_signs.popleft()
        self._oscillation_crossings = sum(
            previous_sign != current_sign
            for (_, previous_sign), (_, current_sign) in zip(
                self._oscillation_signs, list(self._oscillation_signs)[1:]
            )
        )
        delta = self._oscillation_delta(position)
        if delta is None or abs(delta) <= self.config.oscillation_band_m:
            return []
        sign = 1 if delta > 0.0 else -1
        self._oscillation_signs.append((t, sign))
        self._oscillation_crossings = sum(
            previous_sign != current_sign
            for (_, previous_sign), (_, current_sign) in zip(
                self._oscillation_signs, list(self._oscillation_signs)[1:]
            )
        )
        if (
            self._oscillation_crossings < self.config.oscillation_min_crossings
            or self._oscillation_triggered
            or self.state in {
                ExecutionState.ARRIVED,
                ExecutionState.REPLAN_REQUIRED,
                ExecutionState.FAILED,
            }
        ):
            return []
        self._oscillation_triggered = True
        event = self._transition(
            ExecutionState.STALLED,
            t,
            "oscillation_detected",
            stall_reason="waypoint_oscillation",
            oscillation_crossings=self._oscillation_crossings,
        )
        return [event] if event is not None else []

    def _update_progress(self, t: float, position: Point) -> list[dict[str, Any]]:
        target = self.active_waypoint
        if target is None:
            return []
        distance = _distance_m(position, target, self.config.world_units_per_meter)
        geometry = _active_route_geometry(
            self.route.waypoints,
            self.current_waypoint_index,
            position,
            self.config.world_units_per_meter,
        )
        on_active_leg = (
            geometry.active_cross_track_m is None
            or geometry.active_cross_track_m <= self.config.deviation_threshold_m
        )
        route_fraction = geometry.route_progress_fraction if on_active_leg else None
        if self._last_meaningful_progress_t is None:
            self._reset_progress(t, position)
            return []
        while self._recent_distances and (
            t - self._recent_distances[0][0] > self.config.progress_window_s
        ):
            self._recent_distances.popleft()
        self._recent_distances.append((t, distance))

        distance_improved = on_active_leg and self._progress_anchor_distance_m is not None and (
            self._progress_anchor_distance_m - distance >= self.config.progress_epsilon_m
        )
        route_improved = on_active_leg and route_fraction is not None and self._progress_anchor_route_fraction is not None and (
            route_fraction - self._progress_anchor_route_fraction
            >= self.config.route_progress_epsilon
        )
        events: list[dict[str, Any]] = []
        if distance_improved or route_improved:
            self._last_meaningful_progress_t = t
            self._progress_anchor_distance_m = distance
            self._progress_anchor_route_fraction = route_fraction
            if self.state == ExecutionState.STALLED:
                recovered = self._transition(
                    ExecutionState.FOLLOWING,
                    t,
                    "stall_recovered",
                    recovery_reason="meaningful_progress",
                )
                if recovered is not None:
                    events.append(recovered)
                self._reset_oscillation_episode()
            return events

        if (
            self._last_meaningful_progress_t is not None
            and t - self._last_meaningful_progress_t >= self.config.stall_timeout_s
            and self.state not in {
                ExecutionState.ARRIVED,
                ExecutionState.REPLAN_REQUIRED,
                ExecutionState.FAILED,
                ExecutionState.STALLED,
            }
        ):
            stalled = self._transition(
                ExecutionState.STALLED,
                t,
                "stall_detected",
                stall_reason="no_meaningful_progress",
                stall_timeout_s=self.config.stall_timeout_s,
            )
            if stalled is not None:
                events.append(stalled)
        return events

    def on_telemetry(self, sample: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Consume one synthetic telemetry update and return its event records."""

        safe_t: float
        try:
            if not isinstance(sample, Mapping):
                raise _InvalidTelemetry("telemetry_not_object")
            raw_t = sample.get("t")
            if not _finite(raw_t):
                raise _InvalidTelemetry("timestamp_non_finite")
            safe_t = float(raw_t)
            if self._last_t is not None and safe_t < self._last_t:
                raise _InvalidTelemetry("timestamp_regressed")
            position = _parse_point(sample.get("position"), allow_sequence=True)
            raw_heading = sample.get("heading")
            if not _finite(raw_heading):
                raise _InvalidTelemetry("heading_non_finite")
            heading = _normalize_heading(float(raw_heading))
        except _InvalidTelemetry as exc:
            return self._invalid_telemetry_event(
                safe_t if "safe_t" in locals() and _finite(safe_t) else (self._last_t or 0.0),
                exc.reason_code,
            )
        except ValueError as exc:
            return self._invalid_telemetry_event(
                safe_t if "safe_t" in locals() and _finite(safe_t) else (self._last_t or 0.0),
                str(exc),
            )

        previous_position = self._last_position
        self._last_t = safe_t
        self._last_position = position
        self._last_heading = heading
        if self.state == ExecutionState.ROUTE_READY:
            events: list[dict[str, Any]] = []
            started = self._transition(ExecutionState.FOLLOWING, safe_t, "following_started")
            if started is not None:
                events.append(started)
            self._reset_progress(safe_t, position)
        elif self.state in {
            ExecutionState.ARRIVED,
            ExecutionState.REPLAN_REQUIRED,
            ExecutionState.FAILED,
        }:
            return [self._event(safe_t, "telemetry_ignored_after_terminal")]
        else:
            events = []

        events.extend(self._advance_waypoints(safe_t, position, previous_position))
        if self.state not in {ExecutionState.ARRIVED, ExecutionState.REPLAN_REQUIRED, ExecutionState.FAILED}:
            events.extend(self._update_oscillation(safe_t, position))
            events.extend(self._update_deviation(safe_t, position))
            if self.state not in {ExecutionState.REPLAN_REQUIRED, ExecutionState.FAILED}:
                events.extend(self._update_progress(safe_t, position))
        events.append(self._event(safe_t, "telemetry_update"))
        return events

    def _invalid_telemetry_event(self, t: float, reason_code: str) -> list[dict[str, Any]]:
        self._last_t = t if self._last_t is None or t >= self._last_t else self._last_t
        if self.state != ExecutionState.FAILED:
            transition = self._transition(
                ExecutionState.FAILED,
                t,
                "telemetry_invalid",
                reason_code="invalid_telemetry",
                telemetry_error=reason_code,
            )
            return [transition] if transition is not None else []
        return [self._event(t, "telemetry_invalid", reason_code="invalid_telemetry", telemetry_error=reason_code)]

    def process_external_event(self, event_name: str, t: float) -> dict[str, Any]:
        """Invalidate the current plan without attempting replanning."""

        if event_name not in _ALLOWED_INVALIDATIONS:
            raise ReplayInputError(f"unknown_plan_invalidation:{event_name}")
        if not _finite(t):
            raise ReplayInputError("invalidation_timestamp_non_finite")
        if self.state == ExecutionState.FAILED:
            return self._event(float(t), "plan_invalidation_ignored_after_failure")
        if self.state != ExecutionState.REPLAN_REQUIRED:
            transition = self._transition(
                ExecutionState.REPLAN_REQUIRED,
                float(t),
                "plan_invalidated",
                invalidation_event=event_name,
                replan_reason=event_name,
            )
            if transition is not None:
                return transition
        return self._event(
            float(t),
            "plan_invalidated",
            invalidation_event=event_name,
            replan_reason=event_name,
        )


def _event_time(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _failure_event(t: float, reason_code: str, **fields: Any) -> dict[str, Any]:
    return {
        "t": _rounded(t),
        "event": "replay_failed",
        "state": ExecutionState.FAILED.value,
        "waypoint_index": None,
        "distance_m": None,
        "distance_to_final_destination_m": None,
        "best_distance_recent_m": None,
        "elapsed_since_meaningful_progress_s": None,
        "route_progress_fraction": None,
        "cross_track_error_m": None,
        "desired_heading_deg": None,
        "heading_error_deg": None,
        "turn_direction": "none",
        "suggested_speed_band": "stop",
        "reason_code": reason_code,
        **fields,
    }


def replay_payload(
    payload: Mapping[str, Any], config: SimulatorConfig | None = None
) -> list[dict[str, Any]]:
    """Replay a JSON-shaped envelope and return deterministic event records."""

    if not isinstance(payload, Mapping):
        return [_failure_event(0.0, "invalid_replay_input", input_error="replay_not_object")]
    telemetry = payload.get("telemetry")
    if not isinstance(telemetry, list):
        return [_failure_event(0.0, "invalid_replay_input", input_error="telemetry_must_be_list")]

    raw_events = payload.get("events", [])
    if not isinstance(raw_events, list):
        return [_failure_event(0.0, "invalid_replay_input", input_error="events_must_be_list")]

    times = [time for time in (_event_time(sample.get("t") if isinstance(sample, Mapping) else None) for sample in telemetry) if time is not None]
    times.extend(time for time in (_event_time(event.get("t") if isinstance(event, Mapping) else None) for event in raw_events) if time is not None)
    first_t = min(times) if times else 0.0

    try:
        follower = RouteFollower(payload.get("route_plan"), config=config)
    except InvalidRoutePlan as exc:
        return [_failure_event(first_t, "invalid_route_plan", issues=list(exc.issues))]
    except (TypeError, ValueError) as exc:
        return [_failure_event(first_t, "invalid_route_plan", issues=[str(exc)])]

    invalidations: list[tuple[float, int, str]] = []
    for order, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, Mapping):
            return [_failure_event(first_t, "invalid_replay_input", input_error="event_not_object")]
        event_time = _event_time(raw_event.get("t"))
        name = raw_event.get("event", raw_event.get("type", raw_event.get("name")))
        if event_time is None:
            return [_failure_event(first_t, "invalid_replay_input", input_error="event_timestamp_non_finite")]
        if not isinstance(name, str):
            return [_failure_event(first_t, "invalid_replay_input", input_error="event_name_missing")]
        invalidations.append((event_time, order, name))
    invalidations.sort(key=lambda item: (item[0], item[1]))

    timeline: list[dict[str, Any]] = [follower.route_ready_event(first_t)]
    event_index = 0
    for sample in telemetry:
        sample_t = _event_time(sample.get("t") if isinstance(sample, Mapping) else None)
        if sample_t is not None:
            while event_index < len(invalidations) and invalidations[event_index][0] <= sample_t:
                event_time, _, name = invalidations[event_index]
                try:
                    timeline.append(follower.process_external_event(name, event_time))
                except ReplayInputError as exc:
                    timeline.append(_failure_event(event_time, "invalid_replay_input", input_error=str(exc)))
                    return timeline
                event_index += 1
        timeline.extend(follower.on_telemetry(sample))

    while event_index < len(invalidations):
        event_time, _, name = invalidations[event_index]
        try:
            timeline.append(follower.process_external_event(name, event_time))
        except ReplayInputError as exc:
            timeline.append(_failure_event(event_time, "invalid_replay_input", input_error=str(exc)))
            break
        event_index += 1
    return timeline


def replay_file(path: str | Path, config: SimulatorConfig | None = None) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return replay_payload(payload, config=config)
