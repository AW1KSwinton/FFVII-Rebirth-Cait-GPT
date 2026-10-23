#!/usr/bin/env python3
"""Observational benchmark for sharing global/corridor point geometry work.

Correctness mismatches fail. Timing values are evidence only and are never CI
thresholds.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any, Callable

from corridor_resolution_perf import build_graph, ref_for, snake_indices
from ff7r_agent.detour_route import CorridorPointResolution, PointResolution, PointResolutionCandidate
from ff7r_agent.models import Vec3

SNAP_M = 0.75
VERTICAL_M = 1.0


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def summarize_ms(samples: list[float]) -> dict[str, float]:
    return {
        "median_ms": round(statistics.median(samples), 5),
        "p95_ms": round(percentile(samples, 0.95), 5),
        "min_ms": round(min(samples), 5),
        "max_ms": round(max(samples), 5),
    }


def measure(callback: Callable[[int], Any], count: int, repetitions: int = 4) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(repetitions):
        for index in range(count):
            started = time.perf_counter_ns()
            callback(index)
            samples.append((time.perf_counter_ns() - started) / 1_000_000.0)
    return summarize_ms(samples)


def legacy_pair(graph: Any, point: Vec3, corridor: tuple[int, ...]) -> tuple[PointResolution, CorridorPointResolution]:
    global_resolution = graph.resolve_point(
        point,
        max_snap_distance_m=SNAP_M,
        max_vertical_separation_m=VERTICAL_M,
    )
    corridor_resolution = graph.resolve_point_in_corridor(
        point,
        corridor,
        max_snap_distance_m=SNAP_M,
        max_vertical_separation_m=VERTICAL_M,
        global_resolution=global_resolution,
    )
    return global_resolution, corridor_resolution


def shared_pair_prototype(graph: Any, point: Vec3, corridor: tuple[int, ...]) -> tuple[PointResolution, CorridorPointResolution]:
    """Prototype one exact-geometry pass, preserving current public outputs."""
    if not all(math.isfinite(float(value)) for value in (point.x, point.y, point.z)):
        global_resolution = graph.resolve_point(
            point,
            max_snap_distance_m=SNAP_M,
            max_vertical_separation_m=VERTICAL_M,
        )
        corridor_resolution = graph.resolve_point_in_corridor(
            point,
            corridor,
            max_snap_distance_m=SNAP_M,
            max_vertical_separation_m=VERTICAL_M,
            global_resolution=global_resolution,
        )
        return global_resolution, corridor_resolution

    corridor_index_by_ref = {int(reference): index for index, reference in enumerate(corridor)}
    references = graph._point_resolution_references(point, SNAP_M)
    raw: list[tuple[tuple[int, float, float], int, Vec3, float, float, bool]] = []
    exact_calls = 0
    for reference in references:
        polygon = graph.polygons.get(reference)
        if polygon is None or polygon.poly_type != 0:
            continue
        exact_calls += 1
        rank, closest, horizontal_cm, vertical_cm, contained = graph._candidate_resolution(point, polygon)
        if vertical_cm > VERTICAL_M * 100.0:
            continue
        raw.append((rank, reference, closest, horizontal_cm, vertical_cm, contained))

    if not raw:
        global_resolution = graph.resolve_point_full_scan(
            point,
            max_snap_distance_m=SNAP_M,
            max_vertical_separation_m=VERTICAL_M,
        )
    else:
        raw.sort(key=lambda item: (item[0], item[1]))
        _, reference, closest, horizontal_cm, vertical_cm, contained = raw[0]
        if horizontal_cm > SNAP_M * 100.0:
            global_resolution = PointResolution(
                "unresolved", "point_outside_navmesh", point, None, closest,
                horizontal_cm / 100.0, vertical_cm / 100.0, len(raw),
                {
                    "nearest_ref": reference,
                    "contained_candidate": contained,
                    "candidate_count_scope": "indexed_spatial_superset",
                },
            )
        else:
            global_resolution = PointResolution(
                "resolved", "point_inside_polygon" if contained else "point_snapped_to_polygon",
                point, reference, closest, horizontal_cm / 100.0, vertical_cm / 100.0,
                len(raw),
                {
                    "contained_candidate": contained,
                    "candidate_count_scope": "indexed_spatial_superset",
                },
            )

    corridor_candidates = tuple(sorted((
        PointResolutionCandidate(
            reference=reference,
            closest_point=closest,
            horizontal_distance_m=horizontal_cm / 100.0,
            vertical_distance_m=vertical_cm / 100.0,
            contained=contained,
            ranking=tuple(float(value) for value in rank),
            corridor_index=corridor_index_by_ref.get(reference),
        )
        for rank, reference, closest, horizontal_cm, vertical_cm, contained in raw
        if reference in corridor_index_by_ref and horizontal_cm <= SNAP_M * 100.0
    ), key=lambda candidate: (candidate.ranking, candidate.reference)))

    contained_candidates = tuple(candidate for candidate in corridor_candidates if candidate.contained)
    if not corridor_candidates:
        status, reason, selected, corridor_index = "unresolved", "no_authorized_corridor_candidate", None, None
    elif len(contained_candidates) > 1:
        status, reason, selected, corridor_index = "ambiguous", "authorized_shared_boundary_or_overlap", None, None
    elif len(contained_candidates) == 1:
        selected = contained_candidates[0].reference
        corridor_index = contained_candidates[0].corridor_index
        if global_resolution.selected_reference not in corridor_index_by_ref:
            status, reason = "ambiguous", "global_non_corridor_with_authorized_candidate"
        else:
            status, reason = "resolved", "inside_authorized_corridor"
    elif len(corridor_candidates) == 1:
        selected = corridor_candidates[0].reference
        corridor_index = corridor_candidates[0].corridor_index
        status, reason = "resolved", "within_authorized_snap"
    else:
        status, reason, selected, corridor_index = "ambiguous", "authorized_snap_ambiguous", None, None

    return global_resolution, CorridorPointResolution(
        status=status,
        reason_code=reason,
        selected_reference=selected,
        corridor_index=corridor_index,
        candidates=corridor_candidates,
        global_resolution=global_resolution,
    )


def exact_call_counts(graph: Any, point: Vec3, corridor: tuple[int, ...]) -> tuple[int, int]:
    refs = graph._point_resolution_references(point, SNAP_M)
    corridor_set = set(corridor)
    global_count = sum(1 for ref in refs if graph.polygons.get(ref) is not None and graph.polygons[ref].poly_type == 0)
    corridor_count = sum(1 for ref in refs if ref in corridor_set and graph.polygons.get(ref) is not None and graph.polygons[ref].poly_type == 0)
    return global_count + corridor_count, global_count


def benchmark_corridor(graph: Any, corridor_length: int) -> dict[str, Any]:
    path = snake_indices(200, 150)[:corridor_length]
    corridor = tuple(ref_for(index) for index in path)
    sample_count = min(180, max(60, corridor_length))
    points: list[Vec3] = []
    for sample_index in range(sample_count):
        path_index = round(sample_index * (corridor_length - 1) / max(1, sample_count - 1))
        center = graph.polygons[corridor[path_index]].centroid
        offset = ((sample_index % 5) - 2) * 7.5
        points.append(Vec3(center.x + offset, center.y - offset * 0.5, center.z))

    legacy_results = [legacy_pair(graph, point, corridor) for point in points]
    shared_results = [shared_pair_prototype(graph, point, corridor) for point in points]
    equivalent = all(
        old_global.to_dict() == new_global.to_dict() and old_corridor.to_dict() == new_corridor.to_dict()
        for (old_global, old_corridor), (new_global, new_corridor) in zip(legacy_results, shared_results)
    )
    if not equivalent:
        raise AssertionError(f"shared point-resolution mismatch at corridor {corridor_length}")

    counts = [exact_call_counts(graph, point, corridor) for point in points]
    return {
        "corridor_polygons": corridor_length,
        "sample_count": len(points),
        "legacy_two_pass": measure(lambda index: legacy_pair(graph, points[index], corridor), len(points)),
        "shared_one_pass_prototype": measure(lambda index: shared_pair_prototype(graph, points[index], corridor), len(points)),
        "median_exact_geometry_calls_legacy": statistics.median(pair[0] for pair in counts),
        "median_exact_geometry_calls_shared": statistics.median(pair[1] for pair in counts),
        "equivalent_results": equivalent,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    started = time.perf_counter_ns()
    graph = build_graph()
    results = {
        "benchmark_schema": 1,
        "observational_only": True,
        "timing_assertions": False,
        "topology": {"polygon_count": len(graph.polygons), "width": 200, "height": 150},
        "graph_build_ms": round((time.perf_counter_ns() - started) / 1_000_000.0, 4),
        "corridors": {str(length): benchmark_corridor(graph, length) for length in (15, 176, 349)},
    }
    text = json.dumps(results, indent=2, sort_keys=True)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
