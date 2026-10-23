"""Area-map <-> world coordinate conversion from the game's own projection.

The map reader (``map_query.lua``) feeds world points around the player through
``UEndNaviMap::ConvertToPosition2D`` and reports the results.  This module fits
an exact 2D affine transform to those pairs, inverts it, and aligns the
conversion space with the NaviMap widget's local space.  Height is not part of
the map: ``HeightGrid`` samples the menu's ``UEndMenuNaviMapHeightData`` when it
was exported and agrees with the player's own height.

Everything here is pure arithmetic.  Callers decide what a failed check means;
every result carries the evidence that produced it.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .navigation_values import MissionError

MIN_CALIBRATION_PAIRS = 3
MAX_FIT_RESIDUAL_PX = 1.0
# Map-local distance under which the player indicator and the projected
# player point are treated as the same point (icon anchor jitter).
DIRECT_ALIGNMENT_PX = 6.0
MAX_ALIGNMENT_OFFSET_FRACTION = 1.5
# Player telemetry is the capsule centre; the ground is roughly a metre lower.
PLAYER_CAPSULE_HALF_HEIGHT_CM = 90.0
HEIGHT_AGREEMENT_CM = 400.0


def _point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        x, y = float(value["x"]), float(value["y"])
    except (KeyError, TypeError, ValueError):
        return None
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


def _point3(value: Any) -> tuple[float, float, float] | None:
    xy = _point(value)
    if xy is None:
        return None
    try:
        z = float(value["z"])
    except (KeyError, TypeError, ValueError):
        return None
    return (xy[0], xy[1], z) if math.isfinite(z) else None


def _solve3(m: list[list[float]], v: list[float]) -> list[float] | None:
    """Solve a 3x3 system by Cramer's rule; None when singular."""
    def det(a: list[list[float]]) -> float:
        return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
                - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
                + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    d = det(m)
    scale = max(abs(item) for row in m for item in row) or 1.0
    if abs(d) <= 1e-12 * scale ** 3:
        return None
    result = []
    for column in range(3):
        replaced = [row[:] for row in m]
        for r in range(3):
            replaced[r][column] = v[r]
        result.append(det(replaced) / d)
    return result


@dataclass(frozen=True)
class Affine2D:
    """``out = [[a, b], [c, d]] @ in + [tx, ty]``."""

    a: float
    b: float
    c: float
    d: float
    tx: float
    ty: float

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return (self.a * x + self.b * y + self.tx, self.c * x + self.d * y + self.ty)

    @property
    def determinant(self) -> float:
        return self.a * self.d - self.b * self.c

    def inverse(self) -> "Affine2D":
        det = self.determinant
        if not math.isfinite(det) or abs(det) < 1e-15:
            raise MissionError("map_projection_singular")
        ia, ib, ic, id_ = self.d / det, -self.b / det, -self.c / det, self.a / det
        return Affine2D(ia, ib, ic, id_, -(ia * self.tx + ib * self.ty), -(ic * self.tx + id_ * self.ty))

    def linear(self, dx: float, dy: float) -> tuple[float, float]:
        return (self.a * dx + self.b * dy, self.c * dx + self.d * dy)


def fit_affine(pairs: Sequence[tuple[tuple[float, float], tuple[float, float]]]) -> tuple[Affine2D, float]:
    """Least-squares affine fit ``source -> target``; returns (transform, max residual)."""
    if len(pairs) < MIN_CALIBRATION_PAIRS:
        raise MissionError("map_calibration_insufficient")
    # Centre the source for numerical stability (world coordinates are ~1e5 cm).
    cx = sum(p[0][0] for p in pairs) / len(pairs)
    cy = sum(p[0][1] for p in pairs) / len(pairs)
    rows = [(s[0] - cx, s[1] - cy, 1.0) for s, _ in pairs]
    normal = [[sum(r[i] * r[j] for r in rows) for j in range(3)] for i in range(3)]
    solutions = []
    for axis in range(2):
        rhs = [sum(r[i] * t[axis] for r, (_, t) in zip(rows, pairs)) for i in range(3)]
        solved = _solve3(normal, rhs)
        if solved is None:
            raise MissionError("map_calibration_degenerate")
        solutions.append(solved)
    (a, b, t0), (c, d, t1) = solutions
    transform = Affine2D(a, b, c, d, t0 - a * cx - b * cy, t1 - c * cx - d * cy)
    residual = max(math.dist(transform.apply(*s), t) for s, t in pairs)
    return transform, residual


