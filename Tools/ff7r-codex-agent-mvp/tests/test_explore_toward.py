from __future__ import annotations

import math

import pytest

from ff7r_agent import explore_mode
from ff7r_agent.explore_mode import choose_leg
from ff7r_agent.models import Vec3

GOAL = Vec3(6705.0, -259836.0, 23732.0)  # the Mt. Nibel bridge marker, live 2026-10-20


def candidate(x, y, z, kind="far_end"):
    return {"position": {"x": x, "y": y, "z": z}, "reachable": True, "kind": kind}


def test_the_leg_is_the_reachable_point_nearest_the_goal_in_3d():
    feet = Vec3(31800.0, -252000.0, 13000.0)
    low = candidate(19965.0, -261412.0, 17668.0)    # nearer on the map, but 60 m below the bridge
    high = candidate(18480.0, -257328.0, 18628.0)   # a little further on the map, 9.5 m higher
    assert choose_leg([low, high], GOAL, feet, []) is high


def test_points_walked_to_or_no_nearer_are_skipped():
    feet = Vec3(20000.0, -258000.0, 18000.0)
    tried = candidate(19000.0, -258500.0, 19000.0)
    behind = candidate(40000.0, -250000.0, 10000.0)
    assert choose_leg([tried, behind], GOAL, feet, [Vec3(19050.0, -258520.0, 19000.0)]) is None


class Walker:
    """Cloud and a far goal: each leg walks him to its target."""

    def __init__(self, stops=None):
        self.position = {"x": 31800.0, "y": -252000.0, "z": 13000.0}
        self.walks, self.captures = [], []
        self.stops = list(stops or [])
        self._explore_views = {}

    def observe(self):
        return {"map_id": "EndGame", "running": True, "telemetry": {"live": True}, "in_combat": False,
                "player": {"position": dict(self.position), "rotation": {"yaw": 0.0},
                           "locomotion": {"feet_position": dict(self.position), "grounded": True}}}


@pytest.fixture
def walker(monkeypatch):
    cloud = Walker()
    legs = iter([candidate(25000.0, -255000.0, 16000.0), candidate(15000.0, -258000.0, 20000.0),
                 candidate(7000.0, -259800.0, 23700.0)])
    current = {"next": next(legs)}

    def leg_candidates(service, feet, yaw):
        return [current["next"]]

    def walk(service, lifecycle, values):
        cloud.walks.append((values["x"], values["y"], values["z"]))
        if cloud.stops:
            return {"ok": False, "outcome": "stopped", "stop_reason": cloud.stops.pop(0), "input_sent": True}
        cloud.position = {"x": values["x"], "y": values["y"], "z": values["z"]}
        current["next"] = next(legs, current["next"])
        return {"ok": True, "outcome": "arrived", "reason": "arrived", "input_sent": True}

    monkeypatch.setattr(explore_mode, "_leg_candidates", leg_candidates)
    monkeypatch.setattr(explore_mode, "_capture_for", lambda s, l, targets: cloud.captures.append(targets) or {"admitted": True})
    monkeypatch.setattr(explore_mode, "run_explore_goto", walk)
    monkeypatch.setattr(explore_mode, "_lease_active", lambda service: True)
    return cloud


def test_legs_walk_on_until_the_goal_is_reached(walker):
    result = explore_mode.run_explore_toward(walker, None, {"x": GOAL.x, "y": GOAL.y, "z": GOAL.z, "execute": True})
    assert result["ok"] and result["outcome"] == "arrived" and len(walker.walks) == 3
    # Every leg had a nearer point on the graph in hand, so nothing was captured.
    assert walker.captures == []
    assert result["final_distance_m"] < explore_mode.TOWARD_ARRIVE_M


def test_a_fight_on_a_leg_stops_the_walk_for_the_caller(walker):
    walker.stops = ["combat_active"]
    result = explore_mode.run_explore_toward(walker, None, {"x": GOAL.x, "y": GOAL.y, "z": GOAL.z, "execute": True})
    assert result["ok"] is False and result["reason"] == "combat_active" and len(walker.walks) == 1


