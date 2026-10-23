from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ff7r_agent.live_route_adapter import (
    AdapterError,
    AgentServiceTelemetrySource,
)
from ff7r_agent.models import Character, GameState, Rotation, Vec3
from ff7r_agent.service import AgentService, SafetyError


class ProofInput:
    is_real = True

    def backend_status(self):
        return {"stick_state": "neutral", "last_stick": {"x": 0.0, "y": 0.0}}


class ProofEnvironment:
    def __init__(self):
        self.process = True
        self.foreground = True

    def game_process_exists(self):
        return self.process

    def game_is_foreground(self):
        return self.foreground

    def game_process_ids(self):
        return {123: "ff7rebirth_.exe"} if self.process else {}


class ProofClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class SequenceTelemetry:
    def __init__(self, states):
        self.states = list(states)
        self.index = 0

    def observe(self):
        state = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        return state


def gameplay_state(
    sequence,
    *,
    timestamp_utc=None,
    running=True,
    paused=False,
    in_menu=False,
    map_id="EndGame",
    player=True,
    actor_id="CloudActor",
):
    timestamp_utc = timestamp_utc or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    character = None
    if player:
        character = Character(
            "Cloud", 100, 100, 10, 10,
            position=Vec3(1.0, 2.0, 3.0),
            rotation=Rotation(0.0, 0.0, 0.0),
            controller_state=1,
            actor_id=actor_id,
        )
    return GameState(
        schema_version=1,
        sequence=sequence,
        timestamp_utc=timestamp_utc,
        running=running,
        paused=paused,
        in_menu=in_menu,
        in_combat=False,
        location="",
        objective="",
        map_id=map_id,
        player=character,
        target=None,
        source="test",
    )


def make_service(states, *, environment=None, clock=None, stale_gap_grace=None):
    environment = environment or ProofEnvironment()
    clock = clock or ProofClock()
    service = AgentService(
        SequenceTelemetry(states),
        ProofInput(),
        environment=environment,
        monotonic=clock,
        sleeper=lambda _: None,
        settled_gameplay_samples=3,
        settled_proof_stale_gap_grace_seconds=stale_gap_grace,
    )
    return service, environment, clock


def establish(service, states):
    return service._ensure_settled_gameplay(states[0], timeout_seconds=1.0)


def test_settled_proof_continuity_survives_twelve_simulated_seconds():
    initial = [gameplay_state(sequence) for sequence in (1, 2, 3)]
    service, _, clock = make_service(initial)
    establish(service, initial)
    for sequence in range(4, 16):
        clock.advance(1.0)
        health = service.session_health(gameplay_state(sequence))
        assert health["gameplay_ready"] is True
        assert health["gameplay_settled"] is True
        assert health["settled_proof_state"] == "continuity_refreshed"
        assert health["health_sample_sequence"] == sequence
    assert health["settled_proof_sequence"] == 15
    assert health["settled_proof_age_seconds"] == pytest.approx(0.0)


def test_repeated_sequence_does_not_refresh_proof_timestamp_or_sequence():
    states = [gameplay_state(sequence) for sequence in (1, 2, 3)]
    service, _, clock = make_service(states)
    establish(service, states)
    before_sequence = service._settled_gameplay_sequence
    before_at = service._settled_gameplay_at
    clock.advance(10.0)
    health = service.session_health(gameplay_state(3))
    assert health["gameplay_ready"] is True
    assert service._settled_gameplay_sequence == before_sequence
    assert service._settled_gameplay_at == before_at
    assert health["settled_proof_age_seconds"] == pytest.approx(10.0)


def test_stale_telemetry_invalidates_settled_proof():
    states = [gameplay_state(sequence) for sequence in (1, 2, 3)]
    service, _, _ = make_service(states)
    establish(service, states)
    stale = gameplay_state(
        4,
        timestamp_utc=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace(
            "+00:00", "Z"
        ),
    )
    health = service.session_health(stale)
    assert health["gameplay_ready"] is False
    assert health["settled_proof_present"] is False
    assert health["settled_proof_state"] == "invalidated"
    assert health["settled_proof_invalidation_reason"] == "telemetry_stale"


