from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence


@dataclass(frozen=True)
class CameraFollowConfig:
    camera_follow_enabled: bool = False
    camera_follow_lookahead_m: float = 5.0
    camera_follow_sprint_lookahead_m: float = 9.0
    camera_follow_deadband_deg: float = 10.0
    camera_follow_max_input: float = 0.35
    camera_follow_gain: float = 1.0 / 90.0
    camera_follow_settle_hysteresis_deg: float = 3.0
    camera_follow_arrival_target: tuple[float, float, float] | None = None
    camera_follow_arrival_distance_m: float = 5.0

    def validate(self) -> "CameraFollowConfig":
        values = (
            self.camera_follow_lookahead_m,
            self.camera_follow_sprint_lookahead_m,
            self.camera_follow_deadband_deg,
            self.camera_follow_max_input,
            self.camera_follow_gain,
            self.camera_follow_settle_hysteresis_deg,
            self.camera_follow_arrival_distance_m,
        )
        if not all(math.isfinite(v) for v in values):
            raise ValueError("camera follow configuration must be finite")
        if self.camera_follow_lookahead_m <= 0 or self.camera_follow_sprint_lookahead_m <= 0:
            raise ValueError("camera follow lookahead must be positive")
        if not 0 <= self.camera_follow_deadband_deg < 90:
            raise ValueError("camera follow deadband must be in [0, 90)")
        if not 0 < self.camera_follow_max_input <= 1:
            raise ValueError("camera follow max input must be in (0, 1]")
        if self.camera_follow_gain <= 0:
            raise ValueError("camera follow gain must be positive")
        if self.camera_follow_arrival_distance_m <= 0:
            raise ValueError("camera follow arrival distance must be positive")
        if self.camera_follow_arrival_target is not None:
            if (len(self.camera_follow_arrival_target) != 3
                    or not all(math.isfinite(v) for v in self.camera_follow_arrival_target)):
                raise ValueError("camera follow arrival target requires three finite coordinates")
            if not self.camera_follow_enabled:
                raise ValueError("camera follow arrival target requires camera follow")
        if not 0 <= self.camera_follow_settle_hysteresis_deg <= self.camera_follow_deadband_deg:
            raise ValueError("camera follow hysteresis must not exceed the deadband")
        return self


def shortest_yaw_error_deg(target_yaw_deg: float, camera_yaw_deg: float) -> float:
    return (float(target_yaw_deg) - float(camera_yaw_deg) + 180.0) % 360.0 - 180.0


def _xy(point) -> tuple[float, float]:
    return float(point.x), float(point.y)


def _route_segments(route: Sequence[object]) -> list[tuple[object, object, float]]:
    segments: list[tuple[object, object, float]] = []
    for a, b in zip(route, route[1:]):
        ax, ay = _xy(a)
        bx, by = _xy(b)
        length = math.hypot(bx - ax, by - ay)
        if length > 1e-9:
            segments.append((a, b, length))
    return segments


def _project_segment(
    segment: tuple[object, object, float], px: float, py: float
) -> tuple[float, float]:
    a, b, length = segment
    ax, ay = _xy(a)
    bx, by = _xy(b)
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (length * length)))
    qx, qy = ax + t * dx, ay + t * dy
    return t, (px - qx) ** 2 + (py - qy) ** 2


def _progress_segment_index(
    segments: Sequence[tuple[object, object, float]],
    position: object,
    start_segment_index: int,
) -> tuple[int, float]:
    """Advance monotonically from the known route segment.

    Camera-follow must not choose an arbitrary globally-nearest segment because
    folded/self-crossing routes can place a much later leg closer in Euclidean
    space. Progress is therefore local: remain on the current segment until
    its projection reaches the end, or until the next segment is both closer
    and the current projection is already near its end.
    """

    px, py = _xy(position)
    index = max(0, min(int(start_segment_index), len(segments) - 1))
    while index + 1 < len(segments):
        current_t, current_d2 = _project_segment(segments[index], px, py)
        if current_t >= 1.0 - 1e-9:
            index += 1
            continue
        _, next_d2 = _project_segment(segments[index + 1], px, py)
        if current_t >= 0.8 and next_d2 + 1e-9 < current_d2:
            index += 1
            continue
        return index, current_t
    final_t, _ = _project_segment(segments[index], px, py)
    return index, final_t


