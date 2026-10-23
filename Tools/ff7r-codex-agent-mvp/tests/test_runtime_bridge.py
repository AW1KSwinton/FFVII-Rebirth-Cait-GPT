from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ff7r_agent import gamepad_broker
from ff7r_agent.controller_policy import NORMAL_06_TRIAL_PROFILE, NORMAL_08_TRIAL_PROFILE, PROVEN_LIVE_CONTROLLER_POLICY
from ff7r_agent.gamepad_broker import RemoteVirtualLeftStick
from ff7r_agent.recording import HumanTraversalRecorder, RecordingError
from ff7r_agent.runtime_bridge import (
    DEFAULT_RUNTIME_REQUEST_TIMEOUT_SECONDS,
    RuntimeBridgeError,
    RuntimeInfrastructureError,
    RuntimeSupervisor,
)
from ff7r_agent.runtime_commands import _run_live_route_adapter, invoke_runtime


class FakeService:
    def __init__(self) -> None:
        self.calls = []

    def observe(self):
        self.calls.append(("observe", {}))
        return {"sequence": 1}

    def navigate_next_segment(self, **kwargs):
        self.calls.append(("navigate_next_segment", kwargs))
        return {"ok": True, **kwargs}

    def hidden_helper(self):
        return {"unsafe": True}


class FakeLifecycle:
    def status(self):
        return {"active": True}


class FakeRealInput:
    is_real = False


class FakeAdapterService:
    def __init__(self):
        self.input = FakeRealInput()


class FakeAttachedInput:
    is_real = True


class FakeAttachedService:
    def __init__(self):
        self.input = FakeAttachedInput()


class FakeAttachedEnvironment:
    def game_process_ids(self):
        return {19608: "ff7rebirth_.exe", 23656: "ff7rebirth.exe"}


class FakeAttachedLifecycle:
    def __init__(self, events):
        self.environment = FakeAttachedEnvironment()
        self.events = events

    def ensure_started(self, *, allow_launch=True):
        self.events.append(("ensure_started", allow_launch))
        return {
            "mode": "attached",
            "context": {
                "run_id": "attach-current",
                "pid": 19608,
                "attachment": "external_or_prior_task",
            },
        }


def test_live_route_runtime_wires_measured_capture_wait_budget(monkeypatch, tmp_path):
    from ff7r_agent import live_route_adapter

    captured = {}

    class CapturingProvider:
        def __init__(self, *args, **kwargs):
            captured["max_capture_wait_seconds"] = kwargs["max_capture_wait_seconds"]

    class RefusingAdapter:
        def __init__(self, *args, **kwargs):
            pass

        def preflight(self):
            return {"movement_authorized": False, "reason": "test_refusal"}

    monkeypatch.setattr(live_route_adapter, "AgentServiceTelemetrySource", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "AgentServiceRenderEvidenceSource", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "JsonTopologyProvider", CapturingProvider)
    monkeypatch.setattr(live_route_adapter, "IndependentJsonValidator", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "ExistingAnalogueActuator", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "load_route_follower_factory", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "LiveRouteAdapter", RefusingAdapter)

    arguments = {
        "dry_run": True,
        "topology_path": str(tmp_path / "geometry.json"),
        "max_capture_age_seconds": 120.0,
    }
    _run_live_route_adapter(FakeAdapterService(), FakeLifecycle(), arguments)
    assert captured["max_capture_wait_seconds"] == live_route_adapter.DEFAULT_MAX_CAPTURE_WAIT_SECONDS

    arguments["max_capture_wait_seconds"] = 7.5
    _run_live_route_adapter(FakeAdapterService(), FakeLifecycle(), arguments)
    assert captured["max_capture_wait_seconds"] == 7.5


def test_live_route_runtime_refreshes_attached_pid_before_adapter(monkeypatch, tmp_path):
    from ff7r_agent import live_route_adapter

    events = []

    class RefusingAdapter:
        def __init__(self, *args, **kwargs):
            pass

        def preflight(self):
            events.append(("preflight", None))
            return {"movement_authorized": False, "reason": "test_refusal"}

    monkeypatch.setattr(live_route_adapter, "AgentServiceTelemetrySource", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "AgentServiceRenderEvidenceSource", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "JsonTopologyProvider", lambda *args, **kwargs: object())
    monkeypatch.setattr(live_route_adapter, "IndependentJsonValidator", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "ExistingAnalogueActuator", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "load_route_follower_factory", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "LiveRouteAdapter", RefusingAdapter)

    result = _run_live_route_adapter(
        FakeAttachedService(),
        FakeAttachedLifecycle(events),
        {
            "dry_run": True,
            "topology_path": str(tmp_path / "geometry.json"),
        },
    )

    assert events == [("ensure_started", False), ("preflight", None)]
    assert result["lifecycle_attachment"] == {
        "attempted": True,
        "mode": "attached",
        "processes": {19608: "ff7rebirth_.exe", 23656: "ff7rebirth.exe"},
        "run_id": "attach-current",
        "pid": 19608,
        "attachment": "external_or_prior_task",
    }


def test_validated_cache_route_does_not_require_or_honor_caller_snapshot(monkeypatch, tmp_path):
    from ff7r_agent import live_route_adapter

    captured = {}

    class CapturingValidator:
        def __init__(self, *args):
            captured["args"] = args
            self.binding_evidence = {}

    class CapturingAdapter:
        def __init__(self, telemetry, topology, validator, *args, **kwargs):
            captured["validator"] = validator
            captured["topology"] = topology
            self.config = None

        def preflight(self):
            return {"movement_authorized": False, "reason": "test_refusal"}

    monkeypatch.setattr(live_route_adapter, "ValidatedTopologyCacheProvider", lambda *args, **kwargs: SimpleNamespace(_cache_invalidated=False))
    monkeypatch.setattr(live_route_adapter, "IndependentJsonValidator", CapturingValidator)
    monkeypatch.setattr(live_route_adapter, "AgentServiceTelemetrySource", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "AgentServiceRenderEvidenceSource", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "ExistingAnalogueActuator", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "load_route_follower_factory", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "LiveRouteAdapter", CapturingAdapter)

    bogus = tmp_path / "legacy-validator-snapshot.json"
    service = FakeAttachedService()
    result = _run_live_route_adapter(
        service,
        FakeAttachedLifecycle([]),
        {
            "dry_run": True,
            "topology_source": "validated_cache",
            "topology_cache_manifest_path": str(tmp_path / "manifest.json"),
            "topology_path": str(bogus),
            "validator_snapshot": str(bogus),
        },
    )

    assert captured["args"][0] is None
    assert captured["validator"].require_topology_binding is True
    assert result["caller_topology_path"] == str(bogus)
    assert result["caller_validator_snapshot"] == str(bogus)

    first_resources = service._validated_navigation_resources
    values = {"dry_run": True, "topology_source": "validated_cache",
              "topology_cache_manifest_path": str(tmp_path / "manifest.json")}
    _run_live_route_adapter(service, FakeAttachedLifecycle([]), values)
    assert captured["topology"] is first_resources[1]
    assert captured["validator"] is first_resources[2]
    first_resources[1]._cache_invalidated = True
    _run_live_route_adapter(service, FakeAttachedLifecycle([]), values)
    assert captured["topology"] is not first_resources[1]
    second_resources = service._validated_navigation_resources
    values["topology_cache_manifest_path"] = str(tmp_path / "other-manifest.json")
    _run_live_route_adapter(service, FakeAttachedLifecycle([]), values)
    assert captured["topology"] is not second_resources[1]
    assert captured["validator"] is not second_resources[2]


def test_scoped_partial_cache_opt_in_is_feature_validation_only(monkeypatch, tmp_path):
    from ff7r_agent import live_route_adapter

    provider_options = []
    adapter_constructed = []

    class CapturingProvider:
        def __init__(self, *args, **kwargs):
            provider_options.append(kwargs)
            self._cache_invalidated = False

    class RefusingAdapter:
        def __init__(self, *args, **kwargs):
            adapter_constructed.append(True)

        def preflight(self):
            return {"movement_authorized": False, "reason": "test_refusal"}

    monkeypatch.setattr(live_route_adapter, "ValidatedTopologyCacheProvider", CapturingProvider)
    monkeypatch.setattr(live_route_adapter, "IndependentJsonValidator", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "AgentServiceTelemetrySource", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "AgentServiceRenderEvidenceSource", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "ExistingAnalogueActuator", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "load_route_follower_factory", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "LiveRouteAdapter", RefusingAdapter)

    arguments = {
        "dry_run": True,
        "topology_source": "validated_cache",
        "topology_cache_manifest_path": str(tmp_path / "manifest.json"),
        "route_mode": "cached_topology",
        "route_target_position": {"x": 10.0, "y": 0.0, "z": 0.0},
        "allow_scoped_partial_topology": True,
    }
    monkeypatch.setenv("FF7R_RUNTIME_PROFILE", "NORMAL")
    with pytest.raises(ValueError, match="FEATURE_VALIDATION runtime"):
        _run_live_route_adapter(FakeAttachedService(), FakeAttachedLifecycle([]), arguments)
    assert provider_options == []
    assert adapter_constructed == []

    monkeypatch.setenv("FF7R_RUNTIME_PROFILE", "FEATURE_VALIDATION")
    _run_live_route_adapter(FakeAttachedService(), FakeAttachedLifecycle([]), arguments)
    assert len(provider_options) == 1
    assert provider_options[0]["allow_scoped_partial"] is True
    assert adapter_constructed == [True]



def test_runtime_session_commands_use_service_lease_scope(monkeypatch, tmp_path):
    from ff7r_agent import runtime_commands

    captured = {}

    def fake_open(seconds, **kwargs):
        captured["seconds"] = seconds
        captured.update(kwargs)
        return {"active": True}

    monkeypatch.setattr(runtime_commands, "request_session_open", fake_open)
    service = SimpleNamespace(
        _session_lease=SimpleNamespace(runtime_dir=tmp_path, owner_id="scoped-owner")
    )

    result = runtime_commands.invoke_runtime(
        service,
        FakeLifecycle(),
        None,
        "open_control_session",
        {"seconds": 900, "game_pid": 19608, "client_id": "mission-test", "model_id": "gpt-6.1-sol", "reasoning_effort": "high"},
    )

    assert result == {"active": True}
    assert captured == {
        "seconds": 900,
        "game_pid": 19608,
        "client_id": "mission-test",
        "model_id": "gpt-6.1-sol",
        "reasoning_effort": "high",
        "runtime_dir": tmp_path,
        "owner_id": "scoped-owner",
    }


class FakeStartup:
    def __init__(self) -> None:
        self.calls = []

    def continue_from_last_save(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True, "phase": "gameplay"}

    def load_last_save(self, **kwargs):
        self.calls.append(("load_last_save", kwargs))
        return {"ok": True, "mode": "load_last_save"}

    def restart_and_continue_from_last_save(self, **kwargs):
        self.calls.append(("restart_and_continue_from_last_save", kwargs))
        return {"ok": True, "mode": "restart_and_continue_from_last_save"}

    def start_and_continue_from_last_save_resumable(self, **kwargs):
        self.calls.append(("start_and_continue_from_last_save_resumable", kwargs))
        return {"ok": True, "mode": "start_and_continue_from_last_save_resumable"}

    def start_and_load_save_slot(self, **kwargs):
        self.calls.append(("start_and_load_save_slot", kwargs))
        return {"ok": True, "mode": "start_and_load_save_slot"}

    def start_new_game(self, **kwargs):
        self.calls.append(("start_new_game", kwargs))
        return {"ok": True, "mode": "start_new_game"}


class FakePipe:
    def __init__(self) -> None:
        self.writes = []

    def write(self, value):
        self.writes.append(value)

    def flush(self):
        return None


class FakeWorker:
    def __init__(self) -> None:
        self.stdin = FakePipe()
        self.stdout = object()
        self.pid = 48123
        self.terminated = False
        self.killed = False
        self.wait_calls = []

    def poll(self):
        return None if not self.terminated and not self.killed else -9

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return -9


