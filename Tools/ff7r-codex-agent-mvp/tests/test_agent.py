from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import uuid
from dataclasses import replace

import pytest

from ff7r_agent import safety
from ff7r_agent.input import (
    InputError, SimulationInput, WindowsKeyboardInput, normalise_axes
)
from ff7r_agent.models import (
    CameraState, Character, GameState, Interactable, RelativePosition, Rotation, Vec3,
)
from ff7r_agent.safety import (
    ArmState, BenchArmState, CameraArmState, SessionLease, request_arm,
    request_arm_bench, request_reset_stop,
)
from ff7r_agent.service import AgentService, SafetyError
from ff7r_agent.telemetry import JsonFileTelemetry, SimulationTelemetry


def test_observe_simulation():
    service = AgentService(SimulationTelemetry(), SimulationInput())
    state = service.observe()
    assert state["running"] is True
    assert state["source"] == "simulation"


def test_axis_is_bounded():
    x, y = normalise_axes(3, 4)
    assert round((x * x + y * y) ** 0.5, 6) == 1.0


def test_duration_is_bounded():
    service = AgentService(SimulationTelemetry(), SimulationInput())
    with pytest.raises(InputError):
        service.move(0, 1, 5000)


def test_simulation_camera_pulse_is_recorded():
    backend = SimulationInput()
    service = AgentService(SimulationTelemetry(), backend)
    result = service.look_camera(30, -10, 80)
    assert result["action"] == "look"
    assert backend.actions[-2].action == "look"
    assert backend.actions[-2].parameters == {"dx": 30, "dy": -10, "milliseconds": 80}


def test_a_session_written_with_a_wall_clock_lease_expires_until_it_is_adopted(tmp_path):
    # Records from before sessions lasted for the game's life keep their expiry.
    now = [100.0]
    lease = SessionLease(
        tmp_path,
        owner_id="test-owner",
        clock=lambda: now[0],
        session_marker=lambda: "test-session",
    )
    (tmp_path / "control-session.json").write_text(json.dumps({
        "version": 1, "session_marker": "test-session", "owner_id": "test-owner",
        "controller_pid": 1, "game_pid": None, "client_id": None, "recording_driver": None,
        "opened_at": 100.0, "heartbeat_at": 100.0, "expires_at": 160.0, "state": "OPEN",
    }), encoding="utf-8")
    assert lease.status()["active"] is True
    assert lease.status()["seconds_remaining"] == pytest.approx(60.0)
    now[0] += 30
    adopted = lease.heartbeat()
    assert adopted["lifetime"] == "game_process" and adopted["seconds_remaining"] is None
    now[0] += 3600
    assert lease.status()["active"] is True  # no clock any more

    (tmp_path / "control-session.json").write_text(json.dumps({
        "version": 1, "session_marker": "test-session", "owner_id": "test-owner",
        "controller_pid": 1, "game_pid": None, "client_id": None, "recording_driver": None,
        "opened_at": 100.0, "heartbeat_at": 100.0, "expires_at": 160.0, "state": "OPEN",
    }), encoding="utf-8")
    assert lease.status()["active"] is False and lease.status()["state"] == "EXPIRED"
    with pytest.raises(Exception, match="explicitly archived"):
        lease.open(60)
    assert lease.close("test_expiry_cleanup")["closed"] is True
    assert lease.open(60)["active"] is True


def test_session_lease_archives_exited_previous_windows_session(tmp_path):
    state_path = tmp_path / "control-session.json"
    state_path.write_text(json.dumps({
        "version": 1,
        "session_marker": "old-boot",
        "owner_id": "test-owner",
        "controller_pid": 999999,
        "game_pid": 999998,
        "opened_at": 1.0,
        "heartbeat_at": 1.0,
        "expires_at": 9999999999.0,
        "state": "OPEN",
    }), encoding="utf-8")
    lease = SessionLease(
        tmp_path,
        owner_id="test-owner",
        session_marker=lambda: "new-boot",
    )
    stale = lease.status()
    assert stale["state"] == "STALE_WINDOWS_SESSION"
    assert stale["active"] is False
    opened = lease.open(60)
    assert opened["active"] is True
    assert state_path.exists()
    assert list(tmp_path.glob("control-session.stale-*.json"))


def test_session_process_liveness_uses_handle_safe_ownership_probe(monkeypatch):
    calls = []
    monkeypatch.setattr(
        safety,
        "owner_is_alive",
        lambda pid, token: calls.append((pid, token)) or True,
    )
    monkeypatch.setattr(safety.os, "kill", lambda *args: pytest.fail("session liveness must not call os.kill"))

    assert safety._process_exists(1234) is True
    assert calls == [(1234, None)]


@pytest.fixture(autouse=True)
def benchmark_temp_directories_stay_in_pytest_namespace(tmp_path, monkeypatch):
    def make_temp_directory(*_args, **_kwargs):
        path = tmp_path / f"benchmark-{uuid.uuid4().hex}"
        path.mkdir()
        return str(path)

    monkeypatch.setattr(tempfile, "mkdtemp", make_temp_directory)


class FakePyDirectInput:
    PAUSE = None
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010

    def __init__(self, *, down=True, up=True):
        self.down = down
        self.up = up
        self.calls = []
        self.SendInput = self._send_input

    def _send_input(self, expected, pointer, size):
        self.calls.append(("SendInput", expected, size))
        return expected

    def keyDown(self, key):
        self.calls.append(("down", key))
        return self.down

    def keyUp(self, key):
        self.calls.append(("up", key))
        return self.up

    def mouseDown(self, *, button):
        self.calls.append(("mouseDown", button))
        return self.down

    def mouseUp(self, *, button):
        self.calls.append(("mouseUp", button))
        return self.up

    def moveRel(self, x, y, *, duration, _pause, relative):
        self.calls.append(("moveRel", x, y, duration, _pause, relative))
        if duration == 0:
            self.SendInput(1, None, 1)
        return self.down


class NoneReturningMousePyDirectInput(FakePyDirectInput):
    """Mirror pydirectinput's successful mouseDown/mouseUp return value."""

    def mouseDown(self, *, button):
        self.calls.append(("mouseDown", button))
        self.SendInput(1, None, 1)
        return None

    def mouseUp(self, *, button):
        self.calls.append(("mouseUp", button))
        self.SendInput(1, None, 1)
        return None


def make_windows_backend(monkeypatch, fake, *, keymap=None):
    import sys
    monkeypatch.setitem(sys.modules, "pydirectinput", fake)
    return WindowsKeyboardInput(keymap=keymap)


def test_windows_backend_reports_sendinput_keydown_failure(monkeypatch):
    fake = FakePyDirectInput(down=False)
    backend = make_windows_backend(monkeypatch, fake)
    with pytest.raises(InputError, match="failed to press key: w"):
        backend.move(0, 1, 20)
    assert backend._held == set()
    assert fake.calls == [("down", "w")]


def test_windows_backend_reports_sendinput_keyup_failure_and_clears_tracking(monkeypatch):
    fake = FakePyDirectInput(up=False)
    backend = make_windows_backend(monkeypatch, fake)
    with pytest.raises(InputError, match="failed to release key.*w"):
        backend.move(0, 1, 20)
    assert backend._held == set()
    assert fake.calls == [("down", "w"), ("up", "w")]


