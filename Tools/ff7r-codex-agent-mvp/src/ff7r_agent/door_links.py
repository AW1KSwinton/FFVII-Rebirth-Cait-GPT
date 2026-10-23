"""Doors that join navmesh islands, found by sweeping the gaps between them.

A knob door cuts the navmesh (live at Nibelheim: the house Dale stands in
was a stairway island and a room island, joined to the street and to each
other only across 0-0.8 m gaps with BG0012/BG0002_00_Door1Knob_Standard in
them), so the planner finds no corridor. When Cloud and the NPC are on
different islands, the gaps round the smaller island are swept with Cloud's
capsule; a gap whose sweep hits a door gimmick is a door link. Islands are
chained that way until the other end is reached. Each link names a point in
front of the door on either side, so an approach can route to the door, open
it at its prompt and route on from the far side.

Read-only: it plans on the navmesh and sweeps collision; it sends no input.
"""
from __future__ import annotations

import json
import math
import os
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable

from .detour_route import DetourTopologyGraph
from .models import Vec3

# A doorway gap: Cloud's capsule is kept 34 cm off the door on each side.
MAX_GAP_CM = 150.0
MAX_GAP_RISE_CM = 60.0
# Swept across each gap from this far either side of its middle.
GAP_SWEEP_REACH_CM = 90.0
# Candidate gaps per neighbouring island, at least this far apart, so a wall
# between two floors does not hide the doorway next to it.
GAPS_PER_NEIGHBOUR = 4
GAP_SPACING_CM = 150.0
# Islands this big are the town itself: reached, never searched round.
LARGE_ISLAND_POLYGONS = 1500
MAX_DOORS = 4
# A gap sweep with any of these in it is no way through.
GAP_BLOCKING_KINDS = frozenset({"fixed", "vehicle", "ledge", "drop"})
# A point in front of a door, on the navmesh of its side.
FRONT_SNAP_M = 1.5
FRONT_VERTICAL_M = 1.0


def islands(graph: DetourTopologyGraph) -> dict[int, int]:
    """Polygon reference -> island label (the component's first reference)."""
    cached = getattr(graph, "_door_link_islands", None)
    if cached is not None:
        return cached
    label: dict[int, int] = {}
    for seed in graph.polygons:
        if seed in label:
            continue
        label[seed] = seed
        queue = deque([seed])
        while queue:
            current = queue.popleft()
            for portal in graph.adjacency.get(current, ()):
                other = portal.to_reference
                if other in graph.polygons and other not in label:
                    label[other] = seed
                    queue.append(other)
    try:
        graph._door_link_islands = label
    except AttributeError:
        pass
    return label


def _members(label: dict[int, int], island: int) -> list[int]:
    return [reference for reference, owner in label.items() if owner == island]


def _gaps(graph: DetourTopologyGraph, label: dict[int, int], island: int) -> dict[int, list[tuple[float, Vec3, Vec3, int, int]]]:
    """Short gaps from ``island`` to each neighbouring island, closest first."""
    found: dict[int, list[tuple[float, Vec3, Vec3, int, int]]] = {}
    for reference in _members(label, island):
        polygon = graph.polygons[reference]
        for vertex in polygon.vertices:
            for other in graph._point_resolution_references(vertex, MAX_GAP_CM / 100.0):
                neighbour = label.get(other)
                if neighbour is None or neighbour == island:
                    continue
                _, closest, horizontal_cm, vertical_cm, _ = graph._candidate_resolution(vertex, graph.polygons[other])
                if horizontal_cm <= MAX_GAP_CM and vertical_cm <= MAX_GAP_RISE_CM:
                    found.setdefault(neighbour, []).append((horizontal_cm, vertex, closest, reference, other))
    result = {}
    for neighbour, gaps in found.items():
        chosen: list[tuple[float, Vec3, Vec3, int, int]] = []
        for gap in sorted(gaps, key=lambda item: item[0]):
            if all(math.hypot(gap[1].x - kept[1].x, gap[1].y - kept[1].y) >= GAP_SPACING_CM for kept in chosen):
                chosen.append(gap)
            if len(chosen) == GAPS_PER_NEIGHBOUR:
                break
        result[neighbour] = chosen
    return result


