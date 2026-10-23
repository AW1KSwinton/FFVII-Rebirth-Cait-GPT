import asyncio
import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ff7r_agent.entity_interaction import (
    offset_goal, ready, resolve_entity, validate_sample, verification_signals,
)
from ff7r_agent.entity_mission import EntityInteractionMission, EntityMissionConfig
from ff7r_agent.models import GameState
from ff7r_agent.navigation_mission import MissionError
from ff7r_agent.runtime_commands import invoke_runtime
from ff7r_agent.safety import ArmError, SessionLease
from test_agent import FakeArm, FakeEnvironment, FakeRealInput, make_real_service
from test_navigation_mission import Runtime


def entity(kind='npc', x=1000, **changes):
    return dict(id='live-entity', kind=kind, position=dict(x=x, y=0, z=65),
                distance=1.5, usable=True, prompt='Talk', opened=False, **changes)


def snapshot(sequence=1, x=850, target=None):
    return dict(sequence=sequence, source='file', timestamp_utc=datetime.now(timezone.utc).isoformat(),
                running=True, paused=False, in_menu=False, in_combat=False, loading=False,
                map_id='EndGame', telemetry=dict(live=True, age_seconds=0.01),
                player=dict(actor_id='Cloud-1', name='Cloud', hp=10, max_hp=10, mp=2, max_mp=2,
                            position=dict(x=x, y=0, z=65), rotation=dict(yaw=0, pitch=0, roll=0)),
                nearby_interactables=[target or entity()], nearby_actors=[], nearby_entities_complete=True)


@pytest.mark.parametrize('feed', ['nearby_interactables', 'nearby_actors', 'nearby_interactable'])
@pytest.mark.parametrize('kind', ['npc', 'bench', 'chest', 'vending_machine', 'door'])
def test_resolves_semantic_kinds_from_live_feeds(feed, kind):
    state = snapshot(target=entity(kind))
    state['nearby_interactables'] = []
    state[feed] = entity(kind) if feed == 'nearby_interactable' else [entity(kind)]
    assert resolve_entity(state, target_kind=kind.upper())['id'] == 'live-entity'
    assert resolve_entity(state, target_id='live-entity')['kind'] == kind


def test_exact_id_disambiguates_kind_but_duplicate_or_conflicting_ids_refuse():
    state = snapshot()
    other = copy.deepcopy(entity())
    other['id'] = 'other'
    state['nearby_interactables'].append(other)
    with pytest.raises(MissionError, match='ambiguous'):
        resolve_entity(state, target_kind='npc')
    assert resolve_entity(state, 'live-entity')['id'] == 'live-entity'
    state['nearby_actors'] = [entity()]
    assert resolve_entity(state, 'live-entity')['id'] == 'live-entity'
    state['nearby_actors'][0]['usable'] = False
    with pytest.raises(MissionError, match='conflicting'):
        resolve_entity(state, 'live-entity')
    state = snapshot()
    state['nearby_interactables'].append(entity())
    with pytest.raises(MissionError, match='duplicate'):
        resolve_entity(state, 'live-entity')


@pytest.mark.parametrize('mutation', [
    lambda s: s['telemetry'].update(live=False),
    lambda s: s['telemetry'].update(age_seconds=0.6),
    lambda s: s['telemetry'].update(age_seconds=float('nan')),
    lambda s: s.update(source='simulation'),
    lambda s: s.update(sequence=0),
    lambda s: s.update(in_menu=True),
    lambda s: s['player'].update(actor_id=None),
])
def test_stale_unsafe_and_nonadvancing_samples_refuse(mutation):
    state = snapshot()
    mutation(state)
    with pytest.raises(MissionError):
        validate_sample(state, 0)


def test_relative_staging_and_final_coordinates_rotate_with_target():
    state = snapshot(x=0)
    target = entity(x=0)
    target['position']['y'] = 1000
    assert offset_goal(state, target, 4)['y'] == pytest.approx(600)
    assert offset_goal(state, target, 1.25)['y'] == pytest.approx(875)
    assert offset_goal(state, target, 4)['x'] == pytest.approx(0, abs=1e-10)


