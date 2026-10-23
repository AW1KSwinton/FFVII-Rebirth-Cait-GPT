#!/usr/bin/env python3
"""Find real FFVII topology cases where a longer route removes curvature.

This is an offline structural experiment. It first discovers genuine alternate
corridors by blocking one edge of a shortest-distance route, then computes the
turn-penalty break-even point analytically and verifies that the unblocked shadow
planner changes route above that point. No live-control claim is made.
"""
from __future__ import annotations

import argparse
from collections import deque
from itertools import combinations
import json
import math
from pathlib import Path
from typing import Any

from ff7r_agent.detour_route import DetourTopologyGraph, load_shipping_detour_topology
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
SPRINT_EVIDENCE = (
    ROOT
    / "ProjectKnowledge"
    / "Evidence"
    / "2026-09-09-navigation-validation"
    / "sprint-control-passed.json"
)
PROBE_MODEL = TraversalTimeModel(
    straight_speed_m_s=1.0,
    turn_penalty_s_per_rad=1.0,
    narrow_portal_threshold_m=0.0,
    narrow_portal_penalty_s_per_m=0.0,
)


def _reachable_depths(graph: DetourTopologyGraph, origin: int, max_depth: int) -> dict[int, int]:
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


def _evenly_spaced(values: list[int], count: int) -> list[int]:
    if len(values) <= count:
        return values
    if count <= 1:
        return values[:1]
    indices = {
        round(index * (len(values) - 1) / (count - 1))
        for index in range(count)
    }
    return [values[index] for index in sorted(indices)]


def _candidate_pairs(
    graph: DetourTopologyGraph,
    *,
    branch_sample_count: int = 120,
    max_depth: int = 32,
) -> list[tuple[int, int]]:
    branches = [
        reference
        for reference in sorted(graph.polygons)
        if len({
            int(portal.to_reference)
            for portal in graph.adjacency.get(reference, ())
            if int(portal.to_reference) in graph.polygons
        }) >= 2
    ]
    pairs: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for start in _evenly_spaced(branches, branch_sample_count):
        neighbours = sorted({
            int(portal.to_reference)
            for portal in graph.adjacency.get(start, ())
            if int(portal.to_reference) in graph.polygons and int(portal.to_reference) != start
        })[:4]
        reach = {
            neighbour: _reachable_depths(graph, neighbour, max_depth)
            for neighbour in neighbours
        }
        local: list[tuple[int, int, int, int]] = []
        for first, second in combinations(neighbours, 2):
            for target in set(reach[first]).intersection(reach[second]):
                if target == start:
                    continue
                d1 = reach[first][target] + 1
                d2 = reach[second][target] + 1
                if min(d1, d2) < 5 or max(d1, d2) > max_depth:
                    continue
                local.append((abs(d1 - d2), -min(d1, d2), max(d1, d2), target))
        for _, _, _, target in sorted(local)[:2]:
            pair = (start, target)
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    return pairs


def _sample_portals(transitions: tuple[Any, ...], max_samples: int = 10) -> list[Any]:
    if len(transitions) <= max_samples:
        return list(transitions)
    indices = _evenly_spaced(list(range(len(transitions))), max_samples)
    return [transitions[index] for index in indices]


