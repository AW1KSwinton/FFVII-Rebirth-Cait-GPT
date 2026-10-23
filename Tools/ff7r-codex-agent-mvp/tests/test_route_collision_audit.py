from types import SimpleNamespace

import pytest

from ff7r_agent.live_route_adapter import AdapterError, LiveRouteAdapter, MAX_ROUTE_AUDIT_ROUNDS
from ff7r_agent.models import Vec3
from ff7r_agent.route_sweep import classify_hit, obstacle_for_hit, obstacle_from_bounds, obstacle_key, unknown_obstacle_hits
from ff7r_agent.static_obstacles import StaticObstacleOverlay, StaticObstacleScope

TABLE = "BG2051_00_TableSet_Standard_C /Game/Level/Game/EndGame.EndGame:PersistentLevel.BG2051_00_TableSet_Standard_2"
TABLE_BOUNDS = {"origin": {"x": 500.0, "y": 0.0, "z": 50.0}, "extent": {"x": 160.0, "y": 135.0, "z": 49.0}}
BAKED = "CollisionAssetActor /x:PersistentLevel.Spl_BLA_WodPrt_0_NavDefaultAfT_Grid_2003006"


def hit(actor, *, bounds=None, component="EndSkeletalMeshComponent x", normal_z=0.0):
    return {"actor": actor, "actor_bounds": bounds, "component": component, "normal_z": normal_z,
            "simulating_physics": False, "distance_cm": 300}


def sweep(*hits):
    return {"query": {"profile": "EndRootCollision"}, "segments": [{"index": 0, "complete": True, "hits": list(hits)}]}


def plan(*xs):
    return SimpleNamespace(waypoints=tuple(Vec3(x, 0.0, 0.0) for x in xs), total_estimated_path_length_m=xs[-1] / 100)


class Adapter:
    """The attributes _audit_route_collisions uses."""

    _move_target_clear_of_obstacles = LiveRouteAdapter._move_target_clear_of_obstacles

    def __init__(self, sweeps, target=Vec3(1000.0, 0.0, 0.0)):
        self.route_target = target
        self.sweeps = list(sweeps)
        self.swept = []
        self.static_obstacles = StaticObstacleOverlay()
        self.static_obstacles.bind_scope(StaticObstacleScope("session", "EndGame", "EndGame", "F" * 64))
        self.timeline = []
        self.route_collision_audit = self._audit

    def _audit(self, points):
        self.swept.append(points)
        return self.sweeps.pop(0)

    def monotonic(self):
        return 1.0


def audit(adapter, first, replans):
    replans = list(replans)
    return LiveRouteAdapter._audit_route_collisions(
        adapter, SimpleNamespace(sequence=7), first, lambda: replans.pop(0))


def test_blockers_are_fixed_contacts_and_ledges_not_characters_or_floors():
    wall = hit(BAKED, bounds={"origin": {"x": 0, "y": 0, "z": 0}, "extent": {"x": 0, "y": 0, "z": 0}},
               component="CollisionAssetComponent x")
    wall["impact"] = {"x": 900.0, "y": 10.0, "z": 60.0}
    ledge = {"ledge": True, "rise_cm": 70, "normal_z": 0, "impact": {"x": 950.0, "y": 0.0, "z": 80.0}}
    result = unknown_obstacle_hits(sweep(
        hit(TABLE, bounds=TABLE_BOUNDS), wall, ledge,
        hit("PC0003_00_Aerith_Standard_C Aerith", bounds=TABLE_BOUNDS, component="EndCharacterRootComponent x"),
        hit("Floor", bounds=TABLE_BOUNDS, normal_z=0.95),
    ))
    assert [obstacle_key(item) for item in result] == ["actor:" + TABLE, "spot:36:0", "spot:38:0"]
    villager = hit("MB2300_00_KalmCitizen00_C Villager", bounds=TABLE_BOUNDS, component="EndCharacterRootComponent x")
    villager["impact"] = {"x": 700.0, "y": 50.0, "z": 60.0}
    # Townspeople are pushed past, not routed round.
    assert unknown_obstacle_hits(sweep(villager)) == []
    npc = {"segment": 0, **villager}
    assert obstacle_key(npc) == "spot:28:2" and obstacle_for_hit(npc, sequence=1, t=0.0).radius_m == pytest.approx(0.2)
    spot = obstacle_for_hit(result[1], sequence=1, t=0.0)
    assert spot.shape == "circle" and spot.radius_m == pytest.approx(0.2) and spot.center.x == 900.0


