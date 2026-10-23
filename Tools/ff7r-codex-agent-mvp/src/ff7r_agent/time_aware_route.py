"""Offline traversal-time-aware shadow routing over validated Detour topology.

This module does not actuate movement. The opt-in live travel_time objective
uses it through the normal planner and validator. It reuses admitted graph nodes and proven portals,
then changes how those allowed transitions are ranked. Topology admissibility,
portal validation, obstacle blocking and all fail-closed behavior remain owned by
the existing Detour graph.

The cost model is provisional. Its purpose is to support offline replay and
parameter-sensitivity experiments until controlled live measurements can
calibrate geometry -> traversal-time coefficients.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import heapq
import math
from typing import Any, Iterable

from .detour_route import DetourPortal, DetourTopologyGraph
from .models import Vec3


def estimate_polyline_seconds(points: Iterable[Vec3], *, speed_m_s: float = 8.4,
                              acceleration_m_s2: float = 4.0,
                              lateral_acceleration_m_s2: float = 3.0) -> float:
    """Provisional rest-to-rest time with braking and acceleration at bends.

    Evaluate the actual funnel polyline, not polygon-centroid zigzags. The
    turn radius is a speed proxy only: this never changes the validated path.
    Coefficients require matched live calibration before claims of optimality.
    """
    try:
        speed_m_s, acceleration_m_s2, lateral_acceleration_m_s2 = (
            float(value) for value in
            (speed_m_s, acceleration_m_s2, lateral_acceleration_m_s2)
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("speed and acceleration must be finite and positive") from exc
    if not all(math.isfinite(v) and v > 0 for v in
               (speed_m_s, acceleration_m_s2, lateral_acceleration_m_s2)):
        raise ValueError("speed and acceleration must be finite and positive")
    route = []
    for point in points:
        try:
            x, y, z = (float(point.x), float(point.y), float(point.z))
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError("route coordinates must be finite") from exc
        if not all(math.isfinite(v) for v in (x, y, z)):
            raise ValueError("route coordinates must be finite")
        point = Vec3(x, y, z)
        if not route or _distance_m(route[-1], point) > 1e-8:
            route.append(point)
    if len(route) < 2:
        return 0.0
    lengths = [_distance_m(a, b) for a, b in zip(route, route[1:])]
    if not all(math.isfinite(length) and length > 0.0 for length in lengths):
        raise ValueError("route segment lengths must be finite and positive")
    speeds = [0.0] + [speed_m_s] * (len(route) - 2) + [0.0]
    for i in range(1, len(route) - 1):
        a, b, c = route[i - 1:i + 2]
        ux, uy = b.x - a.x, b.y - a.y
        vx, vy = c.x - b.x, c.y - b.y
        norm = math.hypot(ux, uy) * math.hypot(vx, vy)
        if norm <= 1e-9:
            speeds[i] = 0.0
            continue
        angle = math.acos(max(-1.0, min(1.0, (ux * vx + uy * vy) / norm)))
        if angle > 1e-6:
            radius = min(lengths[i - 1], lengths[i]) * 0.25 / max(1e-9, math.tan(angle / 2))
            speeds[i] = min(speed_m_s, math.sqrt(max(0.0, lateral_acceleration_m_s2 * radius)))
    a = acceleration_m_s2
    for i, length in enumerate(lengths):
        speeds[i + 1] = min(speeds[i + 1], math.sqrt(speeds[i] ** 2 + 2 * a * length))
    for i in range(len(lengths) - 1, -1, -1):
        speeds[i] = min(speeds[i], math.sqrt(speeds[i + 1] ** 2 + 2 * a * lengths[i]))
    seconds = 0.0
    for length, first, last in zip(lengths, speeds, speeds[1:]):
        peak = min(speed_m_s, math.sqrt(a * length + (first ** 2 + last ** 2) / 2))
        acceleration_distance = (2 * peak ** 2 - first ** 2 - last ** 2) / (2 * a)
        seconds += (2 * peak - first - last) / a + max(0.0, length - acceleration_distance) / peak
    return seconds


@dataclass(frozen=True)
class TraversalTimeModel:
    """Provisional geometry-to-time model used only by the shadow planner."""

    straight_speed_m_s: float = 8.4
    turn_penalty_s_per_rad: float = 0.45
    narrow_portal_threshold_m: float = 1.5
    narrow_portal_penalty_s_per_m: float = 0.35

    def __post_init__(self) -> None:
        field_names = (
            "straight_speed_m_s",
            "turn_penalty_s_per_rad",
            "narrow_portal_threshold_m",
            "narrow_portal_penalty_s_per_m",
        )
        try:
            values = tuple(float(getattr(self, field_name)) for field_name in field_names)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("traversal-time model values must be finite") from exc
        if not all(math.isfinite(value) for value in values):
            raise ValueError("traversal-time model values must be finite")
        for field_name, value in zip(field_names, values):
            object.__setattr__(self, field_name, value)
        if values[0] <= 0.0:
            raise ValueError("straight_speed_m_s must be positive")
        if values[1] < 0.0:
            raise ValueError("turn_penalty_s_per_rad must be non-negative")
        if values[2] < 0.0:
            raise ValueError("narrow_portal_threshold_m must be non-negative")
        if values[3] < 0.0:
            raise ValueError("narrow_portal_penalty_s_per_m must be non-negative")


@dataclass(frozen=True)
class ShadowRouteResult:
    status: str
    reason_code: str
    polygon_corridor: tuple[int, ...]
    transitions: tuple[DetourPortal, ...]
    estimated_seconds: float | None
    graph_distance_m: float | None
    distance_seconds: float | None
    turn_penalty_seconds: float | None
    narrow_portal_penalty_seconds: float | None
    total_turn_radians: float | None
    max_turn_degrees: float | None
    minimum_portal_width_m: float | None
    expansions: int
    model: TraversalTimeModel

    @property
    def ok(self) -> bool:
        return self.status == "complete"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "polygon_corridor": list(self.polygon_corridor),
            "transition_count": len(self.transitions),
            "estimated_seconds": self.estimated_seconds,
            "graph_distance_m": self.graph_distance_m,
            "distance_seconds": self.distance_seconds,
            "turn_penalty_seconds": self.turn_penalty_seconds,
            "narrow_portal_penalty_seconds": self.narrow_portal_penalty_seconds,
            "total_turn_radians": self.total_turn_radians,
            "max_turn_degrees": self.max_turn_degrees,
            "minimum_portal_width_m": self.minimum_portal_width_m,
            "expansions": self.expansions,
            "model": asdict(self.model),
            "movement_sent": False,
            "authoritative": False,
        }


def _distance_m(first: Vec3, second: Vec3) -> float:
    return math.sqrt(
        (second.x - first.x) ** 2
        + (second.y - first.y) ** 2
        + (second.z - first.z) ** 2
    ) / 100.0


def _portal_width_m(portal: DetourPortal) -> float:
    return math.hypot(portal.right.x - portal.left.x, portal.right.y - portal.left.y) / 100.0


def _turn_radians(
    graph: DetourTopologyGraph,
    incoming: DetourPortal | None,
    outgoing: DetourPortal,
) -> float:
    """Return heading change through the current polygon's centroid.

    Centroid-to-centroid headings are immutable and avoid accidentally treating
    portal-edge orientation as vehicle/player heading. The incoming portal still
    identifies the previous polygon, so the expanded state remains sufficient.
    """
    if incoming is None:
        return 0.0
    previous = graph.polygons.get(incoming.from_reference)
    current = graph.polygons.get(incoming.to_reference)
    target = graph.polygons.get(outgoing.to_reference)
    if (
        previous is None
        or current is None
        or target is None
        or current.reference.value != outgoing.from_reference
    ):
        return 0.0
    first_x = current.centroid.x - previous.centroid.x
    first_y = current.centroid.y - previous.centroid.y
    second_x = target.centroid.x - current.centroid.x
    second_y = target.centroid.y - current.centroid.y
    first_length = math.hypot(first_x, first_y)
    second_length = math.hypot(second_x, second_y)
    if first_length <= 1e-9 or second_length <= 1e-9:
        return 0.0
    cosine = (first_x * second_x + first_y * second_y) / (first_length * second_length)
    return math.acos(max(-1.0, min(1.0, cosine)))


def _transition_components(
    graph: DetourTopologyGraph,
    incoming: DetourPortal | None,
    outgoing: DetourPortal,
    model: TraversalTimeModel,
    traversal_penalty_m: float = 0.0,
) -> tuple[float, float, float, float, float]:
    source = graph.polygons[outgoing.from_reference]
    target = graph.polygons[outgoing.to_reference]
    if outgoing.is_traversal:
        # An off-mesh crossing is walked point to point, and the penalty is
        # the time lost at the link, as a distance at the straight speed.
        distance_m = (
            _distance_m(source.centroid, outgoing.left)
            + _distance_m(outgoing.left, outgoing.right)
            + _distance_m(outgoing.right, target.centroid)
        )
        distance_seconds = (distance_m + traversal_penalty_m) / model.straight_speed_m_s
        return distance_m, distance_seconds, _turn_radians(graph, incoming, outgoing), 0.0, 0.0
    distance_m = _distance_m(source.centroid, target.centroid)
    distance_seconds = distance_m / model.straight_speed_m_s
    turn_radians = _turn_radians(graph, incoming, outgoing)
    turn_seconds = turn_radians * model.turn_penalty_s_per_rad
    width_m = _portal_width_m(outgoing)
    narrow_seconds = (
        max(0.0, model.narrow_portal_threshold_m - width_m)
        * model.narrow_portal_penalty_s_per_m
    )
    return distance_m, distance_seconds, turn_radians, turn_seconds, narrow_seconds


def _complete_result(
    graph: DetourTopologyGraph,
    corridor: tuple[int, ...],
    transitions: tuple[DetourPortal, ...],
    *,
    expansions: int,
    model: TraversalTimeModel,
    traversal_penalty_m: float = 0.0,
) -> ShadowRouteResult:
    incoming: DetourPortal | None = None
    graph_distance_m = 0.0
    distance_seconds = 0.0
    turn_penalty_seconds = 0.0
    narrow_penalty_seconds = 0.0
    turn_values: list[float] = []
    widths: list[float] = []
    for outgoing in transitions:
        distance_m, edge_seconds, turn_radians, turn_seconds, narrow_seconds = _transition_components(
            graph, incoming, outgoing, model, traversal_penalty_m
        )
        graph_distance_m += distance_m
        distance_seconds += edge_seconds
        turn_penalty_seconds += turn_seconds
        narrow_penalty_seconds += narrow_seconds
        turn_values.append(turn_radians)
        if not outgoing.is_traversal:
            widths.append(_portal_width_m(outgoing))
        incoming = outgoing
    estimated_seconds = distance_seconds + turn_penalty_seconds + narrow_penalty_seconds
    return ShadowRouteResult(
        status="complete",
        reason_code="shadow_time_route_complete",
        polygon_corridor=corridor,
        transitions=transitions,
        estimated_seconds=estimated_seconds,
        graph_distance_m=graph_distance_m,
        distance_seconds=distance_seconds,
        turn_penalty_seconds=turn_penalty_seconds,
        narrow_portal_penalty_seconds=narrow_penalty_seconds,
        total_turn_radians=sum(turn_values),
        max_turn_degrees=math.degrees(max(turn_values, default=0.0)),
        minimum_portal_width_m=min(widths) if widths else None,
        expansions=expansions,
        model=model,
    )


def search_time_aware_corridor(
    graph: DetourTopologyGraph,
    start_reference: int,
    end_reference: int,
    *,
    model: TraversalTimeModel = TraversalTimeModel(),
    max_expansions: int = 100_000,
    max_tile_crossings: int | None = None,
    blocked_transitions: Iterable[tuple[int, int]] = (),
    traversal_policy: Any = None,
) -> ShadowRouteResult:
    """Find a minimum proxy-traversal-time corridor over proven graph edges.

    The state retains the incoming portal because turn cost depends on the
    direction used to enter the current polygon. No new topology edges are
    introduced: every expansion comes directly from the graph, ``graph.adjacency``
    and the off-mesh crossings the traversal policy allows.
    """
    policy = graph.traversal_policy if traversal_policy is None else traversal_policy
    traversal_penalty_m = policy.penalty_m
    if max_expansions <= 0:
        return ShadowRouteResult(
            "failed", "invalid_search_bound", (), (), None, None, None, None,
            None, None, None, None, 0, model,
        )
    if start_reference not in graph.polygons or end_reference not in graph.polygons:
        return ShadowRouteResult(
            "failed", "unknown_start_or_end_reference", (), (), None, None,
            None, None, None, None, None, None, 0, model,
        )
    if max_tile_crossings is not None and max_tile_crossings < 0:
        return ShadowRouteResult(
            "failed", "invalid_tile_crossing_bound", (), (), None, None, None,
            None, None, None, None, None, 0, model,
        )

    blocked = {(int(first), int(second)) for first, second in blocked_transitions}
    start_state = (start_reference, None, 0)
    costs: dict[tuple[int, DetourPortal | None, int], float] = {start_state: 0.0}
    came_from: dict[
        tuple[int, DetourPortal | None, int],
        tuple[tuple[int, DetourPortal | None, int], DetourPortal] | None,
    ] = {start_state: None}
    counter = 0
    frontier: list[tuple[float, float, int, tuple[int, DetourPortal | None, int]]] = [
        (0.0, 0.0, counter, start_state)
    ]
    expansions = 0
    final_state: tuple[int, DetourPortal | None, int] | None = None
    end_polygon = graph.polygons[end_reference]

    while frontier and expansions < max_expansions:
        _, current_cost, _, state = heapq.heappop(frontier)
        if current_cost > costs.get(state, float("inf")) + 1e-12:
            continue
        current_reference, incoming, crossings = state
        expansions += 1
        if current_reference == end_reference:
            final_state = state
            break
        source = graph.polygons[current_reference]
        for outgoing in graph.outgoing_portals(current_reference, policy):
            if (outgoing.from_reference, outgoing.to_reference) in blocked:
                continue
            target = graph.polygons.get(outgoing.to_reference)
            if target is None:
                continue
            if max_tile_crossings is None:
                next_crossings = 0
            else:
                next_crossings = crossings + int(
                    source.reference.tile_index != target.reference.tile_index
                )
                if next_crossings > max_tile_crossings:
                    continue
            _, distance_seconds, _, turn_seconds, narrow_seconds = _transition_components(
                graph, incoming, outgoing, model, traversal_penalty_m
            )
            new_cost = current_cost + distance_seconds + turn_seconds + narrow_seconds
            next_state = (target.reference.value, outgoing, next_crossings)
            if new_cost >= costs.get(next_state, float("inf")) - 1e-12:
                continue
            costs[next_state] = new_cost
            came_from[next_state] = (state, outgoing)
            heuristic = _distance_m(target.centroid, end_polygon.centroid) / model.straight_speed_m_s
            counter += 1
            heapq.heappush(frontier, (new_cost + heuristic, new_cost, counter, next_state))

    if final_state is None:
        return ShadowRouteResult(
            "failed", "shadow_time_route_not_found", (), (), None, None, None,
            None, None, None, None, None, expansions, model,
        )

    corridor: list[int] = []
    transitions: list[DetourPortal] = []
    state = final_state
    while True:
        corridor.append(state[0])
        step = came_from[state]
        if step is None:
            break
        previous_state, portal = step
        transitions.append(portal)
        state = previous_state
    corridor.reverse()
    transitions.reverse()
    return _complete_result(
        graph,
        tuple(corridor),
        tuple(transitions),
        expansions=expansions,
        model=model,
        traversal_penalty_m=traversal_penalty_m,
    )


def evaluate_corridor_time(
    graph: DetourTopologyGraph,
    corridor: tuple[int, ...],
    transitions: tuple[DetourPortal, ...],
    *,
    model: TraversalTimeModel = TraversalTimeModel(),
) -> ShadowRouteResult:
    """Evaluate an existing proven corridor under the same provisional model."""
    if not corridor or len(transitions) != max(0, len(corridor) - 1):
        return ShadowRouteResult(
            "failed", "invalid_corridor_shape", (), (), None, None, None, None,
            None, None, None, None, 0, model,
        )
    if any(reference not in graph.polygons for reference in corridor):
        return ShadowRouteResult(
            "failed", "corridor_polygon_not_in_validated_graph", (), (),
            None, None, None, None, None, None, None, None, 0, model,
        )
    for first, second, portal in zip(corridor, corridor[1:], transitions):
        if portal.from_reference != first or portal.to_reference != second:
            return ShadowRouteResult(
                "failed", "corridor_transition_mismatch", (), (), None, None,
                None, None, None, None, None, None, 0, model,
            )
        if portal not in graph.adjacency.get(first, ()) and portal not in graph.traversal_portals_from(first):
            return ShadowRouteResult(
                "failed", "corridor_transition_not_in_validated_graph", (), (),
                None, None, None, None, None, None, None, None, 0, model,
            )
    return _complete_result(
        graph, corridor, transitions, expansions=0, model=model,
    )
