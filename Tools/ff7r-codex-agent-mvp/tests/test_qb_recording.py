from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Event
import time

import pytest

from ff7r_agent.input import SimulationInput, WindowsKeyboardInput, WindowsHybridGamepadInput
from ff7r_agent.qb_recording import QueenBloodRecorder
from ff7r_agent.qb_replay import ReplayClock, ReplayError, ReplayTelemetry, create_replay_service, load_recording, replay_operation
from ff7r_agent.runtime_commands import invoke_runtime
from ff7r_agent.service import AgentService
from ff7r_agent.telemetry import JsonFileTelemetry, TelemetryError


def payload(sequence=1):
    return {"schema_version": 1, "sequence": sequence, "timestamp_utc": "2026-10-07T12:00:00Z",
            "game": {"running": True, "location": "Kalm", "in_menu": False}, "player": None}


def complete(recorder):
    result = recorder.stop()
    assert not result["finalizing"]
    return load_recording(Path(result["path"]), allow_partial=True)


def test_disabled_has_no_thread_files_copy_or_clock_calls(tmp_path):
    def forbidden():
        raise AssertionError("disabled recorder touched clock")
    recorder = QueenBloodRecorder(tmp_path / "unused", monotonic=forbidden, wall_clock=forbidden)
    assert not recorder.capture("input", {"non_json": object()})
    assert recorder._thread is None and not recorder.root.exists()


@pytest.mark.parametrize("values", [{"run_id": "../escape"}, {"duration_seconds": 1801},
                                   {"duration_seconds": 0}, {"duration_seconds": -1},
                                   {"duration_seconds": float("nan")}, {"duration_seconds": float("inf")},
                                   {"max_bytes": 4095}, {"sample_hz": 21}, {"sample_hz": float("nan")}])
def test_limits_validated_before_writes(tmp_path, values):
    recorder = QueenBloodRecorder(tmp_path / "unused")
    with pytest.raises(ValueError):
        recorder.start(**{"run_id": "test", **values})
    assert not recorder.root.exists()


