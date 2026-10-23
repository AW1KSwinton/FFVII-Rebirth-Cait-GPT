import json
import asyncio
from types import SimpleNamespace

import pytest

from ff7r_agent import field_prompt
from ff7r_agent.models import Vec3
from ff7r_agent.navigation_values import MissionError
from ff7r_agent.npc_approach import approach_npc, approach_point, face_until_talk


def fake_control_lease(rpc):
    """Give old generic route fakes an explicit in-memory lease protocol."""
    lease = {"active": False, "client_id": None}
    async def call(operation, arguments):
        result = await rpc(operation, arguments)
        if result == {"ok": True}:
            if operation == "get_control_session_status":
                return dict(lease)
            if operation == "open_control_session":
                lease.update(active=True, client_id=arguments.get("client_id"))
                return dict(lease)
            if operation == "close_control_session":
                lease.update(active=False, client_id=None)
        return result
    return call


class FakeGraph:
    def __init__(self, waypoints, status="complete"):
        self.waypoints = [Vec3(*point) for point in waypoints]
        self.status = status

        self.end_overrides = []
        self.start_overrides = []

    def resolve_point(self, point, **_):
        return SimpleNamespace(ok=False)

    def plan(self, start, end, start_resolution_override=None, end_resolution_override=None):
        self.start_overrides.append(start_resolution_override)
        self.end_overrides.append(end_resolution_override)
        return SimpleNamespace(status=self.status, reason_code="route_plan_complete",
                               waypoints=tuple(self.waypoints), total_estimated_path_length_m=12.0)


def test_approach_resolves_the_npc_near_their_feet_before_a_roof_overhead():
    near = SimpleNamespace(ok=True, reference="street")
    bounds = []

    class Graph(FakeGraph):
        def resolve_point(self, point, max_vertical_separation_m=20.0, **_):
            bounds.append(max_vertical_separation_m)
            return near

    graph = Graph([(0, 0, 0), (1000, 0, 0)])
    approach_point(graph, {"x": 0, "y": 0, "z": 0}, {"x": 1000, "y": 0, "z": 0}, 2.0)
    assert bounds == [2.0, 1.0] and graph.end_overrides == [near]


def test_approach_starts_on_the_floor_at_clouds_feet():
    # Live 2026-09-30 by Nene in Kalm: unbounded, Cloud's start resolved to
    # a six-polygon island 3 m below him and no approach could be planned.
    feet = SimpleNamespace(ok=True, reference="floor")
    calls = []

    class Graph(FakeGraph):
        def resolve_point(self, point, max_snap_distance_m=8.0, max_vertical_separation_m=20.0, **_):
            calls.append((point.x, max_snap_distance_m, max_vertical_separation_m))
            return feet if point.x == 0 else SimpleNamespace(ok=False)

    graph = Graph([(0, 0, 0), (1000, 0, 0)])
    approach_point(graph, {"x": 0, "y": 0, "z": 0}, {"x": 1000, "y": 0, "z": 0}, 2.0)
    assert (0, 0.75, 1.0) in calls and graph.start_overrides == [feet]


def test_approach_point_stops_short_along_the_route_not_the_straight_line():
    # The route turns a corner 1 m before the NPC; 2 m short lies on the first leg.
    graph = FakeGraph([(0, 0, 0), (1000, 0, 0), (1000, 100, 0)])
    point = approach_point(graph, {"x": 0, "y": 0, "z": 0}, {"x": 1000, "y": 100, "z": 0}, 2.0)
    assert point["position"] == {"x": 900.0, "y": 0.0, "z": 0.0}


def test_approach_point_when_already_close():
    graph = FakeGraph([(0, 0, 0), (100, 0, 0)])
    point = approach_point(graph, {"x": 0, "y": 0, "z": 0}, {"x": 100, "y": 0, "z": 0}, 2.0)
    assert point["already_within_stop_distance"]


def test_approach_point_refuses_an_unplannable_route():
    with pytest.raises(MissionError, match="npc_approach_unplannable"):
        approach_point(FakeGraph([], status="failed"), {"x": 0, "y": 0, "z": 0}, {"x": 1, "y": 0, "z": 0}, 2.0)


class FakeRuntime:
    """Talk appears once Cloud has been turned toward the NPC ``turns`` times."""

    def __init__(self, turns, distance_cm=280.0):
        self.turns, self.distance_cm, self.calls, self.sticks = turns, distance_cm, [], []

    async def rpc(self, operation, arguments):
        self.calls.append(operation)
        if operation == "read_field_prompt":
            talk = self.calls.count("move_stick") >= self.turns
            return {"commands": ["$level_action_talk"] if talk else [], "talk": talk,
                    "player_position": {"x": self.distance_cm, "y": 0.0, "z": 0.0}}
        if operation == "observe_game":
            return {"camera": {"rotation": {"yaw": 90.0}}}
        if operation == "move_stick":
            self.sticks.append((arguments["x"], arguments["y"]))
        return {"ok": True}


async def no_sleep(_):
    return None


def test_face_taps_toward_the_npc_relative_to_the_camera_until_talk():
    runtime = FakeRuntime(turns=1)
    result = asyncio.run(face_until_talk(runtime.rpc, {"x": 0, "y": 0, "z": 0}, sleep=no_sleep))
    assert result["ok"] and result["taps"] == 1
    assert runtime.calls == ["read_field_prompt", "align_camera_to_bearing", "read_field_prompt",
                             "observe_game", "move_stick", "read_field_prompt", "read_field_prompt",
                             "observe_game"]  # the body-facing check
    # The NPC is at bearing 180 and the camera faces 90; Unreal yaw grows
    # clockwise, so that is a quarter turn to the right: stick right.
    (x, y), = runtime.sticks
    assert x == pytest.approx(1.0) and y == pytest.approx(0.0, abs=1e-3)


def test_face_turns_the_camera_to_the_npc_first_and_stops_if_that_offers_talk():
    # Live: beside Zahira with the camera elsewhere, Talk appeared only once
    # the camera was turned to her.
    class CameraRuntime(FakeRuntime):
        async def rpc(self, operation, arguments):
            if operation == "align_camera_to_bearing":
                self.calls.append(operation)
                self.bearing = arguments["target_bearing_degrees"]
                self.turns = 0
                return {"ok": True}
            return await super().rpc(operation, arguments)

    runtime = CameraRuntime(turns=5)
    result = asyncio.run(face_until_talk(runtime.rpc, {"x": 0, "y": 0, "z": 0}, sleep=no_sleep))
    assert result["ok"] and result["taps"] == 0 and runtime.bearing == pytest.approx(180.0)
    assert "move_stick" not in runtime.calls


def test_face_sends_nothing_when_talk_is_already_offered():
    runtime = FakeRuntime(turns=0)
    asyncio.run(face_until_talk(runtime.rpc, {"x": 0, "y": 0, "z": 0}, sleep=no_sleep))
    assert runtime.calls == ["read_field_prompt", "read_field_prompt", "observe_game"]


def test_talk_offered_with_cloud_turned_away_turns_his_body_to_the_npc():
    # Live at Michaela's table (#171): Talk was offered 1.4 m away with
    # Cloud's body turned away, and starting the match then refused facing.
    import math
    from ff7r_agent.npc_approach import FACE_BODY_TOLERANCE_DEG

    class TurnedAway(FakeRuntime):
        yaw = 0.0  # facing +x; the NPC is at the origin, behind him
        sequence = 0

        async def rpc(self, operation, arguments):
            if operation == "observe_game":
                from datetime import datetime, timezone
                self.calls.append(operation)
                self.sequence += 1
                return {"sequence": self.sequence, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "running": True, "map_id": "EndGame",
                        "player": {"actor_id": "CloudFixture", "position": {"x": 280.0, "y": 0.0, "z": 0.0},
                                   "rotation": {"yaw": self.yaw}, "locomotion": {"grounded": True,
                                       "velocity_cm_s": {"x": 0, "y": 0, "z": 0}}},
                        "camera": {"rotation": {"yaw": 0.0}}}
            if operation == "move_stick":
                # A gentle tap turns him 70 degrees toward the stick heading.
                heading = math.degrees(math.atan2(arguments["x"], arguments["y"]))
                error = (heading - self.yaw + 180.0) % 360.0 - 180.0
                self.yaw += max(-70.0, min(70.0, error))
            return await super().rpc(operation, arguments)

    runtime = TurnedAway(turns=0)
    result = asyncio.run(face_until_talk(runtime.rpc, {"x": 0, "y": 0, "z": 0}, sleep=no_sleep))
    assert result["ok"] and 1 <= result["facing"]["taps"] <= 3
    assert abs(result["facing"]["heading_error_degrees"]) <= FACE_BODY_TOLERANCE_DEG
    assert runtime.calls[-1] == "read_field_prompt"  # Talk is checked again after turning


def test_face_keeps_closing_in_when_talk_flickers_at_the_edge_of_range():
    # Live at Kalm's item-shop counter: Talk offered at 2.83 m, then gone.
    class EdgeRuntime(FakeRuntime):
        reads = 0

        async def rpc(self, operation, arguments):
            if operation == "read_field_prompt" and self.calls.count("move_stick") < 2:
                self.calls.append(operation)
                self.reads += 1
                talk = self.reads == 1  # shown once, gone on the confirming read
                return {"commands": [], "talk": talk, "player_position": {"x": 280.0, "y": 0.0, "z": 0.0}}
            return await super().rpc(operation, arguments)

    runtime = EdgeRuntime(turns=0)
    result = asyncio.run(face_until_talk(runtime.rpc, {"x": 0, "y": 0, "z": 0}, sleep=no_sleep))
    assert result["ok"] and result["attempts"][0]["talk_lost"] and runtime.calls.count("move_stick") == 2


def test_face_refuses_to_tap_from_out_of_range():
    runtime = FakeRuntime(turns=1, distance_cm=900.0)
    with pytest.raises(MissionError, match="npc_out_of_talk_range"):
        asyncio.run(face_until_talk(runtime.rpc, {"x": 0, "y": 0, "z": 0}, sleep=no_sleep))
    assert "move_stick" not in runtime.calls


def test_field_prompt_reads_the_visible_action_text(monkeypatch):
    response = {"texts": [
        {"path": "X.FieldAction_Command.WidgetTree.Txt_Btn", "text": "<button=field-action>"},
        {"path": "X.FieldAction_Command.WidgetTree.Txt_Command", "text": "$level_action_talk"},
    ], "source_sequence": 7}
    monkeypatch.setattr(field_prompt, "request_snapshot",
                        lambda service, heights, flags: (response, {"player": {"position": {"x": 1}}}))
    result = field_prompt.read_field_prompt(object())
    assert result["talk"] and result["commands"] == ["$level_action_talk"]


