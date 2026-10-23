#!/usr/bin/env python3
"""Observational benchmark for live authorized-corridor point resolution.

Wall-clock values are evidence only. Correctness/equivalence failures are hard
failures; timing values are never used as CI thresholds.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any, Callable

from ff7r_agent.detour_route import (
    CorridorPointResolution,
    DetourPolygon,
    DetourTopologyGraph,
    PointResolutionCandidate,
    PolygonReference,
)
from ff7r_agent.models import Vec3


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


def ref_for(index: int) -> int:
    return PolygonReference.make(1, 0, index + 1).value


def build_graph(width: int = 200, height: int = 150, cell_cm: float = 100.0) -> DetourTopologyGraph:
    polygons: dict[int, DetourPolygon] = {}
    for index in range(width * height):
        row, column = divmod(index, width)
        x = column * cell_cm
        y = row * cell_cm
        reference = PolygonReference.from_value(ref_for(index))
        polygons[reference.value] = DetourPolygon(
            reference=reference,
            tile_x=column // 10,
            tile_y=row // 10,
            layer=0,
            vertices=(
                Vec3(x, y, 0.0),
                Vec3(x + cell_cm, y, 0.0),
                Vec3(x + cell_cm, y + cell_cm, 0.0),
                Vec3(x, y + cell_cm, 0.0),
            ),
            flags=1,
            area=0,
            poly_type=0,
            links=(),
            identity_source="benchmark",
        )
    return DetourTopologyGraph(polygons, {}, snapshot_diagnostics={"fixture": "corridor_perf"})


def snake_indices(width: int, height: int) -> list[int]:
    result: list[int] = []
    for row in range(height):
        columns = range(width) if row % 2 == 0 else range(width - 1, -1, -1)
        result.extend(row * width + column for column in columns)
    return result


def legacy_resolve_point_in_corridor(
    graph: DetourTopologyGraph,
    point: Vec3,
    corridor_references: tuple[int, ...],
    *,
    max_snap_distance_m: float,
    max_vertical_separation_m: float,
    global_resolution: Any,
) -> CorridorPointResolution:
    """Reference implementation matching the pre-optimization full corridor scan."""
    corridor = tuple(int(reference) for reference in corridor_references)
    corridor_index_by_ref = {reference: index for index, reference in enumerate(corridor)}
    candidates = tuple(
        PointResolutionCandidate(
            reference=candidate.reference,
            closest_point=candidate.closest_point,
            horizontal_distance_m=candidate.horizontal_distance_m,
            vertical_distance_m=candidate.vertical_distance_m,
            contained=candidate.contained,
            ranking=candidate.ranking,
            corridor_index=corridor_index_by_ref.get(candidate.reference),
        )
        for candidate in graph.resolve_point_candidates(
            point,
            references=corridor,
            max_snap_distance_m=max_snap_distance_m,
            max_vertical_separation_m=max_vertical_separation_m,
            reject_off_mesh=True,
        )
    )
    contained = tuple(candidate for candidate in candidates if candidate.contained)
    if not candidates:
        status, reason_code, selected_reference, corridor_index = (
            "unresolved", "no_authorized_corridor_candidate", None, None
        )
    elif len(contained) > 1:
        status, reason_code, selected_reference, corridor_index = (
            "ambiguous", "authorized_shared_boundary_or_overlap", None, None
        )
    elif len(contained) == 1:
        selected_reference = contained[0].reference
        corridor_index = contained[0].corridor_index
        if global_resolution.selected_reference not in corridor_index_by_ref:
            status, reason_code = "ambiguous", "global_non_corridor_with_authorized_candidate"
        else:
            status, reason_code = "resolved", "inside_authorized_corridor"
    elif len(candidates) == 1:
        selected_reference = candidates[0].reference
        corridor_index = candidates[0].corridor_index
        status, reason_code = "resolved", "within_authorized_snap"
    else:
        status, reason_code, selected_reference, corridor_index = (
            "ambiguous", "authorized_snap_ambiguous", None, None
        )
    return CorridorPointResolution(
        status=status,
        reason_code=reason_code,
        selected_reference=selected_reference,
        corridor_index=corridor_index,
        candidates=candidates,
        global_resolution=global_resolution,
    )


def measure_calls(callback: Callable[[int], Any], count: int, repetitions: int) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(repetitions):
        for index in range(count):
            started = time.perf_counter_ns()
            callback(index)
            samples.append((time.perf_counter_ns() - started) / 1_000_000.0)
    return summarize_ms(samples)


def benchmark_corridor(graph: DetourTopologyGraph, corridor_length: int) -> dict[str, Any]:
    path = snake_indices(200, 150)[:corridor_length]
    corridor = tuple(ref_for(index) for index in path)
    sample_count = min(180, max(60, corridor_length))
    points: list[Vec3] = []
    for sample_index in range(sample_count):
        path_index = round(sample_index * (corridor_length - 1) / max(1, sample_count - 1))
        polygon = graph.polygons[corridor[path_index]]
        center = polygon.centroid
        # Deterministic sub-polygon movement offsets emulate successive 10 Hz samples.
        offset = ((sample_index % 5) - 2) * 7.5
        points.append(Vec3(center.x + offset, center.y - offset * 0.5, center.z))

    globals_ = [
        graph.resolve_point(point, max_snap_distance_m=0.75, max_vertical_separation_m=1.0)
        for point in points
    ]
    legacy_results = [
        legacy_resolve_point_in_corridor(
            graph,
            point,
            corridor,
            max_snap_distance_m=0.75,
            max_vertical_separation_m=1.0,
            global_resolution=global_resolution,
        )
        for point, global_resolution in zip(points, globals_)
    ]
    production_results = [
        graph.resolve_point_in_corridor(
            point,
            corridor,
            max_snap_distance_m=0.75,
            max_vertical_separation_m=1.0,
            global_resolution=global_resolution,
        )
        for point, global_resolution in zip(points, globals_)
    ]
    equivalent = all(
        legacy.to_dict() == production.to_dict()
        for legacy, production in zip(legacy_results, production_results)
    )
    if not equivalent:
        raise AssertionError(f"corridor resolution mismatch at length {corridor_length}")

    repetitions = 3
    legacy_timing = measure_calls(
        lambda index: legacy_resolve_point_in_corridor(
            graph,
            points[index],
            corridor,
            max_snap_distance_m=0.75,
            max_vertical_separation_m=1.0,
            global_resolution=globals_[index],
        ),
        len(points),
        repetitions,
    )
    production_timing = measure_calls(
        lambda index: graph.resolve_point_in_corridor(
            points[index],
            corridor,
            max_snap_distance_m=0.75,
            max_vertical_separation_m=1.0,
            global_resolution=globals_[index],
        ),
        len(points),
        repetitions,
    )
    return {
        "corridor_polygons": corridor_length,
        "sample_count": len(points),
        "legacy_full_corridor_scan": legacy_timing,
        "production": production_timing,
        "equivalent_results": equivalent,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    started = time.perf_counter_ns()
    graph = build_graph()
    graph_build_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    results = {
        "benchmark_schema": 1,
        "observational_only": True,
        "timing_assertions": False,
        "topology": {"polygon_count": len(graph.polygons), "width": 200, "height": 150},
        "graph_build_ms": round(graph_build_ms, 4),
        "corridors": {
            str(length): benchmark_corridor(graph, length)
            for length in (15, 176, 349)
        },
    }
    text = json.dumps(results, indent=2, sort_keys=True)
    print(text)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
