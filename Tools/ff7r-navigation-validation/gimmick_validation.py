"""Independent check of a route's traversal-gimmick crossings.

The navigation validator (navigation_validation.py) is pinned by its source
hash in every admitted topology cache, and knows only the capture's own
off-mesh connections.  A route may also cross a traversal gimmick the live
scan recorded (a ladder, a crack, a crawl space).  This module validates such
a route without changing the pinned oracle:

* the route is split at each gimmick crossing into walking legs, each a
  complete RoutePlan of its own that the pinned validator checks unchanged;
* each crossing is checked here against the gimmick evidence file, by this
  module's own reading of it, never by what the planner says the gimmick is.

It deliberately imports nothing from the planner package.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping

EVIDENCE_SCHEMA = "ff7r.traversal_gimmick_evidence.v1"
# The scan's gimmick kind -> the link class a route declares for it.
GIMMICK_CLASSES = {"ladder": "ladder", "crack": "crack", "crouch": "crouch"}
DIRECTIONS = ("first_to_second", "second_to_first")
TRAVERSAL_PORTAL_SIDE = 0x40
INTERNAL_LINK_SIDE = 128
POINT_TOLERANCE_CM = 5.0
# The shipping polygon reference layout.  Every leg's tile crossings are
# re-checked by the pinned validator against the snapshot's own layout, so a
# different layout fails closed.
SHIPPING_TILE_BITS = 14
SHIPPING_POLY_BITS = 31
# A crossing's ends lie on the polygons beside the gimmick's own ends.
END_MAX_HORIZONTAL_CM = 200.0
END_MAX_VERTICAL_CM = 150.0


def link_reference(gimmick_id: str, direction: str) -> int:
    digest = hashlib.sha256(f"{gimmick_id}|{direction}".encode("utf-8")).hexdigest()
    return -(int(digest[:15], 16) + 1)


def _point(value: Any) -> list[float] | None:
    if isinstance(value, dict):
        value = [value.get("x"), value.get("y"), value.get("z")]
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    try:
        point = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    return point if all(math.isfinite(item) for item in point) else None


def _distance(first: list[float], second: list[float]) -> float:
    return math.dist(first, second)


def _polyline_m(points: list[list[float]]) -> float:
    return sum(_distance(left, right) for left, right in zip(points, points[1:])) / 100.0


def _tile_crossings(refs: list[int], tile_bits: int, poly_bits: int) -> int:
    tile_mask = (1 << tile_bits) - 1
    return sum(((left >> poly_bits) & tile_mask) != ((right >> poly_bits) & tile_mask)
               for left, right in zip(refs, refs[1:]))


class GimmickEvidence:
    """The evidence file, read once; ``sha256`` binds what was read."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        data = self.path.read_bytes()
        self.sha256 = hashlib.sha256(data).hexdigest().upper()
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != EVIDENCE_SCHEMA:
            raise ValueError("traversal gimmick evidence has an unknown schema")
        self.gimmicks: dict[str, dict[str, Any]] = {}
        for item in payload.get("gimmicks") or []:
            if not isinstance(item, dict) or item.get("kind") not in GIMMICK_CLASSES:
                continue
            first, second = _point(item.get("first")), _point(item.get("second"))
            if not isinstance(item.get("id"), str) or first is None or second is None:
                continue
            observed = item.get("observed_landings") if isinstance(item.get("observed_landings"), dict) else {}
            self.gimmicks[item["id"]] = {
                "kind": item["kind"], "first": first, "second": second,
                "observed": {key: _point(value) for key, value in observed.items()
                             if key in DIRECTIONS and _point(value) is not None},
            }

    def ends(self, gimmick_id: str, direction: str) -> tuple[list[float], list[float]] | None:
        record = self.gimmicks.get(gimmick_id)
        if record is None or direction not in DIRECTIONS:
            return None
        departure, landing = ((record["first"], record["second"]) if direction == DIRECTIONS[0]
                              else (record["second"], record["first"]))
        return departure, record["observed"].get(direction, landing)


