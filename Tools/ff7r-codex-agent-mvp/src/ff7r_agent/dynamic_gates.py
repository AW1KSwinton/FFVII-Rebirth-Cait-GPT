"""Automatic doors and gates: dynamic obstacles a route goes through.

Live 2026-10-13 (the prologue, Zack): a walkway neck under 1 m wide was shut
by an automatic door. The navmesh was baked with the doorway open (tile 684's
polygons 4, 8 and 5 join straight through it); only the route sweep met the
door closed. Its bounds went into the static obstacle overlay, no route past
it existed, and the planner refused every route (static_obstacle_global_
replan_unavailable) although the door opened by itself as Zack walked up.

A swept obstacle is a *dynamic gate*, not a static wall, when

- its actor class or name says door, gate or shutter (``name:...``);
- its bounds moved, or it vanished from where an earlier sweep saw it
  (``bounds_changed``, ``vanished_between_sweeps``): anything that moved is
  dynamic and is never kept as a static obstacle;
- it is a thin leaf spanning the whole of a narrow neck whose navmesh is
  continuous through the footprint, and treating it as a wall leaves no
  route at all (``sealed_neck``).

The planner routes through a gate at a cost (``GATE_PENALTY_M`` of walking per
gate, so a short way round still wins), the plan lists each gate as an explicit
waypoint with its reason and evidence, and the follower stops
``GATE_WAIT_DISTANCE_M`` short of it, re-sweeping just the gate's footprint for
a few seconds until it is clear, or reports ``gate_did_not_open``.

Read-only: it plans on the navmesh and reads sweeps; it sends no input.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import Vec3
from .route_sweep import ROUTE_BLOCKING_KINDS, classify_hit, obstacle_from_bounds, obstacle_key
from .static_obstacles import StaticObstacle, StaticObstacleSnapshot

GATE_SCHEMA = "ff7r-dynamic-gate-v1"
GATE_KIND = "dynamic_gate"

# Planner: walking a gate costs about this much route length (a wait of a few
# seconds at a run), so a way round that is not much longer is preferred.
GATE_PENALTY_M = 20.0
# Cloud's radius and a little: the route touches a gate this close to its box.
GATE_CLEARANCE_CM = 45.0
# A route on another level, further than this above or below the box, is no
# route through the gate.
GATE_VERTICAL_SLACK_CM = 150.0

# Follower: stop this far short of the gate (along the route), then re-sweep
# the gate's footprint every poll for at most the wait.
GATE_WAIT_DISTANCE_M = 2.0
GATE_WAIT_SECONDS = 4.0
GATE_POLL_SECONDS = 0.5
# No sprinting into a gate that may be opening: it slows from here.
GATE_SPRINT_CUTOFF_M = 5.0
# How far past the gate a probe sweep carries on.
GATE_PROBE_BEYOND_M = 1.5
GATE_PROBE_HIT_REACH_CM = 60.0

# A sealed neck: the leaf is thin, covers the walkway's width where the
# navmesh narrows, and the route crosses it square on.
NECK_MAX_WIDTH_M = 2.5
NECK_REACH_CM = 150.0
NECK_SPAN_TOLERANCE_CM = 30.0
DOOR_MAX_THICKNESS_M = 0.8
DOOR_MIN_WIDTH_M = 0.6
DOOR_MIN_HEIGHT_M = 1.2
DOOR_CROSSING_MIN_COS = 0.5
# Bounds that differ by more than this between sweeps have moved.
BOUNDS_TOLERANCE_CM = 5.0
# A spot hit (a ledge, level collision) this close to a gate's bounds is the
# gate itself: live in the prologue, the floor re-sampling read the shut
# door's leaf as a 62 cm ledge inside its footprint.
GATE_SPOT_MARGIN_CM = 10.0

REGISTRY_TTL_SECONDS = 600.0
REGISTRY_MAX_ITEMS = 64
# What the planner says when treating an obstacle as a wall leaves no route.
SEALED_REPLAN_REASONS = (
    "static_obstacle_global_replan_unavailable",
    "static_obstacle_global_replan_still_blocked",
    "static_obstacle_local_detour_unproven",
)

_NAME_TOKENS = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|[0-9]+")
GATE_NAME_TOKENS = frozenset({"door", "doors", "gate", "gates", "shutter", "shutters", "hatch", "portcullis"})
# Parts of a door that do not open by themselves (a knob door opens at its
# field prompt; a frame never moves).
NOT_AUTOMATIC_TOKENS = frozenset({"frame", "knob"})


def gate_id_for(actor: str) -> str:
    return "gate-" + hashlib.sha256(actor.encode("utf-8")).hexdigest()[:16]


def obstacle_id_for(actor: str) -> str:
    """The id route_sweep.obstacle_from_bounds gives an actor's obstacle."""
    return "sweep-" + hashlib.sha256(actor.encode("utf-8")).hexdigest()[:16]