def test_touching_something_where_cloud_already_stands_is_not_a_blocker():
    # Live: the route to Nene started 11 cm from Ned's card table.
    table = {**hit(TABLE, bounds=TABLE_BOUNDS), "distance_cm": 11.0}
    assert unknown_obstacle_hits(sweep(table)) == []
    ahead = {**hit(TABLE, bounds=TABLE_BOUNDS), "distance_cm": 120.0}
    assert len(unknown_obstacle_hits(sweep(ahead))) == 1
    later = {"query": {}, "segments": [{"index": 3, "complete": True, "hits": [table]}]}
    assert len(unknown_obstacle_hits(later)) == 1


def test_starting_inside_baked_level_collision_is_not_a_blocker():
    # Live at Kalm: on the steps up to Nene the lifted capsule started inside
    # the stair collision the navmesh is built from.
    inside = hit(BAKED, component="CollisionAssetComponent x")
    inside.update(start_penetrating=True, impact={"x": 900.0, "y": 0.0, "z": 60.0})
    assert unknown_obstacle_hits(sweep(inside)) == []
    # A real contact with level collision (the route clips a wall) still counts,
    # and so does starting inside an individual actor the navmesh omits.
    clip = {**inside, "start_penetrating": False}
    table = {**hit(TABLE, bounds=TABLE_BOUNDS), "start_penetrating": True}
    assert len(unknown_obstacle_hits(sweep(clip))) == 1
    assert len(unknown_obstacle_hits(sweep(table))) == 1


def test_a_rise_just_over_the_step_limit_is_steep_stairs_not_a_ledge():
    # Live at Costa del Sol: 46 cm over one 50 cm step on the stairs to Dokaccha.
    stairs = {"ledge": True, "rise_cm": 46.4, "normal_z": 0, "impact": {"x": 950.0, "y": 0.0, "z": 80.0}}
    assert classify_hit(stairs) == "steep_step"
    assert unknown_obstacle_hits(sweep(stairs)) == []
    assert classify_hit({**stairs, "rise_cm": 90.0}) == "ledge"
    assert classify_hit({**stairs, "rise_cm": None}) == "ledge"


def test_starting_inside_a_static_level_collision_asset_is_not_a_blocker():
    # Live at Costa del Sol: the steps up to Dokaccha's terrace.
    asset = "CollisionAssetActor /Game/Level/Game/EndGame.EndGame:PersistentLevel.CollisionAssetActor_1"
    inside = hit(asset, component="CollisionAssetComponent0 x", normal_z=0.3)
    inside.update(start_penetrating=True, mobility=0, impact={"x": 900.0, "y": 0.0, "z": 60.0})
    assert unknown_obstacle_hits(sweep(inside)) == []
    assert len(unknown_obstacle_hits(sweep({**inside, "start_penetrating": False}))) == 1
    assert len(unknown_obstacle_hits(sweep({**inside, "mobility": 2}))) == 1


def test_bounds_become_a_capsule_along_the_long_axis():
    obstacle = obstacle_from_bounds(TABLE, TABLE_BOUNDS, sequence=1, t=0.0)
    assert obstacle.shape == "capsule" and obstacle.heading_degrees == 0.0
    assert obstacle.radius_m == pytest.approx(1.35) and obstacle.half_length_m == pytest.approx(0.25)
    assert obstacle.obstacle_id == obstacle_from_bounds(TABLE, TABLE_BOUNDS, sequence=9, t=5.0).obstacle_id


TENT = "BG1216_00_Tent_CSMOA_C /Game/Level/Game/Field/2500-CSMOA/Layout/2500-CSMOA_Gimmick.2500-CSMOA_Gimmick:PersistentLevel.BG1216_00_Tent_CSMOA_01"
TENT_BOUNDS = {"origin": {"x": -173232.8, "y": 49964.1, "z": 22635.6}, "extent": {"x": 255.7, "y": 258.9, "z": 258.8}}


