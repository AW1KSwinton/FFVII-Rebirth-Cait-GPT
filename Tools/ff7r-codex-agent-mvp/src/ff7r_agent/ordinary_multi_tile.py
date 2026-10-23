"""Explicit validation and deterministic selection for the first multi-tile route.

This module is intentionally separate from ``ordinary_multi_polygon``.  The
same-tile route remains the conservative default; this module admits only a
bounded ordinary corridor with independently proven external tile portals.
It has no live-input or service dependencies.
"""

from __future__ import annotations

import heapq
import math
from collections import deque
from typing import Any, Callable, Mapping

from .detour_route import DT_POLYTYPE_GROUND, DetourRoutePlan, DetourTopologyGraph, PointResolution
from .models import Vec3
from .ordinary_multi_polygon import (
    _distance_m,
    _int_or_none,
    _parse_plan_point,
    _portal_point_matches,
    polygon_edge_clearance_m,
)


ORDINARY_PORTAL_SIDES = frozenset({0, 2, 4, 6, 128})
CROSS_TILE_PORTAL_SIDES = frozenset({0, 2, 4, 6})
DEFAULT_MULTI_TILE_MIN_DISTANCE_M = 30.0
DEFAULT_MULTI_TILE_MAX_DISTANCE_M = 50.0
DEFAULT_MULTI_TILE_MAX_CROSSINGS = 2
DEFAULT_MULTI_TILE_ABSOLUTE_MAX_CYCLES = 360
DEFAULT_MULTI_TILE_MIN_CYCLE_BUDGET = 40


class OrdinaryMultiTileRouteError(ValueError):
    """Raised when an ordinary multi-tile route cannot be proven safe."""


def derive_multi_tile_cycle_budget(
    route_length_m: float,
    *,
    absolute_max_cycles: int = DEFAULT_MULTI_TILE_ABSOLUTE_MAX_CYCLES,
    minimum_cycles: int = DEFAULT_MULTI_TILE_MIN_CYCLE_BUDGET,
) -> int:
    """Return a bounded budget derived from route length.

    The factor of six is deliberately conservative relative to the proven
    short routes: the 4 m route used 18 commands and the 10 m route used 18
    commands, while every command remains subject to the unchanged active
    stall detector.  The absolute cap prevents a long zero-progress episode
    from being extended merely by increasing route length.
    """

    value = float(route_length_m)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("route_length_m must be finite and positive")
    if int(absolute_max_cycles) <= 0 or int(minimum_cycles) <= 0:
        raise ValueError("cycle bounds must be positive")
    derived = int(math.ceil(value * 6.0)) + 12
    return max(int(minimum_cycles), min(int(absolute_max_cycles), derived))


def _tile_identity(polygon: Any) -> dict[str, Any]:
    reference = polygon.reference
    return {
        "tile_index": int(reference.tile_index),
        "tile_x": int(polygon.tile_x),
        "tile_y": int(polygon.tile_y),
        "layer": int(polygon.layer),
        "identity_source": str(polygon.identity_source),
    }


def _valid_tile_identity(polygon: Any) -> bool:
    identity = _tile_identity(polygon)
    return (
        identity["tile_index"] >= 0
        and identity["identity_source"] not in {"", "unresolved", "ambiguous", "missing"}
    )


def _finite_point(value: Any) -> bool:
    try:
        return all(math.isfinite(float(getattr(value, field))) for field in ("x", "y", "z"))
    except (AttributeError, TypeError, ValueError):
        return False


def _same_point(first: Vec3 | None, second: Vec3 | None, tolerance_cm: float = 1e-3) -> bool:
    return (
        first is not None
        and second is not None
        and _distance_m(first, second) is not None
        and _distance_m(first, second) <= tolerance_cm / 100.0
    )


