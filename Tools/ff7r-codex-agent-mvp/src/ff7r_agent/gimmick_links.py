"""Traversal gimmicks (ladders, cracks, crawl spaces) as off-mesh links.

A topology capture leaves the floors that a field-action gimmick joins as
separate navmesh islands (live at Kalm, 2026-10-14: the air-raid shelter
reaches the town only through a crack, a crawl space and the inn-cellar
ladder).  The live gimmick scan (``scan_traversal_gimmicks``) reads each
gimmick's two ends: a ladder's lower and upper trigger volumes, a crack's or
crawl space's enter and exit volumes.  This module keeps those scans as
evidence and turns each gimmick into a pair of one-way off-mesh links, one
each way, whose ends are snapped onto the ground polygons beside them.

A passage can set the character down on floor the navmesh does not cover:
live at Kalm the crawl space ends on a two-polygon pad, and Cloud walks on,
still under the low ceiling, about 10 m to the inn cellar's floor.  Where a
crossing has been walked live, the landing it reached is kept with the
evidence (``observed_landings``) and replaces the far end for that direction.

The planner offers the links through the same traversal policy as the
capture's own off-mesh connections, and the independent validator accepts a
crossing only against the same evidence file, by its own reading.

Walking one is a held push, not pulses: once the game has taken Cloud into
the gimmick it steers him along it, and releasing the stick can stop him.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import Vec3
from .offmesh_links import (
    CLASS_CRACK,
    CLASS_CROUCH,
    CLASS_LADDER,
    CONNECTION_FLAG_DYNAMIC,
    OffMeshLink,
)


EVIDENCE_SCHEMA = "ff7r.traversal_gimmick_evidence.v1"
EVIDENCE_DIRECTORY = "TraversalGimmicks"
EVIDENCE_NAME = "evidence.json"
MAX_EVIDENCE_GIMMICKS = 4096

# The scan's kind for each gimmick a route may cross, and the ends it reads.
GIMMICK_KINDS: dict[str, tuple[str, str, str]] = {
    "ladder": (CLASS_LADDER, "lower", "upper"),
    "crack": (CLASS_CRACK, "enter", "exit"),
    "crouch": (CLASS_CROUCH, "enter", "exit"),
}
DIRECTION_FORWARD = "first_to_second"
DIRECTION_REVERSE = "second_to_first"

# A trigger volume sits on or beside the floor it serves (live: the inn
# cellar ladder's lower volume 0.35 m from the navmesh's own climb link).
SNAP_MAX_HORIZONTAL_M = 2.0
# Tight enough that a ladder's upper end never snaps to the floor below it.
SNAP_MAX_VERTICAL_M = 1.5
# A crossing starts this far in front of its trigger, along the gimmick, so
# the walk there ends facing into it: live at Kalm the walk to the ladder's
# foot clipped its frame from behind, and one into the crack ended beside
# its opening.  Where the navmesh does not reach that point, the trigger is.
LEAD_IN_CM = 90.0


@dataclass(frozen=True)
class TraversalGimmick:
    """One scanned gimmick: ``first`` and ``second`` are its two ends
    (lower/upper for a ladder, enter/exit for a passage)."""

    gimmick_id: str
    kind: str
    first: Vec3
    second: Vec3
    position: Vec3 | None = None
    yaw_degrees: float | None = None
    # Direction -> where a live crossing that way landed.
    observed_landings: Mapping[str, Vec3] = field(default_factory=dict)

    @property
    def link_class(self) -> str:
        return GIMMICK_KINDS[self.kind][0]

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.gimmick_id,
            "kind": self.kind,
            "first": _point(self.first),
            "second": _point(self.second),
        }
        if self.position is not None:
            value["position"] = _point(self.position)
        if self.yaw_degrees is not None:
            value["yaw_degrees"] = self.yaw_degrees
        if self.observed_landings:
            value["observed_landings"] = {
                direction: _point(point) for direction, point in sorted(self.observed_landings.items())
            }
        return value

    def ends(self, direction: str) -> tuple[Vec3, Vec3]:
        """(departure, landing) crossing ``direction``; a landing observed
        live that way replaces the far end."""
        departure, landing = (self.first, self.second) if direction == DIRECTION_FORWARD else (self.second, self.first)
        return departure, self.observed_landings.get(direction, landing)


def _point(value: Vec3) -> dict[str, float]:
    return {"x": value.x, "y": value.y, "z": value.z}


def _vec(value: Any) -> Vec3 | None:
    if not isinstance(value, Mapping):
        return None
    try:
        point = Vec3(float(value["x"]), float(value["y"]), float(value["z"]))
    except (KeyError, TypeError, ValueError):
        return None
    return point if all(math.isfinite(v) for v in (point.x, point.y, point.z)) else None


def gimmicks_from_scan(rows: Iterable[Any]) -> list[TraversalGimmick]:
    """The routable gimmicks of a ``scan_traversal_gimmicks`` snapshot.

    A row without both ends (an unreadable volume) is left out, never
    guessed; climbing walls and pipes are not routable yet.
    """
    result: list[TraversalGimmick] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        kind = row.get("kind")
        gimmick_id = row.get("id")
        if kind not in GIMMICK_KINDS or not isinstance(gimmick_id, str) or not gimmick_id:
            continue
        _, first_key, second_key = GIMMICK_KINDS[kind]
        first, second = _vec(row.get(first_key)), _vec(row.get(second_key))
        if first is None or second is None:
            continue
        rotation = row.get("rotation")
        yaw = rotation.get("yaw") if isinstance(rotation, Mapping) else None
        result.append(TraversalGimmick(
            gimmick_id=gimmick_id,
            kind=str(kind),
            first=first,
            second=second,
            position=_vec(row.get("position")),
            yaw_degrees=float(yaw) if isinstance(yaw, (int, float)) and math.isfinite(yaw) else None,
        ))
    return result


def evidence_path(telemetry_directory: str | Path) -> Path:
    return Path(telemetry_directory) / EVIDENCE_DIRECTORY / EVIDENCE_NAME


def read_evidence(path: str | Path) -> tuple[list[TraversalGimmick], str | None]:
    """The gimmicks of an evidence file and the SHA-256 of the bytes read;
    none and None if it is absent."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return [], None
    return _parse_evidence(data), hashlib.sha256(data).hexdigest().upper()


