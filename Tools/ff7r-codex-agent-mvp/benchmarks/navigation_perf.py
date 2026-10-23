#!/usr/bin/env python3
"""Observational navigation microbenchmarks; never used as pass/fail timing gates.

The synthetic graph intentionally matches the production order of magnitude
(~30k ground polygons) while keeping geometry deterministic and independent of
private capture artifacts. Correctness/equivalence checks fail normally; wall
clock results are reported only.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import platform
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from ff7r_agent.detour_route import (
    INVALID_LINK,
    DetourLink,
    DetourPolygon,
    DetourPortal,
    DetourTopologyGraph,
    PolygonReference,
    _portal_from_link,
)
from ff7r_agent.models import Vec3
from ff7r_agent.topology_cache import sha256_file, static_topology_fingerprint


def percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def summarize_ms(samples: list[float]) -> dict[str, float]:
    return {
        "median_ms": round(statistics.median(samples), 4),
        "p95_ms": round(percentile(samples, 0.95), 4),
        "min_ms": round(min(samples), 4),
        "max_ms": round(max(samples), 4),
    }


def measure(callback: Callable[[], Any], repetitions: int) -> tuple[dict[str, float], Any]:
    samples: list[float] = []
    result: Any = None
    for _ in range(repetitions):
        started = time.perf_counter_ns()
        result = callback()
        samples.append((time.perf_counter_ns() - started) / 1_000_000.0)
    return summarize_ms(samples), result


def ref_for(index: int) -> int:
    return PolygonReference.make(1, 0, index + 1).value


def neighbor_specs(index: int, width: int, height: int) -> list[tuple[int, int]]:
    row, column = divmod(index, width)
    result: list[tuple[int, int]] = []
    if row > 0:
        result.append((index - width, 0))
    if column + 1 < width:
        result.append((index + 1, 1))
    if row + 1 < height:
        result.append((index + width, 2))
    if column > 0:
        result.append((index - 1, 3))
    return result


def build_polygon_map(width: int, height: int, cell_cm: float) -> dict[int, DetourPolygon]:
    polygons: dict[int, DetourPolygon] = {}
    for index in range(width * height):
        row, column = divmod(index, width)
        x = column * cell_cm
        y = row * cell_cm
        links = tuple(
            DetourLink(
                reference=ref_for(target),
                edge=edge,
                next_index=INVALID_LINK,
                side=128,
                bmin=0,
                bmax=0,
            )
            for target, edge in neighbor_specs(index, width, height)
        )
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
            links=links,
            identity_source="benchmark",
        )
    return polygons


def build_adjacency(polygons: dict[int, DetourPolygon]) -> dict[int, tuple[DetourPortal, ...]]:
    adjacency: dict[int, tuple[DetourPortal, ...]] = {}
    for reference, source in polygons.items():
        portals: list[DetourPortal] = []
        for link in source.links:
            portal = _portal_from_link(source, polygons[link.reference], link, tolerance_cm=5.0)
            if portal is None:
                raise AssertionError("benchmark portal unexpectedly unresolved")
            portals.append(portal)
        adjacency[reference] = tuple(portals)
    return adjacency


def build_graph(width: int, height: int, cell_cm: float) -> DetourTopologyGraph:
    polygons = build_polygon_map(width, height, cell_cm)
    adjacency = build_adjacency(polygons)
    return DetourTopologyGraph(
        polygons,
        adjacency,
        snapshot_diagnostics={"benchmark": True, "polygon_count": width * height},
    )


def point_for(index: int, width: int, cell_cm: float) -> Vec3:
    row, column = divmod(index, width)
    return Vec3((column + 0.5) * cell_cm, (row + 0.5) * cell_cm, 0.0)


def resolution_signature(value: Any) -> tuple[Any, ...]:
    closest = value.closest_point
    return (
        value.status,
        value.reason_code,
        value.selected_reference,
        None if closest is None else (closest.x, closest.y, closest.z),
        value.horizontal_distance_m,
        value.vertical_distance_m,
    )


def build_fingerprint_payload(width: int, height: int, cell_cm: float) -> dict[str, Any]:
    polygons: list[dict[str, Any]] = []
    vertices: list[dict[str, float]] = []
    for index in range(width * height):
        row, column = divmod(index, width)
        x = column * cell_cm
        y = row * cell_cm
        base = len(vertices)
        vertices.extend((
            {"x": x, "y": y, "z": 0.0},
            {"x": x + cell_cm, "y": y, "z": 0.0},
            {"x": x + cell_cm, "y": y + cell_cm, "z": 0.0},
            {"x": x, "y": y + cell_cm, "z": 0.0},
        ))
        polygons.append({
            "index": index + 1,
            "first_link": 0,
            "vertex_count": 4,
            "vertex_indices": [base, base + 1, base + 2, base + 3],
            "neighbors": [],
            "flags": 1,
            "area": 0,
            "type": 0,
            "links": [
                {"ref": ref_for(target), "next": INVALID_LINK, "edge": edge, "side": 128, "bmin": 0, "bmax": 0}
                for target, edge in neighbor_specs(index, width, height)
            ],
        })
    return {
        "schema_version": 1,
        "source": "ue4ss_native_detour_geometry_dump",
        "status": "complete",
        "movement_sent": False,
        "coordinate_space": "unreal",
        "coordinate_transform_to_unreal": "identity",
        "tile_bits": 14,
        "poly_bits": 31,
        "tiles": [{
            "tile_index_valid": True,
            "tile_index": 0,
            "salt": 1,
            "x": 0,
            "y": 0,
            "layer": 0,
            "lookup_index": 0,
            "vertices": vertices,
            "polygons": polygons,
            "off_mesh_connections": [],
        }],
    }


def route_case(graph: DetourTopologyGraph, width: int, cell_cm: float, start_index: int, end_index: int) -> dict[str, Any]:
    start = point_for(start_index, width, cell_cm)
    end = point_for(end_index, width, cell_cm)
    start_ref = ref_for(start_index)
    end_ref = ref_for(end_index)
    astar_timing, astar = measure(lambda: graph._astar(start_ref, end_ref, max_expansions=100_000), 9)
    if astar is None:
        raise AssertionError("benchmark route unexpectedly disconnected")
    route_timing, plan = measure(lambda: graph.plan(start, end), 9)
    if plan.status != "complete":
        raise AssertionError(f"benchmark route failed: {plan.reason_code}")
    corridor, _, diagnostics = astar
    return {
        "start_index": start_index,
        "end_index": end_index,
        "corridor_polygons": len(corridor),
        "astar_expansions": diagnostics["expansions"],
        "astar": astar_timing,
        "full_plan": route_timing,
        "waypoint_count": len(plan.waypoints),
    }


def run(width: int, height: int, cell_cm: float) -> dict[str, Any]:
    polygon_count = width * height
    if polygon_count < 100:
        raise ValueError("benchmark topology must contain at least 100 polygons")

    gc.collect()
    graph_build, graph = measure(lambda: build_graph(width, height, cell_cm), 3)

    sample_indices = [
        0,
        width - 1,
        width * (height - 1),
        polygon_count - 1,
        polygon_count // 2,
        width * (height // 3) + width // 3,
        width * (2 * height // 3) + 2 * width // 3,
    ]
    sample_points = [point_for(index, width, cell_cm) for index in sample_indices]
    resolution_timing, resolutions = measure(
        lambda: [graph.resolve_point(point) for point in sample_points],
        25,
    )
    for point, indexed in zip(sample_points, resolutions):
        scanned = graph.resolve_point_full_scan(point)
        if resolution_signature(indexed) != resolution_signature(scanned):
            raise AssertionError("indexed point resolution differs from full scan")

    short_end = min(polygon_count - 1, 6 * width + 8)
    medium_end = min(polygon_count - 1, (height // 2) * width + width // 2)
    long_end = polygon_count - 1
    routing = {
        "short": route_case(graph, width, cell_cm, 0, short_end),
        "medium": route_case(graph, width, cell_cm, 0, medium_end),
        "long": route_case(graph, width, cell_cm, 0, long_end),
    }

    payload = build_fingerprint_payload(width, height, cell_cm)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="ff7r-nav-perf-") as directory:
        path = Path(directory) / "topology.json"
        path.write_bytes(raw)
        file_hash_timing, file_digest = measure(lambda: sha256_file(path), 5)
        raw_read_timing, last_raw = measure(path.read_bytes, 5)
        json_decode_timing, decoded = measure(lambda: json.loads(raw), 5)
        fingerprint_timing, fingerprint = measure(lambda: static_topology_fingerprint(payload), 3)

        def legacy_read_hash_decode() -> tuple[str, Any]:
            digest = sha256_file(path)
            return digest, json.loads(path.read_bytes())

        def single_read_hash_decode() -> tuple[str, Any]:
            value = path.read_bytes()
            return hashlib.sha256(value).hexdigest().upper(), json.loads(value)

        legacy_timing, legacy_result = measure(legacy_read_hash_decode, 5)
        one_pass_timing, one_pass_result = measure(single_read_hash_decode, 5)
        if legacy_result[0] != one_pass_result[0] or legacy_result[1] != one_pass_result[1]:
            raise AssertionError("single-read hash/decode candidate is not equivalent")
        if last_raw != raw or decoded != payload or file_digest != hashlib.sha256(raw).hexdigest().upper():
            raise AssertionError("artifact I/O benchmark changed bytes or decoded payload")

    centroid = graph.polygons[ref_for(0)].centroid
    expected_centroid = Vec3(cell_cm / 2.0, cell_cm / 2.0, 0.0)
    if centroid != expected_centroid:
        raise AssertionError("centroid computation changed geometry")

    return {
        "benchmark_schema": 1,
        "observational_only": True,
        "timing_assertions": False,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "topology": {
            "kind": "deterministic_ground_grid",
            "polygon_count": polygon_count,
            "width": width,
            "height": height,
            "cell_cm": cell_cm,
            "raw_json_bytes": len(raw),
        },
        "phases": {
            "graph_and_spatial_index_construction": graph_build,
            "indexed_point_resolution_7_points": resolution_timing,
            "raw_file_hash": file_hash_timing,
            "raw_file_read": raw_read_timing,
            "json_decode_from_memory": json_decode_timing,
            "static_fingerprint_normalize_serialize_hash": fingerprint_timing,
            "separate_hash_then_read_decode": legacy_timing,
            "single_read_hash_decode": one_pass_timing,
        },
        "routing": routing,
        "equivalence": {
            "indexed_resolution_matches_full_scan": True,
            "single_read_hash_decode_matches_separate_passes": True,
            "centroid_matches_vertex_average": True,
            "routes_complete_and_independently_validated": True,
            "static_fingerprint_sha256": fingerprint,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=200)
    parser.add_argument("--height", type=int, default=150)
    parser.add_argument("--cell-cm", type=float, default=100.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.width, args.height, args.cell_cm)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    print(encoded, end="")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
