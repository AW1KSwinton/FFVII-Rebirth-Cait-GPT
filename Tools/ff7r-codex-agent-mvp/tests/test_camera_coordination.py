"""Camera/movement ordering with delayed, physically applied camera response."""
from dataclasses import replace
import math
import threading
from types import SimpleNamespace
import pytest

from ff7r_agent.camera_settling import CameraYawSettling, CAMERA_DELTA_PER_DEGREE
from ff7r_agent.live_route_adapter import LiveAdapterConfig, AdapterState
from ff7r_agent.models import Vec3
from test_live_route_adapter import FakeClock, FakeActuator, make_adapter, make_observation


class CameraWorld:
    def __init__(self, clock, actuator, *, initial_yaw=90, failure=None, lag=2, smooth=False):
        self.smooth, self.streams = smooth, []
        self.clock, self.actuator = clock, actuator
        self.yaw = initial_yaw
        self.position = Vec3(0, 0, 0)
        self.sequence = 0
        self.pending = []
        self.commands = []
        self.failure, self.lag = failure, lag
        self.after_camera = False
        self.traces = []

    def observe(self):
        self.clock.value += .1
        self.sequence += 1
        if self.pending:
            next_yaw = self.pending.pop(0)
            if abs(next_yaw - self.yaw) > 1e-8 and not self.smooth:
                assert self.actuator.is_neutral(), "old movement remained held during camera rotation"
            self.yaw = next_yaw
        if not self.actuator.neutral and getattr(self.actuator, 'updates', None):
            command = self.actuator.updates[-1]
            x, y = command['x'], command['y']
            rad = math.radians(self.yaw)
            dx, dy = y*math.cos(rad)-x*math.sin(rad), y*math.sin(rad)+x*math.cos(rad)
            self.traces.append((dx, dy, self.yaw, x, y))
            self.position = Vec3(self.position.x + dx*40, self.position.y + dy*40, 0)
        changes = {}
        if self.after_camera:
            if self.failure == "readiness": changes['controllable'] = False
            if self.failure == "traversal" and self.clock() < self.camera_t + 3.5:
                changes['controllable'] = False  # the game moves Cloud a while
            if self.failure == "identity": changes['session_id'] = 'different-run'
            if self.failure == "stale": changes['telemetry_age_seconds'] = 10
        return make_observation(sequence=self.sequence, monotonic_t=self.clock(),
            position=self.position, camera_yaw=self.yaw, actuator_neutral=self.actuator.neutral,
            grounded=True, velocity_cm_s=Vec3(0, 0, 0), **changes)

    def stream_horizontal(self, delta, milliseconds, should_stop=None):
        # A sweep lands over the next two samples while Cloud keeps moving.
        self.streams.append((delta, milliseconds))
        change = delta / CAMERA_DELTA_PER_DEGREE
        base = self.pending[-1] if self.pending else self.yaw
        self.pending = [base + change * .5, base + change]
        return {"sent_delta": delta}

    def command_horizontal(self, delta):
        assert self.actuator.is_neutral()
        if not self.after_camera:
            self.camera_t = self.clock()
        self.after_camera = True
        self.commands.append(delta)
        change = delta / CAMERA_DELTA_PER_DEGREE
        self.pending = [self.yaw] * self.lag + [self.yaw + change * p for p in (.3, .7, 1, 1, 1, 1, 1)]
        if self.failure == "actuator":
            raise RuntimeError("partial camera send")
        return {"accepted": True}

    def neutralize(self):
        return {"neutral": True}


