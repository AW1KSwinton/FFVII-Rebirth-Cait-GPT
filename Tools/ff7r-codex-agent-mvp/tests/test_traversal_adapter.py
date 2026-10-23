"""How the live adapter uses a plan's off-mesh crossings."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from ff7r_agent import offmesh_links as om
from ff7r_agent.detour_route import DetourTopologyGraph
from ff7r_agent.live_route_adapter import AdapterError, AdapterState, LiveAdapterConfig, LiveRouteAdapter, load_route_follower_factory
from ff7r_agent.models import Vec3
from ff7r_agent.static_obstacles import StaticObstacleOverlay, StaticObstacleScope

from offmesh_fixtures import GIRDER_GOAL, GIRDER_START, connection, girder_payload, pads_payload, ref
from test_continuous_control import StreamingActuator
from test_live_route_adapter import FakeClock, FakeTelemetry, make_adapter, make_observation

CACHED = LiveAdapterConfig(route_mode="cached_topology")


def girder_plan() -> dict:
    graph = DetourTopologyGraph.from_dict(girder_payload())
    plan = graph.plan(Vec3(*GIRDER_START), Vec3(*GIRDER_GOAL))
    assert plan.status == "complete"
    return plan.to_dict()


def bare_adapter(config=CACHED):
    adapter, _, _, _ = make_adapter(config=config)
    return adapter


# -- locating the crossings of a plan -------------------------------------------


def test_the_adapter_finds_a_plans_crossing_between_its_two_waypoints():
    plan = girder_plan()
    (entry,) = bare_adapter()._locate_route_traversals(plan)
    segment = plan["traversal_segments"][0]
    assert entry["start_index"] == segment["start_waypoint_index"] and entry["end_index"] == entry["start_index"] + 1
    assert (entry["from_ref"], entry["to_ref"]) == (segment["from_ref"], segment["to_ref"])
    assert entry["corridor_index"] == plan["polygon_corridor"].index(entry["from_ref"])
    assert entry["state"] == "pending"


def test_a_plan_without_crossings_has_none():
    plan = DetourTopologyGraph.from_dict(girder_payload()).plan(Vec3(4355.0, 66760.0, -93.0), Vec3(4300.0, 66700.0, -90.0))
    assert plan.status == "complete" and bare_adapter()._locate_route_traversals(plan.to_dict()) == []


def test_a_crossing_an_obstacle_detour_has_moved_stops_the_route_before_movement():
    plan = girder_plan()
    plan["waypoints"].insert(plan["traversal_segments"][0]["end_waypoint_index"], {"x": 4400.0, "y": 67000.0, "z": -100.0})
    with pytest.raises(AdapterError, match="traversal_waypoints_not_found"):
        bare_adapter()._locate_route_traversals(plan)


def test_a_crossing_the_plan_does_not_declare_is_refused():
    plan = girder_plan()
    plan["traversal_segments"] = []
    with pytest.raises(AdapterError, match="traversal_segments_portal_mismatch"):
        bare_adapter()._locate_route_traversals(plan)


def test_the_adapter_can_be_told_never_to_cross():
    with pytest.raises(AdapterError, match="traversal_links_disabled"):
        bare_adapter(replace(CACHED, traversal_links=False))._locate_route_traversals(girder_plan())


def test_the_preflight_report_says_which_link_classes_are_unverified():
    adapter = bare_adapter()
    adapter._adopt_route_traversals(girder_plan())
    summary = adapter._traversal_summary()
    assert summary["count"] == 1
    assert summary["unverified_classes"] == ["step_over"]
    assert summary["route_depends_on_unverified_traversal"] is True
    event = next(item for item in adapter.timeline if item["event"] == "route_uses_traversal_links")
    assert event["unverified_classes"] == ["step_over"]
    assert event["links"][0]["link_class"] == "step_over" and event["links"][0]["live_verified"] is False


# -- the policy the planner is called with ---------------------------------------


def policy_seen_by_planner(config):
    seen = {}

    def planner(graph, start, end, overlay, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(status="complete")

    adapter = SimpleNamespace(
        config=config, monotonic=lambda: 1.0, timeline=[], _static_overlay_plan_memo=None,
        static_obstacles=StaticObstacleOverlay(),
    )
    adapter.static_obstacles.bind_scope(StaticObstacleScope("session", "EndGame", "EndGame", "F" * 64))
    adapter._static_overlay_plan_key = lambda *args: LiveRouteAdapter._static_overlay_plan_key(adapter, *args)
    from ff7r_agent import live_route_adapter

    original = live_route_adapter.plan_with_static_obstacles
    live_route_adapter.plan_with_static_obstacles = planner
    try:
        LiveRouteAdapter._plan_with_static_overlay(
            adapter, SimpleNamespace(graph=None, session_id="s", fingerprint="f"),
            Vec3(0.0, 0.0, 0.0), Vec3(100.0, 0.0, 0.0),
        )
    finally:
        live_route_adapter.plan_with_static_obstacles = original
    return seen


def test_only_cached_topology_routes_may_use_links():
    assert "traversal_policy" not in policy_seen_by_planner(CACHED)
    assert policy_seen_by_planner(LiveAdapterConfig(route_mode="ordinary_multi_tile"))["traversal_policy"] is om.DISABLED_TRAVERSAL_POLICY
    assert policy_seen_by_planner(replace(CACHED, traversal_links=False))["traversal_policy"] is om.DISABLED_TRAVERSAL_POLICY


# -- accepting a crossing in a cached-topology route -----------------------------


def verified_portal(plan):
    graph = DetourTopologyGraph.from_dict(girder_payload())
    portal = next(item for item in plan["portals"] if item.get("kind") == "traversal")
    topology = SimpleNamespace(graph=graph, scope_identity=None)
    return graph, topology, portal


def test_a_traversal_portal_the_graph_allows_is_accepted():
    plan = girder_plan()
    _, topology, portal = verified_portal(plan)
    bare_adapter()._verify_traversal_portal(topology, portal, portal["from_ref"], portal["to_ref"])


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p.update(link_ref=1), "cached_topology_traversal_link_unproven"),
        (lambda p: p.update(traversal_class="drop"), "cached_topology_traversal_link_unproven"),
        (lambda p: p["left"].update(x=p["left"]["x"] + 40.0), "cached_topology_traversal_link_unproven"),
        (lambda p: p.update(direction="end_to_start"), "cached_topology_traversal_link_unproven"),
    ],
)
def test_a_traversal_portal_that_is_not_the_graphs_link_is_refused(change, message):
    plan = girder_plan()
    graph, topology, portal = verified_portal(plan)
    change(portal)
    with pytest.raises(AdapterError, match=message):
        bare_adapter()._verify_traversal_portal(topology, portal, plan["portals"][1]["from_ref"], plan["portals"][1]["to_ref"])


def test_a_class_the_graph_policy_excludes_is_refused_even_when_planned_with_everything():
    graph = DetourTopologyGraph.from_dict(girder_payload())
    graph.traversal_policy = om.TraversalPolicy(enabled_classes=frozenset({om.CLASS_STEP_OVER}))
    plan = graph.plan(Vec3(4378.0, 65702.0, 152.0), Vec3(4188.0, 66010.0, -5.0), traversal_policy=om.EVERYTHING_POLICY).to_dict()
    portal = next(item for item in plan["portals"] if item.get("kind") == "traversal")
    topology = SimpleNamespace(graph=graph, scope_identity=None)
    with pytest.raises(AdapterError, match="cached_topology_traversal_class_not_permitted"):
        bare_adapter()._verify_traversal_portal(topology, portal, portal["from_ref"], portal["to_ref"])


def test_links_are_refused_inside_a_partial_corridor_scope():
    plan = girder_plan()
    graph, _, portal = verified_portal(plan)
    with pytest.raises(AdapterError, match="outside_partial_scope"):
        bare_adapter()._verify_traversal_portal(SimpleNamespace(graph=graph, scope_identity="scope"), portal, portal["from_ref"], portal["to_ref"])


# -- the collision audit ------------------------------------------------------


def test_the_game_collision_on_a_crossings_own_leg_is_not_an_obstacle():
    from test_route_collision_audit import Adapter, audit, hit, sweep

    girder = {"ledge": True, "rise_cm": 70, "normal_z": 0, "impact": {"x": 500.0, "y": 0.0, "z": 80.0}}
    wall = {"ledge": True, "rise_cm": 90, "normal_z": 0, "impact": {"x": 150.0, "y": 0.0, "z": 80.0}}
    crossing = {"segments": [
        {"index": 0, "complete": True, "hits": [wall]},
        {"index": 1, "complete": True, "hits": [girder]},
    ], "query": {}}
    segments = ({"link_class": "step_over", "start": {"x": 300.0, "y": 0.0, "z": 0.0}, "end": {"x": 700.0, "y": 0.0, "z": 0.0}},)
    waypoints = tuple(Vec3(x, 0.0, 0.0) for x in (0, 300, 700, 1000))
    crossing_plan = SimpleNamespace(waypoints=waypoints, traversal_segments=segments, total_estimated_path_length_m=10.0)
    # The wall on the first leg still counts; the girder on the crossing does not.
    adapter = Adapter([crossing, {"segments": [{"index": 0, "complete": True, "hits": []}], "query": {}}])
    result = audit(adapter, crossing_plan, [SimpleNamespace(waypoints=waypoints, traversal_segments=segments, total_estimated_path_length_m=10.0)])
    assert result is not crossing_plan
    assert len(adapter.static_obstacles.planning_snapshot().obstacles) == 1
    only_girder = Adapter([{"segments": [{"index": 1, "complete": True, "hits": [girder]}], "query": {}}])
    assert audit(only_girder, crossing_plan, []) is crossing_plan
    assert only_girder.timeline[-1]["outcome"] == "clear"


# -- walking a crossing -----------------------------------------------------


class TickClock(FakeClock):
    """A clock that moves a little every time it is read."""

    def __init__(self, step):
        super().__init__(10.0)
        self.step = step

    def __call__(self):
        self.value += self.step
        return self.value


def walk(xs, y=0.0):
    return [make_observation(sequence=i + 1, monotonic_t=10.0 + i * 0.12, position=Vec3(x, y, 0.0),
                             grounded=True,
                             velocity_cm_s=Vec3(0.0 if i == 0 else (x - xs[i - 1]) / 0.12, 0.0, 0.0))
            for i, x in enumerate(xs)]


def crossing_adapter(observations, *, clock=None):
    actuator = StreamingActuator()
    config = LiveAdapterConfig(controller_mode="continuous", continuous_fast_follow=True, pursuit=True,
                               post_arrival_samples=0, max_cycles=4, readiness_recovery_timeout_seconds=0.2,
                               preflight_telemetry_wait_seconds=0.2)
    adapter, telemetry, _, _ = make_adapter(actuator=actuator, config=config, observations=observations,
                                            follower_mode="ARRIVED_ON_FIRST", clock=clock)
    observe = telemetry.observe
    def timed_observe():
        sample = observe()
        if isinstance(adapter.monotonic, FakeClock) and not isinstance(adapter.monotonic, TickClock):
            adapter.monotonic.value = max(adapter.monotonic.value, sample.monotonic_t)
        return replace(sample, actuator_neutral=actuator.neutral)
    telemetry.observe = timed_observe
    assert adapter.preflight()["movement_authorized"]
    return adapter, actuator


def link_on_route(adapter):
    """The route's first leg, as a pending crossing."""
    start, end = adapter.authorization_proof.waypoints[0], adapter.authorization_proof.waypoints[1]
    segment = {
        "link_class": "step_over", "link_ref": 99, "direction": "start_to_end", "live_verified": False,
        "start": {"x": start.x, "y": start.y, "z": start.z}, "end": {"x": end.x, "y": end.y, "z": end.z},
        "horizontal_length_m": 3.6, "vertical_delta_m": 0.0, "order": 0,
    }
    entry = {"order": 0, "segment": segment, "start_index": 0, "end_index": 1, "corridor_index": 0,
             "from_ref": adapter.authorization_proof.start_ref, "to_ref": adapter.authorization_proof.end_ref,
             "start": start, "end": end, "state": "pending"}
    adapter._route_traversals = [entry]
    # This wiring fixture invents link 99 on one flat polygon. Real polygon
    # admission and end-to-end handoff are covered by the girder replay tests.
    adapter._traversal_polygon_proven = lambda sample, reference, **kwargs: True
    return entry


