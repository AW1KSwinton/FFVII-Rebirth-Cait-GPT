"""Walk to a named NPC until the game offers Talk, timing each phase.

1. resolve: the NPC's live actor position (``resolve_world_target``);
2. approach: plan Cloud to the NPC on the library capture covering both, and
   take the point ``--stop-distance`` metres short of the NPC along that
   route, so the goal is on the walkable path rather than in the NPC or
   among the furniture around them (live at Ned's table a goal 1.2 m short
   on the direct line ended in chairs);
   an NPC beyond every capture with Cloud is walked toward in staging legs
   first (see staging_point);
3. route: the route follower (``NavigationMission``) with camera-follow;
4. face: aim the camera at the NPC and tap forward until the field prompt
   reads Talk. Live, arriving 3 m from Ned showed no prompt until Cloud
   faced him.

Nothing is interacted with: the run ends with the Talk prompt offered.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .conversation_state import FIELD_BLOCKERS, field_blocker, has_player_position
from .guide_moogle import GOLD_SAUCER_FIELD, GUIDE_MOOGLE, GUIDE_MOOGLE_REACH_M, worth_guide_moogle
from .guide_moogle import zone as guide_moogle_zone
from .navigation_mission import (
    MissionConfig, NavigationMission, VerifiedIdentityCache, canonical_identity_check, feature_identity_check,
    input_recovery_refused,
    unwrap,
)
from .navigation_values import MissionError, finite_position

TALK_KEY = "$level_action_talk"
DEFAULT_STOP_DISTANCE_M = 2.0
# The bridge plans on a graph its prewarm is still building for up to this
# long; past it the approach plans on its own copy (the bridge's requests
# time out at 25 s).
PREWARMED_GRAPH_WAIT_SECONDS = 12.0
APPROACH_GRAPH_STILL_BUILDING = "approach_graph_still_building"
NPC_RESOLVE_VERTICAL_M = 2.0
# Cloud's start is resolved as his routes resolve it (the route adapter's
# start snap): near his feet. Unbounded, it took a polygon 3 m below him
# (live 2026-09-30 by Nene in Kalm, a six-polygon island) and no approach
# could be planned though his routes started fine.
START_RESOLVE_SNAP_M = 0.75
START_RESOLVE_VERTICAL_M = 1.0
# An NPC off the navmesh behind a counter is talked to from in front of it:
# live 2026-10-05 the nearest reachable floor to the Gold Saucer's card
# tournament organiser was 3.4 m behind her counter. The level data gives a
# catalogued NPC's facing, and for most opponents the point where the game
# stands Cloud to start a match, 1.7-2.9 m along that facing. A spot in
# front must be on the floor itself, never snapped from the counter top.
FRONT_DISTANCES_M = (2.2, 1.6, 3.0)
FRONT_MIN_STAND_M = 0.5
FRONT_RESOLVE_SNAP_M = 0.3
# The route stops at the edge of the NPC's surroundings (a seated NPC's
# table set, chairs included, is an obstacle), so the face phase may close
# a few metres: longer taps while far, short ones near.
TALK_RANGE_M = 5.0
MAX_FACE_TAPS = 8
FACE_CAMERA_TOLERANCE_DEG = 8.0
FACE_CAMERA_STEPS = 4
TAP_MS = 200
FAR_TAP_MS = 350
NEAR_M = 2.5
TAP_SETTLE_SECONDS = 1.0
# A Talk prompt at the edge of its range can show for a moment and go (live
# 2026-09-30 at Kalm's item-shop counter: offered at 2.83 m, gone at
# 2.97 m); it counts once it is still offered after this pause.
TALK_CONFIRM_SECONDS = 0.5
MAX_ROUTE_ATTEMPTS = 3
MAX_TERRAIN_ACTIONS = 2
# "Terrain Action" in the field prompt; the key is Space, bound as dodge.
PARKOUR_PROMPT = "$level_sys_show_parkour_input"  # field_prompt.TERRAIN_ACTION_KEY
TERRAIN_ACTION_MS = 150
TERRAIN_ACTION_SETTLE_SECONDS = 2.5
# A route stopped at a door ("Y Open") opens it and routes on; the opening
# walks Cloud through in 5-9 s (live at Nibelheim's Town Hall).
MAX_DOORS = 4
DOOR_OBSERVE_SECONDS = 10.0  # the cap; open_door returns once Cloud is through
# Facing a door from its front point: short half-stick taps toward it.
MAX_DOOR_TAPS = 6
DOOR_REACH_M = 3.0
DOOR_TAP_MAGNITUDE = 0.5
DOOR_TAP_SETTLE_SECONDS = 0.4
# Onto the navmesh beyond a door once its scene is over.
STEP_ONTO_TAPS = 3
STEP_ONTO_REACHED_M = 0.4
STEP_ONTO_TAP_MS = 250
# Through an open doorway from its front point (about 1.8 m across).
STEP_THROUGH_TAPS = 8
STEP_THROUGH_REACHED_M = 0.6
CAPSULE_HALF_HEIGHT_CM = 88.0
KNOWN_SPOT_REACHED_M = 0.75
# Collect completed NPC routes every quarter second; each status response
# is about 1 KB. Standalone missions retain their 2 s default cadence.
APPROACH_POLL_SECONDS = 0.25
# A Guide Moogle ride: the fade and load took under 5 s live (2026-10-04),
# and Cloud lands on the zone's arrival point.
GUIDE_MOOGLE_ARRIVAL_SECONDS = 45.0
GUIDE_MOOGLE_ARRIVAL_M = 5.0

# A capture covers only the ~386 m square of navmesh the game has built
# round Cloud, so an NPC much over half that away is in no capture with him.
# Captures are not stitched together: Cloud walks staging legs instead, each
# to the point of his capture nearest the NPC, where the game builds the
# navmesh on toward them, and plans again from there.
NO_CAPTURE_COVERS_ROUTE = "npc_approach_no_capture_covers_route"
NO_CAPTURE_COVERS_START = "npc_approach_no_capture_covers_start"
MAX_STAGING_LEGS = 4
MIN_STAGING_PROGRESS_M = 20.0

Rpc = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _horizontal_m(first: dict[str, float], second: dict[str, float]) -> float:
    return math.hypot(first["x"] - second["x"], first["y"] - second["y"]) / 100.0


def _point(position: dict[str, float]) -> tuple[float, ...]:
    """(x, y, z) for capture selection, or (x, y) when the height is unknown."""
    z = position.get("z")
    return (position["x"], position["y"]) if z is None else (position["x"], position["y"], z)


def capture_covers(start: dict[str, float], point: dict[str, float], cache_manifest: Path | None = None) -> bool:
    """Whether a library capture covers both Cloud and ``point``; reads manifests only, builds no graph.

    True without a library, where routes use the configured capture.
    """
    from .live_route_adapter import (DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH, LIVE_EXECUTABLE_SHA256,
                                     current_validator_source_sha256)
    from .topology_cache import select_library_manifest

    manifest_path = Path(cache_manifest or DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH)
    selected, decision = select_library_manifest(
        manifest_path.parent, world_id="EndGame", map_id="EndGame", executable_sha256=LIVE_EXECUTABLE_SHA256,
        position=_point(start), target=_point(point), validator_source_sha256=current_validator_source_sha256())
    if selected is None:
        return decision.get("reason") == "topology_library_absent"
    return bool(decision.get("covers_target"))


def load_covering_graph(start: dict[str, float], target: dict[str, float], executable_sha256: str,
                        cache_manifest: Path | None = None, known_graphs: dict[str, Any] | None = None,
                        require_target: bool = True, build_missing: bool = True):
    """The library capture covering Cloud and the NPC, as a planning graph.

    ``known_graphs`` maps raw artifact SHA-256 to graphs already built (the
    bridge's validated topology cache); building one takes ~15 s. Without
    ``require_target`` a capture covering Cloud alone will do (a staging leg).
    Without ``build_missing`` a capture not already built is refused.
    """
    from .detour_route import DetourTopologyGraph
    from .live_route_adapter import DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH, current_validator_source_sha256
    from .topology_cache import resolve_manifest_artifact, select_library_manifest

    manifest_path = Path(cache_manifest or DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH)
    # The capture a route will admit: one the current validator admitted.
    selected, decision = select_library_manifest(
        manifest_path.parent, world_id="EndGame", map_id="EndGame", executable_sha256=executable_sha256,
        position=_point(start), target=_point(target), validator_source_sha256=current_validator_source_sha256())
    if selected is None:
        raise MissionError(NO_CAPTURE_COVERS_START if not require_target else NO_CAPTURE_COVERS_ROUTE)
    if require_target and not decision.get("covers_target"):
        raise MissionError(NO_CAPTURE_COVERS_ROUTE)
    manifest = json.loads(selected.read_text(encoding="utf-8"))
    raw_sha = str(manifest.get("raw_artifact_sha256") or "").upper()
    known = (known_graphs or {}).get(raw_sha)
    if known is not None:
        return known, {**decision, "graph_reused": True}
    if not build_missing:
        raise MissionError(APPROACH_GRAPH_STILL_BUILDING)
    payload = json.loads(resolve_manifest_artifact(selected, manifest).read_text(encoding="utf-8"))
    graph = DetourTopologyGraph.from_dict(payload)
    if known_graphs is not None and raw_sha:
        known_graphs[raw_sha] = graph
    return graph, decision


def plan_approach_in_process(service: Any, start: dict[str, float], target: dict[str, float],
                             stop_distance_m: float = DEFAULT_STOP_DISTANCE_M, stage: bool = False,
                             front: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The approach point, planned on graphs the bridge process already holds.

    With ``stage``, the staging leg toward an NPC no capture covers with
    Cloud instead (see staging_point). Read-only: it plans on the navmesh
    and sends no input.
    """
    from .live_route_adapter import LIVE_EXECUTABLE_SHA256

    known: dict[str, Any] = getattr(service, "_approach_graphs", None) or {}
    resources = getattr(service, "_validated_navigation_resources", None)
    provider = resources[1] if isinstance(resources, tuple) and len(resources) > 1 else None
    cached = getattr(provider, "_cached_state", None)
    if isinstance(cached, dict) and cached.get("graph") is not None and cached.get("raw_sha256"):
        known.setdefault(str(cached["raw_sha256"]).upper(), cached["graph"])
    # The approach's prewarm_navigation has just started building the capture
    # here: wait for it rather than build the same graph alongside it.
    prewarmed = getattr(provider, "prewarmed_graphs", None)
    for raw_sha, graph in (prewarmed(PREWARMED_GRAPH_WAIT_SECONDS) if callable(prewarmed) else {}).items():
        known.setdefault(str(raw_sha).upper(), graph)
    # Still building: a second build here would race it for the worker and
    # could outlast the bridge's request timeout, so the caller plans on its
    # own copy instead.
    pending = getattr(provider, "graph_prewarm_pending", None)
    graph, library = load_covering_graph(start, target, LIVE_EXECUTABLE_SHA256, known_graphs=known,
                                         require_target=not stage,
                                         build_missing=not (callable(pending) and pending()))
    # Keep only the latest few graphs; each holds a whole capture.
    service._approach_graphs = dict(list(known.items())[-2:])
    # The scanned ladders, cracks and crawl spaces join islands for the
    # approach as for the route (gimmick_links.py).
    from .explore_mode import _attach_gimmicks
    _attach_gimmicks(service, graph)
    if stage:
        return {"library": library, "staging": staging_point(graph, start, target)}
    try:
        return {"library": library, "approach": approach_point(graph, start, target, stop_distance_m, front)}
    except MissionError as exc:
        if "no_linked_polygon_corridor" not in str(exc):
            raise
        from .door_links import default_door_links
        doors = plan_door_chain(graph, service.sweep_route, start, target, links=default_door_links(),
                                topology=str((library or {}).get("selected") or ""))
        if not doors:
            raise
        return {"library": library, "doors": doors,
                "approach": approach_point(graph, doors[-1]["beyond"], target, stop_distance_m, front)}


def plan_door_chain(graph: Any, sweep: Any, start: dict[str, float], target: dict[str, float], *,
                    links: Any = None, topology: str = "") -> list[dict[str, Any]]:
    """Doors between Cloud's navmesh island and the NPC's, each leg plannable.

    Doors found are kept in ``links`` (a DoorLinks store) under the capture's
    ``topology`` fingerprint, so they are known later whichever way Cloud
    goes and whether they are shut or have been left open.
    """
    from .door_links import find_door_chain
    from .models import Vec3

    begin = graph.resolve_point(Vec3(start["x"], start["y"], start["z"]), max_vertical_separation_m=1.0)
    end = graph.resolve_point(Vec3(target["x"], target["y"], target["z"]),
                              max_vertical_separation_m=NPC_RESOLVE_VERTICAL_M)
    if not (begin.ok and end.ok):
        return []
    remember = links is not None and topology
    doors = find_door_chain(graph, sweep, Vec3(start["x"], start["y"], start["z"]),
                            end.selected_reference, begin.selected_reference,
                            known=links.for_topology(topology) if remember else (),
                            record=(lambda link: links.record(topology, link)) if remember else None)
    legs = [(start, door["front"]) for door in doors[:1]] + [
        (first["beyond"], second["front"]) for first, second in zip(doors, doors[1:])]
    for leg_start, leg_end in legs:
        plan = graph.plan(Vec3(leg_start["x"], leg_start["y"], leg_start["z"]),
                          Vec3(leg_end["x"], leg_end["y"], leg_end["z"]), max_vertical_separation_m=1.0)
        if plan.status != "complete":
            return []
    return doors


def staging_point(graph: Any, start: dict[str, float], target: dict[str, float]) -> dict[str, Any] | None:
    """The end of a staging leg: the point Cloud can reach nearest the NPC.

    For an NPC beyond Cloud's capture. None when it brings him less than
    MIN_STAGING_PROGRESS_M nearer, or no route there plans.
    """
    from .models import Vec3

    origin = Vec3(start["x"], start["y"], start["z"])
    feet = graph.resolve_point(origin, max_snap_distance_m=START_RESOLVE_SNAP_M,
                               max_vertical_separation_m=START_RESOLVE_VERTICAL_M)
    if not feet.ok:
        return None
    nearest = graph.nearest_reachable_point(origin, Vec3(target["x"], target["y"], target["z"]),
                                            max_horizontal_m=math.inf, max_vertical_m=math.inf,
                                            start_resolution=feet)
    if nearest is None:
        return None
    point = {"x": nearest.x, "y": nearest.y, "z": nearest.z}
    remaining_m = _horizontal_m(point, target)
    progress_m = _horizontal_m(start, target) - remaining_m
    if progress_m < MIN_STAGING_PROGRESS_M:
        return None
    plan = graph.plan(origin, nearest, start_resolution_override=feet)
    if plan.status != "complete":
        return None
    return {"position": point, "remaining_m": round(remaining_m, 1), "progress_m": round(progress_m, 1),
            "route_length_m": plan.total_estimated_path_length_m}


def approach_point(graph: Any, start: dict[str, float], target: dict[str, float],
                   stop_distance_m: float, front: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The point ``stop_distance_m`` short of the target along the planned route.

    With known facing, use the first reachable front spot even when the
    NPC's own floor is linked. Never fall back behind them. Without facing,
    retain the route-shortening and nearest-reachable-floor behavior.
    """
    from .models import Vec3

    end = Vec3(target["x"], target["y"], target["z"])
    # The resolver prefers a polygon whose outline contains the point at any
    # height up to 20 m (live: Turner in Nibelheim resolved onto a roof 5.8 m
    # overhead, an island of its own, while the street ran 0.8 m away at his
    # feet). The NPC's feet height is known, so look near it first.
    near = graph.resolve_point(end, max_vertical_separation_m=NPC_RESOLVE_VERTICAL_M)
    origin = Vec3(start["x"], start["y"], start["z"])
    feet = graph.resolve_point(origin, max_snap_distance_m=START_RESOLVE_SNAP_M,
                               max_vertical_separation_m=START_RESOLVE_VERTICAL_M)
    feet = feet if feet.ok else None
    plan = None if front else graph.plan(origin, end, start_resolution_override=feet,
                                        end_resolution_override=near if near.ok else None)
    adjusted_m = None
    front_of_npc = None
    if front:
        for spot in front or ():
            point = Vec3(float(spot["x"]), float(spot["y"]), float(spot["z"]))
            floor = graph.resolve_point(point, max_snap_distance_m=FRONT_RESOLVE_SNAP_M,
                                        max_vertical_separation_m=NPC_RESOLVE_VERTICAL_M)
            if not floor.ok:
                continue
            routed = graph.plan(origin, point, start_resolution_override=feet, end_resolution_override=floor)
            if routed.status == "complete":
                plan, front_of_npc, stop_distance_m = routed, spot.get("source"), 0.0
                adjusted_m = round(math.hypot(point.x - end.x, point.y - end.y) / 100.0, 2)
                break
        if plan is None:
            raise MissionError("npc_approach_unplannable:no_linked_polygon_corridor:npc_front_unreachable")
    if plan.status != "complete" and plan.reason_code == "no_linked_polygon_corridor":
        # The NPC's own floor is not linked to Cloud's: walk to the nearest
        # floor that is, and talk from there.
        reachable = graph.nearest_reachable_point(origin, end, start_resolution=feet)
        if reachable is not None:
            plan = graph.plan(origin, reachable, start_resolution_override=feet)
            adjusted_m = round(math.hypot(reachable.x - end.x, reachable.y - end.y) / 100.0, 2)
            stop_distance_m = 0.0
    if plan.status != "complete":
        raise MissionError(f"npc_approach_unplannable:{plan.reason_code}")
    points = [(p.x, p.y, p.z) for p in plan.waypoints]
    remaining = stop_distance_m * 100.0
    for index in range(len(points) - 1, 0, -1):
        (ax, ay, az), (bx, by, bz) = points[index - 1], points[index]
        length = math.hypot(bx - ax, by - ay)
        if length >= remaining and length > 0:
            t = (length - remaining) / length
            point = {"x": ax + (bx - ax) * t, "y": ay + (by - ay) * t, "z": az + (bz - az) * t}
            return {"position": point, "route_length_m": plan.total_estimated_path_length_m,
                    "waypoints": len(points), "adjusted_to_reachable_m": adjusted_m,
                    **({"front_of_npc": front_of_npc} if front_of_npc else {})}
        remaining -= length
    if adjusted_m is not None:
        # Go all the way to the nearest reachable point, or the spot in front.
        return {"position": {"x": points[-1][0], "y": points[-1][1], "z": points[-1][2]},
                "route_length_m": plan.total_estimated_path_length_m, "waypoints": len(points),
                "adjusted_to_reachable_m": adjusted_m,
                **({"front_of_npc": front_of_npc} if front_of_npc else {})}
    return {"position": {"x": points[0][0], "y": points[0][1], "z": points[0][2]},
            "route_length_m": plan.total_estimated_path_length_m, "waypoints": len(points),
            "already_within_stop_distance": True}


# The service's minimum gap between movement calls.
MOVEMENT_GAP_SECONDS = 0.5
MOVEMENT_GAP_RETRIES = 3


async def move_stick(rpc: Rpc, arguments: dict[str, Any], sleep=asyncio.sleep) -> dict[str, Any]:
    """One stick tap, waiting out the service's minimum gap after the last
    input instead of failing on it. Live 2026-10-06 at Bernard: the turn to
    face him came straight after the camera alignment, was refused, and the
    approach fast-travelled away with Cloud standing beside him."""
    for attempt in range(MOVEMENT_GAP_RETRIES + 1):
        try:
            return unwrap(await rpc("move_stick", arguments))
        except MissionError as exc:
            if "minimum delay between movement calls" not in str(exc) or attempt == MOVEMENT_GAP_RETRIES:
                raise
            await sleep(MOVEMENT_GAP_SECONDS)
    raise MissionError("unreachable")


# Talk can be offered with Cloud's body turned away (live at Michaela's table,
# 1.4 m from her), while starting a match needs him facing her within 45
# degrees (#171). Gentle taps turn him without walking him into the table.
FACE_BODY_TOLERANCE_DEG = 40.0
FACE_BODY_MAX_TAPS = 3
FACE_BODY_TAP_MS = 120
FACE_BODY_MAGNITUDE = 0.35


async def _settled_body_after_tap(rpc, before, record, sleep):
    """Do not accept the intermediate heading of a still-animating turn."""
    previous = before
    stable = 0
    samples = []
    record.setdefault("settling", []).append(samples)
    for index in range(12):
        await sleep(0.25)
        current = unwrap(await rpc("observe_game", {}))
        player = current.get("player") or {}
        old = previous.get("player") or {}
        samples.append({"sequence": current.get("sequence"), "player": player})
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(current["timestamp_utc"].replace("Z", "+00:00"))).total_seconds()
            yaw, old_yaw = player["rotation"]["yaw"], old["rotation"]["yaw"]
            velocity = player["locomotion"]["velocity_cm_s"]
            settled = (
                type(current["sequence"]) is int and current["sequence"] > previous["sequence"]
                and 0 <= age <= 2 and current.get("running") is True
                and bool(before.get("map_id")) and current.get("map_id") == before["map_id"]
                and bool(player.get("actor_id")) and player.get("actor_id") == before["player"].get("actor_id")
                and not any(current.get(k) for k in ("paused", "in_menu", "in_combat", "loading", "cutscene"))
                and player["locomotion"].get("grounded") is True
                and isinstance(velocity, dict)
                and all(type(v) in (int, float) and math.isfinite(v)
                        for v in (yaw, old_yaw, *(velocity.get(k) for k in ("x", "y", "z"))))
                and math.sqrt(sum(velocity[k] ** 2 for k in ("x", "y", "z"))) <= 50
                and _horizontal_m(player["position"], old["position"]) <= 0.05
                and abs(player["position"]["z"] - old["position"]["z"]) <= 5
                and abs((yaw - old_yaw + 180) % 360 - 180) <= 1
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            settled = False
        stable = stable + 1 if settled else 0
        previous = current
        # Native turn animation can report an unchanged/intermediate heading
        # first. As with the strong recovery, allow it a full observed second.
        if stable >= 2 and index >= 3:
            return current
    return None


async def face_body_toward(rpc: Rpc, target: dict[str, float], sleep=asyncio.sleep) -> dict[str, Any]:
    """Turn Cloud's body toward the NPC; his heading error after at most a few taps."""
    record: dict[str, Any] = {"taps": 0}
    while True:
        observed = unwrap(await rpc("observe_game", {}))
        player = observed.get("player") or {}
        position = player.get("position") or {}
        yaw = (player.get("rotation") or {}).get("yaw")
        if (not isinstance(position,dict) or any(not isinstance(position.get(k),(int,float)) or not math.isfinite(position[k]) for k in ("x","y","z"))
                or not isinstance(yaw,(int,float)) or not math.isfinite(yaw)):
            record.update(ok=False, reason="npc_body_heading_unknown", heading_unknown=True)
            return record
        if _horizontal_m(position,target)>TALK_RANGE_M:
            record.update(ok=False,reason="npc_out_of_talk_range")
            return record
        bearing = math.degrees(math.atan2(target["y"] - position["y"], target["x"] - position["x"]))
        error = (bearing - yaw + 180.0) % 360.0 - 180.0
        record["heading_error_degrees"] = round(error, 1)
        if abs(error) <= FACE_BODY_TOLERANCE_DEG:
            record.update(ok=True, tolerance_degrees=FACE_BODY_TOLERANCE_DEG)
            return record
        if record["taps"] >= FACE_BODY_MAX_TAPS:
            return await _strong_face_once(rpc, target, observed, record, sleep)
        camera_yaw = float(((observed.get("camera") or {}).get("rotation") or {}).get("yaw") or 0.0)
        relative = math.radians(bearing - camera_yaw)
        await move_stick(rpc, {"x": round(math.sin(relative) * FACE_BODY_MAGNITUDE, 3),
                                        "y": round(math.cos(relative) * FACE_BODY_MAGNITUDE, 3),
                                        "milliseconds": FACE_BODY_TAP_MS}, sleep=sleep)
        record["taps"] += 1
        if await _settled_body_after_tap(rpc, observed, record, sleep) is None:
            record.update(ok=False, reason="npc_body_pose_not_settled")
            return record


async def _strong_face_once(rpc, target, before, record, sleep):
    """One observed .6/120ms recovery; never repeat a failed strong pulse."""
    record.update(ok=False, reason="npc_body_facing_not_ready", tolerance_degrees=FACE_BODY_TOLERANCE_DEG)
    canonical = target.get("canonical_id")
    if not isinstance(canonical, str) or not canonical.startswith("field_object:"):
        return record
    from .qb_opponents import opponent
    try:
        expected_actor=opponent(canonical)["actor_name"]
    except (MissionError,KeyError,ValueError,TypeError):
        return record
    def binding(resolved):
        actor = resolved.get("live_actor") or {}
        pose = resolved.get("position") or {}
        named = actor.get("actor_name") == expected_actor
        # Some hosts (Nene live) expose only their unique blueprint class.
        # Accept the resolver's exact, unique canonical proof; a conflicting
        # nonempty actor name or an ambiguous class still refuses recovery.
        candidates = (resolved.get("identity") or {}).get("candidates") or []
        class_proved = False
        if not actor.get("actor_name") and len(candidates) == 1:
            candidate = candidates[0]
            spec = candidate.get("chara_spec_name")
            class_proved = (candidate.get("canonical_id") == canonical
                and candidate.get("actor_name") == expected_actor and isinstance(spec, str) and bool(spec)
                and isinstance(actor.get("class"), str) and actor["class"].endswith(f".{spec}_C")
                and any(isinstance(e, dict) and e.get("field") == "class_chara_spec" and e.get("value") == spec
                    and e.get("static_canonical_ids") == [canonical]
                    and type(e.get("static_candidate_count")) is int and e["static_candidate_count"] == 1
                    and e.get("static_unique_to_record") is True for e in resolved.get("match_evidence", [])))
        if (resolved.get("status") != "located" or resolved.get("canonical_id") != canonical
                or actor.get("is_player") is True or not actor.get("id") or not (named or class_proved)
                or any(not isinstance(pose.get(k), (int,float)) or not math.isfinite(pose[k]) for k in ("x","y","z"))
                or _horizontal_m(pose,target) > .5):
            raise MissionError("npc_facing_target_changed")
        return actor["id"],pose
    def fresh(state, previous):
        player=state.get("player") or {};old=previous.get("player") or {}
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(state["timestamp_utc"].replace("Z","+00:00"))).total_seconds()
            seq=state["sequence"]
            loc=player["locomotion"];velocity=loc["velocity_cm_s"]
            return (type(seq) is int and seq > previous["sequence"] and 0 <= age <= 2
                    and state.get("running") is True and bool(before.get("map_id")) and state.get("map_id")==before.get("map_id")
                    and player.get("actor_id")==before["player"].get("actor_id") and bool(player.get("actor_id"))
                    and not any(state.get(k) for k in ("paused","in_menu","in_combat","loading","cutscene"))
                    and loc.get("grounded") is True
                    and all(isinstance(velocity[k],(int,float)) and math.isfinite(velocity[k]) for k in ("x","y","z"))
                    and math.sqrt(sum(velocity[k]**2 for k in ("x","y","z"))) <= 50
                    and _horizontal_m(player["position"],old["position"]) <= .05
                    and abs(player["position"]["z"]-old["position"]["z"]) <= 5)
        except (KeyError,TypeError,ValueError,OverflowError):
            return False
    try:
        actor_id,pose=binding(unwrap(await rpc("resolve_world_target", {"name":canonical,"seconds":10})))
        current=unwrap(await rpc("observe_game", {}))
        prompt=unwrap(await rpc("read_field_prompt", {}))
        if not fresh(current,before) or not prompt.get("talk") or _horizontal_m(current["player"]["position"],pose)>TALK_RANGE_M or abs(current["player"]["position"]["z"]-pose["z"])>150:
            return record
        player=current["player"];yaw=player["rotation"]["yaw"]
        camera=current["camera"]["rotation"]["yaw"]
        if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (yaw,camera)):
            return record
        bearing=math.degrees(math.atan2(pose["y"]-player["position"]["y"],pose["x"]-player["position"]["x"]))
        relative=math.radians(bearing-camera)
        record["strong_fallback"]={"magnitude":.6,"milliseconds":120,"actor_id":actor_id,"pre_sequence":current["sequence"],"observations":[]}
        await move_stick(rpc,{"x":round(math.sin(relative)*.6,3),"y":round(math.cos(relative)*.6,3),"milliseconds":120},sleep=sleep)
        previous=current;stable=0;settled=None
        for _ in range(12):
            await sleep(.25)
            post=unwrap(await rpc("observe_game", {}))
            record["strong_fallback"]["observations"].append({"sequence":post.get("sequence"),"player":post.get("player")})
            if fresh(post,previous):
                stable+=1
            else:
                stable=0
            previous=post
            # Do not accept the immediate unchanged samples before the observed
            # Mary turn appeared in settled telemetry (~later sequence50425).
            if stable>=2 and _>=3:
                settled=post;break
        if settled is None:
            return record
        post_id,post_pose=binding(unwrap(await rpc("resolve_world_target", {"name":canonical,"seconds":10})))
        if post_id!=actor_id or _horizontal_m(post_pose,pose)>.05 or abs(post_pose["z"]-pose["z"])>5:
            return record
        final=unwrap(await rpc("observe_game", {}))
        prompt=unwrap(await rpc("read_field_prompt", {}))
        if not fresh(final,settled) or not prompt.get("talk") or _horizontal_m(final["player"]["position"],post_pose)>TALK_RANGE_M:
            return record
        pos=final["player"]["position"];yaw=final["player"]["rotation"]["yaw"]
        error=(math.degrees(math.atan2(post_pose["y"]-pos["y"],post_pose["x"]-pos["x"]))-yaw+180)%360-180
        record["heading_error_degrees"]=round(error,1)
        record["strong_fallback"]["post_sequence"]=final["sequence"]
        if math.isfinite(error) and abs(error)<=FACE_BODY_TOLERANCE_DEG:
            record.update(ok=True,reason="npc_body_facing_verified")
    except (MissionError,KeyError,TypeError,ValueError):
        record["reason"]="npc_body_facing_not_ready"
    return record


