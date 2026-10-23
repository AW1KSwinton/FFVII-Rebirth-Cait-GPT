"""Recorded girder positions and a movement-free, synthetic controller replay.

The positions fixture preserves live coordinates. The physics, readiness,
camera changes and 0.12 s clock below are synthetic, not a new live crossing.
"""

import json
import math

import pytest

from ff7r_agent.continuous_control import SupervisedStickLease
from ff7r_agent.detour_route import DetourTopologyGraph
from ff7r_agent.live_route_adapter import (
    AdapterState, IndependentJsonValidator, LiveAdapterConfig, LiveRouteAdapter,
    TopologySession, load_route_follower_factory,
)
from ff7r_agent.models import Vec3
from ff7r_agent.traversal_crossing import TraversalCrossing, along_and_lateral_m, has_landed
from offmesh_fixtures import GIRDER_FIXTURE, GIRDER_GOAL, GIRDER_START, girder_payload
from test_continuous_control import StreamingActuator
from test_live_route_adapter import FakeClock, FakeRenderEvidence, FakeTopologyProvider, make_observation


REPLAY = json.loads((GIRDER_FIXTURE.parent / "girder-crossing-replay.json").read_text(encoding="utf-8"))


def test_recorded_moving_alignment_is_not_promoted_to_a_settled_departure():
    run = REPLAY["runs"]["girder-own-hold-live"]
    samples = run["samples"]
    clock = FakeClock()
    index = 0
    commands, stops, events = [], [], []
    def sample():
        item = samples[min(index, len(samples) - 1)]
        return make_observation(sequence=item["sequence"], monotonic_t=clock.value,
                                position=Vec3(**item["position"]), grounded=True)
    def next_sample(after):
        nonlocal index
        index += 1
        clock.value += 0.12
        if index >= len(samples):
            raise RuntimeError("recorded_trace_exhausted")
        return sample()
    from ff7r_agent.traversal_crossing import CrossingTarget
    target = dict(run["target"])
    target.pop("horizontal_length_m")
    target.pop("vertical_delta_m")
    target["start"], target["end"] = Vec3(**target["start"]), Vec3(**target["end"])
    crossing = TraversalCrossing(monotonic=clock, next_observation=next_sample, readiness=lambda _: [],
        push=lambda heading, obs: commands.append(obs.sequence), align_push=lambda *_: None,
        neutralize=lambda: stops.append(sample().sequence), record=events.append)
    result = crossing.cross(CrossingTarget(**target), sample())
    assert not result.crossed and not run["observed_crossed"]
    assert run["aligned"]["sequence"] in stops
    assert not result.evidence["departure_aligned"] and not commands
    assert not any(e["event"] == "traversal_departure_aligned" for e in events)


def test_recorded_pursuit_proximity_requires_one_more_landing_polygon_sample():
    run = REPLAY["runs"]["girder-aligned-pursuit"]
    graph = DetourTopologyGraph.from_dict(girder_payload())
    plan = graph.plan(Vec3(*GIRDER_START), Vec3(*GIRDER_GOAL)).to_dict()
    # Use the normal adapter setup for its route sample cache and geometry.
    from test_traversal_adapter import bare_adapter
    adapter = bare_adapter()
    adapter.route_plan = plan
    adapter.authorization_proof = None
    adapter.topology_session = TopologySession(graph=graph, artifact_path=str(GIRDER_FIXTURE),
        fingerprint="girder", session_id="run-1", world_id="EndGame", captured_at_utc="synthetic",
        current_session=True, movement_sent=False)
    position = Vec3(**run["samples"][-1]["position"])
    from ff7r_agent.traversal_crossing import CrossingTarget
    segment = plan["traversal_segments"][0]
    target = CrossingTarget(link_class=segment["link_class"], link_ref=segment["link_ref"],
        from_ref=segment["from_ref"], to_ref=segment["to_ref"], start=Vec3(**segment["start"]),
        end=Vec3(**segment["end"]))
    assert run["observed_crossed"] and has_landed(target, position)[0]
    assert not adapter._traversal_polygon_proven(make_observation(position=position), target.to_ref)
    assert adapter._traversal_polygon_proven(make_observation(sequence=2, position=target.end), target.to_ref)


