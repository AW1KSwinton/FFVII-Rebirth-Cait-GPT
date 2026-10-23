"""Small, route-agnostic evidence fusion primitives for live navigation.

This module is intentionally pure.  It consumes the current validated game
snapshot, an optional route-leg bearing, and local clearance evidence.  It
does not read recordings, screenshots, or the known-world store, and it never
sends input.  The service layer is responsible for collecting evidence and
executing one bounded, safety-gated action from the resulting decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Iterable

from .models import GameState, Vec3
from .static_obstacles import navigation_sequence_is_bound


DEFAULT_MAX_ROUTE_DEVIATION_DEGREES = 30.0


@dataclass(frozen=True)
class DirectionEvidence:
    """One locally measurable movement corridor and its fused score."""

    direction: str
    bearing_degrees: float
    clear_distance_m: float | None
    confidence: str
    score: float
    eligible: bool
    blocked: bool
    sources: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class FusionDecision:
    """The executive decision made from live evidence, before movement."""

    outcome: str
    desired_bearing_degrees: float | None
    route_bearing_degrees: float | None
    goal_bearing_degrees: float | None
    selected_direction: str | None
    selected_bearing_degrees: float | None
    route_available: bool
    route_source: str
    target_vector_policy: str
    candidates: tuple[DirectionEvidence, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalise_degrees(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0


def horizontal_distance_m(first: Vec3, second: Vec3) -> float:
    return math.hypot(second.x - first.x, second.y - first.y) / 100.0


def world_bearing_degrees(first: Vec3, second: Vec3) -> float:
    return math.degrees(math.atan2(second.y - first.y, second.x - first.x))


def cross_track_distance_m(position: Vec3, segment_start: Vec3, segment_end: Vec3) -> float:
    """Return horizontal distance from a point to a route segment."""

    dx = segment_end.x - segment_start.x
    dy = segment_end.y - segment_start.y
    length_sq = dx * dx + dy * dy
    if length_sq <= 0.0:
        return horizontal_distance_m(position, segment_start)
    t = max(
        0.0,
        min(
            1.0,
            ((position.x - segment_start.x) * dx + (position.y - segment_start.y) * dy)
            / length_sq,
        ),
    )
    projected = Vec3(segment_start.x + dx * t, segment_start.y + dy * t, position.z)
    return horizontal_distance_m(position, projected)


def _camera_bearing(state: GameState) -> float:
    if state.camera is not None and state.camera.rotation is not None:
        return float(state.camera.rotation.yaw)
    if state.player is not None and state.player.rotation is not None:
        return float(state.player.rotation.yaw)
    return 0.0


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _confidence_weight(value: Any) -> float:
    if isinstance(value, (int, float)):
        numeric = _finite_float(value)
        return max(0.0, min(1.0, numeric)) if numeric is not None else 0.25
    return {
        "high": 1.0,
        "medium": 0.65,
        "low": 0.35,
        "unknown": 0.2,
    }.get(str(value or "unknown").lower(), 0.2)


def fresh_capsule_probe(
    navigation_payload: dict[str, Any],
    direction_evidence: dict[str, Any],
    *,
    observed_sequence: int | None = None,
) -> dict[str, Any] | None:
    """Return only a same-sample, available character-capsule result.

    The probe is intentionally low frequency. A missing, scheduled, stale, or
    sequence-mismatched result must not override the conservative line-trace
    obstacle decision.
    """

    capsule = direction_evidence.get("capsule_probe")
    if not isinstance(capsule, dict):
        return None
    if capsule.get("available") is not True or capsule.get("fresh") is not True:
        return None
    if not navigation_sequence_is_bound(
        navigation_payload,
        observed_sequence=observed_sequence,
        sample_sequence=capsule.get("sample_sequence"),
    ):
        return None
    return capsule


def _blocker_bearing(blocker: Any) -> float | None:
    if isinstance(blocker, dict):
        return _finite_float(blocker.get("bearing_degrees"))
    return _finite_float(getattr(blocker, "bearing_degrees", None))


def _matches_blocker(bearing: float, blockers: Iterable[Any]) -> bool:
    for blocker in blockers:
        candidate = _blocker_bearing(blocker)
        if candidate is not None and abs(normalise_degrees(bearing - candidate)) <= 25.0:
            return True
    return False


def fuse_direction_evidence(
    state: GameState,
    *,
    route_bearing_degrees: float | None = None,
    goal_bearing_degrees: float | None = None,
    route_source: str = "unavailable",
    blockers: Iterable[Any] = (),
    minimum_clearance_m: float = 1.0,
    scoring_distance_m: float = 12.0,
    max_route_deviation_degrees: float = DEFAULT_MAX_ROUTE_DEVIATION_DEGREES,
    visual_review_bearing_degrees: float | None = None,
    movement_horizon_m: float | None = None,
) -> FusionDecision:
    """Fuse route intent, goal geometry, and local clearance evidence.

    A final-target vector is used only as a low-confidence tie-breaker when no
    complete route is available.  It is never labelled as a route.  Movement
    candidates must still come from sequence-bound local navigation evidence.
    """

    try:
        max_route_deviation_degrees = float(max_route_deviation_degrees)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_route_deviation_degrees must be numeric") from exc
    if not math.isfinite(max_route_deviation_degrees) or not 0.0 <= max_route_deviation_degrees <= 180.0:
        raise ValueError("max_route_deviation_degrees must be between 0 and 180")
    if movement_horizon_m is not None:
        try:
            movement_horizon_m = float(movement_horizon_m)
        except (TypeError, ValueError) as exc:
            raise ValueError("movement_horizon_m must be numeric") from exc
        if not math.isfinite(movement_horizon_m) or movement_horizon_m <= 0.0:
            raise ValueError("movement_horizon_m must be positive and finite")

    payload = state.navigation if isinstance(state.navigation, dict) else {}
    directions = payload.get("directions")
    route_available = route_bearing_degrees is not None and route_source not in {
        "", "unavailable", "route_required"
    }
    desired = route_bearing_degrees if route_available else goal_bearing_degrees
    target_policy = (
        "complete_route_leg"
        if route_available
        else "low_confidence_goal_tiebreaker_with_local_clearance"
        if goal_bearing_degrees is not None
        else "no_route_or_goal_vector"
    )
    if not isinstance(directions, dict):
        return FusionDecision(
            "perception_required",
            desired,
            route_bearing_degrees,
            goal_bearing_degrees,
            None,
            None,
            route_available,
            route_source,
            target_policy,
            (),
            "sequence-bound navigation telemetry has no directional clearance evidence",
        )
    if desired is None:
        return FusionDecision(
            "route_required",
            None,
            route_bearing_degrees,
            goal_bearing_degrees,
            None,
            None,
            route_available,
            route_source,
            target_policy,
            (),
            "a route leg or target position is required before movement can be selected",
        )

    camera_yaw = _camera_bearing(state)
    defaults = {
        "forward": camera_yaw,
        "backward": camera_yaw + 180.0,
        "left": camera_yaw - 90.0,
        "right": camera_yaw + 90.0,
    }
    candidates: list[DirectionEvidence] = []
    telemetry_source = str(payload.get("source") or "navigation_telemetry")
    for direction in ("forward", "left", "right", "backward"):
        raw = directions.get(direction)
        if not isinstance(raw, dict):
            continue
        bearing = _finite_float(raw.get("bearing_degrees"))
        if bearing is None:
            bearing = defaults[direction]
        clear = _finite_float(raw.get("clear_distance_m", raw.get("distance_m")))
        confidence_raw = raw.get("confidence", payload.get("confidence", "unknown"))
        confidence = str(confidence_raw or "unknown")
        hit = bool(raw.get("hit", False))
        capsule = fresh_capsule_probe(payload, raw)
        capsule_blocked = (
            bool(capsule.get("blocking_hit", capsule.get("hit", False)))
            if capsule is not None
            else None
        )
        line_hit_within_horizon = bool(
            hit
            and (
                movement_horizon_m is None
                or clear is None
                or clear <= movement_horizon_m
            )
        )
        capsule_clearance = (
            _finite_float(capsule.get("clear_distance_m"))
            if capsule is not None else None
        )
        capsule_blocked_within_horizon = bool(
            capsule_blocked is True
            and (
                movement_horizon_m is None
                or capsule_clearance is None
                or capsule_clearance <= movement_horizon_m
            )
        )
        obstacle_hit = line_hit_within_horizon
        if capsule_blocked_within_horizon:
            obstacle_hit = True
        elif capsule is not None and (
            not line_hit_within_horizon
            or bool(raw.get("walkable_surface_hit", False))
            or raw.get("surface_class") == "walkable_surface_candidate"
            or (
                _finite_float(raw.get("clear_distance_m")) is not None
                and capsule_clearance is not None
                and _finite_float(raw.get("clear_distance_m")) >= _finite_float(capsule.get("clear_distance_m"))
            )
        ):
            # A capsule hit on supporting ground is not permission to erase a
            # ray-confirmed wall. It can only supplement a clear ray or
            # reinterpret a ray that itself hit a walkable surface.
            obstacle_hit = False
        visual_capsule_override = False
        if (
            visual_review_bearing_degrees is not None
            and capsule is not None
            and capsule_blocked is False
            and capsule.get("walkable_surface_hit") is True
            and abs(normalise_degrees(bearing - visual_review_bearing_degrees)) <= 15.0
        ):
            try:
                line_clearance = float(raw.get("clear_distance_m"))
                capsule_clearance = float(capsule.get("clear_distance_m"))
            except (TypeError, ValueError):
                line_clearance = None
                capsule_clearance = None
            if (
                line_clearance is not None
                and capsule_clearance is not None
                and math.isfinite(line_clearance)
                and math.isfinite(capsule_clearance)
                and line_clearance >= capsule_clearance
            ):
                # A structured screenshot review can identify a short path
                # around a companion or floor/foliage ray hit. Keep the
                # override narrow: it must match the reviewed bearing, have
                # fresh non-blocking walkable capsule evidence, and stay
                # inside the capsule's measured horizon.
                obstacle_hit = False
                visual_capsule_override = True
        blocker = _matches_blocker(bearing, blockers)
        alignment_error = abs(normalise_degrees(bearing - desired))
        alignment = math.cos(math.radians(alignment_error))
        clearance = (
            max(0.0, min(clear, scoring_distance_m)) / scoring_distance_m
            if clear is not None
            else 0.0
        )
        score = alignment * 5.0 + clearance * 3.0 + _confidence_weight(confidence)
        if obstacle_hit:
            score -= 2.0
        if blocker:
            score -= 4.0
        route_deviation_ok = (
            not route_available
            or alignment_error <= max_route_deviation_degrees
        )
        eligible = (
            clear is not None
            and clear >= minimum_clearance_m
            and not blocker
            and not obstacle_hit
            and route_deviation_ok
        )
        if clear is None:
            reason = "no clear-distance measurement"
        elif clear < minimum_clearance_m:
            reason = "clearance is below the movement safety minimum"
        elif blocker:
            reason = "recent no-progress evidence blocks this heading"
        elif obstacle_hit and capsule_blocked is True:
            reason = "fresh character-capsule sweep reports a blocking obstacle"
        elif obstacle_hit:
            reason = "line-trace hit blocks this heading until fresh capsule walkability evidence confirms the corridor"
        elif not route_deviation_ok:
            reason = "heading exceeds the route-leg deviation budget"
        else:
            reason = "locally clear candidate"
        sources = ["local_clearance", telemetry_source]
        if capsule is not None:
            sources.append("capsule_sweep")
        if visual_capsule_override:
            sources.append("visual_review_capsule_override")
        if route_available:
            sources.append("route_leg_bearing")
        elif goal_bearing_degrees is not None:
            sources.append("goal_vector_tiebreaker")
        candidates.append(
            DirectionEvidence(
                direction,
                normalise_degrees(bearing),
                clear,
                confidence,
                round(score, 4),
                eligible,
                blocker or obstacle_hit or not route_deviation_ok,
                tuple(dict.fromkeys(sources)),
                reason,
            )
        )

    ranked = sorted(candidates, key=lambda item: (item.eligible, item.score), reverse=True)
    selected = next((item for item in ranked if item.eligible), None)
    if selected is None:
        route_deviation_rejected = bool(
            route_available
            and any(
                item.clear_distance_m is not None
                and item.clear_distance_m >= minimum_clearance_m
                and item.blocked
                and "route-leg deviation budget" in item.reason
                for item in candidates
            )
        )
        outcome = (
            "route_deviation_replan"
            if route_deviation_rejected
            else "blocked_or_uncertain"
            if candidates
            else "perception_required"
        )
        reason = (
            "usable local corridors exist, but none stays within the route-leg deviation budget"
            if route_deviation_rejected
            else "all measured corridors are blocked or below the clearance minimum"
            if candidates
            else "navigation telemetry contains no usable directional candidates"
        )
    else:
        outcome = "route_leg_selected" if route_available else "local_corridor_selected"
        reason = (
            "selected the highest-scoring locally clear corridor aligned with the route leg"
            if route_available
            else "selected a locally clear corridor using the goal vector only as a low-confidence tie-breaker"
        )
    return FusionDecision(
        outcome,
        normalise_degrees(desired),
        normalise_degrees(route_bearing_degrees) if route_bearing_degrees is not None else None,
        normalise_degrees(goal_bearing_degrees) if goal_bearing_degrees is not None else None,
        selected.direction if selected is not None else None,
        selected.bearing_degrees if selected is not None else None,
        route_available,
        route_source,
        target_policy,
        tuple(ranked),
        reason,
    )