@pytest.mark.parametrize('change', [{'usable': False}, {'usable': None, 'prompt': None},
                                  {'prompt': ''}, {'opened': True}, {'visible': False}])
def test_interaction_needs_positive_affordance(change):
    target = entity()
    target.update(change)
    assert not ready(snapshot(), target)


def model(state):
    payload = {**state, 'schema_version': 1,
               'game': {k: state[k] for k in ('running', 'paused', 'in_menu', 'in_combat', 'map_id', 'loading')}}
    return GameState.from_dict(payload)


def test_parser_preserves_actor_prompt_opened_and_explicit_state_evidence():
    state = snapshot()
    state['nearby_actors'] = state.pop('nearby_interactables')
    state['interaction'] = {'state': 'dialogue', 'target_id': 'live-entity'}
    parsed = model(state).to_dict()
    assert parsed['nearby_actors'][0]['prompt'] == 'Talk'
    assert parsed['nearby_actors'][0]['opened'] is False
    assert parsed['interaction']['state'] == 'dialogue'
    assert parsed['nearby_entities_complete'] is True
    state = snapshot()
    del state['nearby_interactables'][0]['usable']
    del state['nearby_interactables'][0]['prompt']
    assert not ready(model(state).to_dict(), resolve_entity(model(state).to_dict(), 'live-entity'))
    state['nearby_actors'] = [dict(entity(), usable='false')]
    assert not model(state).nearby_actors


class RearmingArm(FakeArm):
    def arm(self, seconds):
        self.armed, self.remaining = True, seconds


class Environment(FakeEnvironment):
    def game_process_ids(self):
        return {6932: 'ff7rebirth_.exe'} if self.process else {}


class EntityTelemetry:
    def __init__(self, backend):
        self.backend = backend
        self.seq, self.x, self.target_x = 0, 850, 1000
        self.change_before = lambda s: None
        self.change_after = lambda s: s.update(in_menu=True)
        self.frozen = False

    def observe(self):
        if not self.frozen:
            self.seq += 1
        state = snapshot(self.seq, self.x, entity(x=self.target_x))
        if self.backend.actions:
            self.change_after(state)
        else:
            self.change_before(state)
        return model(state)


def service_fixture(tmp_path, *, open_lease=True):
    backend = FakeRealInput()
    telemetry = EntityTelemetry(backend)
    environment = Environment()
    service, _ = make_real_service(telemetry=telemetry, backend=backend, arm=RearmingArm(),
                                  environment=environment, sleeper=lambda _: None)
    lease = SessionLease(runtime_dir=tmp_path / 'lease')
    if open_lease:
        lease.open(60, 6932, 'mission-client')
    service._session_lease = lease
    return service, backend, telemetry


def interact(service, **changes):
    arguments = dict(target_id='live-entity', target_kind='npc', expected_position=entity()['position'],
                     expected_map_id='EndGame', expected_actor_id='Cloud-1', expected_world={},
                     after_sequence=0, client_id='mission-client', game_pid=6932)
    arguments.update(changes)
    return invoke_runtime(service, None, None, 'interact_with_entity', arguments)


def test_worker_integration_sends_one_press_verifies_and_neutralises(tmp_path):
    service, backend, telemetry = service_fixture(tmp_path)
    result = interact(service)
    assert result['ok'] and result['verification']['signals'] == ['menu_opened']
    assert backend.actions == [('interaction', 250)]
    assert backend.release_count >= 2 and service._arm.stopped
    assert service._action_lock.acquire(blocking=False)
    service._action_lock.release()
    # Token persists in the same durable lease across a newly constructed reader.
    reloaded = SessionLease(runtime_dir=service._session_lease.runtime_dir)
    with pytest.raises(ArmError, match='already_consumed'):
        reloaded.consume_entity_interaction('mission-client', 6932, 'other-target')


