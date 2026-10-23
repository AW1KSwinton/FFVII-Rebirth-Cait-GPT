"""Existing measured camera response and advancing-yaw settling contract."""
from __future__ import annotations

import math
from .camera_follow import shortest_yaw_error_deg

CAMERA_DELTA_PER_DEGREE = 21.40925
CAMERA_MEANINGFUL_CHANGE_DEGREES = 0.05
CAMERA_SETTLE_ENVELOPE_DEGREES = 0.15
CAMERA_SETTLE_DURATION_SECONDS = 0.25
CAMERA_SETTLE_MIN_ADVANCING_SAMPLES = 3
CAMERA_SETTLE_TIMEOUT_SECONDS = 3.0


class CameraYawSettling:
    def __init__(self, initial_yaw: float, initial_sequence: int):
        self.initial_yaw = initial_yaw
        self.sequence = initial_sequence
        self.changed = False
        self.yaws = []
        self.stable_started = None

    def observe(self, *, yaw: float, sequence: int, now: float) -> bool:
        if not math.isfinite(yaw) or not math.isfinite(now):
            raise ValueError("camera_yaw_nonfinite")
        if sequence <= self.sequence:
            return False
        self.sequence = sequence
        if not self.changed:
            if abs(shortest_yaw_error_deg(yaw, self.initial_yaw)) < CAMERA_MEANINGFUL_CHANGE_DEGREES:
                return False
            self.changed = True
        values = self.yaws + [yaw]
        offsets = [shortest_yaw_error_deg(value, values[0]) for value in values]
        if self.stable_started is None or max(offsets) - min(offsets) > CAMERA_SETTLE_ENVELOPE_DEGREES:
            self.stable_started = now
            self.yaws = [yaw]
        else:
            self.yaws = values
        return (len(self.yaws) >= CAMERA_SETTLE_MIN_ADVANCING_SAMPLES
                and now - self.stable_started >= CAMERA_SETTLE_DURATION_SECONDS)
