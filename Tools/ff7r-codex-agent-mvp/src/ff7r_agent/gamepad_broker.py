from __future__ import annotations

import argparse
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from .continuous_control import SupervisedStickLease

from .runtime_ownership import (
    BROKER_OWNERSHIP_PROTOCOL,
    RuntimeOwnershipError,
    acquire_broker_ownership,
    assert_no_existing_broker,
    owner_is_alive,
    process_identity_token,
)
from .subprocess_options import no_console_window_kwargs


class GamepadBrokerError(RuntimeError):
    pass


XUSB_GAMEPAD_A = 0x1000
# Buttons a pulse may press (XUSB report bits). Card-game input uses the
# D-pad and face buttons; the longest pulse is Queen's Blood's pass hold.
PULSE_BUTTONS = {
    "XUSB_GAMEPAD_DPAD_UP": 0x0001,
    "XUSB_GAMEPAD_DPAD_DOWN": 0x0002,
    "XUSB_GAMEPAD_DPAD_LEFT": 0x0004,
    "XUSB_GAMEPAD_DPAD_RIGHT": 0x0008,
    "XUSB_GAMEPAD_START": 0x0010,
    "XUSB_GAMEPAD_BACK": 0x0020,
    "XUSB_GAMEPAD_LEFT_SHOULDER": 0x0100,
    "XUSB_GAMEPAD_RIGHT_SHOULDER": 0x0200,
    "XUSB_GAMEPAD_A": 0x1000,
    "XUSB_GAMEPAD_B": 0x2000,
    "XUSB_GAMEPAD_X": 0x4000,
    "XUSB_GAMEPAD_Y": 0x8000,
}
MAX_PULSE_MS = 2500


def _exchange(port: int, payload: dict[str, Any], timeout: float = 1.0) -> dict[str, Any]:
    raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as stream:
        stream.sendall(raw)
        reader = stream.makefile("rb")
        line = reader.readline()
    if not line:
        raise GamepadBrokerError("gamepad broker closed without a response")
    result = json.loads(line.decode("utf-8"))
    if result.get("ok") is not True:
        raise GamepadBrokerError(str(result.get("error") or "gamepad broker rejected command"))
    return result


class RemoteVirtualLeftStick:
    """VirtualLeftStick proxy whose device is owned by a separate stable process."""

    def __init__(self, port: int, heartbeat_seconds: float = 0.10) -> None:
        self.port = int(port)
        self._heartbeat_seconds = float(heartbeat_seconds)
        self._x = 0.0
        self._y = 0.0
        self._active = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        _exchange(self.port, {"op": "status"})

    def left_joystick_float(self, x_value_float: float, y_value_float: float) -> None:
        self._x = float(x_value_float)
        self._y = float(y_value_float)

    def update(self) -> None:
        _exchange(self.port, {"op": "set", "x": self._x, "y": self._y})
        active = abs(self._x) > 1e-6 or abs(self._y) > 1e-6
        if active and not self._active:
            self._active = True
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._heartbeat, args=(self._stop,), daemon=True)
            self._thread.start()
        elif not active:
            self._active = False
            self._stop.set()

    def set_supervised(self, x, y, *, deadline, stream_id, sequence, sprint=False):
        self._active = False
        self._stop.set()
        result = _exchange(self.port, {"op": "supervised_set", "x": x, "y": y,
            "deadline": deadline, "stream_id": stream_id, "sequence": sequence, "sprint": sprint})
        self._x, self._y = float(x), float(y)
        return result

    def press_a(self, milliseconds: int) -> dict[str, Any]:
        value = int(milliseconds)
        if isinstance(milliseconds, bool) or not 20 <= value <= 1500:
            raise GamepadBrokerError("gamepad A pulse duration must be between 20 and 1500 ms")
        return _exchange(
            self.port,
            {"op": "pulse_a", "milliseconds": value},
            timeout=value / 1000.0 + 1.0,
        )

    def pulse_right_stick(self, x: float, y: float, milliseconds: int) -> dict[str, Any]:
        return _exchange(self.port, {"op": "pulse_right_stick", "x": x, "y": y,
            "milliseconds": milliseconds}, timeout=1.5)

    def pulse_button(self, button: str, milliseconds: int) -> dict[str, Any]:
        value = int(milliseconds)
        if button not in PULSE_BUTTONS:
            raise GamepadBrokerError(f"gamepad button is not allow-listed: {button}")
        if isinstance(milliseconds, bool) or not 20 <= value <= MAX_PULSE_MS:
            raise GamepadBrokerError(f"gamepad pulse duration must be between 20 and {MAX_PULSE_MS} ms")
        return _exchange(
            self.port,
            {"op": "pulse_button", "button": button, "milliseconds": value},
            timeout=value / 1000.0 + 1.0,
        )

    def reset(self) -> None:
        self._x = self._y = 0.0
        self.update()

    def _heartbeat(self, stop: threading.Event) -> None:
        while not stop.wait(self._heartbeat_seconds):
            if not self._active:
                return
            try:
                _exchange(self.port, {"op": "heartbeat"}, timeout=0.5)
            except Exception:
                return


