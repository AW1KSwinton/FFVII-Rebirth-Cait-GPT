from __future__ import annotations

from ff7r_agent.explore_mode import DOOR_APPROACH_M, door_on_path
from ff7r_agent.models import Vec3


def test_finds_the_first_door_the_path_passes_and_stops_short_of_it() -> None:
    # Live, Nibelheim inn: the route from the upstairs landing to the square
    # runs through the front door at (-307.7, -2144.6) m.
    path = [Vec3(-31760, -214555, 4907), Vec3(-31000, -214500, 4380), Vec3(-30000, -214300, 4380),
            Vec3(-30857, -212969, 4340)]
    doors = [Vec3(-30770, -214460, 4300), Vec3(-29440, -213210, 4300), Vec3(-31770, -214740, 4820)]
    door, approach = door_on_path(path, doors)
    assert (door.x, door.y) == (-30770, -214460)
    gap = ((door.x - approach.x) ** 2 + (door.y - approach.y) ** 2) ** 0.5
    assert abs(gap - DOOR_APPROACH_M * 100) < 30


def test_ignores_doors_off_the_path_or_on_another_floor() -> None:
    path = [Vec3(0, 0, 0), Vec3(1000, 0, 0)]
    assert door_on_path(path, [Vec3(500, 400, 0)]) is None
    assert door_on_path(path, [Vec3(500, 0, 500)]) is None


def test_a_door_at_the_start_approaches_from_where_the_player_stands() -> None:
    path = [Vec3(0, 0, 0), Vec3(1000, 0, 0)]
    door, approach = door_on_path(path, [Vec3(50, 20, 0)])
    assert (approach.x, approach.y) == (0, 0)


def test_a_door_beside_the_path_is_not_on_it() -> None:
    # Live: the upstairs landing (-317.6, -2145.6) passes 1.8 m from the inn
    # room door (-317.7, -2147.4) on its way down to the front door.
    path = [Vec3(-31760, -214560, 4907), Vec3(-31500, -214560, 4907)]
    assert door_on_path(path, [Vec3(-31770, -214740, 4820)]) is None


class Vaulter:
    """Cloud before a log he can only cross with Terrain Action."""

    def __init__(self, offered=True):
        self.position = {"x": 5063.0, "y": -216584.0, "z": 2126.0}
        self.offered = offered
        self.moves = []
        self.pressed = 0

    def observe(self):
        return {"map_id": "EndGame", "running": True, "telemetry": {"live": True},
                "camera": {"rotation": {"yaw": 0.0}},
                "player": {"position": dict(self.position), "rotation": {"yaw": 0.0}}}

    def move(self, x, y, milliseconds):
        self.moves.append((x, y, milliseconds))

    def terrain_action(self, milliseconds, settle_seconds):
        self.pressed += 1
        self.position["x"] += 250.0
        return {"ok": True}


def test_a_link_not_crossed_is_vaulted_when_terrain_action_is_offered(monkeypatch):
    from ff7r_agent import explore_mode
    monkeypatch.setattr(explore_mode, "_compact_route", lambda result: {
        "traversal_crossings": [{"outcome": "traversal_link_not_crossed", "link_class": "step_over"}],
        "traversal_commands": [{"heading": 141.1}]})
    monkeypatch.setattr(explore_mode.time, "sleep", lambda seconds: None)
    cloud = Vaulter()
    monkeypatch.setattr(explore_mode, "_read_prompt", lambda service: {"terrain_action": cloud.offered})
    record = explore_mode.terrain_action_on_way(cloud, {})
    assert record["opened"] is True and record["open_outcome"] == "terrain_action_crossed"
    assert cloud.pressed == 1 and len(cloud.moves) == 1 and cloud.moves[0][2] == explore_mode.TERRAIN_FACE_TAP_MS


def test_no_vault_without_the_prompt(monkeypatch):
    from ff7r_agent import explore_mode
    monkeypatch.setattr(explore_mode, "_compact_route", lambda result: {
        "traversal_crossings": [{"outcome": "traversal_link_not_crossed"}], "traversal_commands": [{"heading": 90.0}]})
    monkeypatch.setattr(explore_mode, "_read_prompt", lambda service: {"terrain_action": False})
    cloud = Vaulter()
    assert explore_mode.terrain_action_on_way(cloud, {}) is None
    assert cloud.pressed == 0 and cloud.moves == []



class Squeezer:
    """Cloud in a Mt. Nibel squeeze (live 2026-10-20): not grounded, movement
    mode -1, still until the stick is held; out after three pulses."""

    def __init__(self, stuck=False):
        self.x, self.pulses, self.stuck, self.swimming = 26542.0, 0, stuck, []

    def observe(self):
        squeezing = self.pulses < 3
        return {"map_id": "EndGame", "running": True, "telemetry": {"live": True},
                "camera": {"rotation": {"yaw": 180.0}},
                "player": {"position": {"x": self.x, "y": -243254.0, "z": 13600.0}, "rotation": {"yaw": 180.0},
                           "locomotion": {"feet_position": {"x": self.x, "y": -243254.0, "z": 13512.0},
                                          "grounded": not squeezing, "movement_mode": -1 if squeezing else 1}}}

    def move(self, x, y, milliseconds, swimming=False):
        self.swimming.append(swimming)
        if not self.stuck:
            self.x -= 140.0
            self.pulses += 1


def test_a_squeeze_is_pushed_through_along_clouds_facing():
    from ff7r_agent import explore_mode
    cloud = Squeezer()
    record = explore_mode.squeeze_on_way(cloud)
    assert record["opened"] is True and record["open_outcome"] == "squeeze_crossed" and record["pulses"] == 3
    assert all(cloud.swimming)


def test_no_squeeze_no_push_and_a_stuck_one_stops():
    from ff7r_agent import explore_mode
    grounded = Squeezer()
    grounded.pulses = 3
    assert explore_mode.squeeze_on_way(grounded) is None and grounded.swimming == []
    stuck = Squeezer(stuck=True)
    record = explore_mode.squeeze_on_way(stuck)
    assert record["opened"] is False and record["open_outcome"] == "squeeze_no_progress" and len(stuck.swimming) == 1



def test_the_vault_prompt_is_looked_for_at_facings_either_side_of_the_link(monkeypatch):
    from ff7r_agent import explore_mode
    monkeypatch.setattr(explore_mode, "_compact_route", lambda result: {
        "traversal_crossings": [{"outcome": "traversal_link_not_crossed", "link_class": "step_over"}],
        "traversal_commands": [{"heading": 73.0}]})
    monkeypatch.setattr(explore_mode.time, "sleep", lambda seconds: None)
    cloud = Vaulter()
    reads = iter([True, False, False, True])  # offered on arrival, then only at the third facing
    monkeypatch.setattr(explore_mode, "_read_prompt", lambda service: {"terrain_action": next(reads)})
    record = explore_mode.terrain_action_on_way(cloud, {})
    assert record["opened"] is True and record["facing_offset_degrees"] == -30.0 and len(cloud.moves) == 3
