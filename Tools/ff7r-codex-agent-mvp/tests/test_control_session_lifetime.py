"""A control session lasts for the life of the game, not a wall-clock lease.

Live 2026-10-13: a mission closed the session it had opened, which latched the
stop; the cutscene watcher then failed twenty skips in two seconds against a
lock nothing could lift, and nobody saw it. These pin the replacement: the
session is bound to the game process, tasks borrow it and leave it open, only
an explicit close or stop latches, and a refusal is reported once.
"""

import asyncio
import json
import os
import time

import pytest

from ff7r_agent import safety
from ff7r_agent.navigation_mission import MissionConfig, NavigationMission
from ff7r_agent.runtime_commands import invoke_runtime
from ff7r_agent.safety import (
    ArmError, ArmState, CameraArmState, SessionLease, describe_control,
    request_session_close, request_session_open, single_gameplay_process,
)
from ff7r_agent.service import SafetyError
from test_agent import FakeEnvironment, FakeRealInput, FixedTelemetry, make_real_service
from test_navigation_mission import Runtime
from test_cutscene import INTRO, write as write_cutscene_state


class Game:
    """A stand-in game process: alive until it exits; a restart is a new process."""

    def __init__(self, pid=4242):
        self.pid, self.token, self.alive = pid, "created-1", True

    def probe(self, pid, token):
        return self.alive and pid == self.pid and token in (None, self.token)

    def identity(self, pid):
        return self.token if self.alive and pid == self.pid else None

    def exit(self):
        self.alive = False

    def restart(self, token="created-2"):
        self.alive, self.token = True, token


def lease_for(path, game, clock=None, owner="test-owner"):
    clock = clock if clock is not None else [1000.0]
    return SessionLease(path, owner_id=owner, clock=lambda: clock[0], session_marker=lambda: "test-session",
                        process_alive=game.probe, process_identity=game.identity), clock


# --- the session's lifetime is the game's -------------------------------------------------

def test_a_session_outlives_the_old_thirty_minute_lease_while_the_game_runs(tmp_path):
    game = Game()
    lease, clock = lease_for(tmp_path, game)
    opened = lease.open(60, game_pid=game.pid, client_id="agent")
    assert opened["active"] and opened["lifetime"] == "game_process" and opened["seconds_remaining"] is None
    clock[0] += 1800 + 1
    assert lease.status()["active"] is True
    clock[0] += 6 * 3600  # a long play session, with nobody heartbeating
    status = lease.status()
    assert status["active"] is True and status["state"] == "OPEN" and status["seconds_remaining"] is None
    assert status["game_pid"] == game.pid and status["client_id"] == "agent"


def test_the_session_ends_when_the_game_process_disappears(tmp_path):
    game = Game()
    lease, clock = lease_for(tmp_path, game)
    lease.open(game_pid=game.pid, client_id="agent")
    game.exit()
    status = lease.status()
    assert status["active"] is False and status["state"] == "GAME_EXITED"
    with pytest.raises(ArmError, match="not active"):
        lease.heartbeat()
    assert describe_control(status, None, stopped=True)["state"] == "game_exited"


def test_a_reused_pid_is_not_the_same_game(tmp_path):
    game = Game()
    lease, _ = lease_for(tmp_path, game)
    lease.open(game_pid=game.pid)
    game.exit()
    game.restart()  # the same PID, a new process
    assert lease.status()["state"] == "GAME_EXITED"


def test_a_new_session_replaces_one_whose_game_has_gone_and_keeps_its_archive(tmp_path):
    game = Game()
    lease, clock = lease_for(tmp_path, game)
    lease.open(game_pid=game.pid, client_id="old")
    game.exit()
    game.restart()
    clock[0] += 5
    reopened = lease.open(game_pid=game.pid, client_id="new")
    assert reopened["active"] and reopened["client_id"] == "new"
    archives = list(tmp_path.glob("control-session.closed-*.json"))
    assert len(archives) == 1 and json.loads(archives[0].read_text())["close_reason"] == "game_exited"
    # An exited game's session is replaced only by its own owner.
    game.exit()
    stranger, _ = lease_for(tmp_path, game, owner="someone-else")
    with pytest.raises(ArmError, match="explicitly archived"):
        stranger.open(game_pid=game.pid)


def test_seconds_is_accepted_and_ignored(tmp_path):
    game = Game()
    lease, clock = lease_for(tmp_path, game)
    assert lease.open(5, game_pid=game.pid)["requested_seconds"] == 5  # below the old 60 s minimum
    assert lease.heartbeat(99999)["active"] is True  # above the old 1800 s maximum
    assert lease.heartbeat("soon")["seconds_remaining"] is None