class GirderWorld:
    """Synthetic sampled motion; a beam requires a settled entering turn.

    That entry condition is a regression model, not an assertion about the
    game's animation trigger. The exact graph and follower are production.
    """

    def __init__(self):
        self.graph = DetourTopologyGraph.from_dict(girder_payload())
        plan = self.graph.plan(Vec3(*GIRDER_START), Vec3(*GIRDER_GOAL)).to_dict()
        segment = plan["traversal_segments"][0]
        self.start, self.end = Vec3(**segment["start"]), Vec3(**segment["end"])
        self.position = Vec3(4549.722168, 66804.34375, -107.811005)
        self.velocity = Vec3(0.0, 0.0, 0.0)
        self.clock, self.sequence = FakeClock(), 0
        self.camera_yaw = 67.0
        self.heading, self.magnitude = 0.0, 0.0
        self.entered = False
        self.blocked = False
        self.animation_started = None
        self.airborne = False
        self.animation_samples = []
        self.actuator = GirderActuator(self)

    def advance(self, dt):
        self.clock.value += dt
        if self.actuator.lease.expired(self.clock.value):
            self.heading, self.magnitude = 0.0, 0.0
            self.actuator.neutral = True
        speed = 400.0 * self.magnitude
        dx, dy = math.cos(math.radians(self.heading)) * speed * dt, math.sin(math.radians(self.heading)) * speed * dt
        self.position = Vec3(self.position.x + dx, self.position.y + dy, self.position.z)
        self.velocity = Vec3(dx / dt, dy / dt, 0.0)
        along, _ = along_and_lateral_m(self.start, self.end, self.position)
        if self.blocked and along >= 0.4:
            self.position = Vec3(4455.0, 66930.0, -102.802612)
            self.velocity = Vec3(0.0, 0.0, 0.0)
        if self.entered and self.animation_started is None and along >= 0.35:
            self.animation_started = self.clock.value
        self.airborne = (self.animation_started is not None
                         and self.clock.value - self.animation_started < 0.6)
        floor = self.graph.resolve_point(self.position, max_snap_distance_m=2., max_vertical_separation_m=2.)
        z = floor.closest_point.z if floor.closest_point is not None else self.end.z
        self.position = Vec3(self.position.x, self.position.y, z + (75.0 if self.airborne else -3.0))
        if self.airborne:
            self.camera_yaw += 9.0

    def observe(self):
        self.advance(0.12)
        self.sequence += 1
        observation = make_observation(sequence=self.sequence, monotonic_t=self.clock.value,
            position=self.position, camera_yaw=self.camera_yaw, actor_yaw=self.heading,
            velocity_cm_s=self.velocity, grounded=not self.airborne, controllable=not self.airborne,
            actuator_neutral=self.actuator.neutral)
        if self.airborne:
            self.animation_samples.append(observation)
        return observation


class GirderActuator(StreamingActuator):
    def __init__(self, world):
        super().__init__()
        self.world = world
        self.lease = SupervisedStickLease()

    def set_vector(self, x, y):
        self.world.heading = math.degrees(math.atan2(x, y)) + self.world.camera_yaw
        self.world.magnitude = math.hypot(x, y)

    def supervised_command(self, x, y, **kwargs):
        self.lease.accept(stream_id=kwargs["stream_id"], sequence=kwargs["sequence"],
                          deadline=kwargs["deadline"], now=self.world.clock.value)
        position, velocity = self.world.position, self.world.velocity
        if not self.world.entered and math.hypot(position.x - self.world.start.x,
                position.y - self.world.start.y) <= 30.0 and math.hypot(x, y) > 0.9:
            self.world.blocked = math.hypot(velocity.x, velocity.y) > 25.0
            self.world.entered = not self.world.blocked
        result = super().supervised_command(x, y, **kwargs)
        self.updates[-1].update(t=self.world.clock.value, position=position,
                                speed=math.hypot(velocity.x, velocity.y) / 100.0,
                                airborne=self.world.airborne)
        self.set_vector(x, y)
        return result

    def command(self, x, y, milliseconds):
        result = super().command(x, y, milliseconds)
        self.set_vector(x, y)
        # Pulse time is synthetic and synchronous, as in the real interface.
        self.world.advance(milliseconds / 1000.0)
        self.world.heading, self.world.magnitude = 0.0, 0.0
        return result

    def neutralize(self):
        self.lease.cancel()
        self.world.heading, self.world.magnitude = 0.0, 0.0
        return super().neutralize()