def name_signal(hit: Mapping[str, Any]) -> str | None:
    """``name:door`` when the actor's class or instance name says door/gate.

    Only the class name and the instance name are read, never the level path
    (``.../2500-CSMOA_Gimmick.2500-CSMOA_Gimmick:PersistentLevel.`` names a
    tent as readily as a door).
    """
    actor = str(hit.get("actor") or "")
    names = [actor.split(" ", 1)[0], actor.rsplit(".", 1)[-1], str(hit.get("actor_class") or "")]
    for name in names:
        tokens = [token.lower() for token in _NAME_TOKENS.findall(name)]
        if any(token in NOT_AUTOMATIC_TOKENS for token in tokens):
            continue
        for token in tokens:
            if token in GATE_NAME_TOKENS:
                return "name:" + token
    return None


# ---------------------------------------------------------------- geometry


@dataclass(frozen=True)
class Box:
    """An actor's axis-aligned bounds: centre and half extents, in cm."""

    cx: float
    cy: float
    cz: float
    ex: float
    ey: float
    ez: float

    @classmethod
    def from_bounds(cls, bounds: Any) -> "Box | None":
        try:
            origin, extent = bounds["origin"], bounds["extent"]
            values = [float(origin["x"]), float(origin["y"]), float(origin["z"]),
                      float(extent["x"]), float(extent["y"]), float(extent.get("z") or 0.0)]
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        if not all(math.isfinite(value) for value in values) or values[3] <= 0.0 or values[4] <= 0.0:
            return None
        return cls(*values)

    def grown(self, cm: float) -> "Box":
        return replace(self, ex=self.ex + cm, ey=self.ey + cm)

    @property
    def along_x(self) -> bool:
        """The leaf's long side runs along x."""
        return self.ex >= self.ey

    @property
    def thickness_m(self) -> float:
        return 2.0 * min(self.ex, self.ey) / 100.0

    @property
    def width_m(self) -> float:
        return 2.0 * max(self.ex, self.ey) / 100.0

    @property
    def height_m(self) -> float:
        return 2.0 * self.ez / 100.0

    def distance_cm(self, x: float, y: float) -> float:
        dx = max(abs(x - self.cx) - self.ex, 0.0)
        dy = max(abs(y - self.cy) - self.ey, 0.0)
        return math.hypot(dx, dy)

    def contains_xy(self, x: float, y: float, margin_cm: float = 0.0) -> bool:
        return abs(x - self.cx) <= self.ex + margin_cm and abs(y - self.cy) <= self.ey + margin_cm


def _clip_segment(first: Vec3, second: Vec3, box: Box) -> tuple[float, float] | None:
    """The parameters over which a segment lies inside a box (Liang-Barsky, in x/y)."""
    low, high = 0.0, 1.0
    dx, dy = second.x - first.x, second.y - first.y
    for p, q in ((-dx, first.x - (box.cx - box.ex)), (dx, (box.cx + box.ex) - first.x),
                 (-dy, first.y - (box.cy - box.ey)), (dy, (box.cy + box.ey) - first.y)):
        if abs(p) < 1e-9:
            if q < 0.0:
                return None
            continue
        ratio = q / p
        if p < 0.0:
            if ratio > high:
                return None
            low = max(low, ratio)
        else:
            if ratio < low:
                return None
            high = min(high, ratio)
    return (low, high) if low <= high else None


def _horizontal_m(first: Vec3, second: Vec3) -> float:
    return math.hypot(second.x - first.x, second.y - first.y) / 100.0


@dataclass(frozen=True)
class RouteSpan:
    """Where a route polyline runs inside a box, as distances along it (m)."""

    entry_m: float
    exit_m: float
    segments: tuple[int, ...]
    direction: tuple[float, float]

    @property
    def along_m(self) -> float:
        return (self.entry_m + self.exit_m) / 2.0


def route_span(points: Sequence[Vec3], box: Box, *, vertical_slack_cm: float = GATE_VERTICAL_SLACK_CM) -> RouteSpan | None:
    """The stretch of ``points`` (a polyline, cm) inside ``box`` in x/y, or None.

    A stretch more than ``vertical_slack_cm`` above or below the box (the
    route's other level) is not inside it.
    """
    start_m = 0.0
    entry = exit_ = None
    segments: list[int] = []
    direction = (0.0, 0.0)
    for index, (first, second) in enumerate(zip(points, points[1:])):
        length = _horizontal_m(first, second)
        clipped = _clip_segment(first, second, box)
        if clipped is not None and length > 0.0:
            low, high = clipped
            z = first.z + (second.z - first.z) * low
            if abs(z - box.cz) <= box.ez + vertical_slack_cm:
                if entry is None:
                    entry = start_m + low * length
                    direction = ((second.x - first.x) / (length * 100.0), (second.y - first.y) / (length * 100.0))
                exit_ = start_m + high * length
                segments.append(index)
        start_m += length
    if entry is None or exit_ is None:
        return None
    return RouteSpan(entry, exit_, tuple(segments), direction)