def test_windows_backend_accepts_bounded_menu_toggle(monkeypatch):
    fake = FakePyDirectInput()
    backend = make_windows_backend(monkeypatch, fake)
    backend.tap("menu", 20)
    assert fake.calls == [("down", "esc"), ("up", "esc")]


def test_windows_backend_accepts_bounded_map_toggle(monkeypatch):
    fake = FakePyDirectInput()
    backend = make_windows_backend(monkeypatch, fake)
    backend.tap("map", 20)
    assert fake.calls == [("down", "tab"), ("up", "tab")]


def test_windows_backend_accepts_map_selection_and_pin_controls(monkeypatch):
    fake = FakePyDirectInput()
    backend = make_windows_backend(monkeypatch, fake)
    backend.tap("map_selection_prev", 20)
    backend.tap("map_selection_next", 20)
    backend.tap("map_drop_pin", 20)
    backend.tap("map_tab_prev", 20)
    backend.tap("map_tab_next", 20)
    backend.tap("map_layer_toggle", 20)
    backend.tap("map_era_toggle", 20)
    backend.tap("map_view_corresponding", 20)
    assert fake.calls == [
        ("down", "z"), ("up", "z"),
        ("down", "x"), ("up", "x"),
        ("down", "f2"), ("up", "f2"),
        ("down", "q"), ("up", "q"),
        ("down", "e"), ("up", "e"),
        ("down", "f3"), ("up", "f3"),
        ("down", "r"), ("up", "r"),
        ("down", "g"), ("up", "g"),
    ]


def test_windows_backend_accepts_bounded_camera_pulse(monkeypatch):
    fake = FakePyDirectInput()
    backend = make_windows_backend(monkeypatch, fake)
    backend.look(120, -40, 80)
    assert fake.calls == [("moveRel", 120, -40, 0.08, False, True)]


def test_windows_backend_accepts_verified_calibrated_camera_event(monkeypatch):
    fake = FakePyDirectInput()
    backend = make_windows_backend(monkeypatch, fake)
    result = backend.calibrate_camera_horizontal(-500)
    assert result["event"] == "camera_horizontal_relative"
    assert result["horizontal_delta"] == -500
    assert fake.calls[0][0] == "moveRel"
    assert fake.calls[0][1:3] == (-500, 0)
    assert fake.calls[0][3:] == (0, False, True)
    assert any(call[0] == "SendInput" for call in fake.calls)


def test_windows_backend_holds_and_releases_sprint_modifier_with_movement(monkeypatch):
    fake = FakePyDirectInput()
    backend = make_windows_backend(monkeypatch, fake)
    result = backend.adaptive_move(
        0.0,
        1.0,
        lambda: "test_stop",
        maximum_ms=100,
        sprint=True,
    )
    assert result["sprint"] is True
    assert result["keys"] == ["shift", "w"]
    assert fake.calls == [
        ("down", "shift"), ("down", "w"),
        ("up", "w"), ("up", "shift"),
    ]


def test_windows_backend_accepts_bounded_menu_tab_navigation(monkeypatch):
    fake = FakePyDirectInput()
    backend = make_windows_backend(monkeypatch, fake)
    backend.tap("left", 20)
    backend.tap("right", 20)
    assert fake.calls == [
        ("down", "q"), ("up", "q"), ("down", "e"), ("up", "e")
    ]


def test_windows_backend_accepts_bounded_menu_subtab_navigation(monkeypatch):
    fake = FakePyDirectInput()
    backend = make_windows_backend(monkeypatch, fake)
    backend.tap("sub_left", 20)
    backend.tap("sub_right", 20)
    assert fake.calls == [
        ("down", "z"), ("up", "z"), ("down", "x"), ("up", "x")
    ]


def test_windows_backend_maps_queen_blood_begin_match_to_f3(monkeypatch):
    fake = FakePyDirectInput()
    backend = make_windows_backend(monkeypatch, fake)
    backend.tap("queen_blood_begin_match", 500)
    assert fake.calls == [("down", "f3"), ("up", "f3")]


def test_windows_backend_clicks_queen_blood_mulligan_marker(monkeypatch):
    fake = FakePyDirectInput()
    backend = make_windows_backend(monkeypatch, fake)
    backend.mouse_click("right", 20)
    assert fake.calls == [("mouseDown", "right"), ("mouseUp", "right")]


def test_windows_backend_positions_desktop_pointer_in_game_margin_before_click(monkeypatch):
    fake = FakePyDirectInput()
    backend = make_windows_backend(monkeypatch, fake)
    pointer_positions = []
    clicks = []
    backend._foreground_identity = lambda: (123, 456)
    backend._game_client_margin_position = lambda hwnd: (900, 700)
    backend._set_desktop_pointer = lambda x, y: pointer_positions.append((x, y)) or True
    backend.mouse_click = lambda button, milliseconds: clicks.append((button, milliseconds))

    backend.mouse_click_in_game_window("left", 1000, 123)

    assert pointer_positions == [(900, 700)]
    assert clicks == [("left", 1000)]
    assert backend._event_records[-1]["position_policy"] == "game_client_bottom_right_margin"


def test_windows_backend_accepts_pydirectinput_none_mouse_result(monkeypatch):
    fake = NoneReturningMousePyDirectInput()
    backend = make_windows_backend(monkeypatch, fake)
    backend.mouse_click("right", 20)
    assert fake.calls == [
        ("mouseDown", "right"),
        ("SendInput", 1, 1),
        ("mouseUp", "right"),
        ("SendInput", 1, 1),
    ]


def test_windows_backend_accepts_bounded_physical_interaction(monkeypatch):
    fake = FakePyDirectInput()
    backend = make_windows_backend(monkeypatch, fake)
    backend.use_interaction(20)
    assert fake.calls == [("down", "f"), ("up", "f")]


class FakeRealInput:
    is_real = True

    def __init__(self, bench_error=None):
        self.actions = []
        self.release_count = 0
        self.bench_error = bench_error

    def tap(self, button, milliseconds):
        self.actions.append(("tap", button, milliseconds))

    def mouse_click(self, button, milliseconds):
        self.actions.append(("mouse_click", button, milliseconds))

    def move(self, x, y, milliseconds):
        self.actions.append((x, y, milliseconds))

    def calibrate_camera_horizontal(self, delta):
        self.actions.append(("calibrate_camera_horizontal", delta))
        return {"event": "camera_horizontal_relative", "horizontal_delta": delta}

    def adaptive_forward(self, stop_requested, maximum_ms=1000,
                         poll_seconds=0.025):
        """Model bounded telemetry polling while the supervised pulse is held."""
        self.actions.append(("adaptive_forward", maximum_ms))
        release_reason = "maximum_hold_elapsed"
        for _ in range(40):
            requested = stop_requested()
            if requested:
                release_reason = str(requested)
                break
        return {
            "maximum_ms": maximum_ms,
            "actual_hold_ms": 250,
            "release_reason": release_reason,
        }

    def adaptive_move(self, x, y, stop_requested, maximum_ms=1000,
                      poll_seconds=0.025, sprint=False):
        self.actions.append(("adaptive_move", x, y, maximum_ms, sprint))
        release_reason = "maximum_hold_elapsed"
        for _ in range(40):
            requested = stop_requested()
            if requested:
                release_reason = str(requested)
                break
        return {
            "x": x,
            "y": y,
            "keys": ["w", "a"] if x < 0 and y > 0 else ["w"],
            "sprint": sprint,
            "maximum_ms": maximum_ms,
            "actual_hold_ms": 250,
            "release_reason": release_reason,
        }

    def release_all(self):
        self.release_count += 1

    def use_bench(self, milliseconds):
        self.actions.append(("bench", milliseconds))
        if self.bench_error:
            raise self.bench_error
        return {
            "button": "keyboard_f", "milliseconds": milliseconds,
            "key_down_ok": True, "key_up_ok": True,
            "pressed_timestamp": 1.0, "released_timestamp": 1.1,
        }

    def use_interaction(self, milliseconds):
        self.actions.append(("interaction", milliseconds))
        return {
            "button": "keyboard_f", "milliseconds": milliseconds,
            "key_down_ok": True, "key_up_ok": True,
            "pressed_timestamp": 1.0, "released_timestamp": 1.1,
        }


