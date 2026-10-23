import asyncio
import json

import pytest

from ff7r_agent import working_save_recovery as recovery
from ff7r_agent.working_save_watch import watch, main
from test_working_save import Game, fake_startup


@pytest.mark.parametrize("failure", [None, "nested_failure", "missing_response", "timeout"])
def test_watcher_switches_brb_before_cancelling_busy_input_and_requires_nested_reload(tmp_path, failure):
    game = Game(tmp_path)
    startup, calls = fake_startup(game)
    clock = [0.0]
    operations, armed, result = [], {}, {}

    async def rpc(operation, arguments):
        operations.append(operation)
        if operation in {"get_runtime_identity", "get_bootstrap_verification"}:
            return {"ok": True}
        if operation == "arm_working_save_recovery":
            armed.update(recovery.arm(game))
            game._environment.ids = {}
            return armed
        if operation == "stop_all_input":
            public = json.loads((game._public_stream_event_dir.parent / "public-working-save-recovery.json").read_text())
            assert public["phase"] in {"recovering", "failed"}
            return {"id": 1, "result": game.stop()}
        if operation == "recover_working_save_async":
            if failure is None:
                result.update(recovery.recover(startup, **arguments))
            return {"ok": True, "job_id": "recovery-1"}
        if operation == "get_recover_working_save_status":
            assert arguments == {"job_id": "recovery-1"}
            if failure == "timeout":
                return {"ok": True, "job_id": "recovery-1", "state": "running"}
            payload = {"ok": False, "decision": "load_failed"} if failure == "nested_failure" else result
            return {"ok": True, "job_id": "recovery-1", "state": "completed", "response": {"id": 2, "result": payload}}
        raise AssertionError(operation)

    async def sleep(seconds):
        clock[0] += seconds

    response = asyncio.run(watch(rpc, game._environment, identity_check=lambda *args: {"ok": True},
        watch_seconds=1, recovery_seconds=1, clock=lambda: clock[0], sleep=sleep))
    assert response["ok"] == (failure is None)
    assert operations.index("stop_all_input") < operations.index("recover_working_save_async")
    assert operations.count("recover_working_save_async") == 1
    phase = json.loads((game._public_stream_event_dir.parent / "public-working-save-recovery.json").read_text())["phase"]
    assert phase == ("ready" if failure is None else "failed")
    assert not calls if failure else calls


@pytest.mark.parametrize("reason", ["identity", "intentional_exit", "timeout"])
def test_watcher_never_recovers_without_proven_loss(tmp_path, reason):
    game, clock, operations = Game(tmp_path), [0.0], []

    async def rpc(operation, arguments):
        operations.append(operation)
        if operation in {"get_runtime_identity", "get_bootstrap_verification"}:
            return {"ok": True}
        assert operation == "arm_working_save_recovery"
        armed = recovery.arm(game)
        if reason == "intentional_exit":
            recovery.disarm(game)
            game._environment.ids = {}
        return armed

    async def sleep(seconds):
        clock[0] += seconds

    response = asyncio.run(watch(rpc, game._environment,
        identity_check=lambda *args: {"ok": reason != "identity"},
        watch_seconds=1, recovery_seconds=1, clock=lambda: clock[0], sleep=sleep))
    assert "stop_all_input" not in operations and "recover_working_save_async" not in operations
    assert response["ok"] == (reason != "identity")
    if reason != "identity":
        assert json.loads(recovery.receipt_path(game).read_text())["phase"] == "disarmed"


def test_watch_cli_dry_run_has_no_service_or_game_calls(capsys):
    assert main(["--feature-ref", "codex/working-save-recovery", "--expected-commit", "offline"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "state": "dry_run", "input_sent": False}


@pytest.mark.parametrize("failure", ["pid_reuse", "replacement", "enumeration_failed"])
def test_ambiguous_process_stops_without_input_and_holds_brb(tmp_path, failure):
    game, operations = Game(tmp_path), []

    async def rpc(operation, arguments):
        operations.append(operation)
        if operation in {"get_runtime_identity", "get_bootstrap_verification"}:
            return {"ok": True}
        assert operation == "arm_working_save_recovery"
        armed = recovery.arm(game)
        if failure == "pid_reuse":
            game._environment.tokens[42] = "different-process"
        elif failure == "replacement":
            game._environment.ids = {55: "ff7rebirth_.exe"}
        else:
            def unavailable():
                raise OSError("enumeration unavailable")
            game._environment.game_process_ids_verified = unavailable
        return armed

    result = asyncio.run(watch(rpc, game._environment,
        identity_check=lambda *args: {"ok": True}, watch_seconds=1, recovery_seconds=1))
    assert not result["ok"] and result["requires_review"]
    assert "stop_all_input" not in operations and "recover_working_save_async" not in operations
    public = json.loads((game._public_stream_event_dir.parent / "public-working-save-recovery.json").read_text())
    assert public["phase"] == "failed"
    assert json.loads(recovery.receipt_path(game).read_text())["phase"] == "disarmed"
