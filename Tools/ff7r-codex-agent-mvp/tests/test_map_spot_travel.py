import json
from datetime import datetime, timedelta, timezone
from threading import Lock
from types import SimpleNamespace
from dataclasses import replace
import pytest

from ff7r_agent import map_spot_travel
from ff7r_agent.map_spot_travel import MapSpotTravel, resolve_spot
from ff7r_agent.navigation_values import MissionError

CATALOGUE = {
    "schema_version": 1,
    "regions": {
        "$map1200": {"name": "Grasslands", "list_index": 0},
        "$map1300": {"name": "Junon Region", "list_index": 1},
        "$map2200": {"name": "Corel Region", "list_index": 2},
    },
    "spots": {
        "$map1110": {"name": "Kalm", "regions": ["$map1200"]},
        "$map1110_FastTravel1": {"name": "Kalm - Air-Raid Shelter", "regions": ["$map1200"]},
        "$hub_place_1000_03": {"name": "Abandoned Dock", "regions": ["$map1200"]},
        "$menu_WorldReport_GRASA_01_Title": {"name": "Activation Intel 1", "regions": ["$map1200"]},
        "$map2260_FastTravel1": {"name": "North Corel - Suspension Bridge", "regions": ["$map2200"]},
    },
}
SPOTS = {
    "$map1200": ["$hub_place_1000_03", "$menu_WorldReport_GRASA_01_Title", "$map1110", "$map1110_FastTravel1"],
    "$map2200": ["$map2260_FastTravel1"],
}
NO_TRAVEL = {"$menu_WorldReport_GRASA_01_Title"}
WORLD_ORDER = ["$map1200", "$map1300", "$map2200"]
TOWNS = {"$map1110": "$map1200"}  # Kalm's own map sits below the Grasslands


class FakeMap:
    """The map menus as seen live on 2026-09-26 (Corel Region to Kalm)."""

    def __init__(self) -> None:
        self.open = False
        self.world = False
        self.region = "$map2200"
        self.world_index = 2
        self.spot_index = None
        self.tooltip = "$map2260_FastTravel1"  # lingers from the last region
        self.dialog = False
        self.travelled = False
        self.position = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.dialogue_active = False
        self.presses: list[str] = []
        self.loading_snapshots = 0
        self.main_menu = False  # a main menu over the field swallows the map key
        self.menu_ignores = 0  # Back presses the open menu ignores (#354)

    def press(self, button: str) -> None:
        self.presses.append(button)
        if self.main_menu:
            if button in ("queen_blood_cancel", "cancel"):
                if self.menu_ignores:
                    self.menu_ignores -= 1
                else:
                    self.main_menu = False
            return
        if not self.open:
            if button == "map":
                self.open = True
                self.loading_snapshots = 2
            return
        if self.loading_snapshots:
            return  # dropped during the opening animation
        if self.dialog:
            if button == "confirm":
                self.dialog, self.open, self.travelled = False, False, True
                self.position = {"x": 50000.0, "y": 0.0, "z": 0.0}
            return
        if self.world:
            if button in ("up", "down"):
                self.world_index = max(0, min(2, self.world_index + (1 if button == "down" else -1)))
            elif button == "confirm":
                self.world, self.region, self.spot_index = False, WORLD_ORDER[self.world_index], None
            return
        if button == "map_layer_toggle":
            if self.region in TOWNS:
                self.region, self.spot_index = TOWNS[self.region], None  # up to the region map
            else:
                self.world = True
        elif button in ("queen_blood_right", "queen_blood_left"):
            spots = SPOTS[self.region]
            step = 1 if button == "queen_blood_right" else -1
            self.spot_index = 0 if self.spot_index is None else (self.spot_index + step) % len(spots)
            self.tooltip = spots[self.spot_index]
        elif button == "confirm" and self.tooltip not in NO_TRAVEL:
            self.dialog = True
        elif button == "queen_blood_cancel":
            self.open = False

    def response(self) -> dict:
        if not self.open:
            return {"status": "map_not_open", "texts": []}
        if self.loading_snapshots:
            self.loading_snapshots -= 1  # open, but its texts not filled in yet
            return {"status": "ready", "texts": []}
        prompt = "<button=R3> Drop Pin" if self.tooltip in NO_TRAVEL else "<button=decide>\xa0Fast\xa0Travel"
        texts = [
            {"path": "X.AreaMap.WidgetTree.AreaChangeBtn.WidgetTree.Txt_MainTitle", "text": self.region},
            {"path": "X.Map_Btn_00.WidgetTree.Txt_Title", "text": self.tooltip},
            {"path": "X.Map_Btn_00.WidgetTree.Txt_Btn", "text": prompt},
        ]
        if self.world:
            texts += [
                {"path": "X.WorldMap.WidgetTree.AreaChangeBtn.WidgetTree.Txt_MainTitle", "text": "$menu_map_header_worldmap"},
                {"path": "X.WorldMap.WidgetTree.Txt_AreaInfo", "text": WORLD_ORDER[self.world_index] + "_help"},
            ]
        if self.dialog:
            texts.append({"path": "X.Com_Window_01.WidgetTree.Btn_00.WidgetTree.Txt",
                          "text": "$menu_map_areamap_confirm_fast_travel_yes"})
        return {"status": "ready", "texts": texts}


