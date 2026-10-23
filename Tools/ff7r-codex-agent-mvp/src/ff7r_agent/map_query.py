"""On-demand area-map reading: pins, cursor and map point -> world target.

Nothing here runs in the background.  Each call writes one
``map-query.request``; the exporter's ``map_query.lua`` answers it once with
``map-query.json`` (only while the area map is open) and then idles again.

Resolved points are kept as short-lived *map targets* bound to the map they
were read on.  A map target is plain world coordinates: it is what the route
follower consumes (``navigate_to_map_target`` / ``--map-target-id``).  It never
authorizes movement on its own.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Iterable, Mapping

from .map_projection import (
    MapProjection,
    build_projection,
    distance_agreement,
    parse_distance_text,
    widget_local,
)
from .navigation_values import MissionError

REQUEST_NAME = "map-query.request"
RESPONSE_NAME = "map-query.json"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REQUEST_LIFETIME_SECONDS = 30
# Live answers take 1-2 s (the sampler serves requests on its own tick) and
# spike past 3 s, which failed field-prompt reads; 8 s stays well inside
# the worker's 20 s request limit.
DEFAULT_TIMEOUT_SECONDS = 8.0
# How often the response file is checked (a stat): the exporter now answers
# on the next sample, so a coarse poll was a visible share of each read.
POLL_SECONDS = 0.05
STALE_TELEMETRY_WAIT_SECONDS = 3.0
TARGET_TTL_SECONDS = 1800.0
MAX_TARGETS = 4096
# A live actor this close (horizontally) to an NPC pin lends it its height.
ACTOR_HEIGHT_MATCH_RADIUS_CM = 400.0
# Displayed icon distances are rounded and may be 3D; allow generous slack.
DISTANCE_CHECK_MIN_M = 10.0
DISTANCE_CHECK_FRACTION = 0.1
TARGET_ID = re.compile(r"[0-9a-f]{16}\Z")
CATEGORIES = ("fast_travel", "npc", "quest", "user_pin", "enemy", "other", "unclassified")
SPACES = ("map_local", "screen", "cursor")

# EEndMenuMapIconKind, in declaration order (FF7R2UProj/Source/EndGame/Public/
# EEndMenuMapIconKind.h), so a numeric property value maps to its name.
ICON_KINDS = (
    "PartyMember Enemy MainStory SubStory Elevator Up Down Shop Location Inn InnInTown "
    "MainStoryIntermediate SideDestination VendingMachine WeaponShop MateriaShop Chadley "
    "ChocoboCarriageOld VRBattle Colosseum MogShop PHS CorneoTreasure Jukebox RoadClosed "
    "UserMarker CommunicationTowerBefore CommunicationTowerAfter CommunicationTowerClear "
    "CommunicationTowerComplete CommunicationTowerBefore2 SubjugationPointBefore "
    "SubjugationPointAfter SubjugationPointClear SubjugationPointComplete SubjugationPointBefore2 "
    "LifeSpotBefore LifeSpotAfter LifeSpotClear LifeSpotComplete LifeSpotBefore2 StoryPointBefore "
    "StoryPointAfter StoryPointClear StoryPointComplete StoryPointBefore2 ChocoboFootTraceBefore "
    "ChocoboFootTraceAfter ChocoboFootTraceClear ChocoboFootTraceComplete ChocoboFootTraceBefore2 "
    "ChocoboFeedingGroundBefore ChocoboFeedingGroundAfter ChocoboFeedingGroundClear "
    "ChocoboFeedingGroundComplete ChocoboFeedingGroundBefore2 SummonMonsterSmallShrineBefore "
    "SummonMonsterSmallShrineAfter SummonMonsterSmallShrineClear SummonMonsterSmallShrineComplete "
    "SummonMonsterSmallShrineBefore2 CraftRecipeExcavatePointBefore CraftRecipeExcavatePointAfter "
    "CraftRecipeExcavatePointClear CraftRecipeExcavatePointComplete CraftRecipeExcavatePointBefore2 "
    "ChocoboCarriageBefore ChocoboCarriageAfter ChocoboCarriageClear ChocoboCarriageComplete "
    "ChocoboCarriageBefore2 GatheringGrass GatheringGrassRare GatheringStones GatheringStonesRare "
    "GatheringWood GatheringWoodRare GatheringOthers GatheringOthersRare GatheringPowerUp Town "
    "PlaceName TownBefore PlaceNameBefore BookShop BulletinBoardWithQuest BulletinBoardNoQuest "
    "SubjugationPointRareBefore SubjugationPointRareAfter SubjugationPointRareClear "
    "SubjugationPointRareComplete SubjugationPointRareBefore2 MiniGameR1 MiniGameR1Cleared "
    "MiniGameR2 MiniGameR2Cleared MiniGameR3 MiniGameR3Cleared MiniGameR4 MiniGameR4Cleared "
    "MiniGameR5 MiniGameR5Cleared MiniGameR6 MiniGameR6Cleared ChocoboStable GatheringMetal "
    "GatheringMetalRare LikabilityLow LikabilityMiddle LikabilityHigh SubStoryActive Flag "
    "BookVendingMachine ChangingRoom MiniGame TalkLikabilityLow TalkLikabilityMiddle "
    "TalkLikabilityHigh LookingForLove HubDestination Port PointWarp PointExchangeShop "
    "LifeStreamGimmick GravityGimmick Ropeway Buggy Ship ChocoboCarriage JumpStand QuestAfter "
    "SubStoryIntermediate SwimsuitShop TelephoneBox Heliport Likability1 Likability2 Likability3 "
    "Likability4 Likability5 Likability6 Likability7 TalkLikability1 TalkLikability2 "
    "TalkLikability3 TalkLikability4 TalkLikability5 TalkLikability6 TalkLikability7 SBScooter "
    "JumpStandDisable PhotoCleared LocationPoint Treasure Piano ChocoboCaptureStone "
    "ChocoboCaptureGreens MiniGameR7 MiniGameR7Cleared MiniGameR8 MiniGameR8Cleared MiniGameR9 "
    "MiniGameR9Cleared MiniGameR10 MiniGameR10Cleared Talk PhotoModeFilter Chewing PhotoSpot "
    "TreasureClear PhotoSpotClear SubStoryMemo SubStoryChocobo SubStoryMemoActive "
    "SubStoryChocoboActive SubStoryLifeSpot SubStoryLifeSpotActive MiniGameR11 MiniGameR11Cleared "
    "VendingMachineInTown BookVendingMachineInTown ZipLine VendingMachinesAndBench Bench "
    "PianoMiniGame TreasureBefore ChocoboShop PianoStool CampPoint CardShop SubStorySuspended "
    "PhotoModeFilterClear ItemAnimalClear LikabilityCaitSith MiniGameWithMission "
    "PianoMiniGameWithMission TreasurePort TreasurePortClear TreasurePortBefore"
).split()
# Textures whose meaning DefaultEndMenu.ini's ButtonMappings name directly.
KNOWN_TEXTURES = {
    "u_icon_mainstory_goal": "MainStory",
    "u_icon_mainstory_pass": "MainStoryIntermediate",
    "u_icon_subquest_goal": "SubStory",
    "u_icon_subquest_pass_01": "SubStoryIntermediate",
    "u_icon_side_goal": "SideDestination",
}
# Unlocked fast-travel points. "…Before" states are not yet travel targets.
FAST_TRAVEL_KINDS = frozenset({
    "ChocoboCarriage", "ChocoboCarriageOld", "ChocoboCarriageAfter", "ChocoboCarriageClear",
    "ChocoboCarriageComplete", "ChocoboStable", "PointWarp",
})
NPC_KINDS = frozenset({
    "PartyMember", "Chadley", "Talk", "LookingForLove", "LikabilityCaitSith",
    "Shop", "WeaponShop", "MateriaShop", "MogShop", "CardShop", "ChocoboShop", "SwimsuitShop",
    "BookShop", "PointExchangeShop", "Inn", "InnInTown",
    *(f"Likability{s}" for s in ("Low", "Middle", "High", "1", "2", "3", "4", "5", "6", "7")),
    *(f"TalkLikability{s}" for s in ("Low", "Middle", "High", "1", "2", "3", "4", "5", "6", "7")),
})
QUEST_KINDS = frozenset({
    "MainStory", "MainStoryIntermediate", "SubStory", "SubStoryActive", "SubStoryIntermediate",
    "SubStoryMemo", "SubStoryMemoActive", "SubStoryChocobo", "SubStoryChocoboActive",
    "SubStoryLifeSpot", "SubStoryLifeSpotActive", "SubStorySuspended", "SideDestination",
    "HubDestination", "QuestAfter", "BulletinBoardWithQuest",
    "StoryPointBefore", "StoryPointAfter", "StoryPointClear", "StoryPointComplete", "StoryPointBefore2",
})
USER_PIN_KINDS = frozenset({"UserMarker", "Flag"})
_NORMALISED_KINDS = sorted(((re.sub(r"[^a-z0-9]", "", k.lower()), k) for k in ICON_KINDS),
                           key=lambda item: -len(item[0]))


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def category_for(kind: str | None) -> str:
    if kind is None:
        return "unclassified"
    if kind in FAST_TRAVEL_KINDS:
        return "fast_travel"
    if kind in NPC_KINDS:
        return "npc"
    if kind in QUEST_KINDS:
        return "quest"
    if kind in USER_PIN_KINDS:
        return "user_pin"
    if kind == "Enemy" or kind.startswith("SubjugationPoint"):
        return "enemy"
    return "other"


def classify_icon(record: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return (EEndMenuMapIconKind name, evidence source) for an icon record.

    A reflected kind property is authoritative.  Texture names are the
    fallback: exact ButtonMappings textures first, then the longest kind name
    contained in the texture's asset name.
    """
    properties = record.get("properties") if isinstance(record.get("properties"), Mapping) else {}
    for name in ("IconKind", "MapIconKind", "Kind", "IconType"):
        value = properties.get(name)
        if not isinstance(value, str) or not value:
            continue
        stripped = value.rsplit("::", 1)[-1]
        if stripped.isdigit() and int(stripped) < len(ICON_KINDS):
            return ICON_KINDS[int(stripped)], f"property:{name}"
        if stripped in ICON_KINDS:
            return stripped, f"property:{name}"
    for image in record.get("images") or []:
        if not isinstance(image, str):
            continue
        asset = image.rsplit(".", 1)[-1].lower()
        if asset in KNOWN_TEXTURES:
            return KNOWN_TEXTURES[asset], "texture:button_mappings"
    for image in record.get("images") or []:
        if not isinstance(image, str):
            continue
        asset = _normalise(image.rsplit(".", 1)[-1])
        for normalised, kind in _NORMALISED_KINDS:
            if len(normalised) >= 4 and normalised in asset:
                return kind, "texture:name_match"
    return None, None


