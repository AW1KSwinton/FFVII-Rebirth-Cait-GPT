from dataclasses import replace
import asyncio
import json
import math
import queue
import threading
import time

import pytest
from ff7r_agent import gamepad_broker
from ff7r_agent.continuous_control import SupervisedStickLease, movement_lease_seconds, CONTINUOUS_PROFILE
from ff7r_agent.controller_policy import (PROVEN_LIVE_CONTROLLER_POLICY, require_proven_live_controller_policy,
    proven_live_controller_policy_evidence, LiveControllerPolicyError)
from ff7r_agent.live_route_adapter import LiveAdapterConfig
from ff7r_agent.models import Vec3
from test_live_route_adapter import make_adapter, make_observation, FakeActuator, FakeClock
from test_navigation_mission import Runtime
from ff7r_agent.navigation_mission import NavigationMission, MissionConfig


def test_hold_lease_cannot_be_replayed_extended_by_neutral_or_changed_while_active():
    lease = SupervisedStickLease()
    lease.accept(stream_id='a', sequence=10, deadline=10.4, now=10)
    with pytest.raises(ValueError, match='not_advancing'):
        lease.accept(stream_id='a', sequence=10, deadline=10.5, now=10.1)
    with pytest.raises(ValueError, match='stream_changed'):
        lease.accept(stream_id='b', sequence=11, deadline=10.5, now=10.1)
    assert lease.expired(10.4)
    lease.cancel()
    with pytest.raises(ValueError, match='not_advancing'):
        lease.accept(stream_id='a', sequence=9, deadline=10.8, now=10.4)
    lease.accept(stream_id='b', sequence=1, deadline=10.8, now=10.4)


@pytest.mark.parametrize('deadline', [math.nan, math.inf, True, 10, 9, 10.501, '10.4'])
def test_hold_invalid_deadline_cannot_activate(deadline):
    lease = SupervisedStickLease()
    with pytest.raises(ValueError):
        lease.accept(stream_id='a', sequence=1, deadline=deadline, now=10)
    assert lease.deadline is None


def test_adaptive_lease_reserves_freshness_and_turn_braking():
    args = dict(effective_age=.1, freshness_limit=.5, distance_to_turn_m=10, speed_bound_m_s=8)
    assert movement_lease_seconds(**args) == pytest.approx(.375)
    assert movement_lease_seconds(**{**args, 'effective_age': .48}) == 0
    assert movement_lease_seconds(**{**args, 'distance_to_turn_m': .5}) == 0
    assert movement_lease_seconds(**{**args, 'distance_to_turn_m': 2}) < .1


class Pad:
    def __init__(self):
        self.vector = (0, 0)
        self.right_vector = (0, 0)
        self.right_reports = []
        self.trigger = 0.0
        self.buttons = set()
        self.trigger_reports = []
        self.button_reports = []
        self.reports = []
    def left_joystick_float(self, x, y):
        self.vector = (x, y)
    def right_joystick_float(self, x, y):
        self.right_vector = (x, y)
        self.right_reports.append(self.right_vector)
    def right_trigger_float(self, value):
        self.trigger = value
    def press_button(self, button):
        self.buttons.add(button)
    def release_button(self, button):
        self.buttons.discard(button)
    def update(self):
        self.trigger_reports.append((self.vector, self.trigger))
        self.button_reports.append((self.vector, self.trigger, set(self.buttons)))
        self.reports.append((time.monotonic(), self.vector))


@pytest.fixture
def broker(monkeypatch):
    ready = queue.Queue()
    owner = {'alive': True}
    pad = Pad()
    monkeypatch.setattr(gamepad_broker, 'owner_is_alive', lambda *_: owner['alive'])
    monkeypatch.setattr(gamepad_broker, 'print', lambda payload, **_: ready.put(json.loads(payload)), raising=False)
    thread = threading.Thread(target=gamepad_broker._serve_owned,
        args=(pad,), kwargs=dict(ttl_seconds=.5, owner_pid=1, owner_token='test'), daemon=True)
    thread.start()
    port = ready.get(timeout=2)['port']
    try:
        yield port, pad, owner
    finally:
        if thread.is_alive():
            gamepad_broker._exchange(port, {'op': 'shutdown'})
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert pad.vector == (0, 0)