@dataclass(frozen=True)
class HeightGrid:
    base_x: float
    base_y: float
    base_z: float
    block_size: float
    blocks_x: int
    blocks_y: int
    samples: tuple[float | None, ...]
    x_fastest: bool
    source_id: str

    def _value(self, i: int, j: int) -> float | None:
        if not (0 <= i < self.blocks_x and 0 <= j < self.blocks_y):
            return None
        index = j * self.blocks_x + i if self.x_fastest else i * self.blocks_y + j
        return self.samples[index] if index < len(self.samples) else None

    def sample(self, x: float, y: float) -> float | None:
        """Bilinear height (world cm) at world XY; None outside or on holes."""
        fx = (x - self.base_x) / self.block_size
        fy = (y - self.base_y) / self.block_size
        i, j = math.floor(fx), math.floor(fy)
        if fx == self.blocks_x - 1:
            i -= 1
        if fy == self.blocks_y - 1:
            j -= 1
        corners = [self._value(i, j), self._value(i + 1, j), self._value(i, j + 1), self._value(i + 1, j + 1)]
        if any(value is None for value in corners):
            return None
        u, v = fx - i, fy - j
        h00, h10, h01, h11 = corners  # type: ignore[misc]
        value = (h00 * (1 - u) * (1 - v) + h10 * u * (1 - v) + h01 * (1 - u) * v + h11 * u * v)
        return self.base_z + value


def height_grids(records: Any, player: tuple[float, float, float]) -> tuple[HeightGrid | None, list[dict[str, Any]]]:
    """Choose the exported height grid that agrees with the player's height.

    The grid's memory order is not documented, so both orders are tried and
    only one that places the ground within ``HEIGHT_AGREEMENT_CM`` below the
    player's capsule is accepted.  Returns (grid or None, per-grid diagnostics).
    """
    diagnostics: list[dict[str, Any]] = []
    accepted: list[tuple[float, HeightGrid]] = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, Mapping):
            continue
        entry: dict[str, Any] = {"id": record.get("id"), "sample_count": record.get("sample_count")}
        diagnostics.append(entry)
        base = _point3(record.get("base_position"))
        blocks = record.get("blocks") if isinstance(record.get("blocks"), Mapping) else {}
        samples = record.get("samples")
        try:
            block_size = float(record.get("block_size"))
            bx, by = int(blocks.get("x")), int(blocks.get("y"))
        except (TypeError, ValueError):
            entry["rejected"] = "height_grid_metadata_invalid"
            continue
        if base is None or not math.isfinite(block_size) or block_size <= 0 or bx < 2 or by < 2:
            entry["rejected"] = "height_grid_metadata_invalid"
            continue
        if not isinstance(samples, list) or len(samples) != bx * by:
            entry["rejected"] = "height_grid_samples_unavailable"
            continue
        values = tuple(float(s) if isinstance(s, (int, float)) and not isinstance(s, bool)
                       and math.isfinite(s) else None for s in samples)
        for x_fastest in (True, False):
            grid = HeightGrid(base[0], base[1], base[2], block_size, bx, by, values, x_fastest, str(record.get("id")))
            ground = grid.sample(player[0], player[1])
            key = "player_ground_x_fastest" if x_fastest else "player_ground_y_fastest"
            entry[key] = ground
            if ground is None:
                continue
            error = abs((player[2] - PLAYER_CAPSULE_HALF_HEIGHT_CM) - ground)
            if error <= HEIGHT_AGREEMENT_CM:
                accepted.append((error, grid))
    if not accepted:
        return None, diagnostics
    accepted.sort(key=lambda item: item[0])
    # Two orders or grids that fit equally well but disagree elsewhere cannot
    # be told apart from one player sample.
    if (len(accepted) > 1 and abs(accepted[0][0] - accepted[1][0]) < 1.0
            and not _equivalent(accepted[0][1], accepted[1][1])):
        return None, diagnostics + [{"rejected": "height_grid_ambiguous"}]
    return accepted[0][1], diagnostics


