"""Lightweight navigation values shared by telemetry and mission clients.

Importing telemetry must not load the asynchronous mission orchestration stack.
"""
from __future__ import annotations

import math
from typing import Any


class MissionError(RuntimeError):
    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response



def finite_position(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        raise MissionError("position_missing")
    try:
        result = {axis: float(value[axis]) for axis in ("x", "y", "z")}
    except (KeyError, TypeError, ValueError) as exc:
        raise MissionError("position_invalid") from exc
    if not all(math.isfinite(v) for v in result.values()):
        raise MissionError("position_nonfinite")
    return result


def horizontal_distance(a: dict[str, float], b: dict[str, float]) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"]) / 100.0