def test_broker_holds_between_updates_but_heartbeats_cannot_renew_expiry(broker):
    port, pad, _ = broker
    proxy = gamepad_broker.RemoteVirtualLeftStick(port)
    proxy.set_supervised(.8, 0, deadline=time.monotonic()+.2, stream_id='a', sequence=1)
    proxy.set_supervised(0, .8, deadline=time.monotonic()+.2, stream_id='a', sequence=2)
    assert all(vector != (0, 0) for _, vector in pad.reports)
    assert proxy._thread is None
    expiry = time.monotonic()+1
    while pad.vector != (0, 0) and time.monotonic() < expiry:
        gamepad_broker._exchange(port, {'op': 'heartbeat'})
        time.sleep(.01)
    assert pad.vector == (0, 0)
    assert gamepad_broker._exchange(port, {'op': 'status'})['supervised_sequence'] == 2


def test_broker_bad_update_and_owner_exit_release_held_input(broker):
    port, pad, owner = broker
    proxy = gamepad_broker.RemoteVirtualLeftStick(port)
    proxy.set_supervised(.8, 0, deadline=time.monotonic()+.4, stream_id='a', sequence=1)
    with pytest.raises(gamepad_broker.GamepadBrokerError, match='not_advancing'):
        proxy.set_supervised(.8, 0, deadline=time.monotonic()+.4, stream_id='a', sequence=1)
    assert pad.vector == (0, 0)
    proxy.set_supervised(.8, 0, deadline=time.monotonic()+.4, stream_id='a', sequence=2)
    owner['alive'] = False
    expiry = time.monotonic()+1
    while pad.vector != (0, 0) and time.monotonic() < expiry:
        time.sleep(.01)
    assert pad.vector == (0, 0)


def test_broker_pulses_and_releases_virtual_gamepad_a(broker):
    port, pad, _ = broker
    proxy = gamepad_broker.RemoteVirtualLeftStick(port)

    result = proxy.press_a(20)

    assert result == {
        "ok": True,
        "button": "x360_a",
        "milliseconds": 20,
        "released": True,
    }
    assert gamepad_broker.XUSB_GAMEPAD_A == 0x1000
    assert any(
        gamepad_broker.XUSB_GAMEPAD_A in buttons
        for _, _, buttons in pad.button_reports
    )
    assert pad.buttons == set()
    assert pad.button_reports[-1][2] == set()


def test_broker_rejects_unbounded_gamepad_a_pulse(broker):
    port, _, _ = broker
    proxy = gamepad_broker.RemoteVirtualLeftStick(port)

    with pytest.raises(gamepad_broker.GamepadBrokerError, match="duration"):
        proxy.press_a(1501)


def test_broker_pulses_an_allow_listed_card_game_button(broker):
    port, pad, _ = broker
    proxy = gamepad_broker.RemoteVirtualLeftStick(port)

    result = proxy.pulse_button("XUSB_GAMEPAD_DPAD_RIGHT", 20)

    assert result["released"] is True and result["button"] == "XUSB_GAMEPAD_DPAD_RIGHT"
    assert any(0x0008 in buttons for _, _, buttons in pad.button_reports)
    assert pad.buttons == set()


def test_broker_rejects_unlisted_buttons_and_overlong_pulses(broker):
    port, pad, _ = broker
    proxy = gamepad_broker.RemoteVirtualLeftStick(port)
    with pytest.raises(gamepad_broker.GamepadBrokerError, match="duration"):
        proxy.pulse_button("XUSB_GAMEPAD_Y", 2501)
    with pytest.raises(gamepad_broker.GamepadBrokerError, match="not_allow_listed"):
        gamepad_broker._exchange(port, {"op": "pulse_button", "button": "XUSB_GAMEPAD_GUIDE", "milliseconds": 20})
    assert pad.buttons == set()


def test_broker_pulses_right_stick_and_reports_its_neutral_release(broker):
    port, pad, _ = broker
    proxy = gamepad_broker.RemoteVirtualLeftStick(port)

    result = proxy.pulse_right_stick(.3, -.4, 20)

    assert result == {"ok": True, "released": True, "milliseconds": 20}
    assert any(vector == pytest.approx((.3, -.4)) for vector in pad.right_reports)
    assert pad.right_reports[-1] == (0.0, 0.0)
    assert pad.right_vector == (0.0, 0.0)


@pytest.mark.parametrize("milliseconds", [19, 301, True])
def test_broker_rejects_camera_pulses_outside_20_to_300_ms(broker, milliseconds):
    port, pad, _ = broker

    with pytest.raises(gamepad_broker.GamepadBrokerError, match="camera_duration_out_of_range"):
        gamepad_broker._exchange(port, {
            "op": "pulse_right_stick", "x": .3, "y": 0.0,
            "milliseconds": milliseconds,
        })

    assert pad.right_vector == (0.0, 0.0)


