from dataclasses import replace
import asyncio
import math
import time

import pytest
from ff7r_agent import gamepad_broker
from ff7r_agent.continuous_control import SPRINT_PROFILE, CONTINUOUS_PROFILE
from ff7r_agent.controller_policy import PROVEN_LIVE_CONTROLLER_POLICY, require_proven_live_controller_policy, LiveControllerPolicyError
from ff7r_agent.input import WindowsHybridGamepadInput
from ff7r_agent.live_route_adapter import LiveAdapterConfig, AdapterError
from ff7r_agent.models import Vec3
from ff7r_agent.navigation_mission import MissionConfig, NavigationMission, route_metrics
from test_continuous_control import broker, stream_adapter
from test_gamepad_input import FakeKeyboard
from test_live_route_adapter import make_observation
from test_navigation_mission import Runtime


def sprint(proxy, sequence=1, **changes):
    args = dict(deadline=time.monotonic()+.2, stream_id='sprint', sequence=sequence, sprint=True)
    return proxy.set_supervised(1, 0, **(args | changes))


def wait_until_neutral(pad):
    limit = time.monotonic()+1.0
    while (pad.vector != (0, 0) or pad.trigger) and time.monotonic() < limit:
        time.sleep(.01)
    assert pad.vector == (0, 0) and pad.trigger == 0


def test_rt_changes_atomically_with_supervised_stick_and_releases_while_walking(broker):
    port, pad, _ = broker
    proxy = gamepad_broker.RemoteVirtualLeftStick(port)
    assert sprint(proxy)['right_trigger'] == 1
    assert pad.trigger_reports[-1] == ((1, 0), 1)
    proxy.set_supervised(0, .8, deadline=time.monotonic()+.2, stream_id='sprint', sequence=2)
    assert pad.trigger_reports[-1] == ((0, .8), 0)
    assert all(vector != (0, 0) for vector, _ in pad.trigger_reports)
    assert gamepad_broker._exchange(port, {'op': 'status'})['right_trigger'] == 0


@pytest.mark.parametrize('reason', ['expiry', 'replay', 'neutral', 'owner_exit'])
def test_all_broker_stops_release_rt_and_stick(broker, reason):
    port, pad, owner = broker
    proxy = gamepad_broker.RemoteVirtualLeftStick(port)
    sprint(proxy)
    assert pad.trigger == 1
    if reason == 'expiry':
        limit = time.monotonic()+.4
        while time.monotonic() < limit:
            gamepad_broker._exchange(port, {'op': 'heartbeat'})
            time.sleep(.01)
    elif reason == 'replay':
        with pytest.raises(gamepad_broker.GamepadBrokerError, match='not_advancing'):
            sprint(proxy)
    elif reason == 'neutral':
        proxy.reset()
    else:
        owner['alive'] = False
    wait_until_neutral(pad)


@pytest.mark.parametrize('flag,vector', [('true', (1, 0)), (1, (1, 0)), (True, (.8, 0)), (True, (0, 0))])
def test_malformed_sprint_releases_existing_rt(broker, flag, vector):
    port, pad, _ = broker
    proxy = gamepad_broker.RemoteVirtualLeftStick(port)
    sprint(proxy)
    with pytest.raises(gamepad_broker.GamepadBrokerError, match='sprint_requires'):
        proxy.set_supervised(*vector, deadline=time.monotonic()+.2, stream_id='sprint', sequence=2, sprint=flag)
    wait_until_neutral(pad)


def test_input_backend_release_all_and_dispatch_error_release_rt(broker):
    port, pad, _ = broker
    backend = WindowsHybridGamepadInput(gamepad=gamepad_broker.RemoteVirtualLeftStick(port), keyboard=FakeKeyboard())
    kwargs = dict(deadline=time.monotonic()+.3, stream_id='input', sequence=1, sprint=True)
    backend.supervised_move(1, 0, **kwargs)
    assert backend.backend_status()['last_sprint'] is True
    with pytest.raises(gamepad_broker.GamepadBrokerError):
        backend.supervised_move(1, 0, **kwargs)
    assert backend.backend_status()['last_sprint'] is False
    kwargs.update(deadline=time.monotonic()+.3, sequence=2)
    backend.supervised_move(1, 0, **kwargs)
    backend.release_all()
    wait_until_neutral(pad)
    assert backend.backend_status()['last_sprint'] is False


def sprint_adapter():
    adapter, actuator, telemetry = stream_adapter()
    adapter.config = replace(adapter.config, sprint=True)
    observe = telemetry.observe
    telemetry.observe = lambda: replace(observe(), grounded=True, velocity_cm_s=Vec3(0, 0, 0), actuator_neutral=actuator.neutral)
    callback = adapter.follower.on_telemetry
    def follower(sample):
        result = callback(sample)
        for event in result:
            event.update(distance_m=20, distance_to_final_destination_m=30)
        return result
    adapter.follower.on_telemetry = follower
    return adapter, actuator


