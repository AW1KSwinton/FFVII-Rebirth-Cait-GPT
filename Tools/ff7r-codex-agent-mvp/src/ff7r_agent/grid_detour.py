"""Detour around obstacles on a fine grid over the navmesh.

The overlay's detours block whole polygon transitions or offset a waypoint
within the original corridor, which fails where one navmesh polygon spans
several metres of open ground: live at Kalm, a crowd round the Golden Plumes
wagon left Cloud in a pocket that neither could leave
(static_obstacle_local_detour_unproven, global_replan_unavailable).

Here the stretch of route the obstacles touch is rasterised into a band of
30 cm cells; a cell is walkable when it lies on the navmesh and clear of
every obstacle. A* finds a way through, string-pulling straightens it, and
the navmesh planner plans leg by leg through the resulting points so the
joined plan has an ordinary polygon corridor for the validator and follower.
"""
from __future__ import annotations

import heapq
import math
from collections import Counter
from dataclasses import replace
from typing import Any, Iterator, Sequence

from .detour_route import DetourRoutePlan, DetourTopologyGraph, _distance_m
from .models import Vec3
from .offmesh_links import locate_traversal_waypoints, with_waypoint_indices

CELL_CM = 30.0
BAND_CM = 800.0
WIDE_BAND_CM = 1500.0
STRETCH_MARGIN_CM = 600.0
MAX_CELLS = 40_000
LOS_STEP_CM = 10.0
VERTICAL_TOLERANCE_M = 1.5


def _distance(a: Vec3, b: Vec3) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)


def _cumulative(route: Sequence[Vec3]) -> list[float]:
    total, result = 0.0, [0.0]
    for a, b in zip(route, route[1:]):
        total += _distance(a, b)
        result.append(total)
    return result


def _point_at(route: Sequence[Vec3], cumulative: list[float], distance: float) -> Vec3:
    distance = max(0.0, min(cumulative[-1], distance))
    for index in range(1, len(route)):
        if cumulative[index] >= distance:
            span = cumulative[index] - cumulative[index - 1]
            t = 0.0 if span <= 0 else (distance - cumulative[index - 1]) / span
            a, b = route[index - 1], route[index]
            return Vec3(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t)
    return route[-1]


def _project(route: Sequence[Vec3], cumulative: list[float], point: Vec3) -> tuple[float, float, float]:
    """(distance along route, distance from route, route z) of the nearest route point."""
    best = (math.inf, 0.0, route[0].z)
    for index in range(1, len(route)):
        a, b = route[index - 1], route[index]
        dx, dy = b.x - a.x, b.y - a.y
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 <= 0 else max(0.0, min(1.0, ((point.x - a.x) * dx + (point.y - a.y) * dy) / length2))
        x, y = a.x + dx * t, a.y + dy * t
        off = math.hypot(point.x - x, point.y - y)
        if off < best[0]:
            best = (off, cumulative[index - 1] + math.sqrt(length2) * t, a.z + (b.z - a.z) * t)
    return best[1], best[0], best[2]