@pytest.mark.parametrize('kind,mutation,signal', [
    ('npc', lambda s: s.update(interaction={'state': 'dialogue', 'target_id': 'live-entity'}), 'interaction_state_changed'),
    ('door', lambda s: s.update(loading=True), 'loading_started'),
    ('chest', lambda s: s['nearby_interactables'][0].update(opened=True), 'target_opened'),
    ('vending_machine', lambda s: s['nearby_interactables'][0].update(usable=False), 'target_became_unusable'),
    ('npc', lambda s: s['nearby_interactables'][0].update(prompt=''), 'target_prompt_changed'),
    ('chest', lambda s: s.update(nearby_interactables=[]), 'target_disappeared_from_complete_feed'),
    ('bench', lambda s: s.update(rest=dict(state='resting', bench_id='live-entity', state_identity='Cloud-1', interaction_id='new')), 'bench_lifecycle_changed'),
    ('door', lambda s: s.update(map_id='next', loading=True), 'world_transition'),
])
def test_positive_observable_transitions(tmp_path, kind, mutation, signal):
    service, backend, telemetry = service_fixture(tmp_path)
    def with_kind(state):
        state['nearby_interactables'][0]['kind'] = kind
    telemetry.change_before = with_kind
    def after(state):
        with_kind(state)
        mutation(state)
    telemetry.change_after = after
    result = interact(service, target_kind=kind)
    assert result['ok'], result
    assert signal in result['verification']['signals']
    assert len(backend.actions) == 1


@pytest.mark.parametrize('mutation', [
    lambda s: None,
    lambda s: s.update(paused=True),
    lambda s: s.update(nearby_interactables=[], nearby_entities_complete=False),
    lambda s: (s['nearby_interactables'][0].update(usable=False), s['player']['position'].update(x=900)),
    lambda s: s.update(rest=dict(state='interaction_started', bench_id='live-entity', state_identity='Cloud-1', interaction_id='synthetic')),
])
def test_no_success_from_key_send_pause_drift_or_missing_feed(tmp_path, mutation):
    service, backend, telemetry = service_fixture(tmp_path)
    telemetry.change_after = mutation
    result = interact(service)
    assert not result['ok'] and result['reason'] == 'interaction_unverified'
    assert result['interaction_sent'] and len(backend.actions) == 1
    assert backend.release_count >= 2


@pytest.mark.parametrize('mutation,error', [
    (lambda s: s['nearby_interactables'][0]['position'].update(x=1200), 'moved'),
    (lambda s: s.update(nearby_interactables=[]), 'not_observed'),
    (lambda s: s['nearby_interactables'][0].update(usable=False), 'not_usable'),
    (lambda s: s['nearby_interactables'].append(dict(entity(), id='competitor')), 'prompt_ambiguous'),
    (lambda s: s['player'].update(actor_id='replacement'), 'world_or_player'),
    (lambda s: s.update(in_combat=True), 'active_gameplay'),
    (lambda s: s['player']['rotation'].update(yaw=180), 'not_usable'),
])
def test_worker_rechecks_target_and_safety_before_any_press(tmp_path, mutation, error):
    service, backend, telemetry = service_fixture(tmp_path)
    telemetry.change_before = mutation
    result = interact(service)
    assert not result['ok'] and error in result['reason'], result
    assert not backend.actions and backend.release_count >= 1


def test_frozen_second_sample_never_authorizes_interaction(tmp_path):
    service, backend, telemetry = service_fixture(tmp_path)
    def freeze(state):
        telemetry.frozen = True
    telemetry.change_before = freeze
    result = interact(service)
    assert 'not_advancing' in result['reason'] and not backend.actions