def test_a_process_not_running_at_open_is_recorded_but_not_enforced(tmp_path):
    # A dry run or a title screen before launch: nothing to be bound to. Input
    # still needs a running game for every action.
    game = Game(pid=7)
    lease, _ = lease_for(tmp_path, game)
    opened = lease.open(game_pid=999, client_id="agent")
    assert opened["game_pid"] == 999 and opened["game_bound"] is False
    assert lease.status()["active"] is True


def test_cold_startup_binds_its_unbound_session_once_the_game_exists(tmp_path, monkeypatch):
    from ff7r_agent.startup import StartupCoordinator
    from test_startup import FakeLifecycle, FakeService

    monkeypatch.delenv("FF7R_RECORDING_MODEL", raising=False)
    monkeypatch.delenv("FF7R_RECORDING_EFFORT", raising=False)
    game = Game()
    lease, _clock = lease_for(tmp_path, game)
    lease.open(game_pid=None)
    agent = StartupCoordinator(FakeLifecycle(), FakeService(), session_status=lease.status,
                               session_open=lease.open, session_heartbeat=lease.heartbeat,
                               session_close=lease.close)

    bound = agent._ensure_session(game.pid, 1800)

    assert bound["game_bound"] is True and bound["game_pid"] == game.pid
    assert bound["game_identity"] == game.token
    game.exit()
    assert lease.status()["state"] == "GAME_EXITED"


def test_cold_startup_does_not_replace_another_clients_unbound_session(tmp_path):
    from ff7r_agent.startup import StartupCoordinator, StartupError
    from test_startup import FakeLifecycle, FakeService

    game = Game()
    lease, _clock = lease_for(tmp_path, game)
    original = lease.open(game_pid=None, client_id="another-client")
    agent = StartupCoordinator(FakeLifecycle(), FakeService(), session_status=lease.status,
                               session_open=lease.open, session_heartbeat=lease.heartbeat,
                               session_close=lease.close)

    with pytest.raises(StartupError, match="CONTROL_SESSION_CLIENT_MISMATCH"):
        agent._ensure_session(game.pid, 1800)

    assert lease.status()["active"] is True
    assert lease.status()["opened_at"] == original["opened_at"]
    assert not list(tmp_path.glob("control-session.closed-*.json"))


def test_a_session_open_without_a_game_binds_to_the_single_gameplay_process():
    assert single_gameplay_process({10: "ff7rebirth.exe", 20: "ff7rebirth_.exe"}) == 20  # not the launcher
    assert single_gameplay_process({10: "ff7rebirth.exe"}) is None
    assert single_gameplay_process({20: "ff7rebirth_.exe", 21: "ff7rebirth_.exe"}) is None
    assert single_gameplay_process({}) is None


def test_open_control_session_binds_the_running_game_when_no_pid_is_given(tmp_path):
    service, _ = make_real_service(environment=FakeEnvironment())
    service._session_lease = SessionLease(runtime_dir=tmp_path)
    service._environment.game_process_ids = lambda: {10: "ff7rebirth.exe", 20: "ff7rebirth_.exe"}
    opened = invoke_runtime(service, None, None, "open_control_session", {"seconds": 1800, "client_id": "agent"})
    assert opened["game_pid"] == 20 and opened["lifetime"] == "game_process"
    assert service._session_lease.status()["game_pid"] == 20


# --- closing and stopping ------------------------------------------------------------------

def _open_with_broker(tmp_path, client="agent"):
    opened = request_session_open(runtime_dir=tmp_path, client_id=client)
    return opened, ArmState(runtime_dir=tmp_path), CameraArmState(runtime_dir=tmp_path)


def test_closing_a_borrowed_session_does_not_close_or_latch(tmp_path):
    _, broker, camera = _open_with_broker(tmp_path)
    assert broker.status()[0] and camera.status()[0]
    refused = request_session_close(runtime_dir=tmp_path, client_id="a-mission")
    assert refused["closed"] is False and refused["reason"] == "session_owned_by_another_client"
    assert "safety_state" not in refused
    assert SessionLease(runtime_dir=tmp_path).status()["active"] is True
    assert broker.snapshot()[2] is False and broker.status()[0] and camera.snapshot()[2] is False