def _equivalent(first: HeightGrid, second: HeightGrid) -> bool:
    if (first.base_x, first.base_y, first.base_z, first.block_size, first.blocks_x, first.blocks_y) != (
            second.base_x, second.base_y, second.base_z, second.block_size, second.blocks_x, second.blocks_y):
        return False
    return all(first._value(i, j) == second._value(i, j)
               for i in range(first.blocks_x) for j in range(first.blocks_y))


@dataclass(frozen=True)
class MapProjection:
    """World XY <-> NaviMap local XY (and optionally viewport pixels)."""

    world_to_convert: Affine2D
    convert_to_local: tuple[float, float]
    alignment: str
    fit_residual_px: float
    local_size: tuple[float, float] | None
    local_to_pixel: Affine2D | None
    height: HeightGrid | None

    @property
    def world_to_local(self) -> Affine2D:
        t = self.world_to_convert
        dx, dy = self.convert_to_local
        return Affine2D(t.a, t.b, t.c, t.d, t.tx + dx, t.ty + dy)

    def to_local(self, x: float, y: float) -> tuple[float, float]:
        return self.world_to_local.apply(x, y)

    def to_world(self, x: float, y: float) -> tuple[float, float]:
        return self.world_to_local.inverse().apply(x, y)

    def pixel_to_local(self, x: float, y: float) -> tuple[float, float]:
        if self.local_to_pixel is None:
            raise MissionError("map_screen_conversion_unavailable")
        return self.local_to_pixel.inverse().apply(x, y)

    def local_to_pixel_point(self, x: float, y: float) -> tuple[float, float] | None:
        return None if self.local_to_pixel is None else self.local_to_pixel.apply(x, y)

    def cm_per_local_unit(self) -> float:
        return 1.0 / math.sqrt(abs(self.world_to_convert.determinant))

    def in_view(self, x: float, y: float) -> bool | None:
        if self.local_size is None:
            return None
        return 0.0 <= x <= self.local_size[0] and 0.0 <= y <= self.local_size[1]

    def diagnostics(self) -> dict[str, Any]:
        t = self.world_to_convert
        return {
            "alignment": self.alignment,
            "fit_residual_px": self.fit_residual_px,
            "cm_per_map_unit": self.cm_per_local_unit(),
            "rotation_degrees": math.degrees(math.atan2(t.c, t.a)),
            "mirrored": t.determinant < 0,
            "convert_to_local_offset": {"x": self.convert_to_local[0], "y": self.convert_to_local[1]},
            "screen_conversion": self.local_to_pixel is not None,
            "height_source": self.height.source_id if self.height else None,
            "height_order": (None if self.height is None else
                             "x_fastest" if self.height.x_fastest else "y_fastest"),
        }


def widget_local(record: Any) -> tuple[float, float] | None:
    """NaviMap-local centre of a widget record from the map reader."""
    if not isinstance(record, Mapping):
        return None
    placement = record.get("placement")
    return _point(placement.get("navi_local")) if isinstance(placement, Mapping) else None


