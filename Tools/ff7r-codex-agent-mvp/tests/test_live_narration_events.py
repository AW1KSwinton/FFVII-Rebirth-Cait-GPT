from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ff7r_agent import live_narration_events as events


def _at(seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _live_state(state_dir: Path, *, age: int = 0, session_mode: str | None = None) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc) - timedelta(seconds=60)
    (state_dir / "public-stream-live.json").write_text(json.dumps({
        "version": 1, "streamStartedAt": started.isoformat(), "at": _at(age),
    }), encoding="utf-8")
    policy_dir = state_dir / "live-narrator"
    policy_dir.mkdir(parents=True, exist_ok=True)
    policy = {"version": 1, "mode": "worker", "enabled": True, "changedAt": _at()}
    if session_mode is not None:
        policy["sessionMode"] = session_mode
    (policy_dir / "policy.json").write_text(json.dumps(policy), encoding="utf-8")


def _narrated_rehearsal_state(state_dir: Path, *, age: int = 0, lease_recording_id: str | None = None,
                              stream_state: str = "inactive", include_snapshot: bool = True) -> str:
    recording_id = "a" * 64
    _live_state(state_dir, session_mode="rehearsal")
    (state_dir / "public-stream-live.json").unlink(missing_ok=True)
    (state_dir / "live-narrator" / "policy.json").write_text(json.dumps({
        "version": 1, "mode": "worker", "enabled": True, "changedAt": _at(), "sessionMode": "rehearsal",
    }), encoding="utf-8")
    heartbeat = {"version": 2, "at": _at(age), "recordingStartedAt": _at(30), "recordingId": recording_id}
    (state_dir / "public-stream-rehearsal.json").write_text(json.dumps(heartbeat), encoding="utf-8")
    if include_snapshot:
        stamp = _at(age)
        production = state_dir / "production"
        production.mkdir(exist_ok=True)
        (production / "stream-state.json").write_text(json.dumps({
            "observedAt": stamp, "monitorObservedAt": stamp, "obsObservedAt": stamp,
            "monitorFresh": True, "obsFresh": True,
            "stream": {"state": stream_state, "observedAt": stamp, "source": "obs"},
            "recordingActive": True, "recordingId": "b" * 64,
        }), encoding="utf-8")
    lease = {"version": 1, "mode": "rehearsal", "at": _at(age), "instance_id": "narrator-1",
             "recordingId": lease_recording_id or recording_id, "available": True, "busy": False,
             "speech_ready": True, "consecutive_failures": 0, "failure_limit": 2}
    owner = {"version": 1, "mode": "rehearsal", "instance_id": "narrator-1"}
    (state_dir / "speech-consumer-rehearsal.json").write_text(json.dumps(lease), encoding="utf-8")
    (state_dir / "speech-consumer-rehearsal.json.owner.json").write_text(json.dumps(owner), encoding="utf-8")
    return recording_id


def test_live_event_sink_writes_atomic_idempotent_strict_stream_envelope(tmp_path):
    _live_state(tmp_path)
    sink = events.LiveNarrationEventSink(tmp_path)
    facts = ["lane_swing:middle:+8", "projected_score:24-18"]
    assert sink.emit("queens_blood_exchange", facts, source_key="match-a:turn-6")
    assert sink.emit("queens_blood_exchange", facts, source_key="match-a:turn-6")
    assert sink.flush(2)
    status = sink.status()
    sink.close()

    files = list((tmp_path / "live-narrator" / "events").glob("*.json"))
    assert len(files) == 1
    event = json.loads(files[0].read_text(encoding="utf-8"))
    assert set(event) == {"version", "eventId", "at", "streamStartedAt", "kind", "facts"}
    assert event["version"] == 1 and event["kind"] == "queens_blood_exchange"
    assert event["streamStartedAt"] == events.stream_identity(tmp_path)
    assert files[0].name == hashlib.sha256(event["eventId"].encode()).hexdigest() + ".json"
    assert status["written"] == 1 and status["duplicates"] == 1


@pytest.mark.parametrize("setup", ["missing_policy", "stale_stream"])
def test_event_sink_fails_closed_without_fresh_worker_policy_and_stream(tmp_path, setup):
    _live_state(tmp_path, age=70 if setup == "stale_stream" else 0)
    if setup == "missing_policy":
        (tmp_path / "live-narrator" / "policy.json").unlink()
    sink = events.LiveNarrationEventSink(tmp_path)
    assert sink.emit("navigating", [], source_key="route-1") is False
    assert sink.status()["queued"] == 0
    sink.close()
    assert not list((tmp_path / "live-narrator" / "events").glob("*.json"))