def test_git_checkout_and_linked_worktree_are_rejected(tmp_path):
    (tmp_path / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    with pytest.raises(ValueError, match="outside every Git"):
        QueenBloodRecorder(tmp_path / "ignored" / "recordings").start("test")


def test_capture_snapshots_mutable_values_and_orders_jsonl(tmp_path):
    recorder = QueenBloodRecorder(tmp_path)
    recorder.start("run", metadata={"driver": "codex", "slot": 13})
    data = {"hand": [{"card_id": "original"}]}
    assert recorder.capture("action", data)
    data["hand"][0]["card_id"] = "changed"
    events = complete(recorder)
    assert events[0]["run_id"] == "run"
    assert events[1]["data"]["hand"][0]["card_id"] == "original"
    assert events[-1]["data"]["dropped_events"] == 0
    assert all(event["timestamp_utc"] for event in events)
    assert load_recording(Path(recorder.status()["path"])) == events


def test_backpressure_never_waits_for_blocked_writer_and_memory_is_bounded(tmp_path, monkeypatch):
    recorder = QueenBloodRecorder(tmp_path, queue_events=2, queue_bytes=1024)
    gate = Event()
    original = recorder._open
    monkeypatch.setattr(recorder, "_open", lambda: (gate.wait(2), original())[1])
    recorder.start("run")
    started = time.perf_counter()
    for index in range(100):
        recorder.capture("input", {"index": index})
    assert time.perf_counter() - started < 0.2
    assert recorder.status()["queued_events"] <= 2
    assert recorder.status()["queued_bytes"] <= 1024
    assert recorder.status()["dropped_events"] >= 98
    gate.set()
    complete(recorder)
    with pytest.raises(ReplayError, match="incomplete"):
        load_recording(Path(recorder.status()["path"]))


def test_large_and_non_json_events_drop_without_failing_input(tmp_path):
    recorder = QueenBloodRecorder(tmp_path, event_bytes=512)
    recorder.start("run")
    assert not recorder.capture("huge", {"text": "x" * 1024})
    assert not recorder.capture("invalid", {"object": object()})
    assert recorder.capture("small", {"button": "confirm"})
    complete(recorder)
    assert recorder.status()["dropped_events"] == 2
    assert recorder.status()["drop_reasons"] == {"event byte limit": 1, "non-JSON event": 1}


def test_byte_limit_includes_header_and_footer(tmp_path):
    recorder = QueenBloodRecorder(tmp_path)
    recorder.start("run", max_bytes=4096)
    for i in range(10):
        recorder.capture("input", {"i": i, "value": "x" * 1000})
    events = complete(recorder)
    assert Path(recorder.status()["path"]).stat().st_size <= 4096
    assert events[-1]["kind"] == "summary"
    assert recorder.status()["reason"] == "byte_limit"
    assert recorder.status()["dropped_events"] > 0


@pytest.mark.parametrize("duration_seconds", [1, 600, 601])
def test_deadline_and_sampling_apply_only_to_telemetry(tmp_path, duration_seconds):
    clock = ReplayClock("2026-10-07T12:00:00Z")
    recorder = QueenBloodRecorder(tmp_path, monotonic=clock.monotonic, wall_clock=clock.now)
    recorder.start("run", duration_seconds=duration_seconds, sample_hz=10)
    assert recorder.capture("telemetry", {"payload": payload()}, sample_key="consumer")
    assert not recorder.capture("telemetry", {"payload": payload()}, sample_key="consumer")
    assert recorder.capture("input", {"button": "confirm"})
    clock.sleep(duration_seconds + 0.1)
    assert not recorder.capture("input", {})
    complete(recorder)
    assert recorder.status()["reason"] == "duration_limit"
    assert recorder.status()["sampled_out"] == 1


@pytest.mark.parametrize("start_via", ["recorder", "service", "runtime"])
@pytest.mark.parametrize("duration_seconds", [None, 1800])
def test_full_match_tail_survives_old_ten_minute_limit(tmp_path, monkeypatch, start_via, duration_seconds):
    clock = ReplayClock("2026-10-08T12:00:00Z")
    recorder = QueenBloodRecorder(tmp_path / "records", monotonic=clock.monotonic, wall_clock=clock.now)
    service = AgentService(JsonFileTelemetry(tmp_path / "state.json"), SimulationInput(),
                           queen_blood_recording_root=tmp_path / "records")
    service._qb_recorder = recorder
    # Hold disk work until the synthetic match is queued, avoiding contention
    # while jumping the fake clock. No game backend or real-time wait is needed.
    gate = Event()
    original = recorder._open
    monkeypatch.setattr(recorder, "_open", lambda: (gate.wait(2), original())[1])
    arguments = {"run_id": "twelve-minute-match"}
    if duration_seconds is not None:
        arguments["duration_seconds"] = duration_seconds
    try:
        if start_via == "recorder":
            status = recorder.start(**arguments)
        elif start_via == "service":
            status = service.start_queens_blood_recording(**arguments)
        else:
            status = invoke_runtime(service, None, None, "start_queens_blood_recording", arguments)
        assert status["limits"]["duration_seconds"] == 1800
        assert recorder.capture("action", {"action": "initiate_queens_blood_match"})
        clock.sleep(601)
        assert recorder.capture("input", {"button": "confirm"})
        clock.sleep(119)
        assert recorder.capture("action", {"action": "finish_queens_blood_match", "outcome": "won"})
        assert recorder.status()["enabled"]
    finally:
        gate.set()
        stopped = recorder.stop()
    assert not stopped["finalizing"] and stopped["reason"] == "stopped"
    events = load_recording(Path(stopped["path"]))
    assert events[0]["limits"]["duration_seconds"] == 1800
    tail = events[-2]
    assert tail["offset_seconds"] == 720
    assert tail["data"] == {"action": "finish_queens_blood_match", "outcome": "won"}
    assert events[-1]["data"]["evidence_complete"]
    assert events[-1]["data"]["written_events"] == 3 + int(start_via == "runtime")


@pytest.mark.parametrize("expires_via", ["capture", "writer"])
def test_thirty_minute_deadline_still_closes_recording(tmp_path, monkeypatch, expires_via):
    clock = ReplayClock("2026-10-08T12:00:00Z")
    recorder = QueenBloodRecorder(tmp_path, monotonic=clock.monotonic, wall_clock=clock.now)
    gate = Event()
    original = recorder._open
    monkeypatch.setattr(recorder, "_open", lambda: (gate.wait(2), original())[1])
    try:
        recorder.start("bounded", duration_seconds=1800)
        clock.sleep(1799.999)
        assert recorder.capture("input", {"button": "confirm"})
        clock.sleep(0.001)
        if expires_via == "capture":
            assert not recorder.capture("input", {"button": "late"})
        gate.set()
        recorder._thread.join(timeout=2)
        status = recorder.status()
        assert not status["enabled"] and not status["finalizing"]
        assert status["reason"] == "duration_limit"
        events = load_recording(Path(status["path"]))
        assert events[-1]["offset_seconds"] == 1800
        assert events[-1]["data"]["written_events"] == 1
        assert events[-1]["data"]["reason"] == "duration_limit"
    finally:
        gate.set()
        recorder.stop()


def test_writer_failure_disables_diagnostics_without_gameplay_exception(tmp_path, monkeypatch):
    recorder = QueenBloodRecorder(tmp_path)
    def fail():
        raise OSError("disk full")
    monkeypatch.setattr(recorder, "_open", fail)
    recorder.start("run")
    recorder.stop()
    assert recorder.status()["error"] == "disk full"
    assert recorder.status()["reason"] == "writer_error"
    assert not recorder.capture("input", {})


@pytest.mark.parametrize("backend", [WindowsKeyboardInput, WindowsHybridGamepadInput])
def test_actual_input_event_sinks_capture_without_instantiating_windows_devices(tmp_path, backend):
    recorder = QueenBloodRecorder(tmp_path)
    recorder.start("run")
    # Exercise the existing sink, never a native constructor or SendInput.
    instance = object.__new__(backend)
    instance.qb_recorder, instance._event_records, instance._event_log = recorder, [], None
    instance._write_event_record({"event": "key", "button": "confirm", "key_down_ok": False})
    assert complete(recorder)[1]["data"]["key_down_ok"] is False


def test_telemetry_actions_inputs_and_errors_share_one_recording(tmp_path):
    state_path = tmp_path / "feed" / "state.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps(payload()), encoding="utf-8")
    service = AgentService(JsonFileTelemetry(state_path), SimulationInput(),
                           queen_blood_recording_root=tmp_path / "records")
    assert not service.get_queens_blood_recording_status()["enabled"]
    invoke_runtime(service, None, None, "start_queens_blood_recording", {"run_id": "offline"})
    invoke_runtime(service, None, None, "observe_game")
    service.input.tap("confirm", 80)
    service._record_action(action="test", verified=False)
    with pytest.raises(ValueError):
        invoke_runtime(service, None, None, "unsupported")
    state_path.unlink()
    with pytest.raises(TelemetryError):
        service.telemetry.observe()
    events = complete(service._qb_recorder)
    assert {"sidecar", "telemetry", "input", "action", "operation_error", "telemetry_error"} <= {e["kind"] for e in events}
    fake = create_replay_service(Path(service._qb_recorder.status()["path"]), output_root=tmp_path / "replay", mode="reads")
    event = next(e for e in events if e["kind"] == "operation_start" and e["data"]["operation"] == "observe_game")
    assert replay_operation(fake, event)["location"] == "Kalm"
    with pytest.raises(TelemetryError, match="file not found"):
        fake.telemetry.observe()
    assert fake._public_stream_event_dir is None and fake._action_log is None
    with pytest.raises(ReplayError, match="outside offline"):
        replay_operation(fake, {**event, "data": {"operation": "say_on_stream"}})
    with pytest.raises(ReplayError, match="fully fake"):
        replay_operation(service, event)


def test_replay_preserves_staleness_and_has_finite_eof():
    clock = ReplayClock("2026-10-07T12:00:05Z")
    telemetry = ReplayTelemetry([{"kind": "telemetry", "offset_seconds": 0,
                                  "data": {"payload": payload()}}], clock, mode="reads")
    assert telemetry.observe().age_seconds() == 5
    clock.sleep(3)
    assert clock.now() == datetime(2026, 10, 7, 12, 0, 8, tzinfo=timezone.utc)
    with pytest.raises(ReplayError, match="exhausted"):
        telemetry.observe()


def test_rejected_raw_qb_sidecar_is_preserved_but_not_promoted_in_replay(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(payload()), encoding="utf-8")
    raw = json.dumps({"schema_version": 1, "base_sequence": 999,
                      "source": "unknown", "queen_blood": {"active": True, "phase": "player_turn"}})
    state_path.with_name("queen_blood.json").write_text(raw, encoding="utf-8")
    service = AgentService(JsonFileTelemetry(state_path), SimulationInput(),
                           queen_blood_recording_root=tmp_path / "records")
    service.start_queens_blood_recording("unbound-sidecar")
    assert service.telemetry.observe().queen_blood is None
    events = complete(service._qb_recorder)
    assert next(e["data"]["text"] for e in events if e["kind"] == "sidecar"
                and e["data"]["name"] == "queen_blood.json") == raw
    fake = create_replay_service(Path(service._qb_recorder.status()["path"]),
                                 output_root=tmp_path / "fake", mode="reads")
    assert fake.telemetry.observe().queen_blood is None


def test_loader_rejects_corrupt_crashed_or_reordered_evidence(tmp_path):
    path = tmp_path / "events.jsonl"
    header = {"schema_version": 1, "kind": "header", "timestamp_utc": "2026-10-07T12:00:00Z"}
    path.write_text(json.dumps(header) + '\n{"unfinished":', encoding="utf-8")
    with pytest.raises(ReplayError):
        load_recording(path)
    assert load_recording(path, allow_partial=True) == [header]
    event = {"schema_version": 1, "kind": "input", "event_id": 2}
    path.write_text('\n'.join(json.dumps(v) for v in [header, event, event]), encoding="utf-8")
    with pytest.raises(ReplayError, match="out of order"):
        load_recording(path, allow_partial=True)


@pytest.mark.parametrize("kind", ["action", "operation_result"])
def test_oversized_trace_retains_complete_final_facts_without_hiding_raw_loss(tmp_path, kind):
    recorder = QueenBloodRecorder(tmp_path)
    recorder.start("large-trace")
    qb = {"phase": "deck_select", "deck_screen": {"cards": [
        {"slot": n, "card_id": f"card-{n}", "description": "x" * 1200} for n in range(15)]}}
    response = {"ok": True, "action": "initiate_queens_blood_match", "outcome": "mulligan_ready",
                "pre_sequence": 100, "post_sequence": 160, "match_screen_sequence": 155,
                "queen_blood": qb, "inputs": [{"button": "confirm", "ok": True}],
                "trace": [{"sequence": n, "queen_blood": qb} for n in range(100, 160)]}
    data = {"operation": "run_initiate_queens_blood_match", "response": response} if kind == "operation_result" else response
    assert not recorder.capture(kind, data)
    events = complete(recorder)
    summary = next(e["data"] for e in events if e["kind"] == kind + "_summary")
    assert summary["queen_blood"] == qb and summary["inputs"] == response["inputs"]
    assert summary["trace_first"] == response["trace"][0] and summary["trace_last"] == response["trace"][-1]
    assert summary["pre_sequence"] == 100 and summary["post_sequence"] == 160
    assert summary["outcome"] == "mulligan_ready" and summary["trace_entries"] == 60
    assert summary["trace_omitted"] and summary["raw_event_dropped"]
    assert recorder.status()["drop_reasons"] == {"event byte limit": 1}
    assert recorder.status()["dropped_events"] == 1 and not recorder.status()["evidence_complete"]
    assert events[-1]["data"]["drop_reasons"] == {"event byte limit": 1}
    with pytest.raises(ReplayError, match="incomplete"):
        load_recording(Path(recorder.status()["path"]))


def test_oversized_summary_itself_stays_bounded_and_reports_both_drops(tmp_path):
    recorder = QueenBloodRecorder(tmp_path, event_bytes=512)
    recorder.start("bounded-summary")
    assert not recorder.capture("action", {"trace": [{}], "queen_blood": {"raw": "x" * 10000}})
    events = complete(recorder)
    assert recorder.status()["dropped_events"] == 2
    assert recorder.status()["drop_reasons"] == {"event byte limit": 2}
    assert not any(e["kind"] == "action_summary" for e in events)


def test_live_recording_header_retains_existing_verified_save_process_receipt(tmp_path, monkeypatch):
    from ff7r_agent import service as service_module
    service = AgentService(JsonFileTelemetry(tmp_path / "state.json"), SimulationInput(),
                           queen_blood_recording_root=tmp_path / "records")
    service.input.is_real = True
    service._public_stream_event_dir = tmp_path / "stream" / "public-stream-events"
    driver = {"driver": "ChatGPT", "model_id": "reported-model", "effort": "reported-effort"}
    assignment = {"slot": 15, "profile_id": "profile", "steam_id": "account"}
    loaded = {"slot": 15, "path": "owned-save", "steam_id": "account", "process": {"pid": 42, "creation_token": "token"}}
    monkeypatch.setattr(service_module, "match_record_target", lambda directory: (driver, None, "history"))
    service._queen_blood_runs = type("Runs", (), {"assignment": lambda self, d: assignment})()
    monkeypatch.setattr(service, "get_queens_blood_run_status", lambda: {
        "run_loaded": True, "loaded_save_verified": True, "profile_id": "profile", "loaded_save": loaded})
    service.start_queens_blood_recording("bound-header")
    metadata = complete(service._qb_recorder)[0]["metadata"]
    assert metadata["loaded_save"] == loaded and metadata["loaded_save_verified"] and metadata["run_loaded"]
    assert metadata["driver"] == driver and metadata["assignment"] == assignment


def test_node_limit_has_distinct_drop_reason(tmp_path):
    recorder = QueenBloodRecorder(tmp_path)
    recorder.start("node-limit")
    assert not recorder.capture("many-nodes", {"nodes": [None] * 64001})
    complete(recorder)
    assert recorder.status()["drop_reasons"] == {"event node limit": 1}
    assert recorder.status()["dropped_events"] == 1


def test_snapshot_wire_budget_matches_json_bytes():
    from ff7r_agent.qb_recording import _snapshot
    import pytest
    values = [None, True, False, -123456, -0.0, 1.25e-30, 'quote" slash\\ newline\n tab\t control\x01 caf\u00e9 \u6f22\u5b57 \U0001f600', {'key\n': [None, True, False, 42, -0.0, '\u00e9\U0001f600']}, [], {}]
    for value in values:
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        copied, _ = _snapshot(value, 1024 * 1024, json_budget=size)
        assert copied == value
        with pytest.raises(ValueError, match="event byte limit"):
            _snapshot(value, 1024 * 1024, json_budget=size - 1)


@pytest.mark.parametrize("kind", ["action", "operation_result"])
def test_dense_mulligan_preserves_full_semantics_under_wire_limit(tmp_path, kind):
    from ff7r_agent.qb_recording import _snapshot
    qb = {"phase": "player_turn", "hand": [{"property_source_" + str(n): "UE4SS.CardGame.CardFull.NormalizedIdentity" for n in range(80)} for _ in range(5)]}
    result = {"ok": True, "action": "mulligan_cards", "outcome": "turn_one_hand_verified", "selected_indices": [], "pre_hand": qb["hand"], "queen_blood": qb, "inputs": [{"action": "queen_blood_play"}], "trace": [{"sequence": n, "queen_blood": qb} for n in range(10)]}
    data = result if kind == "action" else {"operation": "mulligan_cards", "response": {"ok": True, "id": 90, "result": result}}
    assert len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) < 1024 * 1024 - 256
    assert _snapshot(data, 8 * 1024 * 1024)[1] > 1024 * 1024
    recorder = QueenBloodRecorder(tmp_path)
    recorder.start("dense")
    assert recorder.capture(kind, data)
    events = complete(recorder)
    assert next(e["data"] for e in events if e["kind"] == kind) == data
    assert events[-1]["data"]["dropped_events"] == 0
    assert events[-1]["data"]["evidence_complete"] is True
    assert not any(e["kind"].endswith("_summary") for e in events)


