"""Synthetic navmesh payloads with off-mesh connections, and the girder fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ff7r_agent.detour_route import PolygonReference

GIRDER_FIXTURE = Path(__file__).parent / "fixtures" / "offmesh-girder-tiles.json"
SALT = 1
TILE_INDEX = 1

# Start and goal of the live failure: the prologue's start island to the next.
GIRDER_START = (4355.0, 66760.0, -93.0)
GIRDER_GOAL = (4592.0, 67343.0, -90.0)


def girder_payload() -> dict[str, Any]:
    return json.loads(GIRDER_FIXTURE.read_text(encoding="utf-8"))


def ref(poly_index: int) -> int:
    return PolygonReference.make(SALT, TILE_INDEX, poly_index).value


def recast(point: tuple[float, float, float]) -> list[float]:
    """An Unreal point in the capture's recast space: x=-recast.x, y=-recast.z, z=recast.y."""
    return [-point[0], point[2], -point[1]]


def connection(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    *,
    flags: int = 64,
    from_pad: int = 0,
    to_pad: int = 1,
    start_link: bool = True,
    end_link: bool = True,
) -> dict[str, Any]:
    return {
        "start": start, "end": end, "flags": flags, "from_pad": from_pad, "to_pad": to_pad,
        "start_link": start_link, "end_link": end_link,
    }


def pads_payload(
    connections: list[dict[str, Any]],
    *,
    pad_count: int = 2,
    gap_cm: float = 200.0,
    rise_cm: tuple[float, ...] = (0.0, 0.0),
) -> dict[str, Any]:
    """Square 4 m floors in a row, each its own island, joined only by the
    given off-mesh connections (the points are given in Unreal coordinates and
    written in the capture's recast space).

    Pad ``n`` spans x ``n * (400 + gap)`` to ``+400``, y 0 to 400, at height
    ``rise_cm[n]``.  A connection's points are given in absolute coordinates;
    its off-mesh polygon links the pads named by ``from_pad`` and ``to_pad``
    as the shipping tiles do: ``edge 0`` to the start polygon with side 0xD0
    (0xF0 when two-way) and ``edge 1`` to the end polygon with side 0xC0
    (0xE0 when two-way).
    """
    vertices: list[dict[str, float]] = []
    polygons: list[dict[str, Any]] = []
    for pad in range(pad_count):
        x0 = pad * (400.0 + gap_cm)
        z = rise_cm[pad] if pad < len(rise_cm) else 0.0
        base = len(vertices)
        for dx, dy in ((0.0, 0.0), (400.0, 0.0), (400.0, 400.0), (0.0, 400.0)):
            x, y, z_ = recast((x0 + dx, dy, z))
            vertices.append({"x": x, "y": y, "z": z_})
        polygons.append({
            "index": pad, "first_link": 0, "vertex_count": 4,
            "vertex_indices": [base, base + 1, base + 2, base + 3],
            "neighbors": [0, 0, 0, 0], "flags": 1, "area": 63, "type": 0, "links": [],
        })
    off_mesh = []
    for number, item in enumerate(connections):
        index = pad_count + number
        base = len(vertices)
        for point in (item["start"], item["end"]):
            x, y, z_ = recast(point)
            vertices.append({"x": x, "y": y, "z": z_})
        two_way = bool(item["flags"] & 0x80)
        links = []
        if item["start_link"]:
            links.append({"ref": ref(item["from_pad"]), "next": 0xFFFFFFFF, "edge": 0,
                          "side": 0xF0 if two_way else 0xD0, "bmin": 0, "bmax": 0})
        if item["end_link"]:
            links.append({"ref": ref(item["to_pad"]), "next": 0xFFFFFFFF, "edge": 1,
                          "side": 0xE0 if two_way else 0xC0, "bmin": 0, "bmax": 0})
        polygons.append({
            "index": index, "first_link": 0, "vertex_count": 2, "vertex_indices": [base, base + 1],
            "neighbors": [0, 0, 0, 0, 0, 0], "flags": 0x8001, "area": 38, "type": 1, "links": links,
        })
        off_mesh.append({
            "start": recast(item["start"]), "end": recast(item["end"]), "radius": 30, "height": 40,
            "user_id": 0, "poly": index, "side": 128, "flags": item["flags"],
        })
    return {
        "source": "ue4ss_native_detour_geometry_dump",
        "movement_sent": False,
        "coordinate_space": "recast",
        "status": "complete",
        "reason_code": "geometry_export_complete",
        "tiles": [{
            "lookup_index": 0, "tile_index": TILE_INDEX, "tile_index_valid": True, "salt": SALT,
            "x": 0, "y": 0, "layer": 0, "vertices": vertices, "polygons": polygons,
            "off_mesh_connections": off_mesh,
        }],
    }
