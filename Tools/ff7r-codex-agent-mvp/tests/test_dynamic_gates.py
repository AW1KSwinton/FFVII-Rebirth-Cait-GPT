"""Automatic doors and gates as dynamic obstacles (dynamic_gates.py).

The navmesh fixture is the prologue door neck of 2026-10-13 (Zack, EndGame):
``geom-zack.json`` trimmed to the tiles round unreal (1000..1600, 58300..59100),
with the obstacle the dry run kept in the static overlay. Its walkway is under
1 m wide at the door, and the navmesh is continuous through it. Synthetic: the
door's vertical extent (the sweep gave x/y only through the overlay entry) and
its actor name and class (the live hit's were not recorded).
"""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ff7r_agent import dynamic_gates as gates
from ff7r_agent.detour_route import DetourPolygon, DetourTopologyGraph, PolygonReference
from ff7r_agent.dynamic_gates import (
    GATE_PENALTY_M, GATE_WAIT_SECONDS, Box, DynamicGate, GateRegistry, name_signal, neck_evidence,
    plan_gates, plan_through_gates, placements, probe_gate, wait_for_gate)
from ff7r_agent.live_route_adapter import AdapterError, LiveAdapterConfig, LiveRouteAdapter
from ff7r_agent.models import Vec3
from ff7r_agent.route_sweep import classify_hit, obstacle_from_bounds, unknown_obstacle_hits
from ff7r_agent.static_obstacles import StaticObstacleOverlay, StaticObstacleScope, plan_with_static_obstacles

from test_continuous_control import StreamingActuator
from test_live_route_adapter import make_adapter, make_observation

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "prologue-door-neck.json").read_text(encoding="utf-8"))
DOOR_ACTOR, DOOR_CLASS, DOOR_BOUNDS = FIXTURE["door_actor"], FIXTURE["door_actor_class"], FIXTURE["door_bounds"]
PANEL_ACTOR = "BG0000_00_Panel_Standard_C /Game/Level/Game/EndGame.EndGame:PersistentLevel.BG0000_00_Panel_Standard_C_3"
START, END = Vec3(1350.0, 58400.0, 730.0), Vec3(1300.0, 59100.0, 720.0)
MAP = "EndGame"

GATE_COVERAGE_FAULTS = [
    "no_segments", "missing_first_leg", "missing_second_leg", "duplicate_leg", "reversed_legs",
    "skipped_index", "extra_leg", "missing_index", "null_index", "string_index", "boolean_index",
    "float_index", "segments_not_array", "segment_not_object", "missing_hits", "null_hits",
    "hits_not_array", "hit_not_object", "error_despite_completion",
]


@pytest.fixture(scope="module")
def graph():
    return DetourTopologyGraph.from_dict(FIXTURE["topology"])


def hit(actor=DOOR_ACTOR, bounds=DOOR_BOUNDS, actor_class=DOOR_CLASS, **changes):
    value = {"actor": actor, "actor_class": actor_class, "actor_bounds": bounds, "component": "StaticMeshComponent0 x",
             "normal_z": 0.0, "simulating_physics": False, "mobility": 2, "distance_cm": 300.0,
             "impact": {"x": 1300.0, "y": 58795.0, "z": 800.0}}
    value.update(changes)
    value["kind"] = classify_hit(value)
    return value


def panel(**changes):
    return hit(actor=PANEL_ACTOR, actor_class="BG0000_00_Panel_Standard_C", **changes)


def box_bounds(x, y, ex, ey, ez=145.0, z=870.0):
    return {"origin": {"x": x, "y": y, "z": z}, "extent": {"x": ex, "y": ey, "z": ez}}


def sweep(*hits, complete=True, segments=1):
    return {"query": {"channel": 0}, "segments": [
        {"index": index, "complete": complete, "hits": list(hits) if index == 0 else []} for index in range(segments)]}


def door_plan(graph):
    plan = graph.plan(START, END)
    assert plan.status == "complete"
    return plan


# ------------------------------------------------------------- the fixture


def test_the_fixture_reproduces_the_refusal_the_dry_run_gave(graph):
    # The door's bounds, kept as a static obstacle, left no route through the neck.
    entry = FIXTURE["overlay_entry"]
    obstacle = obstacle_from_bounds(DOOR_ACTOR, DOOR_BOUNDS, sequence=1, t=0.0)
    assert (obstacle.center.x, obstacle.center.y, obstacle.center.z) == pytest.approx(
        (entry["center"]["x"], entry["center"]["y"], entry["center"]["z"]))
    assert obstacle.radius_m == pytest.approx(entry["radius_m"])
    assert obstacle.half_length_m == pytest.approx(entry["half_length_m"])
    assert obstacle.effective_radius_m == pytest.approx(entry["effective_radius_m"])
    overlay = StaticObstacleOverlay()
    overlay.bind_scope(StaticObstacleScope("session", MAP, MAP, "F" * 64))
    assert plan_with_static_obstacles(graph, START, END, overlay).status == "complete"
    overlay.admit_obstacle(scope=overlay.scope, obstacle=obstacle)
    refused = plan_with_static_obstacles(graph, START, END, overlay)
    assert refused.status == "failed" and refused.reason_code == "static_obstacle_global_replan_unavailable"


# ------------------------------------------------------------------ names


@pytest.mark.parametrize("actor, actor_class, expected", [
    (DOOR_ACTOR, None, "name:door"),
    ("BG0007_00_AutoDoor_Standard_C /x:PersistentLevel.BG0007_00_AutoDoor_Standard_C_2", None, "name:door"),
    ("BG0100_00_ShutterGate_C /x:PersistentLevel.BG0100_00_ShutterGate_C_1", None, "name:shutter"),
    ("BG0100_00_Panel_C /x:PersistentLevel.BG0100_00_Panel_C_1", "BG0100_00_HatchCover_C", "name:hatch"),
    ("BG0001_00_Gate_Iron_C /x:PersistentLevel.Gate_Iron_9", None, "name:gate"),
    # Not doors that open by themselves, or not doors at all.
    ("BG0012_00_Door1Knob_Standard_C /x:PersistentLevel.Door1Knob_3", None, None),
    ("BG0012_00_DoorFrame_C /x:PersistentLevel.BG0012_00_DoorFrame_1", None, None),
    ("BG0001_00_Doorway_Arch_C /x:PersistentLevel.BG0001_00_Doorway_Arch_1", None, None),
    ("BG0001_00_Navigate_Sign_C /x:PersistentLevel.BG0001_00_Navigate_Sign_1", None, None),
    # The level path is not read: this tent sits in a ..._Gimmick level next to a Gate folder.
    ("BG1216_00_Tent_CSMOA_C /Game/Level/Game/Field/2500-Gate/Layout/2500-CSMOA_Gimmick.2500-CSMOA_Gimmick:"
     "PersistentLevel.BG1216_00_Tent_CSMOA_01", None, None),
])
def test_name_signal_reads_the_class_and_instance_not_the_level_path(actor, actor_class, expected):
    assert name_signal({"actor": actor, "actor_class": actor_class}) == expected