class _AttachedBrokerProcess:
    """Process-shaped view of a broker owned by the stable transport host."""

    def __init__(self, pid: int, port: int) -> None:
        self.pid = int(pid)
        self.port = int(port)

    def poll(self) -> int | None:
        try:
            result = _exchange(self.port, {"op": "status"})
        except Exception:
            return 1
        return None if result.get("process_alive") else 1


@dataclass
class BrokerProcess:
    process: Any
    port: int
    owns_process: bool = True

    @classmethod
    def attach(cls, *, port: int, process_id: int) -> "BrokerProcess":
        if int(port) <= 0 or int(process_id) <= 0:
            raise GamepadBrokerError("shared gamepad broker identity is invalid")
        return cls(_AttachedBrokerProcess(process_id, port), int(port), owns_process=False)

    @staticmethod
    def _readline_with_timeout(stream: Any, timeout_seconds: float) -> str:
        value: list[str] = []

        def read() -> None:
            value.append(stream.readline())

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout_seconds)
        if reader.is_alive():
            raise GamepadBrokerError("gamepad broker startup timed out")
        return value[0] if value else ""

    @classmethod
    def start(cls, timeout_seconds: float = 5.0) -> "BrokerProcess":
        # This is a read-only preflight.  The named mutex in ``serve`` remains
        # authoritative and closes the simultaneous-start race.
        assert_no_existing_broker()
        owner_pid = os.getpid()
        owner_token = process_identity_token(owner_pid)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ff7r_agent.gamepad_broker",
                "serve",
                "--owner-pid",
                str(owner_pid),
                "--owner-token",
                owner_token or "",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=os.environ.copy(),
            **no_console_window_kwargs(),
        )
        assert process.stdout is not None
        try:
            line = cls._readline_with_timeout(process.stdout, timeout_seconds)
            ready = json.loads(line)
            if ready.get("ok") is not True:
                raise GamepadBrokerError(str(ready.get("error") or "gamepad broker rejected startup"))
            port = int(ready["port"])
        except Exception as exc:
            process.terminate()
            raise GamepadBrokerError(f"gamepad broker failed to start: {locals().get('line', '')!r}") from exc
        return cls(process, port)

    def status(self) -> dict[str, Any]:
        result = _exchange(self.port, {"op": "status"})
        result.setdefault("process_alive", self.process.poll() is None)
        result.setdefault("listener_alive", True)
        result["pid"] = self.process.pid
        result["port"] = self.port
        return result

    def neutralise(self) -> None:
        _exchange(self.port, {"op": "set", "x": 0.0, "y": 0.0})

    def close(self) -> None:
        if not self.owns_process:
            try:
                self.neutralise()
            except Exception:
                pass
            return
        try:
            self.neutralise()
            _exchange(self.port, {"op": "shutdown"})
        except Exception:
            pass
        try:
            self.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.process.kill()


def serve(
    ttl_seconds: float = 0.50,
    *,
    owner_pid: int | None = None,
    owner_token: str | None = None,
) -> None:
    # Acquire the OS boundary before importing/constructing the virtual
    # controller.  A failed second start therefore cannot create a device.
    ownership = acquire_broker_ownership()
    if owner_pid is None:
        owner_pid = os.getppid()
    if owner_token is None:
        owner_token = process_identity_token(owner_pid)
    try:
        try:
            import vgamepad as vg
        except ImportError as exc:
            raise SystemExit('install the "gamepad" extra before starting the gamepad broker') from exc
        gamepad = vg.VX360Gamepad()
        _serve_owned(
            gamepad,
            ttl_seconds=ttl_seconds,
            owner_pid=owner_pid,
            owner_token=owner_token,
        )
    finally:
        ownership.__exit__(None, None, None)


