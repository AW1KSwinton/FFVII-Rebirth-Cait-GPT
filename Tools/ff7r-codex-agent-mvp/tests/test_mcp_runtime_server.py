from __future__ import annotations

import threading

import pytest

from ff7r_agent import mcp_runtime_server
from ff7r_agent.runtime_bridge import (
    RuntimeBridgeError,
    RuntimeInfrastructureError,
    RuntimeSupervisor,
)


def test_agent_call_returns_structured_infrastructure_error(monkeypatch) -> None:
    error = RuntimeInfrastructureError(
        "RUNTIME_BRIDGE_NOT_READY",
        operation="observe_game",
        phase="rebind",
        message="automatic bridge source rebind failed; live operation was not sent",
        diagnostics={"rebind_attempted": True, "cause": "start failed"},
    )

    class FakeRuntime:
        def call(self, operation: str, arguments: dict | None = None) -> dict:
            raise error

    monkeypatch.setattr(mcp_runtime_server, "_runtime", lambda: FakeRuntime())

    result = mcp_runtime_server.agent_call("observe_game", {})

    assert result == error.as_response()
    assert result["diagnostics"]["rebind_attempted"] is True


def test_agent_call_routes_bridge_identity_without_worker_dispatch(monkeypatch) -> None:
    calls = []

    class FakeRuntime:
        def call(self, operation: str, arguments: dict | None = None) -> dict:
            calls.append((operation, arguments))
            return {"ok": True, "owner": "bridge"}

    monkeypatch.setattr(mcp_runtime_server, "_runtime", lambda: FakeRuntime())

    result = mcp_runtime_server.agent_call("get_runtime_identity", {})

    assert result == {"ok": True, "owner": "bridge"}
    assert calls == [("get_runtime_identity", {})]



def test_narration_tool_tone_schema_matches_publisher() -> None:
    from typing import get_args, get_type_hints

    from ff7r_agent.public_stream_events import SPEECH_TONES

    annotation = get_type_hints(mcp_runtime_server.say_on_stream)["tone"]
    literal = next(part for part in get_args(annotation) if get_args(part))
    assert get_args(literal) == SPEECH_TONES
    assert "focused" not in SPEECH_TONES
    tool = mcp_runtime_server.mcp._tool_manager.get_tool("say_on_stream")
    tone_schema = tool.parameters["properties"]["tone"]
    advertised = next(part["enum"] for part in tone_schema["anyOf"] if "enum" in part)
    assert advertised == list(SPEECH_TONES)
    assert tone_schema["default"] is None
    assert "tone" not in tool.parameters["required"]


@pytest.mark.parametrize("response", [
    {"ok": True}, {"ok": True, "result": None}, {"ok": True, "result": []},
    {"ok": True, "result": {}}, {"ok": True, "result": {"ok": "true"}},
])
def test_dedicated_narration_refuses_missing_or_invalid_speech_acceptance(monkeypatch, response) -> None:
    class FakeRuntime:
        def call(self, operation: str, arguments: dict) -> dict:
            return response

    monkeypatch.setattr(mcp_runtime_server, "_runtime", lambda: FakeRuntime())
    result = mcp_runtime_server.say_on_stream("Cloud is beside the card table.", "calm")
    assert result["ok"] is False
    assert result["reason"] == "speech_response_invalid"


@pytest.mark.parametrize("speech_result", [
    {"ok": True, "queued_at": "2026-10-09T16:02:00Z", "receipt_id": "speech-id", "input_sent": False},
    {"ok": False, "reason": "invalid_tone", "input_sent": False},
    {"ok": False, "reason": "repetitive", "input_sent": False},
    {"ok": False, "reason": "rate_limited", "retry_after_seconds": 8, "input_sent": False},
    {"ok": True, "queued_at": "2026-10-09T16:02:00Z",
     "previous_delivery": {"status": "failed"}, "input_sent": False},
])
def test_dedicated_narration_returns_speech_result_not_dispatch_success(monkeypatch, speech_result) -> None:
    calls = []

    class FakeRuntime:
        def call(self, operation: str, arguments: dict) -> dict:
            calls.append((operation, arguments))
            return {"ok": True, "result": speech_result}

    monkeypatch.setattr(mcp_runtime_server, "_runtime", lambda: FakeRuntime())
    result = mcp_runtime_server.say_on_stream("I'll play around the top lane.", "calm")

    assert result == speech_result
    assert calls == [("say_on_stream", {"text": "I'll play around the top lane.", "tone": "calm"})]