# --------------------------------------------------------------- the neck


def test_the_door_seals_a_narrow_neck_the_navmesh_passes_through(graph):
    evidence = neck_evidence(graph, door_plan(graph), DOOR_BOUNDS)
    assert evidence["spans_neck"] is True
    assert evidence["neck_width_m"] == pytest.approx(0.9, abs=0.01)  # polygon 8's far portal
    assert evidence["thickness_m"] == pytest.approx(0.32, abs=0.01)
    assert evidence["crossing_cos"] > 0.9
    assert len(evidence["under_polygons"]) == 1  # polygon 8 of tile 684 lies under the leaf


@pytest.mark.parametrize("name, bounds", [
    # A crate: thick, not a leaf.
    ("thick", box_bounds(1296.0, 58810.0, 100.0, 100.0)),
    # A leaf that does not reach across the walkway.
    ("short", box_bounds(1262.0, 58810.0, 18.0, 16.0)),
    # A leaf the route runs along, not across.
    ("alongside", box_bounds(1296.0, 58810.0, 16.0, 100.0)),
    # Too low to be a door.
    ("low", box_bounds(1296.0, 58810.0, 100.0, 16.0, ez=30.0, z=760.0)),
    # Nowhere near the route.
    ("elsewhere", box_bounds(1700.0, 58500.0, 100.0, 16.0)),
])
def test_other_obstacles_do_not_seal_the_neck(graph, name, bounds):
    assert neck_evidence(graph, door_plan(graph), bounds) is None


def test_a_leaf_in_a_wide_walkway_is_not_a_neck(graph):
    # The same leaf where the corridor opens out (polygon 7, 8 m across): the
    # route goes round it; it does not seal anything.
    plan = graph.plan(Vec3(1100.0, 59050.0, 720.0), Vec3(1700.0, 59250.0, 720.0))
    assert plan.status == "complete"
    assert neck_evidence(graph, plan, box_bounds(1400.0, 59150.0, 100.0, 16.0, z=870.0)) is None


# ------------------------------------------------------------ the registry


def test_a_named_door_is_a_gate_and_never_a_wall(graph):
    registry = GateRegistry()
    triage = registry.triage(MAP, [hit()], plan=door_plan(graph), graph=graph)
    assert triage.walls == () and [gate.signals for gate in triage.gates] == [("name:door",)]
    (gate,) = triage.new_gates
    assert gate.evidence["neck"]["neck_width_m"] == pytest.approx(0.9, abs=0.01)
    assert registry.gate_for(MAP, DOOR_ACTOR) is gate or registry.gate_for(MAP, DOOR_ACTOR) == gate
    # Seen again it is the same, now known gate.
    again = registry.triage(MAP, [hit()], plan=door_plan(graph), graph=graph)
    assert again.new_gates == () and [item.gate_id for item in again.gates] == [gate.gate_id]


def test_an_unnamed_prop_is_a_wall_until_something_says_otherwise(graph):
    registry = GateRegistry()
    triage = registry.triage(MAP, [panel()], plan=door_plan(graph), graph=graph)
    assert len(triage.walls) == 1 and triage.gates == ()
    # Level collision, ledges and wide actors have no actor box and are never gates.
    spot = {"actor": "CollisionAssetActor x", "normal_z": 0.0, "impact": {"x": 900.0, "y": 0.0, "z": 60.0}}
    assert registry.triage(MAP, [spot], plan=None, graph=None).walls == (spot,)


def test_an_actor_whose_bounds_moved_is_a_gate_not_a_static_obstacle():
    registry = GateRegistry()
    closed = panel()
    open_ = panel(bounds=box_bounds(1296.0, 58810.0, 16.0, 100.0))  # swung through ninety degrees
    assert registry.triage(MAP, [closed]).gates == ()
    changes = registry.reconcile(MAP, sweep(open_), [{"x": 1296.0, "y": 58700.0, "z": 725.0}, {"x": 1296.0, "y": 58900.0, "z": 725.0}])
    assert [change["change"] for change in changes] == ["bounds_changed"]
    triage = registry.triage(MAP, [open_])
    assert triage.walls == () and triage.new_gates[0].signals == ("bounds_changed",)


def test_small_differences_in_bounds_are_not_movement():
    registry = GateRegistry()
    registry.triage(MAP, [panel()])
    jitter = box_bounds(1295.99 + 2.0, 58810.27, 100.5, 16.02)
    assert registry.reconcile(MAP, sweep(panel(bounds=jitter)), []) == []


def test_an_actor_gone_from_where_a_sweep_passes_through_it_is_dynamic():
    registry = GateRegistry()
    registry.triage(MAP, [panel()])
    through = [{"x": 1296.0, "y": 58700.0, "z": 725.0}, {"x": 1296.0, "y": 58900.0, "z": 725.0}]
    # A sweep that never went near it says nothing.
    assert registry.reconcile(MAP, sweep(), [{"x": 0.0, "y": 0.0, "z": 0.0}, {"x": 100.0, "y": 0.0, "z": 0.0}]) == []
    # An unfinished sweep says nothing either.
    assert registry.reconcile(MAP, sweep(complete=False), through) == []
    (change,) = registry.reconcile(MAP, sweep(), through)
    assert change["change"] == "vanished_between_sweeps"
    assert registry.triage(MAP, [panel()]).new_gates[0].signals == ("vanished_between_sweeps",)


@pytest.mark.parametrize("fault", GATE_COVERAGE_FAULTS)
def test_partial_or_malformed_route_coverage_does_not_make_a_remembered_wall_vanish(fault):
    registry = GateRegistry()
    registry.triage(MAP, [panel()])
    through = [{"x": 1296.0, "y": 58700.0, "z": 725.0},
               {"x": 1296.0, "y": 58810.0, "z": 725.0},
               {"x": 1296.0, "y": 58900.0, "z": 725.0}]
    assert registry.reconcile(MAP, faulty_gate_sweep(fault), through) == []
    assert registry.sighting(MAP, PANEL_ACTOR).dynamic_reason is None
    # Complete coverage of the same route remains positive evidence of disappearance.
    assert registry.reconcile(MAP, open_way(), through)[0]["change"] == "vanished_between_sweeps"


