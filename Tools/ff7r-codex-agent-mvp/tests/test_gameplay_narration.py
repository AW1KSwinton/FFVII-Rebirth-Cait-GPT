from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from ff7r_agent.gameplay_narration import AgentSpeechPublisher
from ff7r_agent.public_stream_events import AGENT_SAY_MAX_PER_WINDOW, AGENT_SAY_MIN_GAP_SECONDS


def on_air(directory, *, rehearsal=False):
    name = "public-stream-rehearsal.json" if rehearsal else "public-stream-live.json"
    record = {"version": 1, "at": datetime.now(timezone.utc).isoformat()}
    if not rehearsal:
        record["streamStartedAt"] = "2026-10-07T17:00:00+00:00"
    (directory / name).write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize("rehearsal", [False, True])
def test_a_session_can_explain_plans_and_react_across_gameplay(tmp_path, rehearsal):
    on_air(tmp_path, rehearsal=rehearsal)
    now = [0.0]
    events = tmp_path / "public-stream-events"
    publisher = AgentSpeechPublisher(events, monotonic=lambda: now[0])
    # Public observations are supplied by the player, never inferred from tool
    # responses or generated from backend state. No game/service objects exist.
    lines = [
        ("Kalm is our next stop. I'll fast travel there and find the card shop.", "calm"),
        ("There's a staircase ahead. I'll take it up to the square.", "neutral"),
        ("We're beside the shop now. I'll ask what card packs they have.", "confident"),
        ("That pack costs more than we have. I'll leave it for now.", "concerned"),
        ("Cloud's health is low. That bench should give us a chance to rest.", "calm"),
        ("The enemy is winding up a big attack. I'll guard and look for an opening.", "tense"),
        ("Oh, they went straight for the top lane. I did not see that coming.", "surprised"),
    ]
    for text, tone in lines:
        result = publisher.say(text, tone)
        assert result["ok"] is True and result["rehearsal"] is rehearsal
        assert result["input_sent"] is False
        now[0] += 12.0
    records = [json.loads(file.read_text(encoding="utf-8")) for file in events.glob("*.json")]
    assert {(record["text"], record["tone"]) for record in records} == set(lines)
    assert all(set(record) == {"version", "kind", "text", "tone", "at"} for record in records)
    assert not list(events.glob("*.tmp"))


def test_retry_delay_covers_the_gap_and_the_full_window(tmp_path):
    on_air(tmp_path)
    now = [0.0]
    publisher = AgentSpeechPublisher(tmp_path / "events", monotonic=lambda: now[0])
    assert publisher.say("Line 0.")["ok"]
    now[0] = 0.2
    limited = publisher.say("Line 1.")
    assert limited["reason"] == "rate_limited" and limited["retry_after_seconds"] == 8
    assert limited["input_sent"] is False
    now[0] = 8.0
    repeated = publisher.say("Line 0.")
    assert repeated["reason"] == "repetitive" and "retry_after_seconds" not in repeated
    for index in range(1, AGENT_SAY_MAX_PER_WINDOW):
        now[0] = index * AGENT_SAY_MIN_GAP_SECONDS
        assert publisher.say(f"Line {index}.")["ok"]
    now[0] += AGENT_SAY_MIN_GAP_SECONDS
    capped = publisher.say("Line 99.")
    assert capped["reason"] == "rate_limited" and capped["retry_after_seconds"] == 120
    now[0] += capped["retry_after_seconds"]
    assert publisher.say("Line 99.")["ok"]


