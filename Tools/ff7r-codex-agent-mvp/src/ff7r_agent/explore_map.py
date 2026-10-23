"""Top-down navmesh views and destination analysis for explore mode.

Pure geometry: it reads a ``DetourTopologyGraph`` (the same graph the route
planner walks), never the game, and sends no input. Three jobs:

* ``analyse_view``: where the player can walk on the graph (its navmesh
  island), the far ends of that island, spots where the captured mesh runs
  out, known points of interest, and the nearest points of other islands
  (marked as not reachable on the mesh);
* ``render_view``: a dependency-free PNG of the window around a centre, with
  heights colour-coded, a labelled metre grid, the player and the numbered
  candidates;
* ``snap_target``: a chosen point snapped onto a walkable polygon the player
  can reach, or a refusal that names why and the nearest reachable point.

World coordinates are Unreal centimetres, the same as telemetry
(``observe_game``): the native capture stores (x, y, z) and the graph loads
them as world (-x, -z, y). On the image world +X runs right and world +Y runs
down, which is the true view from above for Unreal's left-handed axes.
"""
from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
import struct
import time
from typing import Any, Iterable, Mapping, Sequence
import zlib

from .detour_route import DT_POLYTYPE_GROUND, DetourPolygon, DetourTopologyGraph
from .door_links import islands as island_labels
from .models import Vec3

DEFAULT_RADIUS_M = 60.0
MIN_RADIUS_M = 10.0
MAX_RADIUS_M = 250.0
DEFAULT_IMAGE_PIXELS = 960
MAX_IMAGE_PIXELS = 2048
MAX_PX_PER_M = 16.0
DEFAULT_MAX_CANDIDATES = 8
MAX_CANDIDATES = 20
# A point this close to a polygon (or inside it) counts as being on that floor.
NEAR_FLOOR_CM = 100.0
# Route targets are kept this far from a polygon's edge (the route adapter's
# destination_edge_clearance_m) where the polygon is wide enough.
EDGE_CLEARANCE_CM = 50.0
# Floors differ by metres; weigh height more than distance along the floor
# when choosing destinations that are far from each other.
HEIGHT_WEIGHT = 3.0
# An unlinked polygon edge this close to the capture's outer boundary may be
# the end of the captured mesh rather than a wall.
CAPTURE_EDGE_MARGIN_CM = 300.0
MAX_ISLAND_CANDIDATES = 6
MIN_ISLAND_POLYGONS = 6
GRID_STEPS_M = (1, 2, 5, 10, 20, 25, 50, 100, 200)
# Candidate distances are replaced by the planner's own funnelled path length
# while this much time remains (the estimate through polygon centres runs up to
# ~35% long on a real capture).
PLAN_REFINEMENT_SECONDS = 3.0

KIND_COLOURS = {
    "far_end": (60, 255, 120),
    "capture_edge": (255, 170, 40),
    "poi": (40, 220, 255),
    "door": (255, 255, 60),
    "other_island": (255, 70, 70),
}


# ---------------------------------------------------------------- PNG output

def encode_png(width: int, height: int, rows: Sequence[bytes | bytearray]) -> bytes:
    """An 8-bit RGB PNG from ``height`` rows of ``width * 3`` bytes."""
    raw = b"".join(b"\x00" + bytes(row) for row in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def decode_png_size(data: bytes) -> tuple[int, int]:
    """(width, height) of a PNG produced here; raises ValueError otherwise."""
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


_GLYPHS = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "m": ("00000", "00000", "11010", "10101", "10101", "10001", "10001"),
    " ": ("00000",) * 7,
}