def test_a_large_actor_is_routed_round_where_it_was_touched_not_as_its_box():
    # Live 2026-10-06 at Cosmo Canyon: the tent's 5.1 x 5.2 m box sealed the
    # walkway beside it and every route to Robin and Ka'dina failed.
    touched = {**hit(TENT, bounds=TENT_BOUNDS), "impact": {"x": -173473.0, "y": 50194.0, "z": 22463.0}}
    obstacle = obstacle_for_hit(touched, sequence=1, t=0.0)
    assert obstacle.provenance == ("route_sweep_contact",) and obstacle.radius_m == pytest.approx(0.2)
    assert (obstacle.center.x, obstacle.center.y) == (-173473.0, 50194.0)
    assert obstacle_key(touched).startswith("spot:")
    elsewhere = {**touched, "impact": {"x": -173100.0, "y": 50300.0, "z": 22463.0}}
    assert obstacle_key(elsewhere) != obstacle_key(touched)
    # A table set is still its box.
    assert obstacle_key(hit(TABLE, bounds=TABLE_BOUNDS)) == "actor:" + TABLE


def test_replans_around_a_table_then_accepts_the_clear_route():
    adapter = Adapter([sweep(hit(TABLE, bounds=TABLE_BOUNDS)), sweep()])
    detour = plan(0, 300, 700)
    result = audit(adapter, plan(0, 1000), [detour])
    assert result is detour
    assert len(adapter.static_obstacles.planning_snapshot().obstacles) == 1
    event = adapter.timeline[-1]
    assert event["outcome"] == "clear" and len(event["rounds"]) == 2
    assert event["rounds"][0]["unknown_obstacles"][0]["key"] == "actor:" + TABLE


def test_a_clear_route_is_swept_once_and_kept():
    adapter = Adapter([sweep(hit(BAKED, component="CollisionAssetComponent x", normal_z=0.9))])
    original = plan(0, 1000)
    assert audit(adapter, original, []) is original
    assert len(adapter.swept) == 1


def test_an_obstacle_hit_again_is_grown():
    adapter = Adapter([sweep(hit(TABLE, bounds=TABLE_BOUNDS)), sweep(hit(TABLE, bounds=TABLE_BOUNDS)), sweep()])
    audit(adapter, plan(0, 1000), [plan(0, 400, 1000), plan(0, 500, 1000)])
    (obstacle,) = adapter.static_obstacles.planning_snapshot().obstacles
    assert obstacle.radius_m == pytest.approx(1.6)


def test_fails_before_movement_when_the_route_cannot_be_cleared():
    rounds = MAX_ROUTE_AUDIT_ROUNDS
    adapter = Adapter([sweep(hit(TABLE, bounds=TABLE_BOUNDS))] * rounds)
    with pytest.raises(AdapterError, match="route_collision_audit_unresolved"):
        audit(adapter, plan(0, 1000), [plan(0, 1000)] * rounds)
    assert adapter.timeline[-1]["outcome"] == "unresolved"


def test_townspeople_and_doors_on_the_route_do_not_block_it():
    # Live at Nibelheim: three townspeople and the Town Hall door used up
    # every replan on the way to Turner.
    citizen = hit("MM1400_03_MidgarPlateCitizen03_C x", component="EndCharacterRootComponent x")
    citizen["impact"] = {"x": 500.0, "y": 0.0, "z": 60.0}
    door = hit("BG0007_00_Door2Bar_Standard_C /Game/Level/Game/Field/2610-NIBLT/Layout/x", bounds=TABLE_BOUNDS,
               component="EndSkeletalMeshComponent x")
    assert classify_hit(door) == "door"
    adapter = Adapter([sweep(citizen, door)])
    first = plan(0, 1000)
    assert audit(adapter, first, []) is first
    assert adapter.timeline[-1]["outcome"] == "clear"
    assert [item["actor"] for item in adapter.timeline[-1]["rounds"][0]["doors"]] == [door["actor"]]


def test_a_fixed_blocker_left_after_the_rounds_still_fails():
    table = hit(TABLE, bounds=TABLE_BOUNDS)
    rounds = MAX_ROUTE_AUDIT_ROUNDS
    adapter = Adapter([sweep(table) for _ in range(rounds + 1)])
    with pytest.raises(AdapterError, match="route_collision_audit_unresolved"):
        audit(adapter, plan(0, 1000), [plan(0, 1000)] * rounds)


