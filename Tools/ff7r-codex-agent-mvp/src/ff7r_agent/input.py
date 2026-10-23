from __future__ import annotations

import gc
import math
import ctypes
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock, Thread
from typing import Callable, Protocol


ALLOWED_BUTTONS = frozenset({
    "confirm", "cancel", "up", "down", "left", "right", "sub_left", "sub_right",
    "interact", "attack", "dodge", "guard", "menu", "map",
    "map_selection_prev", "map_selection_next", "map_drop_pin", "map_tab_prev",
    "map_tab_next", "map_layer_toggle", "map_era_toggle", "map_view_corresponding",
    "map_fast_travel", "queen_blood_begin_match", "queen_blood_left", "queen_blood_right",
    "queen_blood_play", "queen_blood_cancel", "queen_blood_details", "queen_blood_edit_deck",
    "save_shortcut", "dialogue_left", "dialogue_right", "battle_command_menu",
})

DEFAULT_KEYMAP = {
    "confirm": "enter",
    # The keyboard profile backs out of menus with Escape (or right-click);
    # Backspace does nothing (live: Kalm's shop 2026-09-30, the Load list
    # 2026-10-05). Escape also opens the pause menu from the field, so
    # cancel is for menus and dialogues only.
    "cancel": "esc",
    "up": "up",
    "down": "down",
    "left": "q",
    "right": "e",
    "sub_left": "z",
    "sub_right": "x",
    "interact": "e",
    "attack": "f",
    "dodge": "space",
    "guard": "shift",
    # The current keyboard profile uses Shift for sprint. Keep this as a
    # separate semantic binding so sprint can be changed without altering
    # the guard action.
    "sprint": "shift",
    # Escape opens/closes the in-game pause menu. Keep it separate from the
    # world-map binding: Tab opens the map in this control profile.
    "menu": "esc",
    "map": "tab",
    # Map-only controls. Keep these distinct from sub_left/sub_right so
    # callers can express POI navigation without guessing the UI context.
    "map_selection_prev": "z",
    "map_selection_next": "x",
    "map_drop_pin": "f2",
    "map_tab_prev": "q",
    "map_tab_next": "e",
    "map_layer_toggle": "f3",
    "map_era_toggle": "r",
    "map_view_corresponding": "g",
    # Opens the fast-travel prompt for the pin under the map cursor. Not yet
    # live-verified: match it to the key the map's on-screen prompt shows.
    # map_fast_travel.py refuses to confirm unless FastTravel_C then appears.
    "map_fast_travel": "enter",
    "queen_blood_begin_match": "f3",
    "queen_blood_left": "left",
    "queen_blood_right": "right",
    "queen_blood_play": "r",
    # Escape returns a picked-up card to the hand; Backspace does not.
    "queen_blood_cancel": "esc",
    # Deck screens (keyboard glyphs 2026-09-26): R opens the deck editor
    # (Sort / Filter inside it), F4 toggles the card details popup.
    "queen_blood_details": "f4",
    "queen_blood_edit_deck": "r",
    # R in the field main menu opens the Save list directly (owner,
    # live 2026-10-08), skipping the Save / Load submenu.
    "save_shortcut": "r",
    # The dialogue answer wheel moves with the arrow keys; Q/E (left/right)
    # do nothing there (live, Nibelheim 2026-10-14).
    "dialogue_left": "left",
    "dialogue_right": "right",
    # The field prompt "Grab" (<button=catch>) on the keyboard profile: Ctrl,
    # held while moving to push or pull the object (live 2026-10-20, the Mt.
    # Nibel air purifier).
    "grab": "ctrl",
    # The battle HUD's "Commands Menu" key (keyboard glyph C, live
    # 2026-10-20 on Mt. Nibel).
    "battle_command_menu": "c",
}

# Some game UI actions (notably Tab/map) need a sustained press on slower
# systems. Keep the bound finite while allowing the validated 500 ms press.
# Allow deliberate 1-second recovery and traversal pulses. Short taps remain
# available for fine control, while longer adaptive chunks use their own cap.
MAX_ACTION_MS = 1500


def _wasd_keys(x: float, y: float) -> list[str]:
    keys: list[str] = []
    if y > 0.2:
        keys.append("w")
    elif y < -0.2:
        keys.append("s")
    if x > 0.2:
        keys.append("d")
    elif x < -0.2:
        keys.append("a")
    return keys
# Grab is held a moment before the stick moves, so the game takes the grab first.
GRAB_SETTLE_SECONDS = 1.0
# Holds the game itself times. Queen's Blood passes when the Play/Pass key is
# held until the HUD's PassGauge fills; live, 1.5 s holds were often not
# registered, so that one key alone may be held longer.
# The interaction key may be held this long for a "Hold" prompt (live
# 2026-10-21: the Mt. Nibel wall lever).
BUTTON_MAX_ACTION_MS = {"queen_blood_play": 2500, "interaction": 4000}
# Card-game buttons on the virtual X360 pad (XUSB button names). The game
# reads the virtual pad while another window has focus (live 2026-09-25:
# D-pad Right moved the Queen's Blood hand cursor 0 -> 2 with the Claude app
# in front; posted WM_KEYDOWN messages were ignored), so these presses need
# no focus change. Glyphs on the mulligan screen: Y Play, A Select.
CARD_GAME_GAMEPAD_BUTTONS = {
    "confirm": "XUSB_GAMEPAD_A",
    "queen_blood_cancel": "XUSB_GAMEPAD_B",
    "queen_blood_play": "XUSB_GAMEPAD_Y",
    "up": "XUSB_GAMEPAD_DPAD_UP",
    "down": "XUSB_GAMEPAD_DPAD_DOWN",
    "queen_blood_left": "XUSB_GAMEPAD_DPAD_LEFT",
    "queen_blood_right": "XUSB_GAMEPAD_DPAD_RIGHT",
    # Pad-only buttons of the pre-match deck screens (glyphs 2026-09-25):
    # LB/RB change deck, Menu begins the match (Craft Prebuilt Deck in the
    # editor), X jumps to a card in the editor.
    "queen_blood_deck_previous": "XUSB_GAMEPAD_LEFT_SHOULDER",
    "queen_blood_deck_next": "XUSB_GAMEPAD_RIGHT_SHOULDER",
    "queen_blood_menu": "XUSB_GAMEPAD_START",
    "queen_blood_jump": "XUSB_GAMEPAD_X",
}
# Semantic controls for the opt-in pad-only backend. Unmapped actions fail
# before input; they must never fall back to typing into the user's app.
BACKGROUND_GAMEPAD_BUTTONS = {
    **CARD_GAME_GAMEPAD_BUTTONS,
    "cancel": "XUSB_GAMEPAD_B", "left": "XUSB_GAMEPAD_DPAD_LEFT",
    "right": "XUSB_GAMEPAD_DPAD_RIGHT", "menu": "XUSB_GAMEPAD_START",
    "map": "XUSB_GAMEPAD_BACK", "interact": "XUSB_GAMEPAD_Y",
    "attack": "XUSB_GAMEPAD_X", "dodge": "XUSB_GAMEPAD_B",
    "map_fast_travel": "XUSB_GAMEPAD_A",
    "sub_left": "XUSB_GAMEPAD_LEFT_SHOULDER", "sub_right": "XUSB_GAMEPAD_RIGHT_SHOULDER",
    "map_selection_prev": "XUSB_GAMEPAD_LEFT_SHOULDER",
    "map_selection_next": "XUSB_GAMEPAD_RIGHT_SHOULDER",
}
MAX_LOOK_PIXELS = 400
# XInput controllers commonly apply a right-stick centre dead zone around
# 0.265 of full scale. Keep small, deliberate camera corrections above that
# threshold; this is a minimum effective pulse, not a calibrated pixel scale.
BACKGROUND_CAMERA_MIN_MAGNITUDE = 0.30
# Long holds are still bounded and polled for gameplay/telemetry changes. The
# coordinator may use the larger budget only after a long-range walkability
# check authorizes it; raw callers remain responsible for choosing a sensible
# target distance.
MAX_ADAPTIVE_HOLD_MS = 20000


OBS_BRIDGE_STATE_POINTER = Path("Codex") / "OBS Bridge" / "active-state-dir.txt"


def _published_bridge_state_dir() -> Path | None:
    local = os.getenv("LOCALAPPDATA")
    if not local:
        return None
    try:
        value = (Path(local) / OBS_BRIDGE_STATE_POINTER).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    path = Path(value) if value else None
    return path if path is not None and path.is_absolute() and path.is_dir() else None