def join_plans(legs: Sequence[DetourRoutePlan]) -> DetourRoutePlan | None:
    """One plan from consecutive legs, each starting where the last ended."""
    if not legs or any(leg.status != "complete" for leg in legs):
        return None
    corridor = list(legs[0].polygon_corridor)
    portals = list(legs[0].portals[:-1])
    waypoints = list(legs[0].waypoints)
    for index, leg in enumerate(legs[1:], start=1):
        if not leg.polygon_corridor or leg.polygon_corridor[0] != corridor[-1]:
            return None
        corridor.extend(leg.polygon_corridor[1:])
        portals.extend(leg.portals[1:] if index == len(legs) - 1 else leg.portals[1:-1])
        waypoints.extend(leg.waypoints[1:])
    if len(legs) == 1:
        portals.append(legs[0].portals[-1])
    first, last = legs[0], legs[-1]
    validation = dict(last.validation)
    validation.update({
        "corridor_refs_present": all(leg.validation.get("corridor_refs_present") for leg in legs),
        "transition_portals_valid": all(leg.validation.get("transition_portals_valid") for leg in legs)
        and len(portals) == len(corridor) + 1,
        "portal_links_proven": all(leg.validation.get("portal_links_proven") for leg in legs),
        "portal_count": len(portals),
        "waypoint_count": len(waypoints),
        "waypoint_start_matches_resolved": bool(first.validation.get("waypoint_start_matches_resolved")),
        "waypoint_end_matches_resolved": bool(last.validation.get("waypoint_end_matches_resolved")),
        "tile_crossings": sum(int(leg.validation.get("tile_crossings") or 0) for leg in legs),
    })
    # In 3D, as the planner and the validator measure it (live: an 8 m climb
    # at Kalm put a horizontal length 1.1 m short, validator_rejected_route).
    length = sum(_distance_m(a, b) for a, b in zip(waypoints, waypoints[1:]))
    # Each leg is planned on its own, so its off-mesh crossings are its own;
    # the joined plan makes them all, in order.
    crossings = [segment for leg in legs for segment in getattr(leg, "traversal_segments", ())]
    segments = with_waypoint_indices(
        [{**dict(segment), "order": order} for order, segment in enumerate(crossings)], waypoints,
    ) if crossings else ()
    diagnostics = {**dict(first.diagnostics), "joined_legs": len(legs)}
    if segments:
        validation = {**validation, "traversal_count": len(segments), "traversal_links_proven": all(
            leg.validation.get("traversal_links_proven", True) for leg in legs)}
        diagnostics["traversal"] = {
            **dict(first.diagnostics.get("traversal") or {}),
            "count": len(segments),
            "by_class": dict(sorted(Counter(str(item.get("link_class")) for item in segments).items())),
        }
    return replace(
        first,
        requested_end=last.requested_end,
        resolved_end=last.resolved_end,
        end_ref=last.end_ref,
        end_resolution=last.end_resolution,
        polygon_corridor=tuple(corridor),
        portals=tuple(portals),
        waypoints=tuple(waypoints),
        total_estimated_path_length_m=length,
        validation=validation,
        diagnostics=diagnostics,
        traversal_segments=segments,
    )


def plan_via_points(graph: DetourTopologyGraph, base: DetourRoutePlan, via: Sequence[Vec3],
                    **plan_kwargs: Any) -> DetourRoutePlan | None:
    """Plan from the base plan's start, through ``via``, to its end.

    The ends are the base plan's requested points, which the adapter checks
    against Cloud's observed position exactly (the resolved points are
    snapped to the navmesh floor).
    """
    points = [base.requested_start, *via, base.requested_end]
    legs: list[DetourRoutePlan] = []
    previous_end = base.start_resolution
    for index, (a, b) in enumerate(zip(points, points[1:])):
        kwargs = dict(plan_kwargs)
        kwargs["start_resolution_override"] = previous_end
        if index == len(points) - 2:
            kwargs["end_resolution_override"] = base.end_resolution
        leg = graph.plan(a, b, **kwargs)
        if leg.status != "complete":
            return None
        legs.append(leg)
        previous_end = leg.end_resolution
    return join_plans(legs)