def test_a_task_closes_the_session_it_opened_and_that_latches(tmp_path):
    _open_with_broker(tmp_path, client="mission-1")
    closed = request_session_close(runtime_dir=tmp_path, client_id="mission-1")
    assert closed["closed"] is True and closed["safety_state"] == "latched"
    assert SessionLease(runtime_dir=tmp_path).status() is None
    assert ArmState(runtime_dir=tmp_path).snapshot()[2] is True


def test_closing_with_no_session_on_record_by_a_borrower_latches_nothing(tmp_path):
    ArmState(runtime_dir=tmp_path).reset_stop()
    result = request_session_close(runtime_dir=tmp_path, client_id="mission")
    assert result == {"closed": False, "reason": "no_session"}
    assert ArmState(runtime_dir=tmp_path).snapshot()[2] is False


def test_an_explicit_close_latches_every_broker(tmp_path):
    _, broker, camera = _open_with_broker(tmp_path)
    closed = request_session_close(runtime_dir=tmp_path)
    assert closed["closed"] and closed["safety_state"] == "latched"
    assert broker.snapshot()[2] and camera.snapshot()[2]
    assert SessionLease(runtime_dir=tmp_path).last_close()["reason"] == "explicit_close"


def test_close_control_session_carries_the_callers_identity_to_the_guard(tmp_path):
    service, _ = make_real_service()
    service._session_lease = SessionLease(runtime_dir=tmp_path)
    request_session_open(runtime_dir=tmp_path, client_id="agent")
    broker = ArmState(runtime_dir=tmp_path)
    kept = invoke_runtime(service, None, None, "close_control_session", {"client_id": "a-mission"})
    assert kept["closed"] is False and service._session_lease.status()["active"] and not broker.snapshot()[2]
    closed = invoke_runtime(service, None, None, "close_control_session", {})
    assert closed["closed"] and broker.snapshot()[2]


def live_service(tmp_path, *, environment=None, game=None, clock=None):
    """A real-input service on a real session and arm in ``tmp_path``."""
    game = game or Game()
    clock = clock if clock is not None else [100.0]
    runtime = tmp_path / "runtime"
    lease = SessionLease(runtime, owner_id="test-owner", session_marker=lambda: "test-session",
                         process_alive=game.probe, process_identity=game.identity)
    arm = ArmState(runtime, session_marker=lambda: "test-session")
    camera = CameraArmState(runtime, session_marker=lambda: "test-session")
    telemetry = FixedTelemetry()
    telemetry.path = tmp_path / "state.json"  # the cutscene file is read beside it
    service, backend = make_real_service(telemetry=telemetry, arm=arm, camera_arm=camera,
                                         environment=environment or FakeEnvironment(),
                                         monotonic=lambda: clock[0], sleeper=lambda _: None)
    service._session_lease = lease
    return service, backend, lease, arm, clock, game


def open_live_session(service, lease, game, arm, camera=None):
    lease.open(game_pid=game.pid, client_id="agent")
    arm.reset_stop()
    arm.arm(120)


def test_the_routine_latch_between_actions_is_not_a_stop_while_a_session_is_open(tmp_path):
    service, backend, lease, arm, clock, game = live_service(tmp_path)
    open_live_session(service, lease, game, arm)
    assert service.move(0, 1, 100)["ok"]
    clock[0] += 1.0
    assert service.move(0, 1, 100)["ok"]  # the open session re-arms the one-shot arm
    assert len(backend.actions) == 2
    control = service.control_state()
    assert control["state"] == "open" and control["session_open"] is True and control["latched"] is False


def test_a_stop_latches_and_closes_the_session_and_is_reported_where_the_agent_looks(tmp_path):
    service, backend, lease, arm, clock, game = live_service(tmp_path)
    open_live_session(service, lease, game, arm)
    assert service.stop() == {"ok": True, "emergency_stop": True}
    assert lease.status() is None
    assert arm.snapshot()[2] is True
    control = service.control_state()
    assert control["state"] == "closed" and control["reason"] == "emergency_stop"
    assert control["session_open"] is False and control["latched"] is True and control["since"]
    for report in (service.status(), service.input_status(), service.observe(), service.session_health()):
        assert report["control"]["state"] == "closed" and report["control"]["reason"] == "emergency_stop"
    clock[0] += 1.0
    with pytest.raises(SafetyError, match="no control session is open; open_control_session first"):
        service.move(0, 1, 100)
    assert backend.actions == []
    # Opening a session lifts it again.
    lease.open(game_pid=game.pid, client_id="agent")
    clock[0] += 1.0
    assert service.move(0, 1, 100)["ok"]


