"""Supervisor concurrency tests; all process/save/stream data are offline fixtures."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from ff7r_agent import runtime_bridge as bridge
from ff7r_agent import public_stream_events as stream
from ff7r_agent import safety
from ff7r_agent.queen_blood_profile import QueenBloodProfile
from ff7r_agent.queen_blood_runs import QueenBloodRuns
from ff7r_agent.queen_blood_preparation import prepare_runtime_snapshot
from test_queen_blood_preparation import CARDS
from test_queen_blood_runs import DRIVER, PROCESS, Processes, save, loaded


@pytest.fixture
def context(tmp_path, monkeypatch):
    root = tmp_path / "queen-blood"
    store = QueenBloodRuns(root)
    file = save(tmp_path)
    assignment = store.assign(DRIVER, file)
    loaded(store, file)
    profile = QueenBloodProfile(root, assignment["profile_id"])
    profile.record_collection(CARDS, source="offline fixture")
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE_ROOT", str(root))
    monkeypatch.setenv("FF7R_PUBLIC_STREAM_EVENT_DIR", str(tmp_path / "events"))
    monkeypatch.setattr(stream, "match_record_target", lambda _: (DRIVER, "results", "qb-match-history"))
    monkeypatch.setattr(safety, "WindowsGameEnvironment", Processes)
    monkeypatch.setattr(bridge, "profile_contract_from_environment", lambda: None)
    monkeypatch.setattr(bridge, "collect_identity", lambda **kwargs: {})
    supervisor = bridge.RuntimeSupervisor()
    def forbidden(*args, **kwargs):
        pytest.fail("preparation attempted worker/probe/input startup or dispatch")
    for name in ("_request_locked", "_start_worker_locked", "_ensure_worker_locked", "_ensure_broker"):
        monkeypatch.setattr(supervisor, name, forbidden)
    arguments = {"cards": CARDS, "opponent": "Ned", "rationale": "Keep low-cost openers."}
    return supervisor, store, profile, arguments


@pytest.mark.parametrize("work", [None, bridge.ASYNC_ROUTE_WORK_OPERATION,
                                   bridge.ASYNC_TRAVEL_WORK_OPERATION, bridge.ASYNC_QUEENS_BLOOD_WORK_OPERATION])
def test_idle_and_running_safe_jobs_prepare_without_worker_or_input(context, work):
    supervisor, store, profile, arguments = context
    if work:
        supervisor._async_request = {"operation": work, "state": "running", "job_id": "held-input-job"}
    before = {path: path.read_bytes() for path in store.root.rglob("*.json")}
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: supervisor.call("prepare_queens_blood_deck", arguments), range(8)))
    assert all(value["ok"] and not value["result"]["input_sent"] for value in responses)
    assert all(value["result"]["profile_id"] == profile.profile_id for value in responses)
    assert {path: path.read_bytes() for path in store.root.rglob("*.json")} == before
    assert supervisor._worker is None and supervisor._broker is None
    if work:
        assert supervisor._async_request["state"] == "running"
        assert "prepare_queens_blood_deck" in supervisor._async_job_allowed_operations(supervisor._async_request)
        rejected = supervisor.call("place_queens_blood_card", {})
        assert not rejected["ok"] and rejected["job_id"] == "held-input-job"


@pytest.mark.parametrize("work", [bridge.ASYNC_LOAD_SLOT_WORK_OPERATION, bridge.ASYNC_SLOT_STARTUP_WORK_OPERATION,
    bridge.ASYNC_STARTUP_WORK_OPERATION, bridge.ASYNC_SCAN_WORK_OPERATION, bridge.ASYNC_DECK_WORK_OPERATION,
    bridge.ASYNC_MATCH_WORK_OPERATION, bridge.ASYNC_CLOSE_WORK_OPERATION, bridge.ASYNC_MULLIGAN_WORK_OPERATION,
    bridge.ASYNC_SHOP_BUY_WORK_OPERATION, bridge.ASYNC_CAPTURE_WORK_OPERATION, "unknown_job"])
@pytest.mark.parametrize("operation", ["prepare_queens_blood_deck", "get_queens_blood_preparation_context"])
def test_jobs_that_can_change_snapshot_or_world_refuse_preparation(context, work, operation):
    supervisor, _, _, arguments = context
    supervisor._async_request = {"operation": work, "state": "running", "job_id": "busy"}
    response = supervisor.call(operation, arguments if operation == "prepare_queens_blood_deck"
                               else {"opponent": arguments["opponent"]})
    assert not response["ok"] and response["job_id"] == "busy"


@pytest.mark.parametrize("change", ["load", "process", "driver", "profile", "unscanned", "assignment"])
@pytest.mark.parametrize("operation", ["prepare_queens_blood_deck", "get_queens_blood_preparation_context"])
def test_stale_or_wrong_save_model_profile_is_refused(context, monkeypatch, change, operation):
    supervisor, store, profile, arguments = context
    supervisor._async_request = {"operation": bridge.ASYNC_TRAVEL_WORK_OPERATION, "state": "running", "job_id": "busy"}
    if change == "load":
        store.invalidate("human load")
    elif change == "process":
        class Restarted(Processes):
            def game_process_identity_tokens(self, pids):
                return {42: "new-creation"}
        monkeypatch.setattr(safety, "WindowsGameEnvironment", Restarted)
    elif change == "driver":
        monkeypatch.setattr(stream, "match_record_target", lambda _: ({**DRIVER, "model_id": "another-model"}, "results", "qb-match-history"))
    elif change == "assignment":
        store.runs_path.unlink()
    else:
        data = profile.inventory()
        if change == "profile":
            data["profile_id"] = "another-profile"
        else:
            data.pop("scanned_at")
        profile.inventory_path.write_text(json.dumps(data))
    response = supervisor.call(operation, arguments if operation == "prepare_queens_blood_deck"
                               else {"opponent": arguments["opponent"]})
    assert not response["ok"] and not response["input_sent"]
    assert supervisor._async_request["state"] == "running"


@pytest.mark.parametrize("change", ["inventory", "reload", "mode"])
@pytest.mark.parametrize("operation", ["prepare_queens_blood_deck", "get_queens_blood_preparation_context"])
def test_snapshot_change_between_reads_refuses_candidate(context, monkeypatch, change, operation):
    supervisor, store, profile, arguments = context
    original = QueenBloodProfile.inventory
    calls = 0
    def inventory(self):
        nonlocal calls
        result = original(self)
        calls += 1
        if calls == 1:
            if change == "inventory":
                revised = {**result, "revision": result["revision"] + 1}
                self.inventory_path.write_text(json.dumps(revised))
            elif change == "reload":
                loaded(store, {"path": store.loaded(PROCESS)["path"], "slot": 13, "steam_id": "76561198000000000"})
            else:
                monkeypatch.setattr(stream, "match_record_target", lambda _: (DRIVER, "rehearsal", "qb-match-history-rehearsal"))
        return result
    monkeypatch.setattr(QueenBloodProfile, "inventory", inventory)
    response = supervisor.call(operation, arguments if operation == "prepare_queens_blood_deck"
                               else {"opponent": arguments["opponent"]})
    assert not response["ok"] and "changed" in response["error"]


@pytest.mark.parametrize("operation", ["prepare_queens_blood_deck", "get_queens_blood_preparation_context"])
def test_stale_supervisor_source_refuses_without_rebind(context, monkeypatch, operation):
    supervisor, _, _, arguments = context
    monkeypatch.setattr(bridge, "profile_contract_from_environment", lambda: {"deployment_profile": "FEATURE_VALIDATION"})
    monkeypatch.setattr(bridge, "_cached_checkout_validation", lambda _: {})
    monkeypatch.setattr(supervisor, "_source_binding_mismatches", lambda *args: [{"field": "git_commit_sha"}])
    with pytest.raises(bridge.RuntimeInfrastructureError):
        supervisor.call(operation, arguments if operation == "prepare_queens_blood_deck"
                        else {"opponent": arguments["opponent"]})


@pytest.mark.parametrize("start,status", [("initiate_queens_blood_match_async", "get_initiate_queens_blood_match_status"),
                                         ("travel_to_map_spot_async", "get_travel_to_map_spot_status")])
def test_real_async_response_thread_stays_blocked_while_supervisor_prepares(context, monkeypatch, start, status):
    from test_runtime_bridge import FakeAsyncWorker, BlockingStdout
    supervisor, _, _, arguments = context
    output = BlockingStdout('{"ok":true,"id":1,"result":{"ok":true}}\n')
    worker = FakeAsyncWorker(output)
    supervisor._worker = worker
    monkeypatch.setattr(supervisor, "_ensure_worker_locked", lambda: None)
    monkeypatch.setattr(supervisor, "_ensure_live_runtime_ready_locked", lambda operation: None)
    job = supervisor.call(start, {"target_id": "offline-target"})
    try:
        planning = supervisor.call("get_queens_blood_preparation_context", {"opponent": "Ned"})
        assert planning["ok"] and planning["result"]["opponent_info"]["name"] == "Ned"
        prepared = supervisor.call("prepare_queens_blood_deck", {
            **arguments, "expected_snapshot_sha256": planning["result"]["snapshot_sha256"]})
        assert prepared["ok"] and prepared["result"]["cards"] == CARDS
        assert len(worker.stdin.writes) == 1
        assert supervisor.call(status, {"job_id": job["job_id"]})["state"] == "running"
        assert not worker.terminated
    finally:
        output.release.set()


@pytest.mark.parametrize("operation", ["prepare_queens_blood_deck", "get_queens_blood_preparation_context"])
def test_parent_forwards_preparation_to_existing_child_without_rebinding(context, monkeypatch, operation):
    supervisor, _, _, arguments = context
    if operation == "get_queens_blood_preparation_context":
        arguments = {"opponent": arguments["opponent"]}
    expected = {"ok": True, "result": {"input_sent": False}}
    class Child:
        def call(self, operation, values):
            assert operation in bridge.QUEEN_BLOOD_PREPARATION_OPERATIONS and values == arguments
            return expected
    supervisor._bridge_process = Child()
    monkeypatch.setattr(supervisor, "_ensure_live_runtime_ready_locked", lambda _: pytest.fail("read-only preparation rebound child"))
    assert supervisor.call(operation, arguments) == expected


@pytest.mark.parametrize("work", [None, bridge.ASYNC_ROUTE_WORK_OPERATION,
                                   bridge.ASYNC_TRAVEL_WORK_OPERATION, bridge.ASYNC_QUEENS_BLOOD_WORK_OPERATION])
def test_context_reads_cached_tactics_without_input_or_file_writes(context, work):
    supervisor, store, profile, _ = context
    profile.record_deck(CARDS, source="offline verified fixture")
    profile.update_opponent("Ned", card_ids=["card_id_000"], match={"won": False, "score": {"player": 1}})
    if work:
        supervisor._async_request = {"operation": work, "state": "running", "job_id": "busy"}
    before = {path: path.read_bytes() for path in store.root.rglob("*.json")}
    response = supervisor.call("get_queens_blood_preparation_context", {"opponent": "Ned"})
    assert response["ok"]
    planning = response["result"]
    assert planning["known_deck"]["cards"] == CARDS
    assert planning["opponent_info"]["history"]["card_ids"] == ["card_id_000"]
    assert planning["owned_cards"] and not planning["input_sent"]
    assert supervisor._worker is None and supervisor._broker is None
    assert {path: path.read_bytes() for path in store.root.rglob("*.json")} == before
    if work:
        assert supervisor._async_request["state"] == "running"
        assert "get_queens_blood_preparation_context" in supervisor._async_job_allowed_operations(supervisor._async_request)
        assert not supervisor.call("read_queens_blood_cards", {})["ok"]


@pytest.mark.parametrize("start,status", [
    ("initiate_queens_blood_match_async", "get_initiate_queens_blood_match_status"),
    ("travel_to_map_spot_async", "get_travel_to_map_spot_status"),
])
def test_preparation_and_speech_share_a_running_job_without_input_interference(context, monkeypatch, start, status):
    from datetime import datetime, timezone
    from ff7r_agent.gameplay_narration import AgentSpeechPublisher
    from test_runtime_bridge import FakeAsyncWorker, BlockingStdout

    supervisor, store, profile, arguments = context
    events = store.root.parent / "events"
    (events.parent / "public-stream-rehearsal.json").write_text(json.dumps({
        "version": 1, "at": datetime.now(timezone.utc).isoformat()}))
    now = [0.0]
    supervisor._speech = AgentSpeechPublisher(events, monotonic=lambda: now[0])
    output = BlockingStdout('{"ok":true,"id":1,"result":{"ok":true}}\n')
    worker = FakeAsyncWorker(output)
    supervisor._worker = worker
    monkeypatch.setattr(supervisor, "_ensure_worker_locked", lambda: None)
    monkeypatch.setattr(supervisor, "_ensure_live_runtime_ready_locked", lambda operation: None)
    job = supervisor.call(start, {"target_id": "offline-target"})
    try:
        before = {path: path.read_bytes() for path in store.root.rglob("*.json")}
        prepared = supervisor.call("prepare_queens_blood_deck", arguments)
        spoken = supervisor.call("say_on_stream", {"text": "That top lane needs defending. I'll keep these low-cost cards.", "tone": "concerned"})
        assert prepared["ok"] and prepared["result"]["cards"] == CARDS
        assert spoken["ok"] and spoken["result"]["ok"]
        assert spoken["result"]["rehearsal"] and not spoken["result"]["input_sent"]
        # Preparation must not reset or consume the independent shared speech gate.
        assert supervisor.call("prepare_queens_blood_deck", arguments)["ok"]
        limited = supervisor.call("say_on_stream", {"text": "The middle lane still gives us room to build."})
        assert limited["ok"] and limited["result"]["reason"] == "rate_limited"
        assert limited["result"]["retry_after_seconds"] == 8
        observed = supervisor.call(status, {"job_id": job["job_id"]})
        assert observed["state"] == "running" and observed["job_id"] == job["job_id"]
        assert len(worker.stdin.writes) == 1 and not worker.terminated
        assert supervisor._broker is None
        assert {path: path.read_bytes() for path in store.root.rglob("*.json")} == before
        assert len(list(events.glob("*.json"))) == 1
    finally:
        output.release.set()
