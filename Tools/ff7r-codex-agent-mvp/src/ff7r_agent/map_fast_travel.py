"""Fast travel through the area map: steer the map cursor onto a pin, open the
game's own fast-travel prompt, accept it, and verify the arrival.

Every step is observed through an on-demand map snapshot (``map_query``).
Input is bounded and only sent with ``execute=True``:

1. cursor pulses (WASD / left stick) steered in closed loop until the cursor
   sits on the pin; the response of each axis is measured, not assumed;
2. one ``map_fast_travel`` press, which must make ``FastTravel_C`` (the
   game's PointWarpWidget) visible; otherwise nothing more is sent;
3. one ``confirm`` press, after which the player must reappear within
   ``arrival_radius_m`` of the pin.
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable

from .map_query import MapView, get_map_target, read_map
from .navigation_values import MissionError

PROBE_PULSE_MS = 150
MIN_PULSE_MS = 30
MAX_PULSE_MS = 1200
PULSE_DAMPING = 0.85
MIN_HOVER_TOLERANCE = 4.0
MAX_STALLED_STEPS = 3
PROMPT_WAIT_SECONDS = 3.0
TRAVEL_START_SECONDS = 15.0
TRAVEL_SETTLE_SECONDS = 45.0
TRAVEL_JUMP_CM = 5000.0
PIN_MATCH_TOLERANCE = 3.0


def _pin_in(view: MapView, target: dict[str, Any]) -> dict[str, Any]:
    """Locate the target's pin in a fresh snapshot, by identity then position."""
    pin = view.find_pin(str(target.get("pin_id")))
    if pin is not None:
        return pin
    wx, wy = target["position"]["x"], target["position"]["y"]
    lx, ly = view.projection.to_local(wx, wy)
    near = [p for p in view.pins if math.dist((p["map_local"]["x"], p["map_local"]["y"]), (lx, ly)) <= PIN_MATCH_TOLERANCE]
    if len(near) != 1:
        raise MissionError("map_fast_travel_pin_not_found")
    return near[0]


def _error(view: MapView, pin: dict[str, Any]) -> tuple[float, float]:
    cursor = view.cursor_local()
    if cursor is None:
        raise MissionError("map_cursor_unavailable")
    return pin["map_local"]["x"] - cursor[0], pin["map_local"]["y"] - cursor[1]


def _tolerance(view: MapView, pin: dict[str, Any]) -> float:
    record = next((r for r in view.response.get("icons") or []
                   if isinstance(r, dict) and r.get("runtime_id") == pin["pin_id"]), None)
    size = ((record or {}).get("placement") or {}).get("size") or {}
    try:
        return max(MIN_HOVER_TOLERANCE, 0.35 * min(float(size["x"]), float(size["y"])))
    except (KeyError, TypeError, ValueError):
        return MIN_HOVER_TOLERANCE * 2