def test_a_session_that_never_opened_and_one_whose_game_exited_say_so(tmp_path):
    service, backend, lease, arm, clock, game = live_service(tmp_path)
    assert service.control_state()["state"] == "not_opened"
    open_live_session(service, lease, game, arm)
    game.exit()
    control = service.control_state()
    assert control["state"] == "game_exited" and "exited" in control["reason"]
    assert service.status()["control_session"]["state"] == "GAME_EXITED"
    clock[0] += 1.0
    with pytest.raises(SafetyError):
        service.move(0, 1, 100)


def test_an_unreadable_session_record_is_reported_by_the_control_state(tmp_path):
    service, _, lease, arm, _, _ = live_service(tmp_path)
    lease.runtime_dir.mkdir(parents=True, exist_ok=True)
    lease.state_path.write_text("{not json", encoding="utf-8")
    control = service.control_state()
    assert control["state"] == "unreadable" and "unreadable" in control["reason"]


def test_describe_control_orders_its_reasons():
    assert describe_control(None, None, stopped=True, stopped_at=0.0)["state"] == "not_opened"
    closed = describe_control(None, {"reason": "explicit_close", "closed_at": 1.0}, stopped=True)
    assert closed["state"] == "closed" and closed["reason"] == "explicit_close" and closed["since"].startswith("1970")
    assert describe_control({"active": False, "state": "OWNER_MISMATCH"}, None, stopped=True)["state"] == "other_owner"
    assert describe_control({"active": True, "opened_at": 5.0, "client_id": "c"}, None, stopped=True)["latched"] is False


# --- the cutscene watcher ------------------------------------------------------------------

PLAYING = {"active": True, "scenes": [INTRO]}


def watcher(tmp_path, **options):
    """A real-input service whose watcher presses are recorded, not sent."""
    service, backend, lease, arm, clock, game = live_service(tmp_path, **options)
    pressed = []
    state = tmp_path / "cutscene.state"
    write_cutscene_state(state, [INTRO], now=time.time(), leader=False)
    dialog = {"open": False}
    service._cinema_pause_dialog_open = lambda full=False: dialog["open"]

    def tap(button, milliseconds):
        pressed.append(button)
        if button == "menu":
            dialog["open"] = True
        if button == "confirm":
            write_cutscene_state(state, [], now=time.time())
        return {"ok": True, "button": button}

    service.tap = tap
    service._cutscene_autoskip = "always"
    return service, pressed, lease, arm, game


def test_the_watcher_reports_a_closed_session_once_and_presses_nothing(tmp_path):
    service, pressed, lease, arm, game = watcher(tmp_path)
    for _ in range(20):
        assert service._cutscene_watch_once(PLAYING) is None
    assert pressed == []
    notes = [entry for entry in service.cutscene_autoskip_status()["recent"] if entry["reason"] == "control_unavailable"]
    assert len(notes) == 1 and "not_opened" in notes[0]["detail"]
    blocked = service.cutscene_autoskip_status()["blocked"]
    assert blocked["reason"].startswith("no_control_session:not_opened") and blocked["suppressed"] == 19
    assert not any(entry["reason"] == "watch_error" for entry in service.cutscene_autoskip_status()["recent"])


def test_the_watcher_resumes_when_a_session_is_opened(tmp_path):
    service, pressed, lease, arm, game = watcher(tmp_path)
    assert service._cutscene_watch_once(PLAYING) is None
    open_live_session(service, lease, game, arm)
    result = service._cutscene_watch_once(PLAYING)
    assert result["ok"] is True and pressed == ["menu", "down", "confirm"]
    status = service.cutscene_autoskip_status()
    assert status["blocked"] is None
    assert [entry.get("reason") for entry in status["recent"]][:2] == ["control_unavailable", "control_restored"]
    assert status["recent"][-1]["skipped"] == [INTRO["sequence"]]


def test_the_watcher_never_presses_into_a_game_that_is_not_in_front(tmp_path):
    service, pressed, lease, arm, game = watcher(tmp_path, environment=FakeEnvironment(foreground=False))
    open_live_session(service, lease, game, arm)
    for _ in range(5):
        assert service._cutscene_watch_once(PLAYING) is None
    assert pressed == []
    assert service.cutscene_autoskip_status()["blocked"]["reason"] == "game_not_in_foreground"