class FakeEnvironment:
    def __init__(self, process=True, foreground=True):
        self.process = process
        self.foreground = foreground

    def game_process_exists(self):
        return self.process

    def game_is_foreground(self):
        return self.foreground


class FakeArm:
    def __init__(self, armed=True, remaining=60.0, stopped=False):
        self.armed = armed
        self.remaining = remaining
        self.stopped = stopped

    def status(self):
        return self.armed, self.remaining

    def snapshot(self):
        return self.armed and not self.stopped, self.remaining if self.armed else 0.0, self.stopped

    def clear(self):
        self.armed = False
        self.remaining = 0.0

    def latch_stop(self):
        self.clear()
        self.stopped = True

    def reset_stop(self):
        self.clear()
        self.stopped = False


class FixedTelemetry:
    def __init__(self, **changes):
        now = datetime.now(timezone.utc)
        self._refresh_timestamp = "timestamp_utc" not in changes
        values = dict(
            schema_version=1,
            sequence=1,
            timestamp_utc=now.isoformat().replace("+00:00", "Z"),
            running=True,
            paused=False,
            in_menu=False,
            in_combat=False,
            location="",
            objective="",
            player=None,
            target=None,
            source="test",
        )
        values.update(changes)
        self.state = GameState(**values)

    def observe(self):
        # Parametrized fixtures are constructed during collection.  Refresh
        # only the default timestamp so a slow test run does not turn every
        # unrelated gate assertion into a stale-telemetry assertion.  Tests
        # that explicitly provide an old timestamp still exercise that gate.
        if self._refresh_timestamp:
            self.state = replace(
                self.state,
                timestamp_utc=datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
            )
        return self.state


class AdvancingMovementTelemetry:
    def __init__(self):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.state = GameState(
            schema_version=1,
            sequence=1,
            timestamp_utc=now,
            running=True,
            paused=False,
            in_menu=False,
            in_combat=False,
            location="",
            objective="",
            map_id="EndGame",
            player=Character(
                "Cloud", 10, 20, 3, 9,
                position=Vec3(1.0, 2.0, 3.0),
                rotation=Rotation(0.0, 0.0, 0.0),
                controller_state=1,
                actor_id="CloudActor",
            ),
            target=None,
            source="test",
        )
        self.calls = 0

    def observe(self):
        self.calls += 1
        if self.calls > 1:
            return replace(
                self.state,
                sequence=self.calls,
                player=replace(
                    self.state.player,
                    position=Vec3(101.0, 2.0, 3.0),
                ),
            )
        return self.state


class StepClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        self.value += 0.05
        return self.value


def make_real_service(*, telemetry=None, arm=None, bench_arm=None, camera_arm=None, environment=None,
                      monotonic=None, sleeper=None, backend=None, settled_gameplay_samples=1):
    backend = backend or FakeRealInput()
    service = AgentService(
        telemetry or FixedTelemetry(),
        backend,
        arm_state=arm or FakeArm(),
        bench_arm_state=bench_arm or FakeArm(remaining=30.0),
        camera_arm_state=camera_arm,
        environment=environment or FakeEnvironment(),
        monotonic=monotonic or (lambda: 100.0),
        minimum_action_delay=0.5,
        sleeper=sleeper or time.sleep,
        settled_gameplay_samples=settled_gameplay_samples,
        benchmark_root=Path(tempfile.mkdtemp()),
    )
    return service, backend


def test_real_movement_passes_all_gates_and_releases():
    service, backend = make_real_service()
    result = service.move(0, 1, 100)
    assert result["ok"]
    assert backend.actions == [(0.0, 1.0, 100)]
    assert backend.release_count == 1


def test_background_input_status_keeps_actual_foreground_separate_from_reachability():
    backend = FakeRealInput()
    backend.supports_background_input = True
    service, _ = make_real_service(
        backend=backend, environment=FakeEnvironment(foreground=False),
    )

    status = service.input_status()

    assert status["foreground_window"] is False
    assert status["input_reachable"] is True


def test_real_movement_waits_through_post_save_transition_before_input():
    class TransitionTelemetry:
        def __init__(self):
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            player = Character(
                "Cloud", 10, 20, 3, 9,
                position=Vec3(1.0, 2.0, 3.0),
                rotation=Rotation(0.0, 0.0, 0.0),
                controller_state=1,
                actor_id="CloudActor",
            )
            self.states = [
                GameState(1, 1, now, True, False, False, False, "", "", player,
                          map_id="EndGame", source="test"),
                GameState(1, 2, now, True, True, False, False, "", "", player,
                          map_id="EndGame", source="test"),
                GameState(1, 3, now, True, False, False, False, "", "", player,
                          map_id="EndGame", source="test"),
                GameState(1, 4, now, True, False, False, False, "", "", player,
                          map_id="EndGame", source="test"),
                GameState(1, 5, now, True, False, False, False, "", "", player,
                          map_id="EndGame", source="test"),
            ]

        def observe(self):
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    service, backend = make_real_service(
        telemetry=TransitionTelemetry(),
        monotonic=StepClock(),
        sleeper=lambda _: None,
        settled_gameplay_samples=3,
    )
    result = service.move(0, 1, 100)
    assert result["ok"] is True
    assert backend.actions == [(0.0, 1.0, 100)]


def test_real_movement_reports_settling_timeout_without_sending_input():
    class StaticTelemetry(FixedTelemetry):
        def observe(self):
            return replace(self.state, sequence=self.state.sequence)

    service, backend = make_real_service(
        telemetry=StaticTelemetry(map_id="EndGame", player=Character(
            "Cloud", 10, 20, 3, 9,
            position=Vec3(1.0, 2.0, 3.0),
            rotation=Rotation(0.0, 0.0, 0.0),
            controller_state=1,
            actor_id="CloudActor",
        )),
        monotonic=StepClock(),
        sleeper=lambda _: None,
        settled_gameplay_samples=3,
    )
    with pytest.raises(SafetyError, match="gameplay_not_settled"):
        service.move(0, 1, 100)
    assert backend.actions == []