def test_dedicated_narration_preserves_bridge_failure(monkeypatch) -> None:
    class FakeRuntime:
        def call(self, operation: str, arguments: dict) -> dict:
            raise RuntimeInfrastructureError(
                "RUNTIME_BRIDGE_NOT_READY", operation=operation, phase="rebind",
                message="bridge unavailable", diagnostics={},
            )

    monkeypatch.setattr(mcp_runtime_server, "_runtime", lambda: FakeRuntime())
    result = mcp_runtime_server.say_on_stream("I'll watch the center lane.")

    assert result["ok"] is False
    assert result["error_code"] == "RUNTIME_BRIDGE_NOT_READY"
    assert result["operation"] == "say_on_stream"


def test_dedicated_stop_all_input_wrapper_targets_allowlisted_operation(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    class FakeRuntime:
        def call(self, operation: str, arguments: dict) -> dict:
            calls.append((operation, arguments))
            return {"ok": True, "operation": operation}

    monkeypatch.setattr(mcp_runtime_server, "_runtime", lambda: FakeRuntime())

    result = mcp_runtime_server.stop_all_input()

    assert result == {"ok": True, "operation": "stop_all_input"}
    assert calls == [("stop_all_input", {})]


def test_terminal_shutdown_rejects_recreated_supervisor_before_process_exit(monkeypatch) -> None:
    close_entered = threading.Event()
    allow_close = threading.Event()
    constructed: list[object] = []

    class FakeSupervisor:
        def close(self) -> None:
            close_entered.set()
            assert allow_close.wait(2.0)

    existing = FakeSupervisor()

    def construct() -> FakeSupervisor:
        constructed.append(object())
        return FakeSupervisor()

    monkeypatch.setattr(mcp_runtime_server, "RuntimeSupervisor", construct)
    monkeypatch.setattr(mcp_runtime_server, "_supervisor", existing)
    monkeypatch.setattr(mcp_runtime_server, "_transport_shutting_down", False)

    closer = threading.Thread(target=mcp_runtime_server._close_runtime)
    closer.start()
    assert close_entered.wait(2.0)

    with pytest.raises(RuntimeBridgeError, match="runtime transport is shutting down"):
        mcp_runtime_server._runtime()

    assert constructed == []
    assert mcp_runtime_server._supervisor is None
    allow_close.set()
    closer.join(timeout=2.0)
    assert not closer.is_alive()


def test_request_queued_against_closing_supervisor_cannot_restart_worker_or_broker() -> None:
    supervisor = RuntimeSupervisor()
    start_attempts: list[bool] = []
    waiter_entered = threading.Event()

    class GateLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def acquire(self) -> bool:
            if self._lock.acquire(blocking=False):
                return True
            waiter_entered.set()
            return self._lock.acquire()

        def release(self) -> None:
            self._lock.release()

        def __enter__(self) -> "GateLock":
            self.acquire()
            return self

        def __exit__(self, *_: object) -> None:
            self.release()

    supervisor._start_worker_locked = lambda: start_attempts.append(True)  # type: ignore[method-assign]
    supervisor._lock = GateLock()  # type: ignore[assignment]
    supervisor._lock.acquire()
    result: list[BaseException] = []

    def queued_request() -> None:
        try:
            supervisor.call("get_agent_status", {})
        except BaseException as exc:  # deliberate assertion of fail-closed error
            result.append(exc)

    request = threading.Thread(target=queued_request)
    request.start()
    assert waiter_entered.wait(2.0)
    supervisor._closing = True
    supervisor._lock.release()
    request.join(timeout=2.0)

    assert not request.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], RuntimeBridgeError)
    assert "runtime supervisor is closed" in str(result[0])
    assert start_attempts == []