class FakeReboundBridge:
    def __init__(self, identity) -> None:
        self.identity = dict(identity)
        self.calls = []
        self.closed = False
        self.alive = True
        self.active_job = None
        self.status_response = None

    def is_alive(self):
        return self.alive

    def active_async_job(self):
        return dict(self.active_job) if isinstance(self.active_job, dict) else None

    def call(self, operation, arguments=None):
        self.calls.append((operation, arguments or {}))
        if operation == "get_runtime_identity":
            return dict(self.identity)
        if operation == "get_agent_runtime_status":
            return {"ok": True, "worker_running": True}
        if operation in {
            "get_live_route_adapter_status",
            "get_topology_capture_status",
            "get_start_and_continue_from_last_save_status",
            "get_close_game_status",
        } and self.status_response is not None:
            return dict(self.status_response)
        if operation == "get_bootstrap_verification":
            return {"ok": True}
        if operation == "restart_agent_runtime":
            return {"ok": True}
        return {"ok": True, "id": 1, "result": {"sequence": 7}}

    def close(self):
        self.closed = True
        self.alive = False

class FakeAsyncWorker(FakeWorker):
    def __init__(self, stdout=None) -> None:
        super().__init__()
        self.stdout = stdout or io.StringIO(
            '{"ok":true,"id":1,"result":{"preflight":{"movement_authorized":false}}}\n'
        )


class BlockingStdout:
    def __init__(self, line: str) -> None:
        self.line = line
        self.release = threading.Event()
        self.readline_calls = 0

    def readline(self):
        self.readline_calls += 1
        self.release.wait(timeout=2.0)
        return self.line


class FakeBroker:
    def __init__(self) -> None:
        self.process = FakeWorker()
        self.port = 55338
        self.neutralise_calls = 0
        self.close_calls = 0

    def status(self):
        return {
            "ok": True,
            "active": False,
            "device_active": False,
            "process_alive": True,
            "listener_alive": True,
        }

    def neutralise(self):
        self.neutralise_calls += 1

    def close(self):
        self.close_calls += 1


def proven_live_route_arguments(**extra):
    values = {
        "dry_run": False,
        "topology_path": "geometry.json",
        "route_mode": "ordinary_multi_polygon",
        **{
            key: value
            for key, value in PROVEN_LIVE_CONTROLLER_POLICY.items()
            if key != "stall_timeout_s"
        },
    }
    values.update(extra)
    return values


def test_real_live_route_rejects_missing_controller_policy_before_preflight():
    with pytest.raises(ValueError, match="explicit proven controller policy"):
        _run_live_route_adapter(
            FakeAttachedService(),
            FakeAttachedLifecycle([]),
            {"dry_run": False, "topology_path": "geometry.json"},
        )


def test_real_live_route_rejects_legacy_controller_policy_before_preflight():
    arguments = proven_live_route_arguments()
    arguments["normal_stick_magnitude"] = 0.35
    with pytest.raises(ValueError, match="proven controller policy"):
        _run_live_route_adapter(
            FakeAttachedService(),
            FakeAttachedLifecycle([]),
            arguments,
        )


@pytest.mark.parametrize("normal,profile", [(0.6, NORMAL_06_TRIAL_PROFILE), (0.8, NORMAL_08_TRIAL_PROFILE), (1.0, "proven")])
def test_real_live_route_records_effective_controller_policy(monkeypatch, tmp_path, normal, profile):
    monkeypatch.setenv("FF7R_RUNTIME_PROFILE", "FEATURE_VALIDATION")
    from ff7r_agent import live_route_adapter

    class RefusingAdapter:
        def __init__(self, *args, config=None, **kwargs):
            self.config = config
            self.follower = SimpleNamespace(
                config=SimpleNamespace(stall_timeout_s=5.0, final_arrival_radius_m=0.75)
            )

        def preflight(self):
            return {"movement_authorized": False, "reason": "test_refusal"}

    monkeypatch.setattr(live_route_adapter, "ValidatedTopologyCacheProvider", lambda *args, **kwargs: object())
    monkeypatch.setattr(live_route_adapter, "IndependentJsonValidator", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "AgentServiceTelemetrySource", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "AgentServiceRenderEvidenceSource", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "ExistingAnalogueActuator", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "load_route_follower_factory", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "LiveRouteAdapter", RefusingAdapter)

    result = _run_live_route_adapter(
        FakeAttachedService(),
        FakeAttachedLifecycle([]),
        proven_live_route_arguments(
            topology_cache_manifest_path=str(tmp_path / "manifest.json"),
            normal_stick_magnitude=normal,
            controller_policy_profile=profile,
        ),
    )

    assert result["adapter_config"]["normal_stick_magnitude"] == normal
    assert result["adapter_config"]["control_period_seconds"] == 0.3
    assert result["adapter_config"]["final_arrival_radius_m"] == 0.75
    assert result["adapter_config"]["waypoint_vertical_tolerance_m"] == 1.5
    assert result["controller_policy"]["follower_stall_timeout_s"] == 5.0
    assert result["controller_policy"]["follower_final_arrival_radius_m"] == 0.75
    assert result["controller_policy"]["controller_policy_profile"] == profile


def install_clean_worker(supervisor: RuntimeSupervisor) -> FakeWorker:
    worker = FakeWorker()
    response_id = supervisor._request_id + 1
    worker.stdout = io.StringIO(
        json.dumps({"ok": True, "id": response_id, "result": {"ok": True}}) + "\n"
    )
    supervisor._worker = worker
    supervisor._generation += 1
    return worker


def test_runtime_command_registry_preserves_old_mcp_name_and_forwards_arguments():
    service = FakeService()

    result = invoke_runtime(
        service,
        FakeLifecycle(),
        FakeStartup(),
        "navigate_next_segment",
        {"maximum_distance_m": 3.0, "target_id": "test-target"},
    )

    assert result["ok"] is True
    assert service.calls == [(
        "navigate_next_segment",
        {"maximum_distance_m": 3.0, "target_id": "test-target"},
    )]


def test_runtime_command_registry_rejects_arbitrary_service_methods():
    with pytest.raises(ValueError, match="unsupported runtime operation"):
        invoke_runtime(
            FakeService(), FakeLifecycle(), FakeStartup(), "hidden_helper", {}
        )


def test_focus_game_window_is_rejected_when_background_input_is_enabled():
    focus_calls = []
    service = SimpleNamespace(_background_input_enabled=lambda: True)
    lifecycle = SimpleNamespace(
        environment=SimpleNamespace(
            focus_game_window=lambda _pid=None: focus_calls.append(_pid) or {"ok": True}
        )
    )

    with pytest.raises(ValueError, match="background-input mode"):
        invoke_runtime(service, lifecycle, None, "focus_game_window", {"pid": 123})

    assert focus_calls == []


@pytest.mark.parametrize(
    "output_name", ["topology_path", "probe_path", "timing_path", "status_path"]
)
def test_real_topology_capture_rejects_native_output_overrides_before_work(
    monkeypatch, tmp_path, output_name
):
    from ff7r_agent import live_route_adapter

    events = []
    provider_constructions = []
    service = SimpleNamespace(
        input=FakeAttachedInput(),
        wait_for_settled_gameplay=lambda _timeout: events.append("settle"),
        stop=lambda: events.append("stop"),
    )
    lifecycle = SimpleNamespace(
        environment=SimpleNamespace(
            game_process_ids=lambda: events.append("processes") or {1: "ff7rebirth.exe"}
        ),
        ensure_started=lambda **_kwargs: events.append("attach"),
    )
    monkeypatch.setattr(
        live_route_adapter,
        "JsonTopologyProvider",
        lambda *args, **kwargs: provider_constructions.append((args, kwargs)),
    )

    with pytest.raises(ValueError, match=f"custom {output_name}"):
        invoke_runtime(
            service,
            lifecycle,
            None,
            "capture_topology_cache",
            {
                output_name: str(tmp_path / f"custom-{output_name}.json"),
                "topology_cache_manifest_path": str(tmp_path / "custom-manifest.json"),
            },
        )

    assert events == []
    assert provider_constructions == []


def test_real_topology_capture_allows_custom_cache_manifest_path(monkeypatch, tmp_path):
    from ff7r_agent import live_route_adapter

    events = []

    class CapturingProvider:
        def __init__(self, *args, **kwargs):
            events.append("provider_constructed")
            self.request_id = "test-capture"
            self.capture_status_timeline = []

    class RefusingAdapter:
        def __init__(self, *args, **kwargs):
            pass

        def preflight(self, *, require_lease):
            events.append("preflight")
            return {"movement_authorized": False, "reason": "test_refusal"}

    monkeypatch.setattr(live_route_adapter, "JsonTopologyProvider", CapturingProvider)
    monkeypatch.setattr(live_route_adapter, "AgentServiceTelemetrySource", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "AgentServiceRenderEvidenceSource", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "IndependentJsonValidator", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "load_route_follower_factory", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "ExistingAnalogueActuator", lambda *args: object())
    monkeypatch.setattr(live_route_adapter, "LiveRouteAdapter", RefusingAdapter)

    result = invoke_runtime(
        SimpleNamespace(
            input=FakeAttachedInput(),
            wait_for_settled_gameplay=lambda _timeout: events.append("settle") or {"ready": True},
            stop=lambda: events.append("stop"),
        ),
        SimpleNamespace(environment=None),
        None,
        "capture_topology_cache",
        {
            "topology_cache_manifest_path": str(tmp_path / "custom-manifest.json"),
            "native_probe_sha256": "A" * 64,
            "validator_source_sha256": "B" * 64,
        },
    )

    assert result["cache_admitted"] is False
    assert result["preflight"]["reason"] == "test_refusal"
    assert events == ["provider_constructed", "settle", "preflight"]


def test_runtime_registry_keeps_startup_inside_reloadable_worker():
    startup = FakeStartup()

    result = invoke_runtime(
        FakeService(),
        FakeLifecycle(),
        startup,
        "continue_from_last_save",
        {"session_seconds": 600},
    )

    assert result == {"ok": True, "phase": "gameplay"}
    assert startup.calls == [{"session_seconds": 600}]


@pytest.mark.parametrize(
    ("operation", "expected_mode"),
    [
        ("load_last_save", "load_last_save"),
        (
            "restart_and_continue_from_last_save",
            "restart_and_continue_from_last_save",
        ),
        (
            "run_start_and_continue_from_last_save",
            "start_and_continue_from_last_save_resumable",
        ),
        ("start_and_load_save_slot", "start_and_load_save_slot"),
        ("run_start_and_load_save_slot", "start_and_load_save_slot"),
        ("start_new_game", "start_new_game"),
        ("run_start_new_game", "start_new_game"),
    ],
)
def test_runtime_registry_exposes_new_save_lifecycle_operations(
    operation, expected_mode
):
    startup = FakeStartup()

    result = invoke_runtime(
        FakeService(), FakeLifecycle(), startup, operation, {"timeout": 12}
    )

    assert result == {"ok": True, "mode": expected_mode}
    expected_call = {
        "run_start_and_continue_from_last_save": "start_and_continue_from_last_save_resumable",
        "run_start_and_load_save_slot": "start_and_load_save_slot",
        "run_start_new_game": "start_new_game",
    }.get(operation, operation)
    assert startup.calls == [(expected_call, {"timeout": 12})]


def test_runtime_registry_keeps_lifecycle_status_available():
    result = invoke_runtime(
        FakeService(), FakeLifecycle(), FakeStartup(), "get_lifecycle_status", {}
    )
    assert result == {"active": True}


def test_intentional_restart_disarms_crash_watcher_before_closing_the_game():
    service, startup = FakeService(), FakeStartup()
    service._queen_blood_runs = object()
    service.disarm_working_save_recovery = lambda: startup.calls.append(("disarm", {}))
    invoke_runtime(service, FakeLifecycle(), startup, "restart_and_continue_from_last_save", {})
    assert startup.calls == [("disarm", {}), ("restart_and_continue_from_last_save", {})]


def test_runtime_request_timeout_terminates_worker_without_retry(monkeypatch):
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeWorker()
    supervisor._worker = worker

    def timed_out(*args, **kwargs):
        raise RuntimeBridgeError("runtime worker request timed out")

    monkeypatch.setattr(supervisor, "_readline_with_timeout", timed_out)

    with pytest.raises(RuntimeBridgeError, match="worker terminated; request was not retried"):
        supervisor.call("move_stick", {"x": 0.0, "y": 1.0})

    assert len(worker.stdin.writes) == 1
    assert worker.terminated is True
    assert supervisor._worker is None