def test_legs_that_gain_nothing_stop_the_walk(walker, monkeypatch):
    def nowhere(service, lifecycle, values):
        walker.walks.append(values)
        return {"ok": False, "outcome": "stopped", "reason": "traversal_link_not_crossed", "input_sent": True}

    monkeypatch.setattr(explore_mode, "run_explore_goto", nowhere)
    monkeypatch.setattr(explore_mode, "choose_leg",
                        lambda candidates, goal, feet, tried: candidate(25000.0 - len(tried), -255000.0, 16000.0))
    result = explore_mode.run_explore_toward(walker, None, {"x": GOAL.x, "y": GOAL.y, "z": GOAL.z, "execute": True})
    assert result["reason"] == "toward_no_progress" and len(walker.walks) == explore_mode.TOWARD_STALL_LEGS


def test_the_objective_marker_is_a_goal(walker, monkeypatch):
    from ff7r_agent import live_objective
    monkeypatch.setattr(live_objective, "get_active_objective",
                        lambda service: {"ok": True, "step_id": "2660_Ch01_Step045",
                                         "position": {"x": GOAL.x, "y": GOAL.y, "z": GOAL.z},
                                         "sub_objectives": {"items": [{"state": "active", "step_id": "s030",
                                                                       "position": {"x": 1.0, "y": 2.0, "z": 3.0}}]}})
    goal, source = explore_mode.toward_goal(walker, {"objective": True}, 0.0)
    assert goal == GOAL and source["step_id"] == "2660_Ch01_Step045"
    goal, source = explore_mode.toward_goal(walker, {"objective": "sub"}, 0.0)
    assert (goal.x, goal.y, goal.z) == (1.0, 2.0, 3.0) and source["form"] == "sub_objective"


def test_leg_options_need_toward():
    with pytest.raises(ValueError, match="toward"):
        explore_mode.run_explore_goto(Walker(), None, {"x": 1.0, "y": 2.0, "max_legs": 3})



def test_with_no_candidate_nearer_the_goal_itself_is_walked_to(walker, monkeypatch):
    monkeypatch.setattr(explore_mode, "_leg_candidates", lambda service, feet, yaw: [])
    result = explore_mode.run_explore_toward(walker, None, {"x": GOAL.x, "y": GOAL.y, "z": GOAL.z, "execute": True})
    assert result["ok"] and result["outcome"] == "arrived" and walker.walks == [(GOAL.x, GOAL.y, GOAL.z)]


def test_where_the_navmesh_ends_a_sprint_at_the_goal_comes_before_a_capture(walker, monkeypatch):
    """Live 2026-10-21: cave ledges the navmesh stopped at, hopped down at a sprint."""
    monkeypatch.setattr(explore_mode, "_leg_candidates", lambda service, feet, yaw: [])
    monkeypatch.setattr(explore_mode, "run_explore_goto", lambda service, lifecycle, values: {
        "ok": True, "outcome": "arrived", "reason": "arrived", "input_sent": True})
    sprints = []
    drops = iter([700.0])

    def travel_vector_chunk(x, y, maximum_ms, sprint):
        if not sprint:
            return {"ok": True}
        sprints.append((x, y, maximum_ms, sprint))
        step = next(drops, 0.0)
        walker.position = {"x": walker.position["x"] - step, "y": walker.position["y"] - step,
                           "z": walker.position["z"] - step}
        return {"ok": bool(step), "outcome": "maximum_hold_elapsed"}

    walker.travel_vector_chunk = travel_vector_chunk
    walker.align_camera_to_bearing = lambda **kwargs: pytest.fail("the camera is not turned for a sprint")
    monkeypatch.setattr(explore_mode.time, "sleep", lambda seconds: None)
    result = explore_mode.run_explore_toward(walker, None, {"x": GOAL.x, "y": GOAL.y, "z": GOAL.z, "execute": True})
    assert result["ok"] and sprints[0][2:] == (explore_mode.TOWARD_SPRINT_MS, True)
    # Camera yaw 0 faces +x; the goal lies at -x, -y of Cloud: the analogue stick points back and left.
    assert sprints[0][0] < 0 and sprints[0][1] < 0
    # The first sprint gained ground, so no capture; the next gained none, so then one capture.
    assert result["legs"][0]["sprint"]["gained_m"] > explore_mode.TOWARD_SPRINT_MIN_GAIN_M
    assert "capture" not in result["legs"][0] and walker.captures == [[GOAL]]


