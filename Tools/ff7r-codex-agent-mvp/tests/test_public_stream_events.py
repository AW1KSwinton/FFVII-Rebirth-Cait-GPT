import json
from datetime import datetime, timezone
import pytest

from ff7r_agent.public_stream_events import (
    public_event_for_action, write_public_event, write_public_match_result,
    write_public_match_state,
)


def test_public_event_export_never_serializes_action_data(tmp_path):
    action = {
        "ok": True,
        "action": "initiate_queens_blood_match",
        "outcome": "mulligan_ready",
        "target": "SENSITIVE_TARGET_SENTINEL",
        "trace": "SENSITIVE_TRACE_SENTINEL",
    }
    kind = public_event_for_action(action)
    assert kind == "queens_blood_started"
    write_public_event(tmp_path, kind)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    encoded = files[0].read_text(encoding="utf-8")
    assert "SENSITIVE" not in encoded
    assert set(json.loads(encoded)) == {"version", "kind", "at"}


def test_public_event_export_ignores_failed_and_unknown_actions():
    assert public_event_for_action({"ok": False, "action": "play_queens_blood_match", "won": True}) is None
    assert public_event_for_action({"ok": True, "action": "unreviewed_action", "secret": "x"}) is None
    assert public_event_for_action({"ok": True, "action": "play_queens_blood_match", "outcome": "match_finished", "won": "yes"}) is None
    assert public_event_for_action({"ok": True, "action": "travel_forward_chunk", "position": "secret"}) == "navigating"
    assert public_event_for_action({"ok": True, "action": "combat_tap", "target": "secret"}) == "combat_action"


def test_public_event_writer_rejects_unreviewed_text(tmp_path):
    with pytest.raises(ValueError):
        write_public_event(tmp_path, "SENSITIVE_TRACE_SENTINEL")
    assert list(tmp_path.iterdir()) == []


def test_match_stats_export_only_verified_numbers(tmp_path):
    action = {
        "ok": True, "action": "play_queens_blood_match", "outcome": "match_finished",
        "won": True, "elapsed_seconds": 92.7,
        "opponent": "SENSITIVE_OPPONENT_SENTINEL",
        "trace": "SENSITIVE_TRACE_SENTINEL",
    }
    assert write_public_match_result(tmp_path, action) is True
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    encoded = files[0].read_text(encoding="utf-8")
    assert "SENSITIVE" not in encoded
    record = json.loads(encoded)
    assert set(record) == {"version", "result", "durationSeconds", "at", "driver"}
    assert record["result"] == "win" and record["durationSeconds"] == 93
    assert record["version"] == 2 and record["driver"] is None


def test_draw_and_unverified_results_are_not_counted_as_losses(tmp_path):
    draw = {"ok": True, "action": "play_queens_blood_match",
            "outcome": "match_finished", "won": False,
            "score": {"player": 12, "opponent": 12}, "elapsed_seconds": 30.0}
    assert public_event_for_action(draw) == "queens_blood_draw"
    assert write_public_match_result(tmp_path, draw) is True
    assert json.loads(next(tmp_path.glob("*.json")).read_text())["result"] == "draw"
    assert write_public_match_result(tmp_path, {**draw, "ok": False}) is False
    assert write_public_match_result(tmp_path, {**draw, "elapsed_seconds": float("nan")}) is False
    assert write_public_match_result(tmp_path, {**draw, "elapsed_seconds": "30"}) is False
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_current_match_exposes_only_reviewed_opponent_and_closes_on_terminal_play(tmp_path):
    marker = tmp_path / "current.json"
    started = {"ok": True, "action": "initiate_queens_blood_match",
               "outcome": "mulligan_ready", "target": {"name": "Turner", "secret": "SENSITIVE"}}
    assert write_public_match_state(marker, started)
    assert json.loads(marker.read_text())["opponent"] == "Turner"
    assert "SENSITIVE" not in marker.read_text()
    assert write_public_match_state(marker, {**started, "target": {"name": "INJECTION SENTINEL"}})
    assert json.loads(marker.read_text())["opponent"] == "Unknown"
    assert write_public_match_state(marker, {"action": "play_queens_blood_match", "ok": False})
    assert json.loads(marker.read_text())["active"] is False


RUN_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