def _serve_owned(
    gamepad: Any,
    *,
    ttl_seconds: float,
    owner_pid: int,
    owner_token: str | None,
) -> None:
    lock = threading.Lock()
    last_heartbeat = time.monotonic()
    active = False
    supervised = SupervisedStickLease()
    sprint_active = False
    pressed_buttons: set[int] = set()

    def set_sprint(enabled: bool) -> None:
        nonlocal sprint_active
        trigger = getattr(gamepad, "right_trigger_float", None)
        if not callable(trigger):
            if enabled:
                raise ValueError("supervised_rt_unavailable")
        else:
            trigger(1.0 if enabled else 0.0)
        sprint_active = enabled

    def release_pressed_buttons() -> None:
        if not pressed_buttons:
            return
        release_button = getattr(gamepad, "release_button", None)
        if not callable(release_button):
            raise GamepadBrokerError("virtual gamepad provider lacks button release support")
        for button in tuple(pressed_buttons):
            release_button(button=button)
        gamepad.update()
        pressed_buttons.clear()

    stopping = threading.Event()

    def neutral() -> None:
        nonlocal active, last_heartbeat
        gamepad.left_joystick_float(0.0, 0.0)
        right_stick = getattr(gamepad, "right_joystick_float", None)
        if callable(right_stick):
            right_stick(0.0, 0.0)
        set_sprint(False)
        release_pressed_buttons()
        gamepad.update()
        active = False
        supervised.cancel()
        last_heartbeat = time.monotonic()

    def watchdog() -> None:
        next_owner_check = 0.0
        while not stopping.wait(0.01):
            with lock:
                now = time.monotonic()
                owner_dead = False
                if now >= next_owner_check:
                    owner_dead = not owner_is_alive(owner_pid, owner_token)
                    next_owner_check = now + 0.05
                if owner_dead:
                    # The stable HTTP/supervisor owner is gone.  Neutralise
                    # before leaving; no actionable broker may outlive it.
                    neutral()
                    stopping.set()
                    return
                if active and (supervised.expired(time.monotonic()) or
                    (supervised.deadline is None and time.monotonic() - last_heartbeat > ttl_seconds)):
                    neutral()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.10)
    port = int(server.getsockname()[1])
    print(json.dumps({
        "ok": True,
        "port": port,
        "ownership_protocol": BROKER_OWNERSHIP_PROTOCOL,
        "owner_pid": owner_pid,
        "owner_token_present": bool(owner_token),
    }), flush=True)
    threading.Thread(target=watchdog, daemon=True).start()

    while not stopping.is_set():
        try:
            connection, _ = server.accept()
        except socket.timeout:
            continue
        with connection:
            connection.settimeout(0.25)
            request = {}
            try:
                line = connection.makefile("rb").readline(4097)
                if len(line) > 4096:
                    raise ValueError("broker_request_too_large")
                request = json.loads(line.decode("utf-8"))
                op = request.get("op")
                with lock:
                    if op == "supervised_set":
                        x, y = float(request["x"]), float(request["y"])
                        if not all(math.isfinite(v) for v in (x, y)) or math.hypot(x, y) > 1.000001:
                            raise ValueError("supervised_stick_invalid")
                        sprint = request.get("sprint", False)
                        if type(sprint) is not bool or (sprint and math.hypot(x, y) < 0.999):
                            raise ValueError("supervised_sprint_requires_boolean_and_full_stick")
                        supervised.accept(stream_id=request.get("stream_id"), sequence=request.get("sequence"),
                            deadline=request.get("deadline"), now=time.monotonic())
                        gamepad.left_joystick_float(x, y)
                        set_sprint(sprint)
                        gamepad.update()
                        active = abs(x) > 1e-6 or abs(y) > 1e-6
                        response = {"ok": True, "active": active, "supervised": True,
                            "deadline": supervised.deadline, "sequence": supervised.sequence,
                            "sprint": sprint_active, "right_trigger": 1.0 if sprint_active else 0.0}
                    elif op == "set":
                        x, y = float(request["x"]), float(request["y"])
                        if supervised.deadline is not None and (abs(x) > 1e-6 or abs(y) > 1e-6):
                            raise ValueError("supervised_hold_requires_explicit_neutral_before_legacy_input")
                        supervised.cancel()
                        gamepad.left_joystick_float(x, y)
                        set_sprint(False)
                        gamepad.update()
                        active = abs(x) > 1e-6 or abs(y) > 1e-6
                        last_heartbeat = time.monotonic()
                        response = {"ok": True, "active": active}
                    elif op == "heartbeat":
                        last_heartbeat = time.monotonic()
                        response = {"ok": True, "active": active}
                    elif op == "pulse_right_stick":
                        x, y = float(request["x"]), float(request["y"])
                        duration = request.get("milliseconds")
                        if not all(math.isfinite(v) for v in (x, y)) or math.hypot(x, y) > 1.000001:
                            raise ValueError("camera_stick_invalid")
                        if type(duration) is not int or not 20 <= duration <= 300:
                            raise ValueError("camera_duration_out_of_range")
                        if active or supervised.deadline is not None:
                            raise ValueError("camera_pulse_requires_neutral_movement")
                        try:
                            gamepad.right_joystick_float(x, y)
                            gamepad.update()
                            time.sleep(duration / 1000.0)
                        finally:
                            gamepad.right_joystick_float(0.0, 0.0)
                            gamepad.update()
                        response = {"ok": True, "released": True, "milliseconds": duration}
                    elif op == "pulse_a":
                        milliseconds = request.get("milliseconds")
                        if (
                            isinstance(milliseconds, bool)
                            or not isinstance(milliseconds, int)
                            or not 20 <= milliseconds <= 1500
                        ):
                            raise ValueError("gamepad_a_duration_must_be_between_20_and_1500_ms")
                        press_button = getattr(gamepad, "press_button", None)
                        release_button = getattr(gamepad, "release_button", None)
                        if not callable(press_button) or not callable(release_button):
                            raise ValueError("virtual_gamepad_face_buttons_unavailable")
                        pressed = False
                        try:
                            press_button(button=XUSB_GAMEPAD_A)
                            pressed = True
                            pressed_buttons.add(XUSB_GAMEPAD_A)
                            gamepad.update()
                            time.sleep(milliseconds / 1000.0)
                        finally:
                            if pressed:
                                release_button(button=XUSB_GAMEPAD_A)
                                gamepad.update()
                                pressed_buttons.discard(XUSB_GAMEPAD_A)
                        response = {
                            "ok": True,
                            "button": "x360_a",
                            "milliseconds": milliseconds,
                            "released": True,
                        }
                    elif op == "pulse_button":
                        name = request.get("button")
                        milliseconds = request.get("milliseconds")
                        if name not in PULSE_BUTTONS:
                            raise ValueError("gamepad_button_not_allow_listed")
                        if (
                            isinstance(milliseconds, bool)
                            or not isinstance(milliseconds, int)
                            or not 20 <= milliseconds <= MAX_PULSE_MS
                        ):
                            raise ValueError("gamepad_pulse_duration_out_of_range")
                        press_button = getattr(gamepad, "press_button", None)
                        release_button = getattr(gamepad, "release_button", None)
                        if not callable(press_button) or not callable(release_button):
                            raise ValueError("virtual_gamepad_face_buttons_unavailable")
                        code = PULSE_BUTTONS[name]
                        pressed = False
                        try:
                            press_button(button=code)
                            pressed = True
                            pressed_buttons.add(code)
                            gamepad.update()
                            time.sleep(milliseconds / 1000.0)
                        finally:
                            if pressed:
                                release_button(button=code)
                                gamepad.update()
                                pressed_buttons.discard(code)
                        response = {
                            "ok": True,
                            "button": name,
                            "milliseconds": milliseconds,
                            "released": True,
                        }
                    elif op == "status":
                        response = {
                            "ok": True,
                            # ``active`` remains the backwards-compatible
                            # device/stick state.  Liveness is explicit.
                            "active": active,
                            "device_active": active,
                            "process_alive": True,
                            "listener_alive": True,
                            "owner_pid": owner_pid,
                            "owner_alive": owner_is_alive(owner_pid, owner_token),
                            "ttl_seconds": ttl_seconds,
                            "supervised_deadline": supervised.deadline,
                            "supervised_sequence": supervised.sequence,
                            "sprint": sprint_active,
                            "right_trigger": 1.0 if sprint_active else 0.0,
                        }
                    elif op == "shutdown":
                        neutral()
                        stopping.set()
                        response = {"ok": True}
                    else:
                        response = {"ok": False, "error": "unknown broker operation"}
            except Exception as exc:
                if isinstance(request, dict) and request.get("op") in {"supervised_set", "pulse_a", "pulse_button", "pulse_right_stick"}:
                    with lock:
                        try:
                            neutral()
                        except Exception:
                            pass
                response = {"ok": False, "error": str(exc)}
            try:
                connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
            except OSError:
                with lock:
                    neutral()
    with lock:
        neutral()
    server.close()


def main() -> None:
    from .process_priority import raise_harness_priority
    raise_harness_priority()
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["serve"])
    parser.add_argument("--owner-pid", type=int, default=None)
    parser.add_argument("--owner-token", default=None)
    args = parser.parse_args()
    if args.mode == "serve":
        try:
            serve(
                float(os.getenv("FF7R_GAMEPAD_BROKER_TTL_SECONDS", "0.50")),
                owner_pid=args.owner_pid,
                owner_token=args.owner_token or None,
            )
        except (GamepadBrokerError, RuntimeOwnershipError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
            raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