def test_foreground_loss_invalidates_and_one_returning_sample_is_insufficient():
    states = [gameplay_state(sequence) for sequence in (1, 2, 3)]
    service, environment, _ = make_service(states)
    establish(service, states)
    environment.foreground = False
    lost = service.session_health(gameplay_state(4))
    assert lost["gameplay_ready"] is False
    assert lost["settled_proof_invalidation_reason"] == "foreground_missing"
    environment.foreground = True
    returned = service.session_health(gameplay_state(5))
    assert returned["gameplay_ready"] is False
    assert returned["settled_proof_state"] == "reacquisition_required"
    assert returned["settled_proof_invalidation_reason"] == "foreground_missing"

    recovery_states = [gameplay_state(sequence) for sequence in (6, 7, 8)]
    service.telemetry = SequenceTelemetry(recovery_states)
    settled = service.wait_for_settled_gameplay(1.0)
    assert settled["stable_samples"] == 3
    assert service.session_health(gameplay_state(8))["gameplay_ready"] is True


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda state: gameplay_state(4, paused=True), "paused"),
        (lambda state: gameplay_state(4, in_menu=True), "menu"),
        (lambda state: gameplay_state(4, map_id="OtherMap"), "gameplay_identity_changed"),
        (lambda state: gameplay_state(4, player=False), "player_not_loaded"),
    ],
)
def test_real_gameplay_invalidation_reasons_clear_proof(change, reason):
    states = [gameplay_state(sequence) for sequence in (1, 2, 3)]
    service, environment, _ = make_service(states)
    establish(service, states)
    observed = change(states[-1])
    health = service.session_health(observed)
    assert health["gameplay_ready"] is False
    assert health["settled_proof_present"] is False
    assert health["settled_proof_invalidation_reason"] == reason
    if reason == "gameplay_identity_changed":
        assert health["loaded_gameplay"] is True
    if reason == "player_not_loaded":
        assert environment.process is True


def test_process_loss_remains_fail_closed_and_clears_proof():
    states = [gameplay_state(sequence) for sequence in (1, 2, 3)]
    service, environment, _ = make_service(states)
    establish(service, states)
    environment.process = False
    health = service.session_health(gameplay_state(4))
    assert health["gameplay_ready"] is False
    assert health["settled_proof_invalidation_reason"] == "process_missing"


def test_repeated_samples_do_not_count_as_settling_samples():
    states = [gameplay_state(sequence) for sequence in (1, 1, 2, 3)]
    service, _, _ = make_service(states)
    settled = service._ensure_settled_gameplay(states[0], timeout_seconds=1.0)
    assert settled.sequence == 3
    assert service._settled_gameplay_source == "multi_sample_established"


def test_generic_settling_threshold_can_accept_sample_older_than_movement_bound():
    old = (datetime.now(timezone.utc) - timedelta(seconds=0.6)).isoformat().replace(
        "+00:00", "Z"
    )
    states = [gameplay_state(sequence, timestamp_utc=old) for sequence in (1, 2, 3)]
    service, _, _ = make_service(states)

    settled = service.wait_for_settled_gameplay(1.0)

    assert settled["stable_samples"] == 3
    assert settled["max_telemetry_age_seconds"] == pytest.approx(2.0)


def test_movement_settling_rejects_samples_older_than_route_bound():
    old = (datetime.now(timezone.utc) - timedelta(seconds=0.6)).isoformat().replace(
        "+00:00", "Z"
    )
    states = [gameplay_state(sequence, timestamp_utc=old) for sequence in (1, 2, 3)]
    service, _, clock = make_service(states)
    service._sleeper = clock.advance

    with pytest.raises(SafetyError, match="gameplay_not_settled"):
        service.wait_for_settled_gameplay(
            1.0,
            max_telemetry_age_seconds=0.5,
        )


def test_movement_settling_reacquires_three_advancing_samples_at_route_bound():
    generic_old = (datetime.now(timezone.utc) - timedelta(seconds=0.6)).isoformat().replace(
        "+00:00", "Z"
    )
    fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    service, _, _ = make_service([
        gameplay_state(1, timestamp_utc=generic_old),
        gameplay_state(2, timestamp_utc=generic_old),
        gameplay_state(3, timestamp_utc=generic_old),
    ])
    service.wait_for_settled_gameplay(1.0)
    service.telemetry = SequenceTelemetry([
        gameplay_state(4, timestamp_utc=fresh),
        gameplay_state(5, timestamp_utc=fresh),
        gameplay_state(6, timestamp_utc=fresh),
    ])

    settled = service.wait_for_settled_gameplay(
        1.0,
        max_telemetry_age_seconds=0.5,
    )

    assert settled["sequence"] == 6
    assert settled["max_telemetry_age_seconds"] == pytest.approx(0.5)
    assert service._settled_gameplay_freshness_limit == pytest.approx(0.5)


class SourceLifecycle:
    def status(self):
        return {
            "run_context": {
                "run_id": "run-proof",
                "pid": 123,
                "executable_sha256": "37D952FABBFA2B710F8AE6490997C9755E4E57B4FD545078C9BAAF46C9B233D7",
            }
        }