async def face_until_talk(rpc: Rpc, target: dict[str, float], sleep=asyncio.sleep, *,
                          guide_moogle_target: dict[str, Any] | None = None) -> dict[str, Any]:
    """Tap toward the NPC until Talk is offered.

    Each tap pushes the stick toward the NPC relative to the current camera
    yaw, so Cloud turns to face them whichever way the camera points; the
    camera is not swung (live, a 4-step camera alignment ran out 58 degrees
    short and failed the phase).
    """
    async def admit_moogle() -> dict[str, Any]:
        from .guide_moogle import verify_talk_target
        try:
            scan = unwrap(await rpc("scan_character_inventory", {}))
            prompt = unwrap(await rpc("read_field_prompt", {}))
            return verify_talk_target(scan, prompt, guide_moogle_target)
        except MissionError as exc:
            raise MissionError(str(exc), response={"recovery_refused": True, "detail": exc.response}) from exc

    async def face_or_admit(distance: float) -> dict[str, Any]:
        from .guide_moogle import GUIDE_MOOGLE_TALK_M
        if guide_moogle_target is not None and distance <= GUIDE_MOOGLE_TALK_M:
            return {"ok": True, "taps": 0, "admission": await admit_moogle()}
        facing = await face_body_toward(rpc, target, sleep=sleep)
        if not facing.get("ok"):
            raise MissionError("npc_body_facing_not_ready", response={"attempts": attempts, "facing": facing})
        if guide_moogle_target is not None:
            facing["admission"] = await admit_moogle()
        return facing

    attempts = []
    for tap in range(MAX_FACE_TAPS + 1):
        prompt = unwrap(await rpc("read_field_prompt", {}))
        player = prompt.get("player_position") or {}
        distance = _horizontal_m(player, target) if player else None
        attempts.append({"tap": tap, "commands": prompt.get("commands"), "distance_m": distance})
        # The prompt does not say whom it is for (live: Ned's Talk read as
        # Nene's from 28 m away), so it counts only within range of this NPC.
        if prompt.get("talk") and distance is not None and distance <= TALK_RANGE_M:
            await sleep(TALK_CONFIRM_SECONDS)
            if unwrap(await rpc("read_field_prompt", {})).get("talk"):
                facing = await face_or_admit(distance)
                if not facing["taps"] or unwrap(await rpc("read_field_prompt", {})).get("talk"):
                    return {"ok": True, "taps": tap, "attempts": attempts, "facing": facing}
                attempts[-1]["talk_lost_after_facing"] = facing
            else:
                attempts[-1]["talk_lost"] = True
        if tap == MAX_FACE_TAPS:
            break
        if distance is None or distance > TALK_RANGE_M:
            raise MissionError("npc_out_of_talk_range", response={"attempts": attempts})
        bearing = math.degrees(math.atan2(target["y"] - player["y"], target["x"] - player["x"]))
        if tap == 0:
            # Talk needs the camera on the NPC too (live: Cloud beside Zahira
            # with the camera elsewhere got no prompt until it was turned to
            # her). Best effort: a short alignment that falls short does not
            # fail the phase; the taps below still turn Cloud.
            try:
                aligned = unwrap(await rpc("align_camera_to_bearing", {
                    "target_bearing_degrees": round(bearing, 1), "tolerance_degrees": FACE_CAMERA_TOLERANCE_DEG,
                    "maximum_steps": FACE_CAMERA_STEPS, "smooth": True}))
                attempts[-1]["camera_aligned"] = bool(aligned.get("ok", True))
            except MissionError as exc:
                attempts[-1]["camera_aligned"] = False
                attempts[-1]["camera_error"] = str(exc)
            prompt = unwrap(await rpc("read_field_prompt", {}))
            if prompt.get("talk"):
                attempts.append({"tap": 0, "commands": prompt.get("commands"), "distance_m": distance,
                                 "after": "camera_alignment"})
                facing = await face_or_admit(distance)
                if not facing["taps"] or unwrap(await rpc("read_field_prompt", {})).get("talk"):
                    return {"ok": True, "taps": 0, "attempts": attempts, "facing": facing}
                attempts[-1]["talk_lost_after_facing"] = facing
        observed = unwrap(await rpc("observe_game", {}))
        camera_yaw = float(((observed.get("camera") or {}).get("rotation") or {}).get("yaw") or 0.0)
        relative = math.radians(bearing - camera_yaw)
        await move_stick(rpc, {"x": round(math.sin(relative), 3), "y": round(math.cos(relative), 3),
                                        "milliseconds": FAR_TAP_MS if distance > NEAR_M else TAP_MS}, sleep=sleep)
        await sleep(TAP_SETTLE_SECONDS)
    raise MissionError("npc_talk_prompt_not_offered", response={"attempts": attempts})