def test_pursuit_hands_a_crossing_its_own_walk_and_goes_on_to_arrive():
    adapter, actuator = crossing_adapter(walk([0] * 6 + [60, 120, 180, 240, 300, 360, 360, 360, 360, 360]))
    entry = link_on_route(adapter)
    result = adapter.run(dry_run=False)
    assert result["state"] == "ARRIVED", result["reason"]
    assert entry["state"] == "crossed"
    events = [item["event"] for item in result["timeline"]]
    assert "traversal_crossing_started" in events and "traversal_crossing_finished" in events
    assert "pursuit_handed_over" not in events
    finished = next(item for item in result["timeline"] if item["event"] == "traversal_crossing_finished")
    assert finished["crossed"] is True
    # The step-over itself is crossed at a sprint, which takes it by itself.
    crossing = [item for item in result["timeline"] if item["event"] == "traversal_command"]
    assert crossing and all(item["sprint"] is True for item in crossing if item["magnitude"] == 1.0)


@pytest.mark.parametrize("link_class,sprint", [("step_over", True), ("drop", True), ("climb", True),
                                               ("ladder", False), ("crack", False), ("long_span", False)])
def test_only_ledges_and_low_walls_are_crossed_at_a_sprint(link_class, sprint):
    from ff7r_agent.live_route_adapter import SPRINT_CROSSING_CLASSES

    assert (link_class in SPRINT_CROSSING_CLASSES) is sprint


