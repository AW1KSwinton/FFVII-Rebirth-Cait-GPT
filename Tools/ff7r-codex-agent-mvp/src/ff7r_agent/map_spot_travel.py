"""Fast travel by map spot: pick the region, cycle the map's spots, confirm.

Live 2026-09-26 the area map's cursor, projection and icon identities were
not readable, but its text blocks were. The map names everything by
localisation key, which the map spot catalogue joins to display names:

- the region shown is ``AreaMap.AreaChangeBtn.Txt_MainTitle`` (``$map1200``);
- on the world map (F3) the selected region is ``Txt_AreaInfo``
  (``$map1200_help``), moved with up/down and opened with confirm;
- left/right cycles the region's spots; the selected one is the tooltip
  ``Map_Btn_00.Txt_Title`` (``$map1110``), whose ``Txt_Btn`` offers
  ``<button=decide> Fast Travel`` only where fast travel is possible;
- confirm opens ``Com_Window_01`` ("Fast-travel to this location?", Yes
  preselected) and a second confirm travels.

Every press is followed by a snapshot that must show its effect; a press
with no effect, a full cycle without the spot, or a missing prompt stops
before anything further is sent. Map keys only reach the focused game.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .conversation_state import field_blocker, has_player_position
from .field_main_menu import context_blocker, field_identity, identify_main_menu
from .map_query import request_snapshot
from .navigation_values import MissionError
from .save_slot_load import menu_snapshot

CATALOGUE_ENV = "FF7R_MAP_SPOT_CATALOGUE"
DEFAULT_CATALOGUE = (
    Path(__file__).resolve().parents[4] / "ProjectKnowledge" / "Current" / "map-spot-catalogue.json"
)
SNAPSHOT_FLAGS = ("alltexts",)
KEY_MS = 200
# The map button needs a long hold (the harness enforces at least 500 ms).
MAP_KEY_MS = 600
FOCUS_TELEMETRY_WAIT_SECONDS = 5.0
START_SETTLE_WAIT_SECONDS = 30.0
PRESS_SETTLE_SECONDS = 0.6
CHANGE_WAIT_SECONDS = 4.0
MAP_OPEN_WAIT_SECONDS = 8.0
MAX_REGION_STEPS = 10
MAX_TOWN_LEVELS = 2
MAX_SPOT_STEPS = 80
ARRIVAL_WAIT_SECONDS = 60.0
ARRIVAL_JUMP_CM = 2000.0
ALREADY_THERE_CM = 500.0
# Back presses and the wait after each.  Live after a harness restart
# (#354) three Esc presses 1.8 s apart reached the focused game and left the
# main menu open; the same key closed it later.  Quick presses first, then
# longer waits, about 20 s in all.
MENU_CLOSE_SETTLES_SECONDS = (0.8, 0.8, 0.8, 2.0, 3.0, 4.0, 5.0)
MENU_CLOSE_HOLD_MS = 400
MENU_EVIDENCE_MAX_SECONDS = 3.0

TITLE_PATH = "Map_Btn_00.WidgetTree.Txt_Title"
PROMPT_PATH = "Map_Btn_00.WidgetTree.Txt_Btn"
REGION_PATH = "AreaMap.WidgetTree.AreaChangeBtn.WidgetTree.Txt_MainTitle"
WORLD_HEADER = "$menu_map_header_worldmap"
CONFIRM_YES = "$menu_map_areamap_confirm_fast_travel_yes"
REGION_HELP = re.compile(r"^(\$map\d{4})_help$")


def load_catalogue(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    source = Path(path or os.environ.get(CATALOGUE_ENV) or DEFAULT_CATALOGUE)
    try:
        catalogue = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MissionError("map_spot_catalogue_unavailable") from exc
    if catalogue.get("schema_version") != 1:
        raise MissionError("map_spot_catalogue_invalid")
    return catalogue


def resolve_spot(catalogue: dict[str, Any], spot: str) -> dict[str, Any]:
    """A spot by text key (``$map1110``) or exact display name ("Kalm")."""
    spots = catalogue["spots"]
    text = str(spot or "").strip()
    if text in spots:
        return {"key": text, **spots[text]}
    folded = text.casefold()
    matches = [key for key, entry in spots.items() if entry["name"].casefold() == folded]
    if not matches:
        raise MissionError("map_spot_unknown")
    if len(matches) > 1:
        raise MissionError("map_spot_ambiguous")
    return {"key": matches[0], **spots[matches[0]]}


class MapScreen:
    """The map's visible texts in one snapshot, read by widget path."""

    def __init__(self, response: dict[str, Any], region_names: dict[str, str] | None = None) -> None:
        self.response = response
        self.texts = [(str(t.get("path", "")), str(t.get("text", "")))
                      for t in response.get("texts") or [] if isinstance(t, dict)]
        # Region display name (case-folded) -> catalogue key, for region maps
        # titled by name rather than key.
        self.region_names = region_names or {}

    def _last(self, suffix: str) -> str | None:
        values = [text for path, text in self.texts if path.endswith(suffix)]
        return values[-1] if values else None

    @property
    def open(self) -> bool:
        return self.response.get("status") == "ready"

    @property
    def world(self) -> bool:
        return any(text == WORLD_HEADER for path, text in self.texts
                   if path.endswith("AreaChangeBtn.WidgetTree.Txt_MainTitle"))

    @property
    def region(self) -> str | None:
        """The open region map's key. The Nibel map is titled by name with
        its era, "Nibel region (Present)", not "$map2600" (live 2026-10-04:
        every trip into it failed region_map_did_not_open)."""
        title = self._last(REGION_PATH)
        if title is None or title.startswith("$"):
            return title
        name = re.sub(r"\s*\(present\)\s*$", "", title.strip(), flags=re.IGNORECASE)
        return self.region_names.get(name.casefold(), title)

    @property
    def world_selection(self) -> str | None:
        for path, text in self.texts:
            match = REGION_HELP.match(text)
            if path.endswith("Txt_AreaInfo") and match:
                return match.group(1)
        return None

    @property
    def spot(self) -> str | None:
        return self._last(TITLE_PATH)

    @property
    def fast_travel_offered(self) -> bool:
        prompt = self._last(PROMPT_PATH) or ""
        return "<button=decide>" in prompt and "Fast" in prompt

    @property
    def confirm_shown(self) -> bool:
        return any(text == CONFIRM_YES for path, text in self.texts if "Com_Window" in path)

    def summary(self) -> dict[str, Any]:
        return {"open": self.open, "world": self.world, "region": self.region,
                "world_selection": self.world_selection, "spot": self.spot,
                "fast_travel_offered": self.fast_travel_offered, "confirm_shown": self.confirm_shown}