def test_async_route_request_can_be_polled_without_using_short_request_timeout():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker

    started = supervisor.call(
        "start_live_route_adapter",
        {"dry_run": True, "topology_path": "geometry.json"},
    )

    assert started["ok"] is True
    assert started["operation"] == "run_live_route_adapter"
    assert started["state"] in {"running", "completed"}
    job_id = started["job_id"]
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        status = supervisor.call("get_live_route_adapter_status", {"job_id": job_id})
        if status["state"] == "completed":
            break
        time.sleep(0.01)
    else:
        pytest.fail("asynchronous route request did not complete")

    assert status["response"]["result"]["preflight"]["movement_authorized"] is False
    assert '"operation":"run_live_route_adapter"' in worker.stdin.writes[0]


def test_async_startup_request_can_be_polled_without_using_short_request_timeout():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker

    started = supervisor.call(
        "start_and_continue_from_last_save_async",
        {"readiness_timeout_seconds": 120.0},
    )

    assert started["ok"] is True
    assert started["operation"] == "run_start_and_continue_from_last_save"
    assert started["state"] in {"running", "completed"}
    job_id = started["job_id"]
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        status = supervisor.call(
            "get_start_and_continue_from_last_save_status",
            {"job_id": job_id},
        )
        if status["state"] == "completed":
            break
        time.sleep(0.01)
    else:
        pytest.fail("asynchronous startup request did not complete")

    assert status["response"]["result"]["preflight"]["movement_authorized"] is False
    assert '"operation":"run_start_and_continue_from_last_save"' in worker.stdin.writes[0]


@pytest.mark.parametrize(("operation", "error", "use_operation"), [
    ("load_save_slot", "load_save_slot_requires_async", "load_save_slot_async"),
    ("start_and_load_save_slot", "start_and_load_save_slot_requires_async",
     "start_and_load_save_slot_async"),
    ("run_start_and_load_save_slot", "start_and_load_save_slot_requires_async",
     "start_and_load_save_slot_async"),
    ("start_new_game", "start_new_game_requires_async", "start_new_game_async"),
    ("run_start_new_game", "start_new_game_requires_async", "start_new_game_async"),
    ("save_game_progress", "save_game_progress_requires_async", "save_game_progress_async"),
    ("run_recover_working_save", "run_recover_working_save_requires_async", "recover_working_save_async"),
])
def test_sync_save_slot_loads_are_refused_before_worker_write(operation, error, use_operation):
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker

    rejected = supervisor.call(operation, {"slot": 8})

    assert rejected["ok"] is False
    assert rejected["error"] == error
    assert rejected["use_operation"] == use_operation
    assert len(worker.stdin.writes) == 0


@pytest.mark.parametrize(("start", "status", "work", "busy"), [
    ("load_save_slot_async", "get_load_save_slot_status", "load_save_slot",
     "async_load_slot_in_progress"),
    ("start_and_load_save_slot_async", "get_start_and_load_save_slot_status",
     "run_start_and_load_save_slot", "async_slot_startup_in_progress"),
    ("start_new_game_async", "get_start_new_game_status", "run_start_new_game",
     "async_new_game_in_progress"),
    ("save_game_progress_async", "get_save_game_progress_status", "save_game_progress", "async_save_in_progress"),
    ("recover_working_save_async", "get_recover_working_save_status", "run_recover_working_save", "async_recovery_in_progress"),
])
def test_async_save_slot_load_outlives_the_short_request_timeout(start, status, work, busy):
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout(
        '{"ok":true,"id":1,"result":{"ok":true,"phase":"gameplay_ready","state":"loaded"}}\n'
    )
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker

    started = supervisor.call(start, {"slot": 8, "execute": True})
    assert started["ok"] is True
    assert started["operation"] == work and started["state"] == "running"
    message = json.loads(worker.stdin.writes[0])
    assert message["operation"] == work
    assert message["arguments"] == {"slot": 8, "execute": True}

    time.sleep(0.55)
    rejected = supervisor.call(start, {"slot": 8, "execute": True})
    assert rejected["error"] == busy
    assert rejected["allowed_operations"] == [status, "say_on_stream", "stop_all_input"]
    assert supervisor.call(status, {"job_id": started["job_id"]})["state"] == "running"
    assert worker.terminated is False and len(worker.stdin.writes) == 1

    stdout.release.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        polled = supervisor.call(status, {"job_id": started["job_id"]})
        if polled["state"] == "completed":
            break
        time.sleep(0.01)
    else:
        pytest.fail("asynchronous save slot load did not complete")
    assert polled["response"]["result"]["state"] == "loaded"


def test_model_run_async_alias_uses_the_save_load_job_and_cannot_override_its_slot():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout('{"ok":true,"id":1,"result":{"ok":true,"state":"loaded"}}\n')
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker
    with pytest.raises(RuntimeBridgeError, match="registered slot"):
        supervisor.call("start_model_run_async", {"slot": 14})
    assert not worker.stdin.writes
    started = supervisor.call("start_model_run_async", {"session_seconds": 1800})
    request = json.loads(worker.stdin.writes[0])
    assert request["operation"] == "run_start_and_load_save_slot"
    assert request["arguments"] == {"session_seconds": 1800, "model_run": True}
    assert supervisor.call("get_start_model_run_status", {"job_id": started["job_id"]})["state"] == "running"
    stdout.release.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        result = supervisor.call("get_start_model_run_status", {"job_id": started["job_id"]})
        if result["state"] == "completed":
            break
        time.sleep(0.01)
    assert result["response"]["result"]["state"] == "loaded"


def test_second_async_startup_request_is_rejected_without_worker_write():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout(
        '{"ok":true,"id":1,"result":{"phase":"gameplay_loading"}}\n'
    )
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker
    started = supervisor.call(
        "start_and_continue_from_last_save_async",
        {"readiness_timeout_seconds": 120.0},
    )
    assert started["state"] == "running"
    try:
        rejected = supervisor.call(
            "start_and_continue_from_last_save_async",
            {"readiness_timeout_seconds": 120.0},
        )
        assert rejected["ok"] is False
        assert rejected["error"] == "async_startup_in_progress"
        assert rejected["allowed_operations"] == [
            "get_start_and_continue_from_last_save_status",
            "say_on_stream",
            "stop_all_input",
        ]
        assert len(worker.stdin.writes) == 1
        assert stdout.readline_calls == 1
    finally:
        stdout.release.set()


def _recording_lock_cycle(root: Path) -> None:
    recorder = HumanTraversalRecorder(root)
    with recorder.sampler_guard():
        with pytest.raises(RecordingError, match="current process"):
            with recorder.sampler_guard():
                pass
    assert not recorder.sampler_lock_path.exists()


def _async_poll_cycle() -> None:
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker
    try:
        started = supervisor.call(
            "start_live_route_adapter",
            {"dry_run": True, "topology_path": "geometry.json"},
        )
        status = started
        deadline = time.monotonic() + 1.0
        while status.get("state") != "completed" and time.monotonic() < deadline:
            status = supervisor.call(
                "get_live_route_adapter_status",
                {"job_id": started["job_id"]},
            )
            if status.get("state") != "completed":
                time.sleep(0.01)
        assert status["state"] == "completed"
        assert status["response"]["result"]["preflight"]["movement_authorized"] is False
    finally:
        supervisor.close()
    assert supervisor._worker is None


@pytest.mark.parametrize("order", [("recording", "async"), ("async", "recording")])
def test_recording_and_async_polling_are_order_independent(tmp_path: Path, order: tuple[str, str]):
    for index, operation in enumerate(order):
        if operation == "recording":
            _recording_lock_cycle(tmp_path / f"{order[0]}-{index}")
        else:
            _async_poll_cycle()


def test_recording_then_async_polling_repeatedly_leaves_no_lock_or_worker(tmp_path: Path):
    for index in range(5):
        _recording_lock_cycle(tmp_path / f"iteration-{index}")
        _async_poll_cycle()


def test_async_real_route_rejects_missing_controller_policy_without_worker_write():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker

    rejected = supervisor.call(
        "start_live_route_adapter",
        {"dry_run": False, "topology_path": "geometry.json"},
    )

    assert rejected["ok"] is False
    assert rejected["error"] == "live_controller_policy_required"
    assert rejected["operation"] == "start_live_route_adapter"
    assert len(worker.stdin.writes) == 0


def test_async_real_route_reports_proven_controller_policy_before_worker_route():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker

    started = supervisor.call(
        "start_live_route_adapter",
        proven_live_route_arguments(),
    )

    assert started["ok"] is True
    assert started["controller_policy"]["normal_stick_magnitude"] == 1.0
    assert started["controller_policy"]["effective_normal_duration_ms"] == 300
    assert started["controller_policy"]["approach_stick_magnitude"] == 0.5
    assert started["controller_policy"]["effective_approach_duration_ms"] == 250
    assert started["controller_policy"]["final_stick_magnitude"] == 0.5
    assert started["controller_policy"]["effective_final_duration_ms"] == 250
    assert started["controller_policy"]["minimum_effective_stick_magnitude"] == 0.5
    assert started["controller_policy"]["final_arrival_radius_m"] == 0.75
    assert started["controller_policy"]["stall_timeout_s"] == 5.0
    assert len(worker.stdin.writes) == 1


def make_blocked_async_supervisor():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout(
        '{"ok":true,"id":1,"result":{"preflight":{"movement_authorized":false}}}\n'
    )
    worker = FakeAsyncWorker(stdout)
    broker = FakeBroker()
    supervisor._worker = worker
    supervisor._broker = broker
    started = supervisor.call(
        "start_live_route_adapter",
        {"dry_run": True, "topology_path": "geometry.json"},
    )
    assert started["state"] == "running"
    return supervisor, worker, stdout, broker, started["job_id"]