def _obs_bridge_state_dir() -> Path | None:
    configured_state = os.getenv("FF7R_OBS_STATE_DIR")
    if configured_state:
        return Path(configured_state)
    # The running monitor publishes the directory it reads markers from; the
    # agent's own checkout may not hold the bridge's .env (live: 4,392
    # markers written to a worktree's state directory no monitor read).
    published = _published_bridge_state_dir()
    if published is not None:
        return published
    configured_root = os.getenv("FF7R_OBS_BRIDGE_ROOT")
    candidates = [Path(configured_root)] if configured_root else []
    here = Path(__file__).resolve()
    if len(here.parents) > 4:
        candidates.extend([here.parents[4], here.parents[3]])
    root = next((candidate for candidate in candidates if candidate and (candidate / "obs.ps1").is_file()), None)
    if root is None:
        return None
    # Honour the bridge's own BRIDGE_STATE_DIR override so both sides use one marker directory.
    try:
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "BRIDGE_STATE_DIR" and value.strip():
                configured = Path(value.strip().strip('"'))
                return configured if configured.is_absolute() else root / configured
    except OSError:
        pass
    return root / "state"


_obs_state_cache: tuple[tuple[str | None, ...], float, Path | None] | None = None


def _cached_obs_state_dir() -> Path | None:
    """Refresh the monitor's published path periodically, on the writer thread."""
    global _obs_state_cache
    identity = tuple(os.getenv(key) for key in ("LOCALAPPDATA", "FF7R_OBS_STATE_DIR", "FF7R_OBS_BRIDGE_ROOT"))
    now = time.monotonic()
    if _obs_state_cache is None or _obs_state_cache[0] != identity or now >= _obs_state_cache[1]:
        _obs_state_cache = (identity, now + 5.0, _obs_bridge_state_dir())
    return _obs_state_cache[2]


def _declared_recording_driver() -> dict[str, str] | None:
    """The active lease's declared model and effort; None when undeclared or expired."""
    from .recording_driver import validate_recording_driver
    from .safety import ArmError, SessionLease
    try:
        configured_runtime = os.getenv("FF7R_AUTONOMY_RUNTIME_DIR")
        status = SessionLease(runtime_dir=Path(configured_runtime) if configured_runtime else None).status()
        declared = status.get("recording_driver") if status and status.get("active") else None
        if not isinstance(declared, dict):
            return None
        return validate_recording_driver(declared.get("modelId"), declared.get("effort"))
    except (ArmError, ValueError, TypeError, OSError):
        return None


