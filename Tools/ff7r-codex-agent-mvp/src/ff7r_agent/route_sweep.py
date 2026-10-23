"""Sweep Cloud's capsule along a route with the engine's collision.

The navmesh omits placed props (live 2026-09-26 a merchant wagon at Kalm
stood on one walkable polygon), so a planned route is only walkable once the
game's own collision agrees. ``route_sweep.lua`` sweeps the capsule segment by
segment and lists every blocker; this module asks it and classifies the hits:

- ``walkable``: a floor, ramp or step face the capsule met (normal mostly up);
- ``character``: a character's capsule (NPC or party member), which moves;
- ``pushable``: a physics-simulated prop Cloud shoves aside (the green café
  chairs at Kalm);
- ``fixed``: anything else, which the route has to go around.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Iterable, Mapping

from .map_query import file_signature
from .navigation_values import MissionError

REQUEST_NAME = "route-sweep.request"
RESPONSE_NAME = "route-sweep.json"
REQUEST_LIFETIME_SECONDS = 60
POLL_SECONDS = 0.1
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_POINTS = 128
# Cloud's capsule (telemetry: 34 cm radius, 88 cm half height). The sweep
# follows the floor and lifts the capsule by less than a full step, so
# anything taller than a kerb counts; a floor rise above MAX_STEP_CM between
# 50 cm steps is a ledge Cloud cannot walk up.
CAPSULE_RADIUS_CM = 34.0
CAPSULE_HALF_HEIGHT_CM = 88.0
STEP_LIFT_CM = 30.0
MAX_STEP_CM = 45.0
MARGINAL_LEDGE_RISE_CM = 60.0
# The navigation probe's walkable threshold (about 35 degrees).
WALKABLE_NORMAL_Z_MIN = 0.82
# Characters collide through their root component (live: Aerith's hit was
# EndCharacterRootComponent) or a plain capsule. Party members (PC0000 Cloud,
# PC0002 Tifa, PC0003 Aerith...) follow Cloud and step aside; other
# characters mostly stand still (live: a crowd round the Golden Plumes wagon
# at Kalm stopped three routes in a row), so they are routed around.
CHARACTER_COMPONENTS = ("EndCharacterRootComponent ", "CapsuleComponent ")
PARTY_ACTOR = re.compile(r"^PC\d{4}_")
# Door gimmicks (live at Nibelheim: BG0007_00_Door2Bar_Standard_C on the
# Town Hall, BG0012/BG0002_00_Door1Knob_Standard_C on a house). A door opens
# from its field prompt, so it is not routed round.
DOOR_ACTOR = re.compile(r"^BG\d{4}_\d{2}_Door\w*_C\b")
# Vehicles collide like characters but are no one to push past: walking into
# one mounts it (live at Costa del Sol: VE0025_02_RentalBike_VariationB, a
# Segway, became Cloud's actor mid-route and could not climb the steps).
VEHICLE_ACTOR = re.compile(r"^VE\d{4}_")
# The only query that sees a vehicle's body (see vehicle_body_hits).
VEHICLE_BODY_QUERY = "objects:7"
# Kinds the planner routes round. Characters are pushable and doors open
# (live at Nibelheim: three townspeople and the Town Hall door used up every
# replan and no route to Turner was left); the follower's stall review
# waits for a character in the way, then routes round one who stays.
ROUTE_BLOCKING_KINDS = frozenset({"fixed", "vehicle", "ledge", "drop"})
# The Visibility channel: at Kalm's café it hit exactly what Cloud's own
# collision profile did, and at the wagon it also saw the crowd and ledges,
# which the profile query missed entirely.
DEFAULT_QUERY = "channel:0"


def classify_hit(hit: Mapping[str, Any]) -> str:
    if (hit.get("floor_profile_confirmed") is True
            and not hit.get("actor") and not hit.get("component")
            and (hit.get("ledge") is True or hit.get("drop") is True)):
        return "walkable"
    if hit.get("ledge"):
        # A rise only just over the step limit is steep stairs the riser
        # re-sampling could not split (live at Costa del Sol: 46 cm over
        # 50 cm on the steps to Dokaccha walled off the only stairway).
        # The floor-to-floor capsule sweep still reports a wall or platform
        # edge there as a contact of its own.
        rise = hit.get("rise_cm")
        if isinstance(rise, (int, float)) and rise <= MARGINAL_LEDGE_RISE_CM:
            return "steep_step"
        return "ledge"
    if hit.get("drop"):
        return "drop"
    normal_z = hit.get("normal_z")
    if isinstance(normal_z, (int, float)) and normal_z >= WALKABLE_NORMAL_Z_MIN:
        return "walkable"
    if VEHICLE_ACTOR.match(str(hit.get("actor") or "")):
        return "vehicle"
    if str(hit.get("component") or "").startswith(CHARACTER_COMPONENTS):
        return "party" if PARTY_ACTOR.match(str(hit.get("actor") or "")) else "npc"
    if DOOR_ACTOR.match(str(hit.get("actor") or "")):
        return "door"
    if hit.get("simulating_physics") is True:
        return "pushable"
    return "fixed"


QUERY_PATTERN = re.compile(r"^(profile|channel:\d+|objects:\d+(,\d+)*)$")


def _points(points: Iterable[Mapping[str, float]]) -> list[tuple[float, float, float]]:
    result = [(float(p["x"]), float(p["y"]), float(p["z"])) for p in points]
    if len(result) < 2:
        raise MissionError("route_sweep_point_count_invalid")
    return result


def request_route_sweep(
    service: Any,
    points: Iterable[Mapping[str, float]],
    *,
    radius_cm: float = CAPSULE_RADIUS_CM,
    half_height_cm: float = CAPSULE_HALF_HEIGHT_CM,
    lift_cm: float = STEP_LIFT_CM,
    query: str = DEFAULT_QUERY,
    max_step_cm: float = MAX_STEP_CM,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Sweep the capsule along ``points`` (feet positions, cm) and classify every hit.

    ``query`` is Cloud's collision profile by default; ``channel:<n>`` and
    ``objects:<n>,...`` are for diagnosing what a sweep does or does not see.
    A route longer than one request (MAX_POINTS) is swept in chunks that
    share their end points; segment indices stay those of the whole route
    (live: a Kalm route from the plaza to Zahira failed its collision audit
    with route_sweep_point_count_invalid).
    """
    route = _points(points)
    if not QUERY_PATTERN.match(query):
        raise MissionError("route_sweep_query_invalid")
    options = dict(radius_cm=radius_cm, half_height_cm=half_height_cm, lift_cm=lift_cm, query=query,
                   max_step_cm=max_step_cm, timeout_seconds=timeout_seconds)
    if len(route) <= MAX_POINTS:
        payload = _sweep_chunk(service, route, **options)
        bindings = [_sweep_binding(payload)]
    else:
        payload = None
        bindings = []
        for start in range(0, len(route) - 1, MAX_POINTS - 1):
            chunk = _sweep_chunk(service, route[start:start + MAX_POINTS], **options)
            bindings.append(_sweep_binding(chunk))
            for segment in chunk.get("segments") or []:
                segment["index"] = int(segment.get("index") or 0) + start
            if payload is None:
                payload = chunk
                payload["chunks"] = 1
            else:
                payload["segments"] = list(payload.get("segments") or []) + list(chunk.get("segments") or [])
                payload["floor_missing"] = int(payload.get("floor_missing") or 0) + int(chunk.get("floor_missing") or 0)
                payload["chunks"] += 1
    payload["chunk_bindings"] = bindings
    if query == "channel:0" and any(
        _synthetic_floor_hit(hit) for segment in payload.get("segments") or []
        for hit in segment.get("hits") or []
    ):
        _confirm_visibility_floor(service, route, payload, **options)
    blockers = []
    for segment in payload.get("segments") or []:
        for hit in segment.get("hits") or []:
            hit["kind"] = classify_hit(hit)
            if hit["kind"] in {"fixed", "vehicle", "npc", "party", "door", "ledge", "drop"}:
                blockers.append({"segment": segment.get("index"), **hit})
    payload["blockers"] = blockers
    payload["clear"] = not blockers and all(segment.get("complete") for segment in payload.get("segments") or [])
    return payload