def test_a_stalled_route_uses_the_offered_terrain_action_then_routes_again(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    runs = []

    class FakeMission:
        def __init__(self, rpc, output, identity_check, config, client_id=None):
            self.config = config

        async def run(self):
            runs.append(self.config.target_position)
            return {"ok": len(runs) > 1, "state": "ARRIVED" if len(runs) > 1 else "STOPPED",
                    "reason": None if len(runs) > 1 else "static_obstacle_replan_failed"}

    state = {"x": 0.0, "near": False}
    calls = []

    async def rpc(operation, arguments):
        calls.append(operation)
        if operation == "resolve_world_target":
            return {"status": "located", "position": {"x": 3000.0, "y": 0.0, "z": 88.0}}
        if operation == "observe_game":
            return {"player": {"locomotion": {"feet_position": {"x": 0.0, "y": 0.0, "z": 0.0}}}}
        if operation == "terrain_action":
            state["x"] = 1500.0  # vaulted forward
        if operation == "read_field_prompt":
            if len(runs) >= 2:
                return {"commands": ["$level_action_talk"], "talk": True,
                        "player_position": {"x": 2800.0, "y": 0.0, "z": 0.0}}
            return {"commands": ["$level_sys_show_parkour_input"], "talk": False,
                    "player_position": {"x": state["x"], "y": 0.0, "z": 0.0}}
        return {"ok": True}

    monkeypatch.setattr(npc_approach, "NavigationMission", FakeMission)
    monkeypatch.setattr(npc_approach, "load_covering_graph", lambda *a: (None, {"covers_target": True}))
    monkeypatch.setattr(npc_approach, "approach_point",
                        lambda *a: {"position": {"x": 2800.0, "y": 0.0, "z": 0.0}, "route_length_m": 28.0})

    async def no_sleep(_):
        return None

    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Nene", output=tmp_path, identity_check=None,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and report["state"] == "talk_offered"
    assert len(runs) == 2 and calls.count("terrain_action") == 1
    assert report["terrain_actions"][0]["moved_m"] == pytest.approx(15.0)
    assert [route["state"] for route in report["routes"]] == ["STOPPED", "ARRIVED"]


def test_a_route_stopped_at_a_door_opens_it_then_routes_on_without_using_an_attempt(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    runs = []

    class FakeMission:
        def __init__(self, rpc, output, identity_check, config, client_id=None):
            pass

        async def run(self):
            runs.append(1)
            done = len(runs) > npc_approach.MAX_ROUTE_ATTEMPTS
            return {"ok": done, "state": "ARRIVED" if done else "STOPPED", "reason": None if done else "door_ahead"}

    calls = []

    async def rpc(operation, arguments):
        calls.append(operation)
        if operation == "resolve_world_target":
            return {"status": "located", "position": {"x": 3000.0, "y": 0.0, "z": 88.0}}
        if operation == "observe_game":
            return {"player": {"locomotion": {"feet_position": {"x": 0.0, "y": 0.0, "z": 0.0}}}}
        if operation == "open_door":
            return {"ok": True, "prompt_cleared": True,
                    "samples": [{"position": {"x": 500.0 * len(runs) + 200.0, "y": 0.0, "z": 0.0}}]}
        if operation == "read_field_prompt":
            if len(runs) > npc_approach.MAX_ROUTE_ATTEMPTS:
                return {"commands": ["$level_action_talk"], "talk": True,
                        "player_position": {"x": 2800.0, "y": 0.0, "z": 0.0}}
            return {"commands": ["$level_action_door"], "door": True, "talk": False,
                    "player_position": {"x": 500.0 * len(runs), "y": 0.0, "z": 0.0}}
        return {"ok": True}

    monkeypatch.setattr(npc_approach, "NavigationMission", FakeMission)
    monkeypatch.setattr(npc_approach, "load_covering_graph", lambda *a: (None, {"covers_target": True}))
    monkeypatch.setattr(npc_approach, "approach_point",
                        lambda *a: {"position": {"x": 2800.0, "y": 0.0, "z": 0.0}, "route_length_m": 28.0})

    async def no_sleep(_):
        return None

    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Dale", output=tmp_path, identity_check=None,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    # Three doors in a row: each route that ends at one is not a failed attempt.
    assert report["ok"] and calls.count("open_door") == npc_approach.MAX_ROUTE_ATTEMPTS
    assert "extrication" not in report
    assert [door["route_reason"] for door in report["doors"]] == ["door_ahead"] * npc_approach.MAX_ROUTE_ATTEMPTS


def test_cross_door_turns_to_the_door_until_open_is_offered_then_opens_it():
    from ff7r_agent import npc_approach

    calls = []

    async def rpc(operation, arguments):
        calls.append(operation)
        if operation == "read_field_prompt":
            door = calls.count("move_stick") >= 1
            return {"commands": ["$level_action_door"] if door else [], "door": door, "talk": False,
                    "player_position": {"x": 0.0, "y": 0.0, "z": 0.0}}
        if operation == "observe_game":
            return {"camera": {"rotation": {"yaw": 0.0}}}
        if operation == "open_door":
            return {"ok": True, "prompt_cleared": True, "samples": [{"position": {"x": 200.0, "y": 0.0, "z": 0.0}}]}
        return {"ok": True}

    door = {"actor": "BG0012_00_Door1Knob_Standard_C x", "centre": {"x": 100.0, "y": 0.0, "z": 0.0}}
    crossed = asyncio.run(npc_approach.cross_door(rpc, door, sleep=no_sleep))
    assert calls == ["read_field_prompt", "align_camera_to_bearing", "read_field_prompt", "observe_game",
                     "move_stick", "read_field_prompt", "open_door"]
    assert crossed["moved_m"] == pytest.approx(2.0) and crossed["prompt_cleared"]


def test_an_approach_across_doors_routes_to_each_door_opens_it_then_goes_on(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    goals = []

    class FakeMission:
        def __init__(self, rpc, output, identity_check, config, client_id=None):
            self.config = config

        async def run(self):
            goals.append(self.config.target_position[0])
            return {"ok": True, "state": "ARRIVED", "reason": None}

    doors = [{"actor": "BG0012 street", "centre": {"x": 1000.0, "y": 0.0, "z": 0.0},
              "front": {"x": 900.0, "y": 0.0, "z": 0.0}, "beyond": {"x": 1100.0, "y": 0.0, "z": 0.0}},
             {"actor": "BG0002 room", "centre": {"x": 2000.0, "y": 0.0, "z": 0.0},
              "front": {"x": 1900.0, "y": 0.0, "z": 0.0}, "beyond": {"x": 2100.0, "y": 0.0, "z": 0.0}}]
    opened = []

    async def rpc(operation, arguments):
        if operation == "resolve_world_target":
            return {"status": "located", "position": {"x": 2800.0, "y": 0.0, "z": 88.0}}
        if operation == "observe_game":
            return {"player": {"locomotion": {"feet_position": {"x": 0.0, "y": 0.0, "z": 0.0}}},
                    "camera": {"rotation": {"yaw": 0.0}}}
        if operation == "plan_npc_approach":
            return {"library": {}, "doors": doors,
                    "approach": {"position": {"x": 2600.0, "y": 0.0, "z": 0.0}, "route_length_m": 5.0}}
        if operation == "open_door":
            opened.append(len(goals))
            return {"ok": True, "prompt_cleared": True, "samples": [{"position": {"x": 0.0, "y": 0.0, "z": 0.0}}]}
        if operation == "read_field_prompt":
            if len(goals) == 3:
                return {"commands": ["$level_action_talk"], "talk": True,
                        "player_position": {"x": 2600.0, "y": 0.0, "z": 0.0}}
            front = doors[len(goals) - 1]["front"]
            return {"commands": ["$level_action_door"], "door": True, "talk": False, "player_position": front}
        return {"ok": True}

    monkeypatch.setattr(npc_approach, "NavigationMission", FakeMission)
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Dale", output=tmp_path, identity_check=None,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and goals == [900.0, 1900.0, 2600.0] and opened == [1, 2]
    assert [crossing["actor"] for crossing in report["door_crossings"]] == ["BG0012 street", "BG0002 room"]


def test_an_open_doorway_or_a_door_since_opened_is_walked_through_without_a_prompt():
    from ff7r_agent import npc_approach

    for door_open in (True, False):
        feet = {"x": 0.0}
        calls = []

        async def rpc(operation, arguments):
            calls.append(operation)
            if operation == "move_stick":
                feet["x"] += 50.0 * arguments["y"]  # camera yaw 90: +y on the stick is +x in the world
            if operation == "observe_game":
                return {"player": {"locomotion": {"feet_position": {"x": feet["x"], "y": 0.0, "z": 0.0}}},
                        "camera": {"rotation": {"yaw": 0.0}}}
            if operation == "read_field_prompt":
                return {"commands": [], "door": False, "talk": False,
                        "player_position": {"x": feet["x"], "y": 0.0, "z": 0.0}}
            return {"ok": True}

        door = {"actor": None, "open": door_open, "centre": {"x": 100.0, "y": 0.0, "z": 0.0},
                "beyond": {"x": 180.0, "y": 0.0, "z": 0.0}}
        crossed = asyncio.run(npc_approach.cross_door(rpc, door, sleep=no_sleep))
        assert crossed["open"] and crossed["onto_far_side"]["distance_m"] <= 0.6
        assert "open_door" not in calls


def test_a_remembered_spot_behind_doors_plans_them_only_when_a_route_finds_no_corridor(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    spots = npc_approach.TalkSpots(tmp_path / "spots.json")
    spots.record("field_object:dale", {"x": 2800.0, "y": 0.0, "z": 0.0}, {"x": 2600.0, "y": 0.0, "z": 0.0})
    monkeypatch.setattr(npc_approach, "capture_covers", lambda start, point: True)
    goals, calls = [], []

    class FakeMission:
        def __init__(self, rpc, output, identity_check, config, client_id=None):
            self.config = config

        async def run(self):
            goals.append(self.config.target_position[0])
            if len(goals) == 1:
                return {"ok": False, "state": "STOPPED", "reason": "cached_topology_route_plan_no_linked_polygon_corridor"}
            return {"ok": True, "state": "ARRIVED", "reason": None}

    door = {"actor": "BG0012 street", "open": True, "centre": {"x": 1000.0, "y": 0.0, "z": 0.0},
            "front": {"x": 900.0, "y": 0.0, "z": 0.0}, "beyond": {"x": 1100.0, "y": 0.0, "z": 0.0}}
    feet = {"x": 0.0}

    async def rpc(operation, arguments):
        calls.append(operation)
        if operation == "resolve_world_target":
            return {"status": "located", "canonical_id": "field_object:dale",
                    "position": {"x": 2800.0, "y": 0.0, "z": 88.0}}
        if operation == "observe_game":
            return {"player": {"locomotion": {"feet_position": {"x": feet["x"], "y": 0.0, "z": 0.0}}},
                    "camera": {"rotation": {"yaw": 0.0}}}
        if operation == "plan_npc_approach":
            return {"library": {}, "doors": [door], "approach": {"position": {"x": 2600.0, "y": 0.0, "z": 0.0}}}
        if operation == "move_stick":
            feet["x"] = 1100.0
        if operation == "read_field_prompt":
            here = {"x": 2600.0 if len(goals) == 3 else 0.0, "y": 0.0, "z": 0.0}
            return {"commands": ["$level_action_talk"] if len(goals) == 3 else [], "talk": len(goals) == 3,
                    "player_position": here}
        return {"ok": True}

    monkeypatch.setattr(npc_approach, "NavigationMission", FakeMission)
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Dale", output=tmp_path / "out", identity_check=None,
                                                   execute=True, talk_spots=spots, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and calls.count("plan_npc_approach") == 1
    # The spot, the door's front, then the spot again from beyond the door.
    assert goals == [2600.0, 900.0, 2600.0]
    assert [crossing["actor"] for crossing in report["door_crossings"]] == ["BG0012 street"]


def test_extricate_steps_toward_the_most_open_direction():
    from ff7r_agent import npc_approach

    sticks, feet = [], {"x": 0.0, "y": 0.0, "z": 0.0}

    async def rpc(operation, arguments):
        if operation == "capture_game_window":
            return {"image_path": "shot.png"}
        if operation == "observe_game":
            return {"player": {"locomotion": {"feet_position": dict(feet)}}, "camera": {"rotation": {"yaw": 0.0}}}
        if operation == "sweep_route":
            points = arguments["points"]
            segments = []
            for index in range(len(points) - 1):
                end = points[index + 1]
                # Walls everywhere except straight along -y (yaw 270).
                blocked = index % 2 == 0 and not (abs(end["x"]) < 1 and end["y"] < 0)
                segments.append({"index": index, "complete": not blocked,
                                 "hits": [{"kind": "fixed", "distance_cm": 40.0}] if blocked else []})
            return {"segments": segments}
        if operation == "move_stick":
            sticks.append((arguments["x"], arguments["y"]))
            feet["y"] -= 80.0
        return {"ok": True}

    record = asyncio.run(npc_approach.extricate(rpc, {"x": 3000.0, "y": 0.0, "z": 0.0}, sleep=no_sleep))
    assert record["moved"] and record["heading_degrees"] == 270.0 and record["screenshot"] == "shot.png"
    # Bearing 270 with the camera at 0 is a quarter turn left: stick left.
    assert sticks[0] == (pytest.approx(-1.0), pytest.approx(0.0, abs=1e-3))
    assert len(sticks) == 3  # 190 cm of the 250 cm free before the stop margin


def test_extricate_stays_put_when_nothing_is_open():
    from ff7r_agent import npc_approach

    calls = []

    async def rpc(operation, arguments):
        calls.append(operation)
        if operation == "observe_game":
            return {"player": {"locomotion": {"feet_position": {"x": 0.0, "y": 0.0, "z": 0.0}}}}
        if operation == "sweep_route":
            return {"segments": [{"index": i, "hits": [{"kind": "npc", "distance_cm": 20.0}]}
                                 for i in range(len(arguments["points"]) - 1)]}
        if operation == "capture_game_window":
            raise MissionError("capture_refused")
        return {"ok": True}

    record = asyncio.run(npc_approach.extricate(rpc, {"x": 1.0, "y": 0.0, "z": 0.0}, sleep=no_sleep))
    assert not record["moved"] and "move_stick" not in calls and record["screenshot_error"]


def test_a_failed_approach_fast_travels_to_the_reset_spot_and_tries_again(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    attempts, travels = [], []

    async def fake_once(rpc, name, *, output, **options):
        attempts.append(output)
        return {"ok": len(attempts) > 1, "state": "talk_offered" if len(attempts) > 1 else "stopped"}

    async def rpc(operation, arguments):
        if operation == "travel_to_map_spot_async":
            travels.append(arguments["spot"])
            return {"job_id": "j1", "state": "running"}
        if operation == "get_travel_to_map_spot_status":
            return {"state": "completed", "response": {"result": {"state": "arrived", "arrival_position": {"x": 1, "y": 2, "z": 3}}}}
        return {"ok": True}

    monkeypatch.setattr(npc_approach, "_approach_once", fake_once)
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Ned", output=tmp_path, identity_check=None,
                                                   execute=True, reset_spot="Kalm", sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and travels == ["Kalm"] and report["reset"]["spot"] == "Kalm"
    assert report["before_reset"]["state"] == "stopped" and attempts[1] == tmp_path / "after-reset"


def test_no_reset_without_a_reset_spot(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    async def fake_once(rpc, name, **options):
        return {"ok": False, "state": "stopped"}

    async def rpc(operation, arguments):
        raise AssertionError(operation)

    monkeypatch.setattr(npc_approach, "_approach_once", fake_once)
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Ned", output=tmp_path, identity_check=None,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert not report["ok"] and "reset" not in report


def test_the_bridge_plans_the_approach_on_its_cached_graph(monkeypatch):
    from ff7r_agent import npc_approach

    graph = FakeGraph([(0, 0, 0), (1000, 0, 0)])
    provider = SimpleNamespace(_cached_state={"graph": graph, "raw_sha256": "ab"}, _graph_memo={})
    service = SimpleNamespace(_validated_navigation_resources=("key", provider, "validator"))
    seen = {}

    def covering(start, target, sha, known_graphs=None, require_target=True, build_missing=True):
        seen.update(known_graphs)
        return known_graphs["AB"], {"covers_target": True, "graph_reused": True}

    monkeypatch.setattr(npc_approach, "load_covering_graph", covering)
    result = npc_approach.plan_approach_in_process(service, {"x": 0, "y": 0, "z": 0}, {"x": 1000, "y": 0, "z": 0})
    assert seen == {"AB": graph} and result["library"]["graph_reused"]
    assert result["approach"]["position"]["x"] == 800.0



def test_the_bridge_plans_on_a_graph_its_prewarm_just_built(monkeypatch):
    from ff7r_agent import npc_approach

    graph = FakeGraph([(0, 0, 0), (1000, 0, 0)])
    waits = []

    def prewarmed_graphs(wait_seconds=0.0):
        waits.append(wait_seconds)
        return {"cd": graph}

    provider = SimpleNamespace(_cached_state=None, prewarmed_graphs=prewarmed_graphs,
                               graph_prewarm_pending=lambda: False)
    service = SimpleNamespace(_validated_navigation_resources=("key", provider, "validator"))

    def covering(start, target, sha, known_graphs=None, require_target=True, build_missing=True):
        assert build_missing
        return known_graphs["CD"], {"covers_target": True, "graph_reused": True}

    monkeypatch.setattr(npc_approach, "load_covering_graph", covering)
    result = npc_approach.plan_approach_in_process(service, {"x": 0, "y": 0, "z": 0}, {"x": 1000, "y": 0, "z": 0})
    assert waits == [npc_approach.PREWARMED_GRAPH_WAIT_SECONDS] and result["library"]["graph_reused"]


def test_the_bridge_does_not_build_a_graph_its_prewarm_is_still_building(monkeypatch):
    from ff7r_agent import npc_approach

    provider = SimpleNamespace(_cached_state=None, prewarmed_graphs=lambda wait_seconds=0.0: {},
                               graph_prewarm_pending=lambda: True)
    service = SimpleNamespace(_validated_navigation_resources=("key", provider, "validator"))

    def covering(start, target, sha, known_graphs=None, require_target=True, build_missing=True):
        if build_missing:
            pytest.fail("built alongside the prewarm")
        raise MissionError(npc_approach.APPROACH_GRAPH_STILL_BUILDING)

    monkeypatch.setattr(npc_approach, "load_covering_graph", covering)
    with pytest.raises(MissionError, match=npc_approach.APPROACH_GRAPH_STILL_BUILDING):
        npc_approach.plan_approach_in_process(service, {"x": 0, "y": 0, "z": 0}, {"x": 1000, "y": 0, "z": 0})

class ScriptedApproach:
    """A runtime where Talk appears once Cloud stands within ``talk_cm`` of the spot."""

    def __init__(self, feet, talk_at=None):
        self.feet, self.talk_at, self.calls = dict(feet), talk_at, []

    async def rpc(self, operation, arguments):
        self.calls.append(operation)
        if operation == "resolve_world_target":
            return {"status": "located", "canonical_id": "field_object:ned", "position": {"x": 0.0, "y": 0.0, "z": 88.0}}
        if operation == "observe_game":
            return {"player": {"locomotion": {"feet_position": dict(self.feet)}}, "camera": {"rotation": {"yaw": 0.0}}}
        if operation == "read_field_prompt":
            talk = self.talk_at is not None and ((self.feet["x"] - self.talk_at["x"]) ** 2
                                                  + (self.feet["y"] - self.talk_at["y"]) ** 2) ** 0.5 < 50
            return {"commands": ["$level_action_talk"] if talk else [], "talk": talk,
                    "player_position": dict(self.feet)}
        if operation == "plan_npc_approach":
            return {"library": {}, "approach": {"position": {"x": 200.0, "y": 0.0, "z": 0.0}}}
        return {"ok": True}


@pytest.mark.parametrize("reason", ["feature-validation HEAD mismatch",
    "runtime_identity_mismatch", "SafetyError: input stop is latched", "response lost"])
def test_route_refusal_stops_before_any_recovery(tmp_path, monkeypatch, reason):
    from ff7r_agent import npc_approach
    runtime = ScriptedApproach({"x": 5000.0, "y": 0.0, "z": 0.0})
    routes = []
    class RefusedMission:
        def __init__(self, rpc, output, identity_check, config, client_id=None):
            pass
        async def run(self):
            routes.append(True)
            return {"ok": False, "state": "STOPPED", "reason": reason,
                    "recovery_refused": True, "identity_verification": "full"}
    async def forbidden(*_, **__):
        pytest.fail("recovery was attempted after route refusal")
    monkeypatch.setattr(npc_approach, "NavigationMission", RefusedMission)
    for name in ("face_until_talk", "extricate", "leave_water", "travel_to_spot", "stuck_report"):
        monkeypatch.setattr(npc_approach, name, forbidden)
    result = asyncio.run(npc_approach.approach_npc(fake_control_lease(runtime.rpc), "Ned",
        output=tmp_path, identity_check=None, execute=True, reset_spot="Kalm",
        hand_off_when_stuck=True, model_id="test-model", reasoning_effort="test-effort", sleep=no_sleep))
    assert result["recovery_refused"] is True and result["reason"] == reason
    assert len(routes) == 1 and "reset" not in result and "face" not in result


def test_outer_approach_does_not_reset_a_refused_attempt(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach
    attempts = []
    async def refused(*_, **__):
        attempts.append(True)
        return {"ok": False, "state": "stopped", "reason": "runtime_identity_mismatch",
                "recovery_refused": True}
    async def forbidden(*_, **__):
        pytest.fail("refused attempt reached reset or handoff input")
    monkeypatch.setattr(npc_approach, "_approach_once", refused)
    monkeypatch.setattr(npc_approach, "travel_to_spot", forbidden)
    monkeypatch.setattr(npc_approach, "stuck_report", forbidden)
    result = asyncio.run(npc_approach.approach_npc(fake_control_lease(forbidden), "Ned",
        output=tmp_path, identity_check=None, execute=True, reset_spot="Kalm", hand_off_when_stuck=True,
        model_id="test-model", reasoning_effort="test-effort", sleep=no_sleep))
    assert result["recovery_refused"] is True and len(attempts) == 1


@pytest.mark.parametrize("raises", [False, True])
def test_initial_identity_refusal_precedes_menu_travel_and_route_input(tmp_path, raises):
    calls = []
    async def rpc(operation, arguments):
        calls.append(operation)
        assert operation in {"refresh_lifecycle_attachment", "get_runtime_identity", "get_bootstrap_verification"}
        return {"ok": True}
    def wrong_head(*_):
        if raises:
            raise MissionError("feature-validation HEAD mismatch")
        return {"ok": False}
    result = asyncio.run(approach_npc(rpc, "Nene", output=tmp_path, identity_check=wrong_head,
        execute=True, reset_spot="Kalm", hand_off_when_stuck=True, model_id="test-model",
        reasoning_effort="test-effort", sleep=no_sleep))
    assert result["recovery_refused"] is True and "reset" not in result
    assert calls == ["refresh_lifecycle_attachment", "get_runtime_identity", "get_bootstrap_verification"]


def mission_moving(runtime, runs, to=None):
    class FakeMission:
        def __init__(self, rpc, output, identity_check, config, client_id=None):
            self.config = config

        async def run(self):
            x, y, z = self.config.target_position
            runs.append((x, y, z))
            runtime.feet = dict(to) if to else {"x": x, "y": y, "z": z - 88.0}
            return {"ok": True, "state": "ARRIVED"}
    return FakeMission


def test_an_npc_in_another_area_is_fast_travelled_to_first(tmp_path, monkeypatch):
    # Live 2026-09-30: Gongaga's item shop from Kalm.
    from ff7r_agent import npc_approach

    runtime, runs, travels = ScriptedApproach({"x": 900000.0, "y": 0.0, "z": 0.0}, talk_at={"x": 200.0, "y": 0.0}), [], []

    async def travel_to_spot(rpc, spot, sleep=None, clock=None):
        travels.append(spot)
        runtime.feet = {"x": 5000.0, "y": 0.0, "z": 0.0}
        return {"state": "arrived", "arrival_position": dict(runtime.feet)}

    monkeypatch.setattr(npc_approach, "travel_to_spot", travel_to_spot)
    monkeypatch.setattr(npc_approach, "NavigationMission", mission_moving(runtime, runs))
    report = asyncio.run(npc_approach.approach_npc(rpc=fake_control_lease(runtime.rpc), name="Ned", output=tmp_path, identity_check=None,
                                                   execute=True, reset_spot="$hub_place_2400_06", sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and travels == ["$hub_place_2400_06"]
    assert report["travel_first"]["from_distance_m"] == 9000.0 and report["start_distance_m"] == 50.0


def test_approach_refuses_to_resolve_an_npc_during_dialogue(tmp_path):
    calls = []

    async def rpc(operation, arguments=None):
        calls.append(operation)
        if operation == "observe_game":
            return {"ok": True, "id": 1, "result": {
                "map_id": None,
                "loading": False,
                "in_combat": False,
                "target": None,
                "player": {"position": None},
                "queen_blood": {"active": False, "match_screen": False,
                                 "begin_match_available": False},
            }}
        pytest.fail(f"unexpected RPC during active dialogue: {operation}")

    ticks = iter(range(0, 10_000, 5))
    report = asyncio.run(approach_npc(
        rpc=fake_control_lease(rpc), name="Ned", output=tmp_path, identity_check=None,
        execute=True, pursuit=True, sleep=no_sleep, clock=lambda: float(next(ticks)), model_id="test-model", reasoning_effort="test-effort"))

    # A blank feed alone is not reported as a dialogue (cutscenes and menus blank it too).
    assert not report["ok"] and report["reason"] == "field_telemetry_unavailable"
    assert calls and set(calls) == {"observe_game"}  # waited, sent nothing


def test_reset_does_not_retry_when_travel_arrival_has_no_position(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    attempts = []

    async def fake_once(rpc, name, *, output, **options):
        attempts.append(output)
        return {"ok": False, "state": "stopped", "reason": "route_failed"}

    async def travel_to_spot(rpc, spot, sleep=None, clock=None):
        return {"state": "arrived", "arrival_position": None}

    monkeypatch.setattr(npc_approach, "_approach_once", fake_once)
    monkeypatch.setattr(npc_approach, "travel_to_spot", travel_to_spot)

    async def rpc(operation, arguments):
        pytest.fail(f"unexpected RPC during mocked reset: {operation}")

    report = asyncio.run(npc_approach.approach_npc(
        fake_control_lease(rpc), "Ned", output=tmp_path, identity_check=None, execute=True,
        reset_spot="Kalm", sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))

    assert not report["ok"]
    assert report["reset"]["error"] == "reset_travel_arrival_position_unavailable"
    assert attempts == [tmp_path]


def test_travel_refuses_dialogue_before_waiting_or_fast_travel():
    from ff7r_agent import npc_approach

    calls = []

    async def rpc(operation, arguments=None):
        calls.append(operation)
        if operation == "observe_game":
            return {"ok": True, "id": 1, "result": {
                "map_id": None,
                "loading": False,
                "in_combat": False,
                "target": None,
                "player": {"position": None},
                "queen_blood": {"active": False, "match_screen": False,
                                 "begin_match_available": False},
            }}
        pytest.fail(f"unexpected RPC during dialogue: {operation}")

    with pytest.raises(MissionError, match="field_telemetry_unavailable"):
        ticks = iter(range(0, 10_000, 5))
        asyncio.run(npc_approach.travel_to_spot(rpc, "Kalm", sleep=no_sleep, clock=lambda: float(next(ticks))))
    assert calls and set(calls) == {"observe_game"}  # waited, sent nothing


def test_travel_uses_one_async_job_without_a_long_synchronous_settle():
    from ff7r_agent import npc_approach

    calls = []

    async def rpc(operation, arguments=None):
        calls.append(operation)
        if operation == "observe_game":
            return {"ok": True, "id": 1, "result": {"map_id": "Kalm", "player": {"position": {"x": 0, "y": 0, "z": 0}}}}
        if operation == "wait_for_settled_gameplay":
            return {"ok": True, "id": 2, "result": {"settled": True}}
        if operation == "open_control_session":
            return {"ok": True}
        if operation == "travel_to_map_spot_async":
            return {"job_id": "j1", "state": "running"}
        if operation == "get_travel_to_map_spot_status":
            return {"state": "completed", "response": {"id": 3, "result": {
                "state": "arrived", "arrival_position": {"x": 1, "y": 2, "z": 3}}}}
        if operation == "close_control_session":
            return {"ok": True}
        pytest.fail(f"unexpected RPC: {operation}")

    result = asyncio.run(npc_approach.travel_to_spot(rpc, "Kalm", sleep=no_sleep))

    assert result["arrival_position"] == {"x": 1, "y": 2, "z": 3}
    assert "wait_for_settled_gameplay" not in calls
    assert calls.count("travel_to_map_spot_async") == 1


def test_a_learned_talk_spot_is_routed_to_directly_and_talk_spots_are_recorded(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    spots = npc_approach.TalkSpots(tmp_path / "spots.json")
    spots.record("field_object:ned", {"x": 0.0, "y": 0.0, "z": 0.0}, {"x": 0.0, "y": -300.0, "z": 5.0})
    runtime, runs = ScriptedApproach({"x": 5000.0, "y": 0.0, "z": 0.0}, talk_at={"x": 0.0, "y": -300.0}), []
    monkeypatch.setattr(npc_approach, "capture_covers", lambda start, point: True)
    monkeypatch.setattr(npc_approach, "NavigationMission", mission_moving(runtime, runs))
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(runtime.rpc), "Ned", output=tmp_path / "out", identity_check=None,
                                                   execute=True, talk_spots=spots, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and report["approach"]["talk_spot"]
    assert "plan_npc_approach" not in runtime.calls and runs == [(0.0, -300.0, 93.0)]
    assert report["talk_spot_recorded"] == {"x": 0.0, "y": -300.0, "z": 5.0}


def test_a_learned_talk_spot_beyond_every_capture_with_cloud_is_planned_toward_instead(tmp_path, monkeypatch):
    # Live 2026-10-01: Gongaga's shop from its airstrip, 324 m. Routing
    # straight to the learned spot failed four times with no capture covering
    # Cloud, and the staging legs never ran.
    from ff7r_agent import npc_approach

    spots = npc_approach.TalkSpots(tmp_path / "spots.json")
    spots.record("field_object:ned", {"x": 0.0, "y": 0.0, "z": 0.0}, {"x": 0.0, "y": -300.0, "z": 5.0})
    runtime, runs = ScriptedApproach({"x": 5000.0, "y": 0.0, "z": 0.0}, talk_at={"x": 200.0, "y": 0.0}), []
    monkeypatch.setattr(npc_approach, "capture_covers", lambda start, point: False)
    monkeypatch.setattr(npc_approach, "NavigationMission", mission_moving(runtime, runs))
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(runtime.rpc), "Ned", output=tmp_path / "out", identity_check=None,
                                                   execute=True, talk_spots=spots, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and "plan_npc_approach" in runtime.calls
    assert report["talk_spot_beyond_capture"] == {"x": 0.0, "y": -300.0, "z": 5.0}
    assert not report["approach"].get("talk_spot")


def test_a_remembered_talk_spot_off_the_navmesh_is_forgotten_and_planned_onto_it(tmp_path, monkeypatch):
    # Live at Nene, 2026-10-11 and -12: the remembered spot sat 10 cm past
    # the navmesh edge, so every route to it was refused before moving.
    from ff7r_agent import npc_approach

    spots = npc_approach.TalkSpots(tmp_path / "spots.json")
    spots.record("field_object:nene", {"x": 2800.0, "y": 0.0, "z": 0.0}, {"x": 2610.0, "y": 0.0, "z": 0.0})
    monkeypatch.setattr(npc_approach, "capture_covers", lambda start, point: True)
    goals, calls, plans = [], [], []

    class FakeMission:
        def __init__(self, rpc, output, identity_check, config, client_id=None):
            self.config = config

        async def run(self):
            goals.append(self.config.target_position[0])
            if len(goals) == 1:
                return {"ok": False, "state": "STOPPED",
                        "reason": "cached_topology_route_target_not_inside_topology:outside_polygon"}
            return {"ok": True, "state": "ARRIVED", "reason": None}

    async def rpc(operation, arguments):
        calls.append(operation)
        if operation == "resolve_world_target":
            return {"status": "located", "canonical_id": "field_object:nene",
                    "position": {"x": 2800.0, "y": 0.0, "z": 88.0}}
        if operation == "observe_game":
            here = 2600.0 if len(goals) == 2 else 0.0
            return {"player": {"locomotion": {"feet_position": {"x": here + 9.0, "y": 0.0, "z": 0.0}}},
                    "camera": {"rotation": {"yaw": 0.0}}}
        if operation == "plan_npc_approach":
            plans.append(arguments["start"])
            return {"library": {}, "doors": [], "approach": {"position": {"x": 2600.0, "y": 0.0, "z": 0.0}}}
        if operation == "read_field_prompt":
            arrived = len(goals) == 2
            return {"commands": ["$level_action_talk"] if arrived else [], "talk": arrived,
                    "player_position": {"x": 2600.0 if arrived else 0.0, "y": 0.0, "z": 0.0}}
        return {"ok": True}

    monkeypatch.setattr(npc_approach, "NavigationMission", FakeMission)
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Nene", output=tmp_path / "out", identity_check=None,
                                                   execute=True, talk_spots=spots, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and goals == [2610.0, 2600.0]
    assert report["talk_spot_off_navmesh"] == {"x": 2610.0, "y": 0.0, "z": 0.0}
    assert plans == [{"x": 0.0, "y": 0.0, "z": 0.0}] and "extrication" not in report
    # The planned spot on the navmesh is remembered, not the feet past the edge.
    assert report["talk_spot_recorded"] == {"x": 2600.0, "y": 0.0, "z": 0.0}
    assert spots.get("field_object:nene", {"x": 2800.0, "y": 0.0, "z": 0.0})["x"] == 2600.0


def test_a_talk_spot_can_be_forgotten(tmp_path):
    from ff7r_agent import npc_approach

    spots = npc_approach.TalkSpots(tmp_path / "spots.json")
    spots.record("field_object:ned", {"x": 0.0, "y": 0.0, "z": 0.0}, {"x": 0.0, "y": -300.0, "z": 0.0})
    spots.record("field_object:dale", {"x": 0.0, "y": 0.0, "z": 0.0}, {"x": 0.0, "y": 300.0, "z": 0.0})
    spots.forget("field_object:ned")
    spots.forget("field_object:unknown")
    spots.forget(None)
    reloaded = npc_approach.TalkSpots(tmp_path / "spots.json")
    assert reloaded.get("field_object:ned", {"x": 0.0, "y": 0.0, "z": 0.0}) is None
    assert reloaded.get("field_object:dale", {"x": 0.0, "y": 0.0, "z": 0.0}) is not None

def test_a_talk_spot_is_ignored_once_the_npc_has_moved(tmp_path):
    from ff7r_agent import npc_approach

    spots = npc_approach.TalkSpots(tmp_path / "spots.json")
    spots.record("field_object:ned", {"x": 0.0, "y": 0.0, "z": 0.0}, {"x": 0.0, "y": -300.0, "z": 0.0})
    assert spots.get("field_object:ned", {"x": 50.0, "y": 0.0, "z": 0.0}) is not None
    assert spots.get("field_object:ned", {"x": 500.0, "y": 0.0, "z": 0.0}) is None
    assert spots.get("field_object:other", {"x": 0.0, "y": 0.0, "z": 0.0}) is None


def test_no_talk_after_facing_routes_once_more_before_giving_up(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    # Every route ends 4 m away on the wrong side; facing never finds Talk,
    # so the fallback re-route runs once and the approach then stops.
    runtime, runs = ScriptedApproach({"x": 5000.0, "y": 0.0, "z": 0.0}), []
    monkeypatch.setattr(npc_approach, "NavigationMission", mission_moving(runtime, runs, to={"x": 400.0, "y": 0.0, "z": 0.0}))
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(runtime.rpc), "Ned", output=tmp_path, identity_check=None,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert not report["ok"] and report["reason"] == "npc_talk_prompt_not_offered"
    assert len(runs) == 2 and report["reapproach"]["state"] == "ARRIVED" and report["face_failed"]


def consecutive_routes(tmp_path, monkeypatch, first_state="STOPPED", parkour=False, lease_client=None):
    from ff7r_agent import npc_approach

    missions, calls = [], []
    # The control session, as the runtime keeps it.
    lease = {"active": lease_client is not None, "client_id": lease_client}

    class FakeMission:
        def __init__(self, rpc, output, identity_check, config, client_id=None):
            self.identity_check, self.config, self.client_id = identity_check, config, client_id
            self.lease_at_start = dict(lease)

        async def run(self):
            missions.append(self)
            done = len(missions) > 1
            return {"ok": done, "state": "ARRIVED" if done else first_state}

    async def rpc(operation, arguments):
        calls.append(operation)
        if operation == "resolve_world_target":
            return {"status": "located", "position": {"x": 3000.0, "y": 0.0, "z": 88.0}}
        if operation == "observe_game":
            return {"player": {"locomotion": {"feet_position": {"x": 0.0, "y": 0.0, "z": 0.0}}}}
        if operation == "read_field_prompt":
            if len(missions) >= 2:
                return {"commands": ["$level_action_talk"], "talk": True,
                        "player_position": {"x": 2800.0, "y": 0.0, "z": 0.0}}
            return {"commands": [npc_approach.PARKOUR_PROMPT] if parkour else [], "talk": False,
                    "player_position": {"x": 0.0, "y": 0.0, "z": 0.0}}
        if operation == "plan_npc_approach":
            return {"library": {}, "approach": {"position": {"x": 2800.0, "y": 0.0, "z": 0.0}}}
        if operation == "get_control_session_status":
            return dict(lease)
        if operation == "open_control_session":
            if lease["active"]:
                raise MissionError("an active control session already exists")
            lease.update(active=True, client_id=arguments.get("client_id"))
            return dict(lease)
        if operation == "close_control_session":
            lease.update(active=False, client_id=None)
        return {"ok": True}

    def check(identity, bootstrap):
        return {"ok": True}

    monkeypatch.setattr(npc_approach, "NavigationMission", FakeMission)
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Ned", output=tmp_path, identity_check=check,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    return report, missions, calls, check


def test_consecutive_routes_share_one_identity_verifier_and_a_faster_status_cadence(tmp_path, monkeypatch):
    from ff7r_agent.navigation_mission import VerifiedIdentityCache

    report, missions, calls, check = consecutive_routes(tmp_path, monkeypatch)
    assert report["ok"] and len(missions) == 2
    verifier = missions[0].identity_check
    assert isinstance(verifier, VerifiedIdentityCache) and verifier.check is check
    assert missions[1].identity_check is verifier
    assert [mission.config.poll_seconds for mission in missions] == [0.25, 0.25]
    assert all(mission.config.capture_evidence is False for mission in missions)
    assert missions[0].config.target_position == (2800.0, 0.0, 88.0) and missions[0].config.execute


@pytest.mark.parametrize("first_state,parkour", [("STOPPED", False), ("CLEANUP_FAILED", False), ("STOPPED", True)])
def test_every_route_borrows_the_approachs_one_session(tmp_path, monkeypatch, first_state, parkour):
    # #193: closing a session latches the stop, so closing and reopening it
    # around every route cost time and left the next action refused.
    _, missions, calls, _ = consecutive_routes(tmp_path, monkeypatch, first_state, parkour)
    assert len(missions) == 2 and calls.count("open_control_session") == 1
    # The one session this approach opened is closed once, with the approach.
    assert calls.count("close_control_session") == 1 and calls[-1] == "close_control_session"
    held = missions[0].client_id
    assert held and all(mission.client_id == held for mission in missions)
    assert all(mission.lease_at_start == {"active": True, "client_id": held} for mission in missions)
    if parkour:
        assert calls.index("open_control_session") < calls.index("terrain_action")


def test_a_session_open_under_another_client_is_borrowed_and_left_open(tmp_path, monkeypatch):
    report, missions, calls, _ = consecutive_routes(tmp_path, monkeypatch, lease_client="someone-else")
    assert report["ok"] and len(missions) == 2
    assert "close_control_session" not in calls and "open_control_session" not in calls
    assert report["control_session"] == "left_open"


def test_the_reset_spot_defaults_to_the_catalogued_nearest_spot(monkeypatch):
    from ff7r_agent import npc_approach, qb_opponents

    monkeypatch.setattr(qb_opponents, "travel_spot_for", lambda name: {"key": "$map1110"} if name == "Ned" else None)
    assert npc_approach.resolve_reset_spot("Ned", "auto") == "$map1110"
    assert npc_approach.resolve_reset_spot("Stranger", "auto") is None
    assert npc_approach.resolve_reset_spot("Ned", "Kalm") == "Kalm"
    assert npc_approach.resolve_reset_spot("Ned", "none") is None

    def missing(name):
        raise KeyError(name)
    monkeypatch.setattr(qb_opponents, "travel_spot_for", missing)
    assert npc_approach.resolve_reset_spot("Nobody", "auto") is None


def test_another_npcs_talk_prompt_out_of_range_is_not_success():
    # Live: Cloud stood at Ned's talk spot, 28 m from Nene; the game offered
    # Talk (for Ned) and the approach to Nene was wrongly reported done.
    runtime = FakeRuntime(turns=0, distance_cm=2800.0)
    with pytest.raises(MissionError, match="npc_out_of_talk_range"):
        asyncio.run(face_until_talk(runtime.rpc, {"x": 0, "y": 0, "z": 0}, sleep=no_sleep))


def test_an_uncovered_route_is_captured_then_planned(tmp_path, monkeypatch):
    # Live: from the Air-Raid Shelter no stored capture covered Zahira.
    from ff7r_agent import npc_approach

    runtime, runs, plans = ScriptedApproach({"x": 5000.0, "y": 0.0, "z": 0.0}, talk_at={"x": 200.0, "y": 0.0}), [], []
    original = runtime.rpc

    async def rpc(operation, arguments):
        if operation == "plan_npc_approach":
            plans.append(1)
            if len(plans) == 1:
                raise MissionError("npc_approach_unplannable:no_linked_polygon_corridor")
        if operation == "start_topology_capture":
            runtime.calls.append(operation)
            return {"job_id": "capture-1", "state": "running"}
        if operation == "get_topology_capture_status":
            return {"state": "completed", "response": {"result": {"ok": True, "cache_admitted": True}}}
        return await original(operation, arguments)

    monkeypatch.setattr(npc_approach, "NavigationMission", mission_moving(runtime, runs))
    monkeypatch.setattr(npc_approach, "load_covering_graph",
                        lambda *a: (_ for _ in ()).throw(AssertionError("no local reload for missing coverage")))
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Zahira", output=tmp_path, identity_check=None,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and report["topology_capture"]["job_id"] == "capture-1" and len(plans) == 2


def test_a_start_on_a_navmesh_island_steps_off_before_capturing(tmp_path, monkeypatch):
    # A start with no linked corridor steps off once before any capture.
    from ff7r_agent import npc_approach

    runtime, runs, plans, steps = ScriptedApproach({"x": 5000.0, "y": 0.0, "z": 0.0}, talk_at={"x": 200.0, "y": 0.0}), [], [], []
    original = runtime.rpc

    async def rpc(operation, arguments):
        if operation == "plan_npc_approach":
            plans.append(arguments["start"])
            if len(plans) == 1:
                raise MissionError("npc_approach_unplannable:no_linked_polygon_corridor")
        if operation == "start_topology_capture":
            raise AssertionError("no capture after stepping off")
        return await original(operation, arguments)

    async def extricate(rpc_, target, sleep=None):
        steps.append(target)
        return {"moved": True, "moved_m": 1.8}

    monkeypatch.setattr(npc_approach, "extricate", extricate)
    monkeypatch.setattr(npc_approach, "NavigationMission", mission_moving(runtime, runs))
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Zahira", output=tmp_path, identity_check=None,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and len(steps) == 1 and len(plans) == 2
    assert report["stranded_step"]["moved"] and "topology_capture" not in report


def test_an_open_control_session_is_used_whoever_holds_it():
    from ff7r_agent import npc_approach
    calls = []
    async def rpc(operation, arguments):
        calls.append(operation)
        if operation == "get_control_session_status":
            return {"active": True, "client_id": "already-owned"}
        pytest.fail("must only read the open session")
    for bound in (npc_approach.AttributedApproachRpc(rpc, client_id="already-owned"),
                  npc_approach.AttributedApproachRpc(rpc)):  # no identity, no model: nothing to open
        asyncio.run(npc_approach.ensure_control_session(bound))
        asyncio.run(npc_approach.hold_control_session(bound, "caller"))
    assert set(calls) == {"get_control_session_status"}


def test_an_unloaded_catalogued_npc_is_approached_at_its_placement(tmp_path, monkeypatch):
    # Live: Dokaccha was not loaded 79 m from Costa del Sol's arrival point.
    from ff7r_agent import npc_approach

    runtime, runs = ScriptedApproach({"x": 5000.0, "y": 0.0, "z": 0.0}, talk_at={"x": 200.0, "y": 0.0}), []
    original = runtime.rpc

    async def rpc(operation, arguments):
        if operation == "resolve_world_target":
            raise MissionError("runtime_refused", response={
                "ok": False, "status": "known_but_not_currently_instantiated", "canonical_id": "field_object:X:dok"})
        return await original(operation, arguments)

    monkeypatch.setattr(npc_approach, "catalogue_position",
                        lambda name: {"x": 0.0, "y": 0.0, "z": 88.0} if name == "field_object:X:dok" else None)
    monkeypatch.setattr(npc_approach, "NavigationMission", mission_moving(runtime, runs))
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Dokaccha", output=tmp_path, identity_check=None,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and report["target_source"] == "opponent_catalogue_placement"
    assert report["target"]["position"] == {"x": 0.0, "y": 0.0, "z": 0.0}


def test_a_main_menu_left_open_is_closed_first():
    from ff7r_agent import npc_approach

    state = {"menu": True, "taps": []}

    async def rpc(operation, arguments):
        if operation == "observe_game":
            return {"in_menu": state["menu"]}
        if operation == "tap_button":
            state["taps"].append(arguments["button"])
            state["menu"] = False
        return {"ok": True}

    asyncio.run(npc_approach.close_main_menu(rpc, sleep=no_sleep))
    assert state["taps"] == ["cancel"]

    async def stuck(operation, arguments):
        return {"in_menu": True} if operation == "observe_game" else {"ok": True}

    with pytest.raises(MissionError, match="main_menu_did_not_close"):
        asyncio.run(npc_approach.close_main_menu(stuck, sleep=no_sleep))


def test_a_main_menu_that_ignores_quick_back_presses_is_given_longer_waits():
    # Live after a harness restart (#354): three quick presses left it open.
    from ff7r_agent import npc_approach

    state = {"menu": True, "taps": 0}
    waits = []

    async def rpc(operation, arguments):
        if operation == "observe_game":
            return {"in_menu": state["menu"]}
        if operation == "tap_button":
            state["taps"] += 1
            state["menu"] = state["taps"] < 5
        return {"ok": True}

    async def sleep(seconds):
        waits.append(seconds)

    asyncio.run(npc_approach.close_main_menu(rpc, sleep=sleep))
    assert state["taps"] == 5 and not state["menu"]
    assert waits == list(npc_approach.MENU_CLOSE_SETTLES_SECONDS[:5])
    assert sum(npc_approach.MENU_CLOSE_SETTLES_SECONDS) > 10


def test_a_menu_opened_mid_approach_is_closed_before_the_route():
    # Live: the gamepad reconnect opened the menu after the first check.
    from ff7r_agent import npc_approach

    state = {"menu": False, "taps": []}

    async def rpc(operation, arguments):
        if operation == "observe_game":
            return {"in_menu": state["menu"]}
        if operation == "tap_button":
            state["taps"].append(arguments["button"])
            state["menu"] = False
        return {"ok": True}

    assert asyncio.run(npc_approach.close_menu_if_open(rpc, sleep=no_sleep)) is False
    state["menu"] = True
    assert asyncio.run(npc_approach.close_menu_if_open(rpc, sleep=no_sleep)) is True
    assert state["taps"] == ["cancel"] and state["menu"] is False


def test_a_stuck_approach_resets_once_then_hands_over_to_the_agent(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    attempts = []

    async def failed_once(*args, **kwargs):
        attempts.append(kwargs["output"].name)
        return {"ok": False, "state": "stopped", "reason": "npc_out_of_talk_range",
                "target": {"position": {"x": 1000.0, "y": 0.0, "z": 0.0}}}

    async def travelled(rpc, spot, sleep, clock):
        attempts.append("travel:" + spot)
        return {"arrival_position": {"x": 0.0, "y": 0.0, "z": 0.0}}

    async def rpc(operation, arguments):
        if operation == "capture_game_window":
            return {"ok": True, "screenshot_path": "shot.bmp"}
        if operation == "observe_game":
            return {"player": {"locomotion": {"feet_position": {"x": 0.0, "y": 0.0, "z": 0.0}}},
                    "camera": {"rotation": {"yaw": 42.5}}}
        if operation == "sweep_route":
            hits = [{"kind": "vehicle", "distance_cm": 20.0}] if arguments.get("query") == "objects:7" else []
            return {"segments": [{"index": 0, "hits": hits}]}
        return {"ok": True}

    monkeypatch.setattr(npc_approach, "_approach_once", failed_once)
    monkeypatch.setattr(npc_approach, "travel_to_spot", travelled)
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Nene", output=tmp_path / "out", identity_check=None,
                                                   execute=True, reset_spot="Kalm", hand_off_when_stuck=True,
                                                   sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert attempts == ["out", "travel:Kalm", "after-reset"]
    assert report["state"] == "stuck" and not report["ok"] and report["reset"]["spot"] == "Kalm"
    stuck = report["stuck"]
    assert stuck["options"] == ["manual_escape", "unstuck_fast_travel"] and stuck["reset_spot"] == "Kalm"
    assert stuck["screenshot"] == "shot.bmp" and stuck["target_distance_m"] == pytest.approx(10.0)
    # The vehicle sweep is folded into the clearance.
    assert stuck["clearance_cm"]["0"] == 20


def test_an_approach_that_cannot_fast_travel_hands_over_at_once(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    async def failed_once(*args, **kwargs):
        return {"ok": False, "state": "stopped", "reason": "npc_out_of_talk_range", "target": {}}

    async def refused(rpc, spot, sleep, clock):
        raise npc_approach.MissionError("fast_travel_unavailable")

    async def rpc(operation, arguments):
        if operation == "observe_game":
            return {"player": {"locomotion": {"feet_position": {"x": 0.0, "y": 0.0, "z": 0.0}}}}
        if operation == "sweep_route":
            return {"segments": []}
        return {"ok": True}

    monkeypatch.setattr(npc_approach, "_approach_once", failed_once)
    monkeypatch.setattr(npc_approach, "travel_to_spot", refused)
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Nene", output=tmp_path, identity_check=None, execute=True,
                                                   reset_spot="Kalm", hand_off_when_stuck=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["state"] == "stuck" and "fast_travel_unavailable" in report["reset"]["error"]


def test_a_disconnected_zahira_route_retries_from_kalm_instead_of_the_shelter(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach
    attempts, travelled = [], []
    async def approach(*args, **kwargs):
        attempts.append(kwargs.get("travel_spot"))
        if len(attempts) == 1:
            return {"ok": False, "reason": "npc_out_of_talk_range", "routes": [
                {"reason": "cached_topology_route_plan_no_linked_polygon_corridor"}]}
        return {"ok": True, "state": "talk_offered"}
    async def travel(rpc, spot, **kwargs):
        travelled.append(spot)
        return {"arrival_position": {"x": 58003.7, "y": -97280.9, "z": 10886.5}}
    monkeypatch.setattr(npc_approach, "_approach_once", approach)
    monkeypatch.setattr(npc_approach, "travel_to_spot", travel)
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(None), "Zahira", output=tmp_path, identity_check=None,
                                                execute=True, reset_spot="$map1110_FastTravel1", model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and travelled == ["$map1110"]
    assert report["before_reset"]["disconnected_travel_spot"] == "$map1110_FastTravel1"


@pytest.mark.parametrize("new_approach", [False, True])
def test_disconnected_routes_stop_once_or_use_a_new_reachable_approach(tmp_path, monkeypatch, new_approach):
    from ff7r_agent import npc_approach
    runtime = ScriptedApproach({"x": 5000.0, "y": 0.0, "z": 0.0}, talk_at={"x": 300.0, "y": 0.0})
    missions, plans = [], []
    original = runtime.rpc
    async def rpc(operation, arguments):
        if operation == "plan_npc_approach":
            plans.append(1)
            if new_approach and len(plans) > 1:
                return {"library": {}, "approach": {"position": {"x": 300.0, "y": 0.0, "z": 0.0}}}
        return await original(operation, arguments)
    class Mission:
        def __init__(self, *args, config, **kwargs):
            self.config = config
        async def run(self):
            missions.append(self)
            if len(missions) == 1:
                return {"ok": False, "state": "STOPPED", "reason": "cached_topology_route_plan_no_linked_polygon_corridor"}
            runtime.feet = dict(zip(("x", "y", "z"), self.config.target_position))
            return {"ok": True, "state": "ARRIVED"}
    async def dry(*args, **kwargs):
        return None
    monkeypatch.setattr(npc_approach, "NavigationMission", Mission)
    monkeypatch.setattr(npc_approach, "leave_water", dry)
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Ned", output=tmp_path, identity_check=None,
                                                execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] is new_approach and len(missions) == (2 if new_approach else 1)
    if not new_approach:
        assert report["reason"] == "npc_approach_disconnected:no_linked_polygon_corridor"


def test_an_npc_on_an_unlinked_floor_is_approached_at_the_nearest_reachable_floor():
    reachable = Vec3(900, 300, 0)

    class Graph(FakeGraph):
        def plan(self, start, end, start_resolution_override=None, end_resolution_override=None):
            if end == reachable:
                return SimpleNamespace(status="complete", reason_code="route_plan_complete",
                                       waypoints=(Vec3(0, 0, 0), reachable), total_estimated_path_length_m=9.5)
            return SimpleNamespace(status="failed", reason_code="no_linked_polygon_corridor", waypoints=())

        def nearest_reachable_point(self, start, target, start_resolution=None):
            return reachable

    point = approach_point(Graph([]), {"x": 0, "y": 0, "z": 0}, {"x": 1000, "y": 0, "z": 0}, 2.0)
    assert point["position"] == {"x": 900, "y": 300, "z": 0}
    assert point["adjusted_to_reachable_m"] == pytest.approx(3.16, abs=0.01)


class CounterGraph(FakeGraph):
    """An NPC behind a counter at x=1000: the floor behind her (x>1000) is
    reachable round the side, the floor in front (x<1000) too, the counter
    top is no floor."""

    def __init__(self, behind):
        super().__init__([])
        self.behind = behind
        self.snaps = []

    def resolve_point(self, point, max_snap_distance_m=8.0, **_):
        self.snaps.append(max_snap_distance_m)
        return SimpleNamespace(ok=point.x < 950)

    def plan(self, start, end, start_resolution_override=None, end_resolution_override=None):
        if end == self.behind or end.x < 950:
            return SimpleNamespace(status="complete", reason_code="route_plan_complete",
                                   waypoints=(Vec3(0, 0, 0), end), total_estimated_path_length_m=9.0)
        return SimpleNamespace(status="failed", reason_code="no_linked_polygon_corridor", waypoints=())

    def nearest_reachable_point(self, start, target, start_resolution=None):
        return self.behind


def test_an_npc_behind_a_counter_is_approached_at_a_spot_in_front_of_her():
    # Live 2026-10-05: the nearest reachable floor to the Gold Saucer's card
    # organiser was behind her counter, and Talk was never offered there.
    graph = CounterGraph(behind=Vec3(1340, 0, 0))
    front = [{"x": 1000, "y": 400, "z": 0, "source": "catalogue_stand_point"},  # on the counter end: no floor
             {"x": 780, "y": 0, "z": 0, "source": "catalogue_facing_2.2m"}]
    point = approach_point(graph, {"x": 0, "y": 0, "z": 0}, {"x": 1000, "y": 0, "z": 0}, 2.0, front)
    assert point["position"] == {"x": 780.0, "y": 0.0, "z": 0.0}
    assert point["front_of_npc"] == "catalogue_facing_2.2m" and point["adjusted_to_reachable_m"] == 2.2
    # Spots in front are never snapped from the counter top onto nearby floor.
    assert 0.3 in graph.snaps


def test_with_no_spot_in_front_reachable_the_approach_refuses_the_rear_floor():
    graph = CounterGraph(behind=Vec3(1340, 0, 0))
    with pytest.raises(MissionError, match="npc_front_unreachable"):
        approach_point(graph, {"x": 0, "y": 0, "z": 0}, {"x": 1000, "y": 0, "z": 0}, 2.0,
                       [{"x": 1000, "y": 400, "z": 0, "source": "catalogue_stand_point"}])


def test_a_front_spot_is_preferred_even_when_the_npcs_own_floor_is_reachable():
    class Graph(CounterGraph):
        def plan(self, start, end, **kwargs):
            return SimpleNamespace(status="complete", reason_code="route_plan_complete",
                                   waypoints=(Vec3(0, 0, 0), end), total_estimated_path_length_m=9.0)
    point = approach_point(Graph(Vec3(1340, 0, 0)), {"x": 0, "y": 0, "z": 0},
                           {"x": 1000, "y": 0, "z": 0}, 2.0,
                           [{"x": 780, "y": 0, "z": 0, "source": "live_facing_2.2m"}])
    assert point["position"] == {"x": 780.0, "y": 0.0, "z": 0.0}
    assert point["front_of_npc"] == "live_facing_2.2m"


def test_current_actor_rotation_overrides_catalogue_facing_and_requires_matching_pose(monkeypatch):
    from ff7r_agent import npc_approach
    from ff7r_agent.npc_approach import npc_front
    record = {"position": {"xyz": [1000, 0, 88], "yaw_deg": 180}}
    monkeypatch.setattr(npc_approach, "_catalogue_record", lambda name: record)
    pose = {"x": 1000, "y": 0, "z": 88}
    resolved = {"canonical_id": "X", "position": pose, "location_source": "live_actor",
                "live_actor": {"position": pose, "rotation": {"yaw": 90}}}
    spots = npc_front(resolved, {**pose, "z": 0})
    assert spots[0]["x"] == pytest.approx(1000)
    assert spots[0]["y"] == pytest.approx(220)
    assert spots[0]["source"] == "live_facing_2.2m"
    resolved["live_actor"]["position"] = {**pose, "x": 2000}
    assert npc_front(resolved, {**pose, "z": 0})[0]["source"] == "catalogue_facing_2.2m"
    resolved["position"] = {**pose, "x": 3000}
    assert npc_front(resolved, {**pose, "x": 3000, "z": 0}) == []


def test_catalogue_front_puts_the_stand_point_first_then_spots_along_her_facing(monkeypatch):
    from ff7r_agent import npc_approach

    record = {"position": {"xyz": [1000.0, 0.0, 188.0], "yaw_deg": 180.0},
              "stand": {"xyz": [790.0, 10.0, 188.0], "point": "obl_X_Cloud"}}
    monkeypatch.setattr(npc_approach, "_catalogue_record", lambda name: record)
    spots = npc_approach.catalogue_front("X", {"x": 1000.0, "y": 0.0, "z": 100.0})
    assert [spot["source"] for spot in spots] == [
        "catalogue_stand_point", "catalogue_facing_2.2m", "catalogue_facing_1.6m", "catalogue_facing_3m"]
    assert spots[0] == {"x": 790.0, "y": 10.0, "z": 100.0, "source": "catalogue_stand_point"}
    assert spots[1]["x"] == pytest.approx(780.0) and spots[1]["y"] == pytest.approx(0.0, abs=1e-6)
    # Standing elsewhere now (another story phase), her facing there is unknown.
    assert npc_approach.catalogue_front("X", {"x": 1500.0, "y": 0.0, "z": 100.0}) == []


def test_nearest_reachable_point_only_walks_linked_floor():
    from ff7r_agent.detour_route import DetourTopologyGraph

    polygons = {name: SimpleNamespace(centroid=Vec3(*xyz)) for name, xyz in {
        "a": (0, 0, 0), "b": (500, 0, 0), "c": (800, 0, 0),   # linked floor
        "counter": (1000, 0, 0),                             # the keeper's own floor, unlinked
    }.items()}
    adjacency = {"a": [SimpleNamespace(to_reference="b")], "b": [SimpleNamespace(to_reference="c")]}
    graph = SimpleNamespace(polygons=polygons, adjacency=adjacency,
                            resolve_point=lambda point: SimpleNamespace(ok=True, selected_reference="a"))
    best = DetourTopologyGraph.nearest_reachable_point(graph, Vec3(0, 0, 0), Vec3(1000, 0, 0))
    assert best == Vec3(800, 0, 0)
    assert DetourTopologyGraph.nearest_reachable_point(graph, Vec3(0, 0, 0), Vec3(5000, 0, 0)) is None


def test_a_capture_that_did_not_help_is_not_repeated_for_a_day(tmp_path, monkeypatch):
    from ff7r_agent.npc_approach import capture_already_failed, remember_failed_capture

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    shop = {"x": 69214.6, "y": -101145.8, "z": 12297.7}
    assert not capture_already_failed(shop, now=1000.0)
    remember_failed_capture(shop, now=1000.0)
    assert capture_already_failed({"x": 69240.0, "y": -101150.0, "z": 0.0}, now=2000.0)  # same 5 m cell
    assert not capture_already_failed(shop, now=1000.0 + 25 * 3600)


def test_a_capture_that_was_not_admitted_fails_with_its_reason_and_is_not_remembered(tmp_path, monkeypatch):
    # Live 2026-09-30 at Gongaga: the job finished without an admitted
    # capture, the approach took it for a capture that did not help, and a
    # day's "already failed" was recorded.
    from ff7r_agent import npc_approach

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    async def rpc(operation, arguments):
        if operation == "start_topology_capture":
            return {"job_id": "capture-1", "state": "running"}
        return {"state": "completed", "response": {"result": {
            "ok": None, "cache_admitted": False,
            "preflight": {"reason": "no_defensible_same_polygon_destination",
                          "failures": ["no_defensible_same_polygon_destination"]}}}}

    with pytest.raises(MissionError, match="topology_capture_not_admitted:no_defensible_same_polygon_destination"):
        asyncio.run(npc_approach.capture_topology(rpc, {"x": 0.0, "y": 0.0, "z": 0.0}, sleep=no_sleep))
    assert not npc_approach.capture_already_failed({"x": 0.0, "y": 0.0, "z": 0.0})


class ReachGraph(FakeGraph):
    """A capture whose polygon nearest the target, reachable from Cloud, is ``nearest``."""

    def __init__(self, nearest, status="complete"):
        super().__init__([(0, 0, 0), (1, 0, 0)], status=status)
        self.nearest, self.reach = nearest, []

    def resolve_point(self, point, **_):
        return SimpleNamespace(ok=True, reference="feet")

    def nearest_reachable_point(self, start, target, max_horizontal_m=6.0, max_vertical_m=3.0, start_resolution=None):
        self.reach.append((max_horizontal_m, max_vertical_m))
        return None if self.nearest is None else Vec3(*self.nearest)


def test_a_staging_leg_ends_at_the_reachable_point_nearest_the_npc():
    from ff7r_agent.npc_approach import staging_point

    graph = ReachGraph((19000.0, 0.0, 50.0))
    staged = staging_point(graph, {"x": 0.0, "y": 0.0, "z": 0.0}, {"x": 30000.0, "y": 0.0, "z": 0.0})
    assert staged["position"] == {"x": 19000.0, "y": 0.0, "z": 50.0}
    assert staged["progress_m"] == 190.0 and staged["remaining_m"] == 110.0
    # Anywhere in the capture, at any height: the NPC is beyond it.
    assert graph.reach == [(float("inf"), float("inf"))]


@pytest.mark.parametrize("nearest,status", [((1000.0, 0.0, 0.0), "complete"), (None, "complete"),
                                            ((19000.0, 0.0, 0.0), "failed")])
def test_no_staging_leg_without_progress_or_a_route(nearest, status):
    from ff7r_agent.npc_approach import staging_point

    graph = ReachGraph(nearest, status=status)
    assert staging_point(graph, {"x": 0.0, "y": 0.0, "z": 0.0}, {"x": 30000.0, "y": 0.0, "z": 0.0}) is None


def test_the_bridge_plans_a_staging_leg_on_the_capture_covering_cloud(monkeypatch):
    from ff7r_agent import npc_approach

    graph, asked = ReachGraph((19000.0, 0.0, 0.0)), []

    def covering(start, target, sha, known_graphs=None, require_target=True, build_missing=True):
        asked.append(require_target)
        return graph, {"covers_target": False}

    monkeypatch.setattr(npc_approach, "load_covering_graph", covering)
    result = npc_approach.plan_approach_in_process(SimpleNamespace(), {"x": 0, "y": 0, "z": 0},
                                                   {"x": 30000, "y": 0, "z": 0}, stage=True)
    assert asked == [False] and result["staging"]["position"]["x"] == 19000.0


def staged_runtime(stagings):
    """Cloud 300 m from Ned; plans miss until he is within 150 m, and each
    staging request answers the next of ``stagings``."""
    from ff7r_agent import npc_approach

    runtime = ScriptedApproach({"x": 30000.0, "y": 0.0, "z": 0.0}, talk_at={"x": 200.0, "y": 0.0})
    original, record = runtime.rpc, {"plans": [], "stages": [], "captures": 0}

    async def rpc(operation, arguments):
        if operation == "plan_npc_approach" and arguments.get("stage"):
            record["stages"].append(dict(arguments["start"]))
            return {"library": {}, "staging": stagings.pop(0) if stagings else None}
        if operation == "plan_npc_approach":
            record["plans"].append(dict(arguments["start"]))
            if arguments["start"]["x"] > 15000.0:
                raise MissionError(npc_approach.NO_CAPTURE_COVERS_ROUTE)
        if operation == "start_topology_capture":
            record["captures"] += 1
            return {"job_id": f"capture-{record['captures']}", "state": "running"}
        if operation == "get_topology_capture_status":
            return {"state": "completed", "response": {"result": {"ok": True, "cache_admitted": True}}}
        return await original(operation, arguments)

    return runtime, rpc, record


def test_an_npc_beyond_clouds_capture_is_reached_by_a_staging_leg(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    runtime, rpc, record = staged_runtime([{"position": {"x": 11000.0, "y": 0.0, "z": 0.0}, "progress_m": 190.0}])
    runs = []
    monkeypatch.setattr(npc_approach, "NavigationMission", mission_moving(runtime, runs))
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Ned", output=tmp_path / "out", identity_check=None,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and record["captures"] == 0 and len(record["stages"]) == 1
    assert runs[0] == (11000.0, 0.0, 88.0) and len(report["staging_legs"]) == 1
    assert report["staging_legs"][0]["arrived_remaining_m"] == 110.0
    assert record["plans"][-1]["x"] == 11000.0 and "topology_capture" not in report


def test_cloud_at_the_edge_of_his_capture_captures_there_and_walks_on(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # The first leg reaches the edge 200 m on; from there the capture gets
    # him no nearer, so the navmesh there is captured and a second leg runs.
    runtime, rpc, record = staged_runtime([{"position": {"x": 20000.0, "y": 0.0, "z": 0.0}}, None,
                                           {"position": {"x": 9000.0, "y": 0.0, "z": 0.0}}])
    runs = []
    monkeypatch.setattr(npc_approach, "NavigationMission", mission_moving(runtime, runs))
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Ned", output=tmp_path / "out", identity_check=None,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and record["captures"] == 1 and len(report["staging_legs"]) == 2
    assert [run[0] for run in runs[:2]] == [20000.0, 9000.0]
    assert not npc_approach.capture_already_failed({"x": 0.0, "y": 0.0, "z": 0.0})


def test_a_staging_leg_that_gets_cloud_no_nearer_stops_the_approach(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    runtime, rpc, record = staged_runtime([{"position": {"x": 11000.0, "y": 0.0, "z": 0.0}}])
    runs = []

    class Stalled:
        def __init__(self, rpc, output, identity_check, config, client_id=None):
            pass

        async def run(self):
            runs.append(1)
            return {"ok": False, "state": "STOPPED", "reason": "route_stalled"}

    monkeypatch.setattr(npc_approach, "NavigationMission", Stalled)
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Ned", output=tmp_path / "out", identity_check=None,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert not report["ok"] and report["reason"] == "npc_approach_staging_leg_stopped:route_stalled"
    assert len(runs) == 1


def test_staging_legs_are_bounded(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # Each leg gets 25 m nearer but never within any capture with Ned.
    legs = [{"position": {"x": 30000.0 - 2500.0 * (n + 1), "y": 0.0, "z": 0.0}} for n in range(8)]
    runtime, rpc, record = staged_runtime(legs)
    original = rpc

    async def never_covered(operation, arguments):
        if operation == "plan_npc_approach" and not arguments.get("stage"):
            raise MissionError(npc_approach.NO_CAPTURE_COVERS_ROUTE)
        return await original(operation, arguments)

    runs = []
    monkeypatch.setattr(npc_approach, "NavigationMission", mission_moving(runtime, runs))
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(never_covered), "Ned", output=tmp_path / "out",
                                                   identity_check=None, execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert not report["ok"] and report["reason"] == "npc_approach_staging_legs_exhausted"
    assert len(runs) == npc_approach.MAX_STAGING_LEGS


class River:
    """Cloud swims while 0 < x < 1000 (a river 10 m wide); land either side.

    A pulse moves him 3 m along the stick's world heading, unless the bank
    he swims into is ``steep`` (he stays put against it).
    """

    def __init__(self, x, steep=None, camera_yaw=0.0):
        self.feet = {"x": float(x), "y": 0.0, "z": 0.0}
        self.steep, self.camera_yaw, self.calls = steep, camera_yaw, []

    def grounded(self):
        return not 0.0 < self.feet["x"] < 1000.0

    async def rpc(self, operation, arguments):
        import math
        self.calls.append(operation)
        if operation == "observe_game":
            return {"player": {"locomotion": {"feet_position": dict(self.feet), "grounded": self.grounded()}},
                    "camera": {"rotation": {"yaw": self.camera_yaw}}}
        if operation == "move_stick":
            # The harness refuses an ordinary pulse while Cloud is not grounded.
            assert arguments.get("swimming") is True
            heading = self.camera_yaw + math.degrees(math.atan2(arguments["x"], arguments["y"]))
            step_x = 300.0 * math.cos(math.radians(heading))
            nxt = self.feet["x"] + step_x
            if self.steep == "far" and nxt >= 1000.0 or self.steep == "near" and nxt <= 0.0:
                return {"ok": True}
            self.feet = {**self.feet, "x": nxt, "y": self.feet["y"] + 300.0 * math.sin(math.radians(heading))}
            return {"ok": True}
        return {"ok": True}


def test_cloud_on_land_is_not_swimming():
    from ff7r_agent.npc_approach import leave_water

    river = River(x=-500.0)
    assert asyncio.run(leave_water(river.rpc, {"x": 5000.0, "y": 0.0, "z": 0.0}, sleep=no_sleep)) is None
    assert "move_stick" not in river.calls


def test_swimming_cloud_carries_on_across_the_river():
    # Live 2026-10-01: a Grasslands route crossed a river Cloud swims in.
    from ff7r_agent.npc_approach import leave_water

    river = River(x=200.0, camera_yaw=90.0)  # the stick is camera-relative
    swim = asyncio.run(leave_water(river.rpc, {"x": 5000.0, "y": 0.0, "z": 0.0}, sleep=no_sleep))
    assert swim["landed"] and swim["to"]["x"] >= 1000.0
    assert [h["heading_degrees"] for h in swim["headings"]] == [0.0]


def test_a_bank_too_steep_to_climb_sends_cloud_back_the_way_he_came():
    from ff7r_agent.npc_approach import leave_water

    river = River(x=800.0, steep="far")
    swim = asyncio.run(leave_water(river.rpc, {"x": 5000.0, "y": 0.0, "z": 0.0}, sleep=no_sleep))
    assert swim["landed"] and swim["to"]["x"] <= 0.0
    assert [h["heading_degrees"] for h in swim["headings"]] == [0.0, 180.0]


def test_a_route_that_ends_in_the_river_swims_out_and_routes_on(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach

    river = River(x=-2000.0)
    runs = []

    class Mission:
        def __init__(self, rpc, output, identity_check, config, client_id=None):
            self.config = config

        async def run(self):
            runs.append(self.config.target_position[0])
            if len(runs) == 1:
                river.feet = {"x": 500.0, "y": 0.0, "z": 0.0}  # into the river, stopped
                return {"ok": False, "state": "STOPPED", "reason": "readiness_lost"}
            river.feet = {"x": 1800.0, "y": 0.0, "z": 0.0}
            return {"ok": True, "state": "ARRIVED"}

    async def rpc(operation, arguments):
        if operation == "resolve_world_target":
            return {"status": "located", "canonical_id": "field_object:ned", "position": {"x": 2000.0, "y": 0.0, "z": 88.0}}
        if operation == "plan_npc_approach":
            return {"library": {}, "approach": {"position": {"x": 1800.0, "y": 0.0, "z": 0.0}}}
        if operation == "read_field_prompt":
            near = abs(river.feet["x"] - 1800.0) < 50
            return {"commands": ["$level_action_talk"] if near else [], "talk": near, "player_position": dict(river.feet)}
        return await river.rpc(operation, arguments)

    monkeypatch.setattr(npc_approach, "NavigationMission", Mission)
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(rpc), "Ned", output=tmp_path, identity_check=None,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and len(runs) == 2
    assert report["swims"][0]["landed"] and report["swims"][0]["after_route"] == 1


def test_routing_waits_for_a_conversation_that_ends_on_its_own():
    from ff7r_agent import npc_approach

    blank = {"map_id": None, "loading": False, "in_combat": False, "target": None,
             "player": {"position": None},
             "queen_blood": {"active": False, "match_screen": False, "begin_match_available": False}}
    field = {**blank, "map_id": "EndGame", "player": {"position": {"x": 1.0, "y": 2.0, "z": 3.0}}}
    samples = iter([blank, blank, blank, field])

    async def rpc(operation, arguments=None):
        assert operation == "observe_game"
        return {"ok": True, "id": 1, "result": next(samples)}

    ticks = iter(range(0, 10_000))
    observed = asyncio.run(npc_approach.wait_for_field(rpc, sleep=no_sleep, clock=lambda: float(next(ticks))))
    assert observed is field


def test_a_start_just_off_the_navmesh_steps_off_before_routing_again(tmp_path, monkeypatch):
    # Live after Michaela's match (#172): Cloud 0.21 m off the navmesh at her
    # table, and three routes refused the same start before any recovery.
    from ff7r_agent import npc_approach

    runtime, runs, steps = ScriptedApproach({"x": 5000.0, "y": 0.0, "z": 0.0}, talk_at={"x": 200.0, "y": 0.0}), [], []

    class Mission:
        def __init__(self, rpc, output, identity_check, config, client_id=None):
            self.config = config

        async def run(self):
            runs.append(self.config.target_position[0])
            if not steps:
                return {"ok": False, "state": "STOPPED", "reason": "live_start_not_inside_current_topology"}
            runtime.feet = {"x": 200.0, "y": 0.0, "z": 0.0}
            return {"ok": True, "state": "ARRIVED"}

    async def extricate(rpc, target, sleep=None):
        steps.append(target)
        return {"moved": True, "moved_m": 1.2}

    monkeypatch.setattr(npc_approach, "NavigationMission", Mission)
    monkeypatch.setattr(npc_approach, "extricate", extricate)
    report = asyncio.run(npc_approach.approach_npc(fake_control_lease(runtime.rpc), "Ned", output=tmp_path, identity_check=None,
                                                   execute=True, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and len(runs) == 2 and len(steps) == 1
    assert report["start_steps"][0]["after_route"] == 1


def test_any_of_several_identical_guide_moogles_resolves_to_the_nearest():
    from ff7r_agent.npc_approach import nearest_interchangeable

    moogle = {"chara_spec_name": "NP0140_00_GoldSaucerRobot_Standard", "display_names": ["Guide Moogle"]}
    static = {"ok": False, "status": "ambiguous", "identity": {"candidates": [
        {**moogle, "object_id": "oba2350_guideMoogle_entrance"}, {**moogle, "object_id": "oba2350_guideMoogle_station"}]}}
    live = {"ok": False, "status": "live_actor_identity_ambiguous",
            "canonical_id": "field_object:2350-GOLDS:oba2350_guideMoogle_entrance",
            "blocked_match_evidence": [[{"static_identity_field": "chara_spec_name"}]],
            "live_candidates": [{"id": "far", "distance": 90.0, "position": {"x": 9000.0, "y": 0.0, "z": 0.0}},
                                {"id": "near", "distance": 36.0, "position": {"x": 3600.0, "y": 0.0, "z": 0.0}}]}
    asked = []

    async def rpc(operation, arguments):
        if operation == "scan_character_inventory":
            raise MissionError("character_inventory_unavailable")
        asked.append(arguments["name"])
        return live

    found = asyncio.run(nearest_interchangeable(rpc, static))
    # Without the inventory, the live resolution's candidates are used.
    assert asked == ["oba2350_guideMoogle_entrance"]
    assert found["status"] == "located" and found["live_actor"] == "near"
    assert found["position"] == {"x": 3600.0, "y": 0.0, "z": 0.0}
    assert found["canonical_id"].endswith("@36,0")

    # Different people sharing a name are not interchangeable.
    other = {"chara_spec_name": "NP0001_Other", "display_names": ["Guide Moogle"]}
    mixed = {"status": "ambiguous", "identity": {"candidates": [static["identity"]["candidates"][0], other]}}
    assert asyncio.run(nearest_interchangeable(rpc, mixed)) is None


def test_the_nearest_loaded_guide_moogle_comes_from_the_character_inventory():
    # Live 2026-10-05: resolving one Moogle record by name returned a single
    # actor of the shared model 440 m away, and Cloud ran past the Moogle
    # in view. The inventory lists every loaded one.
    from ff7r_agent.npc_approach import nearest_interchangeable

    moogle = {"chara_spec_name": "NP0140_00_GoldSaucerRobot_Standard", "display_names": ["Guide Moogle"]}
    static = {"ok": False, "status": "ambiguous", "identity": {"candidates": [
        {**moogle, "object_id": "oba2350_guideMoogle_battleSQ",
         "canonical_id": "field_object:2350-GOLDS:oba2350_guideMoogle_battleSQ"},
        {**moogle, "object_id": "oba2350_guideMoogle_station"}]}}
    robot = "BlueprintGeneratedClass /Game/BluePrint/Character/NonPlayer/NP0140_00_GoldSaucerRobot_Standard."             "NP0140_00_GoldSaucerRobot_Standard_C"
    rows = [{"id": "far", "class": robot, "distance": 440.0, "position": {"x": 70508.0, "y": 4353.0, "z": 0.0}},
            {"id": "other", "class": "BlueprintGeneratedClass /Game/X.Shopkeeper_C", "distance": 5.0,
             "position": {"x": 1.0, "y": 0.0, "z": 0.0}},
            {"id": "near", "class": robot, "distance": 12.0, "position": {"x": 89400.0, "y": -31500.0, "z": 0.0}}]
    asked = []

    async def rpc(operation, arguments):
        asked.append(operation)
        return {"snapshot": {"complete": True, "characters": rows}}

    found = asyncio.run(nearest_interchangeable(rpc, static))
    assert asked == ["scan_character_inventory"]
    assert found["status"] == "located" and found["live_actor"] == "near" and found["live_candidate_count"] == 2
    assert found["canonical_id"] == "field_object:2350-GOLDS:oba2350_guideMoogle_battleSQ@894,-315"


def test_inside_the_gold_saucer_a_moogle_ride_replaces_fast_travel_to_the_entrance(tmp_path, monkeypatch):
    # Live 2026-10-05: from Chocobo Square to the card organiser, Cloud fast
    # travelled to the entrance first, where no Moogle had loaded yet.
    from ff7r_agent import npc_approach

    class SaucerRuntime(ScriptedApproach):
        async def rpc(self, operation, arguments):
            if operation == "resolve_world_target":
                self.calls.append(operation)
                return {"status": "located", "canonical_id": "field_object:2350-GOLDS:oba2350_cardStaff",
                        "position": {"x": 0.0, "y": 0.0, "z": 88.0}}
            return await super().rpc(operation, arguments)

    runtime, runs, travels, rides = SaucerRuntime({"x": 60000.0, "y": 0.0, "z": 0.0}, talk_at={"x": 200.0, "y": 0.0}), [], [], []

    async def travel_to_spot(rpc, spot, sleep=None, clock=None):
        travels.append(spot)
        return {"state": "arrived"}

    async def travel_by_guide_moogle(rpc, destination, **_):
        rides.append(destination)
        runtime.feet = {"x": 3000.0, "y": 0.0, "z": 0.0}
        return {"destination": destination}

    monkeypatch.setattr(npc_approach, "travel_to_spot", travel_to_spot)
    monkeypatch.setattr(npc_approach, "travel_by_guide_moogle", travel_by_guide_moogle)
    monkeypatch.setattr(npc_approach, "worth_guide_moogle", lambda start, target: SimpleNamespace(name="Wonderment Square"))
    monkeypatch.setattr(npc_approach, "NavigationMission", mission_moving(runtime, runs))
    report = asyncio.run(npc_approach.approach_npc(rpc=fake_control_lease(runtime.rpc), name="Tournament Organizer", output=tmp_path,
                                                   identity_check=None, execute=True,
                                                   reset_spot="$map2350", sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert report["ok"] and travels == [] and rides == ["Wonderment Square"]
    assert "travel_first" not in report


def test_a_guide_moogle_beyond_reach_is_looked_for_again_before_riding(tmp_path, monkeypatch):
    # Live 2026-10-05: just after arriving at the entrance the nearest loaded
    # Moogle was 255 m back across the Saucer; seconds later the entrance
    # Moogle 39 m away had loaded.
    from ff7r_agent import npc_approach

    walks, waits = [], []

    async def approach_once(rpc, name, **kwargs):
        walks.append(kwargs["max_start_distance_m"])
        if len(walks) == 1:
            raise MissionError("npc_beyond_reach", response={"start_distance_m": 255.0})
        return {"ok": True, "state": "talk_offered", "face": {"facing": {
            "admission": {"ok": True, "target": {"actor_id": "fixture-moogle"}}}}}

    async def rpc(operation, arguments):
        return {"ok": True, "state": "arrived"} if operation == "use_guide_moogle" else {"ok": True}

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(npc_approach, "_approach_once", approach_once)
    monkeypatch.setattr(npc_approach, "ensure_control_session", lambda rpc: no_sleep(None))
    report = asyncio.run(npc_approach.travel_by_guide_moogle(rpc, "Wonderment Square", output=tmp_path,
                                                             identity_check=None, sleep=sleep))
    assert walks == [150.0, 150.0] and waits == [3.0]
    assert report["beyond_reach"] == {"start_distance_m": 255.0} and report["walk"]["ok"]

    # Still beyond reach: no ride, and the caller walks on.
    walks.clear()

    async def always_far(rpc, name, **kwargs):
        walks.append(name)
        raise MissionError("npc_beyond_reach", response={"start_distance_m": 255.0})

    monkeypatch.setattr(npc_approach, "_approach_once", always_far)
    with pytest.raises(MissionError, match="guide_moogle_not_reached:npc_beyond_reach"):
        asyncio.run(npc_approach.travel_by_guide_moogle(rpc, "Wonderment Square", output=tmp_path,
                                                        identity_check=None, sleep=sleep))
    assert len(walks) == 2


def test_a_stick_tap_waits_out_the_minimum_gap_instead_of_failing():
    # Live 2026-10-06 at Bernard: the turn to face him came straight after the
    # camera alignment, was refused, and the approach gave up beside him.
    from ff7r_agent.npc_approach import move_stick

    calls, slept = [], []

    async def rpc(operation, arguments):
        calls.append(operation)
        if len(calls) == 1:
            return {"ok": False, "error": "minimum delay between movement calls has not elapsed"}
        return {"ok": True}

    async def sleep(seconds):
        slept.append(seconds)

    assert asyncio.run(move_stick(rpc, {"x": 0.0, "y": 1.0}, sleep=sleep)) == {"ok": True}
    assert calls == ["move_stick", "move_stick"] and slept == [0.5]

    async def refusing(operation, arguments):
        return {"ok": False, "error": "real input is not manually armed"}

    with pytest.raises(MissionError, match="not manually armed"):
        asyncio.run(move_stick(refusing, {"x": 0.0, "y": 1.0}, sleep=sleep))


@pytest.mark.parametrize("outcome", ["arrived", "timeout", "nested_failure"])
def test_travel_polls_one_job_past_sync_deadline_without_replay(outcome):
    from ff7r_agent import npc_approach
    now, polls, calls = [0.0], [0], []

    async def sleep(seconds):
        now[0] += seconds

    async def rpc(operation, arguments):
        calls.append(operation)
        if operation == "observe_game":
            return {"map_id": "Kalm", "player": {"position": {"x": 0, "y": 0, "z": 0}}}
        if operation in {"open_control_session", "close_control_session", "stop_all_input"}:
            return {"ok": True}
        if operation == "travel_to_map_spot_async":
            return {"job_id": "one-travel", "state": "running"}
        if operation == "get_travel_to_map_spot_status":
            assert arguments == {"job_id": "one-travel"}
            polls[0] += 1
            if outcome == "timeout" or polls[0] < 10:
                return {"state": "running"}
            return {"state": "completed", "response": {"id": 1, "result": {
                "ok": outcome != "nested_failure", "state": "arrived",
                "arrival_position": {"x": 1, "y": 2, "z": 3}}}}
        pytest.fail(f"unexpected synchronous/input RPC: {operation}")

    operation = npc_approach.travel_to_spot(rpc, "Kalm", sleep=sleep, clock=lambda: now[0])
    if outcome == "arrived":
        assert asyncio.run(operation)["state"] == "arrived"
        assert now[0] == 30.0  # More than the ordinary 25-second RPC budget.
    else:
        with pytest.raises(MissionError):
            asyncio.run(operation)
    assert calls.count("travel_to_map_spot_async") == 1
    assert "wait_for_settled_gameplay" not in calls
    assert calls.count("stop_all_input") == (1 if outcome == "timeout" else 0)


def test_moogle_arrival_uses_bounded_service_default_settling(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ff7r_agent import npc_approach
    calls = []

    async def walk(*args, **kwargs):
        return {"ok": True, "face": {"facing": {
            "admission": {"ok": True, "target": {"actor_id": "fixture-moogle"}}}}}

    monkeypatch.setattr(npc_approach, "_approach_once", walk)
    monkeypatch.setattr(npc_approach, "guide_moogle_zone", lambda _: SimpleNamespace(name="Ghost Square"))

    async def rpc(operation, arguments):
        calls.append((operation, arguments))
        if operation == "open_control_session":
            return {"ok": True}
        if operation == "use_guide_moogle":
            return {"state": "travelling", "arrival": {"x": 0, "y": 0, "z": 0}}
        if operation == "observe_game":
            return {"map_id": "EndGame", "player": {"position": {"x": 0, "y": 0, "z": 0},
                    "locomotion": {"feet_position": {"x": 0, "y": 0, "z": 0}}}}
        if operation == "wait_for_settled_gameplay":
            assert arguments == {}
            return {"ok": True}
        pytest.fail(f"unexpected RPC: {operation}")

    result = asyncio.run(npc_approach.travel_by_guide_moogle(
        rpc, "Ghost Square", output=tmp_path, identity_check=None, sleep=no_sleep))
    assert "arrival_position" in result
    assert sum(op == "use_guide_moogle" for op, _ in calls) == 1


@pytest.mark.parametrize("initial_menu, closes_during_session", [(False, False), (True, True)])
def test_menu_close_never_sends_back_into_field(initial_menu, closes_during_session):
    from ff7r_agent import npc_approach
    state = {"menu": initial_menu}
    taps = []
    async def rpc(operation, arguments):
        if operation == "observe_game":
            return {"in_menu": state["menu"]}
        if operation == "open_control_session" and closes_during_session:
            state["menu"] = False
        if operation == "tap_button":
            taps.append(arguments)
        return {"ok": True}
    asyncio.run(npc_approach.close_main_menu(rpc, sleep=no_sleep))
    assert taps == []


def test_menu_close_back_is_bounded_and_preserves_input_refusal():
    from ff7r_agent import npc_approach
    taps = []
    async def stuck(operation, arguments):
        if operation == "observe_game":
            return {"in_menu": True}
        if operation == "tap_button":
            taps.append(arguments)
        return {"ok": True}
    with pytest.raises(MissionError, match="main_menu_did_not_close"):
        asyncio.run(npc_approach.close_main_menu(stuck, sleep=no_sleep))
    assert taps == [{"button": "cancel", "milliseconds": 400}] * len(npc_approach.MENU_CLOSE_SETTLES_SECONDS)
    async def refused(operation, arguments):
        if operation == "tap_button":
            raise MissionError("runtime_refused", response={"ok": False, "error": "not foreground"})
        return await stuck(operation, arguments)
    taps.clear()
    with pytest.raises(MissionError, match="runtime_refused"):
        asyncio.run(npc_approach.close_main_menu(refused, sleep=no_sleep))
    assert taps == []
def test_dax_preserved_kalm_departure_travels_before_pursuit_with_reset_disabled(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach
    runtime = ScriptedApproach({"x": 61604.070312, "y": -102885.757812, "z": 11802.213867})
    original = runtime.rpc
    async def rpc(operation, arguments):
        if operation == "resolve_world_target":
            runtime.calls.append(operation)
            return {"status": "known_but_not_currently_instantiated", "map_id": "EndGame",
                    "canonical_id": "field_object:2360-UNDRS:oba2360_Actor_CardGamer01"}
        return await original(operation, arguments)
    events = []
    async def travel(rpc, spot, **kwargs):
        events.append(("travel", spot))
        runtime.feet = {"x": 54270., "y": -12280., "z": 3102.91}
        return {"arrival_position": runtime.feet}
    monkeypatch.setattr(npc_approach, "capture_covers", lambda *args: False)
    monkeypatch.setattr(npc_approach, "travel_to_spot", travel)
    monkeypatch.setattr(npc_approach, "NavigationMission", mission_moving(runtime, events))
    report = asyncio.run(npc_approach.approach_npc(rpc=fake_control_lease(rpc), name="Dax", output=tmp_path,
                         identity_check=None, execute=True, pursuit=True, reset_spot=None, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert events[0] == ("travel", "$map2360")
    assert report["travel_first"]["from_distance_m"] == 812.7
    assert report["travel_first"]["target_field"] == "2360-UNDRS"
    assert report["travel_first"]["reason"] == "unlocated_catalogue_target_outside_admitted_capture"
    assert "staging_legs" not in report


@pytest.mark.parametrize("covered,source,live", [(True, "opponent_catalogue_placement", False),
                                                 (False, None, True),
                                                 (False, "opponent_catalogue_placement", True)])
def test_catalogue_arrival_does_not_override_covered_or_live_target(monkeypatch, covered, source, live):
    from ff7r_agent import npc_approach
    monkeypatch.setattr(npc_approach, "capture_covers", lambda *args: covered)
    resolved = {"canonical_id": "field_object:2360-UNDRS:oba2360_Actor_CardGamer01"}
    if live:
        resolved.update(location_source="live_actor", live_actor={"id": "actual"})
    assert npc_approach.catalogue_arrival_admission("Dax", resolved, source,
                              {"x": 54270., "y": -12280.}, {"x": 55107., "y": -21877.}) is None


def test_catalogue_arrival_requires_matching_explicit_area(monkeypatch):
    from ff7r_agent import npc_approach, qb_opponents
    monkeypatch.setattr(npc_approach, "capture_covers", lambda *args: False)
    monkeypatch.setattr(qb_opponents, "opponent", lambda *args: {
        "field": "2360-UNDRS", "travel_spot": {"key": "$map1110", "goto": "1110-TOWN", "same_area": True}})
    assert npc_approach.catalogue_arrival_admission("Dax", {}, "opponent_catalogue_placement",
                                                {"x": 0, "y": 0}, {"x": 1, "y": 1}) is None


def test_nearby_uninstantiated_dax_uses_covered_pursuit_without_travel(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach
    runtime, runs = ScriptedApproach({"x": 54900., "y": -21700., "z": 3019.}), []
    original = runtime.rpc
    async def rpc(operation, arguments):
        if operation == "resolve_world_target":
            runtime.calls.append(operation)
            return {"status": "known_but_not_currently_instantiated", "map_id": "EndGame",
                    "canonical_id": "field_object:2360-UNDRS:oba2360_Actor_CardGamer01"}
        return await original(operation, arguments)
    async def no_travel(*args, **kwargs):
        pytest.fail("covered nearby catalogue target must retain pursuit")
    monkeypatch.setattr(npc_approach, "capture_covers", lambda *args: True)
    monkeypatch.setattr(npc_approach, "travel_to_spot", no_travel)
    monkeypatch.setattr(npc_approach, "NavigationMission", mission_moving(runtime, runs))
    report = asyncio.run(npc_approach.approach_npc(rpc=fake_control_lease(rpc), name="Dax", output=tmp_path,
                         identity_check=None, execute=True, pursuit=True, reset_spot=None, sleep=no_sleep, model_id="test-model", reasoning_effort="test-effort"))
    assert "travel_first" not in report
    assert "plan_npc_approach" in runtime.calls
    assert runs and report["pursuit"]


@pytest.mark.parametrize("model,effort", [("test-model", None), (None, "high"), (" ", "high"), ("test-model", "")])
def test_attributed_approach_requires_paired_nonempty_metadata(model, effort):
    from ff7r_agent.npc_approach import AttributedApproachRpc
    with pytest.raises(ValueError):
        AttributedApproachRpc(None, model_id=model, reasoning_effort=effort)


def test_new_approach_lease_requires_metadata_before_open():
    from ff7r_agent.npc_approach import AttributedApproachRpc, ensure_control_session
    calls = []
    async def rpc(op, args):
        calls.append((op, args))
        return {"active": False}
    with pytest.raises(MissionError, match="requires_explicit_model_and_effort"):
        asyncio.run(ensure_control_session(AttributedApproachRpc(rpc)))
    assert calls == [("get_control_session_status", {})]


@pytest.mark.parametrize("helper", ["ensure", "hold", "travel"])
def test_approach_paths_forward_exact_metadata(helper):
    from ff7r_agent import npc_approach
    calls, lease = [], {"active": False}
    async def rpc(op, args):
        calls.append((op, args))
        if op == "get_control_session_status":
            return dict(lease)
        if op == "open_control_session":
            lease.update(active=True, client_id=args["client_id"])
            return dict(lease)
        if op == "observe_game":
            return {"map_id": "EndGame", "player": {"position": {"x": 1, "y": 2, "z": 3}}}
        if op == "travel_to_map_spot_async":
            return {"state": "completed", "response": {"result": {"state": "already_there", "arrival_position": {"x": 1, "y": 2, "z": 3}}}}
        return {"ok": True}
    bound = npc_approach.AttributedApproachRpc(rpc, model_id="reported-model", reasoning_effort="reported-effort", client_id="caller")
    if helper == "hold":
        asyncio.run(npc_approach.hold_control_session(bound, "caller"))
    elif helper == "travel":
        asyncio.run(npc_approach.travel_to_spot(bound, "Kalm", sleep=no_sleep))
    else:
        asyncio.run(npc_approach.ensure_control_session(bound))
    opens = [args for op, args in calls if op == "open_control_session"]
    assert opens == [{"client_id": "caller", "model_id": "reported-model", "reasoning_effort": "reported-effort"}]


@pytest.mark.parametrize("op", ["open_control_session", "close_control_session"])
def test_attributed_rpc_borrows_but_never_replaces_or_closes_a_foreign_session(op):
    from ff7r_agent.npc_approach import AttributedApproachRpc
    calls = []
    async def rpc(operation, args):
        calls.append(operation)
        return {"active": True, "client_id": "other-caller"}
    bound = AttributedApproachRpc(rpc, model_id="reported-model", reasoning_effort="high", client_id="caller")
    result = asyncio.run(bound(op, {}))
    assert calls == ["get_control_session_status"]  # nothing opened, nothing closed
    assert result["client_id"] == "other-caller"
    assert op != "close_control_session" or result == {"closed": False, "reason": "session_borrowed", "client_id": "other-caller"}
    assert bound.opened_session is False


def test_attributed_rpc_passes_an_emergency_stop_through_and_guards_its_own_close():
    from ff7r_agent.npc_approach import AttributedApproachRpc
    calls = []
    async def rpc(operation, args):
        calls.append((operation, args))
        return {"active": True, "client_id": "other-caller"} if operation == "get_control_session_status" else {"ok": True}
    bound = AttributedApproachRpc(rpc, client_id="caller")
    asyncio.run(bound("stop_all_input", {}))
    assert calls[-1] == ("stop_all_input", {})
    calls.clear()

    async def own(operation, args):
        calls.append((operation, args))
        return {"active": True, "client_id": "caller"} if operation == "get_control_session_status" else {"ok": True}
    asyncio.run(AttributedApproachRpc(own, client_id="caller")("close_control_session", {}))
    assert calls[-1] == ("close_control_session", {"client_id": "caller"})


def test_gold_saucer_ride_uses_attributed_lease(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach
    calls = []
    async def rpc(op, args):
        calls.append((op, args))
        if op == "get_control_session_status":
            return {"active": False}
        if op == "open_control_session":
            return {"active": True, "client_id": args["client_id"]}
        pytest.fail("unexpected runtime operation")
    async def ride(bound, zone, **options):
        await npc_approach.ensure_control_session(bound)
        return {"destination": zone}
    monkeypatch.setattr(npc_approach, "travel_by_guide_moogle", ride)
    report = asyncio.run(npc_approach.ride_guide_moogle(rpc, "Ghost Square", output=tmp_path,
        identity_check=None, model_id="reported-model", reasoning_effort="reported-effort", client_id="caller"))
    assert report["ok"]
    assert calls[-1] == ("open_control_session", {"client_id": "caller", "model_id": "reported-model", "reasoning_effort": "reported-effort"})


@pytest.mark.parametrize("target", [["--name", "Dax"], ["--gold-saucer-zone", "Ghost Square"]])
def test_npc_cli_forwards_explicit_attribution(target, tmp_path, monkeypatch):
    from ff7r_agent import npc_approach
    calls = []
    async def run(name, output, check, **options):
        calls.append(options)
        return {"ok": True}
    monkeypatch.setattr(npc_approach, "run_http", run)
    assert npc_approach.main(target + ["--execute", "--model-id", "reported-model", "--reasoning-effort", "reported-effort", "--client-id", "caller", "--output", str(tmp_path)]) == 0
    assert calls[0]["model_id"] == "reported-model" and calls[0]["reasoning_effort"] == "reported-effort" and calls[0]["client_id"] == "caller"


@pytest.mark.parametrize("flags", [["--model-id", "model"], ["--reasoning-effort", "high"], ["--model-id", " ", "--reasoning-effort", "high"]])
def test_npc_cli_rejects_partial_attribution_before_http(flags, monkeypatch):
    from ff7r_agent import npc_approach
    monkeypatch.setattr(npc_approach, "run_http", lambda *a, **k: pytest.fail("must not connect"))
    with pytest.raises(SystemExit):
        npc_approach.main(["--name", "Dax", *flags])


@pytest.fixture(autouse=True)
def heading_contract_for_non_facing_route_fakes(monkeypatch):
    # These route fixtures predate body-heading telemetry. Model an already
    # verified heading only for those incomplete fakes; dedicated real-heading
    # tests retain production logic and the fallback tests run without this shim.
    from ff7r_agent import npc_approach
    original=npc_approach.face_body_toward
    async def body(rpc,target,sleep=None):
        observed=await rpc('observe_game',{})
        if not isinstance((observed.get('player',{}).get('rotation') or {}).get('yaw'),(int,float)):
            return {'ok':True,'taps':0,'heading_error_degrees':0,'fixture_heading_contract':True}
        return await original(rpc,target,sleep=sleep)
    monkeypatch.setattr(npc_approach,'face_body_toward',body)


def _lent_session_rpc(client_id):
    """A lease the caller already holds; the emergency stop closes it."""
    lease = {"active": True, "client_id": client_id}
    calls = []

    async def rpc(operation, arguments):
        calls.append(operation)
        if operation == "get_control_session_status":
            return dict(lease)
        if operation == "open_control_session":
            lease.update(active=True, client_id=arguments.get("client_id"))
            return dict(lease)
        if operation in {"stop_all_input", "close_control_session"}:
            lease.update(active=False, client_id=None)
        return {"ok": True}
    return rpc, lease, calls


@pytest.mark.parametrize("stopped", [False, True])
def test_a_session_lent_to_the_approach_stays_open_unless_the_run_stopped(monkeypatch, tmp_path, stopped):
    from ff7r_agent import npc_approach
    # Live 2026-10-12 (#342): the approach's closing emergency stop closed
    # the session startup opened, and the next match call found it latched.
    # A run now leaves the session open; only a run that has to stop says so.
    rpc, lease, calls = _lent_session_rpc("session-1")

    async def approach(rpc, name, **_):
        await rpc("get_control_session_status", {})  # a real run reads it before any input
        if stopped:
            await rpc("stop_all_input", {})
        return {"ok": not stopped, "state": "stopped" if stopped else "talk_offered"}

    monkeypatch.setattr(npc_approach, "_approach_once", approach)
    report = asyncio.run(npc_approach.approach_npc(rpc, "Ned", output=tmp_path, identity_check=None, execute=True,
                                                   model_id="m", reasoning_effort="high", client_id="session-1"))

    assert lease["active"] is (not stopped)
    assert report["control_session"] == ("closed" if stopped else "left_open")
    assert "close_control_session" not in calls
    assert json.loads((tmp_path / "result.json").read_text())["control_session"] == report["control_session"]


def test_an_approach_without_a_session_at_start_leaves_none_open(monkeypatch, tmp_path):
    from ff7r_agent import npc_approach
    rpc, lease, calls = _lent_session_rpc("someone")
    lease.update(active=False, client_id=None)

    async def approach(rpc, name, **_):
        return {"ok": True, "state": "talk_offered"}

    monkeypatch.setattr(npc_approach, "_approach_once", approach)
    report = asyncio.run(npc_approach.approach_npc(rpc, "Ned", output=tmp_path, identity_check=None, execute=True,
                                                   model_id="m", reasoning_effort="high", client_id="session-1"))
    assert "control_session" not in report and lease["active"] is False
    assert "open_control_session" not in calls