@pytest.mark.parametrize("completion", [None, False, 1, "true", "missing"])
def test_incomplete_reverification_keeps_a_static_wall_in_the_overlay(graph, completion):
    payload = sweep()
    if completion == "missing":
        del payload["segments"][0]["complete"]
    else:
        payload["segments"][0]["complete"] = completion
    adapter = AuditAdapter(graph, [payload])
    obstacle = obstacle_from_bounds(PANEL_ACTOR, DOOR_BOUNDS, sequence=1, t=0.0)
    adapter.static_obstacles.admit_obstacle(scope=adapter.static_obstacles.scope, obstacle=obstacle)
    adapter.gate_registry.triage(MAP, [panel(bounds=DOOR_BOUNDS)])
    adapter._reverify_actor_obstacles(adapter.topology_session)
    assert walls(adapter) == (obstacle,) and adapter.gate_registry.gates(MAP) == ()
    with pytest.raises(AdapterError, match="static_obstacle_global_replan_unavailable"):
        adapter.plan()


@pytest.mark.parametrize("bounds", [None, {}, {"origin": {}, "extent": {}}])
def test_an_actor_contact_without_readable_bounds_is_still_present(bounds):
    registry = GateRegistry()
    registry.triage(MAP, [panel()])
    through = [{"x": 1296.0, "y": 58700.0, "z": 725.0}, {"x": 1296.0, "y": 58900.0, "z": 725.0}]
    contact = panel(bounds=bounds)
    assert registry.reconcile(MAP, sweep(contact), through) == []
    assert registry.sighting(MAP, PANEL_ACTOR).dynamic_reason is None
    assert registry.triage(MAP, [panel()]).gates == ()


def test_the_registry_forgets_after_its_ttl_and_keeps_maps_apart():
    now = [0.0]
    registry = GateRegistry(ttl_seconds=600.0, clock=lambda: now[0])
    registry.triage(MAP, [hit()])
    assert len(registry.gates(MAP)) == 1 and registry.gates("OtherMap") == ()
    now[0] = 601.0
    assert registry.gates(MAP) == () and registry.sighting(MAP, DOOR_ACTOR) is None


# ------------------------------------------------------------------ planning


def known_door(registry=None):
    registry = registry or GateRegistry()
    registry.add_gate(MAP, hit(), ["name:door"], {})
    return registry


def planner_on(graph, start=START, end=END):
    return lambda overlay: plan_with_static_obstacles(graph, start, end, overlay, now_t=0.0)


def empty_overlay():
    overlay = StaticObstacleOverlay()
    overlay.bind_scope(StaticObstacleScope("session", MAP, MAP, "F" * 64))
    return overlay


def test_the_plan_goes_through_the_gate_as_a_gate_waypoint(graph):
    registry = known_door()
    plan = plan_through_gates(planner_on(graph), empty_overlay(), registry.gates(MAP))
    assert plan.status == "complete"
    (waypoint,) = plan.diagnostics["dynamic_gates"]
    assert waypoint["kind"] == "dynamic_gate" and waypoint["actor"] == DOOR_ACTOR
    assert waypoint["reason"] == "name:door" and waypoint["signals"] == ["name:door"]
    assert waypoint["bounds"]["origin"]["y"] == pytest.approx(58810.27, abs=0.01)
    assert waypoint["along_m"] == pytest.approx(4.11, abs=0.05)  # the door is 4.1 m up the route
    assert waypoint["waypoint_index"] == 0 and waypoint["penalty_m"] == GATE_PENALTY_M
    # 2 m short of the gate on the route, and past it.
    assert waypoint["approach"]["y"] == pytest.approx(START.y + 210.0, abs=15.0)
    assert waypoint["beyond"]["y"] > waypoint["position"]["y"] > waypoint["approach"]["y"]
    assert plan.diagnostics["dynamic_gate_planning"]["chosen"] == "through"
    assert plan.diagnostics["dynamic_gate_planning"]["around_status"] == "failed"
    # It survives serialization: the serialized plan is what is authorized.
    assert plan_gates(json.loads(json.dumps(plan.to_dict())))[0]["gate_id"] == waypoint["gate_id"]


def test_a_gate_off_the_route_leaves_the_plan_unmarked(graph):
    registry = GateRegistry()
    registry.add_gate(MAP, hit(bounds=box_bounds(1700.0, 58500.0, 100.0, 16.0)), ["name:door"], {})
    plan = plan_through_gates(planner_on(graph), empty_overlay(), registry.gates(MAP))
    assert plan.status == "complete" and plan.diagnostics["dynamic_gates"] == []
    assert plan.diagnostics["dynamic_gate_planning"]["chosen"] == "direct"


def test_a_real_wall_in_the_neck_still_refuses_the_route(graph):
    # Not a gate: nothing says so and the crate is no leaf. As before.
    overlay = empty_overlay()
    overlay.admit_obstacle(scope=overlay.scope, obstacle=obstacle_from_bounds(
        PANEL_ACTOR, box_bounds(1296.0, 58810.0, 100.0, 100.0), sequence=1, t=0.0))
    refused = plan_through_gates(planner_on(graph), overlay, known_door().gates(MAP))
    assert refused.status == "failed" and refused.reason_code == "static_obstacle_global_replan_unavailable"


def open_floor():
    ref = PolygonReference.make(1, 1, 0)
    polygon = DetourPolygon(reference=ref, tile_x=1, tile_y=1, layer=0, flags=1, area=0, poly_type=0, links=(),
                            identity_source="test", vertices=(Vec3(-1000.0, -1000.0, 0.0), Vec3(1000.0, -1000.0, 0.0),
                                                              Vec3(1000.0, 1000.0, 0.0), Vec3(-1000.0, 1000.0, 0.0)))
    return DetourTopologyGraph({ref.value: polygon}, {}, snapshot_diagnostics={})


def leaf(ey):
    return DynamicGate(gate_id="gate-x", actor="BG_Gate_C x", actor_class="BG_Gate_C",
                       bounds=box_bounds(0.0, 0.0, 16.0, ey, ez=150.0, z=0.0), signals=("name:gate",), evidence={})