def _portal_matches_adjacency(
    graph: DetourTopologyGraph,
    source_ref: int,
    target_ref: int,
    portal: Mapping[str, Any],
) -> tuple[Any, Any] | None:
    link_ref = _int_or_none(portal.get("link_ref"))
    source_edge = _int_or_none(portal.get("source_edge"))
    target_edge = _int_or_none(portal.get("target_edge"))
    side = _int_or_none(portal.get("side"))
    if None in (link_ref, source_edge, target_edge, side):
        return None
    admitted = next(
        (
            item for item in graph.adjacency.get(source_ref, ())
            if item.to_reference == target_ref
            and item.link_reference == link_ref
            and item.source_edge == source_edge
            and item.target_edge == target_edge
            and item.side == side
        ),
        None,
    )
    if admitted is None:
        return None
    source = graph.polygons.get(source_ref)
    target = graph.polygons.get(target_ref)
    if source is None or target is None or source_edge < 0 or target_edge < 0:
        return None
    if source_edge >= len(source.vertices) or target_edge >= len(target.vertices):
        return None
    matching_link = next(
        (
            link for link in source.links
            if link.reference == target_ref
            and link.edge == source_edge
            and link.side == side
            and not link.is_shipping_dynamic
        ),
        None,
    )
    if matching_link is None:
        return None
    left = _parse_plan_point(portal.get("left"))
    right = _parse_plan_point(portal.get("right"))
    if left is None or right is None or not _finite_point(left) or not _finite_point(right):
        return None
    if not _portal_point_matches(left, admitted.left, graph.portal_tolerance_cm):
        return None
    if not _portal_point_matches(right, admitted.right, graph.portal_tolerance_cm):
        return None
    target_first, target_second = target.edge(target_edge)
    for point in (left, right):
        dx = target_second.x - target_first.x
        dy = target_second.y - target_first.y
        length_sq = dx * dx + dy * dy
        amount = (
            ((point.x - target_first.x) * dx + (point.y - target_first.y) * dy) / length_sq
            if length_sq > 0.0 else 0.0
        )
        amount = max(0.0, min(1.0, amount))
        closest = Vec3(target_first.x + amount * dx, target_first.y + amount * dy, point.z)
        if math.hypot(point.x - closest.x, point.y - closest.y) > graph.portal_tolerance_cm:
            return None
    return admitted, matching_link