def test_copy_memory_bound_remains_independent_of_wire_limit(tmp_path):
    recorder = QueenBloodRecorder(tmp_path, queue_bytes=512)
    recorder.start("memory")
    assert not recorder.capture("sidecar", {"text": "x" * 100})
    events = complete(recorder)
    assert events[-1]["data"]["drop_reasons"]["event memory limit"] == 1
    assert events[-1]["data"]["evidence_complete"] is False


def test_writer_checks_entire_event_envelope(tmp_path):
    recorder = QueenBloodRecorder(tmp_path, event_bytes=512)
    recorder.start("envelope")
    assert recorder.capture("x" * 1000, {"ok": True})
    events = complete(recorder)
    assert len(events) == 2
    assert events[-1]["data"]["drop_reasons"]["event byte limit"] == 1
    assert events[-1]["data"]["evidence_complete"] is False


def test_begin_dense_trace_retains_full_raw_under_all_limits(tmp_path):
    from ff7r_agent.qb_recording import _snapshot
    data = {"action": "initiate_queens_blood_match", "trace": [{"queen_blood": {"nodes": [{"id": n, "name": "CardGame.Widget.Identity"} for n in range(175)]}} for _ in range(59)]}
    assert len(json.dumps(data, separators=(",", ":")).encode()) < 1024 * 1024 - 256
    assert _snapshot(data, 8 * 1024 * 1024)[1] < 8 * 1024 * 1024
    recorder = QueenBloodRecorder(tmp_path)
    recorder.start("begin")
    assert recorder.capture("action", data)
    events = complete(recorder)
    assert events[1]["data"] == data
    assert events[-1]["data"]["evidence_complete"] is True