def _synthetic_floor_hit(hit: Mapping[str, Any]) -> bool:
    return (not hit.get("actor") and not hit.get("component")
            and (hit.get("ledge") is True or hit.get("drop") is True))


def _sweep_binding(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: payload.get(key) for key in ("map_id", "capsule", "query", "source_sequence", "request_id")}


def _confirm_visibility_floor(service: Any, route: list[tuple[float, float, float]],
                              payload: dict[str, Any], **options: Any) -> None:
    """Corroborate synthetic Visibility floor jumps with Cloud's own profile.

    Visibility sees gaps between Under Junon's stair treads where Cloud's
    collision profile provides continuous support. Keep every actor contact
    from Visibility; only its synthetic floor reports can be confirmed, by
    a complete, clear profile sweep of the identical capsule and whole route.
    An unavailable or ambiguous confirmation leaves the original blockers.
    """
    options = {**options, "query": "profile"}
    try:
        profile = request_route_sweep(service, [dict(zip(("x", "y", "z"), p)) for p in route], **options)
    except MissionError as exc:
        payload["floor_profile_confirmation"] = {"verified": False, "error": str(exc)}
        return
    primary_segments, profile_segments = payload.get("segments") or [], profile.get("segments") or []
    primary_bindings = payload.get("chunk_bindings") or []
    profile_bindings = profile.get("chunk_bindings") or []
    verified = (
        profile.get("clear") is True
        and type(profile.get("floor_missing")) is int and profile["floor_missing"] == 0
        and bool((profile.get("query") or {}).get("profile"))
        and bool(payload.get("map_id")) and profile.get("map_id") == payload["map_id"]
        and payload.get("capsule") == {key: options[key] for key in ("radius_cm", "half_height_cm", "lift_cm")}
        and profile.get("capsule") == payload["capsule"]
        and type(payload.get("source_sequence")) is int
        and type(profile.get("source_sequence")) is int
        and profile["source_sequence"] >= payload["source_sequence"]
        and len(primary_bindings) == len(profile_bindings)
        and all(binding.get("map_id") == payload["map_id"]
                and binding.get("capsule") == payload["capsule"]
                and type(binding.get("source_sequence")) is int
                for binding in primary_bindings + profile_bindings)
        and all(bool((binding.get("query") or {}).get("profile"))
                and binding["source_sequence"] >= max(item["source_sequence"] for item in primary_bindings)
                for binding in profile_bindings)
        and len(primary_segments) == len(profile_segments) == len(route) - 1
        and all(s.get("complete") is True and s.get("index") == i
                for segments in (primary_segments, profile_segments) for i, s in enumerate(segments))
    )
    payload["floor_profile_confirmation"] = {
        "verified": verified, "request_id": profile.get("request_id"),
        "source_sequence": profile.get("source_sequence"), "query": profile.get("query"),
        "floor_missing": profile.get("floor_missing"), "clear": profile.get("clear"),
        "chunk_bindings": profile_bindings,
    }
    if verified:
        for segment in primary_segments:
            for hit in segment.get("hits") or []:
                if _synthetic_floor_hit(hit):
                    hit["floor_profile_confirmed"] = True


