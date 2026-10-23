"""Off-mesh (traversal) links of the shipping navmesh: classes, policy, report.

A navmesh island that the polygon links leave unconnected can still be joined
by an off-mesh connection: a two-vertex ``type 1`` polygon whose ``edge 0``
link leads to the polygon the connection starts on and whose ``edge 1`` link
leads to the polygon it ends on.  The planner used to drop every such link
(``shipping_dynamic_links_route_semantics_unresolved``), so a low girder that
the character walks straight over (no prompt, an automatic step-over) split
the prologue's start area from the rest of the street.

What the 2026-10-13 prologue capture (Zack, 407 tiles, 1,740 connections,
``Telemetry/evidence/2026-10-13-prologue-navmesh/geom-zack.json``) says about
the records, and how sure we are:

* ``side`` of a connection is where its END lies, as in stock Detour: 128 for
  the same tile, 0..7 for the neighbouring tile.  It says nothing about the
  direction of travel.  (certain: it equals the tile side of the end point)
* ``flags`` is 64 on 1,500 connections and 192 (64 plus 0x80) on 240.  Each
  connection leaves four dynamic links: start polygon to off-mesh polygon,
  off-mesh polygon to start polygon (``edge 0``), off-mesh polygon to end
  polygon (``edge 1``), end polygon to off-mesh polygon.  All 6,238 of them
  carry the 0x20 bit in their ``side`` on a 192 connection and none does on
  a 64 one.  On a 64 connection the two links that point back from the end
  (end polygon to off-mesh polygon, off-mesh polygon to start polygon) carry
  the 0x10 bit; on a 192 connection the end polygon's link does not.  The
  vertical delta of the 192 connections is symmetric about zero (median
  +0.3 m, 10th to 90th percentile -2.3 m to +2.5 m); that of the 64
  connections is not (median -2.3 m, 75th percentile +0.2 m): mostly drops.
  Reading: 64 is one way, start to end; 192 is two-way.  (about 85 % sure,
  nothing crossed live yet.  Only start to end is used by default; the
  reverse of a two-way connection is a separate, off-by-default policy
  switch.)
* The landing polygons are those the off-mesh polygon's own links name, not
  whatever polygon lies under the points: that is the pair the engine chose.
* 196 of 1,740 connections have no landing link on one side (the end lies off
  the mesh); they join nothing and are counted, not used.

Only short, near-level, ground-to-ground links are used by default
(``step_over``).  Drops, climbs and long spans have classes of their own and
stay out of routes until a live crossing verifies them.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import Vec3


REPORT_SCHEMA = "ff7r.offmesh_traversal_report.v1"
REPORT_SUFFIX = ".traversal-report.json"

CLASS_STEP_OVER = "step_over"
CLASS_STEP_DOWN = "step_down"
CLASS_STEP_UP = "step_up"
CLASS_DROP = "drop"
CLASS_CLIMB = "climb"
CLASS_LONG_SPAN = "long_span"
# Field-action gimmicks the live scan reads (gimmick_links.py), not
# connections of the capture: their class is the gimmick's, not its geometry.
CLASS_LADDER = "ladder"
CLASS_CRACK = "crack"
CLASS_CROUCH = "crouch"
GIMMICK_LINK_CLASSES = (CLASS_LADDER, CLASS_CRACK, CLASS_CROUCH)
LINK_CLASSES = (
    CLASS_STEP_OVER,
    CLASS_STEP_DOWN,
    CLASS_STEP_UP,
    CLASS_DROP,
    CLASS_CLIMB,
    CLASS_LONG_SPAN,
) + GIMMICK_LINK_CLASSES

# A step-over is level (within a kerb or a low girder) and short (a few
# strides): the live crossing at the prologue's girder was 1.6 m to 5.7 m.
STEP_OVER_MAX_VERTICAL_CM = 50.0
STEP_OVER_MAX_HORIZONTAL_CM = 600.0
# Beyond a step-over, up to this much is a step down or up; more is a drop or
# a climb.
STEP_MAX_VERTICAL_CM = 150.0
# A link shorter than this joins polygons that touch; it is no crossing.
MIN_LINK_LENGTH_CM = 10.0
# An end point must lie on its landing polygon to this tolerance.
LANDING_MAX_HORIZONTAL_CM = 50.0
LANDING_MAX_VERTICAL_CM = 60.0

# Connection ``flags``: every connection carries 0x40; 0x80 marks two-way.
CONNECTION_FLAG_DYNAMIC = 0x40
CONNECTION_FLAG_TWO_WAY = 0x80
# Bits of the ``side`` of the dynamic links of a connection.
LINK_SIDE_REVERSE_BIT = 0x10
LINK_SIDE_TWO_WAY_BIT = 0x20

# Classes crossed live and found to work as planned: Kalm, 2026-10-14, the
# shelter crack, the crawl space under the houses and the inn-cellar ladder.
VERIFIED_LINK_CLASSES: frozenset[str] = frozenset(GIMMICK_LINK_CLASSES)

# Modest: roughly the stride lost stopping at the link and picking up again.
DEFAULT_TRAVERSAL_PENALTY_M = 3.0

# The ``side`` the planner gives a traversal portal.  Ordinary portals use
# 128 (same tile) and 0, 2, 4, 6; 0x40 is the shipping dynamic-link bit.
TRAVERSAL_PORTAL_SIDE = 0x40
PORTAL_KIND_ORDINARY = "ordinary"
PORTAL_KIND_TRAVERSAL = "traversal"
DIRECTION_FORWARD = "start_to_end"
DIRECTION_REVERSE = "end_to_start"

FLAG_SEMANTICS: dict[str, Any] = {
    "side": "where the end point lies: 128 same tile, 0..7 neighbouring tile (not a direction)",
    "flags_0x40": "set on every connection (dynamic-link marker)",
    "flags_0x80": "two-way connection (about 85 % sure; reverse use is off by default)",
    "flags_64": "one way, start to end",
    "link_side_0x20": "the connection is two-way (on every one of its dynamic links)",
    "link_side_0x10": "reverse-direction link of a one-way connection",
    "confidence": "evidence is the 2026-10-13 prologue capture; not yet verified live",
}


def classify_link(horizontal_cm: float, vertical_cm: float) -> str:
    """The geometric class of a link from its length and rise (end minus start)."""
    if abs(vertical_cm) <= STEP_OVER_MAX_VERTICAL_CM:
        return CLASS_STEP_OVER if horizontal_cm <= STEP_OVER_MAX_HORIZONTAL_CM else CLASS_LONG_SPAN
    if horizontal_cm > STEP_OVER_MAX_HORIZONTAL_CM:
        return CLASS_DROP if vertical_cm < 0.0 else CLASS_CLIMB
    if vertical_cm < 0.0:
        return CLASS_STEP_DOWN if vertical_cm >= -STEP_MAX_VERTICAL_CM else CLASS_DROP
    return CLASS_STEP_UP if vertical_cm <= STEP_MAX_VERTICAL_CM else CLASS_CLIMB


def _point_dict(point: Vec3) -> dict[str, float]:
    return {"x": point.x, "y": point.y, "z": point.z}


@dataclass(frozen=True)
class OffMeshLink:
    """One off-mesh connection joining two ground polygons.

    ``start`` and ``end`` are the points on the landing polygons (the
    connection's own points are snapped onto the mesh when the tile is built).
    """

    owner_reference: int
    start_reference: int
    end_reference: int
    start: Vec3
    end: Vec3
    flags: int
    side: int
    link_class: str
    horizontal_length_cm: float
    vertical_delta_cm: float
    # Set on a gimmick's link: the scanned actor and which way it is crossed.
    gimmick_id: str | None = None
    gimmick_direction: str | None = None

    @property
    def two_way_claimed(self) -> bool:
        return bool(self.flags & CONNECTION_FLAG_TWO_WAY)

    @property
    def length_cm(self) -> float:
        return math.hypot(self.horizontal_length_cm, self.vertical_delta_cm)

    def crossing(self, reverse: bool = False) -> tuple[int, int, Vec3, Vec3]:
        """(from polygon, to polygon, departure point, landing point)."""
        if reverse:
            return self.end_reference, self.start_reference, self.end, self.start
        return self.start_reference, self.end_reference, self.start, self.end

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_ref": self.owner_reference,
            "start_ref": self.start_reference,
            "end_ref": self.end_reference,
            "start": _point_dict(self.start),
            "end": _point_dict(self.end),
            "flags": self.flags,
            "side": self.side,
            "link_class": self.link_class,
            "horizontal_length_m": self.horizontal_length_cm / 100.0,
            "vertical_delta_m": self.vertical_delta_cm / 100.0,
            "two_way_claimed": self.two_way_claimed,
            "live_verified": self.link_class in VERIFIED_LINK_CLASSES,
            **self._gimmick_fields(),
        }

    def _gimmick_fields(self) -> dict[str, Any]:
        if self.gimmick_id is None:
            return {}
        return {"gimmick_id": self.gimmick_id, "gimmick_direction": self.gimmick_direction}


def make_link(
    owner_reference: int,
    start_reference: int,
    end_reference: int,
    start: Vec3,
    end: Vec3,
    flags: int,
    side: int,
) -> OffMeshLink:
    horizontal_cm = math.hypot(end.x - start.x, end.y - start.y)
    vertical_cm = end.z - start.z
    return OffMeshLink(
        owner_reference=int(owner_reference),
        start_reference=int(start_reference),
        end_reference=int(end_reference),
        start=start,
        end=end,
        flags=int(flags),
        side=int(side),
        link_class=classify_link(horizontal_cm, vertical_cm),
        horizontal_length_cm=horizontal_cm,
        vertical_delta_cm=vertical_cm,
    )


@dataclass(frozen=True)
class TraversalPolicy:
    """Which links a plan may cross, and what a crossing costs."""

    # Off: climbs and long spans. Cloud cannot climb a bare ledge or jump a
    # gap at will; climbing walls and ladders are scanned gimmicks. Steps and
    # drops went on 2026-10-21 (owner): the Mt. Nibel caves go down by drops,
    # and with them off the planner saw the way on as unreachable islands.
    enabled_classes: frozenset[str] = frozenset({
        CLASS_STEP_OVER, CLASS_STEP_DOWN, CLASS_STEP_UP, CLASS_DROP, *GIMMICK_LINK_CLASSES})
    allow_reverse_of_two_way: bool = False
    penalty_m: float = DEFAULT_TRAVERSAL_PENALTY_M
    # Links (owner references) a crossing failed on: never planned again.
    excluded_links: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled_classes", frozenset(self.enabled_classes))
        object.__setattr__(self, "excluded_links", frozenset(int(item) for item in self.excluded_links))
        unknown = self.enabled_classes - set(LINK_CLASSES)
        if unknown:
            raise ValueError(f"unknown traversal link classes: {sorted(unknown)}")
        if not math.isfinite(float(self.penalty_m)) or float(self.penalty_m) < 0.0:
            raise ValueError("traversal penalty must be finite and non-negative")

    @property
    def active(self) -> bool:
        return bool(self.enabled_classes)

    def permits(self, link_class: str, reverse: bool = False, link_reference: int | None = None) -> bool:
        return (link_class in self.enabled_classes and (not reverse or self.allow_reverse_of_two_way)
                and (link_reference is None or int(link_reference) not in self.excluded_links))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "enabled_classes": sorted(self.enabled_classes),
            "allow_reverse_of_two_way": self.allow_reverse_of_two_way,
            "penalty_m": self.penalty_m,
        }
        if self.excluded_links:
            result["excluded_links"] = sorted(self.excluded_links)
        return result


DEFAULT_TRAVERSAL_POLICY = TraversalPolicy()
DISABLED_TRAVERSAL_POLICY = TraversalPolicy(enabled_classes=frozenset())
EVERYTHING_POLICY = TraversalPolicy(
    enabled_classes=frozenset(LINK_CLASSES), allow_reverse_of_two_way=True,
)


def segment_for_link(
    link: OffMeshLink,
    *,
    reverse: bool,
    order: int,
    start_waypoint_index: int | None = None,
    end_waypoint_index: int | None = None,
) -> dict[str, Any]:
    """The plan's description of one crossing, for the follower and validator."""
    from_ref, to_ref, departure, landing = link.crossing(reverse)
    segment: dict[str, Any] = {
        "order": order,
        "kind": PORTAL_KIND_TRAVERSAL,
        "link_class": link.link_class,
        "direction": DIRECTION_REVERSE if reverse else DIRECTION_FORWARD,
        "from_ref": from_ref,
        "to_ref": to_ref,
        "link_ref": link.owner_reference,
        "start": _point_dict(departure),
        "end": _point_dict(landing),
        "horizontal_length_m": link.horizontal_length_cm / 100.0,
        "vertical_delta_m": (landing.z - departure.z) / 100.0,
        "two_way_claimed": link.two_way_claimed,
        "connection_flags": link.flags,
        "connection_side": link.side,
        "live_verified": link.link_class in VERIFIED_LINK_CLASSES,
        **link._gimmick_fields(),
    }
    if start_waypoint_index is not None:
        segment["start_waypoint_index"] = start_waypoint_index
    if end_waypoint_index is not None:
        segment["end_waypoint_index"] = end_waypoint_index
    return segment


def segment_points(segment: Mapping[str, Any]) -> tuple[Vec3, Vec3]:
    start, end = segment["start"], segment["end"]
    return (
        Vec3(float(start["x"]), float(start["y"]), float(start["z"])),
        Vec3(float(end["x"]), float(end["y"]), float(end["z"])),
    )


def unverified_classes(segments: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted({
        str(segment.get("link_class")) for segment in segments
        if str(segment.get("link_class")) not in VERIFIED_LINK_CLASSES
    })


def plan_traversal_diagnostics(
    segments: Sequence[Mapping[str, Any]], policy: TraversalPolicy,
) -> dict[str, Any]:
    """What a plan using traversal links must say before any movement."""
    by_class = Counter(str(segment.get("link_class")) for segment in segments)
    unverified = unverified_classes(segments)
    return {
        "count": len(segments),
        "by_class": dict(sorted(by_class.items())),
        "unverified_classes": unverified,
        "requires_live_verification": bool(unverified),
        "policy": policy.to_dict(),
    }


WAYPOINT_MATCH_TOLERANCE_CM = 2.0


def _near(first: Vec3, second: Vec3, tolerance_cm: float) -> bool:
    return math.dist((first.x, first.y, first.z), (second.x, second.y, second.z)) <= tolerance_cm


def locate_traversal_waypoints(
    waypoints: Sequence[Vec3],
    segments: Sequence[Mapping[str, Any]],
    *,
    tolerance_cm: float = WAYPOINT_MATCH_TOLERANCE_CM,
) -> list[tuple[Mapping[str, Any], int]]:
    """Each segment with the index of its departure waypoint.

    A segment is crossed between two consecutive waypoints: its departure and
    its landing point.  An obstacle detour that inserts a waypoint between
    them, or moves either, leaves the crossing unlocatable, which the caller
    treats as an error before any movement.
    """
    located: list[tuple[Mapping[str, Any], int]] = []
    cursor = 0
    for segment in segments:
        start, end = segment_points(segment)
        found = None
        for index in range(cursor, len(waypoints) - 1):
            if _near(waypoints[index], start, tolerance_cm) and _near(waypoints[index + 1], end, tolerance_cm):
                found = index
                break
        if found is None:
            raise ValueError("traversal_waypoints_not_found")
        located.append((segment, found))
        cursor = found + 1
    return located


def with_waypoint_indices(
    segments: Sequence[Mapping[str, Any]], waypoints: Sequence[Vec3],
) -> tuple[dict[str, Any], ...]:
    """The segments with their waypoint indices refreshed (best effort)."""
    try:
        located = locate_traversal_waypoints(waypoints, segments)
    except ValueError:
        return tuple(dict(segment) for segment in segments)
    return tuple(
        {**dict(segment), "start_waypoint_index": index, "end_waypoint_index": index + 1}
        for segment, index in located
    )


# ---------------------------------------------------------------------------
# The report: islands, and the links that bridge them, by class.
# ---------------------------------------------------------------------------

MAX_REPORT_ISLANDS = 40
MAX_REPORT_BRIDGES = 300
MAX_BRIDGE_EXAMPLES = 3


def _island_stats(graph: Any, labels: Mapping[int, int]) -> list[dict[str, Any]]:
    members: dict[int, list[int]] = defaultdict(list)
    for reference, island in labels.items():
        members[island].append(reference)
    stats = []
    for island, refs in sorted(members.items()):
        xs = [vertex.x for ref in refs for vertex in graph.polygons[ref].vertices]
        ys = [vertex.y for ref in refs for vertex in graph.polygons[ref].vertices]
        stats.append({
            "island": island,
            "polygons": len(refs),
            "x_cm": [min(xs), max(xs)],
            "y_cm": [min(ys), max(ys)],
        })
    return stats


def _strong_components(island_count: int, edges: Sequence[tuple[int, int]]) -> list[int]:
    """Component label of each island when ``edges`` are one-way (Kosaraju)."""
    forward: list[list[int]] = [[] for _ in range(island_count)]
    backward: list[list[int]] = [[] for _ in range(island_count)]
    for first, second in edges:
        forward[first].append(second)
        backward[second].append(first)
    order: list[int] = []
    seen = [False] * island_count
    for root in range(island_count):
        if seen[root]:
            continue
        seen[root] = True
        stack = [(root, iter(forward[root]))]
        while stack:
            node, successors = stack[-1]
            for successor in successors:
                if not seen[successor]:
                    seen[successor] = True
                    stack.append((successor, iter(forward[successor])))
                    break
            else:
                order.append(node)
                stack.pop()
    label = [-1] * island_count
    count = 0
    for root in reversed(order):
        if label[root] != -1:
            continue
        label[root] = count
        pending = [root]
        while pending:
            node = pending.pop()
            for predecessor in backward[node]:
                if label[predecessor] == -1:
                    label[predecessor] = count
                    pending.append(predecessor)
        count += 1
    return label


def _group_stats(island_count: int, edges: Sequence[tuple[int, int]]) -> dict[str, int]:
    """How far ``edges`` between islands join them: ignoring direction (weak)
    and where every island of a group can walk to every other (strong)."""
    parent = list(range(island_count))

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for first, second in edges:
        parent[find(first)] = find(second)
    weak = Counter(find(item) for item in range(island_count))
    strong = Counter(_strong_components(island_count, edges))
    return {
        "weak_groups": len(weak),
        "islands_joined": island_count - len(weak),
        "largest_weak_group": max(weak.values(), default=0),
        "strong_groups": len(strong),
        "largest_strong_group": max(strong.values(), default=0),
    }


def traversal_policy_variants() -> dict[str, TraversalPolicy]:
    return {
        "step_over_forward": TraversalPolicy(),
        "step_over_with_two_way_reverse": TraversalPolicy(allow_reverse_of_two_way=True),
        "all_classes_forward": TraversalPolicy(enabled_classes=frozenset(LINK_CLASSES)),
        "all_classes_with_two_way_reverse": EVERYTHING_POLICY,
    }


def bridge_edges(
    graph: Any, labels: Mapping[int, int], policy: TraversalPolicy,
) -> list[tuple[int, int, OffMeshLink, bool]]:
    """(from island, to island, link, reverse) for each crossing ``policy`` allows
    between two different islands."""
    edges = []
    for link in graph.off_mesh_links:
        for reverse in (False, True):
            if (not policy.permits(link.link_class, reverse, link.owner_reference)
                    or (reverse and not link.two_way_claimed)):
                continue
            from_ref, to_ref, _, _ = link.crossing(reverse)
            first, second = labels.get(from_ref), labels.get(to_ref)
            if first is None or second is None or first == second:
                continue
            edges.append((first, second, link, reverse))
    return edges


def traversal_summary(graph: Any) -> dict[str, Any]:
    """A short summary of a graph's links, cheap enough for planner diagnostics."""
    links = graph.off_mesh_links
    labels = graph.ground_islands()
    island_count = len(set(labels.values()))
    by_class = Counter(link.link_class for link in links)
    bridging = Counter()
    for first, second, link, reverse in bridge_edges(graph, labels, EVERYTHING_POLICY):
        if not reverse:
            bridging[link.link_class] += 1
    default = graph.traversal_policy
    joined = _group_stats(
        island_count, [(a, b) for a, b, _, _ in bridge_edges(graph, labels, default)],
    )["islands_joined"]
    return {
        "links": len(links),
        "links_by_class": dict(sorted(by_class.items())),
        "islands": island_count,
        "bridging_links_by_class": dict(sorted(bridging.items())),
        "default_policy": default.to_dict(),
        "islands_joined_by_default_policy": joined,
        "unverified_classes": sorted(set(by_class) - VERIFIED_LINK_CLASSES),
        "verified_classes": sorted(VERIFIED_LINK_CLASSES),
    }


def build_traversal_report(graph: Any, *, source: str | None = None) -> dict[str, Any]:
    """Islands, and the off-mesh links bridging them, by class.

    Written beside the capture so that a route depending on a link class
    nobody has crossed yet is known before anything moves.
    """
    labels = graph.ground_islands()
    island_count = len(set(labels.values()))
    links = graph.off_mesh_links
    by_class = Counter(link.link_class for link in links)
    by_flags = Counter((link.flags, link.link_class) for link in links)
    bridges: dict[tuple[int, int], dict[str, Any]] = {}
    for first, second, link, reverse in bridge_edges(graph, labels, EVERYTHING_POLICY):
        entry = bridges.setdefault((first, second), {
            "from_island": first,
            "to_island": second,
            "links_by_class": Counter(),
            "reverse_claims_by_class": Counter(),
            "examples": [],
        })
        entry["reverse_claims_by_class" if reverse else "links_by_class"][link.link_class] += 1
        if len(entry["examples"]) < MAX_BRIDGE_EXAMPLES and not reverse:
            entry["examples"].append(link.to_dict())
    ordered = sorted(
        bridges.values(),
        key=lambda item: (
            -(sum(item["links_by_class"].values()) + sum(item["reverse_claims_by_class"].values())),
            item["from_island"], item["to_island"],
        ),
    )
    for entry in ordered:
        entry["links_by_class"] = dict(sorted(entry["links_by_class"].items()))
        entry["reverse_claims_by_class"] = dict(sorted(entry["reverse_claims_by_class"].items()))
    variants = {}
    for name, policy in traversal_policy_variants().items():
        variants[name] = {
            "policy": policy.to_dict(),
            **_group_stats(
                island_count, [(a, b) for a, b, _, _ in bridge_edges(graph, labels, policy)],
            ),
        }
    island_stats = _island_stats(graph, labels)
    return {
        "schema": REPORT_SCHEMA,
        "source": source,
        "thresholds": {
            "step_over_max_vertical_cm": STEP_OVER_MAX_VERTICAL_CM,
            "step_over_max_horizontal_cm": STEP_OVER_MAX_HORIZONTAL_CM,
            "step_max_vertical_cm": STEP_MAX_VERTICAL_CM,
            "min_link_length_cm": MIN_LINK_LENGTH_CM,
        },
        "flag_semantics": FLAG_SEMANTICS,
        "verified_classes": sorted(VERIFIED_LINK_CLASSES),
        "default_policy": graph.traversal_policy.to_dict(),
        "connections": dict(graph.snapshot_diagnostics.get("off_mesh") or {}),
        "links": {
            "count": len(links),
            "by_class": dict(sorted(by_class.items())),
            "by_flags_and_class": {f"{flags}:{name}": count for (flags, name), count in sorted(by_flags.items())},
            "two_way_claimed": sum(1 for link in links if link.two_way_claimed),
        },
        "islands": {
            "count": island_count,
            "polygons": len(labels),
            "largest": sorted(island_stats, key=lambda item: -item["polygons"])[:MAX_REPORT_ISLANDS],
        },
        "bridges": {
            "count": len(ordered),
            "listed": ordered[:MAX_REPORT_BRIDGES],
        },
        "connectivity_by_policy": variants,
    }


def report_path_for(artifact_path: str | Path) -> Path:
    path = Path(artifact_path)
    return path.with_name(path.stem + REPORT_SUFFIX)


def write_traversal_report(
    graph: Any, artifact_path: str | Path, *, source: str | None = None,
) -> Path | None:
    """Write the report beside ``artifact_path``; None if it cannot be written.

    Never raises: a missing report must not stop a route.  A capture whose
    graph has no usable off-mesh link gets no report.
    """
    try:
        if not graph.off_mesh_links:
            return None
        destination = report_path_for(artifact_path)
        report = build_traversal_report(graph, source=source or str(artifact_path))
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(destination)
        return destination
    except Exception:  # noqa: BLE001 - best effort by design
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report the off-mesh traversal links of a navmesh geometry capture.",
    )
    parser.add_argument("capture", help="ue4ss_native_detour_geometry_dump JSON")
    parser.add_argument("--out", help="report path (default: beside the capture)")
    args = parser.parse_args(argv)
    from .detour_route import DetourTopologyGraph

    graph = DetourTopologyGraph.from_json(args.capture)
    destination = Path(args.out) if args.out else report_path_for(args.capture)
    report = build_traversal_report(graph, source=str(args.capture))
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "report": str(destination),
        "links_by_class": report["links"]["by_class"],
        "islands": report["islands"]["count"],
        "connectivity_by_policy": {
            name: {key: item[key] for key in ("weak_groups", "strong_groups", "largest_strong_group")}
            for name, item in report["connectivity_by_policy"].items()
        },
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