def test_explicit_node_cap_remains_honest(tmp_path):
    recorder = QueenBloodRecorder(tmp_path)
    recorder.start("node-limit")
    assert not recorder.capture("sidecar", {"nodes": [None] * 64000})
    events = complete(recorder)
    assert events[-1]["data"]["drop_reasons"]["event node limit"] == 1
    assert events[-1]["data"]["evidence_complete"] is False


@pytest.mark.parametrize("kind", ["action", "operation_result"])
def test_long_match_start_boundary_keeps_every_trace_frame(tmp_path, kind):
    from ff7r_agent.qb_recording import MAX_BOUNDARY_EVENT_NODES, _snapshot
    # Synthetic dense initiation trace, fixed frame order; no live timing.
    result = {"action": "initiate_queens_blood_match", "outcome": "mulligan_ready",
              "trace": [{"sequence": frame, "queen_blood": {"nodes": [
                  {"id": n, "name": "CardGame.Widget.Identity"} for n in range(175)]}}
                        for frame in range(90)]}
    data = result if kind == "action" else {
        "operation": "run_initiate_queens_blood_match", "response": {"ok": True, "result": result}}
    with pytest.raises(ValueError, match="event node limit"):
        _snapshot(data, 8 * 1024 * 1024)
    assert _snapshot(data, 8 * 1024 * 1024, json_budget=1024 * 1024 - 256,
                     node_limit=MAX_BOUNDARY_EVENT_NODES)[0] == data
    recorder = QueenBloodRecorder(tmp_path)
    recorder.start("long-boundary")
    assert recorder.capture(kind, data)
    events = complete(recorder)
    assert events[1]["data"] == data
    assert events[0]["limits"]["event_nodes"] == 64000
    assert events[0]["limits"]["boundary_event_nodes"] == 128000
    assert events[-1]["data"]["dropped_events"] == 0
    assert load_recording(Path(recorder.status()["path"])) == events