def _write_obs_marker(occurred_at: datetime) -> None:
    state_dir = _cached_obs_state_dir()
    if state_dir is None:
        return
    events_dir = state_dir / "input-events"
    events_dir.mkdir(parents=True, exist_ok=True)
    timestamp = occurred_at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{occurred_at.microsecond // 1000:03d}Z"
    event_id = str(uuid.uuid4())
    target = events_dir / (re.sub(r"[^0-9A-Za-z]", "", timestamp) + "-" + event_id + ".json")
    temp = target.with_name(target.name + f".{os.getpid()}.tmp")
    # Read on this writer thread, never the control thread: the declaration
    # can trail the input by up to the one-second marker throttle.
    driver = _declared_recording_driver()
    mission = None
    mission_error = None
    mission_status = "invalid_context"
    try:
        active = json.loads((state_dir / "mission-active.json").read_text(encoding="utf-8"))
        context = active.get("mission") if active.get("version") == 1 else None
        if (isinstance(context, dict) and isinstance(context.get("id"), str)
                and re.fullmatch(r"[0-9a-f-]{36}", context["id"])
                and isinstance(context.get("name"), str) and context["name"].strip()
                and datetime.fromisoformat(context["startedAt"].replace("Z", "+00:00")) <= occurred_at):
            mission = {key: context[key] for key in ("id", "name", "startedAt", "phase") if key in context}
            mission_status = "attached"
    except FileNotFoundError:
        mission_status = "missing_file"
        pass  # No mission is active until the streamer's mission command creates the file.
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        mission_status = "read_error"
        mission_error = type(exc).__name__
        logging.getLogger(__name__).warning("OBS mission context unreadable: %s", state_dir, exc_info=True)
    temp.write_text(json.dumps({"id": event_id, "occurredAt": timestamp, "driver": driver,
                               "mission": mission, "mission_status": mission_status,
                               **({"mission_error": mission_error} if mission_error else {})}) + "\n", encoding="utf-8")
    os.replace(temp, target)


class _ObsInputMarkerDispatcher:
    """A bounded, best-effort writer; control never waits for marker I/O.

    At most one marker a second. The last input of a burst inside that second
    is remembered and written once the second has passed, so a clip's trail is
    measured from the real last input rather than up to a second before it.
    """

    def __init__(self, writer=_write_obs_marker, clock=time.monotonic):
        self._writer = writer
        self._clock = clock
        self._lock = Lock()
        self._last_marker: float | None = None
        self._trailing: datetime | None = None
        self._queue: Queue[datetime | None] = Queue(maxsize=1)
        # Start during module/runtime setup, before any movement deadline.
        self._thread = Thread(target=self._run, name="ff7r-obs-markers", daemon=True)
        self._thread.start()

    def mark(self) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            now = self._clock()
            occurred_at = datetime.now(timezone.utc)
            if self._last_marker is not None and now - self._last_marker < 1.0:
                self._trailing = occurred_at
                return
            try:
                self._queue.put_nowait(occurred_at)
            except Full:
                self._trailing = occurred_at
                return  # A blocked writer must never build an unbounded backlog.
            self._last_marker = now
            self._trailing = None
        finally:
            self._lock.release()

    def _take_trailing(self) -> datetime | None:
        with self._lock:
            if self._trailing is None or (
                    self._last_marker is not None and self._clock() - self._last_marker < 1.0):
                return None
            occurred_at, self._trailing = self._trailing, None
            self._last_marker = self._clock()
            return occurred_at

    def _write(self, occurred_at: datetime) -> None:
        try:
            self._writer(occurred_at)
        except Exception:
            pass  # Recording failure cannot break gameplay or kill the writer.

    def _run(self) -> None:
        while True:
            try:
                occurred_at = self._queue.get(timeout=0.25)
            except Empty:
                trailing = self._take_trailing()
                if trailing is not None:
                    self._write(trailing)
                continue
            try:
                if occurred_at is None:
                    return
                self._write(occurred_at)
            finally:
                self._queue.task_done()


_obs_marker_dispatcher = _ObsInputMarkerDispatcher()


def mark_gameplay_input() -> None:
    """Enqueue activity at most once a second without blocking game input.

    The daemon writes into the monitor's published state directory, retaining
    the original input time. Both services must run outside Codex's AppData
    redirection (see the root gameplay runbook).
    """
    value = os.getenv("FF7R_OBS_INPUT_CLIPS", "1").strip().lower()
    if value in {"0", "false", "no", "off"} or "PYTEST_CURRENT_TEST" in os.environ:
        return
    _obs_marker_dispatcher.mark()

class InputError(RuntimeError):
    pass


class InputBackend(Protocol):
    def tap(self, button: str, milliseconds: int) -> None: ...
    def mouse_click(self, button: str, milliseconds: int) -> None: ...
    def mouse_click_in_game_window(
        self, button: str, milliseconds: int, hwnd: int
    ) -> None: ...
    def move(self, x: float, y: float, milliseconds: int) -> None: ...
    def look(self, dx: int, dy: int, milliseconds: int) -> None: ...
    def calibrate_camera_horizontal(self, delta: int) -> dict[str, object]: ...
    def release_all(self) -> None: ...
    def use_interaction(self, milliseconds: int) -> dict[str, object]: ...
    def use_bench(self, milliseconds: int) -> dict[str, object]: ...
    def adaptive_forward(
        self, stop_requested: Callable[[], str | None], maximum_ms: int = 1000,
        poll_seconds: float = 0.025,
    ) -> dict[str, object]: ...
    def adaptive_move(
        self, x: float, y: float, stop_requested: Callable[[], str | None],
        maximum_ms: int = 1000, poll_seconds: float = 0.025,
        sprint: bool = False,
    ) -> dict[str, object]: ...


class VirtualLeftStick(Protocol):
    """Minimal provider surface required by the hybrid Windows backend."""

    def left_joystick_float(self, x_value_float: float, y_value_float: float) -> None: ...
    def update(self) -> None: ...


def validate_duration(milliseconds: int, button: str | None = None) -> int:
    value = int(milliseconds)
    limit = BUTTON_MAX_ACTION_MS.get(button, MAX_ACTION_MS) if button else MAX_ACTION_MS
    if not 20 <= value <= limit:
        raise InputError(f"duration must be between 20 and {limit} ms")
    return value


def normalise_axes(x: float, y: float) -> tuple[float, float]:
    x, y = float(x), float(y)
    if not math.isfinite(x) or not math.isfinite(y):
        raise InputError("movement axes must be finite numbers")
    magnitude = math.hypot(x, y)
    if magnitude > 1.0:
        x, y = x / magnitude, y / magnitude
    return x, y


def validate_look_delta(dx: float, dy: float) -> tuple[int, int]:
    values = (float(dx), float(dy))
    if not all(math.isfinite(value) for value in values):
        raise InputError("camera deltas must be finite numbers")
    if any(abs(value) > MAX_LOOK_PIXELS for value in values):
        raise InputError(f"camera deltas must be between -{MAX_LOOK_PIXELS} and {MAX_LOOK_PIXELS} pixels")
    result = tuple(int(round(value)) for value in values)
    if result == (0, 0):
        raise InputError("camera movement requires a non-zero delta")
    return result


@dataclass
class RecordedAction:
    action: str
    parameters: dict[str, object]


class SimulationInput:
    is_real = False
    def __init__(self) -> None:
        self.actions: list[RecordedAction] = []

    def _record(self, action: RecordedAction) -> None:
        self.actions.append(action)
        recorder = getattr(self, "qb_recorder", None)
        if recorder is not None:
            recorder.capture("input", {"event": action.action, **action.parameters})

    def tap(self, button: str, milliseconds: int) -> None:
        if button not in ALLOWED_BUTTONS:
            raise InputError(f"unsupported button: {button}")
        self._record(RecordedAction("tap", {"button": button, "milliseconds": validate_duration(milliseconds, button)}))

    def tap_gamepad(self, button: str, milliseconds: int) -> None:
        if button not in CARD_GAME_GAMEPAD_BUTTONS:
            raise InputError(f"button has no gamepad binding: {button}")
        self._record(RecordedAction("tap_gamepad", {
            "button": button, "milliseconds": validate_duration(milliseconds, button),
        }))

    def mouse_click(self, button: str, milliseconds: int) -> None:
        if button not in {"left", "right"}:
            raise InputError("unsupported mouse button")
        self._record(RecordedAction("mouse_click", {
            "button": button,
            "milliseconds": validate_duration(milliseconds),
        }))

    def mouse_click_in_game_window(
        self, button: str, milliseconds: int, hwnd: int
    ) -> None:
        if button not in {"left", "right"}:
            raise InputError("unsupported mouse button")
        self._record(RecordedAction("mouse_click", {
            "button": button,
            "milliseconds": validate_duration(milliseconds),
            "window": int(hwnd),
            "position_policy": "game_client_bottom_right_margin",
        }))

    def move(self, x: float, y: float, milliseconds: int) -> None:
        x, y = normalise_axes(x, y)
        self._record(RecordedAction("move", {"x": x, "y": y, "milliseconds": validate_duration(milliseconds)}))

    def grab_move(self, segments: list[tuple[float, float, int]], *, stick: bool = False) -> None:
        self._record(RecordedAction("grab_move", {"segments": [list(segment) for segment in segments], "stick": stick}))

    def look(self, dx: int, dy: int, milliseconds: int) -> None:
        dx, dy = validate_look_delta(dx, dy)
        self._record(RecordedAction("look", {"dx": dx, "dy": dy, "milliseconds": validate_duration(milliseconds)}))

    def calibrate_camera_horizontal(self, delta: int) -> dict[str, object]:
        raise InputError("camera calibration requires the Windows input backend")

    def release_all(self) -> None:
        self._record(RecordedAction("release_all", {}))

    def use_bench(self, milliseconds: int) -> dict[str, object]:
        raise InputError("bench interaction requires the Windows input backend")

    def use_interaction(self, milliseconds: int) -> dict[str, object]:
        raise InputError("interaction requires the Windows input backend")

    def adaptive_forward(self, stop_requested, maximum_ms=1000,
                         poll_seconds=0.025) -> dict[str, object]:
        raise InputError("adaptive movement requires the Windows input backend")

    def adaptive_move(self, x, y, stop_requested, maximum_ms=1000,
                      poll_seconds=0.025, sprint=False) -> dict[str, object]:
        raise InputError("adaptive movement requires the Windows input backend")


# Each smooth camera turn is sent as relative mouse events this far apart.
SMOOTH_CAMERA_STEP_MS = 16
# One slice of a steered camera turn (camera_move_linear).
CAMERA_SLICE_MAX_DELTA = 1000
CAMERA_SLICE_MAX_MILLISECONDS = 250
# A camera sweep re-centres the cursor before it gets this close to an edge.
CURSOR_EDGE_MARGIN_PX = 40


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class WindowsKeyboardInput:
    """Bounded keyboard backend for movement and target-gated attack probes."""

    is_real = True

    def __init__(self, keymap: dict[str, str] | None = None,
                 event_log: Path | None = None,
                 monotonic=time.monotonic) -> None:
        try:
            import pydirectinput
        except ImportError as exc:
            raise InputError('install the "windows" extra to use Windows input') from exc
        self._pdi = pydirectinput
        self._pdi.PAUSE = 0
        self.keymap = dict(DEFAULT_KEYMAP if keymap is None else keymap)
        self._held: set[str] = set()
        self._lock = Lock()
        self._camera_lock = Lock()
        self._event_log = event_log
        self._monotonic = monotonic
        self._last_key_up_monotonic: float | None = None
        self._key_down_started: dict[str, float] = {}
        self._event_records: list[dict[str, object]] = []

    def tap(self, button: str, milliseconds: int) -> None:
        if button not in {
            "attack", "dodge", "confirm", "cancel", "up", "down", "left", "right",
            "sub_left", "sub_right", "menu", "map", "map_selection_prev", "map_selection_next",
            "map_drop_pin", "map_tab_prev", "map_tab_next", "map_layer_toggle",
            "map_era_toggle", "map_view_corresponding", "map_fast_travel", "queen_blood_begin_match",
            "queen_blood_left", "queen_blood_right", "queen_blood_play",
            "queen_blood_cancel", "queen_blood_details", "queen_blood_edit_deck", "save_shortcut",
            "dialogue_left", "dialogue_right", "battle_command_menu",
        }:
            raise InputError("button is not enabled for real input")
        key = self.keymap.get(button)
        if not key:
            raise InputError(f"no key mapping for button: {button}")
        duration = validate_duration(milliseconds, button) / 1000
        mark_gameplay_input()
        with self._lock:
            try:
                self._key_down(key)
                time.sleep(duration)
            finally:
                self._release_keys((key,))

    def mouse_click(self, button: str, milliseconds: int) -> None:
        if button not in {"left", "right"}:
            raise InputError("mouse click is limited to the left and right buttons")
        duration = validate_duration(milliseconds) / 1000
        mark_gameplay_input()
        with self._lock:
            down_ok = False
            up_ok = False
            original_send = getattr(self._pdi, "SendInput", None)
            if original_send is None:
                raise InputError("pydirectinput does not expose SendInput")

            def invoke_mouse_event(method_name: str) -> bool:
                """Send one mouse event and verify the underlying Win32 call.

                pydirectinput's mouseDown/mouseUp intentionally return None even
                when SendInput succeeds.  Treating that return value as a bool
                therefore rejected every real right-click in the live game.
                Wrap SendInput for this bounded event so a zero inserted-event
                result still fails closed, while preserving the bool-returning
                fake backend used by the unit tests.
                """
                send_calls: list[tuple[int, int]] = []

                def recording_send(expected, pointer, size):
                    returned = int(original_send(expected, pointer, size))
                    send_calls.append((int(expected), returned))
                    return returned

                setattr(self._pdi, "SendInput", recording_send)
                try:
                    result = getattr(self._pdi, method_name)(button=button)
                finally:
                    setattr(self._pdi, "SendInput", original_send)

                if result is False:
                    return False
                if send_calls:
                    expected, returned = send_calls[-1]
                    return returned >= expected
                # The test double returns an explicit bool; real pydirectinput
                # returns None after its SendInput call has been verified above.
                return result is True or result is None

            try:
                down_ok = invoke_mouse_event("mouseDown")
                time.sleep(duration)
            finally:
                up_ok = invoke_mouse_event("mouseUp")
        if not down_ok:
            raise InputError(f"Windows SendInput failed to press mouse {button}")
        if not up_ok:
            raise InputError(f"Windows SendInput failed to release mouse {button}")
        self._write_event_record({
            "event": "mouse_click",
            "button": button,
            "milliseconds": int(milliseconds),
            "pressed_timestamp": time.time() - duration,
            "released_timestamp": time.time(),
        })

    @staticmethod
    def _game_client_margin_position(hwnd: int) -> tuple[int, int]:
        """Return a safe click point just inside the target window's client edge."""
        if os.name != "nt":
            raise InputError("game-window mouse clicks require Windows")

        class Rect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        handle = ctypes.c_void_p(int(hwnd))
        user32 = ctypes.windll.user32
        user32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(Rect)]
        user32.GetClientRect.restype = ctypes.c_bool
        user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(Point)]
        user32.ClientToScreen.restype = ctypes.c_bool
        rect = Rect()
        if not user32.GetClientRect(handle, ctypes.byref(rect)):
            raise InputError("could not read the game window client rectangle")
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width < 32 or height < 32:
            raise InputError("game window client rectangle is too small for a safe click")
        point = Point(max(1, width - 12), max(1, height - 12))
        if not user32.ClientToScreen(handle, ctypes.byref(point)):
            raise InputError("could not map the game client click point to screen coordinates")
        return int(point.x), int(point.y)

    @staticmethod
    def _set_desktop_pointer(x: int, y: int) -> bool:
        if os.name != "nt":
            return False
        user32 = ctypes.windll.user32
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = ctypes.c_bool
        return bool(user32.SetCursorPos(int(x), int(y)))

    def mouse_click_in_game_window(
        self, button: str, milliseconds: int, hwnd: int
    ) -> None:
        """Click in a blank client margin so the event reaches the game.

        The point is deliberately away from the card row.  Queen's Blood card
        focus is established by the game's keyboard cursor before this method
        runs; the desktop pointer only delivers the click to the game window.
        """
        if button not in {"left", "right"}:
            raise InputError("mouse click is limited to the left and right buttons")
        current_hwnd, _ = self._foreground_identity()
        if current_hwnd != int(hwnd):
            raise InputError("target game window is not foreground for mouse click")
        x, y = self._game_client_margin_position(hwnd)
        if not self._set_desktop_pointer(x, y):
            raise InputError("could not position the desktop pointer inside the game window")
        current_hwnd, _ = self._foreground_identity()
        if current_hwnd != int(hwnd):
            raise InputError("target game window lost foreground before mouse click")
        self._write_event_record({
            "event": "mouse_position",
            "purpose": "deliver_click_to_game_window",
            "screen_x": x,
            "screen_y": y,
            "target_hwnd": int(hwnd),
            "position_policy": "game_client_bottom_right_margin",
        })
        self.mouse_click(button, milliseconds)

    def _foreground_identity(self) -> tuple[int | None, int | None]:
        try:
            hwnd = int(ctypes.windll.user32.GetForegroundWindow())
            pid = ctypes.c_ulong(0)
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return hwnd, int(pid.value)
        except Exception:
            return None, None

    def _write_event_record(self, record: dict[str, object]) -> None:
        self._event_records.append(record)
        recorder = getattr(self, "qb_recorder", None)
        if recorder is not None:
            recorder.capture("input", record)
        if self._event_log is None:
            return
        try:
            self._event_log.parent.mkdir(parents=True, exist_ok=True)
            with self._event_log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            pass

    def _direct_send_key_event(
        self,
        key: str,
        direction: str,
        send_calls: list[dict[str, int]],
    ) -> bool:
        """Send one scan-code event without PyDirectInput's cursor fail-safe.

        PyDirectInput runs its global mouse-corner fail-safe before every
        keyboard event.  That is useful for an interactive script, but it can
        reject an otherwise fully validated game action when the cursor is
        parked at a screen corner (including after a window capture).  The
        service has already checked the process, foreground window, telemetry,
        lease, and emergency-stop state before this method is reached.  Keep
        the fallback local to that specific exception and use the same
        ``SendInput`` scan-code path PyDirectInput uses, preserving the event
        audit trail without globally disabling its fail-safe.
        """
        scan_code = getattr(self._pdi, "KEYBOARD_MAPPING", {}).get(key)
        original_send = getattr(self._pdi, "SendInput", None)
        input_type = getattr(self._pdi, "Input", None)
        input_union = getattr(self._pdi, "Input_I", None)
        keybd_input = getattr(self._pdi, "KeyBdInput", None)
        if scan_code is None or original_send is None or input_type is None:
            return False
        if input_union is None or keybd_input is None:
            return False

        scan_flag = int(getattr(self._pdi, "KEYEVENTF_SCANCODE", 0x0008))
        keyup_flag = int(getattr(self._pdi, "KEYEVENTF_KEYUP", 0x0002))
        extended_flag = int(getattr(self._pdi, "KEYEVENTF_EXTENDEDKEY", 0x0001))
        flags = scan_flag | (keyup_flag if direction == "up" else 0)
        if key in {"up", "down", "left", "right"}:
            flags |= extended_flag
        extra = ctypes.c_ulong(0)
        union = input_union()
        union.ki = keybd_input(0, int(scan_code), flags, 0, ctypes.pointer(extra))
        event = input_type(ctypes.c_ulong(1), union)
        try:
            ctypes.windll.kernel32.SetLastError(0)
        except Exception:
            pass
        returned = int(original_send(1, ctypes.pointer(event), ctypes.sizeof(event)))
        try:
            error = int(ctypes.windll.kernel32.GetLastError())
        except Exception:
            error = 0
        send_calls.append({"expected": 1, "returned": returned, "get_last_error": error})
        return returned == 1

    def _invoke_key_event(self, key: str, direction: str) -> bool:
        before = sorted(self._held)
        started = self._monotonic()
        hwnd, pid = self._foreground_identity()
        scan_code = getattr(self._pdi, "KEYBOARD_MAPPING", {}).get(key)
        scan_flag = int(getattr(self._pdi, "KEYEVENTF_SCANCODE", 0x0008))
        keyup_flag = int(getattr(self._pdi, "KEYEVENTF_KEYUP", 0x0002))
        flags = scan_flag | (keyup_flag if direction == "up" else 0)
        send_calls: list[dict[str, int]] = []
        failsafe_fallback = False
        original_send = getattr(self._pdi, "SendInput", None)
        if original_send is not None:
            def recording_send(expected, pointer, size):
                try:
                    ctypes.windll.kernel32.SetLastError(0)
                except Exception:
                    pass
                returned = int(original_send(expected, pointer, size))
                try:
                    error = int(ctypes.windll.kernel32.GetLastError())
                except Exception:
                    error = 0
                send_calls.append({"expected": int(expected), "returned": returned,
                                   "get_last_error": error})
                return returned
            self._pdi.SendInput = recording_send
        try:
            try:
                ok = ((self._pdi.keyDown(key) if direction == "down"
                       else self._pdi.keyUp(key)) is True)
            except Exception as exc:
                message = str(exc).lower()
                if "fail-safe" not in message and "failsafe" not in message:
                    raise
                failsafe_fallback = True
                if original_send is not None:
                    # The normal path temporarily wraps SendInput for audit
                    # logging.  Restore the real callable before the direct
                    # fallback so one OS event is recorded exactly once.
                    self._pdi.SendInput = original_send
                ok = self._direct_send_key_event(key, direction, send_calls)
        finally:
            if original_send is not None:
                self._pdi.SendInput = original_send
        finished = self._monotonic()
        if direction == "down" and ok:
            self._held.add(key)
            self._key_down_started[key] = started
        elif direction == "up":
            self._held.discard(key)
        actual_duration = None
        if direction == "up":
            down_started = self._key_down_started.pop(key, None)
            actual_duration = finished - down_started if down_started is not None else None
            self._last_key_up_monotonic = finished
        record = {
            "event": f"key_{direction}", "monotonic_timestamp": started,
            "requested_key": key, "scan_code": scan_code, "flags": flags,
            "keyeventf_scancode": bool(flags & scan_flag),
            "send_input_calls": send_calls,
            "send_input_expected_event_count": sum(call["expected"] for call in send_calls),
            "send_input_returned_event_count": sum(call["returned"] for call in send_calls),
            "get_last_error": send_calls[-1]["get_last_error"] if send_calls else None,
            "pydirectinput_result": ok,
            "delivery_path": (
                "direct_sendinput_failsafe_fallback"
                if failsafe_fallback else "pydirectinput"
            ),
            "held_keys_before": before, "held_keys_after": sorted(self._held),
            "foreground_hwnd": hwnd, "foreground_pid": pid,
            "actual_key_down_duration_seconds": actual_duration,
            "time_since_previous_key_up_seconds": (
                started - self._last_key_up_monotonic
                if direction == "down" and self._last_key_up_monotonic is not None else None
            ),
        }
        self._write_event_record(record)
        return ok

    def _key_down(self, key: str) -> None:
        if not self._invoke_key_event(key, "down"):
            raise InputError(f"Windows SendInput failed to press key: {key}")

    def _release_keys(self, keys: tuple[str, ...] | list[str]) -> None:
        failures: list[str] = []
        for key in keys:
            if key not in self._held:
                continue
            try:
                if not self._invoke_key_event(key, "up"):
                    failures.append(key)
            finally:
                self._held.discard(key)
        if failures:
            raise InputError(
                "Windows SendInput failed to release key(s): " + ", ".join(failures)
            )

    def move(self, x: float, y: float, milliseconds: int) -> None:
        x, y = normalise_axes(x, y)
        duration = validate_duration(milliseconds) / 1000
        mark_gameplay_input()
        keys: list[str] = []
        if y > 0.2:
            keys.append("w")
        elif y < -0.2:
            keys.append("s")
        if x > 0.2:
            keys.append("d")
        elif x < -0.2:
            keys.append("a")
        with self._lock:
            try:
                for key in keys:
                    self._key_down(key)
                time.sleep(duration)
            finally:
                self._release_keys(keys)

    def grab_move(self, segments: list[tuple[float, float, int]]) -> None:
        """Drag a field object: hold Grab for the whole pull and move on WASD,
        one segment (camera-relative x, y, milliseconds) after another. Grab
        is held, not toggled (owner, 2026-10-20: the Mt. Nibel air purifier);
        moving with the virtual pad's stick while it was held did nothing."""
        mark_gameplay_input()
        grab = self.keymap.get("grab") or "ctrl"
        with self._lock:
            held = [grab]
            try:
                self._key_down(grab)
                time.sleep(GRAB_SETTLE_SECONDS)
                for x, y, milliseconds in segments:
                    keys = _wasd_keys(*normalise_axes(x, y))
                    for key in keys:
                        self._key_down(key)
                        held.append(key)
                    time.sleep(validate_duration(milliseconds) / 1000)
                    self._release_keys(keys)
                    held = [grab]
            finally:
                self._release_keys(held)

    def look(self, dx: int, dy: int, milliseconds: int) -> None:
        dx, dy = validate_look_delta(dx, dy)
        duration = validate_duration(milliseconds) / 1000
        mark_gameplay_input()
        with self._lock:
            hwnd, pid = self._foreground_identity()
            started = self._monotonic()
            try:
                self._pdi.moveRel(
                    dx, dy, duration=duration, _pause=False, relative=True
                )
                ok = True
            except Exception:
                ok = False
                raise
            finally:
                self._write_event_record({
                    "event": "mouse_move_relative",
                    "monotonic_timestamp": started,
                    "dx": dx,
                    "dy": dy,
                    "milliseconds": int(milliseconds),
                    "pydirectinput_result": ok,
                    "foreground_hwnd": hwnd,
                    "foreground_pid": pid,
                })

    def calibrate_camera_horizontal(self, delta: int) -> dict[str, object]:
        """Send one verified, zero-duration relative mouse event.

        This is deliberately separate from ``look``.  The latter is useful for
        exploratory camera surveys; this path is the calibrated event used by
        the closed-loop bench aligner and records the underlying SendInput
        result so a failed delivery cannot be mistaken for camera motion.
        """
        delta = int(delta)
        if delta == 0 or abs(delta) > 500:
            raise InputError("camera delta must be non-zero and between -500 and 500")
        mark_gameplay_input()
        with self._lock:
            started = self._monotonic()
            hwnd, pid = self._foreground_identity()
            calls: list[dict[str, int]] = []
            original_send = getattr(self._pdi, "SendInput", None)
            if original_send is None:
                raise InputError("pydirectinput does not expose SendInput")

            def recording_send(expected, pointer, size):
                try:
                    ctypes.windll.kernel32.SetLastError(0)
                except Exception:
                    pass
                returned = int(original_send(expected, pointer, size))
                try:
                    error = int(ctypes.windll.kernel32.GetLastError())
                except Exception:
                    error = 0
                calls.append({
                    "expected": int(expected),
                    "returned": returned,
                    "get_last_error": error,
                })
                return returned

            self._pdi.SendInput = recording_send
            try:
                self._pdi.moveRel(delta, 0, duration=0, relative=True, _pause=False)
            finally:
                self._pdi.SendInput = original_send
            if not calls or any(call["returned"] != call["expected"] for call in calls):
                raise InputError("Windows SendInput failed to move the camera horizontally")
            record = {
                "event": "camera_horizontal_relative",
                "monotonic_timestamp": started,
                "horizontal_delta": delta,
                "vertical_delta": 0,
                "send_input_calls": calls,
                "foreground_hwnd": hwnd,
                "foreground_pid": pid,
                "held_keys_before": sorted(self._held),
                "held_keys_after": sorted(self._held),
            }
            self._write_event_record(record)
            return record

    def smooth_camera_horizontal(self, delta: int, milliseconds: int,
                                 should_stop: Callable[[], bool] | None = None) -> dict[str, object]:
        """Turn the camera by ``delta`` pixels eased over ``milliseconds``.

        A human turns the camera in a smooth sweep while walking; one
        relative mouse event snaps it. The same total is sent as small
        relative events on an ease-in-out curve, so the game sees a steady
        drag. It stops early if focus leaves the window it started on or
        ``should_stop`` says so, reporting how much was actually sent.
        """
        return self._smooth_camera(delta, milliseconds, should_stop, 500, 1000)

    def camera_move_linear(self, delta: int, milliseconds: int) -> dict[str, object]:
        """One slice of a steered camera turn: ``delta`` at constant speed.

        Back-to-back slices make one continuous motion; the caller checks
        the camera between them (a sweep's turn per pixel is not constant).
        """
        return self._smooth_camera(delta, milliseconds, None, CAMERA_SLICE_MAX_DELTA,
                                   CAMERA_SLICE_MAX_MILLISECONDS, linear=True)

    def _cursor_x_and_bounds(self) -> tuple[int, int, int] | None:
        """The cursor's x and the left/right of the area it is confined to."""
        try:
            point, rect = _POINT(), _RECT()
            if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                return None
            if not ctypes.windll.user32.GetClipCursor(ctypes.byref(rect)):
                return None
            return int(point.x), int(rect.left), int(rect.right)
        except Exception:
            return None

    def _warp_cursor_x(self, x: int) -> None:
        try:
            point = _POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                ctypes.windll.user32.SetCursorPos(int(x), int(point.y))
        except Exception:
            pass

    def _keep_cursor_off_edge(self, increment: int) -> None:
        """Re-centre the cursor before a camera move would push it into an edge.

        The camera turns with the cursor's motion, which stops at the screen
        edge: live 2026-10-05 a 123 and a 95 degree sweep both stopped near
        68 degrees (about 1450 px) with the cursor pinned there. Warping the
        cursor itself does not turn the camera (verified live), so this keeps
        a long sweep one continuous motion.
        """
        position = self._cursor_x_and_bounds()
        if position is None:
            return
        x, left, right = position
        if not left + CURSOR_EDGE_MARGIN_PX <= x + increment <= right - 1 - CURSOR_EDGE_MARGIN_PX:
            self._warp_cursor_x((left + right) // 2)

    def _smooth_camera(self, delta: int, milliseconds: int, should_stop: Callable[[], bool] | None,
                       max_delta: int, max_milliseconds: int, linear: bool = False) -> dict[str, object]:
        delta = int(delta)
        if abs(delta) > max_delta:
            raise InputError(f"camera delta must be between -{max_delta} and {max_delta}")
        duration = max(0, min(max_milliseconds, int(milliseconds)))
        steps = max(1, min(abs(delta) or 1, duration // SMOOTH_CAMERA_STEP_MS or 1))
        mark_gameplay_input()
        started = self._monotonic()
        hwnd, pid = self._foreground_identity()
        sent, stopped = 0, None
        with self._camera_lock:
            for step in range(1, steps + 1):
                if should_stop is not None and should_stop():
                    stopped = "requested"
                    break
                if self._foreground_identity() != (hwnd, pid):
                    stopped = "foreground_changed"
                    break
                share = step / steps if linear else (1.0 - math.cos(math.pi * step / steps)) / 2.0
                target = int(round(delta * share))
                increment = target - sent
                if increment:
                    self._keep_cursor_off_edge(increment)
                    self._pdi.moveRel(increment, 0, duration=0, relative=True, _pause=False)
                    sent = target
                if step < steps:
                    time.sleep(duration / 1000.0 / steps)
        record = {
            "event": "camera_horizontal_smooth",
            "monotonic_timestamp": started,
            "requested_delta": delta,
            "sent_delta": sent,
            "milliseconds": duration,
            "steps": steps,
            "stopped": stopped,
            "foreground_hwnd": hwnd,
            "foreground_pid": pid,
        }
        self._write_event_record(record)
        return record

    def release_all(self) -> None:
        with self._lock:
            self._release_keys(tuple(self._held))

    def adaptive_move(
        self, x: float, y: float, stop_requested: Callable[[], str | None],
        maximum_ms: int = 1000, poll_seconds: float = 0.025,
        sprint: bool = False,
    ) -> dict[str, object]:
        """Hold movement, optionally with the configured sprint modifier.

        Sprint is deliberately a modifier on the same bounded movement hold,
        rather than a separate unbounded key loop. This guarantees that the
        stop callback and the release path cover both the movement keys and
        the modifier.
        """
        x, y = normalise_axes(x, y)
        if abs(x) < 0.2 and abs(y) < 0.2:
            raise InputError("adaptive movement requires a non-zero direction")
        maximum_ms = int(maximum_ms)
        poll_seconds = float(poll_seconds)
        if not 100 <= maximum_ms <= MAX_ADAPTIVE_HOLD_MS:
            raise InputError(
                f"adaptive hold must be between 100 and {MAX_ADAPTIVE_HOLD_MS} ms"
            )
        if not 0.01 <= poll_seconds <= 0.10:
            raise InputError("adaptive poll interval must be between 10 and 100 ms")
        mark_gameplay_input()
        keys = []
        if y > 0.2:
            keys.append("w")
        elif y < -0.2:
            keys.append("s")
        if x > 0.2:
            keys.append("d")
        elif x < -0.2:
            keys.append("a")
        if not keys:
            raise InputError("adaptive movement vector is below the input deadzone")
        sprint_key = (
            self.keymap.get("sprint") or self.keymap.get("guard") or "shift"
        ) if sprint else None
        held_keys = ([sprint_key] if sprint_key and sprint_key not in keys else []) + keys
        started = self._monotonic()
        reason = "maximum_hold_elapsed"
        with self._lock:
            try:
                for key in held_keys:
                    self._key_down(key)
                deadline = started + maximum_ms / 1000.0
                while self._monotonic() < deadline:
                    requested = stop_requested()
                    if requested:
                        reason = str(requested)
                        break
                    time.sleep(poll_seconds)
            finally:
                # Release movement before the modifier so the game cannot
                # receive a trailing sprint key-up as a movement transition.
                self._release_keys(list(reversed(held_keys)))
        return {
            "x": x,
            "y": y,
            "keys": held_keys,
            "key": "+".join(held_keys),
            "sprint": bool(sprint),
            "maximum_ms": maximum_ms,
            "actual_hold_ms": round((self._monotonic() - started) * 1000.0, 3),
            "release_reason": reason,
        }

    def adaptive_forward(
        self, stop_requested: Callable[[], str | None], maximum_ms: int = 1000,
        poll_seconds: float = 0.025,
    ) -> dict[str, object]:
        return self.adaptive_move(0.0, 1.0, stop_requested, maximum_ms, poll_seconds)

    def diagnostic_key_up(self, key: str) -> bool:
        """Emit one explicit key-up even when internal held tracking is clear."""
        with self._lock:
            return self._invoke_key_event(key, "up")

    def event_record_count(self) -> int:
        return len(self._event_records)

    def event_records_since(self, index: int) -> list[dict[str, object]]:
        return list(self._event_records[index:])

    def _use_fixed_key(self, key: str, milliseconds: int, purpose: str) -> dict[str, object]:
        duration = validate_duration(milliseconds, "interaction") / 1000
        mark_gameplay_input()
        pressed_at = time.time()
        released_at = None
        down_ok = False
        up_ok = False
        label = f"{purpose} interaction key: {key}" if purpose else f"interaction key: {key}"
        with self._lock:
            try:
                # Use the same audited SendInput path as ordinary taps.  The
                # fixed interaction binding remains F, but bypassing
                # _invoke_key_event made this path invisible to the event
                # audit and skipped its local fail-safe fallback.
                down_ok = self._invoke_key_event(key, "down")
                if not down_ok:
                    raise InputError(f"Windows SendInput failed to press {label}")
                time.sleep(duration)
            finally:
                released_at = time.time()
                up_ok = self._invoke_key_event(key, "up")
                self._held.discard(key)
        if not up_ok:
            raise InputError(f"Windows SendInput failed to release {label}")
        return {
            "button": "keyboard_f",
            "milliseconds": int(milliseconds),
            "key_down_ok": down_ok,
            "key_up_ok": up_ok,
            "pressed_timestamp": pressed_at,
            "released_timestamp": released_at,
        }

    def use_interaction(self, milliseconds: int) -> dict[str, object]:
        return self._use_fixed_key("f", milliseconds, "")

    def use_bench(self, milliseconds: int) -> dict[str, object]:
        return self._use_fixed_key("f", milliseconds, "bench")


class WindowsHybridGamepadInput:
    """Use an analogue left stick for movement and keyboard/mouse elsewhere.

    The virtual controller is intentionally injected or loaded lazily. This
    keeps the normal keyboard backend independent of a virtual-device driver,
    and makes the actuator easy to test without touching Windows input.
    """

    is_real = True
    backend_name = "windows_gamepad"

    def __init__(
        self,
        *,
        keyboard: WindowsKeyboardInput | None = None,
        gamepad: VirtualLeftStick | None = None,
        gamepad_factory: Callable[[], VirtualLeftStick] | None = None,
        keymap: dict[str, str] | None = None,
        event_log: Path | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        reconnect_delay_seconds: float = 0.75,
        background_only: bool = False,
    ) -> None:
        self.supports_background_input = bool(background_only)
        self.keyboard = keyboard or WindowsKeyboardInput(
            keymap=keymap,
            event_log=event_log,
            monotonic=monotonic,
        )
        self._gamepad_factory = gamepad_factory or self._create_gamepad
        self._gamepad = gamepad if gamepad is not None else self._gamepad_factory()
        self._validate_gamepad(self._gamepad)
        self._event_log = event_log
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._reconnect_delay_seconds = float(reconnect_delay_seconds)
        if not 0.0 <= self._reconnect_delay_seconds <= 5.0:
            raise InputError("gamepad reconnect delay must be between 0 and 5 seconds")
        self._lock = Lock()
        self._event_records: list[dict[str, object]] = []
        self._last_sprint: bool | None = False
        self._last_stick: tuple[float, float] | None = None
        self._stick_state = "unknown"
        self._neutralisation_error: str | None = None

    @staticmethod
    def _create_gamepad() -> VirtualLeftStick:
        try:
            import vgamepad as vg
        except ImportError as exc:
            raise InputError(
                'install the "gamepad" extra and its explicit virtual-device driver prerequisite to use Windows gamepad input'
            ) from exc
        try:
            return vg.VX360Gamepad()
        except Exception as exc:
            raise InputError(
                "virtual X360 gamepad initialization failed; verify the explicit driver installation"
            ) from exc

    @staticmethod
    def _validate_gamepad(gamepad: VirtualLeftStick | None) -> None:
        if not callable(getattr(gamepad, "left_joystick_float", None)):
            raise InputError("virtual gamepad provider lacks left_joystick_float")
        if not callable(getattr(gamepad, "update", None)):
            raise InputError("virtual gamepad provider lacks update")

    def reconnect_gamepad(self) -> dict[str, object]:
        """Replace the virtual device after the game has entered gameplay.

        Rebirth can bind its input mode to the controller device that existed
        during startup. Releasing that device and creating one after the save
        world is settled gives the movement actuator a fresh device identity
        without changing the keyboard/mouse path used for startup menus.
        """
        with self._lock:
            current = self._gamepad
            self._neutral_stick()
            self._gamepad = None
            self._last_stick = None
            self._stick_state = "unknown"
            try:
                del current
                gc.collect()
                if self._reconnect_delay_seconds:
                    self._sleeper(self._reconnect_delay_seconds)
                replacement = self._gamepad_factory()
                self._validate_gamepad(replacement)
                self._gamepad = replacement
                self._last_stick = (0.0, 0.0)
                self._stick_state = "neutral"
                self._neutralisation_error = None
                return {
                    "ok": True,
                    "event": "gamepad_reconnected_after_gameplay_ready",
                    "provider": type(replacement).__name__,
                    "delay_seconds": self._reconnect_delay_seconds,
                }
            except Exception as exc:
                self._neutralisation_error = str(exc)
                raise InputError("virtual gamepad reconnect failed") from exc

    def _write_event_record(self, record: dict[str, object]) -> None:
        self._event_records.append(record)
        recorder = getattr(self, "qb_recorder", None)
        if recorder is not None:
            recorder.capture("input", record)
        if self._event_log is None:
            return
        try:
            self._event_log.parent.mkdir(parents=True, exist_ok=True)
            with self._event_log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            pass

    def _set_stick(self, x: float, y: float) -> None:
        x, y = normalise_axes(x, y)
        gamepad = self._gamepad
        if gamepad is None:
            raise InputError("virtual gamepad is not connected")
        gamepad.left_joystick_float(x, y)
        gamepad.update()
        self._last_stick = (x, y)
        self._stick_state = "active"
        self._neutralisation_error = None
        self._write_event_record({
            "event": "gamepad_left_stick",
            "monotonic_timestamp": self._monotonic(),
            "x": x,
            "y": y,
        })

    def _neutral_stick(self) -> None:
        errors: list[Exception] = []
        direct_write_ok = False
        direct_update_ok = False
        gamepad = self._gamepad
        if gamepad is None:
            self._stick_state = "unknown"
            self._neutralisation_error = "virtual gamepad is not connected"
            raise InputError("virtual gamepad neutralisation failed")
        try:
            gamepad.left_joystick_float(0.0, 0.0)
            direct_write_ok = True
        except Exception as exc:
            errors.append(exc)
        try:
            gamepad.update()
            direct_update_ok = True
        except Exception as exc:
            errors.append(exc)

        fallback_attempted = False
        fallback_succeeded = False
        if not (direct_write_ok and direct_update_ok):
            reset = getattr(gamepad, "reset", None)
            if callable(reset):
                fallback_attempted = True
                try:
                    reset()
                    gamepad.update()
                    fallback_succeeded = True
                except Exception as exc:
                    errors.append(exc)

        success = (direct_write_ok and direct_update_ok) or fallback_succeeded
        if success:
            self._last_sprint = False
            self._last_stick = (0.0, 0.0)
            self._stick_state = "neutral"
            self._neutralisation_error = None
        else:
            self._last_sprint = None
            # Do not claim that the device is centred when either the write or
            # the report submission failed. The actual virtual-device state is
            # then unknown and must be visible to diagnostics.
            self._stick_state = "unknown"
            self._neutralisation_error = "; ".join(str(error) for error in errors)

        self._write_event_record({
            "event": "gamepad_left_stick_neutral",
            "monotonic_timestamp": self._monotonic(),
            "x": 0.0,
            "y": 0.0,
            "success": success,
            "fallback_attempted": fallback_attempted,
            "fallback_succeeded": fallback_succeeded,
            "errors": [str(error) for error in errors],
        })
        if not success:
            raise InputError("virtual gamepad neutralisation failed") from errors[0]

    def backend_status(self) -> dict[str, object]:
        return {
            "backend": self.backend_name,
            "provider": type(self._gamepad).__name__,
            "last_stick": (
                None
                if self._last_stick is None
                else {"x": self._last_stick[0], "y": self._last_stick[1]}
            ),
            "stick_state": self._stick_state,
            "last_sprint": self._last_sprint,
            "sprint_control": "supervised_held_rt",
            "neutralisation_error": self._neutralisation_error,
            "movement_control": "virtual_x360_left_stick",
            "camera_and_buttons_control": (
                "virtual_x360_right_stick_and_button_pulses"
                if self.supports_background_input
                else "keyboard_mouse_plus_virtual_x360_a_fallback"
            ),
            "background_only": self.supports_background_input,
        }

    # Non-movement actions deliberately remain on the existing, audited
    # keyboard/mouse implementation.
    def tap(self, button: str, milliseconds: int) -> None:
        if self.supports_background_input:
            self.tap_gamepad(button, milliseconds)
            return
        self.keyboard.tap(button, milliseconds)

    def mouse_click(self, button: str, milliseconds: int) -> None:
        if self.supports_background_input:
            raise InputError("mouse clicks are unavailable in background mode")
        self.keyboard.mouse_click(button, milliseconds)

    def mouse_click_in_game_window(
        self, button: str, milliseconds: int, hwnd: int
    ) -> None:
        if self.supports_background_input:
            raise InputError("mouse clicks are unavailable in background mode")
        self.keyboard.mouse_click_in_game_window(button, milliseconds, hwnd)

    def press_gamepad_a(self, milliseconds: int) -> dict[str, object]:
        """Pulse the X360 A/confirm button on the existing virtual device."""
        duration = validate_duration(milliseconds)
        mark_gameplay_input()
        with self._lock:
            gamepad = self._gamepad
            if gamepad is None:
                raise InputError("virtual gamepad is not connected")
            remote_press = getattr(gamepad, "press_a", None)
            if callable(remote_press):
                result = dict(remote_press(duration))
                if result.get("released") is not True:
                    raise InputError("gamepad A pulse did not confirm neutral release")
            else:
                try:
                    import vgamepad as vg
                except ImportError as exc:
                    raise InputError('install the "gamepad" extra to send a virtual controller button') from exc
                press = getattr(gamepad, "press_button", None)
                release = getattr(gamepad, "release_button", None)
                if not callable(press) or not callable(release):
                    raise InputError("virtual gamepad provider lacks face-button support")
                button_a = vg.XUSB_BUTTON.XUSB_GAMEPAD_A
                pressed = False
                try:
                    press(button=button_a)
                    pressed = True
                    gamepad.update()
                    time.sleep(duration / 1000.0)
                finally:
                    if pressed:
                        release(button=button_a)
                        gamepad.update()
                result = {
                    "ok": True,
                    "button": "x360_a",
                    "milliseconds": duration,
                }
            self._write_event_record({
                "event": "gamepad_button_a",
                "monotonic_timestamp": self._monotonic(),
                "milliseconds": duration,
                "result": result,
            })
            return result

    def tap_gamepad(self, button: str, milliseconds: int) -> dict[str, object]:
        """Press one card-game button on the virtual pad; focus is not needed."""
        bindings = BACKGROUND_GAMEPAD_BUTTONS if self.supports_background_input else CARD_GAME_GAMEPAD_BUTTONS
        xusb_name = bindings.get(button)
        if xusb_name is None:
            raise InputError(f"button has no gamepad binding: {button}")
        duration = validate_duration(milliseconds, button)
        mark_gameplay_input()
        with self._lock:
            gamepad = self._gamepad
            if gamepad is None:
                raise InputError("virtual gamepad is not connected")
            remote_pulse = getattr(gamepad, "pulse_button", None)
            if callable(remote_pulse):
                # The device lives in the stable gamepad broker process.
                try:
                    result = remote_pulse(xusb_name, duration)
                except Exception as exc:
                    raise InputError(f"gamepad broker pulse failed: {exc}") from exc
                if not isinstance(result, dict) or result.get("released") is not True:
                    raise InputError("gamepad button pulse did not confirm neutral release")
            else:
                press = getattr(gamepad, "press_button", None)
                release = getattr(gamepad, "release_button", None)
                if not callable(press) or not callable(release):
                    raise InputError("virtual gamepad provider lacks button support")
                try:
                    import vgamepad as vg
                    xusb = getattr(vg.XUSB_BUTTON, xusb_name)
                except ImportError as exc:
                    raise InputError('install the "gamepad" extra to send virtual controller buttons') from exc
                pressed = False
                try:
                    press(button=xusb)
                    pressed = True
                    gamepad.update()
                    self._sleeper(duration / 1000.0)
                finally:
                    if pressed:
                        release(button=xusb)
                        gamepad.update()
            result = {"ok": True, "button": button, "gamepad_button": xusb_name, "milliseconds": duration}
            self._write_event_record({
                "event": "gamepad_button",
                "monotonic_timestamp": self._monotonic(),
                **result,
            })
            return result

    def look(self, dx: int, dy: int, milliseconds: int) -> None:
        if self.supports_background_input:
            dx, dy = validate_look_delta(dx, dy)
            duration = validate_duration(milliseconds)
            if duration > 300:
                raise InputError("background camera pulses are bounded to 300 ms")
            pulse = getattr(self._gamepad, "pulse_right_stick", None)
            if not callable(pulse):
                raise InputError("background camera requires the stable gamepad broker")
            x, y = normalise_axes(dx / MAX_LOOK_PIXELS, -dy / MAX_LOOK_PIXELS)
            magnitude = math.hypot(x, y)
            if 0.0 < magnitude < BACKGROUND_CAMERA_MIN_MAGNITUDE:
                scale = BACKGROUND_CAMERA_MIN_MAGNITUDE / magnitude
                x, y = x * scale, y * scale
            with self._lock:
                try:
                    result = pulse(x, y, duration)
                except Exception as exc:
                    raise InputError(f"background camera pulse failed: {exc}") from exc
            if not isinstance(result, dict) or result.get("released") is not True:
                raise InputError("background camera pulse did not confirm neutral release")
            self._write_event_record({"event": "gamepad_camera", "x": x, "y": y,
                "milliseconds": duration, "monotonic_timestamp": self._monotonic()})
            return
        self.keyboard.look(dx, dy, milliseconds)

    def calibrate_camera_horizontal(self, delta: int) -> dict[str, object]:
        if self.supports_background_input:
            self.look(delta, 0, 100)
            return {"ok": True, "backend": "gamepad_right_stick", "delta": delta}
        return self.keyboard.calibrate_camera_horizontal(delta)

    def smooth_camera_horizontal(self, delta: int, milliseconds: int,
                                 should_stop: Callable[[], bool] | None = None) -> dict[str, object]:
        if self.supports_background_input:
            raise InputError("smooth camera turns use the foreground mouse path")
        # The mouse path has its own lock, so the turn runs alongside a held
        # left stick on the virtual pad.
        return self.keyboard.smooth_camera_horizontal(delta, milliseconds, should_stop)

    def camera_move_linear(self, delta: int, milliseconds: int) -> dict[str, object]:
        if self.supports_background_input:
            raise InputError("smooth camera turns use the foreground mouse path")
        return self.keyboard.camera_move_linear(delta, milliseconds)

    def use_interaction(self, milliseconds: int) -> dict[str, object]:
        if self.supports_background_input:
            return self.tap_gamepad("interact", milliseconds)
        return self.keyboard.use_interaction(milliseconds)

    def use_bench(self, milliseconds: int) -> dict[str, object]:
        if self.supports_background_input:
            return self.tap_gamepad("interact", milliseconds)
        return self.keyboard.use_bench(milliseconds)

    def move(self, x: float, y: float, milliseconds: int) -> None:
        x, y = normalise_axes(x, y)
        duration = validate_duration(milliseconds) / 1000
        mark_gameplay_input()
        with self._lock:
            try:
                self._set_stick(x, y)
                self._sleeper(duration)
            finally:
                self._neutral_stick()

    def grab_move(self, segments: list[tuple[float, float, int]], *, stick: bool = False) -> None:
        """Drag a field object: the keyboard holds Grab; the move is on WASD,
        or on the virtual stick with ``stick``."""
        if self.supports_background_input:
            raise InputError("grab needs the keyboard Grab key, unavailable in background mode")
        if not stick:
            with self._lock:
                self.keyboard.grab_move(segments)
            return
        mark_gameplay_input()
        grab = self.keyboard.keymap.get("grab") or "ctrl"
        with self._lock, self.keyboard._lock:
            try:
                self.keyboard._key_down(grab)
                self._sleeper(GRAB_SETTLE_SECONDS)
                for x, y, milliseconds in segments:
                    self._set_stick(*normalise_axes(x, y))
                    self._sleeper(validate_duration(milliseconds) / 1000)
            finally:
                try:
                    self._neutral_stick()
                finally:
                    self.keyboard._release_keys([grab])

    def supervised_move(self, x: float, y: float, *, deadline: float,
                        stream_id: str, sequence: int, sprint: bool = False) -> dict[str, object]:
        """Set a renewable hold; only the broker may enforce its expiry."""
        x, y = normalise_axes(x, y)
        mark_gameplay_input()
        with self._lock:
            callback = getattr(self._gamepad, "set_supervised", None)
            if not callable(callback):
                raise InputError("supervised movement requires the existing stable broker")
            try:
                result = callback(x, y, deadline=deadline, stream_id=stream_id, sequence=sequence, sprint=sprint)
                self._last_sprint = sprint
                self._last_stick = (x, y)
                self._stick_state = "active" if abs(x) + abs(y) > 0 else "neutral"
                self._neutralisation_error = None
                self._write_event_record({"event": "gamepad_left_stick",
                    "monotonic_timestamp": self._monotonic(), "x": x, "y": y,
                    "supervised": True, "deadline": deadline, "sprint": sprint,
                    "right_trigger": 1.0 if sprint else 0.0,
                    "stream_id": stream_id, "sequence": sequence})
                return dict(result)
            except Exception:
                self._neutral_stick()
                raise

    def adaptive_move(
        self,
        x: float,
        y: float,
        stop_requested: Callable[[], str | None],
        maximum_ms: int = 1000,
        poll_seconds: float = 0.025,
        sprint: bool = False,
    ) -> dict[str, object]:
        x, y = normalise_axes(x, y)
        if abs(x) < 0.2 and abs(y) < 0.2:
            raise InputError("adaptive movement requires a non-zero direction")
        maximum_ms = int(maximum_ms)
        poll_seconds = float(poll_seconds)
        if not 100 <= maximum_ms <= MAX_ADAPTIVE_HOLD_MS:
            raise InputError(
                f"adaptive hold must be between 100 and {MAX_ADAPTIVE_HOLD_MS} ms"
            )
        if not 0.01 <= poll_seconds <= 0.10:
            raise InputError("adaptive poll interval must be between 10 and 100 ms")
        mark_gameplay_input()

        sprint_key = (
            self.keyboard.keymap.get("sprint")
            or self.keyboard.keymap.get("guard")
            or "shift"
        ) if sprint else None
        if self.supports_background_input and sprint:
            raise InputError("background sprint requires supervised continuous movement")
        sprint_held = False
        started = self._monotonic()
        reason = "maximum_hold_elapsed"
        with self._lock:
            keyboard_lock = self.keyboard._lock if sprint else None
            if keyboard_lock is not None:
                keyboard_lock.acquire()
            try:
                if sprint_key is not None:
                    self.keyboard._key_down(sprint_key)
                    sprint_held = True
                self._set_stick(x, y)
                deadline = started + maximum_ms / 1000.0
                while self._monotonic() < deadline:
                    requested = stop_requested()
                    if requested:
                        reason = str(requested)
                        break
                    self._sleeper(poll_seconds)
            finally:
                try:
                    self._neutral_stick()
                finally:
                    if sprint_held and sprint_key is not None:
                        self.keyboard._release_keys([sprint_key])
                    if keyboard_lock is not None:
                        keyboard_lock.release()
        return {
            "x": x,
            "y": y,
            "keys": ["left_stick"] + ([sprint_key] if sprint_key else []),
            "key": "left_stick" + (f"+{sprint_key}" if sprint_key else ""),
            "sprint": bool(sprint),
            "gamepad": True,
            "maximum_ms": maximum_ms,
            "actual_hold_ms": round((self._monotonic() - started) * 1000.0, 3),
            "release_reason": reason,
        }

    def adaptive_forward(
        self,
        stop_requested: Callable[[], str | None],
        maximum_ms: int = 1000,
        poll_seconds: float = 0.025,
    ) -> dict[str, object]:
        return self.adaptive_move(0.0, 1.0, stop_requested, maximum_ms, poll_seconds)

    def release_all(self) -> None:
        errors: list[Exception] = []
        with self._lock:
            try:
                self._neutral_stick()
            except Exception as exc:
                errors.append(exc)
            try:
                if not self.supports_background_input:
                    self.keyboard.release_all()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise InputError("hybrid input release failed") from errors[0]

    def diagnostic_key_up(self, key: str) -> bool:
        if self.supports_background_input:
            raise InputError("keyboard diagnostics are unavailable in background mode")
        return self.keyboard.diagnostic_key_up(key)

    def event_record_count(self) -> int:
        return self.keyboard.event_record_count() + len(self._event_records)

    def event_records_since(self, index: int) -> list[dict[str, object]]:
        records = self.keyboard.event_records_since(0) + list(self._event_records)
        records.sort(key=lambda record: float(record.get("monotonic_timestamp", 0.0)))
        return records[index:]