def test_a_short_way_round_beats_the_gate_and_a_long_one_does_not():
    floor = open_floor()
    start, end = Vec3(-500.0, 0.0, 0.0), Vec3(500.0, 0.0, 0.0)
    around = plan_through_gates(planner_on(floor, start, end), empty_overlay(), [leaf(200.0)])
    assert around.diagnostics["dynamic_gate_planning"]["chosen"] == "around"
    assert around.diagnostics["dynamic_gates"] == []
    assert around.total_estimated_path_length_m > 10.0
    # The same gate costs more than the way round: walk round.
    cheap = plan_through_gates(planner_on(floor, start, end), empty_overlay(), [leaf(200.0)], penalty_m=0.5)
    assert cheap.diagnostics["dynamic_gate_planning"]["chosen"] == "through"
    assert len(cheap.diagnostics["dynamic_gates"]) == 1
    assert cheap.total_estimated_path_length_m == pytest.approx(10.0)
    # A gate across the whole floor has no way round at any price.
    walled = plan_through_gates(planner_on(floor, start, end), empty_overlay(), [leaf(1500.0)])
    assert walled.diagnostics["dynamic_gate_planning"]["chosen"] == "through"


def test_a_gate_extent_change_invalidates_the_memoized_plan():
    adapter = AuditAdapter(open_floor(), [sweep()], start=Vec3(-500.0, 0.0, 0.0), end=Vec3(500.0, 0.0, 0.0))
    adapter.gate_registry.add_gate(MAP, hit(bounds=box_bounds(0.0, 0.0, 16.0, 200.0, z=0.0)), ["name:door"], {})
    around = adapter.plan()
    assert around.diagnostics["dynamic_gate_planning"]["chosen"] == "around"
    assert around.diagnostics["dynamic_gates"] == []
    # The centre stays still as the leaf's extents change; the old way round is now blocked.
    adapter.gate_registry.add_gate(MAP, hit(bounds=box_bounds(0.0, 0.0, 16.0, 1500.0, z=0.0)), ["name:door"], {})
    updated = adapter.plan()
    assert updated.diagnostics["dynamic_gate_planning"]["chosen"] == "through"
    assert updated.diagnostics["dynamic_gates"][0]["bounds"]["extent"]["y"] == 1500.0


def test_a_route_that_ends_in_front_of_a_gate_does_not_cross_it(graph):
    gate = known_door().gates(MAP)[0]
    short = graph.plan(START, Vec3(1320.0, 58700.0, 730.0))
    assert placements(short.waypoints, [gate]) == []
    assert len(placements(door_plan(graph).waypoints, [gate])) == 1


# ------------------------------------------- the audit with a navmesh behind it


class AuditAdapter:
    """What _audit_route_collisions, the gate-aware planner and its helpers use of an adapter."""

    _move_target_clear_of_obstacles = LiveRouteAdapter._move_target_clear_of_obstacles
    _reconcile_gates = LiveRouteAdapter._reconcile_gates
    _promote_sealed_gates = LiveRouteAdapter._promote_sealed_gates
    _reverify_actor_obstacles = LiveRouteAdapter._reverify_actor_obstacles
    _plan_with_static_overlay = LiveRouteAdapter._plan_with_static_overlay
    _static_overlay_plan_key = LiveRouteAdapter._static_overlay_plan_key

    def __init__(self, graph, sweeps, *, start=START, end=END):
        self.config = SimpleNamespace(route_mode="cached_topology", portal_corner_inset_m=0.0,
                                      routing_objective="distance")
        self.topology_session = SimpleNamespace(graph=graph, session_id="session", fingerprint="F" * 64,
                                                map_id=MAP, world_id=MAP)
        self.start, self.route_target = start, end
        self.static_obstacles = empty_overlay()
        self.gate_registry = GateRegistry()
        self.timeline, self.swept, self.sweeps = [], [], list(sweeps)
        self.route_collision_audit = self._audit

    def _audit(self, points):
        self.swept.append(points)
        return self.sweeps.pop(0) if len(self.sweeps) > 1 else self.sweeps[0]

    def monotonic(self):
        return 1.0

    def plan(self):
        result = self._plan_with_static_overlay(self.topology_session, self.start, self.route_target)
        if result.status != "complete":
            raise AdapterError(f"cached_topology_route_plan_{result.reason_code}")
        return result

    def audit(self):
        return LiveRouteAdapter._audit_route_collisions(
            self, SimpleNamespace(sequence=7, map_id=MAP), self.plan(), self.plan)


def walls(adapter):
    return adapter.static_obstacles.planning_snapshot().obstacles


def test_a_closed_named_door_is_planned_through_not_refused(graph):
    adapter = AuditAdapter(graph, [sweep(hit())])
    plan = adapter.audit()
    assert plan.status == "complete" and walls(adapter) == ()
    (waypoint,) = plan.diagnostics["dynamic_gates"]
    assert waypoint["signals"] == ["name:door"]
    event = adapter.timeline[-1]
    assert event["outcome"] == "clear" and len(event["rounds"]) == 2
    assert event["rounds"][0]["gates"][0]["new"] is True and event["rounds"][1]["gates"][0]["new"] is False
    assert event["rounds"][0]["admitted_or_grown"] == []


# Live 2026-10-13, swept on the bare plan from the prologue start: the shut door
# and, inside its bounds, its leaf read by the floor re-sampling as a ledge.
LIVE_GATE_ACTOR = ("BG0109_00_Gate_Standard_C /Game/Level/Game/Field/4000-MIDGR/Layout/4000-MIDGR_020_5thOutWall_Gimmick."
                   "4000-MIDGR_020_5thOutWall_Gimmick:PersistentLevel.BG0109_00_Gate_Standard2")
LIVE_GATE_BOUNDS = {"origin": {"x": 1295.986694, "y": 58810.265625, "z": 870.812683},
                    "extent": {"x": 100.509277, "y": 16.019531, "z": 151.500305}}


def ledge(x=1317.5, y=58811.266667, z=783.333313):
    value = {"distance_cm": 97.97832, "impact": {"x": x, "y": y, "z": z}, "ledge": True, "rise_cm": 62.127869,
             "normal_z": 0}
    value["kind"] = classify_hit(value)
    return value


def live_gate():
    return hit(actor=LIVE_GATE_ACTOR, actor_class="BG0109_00_Gate_Standard_C", bounds=LIVE_GATE_BOUNDS,
               impact={"x": 1316.640503, "y": 58810.101562, "z": 860.534119}, distance_cm=71.484849)


