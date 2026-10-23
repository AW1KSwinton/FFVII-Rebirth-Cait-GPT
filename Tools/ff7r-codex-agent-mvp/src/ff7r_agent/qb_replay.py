"""Offline replay of consumer telemetry; never constructs a live backend."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from typing import Any

from .input import SimulationInput
from .models import GameState
from .qb_recording import MAX_BYTES, SCHEMA_VERSION
from .telemetry import TelemetryError


class ReplayError(TelemetryError):
    pass


class ReplayClock:
    def __init__(self, epoch: str) -> None:
        self.epoch = datetime.fromisoformat(epoch.replace("Z", "+00:00"))
        if self.epoch.tzinfo is None:
            raise ReplayError("replay epoch needs a timezone")
        self.seconds = 0.0

    def monotonic(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return self.epoch + timedelta(seconds=self.seconds)

    def sleep(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ReplayError("invalid replay sleep")
        self.seconds += seconds

    def advance_to(self, seconds: float) -> None:
        self.seconds = max(self.seconds, seconds)


class ReplayState(GameState):
    def age_seconds(self, now: datetime | None = None) -> float:
        return super().age_seconds(now or self._replay_clock.now())


def load_recording(path: Path, *, allow_partial: bool = False) -> list[dict[str, Any]]:
    """Reject silent evidence loss; salvaging a crashed tail is explicit."""
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise ReplayError("recording exceeds replay byte limit")
    events = []
    previous_id = 0
    with path.open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if len(line) > 2 * 1024 * 1024:
                raise ReplayError("recording line exceeds replay limit")
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                if allow_partial and not stream.read(1):
                    break
                raise ReplayError(f"invalid recording JSON on line {number}") from exc
            if not isinstance(event, dict) or event.get("schema_version") != SCHEMA_VERSION:
                raise ReplayError(f"unsupported recording schema on line {number}")
            if len(events) >= 100000:
                raise ReplayError("recording exceeds replay event limit")
            offset = event.get("offset_seconds", 0)
            if not isinstance(offset, (float, int)) or not math.isfinite(offset) or offset < 0:
                raise ReplayError("invalid recording offset")
            event_id = event.get("event_id")
            if event_id is not None:
                if not isinstance(event_id, int) or event_id <= previous_id:
                    raise ReplayError("recording events are out of order")
                previous_id = event_id
            events.append(event)
    if not events or events[0].get("kind") != "header":
        raise ReplayError("recording header missing")
    summary = events[-1] if events[-1].get("kind") == "summary" else None
    if not allow_partial and (summary is None or summary.get("data", {}).get("dropped_events", 0)
                              or summary.get("data", {}).get("error")):
        raise ReplayError("recording is incomplete; inspect gaps or explicitly allow_partial")
    return events


class ReplayTelemetry:
    """Use recorded merged observations, retaining original producer timestamps.

    ``reads`` consumes one recorded sample per observe(), jumping the fake clock
    forward. ``timeline`` holds the latest sample while sleep/input advances time;
    a tiny read tick also bounds loops which poll without sleeping. Sampling can
    omit intermediate transitions: replay is evidence, not a simulated game.
    """

    def __init__(self, events: list[dict], clock: ReplayClock, *, mode: str = "timeline",
                 read_tick: float = 0.01, end_grace: float = 1.0) -> None:
        if mode not in {"reads", "timeline"} or not 0.001 <= read_tick <= 1 or not 0 <= end_grace <= 10:
            raise ReplayError("invalid replay timing")
        self.clock = clock
        self.frames = sorted((event for event in events if event.get("kind") in
                              {"telemetry", "telemetry_error"}), key=lambda e: e["offset_seconds"])
        if not self.frames:
            raise ReplayError("recording contains no consumer telemetry")
        self.mode, self.read_tick, self.end_grace = mode, read_tick, end_grace
        self.index = -1

    def observe(self) -> GameState:
        previous_index = self.index
        if self.mode == "reads":
            self.index += 1
            if self.index >= len(self.frames):
                raise ReplayError("replay telemetry exhausted")
        else:
            self.clock.sleep(self.read_tick)
            if self.index < 0:
                self.index = 0
            while self.index + 1 < len(self.frames) and self.frames[self.index + 1]["offset_seconds"] <= self.clock.seconds:
                self.index += 1
            if self.clock.seconds > self.frames[-1]["offset_seconds"] + self.end_grace:
                raise ReplayError("replay telemetry exhausted")
        frame = self.frames[self.index]
        self.clock.advance_to(frame["offset_seconds"])
        # Preserve the observed age even if the machine's wall clock moved.
        if self.index != previous_index and frame.get("timestamp_utc"):
            wall = datetime.fromisoformat(frame["timestamp_utc"].replace("Z", "+00:00"))
            if wall.tzinfo is None:
                raise ReplayError("recorded frame timestamp needs a timezone")
            self.clock.epoch = wall - timedelta(seconds=frame["offset_seconds"])
        if frame["kind"] == "telemetry_error":
            raise TelemetryError(frame["data"]["error"])
        state = ReplayState.from_dict(frame["data"]["payload"], source="replay")
        object.__setattr__(state, "_replay_clock", self.clock)
        return state

    def request_queen_blood_probe(self, *args, **kwargs) -> bool:
        return True  # No request markers or probe deployment, even on Windows.

    def wake_queen_blood_probe(self, *args, **kwargs) -> bool:
        return True


class ReplayInput(SimulationInput):
    # Run the service's guarded card-game path against fake environment/arms.
    # This flag describes compatibility, never an OS input capability.
    is_real = True

    def __init__(self, clock: ReplayClock) -> None:
        super().__init__()
        self.clock = clock

    def tap(self, button: str, milliseconds: int) -> None:
        super().tap(button, milliseconds)
        self.clock.sleep(milliseconds / 1000)

    def tap_gamepad(self, button: str, milliseconds: int) -> None:
        super().tap_gamepad(button, milliseconds)
        self.clock.sleep(milliseconds / 1000)

    def mouse_click(self, button: str, milliseconds: int) -> None:
        super().mouse_click(button, milliseconds)
        self.clock.sleep(milliseconds / 1000)


class _ReplayEnvironment:
    def game_process_exists(self) -> bool:
        return True

    def game_is_foreground(self) -> bool:
        return True


class _ReplayArm:
    def __init__(self):
        self.stopped = False

    def status(self):
        return not self.stopped, 600.0

    def snapshot(self):
        return not self.stopped, 600.0, self.stopped

    def latch_stop(self):
        self.stopped = True

    def clear(self):
        self.stopped = True

    def reset_stop(self):
        self.stopped = False


def create_replay_service(path: Path, *, output_root: Path, mode: str = "timeline",
                          allow_partial: bool = False):
    """Construct directly: no factory, real input, lifecycle, stream or history."""
    from .service import AgentService

    events = load_recording(path, allow_partial=allow_partial)
    clock = ReplayClock(events[0]["timestamp_utc"])
    telemetry = ReplayTelemetry(events, clock, mode=mode)
    return AgentService(telemetry, ReplayInput(clock), environment=_ReplayEnvironment(),
                        arm_state=_ReplayArm(), camera_arm_state=_ReplayArm(),
                        bench_arm_state=_ReplayArm(), monotonic=clock.monotonic,
                        sleeper=clock.sleep, minimum_action_delay=0,
                        wall_clock=clock.now,
                        benchmark_root=output_root / "benchmarks",
                        human_recording_root=output_root / "human",
                        queen_blood_recording_root=output_root / "recordings")


def replay_operation(service, event: dict[str, Any]) -> Any:
    """Run one chosen recorded QB call. Never replay lifecycle/travel/stream calls."""
    allowed = {"observe_game": "observe", "observe_queens_blood": "observe_queens_blood",
               "mulligan_cards": "mulligan_cards", "select_queens_blood_cards": "select_queens_blood_cards",
               "place_queens_blood_card": "place_queens_blood_card",
               "pass_queens_blood_turn": "pass_queens_blood_turn",
               "finish_queens_blood_match": "finish_queens_blood_match",
               "edit_queens_blood_deck": "edit_queens_blood_deck",
               "open_queens_blood_deck_editor": "open_queens_blood_deck_editor"}
    if not isinstance(service.telemetry, ReplayTelemetry) or not isinstance(service.input, ReplayInput):
        raise ReplayError("replay_operation requires a fully fake replay service")
    if service._public_stream_event_dir is not None or service._queen_blood_profile_root is not None:
        raise ReplayError("replay must not write live match history or profiles")
    if event.get("kind") != "operation_start":
        raise ReplayError("choose an operation_start event")
    data = event["data"]
    operation = data.get("operation")
    if operation not in allowed:
        raise ReplayError("operation is outside offline QB replay")
    service.telemetry.clock.advance_to(event["offset_seconds"])
    return getattr(service, allowed[operation])(**data.get("arguments", {}))