def is_gimmick_segment(segment: Any) -> bool:
    return isinstance(segment, dict) and segment.get("gimmick_id") is not None


def split_route_plan(plan: dict[str, Any], *, tile_bits: int = SHIPPING_TILE_BITS,
                     poly_bits: int = SHIPPING_POLY_BITS) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """(walking legs, gimmick crossings, errors) of a complete RoutePlan.

    Each crossing is found by its own traversal portal in the corridor and
    its two consecutive waypoints; anything that does not line up is an
    error, never repaired.
    """
    errors: list[str] = []
    refs = [item for item in plan.get("polygon_corridor") or [] if isinstance(item, int)]
    portals = list(plan.get("portals") or [])
    waypoints = [_point(item) for item in plan.get("waypoints") or []]
    segments = list(plan.get("traversal_segments") or [])
    gimmicks = [segment for segment in segments if is_gimmick_segment(segment)]
    if not gimmicks:
        return [plan], [], errors
    if len(portals) != len(refs) + 1 or any(point is None for point in waypoints):
        return [], [], ["gimmick route corridor, portals or waypoints are malformed"]
    crossings: list[dict[str, Any]] = []
    corridor_cursor, waypoint_cursor = 0, 0
    for order, segment in enumerate(gimmicks):
        start, end = _point(segment.get("start")), _point(segment.get("end"))
        if start is None or end is None:
            errors.append(f"gimmick crossing {order} has no finite start/end")
            return [], [], errors
        position = next((
            index for index in range(corridor_cursor, len(refs) - 1)
            if isinstance(portals[index + 1], dict) and portals[index + 1].get("kind") == "traversal"
            and portals[index + 1].get("link_ref") == segment.get("link_ref")
        ), None)
        waypoint = next((
            index for index in range(waypoint_cursor, len(waypoints) - 1)
            if _distance(waypoints[index], start) <= POINT_TOLERANCE_CM
            and _distance(waypoints[index + 1], end) <= POINT_TOLERANCE_CM
        ), None)
        if position is None or waypoint is None:
            errors.append(f"gimmick crossing {order} is not a traversal portal between consecutive waypoints")
            return [], [], errors
        crossings.append({"order": order, "segment": segment, "corridor_position": position,
                          "waypoint_index": waypoint, "portal": portals[position + 1],
                          "from_ref": refs[position], "to_ref": refs[position + 1],
                          "departure_waypoint": waypoints[waypoint], "landing_waypoint": waypoints[waypoint + 1]})
        corridor_cursor, waypoint_cursor = position + 1, waypoint + 1
    legs: list[dict[str, Any]] = []
    leg_corridor, leg_waypoint = 0, 0
    bounds = [(item["corridor_position"], item["waypoint_index"]) for item in crossings] + [(len(refs) - 1, len(waypoints) - 1)]
    for index, (corridor_end, waypoint_end) in enumerate(bounds):
        legs.append(_leg(plan, refs, portals, waypoints, segments,
                         leg_corridor, corridor_end, leg_waypoint, waypoint_end,
                         first=index == 0, last=index == len(bounds) - 1,
                         tile_bits=tile_bits, poly_bits=poly_bits))
        leg_corridor, leg_waypoint = corridor_end + 1, waypoint_end + 1
    return legs, crossings, errors


def _sentinel(reference: int, point: list[float]) -> dict[str, Any]:
    value = {"x": point[0], "y": point[1], "z": point[2]}
    return {"from_ref": reference, "to_ref": reference, "link_ref": reference, "source_edge": -1,
            "target_edge": -1, "left": dict(value), "right": dict(value), "side": INTERNAL_LINK_SIDE}


