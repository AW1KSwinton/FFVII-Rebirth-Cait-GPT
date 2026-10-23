"""Combined narration/campaign tests use only fake processes and temporary files."""
import json
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from ff7r_agent import gameplay_narration as narration
from ff7r_agent import public_stream_events as events
from ff7r_agent import runtime_bridge as bridge
from ff7r_agent import queen_blood_speech_receipt as receipts
from ff7r_agent.service import AgentService
from test_campaign_director import NoGame, Processes, DRIVER, NED
from test_queen_blood_runs import save, PROCESS, loaded


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    import ff7r_agent.service as service_module
    declaration = [DRIVER, "public-qb-results", "qb-match-history"]
    monkeypatch.setattr(events, "match_record_target", lambda _: tuple(declaration))
    monkeypatch.setattr(service_module, "match_record_target", lambda _: tuple(declaration))
    monkeypatch.setattr(service_module, "stream_is_live", lambda _: declaration[2] == "qb-match-history")
    monkeypatch.setattr(service_module, "rehearsal_is_live", lambda _: declaration[2] == "qb-match-history-rehearsal")
    monkeypatch.setattr(narration, "stream_is_live", lambda _: declaration[2] == "qb-match-history")
    monkeypatch.setattr(narration, "rehearsal_is_live", lambda _: declaration[2] == "qb-match-history-rehearsal")
    monkeypatch.setattr(bridge, "collect_identity", lambda **kwargs: {})
    clock = [time.time()]
    directory = tmp_path / "events"
    root = tmp_path / "queen-blood"
    service = AgentService(NoGame(), NoGame(), environment=Processes(), queen_blood_profile_root=root,
        public_stream_event_dir=directory, queen_blood_recording_root=tmp_path / "records")
    service._queen_blood_runs.assign(DRIVER, save(tmp_path))
    loaded(service._queen_blood_runs, save(tmp_path))
    service.get_campaign_next_step()
    def context():
        return receipts.binding(service.get_queens_blood_run_status(),
            "live" if declaration[2] == "qb-match-history" else "rehearsal")
    supervisor = bridge.RuntimeSupervisor()
    supervisor._speech = narration.AgentSpeechPublisher(directory, monotonic=lambda: clock[0], receipt_context=context)
    service._queen_blood_match = {"opponent": NED}
    return service, supervisor, context, clock, declaration


def say(supervisor):
    result = supervisor.call("say_on_stream", {"text": "I can keep the middle open and build along the top.", "tone": "calm"})
    assert result["ok"] and result["result"]["ok"]
    return result["result"]


def move(service):
    try:
        service._claim_campaign_speech_before_input()
    except Exception as exc:
        from ff7r_agent.service import SafetyError
        assert isinstance(exc, SafetyError)
    service._record_action(action="place_queens_blood_card", ok=True, hand_index=0, row=0, column=0)
    return service.get_campaign_status()["last_action"]


@pytest.mark.parametrize("mode", ["live", "rehearsal"])
def test_supervisor_speech_binds_once_to_authoritative_move_and_opt_in_diagnostic(fixture, mode):
    service, supervisor, context, clock, declaration = fixture
    if mode == "rehearsal":
        declaration[1:] = ["public-qb-results-rehearsal", "qb-match-history-rehearsal"]
        (service._public_stream_event_dir.parent / "speech-consumer-rehearsal.json").write_text(json.dumps({
            "version": 1, "mode": "rehearsal", "at": events.datetime.now(events.timezone.utc).isoformat(),
            "available": True, "busy": False, "speech_ready": True,
            "consecutive_failures": 0, "failure_limit": 2,
        }))
    service.start_queens_blood_recording("speech-fixture")
    supervisor._async_request = {"operation": "run_initiate_queens_blood_match", "state": "running", "job_id": "busy"}
    response = say(supervisor)
    receipt = move(service)
    assert receipt["speech"]["receipt_id"] == response["receipt_id"]
    assert receipt["speech"]["response"]["ok"] and receipt["mode"] == mode
    assert receipt["speech"]["context"] == context()
    assert supervisor._async_request["state"] == "running"
    assert move(service)["speech"] is None  # no receipt reuse for a second input
    service.stop_queens_blood_recording()
    for _ in range(100):
        if not service._qb_recorder.status()["finalizing"]:
            break
        time.sleep(.01)
    lines = [json.loads(line) for line in receipts.Path(service._qb_recorder.status()["path"]).read_text().splitlines()]
    assert next(line for line in lines if line["kind"] == "speech_receipt")["data"]["speech"]["receipt_id"] == response["receipt_id"]
    assert service.get_campaign_status()["progress"] == 0


