"""Telemetry-first navigation primitives and durable known-world records.

This module deliberately contains no input calls.  It converts validated game
observations into navigation decisions and stores landmarks/routes that can be
used by a later bounded controller.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import heapq
import json
import math
import os
from pathlib import Path
from typing import Any

from .models import GameState, Interactable, ObservedActor, RelativePosition, Vec3


MIN_MEANINGFUL_DISPLACEMENT_M = 0.10


class NavigationDataError(ValueError):
    """Raised when a known-world record cannot be validated."""


@dataclass(frozen=True)
class NavigationTarget:
    target_id: str
    kind: str
    world_position: Vec3
    map_id: str | None = None
    interaction_position: Vec3 | None = None
    distance_m: float | None = None
    usable: bool | None = None
    visible: bool | None = None
    label: str | None = None
    relative: RelativePosition | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_interactable(cls, item: Interactable, map_id: str | None) -> "NavigationTarget":
        if item.position is None:
            raise NavigationDataError("interactable has no world position")
        return cls(
            target_id=item.id,
            kind=item.kind,
            world_position=item.position,
            map_id=map_id,
            interaction_position=item.position,
            distance_m=(item.relative.horizontal_distance if item.relative else item.distance),
            usable=item.usable,
            visible=item.visible,
            label=item.label,
            relative=item.relative,
            metadata={"prompt": item.prompt} if item.prompt is not None else {},
        )

    @classmethod
    def from_actor(cls, item: ObservedActor, map_id: str | None) -> "NavigationTarget":
        if item.position is None:
            raise NavigationDataError("actor has no world position")
        return cls(
            target_id=item.id,
            kind=item.kind or "actor",
            world_position=item.position,
            map_id=map_id,
            distance_m=(item.relative.horizontal_distance if item.relative else item.distance),
            usable=item.usable,
            visible=item.visible,
            label=item.name,
            relative=item.relative,
            metadata={
                key: value for key, value in {
                    "hp": item.hp,
                    "max_hp": item.max_hp,
                }.items() if value is not None
            },
        )

    @classmethod
    def from_landmark(cls, item: "Landmark", state: GameState) -> "NavigationTarget":
        """Rehydrate a durable landmark into a target-relative candidate."""
        position = item.interaction_position or item.position
        relative = None
        player = state.player
        if player is not None and player.position is not None and player.rotation is not None:
            dx = (position.x - player.position.x) / 100.0
            dy = (position.y - player.position.y) / 100.0
            dz = (position.z - player.position.z) / 100.0
            horizontal = math.hypot(dx, dy)
            bearing = math.degrees(math.atan2(dy, dx))
            heading_error = (bearing - player.rotation.yaw + 180.0) % 360.0 - 180.0
            relative = RelativePosition(dx, dy, dz, horizontal, bearing, heading_error)
        return cls(
            target_id=item.landmark_id,
            kind=item.kind,
            world_position=item.position,
            map_id=item.map_id,
            interaction_position=item.interaction_position,
            distance_m=relative.horizontal_distance if relative else None,
            label=item.label,
            relative=relative,
            metadata={
                "known_landmark": True,
                "landmark_id": item.landmark_id,
                **item.metadata,
            },
        )


@dataclass(frozen=True)
class NavigationDecision:
    outcome: str
    target_id: str
    distance_m: float | None
    heading_error_degrees: float | None
    camera_correction_degrees: float | None
    movement_x: float = 0.0
    movement_y: float = 0.0
    reason: str | None = None


@dataclass(frozen=True)
class RoutePlan:
    """A read-only route envelope produced before movement begins."""

    map_id: str | None
    target_id: str
    target_label: str | None
    start_position: Vec3 | None
    target_position: Vec3
    waypoint_ids: tuple[str, ...]
    legs: tuple[dict[str, Any], ...]
    straight_line_distance_m: float | None
    expected_distance_m: float | None
    path_preference: str
    map_scale_status: str
    confidence: str
    notes: tuple[str, ...] = field(default_factory=tuple)
    route_source: str = "unavailable"


@dataclass(frozen=True)
class StallDiagnosis:
    """Ranked, non-mutating recovery advice for a stalled movement step."""

    outcome: str
    primary_action: str
    reason: str
    ranked_actions: tuple[dict[str, Any], ...]
    requires_screenshot: bool
    requires_map: bool


def _world_distance_m(first: Vec3, second: Vec3) -> float:
    return math.sqrt(
        (second.x - first.x) ** 2
        + (second.y - first.y) ** 2
        + (second.z - first.z) ** 2
    ) / 100.0


def _world_bearing(first: Vec3, second: Vec3) -> float:
    return math.degrees(math.atan2(second.y - first.y, second.x - first.x))


def _payload_vec3(value: Any) -> Vec3 | None:
    if not isinstance(value, dict):
        return None
    try:
        result = Vec3.from_dict(value)
    except (KeyError, TypeError, ValueError):
        return None
    return result if all(math.isfinite(float(item)) for item in (result.x, result.y, result.z)) else None


def _runtime_path_points(state: GameState, target: NavigationTarget) -> tuple[list[Vec3], str] | None:
    """Read complete runtime path evidence without treating a target vector as a route."""
    payload = state.navigation if isinstance(state.navigation, dict) else None
    if not payload or str(payload.get("target_id") or "") != target.target_id:
        return None
    for key in ("navmesh_path", "walkable_path", "path_points"):
        raw = payload.get(key)
        if raw is None:
            continue
        if (
            payload.get("path_complete") is not True
            or payload.get("path_is_partial") is True
            or payload.get("partial") is True
        ):
            return None
        if isinstance(raw, dict):
            raw = raw.get("points", raw.get("waypoints"))
        if not isinstance(raw, list):
            return None
        points: list[Vec3] = []
        for item in raw:
            point_value = item.get("position") if isinstance(item, dict) and "position" in item else item
            point = _payload_vec3(point_value)
            if point is None:
                return None
            points.append(point)
        if len(points) < 2:
            return None
        return points, str(payload.get("path_source") or payload.get("source") or key)

    grid = payload.get("occupancy_grid")
    if not isinstance(grid, dict):
        return None
    try:
        cell_size = float(grid["cell_size_m"])
        origin = _payload_vec3(grid["origin"])
        width = int(grid["width"])
        height = int(grid["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if origin is None or not math.isfinite(cell_size) or cell_size <= 0 or width <= 0 or height <= 0:
        return None
    blocked: set[tuple[int, int]] = set()
    for item in grid.get("blocked", []):
        try:
            cell = (int(item[0]), int(item[1]))
        except (TypeError, ValueError, IndexError):
            continue
        if 0 <= cell[0] < width and 0 <= cell[1] < height:
            blocked.add(cell)

    def to_cell(position: Vec3) -> tuple[int, int]:
        return (int(round((position.x - origin.x) / (cell_size * 100.0))), int(round((position.y - origin.y) / (cell_size * 100.0))))

    def to_world(cell: tuple[int, int]) -> Vec3:
        return Vec3(origin.x + cell[0] * cell_size * 100.0, origin.y + cell[1] * cell_size * 100.0, target.world_position.z)

    start = state.player.position if state.player is not None else None
    if start is None:
        return None
    start_cell, goal_cell = to_cell(start), to_cell(target.world_position)
    valid = lambda cell: 0 <= cell[0] < width and 0 <= cell[1] < height and cell not in blocked
    if not valid(start_cell) or not valid(goal_cell):
        return None
    frontier: list[tuple[float, tuple[int, int]]] = [(0.0, start_cell)]
    came_from: dict[tuple[int, int], tuple[int, int] | None] = {start_cell: None}
    cost: dict[tuple[int, int], float] = {start_cell: 0.0}
    while frontier:
        _, current = heapq.heappop(frontier)
        if current == goal_cell:
            break
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            neighbor = (current[0] + dx, current[1] + dy)
            if not valid(neighbor):
                continue
            new_cost = cost[current] + 1.0
            if new_cost >= cost.get(neighbor, float("inf")):
                continue
            cost[neighbor] = new_cost
            heapq.heappush(frontier, (new_cost + abs(goal_cell[0] - neighbor[0]) + abs(goal_cell[1] - neighbor[1]), neighbor))
            came_from[neighbor] = current
    if goal_cell not in came_from:
        return None
    cells: list[tuple[int, int]] = []
    current: tuple[int, int] | None = goal_cell
    while current is not None:
        cells.append(current)
        current = came_from[current]
    cells.reverse()
    return [to_world(cell) for cell in cells], "occupancy_grid_astar"


def plan_route(
    state: GameState,
    target: NavigationTarget,
    routes: tuple["Route", ...] | list["Route"] = (),
    map_scale_m_per_pixel: float | None = None,
) -> RoutePlan:
    """Build a route envelope from telemetry and an optional caller route.

    The service layer passes no persisted routes by default. This pure helper
    still accepts an explicit route so design-time comparison and deliberate
    opt-in experiments remain possible without making historical waypoints a
    hidden runtime dependency.

    The function never pretends that a screenshot has supplied map scale. If
    scale is absent, the plan exposes world-space distances and explicitly
    marks the map-scale field unavailable so callers know when to re-check the
    map visually.
    """
    start = state.player.position if state.player is not None else None
    matching = [route for route in routes if route.map_id == target.map_id or route.map_id == state.map_id]
    landmark_id = target.metadata.get("landmark_id") if target.metadata else None
    chosen = next(
        (
            route for route in matching
            if landmark_id is not None
            and any(point.landmark_id == landmark_id for point in route.waypoints)
        ),
        matching[0] if matching else None,
    )
    runtime_path = _runtime_path_points(state, target)
    runtime_points = runtime_path[0] if runtime_path is not None else []
    runtime_source = runtime_path[1] if runtime_path is not None else None
    waypoints = list(chosen.waypoints) if chosen is not None and runtime_path is None else []

    def remaining_positions(values: list[tuple[str, Vec3, str]]) -> list[tuple[str, Vec3, str]]:
        if start is None or not values:
            return values
        if len(values) == 1:
            return [] if _world_distance_m(start, values[0][1]) <= 1.5 else values
        best_segment = 0
        best_distance = float("inf")
        for index, (_, first, _) in enumerate(values[:-1]):
            second = values[index + 1][1]
            dx, dy, dz = second.x - first.x, second.y - first.y, second.z - first.z
            length_sq = dx * dx + dy * dy + dz * dz
            raw_t = ((start.x - first.x) * dx + (start.y - first.y) * dy + (start.z - first.z) * dz) / length_sq if length_sq > 0.0 else 0.0
            t = min(1.0, max(0.0, raw_t))
            projected = Vec3(first.x + dx * t, first.y + dy * t, first.z + dz * t)
            distance = _world_distance_m(start, projected)
            if distance < best_distance:
                best_segment, best_distance = index, distance
        if best_distance <= 1.5:
            next_index = best_segment + 1
            while next_index < len(values) and _world_distance_m(start, values[next_index][1]) <= 1.5:
                next_index += 1
            return values[next_index:]
        nearest_index = min(range(len(values)), key=lambda index: _world_distance_m(start, values[index][1]))
        return values if nearest_index == 0 else values[nearest_index:]

    route_items: list[tuple[str, Vec3, str]] = []
    if runtime_points:
        route_items.extend((f"path-{index + 1}", position, runtime_source or "runtime_path") for index, position in enumerate(runtime_points) if _world_distance_m(target.world_position, position) > 0.25)
    else:
        route_items.extend((waypoint.waypoint_id, waypoint.position, "known_route") for waypoint in waypoints)
    route_items.append((target.target_id, target.world_position, "target"))
    remaining = remaining_positions(route_items)
    points: list[tuple[str, Vec3, str]] = [("start", start, "telemetry")] if start is not None else []
    points.extend(remaining)

    legs: list[dict[str, Any]] = []
    expected = 0.0
    for (from_id, first, _), (to_id, second, source) in zip(points, points[1:]):
        distance = _world_distance_m(first, second)
        expected += distance
        legs.append({
            "from": from_id,
            "to": to_id,
            "from_position": {"x": first.x, "y": first.y, "z": first.z},
            "to_position": {"x": second.x, "y": second.y, "z": second.z},
            "distance_m": round(distance, 3),
            "bearing_degrees": round(_world_bearing(first, second), 3),
            "source": source,
        })
    direct = _world_distance_m(start, target.world_position) if start is not None else None
    notes: list[str] = []
    has_recorded_route = chosen is not None and bool(waypoints)
    if runtime_path is None and not has_recorded_route:
        notes.append("No global navmesh, occupancy-grid, or recorded route matched this target; do not steer directly to the final target bearing.")
    elif runtime_path is None:
        notes.append("Recorded route is preferred over the final-target vector; validate each leg with local clearance before movement.")
    if map_scale_m_per_pixel is None:
        scale_status = "unavailable"
        notes.append("Map UI scale is not exposed by current runtime telemetry; use a screenshot only for high-level path confirmation.")
    elif not math.isfinite(float(map_scale_m_per_pixel)) or float(map_scale_m_per_pixel) <= 0:
        raise NavigationDataError("map scale must be a positive finite number")
    else:
        scale_status = "provided_by_caller"
    return RoutePlan(
        map_id=state.map_id,
        target_id=target.target_id,
        target_label=target.label,
        start_position=start,
        target_position=target.world_position,
        waypoint_ids=tuple(item[0] for item in points[1:-1]) if start is not None else tuple(),
        legs=tuple(legs),
        straight_line_distance_m=round(direct, 3) if direct is not None else None,
        expected_distance_m=round(expected, 3) if points and start is not None else None,
        path_preference=("navmesh_path" if runtime_path is not None and runtime_source != "occupancy_grid_astar" else "occupancy_grid_astar" if runtime_path is not None else "known_route" if has_recorded_route else "route_required"),
        map_scale_status=scale_status,
        confidence=("runtime_global_path" if runtime_path is not None else "recorded_route" if has_recorded_route else "target_only_no_route"),
        notes=tuple(notes),
        route_source=(runtime_source if runtime_path is not None else "known_world" if has_recorded_route else "unavailable"),
    )


def diagnose_navigation_stall(
    state: GameState,
    *,
    intended_direction: str = "forward",
    displacement_m: float | None = None,
    vertical_change_m: float | None = None,
) -> StallDiagnosis:
    """Rank recovery actions without guessing from a single screenshot."""
    if not state.running:
        return StallDiagnosis("blocked", "wait_for_gameplay", "game telemetry says the game is not running", (), False, False)
    if state.paused or state.in_menu:
        return StallDiagnosis("blocked", "recover_session_control", "a pause or menu state must be cleared before movement", (), False, False)
    if state.in_combat or state.target is not None:
        return StallDiagnosis("blocked", "defer_navigation", "combat or a selected target owns movement", (), False, False)
    if displacement_m is not None and displacement_m >= MIN_MEANINGFUL_DISPLACEMENT_M:
        return StallDiagnosis("moving", "continue_current_leg", "fresh telemetry confirms meaningful displacement", (), False, False)

    direction = str(intended_direction).strip().lower()
    ranked: list[dict[str, Any]] = []
    if direction == "forward":
        ranked.extend([
            {"action": "move_backward", "key": "s", "reason": "reverse directly away from a likely forward collision", "confidence": "high"},
            {"action": "move_left", "key": "a", "reason": "try a perpendicular escape without changing camera heading", "confidence": "medium"},
            {"action": "move_right", "key": "d", "reason": "try the opposite perpendicular escape", "confidence": "medium"},
        ])
    else:
        ranked.append({"action": "survey_camera", "degrees": 60, "reason": "reacquire a traversable path before another pulse", "confidence": "high"})
    if vertical_change_m is not None and abs(vertical_change_m) >= 0.2:
        ranked.insert(0, {"action": "vault", "key": "space", "reason": "telemetry suggests a small terrain step", "confidence": "conditional"})
    ranked.append({"action": "survey_camera", "degrees": 60, "reason": "inspect the path and nearby landmarks before committing to a longer pulse", "confidence": "high"})
    return StallDiagnosis(
        "stalled",
        ranked[0]["action"],
        "no meaningful fresh displacement was observed; use a short recovery action, then re-observe",
        tuple(ranked),
        True,
        True,
    )


def navigation_candidates(state: GameState) -> list[NavigationTarget]:
    """Return deduplicated spatial candidates, preserving telemetry priority."""
    candidates: list[NavigationTarget] = []
    seen: set[str] = set()

    def add(target: NavigationTarget) -> None:
        if target.target_id and target.target_id not in seen:
            seen.add(target.target_id)
            candidates.append(target)

    interactables = list(state.nearby_interactables)
    if state.nearby_interactable is not None:
        interactables.insert(0, state.nearby_interactable)
    for item in interactables:
        try:
            add(NavigationTarget.from_interactable(item, state.map_id))
        except NavigationDataError:
            continue
    for item in (*state.nearby_actors, *state.nearby_enemies):
        try:
            add(NavigationTarget.from_actor(
                item.with_relative_to(state.player), state.map_id
            ))
        except NavigationDataError:
            continue
    if state.target is not None:
        try:
            add(NavigationTarget.from_actor(
                state.target.with_relative_to(state.player), state.map_id
            ))
        except NavigationDataError:
            pass
    return candidates


def select_navigation_target(
    state: GameState,
    target_id: str | None = None,
    target_kind: str | None = None,
) -> NavigationTarget | None:
    for candidate in navigation_candidates(state):
        if target_id is not None and candidate.target_id != target_id:
            continue
        if target_kind is not None and candidate.kind != target_kind:
            continue
        return candidate
    return None


def decide_navigation_step(
    target: NavigationTarget,
    *,
    stop_distance_m: float = 1.5,
    heading_deadband_degrees: float = 8.0,
) -> NavigationDecision:
    """Describe the next safe closed-loop step without sending it.

    The controller must turn the camera when the target is outside the heading
    deadband and only request forward movement once the camera is aligned.  A
    later input layer can apply these decisions with its own arm and budgets.
    """
    distance = target.distance_m
    relative = target.relative
    if distance is None and relative is None:
        return NavigationDecision(
            "spatial_data_missing", target.target_id, None, None, None,
            reason="target has no distance or relative position",
        )
    if distance is None and relative is not None:
        distance = relative.horizontal_distance
    heading = relative.heading_error_degrees if relative else None
    if distance is not None and distance <= stop_distance_m:
        return NavigationDecision(
            "arrived", target.target_id, distance, heading, heading,
            reason="target is inside the configured stop distance",
        )
    if heading is None:
        return NavigationDecision(
            "heading_data_missing", target.target_id, distance, None, None,
            reason="target position exists but player heading telemetry is missing",
        )
    if abs(heading) > heading_deadband_degrees:
        return NavigationDecision(
            "turn_camera", target.target_id, distance, heading, heading,
            reason="camera must align before forward movement",
        )
    return NavigationDecision(
        "move_forward", target.target_id, distance, heading, heading,
        movement_y=1.0,
        reason="target is aligned and outside the stop distance",
    )


@dataclass(frozen=True)
class Landmark:
    landmark_id: str
    label: str
    kind: str
    map_id: str
    position: Vec3
    interaction_position: Vec3 | None = None
    heading_yaw: float | None = None
    source: str = "telemetry"
    confidence: str = "observed"
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Landmark":
        try:
            heading = value.get("heading_yaw")
            result = cls(
                landmark_id=str(value["landmark_id"]),
                label=str(value["label"]),
                kind=str(value["kind"]),
                map_id=str(value["map_id"]),
                position=Vec3.from_dict(value["position"]),
                interaction_position=(
                    Vec3.from_dict(value["interaction_position"])
                    if value.get("interaction_position") else None
                ),
                heading_yaw=float(heading) if heading is not None else None,
                source=str(value.get("source", "telemetry")),
                confidence=str(value.get("confidence", "observed")),
                evidence_refs=tuple(str(item) for item in value.get("evidence_refs", [])),
                metadata=dict(value.get("metadata", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NavigationDataError("invalid landmark") from exc
        if not result.landmark_id or not result.label or not result.kind or not result.map_id:
            raise NavigationDataError("landmark identity fields cannot be empty")
        if result.heading_yaw is not None and not math.isfinite(result.heading_yaw):
            raise NavigationDataError("landmark heading must be finite")
        return result


@dataclass(frozen=True)
class Waypoint:
    waypoint_id: str
    map_id: str
    position: Vec3
    landmark_id: str | None = None
    heading_yaw: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Waypoint":
        try:
            return cls(
                waypoint_id=str(value["waypoint_id"]),
                map_id=str(value["map_id"]),
                position=Vec3.from_dict(value["position"]),
                landmark_id=(
                    str(value["landmark_id"])
                    if value.get("landmark_id") is not None else None
                ),
                heading_yaw=(
                    float(value["heading_yaw"])
                    if value.get("heading_yaw") is not None else None
                ),
                metadata=dict(value.get("metadata", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NavigationDataError("invalid waypoint") from exc


@dataclass(frozen=True)
class Route:
    route_id: str
    map_id: str
    waypoints: tuple[Waypoint, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Route":
        try:
            return cls(
                route_id=str(value["route_id"]),
                map_id=str(value["map_id"]),
                waypoints=tuple(Waypoint.from_dict(item) for item in value.get("waypoints", [])),
                metadata=dict(value.get("metadata", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NavigationDataError("invalid route") from exc


class KnownWorldStore:
    """Atomic JSON persistence for discovered landmarks and routes."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read_document(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": self.SCHEMA_VERSION, "landmarks": {}, "routes": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NavigationDataError(f"cannot read known-world store: {self.path}") from exc
        if not isinstance(value, dict):
            raise NavigationDataError("known-world store must contain an object")
        if value.get("schema_version") != self.SCHEMA_VERSION:
            raise NavigationDataError("unsupported known-world schema version")
        value.setdefault("landmarks", {})
        value.setdefault("routes", {})
        return value

    def _write_document(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)

    def list_landmarks(self, map_id: str | None = None) -> list[Landmark]:
        document = self._read_document()
        result = []
        for value in document.get("landmarks", {}).values():
            landmark = Landmark.from_dict(value)
            if map_id is None or landmark.map_id == map_id:
                result.append(landmark)
        return sorted(result, key=lambda item: item.landmark_id)

    def upsert_landmark(self, landmark: Landmark) -> Landmark:
        document = self._read_document()
        document["landmarks"][landmark.landmark_id] = asdict(landmark)
        self._write_document(document)
        return landmark

    def list_routes(self, map_id: str | None = None) -> list[Route]:
        document = self._read_document()
        result = []
        for value in document.get("routes", {}).values():
            route = Route.from_dict(value)
            if map_id is None or route.map_id == map_id:
                result.append(route)
        return sorted(result, key=lambda item: item.route_id)

    def upsert_route(self, route: Route) -> Route:
        document = self._read_document()
        document["routes"][route.route_id] = asdict(route)
        self._write_document(document)
        return route
