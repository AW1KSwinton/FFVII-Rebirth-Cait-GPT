"""#364 regressions: synthetic process/clock evidence; no live telemetry/input.

The legacy record uses the observed PID 816, live worker 25412, attribution
and timestamps from #364's preserved CLOSED archive. OPEN state and the
observation clock are reconstructed; this is not a real-time capture replay.
Original captures remain unchanged outside Git.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ff7r_agent.autonomy import LifecycleController
from ff7r_agent.control_recovery import recover_stale_lifecycle
from ff7r_agent.runtime_commands import invoke_runtime
from ff7r_agent.safety import ArmError, ArmState, BenchArmState, CameraArmState, SessionLease
from ff7r_agent.session_startup import StartupBlocked, startup
from test_session_startup import Harness, Services, no_sleep, options, validate


class DeadGameEnvironment:
    def __init__(self):
        self.processes = {}
        self.probe_error = None
        self.probes = 0

    def game_process_ids(self):
        return dict(self.processes)

    def game_process_exists(self):
        return bool(self.processes)

    def game_process_ids_verified(self):
        self.probes += 1
        if self.probe_error:
            raise self.probe_error
        return dict(self.processes)


def legacy_runtime(tmp_path, *, client=None):
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    clock = 1791886589.0  # synthetic observation time, 738.83 s before expiry
    pid_absent = [True]
    lease = SessionLease(control_dir, owner_id="ff7r-codex-agent", clock=lambda: clock,
                         session_marker=lambda: "windows:9:boot-minute:29850576", process_id=25412,
                         process_absent=lambda pid: pid == 816 and pid_absent[0])
    record = {"version": 1, "state": "OPEN", "client_id": client,
              "owner_id": lease.owner_id, "controller_pid": 25412, "game_pid": 816,
              "session_marker": lease.session_marker(), "opened_at": 1791884674.548736,
              "heartbeat_at": 1791885527.8295958, "expires_at": 1791887327.8295958,
              "recording_driver": {"modelId": "claude-opus-5-5", "effort": "high"}}
    # Deliberately noncanonical bytes: recovery must preserve them exactly.
    raw = (json.dumps(record, indent=3) + "\n").encode()
    lease.state_path.write_bytes(raw)
    environment = DeadGameEnvironment()
    executable = tmp_path / "ff7rebirth_.exe"
    executable.touch()
    lifecycle = LifecycleController(executable, tmp_path / "telemetry.json", tmp_path / "lifecycle",
                                    owner_id=lease.owner_id, environment=environment)
    lifecycle.runtime_dir.mkdir()
    lifecycle.lease.path.write_text(json.dumps({
        "version": 1, "state": "OWNED_BY_THIS_CONTROLLER", "owner_id": lease.owner_id,
        "pid": 25412, "lease_id": "6db69a27-8df0-41f6-ad33-ee2762e12234"}))
    context = {"owner_id": lease.owner_id, "pid": 816, "executable_path": str(executable),
               "lease_id": "6db69a27-8df0-41f6-ad33-ee2762e12234", "state": "running"}
    (lifecycle.runtime_dir / "run-context.json").write_text(json.dumps(context))
    service = SimpleNamespace(_session_lease=lease,
                              session_health=lambda: {"process_exists": False, "gameplay_ready": False},
                              input_status=lambda: {"real_input": True, "backend_status": {
                                  "stick_state": "neutral", "last_stick": {"x": 0, "y": 0}}})
    return service, lifecycle, record, raw, pid_absent


@pytest.mark.parametrize("client", [None, "old-foreign-client"])
def test_legacy_dead_game_recovers_while_worker_lives_and_preserves_exact_raw_bytes(tmp_path, client):
    service, lifecycle, record, raw, _ = legacy_runtime(tmp_path, client=client)
    lease = service._session_lease
    before = lease.status()
    assert before["active"] is True and before["seconds_remaining"] > 738
    assert before["controller_pid"] == 25412  # worker lifetime is irrelevant
    with pytest.raises(ArmError, match="active control session"):
        lease.open(client_id="new-client")
    result = invoke_runtime(service, lifecycle, None, "recover_stale_lifecycle",
                            {"expected_control_session": before})
    assert result["recovered"] is True and result["control_session"]["closed"] is True
    assert Path(result["control_session"]["raw_archive_path"]).read_bytes() == raw
    assert json.loads(Path(result["control_session"]["archive_path"]).read_text())["close_reason"] == "legacy_game_exited"
    assert lease.status() is None and lifecycle.lease.status() is None
    for broker in (ArmState, BenchArmState, CameraArmState):
        assert broker(runtime_dir=lease.runtime_dir).snapshot()[2] is True
    assert lifecycle.environment.probes == 1


@pytest.mark.parametrize("fault,reason", [
    ("live-game", "legacy_control_session_lifecycle_unverified"),
    ("late-live-game", "legacy_game_process_absence_unverified"),
    ("invalid-inventory", "legacy_game_process_absence_unverified"),
    ("reused-nongame-pid", "legacy_game_pid_live_or_unreadable"),
    ("unreadable-pid", "legacy_game_pid_live_or_unreadable"),
    ("unreadable-inventory", "legacy_game_process_probe_unreadable"),
    ("missing-strict-probe", "legacy_game_process_probe_unavailable"),
    ("ambiguous-health", "legacy_control_session_exit_unverified"),
    ("contradictory-health", "legacy_control_session_exit_unverified"),
    ("moving-input", "legacy_control_session_input_not_neutral"),
    ("unknown-input", "legacy_control_session_input_not_neutral"),
    ("unbound-record", "legacy_control_session_lifecycle_unverified"),
    ("arbitrary-owner", "legacy_control_session_lifecycle_unverified"),
    ("wrong-controller", "legacy_control_session_lifecycle_unverified"),
    ("wrong-run", "legacy_control_session_lifecycle_unverified"),
    ("unreadable-context", "legacy_control_session_lifecycle_unverified"),
    ("unreadable-lease", "unreadable"),
    ("new-record", "control_session_changed"),
    ("heartbeat-race", "control_session_changed"),
    ("attribution-race", "control_session_changed"),
    ("attribution-removed", "control_session_changed"),
    ("client-removed", "control_session_changed"),
])
def test_unproven_or_changed_legacy_lease_is_never_archived(tmp_path, fault, reason):
    service, lifecycle, record, raw, absent = legacy_runtime(tmp_path, client="peer")
    lease = service._session_lease
    snapshot = lease.status()
    if fault == "live-game":
        lifecycle.environment.processes = {816: "ff7rebirth_.exe"}
    elif fault == "late-live-game":
        lifecycle.environment.game_process_ids_verified = lambda: {816: "ff7rebirth_.exe"}
    elif fault == "invalid-inventory":
        lifecycle.environment.game_process_ids_verified = lambda: []
    elif fault == "reused-nongame-pid":
        absent[0] = False  # a live PID is unsafe even if absent from the game-only inventory
    elif fault == "unreadable-pid":
        absent[0] = None
    elif fault == "unreadable-inventory":
        lifecycle.environment.probe_error = OSError("snapshot failed")
    elif fault == "missing-strict-probe":
        lifecycle.environment.game_process_ids_verified = None
    elif fault == "ambiguous-health":
        service.session_health = lambda: {"process_exists": None, "gameplay_ready": False}
    elif fault == "contradictory-health":
        service.session_health = lambda: {"process_exists": False, "gameplay_ready": True}
    elif fault in {"moving-input", "unknown-input"}:
        service.input_status = lambda: {"real_input": True, "backend_status": (
            {"stick_state": "moving", "last_stick": {"x": 0, "y": 1}} if fault == "moving-input" else {})}
    elif fault == "unbound-record":
        record["game_pid"] = None
        lease.state_path.write_text(json.dumps(record))
        snapshot = lease.status()
    elif fault == "arbitrary-owner":
        record["owner_id"] = "unrelated-controller"
        lease.state_path.write_text(json.dumps(record))
        snapshot = lease.status()
    elif fault in {"wrong-controller", "wrong-run"}:
        path = lifecycle.lease.path if fault == "wrong-controller" else lifecycle.runtime_dir / "run-context.json"
        value = json.loads(path.read_text())
        value["pid"] = 999
        path.write_text(json.dumps(value))
    elif fault == "unreadable-context":
        (lifecycle.runtime_dir / "run-context.json").write_text("[]")
    elif fault == "unreadable-lease":
        lease.state_path.write_text("{invalid")
    elif fault in {"attribution-removed", "client-removed"}:
        record.pop("recording_driver" if fault == "attribution-removed" else "client_id")
        lease.state_path.write_text(json.dumps(record))
    else:
        record[{"new-record": "opened_at", "heartbeat-race": "heartbeat_at",
                "attribution-race": "recording_driver"}[fault]] = (
            {"modelId": "new-driver", "effort": "high"} if fault == "attribution-race" else 1791886590)
        lease.state_path.write_text(json.dumps(record))
    untouched = lease.state_path.read_bytes()
    lifecycle_bytes = lifecycle.lease.path.read_bytes()
    with pytest.raises(RuntimeError, match=reason):
        recover_stale_lifecycle(service, lifecycle, snapshot)
    assert lease.state_path.read_bytes() == untouched
    assert lifecycle.lease.path.read_bytes() == lifecycle_bytes
    assert not list(lease.runtime_dir.glob("control-session.*-*.json"))


class LegacyStartupHarness(Harness):
    def __init__(self, snapshot):
        super().__init__(False)
        self.lease = snapshot
        self.overrides["get_runtime_identity"] = {
            "git_commit_sha": "sha", "expected_feature_ref": "ref", "repository_root": "root",
            "bridge_status": {"active_async_request": None}}
        self.overrides["get_lifecycle_status"] = {
            "lease": {"pid": 25412}, "owned_process_running": False,
            "run_context": {"pid": 816}}
        self.overrides["get_session_health"] = {"process_exists": False, "gameplay_ready": False}

    async def __call__(self, operation, arguments=None):
        if operation == "recover_stale_lifecycle":
            self.calls.append((operation, arguments))
            assert arguments == {"expected_control_session": self.lease}
            self.lease = {"active": False, "reason": "no_session"}
            return {"recovered": True, "control_session": {
                "closed": True, "raw_archive_path": "synthetic-raw-archive.json"}}
        result = await super().__call__(operation, arguments)
        if operation == "get_start_model_run_status":
            self.overrides["get_session_health"] = {"gameplay_ready": True, "process_exists": True}
        return result


def test_startup_recovers_legacy_dead_game_before_driver_load_and_foreign_client_gate(tmp_path):
    service, _, _, _, _ = legacy_runtime(tmp_path, client="foreign")
    harness, services = LegacyStartupHarness(service._session_lease.status()), Services()
    result = asyncio.run(startup(harness, services, options(), sleep=no_sleep, identity_validator=validate))
    operations = [op for op, _ in harness.calls]
    assert result["ready"] is True and result["game_pid"] == 101
    assert result["control_recovery"]["control_session"]["raw_archive_path"]
    assert operations.count("recover_stale_lifecycle") == 1
    assert operations.index("recover_stale_lifecycle") < operations.index("say_on_stream") < operations.index("start_model_run_async")
    assert result["control_session"]["client_id"] == "session-test"


@pytest.mark.parametrize("fault,reason", [
    ("active-job", "another_input_job_is_running"),
    ("missing-job-evidence", "another_input_job_is_running"),
    ("owned-game-running", "legacy_control_session_exit_unverified"),
    ("wrong-game-context", "legacy_control_session_exit_unverified"),
    ("check-only", "legacy_control_session_recovery_required"),
    ("live-peer", "another_client_owns_control"),
    ("unbound-peer", "another_client_owns_control"),
    ("failed-recovery", "legacy_control_session_recovery_failed"),
])
def test_startup_cannot_turn_unproven_recovery_into_input(tmp_path, fault, reason):
    service, _, _, _, _ = legacy_runtime(tmp_path, client="peer")
    harness, services = LegacyStartupHarness(service._session_lease.status()), Services()
    opts = options()
    if fault == "active-job":
        harness.overrides["get_runtime_identity"]["bridge_status"]["active_async_request"] = {"job_id": "pending"}
    elif fault == "missing-job-evidence":
        harness.overrides["get_runtime_identity"].pop("bridge_status")
    elif fault == "owned-game-running":
        harness.overrides["get_lifecycle_status"]["owned_process_running"] = True
    elif fault == "wrong-game-context":
        harness.overrides["get_lifecycle_status"]["run_context"]["pid"] = 999
    elif fault == "check-only":
        opts.check_only = True
    elif fault == "live-peer":
        harness.overrides["get_session_health"] = {"process_exists": True, "gameplay_ready": True}
    elif fault == "unbound-peer":
        harness.lease["game_pid"] = None

    async def call(operation, arguments=None):
        if operation == "recover_stale_lifecycle" and fault == "failed-recovery":
            harness.calls.append((operation, arguments))
            return {"ok": False, "recovered": False}
        return await harness(operation, arguments)

    with pytest.raises(StartupBlocked, match=reason):
        asyncio.run(startup(call, services, opts, sleep=no_sleep, identity_validator=validate))
    assert not services.actions
    assert not {"open_control_session", "close_control_session", "start_model_run_async",
                "stop_all_input", "say_on_stream"} & {op for op, _ in harness.calls}
    if fault != "failed-recovery":
        assert "recover_stale_lifecycle" not in [op for op, _ in harness.calls]


class Win32Function:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


@pytest.mark.parametrize("handle,error,readable,exit_code,absent", [
    (None, 87, False, 0, True),   # proven missing PID
    (None, 5, False, 0, False),   # access denied
    (None, 6, False, 0, False),   # any other Win32 failure is unknown
    (42, 0, True, 259, False),   # live PID, including PID reuse
    (42, 0, True, 1, True),      # retained handle to an exited process
    (42, 0, False, 0, False),    # unreadable exit code
])
def test_strict_windows_pid_probe_never_confuses_errors_or_pid_reuse_with_exit(
        monkeypatch, handle, error, readable, exit_code, absent):
    from ff7r_agent import runtime_ownership
    closed = []

    def exit_status(_handle, pointer):
        pointer._obj.value = exit_code
        return readable

    kernel32 = SimpleNamespace(
        OpenProcess=Win32Function(lambda *_: handle),
        GetExitCodeProcess=Win32Function(exit_status),
        GetLastError=Win32Function(lambda: error),
        CloseHandle=Win32Function(lambda process: closed.append(process)))
    monkeypatch.setattr(runtime_ownership, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(runtime_ownership.ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    assert runtime_ownership.process_absence_verified(816) is absent
    assert closed == ([handle] if handle else [])


@pytest.mark.parametrize("pid", [None, 0, -1, "816", True])
def test_strict_pid_probe_requires_a_positive_integer(pid):
    from ff7r_agent.runtime_ownership import process_absence_verified
    assert process_absence_verified(pid) is False