def build_projection(snapshot: Mapping[str, Any]) -> MapProjection:
    """Fit and validate the projection carried by a ready map snapshot."""
    pairs = []
    for sample in snapshot.get("calibration") or []:
        if not isinstance(sample, Mapping):
            continue
        world, mapped = _point(sample.get("world")), _point(sample.get("map"))
        if world is not None and mapped is not None:
            pairs.append((world, mapped))
    assumed_axes = False
    if not pairs:
        pairs = _pixel_per_cm_pairs(snapshot)
        assumed_axes = True
    transform, residual = fit_affine(pairs)
    if residual > MAX_FIT_RESIDUAL_PX:
        raise MissionError("map_projection_not_affine")
    if abs(transform.determinant) < 1e-12:
        raise MissionError("map_projection_singular")

    navi = snapshot.get("navi_map") if isinstance(snapshot.get("navi_map"), Mapping) else {}
    size = _point(navi.get("local_size")) or _point(navi.get("size_property"))
    player = _point3(snapshot.get("player_position"))
    if player is None:
        raise MissionError("map_player_position_unavailable")

    # ConvertToPosition2D may return widget-local units or a view-relative
    # variant. The player indicator is the one widget with a known world point.
    projected_player = transform.apply(player[0], player[1])
    indicator = widget_local(snapshot.get("player_indicator"))
    offset = (0.0, 0.0)
    if indicator is None:
        alignment = "unverified"
    else:
        dx, dy = indicator[0] - projected_player[0], indicator[1] - projected_player[1]
        if math.hypot(dx, dy) <= DIRECT_ALIGNMENT_PX:
            alignment = "direct"
        else:
            limit = MAX_ALIGNMENT_OFFSET_FRACTION * max(size) if size else math.inf
            if math.hypot(dx, dy) > limit:
                raise MissionError("map_projection_alignment_implausible")
            offset, alignment = (dx, dy), "player_indicator_offset"

    local_to_pixel = None
    corners = navi.get("viewport_corners")
    if isinstance(corners, list) and len(corners) >= 3:
        pixel_pairs = []
        for corner in corners:
            if isinstance(corner, Mapping):
                local, pixel = _point(corner.get("local_point")), _point(corner.get("pixel"))
                if local is not None and pixel is not None:
                    pixel_pairs.append((local, pixel))
        try:
            pixel_transform, pixel_residual = fit_affine(pixel_pairs)
            if pixel_residual <= MAX_FIT_RESIDUAL_PX and abs(pixel_transform.determinant) > 1e-9:
                local_to_pixel = pixel_transform
        except MissionError:
            local_to_pixel = None

    grid, _ = height_grids(snapshot.get("height_data"), player)
    if assumed_axes:
        alignment = "pixel_per_cm_north_up_assumed"
    return MapProjection(transform, offset, alignment, residual, size, local_to_pixel, grid)


# World offset (cm) used to synthesise calibration pairs.
PIXEL_PER_CM_SPAN_CM = 10000.0


def _pixel_per_cm_pairs(snapshot: Mapping[str, Any]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Calibration pairs from the map's scale and the player indicator.

    Live 2026-09-26 UEndNaviMap::ConvertToPosition2D could not be called
    (it aborted the sampler), so the reader skips calibration. The NaviMap's
    PixelPerCm gives the scale and the player indicator the player's map
    point; the axes are assumed north-up (world +X right, world +Y down on
    the map). Pins' displayed distances still check the scale downstream.
    """
    navi = snapshot.get("navi_map") if isinstance(snapshot.get("navi_map"), Mapping) else {}
    scale = navi.get("pixel_per_cm")
    player = _point3(snapshot.get("player_position"))
    indicator = widget_local(snapshot.get("player_indicator"))
    if not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0             or player is None or indicator is None:
        raise MissionError("map_calibration_insufficient")
    pairs = []
    for dx, dy in ((0.0, 0.0), (PIXEL_PER_CM_SPAN_CM, 0.0), (0.0, PIXEL_PER_CM_SPAN_CM)):
        pairs.append(((player[0] + dx, player[1] + dy),
                      (indicator[0] + dx * scale, indicator[1] + dy * scale)))
    return pairs


_DISTANCE_TEXT = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(k?m)\s*$", re.IGNORECASE)


def parse_distance_text(value: Any) -> float | None:
    """Parse an icon's on-map distance label ("120m", "1.2km") into metres."""
    if not isinstance(value, str):
        return None
    match = _DISTANCE_TEXT.match(value)
    if not match:
        return None
    amount = float(match.group(1).replace(",", "."))
    return amount * 1000.0 if match.group(2).lower() == "km" else amount


def distance_agreement(records: Iterable[tuple[float, float]]) -> dict[str, Any]:
    """Summarise |computed - displayed| distance errors for labelled icons."""
    errors = [abs(computed - shown) for computed, shown in records]
    if not errors:
        return {"checked": 0}
    return {"checked": len(errors), "max_error_m": max(errors),
            "mean_error_m": sum(errors) / len(errors)}