def test_response_uncertainty_consumes_token_before_input_exception(tmp_path):
    service, backend, telemetry = service_fixture(tmp_path)
    def fail(milliseconds):
        backend.actions.append(('interaction', milliseconds))
        raise OSError('release response lost')
    backend.use_interaction = fail
    result = interact(service)
    assert not result['ok'] and result['interaction_attempted'] and len(backend.actions) == 1
    telemetry.change_after = lambda s: None
    result = interact(service)
    assert not result['ok'] and len(backend.actions) == 1
    assert service._session_lease.status()['entity_interaction']['target_id'] == 'live-entity'


@pytest.mark.parametrize('mutation', [lambda s: s.update(in_menu=True, interaction={'state': 'dialogue', 'target_id': 'wrong'}),
                                      lambda s: s.update(map_id='unexplained'),
                                      lambda s: s.update(in_combat=True)])
def test_post_input_wrong_target_or_unexplained_state_stops(tmp_path, mutation):
    service, backend, telemetry = service_fixture(tmp_path)
    telemetry.change_after = mutation
    assert not interact(service)['ok']
    assert len(backend.actions) == 1


class EntityRuntime(Runtime):
    """Mock route transport, real worker dispatch/telemetry/lease/interaction."""
    def __init__(self, tmp_path):
        super().__init__()
        self.service, self.backend, self.telemetry = service_fixture(tmp_path, open_lease=False)
        self.telemetry.x = 0
        self.route_goals = []
        self.mutate_route = lambda r: None
        self.lose_interact_response = False

    async def call(self, op, args):
        if op == 'observe_game':
            self.calls.append((op, args))
            return self.service.observe()
        if op == 'interact_with_entity':
            self.calls.append((op, args))
            value = invoke_runtime(self.service, None, None, op, args)
            if self.lose_interact_response:
                raise OSError('lost interaction response')
            return {'id': 123, 'ok': True, 'result': value}
        if op == 'get_control_session_status':
            self.calls.append((op, args))
            return self.service._session_lease.status() or {'active': False}
        if op == 'open_control_session':
            self.calls.append((op, args))
            return self.service._session_lease.open(args['seconds'], args['game_pid'], args['client_id'])
        if op == 'close_control_session':
            self.calls.append((op, args))
            if self.service._session_lease.status():
                return self.service._session_lease.close()
            return {'closed': False}
        if op == 'stop_all_input':
            self.calls.append((op, args))
            self.backend.release_all()
            return {'ok': not self.fail_cleanup}
        value = await super().call(op, args)
        if op == 'start_live_route_adapter':
            self.route_goals.append(args['route_target_position'])
            if not args['dry_run']:
                self.telemetry.x = args['route_target_position']['x']
            self.mutate_route(self)
        return value


def mission(runtime, tmp_path, **config):
    client = EntityInteractionMission(runtime.call, tmp_path / 'mission', config=EntityMissionConfig(
        **{'target_kind': 'npc', 'execute': True, 'timeout_seconds': 60, **config}), identity_check=lambda *a: {'ok': True},
        clock=lambda: runtime.time, sleep=runtime.sleep, progress=lambda _: None)
    return asyncio.run(client.run())


def test_mission_stages_reacquires_and_uses_existing_route_for_final_approach(tmp_path):
    runtime = EntityRuntime(tmp_path)
    result = mission(runtime, tmp_path)
    assert result['ok'], result
    assert [p['x'] for p in runtime.route_goals] == [600, 875]
    assert len(result['reacquisitions']) == 2
    assert runtime.backend.actions == [('interaction', 250)]
    assert result['verification']['signals'] == ['menu_opened']
    assert [op for op, _ in runtime.calls][-2:] == ['stop_all_input', 'close_control_session']
    assert not runtime.service._session_lease.status()
    assert not set(op for op, _ in runtime.calls) & {'move_stick', 'move_direction', 'tap_button'}
    assert (tmp_path / 'mission' / 'sha256.json').exists()


def test_small_target_move_replans_relative_to_reacquired_same_entity(tmp_path):
    runtime = EntityRuntime(tmp_path)
    runtime.mutate_route = lambda r: setattr(r.telemetry, 'target_x', 1100)
    assert mission(runtime, tmp_path)['ok']
    assert runtime.route_goals[-1]['x'] == 975


