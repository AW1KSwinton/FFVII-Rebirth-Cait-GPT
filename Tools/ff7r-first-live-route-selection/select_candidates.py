"""Select conservative, offline first-live Cloud route candidates.

This tool is intentionally a diagnostic producer.  It imports the accepted
shipping Detour planner as a read-only executable dependency, reads only the
preserved topology snapshot, and writes RoutePlan-shaped JSON artifacts.  It
does not import MCP/controller code, send input, launch the game, or touch the
production navigation-path output.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import heapq
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
PLANNER_SOURCE = REPO_ROOT / "Tools" / "ff7r-codex-agent-mvp" / "src"
if str(PLANNER_SOURCE) not in sys.path:
    sys.path.insert(0, str(PLANNER_SOURCE))

from ff7r_agent.detour_route import (  # noqa: E402
    DT_POLYTYPE_GROUND,
    DetourPolygon,
    DetourPortal,
    DetourTopologyGraph,
    SHIPPING_CONNECTION_INTERNAL_SIDE,
    SHIPPING_EXTERNAL_SIDES,
    load_shipping_detour_topology,
)
from ff7r_agent.models import Vec3  # noqa: E402


RECORDED_CLOUD = Vec3(-33674.804688, -210426.75, 4001.023682)
ORDINARY_SIDES = SHIPPING_EXTERNAL_SIDES | {SHIPPING_CONNECTION_INTERNAL_SIDE}
INVALID_EDGE = 0xFF


class Footprint:
    """Geometry-only footprint for raw unresolved off-mesh records."""

    def __init__(self, vertices: tuple[Vec3, ...], kind: str) -> None:
        self.vertices = vertices
        self.kind = kind


def point_dict(point: Vec3 | None) -> dict[str, float] | None:
    if point is None:
        return None
    return {"x": point.x, "y": point.y, "z": point.z}


def distance(first: Vec3, second: Vec3) -> float:
    return math.sqrt(
        (second.x - first.x) ** 2
        + (second.y - first.y) ** 2
        + (second.z - first.z) ** 2
    ) / 100.0


def horizontal_distance(first: Vec3, second: Vec3) -> float:
    return math.hypot(second.x - first.x, second.y - first.y) / 100.0


def finite_point(point: Vec3) -> bool:
    return all(math.isfinite(value) for value in (point.x, point.y, point.z))


def point_segment_distance_xy(point: Vec3, first: Vec3, second: Vec3) -> float:
    dx, dy = second.x - first.x, second.y - first.y
    length_sq = dx * dx + dy * dy
    if length_sq <= 0.0:
        return math.hypot(point.x - first.x, point.y - first.y)
    amount = ((point.x - first.x) * dx + (point.y - first.y) * dy) / length_sq
    amount = min(1.0, max(0.0, amount))
    return math.hypot(
        point.x - (first.x + amount * dx),
        point.y - (first.y + amount * dy),
    )


def point_in_polygon_xy(point: Vec3, polygon: DetourPolygon) -> bool:
    inside = False
    vertices = polygon.vertices
    for index, first in enumerate(vertices):
        second = vertices[(index + 1) % len(vertices)]
        if ((first.y > point.y) != (second.y > point.y)):
            crossing = (second.x - first.x) * (point.y - first.y) / (
                second.y - first.y
            ) + first.x
            if point.x < crossing:
                inside = not inside
    return inside


def orientation(first: Vec3, second: Vec3, third: Vec3) -> float:
    return (second.x - first.x) * (third.y - first.y) - (
        second.y - first.y
    ) * (third.x - first.x)


def segments_intersect_xy(
    first: Vec3, second: Vec3, third: Vec3, fourth: Vec3
) -> bool:
    eps = 1e-6
    a = orientation(first, second, third)
    b = orientation(first, second, fourth)
    c = orientation(third, fourth, first)
    d = orientation(third, fourth, second)
    if ((a > eps and b < -eps) or (a < -eps and b > eps)) and (
        (c > eps and d < -eps) or (c < -eps and d > eps)
    ):
        return True

    def on_segment(one: Vec3, two: Vec3, candidate: Vec3) -> bool:
        return (
            abs(orientation(one, two, candidate)) <= eps
            and min(one.x, two.x) - eps <= candidate.x <= max(one.x, two.x) + eps
            and min(one.y, two.y) - eps <= candidate.y <= max(one.y, two.y) + eps
        )

    return (
        on_segment(first, second, third)
        or on_segment(first, second, fourth)
        or on_segment(third, fourth, first)
        or on_segment(third, fourth, second)
    )


def segment_segment_distance_xy(
    first: Vec3, second: Vec3, third: Vec3, fourth: Vec3
) -> float:
    if segments_intersect_xy(first, second, third, fourth):
        return 0.0
    return min(
        point_segment_distance_xy(first, third, fourth),
        point_segment_distance_xy(second, third, fourth),
        point_segment_distance_xy(third, first, second),
        point_segment_distance_xy(fourth, first, second),
    )


def segment_polygon_distance_xy(
    first: Vec3, second: Vec3, polygon: DetourPolygon
) -> float:
    if point_in_polygon_xy(first, polygon) or point_in_polygon_xy(second, polygon):
        return 0.0
    best = float("inf")
    for index, edge_first in enumerate(polygon.vertices):
        edge_second = polygon.vertices[(index + 1) % len(polygon.vertices)]
        best = min(
            best,
            segment_segment_distance_xy(first, second, edge_first, edge_second),
        )
    return best


def route_offmesh_distance(
    waypoints: tuple[Vec3, ...], offmesh: tuple[Footprint, ...]
) -> float | None:
    if not offmesh:
        return None
    best = float("inf")
    for first, second in zip(waypoints, waypoints[1:]):
        for polygon in offmesh:
            best = min(best, segment_polygon_distance_xy(first, second, polygon))
    if len(waypoints) == 1:
        for polygon in offmesh:
            if point_in_polygon_xy(waypoints[0], polygon):
                return 0.0
            best = min(
                best,
                min(
                    point_segment_distance_xy(
                        waypoints[0], edge_first, polygon.vertices[(index + 1) % len(polygon.vertices)]
                    )
                    for index, edge_first in enumerate(polygon.vertices)
                ),
            )
    return best / 100.0 if math.isfinite(best) else None


def load_raw_offmesh_footprints(
    snapshot: Path,
) -> tuple[tuple[Footprint, ...], dict[str, int]]:
    """Load only raw non-ground geometry; do not infer identity or links."""
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    coordinate_space = payload.get("coordinate_space")
    result: list[Footprint] = []
    counts = {"offmesh_polygons": 0, "offmesh_connections": 0}
    for tile in payload.get("tiles", []):
        raw_vertices = tile.get("vertices", [])
        for polygon in tile.get("polygons", []):
            if int(polygon.get("type", 0)) == DT_POLYTYPE_GROUND:
                continue
            indices = polygon.get("vertex_indices", [])
            if not isinstance(indices, list) or not 2 <= len(indices) <= 6:
                continue
            try:
                vertices = []
                for index in indices:
                    value = raw_vertices[int(index)]
                    x, y, z = float(value["x"]), float(value["y"]), float(value["z"])
                    if coordinate_space == "recast":
                        point = Vec3(-x, -z, y)
                    elif coordinate_space == "unreal":
                        point = Vec3(x, y, z)
                    else:
                        raise ValueError("unsupported_coordinate_space")
                    if not finite_point(point):
                        raise ValueError("non_finite_vertex")
                    vertices.append(point)
                result.append(Footprint(tuple(vertices), "offmesh_polygon"))
                counts["offmesh_polygons"] += 1
            except (IndexError, KeyError, TypeError, ValueError):
                continue
        for connection in tile.get("off_mesh_connections", []):
            try:
                start = connection["start"]
                end = connection["end"]
                if coordinate_space == "recast":
                    first = Vec3(float(-start[0]), float(-start[2]), float(start[1]))
                    second = Vec3(float(-end[0]), float(-end[2]), float(end[1]))
                elif coordinate_space == "unreal":
                    first = Vec3(float(start[0]), float(start[1]), float(start[2]))
                    second = Vec3(float(end[0]), float(end[1]), float(end[2]))
                else:
                    raise ValueError("unsupported_coordinate_space")
                if not finite_point(first) or not finite_point(second):
                    raise ValueError("non_finite_connection")
                result.append(Footprint((first, second), "offmesh_connection"))
                counts["offmesh_connections"] += 1
            except (IndexError, KeyError, TypeError, ValueError):
                continue
    return tuple(result), counts


def heading_change_degrees(waypoints: tuple[Vec3, ...]) -> float:
    headings: list[tuple[float, float]] = []
    for first, second in zip(waypoints, waypoints[1:]):
        dx, dy = second.x - first.x, second.y - first.y
        length = math.hypot(dx, dy)
        if length > 1e-6:
            headings.append((dx / length, dy / length))
    maximum = 0.0
    for first, second in zip(headings, headings[1:]):
        cosine = min(1.0, max(-1.0, first[0] * second[0] + first[1] * second[1]))
        maximum = max(maximum, math.degrees(math.acos(cosine)))
    return maximum


def portal_width_m(portal: DetourPortal) -> float:
    return horizontal_distance(portal.left, portal.right)


def directed_dijkstra(graph: DetourTopologyGraph, start: int) -> dict[int, float]:
    distances = {start: 0.0}
    frontier: list[tuple[float, int]] = [(0.0, start)]
    while frontier:
        cost, current = heapq.heappop(frontier)
        if cost > distances.get(current, float("inf")) + 1e-9:
            continue
        source = graph.polygons[current]
        for portal in graph.adjacency.get(current, ()):
            target = graph.polygons.get(portal.to_reference)
            if target is None or target.poly_type != DT_POLYTYPE_GROUND:
                continue
            next_cost = cost + distance(source.centroid, target.centroid)
            if next_cost >= distances.get(target.reference.value, float("inf")) - 1e-9:
                continue
            distances[target.reference.value] = next_cost
            heapq.heappush(frontier, (next_cost, target.reference.value))
    return distances


def resolution_rank_details(
    graph: DetourTopologyGraph, point: Vec3, resolution: Any
) -> dict[str, Any]:
    rows: list[tuple[Any, int, Vec3, float, float, bool]] = []
    for reference, polygon in graph.polygons.items():
        if polygon.poly_type != DT_POLYTYPE_GROUND:
            continue
        rank, closest, horizontal_cm, vertical_cm, contained = graph._candidate_resolution(
            point, polygon
        )
        if vertical_cm <= 20.0 * 100.0:
            rows.append((rank, reference, closest, horizontal_cm, vertical_cm, contained))
    rows.sort(key=lambda row: (row[0], row[1]))
    top = rows[0] if rows else None
    second = rows[1] if len(rows) > 1 else None
    unique_rank = bool(top) and sum(row[0] == top[0] for row in rows) == 1
    gap_m = None
    if top and second:
        gap_m = max(0.0, second[3] - top[3]) / 100.0
    return {
        "planner_resolution": resolution.to_dict(),
        "selected_polygon_ref": resolution.selected_reference,
        "resolved_position": point_dict(resolution.closest_point),
        "horizontal_snap_m": resolution.horizontal_distance_m,
        "vertical_separation_m": resolution.vertical_distance_m,
        "candidate_count": resolution.candidate_count,
        "unambiguous_under_planner_rank": unique_rank,
        "horizontal_gap_to_next_candidate_m": gap_m,
        "top_candidates": [
            {
                "ref": row[1],
                "horizontal_distance_m": row[3] / 100.0,
                "vertical_separation_m": row[4] / 100.0,
                "contained": row[5],
                "tile_index": graph.polygons[row[1]].reference.tile_index,
                "layer": graph.polygons[row[1]].layer,
            }
            for row in rows[:5]
        ],
    }


def transition_checks(
    graph: DetourTopologyGraph, corridor: tuple[int, ...], portals: tuple[DetourPortal, ...]
) -> tuple[list[dict[str, Any]], list[str], float | None]:
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    widths: list[float] = []
    transitions = portals[1:-1]
    if len(transitions) != max(0, len(corridor) - 1):
        failures.append("transition_portal_count_mismatch")
    for index, (source_ref, target_ref) in enumerate(zip(corridor, corridor[1:])):
        portal = transitions[index] if index < len(transitions) else None
        source = graph.polygons.get(source_ref)
        target = graph.polygons.get(target_ref)
        matching_links = []
        if source is not None:
            matching_links = [
                link
                for link in source.links
                if link.reference == target_ref
                and link.reference == (portal.link_reference if portal else None)
                and link.edge == (portal.source_edge if portal else None)
            ]
        link = matching_links[0] if matching_links else None
        width = portal_width_m(portal) if portal else None
        if width is not None and width > 0.0:
            widths.append(width)
        transition = {
            "index": index,
            "from_ref": source_ref,
            "to_ref": target_ref,
            "link_ref": portal.link_reference if portal else None,
            "side": link.side if link else (portal.side if portal else None),
            "source_edge": link.edge if link else (portal.source_edge if portal else None),
            "target_edge": portal.target_edge if portal else None,
            "portal_width_m": width,
            "ordinary_ground_to_ground": bool(
                source is not None
                and target is not None
                and source.poly_type == DT_POLYTYPE_GROUND
                and target.poly_type == DT_POLYTYPE_GROUND
                and link is not None
                and link.side in ORDINARY_SIDES
            ),
            "target_ref_resolved": target is not None,
            "edge_not_0xff": bool(link is not None and link.edge != INVALID_EDGE),
            "unsupported_link_side": bool(link is None or link.side not in ORDINARY_SIDES),
            "portal_finite": bool(
                portal is not None
                and finite_point(portal.left)
                and finite_point(portal.right)
            ),
            "target_edge_geometry_match": bool(portal is not None and portal.target_edge >= 0),
        }
        checks.append(transition)
        if not transition["ordinary_ground_to_ground"]:
            failures.append(f"transition_{index}_not_ordinary_ground_to_ground")
        if not transition["target_ref_resolved"]:
            failures.append(f"transition_{index}_target_ref_unresolved")
        if not transition["edge_not_0xff"]:
            failures.append(f"transition_{index}_edge_0xff")
        if transition["unsupported_link_side"]:
            failures.append(f"transition_{index}_unsupported_link_side")
        if not transition["portal_finite"] or not transition["target_edge_geometry_match"]:
            failures.append(f"transition_{index}_malformed_or_clipped_portal")
        if width is None or width <= 0.0:
            failures.append(f"transition_{index}_zero_or_nonfinite_portal_width")
    return checks, failures, min(widths) if widths else None


def evaluate_candidate(
    graph: DetourTopologyGraph,
    candidate_id: str,
    requested_start: Vec3,
    target_ref: int,
    known_offmesh: tuple[Footprint, ...],
    known_offmesh_counts: dict[str, int],
) -> dict[str, Any]:
    target = graph.polygons[target_ref]
    requested_end = target.centroid
    plan = graph.plan(
        requested_start,
        requested_end,
        max_snap_distance_m=8.0,
        max_vertical_separation_m=20.0,
        max_expansions=50000,
        max_waypoints=128,
    )
    route = plan.to_dict()
    failures: list[str] = []
    transition_data: list[dict[str, Any]] = []
    minimum_portal_width_m: float | None = None
    if plan.status != "complete":
        failures.append(f"planner_{plan.reason_code}")
    if plan.movement_sent is not False:
        failures.append("movement_sent_true")
    if plan.status == "complete":
        transition_data, transition_failures, minimum_portal_width_m = transition_checks(
            graph, plan.polygon_corridor, plan.portals
        )
        failures.extend(transition_failures)
        if any(graph.polygons[ref].poly_type != DT_POLYTYPE_GROUND for ref in plan.polygon_corridor):
            failures.append("corridor_contains_non_ground_polygon")
        if not all(finite_point(point) for point in plan.waypoints):
            failures.append("waypoint_validation_failure_nonfinite")
        if not plan.validation.get("waypoints_finite"):
            failures.append("waypoint_validation_failure_planner_flag")
        if not plan.validation.get("waypoint_start_matches_resolved"):
            failures.append("waypoint_validation_failure_start")
        if not plan.validation.get("waypoint_end_matches_resolved"):
            failures.append("waypoint_validation_failure_end")
        if len(plan.waypoints) == 0:
            failures.append("waypoint_validation_failure_empty")

    offmesh_distance = route_offmesh_distance(plan.waypoints, known_offmesh) if plan.status == "complete" else None
    metrics = {
        "requested_A": point_dict(plan.requested_start),
        "requested_B": point_dict(plan.requested_end),
        "resolved_A": point_dict(plan.resolved_start),
        "resolved_B": point_dict(plan.resolved_end),
        "start_ref": plan.start_ref,
        "end_ref": plan.end_ref,
        "polygon_count": len(plan.polygon_corridor),
        "tile_crossings": plan.validation.get("tile_crossings"),
        "waypoint_count": len(plan.waypoints),
        "estimated_length_m": plan.total_estimated_path_length_m,
        "minimum_portal_width_m": minimum_portal_width_m,
        "maximum_heading_change_degrees": heading_change_degrees(plan.waypoints) if plan.status == "complete" else None,
        "minimum_distance_to_known_offmesh_m": offmesh_distance,
        "known_offmesh_polygon_count": known_offmesh_counts["offmesh_polygons"],
        "known_offmesh_connection_count": known_offmesh_counts["offmesh_connections"],
        "known_offmesh_geometry_count": len(known_offmesh),
        "transition_count": len(plan.polygon_corridor) - 1 if plan.status == "complete" else None,
        "transition_checks": transition_data,
        "validation_failures": failures,
        "all_required_checks_pass": not failures,
    }
    return {
        "candidate_id": candidate_id,
        "movement_sent": False,
        "route_plan": route,
        "metrics": metrics,
    }


def safety_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    metrics = candidate["metrics"]
    if not metrics["all_required_checks_pass"]:
        return (1, float("inf"))
    return (
        0,
        metrics["tile_crossings"],
        metrics["transition_count"],
        metrics["waypoint_count"],
        metrics["maximum_heading_change_degrees"],
        -(metrics["minimum_portal_width_m"] or 0.0),
        metrics["estimated_length_m"],
    )


def choose_near_band(
    candidates: list[dict[str, Any]], minimum: float, maximum: float
) -> tuple[dict[str, Any] | None, bool]:
    complete = [
        candidate
        for candidate in candidates
        if candidate["metrics"]["all_required_checks_pass"]
        and candidate["metrics"]["estimated_length_m"] is not None
    ]
    inside = [
        candidate
        for candidate in complete
        if minimum <= candidate["metrics"]["estimated_length_m"] <= maximum
    ]
    if inside:
        return min(inside, key=safety_key), True
    if not complete:
        return None, False
    target = (minimum + maximum) / 2.0
    return min(
        complete,
        key=lambda candidate: (
            abs(candidate["metrics"]["estimated_length_m"] - target),
            safety_key(candidate),
        ),
    ), False


def nearby_start_options(
    graph: DetourTopologyGraph,
    recorded_point: Vec3,
    recorded_ref: int,
    *,
    max_distance_m: float = 50.0,
    max_options: int = 6,
) -> list[dict[str, Any]]:
    """Find nearby captured ground anchors with useful directed reachability."""
    nearby = sorted(
        (
            horizontal_distance(recorded_point, polygon.centroid),
            reference,
        )
        for reference, polygon in graph.polygons.items()
        if reference != recorded_ref
        and polygon.poly_type == DT_POLYTYPE_GROUND
        and horizontal_distance(recorded_point, polygon.centroid) <= max_distance_m
    )
    profiled: list[dict[str, Any]] = []
    for distance_to_recorded, reference in nearby[:96]:
        reachable = directed_dijkstra(graph, reference)
        if len(reachable) < 2:
            continue
        profiled.append(
            {
                "start_ref": reference,
                "requested_start": graph.polygons[reference].centroid,
                "distance_to_recorded_m": distance_to_recorded,
                "reachable_ground_polygon_count": len(reachable),
                "outgoing_transition_count": len(graph.adjacency.get(reference, ())),
            }
        )
    profiled.sort(
        key=lambda option: (
            option["distance_to_recorded_m"],
            -option["reachable_ground_polygon_count"],
            -option["outgoing_transition_count"],
            option["start_ref"],
        )
    )
    return profiled[:max_options]


def target_refs_for_start(
    graph: DetourTopologyGraph,
    requested_start: Vec3,
    start_ref: int,
    *,
    allow_same_polygon: bool,
    max_target_distance_m: float,
) -> tuple[list[int], int, int]:
    distances = directed_dijkstra(graph, start_ref)
    start_poly = graph.polygons[start_ref]
    start_resolution = graph.resolve_point(requested_start)
    if start_resolution.closest_point is None:
        return [], len(distances), 0
    offset_to_start_centroid = distance(start_resolution.closest_point, start_poly.centroid)
    approximations: list[tuple[float, int]] = []
    for ref, graph_cost in distances.items():
        polygon = graph.polygons[ref]
        if polygon.poly_type != DT_POLYTYPE_GROUND:
            continue
        if ref == start_ref and not allow_same_polygon:
            continue
        approximate = (
            distance(start_resolution.closest_point, polygon.centroid)
            if ref == start_ref
            else offset_to_start_centroid + graph_cost
        )
        if approximate <= max_target_distance_m:
            approximations.append((approximate, ref))
    approximations.sort()

    desired_lengths = (7.5, 15.0, 30.0, 42.0, 22.0)
    selected_refs: list[int] = []
    for desired in desired_lengths:
        nearby = sorted(
            approximations,
            key=lambda item: (abs(item[0] - desired), item[0], item[1]),
        )
        for _, ref in nearby:
            if ref not in selected_refs:
                selected_refs.append(ref)
                break
    # Add a small local sweep so a simple fallback can be chosen from actual
    # alternatives without planning every polygon in the partial snapshot.
    for approximate, ref in approximations:
        if 5.0 <= approximate <= 50.0 and ref not in selected_refs:
            selected_refs.append(ref)
        if len(selected_refs) >= 12:
            break
    return selected_refs, len(distances), len(approximations)


def select_candidates(
    graph: DetourTopologyGraph,
    requested_start: Vec3,
    start_ref: int,
    *,
    start_specs: list[tuple[str, Vec3, int]] | None = None,
    known_offmesh: tuple[Footprint, ...] = (),
    known_offmesh_counts: dict[str, int] | None = None,
    max_target_distance_m: float = 65.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if start_specs is None:
        start_specs = [("recorded", requested_start, start_ref)]
    if known_offmesh_counts is None:
        known_offmesh_counts = {"offmesh_polygons": 0, "offmesh_connections": 0}
    evaluation_specs: list[tuple[Vec3, int]] = []
    start_summaries: list[dict[str, Any]] = []
    for label, start_point, anchor_ref in start_specs:
        refs, reachable_count, pool_count = target_refs_for_start(
            graph,
            start_point,
            anchor_ref,
            allow_same_polygon=label == "recorded",
            max_target_distance_m=max_target_distance_m,
        )
        for ref in refs:
            pair = (anchor_ref, ref)
            if pair not in {(item[1], item[0]) for item in evaluation_specs}:
                evaluation_specs.append((ref, anchor_ref))
        start_summaries.append(
            {
                "label": label,
                "start_ref": anchor_ref,
                "requested_start": point_dict(start_point),
                "reachable_ground_polygon_count": reachable_count,
                "approximate_target_pool_count": pool_count,
                "selected_target_ref_count": len(refs),
            }
        )

    evaluations = []
    for index, (target_ref, anchor_ref) in enumerate(evaluation_specs, start=1):
        anchor_point = next(point for _, point, ref in start_specs if ref == anchor_ref)
        evaluations.append(
            evaluate_candidate(
                graph,
                f"candidate-{index:02d}",
                anchor_point,
                target_ref,
                known_offmesh,
                known_offmesh_counts,
            )
        )
    successful = [
        candidate
        for candidate in evaluations
        if candidate["metrics"]["all_required_checks_pass"]
    ]
    very_short, very_short_exact = choose_near_band(successful, 5.0, 10.0)
    short, short_exact = choose_near_band(successful, 10.0, 25.0)
    moderate, moderate_exact = choose_near_band(successful, 25.0, 50.0)

    chosen_ids = {candidate["candidate_id"] for candidate in (very_short, short, moderate) if candidate}
    fallback_pool = [candidate for candidate in successful if candidate["candidate_id"] not in chosen_ids]
    if not fallback_pool:
        fallback_pool = successful
    fallback = min(fallback_pool, key=safety_key) if fallback_pool else None

    assignments = {
        "very_short": {"candidate": very_short, "exact_band": very_short_exact},
        "short": {"candidate": short, "exact_band": short_exact},
        "moderate": {"candidate": moderate, "exact_band": moderate_exact},
        "fallback": {"candidate": fallback, "exact_band": False},
    }
    ranked = sorted(successful, key=safety_key)
    return evaluations, {
        "candidate_assignments": {
            name: {
                "candidate_id": value["candidate"]["candidate_id"] if value["candidate"] else None,
                "exact_distance_band": value["exact_band"],
            }
            for name, value in assignments.items()
        },
        "ranked_successful_candidate_ids": [candidate["candidate_id"] for candidate in ranked],
        "start_summaries": start_summaries,
        "evaluated_target_count": len(evaluations),
        "assignments": assignments,
    }


def output_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=REPO_ROOT / "ProjectKnowledge" / "Evidence" / "2026-08-17-shipping-detour-topology-live" / "navigation-native-geometry.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "ProjectKnowledge" / "Evidence" / "2026-08-18-first-live-route-candidates",
    )
    args = parser.parse_args()
    snapshot = args.snapshot if args.snapshot.is_absolute() else REPO_ROOT / args.snapshot
    output = args.output if args.output.is_absolute() else REPO_ROOT / args.output

    graph = load_shipping_detour_topology(snapshot)
    known_offmesh, known_offmesh_counts = load_raw_offmesh_footprints(snapshot)
    recorded_resolution = graph.resolve_point(RECORDED_CLOUD)
    resolution_report = resolution_rank_details(graph, RECORDED_CLOUD, recorded_resolution)
    if not recorded_resolution.ok:
        nearest_ref = recorded_resolution.diagnostics.get("nearest_ref")
        if nearest_ref not in graph.polygons or graph.polygons[nearest_ref].poly_type != DT_POLYTYPE_GROUND:
            raise SystemExit("recorded Cloud point cannot be resolved and no nearby ground polygon is available")
        start_ref = nearest_ref
        requested_start = graph.polygons[start_ref].centroid
        start_policy = "nearby_ground_polygon_centroid_fallback"
    else:
        start_ref = recorded_resolution.selected_reference
        requested_start = RECORDED_CLOUD
        start_policy = "recorded_position_unique_ground_snap"

    nearby_options = nearby_start_options(graph, RECORDED_CLOUD, start_ref)
    start_specs = [("recorded", requested_start, start_ref)] + [
        (f"nearby-{index:02d}", option["requested_start"], option["start_ref"])
        for index, option in enumerate(nearby_options, start=1)
    ]
    evaluations, selection = select_candidates(
        graph,
        requested_start,
        start_ref,
        start_specs=start_specs,
        known_offmesh=known_offmesh,
        known_offmesh_counts=known_offmesh_counts,
    )
    assignments = selection.pop("assignments")
    output.mkdir(parents=True, exist_ok=True)
    selected_candidate_ids = {
        value["candidate"]["candidate_id"]
        for value in assignments.values()
        if value["candidate"]
    }
    for candidate in evaluations:
        if candidate["candidate_id"] not in selected_candidate_ids:
            continue
        output_json(output / f"{candidate['candidate_id']}.json", candidate["route_plan"])

    selected = {
        name: value["candidate"] for name, value in assignments.items() if value["candidate"]
    }
    report = {
        "artifact_type": "offline_first_live_route_candidate_selection",
        "artifact_version": 1,
        "movement_sent": False,
        "planner_dependency": {
            "module": "ff7r_agent.detour_route",
            "source_path": "Tools/ff7r-codex-agent-mvp/src/ff7r_agent/detour_route.py",
            "accepted_base": "codex/validate-global-route-source-plan-a",
            "accepted_commit": "1c04287",
            "read_only_dependency": True,
        },
        "snapshot": {
            "path": str(snapshot.relative_to(REPO_ROOT)).replace("\\", "/") if snapshot.is_relative_to(REPO_ROOT) else str(snapshot),
            "diagnostics": graph.snapshot_diagnostics,
            "raw_known_offmesh_geometry_count": len(known_offmesh),
            "raw_known_offmesh_counts": known_offmesh_counts,
            "movement_sent": False,
        },
        "recorded_cloud_position": point_dict(RECORDED_CLOUD),
        "recorded_cloud_resolution": resolution_report,
        "candidate_start_policy": {
            "policy": start_policy,
            "requested_start_used": point_dict(requested_start),
            "start_ref_used": start_ref,
            "nearby_ground_anchor_options": [
                {
                    **option,
                    "requested_start": point_dict(option["requested_start"]),
                }
                for option in nearby_options
            ],
            "note": "The preserved pose is a unique 4.75893 m outside-footprint snap; a fresh live project/clearance check remains required before attempting movement.",
        },
        "selection": selection,
        "evaluated_candidate_summaries": [
            {
                "candidate_id": candidate["candidate_id"],
                "status": candidate["route_plan"]["status"],
                "reason_code": candidate["route_plan"]["reason_code"],
                "movement_sent": candidate["route_plan"]["movement_sent"],
                "start_ref": candidate["route_plan"]["start_ref"],
                "end_ref": candidate["route_plan"]["end_ref"],
                "polygon_corridor": candidate["route_plan"]["polygon_corridor"],
                "metrics": candidate["metrics"],
            }
            for candidate in evaluations
        ],
        "selected_routes": {
            name: {
                "candidate_id": candidate["candidate_id"],
                "route_plan": candidate["route_plan"],
                "metrics": candidate["metrics"],
            }
            for name, candidate in selected.items()
        },
        "all_routeplans_movement_sent": all(
            candidate["route_plan"].get("movement_sent") is False for candidate in evaluations
        ),
    }
    output_json(output / "candidate-selection-report.json", report)
    print(json.dumps({
        "output": str(output),
        "recorded_ref": recorded_resolution.selected_reference,
        "recorded_resolution_reason": recorded_resolution.reason_code,
        "evaluated": len(evaluations),
        "successful": sum(1 for candidate in evaluations if candidate["metrics"]["all_required_checks_pass"]),
        "assignments": selection["candidate_assignments"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
