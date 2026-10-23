#!/usr/bin/env python3
"""Offline topology analyzer, deterministic corpus builder, and RoutePlan oracle.

This module intentionally has no dependency on the planner, UE4SS, Unreal, or
any live-game API.  It consumes only the preserved JSON snapshot and future
RoutePlan-shaped JSON data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable


INTERNAL_LINK_SIDE = 128
EXTERNAL_LINK_SIDES = frozenset({0, 2, 4, 6})
SHIPPING_DYNAMIC_LINK_EDGE = 0xFF
SHIPPING_DYNAMIC_LINK_FLAG = 0x40
SHIPPING_DYNAMIC_EDGE_ZERO_SIDES = frozenset({0xD0, 0xF0})
SHIPPING_DYNAMIC_EDGE_ONE_SIDES = frozenset(
    set(range(0x40, 0x48))
    | {0x60, 0x61, 0x62, 0x66, 0x67, 0xC0, 0xD0, 0xE0}
)
MAX_SHIPPING_TILE_INDEX = (1 << 14) - 1
MAX_SHIPPING_POLY_INDEX = (1 << 31) - 1
MAX_SHIPPING_SALT = (1 << (64 - 14 - 31)) - 1
PORTAL_TOLERANCE_CM = 5.0
# Off-mesh traversal links.  This oracle keeps its own copy of the rules the
# planner follows: steps and drops are accepted (climbs and long spans are
# not: Cloud cannot climb a bare ledge or jump a gap at will), they are
# crossed start to end, and a plan that declares anything else is invalid.
# Steps and drops joined step-overs on 2026-10-21: the Mt. Nibel caves go
# down by drops.
TRAVERSAL_ALLOWED_CLASSES = frozenset({"step_over", "step_down", "step_up", "drop"})
TRAVERSAL_STEP_OVER_MAX_VERTICAL_CM = 50.0
TRAVERSAL_STEP_OVER_MAX_HORIZONTAL_CM = 600.0
TRAVERSAL_STEP_MAX_VERTICAL_CM = 150.0
TRAVERSAL_TWO_WAY_FLAG = 0x80
# Walking a two-way connection backwards (end to start) is not verified live.
TRAVERSAL_REVERSE_ALLOWED = False
TRAVERSAL_POINT_TOLERANCE_CM = 5.0
TRAVERSAL_PORTAL_SIDE = 0x40
DEFAULT_SNAPSHOT = (
    Path(__file__).resolve().parents[2]
    / "ProjectKnowledge"
    / "Evidence"
    / "2026-08-17-shipping-detour-topology-live"
    / "navigation-native-geometry.json"
)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _point(values: Iterable[Any]) -> list[float]:
    result = [float(value) for value in values]
    if len(result) != 3 or not all(_finite(value) for value in result):
        raise ValueError(f"expected three finite coordinates, got {result!r}")
    return result


def _distance(a: Iterable[float], b: Iterable[float]) -> float:
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


def _mean(points: list[list[float]]) -> list[float]:
    if not points:
        return [0.0, 0.0, 0.0]
    return [sum(point[index] for point in points) / len(points) for index in range(3)]


def _transform_recast_to_unreal(point: list[float]) -> list[float]:
    # Snapshot contract: x=-recast.x, y=-recast.z, z=recast.y.
    return [-point[0], -point[2], point[1]]


def _ref_parts(reference: int, tile_bits: int, poly_bits: int) -> dict[str, int]:
    value = int(reference)
    poly_mask = (1 << poly_bits) - 1
    tile_mask = (1 << tile_bits) - 1
    return {
        "reference": value,
        "tile_index": (value >> poly_bits) & tile_mask,
        "poly_index": value & poly_mask,
        "salt": value >> (tile_bits + poly_bits),
    }


def _compose_ref(tile_index: int, poly_index: int, salt: int, tile_bits: int, poly_bits: int) -> int:
    return (int(salt) << (tile_bits + poly_bits)) | (int(tile_index) << poly_bits) | int(poly_index)


def _shipping_dynamic_subtype(
    edge: Any, side: Any, bmin: Any = 0, bmax: Any = 0,
) -> str | None:
    if (
        not isinstance(edge, int) or isinstance(edge, bool)
        or not isinstance(side, int) or isinstance(side, bool)
        or not isinstance(bmin, int) or isinstance(bmin, bool)
        or not isinstance(bmax, int) or isinstance(bmax, bool)
    ):
        return None
    if edge == SHIPPING_DYNAMIC_LINK_EDGE and (side & SHIPPING_DYNAMIC_LINK_FLAG) != 0:
        return "edge_ff"
    if edge == 0 and side in SHIPPING_DYNAMIC_EDGE_ZERO_SIDES:
        return "edge_zero" if bmin == 0 and bmax == 0 else "invalid_edge_zero"
    if edge == 1 and side in SHIPPING_DYNAMIC_EDGE_ONE_SIDES:
        return "edge_one" if bmin == 0 and bmax == 0 else "invalid_edge_one"
    return None


def _is_shipping_dynamic_link(
    edge: Any, side: Any, bmin: Any = 0, bmax: Any = 0,
) -> bool:
    return _shipping_dynamic_subtype(edge, side, bmin, bmax) in {
        "edge_ff", "edge_zero", "edge_one",
    }


def _direct_tile_identity(tile: dict[str, Any]) -> tuple[str, tuple[int, int] | None, str | None]:
    marker = tile.get("tile_index_valid", "__missing__")
    if marker == "__missing__" or marker is False:
        return "missing", None, None
    if marker is not True:
        return "invalid", None, "tile_index_valid_not_boolean_true"
    tile_index, salt = tile.get("tile_index"), tile.get("salt")
    if (
        not isinstance(tile_index, int) or isinstance(tile_index, bool)
        or not 0 <= tile_index <= MAX_SHIPPING_TILE_INDEX
        or not isinstance(salt, int) or isinstance(salt, bool)
        or not 1 <= salt <= MAX_SHIPPING_SALT
    ):
        return "invalid", None, "tile_index_or_salt_out_of_shipping_range"
    reference = _compose_ref(tile_index, 0, salt, 14, 31)
    if reference <= 0 or _ref_parts(reference, 14, 31)["tile_index"] != tile_index or _ref_parts(reference, 14, 31)["salt"] != salt:
        return "invalid", None, "tile_identity_reference_round_trip_failed"
    return "valid", (int(tile_index), int(salt)), None


def _json_point(point: Any) -> list[float] | None:
    if isinstance(point, dict):
        values = [point.get("x"), point.get("y"), point.get("z")]
    elif isinstance(point, (list, tuple)):
        values = list(point)
    else:
        return None
    if len(values) != 3 or not all(_finite(value) for value in values):
        return None
    return [float(value) for value in values]


def _point_distance(a: list[float] | None, b: list[float] | None) -> float | None:
    if a is None or b is None:
        return None
    return _distance(a, b)


def _subtract(a: list[float], b: list[float]) -> list[float]:
    return [float(a[index]) - float(b[index]) for index in range(3)]


def _cross_xy(a: list[float], b: list[float]) -> float:
    return float(a[0]) * float(b[1]) - float(a[1]) * float(b[0])


def _lerp(a: list[float], b: list[float], amount: float) -> list[float]:
    return [float(a[index]) + (float(b[index]) - float(a[index])) * float(amount) for index in range(3)]


def _source_portal_endpoints(source: dict[str, Any], link: dict[str, Any]) -> tuple[list[float], list[float]] | None:
    """Reconstruct a shipping link interval from raw source geometry.

    This is deliberately local oracle code.  It consumes only the serialized
    source polygon and link fields and does not call the planner's portal
    helpers.
    """
    if int(source.get("type", -1)) != 0:
        return None
    vertices = source.get("vertices_unreal") or []
    edge = link.get("edge")
    side = link.get("side")
    if _is_shipping_dynamic_link(edge, side, link.get("bmin", 0), link.get("bmax", 0)):
        # Valid shipping dynamic links are not ordinary bmin/bmax portals.
        return None
    if not isinstance(edge, int) or edge < 0 or edge >= len(vertices):
        return None
    first = vertices[edge]
    second = vertices[(edge + 1) % len(vertices)]
    if not isinstance(side, int):
        return None
    if side == INTERNAL_LINK_SIDE:
        return [list(first), list(second)]
    if side not in EXTERNAL_LINK_SIDES:
        return None
    bmin, bmax = link.get("bmin"), link.get("bmax")
    if not isinstance(bmin, int) or not isinstance(bmax, int) or not (0 <= bmin <= bmax <= 255) or bmax <= bmin:
        return None
    return _lerp(first, second, bmin / 255.0), _lerp(first, second, bmax / 255.0)


def _traversal_class(horizontal_cm: float, vertical_cm: float) -> str:
    """The geometric class of an off-mesh link from its length and rise."""
    if abs(vertical_cm) <= TRAVERSAL_STEP_OVER_MAX_VERTICAL_CM:
        return "step_over" if horizontal_cm <= TRAVERSAL_STEP_OVER_MAX_HORIZONTAL_CM else "long_span"
    if horizontal_cm > TRAVERSAL_STEP_OVER_MAX_HORIZONTAL_CM:
        return "drop" if vertical_cm < 0.0 else "climb"
    if vertical_cm < 0.0:
        return "step_down" if vertical_cm >= -TRAVERSAL_STEP_MAX_VERTICAL_CM else "drop"
    return "step_up" if vertical_cm <= TRAVERSAL_STEP_MAX_VERTICAL_CM else "climb"


def _point_segment_distance_xy(point: list[float], first: list[float], second: list[float]) -> float:
    dx = float(second[0]) - float(first[0])
    dy = float(second[1]) - float(first[1])
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(float(point[0]) - float(first[0]), float(point[1]) - float(first[1]))
    amount = ((float(point[0]) - float(first[0])) * dx + (float(point[1]) - float(first[1])) * dy) / length_sq
    amount = max(0.0, min(1.0, amount))
    closest = [float(first[0]) + amount * dx, float(first[1]) + amount * dy, 0.0]
    return math.hypot(float(point[0]) - closest[0], float(point[1]) - closest[1])


def _matching_target_edge(target: dict[str, Any], first: list[float], second: list[float], tolerance_cm: float = PORTAL_TOLERANCE_CM) -> int:
    vertices = target.get("vertices_unreal") or []
    for edge in range(len(vertices)):
        target_first = vertices[edge]
        target_second = vertices[(edge + 1) % len(vertices)]
        if (
            _point_segment_distance_xy(first, target_first, target_second) <= tolerance_cm
            and _point_segment_distance_xy(second, target_first, target_second) <= tolerance_cm
        ):
            return edge
    return -1


def _independent_portal_geometry(source: dict[str, Any], target: dict[str, Any], link: dict[str, Any]) -> dict[str, Any] | None:
    endpoints = _source_portal_endpoints(source, link)
    if endpoints is None or int(target.get("type", -1)) != 0:
        return None
    first, second = endpoints
    target_edge = _matching_target_edge(target, first, second)
    direction = _subtract(target["centroid_unreal"], source["centroid_unreal"])
    edge_direction = _subtract(second, first)
    if _cross_xy(direction, edge_direction) < 0.0:
        left, right = first, second
    else:
        left, right = second, first
    return {
        "left": left,
        "right": right,
        "target_edge": target_edge,
        "source_edge": link.get("edge"),
        "side": link.get("side"),
    }


class TopologyAnalyzer:
    """Parse and analyze the detailed, serialized portion of the proof snapshot."""

    def __init__(self, snapshot_path: Path):
        self.snapshot_path = Path(snapshot_path).resolve()
        self.raw: dict[str, Any] = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        proof = self.raw.get("topology_proof") or {}
        self.tile_bits = int(proof.get("tile_bits", self.raw.get("tile_bits", 14)))
        self.poly_bits = int(proof.get("poly_bits", self.raw.get("poly_bits", 31)))
        self.tiles: list[dict[str, Any]] = []
        self.nodes: dict[str, dict[str, Any]] = {}
        self.reference_to_node: dict[int, str] = {}
        self.edges: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.directed_edges: list[dict[str, Any]] = []
        self.off_mesh: list[dict[str, Any]] = []
        self.unusual_links: Counter[str] = Counter()
        self.identity_diagnostics: Counter[str] = Counter()
        self.dynamic_links: Counter[str] = Counter()
        self._parse_tiles()
        self._parse_edges()
        self.proof_reference_entries: list[dict[str, Any]] = []
        for key in ("nearby_polygon", "same_tile_adjacent", "cross_tile_adjacent"):
            entry = self.proof.get(key)
            if isinstance(entry, dict) and isinstance(entry.get("reference"), int):
                self.proof_reference_entries.append(dict(entry))
                self._node_for_reference(int(entry["reference"]))
        self._infer_geometry_identities()
        self._build_strict_index()
        self.traversal_by_owner: dict[int, list[dict[str, Any]]] = defaultdict(list)
        self._build_traversal_records()

    def _build_traversal_records(self) -> None:
        """The off-mesh connections that name a landing polygon at each end.

        The off-mesh polygon's ``edge 0`` dynamic link leads to the polygon
        the connection starts on and its ``edge 1`` link to the one it ends
        on.  A connection missing either, or naming two, is not a record.
        """
        landings: dict[str, dict[int, list[int]]] = defaultdict(lambda: {0: [], 1: []})
        for edge in self.directed_edges:
            subtype = edge.get("shipping_dynamic_subtype")
            if subtype == "edge_zero":
                landings[edge["source"]][0].append(int(edge["target_reference"]))
            elif subtype == "edge_one":
                landings[edge["source"]][1].append(int(edge["target_reference"]))
        for item in self.off_mesh:
            owner = item.get("owner_polygon_ref")
            poly = item.get("poly")
            if not isinstance(owner, int) or not isinstance(poly, int) or not item.get("tile_key"):
                continue
            node_id = self._node_key(item["tile_key"], poly)
            found = landings.get(node_id)
            if not found or len(found[0]) != 1 or len(found[1]) != 1:
                continue
            node = self.nodes.get(node_id) or {}
            vertices = node.get("vertices_unreal") or []
            start, end = (
                (list(vertices[0]), list(vertices[1])) if len(vertices) == 2
                else (item.get("start_unreal"), item.get("end_unreal"))
            )
            if start is None or end is None:
                continue
            flags = item.get("flags")
            self.traversal_by_owner[owner].append({
                "owner_ref": owner,
                "start_ref": found[0][0],
                "end_ref": found[1][0],
                "start": [float(value) for value in start],
                "end": [float(value) for value in end],
                "flags": flags,
                "two_way": isinstance(flags, int) and bool(flags & TRAVERSAL_TWO_WAY_FLAG),
            })

    @property
    def proof(self) -> dict[str, Any]:
        return self.raw.get("topology_proof") or {}

    @property
    def snapshot_sha256(self) -> str:
        return hashlib.sha256(self.snapshot_path.read_bytes()).hexdigest()

    @staticmethod
    def _tile_key(tile: dict[str, Any]) -> str:
        return f"tile:{int(tile['x'])}:{int(tile['y'])}:{int(tile['layer'])}"

    @staticmethod
    def _node_key(tile_key: str, poly_index: int) -> str:
        return f"{tile_key}/poly:{int(poly_index)}"

    def _decode_link(self, link: dict[str, Any]) -> dict[str, int] | None:
        raw_ref = link.get("ref")
        if not isinstance(raw_ref, int) or raw_ref <= 0:
            self.unusual_links["non_positive_or_non_integer_ref"] += 1
            return None
        return _ref_parts(raw_ref, self.tile_bits, self.poly_bits)

    def _parse_tiles(self) -> None:
        for raw_tile in _as_list(self.raw.get("tiles")):
            if not isinstance(raw_tile, dict):
                continue
            tile = dict(raw_tile)
            tile_key = self._tile_key(tile)
            self.tiles.append(tile)
            same_refs: list[dict[str, int]] = []
            for polygon in _as_list(tile.get("polygons")):
                if not isinstance(polygon, dict):
                    continue
                for link in _as_list(polygon.get("links")):
                    if isinstance(link, dict) and int(link.get("side", -1)) == INTERNAL_LINK_SIDE:
                        decoded = self._decode_link(link)
                        if decoded:
                            same_refs.append(decoded)
            link_identities = {
                (entry["tile_index"], entry["salt"])
                for entry in same_refs
                if 0 <= entry["tile_index"] <= MAX_SHIPPING_TILE_INDEX
                and 1 <= entry["salt"] <= MAX_SHIPPING_SALT
                and 0 <= entry["poly_index"] < len(_as_list(tile.get("polygons")))
            }
            direct_status, direct_identity, direct_reason = _direct_tile_identity(tile)
            tile_index: int | None = None
            salt: int | None = None
            identity_source = "unresolved"
            identity_blocked = False
            if direct_status == "valid":
                if link_identities and link_identities != {direct_identity}:
                    self.identity_diagnostics["disagreements"] += 1
                    identity_blocked = True
                else:
                    tile_index, salt = direct_identity
                    identity_source = "native_pool"
                    self.identity_diagnostics["native_pool"] += 1
            elif direct_status == "invalid":
                self.identity_diagnostics["invalid_direct"] += 1
                self.identity_diagnostics[direct_reason or "invalid_direct"] += 1
                identity_blocked = True
            elif len(link_identities) == 1:
                tile_index, salt = next(iter(link_identities))
                identity_source = "inferred_internal_link"
                self.identity_diagnostics["inferred_internal_link"] += 1
            elif len(link_identities) > 1:
                self.unusual_links["conflicting_same_tile_ref_identity"] += 1
                self.identity_diagnostics["disagreements"] += 1
                identity_blocked = True
            else:
                self.identity_diagnostics["unresolved"] += 1
            tile["_tile_key"] = tile_key
            tile["_inferred_tile_index"] = tile_index
            tile["_inferred_salt"] = salt
            tile["_identity_source"] = identity_source
            tile["_identity_blocked"] = identity_blocked
            for polygon in _as_list(tile.get("polygons")):
                if not isinstance(polygon, dict):
                    continue
                poly_index = int(polygon.get("index", -1))
                if poly_index < 0:
                    continue
                node_id = self._node_key(tile_key, poly_index)
                recast_vertices: list[list[float]] = []
                for vertex_index in _as_list(polygon.get("vertex_indices")):
                    try:
                        recast_vertices.append(_point(tile["vertices"][int(vertex_index)].values()))
                    except (KeyError, IndexError, TypeError, ValueError):
                        self.unusual_links["invalid_polygon_vertex_reference"] += 1
                if not recast_vertices:
                    recast_vertices = []
                unreal_vertices = [_transform_recast_to_unreal(point) for point in recast_vertices]
                reference = None
                if tile_index is not None and salt is not None:
                    reference = _compose_ref(tile_index, poly_index, salt, self.tile_bits, self.poly_bits)
                reference_valid = reference is not None and not identity_blocked and 0 <= poly_index <= MAX_SHIPPING_POLY_INDEX
                if not reference_valid:
                    reference = None
                node = {
                    "id": node_id,
                    "kind": "serialized_polygon",
                    "identity_source": identity_source if reference is not None else "unresolved",
                    "reference": reference,
                    "reference_parts": _ref_parts(reference, self.tile_bits, self.poly_bits) if reference is not None else None,
                    "tile_index": tile_index,
                    "salt": salt,
                    "tile": {"x": int(tile["x"]), "y": int(tile["y"]), "layer": int(tile["layer"])},
                    "tile_key": tile_key,
                    "lookup_index": int(tile.get("lookup_index", -1)),
                    "polygon_index": poly_index,
                    "vertex_count": int(polygon.get("vertex_count", len(recast_vertices))),
                    "vertices_recast": recast_vertices,
                    "vertices_unreal": unreal_vertices,
                    "centroid_recast": _mean(recast_vertices),
                    "centroid_unreal": _mean(unreal_vertices),
                    "flags": polygon.get("flags"),
                    "area": polygon.get("area"),
                    "type": polygon.get("type"),
                    "neighbors": [int(value) for value in _as_list(polygon.get("neighbors")) if isinstance(value, int)],
                    "identity_blocked": identity_blocked,
                }
                self.nodes[node_id] = node
                if reference is not None:
                    prior = self.reference_to_node.get(reference)
                    if prior is None:
                        self.reference_to_node[reference] = node_id
                    elif prior != node_id:
                        self.unusual_links["duplicate_reference_identity"] += 1
            for off_mesh in _as_list(tile.get("off_mesh_connections")):
                if not isinstance(off_mesh, dict):
                    continue
                item = dict(off_mesh)
                item["tile"] = {"x": int(tile["x"]), "y": int(tile["y"]), "layer": int(tile["layer"])}
                item["tile_key"] = tile_key
                owner_ref = None
                if tile_index is not None and salt is not None and isinstance(item.get("poly"), int):
                    owner_ref = _compose_ref(tile_index, int(item["poly"]), salt, self.tile_bits, self.poly_bits)
                item["owner_polygon_ref"] = owner_ref
                item["start_recast"] = _json_point(item.get("start"))
                item["end_recast"] = _json_point(item.get("end"))
                item["start_unreal"] = _transform_recast_to_unreal(item["start_recast"]) if item["start_recast"] else None
                item["end_unreal"] = _transform_recast_to_unreal(item["end_recast"]) if item["end_recast"] else None
                self.off_mesh.append(item)

    def _node_for_reference(self, reference: int) -> str:
        node_id = self.reference_to_node.get(reference)
        if node_id is not None:
            return node_id
        node_id = f"ref:{int(reference)}"
        if node_id not in self.nodes:
            self.nodes[node_id] = {
                "id": node_id,
                "kind": "reference_only_target",
                "reference": int(reference),
                "reference_parts": _ref_parts(int(reference), self.tile_bits, self.poly_bits),
                "tile_index": _ref_parts(int(reference), self.tile_bits, self.poly_bits)["tile_index"],
                "salt": _ref_parts(int(reference), self.tile_bits, self.poly_bits)["salt"],
                "tile": None,
                "tile_key": None,
                "polygon_index": _ref_parts(int(reference), self.tile_bits, self.poly_bits)["poly_index"],
                "vertices_recast": [],
                "vertices_unreal": [],
                "centroid_recast": None,
                "centroid_unreal": None,
                "neighbors": [],
            }
        return node_id

    def _add_edge(self, source: str, target: str, evidence: str) -> None:
        if source == target:
            self.unusual_links["self_loop"] += 1
            return
        pair = tuple(sorted((source, target)))
        self.edges[pair].add(evidence)

    def _parse_edges(self) -> None:
        for tile in self.tiles:
            tile_key = tile["_tile_key"]
            for polygon in _as_list(tile.get("polygons")):
                if not isinstance(polygon, dict):
                    continue
                source_id = self._node_key(tile_key, int(polygon.get("index", -1)))
                if source_id not in self.nodes:
                    continue
                for neighbor in _as_list(polygon.get("neighbors")):
                    if not isinstance(neighbor, int) or neighbor <= 0:
                        continue
                    # DT_EXT_LINK is 0x8000 in the serialized neighbor field;
                    # low values are same-tile polygon indices.
                    if neighbor & 0x8000:
                        continue
                    target_id = self._node_key(tile_key, neighbor)
                    if target_id in self.nodes:
                        self._add_edge(source_id, target_id, "same_tile_neighbor")
                for link in _as_list(polygon.get("links")):
                    if not isinstance(link, dict):
                        continue
                    decoded = self._decode_link(link)
                    if decoded is None:
                        continue
                    target_ref = decoded["reference"]
                    side = link.get("side")
                    edge_value = link.get("edge")
                    bmin = link.get("bmin", 0)
                    bmax = link.get("bmax", 0)
                    dynamic_subtype = _shipping_dynamic_subtype(
                        edge_value, side, bmin, bmax
                    )
                    excluded = False
                    if dynamic_subtype in {"invalid_edge_zero", "invalid_edge_one"}:
                        evidence = f"invalid_{dynamic_subtype}"
                        self.dynamic_links["invalid"] += 1
                        self.dynamic_links[dynamic_subtype] += 1
                        self.unusual_links[evidence] += 1
                        excluded = True
                    elif _is_shipping_dynamic_link(edge_value, side, bmin, bmax):
                        evidence = "shipping_dynamic_link_unresolved"
                        self.dynamic_links["valid"] += 1
                        self.dynamic_links["route_semantics_unresolved"] += 1
                        self.dynamic_links[dynamic_subtype] += 1
                        self.dynamic_links["adjacency_excluded"] += 1
                        excluded = True
                    elif side == INTERNAL_LINK_SIDE:
                        evidence = "same_tile_link"
                    elif 0 <= side <= 7:
                        evidence = "cross_tile_link"
                    else:
                        evidence = "unusual_link_side"
                        self.unusual_links["unusual_side_value"] += 1
                        excluded = True
                    if excluded:
                        self.directed_edges.append(
                            {
                                "source": source_id,
                                "target": None,
                                "target_reference": target_ref,
                                "target_reference_parts": decoded,
                                "side": side,
                                "edge": edge_value,
                                "next": link.get("next"),
                                "bmin": link.get("bmin"),
                                "bmax": link.get("bmax"),
                                "evidence": evidence,
                                "shipping_dynamic_subtype": dynamic_subtype,
                                "route_semantics_unresolved": evidence == "shipping_dynamic_link_unresolved",
                            }
                        )
                        continue
                    target_id = self._node_for_reference(target_ref)
                    self._add_edge(source_id, target_id, evidence)
                    self.directed_edges.append(
                        {
                            "source": source_id,
                            "target": target_id,
                            "target_reference": target_ref,
                            "target_reference_parts": decoded,
                            "side": side,
                            "edge": link.get("edge"),
                            "next": link.get("next"),
                            "bmin": bmin,
                            "bmax": bmax,
                            "evidence": evidence,
                            "route_semantics_unresolved": False,
                        }
                    )

    def _valid_geometry_node(self, node: dict[str, Any]) -> bool:
        return (
            node.get("kind") == "serialized_polygon"
            and isinstance(node.get("reference"), int)
            and 3 <= len(node.get("vertices_unreal") or []) <= 6
            and isinstance(node.get("centroid_unreal"), list)
        )

    def _infer_geometry_identities(self) -> None:
        """Resolve uniquely geometry-confirmed identities without planner code."""
        unresolved = {
            node_id
            for node_id, node in self.nodes.items()
            if node.get("kind") == "serialized_polygon"
            and node.get("reference") is None
            and not node.get("identity_blocked")
        }
        if not unresolved:
            return
        for _ in range(len(unresolved) + 1):
            candidates_by_poly: dict[int, list[str]] = defaultdict(list)
            nodes_by_tile: dict[str, list[str]] = defaultdict(list)
            for node_id in sorted(unresolved):
                node = self.nodes[node_id]
                if 3 <= len(node.get("vertices_unreal") or []) <= 6:
                    candidates_by_poly[int(node.get("polygon_index", -1))].append(node_id)
                nodes_by_tile[str(node["tile_key"])].append(node_id)
            proposals: dict[str, set[tuple[int, int]]] = defaultdict(set)
            for edge in self.directed_edges:
                if edge.get("evidence") != "same_tile_link" and edge.get("evidence") != "cross_tile_link":
                    continue
                source = self.nodes.get(edge["source"])
                if source is None or not isinstance(source.get("reference"), int) or not self._valid_geometry_node(source):
                    continue
                poly_index = (edge.get("target_reference_parts") or {}).get("poly_index")
                if not isinstance(poly_index, int):
                    continue
                for node_id in candidates_by_poly.get(poly_index, []):
                    target = self.nodes[node_id]
                    geometry = _independent_portal_geometry(source, target, edge)
                    if geometry is not None and geometry["target_edge"] >= 0:
                        target_parts = edge["target_reference_parts"]
                        proposals[str(target["tile_key"])].add((int(target_parts["tile_index"]), int(target_parts["salt"])))
            added = 0
            for tile_key, identities in sorted(proposals.items()):
                if len(identities) != 1:
                    continue
                tile_index, salt = next(iter(identities))
                tile_added = True
                for node_id in nodes_by_tile[tile_key]:
                    node = self.nodes[node_id]
                    reference = _compose_ref(tile_index, int(node["polygon_index"]), salt, self.tile_bits, self.poly_bits)
                    prior = self.reference_to_node.get(reference)
                    if prior is not None and prior != node_id:
                        tile_added = False
                        break
                if not tile_added:
                    continue
                for node_id in nodes_by_tile[tile_key]:
                    node = self.nodes[node_id]
                    reference = _compose_ref(tile_index, int(node["polygon_index"]), salt, self.tile_bits, self.poly_bits)
                    node["reference"] = reference
                    node["reference_parts"] = _ref_parts(reference, self.tile_bits, self.poly_bits)
                    node["tile_index"] = node["reference_parts"]["tile_index"]
                    node["salt"] = node["reference_parts"]["salt"]
                    node["identity_source"] = "geometry_consistent_link"
                    self.reference_to_node[reference] = node_id
                    unresolved.discard(node_id)
                    added += 1
            if not added:
                break

    def _build_strict_index(self) -> None:
        """Build the directed, geometry-backed graph used only by the oracle."""
        self.strict_nodes: dict[int, str] = {}
        for node_id, node in self.nodes.items():
            reference = node.get("reference")
            if self._valid_geometry_node(node) and isinstance(reference, int):
                self.strict_nodes[int(reference)] = node_id
        self.strict_links_by_source: dict[int, list[dict[str, Any]]] = defaultdict(list)
        self.strict_adjacency: dict[int, set[int]] = defaultdict(set)
        for edge in self.directed_edges:
            source = self.nodes.get(edge["source"])
            if source is None or not isinstance(source.get("reference"), int):
                continue
            if edge.get("evidence") not in {"same_tile_link", "cross_tile_link"}:
                continue
            source_ref = int(source["reference"])
            target_ref = int(edge["target_reference"])
            if source_ref not in self.strict_nodes or target_ref not in self.strict_nodes or source_ref == target_ref:
                continue
            if edge.get("side") != INTERNAL_LINK_SIDE and edge.get("side") not in EXTERNAL_LINK_SIDES:
                continue
            target = self.nodes[self.strict_nodes[target_ref]]
            geometry = _independent_portal_geometry(source, target, edge)
            if geometry is None:
                continue
            strict_edge = dict(edge)
            strict_edge.update({"source_ref": source_ref, "target_ref": target_ref, "geometry": geometry})
            self.strict_links_by_source[source_ref].append(strict_edge)
            self.strict_adjacency[source_ref].add(target_ref)
        for source_ref in self.strict_links_by_source:
            self.strict_links_by_source[source_ref].sort(key=lambda item: (int(item["target_ref"]), int(item.get("edge", -1)), int(item.get("next", -1))))

    def strict_graph(self) -> dict[int, set[int]]:
        return {reference: set(self.strict_adjacency.get(reference, set())) for reference in self.strict_nodes}

    def _strict_components(self) -> list[list[int]]:
        graph = self.strict_graph()
        reverse: dict[int, set[int]] = defaultdict(set)
        for source, targets in graph.items():
            for target in targets:
                reverse[target].add(source)
        undirected = {reference: set(graph.get(reference, set())) | reverse.get(reference, set()) for reference in graph}
        seen: set[int] = set()
        components: list[list[int]] = []
        for start in sorted(undirected):
            if start in seen:
                continue
            queue = [start]
            seen.add(start)
            component: list[int] = []
            while queue:
                reference = queue.pop()
                component.append(reference)
                for neighbor in sorted(undirected[reference]):
                    if neighbor not in seen:
                        seen.add(neighbor)
                        queue.append(neighbor)
            components.append(sorted(component))
        return sorted(components, key=lambda item: (-len(item), item[0]))

    def _strict_bfs(self, start: int) -> tuple[dict[int, int], dict[int, int | None]]:
        graph = self.strict_graph()
        distances = {start: 0}
        previous: dict[int, int | None] = {start: None}
        queue: deque[int] = deque([start])
        while queue:
            current = queue.popleft()
            for neighbor in sorted(graph.get(current, set())):
                if neighbor not in distances:
                    distances[neighbor] = distances[current] + 1
                    previous[neighbor] = current
                    queue.append(neighbor)
        return distances, previous

    def _graph(self) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = {node_id: set() for node_id in self.nodes}
        for left, right in self.edges:
            graph[left].add(right)
            graph[right].add(left)
        return graph

    def _components(self) -> list[list[str]]:
        graph = self._graph()
        seen: set[str] = set()
        components: list[list[str]] = []
        for start in sorted(graph):
            if start in seen:
                continue
            queue = [start]
            seen.add(start)
            component: list[str] = []
            while queue:
                node = queue.pop()
                component.append(node)
                for neighbor in sorted(graph[node]):
                    if neighbor not in seen:
                        seen.add(neighbor)
                        queue.append(neighbor)
            components.append(sorted(component))
        return sorted(components, key=lambda item: (-len(item), item[0]))

    def _bfs(self, start: str) -> tuple[dict[str, int], dict[str, str | None]]:
        graph = self._graph()
        distances = {start: 0}
        previous: dict[str, str | None] = {start: None}
        queue: deque[str] = deque([start])
        while queue:
            node = queue.popleft()
            for neighbor in sorted(graph[node]):
                if neighbor not in distances:
                    distances[neighbor] = distances[node] + 1
                    previous[neighbor] = node
                    queue.append(neighbor)
        return distances, previous

    def _local_exact_nodes(self) -> list[str]:
        return sorted(
            node_id
            for node_id, node in self.nodes.items()
            if node.get("kind") == "serialized_polygon" and isinstance(node.get("reference"), int)
        )

    def _edge_has(self, left: str, right: str, evidence: str) -> bool:
        return evidence in self.edges.get(tuple(sorted((left, right))), set())

    def _bounds(self) -> dict[str, Any]:
        serialized_nodes = [node for node in self.nodes.values() if node.get("kind") == "serialized_polygon"]
        recast_vertices = [point for node in serialized_nodes for point in node.get("vertices_recast", [])]
        unreal_vertices = [point for node in serialized_nodes for point in node.get("vertices_unreal", [])]

        def bounds(points: list[list[float]]) -> dict[str, list[float] | None]:
            if not points:
                return {"min": None, "max": None}
            return {"min": [min(point[index] for point in points) for index in range(3)], "max": [max(point[index] for point in points) for index in range(3)]}

        tile_coords = [node["tile"] for node in serialized_nodes if node.get("tile")]
        return {
            "tile_coordinate_extents": {
                "x": [min(item["x"] for item in tile_coords), max(item["x"] for item in tile_coords)] if tile_coords else None,
                "y": [min(item["y"] for item in tile_coords), max(item["y"] for item in tile_coords)] if tile_coords else None,
                "layer": [min(item["layer"] for item in tile_coords), max(item["layer"] for item in tile_coords)] if tile_coords else None,
            },
            "vertex_extents_recast": bounds(recast_vertices),
            "vertex_extents_unreal": bounds(unreal_vertices),
        }

    def _stacked_candidates(self) -> list[dict[str, Any]]:
        groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for tile in self.tiles:
            groups[(int(tile["x"]), int(tile["y"]))].append(tile)
        result = []
        for (x, y), tiles in sorted(groups.items()):
            if len(tiles) < 2:
                continue
            result.append(
                {
                    "x": x,
                    "y": y,
                    "layers": sorted(int(tile["layer"]) for tile in tiles),
                    "tile_count": len(tiles),
                    "polygon_count": sum(int(tile.get("poly_count", 0)) for tile in tiles),
                    "tile_keys": sorted(tile["_tile_key"] for tile in tiles),
                }
            )
        return result

    def _diameter_approximation(self, components: list[list[str]]) -> dict[str, Any]:
        if not components:
            return {"hops": None, "start_reference": None, "end_reference": None, "method": "none"}
        candidates = [node_id for node_id in components[0] if node_id in self._local_exact_nodes()]
        if not candidates:
            return {"hops": None, "start_reference": None, "end_reference": None, "method": "no-exact-local-endpoints"}
        current = candidates[0]
        best = (0, current, current)
        for _ in range(8):
            distances, _ = self._bfs(current)
            farthest = max(
                (node for node in candidates if node in distances),
                key=lambda node: (distances[node], node),
            )
            if distances[farthest] <= best[0] and farthest == current:
                break
            best = (distances[farthest], current, farthest)
            current = farthest
        return {
            "hops": int(best[0]),
            "start_reference": self.nodes[best[1]].get("reference"),
            "end_reference": self.nodes[best[2]].get("reference"),
            "method": "bounded-eight-sweep-BFS-lower-bound/approximation",
        }

    def summary(self) -> dict[str, Any]:
        components = self._components()
        graph = self._graph()
        strict_graph = self.strict_graph()
        strict_components = self._strict_components()
        serialized_nodes = [node for node in self.nodes.values() if node.get("kind") == "serialized_polygon"]
        local_exact = self._local_exact_nodes()
        native_identity_count = len([node for node in serialized_nodes if node.get("identity_source") == "native_pool"])
        inferred_identity_count = len([node for node in serialized_nodes if node.get("identity_source") == "inferred_internal_link"])
        unresolved_identity_count = len([node for node in serialized_nodes if node.get("identity_source") == "unresolved"])
        degree_values = [len(graph[node_id]) for node_id in graph]
        degree_distribution = dict(sorted(Counter(degree_values).items()))
        same_edges = [pair for pair, evidence in self.edges.items() if any("same_tile" in item for item in evidence)]
        cross_edges = [pair for pair, evidence in self.edges.items() if "cross_tile_link" in evidence]
        unknown_target_edges = [pair for pair in cross_edges if any(self.nodes[node_id].get("kind") == "reference_only_target" for node_id in pair)]
        tile_indices = [node["tile_index"] for node in serialized_nodes if isinstance(node.get("tile_index"), int)]
        side_distribution = Counter(int(edge.get("side", -1)) for edge in self.directed_edges)
        low_degree = sorted((node_id for node_id in graph if len(graph[node_id]) <= 1))[:20]
        high_degree = sorted(graph, key=lambda node_id: (-len(graph[node_id]), node_id))[:20]
        strict_degree_values = [len(strict_graph[reference]) for reference in strict_graph]
        strict_tile_crossings = sum(
            _ref_parts(source, self.tile_bits, self.poly_bits)["tile_index"] != _ref_parts(target, self.tile_bits, self.poly_bits)["tile_index"]
            for source, targets in strict_graph.items()
            for target in targets
        )
        return {
            "source": {
                "path": str(self.snapshot_path),
                "sha256": self.snapshot_sha256,
                "source_field": self.raw.get("source"),
                "geometry_mode": self.raw.get("geometry_mode"),
                "coordinate_space": self.raw.get("coordinate_space"),
                "coordinate_transform_to_unreal": self.raw.get("coordinate_transform_to_unreal"),
                "movement_sent": self.raw.get("movement_sent"),
            },
            "coverage": {
                "serialized_tile_count": len(self.tiles),
                "serialized_polygon_count": len(serialized_nodes),
                "serialized_directed_link_count": len(self.directed_edges),
                "serialized_unique_graph_node_count": len(graph),
                "serialized_exact_source_ref_count": len(local_exact),
                "serialized_internal_identity_ref_count": inferred_identity_count,
                "serialized_geometry_consistent_ref_count": 0,
                "serialized_native_pool_identity_ref_count": native_identity_count,
                "serialized_unresolved_identity_ref_count": unresolved_identity_count,
                "serialized_unique_target_ref_count": len({edge["target_reference"] for edge in self.directed_edges}),
                "embedded_proof_tiles_considered": self.proof.get("tiles_considered"),
                "embedded_proof_polygon_count": self.proof.get("polygons_valid"),
                "embedded_proof_same_tile_link_count": self.proof.get("same_tile_links"),
                "embedded_proof_cross_tile_link_count": self.proof.get("cross_tile_links"),
                "embedded_proof_reference_count": len(self.proof_reference_entries),
                "detail_export_is_complete": bool(self.raw.get("exported_tile_count_matches")),
            },
            "reference_encoding": {
                "tile_bits": self.tile_bits,
                "poly_bits": self.poly_bits,
                "source_refs_inferred_from_internal_link_identity": inferred_identity_count,
                "source_refs_from_native_pool_identity": native_identity_count,
                "reference_only_targets": len([node for node in self.nodes.values() if node.get("kind") == "reference_only_target"]),
                "geometry_consistent_identity_count": 0,
                "identity_unresolved_count": unresolved_identity_count,
                "identity_disagreements": self.identity_diagnostics.get("disagreements", 0),
                "identity_invalid_direct_count": self.identity_diagnostics.get("invalid_direct", 0),
                "identity_diagnostics": dict(self.identity_diagnostics),
            },
            "graph": {
                "node_count_including_reference_only_targets": len(graph),
                "undirected_edge_count": len(self.edges),
                "same_tile_edge_count": len(same_edges),
                "cross_tile_edge_count": len(cross_edges),
                "directed_same_tile_link_count": sum(edge.get("evidence") == "same_tile_link" for edge in self.directed_edges),
                "directed_cross_tile_link_count": sum(edge.get("evidence") == "cross_tile_link" for edge in self.directed_edges),
                "shipping_dynamic_link_count": self.dynamic_links.get("valid", 0),
                "shipping_dynamic_route_semantics_unresolved_count": self.dynamic_links.get("route_semantics_unresolved", 0),
                "shipping_dynamic_adjacency_excluded_count": self.dynamic_links.get("adjacency_excluded", 0),
                "shipping_dynamic_edge_zero_valid_count": self.dynamic_links.get("edge_zero", 0),
                "shipping_dynamic_edge_one_valid_count": self.dynamic_links.get("edge_one", 0),
                "shipping_dynamic_edge_ff_valid_count": self.dynamic_links.get("edge_ff", 0),
                "shipping_dynamic_invalid_count": self.dynamic_links.get("invalid", 0),
                "cross_tile_edges_with_reference_only_endpoint": len(unknown_target_edges),
                "connected_component_count": len(components),
                "connected_component_sizes_descending": [len(component) for component in components],
                "largest_component_sample_nodes": components[0][:20] if components else [],
                "degree_distribution": degree_distribution,
                "lowest_degree_nodes": [{"node": node_id, "degree": len(graph[node_id]), "reference": self.nodes[node_id].get("reference")} for node_id in low_degree],
                "highest_degree_nodes": [{"node": node_id, "degree": len(graph[node_id]), "reference": self.nodes[node_id].get("reference")} for node_id in high_degree],
                "diameter_approximation": self._diameter_approximation(components),
            },
            "strict_planner_graph": {
                "node_count": len(self.strict_nodes),
                "directed_edge_count": sum(len(targets) for targets in strict_graph.values()),
                "unique_ordered_pair_count": sum(len(targets) for targets in strict_graph.values()),
                "cross_tile_edge_count": strict_tile_crossings,
                "connected_component_count": len(strict_components),
                "connected_component_sizes_descending": [len(component) for component in strict_components],
                "degree_distribution": dict(sorted(Counter(strict_degree_values).items())),
                "identity_sources": dict(sorted(Counter(self.nodes[node_id].get("identity_source", "unknown") for node_id in self.strict_nodes.values()).items())),
                "definition": "Only serialized polygon geometry with an independently reconstructed ref and a directed raw link whose target also has serialized geometry; neighbour-only and undirected evidence excluded.",
            },
            "tile_and_vertical_distribution": {
                **self._bounds(),
                "decoded_source_tile_index_extents": [min(tile_indices), max(tile_indices)] if tile_indices else None,
                "layer_distribution": dict(sorted(Counter(int(tile["layer"]) for tile in self.tiles).items())),
                "stacked_tile_coordinate_group_count": len(self._stacked_candidates()),
                "stacked_tile_coordinate_examples": self._stacked_candidates()[:20],
            },
            "links_and_special_cases": {
                "link_side_distribution": dict(sorted(side_distribution.items())),
                "internal_link_side_value": INTERNAL_LINK_SIDE,
                "unusual_link_counters": dict(sorted(self.unusual_links.items())),
                "off_mesh_connection_count": len(self.off_mesh),
                "off_mesh_owner_ref_known_count": sum(1 for item in self.off_mesh if isinstance(item.get("owner_polygon_ref"), int)),
                "off_mesh_examples": self.off_mesh[:20],
                "embedded_proof_reference_entries": self.proof_reference_entries,
                "top_level_rejection_counters": {
                    key: self.raw.get(key)
                    for key in ("link_pointer_rejections", "link_index_out_of_range", "link_read_failures", "off_mesh_pointer_rejections", "off_mesh_read_failures")
                    if key in self.raw
                },
            },
        }

    def _case_for_nodes(self, case_id: str, category: str, start_id: str, end_id: str, why: str, *, point_a: list[float] | None = None, point_b: list[float] | None = None, expected_connectivity: str | None = None, special: dict[str, Any] | None = None, strict: bool = False) -> dict[str, Any]:
        start = self.nodes[start_id]
        end = self.nodes[end_id]
        if point_a is None:
            point_a = list(start.get("centroid_unreal") or [0.0, 0.0, 0.0])
        if point_b is None:
            point_b = list(end.get("centroid_unreal") or [0.0, 0.0, 0.0])
        start_ref = start.get("reference")
        end_ref = end.get("reference")
        if strict and isinstance(start_ref, int) and isinstance(end_ref, int):
            distances, _ = self._strict_bfs(start_ref)
            hops = distances.get(end_ref)
            connectivity_evidence = "strict_planner_validation"
        else:
            distances, _ = self._bfs(start_id)
            hops = distances.get(end_id)
            connectivity_evidence = "topology_evidence"
        if expected_connectivity is None:
            expected_connectivity = "connected" if hops is not None else "unreachable"
        start_parts = _ref_parts(start_ref, self.tile_bits, self.poly_bits) if isinstance(start_ref, int) else None
        end_parts = _ref_parts(end_ref, self.tile_bits, self.poly_bits) if isinstance(end_ref, int) else None
        case = {
            "case_id": case_id,
            "category": category,
            "point_a": point_a,
            "point_b": point_b,
            "expected_connectivity": expected_connectivity,
            "known_start_polygon_ref": start_ref,
            "known_end_polygon_ref": end_ref,
            "minimum_expected_tile_crossings": 1 if start_parts and end_parts and start_parts["tile_index"] != end_parts["tile_index"] else 0,
            "graph_hop_lower_bound": hops,
            "why_useful": why,
            "evidence_nodes": [start_id, end_id],
            "connectivity_evidence": connectivity_evidence,
        }
        if special:
            case.update(special)
        return case

    def _choose_edge(self, evidence: str) -> tuple[str, str] | None:
        candidates = []
        for left, right in self.edges:
            if evidence not in self.edges[(left, right)]:
                continue
            if left in self.nodes and right in self.nodes and all(self.nodes[node].get("kind") == "serialized_polygon" and isinstance(self.nodes[node].get("reference"), int) for node in (left, right)):
                candidates.append((left, right))
        return sorted(candidates)[0] if candidates else None

    def _choose_strict_edge(self, *, cross_tile: bool) -> tuple[str, str] | None:
        candidates: list[tuple[str, str]] = []
        for source_ref in sorted(self.strict_links_by_source):
            source_id = self.strict_nodes[source_ref]
            source_parts = _ref_parts(source_ref, self.tile_bits, self.poly_bits)
            for edge in self.strict_links_by_source[source_ref]:
                target_ref = int(edge["target_ref"])
                target_id = self.strict_nodes[target_ref]
                target_parts = _ref_parts(target_ref, self.tile_bits, self.poly_bits)
                if bool(source_parts["tile_index"] != target_parts["tile_index"]) == cross_tile:
                    candidates.append((source_id, target_id))
        return sorted(candidates)[0] if candidates else None

    def _choose_route_pair(self, minimum_hops: int, prefer_cross_tile: bool = False, maximum_hops: int | None = None, target_hops: int | None = None, *, strict: bool = False) -> tuple[str, str, int] | None:
        exact = sorted(self.strict_nodes.values()) if strict else self._local_exact_nodes()
        if not exact:
            return None
        graph = self._graph()
        candidates: list[tuple[int, int, str, str]] = []
        for start in exact[:: max(1, len(exact) // 80)]:
            if strict:
                start_ref = int(self.nodes[start]["reference"])
                distances, _ = self._strict_bfs(start_ref)
                endpoints = [(self.strict_nodes[reference], distance) for reference, distance in distances.items() if reference in self.strict_nodes]
            else:
                distances, _ = self._bfs(start)
                endpoints = [(end, distances.get(end)) for end in exact]
            for end, hops in endpoints:
                if hops is None or hops < minimum_hops or (maximum_hops is not None and hops > maximum_hops):
                    continue
                start_parts = self.nodes[start].get("reference_parts")
                end_parts = self.nodes[end].get("reference_parts")
                cross = int(bool(start_parts and end_parts and start_parts["tile_index"] != end_parts["tile_index"]))
                score = (hops, cross, end, start)
                candidates.append((score[0], score[1], score[2], score[3]))
        if not candidates:
            return None
        if target_hops is None:
            candidates.sort(reverse=True)
        else:
            candidates.sort(key=lambda item: (abs(item[0] - target_hops), -item[1], item[2], item[3]))
        if prefer_cross_tile:
            cross_candidates = [item for item in candidates if item[1] == 1]
            if cross_candidates:
                candidates = cross_candidates
        hops, _, end, start = candidates[0]
        return start, end, hops

    def _choose_vertical_pair(self, *, strict: bool = False) -> tuple[str, str] | None:
        groups: dict[tuple[int, int], list[str]] = defaultdict(list)
        node_ids = sorted(self.strict_nodes.values()) if strict else self._local_exact_nodes()
        for node_id in node_ids:
            tile = self.nodes[node_id].get("tile") or {}
            groups[(tile.get("x"), tile.get("y"))].append(node_id)
        pairs: list[tuple[float, str, str]] = []
        for node_ids in groups.values():
            for left_index, left in enumerate(node_ids):
                for right in node_ids[left_index + 1 :]:
                    if self.nodes[left]["tile"]["layer"] == self.nodes[right]["tile"]["layer"]:
                        continue
                    distance = _distance(self.nodes[left]["centroid_unreal"], self.nodes[right]["centroid_unreal"])
                    pairs.append((distance, left, right))
        return (min(pairs)[1], min(pairs)[2]) if pairs else None

    def build_corpus(self) -> dict[str, Any]:
        cases: list[dict[str, Any]] = []
        exact = sorted(self.strict_nodes.values())
        if not exact:
            raise RuntimeError("snapshot has no serialized polygon with strict geometry-backed identity")
        first = exact[0]
        cases.append(self._case_for_nodes("same-polygon-trivial", "trivial_same_polygon", first, first, "Catches endpoint resolution and the one-polygon corridor special case.", point_a=self.nodes[first]["centroid_unreal"], point_b=self.nodes[first]["centroid_unreal"], strict=True))

        same_edge = self._choose_strict_edge(cross_tile=False)
        if same_edge:
            cases.append(self._case_for_nodes("short-same-tile", "short_same_tile", same_edge[0], same_edge[1], "Exercises a genuine directed link-backed local adjacency without a tile boundary.", strict=True))
        else:
            cases.append({"case_id": "short-same-tile", "category": "short_same_tile", "expected_connectivity": "not_available", "why_unavailable": "No same-tile edge had two exact serialized endpoint refs."})

        cross_edge = self._choose_strict_edge(cross_tile=True)
        if cross_edge:
            cases.append(self._case_for_nodes("short-cross-tile", "short_cross_tile", cross_edge[0], cross_edge[1], "Exercises a directed serialized link whose encoded refs identify a cross-tile transition.", strict=True))
        else:
            cases.append({"case_id": "short-cross-tile", "category": "short_cross_tile", "expected_connectivity": "not_available", "why_unavailable": "Cross-tile links exist in the embedded proof, but no detailed cross-tile edge had two exact serialized endpoint refs."})

        medium = self._choose_route_pair(4, prefer_cross_tile=True, maximum_hops=12, target_hops=8, strict=True)
        if medium:
            cases.append(self._case_for_nodes("medium-multi-tile", "medium_multi_tile", medium[0], medium[1], "Exercises several strict directed graph hops and prefers endpoints whose encoded refs differ by tile.", special={"selected_graph_hops": medium[2]}, strict=True))
        else:
            cases.append({"case_id": "medium-multi-tile", "category": "medium_multi_tile", "expected_connectivity": "not_available", "why_unavailable": "The serialized detail did not expose a four-hop exact-ref pair."})

        long_route = self._choose_route_pair(10, prefer_cross_tile=True, strict=True)
        if long_route:
            cases.append(self._case_for_nodes("long-captured-component", "long_route_captured_component", long_route[0], long_route[1], "Stress case spanning much of the largest strict validation component observed in the serialized detail.", special={"selected_graph_hops": long_route[2]}, strict=True))
        else:
            cases.append({"case_id": "long-captured-component", "category": "long_route_captured_component", "expected_connectivity": "not_available", "why_unavailable": "The serialized detail did not expose a ten-hop exact-ref pair."})

        vertical = self._choose_vertical_pair(strict=True)
        if vertical:
            cases.append(self._case_for_nodes("vertical-stacked-ambiguous", "vertically_ambiguous_stacked_nav", vertical[0], vertical[1], "Uses different navigation layers at one tile coordinate to expose projection/layer-selection mistakes.", strict=True))
        else:
            cases.append({"case_id": "vertical-stacked-ambiguous", "category": "vertically_ambiguous_stacked_nav", "expected_connectivity": "not_available", "why_unavailable": "No stacked tile coordinate had two exact polygon refs."})

        boundary = max((node_id for node_id in exact if len(self.nodes[node_id].get("vertices_unreal", [])) >= 2), key=lambda node_id: (len(self.nodes[node_id].get("vertices_unreal", [])), node_id))
        vertices = self.nodes[boundary]["vertices_unreal"]
        boundary_point = [_mean([vertices[0], vertices[1]])[index] for index in range(3)]
        cases.append(self._case_for_nodes("near-boundary-point", "near_boundary_point", boundary, boundary, "Places the request on the midpoint of a captured polygon edge to stress projection tolerances.", point_a=boundary_point, point_b=self.nodes[boundary]["centroid_unreal"], strict=True))

        bounds = self._bounds()["vertex_extents_unreal"]
        maximum = bounds["max"] or [0.0, 0.0, 0.0]
        invalid_point = [maximum[0] + 100000.0, maximum[1] + 100000.0, maximum[2] + 100000.0]
        cases.append({"case_id": "outside-nav-invalid", "category": "deliberately_invalid_outside_nav", "point_a": invalid_point, "point_b": self.nodes[first]["centroid_unreal"], "expected_connectivity": "invalid", "known_start_polygon_ref": None, "known_end_polygon_ref": self.nodes[first]["reference"], "minimum_expected_tile_crossings": None, "graph_hop_lower_bound": None, "why_useful": "Ensures point-to-polygon resolution fails closed instead of inventing a nearest polygon."})

        components = [[self.strict_nodes[reference] for reference in component] for component in self._strict_components()]
        exact_components = []
        exact_set = set(exact)
        for component in components:
            members = [node_id for node_id in component if node_id in exact_set]
            if members:
                exact_components.append(members)
        if len(exact_components) >= 2:
            left, right = exact_components[0][0], exact_components[1][0]
            cases.append(self._case_for_nodes("disconnected-components", "disconnected_unreachable", left, right, "Uses distinct strict graph components; an offline planner must report unreachable rather than fabricate a corridor.", expected_connectivity="unreachable", strict=True))
        else:
            cases.append({"case_id": "disconnected-components", "category": "disconnected_unreachable", "expected_connectivity": "not_available", "connectivity_evidence": "strict_planner_validation", "why_unavailable": "The strict directed graph did not contain two distinct components with geometric endpoints."})

        if self.off_mesh:
            offmesh = next((item for item in self.off_mesh if item.get("start_unreal") and item.get("end_unreal")), self.off_mesh[0])
            cases.append({
                "case_id": "offmesh-special-transition-candidate",
                "category": "special_off_mesh_transition_candidate",
                "point_a": offmesh.get("start_unreal"),
                "point_b": offmesh.get("end_unreal"),
                "expected_connectivity": "special_transition_candidate",
                "known_start_polygon_ref": offmesh.get("owner_polygon_ref"),
                "known_end_polygon_ref": None,
                "minimum_expected_tile_crossings": None,
                "graph_hop_lower_bound": None,
                "special_transition": {"kind": "off_mesh_connection", "owner_polygon_ref": offmesh.get("owner_polygon_ref"), "poly_index": offmesh.get("poly"), "side": offmesh.get("side"), "flags": offmesh.get("flags"), "user_id": offmesh.get("user_id")},
                "why_useful": "Exercises a captured off-mesh connection candidate without claiming that its endpoint projection or route semantics are proven."})
        else:
            cases.append({"case_id": "offmesh-special-transition-candidate", "category": "special_off_mesh_transition_candidate", "expected_connectivity": "not_available", "why_unavailable": "No off-mesh records were serialized."})

        return {
            "schema_version": 1,
            "source_snapshot": {"path": str(self.snapshot_path), "sha256": self.snapshot_sha256},
            "reference_encoding": {"tile_bits": self.tile_bits, "poly_bits": self.poly_bits},
            "generation_contract": "BFS is used only for connectivity/hop classification and case selection; no production route corridor is stored.",
            "case_count": len(cases),
            "cases": cases,
        }

    def analysis_document(self) -> dict[str, Any]:
        summary = self.summary()
        polygons = []
        for node_id in sorted(self.nodes):
            node = self.nodes[node_id]
            if node.get("kind") == "serialized_polygon":
                polygons.append(node)
        edges = []
        for (left, right), evidence in sorted(self.edges.items()):
            left_parts = self.nodes[left].get("reference_parts")
            right_parts = self.nodes[right].get("reference_parts")
            tile_crossing = bool(left_parts and right_parts and left_parts["tile_index"] != right_parts["tile_index"])
            edges.append({"left": left, "right": right, "evidence": sorted(evidence), "tile_crossing": tile_crossing, "left_reference": self.nodes[left].get("reference"), "right_reference": self.nodes[right].get("reference")})
        reference_only_targets = [node for node in self.nodes.values() if node.get("kind") == "reference_only_target"]
        strict_links = [edge for source in sorted(self.strict_links_by_source) for edge in self.strict_links_by_source[source]]
        return {"schema_version": 1, "summary": summary, "polygons": polygons, "reference_only_targets": sorted(reference_only_targets, key=lambda node: int(node["reference"])), "edges": edges, "directed_links": self.directed_edges, "strict_directed_links": strict_links, "off_mesh_connections": self.off_mesh}


def _extract_point(plan: dict[str, Any], *keys: str) -> list[float] | None:
    for key in keys:
        if key in plan:
            point = _json_point(plan.get(key))
            if point is not None:
                return point
    return None


def _extract_refs(plan: dict[str, Any]) -> list[int]:
    raw = plan.get("polygon_corridor")
    if raw is None:
        for key in ("corridor_polygon_refs", "polygon_refs", "corridor", "polygons"):
            if key in plan:
                raw = plan[key]
                break
    refs: list[int] = []
    for item in _as_list(raw):
        if isinstance(item, int) and not isinstance(item, bool):
            refs.append(int(item))
        elif isinstance(item, dict):
            value = next((item.get(key) for key in ("reference", "polygon_ref", "ref", "id") if isinstance(item.get(key), int)), None)
            if value is not None:
                refs.append(int(value))
    return refs


def _extract_waypoints(plan: dict[str, Any]) -> list[list[float]]:
    result: list[list[float]] = []
    for item in _as_list(plan.get("waypoints", [])):
        point = _json_point(item)
        result.append(point if point is not None else [float("nan")] * 3)
    return result


def _polyline_length(points: list[list[float]]) -> float:
    return sum(_distance(left, right) for left, right in zip(points, points[1:]))


class RoutePlanValidator:
    """Independent strict oracle for the shipping DetourRoutePlan contract."""

    def __init__(self, analyzer: TopologyAnalyzer, endpoint_tolerance: float = 250.0, portal_tolerance: float = PORTAL_TOLERANCE_CM):
        self.analyzer = analyzer
        self.endpoint_tolerance = float(endpoint_tolerance)
        self.portal_tolerance = float(portal_tolerance)
        self.known_refs = {int(node["reference"]) for node in analyzer.nodes.values() if isinstance(node.get("reference"), int)}
        self.strict_refs = set(analyzer.strict_nodes)

    def _error(self, errors: list[str], message: str) -> None:
        if message not in errors:
            errors.append(message)

    def _finite_point_field(self, plan: dict[str, Any], field: str, errors: list[str], *, required: bool) -> list[float] | None:
        if field not in plan:
            if required:
                self._error(errors, f"missing {field}")
            return None
        if plan.get(field) is None and not required:
            return None
        point = _json_point(plan.get(field))
        if point is None:
            self._error(errors, f"{field} must contain three finite coordinates")
        return point

    def _validate_endpoint_sentinel(self, portal: Any, index: int, reference: int, point: list[float] | None, errors: list[str], label: str) -> None:
        if not isinstance(portal, dict):
            self._error(errors, f"{label} endpoint sentinel {index} is not an object")
            return
        for field, expected in (("from_ref", reference), ("to_ref", reference), ("link_ref", reference), ("source_edge", -1), ("target_edge", -1), ("side", INTERNAL_LINK_SIDE)):
            if portal.get(field) != expected:
                self._error(errors, f"{label} endpoint sentinel {index} has incorrect {field}: expected {expected}, got {portal.get(field)!r}")
        left = _json_point(portal.get("left"))
        right = _json_point(portal.get("right"))
        if left is None or right is None:
            self._error(errors, f"{label} endpoint sentinel {index} left/right must be finite")
        elif point is not None:
            if _distance(left, point) > self.portal_tolerance or _distance(right, point) > self.portal_tolerance:
                self._error(errors, f"{label} endpoint sentinel {index} does not match resolved endpoint")

    def _validate_transition_portal(self, portal: Any, index: int, source_ref: int, target_ref: int, errors: list[str]) -> None:
        if not isinstance(portal, dict):
            self._error(errors, f"invalid transition portal {index}: not an object")
            return
        expected_link = next((edge for edge in self.analyzer.strict_links_by_source.get(source_ref, []) if int(edge["target_ref"]) == target_ref), None)
        if expected_link is None:
            if source_ref in self.known_refs and target_ref in self.known_refs:
                self._error(errors, f"strict directed source link missing: {source_ref} -> {target_ref}")
            else:
                self._error(errors, f"strict transition references unknown geometry: {source_ref} -> {target_ref}")
            return
        geometry = expected_link["geometry"]
        for field, expected in (("from_ref", source_ref), ("to_ref", target_ref), ("link_ref", int(expected_link["target_reference"])), ("source_edge", expected_link.get("edge")), ("side", expected_link.get("side")), ("target_edge", geometry.get("target_edge"))):
            if portal.get(field) != expected:
                self._error(errors, f"invalid transition portal {index}: {field} expected {expected}, got {portal.get(field)!r}")
        left = _json_point(portal.get("left"))
        right = _json_point(portal.get("right"))
        if left is None or right is None:
            self._error(errors, f"invalid transition portal {index}: left/right must be finite")
            return
        expected_left, expected_right = geometry["left"], geometry["right"]
        if _distance(left, expected_left) > self.portal_tolerance or _distance(right, expected_right) > self.portal_tolerance:
            self._error(errors, f"invalid transition portal {index}: portal interval disagrees with raw source-edge geometry")
        source = self.analyzer.nodes[self.analyzer.strict_nodes[source_ref]]
        raw_endpoints = _source_portal_endpoints(source, expected_link)
        if raw_endpoints is None:
            self._error(errors, f"invalid transition portal {index}: raw source link interval is unsupported")
        else:
            if _distance(left, expected_left) > self.portal_tolerance or _distance(right, expected_right) > self.portal_tolerance:
                self._error(errors, f"invalid transition portal {index}: bmin/bmax clipping mismatch")

    @staticmethod
    def _traversal_positions(refs: list[int], portal_list: list[Any]) -> list[int]:
        """Corridor transitions (index of the source polygon) declared as off-mesh."""
        return [
            index for index in range(len(refs) - 1)
            if index + 1 < len(portal_list)
            and isinstance(portal_list[index + 1], dict)
            and portal_list[index + 1].get("kind") == "traversal"
        ]

    def _validate_traversals(
        self,
        plan: dict[str, Any],
        refs: list[int],
        portal_list: list[Any],
        positions: list[int],
        waypoints: list[list[float]],
        errors: list[str],
    ) -> None:
        raw = plan.get("traversal_segments")
        segments = _as_list(raw) if raw is not None else []
        if len(segments) != len(positions):
            self._error(errors, f"traversal segment count disagrees with corridor: segments={len(segments)}, traversal portals={len(positions)}")
            return
        cursor = 0
        for order, (position, segment) in enumerate(zip(positions, segments)):
            label = f"traversal {order}"
            if not isinstance(segment, dict):
                self._error(errors, f"{label} is not an object")
                continue
            source_ref, target_ref = refs[position], refs[position + 1]
            link_ref = segment.get("link_ref")
            records = self.analyzer.traversal_by_owner.get(link_ref, []) if isinstance(link_ref, int) else []
            if len(records) != 1:
                self._error(errors, f"{label} names no single off-mesh connection: link_ref={link_ref!r}")
                continue
            record = records[0]
            direction = segment.get("direction")
            if direction not in {"start_to_end", "end_to_start"}:
                self._error(errors, f"{label} has an unknown direction: {direction!r}")
                continue
            reverse = direction == "end_to_start"
            if reverse and not (TRAVERSAL_REVERSE_ALLOWED and record["two_way"]):
                self._error(errors, f"{label} crosses its link backwards, which is not verified")
                continue
            expected_from, expected_to = (
                (record["end_ref"], record["start_ref"]) if reverse else (record["start_ref"], record["end_ref"])
            )
            departure, landing = (
                (record["end"], record["start"]) if reverse else (record["start"], record["end"])
            )
            if (source_ref, target_ref) != (expected_from, expected_to):
                self._error(errors, f"{label} does not join the polygons of its connection: corridor {source_ref} -> {target_ref}, connection {expected_from} -> {expected_to}")
            if (segment.get("from_ref"), segment.get("to_ref")) != (source_ref, target_ref):
                self._error(errors, f"{label} from_ref/to_ref disagree with the corridor")
            start, end = _json_point(segment.get("start")), _json_point(segment.get("end"))
            if start is None or end is None:
                self._error(errors, f"{label} start/end must be finite points")
                continue
            if _distance(start, departure) > TRAVERSAL_POINT_TOLERANCE_CM or _distance(end, landing) > TRAVERSAL_POINT_TOLERANCE_CM:
                self._error(errors, f"{label} points disagree with the off-mesh connection")
            actual_class = _traversal_class(
                math.hypot(landing[0] - departure[0], landing[1] - departure[1]),
                landing[2] - departure[2],
            )
            if segment.get("link_class") != actual_class:
                self._error(errors, f"{label} class disagrees with its geometry: declared {segment.get('link_class')!r}, geometry {actual_class!r}")
            if actual_class not in TRAVERSAL_ALLOWED_CLASSES:
                self._error(errors, f"{label} class {actual_class!r} is not allowed in a route")
            portal = portal_list[position + 1]
            left, right = _json_point(portal.get("left")), _json_point(portal.get("right"))
            if (
                portal.get("from_ref") != source_ref or portal.get("to_ref") != target_ref
                or portal.get("link_ref") != link_ref or portal.get("side") != TRAVERSAL_PORTAL_SIDE
                or portal.get("source_edge") != -1 or portal.get("target_edge") != -1
                or portal.get("traversal_class") != actual_class or portal.get("direction") != direction
                or left is None or right is None
                or _distance(left, departure) > TRAVERSAL_POINT_TOLERANCE_CM
                or _distance(right, landing) > TRAVERSAL_POINT_TOLERANCE_CM
            ):
                self._error(errors, f"{label} portal disagrees with the off-mesh connection")
            found = None
            for index in range(cursor, len(waypoints) - 1):
                if (
                    all(_finite(value) for value in waypoints[index] + waypoints[index + 1])
                    and _distance(waypoints[index], departure) <= TRAVERSAL_POINT_TOLERANCE_CM
                    and _distance(waypoints[index + 1], landing) <= TRAVERSAL_POINT_TOLERANCE_CM
                ):
                    found = index
                    break
            if found is None:
                self._error(errors, f"{label} is not made between consecutive waypoints")
            else:
                cursor = found + 1

    def validate(self, plan: dict[str, Any], case: dict[str, Any] | None = None) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if not isinstance(plan, dict):
            return {"valid": False, "errors": ["route plan must be a JSON object"], "warnings": []}
        if plan.get("schema_version") != 1:
            self._error(errors, f"unsupported or missing schema_version: {plan.get('schema_version')!r}")
        if plan.get("source") != "shipping_detour":
            self._error(errors, f"unsupported or missing source: {plan.get('source')!r}")
        if not isinstance(plan.get("executable_sha256"), str) or len(plan.get("executable_sha256", "")) != 64:
            self._error(errors, "executable_sha256 must be a 64-character string")
        if not isinstance(plan.get("reason_code"), str) or not plan.get("reason_code"):
            self._error(errors, "reason_code must be a non-empty string")
        if plan.get("movement_sent") is not False:
            self._error(errors, "movement_sent must be false")
        status = str(plan.get("status", "")).lower()
        if status not in {"complete", "failed"}:
            self._error(errors, f"unsupported or missing status: {plan.get('status')!r}")
        refs = _extract_refs(plan)
        start_ref = plan.get("start_ref")
        end_ref = plan.get("end_ref")
        if status == "complete" and (not isinstance(start_ref, int) or isinstance(start_ref, bool)):
            self._error(errors, "start_ref must be an integer")
        if status == "complete" and (not isinstance(end_ref, int) or isinstance(end_ref, bool)):
            self._error(errors, "end_ref must be an integer")
        for label, reference in (("start", start_ref), ("end", end_ref)):
            if reference is not None and (not isinstance(reference, int) or isinstance(reference, bool)):
                self._error(errors, f"{label}_ref must be an integer or null")
        requested_start = self._finite_point_field(plan, "requested_start", errors, required=status == "complete")
        requested_end = self._finite_point_field(plan, "requested_end", errors, required=status == "complete")
        resolved_start = self._finite_point_field(plan, "resolved_start", errors, required=status == "complete")
        resolved_end = self._finite_point_field(plan, "resolved_end", errors, required=status == "complete")
        if isinstance(start_ref, int) and start_ref not in self.known_refs:
            self._error(errors, f"nonexistent start polygon ref: {start_ref}")
        if isinstance(end_ref, int) and end_ref not in self.known_refs:
            self._error(errors, f"nonexistent end polygon ref: {end_ref}")

        for label, reference in (("start_resolution", start_ref), ("end_resolution", end_ref)):
            resolution = plan.get(label)
            if not isinstance(resolution, dict):
                self._error(errors, f"{label} must be an object")
            elif status == "complete" and resolution.get("selected_ref") != reference:
                self._error(errors, f"{label}.selected_ref must equal {label.replace('_resolution', '_ref')}")

        if status == "failed":
            for reference in refs:
                if reference not in self.known_refs:
                    self._error(errors, f"nonexistent polygon ref: {reference}")
            failed_waypoints = _extract_waypoints(plan)
            for index, waypoint in enumerate(failed_waypoints):
                if not all(_finite(value) for value in waypoint):
                    self._error(errors, f"non-finite waypoint at index {index}")
            if plan.get("total_estimated_path_length_m") is not None and (not _finite(plan["total_estimated_path_length_m"]) or float(plan["total_estimated_path_length_m"]) < 0):
                self._error(errors, "total_estimated_path_length_m must be finite and non-negative when present")
            return {"valid": not errors, "errors": errors, "warnings": warnings, "checked_polygon_refs": refs, "checked_waypoint_count": len(failed_waypoints), "checked_status": status}

        if not refs:
            self._error(errors, "complete RoutePlan requires a non-empty polygon_corridor")
        if isinstance(start_ref, int) and refs and start_ref != refs[0]:
            self._error(errors, f"start_ref must equal first polygon_corridor ref: {start_ref} != {refs[0]}")
        if isinstance(end_ref, int) and refs and end_ref != refs[-1]:
            self._error(errors, f"end_ref must equal final polygon_corridor ref: {end_ref} != {refs[-1]}")
        for reference in refs:
            if reference not in self.known_refs:
                self._error(errors, f"nonexistent polygon ref: {reference}")
            if reference not in self.strict_refs:
                self._error(errors, f"polygon ref lacks strict serialized geometry: {reference}")

        portals = plan.get("portals")
        portal_list = _as_list(portals)
        traversal_positions = self._traversal_positions(refs, portal_list)
        for index, (left, right) in enumerate(zip(refs, refs[1:])):
            if index in traversal_positions:
                continue
            if not any(int(edge["target_ref"]) == right for edge in self.analyzer.strict_links_by_source.get(left, [])):
                self._error(errors, f"broken directed adjacency: {left} -> {right} has no serialized source link")

        if len(portal_list) != len(refs) + 1:
            self._error(errors, f"portal count must equal corridor polygon count plus endpoint sentinels: expected {len(refs) + 1}, got {len(portal_list)}")
        if refs:
            self._validate_endpoint_sentinel(portal_list[0] if portal_list else None, 0, refs[0], resolved_start, errors, "start")
            for index, (source_ref, target_ref) in enumerate(zip(refs, refs[1:]), start=1):
                if index < len(portal_list) and (index - 1) not in traversal_positions:
                    self._validate_transition_portal(portal_list[index], index, source_ref, target_ref, errors)
            self._validate_endpoint_sentinel(portal_list[-1] if portal_list else None, len(portal_list) - 1, refs[-1], resolved_end, errors, "end")

        waypoints = _extract_waypoints(plan)
        for index, waypoint in enumerate(waypoints):
            if not all(_finite(value) for value in waypoint):
                self._error(errors, f"non-finite waypoint at index {index}")
        if not waypoints:
            self._error(errors, "complete RoutePlan requires a non-empty waypoint sequence")
        if traversal_positions or plan.get("traversal_segments"):
            self._validate_traversals(plan, refs, portal_list, traversal_positions, waypoints, errors)
        if waypoints and resolved_start is not None and _distance(waypoints[0], resolved_start) > self.portal_tolerance:
            self._error(errors, "first waypoint does not match resolved_start")
        if waypoints and resolved_end is not None and _distance(waypoints[-1], resolved_end) > self.portal_tolerance:
            self._error(errors, "final waypoint does not match resolved_end")
        if requested_start is not None and resolved_start is not None and _distance(requested_start, resolved_start) > self.endpoint_tolerance:
            self._error(errors, "resolved_start exceeds requested endpoint tolerance")
        if requested_end is not None and resolved_end is not None and _distance(requested_end, resolved_end) > self.endpoint_tolerance:
            self._error(errors, "resolved_end exceeds requested endpoint tolerance")
        if case:
            expected_start = case.get("known_start_polygon_ref")
            expected_end = case.get("known_end_polygon_ref")
            if isinstance(expected_start, int) and start_ref != expected_start:
                self._error(errors, f"start polygon ref disagrees with case metadata: expected {expected_start}, got {start_ref}")
            if isinstance(expected_end, int) and end_ref != expected_end:
                self._error(errors, f"end polygon ref disagrees with case metadata: expected {expected_end}, got {end_ref}")
            if str(case.get("expected_connectivity", "")).lower() == "connected" and status != "complete":
                self._error(errors, "connectivity status disagrees with case metadata: expected complete")
            for label, actual, expected in (("requested start", requested_start, _json_point(case.get("point_a"))), ("requested end", requested_end, _json_point(case.get("point_b")))):
                if expected is not None and actual is None:
                    self._error(errors, f"missing {label} endpoint")
                elif expected is not None and actual is not None and _distance(actual, expected) > self.endpoint_tolerance:
                    self._error(errors, f"{label} exceeds case tolerance")

        raw_length = plan.get("total_estimated_path_length_m")
        if raw_length is None or not _finite(raw_length) or float(raw_length) < 0:
            self._error(errors, "total_estimated_path_length_m must be finite and non-negative")
        if raw_length is not None and waypoints and all(all(_finite(value) for value in point) for point in waypoints):
            expected_length_m = _polyline_length(waypoints) / 100.0
            length_tolerance_m = max(1e-6, expected_length_m * 1e-6)
            if abs(float(raw_length) - expected_length_m) > length_tolerance_m:
                self._error(errors, f"route length mismatch in metres: reported={float(raw_length):.9f}, waypoint_polyline_m={expected_length_m:.9f}, tolerance={length_tolerance_m:.9f}")

        validation = plan.get("validation")
        if not isinstance(validation, dict):
            self._error(errors, "validation must be an object")
            validation = {}
        reported_crossings = validation.get("tile_crossings")
        expected_crossings = sum(
            _ref_parts(left, self.analyzer.tile_bits, self.analyzer.poly_bits)["tile_index"] != _ref_parts(right, self.analyzer.tile_bits, self.analyzer.poly_bits)["tile_index"]
            for left, right in zip(refs, refs[1:])
        )
        if not isinstance(reported_crossings, int) or isinstance(reported_crossings, bool):
            self._error(errors, "validation.tile_crossings must be a non-negative integer")
        elif reported_crossings != expected_crossings:
            self._error(errors, f"incorrect validation.tile_crossings: reported={reported_crossings}, corridor={expected_crossings}")
        if "waypoint_count" in validation and validation.get("waypoint_count") != len(waypoints):
            self._error(errors, f"validation.waypoint_count disagrees: reported={validation.get('waypoint_count')}, actual={len(waypoints)}")

        for index in range(len(refs) - 3):
            if refs[index] == refs[index + 2] and refs[index + 1] == refs[index + 3]:
                self._error(errors, f"pathological oscillation detected at corridor indices {index}-{index + 3}")
                break
        return {"valid": not errors, "errors": errors, "warnings": warnings, "checked_polygon_refs": refs, "checked_waypoint_count": len(waypoints), "checked_status": status, "strict_graph_node_count": len(self.strict_refs)}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _render_report(analyzer: TopologyAnalyzer, summary: dict[str, Any], corpus: dict[str, Any]) -> str:
    coverage = summary["coverage"]
    graph = summary["graph"]
    strict_graph = summary["strict_planner_graph"]
    special = summary["links_and_special_cases"]
    lines = [
        "# Offline navigation validation report",
        "",
        f"Source snapshot: `{summary['source']['path']}`",
        f"Snapshot SHA-256: `{summary['source']['sha256']}`",
        "",
        "## Scope and evidence boundary",
        "",
        "This report is offline-only. It does not call UE4SS, EndNavAPI, EQS, native path queries, movement, A*, funnel routing, or planner code.",
        "",
        f"The serialized detail contains {coverage['serialized_tile_count']} tiles and {coverage['serialized_polygon_count']} polygons. Its embedded proven topology summary covers {coverage['embedded_proof_tiles_considered']} active tiles, {coverage['embedded_proof_polygon_count']} polygons, {coverage['embedded_proof_same_tile_link_count']} same-tile links, and {coverage['embedded_proof_cross_tile_link_count']} cross-tile links. The detail-export completeness flag is `{coverage['detail_export_is_complete']}`; no missing per-polygon records were synthesized.",
        "",
        "## Captured graph characteristics",
        "",
        f"- Normalized graph nodes: {graph['node_count_including_reference_only_targets']} including reference-only link targets.",
        f"- Undirected adjacency edges: {graph['undirected_edge_count']} ({graph['same_tile_edge_count']} same-tile, {graph['cross_tile_edge_count']} cross-tile-link evidence edges); raw serialized link records include {graph['directed_same_tile_link_count']} same-tile and {graph['directed_cross_tile_link_count']} cross-tile links.",
        f"- Connected components: {graph['connected_component_count']}; sizes: {graph['connected_component_sizes_descending'][:12]}.",
        f"- Degree distribution: `{json.dumps(graph['degree_distribution'], sort_keys=True)}`.",
        f"- Tile coordinates: x={summary['tile_and_vertical_distribution']['tile_coordinate_extents']['x']}, y={summary['tile_and_vertical_distribution']['tile_coordinate_extents']['y']}; layers={summary['tile_and_vertical_distribution']['tile_coordinate_extents']['layer']}.",
        f"- Unreal-space vertex extent: {summary['tile_and_vertical_distribution']['vertex_extents_unreal']}.",
        f"- Stacked tile-coordinate groups: {summary['tile_and_vertical_distribution']['stacked_tile_coordinate_group_count']}.",
        f"- Off-mesh records: {special['off_mesh_connection_count']}; owner refs determinable in {special['off_mesh_owner_ref_known_count']}.",
        f"- Unusual serialized-link counters: {special.get('unusual_link_counters', {})} (self-loop candidates are retained as anomalies and are not promoted to adjacency).",
        f"- Bounded diameter approximation: {graph['diameter_approximation']}.",
        f"- Strict planner-validation graph: {strict_graph['node_count']} geometry-backed nodes and {strict_graph['directed_edge_count']} directed link-backed edges; {strict_graph['cross_tile_edge_count']} cross-tile transitions; components={strict_graph['connected_component_count']}, largest sizes={strict_graph['connected_component_sizes_descending'][:12]}.",
        f"- Strict graph identity sources: `{json.dumps(strict_graph['identity_sources'], sort_keys=True)}`. Reference-only targets and neighbour-only evidence are excluded from this oracle graph.",
        "",
        "Low-degree and high-degree examples are retained in `topology-analysis.json`; these are observations of the captured graph, not claims about unexported tiles.",
        "",
        "## Deterministic corpus",
        "",
        f"Generated {corpus['case_count']} cases from a stable sort/order and the snapshot SHA-256. No production corridor is stored; graph search is used only for connectivity, lower-bound hop classification, and representative selection. Cases marked `strict_planner_validation` use only directed raw-link evidence with reconstructed geometry; broader topology-evidence candidates remain diagnostic.",
        "",
        "| Case | Category | Expected status | Start ref | End ref | Hop lower bound | Tile-crossing lower bound |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for case in corpus["cases"]:
        lines.append(f"| {case['case_id']} | {case['category']} | {case.get('expected_connectivity')} | {case.get('known_start_polygon_ref', '')} | {case.get('known_end_polygon_ref', '')} | {case.get('graph_hop_lower_bound', '')} | {case.get('minimum_expected_tile_crossings', '')} |")
    lines.extend([
        "",
        "## Validator interface",
        "",
        "The independent validator reads a future RoutePlan JSON object, checks refs and adjacency against this snapshot, validates portals/transitions, endpoints, finite waypoints, route length, loops, and tile-crossing counts, then emits explicit reasons. See the README for the exact command.",
        "",
        "## Likely planner trouble spots",
        "",
        "- The detailed export has many stacked tile-coordinate groups and multiple encoded layer values, so point projection must not select a polygon by horizontal proximity alone.",
        "- The embedded proof is broader than the serialized detail; a planner must not assume that every proof-only ref has local polygon geometry available to the offline oracle.",
        "- Cross-tile and same-tile links use different serialized side evidence, and many target refs are reference-only in this detail export; corridor construction must preserve exact refs and adjacency evidence.",
    ])
    return "\n".join(lines) + "\n"


def _load_route_plans(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict) and isinstance(value.get("plans"), list):
        return [item for item in value["plans"] if isinstance(item, dict)]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    raise ValueError("route-plan JSON must be an object, list, or {plans:[...]}")


def cmd_analyze(args: argparse.Namespace) -> int:
    analyzer = TopologyAnalyzer(Path(args.snapshot))
    out_dir = Path(args.out_dir)
    analysis = analyzer.analysis_document()
    summary = analysis["summary"]
    corpus = analyzer.build_corpus()
    _write_json(out_dir / "topology-analysis.json", analysis)
    _write_json(out_dir / "validation-corpus.json", corpus)
    (out_dir / "validation-report.md").write_text(_render_report(analyzer, summary, corpus), encoding="utf-8")
    print(json.dumps({"out_dir": str(out_dir.resolve()), "summary": summary, "case_count": corpus["case_count"]}, indent=2, sort_keys=True))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    analyzer = TopologyAnalyzer(Path(args.snapshot))
    corpus = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
    cases = {case["case_id"]: case for case in corpus.get("cases", []) if isinstance(case, dict) and "case_id" in case}
    validator = RoutePlanValidator(analyzer, endpoint_tolerance=float(args.endpoint_tolerance))
    results = []
    for plan in _load_route_plans(Path(args.route_plan)):
        case_id = plan.get("case_id")
        result = validator.validate(plan, cases.get(case_id))
        results.append({"case_id": case_id, "result": result})
    document = {"valid": all(item["result"]["valid"] for item in results), "plan_count": len(results), "results": results}
    if args.output:
        _write_json(Path(args.output), document)
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0 if document["valid"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze", help="analyze the snapshot and generate corpus/report")
    analyze.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    analyze.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent / "outputs")
    analyze.set_defaults(func=cmd_analyze)
    validate = subparsers.add_parser("validate", help="validate future RoutePlan JSON without generating a route")
    validate.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    validate.add_argument("--corpus", type=Path, default=Path(__file__).resolve().parent / "outputs" / "validation-corpus.json")
    validate.add_argument("--route-plan", type=Path, required=True)
    validate.add_argument("--output", type=Path)
    validate.add_argument("--endpoint-tolerance", type=float, default=250.0)
    validate.set_defaults(func=cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