def point_along(points: Sequence[Vec3], along_m: float) -> Vec3:
    """The point ``along_m`` metres along a polyline (clamped to its ends)."""
    if not points:
        raise ValueError("route has no points")
    remaining = max(0.0, along_m)
    for first, second in zip(points, points[1:]):
        length = _horizontal_m(first, second)
        if length > 0.0 and remaining <= length:
            t = remaining / length
            return Vec3(first.x + (second.x - first.x) * t, first.y + (second.y - first.y) * t,
                        first.z + (second.z - first.z) * t)
        remaining -= length
    return points[-1]


def polygon_overlaps_box(vertices: Sequence[Vec3], box: Box) -> bool:
    """A convex polygon and an axis-aligned box overlap in x/y (separating axes)."""
    if len(vertices) < 3:
        return False
    xs, ys = [v.x for v in vertices], [v.y for v in vertices]
    if max(xs) < box.cx - box.ex or min(xs) > box.cx + box.ex or max(ys) < box.cy - box.ey or min(ys) > box.cy + box.ey:
        return False
    corners = [(box.cx + sx * box.ex, box.cy + sy * box.ey) for sx in (-1, 1) for sy in (-1, 1)]
    for first, second in zip(vertices, list(vertices[1:]) + [vertices[0]]):
        nx, ny = second.y - first.y, first.x - second.x
        if nx == 0.0 and ny == 0.0:
            continue
        polygon = [nx * v.x + ny * v.y for v in vertices]
        mine = [nx * x + ny * y for x, y in corners]
        if max(polygon) < min(mine) or min(polygon) > max(mine):
            return False
    return True


def neck_evidence(graph: Any, plan: Any, bounds: Any) -> dict[str, Any] | None:
    """Whether the planned route crosses a thin leaf that seals a narrow neck.

    The route has to cross the leaf square on over navmesh polygons that stay
    joined through its footprint, the walkway where it crosses has to be no
    wider than ``NECK_MAX_WIDTH_M`` (the corridor's portals there), and the
    leaf has to span all of it. None when any of that fails: the obstacle is
    then a wall the planner routes round, as before.
    """
    box = Box.from_bounds(bounds)
    if box is None or graph is None:
        return None
    if box.thickness_m > DOOR_MAX_THICKNESS_M or box.width_m < DOOR_MIN_WIDTH_M:
        return None
    if box.ez > 0.0 and box.height_m < DOOR_MIN_HEIGHT_M:
        return None
    waypoints = tuple(getattr(plan, "waypoints", ()) or ())
    span = route_span(waypoints, box.grown(GATE_CLEARANCE_CM))
    if span is None:
        return None
    # Square on: the route runs along the leaf's short side.
    short_axis = (0.0, 1.0) if box.along_x else (1.0, 0.0)
    crossing_cos = abs(span.direction[0] * short_axis[0] + span.direction[1] * short_axis[1])
    if crossing_cos < DOOR_CROSSING_MIN_COS:
        return None
    corridor = [int(ref) for ref in getattr(plan, "polygon_corridor", ()) or ()]
    portals = tuple(getattr(plan, "portals", ()) or ())
    polygons = getattr(graph, "polygons", {})
    if len(portals) != len(corridor) + 1:
        return None
    under = [index for index, ref in enumerate(corridor)
             if ref in polygons and polygon_overlaps_box(polygons[ref].vertices, box)]
    if not under or under[0] == 0 or under[-1] == len(corridor) - 1:
        return None
    # Portals into and out of the polygons under the leaf; portal k joins
    # corridor[k - 1] to corridor[k].
    long_low = (box.cx if box.along_x else box.cy) - max(box.ex, box.ey) - NECK_SPAN_TOLERANCE_CM
    long_high = (box.cx if box.along_x else box.cy) + max(box.ex, box.ey) + NECK_SPAN_TOLERANCE_CM
    widths: list[float] = []
    for k in range(under[0], under[-1] + 2):
        portal = portals[k]
        middle_x, middle_y = (portal.left.x + portal.right.x) / 2.0, (portal.left.y + portal.right.y) / 2.0
        if box.distance_cm(middle_x, middle_y) > NECK_REACH_CM:
            continue
        along = [(portal.left.x if box.along_x else portal.left.y), (portal.right.x if box.along_x else portal.right.y)]
        if min(along) < long_low or max(along) > long_high:
            return None  # the walkway is wider than the leaf here
        widths.append(math.hypot(portal.left.x - portal.right.x, portal.left.y - portal.right.y) / 100.0)
    if not widths or min(widths) > NECK_MAX_WIDTH_M:
        return None
    return {
        "neck_width_m": round(min(widths), 3),
        "portal_widths_m": [round(width, 3) for width in widths],
        "thickness_m": round(box.thickness_m, 3),
        "width_m": round(box.width_m, 3),
        "height_m": round(box.height_m, 3) if box.ez > 0.0 else None,
        "crossing_cos": round(crossing_cos, 3),
        "under_polygons": [corridor[index] for index in under],
        "spans_neck": True,
    }