def test_the_shut_gates_leaf_read_as_a_ledge_is_the_gate_not_a_wall(graph):
    adapter = AuditAdapter(graph, [sweep(ledge(), live_gate())])
    plan = adapter.audit()
    assert plan.status == "complete" and walls(adapter) == ()
    (waypoint,) = plan.diagnostics["dynamic_gates"]
    assert waypoint["signals"] == ["name:gate"]
    first = adapter.timeline[-1]["rounds"][0]
    assert first["unknown_obstacles"] == [] and first["admitted_or_grown"] == []
    assert [item["gate_id"] for item in first["absorbed_by_gates"]] == [waypoint["gate_id"]]
    assert first["absorbed_by_gates"][0]["kind"] == "ledge"


def test_only_spots_inside_a_gates_bounds_are_absorbed():
    registry = GateRegistry()
    outside = ledge(y=58700.0)
    above = ledge(z=1300.0)
    triage = registry.triage(MAP, [ledge(), outside, above, live_gate()])
    assert [gate.actor for gate in triage.gates] == [LIVE_GATE_ACTOR]
    assert triage.walls == (outside, above)
    assert [hit for _, hit in triage.absorbed] == [ledge()]


def test_a_ledge_on_a_known_gate_is_absorbed_when_the_gate_itself_is_not_hit():
    registry = GateRegistry()
    registry.triage(MAP, [live_gate()])
    triage = registry.triage(MAP, [ledge()])
    assert triage.walls == () and triage.gates == () and len(triage.absorbed) == 1


def test_a_promoted_leaf_takes_its_ledge_out_of_the_overlay_too(graph):
    leaf_hit = panel(bounds=LIVE_GATE_BOUNDS)
    adapter = AuditAdapter(graph, [sweep(ledge(), leaf_hit)])
    plan = adapter.audit()
    assert plan.status == "complete" and walls(adapter) == ()
    (waypoint,) = plan.diagnostics["dynamic_gates"]
    assert waypoint["signals"] == ["sealed_neck"]


def test_an_unnamed_leaf_sealing_the_neck_is_promoted_to_a_gate_after_the_refusal(graph):
    adapter = AuditAdapter(graph, [sweep(panel())])
    plan = adapter.audit()
    assert plan.status == "complete" and walls(adapter) == ()  # the wall it was admitted as is gone
    (waypoint,) = plan.diagnostics["dynamic_gates"]
    assert waypoint["signals"] == ["sealed_neck"]
    assert waypoint["evidence"]["neck"]["neck_width_m"] == pytest.approx(0.9, abs=0.01)
    assert adapter.timeline[0]["event"] == "dynamic_gate_registered" and adapter.timeline[0]["change"] == "sealed_neck"
    assert "static_obstacle_global_replan_unavailable" in adapter.timeline[0]["replan_failure"]
    assert adapter.timeline[-1]["rounds"][0]["promoted_gates"] == [waypoint["gate_id"]]


def test_a_thick_obstacle_in_the_neck_still_blocks_and_stays_a_wall(graph):
    crate = panel(bounds=box_bounds(1296.0, 58810.0, 100.0, 100.0))
    adapter = AuditAdapter(graph, [sweep(crate)])
    with pytest.raises(AdapterError, match="static_obstacle_global_replan_unavailable"):
        adapter.audit()
    assert len(walls(adapter)) == 1 and adapter.gate_registry.gates(MAP) == ()
    # The refusal keeps what the sweeps found.
    event = adapter.timeline[-1]
    assert event["event"] == "route_collision_audit" and event["outcome"] == "no_route"
    assert event["rounds"][0]["unknown_obstacles"][0]["actor"] == PANEL_ACTOR


def test_an_obstacle_with_a_way_round_is_routed_round_as_before(graph):
    # Open ground (polygon 7): a leaf nothing names, with room to pass.
    start, end = Vec3(1100.0, 59050.0, 720.0), Vec3(1700.0, 59250.0, 720.0)
    adapter = AuditAdapter(graph, [sweep(panel(bounds=box_bounds(1400.0, 59150.0, 60.0, 16.0, z=870.0))), sweep()],
                           start=start, end=end)
    plan = adapter.audit()
    assert len(walls(adapter)) == 1 and adapter.gate_registry.gates(MAP) == ()
    assert "dynamic_gates" not in plan.diagnostics
    assert adapter.timeline[-1]["outcome"] == "clear"


def test_a_door_that_moved_between_sweeps_leaves_the_overlay(graph):
    # Round one: a prop nothing names, shut across a room with a way round, so
    # it is a wall. Sweeping it again, its bounds have moved: it is a gate, and
    # the wall it was is taken out of the overlay.
    start, end = Vec3(1100.0, 59050.0, 720.0), Vec3(1700.0, 59250.0, 720.0)
    shut = panel(bounds=box_bounds(1400.0, 59150.0, 60.0, 16.0, z=870.0))
    moved = panel(bounds=box_bounds(1400.0, 59150.0, 16.0, 60.0, z=870.0))
    adapter = AuditAdapter(graph, [sweep(shut), sweep(moved), sweep()], start=start, end=end)
    adapter.audit()
    assert walls(adapter) == ()
    (gate,) = adapter.gate_registry.gates(MAP)
    assert gate.signals == ("bounds_changed",)
    assert any(event.get("change") == "bounds_changed" for event in adapter.timeline if event["event"] == "dynamic_gate_registered")


def test_a_wall_kept_from_an_earlier_attempt_is_swept_again_before_planning(graph):
    # A door seen shut and kept as a wall; the next attempt finds it open.
    adapter = AuditAdapter(graph, [sweep()])
    obstacle = obstacle_from_bounds(PANEL_ACTOR, DOOR_BOUNDS, sequence=1, t=0.0)
    adapter.static_obstacles.admit_obstacle(scope=adapter.static_obstacles.scope, obstacle=obstacle)
    adapter.gate_registry.triage(MAP, [panel(bounds=DOOR_BOUNDS)])
    with pytest.raises(AdapterError):
        adapter.plan()  # refused: the wall seals the neck
    adapter._reverify_actor_obstacles(adapter.topology_session)
    assert len(adapter.swept) == 1 and walls(adapter) == ()
    (gate,) = adapter.gate_registry.gates(MAP)
    assert gate.signals == ("vanished_between_sweeps",)
    # The sweep went across the leaf, square to its long axis, on the navmesh floor.
    first, second = adapter.swept[0]
    assert first["x"] == pytest.approx(second["x"], abs=1.0) and first["y"] < 58810.0 < second["y"]
    assert first["z"] == pytest.approx(725.0, abs=15.0)
    assert adapter.plan().status == "complete"
    assert adapter.timeline[-1]["event"] == "obstacle_reverified"