def test_the_watcher_stops_when_the_game_has_exited(tmp_path):
    service, pressed, lease, arm, game = watcher(tmp_path, environment=FakeEnvironment(process=False))
    open_live_session(service, lease, game, arm)
    game.exit()
    assert service._cutscene_watch_once(PLAYING) is None
    assert pressed == [] and "game_exited" in service.cutscene_autoskip_status()["blocked"]["reason"]


class Waits:
    """Stands in for the watcher's stop event: records each wait, ends after ``rounds``."""

    def __init__(self, rounds, between=lambda round_number: None):
        self.delays, self.rounds, self.between = [], rounds, between

    def wait(self, delay):
        self.delays.append(delay)
        self.between(len(self.delays))
        return len(self.delays) > self.rounds


def test_the_watcher_backs_off_while_it_cannot_send_and_returns_to_the_quick_poll(tmp_path):
    from ff7r_agent.service import CUTSCENE_BLOCKED_WATCH_SECONDS, CUTSCENE_WATCH_SECONDS

    service, pressed, lease, arm, game = watcher(tmp_path)

    def open_after_four(round_number):
        if round_number == 4:
            open_live_session(service, lease, game, arm)

    service._cutscene_watch_stop = waits = Waits(8, open_after_four)
    service._cutscene_watch()
    # Quick poll, then (the first round finds the lock) slow while it holds,
    # then quick again once input is possible.
    assert waits.delays[0] == CUTSCENE_WATCH_SECONDS
    assert waits.delays[1:4] == [CUTSCENE_BLOCKED_WATCH_SECONDS] * 3
    assert waits.delays[4:] == [CUTSCENE_WATCH_SECONDS] * (len(waits.delays) - 4)
    assert pressed[:3] == ["menu", "down", "confirm"]


def test_a_repeating_watch_error_is_logged_once_with_a_count_and_backs_off(tmp_path):
    from ff7r_agent.service import CUTSCENE_BLOCKED_WATCH_SECONDS

    service, *_ = watcher(tmp_path)

    def broken(state):
        raise RuntimeError("the same failure")

    service._cutscene_watch_once = broken
    service._cutscene_watch_stop = waits = Waits(10)
    service._cutscene_watch()
    entries = [entry for entry in service.cutscene_autoskip_status()["recent"] if entry["reason"] == "watch_error"]
    assert len(entries) == 1 and entries[0]["repeats"] == 10
    assert waits.delays[-1] == CUTSCENE_BLOCKED_WATCH_SECONDS


# --- tasks borrow the session --------------------------------------------------------------

class BorrowRuntime(Runtime):
    def __init__(self, *, client="agent-session", pid=6932):
        super().__init__()
        self.active_lease, self.client_id, self.lease_pid = True, client, pid

    async def call(self, op, args):
        value = await super().call(op, args)
        if op == "get_control_session_status":
            value["result"]["game_pid"] = self.lease_pid
        return value


def mission(runtime, tmp_path, **options):
    config = MissionConfig(execute=True, timeout_seconds=options.pop("timeout", 20))
    return NavigationMission(runtime.call, tmp_path / "run", identity_check=lambda *args: {"ok": True}, config=config,
                             clock=lambda: runtime.time, sleep=runtime.sleep, progress=lambda _: None, **options)


def test_a_mission_borrows_the_open_session_and_leaves_it_open(tmp_path):
    runtime = BorrowRuntime()
    result = asyncio.run(mission(runtime, tmp_path).run())
    assert result["ok"] and result["state"] == "ARRIVED" and result["control_session"] == "borrowed"
    operations = [op for op, _ in runtime.calls]
    assert not set(operations) & {"open_control_session", "close_control_session", "stop_all_input"}
    assert result["cleanup"] == {"session": "left_open"}
    assert runtime.active_lease is True


def test_a_borrowing_mission_that_fails_without_input_in_flight_still_leaves_the_session_open(tmp_path):
    runtime = BorrowRuntime()
    runtime.preflight = False
    result = asyncio.run(mission(runtime, tmp_path).run())
    assert not result["ok"] and result["reason"] == "unreachable"
    assert not {"close_control_session", "stop_all_input"} & {op for op, _ in runtime.calls}


def test_a_borrowing_mission_with_input_possibly_in_flight_emergency_stops_and_says_so(tmp_path):
    runtime = BorrowRuntime()
    runtime.running_polls = 100
    result = asyncio.run(mission(runtime, tmp_path, ).run())
    assert result["reason"] == "mission_time_budget_exhausted"
    operations = [op for op, _ in runtime.calls]
    assert operations[-1] == "stop_all_input" and "close_control_session" not in operations
    assert result["cleanup"]["session"] == "closed"


