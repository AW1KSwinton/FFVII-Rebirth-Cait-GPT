"""Offline RoutePlan generation from the proven shipping Detour snapshot.

This module is deliberately independent of MCP, UE4SS, process state, and
input.  It consumes the preserved read-only geometry evidence and produces a
machine-readable route plan only.  The diagnostic export is a subset of the
live navmesh, so ambiguous tile identities, missing targets, and unprovable
portals are rejected rather than guessed.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace
from functools import cached_property
import heapq
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import Vec3
from .offmesh_links import (
    DEFAULT_TRAVERSAL_POLICY,
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    EVERYTHING_POLICY,
    LANDING_MAX_HORIZONTAL_CM,
    LANDING_MAX_VERTICAL_CM,
    MIN_LINK_LENGTH_CM,
    PORTAL_KIND_ORDINARY,
    PORTAL_KIND_TRAVERSAL,
    TRAVERSAL_PORTAL_SIDE,
    OffMeshLink,
    TraversalPolicy,
    make_link,
    plan_traversal_diagnostics,
    segment_for_link,
    traversal_summary,
    with_waypoint_indices,
)


SHIPPING_DETOUR_SCHEMA_VERSION = 1
SHIPPING_EXECUTABLE_SHA256 = (
    "37D952FABBFA2B710F8AE6490997C9755E4E57B4FD545078C9BAAF46C9B233D7"
)
SHIPPING_TILE_BITS = 14
SHIPPING_POLY_BITS = 31
DT_POLYTYPE_GROUND = 0
INVALID_LINK = 0xFFFFFFFF
# Rebirth's shipping encoding uses 128 for an internal ordinary connection.
# Stock Detour's DT_CONNECTION_INTERNAL constant is deliberately not used.
SHIPPING_CONNECTION_INTERNAL_SIDE = 128
SHIPPING_EXTERNAL_SIDES = frozenset({0, 2, 4, 6})
SHIPPING_DYNAMIC_LINK_EDGE = 0xFF
SHIPPING_DYNAMIC_LINK_FLAG = 0x40
SHIPPING_DYNAMIC_EDGE_ZERO_SIDES = frozenset({0xD0, 0xF0})
# FUN_140a7a2d8 emits edge=1 with the shipping flag and the observed side
# families below.  Keep this explicit until the remaining low-bit producer
# semantics are independently proven.
SHIPPING_DYNAMIC_EDGE_ONE_SIDES = frozenset(
    set(range(0x40, 0x48))
    | {0x60, 0x61, 0x62, 0x66, 0x67, 0xC0, 0xD0, 0xE0}
)
SHIPPING_SALT_BITS = 64 - SHIPPING_TILE_BITS - SHIPPING_POLY_BITS
MAX_SHIPPING_TILE_INDEX = (1 << SHIPPING_TILE_BITS) - 1
MAX_SHIPPING_POLY_INDEX = (1 << SHIPPING_POLY_BITS) - 1
MAX_SHIPPING_SALT = (1 << SHIPPING_SALT_BITS) - 1
# The graph is immutable after construction.  A measured 2.5 m XY bucket
# materially reduces the conservative candidate superset while keeping the
# largest observed polygons below the overflow threshold. Candidate acceptance
# and ranking still use the exact polygon geometry below; the index is only a
# conservative bounding-box prefilter.
POINT_RESOLUTION_INDEX_CELL_SIZE_CM = 250.0
POINT_RESOLUTION_INDEX_MAX_BUCKETS_PER_POLYGON = 64
# Barycentric tolerance of the footprint containment test. A point this far
# outside a triangle, in barycentric units, still counts as contained, so it
# may lie up to this fraction of the polygon's size outside its bounding box.
TRIANGLE_CONTAINMENT_EPSILON = 1e-3


class DetourRouteDataError(ValueError):
    """Raised when a topology snapshot cannot be safely interpreted."""


def _finite(value: float) -> bool:
    return math.isfinite(float(value))


def _point_dict(point: Vec3) -> dict[str, float]:
    return {"x": point.x, "y": point.y, "z": point.z}


def _distance_cm(first: Vec3, second: Vec3) -> float:
    return math.sqrt(
        (second.x - first.x) ** 2
        + (second.y - first.y) ** 2
        + (second.z - first.z) ** 2
    )


def _distance_m(first: Vec3, second: Vec3) -> float:
    return _distance_cm(first, second) / 100.0


def _cross_xy(first: Vec3, second: Vec3) -> float:
    return first.x * second.y - first.y * second.x


def _subtract(first: Vec3, second: Vec3) -> Vec3:
    return Vec3(first.x - second.x, first.y - second.y, first.z - second.z)


def _add(first: Vec3, second: Vec3) -> Vec3:
    return Vec3(first.x + second.x, first.y + second.y, first.z + second.z)


def _scale(value: Vec3, amount: float) -> Vec3:
    return Vec3(value.x * amount, value.y * amount, value.z * amount)


def _lerp(first: Vec3, second: Vec3, amount: float) -> Vec3:
    return _add(first, _scale(_subtract(second, first), amount))


def _same_point(first: Vec3, second: Vec3, tolerance_cm: float = 2.0) -> bool:
    return _distance_cm(first, second) <= tolerance_cm


def _decode_ref(value: int) -> tuple[int, int, int]:
    mask = (1 << SHIPPING_POLY_BITS) - 1
    return (
        value >> (SHIPPING_TILE_BITS + SHIPPING_POLY_BITS),
        (value >> SHIPPING_POLY_BITS) & ((1 << SHIPPING_TILE_BITS) - 1),
        value & mask,
    )


def _encode_ref(salt: int, tile_index: int, poly_index: int) -> int:
    return (
        (int(salt) << (SHIPPING_TILE_BITS + SHIPPING_POLY_BITS))
        | (int(tile_index) << SHIPPING_POLY_BITS)
        | int(poly_index)
    )


def _shipping_dynamic_subtype(
    edge: Any, side: Any, bmin: Any = 0, bmax: Any = 0,
) -> str | None:
    """Classify only binary-proven live shipping dynamic-link records.

    The edge=0/1 forms are structurally valid records, but their bmin/bmax
    fields are cleared by the shipping writers and their route geometry is
    not proven.  The edge=0xff rule remains the previously accepted shipping
    class for old captures.
    """
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
    """Return true only for recovered, structurally valid shipping links."""
    return _shipping_dynamic_subtype(edge, side, bmin, bmax) in {
        "edge_ff", "edge_zero", "edge_one",
    }


def _valid_identity_parts(tile_index: Any, salt: Any) -> bool:
    return (
        isinstance(tile_index, int) and not isinstance(tile_index, bool)
        and 0 <= tile_index <= MAX_SHIPPING_TILE_INDEX
        and isinstance(salt, int) and not isinstance(salt, bool)
        and 1 <= salt <= MAX_SHIPPING_SALT
    )


def _direct_tile_identity(tile: Mapping[str, Any]) -> tuple[str, tuple[int, int] | None, str | None]:
    """Validate native pool identity, preserving legacy-capture fallback."""
    marker = tile.get("tile_index_valid", "__missing__")
    if marker == "__missing__" or marker is False:
        return "missing", None, None
    if marker is not True:
        return "invalid", None, "tile_index_valid_not_boolean_true"
    tile_index = tile.get("tile_index")
    salt = tile.get("salt")
    if not _valid_identity_parts(tile_index, salt):
        return "invalid", None, "tile_index_or_salt_out_of_shipping_range"
    # Ensure the parts are a lossless, positive shipping reference prefix.
    probe = _encode_ref(int(salt), int(tile_index), 0)
    decoded_salt, decoded_tile, decoded_poly = _decode_ref(probe)
    if probe <= 0 or (decoded_salt, decoded_tile, decoded_poly) != (salt, tile_index, 0):
        return "invalid", None, "tile_identity_reference_round_trip_failed"
    return "valid", (int(tile_index), int(salt)), None


@dataclass(frozen=True, order=True)
class PolygonReference:
    """A decoded shipping polygon reference."""

    value: int
    salt: int = field(compare=False)
    tile_index: int = field(compare=False)
    poly_index: int = field(compare=False)

    @classmethod
    def from_value(cls, value: int) -> "PolygonReference":
        salt, tile_index, poly_index = _decode_ref(int(value))
        return cls(int(value), salt, tile_index, poly_index)

    @classmethod
    def make(cls, salt: int, tile_index: int, poly_index: int) -> "PolygonReference":
        return cls.from_value(_encode_ref(salt, tile_index, poly_index))

    def to_dict(self) -> dict[str, int]:
        return {
            "value": self.value,
            "salt": self.salt,
            "tile_index": self.tile_index,
            "poly_index": self.poly_index,
        }


@dataclass(frozen=True)
class DetourLink:
    reference: int
    edge: int
    next_index: int
    side: int
    bmin: int
    bmax: int

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DetourLink":
        fields = {
            "reference": int(value["ref"]),
            "edge": int(value["edge"]),
            "next_index": int(value["next"]),
            "side": int(value["side"]),
            "bmin": int(value.get("bmin", 0)),
            "bmax": int(value.get("bmax", 0)),
        }
        if fields["reference"] <= 0 or not 0 <= fields["edge"] <= 255 or not 0 <= fields["side"] <= 255:
            raise DetourRouteDataError("invalid_link_reference_or_edge")
        if not all(0 <= fields[name] <= 255 for name in ("bmin", "bmax")):
            raise DetourRouteDataError("invalid_link_portal_bounds")
        subtype = _shipping_dynamic_subtype(
            fields["edge"], fields["side"], fields["bmin"], fields["bmax"]
        )
        if subtype in {"invalid_edge_zero", "invalid_edge_one"}:
            raise DetourRouteDataError("invalid_shipping_dynamic_subtype")
        return cls(**fields)

    @property
    def is_shipping_dynamic(self) -> bool:
        return _is_shipping_dynamic_link(self.edge, self.side, self.bmin, self.bmax)

    @property
    def shipping_dynamic_subtype(self) -> str | None:
        return _shipping_dynamic_subtype(self.edge, self.side, self.bmin, self.bmax)

    @property
    def route_semantics_unresolved(self) -> bool:
        return self.is_shipping_dynamic

    def to_dict(self) -> dict[str, int]:
        return {
            "ref": self.reference,
            "edge": self.edge,
            "next": self.next_index,
            "side": self.side,
            "bmin": self.bmin,
            "bmax": self.bmax,
        }


@dataclass(frozen=True)
class DetourPolygon:
    reference: PolygonReference
    tile_x: int
    tile_y: int
    layer: int
    vertices: tuple[Vec3, ...]
    flags: int
    area: int
    poly_type: int
    links: tuple[DetourLink, ...]
    identity_source: str

    @cached_property
    def centroid(self) -> Vec3:
        return Vec3(
            sum(point.x for point in self.vertices) / len(self.vertices),
            sum(point.y for point in self.vertices) / len(self.vertices),
            sum(point.z for point in self.vertices) / len(self.vertices),
        )

    def edge(self, index: int) -> tuple[Vec3, Vec3]:
        if index < 0 or index >= len(self.vertices):
            raise DetourRouteDataError("polygon_edge_index_out_of_range")
        return self.vertices[index], self.vertices[(index + 1) % len(self.vertices)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.reference.to_dict(),
            "tile_x": self.tile_x,
            "tile_y": self.tile_y,
            "layer": self.layer,
            "vertices": [_point_dict(point) for point in self.vertices],
            "flags": self.flags,
            "area": self.area,
            "type": self.poly_type,
            "links": [link.to_dict() for link in self.links],
            "identity_source": self.identity_source,
        }


@dataclass(frozen=True)
class DetourPortal:
    from_reference: int
    to_reference: int
    link_reference: int
    source_edge: int
    target_edge: int
    left: Vec3
    right: Vec3
    side: int
    # An ordinary portal is an interval of the shared polygon edge.  A
    # traversal portal is an off-mesh crossing: ``left`` is the point it is
    # entered at and ``right`` the point it lands on, ``link_reference`` the
    # off-mesh polygon, ``source_edge`` and ``target_edge`` -1.
    kind: str = PORTAL_KIND_ORDINARY
    traversal_class: str = ""
    traversal_reverse: bool = False

    @property
    def is_traversal(self) -> bool:
        return self.kind == PORTAL_KIND_TRAVERSAL

    def to_dict(self) -> dict[str, Any]:
        result = {
            "from_ref": self.from_reference,
            "to_ref": self.to_reference,
            "link_ref": self.link_reference,
            "source_edge": self.source_edge,
            "target_edge": self.target_edge,
            "left": _point_dict(self.left),
            "right": _point_dict(self.right),
            "side": self.side,
        }
        if self.is_traversal:
            result.update({
                "kind": self.kind,
                "traversal_class": self.traversal_class,
                "direction": DIRECTION_REVERSE if self.traversal_reverse else DIRECTION_FORWARD,
            })
        return result


@dataclass(frozen=True)
class PointResolution:
    status: str
    reason_code: str
    requested_point: Vec3
    selected_reference: int | None
    closest_point: Vec3 | None
    horizontal_distance_m: float | None
    vertical_distance_m: float | None
    candidate_count: int
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "resolved" and self.selected_reference is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "requested_position": _point_dict(self.requested_point),
            "selected_ref": self.selected_reference,
            "closest_point": _point_dict(self.closest_point) if self.closest_point else None,
            "horizontal_distance_m": self.horizontal_distance_m,
            "vertical_distance_m": self.vertical_distance_m,
            "candidate_count": self.candidate_count,
            "diagnostics": self.diagnostics,
        }


@dataclass(frozen=True)
class PointResolutionCandidate:
    """One bounded polygon candidate retained for corridor reasoning."""

    reference: int
    closest_point: Vec3
    horizontal_distance_m: float
    vertical_distance_m: float
    contained: bool
    ranking: tuple[float, float, float]
    corridor_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.reference,
            "closest_point": _point_dict(self.closest_point),
            "horizontal_distance_m": self.horizontal_distance_m,
            "vertical_distance_m": self.vertical_distance_m,
            "contained": self.contained,
            "ranking": list(self.ranking),
            "corridor_index": self.corridor_index,
        }


@dataclass(frozen=True)
class CorridorPointResolution:
    """Resolution evidence against an explicitly authorized corridor.

    The unrestricted resolver remains the source of global diagnostics.  This
    result deliberately retains every candidate within the live snap/vertical
    bounds so an overlapping or boundary case cannot be hidden by a single
    global tie-break.
    """

    status: str
    reason_code: str
    selected_reference: int | None
    corridor_index: int | None
    candidates: tuple[PointResolutionCandidate, ...]
    global_resolution: PointResolution

    @property
    def ok(self) -> bool:
        return bool(self.candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "selected_ref": self.selected_reference,
            "corridor_index": self.corridor_index,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "candidate_count": len(self.candidates),
            "global_resolution": self.global_resolution.to_dict(),
            "global_selected_ref": self.global_resolution.selected_reference,
            "global_selected_is_authorized": self.global_resolution.selected_reference in {
                candidate.reference for candidate in self.candidates
            },
            "corridor_candidate_refs": [candidate.reference for candidate in self.candidates],
        }


@dataclass(frozen=True)
class DetourRoutePlan:
    """Machine-readable, movement-free route result."""

    status: str
    reason_code: str
    source: str
    schema_version: int
    executable_sha256: str
    requested_start: Vec3
    requested_end: Vec3
    resolved_start: Vec3 | None
    resolved_end: Vec3 | None
    start_ref: int | None
    end_ref: int | None
    polygon_corridor: tuple[int, ...]
    portals: tuple[DetourPortal, ...]
    waypoints: tuple[Vec3, ...]
    total_estimated_path_length_m: float | None
    start_resolution: PointResolution
    end_resolution: PointResolution
    validation: dict[str, Any]
    diagnostics: dict[str, Any]
    movement_sent: bool = False
    # Off-mesh crossings the route makes, in order; each is entered at its
    # ``start`` waypoint and lands on the next waypoint, its ``end``.
    traversal_segments: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = self._base_dict()
        if self.traversal_segments:
            result["traversal_segments"] = [dict(segment) for segment in self.traversal_segments]
        return result

    def _base_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "executable_sha256": self.executable_sha256,
            "status": self.status,
            "reason_code": self.reason_code,
            "requested_start": _point_dict(self.requested_start),
            "requested_end": _point_dict(self.requested_end),
            "resolved_start": _point_dict(self.resolved_start) if self.resolved_start else None,
            "resolved_end": _point_dict(self.resolved_end) if self.resolved_end else None,
            "start_ref": self.start_ref,
            "end_ref": self.end_ref,
            "polygon_corridor": list(self.polygon_corridor),
            "portals": [portal.to_dict() for portal in self.portals],
            "waypoints": [_point_dict(point) for point in self.waypoints],
            "total_estimated_path_length_m": self.total_estimated_path_length_m,
            "start_resolution": self.start_resolution.to_dict(),
            "end_resolution": self.end_resolution.to_dict(),
            "validation": self.validation,
            "diagnostics": self.diagnostics,
            "movement_sent": self.movement_sent,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


def _recast_to_unreal(value: Mapping[str, Any]) -> Vec3:
    try:
        x, y, z = float(value["x"]), float(value["y"]), float(value["z"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DetourRouteDataError("non_finite_or_malformed_vertex") from exc
    if not all(_finite(item) for item in (x, y, z)):
        raise DetourRouteDataError("non_finite_or_malformed_vertex")
    return Vec3(-x, -z, y)


def _load_vertex(value: Mapping[str, Any], coordinate_space: str) -> Vec3:
    if coordinate_space == "recast":
        return _recast_to_unreal(value)
    if coordinate_space == "unreal":
        try:
            point = Vec3(float(value["x"]), float(value["y"]), float(value["z"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise DetourRouteDataError("non_finite_or_malformed_vertex") from exc
        if not all(_finite(item) for item in (point.x, point.y, point.z)):
            raise DetourRouteDataError("non_finite_or_malformed_vertex")
        return point
    raise DetourRouteDataError("unsupported_coordinate_space")


def _point_in_triangle_xy(point: Vec3, first: Vec3, second: Vec3, third: Vec3, epsilon: float = TRIANGLE_CONTAINMENT_EPSILON) -> tuple[bool, tuple[float, float, float]]:
    denominator = (
        (second.y - third.y) * (first.x - third.x)
        + (third.x - second.x) * (first.y - third.y)
    )
    if abs(denominator) <= epsilon:
        return False, (0.0, 0.0, 0.0)
    a = (
        (second.y - third.y) * (point.x - third.x)
        + (third.x - second.x) * (point.y - third.y)
    ) / denominator
    b = (
        (third.y - first.y) * (point.x - third.x)
        + (first.x - third.x) * (point.y - third.y)
    ) / denominator
    c = 1.0 - a - b
    return (
        a >= -epsilon and b >= -epsilon and c >= -epsilon,
        (a, b, c),
    )


def _closest_point_on_polygon(point: Vec3, polygon: DetourPolygon) -> tuple[Vec3, bool, float, float]:
    """Return closest point, footprint containment, horizontal and vertical cm."""
    vertices = polygon.vertices
    for index in range(1, len(vertices) - 1):
        inside, barycentric = _point_in_triangle_xy(point, vertices[0], vertices[index], vertices[index + 1])
        if inside:
            a, b, c = barycentric
            closest = Vec3(
                point.x,
                point.y,
                a * vertices[0].z + b * vertices[index].z + c * vertices[index + 1].z,
            )
            return closest, True, 0.0, abs(point.z - closest.z)

    best: tuple[float, Vec3] | None = None
    for index, first in enumerate(vertices):
        second = vertices[(index + 1) % len(vertices)]
        dx, dy = second.x - first.x, second.y - first.y
        length_sq = dx * dx + dy * dy
        amount = (
            min(1.0, max(0.0, ((point.x - first.x) * dx + (point.y - first.y) * dy) / length_sq))
            if length_sq > 0.0 else 0.0
        )
        candidate = _lerp(first, second, amount)
        horizontal_sq = (point.x - candidate.x) ** 2 + (point.y - candidate.y) ** 2
        if best is None or horizontal_sq < best[0]:
            best = (horizontal_sq, candidate)
    if best is None:
        raise DetourRouteDataError("polygon_has_no_vertices")
    closest = best[1]
    return closest, False, math.sqrt(best[0]), abs(point.z - closest.z)


def _point_on_segment_xy(point: Vec3, first: Vec3, second: Vec3, tolerance_cm: float) -> bool:
    dx, dy = second.x - first.x, second.y - first.y
    length_sq = dx * dx + dy * dy
    if length_sq <= 0.0:
        return _same_point(point, first, tolerance_cm)
    amount = ((point.x - first.x) * dx + (point.y - first.y) * dy) / length_sq
    # Shipping portal intervals can extend a few centimetres beyond the
    # serialized target-edge endpoint after clipping. Compare with the
    # closest point on the finite segment instead of rejecting every
    # slightly out-of-range projection. This matches the independent
    # validator's segment-distance rule while retaining the same strict
    # tolerance for target-edge association.
    closest = _lerp(first, second, min(1.0, max(0.0, amount)))
    return math.hypot(point.x - closest.x, point.y - closest.y) <= tolerance_cm


def _source_portal_endpoints(
    source: DetourPolygon,
    link: DetourLink,
) -> tuple[Vec3, Vec3] | None:
    """Apply the proven shipping link semantics to one source polygon edge."""
    if source.poly_type != DT_POLYTYPE_GROUND:
        return None
    # Shipping dynamic links use edge=0xff and side flag 0x40.  They are
    # valid records, but their route geometry is not the ordinary Detour
    # portal interval represented by bmin/bmax.
    if link.is_shipping_dynamic:
        return None
    if link.edge < 0 or link.edge >= len(source.vertices):
        return None
    if not (0 <= link.bmin <= 255 and 0 <= link.bmax <= 255):
        return None
    if link.side == SHIPPING_CONNECTION_INTERNAL_SIDE:
        first, second = source.edge(link.edge)
        return first, second
    if link.side not in SHIPPING_EXTERNAL_SIDES or link.bmax <= link.bmin:
        return None
    first, second = source.edge(link.edge)
    return (
        _lerp(first, second, link.bmin / 255.0),
        _lerp(first, second, link.bmax / 255.0),
    )


def _matching_target_edge(
    target: DetourPolygon,
    first: Vec3,
    second: Vec3,
    tolerance_cm: float,
) -> int:
    """Return a geometry-consistency edge, without defining the portal."""
    for edge_index in range(len(target.vertices)):
        target_first, target_second = target.edge(edge_index)
        if (
            _point_on_segment_xy(first, target_first, target_second, tolerance_cm)
            and _point_on_segment_xy(second, target_first, target_second, tolerance_cm)
        ):
            return edge_index
    return -1


def _portal_from_link(
    source: DetourPolygon,
    target: DetourPolygon,
    link: DetourLink,
    *,
    tolerance_cm: float,
) -> DetourPortal | None:
    """Build a portal from shipping link data, not a target shared-edge guess."""
    if link.route_semantics_unresolved:
        return None
    if target.poly_type != DT_POLYTYPE_GROUND:
        return None
    endpoints = _source_portal_endpoints(source, link)
    if endpoints is None:
        return None
    first, second = endpoints
    direction = _subtract(target.centroid, source.centroid)
    edge_direction = _subtract(second, first)
    if _cross_xy(direction, edge_direction) < 0.0:
        left, right = first, second
    else:
        left, right = second, first
    return DetourPortal(
        from_reference=source.reference.value,
        to_reference=target.reference.value,
        link_reference=link.reference,
        source_edge=link.edge,
        target_edge=_matching_target_edge(target, first, second, tolerance_cm),
        left=left,
        right=right,
        side=link.side,
    )


def _portal_geometry_consistent(
    source: DetourPolygon,
    target: DetourPolygon,
    link: DetourLink,
    *,
    tolerance_cm: float,
) -> bool:
    """Use target geometry only to validate unresolved tile identity inference."""
    endpoints = _source_portal_endpoints(source, link)
    if endpoints is None or target.poly_type != DT_POLYTYPE_GROUND:
        return False
    return _matching_target_edge(target, *endpoints, tolerance_cm) >= 0


def _extract_off_mesh_links(
    records: Mapping[int, tuple[Mapping[str, Any], tuple[int, int], str]],
    polygons: Mapping[int, DetourPolygon],
    coordinate_space: str,
) -> tuple[list[OffMeshLink], dict[str, Any]]:
    """The off-mesh connections that join two ground polygons of the graph.

    A connection is the tile's ``off_mesh_connections`` entry and a two-vertex
    off-mesh polygon of the same index.  The polygon's ``edge 0`` dynamic link
    names the polygon the connection starts on and its ``edge 1`` link the one
    it ends on; those are the landing polygons, whatever lies under the
    points.  A connection with either link missing or ambiguous (its end lies
    off the mesh), landing on something that is not a ground polygon of the
    graph, or whose points are not on its landing polygons is counted and
    left out, never guessed.
    """
    stats: Counter[str] = Counter()
    links: list[OffMeshLink] = []
    max_snap_cm = 0.0
    for tile, (tile_index, salt), _ in records.values():
        raw_polygons = {
            int(polygon["index"]): polygon
            for polygon in tile.get("polygons", [])
            if isinstance(polygon, Mapping) and isinstance(polygon.get("index"), int)
        }
        vertices = tile.get("vertices", [])
        for connection in tile.get("off_mesh_connections") or []:
            stats["serialized"] += 1
            try:
                poly_index = int(connection["poly"])
                flags, side = int(connection["flags"]), int(connection["side"])
                connection_points = [
                    _load_vertex(dict(zip("xyz", connection[name])), coordinate_space)
                    for name in ("start", "end")
                ]
            except (KeyError, TypeError, ValueError, DetourRouteDataError):
                stats["malformed"] += 1
                continue
            raw = raw_polygons.get(poly_index)
            if raw is None:
                stats["owner_polygon_missing"] += 1
                continue
            landing: dict[int, list[int]] = {0: [], 1: []}
            for link in raw.get("links", []) or []:
                try:
                    edge, link_side = int(link["edge"]), int(link["side"])
                    subtype = _shipping_dynamic_subtype(
                        edge, link_side, int(link.get("bmin", 0)), int(link.get("bmax", 0)),
                    )
                    if (edge, subtype) in {(0, "edge_zero"), (1, "edge_one")}:
                        landing[edge].append(int(link["ref"]))
                except (KeyError, TypeError, ValueError):
                    continue
            if not landing[0] or not landing[1]:
                stats["landing_unresolved"] += 1
                continue
            if len(landing[0]) != 1 or len(landing[1]) != 1:
                stats["landing_ambiguous"] += 1
                continue
            start_ref, end_ref = landing[0][0], landing[1][0]
            try:
                owner = PolygonReference.make(salt, tile_index, poly_index).value
            except (TypeError, ValueError):
                stats["malformed"] += 1
                continue
            # The tile build snaps the connection's points onto the landing
            # polygons and stores them as the off-mesh polygon's vertices.
            points = connection_points
            indices = raw.get("vertex_indices", [])
            if isinstance(indices, list) and len(indices) == 2:
                try:
                    points = [_load_vertex(vertices[int(index)], coordinate_space) for index in indices]
                except (IndexError, KeyError, TypeError, ValueError, DetourRouteDataError):
                    points = connection_points
            snap_cm = max(_distance_cm(snapped, raw_point) for snapped, raw_point in zip(points, connection_points))
            if snap_cm > 2.0 * LANDING_MAX_HORIZONTAL_CM:
                stats["endpoint_mismatch"] += 1
                continue
            start_polygon, end_polygon = polygons.get(start_ref), polygons.get(end_ref)
            if start_polygon is None or end_polygon is None:
                stats["landing_polygon_not_in_graph"] += 1
                continue
            if start_polygon.poly_type != DT_POLYTYPE_GROUND or end_polygon.poly_type != DT_POLYTYPE_GROUND:
                stats["landing_not_ground"] += 1
                continue
            if start_ref == end_ref:
                stats["same_polygon"] += 1
                continue
            landings = [
                _closest_point_on_polygon(point, polygon)
                for point, polygon in zip(points, (start_polygon, end_polygon))
            ]
            if any(
                horizontal_cm > LANDING_MAX_HORIZONTAL_CM or vertical_cm > LANDING_MAX_VERTICAL_CM
                for _, _, horizontal_cm, vertical_cm in landings
            ):
                stats["landing_geometry_mismatch"] += 1
                continue
            if _distance_cm(points[0], points[1]) < MIN_LINK_LENGTH_CM:
                stats["degenerate"] += 1
                continue
            max_snap_cm = max(max_snap_cm, snap_cm)
            links.append(make_link(owner, start_ref, end_ref, points[0], points[1], flags, side))
            stats["linked"] += 1
    summary: dict[str, Any] = dict(sorted(stats.items()))
    summary["max_endpoint_snap_cm"] = max_snap_cm
    return links, summary


class DetourTopologyGraph:
    """Validated graph and portals reconstructed from a diagnostic snapshot."""

    def __init__(
        self,
        polygons: Mapping[int, DetourPolygon],
        adjacency: Mapping[int, tuple[DetourPortal, ...]],
        *,
        snapshot_diagnostics: Mapping[str, Any],
        portal_tolerance_cm: float = 5.0,
        off_mesh_links: Iterable[OffMeshLink] = (),
        traversal_policy: TraversalPolicy = DEFAULT_TRAVERSAL_POLICY,
    ) -> None:
        self.polygons = dict(polygons)
        self.adjacency = {key: tuple(value) for key, value in adjacency.items()}
        self.snapshot_diagnostics = dict(snapshot_diagnostics)
        self.portal_tolerance_cm = float(portal_tolerance_cm)
        # Off-mesh links join polygons the ordinary links leave apart.  They
        # are kept out of ``adjacency`` (whose consumers assume a shared
        # polygon edge) and offered to the planner through
        # ``outgoing_portals``, filtered by the traversal policy.
        self.off_mesh_links = tuple(off_mesh_links)
        self.traversal_policy = traversal_policy
        self._links_by_owner: dict[int, OffMeshLink] = {}
        self._traversal_portals: dict[int, tuple[DetourPortal, ...]] = {}
        self._ground_islands: dict[int, int] | None = None
        self._ground_islands_polygon_count = -1
        self._point_resolution_index: dict[tuple[int, int], tuple[int, ...]] = {}
        self._point_resolution_index_overflow: tuple[int, ...] = tuple()
        self._point_resolution_index_polygon_count = -1
        self._point_resolution_containment_margin_cm = 0.0
        self._build_point_resolution_index()
        self._build_traversal_portals()
        if self._traversal_portals:
            self.snapshot_diagnostics["traversal_links"] = traversal_summary(self)

    def _build_traversal_portals(self) -> None:
        buckets: defaultdict[int, list[DetourPortal]] = defaultdict(list)
        for link in self.off_mesh_links:
            self._links_by_owner[link.owner_reference] = link
            for reverse in (False, True):
                if reverse and not link.two_way_claimed:
                    continue
                from_ref, to_ref, departure, landing = link.crossing(reverse)
                source, target = self.polygons.get(from_ref), self.polygons.get(to_ref)
                if (
                    source is None or target is None or from_ref == to_ref
                    or source.poly_type != DT_POLYTYPE_GROUND
                    or target.poly_type != DT_POLYTYPE_GROUND
                ):
                    continue
                buckets[from_ref].append(DetourPortal(
                    from_reference=from_ref,
                    to_reference=to_ref,
                    link_reference=link.owner_reference,
                    source_edge=-1,
                    target_edge=-1,
                    left=departure,
                    right=landing,
                    side=TRAVERSAL_PORTAL_SIDE,
                    kind=PORTAL_KIND_TRAVERSAL,
                    traversal_class=link.link_class,
                    traversal_reverse=reverse,
                ))
        self._traversal_portals = {
            source: tuple(sorted(
                portals,
                key=lambda item: (item.to_reference, item.link_reference, item.traversal_reverse),
            ))
            for source, portals in buckets.items()
        }

    def set_gimmick_links(self, links: Iterable[OffMeshLink]) -> int:
        """Replace the graph's traversal-gimmick links (gimmick_links.py) with
        ``links``; the capture's own connections are untouched.  The graph is
        shared between routes, so each route sets the evidence it planned on.
        """
        gimmicks = tuple(link for link in links if link.gimmick_id is not None)
        captured = tuple(link for link in self.off_mesh_links if link.gimmick_id is None)
        if self.off_mesh_links == captured + gimmicks:
            return len(gimmicks)
        self.off_mesh_links = captured + gimmicks
        self._links_by_owner = {}
        self._build_traversal_portals()
        self.snapshot_diagnostics["traversal_links"] = traversal_summary(self)
        return len(gimmicks)

    def off_mesh_link(self, owner_reference: int) -> OffMeshLink | None:
        """The off-mesh link whose off-mesh polygon is ``owner_reference``."""
        return self._links_by_owner.get(owner_reference)

    def traversal_portals_from(self, reference: int) -> tuple[DetourPortal, ...]:
        """Every off-mesh crossing out of a polygon, whatever the policy."""
        return self._traversal_portals.get(reference, ())

    def outgoing_portals(
        self, reference: int, policy: TraversalPolicy | None = None,
    ) -> tuple[DetourPortal, ...]:
        """The ordinary portals out of a polygon and the off-mesh crossings
        ``policy`` (default: the graph's) allows."""
        ordinary = self.adjacency.get(reference, ())
        extra = self._traversal_portals.get(reference)
        if not extra:
            return ordinary
        policy = self.traversal_policy if policy is None else policy
        if not policy.active:
            return ordinary
        permitted = tuple(
            portal for portal in extra
            if policy.permits(portal.traversal_class, portal.traversal_reverse, portal.link_reference)
        )
        return ordinary + permitted if permitted else ordinary

    def traversal_link_for_portal(self, portal: DetourPortal) -> OffMeshLink | None:
        """The off-mesh link a traversal portal crosses, if it really does."""
        if not portal.is_traversal:
            return None
        link = self._links_by_owner.get(portal.link_reference)
        if link is None:
            return None
        from_ref, to_ref, departure, landing = link.crossing(portal.traversal_reverse)
        if (
            from_ref != portal.from_reference or to_ref != portal.to_reference
            or departure != portal.left or landing != portal.right
            or link.link_class != portal.traversal_class
        ):
            return None
        return link

    def ground_islands(self) -> dict[int, int]:
        """Polygon reference to island number (0 is the largest) over the
        ordinary links: the pieces of floor no walk along polygons joins."""
        if (
            self._ground_islands is not None
            and self._ground_islands_polygon_count == len(self.polygons)
        ):
            return self._ground_islands
        parent = {reference: reference for reference in self.polygons}

        def find(item: int) -> int:
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        for source, portals in self.adjacency.items():
            if source not in parent:
                continue
            for portal in portals:
                if portal.to_reference in parent:
                    parent[find(source)] = find(portal.to_reference)
        groups: defaultdict[int, list[int]] = defaultdict(list)
        for reference in self.polygons:
            groups[find(reference)].append(reference)
        ordered = sorted(groups.values(), key=lambda members: (-len(members), min(members)))
        labels = {
            reference: number
            for number, members in enumerate(ordered)
            for reference in members
        }
        self._ground_islands = labels
        self._ground_islands_polygon_count = len(self.polygons)
        return labels

    def _transition_cost_m(
        self,
        source: DetourPolygon,
        target: DetourPolygon,
        portal: DetourPortal,
        policy: TraversalPolicy,
    ) -> float:
        if not portal.is_traversal:
            return _distance_m(source.centroid, target.centroid)
        return (
            _distance_m(source.centroid, portal.left)
            + _distance_m(portal.left, portal.right)
            + _distance_m(portal.right, target.centroid)
            + policy.penalty_m
        )

    def _build_point_resolution_index(self) -> None:
        buckets: defaultdict[tuple[int, int], list[int]] = defaultdict(list)
        overflow: list[int] = []
        largest_diagonal_cm = 0.0
        cell_size = POINT_RESOLUTION_INDEX_CELL_SIZE_CM
        for reference, polygon in self.polygons.items():
            if not polygon.vertices:
                overflow.append(reference)
                continue
            min_x = min(vertex.x for vertex in polygon.vertices)
            max_x = max(vertex.x for vertex in polygon.vertices)
            min_y = min(vertex.y for vertex in polygon.vertices)
            max_y = max(vertex.y for vertex in polygon.vertices)
            min_cell_x = math.floor(min_x / cell_size)
            max_cell_x = math.floor(max_x / cell_size)
            min_cell_y = math.floor(min_y / cell_size)
            max_cell_y = math.floor(max_y / cell_size)
            bucket_count = (max_cell_x - min_cell_x + 1) * (max_cell_y - min_cell_y + 1)
            if bucket_count > POINT_RESOLUTION_INDEX_MAX_BUCKETS_PER_POLYGON:
                overflow.append(reference)
                continue
            largest_diagonal_cm = max(largest_diagonal_cm, math.hypot(max_x - min_x, max_y - min_y))
            for cell_x in range(min_cell_x, max_cell_x + 1):
                for cell_y in range(min_cell_y, max_cell_y + 1):
                    buckets[(cell_x, cell_y)].append(reference)
        self._point_resolution_index = {
            key: tuple(sorted(values))
            for key, values in buckets.items()
        }
        self._point_resolution_index_overflow = tuple(sorted(overflow))
        self._point_resolution_index_polygon_count = len(self.polygons)
        # How far outside its bounding box an indexed polygon can still
        # contain a point: the containment tolerance times the polygon's
        # size (a triangle's height is at most its polygon's diagonal), plus
        # a centimetre for rounding. Overflow polygons are always candidates.
        self._point_resolution_containment_margin_cm = (
            TRIANGLE_CONTAINMENT_EPSILON * largest_diagonal_cm + 1.0
        )

    def _point_resolution_references(
        self,
        point: Vec3,
        max_snap_distance_m: float,
    ) -> tuple[int, ...]:
        """Return a conservative bbox superset for point resolution.

        A polygon whose closest point is within the requested horizontal snap
        bound must have an XY bounding box intersecting the expanded query
        box.  Overflow polygons are always included so large/odd geometry
        cannot be lost through indexing.
        """
        # The production graph is immutable, but the public graph model is
        # also used by deterministic fixtures that append synthetic polygons
        # after construction.  Rebuild only when that structural mutation is
        # observable; the normal live path pays a single integer comparison.
        if self._point_resolution_index_polygon_count != len(self.polygons):
            self._build_point_resolution_index()
        expansion_cm = max(0.0, float(max_snap_distance_m)) * 100.0
        cell_size = POINT_RESOLUTION_INDEX_CELL_SIZE_CM
        min_cell_x = math.floor((point.x - expansion_cm) / cell_size)
        max_cell_x = math.floor((point.x + expansion_cm) / cell_size)
        min_cell_y = math.floor((point.y - expansion_cm) / cell_size)
        max_cell_y = math.floor((point.y + expansion_cm) / cell_size)
        references: set[int] = set(self._point_resolution_index_overflow)
        for cell_x in range(min_cell_x, max_cell_x + 1):
            for cell_y in range(min_cell_y, max_cell_y + 1):
                references.update(self._point_resolution_index.get((cell_x, cell_y), ()))
        return tuple(sorted(references))

    def valid_neighbor_references(self, reference: int) -> tuple[int, ...]:
        """Return only link-backed neighbours with a proven transition portal."""
        return tuple(portal.to_reference for portal in self.adjacency.get(reference, ()))

    @classmethod
    def from_json(cls, path: str | Path) -> "DetourTopologyGraph":
        with Path(path).open("r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DetourTopologyGraph":
        if payload.get("source") != "ue4ss_native_detour_geometry_dump":
            raise DetourRouteDataError("unsupported_topology_source")
        if payload.get("movement_sent") is not False:
            raise DetourRouteDataError("movement_evidence_not_read_only")
        if payload.get("coordinate_space") not in {"recast", "unreal"}:
            raise DetourRouteDataError("unsupported_coordinate_space")
        tiles = payload.get("tiles")
        if not isinstance(tiles, list) or not tiles:
            raise DetourRouteDataError("topology_snapshot_has_no_tiles")
        records: dict[int, tuple[Mapping[str, Any], tuple[int, int], str]] = {}
        unresolved: set[int] = set(range(len(tiles)))
        identity_blocked: set[int] = set()
        identity_disagreements = 0
        identity_invalid_direct = 0
        identity_native_pool = 0
        identity_inferred_internal_link = 0
        for record_index, tile in enumerate(tiles):
            identity_candidates: set[tuple[int, int]] = set()
            polygons = tile.get("polygons") if isinstance(tile, Mapping) else None
            if not isinstance(polygons, list):
                raise DetourRouteDataError("tile_polygons_missing")
            for polygon in polygons:
                for link in polygon.get("links", []) if isinstance(polygon, Mapping) else []:
                    if int(link.get("side", -1)) != 128:
                        continue
                    reference = int(link.get("ref", 0))
                    if reference <= 0:
                        continue
                    _, tile_index, poly_index = _decode_ref(reference)
                    if 0 <= poly_index < len(polygons) and 0 <= tile_index <= MAX_SHIPPING_TILE_INDEX:
                        identity_candidates.add((tile_index, reference >> (SHIPPING_TILE_BITS + SHIPPING_POLY_BITS)))
            direct_status, direct_identity, _ = _direct_tile_identity(tile)
            if direct_status == "invalid":
                identity_invalid_direct += 1
                identity_blocked.add(record_index)
                unresolved.discard(record_index)
                continue
            if direct_status == "valid":
                if identity_candidates and identity_candidates != {direct_identity}:
                    identity_disagreements += 1
                    identity_blocked.add(record_index)
                    unresolved.discard(record_index)
                    continue
                records[record_index] = (tile, direct_identity, "native_pool")
                identity_native_pool += 1
                unresolved.discard(record_index)
            elif len(identity_candidates) == 1:
                records[record_index] = (tile, next(iter(identity_candidates)), "inferred_internal_link")
                identity_inferred_internal_link += 1
                unresolved.discard(record_index)
            elif len(identity_candidates) > 1:
                identity_disagreements += 1
                identity_blocked.add(record_index)
                unresolved.discard(record_index)

        def build_polygons() -> dict[int, DetourPolygon]:
            result: dict[int, DetourPolygon] = {}
            for record_index, (tile, identity, identity_source) in records.items():
                tile_index, salt = identity
                tile_vertices = tile.get("vertices", [])
                for polygon in tile.get("polygons", []):
                    try:
                        vertices = tuple(
                            _load_vertex(tile_vertices[int(vertex_index)], str(payload["coordinate_space"]))
                            for vertex_index in polygon["vertex_indices"]
                        )
                        if len(vertices) < 3 or len(vertices) > 6:
                            # The diagnostic export contains a bounded number
                            # of rejected/degenerate records.  They are not
                            # graph nodes and must not poison otherwise valid
                            # tiles or become guessed neighbours.
                            continue
                        poly_index = int(polygon["index"])
                        if not 0 <= poly_index <= MAX_SHIPPING_POLY_INDEX:
                            continue
                        reference = PolygonReference.make(salt, tile_index, poly_index)
                        links = tuple(DetourLink.from_dict(link) for link in polygon.get("links", []))
                        result[reference.value] = DetourPolygon(
                            reference=reference,
                            tile_x=int(tile.get("x", 0)),
                            tile_y=int(tile.get("y", 0)),
                            layer=int(tile.get("layer", 0)),
                            vertices=vertices,
                            flags=int(polygon.get("flags", 0)),
                            area=int(polygon.get("area", 0)),
                            poly_type=int(polygon.get("type", 0)),
                            links=links,
                            identity_source=identity_source,
                        )
                    except (KeyError, TypeError, ValueError, DetourRouteDataError):
                        # Fail closed for this record while retaining the
                        # valid polygons from the same serialized tile.
                        continue
            return result

        # Resolve tile identities that lack an internal side-128 link only when
        # a known polygon points to them and the requested polygon has one
        # unique geometry-consistent link portal. This inference may use target
        # geometry, but production portal endpoints below remain link-semantic.
        unresolved_by_poly: dict[int, list[int]] = defaultdict(list)
        for record_index in unresolved:
            for polygon in tiles[record_index].get("polygons", []):
                unresolved_by_poly[int(polygon.get("index", -1))].append(record_index)
        for _ in range(len(unresolved) + 1):
            polygons_by_ref = build_polygons()
            proposals: dict[int, set[tuple[int, int]]] = defaultdict(set)
            for source in polygons_by_ref.values():
                for link in source.links:
                    _, target_tile, target_poly = _decode_ref(link.reference)
                    if link.reference in polygons_by_ref or target_poly not in unresolved_by_poly:
                        continue
                    candidates: list[int] = []
                    for record_index in unresolved_by_poly[target_poly]:
                        if record_index not in unresolved:
                            continue
                        tile = tiles[record_index]
                        raw_polygon = next(
                            (item for item in tile.get("polygons", []) if int(item.get("index", -1)) == target_poly),
                            None,
                        )
                        if raw_polygon is None:
                            continue
                        try:
                            target_vertices = tuple(
                                _load_vertex(tile["vertices"][int(index)], str(payload["coordinate_space"]))
                                for index in raw_polygon["vertex_indices"]
                            )
                            target = DetourPolygon(
                                reference=PolygonReference.make(target_tile, target_tile, target_poly),
                                tile_x=int(tile.get("x", 0)), tile_y=int(tile.get("y", 0)),
                                layer=int(tile.get("layer", 0)), vertices=target_vertices,
                                flags=int(raw_polygon.get("flags", 0)), area=int(raw_polygon.get("area", 0)),
                                poly_type=int(raw_polygon.get("type", 0)), links=(),
                                identity_source="candidate",
                            )
                            if _portal_geometry_consistent(
                                source,
                                target,
                                link,
                                tolerance_cm=5.0,
                            ):
                                candidates.append(record_index)
                        except (KeyError, TypeError, ValueError, DetourRouteDataError):
                            continue
                    if len(candidates) == 1:
                        proposals[candidates[0]].add((target_tile, link.reference >> (SHIPPING_TILE_BITS + SHIPPING_POLY_BITS)))
            added = 0
            for record_index, identities in proposals.items():
                if len(identities) == 1:
                    records[record_index] = (tiles[record_index], next(iter(identities)), "inferred_internal_link")
                    identity_inferred_internal_link += 1
                    unresolved.discard(record_index)
                    added += 1
            if not added:
                break

        polygons = build_polygons()
        adjacency: dict[int, list[DetourPortal]] = defaultdict(list)
        missing_targets = 0
        portal_rejections = 0
        shipping_dynamic_subtypes: defaultdict[str, int] = defaultdict(int)
        serialized_shipping_dynamic_subtypes: defaultdict[str, int] = defaultdict(int)
        for tile in tiles:
            for polygon in tile.get("polygons", []):
                for raw_link in polygon.get("links", []):
                    subtype = _shipping_dynamic_subtype(
                        raw_link.get("edge"),
                        raw_link.get("side"),
                        raw_link.get("bmin", 0),
                        raw_link.get("bmax", 0),
                    )
                    if subtype in {"edge_ff", "edge_zero", "edge_one"}:
                        serialized_shipping_dynamic_subtypes[subtype] += 1
        for source in polygons.values():
            for link in source.links:
                if link.is_shipping_dynamic:
                    # Valid shipping dynamic links are retained in the
                    # polygon record, but are not ordinary route portals.
                    subtype = link.shipping_dynamic_subtype
                    if subtype is not None:
                        shipping_dynamic_subtypes[subtype] += 1
                    continue
                target = polygons.get(link.reference)
                if target is None:
                    missing_targets += 1
                    continue
                if target.reference.value == source.reference.value:
                    continue
                resolved = _portal_from_link(
                    source,
                    target,
                    link,
                    tolerance_cm=5.0,
                )
                if resolved is None:
                    portal_rejections += 1
                    continue
                adjacency[source.reference.value].append(resolved)
        frozen_adjacency = {
            key: tuple(sorted(value, key=lambda item: (item.to_reference, item.link_reference)))
            for key, value in adjacency.items()
        }
        off_mesh_links, off_mesh_summary = _extract_off_mesh_links(
            records, polygons, str(payload["coordinate_space"]),
        )
        return cls(
            polygons,
            frozen_adjacency,
            off_mesh_links=off_mesh_links,
            snapshot_diagnostics={
                **({"off_mesh": off_mesh_summary} if off_mesh_summary.get("serialized") else {}),
                "snapshot_status": payload.get("status"),
                "snapshot_reason_code": payload.get("reason_code"),
                "serialized_tile_records": len(tiles),
                "identity_resolved_tile_records": len(records),
                "identity_unresolved_tile_records": len(unresolved) + len(identity_blocked),
                "identity_native_pool_tiles": identity_native_pool,
                "identity_inferred_internal_link_tiles": identity_inferred_internal_link,
                "identity_unresolved_tiles": len(unresolved) + len(identity_blocked),
                "identity_disagreements": identity_disagreements,
                "identity_invalid_direct_tiles": identity_invalid_direct,
                "identity_sources": {
                    "native_pool": identity_native_pool,
                    "inferred_internal_link": identity_inferred_internal_link,
                    "unresolved": len(unresolved) + len(identity_blocked),
                },
                "serialized_polygon_count": sum(len(tile.get("polygons", [])) for tile in tiles),
                "resolved_polygon_count": len(polygons),
                "rejected_polygon_records": sum(
                    1 for tile in tiles for polygon in tile.get("polygons", [])
                    if len(polygon.get("vertex_indices", [])) < 3
                    or len(polygon.get("vertex_indices", [])) > 6
                ),
                "adjacency_edge_count": sum(len(value) for value in frozen_adjacency.values()),
                "missing_target_refs": missing_targets,
                "portal_rejections": portal_rejections,
                "shipping_dynamic_links_valid": sum(
                    1 for polygon in polygons.values() for link in polygon.links if link.is_shipping_dynamic
                ),
                "shipping_dynamic_links_route_semantics_unresolved": sum(
                    1 for polygon in polygons.values() for link in polygon.links if link.route_semantics_unresolved
                ),
                "shipping_dynamic_links_by_subtype": dict(sorted(shipping_dynamic_subtypes.items())),
                # Keep the route-graph count above separate from the raw
                # serialized count.  Non-ground/otherwise non-route polygon
                # records can carry valid shipping dynamic links; dropping
                # those records from the route graph must not erase evidence
                # that the native exporter accepted them structurally.
                "serialized_shipping_dynamic_links_valid": sum(
                    serialized_shipping_dynamic_subtypes.values()
                ),
                "serialized_shipping_dynamic_links_by_subtype": dict(
                    sorted(serialized_shipping_dynamic_subtypes.items())
                ),
                "shipping_dynamic_edge_zero_valid": shipping_dynamic_subtypes.get("edge_zero", 0),
                "shipping_dynamic_edge_one_valid": shipping_dynamic_subtypes.get("edge_one", 0),
                "shipping_dynamic_edge_ff_valid": shipping_dynamic_subtypes.get("edge_ff", 0),
                "shipping_dynamic_adjacency_excluded": sum(
                    1 for polygon in polygons.values() for link in polygon.links if link.is_shipping_dynamic
                ),
                "coordinate_space": payload.get("coordinate_space"),
            },
        )

    def _candidate_resolution(self, point: Vec3, polygon: DetourPolygon) -> tuple[tuple[int, float, float], Vec3, float, float, bool]:
        closest, contained, horizontal_cm, vertical_cm = _closest_point_on_polygon(point, polygon)
        rank = (0 if contained else 1, vertical_cm if contained else math.hypot(horizontal_cm, vertical_cm), horizontal_cm)
        return rank, closest, horizontal_cm, vertical_cm, contained

    def resolve_point_candidates(
        self,
        point: Vec3,
        *,
        references: Iterable[int] | None = None,
        max_snap_distance_m: float = 8.0,
        max_vertical_separation_m: float = 20.0,
        reject_off_mesh: bool = True,
    ) -> tuple[PointResolutionCandidate, ...]:
        """Return every polygon candidate inside the supplied live bounds.

        ``resolve_point`` intentionally retains its historical unrestricted
        single-winner behavior for planner/start-resolution compatibility.
        This method is the explicit evidence seam for route authorization: a
        bounded reference set is evaluated without hiding alternatives behind
        the global resolver's tie-break.
        """
        if not all(_finite(value) for value in (point.x, point.y, point.z)):
            return tuple()
        reference_set = (
            sorted({int(reference) for reference in references})
            if references is not None
            else sorted(self.polygons)
        )
        candidates: list[PointResolutionCandidate] = []
        for reference in reference_set:
            polygon = self.polygons.get(reference)
            if polygon is None:
                continue
            if reject_off_mesh and polygon.poly_type != DT_POLYTYPE_GROUND:
                continue
            rank, closest, horizontal_cm, vertical_cm, contained = self._candidate_resolution(point, polygon)
            if horizontal_cm > max_snap_distance_m * 100.0:
                continue
            if vertical_cm > max_vertical_separation_m * 100.0:
                continue
            candidates.append(PointResolutionCandidate(
                reference=reference,
                closest_point=closest,
                horizontal_distance_m=horizontal_cm / 100.0,
                vertical_distance_m=vertical_cm / 100.0,
                contained=contained,
                ranking=tuple(float(value) for value in rank),
            ))
        return tuple(sorted(candidates, key=lambda candidate: (candidate.ranking, candidate.reference)))

    def resolve_point_in_corridor(
        self,
        point: Vec3,
        corridor_references: Iterable[int],
        *,
        max_snap_distance_m: float = 0.75,
        max_vertical_separation_m: float = 1.0,
        reject_off_mesh: bool = True,
        global_resolution: PointResolution | None = None,
    ) -> CorridorPointResolution:
        """Resolve a point against only the immutable authorized corridor.

        A point may be globally resolved to a non-corridor polygon while also
        being a bounded corridor snap candidate.  That is reported as
        ambiguity; the method never silently substitutes the corridor winner
        for the global result.  A transition proof must use contained,
        unambiguous candidates plus portal geometry, not this method alone.
        """
        corridor = tuple(int(reference) for reference in corridor_references)
        corridor_index_by_ref = {reference: index for index, reference in enumerate(corridor)}
        # The existing spatial index is a conservative XY bounding-box superset.
        # Intersect it with the authorized corridor before exact polygon geometry
        # checks so long corridors do not rescan every immutable polygon at each
        # live sample. Overflow polygons remain in the index result, and exact
        # snap/vertical/containment tests below are unchanged.
        indexed_references = self._point_resolution_references(point, max_snap_distance_m)
        local_corridor_references = tuple(
            reference for reference in indexed_references
            if reference in corridor_index_by_ref
        )
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
            for candidate in self.resolve_point_candidates(
                point,
                references=local_corridor_references,
                max_snap_distance_m=max_snap_distance_m,
                max_vertical_separation_m=max_vertical_separation_m,
                reject_off_mesh=reject_off_mesh,
            )
        )
        if global_resolution is None:
            global_resolution = self.resolve_point(
                point,
                max_snap_distance_m=max_snap_distance_m,
                max_vertical_separation_m=max_vertical_separation_m,
                reject_off_mesh=reject_off_mesh,
            )
        contained = tuple(candidate for candidate in candidates if candidate.contained)
        if not candidates:
            status = "unresolved"
            reason_code = "no_authorized_corridor_candidate"
            selected_reference = None
            corridor_index = None
        elif len(contained) > 1:
            status = "ambiguous"
            reason_code = "authorized_shared_boundary_or_overlap"
            selected_reference = None
            corridor_index = None
        elif len(contained) == 1:
            selected_reference = contained[0].reference
            corridor_index = contained[0].corridor_index
            if global_resolution.selected_reference not in corridor_index_by_ref:
                status = "ambiguous"
                reason_code = "global_non_corridor_with_authorized_candidate"
            else:
                status = "resolved"
                reason_code = "inside_authorized_corridor"
        elif len(candidates) == 1:
            selected_reference = candidates[0].reference
            corridor_index = candidates[0].corridor_index
            status = "resolved"
            reason_code = "within_authorized_snap"
        else:
            status = "ambiguous"
            reason_code = "authorized_snap_ambiguous"
            selected_reference = None
            corridor_index = None
        return CorridorPointResolution(
            status=status,
            reason_code=reason_code,
            selected_reference=selected_reference,
            corridor_index=corridor_index,
            candidates=candidates,
            global_resolution=global_resolution,
        )

    def nearest_reachable_point(
        self,
        start: Vec3,
        target: Vec3,
        *,
        max_horizontal_m: float = 6.0,
        max_vertical_m: float = 3.0,
        max_polygons: int = 400000,
        start_resolution: "PointResolution | None" = None,
    ) -> Vec3 | None:
        """The centre of the polygon reachable from ``start`` nearest ``target``.

        For a target whose own floor is not linked to the start's (an NPC
        on a floor the navmesh leaves apart): the polygons linked to the
        start's are walked outward and the one whose centre is horizontally
        nearest the target, within the given reach, is kept.
        """
        resolution = start_resolution or self.resolve_point(start)
        if not resolution.ok:
            return None
        origin = resolution.selected_reference
        seen = {origin}
        queue = deque([origin])
        best, best_distance = None, float("inf")
        while queue and len(seen) <= max_polygons:
            reference = queue.popleft()
            polygon = self.polygons.get(reference)
            if polygon is not None:
                centre = polygon.centroid
                horizontal = math.hypot(centre.x - target.x, centre.y - target.y) / 100.0
                vertical = abs(centre.z - target.z) / 100.0
                if horizontal <= max_horizontal_m and vertical <= max_vertical_m and horizontal < best_distance:
                    best, best_distance = centre, horizontal
            for portal in self.adjacency.get(reference, ()):
                if portal.to_reference not in seen:
                    seen.add(portal.to_reference)
                    queue.append(portal.to_reference)
        return best

    def resolve_point(
        self,
        point: Vec3,
        *,
        max_snap_distance_m: float = 8.0,
        max_vertical_separation_m: float = 20.0,
        reject_off_mesh: bool = True,
    ) -> PointResolution:
        if not all(_finite(value) for value in (point.x, point.y, point.z)):
            return PointResolution("unresolved", "point_non_finite", point, None, None, None, None, 0)
        candidates = self._indexed_point_candidates(
            point, max_snap_distance_m, max_vertical_separation_m, reject_off_mesh,
        )
        if not candidates:
            # Preserve the legacy diagnostic distinction for an unresolved
            # point.  This fallback is only reached when the spatial index
            # finds no possible polygon, so it cannot add latency to a valid
            # live route; it also makes an index miss fail closed rather than
            # silently changing the resolver's reason semantics.
            return self.resolve_point_full_scan(
                point,
                max_snap_distance_m=max_snap_distance_m,
                max_vertical_separation_m=max_vertical_separation_m,
                reject_off_mesh=reject_off_mesh,
            )
        return self._indexed_point_resolution(point, candidates, max_snap_distance_m)

    def resolve_point_if_resolvable(
        self,
        point: Vec3,
        *,
        max_snap_distance_m: float = 8.0,
        max_vertical_separation_m: float = 20.0,
        reject_off_mesh: bool = True,
    ) -> PointResolution:
        """``resolve_point`` for callers that use only a resolved result.

        Where the spatial index finds no candidate, ``resolve_point`` scans
        every polygon to name the nearest one in an unresolved result's
        diagnostics (live at Kalm: 0.8 s over 23,851 polygons, 32 times in
        one contour search, 25 s of a recovery preflight). A polygon can
        resolve the point only within the snap bound, so only polygons whose
        bounding box lies within that bound plus the containment margin are
        checked here. If one of them could resolve the point the full scan
        runs as before; otherwise no polygon can and the result is unresolved
        without naming the nearest one. A resolved result is therefore always
        identical to ``resolve_point``'s.
        """
        if not all(_finite(value) for value in (point.x, point.y, point.z)):
            return PointResolution("unresolved", "point_non_finite", point, None, None, None, None, 0)
        candidates = self._indexed_point_candidates(
            point, max_snap_distance_m, max_vertical_separation_m, reject_off_mesh,
        )
        if candidates:
            return self._indexed_point_resolution(point, candidates, max_snap_distance_m)
        reach_m = max(0.0, float(max_snap_distance_m)) + self._point_resolution_containment_margin_cm / 100.0
        for reference in self._point_resolution_references(point, reach_m):
            polygon = self.polygons.get(reference)
            if polygon is None:
                continue
            if reject_off_mesh and polygon.poly_type != DT_POLYTYPE_GROUND:
                continue
            _, _, horizontal_cm, vertical_cm, _ = self._candidate_resolution(point, polygon)
            if vertical_cm <= max_vertical_separation_m * 100.0 and horizontal_cm <= max_snap_distance_m * 100.0:
                return self.resolve_point_full_scan(
                    point,
                    max_snap_distance_m=max_snap_distance_m,
                    max_vertical_separation_m=max_vertical_separation_m,
                    reject_off_mesh=reject_off_mesh,
                )
        return PointResolution(
            "unresolved", "point_outside_navmesh", point, None, None, None, None, 0,
            {"candidate_count_scope": "bounded_no_resolvable_polygon"},
        )

    def _indexed_point_candidates(
        self,
        point: Vec3,
        max_snap_distance_m: float,
        max_vertical_separation_m: float,
        reject_off_mesh: bool,
    ) -> list[tuple[tuple[int, float, float], int, Vec3, float, float, bool]]:
        candidates: list[tuple[tuple[int, float, float], int, Vec3, float, float, bool]] = []
        for reference in self._point_resolution_references(point, max_snap_distance_m):
            polygon = self.polygons.get(reference)
            if polygon is None:
                continue
            if reject_off_mesh and polygon.poly_type != DT_POLYTYPE_GROUND:
                continue
            rank, closest, horizontal_cm, vertical_cm, contained = self._candidate_resolution(point, polygon)
            if vertical_cm > max_vertical_separation_m * 100.0:
                continue
            candidates.append((rank, reference, closest, horizontal_cm, vertical_cm, contained))
        return candidates

    @staticmethod
    def _indexed_point_resolution(
        point: Vec3,
        candidates: list[tuple[tuple[int, float, float], int, Vec3, float, float, bool]],
        max_snap_distance_m: float,
    ) -> PointResolution:
        candidates.sort(key=lambda item: (item[0], item[1]))
        _, reference, closest, horizontal_cm, vertical_cm, contained = candidates[0]
        if horizontal_cm > max_snap_distance_m * 100.0:
            return PointResolution(
                "unresolved", "point_outside_navmesh", point, None, closest,
                horizontal_cm / 100.0, vertical_cm / 100.0, len(candidates),
                {
                    "nearest_ref": reference,
                    "contained_candidate": contained,
                    "candidate_count_scope": "indexed_spatial_superset",
                },
            )
        return PointResolution(
            "resolved", "point_inside_polygon" if contained else "point_snapped_to_polygon",
            point, reference, closest, horizontal_cm / 100.0, vertical_cm / 100.0,
            len(candidates), {
                "contained_candidate": contained,
                "candidate_count_scope": "indexed_spatial_superset",
            },
        )

    def resolve_point_full_scan(
        self,
        point: Vec3,
        *,
        max_snap_distance_m: float = 8.0,
        max_vertical_separation_m: float = 20.0,
        reject_off_mesh: bool = True,
    ) -> PointResolution:
        """Reference implementation used only for deterministic equivalence tests.

        Production callers use the immutable spatial index in ``resolve_point``.
        Keeping this explicit full-scan seam makes it possible to prove that
        the indexed resolver preserves the selected polygon, reason, closest
        point and distance bounds on representative snapshots.
        """
        if not all(_finite(value) for value in (point.x, point.y, point.z)):
            return PointResolution("unresolved", "point_non_finite", point, None, None, None, None, 0)
        candidates: list[tuple[tuple[int, float, float], int, Vec3, float, float, bool]] = []
        for reference, polygon in self.polygons.items():
            if reject_off_mesh and polygon.poly_type != DT_POLYTYPE_GROUND:
                continue
            rank, closest, horizontal_cm, vertical_cm, contained = self._candidate_resolution(point, polygon)
            if vertical_cm > max_vertical_separation_m * 100.0:
                continue
            candidates.append((rank, reference, closest, horizontal_cm, vertical_cm, contained))
        if not candidates:
            return PointResolution(
                "unresolved", "no_suitable_polygon", point, None, None, None, None, 0,
                {"candidate_count_scope": "full_scan"},
            )
        candidates.sort(key=lambda item: (item[0], item[1]))
        _, reference, closest, horizontal_cm, vertical_cm, contained = candidates[0]
        if horizontal_cm > max_snap_distance_m * 100.0:
            return PointResolution(
                "unresolved", "point_outside_navmesh", point, None, closest,
                horizontal_cm / 100.0, vertical_cm / 100.0, len(candidates),
                {
                    "nearest_ref": reference,
                    "contained_candidate": contained,
                    "candidate_count_scope": "full_scan",
                },
            )
        return PointResolution(
            "resolved", "point_inside_polygon" if contained else "point_snapped_to_polygon",
            point, reference, closest, horizontal_cm / 100.0, vertical_cm / 100.0,
            len(candidates), {
                "contained_candidate": contained,
                "candidate_count_scope": "full_scan",
            },
        )

    @staticmethod
    def _heuristic(first: DetourPolygon, second: DetourPolygon) -> float:
        return _distance_m(first.centroid, second.centroid)

    def _astar(
        self,
        start_reference: int,
        end_reference: int,
        *,
        max_expansions: int,
        max_tile_crossings: int | None = None,
        blocked_transitions: Iterable[tuple[int, int]] = (),
        traversal_policy: TraversalPolicy | None = None,
    ) -> tuple[tuple[int, ...], tuple[DetourPortal, ...], dict[str, Any]] | None:
        blocked = {(int(first), int(second)) for first, second in blocked_transitions}
        policy = self.traversal_policy if traversal_policy is None else traversal_policy
        if max_tile_crossings is not None:
            frontier: list[tuple[float, float, int, int]] = [(0.0, 0.0, 0, start_reference)]
            start_state = (start_reference, 0)
            came_from: dict[tuple[int, int], tuple[tuple[int, int], DetourPortal] | None] = {start_state: None}
            cost: dict[tuple[int, int], float] = {start_state: 0.0}
            expansions = 0
            final_state: tuple[int, int] | None = None
            while frontier and expansions < max_expansions:
                _, current_cost, crossings, current = heapq.heappop(frontier)
                state = (current, crossings)
                if current_cost > cost.get(state, float("inf")) + 1e-9:
                    continue
                expansions += 1
                if current == end_reference:
                    final_state = state
                    break
                source = self.polygons[current]
                for portal in self.outgoing_portals(current, policy):
                    if (int(portal.from_reference), int(portal.to_reference)) in blocked:
                        continue
                    target = self.polygons.get(portal.to_reference)
                    if target is None:
                        continue
                    next_crossings = crossings + int(
                        source.reference.tile_index != target.reference.tile_index
                    )
                    if next_crossings > max_tile_crossings:
                        continue
                    new_cost = current_cost + self._transition_cost_m(source, target, portal, policy)
                    next_state = (target.reference.value, next_crossings)
                    if new_cost >= cost.get(next_state, float("inf")) - 1e-9:
                        continue
                    cost[next_state] = new_cost
                    came_from[next_state] = (state, portal)
                    priority = new_cost + self._heuristic(target, self.polygons[end_reference])
                    heapq.heappush(frontier, (priority, new_cost, next_crossings, target.reference.value))
            if final_state is None:
                return None
            corridor: list[int] = []
            transitions: list[DetourPortal] = []
            state = final_state
            while True:
                corridor.append(state[0])
                step = came_from[state]
                if step is None:
                    break
                previous, portal = step
                transitions.append(portal)
                state = previous
            corridor.reverse()
            transitions.reverse()
            return tuple(corridor), tuple(transitions), {
                "expansions": expansions,
                "graph_cost_m": cost[final_state],
                "max_tile_crossings": max_tile_crossings,
            }
        frontier: list[tuple[float, float, int]] = [(0.0, 0.0, start_reference)]
        came_from: dict[int, tuple[int, DetourPortal] | None] = {start_reference: None}
        cost: dict[int, float] = {start_reference: 0.0}
        expansions = 0
        while frontier and expansions < max_expansions:
            _, current_cost, current = heapq.heappop(frontier)
            if current_cost > cost.get(current, float("inf")) + 1e-9:
                continue
            expansions += 1
            if current == end_reference:
                break
            source = self.polygons[current]
            for portal in self.outgoing_portals(current, policy):
                if (int(portal.from_reference), int(portal.to_reference)) in blocked:
                    continue
                target = self.polygons.get(portal.to_reference)
                if target is None:
                    continue
                new_cost = current_cost + self._transition_cost_m(source, target, portal, policy)
                if new_cost >= cost.get(target.reference.value, float("inf")) - 1e-9:
                    continue
                cost[target.reference.value] = new_cost
                came_from[target.reference.value] = (current, portal)
                priority = new_cost + self._heuristic(target, self.polygons[end_reference])
                heapq.heappush(frontier, (priority, new_cost, target.reference.value))
        if end_reference not in came_from:
            return None
        corridor: list[int] = []
        transitions: list[DetourPortal] = []
        current = end_reference
        while True:
            corridor.append(current)
            step = came_from[current]
            if step is None:
                break
            previous, portal = step
            transitions.append(portal)
            current = previous
        corridor.reverse()
        transitions.reverse()
        return tuple(corridor), tuple(transitions), {"expansions": expansions, "graph_cost_m": cost[end_reference]}

    @staticmethod
    def _degenerate_portal(reference: int, point: Vec3) -> DetourPortal:
        return DetourPortal(reference, reference, reference, -1, -1, point, point, 128)

    @staticmethod
    def _inset_funnel_portals(
        portals: tuple[DetourPortal, ...], margin_m: float,
    ) -> tuple[DetourPortal, ...]:
        """Inset steering targets; preserve the original portal evidence.

        This is a tracking margin, not physical capsule clearance. Narrow
        portals retain their middle half. Collision gates remain independent.
        """
        if not math.isfinite(margin_m) or margin_m < 0:
            raise ValueError("portal corner inset must be finite and non-negative")
        if margin_m == 0:
            return portals
        adjusted = []
        for portal in portals:
            width_cm = math.hypot(portal.right.x - portal.left.x, portal.right.y - portal.left.y)
            fraction = min(0.25, margin_m * 100.0 / width_cm) if width_cm else 0.0
            adjusted.append(replace(
                portal,
                left=_lerp(portal.left, portal.right, fraction),
                right=_lerp(portal.left, portal.right, 1.0 - fraction),
            ))
        return tuple(adjusted)

    @staticmethod
    def _funnel(portals: tuple[DetourPortal, ...]) -> tuple[Vec3, ...]:
        if not portals:
            return tuple()
        apex = portals[0].left
        left = apex
        right = portals[0].right
        apex_index = left_index = right_index = 0
        points = [apex]
        index = 1
        epsilon = 1e-3
        while index < len(portals):
            new_left = portals[index].left
            new_right = portals[index].right

            # Tighten against the same side, then test the new boundary
            # against the opposite side. Mixing these emits false corners.
            if _cross_xy(_subtract(new_right, apex), _subtract(right, apex)) <= epsilon:
                if _same_point(apex, right) or _cross_xy(_subtract(new_right, apex), _subtract(left, apex)) > epsilon:
                    right = new_right
                    right_index = index
                else:
                    points.append(left)
                    apex = left
                    apex_index = left_index
                    left = apex
                    right = apex
                    left_index = apex_index
                    right_index = apex_index
                    index = apex_index + 1
                    continue

            if _cross_xy(_subtract(new_left, apex), _subtract(left, apex)) >= -epsilon:
                if _same_point(apex, left) or _cross_xy(_subtract(new_left, apex), _subtract(right, apex)) < -epsilon:
                    left = new_left
                    left_index = index
                else:
                    points.append(right)
                    apex = right
                    apex_index = right_index
                    left = apex
                    right = apex
                    left_index = apex_index
                    right_index = apex_index
                    index = apex_index + 1
                    continue
            index += 1
        if not _same_point(points[-1], portals[-1].left):
            points.append(portals[-1].left)
        deduplicated: list[Vec3] = []
        for point in points:
            if not deduplicated or not _same_point(deduplicated[-1], point):
                deduplicated.append(point)
        return tuple(deduplicated)

    def plan(
        self,
        start: Vec3,
        end: Vec3,
        *,
        max_expansions: int = 100_000,
        max_snap_distance_m: float = 8.0,
        max_vertical_separation_m: float = 20.0,
        max_waypoints: int = 256,
        start_resolution_override: PointResolution | None = None,
        end_resolution_override: PointResolution | None = None,
        max_tile_crossings: int | None = None,
        blocked_transitions: Iterable[tuple[int, int]] = (),
        portal_corner_inset_m: float = 0.0,
        routing_objective: str = "distance",
        traversal_policy: TraversalPolicy | None = None,
    ) -> DetourRoutePlan:
        if not isinstance(routing_objective, str) or routing_objective not in {"distance", "travel_time"}:
            raise ValueError("routing_objective must be distance or travel_time")
        blocked_transitions = tuple(blocked_transitions)
        policy = self.traversal_policy if traversal_policy is None else traversal_policy
        if max_expansions <= 0:
            unresolved = PointResolution("unresolved", "invalid_search_bound", start, None, None, None, None, 0)
            return DetourRoutePlan(
                "failed", "invalid_search_bound", polygon_corridor=(), portals=(),
                waypoints=(), total_estimated_path_length_m=None, validation={},
                diagnostics={"max_expansions": max_expansions},
                schema_version=SHIPPING_DETOUR_SCHEMA_VERSION,
                source="shipping_detour", executable_sha256=SHIPPING_EXECUTABLE_SHA256,
                requested_start=start, requested_end=end, resolved_start=None,
                resolved_end=None, start_ref=None, end_ref=None,
                start_resolution=unresolved, end_resolution=unresolved,
                movement_sent=False,
            )
        if max_waypoints <= 0:
            unresolved = PointResolution("unresolved", "invalid_waypoint_bound", start, None, None, None, None, 0)
            return DetourRoutePlan(
                "failed", "invalid_waypoint_bound", polygon_corridor=(), portals=(),
                waypoints=(), total_estimated_path_length_m=None, validation={},
                diagnostics={"max_waypoints": max_waypoints},
                schema_version=SHIPPING_DETOUR_SCHEMA_VERSION,
                source="shipping_detour", executable_sha256=SHIPPING_EXECUTABLE_SHA256,
                requested_start=start, requested_end=end, resolved_start=None,
                resolved_end=None, start_ref=None, end_ref=None,
                start_resolution=unresolved, end_resolution=unresolved,
                movement_sent=False,
            )
        start_resolution = start_resolution_override or self.resolve_point(
            start, max_snap_distance_m=max_snap_distance_m,
            max_vertical_separation_m=max_vertical_separation_m,
        )
        end_resolution = end_resolution_override or self.resolve_point(
            end, max_snap_distance_m=max_snap_distance_m,
            max_vertical_separation_m=max_vertical_separation_m,
        )
        common = {
            "schema_version": SHIPPING_DETOUR_SCHEMA_VERSION,
            "source": "shipping_detour",
            "executable_sha256": SHIPPING_EXECUTABLE_SHA256,
            "requested_start": start,
            "requested_end": end,
            "resolved_start": start_resolution.closest_point,
            "resolved_end": end_resolution.closest_point,
            "start_ref": start_resolution.selected_reference,
            "end_ref": end_resolution.selected_reference,
            "start_resolution": start_resolution,
            "end_resolution": end_resolution,
            "movement_sent": False,
        }

        def distance_fallback(reason: str, *, timed_candidate: DetourRoutePlan | None = None) -> DetourRoutePlan:
            baseline = self.plan(
                start,
                end,
                max_expansions=max_expansions,
                max_snap_distance_m=max_snap_distance_m,
                max_vertical_separation_m=max_vertical_separation_m,
                max_waypoints=max_waypoints,
                start_resolution_override=start_resolution,
                end_resolution_override=end_resolution,
                max_tile_crossings=max_tile_crossings,
                blocked_transitions=blocked_transitions,
                portal_corner_inset_m=portal_corner_inset_m,
                routing_objective="distance",
                traversal_policy=policy,
            )
            if baseline.status == "complete":
                return replace(baseline, diagnostics={
                    **dict(baseline.diagnostics),
                    "routing_objective": "travel_time",
                    "time_estimate_provisional": True,
                    "selected_candidate": "distance_fallback",
                    "time_candidate_fallback_reason": reason,
                })
            selected = timed_candidate if timed_candidate is not None and timed_candidate.status == "complete" else baseline
            return replace(selected, diagnostics={
                **dict(selected.diagnostics),
                "routing_objective": "travel_time",
                "time_estimate_provisional": True,
                "selected_candidate": "travel_time" if selected is timed_candidate else "failed",
                "time_candidate_fallback_reason": reason,
                "distance_fallback_reason": baseline.reason_code,
            })

        if not start_resolution.ok:
            return DetourRoutePlan("failed", "start_polygon_unresolved", polygon_corridor=(), portals=(), waypoints=(), total_estimated_path_length_m=None, validation={}, diagnostics=self.snapshot_diagnostics, **common)
        if not end_resolution.ok:
            return DetourRoutePlan("failed", "end_polygon_unresolved", polygon_corridor=(), portals=(), waypoints=(), total_estimated_path_length_m=None, validation={}, diagnostics=self.snapshot_diagnostics, **common)
        if start_resolution.selected_reference == end_resolution.selected_reference:
            corridor = (start_resolution.selected_reference,)
            portals = (
                self._degenerate_portal(corridor[0], start_resolution.closest_point or start),
                self._degenerate_portal(corridor[0], end_resolution.closest_point or end),
            )
            resolved_start = start_resolution.closest_point or start
            resolved_end = end_resolution.closest_point or end
            waypoints = (resolved_start, resolved_end) if not _same_point(resolved_start, resolved_end) else (resolved_start,)
            plan = self._complete_plan(common, corridor, portals, waypoints, {"expansions": 0, "graph_cost_m": 0.0}, max_waypoints)
            if routing_objective == "travel_time" and plan.status == "complete":
                from .time_aware_route import estimate_polyline_seconds
                seconds = estimate_polyline_seconds(plan.waypoints)
                return replace(plan, diagnostics={**dict(plan.diagnostics),
                    "routing_objective": "travel_time", "time_estimate_provisional": True,
                    "time_candidate_seconds": seconds, "distance_candidate_seconds": seconds,
                    "estimated_travel_seconds": seconds, "selected_candidate": "single_corridor"})
            return plan
        if routing_objective == "travel_time":
            from .time_aware_route import search_time_aware_corridor
            timed = search_time_aware_corridor(self,
                start_resolution.selected_reference, end_resolution.selected_reference,
                max_expansions=max_expansions, max_tile_crossings=max_tile_crossings,
                blocked_transitions=blocked_transitions, traversal_policy=policy)
            result = (timed.polygon_corridor, timed.transitions, {
                "routing_objective": routing_objective,
                "time_model": timed.to_dict(),
                "expansions": timed.expansions,
                "graph_cost_m": timed.graph_distance_m,
            }) if timed.ok else None
            if not timed.ok:
                return distance_fallback(f"time_search_{timed.reason_code}")
        else:
            result = self._astar(
                start_resolution.selected_reference,
                end_resolution.selected_reference,
                max_expansions=max_expansions,
                max_tile_crossings=max_tile_crossings,
                blocked_transitions=blocked_transitions,
                traversal_policy=policy,
            )
        if result is None:
            failure_diagnostics = {**self.snapshot_diagnostics, "max_expansions": max_expansions}
            hint = self._excluded_traversal_hint(
                start_resolution.selected_reference, end_resolution.selected_reference, policy,
                max_expansions=max_expansions, max_tile_crossings=max_tile_crossings,
                blocked_transitions=blocked_transitions,
            )
            if hint is not None:
                failure_diagnostics["traversal_hint"] = hint
            return DetourRoutePlan("failed", "no_linked_polygon_corridor", polygon_corridor=(), portals=(), waypoints=(), total_estimated_path_length_m=None, validation={}, diagnostics=failure_diagnostics, **common)
        corridor, transitions, astar_diagnostics = result
        portals = (
            self._degenerate_portal(corridor[0], start_resolution.closest_point or start),
            *transitions,
            self._degenerate_portal(corridor[-1], end_resolution.closest_point or end),
        )
        if any(
            portal.from_reference != corridor[index]
            or portal.to_reference != corridor[index + 1]
            for index, portal in enumerate(portals[1:-1])
        ):
            invalid_candidate = DetourRoutePlan("failed", "portal_corridor_mismatch", polygon_corridor=corridor, portals=portals, waypoints=(), total_estimated_path_length_m=None, validation={}, diagnostics=astar_diagnostics, **common)
            if routing_objective == "travel_time":
                return distance_fallback("time_candidate_validation_portal_corridor_mismatch", timed_candidate=invalid_candidate)
            return invalid_candidate
        waypoints = self._route_waypoints(portals, portal_corner_inset_m)
        astar_diagnostics = {**astar_diagnostics, "portal_corner_inset_m": portal_corner_inset_m}
        candidate = self._complete_plan(
            common, corridor, portals, waypoints, astar_diagnostics, max_waypoints,
            traversal_policy=policy,
        )
        if routing_objective == "travel_time" and candidate.status != "complete":
            return distance_fallback(f"time_candidate_validation_{candidate.reason_code}", timed_candidate=candidate)
        if routing_objective == "travel_time" and candidate.status == "complete":
            from .time_aware_route import estimate_polyline_seconds
            baseline = self.plan(start, end, max_expansions=max_expansions,
                max_snap_distance_m=max_snap_distance_m, max_vertical_separation_m=max_vertical_separation_m,
                max_waypoints=max_waypoints, start_resolution_override=start_resolution,
                end_resolution_override=end_resolution, max_tile_crossings=max_tile_crossings,
                blocked_transitions=blocked_transitions, portal_corner_inset_m=portal_corner_inset_m,
                traversal_policy=policy)
            candidate_seconds = estimate_polyline_seconds(candidate.waypoints)
            baseline_seconds = (estimate_polyline_seconds(baseline.waypoints)
                                if baseline.status == "complete" else None)
            selected = baseline if baseline_seconds is not None and baseline_seconds <= candidate_seconds else candidate
            return replace(selected, diagnostics={**dict(selected.diagnostics),
                "routing_objective": "travel_time", "time_estimate_provisional": True,
                "time_candidate_seconds": candidate_seconds, "distance_candidate_seconds": baseline_seconds,
                "estimated_travel_seconds": min(candidate_seconds, baseline_seconds) if baseline_seconds is not None else candidate_seconds,
                "selected_candidate": "distance" if selected is baseline else "travel_time",
                "time_route_candidate": timed.to_dict()})
        return candidate

    def _route_waypoints(
        self,
        portals: tuple[DetourPortal, ...],
        portal_corner_inset_m: float,
    ) -> tuple[Vec3, ...]:
        """String-pull the portals, leg by leg between off-mesh crossings.

        A crossing is entered at its departure point and left at its landing
        point, both of which the route must pass exactly, so the funnel runs
        separately on each stretch of polygons between crossings and the
        departure and landing points are the stretches' own ends.
        """
        if not any(portal.is_traversal for portal in portals):
            return self._funnel(self._inset_funnel_portals(portals, portal_corner_inset_m))
        legs: list[list[DetourPortal]] = [[]]
        for portal in portals:
            if portal.is_traversal:
                legs[-1].append(self._degenerate_portal(portal.from_reference, portal.left))
                legs.append([self._degenerate_portal(portal.to_reference, portal.right)])
            else:
                legs[-1].append(portal)
        points: list[Vec3] = []
        for leg in legs:
            points.extend(self._funnel(self._inset_funnel_portals(tuple(leg), portal_corner_inset_m)))
        return tuple(points)

    def _excluded_traversal_hint(
        self,
        start_reference: int,
        end_reference: int,
        policy: TraversalPolicy,
        *,
        max_expansions: int,
        max_tile_crossings: int | None,
        blocked_transitions: Iterable[tuple[int, int]],
    ) -> dict[str, Any] | None:
        """When no corridor exists, whether links the policy excludes would
        make one: a route that waits on an unverified link class says so."""
        if not self._traversal_portals:
            return None
        everything = EVERYTHING_POLICY
        if (
            policy.enabled_classes >= everything.enabled_classes
            and (policy.allow_reverse_of_two_way or not any(
                link.two_way_claimed for link in self.off_mesh_links
            ))
        ):
            return None
        result = self._astar(
            start_reference, end_reference,
            max_expansions=min(int(max_expansions), 20_000),
            max_tile_crossings=max_tile_crossings,
            blocked_transitions=blocked_transitions,
            traversal_policy=everything,
        )
        if result is None:
            return {"reachable_with_excluded_links": False, "policy": policy.to_dict()}
        _, transitions, _ = result
        used = [portal for portal in transitions if portal.is_traversal]
        return {
            "reachable_with_excluded_links": True,
            "classes_needed": sorted({portal.traversal_class for portal in used}),
            "uses_two_way_reverse": any(portal.traversal_reverse for portal in used),
            "crossings_needed": len(used),
            "policy": policy.to_dict(),
        }

    def _portal_link_proven(self, portal: DetourPortal) -> bool:
        if portal.is_traversal:
            return self.traversal_link_for_portal(portal) is not None
        return any(
            link.reference == portal.link_reference
            and link.edge == portal.source_edge
            and link.reference == portal.to_reference
            for link in self.polygons[portal.from_reference].links
        )

    def _complete_plan(
        self,
        common: Mapping[str, Any],
        corridor: tuple[int, ...],
        portals: tuple[DetourPortal, ...],
        waypoints: tuple[Vec3, ...],
        astar_diagnostics: Mapping[str, Any],
        max_waypoints: int,
        *,
        traversal_policy: TraversalPolicy | None = None,
    ) -> DetourRoutePlan:
        validation: dict[str, Any] = {
            "corridor_refs_present": all(reference in self.polygons for reference in corridor),
            "portal_count": len(portals),
            "transition_portals_valid": len(portals) == len(corridor) + 1 and all(
                portal.from_reference == corridor[index]
                and portal.to_reference == corridor[index + 1]
                for index, portal in enumerate(portals[1:-1], start=0)
            ),
            "portal_links_proven": all(
                self._portal_link_proven(portal)
                for portal in portals[1:-1]
            ),
            "waypoints_finite": all(
                all(_finite(component) for component in (point.x, point.y, point.z))
                for point in waypoints
            ),
            "waypoint_sequence_nonempty": bool(waypoints),
            "waypoint_start_matches_resolved": bool(waypoints) and common["resolved_start"] is not None and _same_point(waypoints[0], common["resolved_start"]),
            "waypoint_end_matches_resolved": bool(waypoints) and common["resolved_end"] is not None and _same_point(waypoints[-1], common["resolved_end"]),
            "movement_sent": False,
        }
        if len(waypoints) > max_waypoints:
            return DetourRoutePlan("failed", "waypoint_bound_exceeded", polygon_corridor=corridor, portals=portals, waypoints=waypoints[:max_waypoints], total_estimated_path_length_m=None, validation=validation, diagnostics=dict(astar_diagnostics), **common)
        if not validation["corridor_refs_present"] or not validation["transition_portals_valid"] or not validation["portal_links_proven"]:
            return DetourRoutePlan("failed", "route_validation_failed", polygon_corridor=corridor, portals=portals, waypoints=(), total_estimated_path_length_m=None, validation=validation, diagnostics=dict(astar_diagnostics), **common)
        if not validation["waypoints_finite"] or not validation["waypoint_sequence_nonempty"]:
            return DetourRoutePlan("failed", "waypoint_validation_failed", polygon_corridor=corridor, portals=portals, waypoints=(), total_estimated_path_length_m=None, validation=validation, diagnostics=dict(astar_diagnostics), **common)
        if not validation["waypoint_start_matches_resolved"] or not validation["waypoint_end_matches_resolved"]:
            return DetourRoutePlan("failed", "waypoint_endpoint_validation_failed", polygon_corridor=corridor, portals=portals, waypoints=(), total_estimated_path_length_m=None, validation=validation, diagnostics=dict(astar_diagnostics), **common)
        length = sum(_distance_m(first, second) for first, second in zip(waypoints, waypoints[1:]))
        validation["waypoint_count"] = len(waypoints)
        validation["tile_crossings"] = sum(
            1 for first, second in zip(corridor, corridor[1:])
            if self.polygons[first].reference.tile_index != self.polygons[second].reference.tile_index
        )
        diagnostics = dict(astar_diagnostics)
        segments: tuple[Mapping[str, Any], ...] = ()
        crossings = [portal for portal in portals[1:-1] if portal.is_traversal]
        if crossings:
            built = []
            for order, portal in enumerate(crossings):
                link = self.traversal_link_for_portal(portal)
                if link is None:
                    return DetourRoutePlan("failed", "route_validation_failed", polygon_corridor=corridor, portals=portals, waypoints=(), total_estimated_path_length_m=None, validation=validation, diagnostics=diagnostics, **common)
                built.append(segment_for_link(link, reverse=portal.traversal_reverse, order=order))
            segments = with_waypoint_indices(built, waypoints)
            policy = self.traversal_policy if traversal_policy is None else traversal_policy
            validation["traversal_count"] = len(segments)
            validation["traversal_links_proven"] = True
            diagnostics["traversal"] = plan_traversal_diagnostics(segments, policy)
        return DetourRoutePlan("complete", "route_plan_complete", polygon_corridor=corridor, portals=portals, waypoints=waypoints, total_estimated_path_length_m=length / 1.0, validation=validation, diagnostics=diagnostics, traversal_segments=segments, **common)


def load_shipping_detour_topology(path: str | Path) -> DetourTopologyGraph:
    """Load a preserved read-only shipping Detour geometry snapshot."""
    return DetourTopologyGraph.from_json(path)


def generate_shipping_detour_route_plan(
    path: str | Path,
    start: Vec3,
    end: Vec3,
    **kwargs: Any,
) -> DetourRoutePlan:
    """Convenience API for offline Point-A-to-Point-B planning."""
    return load_shipping_detour_topology(path).plan(start, end, **kwargs)