class FakeService:
    def __init__(self, game: FakeMap) -> None:
        self.game = game
        self.now = 0.0
        self.settled = 0
        self._action_lock = Lock()
        self.input = SimpleNamespace(is_real=False, release_all=lambda: None)

    def observe(self) -> dict:
        return {
            "running": True, "source": "test", "telemetry": {"live": True},
            "sequence": 1, "paused": self.game.main_menu or self.game.open,
            "in_combat": False, "loading": False, "target": None,
            "player": {"position": dict(self.game.position) if self.game.position is not None else None},
            "map_id": None if self.game.dialogue_active else "map1200",
            "interaction": {"state": "dialogue"} if self.game.dialogue_active else None,
            "queen_blood": {"active": False, "match_screen": False, "begin_match_available": False},
            "in_menu": self.game.main_menu,
        }

    def _sleeper(self, seconds: float) -> None:
        self.now += seconds

    def _monotonic(self) -> float:
        return self.now

    def _wall_clock(self):
        # Synthetic replay time, not timing measured during a reconnect.
        return datetime(2026, 10, 13, tzinfo=timezone.utc) + timedelta(seconds=self.now)

    def cutscene_state(self):
        return {"observed": True, "active": False, "anim_cutscene": False, "playing": []}

    def tap(self, button, milliseconds):
        assert self._action_lock.locked()
        self.game.press(button)
        return {"ok": True, "button": button, "milliseconds": milliseconds}

    def _map_menu_key(self, button: str, milliseconds: int) -> None:
        self.game.press(button)

    def wait_for_settled_gameplay(self, timeout: float) -> dict:
        self.settled += 1
        return {"ok": True}


@pytest.fixture
def game(monkeypatch):
    game = FakeMap()
    monkeypatch.setattr(map_spot_travel, "request_snapshot",
                        lambda service, heights, flags, **kwargs: (game.response(), service.observe()))
    monkeypatch.setattr(map_spot_travel, "menu_snapshot", fake_menu_snapshot)

    original = map_spot_travel.request_snapshot
    def snapshot(service, heights, flags, **kwargs):
        if "uiroots" in flags:
            return recovery_response(service, ui_roots=main_menu_roots(game)), service.observe()
        return original(service, heights, flags, **kwargs)
    monkeypatch.setattr(map_spot_travel, "request_snapshot", snapshot)
    return game


def recovery_response(service, **extra):
    """Synthetic current request metadata; no raw reconnect capture is available."""
    base = service.observe()
    return {"source": "ue4ss_map_query", "request_id": "synthetic-recovery",
            "source_sequence": base["sequence"], "timestamp_utc": service._wall_clock().isoformat(),
            "map_id": base.get("map_id"), **extra}


def main_menu_roots(game):
    return {"observed": True, "configured_complete": True, "truncated": False, "widgets": [
        {"class": "MainMenu_Base_C", "path": "/Temp/MainMenu_Base", "configured_root": True,
         "is_visible": game.main_menu, "details": {"in_viewport_ex": game.main_menu}},
        {"class": "BattleTips_C", "configured_root": True, "details": {"in_viewport_ex": False}},
    ]}


def fake_menu_snapshot(service):
    texts = [{"path": "X.MainMenu_Base.WidgetTree.Txt_Title", "text": "$menu_mainmenu_0000_0000"},
             {"path": "X.Foot.WidgetTree.Txt_Help", "text": "$help_message_0000_0011"}]
    return recovery_response(service, texts=texts if service.game.main_menu else []), service.observe()


def test_resolves_spots_by_key_or_display_name() -> None:
    assert resolve_spot(CATALOGUE, "kalm")["key"] == "$map1110"
    assert resolve_spot(CATALOGUE, "$map1110_FastTravel1")["name"] == "Kalm - Air-Raid Shelter"
    with pytest.raises(MissionError, match="map_spot_unknown"):
        resolve_spot(CATALOGUE, "Midgar")