def test_a_wall_that_is_still_there_stays_a_wall_after_the_second_look(graph):
    adapter = AuditAdapter(graph, [sweep(panel(bounds=DOOR_BOUNDS))])
    adapter.static_obstacles.admit_obstacle(scope=adapter.static_obstacles.scope, obstacle=obstacle_from_bounds(
        PANEL_ACTOR, DOOR_BOUNDS, sequence=1, t=0.0))
    adapter.gate_registry.triage(MAP, [panel(bounds=DOOR_BOUNDS)])
    adapter._reverify_actor_obstacles(adapter.topology_session)
    assert len(walls(adapter)) == 1 and adapter.gate_registry.gates(MAP) == ()


# ---------------------------------------------------------------- the follower


GATE_WAYPOINT = {"gate_id": "gate-1", "actor": DOOR_ACTOR, "bounds": DOOR_BOUNDS, "along_m": 4.11,
                 "approach": {"x": 1335.0, "y": 58610.0, "z": 727.0}, "position": {"x": 1321.0, "y": 58810.0, "z": 724.0},
                 "beyond": {"x": 1305.0, "y": 59020.0, "z": 721.0}}


def closed(segments=2):
    return sweep(hit(), segments=segments)


def open_way(segments=2):
    return sweep(segments=segments)


class Clock:
    def __init__(self):
        self.t = 100.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def scripted(*payloads):
    queue = list(payloads)
    calls = []

    def sweeper(points):
        calls.append(points)
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(value, Exception):
            raise value
        return value

    sweeper.calls = calls
    return sweeper


def test_probe_is_clear_only_when_the_whole_sweep_came_back_without_the_gate():
    assert probe_gate(open_way(), GATE_WAYPOINT).clear
    shut = probe_gate(closed(), GATE_WAYPOINT)
    assert not shut.clear and shut.verified and shut.blocking[0]["actor"] == DOOR_ACTOR
    unfinished = probe_gate(sweep(complete=False, segments=2), GATE_WAYPOINT)
    assert not unfinished.clear and not unfinished.verified
    assert not probe_gate({"segments": []}, GATE_WAYPOINT).clear
    # Something else in the doorway still counts; something far away does not.
    other = hit(actor="BG0003_00_Crate_C x", bounds=box_bounds(1330.0, 58805.0, 30.0, 30.0, ez=40.0, z=750.0),
                impact={"x": 1320.0, "y": 58800.0, "z": 740.0})
    assert not probe_gate(sweep(other, segments=2), GATE_WAYPOINT).clear
    far = hit(actor="BG0003_00_Crate_C x", bounds=box_bounds(3000.0, 58000.0, 30.0, 30.0, ez=40.0, z=750.0),
              impact={"x": 3000.0, "y": 58000.0, "z": 740.0})
    assert probe_gate(sweep(far, segments=2), GATE_WAYPOINT).clear


@pytest.mark.parametrize("value", [None, False, 1, "true"])
def test_missing_or_non_boolean_completion_never_opens_a_gate(value):
    payload = open_way()
    payload["segments"][0]["complete"] = value
    assert not probe_gate(payload, GATE_WAYPOINT).verified
    assert not probe_gate(payload, GATE_WAYPOINT).clear
    del payload["segments"][0]["complete"]
    assert not probe_gate(payload, GATE_WAYPOINT).clear


def faulty_gate_sweep(fault):
    payload = open_way()
    rows = payload["segments"]
    if fault == "no_segments":
        del payload["segments"]
    elif fault == "missing_first_leg":
        rows.pop(0)
    elif fault == "missing_second_leg":
        rows.pop()
    elif fault == "duplicate_leg":
        rows[1]["index"] = 0
    elif fault == "reversed_legs":
        rows.reverse()
    elif fault == "skipped_index":
        rows[1]["index"] = 2
    elif fault == "extra_leg":
        rows.append({"index": 2, "complete": True, "hits": []})
    elif fault == "missing_index":
        del rows[0]["index"]
    elif fault in {"null_index", "string_index", "boolean_index", "float_index"}:
        rows[0]["index"] = {"null_index": None, "string_index": "0", "boolean_index": False,
                            "float_index": 0.0}[fault]
    elif fault == "segments_not_array":
        payload["segments"] = {"0": rows[0], "1": rows[1]}
    elif fault == "segment_not_object":
        rows[0] = None
    elif fault == "missing_hits":
        del rows[0]["hits"]
    elif fault in {"null_hits", "hits_not_array", "hit_not_object"}:
        rows[0]["hits"] = {"null_hits": None, "hits_not_array": {}, "hit_not_object": [None]}[fault]
    elif fault == "error_despite_completion":
        rows[0]["error"] = "capsule_sweep_failed"
    else:
        raise AssertionError(fault)
    return payload


@pytest.mark.parametrize("fault", GATE_COVERAGE_FAULTS)
def test_malformed_coverage_never_verifies_or_opens_a_gate(fault):
    result = probe_gate(faulty_gate_sweep(fault), GATE_WAYPOINT)
    assert not result.verified and not result.clear
    assert result.error  # The failed coverage check survives in the wait evidence.


@pytest.mark.parametrize("fault", GATE_COVERAGE_FAULTS)
def test_malformed_coverage_times_out_as_unverified(fault):
    clock = Clock()
    result = wait_for_gate(GATE_WAYPOINT, sweep=scripted(faulty_gate_sweep(fault)),
                           now=clock.now, sleep=clock.sleep)
    assert result.outcome == "gate_state_unverified"
    assert result.polls == 9 and result.waited_seconds == pytest.approx(GATE_WAIT_SECONDS)
    assert result.to_dict()["last_probe"]["verified"] is False


@pytest.mark.parametrize("distance_cm", [0.0, 20.0, 300.0])
def test_a_known_gate_contact_is_not_filtered_out_as_a_start_contact(distance_cm):
    result = probe_gate(sweep(hit(distance_cm=distance_cm), segments=2), GATE_WAYPOINT)
    assert result.verified and not result.clear
    assert result.blocking[0]["actor"] == DOOR_ACTOR


