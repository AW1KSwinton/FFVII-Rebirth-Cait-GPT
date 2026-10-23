from __future__ import annotations

import copy
import json
import threading
import time

import pytest

from explore_fixtures import corridor_world, decode_png
from ff7r_agent import explore_map, explore_mode, runtime_commands
from ff7r_agent.explore_mode import ExploreRefused
from ff7r_agent.models import Vec3


def make_state(x=250.0, y=250.0, z=0.0, yaw=0.0, **extra):
    state = {
        "map_id": "EndGame", "sequence": 1, "in_combat": False, "in_menu": False, "paused": False, "loading": False,
        "player": {"position": {"x": x, "y": y, "z": z + 88.0}, "rotation": {"yaw": yaw},
                   "locomotion": {"feet_position": {"x": x, "y": y, "z": z}}},
        "nearby_actors": [], "nearby_interactables": [],
    }
    state.update(extra)
    return state


class FakeService:
    """Plays back observe() results; any stop is a test failure."""

    def __init__(self, *states):
        self.states = list(states or [make_state()])
        self.calls = []
        self.cutscene = {"active": False}
        self.prewarms = []

    def observe(self):
        self.calls.append("observe")
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return copy.deepcopy(state)

    def prewarm_navigation(self, x, y, z=None, **_):
        self.prewarms.append((x, y, z))
        return {"ok": True, "state": "started"}

    def cutscene_state(self):
        return dict(self.cutscene)

    def _runtime_landmarks(self, map_id):
        return []

    def stop(self):
        raise AssertionError("explore mode must never call the stop operation")


@pytest.fixture
def world(monkeypatch):
    graph = corridor_world()
    monkeypatch.setattr(explore_mode, "load_view_graph", lambda service, position, target=None, wait_seconds=0: (graph, {"selected": None}))
    return graph


@pytest.fixture
def no_session_changes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("explore mode must not open, close or heartbeat the control session")

    for name in ("request_session_open", "request_session_close", "request_session_heartbeat"):
        monkeypatch.setattr(runtime_commands, name, forbidden)


# ------------------------------------------------------------------ explore_view

def test_view_writes_a_png_and_returns_transform_player_and_candidates(world, tmp_path):
    service = FakeService(make_state(nearby_actors=[
        {"id": "npc_1", "name": "Guard", "kind": "npc", "position": {"x": 2100.0, "y": 700.0, "z": 88.0}}]))
    result = explore_mode.explore_view(service, radius_m=40.0, output_dir=str(tmp_path))
    assert result["ok"] is True and result["input_sent"] is False
    json.dumps(result, allow_nan=False)  # plain JSON all the way down
    width, height, _ = decode_png(open(result["image"]["path"], "rb").read())
    assert (width, height) == (result["image"]["width"], result["image"]["height"]) == (640, 640)
    transform = explore_map.ViewTransform.from_dict(result["transform"])
    assert transform.to_pixel(250.0, 250.0) == pytest.approx((result["player"]["pixel"]["px"], result["player"]["pixel"]["py"]), abs=1)
    assert transform.to_world(320.0, 320.0) == pytest.approx((250.0, 250.0))  # the player is the view's centre
    player = result["player"]
    assert player["position"] == {"x": 250.0, "y": 250.0, "z": 0.0} and player["position_reference"] == "feet"
    assert player["on_navmesh"] and player["route_start_ok"] and player["island_polygons"] == 30
    assert player["yaw_deg"] == 0.0
    kinds = {c["kind"] for c in result["candidates"]}
    assert {"far_end", "other_island", "poi"} <= kinds
    guard = next(c for c in result["candidates"] if c.get("label") == "Guard")
    assert guard["reachable"] and guard["pixel"]["in_view"]
    far = next(c for c in result["candidates"] if c["kind"] == "far_end")
    px, py = transform.to_pixel(far["position"]["x"], far["position"]["y"])
    assert (far["pixel"]["px"], far["pixel"]["py"]) == (round(px), round(py))
    assert result["capture_bounds_m"]["x"] == [0.0, 50.0]
    assert service.prewarms == [(250.0, 250.0, 0.0)]  # the validator and graph warm up for the walk
    assert "explore_goto_async" in result["usage"]


def test_view_without_an_actor_feet_sample_lowers_the_actor_origin(world, tmp_path):
    state = make_state()
    del state["player"]["locomotion"]
    state["player"]["position"]["z"] = 88.0
    result = explore_mode.explore_view(FakeService(state), output_dir=str(tmp_path))
    assert result["player"]["position"]["z"] == 0.0
    assert result["player"]["position_reference"] == "actor_origin_less_88cm"


def test_view_can_be_centred_elsewhere_and_validates_its_arguments(world, tmp_path):
    result = explore_mode.explore_view(FakeService(), radius_m=20.0, center_x=3000.0, center_y=750.0, output_dir=str(tmp_path))
    transform = explore_map.ViewTransform.from_dict(result["transform"])
    assert transform.to_world(transform.width / 2, transform.height / 2) == pytest.approx((3000.0, 750.0))
    assert result["player"]["pixel"]["px"] < 0  # the player is outside this window
    for bad in ({"radius_m": 5.0}, {"radius_m": 400.0}, {"px_per_m": 40.0}, {"center_x": 1.0}, {"radius_m": True},
                {"wait_seconds": -1.0}, {"show_islands": "yes"}):
        with pytest.raises(ValueError):
            explore_mode.explore_view(FakeService(), output_dir=str(tmp_path), **bad)