def test_travels_across_regions_by_cycling_spots(game) -> None:
    service = FakeService(game)
    report = MapSpotTravel(service, CATALOGUE).run("Kalm", execute=True)

    assert report["state"] == "arrived" and game.travelled
    assert game.presses == ["map", "map_layer_toggle", "up", "up", "confirm",
                            "queen_blood_right", "queen_blood_right", "queen_blood_right",
                            "confirm", "confirm"]
    assert [spot["key"] for spot in report["cycled"]] == [
        "$hub_place_1000_03", "$menu_WorldReport_GRASA_01_Title", "$map1110"]
    assert service.settled == 2


def test_dry_run_selects_then_closes_the_map(game) -> None:
    report = MapSpotTravel(FakeService(game), CATALOGUE).run("$map1110_FastTravel1", execute=False)

    assert report["state"] == "dry_run_selected"
    assert not game.open and not game.travelled and game.presses[-1] == "queen_blood_cancel"


def test_refuses_a_spot_without_the_fast_travel_prompt(game) -> None:
    catalogue = {**CATALOGUE}
    with pytest.raises(MissionError, match="map_spot_fast_travel_not_offered"):
        MapSpotTravel(FakeService(game), catalogue).run("Activation Intel 1", execute=True)
    assert "confirm" not in game.presses[5:] and not game.travelled


def test_stops_after_a_full_cycle_without_the_spot(game) -> None:
    catalogue = {**CATALOGUE, "spots": {**CATALOGUE["spots"],
                                        "$map1230": {"name": "Mythril Mine", "regions": ["$map1200"]}}}
    with pytest.raises(MissionError, match="map_spot_not_available"):
        MapSpotTravel(FakeService(game), catalogue).run("Mythril Mine", execute=True)
    assert not game.travelled


def test_inside_a_town_goes_up_to_the_region_map_first(game) -> None:
    # Live 2026-09-27: in Kalm the map opened on Kalm's own map ($map1110).
    game.region, game.world_index, game.tooltip = "$map1110", 0, "$str2500_COSMA_Ch10_Step100"
    report = MapSpotTravel(FakeService(game), CATALOGUE).run("Kalm - Air-Raid Shelter", execute=True)
    assert report["state"] == "arrived"
    assert game.presses[:3] == ["map", "map_layer_toggle", "queen_blood_right"]


def test_a_learned_spot_order_steps_the_shorter_way(game, tmp_path) -> None:
    from ff7r_agent.map_spot_travel import SpotOrder

    order = SpotOrder(tmp_path / "order.json")
    # Any travel learns the order of the spots it cycled past.
    MapSpotTravel(FakeService(game), CATALOGUE, order=order).run("Kalm - Air-Raid Shelter", execute=True)
    assert order.steps("$map1200", "$map1110_FastTravel1", "$map1110") == (None, 1)

    # Later the map opens on Kalm (where Cloud stands); the first press moves
    # on to the Air-Raid Shelter, and Kalm is one press back, not a lap.
    game.open, game.region, game.presses = False, "$map1200", []
    game.position = {"x": 0.0, "y": 0.0, "z": 0.0}
    game.spot_index, game.tooltip = 2, "$map1110"
    report = MapSpotTravel(FakeService(game), CATALOGUE, order=order).run("Kalm", execute=True)
    assert report["state"] == "arrived"
    assert game.presses == ["map", "queen_blood_right", "queen_blood_left", "confirm", "confirm"]


def test_a_changed_order_falls_back_to_cycling_right(game, tmp_path) -> None:
    from ff7r_agent.map_spot_travel import SpotOrder

    order = SpotOrder(tmp_path / "order.json")
    # A stale belief: Kalm to the left of the first spot.
    order.learn("$map1200", [("$map1110", "$hub_place_1000_03")])
    game.region, game.world_index = "$map1200", 0
    report = MapSpotTravel(FakeService(game), CATALOGUE, order=order).run("Kalm", execute=True)
    assert report["state"] == "arrived"
    assert order.steps("$map1200", "$hub_place_1000_03", "$map1110")[0] == 2


def test_the_selection_the_map_opened_on_is_learned_once_the_cycle_passes_it(game, tmp_path) -> None:
    from ff7r_agent.map_spot_travel import SpotOrder

    # Opening on Kalm and lapping right to Kalm links Kalm to the next spot,
    # so the next travel to Kalm is a single press back.
    order = SpotOrder(tmp_path / "order.json")
    game.region, game.world_index, game.spot_index, game.tooltip = "$map1200", 0, 2, "$map1110"
    MapSpotTravel(FakeService(game), CATALOGUE, order=order).run("Kalm", execute=True)
    assert order.steps("$map1200", "$map1110_FastTravel1", "$map1110") == (3, 1)


