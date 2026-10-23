"""Opt-in full-stick continuous travel (continuous_fast_follow_trial)."""
from dataclasses import replace
import asyncio

import pytest

from ff7r_agent.continuous_control import (
    CONTINUOUS_PROFILE, FAST_FOLLOW_CAMERA_DEADBAND_DEG, FAST_FOLLOW_PROFILE,
)
from ff7r_agent.controller_policy import (
    PROVEN_LIVE_CONTROLLER_POLICY, LiveControllerPolicyError, live_controller_policy_evidence,
    proven_live_controller_policy_evidence, require_proven_live_controller_policy,
)
from ff7r_agent.live_route_adapter import LiveAdapterConfig
from ff7r_agent.models import Vec3
from ff7r_agent.navigation_mission import MissionConfig, NavigationMission
from test_continuous_control import StreamingActuator, stream_adapter
from test_live_route_adapter import FakeFollower, make_adapter, make_observation
from test_navigation_mission import Runtime


def fast_adapter(observations=None, *, fast_follow=True, magnitude=1.0, max_cycles=4):
    actuator = StreamingActuator()
    config = LiveAdapterConfig(controller_mode='continuous', normal_stick_magnitude=magnitude,
        continuous_fast_follow=fast_follow, post_arrival_samples=0, max_cycles=max_cycles,
        control_period_seconds=.3, approach_control_period_seconds=.25, final_control_period_seconds=.25,
        readiness_recovery_timeout_seconds=.2, preflight_telemetry_wait_seconds=.2)
    adapter, telemetry, _, _ = make_adapter(actuator=actuator, config=config, observations=observations)
    assert adapter.preflight()['movement_authorized']
    return adapter, actuator, telemetry


def moving(**changes):
    return make_observation(velocity_cm_s=Vec3(300.0, 0.0, 0.0), **changes)


def slow_event(**changes):
    """A follower event inside an intermediate waypoint's 2 m slow band."""
    event = {'state': 'FOLLOWING', 'waypoint_index': 0, 'distance_m': 1.2,
        'distance_to_final_destination_m': 20.0, 'cross_track_error_m': 0.0,
        'route_progress_fraction': 0.2, 'desired_heading_deg': 0.0, 'heading_error_deg': 0.0,
        'suggested_speed_band': 'slow'}
    event.update(changes)
    return event


class SlowBandFollower(FakeFollower):
    def on_telemetry(self, sample):
        return [slow_event()]


# -- configuration and policy boundary ---------------------------------------

@pytest.mark.parametrize('changes', [dict(controller_mode='pulse'),
    dict(sprint=True, normal_stick_magnitude=.8), dict(continuous_fast_follow=1)])
def test_adapter_config_requires_non_sprint_continuous_boolean(changes):
    values = dict(controller_mode='continuous', continuous_fast_follow=True)
    values.update(changes)
    with pytest.raises(ValueError, match='continuous_fast_follow'):
        LiveAdapterConfig(**values)


def test_adapter_config_defaults_off():
    assert LiveAdapterConfig().continuous_fast_follow is False


@pytest.mark.parametrize('runtime', ['CANONICAL', 'FEATURE_VALIDATION'])
def test_fast_follow_policy_is_accepted_in_every_runtime_at_full_stick(monkeypatch, runtime):
    # Pursuit rides on this profile and is the default for navigating to
    # targets, so the canonical runtime accepts it too.
    monkeypatch.setenv('FF7R_RUNTIME_PROFILE', runtime)
    args = {**PROVEN_LIVE_CONTROLLER_POLICY, 'controller_mode': 'continuous',
        'controller_policy_profile': FAST_FOLLOW_PROFILE, 'continuous_fast_follow': True}
    assert require_proven_live_controller_policy(args)['normal_stick_magnitude'] == 1.0
    evidence = proven_live_controller_policy_evidence(args)
    assert evidence['continuous_fast_follow'] is True
    assert evidence['controller_policy_profile'] == FAST_FOLLOW_PROFILE
    assert evidence['validation_status'] == 'live_validated'
    # The flag and the profile must travel together, in continuous mode, at 1.0.
    for broken, code in [
        ({'continuous_fast_follow': False}, 'live_fast_follow_profile_mismatch'),
        ({'controller_policy_profile': CONTINUOUS_PROFILE}, 'live_fast_follow_profile_mismatch'),
        ({'continuous_fast_follow': 'yes'}, 'live_fast_follow_profile_mismatch'),
        ({'controller_mode': 'pulse'}, 'live_controller_mode_profile_mismatch'),
        ({'normal_stick_magnitude': .8}, 'live_controller_policy_mismatch'),
    ]:
        with pytest.raises(LiveControllerPolicyError) as error:
            require_proven_live_controller_policy({**args, **broken})
        assert error.value.code == code