def _across(graph: DetourTopologyGraph, gap: tuple[float, Vec3, Vec3, int, int]) -> tuple[Vec3, Vec3]:
    """Sweep ends either side of a gap, from its own island to the other."""
    _, vertex, closest, own, other = gap
    dx, dy = closest.x - vertex.x, closest.y - vertex.y
    length = math.hypot(dx, dy)
    if length < 20.0:
        # The islands touch at a point: cross between the polygons' centres.
        a, b = graph.polygons[own].centroid, graph.polygons[other].centroid
        dx, dy = b.x - a.x, b.y - a.y
        length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    mx, my = (vertex.x + closest.x) / 2.0, (vertex.y + closest.y) / 2.0
    return (Vec3(mx - ux * GAP_SWEEP_REACH_CM, my - uy * GAP_SWEEP_REACH_CM, vertex.z),
            Vec3(mx + ux * GAP_SWEEP_REACH_CM, my + uy * GAP_SWEEP_REACH_CM, closest.z))


def _front(graph: DetourTopologyGraph, label: dict[int, int], island: int, point: Vec3) -> Vec3 | None:
    """``point`` snapped onto ``island``'s navmesh."""
    nearby = [reference for reference in graph._point_resolution_references(point, FRONT_SNAP_M)
              if label.get(reference) == island]
    candidates = graph.resolve_point_candidates(point, references=nearby, max_snap_distance_m=FRONT_SNAP_M,
                                                max_vertical_separation_m=FRONT_VERTICAL_M)
    return candidates[0].closest_point if candidates else None


def _crossing(sweep: dict[str, Any], segment_index: int) -> dict[str, Any] | None:
    """What joins a gap: a door to open, an open way, or nothing (None).

    A door opened once stays open (live at Nibelheim: after Cloud had been
    in to Dale, the sweep across the room's doorway hit nothing), so a gap
    Cloud's capsule crosses cleanly is a way through with nothing to open.
    """
    for segment in sweep.get("segments") or []:
        if segment.get("index") != segment_index:
            continue
        hits = segment.get("hits") or []
        door = next((hit for hit in hits if hit.get("kind") == "door"), None)
        if door is not None:
            return {"open": False, "hit": door}
        if segment.get("complete") is not False and not any(hit.get("kind") in GAP_BLOCKING_KINDS for hit in hits):
            return {"open": True, "hit": None}
    return None


def _point(value: dict[str, float]) -> Vec3:
    return Vec3(float(value["x"]), float(value["y"]), float(value["z"]))