def test_a_failed_sweep_fails_the_route():
    adapter = Adapter([])
    adapter.route_collision_audit = lambda points: (_ for _ in ()).throw(RuntimeError("route_sweep_timeout"))
    with pytest.raises(AdapterError, match="route_collision_audit_failed"):
        audit(adapter, plan(0, 1000), [])


def test_a_target_inside_an_obstacle_moves_back_along_the_route():
    # The NPC sits at the table: the target is 1 m from its centre.
    adapter = Adapter([sweep(hit(TABLE, bounds=TABLE_BOUNDS)), sweep()], target=Vec3(600.0, 0.0, 0.0))
    audit(adapter, plan(0, 600), [plan(0, 200)])
    moved = next(e for e in adapter.timeline if e["event"] == "route_target_moved_clear_of_obstacle")
    # Clear of the capsule (axis x 475..525, radius 1.35 m + 0.55 m padding) plus the margin.
    assert adapter.route_target.x == pytest.approx(475 - 190 - 15, abs=10)
    assert moved["original_target"]["x"] == 600.0


def test_a_recovery_moves_a_target_inside_a_known_obstacle_before_planning():
    # A fresh adapter recovering the route starts from the original target
    # while the overlay keeps the table learned by the first audit (live:
    # Ned, global_replan_unavailable).
    adapter = Adapter([], target=Vec3(600.0, 0.0, 0.0))
    adapter.static_obstacles.admit_obstacle(scope=adapter.static_obstacles.scope,
                                            obstacle=obstacle_from_bounds(TABLE, TABLE_BOUNDS, sequence=1, t=0.0))
    adapter.config = SimpleNamespace(cached_topology_max_expansions=1, cached_topology_max_waypoints=64,
                                     start_max_snap_m=0.75, start_max_vertical_m=1.0,
                                     portal_corner_inset_m=0.1, routing_objective="distance")
    graph = SimpleNamespace(plan=lambda start, end, **kw: SimpleNamespace(
        status="complete", waypoints=(start, end)))
    LiveRouteAdapter._clear_target_of_known_obstacles(adapter, SimpleNamespace(graph=graph), Vec3(0.0, 0.0, 0.0))
    assert adapter.route_target.x == pytest.approx(475 - 190 - 15, abs=10)


def test_no_known_obstacles_leaves_the_target_alone():
    adapter = Adapter([], target=Vec3(600.0, 0.0, 0.0))
    graph = SimpleNamespace(plan=lambda *a, **k: pytest.fail("planned"))
    LiveRouteAdapter._clear_target_of_known_obstacles(adapter, SimpleNamespace(graph=graph), Vec3(0.0, 0.0, 0.0))
    assert adapter.route_target.x == 600.0


class StallAdapter:
    _review_stall_guess = LiveRouteAdapter._review_stall_guess

    def __init__(self, kinds):
        self.config = SimpleNamespace(route_mode="cached_topology")
        self.static_obstacles = StaticObstacleOverlay()
        self.static_obstacles.bind_scope(StaticObstacleScope("session", "EndGame", "EndGame", "F" * 64))
        self.timeline, self.slept = [], []
        self.blocker_memory = None
        self.route_collision_audit = lambda points: {"segments": [{"index": 0, "hits": [{"kind": k} for k in kinds]}]}

    def monotonic(self):
        return 1.0

    def sleeper(self, seconds):
        self.slept.append(seconds)


def guessed(adapter, x=500.0):
    from ff7r_agent.static_obstacles import StaticObstacle
    obstacle = StaticObstacle(obstacle_id=f"static-{int(x)}", center=Vec3(x, 0.0, 0.0), radius_m=0.55,
                              evidence_count=1)
    adapter.static_obstacles.admit_obstacle(scope=adapter.static_obstacles.scope, obstacle=obstacle)
    return {"obstacle": obstacle.to_dict()}


def test_a_stall_on_a_party_member_is_waited_out_not_learned():
    adapter = StallAdapter(["party"])
    adapter._review_stall_guess(SimpleNamespace(position=Vec3(0.0, 0.0, 0.0)), guessed(adapter))
    assert not adapter.static_obstacles.planning_snapshot().obstacles
    assert adapter.slept == [pytest.approx(1.5)]
    assert adapter.timeline[-1]["decision"] == "discarded_guess_waited_for_character"