def test_a_crossing_that_does_not_cross_stops_the_route_with_its_own_outcome_and_evidence():
    clock = TickClock(0.04)
    adapter, actuator = crossing_adapter(walk([0] * 6 + [80] * 400), clock=clock)
    entry = link_on_route(adapter)
    result = adapter.run(dry_run=False)
    assert result["reason"] == "traversal_link_not_crossed", result["reason"]
    assert result["state"] == AdapterState.STOPPED.value
    assert entry["state"] == "failed"
    assert actuator.neutral
    event = next(item for item in result["timeline"] if item["event"] == "traversal_link_not_crossed")
    evidence = event["evidence"]
    assert evidence["outcome"] == "traversal_link_not_crossed"
    assert evidence["target"]["link_class"] == "step_over" and evidence["target"]["link_ref"] == 99
    assert evidence["position_last"]["x"] == 80.0
    assert evidence["distance_to_end_m"] == pytest.approx((adapter.authorization_proof.waypoints[1].x - 80.0) / 100.0)
    assert evidence["recent_samples"]
    # It did not hand the same crossing to the careful controller to try again.
    assert "pursuit_handed_over" not in [item["event"] for item in result["timeline"]]


def test_careful_crossings_keep_the_stick_held_between_fresh_samples():
    adapter, actuator = crossing_adapter(walk([0, 0, 0, 360]))
    adapter.config = replace(adapter.config, controller_mode="pulse", pursuit=False, continuous_fast_follow=False)
    adapter._traversal_push(0.0, make_observation(sequence=99))
    adapter._traversal_push(0.0, make_observation(sequence=100))
    assert len(actuator.updates) == 2
    assert actuator.commands == []
    assert all(update["sprint"] is False for update in actuator.updates)
    assert not actuator.updates[-1]["neutral_before"]