class _Grid:
    def __init__(self, graph, route, cumulative, first, last, obstacles, band, clearance):
        self.graph, self.obstacles, self.clearance = graph, obstacles, clearance
        self.route, self.cumulative = route, cumulative
        stretch = [p for p, d in zip(route, cumulative) if first <= d <= last]
        stretch = [_point_at(route, cumulative, first), *stretch, _point_at(route, cumulative, last)]
        self.stretch = stretch
        self.stretch_cumulative = _cumulative(stretch)
        self.band = band
        self.min_x = min(p.x for p in stretch) - band
        self.min_y = min(p.y for p in stretch) - band
        self.nx = int((max(p.x for p in stretch) + band - self.min_x) / CELL_CM) + 1
        self.ny = int((max(p.y for p in stretch) + band - self.min_y) / CELL_CM) + 1
        self.cache: dict[tuple[int, int], float | None] = {}

    def center(self, i: int, j: int) -> tuple[float, float]:
        return self.min_x + (i + 0.5) * CELL_CM, self.min_y + (j + 0.5) * CELL_CM

    def cell(self, x: float, y: float) -> tuple[int, int]:
        return int((x - self.min_x) / CELL_CM), int((y - self.min_y) / CELL_CM)

    def floor(self, i: int, j: int) -> float | None:
        """The navmesh floor z of a walkable cell, else None."""
        key = (i, j)
        if key in self.cache:
            return self.cache[key]
        value = None
        if 0 <= i < self.nx and 0 <= j < self.ny:
            x, y = self.center(i, j)
            _, off, z = _project(self.stretch, self.stretch_cumulative, Vec3(x, y, 0.0))
            if off <= self.band:
                point = Vec3(x, y, z)
                if self.clearance(point):
                    # Only a resolved cell is used; off-mesh cells skip the
                    # full polygon scan where the graph offers that.
                    resolve = getattr(self.graph, "resolve_point_if_resolvable", self.graph.resolve_point)
                    resolution = resolve(point, max_snap_distance_m=0.05,
                                         max_vertical_separation_m=VERTICAL_TOLERANCE_M)
                    if resolution.reason_code == "point_inside_polygon" and resolution.closest_point is not None:
                        value = resolution.closest_point.z
        self.cache[key] = value
        return value

    def nearest_walkable(self, point: Vec3, reach: int = 4) -> tuple[int, int] | None:
        ci, cj = self.cell(point.x, point.y)
        best = None
        for di in range(-reach, reach + 1):
            for dj in range(-reach, reach + 1):
                if self.floor(ci + di, cj + dj) is not None:
                    distance = di * di + dj * dj
                    if best is None or distance < best[0]:
                        best = (distance, (ci + di, cj + dj))
        return best[1] if best else None

    def line_clear(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        (ax, ay), (bx, by) = self.center(*a), self.center(*b)
        steps = max(1, int(math.hypot(bx - ax, by - ay) / LOS_STEP_CM))
        for step in range(steps + 1):
            t = step / steps
            if self.floor(*self.cell(ax + (bx - ax) * t, ay + (by - ay) * t)) is None:
                return False
        return True

    def search(self, start: tuple[int, int], goal: tuple[int, int]) -> list[tuple[int, int]] | None:
        if self.nx * self.ny > MAX_CELLS * 4:
            return None
        open_heap = [(0.0, 0.0, start)]
        came: dict[tuple[int, int], tuple[int, int]] = {}
        cost = {start: 0.0}
        expanded = 0
        while open_heap:
            _, g, current = heapq.heappop(open_heap)
            if current == goal:
                path = [current]
                while current in came:
                    current = came[current]
                    path.append(current)
                return path[::-1]
            if g > cost.get(current, math.inf):
                continue
            expanded += 1
            if expanded > MAX_CELLS:
                return None
            i, j = current
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                nxt = (i + di, j + dj)
                if self.floor(*nxt) is None:
                    continue
                if di and dj and (self.floor(i + di, j) is None or self.floor(i, j + dj) is None):
                    continue  # no corner cutting
                step = math.sqrt(2.0) if di and dj else 1.0
                new_cost = g + step
                if new_cost < cost.get(nxt, math.inf):
                    cost[nxt] = new_cost
                    came[nxt] = current
                    heuristic = math.hypot(goal[0] - nxt[0], goal[1] - nxt[1])
                    heapq.heappush(open_heap, (new_cost + heuristic, new_cost, nxt))
        return None

    def smooth(self, path: list[tuple[int, int]]) -> list[tuple[int, int]]:
        result, index = [path[0]], 0
        while index < len(path) - 1:
            furthest = index + 1
            for candidate in range(len(path) - 1, index, -1):
                if self.line_clear(path[index], path[candidate]):
                    furthest = candidate
                    break
            result.append(path[furthest])
            index = furthest
        return result


MIN_VIA_SPACING_CM = 40.0
DENSE_VIA_CELLS = 4


def _without_close_points(via: list[Vec3], base: DetourRoutePlan) -> list[Vec3]:
    """Drop via points bunched against each other or the route's ends, which
    otherwise leave centimetre-long legs for the follower."""
    start = base.resolved_start or base.requested_start
    end = base.resolved_end or base.requested_end
    kept: list[Vec3] = []
    for point in via:
        previous = kept[-1] if kept else start
        if _distance(previous, point) >= MIN_VIA_SPACING_CM and _distance(point, end) >= MIN_VIA_SPACING_CM:
            kept.append(point)
    return kept


def _stretch_vias(graph: DetourTopologyGraph, route: list[Vec3], cumulative: list[float], first: float,
                  last: float, obstacles: Sequence[Any], clear) -> Iterator[list[Vec3]]:
    """Candidate via points through the grid over the route from ``first``
    to ``last`` (cm along it): per band, the smoothed path, then dense."""
    for band in (BAND_CM, WIDE_BAND_CM):
        grid = _Grid(graph, route, cumulative, first, last, obstacles, band, clear)
        a = _point_at(route, cumulative, first)
        b = _point_at(route, cumulative, last)
        start, goal = grid.nearest_walkable(a), grid.nearest_walkable(b)
        if start is None or goal is None:
            continue
        path = grid.search(start, goal)
        if path is None:
            continue
        # The smoothed path first. The planner string-pulls each leg through
        # portals inset from their corners, so a long leg can bend back into
        # an obstacle (live: Kalm, portal_corner_inset_m 0.5); points every
        # DENSE_VIA_CELLS along the raw path then keep the legs on the grid.
        for cells in (grid.smooth(path), path[::DENSE_VIA_CELLS] + path[-1:]):
            via = []
            for i, j in cells:
                x, y = grid.center(i, j)
                via.append(Vec3(x, y, grid.floor(i, j)))
            # Rejoin the route at its own points where the grid ends are close.
            if first > 0.0:
                via.insert(0, a)
            if last < cumulative[-1]:
                via.append(b)
            yield via


def _stretches(spans: list[tuple[float, float]], total: float) -> list[tuple[float, float]]:
    """The stretches of route to grid: each group of obstacles whose margins
    meet, not one band from the first obstacle to the last."""
    stretches: list[tuple[float, float]] = []
    for low, high in sorted(spans):
        low, high = max(0.0, low - STRETCH_MARGIN_CM), min(total, high + STRETCH_MARGIN_CM)
        if stretches and low <= stretches[-1][1]:
            stretches[-1] = (stretches[-1][0], max(stretches[-1][1], high))
        else:
            stretches.append((low, high))
    return stretches


def _split_at_crossings(stretches: list[tuple[float, float]],
                        crossings: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """The stretches with each off-mesh crossing's leg cut out: the grid is
    laid over navmesh floor, and there is none between a link's two ends."""
    for start, end in crossings:
        pieces: list[tuple[float, float]] = []
        for low, high in stretches:
            if high <= start or low >= end:
                pieces.append((low, high))
                continue
            if low < start:
                pieces.append((low, start))
            if high > end:
                pieces.append((end, high))
        stretches = pieces
    return [(low, high) for low, high in stretches if high - low > 1.0]


def grid_detour(graph: DetourTopologyGraph, base: DetourRoutePlan, obstacles: Sequence[Any],
                *, clearance_m, route_hits, plan_kwargs: dict[str, Any]) -> DetourRoutePlan | None:
    """A plan around ``obstacles`` through a grid over the navmesh, or None.

    ``clearance_m(point, obstacle)`` is the obstacle's clearance (negative
    inside); ``route_hits(waypoints, obstacle)`` lists hit segments.

    Obstacles far apart along the route are gridded separately and the
    route kept between them: live 2026-10-06 at Cosmo Canyon, a tent at the
    start and two contacts by Ka'dina 130 m on, up a winding climb, made
    one band from the first to the last that no search could cross, though
    each alone was passed.
    """
    route = list(base.waypoints)
    if len(route) < 2 or not callable(getattr(graph, "resolve_point", None)):
        return None
    blocking = [o for o in obstacles if route_hits(route, o)]
    if not blocking:
        return None
    cumulative = _cumulative(route)
    spans = []
    for obstacle in blocking:
        along, _, _ = _project(route, cumulative, obstacle.center)
        reach = (obstacle.effective_radius_m + getattr(obstacle, "half_length_m", 0.0)) * 100.0
        spans.append((along - reach, along + reach))

    def clear(point: Vec3) -> bool:
        return all(clearance_m(point, obstacle) > 0.0 for obstacle in obstacles)

    def plan_through(via: list[Vec3]) -> DetourRoutePlan | None:
        plan = plan_via_points(graph, base, _without_close_points(via, base), **plan_kwargs)
        if plan is not None and not any(route_hits(plan.waypoints, o) for o in obstacles):
            return plan
        return None

    stretches = _stretches(spans, cumulative[-1])
    if getattr(base, "traversal_segments", ()):
        try:
            located = locate_traversal_waypoints(route, base.traversal_segments)
        except ValueError:
            return None
        stretches = _split_at_crossings(
            stretches, [(cumulative[index], cumulative[index + 1]) for _, index in located],
        )
        if not stretches:
            return None
    if len(stretches) == 1:
        first, last = stretches[0]
        for via in _stretch_vias(graph, route, cumulative, first, last, obstacles, clear):
            plan = plan_through(via)
            if plan is not None:
                return plan
        return None
    options = [list(_stretch_vias(graph, route, cumulative, first, last, obstacles, clear))
               for first, last in stretches]
    if not all(options):
        return None
    for choice in range(max(len(candidates) for candidates in options)):
        via = [point for candidates in options for point in candidates[min(choice, len(candidates) - 1)]]
        plan = plan_through(via)
        if plan is not None:
            return plan
    return None