class SpotOrder:
    """Which spot follows which on each region map, as cycling has shown.

    Each step costs a key press and a map read (2-4 s live), and a region
    lists a dozen spots, so the one to the left can be eleven presses to
    the right (live: Kalm from Kalm). Learned pairs let the next travel step
    the shorter way.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def learn(self, region: str, pairs: list[tuple[str, str]]) -> None:
        if not region or not pairs:
            return
        orders = self._load()
        following = dict(orders.get(region) or {})
        for before, after in pairs:
            if before and after and before != after:
                following[before] = after
        orders[region] = following
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(orders, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def steps(self, region: str, current: str, target: str) -> tuple[int | None, int | None]:
        """Presses right and left from ``current`` to ``target``, where known."""
        following = self._load().get(region) or {}
        preceding = {after: before for before, after in following.items()}

        def walk(links: dict[str, str]) -> int | None:
            spot, count = current, 0
            while spot in links and count < MAX_SPOT_STEPS:
                spot, count = links[spot], count + 1
                if spot == target:
                    return count
                if spot == current:
                    return None
            return None

        return walk(following), walk(preceding)


def default_spot_order() -> SpotOrder:
    return SpotOrder(Path(os.environ.get("LOCALAPPDATA", ".")) / "ff7r-cait-gpt" / "map-spot-order.json")


class SpotArrivals:
    """Where each fast-travel spot has put Cloud, as travel has shown."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def get(self, spot: str) -> dict[str, float] | None:
        value = self._load().get(spot)
        if not isinstance(value, dict):
            return None
        try:
            return {axis: float(value[axis]) for axis in ("x", "y", "z")}
        except (KeyError, TypeError, ValueError):
            return None

    def record(self, spot: str, position: Any) -> None:
        if not isinstance(position, dict):
            return
        arrivals = self._load()
        arrivals[spot] = {axis: position.get(axis) for axis in ("x", "y", "z")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(arrivals, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


def default_spot_arrivals() -> SpotArrivals:
    return SpotArrivals(Path(os.environ.get("LOCALAPPDATA", ".")) / "ff7r-cait-gpt" / "map-spot-arrivals.json")


class MapSpotTravel:
    def __init__(self, service: Any, catalogue: dict[str, Any] | None = None,
                 order: SpotOrder | None = None, arrivals: SpotArrivals | None = None) -> None:
        self.service = service
        self.catalogue = catalogue or load_catalogue()
        self.region_names = {str(region.get("name") or "").casefold(): key
                             for key, region in self.catalogue["regions"].items() if region.get("name")}
        self.order = order
        self.arrivals = arrivals
        self.sleep = service._sleeper
        self.clock = service._monotonic
        self.presses: list[dict[str, Any]] = []
        self.menu_recovery: list[dict[str, Any]] = []

    def screen(self) -> MapScreen:
        response, _ = request_snapshot(self.service, heights=False, flags=SNAPSHOT_FLAGS)
        return MapScreen(response, self.region_names)

    def press(self, button: str, why: str) -> None:
        self.service._map_menu_key(button, MAP_KEY_MS if button == "map" else KEY_MS)
        self.presses.append({"button": button, "why": why})
        self.sleep(PRESS_SETTLE_SECONDS)

    def wait_for(self, predicate, timeout: float = CHANGE_WAIT_SECONDS) -> MapScreen:
        deadline = self.clock() + timeout
        while True:
            screen = self.screen()
            if predicate(screen):
                return screen
            if self.clock() >= deadline:
                return screen
            self.sleep(0.2)

    def open_map(self) -> MapScreen:
        # The map reads as open before its texts are filled in, and a press
        # during the opening animation is dropped (live, F3 sent before the
        # region title appeared never opened the world map), so wait for the
        # region or world-map selection to be readable.
        def ready(s: MapScreen) -> bool:
            return s.open and (s.region is not None or s.world_selection is not None)

        # A cached map widget must not override a freshly observed Main Menu
        # owner, even for the select-only dry run.
        if self.service.observe().get("in_menu") is True:
            self.close_main_menu()
        screen = self.screen()
        if not screen.open:
            self.close_main_menu()
            self.press("map", "open_map")
        screen = self.wait_for(ready, MAP_OPEN_WAIT_SECONDS)
        if not screen.open:
            raise MissionError("map_did_not_open")
        if not ready(screen):
            raise MissionError("map_region_unreadable")
        return screen

    def enter_region(self, screen: MapScreen, regions: list[str]) -> MapScreen:
        # Inside a town the map opens on the town's own map ($map1110 in
        # Kalm), a level below its region; F3 goes up one level at a time.
        order = self.catalogue["regions"]
        for _ in range(MAX_TOWN_LEVELS):
            if screen.world or screen.region in order:
                break
            town = screen.region
            self.press("map_layer_toggle", "leave_town_map")
            screen = self.wait_for(lambda s, before=town: s.world or (s.region not in (None, before)),
                                   MAP_OPEN_WAIT_SECONDS)
            if not screen.world and screen.region == town:
                raise MissionError("town_map_did_not_close")
        if not screen.world and screen.region in regions:
            return screen
        if not screen.world:
            self.press("map_layer_toggle", "world_map")
            screen = self.wait_for(lambda s: s.world and s.world_selection)
            if not screen.world:
                raise MissionError("world_map_did_not_open")
        target = min(regions, key=lambda key: abs(order[key]["list_index"]
                                                  - order.get(screen.world_selection or "", {}).get("list_index", 0)))
        for _ in range(MAX_REGION_STEPS):
            current = screen.world_selection
            if current == target:
                break
            if current not in order:
                raise MissionError("world_map_selection_unknown")
            button = "down" if order[target]["list_index"] > order[current]["list_index"] else "up"
            self.press(button, "select_region")
            screen = self.wait_for(lambda s, before=current: s.world_selection not in (None, before))
            if screen.world_selection == current:
                raise MissionError("world_map_selection_stalled")
        else:
            raise MissionError("world_map_region_not_reached")
        self.press("confirm", "open_region")
        screen = self.wait_for(lambda s: not s.world and s.region == target, MAP_OPEN_WAIT_SECONDS)
        if screen.world or screen.region != target:
            raise MissionError("region_map_did_not_open")
        return screen

    def _step(self, button: str, before: str | None) -> MapScreen:
        self.press(button, "next_spot" if button == "queen_blood_right" else "previous_spot")
        screen = self.wait_for(lambda s: s.spot not in (None, before))
        if screen.spot in (None, before):
            raise MissionError("map_spot_cycle_stalled")
        return screen

    def select_spot(self, screen: MapScreen, key: str, region: str | None = None) -> tuple[MapScreen, list[str]]:
        visited: list[str] = []
        pairs: list[tuple[str, str]] = []
        initial = first = None
        try:
            # The tooltip keeps the last spot of another region until a spot
            # is selected here, so the first title only counts once a press
            # moved it.
            initial = screen.spot
            screen = self._step("queen_blood_right", initial)
            first = screen.spot
            if self.order is not None and region and screen.spot != key:
                right, left = self.order.steps(region, screen.spot, key)
                if left is not None and (right is None or left < right):
                    for _ in range(left):
                        before = screen.spot
                        screen = self._step("queen_blood_left", before)
                        pairs.append((screen.spot, before))
                        visited.append(before)
                    if screen.spot == key:
                        return screen, visited + [key]
                    # The order has changed; cycle right as without it.
            seen: list[str] = []
            for _ in range(MAX_SPOT_STEPS):
                if screen.spot == key:
                    return screen, visited + [key]
                if screen.spot in seen:
                    raise MissionError("map_spot_not_available")
                seen.append(screen.spot)
                visited.append(screen.spot)
                before = screen.spot
                screen = self._step("queen_blood_right", before)
                pairs.append((before, screen.spot))
            raise MissionError("map_spot_not_reached")
        finally:
            if self.order is not None and region:
                # The first press started from the tooltip, which may linger
                # from another region; it was this region's selection only if
                # this region's cycle passed it too.
                if initial is not None and any(initial in pair for pair in pairs):
                    pairs.insert(0, (initial, first))
                self.order.learn(region, pairs)

    def _main_menu_evidence(self, start: dict[str, Any]) -> dict[str, Any]:
        started = self.clock()
        roots, root_base = request_snapshot(self.service, heights=False,
                                            flags=("uiroots", "uirootdetails", "uiownerattachments"), require_map=False)
        menu, menu_base = menu_snapshot(self.service)
        latest = self.service.observe()
        cinema = self.service.cutscene_state()
        evidence = identify_main_menu(menu, roots.get("ui_roots"), latest, cinema)
        evidence["reads"] = [{key: response.get(key) for key in
                              ("request_id", "source_sequence", "timestamp_utc")}
                             for response in (roots, menu)]
        for response, base in ((roots, root_base), (menu, menu_base)):
            try:
                stamp = datetime.fromisoformat(str(response["timestamp_utc"]).replace("Z", "+00:00"))
                age = (self.service._wall_clock() - stamp).total_seconds()
            except (KeyError, TypeError, ValueError):
                age = float("inf")
            if (response.get("source") != "ue4ss_map_query" or not response.get("request_id")
                    or type(response.get("source_sequence")) is not int
                    or response.get("source_sequence", -1) < start.get("sequence", 0)
                    or not -0.5 <= age <= 1.0 + MENU_EVIDENCE_MAX_SECONDS
                    or self.clock() - started > MENU_EVIDENCE_MAX_SECONDS):
                evidence.update(identified=False, reason="menu_evidence_stale_or_invalid")
            if (context_blocker(base) or field_identity(base) != field_identity(start)
                    or response.get("map_id") != start.get("map_id")
                    or field_identity(latest) != field_identity(start)):
                evidence.update(identified=False, reason="menu_context_changed")
        return evidence

    def close_main_menu(self) -> None:
        """Close only a freshly identified field Main Menu, inside this job.

        A reconnect can precede the cached ``in_menu`` flag. A pause alone
        starts identification, never a blind Back press on another owner.
        """
        for settle in MENU_CLOSE_SETTLES_SECONDS:
            start = self.service.observe()
            if not start.get("in_menu") and not start.get("paused"):
                return
            # Rearming may reconnect the pad. Do it before the UI proof, then
            # use the normal guarded Cancel path as npc_approach does.
            if getattr(getattr(self.service, "input", None), "is_real", False):
                self.service._ensure_game_foreground(force=True)
                self.service._assert_real_menu_input_allowed("cancel", MENU_CLOSE_HOLD_MS)
            try:
                evidence = self._main_menu_evidence(start)
            except MissionError as exc:
                # An old/missing exporter diagnostic cannot authorize Back.
                evidence = {"identified": False, "reason": str(exc)}
            self.menu_recovery.append(evidence)
            latest = self.service.observe()
            if not latest.get("in_menu") and not latest.get("paused"):
                return
            if context_blocker(latest) or field_identity(latest) != field_identity(start):
                evidence.update(identified=False, reason="menu_context_changed")
            if not evidence["identified"]:
                reason = ("main_menu_open" if start.get("in_menu") is True
                          or evidence.get("main_menu_text_seen") else "menu_or_pause_unidentified")
                raise MissionError(reason, response={"menu_recovery": self.menu_recovery,
                    "recovery": "Inspect capture_game_window, read_ui_roots(details=true) and read_save_menu. Close only the confirmed Main Menu with guarded tap_button cancel, then retry travel_to_map_spot_async; leave other owners intact."})
            press = {"button": "cancel", "why": "close_main_menu", "evidence": evidence,
                     "outcome": "unverified"}
            try:
                response = self.service.tap("cancel", MENU_CLOSE_HOLD_MS)
            except Exception as exc:
                press["error"] = str(exc)
                self.presses.append(press)
                raise
            finally:
                self.service.input.release_all()
            press["response"] = response
            self.presses.append(press)
            if not isinstance(response, dict) or response.get("ok") is not True:
                raise MissionError("main_menu_cancel_unverified")
            press["outcome"] = "accepted"
            self.sleep(PRESS_SETTLE_SECONDS)
            self.sleep(settle)
        latest = self.service.observe()
        if latest.get("in_menu") or latest.get("paused"):
            raise MissionError("main_menu_did_not_close")

    def close_map(self) -> None:
        for _ in range(3):
            if not self.screen().open:
                return
            self.press("queen_blood_cancel", "close_map")
            self.sleep(0.5)

    def run(self, spot: str, execute: bool) -> dict[str, Any]:
        # Async bridge exclusivity covers other RPCs; this lock also covers
        # service-local watchers (including PR #368) for the whole travel.
        lock = getattr(self.service, "_action_lock", None)
        if lock is not None and not lock.acquire(blocking=False):
            raise MissionError("another input action is executing")
        try:
            return self._run(spot, execute)
        except MissionError as exc:
            exc.response = {**(exc.response or {}), "presses": self.presses,
                            "menu_recovery": self.menu_recovery}
            raise
        finally:
            if lock is not None:
                lock.release()

    def _run(self, spot: str, execute: bool) -> dict[str, Any]:
        entry = resolve_spot(self.catalogue, spot)
        regions = [key for key in entry.get("regions") or [] if key in self.catalogue["regions"]]
        if not regions:
            raise MissionError("map_spot_region_unknown")
        report: dict[str, Any] = {"spot": entry["key"], "name": entry["name"], "regions": regions,
                                  "execute": execute, "presses": self.presses,
                                  "menu_recovery": self.menu_recovery}
        start = self.service.observe()
        blocker = field_blocker(start)
        if blocker:
            raise MissionError(blocker)
        report["start_position"] = (start.get("player") or {}).get("position")
        if not has_player_position(start):
            raise MissionError("map_spot_start_position_unavailable")
        # Travelling to where Cloud already stands moves him nowhere, so the
        # arrival (a jump away from the start) is never seen (live: Kalm from
        # Kalm's arrival point, fast_travel_arrival_not_observed).
        known = self.arrivals.get(entry["key"]) if self.arrivals is not None else None
        if known is not None and not _moved(known, start, ALREADY_THERE_CM):
            report.update(state="already_there", arrival_position=report["start_position"])
            return report
        prewarm = getattr(self.service, "prewarm_navigation", None)
        if execute and known is not None and callable(prewarm):
            # Build the route validator for the destination behind the
            # loading screen (74 s for Kalm, live, with the game running).
            try:
                report["validator_prewarm"] = prewarm(known["x"], known["y"], z=known.get("z"))
            except Exception as exc:
                report["validator_prewarm"] = {"state": "failed", "detail": str(exc)[:200]}
        try:
            return self._travel(entry, regions, report, execute)
        except MissionError as exc:
            if exc.response is None:
                exc.response = {"presses": self.presses}
            raise

    def _travel(self, entry: dict[str, Any], regions: list[str], report: dict[str, Any],
                execute: bool) -> dict[str, Any]:
        # In the background the game renders slowly and the sampler answers
        # late (live: map_query_timeout on the first read after a rebind).
        focus = getattr(self.service, "_ensure_game_foreground", None)
        if execute and callable(focus):
            focus(force=True)
            # Telemetry catches up a moment after the game regains focus
            # (live: map_query_telemetry_stale straight after focusing).
            deadline = self.clock() + FOCUS_TELEMETRY_WAIT_SECONDS
            while not (self.service.observe().get("telemetry") or {}).get("live") and self.clock() < deadline:
                self.sleep(0.25)
        if execute and (self.service.observe().get("in_menu") is True or not self.screen().open):
            # Keep the existing bounded main-menu recovery: a field proof
            # cannot settle while that menu is open. No map key is sent yet.
            self.close_main_menu()
            # This runs inside the existing async travel job, after focus and
            # before any map key. Long settling is never an ordinary sync RPC.
            settled = self.service.wait_for_settled_gameplay(START_SETTLE_WAIT_SECONDS)
            if not isinstance(settled, dict) or settled.get("ok") is not True:
                raise MissionError("map_spot_start_settled_unverified")
            report["start_settled"] = settled
        screen = self.enter_region(self.open_map(), regions)
        screen, seen = self.select_spot(screen, entry["key"], screen.region)
        report["cycled"] = [{"key": key, "name": self.catalogue["spots"].get(key, {}).get("name")} for key in seen]
        report["selected"] = screen.summary()
        if not screen.fast_travel_offered:
            self.close_map()
            raise MissionError("map_spot_fast_travel_not_offered")
        if not execute:
            self.close_map()
            report["state"] = "dry_run_selected"
            return report
        self.press("confirm", "fast_travel")
        screen = self.wait_for(lambda s: s.confirm_shown)
        if not screen.confirm_shown:
            raise MissionError("fast_travel_confirm_not_shown")
        self.press("confirm", "confirm_yes")
        # The paused map can still read as settled gameplay, so arrival is the
        # player reappearing away from where they stood.
        deadline = self.clock() + ARRIVAL_WAIT_SECONDS
        while not _moved(report["start_position"], self.service.observe(), ARRIVAL_JUMP_CM):
            if self.clock() >= deadline:
                raise MissionError("fast_travel_arrival_not_observed")
            self.sleep(1.0)
        settled = self.service.wait_for_settled_gameplay(ARRIVAL_WAIT_SECONDS)
        arrival = self.service.observe()
        report["arrival_position"] = (arrival.get("player") or {}).get("position")
        if not has_player_position(arrival):
            raise MissionError("map_spot_arrival_position_unavailable")
        report["settled"] = settled
        report["state"] = "arrived"
        if self.arrivals is not None:
            self.arrivals.record(entry["key"], report["arrival_position"])
        return report


def _moved(start: Any, sample: dict[str, Any], distance_cm: float) -> bool:
    position = (sample.get("player") or {}).get("position")
    if not isinstance(start, dict) or not isinstance(position, dict):
        return False
    try:
        return ((position["x"] - start["x"]) ** 2 + (position["y"] - start["y"]) ** 2) ** 0.5 > distance_cm
    except (KeyError, TypeError):
        return False


def travel_to_map_spot(service: Any, spot: str, execute: bool = False) -> dict[str, Any]:
    return MapSpotTravel(service, order=default_spot_order(), arrivals=default_spot_arrivals()).run(spot, execute)