async def cross_door(rpc: Rpc, door: dict[str, Any], sleep=asyncio.sleep) -> dict[str, Any]:
    """From in front of a door, face it until "Open" is offered and open it.

    The prompt shows within about 1.2 m of the door with Cloud and the camera
    on it (live at Nibelheim's Town Hall). Opening walks Cloud through.
    """
    centre = door["centre"]
    attempts = []
    if not door.get("open") and door.get("front") and door.get("beyond"):
        # A known door may have been left open: its leaf is then out of the
        # way, and there is no Open prompt to wait for.
        try:
            swept = unwrap(await rpc("sweep_route", {"points": [door["front"], door["beyond"]]}))
            kinds = [hit.get("kind") for segment in swept.get("segments") or [] for hit in segment.get("hits") or []]
            attempts.append({"swept": kinds})
            if "door" not in kinds:
                door = {**door, "open": True}
        except MissionError as exc:
            attempts.append({"sweep_error": str(exc)})
    if door.get("open") and door.get("beyond"):
        # Nothing to open (a door opened earlier stays open): step across.
        through = await step_onto(rpc, door["beyond"], sleep=sleep, taps=STEP_THROUGH_TAPS)
        if through.get("distance_m") is not None and through["distance_m"] <= STEP_THROUGH_REACHED_M:
            return {"actor": door.get("actor"), "open": True, "attempts": attempts, "onto_far_side": through}
        attempts.append({"stepped_through": through})
    for tap in range(MAX_DOOR_TAPS + 1):
        prompt = unwrap(await rpc("read_field_prompt", {}))
        player = prompt.get("player_position") or {}
        distance = _horizontal_m(player, centre) if player else None
        attempts.append({"tap": tap, "commands": prompt.get("commands"), "distance_m": distance})
        if prompt.get("door"):
            opened = unwrap(await rpc("open_door", {"observe_seconds": DOOR_OBSERVE_SECONDS}))
            moved = (opened.get("samples") or [{}])[-1].get("position") or {}
            crossed = {"actor": door.get("actor"), "taps": tap, "attempts": attempts, "from": player, "to": moved,
                       "moved_m": _horizontal_m(player, moved) if player and moved else None,
                       "prompt_cleared": opened.get("prompt_cleared"), "open_seconds": opened.get("seconds"),
                       "open_outcome": opened.get("outcome"), "open_samples": opened.get("samples")}
            if door.get("beyond"):
                crossed["onto_far_side"] = await step_onto(rpc, door["beyond"], sleep=sleep)
            return crossed
        if tap == MAX_DOOR_TAPS or distance is None or distance > DOOR_REACH_M:
            break
        bearing = math.degrees(math.atan2(centre["y"] - player["y"], centre["x"] - player["x"]))
        if tap == 0:
            try:
                unwrap(await rpc("align_camera_to_bearing", {
                    "target_bearing_degrees": round(bearing, 1), "tolerance_degrees": FACE_CAMERA_TOLERANCE_DEG,
                    "maximum_steps": FACE_CAMERA_STEPS, "smooth": True}))
            except MissionError as exc:
                attempts[-1]["camera_error"] = str(exc)
            continue
        observed = unwrap(await rpc("observe_game", {}))
        camera_yaw = float(((observed.get("camera") or {}).get("rotation") or {}).get("yaw") or 0.0)
        relative = math.radians(bearing - camera_yaw)
        await move_stick(rpc, {"x": round(math.sin(relative) * DOOR_TAP_MAGNITUDE, 3),
                                        "y": round(math.cos(relative) * DOOR_TAP_MAGNITUDE, 3),
                                        "milliseconds": TAP_MS}, sleep=sleep)
        await sleep(DOOR_TAP_SETTLE_SECONDS)
    if door.get("beyond") and not door.get("open"):
        # No Open prompt: the door may have been opened since it was found.
        through = await step_onto(rpc, door["beyond"], sleep=sleep, taps=STEP_THROUGH_TAPS)
        if through.get("distance_m") is not None and through["distance_m"] <= STEP_THROUGH_REACHED_M:
            return {"actor": door.get("actor"), "open": True, "attempts": attempts, "onto_far_side": through}
        attempts.append({"stepped_through": through})
    raise MissionError("door_prompt_not_offered", response={"door": door, "attempts": attempts})


async def step_onto(rpc: Rpc, point: dict[str, float], sleep=asyncio.sleep,
                    taps: int = 0) -> dict[str, Any]:
    """Short taps toward ``point`` until Cloud stands on it.

    After a door's scene Cloud can be left in the doorway, between the two
    navmesh islands, where no route can start (live at Nibelheim:
    live_start_not_inside_current_topology after Dale's street door). The
    door's far-side point is on the navmesh beyond it. ``taps`` (default
    STEP_ONTO_TAPS) bounds the taps; more walk Cloud through an open doorway.
    """
    limit = taps or STEP_ONTO_TAPS
    tap = 0
    distance = None
    for tap in range(limit + 1):
        observed = unwrap(await rpc("observe_game", {}))
        feet = ((observed.get("player") or {}).get("locomotion") or {}).get("feet_position") or {}
        if not feet:
            break
        distance = _horizontal_m(feet, point)
        if distance <= STEP_ONTO_REACHED_M or tap == limit:
            break
        bearing = math.degrees(math.atan2(point["y"] - feet["y"], point["x"] - feet["x"]))
        camera_yaw = float(((observed.get("camera") or {}).get("rotation") or {}).get("yaw") or 0.0)
        relative = math.radians(bearing - camera_yaw)
        await move_stick(rpc, {"x": round(math.sin(relative) * DOOR_TAP_MAGNITUDE, 3),
                                        "y": round(math.cos(relative) * DOOR_TAP_MAGNITUDE, 3),
                                        "milliseconds": STEP_ONTO_TAP_MS}, sleep=sleep)
        await sleep(DOOR_TAP_SETTLE_SECONDS)
    return {"taps": tap, "distance_m": distance}


# Where Talk was offered, per NPC: routing straight there next time skips
# the stop-short guess (live: Ned sits behind his card table, and a route
# stopping 4.4 m away on the wrong side never got the prompt).
TALK_SPOT_NPC_TOLERANCE_CM = 100.0
TALK_SPOT_OFF_NAVMESH = "cached_topology_route_target_not_inside_topology"


class TalkSpots:
    """Cloud's feet position where each NPC last offered Talk, on disk."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def get(self, canonical_id: str | None, npc: dict[str, float]) -> dict[str, float] | None:
        entry = self._load().get(str(canonical_id)) if canonical_id else None
        if not isinstance(entry, dict):
            return None
        try:
            if _horizontal_m(entry["npc"], npc) * 100.0 > TALK_SPOT_NPC_TOLERANCE_CM:
                return None  # the NPC stands elsewhere now
            return {axis: float(entry["feet"][axis]) for axis in ("x", "y", "z")}
        except (KeyError, TypeError, ValueError):
            return None

    def record(self, canonical_id: str | None, npc: dict[str, float], feet: dict[str, float]) -> None:
        if not canonical_id:
            return
        spots = self._load()
        spots[str(canonical_id)] = {"npc": npc, "feet": feet,
                                    "recorded_at_utc": datetime.now(timezone.utc).isoformat()}
        self._save(spots)

    def forget(self, canonical_id: str | None) -> None:
        spots = self._load()
        if canonical_id and spots.pop(str(canonical_id), None) is not None:
            self._save(spots)

    def _save(self, spots: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(spots, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


def default_talk_spots() -> TalkSpots:
    return TalkSpots(Path(os.environ.get("LOCALAPPDATA", ".")) / "ff7r-agent" / "talk-spots.json")


# Back presses and the wait after each: the menu has ignored quick presses
# straight after a harness restart (#354), so later waits are longer.
MENU_CLOSE_SETTLES_SECONDS = (1.0, 1.0, 1.0, 2.0, 3.0, 4.0, 5.0)
MENU_CLOSE_HOLD_MS = 400


async def close_main_menu(rpc: Rpc, sleep=asyncio.sleep) -> None:
    """Back out of a main menu open over the field (it pauses the game).

    Live, a virtual-gamepad reconnect opened the main menu and every route of
    an approach failed "gameplay is paused; no input was sent". Use Back:
    the menu toggle failed to close it live and guarded Back recovered it.
    Back maps to pad B or keyboard Esc; the live backend was not recorded.
    Keep the normal guarded tap and bounded retries.
    """
    for settle in MENU_CLOSE_SETTLES_SECONDS:
        if not unwrap(await rpc("observe_game", {})).get("in_menu"):
            return
        await ensure_control_session(rpc)
        # Opening the session can reconnect the pad and change menu state.
        # Reobserve before Back so it cannot become a field dodge.
        if not unwrap(await rpc("observe_game", {})).get("in_menu"):
            return
        unwrap(await rpc("tap_button", {"button": "cancel", "milliseconds": MENU_CLOSE_HOLD_MS}))
        await sleep(settle)
    if unwrap(await rpc("observe_game", {})).get("in_menu"):
        raise MissionError("main_menu_did_not_close")


async def close_menu_if_open(rpc: Rpc, sleep=asyncio.sleep) -> bool:
    """Close a main menu opened since the approach began; True if one was.

    Live, the virtual-gamepad reconnect after a bridge restart opened the
    main menu during target resolution, after the approach's first check,
    and all three routes refused "gameplay is paused".
    """
    if not unwrap(await rpc("observe_game", {})).get("in_menu"):
        return False
    await close_main_menu(rpc, sleep=sleep)
    return True


def _catalogue_record(name: str) -> dict[str, Any] | None:
    """A Queen's Blood opponent's record, else a card vendor's host record."""
    from .qb_opponents import opponent
    try:
        record = opponent(name)
    except Exception:
        record = None
    if not isinstance(record, dict):
        from .qb_card_vendors import vendor_host
        try:
            record = vendor_host(name)
        except Exception:
            return None
    return record if isinstance(record, dict) else None


def _xyz(value: Any) -> dict[str, float] | None:
    try:
        x, y, z = (float(item) for item in value["xyz"])
    except (TypeError, KeyError, ValueError):
        return None
    return {"x": x, "y": y, "z": z}


def catalogue_position(name: str) -> dict[str, float] | None:
    """Where the level data places an NPC, if a catalogue knows it: a Queen's
    Blood opponent, else a card vendor behind its counter."""
    return _xyz((_catalogue_record(name) or {}).get("position"))