@pytest.mark.parametrize('mutation,reason', [
    (lambda r: setattr(r.telemetry, 'target_x', 1600), 'moved_outside_bound'),
    (lambda r: setattr(r.telemetry, 'change_before', lambda s: s.update(nearby_interactables=[])), 'not_observed'),
    (lambda r: setattr(r.telemetry, 'change_before', lambda s: s['nearby_interactables'][0].update(id='replacement')), 'not_observed'),
    (lambda r: setattr(r.telemetry, 'frozen', True), 'not_advancing'),
])
def test_staging_reacquisition_refuses_moved_disappeared_replaced_or_frozen(tmp_path, mutation, reason):
    runtime = EntityRuntime(tmp_path)
    runtime.mutate_route = mutation
    result = mission(runtime, tmp_path)
    assert not result['ok'] and reason in result['reason'], result
    assert not runtime.backend.actions and len(runtime.route_goals) == 1


def test_already_near_target_skips_movement_but_rechecks_and_verifies(tmp_path):
    runtime = EntityRuntime(tmp_path)
    runtime.telemetry.x = 850
    assert mission(runtime, tmp_path)['ok']
    assert not runtime.route_goals and len(runtime.backend.actions) == 1


def test_mission_never_retries_lost_interaction_response(tmp_path):
    runtime = EntityRuntime(tmp_path)
    runtime.lose_interact_response = True
    result = mission(runtime, tmp_path)
    assert not result['ok'] and result['interaction_attempted']
    assert len(runtime.backend.actions) == 1
    assert sum(op == 'interact_with_entity' for op, _ in runtime.calls) == 1
    assert not runtime.service._session_lease.status()


def test_final_positioning_cannot_loop_indefinitely(tmp_path):
    runtime = EntityRuntime(tmp_path)
    runtime.mutate_route = lambda r: setattr(r.telemetry, 'x', 600)
    result = mission(runtime, tmp_path)
    assert not result['ok'] and 'positioning_bound' in result['reason']
    assert len(runtime.route_goals) == 4 and not runtime.backend.actions
    final_calls = [args for op, args in runtime.calls if op == 'start_live_route_adapter'][1:]
    assert all(args['max_cycles'] <= 32 and args['cached_topology_wall_clock_seconds'] <= 60 for args in final_calls)


def test_lease_token_rejects_wrong_client_and_survives_heartbeat(tmp_path):
    lease = SessionLease(runtime_dir=tmp_path)
    lease.open(60, 6932, 'client')
    with pytest.raises(ArmError, match='mismatch'):
        lease.consume_entity_interaction('other', 6932, 'target')
    lease.consume_entity_interaction('client', 6932, 'target')
    lease.heartbeat(60)
    with pytest.raises(ArmError, match='already_consumed'):
        lease.consume_entity_interaction('client', 6932, 'target')


def test_entity_dry_run_never_moves_or_interacts(tmp_path):
    runtime = EntityRuntime(tmp_path)
    result = mission(runtime, tmp_path, execute=False)
    assert result['ok'] and result['state'] == 'PREFLIGHT_PASSED'
    assert not result['movement_attempted'] and not result['interaction_attempted']
    assert not runtime.backend.actions and runtime.telemetry.x == 0
    assert len(runtime.route_goals) == 1


def test_failed_verification_preserves_actual_sent_evidence(tmp_path):
    runtime = EntityRuntime(tmp_path)
    runtime.telemetry.change_after = lambda s: None
    result = mission(runtime, tmp_path)
    assert not result['ok'] and result['reason'] == 'interaction_unverified'
    assert result['interaction_sent'] is True
    assert result['interaction']['interaction_attempted']
    assert len(runtime.backend.actions) == 1


def test_lost_response_reports_send_status_unknown(tmp_path):
    runtime = EntityRuntime(tmp_path)
    runtime.lose_interact_response = True
    result = mission(runtime, tmp_path)
    assert result['interaction_sent'] is None


