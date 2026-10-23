from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ff7r_agent.models import Character, GameState, Interactable, RestState
from ff7r_agent.telemetry import LifecycleError, RestLifecycleTracker


def state(*, sequence=10, controller_state=1, actor_id="CloudActor", running=True,
          timestamp=None, bench=True):
    return GameState(
        schema_version=1,
        sequence=sequence,
        timestamp_utc=timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        running=running,
        paused=False,
        in_menu=False,
        in_combat=False,
        location="",
        objective="",
        player=Character("Cloud", 10, 100, 5, 10, controller_state=controller_state,
                         actor_id=actor_id),
        nearby_interactable=(Interactable("BenchA", "bench", 1.5, True) if bench else None),
        source="test",
    )


def test_valid_lifecycle_transitions_and_terminal_reset():
    now = [100.0]
    tracker = RestLifecycleTracker(monotonic=lambda: now[0], terminal_seconds=5)
    started = tracker.begin(state())
    assert started.state == "interaction_started"
    assert started.bench_id == "BenchA"
    active = tracker.update(state(sequence=11, controller_state=8))
    assert active.rest.state == "resting"
    completed = tracker.update(state(sequence=24, controller_state=1))
    assert completed.rest.state == "interaction_completed"
    assert completed.rest.started_sequence == 10
    assert completed.rest.completed_sequence == 24
    now[0] += 6
    reset = tracker.update(state(sequence=30))
    assert reset.rest.state == "bench_detected"


def test_invalid_second_start_is_rejected():
    tracker = RestLifecycleTracker()
    tracker.begin(state())
    with pytest.raises(LifecycleError, match="already active"):
        tracker.begin(state(sequence=11))


def test_completion_without_start_is_rejected():
    tracker = RestLifecycleTracker()
    with pytest.raises(LifecycleError, match="invalid terminal"):
        tracker._terminal("interaction_completed", 1)


def test_mismatched_interaction_id_fails_closed():
    tracker = RestLifecycleTracker()
    tracker.begin(state())
    observed = tracker.update(state(sequence=11), interaction_id="wrong")
    assert observed.rest.state == "failed"
    assert observed.rest.failure_reason == "mismatched_interaction_id"


def test_mismatched_state_identity_fails_closed():
    tracker = RestLifecycleTracker()
    tracker.begin(state())
    observed = tracker.update(state(sequence=11, actor_id="AerithActor"))
    assert observed.rest.state == "failed"
    assert observed.rest.failure_reason == "mismatched_state_identity"


def test_stale_lifecycle_fails_then_resets():
    now = [100.0]
    tracker = RestLifecycleTracker(monotonic=lambda: now[0], terminal_seconds=1)
    tracker.begin(state())
    old = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    failed = tracker.update(state(sequence=11, timestamp=old))
    assert failed.rest.failure_reason == "stale_or_stopped_telemetry"
    now[0] += 2
    reset = tracker.update(state(sequence=12))
    assert reset.rest.state == "bench_detected"


def test_start_timeout_fails():
    now = [100.0]
    tracker = RestLifecycleTracker(monotonic=lambda: now[0], start_timeout=3)
    tracker.begin(state())
    now[0] += 4
    failed = tracker.update(state(sequence=12, controller_state=1))
    assert failed.rest.failure_reason == "field_action_not_started"


def test_non_idle_exit_is_cancelled():
    tracker = RestLifecycleTracker()
    tracker.begin(state())
    tracker.update(state(sequence=11, controller_state=8))
    cancelled = tracker.update(state(sequence=12, controller_state=3))
    assert cancelled.rest.state == "cancelled"
    assert cancelled.rest.failure_reason == "field_action_left_to_state_3"


def test_malformed_lifecycle_telemetry_is_rejected():
    with pytest.raises(Exception):
        RestState.from_dict({"state": "resting", "started_sequence": "bad"})
    with pytest.raises(Exception):
        RestState.from_dict({"state": "invented"})


def test_older_snapshot_without_rest_or_controller_metadata_is_readable():
    payload = {
        "schema_version": 1,
        "sequence": 1,
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "game": {"running": True},
        "player": {"name": "Cloud", "hp": 1, "max_hp": 10, "mp": 1, "max_mp": 10},
    }
    parsed = GameState.from_dict(payload)
    assert parsed.rest.state == "idle"
    assert parsed.player.controller_state is None
    assert parsed.player.actor_id is None