def catalogue_front(name: str, npc: dict[str, float]) -> list[dict[str, Any]]:
    """Spots in front of a catalogued NPC at feet height ``npc``, best first.

    The game's own stand point for Cloud, then FRONT_DISTANCES_M along the
    NPC's facing. Empty unless the catalogue places them where they stand
    now (within TALK_SPOT_NPC_TOLERANCE_CM): elsewhere, their facing there
    is unknown.
    """
    record = _catalogue_record(name) or {}
    placed = _xyz(record.get("position"))
    if placed is None or _horizontal_m(placed, npc) * 100.0 > TALK_SPOT_NPC_TOLERANCE_CM:
        return []
    spots = []
    stand = _xyz(record.get("stand"))
    if stand is not None and FRONT_MIN_STAND_M <= _horizontal_m(stand, placed) <= TALK_RANGE_M:
        spots.append({"x": stand["x"], "y": stand["y"], "z": stand["z"] - CAPSULE_HALF_HEIGHT_CM,
                      "source": "catalogue_stand_point"})
    yaw = (record.get("position") or {}).get("yaw_deg")
    if isinstance(yaw, (int, float)) and math.isfinite(yaw):
        heading = math.radians(yaw)
        for distance_m in FRONT_DISTANCES_M:
            spots.append({"x": npc["x"] + math.cos(heading) * distance_m * 100.0,
                          "y": npc["y"] + math.sin(heading) * distance_m * 100.0,
                          "z": npc["z"], "source": f"catalogue_facing_{distance_m:g}m"})
    return spots


def npc_front(resolved: dict[str, Any], npc: dict[str, float]) -> list[dict[str, Any]]:
    """Front spots from this resolved actor's rotation, else matching placement."""
    actor = resolved.get("live_actor")
    if not isinstance(actor, dict):
        actor = {"position": resolved.get("position"), "rotation": resolved.get("rotation")}
    pose = actor.get("position") or {}
    yaw = (actor.get("rotation") or {}).get("yaw")
    resolved_pose = resolved.get("position") or {}
    if (resolved.get("location_source") == "live_actor"
            and type(yaw) in (int, float) and math.isfinite(yaw)
            and all(type(pose.get(k)) in (int, float) and math.isfinite(pose[k])
                    and pose[k] == resolved_pose.get(k) for k in ("x", "y", "z"))):
        angle = math.radians(yaw)
        return [{"x": npc["x"] + math.cos(angle) * d * 100.0,
                 "y": npc["y"] + math.sin(angle) * d * 100.0, "z": npc["z"],
                 "source": f"live_facing_{d:g}m", "facing_yaw_deg": yaw}
                for d in FRONT_DISTANCES_M]
    name = resolved.get("canonical_id") or resolved.get("requested_name") or ""
    spots = catalogue_front(name, npc)
    yaw = ((_catalogue_record(name) or {}).get("position") or {}).get("yaw_deg")
    if type(yaw) not in (int, float) or not math.isfinite(yaw):
        return []
    # A game stand point is useful only if it is on the current front side.
    return [{**spot, "facing_yaw_deg": yaw} for spot in spots
            if in_front_of_npc(spot, npc, yaw)]


def in_front_of_npc(point: dict[str, float], npc: dict[str, float], yaw: float) -> bool:
    dx, dy = point["x"] - npc["x"], point["y"] - npc["y"]
    distance = math.hypot(dx, dy)
    return (FRONT_MIN_STAND_M * 100.0 <= distance <= TALK_RANGE_M * 100.0
            and (dx * math.cos(math.radians(yaw)) + dy * math.sin(math.radians(yaw))) / distance >= 0.5)


async def _nearest_loaded(rpc: Rpc, candidate: dict[str, Any]) -> dict[str, Any] | None:
    """The nearest loaded actor of ``candidate``'s model, as a located target;
    None when the inventory cannot be read or holds none."""
    spec = str(candidate.get("chara_spec_name") or "")
    try:
        scan = unwrap(await rpc("scan_character_inventory", {}))
    except MissionError:
        return None
    rows = ((scan.get("snapshot") or {}).get("characters") if isinstance(scan, dict) else None) or []
    model = f".{spec}_C".casefold()
    live = [row for row in rows if isinstance(row, dict) and not row.get("is_player") and spec
            and str(row.get("class") or "").casefold().endswith(model)
            and isinstance(row.get("position"), dict) and isinstance(row.get("distance"), (int, float))]
    if not live:
        return None
    nearest = min(live, key=lambda row: float(row["distance"]))
    position = nearest["position"]
    # Talk spots are kept per actor: name this one by where it stands.
    spot_id = f"{candidate.get('canonical_id')}@{round(position['x'] / 100)},{round(position['y'] / 100)}"
    return {"status": "located", "position": position, "canonical_id": spot_id,
            "live_actor": nearest.get("id"), "live_candidate_count": len(live),
            "location_source": "live_actor", "rotation": nearest.get("rotation")}


def _interchangeable(candidates: list[dict[str, Any]]) -> bool:
    """Several static records that are one service under one name and model:
    the Gold Saucer's eleven Guide Moogles. Any of them will do."""
    specs = {c.get("chara_spec_name") for c in candidates}
    names = {tuple(c.get("display_names") or ()) for c in candidates}
    return len(candidates) > 1 and len(specs) == 1 and None not in specs and len(names) == 1 and () not in names


async def nearest_interchangeable(rpc: Rpc, resolved: dict[str, Any]) -> dict[str, Any] | None:
    """The nearest live actor when the name means any of several identical
    ones (live 2026-10-04: "Guide Moogle" is ambiguous statically, and each
    Moogle's object id is ambiguous live, since all share one model)."""
    identity = resolved.get("identity") or {}
    if resolved.get("status") == "ambiguous":
        candidates = identity.get("candidates") or []
        if not _interchangeable(candidates):
            return None
        # Every loaded one, from the character inventory: resolving one
        # record by name probes for a single actor of the shared model, any
        # one (live 2026-10-05 from the entrance it gave the Moogle 440 m
        # away by the card organiser, and Cloud ran past the one in view).
        nearest = await _nearest_loaded(rpc, candidates[0])
        if nearest is not None:
            return {**resolved, **nearest}
        try:
            live = unwrap(await rpc("resolve_world_target", {"name": candidates[0]["object_id"]}))
        except MissionError as exc:
            live = exc.response if isinstance(exc.response, dict) else {}
        if live.get("status") == "located":
            return live
        resolved = live
    if resolved.get("status") != "live_actor_identity_ambiguous":
        return None
    for evidence in resolved.get("blocked_match_evidence") or []:
        for item in evidence if isinstance(evidence, list) else [evidence]:
            if item.get("static_identity_field") != "chara_spec_name":
                return None
    live = [c for c in resolved.get("live_candidates") or [] if isinstance(c.get("position"), dict)]
    if not live:
        return None
    nearest = min(live, key=lambda c: float(c.get("distance") or 0.0))
    position = nearest["position"]
    # Talk spots are kept per actor: name this one by where it stands.
    spot_id = f"{resolved.get('canonical_id')}@{round(position['x'] / 100)},{round(position['y'] / 100)}"
    return {**resolved, "status": "located", "position": position, "canonical_id": spot_id,
            "live_actor": nearest.get("id")}


class AttributedApproachRpc:
    """One caller identity across travel, routes, recovery and Moogle input."""
    def __init__(self, rpc: Rpc, *, model_id: str | None = None,
                 reasoning_effort: str | None = None, client_id: str | None = None):
        if (model_id is None) != (reasoning_effort is None):
            raise ValueError("model_id and reasoning_effort must be supplied together")
        if model_id is not None and any(not isinstance(v, str) or not v.strip()
                                       for v in (model_id, reasoning_effort)):
            raise ValueError("model_id and reasoning_effort must be nonempty strings")
        if client_id is not None and (not isinstance(client_id, str) or not client_id.strip()):
            raise ValueError("client_id must be a nonempty string")
        self.rpc, self.model_id, self.reasoning_effort = rpc, model_id, reasoning_effort
        self.client_id = client_id or uuid.uuid4().hex
        # Whether this caller already held the session at the run's first
        # lease read (None until then).
        self.lent_session: bool | None = None
        # Whether this run opened the session it used. Only such a session is
        # closed with the run; one that was open is the agent's and stays so.
        self.opened_session = False
        # Whether the run has read the session at all (a run that never
        # needed input never touches it).
        self.session_seen = False

    def _note_lease(self, lease: Any) -> None:
        if isinstance(lease, dict):
            self.session_seen = True
        if self.lent_session is None and isinstance(lease, dict):
            self.lent_session = bool(lease.get("active")) and lease.get("client_id") == self.client_id

    async def __call__(self, operation: str, arguments: dict[str, Any]):
        if operation == "get_control_session_status":
            result = await self.rpc(operation, arguments)
            try:
                self._note_lease(unwrap(result))
            except MissionError:
                pass
            return result
        if operation in {"open_control_session", "close_control_session", "stop_all_input"}:
            lease = unwrap(await self.rpc("get_control_session_status", {}))
            self._note_lease(lease)
            if lease.get("active"):
                # An open session lasts as long as the game and is used by
                # whoever asks, not replaced: the approach borrows it.
                if operation == "open_control_session":
                    return lease
                if operation == "close_control_session" and lease.get("client_id") != self.client_id:
                    return {"closed": False, "reason": "session_borrowed", "client_id": lease.get("client_id")}
            if operation == "open_control_session":
                if self.model_id is None:
                    raise MissionError("control_session_requires_explicit_model_and_effort")
                if arguments.get("client_id") not in (None, self.client_id):
                    raise MissionError("control_session_client_identity_mismatch")
                arguments = {**arguments, "client_id": self.client_id,
                             "model_id": self.model_id, "reasoning_effort": self.reasoning_effort}
                opened = unwrap(await self.rpc(operation, arguments))
                if not opened.get("active") or opened.get("client_id") != self.client_id:
                    raise MissionError("control_session_identity_mismatch")
                self.opened_session = True
                return opened
            if operation == "close_control_session":
                # The server closes only the client's own session.
                arguments = {**arguments, "client_id": self.client_id}
        return await self.rpc(operation, arguments)


async def ensure_control_session(rpc: Rpc) -> None:
    """Use the open control session, or open one if the caller can attribute it."""
    unwrap(await rpc("open_control_session", {}))


async def hold_control_session(rpc: Rpc, client_id: str) -> None:
    """Use the open control session, whoever holds it; open one if none is."""
    lease = unwrap(await rpc("get_control_session_status", {}))
    if lease.get("active"):
        return
    unwrap(await rpc("open_control_session", {"client_id": client_id}))


TOPOLOGY_CAPTURE_POLL_SECONDS = 10.0
TOPOLOGY_CAPTURE_TIMEOUT_SECONDS = 900.0
COVERAGE_MISSING_REASONS = (NO_CAPTURE_COVERS_ROUTE, NO_CAPTURE_COVERS_START, "no_linked_polygon_corridor")


def _coverage_missing(exc: Exception) -> bool:
    return any(reason in str(exc) for reason in COVERAGE_MISSING_REASONS)


# A capture that left no route to a target is not repeated for a day: live
# 2026-09-30, Kalm's item shop was captured twice in one approach (10 min
# each, before and after the fast-travel reset) and neither helped.
CAPTURE_RETRY_AFTER_SECONDS = 24 * 3600


def _capture_memory_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", ".")) / "ff7r-agent" / "approach-captures.json"


def _capture_key(target: dict[str, float]) -> str:
    return f"{round(target['x'] / 500)}:{round(target['y'] / 500)}"  # 5 m cells


