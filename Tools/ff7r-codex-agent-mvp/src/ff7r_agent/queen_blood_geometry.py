"""Locate Queen's Blood's placement cursor on any board cell.

The placement preview only marks cells the held card may use, so on any
other cell the cursor was invisible to telemetry. Live, navigating by an
estimate through those cells meant a dropped or lagging press was only
discovered at the target, and the cursor was driven back to the corner and
tried again -- the back-and-forth seen on screen.

The probe now publishes world locations: the held card (CardPlacementActor)
hovers over the cursor cell, and placed cards (CardBoardActors) sit on their
cells. The card game always renders the same 0000-COMON_CardGame_Gimmick
layout, so the cell grid is fixed. This module learns it from observed
(cell, location) samples -- held-card locations over preview-confirmed cells
first, placed-card locations otherwise -- fits ``centre = origin + column * u
+ row * v`` by least squares, persists the samples, and maps a held-card
location to the nearest cell.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Iterable

from .queen_blood import BOARD_COLUMNS, BOARD_ROWS

Cell = tuple[int, int]
Point = tuple[float, float]

# A location counts as over a cell within this fraction of the smaller cell
# spacing from its centre; between cells (the card gliding) it is unknown.
HOVER_TOLERANCE = 0.35
MIN_FIT_CELLS = 3


def fit_grid(samples: dict[Cell, Point]) -> tuple[Point, Point, Point] | None:
    """Least-squares (origin, u per column, v per row), or None if the
    sampled cells do not span both board axes."""
    if len(samples) < MIN_FIT_CELLS:
        return None
    rows = [(1.0, float(column), float(row)) for (row, column) in samples]
    # Normal equations A^T A p = A^T b for each coordinate.
    ata = [[sum(a[i] * a[j] for a in rows) for j in range(3)] for i in range(3)]
    solution = []
    for axis in range(2):
        atb = [sum(a[i] * point[axis] for a, point in zip(rows, samples.values())) for i in range(3)]
        solved = _solve3(ata, atb)
        if solved is None:
            return None
        solution.append(solved)
    (ox, ux, vx), (oy, uy, vy) = solution
    return (ox, oy), (ux, uy), (vx, vy)


def _solve3(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    m = [row[:] + [value] for row, value in zip(matrix, vector)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda r: abs(m[r][column]))
        if abs(m[pivot][column]) < 1e-9:
            return None
        m[column], m[pivot] = m[pivot], m[column]
        for r in range(3):
            if r != column:
                factor = m[r][column] / m[column][column]
                m[r] = [a - factor * b for a, b in zip(m[r], m[column])]
    return [m[i][3] / m[i][i] for i in range(3)]


def nearest_cell(grid: tuple[Point, Point, Point], point: Point) -> Cell | None:
    (ox, oy), (ux, uy), (vx, vy) = grid
    spacing = min(math.hypot(ux, uy), math.hypot(vx, vy))
    if spacing < 1e-6:
        return None
    best: tuple[float, Cell] | None = None
    for row in range(BOARD_ROWS):
        for column in range(BOARD_COLUMNS):
            cx = ox + column * ux + row * vx
            cy = oy + column * uy + row * vy
            distance = math.hypot(point[0] - cx, point[1] - cy)
            if best is None or distance < best[0]:
                best = (distance, (row, column))
    if best is None or best[0] > HOVER_TOLERANCE * spacing:
        return None
    return best[1]


class BoardGeometry:
    """Running per-cell mean locations, persisted between matches."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._samples: dict[str, dict[Cell, tuple[float, float, int]]] = {"hover": {}, "board": {}}
        self._grid_cache: tuple[Point, Point, Point] | None = None
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for kind in ("hover", "board"):
            for item in data.get(kind, []) if isinstance(data, dict) else []:
                try:
                    cell = (int(item["row"]), int(item["column"]))
                    self._samples[kind][cell] = (float(item["x"]), float(item["y"]), int(item["n"]))
                except (KeyError, TypeError, ValueError):
                    continue
        self._grid_cache = None

    def save(self) -> None:
        if self._path is None or not self._dirty:
            return
        data = {"schema_version": 1}
        for kind, cells in self._samples.items():
            data[kind] = [
                {"row": r, "column": c, "x": x, "y": y, "n": n}
                for (r, c), (x, y, n) in sorted(cells.items())
            ]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(self._path.suffix + ".tmp")
            temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(temporary, self._path)
            self._dirty = False
        except OSError:
            pass

    def observe(self, kind: str, cell: Cell, point: Point) -> None:
        cells = self._samples[kind]
        x, y, n = cells.get(cell, (0.0, 0.0, 0))
        n = min(n, 50)  # keep adapting to a moved table
        cells[cell] = ((x * n + point[0]) / (n + 1), (y * n + point[1]) / (n + 1), n + 1)
        self._grid_cache = None
        self._dirty = True

    def observe_many(self, kind: str, items: Iterable[tuple[Cell, Point]]) -> None:
        for cell, point in items:
            self.observe(kind, cell, point)

    def grid(self) -> tuple[Point, Point, Point] | None:
        if self._grid_cache is None:
            for kind in ("hover", "board"):
                fitted = fit_grid({cell: (x, y) for cell, (x, y, _n) in self._samples[kind].items()})
                if fitted is not None:
                    self._grid_cache = fitted
                    break
        return self._grid_cache

    def hovered_cell(self, point: Point | None) -> Cell | None:
        grid = self.grid()
        if grid is None or point is None:
            return None
        return nearest_cell(grid, point)