def test_a_tooltip_from_another_region_is_not_learned(game, tmp_path) -> None:
    from ff7r_agent.map_spot_travel import SpotOrder

    order = SpotOrder(tmp_path / "order.json")
    MapSpotTravel(FakeService(game), CATALOGUE, order=order).run("Kalm", execute=True)
    assert "$map2260_FastTravel1" not in json.dumps(order._load())


def test_an_arrival_is_learned_and_a_travel_from_there_is_skipped(game, tmp_path) -> None:
    from ff7r_agent.map_spot_travel import SpotArrivals

    arrivals = SpotArrivals(tmp_path / "arrivals.json")
    MapSpotTravel(FakeService(game), CATALOGUE, arrivals=arrivals).run("Kalm", execute=True)
    assert arrivals.get("$map1110") == {"x": 50000.0, "y": 0.0, "z": 0.0}

    # Standing at Kalm's arrival point, travelling to Kalm is a no-op.
    game.presses = []
    report = MapSpotTravel(FakeService(game), CATALOGUE, arrivals=arrivals).run("Kalm", execute=True)
    assert report["state"] == "already_there" and game.presses == []


def test_an_open_main_menu_is_closed_before_the_map_key(game) -> None:
    # Live: a virtual-gamepad reconnect left the main menu open.
    game.main_menu = True
    report = MapSpotTravel(FakeService(game), CATALOGUE).run("Kalm", execute=True)
    assert report["state"] == "arrived"
    assert game.presses[:2] == ["cancel", "map"]
    assert report["presses"][0]["evidence"]["identified"]
    assert report["presses"][0]["response"]["ok"]


def test_a_main_menu_that_ignores_quick_back_presses_still_closes(game) -> None:
    # Live after a harness restart (#354): three Esc presses reached the game
    # and the menu stayed open; the same key closed it later.
    game.main_menu = True
    game.menu_ignores = 4
    report = MapSpotTravel(FakeService(game), CATALOGUE).run("Kalm", execute=True)
    assert report["state"] == "arrived"
    assert game.presses[:6] == ["cancel"] * 5 + ["map"]


def test_a_main_menu_that_never_closes_fails_without_the_map_key(game) -> None:
    from ff7r_agent.map_spot_travel import MENU_CLOSE_SETTLES_SECONDS

    game.main_menu = True
    game.menu_ignores = 100
    with pytest.raises(MissionError, match="main_menu_did_not_close"):
        MapSpotTravel(FakeService(game), CATALOGUE).run("Kalm", execute=True)
    assert game.presses == ["cancel"] * len(MENU_CLOSE_SETTLES_SECONDS)


def test_refuses_to_start_when_player_position_is_missing(game):
    game.position = None
    with pytest.raises(MissionError, match="map_spot_start_position_unavailable"):
        MapSpotTravel(FakeService(game), CATALOGUE).run("Kalm", execute=True)
    assert game.presses == []


def test_refuses_to_travel_during_dialogue_before_opening_the_map(game):
    game.position = None
    game.dialogue_active = True
    with pytest.raises(MissionError, match="dialogue_active"):
        MapSpotTravel(FakeService(game), CATALOGUE).run("Kalm", execute=True)
    assert game.presses == []


def test_a_region_map_titled_by_name_reads_as_its_catalogue_key():
    # Live 2026-10-04: the Nibel map is titled "Nibel region (Present)", not
    # "$map2600", so every fast travel into it failed region_map_did_not_open.
    from ff7r_agent.map_spot_travel import REGION_PATH, MapScreen

    names = {"nibel region": "$map2600"}

    def screen(title):
        return MapScreen({"status": "ready", "texts": [{"path": "X." + REGION_PATH, "text": title}]}, names)

    assert screen("Nibel region (Present)").region == "$map2600"
    assert screen("$map2200").region == "$map2200"
    assert screen("Nibel region (Past)").region == "Nibel region (Past)"


@pytest.mark.parametrize("failure", [None, "raises", "unverified"])
def test_start_settle_runs_after_focus_and_before_any_map_input(game, failure):
    service = FakeService(game)
    focused, starts = [False], []

    def focus(**kwargs):
        assert kwargs == {"force": True}
        assert not game.presses
        focused[0] = True

    service._ensure_game_foreground = focus
    original_observe = service.observe
    service.observe = lambda: {**original_observe(), "telemetry": {"live": focused[0]}}

    def settle(timeout):
        if not game.travelled:
            assert focused[0] and not game.presses
            starts.append(timeout)
            # A start proof taking longer than the ordinary RPC budget is
            # still inside this one async travel worker, never replayed.
            service.now += 26.0
            if failure == "raises":
                raise MissionError("settled_field_unavailable")
            if failure == "unverified":
                return {"ok": False}
        return {"ok": True}

    service.wait_for_settled_gameplay = settle
    if failure:
        with pytest.raises(MissionError):
            MapSpotTravel(service, CATALOGUE).run("Kalm", execute=True)
        assert not game.presses and not game.travelled
    else:
        report = MapSpotTravel(service, CATALOGUE).run("Kalm", execute=True)
        assert report["state"] == "arrived" and report["start_settled"]["ok"]
        assert game.presses.count("map") == 1 and game.presses.count("confirm") == 3
    assert starts == [map_spot_travel.START_SETTLE_WAIT_SECONDS]