def coupled_adapter(*, continuous=False, sprint=False, smooth=False, arrival_target=None, **options):
    from test_continuous_control import StreamingActuator
    clock = FakeClock()
    actuator = StreamingActuator() if continuous else FakeActuator()
    world = CameraWorld(clock, actuator, smooth=smooth, **options)
    adapter, _, _, _ = make_adapter(telemetry=world, actuator=actuator, clock=clock,
        config=LiveAdapterConfig(camera_follow_enabled=True, post_arrival_samples=0,
            controller_mode="continuous" if continuous else "pulse",
            normal_stick_magnitude=.8 if continuous else 1.0, sprint=sprint,
            camera_follow_smooth=smooth, camera_follow_arrival_target=arrival_target))
    adapter.camera_actuator = world
    original = actuator.command
    def move(x, y, milliseconds):
        assert smooth or not world.pending or all(abs(v-world.yaw)<1e-6 for v in world.pending)
        angle = math.radians(world.yaw)
        dx, dy = y*math.cos(angle)-x*math.sin(angle), y*math.sin(angle)+x*math.cos(angle)
        world.traces.append((dx, dy, world.yaw, x, y))
        world.position = Vec3(world.position.x + dx * 10, world.position.y + dy * 10, 0)
        return original(x, y, milliseconds)
    actuator.command = move
    assert adapter.preflight()["movement_authorized"]
    if sprint:
        callback = adapter.follower.on_telemetry
        def follower(sample):
            result = callback(sample)
            for event in result:
                event.update(distance_m=20, distance_to_final_destination_m=30)
            return result
        adapter.follower.on_telemetry = follower
    return adapter, world, actuator


def test_straight_world_trajectory_survives_substantial_camera_rotation():
    adapter, world, actuator = coupled_adapter()
    adapter.run(dry_run=False, max_cycles=5)
    assert len(world.traces) == 5
    assert abs(world.traces[-1][2] - world.traces[0][2]) > 40
    assert all(dx > 0 and abs(dy) < 1e-12 for dx, dy, *_ in world.traces)
    assert world.traces[0][3:] != world.traces[-1][3:]
    assert all(abs(d) <= 500 for d in world.commands)
    assert actuator.is_neutral()


def test_arrival_camera_focus_preserves_the_authorized_world_movement_direction():
    adapter, world, actuator = coupled_adapter(arrival_target=(350.0, 200.0, 0.0))
    adapter.run(dry_run=False, max_cycles=5)
    assert world.commands
    assert all(dx > 0 and abs(dy) < 1e-12 for dx, dy, *_ in world.traces)
    assert any(e.get('arrival_focus') for e in adapter.timeline if e.get('event') == 'camera_follow')
    assert actuator.is_neutral()


def test_superseded_camera_turn_stays_cancelled_when_its_native_send_returns_late():
    started, unblock = threading.Event(), threading.Event()
    callbacks, late_input = [], []
    def stream(delta, milliseconds, should_stop):
        callbacks.append(should_stop)
        if len(callbacks) == 1:
            started.set()
            assert unblock.wait(2)
            if not should_stop():
                late_input.append(delta)
        return {'accepted': True}
    adapter, _, _, _ = make_adapter()
    adapter.camera_actuator = SimpleNamespace(stream_horizontal=stream)
    old = None
    try:
        adapter._start_camera_stream(10, 300)
        assert started.wait(1)
        old = adapter._camera_stream_thread
        adapter._start_camera_stream(-10, 300)
        assert callbacks[0]() is True
        unblock.set()
        old.join(1)
        assert not old.is_alive() and late_input == []
    finally:
        unblock.set()
        if old:
            old.join(1)
        adapter._finish_camera_stream(stop=True)


@pytest.mark.parametrize("failure", ["readiness", "identity", "stale"])
def test_camera_interruption_cannot_authorize_movement(failure):
    adapter, world, actuator = coupled_adapter(failure=failure)
    result = adapter.run(dry_run=False, max_cycles=1)
    assert world.commands
    assert not actuator.commands
    assert actuator.is_neutral()
    assert result["state"] in (AdapterState.STOPPED.value, AdapterState.REPLAN_REQUIRED.value)


