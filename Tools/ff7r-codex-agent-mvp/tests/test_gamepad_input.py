from __future__ import annotations

from threading import Lock
from types import SimpleNamespace

import types

import pytest

from ff7r_agent import factory
from ff7r_agent.input import InputError, WindowsHybridGamepadInput


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeGamepad:
    def __init__(self, *, fail_neutral: bool = False, supports_reset: bool = False) -> None:
        self.reports: list[tuple[float, float]] = []
        self.updates = 0
        self.fail_neutral = fail_neutral
        self.supports_reset = supports_reset
        self.reset_calls = 0

    def left_joystick_float(self, x_value_float: float, y_value_float: float) -> None:
        if self.fail_neutral and (x_value_float, y_value_float) == (0.0, 0.0):
            raise RuntimeError("simulated neutral failure")
        self.reports.append((x_value_float, y_value_float))

    def update(self) -> None:
        self.updates += 1

    def reset(self) -> None:
        if not self.supports_reset:
            raise AttributeError("reset is not supported")
        self.reset_calls += 1


class FakeKeyboard:
    def __init__(self) -> None:
        self.keymap = {"sprint": "shift"}
        self._lock = Lock()
        self.calls: list[tuple[object, ...]] = []
        self._records: list[dict[str, object]] = []

    def _key_down(self, key: str) -> None:
        self.calls.append(("down", key))

    def _release_keys(self, keys: list[str]) -> None:
        for key in keys:
            self.calls.append(("up", key))

    def tap(self, button: str, milliseconds: int) -> None:
        self.calls.append(("tap", button, milliseconds))

    def look(self, dx: int, dy: int, milliseconds: int) -> None:
        self.calls.append(("look", dx, dy, milliseconds))

    def calibrate_camera_horizontal(self, delta: int) -> dict[str, object]:
        self.calls.append(("calibrate", delta))
        return {"event": "camera_horizontal_relative", "horizontal_delta": delta}

    def use_interaction(self, milliseconds: int) -> dict[str, object]:
        self.calls.append(("interaction", milliseconds))
        return {"button": "keyboard_f"}

    def use_bench(self, milliseconds: int) -> dict[str, object]:
        self.calls.append(("bench", milliseconds))
        return {"button": "keyboard_f"}

    def release_all(self) -> None:
        self.calls.append(("release_all",))

    def diagnostic_key_up(self, key: str) -> bool:
        self.calls.append(("diagnostic_key_up", key))
        return True

    def event_record_count(self) -> int:
        return len(self._records)

    def event_records_since(self, index: int) -> list[dict[str, object]]:
        return self._records[index:]