def test_start_settle_preserves_main_menu_close_and_existing_map_resume(game):
    service = FakeService(game)
    game.main_menu = True
    def settle(timeout):
        assert not game.main_menu
        return {"ok": True}
    service.wait_for_settled_gameplay = settle
    assert MapSpotTravel(service, CATALOGUE).run("Kalm", execute=True)["state"] == "arrived"
    assert game.presses[0] == "cancel" and game.presses[1] == "map"

    game.presses.clear()
    game.open, game.travelled = True, False
    game.position = {"x": 0.0, "y": 0.0, "z": 0.0}
    game.region, game.spot_index, game.tooltip = "$map1200", 2, "$map1110"
    settled = []
    def settle_after_arrival(timeout):
        assert game.travelled  # Never request a field proof inside the open map.
        settled.append(timeout)
        return {"ok": True}
    service.wait_for_settled_gameplay = settle_after_arrival
    assert MapSpotTravel(service, CATALOGUE).run("Kalm", execute=True)["state"] == "arrived"
    assert settled == [map_spot_travel.ARRIVAL_WAIT_SECONDS]
    assert "map" not in game.presses


def test_reconnect_menu_recovers_even_before_cached_in_menu_flag(game):
    game.main_menu = True
    service = FakeService(game)
    observe = service.observe
    service.observe = lambda: {**observe(), "in_menu": False}
    report = MapSpotTravel(service, CATALOGUE).run("Kalm", execute=True)
    assert report["state"] == "arrived"
    assert game.presses[:2] == ["cancel", "map"]
    assert service.settled == 2


def test_native_main_menu_title_ignores_detached_hud_and_hidden_tutorial(game, monkeypatch):
    # Native header/footer and retained SaveIcon keys captured 2026-10-23.
    # Render flags and replay timing are synthetic producer evidence.
    game.main_menu = True
    original_menu = fake_menu_snapshot

    def menu(service):
        response, base = original_menu(service)
        if game.main_menu:
            response["texts"][0]["path"] = "X.MainMenu_Top_Window.WidgetTree.TopContent2.WidgetTree.Txt_MainTitle"
            response["texts"] += [
                {"path": "X.SaveIcon.WidgetTree.Txt_Info", "text": "$menu_save_load_0005_0001"},
                {"path": "X.BattleTutorial.WidgetTree.Txt_Title", "text": "$menu_tutorial_ui_0000_0001", "rendered": False},
            ]
        return response, base

    read = map_spot_travel.request_snapshot
    def roots(service, **kwargs):
        response, base = read(service, **kwargs)
        if "uiroots" in kwargs.get("flags", ()):
            response["ui_roots"]["widgets"].append({"class": "Command_C", "is_visible": True,
                "in_viewport": False, "details": {"in_viewport_ex": False}})
        return response, base
    monkeypatch.setattr(map_spot_travel, "menu_snapshot", menu)
    monkeypatch.setattr(map_spot_travel, "request_snapshot", roots)
    report = MapSpotTravel(FakeService(game), CATALOGUE).run("Kalm", execute=True)
    assert report["state"] == "arrived" and game.presses[0] == "cancel"


@pytest.mark.parametrize("bound", [True, False])
def test_native_sibling_menu_component_requires_rendered_text_owner(game, bound):
    from ff7r_agent.field_main_menu import identify_main_menu, NATIVE_TITLE_SUFFIX
    game.main_menu = True
    service = FakeService(game)
    menu, base = fake_menu_snapshot(service)
    menu["texts"][0].update(path="X" + NATIVE_TITLE_SUFFIX, rendered=True,
                            full_id="EndTextBlock /Temp/NativeMenu.WidgetTree.Txt_MainTitle" if bound else "EndTextBlock /Temp/Other.WidgetTree.Txt_MainTitle")
    roots = main_menu_roots(game)
    roots["widgets"].append({"class": "MainMenu_Top_Window_C", "path": "MainMenu_Top_Window_C /Temp/NativeMenu",
        "configured_root": False, "is_visible": True, "in_viewport": False, "details": {"in_viewport_ex": True}})
    cinema = {"observed": True, "active": False, "anim_cutscene": False, "playing": []}
    assert identify_main_menu(menu, roots, base, cinema)["identified"] is bound


