from dataclasses import replace
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ff7r_agent.models import (
    Character, GameState, Interactable, RelativePosition, RestState, Rotation, Vec3,
)
from ff7r_agent.service import AgentService, SafetyError
from test_agent import FakeArm, FakeEnvironment, FakeRealInput


BENCH_ID = "PersistentLevel.ValidatedBench"
# The current approacher requires advancing telemetry for roughly 0.6 seconds
# after movement before it calls a pulse settled. Four samples modelled the
# retired fixed-pulse contract and left the current settling state machine
# with no fresh samples to qualify.
SETTLED_SAMPLE_COUNT = 24


def state(sequence=1, *, distance=4.0, heading=0.0, player_x=0.0,
          bench_id=BENCH_ID, target=None, paused=False, in_menu=False,
          timestamp=None, spatial=True, rest="bench_detected", usable=False):
    player = Character(
        "Cloud", 10, 20, 3, 9, position=Vec3(player_x, 0.0, 0.0),
        rotation=Rotation(0.0, 0.0, 0.0), controller_state=1, actor_id="CloudActor",
    )
    bench = Interactable(
        bench_id, "bench", distance, usable,
        position=Vec3(400.0, 0.0, 0.0) if spatial else None,
        relative=(RelativePosition(distance, 0.0, 0.0, distance, 0.0, heading)
                  if spatial else None),
    )
    return GameState(
        1, sequence,
        timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        True, paused, in_menu, False, "", "", player,
        target=target, nearby_interactable=bench, rest=RestState(rest), source="test",
    )


class SequenceTelemetry:
    def __init__(self, states, on_observe=None):
        self.states = list(states)
        self.index = 0
        self.on_observe = on_observe

    def observe(self):
        current = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        if self.on_observe:
            self.on_observe(self.index)
        return current


class SequenceEnvironment(FakeEnvironment):
    def __init__(self, foregrounds):
        super().__init__()
        self.foregrounds = list(foregrounds)

    def game_is_foreground(self):
        if len(self.foregrounds) > 1:
            return self.foregrounds.pop(0)
        return self.foregrounds[0]


def service(states, *, arm=None, bench_arm=None, environment=None, sleeper=None,
            max_telemetry_age=2.0):
    backend = FakeRealInput()
    clock = [0.0]
    if sleeper is None:
        sleeper = lambda seconds: clock.__setitem__(0, clock[0] + seconds)

    def monotonic():
        clock[0] += 0.03
        return clock[0]

    instance = AgentService(
        SequenceTelemetry(states), backend,
        max_telemetry_age=max_telemetry_age,
        arm_state=arm or FakeArm(), bench_arm_state=bench_arm or FakeArm(armed=False),
        environment=environment or FakeEnvironment(), sleeper=sleeper,
        monotonic=monotonic,
        minimum_action_delay=0.0,
    )
    return instance, backend


def settled_states(start_sequence, *, distance, player_x, usable=False):
    return [
        state(start_sequence + offset, distance=distance,
              player_x=player_x, usable=usable)
        for offset in range(SETTLED_SAMPLE_COUNT)
    ]


def test_one_forward_pulse_returns_complete_trace_and_latches_stop():
    arm = FakeArm()
    svc, backend = service([state()] + settled_states(
        2, distance=3.4, player_x=60.0
    ), arm=arm)
    result = svc.approach_detected_bench(1, 2.5)
    assert result["outcome"] == "pulse_limit_reached"
    pulse = result["pulses"][0]
    assert pulse["pre_sequence"] == 1
    assert pulse["post_sequence"] > 2
    assert pulse["horizontal_displacement"] == 0.6
    assert pulse["settled"] is True
    assert backend.actions == [("adaptive_forward", 1000)]
    assert backend.release_count >= 2
    assert arm.stopped and not arm.armed


def test_multi_pulse_stops_within_range():
    svc, backend = service(
        [state()] + settled_states(2, distance=3.2, player_x=80.0) +
        settled_states(2 + SETTLED_SAMPLE_COUNT,
                       distance=2.4, player_x=160.0, usable=True)
    )
    result = svc.approach_detected_bench(3, 2.5)
    assert result["ok"] is True
    assert result["outcome"] == "bench_usable"
    assert result["final_distance"] == 2.4
    assert len(result["pulses"]) == 2
    assert backend.actions == [("adaptive_forward", 1000), ("adaptive_forward", 1000)]


@pytest.mark.parametrize("maximum,stop", [(0, 2.5), (9, 2.5), (1, 1.9), (1, 3.1)])
def test_option_budgets_are_rejected(maximum, stop):
    svc, _ = service([state()])
    with pytest.raises(SafetyError):
        svc.approach_detected_bench(maximum, stop)


@pytest.mark.parametrize(
    ("before", "after", "outcome"),
    [
        (state(), state(2, bench_id="OtherBench", player_x=50), "bench_lost"),
        (state(), state(2, distance=4.2, player_x=50), "distance_increased"),
        (state(), state(2, distance=3.9, player_x=2), "blocked_but_not_usable"),
        (state(), state(2, distance=3.9, player_x=50, target=object()), "safety_gate_rejected"),
        (state(), state(2, distance=3.9, player_x=50, paused=True), "safety_gate_rejected"),
        (state(), state(2, distance=3.9, player_x=50, in_menu=True), "safety_gate_rejected"),
        (state(), state(2, distance=3.9, player_x=50, heading=30.1), "heading_out_of_range"),
    ],
)
def test_between_pulse_stop_conditions(before, after, outcome):
    # These parametrized states are created during collection, so use a
    # bounded wider freshness window to test the between-pulse outcome rather
    # than making it depend on full-suite runtime.
    now = datetime.now(timezone.utc)
    before = replace(before, timestamp_utc=now.isoformat().replace("+00:00", "Z"))
    after = replace(
        after,
        timestamp_utc=(now + timedelta(milliseconds=1)).isoformat().replace("+00:00", "Z"),
    )
    svc, backend = service([before, after], max_telemetry_age=60.0)
    result = svc.approach_detected_bench(2, 2.5)
    assert result["outcome"] == outcome
    assert len(backend.actions) == 1