def test_only_the_crossings_own_hold_can_remain_active_during_careful_readiness():
    adapter = bare_adapter()
    adapter.state = AdapterState.FOLLOWING
    observation = make_observation(actuator_neutral=False)
    adapter._stream_hold_active = True
    assert "actuator_not_neutral" in adapter._check_preconditions(observation)
    assert "actuator_not_neutral" not in adapter._check_preconditions(observation, allow_owned_hold=True)
    adapter._stream_hold_active = False
    assert "actuator_not_neutral" in adapter._check_preconditions(observation, allow_owned_hold=True)
    adapter._stream_hold_active = True
    assert "movement_lease_missing" in adapter._check_preconditions(
        replace(observation, lease_active=False), allow_owned_hold=True)


def test_the_lookahead_is_not_consulted_near_a_link():
    adapter, _ = crossing_adapter(walk([0, 0, 0, 0]))
    link_on_route(adapter)
    assert adapter._traversal_link_near(Vec3(100.0, 0.0, 0.0)) is True
    assert adapter._traversal_link_near(Vec3(5000.0, 0.0, 0.0)) is False
    steering = {"control_band": "normal", "desired_world_heading_deg": 0.0}
    assert adapter._precommand_static_obstacle_gate(make_observation(), steering) in {"not_checked", "traversal_link_ahead"}
    adapter.lookahead = object()
    assert adapter._precommand_static_obstacle_gate(make_observation(), steering) == "traversal_link_ahead"