def test_only_save_icon_checking_is_excluded_from_menu_signals():
    from ff7r_agent.save_slot_load import SaveMenuScreen
    icon = {"path": "X.SaveIcon.WidgetTree.Txt_Info", "text": "$menu_save_load_0005_0001"}
    assert SaveMenuScreen({"texts": [icon]}, []).signals == set()
    checking = {**icon, "path": "X.MainMenu_SaveLoad.WidgetTree.Txt_Info"}
    assert SaveMenuScreen({"texts": [icon, checking]}, []).signals == {"checking"}


def test_attachment_only_diagnostics_use_the_recovery_reader_without_input(monkeypatch):
    from ff7r_agent.service import AgentService
    from ff7r_agent import map_query
    calls = []
    def read(service, **kwargs):
        calls.append(kwargs)
        return {"ui_roots": {"observed": True}, "request_id": "diagnostic", "source_sequence": 1}, {}
    monkeypatch.setattr(map_query, "request_snapshot", read)
    result = AgentService.read_ui_roots(object.__new__(AgentService), attachments_only=True)
    assert result["input_sent"] is False and result["input_authorized"] is False
    assert calls == [{"heights": False, "flags": ("uiroots", "uirootdetails", "uiownerattachments"), "require_map": False}]


@pytest.mark.parametrize("blocker", [None, "unknown_modal", "combat"])
def test_native_owner_projection_identifies_only_the_field_main_menu(game, blocker):
    import json
    from pathlib import Path
    from ff7r_agent.field_main_menu import identify_main_menu
    projection = json.loads((Path(__file__).parent / "fixtures/field-main-menu-native.json").read_text(encoding="utf-8"))
    game.main_menu = True
    base = FakeService(game).observe()
    if blocker == "combat":
        base["in_combat"] = True
    if blocker == "unknown_modal":
        projection["roots"]["widgets"].append({"class": "UnknownModal_C", "is_visible": True,
            "in_viewport": False, "details": {"in_viewport_ex": True}})
    cinema = {"observed": True, "active": False, "anim_cutscene": False, "playing": []}
    result = identify_main_menu({"texts": projection["texts"]}, projection["roots"], base, cinema)
    assert result["identified"] is (blocker is None), result


@pytest.mark.parametrize("owner", ["BattleTips_C", "BattleTutorial_C", "MainMenu_Tutorial_Window_C",
    "TalkMenu_00_C", "Subtitle00_C", "Shop_C", "CardGame_Top_C", "MainMenu_Card_Top_C",
    "Pause_00_C", "Cinema_Skip_Btn_C", "Com_Window_01_C"])
def test_another_attached_screen_owner_is_never_cancelled(game, monkeypatch, owner):
    game.main_menu = True
    def snapshot(service, **kwargs):
        roots = main_menu_roots(game)
        roots["widgets"].append({"class": owner, "configured_root": True,
                                 "details": {"in_viewport_ex": True}})
        return recovery_response(service, ui_roots=roots), service.observe()
    monkeypatch.setattr(map_spot_travel, "request_snapshot", snapshot)
    with pytest.raises(MissionError, match="main_menu_open") as exc:
        MapSpotTravel(FakeService(game), CATALOGUE).run("Kalm", execute=True)
    assert game.presses == []
    assert exc.value.response["menu_recovery"][-1]["owner"] == owner


@pytest.mark.parametrize("fault", ["unknown_pause", "text_only", "multiple_menus", "unknown_root",
    "unknown_attachment", "partial_scan", "truncated_scan", "missing_header", "ambiguous_footer",
    "submenu", "other_text", "stale_read", "slow_read", "wrong_source", "sequence_regression",
    "changed_map", "changed_actor", "simulation", "combat", "loading", "qb", "held_card",
    "cutscene", "unknown_cutscene", "visible_unknown_child", "text_cap", "missing_root_path"])