@pytest.mark.parametrize('failure', ['stale', 'repeat', 'process', 'foreground', 'session'])
def test_verification_failures_neutralise_without_replay(tmp_path, failure):
    service, backend, telemetry = service_fixture(tmp_path)
    def after(state):
        state['in_menu'] = True
        if failure == 'stale':
            state['timestamp_utc'] = '2000-01-01T00:00:00+00:00'
        elif failure == 'repeat':
            state['sequence'] = 2
        elif failure == 'process':
            service._environment.process = False
        elif failure == 'foreground':
            service._environment.foreground = False
        else:
            service._session_lease.close()
    telemetry.change_after = after
    result = interact(service)
    assert not result['ok'] and len(backend.actions) == 1
    assert backend.release_count >= 2 and service._arm.stopped


def test_one_observation_of_transition_is_not_enough(tmp_path):
    service, backend, telemetry = service_fixture(tmp_path)
    telemetry.change_after = lambda s: s.update(in_menu=s['sequence'] % 2 == 0)
    result = interact(service)
    assert not result['ok'] and result['reason'] == 'interaction_unverified'
    assert len(backend.actions) == 1


def test_entity_changed_during_worker_admission_never_sends(tmp_path):
    service, backend, telemetry = service_fixture(tmp_path)
    def before(state):
        if state['sequence'] >= 2:
            state['nearby_interactables'][0]['position']['x'] += 50
    telemetry.change_before = before
    result = interact(service)
    assert not result['ok'] and 'moved' in result['reason'] and not backend.actions


@pytest.mark.parametrize('component', ['input', 'arm'])
def test_worker_cleanup_failure_unlocks_and_cannot_claim_success(tmp_path, component):
    service, backend, telemetry = service_fixture(tmp_path)
    if component == 'input':
        original = backend.release_all
        def fail_release():
            original()
            if backend.actions:
                raise OSError('neutralisation failed')
        backend.release_all = fail_release
    else:
        service._arm.latch_stop = lambda: (_ for _ in ()).throw(OSError('arm write failed'))
    result = interact(service)
    assert not result['ok'] and result['state'] == 'CLEANUP_FAILED'
    assert service._action_lock.acquire(blocking=False)
    service._action_lock.release()
    assert len(backend.actions) == 1


def test_token_persistence_delay_cannot_authorize_late_input(tmp_path):
    service, backend, telemetry = service_fixture(tmp_path)
    clock = [100.0]
    service._monotonic = lambda: clock[0]
    original = service._session_lease.consume_entity_interaction
    def delayed(*args, **kwargs):
        original(*args, **kwargs)
        clock[0] += 11
    service._session_lease.consume_entity_interaction = delayed
    result = interact(service)
    assert not result['ok'] and 'admission_expired' in result['reason']
    assert not backend.actions
    assert service._session_lease.status()['entity_interaction']


def test_invalid_entity_record_cannot_prove_disappearance():
    before = snapshot()
    after = snapshot(2)
    after['nearby_interactables'][0]['opened'] = 'invalid'
    after = model(after).to_dict()
    assert not after['nearby_entities_complete']
    assert not verification_signals(before, after, entity())


def test_total_mission_movement_budget_applies_to_final_routes(tmp_path):
    runtime = EntityRuntime(tmp_path)
    result = mission(runtime, tmp_path, max_cycles=1)
    assert not result['ok'] and 'movement_budget' in result['reason']
    assert len(runtime.route_goals) == 1 and not runtime.backend.actions


def test_mission_cleanup_failure_overrides_verified_success(tmp_path):
    runtime = EntityRuntime(tmp_path)
    runtime.fail_cleanup = True
    result = mission(runtime, tmp_path)
    assert not result['ok'] and result['state'] == 'CLEANUP_FAILED'
    assert result['verification']['signals'] == ['menu_opened']
    assert not runtime.service._session_lease.status()


