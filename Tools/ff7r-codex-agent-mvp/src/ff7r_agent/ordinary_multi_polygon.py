"""Explicit, conservative validation for the first ordinary multi-polygon route.

This module is deliberately independent of live service state, input, and the
standalone JSON validator.  It only selects and validates a route against the
already loaded immutable Detour graph.  Movement authorization remains in the
live adapter and still requires the independent validator afterwards.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Mapping

from .detour_route import DT_POLYTYPE_GROUND, DetourRoutePlan, DetourTopologyGraph
from .models import Vec3


ALLOWED_ORDINARY_PORTAL_SIDES = frozenset({0, 2, 4, 6, 128})


class OrdinaryMultiPolygonRouteError(ValueError):
    """Raised when an ordinary multi-polygon route cannot be proven safe."""


def _distance_m(first: Vec3 | None, second: Vec3 | None) -> float | None:
    if first is None or second is None:
        return None
    return math.sqrt(
        (second.x - first.x) ** 2
        + (second.y - first.y) ** 2
        + (second.z - first.z) ** 2
    ) / 100.0


def polygon_edge_clearance_m(point: Vec3, polygon: Any) -> float:
    """Return the minimum horizontal distance to a polygon edge."""

    distances: list[float] = []
    vertices = tuple(getattr(polygon, "vertices", ()))
    if not vertices:
        return 0.0
    for index, first in enumerate(vertices):
        second = vertices[(index + 1) % len(vertices)]
        dx = second.x - first.x
        dy = second.y - first.y
        denominator = dx * dx + dy * dy
        if denominator <= 1e-9:
            amount = 0.0
        else:
            amount = (
                (point.x - first.x) * dx + (point.y - first.y) * dy
            ) / denominator
            amount = max(0.0, min(1.0, amount))
        closest_x = first.x + amount * dx
        closest_y = first.y + amount * dy
        distances.append(math.hypot(point.x - closest_x, point.y - closest_y) / 100.0)
    return min(distances)


def _finite_point(value: Any) -> bool:
    try:
        return all(math.isfinite(float(getattr(value, field))) for field in ("x", "y", "z"))
    except (AttributeError, TypeError, ValueError):
        return False


def _int_or_none(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _portal_point_matches(first: Vec3, second: Vec3, tolerance_cm: float) -> bool:
    return _distance_m(first, second) is not None and _distance_m(first, second) <= tolerance_cm / 100.0


def validate_ordinary_multi_polygon_route(
    plan: Mapping[str, Any],
    graph: DetourTopologyGraph,
    current_start: Vec3,
    *,
    start_ref: int,
    max_start_snap_m: float = 0.75,
    max_start_vertical_m: float = 1.0,
    endpoint_edge_clearance_m: float = 0.5,
    min_route_distance_m: float = 10.0,
    max_route_distance_m: float = 15.0,
    require_zero_tile_crossings: bool = True,
) -> tuple[Vec3, Vec3, dict[str, Any]]:
    """Prove a route uses only ordinary, resolved, same-tile graph adjacency."""

    errors: list[str] = []
    if plan.get("status") != "complete" or plan.get("reason_code") != "route_plan_complete":
        errors.append("planner_did_not_return_complete_route")
    if plan.get("movement_sent") is not False:
        errors.append("planner_route_not_movement_free")
    corridor = plan.get("polygon_corridor")
    portals = plan.get("portals")
    if not isinstance(corridor, list) or len(corridor) < 2:
        errors.append("ordinary_multi_polygon_requires_two_polygons")
        corridor = corridor if isinstance(corridor, list) else []
    if not isinstance(portals, list) or len(portals) != len(corridor) + 1:
        errors.append("ordinary_multi_polygon_portal_count_mismatch")
        portals = portals if isinstance(portals, list) else []
    refs: list[int] = []
    for value in corridor:
        try:
            ref = int(value)
        except (TypeError, ValueError):
            errors.append("ordinary_multi_polygon_invalid_corridor_ref")
            continue
        refs.append(ref)
        polygon = graph.polygons.get(ref)
        if polygon is None:
            errors.append(f"ordinary_multi_polygon_missing_corridor_ref:{ref}")
        elif polygon.poly_type != DT_POLYTYPE_GROUND:
            errors.append(f"ordinary_multi_polygon_non_ground_polygon:{ref}")

    resolved_start = _parse_plan_point(plan.get("resolved_start"))
    resolved_end = _parse_plan_point(plan.get("resolved_end"))
    if resolved_start is None or resolved_end is None:
        errors.append("ordinary_multi_polygon_endpoint_non_finite")
    waypoints = plan.get("waypoints")
    if not isinstance(waypoints, list) or not waypoints or any(
        _parse_plan_point(value) is None for value in waypoints
    ):
        errors.append("ordinary_multi_polygon_waypoints_non_finite")

    if refs and _int_or_none(plan.get("start_ref")) != refs[0]:
        errors.append("ordinary_multi_polygon_start_ref_mismatch")
    if refs and _int_or_none(plan.get("end_ref")) != refs[-1]:
        errors.append("ordinary_multi_polygon_end_ref_mismatch")
    if refs and _int_or_none(start_ref) != refs[0]:
        errors.append("ordinary_multi_polygon_current_start_ref_mismatch")

    current_resolution = graph.resolve_point(
        current_start,
        max_snap_distance_m=max_start_snap_m,
        max_vertical_separation_m=max_start_vertical_m,
    )
    if not current_resolution.ok or current_resolution.selected_reference != start_ref:
        errors.append("ordinary_multi_polygon_current_start_unresolvable")
    elif (
        current_resolution.horizontal_distance_m is None
        or current_resolution.horizontal_distance_m > max_start_snap_m
    ):
        errors.append("ordinary_multi_polygon_start_snap_exceeds_bound")
    elif (
        current_resolution.vertical_distance_m is None
        or current_resolution.vertical_distance_m > max_start_vertical_m
    ):
        errors.append("ordinary_multi_polygon_start_vertical_separation_exceeds_bound")

    start_resolution = plan.get("start_resolution")
    end_resolution = plan.get("end_resolution")
    if not isinstance(start_resolution, Mapping) or not isinstance(end_resolution, Mapping):
        errors.append("ordinary_multi_polygon_resolution_evidence_missing")
    else:
        if start_resolution.get("reason_code") != "point_inside_polygon":
            errors.append("ordinary_multi_polygon_start_not_inside_polygon")
        if end_resolution.get("reason_code") != "point_inside_polygon":
            errors.append("ordinary_multi_polygon_endpoint_not_inside_polygon")
        if float(start_resolution.get("horizontal_distance_m", math.inf)) > max_start_snap_m:
            errors.append("ordinary_multi_polygon_plan_start_snap_exceeds_bound")
        if float(start_resolution.get("vertical_distance_m", math.inf)) > max_start_vertical_m:
            errors.append("ordinary_multi_polygon_plan_start_vertical_exceeds_bound")

    transition_evidence: list[dict[str, Any]] = []
    for index, (source_ref, target_ref) in enumerate(zip(refs, refs[1:]), start=1):
        portal = portals[index] if index < len(portals) else None
        if not isinstance(portal, Mapping):
            errors.append(f"ordinary_multi_polygon_transition_portal_missing:{source_ref}->{target_ref}")
            continue
        portal_from_ref = _int_or_none(portal.get("from_ref"))
        portal_to_ref = _int_or_none(portal.get("to_ref"))
        link_ref = _int_or_none(portal.get("link_ref"))
        source_edge = _int_or_none(portal.get("source_edge"))
        target_edge = _int_or_none(portal.get("target_edge"))
        side = _int_or_none(portal.get("side"))
        if portal_from_ref != source_ref or portal_to_ref != target_ref:
            errors.append(f"ordinary_multi_polygon_transition_portal_corridor_mismatch:{source_ref}->{target_ref}")
        if side not in ALLOWED_ORDINARY_PORTAL_SIDES:
            errors.append(f"ordinary_multi_polygon_special_transition:{source_ref}->{target_ref}")
        matches = [
            candidate for candidate in graph.adjacency.get(source_ref, ())
            if candidate.to_reference == target_ref
            and candidate.link_reference == link_ref
            and candidate.source_edge == source_edge
            and candidate.target_edge == target_edge
            and candidate.side == side
        ]
        if not matches:
            errors.append(f"ordinary_multi_polygon_unresolved_portal:{source_ref}->{target_ref}")
        else:
            admitted_portal = matches[0]
            left = _parse_plan_point(portal.get("left"))
            right = _parse_plan_point(portal.get("right"))
            if (
                left is None
                or right is None
                or not _portal_point_matches(left, admitted_portal.left, graph.portal_tolerance_cm)
                or not _portal_point_matches(right, admitted_portal.right, graph.portal_tolerance_cm)
            ):
                errors.append(f"ordinary_multi_polygon_portal_geometry_mismatch:{source_ref}->{target_ref}")
            transition_evidence.append({
                "from_ref": source_ref,
                "to_ref": target_ref,
                "link_ref": link_ref,
                "source_edge": source_edge,
                "target_edge": target_edge,
                "side": side,
                "resolved": True,
            })
        left = _parse_plan_point(portal.get("left"))
        right = _parse_plan_point(portal.get("right"))
        if left is None or right is None:
            errors.append(f"ordinary_multi_polygon_portal_geometry_non_finite:{source_ref}->{target_ref}")

    validation = plan.get("validation")
    if not isinstance(validation, Mapping):
        errors.append("ordinary_multi_polygon_validation_evidence_missing")
        validation = {}
    if validation.get("transition_portals_valid") is not True or validation.get("portal_links_proven") is not True:
        errors.append("ordinary_multi_polygon_planner_portal_validation_failed")
    tile_crossings = validation.get("tile_crossings")
    if not isinstance(tile_crossings, int) or isinstance(tile_crossings, bool):
        errors.append("ordinary_multi_polygon_tile_crossings_missing")
    elif require_zero_tile_crossings and tile_crossings != 0:
        errors.append("ordinary_multi_polygon_tile_crossing_rejected")
    actual_tile_crossings = sum(
        1
        for first, second in zip(refs, refs[1:])
        if first in graph.polygons
        and second in graph.polygons
        and graph.polygons[first].reference.tile_index != graph.polygons[second].reference.tile_index
    )
    if isinstance(tile_crossings, int) and tile_crossings != actual_tile_crossings:
        errors.append("ordinary_multi_polygon_tile_crossing_evidence_mismatch")

    route_length = plan.get("total_estimated_path_length_m")
    if not isinstance(route_length, (int, float)) or isinstance(route_length, bool) or not math.isfinite(float(route_length)):
        errors.append("ordinary_multi_polygon_route_length_invalid")
        route_length_value = None
    else:
        route_length_value = float(route_length)
        if not min_route_distance_m <= route_length_value <= max_route_distance_m:
            errors.append("ordinary_multi_polygon_route_length_outside_bound")

    endpoint_clearance = None
    if resolved_end is not None and refs and refs[-1] in graph.polygons:
        endpoint_resolution = graph.resolve_point(
            resolved_end,
            max_snap_distance_m=max_start_snap_m,
            max_vertical_separation_m=max_start_vertical_m,
        )
        if not endpoint_resolution.ok or endpoint_resolution.selected_reference != refs[-1]:
            errors.append("ordinary_multi_polygon_endpoint_ref_unresolvable")
        endpoint_clearance = polygon_edge_clearance_m(resolved_end, graph.polygons[refs[-1]])
        if endpoint_clearance < endpoint_edge_clearance_m:
            errors.append("ordinary_multi_polygon_endpoint_edge_clearance_below_bound")

    if errors:
        raise OrdinaryMultiPolygonRouteError(";".join(dict.fromkeys(errors)))
    evidence = {
        "route_mode": "ordinary_multi_polygon",
        "corridor_refs": refs,
        "polygon_count": len(refs),
        "tile_identities": sorted({graph.polygons[ref].reference.tile_index for ref in refs}),
        "tile_crossings": actual_tile_crossings,
        "portal_count": len(portals),
        "transition_portals": transition_evidence,
        "geodesic_length_m": route_length_value,
        "endpoint_edge_clearance_m": endpoint_clearance,
        "current_start_resolution": current_resolution.to_dict(),
    }
    return resolved_start, resolved_end, evidence


def select_ordinary_multi_polygon_destination(
    graph: DetourTopologyGraph,
    start: Vec3,
    start_ref: int,
    *,
    max_start_snap_m: float = 0.75,
    max_start_vertical_m: float = 1.0,
    endpoint_edge_clearance_m: float = 0.5,
    min_route_distance_m: float = 10.0,
    max_route_distance_m: float = 15.0,
    require_zero_tile_crossings: bool = True,
    planner: Callable[..., DetourRoutePlan] | None = None,
) -> tuple[Vec3, Any, DetourRoutePlan, list[dict[str, Any]]]:
    """Select the simplest deterministic same-tile ordinary candidate.

    Polygon centroids are used as the first-proof endpoint candidates.  They
    are accepted only when the graph's natural resolver reports the point as
    inside the intended polygon and edge clearance meets the existing bound.
    """

    start_polygon = graph.polygons.get(int(start_ref))
    if start_polygon is None or start_polygon.poly_type != DT_POLYTYPE_GROUND:
        raise OrdinaryMultiPolygonRouteError("ordinary_multi_polygon_start_not_ground")
    candidates: list[tuple[tuple[Any, ...], Vec3, Any, DetourRoutePlan, dict[str, Any]]] = []
    considered: list[dict[str, Any]] = []
    start_tile = start_polygon.reference.tile_index
    for reference in sorted(graph.polygons):
        if reference == int(start_ref):
            continue
        polygon = graph.polygons[reference]
        if polygon.poly_type != DT_POLYTYPE_GROUND or polygon.reference.tile_index != start_tile:
            continue
        endpoint = polygon.centroid
        resolution = graph.resolve_point(
            endpoint,
            max_snap_distance_m=max_start_snap_m,
            max_vertical_separation_m=max_start_vertical_m,
        )
        base = {
            "start_ref": int(start_ref),
            "end_ref": int(reference),
            "tile_identities": [start_tile],
            "candidate_endpoint": {"x": endpoint.x, "y": endpoint.y, "z": endpoint.z},
        }
        if not resolution.ok or resolution.selected_reference != reference or resolution.reason_code != "point_inside_polygon":
            considered.append({**base, "independent_validator": "not_run", "rejection": "candidate_endpoint_not_inside_polygon"})
            continue
        clearance = polygon_edge_clearance_m(resolution.closest_point or endpoint, polygon)
        if clearance < endpoint_edge_clearance_m:
            considered.append({**base, "independent_validator": "not_run", "rejection": "candidate_endpoint_edge_clearance_below_bound", "endpoint_edge_clearance_m": clearance})
            continue
        route_planner = planner or graph.plan
        plan = route_planner(
            start,
            endpoint,
            max_snap_distance_m=max_start_snap_m,
            max_vertical_separation_m=max_start_vertical_m,
        )
        try:
            _, _, evidence = validate_ordinary_multi_polygon_route(
                plan.to_dict(),
                graph,
                start,
                start_ref=start_ref,
                max_start_snap_m=max_start_snap_m,
                max_start_vertical_m=max_start_vertical_m,
                endpoint_edge_clearance_m=endpoint_edge_clearance_m,
                min_route_distance_m=min_route_distance_m,
                max_route_distance_m=max_route_distance_m,
                require_zero_tile_crossings=require_zero_tile_crossings,
            )
        except OrdinaryMultiPolygonRouteError as exc:
            considered.append({
                **base,
                "corridor_refs": list(plan.polygon_corridor),
                "geodesic_length_m": plan.total_estimated_path_length_m,
                "endpoint_edge_clearance_m": clearance,
                "independent_validator": "not_run",
                "rejection": str(exc),
            })
            continue
        route_length = float(plan.total_estimated_path_length_m or 0.0)
        selection_key = (
            len(plan.polygon_corridor),
            abs(route_length - ((min_route_distance_m + max_route_distance_m) / 2.0)),
            route_length,
            int(reference),
        )
        candidates.append((selection_key, endpoint, resolution, plan, evidence))
        considered.append({
            **base,
            "corridor_refs": list(plan.polygon_corridor),
            "polygon_count": len(plan.polygon_corridor),
            "tile_crossings": plan.validation.get("tile_crossings"),
            "portal_count": len(plan.portals),
            "geodesic_length_m": plan.total_estimated_path_length_m,
            "straight_line_distance_m": _distance_m(plan.resolved_start, plan.resolved_end),
            "endpoint_edge_clearance_m": clearance,
            "ordinary_ground": True,
            "independent_validator": "deferred_to_bound_artifact",
            "selection_key": list(selection_key),
            "rejection": None,
        })
    if not candidates:
        raise OrdinaryMultiPolygonRouteError("no_defensible_same_tile_ordinary_multi_polygon_destination")
    candidates.sort(key=lambda item: item[0])
    _, endpoint, resolution, plan, _ = candidates[0]
    for item in considered:
        if item.get("end_ref") == plan.end_ref and item.get("rejection") is None:
            item["selected"] = True
            item["selection_reason"] = "fewest_polygon_transitions_then_closest_to_window_midpoint_then_reference"
        else:
            item["selected"] = False
    return endpoint, resolution, plan, considered


def _parse_plan_point(value: Any) -> Vec3 | None:
    if not isinstance(value, Mapping):
        return None
    try:
        point = Vec3(float(value["x"]), float(value["y"]), float(value["z"]))
    except (KeyError, TypeError, ValueError):
        return None
    return point if _finite_point(point) else None