def test_proven_pulse_policy_rejects_a_stray_fast_follow_flag():
    with pytest.raises(LiveControllerPolicyError) as error:
        require_proven_live_controller_policy({**PROVEN_LIVE_CONTROLLER_POLICY, 'continuous_fast_follow': True})
    assert error.value.code == 'live_fast_follow_profile_mismatch'
    # The default route is untouched: no flag, proven profile.
    require_proven_live_controller_policy(dict(PROVEN_LIVE_CONTROLLER_POLICY))


def test_policy_evidence_names_the_fast_follow_profile():
    config = LiveAdapterConfig(controller_mode='continuous', continuous_fast_follow=True)
    evidence = live_controller_policy_evidence(adapter_config=config)
    assert evidence['controller_policy_profile'] == FAST_FOLLOW_PROFILE
    assert evidence['validation_status'] == 'live_validated'
    baseline = live_controller_policy_evidence(adapter_config=LiveAdapterConfig())
    assert baseline['controller_policy_profile'] == 'proven'
    assert baseline['continuous_fast_follow'] is False


# -- supervised hold: momentum ray -------------------------------------------

def test_hold_requires_measured_velocity_and_otherwise_uses_pulses():
    adapter, _, _ = fast_adapter()
    steering = {'control_band': 'normal', 'desired_world_heading_deg': 0}
    assert adapter._supervised_deadline(make_observation(), steering, {'distance_m': 4}) is None
    lease = adapter._supervised_deadline(moving(), steering, {'distance_m': 4})
    assert lease is not None and lease['corridor_proven'] and lease['momentum_ray_proven']


def test_momentum_ray_shortens_or_refuses_a_hold_the_commanded_ray_alone_would_allow():
    fast, _, _ = fast_adapter()
    plain, _, _ = stream_adapter()
    steering = {'control_band': 'normal', 'desired_world_heading_deg': 0}
    sideways = dict(velocity_cm_s=Vec3(0.0, 300.0, 0.0))
    # 7 m from the +y edge: the 3.95 m full-lease momentum horizon leaves the
    # polygon, a half lease stays inside.
    near = make_observation(position=Vec3(0, 700, 0), **sideways)
    assert plain._supervised_deadline(near, steering, {'distance_m': 4})['lease_seconds'] == pytest.approx(.4)
    # With a 100 ms pulse to replace, the half lease is long enough to hold.
    short_pulse = {**steering, 'command_duration_ms': 100}
    shortened = fast._supervised_deadline(near, short_pulse, {'distance_m': 4})
    assert shortened['lease_seconds'] == pytest.approx(.2)
    # Against the 300 ms normal pulse the same 0.2 s hold is refused: pulse.
    assert fast._supervised_deadline(near, {**steering, 'command_duration_ms': 300}, {'distance_m': 4}) is None
    # 9 m from the edge along the momentum ray: no hold at all.
    edge = make_observation(position=Vec3(0, 900, 0), **sideways)
    assert plain._supervised_deadline(edge, steering, {'distance_m': 4}) is not None
    assert fast._supervised_deadline(edge, steering, {'distance_m': 4}) is None


def test_fast_follow_keeps_the_speed_envelope_and_final_pulse():
    adapter, _, _ = fast_adapter()
    with pytest.raises(Exception, match='continuous_speed_envelope_exceeded'):
        adapter._supervised_deadline(make_observation(velocity_cm_s=Vec3(900, 0, 0)),
            {'control_band': 'normal', 'desired_world_heading_deg': 0}, {'distance_m': 4})
    assert adapter._supervised_deadline(moving(), {'control_band': 'final', 'desired_world_heading_deg': 0},
        {'distance_m': 1}) is None


# -- waypoint carry -----------------------------------------------------------

def carry(adapter, observation=None, **event_changes):
    observation = observation or moving()
    event = slow_event(**event_changes)
    _, _, steering = adapter._steering(observation, event)
    return steering, adapter._fast_follow_waypoint_carry(observation, steering, event)