@pytest.mark.parametrize("pursuit", [False, True])
def test_real_girder_plan_validator_and_follower_cross_and_resume_offline(pursuit):
    world = GirderWorld()
    topology = TopologySession(graph=world.graph, artifact_path=str(GIRDER_FIXTURE), fingerprint="girder",
        session_id="run-1", world_id="EndGame", captured_at_utc="synthetic", current_session=True,
        movement_sent=False)
    adapter = LiveRouteAdapter(world, FakeTopologyProvider(topology), IndependentJsonValidator(GIRDER_FIXTURE),
        load_route_follower_factory(), world.actuator, render_evidence=FakeRenderEvidence(),
        config=LiveAdapterConfig(route_mode="cached_topology", pursuit=pursuit,
            controller_mode="continuous" if pursuit else "pulse", max_cycles=120, post_arrival_samples=0),
        route_target=Vec3(*GIRDER_GOAL), monotonic=world.clock,
        sleeper=lambda seconds: world.advance(seconds))
    try:
        preflight = adapter.preflight()
        assert preflight["movement_authorized"], preflight
        assert preflight["validator"]["valid"]
        assert preflight["adapter_route_plan_sha256"] == preflight["independent_validator_route_plan_sha256"]
        result = adapter.run(dry_run=False)
        assert result["state"] == AdapterState.ARRIVED.value, result
        events = result["timeline"]
        assert next(e for e in events if e["event"] == "traversal_crossing_finished")["crossed"]
        aligned = next(e for e in events if e["event"] == "traversal_departure_aligned")
        assert aligned["horizontal_speed_m_s"] <= 0.25
        assert world.entered and not world.blocked and world.animation_samples
        commands = [e for e in events if e["event"] == "traversal_command" and e["t"] >= aligned["t"]]
        # A step-over is crossed at a sprint: the game takes it by itself.
        assert commands and all(e["magnitude"] == 1.0 and e["sprint"] for e in commands)
        updates = [u for u in world.actuator.updates if commands[0]["t"] <= u["t"] <= commands[-1]["t"]]
        assert updates and all(not u["neutral_before"] for u in updates[1:])
        assert any(u["airborne"] for u in updates)
        air_commands = [e for e in commands if e["sequence"] in {o.sequence for o in world.animation_samples}]
        assert len(air_commands) >= 2
        assert len({round(e["camera_yaw"], 3) for e in air_commands}) >= 2
        assert max(e["heading_degrees"] for e in air_commands) - min(e["heading_degrees"] for e in air_commands) < 1e-6
        assert world.actuator.neutral
        assert adapter.route_plan["diagnostics"]["traversal"]["requires_live_verification"]
        if not pursuit:
            committed = next(e for e in events if e["event"] == "traversal_landing_committed")
            entry = adapter._route_traversals[0]
            assert committed["accepted_ref"] == entry["to_ref"]
            assert committed["follower_resume"]["landing_waypoint_index"] == entry["end_index"]
            assert adapter.follower.current_waypoint_index > entry["start_index"]
    finally:
        adapter.validator.close()


@pytest.mark.parametrize("change", ["undeclared", "reverse", "class", "nonconsecutive", "wrong_waypoint",
                                     "timestamp", "position", "nonfinite"])
def test_real_follower_refuses_an_unproven_handoff_without_advancing(change):
    plan = DetourTopologyGraph.from_dict(girder_payload()).plan(Vec3(*GIRDER_START), Vec3(*GIRDER_GOAL)).to_dict()
    segment = plan["traversal_segments"][0]
    start, end = segment["start_waypoint_index"], segment["end_waypoint_index"]
    if change == "undeclared":
        plan["traversal_segments"] = []
    elif change == "reverse":
        segment["direction"] = "end_to_start"
    elif change == "class":
        segment["link_class"] = "climb"
    follower = load_route_follower_factory()(plan)
    follower.on_telemetry({"t": 10., "position": plan["waypoints"][0], "heading": 0.})
    before = follower.current_waypoint_index
    sample = {"t": 11., "position": plan["waypoints"][end], "heading": 80.}
    if change == "nonconsecutive":
        start -= 1
    elif change == "wrong_waypoint":
        follower.current_waypoint_index = 0
        before = 0
    elif change == "timestamp":
        sample["t"] = 9.
    elif change == "position":
        sample["position"] = plan["waypoints"][start]
    elif change == "nonfinite":
        sample["heading"] = math.nan
    with pytest.raises(ValueError):
        follower.commit_traversal_landing(start, end, sample)
    assert follower.current_waypoint_index == before