# -- the landing, for the careful controller ---------------------------------


def test_after_a_crossing_the_corridor_state_resumes_at_the_landing_polygon():
    graph = DetourTopologyGraph.from_dict(girder_payload())
    plan = graph.plan(Vec3(*GIRDER_START), Vec3(*GIRDER_GOAL)).to_dict()
    adapter = bare_adapter()
    adapter.topology_session = SimpleNamespace(graph=graph, scope_identity=None, session_id="run-1", fingerprint="f")
    adapter.route_plan = plan
    adapter.authorization_proof = None
    adapter.follower = load_route_follower_factory()(plan)
    adapter.follower.on_telemetry({"t": 10.0, "position": plan["waypoints"][0], "heading": 0.0})
    adapter._route_traversals = adapter._locate_route_traversals(plan)
    (entry,) = adapter._route_traversals
    landing = entry["end"]
    observation = make_observation(sequence=9, position=Vec3(landing.x, landing.y + 30.0, landing.z), grounded=True)
    assert adapter._commit_traversal_landing(observation, entry) is True
    assert adapter._route_local_corridor_ref == entry["to_ref"]
    assert adapter._route_local_corridor_index == entry["corridor_index"] + 1
    assert adapter._route_local_last_sequence == 9
    committed = [item for item in adapter.timeline if item["event"] == "traversal_landing_committed"]
    assert committed and committed[0]["accepted_ref"] == entry["to_ref"]
    assert adapter.follower.current_waypoint_index == entry["end_index"]


@pytest.mark.parametrize("position", [Vec3(4481.547852, 67103.515625, -111.762306),
                                      Vec3(4455.0, 66930.0, -102.802612)])
def test_a_snap_candidate_on_the_girder_is_not_a_landing_commit(position):
    adapter = bare_adapter()
    graph = DetourTopologyGraph.from_dict(girder_payload())
    adapter.topology_session = SimpleNamespace(graph=graph)
    adapter.route_plan = girder_plan()
    adapter.authorization_proof = None
    (entry,) = adapter._locate_route_traversals(adapter.route_plan)
    assert not adapter._commit_traversal_landing(make_observation(position=position), entry)
    assert adapter.reason == "traversal_landing_polygon_unproven"
    assert not any(e["event"] == "traversal_landing_committed" for e in adapter.timeline)


def test_a_missing_polygon_sample_cannot_commit_a_traversal():
    adapter = bare_adapter()
    (entry,) = adapter._locate_route_traversals(girder_plan())
    adapter.topology_session = None
    assert not adapter._commit_traversal_landing(make_observation(position=entry["end"]), entry)
    assert adapter.reason == "traversal_landing_polygon_unproven"


def test_a_crossing_requires_a_supervised_actuator_in_careful_mode():
    adapter = bare_adapter()
    with pytest.raises(AdapterError, match="supervised_actuator_unavailable"):
        adapter._traversal_push(0.0, make_observation())
    assert adapter.actuator.commands == []


def test_a_landing_off_the_landing_polygon_stops_the_route():
    graph = DetourTopologyGraph.from_dict(girder_payload())
    plan = graph.plan(Vec3(*GIRDER_START), Vec3(*GIRDER_GOAL)).to_dict()
    adapter = bare_adapter()
    adapter.topology_session = SimpleNamespace(graph=graph, scope_identity=None, session_id="run-1", fingerprint="f")
    adapter.route_plan = plan
    adapter.authorization_proof = None
    adapter._route_traversals = adapter._locate_route_traversals(plan)
    (entry,) = adapter._route_traversals
    start = entry["start"]
    observation = make_observation(sequence=9, position=Vec3(start.x, start.y, start.z))
    assert adapter._commit_traversal_landing(observation, entry) is False
    assert adapter.state == AdapterState.STOPPED and adapter.reason == "traversal_landing_polygon_unproven"
    assert any(item["event"] == "traversal_landing_polygon_unproven" for item in adapter.timeline)