# ---------------------------------------------------------------------------
# Request / response transport


def _telemetry_directory(service: Any) -> Path:
    path = getattr(getattr(service, "telemetry", None), "path", None)
    if path is None:
        raise MissionError("map_query_live_sampler_required")
    return Path(path).parent


def file_signature(path: Path) -> tuple[int, int, int] | None:
    """(file id, mtime, size) of a response file, read without opening it.

    The sampler replaces its response by delete-and-rename, which Windows
    refuses while a reader holds the file open; polling it open ten times a
    second lost answers outright (live: map_query_timeout, trace "written
    ok=true result=false"). A stat opens no data handle, so poll that and
    open the file only once it has changed.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    # Each response is a new file renamed into place, so its id changes even
    # when a same-sized answer lands within one clock tick.
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


def _read_response(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_RESPONSE_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def request_snapshot(service: Any, *, heights: bool = True,
                     timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                     flags: Iterable[str] = (), require_map: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ask the exporter for one map snapshot; returns (response, base sample).

    ``flags`` adds sampler options, e.g. ``alltexts`` for every visible text.
    ``require_map=False`` skips the map checks for a read of screen texts
    only: a shop's screen carries no map identity (live 2026-09-30, Kalm's
    item shop).
    """
    if not isinstance(timeout_seconds, (int, float)) or not 0.5 <= timeout_seconds <= 15:
        raise MissionError("map_query_timeout_invalid")
    base = service.observe()
    # A sample can age past the live bound for a moment when the machine is
    # busy (live: map_query_telemetry_stale in the middle of a fast travel);
    # wait briefly for a fresh one before refusing.
    sleeper = getattr(service, "_sleeper", time.sleep)
    stale_deadline = time.monotonic() + STALE_TELEMETRY_WAIT_SECONDS
    while not (base.get("telemetry") or {}).get("live") and time.monotonic() < stale_deadline:
        sleeper(POLL_SECONDS)
        base = service.observe()
    if not (base.get("telemetry") or {}).get("live"):
        raise MissionError("map_query_telemetry_stale")
    if require_map and not base.get("map_id"):
        raise MissionError("map_query_map_identity_missing")
    directory = _telemetry_directory(service)
    request_path = directory / REQUEST_NAME
    response_path = directory / RESPONSE_NAME
    request_id = uuid.uuid4().hex
    content = "|".join([request_id, str(int(time.time()) + REQUEST_LIFETIME_SECONDS),
                        "snapshot", ",".join((["heights"] if heights else []) + list(flags)) or "-"])
    lock = getattr(service, "_lock", None)
    seen = file_signature(response_path)
    with lock if lock is not None else nullcontext():
        temporary = request_path.with_name(REQUEST_NAME + "." + uuid.uuid4().hex)
        try:
            temporary.write_text(content, encoding="ascii")
            os.replace(temporary, request_path)
        finally:
            temporary.unlink(missing_ok=True)
    sleeper = getattr(service, "_sleeper", time.sleep)
    deadline = time.monotonic() + float(timeout_seconds)
    try:
        while True:
            signature = file_signature(response_path)
            payload = None
            if signature is not None and signature != seen:
                seen = signature
                payload = _read_response(response_path)
            if payload is not None and payload.get("request_id") == request_id:
                break
            if time.monotonic() >= deadline:
                raise MissionError("map_query_timeout")
            sleeper(POLL_SECONDS)
    finally:
        with lock if lock is not None else nullcontext():
            try:
                if request_path.read_text(encoding="ascii").split("|")[0] == request_id:
                    request_path.unlink()
            except (FileNotFoundError, OSError, UnicodeError):
                pass
    if payload.get("schema_version") != 1 or payload.get("source") != "ue4ss_map_query":
        raise MissionError("map_query_response_invalid")
    if type(payload.get("source_sequence")) is not int:
        raise MissionError("map_query_response_invalid")
    if not require_map:
        return payload, base
    if payload.get("map_id") != base.get("map_id"):
        raise MissionError("map_query_map_changed")
    # A closed map's widget can linger with its visibility flag set; the open
    # map always pauses the game.
    if payload.get("status") == "ready" and payload.get("paused") != 1:
        raise MissionError("map_query_game_not_paused")
    return payload, base