@pytest.mark.parametrize(("start", "status", "arguments"), [
    ("start_live_route_adapter", "get_live_route_adapter_status", {"dry_run": True, "topology_path": "geometry.json"}),
    ("travel_to_map_spot_async", "get_travel_to_map_spot_status", {"spot": "Kalm", "execute": True}),
    ("load_save_slot_async", "get_load_save_slot_status", {"slot": 13, "execute": True}),
    ("edit_queens_blood_deck_async", "get_edit_queens_blood_deck_status", {"cards": list(range(15))}),
    ("finish_conversation_async", "get_finish_conversation_status", {}),
    ("talk_to_npc_async", "get_talk_to_npc_status", {}),
    ("mulligan_cards_async", "get_mulligan_cards_status", {"card_indices": [0]}),
])
def test_gameplay_commentary_can_run_during_input_jobs_without_touching_the_worker(
        tmp_path, monkeypatch, start, status, arguments):
    from datetime import datetime, timezone

    events = tmp_path / "public-stream-events"
    monkeypatch.setenv("FF7R_PUBLIC_STREAM_EVENT_DIR", str(events))
    (tmp_path / "public-stream-live.json").write_text(json.dumps({
        "version": 1, "streamStartedAt": "2026-10-07T17:00:00+00:00",
        "at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout('{"ok":true,"id":1,"result":{"ok":true,"outcome":"finished"}}\n')
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker
    started = supervisor.call(start, arguments)
    assert started["state"] == "running"
    worker_request = list(worker.stdin.writes)
    try:
        def forbidden(*args, **kwargs):
            pytest.fail("speech must not trigger worker, input broker or source rebind readiness")

        monkeypatch.setattr(supervisor, "_ensure_live_runtime_ready_locked", forbidden)
        monkeypatch.setattr(supervisor, "_ensure_worker_locked", forbidden)
        monkeypatch.setattr(supervisor, "_terminate_worker_locked", forbidden)
        line = "There's a staircase ahead. I'll take it up to the square."
        speech = supervisor.call("say_on_stream", {"text": line, "tone": "calm"})
        assert speech["ok"] and speech["result"]["ok"]
        assert speech["result"]["input_sent"] is False
        assert supervisor.call(status, {"job_id": started["job_id"]})["state"] == "running"
        rejected = supervisor.call("tap_button", {"button": "confirm"})
        assert not rejected["ok"] and "say_on_stream" in rejected["allowed_operations"]
        assert worker.stdin.writes == worker_request and not worker.terminated
        [file] = list(events.glob("*.json"))
        assert json.loads(file.read_text(encoding="utf-8"))["text"] == line
    finally:
        stdout.release.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        final = supervisor.call(status, {"job_id": started["job_id"]})
        if final["state"] == "completed":
            break
        time.sleep(0.01)
    else:
        pytest.fail("narrated job did not complete")
    assert final["response"]["result"]["outcome"] == "finished"


def test_speech_never_requires_a_worker_or_forwards_to_a_rebound_game_runtime(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from ff7r_agent.gameplay_narration import AgentSpeechPublisher

    events = tmp_path / "events"
    monkeypatch.setenv("FF7R_PUBLIC_STREAM_EVENT_DIR", str(events))
    (tmp_path / "public-stream-rehearsal.json").write_text(json.dumps({
        "version": 1, "at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    supervisor = RuntimeSupervisor()
    now = [0.0]
    supervisor._speech = AgentSpeechPublisher(events, monotonic=lambda: now[0])

    def forbidden(*args, **kwargs):
        pytest.fail("speech must not call the game runtime")

    supervisor._bridge_process = SimpleNamespace(call=forbidden)
    monkeypatch.setattr(supervisor, "_ensure_live_runtime_ready_locked", forbidden)
    speech = supervisor.call("say_on_stream", {"text": "Cloud's health is low. I'll look for a bench."})
    assert speech["result"]["ok"] and speech["result"]["rehearsal"]
    # Input-worker replacement must not reset narration pacing.
    supervisor._worker = FakeWorker()
    now[0] = 0.2
    limited = supervisor.call("say_on_stream", {"text": "There's the bench beside the inn."})
    assert limited["result"]["reason"] == "rate_limited"
    assert limited["result"]["retry_after_seconds"] == 8
    assert supervisor._worker.stdin.writes == [] and supervisor._broker is None
    supervisor._closed = True
    with pytest.raises(RuntimeBridgeError, match="closed"):
        supervisor.call("say_on_stream", {"text": "Time to head to the square."})


def test_failed_speech_during_a_job_leaves_its_input_and_state_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("FF7R_PUBLIC_STREAM_EVENT_DIR", str(tmp_path / "events"))
    supervisor, worker, stdout, broker, job_id = make_blocked_async_supervisor()
    try:
        for text, reason in (("I'll head to the square.", "not_live"),
                             ("The harness telemetry is ready.", "invalid_text")):
            failed = supervisor.call("say_on_stream", {"text": text})
            assert failed["ok"] and failed["result"]["reason"] == reason
            assert failed["result"]["input_sent"] is False
            assert supervisor.call("get_live_route_adapter_status", {"job_id": job_id})["state"] == "running"
        assert not (tmp_path / "events").exists()
        assert len(worker.stdin.writes) == 1 and not worker.terminated
    finally:
        stdout.release.set()


def test_second_async_route_start_is_rejected_without_worker_write():
    supervisor, worker, stdout, broker, job_id = make_blocked_async_supervisor()
    try:
        rejected = supervisor.call(
            "start_live_route_adapter",
            {"dry_run": True, "topology_path": "other.json"},
        )
        assert rejected == {
            "ok": False,
            "error": "async_route_in_progress",
            "error_type": "RuntimeBridgeError",
            "operation": "start_live_route_adapter",
            "job_id": job_id,
            "allowed_operations": ["get_live_route_adapter_status", "get_queens_blood_preparation_context", "prepare_queens_blood_deck", "say_on_stream", "stop_all_input"],
        }
        assert len(worker.stdin.writes) == 1
        assert stdout.readline_calls == 1
    finally:
        stdout.release.set()


@pytest.mark.parametrize("operation", ["observe_game", "recover_stale_lifecycle"])
def test_ordinary_worker_call_is_rejected_locally_during_async_route(operation):
    supervisor, worker, stdout, broker, job_id = make_blocked_async_supervisor()
    try:
        rejected = supervisor.call(operation, {})
        assert rejected["error"] == "async_route_in_progress"
        assert rejected["job_id"] == job_id
        assert len(worker.stdin.writes) == 1
        assert stdout.readline_calls == 1
    finally:
        stdout.release.set()


def test_bootstrap_worker_reads_are_rejected_locally_during_async_route():
    supervisor, worker, stdout, broker, job_id = make_blocked_async_supervisor()
    try:
        with pytest.raises(RuntimeBridgeError, match="async_route_in_progress"):
            supervisor.bootstrap_verification()
        assert len(worker.stdin.writes) == 1
        assert stdout.readline_calls == 1
    finally:
        stdout.release.set()


def test_async_status_polling_never_reads_worker_stdout():
    supervisor, worker, stdout, broker, job_id = make_blocked_async_supervisor()
    try:
        for _ in range(5):
            status = supervisor.call("get_live_route_adapter_status", {"job_id": job_id})
            assert status["state"] == "running"
        assert stdout.readline_calls == 1
        assert len(worker.stdin.writes) == 1
        stdout.release.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status = supervisor.call("get_live_route_adapter_status", {"job_id": job_id})
            if status["state"] == "completed":
                break
            time.sleep(0.01)
        assert status["state"] == "completed"
        assert status["response"]["id"] == 1
    finally:
        stdout.release.set()


def test_compact_async_status_exposes_bounded_summary_without_full_response():
    supervisor, worker, stdout, broker, job_id = make_blocked_async_supervisor()
    try:
        stdout.release.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            status = supervisor.call(
                "get_live_route_adapter_status",
                {"job_id": job_id, "include_response": False},
            )
            if status["state"] == "completed":
                break
            time.sleep(0.01)
        assert status["state"] == "completed"
        assert status["response_available"] is True
        assert "response" not in status
        assert status["response_summary"]["response_ok"] is True
        assert status["response_summary"]["evidence_retained"] is True
    finally:
        stdout.release.set()


def test_large_async_result_uses_hash_bound_artifact_and_full_retrieval(tmp_path: Path):
    full_response = {
        "ok": True,
        "id": 1,
        "result": {
            "state": "ARRIVED",
            "timeline": [{"event": "control_cycle", "sequence": 7}],
            "forensic_payload": "evidence",
        },
    }
    raw = json.dumps(full_response, separators=(",", ":")).encode("utf-8")
    artifact_path = tmp_path / "route-result.json"
    artifact_path.write_bytes(raw)
    artifact = {
        "schema": "ff7r-runtime-result-v1",
        "path": str(artifact_path),
        "sha256": __import__("hashlib").sha256(raw).hexdigest(),
        "bytes": len(raw),
        "operation": "run_live_route_adapter",
        "response_id": 1,
        # The Windows venv launcher PID (the fake worker PID below) can differ
        # from the child Python PID that wrote the retained artifact.
        "worker_pid": 90001,
    }
    envelope = {
        "ok": True,
        "id": 1,
        "result": {"artifact": artifact, "response_available": True},
    }
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    supervisor._worker = FakeAsyncWorker(io.StringIO(json.dumps(envelope) + "\n"))
    supervisor._worker_identity = {"pid": 90001}

    started = supervisor.call(
        "start_live_route_adapter",
        {"dry_run": True, "topology_path": "geometry.json"},
    )
    deadline = time.monotonic() + 1.0
    compact = supervisor.call(
        "get_live_route_adapter_status",
        {"job_id": started["job_id"], "include_response": False},
    )
    while compact.get("state") != "completed" and time.monotonic() < deadline:
        compact = supervisor.call(
            "get_live_route_adapter_status",
            {"job_id": started["job_id"], "include_response": False},
        )
        if compact.get("state") != "completed":
            time.sleep(0.01)

    assert compact["state"] == "completed"
    assert compact["response_available"] is True
    assert "response" not in compact
    assert compact["response_summary"]["evidence_artifact"]["sha256"] == artifact["sha256"]
    assert supervisor._async_request["response"] is None

    full = supervisor.call(
        "get_live_route_adapter_status",
        {"job_id": started["job_id"], "include_response": True},
    )
    assert full["response"] == full_response
    supervisor.close()


def test_async_topology_capture_status_is_bridge_local_and_request_bound():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout(
        '{"ok":true,"id":1,"result":{"cache_admitted":true}}\n'
    )
    worker = FakeAsyncWorker(stdout)
    broker = FakeBroker()
    supervisor._worker = worker
    supervisor._broker = broker
    with tempfile.TemporaryDirectory() as directory:
        status_path = str(Path(directory) / "status.json")
        started = supervisor.call(
            "start_topology_capture",
            {"status_path": status_path, "topology_path": "geometry.json"},
        )
        assert started["ok"] is True
        assert started["operation"] == "capture_topology_cache"
        message = json.loads(worker.stdin.writes[0])
        request_id = message["arguments"]["capture_request_id"]
        Path(status_path).write_text(
            json.dumps({"request_id": "other-request", "state": "geometry_extracting"}),
            encoding="utf-8",
        )
        status = supervisor.call(
            "get_topology_capture_status", {"job_id": started["job_id"]}
        )
        assert "capture_status" not in status
        Path(status_path).write_text(
            json.dumps({"request_id": request_id, "state": "geometry_extracting"}),
            encoding="utf-8",
        )
        status = supervisor.call(
            "get_topology_capture_status", {"job_id": started["job_id"]}
        )
        assert status["capture_status"]["request_id"] == request_id
        assert status["capture_status"]["state"] == "geometry_extracting"
        assert len(worker.stdin.writes) == 1
        assert stdout.readline_calls == 1
        stdout.release.set()


def test_second_capture_and_ordinary_worker_call_are_rejected_locally():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout(
        '{"ok":true,"id":1,"result":{}}\n'
    )
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker
    supervisor._broker = FakeBroker()
    started = supervisor.call(
        "start_topology_capture",
        {"status_path": "status.json", "topology_path": "geometry.json"},
    )
    try:
        rejected = supervisor.call("start_topology_capture", {"topology_path": "other.json"})
        assert rejected["error"] == "async_topology_capture_in_progress"
        assert rejected["job_id"] == started["job_id"]
        ordinary = supervisor.call("observe_game", {})
        assert ordinary["error"] == "async_topology_capture_in_progress"
        assert len(worker.stdin.writes) == 1
        assert stdout.readline_calls == 1
    finally:
        stdout.release.set()


def test_stop_all_input_cancels_async_topology_capture_and_neutralizes(monkeypatch):
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout('{"ok":true,"id":1,"result":{}}\n')
    worker = FakeAsyncWorker(stdout)
    broker = FakeBroker()
    supervisor._worker = worker
    supervisor._broker = broker
    started = supervisor.call(
        "start_topology_capture",
        {"status_path": "status.json", "topology_path": "geometry.json"},
    )
    monkeypatch.setattr(
        supervisor,
        "_start_worker_locked",
        lambda: install_clean_worker(supervisor) and supervisor.status_locked(),
    )
    try:
        stopped = supervisor.call("stop_all_input", {})
        assert stopped["result"]["async_capture_cancelled"] is True
        assert stopped["result"]["cancelled_job_id"] == started["job_id"]
        assert worker.terminated is True
        assert broker.neutralise_calls >= 1
    finally:
        stdout.release.set()


@pytest.mark.parametrize(("start", "cancelled"), [
    ("save_game_progress_async", "async_save_cancelled"),
    ("recover_working_save_async", "async_recovery_cancelled"),
])
def test_stop_all_input_cancels_save_and_recovery_workers_and_neutralizes(monkeypatch, start, cancelled):
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout('{"ok":true,"id":1,"result":{}}\n')
    worker, broker = FakeAsyncWorker(stdout), FakeBroker()
    supervisor._worker, supervisor._broker = worker, broker
    started = supervisor.call(start, {})
    monkeypatch.setattr(supervisor, "_start_worker_locked",
                        lambda: install_clean_worker(supervisor) and supervisor.status_locked())
    try:
        stopped = supervisor.call("stop_all_input", {})
        assert stopped["result"][cancelled] is True
        assert stopped["result"]["cancelled_job_id"] == started["job_id"]
        assert worker.terminated and broker.neutralise_calls >= 1
    finally:
        stdout.release.set()


def test_stop_all_input_cancels_async_route_and_leaves_broker_neutral(monkeypatch):
    supervisor, worker, stdout, broker, job_id = make_blocked_async_supervisor()
    monkeypatch.setattr(
        supervisor,
        "_start_worker_locked",
        lambda: install_clean_worker(supervisor) and supervisor.status_locked(),
    )
    try:
        stopped = supervisor.call("stop_all_input", {})
        assert stopped["result"]["async_route_cancelled"] is True
        assert stopped["result"]["cancelled_job_id"] == job_id
        assert worker.terminated is True
        assert broker.neutralise_calls >= 1
        assert supervisor._worker is not worker
        assert len(supervisor._worker.stdin.writes) == 1
        assert json.loads(supervisor._worker.stdin.writes[0])["operation"] == "stop_all_input"
    finally:
        stdout.release.set()


def test_restart_during_async_route_fails_closed_and_neutralizes():
    supervisor, worker, stdout, broker, job_id = make_blocked_async_supervisor()
    try:
        with pytest.raises(RuntimeBridgeError, match="async_route_in_progress"):
            supervisor.restart()
        assert worker.terminated is True
        assert broker.neutralise_calls >= 1
        assert supervisor._worker is None
    finally:
        stdout.release.set()


def test_shutdown_during_async_route_fails_closed_and_neutralizes():
    supervisor, worker, stdout, broker, _ = make_blocked_async_supervisor()
    try:
        supervisor.close()
        assert worker.terminated is True
        assert broker.neutralise_calls >= 1
        assert broker.close_calls == 1
        assert supervisor._closed is True
    finally:
        stdout.release.set()


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not-json\n",
        '{"ok":true,"id":999,"result":{}}\n',
    ],
)
def test_async_response_transport_failures_fail_job_and_reset_worker(monkeypatch, payload):
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker(io.StringIO(payload))
    broker = FakeBroker()
    supervisor._worker = worker
    supervisor._broker = broker
    started = supervisor.call(
        "start_live_route_adapter",
        {"dry_run": True, "topology_path": "geometry.json"},
    )
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        status = supervisor.call("get_live_route_adapter_status", {"job_id": started["job_id"]})
        if status["state"] == "failed":
            break
        time.sleep(0.01)
    assert status["state"] == "failed"
    assert worker.terminated is True
    assert broker.neutralise_calls >= 1

    monkeypatch.setattr(
        supervisor,
        "_start_worker_locked",
        lambda: install_clean_worker(supervisor) and supervisor.status_locked(),
    )
    recovered = supervisor.call("observe_game", {})
    assert recovered["id"] == 2


@pytest.mark.parametrize("timeout", [0.49, 120.01])
def test_runtime_request_timeout_is_bounded(timeout):
    with pytest.raises(ValueError, match="request timeout"):
        RuntimeSupervisor(request_timeout_seconds=timeout)


def test_remote_gamepad_provider_sends_active_and_neutral_updates(monkeypatch):
    calls = []

    def fake_exchange(port, payload, timeout=1.0):
        calls.append((port, dict(payload)))
        return {"ok": True, "active": payload.get("op") == "set" and payload.get("x") != 0.0}

    monkeypatch.setattr(gamepad_broker, "_exchange", fake_exchange)
    provider = RemoteVirtualLeftStick(43123, heartbeat_seconds=10.0)

    provider.left_joystick_float(0.4, 0.8)
    provider.update()
    provider.left_joystick_float(0.0, 0.0)
    provider.update()

    assert calls[0] == (43123, {"op": "status"})
    assert calls[1] == (43123, {"op": "set", "x": 0.4, "y": 0.8})
    assert calls[2] == (43123, {"op": "set", "x": 0.0, "y": 0.0})


def test_remote_gamepad_heartbeat_stops_after_neutral(monkeypatch):
    calls = []
    lock = threading.Lock()

    def fake_exchange(port, payload, timeout=1.0):
        with lock:
            calls.append(dict(payload))
        return {"ok": True}

    monkeypatch.setattr(gamepad_broker, "_exchange", fake_exchange)
    provider = RemoteVirtualLeftStick(43123, heartbeat_seconds=0.01)
    provider.left_joystick_float(0.0, 1.0)
    provider.update()
    time.sleep(0.035)
    provider.left_joystick_float(0.0, 0.0)
    provider.update()
    with lock:
        heartbeat_count = sum(call.get("op") == "heartbeat" for call in calls)
    time.sleep(0.025)
    with lock:
        later_heartbeat_count = sum(call.get("op") == "heartbeat" for call in calls)

    assert heartbeat_count >= 1
    assert later_heartbeat_count == heartbeat_count


def test_package_entrypoint_uses_stable_runtime_bridge():
    from pathlib import Path

    text = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'ff7r-agent-mcp = "ff7r_agent.mcp_runtime_server:main"' in text
    assert 'ff7r-agent-mcp-legacy = "ff7r_agent.mcp_server:main"' in text


def test_bridge_control_operations_bypass_worker_dispatch(monkeypatch):
    supervisor = RuntimeSupervisor()
    worker = FakeWorker()
    supervisor._worker = worker
    expected = {"ok": True, "worker_pid": 123}
    monkeypatch.setattr(supervisor, "status", lambda: expected)

    assert supervisor.call("get_agent_runtime_status") is expected
    assert worker.stdin.writes == []


def test_rebound_bridge_parent_owns_gamepad_broker_across_generations(monkeypatch):
    import ff7r_agent.runtime_bridge as runtime_bridge

    monkeypatch.setenv("FF7R_INPUT_BACKEND", "windows_gamepad")
    monkeypatch.delenv("FF7R_RUNTIME_SHARED_BROKER_PORT", raising=False)
    monkeypatch.delenv("FF7R_RUNTIME_SHARED_BROKER_PID", raising=False)
    broker = FakeBroker()
    broker_starts = []
    rebound_brokers = []

    def fake_broker_start():
        broker_starts.append(True)
        return broker

    def fake_rebound_start(**kwargs):
        rebound_brokers.append(kwargs["broker"])
        return object()

    monkeypatch.setattr(runtime_bridge.BrokerProcess, "start", fake_broker_start)
    monkeypatch.setattr(runtime_bridge._ReboundBridgeProcess, "start", fake_rebound_start)

    supervisor = RuntimeSupervisor()
    supervisor._start_rebound_bridge_locked({}, {})
    supervisor._start_rebound_bridge_locked({}, {})

    assert len(broker_starts) == 1
    assert supervisor._broker is broker
    assert rebound_brokers == [broker, broker]


def test_rebind_is_deferred_when_active_child_async_job_exists(monkeypatch):
    import ff7r_agent.runtime_bridge as runtime_bridge

    contract = {
        "deployment_profile": "FEATURE_VALIDATION",
        "repository_root": "C:/runtime",
        "source_root": "C:/runtime/Tools/ff7r-codex-agent-mvp",
        "package_import_root": "C:/runtime/Tools/ff7r-codex-agent-mvp/src",
        "expected_feature_ref": "codex/live-game-bridge-readiness",
        "expected_feature_commit": "old-commit",
        "expected_branch": None,
        "expected_clean_checkout": True,
    }
    old = dict(contract, git_branch="codex/live-game-bridge-readiness", git_commit_sha="old-commit", git_dirty=False)
    current = dict(old, expected_feature_commit="new-commit", git_commit_sha="new-commit")
    monkeypatch.setattr(runtime_bridge, "collect_identity", lambda **kwargs: old)
    monkeypatch.setattr(runtime_bridge, "validate_current_checkout", lambda value: current)
    monkeypatch.setattr(runtime_bridge, "profile_contract_from_environment", lambda: contract)
    monkeypatch.setattr(
        runtime_bridge,
        "verify_runtime_identity",
        lambda *args, **kwargs: {"ok": True, "mismatches": []},
    )

    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    bridge = FakeReboundBridge(old)
    bridge.active_job = {
        "job_id": "route-7",
        "state": "running",
        "operation": runtime_bridge.ASYNC_ROUTE_WORK_OPERATION,
    }
    supervisor._bridge_process = bridge
    supervisor._active_bridge_identity = old
    starts = []
    monkeypatch.setattr(
        supervisor,
        "_start_rebound_bridge_locked",
        lambda *args, **kwargs: starts.append(True),
    )

    with pytest.raises(RuntimeInfrastructureError) as raised:
        supervisor.call("observe_game", {})

    response = raised.value.as_response()
    assert response["error_code"] == "REBIND_DEFERRED_ACTIVE_JOB"
    assert response["phase"] == "rebind_deferred"
    assert response["diagnostics"]["rebind_attempted"] is False
    assert response["diagnostics"]["active_job"]["job_id"] == "route-7"
    assert starts == []
    assert bridge.closed is False

def test_stale_rebound_child_retains_completed_async_status(monkeypatch):
    import ff7r_agent.runtime_bridge as runtime_bridge

    contract = {
        "deployment_profile": "FEATURE_VALIDATION",
        "repository_root": "C:/runtime",
        "source_root": "C:/runtime/Tools/ff7r-codex-agent-mvp",
        "package_import_root": "C:/runtime/Tools/ff7r-codex-agent-mvp/src",
        "expected_feature_ref": "codex/live-game-bridge-readiness",
        "expected_feature_commit": "old-commit",
        "expected_branch": None,
        "expected_clean_checkout": True,
    }
    old = dict(contract, git_branch="codex/live-game-bridge-readiness", git_commit_sha="old-commit", git_dirty=False)
    current = dict(old, expected_feature_commit="new-commit", git_commit_sha="new-commit")
    monkeypatch.setattr(runtime_bridge, "collect_identity", lambda **kwargs: old)
    monkeypatch.setattr(runtime_bridge, "validate_current_checkout", lambda value: current)
    monkeypatch.setattr(runtime_bridge, "profile_contract_from_environment", lambda: contract)
    monkeypatch.setattr(
        runtime_bridge,
        "verify_runtime_identity",
        lambda *args, **kwargs: {"ok": True, "mismatches": []},
    )

    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    bridge = FakeReboundBridge(old)
    bridge.status_response = {
        "ok": True,
        "job_id": "route-7",
        "operation": runtime_bridge.ASYNC_ROUTE_WORK_OPERATION,
        "state": "completed",
        "response": {"result": {"completed_on": "old-commit"}},
    }
    supervisor._bridge_process = bridge
    supervisor._active_bridge_identity = old
    starts = []
    monkeypatch.setattr(
        supervisor,
        "_start_rebound_bridge_locked",
        lambda *args, **kwargs: starts.append(True),
    )

    result = supervisor.call(
        "get_live_route_adapter_status",
        {"job_id": "route-7"},
    )

    assert result["state"] == "completed"
    assert result["response"]["result"]["completed_on"] == "old-commit"
    assert bridge.calls == [("get_live_route_adapter_status", {"job_id": "route-7"})]
    assert starts == []
    assert bridge.closed is False

def test_live_call_rebinds_stale_source_once_before_writing_request(monkeypatch):
    import ff7r_agent.runtime_bridge as runtime_bridge

    contract = {
        "deployment_profile": "FEATURE_VALIDATION",
        "repository_root": "C:/runtime",
        "source_root": "C:/runtime/Tools/ff7r-codex-agent-mvp",
        "package_import_root": "C:/runtime/Tools/ff7r-codex-agent-mvp/src",
        "expected_feature_ref": "codex/live-game-bridge-readiness",
        "expected_feature_commit": "old-commit",
        "expected_branch": None,
        "expected_clean_checkout": True,
    }
    old = {
        "deployment_profile": "FEATURE_VALIDATION",
        "repository_root": "C:/runtime",
        "source_root": "C:/runtime/Tools/ff7r-codex-agent-mvp",
        "package_import_root": "C:/runtime/Tools/ff7r-codex-agent-mvp/src",
        "expected_feature_ref": contract["expected_feature_ref"],
        "expected_feature_commit": "old-commit",
        "git_branch": "codex/live-game-bridge-readiness",
        "git_commit_sha": "old-commit",
        "git_dirty": False,
    }
    current = dict(old)
    current.update({"expected_feature_commit": "new-commit", "git_commit_sha": "new-commit"})
    monkeypatch.setattr(runtime_bridge, "collect_identity", lambda **kwargs: old)
    monkeypatch.setattr(runtime_bridge, "validate_current_checkout", lambda value: current)
    monkeypatch.setattr(runtime_bridge, "profile_contract_from_environment", lambda: contract)
    monkeypatch.setattr(
        runtime_bridge,
        "verify_runtime_identity",
        lambda *args, **kwargs: {"ok": True, "mismatches": []},
    )
    monkeypatch.setenv("FF7R_RUNTIME_EXPECTED_COMMIT", "old-commit")

    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeWorker()
    supervisor._worker = worker
    supervisor._worker_identity = dict(old)
    replacement = FakeReboundBridge(current)
    events = []
    target_contracts = []

    def fake_shutdown():
        events.append("shutdown")
        supervisor._worker = None

    def fake_start(current_value, target_contract):
        events.append("process-start")
        target_contracts.append(dict(target_contract))
        return replacement

    monkeypatch.setattr(supervisor, "_shutdown_worker_locked", fake_shutdown)
    monkeypatch.setattr(supervisor, "_start_rebound_bridge_locked", fake_start)

    result = supervisor.call("observe_game", {})

    assert result["result"] == {"sequence": 7}
    assert events == ["process-start", "shutdown"]
    assert replacement.calls == [("observe_game", {})]
    assert target_contracts[0]["expected_feature_commit"] == "new-commit"
    assert os.environ["FF7R_RUNTIME_EXPECTED_COMMIT"] == "old-commit"
    assert supervisor._bridge_source_identity == old
    assert supervisor._active_bridge_identity == current
    assert replacement.closed is False


def test_failed_rebind_is_structured_and_retryable_per_operation(monkeypatch):
    import ff7r_agent.runtime_bridge as runtime_bridge

    contract = {
        "deployment_profile": "FEATURE_VALIDATION",
        "repository_root": "C:/runtime",
        "source_root": "C:/runtime/Tools/ff7r-codex-agent-mvp",
        "package_import_root": "C:/runtime/Tools/ff7r-codex-agent-mvp/src",
        "expected_feature_ref": "codex/live-game-bridge-readiness",
        "expected_feature_commit": "old-commit",
        "expected_branch": None,
        "expected_clean_checkout": True,
    }
    old = dict(contract, git_branch="codex/live-game-bridge-readiness", git_commit_sha="old-commit", git_dirty=False)
    current = dict(old, expected_feature_commit="new-commit", git_commit_sha="new-commit")
    monkeypatch.setattr(runtime_bridge, "collect_identity", lambda **kwargs: old)
    monkeypatch.setattr(runtime_bridge, "validate_current_checkout", lambda value: current)
    monkeypatch.setattr(runtime_bridge, "profile_contract_from_environment", lambda: contract)
    monkeypatch.setenv("FF7R_RUNTIME_EXPECTED_COMMIT", "old-commit")
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeWorker()
    supervisor._worker = worker
    supervisor._worker_identity = dict(old)
    attempts = []

    def fail_start(current_value, target_contract):
        attempts.append((current_value, target_contract))
        raise RuntimeBridgeError("start failed")

    monkeypatch.setattr(supervisor, "_start_rebound_bridge_locked", fail_start)

    with pytest.raises(RuntimeInfrastructureError) as raised:
        supervisor.call("observe_game", {})

    response = raised.value.as_response()
    assert response["error_code"] == "RUNTIME_BRIDGE_NOT_READY"
    assert response["phase"] == "rebind"
    assert response["diagnostics"]["rebind_attempted"] is True
    assert worker.stdin.writes == []
    assert supervisor._bridge_process is None
    assert supervisor._active_bridge_identity is None
    assert os.environ["FF7R_RUNTIME_EXPECTED_COMMIT"] == "old-commit"

    with pytest.raises(RuntimeInfrastructureError) as second:
        supervisor.call("observe_game", {})
    assert second.value.as_response()["diagnostics"]["rebind_attempted"] is True
    assert len(attempts) == 2
    assert os.environ["FF7R_RUNTIME_EXPECTED_COMMIT"] == "old-commit"

def test_rebound_bridge_process_round_trip_uses_fresh_runtime_process(monkeypatch):
    from ff7r_agent.runtime_bridge import _ReboundBridgeProcess

    for name in (
        "FF7R_RUNTIME_PROFILE",
        "FF7R_RUNTIME_REPOSITORY_ROOT",
        "FF7R_RUNTIME_SOURCE_ROOT",
        "FF7R_RUNTIME_PACKAGE_IMPORT_ROOT",
        "FF7R_RUNTIME_EXPECTED_BRANCH",
        "FF7R_RUNTIME_EXPECTED_COMMIT",
        "FF7R_RUNTIME_EXPECTED_REF",
        "FF7R_RUNTIME_EXPECTED_CLEAN",
        "FF7R_RUNTIME_SHARED_BROKER_PORT",
        "FF7R_RUNTIME_SHARED_BROKER_PID",
    ):
        monkeypatch.delenv(name, raising=False)
    repository_root = Path(__file__).parents[3].resolve()
    current = {
        "repository_root": str(repository_root),
        "package_import_root": str(repository_root / "Tools" / "ff7r-codex-agent-mvp" / "src"),
    }
    bridge = _ReboundBridgeProcess.start(
        current=current,
        contract={},
        broker=None,
        startup_timeout_seconds=20.0,
        request_timeout_seconds=2.0,
    )
    try:
        assert bridge.is_alive()
        assert bridge.identity["pid"] != os.getpid()
        assert bridge.identity["module_paths"]["ff7r_agent.runtime_bridge_process"]
        status = bridge.call("get_agent_runtime_status", {})
        assert status["ok"] is True
        assert status["worker_running"] is True
        assert bridge.active_async_job() is None
    finally:
        bridge.close()
    assert not bridge.is_alive()

def test_bridge_identity_keeps_original_binding_when_disk_revision_changes(monkeypatch):
    import ff7r_agent.runtime_bridge as runtime_bridge

    old = {
        "git_commit": "commit-a",
        "git_commit_sha": "commit-a",
        "git_branch": "codex/live-game-bridge-readiness",
        "git_dirty": False,
    }
    current = dict(old, git_commit="commit-b", git_commit_sha="commit-b")
    monkeypatch.setattr(runtime_bridge, "collect_identity", lambda **kwargs: current)
    monkeypatch.setattr(runtime_bridge, "profile_contract_from_environment", lambda: None)
    supervisor = RuntimeSupervisor()
    supervisor._bridge_source_identity = old

    identity = supervisor.identity_locked()

    assert identity["git_commit_sha"] == "commit-a"
    assert identity["bridge_source_binding"]["git_commit_sha"] == "commit-a"

def test_rebound_bridge_child_does_not_recursively_rebind(monkeypatch):
    import ff7r_agent.runtime_bridge as runtime_bridge

    contract = {
        "deployment_profile": "FEATURE_VALIDATION",
        "repository_root": "C:/runtime",
        "source_root": "C:/runtime/Tools/ff7r-codex-agent-mvp",
        "package_import_root": "C:/runtime/Tools/ff7r-codex-agent-mvp/src",
        "expected_feature_ref": "codex/live-game-bridge-readiness",
        "expected_feature_commit": "old-commit",
        "expected_branch": None,
        "expected_clean_checkout": True,
    }
    old = dict(
        contract,
        git_branch="codex/live-game-bridge-readiness",
        git_commit_sha="old-commit",
    )
    current = dict(old, expected_feature_commit="new-commit", git_commit_sha="new-commit")
    monkeypatch.setattr(runtime_bridge, "collect_identity", lambda **kwargs: old)
    monkeypatch.setattr(runtime_bridge, "validate_current_checkout", lambda value: current)
    monkeypatch.setattr(runtime_bridge, "profile_contract_from_environment", lambda: contract)
    monkeypatch.setattr(
        runtime_bridge,
        "verify_runtime_identity",
        lambda *args, **kwargs: {"ok": True, "mismatches": []},
    )
    monkeypatch.setenv("FF7R_RUNTIME_REBIND_DISABLED", "1")
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeWorker()
    supervisor._worker = worker
    supervisor._worker_identity = dict(old)

    with pytest.raises(RuntimeInfrastructureError) as raised:
        supervisor.call("observe_game", {})

    response = raised.value.as_response()
    assert response["phase"] == "child_binding"
    assert response["diagnostics"]["rebind_attempted"] is False
    assert worker.stdin.writes == []

def test_rebound_bridge_process_honors_feature_source_contract(monkeypatch):
    from ff7r_agent.runtime_bridge import _ReboundBridgeProcess
    from ff7r_agent.runtime_profile import validate_current_checkout

    repository_root = Path(__file__).parents[3].resolve()
    commit = subprocess.check_output(
        [
            "git",
            "-c",
            f"safe.directory={repository_root.as_posix()}",
            "-C",
            str(repository_root),
            "rev-parse",
            "HEAD",
        ],
        text=True,
    ).strip()
    monkeypatch.setenv("FF7R_RUNTIME_PROFILE", "FEATURE_VALIDATION")
    monkeypatch.setenv("FF7R_RUNTIME_REPOSITORY_ROOT", str(repository_root))
    monkeypatch.setenv(
        "FF7R_RUNTIME_SOURCE_ROOT",
        str(repository_root / "Tools" / "ff7r-codex-agent-mvp"),
    )
    monkeypatch.setenv(
        "FF7R_RUNTIME_PACKAGE_IMPORT_ROOT",
        str(repository_root / "Tools" / "ff7r-codex-agent-mvp" / "src"),
    )
    # GitHub PR validation checks out a synthetic merge commit, so the source
    # contract must use the exact checkout being validated rather than the
    # feature branch ref, which still resolves to the head commit.
    feature_ref = "HEAD"
    monkeypatch.setenv("FF7R_RUNTIME_EXPECTED_REF", feature_ref)
    monkeypatch.setenv("FF7R_RUNTIME_EXPECTED_COMMIT", commit)
    monkeypatch.setenv("FF7R_RUNTIME_EXPECTED_CLEAN", "true")
    monkeypatch.setenv("FF7R_INPUT_BACKEND", "simulation")
    monkeypatch.delenv("FF7R_RUNTIME_SHARED_BROKER_PORT", raising=False)
    monkeypatch.delenv("FF7R_RUNTIME_SHARED_BROKER_PID", raising=False)
    monkeypatch.delenv("FF7R_RUNTIME_REBIND_DISABLED", raising=False)

    contract = {
        "deployment_profile": "FEATURE_VALIDATION",
        "repository_root": str(repository_root),
        "source_root": str(repository_root / "Tools" / "ff7r-codex-agent-mvp"),
        "package_import_root": str(repository_root / "Tools" / "ff7r-codex-agent-mvp" / "src"),
        "expected_feature_ref": feature_ref,
        "expected_feature_commit": commit,
        "expected_branch": None,
        "expected_clean_checkout": True,
    }
    current = validate_current_checkout(contract)
    bridge = _ReboundBridgeProcess.start(
        current=current,
        contract=contract,
        broker=None,
        startup_timeout_seconds=20.0,
        # This exercises real source validation, including Git subprocesses.
        # Short transport-deadline behavior is tested separately with fake workers.
        request_timeout_seconds=DEFAULT_RUNTIME_REQUEST_TIMEOUT_SECONDS,
    )
    try:
        assert bridge.identity["git_commit_sha"] == commit
        assert bridge.identity["bridge_source_binding"]["git_commit_sha"] == commit
        response = bridge.call("observe_game", {})
        assert response["id"] == 1
    finally:
        bridge.close()

def test_runtime_supervisor_identity_is_read_only_and_contains_bridge_fields():
    supervisor = RuntimeSupervisor()

    identity = supervisor.identity_locked()

    assert identity["process_role"] == "mcp-bridge"
    assert identity["bridge_identity"]["pid"] > 0
    assert identity["module_paths"]["ff7r_agent.mcp_runtime_server"]
    assert identity["module_paths"]["ff7r_agent.runtime_worker"]
    assert "worker_generation" in identity
    assert "worker_pid" in identity
    assert "gamepad_broker_identity" in identity


def test_runtime_supervisor_reuses_ready_worker_identity(monkeypatch):
    import ff7r_agent.runtime_bridge as runtime_bridge

    supervisor = RuntimeSupervisor()
    supervisor._worker = FakeWorker()
    supervisor._worker.pid = 4321
    supervisor._generation = 4
    supervisor._worker_identity = {
        "schema_version": 2,
        "process_role": "reloadable-worker",
        "pid": 4321,
        "source_root": "C:/canonical/Tools/ff7r-codex-agent-mvp",
        "package_import_root": "C:/canonical/Tools/ff7r-codex-agent-mvp/src",
        "loaded_package_path": "C:/canonical/Tools/ff7r-codex-agent-mvp/src/ff7r_agent/__init__.py",
        "loaded_module_path": "C:/canonical/Tools/ff7r-codex-agent-mvp/src/ff7r_agent/runtime_worker.py",
        "module_paths": {
            "ff7r_agent": "C:/canonical/Tools/ff7r-codex-agent-mvp/src/ff7r_agent/__init__.py",
            "ff7r_agent.runtime_worker": "C:/canonical/Tools/ff7r-codex-agent-mvp/src/ff7r_agent/runtime_worker.py",
        },
        "python_executable": "D:/runtime/python.exe",
        "worker_generation": 4,
        "worker_pid": 4321,
        "bridge_identity": {
            "name": "ff7r-agent-reloadable-worker",
            "entrypoint": "ff7r_agent.runtime_worker:main",
            "pid": 4321,
        },
        "gamepad_broker_identity": {
            "active": False,
            "pid": None,
            "port": None,
        },
        "runtime_directory": "C:/runtime/ff7r-agent",
        "lifecycle_directory": "C:/runtime/ff7r-agent/lifecycle",
        "git_root": "C:/canonical",
        "git_branch": "codex/ff7r-canonical-consolidation",
        "git_commit": "canonical-sha",
        "git_commit_sha": "canonical-sha",
        "source_commit_sha": "implementation-sha",
        "git_dirty": False,
    }
    monkeypatch.setattr(
        runtime_bridge,
        "collect_identity",
        lambda **kwargs: pytest.fail("identity rediscovery should not run"),
    )

    identity = supervisor.identity_locked()

    assert identity["process_role"] == "mcp-bridge"
    assert identity["git_commit_sha"] == "canonical-sha"
    assert identity["git_branch"] == "codex/ff7r-canonical-consolidation"
    assert identity["source_root"] == "C:/canonical/Tools/ff7r-codex-agent-mvp"
    assert identity["worker_pid"] == 4321
    assert identity["worker_identity"]["process_role"] == "reloadable-worker"
    assert identity["module_paths"]["ff7r_agent.runtime_bridge"].endswith(
        "runtime_bridge.py"
    )


def test_stable_bridge_exposes_runtime_identity_without_worker_operation(monkeypatch):
    from types import SimpleNamespace

    from ff7r_agent import mcp_runtime_server

    expected = {"source_root": "test", "worker_pid": 123}
    monkeypatch.setattr(
        mcp_runtime_server,
        "_runtime",
        lambda: SimpleNamespace(identity=lambda: expected),
    )

    assert mcp_runtime_server.get_runtime_identity() == expected


def test_bootstrap_verification_is_read_only_and_checks_runtime_state(monkeypatch):
    supervisor = RuntimeSupervisor()
    supervisor._worker = FakeWorker()
    calls = []
    monkeypatch.setattr(
        supervisor,
        "_request_locked",
        lambda operation, arguments: calls.append(operation)
        or {"ok": True, "id": len(calls), "result": {"operation": operation}},
    )
    monkeypatch.setattr(
        supervisor,
        "identity_locked",
        lambda: {"source_root": "canonical", "worker_pid": 123},
    )

    result = supervisor.bootstrap_verification()

    assert calls == ["get_lifecycle_status", "get_session_health", "observe_game"]
    assert result["runtime_identity"]["source_root"] == "canonical"
    assert result["read_only"] is True
    assert result["input_sent"] is False
    assert result["lifecycle_mutated"] is False


def test_worker_identity_has_distinct_worker_entrypoint():
    from ff7r_agent.runtime_identity import collect_identity

    identity = collect_identity(
        process_role="reloadable-worker",
        module_path=__file__,
        bridge_identity={
            "name": "ff7r-agent-reloadable-worker",
            "entrypoint": "ff7r_agent.runtime_worker:main",
            "pid": 123,
        },
    )

    assert identity["bridge_identity"]["name"] == "ff7r-agent-reloadable-worker"
    assert identity["bridge_identity"]["entrypoint"] == "ff7r_agent.runtime_worker:main"


def test_stable_bridge_exposes_bootstrap_verification(monkeypatch):
    from ff7r_agent import mcp_runtime_server

    expected = {"ok": True, "read_only": True, "input_sent": False}
    monkeypatch.setattr(
        mcp_runtime_server,
        "_runtime",
        lambda: SimpleNamespace(bootstrap_verification=lambda: expected),
    )

    assert mcp_runtime_server.get_bootstrap_verification() == expected


@pytest.mark.parametrize("profile", [None, "CANONICAL"])
def test_speed_trial_rejects_non_feature_runtime_before_any_worker_write(monkeypatch, profile):
    if profile is None:
        monkeypatch.delenv("FF7R_RUNTIME_PROFILE", raising=False)
    else:
        monkeypatch.setenv("FF7R_RUNTIME_PROFILE", profile)
    values = proven_live_route_arguments(normal_stick_magnitude=0.8, controller_policy_profile="normal_0_8_trial")
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker
    rejected = supervisor.call("start_live_route_adapter", values)
    assert rejected["error"] == "live_controller_trial_requires_feature_runtime"
    assert not worker.stdin.writes
    with pytest.raises(ValueError, match="feature-validation runtime"):
        _run_live_route_adapter(FakeAttachedService(), FakeAttachedLifecycle([]), values)


def test_async_speed_trial_records_actual_08_policy(monkeypatch):
    monkeypatch.setenv("FF7R_RUNTIME_PROFILE", "FEATURE_VALIDATION")
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker
    # This test isolates policy evidence; source-binding readiness is covered
    # by the dedicated recovery tests above.
    monkeypatch.setattr(supervisor, "_ensure_live_runtime_ready_locked", lambda operation: None)
    started = supervisor.call("start_live_route_adapter", proven_live_route_arguments(
        normal_stick_magnitude=0.8, controller_policy_profile="normal_0_8_trial"))
    assert started["ok"]
    assert started["controller_policy"]["normal_stick_magnitude"] == 0.8
    assert started["controller_policy"]["validation_status"] == "experimental"
    assert started["controller_policy"]["final_arrival_radius_m"] == 0.75
    assert len(worker.stdin.writes) == 1


@pytest.mark.parametrize("change", [
    {"normal_stick_magnitude": 0.8},
    {"normal_stick_magnitude": 1.0, "controller_policy_profile": "normal_0_8_trial"},
    {"normal_stick_magnitude": 0.8, "controller_policy_profile": "normal_0_8_trial", "final_arrival_radius_m": 1.0},
    {"controller_policy_profile": "unknown"},
])
def test_speed_trial_cannot_relax_other_policy_fields(monkeypatch, change):
    monkeypatch.setenv("FF7R_RUNTIME_PROFILE", "FEATURE_VALIDATION")
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker
    result = supervisor.call("start_live_route_adapter", proven_live_route_arguments(**change))
    assert not result["ok"]
    assert not worker.stdin.writes



def test_sync_close_game_is_refused_before_worker_write():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker

    rejected = supervisor.call("close_game", {})

    assert rejected["ok"] is False
    assert rejected["error"] == "close_game_requires_async"
    assert rejected["use_operation"] == "close_game_async"
    assert rejected["status_operation"] == "get_close_game_status"
    assert len(worker.stdin.writes) == 0


def test_async_close_game_bypasses_short_worker_request_timeout():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout(
        '{"ok":true,"id":1,"result":{"ok":false,"state":"manual_end_task_required"}}\n'
    )
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker

    started = supervisor.call("close_game_async", {"timeout_seconds": 90.0})
    assert started["ok"] is True
    assert started["operation"] == "close_game"
    assert started["state"] == "running"
    assert len(worker.stdin.writes) == 1
    message = json.loads(worker.stdin.writes[0])
    assert message["operation"] == "close_game"
    assert message["arguments"] == {"timeout_seconds": 90.0}

    # The synchronous bridge deadline is 0.5 s in this test.  The asynchronous
    # close must remain alive beyond that deadline instead of terminating the
    # worker and losing the lifecycle result.
    time.sleep(0.55)
    status = supervisor.call("get_close_game_status", {"job_id": started["job_id"]})
    assert status["state"] == "running"
    assert worker.terminated is False
    assert stdout.readline_calls == 1

    stdout.release.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        status = supervisor.call("get_close_game_status", {"job_id": started["job_id"]})
        if status["state"] == "completed":
            break
        time.sleep(0.01)
    else:
        pytest.fail("asynchronous close request did not complete")

    assert status["response"]["result"]["state"] == "manual_end_task_required"


def test_second_async_close_is_rejected_without_duplicate_worker_write():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout(
        '{"ok":true,"id":1,"result":{"ok":true,"state":"closed"}}\n'
    )
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker
    started = supervisor.call("close_game_async", {"timeout_seconds": 90.0})
    assert started["state"] == "running"
    try:
        rejected = supervisor.call("close_game_async", {"timeout_seconds": 90.0})
        assert rejected["ok"] is False
        assert rejected["error"] == "async_close_in_progress"
        assert rejected["allowed_operations"] == ["get_close_game_status", "say_on_stream", "stop_all_input"]
        assert len(worker.stdin.writes) == 1
        assert stdout.readline_calls == 1
    finally:
        stdout.release.set()


def test_sync_mulligan_cards_is_refused_before_worker_write():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker

    rejected = supervisor.call("mulligan_cards", {"card_indices": [0]})

    assert rejected["ok"] is False
    assert rejected["error"] == "mulligan_cards_requires_async"
    assert rejected["use_operation"] == "mulligan_cards_async"
    assert rejected["status_operation"] == "get_mulligan_cards_status"
    assert len(worker.stdin.writes) == 0


def test_async_mulligan_cards_outlives_short_worker_request_timeout():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout(
        '{"ok":true,"id":1,"result":{"ok":true,"outcome":"cards_marked_verified"}}\n'
    )
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker
    arguments = {"card_indices": [0, 2, 4], "submit_play": False}

    started = supervisor.call("mulligan_cards_async", arguments)
    assert started["ok"] is True
    assert started["operation"] == "mulligan_cards"
    assert started["job_id"].startswith("mulligan-")
    message = json.loads(worker.stdin.writes[0])
    assert message["operation"] == "mulligan_cards"
    assert message["arguments"] == arguments

    # Only status and stop are accepted while cards are being marked.
    busy = supervisor.call("observe_queens_blood", {})
    assert busy["ok"] is False
    assert busy["error"] == "async_mulligan_in_progress"

    time.sleep(0.55)
    status = supervisor.call("get_mulligan_cards_status", {"job_id": started["job_id"]})
    assert status["state"] == "running"
    assert worker.terminated is False

    stdout.release.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        status = supervisor.call("get_mulligan_cards_status", {"job_id": started["job_id"]})
        if status["state"] == "completed":
            break
        time.sleep(0.01)
    else:
        pytest.fail("asynchronous mulligan request did not complete")

    assert status["response"]["result"]["outcome"] == "cards_marked_verified"


def test_match_setup_injects_actual_context_and_caches_progress_without_worker_polls(monkeypatch):
    supervisor = RuntimeSupervisor()
    progress = {"stage": "deck", "completed_stages": ["preflight", "approach", "interaction"], "elapsed_seconds": 15}
    worker = FakeAsyncWorker(io.StringIO(json.dumps({"ok": True, "id": 1,
        "event": "match_setup_progress", "progress": progress}) + '\n' +
        '{"ok":true,"id":1,"result":{"ok":true,"outcome":"mulligan_ready"}}\n'))
    supervisor._worker = worker
    monkeypatch.setattr(supervisor, "_bootstrap_verification_locked", lambda: {"ok": True, "checked": "actual"})
    monkeypatch.setattr(supervisor, "identity_locked", lambda: {"repository_root": "actual-checkout"})
    started = supervisor.call("setup_queens_blood_match_async", {"target_id": "zhijun"})
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        result = supervisor.call("get_setup_queens_blood_match_status", {"job_id": started["job_id"]})
        if result["state"] == "completed": break
        time.sleep(.01)
    assert result["response"]["result"]["outcome"] == "mulligan_ready"
    assert result["progress"] == progress
    sent = json.loads(worker.stdin.writes[0])
    assert sent["arguments"]["__setup_context"]["identity"]["repository_root"] == "actual-checkout"
    assert sent["arguments"]["__setup_context"]["bootstrap"] == {"ok": True, "checked": "actual"}
    for _ in range(10): supervisor.call("get_setup_queens_blood_match_status", {"job_id": started["job_id"]})
    assert len(worker.stdin.writes) == 1


def test_match_setup_refuses_caller_context_before_worker_write():
    supervisor = RuntimeSupervisor(); worker = FakeAsyncWorker(); supervisor._worker = worker
    result = supervisor.call("setup_queens_blood_match_async", {"__setup_context": {"identity": "forged"}})
    assert result["error"] == "match_setup_context_is_supervisor_owned"
    assert not worker.stdin.writes


def test_match_setup_blocks_a_second_input_job(monkeypatch):
    supervisor = RuntimeSupervisor()
    stdout = BlockingStdout('{"ok":true,"id":1,"result":{"ok":false,"outcome":"setup_stopped"}}\n')
    worker = FakeAsyncWorker(stdout); supervisor._worker = worker
    monkeypatch.setattr(supervisor, "_bootstrap_verification_locked", lambda: {"ok": True})
    monkeypatch.setattr(supervisor, "identity_locked", lambda: {"repository_root": "actual"})
    started = supervisor.call("setup_queens_blood_match_async", {})
    try:
        assert supervisor.call("initiate_queens_blood_match_async", {})["error"] == "async_match_setup_in_progress"
        assert supervisor.call("observe_queens_blood", {})["error"] == "async_match_setup_in_progress"
        for _ in range(10):
            assert supervisor.call("get_setup_queens_blood_match_status", {"job_id": started["job_id"]})["state"] == "running"
        assert len(worker.stdin.writes) == 1
    finally:
        stdout.release.set()


def test_a_shop_purchase_runs_asynchronously_and_a_dry_run_does_not():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker
    rejected = supervisor.call("buy_shop_item", {"item": "it_key_minigamecardPack_001", "execute": True})
    assert rejected["error"] == "buy_shop_item_requires_async"
    assert rejected["use_operation"] == "buy_shop_item_async"
    assert len(worker.stdin.writes) == 0

    stdout = BlockingStdout('{"ok":true,"id":1,"result":{"ok":true,"paid":500}}\n')
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker
    started = supervisor.call("buy_shop_item_async", {"item": "it_key_minigamecardPack_001", "execute": True})
    assert started["ok"] is True and started["job_id"].startswith("shop-buy-")
    busy = supervisor.call("read_shop", {})
    assert busy["error"] == "async_shop_buy_in_progress"
    stdout.release.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        status = supervisor.call("get_buy_shop_item_status", {"job_id": started["job_id"]})
        if status["state"] == "completed":
            break
        time.sleep(0.01)
    else:
        pytest.fail("asynchronous purchase did not complete")
    assert status["response"]["result"]["paid"] == 500


@pytest.mark.parametrize("operation", ["run_field_menu", "cast_field_spell", "use_field_item"])
def test_the_field_menu_runs_only_asynchronously(operation):
    """Live 2026-10-21 an Ether outran the request deadline with the menu open."""
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker
    rejected = supervisor.call(operation, {"spell": "cure"})
    assert rejected["error"] == "field_menu_requires_async"
    assert rejected["use_operation"] == "field_menu_async"
    assert len(worker.stdin.writes) == 0


def test_a_field_menu_job_runs_and_reports():
    stdout = BlockingStdout('{"ok":true,"id":1,"result":{"ok":true,"uses":4}}\n')
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker
    started = supervisor.call("field_menu_async", {"action": "cast_spell", "spell": "cure"})
    assert started["ok"] is True and started["job_id"].startswith("field-menu-")
    assert supervisor.call("observe_game", {})["error"] == "async_field_menu_in_progress"
    stdout.release.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        status = supervisor.call("get_field_menu_status", {"job_id": started["job_id"]})
        if status["state"] == "completed":
            break
        time.sleep(0.01)
    else:
        pytest.fail("the field menu job did not complete")
    assert status["response"]["result"]["uses"] == 4


@pytest.mark.parametrize("operation", ["play_queens_blood_match", "play_queens_blood"])
def test_sync_queens_blood_match_is_refused_before_worker_write(operation):
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    worker = FakeAsyncWorker()
    supervisor._worker = worker

    rejected = supervisor.call(operation, {})

    assert rejected["ok"] is False
    assert rejected["error"] == "play_queens_blood_match_requires_async"
    assert rejected["use_operation"] == "play_queens_blood_match_async"
    assert len(worker.stdin.writes) == 0


def test_async_queens_blood_match_runs_as_one_job():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout('{"ok":true,"id":1,"result":{"ok":true,"outcome":"match_finished"}}\n')
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker

    started = supervisor.call("play_queens_blood_match_async", {"timeout_seconds": 900})
    assert started["ok"] is True and started["job_id"].startswith("match-")
    assert json.loads(worker.stdin.writes[0])["operation"] == "play_queens_blood_match"
    assert supervisor.call("place_queens_blood_card", {})["error"] == "async_match_in_progress"

    stdout.release.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        status = supervisor.call("get_play_queens_blood_match_status", {"job_id": started["job_id"]})
        if status["state"] == "completed":
            break
        time.sleep(0.01)
    else:
        pytest.fail("asynchronous match request did not complete")
    assert status["response"]["result"]["outcome"] == "match_finished"


def test_async_conversation_recovery_runs_as_one_job():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout('{"ok":true,"id":1,"result":{"ok":true,"outcome":"declined"}}\n')
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker

    started = supervisor.call("finish_conversation_async", {"timeout_seconds": 120})
    assert started["ok"] is True and started["job_id"].startswith("conversation-")
    request = json.loads(worker.stdin.writes[0])
    assert request["operation"] == "run_finish_conversation"
    assert request["arguments"] == {"timeout_seconds": 120}
    assert supervisor.call("resolve_world_target", {})["error"] == "async_conversation_recovery_in_progress"

    stdout.release.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        status = supervisor.call("get_finish_conversation_status", {"job_id": started["job_id"]})
        if status["state"] == "completed":
            break
        time.sleep(0.01)
    else:
        pytest.fail("asynchronous conversation recovery did not complete")
    assert status["response"]["result"]["outcome"] == "declined"


def test_an_expired_checkout_validation_is_rechecked_without_making_requests_wait(monkeypatch):
    # Live 2026-10-05 the Git calls took 3-6 s, stalling one request every 10 s.
    import threading as _threading
    from ff7r_agent import runtime_bridge as bridge
    calls, outcomes = [], [{"git_commit_sha": "a"}, {"git_commit_sha": "a"}, RuntimeError("checkout is dirty")]
    def validate(contract):
        calls.append(contract)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    def settle():
        for thread in _threading.enumerate():
            if thread.name == "checkout-validation":
                thread.join(5)
    monkeypatch.setattr(bridge, "validate_current_checkout", validate)
    monkeypatch.setattr(bridge, "_checkout_state_stamp", lambda contract: ("same",))
    monkeypatch.setattr(bridge, "CHECKOUT_VALIDATION_TTL_SECONDS", 0.0)
    monkeypatch.setattr(bridge, "_checkout_validation_cache", {})
    contract = {"repository_root": "checkout"}
    assert bridge._cached_checkout_validation(contract) == {"git_commit_sha": "a"}  # validated first
    assert len(calls) == 1
    assert bridge._cached_checkout_validation(contract) == {"git_commit_sha": "a"}  # served, re-checked behind
    settle()
    assert len(calls) == 2
    bridge._cached_checkout_validation(contract)  # served again; this re-check fails
    settle()
    with pytest.raises(RuntimeError, match="dirty"):
        bridge._cached_checkout_validation(contract)


def test_a_git_timeout_never_refuses_a_request(monkeypatch):
    # Live 2026-10-05 a background `git rev-parse HEAD` timed out under game
    # load and the next request (a menu press) was refused.
    import threading as _threading
    from ff7r_agent import runtime_bridge as bridge
    from ff7r_agent.runtime_profile import GitTimeoutError
    outcomes = [{"git_commit_sha": "a"}, GitTimeoutError("timed out"), GitTimeoutError("timed out"), {"git_commit_sha": "b"}]
    def validate(contract):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    def settle():
        for thread in _threading.enumerate():
            if thread.name == "checkout-validation":
                thread.join(5)
    stamp = ["one"]
    monkeypatch.setattr(bridge, "validate_current_checkout", validate)
    monkeypatch.setattr(bridge, "_checkout_state_stamp", lambda contract: (stamp[0],))
    monkeypatch.setattr(bridge, "CHECKOUT_VALIDATION_TTL_SECONDS", 0.0)
    monkeypatch.setattr(bridge, "_checkout_validation_cache", {})
    contract = {"repository_root": "checkout"}
    assert bridge._cached_checkout_validation(contract) == {"git_commit_sha": "a"}
    bridge._cached_checkout_validation(contract)  # the background re-check times out
    settle()
    assert bridge._cached_checkout_validation(contract) == {"git_commit_sha": "a"}  # not refused
    settle()
    stamp[0] = "two"  # a changed checkout is validated before the request, retried once
    outcomes[:] = [GitTimeoutError("timed out"), {"git_commit_sha": "b"}]
    assert bridge._cached_checkout_validation(contract) == {"git_commit_sha": "b"}


def test_async_map_travel_outlives_sync_deadline_without_replaying_worker_request():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout('{"ok":true,"id":1,"result":{"state":"arrived"}}\n')
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker
    arguments = {"spot": "Kalm", "execute": True}
    started = supervisor.call("travel_to_map_spot_async", arguments)
    try:
        assert started["state"] == "running"
        time.sleep(0.55)
        assert supervisor.call("get_travel_to_map_spot_status", {"job_id": started["job_id"]})["state"] == "running"
        rejected = supervisor.call("travel_to_map_spot_async", arguments)
        assert rejected["error"] == "async_travel_in_progress"
        assert worker.terminated is False and len(worker.stdin.writes) == 1
        assert json.loads(worker.stdin.writes[0])["arguments"] == arguments
        stdout.release.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            response = supervisor.call("get_travel_to_map_spot_status", {"job_id": started["job_id"]})
            if response["state"] == "completed":
                break
            time.sleep(0.01)
        else:
            pytest.fail("async map travel did not complete")
        assert response["response"]["result"]["state"] == "arrived"
        assert len(worker.stdin.writes) == 1
    finally:
        stdout.release.set()


def test_calibration_resolution_async_alias_uses_exclusive_save_job():
    supervisor = RuntimeSupervisor(request_timeout_seconds=0.5)
    stdout = BlockingStdout('{"ok":true,"id":1,"result":{"ok":true,"state":"save_calibration_cancelled"}}\n')
    worker = FakeAsyncWorker(stdout)
    supervisor._worker = worker
    values = {"expected_token": "a" * 32, "expected_sha256": "b" * 64, "execute": True}
    started = supervisor.call("resolve_save_calibration_async", values)
    assert started["state"] == "running"
    message = json.loads(worker.stdin.writes[0])
    assert message["operation"] == "save_game_progress"
    assert message["arguments"] == {"execute": True, "resolve_calibration": {"expected_token": "a" * 32, "expected_sha256": "b" * 64}}
    assert supervisor.call("get_resolve_save_calibration_status", {"job_id": started["job_id"]})["state"] == "running"
    assert supervisor.call("save_game_progress_async", {})["error"] == "async_save_in_progress"
    stdout.release.set()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        polled = supervisor.call("get_resolve_save_calibration_status", {"job_id": started["job_id"]})
        if polled["state"] == "completed": break
        time.sleep(0.01)
    assert polled["response"]["result"]["state"] == "save_calibration_cancelled"