def test_control_returning_after_the_settle_window_continues_the_route():
    # Live: not controllable while the game carried Cloud up steps, longer
    # than the camera settle window; the route went on once control was back.
    adapter, world, actuator = coupled_adapter(failure="traversal")
    adapter.run(dry_run=False, max_cycles=1)
    assert any(item.get("event") == "camera_follow_fallback_control_regained" for item in adapter.timeline)
    assert actuator.commands and adapter._camera_follow_runtime_disabled
    assert actuator.is_neutral()


def test_readiness_flicker_reacquires_settled_proof_before_movement():
    adapter, world, actuator = coupled_adapter(failure="readiness")
    settle_calls = []

    def settle(timeout_seconds, *, max_telemetry_age_seconds=None):
        settle_calls.append((timeout_seconds, max_telemetry_age_seconds))
        world.failure = None
        return {"ok": True, "sequence": world.sequence}

    world.wait_for_settled_gameplay = settle
    result = adapter.run(dry_run=False, max_cycles=1)
    assert settle_calls
    assert world.commands
    assert actuator.commands
    assert not adapter._camera_follow_runtime_disabled
    assert any(item.get("event") == "camera_settle_readiness_recovery"
               for item in adapter.timeline)
    assert actuator.is_neutral()


def test_partial_actuator_failure_resumes_only_after_fresh_compensated_yaw():
    adapter, world, actuator = coupled_adapter(failure="actuator")
    adapter.run(dry_run=False, max_cycles=2)
    assert len(world.commands) == 1
    assert adapter._camera_follow_runtime_disabled
    assert len(actuator.commands) == 2
    assert all(dx > 0 and abs(dy) < 1e-12 for dx, dy, *_ in world.traces)


def test_deadband_does_not_interrupt_travel():
    adapter, world, actuator = coupled_adapter(initial_yaw=5)
    before = actuator.neutralize_count
    adapter.run(dry_run=False, max_cycles=2)
    assert world.commands == []
    assert len(actuator.commands) == 2
    assert not any(e['event'] == 'follower_paused_for_camera_adjustment' for e in adapter.timeline)


def test_yaw_tracker_rejects_lag_duplicates_and_unsettled_envelope():
    tracker = CameraYawSettling(0, 1)
    assert not tracker.observe(yaw=0, sequence=2, now=1)
    assert not tracker.observe(yaw=30, sequence=2, now=2)
    assert not tracker.observe(yaw=20, sequence=3, now=2)
    assert not tracker.observe(yaw=21, sequence=4, now=2.2)
    assert not tracker.observe(yaw=21.05, sequence=5, now=2.3)
    assert tracker.observe(yaw=21.02, sequence=6, now=2.6)


def test_yaw_tracker_wraparound_is_continuous():
    tracker = CameraYawSettling(170, 1)
    assert not tracker.observe(yaw=179.99, sequence=2, now=1)
    assert not tracker.observe(yaw=-179.99, sequence=3, now=1.1)
    assert tracker.observe(yaw=179.99, sequence=4, now=1.3)


@pytest.mark.parametrize("sprint", [False, True])
def test_continuous_camera_coordination_keeps_supervised_world_direction(sprint):
    adapter, world, actuator = coupled_adapter(continuous=True, sprint=sprint)
    result = adapter.run(dry_run=False, max_cycles=5)
    assert len(actuator.updates) == 5, result['reason']
    commands = [e for e in result['timeline'] if e.get('event') == 'non_neutral_command_imminent']
    assert len(commands) == 5
    for event in commands:
        yaw = math.radians(event['camera_yaw'])
        x, y = event['stick_x'], event['stick_y']
        assert abs(y * math.sin(yaw) + x * math.cos(yaw)) < 1e-12
        assert y * math.cos(yaw) - x * math.sin(yaw) > 0
    if sprint:
        assert all(u['sprint'] for u in actuator.updates)
    assert actuator.neutral