def validate_ordinary_multi_tile_route(
    plan: Mapping[str, Any],
    graph: DetourTopologyGraph,
    current_start: Vec3,
    *,
    start_ref: int,
    max_start_snap_m: float = 0.75,
    max_start_vertical_m: float = 1.0,
    endpoint_edge_clearance_m: float = 0.5,
    min_route_distance_m: float = DEFAULT_MULTI_TILE_MIN_DISTANCE_M,
    max_route_distance_m: float = DEFAULT_MULTI_TILE_MAX_DISTANCE_M,
    max_tile_crossings: int = DEFAULT_MULTI_TILE_MAX_CROSSINGS,
    current_resolution_override: PointResolution | None = None,
    endpoint_resolution_override: PointResolution | None = None,
) -> tuple[Vec3, Vec3, dict[str, Any]]:
    """Independently prove a bounded ordinary route with tile crossings."""

    errors: list[str] = []
    if plan.get("status") != "complete" or plan.get("reason_code") != "route_plan_complete":
        errors.append("ordinary_multi_tile_planner_did_not_return_complete_route")
    if plan.get("movement_sent") is not False:
        errors.append("ordinary_multi_tile_planner_route_not_movement_free")
    corridor_raw = plan.get("polygon_corridor")
    corridor: list[int] = []
    if isinstance(corridor_raw, list):
        for value in corridor_raw:
            parsed = _int_or_none(value)
            if parsed is None:
                errors.append("ordinary_multi_tile_corridor_reference_invalid")
            else:
                corridor.append(parsed)
    if len(corridor) < 2:
        errors.append("ordinary_multi_tile_requires_two_polygons")
    if len(corridor) != len(set(corridor)):
        errors.append("ordinary_multi_tile_corridor_repeats_polygon")
    portals = plan.get("portals") if isinstance(plan.get("portals"), list) else []
    if len(portals) != len(corridor) + 1:
        errors.append("ordinary_multi_tile_portal_count_mismatch")
    polygons = [graph.polygons.get(reference) for reference in corridor]
    if any(polygon is None for polygon in polygons):
        errors.append("ordinary_multi_tile_missing_corridor_ref")
    if any(polygon is not None and polygon.poly_type != DT_POLYTYPE_GROUND for polygon in polygons):
        errors.append("ordinary_multi_tile_non_ground_polygon")
    if any(polygon is not None and not _valid_tile_identity(polygon) for polygon in polygons):
        errors.append("ordinary_multi_tile_ambiguous_tile_identity")
    plan_start_ref = _int_or_none(plan.get("start_ref"))
    plan_end_ref = _int_or_none(plan.get("end_ref"))
    if corridor and plan_start_ref != corridor[0]:
        errors.append("ordinary_multi_tile_start_ref_mismatch")
    if corridor and plan_end_ref != corridor[-1]:
        errors.append("ordinary_multi_tile_end_ref_mismatch")
    if corridor and int(start_ref) != corridor[0]:
        errors.append("ordinary_multi_tile_current_start_ref_mismatch")

    resolved_start = _parse_plan_point(plan.get("resolved_start"))
    resolved_end = _parse_plan_point(plan.get("resolved_end"))
    waypoints = plan.get("waypoints")
    if resolved_start is None or resolved_end is None:
        errors.append("ordinary_multi_tile_endpoint_non_finite")
    if not isinstance(waypoints, list) or not waypoints or any(_parse_plan_point(item) is None for item in waypoints):
        errors.append("ordinary_multi_tile_waypoints_non_finite")

    current_resolution = current_resolution_override or graph.resolve_point(
        current_start,
        max_snap_distance_m=max_start_snap_m,
        max_vertical_separation_m=max_start_vertical_m,
    )
    if not current_resolution.ok or current_resolution.selected_reference != int(start_ref):
        errors.append("ordinary_multi_tile_current_start_unresolvable")
    if current_resolution.horizontal_distance_m is None or current_resolution.horizontal_distance_m > max_start_snap_m:
        errors.append("ordinary_multi_tile_start_snap_exceeds_bound")
    if current_resolution.vertical_distance_m is None or current_resolution.vertical_distance_m > max_start_vertical_m:
        errors.append("ordinary_multi_tile_start_vertical_separation_exceeds_bound")

    start_resolution = plan.get("start_resolution")
    end_resolution = plan.get("end_resolution")
    if not isinstance(start_resolution, Mapping) or not isinstance(end_resolution, Mapping):
        errors.append("ordinary_multi_tile_resolution_evidence_missing")
    else:
        if start_resolution.get("reason_code") != "point_inside_polygon":
            errors.append("ordinary_multi_tile_start_not_inside_polygon")
        if end_resolution.get("reason_code") != "point_inside_polygon":
            errors.append("ordinary_multi_tile_endpoint_not_inside_polygon")
        if float(start_resolution.get("horizontal_distance_m", math.inf)) > max_start_snap_m:
            errors.append("ordinary_multi_tile_plan_start_snap_exceeds_bound")
        if float(start_resolution.get("vertical_distance_m", math.inf)) > max_start_vertical_m:
            errors.append("ordinary_multi_tile_plan_start_vertical_exceeds_bound")

    transition_evidence: list[dict[str, Any]] = []
    actual_crossings = 0
    for index, (source_ref, target_ref) in enumerate(zip(corridor, corridor[1:]), start=1):
        source = graph.polygons.get(source_ref)
        target = graph.polygons.get(target_ref)
        portal = portals[index] if index < len(portals) else None
        if source is None or target is None or not isinstance(portal, Mapping):
            errors.append(f"ordinary_multi_tile_transition_missing:{source_ref}->{target_ref}")
            continue
        portal_from_ref = _int_or_none(portal.get("from_ref"))
        portal_to_ref = _int_or_none(portal.get("to_ref"))
        if portal_from_ref != source_ref or portal_to_ref != target_ref:
            errors.append(f"ordinary_multi_tile_transition_portal_corridor_mismatch:{source_ref}->{target_ref}")
        source_tile = source.reference.tile_index
        target_tile = target.reference.tile_index
        crosses_tile = source_tile != target_tile
        if crosses_tile:
            actual_crossings += 1
        side = _int_or_none(portal.get("side"))
        if side not in ORDINARY_PORTAL_SIDES:
            errors.append(f"ordinary_multi_tile_special_transition:{source_ref}->{target_ref}")
        if crosses_tile and side not in CROSS_TILE_PORTAL_SIDES:
            errors.append(f"ordinary_multi_tile_cross_tile_internal_side:{source_ref}->{target_ref}")
        if crosses_tile and source.layer != target.layer:
            errors.append(f"ordinary_multi_tile_layer_transition_rejected:{source_ref}->{target_ref}")
        if _portal_matches_adjacency(graph, source_ref, target_ref, portal) is None:
            errors.append(f"ordinary_multi_tile_unresolved_or_dynamic_portal:{source_ref}->{target_ref}")
        transition_evidence.append({
            "from_ref": source_ref,
            "to_ref": target_ref,
            "from_tile": _tile_identity(source),
            "to_tile": _tile_identity(target),
            "tile_crossing": crosses_tile,
            "portal": dict(portal),
            "portal_validated": _portal_matches_adjacency(graph, source_ref, target_ref, portal) is not None,
        })

    validation = plan.get("validation") if isinstance(plan.get("validation"), Mapping) else {}
    claimed_crossings = validation.get("tile_crossings")
    if not isinstance(claimed_crossings, int) or isinstance(claimed_crossings, bool):
        errors.append("ordinary_multi_tile_tile_crossings_missing")
    elif claimed_crossings != actual_crossings:
        errors.append("ordinary_multi_tile_tile_crossing_evidence_mismatch")
    if actual_crossings < 1:
        errors.append("ordinary_multi_tile_requires_tile_crossing")
    if actual_crossings > int(max_tile_crossings):
        errors.append("ordinary_multi_tile_too_many_tile_crossings")
    tile_ids = [_tile_identity(polygon) for polygon in polygons if polygon is not None]
    if len({item["tile_index"] for item in tile_ids}) < 2:
        errors.append("ordinary_multi_tile_requires_two_tiles")

    route_length = plan.get("total_estimated_path_length_m")
    if not isinstance(route_length, (int, float)) or isinstance(route_length, bool) or not math.isfinite(float(route_length)):
        errors.append("ordinary_multi_tile_route_length_invalid")
        route_length_value = None
    else:
        route_length_value = float(route_length)
        if not min_route_distance_m <= route_length_value <= max_route_distance_m:
            errors.append("ordinary_multi_tile_route_length_outside_bound")

    endpoint_clearance = None
    if resolved_end is not None and corridor and corridor[-1] in graph.polygons:
        endpoint_resolution = endpoint_resolution_override or graph.resolve_point(
            resolved_end,
            max_snap_distance_m=max_start_snap_m,
            max_vertical_separation_m=max_start_vertical_m,
        )
        if not endpoint_resolution.ok or endpoint_resolution.selected_reference != corridor[-1]:
            errors.append("ordinary_multi_tile_endpoint_ref_unresolvable")
        endpoint_clearance = polygon_edge_clearance_m(resolved_end, graph.polygons[corridor[-1]])
        if endpoint_clearance < endpoint_edge_clearance_m:
            errors.append("ordinary_multi_tile_endpoint_edge_clearance_below_bound")

    if errors:
        raise OrdinaryMultiTileRouteError(";".join(dict.fromkeys(errors)))
    evidence = {
        "route_mode": "ordinary_multi_tile",
        "corridor_refs": corridor,
        "polygon_count": len(corridor),
        "tile_identities": tile_ids,
        "tile_crossings": actual_crossings,
        "portal_count": len(portals),
        "transition_portals": transition_evidence,
        "geodesic_length_m": route_length_value,
        "endpoint_edge_clearance_m": endpoint_clearance,
        "current_start_resolution": current_resolution.to_dict(),
        "max_tile_crossings": int(max_tile_crossings),
    }
    return resolved_start, resolved_end, evidence