def test_entity_mission_borrows_another_clients_session_and_leaves_it_open(tmp_path):
    runtime = EntityRuntime(tmp_path)
    runtime.service._session_lease.open(60, 6932, 'another-task')
    result = mission(runtime, tmp_path)
    assert result['ok'], result
    assert result['control_session'] == 'borrowed' and result['cleanup'] == {'session': 'left_open'}
    # The interaction is admitted under the session's own client, and the
    # session is neither closed nor stopped: it is the agent's.
    sent = next(args for op, args in runtime.calls if op == 'interact_with_entity')
    assert sent['client_id'] == 'another-task'
    assert not {'open_control_session', 'close_control_session', 'stop_all_input'} & {op for op, _ in runtime.calls}
    assert runtime.service._session_lease.status()['active']
    assert runtime.service._session_lease.status()['client_id'] == 'another-task'


def test_two_entity_missions_use_one_session_one_after_the_other(tmp_path):
    runtime = EntityRuntime(tmp_path)
    runtime.service._session_lease.open(60, 6932, 'agent')
    assert mission(runtime, tmp_path)['ok']
    runtime.telemetry.x, runtime.backend.actions = 0, []
    runtime.service._monotonic = lambda: 200.0  # past the minimum delay between actions
    runtime.calls = []
    second = mission(runtime, tmp_path.parent / (tmp_path.name + '-second'))
    assert second['ok'], second
    assert runtime.service._session_lease.status()['active']


@pytest.mark.parametrize('config', [dict(target_id='a', sprint=True, controller_mode='continuous', normal_stick_magnitude=0.8),
                                    dict(target_kind=' '), dict(target_id='a', execute='yes')])
def test_invalid_entity_configuration_refuses(config):
    with pytest.raises(ValueError):
        EntityMissionConfig(**config)


def test_cli_dispatches_entity_mission_without_bypassing_identity_options(monkeypatch, tmp_path):
    from ff7r_agent import navigation_mission as module
    captured = {}
    async def fake_http(config, output, manifest, **kwargs):
        captured.update(config=config, output=output, **kwargs)
        return {'ok': True}
    monkeypatch.setattr(module, 'run_http_mission', fake_http)
    assert module.main(['--target-id', 'live-entity', '--output', str(tmp_path / 'evidence')]) == 0
    assert isinstance(captured['config'], EntityMissionConfig)
    assert captured['mission_class'] is EntityInteractionMission
    assert captured['config'].execute is False


def test_numeric_prompt_is_not_positive_interaction_evidence():
    state = snapshot()
    state['nearby_interactables'][0].update(usable=None, prompt=123)
    parsed = model(state)
    assert not parsed.nearby_interactables
    assert not parsed.nearby_entities_complete


@pytest.mark.parametrize('positioned', [True, False])
def test_competing_prompt_behind_player_or_without_geometry_is_ambiguous(tmp_path, positioned):
    service, backend, telemetry = service_fixture(tmp_path)
    def before(state):
        other = entity(x=700)
        other['id'] = 'other'
        if not positioned:
            del other['position']
        state['nearby_interactables'].append(other)
    telemetry.change_before = before
    result = interact(service)
    assert not result['ok'] and not backend.actions


def test_cleanup_does_not_stop_a_replacement_clients_session(tmp_path):
    runtime = EntityRuntime(tmp_path)
    original = runtime.call
    async def replacement(op, args):
        result = await original(op, args)
        if op == 'interact_with_entity':
            runtime.service._session_lease.close()
            runtime.service._session_lease.open(60, 6932, 'replacement-client')
        return result
    runtime.call = replacement
    result = mission(runtime, tmp_path)
    assert not result['ok'] and result['reason'] == 'entity_cleanup_session_changed'
    assert not any(op == 'stop_all_input' for op, _ in runtime.calls)
    assert runtime.service._session_lease.status()['client_id'] == 'replacement-client'
    assert len(runtime.backend.actions) == 1