def test_a_session_for_another_game_is_not_borrowed(tmp_path):
    runtime = BorrowRuntime(pid=1111)
    result = asyncio.run(mission(runtime, tmp_path).run())
    assert result["reason"] == "control_session_game_mismatch"
    assert not {"start_live_route_adapter", "close_control_session", "stop_all_input"} & {op for op, _ in runtime.calls}


def test_a_mission_closes_only_the_session_it_opened_with_its_own_identity(tmp_path):
    runtime = Runtime()
    client = mission(runtime, tmp_path)
    result = asyncio.run(client.run())
    assert result["ok"] and result["control_session"] == "opened"
    assert [op for op, _ in runtime.calls][-2:] == ["stop_all_input", "close_control_session"]
    assert runtime.calls[-1][1] == {"client_id": client.client_id}


# --- entity interaction under a long-lived session ----------------------------------------

def test_an_entity_interaction_is_admitted_once_per_request_not_once_per_session(tmp_path):
    game = Game()
    lease, _ = lease_for(tmp_path, game)
    lease.open(game_pid=game.pid, client_id="agent")
    lease.consume_entity_interaction("agent", game.pid, "npc-1", request_key="npc-1@10")
    with pytest.raises(ArmError, match="already_consumed"):
        lease.consume_entity_interaction("agent", game.pid, "npc-1", request_key="npc-1@10")  # a replay
    lease.consume_entity_interaction("agent", game.pid, "npc-1", request_key="npc-1@57")  # a later mission
    lease.heartbeat()
    with pytest.raises(ArmError, match="already_consumed"):
        lease.consume_entity_interaction("agent", game.pid, "npc-2")  # unkeyed: once per session
    game.exit()
    with pytest.raises(ArmError, match="session_mismatch"):
        lease.consume_entity_interaction("agent", game.pid, "npc-3", request_key="npc-3@1")


def test_owned_cold_start_binding_survives_individual_approach_handoff(tmp_path, monkeypatch):
    """#367: synthetic successful face boundary; use real lease persistence."""
    from ff7r_agent import npc_approach
    from ff7r_agent.startup import StartupCoordinator
    from test_startup import FakeLifecycle, FakeService

    game = Game(pid=23836)
    lease, _ = lease_for(tmp_path, game)
    driver = {"modelId": "gpt-6.1-sol", "effort": "max"}
    client = "ff7r-session-synthetic-367"
    lease.open(game_pid=None, client_id=client, model_id=driver["modelId"], reasoning_effort=driver["effort"])
    coordinator = StartupCoordinator(FakeLifecycle(), FakeService(), session_status=lease.status,
                                     session_open=lease.open, session_heartbeat=lease.heartbeat,
                                     session_close=lease.close)
    token = coordinator._startup_session_binding.set({
        "client_id": client, "explicit_client": True, "recording": driver})
    try:
        bound = coordinator._ensure_session(game.pid, 1800)
    finally:
        coordinator._startup_session_binding.reset(token)
    assert bound["game_pid"] == game.pid and bound["game_identity"] == game.token
    assert bound["client_id"] == client and bound["recording_driver"] == driver
    before = lease.state_path.read_bytes()
    calls = []

    async def rpc(operation, arguments):
        calls.append((operation, arguments))
        if operation == "get_control_session_status":
            return lease.status()
        if operation == "open_control_session":
            return lease.open(**arguments)
        if operation == "close_control_session":
            return lease.close(expected_client_id=arguments.get("client_id"))
        if operation == "stop_all_input":
            return lease.abort()
        raise AssertionError(operation)

    async def successful_face(rpc, _name, **_options):
        await npc_approach.hold_control_session(rpc, client)
        return {"ok": True, "state": "talk_offered"}

    monkeypatch.setattr(npc_approach, "_approach_npc", successful_face)
    result = asyncio.run(npc_approach.approach_npc(
        rpc, "Isabelle", execute=True, client_id=client, model_id=driver["modelId"], reasoning_effort=driver["effort"]))
    assert result["ok"] and result["control_session"] == "left_open"
    assert lease.state_path.read_bytes() == before
    assert not {"open_control_session", "close_control_session", "stop_all_input"} & {op for op, _ in calls}
    with pytest.raises(ArmError, match="active control session"):
        lease.open(game_pid=game.pid, client_id=client)  # documented reopening is refused
    assert lease.status()["active"] is True
    game.exit()
    assert lease.status()["state"] == "GAME_EXITED"