def test_gameplay_settling_is_not_synthetic_loading_and_health_uses_same_sample():
    state = gameplay_state(10)
    service, _, _ = make_service([state])
    source = AgentServiceTelemetrySource(service, SourceLifecycle())
    observation = source.observe()
    assert observation.sequence == 10
    assert observation.loading is False
    assert observation.telemetry_gameplay_ready is False
    assert observation.raw["health"]["diagnosis"] == "gameplay_settling"
    assert observation.raw["health"]["health_sample_sequence"] == observation.sequence


def test_telemetry_health_sequence_mismatch_fails_closed():
    state = gameplay_state(10)

    class MismatchedService:
        telemetry = SimpleNamespace(observe=lambda: state)
        _environment = SimpleNamespace(game_process_ids=lambda: {123: "ff7rebirth_.exe"})

        def session_health(self, state=None):
            return {
                "process_exists": True,
                "gameplay_ready": True,
                "target_clear": True,
                "health_sample_sequence": 11,
            }

        def input_status(self, state=None):
            return {
                "backend_status": {"stick_state": "neutral"},
                "control_session": {"active": True},
            }

    with pytest.raises(AdapterError, match="telemetry_health_sequence_mismatch"):
        AgentServiceTelemetrySource(MismatchedService(), SourceLifecycle()).observe()


def test_loading_pose_cannot_establish_or_refresh_ground_readiness():
    from dataclasses import replace
    from ff7r_agent.models import LocomotionState
    def physical(sequence, grounded):
        state = gameplay_state(sequence)
        locomotion = LocomotionState(reference="upright_capsule_bottom", feet_position=Vec3(1, 2, -85),
            capsule_radius_cm=34, capsule_half_height_cm=88, velocity_cm_s=Vec3(0, 0, 0),
            grounded=grounded, movement_mode=None)
        return replace(state, player=replace(state.player, locomotion=locomotion))
    states = [physical(n, n >= 4) for n in range(1, 7)]
    service, _, _ = make_service(states)
    settled = service._ensure_settled_gameplay(states[0], timeout_seconds=1)
    assert settled.sequence == 6  # The three loaded-but-airborne samples do not count.
    assert service.session_health(physical(7, True))["gameplay_ready"]
    health = service.session_health(physical(8, False))
    assert not health["gameplay_ready"] and not health["physical_ground_ready"]
    assert health["settled_proof_invalidation_reason"] == "physical_ground_not_ready"
    # Grounded again in the same world (a step while running): the proof is
    # restored rather than needing Cloud to stand still to settle again.
    assert service.session_health(physical(9, True))["gameplay_ready"]
    # Any other invalidation still requires a full settle.
    service.session_health(replace(physical(10, True), paused=True))
    assert not service.session_health(physical(11, True))["gameplay_ready"]



