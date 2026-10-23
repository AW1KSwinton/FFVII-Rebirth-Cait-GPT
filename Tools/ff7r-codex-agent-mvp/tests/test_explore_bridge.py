"""explore_view and explore_goto_async through the runtime registry and the stable bridge."""
from __future__ import annotations

import json
import time

import pytest

from ff7r_agent import explore_mode, runtime_bridge
from ff7r_agent.runtime_bridge import RuntimeBridgeError, RuntimeInfrastructureError, RuntimeSupervisor
from ff7r_agent.runtime_commands import SERVICE_ALIASES, invoke_runtime
from ff7r_agent.service import AgentService
from test_runtime_bridge import BlockingStdout, FakeAsyncWorker, FakeBroker, install_clean_worker


class ExploreService:
    def __init__(self):
        self.calls = []

    def explore_view(self, **kwargs):
        self.calls.append(("explore_view", kwargs))
        return {"ok": True, "input_sent": False, **kwargs}


def test_explore_view_is_a_registered_read_only_service_operation():
    assert SERVICE_ALIASES["explore_view"] == "explore_view"
    assert callable(AgentService.explore_view)
    service = ExploreService()
    result = invoke_runtime(service, object(), None, "explore_view", {"radius_m": 30.0, "max_candidates": 4})
    assert result == {"ok": True, "input_sent": False, "radius_m": 30.0, "max_candidates": 4}
    assert service.calls == [("explore_view", {"radius_m": 30.0, "max_candidates": 4})]


def test_the_agent_service_view_delegates_to_explore_mode(monkeypatch):
    seen = {}
    monkeypatch.setattr(explore_mode, "explore_view", lambda service, **kwargs: seen.update(kwargs) or {"ok": True})
    # explore_view reads only observe()/telemetry, so an uninitialised service object is enough to check delegation
    assert AgentService.explore_view(object.__new__(AgentService), radius_m=45.0, include_pois=False) == {"ok": True}
    assert seen["radius_m"] == 45.0 and seen["include_pois"] is False and seen["output_dir"] is None


def test_the_worker_runs_explore_goto_through_the_internal_operation(monkeypatch):
    seen = {}
    monkeypatch.setattr(explore_mode, "run_explore_goto",
                        lambda service, lifecycle, values: seen.update(values=dict(values), lifecycle=lifecycle) or {"ok": True})
    marker = object()
    assert invoke_runtime(ExploreService(), marker, None, "run_explore_goto", {"x": 1.0, "y": 2.0}) == {"ok": True}
    assert seen == {"values": {"x": 1.0, "y": 2.0}, "lifecycle": marker}


def make_job(line='{"ok":true,"id":1,"result":{"ok":true,"outcome":"arrived"}}\n'):
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout(line)
    worker, broker = FakeAsyncWorker(stdout), FakeBroker()
    supervisor._worker, supervisor._broker = worker, broker
    started = supervisor.call("explore_goto_async", {"candidate": 3, "execute": True, "view_id": "view-1"})
    return supervisor, worker, broker, stdout, started


def test_explore_goto_runs_as_one_async_job_polled_by_its_status():
    supervisor, worker, _, stdout, started = make_job()
    assert started["ok"] is True and started["job_id"].startswith("explore-") and started["state"] == "running"
    request = json.loads(worker.stdin.writes[0])
    assert request["operation"] == "run_explore_goto"
    assert request["arguments"] == {"candidate": 3, "execute": True, "view_id": "view-1"}
    assert supervisor.call("get_explore_goto_status", {"job_id": started["job_id"]})["state"] == "running"
    stdout.release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        status = supervisor.call("get_explore_goto_status", {"job_id": started["job_id"]})
        if status["state"] == "completed":
            break
        time.sleep(0.01)
    else:
        pytest.fail("the explore walk did not complete")
    assert status["operation"] == "run_explore_goto" and status["response"]["result"]["outcome"] == "arrived"
    # finished: ordinary operations work again
    assert supervisor.call("get_explore_goto_status", {"job_id": started["job_id"], "include_response": False})["response_available"]