def test_a_stall_guess_is_left_to_the_audit_then_kept_when_it_repeats():
    adapter = StallAdapter(["fixed"])
    for expected in ("discarded_guess_for_audit", "discarded_guess_for_audit", "kept_guess_repeated_stall"):
        adapter._review_stall_guess(SimpleNamespace(position=Vec3(0.0, 0.0, 0.0)), guessed(adapter))
        assert adapter.timeline[-1]["decision"] == expected
    assert len(adapter.static_obstacles.planning_snapshot().obstacles) == 1


def test_an_obstacle_padded_over_the_start_is_shrunk_to_clear_it():
    from ff7r_agent.static_obstacles import StaticObstacle, _clear_of_start, obstacle_segment_clearance_m
    table = StaticObstacle(obstacle_id="t", center=Vec3(100.0, 0.0, 0.0), radius_m=1.35, evidence_count=1)
    start = Vec3(0.0, 0.0, 0.0)  # 1 m from the axis, inside 1.35 m + padding
    shrunk = _clear_of_start(start, table)
    assert obstacle_segment_clearance_m(start, start, shrunk) > 0
    assert shrunk.radius_m == pytest.approx(0.9)
    far = Vec3(-500.0, 0.0, 0.0)
    assert _clear_of_start(far, table) is table


def test_a_stall_nothing_explains_places_a_blocker_ahead_and_replans():
    adapter = StallAdapter([])
    adapter.route_collision_audit = lambda points: {"segments": []}
    adapter._replan_around_invisible_blocker = LiveRouteAdapter._replan_around_invisible_blocker.__get__(adapter)
    replans = []
    adapter._replan_for_static_obstacle = lambda observation: replans.append(adapter._static_pending_discovery) or "fresh"
    observation = SimpleNamespace(position=Vec3(0.0, 0.0, 0.0), sequence=7, monotonic_t=1.0, actor_yaw=0.0)
    assert adapter._replan_around_invisible_blocker(observation, {"desired_heading_deg": 90.0}) == "fresh"
    (obstacle,) = adapter.static_obstacles.planning_snapshot().obstacles
    assert obstacle.obstacle_id.startswith("stall-")
    assert obstacle.center.y == pytest.approx(60.0) and abs(obstacle.center.x) < 1e-6
    for _ in range(2):
        adapter._replan_around_invisible_blocker(observation, {"desired_heading_deg": 90.0})
    assert adapter._replan_around_invisible_blocker(observation, {"desired_heading_deg": 90.0}) is None
    assert len(replans) == 3


def test_invisible_blockers_are_remembered_briefly_across_routes():
    from ff7r_agent.static_obstacles import RecentBlockers, StaticObstacle
    now = [0.0]
    memory = RecentBlockers(ttl_seconds=600, clock=lambda: now[0])
    wagon = StaticObstacle(obstacle_id="stall-1", center=Vec3(0.0, 0.0, 0.0), radius_m=0.5, evidence_count=1)
    memory.remember(wagon)
    assert [o.obstacle_id for o in memory.active()] == ["stall-1"]
    adapter = StallAdapter([])
    adapter.blocker_memory = memory
    LiveRouteAdapter._readmit_remembered_blockers(adapter)
    assert [o.obstacle_id for o in adapter.static_obstacles.planning_snapshot().obstacles] == ["stall-1"]
    now[0] = 601.0
    assert memory.active() == []


class PlanningAdapter:
    """The attributes _plan_with_static_overlay uses."""

    _plan_with_static_overlay = LiveRouteAdapter._plan_with_static_overlay
    _static_overlay_plan_key = LiveRouteAdapter._static_overlay_plan_key

    def __init__(self):
        self.config = SimpleNamespace(route_mode="cached_topology", portal_corner_inset_m=0.5,
                                      routing_objective="distance")
        self.static_obstacles = StaticObstacleOverlay()
        self.static_obstacles.bind_scope(StaticObstacleScope("session", "EndGame", "EndGame", "F" * 64))
        self.timeline = []
        self._static_overlay_plan_memo = None

    def monotonic(self):
        return 1.0