# ------------------------------------------------------------------- gates


def _point_dict(point: Vec3) -> dict[str, float]:
    return {"x": float(point.x), "y": float(point.y), "z": float(point.z)}


def _clean_bounds(bounds: Mapping[str, Any]) -> dict[str, Any]:
    return {"origin": {axis: float(bounds["origin"][axis]) for axis in ("x", "y", "z")},
            "extent": {axis: float(bounds["extent"].get(axis) or 0.0) for axis in ("x", "y", "z")}}


@dataclass(frozen=True)
class DynamicGate:
    """A swept obstacle that opens by itself, with why it is thought to."""

    gate_id: str
    actor: str
    actor_class: str | None
    bounds: Mapping[str, Any]
    signals: tuple[str, ...]
    evidence: Mapping[str, Any]
    map_id: str = ""
    first_t: float = 0.0

    @property
    def reason(self) -> str:
        return "+".join(self.signals)

    def covers(self, point: Mapping[str, Any], margin_cm: float = GATE_SPOT_MARGIN_CM) -> bool:
        """Whether a point lies in the gate's bounds (a contact on the gate itself)."""
        box = Box.from_bounds(self.bounds)
        try:
            x, y, z = float(point["x"]), float(point["y"]), float(point.get("z") or 0.0)
        except (KeyError, TypeError, ValueError, AttributeError):
            return False
        if box is None or not all(math.isfinite(value) for value in (x, y, z)):
            return False
        return box.contains_xy(x, y, margin_cm) and abs(z - box.cz) <= box.ez + margin_cm

    def obstacle(self, *, sequence: int = 0, t: float = 0.0) -> StaticObstacle:
        """The gate as the wall it would be shut (for the way round it)."""
        return obstacle_from_bounds(self.actor, self.bounds, sequence=sequence, t=t)

    def to_dict(self) -> dict[str, Any]:
        return {"schema": GATE_SCHEMA, "kind": GATE_KIND, "gate_id": self.gate_id, "actor": self.actor,
                "actor_class": self.actor_class, "bounds": dict(self.bounds), "signals": list(self.signals),
                "reason": self.reason, "evidence": dict(self.evidence), "map_id": self.map_id}


@dataclass(frozen=True)
class GatePlacement:
    """Where a planned route meets a gate."""

    gate: DynamicGate
    span: RouteSpan
    position: Vec3
    approach: Vec3
    beyond: Vec3
    waypoint_index: int
    penalty_m: float = GATE_PENALTY_M

    @property
    def along_m(self) -> float:
        return self.span.along_m

    def to_dict(self) -> dict[str, Any]:
        return {**self.gate.to_dict(), "waypoint_index": self.waypoint_index,
                "along_m": round(self.along_m, 3), "entry_m": round(self.span.entry_m, 3),
                "exit_m": round(self.span.exit_m, 3), "position": _point_dict(self.position),
                "approach": _point_dict(self.approach), "beyond": _point_dict(self.beyond),
                "wait_distance_m": GATE_WAIT_DISTANCE_M, "wait_seconds": GATE_WAIT_SECONDS,
                "penalty_m": self.penalty_m}


def placements(waypoints: Sequence[Vec3], gates: Iterable[DynamicGate], *, penalty_m: float = GATE_PENALTY_M) -> list[GatePlacement]:
    """The gates a route crosses, in walking order.

    A route that ends in front of a gate (inside its clearance, never past
    it) does not cross it.
    """
    points = tuple(waypoints)
    total_m = sum(_horizontal_m(first, second) for first, second in zip(points, points[1:]))
    found: list[GatePlacement] = []
    for gate in gates:
        box = Box.from_bounds(gate.bounds)
        if box is None:
            continue
        span = route_span(points, box.grown(GATE_CLEARANCE_CM))
        if span is None or span.exit_m >= total_m - 0.05:
            continue
        found.append(GatePlacement(
            gate=gate, span=span, position=point_along(points, span.along_m),
            approach=point_along(points, max(0.0, span.along_m - GATE_WAIT_DISTANCE_M)),
            beyond=point_along(points, span.exit_m + GATE_PROBE_BEYOND_M),
            waypoint_index=span.segments[0], penalty_m=penalty_m))
    return sorted(found, key=lambda item: item.along_m)