def capture_already_failed(target: dict[str, float], now: float | None = None) -> bool:
    try:
        record = json.loads(_capture_memory_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    failed_at = record.get(_capture_key(target)) if isinstance(record, dict) else None
    return isinstance(failed_at, (int, float)) and (now or time.time()) - failed_at < CAPTURE_RETRY_AFTER_SECONDS


def remember_failed_capture(target: dict[str, float], now: float | None = None) -> None:
    path = _capture_memory_path()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            record = {}
    except (OSError, ValueError):
        record = {}
    record[_capture_key(target)] = now or time.time()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    except OSError:
        pass


async def capture_topology(rpc: Rpc, target: dict[str, float], sleep=asyncio.sleep,
                           clock=time.monotonic) -> dict[str, Any]:
    """Capture and admit the navmesh around Cloud and ``target`` (no movement)."""
    job = unwrap(await rpc("start_topology_capture", {"route_target_position": target}))
    started, deadline = clock(), clock() + TOPOLOGY_CAPTURE_TIMEOUT_SECONDS
    status = job
    while status.get("state") == "running" and clock() < deadline:
        await sleep(TOPOLOGY_CAPTURE_POLL_SECONDS)
        status = unwrap(await rpc("get_topology_capture_status", {"job_id": job["job_id"]}))
    response = status.get("response") if isinstance(status.get("response"), dict) else {}
    result = response.get("result") if isinstance(response.get("result"), dict) else None
    if status.get("state") == "running" or result is None or result.get("ok") is False:
        raise MissionError(f"topology_capture_failed:{response.get('error') or status.get('state')}",
                           response={"status": status})
    # A capture job can finish without a capture admitted to the library
    # (live 2026-09-30 at Gongaga): that is a failed capture, with its reason.
    preflight = result.get("preflight") if isinstance(result.get("preflight"), dict) else {}
    if result.get("cache_admitted") is not True:
        raise MissionError(f"topology_capture_not_admitted:{preflight.get('reason')}",
                           response={"job_id": job.get("job_id"), "seconds": round(clock() - started, 1),
                                     "failures": preflight.get("failures"),
                                     "exception": preflight.get("exception_diagnostics")})
    return {"job_id": job.get("job_id"), "seconds": round(clock() - started, 1), "admitted": True}


# An NPC this far off is in another area: fast travel to the spot nearest
# them first (live 2026-09-30, Gongaga's item shop from Kalm).
TRAVEL_FIRST_M = 500.0
EXTRICATE_DIRECTIONS = 16
EXTRICATE_REACH_CM = 250.0
EXTRICATE_MIN_CLEAR_CM = 100.0
EXTRICATE_PULSE_MS = 300
EXTRICATE_MAX_PULSES = 4
BLOCKING_KINDS = {"fixed", "vehicle", "npc", "ledge", "drop"}
TRAVEL_POLL_SECONDS = 3.0
TRAVEL_TIMEOUT_SECONDS = 180.0


async def clearance(rpc: Rpc, feet: dict[str, float]) -> dict[float, float]:
    """How far Cloud's capsule gets in each of 16 directions (cm, up to the reach).

    Vehicle bodies are swept for too: they answer only their own object type
    (live at Kalm: pinned against the Golden Plumes wagon's shafts, the
    default sweep scored them wide open and each step went into them).
    """
    from .route_sweep import VEHICLE_BODY_QUERY
    angles = [360.0 * k / EXTRICATE_DIRECTIONS for k in range(EXTRICATE_DIRECTIONS)]
    points = []
    for angle in angles:
        radians = math.radians(angle)
        points += [dict(feet), {"x": feet["x"] + math.cos(radians) * EXTRICATE_REACH_CM,
                                "y": feet["y"] + math.sin(radians) * EXTRICATE_REACH_CM, "z": feet["z"]}]
    sweeps = [unwrap(await rpc("sweep_route", {"points": points}))]
    try:
        sweeps.append(unwrap(await rpc("sweep_route", {"points": points, "query": VEHICLE_BODY_QUERY})))
    except MissionError:
        pass
    clear = {angle: EXTRICATE_REACH_CM for angle in angles}
    for sweep in sweeps:
        for segment in sweep.get("segments") or []:
            if segment.get("index", 1) % 2:
                continue  # the legs back to Cloud
            blocking = [float(hit.get("distance_cm") or 0.0) for hit in segment.get("hits") or []
                        if hit.get("kind") in BLOCKING_KINDS]
            angle = angles[segment["index"] // 2]
            clear[angle] = min([clear[angle], *blocking])
    return clear


async def extricate(rpc: Rpc, target: dict[str, float], sleep=asyncio.sleep) -> dict[str, Any]:
    """Step Cloud out of a spot routes keep stalling in.

    The last resort before a fast-travel reset: a screenshot is kept for
    review, the capsule is swept in 16 directions round Cloud, and he steps
    toward the most open one (ties broken toward the NPC), a few bounded
    pulses at most.
    """
    record: dict[str, Any] = {}
    try:
        shot = unwrap(await rpc("capture_game_window", {}))
        record["screenshot"] = next((value for key, value in shot.items() if key.endswith("path")), None)
    except MissionError as exc:
        record["screenshot_error"] = str(exc)
    observed = unwrap(await rpc("observe_game", {}))
    feet = observed["player"]["locomotion"]["feet_position"]
    camera_yaw = float(((observed.get("camera") or {}).get("rotation") or {}).get("yaw") or 0.0)
    clear = await clearance(rpc, feet)
    bearing = math.degrees(math.atan2(target["y"] - feet["y"], target["x"] - feet["x"]))
    ranked = sorted(clear.items(), key=lambda item: -(item[1] + 50.0 * math.cos(math.radians(item[0] - bearing))))
    record["clearance_cm"] = {str(round(angle)): round(value) for angle, value in clear.items()}
    if not ranked or ranked[0][1] < EXTRICATE_MIN_CLEAR_CM:
        record["moved"] = False
        return record
    heading, room = ranked[0]
    record["heading_degrees"] = heading
    relative = math.radians(heading - camera_yaw)
    await ensure_control_session(rpc)
    start = feet
    for _ in range(EXTRICATE_MAX_PULSES):
        await move_stick(rpc, {"x": round(math.sin(relative), 3), "y": round(math.cos(relative), 3),
                                        "milliseconds": EXTRICATE_PULSE_MS}, sleep=sleep)
        await sleep(0.4)
        now = unwrap(await rpc("observe_game", {}))["player"]["locomotion"]["feet_position"]
        if _horizontal_m(start, now) * 100.0 >= min(room - 60.0, 200.0):
            break
    moved = _horizontal_m(start, now)
    record.update(moved=moved > 0.3, moved_m=moved)
    return record


# Swimming: the navmesh can cross a river deep enough that Cloud swims (live
# 2026-10-01, Grasslands, twice at the same ford). While he swims telemetry
# reports him not grounded and not controllable, so routes stop and none can
# start. A hop off a ledge is airborne for a moment only; swimming persists.
START_OFF_NAVMESH_REASON = "live_start_not_inside_current_topology"
UNRESOLVABLE_START_REASONS = (START_OFF_NAVMESH_REASON, "cache_current_cloud_unresolvable")
MAX_START_STEPS = 2
SWIM_CHECK_SAMPLES = 3
SWIM_CHECK_INTERVAL_SECONDS = 0.3
SWIM_PULSE_MS = 700
SWIM_MAX_PULSES_PER_HEADING = 8
SWIM_STALLED_M = 0.3
SWIM_HEADING_OFFSETS = (0.0, 180.0, 90.0, -90.0)


def _feet_and_grounded(observed: dict[str, Any]) -> tuple[dict[str, float], bool | None]:
    locomotion = (observed.get("player") or {}).get("locomotion") or {}
    return locomotion.get("feet_position") or {}, locomotion.get("grounded")


async def leave_water(rpc: Rpc, toward: dict[str, float], sleep=asyncio.sleep) -> dict[str, Any] | None:
    """Swim Cloud to land if he is swimming; None when he is not.

    He swims on toward ``toward`` (across the river, as the route meant),
    then back, then to either side, a heading at a time, until telemetry
    has him grounded again.
    """
    samples = []
    for index in range(SWIM_CHECK_SAMPLES):
        feet, grounded = _feet_and_grounded(unwrap(await rpc("observe_game", {})))
        if grounded is not False or not feet:
            return None
        samples.append(feet)
        if index + 1 < SWIM_CHECK_SAMPLES:
            await sleep(SWIM_CHECK_INTERVAL_SECONDS)
    start = samples[-1]
    record: dict[str, Any] = {"swimming": True, "from": start, "headings": [], "landed": False}
    await ensure_control_session(rpc)
    bearing = math.degrees(math.atan2(toward["y"] - start["y"], toward["x"] - start["x"]))
    feet = start
    for offset in SWIM_HEADING_OFFSETS:
        heading = bearing + offset
        tried = {"heading_degrees": round(heading % 360.0, 1), "pulses": 0}
        record["headings"].append(tried)
        recent: list[dict[str, float]] = [feet]
        for _ in range(SWIM_MAX_PULSES_PER_HEADING):
            observed = unwrap(await rpc("observe_game", {}))
            camera_yaw = float(((observed.get("camera") or {}).get("rotation") or {}).get("yaw") or 0.0)
            relative = math.radians(heading - camera_yaw)
            await move_stick(rpc, {"x": round(math.sin(relative), 3), "y": round(math.cos(relative), 3),
                                            "milliseconds": SWIM_PULSE_MS, "swimming": True}, sleep=sleep)
            tried["pulses"] += 1
            await sleep(0.3)
            feet, grounded = _feet_and_grounded(unwrap(await rpc("observe_game", {})))
            if grounded is True:
                await sleep(SWIM_CHECK_INTERVAL_SECONDS)
                feet, grounded = _feet_and_grounded(unwrap(await rpc("observe_game", {})))
                if grounded is True:
                    record.update(landed=True, to=feet)
                    return record
            recent = (recent + [feet])[-3:]
            if len(recent) == 3 and _horizontal_m(recent[0], recent[-1]) < SWIM_STALLED_M:
                break  # against a bank too steep to climb out of here
    record["to"] = feet
    return record


# A post-match or post-visit conversation plays out on its own for a few
# seconds; wait (sending nothing) before refusing to route or travel.
FIELD_RETURN_WAIT_SECONDS = 20.0


async def wait_for_field(rpc: Rpc, sleep=asyncio.sleep, clock=time.monotonic,
                         seconds: float = FIELD_RETURN_WAIT_SECONDS,
                         require_locomotion: bool = False) -> dict[str, Any]:
    """The first observation with live field telemetry; raises the blocker if none comes.

    Read-only: it never sends input, so an open dialogue that needs a choice
    still fails here with ``dialogue_active`` for finish_conversation_async.
    """
    deadline = clock() + seconds
    while True:
        observation = unwrap(await rpc("observe_game", {}))
        blocker = field_blocker(observation)
        if not blocker and require_locomotion:
            try:
                finite_position(((observation.get("player") or {}).get("locomotion") or {}).get("feet_position"))
            except MissionError:
                blocker = "field_locomotion_unavailable"
        if not blocker:
            return observation
        if clock() >= deadline:
            raise MissionError(blocker, response=observation)
        await sleep(1.0)


async def travel_to_spot(rpc: Rpc, spot: str, sleep=asyncio.sleep, clock=time.monotonic) -> dict[str, Any]:
    """Fast travel by map spot name (travel_to_map_spot_async) and wait."""
    await wait_for_field(rpc, sleep=sleep, clock=clock)
    # The async travel worker focuses the game and establishes the settled
    # start before any map input. A 30-second synchronous settle here exceeded
    # the ordinary bridge's 25-second deadline, especially while unfocused.
    await ensure_control_session(rpc)
    job = unwrap(await rpc("travel_to_map_spot_async", {"spot": spot, "execute": True}))
    deadline = clock() + TRAVEL_TIMEOUT_SECONDS
    status = job
    while status.get("state") == "running" and clock() < deadline:
        await sleep(TRAVEL_POLL_SECONDS)
        status = unwrap(await rpc("get_travel_to_map_spot_status", {"job_id": job["job_id"]}))
    if status.get("state") == "running":
        # Cancel this job before handing off; never start another travel while
        # its map confirmation or loading state may still be in progress.
        unwrap(await rpc("stop_all_input", {}))
        raise MissionError("reset_travel_uncertain:job_timeout", response=status)
    response = status.get("response") or {}
    result = unwrap(response) if isinstance(response, dict) else None
    if isinstance(result, dict) and isinstance(result.get("result"), dict):
        result = unwrap(result["result"])
    if not isinstance(result, dict) or result.get("state") not in ("arrived", "already_there"):
        raise MissionError(f"reset_travel_failed:{response.get('error') if isinstance(response, dict) else status}")
    if not has_player_position({"player": {"position": result.get("arrival_position")}}):
        raise MissionError("reset_travel_arrival_position_unavailable")
    # The control session stays open: the approach goes on from the arrival.
    return result


GUIDE_MOOGLE_LOAD_WAIT_SECONDS = 3.0


async def travel_by_guide_moogle(rpc: Rpc, destination: str, *, output: Path, identity_check,
                                 clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep,
                                 talk_spots: TalkSpots | None = None, pursuit: bool = False) -> dict[str, Any]:
    """Walk to the nearest Guide Moogle and ride it to a Gold Saucer zone
    (``destination``, e.g. "Ghost Square"); waits for the arrival."""
    target = guide_moogle_zone(destination)
    started = clock()
    report: dict[str, Any] = {"destination": target.name}
    for attempt in range(2):
        try:
            # Only a Moogle at hand: just after arriving, the one by Cloud may
            # not be loaded yet, and the nearest loaded one is across the
            # Saucer (live 2026-10-05: 255 m from the entrance).
            walk = await _approach_once(rpc, GUIDE_MOOGLE, output=output / "guide-moogle",
                                        identity_check=identity_check, execute=True,
                                        stop_distance_m=DEFAULT_STOP_DISTANCE_M, camera_follow=True,
                                        clock=clock, sleep=sleep, talk_spots=talk_spots, pursuit=pursuit,
                                        moogle_travel=False, max_start_distance_m=GUIDE_MOOGLE_REACH_M)
            break
        except MissionError as exc:
            if "npc_beyond_reach" not in str(exc) or attempt:
                raise MissionError(f"guide_moogle_not_reached:{exc}", response=report) from exc
            report["beyond_reach"] = exc.response
            await sleep(GUIDE_MOOGLE_LOAD_WAIT_SECONDS)
    report["walk"] = {key: walk.get(key) for key in ("ok", "state", "reason", "start_distance_m", "phases_seconds", "recovery_refused")}
    if not walk.get("ok"):
        if walk.get("recovery_refused"):
            report["recovery_refused"] = True
        raise MissionError(f"guide_moogle_not_reached:{walk.get('reason')}", response=report)
    await ensure_control_session(rpc)
    # Carry the exact actor binding into the worker's final, locked Talk check.
    admission = ((walk.get("face") or {}).get("facing") or {}).get("admission")
    if (not isinstance(admission, dict) or admission.get("ok") is not True
            or not isinstance(admission.get("target"), dict)):
        raise MissionError("guide_moogle_talk_admission_missing", response={**report, "recovery_refused": True})
    if identity_check is not None:
        unwrap(await rpc("refresh_lifecycle_attachment", {}))
        identity = unwrap(await rpc("get_runtime_identity", {}))
        bootstrap = unwrap(await rpc("get_bootstrap_verification", {}))
        if identity_check(identity, bootstrap).get("ok") is not True:
            raise MissionError("runtime_identity_mismatch", response={"recovery_refused": True})
        report["runtime_identity"] = {
            key: identity[key] for key in ("git_commit_sha", "git_branch", "deployment_profile")
            if identity.get(key) is not None
        }
    used = unwrap(await rpc("use_guide_moogle", {"destination": target.name, "execute": True,
                                               "expected_target": admission["target"]}))
    report["menu"] = used
    if used.get("state") == "travelling":
        arrival = used["arrival"]
        deadline = clock() + GUIDE_MOOGLE_ARRIVAL_SECONDS
        while True:
            await sleep(1.0)
            try:
                observation = unwrap(await rpc("observe_game", {}))
            except MissionError:
                observation = {}  # loading
            feet = ((observation.get("player") or {}).get("locomotion") or {}).get("feet_position")
            if feet and not field_blocker(observation) and _horizontal_m(feet, arrival) <= GUIDE_MOOGLE_ARRIVAL_M:
                report["arrival_position"] = feet
                break
            if clock() >= deadline:
                raise MissionError("guide_moogle_arrival_timeout", response=report)
        # The service's bounded default is eight seconds, within the ordinary
        # RPC deadline; a failed proof still stops rather than authorizing input.
        settled = unwrap(await rpc("wait_for_settled_gameplay", {}))
        if isinstance(settled, dict) and settled.get("ok") is True:
            report["guide_moogle_proof"] = {
                "arrival_observed": True,
                "arrival_position": feet,
                "settled": True,
            }
    report["seconds"] = round(clock() - started, 2)
    return report


async def approach_npc(rpc: Rpc, name: str, *, execute: bool, model_id: str | None = None,
                       reasoning_effort: str | None = None, client_id: str | None = None,
                       **options: Any) -> dict[str, Any]:
    """Approach ``name`` (see _approach_npc).

    The control session lasts as long as the game, so the run uses the one
    that is open, whoever holds it (such as the session startup opens), and
    leaves it open (#342). Only a session the run had to open itself is
    closed with it. A run that ends with input possibly still in flight
    sends an emergency stop, which closes the session it ran under and
    says so in ``control_session``; the stop is never undone."""
    if not isinstance(rpc, AttributedApproachRpc):
        rpc = AttributedApproachRpc(rpc, model_id=model_id, reasoning_effort=reasoning_effort, client_id=client_id)
    result = await _approach_npc(rpc, name, execute=execute, **options)
    if execute and rpc.session_seen:
        try:
            active = bool(unwrap(await rpc("get_control_session_status", {})).get("active"))
            if active and rpc.opened_session and not rpc.lent_session:
                await rpc("close_control_session", {})
                active = False
            result["control_session"] = "left_open" if active else "closed"
        except MissionError as exc:
            result["control_session_error"] = str(exc)
        output = options.get("output")
        if output is not None:
            Path(output).mkdir(parents=True, exist_ok=True)
            (Path(output) / "result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result


async def _approach_npc(rpc: Rpc, name: str, *, output: Path, identity_check, execute: bool,
                       stop_distance_m: float = DEFAULT_STOP_DISTANCE_M, camera_follow: bool = True,
                       reset_spot: str | None = None, talk_spots: TalkSpots | None = None,
                       fast_follow: bool = False, smooth_camera: bool = False, sprint: bool = False,
                       pursuit: bool = False, hand_off_when_stuck: bool = False,
                       model_id: str | None = None, reasoning_effort: str | None = None,
                       client_id: str | None = None,
                       clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep) -> dict[str, Any]:
    """Approach, and if every in-place recovery fails, fast travel to
    ``reset_spot`` (the spot nearest the NPC) and approach once more.
    A disconnected corridor uses another catalogued entrance to the same
    area when one exists, rather than retrying the failed arrival floor.

    With ``hand_off_when_stuck``, an approach still stuck after that one
    reset, or one that cannot fast travel (no spot, or the game refuses),
    stops with state "stuck" and a stuck report (see stuck_report) for the
    agent to steer Cloud free by hand or try the fast travel again.
    """
    if not isinstance(rpc, AttributedApproachRpc):
        rpc = AttributedApproachRpc(rpc, model_id=model_id, reasoning_effort=reasoning_effort, client_id=client_id)
    run_started = clock()
    attempts: list[dict[str, Any]] = []

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        # Per-attempt reports stay in their own directories. The root report
        # accounts for the whole run, including failed attempts and travel.
        phases: dict[str, float] = {}
        for attempt in attempts:
            for phase, seconds in (attempt.get("phases_seconds") or {}).items():
                if phase != "total":
                    phases[phase] = round(phases.get(phase, 0.0) + seconds, 2)
        if "reset" in result:
            phases["reset"] = result["reset"]["seconds"]
        total = round(clock() - run_started, 2)
        phases["overhead"] = round(max(0.0, total - sum(phases.values())), 2)
        phases["total"] = total
        result["phases_seconds"] = phases
        if attempts[0].get("started_at_utc"):
            result["started_at_utc"] = attempts[0]["started_at_utc"]
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        return result

    async def hand_off(stuck: dict[str, Any]) -> dict[str, Any]:
        if hand_off_when_stuck:
            try:
                stuck["stuck"] = await stuck_report(rpc, stuck.get("target") or {}, reset_spot)
                stuck["state"] = "stuck"
            except MissionError as exc:
                stuck["stuck_report_error"] = str(exc)
        return finish(stuck)

    report = await _approach_once(rpc, name, output=output, identity_check=identity_check, execute=execute,
                                  stop_distance_m=stop_distance_m, camera_follow=camera_follow,
                                  talk_spots=talk_spots, fast_follow=fast_follow, smooth_camera=smooth_camera,
                                  sprint=sprint, pursuit=pursuit, clock=clock, sleep=sleep,
                                  travel_spot=reset_spot)
    attempts.append(report)
    if report.get("ok") or not execute or report.get("recovery_refused"):
        return finish(report)
    if str(report.get("reason") or "").startswith((*FIELD_BLOCKERS, "field_locomotion_unavailable", "npc_initial_travel_failed:", "npc_body_facing_not_ready")):
        return await hand_off(report)
    moogle = report.get("guide_moogle") or {}
    if has_player_position({"player": {"position": moogle.get("arrival_position")}}) and not moogle.get("error"):
        # Keep the verified zone arrival when capture/planning fails afterward.
        # A blind entrance reset undoes this ride and cannot fix bad topology.
        report["reset_suppressed"] = {"spot": reset_spot, "reason": "verified_guide_moogle_arrival"}
        return await hand_off(report)
    if not reset_spot:
        return await hand_off(report)
    # A fresh capture cannot connect underground and surface islands. Use
    # another entrance instead of sending Cloud back onto the failed floor.
    failures = [report.get("reason"), report.get("door_plan_error")]
    failures.extend(route.get("reason") for route in report.get("routes") or [])
    if any("no_linked_polygon_corridor" in str(reason) for reason in failures):
        from .qb_opponents import alternate_travel_spot_for
        try:
            alternate = alternate_travel_spot_for(name, reset_spot)
        except MissionError:
            alternate = None
        if alternate:
            report["disconnected_travel_spot"] = reset_spot
            reset_spot = alternate
    started = clock()
    try:
        travel = await travel_to_spot(rpc, reset_spot, sleep=sleep, clock=clock)
    except MissionError as exc:
        report["reset"] = {"spot": reset_spot, "error": str(exc), "seconds": round(clock() - started, 2)}
        return await hand_off(report)
    if not has_player_position({"player": {"position": travel.get("arrival_position")}}):
        report["reset"] = {
            "spot": reset_spot,
            "error": "reset_travel_arrival_position_unavailable",
            "seconds": round(clock() - started, 2),
            "arrival_position": travel.get("arrival_position"),
        }
        return await hand_off(report)
    reset_seconds = round(clock() - started, 2)
    retry = await _approach_once(rpc, name, output=output / "after-reset", identity_check=identity_check,
                                 execute=execute, stop_distance_m=stop_distance_m, camera_follow=camera_follow,
                                 talk_spots=talk_spots, fast_follow=fast_follow, smooth_camera=smooth_camera,
                                 sprint=sprint, pursuit=pursuit, clock=clock, sleep=sleep)
    attempts.append(retry)
    retry["before_reset"] = {key: report.get(key) for key in ("state", "reason", "detail", "phases_seconds",
                                                              "routes", "terrain_actions", "extrication",
                                                              "start_steps", "face_failed", "reapproach", "disconnected_travel_spot")}
    retry["reset"] = {"spot": reset_spot, "seconds": reset_seconds,
                      "arrival_position": travel.get("arrival_position")}
    if not retry.get("ok"):
        return await hand_off(retry)
    return finish(retry)


async def stuck_report(rpc: Rpc, target: dict[str, Any], reset_spot: str | None) -> dict[str, Any]:
    """What the agent needs to get Cloud unstuck when every route has failed.

    The agent then chooses: steer Cloud free by hand (short move_stick taps,
    a screenshot after each, until a route plans from where he stands), or
    the unstuck fast travel to ``reset_spot``, the last resort (the game
    can refuse fast travel, e.g. in some story sections).
    """
    record: dict[str, Any] = {"options": ["manual_escape", "unstuck_fast_travel"], "reset_spot": reset_spot}
    try:
        shot = unwrap(await rpc("capture_game_window", {}))
        record["screenshot"] = next((value for key, value in shot.items() if key.endswith("path")), None)
    except MissionError as exc:
        record["screenshot_error"] = str(exc)
    observed = unwrap(await rpc("observe_game", {}))
    feet = finite_position(((observed.get("player") or {}).get("locomotion") or {}).get("feet_position"))
    record["position"] = feet
    record["camera_yaw"] = float(((observed.get("camera") or {}).get("rotation") or {}).get("yaw") or 0.0)
    position = target.get("position") if isinstance(target.get("position"), dict) else None
    if position:
        record["target_distance_m"] = _horizontal_m(feet, position)
        record["target_bearing_degrees"] = round(math.degrees(math.atan2(
            position["y"] - feet["y"], position["x"] - feet["x"])), 1)
    record["clearance_cm"] = {str(round(angle)): round(value)
                              for angle, value in (await clearance(rpc, feet)).items()}
    return record


def catalogue_arrival_admission(name: str, resolved: dict[str, Any], source: str | None,
                                start: dict[str, float], target: dict[str, float]) -> dict[str, Any] | None:
    """Prefer a verified catalogue entrance over walking an unlocated target.

    A missing capture does not identify a region. Combined with absent live
    target evidence it means walking cannot currently be admitted; a catalogue
    entrance explicitly belonging to that target's field is a safe alternative.
    Never apply this policy to a live actor or guess an area from EndGame/XY.
    """
    if source != "opponent_catalogue_placement" or resolved.get("location_source") == "live_actor":
        return None
    if resolved.get("source") == "live_actor" or resolved.get("live_actor"):
        return None
    from .qb_opponents import opponent
    try:
        record = opponent(resolved.get("canonical_id") or name)
    except MissionError:
        return None
    spot = record.get("travel_spot") or {}
    if (not spot.get("key") or spot.get("same_area") is not True
            or not record.get("field") or spot.get("goto") != record["field"]):
        return None
    if capture_covers(start, target):
        return None
    return {"spot": spot["key"], "reason": "unlocated_catalogue_target_outside_admitted_capture",
            "target_field": record["field"], "destination_goto": spot["goto"],
            "target_source": source, "capture_covers_target": False}


async def _approach_once(rpc: Rpc, name: str, *, output: Path, identity_check, execute: bool,
                         stop_distance_m: float, camera_follow: bool,
                         clock: Callable[[], float], sleep, talk_spots: TalkSpots | None = None,
                         fast_follow: bool = False, smooth_camera: bool = False,
                         sprint: bool = False, pursuit: bool = False,
                         travel_spot: str | None = None, moogle_travel: bool = True,
                         max_start_distance_m: float | None = None) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    started = clock()
    mark = started
    active_phase = "resolve"
    phases: dict[str, float] = {}
    report: dict[str, Any] = {"npc": name, "execute": execute, "fast_follow": fast_follow, "sprint": sprint, "pursuit": pursuit,
                              "smooth_camera": smooth_camera, "phases_seconds": phases,
                              "started_at_utc": datetime.now(timezone.utc).isoformat()}

    def lap(phase: str, since: float) -> float:
        now = clock()
        phases[phase] = round(phases.get(phase, 0.0) + now - since, 2)
        return now

    def require_recoverable_route(route: dict[str, Any]) -> None:
        if route.get("recovery_refused"):
            detail = {key: route.get(key) for key in ("state", "reason", "identity_verification")}
            raise MissionError(str(route.get("reason") or "route_input_refused"),
                               response={"recovery_refused": True, "route": detail})

    try:
        if identity_check is not None:
            active_phase = "identity"
            # Initial travel and menu recovery precede NavigationMission.
            # Verify them too, before the first possible input.
            try:
                unwrap(await rpc("refresh_lifecycle_attachment", {}))
                identity = unwrap(await rpc("get_runtime_identity", {}))
                bootstrap = unwrap(await rpc("get_bootstrap_verification", {}))
                checked = identity_check(identity, bootstrap)
                if checked.get("ok") is not True:
                    raise MissionError("runtime_identity_mismatch")
            except Exception as exc:
                raise MissionError(str(exc) or type(exc).__name__,
                    response={"recovery_refused": True, "phase": "identity"}) from exc
            mark = lap("identity", mark)
            active_phase = "resolve"
        await wait_for_field(rpc, sleep=sleep, clock=clock)
        if execute:
            await close_main_menu(rpc, sleep=sleep)
        try:
            resolved = unwrap(await rpc("resolve_world_target", {"name": name}))
        except MissionError as exc:
            if input_recovery_refused(exc):
                raise
            resolved = exc.response if isinstance(exc.response, dict) else {"status": str(exc)}
        if resolved.get("status") == "known_but_not_currently_instantiated":
            # Not loaded yet (live: Dokaccha, 79 m from Costa del Sol's
            # arrival): head for where the level data places them; they load
            # on the way and Talk confirms the right person is reached.
            placed = catalogue_position(resolved.get("canonical_id") or name)
            if placed is not None:
                resolved = {**resolved, "status": "located", "position": placed}
                report["target_source"] = "opponent_catalogue_placement"
        elif resolved.get("status") != "located":
            # The resolver's identity index can know an NPC by label only:
            # shop staff (live 2026-09-30, Kalm's Npc1110_ItemShop) and card
            # players by actor name (live 2026-10-04, Riana's
            # Npc2110_CardGamer03: identity_label_only). Use the catalogue.
            from .qb_card_vendors import vendor_host
            from .qb_opponents import opponent
            try:
                record, source = opponent(name), "opponent_catalogue_placement"
            except Exception:
                try:
                    record, source = vendor_host(name), "card_vendor_catalogue_placement"
                except Exception:
                    record, source = None, None
            placed = catalogue_position(name) if record else None
            if placed is not None:
                resolved = {**resolved, "status": "located", "position": placed,
                            "canonical_id": record.get("canonical_id")}
                report["target_source"] = source
        if resolved.get("status") != "located":
            nearest = await nearest_interchangeable(rpc, resolved)
            if nearest is not None:
                resolved = nearest
                report["target_source"] = "nearest_interchangeable_actor"
        if resolved.get("status") != "located":
            raise MissionError(f"npc_not_located:{resolved.get('status')}", response=resolved)
        actor = resolved["position"]
        target = {"x": actor["x"], "y": actor["y"], "z": actor["z"] - CAPSULE_HALF_HEIGHT_CM}
        report["target"] = {"canonical_id": resolved.get("canonical_id"), "position": target}
        front = npc_front({"requested_name": name, **resolved}, target)
        if front:
            report["front_spots"] = front
        observation = await wait_for_field(rpc, sleep=sleep, clock=clock, require_locomotion=True)
        start = observation["player"]["locomotion"]["feet_position"]
        report["start_position"] = start
        report["start_distance_m"] = _horizontal_m(start, target)
        if max_start_distance_m is not None and report["start_distance_m"] > max_start_distance_m:
            raise MissionError("npc_beyond_reach", response={"target": report["target"],
                                                              "start_distance_m": report["start_distance_m"]})
        in_gold_saucer = moogle_travel and GOLD_SAUCER_FIELD in str(resolved.get("canonical_id") or "")
        # Inside the Gold Saucer a Moogle ride from where Cloud stands beats
        # fast travel to its entrance and a ride from there (live
        # 2026-10-05: 28 s of travel, then the entrance Moogle not loaded).
        arrival = (catalogue_arrival_admission(name, resolved, report.get("target_source"), start, target)
                   if execute and not in_gold_saucer else None)
        initial_spot = arrival["spot"] if arrival else travel_spot
        if (execute and initial_spot and (arrival or report["start_distance_m"] > TRAVEL_FIRST_M)
                and not (in_gold_saucer and worth_guide_moogle(start, target) is not None)):
            mark = lap("resolve", mark)
            active_phase = "travel_first"
            report["travel_first"] = {**(arrival or {}), "spot": initial_spot, "from_distance_m": round(report["start_distance_m"], 1)}
            try:
                travelled = await travel_to_spot(rpc, initial_spot, sleep=sleep, clock=clock)
            except MissionError as exc:
                # A failed initial travel is not a failed walking route. Its
                # confirmation/load may have happened; do not replay it as reset.
                raise MissionError(f"npc_initial_travel_failed:{exc}", response=exc.response) from exc
            report["travel_first"]["arrival_position"] = travelled.get("arrival_position")
            observation = await wait_for_field(rpc, sleep=sleep, clock=clock, require_locomotion=True)
            start = observation["player"]["locomotion"]["feet_position"]
            report["start_position"] = start
            report["start_distance_m"] = _horizontal_m(start, target)
            mark = lap("travel_first", mark)
            active_phase = "resolve"
        if execute and in_gold_saucer:
            destination = worth_guide_moogle(start, target)
            if destination is not None:
                mark = lap("resolve", mark)
                active_phase = "guide_moogle"
                try:
                    report["guide_moogle"] = await travel_by_guide_moogle(
                        rpc, destination.name, output=output, identity_check=identity_check, clock=clock,
                        sleep=sleep, talk_spots=talk_spots, pursuit=pursuit)
                    observation = await wait_for_field(rpc, sleep=sleep, clock=clock, require_locomotion=True)
                    start = observation["player"]["locomotion"]["feet_position"]
                    report["start_position"] = start
                    report["start_distance_m"] = _horizontal_m(start, target)
                except MissionError as exc:  # walk on instead
                    if input_recovery_refused(exc):
                        raise
                    report["guide_moogle"] = {"destination": destination.name, "error": str(exc)}
                mark = lap("guide_moogle", mark)
                active_phase = "resolve"
        if execute and (report.get("travel_first") or (report.get("guide_moogle") or {}).get("arrival_position")):
            mark = lap("resolve", mark)
            active_phase = "target_refresh"
            # A remote placement may now have a live actor. Refresh the same
            # identity after travel before choosing its front and camera aim.
            try:
                refreshed = unwrap(await rpc("resolve_world_target", {"name": resolved.get("canonical_id") or name}))
                report["target_refresh"] = {"status": refreshed.get("status"), "source": refreshed.get("location_source")}
                if refreshed.get("status") == "located":
                    actor = finite_position(refreshed.get("position"))
                    resolved = refreshed
                    target = {**actor, "z": actor["z"] - CAPSULE_HALF_HEIGHT_CM}
                    front = npc_front({"requested_name": name, **resolved}, target)
                    report["target"] = {"canonical_id": resolved.get("canonical_id"), "position": target}
                    report["front_spots"] = front
                    report["start_distance_m"] = _horizontal_m(start, target)
            except MissionError as exc:
                if input_recovery_refused(exc):
                    raise
                report["target_refresh_error"] = str(exc)
            finally:
                mark = lap("target_refresh", mark)
                active_phase = "resolve"
        if execute and ((observation.get("player") or {}).get("locomotion") or {}).get("grounded") is False:
            # Left swimming by an earlier approach: no route starts in water.
            swim = await leave_water(rpc, target, sleep=sleep)
            if swim is not None:
                report["start_swim"] = swim
                start = unwrap(await rpc("observe_game", {}))["player"]["locomotion"]["feet_position"]
                report["start_position"] = start
                report["start_distance_m"] = _horizontal_m(start, target)
        if execute:
            # Start the route validator's build now, while resolving and
            # planning, if the capture here has none yet.
            try:
                report["validator_prewarm"] = unwrap(await rpc("prewarm_navigation", {
                    "x": start["x"], "y": start["y"], "z": start.get("z"),
                    "target_x": target["x"], "target_y": target["y"], "target_z": target.get("z")}))
            except MissionError as exc:
                report["validator_prewarm"] = {"state": "failed", "detail": str(exc)[:200]}
        mark = lap("resolve", mark)
        active_phase = "plan_approach"

        def mission_config(goal: dict[str, float], camera_target: dict[str, float] | None = None) -> MissionConfig:
            # Mission goals use the actor-origin height convention.
            # Fast follow and sprint (feature-only, exclusive) swap the proven
            # pulse controller for continuous travel; the default stays pulse.
            # Sprint holds RT: live without it, 85 m at Costa del Sol took
            # 107 s of route time with no sprint setpoints at all.
            config = MissionConfig(execute=execute, camera_follow_enabled=camera_follow, capture_evidence=False,
                                   target_position=(goal["x"], goal["y"], goal["z"] + CAPSULE_HALF_HEIGHT_CM),
                                   target_map_id="EndGame", timeout_seconds=600.0,
                                   controller_mode="continuous" if fast_follow or sprint or pursuit else "pulse",
                                   # Pursuit falls back to fast follow.
                                   fast_follow=(fast_follow or pursuit) and not sprint, sprint=sprint,
                                   pursuit=pursuit,
                                   **({"normal_stick_magnitude": 0.8} if sprint else {}),
                                   camera_follow_smooth=camera_follow and (smooth_camera or (pursuit and camera_target is not None)),
                                   camera_follow_arrival_target=(camera_target["x"], camera_target["y"], camera_target["z"])
                                   if camera_follow and camera_target is not None else None)
            return replace(config, poll_seconds=min(config.poll_seconds, APPROACH_POLL_SECONDS))

        # One approach verifies the runtime once; each later route reuses
        # it only while its own fresh identity and game binding match.
        route_identity = VerifiedIdentityCache(identity_check) if identity_check is not None else None
        session_client = getattr(rpc, "client_id", None) or uuid.uuid4().hex

        canonical_id = resolved.get("canonical_id")
        known = talk_spots.get(canonical_id, target) if talk_spots is not None else None
        if known is not None and front and not in_front_of_npc(known, target, front[0]["facing_yaw_deg"]):
            report["talk_spot_behind_npc"] = known
            known = None
        if known is not None and not await asyncio.to_thread(capture_covers, start, known):
            # No capture covers Cloud and the spot, so a route there cannot
            # plan; plan the approach instead, which captures and stages
            # (live 2026-10-01: Gongaga's shop from its airstrip, 324 m).
            report["talk_spot_beyond_capture"] = known
            known = None
        async def plan(origin: dict[str, float] | None = None) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
            origin = origin or start
            try:
                # The bridge plans on the graph its routes already use.
                planned = unwrap(await rpc("plan_npc_approach", {"start": origin, "target": target,
                                                                 "stop_distance_m": stop_distance_m,
                                                                 **({"front": front} if front else {})}))
                return planned["library"], planned["approach"], planned.get("doors") or []
            except (MissionError, KeyError) as exc:
                if _coverage_missing(exc):
                    raise
            # Library entries are admitted under the adapter's pinned executable.
            from .live_route_adapter import LIVE_EXECUTABLE_SHA256
            graph, covering = await asyncio.to_thread(load_covering_graph, origin, target, LIVE_EXECUTABLE_SHA256)
            return covering, approach_point(graph, origin, target, stop_distance_m, front), []

        if known is not None:
            approach = {"position": known, "talk_spot": True,
                        "already_within_stop_distance": _horizontal_m(start, known) <= KNOWN_SPOT_REACHED_M}
            # Doors on the way, if any, are planned once a route to the spot
            # finds no corridor: planning first cost a graph build after
            # every fast travel (live at Kalm: the request timed out at 20 s
            # and took the runtime worker with it).
            library, door_chain = None, []
        else:
            async def plan_or_step_off() -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
                # A start with no linked corridor may stand where the navmesh
                # layers overlap or an island ends (off-mesh steps routes do
                # not take). Step off once, then plan from where he stands.
                nonlocal start
                try:
                    return await plan()
                except MissionError as exc:
                    if not execute or "no_linked_polygon_corridor" not in str(exc) or "npc_front_unreachable" in str(exc):
                        raise
                    report["stranded_step"] = await extricate(rpc, target, sleep=sleep)
                    if not report["stranded_step"].get("moved"):
                        raise
                start = unwrap(await rpc("observe_game", {}))["player"]["locomotion"]["feet_position"]
                report["start_position"] = start
                return await plan()

            async def stage() -> dict[str, Any] | None:
                try:
                    planned = unwrap(await rpc("plan_npc_approach", {"start": start, "target": target,
                                                                     "stop_distance_m": stop_distance_m,
                                                                     "stage": True}))
                    return planned.get("staging")
                except (MissionError, KeyError) as exc:
                    if NO_CAPTURE_COVERS_START in str(exc):
                        return None
                from .live_route_adapter import LIVE_EXECUTABLE_SHA256
                try:
                    graph, _ = await asyncio.to_thread(load_covering_graph, start, target, LIVE_EXECUTABLE_SHA256,
                                                       require_target=False)
                except MissionError:
                    return None
                return staging_point(graph, start, target)

            async def walk_leg(staged: dict[str, Any]) -> None:
                """Route to a staging point, then plan on from where Cloud stops."""
                nonlocal start
                await close_menu_if_open(rpc, sleep=sleep)
                await hold_control_session(rpc, session_client)
                legs = report.setdefault("staging_legs", [])
                before_m = _horizontal_m(start, target)
                leg = await NavigationMission(rpc, output / f"leg-{len(legs) + 1}", identity_check=route_identity, client_id=session_client,
                                              config=mission_config(staged["position"])).run()
                require_recoverable_route(leg)
                swim = None if leg.get("ok") else await leave_water(rpc, staged["position"], sleep=sleep)
                start = unwrap(await rpc("observe_game", {}))["player"]["locomotion"]["feet_position"]
                legs.append({**staged, "route": {key: leg.get(key) for key in ("ok", "state", "reason")},
                             "arrived_at": start, "arrived_remaining_m": round(_horizontal_m(start, target), 1),
                             **({"swim": swim} if swim is not None else {})})
                # A leg that stopped short still counts if it got Cloud nearer.
                if before_m - _horizontal_m(start, target) < MIN_STAGING_PROGRESS_M:
                    raise MissionError(f"npc_approach_staging_leg_stopped:{leg.get('reason') or leg.get('state')}")

            captured = False
            while True:
                try:
                    library, approach, door_chain = await (plan() if captured else plan_or_step_off())
                    break
                except MissionError as exc:
                    if not execute or not _coverage_missing(exc):
                        raise
                    failure = exc
                # Beyond every capture with Cloud: walk toward the NPC on the
                # one he stands in, if it gets him nearer.
                staged = await stage() if NO_CAPTURE_COVERS_ROUTE in str(failure) else None
                if staged is not None:
                    if len(report.get("staging_legs") or []) >= MAX_STAGING_LEGS:
                        raise MissionError("npc_approach_staging_legs_exhausted") from failure
                    await walk_leg(staged)
                    captured = False
                    continue
                if captured:
                    remember_failed_capture(target)
                    raise failure
                if capture_already_failed(target):
                    raise failure
                # No stored navmesh capture covers Cloud and the NPC (live: Zahira
                # from the Air-Raid Shelter): capture one here, then plan again.
                report["topology_capture"] = await capture_topology(rpc, target, sleep=sleep, clock=clock)
                report.setdefault("topology_captures", []).append(report["topology_capture"])
                captured = True
        report.update(library=library, approach=approach, door_chain=door_chain)
        mark = lap("plan_approach", mark)
        active_phase = "route"

        report["door_crossings"] = []

        async def cross_chain(doors: list[dict[str, Any]]) -> None:
            """Doors that split the navmesh between Cloud and the NPC: route to
            each one's front, open it, and go on from the far side."""
            for door in doors:
                await close_menu_if_open(rpc, sleep=sleep)
                await hold_control_session(rpc, session_client)
                index = len(report["door_crossings"]) + 1
                passed = []
                for leg_try in range(MAX_DOORS + 1):
                    leg = await NavigationMission(rpc, output / f"door-{index}-{leg_try + 1}",
                                                  identity_check=route_identity, client_id=session_client,
                                                  config=mission_config(door["front"])).run()
                    require_recoverable_route(leg)
                    await hold_control_session(rpc, session_client)
                    prompt = unwrap(await rpc("read_field_prompt", {}))
                    player = prompt.get("player_position") or {}
                    # Another door on the way to this one (the navmesh runs on
                    # through it): open it and route on to this door's front.
                    if (not leg.get("ok") and prompt.get("door") and player
                            and _horizontal_m(player, door["centre"]) > DOOR_REACH_M):
                        opened = unwrap(await rpc("open_door", {"observe_seconds": DOOR_OBSERVE_SECONDS}))
                        passed.append({"from": player, "prompt_cleared": opened.get("prompt_cleared")})
                        continue
                    break
                crossing = await cross_door(rpc, door, sleep=sleep)
                crossing["doors_on_the_way"] = passed
                crossing["route"] = {key: leg.get(key) for key in ("ok", "state", "reason")}
                report["door_crossings"].append(crossing)

        if execute and door_chain:
            active_phase = "doors"
            await cross_chain(door_chain)
            mark = lap("doors", mark)
            active_phase = "route"

        if not approach.get("already_within_stop_distance") or door_chain:
            route_config = mission_config(approach["position"], target)
            report["routes"] = []
            report["terrain_actions"] = []
            report["doors"] = []
            # A route that ends at a door it then opens is not a failed one.
            for route_number in range(MAX_ROUTE_ATTEMPTS + MAX_DOORS + 1):
                attempt = route_number - len(report["doors"])
                if attempt > MAX_ROUTE_ATTEMPTS:
                    break
                if attempt == MAX_ROUTE_ATTEMPTS:
                    # Every route and Terrain Action stopped short: step out
                    # of the spot, then one last route from there.
                    report["extrication"] = await extricate(rpc, target, sleep=sleep)
                    if not report["extrication"].get("moved"):
                        break
                if execute:
                    await close_menu_if_open(rpc, sleep=sleep)
                await hold_control_session(rpc, session_client)
                route_started = clock()
                route = await NavigationMission(rpc, output / f"route-{route_number + 1}", identity_check=route_identity, client_id=session_client,
                                                config=route_config).run()
                summary = {key: route.get(key) for key in ("ok", "state", "reason", "metrics", "identity_verification", "recovery_refused")}
                summary["seconds"] = round(clock() - route_started, 2)
                report["routes"].append(summary)
                report["route"] = summary
                require_recoverable_route(route)
                if not execute:
                    break
                prompt = unwrap(await rpc("read_field_prompt", {}))
                player = prompt.get("player_position") or {}
                distance = _horizontal_m(player, target) if player else None
                summary["distance_after_m"] = distance
                if route.get("ok") or (distance is not None and distance <= TALK_RANGE_M):
                    break
                swim = await leave_water(rpc, approach["position"], sleep=sleep)
                if swim is not None:
                    report.setdefault("swims", []).append({**swim, "after_route": route_number + 1})
                    if swim.get("landed"):
                        continue  # route on from the bank
                # A start just off the navmesh is refused before any movement,
                # and routing again from the same spot is refused the same way
                # (live after Michaela's match: 0.21 m off, three refusals, #172).
                # Step off first, then route on.
                if any(reason in str(route.get("reason") or "") for reason in UNRESOLVABLE_START_REASONS):
                    steps = report.setdefault("start_steps", [])
                    if len(steps) < MAX_START_STEPS:
                        step = await extricate(rpc, target, sleep=sleep)
                        steps.append({**step, "after_route": route_number + 1})
                        if step.get("moved"):
                            continue
                    # The start is still unusable. Do not run another cold
                    # mission (including the face recovery) from that spot.
                    # The caller can reset once or hand the attempt off.
                    raise MissionError(f"npc_approach_start_unresolvable:{route.get('reason')}")
                # A remembered talk spot just past the navmesh edge (Cloud's
                # feet stood there when Talk appeared) is refused before any
                # movement, and every route to it is refused the same way
                # (live at Nene, 2026-10-11 and -12: four refusals, a reset
                # and no Talk). Forget it and plan a spot on the navmesh.
                if approach.get("talk_spot") and TALK_SPOT_OFF_NAVMESH in str(route.get("reason") or ""):
                    report["talk_spot_off_navmesh"] = approach["position"]
                    if talk_spots is not None:
                        talk_spots.forget(canonical_id)
                    library, approach, replanned_doors = await plan(player or None)
                    report.update(library=library, replanned_approach=approach)
                    if replanned_doors:
                        report["door_chain"] = replanned_doors
                        await cross_chain(replanned_doors)
                        report["doors"].append({"after_route": route_number + 1, "planned": len(replanned_doors)})
                    route_config = mission_config(approach["position"], target)
                    continue
                # No corridor to a remembered talk spot: doors split the way.
                if ("no_linked_polygon_corridor" in str(route.get("reason") or "")
                        and not report["door_chain"] and player):
                    planned = None
                    try:
                        planned = unwrap(await rpc("plan_npc_approach", {"start": player, "target": target,
                                                                         "stop_distance_m": stop_distance_m,
                                                                         **({"front": front} if front else {})}))
                        report["door_chain"] = planned.get("doors") or []
                    except MissionError as exc:
                        report["door_plan_error"] = str(exc)
                    if report["door_chain"]:
                        await cross_chain(report["door_chain"])
                        report["doors"].append({"after_route": route_number + 1, "planned": len(report["door_chain"])})
                        continue
                    replanned = (planned or {}).get("approach")
                    if replanned and replanned.get("position") != approach.get("position"):
                        approach = replanned
                        route_config = mission_config(approach["position"], target)
                        report["replanned_approach"] = approach
                        continue
                    if not prompt.get("door") and PARKOUR_PROMPT not in (prompt.get("commands") or []):
                        raise MissionError("npc_approach_disconnected:no_linked_polygon_corridor")
                # Stopped at a door (the route's stall review found one ahead,
                # or the planner ran into it): open it at its prompt, then
                # route on from the far side.
                if prompt.get("door") and len(report["doors"]) < MAX_DOORS:
                    await hold_control_session(rpc, session_client)
                    opened = unwrap(await rpc("open_door", {"observe_seconds": DOOR_OBSERVE_SECONDS}))
                    moved = (opened.get("samples") or [{}])[-1].get("position") or {}
                    report["doors"].append({
                        "after_route": route_number + 1, "route_reason": route.get("reason"), "from": player,
                        "to": moved, "moved_m": _horizontal_m(player, moved) if player and moved else None,
                        "prompt_cleared": opened.get("prompt_cleared"),
                    })
                    continue
                # Stopped short. Where a replan could not get past something, a
                # Terrain Action (vault, climb, squeeze) often can: press it
                # when the game offers one, then route again from where Cloud
                # lands. Camera-follow has left the camera along the route.
                if PARKOUR_PROMPT in (prompt.get("commands") or []) and len(report["terrain_actions"]) < MAX_TERRAIN_ACTIONS:
                    await hold_control_session(rpc, session_client)
                    unwrap(await rpc("terrain_action", {"milliseconds": TERRAIN_ACTION_MS,
                                                        "settle_seconds": TERRAIN_ACTION_SETTLE_SECONDS}))
                    after = unwrap(await rpc("read_field_prompt", {}))
                    moved = after.get("player_position") or {}
                    report["terrain_actions"].append({
                        "after_route": attempt + 1, "from": player, "to": moved,
                        "moved_m": _horizontal_m(player, moved) if player and moved else None,
                    })
            mark = lap("route", mark)
            active_phase = "face"
            if not execute:
                route = report["routes"][-1]
                report.update(ok=bool(route.get("ok")) or route.get("state") == "PLAN_VALIDATED",
                              state="dry_run_route_validated")
                return report
            # A route that stopped short can still end within talking range;
            # the face phase decides by distance and the prompt.
        active_phase = "face"
        await hold_control_session(rpc, session_client)
        guide_moogle_target = None
        if name == GUIDE_MOOGLE:
            from .entity_interaction import world_identity
            live_actor = resolved.get("live_actor")
            guide_moogle_target = {
                "canonical_id": canonical_id,
                "actor_id": live_actor.get("id") if isinstance(live_actor, dict) else live_actor,
                "position": {**target, "z": target["z"] + CAPSULE_HALF_HEIGHT_CM},
                "map_id": observation.get("map_id"),
                "player_actor_id": (observation.get("player") or {}).get("actor_id"),
                "after_sequence": observation.get("sequence"),
                "after_source_sequence": observation.get("source_sequence", observation.get("sequence")),
                "world_identity": world_identity(observation),
            }
        try:
            try:
                report["face"] = await face_until_talk(rpc, {**target, "canonical_id":canonical_id}, sleep=sleep,
                    **({"guide_moogle_target": guide_moogle_target} if guide_moogle_target is not None else {}))
            except MissionError as exc:
                if (input_recovery_refused(exc) or str(exc) == "npc_body_facing_not_ready"
                        or guide_moogle_target is not None or not execute or approach.get("already_within_stop_distance")):
                    raise
                # Within range but no Talk (live: 4.4 m from Ned on the far
                # side of his table): one more route to the approach point
                # from here is far cheaper than a fast-travel reset.
                report["face_failed"] = {"reason": str(exc), "detail": getattr(exc, "response", None)}
                mark = lap("face", mark)
                active_phase = "reapproach"
                await close_menu_if_open(rpc, sleep=sleep)
                await hold_control_session(rpc, session_client)
                route_started = clock()
                route = await NavigationMission(rpc, output / "route-reapproach", identity_check=route_identity, client_id=session_client,
                                                config=route_config).run()
                report["reapproach"] = {key: route.get(key) for key in ("ok", "state", "reason")}
                require_recoverable_route(route)
                report["reapproach"]["seconds"] = round(clock() - route_started, 2)
                mark = lap("reapproach", mark)
                active_phase = "face"
                await hold_control_session(rpc, session_client)
                report["face"] = await face_until_talk(rpc, {**target, "canonical_id":canonical_id}, sleep=sleep,
                    **({"guide_moogle_target": guide_moogle_target} if guide_moogle_target is not None else {}))
        except BaseException:
            # A run that fails here may have input in flight: stop it. That is
            # an emergency stop, which closes the session. A run that ends
            # cleanly has released its input and leaves the session open.
            try:
                await rpc("stop_all_input", {})
            except MissionError:
                pass
            raise
        mark = lap("face", mark)
        active_phase = "finalize"
        if talk_spots is not None:
            try:
                feet = unwrap(await rpc("observe_game", {}))["player"]["locomotion"]["feet_position"]
                # Where Talk appeared after an off-navmesh spot, keep the
                # planned spot the route reached: those feet may be past the
                # edge again.
                spot = approach["position"] if report.get("talk_spot_off_navmesh") else feet
                talk_spots.record(canonical_id, target, spot)
                report["talk_spot_recorded"] = spot
            except (MissionError, KeyError, TypeError, OSError) as exc:
                report["talk_spot_error"] = str(exc)
        report.update(ok=True, state="talk_offered")
    except MissionError as exc:
        report.update(ok=False, state="stopped", reason=str(exc))
        if input_recovery_refused(exc):
            report["recovery_refused"] = True
        if getattr(exc, "response", None):
            report["detail"] = exc.response
    finally:
        now = clock()
        phases[active_phase] = round(phases.get(active_phase, 0.0) + now - mark, 2)
        phases["total"] = round(now - started, 2)
        (output / "result.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


async def run_http(name: str, output: Path, identity_check, **options: Any) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client("http://127.0.0.1:8765/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def rpc(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
                direct = operation in {"get_runtime_identity", "get_bootstrap_verification"}
                response = await session.call_tool(
                    operation if direct else "agent_call",
                    {} if direct else {"operation": operation, "arguments": arguments})
                if response.isError:
                    raise MissionError(str(response.content))
                value = response.structuredContent
                if value is None:
                    value = json.loads(next(item.text for item in response.content if item.type == "text"))
                return value

            zone = options.pop("gold_saucer_zone", None)
            if zone:
                return await ride_guide_moogle(rpc, zone, output=output, identity_check=identity_check,
                                               talk_spots=options.get("talk_spots"), pursuit=options.get("pursuit", False),
                                               model_id=options.get("model_id"), reasoning_effort=options.get("reasoning_effort"),
                                               client_id=options.get("client_id"))
            return await approach_npc(rpc, name, output=output, identity_check=identity_check, **options)


async def ride_guide_moogle(rpc: Rpc, zone: str, *, output: Path, identity_check, **options: Any) -> dict[str, Any]:
    """``travel_by_guide_moogle`` as a run: its report in output/result.json."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if not isinstance(rpc, AttributedApproachRpc):
        rpc = AttributedApproachRpc(rpc, model_id=options.pop("model_id", None),
            reasoning_effort=options.pop("reasoning_effort", None), client_id=options.pop("client_id", None))
    try:
        report = {"ok": True, "state": "arrived", **await travel_by_guide_moogle(
            rpc, zone, output=output, identity_check=identity_check, **options)}
    except MissionError as exc:
        report = {"ok": False, "state": "stopped", "reason": str(exc),
                  **(exc.response if isinstance(exc.response, dict) else {})}
    report["started_at_utc"] = started_at
    report["ended_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def resolve_reset_spot(name: str, requested: str | None) -> str | None:
    """The fast-travel spot to retry from: as given, or the catalogue's for ``name``."""
    if not requested or requested.casefold() == "none":
        return None
    if requested.casefold() != "auto":
        return requested
    from .qb_opponents import travel_spot_for
    try:
        spot = travel_spot_for(name)
    except Exception:  # not a catalogued opponent: perhaps a card vendor
        from .qb_card_vendors import vendor_host
        try:
            spot = (vendor_host(name) or {}).get("travel_spot")
        except Exception:
            return None
    return spot.get("key") if isinstance(spot, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-id", help="Exact client-reported model for a new control lease.")
    parser.add_argument("--reasoning-effort", help="Exact client-reported effort; requires --model-id.")
    parser.add_argument("--client-id", help="Explicit caller identity; may adopt only its already-owned active lease.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--name", help="The NPC's display name, e.g. Nene.")
    target.add_argument("--gold-saucer-zone",
                        help="Instead of an NPC: walk to the nearest Guide Moogle and ride it to this "
                             "Gold Saucer zone, e.g. 'Ghost Square' (needs --execute).")
    parser.add_argument("--execute", action="store_true", help="Move; default only validates the route.")
    parser.add_argument("--stop-distance", type=float, default=DEFAULT_STOP_DISTANCE_M)
    parser.add_argument("--no-camera-follow", action="store_true")
    parser.add_argument("--fast-follow", action="store_true",
                        help="Full-stick continuous route travel; with --no-pursuit, fast follow on its own.")
    parser.add_argument("--pursuit", action=argparse.BooleanOptionalAction, default=None,
                        help="Streamed pure-pursuit following (fast follow as fallback). On by default unless "
                             "--sprint is given; --no-pursuit selects the pulse controller.")
    parser.add_argument("--sprint", action="store_true",
                        help="Feature-only continuous route travel with sprint held; requires --feature-ref.")
    parser.add_argument("--snap-camera", action="store_true",
                        help="Stop to snap the camera round instead of turning it smoothly while moving.")
    parser.add_argument("--no-talk-spots", action="store_true", help="Ignore and do not record learned talk spots.")
    parser.add_argument("--reset-spot", default="auto",
                        help="Map spot to fast travel to and retry from if all else fails, e.g. Kalm; "
                             "'auto' (default) takes the opponent catalogue's nearest spot, 'none' disables.")
    parser.add_argument("--hand-off-when-stuck", action="store_true",
                        help="If still stuck after the one fast-travel reset (or unable to fast travel), stop "
                             "with state 'stuck' and a stuck report for the agent to steer Cloud free.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).resolve().parents[4] / "ProjectKnowledge" / "Current" / "canonical-project-state.json")
    parser.add_argument("--feature-ref")
    parser.add_argument("--expected-commit")
    parser.add_argument("--production-test-context", type=Path,
                        help="Explicit JSON feature-test context; emits one model-neutral development event after the verified NPC result.")
    parser.add_argument("--production-state-dir", type=Path,
                        help="Existing OBS monitor state directory (defaults to FF7R_PRODUCTION_STATE_DIR / FF7R_PUBLIC_STREAM_EVENT_DIR).")
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[4])
    args = parser.parse_args(argv)
    if (args.model_id is None) != (args.reasoning_effort is None):
        parser.error("--model-id and --reasoning-effort must be supplied together")
    if args.model_id is not None and (not args.model_id.strip() or not args.reasoning_effort.strip()):
        parser.error("model and effort must be nonempty")
    if args.client_id is not None and not args.client_id.strip():
        parser.error("client identity must be nonempty")
    if bool(args.feature_ref) != bool(args.expected_commit):
        parser.error("--feature-ref and --expected-commit must be provided together")
    if args.production_test_context is not None and not args.feature_ref:
        parser.error("--production-test-context requires --feature-ref and --expected-commit")
    from .production_events import (emit_development_report, read_recording_snapshot,
                                    state_dir_from_env)
    production_state = args.production_state_dir or state_dir_from_env()
    if args.production_test_context is not None and production_state is None:
        parser.error("--production-test-context requires --production-state-dir or an existing state-dir environment pointer")
    production_start = read_recording_snapshot(production_state) if args.production_test_context else None
    if args.pursuit is None:
        args.pursuit = not args.sprint
    if args.sprint and not args.feature_ref:
        parser.error("--sprint is a feature trial and requires --feature-ref and --expected-commit")
    if args.sprint and args.fast_follow:
        parser.error("--sprint and --fast-follow are exclusive")
    if args.pursuit and args.sprint:
        parser.error("--pursuit excludes --sprint")
    if args.gold_saucer_zone and not args.execute:
        parser.error("--gold-saucer-zone moves Cloud and requires --execute")
    check = (feature_identity_check(args.repository_root, args.feature_ref, args.expected_commit)
             if args.feature_ref else canonical_identity_check(args.manifest))
    output = args.output or Path(os.environ.get("LOCALAPPDATA", ".")) / "ff7r-agent" / "npc-approach" / datetime.now(timezone.utc).strftime("approach-%Y%m%dT%H%M%S.%fZ")
    result = asyncio.run(run_http(args.name, output, check, execute=args.execute,
                                  model_id=args.model_id, reasoning_effort=args.reasoning_effort, client_id=args.client_id,
                                  stop_distance_m=args.stop_distance, camera_follow=not args.no_camera_follow,
                                  reset_spot=resolve_reset_spot(args.name, args.reset_spot) if args.name else None,
                                  fast_follow=args.fast_follow, gold_saucer_zone=args.gold_saucer_zone,
                                  smooth_camera=not args.snap_camera, sprint=args.sprint, pursuit=args.pursuit,
                                  talk_spots=None if args.no_talk_spots else default_talk_spots(),
                                  hand_off_when_stuck=args.hand_off_when_stuck))
    if args.gold_saucer_zone and production_state is not None:
        # Public narration is a sparse fact emitted only after this route's
        # nested menu, observed arrival and settled-gameplay proof succeeded.
        try:
            from .live_narration_events import LiveNarrationEventSink, guide_moogle_arrival_facts
            facts = guide_moogle_arrival_facts(result)
            if facts:
                narrator = LiveNarrationEventSink(production_state)
                accepted = narrator.emit(
                    "navigating", facts,
                    source_key="guide-moogle:" + str(result.get("destination")) + ":" +
                    json.dumps(result.get("arrival_position"), sort_keys=True, separators=(",", ":")))
                if accepted:
                    narrator.flush(0.5)
                result["live_narration_event_status"] = narrator.status()
                narrator.close(0.5)
        except Exception:
            # Public narration is advisory and cannot alter the route outcome.
            pass
    if args.production_test_context is not None:
        result["output_directory"] = str(output.resolve())
        production_end = read_recording_snapshot(production_state)
        result["production_event_status"] = emit_development_report(
            state_dir=production_state, context_path=args.production_test_context, report=result,
            event_type=("npc_approach_outcome" if args.name else "guide_moogle_travel_outcome"),
            start_snapshot=production_start, end_snapshot=production_end)
    print(json.dumps({key: result.get(key) for key in ("ok", "state", "reason", "phases_seconds", "start_distance_m",
                                                       "reset", "stuck", "destination", "arrival_position",
                                                       "production_event_status", "live_narration_event_status")},
                     indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
