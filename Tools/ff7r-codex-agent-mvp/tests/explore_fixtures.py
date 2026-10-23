"""Synthetic navmesh graphs and a PNG reader for the explore-mode tests."""
from __future__ import annotations

import struct
import zlib

from ff7r_agent.detour_route import DetourTopologyGraph

CELL_CM = 500.0


def shipping_ref(tile_index: int, poly_index: int, salt: int = 1) -> int:
    return (salt << (14 + 31)) | (tile_index << 31) | poly_index


def build_graph(floors: list[dict]) -> DetourTopologyGraph:
    """A graph of 5 m square polygons. Each floor is ``{"cells": [(i, j), ...], "z": cm}``.

    Neighbouring cells of one floor are linked; floors never link to each other,
    so each floor is its own navmesh island (a stacked floor shares x/y with
    another one). World x = i * 5 m, y = j * 5 m.
    """
    vertices: list[dict] = []
    polygons: list[dict] = []
    for floor in floors:
        cells = [tuple(cell) for cell in floor["cells"]]
        first_index = len(polygons)
        index = {cell: first_index + number for number, cell in enumerate(cells)}
        for (i, j) in cells:
            base = len(vertices)
            x0, y0, z = i * CELL_CM, j * CELL_CM, float(floor.get("z", 0.0))
            vertices += [{"x": x0, "y": y0, "z": z}, {"x": x0 + CELL_CM, "y": y0, "z": z},
                         {"x": x0 + CELL_CM, "y": y0 + CELL_CM, "z": z}, {"x": x0, "y": y0 + CELL_CM, "z": z}]
            links = []
            # edge 0 faces -y, 1 faces +x, 2 faces +y, 3 faces -x
            for edge, (di, dj) in enumerate(((0, -1), (1, 0), (0, 1), (-1, 0))):
                neighbour = (i + di, j + dj)
                if neighbour in index:
                    links.append({"ref": shipping_ref(0, index[neighbour]), "edge": edge, "next": 0xFFFFFFFF,
                                  "side": 128, "bmin": 0, "bmax": 0})
            polygons.append({"index": index[(i, j)], "vertex_indices": [base, base + 1, base + 2, base + 3],
                             "flags": 1, "area": 0, "type": 0, "links": links})
    tile = {"x": 0, "y": 0, "layer": 0, "tile_index_valid": True, "tile_index": 0, "salt": 1,
            "vertices": vertices, "polygons": polygons}
    return DetourTopologyGraph.from_dict({
        "source": "ue4ss_native_detour_geometry_dump", "movement_sent": False,
        "coordinate_space": "unreal", "tiles": [tile]})


def corridor_world() -> DetourTopologyGraph:
    """A 50 m x 15 m ground corridor (island A), a separate 15 m square 30 m away (island B)
    and a 15 m x 10 m floor 5 m above the corridor's middle (island C)."""
    return build_graph([
        {"cells": [(i, j) for i in range(10) for j in range(3)], "z": 0.0},
        {"cells": [(i, j) for i in range(3) for j in range(6, 9)], "z": 0.0},
        {"cells": [(i, j) for i in range(3, 6) for j in range(2)], "z": 500.0},
    ])


def decode_png(data: bytes) -> tuple[int, int, list[bytes]]:
    """(width, height, rows of RGB bytes) of a PNG written with filter type 0 rows."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    position, compressed = 8, b""
    while position < len(data):
        length, tag = struct.unpack(">I4s", data[position:position + 8])
        body = data[position + 8:position + 8 + length]
        crc = struct.unpack(">I", data[position + 8 + length:position + 12 + length])[0]
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF
        if tag == b"IDAT":
            compressed += body
        position += 12 + length
    raw = zlib.decompress(compressed)
    stride = width * 3 + 1
    rows = []
    for row in range(height):
        chunk = raw[row * stride:(row + 1) * stride]
        assert chunk[0] == 0
        rows.append(chunk[1:])
    return width, height, rows


def pixel(rows: list[bytes], x: int, y: int) -> tuple[int, int, int]:
    return tuple(rows[y][x * 3:x * 3 + 3])  # type: ignore[return-value]
