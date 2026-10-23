#!/usr/bin/env python3
"""Offline sensitivity benchmark for traversal-time-aware shadow routing.

The committed August Detour artifact is a partial connected snapshot and cannot
reconstruct the later Chadley route end-to-end.  This benchmark therefore does
both of the things the repository evidence can support honestly:

1. attempt the archived Chadley endpoints and record the provenance limitation;
2. find real diverge/reconverge route choices inside the committed FFVII graph
   and compare shortest-distance A* with the provisional time-aware search.

No movement is sent and no wall-clock timing is asserted in CI.
"""
from __future__ import annotations

import argparse
from collections import deque
import hashlib
from itertools import combinations
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

from ff7r_agent.detour_route import DetourTopologyGraph, load_shipping_detour_topology
from ff7r_agent.models import Vec3
from ff7r_agent.time_aware_route import (
    TraversalTimeModel,
    evaluate_corridor_time,
    search_time_aware_corridor,
)

ROOT = Path(__file__).parents[3]
TOPOLOGY = (
    ROOT
    / "ProjectKnowledge"
    / "Evidence"
    / "2026-08-17-shipping-detour-topology-live"
    / "navigation-native-geometry.json"
)
CONTINUOUS_EVIDENCE = (
    ROOT
    / "ProjectKnowledge"
    / "Evidence"
    / "2026-09-09-navigation-validation"
    / "continuous-control-passed.json"
)
SPRINT_EVIDENCE = (
    ROOT
    / "ProjectKnowledge"
    / "Evidence"
    / "2026-09-09-navigation-validation"
    / "sprint-control-passed.json"
)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _timing_summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "median_ms": round(statistics.median(values), 5),
        "p95_ms": round(_percentile(values, 0.95), 5),
    }


def _route_id(corridor: tuple[int, ...]) -> str:
    value = ",".join(str(reference) for reference in corridor).encode("ascii")
    return hashlib.sha256(value).hexdigest()[:16]


def _load_archived_inputs() -> tuple[dict[str, Any], dict[str, Any], Vec3, Vec3]:
    continuous = json.loads(CONTINUOUS_EVIDENCE.read_text(encoding="utf-8"))
    sprint = json.loads(SPRINT_EVIDENCE.read_text(encoding="utf-8"))
    start = Vec3.from_dict(continuous["start_feet_position"])
    end = Vec3.from_dict(continuous["objective"]["target_position"])
    return continuous, sprint, start, end


def _sensitivity_models(sprint: dict[str, Any]) -> list[tuple[str, TraversalTimeModel]]:
    measured_peak_speed = float(sprint["maximum_observed_horizontal_speed_m_s"])
    attempts = sprint.get("attempts") or []
    successful = next((attempt for attempt in attempts if attempt.get("state") == "ARRIVED"), {})
    completed_waits = int(successful.get("completed_braking_waits", 0))
    completed_wait_seconds = float(successful.get("completed_braking_wait_seconds", 0.0))
    mean_braking_wait = completed_wait_seconds / completed_waits if completed_waits else 0.9

    # These are intentionally sensitivity parameters, not calibration.  The
    # archived braking waits were not controlled corner experiments.
    central_turn_penalty = mean_braking_wait / (math.pi / 2.0)
    speeds = (
        ("half_peak", measured_peak_speed * 0.5),
        ("three_quarter_peak", measured_peak_speed * 0.75),
        ("measured_peak", measured_peak_speed),
    )
    turn_scales = (0.25, 0.5, 1.0, 2.0)
    models: list[tuple[str, TraversalTimeModel]] = []
    for speed_name, speed in speeds:
        for turn_scale in turn_scales:
            turn_penalty = central_turn_penalty * turn_scale
            models.append((
                f"turn_only/{speed_name}/x{turn_scale:g}",
                TraversalTimeModel(
                    straight_speed_m_s=speed,
                    turn_penalty_s_per_rad=turn_penalty,
                    narrow_portal_threshold_m=0.0,
                    narrow_portal_penalty_s_per_m=0.0,
                ),
            ))
            models.append((
                f"turn_plus_clearance/{speed_name}/x{turn_scale:g}",
                TraversalTimeModel(
                    straight_speed_m_s=speed,
                    turn_penalty_s_per_rad=turn_penalty,
                    narrow_portal_threshold_m=1.5,
                    narrow_portal_penalty_s_per_m=mean_braking_wait * 0.5,
                ),
            ))
    return models


def _reachable_depths(
    graph: DetourTopologyGraph,
    origin: int,
    *,
    max_depth: int,
) -> dict[int, int]:
    depths = {origin: 0}
    queue: deque[int] = deque((origin,))
    while queue:
        current = queue.popleft()
        depth = depths[current]
        if depth >= max_depth:
            continue
        for portal in graph.adjacency.get(current, ()):
            target = int(portal.to_reference)
            if target not in graph.polygons or target in depths:
                continue
            depths[target] = depth + 1
            queue.append(target)
    return depths