def _route_eval(
    graph: DetourTopologyGraph,
    corridor: tuple[int, ...],
    transitions: tuple[Any, ...],
):
    result = evaluate_corridor_time(graph, corridor, transitions, model=PROBE_MODEL)
    if not result.ok:
        raise RuntimeError(result.reason_code)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    graph = load_shipping_detour_topology(TOPOLOGY)
    sprint = json.loads(SPRINT_EVIDENCE.read_text(encoding="utf-8"))
    peak_speed = float(sprint["maximum_observed_horizontal_speed_m_s"])
    pairs = _candidate_pairs(graph)

    tradeoffs: list[dict[str, Any]] = []
    seen_tradeoff_routes: set[tuple[int, int, tuple[int, ...]]] = set()
    alternate_routes_checked = 0
    duplicate_tradeoff_discoveries = 0
    for start_reference, end_reference in pairs:
        baseline = graph._astar(start_reference, end_reference, max_expansions=50_000)
        if baseline is None:
            continue
        baseline_corridor, baseline_transitions, _ = baseline
        if len(baseline_corridor) < 6 or len(baseline_corridor) > 100:
            continue
        baseline_eval = _route_eval(graph, baseline_corridor, baseline_transitions)
        for blocked_portal in _sample_portals(baseline_transitions):
            alternate = graph._astar(
                start_reference,
                end_reference,
                max_expansions=50_000,
                blocked_transitions={(blocked_portal.from_reference, blocked_portal.to_reference)},
            )
            if alternate is None:
                continue
            alternate_routes_checked += 1
            alt_corridor, alt_transitions, _ = alternate
            if alt_corridor == baseline_corridor:
                continue
            alt_eval = _route_eval(graph, alt_corridor, alt_transitions)
            distance_overhead_m = float(alt_eval.graph_distance_m - baseline_eval.graph_distance_m)
            turn_saved_rad = float(baseline_eval.total_turn_radians - alt_eval.total_turn_radians)
            if distance_overhead_m <= 0.05 or turn_saved_rad <= math.radians(2.0):
                continue
            if float(alt_eval.graph_distance_m) > float(baseline_eval.graph_distance_m) * 1.75:
                continue

            tradeoff_key = (start_reference, end_reference, tuple(alt_corridor))
            if tradeoff_key in seen_tradeoff_routes:
                duplicate_tradeoff_discoveries += 1
                continue
            seen_tradeoff_routes.add(tradeoff_key)

            break_even_peak_s_per_rad = (
                distance_overhead_m / peak_speed / turn_saved_rad
            )
            break_even_half_peak_s_per_rad = (
                distance_overhead_m / (peak_speed * 0.5) / turn_saved_rad
            )
            verification_model = TraversalTimeModel(
                straight_speed_m_s=peak_speed,
                turn_penalty_s_per_rad=break_even_peak_s_per_rad * 1.25,
                narrow_portal_threshold_m=0.0,
                narrow_portal_penalty_s_per_m=0.0,
            )
            shadow = search_time_aware_corridor(
                graph,
                start_reference,
                end_reference,
                model=verification_model,
                max_expansions=50_000,
            )
            baseline_at_verify = evaluate_corridor_time(
                graph, baseline_corridor, baseline_transitions, model=verification_model,
            )
            route_changed = shadow.ok and shadow.polygon_corridor != baseline_corridor
            lower_proxy_time = bool(
                shadow.ok
                and shadow.estimated_seconds is not None
                and baseline_at_verify.estimated_seconds is not None
                and shadow.estimated_seconds < baseline_at_verify.estimated_seconds - 1e-9
            )
            if shadow.ok and any(
                portal not in graph.adjacency.get(first, ())
                for first, portal in zip(shadow.polygon_corridor, shadow.transitions)
            ):
                raise RuntimeError("shadow verification introduced an unvalidated portal")

            tradeoffs.append({
                "start_reference": start_reference,
                "end_reference": end_reference,
                "baseline_polygons": len(baseline_corridor),
                "alternate_polygons": len(alt_corridor),
                "baseline_graph_m": round(float(baseline_eval.graph_distance_m), 6),
                "alternate_graph_m": round(float(alt_eval.graph_distance_m), 6),
                "distance_overhead_m": round(distance_overhead_m, 6),
                "distance_overhead_percent": round(
                    100.0 * distance_overhead_m / float(baseline_eval.graph_distance_m), 3
                ),
                "baseline_total_turn_degrees": round(
                    math.degrees(float(baseline_eval.total_turn_radians)), 3
                ),
                "alternate_total_turn_degrees": round(
                    math.degrees(float(alt_eval.total_turn_radians)), 3
                ),
                "turn_saved_degrees": round(math.degrees(turn_saved_rad), 3),
                "baseline_max_turn_degrees": round(float(baseline_eval.max_turn_degrees), 3),
                "alternate_max_turn_degrees": round(float(alt_eval.max_turn_degrees), 3),
                "break_even_turn_penalty_s_per_rad_at_peak": round(break_even_peak_s_per_rad, 6),
                "break_even_turn_penalty_s_per_90deg_at_peak": round(
                    break_even_peak_s_per_rad * math.pi / 2.0, 6
                ),
                "break_even_turn_penalty_s_per_90deg_at_half_peak": round(
                    break_even_half_peak_s_per_rad * math.pi / 2.0, 6
                ),
                "shadow_changes_route_above_break_even": route_changed,
                "shadow_has_lower_proxy_time_above_break_even": lower_proxy_time,
                "shadow_graph_m_above_break_even": (
                    round(float(shadow.graph_distance_m), 6) if shadow.ok else None
                ),
                "shadow_total_turn_degrees_above_break_even": (
                    round(math.degrees(float(shadow.total_turn_radians)), 3) if shadow.ok else None
                ),
                "shadow_expansions_above_break_even": shadow.expansions,
            })

    tradeoffs.sort(key=lambda item: (
        item["break_even_turn_penalty_s_per_rad_at_peak"],
        -item["turn_saved_degrees"],
    ))
    verified = [
        item for item in tradeoffs
        if item["shadow_changes_route_above_break_even"]
        and item["shadow_has_lower_proxy_time_above_break_even"]
    ]
    result = {
        "benchmark_schema": 2,
        "observational_only": True,
        "authoritative_route_selection": False,
        "movement_sent": False,
        "topology_polygon_count": len(graph.polygons),
        "candidate_pair_count": len(pairs),
        "alternate_routes_checked": alternate_routes_checked,
        "duplicate_tradeoff_discoveries_removed": duplicate_tradeoff_discoveries,
        "unique_longer_smoother_tradeoff_count": len(tradeoffs),
        "verified_shadow_route_change_count": len(verified),
        "measured_sprint_peak_speed_m_s": peak_speed,
        "best_tradeoffs": tradeoffs[:12],
        "interpretation": (
            "The real partial FFVII topology contains longer corridors with less curvature, "
            "and the unblocked shadow planner changes route when the turn penalty crosses "
            "their computed break-even point."
            if verified else
            "No real longer/smoother tradeoff was verified in the committed partial topology."
        ),
        "limitations": [
            "Break-even values are model thresholds, not measured corner-time calibration.",
            "Alternate corridors are discovered offline by blocking one baseline edge; the verification search itself is unblocked.",
            "The committed topology is partial and cannot reconstruct the later Chadley route.",
        ],
    }
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