@pytest.mark.parametrize("change", ["model", "load", "process", "run", "mode", "age", "future", "failed"])
def test_stale_wrong_or_failed_receipt_cannot_supply_action_speech(fixture, change):
    service, supervisor, context, clock, declaration = fixture
    say(supervisor)
    path = receipts.receipt_path(service._public_stream_event_dir)
    value = json.loads(path.read_text())
    target = value["receipt"]
    if change == "model":
        target["context"]["driver"]["model_id"] = "wrong"
    elif change == "load":
        target["context"]["loaded_save"]["load_id"] = "wrong"
    elif change == "process":
        target["context"]["loaded_save"]["process"]["token"] = "wrong"
    elif change == "run":
        target["context"]["driver"]["run_id"] = "wrong"
    elif change == "mode":
        target["context"]["mode"] = "rehearsal"
    elif change == "age":
        target["created_epoch"] -= 100
    elif change == "future":
        target["created_epoch"] += 100
    else:
        target["response"]["ok"] = False
    path.write_text(json.dumps(value))
    assert move(service)["speech"] is None
    assert json.loads(path.read_text())["receipt"] is None


def test_rate_limited_attempt_revokes_previous_unconsumed_receipt(fixture):
    service, supervisor, context, clock, declaration = fixture
    say(supervisor)
    refused = supervisor.call("say_on_stream", {"text": "The bottom lane can wait while I take space.", "tone": "calm"})
    assert not refused["result"]["ok"]
    assert move(service)["speech"] is None


@pytest.mark.parametrize("mode", ["live", "rehearsal"])
def test_delivery_failure_gate_revokes_pending_campaign_receipt_without_interrupting_input_job(fixture, mode):
    service, supervisor, context, clock, declaration = fixture
    if mode == "rehearsal":
        declaration[1:] = ["public-qb-results-rehearsal", "qb-match-history-rehearsal"]
        (service._public_stream_event_dir.parent / "speech-consumer-rehearsal.json").write_text(json.dumps({
            "version": 1, "mode": "rehearsal", "at": events.datetime.now(events.timezone.utc).isoformat(),
            "available": True, "busy": False, "speech_ready": True,
            "consecutive_failures": 0, "failure_limit": 2,
        }))
    say(supervisor)
    directory = service._public_stream_event_dir
    before = list(directory.glob("*.json"))
    job = {"operation": "run_initiate_queens_blood_match", "state": "running", "job_id": "busy"}
    supervisor._async_request = job
    at = events.datetime.fromtimestamp(clock[0], events.timezone.utc).isoformat()
    (directory.parent / f"speech-consumer-{mode}.json").write_text(json.dumps({
        "version": 1, "mode": mode, "at": at, "available": True, "busy": False,
        "speech_ready": True, "consecutive_failures": 2, "failure_limit": 2,
        "previous_delivery": {"status": "failed", "queued_at": at, "at": at,
                              "error": {"name": "OBSWebSocketError", "message": "OBS voice playback: request failed", "code": 601}},
    }))
    refused = supervisor.call("say_on_stream", {"text": "The bottom lane can wait while I take space.", "tone": "calm"})["result"]
    assert refused["ok"] is False and refused["reason"] == "speech_delivery_failing"
    assert refused["previous_delivery"]["error"]["code"] == 601
    assert "receipt_id" not in refused and list(directory.glob("*.json")) == before
    assert supervisor._async_request is job and job["state"] == "running"
    from ff7r_agent.service import SafetyError
    with pytest.raises(SafetyError, match="fresh successful"):
        service._claim_campaign_speech_before_input()
    assert json.loads(receipts.receipt_path(directory).read_text())["receipt"] is None


def test_fresh_receipt_survives_worker_restart_and_claims_atomically(fixture):
    service, supervisor, context, clock, declaration = fixture
    response = say(supervisor)
    directory = service._public_stream_event_dir
    def claim(_):
        publisher = narration.AgentSpeechPublisher(directory)
        with publisher._shared_gate(directory) as acquired:
            return receipts.consume_locked(directory, context()) if acquired else None
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, range(2)))
    claimed = [value for value in claims if value is not None]
    assert len(claimed) == 1 and claimed[0]["receipt_id"] == response["receipt_id"]


@pytest.mark.parametrize("operation", ["place", "pass"])
def test_campaign_input_refuses_missing_fresh_speech_before_ui_or_taps(fixture, operation):
    from test_agent import FakeRealInput
    from ff7r_agent.service import SafetyError
    service, *_ = fixture
    service.input = FakeRealInput()
    with pytest.raises(SafetyError, match="fresh successful"):
        if operation == "place":
            service.place_queens_blood_card(0, 0, 0)
        else:
            service.pass_queens_blood_turn()
    assert not any(action.get("action") == "tap" for action in service.input.actions)
    assert not service._action_lock.locked()