@pytest.mark.parametrize("same_actor", [True, False])
def test_a_closed_door_contact_is_not_filtered_out_by_the_planners_door_rule(same_actor):
    actor = "BG0007_00_Door2Bar_Standard_C /Game/EndGame:PersistentLevel.Door_7"
    shut = hit(actor=actor, actor_class="BG0007_00_Door2Bar_Standard_C")
    assert classify_hit(shut) == "door"
    gate = {**GATE_WAYPOINT, "actor": actor if same_actor else DOOR_ACTOR}
    clock = Clock()
    result = wait_for_gate(gate, sweep=scripted(sweep(shut, segments=2)),
                           now=clock.now, sleep=clock.sleep)
    assert result.outcome == "gate_did_not_open" and result.probe.verified
    assert result.probe.blocking[0]["actor"] == actor


@pytest.mark.parametrize("impact", [None, {}, {"x": "invalid", "y": 0.0},
                                  {"x": float("nan"), "y": 0.0}, {"x": float("inf"), "y": 0.0}])
def test_a_blocker_that_cannot_be_located_does_not_prove_the_gate_open(impact):
    contact = hit(actor="BP_UnknownProp x", bounds=None, impact=impact)
    result = probe_gate(sweep(contact, segments=2), GATE_WAYPOINT)
    assert not result.clear and not result.verified and result.error


def test_a_closed_reading_followed_by_unverified_readings_is_not_current_closed_evidence():
    clock = Clock()
    result = wait_for_gate(GATE_WAYPOINT,
                           sweep=scripted(closed(), faulty_gate_sweep("missing_second_leg")),
                           now=clock.now, sleep=clock.sleep)
    assert result.outcome == "gate_state_unverified" and not result.probe.verified


def test_waiting_ends_the_moment_the_gate_is_clear():
    clock = Clock()
    sweeper = scripted(closed(), closed(), open_way())
    result = wait_for_gate(GATE_WAYPOINT, sweep=sweeper, now=clock.now, sleep=clock.sleep)
    assert result.outcome == "opened" and result.polls == 3
    assert result.waited_seconds == pytest.approx(1.0) and clock.slept == [0.5, 0.5]
    # Only the gate was swept: from the approach, through it, and past it.
    assert sweeper.calls[0] == [GATE_WAYPOINT["approach"], GATE_WAYPOINT["position"], GATE_WAYPOINT["beyond"]]


def test_a_gate_that_stays_shut_ends_the_wait_with_its_evidence_inside_the_bound():
    clock = Clock()
    result = wait_for_gate(GATE_WAYPOINT, sweep=scripted(closed()), now=clock.now, sleep=clock.sleep)
    assert result.outcome == "gate_did_not_open"
    assert GATE_WAIT_SECONDS <= result.waited_seconds < GATE_WAIT_SECONDS + 0.5 + 1e-9
    assert result.polls == 9 and result.probe.blocking[0]["actor"] == DOOR_ACTOR
    assert result.to_dict()["last_probe"]["verified"] is True


def test_a_gate_that_cannot_be_swept_is_not_called_closed_or_open():
    clock = Clock()
    result = wait_for_gate(GATE_WAYPOINT, sweep=scripted(RuntimeError("route_sweep_timeout")),
                           now=clock.now, sleep=clock.sleep)
    assert result.outcome == "gate_state_unverified" and result.sweep_errors == result.polls
    # An error between answers does not stop the wait.
    clock = Clock()
    recovered = wait_for_gate(GATE_WAYPOINT, sweep=scripted(RuntimeError("x"), open_way()), now=clock.now, sleep=clock.sleep)
    assert recovered.outcome == "opened" and recovered.sweep_errors == 1


def test_the_wait_gives_way_to_a_lost_readiness():
    clock = Clock()
    reasons = iter([None, "menu_or_pause"])
    result = wait_for_gate(GATE_WAYPOINT, sweep=scripted(closed()), now=clock.now, sleep=clock.sleep,
                           interrupted=lambda: next(reasons))
    assert result.outcome == "interrupted" and result.interruption == "menu_or_pause" and result.polls == 2


# ------------------------------------------- the pursuit follower at a gate


def pursuit_gate_adapter(observations, sweeps):
    actuator = StreamingActuator()
    config = LiveAdapterConfig(controller_mode="continuous", continuous_fast_follow=True, pursuit=True,
                               post_arrival_samples=0, max_cycles=4, readiness_recovery_timeout_seconds=.2,
                               preflight_telemetry_wait_seconds=.2)
    adapter, telemetry, _, _ = make_adapter(actuator=actuator, config=config, observations=observations,
                                            follower_mode="ARRIVED_ON_FIRST")
    # A gate across the whole floor at x=300 on the 4 m route: no way round it.
    adapter.gate_registry.add_gate(MAP, {"actor": DOOR_ACTOR, "actor_class": DOOR_CLASS, "actor_bounds": box_bounds(
        300.0, 0.0, 16.0, 1500.0, ez=150.0, z=0.0)}, ["name:door"], {})
    adapter.route_collision_audit = scripted(*sweeps)
    return adapter, actuator


def gate_sweep(shut):
    hit_ = {"actor": DOOR_ACTOR, "actor_bounds": box_bounds(300.0, 0.0, 16.0, 1500.0, ez=150.0, z=0.0),
            "normal_z": 0.0, "distance_cm": 150.0, "impact": {"x": 285.0, "y": 0.0, "z": 60.0}}
    return sweep(*([hit_] if shut else []), segments=2)


def march(xs, *, start=1, **changes):
    return [make_observation(sequence=start + i, position=Vec3(x, 0.0, 0.0), velocity_cm_s=Vec3(300.0, 0.0, 0.0),
                             **changes) for i, x in enumerate(xs)]


APPROACH = [0, 0, 0, 0, 50, 100, 100]
STANDING = [100] * 30


def events(adapter, name):
    return [item for item in adapter.timeline if item.get("event") == name]


def test_the_preflight_lists_the_gates_on_the_route_before_any_movement():
    adapter, actuator = pursuit_gate_adapter(march(APPROACH + STANDING), [gate_sweep(False)])
    report = adapter.preflight()
    assert report["movement_authorized"] is True and actuator.updates == []
    (gate,) = report["dynamic_gates"]
    assert gate["actor"] == DOOR_ACTOR and gate["reason"] == "name:door" and gate["along_m"] == pytest.approx(3.0, abs=0.05)
    assert gate["approach"]["x"] == pytest.approx(100.0, abs=1.0)  # 2 m short of it
    (reported,) = events(adapter, "route_gates_reported")
    assert reported["gate_count"] == 1 and reported["movement_command_sent"] is False
    assert reported["planning"]["chosen"] == "through"
    assert report["route_plan"]["diagnostics"]["dynamic_gates"][0]["gate_id"] == gate["gate_id"]