def make_backend(*, fail_neutral: bool = False, supports_reset: bool = False):
    clock = FakeClock()
    keyboard = FakeKeyboard()
    gamepad = FakeGamepad(
        fail_neutral=fail_neutral,
        supports_reset=supports_reset,
    )
    backend = WindowsHybridGamepadInput(
        keyboard=keyboard,
        gamepad=gamepad,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    return backend, keyboard, gamepad, clock


def test_move_preserves_float_vector_and_neutralises_stick():
    backend, keyboard, gamepad, _ = make_backend()

    backend.move(-0.35, 1.0, 20)

    assert gamepad.reports == [pytest.approx((-0.3303504247, 0.9438583564)), (0.0, 0.0)]
    assert gamepad.updates == 2
    assert keyboard.calls == []
    assert backend.backend_status()["last_stick"] == {"x": 0.0, "y": 0.0}
    assert backend.backend_status()["stick_state"] == "neutral"
    assert backend.backend_status()["neutralisation_error"] is None


def test_hybrid_input_pulses_a_through_the_existing_gamepad_proxy():
    backend, keyboard, gamepad, _ = make_backend()
    calls = []
    gamepad.press_a = lambda milliseconds: calls.append(milliseconds) or {
        "ok": True,
        "button": "x360_a",
        "milliseconds": milliseconds,
        "released": True,
    }

    result = backend.press_gamepad_a(1000)

    assert calls == [1000]
    assert result["button"] == "x360_a"
    assert keyboard.calls == []
    assert backend._event_records[-1]["event"] == "gamepad_button_a"


def test_adaptive_move_stops_and_neutralises_without_keyboard_movement():
    backend, keyboard, gamepad, _ = make_backend()

    result = backend.adaptive_move(
        0.5,
        0.8660254,
        lambda: "visual_review_required",
        maximum_ms=100,
    )

    assert result["release_reason"] == "visual_review_required"
    assert result["gamepad"] is True
    assert result["keys"] == ["left_stick"]
    assert gamepad.reports[-1] == (0.0, 0.0)
    assert keyboard.calls == []


def test_adaptive_move_releases_sprint_after_stick_neutralisation():
    backend, keyboard, gamepad, _ = make_backend()

    result = backend.adaptive_move(
        0.0,
        1.0,
        lambda: "test_stop",
        maximum_ms=100,
        sprint=True,
    )

    assert result["keys"] == ["left_stick", "shift"]
    assert keyboard.calls == [("down", "shift"), ("up", "shift")]
    assert gamepad.reports[-1] == (0.0, 0.0)


def test_adaptive_move_neutralises_when_stop_callback_raises():
    backend, _, gamepad, _ = make_backend()

    def fail() -> str | None:
        raise RuntimeError("telemetry failed")

    with pytest.raises(RuntimeError, match="telemetry failed"):
        backend.adaptive_move(0.0, 1.0, fail, maximum_ms=100)

    assert gamepad.reports[-1] == (0.0, 0.0)


def test_release_all_attempts_keyboard_cleanup_after_gamepad_failure():
    backend, keyboard, gamepad, _ = make_backend(fail_neutral=True)

    with pytest.raises(InputError, match="hybrid input release failed"):
        backend.release_all()

    assert gamepad.updates == 1
    assert keyboard.calls == [("release_all",)]
    assert backend.backend_status()["stick_state"] == "unknown"
    assert backend.backend_status()["neutralisation_error"]


def test_neutralisation_uses_reset_fallback_when_direct_report_fails():
    backend, _, gamepad, _ = make_backend(fail_neutral=True, supports_reset=True)

    backend.release_all()

    assert gamepad.reset_calls == 1
    assert backend.backend_status()["stick_state"] == "neutral"
    assert backend.backend_status()["last_stick"] == {"x": 0.0, "y": 0.0}


@pytest.mark.parametrize("poll_seconds", [0.009, 0.101])
def test_adaptive_move_rejects_polling_outside_shared_safety_bound(poll_seconds):
    backend, _, _, _ = make_backend()

    with pytest.raises(InputError, match="between 10 and 100 ms"):
        backend.adaptive_move(
            0.0,
            1.0,
            lambda: "test_stop",
            maximum_ms=100,
            poll_seconds=poll_seconds,
        )


def test_non_movement_actions_delegate_to_keyboard_backend():
    backend, keyboard, _, _ = make_backend()

    backend.tap("map", 500)
    backend.look(30, 0, 80)
    backend.calibrate_camera_horizontal(45)

    assert keyboard.calls == [
        ("tap", "map", 500),
        ("look", 30, 0, 80),
        ("calibrate", 45),
    ]


def test_factory_keeps_real_input_safety_for_gamepad_backend(monkeypatch, tmp_path):
    fake_backend = SimpleNamespace(is_real=True)
    monkeypatch.setenv("FF7R_INPUT_BACKEND", "windows_gamepad")
    monkeypatch.delenv("FF7R_TELEMETRY_FILE", raising=False)
    monkeypatch.setenv("FF7R_AUTONOMY_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(factory, "WindowsHybridGamepadInput", lambda **_: fake_backend)
    monkeypatch.setattr(factory, "default_runtime_dir", lambda: tmp_path)

    service = factory.create_service()

    assert service.input is fake_backend
    assert service._arm is not None
    assert service._bench_arm is not None
    assert service._camera_arm is not None
    assert service._session_lease is not None
    assert service._settled_gameplay_samples == 3


def test_factory_requires_explicit_background_input_opt_in(monkeypatch, tmp_path):
    fake_backend = SimpleNamespace(is_real=True)
    captured = {}
    monkeypatch.setenv("FF7R_INPUT_BACKEND", "windows_gamepad")
    monkeypatch.setenv("FF7R_BACKGROUND_INPUT", "1")
    monkeypatch.delenv("FF7R_TELEMETRY_FILE", raising=False)
    monkeypatch.setenv("FF7R_AUTONOMY_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        factory, "WindowsHybridGamepadInput",
        lambda **kwargs: (captured.update(kwargs) or fake_backend),
    )
    monkeypatch.setattr(factory, "default_runtime_dir", lambda: tmp_path)

    service = factory.create_service()

    assert service.input is fake_backend
    assert captured["background_only"] is True


def test_invalid_gamepad_provider_fails_closed():
    keyboard = FakeKeyboard()
    with pytest.raises(InputError, match="left_joystick_float"):
        WindowsHybridGamepadInput(keyboard=keyboard, gamepad=object())


def test_reconnect_gamepad_releases_old_device_and_creates_replacement():
    keyboard = FakeKeyboard()
    first = FakeGamepad()
    second = FakeGamepad()
    created = iter((second,))
    backend = WindowsHybridGamepadInput(
        keyboard=keyboard,
        gamepad=first,
        gamepad_factory=lambda: next(created),
    )

    result = backend.reconnect_gamepad()

    assert result["ok"] is True
    assert result["event"] == "gamepad_reconnected_after_gameplay_ready"
    assert first.reports[-1] == (0.0, 0.0)
    assert backend.backend_status()["provider"] == "FakeGamepad"
    assert backend.backend_status()["stick_state"] == "neutral"


def test_card_game_buttons_pulse_through_the_gamepad_broker():
    from ff7r_agent.input import WindowsHybridGamepadInput

    class RemotePad:
        def __init__(self):
            self.pulses = []
        def left_joystick_float(self, x, y):
            pass
        def update(self):
            pass
        def pulse_button(self, button, milliseconds):
            self.pulses.append((button, milliseconds))
            return {"ok": True, "released": True}

    pad = RemotePad()
    backend = WindowsHybridGamepadInput(keyboard=object(), gamepad=pad)
    backend.tap_gamepad("queen_blood_right", 250)
    backend.tap_gamepad("queen_blood_play", 2200)
    assert pad.pulses == [("XUSB_GAMEPAD_DPAD_RIGHT", 250), ("XUSB_GAMEPAD_Y", 2200)]
    with pytest.raises(Exception, match="no gamepad binding"):
        backend.tap_gamepad("map", 500)


def test_background_backend_uses_only_allowlisted_gamepad_input():
    class BackgroundPad(FakeGamepad):
        def __init__(self):
            super().__init__()
            self.buttons = []
            self.camera = []

        def pulse_button(self, button, milliseconds):
            self.buttons.append((button, milliseconds))
            return {"ok": True, "released": True}

        def pulse_right_stick(self, x, y, milliseconds):
            self.camera.append((x, y, milliseconds))
            return {"ok": True, "released": True}

    keyboard = FakeKeyboard()
    gamepad = BackgroundPad()
    backend = WindowsHybridGamepadInput(
        keyboard=keyboard, gamepad=gamepad, background_only=True,
    )

    backend.tap("confirm", 100)
    backend.use_interaction(120)
    backend.look(1, 0, 100)
    calibrated = backend.calibrate_camera_horizontal(-1)

    assert backend.supports_background_input is True
    assert keyboard.calls == []
    assert gamepad.buttons == [
        ("XUSB_GAMEPAD_A", 100),
        ("XUSB_GAMEPAD_Y", 120),
    ]
    x, y, duration = gamepad.camera[0]
    assert (x, y) == pytest.approx((0.30, 0.0))
    assert duration == 100
    assert gamepad.camera[1] == pytest.approx((-0.30, 0.0, 100))
    assert calibrated == {"ok": True, "backend": "gamepad_right_stick", "delta": -1}
    assert backend.backend_status()["camera_and_buttons_control"] == (
        "virtual_x360_right_stick_and_button_pulses"
    )

    with pytest.raises(InputError, match="mouse clicks are unavailable"):
        backend.mouse_click("left", 100)
    with pytest.raises(InputError, match="mouse clicks are unavailable"):
        backend.mouse_click_in_game_window("left", 100, 123)
    with pytest.raises(InputError, match="no gamepad binding"):
        backend.tap("guard", 100)
    with pytest.raises(InputError, match="keyboard diagnostics are unavailable"):
        backend.diagnostic_key_up("w")
    with pytest.raises(InputError, match="supervised continuous movement"):
        backend.adaptive_move(0.0, 1.0, lambda: "stop", sprint=True)
    with pytest.raises(InputError, match="bounded to 300 ms"):
        backend.look(30, 0, 301)
    assert keyboard.calls == []


def test_background_camera_requires_broker_neutral_release_acknowledgement():
    backend, keyboard, gamepad, _ = make_backend()
    gamepad.pulse_right_stick = lambda *_args: {"ok": True, "released": False}
    backend.supports_background_input = True

    with pytest.raises(InputError, match="neutral release"):
        backend.look(30, 0, 100)

    assert keyboard.calls == []


def test_a_camera_sweep_turns_a_whole_aim_in_one_eased_motion(monkeypatch):
    import threading
    from ff7r_agent import input as input_module
    moves = []
    backend = input_module.WindowsKeyboardInput.__new__(input_module.WindowsKeyboardInput)
    backend._pdi = types.SimpleNamespace(moveRel=lambda dx, dy, **kwargs: moves.append(dx))
    backend._camera_lock = threading.Lock()
    backend._monotonic = lambda: 0.0
    backend._foreground_identity = lambda: (1, 2)
    backend._write_event_record = lambda record: None
    backend._cursor_x_and_bounds = lambda: None  # no real cursor here
    monkeypatch.setattr(input_module, "mark_gameplay_input", lambda: None)
    monkeypatch.setattr(input_module.time, "sleep", lambda seconds: None)
    record = backend.camera_move_linear(900, 200)
    assert record["sent_delta"] == sum(moves) == 900
    assert len(moves) >= 10 and max(moves) - min(moves) <= 1  # even steps: constant speed
    with pytest.raises(input_module.InputError):
        backend.smooth_camera_horizontal(600, 300)  # route-follow turns keep their bound
    with pytest.raises(input_module.InputError):
        backend.camera_move_linear(1001, 100)


def test_a_camera_sweep_recentres_the_cursor_before_an_edge(monkeypatch):
    # Live 2026-10-05 sweeps stopped turning once the cursor reached the edge.
    import threading
    from ff7r_agent import input as input_module
    cursor = {"x": 1400}
    warps = []
    def move(dx, dy, **kwargs):
        cursor["x"] = max(0, min(1535, cursor["x"] + dx))
    def warp(x):
        warps.append(cursor["x"]); cursor["x"] = x
    backend = input_module.WindowsKeyboardInput.__new__(input_module.WindowsKeyboardInput)
    backend._pdi = types.SimpleNamespace(moveRel=move)
    backend._camera_lock = threading.Lock()
    backend._monotonic = lambda: 0.0
    backend._foreground_identity = lambda: (1, 2)
    backend._write_event_record = lambda record: None
    backend._cursor_x_and_bounds = lambda: (cursor["x"], 0, 1536)
    backend._warp_cursor_x = warp
    monkeypatch.setattr(input_module, "mark_gameplay_input", lambda: None)
    monkeypatch.setattr(input_module.time, "sleep", lambda seconds: None)
    for _ in range(4):
        backend.camera_move_linear(650, 100)
    assert warps and all(x > 1536 - 40 - 200 for x in warps)  # only near the right edge
    assert 40 <= cursor["x"] <= 1536 - 40
