import json
import os
import time

import pytest

from ff7r_agent import input as input_module
from ff7r_agent import safety
from ff7r_agent.recording_driver import validate_recording_driver
from ff7r_agent.safety import ArmError, SessionLease


@pytest.fixture
def recording(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("FF7R_AUTONOMY_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("FF7R_OBS_STATE_DIR", str(tmp_path / "obs"))
    monkeypatch.setenv("FF7R_OBS_INPUT_CLIPS", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("FF7R_RECORDING_MODEL", raising=False)
    monkeypatch.delenv("FF7R_RECORDING_EFFORT", raising=False)
    return SessionLease(runtime_dir=tmp_path / "runtime"), tmp_path / "obs" / "input-events"


def events(directory):
    return [json.loads(file.read_text()) for file in sorted(directory.glob("*.json"))]


def mark(monkeypatch):
    # The marker writer thread's write, run directly: it reads the lease's
    # declaration as it writes, with all paths in the test's directory.
    from datetime import datetime, timezone
    input_module._write_obs_marker(datetime.now(timezone.utc))


def test_input_snapshots_survive_model_changes_and_do_not_credit_sessions_of_an_exited_game(recording, monkeypatch):
    lease, directory = recording
    lease.open(60, game_pid=os.getpid(), model_id="gpt-6.1-sol", reasoning_effort="high")
    mark(monkeypatch)
    lease.heartbeat(60, model_id="claude-sonnet-4-5-20250929", reasoning_effort="medium")
    mark(monkeypatch)
    recorded = events(directory)
    assert {json.dumps(event["driver"], sort_keys=True) for event in recorded} == {
        json.dumps({"modelId": "gpt-6.1-sol", "effort": "high"}, sort_keys=True),
        json.dumps({"modelId": "claude-sonnet-4-5-20250929", "effort": "medium"}, sort_keys=True),
    }
    # The game this session was bound to has exited.
    monkeypatch.setattr(safety, "owner_is_alive", lambda pid, token: False)
    mark(monkeypatch)
    assert len(events(directory)) == 3
    assert sum(event["driver"] is None for event in events(directory)) == 1


def test_heartbeat_retains_declaration_and_only_an_immediate_reopen_inherits_it(recording, tmp_path):
    now = [1000.0]
    lease = SessionLease(runtime_dir=tmp_path / "runtime", clock=lambda: now[0])
    expected = {"modelId": "gpt-6.1-sol", "effort": "high"}
    lease.open(60, model_id="gpt-6.1-sol", reasoning_effort="high")
    assert lease.heartbeat(60)["recording_driver"] == expected
    lease.close()
    now[0] += 5  # routes and approaches reopen the lease undeclared within seconds
    assert lease.open(60)["recording_driver"] == expected
    now[0] += 1
    lease.close()
    now[0] += SessionLease.DRIVER_CARRY_SECONDS + 5
    assert lease.open(60)["recording_driver"] is None


def test_launcher_defaults_are_snapshotted_at_open(recording, monkeypatch):
    lease, _ = recording
    monkeypatch.setenv("FF7R_RECORDING_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("FF7R_RECORDING_EFFORT", "xhigh")
    lease.open(60)
    monkeypatch.setenv("FF7R_RECORDING_EFFORT", "low")
    assert lease.heartbeat(60)["recording_driver"] == {"modelId": "gpt-6.1-sol", "effort": "xhigh"}


@pytest.mark.parametrize("model,effort", [("auto", "high"), ("sonnet", "medium"), ("gpt-6.1-sol", None), (None, "high"), ("../../secret", "high"), ("gpt-6.1-sol", "guess")])
def test_invalid_attribution_does_not_open_or_extend_control(recording, model, effort):
    lease, _ = recording
    with pytest.raises(ArmError):
        lease.open(60, model_id=model, reasoning_effort=effort)
    assert not lease.state_path.exists()
    lease.open(60, model_id="gpt-6.1-sol", reasoning_effort="high")
    before = lease.state_path.read_bytes()
    with pytest.raises(ArmError):
        lease.heartbeat(60, model_id=model, reasoning_effort=effort)
    assert lease.state_path.read_bytes() == before


def test_missing_or_malformed_declaration_does_not_block_input_markers(recording, monkeypatch):
    lease, directory = recording
    mark(monkeypatch)
    lease.open(60)
    state = json.loads(lease.state_path.read_text())
    state["recording_driver"] = {"modelId": "auto", "effort": "high"}
    lease.state_path.write_text(json.dumps(state))
    mark(monkeypatch)
    assert len(events(directory)) == 2
    assert all(event["driver"] is None for event in events(directory))


def test_supported_efforts_preserve_exact_model_id():
    for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
        assert validate_recording_driver("exact_model.20261004", effort) == {"modelId": "exact_model.20261004", "effort": effort}