def test_narrated_rehearsal_requires_fresh_same_mode_speech_consumer_and_matching_claim(tmp_path):
    _narrated_rehearsal_state(tmp_path)
    lease = json.loads((tmp_path / "speech-consumer-rehearsal.json").read_text(encoding="utf-8"))
    owner = json.loads((tmp_path / "speech-consumer-rehearsal.json.owner.json").read_text(encoding="utf-8"))
    lease_path = tmp_path / "speech-consumer-rehearsal.json"
    owner_path = tmp_path / "speech-consumer-rehearsal.json.owner.json"
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    owner_path.write_text(json.dumps(owner), encoding="utf-8")
    assert events.narrated_rehearsal_enabled(tmp_path)
    lease["speech_ready"] = False
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    assert not events.narrated_rehearsal_enabled(tmp_path)
    lease["speech_ready"] = True
    owner["instance_id"] = "another-instance"
    lease_path.write_text(json.dumps(lease), encoding="utf-8")
    owner_path.write_text(json.dumps(owner), encoding="utf-8")
    assert not events.narrated_rehearsal_enabled(tmp_path)


def test_rehearsal_worker_event_is_bound_to_fresh_recording_and_inactive_obs(monkeypatch, tmp_path):
    recording_id = _narrated_rehearsal_state(tmp_path)
    monkeypatch.setenv("LIVE_NARRATOR_SESSION_MODE", "rehearsal")
    sink = events.LiveNarrationEventSink(tmp_path)
    assert sink.emit("navigating", [], source_key="test-route")
    assert sink.flush(2)
    sink.close()
    [event_path] = list((tmp_path / "live-narrator" / "events").glob("*.json"))
    event = json.loads(event_path.read_text(encoding="utf-8"))
    assert event["sessionMode"] == "rehearsal"
    assert event["recordingId"] == recording_id
    assert event["recordingStartedAt"] == json.loads((tmp_path / "public-stream-rehearsal.json").read_text())["recordingStartedAt"]
    assert event["streamStartedAt"] == int(datetime.fromisoformat(event["recordingStartedAt"].replace("Z", "+00:00")).timestamp() * 1000)


@pytest.mark.parametrize("failure", ["missing_snapshot", "active_obs", "mismatched_lease", "stale_rehearsal", "stale_heartbeat", "actual_live", "mode_mismatch"])
def test_rehearsal_worker_event_fails_closed_without_matching_authoritative_context(monkeypatch, tmp_path, failure):
    lease_id = "c" * 64 if failure == "mismatched_lease" else None
    _narrated_rehearsal_state(tmp_path, age=70 if failure == "stale_rehearsal" else 0,
                              lease_recording_id=lease_id,
                              stream_state="active" if failure == "active_obs" else "inactive",
                              include_snapshot=failure != "missing_snapshot")
    if failure == "stale_heartbeat":
        heartbeat_path = tmp_path / "public-stream-rehearsal.json"
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        heartbeat["at"] = _at(20)
        heartbeat_path.write_text(json.dumps(heartbeat), encoding="utf-8")
    monkeypatch.setenv("LIVE_NARRATOR_SESSION_MODE", "live" if failure == "mode_mismatch" else "rehearsal")
    if failure == "actual_live":
        (tmp_path / "public-stream-live.json").write_text(json.dumps({"version": 1, "streamStartedAt": _at(30), "at": _at()}), encoding="utf-8")
    sink = events.LiveNarrationEventSink(tmp_path)
    assert sink.emit("navigating", [], source_key="unsafe-context") is False
    sink.close()
    assert not list((tmp_path / "live-narrator" / "events").glob("*.json"))


def test_offline_recording_context_requires_matching_fresh_owner(tmp_path):
    _write_offline_context(tmp_path)
    assert events.offline_recording_context(tmp_path)
    _write_offline_context(tmp_path, age=70)
    assert not events.offline_recording_context(tmp_path)
    _write_offline_context(tmp_path, owner_match=False)
    assert not events.offline_recording_context(tmp_path)
    _write_offline_context(tmp_path, heartbeat_match=False)
    assert not events.offline_recording_context(tmp_path)