def _find_real_alternative_pairs(
    graph: DetourTopologyGraph,
    *,
    limit: int = 8,
    max_depth: int = 28,
) -> list[tuple[int, int]]:
    """Find deterministic branch/reconvergence pairs in the real graph."""
    selected: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for start in sorted(graph.polygons):
        neighbours = sorted({
            int(portal.to_reference)
            for portal in graph.adjacency.get(start, ())
            if int(portal.to_reference) in graph.polygons and int(portal.to_reference) != start
        })
        if len(neighbours) < 2:
            continue
        reach = {
            neighbour: _reachable_depths(graph, neighbour, max_depth=max_depth)
            for neighbour in neighbours[:4]
        }
        for first, second in combinations(neighbours[:4], 2):
            common = set(reach[first]).intersection(reach[second])
            candidates = []
            for target in common:
                if target == start:
                    continue
                first_depth = reach[first][target] + 1
                second_depth = reach[second][target] + 1
                shortest_branch = min(first_depth, second_depth)
                longest_branch = max(first_depth, second_depth)
                if shortest_branch < 4 or longest_branch > max_depth:
                    continue
                candidates.append((
                    abs(first_depth - second_depth),
                    -shortest_branch,
                    longest_branch,
                    target,
                ))
            if not candidates:
                continue
            target = min(candidates)[3]
            pair = (start, target)
            if pair in seen:
                continue
            baseline = graph._astar(start, target, max_expansions=50_000)
            if baseline is None:
                continue
            corridor = baseline[0]
            if len(corridor) < 5 or len(corridor) > 90:
                continue
            seen.add(pair)
            selected.append(pair)
            if len(selected) >= limit:
                return selected
    return selected