def test_broker_camera_pulse_rejects_concurrent_movement_and_neutralizes_it(broker):
    port, pad, _ = broker
    proxy = gamepad_broker.RemoteVirtualLeftStick(port)
    proxy.set_supervised(.8, 0.0, deadline=time.monotonic() + .4, stream_id="camera", sequence=1)

    with pytest.raises(gamepad_broker.GamepadBrokerError, match="camera_pulse_requires_neutral_movement"):
        proxy.pulse_right_stick(.3, 0.0, 20)

    assert pad.vector == (0.0, 0.0)
    assert pad.right_vector == (0.0, 0.0)


class StreamingActuator(FakeActuator):
    def __init__(self):
        super().__init__()
        self.updates = []
    def supervised_command(self, x, y, **kwargs):
        self.updates.append(dict(x=x, y=y, neutral_before=self.neutral,
            neutralizations=self.neutralize_count, **kwargs))
        self.neutral = False
        return {'ok': True}


def stream_adapter(observations=None):
    actuator = StreamingActuator()
    config = LiveAdapterConfig(controller_mode='continuous', normal_stick_magnitude=.8,
        post_arrival_samples=0, max_cycles=4, readiness_recovery_timeout_seconds=.2,
        preflight_telemetry_wait_seconds=.2)
    adapter, telemetry, _, _ = make_adapter(actuator=actuator, config=config, observations=observations)
    assert adapter.preflight()['movement_authorized']
    return adapter, actuator, telemetry


def test_adapter_renews_held_input_without_neutral_gap_then_stops_at_bound():
    adapter, actuator, _ = stream_adapter()
    original_observe = adapter.telemetry.observe
    adapter.telemetry.observe = lambda: replace(original_observe(), actuator_neutral=actuator.neutral)
    result = adapter.run(dry_run=False, max_cycles=2)
    assert len(actuator.updates) == 2, result['reason']
    assert actuator.updates[0]['neutral_before']
    assert not actuator.updates[1]['neutral_before']
    assert actuator.updates[0]['neutralizations'] == actuator.updates[1]['neutralizations']
    assert len(actuator.commands) == 0
    assert actuator.neutral
    assert result['reason'] == 'control_cycle_bound_reached'


@pytest.mark.parametrize('change', [dict(lease_active=False), dict(actor_id='different'),
    dict(world_id='different'), dict(position=Vec3(2000, 0, 0)), dict(telemetry_age_seconds=2)])
def test_stream_stops_for_lost_lease_identity_route_or_freshness(change):
    obs = [make_observation(sequence=i) for i in (1,2,3)]
    obs.append(make_observation(sequence=4, **change))
    adapter, actuator, _ = stream_adapter(obs)
    result = adapter.run(dry_run=False, max_cycles=2)
    assert len(actuator.updates) == 1, result['reason']
    assert actuator.neutral
    assert result['state'] != 'FOLLOWING'


def test_stream_final_control_uses_existing_pulse_and_exceptions_neutralize():
    adapter, actuator, _ = stream_adapter()
    observation = make_observation(sequence=3)
    steering = {'control_band': 'final', 'desired_world_heading_deg': 0}
    assert adapter._supervised_deadline(observation, steering, {'distance_m': 1}) is None
    def fail(*args, **kwargs):
        actuator.neutral = False
        raise RuntimeError('dispatch failed')
    actuator.supervised_command = fail
    with pytest.raises(RuntimeError, match='dispatch failed'):
        adapter.run(dry_run=False, max_cycles=1)
    assert actuator.neutral


def test_an_expired_lease_falls_back_to_a_pulse_instead_of_ending_the_route():
    # Live on pursuit routes (#169): the broker refused a lease that expired
    # in transit, and the whole route stopped and was replanned.
    adapter, actuator, _ = stream_adapter()
    original_observe = adapter.telemetry.observe
    adapter.telemetry.observe = lambda: replace(original_observe(), actuator_neutral=actuator.neutral)

    def expired(*args, **kwargs):
        raise ValueError("supervised_deadline_invalid_or_expired")

    actuator.supervised_command = expired
    result = adapter.run(dry_run=False, max_cycles=2)
    assert result['reason'] == 'control_cycle_bound_reached'
    assert len(actuator.commands) == 2  # each cycle sent its ordinary pulse
    assert sum(event.get('event') == 'supervised_lease_expired_pulse_fallback' for event in adapter.timeline) == 2
    assert actuator.neutral


def test_stream_horizon_shortens_near_boundary_and_refuses_outside_corridor():
    adapter, _, _ = stream_adapter()
    steering = {'control_band': 'normal', 'desired_world_heading_deg': 0}
    event = {'distance_m': 4}
    full = adapter._supervised_deadline(make_observation(), steering, event)
    near = adapter._supervised_deadline(make_observation(position=Vec3(700,0,0)), steering, event)
    assert full and near and near['lease_seconds'] < full['lease_seconds']
    assert adapter._supervised_deadline(make_observation(position=Vec3(990,0,0)), steering, event) is None


