"""Issue #234: startup leases must not block the next gameplay client."""

import asyncio

import pytest

from ff7r_agent.npc_approach import AttributedApproachRpc
from ff7r_agent.safety import ArmState, SessionLease, request_session_close, request_session_open
from test_startup import _bound_slot_agent


def real_startup(monkeypatch, tmp_path):
    agent, service, driver, _ = _bound_slot_agent(monkeypatch)
    clock = [1000.0]
    lease = SessionLease(runtime_dir=tmp_path, owner_id="startup-test", clock=lambda: clock[0])
    agent._session_status = lease.status
    agent._session_open = lease.open
    agent._session_heartbeat = lease.heartbeat

    def close(**values):
        clock[0] += 1  # Distinct archive names for synthetic recovery/replacement.
        return lease.close(**values)

    agent._session_close = close
    return agent, service, driver, lease


@pytest.mark.parametrize("recovery", [False, True])
def test_next_pursuit_client_can_open_after_model_load_or_exited_game_recovery(monkeypatch, tmp_path, recovery):
    agent, service, driver, lease = real_startup(monkeypatch, tmp_path)
    if recovery:
        lease.open(game_pid=1234, client_id="model-startup:registered-test-run",
                   model_id=driver["model_id"], reasoning_effort=driver["effort"])

        class Environment:
            def game_process_ids_verified(self):
                return {4242: "replacement-game"}

        agent.lifecycle.environment = Environment()
    load = service.load_save_slot
    held_during_load = []

    def checked_load(**values):
        held_during_load.append(lease.status())
        return load(**values)

    service.load_save_slot = checked_load
    result = agent.start_and_load_save_slot(model_run=True)

    assert result["ok"] and result["control_session_release"]["closed"]
    assert held_during_load[0]["active"] and held_during_load[0]["game_pid"] == 4242
    assert held_during_load[0]["client_id"] == "model-startup:registered-test-run"
    assert lease.status() is None
    assert service.get_queens_blood_run_status()["run_loaded"]

    async def rpc(operation, arguments):
        if operation == "get_control_session_status":
            return lease.status() or {"active": False}
        assert operation == "open_control_session"
        return lease.open(**arguments)

    approach = AttributedApproachRpc(rpc, model_id=driver["model_id"],
                                     reasoning_effort=driver["effort"], client_id="pursuit-client")
    opened = asyncio.run(approach("open_control_session", {"game_pid": 4242}))
    assert opened["active"] and opened["client_id"] == "pursuit-client"
    assert opened["recording_driver"] == {"modelId": driver["model_id"], "effort": driver["effort"]}


@pytest.mark.parametrize("failure", ["load", "receipt", "declaration", "pending", "exception"])
def test_temporary_lease_is_released_on_unsuccessful_startup(monkeypatch, tmp_path, failure):
    agent, service, driver, lease = real_startup(monkeypatch, tmp_path)
    if failure == "load":
        service.result = {"ok": False, "phase": "load_failed", "decision": "unreadable_save_row"}
    elif failure == "receipt":
        status = service.get_queens_blood_run_status
        service.get_queens_blood_run_status = lambda: {**status(), "run_loaded": False}
    elif failure == "declaration":
        load = service.load_save_slot

        def changed(**values):
            result = load(**values)
            driver["run_id"] = "changed-run"
            return result

        service.load_save_slot = changed
    else:
        startup = agent.start_game_to_main_menu

        def interrupted(**values):
            assert startup(**values)["ok"]
            if failure == "exception":
                raise RuntimeError("unexpected startup failure")
            return {"ok": False, "pending": True, "phase": "startup_loading", "decision": "STARTUP_PENDING"}

        agent.start_game_to_main_menu = interrupted

    if failure == "exception":
        with pytest.raises(RuntimeError, match="unexpected startup failure"):
            agent.start_and_load_save_slot(model_run=True)
    else:
        result = agent.start_and_load_save_slot(model_run=True)
        assert not result["ok"] and result["control_session_release"]["ok"]
        assert result["decision"] != "MODEL_RUN_CONTROL_SESSION_RELEASE_FAILED"
    assert lease.status() is None
    assert agent._startup_session_binding.get() is None


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("success", [False, True])
def test_explicit_caller_keeps_lease_after_model_load(monkeypatch, tmp_path, existing, success):
    agent, service, driver, lease = real_startup(monkeypatch, tmp_path)
    if existing:
        lease.open(game_pid=4242, client_id="caller", model_id=driver["model_id"], reasoning_effort=driver["effort"])
    if not success:
        service.result = {"ok": False, "phase": "load_failed", "decision": "load_refused"}
    result = agent.start_and_load_save_slot(model_run=True, client_id="caller")
    assert result["ok"] is success
    assert "control_session_release" not in result
    assert lease.status()["active"] and lease.status()["client_id"] == "caller"
    assert not list(tmp_path.glob("control-session.closed-*.json"))


