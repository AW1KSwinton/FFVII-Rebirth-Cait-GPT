"""Explore mode: look at the navmesh around the player, pick a point, walk there.

``explore_view`` (read-only) renders a top-down PNG of the navmesh round the
player with a labelled metre grid and a list of candidate destinations.
``run_explore_goto`` (the worker side of ``explore_goto_async``) walks to a
chosen point with the same pursuit route planner and follower that
``navigation_mission --pursuit`` uses, as one job inside the service: it
never opens or closes the control session and never latches the stop, so a
cutscene watcher that needs the session keeps working across it.

Both read the planning graph the route planner uses (the validated capture
library through ``npc_approach.load_covering_graph``); neither captures
anything. Where no capture covers the player, ``explore_view`` says so and
names the existing ``start_topology_capture`` operation.
"""
from __future__ import annotations

import math
import os
import tempfile
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import explore_map
from .conversation_state import dialogue_active, field_blocker, has_player_position
from .models import Vec3

DEFAULT_GRAPH_WAIT_SECONDS = 12.0
MAX_GRAPH_WAIT_SECONDS = 20.0
DEFAULT_GOTO_TIMEOUT_SECONDS = 300.0
MAX_GOTO_TIMEOUT_SECONDS = 900.0
DEFAULT_MAX_SNAP_M = 6.0
MAX_SNAP_M = 25.0
# The route's own arrival radius is 0.75 m; a fresh sample within this counts as there.
ARRIVAL_CONFIRM_M = 1.5
MAX_STORED_VIEWS = 8
MAX_POIS = 40
# Cloud's capsule half-height: an actor-origin point is this far above his feet.
ACTOR_ORIGIN_ABOVE_FEET_CM = 88.0
# A start this close to the captured floor (live: 0.07 m past an eroded edge at the Dustbowl
# fast-travel point, which the follower's cached plan refuses) gets a short step onto it first.
START_STEP_MAX_M = 0.75
START_STEP_INSET_CM = 40.0
START_STEP_TAPS = 3
START_STEP_TAP_MS = 250
START_STEP_MAGNITUDE = 0.5
START_STEP_SETTLE_SECONDS = 0.35
# Doors on the way (Nibelheim's inn, live 2026-10-14): the follower stops at a
# shown Open prompt with door_ahead, and a closed door across the planned path
# refuses the route as a static obstacle. Either way the walk opens it and
# routes on, for at most this many doors.
MAX_DOORS = 4
# A path through a doorway passes close to the door actor; a door beside the
# path (live: the inn room door off the upstairs landing, 1.8 m) is not on it.
DOOR_PATH_RADIUS_M = 0.9
DOOR_PATH_VERTICAL_M = 2.0
DOOR_APPROACH_M = 1.2
DOOR_TAPS = 6
DOOR_TAP_MS = 200
DOOR_TAP_SETTLE_SECONDS = 0.5
DOOR_OBSERVE_SECONDS = 12.0
# A link the follower could not cross with the stick alone (live, Mt. Nibel
# trail: a fallen log, step_over) is crossed by facing along it and pressing
# Terrain Action while the game offers it.
TERRAIN_FACE_TAP_MS = 150
TERRAIN_FACE_SETTLE_SECONDS = 0.6
TERRAIN_FACE_OFFSETS = (0.0, 30.0, -30.0, 60.0, -60.0)
TERRAIN_ACTION_MS = 200
TERRAIN_ACTION_SETTLE_SECONDS = 2.5
CAPTURE_HINT = {
    "operation": "start_topology_capture",
    "status_operation": "get_topology_capture_status",
    "arguments": {},
    "note": "a native capture takes minutes and sends no input; then call explore_view again",
}

NO_CAPTURE_REASON = "topology_not_captured_here"
GRAPH_BUILDING_REASON = "topology_graph_building"
LOAD_FAILED_REASON = "topology_load_failed"


class ExploreRefused(Exception):
    """A refusal that is an answer, not a fault: it becomes ``ok: false`` with ``reason``."""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details

    def as_response(self, operation: str) -> dict[str, Any]:
        return {"ok": False, "operation": operation, "reason": self.reason, "input_sent": False, **self.details}


# ------------------------------------------------------------------ helpers

def _number(values: Mapping[str, Any], key: str) -> float | None:
    value = values.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{key} must be a finite number")
    return float(value)


def _clamped(values: Mapping[str, Any], key: str, default: float, low: float, high: float) -> float:
    value = _number(values, key)
    if value is None:
        return default
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low:g} and {high:g}")
    return value


def _boolean(values: Mapping[str, Any], key: str, default: bool) -> bool:
    value = values.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"{key} must be a boolean")
    return value


def _field_blocker(state: Mapping[str, Any]) -> str | None:
    """Do not treat retained positions in a dead/stale feed as current field evidence."""
    blocker = field_blocker(state)
    if blocker:
        return blocker
    if state.get("running") is False or (state.get("telemetry") or {}).get("live") is False:
        return "field_telemetry_unavailable"
    return None


def player_pose(state: Mapping[str, Any]) -> tuple[Vec3, float | None, str]:
    """(feet position in cm, yaw degrees, which reference the position came from)."""
    player = state.get("player") or {}
    feet = (player.get("locomotion") or {}).get("feet_position")
    if feet:
        position, reference = Vec3.from_dict(feet), "feet"
    else:
        origin = Vec3.from_dict(player["position"])
        position, reference = Vec3(origin.x, origin.y, origin.z - ACTOR_ORIGIN_ABOVE_FEET_CM), "actor_origin_less_88cm"
    yaw = (player.get("rotation") or {}).get("yaw")
    return position, float(yaw) if isinstance(yaw, (int, float)) and math.isfinite(yaw) else None, reference


def _vec_dict(value: Vec3) -> dict[str, float]:
    return {"x": round(value.x, 1), "y": round(value.y, 1), "z": round(value.z, 1)}


def _view_directory(override: str | os.PathLike[str] | None = None) -> Path:
    if override:
        return Path(override)
    configured = os.environ.get("FF7R_EXPLORE_VIEW_DIR")
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) / "ff7r-cait-gpt" / "explore-views") if local else Path(tempfile.gettempdir()) / "ff7r-explore-views"


# ------------------------------------------------------------ graph loading

def _known_graphs(service: Any) -> dict[str, Any]:
    """Planning graphs this process already holds, by raw capture SHA-256 (as plan_approach_in_process)."""
    known: dict[str, Any] | None = getattr(service, "_approach_graphs", None)
    if known is None:
        known = service._approach_graphs = {}
    resources = getattr(service, "_validated_navigation_resources", None)
    provider = resources[1] if isinstance(resources, tuple) and len(resources) > 1 else None
    cached = getattr(provider, "_cached_state", None)
    if isinstance(cached, dict) and cached.get("graph") is not None and cached.get("raw_sha256"):
        known.setdefault(str(cached["raw_sha256"]).upper(), cached["graph"])
    prewarmed = getattr(provider, "prewarmed_graphs", None)
    for raw_sha, graph in (prewarmed(0.0) if callable(prewarmed) else {}).items():
        known.setdefault(str(raw_sha).upper(), graph)
    return known


def load_view_graph(service: Any, position: Mapping[str, float], target: Mapping[str, float] | None = None,
                    wait_seconds: float = DEFAULT_GRAPH_WAIT_SECONDS) -> tuple[Any, dict[str, Any]]:
    """The library capture covering ``position`` as a planning graph, with the library's decision.

    A graph not built yet is built on a background thread: this waits up to
    ``wait_seconds`` (the bridge's request deadline is 25 s, and a cold build
    took 9-15 s live), then raises ``topology_graph_building`` while the
    build carries on, so the next call finds it.
    """
    from .live_route_adapter import LIVE_EXECUTABLE_SHA256
    from .navigation_values import MissionError
    from .npc_approach import load_covering_graph

    known = _known_graphs(service)
    deadline = time.monotonic() + max(0.0, wait_seconds)

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    builds: dict[Any, Future] = getattr(service, "_explore_graph_builds", None) or {}
    service._explore_graph_builds = builds
    # Only join the same selection request: nearby points can be covered by
    # different captures, and a target can change the library's selection.
    key = (tuple(position[axis] for axis in "xyz"), tuple((target or position)[axis] for axis in "xyz"))
    future = builds.get(key)
    if future is None or (future.done() and future.exception() is not None):
        # One build at a time: a second one for the same capture would race it for the GIL.
        for other_key, other in list(builds.items()):
            if other_key != key and not other.done():
                try:
                    other.result(timeout=remaining())
                except FutureTimeout:
                    raise ExploreRefused(GRAPH_BUILDING_REASON, message="another navmesh graph is still being built; "
                                         "call again in a few seconds", retry_after_seconds=5) from None
                except Exception:
                    pass
        future = Future()
        builds[key] = future
        while len(builds) > 4:
            builds.pop(next(iter(builds)))

        def build() -> None:
            try:
                result = load_covering_graph(
                    dict(position), dict(target or position), LIVE_EXECUTABLE_SHA256, known_graphs=known,
                    require_target=False, build_missing=True)
                # Each graph holds a whole capture: keep the latest two, in the dict the service holds.
                kept = dict(list(known.items())[-2:])
                known.clear()
                known.update(kept)
                future.set_result(result)
            except BaseException as exc:  # reported through the future, never lost on the thread
                future.set_exception(exc)

        threading.Thread(target=build, name="ff7r-explore-graph", daemon=True).start()
    try:
        graph, decision = future.result(timeout=remaining())
    except FutureTimeout:
        raise ExploreRefused(GRAPH_BUILDING_REASON, message="the navmesh graph for this area is still being built; "
                             "call again in a few seconds", retry_after_seconds=5) from None
    except MissionError as exc:
        builds.pop(key, None)
        raise ExploreRefused(NO_CAPTURE_REASON, detail=str(exc), capture=dict(CAPTURE_HINT),
                             message="no admitted navmesh capture covers where the player stands") from exc
    except Exception as exc:
        builds.pop(key, None)
        raise ExploreRefused(LOAD_FAILED_REASON, detail=f"{type(exc).__name__}: {str(exc)[:300]}") from exc
    # Keep the graph by artifact SHA, but reselect/revalidate the covering
    # manifest next time; a completed future must not pin an old decision.
    builds.pop(key, None)
    return graph, decision


