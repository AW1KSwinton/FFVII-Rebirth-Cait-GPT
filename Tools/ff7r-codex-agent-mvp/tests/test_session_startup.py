from copy import deepcopy
import asyncio
from functools import wraps
from types import SimpleNamespace

import pytest

from ff7r_agent.session_startup import NativeServices, StartupBlocked, failure_reason, payload, startup


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


def options(**changes):
    return SimpleNamespace(model="gpt-6.1-sol", effort="xhigh", driver="ChatGPT", mode="rehearsal",
                           client_id="session-test", timeout=120, check_only=False, **changes)


def run_state(loaded=True):
    return {"driver": {"driver": "ChatGPT", "model_id": "gpt-6.1-sol", "effort": "xhigh"},
            "driver_state": {"rehearsal_live": True},
            "assignment": {"model_id": "gpt-6.1-sol", "slot": 15},
            "run_loaded": loaded, "loaded_save_verified": loaded, "profile_id": "model-15",
            "loaded_save": {"slot": 15, "load_id": "receipt", "process": {"pid": 101, "token": "created"}}}


class Services:
    def __init__(self):
        self.actions = []
        self.status = dict(localSpeechReady=True, voiceInputReady=True, statsInputReady=True,
                           legalNoticeReady=True, streaming=False, recording=True, consumerReady=True,
                           consumerPresent=True, contextReady=True, contextPresent=True,
                           monitorRunning=True, monitorSeesGame=True,
                           previousDelivery="media_completed", speechFailures=0)

    async def inspect(self, operation="inspect"):
        self.actions.append(operation)
        if operation == "record": self.status["recording"] = True
        return deepcopy(self.status)

    async def start_speech(self):
        self.actions.append("start-speech")
        self.status.update(consumerReady=True, consumerPresent=True)

    async def start_recording_context(self):
        self.actions.append("start-recording-context")
        self.status.update(contextReady=True, contextPresent=True)

    async def declare_driver(self):
        self.actions.append("declare-driver")

    async def readiness_checks(self):
        self.actions.append("readiness-checks")


class Harness:
    def __init__(self, loaded=True):
        self.calls = []
        self.run = run_state(loaded)
        self.lease = {"active": False}
        self.overrides = {}
        self.job_result = {"state": "completed", "response": {"ok": True, "result": {"ok": True}}}

    async def __call__(self, operation, arguments=None):
        self.calls.append((operation, arguments or {}))
        if operation in self.overrides: return deepcopy(self.overrides[operation])
        values = {"get_runtime_identity": {"git_commit_sha": "sha", "expected_feature_ref": "ref", "repository_root": "root"},
                  "get_lifecycle_status": {"lease": {"pid": 99}}, "get_session_health": {"gameplay_ready": True, "process_exists": False},
                  "observe_game": {"running": False}, "get_bootstrap_verification": {"ok": True, "runtime_ownership": {"conflicts": []}},
                  "get_control_session_status": self.lease, "get_queens_blood_run_status": self.run,
                  "say_on_stream": {"ok": True, "previous_delivery": {"status": "media_completed"}},
                  "start_model_run_async": {"job_id": "job-1"}, "recover_stale_lifecycle": {"ok": True},
                  "get_campaign_status": {}, "get_campaign_next_step": {"action": "visit_opponent"},
                  "stop_all_input": {}, "close_control_session": {}}
        if operation == "get_start_model_run_status":
            self.run = run_state()
            # Real registered startup retains the explicitly attributed lease.
            self.lease = {"active": True, "client_id": "session-test", "game_pid": 101,
                          "recording_driver": {"modelId": "gpt-6.1-sol", "effort": "xhigh"}}
            return deepcopy(self.job_result)
        if operation == "open_control_session":
            self.lease = {"active": True, "client_id": arguments["client_id"], "game_pid": 101,
                          "recording_driver": {"modelId": "gpt-6.1-sol", "effort": "xhigh"}}
            return deepcopy(self.lease)
        return deepcopy(values[operation])


async def no_sleep(_): pass
def validate(_identity, _options): pass


@async_test
async def test_ready_repeat_does_not_load_or_start_duplicate_services():
    harness, services = Harness(), Services()
    result = await startup(harness, services, options(), sleep=no_sleep, identity_validator=validate)
    assert result["ready"] and result["slot"] == 15
    operations = [name for name, _ in harness.calls]
    assert operations[:4] == ["get_runtime_identity", "get_lifecycle_status", "get_session_health", "observe_game"]
    assert "start_model_run_async" not in operations
    assert "start-speech" not in services.actions
    assert not any("save" in name or "assign" in name for name in operations)


