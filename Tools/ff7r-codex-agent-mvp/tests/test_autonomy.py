from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ff7r_agent.autonomy import (
    AssertionEvaluator,
    AutonomyError,
    EvidenceBundle,
    FixtureBroker,
    LifecycleController,
    LifecycleLease,
    sha256_file,
)


def test_lifecycle_lease_never_reclaims_existing_owner(tmp_path: Path):
    first = LifecycleLease(tmp_path, "owner-one")
    record = first.acquire()
    assert record["owner_id"] == "owner-one"
    second = LifecycleLease(tmp_path, "owner-two")
    with pytest.raises(AutonomyError, match="REFUSED_OWNER_ACTIVE_OR_UNKNOWN"):
        second.acquire()
    first.release()
    assert first.status() is None


def test_fixture_clone_is_hash_verified_and_does_not_overwrite(tmp_path: Path):
    source = tmp_path / "source.sav"
    source.write_bytes(b"disposable-save-fixture")
    manifest = FixtureBroker().clone(source, tmp_path / "fixtures", "fixture-one")
    assert manifest.source_sha256 == manifest.target_sha256
    assert Path(manifest.target_path).read_bytes() == source.read_bytes()
    with pytest.raises(AutonomyError, match="REFUSED_OVERWRITE"):
        FixtureBroker().clone(source, tmp_path / "fixtures", "fixture-one")