@pytest.mark.parametrize('angles', [(0, 15, 30), (0, 45), (0, 90), (0, 45, 0, -45, 0)])
@pytest.mark.parametrize('sprint', [False, True])
def test_compensation_tracks_authorized_bends_and_s_curve(angles, sprint):
    from ff7r_agent.camera_follow import CameraFollowController, CameraFollowConfig
    from ff7r_agent.live_route_adapter import world_heading_to_camera_stick
    route = [Vec3(0, 0, 0)]
    for angle in angles:
        rad = math.radians(angle)
        route.append(Vec3(route[-1].x + 1000*math.cos(rad), route[-1].y + 1000*math.sin(rad), 0))
    controller = CameraFollowController(CameraFollowConfig(camera_follow_enabled=True))
    yaw = -90
    for i, angle in enumerate(angles):
        a, b = route[i], route[i+1]
        for fraction in (0, .25, .5, .75, .95):
            position = Vec3(a.x + fraction*(b.x-a.x), a.y + fraction*(b.y-a.y), 0)
            result = controller.command(route=route, position=position, camera_yaw_deg=yaw, sprint=sprint)
            yaw += max(-500, min(500, round(result['camera_input_x']*90*CAMERA_DELTA_PER_DEGREE*.75))) / CAMERA_DELTA_PER_DEGREE
            magnitude = 1.0 if sprint else .8
            x, y = world_heading_to_camera_stick(angle, yaw, magnitude)
            rad = math.radians(yaw)
            assert y*math.cos(rad)-x*math.sin(rad) == pytest.approx(magnitude*math.cos(math.radians(angle)))
            assert y*math.sin(rad)+x*math.cos(rad) == pytest.approx(magnitude*math.sin(math.radians(angle)))


def test_smooth_camera_turns_while_moving_without_stopping():
    adapter, world, actuator = coupled_adapter(smooth=True, initial_yaw=150)
    adapter._finish_camera_stream()
    adapter.run(dry_run=False, max_cycles=8)
    adapter._finish_camera_stream()
    # Every cycle moves; the camera is turned during the commands, never snapped.
    assert len(actuator.commands) == 8 and world.streams and not world.commands
    assert not any(e["event"] == "follower_paused_for_camera_adjustment" for e in adapter.timeline)
    turns = [e["turn_degrees"] for e in adapter.timeline if e["event"] == "camera_follow_smooth_turn"]
    assert all(abs(t) <= 60.0 * 0.3 + 1e-6 for t in turns)
    # The camera converges on the route heading (0 degrees) instead of jumping.
    assert abs(world.yaw) < 150 - 30
    # Aiming the stick against the mid-turn camera keeps the path near the
    # route heading while the camera swings.
    assert all(abs(math.degrees(math.atan2(dy, dx))) <= 12.0 for dx, dy, *_ in world.traces)


def test_smooth_camera_leaves_an_aligned_camera_alone():
    adapter, world, actuator = coupled_adapter(smooth=True, initial_yaw=1)
    adapter.run(dry_run=False, max_cycles=3)
    assert not world.streams and len(actuator.commands) == 3


def test_supervised_holds_are_sent_before_the_camera_sweep_starts():
    # Live (fast follow): waiting on the previous sweep before a supervised
    # hold let its deadline expire (supervised_deadline_invalid_or_expired).
    adapter, world, actuator = coupled_adapter(smooth=True, continuous=True, initial_yaw=150)
    order = []
    stream = world.stream_horizontal
    supervised = actuator.supervised_command

    def recording_stream(*args, **kwargs):
        order.append("camera")
        return stream(*args, **kwargs)

    def recording_supervised(*args, **kwargs):
        order.append("stick")
        return supervised(*args, **kwargs)

    world.stream_horizontal = recording_stream
    actuator.supervised_command = recording_supervised
    adapter.run(dry_run=False, max_cycles=6)
    adapter._finish_camera_stream()
    pairs = [(a, b) for a, b in zip(order, order[1:]) if b == "camera"]
    assert pairs and all(a == "stick" for a, _ in pairs)
