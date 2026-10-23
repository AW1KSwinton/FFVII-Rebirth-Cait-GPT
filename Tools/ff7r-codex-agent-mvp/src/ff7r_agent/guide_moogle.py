"""The Gold Saucer's Guide Moogles: fast travel between its zones.

Each zone has a Guide Moogle; talking to one opens a destination list of
every zone (live 2026-10-04). Map fast travel can be locked inside the Gold
Saucer (chapter 12's first visit), but the Moogles still work.

The menu reads from its widget texts: ``FastTravel_Cell`` rows name each
zone by its map text key (in no reliable order), ``FastTravel_Menu``'s
``Txt_LocaName`` is the highlighted row and ``Txt_CurrentName`` the zone
Cloud is in. Rows are listed in ``ZONES`` order. Arrival points are the
level's ``obl2350_point_setpos_fasttravel_*`` points (Ghost Square's matched
the live arrival to the centimetre).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .navigation_values import MissionError

MENU_TITLE_KEY = "$level_GOLDS_fastTravel"
GUIDE_MOOGLE = "Guide Moogle"
GOLD_SAUCER_FIELD = "2350-GOLDS"
# #361: Talk was visible at 0.843 m despite an 88.5 degree body-heading
# error. This admission applies only to the selected, stationary robot.
GUIDE_MOOGLE_CLASS = ".NP0140_00_GoldSaucerRobot_Standard_C"
GUIDE_MOOGLE_TALK_M = 1.0
GUIDE_MOOGLE_TARGET_MOVEMENT_M = 0.1
# The field prompt has no recipient. Any other character within the normal
# NPC Talk range makes its ownership ambiguous, even if the robot is nearer.
TALK_COMPETITOR_M = 5.0
# A ride costs a talk, the menu and a load (~20 s): worth it only when it
# saves a fair walk. Beyond reach of every zone's Moogle, Cloud is not in
# the Gold Saucer.
GUIDE_MOOGLE_SAVING_M = 40.0
GUIDE_MOOGLE_REACH_M = 150.0
MENU_OPEN_READS = 3
MENU_PRESS_MS = 100
MENU_PRESS_SETTLE_SECONDS = 0.35


@dataclass(frozen=True)
class Zone:
    key: str          # the zone's map text key, as the menu rows show it
    name: str         # English row text
    point: str        # level point suffix: obl2350_point_setpos_fasttravel_<point>
    arrival: tuple[float, float, float]
    aliases: tuple[str, ...] = ()


# Menu order, top to bottom.
ZONES: tuple[Zone, ...] = (
    Zone("$map2350_layer00_group10", "Welcome Area", "station", (89495.0, -31604.0, 46432.0), ("station",)),
    Zone("$map2350_layer00_group2", "Park Central", "entrance", (75776.0, -23891.0, 46371.0), ("entrance", "center")),
    Zone("$level_GOLDS_locationtitle_rightSQ", "Adventureworld", "rightsq", (71305.0, -25404.0, 47540.0), ("right side",)),
    Zone("$map2350_layer00_group4", "Chocobo Square", "chocobosq", (69275.0, -45067.0, 50349.0)),
    Zone("$map2350_layer02_group4", "Speed Square", "speedsq", (47627.0, -37752.0, 55013.0)),
    Zone("$map2350_layer01_group6", "Battle Square", "battlesq", (35117.0, -22480.0, 50827.0)),
    Zone("$level_GOLDS_locationtitle_leftSQ", "Fantasyworld", "leftsq", (75010.0, -18939.0, 47552.0), ("left side",)),
    Zone("$map2350_layer00_group3", "Event Square", "eventsq", (84700.0, -2145.0, 50707.0)),
    Zone("$map2350_layer02_group3", "Wonderment Square", "wondersq", (70850.0, 4253.0, 50234.0)),
    Zone("$map2350_layer01_group5", "Ghost Square", "ghostsq", (53719.0, 4485.0, 54867.0), ("hotel",)),
    Zone("$map2350_layer03_group2", "Skywheel Square", "roundsq", (49816.0, -11359.0, 53813.0), ("round square",)),
)
_BY_KEY = {zone.key: zone for zone in ZONES}


def zone(name: str) -> Zone:
    """A zone by English name, key, level point or alias (case-insensitive)."""
    wanted = str(name or "").strip().casefold()
    for candidate in ZONES:
        if wanted in {candidate.key.casefold(), candidate.name.casefold(), candidate.point,
                      *(alias.casefold() for alias in candidate.aliases)}:
            return candidate
    raise MissionError(f"guide_moogle_unknown_destination:{name}")


def _xyz(position: dict[str, float] | tuple[float, ...]) -> tuple[float, float, float]:
    if isinstance(position, dict):
        return float(position["x"]), float(position["y"]), float(position["z"])
    return float(position[0]), float(position[1]), float(position[2])


def nearest_zone(position: dict[str, float] | tuple[float, ...]) -> tuple[Zone, float]:
    """The zone whose arrival is nearest ``position`` (cm), with that distance in m."""
    here = _xyz(position)
    best = min(ZONES, key=lambda candidate: math.dist(here, candidate.arrival))
    return best, math.dist(here, best.arrival) / 100.0


def worth_guide_moogle(start: dict[str, float], target: dict[str, float]) -> Zone | None:
    """The zone to ride to before walking from ``start`` to ``target`` (cm),
    when walking to the nearest Moogle and on from the arrival saves at least
    ``GUIDE_MOOGLE_SAVING_M``; None to walk (or when Cloud is not in the Gold
    Saucer)."""
    destination, onward = nearest_zone(target)
    here, to_moogle = nearest_zone(start)
    if destination == here or to_moogle > GUIDE_MOOGLE_REACH_M:
        return None
    direct = math.dist(_xyz(start), _xyz(target)) / 100.0
    return destination if to_moogle + onward + GUIDE_MOOGLE_SAVING_M < direct else None


def read_menu(texts: list[dict[str, str]]) -> dict[str, Any]:
    """The Guide Moogle menu from screen texts: open, rows (menu order),
    highlighted and current zone keys."""
    title = selected = current = None
    rows: set[str] = set()
    for item in texts:
        path, text = item.get("path", ""), item.get("text", "")
        if ".FastTravel_Menu." in path:
            if path.endswith(".Txt_Title"):
                title = text
            elif path.endswith(".Txt_LocaName"):
                selected = text
            elif path.endswith(".Txt_CurrentName"):
                current = text
        elif ".FastTravel_Cell." in path and path.endswith(".Txt_Name"):
            rows.add(text)
    ordered = [candidate.key for candidate in ZONES if candidate.key in rows]
    return {"open": title == MENU_TITLE_KEY and bool(ordered), "rows": ordered,
            "unknown_rows": sorted(rows - set(ordered)), "selected": selected, "current": current}


def presses_to(menu: dict[str, Any], destination: Zone) -> tuple[str, int]:
    """The direction and number of presses from the highlighted row to ``destination``."""
    rows = menu["rows"]
    if destination.key not in rows:
        raise MissionError(f"guide_moogle_destination_not_offered:{destination.name}")
    if menu.get("selected") not in rows:
        raise MissionError("guide_moogle_highlight_unreadable")
    steps = rows.index(destination.key) - rows.index(menu["selected"])
    return ("down" if steps >= 0 else "up"), abs(steps)


def zone_name(key: str | None) -> str | None:
    found = _BY_KEY.get(str(key))
    return found.name if found else key


def verify_talk_target(scan: dict[str, Any], prompt: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """Admit the actual Talk prompt for one exact live Guide Moogle.

    The inventory operation already validates its native request/sequence;
    check that contract here too. Never infer a recipient from Talk alone or
    resolve the shared robot class to a different, newly nearest actor.
    """
    from .entity_interaction import validate_sample
    from .field_prompt import TALK_KEY
    from .navigation_values import finite_position, horizontal_distance

    def refuse(reason: str) -> None:
        raise MissionError(f"guide_moogle_talk_{reason}", response={"recovery_refused": True})

    if not all(isinstance(value, dict) for value in (expected, scan, prompt)):
        refuse("target_invalid")
    canonical = expected.get("canonical_id")
    actor_id = expected.get("actor_id")
    if (not isinstance(canonical, str)
            or not canonical.startswith(f"field_object:{GOLD_SAUCER_FIELD}:oba2350_guideMoogle_")
            or not isinstance(actor_id, str) or not actor_id
            or not expected.get("map_id") or not expected.get("player_actor_id")
            or type(expected.get("after_sequence")) is not int or expected["after_sequence"] < 0
            or type(expected.get("after_source_sequence", expected["after_sequence"])) is not int):
        refuse("target_invalid")
    anchor = finite_position(expected.get("position"))
    snapshot = scan.get("snapshot") or {}
    sample = scan.get("sample") or {}
    if not isinstance(snapshot, dict) or not isinstance(sample, dict):
        refuse("inventory_unverified")
    validate_sample(sample, expected["after_sequence"], baseline={
        "map_id": expected["map_id"], "player": {"actor_id": expected["player_actor_id"]},
        **expected.get("world_identity", {})})
    source_sequence = sample.get("source_sequence", sample.get("sequence"))
    inventory_sequence = snapshot.get("source_sequence")
    if (scan.get("ok") is not True or not scan.get("request_id")
            or snapshot.get("request_id") != scan["request_id"]
            or snapshot.get("source") != "ue4ss_character_inventory"
            or snapshot.get("complete") is not True
            or snapshot.get("map_id") != expected["map_id"]
            or snapshot.get("player_actor_id") != expected["player_actor_id"]
            or type(inventory_sequence) is not int or type(source_sequence) is not int
            or inventory_sequence > source_sequence
            or inventory_sequence < expected.get("after_source_sequence", expected["after_sequence"])
            or not isinstance(snapshot.get("characters"), list)):
        refuse("inventory_unverified")
    rows = snapshot["characters"]
    matches = [row for row in rows if isinstance(row, dict) and row.get("id") == actor_id]
    if len(matches) != 1:
        refuse("target_changed")
    actor = matches[0]
    if (actor.get("is_player") is not False
            or not str(actor.get("class") or "").endswith(GUIDE_MOOGLE_CLASS)):
        refuse("target_changed")
    position = finite_position(actor.get("position"))
    if (horizontal_distance(position, anchor) > GUIDE_MOOGLE_TARGET_MOVEMENT_M
            or abs(position["z"] - anchor["z"]) > GUIDE_MOOGLE_TARGET_MOVEMENT_M * 100):
        refuse("target_moved")
    if (prompt.get("talk") is not True or TALK_KEY not in (prompt.get("commands") or [])
            or type(prompt.get("source_sequence")) is not int
            or prompt["source_sequence"] < inventory_sequence):
        refuse("prompt_unverified")
    player = finite_position(prompt.get("player_position"))
    scanned_player = finite_position(sample["player"].get("position"))
    if (horizontal_distance(player, scanned_player) > 0.05
            or abs(player["z"] - scanned_player["z"]) > 5):
        refuse("player_not_settled")
    distance = horizontal_distance(player, position)
    if distance > TALK_COMPETITOR_M or abs(player["z"] - position["z"]) > 150:
        refuse("out_of_range")
    if distance > GUIDE_MOOGLE_TALK_M:
        # Outside the evidence-backed close admission, retain body facing.
        from .npc_approach import FACE_BODY_TOLERANCE_DEG
        yaw = (sample["player"].get("rotation") or {}).get("yaw")
        if type(yaw) not in (int, float) or not math.isfinite(yaw):
            refuse("facing_not_ready")
        bearing = math.degrees(math.atan2(position["y"] - player["y"], position["x"] - player["x"]))
        if abs((bearing - yaw + 180) % 360 - 180) > FACE_BODY_TOLERANCE_DEG:
            refuse("facing_not_ready")
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            refuse("inventory_unverified")
        if row.get("id") == actor_id or row.get("is_player") is True:
            continue
        other = finite_position(row.get("position"))
        if horizontal_distance(player, other) <= TALK_COMPETITOR_M and abs(player["z"] - other["z"]) <= 150:
            refuse("prompt_ambiguous")
    return {"ok": True, "reason": "guide_moogle_talk_verified", "target": expected,
            "actor_id": actor_id, "position": position, "distance_m": round(distance, 3),
            "inventory_sequence": inventory_sequence, "prompt": prompt}


def use(service: Any, destination: str, *, execute: bool = False, sleep=time.sleep,
        expected_target: dict[str, Any] | None = None) -> dict[str, Any]:
    """At a Guide Moogle (Talk shown) or with its menu open: talk, highlight
    ``destination``, check the highlight and confirm. Returns as the screen
    fades, with the arrival point to wait for."""
    from . import field_prompt, shop
    target = zone(destination)
    plan = {"destination": target.name, "destination_key": target.key,
            "arrival": dict(zip(("x", "y", "z"), target.arrival))}

    def menu_now() -> dict[str, Any]:
        return read_menu(shop.read_screen_texts(service)["texts"])

    menu = menu_now()
    if not menu["open"]:
        prompt = field_prompt.read_field_prompt(service)
        if not prompt.get("talk"):
            raise MissionError(f"guide_moogle_not_offered:{prompt.get('commands')}")
        if not execute:
            return {"ok": True, "execute": False, "state": "would_talk", **plan}
        # The approach's proof is not a cached authorization. Check the same
        # actor and the latest Talk again under talk_to_npc's action lock.
        arguments = {"settle_seconds": 2.0}
        if expected_target is not None:
            arguments["expected_guide_moogle"] = expected_target
        talked = service.talk_to_npc(**arguments)
        if expected_target is not None:
            plan["talk_admission"] = talked["guide_moogle_admission"]
        for _ in range(MENU_OPEN_READS):
            menu = menu_now()
            if menu["open"]:
                break
            sleep(1.0)
        else:
            raise MissionError("guide_moogle_menu_not_opened")
    elif not execute:
        return {"ok": True, "execute": False, "state": "would_select", **plan, "menu": menu}
    if menu["current"] == target.key:
        service.tap("cancel", MENU_PRESS_MS)
        return {"ok": True, "state": "already_there", **plan}
    presses = 0
    for _ in range(2):  # one correction if a press was dropped
        direction, count = presses_to(menu, target)
        for _ in range(count):
            service.tap(direction, MENU_PRESS_MS)
            presses += 1
            sleep(MENU_PRESS_SETTLE_SECONDS)
        menu = menu_now()
        if menu["selected"] == target.key:
            break
    else:
        raise MissionError(f"guide_moogle_highlight_wrong:{zone_name(menu['selected'])}")
    service.tap("confirm", MENU_PRESS_MS)
    return {"ok": True, "state": "travelling", **plan, "from": zone_name(menu["current"]), "presses": presses}
