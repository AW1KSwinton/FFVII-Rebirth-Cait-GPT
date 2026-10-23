import asyncio
import copy

import pytest

from ff7r_agent.map_fast_travel import fast_travel_via_map
from ff7r_agent.map_query import MapView, _remember
from ff7r_agent.navigation_mission import MissionConfig, NavigationMission
from ff7r_agent.navigation_values import MissionError
from map_fixtures import PLAYER, Projection, base_sample, icon, response

STOP = (40000.0, -25000.0)


class MapGame:
    """A map whose cursor moves 0.2 px/ms; W (y=+1) moves it up the screen."""

    def __init__(self, *, kind="ChocoboCarriage", prompt_works=True, cursor_moves=True, travel=True):
        self.projection = Projection(scale=0.004, degrees=10)
        self.cursor = list(self.projection.local(PLAYER["x"], PLAYER["y"]))
        self.player = dict(PLAYER)
        self.kind, self.prompt_works, self.cursor_moves, self.travel = kind, prompt_works, cursor_moves, travel
        self.prompt = False
        self.inputs = []
        self.now = 0.0

    def _sleeper(self, seconds):
        self.now += seconds

    def icons(self):
        return [icon(self.projection, "Icon_NaviMap_C.Stop", STOP, properties={"IconKind": self.kind}, size=20)]

    def read(self, _service):
        return MapView(response(self.projection, icons=self.icons(), cursor=tuple(self.cursor), prompt=self.prompt),
                       base_sample(player=self.player))

    def observe(self):
        return base_sample(player=self.player)

    def _map_cursor_pulse(self, x, y, ms):
        self.inputs.append(("pulse", x, y, ms))
        if self.cursor_moves:
            self.cursor[0] += 0.2 * ms * x
            self.cursor[1] -= 0.2 * ms * y

    def _tap_map_control(self, button, ms, action):
        self.inputs.append(("map", button))
        self.prompt = self.prompt_works

    def tap(self, button, ms):
        self.inputs.append(("tap", button))
        if self.travel:
            self.player = {"x": STOP[0] + 1500, "y": STOP[1], "z": 300}

    def wait_for_settled_gameplay(self, **_kwargs):
        return {"ok": True}


def pin_target(game):
    view = game.read(game)
    pin = view.pins[0]
    return _remember(game, view, source="map_pin", label=pin["kind"], world=pin["world"],
                     map_local=pin["map_local"], extra={"pin_id": pin["pin_id"], "kind": pin["kind"],
                                                        "category": pin["category"],
                                                        "fast_travel_candidate": pin["fast_travel_candidate"]})


def run(game, **kwargs):
    return fast_travel_via_map(game, pin_target(game)["target_id"], read=game.read, clock=lambda: game.now, **kwargs)


def test_dry_run_sends_no_input():
    game = MapGame()
    result = run(game)
    assert result["ok"] and result["state"] == "PREFLIGHT_PASSED" and game.inputs == []
    assert result["start_cursor_error_px"] > 100


def test_cursor_is_steered_onto_the_pin_then_prompt_is_confirmed():
    game = MapGame()
    result = run(game, execute=True)
    assert result["ok"] and result["state"] == "ARRIVED", result
    assert result["distance_to_pin_m"] == pytest.approx(15)
    assert result["cursor_trace"][-1]["error_px"] <= result["cursor_trace"][-1]["tolerance_px"]
    assert [i for i in game.inputs if i[0] != "pulse"] == [("map", "map_fast_travel"), ("tap", "confirm")]


def test_missing_prompt_stops_before_confirm():
    game = MapGame(prompt_works=False)
    result = run(game, execute=True)
    assert result == {**result, "ok": False, "reason": "fast_travel_prompt_not_shown"}
    assert ("tap", "confirm") not in game.inputs


def test_unresponsive_cursor_stops_after_probing():
    game = MapGame(cursor_moves=False)
    result = run(game, execute=True)
    assert result["reason"] == "map_cursor_not_responding"
    assert [i[0] for i in game.inputs] == ["pulse", "pulse"]


def test_arrival_must_be_near_the_pin():
    game = MapGame()
    game.tap = lambda button, ms: (game.inputs.append(("tap", button)),
                                   game.player.update(x=-90000.0))
    assert run(game, execute=True)["reason"] == "fast_travel_arrival_outside_radius"


def test_only_fast_travel_pins_are_eligible_by_default():
    game = MapGame(kind="Talk")
    assert run(game)["reason"] == "map_pin_not_fast_travel_candidate"
    assert run(game, require_fast_travel_icon=False)["state"] == "PREFLIGHT_PASSED"


def test_navigation_mission_uses_a_map_target_as_its_goal(tmp_path):
    target = {"target_id": "b" * 16, "map_id": "EndGame", "label": "Talk",
              "position": {"x": 1500.0, "y": 2600.0, "z": 222.0}}

    async def rpc(operation, arguments):
        assert operation == "get_map_target" and arguments == {"target_id": "b" * 16}
        return {"ok": True, "target": copy.deepcopy(target)}

    mission = NavigationMission(rpc, tmp_path / "out", identity_check=lambda *_: {"ok": True},
                                config=MissionConfig(map_target_id="b" * 16))
    (tmp_path / "out").mkdir()
    mission.deadline = float("inf")
    report = {}
    goal = asyncio.run(mission.resolve_mission_goal({"map_id": "EndGame"}, report))
    assert goal["position"] == target["position"] and report["map_target"]["label"] == "Talk"
    target["position"]["z"] = None
    with pytest.raises(MissionError, match="z_unresolved"):
        asyncio.run(mission.resolve_mission_goal({"map_id": "EndGame"}, {}))
    with pytest.raises(MissionError, match="map_mismatch"):
        asyncio.run(mission.resolve_mission_goal({"map_id": "Other"}, {}))
    with pytest.raises(ValueError, match="cannot be combined"):
        MissionConfig(map_target_id="a" * 16, target_position=(1, 2, 3), target_map_id="EndGame")


def test_service_cursor_pulses_only_while_the_map_is_open():
    from dataclasses import replace
    from ff7r_agent.input import SimulationInput
    from ff7r_agent.runtime_commands import SERVICE_ALIASES
    from ff7r_agent.service import AgentService, SafetyError
    from ff7r_agent.telemetry import SimulationTelemetry

    class MapOpen(SimulationTelemetry):
        def observe(self):
            return replace(super().observe(), paused=True, in_menu=True)

    service = AgentService(MapOpen(), SimulationInput())
    assert service._map_cursor_pulse(0.0, 1.0, 120)["action"] == "map_cursor_pulse"
    assert service.input.actions[-1].parameters == {"x": 0.0, "y": 1.0, "milliseconds": 120}
    with pytest.raises(SafetyError, match="requires a direction"):
        service._map_cursor_pulse(0.0, 0.0, 120)
    with pytest.raises(SafetyError, match="map to be open"):
        AgentService(SimulationTelemetry(), SimulationInput())._map_cursor_pulse(1.0, 0.0, 120)
    service._tap_map_control("map_fast_travel", 500, "map_fast_travel")
    assert service.input.actions[-1].parameters["button"] == "map_fast_travel"
    for operation in ("observe_map_pins", "map_point_to_world", "get_map_target", "fast_travel_via_map"):
        assert callable(getattr(service, SERVICE_ALIASES[operation]))