def test_the_follower_stops_short_of_a_closed_gate_waits_and_goes_when_it_opens():
    adapter, actuator = pursuit_gate_adapter(
        march(APPROACH + STANDING + [150, 200, 250, 300, 340, 380, 400, 400, 400]),
        [gate_sweep(True), gate_sweep(True), gate_sweep(False)])
    assert adapter.preflight()["movement_authorized"]
    result = adapter.run(dry_run=False)
    assert result["state"] == "ARRIVED", result["reason"]
    names = [item["event"] for item in result["timeline"]]
    assert names.index("gate_wait_started") < names.index("gate_opened") < names.index("pursuit_arrived")
    started, opened = names.index("gate_wait_started"), names.index("gate_opened")
    # The stick was released as it stopped, and no stick commands went out while it waited.
    released = [index for index, item in enumerate(result["timeline"])
                if item["event"] == "actuator_neutralized" and item["reason"] == "pursuit_gate_wait"]
    assert released and released[0] <= started
    assert "pursuit_command" not in names[released[0]:opened]
    assert "pursuit_command" in names[:released[0]]  # it had been walking up to the gate
    assert events(adapter, "gate_opened")[0]["wait"]["polls"] == 3
    # Commands resumed afterwards, on the same hold-and-renew stream.
    assert "pursuit_command" in names[opened:]
    assert not events(adapter, "gate_did_not_open")


def test_a_gate_that_never_opens_ends_the_route_with_that_outcome_and_evidence():
    adapter, actuator = pursuit_gate_adapter(march(APPROACH + STANDING, in_interaction=False), [gate_sweep(True)])
    assert adapter.preflight()["movement_authorized"]
    result = adapter.run(dry_run=False)
    assert result["state"] == "STOPPED" and result["reason"] == "gate_did_not_open"
    (event,) = events(adapter, "gate_did_not_open")
    assert event["gate"]["actor"] == DOOR_ACTOR and event["gate"]["reason"] == "name:door"
    assert event["wait"]["outcome"] == "gate_did_not_open" and event["wait"]["last_probe"]["blocking"]
    assert 4.0 <= event["wait"]["waited_seconds"] < 5.0
    assert event["distance_to_gate_m"] == pytest.approx(2.0, abs=0.2)
    assert event["interaction_prompt_visible"] is False and event["movement_command_sent"] is False
    # It stopped there: nothing was sent after the wait, and the careful controller did not take over.
    names = [item["event"] for item in result["timeline"]]
    assert "pursuit_command" not in names[names.index("gate_wait_started"):]
    assert "pursuit_handed_over" not in names and not any(name == "control_cycle" for name in names)
    assert actuator.neutral


def test_a_prompt_beside_the_gate_is_reported_not_a_reason_to_hand_over():
    prompted = march(APPROACH, start=1) + march(STANDING, start=len(APPROACH) + 1, in_interaction=True)
    adapter, _ = pursuit_gate_adapter(prompted, [gate_sweep(True)])
    assert adapter.preflight()["movement_authorized"]
    result = adapter.run(dry_run=False)
    assert result["reason"] == "gate_did_not_open"
    assert events(adapter, "gate_did_not_open")[0]["interaction_prompt_visible"] is True


def test_a_gate_that_cannot_be_checked_is_reported_as_unverified():
    adapter, _ = pursuit_gate_adapter(march(APPROACH + STANDING), [RuntimeError("route_sweep_timeout")])
    assert adapter.preflight()["movement_authorized"]
    result = adapter.run(dry_run=False)
    assert result["state"] == "STOPPED" and result["reason"] == "gate_state_unverified"
    assert events(adapter, "gate_state_unverified")


@pytest.mark.parametrize("fault", ["missing_second_leg", "duplicate_leg", "reversed_legs"])
def test_the_follower_does_not_resume_when_the_gate_probe_has_partial_coverage(fault):
    adapter, actuator = pursuit_gate_adapter(march(APPROACH + STANDING), [faulty_gate_sweep(fault)])
    assert adapter.preflight()["movement_authorized"]
    result = adapter.run(dry_run=False)
    assert result["state"] == "STOPPED" and result["reason"] == "gate_state_unverified"
    names = [item["event"] for item in result["timeline"]]
    assert "gate_opened" not in names and "pursuit_handed_over" not in names
    assert "pursuit_command" not in names[names.index("gate_wait_started"):]
    assert events(adapter, "gate_state_unverified")[0]["wait"]["last_probe"]["error"]
    assert actuator.neutral


def test_losing_gameplay_during_the_wait_hands_the_route_back():
    samples = march(APPROACH, start=1) + march([100] * 3, start=len(APPROACH) + 1) \
        + march(STANDING, start=len(APPROACH) + 4, in_menu=True)
    adapter, _ = pursuit_gate_adapter(samples, [gate_sweep(True)])
    assert adapter.preflight()["movement_authorized"]
    adapter.run(dry_run=False, max_cycles=1)
    handed = events(adapter, "pursuit_handed_over")
    assert handed and handed[0]["reason"].startswith("gate_wait_interrupted:menu_or_pause")


def test_a_route_without_gates_is_unchanged_and_never_sweeps_for_one():
    actuator = StreamingActuator()
    config = LiveAdapterConfig(controller_mode="continuous", continuous_fast_follow=True, pursuit=True,
                               post_arrival_samples=0, max_cycles=4, readiness_recovery_timeout_seconds=.2,
                               preflight_telemetry_wait_seconds=.2)
    adapter, _, _, _ = make_adapter(actuator=actuator, config=config, follower_mode="ARRIVED_ON_FIRST",
                                    observations=march([0, 0, 50, 100, 150, 200, 250, 300, 360, 360, 360, 360]))
    adapter.route_collision_audit = lambda points: pytest.fail("swept")
    report = adapter.preflight()
    assert report["movement_authorized"] and report["dynamic_gates"] == []
    assert "dynamic_gates" not in report["route_plan"]["diagnostics"]
    assert adapter.run(dry_run=False)["state"] == "ARRIVED"
