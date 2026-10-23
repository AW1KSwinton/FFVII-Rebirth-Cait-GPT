from __future__ import annotations

import pytest

from ff7r_agent.runtime_bridge import ASYNC_ROUTE_WORK_OPERATION, RuntimeBridgeError, RuntimeSupervisor


def completed_supervisor() -> RuntimeSupervisor:
    supervisor = RuntimeSupervisor()
    supervisor._async_request = {
        "job_id": "route-1",
        "state": "completed",
        "operation": ASYNC_ROUTE_WORK_OPERATION,
        "started_at": 1.0,
        "worker_generation": 2,
        "worker_pid": 123,
        "response": {
            "ok": True,
            "id": 7,
            "result": {
                "preflight": {"movement_authorized": True},
                "run": {"state": "ARRIVED", "timeline": [{"event": "x"}] * 1000},
            },
        },
    }
    return supervisor


def test_compact_completed_route_status_omits_retained_response():
    supervisor = completed_supervisor()

    compact = supervisor.call(
        "get_live_route_adapter_status",
        {"job_id": "route-1", "include_response": False},
    )

    assert compact["ok"] is True
    assert compact["state"] == "completed"
    assert compact["response_available"] is True
    assert "response" not in compact
    assert supervisor._async_request["response"]["result"]["run"]["state"] == "ARRIVED"


def test_completed_route_status_default_contract_still_returns_full_response():
    supervisor = completed_supervisor()

    full = supervisor.call("get_live_route_adapter_status", {"job_id": "route-1"})

    assert full["state"] == "completed"
    assert full["response"]["result"]["run"]["state"] == "ARRIVED"
    assert "response_available" not in full


def test_compact_status_argument_must_be_boolean():
    supervisor = completed_supervisor()
    with pytest.raises(RuntimeBridgeError, match="include_response must be a boolean"):
        supervisor.call(
            "get_live_route_adapter_status",
            {"job_id": "route-1", "include_response": "false"},
        )


def test_compact_close_preserves_the_lifecycle_result_for_full_retrieval():
    from ff7r_agent.runtime_bridge import ASYNC_CLOSE_WORK_OPERATION
    supervisor = completed_supervisor()
    supervisor._async_request["operation"] = ASYNC_CLOSE_WORK_OPERATION
    supervisor._async_request["response"] = {
        "ok": True, "result": {"ok": False, "state": "manual_end_task_required"}}
    compact = supervisor.call("get_close_game_status", {"include_response": False})
    assert compact["state"] == "completed"
    assert compact["response_available"] is True
    assert "response" not in compact
    full = supervisor.call("get_close_game_status")
    assert full["response"]["result"]["ok"] is False
    assert full["response"]["result"]["state"] == "manual_end_task_required"