def test_unknown_or_conflicting_menu_evidence_stops_without_input(game, monkeypatch, fault):
    game.main_menu = True
    service = FakeService(game)
    roots = main_menu_roots(game)
    menu, _ = fake_menu_snapshot(service)
    base = service.observe()
    cinema = service.cutscene_state()
    if fault == "unknown_pause": game.main_menu = False; base.update(paused=True, in_menu=False); menu["texts"] = []
    if fault == "text_only": roots["widgets"] = []
    if fault == "multiple_menus": roots["widgets"].append(dict(roots["widgets"][0]))
    if fault == "unknown_root": roots["widgets"].append({"class": "UnknownMenu_C", "in_viewport": True})
    if fault == "visible_unknown_child": roots["widgets"].append({"class": "UnknownMenu_C", "in_viewport": False, "is_visible": True})
    if fault == "missing_root_path": roots["widgets"][0].pop("path")
    if fault == "text_cap": menu["texts"] += [{"path": "X.Text", "text": ""}] * 400
    if fault == "unknown_attachment": roots["widgets"][1]["details"]["in_viewport_ex"] = None
    if fault == "partial_scan": roots["configured_complete"] = False
    if fault == "truncated_scan": roots["truncated"] = True
    if fault == "missing_header": menu["texts"].pop(0)
    if fault == "ambiguous_footer": menu["texts"].append({"text": "$help_message_0000_0010"})
    if fault == "submenu": menu["texts"].append({"text": "$help_message_0008_0001"})
    if fault == "other_text": menu["texts"].append({"path": "X.BattleTips.WidgetTree.Txt_Title", "text": "$tutorial_Fld_01_0070"})
    if fault == "stale_read": menu["timestamp_utc"] = (service._wall_clock() - timedelta(seconds=10)).isoformat()
    if fault == "wrong_source": menu["source"] = "simulation"
    if fault == "sequence_regression": menu["source_sequence"] = 0
    if fault == "simulation": base["source"] = "simulation"
    if fault == "combat": base["in_combat"] = True
    if fault == "loading": base["loading"] = True
    if fault == "qb": base["queen_blood"]["match_screen"] = True
    if fault == "held_card": base["queen_blood"]["placement"] = {"active": True}
    if fault == "cutscene": cinema["active"] = True
    if fault == "unknown_cutscene": cinema["observed"] = False
    service.observe = lambda: base
    service.cutscene_state = lambda: cinema
    def menu_read(_):
        if fault == "slow_read": service.now += 4
        if fault == "changed_map": base["map_id"] = "changed"
        if fault == "changed_actor": base["player"] = {**base["player"], "actor_id": "changed"}
        return menu, dict(base)
    def snapshot(service, **kwargs):
        return recovery_response(service, ui_roots=roots), dict(base)
    monkeypatch.setattr(map_spot_travel, "menu_snapshot", menu_read)
    monkeypatch.setattr(map_spot_travel, "request_snapshot", snapshot)
    with pytest.raises(MissionError, match="main_menu_open|menu_or_pause_unidentified") as exc:
        MapSpotTravel(service, CATALOGUE).run("Kalm", execute=True)
    assert game.presses == [] and not service._action_lock.locked()
    assert exc.value.response["menu_recovery"][-1]["identified"] is False


def test_a_screen_owner_change_after_cancel_stops_retries(game, monkeypatch):
    game.main_menu, game.menu_ignores = True, 1
    service = FakeService(game)
    def snapshot(service, **kwargs):
        roots = main_menu_roots(game)
        if game.presses:
            roots["widgets"][1]["details"]["in_viewport_ex"] = True
        return recovery_response(service, ui_roots=roots), service.observe()
    monkeypatch.setattr(map_spot_travel, "request_snapshot", snapshot)
    with pytest.raises(MissionError, match="main_menu_open") as exc:
        MapSpotTravel(service, CATALOGUE).run("Kalm", execute=True)
    assert game.presses == ["cancel"] and service.settled == 0
    assert len(exc.value.response["menu_recovery"]) == 2


def test_travel_leaves_a_busy_input_owner_and_its_lock_intact(game):
    service = FakeService(game)
    service._action_lock.acquire()
    try:
        with pytest.raises(MissionError, match="another input action"):
            MapSpotTravel(service, CATALOGUE).run("Kalm", execute=True)
        assert service._action_lock.locked() and game.presses == []
    finally:
        service._action_lock.release()


def test_travel_holds_input_ownership_through_settling_and_arrival(game):
    service = FakeService(game)
    def settle(timeout):
        assert service._action_lock.locked()
        assert not service._action_lock.acquire(blocking=False)  # watcher must skip
        return {"ok": True}
    service.wait_for_settled_gameplay = settle
    assert MapSpotTravel(service, CATALOGUE).run("Kalm", execute=True)["state"] == "arrived"
    assert not service._action_lock.locked()