def _evaluate_real_pair(
    graph: DetourTopologyGraph,
    start_reference: int,
    end_reference: int,
    model_items: list[tuple[str, TraversalTimeModel]],
) -> tuple[dict[str, Any], list[float]]:
    baseline_started = time.perf_counter_ns()
    baseline = graph._astar(start_reference, end_reference, max_expansions=50_000)
    baseline_ms = (time.perf_counter_ns() - baseline_started) / 1_000_000.0
    if baseline is None:
        raise RuntimeError("selected real pair lost its baseline route")
    baseline_corridor, baseline_transitions, baseline_diagnostics = baseline

    changed = 0
    longer_but_lower = 0
    search_times: list[float] = []
    examples: list[dict[str, Any]] = []
    route_ids = {_route_id(baseline_corridor)}
    for name, model in model_items:
        baseline_model = evaluate_corridor_time(
            graph, baseline_corridor, baseline_transitions, model=model,
        )
        started = time.perf_counter_ns()
        shadow = search_time_aware_corridor(
            graph,
            start_reference,
            end_reference,
            model=model,
            max_expansions=50_000,
        )
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        search_times.append(elapsed_ms)
        if not shadow.ok:
            continue
        # Every returned transition must still be one of the validated graph's
        # existing portals.  This is benchmarked in addition to unit coverage.
        if any(
            portal not in graph.adjacency.get(first, ())
            for first, portal in zip(shadow.polygon_corridor, shadow.transitions)
        ):
            raise RuntimeError("shadow route introduced an unvalidated portal")
        route_ids.add(_route_id(shadow.polygon_corridor))
        route_changed = shadow.polygon_corridor != baseline_corridor
        is_longer = bool(
            shadow.graph_distance_m is not None
            and baseline_model.graph_distance_m is not None
            and shadow.graph_distance_m > baseline_model.graph_distance_m + 1e-9
        )
        lower_proxy = bool(
            shadow.estimated_seconds is not None
            and baseline_model.estimated_seconds is not None
            and shadow.estimated_seconds < baseline_model.estimated_seconds - 1e-9
        )
        changed += int(route_changed)
        longer_but_lower += int(route_changed and is_longer and lower_proxy)
        if route_changed:
            examples.append({
                "profile": name,
                "route_id": _route_id(shadow.polygon_corridor),
                "longer_graph_distance": is_longer,
                "lower_proxy_time": lower_proxy,
                "baseline_graph_m": round(float(baseline_model.graph_distance_m), 6),
                "shadow_graph_m": round(float(shadow.graph_distance_m), 6),
                "baseline_proxy_seconds": round(float(baseline_model.estimated_seconds), 6),
                "shadow_proxy_seconds": round(float(shadow.estimated_seconds), 6),
                "proxy_seconds_saved": round(
                    float(baseline_model.estimated_seconds - shadow.estimated_seconds), 6
                ),
                "baseline_total_turn_degrees": round(
                    math.degrees(float(baseline_model.total_turn_radians)), 3
                ),
                "shadow_total_turn_degrees": round(
                    math.degrees(float(shadow.total_turn_radians)), 3
                ),
                "baseline_max_turn_degrees": round(float(baseline_model.max_turn_degrees), 3),
                "shadow_max_turn_degrees": round(float(shadow.max_turn_degrees), 3),
                "shadow_expansions": shadow.expansions,
                "search_ms": round(elapsed_ms, 5),
            })

    examples.sort(key=lambda item: item["proxy_seconds_saved"], reverse=True)
    return ({
        "start_reference": start_reference,
        "end_reference": end_reference,
        "baseline_route_id": _route_id(baseline_corridor),
        "baseline_corridor_polygons": len(baseline_corridor),
        "baseline_graph_cost_m": round(float(baseline_diagnostics["graph_cost_m"]), 6),
        "baseline_expansions": int(baseline_diagnostics["expansions"]),
        "baseline_search_ms": round(baseline_ms, 5),
        "profile_count": len(model_items),
        "changed_profile_count": changed,
        "longer_but_lower_proxy_time_count": longer_but_lower,
        "distinct_route_families": len(route_ids),
        "strongest_changed_examples": examples[:4],
    }, search_times)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    continuous, sprint, chadley_start, chadley_end = _load_archived_inputs()
    load_started = time.perf_counter_ns()
    graph = load_shipping_detour_topology(TOPOLOGY)
    graph_load_ms = (time.perf_counter_ns() - load_started) / 1_000_000.0

    chadley_attempt = graph.plan(chadley_start, chadley_end, max_expansions=100_000)
    chadley_exact_replay_available = chadley_attempt.status == "complete"
    chadley = {
        "exact_replay_available": chadley_exact_replay_available,
        "replay_status": chadley_attempt.status,
        "replay_reason_code": chadley_attempt.reason_code,
        "archived_elapsed_seconds": continuous["result"]["elapsed_seconds"],
        "archived_planned_distance_m": continuous["result"]["metrics"]["planned_distance_m"],
        "route_attempts": continuous["result"]["metrics"]["route_attempts"],
        "recovery_attempts": continuous["objective"]["recovery_attempts"],
        "sprint_peak_speed_m_s": sprint["maximum_observed_horizontal_speed_m_s"],
        "provenance_note": (
            "The committed August navigation-native-geometry artifact is a partial "
            "topology snapshot and does not contain a linked corridor between the "
            "later September Chadley endpoints; exact Chadley alternative replanning "
            "therefore requires the original full admitted topology or a live capture."
            if not chadley_exact_replay_available else None
        ),
    }

    model_items = _sensitivity_models(sprint)
    pairs = _find_real_alternative_pairs(graph)
    pair_results: list[dict[str, Any]] = []
    shadow_times: list[float] = []
    for start_reference, end_reference in pairs:
        pair_result, pair_times = _evaluate_real_pair(
            graph, start_reference, end_reference, model_items,
        )
        pair_results.append(pair_result)
        shadow_times.extend(pair_times)

    changed_profiles = sum(item["changed_profile_count"] for item in pair_results)
    longer_lower = sum(item["longer_but_lower_proxy_time_count"] for item in pair_results)
    pairs_with_change = sum(item["changed_profile_count"] > 0 for item in pair_results)
    pairs_with_longer_lower = sum(
        item["longer_but_lower_proxy_time_count"] > 0 for item in pair_results
    )
    strongest = [
        {"pair_index": index, **example}
        for index, item in enumerate(pair_results)
        for example in item["strongest_changed_examples"]
        if example["longer_graph_distance"] and example["lower_proxy_time"]
    ]
    strongest.sort(key=lambda item: item["proxy_seconds_saved"], reverse=True)

    results = {
        "benchmark_schema": 2,
        "observational_only": True,
        "authoritative_route_selection": False,
        "movement_sent": False,
        "timing_assertions": False,
        "topology": {
            "path": str(TOPOLOGY.relative_to(ROOT)),
            "polygon_count": len(graph.polygons),
            "directed_portal_count": sum(len(value) for value in graph.adjacency.values()),
            "graph_load_ms": round(graph_load_ms, 5),
        },
        "archived_chadley": chadley,
        "real_topology_experiment": {
            "pair_count": len(pair_results),
            "sensitivity_profiles_per_pair": len(model_items),
            "comparison_count": len(pair_results) * len(model_items),
            "changed_profile_count": changed_profiles,
            "longer_but_lower_proxy_time_count": longer_lower,
            "pairs_with_any_route_change": pairs_with_change,
            "pairs_with_longer_lower_proxy_route": pairs_with_longer_lower,
            "shadow_search_timing": _timing_summary(shadow_times),
            "strongest_longer_lower_proxy_examples": strongest[:8],
            "pairs": pair_results,
        },
        "interpretation": {
            "architecture_supported": longer_lower > 0,
            "live_calibration_still_required": True,
            "reason": (
                "Real validated FFVII graph alternatives include cases where the provisional "
                "time model selects a longer route with lower modeled traversal time."
                if longer_lower > 0
                else "The synthetic proof passes, but this partial real topology did not expose a "
                "longer/lower-proxy alternative under the tested sensitivity range."
            ),
        },
        "limitations": [
            "Turn and clearance coefficients are sensitivity parameters, not live-calibrated corner-speed measurements.",
            "The measured sprint peak and archived braking waits set scale only; braking events were not controlled corner experiments.",
            "The committed topology is partial and cannot exactly replay the later Chadley route.",
            "The shadow planner ranks only already-proven Detour portals and is not wired to live movement.",
        ],
    }
    text = json.dumps(results, indent=2, sort_keys=True)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