def find_door_chain(graph: DetourTopologyGraph, sweep: Callable[[list[dict[str, float]]], dict[str, Any]],
                    start: Vec3, end_reference: int, start_reference: int, *,
                    known: Iterable[dict[str, Any]] = (),
                    record: Callable[[dict[str, Any]], None] | None = None) -> list[dict[str, Any]]:
    """Doors from Cloud's island to the NPC's, in walking order ([] if none).

    ``known`` links (DoorLinks.for_topology) join islands without a sweep:
    a door found shut and opened stays open, and its open doorway sweeps like
    a wall (the sill and frame). Each door a sweep finds is passed to
    ``record``.
    """
    label = islands(graph)
    known_by_island: dict[int, list[dict[str, Any]]] = {}
    for link in known:
        try:
            first, second = (int(value) for value in link["islands"])
        except (KeyError, TypeError, ValueError):
            continue
        if first in graph.polygons and second in graph.polygons:
            known_by_island.setdefault(first, []).append(link)
            known_by_island.setdefault(second, []).append(link)
    start_island, end_island = label[start_reference], label[end_reference]
    if start_island == end_island:
        return []
    sizes: dict[int, int] = {}
    for owner in label.values():
        sizes[owner] = sizes.get(owner, 0) + 1
    # Search out from the smaller end; the town is only ever reached.
    root, goal = (end_island, start_island) if sizes[end_island] <= sizes[start_island] else (start_island, end_island)
    came_from: dict[int, tuple[int, dict[str, Any]] | None] = {root: None}
    queue = deque([root])
    while queue and goal not in came_from:
        island = queue.popleft()
        if island != root and sizes[island] > LARGE_ISLAND_POLYGONS:
            continue
        for link in known_by_island.get(island, ()):
            neighbour = next(int(value) for value in link["islands"] if int(value) != island)
            if neighbour in came_from:
                continue
            points = link.get("points") or {}
            came_from[neighbour] = (island, {"actor": link.get("actor"), "bounds": link.get("bounds"),
                                             "open": False, "known": True,
                                             "island_side": _point(points[str(island)]),
                                             "neighbour_side": _point(points[str(neighbour)])})
            queue.append(neighbour)
        if goal in came_from:
            break
        gaps = [(neighbour, gap) for neighbour, items in _gaps(graph, label, island).items()
                if neighbour not in came_from for gap in items]
        if not gaps:
            continue
        points: list[dict[str, float]] = []
        for _, gap in gaps:
            a, b = _across(graph, gap)
            points += [{"x": a.x, "y": a.y, "z": a.z}, {"x": b.x, "y": b.y, "z": b.z}]
        swept = sweep(points)
        for index, (neighbour, gap) in enumerate(gaps):
            if neighbour in came_from:
                continue
            crossing = _crossing(swept, 2 * index)  # odd segments join one gap to the next
            if crossing is None:
                continue
            hit = crossing["hit"] or {}
            a, b = _across(graph, gap)
            came_from[neighbour] = (island, {"actor": hit.get("actor"), "bounds": hit.get("actor_bounds"),
                                             "open": crossing["open"], "island_side": a, "neighbour_side": b})
            queue.append(neighbour)
            if record is not None and not crossing["open"]:
                record({"islands": [island, neighbour], "actor": hit.get("actor"), "bounds": hit.get("actor_bounds"),
                        "points": {str(island): {"x": a.x, "y": a.y, "z": a.z},
                                   str(neighbour): {"x": b.x, "y": b.y, "z": b.z}}})
            if len(came_from) > 4 * MAX_DOORS + 1:
                break
    if goal not in came_from:
        return []
    links: list[tuple[int, int, dict[str, Any]]] = []
    island = goal
    while came_from[island] is not None:
        previous, link = came_from[island]
        links.append((previous, island, link))
        island = previous
    # links run goal -> root; walking order is start -> end.
    ordered = links if root == end_island else list(reversed(links))
    doors = []
    for previous, island, link in ordered:
        if root == end_island:
            # Found searching from the NPC: ``island`` is on Cloud's side.
            near_island, near, far_island, far = island, link["neighbour_side"], previous, link["island_side"]
        else:
            near_island, near, far_island, far = previous, link["island_side"], island, link["neighbour_side"]
        front, beyond = _front(graph, label, near_island, near), _front(graph, label, far_island, far)
        if front is None or beyond is None:
            return []
        bounds = link.get("bounds") or {}
        origin = bounds.get("origin") or {}
        centre = ({"x": float(origin["x"]), "y": float(origin["y"]), "z": float(origin.get("z") or front.z)}
                  if "x" in origin and "y" in origin else
                  {"x": (front.x + beyond.x) / 2.0, "y": (front.y + beyond.y) / 2.0, "z": front.z})
        doors.append({"actor": link.get("actor"), "open": bool(link.get("open")), "centre": centre,
                      "front": {"x": front.x, "y": front.y, "z": front.z},
                      "beyond": {"x": beyond.x, "y": beyond.y, "z": beyond.z}})
    return doors[:MAX_DOORS] if len(doors) <= MAX_DOORS else []


class DoorLinks:
    """Door links found so far, per navmesh capture, on disk.

    Keyed by the capture's topology fingerprint; island labels are that
    capture's own polygon references, so they hold only for it.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        self.links: dict[str, list[dict[str, Any]]] = loaded if isinstance(loaded, dict) else {}

    def for_topology(self, topology: str) -> list[dict[str, Any]]:
        return list(self.links.get(topology) or [])

    def record(self, topology: str, link: dict[str, Any]) -> None:
        pair = sorted(int(value) for value in link["islands"])
        kept = [item for item in self.links.get(topology) or [] if sorted(item.get("islands") or []) != pair]
        self.links[topology] = kept + [link]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self.links, indent=1), encoding="utf-8")
        temporary.replace(self.path)


def default_door_links() -> DoorLinks:
    return DoorLinks(Path(os.environ.get("LOCALAPPDATA", ".")) / "ff7r-cait-gpt" / "door-links.json")
