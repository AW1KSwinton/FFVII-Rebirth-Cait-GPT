import json
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
            if button == "queen_blood_cancel":
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

    def observe(self) -> dict:
        return {
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

    def _map_menu_key(self, button: str, milliseconds: int) -> None:
        self.game.press(button)

    def wait_for_settled_gameplay(self, timeout: float) -> dict:
        self.settled += 1
        return {"ok": True}


@pytest.fixture
def game(monkeypatch):
    game = FakeMap()
    monkeypatch.setattr(map_spot_travel, "request_snapshot",
                        lambda service, heights, flags: (game.response(), {}))
    return game


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
    assert game.presses[:2] == ["queen_blood_cancel", "map"]


def test_a_main_menu_that_ignores_quick_back_presses_still_closes(game) -> None:
    # Live after a harness restart (#354): three Esc presses reached the game
    # and the menu stayed open; the same key closed it later.
    game.main_menu = True
    game.menu_ignores = 4
    report = MapSpotTravel(FakeService(game), CATALOGUE).run("Kalm", execute=True)
    assert report["state"] == "arrived"
    assert game.presses[:6] == ["queen_blood_cancel"] * 5 + ["map"]


def test_a_main_menu_that_never_closes_fails_without_the_map_key(game) -> None:
    from ff7r_agent.map_spot_travel import MENU_CLOSE_SETTLES_SECONDS

    game.main_menu = True
    game.menu_ignores = 100
    with pytest.raises(MissionError, match="main_menu_did_not_close"):
        MapSpotTravel(FakeService(game), CATALOGUE).run("Kalm", execute=True)
    assert game.presses == ["queen_blood_cancel"] * len(MENU_CLOSE_SETTLES_SECONDS)


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
    assert game.presses[0] == "queen_blood_cancel" and game.presses[1] == "map"

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