@pytest.mark.parametrize(("operation", "arguments"), [
    ("explore_view", {}),
    ("explore_goto_async", {"x": 100.0, "y": 200.0, "execute": True}),
])
def test_explore_source_validation_fails_before_a_worker_request(monkeypatch, operation, arguments):
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker
    monkeypatch.setattr(runtime_bridge, "profile_contract_from_environment",
                        lambda: {"deployment_profile": "FEATURE_VALIDATION"})

    def invalid_checkout(contract):
        raise ValueError("expected feature commit does not match")

    monkeypatch.setattr(runtime_bridge, "_cached_checkout_validation", invalid_checkout)
    monkeypatch.setattr(supervisor, "_ensure_worker_locked", lambda: pytest.fail("must not start a worker"))
    with pytest.raises(RuntimeInfrastructureError) as refusal:
        supervisor.call(operation, arguments)
    assert refusal.value.as_response()["phase"] == "checkout"
    assert worker.stdin.writes == [] and supervisor._async_request is None


def test_other_input_is_refused_while_the_walk_runs_but_status_stop_and_speech_are_allowed():
    supervisor, worker, _, stdout, started = make_job()
    try:
        for operation in ("tap_button", "move_direction", "explore_view", "start_live_route_adapter", "explore_goto_async"):
            refused = supervisor.call(operation, {})
            assert refused["error"] == "async_explore_in_progress", operation
            assert refused["job_id"] == started["job_id"]
            assert refused["allowed_operations"] == ["get_explore_goto_status", "say_on_stream", "stop_all_input"]
        assert len(worker.stdin.writes) == 1
    finally:
        stdout.release.set()


def test_the_internal_worker_operation_cannot_be_called_directly():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    supervisor._worker = FakeAsyncWorker()
    refused = supervisor.call("run_explore_goto", {"x": 1.0, "y": 2.0})
    assert refused == {"ok": False, "error": "explore_goto_requires_async", "use_operation": "explore_goto_async",
                       "status_operation": "get_explore_goto_status"}


def test_stop_all_input_cancels_the_walk_and_neutralises(monkeypatch):
    supervisor, worker, broker, stdout, started = make_job('{"ok":true,"id":1,"result":{}}\n')
    monkeypatch.setattr(supervisor, "_start_worker_locked",
                        lambda: install_clean_worker(supervisor) and supervisor.status_locked())
    try:
        stopped = supervisor.call("stop_all_input", {})
        assert stopped["result"]["async_explore_cancelled"] is True
        assert stopped["result"]["cancelled_job_id"] == started["job_id"]
        assert worker.terminated and broker.neutralise_calls >= 1
    finally:
        stdout.release.set()


def test_a_restart_during_the_walk_is_refused_and_names_the_walk(monkeypatch):
    supervisor, worker, _, stdout, started = make_job()
    try:
        with pytest.raises(RuntimeBridgeError, match="async_explore_in_progress.*explore walk"):
            supervisor.call("restart_agent_runtime", {})
    finally:
        stdout.release.set()


def test_status_of_an_unknown_walk_is_labelled():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    supervisor._worker = FakeAsyncWorker()
    assert supervisor.call("get_explore_goto_status", {})["error"] == "no asynchronous explore walk request exists"
    supervisor, _, _, stdout, started = make_job()
    try:
        missing = supervisor.call("get_explore_goto_status", {"job_id": "explore-999"})
        assert missing["error"] == "asynchronous explore walk job not found"
    finally:
        stdout.release.set()


def test_speech_does_not_touch_the_worker_during_a_walk(monkeypatch):
    supervisor, worker, _, stdout, started = make_job()
    try:
        monkeypatch.setattr(supervisor, "_terminate_worker_locked", lambda: pytest.fail("speech must not stop the walk"))
        monkeypatch.setattr(supervisor, "_ensure_worker_locked", lambda: pytest.fail("speech must not start a worker"))
        # not live: refused, but never by touching the worker
        reply = supervisor.call("say_on_stream", {"text": "Heading for the far end of the street."})
        assert reply["ok"] is True and not worker.terminated and len(worker.stdin.writes) == 1
    finally:
        stdout.release.set()