def test_a_sprint_that_gains_nothing_ends_the_walk(walker, monkeypatch):
    monkeypatch.setattr(explore_mode, "_leg_candidates", lambda service, feet, yaw: [])
    monkeypatch.setattr(explore_mode, "run_explore_goto", lambda service, lifecycle, values: {
        "ok": False, "outcome": "refused", "reason": "target_off_navmesh", "input_sent": False})
    walker.travel_vector_chunk = lambda x, y, maximum_ms, sprint: {"ok": False, "outcome": "blocked"}
    walker.align_camera_to_bearing = lambda target_bearing_degrees: None
    monkeypatch.setattr(explore_mode.time, "sleep", lambda seconds: None)
    result = explore_mode.run_explore_toward(walker, None, {"x": GOAL.x, "y": GOAL.y, "z": GOAL.z, "execute": True})
    assert result["reason"] == "toward_no_candidate_nearer" and result["legs"][0]["sprint"]["gained_m"] == 0.0


def test_a_capture_is_taken_only_when_no_point_is_nearer(walker, monkeypatch):
    """Live 2026-10-21: a capture cost about 4 min and left a cold graph each leg."""
    offered = iter([[], [candidate(7000.0, -259800.0, 23700.0)]])
    monkeypatch.setattr(explore_mode, "_leg_candidates", lambda service, feet, yaw: next(offered))
    walker.travel_vector_chunk = lambda x, y, maximum_ms, sprint: {"ok": False}
    walker.align_camera_to_bearing = lambda target_bearing_degrees: None
    monkeypatch.setattr(explore_mode.time, "sleep", lambda seconds: None)
    result = explore_mode.run_explore_toward(walker, None, {"x": GOAL.x, "y": GOAL.y, "z": GOAL.z, "execute": True})
    assert result["ok"] and walker.captures == [[GOAL]] and len(walker.walks) == 1
def test_a_leg_waits_for_a_graph_still_being_built(monkeypatch):
    attempts = []

    def load(service, position, target, wait_seconds):
        attempts.append(wait_seconds)
        if len(attempts) < 3:
            raise explore_mode.ExploreRefused(explore_mode.GRAPH_BUILDING_REASON)
        return object(), {}

    monkeypatch.setattr(explore_mode, "load_view_graph", load)
    monkeypatch.setattr(explore_mode.explore_map, "analyse_view", lambda *a, **k: None)
    monkeypatch.setattr(explore_mode.explore_map, "public_candidates", lambda analysis: [candidate(1.0, 2.0, 3.0)])
    assert explore_mode._leg_candidates(None, Vec3(0.0, 0.0, 0.0), 0.0) == [candidate(1.0, 2.0, 3.0)]
    assert len(attempts) == 3


def test_a_blocked_sprint_tries_either_side(walker, monkeypatch):
    """Live 2026-10-21: a mako crystal stood straight between Cloud and the spring."""
    sprints, steps = [], []

    def travel_vector_chunk(x, y, maximum_ms, sprint):
        if not sprint:  # the step back off the crystal between headings
            assert maximum_ms == explore_mode.TOWARD_BACK_STEP_MS
            assert (x, y) == pytest.approx((-sprints[-1][0], -sprints[-1][1]))
            steps.append(len(sprints))
            return {"ok": True}
        sprints.append((x, y))
        if len(sprints) == 3:  # the crystal blocks straight on and 30 degrees one way
            walker.position = {"x": walker.position["x"] - 400.0, "y": walker.position["y"] - 400.0,
                               "z": walker.position["z"]}
        return {"ok": True}

    walker.travel_vector_chunk = travel_vector_chunk
    monkeypatch.setattr(explore_mode.time, "sleep", lambda seconds: None)
    result = explore_mode._sprint_toward(walker, GOAL)
    assert result["ok"] and [item["offset_degrees"] for item in result["tries"]] == [0.0, 30.0, -30.0]
    assert steps == [1, 2]  # a step back after each blocked heading
    headings = [math.degrees(math.atan2(x, y)) for x, y in sprints]  # camera yaw 0: stick angle = bearing offset
    assert headings[1] - headings[0] == pytest.approx(-30.0, abs=0.2) or headings[1] - headings[0] == pytest.approx(30.0, abs=0.2)