@async_test
async def test_absent_game_narrated_registered_load_and_nested_receipt():
    harness, services = Harness(False), Services()
    result = await startup(harness, services, options(), sleep=no_sleep, identity_validator=validate)
    operations = [name for name, _ in harness.calls]
    assert operations.index("say_on_stream") < operations.index("start_model_run_async")
    assert operations.index("recover_stale_lifecycle") < operations.index("start_model_run_async")
    start = next(arguments for name, arguments in harness.calls if name == "start_model_run_async")
    assert "slot" not in start and start["model_id"] == "gpt-6.1-sol"
    assert result["loaded_save_receipt"] == "receipt"
    assert "open_control_session" not in operations


@async_test
async def test_cold_title_lease_is_bound_to_verified_loaded_process_once():
    harness, services = Harness(False), Services()
    original = harness.__call__
    async def call(operation, arguments=None):
        result = await original(operation, arguments)
        if operation == "get_start_model_run_status":
            harness.lease.update(game_pid=None, owner_matches=True)
        if operation == "close_control_session":
            harness.lease = {"active": False}
            return {"closed": True}
        return result
    result = await startup(call, services, options(), sleep=no_sleep, identity_validator=validate)
    operations = [name for name, _ in harness.calls]
    assert result["ready"] and result["control_session"]["game_pid"] == 101
    assert operations.count("start_model_run_async") == 1
    assert operations.count("close_control_session") == operations.count("open_control_session") == 1
    assert operations.index("get_start_model_run_status") < operations.index("close_control_session") < operations.index("open_control_session")


@async_test
async def test_foreign_control_blocks_before_driver_or_services():
    harness, services = Harness(), Services()
    harness.lease = {"active": True, "client_id": "another-task"}
    with pytest.raises(StartupBlocked, match="another_client_owns_control"):
        await startup(harness, services, options(), identity_validator=validate)
    assert services.actions == []


@async_test
async def test_identity_failure_precedes_all_service_and_input_actions():
    harness, services = Harness(), Services()
    def mismatch(_identity, _options): raise StartupBlocked("runtime_commit_mismatch")
    with pytest.raises(StartupBlocked, match="runtime_commit_mismatch"):
        await startup(harness, services, options(), identity_validator=mismatch)
    assert len(harness.calls) == 4 and not services.actions


@async_test
@pytest.mark.parametrize("speech", [{"ok": False, "reason": "not_live"},
                                  {"ok": True, "previous_delivery": {"status": "dropped"}},
                                  {"ok": False, "reason": "rate_limited", "previous_delivery": {"status": "failed"}}])
async def test_speech_refusal_sends_no_game_input(speech):
    harness, services = Harness(False), Services()
    harness.overrides["say_on_stream"] = speech
    with pytest.raises(StartupBlocked, match="startup_speech_failed"):
        await startup(harness, services, options(), identity_validator=validate)
    assert "start_model_run_async" not in [name for name, _ in harness.calls]


@async_test
async def test_completed_async_with_failed_nested_result_is_not_ready():
    harness, services = Harness(False), Services()
    harness.job_result["response"]["result"] = {"ok": False, "decision": "load_failed"}
    with pytest.raises(StartupBlocked, match="model_load_failed"):
        await startup(harness, services, options(), identity_validator=validate)
    operations = [name for name, _ in harness.calls]
    assert "stop_all_input" in operations and "open_control_session" not in operations


@async_test
async def test_check_only_has_no_driver_load_record_or_lease_mutations():
    harness, services = Harness(), Services()
    config = options(); config.check_only = True
    assert (await startup(harness, services, config, identity_validator=validate))["ready"]
    assert services.actions == ["inspect", "readiness-checks"]
    assert not any(name in {"say_on_stream", "open_control_session", "start_model_run_async"} for name, _ in harness.calls)


@async_test
async def test_live_mode_never_starts_recording_or_broadcast():
    harness, services = Harness(), Services()
    config = options(); config.mode = "live"
    with pytest.raises(StartupBlocked, match="obs_not_streaming"):
        await startup(harness, services, config, identity_validator=validate)
    assert services.actions == ["inspect"]