# ---------------------------------------------------------------------------
# Interpretation


def _nearby_heights(base: Mapping[str, Any]) -> list[tuple[float, float, float, str]]:
    result = []
    for feed in ("nearby_actors", "nearby_interactables"):
        for item in base.get(feed) or []:
            position = item.get("position") if isinstance(item, Mapping) else None
            try:
                x, y, z = (float(position[k]) for k in ("x", "y", "z"))  # type: ignore[index]
            except (TypeError, KeyError, ValueError):
                continue
            if all(math.isfinite(v) for v in (x, y, z)):
                result.append((x, y, z, str(item.get("id"))))
    return result


def _resolve_height(projection: MapProjection, x: float, y: float, *,
                    actors: Iterable[tuple[float, float, float, str]] = (),
                    allow_actor: bool = False) -> tuple[float | None, str]:
    if projection.height is not None:
        ground = projection.height.sample(x, y)
        if ground is not None:
            return ground, "navimap_height_data"
    if allow_actor:
        matches = [(math.hypot(ax - x, ay - y), az, actor_id) for ax, ay, az, actor_id in actors]
        matches = [m for m in matches if m[0] <= ACTOR_HEIGHT_MATCH_RADIUS_CM]
        if len(matches) == 1:
            return matches[0][1], f"live_actor:{matches[0][2]}"
    return None, "unresolved"