def _route_lookahead_target_with_progress(
    route: Sequence[object],
    position: object,
    lookahead_m: float,
    *,
    start_segment_index: int = 0,
) -> tuple[float, float, int]:
    if len(route) < 2:
        raise ValueError("camera follow requires at least two route points")
    segments = _route_segments(route)
    if not segments:
        return (*_xy(route[-1]), 0)

    index, t = _progress_segment_index(segments, position, start_segment_index)
    remaining_cm = float(lookahead_m) / 0.01  # repository geometry is centimetres
    a, b, length = segments[index]
    segment_remaining = length * (1.0 - t)
    if remaining_cm <= segment_remaining:
        ax, ay = _xy(a)
        bx, by = _xy(b)
        ratio = remaining_cm / length
        return ax + (t + ratio) * (bx - ax), ay + (t + ratio) * (by - ay), index

    remaining_cm -= segment_remaining
    for a, b, length in segments[index + 1 :]:
        if remaining_cm <= length:
            ax, ay = _xy(a)
            bx, by = _xy(b)
            ratio = remaining_cm / length
            return ax + ratio * (bx - ax), ay + ratio * (by - ay), index
        remaining_cm -= length
    return (*_xy(route[-1]), index)


def route_lookahead_target(
    route: Sequence[object],
    position: object,
    lookahead_m: float,
    *,
    start_segment_index: int = 0,
) -> tuple[float, float]:
    x, y, _ = _route_lookahead_target_with_progress(
        route,
        position,
        lookahead_m,
        start_segment_index=start_segment_index,
    )
    return x, y


def target_yaw_for_route(
    route: Sequence[object],
    position: object,
    lookahead_m: float,
    *,
    start_segment_index: int = 0,
) -> float:
    tx, ty = route_lookahead_target(
        route,
        position,
        lookahead_m,
        start_segment_index=start_segment_index,
    )
    px, py = _xy(position)
    dx, dy = tx - px, ty - py
    if math.hypot(dx, dy) <= 1e-6:
        # Fall back to final non-degenerate route segment.
        for a, b in reversed(list(zip(route, route[1:]))):
            ax, ay = _xy(a)
            bx, by = _xy(b)
            dx, dy = bx - ax, by - ay
            if math.hypot(dx, dy) > 1e-6:
                break
        else:
            raise ValueError("camera follow route has no horizontal direction")
    return math.degrees(math.atan2(dy, dx))