def _leg(plan: dict[str, Any], refs: list[int], portals: list[Any], waypoints: list[list[float]],
         segments: list[Any], corridor_start: int, corridor_end: int, waypoint_start: int,
         waypoint_end: int, *, first: bool, last: bool, tile_bits: int, poly_bits: int) -> dict[str, Any]:
    """One walking leg as a complete RoutePlan for the pinned validator.

    The leg's length and its planner summary describe the leg; the whole
    route's own claims are checked in ``validate_gimmick_route``.
    """
    leg = copy.deepcopy(plan)
    leg_refs = refs[corridor_start:corridor_end + 1]
    leg_points = waypoints[waypoint_start:waypoint_end + 1]
    start_point, end_point = leg_points[0], leg_points[-1]
    leg["polygon_corridor"] = leg_refs
    leg["portals"] = (
        [copy.deepcopy(portals[0]) if first else _sentinel(leg_refs[0], start_point)]
        + copy.deepcopy(portals[corridor_start + 1:corridor_end + 1])
        + [copy.deepcopy(portals[-1]) if last else _sentinel(leg_refs[-1], end_point)]
    )
    leg["waypoints"] = [{"x": point[0], "y": point[1], "z": point[2]} for point in leg_points]
    leg["start_ref"], leg["end_ref"] = leg_refs[0], leg_refs[-1]
    if not first:
        point = {"x": start_point[0], "y": start_point[1], "z": start_point[2]}
        leg["requested_start"], leg["resolved_start"] = dict(point), dict(point)
        leg["start_resolution"] = {"selected_ref": leg_refs[0], "status": "resolved"}
    if not last:
        point = {"x": end_point[0], "y": end_point[1], "z": end_point[2]}
        leg["requested_end"], leg["resolved_end"] = dict(point), dict(point)
        leg["end_resolution"] = {"selected_ref": leg_refs[-1], "status": "resolved"}
    # Capture links crossed inside this leg stay for the pinned validator.
    leg["traversal_segments"] = [
        copy.deepcopy(segment) for segment in segments
        if not is_gimmick_segment(segment) and any(
            _distance(point, _point(segment.get("start")) or [math.inf] * 3) <= POINT_TOLERANCE_CM
            for point in leg_points)
    ]
    if not leg["traversal_segments"]:
        leg.pop("traversal_segments")
    leg["total_estimated_path_length_m"] = _polyline_m(leg_points)
    summary = dict(leg.get("validation") or {})
    summary["tile_crossings"] = _tile_crossings(leg_refs, tile_bits, poly_bits)
    if "waypoint_count" in summary:
        summary["waypoint_count"] = len(leg_points)
    leg["validation"] = summary
    return leg