def _player(base: Mapping[str, Any]) -> tuple[float, float, float]:
    position = (base.get("player") or {}).get("position") or {}
    try:
        return float(position["x"]), float(position["y"]), float(position["z"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MissionError("map_query_player_position_unavailable") from exc


class MapView:
    """One interpreted map snapshot: projection, pins, cursor and target icon."""

    def __init__(self, response: Mapping[str, Any], base: Mapping[str, Any]) -> None:
        status = response.get("status")
        if status != "ready":
            raise MissionError(f"map_query_{status or 'unavailable'}")
        self.response = response
        self.base = base
        self.map_id = str(response["map_id"])
        self.projection = build_projection(response)
        self.player = _player(base)
        self.actors = _nearby_heights(base)
        self.pins = [pin for pin in (self._pin(record, index) for index, record in
                                     enumerate(response.get("icons") or [])) if pin is not None]
        checks = []
        for pin in self.pins:
            shown = pin.get("displayed_distance_m")
            if shown is not None and pin["visible"]:
                checks.append((pin["horizontal_distance_m"], shown))
        self.distance_check = distance_agreement(checks)
        bad = [(c, s) for c, s in checks if abs(c - s) > max(DISTANCE_CHECK_MIN_M, DISTANCE_CHECK_FRACTION * s)]
        self.distance_check["disagreeing"] = len(bad)
        # Labels may be rounded or path-based; a majority disagreeing means the
        # projection, not the labels, is wrong.
        if bad and 2 * len(bad) >= len(checks):
            raise MissionError("map_projection_distance_mismatch")

    def world_point(self, local_x: float, local_y: float, *, allow_actor_height: bool = False) -> dict[str, Any]:
        x, y = self.projection.to_world(local_x, local_y)
        z, z_source = _resolve_height(self.projection, x, y, actors=self.actors, allow_actor=allow_actor_height)
        return {"x": x, "y": y, "z": z, "z_source": z_source}

    def _pin(self, record: Any, index: int) -> dict[str, Any] | None:
        if not isinstance(record, Mapping):
            return None
        local = widget_local(record)
        if local is None:
            return None
        kind, kind_source = classify_icon(record)
        category = category_for(kind)
        world = self.world_point(*local, allow_actor_height=category == "npc")
        pixel = self.projection.local_to_pixel_point(*local)
        return {
            "pin_id": str(record.get("runtime_id") or f"icon:{index}"),
            "kind": kind,
            "kind_source": kind_source,
            "category": category,
            "fast_travel_candidate": category == "fast_travel",
            "visible": record.get("visible") is True,
            "in_view": self.projection.in_view(*local),
            "map_local": {"x": local[0], "y": local[1]},
            "screen_pixel": None if pixel is None else {"x": pixel[0], "y": pixel[1]},
            "world": world,
            "horizontal_distance_m": math.hypot(world["x"] - self.player[0], world["y"] - self.player[1]) / 100.0,
            "displayed_distance_m": parse_distance_text(record.get("distance_text")),
            "evidence": {key: record.get(key) for key in ("class", "images", "texts", "properties", "distance_text")},
        }

    def cursor_local(self) -> tuple[float, float] | None:
        return widget_local(self.response.get("cursor"))

    def tracked_target(self) -> dict[str, Any] | None:
        record = self.response.get("target_icon")
        local = widget_local(record)
        if local is None or not (isinstance(record, Mapping) and record.get("visible") is True):
            return None
        return {"map_local": {"x": local[0], "y": local[1]}, "world": self.world_point(*local)}

    def find_pin(self, pin_id: str) -> dict[str, Any] | None:
        return next((pin for pin in self.pins if pin["pin_id"] == pin_id), None)

    def summary(self) -> dict[str, Any]:
        cursor = self.cursor_local()
        return {
            "map_id": self.map_id,
            "request_id": self.response.get("request_id"),
            "source_sequence": self.response.get("source_sequence"),
            "projection": {**self.projection.diagnostics(), "distance_check": self.distance_check},
            "player_position": dict(zip("xyz", self.player)),
            "cursor": None if cursor is None else {"map_local": {"x": cursor[0], "y": cursor[1]},
                                                   "world": self.world_point(*cursor)},
            "tracked_target": self.tracked_target(),
            "icons_complete": self.response.get("icons_complete") is True,
            "info_window": self.response.get("info_window"),
            "fast_travel_prompt_visible": (self.response.get("fast_travel_prompt") or {}).get("visible") is True,
        }


def read_map(service: Any, *, heights: bool = True) -> MapView:
    response, base = request_snapshot(service, heights=heights)
    if response.get("status") == "map_not_open":
        raise MissionError("map_not_open")
    return MapView(response, base)


# ---------------------------------------------------------------------------
# Map targets (world points the route follower can consume)


def _targets(service: Any) -> dict[str, dict[str, Any]]:
    registry = getattr(service, "_map_targets", None)
    if registry is None:
        registry = {}
        setattr(service, "_map_targets", registry)
    now = time.monotonic()
    for key in [k for k, v in registry.items() if now - v["_created"] > TARGET_TTL_SECONDS]:
        del registry[key]
    return registry


def _remember(service: Any, view: MapView, *, source: str, label: str, world: Mapping[str, Any],
              map_local: Mapping[str, float], extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    registry = _targets(service)
    while len(registry) >= MAX_TARGETS:
        del registry[min(registry, key=lambda k: registry[k]["_created"])]
    target_id = uuid.uuid4().hex[:16]
    z = world.get("z")
    target = {
        "target_id": target_id,
        "source": source,
        "label": label,
        "map_id": view.map_id,
        "position": {"x": world["x"], "y": world["y"], "z": z},
        "z_source": world.get("z_source"),
        "navigable": z is not None,
        "map_local": dict(map_local),
        "request_id": view.response.get("request_id"),
        "alignment": view.projection.alignment,
        **(extra or {}),
    }
    registry[target_id] = {**target, "_created": time.monotonic()}
    return target


def public_target(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def observe_map_pins(service: Any, categories: Iterable[str] | None = None,
                     include_hidden: bool = False) -> dict[str, Any]:
    wanted = None
    if categories is not None:
        wanted = {str(c) for c in categories}
        if not wanted <= set(CATEGORIES):
            raise MissionError("map_pin_category_invalid")
    view = read_map(service)
    pins = []
    for pin in view.pins:
        if (wanted is not None and pin["category"] not in wanted) or (not include_hidden and not pin["visible"]):
            continue
        target = _remember(service, view, source="map_pin", label=pin["kind"] or pin["pin_id"],
                           world=pin["world"], map_local=pin["map_local"],
                           extra={"pin_id": pin["pin_id"], "kind": pin["kind"], "category": pin["category"],
                                  "fast_travel_candidate": pin["fast_travel_candidate"]})
        pins.append({**pin, "target_id": target["target_id"], "navigable": target["navigable"]})
    counts: dict[str, int] = {}
    for pin in pins:
        counts[pin["category"]] = counts.get(pin["category"], 0) + 1
    return {"ok": True, **view.summary(), "pins": pins, "counts": counts}


def map_point_to_world(service: Any, x: float | None = None, y: float | None = None, *,
                       space: str = "map_local", z: float | None = None,
                       label: str | None = None) -> dict[str, Any]:
    """Convert a map point (map-local, screen pixel, or the cursor) to a target."""
    if space not in SPACES:
        raise MissionError("map_point_space_invalid")
    view = read_map(service)
    if space == "cursor":
        local = view.cursor_local()
        if local is None:
            raise MissionError("map_cursor_unavailable")
    else:
        try:
            px, py = float(x), float(y)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise MissionError("map_point_invalid") from exc
        if not (math.isfinite(px) and math.isfinite(py)):
            raise MissionError("map_point_invalid")
        local = view.projection.pixel_to_local(px, py) if space == "screen" else (px, py)
    world = view.world_point(*local)
    if z is not None:
        z_value = float(z)
        if not math.isfinite(z_value):
            raise MissionError("map_point_z_invalid")
        world = {**world, "z": z_value, "z_source": "caller"}
    target = _remember(service, view, source=f"map_{space}", label=label or f"map {space} point",
                       world=world, map_local={"x": local[0], "y": local[1]},
                       extra={"input": {"space": space, "x": x, "y": y}})
    return {"ok": True, **view.summary(), "target": target}


def get_map_target(service: Any, target_id: str) -> dict[str, Any]:
    if not isinstance(target_id, str) or not TARGET_ID.fullmatch(target_id):
        raise MissionError("map_target_id_invalid")
    entry = _targets(service).get(target_id)
    if entry is None:
        raise MissionError("map_target_unknown_or_expired")
    return {"ok": True, "target": public_target(entry)}