def test_view_on_a_blank_feed_explains_instead_of_guessing(world, tmp_path):
    blank = explore_mode.explore_view(FakeService({"map_id": None, "player": None}), output_dir=str(tmp_path))
    assert blank["ok"] is False and blank["reason"] == "field_telemetry_unavailable" and blank["input_sent"] is False
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("flags", [{"telemetry": {"live": False}}, {"running": False}])
def test_retained_positions_in_a_stale_or_stopped_feed_cannot_start_explore(monkeypatch, tmp_path, flags):
    monkeypatch.setattr(explore_mode, "load_view_graph", lambda *a, **k: pytest.fail("must not load stale geometry"))
    monkeypatch.setattr(explore_mode, "_run_route", lambda *a, **k: pytest.fail("must not route on stale telemetry"))
    service = FakeService(make_state(**flags))
    view = explore_mode.explore_view(service, output_dir=str(tmp_path))
    walk = explore_mode.run_explore_goto(service, object(), {"x": 1250.0, "y": 750.0, "execute": True})
    assert view["reason"] == walk["reason"] == "field_telemetry_unavailable"
    assert view["input_sent"] is walk["input_sent"] is False and not list(tmp_path.iterdir())


def test_view_without_a_covering_capture_names_the_capture_operation(monkeypatch, tmp_path):
    from ff7r_agent import npc_approach
    from ff7r_agent.navigation_values import MissionError

    def refuse(*args, **kwargs):
        raise MissionError(npc_approach.NO_CAPTURE_COVERS_START)

    monkeypatch.setattr(npc_approach, "load_covering_graph", refuse)
    result = explore_mode.explore_view(FakeService(), output_dir=str(tmp_path))
    assert result["ok"] is False and result["reason"] == "topology_not_captured_here"
    assert result["capture"]["operation"] == "start_topology_capture"
    assert result["capture"]["status_operation"] == "get_topology_capture_status"
    assert result["input_sent"] is False


def test_a_slow_graph_build_runs_on_and_the_next_view_finds_it(monkeypatch, tmp_path):
    from ff7r_agent import npc_approach

    graph, release, calls = corridor_world(), threading.Event(), []

    def slow(start, target, sha, cache_manifest=None, known_graphs=None, require_target=True, build_missing=True):
        calls.append(start)
        release.wait(5)
        return graph, {"selected": "abc"}

    monkeypatch.setattr(npc_approach, "load_covering_graph", slow)
    service = FakeService()
    waiting = explore_mode.explore_view(service, output_dir=str(tmp_path), wait_seconds=0.05)
    assert waiting["ok"] is False and waiting["reason"] == "topology_graph_building" and waiting["retry_after_seconds"] == 5
    again = explore_mode.explore_view(service, output_dir=str(tmp_path), wait_seconds=0.05)
    assert again["reason"] == "topology_graph_building" and len(calls) == 1  # joined, not rebuilt
    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        done = explore_mode.explore_view(service, output_dir=str(tmp_path), wait_seconds=1.0)
        if done["ok"]:
            break
    assert done["ok"] is True and len(calls) == 1
    assert done["library"]["selected"] == "abc"