def validate_crossing(crossing: dict[str, Any], evidence: GimmickEvidence) -> list[str]:
    """Errors in one gimmick crossing, by the evidence file's own record.

    Its two polygons are the end of one walking leg and the start of the
    next, whose existence the pinned validator checks with each leg.
    """
    errors: list[str] = []
    label = f"gimmick crossing {crossing['order']}"
    segment, portal = crossing["segment"], crossing["portal"]
    gimmick_id, direction = segment.get("gimmick_id"), segment.get("gimmick_direction")
    record = evidence.gimmicks.get(gimmick_id) if isinstance(gimmick_id, str) else None
    if record is None:
        return [f"{label} names a gimmick the evidence does not hold: {gimmick_id!r}"]
    if direction not in DIRECTIONS:
        return [f"{label} has an unknown gimmick direction: {direction!r}"]
    link_class = GIMMICK_CLASSES[record["kind"]]
    if segment.get("link_class") != link_class or portal.get("traversal_class") != link_class:
        errors.append(f"{label} class disagrees with the scanned gimmick: {link_class!r}")
    expected_ref = link_reference(gimmick_id, direction)
    if segment.get("link_ref") != expected_ref or portal.get("link_ref") != expected_ref:
        errors.append(f"{label} link_ref is not the gimmick's crossing reference")
    if segment.get("direction") != "start_to_end" or portal.get("direction") != "start_to_end":
        errors.append(f"{label} must be crossed start to end (each way is its own link)")
    departure, landing = evidence.ends(gimmick_id, direction)
    start, end = _point(segment.get("start")), _point(segment.get("end"))
    for name, actual, expected in (("start", start, departure), ("end", end, landing)):
        if actual is None:
            errors.append(f"{label} {name} is not a finite point")
            continue
        if (math.hypot(actual[0] - expected[0], actual[1] - expected[1]) > END_MAX_HORIZONTAL_CM
                or abs(actual[2] - expected[2]) > END_MAX_VERTICAL_CM):
            errors.append(f"{label} {name} is not beside the gimmick's scanned end")
    if start is not None and end is not None:
        left, right = _point(portal.get("left")), _point(portal.get("right"))
        if (left is None or right is None or _distance(left, start) > POINT_TOLERANCE_CM
                or _distance(right, end) > POINT_TOLERANCE_CM
                or _distance(crossing["departure_waypoint"], start) > POINT_TOLERANCE_CM
                or _distance(crossing["landing_waypoint"], end) > POINT_TOLERANCE_CM):
            errors.append(f"{label} portal or waypoints disagree with its points")
    if (portal.get("from_ref"), portal.get("to_ref")) != (crossing["from_ref"], crossing["to_ref"]) or (
            segment.get("from_ref"), segment.get("to_ref")) != (crossing["from_ref"], crossing["to_ref"]):
        errors.append(f"{label} does not join the corridor's polygons")
    if (portal.get("side") != TRAVERSAL_PORTAL_SIDE or portal.get("source_edge") != -1
            or portal.get("target_edge") != -1):
        errors.append(f"{label} portal is not a traversal portal")
    return errors


def validate_gimmick_route(plan: dict[str, Any], validate: Callable[[dict[str, Any]], Mapping[str, Any]],
                           evidence: GimmickEvidence | None) -> dict[str, Any]:
    """``validate`` (the pinned validator) over each walking leg, and the
    crossings here."""
    if not any(is_gimmick_segment(segment) for segment in plan.get("traversal_segments") or []):
        return dict(validate(plan))
    if evidence is None:
        return {"valid": False, "errors": ["route crosses traversal gimmicks but no gimmick evidence is bound"],
                "warnings": []}
    legs, crossings, errors = split_route_plan(plan)
    warnings: list[str] = []
    # The whole route's own claims, which the legs no longer carry.
    refs = [item for item in plan.get("polygon_corridor") or [] if isinstance(item, int)]
    points = [_point(item) for item in plan.get("waypoints") or []]
    reported = plan.get("validation") if isinstance(plan.get("validation"), dict) else {}
    length = plan.get("total_estimated_path_length_m")
    if all(point is not None for point in points) and points:
        expected = _polyline_m(points)
        if not isinstance(length, (int, float)) or abs(float(length) - expected) > max(1e-6, expected * 1e-6):
            errors.append(f"route length mismatch in metres: reported={length!r}, waypoint_polyline_m={expected:.9f}")
    if reported.get("tile_crossings") != _tile_crossings(refs, SHIPPING_TILE_BITS, SHIPPING_POLY_BITS):
        errors.append("route validation.tile_crossings disagrees with its corridor")
    if "waypoint_count" in reported and reported.get("waypoint_count") != len(points):
        errors.append("route validation.waypoint_count disagrees with its waypoints")
    leg_results = []
    for index, leg in enumerate(legs):
        result = validate(leg)
        leg_results.append({"valid": result.get("valid"), "errors": list(result.get("errors") or [])})
        errors += [f"leg {index}: {error}" for error in result.get("errors") or []]
        warnings += [f"leg {index}: {warning}" for warning in result.get("warnings") or []]
    for crossing in crossings:
        errors += validate_crossing(crossing, evidence)
    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "gimmick_route": {"legs": len(legs), "crossings": len(crossings),
                              "evidence_sha256": evidence.sha256, "leg_results": leg_results}}