def _declare(state, *, driver="Claude", model="claude-opus-5-5", effort="high",
             started="2026-09-30T12:00:00.000Z", declared="2026-09-30T12:01:00.000Z",
             live_started="2026-09-30T12:00:02.000Z", live_at="2026-09-30T13:00:00.000Z"):
    (state / "public-driver.json").write_text(json.dumps({
        "version": 1, "driver": driver, "modelId": model, "effort": effort, "runId": RUN_ID,
        "streamStartedAt": started, "declaredAt": declared}), encoding="utf-8")
    if live_at is not None:
        (state / "public-stream-live.json").write_text(json.dumps({
            "version": 1, "streamStartedAt": live_started, "at": live_at}), encoding="utf-8")


def _at(text):
    from datetime import datetime
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def test_matches_belong_to_the_driver_declared_for_the_live_stream(tmp_path):
    from ff7r_agent.public_stream_events import stream_driver

    now = _at("2026-09-30T13:00:20Z")
    _declare(tmp_path)
    assert stream_driver(tmp_path, now) == {
        "driver": "Claude", "model_id": "claude-opus-5-5", "effort": "high", "run_id": RUN_ID}
    action = {"ok": True, "action": "finish_queens_blood_match", "outcome": "match_finished",
              "won": False, "score": {"player": 3, "opponent": 9}, "elapsed_seconds": 60.0}
    assert write_public_match_result(tmp_path / "results", action, driver=stream_driver(tmp_path, now))
    record = json.loads(next((tmp_path / "results").glob("*.json")).read_text())
    assert record["driver"] == {"driver": "Claude", "modelId": "claude-opus-5-5", "effort": "high", "runId": RUN_ID}


def test_no_owner_without_a_live_stream_and_a_declaration_for_it(tmp_path):
    from ff7r_agent.public_stream_events import stream_driver

    now = _at("2026-09-30T13:00:20Z")
    assert stream_driver(tmp_path, now) is None  # nothing declared
    _declare(tmp_path, live_at=None)
    assert stream_driver(tmp_path, now) is None  # streamer not confirming a stream
    _declare(tmp_path, live_at="2026-09-30T12:58:00.000Z")
    assert stream_driver(tmp_path, now) is None  # stream heartbeat stale (stream ended)
    _declare(tmp_path, live_started="2026-09-30T14:00:00.000Z")
    assert stream_driver(tmp_path, now) is None  # declared for an earlier stream
    _declare(tmp_path, driver="SomeBot")
    assert stream_driver(tmp_path, now) is None
    _declare(tmp_path, model="gpt 6; drop")
    assert stream_driver(tmp_path, now) is None
    _declare(tmp_path, effort="extreme")
    assert stream_driver(tmp_path, now) is None
    (tmp_path / "public-driver.json").write_text("{", encoding="utf-8")
    assert stream_driver(tmp_path, now) is None


# --- Agent speech (say_on_stream) ---------------------------------------------------------

from pathlib import Path

from ff7r_agent.public_stream_events import (
    AGENT_SAY_MAX_PER_WINDOW, AGENT_SAY_MIN_GAP_SECONDS, AgentSpeechLimiter, agent_speech_problem,
    speech_tone_problem, stream_is_live, write_agent_speech,
)