def test_initial_heading_and_missing_spatial_reject_without_input():
    for initial in (state(heading=30.1), state(spatial=False)):
        svc, backend = service([initial])
        result = svc.approach_detected_bench()
        assert result["outcome"] in {"heading_out_of_range", "safety_gate_rejected"}
        assert backend.actions == []


@pytest.mark.parametrize("heading", [-30.0, 30.0])
def test_widened_heading_boundary_allows_supervised_forward_pulse(heading):
    svc, backend = service(
        [state(heading=heading)] + settled_states(
            2, distance=3.5, player_x=50.0
        )
    )
    result = svc.approach_detected_bench(1, 2.5)
    assert result["outcome"] == "pulse_limit_reached"
    assert len(backend.actions) == 1


def test_bench_position_change_stops_after_one_pulse():
    moved_bench = replace(
        state(2, distance=3.5, player_x=50).nearby_interactable,
        position=Vec3(402.0, 0.0, 0.0),
    )
    after = replace(state(2, distance=3.5, player_x=50),
                    nearby_interactable=moved_bench)
    svc, backend = service([state(), after])
    result = svc.approach_detected_bench(2, 2.5)
    assert result["outcome"] == "bench_lost"
    assert len(backend.actions) == 1


def test_stale_sequence_stops_after_one_pulse():
    same = state()
    svc, backend = service([same])
    result = svc.approach_detected_bench()
    assert result["outcome"] == "safety_gate_rejected"
    assert result["rejection_reason"] == "telemetry sequence did not advance"
    assert len(backend.actions) == 1


def test_post_pulse_wait_ignores_early_unchanged_sequence():
    early = state(2, distance=4.0, player_x=0.0)
    svc, backend = service([state(), early] + settled_states(
        3, distance=3.6, player_x=40.0
    ))
    result = svc.approach_detected_bench(1, 2.5)
    assert result["outcome"] == "pulse_limit_reached"
    assert result["pulses"][0]["post_sequence"] > 3
    assert result["pulses"][0]["horizontal_displacement"] == 0.4
    assert len(backend.actions) == 1


def test_continuing_movement_never_authorises_another_pulse():
    moving = [state(index + 2, distance=4.0 - index * 0.02,
                    player_x=(index + 1) * 3.0) for index in range(40)]
    svc, backend = service([state()] + moving)
    result = svc.approach_detected_bench(3, 2.5)
    assert result["outcome"] == "safety_gate_rejected"
    assert result["rejection_reason"] == "movement did not settle before telemetry deadline"
    assert len(backend.actions) == 1
    assert result["pulses"][0]["settled"] is False


def test_stale_telemetry_rejects_before_input():
    old = (datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat().replace("+00:00", "Z")
    svc, backend = service([state(timestamp=old)])
    result = svc.approach_detected_bench()
    assert result["outcome"] == "safety_gate_rejected"
    assert backend.actions == []


def test_foreground_loss_between_pulses_stops():
    svc, backend = service(
        [state(), state(2, distance=3.5, player_x=50)],
        environment=SequenceEnvironment([True, False]),
    )
    result = svc.approach_detected_bench(2, 2.5)
    assert result["outcome"] == "safety_gate_rejected"
    assert len(backend.actions) == 1


def test_emergency_stop_between_pulses_stops():
    arm = FakeArm()
    telemetry = SequenceTelemetry(
        [state(), state(2, distance=3.5, player_x=50)],
        on_observe=lambda count: arm.latch_stop() if count == 2 else None,
    )
    backend = FakeRealInput()
    svc = AgentService(telemetry, backend, arm_state=arm,
                       bench_arm_state=FakeArm(armed=False),
                       environment=FakeEnvironment(), sleeper=lambda _: None,
                       minimum_action_delay=0.0)
    result = svc.approach_detected_bench(2, 2.5)
    assert result["outcome"] == "emergency_stopped"
    assert len(backend.actions) == 1


def test_bench_arm_does_not_authorise_approach():
    svc, backend = service([state()], arm=FakeArm(armed=False), bench_arm=FakeArm())
    result = svc.approach_detected_bench()
    assert result["outcome"] == "safety_gate_rejected"
    assert "not manually armed" in result["rejection_reason"]
    assert backend.actions == []


def test_cumulative_distance_budget_stops():
    states = [state(distance=4.9)]
    sequence = 2
    for index in range(1, 8):
        states.extend(settled_states(
            sequence, distance=4.9 - index * 0.1, player_x=index * 100.0
        ))
        sequence += SETTLED_SAMPLE_COUNT
    svc, backend = service(states)
    result = svc.approach_detected_bench(8, 2.0)
    assert result["outcome"] == "pulse_limit_reached"
    assert result["rejection_reason"] == "cumulative distance budget exhausted"
    assert len(backend.actions) == 7
