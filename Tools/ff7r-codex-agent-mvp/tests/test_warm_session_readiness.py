from dataclasses import replace

from test_settled_gameplay_proof import (
    ProofEnvironment,
    ProofInput,
    SequenceTelemetry,
    gameplay_state,
    make_service,
)


class MutableInput(ProofInput):
    def __init__(self):
        self.neutral = True

    def backend_status(self):
        return {
            "stick_state": "neutral" if self.neutral else "active",
            "last_stick": {"x": 0.0, "y": 0.0} if self.neutral else {"x": 1.0, "y": 0.0},
        }


class MutableSession:
    def __init__(self, marker):
        self.marker = marker

    def status(self):
        return {
            "active": True,
            "owner_matches": True,
            "session_marker": self.marker,
            "owner_id": "owner",
            "controller_pid": 10,
            "game_pid": 20,
            "opened_at": 100.0,
        }


def test_valid_warm_proof_avoids_redundant_settling_and_never_authorizes_movement():
    states = [gameplay_state(sequence) for sequence in (1, 2, 3, 4)]
    service, _, _ = make_service(states)
    service._ensure_settled_gameplay(states[0], timeout_seconds=1.0)

    snapshot = service.benchmark_readiness_snapshot()

    assert snapshot["sequence"] == 4
    assert snapshot["readiness"] == "warm_session"
    assert snapshot["settled_proof_valid"] is True
    assert snapshot["settling_required"] is False
    assert snapshot["session_health"]["gameplay_ready"] is True
    assert snapshot["input_status"]["backend_status"]["stick_state"] == "neutral"
    assert snapshot["movement_authorized"] is False
    assert snapshot["navigation_admitted"] is False
    assert snapshot["navigation_admission_required"] is True
    assert snapshot["fresh_advancing_state_required"] is True
    assert service.telemetry.index == 4


def test_non_neutral_input_invalidates_warm_proof_and_restores_three_sample_gate():
    states = [gameplay_state(sequence) for sequence in (1, 2, 3)]
    input_backend = MutableInput()
    environment = ProofEnvironment()
    service, _, _ = make_service(states, environment=environment)
    service.input = input_backend
    service._ensure_settled_gameplay(states[0], timeout_seconds=1.0)
    input_backend.neutral = False
    service.telemetry = SequenceTelemetry([
        gameplay_state(4),
        gameplay_state(5),
        gameplay_state(6),
        gameplay_state(7),
    ])

    invalid = service.benchmark_readiness_snapshot()

    assert invalid["settled_proof_valid"] is False
    assert invalid["settling_required"] is True
    assert invalid["settled_proof_invalidation_reason"] == "input_not_neutral"
    assert invalid["session_health"]["settled_proof_present"] is False
    assert invalid["session_health"]["settled_proof_invalidation_reason"] == "input_not_neutral"
    assert invalid["movement_authorized"] is False
    assert invalid["navigation_admitted"] is False

    input_backend.neutral = True
    settled = service.wait_for_settled_gameplay(1.0)

    assert settled["stable_samples"] == 3
    assert service.session_health(gameplay_state(7))["gameplay_ready"] is True


def test_world_and_session_identity_changes_invalidate_warm_proof():
    states = [
        replace(
            gameplay_state(sequence),
            navigation={"world_id": "world-a", "session_id": "session-a"},
        )
        for sequence in (1, 2, 3)
    ]
    states.append(
        replace(
            gameplay_state(4),
            navigation={"world_id": "world-b", "session_id": "session-b"},
        )
    )
    service, _, _ = make_service(states)
    service._ensure_settled_gameplay(states[0], timeout_seconds=1.0)

    snapshot = service.benchmark_readiness_snapshot()

    assert snapshot["settled_proof_valid"] is False
    assert snapshot["settling_required"] is True
    assert snapshot["session_health"]["settled_proof_invalidation_reason"] == "gameplay_identity_changed"
    assert snapshot["movement_authorized"] is False
    assert snapshot["navigation_admitted"] is False


def test_control_session_identity_change_invalidates_warm_proof():
    states = [gameplay_state(sequence) for sequence in (1, 2, 3, 4, 5)]
    input_backend = MutableInput()
    service, _, _ = make_service(states)
    service.input = input_backend
    session = MutableSession("session-a")
    service._session_lease = session
    service._ensure_settled_gameplay(states[0], timeout_seconds=1.0)

    warm = service.benchmark_readiness_snapshot()
    session.marker = "session-b"
    changed = service.benchmark_readiness_snapshot()

    assert warm["settled_proof_valid"] is True
    assert changed["settled_proof_valid"] is False
    assert changed["settling_required"] is True
    assert changed["session_health"]["settled_proof_invalidation_reason"] == "control_session_changed"
    assert changed["movement_authorized"] is False
    assert changed["navigation_admitted"] is False


def test_combat_loading_process_and_foreground_failures_are_conservative():
    cases = [
        gameplay_state(4, paused=True),
        gameplay_state(4, in_menu=True),
        replace(gameplay_state(4), in_combat=True),
        replace(gameplay_state(4), navigation={"loading": True}),
        gameplay_state(4),
        gameplay_state(4, running=False),
    ]
    for index, state in enumerate(cases):
        service, environment, _ = make_service([
            gameplay_state(1),
            gameplay_state(2),
            gameplay_state(3),
            state,
        ])
        service._ensure_settled_gameplay(gameplay_state(1), timeout_seconds=1.0)
        if index == 4:
            environment.foreground = False
        elif not state.running:
            environment.process = False
        snapshot = service.benchmark_readiness_snapshot()

        assert snapshot["settled_proof_valid"] is False
        assert snapshot["settling_required"] is True
        assert snapshot["settled_proof_invalidation_reason"] is not None
        assert snapshot["movement_authorized"] is False
        assert snapshot["navigation_admitted"] is False
        assert snapshot["navigation_admission_required"] is True
        assert snapshot["fresh_advancing_state_required"] is True