class MapFastTravel:
    def __init__(self, service: Any, *, read: Callable[[Any], MapView] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.service = service
        self.read = read or (lambda svc: read_map(svc, heights=False))
        self.clock = clock
        self.sleep = getattr(service, "_sleeper", time.sleep)
        self.inputs: list[dict[str, Any]] = []

    def _pulse(self, x: float, y: float, milliseconds: float) -> None:
        ms = int(round(min(MAX_PULSE_MS, max(MIN_PULSE_MS, milliseconds))))
        self.service._map_cursor_pulse(x, y, ms)
        self.inputs.append({"action": "cursor_pulse", "x": x, "y": y, "milliseconds": ms})

    def _probe(self, pin: dict[str, Any], view: MapView) -> tuple[tuple[float, float], tuple[float, float], MapView]:
        """Measure how far one probe pulse on each axis moves the cursor (px/ms)."""
        columns = []
        for axis in ((1.0, 0.0), (0.0, 1.0)):
            before = _error(view, pin)
            self._pulse(*axis, PROBE_PULSE_MS)
            view = self.read(self.service)
            pin = _pin_in(view, pin | {"position": pin["world"]})
            after = _error(view, pin)
            # The error shrinks by exactly the cursor's motion over the map.
            columns.append(((before[0] - after[0]) / PROBE_PULSE_MS, (before[1] - after[1]) / PROBE_PULSE_MS))
        (ax, ay), (bx, by) = columns
        if abs(ax * by - bx * ay) < 1e-6:
            raise MissionError("map_cursor_not_responding")
        return columns[0], columns[1], view

    def steer(self, pin: dict[str, Any], view: MapView, max_steps: int) -> tuple[dict[str, Any], MapView, list]:
        trace = []
        error = _error(view, pin)
        tolerance = _tolerance(view, pin)
        trace.append({"error_px": math.hypot(*error), "tolerance_px": tolerance})
        if math.hypot(*error) <= tolerance:
            return pin, view, trace
        (ax, ay), (bx, by), view = self._probe(pin, view)
        det = ax * by - bx * ay
        best, stalled = math.inf, 0
        for _ in range(max_steps):
            pin = _pin_in(view, pin | {"position": pin["world"]})
            error = _error(view, pin)
            distance = math.hypot(*error)
            trace.append({"error_px": distance, "tolerance_px": tolerance})
            if distance <= tolerance:
                return pin, view, trace
            stalled = stalled + 1 if distance >= best - 0.5 else 0
            best = min(best, distance)
            if stalled >= MAX_STALLED_STEPS:
                raise MissionError("map_cursor_not_converging")
            # Solve [a b] t = error for the signed hold time on each axis.
            tx = (error[0] * by - bx * error[1]) / det * PULSE_DAMPING
            ty = (ax * error[1] - error[0] * ay) / det * PULSE_DAMPING
            for axis, value in (((1.0, 0.0), tx), ((0.0, 1.0), ty)):
                if abs(value) >= MIN_PULSE_MS / 2:
                    self._pulse(axis[0] * math.copysign(1, value), axis[1] * math.copysign(1, value), abs(value))
            view = self.read(self.service)
        pin = _pin_in(view, pin | {"position": pin["world"]})
        if math.hypot(*_error(view, pin)) <= tolerance:
            return pin, view, trace
        raise MissionError("map_cursor_step_budget_exhausted")

    def _wait_for_prompt(self) -> MapView:
        deadline = self.clock() + PROMPT_WAIT_SECONDS
        while True:
            try:
                view = self.read(self.service)
            except MissionError as exc:
                # The prompt may be modal over a map that no longer reports ready.
                if str(exc) not in {"map_query_navimap_unavailable", "map_not_open"}:
                    raise
                view = None
            if view is not None and view.summary()["fast_travel_prompt_visible"]:
                return view
            if self.clock() >= deadline:
                raise MissionError("fast_travel_prompt_not_shown")
            self.sleep(0.2)

    def _wait_for_arrival(self, start: dict[str, Any], target: dict[str, Any], radius_m: float) -> dict[str, Any]:
        origin = start["player"]["position"]
        deadline = self.clock() + TRAVEL_START_SECONDS
        while True:
            sample = self.service.observe()
            position = (sample.get("player") or {}).get("position")
            moved = position is not None and math.hypot(position["x"] - origin["x"], position["y"] - origin["y"]) >= TRAVEL_JUMP_CM
            if moved or (sample.get("map_id") and sample.get("map_id") != start.get("map_id")):
                break
            if self.clock() >= deadline:
                raise MissionError("fast_travel_not_started")
            self.sleep(0.25)
        settled = self.service.wait_for_settled_gameplay(timeout_seconds=TRAVEL_SETTLE_SECONDS,
                                                         max_telemetry_age_seconds=0.5)
        if settled.get("ok") is not True:
            raise MissionError("fast_travel_gameplay_not_settled")
        sample = self.service.observe()
        position = sample["player"]["position"]
        goal = target["position"]
        distance = math.hypot(position["x"] - goal["x"], position["y"] - goal["y"]) / 100.0
        if distance > radius_m:
            raise MissionError("fast_travel_arrival_outside_radius")
        return {"arrival_position": position, "distance_to_pin_m": distance, "map_id": sample.get("map_id")}

    def run(self, target_id: str, *, execute: bool, max_cursor_steps: int, arrival_radius_m: float,
            require_fast_travel_icon: bool) -> dict[str, Any]:
        target = get_map_target(self.service, target_id)["target"]
        if target.get("source") != "map_pin":
            raise MissionError("map_fast_travel_requires_pin_target")
        if require_fast_travel_icon and not target.get("fast_travel_candidate"):
            raise MissionError("map_pin_not_fast_travel_candidate")
        start = self.service.observe()
        view = self.read(self.service)
        if view.map_id != target["map_id"]:
            raise MissionError("map_target_map_mismatch")
        pin = _pin_in(view, target)
        error = _error(view, pin)
        report: dict[str, Any] = {"ok": False, "target": target, "execute": execute,
                                  "start_cursor_error_px": math.hypot(*error), "inputs": self.inputs}
        if not execute:
            report.update(ok=True, state="PREFLIGHT_PASSED")
            return report
        pin, view, trace = self.steer(pin, view, max_cursor_steps)
        report["cursor_trace"] = trace
        report["hover"] = {"pin_id": pin["pin_id"], "info_window": view.summary()["info_window"]}
        self.service._tap_map_control("map_fast_travel", 500, "map_fast_travel")
        self.inputs.append({"action": "map_fast_travel"})
        prompt = self._wait_for_prompt()
        report["prompt"] = prompt.response.get("fast_travel_prompt")
        self.service.tap("confirm", 500)
        self.inputs.append({"action": "confirm"})
        report.update(self._wait_for_arrival(start, target, arrival_radius_m))
        report.update(ok=True, state="ARRIVED")
        return report


def fast_travel_via_map(service: Any, target_id: str, *, execute: bool = False, max_cursor_steps: int = 12,
                        arrival_radius_m: float = 80.0, require_fast_travel_icon: bool = True,
                        read: Callable[[Any], MapView] | None = None,
                        clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    if type(execute) is not bool or type(require_fast_travel_icon) is not bool:
        raise MissionError("map_fast_travel_flag_invalid")
    if type(max_cursor_steps) is not int or not 1 <= max_cursor_steps <= 30:
        raise MissionError("map_cursor_step_budget_invalid")
    if not isinstance(arrival_radius_m, (int, float)) or not 5 <= arrival_radius_m <= 500:
        raise MissionError("fast_travel_arrival_radius_invalid")
    runner = MapFastTravel(service, read=read, clock=clock)
    try:
        return runner.run(target_id, execute=execute, max_cursor_steps=max_cursor_steps,
                          arrival_radius_m=float(arrival_radius_m), require_fast_travel_icon=require_fast_travel_icon)
    except MissionError as exc:
        return {"ok": False, "state": "STOPPED", "reason": str(exc), "inputs": runner.inputs}