def normal_event(**changes):
    return dict(distance_m=20, distance_to_final_destination_m=30, heading_error_deg=0) | changes


def pose(**changes):
    return make_observation(grounded=True, velocity_cm_s=Vec3(0, 0, 0), **changes)


def test_route_uses_full_stick_and_rt_only_for_proven_sprint_horizon():
    adapter, actuator = sprint_adapter()
    result = adapter.run(dry_run=False, max_cycles=2)
    assert len(actuator.updates) == 2, result['reason']
    assert all(u['sprint'] and math.hypot(u['x'], u['y']) == pytest.approx(1) for u in actuator.updates)
    assert not actuator.updates[1]['neutral_before']
    evidence = [e['supervision'] for e in result['timeline'] if e.get('event') == 'movement_setpoint_issued']
    assert all(e['speed_bound_m_s'] == 12 and e['braking_margin_m'] == 1.5 and e['lease_seconds'] <= .3 for e in evidence)
    assert actuator.neutral


@pytest.mark.parametrize('band,event_changes,pose_changes', [
    ('approach', {}, {}), ('final', {}, {}), ('normal', {'distance_m': 5.9}, {}),
    ('normal', {'distance_to_final_destination_m': 7.9}, {}),
    ('normal', {'heading_error_deg': 16}, {}), ('normal', {'heading_error_deg': math.nan}, {}),
    ('normal', {}, {'grounded': False}), ('normal', {}, {'velocity_cm_s': None}),
])
def test_turns_goal_unmeasured_velocity_and_airborne_samples_do_not_request_sprint(band, event_changes, pose_changes):
    adapter, _ = sprint_adapter()
    observation = replace(pose(), **pose_changes)
    assert not adapter._sprint_eligible(observation, {'control_band': band}, normal_event(**event_changes))


def test_sprint_cannot_cross_unproven_corridor_or_ignore_speed_limit():
    adapter, _ = sprint_adapter()
    steering = {'control_band': 'normal', 'desired_world_heading_deg': 0}
    assert adapter._supervised_deadline(pose(position=Vec3(990, 0, 0)), steering, normal_event()) is None
    for velocity in (None, Vec3(math.nan, 0, 0), Vec3(1300, 0, 0)):
        with pytest.raises(AdapterError):
            adapter._supervised_deadline(replace(pose(), velocity_cm_s=velocity), steering, normal_event())


def test_sprint_to_pulse_releases_on_proven_low_speed_drift_and_times_out_when_unproven():
    adapter, actuator = sprint_adapter()
    clock = [10.0]
    adapter.monotonic = lambda: clock[0]
    adapter._sprint_pending_settle = True
    actuator.neutral = False
    assert adapter._sprint_braking_required(pose(sequence=3), None)
    assert actuator.neutral
    clock[0] = 10.2
    assert adapter._sprint_braking_required(
        replace(pose(sequence=4), velocity_cm_s=Vec3(61, 0, 0)), None)
    assert adapter._sprint_pending_settle
    clock[0] = 10.3
    assert adapter._sprint_braking_required(
        replace(pose(sequence=5), velocity_cm_s=Vec3(55, 0, 0)), None)
    assert not adapter._sprint_pending_settle
    assert any(e.get('event') == 'sprint_low_speed_pulse_handoff_proven' for e in adapter.timeline)
    assert not adapter._sprint_braking_required(pose(sequence=6), None)
    adapter._sprint_pending_settle = True
    assert adapter._sprint_braking_required(pose(sequence=7), None)
    clock[0] = 16
    with pytest.raises(AdapterError, match='sprint_braking_unproven'):
        adapter._sprint_braking_required(pose(sequence=8), None)


def test_sprint_low_speed_handoff_fails_closed_when_residual_drift_leaves_corridor():
    adapter, actuator = sprint_adapter()
    clock = [20.0]
    adapter.monotonic = lambda: clock[0]
    adapter._sprint_pending_settle = True
    actuator.neutral = False
    assert adapter._sprint_braking_required(pose(sequence=10), None)
    clock[0] = 20.2
    outward = replace(pose(sequence=11, position=Vec3(990, 0, 0)),
                      monotonic_t=clock[0], velocity_cm_s=Vec3(55, 0, 0))
    assert adapter._sprint_braking_required(outward, None)
    assert adapter._sprint_pending_settle
    sample = next(e for e in reversed(adapter.timeline) if e.get('event') == 'sprint_braking_sample')
    assert sample['low_speed_handoff_proven'] is False
    assert sample['low_speed_handoff']['reason'] == 'residual_drift_corridor_unproven'