def _reachable_centroid_costs(graph: DetourTopologyGraph, start_ref: int, max_cost_m: float) -> dict[int, float]:
    costs: dict[int, float] = {int(start_ref): 0.0}
    frontier: list[tuple[float, int]] = [(0.0, int(start_ref))]
    while frontier:
        cost, reference = heapq.heappop(frontier)
        if cost > costs.get(reference, math.inf) + 1e-9 or cost > max_cost_m:
            continue
        source = graph.polygons.get(reference)
        if source is None:
            continue
        for portal in graph.adjacency.get(reference, ()):
            target = graph.polygons.get(portal.to_reference)
            if target is None or target.poly_type != DT_POLYTYPE_GROUND:
                continue
            if any(link.reference == target.reference.value and link.is_shipping_dynamic for link in source.links):
                continue
            new_cost = cost + _distance_m(source.centroid, target.centroid)
            if new_cost < costs.get(target.reference.value, math.inf) - 1e-9:
                costs[target.reference.value] = new_cost
                heapq.heappush(frontier, (new_cost, target.reference.value))
    return costs


def _reachable_tile_ids(graph: DetourTopologyGraph, start_ref: int, max_crossings: int) -> set[int]:
    """Return tile identities reachable with at most ``max_crossings`` hops."""

    start_tile = graph.polygons[start_ref].reference.tile_index
    queue = deque([(int(start_ref), 0)])
    seen: set[tuple[int, int]] = set(queue)
    tiles = {start_tile}
    while queue:
        reference, crossings = queue.popleft()
        source = graph.polygons.get(reference)
        if source is None:
            continue
        for portal in graph.adjacency.get(reference, ()):
            target = graph.polygons.get(portal.to_reference)
            if target is None:
                continue
            next_crossings = crossings + int(
                source.reference.tile_index != target.reference.tile_index
            )
            if next_crossings > max_crossings:
                continue
            state = (target.reference.value, next_crossings)
            tiles.add(target.reference.tile_index)
            if state not in seen:
                seen.add(state)
                queue.append(state)
    return tiles


