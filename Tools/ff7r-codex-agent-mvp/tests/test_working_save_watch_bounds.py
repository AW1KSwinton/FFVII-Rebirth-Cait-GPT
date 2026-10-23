"""Offline RPC faults; every fake crash is confined to a temporary Game."""
import asyncio
import json

import pytest

from ff7r_agent import working_save_recovery as recovery
from ff7r_agent.working_save_watch import watch
from test_working_save import Game, fake_startup


class Rpc:
    def __init__(self, game):
        self.game, self.calls, self.armed = game, [], {}
        self.fault = lambda operation, value: value
        self.block = None
        self.cancelled = []
        self.clock = [0.0]
        self.stop_delay = 0.0

    async def __call__(self, operation, arguments):
        self.calls.append(operation)
        if operation == self.block:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.append(operation)
                raise
        if operation in {"get_runtime_identity", "get_bootstrap_verification"}:
            value = {"ok": True}
        elif operation == "arm_working_save_recovery":
            self.armed = recovery.arm(self.game)
            self.game._environment.ids = {}
            value = self.armed
        elif operation == "stop_all_input":
            self.clock[0] += self.stop_delay
            value = {"id": 1, "result": self.game.stop()}
        elif operation == "recover_working_save_async":
            value = {"ok": True, "job_id": "recovery-test"}
        elif operation == "get_recover_working_save_status":
            value = {"ok": True, "job_id": "recovery-test", "state": "completed", "response": {"id": 2, "result": {
                "ok": True, "state": "recovered", "slot": 8, "recovery_id": self.armed["recovery_id"]}}}
        else:
            raise AssertionError(operation)
        return self.fault(operation, value)

    def run(self):
        return asyncio.run(watch(self, self.game._environment, identity_check=lambda *args: {"ok": True},
            watch_seconds=1, recovery_seconds=1, rpc_timeout_seconds=0.01, clock=lambda: self.clock[0]))


@pytest.mark.parametrize("operation", ["get_runtime_identity", "get_bootstrap_verification",
    "arm_working_save_recovery", "stop_all_input", "recover_working_save_async", "get_recover_working_save_status"])
def test_stalled_rpc_is_cancelled_and_never_triggers_another_restart(tmp_path, operation):
    rpc = Rpc(Game(tmp_path))
    rpc.block = operation
    result = rpc.run()
    assert result["decision"] == "watch_rpc_timeout:" + operation
    assert operation in rpc.cancelled
    assert rpc.calls.count("recover_working_save_async") <= 1
    if rpc.armed:
        assert rpc.calls.count("stop_all_input") >= 1
        public = json.loads((rpc.game._public_stream_event_dir.parent / "public-working-save-recovery.json").read_text())
        assert public["phase"] == "failed"


@pytest.mark.parametrize("defect", ["stop_refused", "stop_missing", "start_refused", "start_missing_job",
    "status_refused", "wrong_job", "wrong_recovery", "wrong_slot"])
def test_nested_stop_start_and_completion_failures_hold_brb(tmp_path, defect):
    rpc = Rpc(Game(tmp_path))

    def fault(operation, value):
        if operation == "stop_all_input" and defect.startswith("stop_"):
            value["result"] = {"ok": False} if defect == "stop_refused" else {}
        if operation == "recover_working_save_async":
            if defect == "start_refused":
                value["ok"] = False
            if defect == "start_missing_job":
                value.pop("job_id")
        if operation == "get_recover_working_save_status":
            if defect == "status_refused":
                value["ok"] = False
            if defect == "wrong_job":
                value["job_id"] = "different-job"
            if defect == "wrong_recovery":
                value["response"]["result"]["recovery_id"] = "f" * 32
            if defect == "wrong_slot":
                value["response"]["result"]["slot"] = 13
        return value

    rpc.fault = fault
    result = rpc.run()
    assert not result["ok"] and result["requires_review"]
    if defect.startswith("stop_"):
        assert "recover_working_save_async" not in rpc.calls
    assert rpc.calls.count("recover_working_save_async") <= 1
    public = json.loads((rpc.game._public_stream_event_dir.parent / "public-working-save-recovery.json").read_text())
    assert public["phase"] == "failed"


def test_stop_time_is_part_of_the_total_recovery_budget(tmp_path):
    rpc = Rpc(Game(tmp_path))
    rpc.stop_delay = 2.0
    assert rpc.run()["decision"] == "watch_recovery_timeout"
    assert "recover_working_save_async" not in rpc.calls