@pytest.mark.parametrize("kind,data", [
    ("action", {"nodes": [None] * 64000}),
    ("operation_result", {"response": {"result": {"nodes": [None] * 64000}}}),
    ("telemetry", {"trace": [None] * 64000}),
], ids=["action-without-trace", "result-without-trace", "periodic-with-trace"])
def test_extra_boundary_nodes_do_not_expand_other_events(tmp_path, kind, data):
    recorder = QueenBloodRecorder(tmp_path)
    recorder.start("ordinary-node-limit")
    assert not recorder.capture(kind, data)
    events = complete(recorder)
    assert events[-1]["data"]["drop_reasons"] == {"event node limit": 1}
    assert events[-1]["data"]["evidence_complete"] is False


def test_boundary_node_cap_still_reports_raw_loss(tmp_path):
    recorder = QueenBloodRecorder(tmp_path)
    recorder.start("boundary-node-limit")
    assert not recorder.capture("action", {"trace": [None] * 128000})
    events = complete(recorder)
    assert events[-1]["data"]["drop_reasons"] == {"event node limit": 1}
    assert events[-1]["data"]["evidence_complete"] is False
    summary = next(event["data"] for event in events if event["kind"] == "action_summary")
    assert summary["raw_event_dropped"] and summary["trace_entries"] == 128000


@pytest.mark.parametrize("limits,reason", [
    ({"queue_bytes": 512}, "event memory limit"),
    ({"event_bytes": 512}, "event byte limit"),
])
def test_boundary_retains_memory_and_wire_limits(tmp_path, limits, reason):
    recorder = QueenBloodRecorder(tmp_path, **limits)
    recorder.start("boundary-byte-limit")
    assert not recorder.capture("action", {"trace": [{"text": "x" * 1000}]})
    events = complete(recorder)
    assert events[-1]["data"]["drop_reasons"][reason] >= 1
    assert events[-1]["data"]["evidence_complete"] is False
