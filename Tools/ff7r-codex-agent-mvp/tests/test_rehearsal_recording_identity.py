"""Rehearsal identity is a file token, never an elapsed-media-time estimate."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from ff7r_agent.public_stream_events import match_record_target, rehearsal_driver, rehearsal_is_live, stream_driver

RUN = "0b5c1b2e-6f4f-4c3a-9f0e-2d3b4a5c6d7e"
IDENTITY = "a" * 64
NOW = datetime(2026, 10, 8, 11, 30, tzinfo=timezone.utc)
START = NOW - timedelta(hours=1)


def declaration(**changes):
    return {"version": 1, "driver": "ChatGPT", "modelId": "gpt-6-sol", "effort": "high", "runId": RUN,
            "streamStartedAt": START.isoformat(), "declaredAt": (START + timedelta(seconds=20)).isoformat(),
            "recordingId": IDENTITY, **changes}


def heartbeat(**changes):
    return {"version": 2, "at": NOW.isoformat(), "recordingStartedAt": START.isoformat(),
            "recordingId": IDENTITY, **changes}


def write(directory, declared, live):
    (directory / "public-driver-rehearsal.json").write_text(json.dumps(declared), encoding="utf-8")
    (directory / "public-stream-rehearsal.json").write_text(json.dumps(live), encoding="utf-8")


def test_stable_identity_credits_rehearsal_only_and_keeps_the_same_run(tmp_path):
    write(tmp_path, declaration(), heartbeat())
    driver = {"driver": "ChatGPT", "model_id": "gpt-6-sol", "effort": "high", "run_id": RUN}
    for elapsed in (0, 30, 60, 3600):
        now = NOW + timedelta(seconds=elapsed)
        # Restarting/refreshing the streamer changes only heartbeat freshness.
        write(tmp_path, declaration(), heartbeat(at=now.isoformat()))
        assert rehearsal_is_live(tmp_path, now.timestamp())
        assert rehearsal_driver(tmp_path, now.timestamp()) == driver
        assert stream_driver(tmp_path, now.timestamp()) is None
        assert match_record_target(tmp_path, now.timestamp()) == (
            driver, "public-qb-results-rehearsal", "qb-match-history-rehearsal")


def test_new_recording_rejects_old_declaration_even_with_identical_start_timestamp(tmp_path):
    write(tmp_path, declaration(), heartbeat(recordingId="b" * 64))
    assert rehearsal_is_live(tmp_path, NOW.timestamp())
    assert rehearsal_driver(tmp_path, NOW.timestamp()) is None
    assert match_record_target(tmp_path, NOW.timestamp()) == (
        None, "public-qb-results-rehearsal", "qb-match-history-rehearsal")


@pytest.mark.parametrize("changes", [
    {"at": (NOW - timedelta(seconds=61)).isoformat()},
    {"at": (NOW + timedelta(seconds=11)).isoformat()},
    {"recordingId": "invalid"}, {"recordingId": IDENTITY + "\n"},
    {"recordingId": None}, {"recordingStartedAt": "invalid"}, {"extra": "unreviewed"}, {"version": 3},
])
def test_stale_future_or_malformed_heartbeats_fail_closed(tmp_path, changes):
    write(tmp_path, declaration(), heartbeat(**changes))
    assert not rehearsal_is_live(tmp_path, NOW.timestamp())
    assert rehearsal_driver(tmp_path, NOW.timestamp()) is None


@pytest.mark.parametrize("changes", [
    {"recordingId": "b" * 64}, {"recordingId": None}, {"extra": "unreviewed"},
    {"declaredAt": (START - timedelta(seconds=11)).isoformat()},
    {"declaredAt": (NOW + timedelta(seconds=11)).isoformat()},
])
def test_invalid_declarations_fail_closed(tmp_path, changes):
    write(tmp_path, declaration(**changes), heartbeat())
    assert rehearsal_driver(tmp_path, NOW.timestamp()) is None


def test_legacy_and_stable_identity_cannot_be_mixed(tmp_path):
    legacy_declaration = declaration()
    del legacy_declaration["recordingId"]
    write(tmp_path, legacy_declaration, heartbeat())
    assert rehearsal_driver(tmp_path, NOW.timestamp()) is None
    legacy_heartbeat = heartbeat(version=1)
    del legacy_heartbeat["recordingId"]
    write(tmp_path, declaration(), legacy_heartbeat)
    assert rehearsal_driver(tmp_path, NOW.timestamp()) is None
    write(tmp_path, legacy_declaration, legacy_heartbeat)
    assert rehearsal_driver(tmp_path, NOW.timestamp()) is not None


def test_recording_identity_cannot_be_used_as_a_live_stream_declaration(tmp_path):
    (tmp_path / "public-driver.json").write_text(json.dumps(declaration()), encoding="utf-8")
    (tmp_path / "public-stream-live.json").write_text(json.dumps({
        "version": 1, "at": NOW.isoformat(), "streamStartedAt": START.isoformat()}), encoding="utf-8")
    assert stream_driver(tmp_path, NOW.timestamp()) is None


def test_loaded_campaign_survives_heartbeat_refresh_and_worker_restart(tmp_path):
    from ff7r_agent.gameplay_narration import AgentSpeechPublisher
    from ff7r_agent.queen_blood_speech_receipt import binding
    from test_campaign_worker_identity import fixture

    worker, _, state = fixture(tmp_path)
    declared_path = state / "public-driver-rehearsal.json"
    declared = json.loads(declared_path.read_text(encoding="utf-8"))
    declared["recordingId"] = IDENTITY
    for _ in range(3):
        write(state, declared, heartbeat(
            at=datetime.now(timezone.utc).isoformat(), recordingStartedAt=declared["streamStartedAt"]))
        service = worker()
        assert service.get_queens_blood_run_status()["run_loaded"] is True
        assert service.get_campaign_status()["profile_id"].endswith("slot015")
    publisher = AgentSpeechPublisher(state / "events", receipt_context=lambda: binding(
        service.get_queens_blood_run_status(), "rehearsal"))
    spoken = publisher.say("The middle is open, so I can build there.", "calm")
    assert spoken["ok"] and spoken["rehearsal"] and spoken["receipt_id"]
    write(state, declared, heartbeat(
        at=datetime.now(timezone.utc).isoformat(), recordingStartedAt=declared["streamStartedAt"], recordingId="b" * 64))
    assert worker().get_queens_blood_run_status()["driver"] is None