def test_intermediate_waypoint_slow_band_is_carried_at_full_stick_with_a_proven_hold():
    adapter, _, _ = fast_adapter()
    steering, carried = carry(adapter)
    assert steering['control_band'] == 'approach' and steering['magnitude'] == pytest.approx(.5)
    x, y, carried_steering, supervision = carried
    assert carried_steering['control_band'] == 'normal'
    assert carried_steering['magnitude'] == pytest.approx(1.0)
    assert (x, y) == pytest.approx((0.0, 1.0))
    assert carried_steering['control_selection_reasons'] == ['follower_distance_slow', 'fast_follow_waypoint_carry']
    assert supervision['corridor_proven'] and supervision['momentum_ray_proven']


@pytest.mark.parametrize('event_changes', [
    dict(waypoint_index=1),                       # final waypoint
    dict(distance_to_final_destination_m=5.0),    # inside the arrival stopping horizon
    dict(heading_error_deg=60.0),                 # a real turn
    dict(heading_error_deg=None),
    dict(heading_error_deg=120.0, distance_m=1.2),  # follower heading slow band
    dict(suggested_speed_band='normal', distance_m=4.0),
])
def test_carry_is_refused_outside_an_intermediate_distance_slow_band(event_changes):
    adapter, _, _ = fast_adapter()
    assert carry(adapter, **event_changes)[1] is None


def test_carry_is_refused_when_the_full_stick_hold_is_unproven_or_opted_out():
    adapter, _, _ = fast_adapter()
    assert carry(adapter, observation=make_observation())[1] is None  # no measured velocity
    assert carry(adapter, observation=moving(position=Vec3(990, 0, 0)))[1] is None  # corridor edge
    plain, _, _ = stream_adapter()
    assert carry(plain)[1] is None


def test_run_holds_full_stick_through_intermediate_waypoint_slow_band():
    adapter, actuator, _ = fast_adapter([moving(sequence=i) for i in range(1, 8)])
    adapter.follower = SlowBandFollower(adapter.route_plan)
    original_observe = adapter.telemetry.observe
    adapter.telemetry.observe = lambda: replace(original_observe(), actuator_neutral=actuator.neutral)
    result = adapter.run(dry_run=False, max_cycles=2)
    assert result['reason'] == 'control_cycle_bound_reached'
    assert len(actuator.updates) == 2 and not actuator.commands
    assert all(abs(update['y']) == pytest.approx(1.0) for update in actuator.updates)
    assert not actuator.updates[1]['neutral_before']
    imminent = [e for e in result['timeline'] if e['event'] == 'non_neutral_command_imminent']
    assert all('fast_follow_waypoint_carry' in e['control_selection_reasons'] for e in imminent)
    assert all(e['control_band'] == 'normal' for e in imminent)
    assert actuator.neutral


def test_run_without_fast_follow_holds_the_same_slow_band_at_approach_magnitude():
    adapter, actuator, _ = fast_adapter([moving(sequence=i) for i in range(1, 8)],
        fast_follow=False, magnitude=.8)
    adapter.follower = SlowBandFollower(adapter.route_plan)
    original_observe = adapter.telemetry.observe
    adapter.telemetry.observe = lambda: replace(original_observe(), actuator_neutral=actuator.neutral)
    result = adapter.run(dry_run=False, max_cycles=2)
    assert len(actuator.updates) == 2, result['reason']
    assert all(abs(update['y']) == pytest.approx(.5) for update in actuator.updates)


def test_run_without_velocity_telemetry_falls_back_to_neutralized_pulses():
    adapter, actuator, _ = fast_adapter()
    result = adapter.run(dry_run=False, max_cycles=2)
    assert result['reason'] == 'control_cycle_bound_reached'
    assert not actuator.updates and len(actuator.commands) == 2
    assert actuator.neutral


@pytest.mark.parametrize('change', [dict(lease_active=False), dict(actor_id='different'),
    dict(telemetry_age_seconds=2)])
def test_fast_follow_hold_still_stops_for_lost_lease_identity_or_freshness(change):
    observations = [moving(sequence=i) for i in (1, 2, 3)] + [moving(sequence=4, **change)]
    adapter, actuator, _ = fast_adapter(observations)
    adapter.follower = SlowBandFollower(adapter.route_plan)
    result = adapter.run(dry_run=False, max_cycles=2)
    assert len(actuator.updates) == 1, result['reason']
    assert actuator.neutral
    assert result['state'] != 'FOLLOWING'