@async_test
async def test_unhealthy_existing_consumer_is_preserved():
    harness, services = Harness(), Services()
    services.status["consumerReady"] = False
    with pytest.raises(StartupBlocked, match="existing_speech_consumer_unhealthy"):
        await startup(harness, services, options(), identity_validator=validate)
    assert "start-speech" not in services.actions


def test_worker_failure_and_semantic_rate_limit_are_distinguished():
    with pytest.raises(StartupBlocked, match="SafetyError"):
        payload({"ok": False, "error": "private detail", "error_type": "SafetyError"})
    assert payload({"ok": True, "result": {"ok": False, "reason": "rate_limited"}})["reason"] == "rate_limited"


def test_mcp_context_group_keeps_specific_blocker_without_private_exception():
    error = ExceptionGroup("private context detail", [ExceptionGroup("transport", [StartupBlocked("obs_unavailable:ECONNREFUSED")])])
    assert failure_reason(error) == "obs_unavailable:ECONNREFUSED"


@async_test
async def test_verified_running_save_gets_narrated_focus_recovery_without_reload():
    harness, services = Harness(), Services()
    original = harness.__call__
    focused = False
    settled = False
    async def call(operation, arguments=None):
        nonlocal focused, settled
        if operation == "get_session_health" and not settled:
            harness.calls.append((operation, arguments or {}))
            return {"gameplay_ready": False, "process_exists": True,
                    "diagnosis": "gameplay_settling" if focused else "foreground_missing"}
        if operation == "focus_game_window":
            harness.calls.append((operation, arguments or {})); focused = True
            return {"ok": True}
        if operation == "wait_for_settled_gameplay":
            harness.calls.append((operation, arguments or {})); settled = True
            return {"ok": True}
        return await original(operation, arguments)
    assert (await startup(call, services, options(), identity_validator=validate))["ready"]
    operations = [name for name, _ in harness.calls]
    assert operations.index("say_on_stream") < operations.index("focus_game_window")
    assert operations.index("focus_game_window") < operations.index("wait_for_settled_gameplay")
    assert operations.count("wait_for_settled_gameplay") == 1
    assert operations.count("get_session_health") == 4
    assert "start_model_run_async" not in operations


@async_test
async def test_failed_settling_gate_never_opens_control_or_loads_again():
    harness, services = Harness(), Services()
    harness.overrides["get_session_health"] = {"gameplay_ready": False, "process_exists": True, "diagnosis": "gameplay_settling"}
    harness.overrides["wait_for_settled_gameplay"] = {"ok": False}
    with pytest.raises(StartupBlocked, match="loaded_gameplay_not_settled"):
        await startup(harness, services, options(), identity_validator=validate)
    operations = [name for name, _ in harness.calls]
    assert operations.count("wait_for_settled_gameplay") == 1
    assert not {"open_control_session", "start_model_run_async"} & set(operations)


@async_test
async def test_explicit_rehearsal_starts_recording_and_one_missing_consumer():
    harness, services = Harness(), Services()
    services.status.update(recording=False, consumerReady=False, consumerPresent=False)
    assert (await startup(harness, services, options(), sleep=no_sleep, identity_validator=validate))["ready"]
    assert services.actions.count("record") == 1
    assert services.actions.count("start-speech") == 1


@async_test
async def test_load_timeout_stops_before_other_input_and_does_not_retry():
    harness, services = Harness(False), Services()
    harness.overrides["get_start_model_run_status"] = {"state": "running"}
    ticks = iter((0, 200))
    with pytest.raises(StartupBlocked, match="model_load_timeout"):
        await startup(harness, services, options(), clock=lambda: next(ticks), identity_validator=validate)
    operations = [name for name, _ in harness.calls]
    assert operations.count("start_model_run_async") == 1
    assert operations.index("stop_all_input") > operations.index("get_start_model_run_status")
    assert "open_control_session" not in operations


@async_test
async def test_receipt_must_match_assignment_before_opening_control():
    harness, services = Harness(), Services()
    harness.run["loaded_save"]["slot"] = 14
    with pytest.raises(StartupBlocked, match="model_save_receipt_unverified"):
        await startup(harness, services, options(), identity_validator=validate)
    assert "open_control_session" not in [name for name, _ in harness.calls]