class CameraFollowController:
    """Presentation-only horizontal camera controller.

    The controller consumes authorized route geometry, measured camera yaw
    and an optional NPC look target. It never authorizes or chooses movement.
    """

    def __init__(self, config: CameraFollowConfig | None = None) -> None:
        self.config = (config or CameraFollowConfig()).validate()
        self._turning = False
        self._last_sign = 0
        self._route_token: int | None = None
        self._segment_index = 0
        self._arrival_focusing = False

    def neutral(self, reason: str) -> dict[str, object]:
        # Neutralizing camera input must not discard route progress. Pulse-mode
        # movement neutralizes after every command, and resetting here would
        # make folded routes vulnerable to snapping back to an earlier leg.
        # A new route object resets progress in command() instead.
        self._turning = False
        self._last_sign = 0
        return {"active": False, "camera_input_x": 0.0, "reason": str(reason)}

    def command(self, *, route, position, camera_yaw_deg, sprint=False, authority=True,
                remaining_route_m=None, speed_m_s=0.0) -> dict[str, object]:
        if not self.config.camera_follow_enabled:
            return self.neutral("disabled")
        if not authority:
            return self.neutral("authority_suppressed")
        if camera_yaw_deg is None or not math.isfinite(float(camera_yaw_deg)):
            return self.neutral("camera_yaw_missing")
        route_token = id(route)
        if route_token != self._route_token:
            self._route_token = route_token
            self._segment_index = 0
            self._arrival_focusing = False
        lookahead = (
            self.config.camera_follow_sprint_lookahead_m
            if sprint
            else self.config.camera_follow_lookahead_m
        )
        tx, ty, segment_index = _route_lookahead_target_with_progress(
            route,
            position,
            lookahead,
            start_segment_index=self._segment_index,
        )
        self._segment_index = max(self._segment_index, segment_index)
        px, py = _xy(position)
        segments = _route_segments(route)
        remaining_m = 0.0
        if segments:
            index, fraction = _progress_segment_index(segments, position, self._segment_index)
            remaining_m = (segments[index][2] * (1.0 - fraction)
                           + sum(s[2] for s in segments[index + 1:])) / 100.0
        if remaining_route_m is not None:
            if not math.isfinite(remaining_route_m) or remaining_route_m < 0:
                return self.neutral("route_progress_invalid")
            remaining_m = float(remaining_route_m)
        final_yaw = None
        focus_distance = self.config.camera_follow_arrival_distance_m
        if self.config.camera_follow_arrival_target is not None:
            npc_x, npc_y = self.config.camera_follow_arrival_target[:2]
            goal_x, goal_y = _xy(route[-1])
            if math.hypot(npc_x - goal_x, npc_y - goal_y) > 1e-6:
                final_yaw = math.degrees(math.atan2(npc_y - goal_y, npc_x - goal_x))
                # The native sweep requests at most 60 degree/s, but fresh
                # samples and delayed sends leave gaps between sweeps. Live
                # at Kalm, a 125 degree turn achieved about 30 degree/s.
                # Reserve that time before arrival, including acceleration
                # from a corner back to normal running speed.
                turn_seconds = abs(shortest_yaw_error_deg(final_yaw, camera_yaw_deg)) / 30.0 + 0.8
                if math.isfinite(speed_m_s) and speed_m_s > 0:
                    focus_distance = max(focus_distance, max(7.0, speed_m_s) * turn_seconds)
        self._arrival_focusing = self._arrival_focusing or (final_yaw is not None and remaining_m <= focus_distance)
        arrival_focus = self._arrival_focusing
        if arrival_focus:
            tx, ty = self.config.camera_follow_arrival_target[:2]
        dx, dy = tx - px, ty - py
        if math.hypot(dx, dy) <= 1e-6:
            target = target_yaw_for_route(
                route,
                position,
                lookahead,
                start_segment_index=self._segment_index,
            )
        else:
            target = math.degrees(math.atan2(dy, dx))
        if arrival_focus and final_yaw is not None:
            # Prepare the view from the arrival point. A side approach can
            # otherwise swing the current NPC bearing sharply just at arrival.
            target = final_yaw
        error = shortest_yaw_error_deg(target, float(camera_yaw_deg))
        threshold = (
            self.config.camera_follow_deadband_deg
            + self.config.camera_follow_settle_hysteresis_deg
            if not self._turning
            else self.config.camera_follow_deadband_deg
        )
        if arrival_focus:
            threshold = min(threshold, 8.0)
        if abs(error) <= threshold:
            self._turning = False
            self._last_sign = 0
            value = 0.0
        else:
            sign = 1 if error > 0 else -1
            magnitude = min(
                self.config.camera_follow_max_input,
                max(0.08, abs(error) * self.config.camera_follow_gain),
            )
            # Avoid a one-sample sign flip near alignment; require a larger
            # opposite error before reversing an active correction.
            if self._last_sign and sign != self._last_sign and abs(error) < threshold + 5.0:
                value = 0.0
                self._turning = False
                self._last_sign = 0
            else:
                value = sign * magnitude
                self._turning = True
                self._last_sign = sign
        return {
            "active": self._turning,
            "camera_input_x": value,
            "target_yaw_deg": target,
            "camera_yaw_deg": float(camera_yaw_deg),
            "yaw_error_deg": error,
            "lookahead_m": lookahead,
            "route_segment_index": self._segment_index,
            "arrival_focus": arrival_focus,
            "remaining_route_m": remaining_m,
            "reason": "tracking" if value else "aligned",
        }