def test_real_vault_is_guarded_and_releases():
    service, backend = make_real_service()
    result = service.tap_vault(120)
    assert result["ok"] is True
    assert result["action"] == "tap_vault"
    assert backend.actions == [("tap", "dodge", 120)]
    assert backend.release_count == 1


@pytest.mark.parametrize(
    ("telemetry", "arm", "environment", "reason"),
    [
        (FixedTelemetry(running=False), FakeArm(), FakeEnvironment(), "not running"),
        (FixedTelemetry(paused=True), FakeArm(), FakeEnvironment(), "paused"),
        (FixedTelemetry(in_menu=True), FakeArm(), FakeEnvironment(), "Main Menu"),
        (
            FixedTelemetry(timestamp_utc=(
                datetime.now(timezone.utc) - timedelta(seconds=3)
            ).isoformat().replace("+00:00", "Z")),
            FakeArm(), FakeEnvironment(), "stale",
        ),
        (FixedTelemetry(), FakeArm(), FakeEnvironment(process=False), "process"),
        (FixedTelemetry(), FakeArm(), FakeEnvironment(foreground=False), "foreground"),
        (FixedTelemetry(), FakeArm(armed=False, remaining=0), FakeEnvironment(), "not manually armed"),
    ],
)
def test_each_real_input_gate_rejects(telemetry, arm, environment, reason):
    service, backend = make_real_service(
        telemetry=telemetry, arm=arm, environment=environment
    )
    with pytest.raises(SafetyError, match=reason):
        service.move(0, 1, 100)
    assert backend.actions == []
    assert backend.release_count == 1
    assert reason in service.input_status()["last_action"]["rejection_reason"]


def test_target_blocks_real_movement():
    from ff7r_agent.models import ObservedActor
    service, _ = make_real_service(
        telemetry=FixedTelemetry(target=ObservedActor("enemy", 2.0))
    )
    with pytest.raises(SafetyError, match="target is present"):
        service.move(0, 1, 100)


def test_target_gated_combat_tap_requires_target_and_records_unknown_damage():
    from ff7r_agent.models import ObservedActor
    service, backend = make_real_service(
        telemetry=FixedTelemetry(target=ObservedActor("enemy", 2.0))
    )
    result = service.combat_tap("attack", 80)
    assert result["ok"] is True
    assert result["target_id"] == "enemy"
    assert result["damage_result_status"] == "target_present_hp_unavailable"
    assert backend.actions == [("tap", "attack", 80)]
    assert backend.release_count == 1


def test_target_gated_combat_tap_rejects_missing_target_and_other_buttons():
    service, backend = make_real_service()
    with pytest.raises(SafetyError, match="selected target"):
        service.combat_tap("attack", 80)
    assert backend.actions == []
    from ff7r_agent.models import ObservedActor
    service, backend = make_real_service(
        telemetry=FixedTelemetry(target=ObservedActor("enemy", 2.0))
    )
    with pytest.raises(SafetyError, match="only the target-gated attack"):
        service.combat_tap("interact", 80)
    assert backend.actions == []


def test_emergency_stop_clears_arm_and_remains_available_without_telemetry():
    class BrokenTelemetry:
        def observe(self):
            raise RuntimeError("unavailable")

    arm = FakeArm()
    service, backend = make_real_service(telemetry=BrokenTelemetry(), arm=arm)
    result = service.stop()
    assert result["emergency_stop"] is True
    assert arm.stopped
    assert backend.release_count == 1
    assert service.input_status()["armed"] is False


def test_local_broker_reset_clears_latch_without_arming():
    arm = FakeArm()
    service, _ = make_real_service(arm=arm)
    service.stop()
    arm.reset_stop()
    status = service.input_status()
    assert status["emergency_stop"] is False
    assert status["armed"] is False


def test_legacy_status_reads_shared_broker_latch():
    arm = FakeArm(stopped=True, armed=False, remaining=0.0)
    service, _ = make_real_service(arm=arm)
    assert service.status()["emergency_stop"] is True


def test_real_buttons_and_autonomy_are_disabled():
    service, _ = make_real_service()
    with pytest.raises(SafetyError, match="bounded menu navigation"):
        service.tap("interact", 80)
    with pytest.raises(SafetyError, match="autonomous"):
        service.approach_and_interact()


def test_real_menu_navigation_is_bounded_and_requires_menu_state():
    service, backend = make_real_service(
        telemetry=FixedTelemetry(player=None, in_menu=False)
    )
    result = service.tap("confirm", 80)
    assert result["mode"] == "menu"
    assert backend.actions == [("tap", "confirm", 80)]
    service, _ = make_real_service(telemetry=FixedTelemetry(
        map_id="EndGame",
        player=Character(
            "Cloud", 10, 20, 3, 9, position=Vec3(1.0, 2.0, 3.0),
            rotation=Rotation(0.0, 0.0, 0.0), controller_state=1,
            actor_id="CloudActor",
        ),
    ))
    with pytest.raises(SafetyError, match="loaded gameplay"):
        service.tap("confirm", 80)


def test_real_menu_toggle_is_allowed_from_loaded_noncombat_gameplay():
    service, backend = make_real_service(telemetry=FixedTelemetry(
        map_id="EndGame",
        player=Character(
            "Cloud", 10, 20, 3, 9, position=Vec3(1.0, 2.0, 3.0),
            rotation=Rotation(0.0, 0.0, 0.0), controller_state=1,
            actor_id="CloudActor",
        ),
    ))
    result = service.tap("menu", 80)
    assert result["mode"] == "menu"
    assert backend.actions == [("tap", "menu", 80)]


def test_session_health_identifies_loaded_active_gameplay():
    service, _ = make_real_service(telemetry=FixedTelemetry(
        map_id="EndGame",
        player=Character(
            "Cloud", 10, 20, 3, 9, position=Vec3(1.0, 2.0, 3.0),
            rotation=Rotation(0.0, 0.0, 0.0), controller_state=1,
            actor_id="CloudActor",
        ),
    ))
    health = service.session_health()
    assert health["gameplay_ready"] is True
    assert health["diagnosis"] == "ready"


def test_session_control_recovery_is_bounded_and_latches_stop():
    arm = FakeArm()
    clock = StepClock()
    service, backend = make_real_service(
        telemetry=FixedTelemetry(
            map_id="EndGame",
            player=Character(
                "Cloud", 10, 20, 3, 9, position=Vec3(1.0, 2.0, 3.0),
                rotation=Rotation(0.0, 0.0, 0.0), controller_state=1,
                actor_id="CloudActor",
            ),
        ),
        arm=arm,
        monotonic=clock,
        sleeper=lambda _: None,
    )
    result = service.recover_session_control(80, 1.0)
    assert result["ok"] is True
    assert result["outcome"] == "recovered"
    assert result["toggles"] == 2
    assert backend.actions == [("tap", "menu", 80), ("tap", "menu", 80)]
    assert arm.stopped is True


def test_forward_probe_reports_observed_displacement():
    arm = FakeArm()
    service, backend = make_real_service(
        telemetry=AdvancingMovementTelemetry(),
        arm=arm,
        monotonic=time.monotonic,
        sleeper=lambda _: None,
    )
    result = service.probe_forward_movement(100)
    assert result["ok"] is True
    assert result["outcome"] == "movement_observed"
    assert result["horizontal_displacement"] == 1.0
    assert backend.actions == [("adaptive_forward", 100)]
    assert arm.stopped is True