@async_test
async def test_cold_obs_connection_is_retried_without_game_input():
    harness, services = Harness(), Services()
    original = services.inspect
    attempts = []
    async def cold(operation="inspect"):
        attempts.append(operation)
        if len(attempts) == 1:
            raise StartupBlocked("obs_connection_refused:obs_connection:ECONNREFUSED")
        return await original(operation)
    services.inspect = cold
    assert (await startup(harness, services, options(), sleep=no_sleep, identity_validator=validate))["ready"]
    assert attempts[:2] == ["inspect", "inspect"]
    assert "start_model_run_async" not in [name for name, _ in harness.calls]


@async_test
async def test_receipt_string_pid_matches_integer_control_session():
    harness, services = Harness(), Services()
    harness.run["loaded_save"]["process"]["pid"] = "101"
    assert (await startup(harness, services, options(), identity_validator=validate))["game_pid"] == 101


@async_test
async def test_offline_mode_uses_silent_recording_context_without_speech_gates_or_narration():
    harness, services = Harness(False), Services()
    services.status.update(localSpeechReady=False, voiceInputReady=False, statsInputReady=False,
                           legalNoticeReady=True, consumerReady=False, consumerPresent=False,
                           contextReady=True, contextPresent=True)
    config = options(); config.mode = "offline"
    result = await startup(harness, services, config, sleep=no_sleep, identity_validator=validate)
    calls = [name for name, _ in harness.calls]
    assert result["ready"] and result["mode"] == "offline"
    assert "say_on_stream" not in calls
    assert "readiness-checks" in services.actions and "declare-driver" in services.actions
    assert "start-speech" not in services.actions and "start-recording-context" not in services.actions
    assert "open_control_session" not in calls


@async_test
async def test_offline_starts_recording_then_silent_context_and_never_speaks():
    harness, services = Harness(False), Services()
    services.status.update(recording=False, contextReady=False, contextPresent=False,
                           consumerReady=False, consumerPresent=False)
    config = options(); config.mode = "offline"
    result = await startup(harness, services, config, sleep=no_sleep, identity_validator=validate)
    calls = [name for name, _ in harness.calls]
    assert result["ready"]
    assert services.actions[:5] == ["inspect", "record", "start-recording-context", "inspect", "readiness-checks"], services.actions
    assert "start-speech" not in services.actions
    assert "say_on_stream" not in calls
    assert calls.index("start_model_run_async") < calls.index("get_start_model_run_status")


@async_test
async def test_offline_check_only_requires_existing_context_without_starting_or_speaking():
    harness, services = Harness(), Services()
    config = options(); config.mode = "offline"; config.check_only = True
    result = await startup(harness, services, config, identity_validator=validate)
    assert result["ready"] and result["check_only"]
    assert services.actions == ["inspect", "readiness-checks"]
    assert not any(name in {"say_on_stream", "start_model_run_async", "open_control_session"}
                   for name, _ in harness.calls)


@async_test
async def test_offline_mode_fails_closed_on_stream_or_unhealthy_context():
    harness, services = Harness(), Services()
    services.status["streaming"] = True
    config = options(); config.mode = "offline"
    with pytest.raises(StartupBlocked, match="offline_stream_state_unverified"):
        await startup(harness, services, config, identity_validator=validate)
    assert "declare-driver" not in services.actions

    harness, services = Harness(), Services()
    services.status.update(contextReady=False, contextPresent=True)
    config = options(); config.mode = "offline"
    with pytest.raises(StartupBlocked, match="existing_recording_context_unhealthy"):
        await startup(harness, services, config, identity_validator=validate)
    assert "start-recording-context" not in services.actions


@async_test
async def test_native_offline_adapter_starts_silent_rehearsal_and_skips_streamer_speech_check():
    config = SimpleNamespace(mode="offline", powershell="powershell.exe", model="gpt-6.1-sol",
                             effort="xhigh", driver="ChatGPT", node="node.exe",
                             runtime_root="C:/runtime")
    services = NativeServices(config)
    calls = []
    async def process(arguments, timeout=30, *, name=None):
        calls.append(arguments)
        return SimpleNamespace(returncode=0, stdout="monitorRunning=true\n")
    services.process = process

    await services.start_recording_context()
    await services.declare_driver()
    await services.readiness_checks()

    context_args = calls[0]
    assert "-Mode" in context_args and context_args[context_args.index("-Mode") + 1] == "Offline"
    assert "-StartSpeech" in context_args
    assert "--rehearsal" in calls[1] and "--silent" in calls[1]
    assert len(calls) == 3 and "obs.ps1" in calls[2][-2] and calls[2][-1] == "status"