def _aged(sequence, seconds, **changes):
    return gameplay_state(
        sequence,
        timestamp_utc=(datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat().replace(
            "+00:00", "Z"
        ),
        **changes,
    )


def _establish_at_route_bound(stale_gap_grace=1.0):
    states = [gameplay_state(sequence) for sequence in (1, 2, 3)]
    service, environment, clock = make_service(states, stale_gap_grace=stale_gap_grace)
    service._ensure_settled_gameplay(states[0], timeout_seconds=1.0, max_telemetry_age_seconds=0.5)
    assert service._settled_gameplay_sequence == 3
    return service, environment, clock


def test_proof_survives_its_own_sample_aging_through_one_bounded_sampler_gap():
    service, _, _ = _establish_at_route_bound()
    # Live: ~1 in 10 samples arrives 0.5-0.8 s after its predecessor.
    waiting = service.session_health(_aged(3, 0.8))
    assert waiting["gameplay_ready"] is False  # nothing may act on the stale sample
    assert waiting["telemetry_live"] is False
    assert waiting["settled_proof_present"] is True
    assert waiting["settled_proof_stale_gap_hold"] is True
    assert waiting["settled_proof_invalidation_reason"] is None
    resumed = service.session_health(gameplay_state(4))
    assert resumed["gameplay_ready"] is True
    assert resumed["settled_proof_state"] == "continuity_refreshed"
    assert resumed["settled_proof_sequence"] == 4


@pytest.mark.parametrize("stale", [
    lambda: _aged(3, 1.5),                        # gap longer than the grace
    lambda: _aged(4, 0.8),                        # a new sample that is already stale
    lambda: _aged(3, 0.8, actor_id="OtherActor"),  # identity changed
])
def test_stale_gap_hold_is_bounded_to_the_proofs_own_sample(stale):
    service, _, _ = _establish_at_route_bound()
    health = service.session_health(stale())
    assert health["settled_proof_stale_gap_hold"] is False
    assert health["settled_proof_present"] is False
    assert health["gameplay_ready"] is False
    # One returning sample is not enough: the multi-sample gate is required.
    assert service.session_health(gameplay_state(5))["gameplay_ready"] is False


@pytest.mark.parametrize("change, reason", [
    (dict(paused=True), "paused"),
    (dict(in_menu=True), "menu"),
    (dict(running=False), "game_not_running"),
])
def test_stale_gap_hold_never_masks_another_invalidation(change, reason):
    service, _, _ = _establish_at_route_bound()
    health = service.session_health(_aged(3, 0.8, **change))
    assert health["settled_proof_present"] is False
    assert health["settled_proof_invalidation_reason"] == reason


def test_stale_gap_hold_still_checks_process_and_foreground():
    service, environment, _ = _establish_at_route_bound()
    environment.foreground = False
    health = service.session_health(_aged(3, 0.8))
    assert health["settled_proof_present"] is False
    assert health["settled_proof_invalidation_reason"] == "foreground_missing"


def test_stale_gap_grace_is_off_by_default_and_keeps_the_strict_contract(monkeypatch):
    monkeypatch.delenv('FF7R_SETTLED_PROOF_STALE_GAP_GRACE_SECONDS', raising=False)
    service, _, _ = _establish_at_route_bound(stale_gap_grace=None)
    health = service.session_health(_aged(3, 0.8))
    assert health['settled_proof_stale_gap_hold'] is False
    assert health['settled_proof_invalidation_reason'] == 'telemetry_stale'
    assert service.session_health(gameplay_state(4))['gameplay_ready'] is False


def test_stale_gap_grace_opt_in_from_the_runtime_environment(monkeypatch):
    monkeypatch.setenv('FF7R_SETTLED_PROOF_STALE_GAP_GRACE_SECONDS', '1.0')
    service, _, _ = _establish_at_route_bound(stale_gap_grace=None)
    assert service.session_health(_aged(3, 0.8))['settled_proof_stale_gap_hold'] is True
    assert service.session_health(gameplay_state(4))['gameplay_ready'] is True


@pytest.mark.parametrize('value', ['1.5', '-0.1', 'nan', 'soon'])
def test_stale_gap_grace_rejects_values_outside_its_bound(monkeypatch, value):
    monkeypatch.setenv('FF7R_SETTLED_PROOF_STALE_GAP_GRACE_SECONDS', value)
    with pytest.raises(ValueError):
        make_service([gameplay_state(1)])


def test_route_adapter_rides_through_one_sampler_gap_with_the_grace(monkeypatch):
    """Live shape: seq 7 ages to 0.6 s before seq 8; no readiness recovery."""
    from test_live_route_adapter import _production_freshness_state, make_production_freshness_adapter
    monkeypatch.setenv('FF7R_SETTLED_PROOF_STALE_GAP_GRACE_SECONDS', '1.0')
    tail = [_production_freshness_state(sequence, 0.1) for sequence in (4, 5, 6)]
    tail += [_production_freshness_state(7, 0.19238), _production_freshness_state(7, 0.6)]
    tail += [_production_freshness_state(sequence, 0.1) for sequence in (8, 9, 10)]
    adapter, service, _, actuator, _ = make_production_freshness_adapter(tail)
    assert adapter.preflight()['movement_authorized']
    result = adapter.run(dry_run=False, max_cycles=3)
    events = [item.get('event') for item in result['timeline']]
    assert 'readiness_loss_triggered' not in events
    assert result['movement_cycles'] == 3 and len(actuator.commands) == 3
    assert adapter.actuator.is_neutral()


def test_only_a_swimming_stick_pulse_passes_the_settling_gate_while_not_grounded():
    # Live 2026-10-01: the swim-to-shore pulses were refused as
    # gameplay_not_settled because swimming never counts as grounded.
    from dataclasses import replace
    from ff7r_agent.models import LocomotionState

    def swimming(sequence):
        state = gameplay_state(sequence)
        locomotion = LocomotionState(reference="upright_capsule_bottom", feet_position=Vec3(1, 2, -85),
            capsule_radius_cm=34, capsule_half_height_cm=88, velocity_cm_s=Vec3(0, 0, 0),
            grounded=False, movement_mode=None)
        return replace(state, player=replace(state.player, locomotion=locomotion))

    class Advancing(ProofClock):
        def __call__(self):
            self.value += 1.0
            return self.value

    states = [swimming(n) for n in range(1, 400)]
    service, _, _ = make_service(states, clock=Advancing())
    with pytest.raises(SafetyError, match="not grounded"):
        service._assert_action_allowed(observed_state=states[0])
    assert service._assert_action_allowed(allow_swimming=True, observed_state=states[1]).sequence == 2
    paused = replace(states[2], paused=True)
    with pytest.raises(SafetyError):
        service._assert_action_allowed(allow_swimming=True, observed_state=paused)