def test_failed_write_is_not_counted_and_unsafe_lines_never_reach_the_spool(tmp_path, monkeypatch):
    from ff7r_agent import gameplay_narration

    on_air(tmp_path)
    publisher = AgentSpeechPublisher(tmp_path / "events", monotonic=lambda: 0.0)
    for text in ("I'll restart the worker before the next move.", "I'll read the API credentials."):
        assert publisher.say(text)["reason"] == "invalid_text"
    real_write = gameplay_narration.write_agent_speech
    writes = []

    def failed_write(*args):
        writes.append(args)
        raise OSError("private path must never become public speech")

    monkeypatch.setattr(gameplay_narration, "write_agent_speech", failed_write)
    failed = publisher.say("I'll follow the stairs to the square.")
    assert failed == {"ok": False, "reason": "spool_unavailable", "input_sent": False}
    assert len(writes) == 1 and not (tmp_path / "events").exists()
    monkeypatch.setattr(gameplay_narration, "write_agent_speech", real_write)
    assert publisher.say("I'll follow the stairs to the square.")["ok"]



def test_invalid_tone_and_repeated_placement_line_do_not_queue_speech(tmp_path):
    on_air(tmp_path)
    now = [0.0]
    events = tmp_path / "events"
    publisher = AgentSpeechPublisher(events, monotonic=lambda: now[0])

    invalid = publisher.say("I'll put the Wolf in the center lane.", "focused")
    assert invalid["ok"] is False and invalid["reason"] == "invalid_tone"
    assert invalid["input_sent"] is False
    assert not list(events.glob("*.json"))

    first = publisher.say("I'll put the Wolf in the center lane.", "calm")
    assert first["ok"] is True
    now[0] = 8.0
    repeated = publisher.say("I'll put the Wolf in the center lane.", "calm")
    assert repeated["ok"] is False and repeated["reason"] == "repetitive"
    assert repeated["input_sent"] is False

    # A new observed reaction, not another rendering of the same pending move.
    fresh = publisher.say("Nene blocked our top lane, so I'll defend the middle.", "concerned")
    assert fresh["ok"] is True
    assert len(list(events.glob("*.json"))) == 2


def test_concurrent_speech_requests_cannot_bypass_pacing(tmp_path):
    on_air(tmp_path)
    publisher = AgentSpeechPublisher(tmp_path / "events", monotonic=lambda: 0.0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: publisher.say(f"Plan {n}: I'll head to the square."), range(8)))
    assert sum(result["ok"] for result in results) == 1
    assert sum(result.get("reason") == "rate_limited" for result in results) == 7
    assert len(list((tmp_path / "events").glob("*.json"))) == 1


def test_publishers_and_restarts_share_pacing_and_repetition(tmp_path):
    on_air(tmp_path)
    events = tmp_path / "events"
    first = AgentSpeechPublisher(events, monotonic=lambda: 0.0)
    assert first.say("The square is ahead. Let's follow those stairs.")["ok"]
    restarted = AgentSpeechPublisher(events, monotonic=lambda: 2.0)
    refused = restarted.say("Cloud could use a rest at that bench.")
    assert refused["reason"] == "rate_limited" and refused["retry_after_seconds"] == 6
    restarted = AgentSpeechPublisher(events, monotonic=lambda: 8.0)
    assert restarted.say("The square is ahead. Let's follow those stairs.")["reason"] == "repetitive"
    assert restarted.say("Cloud could use a rest at that bench.")["ok"]
    # Private gate records must never enter the streamer's event intake.
    assert all(json.loads(p.read_text())["kind"] == "agent_say" for p in events.glob("*.json"))