class Canvas:
    """A small RGB raster with just the primitives the view needs."""

    def __init__(self, width: int, height: int, background: tuple[int, int, int]) -> None:
        self.width, self.height = width, height
        self.rows = [bytearray(bytes(background) * width) for _ in range(height)]

    def put(self, x: int, y: int, colour: tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = x * 3
            self.rows[y][offset:offset + 3] = bytes(colour)

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        offset = x * 3
        row = self.rows[y]
        return row[offset], row[offset + 1], row[offset + 2]

    def span(self, y: int, x0: int, x1: int, colour: tuple[int, int, int]) -> None:
        """Fill columns ``x0`` .. ``x1`` (exclusive) of row ``y``."""
        if not 0 <= y < self.height:
            return
        x0, x1 = max(0, x0), min(self.width, x1)
        if x1 > x0:
            self.rows[y][x0 * 3:x1 * 3] = bytes(colour) * (x1 - x0)

    def line(self, x0: float, y0: float, x1: float, y1: float, colour: tuple[int, int, int], thickness: int = 1) -> None:
        steps = max(1, int(max(abs(x1 - x0), abs(y1 - y0))) + 1)
        reach = thickness // 2
        for k in range(steps + 1):
            px = int(round(x0 + (x1 - x0) * k / steps))
            py = int(round(y0 + (y1 - y0) * k / steps))
            for dx in range(-reach, reach + 1):
                for dy in range(-reach, reach + 1):
                    self.put(px + dx, py + dy, colour)

    def disc(self, cx: float, cy: float, radius: int, colour: tuple[int, int, int]) -> None:
        px, py = int(round(cx)), int(round(cy))
        for dy in range(-radius, radius + 1):
            half = int(math.sqrt(max(0, radius * radius - dy * dy)))
            self.span(py + dy, px - half, px + half + 1, colour)

    def ring(self, cx: float, cy: float, radius: int, colour: tuple[int, int, int], thickness: int = 2) -> None:
        px, py = int(round(cx)), int(round(cy))
        for dy in range(-radius - 1, radius + 2):
            for dx in range(-radius - 1, radius + 2):
                distance = math.hypot(dx, dy)
                if radius - thickness < distance <= radius:
                    self.put(px + dx, py + dy, colour)

    def cross(self, cx: float, cy: float, reach: int, colour: tuple[int, int, int]) -> None:
        self.line(cx - reach, cy - reach, cx + reach, cy + reach, colour, 2)
        self.line(cx - reach, cy + reach, cx + reach, cy - reach, colour, 2)

    def diamond(self, cx: float, cy: float, reach: int, colour: tuple[int, int, int]) -> None:
        points = [(cx, cy - reach), (cx + reach, cy), (cx, cy + reach), (cx - reach, cy)]
        for a, b in zip(points, points[1:] + points[:1]):
            self.line(a[0], a[1], b[0], b[1], colour, 2)

    def fill_polygon(self, points: Sequence[tuple[float, float]], colour: tuple[int, int, int]) -> None:
        if len(points) < 3:
            return
        first = max(0, int(math.floor(min(p[1] for p in points))))
        last = min(self.height - 1, int(math.ceil(max(p[1] for p in points))))
        edges = list(zip(points, points[1:] + points[:1]))
        for y in range(first, last + 1):
            centre = y + 0.5
            crossings = []
            for (xa, ya), (xb, yb) in edges:
                if (ya <= centre < yb) or (yb <= centre < ya):
                    crossings.append(xa + (centre - ya) * (xb - xa) / (yb - ya))
            crossings.sort()
            for i in range(0, len(crossings) - 1, 2):
                self.span(y, int(math.ceil(crossings[i] - 0.5)), int(math.floor(crossings[i + 1] - 0.5)) + 1, colour)

    def text(self, x: int, y: int, value: str, colour: tuple[int, int, int] = (255, 255, 255), scale: int = 2,
             outline: tuple[int, int, int] | None = (0, 0, 0)) -> int:
        """Draw ``value`` with its top-left at (x, y); returns the width drawn."""
        lit: list[tuple[int, int]] = []
        for index, character in enumerate(value):
            glyph = _GLYPHS.get(character, _GLYPHS[" "])
            for row, bits in enumerate(glyph):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        lit.append((x + (index * 6 + column) * scale, y + row * scale))
        if outline is not None:
            for px, py in lit:
                for dx in range(-1, scale + 1):
                    for dy in range(-1, scale + 1):
                        self.put(px + dx, py + dy, outline)
        for px, py in lit:
            for dx in range(scale):
                for dy in range(scale):
                    self.put(px + dx, py + dy, colour)
        return len(value) * 6 * scale - scale

    def png(self) -> bytes:
        return encode_png(self.width, self.height, self.rows)


def _ramp(fraction: float) -> tuple[int, int, int]:
    fraction = max(0.0, min(1.0, fraction))
    stops = [(0.0, (40, 60, 160)), (0.33, (40, 160, 160)), (0.66, (90, 180, 60)), (1.0, (230, 200, 60))]
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if fraction <= b:
            k = (fraction - a) / (b - a)
            return tuple(int(ca[i] + (cb[i] - ca[i]) * k) for i in range(3))  # type: ignore[return-value]
    return stops[-1][1]


def _blend(colour: tuple[int, int, int], other: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(int(colour[i] + (other[i] - colour[i]) * amount) for i in range(3))  # type: ignore[return-value]


# ----------------------------------------------------------------- transform

@dataclass(frozen=True)
class ViewTransform:
    """World centimetres <-> image pixels for one rendered view."""

    left_cm: float
    top_cm: float
    px_per_m: float
    width: int
    height: int

    def to_pixel(self, x_cm: float, y_cm: float) -> tuple[float, float]:
        scale = self.px_per_m / 100.0
        return (x_cm - self.left_cm) * scale, (y_cm - self.top_cm) * scale

    def to_world(self, px: float, py: float) -> tuple[float, float]:
        scale = 100.0 / self.px_per_m
        return self.left_cm + px * scale, self.top_cm + py * scale

    def contains(self, x_cm: float, y_cm: float) -> bool:
        px, py = self.to_pixel(x_cm, y_cm)
        return 0 <= px < self.width and 0 <= py < self.height

    def to_dict(self) -> dict[str, Any]:
        right_cm, bottom_cm = self.to_world(self.width, self.height)
        return {
            "left_cm": round(self.left_cm, 2), "top_cm": round(self.top_cm, 2),
            "px_per_m": self.px_per_m, "width": self.width, "height": self.height,
            "top_left_m": {"x": round(self.left_cm / 100.0, 2), "y": round(self.top_cm / 100.0, 2)},
            "bottom_right_m": {"x": round(right_cm / 100.0, 2), "y": round(bottom_cm / 100.0, 2)},
            "axes": {"x": "world +X to the right", "y": "world +Y downwards"},
            "pixel_to_world_cm": "x = left_cm + px / px_per_m * 100; y = top_cm + py / px_per_m * 100",
            "world_to_pixel": "px = (x - left_cm) / 100 * px_per_m; py = (y - top_cm) / 100 * px_per_m",
            "units": "world positions are centimetres, as in observe_game; grid labels are metres",
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ViewTransform":
        try:
            transform = cls(float(value["left_cm"]), float(value["top_cm"]), float(value["px_per_m"]),
                            int(value.get("width", 0)), int(value.get("height", 0)))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("transform needs left_cm, top_cm and px_per_m") from exc
        if not all(math.isfinite(v) for v in (transform.left_cm, transform.top_cm, transform.px_per_m)) \
                or transform.px_per_m <= 0:
            raise ValueError("transform values must be finite and px_per_m positive")
        return transform


def choose_px_per_m(radius_m: float, requested: float | None = None) -> float:
    """Pixels per metre: about ``DEFAULT_IMAGE_PIXELS`` across unless requested, never beyond the size cap."""
    cap = MAX_IMAGE_PIXELS / (2.0 * radius_m)
    wanted = requested if requested is not None else min(8.0, DEFAULT_IMAGE_PIXELS / (2.0 * radius_m))
    return round(max(0.5, min(wanted, cap, MAX_PX_PER_M)), 4)


def make_transform(centre_x_cm: float, centre_y_cm: float, radius_m: float, px_per_m: float) -> ViewTransform:
    size = max(1, int(round(2.0 * radius_m * px_per_m)))
    return ViewTransform(centre_x_cm - radius_m * 100.0, centre_y_cm - radius_m * 100.0, px_per_m, size, size)


def grid_step_m(radius_m: float) -> int:
    """The smallest round step that leaves about a dozen lines across the view."""
    wanted = 2.0 * radius_m / 12.0
    return next((step for step in GRID_STEPS_M if step >= wanted), GRID_STEPS_M[-1])


# ------------------------------------------------------------ graph analysis

def _ground(graph: DetourTopologyGraph) -> Iterable[tuple[int, DetourPolygon]]:
    return ((ref, polygon) for ref, polygon in graph.polygons.items() if polygon.poly_type == DT_POLYTYPE_GROUND)


def _distance_cm(a: Vec3, b: Vec3) -> float:
    return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2)


def _horizontal_cm(a: Vec3, b: Vec3) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _point(value: Vec3) -> dict[str, float]:
    return {"x": round(value.x, 1), "y": round(value.y, 1), "z": round(value.z, 1)}


def _point_m(value: Vec3) -> dict[str, float]:
    return {"x": round(value.x / 100.0, 2), "y": round(value.y / 100.0, 2), "z": round(value.z / 100.0, 2)}


def graph_bounds(graph: DetourTopologyGraph) -> tuple[float, float, float, float]:
    """(min_x, min_y, max_x, max_y) of every ground vertex, in centimetres."""
    cached = getattr(graph, "_explore_bounds", None)
    if cached is not None:
        return cached
    xs, ys = [], []
    for _, polygon in _ground(graph):
        for vertex in polygon.vertices:
            xs.append(vertex.x)
            ys.append(vertex.y)
    bounds = (min(xs), min(ys), max(xs), max(ys)) if xs else (0.0, 0.0, 0.0, 0.0)
    try:
        graph._explore_bounds = bounds
    except AttributeError:
        pass
    return bounds


def island_sizes(labels: Mapping[int, int]) -> dict[int, int]:
    sizes: dict[int, int] = {}
    for owner in labels.values():
        sizes[owner] = sizes.get(owner, 0) + 1
    return sizes


def _planner_portals(graph: DetourTopologyGraph, reference: int):
    """Use the planner's permitted, directed traversal links when available."""
    outgoing = getattr(graph, "outgoing_portals", None)
    return outgoing(reference) if callable(outgoing) else graph.adjacency.get(reference, ())


def reachable_references(graph: DetourTopologyGraph, start_reference: int,
                         labels: Mapping[int, int] | None = None) -> set[int]:
    """Polygons linked to ``start_reference`` through the planner's portals.

    Follow the same directed portals and traversal policy as the planner.
    Ordinary island labels alone do not include off-mesh connections.
    """
    reached = {start_reference}
    pending = [start_reference]
    while pending:
        for portal in _planner_portals(graph, pending.pop()):
            neighbour = portal.to_reference
            if neighbour in graph.polygons and neighbour not in reached:
                reached.add(neighbour)
                pending.append(neighbour)
    return reached


def geodesic_costs(graph: DetourTopologyGraph, start_reference: int) -> dict[int, float]:
    """Walking distance (cm) from the start polygon to each polygon linked to it."""
    best = {start_reference: 0.0}
    heap = [(0.0, start_reference)]
    while heap:
        cost, reference = heapq.heappop(heap)
        if cost > best.get(reference, math.inf):
            continue
        here = graph.polygons[reference].centroid
        for portal in _planner_portals(graph, reference):
            neighbour = graph.polygons.get(portal.to_reference)
            if neighbour is None:
                continue
            middle = Vec3((portal.left.x + portal.right.x) / 2.0, (portal.left.y + portal.right.y) / 2.0,
                          (portal.left.z + portal.right.z) / 2.0)
            step = _distance_cm(here, middle) + _distance_cm(middle, neighbour.centroid)
            if cost + step < best.get(portal.to_reference, math.inf):
                best[portal.to_reference] = cost + step
                heapq.heappush(heap, (cost + step, portal.to_reference))
    return best


def _edge_clearance_cm(point: Vec3, polygon: DetourPolygon) -> float:
    best = math.inf
    vertices = polygon.vertices
    for index, first in enumerate(vertices):
        second = vertices[(index + 1) % len(vertices)]
        dx, dy = second.x - first.x, second.y - first.y
        length_sq = dx * dx + dy * dy
        amount = 0.0 if length_sq <= 0 else min(1.0, max(0.0, ((point.x - first.x) * dx + (point.y - first.y) * dy) / length_sq))
        best = min(best, math.hypot(point.x - (first.x + dx * amount), point.y - (first.y + dy * amount)))
    return best


def _on_polygon(graph: DetourTopologyGraph, polygon: DetourPolygon, point: Vec3) -> Vec3:
    """``point`` with the height of the polygon's surface at its x/y (nearest edge point outside)."""
    _, closest, _, _, _ = graph._candidate_resolution(point, polygon)
    return closest


def _inset(graph: DetourTopologyGraph, polygon: DetourPolygon, point: Vec3, clearance_cm: float) -> Vec3:
    """Move ``point`` toward the polygon's centre until it is ``clearance_cm`` inside (or at the centre)."""
    centre = polygon.centroid
    if _edge_clearance_cm(point, polygon) >= clearance_cm:
        return point
    for step in range(1, 11):
        amount = step / 10.0
        moved = Vec3(point.x + (centre.x - point.x) * amount, point.y + (centre.y - point.y) * amount, point.z)
        if _edge_clearance_cm(moved, polygon) >= clearance_cm or step == 10:
            return _on_polygon(graph, polygon, moved)
    return point


def _boundary_edges(graph: DetourTopologyGraph, reference: int) -> list[int]:
    """Edge indices of the polygon that have no portal to another polygon."""
    polygon = graph.polygons[reference]
    linked = {portal.source_edge for portal in graph.adjacency.get(reference, ())}
    return [index for index in range(len(polygon.vertices)) if index not in linked]


def _window_references(graph: DetourTopologyGraph, centre: Vec3, radius_m: float) -> list[int]:
    """Ground polygons whose bounding box touches the square window round ``centre``."""
    reach = radius_m * 100.0
    found = []
    for ref in graph._point_resolution_references(centre, radius_m * 1.5):
        polygon = graph.polygons.get(ref)
        if polygon is None or polygon.poly_type != DT_POLYTYPE_GROUND or not polygon.vertices:
            continue
        xs = [v.x for v in polygon.vertices]
        ys = [v.y for v in polygon.vertices]
        if max(xs) < centre.x - reach or min(xs) > centre.x + reach or max(ys) < centre.y - reach or min(ys) > centre.y + reach:
            continue
        found.append(ref)
    return found


def locate_player(graph: DetourTopologyGraph, position: Vec3, *, snap_m: float = 3.0, vertical_m: float = 3.0):
    """The player's polygon with generous bounds (a view reads, a route is stricter)."""
    return graph.resolve_point(position, max_snap_distance_m=snap_m, max_vertical_separation_m=vertical_m)


def route_start_ok(resolution: Any) -> bool:
    """Whether the route adapter would accept this start (0.75 m snap, 1 m height, inside a polygon)."""
    return bool(resolution.ok and resolution.reason_code == "point_inside_polygon"
                and (resolution.horizontal_distance_m or 0.0) <= 0.75 and (resolution.vertical_distance_m or 0.0) <= 1.0)


def _far_end_candidates(graph: DetourTopologyGraph, geodesic: Mapping[int, float], window: Sequence[int],
                        player: Vec3, limit: int, spacing_cm: float, min_path_cm: float) -> list[dict[str, Any]]:
    pool = [(ref, graph.polygons[ref].centroid) for ref in window if ref in geodesic and geodesic[ref] >= min_path_cm]
    chosen: list[tuple[int, Vec3]] = []

    def weighted(a: Vec3, b: Vec3) -> float:
        return math.sqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (HEIGHT_WEIGHT * (a.z - b.z)) ** 2)

    gaps = {ref: weighted(centre, player) for ref, centre in pool}
    while pool and len(chosen) < limit:
        ref, centre = max(pool, key=lambda item: gaps[item[0]])
        if gaps[ref] < spacing_cm:
            break
        chosen.append((ref, centre))
        pool = [item for item in pool if item[0] != ref]
        for other_ref, other in pool:
            gaps[other_ref] = min(gaps[other_ref], weighted(other, centre))
    return [{"kind": "far_end", "reference": ref, "point": centre, "path_cm": geodesic[ref],
             "note": "far end of the walkable area in view"} for ref, centre in chosen]


def _capture_edge_candidates(graph: DetourTopologyGraph, geodesic: Mapping[int, float], window: Sequence[int],
                             limit: int, spacing_cm: float) -> list[dict[str, Any]]:
    min_x, min_y, max_x, max_y = graph_bounds(graph)
    found = []
    for ref in window:
        if ref not in geodesic:
            continue
        polygon = graph.polygons[ref]
        for index in _boundary_edges(graph, ref):
            first, second = polygon.edge(index)
            for side, axis, edge in (("-x", "x", min_x), ("+x", "x", max_x), ("-y", "y", min_y), ("+y", "y", max_y)):
                values = (getattr(first, axis), getattr(second, axis))
                if all(abs(value - edge) <= CAPTURE_EDGE_MARGIN_CM for value in values):
                    middle = Vec3((first.x + second.x) / 2.0, (first.y + second.y) / 2.0, (first.z + second.z) / 2.0)
                    found.append((geodesic[ref], ref, side,
                                  _inset(graph, polygon, _on_polygon(graph, polygon, middle), 100.0)))
    chosen: list[dict[str, Any]] = []
    for path, ref, side, point in sorted(found, key=lambda item: -item[0]):
        if all(_horizontal_cm(point, kept["point"]) >= spacing_cm for kept in chosen):
            chosen.append({"kind": "capture_edge", "reference": ref, "point": point, "path_cm": path, "side": side,
                           "note": f"the captured navmesh ends here ({side}); more may lie beyond - "
                                   "run start_topology_capture there"})
        if len(chosen) >= limit:
            break
    return chosen


def _island_candidates(graph: DetourTopologyGraph, labels: Mapping[int, int], reachable: Mapping[int, float],
                       window: Sequence[int], player: Vec3, limit: int) -> list[dict[str, Any]]:
    sizes = island_sizes(labels)
    nearest: dict[int, tuple[float, Vec3, int]] = {}
    for ref in window:
        label = labels.get(ref)
        if label is None or ref in reachable or sizes.get(label, 0) < MIN_ISLAND_POLYGONS:
            continue
        _, closest, horizontal_cm, vertical_cm, _ = graph._candidate_resolution(player, graph.polygons[ref])
        score = math.hypot(horizontal_cm, vertical_cm)
        if label not in nearest or score < nearest[label][0]:
            nearest[label] = (score, closest, ref)
    result = []
    for label, (score, closest, ref) in sorted(nearest.items(), key=lambda item: item[1][0])[:limit]:
        polygon = graph.polygons[ref]
        result.append({"kind": "other_island", "reference": ref,
                       "point": _inset(graph, polygon, closest, EDGE_CLEARANCE_CM),
                       "island": label, "island_polygons": sizes[label], "path_cm": None,
                       "note": "separate navmesh island: not reachable on the mesh (a door or gap may join it)"})
    return result


def _nearest_ground_point(graph: DetourTopologyGraph, target: Vec3, within_m: float, *, reference_z: float
                          ) -> tuple[int, Vec3, float] | None:
    best: tuple[tuple[float, float], int, Vec3, float] | None = None
    for ref in graph._point_resolution_references(target, within_m):
        polygon = graph.polygons.get(ref)
        if polygon is None or polygon.poly_type != DT_POLYTYPE_GROUND:
            continue
        _, closest, horizontal_cm, _, _ = graph._candidate_resolution(Vec3(target.x, target.y, reference_z), polygon)
        if horizontal_cm > within_m * 100.0:
            continue
        key = (horizontal_cm, abs(closest.z - reference_z))
        if best is None or key < best[0]:
            best = (key, ref, closest, horizontal_cm)
    if best is None:
        return None
    return best[1], _inset(graph, graph.polygons[best[1]], best[2], EDGE_CLEARANCE_CM), best[3]


def _poi_candidate(graph: DetourTopologyGraph, poi: Mapping[str, Any], labels: Mapping[int, int],
                   start_label: int | None, geodesic: Mapping[int, float]) -> dict[str, Any] | None:
    position = poi.get("position")
    try:
        target = Vec3(float(position["x"]), float(position["y"]), float(position["z"]))
    except (KeyError, TypeError, ValueError):
        return None
    snapped = _nearest_ground_point(graph, target, 6.0, reference_z=target.z)
    if snapped is None:
        return None
    ref, point, horizontal_cm = snapped
    reachable = ref in geodesic
    return {"kind": "door" if poi.get("kind") == "door" else "poi", "reference": ref, "point": point,
            "path_cm": geodesic.get(ref) if reachable else None,
            "label": poi.get("label"), "poi_kind": poi.get("kind"), "poi_position": _point(target),
            "poi_snap_m": round(horizontal_cm / 100.0, 2),
            "note": poi.get("note") or "known point of interest, snapped to the nearest walkable floor"}


@dataclass
class ViewAnalysis:
    player: Vec3
    yaw_deg: float | None
    start_reference: int | None
    start_resolution: Any
    labels: Mapping[int, int]
    start_label: int | None
    geodesic: Mapping[int, float]
    window: list[int]
    candidates: list[dict[str, Any]]
    islands: list[dict[str, Any]]
    island_polygons: int
    bounds_cm: tuple[float, float, float, float]

    @property
    def pois(self) -> list[dict[str, Any]]:
        return [c for c in self.candidates if c["kind"] in ("poi", "door")]


def analyse_view(graph: DetourTopologyGraph, player: Vec3, yaw_deg: float | None, *, centre: Vec3 | None = None,
                 radius_m: float = DEFAULT_RADIUS_M, max_candidates: int = DEFAULT_MAX_CANDIDATES,
                 pois: Sequence[Mapping[str, Any]] = ()) -> ViewAnalysis:
    """Where the player can walk in the window, and the destinations worth offering."""
    centre = centre or player
    labels = island_labels(graph)
    resolution = locate_player(graph, player)
    start_ref = int(resolution.selected_reference) if resolution.ok else None
    start_label = labels.get(start_ref) if start_ref is not None else None
    geodesic = geodesic_costs(graph, start_ref) if start_ref is not None else {}
    window = _window_references(graph, centre, radius_m)
    reach = radius_m * 100.0
    candidate_window = [ref for ref in window
                        if centre.x - reach <= graph.polygons[ref].centroid.x < centre.x + reach
                        and centre.y - reach <= graph.polygons[ref].centroid.y < centre.y + reach]
    max_candidates = max(1, min(int(max_candidates), MAX_CANDIDATES))
    spacing_cm = max(800.0, radius_m * 100.0 / 5.0)
    found: list[dict[str, Any]] = []
    if start_ref is not None:
        found += _far_end_candidates(graph, geodesic, candidate_window, player, max_candidates, spacing_cm, 500.0)
        found += _capture_edge_candidates(graph, geodesic, window, 3, spacing_cm * 1.5)
    reach = radius_m * 100.0
    for poi in pois:
        item = _poi_candidate(graph, poi, labels, start_label, geodesic)
        if item is not None and abs(item["point"].x - centre.x) <= reach and abs(item["point"].y - centre.y) <= reach:
            found.append(item)
    found += _island_candidates(graph, labels, geodesic, window, player, MAX_ISLAND_CANDIDATES)
    found = [item for item in found
             if centre.x - reach <= item["point"].x < centre.x + reach
             and centre.y - reach <= item["point"].y < centre.y + reach]
    sizes = island_sizes(labels)
    deadline = time.monotonic() + PLAN_REFINEMENT_SECONDS
    candidates: list[dict[str, Any]] = []
    for number, item in enumerate(found, start=1):
        point: Vec3 = item["point"]
        reachable = item["reference"] in geodesic
        path_source = "estimate"
        if reachable and item.get("path_cm") is not None and time.monotonic() < deadline:
            planned = graph.plan(player, point, max_snap_distance_m=3.0, max_vertical_separation_m=3.0,
                                 start_resolution_override=resolution)
            if planned.status == "complete" and planned.total_estimated_path_length_m is not None:
                item["path_cm"] = planned.total_estimated_path_length_m * 100.0
                path_source = "planner"
        bearing = math.degrees(math.atan2(point.y - player.y, point.x - player.x))
        entry: dict[str, Any] = {
            "id": number, "kind": item["kind"], "reachable": bool(reachable),
            "position": _point(point), "position_m": _point_m(point),
            "distance_m": round(_horizontal_cm(point, player) / 100.0, 1),
            # walking distance: the planner's funnelled path, or (past its time budget) an estimate
            # through polygon centres and portal middles that runs a little long
            "path_m": round(item["path_cm"] / 100.0, 1) if item.get("path_cm") is not None and reachable else None,
            "path_source": path_source if reachable else None,
            "height_vs_player_m": round((point.z - player.z) / 100.0, 1),
            "bearing_deg": round(bearing, 1),
            "relative_to_facing_deg": round((bearing - yaw_deg + 180.0) % 360.0 - 180.0, 1) if yaw_deg is not None else None,
            "note": item["note"],
        }
        for key in ("label", "poi_kind", "poi_position", "poi_snap_m", "side", "island", "island_polygons"):
            if item.get(key) is not None:
                entry[key] = item[key]
        entry["_point"] = point
        candidates.append(entry)
    visible = sorted({labels[ref] for ref in window if ref in labels})
    reached_labels = {labels[ref] for ref in geodesic if ref in labels}
    islands = [{"id": label, "polygons": sizes[label], "reachable": label in reached_labels}
               for label in visible if sizes[label] >= MIN_ISLAND_POLYGONS or label == start_label]
    islands.sort(key=lambda item: (not item["reachable"], -item["polygons"]))
    return ViewAnalysis(player, yaw_deg, start_ref, resolution, labels, start_label, geodesic, window, candidates,
                        islands[:12], sizes.get(start_label, 0) if start_label is not None else 0, graph_bounds(graph))


def public_candidates(analysis: ViewAnalysis) -> list[dict[str, Any]]:
    """The candidates without their internal geometry, ready for JSON."""
    return [{key: value for key, value in item.items() if not key.startswith("_")} for item in analysis.candidates]


# ----------------------------------------------------------------- rendering

def render_view(graph: DetourTopologyGraph, analysis: ViewAnalysis, transform: ViewTransform, *,
                radius_m: float, show_islands: bool = True,
                extra_pois: Sequence[Mapping[str, Any]] = ()) -> tuple[bytes, dict[str, Any]]:
    """The PNG for ``analysis`` in ``transform``; returns it with drawing facts."""
    canvas = Canvas(transform.width, transform.height, (18, 18, 22))
    step = grid_step_m(radius_m)
    scale = transform.px_per_m
    # The colour ramp spans the player's own floors (a far plateau must not wash them out);
    # other floors beyond it take the end colours.
    own = [graph.polygons[ref].centroid.z for ref in analysis.window
           if ref in analysis.geodesic]
    heights = own or [graph.polygons[ref].centroid.z for ref in analysis.window]
    margin = 100.0 if own else 0.0
    z_min = (min(heights) - margin) if heights else 0.0
    z_max = (max(heights) + margin) if heights else 1.0
    if z_max - z_min < 300.0:
        z_max = z_min + 300.0

    def pixel(vertex: Vec3) -> tuple[float, float]:
        return transform.to_pixel(vertex.x, vertex.y)

    # grid first, under the floors
    left_m, top_m = transform.left_cm / 100.0, transform.top_cm / 100.0
    right_m = left_m + transform.width / scale
    bottom_m = top_m + transform.height / scale
    first_x = math.ceil(left_m / step) * step
    first_y = math.ceil(top_m / step) * step
    grid_x = [first_x + k * step for k in range(int((right_m - first_x) // step) + 1)]
    grid_y = [first_y + k * step for k in range(int((bottom_m - first_y) // step) + 1)]
    for value in grid_x:
        column = int(round((value - left_m) * scale))
        for row in range(transform.height):
            canvas.put(column, row, (44, 44, 56))
    for value in grid_y:
        canvas.span(int(round((value - top_m) * scale)), 0, transform.width, (44, 44, 56))

    ordered = sorted(analysis.window, key=lambda ref: graph.polygons[ref].centroid.z)
    for ref in ordered:
        polygon = graph.polygons[ref]
        base = _ramp((polygon.centroid.z - z_min) / (z_max - z_min))
        reachable = ref in analysis.geodesic
        if show_islands and not reachable and analysis.start_label is not None:
            base = _blend(base, (105, 105, 118), 0.6)
        canvas.fill_polygon([pixel(v) for v in polygon.vertices], base)
    faint = _blend(_ramp(0.5), (18, 18, 22), 0.75)
    for ref in ordered:
        polygon = graph.polygons[ref]
        reachable = ref in analysis.geodesic
        boundary = set(_boundary_edges(graph, ref))
        for index in range(len(polygon.vertices)):
            first, second = polygon.edge(index)
            (xa, ya), (xb, yb) = pixel(first), pixel(second)
            if index in boundary:
                canvas.line(xa, ya, xb, yb, (235, 235, 235) if reachable or not show_islands else (230, 90, 90))
            else:
                canvas.line(xa, ya, xb, yb, faint)

    for poi in extra_pois:
        position = poi.get("position") or {}
        try:
            px, py = transform.to_pixel(float(position["x"]), float(position["y"]))
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= px < transform.width and 0 <= py < transform.height:
            canvas.diamond(px, py, 5, (150, 150, 160))

    for entry in analysis.candidates:
        point: Vec3 = entry["_point"]
        px, py = pixel(point)
        if not (0 <= px < transform.width and 0 <= py < transform.height):
            continue
        colour = KIND_COLOURS.get(entry["kind"], (255, 255, 255))
        if entry["kind"] == "other_island":
            canvas.cross(px, py, 6, colour)
        elif entry["kind"] in ("poi", "door"):
            canvas.diamond(px, py, 7, colour)
        else:
            canvas.ring(px, py, 7, colour, 3)
        canvas.text(int(px) + 10, int(py) - 7, str(entry["id"]), colour)

    if transform.contains(analysis.player.x, analysis.player.y):
        px, py = pixel(analysis.player)
        if analysis.yaw_deg is not None:
            heading = math.radians(analysis.yaw_deg)
            tip = (px + math.cos(heading) * 42, py + math.sin(heading) * 42)
            canvas.line(px, py, tip[0], tip[1], (255, 80, 80), 3)
            for wing in (150, -150):
                angle = heading + math.radians(wing)
                canvas.line(tip[0], tip[1], tip[0] + math.cos(angle) * 11, tip[1] + math.sin(angle) * 11,
                            (255, 80, 80), 3)
        canvas.disc(px, py, 7, (0, 0, 0))
        canvas.disc(px, py, 5, (255, 255, 255))

    # axis labels over everything: world X (metres) along the top, world Y down the left
    label_gap = max(1, int(math.ceil(46 / (step * scale))))
    for index, value in enumerate(grid_x):
        if index % label_gap == 0:
            canvas.text(int(round((value - left_m) * scale)) + 3, 3, f"{value:g}")
    for index, value in enumerate(grid_y):
        if index % label_gap == 0:
            canvas.text(3, int(round((value - top_m) * scale)) + 3, f"{value:g}")
    canvas.text(transform.width // 2 - 8, 20, "X m", (255, 255, 120))
    canvas.text(3, transform.height // 2 - 7, "Y m", (255, 255, 120))
    if transform.width >= 240 and transform.height >= 120:
        # bottom-right, clear of the Y labels down the left edge
        bar_width, bar_x, bar_y = 160, transform.width - 160 - 56, transform.height - 36
        for offset in range(bar_width):
            colour = _ramp(offset / (bar_width - 1))
            for row in range(10):
                canvas.put(bar_x + offset, bar_y + row, colour)
        canvas.text(bar_x, bar_y + 14, f"{z_min / 100.0:.1f}")
        high = f"{z_max / 100.0:.1f}"
        canvas.text(bar_x + bar_width - (len(high) * 12 - 2), bar_y + 14, high)
        canvas.text(bar_x + bar_width + 8, bar_y - 1, "Z m", (255, 255, 120))
    return canvas.png(), {"polygons_drawn": len(analysis.window), "z_min_cm": round(z_min, 1),
                          "z_max_cm": round(z_max, 1), "grid_step_m": step}


# ------------------------------------------------------------------ snapping

def nearest_reachable_point(graph: DetourTopologyGraph, reachable: set[int], target: Vec3,
                            reference_z: float | None = None) -> dict[str, Any] | None:
    """The point on the reachable floor horizontally nearest ``target`` (with its distance)."""
    best: tuple[tuple[float, float], int, Vec3] | None = None
    probe = Vec3(target.x, target.y, target.z if reference_z is None else reference_z)
    for ref in reachable:
        polygon = graph.polygons.get(ref)
        if polygon is None or polygon.poly_type != DT_POLYTYPE_GROUND:
            continue
        centre = polygon.centroid
        radius = max(math.hypot(v.x - centre.x, v.y - centre.y) for v in polygon.vertices)
        if best is not None and _horizontal_cm(centre, probe) - radius > best[0][0]:
            continue
        _, closest, horizontal_cm, vertical_cm, _ = graph._candidate_resolution(probe, polygon)
        score = (horizontal_cm, vertical_cm)
        if best is None or score < best[0]:
            best = (score, ref, closest)
    if best is None:
        return None
    _, ref, closest = best
    point = _inset(graph, graph.polygons[ref], closest, EDGE_CLEARANCE_CM)
    return {"position": _point(point), "position_m": _point_m(point), "reference": ref,
            "distance_m": round(_horizontal_cm(point, target) / 100.0, 2)}


def snap_target(graph: DetourTopologyGraph, requested: Vec3, *, z_given: bool, player: Vec3,
                reachable: set[int], labels: Mapping[int, int], max_snap_m: float = 6.0,
                max_vertical_m: float = 3.0) -> dict[str, Any]:
    """Snap a requested point onto the floor the player can walk to.

    ``status`` is ``ok`` with the snapped ``point`` (on a polygon's surface,
    kept off its edge where it can be), the polygon ``reference`` and the
    snap distances, or ``refused`` with a ``reason`` and the nearest
    reachable point. When the request has no height, the player's height
    breaks ties between stacked floors.
    """
    probe = Vec3(requested.x, requested.y, requested.z if z_given else player.z)
    candidates = []
    for ref in graph._point_resolution_references(probe, max_snap_m):
        polygon = graph.polygons.get(ref)
        if polygon is None or polygon.poly_type != DT_POLYTYPE_GROUND:
            continue
        _, closest, horizontal_cm, vertical_cm, contained = graph._candidate_resolution(probe, polygon)
        if horizontal_cm > max_snap_m * 100.0:
            continue
        if z_given and vertical_cm > max_vertical_m * 100.0:
            continue
        candidates.append({"reference": ref, "closest": closest, "horizontal_cm": horizontal_cm,
                           "vertical_cm": vertical_cm, "contained": contained, "reachable": ref in reachable})
    near = [c for c in candidates if c["horizontal_cm"] <= NEAR_FLOOR_CM]
    near_reachable = [c for c in near if c["reachable"]]
    warnings: list[str] = []
    chosen = None
    if z_given and near:
        # An explicit height names a floor. Do not silently replace an
        # unreachable upper/lower floor with a reachable one within 3 m.
        closest_floor = min(near, key=lambda c: (c["vertical_cm"], c["horizontal_cm"]))
        if closest_floor["reachable"]:
            chosen = closest_floor
    elif near_reachable:
        chosen = min(near_reachable, key=lambda c: (c["vertical_cm"], c["horizontal_cm"]))
        if any(not c["reachable"] and c["vertical_cm"] < chosen["vertical_cm"] - 1.0 for c in near):
            warnings.append("a closer floor at this spot is on another navmesh island; the reachable floor was chosen")
    elif not near:
        reachable_any = [c for c in candidates if c["reachable"]]
        if reachable_any:
            chosen = min(reachable_any, key=lambda c: (c["horizontal_cm"], c["vertical_cm"]))
            if any(not c["reachable"] and c["horizontal_cm"] < chosen["horizontal_cm"] for c in candidates):
                warnings.append("a nearer walkable floor is on another navmesh island; the reachable floor was chosen")
    if chosen is None:
        fallback = nearest_reachable_point(graph, reachable, requested, None if z_given else player.z)
        if candidates:
            target_ref = min(near or candidates, key=lambda c: (c["vertical_cm"], c["horizontal_cm"])
                             if z_given else (c["horizontal_cm"], c["vertical_cm"]))["reference"]
            owner = labels.get(target_ref)
            return {"status": "refused", "reason": "target_on_unreachable_island",
                    "message": "that spot is walkable but on a navmesh island the player cannot reach on the mesh; "
                               "a door, gap or traversal may join it",
                    "target_island": {"id": owner, "polygons": sum(1 for v in labels.values() if v == owner)},
                    "nearest_reachable": fallback}
        return {"status": "refused", "reason": "target_off_navmesh",
                "message": f"no walkable floor within {max_snap_m:g} m of that spot",
                "nearest_reachable": fallback}
    polygon = graph.polygons[chosen["reference"]]
    point = _inset(graph, polygon, chosen["closest"], EDGE_CLEARANCE_CM)
    return {"status": "ok", "point": point, "reference": chosen["reference"], "contained": chosen["contained"],
            "snap_horizontal_m": round(_horizontal_cm(point, requested) / 100.0, 2),
            "snap_vertical_m": round(abs(point.z - (requested.z if z_given else player.z)) / 100.0, 2),
            "warnings": warnings}