def _sweep_chunk(service: Any, route: list[tuple[float, float, float]], *, radius_cm: float,
                 half_height_cm: float, lift_cm: float, query: str, max_step_cm: float,
                 timeout_seconds: float) -> dict[str, Any]:
    """One sampler request for at most MAX_POINTS points."""
    path = getattr(getattr(service, "telemetry", None), "path", None)
    if path is None:
        raise MissionError("route_sweep_live_sampler_required")
    directory = Path(path).parent
    request_path, response_path = directory / REQUEST_NAME, directory / RESPONSE_NAME
    request_id = uuid.uuid4().hex
    lines = [f"{request_id}|{int(time.time()) + REQUEST_LIFETIME_SECONDS}|{radius_cm:g}|{half_height_cm:g}|{lift_cm:g}|{query}|{max_step_cm:g}"]
    lines += [f"{x:.1f},{y:.1f},{z:.1f}" for x, y, z in route]
    lock = getattr(service, "_lock", None)
    # Poll by stat; see map_query.file_signature.
    seen = file_signature(response_path)
    with lock if lock is not None else nullcontext():
        temporary = request_path.with_name(REQUEST_NAME + "." + uuid.uuid4().hex)
        try:
            temporary.write_text("\n".join(lines) + "\n", encoding="ascii")
            os.replace(temporary, request_path)
        finally:
            temporary.unlink(missing_ok=True)
    sleeper = getattr(service, "_sleeper", time.sleep)
    deadline = time.monotonic() + timeout_seconds
    payload = None
    try:
        while True:
            signature = file_signature(response_path)
            candidate = None
            if signature is not None and signature != seen:
                seen = signature
                try:
                    candidate = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    candidate = None
            if isinstance(candidate, dict) and candidate.get("request_id") == request_id:
                payload = candidate
                break
            if time.monotonic() >= deadline:
                raise MissionError("route_sweep_timeout")
            sleeper(POLL_SECONDS)
    finally:
        with lock if lock is not None else nullcontext():
            try:
                if request_path.read_text(encoding="ascii").split("|")[0] == request_id:
                    request_path.unlink()
            except (OSError, UnicodeError):
                pass
    if payload.get("source") != "ue4ss_route_sweep" or payload.get("status") != "ok":
        raise MissionError(f"route_sweep_failed:{payload.get('error') or payload.get('status')}")
    return payload