def test_stream_exact_corridor_proof_can_bridge_near_waypoint_without_broadening_boundary():
    adapter, _, _ = stream_adapter()
    steering = {'control_band': 'normal', 'desired_world_heading_deg': 0}
    # The old waypoint-distance prefilter returned None here. The exact held
    # travel horizon is nevertheless fully contained in the authorized polygon.
    lease = adapter._supervised_deadline(make_observation(), steering, {'distance_m': 0.5})
    assert lease is not None
    assert lease['corridor_proven'] is True
    assert lease['lease_seconds'] == pytest.approx(0.4)
    # Geometry, rather than waypoint proximity, remains authoritative.
    assert adapter._supervised_deadline(
        make_observation(position=Vec3(990, 0, 0)), steering, {'distance_m': 0.5}
    ) is None


def test_late_displacement_is_discarded_before_obstacle_becomes_planning_geometry():
    observations = [make_observation(sequence=i) for i in range(1, 7)]
    observations += [make_observation(sequence=i, position=Vec3(75, 0, 0)) for i in range(7, 11)]
    # Static-obstacle learning only accepts a sustained command as movement
    # evidence. Keep this regression on the production 250 ms final pulse
    # floor; the old 150 ms fixture no longer represents admissible evidence.
    config = LiveAdapterConfig(
        max_cycles=4,
        post_arrival_samples=0,
        control_period_seconds=0.25,
        approach_control_period_seconds=0.25,
        final_control_period_seconds=0.25,
    )
    adapter, _, actuator, _ = make_adapter(observations=observations, config=config)
    assert adapter.preflight()['movement_authorized']
    result = adapter.run(dry_run=False, max_cycles=4)
    confirmed = [e for e in result['timeline'] if e.get('event') == 'static_obstacle_settled_confirmation']
    assert not confirmed
    assert result['reason'] == 'control_cycle_bound_reached'
    assert not adapter.static_obstacles.planning_snapshot(now_t=10).obstacles
    assert len(actuator.commands) == 4
    assert actuator.neutral


@pytest.mark.parametrize('runtime', ['CANONICAL', 'FEATURE_VALIDATION'])
def test_continuous_policy_is_explicit_feature_only(monkeypatch, runtime):
    monkeypatch.setenv('FF7R_RUNTIME_PROFILE', runtime)
    args = {**PROVEN_LIVE_CONTROLLER_POLICY, 'normal_stick_magnitude': .8,
        'controller_mode': 'continuous', 'controller_policy_profile': CONTINUOUS_PROFILE}
    if runtime == 'CANONICAL':
        with pytest.raises(LiveControllerPolicyError, match='feature-validation'):
            require_proven_live_controller_policy(args)
    else:
        require_proven_live_controller_policy(args)
        evidence = proven_live_controller_policy_evidence(args)
        assert evidence['controller_mode'] == 'continuous'
        assert evidence['controller_policy_profile'] == CONTINUOUS_PROFILE
    with pytest.raises(LiveControllerPolicyError, match='explicit'):
        require_proven_live_controller_policy({**args, 'controller_policy_profile': 'normal_0_8_trial'})


def test_mission_forwards_continuous_mode_and_profile(tmp_path):
    runtime = Runtime()
    async def rpc(op, args):
        result = await runtime.call(op, args)
        if op == 'get_runtime_identity':
            result['result']['deployment_profile'] = 'FEATURE_VALIDATION'
        return result
    mission = NavigationMission(rpc, tmp_path/'continuous', identity_check=lambda *_: {'ok': True},
        config=MissionConfig(execute=True, controller_mode='continuous', normal_stick_magnitude=.8),
        clock=lambda: runtime.time, sleep=runtime.sleep, progress=lambda _: None)
    result = asyncio.run(mission.run())
    assert result['ok']
    args = next(a for op,a in runtime.calls if op == 'start_live_route_adapter')
    assert args['controller_mode'] == 'continuous'
    assert args['controller_policy_profile'] == CONTINUOUS_PROFILE


def test_stray_active_input_does_not_gain_continuous_authority():
    adapter, _, _ = stream_adapter()
    assert 'actuator_not_neutral' in adapter._check_preconditions(make_observation(actuator_neutral=False))


def test_continuous_rejects_measured_speed_outside_envelope():
    adapter, _, _ = stream_adapter()
    with pytest.raises(Exception, match='continuous_speed_envelope_exceeded'):
        adapter._supervised_deadline(make_observation(velocity_cm_s=Vec3(900, 0, 0)),
            {'control_band': 'normal'}, {'distance_m': 4})