# -- mission and NPC approach plumbing -----------------------------------------

@pytest.mark.parametrize('changes', [dict(), dict(controller_mode='continuous', sprint=True),
    dict(controller_mode='continuous', normal_stick_magnitude=.8), dict(fast_follow='yes')])
def test_mission_config_rejects_inconsistent_fast_follow(changes):
    values = dict(fast_follow=True)
    values.update(changes)
    with pytest.raises(ValueError):
        MissionConfig(**values)


def feature_runtime(profile='FEATURE_VALIDATION'):
    runtime = Runtime()

    async def rpc(op, args):
        result = await runtime.call(op, args)
        if op == 'get_runtime_identity':
            result['result']['deployment_profile'] = profile
        return result
    return runtime, rpc


def run_mission(tmp_path, config, profile='FEATURE_VALIDATION'):
    runtime, rpc = feature_runtime(profile)
    mission = NavigationMission(rpc, tmp_path / 'mission', identity_check=lambda *_: {'ok': True},
        config=config, clock=lambda: runtime.time, sleep=runtime.sleep, progress=lambda _: None)
    return runtime, asyncio.run(mission.run())


def test_mission_forwards_fast_follow_profile_and_wider_camera_deadband(tmp_path):
    runtime, result = run_mission(tmp_path, MissionConfig(execute=True, controller_mode='continuous',
        fast_follow=True, camera_follow_enabled=True))
    assert result['ok']
    args = next(a for op, a in runtime.calls if op == 'start_live_route_adapter')
    assert args['controller_mode'] == 'continuous'
    assert args['controller_policy_profile'] == FAST_FOLLOW_PROFILE
    assert args['continuous_fast_follow'] is True
    assert args['normal_stick_magnitude'] == 1.0 and args['sprint'] is False
    assert args['camera_follow_deadband_deg'] == FAST_FOLLOW_CAMERA_DEADBAND_DEG
    assert result['controller_policy']['fast_follow'] is True


def test_default_mission_arguments_are_unchanged(tmp_path):
    runtime, result = run_mission(tmp_path, MissionConfig(execute=True, camera_follow_enabled=True))
    assert result['ok']
    args = next(a for op, a in runtime.calls if op == 'start_live_route_adapter')
    assert args['controller_mode'] == 'pulse'
    assert 'continuous_fast_follow' not in args
    assert 'camera_follow_deadband_deg' not in args
    assert 'controller_policy_profile' not in args


def test_fast_follow_mission_runs_in_the_canonical_runtime(tmp_path):
    runtime, result = run_mission(tmp_path, MissionConfig(execute=True, controller_mode='continuous',
        fast_follow=True), profile='CANONICAL')
    assert result['ok']
    args = next(a for op, a in runtime.calls if op == 'start_live_route_adapter')
    assert args['controller_policy_profile'] == FAST_FOLLOW_PROFILE


def test_npc_approach_builds_a_fast_follow_mission_only_when_asked(tmp_path, monkeypatch):
    from ff7r_agent import npc_approach
    from test_npc_approach import ScriptedApproach, no_sleep, fake_control_lease

    configs = []

    class FakeMission:
        def __init__(self, rpc, output, identity_check, config, client_id=None):
            configs.append(config)

        async def run(self):
            return {'ok': True, 'state': 'ARRIVED'}

    monkeypatch.setattr(npc_approach, 'NavigationMission', FakeMission)
    for fast_follow in (False, True):
        # Within talking range: Talk counts only near the NPC being approached.
        runtime = ScriptedApproach({'x': 300.0, 'y': 0.0, 'z': 0.0}, talk_at={'x': 300.0, 'y': 0.0})
        report = asyncio.run(npc_approach.approach_npc(fake_control_lease(runtime.rpc), 'Ned', output=tmp_path / f'out-{fast_follow}',
            identity_check=None, execute=True, fast_follow=fast_follow, sleep=no_sleep,
            model_id="gpt-6.1-sol", reasoning_effort="xhigh"))
        assert report['fast_follow'] is fast_follow
    assert [(c.fast_follow, c.controller_mode, c.normal_stick_magnitude) for c in configs] == [
        (False, 'pulse', 1.0), (True, 'continuous', 1.0)]
    assert all(c.camera_follow_enabled for c in configs)


