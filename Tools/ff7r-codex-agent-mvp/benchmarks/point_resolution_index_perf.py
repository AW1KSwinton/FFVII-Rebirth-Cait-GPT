#!/usr/bin/env python3
"""Measure spatial-index selectivity versus construction/memory trade-offs."""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

import ff7r_agent.detour_route as detour
from corridor_resolution_perf import build_graph, ref_for, snake_indices
from ff7r_agent.models import Vec3

SNAP_M = 0.75
VERTICAL_M = 1.0
CELL_SIZES_CM = (1000.0, 500.0, 250.0, 200.0, 100.0)
REAL_SNAPSHOT = (
    Path(__file__).parents[3]
    / "ProjectKnowledge"
    / "Evidence"
    / "2026-08-17-shipping-detour-topology-live"
    / "navigation-native-geometry.json"
)


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def summary(samples: list[float]) -> dict[str, float]:
    return {
        "median": round(statistics.median(samples), 5),
        "p95": round(percentile(samples, 0.95), 5),
    }


def semantic_resolution(value: Any) -> tuple[Any, ...]:
    return (
        value.status,
        value.reason_code,
        value.selected_reference,
        value.closest_point,
        value.horizontal_distance_m,
        value.vertical_distance_m,
    )


def synthetic_points(graph: Any, count: int = 180) -> list[Vec3]:
    path = snake_indices(200, 150)
    points: list[Vec3] = []
    for sample_index in range(count):
        path_index = round(sample_index * (len(path) - 1) / max(1, count - 1))
        center = graph.polygons[ref_for(path[path_index])].centroid
        offset = ((sample_index % 5) - 2) * 7.5
        points.append(Vec3(center.x + offset, center.y - offset * 0.5, center.z))
    return points


def real_points(graph: Any, count: int = 180) -> list[Vec3]:
    references = sorted(graph.polygons)
    if len(references) <= count:
        chosen = references
    else:
        chosen = [references[round(i * (len(references) - 1) / (count - 1))] for i in range(count)]
    return [graph.polygons[reference].centroid for reference in chosen]


def run_resolution_measurements(graph: Any, points: list[Vec3]) -> dict[str, Any]:
    resolutions = []
    timings_ms: list[float] = []
    reference_counts: list[float] = []
    for _ in range(3):
        for point in points:
            reference_counts.append(float(len(graph._point_resolution_references(point, SNAP_M))))
            started = time.perf_counter_ns()
            result = graph.resolve_point(
                point,
                max_snap_distance_m=SNAP_M,
                max_vertical_separation_m=VERTICAL_M,
            )
            timings_ms.append((time.perf_counter_ns() - started) / 1_000_000.0)
            if len(resolutions) < len(points):
                resolutions.append(result)
    return {
        "resolutions": resolutions,
        "resolution_ms": summary(timings_ms),
        "candidate_references": summary(reference_counts),
        "bucket_count": len(graph._point_resolution_index),
        "bucket_memberships": sum(len(values) for values in graph._point_resolution_index.values()),
        "overflow_polygons": len(graph._point_resolution_index_overflow),
    }


def benchmark_family(kind: str) -> dict[str, Any]:
    baseline_semantics: list[tuple[Any, ...]] | None = None
    result: dict[str, Any] = {}
    original = detour.POINT_RESOLUTION_INDEX_CELL_SIZE_CM
    try:
        for cell_size in CELL_SIZES_CM:
            detour.POINT_RESOLUTION_INDEX_CELL_SIZE_CM = cell_size
            started = time.perf_counter_ns()
            graph = build_graph() if kind == "synthetic_30k" else detour.load_shipping_detour_topology(REAL_SNAPSHOT)
            build_ms = (time.perf_counter_ns() - started) / 1_000_000.0
            points = synthetic_points(graph) if kind == "synthetic_30k" else real_points(graph)
            measured = run_resolution_measurements(graph, points)
            semantics = [semantic_resolution(value) for value in measured.pop("resolutions")]
            if baseline_semantics is None:
                baseline_semantics = semantics
                equivalent = True
            else:
                equivalent = semantics == baseline_semantics
            if not equivalent:
                raise AssertionError(f"resolution semantics changed for {kind} at {cell_size} cm")
            result[str(int(cell_size))] = {
                "cell_size_cm": cell_size,
                "graph_build_ms": round(build_ms, 5),
                "semantic_equivalence_to_1000cm": equivalent,
                **measured,
            }
    finally:
        detour.POINT_RESOLUTION_INDEX_CELL_SIZE_CM = original
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = {
        "benchmark_schema": 1,
        "observational_only": True,
        "timing_assertions": False,
        "max_buckets_per_polygon": detour.POINT_RESOLUTION_INDEX_MAX_BUCKETS_PER_POLYGON,
        "families": {
            "synthetic_30k": benchmark_family("synthetic_30k"),
            "preserved_shipping_snapshot": benchmark_family("preserved_shipping_snapshot"),
        },
    }
    text = json.dumps(results, indent=2, sort_keys=True)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