def test_an_unchanged_overlay_replan_reuses_the_audited_plan(monkeypatch):
    # Live at Kalm the preflight re-planned the plan the audit had just
    # accepted, from the same start, target and overlay: 32 s of grid detour.
    from ff7r_agent import live_route_adapter
    from ff7r_agent.detour_route import DetourTopologyGraph
    from test_topology_cache import topology_payload

    planned = []
    real = live_route_adapter.plan_with_static_obstacles

    def counted(*args, **kwargs):
        planned.append(args[1:3])
        return real(*args, **kwargs)

    monkeypatch.setattr(live_route_adapter, "plan_with_static_obstacles", counted)
    adapter = PlanningAdapter()
    topology = SimpleNamespace(graph=DetourTopologyGraph.from_dict(topology_payload()),
                               session_id="session", fingerprint="F" * 64)
    start, end = Vec3(-800.0, 0.0, 0.0), Vec3(800.0, 0.0, 0.0)
    kwargs = {"max_expansions": 500_000, "max_waypoints": 512, "max_snap_distance_m": 0.75,
              "max_vertical_separation_m": 1.0}
    adapter.static_obstacles.admit_obstacle(scope=adapter.static_obstacles.scope, obstacle=obstacle_for_hit(
        {**hit(TABLE, bounds=TABLE_BOUNDS), "impact": {"x": 0.0, "y": 0.0, "z": 0.0}}, sequence=1, t=1.0))

    first = adapter._plan_with_static_overlay(topology, start, end, **kwargs)
    # The preflight passes max_tile_crossings=None, the planner's default.
    again = adapter._plan_with_static_overlay(topology, start, end, max_tile_crossings=None, **kwargs)
    assert first.status == "complete"
    assert again is first and len(planned) == 1
    assert [e["event"] for e in adapter.timeline] == ["static_overlay_plan_reused"]

    # Anything the plan depends on plans afresh.
    adapter._plan_with_static_overlay(topology, Vec3(-790.0, 0.0, 0.0), end, **kwargs)
    adapter._plan_with_static_overlay(topology, start, end, **{**kwargs, "max_waypoints": 256})
    adapter.static_obstacles.admit_obstacle(scope=adapter.static_obstacles.scope, obstacle=obstacle_for_hit(
        {**hit(TABLE, bounds=TABLE_BOUNDS), "impact": {"x": 300.0, "y": 500.0, "z": 0.0}}, sequence=2, t=1.0))
    adapter._plan_with_static_overlay(topology, start, end, **kwargs)
    other_graph = SimpleNamespace(graph=DetourTopologyGraph.from_dict(topology_payload()),
                                  session_id="session", fingerprint="F" * 64)
    adapter._plan_with_static_overlay(other_graph, start, end, **kwargs)
    assert len(planned) == 5


def test_a_stall_at_a_door_hands_the_route_back_to_open_it():
    adapter = StallAdapter(["door"])
    decision = adapter._review_stall_guess(SimpleNamespace(position=Vec3(0.0, 0.0, 0.0)), guessed(adapter))
    assert decision == "door_ahead" and adapter.slept == []
    assert not adapter.static_obstacles.planning_snapshot().obstacles


def test_a_second_stall_on_the_same_person_routes_round_them():
    adapter = StallAdapter(["npc"])
    person = {"kind": "npc", "actor": "MB2300_00_Patron_C x", "component": "EndCharacterRootComponent x",
              "impact": {"x": 480.0, "y": 0.0, "z": 60.0}}
    adapter.route_collision_audit = lambda points: {"segments": [{"index": 0, "hits": [person]}]}
    observation = SimpleNamespace(position=Vec3(0.0, 0.0, 0.0), sequence=7)
    assert adapter._review_stall_guess(observation, guessed(adapter)) == "discarded_guess_waited_for_character"
    assert not adapter.static_obstacles.planning_snapshot().obstacles
    assert adapter._review_stall_guess(observation, guessed(adapter)) == "routed_round_character"
    (kept,) = adapter.static_obstacles.planning_snapshot().obstacles
    assert kept.center.x == 480.0 and kept.radius_m == pytest.approx(0.2)


def test_a_segway_is_a_vehicle_to_route_round_not_a_person_to_push_past():
    segway = hit("VE0025_02_RentalBike_VariationB_C x", bounds=TABLE_BOUNDS, component="EndCharacterRootComponent x")
    assert classify_hit(segway) == "vehicle"
    assert len(unknown_obstacle_hits(sweep(segway))) == 1