def test_forward_travel_chunk_does_not_release_on_first_small_displacement():
    arm = FakeArm()
    service, backend = make_real_service(
        telemetry=AdvancingMovementTelemetry(),
        arm=arm,
        monotonic=time.monotonic,
        sleeper=lambda _: None,
    )
    result = service.travel_forward_chunk(3000, 0.5)
    assert result["ok"] is True
    assert result["outcome"] == "travel_distance_reached"
    assert result["horizontal_displacement"] == 1.0
    assert backend.actions == [("adaptive_forward", 3000)]
    assert arm.stopped is True


def test_vector_travel_chunk_allows_bounded_diagonal_input():
    arm = FakeArm()
    service, backend = make_real_service(
        telemetry=AdvancingMovementTelemetry(),
        arm=arm,
        monotonic=time.monotonic,
        sleeper=lambda _: None,
    )
    result = service.travel_vector_chunk(-0.35, 1.0, 3000, 0.5)
    assert result["ok"] is True
    assert result["outcome"] == "travel_distance_reached"
    assert result["x"] == pytest.approx(-0.3303504247)
    assert result["y"] == pytest.approx(0.9438583564)
    assert backend.actions == [("adaptive_move", pytest.approx(-0.3303504247), pytest.approx(0.9438583564), 3000, False)]
    assert arm.stopped is True


def test_coordinator_vector_detour_is_forward_biased_and_short():
    service = AgentService(SimulationTelemetry(), SimulationInput())

    with pytest.raises(SafetyError, match="forward-biased"):
        service.navigate_next_segment(
            target_position={"x": 10.0, "y": 0.0, "z": 0.0},
            movement_vector={"x": 0.8, "y": 0.6},
        )

    with pytest.raises(SafetyError, match="limited to 3 m"):
        service.navigate_next_segment(
            target_position={"x": 10.0, "y": 0.0, "z": 0.0},
            maximum_distance_m=4.0,
            movement_vector={"x": -0.35, "y": 1.0},
        )


def test_active_chadley_benchmark_rejects_raw_movement_without_evidence(tmp_path):
    service = AgentService(
        SimulationTelemetry(),
        SimulationInput(),
        benchmark_root=tmp_path / "benchmarks",
    )
    started = service.start_chadley_benchmark(benchmark_id="guard-test")
    assert started["ok"] is True

    with pytest.raises(SafetyError, match="active Chadley benchmark requires"):
        service._require_guarded_benchmark_movement(None)

    service._require_guarded_benchmark_movement({"coordinator": "evidence_fusion"})
    service.finish_chadley_benchmark(outcome="test")


def test_sprint_travel_chunk_uses_explicit_modifier():
    service, backend = make_real_service(
        telemetry=AdvancingMovementTelemetry(),
        monotonic=time.monotonic,
        sleeper=lambda _: None,
    )
    result = service.travel_sprint_chunk("forward", 3000, 0.5)
    assert result["sprint"] is True
    assert backend.actions[0] == ("adaptive_move", 0.0, 1.0, 3000, True)
    assert result["ok"] is True


def test_real_menu_navigation_is_allowed_when_gameplay_is_paused():
    service, backend = make_real_service(telemetry=FixedTelemetry(
        map_id="EndGame",
        paused=True,
        player=Character(
            "Cloud", 10, 20, 3, 9, position=Vec3(1.0, 2.0, 3.0),
            rotation=Rotation(0.0, 0.0, 0.0), controller_state=1,
            actor_id="CloudActor",
        ),
    ))
    result = service.tap("down", 80)
    assert result["mode"] == "menu"
    assert backend.actions == [("tap", "down", 80)]


def test_real_map_controls_require_paused_map_state():
    service, backend = make_real_service(telemetry=FixedTelemetry(
        map_id="EndGame",
        paused=True,
        player=Character(
            "Cloud", 10, 20, 3, 9, position=Vec3(1.0, 2.0, 3.0),
            rotation=Rotation(0.0, 0.0, 0.0), controller_state=1,
            actor_id="CloudActor",
        ),
    ))
    assert service.map_selection("previous", 500)["button"] == "map_selection_prev"
    assert service.map_selection("next", 500)["button"] == "map_selection_next"
    assert service.map_tab("previous", 500)["button"] == "map_tab_prev"
    assert service.map_tab("next", 500)["button"] == "map_tab_next"
    assert service.toggle_map_layer(500)["button"] == "map_layer_toggle"
    assert service.toggle_map_era(500)["button"] == "map_era_toggle"
    assert service.view_map_corresponding(500)["button"] == "map_view_corresponding"
    assert service.drop_map_pin(500)["button"] == "map_drop_pin"
    assert backend.actions == [
        ("tap", "map_selection_prev", 500),
        ("tap", "map_selection_next", 500),
        ("tap", "map_tab_prev", 500),
        ("tap", "map_tab_next", 500),
        ("tap", "map_layer_toggle", 500),
        ("tap", "map_era_toggle", 500),
        ("tap", "map_view_corresponding", 500),
        ("tap", "map_drop_pin", 500),
    ]
    with pytest.raises(SafetyError, match="at least 500 ms"):
        service.map_selection("next", 499)


def test_map_telemetry_reports_runtime_sources_and_ui_gap():
    service, _ = make_real_service(telemetry=FixedTelemetry(
        map_id="EndGame",
        paused=True,
        player=Character(
            "Cloud", 10, 20, 3, 9, position=Vec3(1.0, 2.0, 3.0),
            rotation=Rotation(0.0, 0.0, 0.0), controller_state=1,
            actor_id="CloudActor",
        ),
    ))
    result = service.map_telemetry()
    assert result["map_id"] == "EndGame"
    assert result["map_ui_telemetry_available"] is False
    assert result["sources"]["map_identity"] == "runtime_telemetry"
    assert result["sources"]["map_ui_markers"] == "not_exposed"


def test_real_menu_tab_navigation_is_allowed_when_gameplay_is_paused():
    service, backend = make_real_service(telemetry=FixedTelemetry(
        map_id="EndGame",
        paused=True,
        player=Character(
            "Cloud", 10, 20, 3, 9, position=Vec3(1.0, 2.0, 3.0),
            rotation=Rotation(0.0, 0.0, 0.0), controller_state=1,
            actor_id="CloudActor",
        ),
    ))
    result = service.tap("right", 80)
    assert result["mode"] == "menu"
    assert backend.actions == [("tap", "right", 80)]


def test_real_menu_subtab_navigation_is_allowed_when_gameplay_is_paused():
    service, backend = make_real_service(telemetry=FixedTelemetry(
        map_id="EndGame",
        paused=True,
        player=Character(
            "Cloud", 10, 20, 3, 9, position=Vec3(1.0, 2.0, 3.0),
            rotation=Rotation(0.0, 0.0, 0.0), controller_state=1,
            actor_id="CloudActor",
        ),
    ))
    result = service.tap("sub_right", 80)
    assert result["mode"] == "menu"
    assert backend.actions == [("tap", "sub_right", 80)]