@pytest.mark.parametrize("kind,facts", [
    ("queens_blood_turn", []),
    ("queens_blood_exchange", ["C:/private/path"]),
    ("queens_blood_exchange", ["a" * 181]),
])
def test_event_sink_rejects_ordinary_qb_turn_and_unsafe_or_oversize_facts(tmp_path, kind, facts):
    _live_state(tmp_path)
    sink = events.LiveNarrationEventSink(tmp_path)
    assert sink.emit(kind, facts, source_key="not-public") is False
    assert sink.status()["queued"] == 0
    sink.close()


def test_qb_exchange_fact_projection_contains_only_small_observed_values():
    facts = events.queens_blood_exchange_facts([
        {"kind": "lane_swing", "lane": "middle", "swing": 8,
         "card_id": "private-looking-id"},
        {"kind": "match_lead_changed", "from": "opponent", "to": "player",
         "player_score": 24, "opponent_score": 18},
        {"kind": "ignored", "detail": "do not export"},
    ], {"player_score": 24, "opponent_score": 18}, -3)
    assert facts == ["lane_swing:middle:+8", "match_lead_changed:opponent-to-player",
                     "projected_score:24-18", "projection_change:-3"]


def _service_with_campaign(tmp_path):
    from ff7r_agent.service import AgentService

    campaign_file = tmp_path / "campaign.json"
    campaign_file.write_text("{}", encoding="utf-8")
    service = object.__new__(AgentService)
    service._queen_blood_campaign = SimpleNamespace(path=campaign_file)
    service._public_stream_event_dir = tmp_path / "events"
    service._campaign_speech = None
    service.get_queens_blood_run_status = lambda: {"run_loaded": True}
    @contextmanager
    def gate(_directory):
        yield True
    service._agent_speech = SimpleNamespace(_shared_gate=gate)
    return service


def _write_offline_context(state_dir: Path, *, age: int = 0, owner_match: bool = True,
                           heartbeat_match: bool = True) -> None:
    recording_id = "a" * 64
    context = {"version": 1, "mode": "rehearsal", "at": _at(age), "instance_id": "offline-instance",
               "available": True, "busy": False, "speech_ready": False,
               "consecutive_failures": 0, "failure_limit": 2, "recordingId": recording_id}
    heartbeat = {"version": 2, "at": _at(age), "recordingStartedAt": _at(30),
                 "recordingId": recording_id}
    if not heartbeat_match:
        heartbeat["recordingId"] = "b" * 64
    owner = {"version": 1, "mode": "rehearsal",
             "instance_id": context["instance_id"] if owner_match else "other"}
    (state_dir / "recording-context.json").write_text(json.dumps(context), encoding="utf-8")
    (state_dir / "recording-context.json.owner.json").write_text(json.dumps(owner), encoding="utf-8")
    (state_dir / "public-stream-rehearsal.json").write_text(json.dumps(heartbeat), encoding="utf-8")


@pytest.mark.parametrize("context_state", ["offline", "silent_rehearsal"])
def test_campaign_input_has_no_speech_receipt_requirement_offline(monkeypatch, tmp_path, context_state):
    import ff7r_agent.service as service_module

    service = _service_with_campaign(tmp_path)
    monkeypatch.setattr(service_module, "stream_is_live", lambda _state: False)
    monkeypatch.setattr(service_module, "rehearsal_is_live", lambda _state: context_state == "silent_rehearsal")
    if context_state == "offline":
        _write_offline_context(tmp_path)
    service._claim_campaign_speech_before_input()
    assert service._campaign_speech is None


@pytest.mark.parametrize("lease_state", ["fresh", "failed"])
def test_explicit_narrated_rehearsal_keeps_receipt_safety_gate(monkeypatch, tmp_path, lease_state):
    import ff7r_agent.service as service_module
    import ff7r_agent.queen_blood_speech_receipt as receipt_module
    from ff7r_agent.service import SafetyError

    service = _service_with_campaign(tmp_path)
    monkeypatch.setattr(service_module, "stream_is_live", lambda _state: False)
    monkeypatch.setattr(service_module, "rehearsal_is_live", lambda _state: True)
    lease_at = _at(70 if lease_state == "stale" else 0)
    lease = {"version": 1, "mode": "rehearsal", "at": lease_at, "available": True,
             "busy": False, "speech_ready": lease_state != "failed", "consecutive_failures": 0,
             "failure_limit": 2, "instance_id": "narrator-1"}
    (tmp_path / "speech-consumer-rehearsal.json").write_text(json.dumps(lease), encoding="utf-8")
    monkeypatch.setattr(receipt_module, "consume_locked", lambda *_args: None)
    with pytest.raises(SafetyError, match="fresh successful"):
        service._claim_campaign_speech_before_input()