@pytest.mark.parametrize("gate", [None, "stopped", "unarmed", "stale", "simulation", "process", "foreground"])
def test_menu_recovery_uses_real_service_guards_with_fake_backend(game, gate):
    # Uses AgentService's actual guards; never constructs a real input device.
    from test_agent import FixedTelemetry, make_real_service, FakeArm, FakeEnvironment, StepClock
    from ff7r_agent.models import Character, Vec3
    from ff7r_agent.service import SafetyError
    game.main_menu = True
    state = dict(paused=True, in_menu=True, map_id="map1200",
                 player=Character("Cloud", 10, 10, 5, 5, position=Vec3(0, 0, 0), actor_id="CloudActor"))
    if gate == "stale": state["timestamp_utc"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    if gate == "simulation": state["source"] = "simulation"
    arm = FakeArm(armed=gate != "unarmed", stopped=gate == "stopped")
    service, backend = make_real_service(telemetry=FixedTelemetry(**state), arm=arm,
        environment=FakeEnvironment(process=gate != "process", foreground=gate != "foreground"),
        monotonic=StepClock(), sleeper=lambda _: None, settled_gameplay_samples=3)
    service.game = game
    service.cutscene_state = lambda: {"observed": True, "active": False, "anim_cutscene": False, "playing": []}
    # No settled field proof exists on this paused frame; Cancel must be
    # admitted only by the menu guard, without weakening the field gate.
    travel = MapSpotTravel(service, CATALOGUE)
    if gate:
        with pytest.raises((MissionError, SafetyError)):
            travel.run("Kalm", execute=True)
        assert backend.actions == []
    else:
        tap = backend.tap
        def close(button, milliseconds):
            assert service._action_lock.locked()
            tap(button, milliseconds)
            service.telemetry.state = replace(service.telemetry.state, paused=False, in_menu=False)
            game.main_menu = False
        backend.tap = close
        # Exercise recovery inside the real service's action lock without
        # faking a successful load/arrival or sending a real controller press.
        with service._action_lock:
            travel.close_main_menu()
        assert backend.actions == [("tap", "cancel", 400)]
        assert travel.presses[0]["response"]["ok"]
    assert service._settled_gameplay_identity is None
    assert not service._action_lock.locked()


@pytest.mark.parametrize("failure", ["raises", "unverified"])
def test_ambiguous_cancel_result_is_not_replayed(game, failure):
    game.main_menu = True
    service = FakeService(game)
    def tap(button, milliseconds):
        game.press(button)  # The press landed, regardless of its receipt.
        if failure == "raises": raise MissionError("lost_cancel_receipt")
        return {"ok": False}
    service.tap = tap
    with pytest.raises(MissionError) as exc:
        MapSpotTravel(service, CATALOGUE).run("Kalm", execute=True)
    assert game.presses == ["cancel"]
    assert exc.value.response["presses"][0]["outcome"] == "unverified"
    assert not service._action_lock.locked()


def test_missing_native_diagnostics_returns_supported_main_menu_blocker(game, monkeypatch):
    game.main_menu = True
    service = FakeService(game)
    read = map_spot_travel.request_snapshot
    def snapshot(*args, **kwargs):
        if "uiroots" in kwargs.get("flags", ()):
            raise MissionError("map_query_timeout")
        return read(*args, **kwargs)
    monkeypatch.setattr(map_spot_travel, "request_snapshot", snapshot)
    with pytest.raises(MissionError, match="main_menu_open") as exc:
        MapSpotTravel(service, CATALOGUE).run("Kalm", execute=True)
    assert game.presses == []
    assert exc.value.response["menu_recovery"][-1]["reason"] == "map_query_timeout"
    assert "capture_game_window" in exc.value.response["recovery"]


def test_menu_disappearing_during_identification_never_gets_a_field_cancel(game, monkeypatch):
    game.main_menu = True
    service = FakeService(game)
    def menu(service):
        game.main_menu = False
        return fake_menu_snapshot(service)
    monkeypatch.setattr(map_spot_travel, "menu_snapshot", menu)
    assert MapSpotTravel(service, CATALOGUE).run("Kalm", execute=True)["state"] == "arrived"
    assert game.presses[0] == "map" and "cancel" not in game.presses


@pytest.mark.parametrize("execute", [True, False])
def test_cached_map_widget_never_overrides_a_main_menu_with_another_owner(game, monkeypatch, execute):
    # request_snapshot documents that a closed map widget can linger. A
    # conflicting Main Menu flag must trigger owner checks before map keys,
    # including the select-only dry run, which still sends menu input.
    game.open, game.main_menu = True, True
    read = map_spot_travel.request_snapshot
    def snapshot(service, **kwargs):
        if "uiroots" not in kwargs.get("flags", ()):
            return read(service, **kwargs)
        roots = main_menu_roots(game)
        roots["widgets"][1]["details"]["in_viewport_ex"] = True
        return recovery_response(service, ui_roots=roots), service.observe()
    monkeypatch.setattr(map_spot_travel, "request_snapshot", snapshot)
    with pytest.raises(MissionError, match="main_menu_open"):
        MapSpotTravel(FakeService(game), CATALOGUE).run("Kalm", execute=execute)
    assert game.presses == []