def test_implicit_startup_does_not_release_another_clients_existing_lease(monkeypatch, tmp_path):
    agent, service, driver, lease = real_startup(monkeypatch, tmp_path)
    lease.open(game_pid=4242, client_id="other-client", model_id=driver["model_id"], reasoning_effort=driver["effort"])
    result = agent.start_and_load_save_slot(model_run=True)
    assert result["ok"] and "control_session_release" not in result
    assert lease.status()["active"] and lease.status()["client_id"] == "other-client"


def test_generic_save_startup_keeps_its_control_lease(monkeypatch, tmp_path):
    agent, service, driver, lease = real_startup(monkeypatch, tmp_path)
    result = agent.start_and_load_save_slot(slot=8, execute=True)
    assert result["ok"] and "control_session_release" not in result
    assert lease.status()["active"]


@pytest.mark.parametrize("replacement", ["client", "driver", "new_lease"])
def test_cleanup_refuses_changed_lease_and_reports_failed_handoff(monkeypatch, tmp_path, replacement):
    agent, service, driver, lease = real_startup(monkeypatch, tmp_path)
    load = service.load_save_slot

    def changed(**values):
        result = load(**values)
        if replacement == "driver":
            lease.heartbeat(model_id="other-model", reasoning_effort="high")
        else:
            agent._session_close(reason="replace_for_test")
            lease.open(game_pid=4242, client_id="other-client" if replacement == "client" else "model-startup:registered-test-run",
                       model_id=driver["model_id"], reasoning_effort=driver["effort"])
        return result

    service.load_save_slot = changed
    result = agent.start_and_load_save_slot(model_run=True)
    assert not result["ok"] and result["decision"] == "MODEL_RUN_CONTROL_SESSION_RELEASE_FAILED"
    assert result["control_session_release"]["reason"] == "control_session_changed"
    assert lease.status()["active"]
    assert result["load"]["ok"]  # Save verification remains visible separately.


@pytest.mark.parametrize("failure", ["refusal", "exception"])
def test_failed_release_never_claims_successful_model_startup(monkeypatch, tmp_path, failure):
    agent, service, driver, lease = real_startup(monkeypatch, tmp_path)

    def failed_close(**values):
        if failure == "exception":
            raise OSError("archive unavailable")
        return {"closed": False, "reason": "archive unavailable"}

    agent._session_close = failed_close
    result = agent.start_and_load_save_slot(model_run=True)
    assert not result["ok"] and result["decision"] == "MODEL_RUN_CONTROL_SESSION_RELEASE_FAILED"
    assert not result["control_session_release"]["ok"] and lease.status()["active"]
    assert agent._startup_session_binding.get() is None


def test_guarded_close_does_not_latch_input_for_a_replacement_lease(tmp_path):
    lease = SessionLease(runtime_dir=tmp_path)
    original = lease.open(game_pid=4242, client_id="startup")
    lease.close()
    replacement = lease.open(game_pid=4242, client_id="pursuit")
    broker = ArmState(runtime_dir=tmp_path)
    broker.reset_stop()
    broker.arm(60)

    result = request_session_close(runtime_dir=tmp_path, expected_session=original)

    assert result == {"closed": False, "reason": "control_session_changed"}
    assert lease.status()["opened_at"] == replacement["opened_at"]
    assert lease.status()["client_id"] == "pursuit" and broker.status()[0]


def test_guarded_close_accepts_heartbeat_renewal(tmp_path):
    clock = [1000.0]
    lease = SessionLease(runtime_dir=tmp_path, clock=lambda: clock[0])
    original = lease.open(game_pid=4242, client_id="startup")
    clock[0] += 1
    lease.heartbeat()
    assert lease.close(expected_session=original)["closed"]
    assert lease.status() is None


def test_two_session_closures_in_same_second_preserve_both_archives(tmp_path):
    lease = SessionLease(runtime_dir=tmp_path, clock=lambda: 1000.0)
    lease.open(game_pid=4242, client_id="approach")
    first = lease.close()
    lease.open(game_pid=4242, client_id="setup")
    second = lease.abort()
    assert first["closed"] and second["closed"] and lease.status() is None
    assert first["archive_path"] != second["archive_path"]
    assert len(list(tmp_path.glob("control-session.closed-*.json"))) == 2


def test_released_startup_can_rearm_through_normal_gameplay_session_open(tmp_path):
    original = request_session_open(runtime_dir=tmp_path, game_pid=4242, client_id="startup",
                                    model_id="gpt-6.1-sol", reasoning_effort="xhigh")
    assert request_session_close(runtime_dir=tmp_path, expected_session=original)["closed"]
    broker = ArmState(runtime_dir=tmp_path)
    assert broker.status()[0] is False
    opened = request_session_open(runtime_dir=tmp_path, game_pid=4242, client_id="pursuit",
                                 model_id="gpt-6.1-sol", reasoning_effort="xhigh")
    assert opened["active"] and opened["client_id"] == "pursuit" and broker.status()[0]