def select_ordinary_multi_tile_destination(
    graph: DetourTopologyGraph,
    start: Vec3,
    start_ref: int,
    *,
    max_start_snap_m: float = 0.75,
    max_start_vertical_m: float = 1.0,
    endpoint_edge_clearance_m: float = 0.5,
    min_route_distance_m: float = DEFAULT_MULTI_TILE_MIN_DISTANCE_M,
    max_route_distance_m: float = DEFAULT_MULTI_TILE_MAX_DISTANCE_M,
    max_tile_crossings: int = DEFAULT_MULTI_TILE_MAX_CROSSINGS,
    _planning_crossing_limit: int | None = None,
    planner: Callable[..., DetourRoutePlan] | None = None,
) -> tuple[Vec3, Any, DetourRoutePlan, list[dict[str, Any]]]:
    """Select a deterministic simple 30–50 m ordinary multi-tile route."""

    start_polygon = graph.polygons.get(int(start_ref))
    if start_polygon is None or start_polygon.poly_type != DT_POLYTYPE_GROUND or not _valid_tile_identity(start_polygon):
        raise OrdinaryMultiTileRouteError("ordinary_multi_tile_start_not_valid_ground")
    start_resolution = graph.resolve_point(
        start,
        max_snap_distance_m=max_start_snap_m,
        max_vertical_separation_m=max_start_vertical_m,
    )
    if not start_resolution.ok or start_resolution.selected_reference != int(start_ref):
        raise OrdinaryMultiTileRouteError("ordinary_multi_tile_current_start_unresolvable")
    reachable = _reachable_centroid_costs(graph, int(start_ref), max_route_distance_m * 1.6)
    planning_crossing_limit = int(_planning_crossing_limit or min(1, max_tile_crossings))
    allowed_tiles = _reachable_tile_ids(graph, int(start_ref), planning_crossing_limit)
    candidates: list[tuple[tuple[Any, ...], Vec3, Any, DetourRoutePlan, dict[str, Any]]] = []
    considered: list[dict[str, Any]] = []
    shortlist: list[tuple[float, float, int]] = []
    for reference in sorted(reachable):
        if reference == int(start_ref):
            continue
        polygon = graph.polygons.get(reference)
        if polygon is None or polygon.poly_type != DT_POLYTYPE_GROUND or not _valid_tile_identity(polygon):
            continue
        if polygon.reference.tile_index == start_polygon.reference.tile_index:
            continue
        if polygon.reference.tile_index not in allowed_tiles:
            continue
        endpoint = polygon.centroid
        straight = _distance_m(start, endpoint) or math.inf
        rough = reachable[reference]
        # A route cannot be shorter than its chord.  This inexpensive filter
        # keeps the expensive full graph planner (which revalidates the live
        # start and endpoint) bounded on the 30k-polygon cache.
        if straight > max_route_distance_m + 5.0 or rough > max_route_distance_m * 1.6:
            continue
        shortlist.append((abs(rough - 40.0), abs(straight - 40.0), reference))
    shortlist.sort()
    for _, _, reference in shortlist[:96]:
        polygon = graph.polygons[reference]
        endpoint = polygon.centroid
        base = {
            "start_ref": int(start_ref),
            "end_ref": int(reference),
            "planning_crossing_limit": planning_crossing_limit,
            "tile_identities": [_tile_identity(start_polygon), _tile_identity(polygon)],
            "candidate_endpoint": {"x": endpoint.x, "y": endpoint.y, "z": endpoint.z},
        }
        rank, closest, horizontal_cm, vertical_cm, contained = graph._candidate_resolution(endpoint, polygon)
        if not contained or vertical_cm > max_start_vertical_m * 100.0:
            considered.append({**base, "rejection": "candidate_endpoint_not_inside_polygon", "independent_validator": "not_run"})
            continue
        resolution = PointResolution(
            "resolved", "point_inside_polygon", endpoint, reference, closest,
            horizontal_cm / 100.0, vertical_cm / 100.0, 1,
            {"contained_candidate": True},
        )
        clearance = polygon_edge_clearance_m(closest, polygon)
        if clearance < endpoint_edge_clearance_m:
            considered.append({**base, "rejection": "candidate_endpoint_edge_clearance_below_bound", "endpoint_edge_clearance_m": clearance, "independent_validator": "not_run"})
            continue
        route_planner = planner or graph.plan
        plan = route_planner(
            start,
            endpoint,
            max_snap_distance_m=max_start_snap_m,
            max_vertical_separation_m=max_start_vertical_m,
            start_resolution_override=start_resolution,
            end_resolution_override=resolution,
            max_tile_crossings=planning_crossing_limit,
        )
        try:
            _, _, evidence = validate_ordinary_multi_tile_route(
                plan.to_dict(), graph, start, start_ref=start_ref,
                max_start_snap_m=max_start_snap_m,
                max_start_vertical_m=max_start_vertical_m,
                endpoint_edge_clearance_m=endpoint_edge_clearance_m,
                min_route_distance_m=min_route_distance_m,
                max_route_distance_m=max_route_distance_m,
                max_tile_crossings=planning_crossing_limit,
                current_resolution_override=start_resolution,
                endpoint_resolution_override=resolution,
            )
        except OrdinaryMultiTileRouteError as exc:
            considered.append({
                **base,
                "corridor_refs": list(plan.polygon_corridor),
                "geodesic_length_m": plan.total_estimated_path_length_m,
                "endpoint_edge_clearance_m": clearance,
                "independent_validator": "not_run",
                "rejection": str(exc),
            })
            continue
        length = float(plan.total_estimated_path_length_m or 0.0)
        crossings = int(evidence["tile_crossings"])
        key = (crossings, len(plan.polygon_corridor), abs(length - 40.0), length, int(reference))
        candidates.append((key, endpoint, resolution, plan, evidence))
        considered.append({
            **base,
            "corridor_refs": list(plan.polygon_corridor),
            "polygon_count": len(plan.polygon_corridor),
            "tile_crossings": crossings,
            "portal_count": len(plan.portals),
            "geodesic_length_m": length,
            "straight_line_distance_m": _distance_m(plan.resolved_start, plan.resolved_end),
            "endpoint_edge_clearance_m": clearance,
            "independent_validator": "deferred_to_bound_artifact",
            "selection_key": list(key),
            "rejection": None,
        })
    if not candidates:
        if planning_crossing_limit < int(max_tile_crossings):
            endpoint, resolution, plan, relaxed_considered = select_ordinary_multi_tile_destination(
                graph,
                start,
                start_ref,
                max_start_snap_m=max_start_snap_m,
                max_start_vertical_m=max_start_vertical_m,
                endpoint_edge_clearance_m=endpoint_edge_clearance_m,
                min_route_distance_m=min_route_distance_m,
                max_route_distance_m=max_route_distance_m,
                max_tile_crossings=max_tile_crossings,
                _planning_crossing_limit=int(max_tile_crossings),
            )
            relaxed_considered.extend(considered)
            return endpoint, resolution, plan, relaxed_considered
        raise OrdinaryMultiTileRouteError("no_defensible_ordinary_multi_tile_destination")
    candidates.sort(key=lambda item: item[0])
    _, endpoint, resolution, plan, _ = candidates[0]
    for item in considered:
        item["selected"] = item.get("end_ref") == plan.end_ref and item.get("rejection") is None
        if item["selected"]:
            item["selection_reason"] = "fewest_tile_crossings_then_fewest_polygons_then_closest_to_40m_then_shortest_then_ref"
    return endpoint, resolution, plan, considered