# --------------------------------------------------------------------- POIs

def collect_pois(service: Any, state: Mapping[str, Any], library: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Points of interest the harness already knows: nearby actors and interactables, landmarks, known doors."""
    found: list[dict[str, Any]] = []

    def add(kind: str, label: Any, position: Any, note: str | None = None) -> None:
        if isinstance(position, Mapping) and all(isinstance(position.get(axis), (int, float)) for axis in "xyz"):
            found.append({"kind": kind, "label": str(label) if label else kind, "position": dict(position), "note": note})

    for actor in state.get("nearby_actors") or []:
        if isinstance(actor, Mapping):
            add(str(actor.get("kind") or "actor"), actor.get("name") or actor.get("id"), actor.get("position"),
                actor.get("prompt"))
    for item in list(state.get("nearby_interactables") or []) + [state.get("nearby_interactable")]:
        if isinstance(item, Mapping):
            add(str(item.get("kind") or "interactable"), item.get("label") or item.get("id"), item.get("position"),
                item.get("prompt"))
    try:
        for landmark in service._runtime_landmarks(state.get("map_id")) or []:
            add("landmark", landmark.label, {"x": landmark.position.x, "y": landmark.position.y, "z": landmark.position.z})
    except Exception:
        pass
    try:
        from .door_links import default_door_links
        selected = (library or {}).get("selected")
        for link in default_door_links().for_topology(str(selected)) if selected else ():
            origin = ((link.get("bounds") or {}).get("origin")) or None
            points = list((link.get("points") or {}).values())
            if origin is None and points:
                origin = {axis: sum(float(point[axis]) for point in points) / len(points) for axis in "xyz"}
            add("door", link.get("actor") or "door", origin, "door joining two navmesh islands")
    except Exception:
        pass
    return found[:MAX_POIS]


# ------------------------------------------------------------------- views

def _register_view(service: Any, view_id: str, record: dict[str, Any]) -> None:
    views: OrderedDict[str, dict[str, Any]] = getattr(service, "_explore_views", None) or OrderedDict()
    views[view_id] = record
    while len(views) > MAX_STORED_VIEWS:
        views.popitem(last=False)
    service._explore_views = views
    service._explore_last_view_id = view_id


def explore_view(service: Any, radius_m: float = explore_map.DEFAULT_RADIUS_M, px_per_m: float | None = None,
                 center_x: float | None = None, center_y: float | None = None,
                 max_candidates: int = explore_map.DEFAULT_MAX_CANDIDATES, show_islands: bool = True,
                 include_pois: bool = True, wait_seconds: float = DEFAULT_GRAPH_WAIT_SECONDS,
                 output_dir: str | None = None) -> dict[str, Any]:
    """Read-only top-down navmesh view round the player (or ``center_x``/``center_y``, in cm).

    Sends no game input. Returns the PNG path, the pixel<->world transform,
    the player's position and facing, and numbered candidate destinations.
    """
    values = {"radius_m": radius_m, "px_per_m": px_per_m, "center_x": center_x, "center_y": center_y}
    radius = _clamped(values, "radius_m", explore_map.DEFAULT_RADIUS_M, explore_map.MIN_RADIUS_M, explore_map.MAX_RADIUS_M)
    requested_scale = _number(values, "px_per_m")
    if requested_scale is not None and not 0.5 <= requested_scale <= explore_map.MAX_PX_PER_M:
        raise ValueError(f"px_per_m must be between 0.5 and {explore_map.MAX_PX_PER_M:g}")
    centre_x, centre_y = _number(values, "center_x"), _number(values, "center_y")
    if (centre_x is None) != (centre_y is None):
        raise ValueError("give center_x and center_y together (centimetres)")
    wait = _clamped({"w": wait_seconds}, "w", DEFAULT_GRAPH_WAIT_SECONDS, 0.0, MAX_GRAPH_WAIT_SECONDS)
    show_islands = _boolean({"v": show_islands}, "v", True)
    include_pois = _boolean({"v": include_pois}, "v", True)
    try:
        state = service.observe()
        blocker = _field_blocker(state)
        if blocker or not has_player_position(state):
            raise ExploreRefused(blocker or "player_position_unavailable",
                                 message="no player position: a conversation, cutscene, menu or loading screen "
                                         "blanks the field feed; capture_game_window to see which")
        feet, yaw, reference = player_pose(state)
        centre = Vec3(centre_x if centre_x is not None else feet.x, centre_y if centre_y is not None else feet.y, feet.z)
        graph, library = load_view_graph(service, _vec_dict(feet), None, wait)
    except ExploreRefused as refusal:
        return refusal.as_response("explore_view")
    prewarm = None
    prewarmer = getattr(service, "prewarm_navigation", None)
    if callable(prewarmer):
        try:
            prewarm = prewarmer(feet.x, feet.y, z=feet.z)
        except Exception as exc:
            prewarm = {"state": "failed", "detail": str(exc)[:160]}
    pois = collect_pois(service, state, library) if include_pois else []
    analysis = explore_map.analyse_view(graph, feet, yaw, centre=centre, radius_m=radius,
                                        max_candidates=max_candidates, pois=pois)
    transform = explore_map.make_transform(centre.x, centre.y, radius, explore_map.choose_px_per_m(radius, requested_scale))
    drawn_pois = [poi for poi in pois if poi["kind"] not in ("door",)]
    png, facts = explore_map.render_view(graph, analysis, transform, radius_m=radius, show_islands=show_islands,
                                         extra_pois=drawn_pois)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = _view_directory(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"explore-{stamp}.png"
    path.write_bytes(png)
    view_id = f"view-{stamp}"
    public = explore_map.public_candidates(analysis)
    for entry, candidate in zip(public, analysis.candidates):
        px, py = transform.to_pixel(candidate["_point"].x, candidate["_point"].y)
        entry["pixel"] = {"px": round(px), "py": round(py), "in_view": 0 <= px < transform.width and 0 <= py < transform.height}
    _register_view(service, view_id, {
        "transform": transform, "map_id": state.get("map_id"), "created_at": time.time(),
        "candidates": {item["id"]: dict(item["position"]) for item in public},
    })
    player_px, player_py = transform.to_pixel(feet.x, feet.y)
    min_x, min_y, max_x, max_y = analysis.bounds_cm
    on_mesh = analysis.start_reference is not None
    response: dict[str, Any] = {
        "ok": True, "operation": "explore_view", "input_sent": False, "view_id": view_id,
        "image": {"path": str(path), "width": transform.width, "height": transform.height, "bytes": len(png)},
        "transform": transform.to_dict(),
        "grid": {"step_m": facts["grid_step_m"], "labels": "metres: world X along the top edge, world Y down the left edge"},
        "height_scale": {"min_m": round(facts["z_min_cm"] / 100.0, 1), "max_m": round(facts["z_max_cm"] / 100.0, 1),
                         "note": "colour bar bottom-right: blue low to yellow high across the player's own floors "
                                 "(floors beyond that range take the end colours); grey with red edges = "
                                 "other navmesh islands"},
        "legend": {"white dot + red arrow": "the player and the way he faces",
                   "green ring": "far_end", "orange ring": "capture_edge", "cyan diamond": "poi (and yellow: door)",
                   "red X": "other_island, not reachable on the mesh", "white outline": "edge of the player's island"},
        "map_id": state.get("map_id"), "sequence": state.get("sequence"),
        "player": {
            "position": _vec_dict(feet), "position_m": {k: round(v / 100.0, 2) for k, v in _vec_dict(feet).items()},
            "position_reference": reference, "yaw_deg": round(yaw, 1) if yaw is not None else None,
            "pixel": {"px": round(player_px), "py": round(player_py)},
            "on_navmesh": on_mesh, "route_start_ok": explore_map.route_start_ok(analysis.start_resolution),
            "island": analysis.start_label, "island_polygons": analysis.island_polygons,
            "start_resolution": analysis.start_resolution.reason_code,
        },
        "candidates": public,
        "islands": analysis.islands,
        "pois": [{k: v for k, v in item.items() if not k.startswith("_")} for item in analysis.pois],
        "capture_bounds_m": {"x": [round(min_x / 100.0, 1), round(max_x / 100.0, 1)],
                             "y": [round(min_y / 100.0, 1), round(max_y / 100.0, 1)]},
        "library": {key: (library or {}).get(key) for key in ("reason", "selected", "entries")},
        "prewarm": prewarm,
        "usage": ("Open image.path with your image reader. Pick a candidate id, or world coordinates (x, y in cm, "
                  "as observe_game) or a pixel (px, py) in this view, then explore_goto_async(candidate=ID or "
                  "x=,y= or px=,py=, view_id=this view_id, execute=true) and poll get_explore_goto_status."),
    }
    if not on_mesh:
        response["warning"] = ("the player is not on the captured navmesh (a prop, ledge, or an uncaptured area), so "
                               "no on-mesh destinations are offered; explore_goto would refuse this start")
    return response


# --------------------------------------------------------------- goto parse

def resolve_goto_target(service: Any, values: Mapping[str, Any], player_z_cm: float) -> dict[str, Any]:
    """The requested point (cm), whether its height was given, and where it came from."""
    forms = {
        "candidate": values.get("candidate") is not None,
        "xy": values.get("x") is not None or values.get("y") is not None or values.get("z") is not None,
        "xy_m": any(values.get(key) is not None for key in ("x_m", "y_m", "z_m")),
        "pixel": values.get("px") is not None or values.get("py") is not None,
    }
    used = [name for name, present in forms.items() if present]
    if len(used) != 1:
        raise ValueError("explore_goto needs exactly one target form: candidate, x/y(/z) in cm, x_m/y_m(/z_m) in "
                         "metres, or px/py from an explore_view image")
    form = used[0]
    if form == "xy":
        x, y, z = _number(values, "x"), _number(values, "y"), _number(values, "z")
        if x is None or y is None:
            raise ValueError("x and y are both required (centimetres)")
        return {"form": form, "x": x, "y": y, "z": z}
    if form == "xy_m":
        x, y, z = _number(values, "x_m"), _number(values, "y_m"), _number(values, "z_m")
        if x is None or y is None:
            raise ValueError("x_m and y_m are both required (metres)")
        return {"form": form, "x": x * 100.0, "y": y * 100.0, "z": z * 100.0 if z is not None else None}
    views: Mapping[str, Any] = getattr(service, "_explore_views", None) or {}
    view_id = values.get("view_id") or getattr(service, "_explore_last_view_id", None)
    view = views.get(view_id) if view_id else None
    if form == "candidate":
        candidate = values.get("candidate")
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            raise ValueError("candidate must be an integer id from explore_view")
        if view is None:
            raise ExploreRefused("explore_view_unknown", message="no such explore_view in this runtime (it may have "
                                 "restarted); call explore_view again, or give x/y in centimetres")
        position = view["candidates"].get(candidate)
        if position is None:
            raise ExploreRefused("candidate_unknown", message=f"view {view_id} has no candidate {candidate}",
                                 candidates=sorted(view["candidates"]))
        return {"form": form, "x": position["x"], "y": position["y"], "z": position["z"], "view_id": view_id,
                "candidate": candidate}
    px, py = _number(values, "px"), _number(values, "py")
    if px is None or py is None:
        raise ValueError("px and py are both required")
    given = values.get("transform")
    if given is not None:
        try:
            transform = explore_map.ViewTransform.from_dict(given)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    elif view is not None:
        transform = view["transform"]
    else:
        raise ExploreRefused("explore_view_unknown", message="a pixel target needs the view_id of a recent "
                             "explore_view, or its transform")
    x, y = transform.to_world(px, py)
    return {"form": form, "x": x, "y": y, "z": None, "view_id": view_id if given is None else None,
            "pixel": {"px": px, "py": py}}


# ----------------------------------------------------------- stop reporting

def classify_stop(state: Mapping[str, Any] | None, cutscene: Mapping[str, Any] | None,
                  route_reason: str | None, readiness_failures: list[str]) -> tuple[str, dict[str, Any]]:
    """Name why a walk stopped, with the route follower's reasons kept beside it.

    The route follower pauses on any readiness loss and gives up after its
    recovery window with ``readiness_recovery_timeout``; the fresh state
    decides which loss it was. Names follow the follower's readiness
    failures (``combat_active``, ``cutscene_active``, ``menu_or_pause``,
    ``loading_or_streaming``) and the field blockers (``dialogue_active``,
    ``field_telemetry_unavailable``).
    """
    evidence = {"route_reason": route_reason, "readiness_failures": list(readiness_failures)}
    if state is None:
        return "field_telemetry_unavailable", evidence
    if dialogue_active(state):
        return "dialogue_active", evidence
    if cutscene is not None and cutscene.get("active") is True:
        return "cutscene_active", evidence
    blocker = _field_blocker(state)
    if blocker:
        return blocker, evidence
    if state.get("in_combat"):
        return "combat_active", evidence
    if state.get("loading"):
        return "loading_or_streaming", evidence
    if state.get("in_menu") or state.get("paused"):
        return "menu_or_pause", evidence
    for name in readiness_failures:
        return name, evidence
    return route_reason or "route_stopped", evidence


def _readiness_failures(run: Mapping[str, Any] | None) -> list[str]:
    found: list[str] = []
    for event in (run or {}).get("timeline") or []:
        if isinstance(event, Mapping) and event.get("event") in ("readiness_loss_triggered", "readiness_recovery_failed"):
            for name in event.get("failures") or []:
                if name not in found:
                    found.append(str(name))
    return found


def _compact_route(result: Mapping[str, Any]) -> dict[str, Any]:
    """Count all attempts once, retaining bounded event evidence without timelines."""
    from .navigation_mission import route_metrics

    run = result.get("run") or {}
    summary: dict[str, Any] = {"route_state": run.get("state"), "route_reason": run.get("reason")}
    try:
        summary["metrics"] = route_metrics(dict(result))
    except Exception:
        summary["metrics"] = None
    attempts = result.get("route_attempts") or [{"run": run, "preflight": result.get("preflight")}]
    counts: dict[str, int] = {}
    crossings: list[dict[str, Any]] = []
    crossing_commands: deque[dict[str, Any]] = deque(maxlen=60)
    last_events: deque[dict[str, Any]] = deque(maxlen=8)
    last_gate_events: deque[dict[str, Any]] = deque(maxlen=8)
    movement_cycles = 0
    event_count = 0
    for index, attempt in enumerate(attempts, start=1):
        attempt_run = attempt.get("run") or {}
        movement_cycles += int(attempt_run.get("movement_cycles") or 0)
        # A run already includes its preflight timeline. A refused attempt
        # has no run: preserve its preflight events instead. The objective
        # timeline and result.run duplicate these and are not counted again.
        timeline = attempt_run.get("timeline") if attempt.get("run") is not None else (
            attempt.get("preflight") or {}).get("timeline")
        for item in timeline or []:
            if not isinstance(item, Mapping):
                continue
            event_count += 1
            event = item.get("event")
            record = {"event": event, "reason": item.get("reason"), "attempt_index": index}
            last_events.append(record)
            if event in ("traversal_crossing_started", "traversal_gimmick_engaged", "traversal_departure_aligned"):
                crossing_commands.append({"event": event, "t": item.get("t"), "attempt_index": index,
                                          "position": item.get("position")})
            elif event == "traversal_command":
                # Where each held push went: the evidence a failed crossing needs.
                position = item.get("position") or {}
                crossing_commands.append({
                    "t": round(float(item.get("t") or 0.0), 2), "sequence": item.get("sequence"),
                    "position": {axis: round(float(position.get(axis) or 0.0)) for axis in "xyz"},
                    "heading": round(float(item.get("heading_degrees") or 0.0), 1),
                    "camera_yaw": item.get("camera_yaw"), "actor_yaw": item.get("actor_yaw"),
                    "magnitude": item.get("magnitude")})
            if event == "traversal_crossing_finished":
                # A crossing's own outcome says why it stopped; keep it.
                evidence = item.get("evidence") or {}
                crossings.append({key: evidence.get(key) for key in (
                    "outcome", "failures", "detail", "elapsed_seconds", "departure_aligned", "commands",
                    "position_first", "position_last", "distance_to_end_m", "vertical_to_end_m")}
                    | {"link_class": (evidence.get("target") or {}).get("link_class"), "attempt_index": index})
            if isinstance(event, str):
                counts[event] = counts.get(event, 0) + 1
                if "gate" in event:
                    last_gate_events.append(record)
    # Keep gate evidence ahead of routine cycle names if the vocabulary grows
    # past the cap, and report the omitted counts explicitly.
    names = sorted(counts, key=lambda name: ("gate" not in name, name))[:40]
    summary["last_events"] = list(last_events)
    summary["last_gate_events"] = list(last_gate_events)
    summary["timeline_event_counts"] = {name: counts[name] for name in sorted(names)}
    summary["timeline_scope"] = "all_route_attempts"
    summary["timeline_event_count"] = event_count
    summary["timeline_event_kinds_omitted"] = len(counts) - len(names)
    summary["timeline_named_events_omitted"] = sum(counts.values()) - sum(counts[name] for name in names)
    summary["movement_cycles"] = movement_cycles
    if crossings:
        summary["traversal_crossings"] = crossings[-6:]
        summary["traversal_commands"] = list(crossing_commands)
    objective = result.get("navigation_objective") or {}
    summary["objective_state"] = objective.get("state")
    summary["recovery_attempts"] = objective.get("recovery_attempts")
    summary["route_attempt_count"] = len(attempts)
    preflight = result.get("preflight") or {}
    summary["preflight"] = {"movement_authorized": preflight.get("movement_authorized"),
                            "reason": preflight.get("reason"), "failures": preflight.get("failures")}
    return summary


# ------------------------------------------------------------- the walk job

def _lease_active(service: Any) -> bool:
    from .runtime_commands import _session_scope
    from .safety import SessionLease

    status = SessionLease(**_session_scope(service)).status()
    return bool(isinstance(status, Mapping) and status.get("active"))


def _run_route(service: Any, lifecycle: Any, values: dict[str, Any]) -> dict[str, Any]:
    from . import runtime_commands

    return runtime_commands._run_live_route_adapter(service, lifecycle, values)


def _fresh_state(service: Any) -> dict[str, Any] | None:
    try:
        return service.observe()
    except Exception:
        return None


def _fresh_cutscene(service: Any) -> dict[str, Any] | None:
    reader = getattr(service, "cutscene_state", None)
    try:
        return reader() if callable(reader) else None
    except Exception:
        return None


def _camera_yaw(state: Mapping[str, Any]) -> float | None:
    for source in (state.get("camera"), state.get("player")):
        yaw = ((source or {}).get("rotation") or {}).get("yaw")
        if isinstance(yaw, (int, float)) and math.isfinite(yaw):
            return float(yaw)
    return None


def step_onto_floor(service: Any, graph: Any, nearest: Any, state: Mapping[str, Any],
                    sleep: Any = None) -> tuple[dict[str, Any], Mapping[str, Any], Any]:
    """A few short taps from just off the captured floor onto it, so the route can start.

    Aims ``START_STEP_INSET_CM`` inside the nearest polygon, from its closest edge point
    toward its centre (a Detour polygon is convex), as ``npc_approach.step_onto`` does after a
    door. Re-reads the player after every tap and stops on a field blocker or a map change.
    Returns (report record, latest state, latest strict start resolution).
    """
    sleep = sleep or time.sleep
    edge = nearest.closest_point
    centre = graph.polygons[int(nearest.selected_reference)].centroid
    span = math.hypot(centre.x - edge.x, centre.y - edge.y)
    inset = min(START_STEP_INSET_CM, span / 2.0)
    aim = (Vec3(edge.x + (centre.x - edge.x) * inset / span, edge.y + (centre.y - edge.y) * inset / span, edge.z)
           if span > 1.0 else edge)
    record: dict[str, Any] = {"aim": _vec_dict(aim), "taps": 0, "on_navmesh": False}
    current: Mapping[str, Any] = state
    resolution = None
    for tap in range(START_STEP_TAPS + 1):
        blocker = _field_blocker(current) if has_player_position(current) else "player_position_unavailable"
        if blocker is None and current.get("map_id") != state.get("map_id"):
            blocker = "map_changed"
        if blocker:
            record["stopped_by"] = blocker
            break
        feet, _, _ = player_pose(current)
        resolution = explore_map.locate_player(graph, feet, snap_m=0.75, vertical_m=1.0)
        if explore_map.route_start_ok(resolution):
            record["on_navmesh"] = True
            break
        yaw = _camera_yaw(current)
        if tap == START_STEP_TAPS or yaw is None:
            if yaw is None:
                record["stopped_by"] = "camera_yaw_unavailable"
            break
        relative = math.radians(math.degrees(math.atan2(aim.y - feet.y, aim.x - feet.x)) - yaw)
        service.move(round(math.sin(relative) * START_STEP_MAGNITUDE, 3),
                     round(math.cos(relative) * START_STEP_MAGNITUDE, 3), START_STEP_TAP_MS)
        record["taps"] += 1
        sleep(START_STEP_SETTLE_SECONDS)
        current = service.observe()
    if has_player_position(current):
        feet, _, _ = player_pose(current)
        wide = explore_map.locate_player(graph, feet)
        record["position"] = _vec_dict(feet)
        record["nearest_polygon_m"] = round(wide.horizontal_distance_m, 2) if wide.ok else None
    return record, current, resolution


def pursuit_route_values(target: Vec3, *, execute: bool, timeout_seconds: float, max_cycles: int,
                         objective_id: str, manifest_path: str | None = None) -> dict[str, Any]:
    """The route request ``navigation_mission --pursuit`` builds for a coordinate goal."""
    from .continuous_control import FAST_FOLLOW_CAMERA_DEADBAND_DEG, FAST_FOLLOW_PROFILE
    from .controller_policy import LIVE_CONTROLLER_POLICY_INPUT_FIELDS, PROVEN_LIVE_CONTROLLER_POLICY

    policy = {field: PROVEN_LIVE_CONTROLLER_POLICY[field] for field in LIVE_CONTROLLER_POLICY_INPUT_FIELDS}
    values: dict[str, Any] = {
        "dry_run": not execute, "route_mode": "cached_topology",
        "route_target_position": {"x": target.x, "y": target.y, "z": target.z},
        "max_cycles": max_cycles if execute else 1,
        "cached_topology_wall_clock_seconds": min(600.0, timeout_seconds), **policy,
        "controller_mode": "continuous", "controller_policy_profile": FAST_FOLLOW_PROFILE, "sprint": False,
        # The camera follows the route smoothly, as on npc_approach's pursuit
        # routes; a walk recorded for footage otherwise looks the wrong way.
        "camera_follow_enabled": True, "camera_follow_smooth": True,
        "camera_follow_deadband_deg": FAST_FOLLOW_CAMERA_DEADBAND_DEG,
        "pursuit": True, "continuous_fast_follow": True,
        "navigation_objective_id": objective_id,
    }
    if manifest_path:
        values["topology_cache_manifest_path"] = manifest_path
    return values


GOTO_ARGUMENTS = frozenset({
    "candidate", "view_id", "x", "y", "z", "x_m", "y_m", "z_m", "px", "py", "transform",
    "execute", "max_snap_m", "timeout_seconds", "max_cycles", "allow_off_island", "topology_cache_manifest_path",
    "gimmick", "toward", "objective", "max_legs", "arrive_m",
})


def _attach_gimmicks(service: Any, graph: Any) -> dict[str, Any] | None:
    """Give the planning graph the scanned gimmicks (gimmick_links.py)."""
    from .gimmick_links import attach_gimmick_links

    path = getattr(service, "traversal_gimmick_evidence_path", None)
    if not callable(path):
        return None
    try:
        return attach_gimmick_links(graph, path())
    except (OSError, ValueError) as exc:
        graph.set_gimmick_links(())
        return {"attached": False, "reason": "evidence_unreadable", "error": str(exc)[:200]}


# Walk to this far short of a gimmick's departure: its trigger box starts the
# action (live at the Kalm crawl space, arriving on it started the crawl and
# the route's next gate refused the moving character).
GIMMICK_APPROACH_BACKOFF_CM = 120.0
# Already this near the departure, or carried along the gimmick, the probe
# starts straight away.
GIMMICK_UNDERWAY_M = 1.5
# Past the far end, off the navmesh, this many gimmick lengths along it.
GIMMICK_UNDERWAY_ALONG_MAX = 4.0


def _gimmick_underway(state: Mapping[str, Any], gimmick: Any, direction: str, graph: Any) -> bool:
    """Whether the player is at the gimmick's departure, in it, or carried on
    past it onto floor the navmesh does not cover (where the probe must go
    on: live at Kalm, past the crawl space's end)."""
    from .gimmick_probe import far_end
    from .traversal_crossing import along_and_lateral_m

    if not has_player_position(state):
        return False
    feet, _, _ = player_pose(state)
    departure, _ = gimmick.ends(direction)
    if math.hypot(feet.x - departure.x, feet.y - departure.y) / 100.0 <= GIMMICK_UNDERWAY_M:
        return True
    grounded = ((state.get("player") or {}).get("locomotion") or {}).get("grounded")
    along, lateral = along_and_lateral_m(departure, far_end(gimmick, direction), feet)
    if lateral > GIMMICK_UNDERWAY_M:
        return False
    if grounded is False and -0.2 <= along <= 1.2:
        return True
    off_mesh = not explore_map.route_start_ok(explore_map.locate_player(graph, feet, snap_m=0.75, vertical_m=1.0))
    return off_mesh and 0.8 <= along <= GIMMICK_UNDERWAY_ALONG_MAX


def _gimmick_target(service: Any, name: Any, feet: Vec3) -> tuple[dict[str, Any], Any, str]:
    """A point short of the named gimmick's nearer departure, as a goto target."""
    from .gimmick_links import load_evidence
    from .gimmick_probe import find_gimmick, nearer_direction

    path = service.traversal_gimmick_evidence_path()
    if not isinstance(name, str) or not name:
        raise ValueError("gimmick must be a scanned gimmick id or the end of one")
    try:
        gimmick = find_gimmick(load_evidence(path), name)
    except ValueError as exc:
        raise ExploreRefused("gimmick_unknown", message=str(exc)) from exc
    from .gimmick_probe import far_end

    direction = nearer_direction(gimmick, feet)
    departure, _ = gimmick.ends(direction)
    beyond = far_end(gimmick, direction)
    length = math.hypot(beyond.x - departure.x, beyond.y - departure.y) or 1.0
    back = GIMMICK_APPROACH_BACKOFF_CM / length
    return ({"form": "gimmick", "x": departure.x - (beyond.x - departure.x) * back,
             "y": departure.y - (beyond.y - departure.y) * back, "z": departure.z,
             "gimmick": gimmick.gimmick_id, "direction": direction}, gimmick, direction)


def _probe(service: Any, graph: Any, gimmick: Any, direction: str, report: dict[str, Any]) -> None:
    """Cross the gimmick and record its landing; fills the report's outcome."""
    from .gimmick_probe import probe_gimmick

    probe = probe_gimmick(service, graph, gimmick, direction, service.traversal_gimmick_evidence_path(),
                          pose=player_pose, camera_yaw=_camera_yaw, lease_active=lambda: _lease_active(service))
    report["gimmick_probe"] = probe
    report["input_sent"] = bool(report.get("input_sent")) or probe.get("pushes", 0) > 0
    landed = probe["outcome"] == "landed"
    report.update(ok=landed, outcome="crossed" if landed else "stopped",
                  reason="gimmick_crossed" if landed else probe.get("reason"))


def door_actors(service: Any) -> list[Vec3]:
    """Positions of the loaded levels' doors (SampleDoor field actions)."""
    scan = service.scan_actor_inventory()
    rows = ((scan or {}).get("snapshot") or {}).get("actors") or []
    doors = []
    for row in rows:
        position = row.get("position") if isinstance(row, Mapping) else None
        if "SampleDoor" in str(row.get("id") or row.get("class") or "") and isinstance(position, Mapping):
            doors.append(Vec3(float(position["x"]), float(position["y"]), float(position["z"])))
    return doors


def doors_on_path(waypoints: list[Vec3], doors: list[Vec3]) -> list[tuple[Vec3, Vec3]]:
    """The doors the path passes through (within DOOR_PATH_RADIUS_M), in path
    order, each with the point DOOR_APPROACH_M before it to walk to first."""
    found: list[tuple[float, Vec3]] = []
    walked = 0.0
    for start, end in zip(waypoints, waypoints[1:]):
        dx, dy = end.x - start.x, end.y - start.y
        length = math.hypot(dx, dy)
        for door in doors:
            t = 0.0 if length < 1.0 else max(0.0, min(1.0, ((door.x - start.x) * dx + (door.y - start.y) * dy) / length ** 2))
            closest = Vec3(start.x + dx * t, start.y + dy * t, start.z + (end.z - start.z) * t)
            if (math.hypot(door.x - closest.x, door.y - closest.y) <= DOOR_PATH_RADIUS_M * 100.0
                    and abs(door.z - closest.z) <= DOOR_PATH_VERTICAL_M * 100.0
                    and all(door is not seen for _, seen in found)):
                found.append((walked + t * length, door))
        walked += length
    found.sort(key=lambda item: item[0])
    return [(door, _point_along(waypoints, max(0.0, along - DOOR_APPROACH_M * 100.0))) for along, door in found]


def door_on_path(waypoints: list[Vec3], doors: list[Vec3]) -> tuple[Vec3, Vec3] | None:
    """The first door the path passes through, and where to stand before it."""
    found = doors_on_path(waypoints, doors)
    return found[0] if found else None


def _point_along(waypoints: list[Vec3], distance_cm: float) -> Vec3:
    for start, end in zip(waypoints, waypoints[1:]):
        length = math.hypot(end.x - start.x, end.y - start.y)
        if distance_cm <= length:
            t = 0.0 if length < 1.0 else distance_cm / length
            return Vec3(start.x + (end.x - start.x) * t, start.y + (end.y - start.y) * t,
                        start.z + (end.z - start.z) * t)
        distance_cm -= length
    return waypoints[-1]


def _read_prompt(service: Any) -> Mapping[str, Any]:
    from .field_prompt import read_field_prompt
    try:
        return read_field_prompt(service) or {}
    except Exception:
        return {}


def _open_shown_door(service: Any, record: dict[str, Any]) -> bool:
    opened = service.open_door(observe_seconds=DOOR_OBSERVE_SECONDS)
    record["opened"] = bool(opened.get("ok"))
    record["open_outcome"] = opened.get("outcome")
    return record["opened"]


def cross_door_on_way(service: Any, lifecycle: Any, graph: Any, target: Vec3, route_reason: str | None,
                      timeout: float, values: Mapping[str, Any]) -> dict[str, Any] | None:
    """Open the door that stopped or refused this walk; None when no door is to blame."""
    record: dict[str, Any] = {"route_reason": route_reason}
    prompt = _read_prompt(service)
    if prompt.get("door") and not prompt.get("talk"):
        record["found"] = "prompt_shown"
        _open_shown_door(service, record)
        return record
    if route_reason != "door_ahead" and "static_obstacle" not in str(route_reason or ""):
        return None
    state = service.observe()
    if not has_player_position(state) or _field_blocker(state):
        return None
    feet, _, _ = player_pose(state)
    plan = graph.plan(feet, target, max_snap_distance_m=0.75, max_vertical_separation_m=1.0)
    if plan.status != "complete" or len(plan.waypoints) < 2:
        return None
    candidates = doors_on_path([feet, *plan.waypoints], door_actors(service))
    if not candidates:
        return None
    record["found"] = "on_planned_path"
    record["skipped_open"] = []
    # A door already open shows no Open prompt: pass it and try the next one.
    for door, approach in candidates[:MAX_DOORS]:
        record.update(door=_vec_dict(door), approach=_vec_dict(approach))
        state = service.observe()
        if not has_player_position(state) or _field_blocker(state):
            break
        feet, _, _ = player_pose(state)
        if math.hypot(approach.x - feet.x, approach.y - feet.y) > 50.0:
            leg = _run_route(service, lifecycle, pursuit_route_values(
                approach, execute=True, timeout_seconds=timeout, max_cycles=int(values.get("max_cycles") or 2400),
                objective_id=f"explore-door:{door.x:.0f},{door.y:.0f},{door.z:.0f}",
                manifest_path=values.get("topology_cache_manifest_path")))
            record["approach_route"] = (leg.get("run") or {}).get("reason") or (leg.get("preflight") or {}).get("reason")
        if _tap_until_door_prompt(service, door, record):
            _open_shown_door(service, record)
            return record
        record["skipped_open"].append(_vec_dict(door))
    record.update(opened=False, open_outcome="no_door_prompt")
    return record


def terrain_action_on_way(service: Any, result: Mapping[str, Any]) -> dict[str, Any] | None:
    """Vault the traversal link the walk just failed to cross, when the game
    offers Terrain Action there; None when that is not what stopped it."""
    compact = _compact_route(result)
    crossings = compact.get("traversal_crossings") or []
    if not crossings or crossings[-1].get("outcome") != "traversal_link_not_crossed":
        return None
    prompt = _read_prompt(service)
    if not prompt.get("terrain_action"):
        return None
    headings = [c.get("heading") for c in compact.get("traversal_commands") or []
                if isinstance(c.get("heading"), (int, float))]
    state = service.observe()
    if not headings or not has_player_position(state) or _field_blocker(state):
        return None
    before, _, _ = player_pose(state)
    yaw = _camera_yaw(state)
    if yaw is None:
        return None
    heading = float(headings[-1])
    record: dict[str, Any] = {"found": "terrain_action_prompt", "link_class": crossings[-1].get("link_class"),
                              "heading_degrees": round(heading, 1), "from": _vec_dict(before)}
    # A short tap only turns Cloud; the press then crosses. The prompt depends
    # on his exact facing, which is not always the link's heading (live at
    # Mt. Nibel: shown at the heading only once he had settled), so it is
    # read after a settle, then at facings either side of the link.
    for offset in TERRAIN_FACE_OFFSETS:
        relative = math.radians(heading + offset - yaw)
        service.move(round(math.sin(relative), 3), round(math.cos(relative), 3), TERRAIN_FACE_TAP_MS)
        time.sleep(TERRAIN_FACE_SETTLE_SECONDS)
        if _read_prompt(service).get("terrain_action"):
            record["facing_offset_degrees"] = offset
            break
    else:
        record.update(opened=False, open_outcome="terrain_action_prompt_lost")
        return record
    service.terrain_action(milliseconds=TERRAIN_ACTION_MS, settle_seconds=TERRAIN_ACTION_SETTLE_SECONDS)
    after = service.observe()
    if has_player_position(after):
        now, _, _ = player_pose(after)
        record["to"] = _vec_dict(now)
        record["moved_m"] = round(math.hypot(now.x - before.x, now.y - before.y) / 100.0, 2)
    record["opened"] = bool(record.get("moved_m", 0) >= 0.5)
    record["open_outcome"] = "terrain_action_crossed" if record["opened"] else "terrain_action_no_progress"
    return record


# A squeeze through a narrow gap (live 2026-10-20, Mt. Nibel): Cloud is not
# grounded, his movement mode reads -1 (no standard mode) and he stands still
# until the stick is held; the walk stops there (pose gaps, not controllable).
SQUEEZE_PULSE_MS = 1500
SQUEEZE_MAX_PULSES = 12
SQUEEZE_MIN_PROGRESS_CM = 30.0


def in_squeeze(state: Mapping[str, Any]) -> bool:
    locomotion = (state.get("player") or {}).get("locomotion") or {}
    return (has_player_position(state) and locomotion.get("grounded") is False
            and locomotion.get("movement_mode") == -1 and not _field_blocker(state))


def squeeze_on_way(service: Any, result: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Carry Cloud through a squeeze the walk stopped in, pushing along his
    facing (the stick is admitted as it is while swimming) until he is
    grounded again; None when he is not in one."""
    state = service.observe()
    if not in_squeeze(state):
        return None
    start, _, _ = player_pose(state)
    record: dict[str, Any] = {"found": "squeeze", "from": _vec_dict(start), "pulses": 0}
    for _ in range(SQUEEZE_MAX_PULSES):
        before, yaw, _ = player_pose(state)
        camera = _camera_yaw(state)
        if yaw is None or camera is None:
            record.update(opened=False, open_outcome="squeeze_heading_unknown")
            return record
        relative = math.radians(yaw - camera)
        service.move(round(math.sin(relative), 3), round(math.cos(relative), 3), SQUEEZE_PULSE_MS, swimming=True)
        record["pulses"] += 1
        state = service.observe()
        if not has_player_position(state):
            continue
        now, _, _ = player_pose(state)
        if not in_squeeze(state):
            record.update(to=_vec_dict(now), opened=True, open_outcome="squeeze_crossed")
            return record
        if math.hypot(now.x - before.x, now.y - before.y) < SQUEEZE_MIN_PROGRESS_CM:
            record.update(to=_vec_dict(now), opened=False, open_outcome="squeeze_no_progress")
            return record
    record.update(opened=False, open_outcome="squeeze_not_finished")
    return record


def _tap_until_door_prompt(service: Any, door: Vec3, record: dict[str, Any]) -> bool:
    for tap in range(DOOR_TAPS + 1):
        prompt = _read_prompt(service)
        if prompt.get("door") and not prompt.get("talk"):
            record["taps"] = tap
            return True
        if tap == DOOR_TAPS:
            break
        state = service.observe()
        if not has_player_position(state) or _field_blocker(state):
            break
        feet, _, _ = player_pose(state)
        yaw = _camera_yaw(state)
        if yaw is None:
            break
        if math.hypot(door.x - feet.x, door.y - feet.y) < 60.0:
            break  # standing in the doorway: an open door
        relative = math.radians(math.degrees(math.atan2(door.y - feet.y, door.x - feet.x)) - yaw)
        service.move(round(math.sin(relative), 3), round(math.cos(relative), 3), DOOR_TAP_MS)
        time.sleep(DOOR_TAP_SETTLE_SECONDS)
    return False


def run_explore_goto(service: Any, lifecycle: Any, values: Mapping[str, Any]) -> dict[str, Any]:
    """Walk to the chosen point (``explore_goto_async``'s worker side). See the module docstring.

    Without ``execute`` it snaps the target, plans, and runs the route
    adapter's dry-run preflight (no input). Never opens or closes the control
    session and never calls the stop operation.
    """
    started = time.monotonic()
    unknown = sorted(str(key) for key in values if key not in GOTO_ARGUMENTS and not str(key).startswith("_"))
    if unknown:
        # A mistyped option (``units: "m"``) would otherwise be read as centimetres without a word.
        raise ValueError("unknown explore_goto argument(s): " + ", ".join(unknown)
                         + "; give metres as x_m/y_m(/z_m)")
    if _boolean(values, "toward", False) or values.get("objective") is not None:
        return run_explore_toward(service, lifecycle, values)
    if any(values.get(key) is not None for key in ("max_legs", "arrive_m")):
        raise ValueError("max_legs and arrive_m belong to toward=true")
    execute = _boolean(values, "execute", False)
    max_snap = _clamped(values, "max_snap_m", DEFAULT_MAX_SNAP_M, 0.5, MAX_SNAP_M)
    timeout = _clamped(values, "timeout_seconds", DEFAULT_GOTO_TIMEOUT_SECONDS, 10.0, MAX_GOTO_TIMEOUT_SECONDS)
    allow_off_island = _boolean(values, "allow_off_island", False)
    report: dict[str, Any] = {"operation": "explore_goto", "execute": execute, "input_sent": False,
                              "control_session": "untouched", "stop_latched": False}
    try:
        state = service.observe()
        if not has_player_position(state):
            raise ExploreRefused(_field_blocker(state) or "player_position_unavailable")
        blocker = _field_blocker(state)
        if blocker:
            raise ExploreRefused(blocker)
        feet, yaw, _ = player_pose(state)
        gimmick = None
        if values.get("gimmick") is not None:
            if any(values.get(key) is not None for key in ("candidate", "x", "y", "x_m", "y_m", "px", "py")):
                raise ValueError("gimmick is a target form of its own; give no other target")
            requested, gimmick, gimmick_direction = _gimmick_target(service, values.get("gimmick"), feet)
        else:
            requested = resolve_goto_target(service, values, feet.z)
        if requested.get("view_id"):
            view = service._explore_views[requested["view_id"]]
            if view.get("map_id") != state.get("map_id"):
                raise ExploreRefused("explore_view_map_changed", message="the player is on a different map; "
                                     "call explore_view again before choosing a destination")
        report["requested"] = {key: (round(value, 1) if isinstance(value, float) else value)
                               for key, value in requested.items()
                               if key in ("form", "x", "y", "z", "candidate", "view_id", "pixel", "gimmick", "direction")}
        if execute and not _lease_active(service):
            raise ExploreRefused("control_session_required",
                                 message="open_control_session first (with model_id and reasoning_effort)")
        graph, library = load_view_graph(service, _vec_dict(feet), {"x": requested["x"], "y": requested["y"],
                                                                    "z": requested["z"] if requested["z"] is not None else feet.z},
                                         wait_seconds=60.0)
        report["gimmicks"] = _attach_gimmicks(service, graph)
        underway = None
        if gimmick is not None and execute:
            from .gimmick_links import DIRECTION_FORWARD, DIRECTION_REVERSE
            # Carried past the far end, the far end is the nearer one: the
            # crossing under way decides the direction, not distance.
            underway = next((direction for direction in (gimmick_direction, (
                DIRECTION_REVERSE if gimmick_direction == DIRECTION_FORWARD else DIRECTION_FORWARD))
                if _gimmick_underway(state, gimmick, direction, graph)), None)
        if underway is not None:
            report["requested"]["direction"] = underway
            _probe(service, graph, gimmick, underway, report)
            report["elapsed_seconds"] = round(time.monotonic() - started, 2)
            return report
        labels = explore_map.island_labels(graph)
        resolution = explore_map.locate_player(graph, feet, snap_m=0.75, vertical_m=1.0)
        if not explore_map.route_start_ok(resolution):
            wide = explore_map.locate_player(graph, feet)
            nearest_m = round(wide.horizontal_distance_m, 2) if wide.ok else None
            steppable = (wide.ok and wide.reason_code != "point_inside_polygon" and wide.closest_point is not None
                         and wide.horizontal_distance_m <= START_STEP_MAX_M and (wide.vertical_distance_m or 0.0) <= 1.0)
            if not steppable:
                raise ExploreRefused("start_not_on_navmesh", message="the player is not inside the captured navmesh at his "
                                     "feet (a prop, ledge or uncaptured area); move to open floor first",
                                     nearest_polygon_m=nearest_m)
            if not execute:
                raise ExploreRefused("start_step_needed", message="the player stands just off the captured floor; "
                                     "with execute=true the walk first takes a short step onto it",
                                     nearest_polygon_m=nearest_m)
            step, state, resolution = step_onto_floor(service, graph, wide, state)
            report["start_step"] = step
            report["input_sent"] = step["taps"] > 0
            if not step["on_navmesh"]:
                raise ExploreRefused(step.get("stopped_by") or "start_not_on_navmesh",
                                     message="a short step did not bring the player onto the captured floor; "
                                     "move to open floor first", nearest_polygon_m=step.get("nearest_polygon_m"))
            feet, yaw, _ = player_pose(state)
        reachable = explore_map.reachable_references(graph, int(resolution.selected_reference), labels)
        snap = explore_map.snap_target(
            graph, Vec3(requested["x"], requested["y"], requested["z"] if requested["z"] is not None else feet.z),
            z_given=requested["z"] is not None, player=feet, reachable=reachable, labels=labels, max_snap_m=max_snap)
        if snap["status"] != "ok" and allow_off_island and snap.get("reason") == "target_on_unreachable_island":
            snap = explore_map.snap_target(
                graph, Vec3(requested["x"], requested["y"], requested["z"] if requested["z"] is not None else feet.z),
                z_given=requested["z"] is not None, player=feet, reachable=set(graph.polygons), labels=labels,
                max_snap_m=max_snap)
            snap.setdefault("warnings", []).append("allow_off_island: the planner decides whether it can get there")
        if snap["status"] != "ok":
            raise ExploreRefused(snap["reason"], **{k: v for k, v in snap.items() if k not in ("status", "reason")})
        target: Vec3 = snap["point"]
        report["target"] = {"position": _vec_dict(target), "polygon_ref": snap["reference"],
                            "snap_distance_m": snap["snap_horizontal_m"], "snap_vertical_m": snap["snap_vertical_m"],
                            "warnings": snap.get("warnings", [])}
        straight_m = round(math.hypot(target.x - feet.x, target.y - feet.y) / 100.0, 1)
        plan = graph.plan(feet, target, max_snap_distance_m=0.75, max_vertical_separation_m=1.0)
        report["plan"] = {"status": plan.status, "reason_code": plan.reason_code, "straight_line_m": straight_m,
                          "path_m": round(plan.total_estimated_path_length_m, 1)
                          if plan.total_estimated_path_length_m is not None else None}
        if plan.status != "complete" and not allow_off_island:
            raise ExploreRefused("no_corridor_to_target", message="the planner finds no corridor from here to that point",
                                 plan=report["plan"])
        route_values = pursuit_route_values(
            target, execute=execute, timeout_seconds=timeout, max_cycles=int(values.get("max_cycles") or 2400),
            objective_id=f"explore:{target.x:.0f},{target.y:.0f},{target.z:.0f}",
            manifest_path=values.get("topology_cache_manifest_path"))
        result = _run_route(service, lifecycle, route_values)
        doors: list[dict[str, Any]] = []
        while execute and len(doors) < MAX_DOORS:
            run = result.get("run") or {}
            if run.get("state") == "ARRIVED":
                break
            reason = run.get("reason") or (result.get("preflight") or {}).get("reason")
            crossing = squeeze_on_way(service, result)
            if crossing is None:
                crossing = cross_door_on_way(service, lifecycle, graph, target, reason, timeout, values)
            if crossing is None:
                crossing = terrain_action_on_way(service, result)
            if crossing is None:
                break
            doors.append(crossing)
            report["doors"] = doors
            report["input_sent"] = True
            if not crossing.get("opened"):
                break
            result = _run_route(service, lifecycle, route_values)
    except ExploreRefused as refusal:
        report.update(ok=False, outcome="stopped" if report["input_sent"] else "refused", reason=refusal.reason,
                      **refusal.details)
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        return report

    run = result.get("run") or {}
    preflight = result.get("preflight") or {}
    report.update(_compact_route(result))
    report["input_sent"] = report["movement_cycles"] > 0 or bool((report.get("start_step") or {}).get("taps"))
    if not preflight.get("movement_authorized"):
        stop, evidence = classify_stop(_fresh_state(service), _fresh_cutscene(service), preflight.get("reason"),
                                       list(preflight.get("failures") or []))
        report.update(ok=False, outcome="stopped" if report["input_sent"] else "refused",
                      reason=preflight.get("reason") or "route_preflight_refused",
                      stop_reason=stop, stop_evidence=evidence)
    elif not execute:
        validated = run.get("reason") == "control_cycle_bound_reached"
        report.update(ok=validated, outcome="dry_run_ok" if validated else "stopped",
                      reason="dry_run_validated" if validated else run.get("reason") or "dry_run_not_validated")
    else:
        final = _fresh_state(service)
        remaining_m = None
        vertical_m = None
        if final is not None and has_player_position(final):
            now, _, _ = player_pose(final)
            remaining_m = round(math.hypot(now.x - target.x, now.y - target.y) / 100.0, 2)
            vertical_m = round(abs(now.z - target.z) / 100.0, 2)
            report["final"] = {"position": _vec_dict(now), "distance_to_target_m": remaining_m,
                               "vertical_distance_to_target_m": vertical_m}
        if (run.get("state") == "ARRIVED" and remaining_m is not None and remaining_m <= ARRIVAL_CONFIRM_M
                and vertical_m is not None and vertical_m <= ARRIVAL_CONFIRM_M
                and final.get("map_id") == state.get("map_id")
                and final.get("running") is not False and (final.get("telemetry") or {}).get("live") is not False):
            report.update(ok=True, outcome="arrived", reason="arrived")
            if gimmick is not None:
                _probe(service, graph, gimmick, gimmick_direction, report)
        elif gimmick is not None and final is not None and _gimmick_underway(final, gimmick, gimmick_direction, graph):
            # The walk ran onto the trigger and the game took over: cross on.
            _probe(service, graph, gimmick, gimmick_direction, report)
        else:
            stop, evidence = classify_stop(final, _fresh_cutscene(service), run.get("reason"), _readiness_failures(run))
            if run.get("state") == "ARRIVED":
                stop = "arrival_not_confirmed"
            report.update(ok=False, outcome="stopped", reason=run.get("reason") or "route_not_arrived",
                          stop_reason=stop, stop_evidence=evidence)
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return report


# -- walking toward a far point ----------------------------------------------
# A capture covers only the navmesh the game has loaded round Cloud (live on
# Mt. Nibel, about 250 m), so a far goal is reached in legs: capture, take the
# reachable candidate nearest the goal in 3D (height matters: the bridge was
# 75 m up), walk there with the ordinary goto, repeat.
TOWARD_DEFAULT_LEGS = 10
TOWARD_MAX_LEGS = 20
TOWARD_ARRIVE_M = 8.0
TOWARD_MIN_GAIN_M = 3.0
TOWARD_STALL_LEGS = 3
TOWARD_REVISIT_CM = 300.0
TOWARD_VIEW_RADIUS_M = 120.0
TOWARD_CANDIDATES = 20
# How long a leg waits for a navmesh graph still being built.
TOWARD_GRAPH_WAIT_SECONDS = 240.0
# Where the captured navmesh ends short of the goal, a sprint toward it: the
# game hops down a ledge or vaults a wall by itself only at a sprint (owner,
# 2026-10-21; live, a Mt. Nibel cave ledge where the navmesh ended). It goes
# on when this gets Cloud this much nearer (live, scrapes of just over a metre
# along a wall counted and kept him sprinting into it), at most so many times
# a call.
TOWARD_SPRINT_MS = 1500
TOWARD_SPRINT_MIN_GAIN_M = 3.0
TOWARD_SPRINT_TRIES = 3
TOWARD_SPRINT_OFFSETS = (0.0, 30.0, -30.0)
TOWARD_BACK_STEP_MS = 400
# What a leg reports of each link crossing its walk tried.
TOWARD_CROSSING_FIELDS = ("link_class", "outcome", "detail", "departure_aligned", "distance_to_end_m",
                          "vertical_to_end_m", "position_first", "position_last")
TOWARD_LEG_STOPS = {"combat_active", "cutscene_active", "dialogue_active", "field_telemetry_unavailable",
                    "control_session_required", "game_paused", "menu_open"}


def _distance_3d_m(a: Vec3, b: Vec3) -> float:
    return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z)) / 100.0


def toward_goal(service: Any, values: Mapping[str, Any], player_z_cm: float) -> tuple[Vec3, dict[str, Any]]:
    """The goal: x/y(/z) in cm or metres, or the active objective's marker
    (``objective`` true or "main"), or its single active located sub-objective ("sub")."""
    which = values.get("objective")
    if which is None:
        requested = resolve_goto_target(service, values, player_z_cm)
        z = requested["z"] if requested["z"] is not None else player_z_cm
        return Vec3(requested["x"], requested["y"], z), {"form": requested.get("form")}
    if which not in (True, "main", "sub"):
        raise ValueError('objective must be true, "main" or "sub"')
    from .live_objective import get_active_objective
    objective = get_active_objective(service)
    position, source = objective.get("position"), {"form": "objective", "step_id": objective.get("step_id")}
    if which == "sub":
        subs = (objective.get("sub_objectives") or {}).get("items") or []
        active = [item for item in subs if item.get("state") == "active" and item.get("position")]
        if len(active) != 1:
            raise ExploreRefused("objective_sub_target_unavailable",
                                 message="no single active sub-objective with a located marker")
        position = active[0]["position"]
        source.update(form="sub_objective", sub_step_id=active[0].get("step_id"))
    elif not objective.get("ok"):
        raise ExploreRefused(objective.get("reason") or "objective_unavailable")
    if not isinstance(position, Mapping):
        raise ExploreRefused("objective_position_unavailable")
    return Vec3(float(position["x"]), float(position["y"]), float(position["z"])), source


def _capture_for(service: Any, lifecycle: Any, targets: list[Vec3]) -> dict[str, Any]:
    """A capture is admitted only with a validated route to a point inside
    the loaded area (live: a plain one found no destination). Try each target
    in turn, then a plain capture."""
    from .runtime_commands import _capture_topology_cache
    tried = []
    for target in [*targets, None]:
        request = {} if target is None else {"route_target_position": _vec_dict(target)}
        try:
            result = _capture_topology_cache(service, lifecycle, request)
        except Exception as exc:
            tried.append({"target": request.get("route_target_position"),
                          "error": f"{type(exc).__name__}: {exc}"[:160]})
            continue
        admitted = bool(result.get("cache_admitted"))
        tried.append({"target": request.get("route_target_position"), "admitted": admitted,
                      "reason": (result.get("preflight") or {}).get("reason")})
        if admitted:
            break
    return {"admitted": any(item.get("admitted") for item in tried), "attempts": tried}


def _leg_candidates(service: Any, feet: Vec3, yaw: float | None) -> list[dict[str, Any]]:
    """Reachable explore candidates round Cloud on the freshest graph.

    The walk runs as a job, not inside a bridge request, so it waits for a
    graph still being built (live 2026-10-21: a fresh capture's graph took
    longer than the 20 s a request allows, and every leg was refused)."""
    deadline = time.monotonic() + TOWARD_GRAPH_WAIT_SECONDS
    while True:
        try:
            graph, _ = load_view_graph(service, _vec_dict(feet), None, wait_seconds=MAX_GRAPH_WAIT_SECONDS)
            break
        except ExploreRefused as refusal:
            if refusal.reason != GRAPH_BUILDING_REASON or time.monotonic() >= deadline:
                raise
    analysis = explore_map.analyse_view(graph, feet, yaw, centre=feet, radius_m=TOWARD_VIEW_RADIUS_M,
                                        max_candidates=TOWARD_CANDIDATES)
    return [item for item in explore_map.public_candidates(analysis) if item.get("reachable")]


def choose_leg(candidates: list[dict[str, Any]], goal: Vec3, feet: Vec3, tried: list[Vec3]) -> dict[str, Any] | None:
    """The reachable candidate nearest the goal in 3D that is nearer than
    Cloud and not one already walked to."""
    here = _distance_3d_m(feet, goal)
    best = None
    for item in candidates:
        point = Vec3.from_dict(item["position"])
        if any(math.dist((point.x, point.y, point.z), (t.x, t.y, t.z)) < TOWARD_REVISIT_CM for t in tried):
            continue
        distance = _distance_3d_m(point, goal)
        if distance >= here - 1.0:
            continue
        if best is None or distance < best[0]:
            best = (distance, item)
    return None if best is None else best[1]


def _sprint_toward(service: Any, goal: Vec3) -> dict[str, Any]:
    """Bounded sprints at the goal; how much nearer they got Cloud.

    The stick is analogue and camera-relative, so each heading is aimed
    from the camera's yaw as it stands; the camera is not turned. A heading
    that gains too little is tried again to either side (live: a mako
    crystal, a plain static mesh, stood straight between Cloud and the
    spring)."""
    tries: list[dict[str, Any]] = []
    for offset in TOWARD_SPRINT_OFFSETS:
        state = service.observe()
        if not has_player_position(state) or _field_blocker(state):
            break
        feet, _, _ = player_pose(state)
        camera = _camera_yaw(state)
        if camera is None:
            tries.append({"offset_degrees": offset, "reason": "camera_yaw_unavailable"})
            break
        bearing = math.degrees(math.atan2(goal.y - feet.y, goal.x - feet.x)) + offset
        relative = math.radians(bearing - camera)
        stick = (round(math.sin(relative), 3), round(math.cos(relative), 3))
        try:
            # Movement calls must be this far apart (live, a sprint straight
            # after another movement call was refused).
            time.sleep(float(getattr(service, "_minimum_action_delay", 0.0) or 0.0) + 0.1)
            travel = service.travel_vector_chunk(*stick, maximum_ms=TOWARD_SPRINT_MS, sprint=True)
        except Exception as exc:
            tries.append({"offset_degrees": offset, "reason": f"{type(exc).__name__}: {exc}"[:160]})
            break
        after = service.observe()
        if not has_player_position(after):
            tries.append({"offset_degrees": offset, "reason": "player_position_unavailable"})
            break
        now, _, _ = player_pose(after)
        gained = round(_distance_3d_m(feet, goal) - _distance_3d_m(now, goal), 2)
        tries.append({"offset_degrees": offset, "to": _vec_dict(now), "gained_m": gained,
                      "dropped_m": round((feet.z - now.z) / 100.0, 2),
                      "travel": {key: (travel or {}).get(key) for key in
                                 ("outcome", "keys", "actual_hold_ms", "horizontal_displacement", "rejection_reason")}})
        if gained >= TOWARD_SPRINT_MIN_GAIN_M:
            break
        # Off whatever stopped it before the next heading: pressed against a
        # crystal, every heading was blocked; one step back, 25 degrees off
        # cleared it (live, the Mt. Nibel spring).
        try:
            time.sleep(float(getattr(service, "_minimum_action_delay", 0.0) or 0.0) + 0.1)
            service.travel_vector_chunk(-stick[0], -stick[1], maximum_ms=TOWARD_BACK_STEP_MS, sprint=False)
        except Exception as exc:
            tries[-1]["back_step"] = f"{type(exc).__name__}: {exc}"[:160]
    best = max((item.get("gained_m", 0.0) for item in tries), default=0.0)
    return {"ok": best >= TOWARD_SPRINT_MIN_GAIN_M, "input_sent": any("to" in item for item in tries),
            "gained_m": best, "tries": tries}


def run_explore_toward(service: Any, lifecycle: Any, values: Mapping[str, Any]) -> dict[str, Any]:
    """Walk toward a far goal in legs (``explore_goto_async(toward=true, ...)``).

    Stops on arrival (within ``arrive_m``), when a leg stops for combat, a
    scene, dialogue or a lost feed (deal with it, then call again), or after
    TOWARD_STALL_LEGS legs gaining under TOWARD_MIN_GAIN_M. Where nothing on
    the navmesh is nearer and the goal itself is not reached, it sprints at
    the goal (a ledge the game hops down only at a sprint) and goes on if
    that got Cloud nearer.
    """
    started = time.monotonic()
    execute = _boolean(values, "execute", False)
    legs_allowed = int(_clamped(values, "max_legs", TOWARD_DEFAULT_LEGS, 1, TOWARD_MAX_LEGS))
    arrive_m = _clamped(values, "arrive_m", TOWARD_ARRIVE_M, 1.5, 50.0)
    leg_values = {key: values[key] for key in ("max_snap_m", "timeout_seconds", "max_cycles") if key in values}
    report: dict[str, Any] = {"operation": "explore_toward", "execute": execute, "input_sent": False,
                              "control_session": "untouched", "stop_latched": False, "legs": []}
    try:
        state = service.observe()
        if not has_player_position(state) or _field_blocker(state):
            raise ExploreRefused(_field_blocker(state) or "player_position_unavailable")
        feet, yaw, _ = player_pose(state)
        goal, source = toward_goal(service, values, feet.z)
        report["goal"] = {**source, "position": _vec_dict(goal)}
        report["start_distance_m"] = round(_distance_3d_m(feet, goal), 1)
        if execute and not _lease_active(service):
            raise ExploreRefused("control_session_required",
                                 message="open_control_session first (with model_id and reasoning_effort)")
        tried: list[Vec3] = []
        best, stalls, sprints = None, 0, 0
        for _ in range(legs_allowed if execute else 1):
            state = service.observe()
            if not has_player_position(state) or _field_blocker(state):
                raise ExploreRefused(_field_blocker(state) or "player_position_unavailable")
            feet, yaw, _ = player_pose(state)
            distance = _distance_3d_m(feet, goal)
            if distance <= arrive_m:
                report.update(ok=True, outcome="arrived", reason="arrived")
                break
            if best is not None and distance > best - TOWARD_MIN_GAIN_M:
                stalls += 1
                if stalls >= TOWARD_STALL_LEGS:
                    raise ExploreRefused("toward_no_progress",
                                         message=f"{TOWARD_STALL_LEGS} legs without getting "
                                                 f"{TOWARD_MIN_GAIN_M:g} m nearer")
            else:
                stalls = 0
            best = distance if best is None else min(best, distance)
            leg: dict[str, Any] = {"from": _vec_dict(feet), "distance_m": round(distance, 1)}
            report["legs"].append(leg)
            try:
                near = choose_leg(_leg_candidates(service, feet, yaw), goal, feet, tried)
            except ExploreRefused as refusal:
                # No admitted capture covers Cloud (live: every capture
                # stopped being selectable when the route checker changed):
                # take one, then look again.
                if not execute or refusal.reason != NO_CAPTURE_REASON or "capture" in leg:
                    raise
                leg["capture"] = _capture_for(service, lifecycle, [goal])
                near = choose_leg(_leg_candidates(service, feet, yaw), goal, feet, tried)
            if execute and near is None and sprints < TOWARD_SPRINT_TRIES:
                # Where the navmesh ends short of the goal, a sprint at it
                # (2 s) before a capture (minutes): live, the ledges down to
                # the Mt. Nibel mako spring. After a capture the same sprint
                # moved nothing.
                sprints += 1
                leg["sprint"] = _sprint_toward(service, goal)
                report["input_sent"] = report["input_sent"] or bool(leg["sprint"].get("input_sent"))
                if leg["sprint"].get("gained_m", 0.0) >= TOWARD_SPRINT_MIN_GAIN_M:
                    continue
            if execute and near is None and "capture" not in leg:
                # A capture costs minutes and leaves a graph to build: take
                # one only when the graph in hand offers no nearer point
                # (live 2026-10-21, every leg captured first, about 4 min each).
                leg["capture"] = _capture_for(service, lifecycle, [goal])
                near = choose_leg(_leg_candidates(service, feet, yaw), goal, feet, tried)
            if near is None:
                # Candidates are the far ends of the walkable area; the goal
                # itself may be on it (live: 17.9 m from the elevator, no
                # candidate nearer). Walk to it directly once.
                walk = run_explore_goto(service, lifecycle, {"x": goal.x, "y": goal.y, "z": goal.z,
                                                             "execute": execute, **leg_values})
                leg["to"] = {"position": _vec_dict(goal), "kind": "goal"}
                leg["walk"] = {key: walk.get(key) for key in ("ok", "outcome", "reason", "stop_reason", "final")
                               if walk.get(key) is not None}
                report["input_sent"] = report["input_sent"] or bool(walk.get("input_sent"))
                if walk.get("ok"):
                    report.update(ok=True, outcome="arrived" if execute else "dry_run_ok",
                                  reason="arrived" if execute else walk.get("reason"))
                    break
                raise ExploreRefused("toward_no_candidate_nearer",
                                     message="no reachable point on the captured navmesh is nearer the goal, "
                                             "and the goal itself was not reached")
            point = Vec3.from_dict(near["position"])
            tried.append(point)
            leg["to"] = {"position": _vec_dict(point), "kind": near.get("kind"),
                         "goal_distance_m": round(_distance_3d_m(point, goal), 1)}
            walk_values = {"x": point.x, "y": point.y, "z": point.z, "execute": execute, **leg_values}
            walk = run_explore_goto(service, lifecycle, walk_values)
            if walk.get("reason") == "cache_manifest_stat_changed":
                # A capture just rewrote the cache under the walk's snapshot.
                walk = run_explore_goto(service, lifecycle, walk_values)
            leg["walk"] = {key: walk.get(key) for key in ("ok", "outcome", "reason", "stop_reason", "final", "doors")
                           if walk.get(key) is not None}
            crossings = [{key: item.get(key) for key in TOWARD_CROSSING_FIELDS if item.get(key) is not None}
                         for item in walk.get("traversal_crossings") or [] if isinstance(item, Mapping)]
            if crossings:
                leg["walk"]["crossings"] = crossings
            report["input_sent"] = report["input_sent"] or bool(walk.get("input_sent"))
            if not execute:
                report.update(ok=bool(walk.get("ok")), outcome="dry_run_ok" if walk.get("ok") else "stopped",
                              reason=walk.get("reason"))
                break
            if walk.get("stop_reason") in TOWARD_LEG_STOPS:
                raise ExploreRefused(str(walk["stop_reason"]), message="a leg stopped; deal with it and call again")
        else:
            if execute:
                raise ExploreRefused("toward_leg_limit", message=f"{legs_allowed} legs walked; call again to go on")
    except ExploreRefused as refusal:
        report.update(ok=False, outcome="stopped" if report["input_sent"] else "refused", reason=refusal.reason,
                      **refusal.details)
    final = _fresh_state(service)
    if final is not None and has_player_position(final) and report.get("goal"):
        now, _, _ = player_pose(final)
        report["final_distance_m"] = round(_distance_3d_m(now, Vec3.from_dict(report["goal"]["position"])), 1)
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return report