# Level collision merged into per-material grid actors (Spl_BLA_WodPrt_..._
# NavDefaultAfT_Grid_...) is already cut out of the navmesh: live at Kalm a
# point 20 cm inside every such hit was off the mesh. The obstacles the
# navmesh misses are individual actors (the café's BG2051 table sets, Ned's
# Queen's Blood board), whose bounds are real.
BAKED_COLLISION = re.compile(r"_Nav[A-Za-z]+_Grid_\d+$")
# A level's collision asset is level collision too (live at Costa del Sol:
# the steps up to Dokaccha's terrace, static CollisionAssetActor hits the
# capsule started inside, walled off the only stairway).
LEVEL_COLLISION_ASSET = re.compile(r"^CollisionAssetActor( |$)")
STATIC_MOBILITY = 0
GROWTH_M = 0.25
MAX_GROWN_RADIUS_M = 4.0
# Bounds are measured, not guessed from getting stuck, so the padding is
# Cloud's radius (0.34 m) and a little, not the overlay's defaults (0.65 m
# clearance + 0.25 m uncertainty) which put Ned's talk spot out of reach.
SWEEP_CLOUD_CLEARANCE_M = 0.45
SWEEP_UNCERTAINTY_MARGIN_M = 0.10


POINT_OBSTACLE_RADIUS_M = 0.2
POINT_KEY_GRID_CM = 25.0
# An actor wider than a table set is routed round where it was touched, not
# as its whole box (live 2026-10-06 at Cosmo Canyon: a village tent,
# BG1216_00_Tent_CSMOA_01, has a 5.1 x 5.2 m box; as a 3.1 m footprint it
# sealed the walkway beside it, and every route to Robin and Ka'dina failed
# global_replan_unavailable). A spot hit again is grown, and every re-plan
# is swept again, so a solid large actor is still routed round.
MAX_BOX_FOOTPRINT_RADIUS_M = 2.0


def _has_bounds(hit: Mapping[str, Any]) -> bool:
    extent = (hit.get("actor_bounds") or {}).get("extent") or {}
    return (bool(hit.get("actor")) and not BAKED_COLLISION.search(str(hit.get("actor")))
            and float(extent.get("x") or 0) > 0 and float(extent.get("y") or 0) > 0)


def _box_footprint(hit: Mapping[str, Any]) -> bool:
    """The hit stands for its actor's box: an actor with bounds no wider than
    MAX_BOX_FOOTPRINT_RADIUS_M across its narrow side, and not a character."""
    if not _has_bounds(hit) or classify_hit(hit) == "npc":
        return False
    extent = hit["actor_bounds"]["extent"]
    return min(float(extent["x"]), float(extent["y"])) / 100.0 <= MAX_BOX_FOOTPRINT_RADIUS_M


def obstacle_key(hit: Mapping[str, Any]) -> str | None:
    """What a blocking hit is: an actor with bounds, or a spot (level
    collision, a ledge, a large actor), so a spot hit again is recognised."""
    if _box_footprint(hit):
        return "actor:" + str(hit["actor"])
    impact = hit.get("impact") or {}
    try:
        x, y = float(impact["x"]), float(impact["y"])
    except (KeyError, TypeError, ValueError):
        return None
    return f"spot:{round(x / POINT_KEY_GRID_CM)}:{round(y / POINT_KEY_GRID_CM)}"


def vehicle_body_hits(sweep: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Where a vehicle's body touches the route, as spots to route round.

    A vehicle's collision capsule is a fraction of it (live at Kalm: 48 cm
    on the Golden Plumes wagon, VE0027_01_ChocoboWagon_Burdens, whose
    actor spans 6 x 7 m); its body and shafts answer only object type 7
    (VEHICLE_BODY_QUERY), and Cloud stuck between them and a tree on routes
    the default sweep called clear. Spots, not the actor's box: the box
    takes in the reins and a standing chocobo, and swallowed Cloud himself.
    Unlike other hits, a body touching where Cloud stands counts: pinned
    against the shafts, every route out through them was audited clear.
    """
    result = []
    for segment in sweep.get("segments") or []:
        for hit in segment.get("hits") or []:
            if classify_hit(hit) != "vehicle":
                continue
            spot = {key: value for key, value in hit.items() if key != "actor_bounds"}
            if obstacle_key(spot) is not None:
                result.append({"segment": segment.get("index"), **spot})
    return result


def unknown_obstacle_hits(sweep: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Blocking hits the planner must route around.

    With the capsule on the floor, a fixed hit is something Cloud's body
    touches: an individual actor the navmesh omits (bounds), level collision
    the route clips, or a ledge. Characters are pushed past, doors opened,
    and walkable surfaces and pushable props are not blockers.
    """
    result = []
    for segment in sweep.get("segments") or []:
        for hit in segment.get("hits") or []:
            if _navmesh_vouches_for(hit) or _touching_at_start(segment, hit):
                continue
            if classify_hit(hit) in ROUTE_BLOCKING_KINDS and obstacle_key(hit) is not None:
                result.append({"segment": segment.get("index"), **hit})
    return result


