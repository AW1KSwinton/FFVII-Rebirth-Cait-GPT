"""Bounded local navigation episodes.

This module deliberately has no MCP or FastMCP dependency.  It owns the
observe/decide/act/validate loop for one short episode and receives the
runtime-specific operations as callables.  The current service hosts it
synchronously; a future persistent runtime can host the same executor
without changing the episode logic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import time
from typing import Any, Callable, Protocol


TERMINAL_OUTCOMES = frozenset({
    "goal_reached",
    "time_budget_exhausted",
    "distance_budget_exhausted",
    "maximum_cycles_reached",
    "blocked",
    "route_required",
    "visual_reasoning_required",
    "no_progress",
    "combat_started",
    "menu_opened",
    "gameplay_state_changed",
    "telemetry_stale",
    "telemetry_not_advancing",
    "foreground_lost",
    "game_process_lost",
    "control_session_lost",
    "emergency_stop",
    "backend_error",
})

VISUAL_TERMINAL_OUTCOMES = frozenset({
    "blocked",
    "visual_reasoning_required",
    "route_required",
})


class EpisodeDriver(Protocol):
    """Runtime operations required by :class:`LocalNavigationExecutor`."""

    def observe(self) -> Any: ...

    def safety_reason(self, state: Any, map_id: str | None) -> str | None: ...

    def execute_segment(
        self,
        state: Any,
        cycle_index: int,
        maximum_ms: int,
        maximum_distance_m: float,
    ) -> dict[str, Any]: ...

    def neutralise(self) -> None: ...

    def capture_terminal(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class LocalNavigationLimits:
    """Validated hard bounds for a single local episode."""

    maximum_seconds: float = 8.0
    maximum_distance_m: float = 12.0
    arrival_radius_m: float = 1.5
    maximum_cycles: int = 8
    maximum_segment_ms: int = 2000
    maximum_segment_distance_m: float = 3.0

    HARD_MAXIMUM_SECONDS = 30.0
    HARD_MAXIMUM_DISTANCE_M = 40.0
    HARD_MAXIMUM_CYCLES = 32
    HARD_MAXIMUM_SEGMENT_MS = 3000
    HARD_MAXIMUM_SEGMENT_DISTANCE_M = 3.0

    def validate(self) -> "LocalNavigationLimits":
        values = (
            self.maximum_seconds,
            self.maximum_distance_m,
            self.arrival_radius_m,
            self.maximum_segment_distance_m,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("local navigation limits must be finite")
        if not 0.1 <= self.maximum_seconds <= self.HARD_MAXIMUM_SECONDS:
            raise ValueError(
                f"maximum_seconds must be between 0.1 and {self.HARD_MAXIMUM_SECONDS}"
            )
        if not 0.1 <= self.maximum_distance_m <= self.HARD_MAXIMUM_DISTANCE_M:
            raise ValueError(
                f"maximum_distance_m must be between 0.1 and {self.HARD_MAXIMUM_DISTANCE_M}"
            )
        if not 0.1 <= self.arrival_radius_m <= self.maximum_distance_m:
            raise ValueError("arrival_radius_m must be positive and within maximum_distance_m")
        if not 1 <= int(self.maximum_cycles) <= self.HARD_MAXIMUM_CYCLES:
            raise ValueError(
                f"maximum_cycles must be between 1 and {self.HARD_MAXIMUM_CYCLES}"
            )
        if not 100 <= int(self.maximum_segment_ms) <= self.HARD_MAXIMUM_SEGMENT_MS:
            raise ValueError(
                f"maximum_segment_ms must be between 100 and {self.HARD_MAXIMUM_SEGMENT_MS}"
            )
        if not 0.1 <= self.maximum_segment_distance_m <= min(
            self.HARD_MAXIMUM_SEGMENT_DISTANCE_M,
            self.maximum_distance_m,
        ):
            raise ValueError(
                "maximum_segment_distance_m must be positive, bounded to 3 m, "
                "and within maximum_distance_m"
            )
        return self


@dataclass
class LocalNavigationEpisodeResult:
    """Compact final result returned to the MCP boundary."""

    ok: bool = False
    action: str = "run_local_navigation_episode"
    terminal_outcome: str = "backend_error"
    terminal_reason: str = "episode did not start"
    requested_maximum_seconds: float = 0.0
    requested_maximum_distance_m: float = 0.0
    arrival_radius_m: float = 0.0
    maximum_cycles: int = 0
    total_elapsed_ms: float = 0.0
    useful_cycles_per_invocation: int = 0
    cycles_completed: int = 0
    movement_segments: int = 0
    start_sequence: int | None = None
    end_sequence: int | None = None
    start_position: dict[str, float] | None = None
    end_position: dict[str, float] | None = None
    horizontal_displacement_m: float = 0.0
    cumulative_distance_m: float = 0.0
    target_distance_before_m: float | None = None
    target_distance_after_m: float | None = None
    target_progress_m: float = 0.0
    map_id: str | None = None
    coordinator_decision: str | None = None
    coordinator_reason: str | None = None
    screenshot: dict[str, Any] | None = None
    neutralised: bool = False
    cycles: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _position(state: Any) -> Any | None:
    player = getattr(state, "player", None)
    if player is not None:
        return getattr(player, "position", None)
    if isinstance(state, dict):
        player = state.get("player")
        if isinstance(player, dict):
            return player.get("position")
    return None


def _position_dict(value: Any | None) -> dict[str, float] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        try:
            return {key: float(value[key]) for key in ("x", "y", "z")}
        except (KeyError, TypeError, ValueError):
            return None
    try:
        return {key: float(getattr(value, key)) for key in ("x", "y", "z")}
    except (AttributeError, TypeError, ValueError):
        return None


def _horizontal_distance_m(left: Any | None, right: Any | None) -> float | None:
    left_dict = _position_dict(left)
    right_dict = _position_dict(right)
    if left_dict is None or right_dict is None:
        return None
    return math.hypot(
        left_dict["x"] - right_dict["x"],
        left_dict["y"] - right_dict["y"],
    ) / 100.0


def _state_value(state: Any, name: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(name, default)
    return getattr(state, name, default)


def _driver_decision(result: dict[str, Any]) -> str:
    return str(result.get("decision") or result.get("outcome") or "")


def _driver_rejected(result: dict[str, Any]) -> bool:
    """Return whether the coordinator rejected this segment.

    A movement payload is not an acceptance signal: the coordinator may have
    moved before discovering that the segment could not be verified.  The
    explicit coordinator status fields therefore take precedence over any
    displacement or movement payload.
    """
    return result.get("ok") is False or result.get("replan_required") is True


def _outcome_from_driver_result(result: dict[str, Any]) -> str | None:
    explicit = result.get("terminal_outcome")
    if isinstance(explicit, str) and explicit in TERMINAL_OUTCOMES:
        return explicit
    if not _driver_rejected(result):
        return None
    decision = _driver_decision(result)
    lowered = decision.lower()
    if "route_required" in lowered:
        return "route_required"
    if "visual" in lowered and ("required" in lowered or "review" in lowered):
        return "visual_reasoning_required"
    if "blocked" in lowered or "obstacle" in lowered or "uncertain" in lowered:
        return "blocked"
    if "control_session" in lowered or "session_required" in lowered:
        return "control_session_lost"
    if "combat" in lowered:
        return "combat_started"
    if "menu" in lowered or "pause" in lowered:
        return "menu_opened"
    if "not_ready" in lowered or "telemetry" in lowered:
        return "telemetry_stale"
    # Any unrecognised rejection remains terminal.  It is safer to report a
    # blocked segment than to treat partial movement as a verified cycle.
    return "blocked"


def _driver_rejection_details(result: dict[str, Any]) -> tuple[str | None, str | None]:
    decision = _driver_decision(result) or None
    reason_value = result.get("reason")
    reason = str(reason_value) if reason_value is not None else decision
    return decision, reason


class LocalNavigationExecutor:
    """Run several bounded local navigation cycles below the MCP boundary."""

    def __init__(
        self,
        driver: EpisodeDriver,
        *,
        clock: Callable[[], float] = time.monotonic,
        trace_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.driver = driver
        self.clock = clock
        self.trace_sink = trace_sink

    def run(
        self,
        *,
        target_position: dict[str, float] | None = None,
        target_id: str | None = None,
        route_bearing_degrees: float | None = None,
        limits: LocalNavigationLimits | None = None,
        capture_on_terminal_uncertainty: bool = True,
    ) -> dict[str, Any]:
        limits = (limits or LocalNavigationLimits()).validate()
        result = LocalNavigationEpisodeResult(
            requested_maximum_seconds=limits.maximum_seconds,
            requested_maximum_distance_m=limits.maximum_distance_m,
            arrival_radius_m=limits.arrival_radius_m,
            maximum_cycles=limits.maximum_cycles,
        )
        started_at = self.clock()
        target = _position_dict(target_position)
        previous_state: Any | None = None
        latest_state: Any | None = None
        stop_outcome: str | None = None

        def finish(outcome: str, reason: str, *, ok: bool = False) -> None:
            result.terminal_outcome = outcome
            result.terminal_reason = reason
            result.ok = ok

        try:
            latest_state = self.driver.observe()
            previous_state = latest_state
            result.start_sequence = _state_value(latest_state, "sequence")
            result.map_id = _state_value(latest_state, "map_id")
            start_position = _position(latest_state)
            result.start_position = _position_dict(start_position)
            if result.start_position is None:
                finish("telemetry_stale", "player spatial telemetry is unavailable")
            else:
                result.target_distance_before_m = _horizontal_distance_m(start_position, target)
                safety_reason = self.driver.safety_reason(latest_state, result.map_id)
                if safety_reason:
                    finish(*self._split_reason(safety_reason))
                elif target is not None and result.target_distance_before_m is not None and result.target_distance_before_m <= limits.arrival_radius_m:
                    finish("goal_reached", "target is already within the arrival radius", ok=True)
                elif route_bearing_degrees is None and target is not None:
                    # A final coordinate is not a route.  The driver may still
                    # resolve a target ID to a runtime route, but a bare target
                    # must not silently authorize straight-line movement.
                    finish("route_required", "a walkable route bearing is required for a bare target coordinate")
                else:
                    stop_outcome = self._run_cycles(
                        result,
                        previous_state,
                        target,
                        target_id,
                        route_bearing_degrees,
                        limits,
                        capture_on_terminal_uncertainty,
                    )
                    finish(
                        stop_outcome or "backend_error",
                        result.terminal_reason
                        if result.terminal_reason != "episode did not start"
                        else self._default_reason(stop_outcome or "backend_error"),
                        ok=(stop_outcome == "goal_reached"),
                    )
        except Exception as exc:
            finish("backend_error", str(exc))
        finally:
            try:
                self.driver.neutralise()
                result.neutralised = True
            except Exception as exc:
                result.neutralised = False
                if result.terminal_outcome in {"goal_reached", "time_budget_exhausted", "distance_budget_exhausted", "maximum_cycles_reached"}:
                    result.terminal_outcome = "backend_error"
                    result.terminal_reason = f"input neutralisation failed: {exc}"
                    result.ok = False
            try:
                latest_state = self.driver.observe()
            except Exception:
                pass
            result.end_sequence = _state_value(latest_state, "sequence") if latest_state is not None else result.start_sequence
            result.end_position = _position_dict(_position(latest_state)) if latest_state is not None else result.start_position
            result.horizontal_displacement_m = _horizontal_distance_m(
                result.start_position,
                result.end_position,
            ) or 0.0
            result.target_distance_after_m = _horizontal_distance_m(result.end_position, target)
            if result.target_distance_before_m is not None and result.target_distance_after_m is not None:
                result.target_progress_m = result.target_distance_before_m - result.target_distance_after_m
            result.total_elapsed_ms = max(0.0, (self.clock() - started_at) * 1000.0)
            result.useful_cycles_per_invocation = result.cycles_completed
            if result.terminal_outcome in VISUAL_TERMINAL_OUTCOMES and capture_on_terminal_uncertainty:
                already_captured = any(
                    isinstance(item.get("visual_evidence"), dict)
                    or isinstance(item.get("screenshot"), dict)
                    for item in result.cycles
                )
                if not already_captured:
                    try:
                        result.screenshot = self.driver.capture_terminal()
                    except Exception as exc:
                        result.screenshot = {"ok": False, "reason": str(exc)}
            if self.trace_sink is not None:
                self.trace_sink(result.to_dict())
        return result.to_dict()

    def _run_cycles(
        self,
        result: LocalNavigationEpisodeResult,
        previous_state: Any,
        target: dict[str, float] | None,
        target_id: str | None,
        route_bearing_degrees: float | None,
        limits: LocalNavigationLimits,
        capture_on_terminal_uncertainty: bool,
    ) -> str:
        del target_id, capture_on_terminal_uncertainty
        cumulative = 0.0
        started_at = self.clock()
        map_id = _state_value(previous_state, "map_id")
        for cycle_index in range(1, limits.maximum_cycles + 1):
            elapsed = self.clock() - started_at
            if elapsed >= limits.maximum_seconds:
                return "time_budget_exhausted"
            if cumulative >= limits.maximum_distance_m:
                return "distance_budget_exhausted"

            state = self.driver.observe()
            safety_reason = self.driver.safety_reason(state, map_id)
            if safety_reason:
                return self._split_reason(safety_reason)[0]
            state_position = _position(state)
            if state_position is None:
                return "telemetry_stale"
            goal_before = _horizontal_distance_m(state_position, target)
            if goal_before is not None and goal_before <= limits.arrival_radius_m:
                return "goal_reached"
            remaining_seconds = max(0.1, limits.maximum_seconds - elapsed)
            remaining_distance = max(0.1, limits.maximum_distance_m - cumulative)
            segment_distance = min(limits.maximum_segment_distance_m, remaining_distance)
            segment_ms = min(
                limits.maximum_segment_ms,
                max(100, int(remaining_seconds * 1000.0)),
            )
            cycle_started = self.clock()
            cycle: dict[str, Any] = {
                "cycle_index": cycle_index,
                "sequence_before": _state_value(state, "sequence"),
                "position_before": _position_dict(state_position),
                "goal_distance_before_m": goal_before,
                "selected_bearing_degrees": route_bearing_degrees,
                "movement_distance_budget_m": segment_distance,
                "movement_duration_ms": segment_ms,
            }
            try:
                decision = self.driver.execute_segment(
                    state,
                    cycle_index,
                    segment_ms,
                    segment_distance,
                )
            except Exception as exc:
                cycle.update({"decision": "backend_error", "reason": str(exc)})
                result.cycles.append(cycle)
                result.cycles_completed += 1
                result.terminal_reason = str(exc)
                return "backend_error"
            cycle["decision"] = decision.get("decision") or decision.get("outcome") or "segment_executed"
            for key in (
                "visual_evidence",
                "screenshot",
                "movement",
                "camera_alignment",
                "route_bearing_degrees",
                "route_source",
                "route_segment_start",
                "route_segment_end",
            ):
                if key in {"visual_evidence", "screenshot"} and isinstance(decision.get(key), dict):
                    cycle[key] = decision[key]
                elif key in {"movement", "camera_alignment"} and isinstance(decision.get(key), dict):
                    cycle[key] = {"ok": decision[key].get("ok")}
                elif decision.get(key) is not None:
                    cycle[key] = decision[key]
            if decision.get("route_bearing_degrees") is not None:
                cycle["selected_bearing_degrees"] = decision["route_bearing_degrees"]
            driver_outcome = _outcome_from_driver_result(decision)
            if driver_outcome is not None:
                coordinator_decision, coordinator_reason = _driver_rejection_details(decision)
                result.coordinator_decision = coordinator_decision
                result.coordinator_reason = coordinator_reason
                result.terminal_reason = ": ".join(
                    item for item in (coordinator_decision, coordinator_reason) if item
                ) or self._default_reason(driver_outcome)
                result.cycles.append(cycle)
                result.cycles_completed += 1
                return driver_outcome

            post = self.driver.observe()
            post_position = _position(post)
            displacement = _horizontal_distance_m(state_position, post_position) or 0.0
            goal_after = _horizontal_distance_m(post_position, target)
            progress = (
                goal_before - goal_after
                if goal_before is not None and goal_after is not None
                else displacement
            )
            sequence_before = _state_value(state, "sequence")
            sequence_after = _state_value(post, "sequence")
            cycle.update({
                "sequence_after": sequence_after,
                "position_after": _position_dict(post_position),
                "displacement_m": displacement,
                "goal_distance_after_m": goal_after,
                "progress_m": progress,
                "elapsed_ms": max(0.0, (self.clock() - cycle_started) * 1000.0),
            })
            result.cycles.append(cycle)
            result.cycles_completed += 1
            cumulative += displacement
            result.movement_segments += 1
            result.cumulative_distance_m = cumulative

            post_safety = self.driver.safety_reason(post, map_id)
            if post_safety:
                return self._split_reason(post_safety)[0]
            if _state_value(post, "map_id") != map_id:
                return "gameplay_state_changed"
            if sequence_after is not None and sequence_before is not None and sequence_after <= sequence_before:
                return "telemetry_not_advancing"
            if goal_after is not None and goal_after <= limits.arrival_radius_m:
                return "goal_reached"
            if displacement < 0.05 or (target is not None and progress <= 0.0):
                return "no_progress"
            previous_state = post
            del previous_state
        return "maximum_cycles_reached"

    @staticmethod
    def _split_reason(value: str) -> tuple[str, str]:
        if value in TERMINAL_OUTCOMES:
            return value, value.replace("_", " ")
        return "backend_error", value

    @staticmethod
    def _default_reason(outcome: str) -> str:
        return {
            "goal_reached": "target reached within the arrival radius",
            "time_budget_exhausted": "episode wall-clock budget expired",
            "distance_budget_exhausted": "episode movement-distance budget expired",
            "maximum_cycles_reached": "episode cycle budget expired",
            "blocked": "local corridor was blocked or uncertain",
            "route_required": "a walkable route is required before movement",
            "visual_reasoning_required": "visual reasoning is required to continue",
            "no_progress": "the last bounded segment did not make sufficient progress",
            "telemetry_not_advancing": "telemetry did not advance after the segment",
        }.get(outcome, outcome.replace("_", " "))