def plan_gates(plan: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The gate waypoints a serialized route plan carries, in walking order."""
    diagnostics = (plan or {}).get("diagnostics") if isinstance(plan, Mapping) else None
    gates = diagnostics.get("dynamic_gates") if isinstance(diagnostics, Mapping) else None
    return sorted((dict(item) for item in gates or [] if isinstance(item, Mapping)),
                  key=lambda item: float(item.get("along_m") or 0.0))


# ---------------------------------------------------------------- registry


@dataclass
class Sighting:
    """A blocking actor an earlier sweep saw, and whether it has since moved."""

    actor: str
    actor_class: str | None
    bounds: dict[str, Any]
    obstacle_id: str
    first_t: float
    last_t: float
    seen: int = 1
    dynamic_reason: str | None = None


def bounds_differ(first: Mapping[str, Any], second: Mapping[str, Any], tolerance_cm: float = BOUNDS_TOLERANCE_CM) -> bool:
    try:
        return any(abs(float(first[part][axis]) - float(second[part][axis])) > tolerance_cm
                   for part in ("origin", "extent") for axis in ("x", "y", "z"))
    except (KeyError, TypeError, ValueError):
        return False


def _sweep_coverage_error(sweep: Mapping[str, Any], segment_count: int) -> str | None:
    """Validate the native sweep's ordered, zero-based rows for a whole route.

    route_sweep.lua emits one row per requested leg, with an explicit hits
    array and boolean completion. Missing or malformed rows say nothing
    about clearance, even when every received row claims completion.
    """
    if not isinstance(sweep, Mapping) or not isinstance(sweep.get("segments"), list):
        return "route_sweep_segments_malformed"
    segments = sweep["segments"]
    if segment_count < 1 or len(segments) != segment_count:
        return f"route_sweep_segment_count_mismatch:expected={segment_count}:received={len(segments)}"
    for index, segment in enumerate(segments):
        if not isinstance(segment, Mapping):
            return f"route_sweep_segment_malformed:{index}"
        if type(segment.get("index")) is not int or segment["index"] != index:
            return f"route_sweep_segment_index_mismatch:{index}"
        if segment.get("complete") is not True or segment.get("error") is not None:
            return f"route_sweep_segment_incomplete:{index}"
        hits = segment.get("hits")
        if not isinstance(hits, list) or not all(isinstance(hit, Mapping) for hit in hits):
            return f"route_sweep_hits_malformed:{index}"
    return None


class GateRegistry:
    """What sweeps have shown about actors that block, and the gates found.

    Kept in memory and briefly (the world changes and restarts forget), keyed
    by map and actor; the service shares one across route attempts, like
    RecentBlockers. A sighting whose bounds change, or that is gone from where
    a later sweep passes through its footprint, is dynamic from then on.
    """

    def __init__(self, *, ttl_seconds: float = REGISTRY_TTL_SECONDS, max_items: int = REGISTRY_MAX_ITEMS,
                 penalty_m: float = GATE_PENALTY_M, clock: Callable[[], float] | None = None) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.max_items = int(max_items)
        self.penalty_m = float(penalty_m)
        self.clock = clock or time.monotonic
        self._sightings: dict[tuple[str, str], Sighting] = {}
        self._gates: dict[tuple[str, str], DynamicGate] = {}

    def _prune(self) -> None:
        now = self.clock()
        self._sightings = {key: item for key, item in self._sightings.items() if now - item.last_t <= self.ttl_seconds}
        self._gates = {key: item for key, item in self._gates.items() if now - item.first_t <= self.ttl_seconds}
        for store in (self._sightings, self._gates):
            while len(store) > self.max_items:
                store.pop(next(iter(store)))

    def sighting(self, map_id: str, actor: str) -> Sighting | None:
        self._prune()
        return self._sightings.get((map_id, actor))

    def sighting_for_obstacle(self, map_id: str, obstacle_id: str) -> Sighting | None:
        self._prune()
        return next((item for (map_, _), item in self._sightings.items()
                     if map_ == map_id and item.obstacle_id == obstacle_id), None)

    def sight(self, map_id: str, hit: Mapping[str, Any]) -> Sighting | None:
        """Record a blocking hit on an actor with bounds."""
        actor, bounds = str(hit.get("actor") or ""), hit.get("actor_bounds")
        if not actor or Box.from_bounds(bounds) is None:
            return None
        self._prune()
        now = self.clock()
        key = (map_id, actor)
        known = self._sightings.get(key)
        if known is None:
            known = self._sightings[key] = Sighting(
                actor=actor, actor_class=hit.get("actor_class"), bounds=_clean_bounds(bounds),
                obstacle_id=obstacle_id_for(actor), first_t=now, last_t=now)
            return known
        if bounds_differ(known.bounds, bounds):
            known.dynamic_reason = known.dynamic_reason or "bounds_changed"
        known.bounds, known.last_t, known.seen = _clean_bounds(bounds), now, known.seen + 1
        return known

    def reconcile(self, map_id: str, sweep: Mapping[str, Any], points: Sequence[Mapping[str, float]],
                  *, only: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Compare a sweep with the sightings it passed through: what moved or vanished.

        An actor whose bounds differ from its sighting moved; one whose
        footprint the swept route runs through, when every requested segment
        was swept completely and no hit names it, is gone. Both are dynamic from now on.
        """
        self._prune()
        wanted = set(only) if only is not None else None
        coverage_verified = _sweep_coverage_error(sweep, len(points) - 1) is None
        segments = sweep.get("segments") if isinstance(sweep, Mapping) else None
        if not isinstance(segments, list):
            return []
        present: dict[str, Mapping[str, Any]] = {}
        for segment in segments:
            if not isinstance(segment, Mapping) or not isinstance(segment.get("hits"), list):
                continue
            for hit in segment["hits"]:
                if isinstance(hit, Mapping) and hit.get("actor"):
                    present.setdefault(str(hit["actor"]), hit)
        route = [Vec3(float(p["x"]), float(p["y"]), float(p["z"])) for p in points]
        changes: list[dict[str, Any]] = []
        for (map_, actor), sighting in list(self._sightings.items()):
            if map_ != map_id or (wanted is not None and actor not in wanted):
                continue
            hit = present.get(actor)
            if hit is not None:
                # A failed bounds read does not make an actor that was hit
                # disappear; it just cannot establish a measured movement.
                bounds = hit.get("actor_bounds")
                if Box.from_bounds(bounds) is not None and bounds_differ(sighting.bounds, bounds):
                    changes.append({"actor": actor, "change": "bounds_changed",
                                    "previous_bounds": sighting.bounds, "bounds": _clean_bounds(hit["actor_bounds"])})
                    sighting.dynamic_reason = sighting.dynamic_reason or "bounds_changed"
                    sighting.bounds = _clean_bounds(hit["actor_bounds"])
                continue
            # A measured bounds change is positive evidence even on a partial
            # route; absence requires complete coverage of every requested leg.
            if not coverage_verified or sighting.dynamic_reason == "vanished_between_sweeps":
                continue
            box = Box.from_bounds(sighting.bounds)
            span = route_span(route, box) if box is not None else None
            if span is None or not span.segments:
                continue
            changes.append({"actor": actor, "change": "vanished_between_sweeps",
                            "previous_bounds": sighting.bounds, "bounds": None})
            sighting.dynamic_reason = sighting.dynamic_reason or "vanished_between_sweeps"
        return changes

    def add_gate(self, map_id: str, sighting_or_hit: Any, signals: Sequence[str], evidence: Mapping[str, Any]) -> DynamicGate:
        if isinstance(sighting_or_hit, Sighting):
            actor, actor_class, bounds = sighting_or_hit.actor, sighting_or_hit.actor_class, sighting_or_hit.bounds
        else:
            actor, actor_class = str(sighting_or_hit["actor"]), sighting_or_hit.get("actor_class")
            bounds = _clean_bounds(sighting_or_hit["actor_bounds"])
        self._prune()
        key = (map_id, actor)
        known = self._gates.get(key)
        merged = tuple(dict.fromkeys((*(known.signals if known else ()), *signals)))
        gate = DynamicGate(gate_id=gate_id_for(actor), actor=actor, actor_class=actor_class, bounds=bounds,
                           signals=merged, evidence={**(known.evidence if known else {}), **evidence},
                           map_id=map_id, first_t=known.first_t if known else self.clock())
        self._gates[key] = gate
        return gate

    def gate_for(self, map_id: str, actor: str) -> DynamicGate | None:
        self._prune()
        return self._gates.get((map_id, actor))

    def gates(self, map_id: str) -> tuple[DynamicGate, ...]:
        self._prune()
        return tuple(sorted((gate for (map_, _), gate in self._gates.items() if map_ == map_id),
                            key=lambda gate: gate.gate_id))

    def digest(self, map_id: str) -> str:
        """What a plan made with these gates depends on."""
        text = "|".join(
            f"{gate.gate_id}:{gate.reason}:"
            + str(tuple(gate.bounds[part][axis] for part in ("origin", "extent") for axis in ("x", "y", "z")))
            for gate in self.gates(map_id))
        return hashlib.sha256(f"{self.penalty_m}|{text}".encode("utf-8")).hexdigest()[:16]

    def triage(self, map_id: str, hits: Sequence[Mapping[str, Any]], *, plan: Any = None, graph: Any = None) -> "Triage":
        """Split blocking hits into gates (new or already known) and walls.

        Only an actor with bounds can be a gate; a spot of level collision, a
        ledge or a wide actor is always a wall. A hit that a name, a known gate
        or a moved sighting makes a gate never reaches the static overlay.
        """
        walls: list[Mapping[str, Any]] = []
        found: list[tuple[DynamicGate, bool]] = []
        for hit in hits:
            key = obstacle_key(hit) or ""
            sighting = self.sight(map_id, hit) if key.startswith("actor:") else None
            if sighting is None:
                walls.append(hit)
                continue
            known = self.gate_for(map_id, sighting.actor)
            signals = [name_signal(hit)] if name_signal(hit) else []
            if sighting.dynamic_reason:
                signals.append(sighting.dynamic_reason)
            if known is None and not signals:
                walls.append(hit)
                continue
            evidence: dict[str, Any] = {"mobility": hit.get("mobility"), "component": hit.get("component")}
            neck = neck_evidence(graph, plan, sighting.bounds) if plan is not None else None
            if neck is not None:
                evidence["neck"] = neck
            gate = self.add_gate(map_id, sighting, signals, evidence)
            found.append((gate, known is None))
        # A spot hit inside a gate's bounds is that gate seen shut, not a wall.
        candidates = {gate.gate_id: gate for gate in (*(gate for gate, _ in found), *self.gates(map_id))}
        kept: list[Mapping[str, Any]] = []
        absorbed: list[tuple[str, Mapping[str, Any]]] = []
        for hit in walls:
            owner = None
            if not (obstacle_key(hit) or "").startswith("actor:"):
                owner = next((gate for gate in candidates.values() if gate.covers(hit.get("impact") or {})), None)
            if owner is None:
                kept.append(hit)
            else:
                absorbed.append((owner.gate_id, hit))
        return Triage(tuple(kept), tuple(gate for gate, _ in found),
                      tuple(gate for gate, new in found if new), tuple(absorbed))

    def promote_sealing(self, map_id: str, hits: Sequence[Mapping[str, Any]], *, plan: Any, graph: Any) -> list[DynamicGate]:
        """Gates among obstacles that, as walls, left no route.

        The caller has just found the planner out of routes with these
        admitted; those that are thin leaves sealing a narrow neck the route
        crosses (neck_evidence) are gates after all.
        """
        promoted: list[DynamicGate] = []
        for hit in hits:
            sighting = self.sight(map_id, hit) if (obstacle_key(hit) or "").startswith("actor:") else None
            if sighting is None:
                continue
            neck = neck_evidence(graph, plan, sighting.bounds)
            if neck is None:
                continue
            signals = ["sealed_neck"] + ([name_signal(hit)] if name_signal(hit) else [])
            promoted.append(self.add_gate(map_id, sighting, signals,
                                          {"neck": neck, "mobility": hit.get("mobility"), "component": hit.get("component")}))
        return promoted


@dataclass(frozen=True)
class Triage:
    walls: tuple[Mapping[str, Any], ...]
    gates: tuple[DynamicGate, ...]
    new_gates: tuple[DynamicGate, ...]
    # (gate_id, hit): spot hits on a gate's own bounds, neither wall nor gate.
    absorbed: tuple[tuple[str, Mapping[str, Any]], ...] = ()


# ---------------------------------------------------------------- planning


class GateAvoidingOverlay:
    """An obstacle overlay with some gates shut as walls, for planning only.

    ``plan_with_static_obstacles`` asks its overlay for a planning snapshot;
    this adds the gate walls to the real overlay's without touching it.
    """

    def __init__(self, overlay: Any, walls: Sequence[StaticObstacle]) -> None:
        self._overlay = overlay
        self._walls = tuple(walls)

    def planning_snapshot(self, *, now_t: float | None = None) -> StaticObstacleSnapshot:
        snapshot = self._overlay.planning_snapshot(now_t=now_t)
        known = {item.obstacle_id for item in snapshot.obstacles}
        return replace(snapshot, obstacles=snapshot.obstacles + tuple(w for w in self._walls if w.obstacle_id not in known))


def _length_m(plan: Any) -> float:
    length = getattr(plan, "total_estimated_path_length_m", None)
    return float(length) if isinstance(length, (int, float)) else math.inf


def annotate_plan(plan: Any, found: Sequence[GatePlacement], planning: Mapping[str, Any]) -> Any:
    """The plan with its gate waypoints and how they were weighed in diagnostics."""
    diagnostics = {**dict(plan.diagnostics), "dynamic_gates": [item.to_dict() for item in found],
                   "dynamic_gate_planning": dict(planning)}
    return replace(plan, diagnostics=diagnostics)


def plan_through_gates(plan_with: Callable[[Any], Any], overlay: Any, gates: Sequence[DynamicGate],
                       *, penalty_m: float = GATE_PENALTY_M, sequence: int = 0, t: float = 0.0) -> Any:
    """Plan with the known gates passable, at a cost.

    ``plan_with(overlay)`` plans on a given obstacle overlay. The gates are
    not in the real one, so the first plan goes straight through them; if it
    crosses any, a second plan shuts them as walls, and that way round wins
    only when it is shorter than the way through plus ``penalty_m`` per gate
    crossed. The chosen plan carries its gate waypoints in diagnostics.
    """
    through = plan_with(overlay)
    if getattr(through, "status", None) != "complete":
        return through
    crossed = placements(through.waypoints, gates, penalty_m=penalty_m)
    planning: dict[str, Any] = {"penalty_m": penalty_m, "known_gates": len(gates),
                                "through_length_m": _length_m(through), "through_gates": len(crossed)}
    if not crossed:
        return annotate_plan(through, [], {**planning, "chosen": "direct"})
    around = plan_with(GateAvoidingOverlay(overlay, [item.gate.obstacle(sequence=sequence, t=t) for item in crossed]))
    planning["around_status"] = getattr(around, "status", None)
    if getattr(around, "status", None) == "complete":
        crossed_around = placements(around.waypoints, gates, penalty_m=penalty_m)
        planning.update(around_length_m=_length_m(around), around_gates=len(crossed_around))
        if _length_m(around) + penalty_m * len(crossed_around) < _length_m(through) + penalty_m * len(crossed):
            return annotate_plan(around, crossed_around, {**planning, "chosen": "around"})
    return annotate_plan(through, crossed, {**planning, "chosen": "through"})


# ----------------------------------------------------------------- follower


@dataclass(frozen=True)
class GateProbe:
    """One re-sweep of a gate's footprint."""

    clear: bool
    verified: bool
    blocking: tuple[Mapping[str, Any], ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"clear": self.clear, "verified": self.verified, "error": self.error,
                "blocking": [dict(item) for item in self.blocking]}


def probe_gate(sweep: Mapping[str, Any], gate: Mapping[str, Any]) -> GateProbe:
    """Whether a complete sweep of both requested legs still meets the gate.

    ``gate`` is a plan's gate waypoint (GatePlacement.to_dict). Clearance
    requires approach -> position -> beyond, in that order, and no contact
    on the gate or blocking hit near its footprint. Planner exceptions for
    doors and start contacts cannot establish that a gate is open.
    """
    error = _sweep_coverage_error(sweep, len(gate_probe_points(gate)) - 1)
    if error is not None:
        return GateProbe(clear=False, verified=False, error=error)
    box = Box.from_bounds(gate.get("bounds"))
    if box is None:
        return GateProbe(clear=False, verified=False, error="gate_probe_bounds_unverified")
    actor = gate.get("actor")
    blocking = []
    for segment in sweep["segments"]:
        for hit in segment["hits"]:
            kind = classify_hit(hit)
            known_gate = hit.get("actor") == actor
            if not known_gate and kind not in ROUTE_BLOCKING_KINDS and kind != "door":
                continue
            near = known_gate
            if not known_gate:
                try:
                    x, y = float(hit["impact"]["x"]), float(hit["impact"]["y"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    error = "gate_probe_hit_location_unverified"
                    continue
                if not math.isfinite(x) or not math.isfinite(y):
                    error = "gate_probe_hit_location_unverified"
                    continue
                near = box.contains_xy(x, y, GATE_PROBE_HIT_REACH_CM)
            if near:
                blocking.append({"actor": hit.get("actor"), "kind": kind,
                                 "segment": segment["index"], "impact": hit.get("impact"),
                                 "distance_cm": hit.get("distance_cm")})
    return GateProbe(clear=error is None and not blocking, verified=error is None,
                     blocking=tuple(blocking), error=error)


@dataclass(frozen=True)
class GateWait:
    """How waiting at a gate ended."""

    outcome: str  # opened | gate_did_not_open | gate_state_unverified | interrupted
    waited_seconds: float
    polls: int
    probe: GateProbe | None = None
    interruption: str | None = None
    sweep_errors: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "waited_seconds": round(self.waited_seconds, 3), "polls": self.polls,
                "sweep_errors": self.sweep_errors, "interruption": self.interruption,
                "last_probe": self.probe.to_dict() if self.probe is not None else None}


def gate_probe_points(gate: Mapping[str, Any]) -> list[dict[str, float]]:
    """Feet positions a probe sweeps: the approach, through the gate, and past it."""
    return [dict(gate[name]) for name in ("approach", "position", "beyond")]


def wait_for_gate(gate: Mapping[str, Any], *, sweep: Callable[[list[dict[str, float]]], Mapping[str, Any]],
                  now: Callable[[], float], sleep: Callable[[float], None],
                  interrupted: Callable[[], str | None] | None = None,
                  wait_seconds: float = GATE_WAIT_SECONDS, poll_seconds: float = GATE_POLL_SECONDS) -> GateWait:
    """Re-sweep a gate's footprint until it is clear or the wait is up.

    Sends no input. ``interrupted`` is asked between polls and returns a
    reason to stop waiting (the game left gameplay); a sweep that errors is
    counted and tried again until the wait is up.
    """
    started = now()
    polls = errors = 0
    probe: GateProbe | None = None
    points = gate_probe_points(gate)
    while True:
        try:
            probe = probe_gate(sweep(points), gate)
        except Exception as exc:  # a sweep that cannot answer is not "open"
            errors += 1
            probe = GateProbe(clear=False, verified=False, error=f"{type(exc).__name__}: {exc}")
        polls += 1
        waited = now() - started
        if probe.clear:
            return GateWait("opened", waited, polls, probe, sweep_errors=errors)
        if waited >= wait_seconds:
            # The last probe must support the reported state. An earlier
            # closed reading cannot verify the gate after coverage is lost.
            outcome = "gate_did_not_open" if probe.verified else "gate_state_unverified"
            return GateWait(outcome, waited, polls, probe, sweep_errors=errors)
        reason = interrupted() if interrupted is not None else None
        if reason:
            return GateWait("interrupted", waited, polls, probe, interruption=reason, sweep_errors=errors)
        sleep(poll_seconds)