@pytest.mark.parametrize("when", ["arm_working_save_recovery", "stop_all_input"])
def test_save_as_refresh_is_used_instead_of_the_original_armed_slot(tmp_path, when):
    rpc = Rpc(Game(tmp_path))

    def refresh(operation, value):
        if operation == when:
            # Simulate a successful Save As before the observed loss. The run
            # keeps its watcher ID while its verified target advances to 9.
            rpc.game._environment.ids = {42: "ff7rebirth_.exe"}
            rpc.game.path(9).write_bytes(b"verified-save-as")
            file = {"slot": 9, "steam_id": "7656", "path": str(rpc.game.path(9))}
            with rpc.game._queen_blood_runs.mutation_lock():
                rpc.game._queen_blood_runs.record_verified_save(rpc.game.driver, file, rpc.armed["process"])
            refreshed = recovery.arm(rpc.game)
            assert refreshed["recovery_id"] == rpc.armed["recovery_id"]
            rpc.game._environment.ids = {}
        if operation == "get_recover_working_save_status":
            value["response"]["result"]["slot"] = 9
        return value

    rpc.fault = refresh
    result = rpc.run()
    assert result["ok"] and result["slot"] == 9
    assert rpc.calls.count("recover_working_save_async") == 1


def test_cancellation_during_recovery_has_bounded_cleanup_and_holds_brb(tmp_path):
    rpc = Rpc(Game(tmp_path))

    def cancel(operation, value):
        if operation == "get_recover_working_save_status":
            rpc.block = "stop_all_input"
            raise asyncio.CancelledError()
        return value

    rpc.fault = cancel
    with pytest.raises(asyncio.CancelledError):
        rpc.run()
    assert rpc.cancelled == ["stop_all_input"]
    assert rpc.calls.count("recover_working_save_async") == 1
    public = json.loads((rpc.game._public_stream_event_dir.parent / "public-working-save-recovery.json").read_text())
    assert public["phase"] == "failed"


@pytest.mark.parametrize("change", ["disarmed", "different_recovery", "different_process", "invalid_version"])
def test_target_replacement_during_input_stop_never_restarts(tmp_path, change):
    from ff7r_agent.queen_blood_profile import _read, _write
    rpc = Rpc(Game(tmp_path))

    def changed(operation, value):
        if operation == "stop_all_input":
            path = recovery.receipt_path(rpc.game)
            record = _read(path)
            if change == "disarmed":
                record["phase"] = "disarmed"
            elif change == "different_recovery":
                record["recovery_id"] = "f" * 32
            elif change == "different_process":
                record["process"] = {"pid": "43", "token": "new-process"}
            else:
                record["version"] = 2
            _write(path, record)
        return value

    rpc.fault = changed
    result = rpc.run()
    assert result["decision"] == "watch_recovery_target_changed"
    assert "recover_working_save_async" not in rpc.calls
    public = _read(rpc.game._public_stream_event_dir.parent / "public-working-save-recovery.json")
    assert public["phase"] == "failed"


@pytest.mark.parametrize("ids", [None, [], {"42": "ff7rebirth_.exe"}, {42: None}, {42: ""}, {True: "other"}])
def test_unknown_process_inventory_never_proves_loss(tmp_path, ids):
    environment = Game(tmp_path)._environment
    environment.game_process_ids_verified = lambda: ids
    with pytest.raises(ValueError, match="recovery_process_probe_invalid"):
        recovery.process_lost(environment, {"pid": "42", "token": "created-42"})


def test_expected_pid_with_wrong_executable_is_not_a_verified_game(tmp_path):
    environment = Game(tmp_path)._environment
    environment.ids[42] = "different.exe"
    with pytest.raises(ValueError, match="recovery_process_identity_changed_or_unknown"):
        recovery.process_lost(environment, {"pid": "42", "token": "created-42"})


def test_save_changes_during_reload_hold_brb_and_preserve_failure_evidence(tmp_path):
    game = Game(tmp_path)
    armed = recovery.arm(game)
    game._environment.ids = {}
    startup, calls = fake_startup(game)
    start = startup.start_and_load_save_slot

    def changed(**kwargs):
        result = start(**kwargs)
        game.path(8).write_bytes(b"changed-while-reloading")
        # An independently refreshed receipt must still not change the armed file.
        file = {"slot": 8, "steam_id": "7656", "path": str(game.path(8))}
        candidate = game._queen_blood_runs.begin_load(file, recovery.process_identity(game._environment))
        game._queen_blood_runs.complete_load(candidate, recovery.process_identity(game._environment))
        return result

    startup.start_and_load_save_slot = changed
    result = recovery.recover(startup, recovery_id=armed["recovery_id"])
    assert result["decision"] == "recovery_loaded_save_unverified" and result["requires_review"]
    assert len(calls) == 2
    private = json.loads(recovery.receipt_path(game).read_text())
    assert private["phase"] == "failed" and private["decision"] == result["decision"]
    assert private["result"]["startup"]["ok"] is True
    public = json.loads((game._public_stream_event_dir.parent / "public-working-save-recovery.json").read_text())
    assert public["phase"] == "failed"