START_CONTACT_CM = 40.0


def _touching_at_start(segment: Mapping[str, Any], hit: Mapping[str, Any]) -> bool:
    """Contact where Cloud already stands, not something to route round.

    Live: starting at Ned's talk spot, 11 cm from his card table, every audit
    round grew the table, the planner shrank it off Cloud's start again, and
    the route to Nene failed route_collision_audit_unresolved. A ledge or
    drop there is still a blocker.
    """
    if segment.get("index") != 0 or hit.get("ledge") or hit.get("drop"):
        return False
    distance = hit.get("distance_cm")
    return isinstance(distance, (int, float)) and 0.0 <= float(distance) < START_CONTACT_CM


def _navmesh_vouches_for(hit: Mapping[str, Any]) -> bool:
    """A capsule that starts inside the level's own collision, not a blocker.

    The navmesh is built from the merged level collision, so a route on it
    already clears that geometry; the lifted capsule can still start inside
    it on stairs and slopes (live at Kalm: the steps up to Nene, three
    start-penetrating hits with no normal that walled the stairs off).
    """
    actor = str(hit.get("actor") or "")
    return (hit.get("start_penetrating") is True
            and (bool(BAKED_COLLISION.search(actor))
                 or (bool(LEVEL_COLLISION_ASSET.match(actor)) and hit.get("mobility") == STATIC_MOBILITY)))


def obstacle_for_hit(hit: Mapping[str, Any], *, sequence: int, t: float, radius_m: float | None = None):
    """The overlay obstacle for a blocking hit: its actor's bounds, or a spot."""
    if _box_footprint(hit):
        return obstacle_from_bounds(str(hit["actor"]), hit["actor_bounds"], sequence=sequence, t=t,
                                    radius_m=radius_m)
    from .models import Vec3
    from .static_obstacles import StaticObstacle

    impact = hit["impact"]
    key = obstacle_key(hit) or ""
    return StaticObstacle(
        obstacle_id="sweep-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
        center=Vec3(float(impact["x"]), float(impact["y"]), float(impact.get("z") or 0.0)),
        radius_m=radius_m if radius_m is not None else POINT_OBSTACLE_RADIUS_M,
        cloud_clearance_m=SWEEP_CLOUD_CLEARANCE_M,
        uncertainty_margin_m=SWEEP_UNCERTAINTY_MARGIN_M,
        confidence=0.9,
        evidence_count=1,
        first_sequence=sequence,
        last_sequence=sequence,
        first_t=t,
        last_t=t,
        provenance=("route_sweep_contact",),
    )


def obstacle_from_bounds(actor: str, bounds: Mapping[str, Any], *, sequence: int, t: float,
                         radius_m: float | None = None):
    """A capsule footprint along the long axis of an actor's bounding box."""
    from .models import Vec3
    from .static_obstacles import StaticObstacle

    origin, extent = bounds["origin"], bounds["extent"]
    ex, ey = float(extent["x"]) / 100.0, float(extent["y"]) / 100.0
    along_x = ex >= ey
    radius = radius_m if radius_m is not None else min(ex, ey)
    return StaticObstacle(
        obstacle_id="sweep-" + hashlib.sha256(actor.encode("utf-8")).hexdigest()[:16],
        center=Vec3(float(origin["x"]), float(origin["y"]), float(origin["z"])),
        radius_m=max(0.1, radius),
        cloud_clearance_m=SWEEP_CLOUD_CLEARANCE_M,
        uncertainty_margin_m=SWEEP_UNCERTAINTY_MARGIN_M,
        shape="capsule" if abs(ex - ey) > 0.01 else "circle",
        half_length_m=abs(ex - ey),
        heading_degrees=0.0 if along_x else 90.0,
        confidence=0.9,
        evidence_count=1,
        first_sequence=sequence,
        last_sequence=sequence,
        first_t=t,
        last_t=t,
        provenance=("route_sweep_actor_bounds",),
    )