def test_real_duration_zero_direction_and_cooldown_are_rejected():
    times = iter([100.0, 100.0, 100.1, 100.1])
    service, _ = make_real_service(monotonic=lambda: next(times))
    with pytest.raises(InputError):
        service.move(0, 1, 1501)
    with pytest.raises(SafetyError, match="non-zero"):
        service.move(0, 0, 100)
    service.move(0, 1, 100)
    with pytest.raises(SafetyError, match="minimum delay"):
        service.move(0, -1, 100)


def test_broker_survives_agent_reconnect_and_expires(tmp_path):
    now = [1000.0]
    marker = lambda: "test-session"
    arm = ArmState(tmp_path, clock=lambda: now[0], session_marker=marker)
    arm.reset_stop()
    arm.arm(60)
    now[0] += 20
    reconnected_agent = ArmState(
        tmp_path, clock=lambda: now[0], session_marker=marker
    )
    assert reconnected_agent.status() == (True, 40.0)
    now[0] += 41
    assert reconnected_agent.status() == (False, 0.0)


def test_arm_duration_is_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(Exception, match="between 1 and 120"):
        request_arm(121)


def test_broker_fails_closed_for_a_different_windows_session(tmp_path):
    now = [1000.0]
    first = ArmState(
        tmp_path, clock=lambda: now[0], session_marker=lambda: "session-one"
    )
    first.reset_stop()
    first.arm(60)
    next_session = ArmState(
        tmp_path, clock=lambda: now[0], session_marker=lambda: "session-two"
    )
    assert next_session.snapshot() == (False, 0.0, True)


def test_broker_emergency_stop_persists_across_agent_reconnect(tmp_path):
    marker = lambda: "test-session"
    first = ArmState(tmp_path, session_marker=marker)
    first.reset_stop()
    first.arm(60)
    first.latch_stop()
    reconnected_agent = ArmState(tmp_path, session_marker=marker)
    assert reconnected_agent.snapshot() == (False, 0.0, True)


def test_stop_latches():
    service = AgentService(SimulationTelemetry(), SimulationInput())
    service.stop()
    with pytest.raises(SafetyError):
        service.tap("interact")
    with pytest.raises(SafetyError):
        service.move(0, 1, 100)


def test_demo_reaches_target(tmp_path):
    service = AgentService(
        SimulationTelemetry(),
        SimulationInput(),
        benchmark_root=tmp_path / "benchmarks",
    )
    result = service.approach_and_interact(max_steps=8)
    assert result["ok"]
    assert result["outcome"] == "interacted"