def test_sprint_low_speed_handoff_rejects_airborne_missing_velocity_and_speed_above_bound():
    adapter, actuator = sprint_adapter()
    clock = [30.0]
    adapter.monotonic = lambda: clock[0]
    adapter._sprint_pending_settle = True
    actuator.neutral = False
    assert adapter._sprint_braking_required(pose(sequence=20), None)
    cases = [
        replace(pose(sequence=21), grounded=False, velocity_cm_s=Vec3(0, 0, 0)),
        replace(pose(sequence=22), velocity_cm_s=None),
        replace(pose(sequence=23), velocity_cm_s=Vec3(61, 0, 0)),
    ]
    for offset, sample in enumerate(cases, 1):
        clock[0] = 30.2 + offset * .1
        assert adapter._sprint_braking_required(sample, None)
        assert adapter._sprint_pending_settle


def test_sprint_trial_is_explicit_and_feature_only(monkeypatch):
    values = dict(PROVEN_LIVE_CONTROLLER_POLICY, normal_stick_magnitude=.8,
        controller_mode='continuous', controller_policy_profile=SPRINT_PROFILE, sprint=True)
    monkeypatch.setenv('FF7R_RUNTIME_PROFILE', 'CANONICAL')
    with pytest.raises(LiveControllerPolicyError, match='feature-validation'):
        require_proven_live_controller_policy(values)
    monkeypatch.setenv('FF7R_RUNTIME_PROFILE', 'FEATURE_VALIDATION')
    require_proven_live_controller_policy(values)
    for changes in ({'sprint': False}, {'sprint': 1}, {'controller_policy_profile': CONTINUOUS_PROFILE}, {'controller_mode': 'pulse'}):
        with pytest.raises(LiveControllerPolicyError):
            require_proven_live_controller_policy(values | changes)
    with pytest.raises(ValueError):
        MissionConfig(sprint=True)
    with pytest.raises(ValueError):
        LiveAdapterConfig(sprint=True)


def test_mission_forwards_sprint_flag_and_records_separate_sprint_metrics(tmp_path):
    runtime = Runtime()
    async def rpc(op, args):
        result = await runtime.call(op, args)
        if op == 'get_runtime_identity':
            result['result']['deployment_profile'] = 'FEATURE_VALIDATION'
        return result
    mission = NavigationMission(rpc, tmp_path/'sprint', identity_check=lambda *_: {'ok': True},
        config=MissionConfig(execute=True, controller_mode='continuous', normal_stick_magnitude=.8, sprint=True),
        clock=lambda: runtime.time, sleep=runtime.sleep, progress=lambda _: None)
    result = asyncio.run(mission.run())
    assert result['ok'] and result['controller_policy']['sprint_binding'] == 'held_rt'
    args = next(a for op,a in runtime.calls if op == 'start_live_route_adapter')
    assert args['sprint'] is True and args['controller_policy_profile'] == SPRINT_PROFILE
    metrics = route_metrics({'run': {'timeline': [
        {'event': 'movement_setpoint_issued', 'supervision': {'sprint': True}},
        {'event': 'movement_setpoint_issued', 'supervision': {'sprint': False}},
        {'event': 'movement_pulse_completed'}, {'event': 'sprint_braking_sample'},
    ]}})
    assert metrics['sprint_setpoints'] == 1 and metrics['supervised_setpoints'] == 2
    assert metrics['completed_pulses'] == 1 and metrics['sprint_braking_samples'] == 1


def test_observed_braking_can_settle_after_two_seconds_without_input_or_stall_charge():
    # Actual sequence/speed timing from the first RT benchmark; the former
    # two-second deadline expired just before the second stationary sample.
    adapter, actuator = sprint_adapter()
    clock = [8057.6375557]
    adapter.monotonic = lambda: clock[0]
    accounting = []
    adapter.follower.pause_for_safety = lambda t: accounting.append(('pause', t))
    adapter.follower.resume_after_safety_pause = lambda t: accounting.append(('resume', t))
    adapter._sprint_pending_settle = True
    assert adapter._sprint_braking_required(pose(sequence=29344), None)
    samples = [
        (8057.6610283, 29345, .87176847898),
        (8058.2398766, 29349, .55325077185),
        (8059.6380622, 29353, 0.0),
        (8059.7554641, 29354, 0.0),
    ]
    release_t = None
    for t, sequence, speed in samples:
        clock[0] = t
        sample = replace(pose(sequence=sequence), monotonic_t=t, velocity_cm_s=Vec3(speed*100, 0, 0))
        consumed = adapter._sprint_braking_required(sample, None)
        assert consumed
        if not adapter._sprint_pending_settle:
            release_t = t
            break
    assert release_t == pytest.approx(8058.2398766)
    assert release_t - 8057.6375557 == pytest.approx(.6023209)
    assert [kind for kind, _ in accounting] == ['pause', 'resume']
    handoff = next(e for e in adapter.timeline if e.get('event') == 'sprint_low_speed_pulse_handoff_proven')
    assert handoff['horizontal_speed_m_s'] == pytest.approx(.55325077185)
    assert handoff['drift_bound_m'] < .4
    assert actuator.neutral and not actuator.commands and not actuator.updates