def load_evidence(path: str | Path) -> list[TraversalGimmick]:
    """The gimmicks recorded in an evidence file; none if it is absent."""
    return read_evidence(path)[0]


def _parse_evidence(data: bytes) -> list[TraversalGimmick]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("traversal_gimmick_evidence_invalid") from exc
    if not isinstance(payload, Mapping) or payload.get("schema") != EVIDENCE_SCHEMA:
        raise ValueError("traversal_gimmick_evidence_invalid")
    gimmicks = payload.get("gimmicks")
    if not isinstance(gimmicks, list):
        raise ValueError("traversal_gimmick_evidence_invalid")
    result: list[TraversalGimmick] = []
    for item in gimmicks:
        if not isinstance(item, Mapping) or item.get("kind") not in GIMMICK_KINDS:
            continue
        gimmick_id = item.get("id")
        first, second = _vec(item.get("first")), _vec(item.get("second"))
        if not isinstance(gimmick_id, str) or not gimmick_id or first is None or second is None:
            continue
        yaw = item.get("yaw_degrees")
        observed = item.get("observed_landings")
        landings = {}
        for direction, value in (observed.items() if isinstance(observed, Mapping) else ()):
            point = _vec(value)
            if direction in (DIRECTION_FORWARD, DIRECTION_REVERSE) and point is not None:
                landings[direction] = point
        result.append(TraversalGimmick(
            gimmick_id=gimmick_id, kind=str(item["kind"]), first=first, second=second,
            position=_vec(item.get("position")),
            yaw_degrees=float(yaw) if isinstance(yaw, (int, float)) and math.isfinite(yaw) else None,
            observed_landings=landings,
        ))
    return result


def record_scan(path: str | Path, rows: Iterable[Any], *, map_id: str | None = None) -> dict[str, Any]:
    """Merge one scan into the evidence file, by gimmick id; atomic.

    Gimmicks are static level actors, so a later scan of the same id
    replaces the earlier record and scans of other areas accumulate.
    """
    destination = Path(path)
    scanned = gimmicks_from_scan(rows)
    known = {gimmick.gimmick_id: gimmick for gimmick in load_evidence(destination)}
    for gimmick in scanned:
        previous = known.get(gimmick.gimmick_id)
        # A rescan of the same actor keeps what live crossings observed,
        # unless the actor itself has moved.
        if previous is not None and (previous.first, previous.second) == (gimmick.first, gimmick.second):
            gimmick = replace(gimmick, observed_landings=previous.observed_landings)
        known[gimmick.gimmick_id] = gimmick
    _write_evidence(destination, known, map_id=map_id)
    return {"path": str(destination), "scanned": len(scanned), "recorded": min(len(known), MAX_EVIDENCE_GIMMICKS)}


def record_observed_landing(path: str | Path, gimmick_id: str, direction: str, landing: Vec3) -> dict[str, Any]:
    """Keep where a live crossing of ``gimmick_id`` in ``direction`` landed."""
    if direction not in (DIRECTION_FORWARD, DIRECTION_REVERSE):
        raise ValueError("traversal_gimmick_direction_invalid")
    destination = Path(path)
    known = {gimmick.gimmick_id: gimmick for gimmick in load_evidence(destination)}
    gimmick = known.get(gimmick_id)
    if gimmick is None:
        raise ValueError("traversal_gimmick_not_in_evidence")
    known[gimmick_id] = replace(gimmick, observed_landings={**gimmick.observed_landings, direction: landing})
    _write_evidence(destination, known, map_id=None)
    return {"path": str(destination), "gimmick_id": gimmick_id, "direction": direction, "landing": _point(landing)}