def cli_options(monkeypatch, module, runner, argv):
    """Run a CLI's main() with its HTTP runner replaced; return what it passed."""
    seen = {}
    async def fake(*args, **kwargs):
        seen.update(kwargs, args=args)
        return {'ok': True}
    monkeypatch.setattr(module, runner, fake)
    monkeypatch.setattr(module, 'feature_identity_check', lambda *args: None)
    if hasattr(module, 'canonical_identity_check'):
        monkeypatch.setattr(module, 'canonical_identity_check', lambda *args: None)
    module.main(argv)
    return seen


@pytest.mark.parametrize('argv, pursuit', [
    ([], True),
    (['--no-pursuit'], False),
    (['--pursuit'], True),
    (['--sprint', '--feature-ref', 'f', '--expected-commit', 'c'], False),
])
def test_npc_approach_cli_defaults_to_pursuit(monkeypatch, argv, pursuit):
    from ff7r_agent import npc_approach
    options = cli_options(monkeypatch, npc_approach, 'run_http', ['--name', 'Ned', *argv])
    assert options['pursuit'] is pursuit


@pytest.mark.parametrize('argv, pursuit, mode', [
    ([], True, 'continuous'),
    (['--no-pursuit'], False, 'pulse'),
    (['--controller-mode', 'pulse'], False, 'pulse'),
    (['--normal-stick-magnitude', '0.8', '--feature-ref', 'f', '--expected-commit', 'c'], False, 'pulse'),
    (['--target-id', 'npc:1'], False, 'pulse'),
])
def test_navigation_mission_cli_defaults_to_pursuit(monkeypatch, argv, pursuit, mode):
    from ff7r_agent import navigation_mission
    options = cli_options(monkeypatch, navigation_mission, 'run_http_mission', argv)
    config = options['args'][0]
    assert (config.pursuit, config.controller_mode, config.fast_follow) == (pursuit, mode, pursuit)


def test_pursuit_cli_refuses_sprint(monkeypatch):
    from ff7r_agent import npc_approach
    with pytest.raises(SystemExit):
        cli_options(monkeypatch, npc_approach, 'run_http',
                    ['--name', 'Ned', '--pursuit', '--sprint', '--feature-ref', 'f', '--expected-commit', 'c'])


def test_fast_follow_never_holds_for_less_than_the_pulse_it_replaces():
    fast, _, _ = fast_adapter()
    plain, _, _ = stream_adapter()
    normal = {'control_band': 'normal', 'desired_world_heading_deg': 0, 'command_duration_ms': 300}
    # 7 m from the +x edge: only a half (0.2 s) lease fits the corridor.
    near = moving(position=Vec3(700, 0, 0))
    assert plain._supervised_deadline(near, normal, {'distance_m': 4})['lease_seconds'] == pytest.approx(.2)
    assert fast._supervised_deadline(near, normal, {'distance_m': 4}) is None
    # Fast follow's hold budget (age + hold <= 0.75 s, ray proof over both)
    # keeps a 0.25 s-old sample holding (live: median age 0.18 s turned most
    # commands into pulses under the 0.5 s cap); 0.45 s leaves < 300 ms.
    aged = moving(telemetry_age_seconds=.25)
    assert plain._supervised_deadline(aged, normal, {'distance_m': 4}) is not None
    assert fast._supervised_deadline(aged, normal, {'distance_m': 4})['lease_seconds'] >= .3
    older = moving(telemetry_age_seconds=.45)
    assert fast._supervised_deadline(older, normal, {'distance_m': 4}) is None
    full = fast._supervised_deadline(moving(), normal, {'distance_m': 4})
    assert full['lease_seconds'] == pytest.approx(.4) and full['minimum_hold_seconds'] == pytest.approx(.3)


def test_run_sends_the_ordinary_pulse_where_only_a_short_hold_is_proven():
    observations = [moving(sequence=i, position=Vec3(700, 0, 0)) for i in range(1, 8)]
    adapter, actuator, _ = fast_adapter(observations)
    result = adapter.run(dry_run=False, max_cycles=2)
    assert result['reason'] == 'control_cycle_bound_reached'
    assert not actuator.updates
    assert [c['milliseconds'] for c in actuator.commands] == [300, 300]
    assert actuator.neutral
