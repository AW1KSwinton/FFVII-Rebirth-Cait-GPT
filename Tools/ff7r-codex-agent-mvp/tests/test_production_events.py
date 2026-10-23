import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ff7r_agent import production_events


def _at(seconds=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _snapshot(position, *, paused=False, duration_rollback=False, recording_id="recording-a",
              path="C:/capture/session.mkv", samples=None):
    at = _at(1) if position < 3000 else _at(0)
    return {
        "recordingId": recording_id, "sessionId": "session-a", "startedAt": _at(30),
        "observedAt": at, "positionMs": position, "positionKnown": True, "provenance": "monitor-session",
        "visualProof": True, "paused": paused, "recordingPath": path, "outputPath": path,
        "durationMs": position, "durationObservedAt": at, "durationRollback": duration_rollback,
        "recordingActive": True, "recordingComplete": False, "coverageStartMs": 0,
        "coverageEndMs": position, "quarantined": False,
        "timingSamples": samples if samples is not None else [
            {"observedAt": at, "durationMs": position, "paused": paused, "recordingActive": True},
        ],
        "stream": {"state": "inactive", "observedAt": at, "source": "obs"},
        "monitorObservedAt": at, "obsObservedAt": at, "monitorFresh": True, "obsFresh": True,
    }


def _context():
    return {
        "feature": "pursuit-route", "featureVersion": "route-pursuit-v3",
        "behaviorIdentity": "pursuit.arrival.fresh-final-sample",
        "testObjective": "reach the requested marker using verified pursuit",
        "expectedBehavior": "completed route reports ARRIVED after a fresh final observation",
    }


def _report(tmp_path, **overrides):
    return {
        "ok": True, "state": "ARRIVED", "dry_run": False, "movement_attempted": True,
        "route_state": "ARRIVED", "started_at_utc": _at(1), "ended_at_utc": _at(),
        "runtime_identity": {"git_commit_sha": "a" * 40},
        "output_directory": str(tmp_path),
        **overrides,
    }


def test_development_event_requires_verified_nested_completion_and_timed_interval(tmp_path):
    start, end = _snapshot(1000), _snapshot(5000)
    report = _report(tmp_path)
    event = production_events.emit_development_outcome(
        state_dir=tmp_path, context=_context(), report=report,
        event_type="navigation_mission_outcome", start_snapshot=start, end_snapshot=end)

    assert event["mode"] == "development"
    assert event["development"]["result"] == "verified"
    assert event["development"]["featureVersion"] == "route-pursuit-v3"
    assert event["development"]["commit"] == "a" * 40
    assert event["recording"]["positionKnown"] is True
    assert event["interval"] == {
        "wallStartedAt": report["started_at_utc"],
        "wallEndedAt": report.get("ended_at_utc") or event["occurredAt"],
        "mediaStartMs": 1000, "mediaEndMs": 5000,
        "uncertain": False, "uncertaintyReasons": [],
    }


@pytest.mark.parametrize("report,expected", [
    ({"ok": False, "state": "STOPPED", "reason": "npc_not_located"}, "failed"),
    ({"ok": True, "state": "PREFLIGHT_PASSED", "dry_run": True}, "unknown"),
    ({"ok": True, "state": "ARRIVED", "dry_run": False, "movement_attempted": False}, "unknown"),
])
def test_development_event_preserves_failure_and_inconclusive_results(tmp_path, report, expected):
    full_report = _report(tmp_path, **report)
    event = production_events.emit_development_outcome(
        state_dir=tmp_path, context=_context(), report=full_report,
        event_type="navigation_mission_outcome", start_snapshot=None, end_snapshot=None)
    assert event["development"]["result"] == expected
    assert event["interval"]["uncertain"] is True


def test_explicit_talk_prompt_is_a_verified_npc_result(tmp_path):
    report = {"ok": True, "state": "talk_offered", "execute": True, "started_at_utc": _at(1)}
    event = production_events.emit_development_outcome(
        state_dir=tmp_path, context=_context(), report=report, event_type="npc_approach_outcome",
        start_snapshot=_snapshot(1000), end_snapshot=_snapshot(2000))
    assert event["development"]["result"] == "verified"


def _guide_moogle_report(**overrides):
    report = {
        "ok": True, "state": "arrived", "destination": "Wonderment Square",
        "started_at_utc": _at(2), "ended_at_utc": _at(),
        "menu": {
            "ok": True, "state": "travelling", "destination": "Wonderment Square",
            "destination_key": "$map2350_layer02_group3", "arrival": {"x": 70850.0, "y": 4253.0, "z": 50234.0},
            "from": "Battle Square",
            "talk_admission": {"ok": True, "actor_id": "actor-1",
                               "target": {"canonical_id": "field_object:2350-GOLDS:oba2350_guideMoogle_1",
                                          "actor_id": "actor-1"}},
        },
        "arrival_position": {"x": 70870.0, "y": 4273.0, "z": 50234.0},
        "guide_moogle_proof": {"arrival_observed": True,
                               "arrival_position": {"x": 70870.0, "y": 4273.0, "z": 50234.0},
                               "settled": True},
    }
    report.update(overrides)
    return report


def test_guide_moogle_result_requires_nested_menu_talk_arrival_and_settle_proof(tmp_path):
    report = _guide_moogle_report()
    event = production_events.emit_development_outcome(
        state_dir=tmp_path, context=_context(), report=report,
        event_type="guide_moogle_travel_outcome", start_snapshot=_snapshot(1000), end_snapshot=_snapshot(3000))
    assert event["development"]["result"] == "verified"
    assert event["interval"]["uncertain"] is False


def test_guide_moogle_route_produces_proof_only_after_nested_menu_arrival_and_settle(tmp_path, monkeypatch):
    import asyncio
    from ff7r_agent import npc_approach
    from ff7r_agent.guide_moogle import zone

    destination = zone("Wonderment Square")
    arrival = dict(zip(("x", "y", "z"), destination.arrival))
    actor = {"canonical_id": "field_object:2350-GOLDS:oba2350_guideMoogle_1", "actor_id": "actor-1"}

    async def walk(*_args, **_kwargs):
        return {"ok": True, "state": "talk_offered", "face": {"facing": {
            "admission": {"ok": True, "target": actor}}}}

    async def rpc(operation, _arguments):
        if operation == "use_guide_moogle":
            return {"ok": True, "state": "travelling", "destination": destination.name,
                    "destination_key": destination.key, "arrival": arrival, "from": "Battle Square",
                    "talk_admission": {"ok": True, "actor_id": "actor-1", "target": actor}}
        if operation == "observe_game":
            nearby = {"x": arrival["x"] + 20, "y": arrival["y"] + 20, "z": arrival["z"]}
            return {"player": {"locomotion": {"feet_position": nearby}}}
        if operation == "wait_for_settled_gameplay":
            return {"ok": True, "readiness": "settled_loaded_active_gameplay"}
        pytest.fail(f"unexpected operation: {operation}")

    monkeypatch.setattr(npc_approach, "_approach_once", walk)
    monkeypatch.setattr(npc_approach, "ensure_control_session", lambda *_: asyncio.sleep(0))
    report = asyncio.run(npc_approach.travel_by_guide_moogle(
        rpc, destination.name, output=tmp_path, identity_check=None, sleep=lambda _seconds: asyncio.sleep(0)))
    assert report["guide_moogle_proof"]["arrival_observed"] is True
    assert report["guide_moogle_proof"]["settled"] is True
    assert production_events.verified_guide_moogle_arrival({"ok": True, "state": "arrived", **report}) is True


@pytest.mark.parametrize("change", [
    {"menu": {"ok": True, "state": "travelling"}},
    {"guide_moogle_proof": {"arrival_observed": True, "settled": False}},
    {"guide_moogle_proof": {"arrival_observed": False, "settled": True}},
    {"menu": {"ok": True, "state": "arrived", "destination": "Wonderment Square",
                   "destination_key": "$map2350_layer02_group3", "arrival": {"x": 70850, "y": 4253, "z": 50234},
                   "from": "Battle Square",
                   "talk_admission": {"ok": True, "actor_id": "actor-1",
                                      "target": {"canonical_id": "field_object:2350-GOLDS:oba2350_guideMoogle_1",
                                                 "actor_id": "actor-1"}}}},
    {"menu": {"ok": True, "state": "travelling", "destination": "Wrong Square",
                   "destination_key": "wrong", "arrival": {"x": 70850, "y": 4253, "z": 50234},
                   "from": "Battle Square",
                   "talk_admission": {"ok": True, "actor_id": "actor-1",
                                      "target": {"canonical_id": "field_object:2350-GOLDS:oba2350_guideMoogle_1",
                                                 "actor_id": "actor-1"}}}},
    {"arrival_position": {"x": 5000, "y": 200, "z": 3}},
        {"ok": False, "state": "stopped"},
        {"menu": {"ok": True, "state": "travelling", "destination": "Wonderment Square",
                   "destination_key": "$map2350_layer02_group3", "arrival": {"x": 70850, "y": 4253, "z": 50234},
                   "from": "Battle Square",
                   "talk_admission": {"ok": True, "actor_id": "actor-1",
                                      "target": {"canonical_id": "field_object:2350-GOLDS:oba2350_guideMoogle_1",
                                                 "actor_id": "different-actor"}}}},
        {"arrival_position": {"x": 70870.0, "y": 4273.0, "z": 50400.0},
         "guide_moogle_proof": {"arrival_observed": True,
                                "arrival_position": {"x": 70870.0, "y": 4273.0, "z": 50400.0},
                                "settled": True}},
])
def test_guide_moogle_success_claims_without_nested_proof_remain_unknown(tmp_path, change):
    report = _guide_moogle_report(**change)
    event = production_events.emit_development_outcome(
        state_dir=tmp_path, context=_context(), report=report,
        event_type="guide_moogle_travel_outcome", start_snapshot=None, end_snapshot=None)
    assert event["development"]["result"] == "unknown"


def test_guide_moogle_failed_attempt_is_retained_as_failed(tmp_path):
    event = production_events.emit_development_outcome(
        state_dir=tmp_path, context=_context(),
        report={"ok": False, "state": "stopped", "reason": "guide_moogle_arrival_timeout"},
        event_type="guide_moogle_travel_outcome")
    assert event["development"]["result"] == "failed"


def test_guide_moogle_public_narration_fact_requires_verified_nested_arrival():
    from ff7r_agent.live_narration_events import guide_moogle_arrival_facts

    assert guide_moogle_arrival_facts(_guide_moogle_report()) == [
        "Guide Moogle transport connects Battle Square and Wonderment Square"]
    assert guide_moogle_arrival_facts({"ok": True, "state": "arrived",
                                       "destination": "Wonderment Square"}) == []


@pytest.mark.parametrize("driver,model,effort", [
    ("Claude", "claude-test-model", "high"),
    ("ChatGPT", "gpt-test-model", "xhigh"),
])
def test_dev_attribution_comes_from_current_declared_driver(monkeypatch, tmp_path, driver, model, effort):
    from ff7r_agent import public_stream_events
    monkeypatch.setattr(public_stream_events, "match_record_target",
                        lambda _path: ({"driver": driver, "model_id": model, "effort": effort}, "", ""))
    report = _report(tmp_path)
    event = production_events.emit_development_outcome(
        state_dir=tmp_path, context=_context(), report=report,
        event_type="navigation_mission_outcome", start_snapshot=_snapshot(1000), end_snapshot=_snapshot(5000))
    assert event["development"]["model"] == model
    assert event["development"]["effort"] == effort
    assert event["development"]["result"] == "verified"


def test_stream_state_is_unknown_when_missing_or_stale_and_active_when_fresh(tmp_path):
    production = tmp_path / "production"
    production.mkdir()
    (production / "current-recording.json").write_text(json.dumps(_snapshot(2000)), encoding="utf-8")
    event = production_events.build_event(state_dir=tmp_path, mode="gameplay", event_type="test",
                                          details={"gameplay": {"result": "unknown"}}, source_key="one")
    assert event["stream"]["state"] == "unknown"

    stream_at = _at()
    (production / "stream-state.json").write_text(json.dumps({
        "observedAt": stream_at, "monitorObservedAt": stream_at,
        "obsObservedAt": stream_at, "monitorFresh": True, "obsFresh": True,
        "stream": {"state": "active", "observedAt": stream_at, "source": "obs"},
        "recordingActive": True, "recordingId": "recording-a",
    }), encoding="utf-8")
    active = production_events.build_event(state_dir=tmp_path, mode="gameplay", event_type="test",
                                            details={"gameplay": {"result": "unknown"}}, source_key="two")
    assert active["stream"]["state"] == "active"
    state = json.loads((production / "stream-state.json").read_text(encoding="utf-8"))
    state["obsFresh"] = False
    (production / "stream-state.json").write_text(json.dumps(state), encoding="utf-8")
    stale = production_events.build_event(state_dir=tmp_path, mode="gameplay", event_type="test",
                                          details={"gameplay": {"result": "unknown"}}, source_key="three")
    assert stale["stream"]["state"] == "unknown"


def test_gameplay_events_include_exact_point_intervals_and_stale_timing_is_unknown(tmp_path):
    production = tmp_path / "production"
    production.mkdir()
    snapshot = _snapshot(2000)
    (production / "current-recording.json").write_text(json.dumps(snapshot), encoding="utf-8")
    event = production_events.build_event(state_dir=tmp_path, mode="gameplay", event_type="match.finish",
                                          details={"gameplay": {"result": "verified"}}, source_key="finish")
    assert event["interval"]["uncertain"] is False
    assert event["interval"]["mediaStartMs"] == event["interval"]["mediaEndMs"] == 2000
    snapshot["observedAt"] = _at(60)
    snapshot["monitorObservedAt"] = snapshot["obsObservedAt"] = snapshot["durationObservedAt"] = snapshot["observedAt"]
    snapshot["timingSamples"] = [{"observedAt": snapshot["observedAt"], "durationMs": 2000,
                                  "paused": False, "recordingActive": True}]
    (production / "current-recording.json").write_text(json.dumps(snapshot), encoding="utf-8")
    stale = production_events.build_event(state_dir=tmp_path, mode="gameplay", event_type="match.finish",
                                          details={"gameplay": {"result": "verified"}}, source_key="stale")
    assert stale["recording"]["positionKnown"] is False
    assert stale["interval"]["uncertain"] is True
    assert "observedAt_stale_or_missing" in stale["interval"]["uncertaintyReasons"]


def test_long_recording_source_history_stays_out_of_compact_accepted_events(tmp_path):
    production = tmp_path / "production"
    production.mkdir()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    samples = []
    for index in range(600):
        observed = (now - timedelta(seconds=599 - index)).isoformat(timespec="seconds").replace("+00:00", "Z")
        samples.append({"observedAt": observed, "durationMs": index * 1000,
                        "paused": False, "recordingActive": True, "sourceEvidence": "monitor-sample-" + str(index)})
    start = _snapshot(0, samples=[samples[0]])
    end = _snapshot(599000, samples=samples)
    for snapshot, sample in ((start, samples[0]), (end, samples[-1])):
        for key in ("observedAt", "durationObservedAt", "monitorObservedAt", "obsObservedAt"):
            snapshot[key] = sample["observedAt"]
    end["streamEvidenceIds"] = ["obs-evidence-" + str(index) + "-" + ("x" * 64) for index in range(600)]
    (production / "current-recording.json").write_text(json.dumps(end), encoding="utf-8")
    full_snapshot_size = len(json.dumps(end, separators=(",", ":")).encode("utf-8"))
    assert full_snapshot_size > production_events.MAX_EVENT_BYTES

    development = production_events.build_event(
        state_dir=tmp_path, mode="development", event_type="navigation_mission_outcome",
        details={"development": {"feature": "route", "featureVersion": "route-v2",
                                  "behaviorIdentity": "verified-arrival", "result": "verified"}},
        source_key="long-development-run", start_snapshot=start, end_snapshot=end,
        interval_wall_start=samples[0]["observedAt"], interval_wall_end=samples[-1]["observedAt"],
    )
    gameplay = production_events.build_event(
        state_dir=tmp_path, mode="gameplay", event_type="match.finish",
        details={"gameplay": {"mission": "queens_blood", "result": "verified",
                              "score": {"player": 24, "opponent": 18}, "won": True,
                              "notableCues": [{"cueId": "cue-1", "label": "lane_swing", "kind": "lane_swing",
                                               "swing": 8, "lane": 1, "score": {"player": 24, "opponent": 18}}]}},
        source_key="long-gameplay-match", story_id="match-long",
    )

    sink = production_events.ProductionEventSink(tmp_path)
    assert sink.emit(development) is True
    assert sink.emit(gameplay) is True
    assert sink.flush(2) is True
    status = sink.status()
    sink.close()
    assert status["accepted"] == status["written"] == 2
    for event in (development, gameplay):
        assert event["recording"]["positionMs"] == 599000
        assert event["recording"]["positionKnown"] is True
        assert "timingSamples" not in event["recording"]
        assert "streamEvidenceIds" not in event["recording"]
        assert len(production_events._canonical(event)) < production_events.MAX_EVENT_BYTES
    assert development["interval"]["mediaEndMs"] == 599000
    assert development["interval"]["uncertain"] is False
    # The authoritative monitor history remains complete for worker reconciliation.
    persisted_source = json.loads((production / "current-recording.json").read_text(encoding="utf-8"))
    assert len(persisted_source["timingSamples"]) == len(persisted_source["streamEvidenceIds"]) == 600


@pytest.mark.parametrize("stream_state,expected_accepted", [("inactive", True), ("active", False)])
def test_mcp_feature_context_uses_persisted_nested_route_result_and_stream_gate(
        monkeypatch, tmp_path, stream_state, expected_accepted):
    from types import SimpleNamespace
    from ff7r_agent import public_stream_events
    from ff7r_agent.service import AgentService

    monkeypatch.setenv("FF7R_RUNTIME_PROFILE", "FeatureValidation")
    state_dir = tmp_path / "state"
    production = state_dir / "production"
    production.mkdir(parents=True)
    start, end = _snapshot(1000), _snapshot(5000)
    (production / "current-recording.json").write_text(json.dumps(end), encoding="utf-8")
    stream_at = _at()
    (production / "stream-state.json").write_text(json.dumps({
        "observedAt": stream_at, "monitorObservedAt": stream_at, "obsObservedAt": stream_at,
        "monitorFresh": True, "obsFresh": True,
        "stream": {"state": stream_state, "observedAt": stream_at, "source": "obs"},
        "recordingActive": True, "recordingId": "recording-a",
    }), encoding="utf-8")
    monkeypatch.setattr(public_stream_events, "match_record_target", lambda _path: (
        {"driver": "Claude", "model_id": "claude-test-model", "effort": "high"}, "", ""))

    class Sink:
        def __init__(self): self.events = []; self.excluded = []
        def emit(self, event): self.events.append(event); return True
        def emit_excluded(self, event, reason): self.excluded.append((event, reason)); return True
        def status(self): return {"written": len(self.events), "dropped": 0}

    service = object.__new__(AgentService)
    service._public_stream_event_dir = state_dir / "public-stream-events"
    service._production_event_sink = Sink()
    service._wall_clock = lambda: datetime.now(timezone.utc)
    context = _context()
    service.register_production_test_context(context)
    service.begin_production_test()
    service.production_test_route_started()
    service.production_test_route_completed({"route_target_position": {"x": 1}, "run": {
        "state": "ARRIVED", "reason": None,
        "timeline": [{"event": "control_cycle", "actual_command_sent": True}],
    }})
    result = service.end_production_test()
    assert result["accepted"] is expected_accepted
    if expected_accepted:
        event = service._production_event_sink.events[0]
        assert event["development"]["result"] == "verified"
        assert event["development"]["model"] == "claude-test-model"
        assert event["development"]["effort"] == "high"
        assert event["interval"]["uncertain"] is False
    else:
        assert result["reason"] == "active_stream_excluded"
        assert result["exclusionRecorded"] is True
        assert service._production_event_sink.events == []
        assert service._production_event_sink.excluded[0][1] == "fresh_obs_stream_active"


def test_mcp_feature_context_refuses_unverified_route_success(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from ff7r_agent import public_stream_events
    from ff7r_agent.service import AgentService

    monkeypatch.setenv("FF7R_RUNTIME_PROFILE", "FEATURE_VALIDATION")
    state_dir = tmp_path / "state"
    production = state_dir / "production"
    production.mkdir(parents=True)
    (production / "current-recording.json").write_text(json.dumps(_snapshot(5000)), encoding="utf-8")
    at = _at()
    (production / "stream-state.json").write_text(json.dumps({
        "observedAt": at, "monitorObservedAt": at, "obsObservedAt": at,
        "monitorFresh": True, "obsFresh": True,
        "stream": {"state": "inactive", "observedAt": at, "source": "obs"},
    }), encoding="utf-8")
    monkeypatch.setattr(public_stream_events, "match_record_target", lambda _path: (
        {"driver": "Codex", "model_id": "gpt-test-model", "effort": "xhigh"}, "", ""))
    class Sink:
        def __init__(self): self.events = []; self.excluded = []
        def emit(self, event): self.events.append(event); return True
        def emit_excluded(self, event, reason): self.excluded.append((event, reason)); return True
        def status(self): return {"written": len(self.events), "dropped": 0}
    service = object.__new__(AgentService)
    service._public_stream_event_dir = state_dir / "public-stream-events"
    service._production_event_sink = Sink()
    service._wall_clock = lambda: datetime.now(timezone.utc)
    service.register_production_test_context(_context())
    service.begin_production_test()
    service.production_test_route_started()
    service.production_test_route_completed({"run": {"state": "ARRIVED", "timeline": []}})
    result = service.end_production_test()
    event = service._production_event_sink.events[0]
    assert result["accepted"] is True
    assert event["development"]["result"] == "unknown"
    assert event["development"]["model"] == "gpt-test-model"


@pytest.mark.parametrize("start,end,reason", [
    (_snapshot(1000), _snapshot(5000, paused=True), "recording_paused_at_boundary"),
    (_snapshot(5000), _snapshot(1000), "media_position_regressed"),
    (_snapshot(1000), _snapshot(5000, duration_rollback=True), "duration_rollback"),
    (_snapshot(1000), _snapshot(5000, path="C:/capture/other.mkv"), "recording_identity_or_path_changed"),
    (_snapshot(1000), _snapshot(5000, samples=[
        {"observedAt": _at(0.5), "durationMs": 1500, "paused": True, "recordingActive": True},
    ]), "recording_paused_within_interval"),
])
def test_interval_uncertainty_covers_pause_rollback_and_identity_changes(start, end, reason):
    interval = production_events.recording_interval(start, end)
    assert interval["uncertain"] is True
    assert reason in interval["uncertaintyReasons"]


def _event(event_id="event-a"):
    return {
        "schemaVersion": 1, "eventId": event_id, "occurredAt": "2026-10-13T12:00:00Z",
        "mode": "gameplay", "eventType": "queens_blood_notable_exchange",
        "recording": {"recordingId": "unbound", "sessionId": "unknown-session", "startedAt": "2026-10-13T12:00:00Z",
                      "observedAt": "2026-10-13T12:00:00Z", "positionMs": 0, "positionKnown": False,
                      "provenance": "external", "visualProof": False, "paused": False},
        "stream": {"state": "unknown", "observedAt": "2026-10-13T12:00:00Z", "source": "operator"},
        "gameplay": {"result": "unknown"},
    }


def test_sink_writes_idempotent_atomic_event_documents_and_reports_duplicate(tmp_path):
    event = _event()
    sink = production_events.ProductionEventSink(tmp_path)
    assert sink.emit(event)
    assert sink.emit(event)
    assert sink.flush(1)
    status = sink.status()
    sink.close()
    files = list((tmp_path / "production" / "events").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8")) == event
    assert status["written"] == 1
    assert status["duplicates"] == 1


def test_known_active_stream_exclusion_is_persisted_outside_intake(tmp_path):
    sink = production_events.ProductionEventSink(tmp_path)
    event = _event("excluded-live-event")
    assert sink.emit_excluded(event, "fresh_obs_stream_active")
    assert sink.flush(1)
    status = sink.status()
    sink.close()
    assert status["excluded"] == 1
    assert list((tmp_path / "production" / "events").glob("*.json")) == []
    excluded = list((tmp_path / "production" / "excluded-events").glob("*.json"))
    assert len(excluded) == 1
    document = json.loads(excluded[0].read_text(encoding="utf-8"))
    assert document["reason"] == "fresh_obs_stream_active"
    assert document["event"]["eventId"] == "excluded-live-event"


def test_sink_is_bounded_nonblocking_and_retries_write_errors(tmp_path):
    sink = production_events.ProductionEventSink(tmp_path, queue_size=1)
    entered, release = threading.Event(), threading.Event()
    original = sink._write_once

    def slow(event, exclusion_reason=None):
        entered.set()
        release.wait(1)
        original(event, exclusion_reason)

    sink._write_once = slow
    assert sink.emit(_event("one"))
    assert entered.wait(1)
    assert sink.emit(_event("two"))
    assert sink.emit(_event("three")) is False
    release.set()
    assert sink.flush(1)
    assert sink.status()["dropped"] == 1
    sink.close()

    obstructed = tmp_path / "blocked"
    obstructed.mkdir()
    (obstructed / "production").write_text("file blocks directory", encoding="utf-8")
    failed = production_events.ProductionEventSink(obstructed)
    assert failed.emit(_event("write-failure"))
    assert failed.flush(1)
    status = failed.status()
    failed.close()
    assert status["failed"] == 1
    assert status["retried"] == 2
    assert status["last_error"]


def test_harness_action_hook_emits_only_verified_finish_and_uncertain_placement(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from ff7r_agent.service import AgentService

    class Sink:
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    service = object.__new__(AgentService)
    service._one_off_visit_binding = None
    service._wall_clock = lambda: __import__("datetime").datetime(2026, 10, 13, tzinfo=__import__("datetime").timezone.utc)
    service._last_action = None
    service._queen_blood_match = {"opponent": "test-opponent", "story_id": "story-1"}
    service._queen_blood_campaign = None
    service._production_qb_event_ids = ["cue-event"]
    service._production_qb_event_counter = 1
    service._production_event_sink = Sink()
    service._public_stream_event_dir = tmp_path / "stream" / "public-stream-events"
    service._public_stream_last_emitted = {}
    service._qb_recorder = SimpleNamespace(capture=lambda *_args, **_kwargs: None,
                                           status=lambda: {"path": None})
    service._action_log = None
    service.input = SimpleNamespace(is_real=True)
    service._queen_blood_last_turn_payload = None
    service._queen_blood_commentary_baseline = None
    service._queen_blood_pre_move_payload = None
    service._queen_blood_commentary_after_pass = False
    service._emit_production_qb_event = lambda *args, **kwargs: AgentService._emit_production_qb_event(service, *args, **kwargs)

    # A claim with outer success but no verified score does not create a result event.
    service._record_action(action="finish_queens_blood_match", ok=True, outcome="result_unscored", score=None)
    assert service._production_event_sink.events == []
    service._record_action(action="finish_queens_blood_match", ok=True, outcome="match_finished",
                           score={"player": 12, "opponent": 8}, won=True, opponent="test-opponent")
    assert len(service._production_event_sink.events) == 1
    event = service._production_event_sink.events[0]
    assert event["eventType"] == "match.finish"
    assert event["storyId"] == "story-1"
    assert event["relatedEventIds"] == ["cue-event"]
    assert event["gameplay"]["result"] == "verified"
    assert event["gameplay"]["score"] == {"player": 12, "opponent": 8}
    service._record_action(action="place_queens_blood_card", ok=False,
                           outcome="placement_unverified", reason="fresh_observation_required")
    uncertain = service._production_event_sink.events[-1]
    assert uncertain["eventType"] == "input.qb.placement-uncertain"
    assert uncertain["gameplay"]["result"] == "unknown"


def test_notable_queen_blood_exchange_keeps_verified_facts_and_canonical_cue_label(tmp_path):
    from types import SimpleNamespace
    from ff7r_agent.service import AgentService

    class Sink:
        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    service = object.__new__(AgentService)
    service._queen_blood_match = {"opponent": "test-opponent", "story_id": "match-1"}
    service._production_qb_event_ids = []
    service._production_qb_event_counter = 0
    service._production_event_sink = Sink()
    service._public_stream_event_dir = tmp_path / "stream" / "public-stream-events"
    service._qb_recorder = SimpleNamespace(status=lambda: {"path": None})
    service.input = SimpleNamespace(is_real=True)
    service._emit_production_qb_event(
        "queens_blood_notable_exchange", object(),
        cues=[{"kind": "lane_swing", "lane": "middle", "swing": 7,
               "player_power": 9, "opponent_power": 2}],
        projection={"player_score": 12, "opponent_score": 8},
        projection_change=5,
    )
    event = service._production_event_sink.events[0]
    assert event["gameplay"]["result"] == "verified"
    assert event["gameplay"]["score"] == {
        "player": 12, "opponent": 8, "source": "verified_board_projection"
    }
    assert event["gameplay"]["projectionChange"] == 5
    cue = event["gameplay"]["notableCues"][0]
    assert cue["label"] == cue["kind"] == "lane_swing"
    assert cue["lane"] == 1
    assert cue["swing"] == 7
    assert cue["playerPower"] == 9
    assert cue["opponentPower"] == 2