def _write_evidence(destination: Path, known: Mapping[str, TraversalGimmick], *, map_id: str | None) -> None:
    ordered = [known[key] for key in sorted(known)][:MAX_EVIDENCE_GIMMICKS]
    payload = {
        "schema": EVIDENCE_SCHEMA,
        "updated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "last_map_id": map_id,
        "gimmicks": [gimmick.to_dict() for gimmick in ordered],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def lead_in(gimmick: TraversalGimmick, direction: str) -> Vec3:
    """The point ``LEAD_IN_CM`` in front of the departure trigger, along the
    gimmick (its own two ends, not an observed landing)."""
    departure, beyond = (gimmick.first, gimmick.second) if direction == DIRECTION_FORWARD else (gimmick.second, gimmick.first)
    length = math.hypot(beyond.x - departure.x, beyond.y - departure.y)
    if length < 1.0:
        return departure
    scale = LEAD_IN_CM / length
    return Vec3(departure.x - (beyond.x - departure.x) * scale, departure.y - (beyond.y - departure.y) * scale, departure.z)


def link_reference(gimmick_id: str, direction: str) -> int:
    """A stable reference for a gimmick crossing; negative, so it never
    names a navmesh polygon.  The validator derives the same value."""
    digest = hashlib.sha256(f"{gimmick_id}|{direction}".encode("utf-8")).hexdigest()
    return -(int(digest[:15], 16) + 1)


def _snap(graph: Any, point: Vec3) -> tuple[int, Vec3] | None:
    resolution = graph.resolve_point_if_resolvable(
        point,
        max_snap_distance_m=SNAP_MAX_HORIZONTAL_M,
        max_vertical_separation_m=SNAP_MAX_VERTICAL_M,
    )
    if not resolution.ok or resolution.closest_point is None:
        return None
    return int(resolution.selected_reference), resolution.closest_point


def gimmick_links(graph: Any, gimmicks: Iterable[TraversalGimmick]) -> tuple[list[OffMeshLink], dict[str, Any]]:
    """Two one-way links per gimmick whose ends both lie on ``graph``.

    A gimmick outside the graph's tiles, or with an end the navmesh does not
    reach within the snap bounds, is counted and left out.
    """
    links: list[OffMeshLink] = []
    stats = {"gimmicks": 0, "linked": 0, "end_not_on_mesh": 0, "same_polygon": 0}
    for gimmick in gimmicks:
        stats["gimmicks"] += 1
        linked = False
        for direction in (DIRECTION_FORWARD, DIRECTION_REVERSE):
            departure, landing = gimmick.ends(direction)
            start_snap, end_snap = _snap(graph, lead_in(gimmick, direction)), _snap(graph, landing)
            if start_snap is None:
                start_snap = _snap(graph, departure)
            if start_snap is None or end_snap is None:
                stats["end_not_on_mesh"] += 1
                continue
            (start_ref, start), (end_ref, end) = start_snap, end_snap
            if start_ref == end_ref:
                stats["same_polygon"] += 1
                continue
            linked = True
            links.append(OffMeshLink(
                owner_reference=link_reference(gimmick.gimmick_id, direction),
                start_reference=start_ref,
                end_reference=end_ref,
                start=start,
                end=end,
                flags=CONNECTION_FLAG_DYNAMIC,
                side=0,
                link_class=gimmick.link_class,
                horizontal_length_cm=math.hypot(end.x - start.x, end.y - start.y),
                vertical_delta_cm=end.z - start.z,
                gimmick_id=gimmick.gimmick_id,
                gimmick_direction=direction,
            ))
        stats["linked"] += int(linked)
    return links, stats


def attach_gimmick_links(graph: Any, evidence_file: str | Path | None) -> dict[str, Any]:
    """Set ``graph``'s gimmick links from the evidence file, as read now.

    The graph is memoised between routes, so its gimmick links are replaced
    on every route; ``sha256`` names the exact evidence they came from, which
    the validator must read too.
    """
    if evidence_file is None:
        graph.set_gimmick_links(())
        return {"attached": False, "reason": "no_evidence_file", "sha256": None}
    path = Path(evidence_file)
    gimmicks, sha256 = read_evidence(path)
    links, stats = gimmick_links(graph, gimmicks)
    count = graph.set_gimmick_links(links)
    return {"attached": bool(links), "path": str(path), "sha256": sha256, "links": count, **stats}