def test_graph_load_failures_are_reported_not_raised(monkeypatch, tmp_path):
    from ff7r_agent import npc_approach

    monkeypatch.setattr(npc_approach, "load_covering_graph", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    result = explore_mode.explore_view(FakeService(), output_dir=str(tmp_path))
    assert result["reason"] == "topology_load_failed" and "OSError" in result["detail"]


def test_completed_graph_loads_reselect_the_library_and_reuse_graphs_by_sha(monkeypatch):
    from ff7r_agent import npc_approach

    graph, calls = corridor_world(), []

    def load(start, target, sha, **kwargs):
        known = kwargs["known_graphs"]
        calls.append((start, target, graph is known.get("SHA")))
        known["SHA"] = graph
        return graph, {"selected": f"capture-{len(calls)}"}

    monkeypatch.setattr(npc_approach, "load_covering_graph", load)
    service = FakeService()
    position = {"x": 250.0, "y": 250.0, "z": 0.0}
    for index, offset in enumerate((0.0, 1.0, 1.0), start=1):
        start = dict(position, x=position["x"] + offset)
        loaded, decision = explore_mode.load_view_graph(service, start, wait_seconds=1.0)
        assert loaded is graph and decision["selected"] == f"capture-{index}"
    assert [reused for _, _, reused in calls] == [False, True, True]
    assert not service._explore_graph_builds


def test_pending_graph_selection_does_not_join_a_different_target(monkeypatch):
    from concurrent.futures import Future
    from ff7r_agent import npc_approach

    calls = []
    graph = corridor_world()
    monkeypatch.setattr(npc_approach, "load_covering_graph",
                        lambda start, target, *a, **k: (calls.append(target) or graph, {}))
    service = FakeService()
    position = {"x": 250.0, "y": 250.0, "z": 0.0}
    old = Future()
    old.set_result((graph, {"selected": "old"}))
    service._explore_graph_builds = {(tuple(position.values()), tuple(position.values())): old}
    target = dict(position, x=1000.0)
    _, decision = explore_mode.load_view_graph(service, position, target, wait_seconds=1.0)
    assert calls == [target] and decision != {"selected": "old"}


def test_graph_wait_budget_covers_both_an_existing_build_and_the_new_one(monkeypatch):
    from types import SimpleNamespace

    now, timeouts = [100.0], []

    def wait_other(timeout):
        timeouts.append(timeout)
        now[0] += 19.0

    def wait_new(timeout):
        timeouts.append(timeout)
        raise explore_mode.FutureTimeout()

    monkeypatch.setattr(explore_mode.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(explore_mode, "Future", lambda: SimpleNamespace(result=wait_new))
    monkeypatch.setattr(explore_mode.threading, "Thread", lambda **k: SimpleNamespace(start=lambda: None))
    service = FakeService()
    service._explore_graph_builds = {"other": SimpleNamespace(done=lambda: False, result=wait_other)}
    with pytest.raises(ExploreRefused, match="topology_graph_building"):
        explore_mode.load_view_graph(service, {"x": 250.0, "y": 250.0, "z": 0.0}, wait_seconds=20.0)
    assert timeouts == [20.0, 1.0]


def test_known_doors_appear_as_points_of_interest(monkeypatch, world, tmp_path):
    from ff7r_agent import door_links

    class Links:
        def for_topology(self, fingerprint):
            assert fingerprint == "abc"
            return [{"islands": [1, 2], "actor": "Door1Knob", "bounds": {"origin": {"x": 600.0, "y": 1600.0, "z": 0.0}}}]

    monkeypatch.setattr(door_links, "default_door_links", lambda: Links())
    monkeypatch.setattr(explore_mode, "load_view_graph", lambda *a, **k: (corridor_world(), {"selected": "abc"}))
    result = explore_mode.explore_view(FakeService(), radius_m=40.0, output_dir=str(tmp_path))
    door = next(c for c in result["candidates"] if c["kind"] == "door")
    assert door["label"] == "Door1Knob" and door["reachable"]


# ----------------------------------------------------------- target resolution

def test_targets_resolve_from_candidates_points_metres_and_pixels(world, tmp_path):
    service = FakeService()
    view = explore_mode.explore_view(service, radius_m=40.0, output_dir=str(tmp_path))
    candidate = view["candidates"][0]
    by_id = explore_mode.resolve_goto_target(service, {"candidate": candidate["id"]}, 0.0)
    assert by_id["form"] == "candidate" and by_id["view_id"] == view["view_id"]
    assert (by_id["x"], by_id["y"], by_id["z"]) == (candidate["position"]["x"], candidate["position"]["y"], candidate["position"]["z"])
    assert explore_mode.resolve_goto_target(service, {"x": 100.0, "y": 200.0}, 0.0) == {"form": "xy", "x": 100.0, "y": 200.0, "z": None}
    metres = explore_mode.resolve_goto_target(service, {"x_m": 1.5, "y_m": -2.0, "z_m": 3.0}, 0.0)
    assert (metres["x"], metres["y"], metres["z"]) == (150.0, -200.0, 300.0)
    # pixel of the last view: the image centre is the player
    pixel_target = explore_mode.resolve_goto_target(service, {"px": 320, "py": 320}, 0.0)
    assert (pixel_target["x"], pixel_target["y"], pixel_target["z"]) == pytest.approx((250.0, 250.0, None))
    explicit = explore_mode.resolve_goto_target(
        FakeService(), {"px": 10, "py": 20, "transform": {"left_cm": 1000.0, "top_cm": 2000.0, "px_per_m": 4.0}}, 0.0)
    assert (explicit["x"], explicit["y"]) == pytest.approx((1250.0, 2500.0))


@pytest.mark.parametrize("values", [{}, {"x": 1.0}, {"candidate": 1, "x": 1.0, "y": 2.0}, {"x": 1.0, "y": 2.0, "px": 1, "py": 1},
                                    {"x": float("nan"), "y": 1.0}, {"x": True, "y": 1.0}, {"px": 3}, {"candidate": "1"},
                                    {"x_m": 1.0}])
def test_a_target_needs_exactly_one_complete_form(values):
    with pytest.raises(ValueError):
        explore_mode.resolve_goto_target(FakeService(), values, 0.0)


def test_unknown_views_and_candidates_are_answers_not_crashes(world, tmp_path):
    service = FakeService()
    with pytest.raises(ExploreRefused) as unknown:
        explore_mode.resolve_goto_target(service, {"candidate": 1}, 0.0)
    assert unknown.value.reason == "explore_view_unknown"
    explore_mode.explore_view(service, output_dir=str(tmp_path))
    with pytest.raises(ExploreRefused) as missing:
        explore_mode.resolve_goto_target(service, {"candidate": 999}, 0.0)
    assert missing.value.reason == "candidate_unknown" and missing.value.details["candidates"]
    with pytest.raises(ExploreRefused):
        explore_mode.resolve_goto_target(service, {"px": 1, "py": 1, "view_id": "view-other"}, 0.0)


@pytest.mark.parametrize("form", ["candidate", "pixel"])
def test_remembered_targets_from_a_different_map_are_refused_before_routing(walk, tmp_path, form):
    route = walk(ARRIVED)
    service = FakeService()
    view = explore_mode.explore_view(service, radius_m=40.0, output_dir=str(tmp_path))
    service.states = [make_state(map_id="OtherMap")]
    target = {"candidate": view["candidates"][0]["id"]} if form == "candidate" else {"px": 320, "py": 320}
    result = explore_mode.run_explore_goto(service, object(), dict(target, view_id=view["view_id"], execute=True))
    assert result["reason"] == "explore_view_map_changed" and result["input_sent"] is False
    assert route.calls == []


def test_only_the_latest_views_are_kept(world, tmp_path):
    service = FakeService()
    ids = [explore_mode.explore_view(service, output_dir=str(tmp_path))["view_id"] for _ in range(explore_mode.MAX_STORED_VIEWS + 2)]
    assert list(service._explore_views) == ids[-explore_mode.MAX_STORED_VIEWS:]


# ------------------------------------------------------------------ the walk job

class Route:
    """Stands in for the route adapter: records the request and returns a canned result."""

    def __init__(self, run, preflight=None):
        self.calls = []
        self.run = run
        self.preflight = preflight if preflight is not None else {"movement_authorized": True}

    def __call__(self, service, lifecycle, values):
        self.calls.append(values)
        return {"preflight": self.preflight, "run": self.run, "navigation_objective": {
            "state": "NAVIGATION_OBJECTIVE_COMPLETED", "recovery_attempts": 0, "route_attempt_count": 1},
            "route_attempts": [{"run": self.run, "preflight": self.preflight}]}


@pytest.fixture
def walk(monkeypatch, world, no_session_changes):
    monkeypatch.setattr(explore_mode, "_lease_active", lambda service: True)

    def install(run, preflight=None):
        route = Route(run, preflight)
        monkeypatch.setattr(explore_mode, "_run_route", route)
        return route

    return install


ARRIVED = {"state": "ARRIVED", "reason": None, "movement_cycles": 14, "timeline": []}


def at(x, y, **extra):
    return make_state(x=x, y=y, **extra)


def test_a_walk_snaps_the_target_and_runs_the_pursuit_route_without_touching_the_session(walk):
    route = walk(ARRIVED)
    # 3 m outside the corridor's -x wall at y = 750: the target snaps onto the floor; the player ends there
    service = FakeService(make_state(), at(50.0, 750.0))
    report = explore_mode.run_explore_goto(service, object(), {"x": -300.0, "y": 750.0, "execute": True})
    json.dumps(report, allow_nan=False)
    assert report["ok"] is True and report["outcome"] == "arrived" and report["execute"] is True
    assert report["target"]["snap_distance_m"] == pytest.approx(3.5, abs=0.1)
    assert report["target"]["position"]["z"] == 0.0
    assert report["input_sent"] is True and report["metrics"] is not None
    assert report["control_session"] == "untouched" and report["stop_latched"] is False
    [values] = route.calls
    assert values["dry_run"] is False and values["route_mode"] == "cached_topology"
    assert values["pursuit"] is True and values["continuous_fast_follow"] is True and values["controller_mode"] == "continuous"
    assert values["controller_policy_profile"] == "continuous_fast_follow_trial" and values["sprint"] is False
    assert values["route_target_position"] == {"x": report["target"]["position"]["x"],
                                               "y": report["target"]["position"]["y"], "z": 0.0}
    assert values["normal_stick_magnitude"] == 1.0 and values["max_cycles"] == 2400
    assert report["final"]["distance_to_target_m"] <= 1.5
    assert "timeline" not in report
    # the snapped target is the one the route was given, and the route policy passes the proven-policy check
    from ff7r_agent.controller_policy import require_proven_live_controller_policy
    require_proven_live_controller_policy(values)


def test_the_route_values_are_exactly_what_the_pursuit_mission_builds():
    values = explore_mode.pursuit_route_values(Vec3(1.0, 2.0, 3.0), execute=False, timeout_seconds=900.0, max_cycles=77,
                                                objective_id="x")
    assert values["dry_run"] is True and values["max_cycles"] == 1
    assert values["cached_topology_wall_clock_seconds"] == 600.0
    assert values["route_target_position"] == {"x": 1.0, "y": 2.0, "z": 3.0}


def test_without_execute_a_walk_only_validates_and_sends_no_movement(walk):
    route = walk({"state": "STOPPED", "reason": "control_cycle_bound_reached", "movement_cycles": 0, "timeline": []})
    report = explore_mode.run_explore_goto(FakeService(), object(), {"x": 1250.0, "y": 750.0})
    assert report["outcome"] == "dry_run_ok" and report["ok"] is True and report["execute"] is False
    assert report["input_sent"] is False
    assert route.calls[0]["dry_run"] is True and route.calls[0]["max_cycles"] == 1
    assert report["plan"]["status"] == "complete" and report["plan"]["path_m"] >= report["plan"]["straight_line_m"] - 0.5


def test_a_target_on_an_unreachable_island_is_refused_before_any_route(walk):
    route = walk(ARRIVED)
    report = explore_mode.run_explore_goto(FakeService(), object(), {"x": 750.0, "y": 3750.0, "execute": True})
    assert report["ok"] is False and report["outcome"] == "refused" and report["reason"] == "target_on_unreachable_island"
    assert report["nearest_reachable"]["distance_m"] > 0 and report["target_island"]["polygons"] == 9
    assert report["input_sent"] is False and route.calls == []


def test_a_target_far_off_the_mesh_is_refused_with_the_nearest_reachable_point(walk):
    route = walk(ARRIVED)
    report = explore_mode.run_explore_goto(FakeService(), object(), {"x": -9000.0, "y": 750.0, "execute": True})
    assert report["reason"] == "target_off_navmesh" and report["nearest_reachable"]["position"]["x"] == pytest.approx(50.0, abs=1.0)
    assert route.calls == []
    walk({"state": "STOPPED", "reason": "control_cycle_bound_reached", "movement_cycles": 0, "timeline": []})
    wider = explore_mode.run_explore_goto(FakeService(), object(), {"x": -2000.0, "y": 750.0, "max_snap_m": 25.0})
    assert wider["outcome"] == "dry_run_ok" and wider["target"]["snap_distance_m"] == pytest.approx(20.5, abs=0.2)


def test_allow_off_island_lets_the_planner_decide(walk):
    route = walk({"state": "STOPPED", "reason": "control_cycle_bound_reached", "movement_cycles": 0, "timeline": []})
    report = explore_mode.run_explore_goto(FakeService(), object(), {"x": 750.0, "y": 3750.0, "allow_off_island": True})
    assert report["outcome"] == "dry_run_ok" and report["plan"]["status"] != "complete"
    assert any("allow_off_island" in warning for warning in report["target"]["warnings"])
    assert len(route.calls) == 1


def test_walking_needs_an_open_control_session_and_never_opens_one(monkeypatch, world, no_session_changes):
    monkeypatch.setattr(explore_mode, "_lease_active", lambda service: False)
    monkeypatch.setattr(explore_mode, "_run_route", lambda *a: pytest.fail("no route without a session"))
    report = explore_mode.run_explore_goto(FakeService(), object(), {"x": 1250.0, "y": 750.0, "execute": True})
    assert report["reason"] == "control_session_required" and report["outcome"] == "refused" and report["input_sent"] is False
    assert report["control_session"] == "untouched"


def test_the_lease_check_reads_the_live_session_status(monkeypatch):
    from ff7r_agent import safety

    class Lease:
        def __init__(self, **kwargs):
            pass

        def status(self):
            return {"active": True, "client_id": "someone"}

    monkeypatch.setattr(safety, "SessionLease", Lease)
    assert explore_mode._lease_active(FakeService()) is True
    monkeypatch.setattr(Lease, "status", lambda self: None)
    assert explore_mode._lease_active(FakeService()) is False


def test_a_start_off_the_navmesh_is_refused(walk):
    route = walk(ARRIVED)
    report = explore_mode.run_explore_goto(FakeService(make_state(x=-9000.0, y=250.0)), object(), {"x": 1250.0, "y": 750.0, "execute": True})
    assert report["reason"] == "start_not_on_navmesh" and route.calls == []


class SteppingService(FakeService):
    """Records stick taps; each tap plays back the next state."""

    def __init__(self, *states):
        super().__init__(*states)
        self.moves = []

    def move(self, x, y, milliseconds=250, swimming=False):
        self.moves.append((x, y, milliseconds))
        return {"ok": True}


def just_off(x=-7.0, **extra):
    # live 2026-10-13 at the Dustbowl fast-travel point: 7 cm past an eroded polygon edge
    return make_state(x=x, y=750.0, camera={"rotation": {"yaw": 0.0}}, **extra)


@pytest.fixture
def no_settle(monkeypatch):
    monkeypatch.setattr(explore_mode, "START_STEP_SETTLE_SECONDS", 0.0)


def test_a_start_just_off_the_floor_takes_a_short_step_onto_it_then_walks(walk, no_settle):
    route = walk(ARRIVED)
    service = SteppingService(just_off(), just_off(x=40.0), at(1250.0, 750.0))
    report = explore_mode.run_explore_goto(service, object(), {"x": 1250.0, "y": 750.0, "execute": True})
    assert report["outcome"] == "arrived" and report["input_sent"] is True
    # one tap straight up the stick (camera yaw 0 looks along +x, the floor's side)
    assert service.moves == [(0.0, 0.5, 250)]
    step = report["start_step"]
    assert step["taps"] == 1 and step["on_navmesh"] is True and step["aim"]["x"] == pytest.approx(40.0, abs=0.5)
    assert len(route.calls) == 1 and report["control_session"] == "untouched"


def test_the_step_is_bounded_and_a_start_still_off_the_floor_is_refused(walk, no_settle):
    route = walk(ARRIVED)
    service = SteppingService(just_off())
    report = explore_mode.run_explore_goto(service, object(), {"x": 1250.0, "y": 750.0, "execute": True})
    assert report["reason"] == "start_not_on_navmesh" and report["outcome"] == "stopped"
    assert report["input_sent"] is True and len(service.moves) == explore_mode.START_STEP_TAPS
    assert report["start_step"]["on_navmesh"] is False and report["nearest_polygon_m"] == pytest.approx(0.07, abs=0.01)
    assert route.calls == []


def test_the_step_stops_when_a_conversation_opens(walk, no_settle):
    route = walk(ARRIVED)
    service = SteppingService(just_off(), just_off(interaction={"state": "interacting"}))
    report = explore_mode.run_explore_goto(service, object(), {"x": 1250.0, "y": 750.0, "execute": True})
    assert report["reason"] == "dialogue_active" and len(service.moves) == 1 and route.calls == []


def test_without_execute_or_far_off_the_floor_nothing_steps(walk, no_settle):
    route = walk(ARRIVED)
    service = SteppingService(just_off())
    dry = explore_mode.run_explore_goto(service, object(), {"x": 1250.0, "y": 750.0})
    assert dry["reason"] == "start_step_needed" and dry["outcome"] == "refused" and dry["input_sent"] is False
    far = SteppingService(make_state(x=-200.0, y=750.0, camera={"rotation": {"yaw": 0.0}}))
    refused = explore_mode.run_explore_goto(far, object(), {"x": 1250.0, "y": 750.0, "execute": True})
    assert refused["reason"] == "start_not_on_navmesh" and refused["input_sent"] is False
    assert service.moves == far.moves == [] and route.calls == []


@pytest.mark.parametrize(("state", "reason"), [
    (make_state(interaction={"state": "interacting"}), "dialogue_active"),
    ({"map_id": None, "player": None}, "field_telemetry_unavailable"),
])
def test_a_walk_will_not_start_in_a_conversation_or_on_a_blank_feed(walk, state, reason):
    route = walk(ARRIVED)
    report = explore_mode.run_explore_goto(FakeService(state), object(), {"x": 1250.0, "y": 750.0, "execute": True})
    assert report["outcome"] == "refused" and report["reason"] == reason and route.calls == []


STOPPED = {"state": "STOPPED", "reason": "readiness_recovery_timeout", "movement_cycles": 9, "timeline": [
    {"event": "control_cycle"}, {"event": "readiness_loss_triggered", "failures": ["combat_active"]},
    {"event": "readiness_recovery_failed", "reason": "readiness_recovery_timeout"}]}


@pytest.mark.parametrize(("final", "cutscene", "stop"), [
    (at(1500.0, 250.0, in_combat=True), False, "combat_active"),
    (at(1500.0, 250.0), True, "cutscene_active"),
    (at(1500.0, 250.0, interaction={"state": "dialogue"}), False, "dialogue_active"),
    ({"map_id": None, "player": None}, False, "field_telemetry_unavailable"),
    (at(1500.0, 250.0, loading=True), False, "loading_or_streaming"),
    (at(1500.0, 250.0, in_menu=True), False, "menu_or_pause"),
    (at(1500.0, 250.0), False, "combat_active"),  # nothing visible now: the follower's own readiness failure names it
])
def test_a_walk_that_stops_names_why_with_the_followers_reasons(walk, final, cutscene, stop):
    walk(STOPPED)
    service = FakeService(make_state(), final)
    service.cutscene = {"active": cutscene}
    report = explore_mode.run_explore_goto(service, object(), {"x": 3750.0, "y": 750.0, "execute": True})
    assert report["ok"] is False and report["outcome"] == "stopped" and report["stop_reason"] == stop
    assert report["reason"] == "readiness_recovery_timeout"
    assert report["stop_evidence"] == {"route_reason": "readiness_recovery_timeout", "readiness_failures": ["combat_active"]}
    assert report["control_session"] == "untouched" and report["stop_latched"] is False
    assert report["input_sent"] is True
    assert report["last_events"][-1]["event"] == "readiness_recovery_failed"


def test_a_preflight_refusal_names_the_blocker(walk):
    route = walk(None, {"movement_authorized": False, "reason": "combat_active", "failures": ["combat_active"]})
    service = FakeService(make_state(), at(250.0, 250.0, in_combat=True))
    report = explore_mode.run_explore_goto(service, object(), {"x": 1250.0, "y": 750.0, "execute": True})
    assert report["outcome"] == "refused" and report["reason"] == "combat_active" and report["stop_reason"] == "combat_active"
    assert report["input_sent"] is False and len(route.calls) == 1


def test_arrival_is_claimed_only_when_a_fresh_sample_confirms_it(walk):
    walk(ARRIVED)
    # the route says arrived but the player is 12 m from the target
    service = FakeService(make_state(), at(1250.0, 250.0))
    report = explore_mode.run_explore_goto(service, object(), {"x": 2450.0, "y": 250.0, "execute": True})
    assert report["ok"] is False and report["outcome"] == "stopped" and report["stop_reason"] == "arrival_not_confirmed"
    assert report["final"]["distance_to_target_m"] > explore_mode.ARRIVAL_CONFIRM_M


@pytest.mark.parametrize("final", [at(1250.0, 750.0, z=500.0), at(1250.0, 750.0, map_id="OtherMap"),
                                   at(1250.0, 750.0, telemetry={"live": False}),
                                   at(1250.0, 750.0, running=False)])
def test_arrival_cannot_be_confirmed_on_another_floor_map_or_stale_feed(walk, final):
    walk(ARRIVED)
    result = explore_mode.run_explore_goto(FakeService(make_state(), final), object(),
                                           {"x": 1250.0, "y": 750.0, "execute": True})
    assert result["ok"] is False and result["stop_reason"] == "arrival_not_confirmed"


def test_dry_run_does_not_claim_validation_after_the_route_stops(walk):
    walk(dict(STOPPED, movement_cycles=0))
    result = explore_mode.run_explore_goto(FakeService(), object(), {"x": 1250.0, "y": 750.0})
    assert result["ok"] is False and result["outcome"] == "stopped"
    assert result["reason"] == "readiness_recovery_timeout" and result["input_sent"] is False


@pytest.mark.parametrize("recovery_arrived", [True, False])
def test_compact_reports_keep_earlier_gate_events_and_movement_after_recovery(monkeypatch, walk, recovery_arrived):
    from types import SimpleNamespace

    walk(ARRIVED)
    # Exercise the real objective wrapper: final_run is also present in the
    # attempts and the combined timeline, and must never be counted twice.
    gate = {"event": "dynamic_gate_wait_started", "reason": "door_closed", "padding": "x" * 10000}
    first = {"preflight": {"movement_authorized": True, "timeline": [gate]},
             "run": {"state": "STOPPED", "reason": "multi_tile_corridor_departure", "movement_cycles": 9,
                     "timeline": [gate] + [{"event": "control_cycle"}] * 5000},
             "adapter": SimpleNamespace()}
    final = ({"preflight": {"movement_authorized": True}, "run": dict(ARRIVED, movement_cycles=0),
              "adapter": SimpleNamespace()} if recovery_arrived else
             {"preflight": {"movement_authorized": False, "reason": "source_rejected",
                            "timeline": [{"event": "preflight_refused"}]}, "run": None,
              "adapter": SimpleNamespace()})
    sequence = iter([first, final])
    objective = runtime_commands._run_navigation_objective(lambda: next(sequence), dry_run=False,
                                                           objective_id="explore:test", target_position=None)
    raw = {"run": objective["final_run"], "preflight": objective["final_preflight"],
           "route_attempts": objective["route_attempts"], "navigation_objective": {
               "state": objective["objective_state"], "route_attempt_count": len(objective["route_attempts"]),
               "recovery_attempts": objective["recovery_attempts"], "timeline": objective["timeline"]}}
    monkeypatch.setattr(explore_mode, "_run_route", lambda *a: raw)
    result = explore_mode.run_explore_goto(FakeService(make_state(), at(1250.0, 750.0)), object(),
                                           {"x": 1250.0, "y": 750.0, "execute": True})
    assert result["outcome"] == ("arrived" if recovery_arrived else "stopped")
    assert result["input_sent"] is True and result["movement_cycles"] == result["metrics"]["movement_cycles"] == 9
    assert result["timeline_event_counts"]["dynamic_gate_wait_started"] == 1
    assert result["timeline_event_counts"]["control_cycle"] == 5000
    assert result["last_gate_events"] == [{"event": gate["event"], "reason": "door_closed", "attempt_index": 1}]
    assert result["timeline_scope"] == "all_route_attempts" and result["route_attempt_count"] == 2
    assert result["timeline_event_kinds_omitted"] == 0 and "route_attempts" not in result
    assert len(json.dumps(result)) < 5000 and len(result["last_events"]) <= 8


def test_compact_event_count_caps_preserve_gate_evidence_and_report_omissions():
    timeline = [{"event": f"cycle_{i}"} for i in range(50)] + [{"event": "route_waiting_for_gate"}] * 3
    result = explore_mode._compact_route({"run": {"timeline": timeline}})
    assert len(result["timeline_event_counts"]) == 40
    assert result["timeline_event_counts"]["route_waiting_for_gate"] == 3
    assert result["timeline_event_count"] == 53
    assert result["timeline_event_kinds_omitted"] == result["timeline_named_events_omitted"] == 11


def test_the_route_timeline_is_never_returned(walk):
    run = dict(STOPPED, timeline=[{"event": "control_cycle", "padding": "x" * 100}] * 5000
               + [{"event": "readiness_recovery_failed", "reason": "readiness_recovery_timeout"}])
    walk(run)
    report = explore_mode.run_explore_goto(FakeService(make_state(), at(1500.0, 250.0)), object(),
                                           {"x": 3750.0, "y": 750.0, "execute": True})
    assert "timeline" not in report and report["timeline_event_counts"]["control_cycle"] == 5000
    assert len(report["last_events"]) == 8


@pytest.mark.parametrize("values", [{"x": 1250.0, "y": 750.0, "execute": "yes"}, {"x": 1250.0, "y": 750.0, "max_snap_m": 99.0},
                                    {"x": 1250.0, "y": 750.0, "timeout_seconds": 5.0},
                                    {"x": 1250.0, "y": 750.0, "allow_off_island": 1},
                                    {"x": 540.0, "y": -200.0, "units": "m"}])
def test_bad_options_are_rejected_not_defaulted(walk, values):
    with pytest.raises(ValueError):
        explore_mode.run_explore_goto(FakeService(), object(), values)


def test_classify_stop_prefers_dialogue_then_cutscene_then_the_followers_failure():
    classify = explore_mode.classify_stop
    assert classify(None, None, "x", [])[0] == "field_telemetry_unavailable"
    both = make_state(in_combat=True, interaction={"state": "menu"})
    assert classify(both, {"active": True}, "x", [])[0] == "dialogue_active"
    assert classify(make_state(in_combat=True), {"active": True}, "x", [])[0] == "cutscene_active"
    assert classify(make_state(), None, "route_diverged", [])[0] == "route_diverged"
    assert classify(make_state(), None, None, [])[0] == "route_stopped"


# ------------------------------------------------------- parity with the mission

def test_route_values_match_what_navigation_mission_pursuit_sends_for_a_coordinate_goal(tmp_path):
    """explore_goto must not drift from the pursuit route the mission has proven live."""
    import asyncio

    from ff7r_agent.navigation_mission import MissionConfig, NavigationMission
    from test_navigation_mission import Runtime

    runtime = Runtime()
    mission = NavigationMission(
        runtime.call, tmp_path / "run", identity_check=lambda *args: {"ok": True},
        config=MissionConfig(execute=True, timeout_seconds=900.0, capture_evidence=False, controller_mode="continuous",
                             fast_follow=True, pursuit=True, target_position=(1000.0, 0.0, 65.0), target_map_id="EndGame",
                             # As npc_approach's pursuit routes: the camera follows smoothly.
                             camera_follow_enabled=True, camera_follow_smooth=True),
        clock=lambda: runtime.time, sleep=runtime.sleep, progress=lambda _: None)
    asyncio.run(mission.run())
    [sent] = [args for op, args in runtime.calls if op == "start_live_route_adapter"]
    mine = explore_mode.pursuit_route_values(Vec3(1000.0, 0.0, 65.0), execute=True, timeout_seconds=900.0,
                                              max_cycles=2400, objective_id="explore:test")
    assert mine.pop("navigation_objective_id") == "explore:test"
    assert mine == sent


def test_the_route_values_build_a_valid_live_adapter_configuration(monkeypatch, tmp_path):
    """The real LiveAdapterConfig and controller-policy checks accept the pursuit request."""
    from ff7r_agent import live_route_adapter

    captured = {}

    class CapturingAdapter:
        def __init__(self, *args, **kwargs):
            captured["config"] = kwargs["config"]
            captured["target"] = kwargs["route_target"]

        def preflight(self):
            return {"movement_authorized": False, "reason": "test_refusal"}

    class FakeInput:
        is_real = False

    class FakeAdapterService:
        input = FakeInput()

    for name in ("AgentServiceTelemetrySource", "AgentServiceRenderEvidenceSource", "IndependentJsonValidator",
                 "ExistingAnalogueActuator", "ExistingHorizontalCameraActuator", "JsonTopologyProvider",
                 "load_route_follower_factory"):
        monkeypatch.setattr(live_route_adapter, name, lambda *a, **k: object())
    monkeypatch.setattr(live_route_adapter, "LiveRouteAdapter", CapturingAdapter)
    values = explore_mode.pursuit_route_values(Vec3(100.0, 200.0, 0.0), execute=False, timeout_seconds=300.0,
                                                max_cycles=2400, objective_id="explore:x")
    values.update(topology_path=str(tmp_path / "geometry.json"), validator_snapshot=str(tmp_path / "geometry.json"))
    result = runtime_commands._run_live_route_adapter(FakeAdapterService(), object(), values)
    config = captured["config"]
    assert config.pursuit and config.continuous_fast_follow and config.controller_mode == "continuous"
    assert config.normal_stick_magnitude == 1.0 and config.route_mode == "cached_topology"
    assert (captured["target"].x, captured["target"].y) == (100.0, 200.0)
    assert result["navigation_objective"]["id"] == "explore:x"


# ------------------------------------------------------- crossing a gimmick

class GimmickService(FakeService):
    def __init__(self, evidence_path, *states):
        super().__init__(*states)
        self.evidence_path = evidence_path

    def traversal_gimmick_evidence_path(self):
        return self.evidence_path


def test_a_gimmick_is_a_target_form_of_its_own(walk, tmp_path):
    with pytest.raises(ValueError, match="gimmick"):
        explore_mode.run_explore_goto(GimmickService(tmp_path / "e.json"), object(), {"gimmick": "Crack", "x": 1.0, "y": 2.0})


def test_an_unscanned_gimmick_is_refused_before_any_route(walk, tmp_path):
    route = walk(ARRIVED)
    report = explore_mode.run_explore_goto(GimmickService(tmp_path / "e.json"), object(),
                                           {"gimmick": "obl_Crack", "execute": True})
    assert report["outcome"] == "refused" and report["reason"] == "gimmick_unknown" and route.calls == []


def test_a_gimmick_walk_goes_to_its_nearer_end(walk, tmp_path):
    from ff7r_agent import gimmick_links as gl

    path = tmp_path / "evidence.json"
    gl.record_scan(path, [{"id": "SampleCrack00_C /T:PersistentLevel.obl_Crack", "kind": "crack",
                           "enter": {"x": 3000.0, "y": 750.0, "z": 0.0}, "exit": {"x": 1250.0, "y": 750.0, "z": 0.0}}])
    route = walk({"state": "STOPPED", "reason": "control_cycle_bound_reached", "movement_cycles": 0, "timeline": []})
    report = explore_mode.run_explore_goto(GimmickService(path), object(), {"gimmick": "obl_Crack"})
    assert report["requested"]["form"] == "gimmick" and report["requested"]["direction"] == gl.DIRECTION_REVERSE
    # 1.2 m short of the trigger, on the near side.
    assert (report["requested"]["x"], report["requested"]["y"]) == (1250.0 - 120.0, 750.0)
    assert report["outcome"] == "dry_run_ok" and "gimmick_probe" not in report
    assert route.calls and route.calls[0]["dry_run"] is True