def test_stale_real_telemetry_blocks_input(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(seconds=10)
    payload = {
        "schema_version": 1,
        "sequence": 1,
        "timestamp_utc": old.isoformat().replace("+00:00", "Z"),
        "game": {"running": True},
        "player": {"name": "Cloud", "hp": 1, "max_hp": 1, "mp": 1, "max_mp": 1},
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    service = AgentService(JsonFileTelemetry(path), SimulationInput(), max_telemetry_age=2)
    observed = service.observe()
    assert observed["running"] is False
    assert observed["telemetry"]["live"] is False
    assert observed["telemetry"]["last_captured_running"] is True
    with pytest.raises(SafetyError):
        service.tap("interact")


def test_stage_3_fields_and_old_schema_compatibility(tmp_path):
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": 1,
        "sequence": 9,
        "timestamp_utc": now.isoformat().replace("+00:00", "Z"),
        "game": {
            "running": True,
            "map_id": "Field_Test",
            "location": "loc_test",
            "in_combat": True,
        },
        "player": {
            "name": "Cloud",
            "hp": 10,
            "max_hp": 20,
            "mp": 3,
            "max_mp": 9,
            "controlled_character": "Cloud",
            "position": {"x": 1, "y": 2, "z": 3},
            "rotation": {"pitch": 4, "yaw": 5, "roll": 6},
        },
        "target": {"id": "Enemy_Test", "distance": 123.5},
        "nearby_enemies": [{"id": "Enemy_Test", "distance": 123.5}],
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    observed = AgentService(
        JsonFileTelemetry(path), SimulationInput(), max_telemetry_age=2
    ).observe()
    assert observed["running"] is True
    assert observed["telemetry"]["live"] is True
    assert observed["map_id"] == "Field_Test"
    assert observed["player"]["position"] == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert observed["player"]["rotation"] == {"pitch": 4.0, "yaw": 5.0, "roll": 6.0}
    assert observed["target"]["id"] == "Enemy_Test"


def test_stage_4b_bench_detection_parsing(tmp_path):
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": 1,
        "sequence": 10,
        "timestamp_utc": now.isoformat().replace("+00:00", "Z"),
        "game": {"running": True},
        "player": {"name": "Cloud", "hp": 10, "max_hp": 20, "mp": 3, "max_mp": 9},
        "party": [],
        "nearby_interactable": {
            "id": "PersistentLevel.SampleBenchBreak00_C_1",
            "kind": "bench",
            "distance": 125.5,
            "usable": True,
        },
        "rest": {
            "state": "bench_detected",
            "interaction_id": None,
            "state_identity": None,
        },
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    observed = AgentService(JsonFileTelemetry(path), SimulationInput()).observe()
    assert observed["nearby_interactable"] == {
        "id": "PersistentLevel.SampleBenchBreak00_C_1",
        "kind": "bench",
        "distance": 125.5,
        "usable": True,
    }
    assert observed["rest"]["state"] == "bench_detected"


@pytest.mark.parametrize("bad_bench", [
    {"id": "bench", "kind": "bench", "distance": -1, "usable": True},
    {"id": "bench", "kind": "bench", "distance": "bad", "usable": True},
    {"id": "bench", "kind": "bench", "distance": 1, "usable": "yes"},
])
def test_malformed_bench_isolated_from_party_telemetry(tmp_path, bad_bench):
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": 1,
        "sequence": 11,
        "timestamp_utc": now.isoformat().replace("+00:00", "Z"),
        "game": {"running": True},
        "player": {"name": "Cloud", "hp": 10, "max_hp": 20, "mp": 3, "max_mp": 9},
        "party": [{"name": "Cloud", "hp": 10, "max_hp": 20, "mp": 3, "max_mp": 9}],
        "nearby_interactable": bad_bench,
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    observed = AgentService(JsonFileTelemetry(path), SimulationInput()).observe()
    assert observed["nearby_interactable"] is None
    assert observed["party"][0]["hp"] == 10


def usable_bench(distance=1.5, usable=True):
    return Interactable("PersistentLevel.ValidatedBench", "bench", distance, usable)


def chadley_player():
    return Character(
        "Cloud", 5961, 5961, 22, 62,
        position=Vec3(-33646.871094, -211677.125, 3989.840576),
    )


def chadley_telemetry(**changes):
    values = {
        "map_id": "EndGame",
        "player": chadley_player(),
        "nearby_interactable": None,
        "nearby_enemies": (),
    }
    values.update(changes)
    return FixedTelemetry(**values)


def test_physical_chadley_interaction_passes_live_route_gates_and_is_one_shot():
    movement_arm = FakeArm()
    service, backend = make_real_service(
        telemetry=chadley_telemetry(), arm=movement_arm,
    )
    result = service.interact_with_physical_chadley()
    assert result["outcome"] == "physical_chadley_interaction_sent"
    assert result["route_id"] == "nibelheim_upper_square"
    assert backend.actions == [("interaction", 250)]
    assert backend.release_count == 1
    assert movement_arm.stopped


@pytest.mark.parametrize(
    ("telemetry", "environment", "reason"),
    [
        (chadley_telemetry(map_id="Midgar"), FakeEnvironment(), "EndGame map"),
        (chadley_telemetry(player=None), FakeEnvironment(), "spatial telemetry"),
        (chadley_telemetry(in_combat=True), FakeEnvironment(), "combat or targeting"),
        (chadley_telemetry(nearby_enemies=(object(),)), FakeEnvironment(), "combat or targeting"),
        (chadley_telemetry(), FakeEnvironment(process=False), "process"),
        (chadley_telemetry(), FakeEnvironment(foreground=False), "foreground"),
        (
            chadley_telemetry(
                player=Character(
                    "Cloud", 5961, 5961, 22, 62,
                    position=Vec3(-50000, -211677, 3989),
                )
            ),
            FakeEnvironment(),
            "outside",
        ),
    ],
)
def test_physical_chadley_route_gates_reject(telemetry, environment, reason):
    service, backend = make_real_service(telemetry=telemetry, environment=environment)
    with pytest.raises(SafetyError, match=reason):
        service.interact_with_physical_chadley()
    assert not any(action[0] == "interaction" for action in backend.actions)
    assert backend.release_count == 1


def test_use_nearby_bench_passes_gates_consumes_arm_and_latches_stop():
    movement_arm = FakeArm()
    bench_arm = FakeArm(remaining=30.0)
    service, backend = make_real_service(
        telemetry=FixedTelemetry(nearby_interactable=usable_bench()),
        arm=movement_arm, bench_arm=bench_arm,
    )
    result = service.use_nearby_bench()
    assert result["outcome"] == "interaction_sent"
    assert result["bench"]["id"] == "PersistentLevel.ValidatedBench"
    assert backend.actions == [("bench", 250)]
    assert backend.release_count == 1
    assert movement_arm.stopped and bench_arm.stopped
    assert service.input_status()["bench_armed"] is False
    with pytest.raises(SafetyError, match="emergency stop"):
        service.use_nearby_bench()


@pytest.mark.parametrize(
    ("telemetry", "bench_arm", "environment", "reason"),
    [
        (FixedTelemetry(running=False, nearby_interactable=usable_bench()), FakeArm(), FakeEnvironment(), "not running"),
        (FixedTelemetry(paused=True, nearby_interactable=usable_bench()), FakeArm(), FakeEnvironment(), "paused"),
        (FixedTelemetry(in_menu=True, nearby_interactable=usable_bench()), FakeArm(), FakeEnvironment(), "Main Menu"),
        (FixedTelemetry(target=__import__("ff7r_agent.models", fromlist=["ObservedActor"]).ObservedActor("enemy"), nearby_interactable=usable_bench()), FakeArm(), FakeEnvironment(), "target"),
        (FixedTelemetry(nearby_interactable=None), FakeArm(), FakeEnvironment(), "no validated"),
        (FixedTelemetry(nearby_interactable=usable_bench(1.5, False)), FakeArm(), FakeEnvironment(), "not currently usable"),
        (FixedTelemetry(nearby_interactable=usable_bench(3.01)), FakeArm(), FakeEnvironment(), "outside"),
        (FixedTelemetry(nearby_interactable=usable_bench()), FakeArm(), FakeEnvironment(process=False), "process"),
        (FixedTelemetry(nearby_interactable=usable_bench()), FakeArm(), FakeEnvironment(foreground=False), "foreground"),
        (FixedTelemetry(nearby_interactable=usable_bench()), FakeArm(armed=False, remaining=0), FakeEnvironment(), "not manually armed"),
    ],
)
def test_each_bench_gate_rejects(telemetry, bench_arm, environment, reason):
    service, backend = make_real_service(
        telemetry=telemetry, bench_arm=bench_arm, environment=environment
    )
    with pytest.raises(SafetyError, match=reason):
        service.use_nearby_bench()
    assert not any(action[0] == "bench" for action in backend.actions)
    assert backend.release_count == 1


def test_stale_telemetry_rejects_bench():
    old = (datetime.now(timezone.utc) - timedelta(seconds=3)).isoformat().replace("+00:00", "Z")
    service, _ = make_real_service(
        telemetry=FixedTelemetry(timestamp_utc=old, nearby_interactable=usable_bench())
    )
    with pytest.raises(SafetyError, match="stale"):
        service.use_nearby_bench()


def test_concurrent_action_rejects_bench():
    service, backend = make_real_service(
        telemetry=FixedTelemetry(nearby_interactable=usable_bench())
    )
    assert service._action_lock.acquire(blocking=False)
    try:
        with pytest.raises(SafetyError, match="another input action"):
            service.use_nearby_bench()
    finally:
        service._action_lock.release()
    assert backend.actions == []


def test_movement_arm_does_not_authorise_bench_and_bench_arm_does_not_authorise_movement():
    service, _ = make_real_service(
        telemetry=FixedTelemetry(nearby_interactable=usable_bench()),
        arm=FakeArm(), bench_arm=FakeArm(armed=False, remaining=0),
    )
    with pytest.raises(SafetyError, match="bench interaction is not manually armed"):
        service.use_nearby_bench()
    service, _ = make_real_service(
        telemetry=FixedTelemetry(nearby_interactable=usable_bench()),
        arm=FakeArm(armed=False, remaining=0), bench_arm=FakeArm(remaining=30),
    )
    with pytest.raises(SafetyError, match="real input is not manually armed"):
        service.move(0, 1, 100)


def test_bench_backend_press_failure_still_releases(monkeypatch):
    fake = FakePyDirectInput(down=False, up=True)
    backend = make_windows_backend(monkeypatch, fake)
    with pytest.raises(InputError, match="failed to press bench interaction key: f"):
        backend.use_bench(100)
    assert fake.calls == [("down", "f"), ("up", "f")]
    assert backend._held == set()


def test_bench_backend_release_failure_clears_tracking(monkeypatch):
    fake = FakePyDirectInput(down=True, up=False)
    backend = make_windows_backend(monkeypatch, fake)
    with pytest.raises(InputError, match="failed to release bench interaction key: f"):
        backend.use_bench(100)
    assert fake.calls == [("down", "f"), ("up", "f")]
    assert backend._held == set()


def test_bench_arm_is_capped_expires_and_is_separate(tmp_path):
    now = [1000.0]
    marker = lambda: "test-session"
    bench = BenchArmState(tmp_path, clock=lambda: now[0], session_marker=marker)
    bench.reset_stop()
    with pytest.raises(Exception, match="between 1 and 30"):
        bench.arm(31)
    bench.arm(30)
    assert bench.status() == (True, 30.0)
    movement = ArmState(tmp_path, clock=lambda: now[0], session_marker=marker)
    assert movement.status() == (False, 0.0)
    now[0] += 31
    assert bench.status() == (False, 0.0)


def test_camera_arm_is_capped_and_separate(tmp_path):
    now = [1000.0]
    marker = lambda: "test-session"
    camera = CameraArmState(tmp_path, clock=lambda: now[0], session_marker=marker)
    camera.reset_stop()
    with pytest.raises(Exception, match="between 1 and 120"):
        camera.arm(121)
    camera.arm(120)
    assert camera.status() == (True, 120.0)
    movement = ArmState(tmp_path, clock=lambda: now[0], session_marker=marker)
    assert movement.status() == (False, 0.0)


def test_calibrated_camera_service_requires_camera_arm_and_latches_after_send():
    camera = FakeArm(remaining=120.0)
    service, backend = make_real_service(camera_arm=camera)
    result = service.calibrate_camera_horizontal(120)
    assert result["outcome"] == "camera_calibration_sent"
    assert backend.actions == [("calibrate_camera_horizontal", 120)]
    assert camera.stopped is True


def test_camera_bearing_aligner_waits_for_settled_feedback_before_next_step():
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat().replace("+00:00", "Z")
    player = Character(
        "Cloud", 10, 20, 3, 9, position=Vec3(0.0, 0.0, 0.0),
        rotation=Rotation(0.0, 0.0, 0.0), controller_state=1,
        actor_id="CloudActor",
    )

    def snapshot(sequence, yaw):
        return GameState(
            1, sequence, timestamp, True, False, False, False, "", "", player,
            map_id="EndGame", source="test",
            camera=CameraState(rotation=Rotation(0.0, yaw, 0.0)),
        )

    class SequenceTelemetry:
        def __init__(self):
            self.states = [snapshot(1, 0.0), snapshot(2, 45.0),
                           snapshot(3, 90.0), snapshot(4, 90.0),
                           snapshot(5, 90.0), snapshot(6, 90.0),
                           snapshot(7, 90.0)]
            self.index = 0

        def observe(self):
            value = self.states[min(self.index, len(self.states) - 1)]
            self.index += 1
            return value

    clock = [0.0]
    camera = FakeArm(remaining=120.0)
    service = AgentService(
        SequenceTelemetry(), FakeRealInput(),
        arm_state=FakeArm(), camera_arm_state=camera,
        environment=FakeEnvironment(), monotonic=lambda: clock.__setitem__(0, clock[0] + 0.03) or clock[0],
        sleeper=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        minimum_action_delay=0.0,
    )
    result = service.align_camera_to_bearing(90.0)
    assert result["ok"] is True
    assert result["outcome"] == "camera_aligned"
    assert result["final_heading_error_degrees"] == pytest.approx(0.0)
    assert result["steps"][0]["horizontal_delta"] == 500
    assert camera.stopped is True


def test_smooth_camera_alignment_is_one_steered_motion_that_lands(monkeypatch):
    # Live 2026-10-05: settled pulses looked janky on stream, and one
    # open-loop sweep landed 14-42 degrees short because a sweep's turn per
    # pixel is not constant. The steered turn sends back-to-back slices and
    # stops on the target whatever the true scale is (36 px a degree here).
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    player = Character("Cloud", 10, 20, 3, 9, position=Vec3(0.0, 0.0, 0.0),
                       rotation=Rotation(0.0, 0.0, 0.0), controller_state=1, actor_id="CloudActor")
    camera = {"yaw": 0.0, "sequence": 1}

    class CameraTelemetry:
        def observe(self):
            camera["sequence"] += 1
            return GameState(1, camera["sequence"], timestamp, True, False, False, False, "", "", player,
                             map_id="EndGame", source="test",
                             camera=CameraState(rotation=Rotation(0.0, camera["yaw"], 0.0)))

    class SliceInput(FakeRealInput):
        def __init__(self):
            super().__init__()
            self.slices = []

        def camera_move_linear(self, delta, milliseconds):
            self.slices.append((delta, milliseconds))
            camera["yaw"] += delta / 36.0
            return {"sent_delta": delta}

    clock = [0.0]
    backend = SliceInput()
    service = AgentService(
        CameraTelemetry(), backend, arm_state=FakeArm(), camera_arm_state=FakeArm(remaining=120.0),
        environment=FakeEnvironment(), monotonic=lambda: clock.__setitem__(0, clock[0] + 0.03) or clock[0],
        sleeper=lambda seconds: clock.__setitem__(0, clock[0] + seconds), minimum_action_delay=0.0,
    )
    result = service.align_camera_to_bearing(150.0, tolerance_degrees=5.0, smooth=True)
    assert result["ok"] is True and result["outcome"] == "camera_aligned"
    assert abs(result["final_heading_error_degrees"]) <= 5.0
    assert len(backend.slices) > 3 and all(delta > 0 for delta, _ in backend.slices)  # one direction, no corrections
    assert not any(action[0] == "calibrate_camera_horizontal" for action in backend.actions)
    assert [step["smooth"] for step in result["steps"]] == [True]
    assert result["steps"][0]["stop"] == "on_target"


def test_camera_aligner_uses_bench_bearing_and_checks_telemetry_feedback():
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat().replace("+00:00", "Z")
    player = Character(
        "Cloud", 10, 20, 3, 9, position=Vec3(0.0, 0.0, 0.0),
        rotation=Rotation(0.0, 0.0, 0.0), controller_state=1,
        actor_id="CloudActor",
    )
    bench = Interactable(
        "ValidatedBench", "bench", 2.0, True,
        position=Vec3(200.0, 0.0, 0.0),
        relative=RelativePosition(2.0, 0.0, 0.0, 2.0, 90.0, 90.0),
    )

    def snapshot(sequence, yaw):
        return GameState(
            1, sequence, timestamp, True, False, False, False, "", "", player,
            map_id="EndGame", nearby_interactable=bench, source="test",
            camera=CameraState(rotation=Rotation(0.0, yaw, 0.0)),
        )

    class SequenceTelemetry:
        def __init__(self):
            self.states = [snapshot(1, 0.0), snapshot(2, 45.0),
                           snapshot(3, 90.0), snapshot(4, 90.0),
                           snapshot(5, 90.0), snapshot(6, 90.0),
                           snapshot(7, 90.0), snapshot(8, 90.0)]
            self.index = 0

        def observe(self):
            value = self.states[min(self.index, len(self.states) - 1)]
            self.index += 1
            return value

    clock = [0.0]
    service = AgentService(
        SequenceTelemetry(), FakeRealInput(),
        arm_state=FakeArm(), camera_arm_state=FakeArm(remaining=120.0),
        environment=FakeEnvironment(), monotonic=lambda: clock.__setitem__(0, clock[0] + 0.03) or clock[0],
        sleeper=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        minimum_action_delay=0.0,
    )
    result = service.align_camera_to_detected_bench()
    assert result["outcome"] == "camera_aligned"
    assert result["horizontal_delta"] == 500
    assert result["initial_heading_error_degrees"] == pytest.approx(90.0)
    assert result["post_heading_error_degrees"] == pytest.approx(0.0)


def test_old_screenshots_are_pruned_to_the_newest(tmp_path) -> None:
    import os
    from ff7r_agent.service import prune_screenshots

    for index in range(5):
        path = tmp_path / f"ff7r-{index}.bmp"
        path.write_bytes(b"x")
        os.utime(path, (1000 + index, 1000 + index))
    (tmp_path / "notes.txt").write_text("keep me")
    assert prune_screenshots(tmp_path, 2) == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ff7r-3.bmp", "ff7r-4.bmp", "notes.txt"]