def test_a_separate_process_cannot_bypass_the_gate(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    import ff7r_agent

    on_air(tmp_path)
    events = tmp_path / "events"
    assert AgentSpeechPublisher(events, monotonic=lambda: 0.0).say("That top lane looks dangerous.")["ok"]
    code = ("import json; from pathlib import Path; "
            "from ff7r_agent.gameplay_narration import AgentSpeechPublisher; "
            f"print(json.dumps(AgentSpeechPublisher(Path({str(events)!r}), monotonic=lambda: 1.0).say('Our middle lane is still holding.')))" )
    env = dict(os.environ, PYTHONPATH=str(Path(ff7r_agent.__file__).parent.parent))
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["reason"] == "rate_limited"
    assert len(list(events.glob("*.json"))) == 1


def test_contention_and_corrupt_history_refuse_without_a_public_write(tmp_path):
    on_air(tmp_path)
    events = tmp_path / "events"
    publisher = AgentSpeechPublisher(events, monotonic=lambda: 0.0)
    with publisher._shared_gate(events) as acquired:
        assert acquired
        result = AgentSpeechPublisher(events).say("That bench should restore Cloud's health.")
        assert result["reason"] == "rate_limited" and result["retry_after_seconds"] == 1
    (tmp_path / ".events-speech-history.json").write_text('{broken')
    assert publisher.say("That bench should restore Cloud's health.")["reason"] == "spool_unavailable"
    assert not events.exists()


def test_stale_heartbeat_does_not_reset_shared_history(tmp_path):
    from datetime import timedelta

    on_air(tmp_path)
    events = tmp_path / "events"
    assert AgentSpeechPublisher(events, monotonic=lambda: 0.0).say("Let's follow the path to the square.")["ok"]
    heartbeat = tmp_path / "public-stream-live.json"
    live = json.loads(heartbeat.read_text())
    live["at"] = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
    heartbeat.write_text(json.dumps(live))
    restarted = AgentSpeechPublisher(events, monotonic=lambda: 8.0)
    assert restarted.say("Cloud could rest at that bench.")["reason"] == "not_live"
    on_air(tmp_path)
    assert restarted.say("Let's follow the path to the square.")["reason"] == "repetitive"


def test_persistence_failure_never_queues_untracked_speech(tmp_path, monkeypatch):
    on_air(tmp_path)
    events = tmp_path / "events"
    publisher = AgentSpeechPublisher(events)
    def unavailable(*args):
        raise OSError("private history path")
    monkeypatch.setattr(publisher, "_save_history", unavailable)
    assert publisher.say("Cloud could rest at that bench.") == {
        "ok": False, "reason": "spool_unavailable", "input_sent": False}
    assert not events.exists()


def test_a_clock_rollback_does_not_clear_the_reserved_gap(tmp_path):
    on_air(tmp_path)
    events = tmp_path / "events"
    assert AgentSpeechPublisher(events, monotonic=lambda: 100.0).say("The top lane needs defending.")["ok"]
    result = AgentSpeechPublisher(events, monotonic=lambda: 90.0).say("Let's build our middle lane.")
    assert result["reason"] == "rate_limited" and result["retry_after_seconds"] == 18
    assert len(list(events.glob("*.json"))) == 1


def consumer_lease(directory, now, *, mode="live", busy=False, available=True):
    path=directory/f"speech-consumer-{mode}.json"
    path.write_text(json.dumps({"version":1,"mode":mode,"at":datetime.fromtimestamp(now,timezone.utc).isoformat(),
                                "busy":busy,"available":available}),encoding="utf-8")


@pytest.mark.parametrize("rehearsal",[False,True])
def test_long_delivery_backpressure_refuses_without_new_queue_or_pacing_reservation(tmp_path,rehearsal):
    on_air(tmp_path,rehearsal=rehearsal);mode="rehearsal" if rehearsal else "live"
    now=[1000.0];events=tmp_path/"events";publisher=AgentSpeechPublisher(events,monotonic=lambda:now[0])
    consumer_lease(tmp_path,now[0],mode=mode,busy=True)
    refused=publisher.say("I can spread along the top while keeping the middle clear.")
    assert refused["reason"]=="rate_limited" and refused["retry_after_seconds"]==2
    assert not list(events.glob("*.json"))
    now[0]+=31;consumer_lease(tmp_path,now[0],mode=mode,busy=True)
    assert publisher.say("I can spread along the top while keeping the middle clear.")["reason"]=="rate_limited"
    consumer_lease(tmp_path,now[0],mode=mode)
    assert publisher.say("I can spread along the top while keeping the middle clear.")["ok"]


def test_pending_accepted_line_prevents_a_second_queue_even_after_minimum_gap(tmp_path):
    on_air(tmp_path);now=[1000.0];events=tmp_path/"events"
    consumer_lease(tmp_path,now[0]);publisher=AgentSpeechPublisher(events,monotonic=lambda:now[0])
    assert publisher.say("I'll keep the middle open and build along the top.")["ok"]
    now[0]+=8;consumer_lease(tmp_path,now[0])
    refused=publisher.say("His challenge is opening; I want room to spread into more than one lane.")
    assert refused["reason"]=="rate_limited" and refused["retry_after_seconds"]==2
    assert len(list(events.glob("*.json")))==1
    next(events.glob("*.json")).unlink()  # Fake consumer has finished the first line.
    assert publisher.say("His challenge is opening; I want room to spread into more than one lane.")["ok"]


def test_delivery_admission_modes_are_isolated_and_expired_consumers_fail_closed(tmp_path):
    on_air(tmp_path);now=[1000.0];events=tmp_path/"events"
    consumer_lease(tmp_path,now[0],mode="rehearsal",busy=True)
    consumer_lease(tmp_path,now[0],mode="live")
    publisher=AgentSpeechPublisher(events,monotonic=lambda:now[0])
    assert publisher.say("I'll hold the centre and leave room to expand.")["ok"]
    next(events.glob("*.json")).unlink();now[0]+=8
    assert publisher.say("I can build along the top and keep the middle open.")["reason"]=="speech_delivery_unavailable"
    assert not list(events.glob("*.json"))


@pytest.mark.parametrize("other_only",[False,True])
def test_known_unavailable_or_wrong_mode_consumer_cannot_accept_speech(tmp_path,other_only):
    on_air(tmp_path);consumer_lease(tmp_path,1000,mode="rehearsal" if other_only else "live",available=other_only)
    result=AgentSpeechPublisher(tmp_path/"events",monotonic=lambda:1000).say("I'll build along the top and leave room in the middle.")
    assert result["reason"]=="speech_delivery_unavailable" and result["input_sent"] is False
    assert not list((tmp_path/"events").glob("*.json"))


@pytest.mark.parametrize("claim", [None, {}, {"version":1,"mode":"rehearsal","instance_id":"instance"},
                                  {"version":1,"mode":"live","instance_id":"different"}])
def test_owned_consumer_snapshot_requires_matching_instance_claim(tmp_path, claim):
    on_air(tmp_path); consumer_lease(tmp_path,1000)
    path=tmp_path/"speech-consumer-live.json"
    data=json.loads(path.read_text()); data["instance_id"]="instance"; path.write_text(json.dumps(data))
    if claim is not None:
        path.with_name(path.name+".owner.json").write_text(json.dumps(claim))
    publisher=AgentSpeechPublisher(tmp_path/"events",monotonic=lambda:1000)
    assert publisher.say("I'll build along the top and keep the middle clear.")["reason"]=="speech_delivery_unavailable"
    assert not list((tmp_path/"events").glob("*.json"))


def test_owned_consumer_snapshot_accepts_only_after_matching_claim_is_observed(tmp_path):
    on_air(tmp_path); consumer_lease(tmp_path,1000)
    path=tmp_path/"speech-consumer-live.json"
    data=json.loads(path.read_text()); data["instance_id"]="instance"; path.write_text(json.dumps(data))
    path.with_name(path.name+".owner.json").write_text(json.dumps({"version":1,"mode":"live","instance_id":"instance"}))
    assert AgentSpeechPublisher(tmp_path/"events",monotonic=lambda:1000).say("I'll build along the top and keep the middle clear.")["ok"]


def test_starting_known_owned_consumer_without_snapshot_fails_closed(tmp_path):
    on_air(tmp_path)
    (tmp_path/"speech-consumer-live.json.owner.json").write_text(json.dumps({"version":1,"mode":"live","instance_id":"starting"}))
    assert AgentSpeechPublisher(tmp_path/"events",monotonic=lambda:1000).say("I'll build along the top and keep the middle clear.")["reason"]=="speech_delivery_unavailable"


@pytest.mark.parametrize("snapshot", [False, True])
def test_new_instance_claim_never_falls_back_to_unowned_legacy_admission(tmp_path, snapshot):
    on_air(tmp_path)
    if snapshot: consumer_lease(tmp_path,1000)
    (tmp_path/"speech-consumer-live.json.owner.json").write_text(json.dumps({"version":1,"mode":"live","instance_id":"starting"}))
    assert AgentSpeechPublisher(tmp_path/"events",monotonic=lambda:1000).say("I'll build along the top and keep the middle clear.")["reason"]=="speech_delivery_unavailable"


def delivery_health(directory, *, mode="live", failures=0, ready=True, status="media_completed"):
    consumer_lease(directory, 1000, mode=mode)
    path = directory / f"speech-consumer-{mode}.json"
    lease = json.loads(path.read_text())
    lease.update(speech_ready=ready, consecutive_failures=failures, failure_limit=2,
                 previous_delivery={"status": status, "queued_at": "1970-01-01T00:16:20Z",
                                    "at": "1970-01-01T00:16:25Z",
                                    "error": {"name": "Error", "message": "OBS voice playback: request failed", "code": "ENOENT"}})
    path.write_text(json.dumps(lease))
    return path, lease


@pytest.mark.parametrize("rehearsal", [False, True])
def test_previous_failure_is_visible_in_the_next_accepted_line_and_modes_are_isolated(tmp_path, rehearsal):
    on_air(tmp_path, rehearsal=rehearsal)
    mode = "rehearsal" if rehearsal else "live"
    delivery_health(tmp_path, mode="live" if rehearsal else "rehearsal", failures=20, ready=False, status="failed")
    delivery_health(tmp_path, mode=mode, failures=1, status="failed")
    result = AgentSpeechPublisher(tmp_path / "events", monotonic=lambda:1000).say("I'll build along the top and keep the middle clear.")
    assert result["ok"] is True and result["rehearsal"] is rehearsal
    assert result["previous_delivery"]["status"] == "failed"
    assert result["previous_delivery"]["error"]["message"] == "OBS voice playback: request failed"
    assert result["consecutive_failures"] == 1


@pytest.mark.parametrize("rehearsal", [False, True])
@pytest.mark.parametrize("ready,failures,reason", [(True, 2, "speech_delivery_failing"), (True, 20, "speech_delivery_failing"),
                                                  (False, 0, "speech_runtime_unavailable")])
def test_delivery_failure_gate_rejects_without_queue_or_reservation(tmp_path, rehearsal, ready, failures, reason):
    on_air(tmp_path, rehearsal=rehearsal)
    mode = "rehearsal" if rehearsal else "live"
    path, lease = delivery_health(tmp_path, mode=mode, failures=failures, ready=ready, status="failed")
    events = tmp_path / "events"
    publisher = AgentSpeechPublisher(events, monotonic=lambda:1000)
    line = "I'll build along the top and keep the middle clear."
    result = publisher.say(line)
    assert result["ok"] is False and result["reason"] == reason and result["input_sent"] is False
    assert "receipt_id" not in result and not list(events.glob("*.json"))
    assert not (tmp_path / ".events-speech-history.json").exists()
    lease.update(speech_ready=True, consecutive_failures=0)
    lease["previous_delivery"]["status"] = "media_completed"
    path.write_text(json.dumps(lease))
    assert publisher.say(line)["ok"] is True


@pytest.mark.parametrize("key,value", [("speech_ready", "true"), ("consecutive_failures", True), ("consecutive_failures", -1),
                                      ("failure_limit", 100), ("previous_delivery", {}), ("previous_delivery", {"status": []})])
def test_malformed_delivery_health_fails_closed(tmp_path, key, value):
    on_air(tmp_path)
    path, lease = delivery_health(tmp_path)
    lease[key] = value
    path.write_text(json.dumps(lease))
    result = AgentSpeechPublisher(tmp_path / "events", monotonic=lambda:1000).say("I'll build along the top and keep the middle clear.")
    assert result["reason"] == "speech_delivery_unavailable" and result["ok"] is False