def test_not_live_new_attempt_cannot_reuse_prior_success(fixture, monkeypatch):
    service, supervisor, *_ = fixture
    say(supervisor)
    monkeypatch.setattr(narration, "stream_is_live", lambda _: False)
    monkeypatch.setattr(narration, "rehearsal_is_live", lambda _: False)
    result = supervisor.call("say_on_stream", {"text": "That lane needs another card.", "tone": "calm"})
    assert result["result"]["reason"] == "not_live"
    assert move(service)["speech"] is None


@pytest.mark.parametrize("mode", ["live", "rehearsal"])
def test_public_result_and_history_writers_touch_only_current_mode(fixture, mode):
    from types import SimpleNamespace
    service, supervisor, context, clock, declaration = fixture
    if mode == "rehearsal":
        declaration[1:] = ["public-qb-results-rehearsal", "qb-match-history-rehearsal"]
    root = service._public_stream_event_dir.parent
    results, history = declaration[1:]
    opposite = ["public-qb-results-rehearsal", "qb-match-history-rehearsal"] if mode == "live" else ["public-qb-results", "qb-match-history"]
    for folder in opposite:
        (root / folder).mkdir()
        (root / folder / "preserved.json").write_text('{"previous":true}')
    before = {path: path.read_bytes() for folder in opposite for path in (root / folder).iterdir()}
    service.input.is_real = True  # exercise the production writers; no input method is invoked
    summary = {"ok": True, "action": "finish_queens_blood_match", "outcome": "match_finished", "opponent": NED,
               "won": True, "score": {"player": 12, "opponent": 3}, "elapsed_seconds": 60}
    service._record_action(**summary)
    service._write_queen_blood_history(summary, SimpleNamespace(board=[]), [[12, 3], [0, 0], [0, 0]], None)
    assert len(list((root / results).glob("*.json"))) == 1
    assert len(list((root / history).glob("*.json"))) == 1
    assert {path: path.read_bytes() for folder in opposite for path in (root / folder).iterdir()} == before


def test_legacy_service_publisher_uses_its_exact_loaded_profile(fixture):
    service, supervisor, context, clock, declaration = fixture
    result = service.say_on_stream("The middle lane is open enough to build from.", "calm")
    assert result["ok"] and result["receipt_id"]
    assert move(service)["speech"]["context"] == context()



def test_restoring_consumed_pending_receipt_is_refused_by_separate_claim_history(fixture):
    service, supervisor, *_ = fixture
    say(supervisor)
    path = receipts.receipt_path(service._public_stream_event_dir)
    saved = path.read_bytes()
    assert move(service)["speech"] is not None
    path.write_bytes(saved)
    assert move(service)["speech"] is None


def test_unconsumed_receipt_survives_same_game_worker_restart(fixture):
    service, supervisor, *_ = fixture
    response = say(supervisor)
    restarted = AgentService(NoGame(), NoGame(), environment=Processes(), queen_blood_profile_root=service._queen_blood_profile_root,
        public_stream_event_dir=service._public_stream_event_dir, queen_blood_recording_root=service._qb_recorder.root)
    restarted._queen_blood_match = {"opponent": NED}
    assert move(restarted)["speech"]["receipt_id"] == response["receipt_id"]



def test_default_supervisor_provider_reads_exact_verified_run_without_public_identity_leak(fixture, monkeypatch):
    from ff7r_agent import safety
    service, supervisor, context, clock, declaration = fixture
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE_ROOT", str(service._queen_blood_profile_root))
    monkeypatch.setattr(safety, "WindowsGameEnvironment", Processes)
    supervisor._speech = narration.AgentSpeechPublisher(service._public_stream_event_dir)
    response = say(supervisor)
    assert move(service)["speech"]["context"] == context()
    queued = [json.loads(path.read_text()) for path in service._public_stream_event_dir.glob("*.json")]
    speech = next(value for value in queued if value.get("kind") == "agent_say")
    assert "context" not in speech and "profile_id" not in speech and "receipt_id" not in speech



def test_clock_rollback_retains_claim_and_refuses_replayed_receipt(fixture):
    service, supervisor, context, *_ = fixture
    say(supervisor)
    path = receipts.receipt_path(service._public_stream_event_dir)
    saved = path.read_bytes()
    created = json.loads(saved)["receipt"]["created_epoch"]
    with service._agent_speech._shared_gate(service._public_stream_event_dir) as acquired:
        assert acquired
        assert receipts.consume_locked(service._public_stream_event_dir, context(), now=created + 10)
    path.write_bytes(saved)
    with service._agent_speech._shared_gate(service._public_stream_event_dir) as acquired:
        assert acquired
        assert receipts.consume_locked(service._public_stream_event_dir, context(), now=created + 5) is None