def test_stale_unowned_rehearsal_lease_does_not_force_narration(monkeypatch, tmp_path):
    import ff7r_agent.service as service_module

    service = _service_with_campaign(tmp_path)
    monkeypatch.setattr(service_module, "stream_is_live", lambda _state: False)
    monkeypatch.setattr(service_module, "rehearsal_is_live", lambda _state: True)
    lease = {"version": 1, "mode": "rehearsal", "at": _at(70), "available": False,
             "busy": False, "speech_ready": False, "consecutive_failures": 2, "failure_limit": 2}
    (tmp_path / "speech-consumer-rehearsal.json").write_text(json.dumps(lease), encoding="utf-8")
    service._claim_campaign_speech_before_input()
    assert service._campaign_speech is None


def test_actual_live_worker_mode_skips_primary_receipt_even_without_narrator(monkeypatch, tmp_path):
    import ff7r_agent.service as service_module

    service = _service_with_campaign(tmp_path)
    _live_state(tmp_path)
    monkeypatch.setattr(service_module, "stream_is_live", lambda _state: True)
    service._claim_campaign_speech_before_input()
    assert service._campaign_speech is None


def test_explicit_rehearsal_worker_mode_skips_primary_receipt_even_without_synchronous_line(monkeypatch, tmp_path):
    import ff7r_agent.service as service_module

    _narrated_rehearsal_state(tmp_path)
    monkeypatch.setenv("LIVE_NARRATOR_SESSION_MODE", "rehearsal")
    service = _service_with_campaign(tmp_path)
    monkeypatch.setattr(service_module, "stream_is_live", lambda _state: False)
    monkeypatch.setattr(service_module, "rehearsal_is_live", lambda _state: True)
    service._claim_campaign_speech_before_input()
    assert service._campaign_speech is None


def test_changed_at_worker_policy_shape_enables_event_emission_and_primary_bypass(monkeypatch, tmp_path):
    import ff7r_agent.service as service_module

    _live_state(tmp_path)
    policy_path = tmp_path / "live-narrator" / "policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    assert set(policy) == {"version", "mode", "enabled", "changedAt"}
    assert events.worker_policy_enabled(tmp_path)
    sink = events.LiveNarrationEventSink(tmp_path)
    assert sink.emit("navigating", [], source_key="schema-regression")
    assert sink.flush(2)
    sink.close()
    assert len(list((tmp_path / "live-narrator" / "events").glob("*.json"))) == 1

    service = _service_with_campaign(tmp_path)
    monkeypatch.setattr(service_module, "stream_is_live", lambda _state: True)
    service._claim_campaign_speech_before_input()
    assert service._campaign_speech is None


def test_actual_live_manual_mode_still_requires_a_fresh_receipt(monkeypatch, tmp_path):
    import ff7r_agent.service as service_module
    import ff7r_agent.queen_blood_speech_receipt as receipt_module
    from ff7r_agent.service import SafetyError

    service = _service_with_campaign(tmp_path)
    monkeypatch.setattr(service_module, "stream_is_live", lambda _state: True)
    monkeypatch.setattr(receipt_module, "consume_locked", lambda *_args: None)
    with pytest.raises(SafetyError, match="fresh successful"):
        service._claim_campaign_speech_before_input()


def test_service_qb_hook_emits_only_notable_public_facts_to_worker_sink():
    from ff7r_agent.service import AgentService

    class Sink:
        def __init__(self): self.events = []
        def emit(self, kind, facts, *, source_key): self.events.append((kind, facts, source_key)); return True

    service = object.__new__(AgentService)
    service.input = SimpleNamespace(is_real=True)
    service._production_event_sink = None
    service._queen_blood_match = {"story_id": "match-test"}
    service._live_narration_event_sink = Sink()
    service._emit_production_qb_event(
        "queens_blood_notable_exchange", SimpleNamespace(turn=7),
        cues=[{"kind": "lane_swing", "lane": "top", "swing": 6}],
        projection={"player_score": 15, "opponent_score": 9}, projection_change=3,
    )
    assert len(service._live_narration_event_sink.events) == 1
    kind, facts, source_key = service._live_narration_event_sink.events[0]
    assert kind == "queens_blood_exchange"
    assert facts == ["lane_swing:top:+6", "projected_score:15-9", "projection_change:+3"]
    assert source_key.startswith("match-test:7:")