# Shared with tests/streamer-safety.test.ts so the harness and streamer agree.
SPEECH_CASES = json.loads(
    (Path(__file__).parents[3] / "tests" / "fixtures" / "agent-speech-cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("text", SPEECH_CASES["accepted"])
def test_agent_speech_accepts_the_shared_cases(text):
    assert agent_speech_problem(text) is None


@pytest.mark.parametrize("text", SPEECH_CASES["rejected"])
def test_agent_speech_rejects_the_shared_cases(text):
    assert agent_speech_problem(text) is not None


@pytest.mark.parametrize("tone", SPEECH_CASES["tones"]["accepted"])
def test_agent_speech_accepts_the_shared_tones(tone):
    assert speech_tone_problem(tone) is None


@pytest.mark.parametrize("tone", SPEECH_CASES["tones"]["rejected"])
def test_agent_speech_rejects_the_shared_tones(tone):
    assert speech_tone_problem(tone) is not None


def test_agent_speech_spool_record_carries_a_tone_only_when_given(tmp_path):
    write_agent_speech(tmp_path / "toned", "Oh, I did not see that coming.", "surprised")
    [file] = list((tmp_path / "toned").glob("*.json"))
    record = json.loads(file.read_text(encoding="utf-8"))
    assert set(record) == {"version", "kind", "text", "tone", "at"} and record["tone"] == "surprised"
    with pytest.raises(ValueError):
        write_agent_speech(tmp_path / "bad", "That lane is theirs now.", "angry")
    assert not (tmp_path / "bad").exists()


def test_agent_speech_spool_record_holds_only_the_normalised_line(tmp_path):
    write_agent_speech(tmp_path, "  I'm trying   the other side.  ")
    [file] = list(tmp_path.glob("*.json"))
    record = json.loads(file.read_text(encoding="utf-8"))
    assert set(record) == {"version", "kind", "text", "at"}
    assert record["kind"] == "agent_say" and record["text"] == "I'm trying the other side."
    with pytest.raises(ValueError):
        write_agent_speech(tmp_path, "Support me on Ko-fi")
    assert len(list(tmp_path.iterdir())) == 1


def test_agent_speech_limiter_paces_caps_and_blocks_repeats():
    limiter = AgentSpeechLimiter()
    limiter.record("Line 0.", 0.0)
    assert limiter.problem("Line 1.", AGENT_SAY_MIN_GAP_SECONDS - 1) is not None
    assert limiter.problem("line 0.", AGENT_SAY_MIN_GAP_SECONDS) is not None
    now = 0.0
    for number in range(1, AGENT_SAY_MAX_PER_WINDOW):
        now += AGENT_SAY_MIN_GAP_SECONDS
        assert limiter.problem(f"Line {number}.", now) is None
        limiter.record(f"Line {number}.", now)
    assert limiter.problem("Line 99.", now + AGENT_SAY_MIN_GAP_SECONDS) == f"at most {AGENT_SAY_MAX_PER_WINDOW} lines per 10 minutes"


def test_agent_speech_limiter_asks_for_new_words_and_new_openings():
    # #182: stock phrases and the same opening every move sound robotic.
    limiter = AgentSpeechLimiter()
    limiter.record("I'll place Crystalline Crab in the bottom lane to take the centre.", 0.0)
    near = limiter.problem("I'll place Crystalline Crab in the bottom lane to take back the centre.", 10.0)
    assert near is not None and near.startswith("repetitive")
    opening = limiter.problem("I'll place Elphadunk up top for its bonus.", 10.0)
    assert opening is not None and "opened the same way" in opening
    assert limiter.problem("Oh, they went straight for the top lane.", 10.0) is None
    # After five minutes a similar line is fine again; openings only look at the last three lines.
    for number, line in enumerate(("Momentum is with us now.", "That middle lane is the key.",
                                   "They have two cards left at most."), start=1):
        limiter.record(line, 10.0 * number)
    assert limiter.problem("I'll place Elphadunk up top for its bonus.", 400.0) is None


def _live(state_dir, at):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "public-stream-live.json").write_text(json.dumps(
        {"version": 1, "streamStartedAt": "2026-10-01T09:00:00+00:00", "at": at}), encoding="utf-8")


def test_stream_is_live_only_with_a_fresh_heartbeat(tmp_path):
    assert stream_is_live(tmp_path) is False
    _live(tmp_path, "2026-10-01T10:00:00+00:00")
    now = datetime.fromisoformat("2026-10-01T10:00:30+00:00").timestamp()
    assert stream_is_live(tmp_path, now) is True
    assert stream_is_live(tmp_path, now + 60) is False


def test_say_on_stream_writes_only_while_live_and_reports_why_not(tmp_path):
    from ff7r_agent.service import AgentService
    from ff7r_agent.input import SimulationInput
    from ff7r_agent.telemetry import SimulationTelemetry

    events = tmp_path / "public-stream-events"
    service = AgentService(SimulationTelemetry(), SimulationInput(), public_stream_event_dir=events)
    assert service.say_on_stream("Trying the other side now.")["reason"] == "not_live"
    _live(tmp_path, datetime.now(timezone.utc).isoformat())
    assert service.say_on_stream("Read C:/state aloud")["reason"] == "invalid_text"
    assert service.say_on_stream("Trying the other side now.", tone="angry")["reason"] == "invalid_tone"
    assert service.say_on_stream("Trying the other side now.", tone="tense")["ok"] is True
    assert service.say_on_stream("And now something else.")["reason"] == "rate_limited"
    assert len(list(events.glob("*.json"))) == 1
    assert AgentService(SimulationTelemetry(), SimulationInput()).say_on_stream("Hello there.")["reason"] == "stream_events_disabled"


def test_a_rehearsal_lets_the_agent_speak_but_never_credits_a_stream(tmp_path):
    from ff7r_agent.public_stream_events import rehearsal_is_live
    from ff7r_agent.service import AgentService
    from ff7r_agent.input import SimulationInput
    from ff7r_agent.telemetry import SimulationTelemetry

    (tmp_path / "public-stream-rehearsal.json").write_text(json.dumps(
        {"version": 1, "at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    assert rehearsal_is_live(tmp_path) and not stream_is_live(tmp_path)
    service = AgentService(SimulationTelemetry(), SimulationInput(), public_stream_event_dir=tmp_path / "public-stream-events")
    spoken = service.say_on_stream("Heading to the merchant now.")
    assert spoken["ok"] is True and spoken["rehearsal"] is True
    old = datetime.fromisoformat("2026-10-01T10:00:00+00:00")
    (tmp_path / "public-stream-rehearsal.json").write_text(json.dumps({"version": 1, "at": old.isoformat()}), encoding="utf-8")
    assert not rehearsal_is_live(tmp_path, old.timestamp() + 61)


def _declaration(started: str, declared: str) -> dict:
    return {"version": 1, "driver": "ChatGPT", "modelId": "gpt-6-sol", "effort": "high",
            "runId": "0b5c1b2e-6f4f-4c3a-9f0e-2d3b4a5c6d7e", "streamStartedAt": started, "declaredAt": declared}


def test_a_rehearsal_declaration_credits_only_the_rehearsal_folders(tmp_path):
    from ff7r_agent.public_stream_events import match_record_target, rehearsal_driver, stream_driver

    now = datetime.fromisoformat("2026-10-03T18:00:30+00:00").timestamp()
    (tmp_path / "public-stream-rehearsal.json").write_text(json.dumps(
        {"version": 1, "at": "2026-10-03T18:00:20+00:00", "recordingStartedAt": "2026-10-03T17:00:00+00:00"}),
        encoding="utf-8")
    (tmp_path / "public-driver-rehearsal.json").write_text(json.dumps(
        _declaration("2026-10-03T17:00:02+00:00", "2026-10-03T17:01:00+00:00")), encoding="utf-8")
    driver = rehearsal_driver(tmp_path, now)
    assert driver == {"driver": "ChatGPT", "model_id": "gpt-6-sol", "effort": "high",
                      "run_id": "0b5c1b2e-6f4f-4c3a-9f0e-2d3b4a5c6d7e"}
    assert stream_driver(tmp_path, now) is None
    assert match_record_target(tmp_path, now) == (driver, "public-qb-results-rehearsal", "qb-match-history-rehearsal")


def test_a_rehearsal_declaration_for_another_recording_credits_nobody(tmp_path):
    from ff7r_agent.public_stream_events import match_record_target

    now = datetime.fromisoformat("2026-10-03T18:00:30+00:00").timestamp()
    (tmp_path / "public-stream-rehearsal.json").write_text(json.dumps(
        {"version": 1, "at": "2026-10-03T18:00:20+00:00", "recordingStartedAt": "2026-10-03T17:30:00+00:00"}),
        encoding="utf-8")
    (tmp_path / "public-driver-rehearsal.json").write_text(json.dumps(
        _declaration("2026-10-03T17:00:02+00:00", "2026-10-03T17:01:00+00:00")), encoding="utf-8")
    assert match_record_target(tmp_path, now) == (None, "public-qb-results-rehearsal", "qb-match-history-rehearsal")


def test_a_live_declaration_never_credits_a_rehearsal_and_offline_play_stays_live_unverified(tmp_path):
    from ff7r_agent.public_stream_events import match_record_target

    now = datetime.fromisoformat("2026-10-03T18:00:30+00:00").timestamp()
    (tmp_path / "public-driver.json").write_text(json.dumps(
        _declaration("2026-10-03T17:00:00+00:00", "2026-10-03T17:01:00+00:00")), encoding="utf-8")
    assert match_record_target(tmp_path, now) == (None, "public-qb-results", "qb-match-history")
    (tmp_path / "public-stream-rehearsal.json").write_text(json.dumps(
        {"version": 1, "at": "2026-10-03T18:00:20+00:00", "recordingStartedAt": "2026-10-03T17:00:00+00:00"}),
        encoding="utf-8")
    assert match_record_target(tmp_path, now) == (None, "public-qb-results-rehearsal", "qb-match-history-rehearsal")
    _live(tmp_path, "2026-10-03T18:00:20+00:00")
    assert match_record_target(tmp_path, now)[1:] == ("public-qb-results", "qb-match-history")