def test_assertions_and_evidence_seal(tmp_path: Path):
    observation = {"telemetry": {"live": True}, "sequence": 12}
    result = AssertionEvaluator().evaluate(observation, [
        {"path": "telemetry.live", "op": "eq", "value": True},
        {"path": "sequence", "op": "gt", "value": 10},
    ])
    assert result["passed"] is True

    bundle = EvidenceBundle(tmp_path / "evidence", "run-one", {"run_id": "run-one"})
    bundle.record("observation", observation)
    bundle.seal()
    assert bundle.verify()["valid"] is True
    with (bundle.root / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("tampered\n")
    assert bundle.verify()["valid"] is False


class FakeEnvironment:
    def __init__(self, process: bool = False):
        self.process = process

    def game_process_exists(self) -> bool:
        return self.process


class OwnedOrphanEnvironment:
    def __init__(self):
        self.processes = {
            1111: "ff7rebirth_.exe",
            2222: "ff7rebirth.exe",
        }

    def game_process_ids(self) -> dict[int, str]:
        return dict(self.processes)

    def game_process_exists(self) -> bool:
        return bool(self.processes)

    def request_graceful_close(self) -> dict[str, object]:
        return {
            "requested": False,
            "state": "manual_end_task_required",
            "reason": "game_window_not_found",
            "processes": dict(self.processes),
            "windows": [],
        }

    def terminate_game_processes(self, pids):
        terminated = [pid for pid in pids if pid in self.processes]
        for pid in terminated:
            self.processes.pop(pid, None)
        return {
            "ok": True,
            "state": "termination_requested",
            "requested_pids": list(pids),
            "terminated_pids": terminated,
            "failures": [],
        }


def test_controller_recovers_its_stale_lease_only_when_game_is_absent(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    lease = LifecycleLease(runtime, "owner-one")
    record = lease.acquire()
    (runtime / "run-context.json").write_text(json.dumps({
        "lease_id": record["lease_id"],
        "owner_id": "owner-one",
        "run_id": "run-one",
    }), encoding="utf-8")

    controller = LifecycleController(
        tmp_path / "ff7rebirth_.exe", tmp_path / "state.json", runtime,
        owner_id="owner-one", environment=FakeEnvironment(process=False),
    )
    result = controller.recover_stale()
    assert result["recovered"] is True
    assert not (runtime / "lifecycle-lease.json").exists()
    assert list(runtime.glob("lifecycle-lease.stale-*.json"))
    context = json.loads((runtime / "run-context.json").read_text(encoding="utf-8"))
    assert context["state"] == "stale_recovered"


def test_controller_refuses_stale_recovery_while_game_exists(tmp_path: Path):
    runtime = tmp_path / "runtime"
    lease = LifecycleLease(runtime, "owner-one")
    lease.acquire()
    controller = LifecycleController(
        tmp_path / "ff7rebirth_.exe", tmp_path / "state.json", runtime,
        owner_id="owner-one", environment=FakeEnvironment(process=True),
    )
    with pytest.raises(AutonomyError, match="REFUSED_GAME_RUNNING"):
        controller.recover_stale()


def test_controller_recovers_owned_lease_when_attached_context_has_no_lease_id(tmp_path: Path):
    """An exited attached run must not permanently block the next startup."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    lease = LifecycleLease(runtime, "owner-one")
    record = lease.acquire()
    (runtime / "run-context.json").write_text(json.dumps({
        "lease_id": None,
        "owner_id": "owner-one",
        "run_id": "attach-one",
        "telemetry_binding": "attached-existing-process",
    }), encoding="utf-8")
    controller = LifecycleController(
        tmp_path / "ff7rebirth_.exe", tmp_path / "state.json", runtime,
        owner_id="owner-one", environment=FakeEnvironment(process=False),
    )
    result = controller.recover_stale()
    assert result["recovered"] is True
    assert result["warning"] == "lease_context_mismatch"
    context = json.loads((runtime / "run-context.json").read_text(encoding="utf-8"))
    assert context["recovery_warning"] == "lease_context_mismatch"


class FakeProcess:
    pid = 4242

    def poll(self):
        return None


def test_controller_start_records_identity_without_force_close(tmp_path: Path, monkeypatch):
    executable = tmp_path / "ff7rebirth_.exe"
    executable.write_bytes(b"approved-test-executable")
    telemetry = tmp_path / "state.json"
    captured = {}

    def fake_popen(command, cwd, creationflags):
        captured["command"] = command
        captured["cwd"] = cwd
        return FakeProcess()

    monkeypatch.setattr("ff7r_agent.autonomy.subprocess.Popen", fake_popen)
    controller = LifecycleController(
        executable,
        telemetry,
        tmp_path / "runtime",
        environment=FakeEnvironment(),
    )
    context = controller.start(["-windowed"])
    assert context["pid"] == 4242
    assert context["executable_sha256"]
    assert captured["command"][-1] == "-windowed"
    with pytest.raises(AutonomyError, match="CLEANUP_REFUSED_PROCESS_STILL_RUNNING"):
        controller.release()


def test_attached_context_lazily_verifies_and_persists_executable_identity(tmp_path: Path, monkeypatch):
    executable = tmp_path / "ff7rebirth_.exe"
    executable.write_bytes(b"approved-attached-executable")
    controller = LifecycleController(
        executable,
        tmp_path / "state.json",
        tmp_path / "runtime",
        owner_id="owner-one",
        environment=OwnedOrphanEnvironment(),
    )
    attached = controller.ensure_started(allow_launch=False)
    assert attached["mode"] == "attached"
    assert attached["context"].get("executable_sha256") is None

    expected = sha256_file(executable)
    verified = controller.verify_executable_identity(expected)
    assert verified["verified"] is True
    assert verified["source"] == "stable_sha256_lazy_gameplay_gate"
    context = json.loads((tmp_path / "runtime" / "run-context.json").read_text(encoding="utf-8"))
    assert context["executable_sha256"] == expected
    assert context["executable_identity_verified"] is True

    def should_not_hash(*_args, **_kwargs):
        raise AssertionError("attached executable was hashed more than once")

    monkeypatch.setattr("ff7r_agent.autonomy.stable_sha256", should_not_hash)
    reused = controller.verify_executable_identity(expected)
    assert reused["verified"] is True
    assert reused["source"] == "durable_run_context"


def test_attached_executable_identity_mismatch_is_fail_closed(tmp_path: Path):
    executable = tmp_path / "ff7rebirth_.exe"
    executable.write_bytes(b"different-attached-executable")
    controller = LifecycleController(
        executable,
        tmp_path / "state.json",
        tmp_path / "runtime",
        owner_id="owner-one",
        environment=OwnedOrphanEnvironment(),
    )
    controller.ensure_started(allow_launch=False)
    result = controller.verify_executable_identity("0" * 64)
    assert result["verified"] is False
    assert result["reason"] == "executable_identity_mismatch"
    assert result["sha256"] == sha256_file(executable)


def test_lazy_identity_rehydrates_durable_attached_context(tmp_path: Path):
    executable = tmp_path / "ff7rebirth_.exe"
    executable.write_bytes(b"attached-executable")
    telemetry = tmp_path / "state.json"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    context = {
        "version": 1,
        "run_id": "attached-run",
        "owner_id": "owner-one",
        "lease_id": None,
        "attachment": "external_or_prior_task",
        "pid": 1111,
        "launcher_pid": 2222,
        "executable_path": str(executable.resolve()),
        "launcher_path": str(executable.resolve()),
        "telemetry_path": str(telemetry.resolve()),
    }
    (runtime / "run-context.json").write_text(json.dumps(context), encoding="utf-8")
    controller = LifecycleController(
        executable,
        telemetry,
        runtime,
        owner_id="owner-one",
        environment=OwnedOrphanEnvironment(),
    )

    expected = sha256_file(executable)
    result = controller.verify_executable_identity(expected)

    assert result["verified"] is True
    assert result["source"] == "stable_sha256_lazy_gameplay_gate"
    persisted = json.loads((runtime / "run-context.json").read_text(encoding="utf-8"))
    assert persisted["executable_sha256"] == expected


def test_controller_start_failure_releases_lease(tmp_path: Path, monkeypatch):
    executable = tmp_path / "ff7rebirth_.exe"
    executable.write_bytes(b"approved-test-executable")

    def failing_popen(command, cwd, creationflags):
        raise OSError("launcher failed")

    monkeypatch.setattr("ff7r_agent.autonomy.subprocess.Popen", failing_popen)
    controller = LifecycleController(
        executable,
        tmp_path / "state.json",
        tmp_path / "runtime",
        environment=FakeEnvironment(),
    )
    with pytest.raises(OSError, match="launcher failed"):
        controller.start()
    assert controller.lease.status() is None
    assert not (tmp_path / "runtime" / "run-context.json").exists()


def test_controller_preserves_owned_game_after_graceful_close_fails(tmp_path: Path):
    executable = tmp_path / "ff7rebirth_.exe"
    executable.write_bytes(b"approved-test-executable")
    runtime = tmp_path / "runtime"
    environment = OwnedOrphanEnvironment()
    controller = LifecycleController(
        executable,
        tmp_path / "state.json",
        runtime,
        owner_id="owner-one",
        environment=environment,
    )
    lease = controller.lease.acquire()
    context = {
        "version": 1,
        "run_id": "run-one",
        "owner_id": "owner-one",
        "lease_id": lease["lease_id"],
        "pid": 1111,
        "launcher_pid": 2222,
        "executable_path": str(executable.resolve()),
        "launcher_path": str(executable.resolve()),
        "telemetry_path": str((tmp_path / "state.json").resolve()),
    }
    (runtime / "run-context.json").write_text(json.dumps(context), encoding="utf-8")

    result = controller.close_game(timeout_seconds=0.1, force=False)

    assert result["ok"] is False
    assert result["state"] == "manual_end_task_required"
    assert result["termination"]["terminated_pids"] == []
    assert set(environment.processes) == {1111, 2222}
    assert controller.lease.status() is not None


def test_attach_keeps_external_context_but_allows_bound_game_close(tmp_path: Path):
    executable = tmp_path / "ff7rebirth_.exe"
    executable.write_bytes(b"approved-test-executable")
    telemetry = tmp_path / "state.json"
    runtime = tmp_path / "runtime"
    environment = OwnedOrphanEnvironment()
    controller = LifecycleController(
        executable,
        telemetry,
        runtime,
        owner_id="owner-one",
        environment=environment,
    )
    controller.lease.acquire()
    (runtime / "run-context.json").write_text(json.dumps({
        "version": 1,
        "run_id": "unbound-attach",
        "owner_id": "owner-one",
        "lease_id": None,
        "pid": 1111,
        "launcher_pid": 2222,
        "executable_path": str(executable.resolve()),
        "launcher_path": str(executable.resolve()),
        "telemetry_path": str(telemetry.resolve()),
    }), encoding="utf-8")

    attached = controller.ensure_started(allow_launch=False)

    assert attached["mode"] == "attached"
    assert attached["context"]["attachment"] == "external_or_prior_task"
    assert controller._can_force_close_owned_run() is False


def test_controller_waits_for_loaded_active_gameplay(tmp_path: Path, monkeypatch):
    executable = tmp_path / "ff7rebirth_.exe"
    executable.write_bytes(b"approved-test-executable")
    telemetry = tmp_path / "state.json"
    controller = LifecycleController(
        executable,
        telemetry,
        tmp_path / "runtime",
        environment=FakeEnvironment(),
    )

    monkeypatch.setattr(
        "ff7r_agent.autonomy.subprocess.Popen",
        lambda command, cwd, creationflags: FakeProcess(),
    )
    controller.start()

    def payload(sequence: int) -> dict:
        return {
            "schema_version": 1,
            "sequence": sequence,
                "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "game": {
                "running": True,
                "paused": False,
                "in_menu": False,
                "map_id": "EndGame",
                "player": {"position": {"x": 1, "y": 2, "z": 3}},
            },
        }

    telemetry.write_text(json.dumps(payload(1)), encoding="utf-8")
    next_sequence = 1

    def fake_sleep(_seconds):
        nonlocal next_sequence
        next_sequence += 1
        telemetry.write_text(json.dumps(payload(next_sequence)), encoding="utf-8")

    monkeypatch.setattr("ff7r_agent.autonomy.time.sleep", fake_sleep)
    result = controller.wait_for_gameplay_ready(timeout_seconds=1.0, poll_seconds=0.01)
    assert result["sequence"] == 3
    assert result["run_binding"]["readiness"] == "settled_loaded_active_gameplay"
    assert result["run_binding"]["stable_samples"] == 3