def test_the_follower_hook_waits_for_the_departure_point_to_be_reached():
    adapter = bare_adapter()
    adapter._adopt_route_traversals(girder_plan())
    (entry,) = adapter._route_traversals
    start = entry["start"]
    at_start = make_observation(position=Vec3(start.x, start.y, start.z))
    far = make_observation(position=Vec3(start.x + 2000.0, start.y, start.z))
    following = {"waypoint_index": entry["end_index"], "state": "FOLLOWING"}
    assert adapter._pending_traversal_for(following, at_start) is entry
    assert adapter._pending_traversal_for(following, far) is None
    # The follower's lookahead can cross the departure while it still reports
    # that waypoint as its current target. Hand over before that happens.
    assert adapter._pending_traversal_for({"waypoint_index": entry["start_index"], "state": "FOLLOWING"}, at_start) is entry
    assert adapter._pending_traversal_for({"waypoint_index": entry["start_index"], "state": "FOLLOWING"}, far) is None
    assert adapter._pending_traversal_for({**following, "state": "STALLED"}, at_start) is None
    # Live, one follower pulse carried Zack from 2.2 m short of a link into
    # the game's own vault: the hand-over has to come a pulse earlier.
    one_pulse_short = make_observation(position=Vec3(start.x + 250.0, start.y, start.z))
    beyond_handoff = make_observation(position=Vec3(start.x + 350.0, start.y, start.z))
    approaching = {"waypoint_index": entry["start_index"], "state": "FOLLOWING"}
    assert adapter._pending_traversal_for(approaching, one_pulse_short) is entry
    assert adapter._pending_traversal_for(approaching, beyond_handoff) is None
    entry["state"] = "crossed"
    assert adapter._pending_traversal_for(following, at_start) is None


def test_a_failed_crossing_keeps_its_link_out_of_later_plans(monkeypatch):
    from ff7r_agent import live_route_adapter as lra

    monkeypatch.setattr(lra, "FAILED_TRAVERSAL_LINKS", {})
    adapter, actuator = crossing_adapter(walk([0] * 6 + [80] * 400), clock=TickClock(0.04))
    entry = link_on_route(adapter)
    result = adapter.run(dry_run=False)
    assert result["reason"] == "traversal_link_not_crossed" and entry["state"] == "failed"
    assert list(lra.FAILED_TRAVERSAL_LINKS) == [99]
    # The next plan on a graph of its own excludes it.
    captured = {}
    monkeypatch.setattr(lra, "plan_with_static_obstacles",
                        lambda graph, start, end, overlay, **kwargs: captured.update(kwargs) or "plan")
    graph = DetourTopologyGraph.from_dict(girder_payload())
    adapter.config = replace(adapter.config, route_mode="cached_topology")
    adapter._static_overlay_plan_key = lambda *args: None
    topology = SimpleNamespace(graph=graph, session_id="s", fingerprint="f")
    assert adapter._plan_with_static_overlay(topology, Vec3(0.0, 0.0, 0.0), Vec3(1.0, 0.0, 0.0)) == "plan"
    assert 99 in captured["traversal_policy"].excluded_links
    assert captured["traversal_policy"].enabled_classes == graph.traversal_policy.enabled_classes


def test_the_failed_link_record_is_bounded(monkeypatch):
    from ff7r_agent import live_route_adapter as lra

    monkeypatch.setattr(lra, "FAILED_TRAVERSAL_LINKS", {})
    for reference in range(lra.FAILED_TRAVERSAL_LINKS_MAX + 5):
        lra.record_failed_traversal_link(reference)
    assert len(lra.FAILED_TRAVERSAL_LINKS) == lra.FAILED_TRAVERSAL_LINKS_MAX
    assert 0 not in lra.FAILED_TRAVERSAL_LINKS and lra.FAILED_TRAVERSAL_LINKS_MAX + 4 in lra.FAILED_TRAVERSAL_LINKS
