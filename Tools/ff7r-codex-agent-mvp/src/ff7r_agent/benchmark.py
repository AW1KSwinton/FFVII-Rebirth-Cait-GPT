"""Durable, run-scoped metrics for the physical Chadley route.

The benchmark is observational. Starting, polling, and finishing one never
send game input; movement and interaction methods contribute their already
recorded action JSONL to the active run.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .predictive import replay_navigation_metrics


class BenchmarkError(RuntimeError):
    """Raised when a benchmark lifecycle cannot be completed safely."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _position_dict(state: Any) -> dict[str, float] | None:
    position = getattr(getattr(state, "player", None), "position", None)
    if position is None:
        return None
    values: dict[str, float] = {}
    for axis in ("x", "y", "z"):
        try:
            number = float(getattr(position, axis))
        except (AttributeError, TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        values[axis] = number
    return values


def _normalise_position(value: Any) -> dict[str, float] | None:
    """Return a finite Unreal position dict from state/action-log data."""
    if not isinstance(value, dict):
        return None
    values: dict[str, float] = {}
    for axis in ("x", "y", "z"):
        try:
            number = float(value[axis])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        values[axis] = number
    return values


def _observation_summary(state: Any) -> dict[str, Any] | None:
    if state is None:
        return None
    return {"sequence": getattr(state, "sequence", None), "timestamp_utc": getattr(state, "timestamp_utc", None), "map_id": getattr(state, "map_id", None), "location": getattr(state, "location", None), "position": _position_dict(state)}


def _last_recorded_position(records: Iterable[dict[str, Any]]) -> dict[str, float] | None:
    """Find the latest position emitted by a movement action.

    This is deliberately a fallback, not a replacement for live telemetry:
    action records are enough to preserve a useful benchmark when the game
    unloads or the telemetry producer stops immediately before finish.
    """
    for record in reversed(list(records)):
        for key in ("post_position", "position"):
            position = _normalise_position(record.get(key))
            if position is not None:
                return position
        trace = record.get("navigation_trace_tail")
        if isinstance(trace, list):
            for item in reversed(trace):
                if isinstance(item, dict):
                    position = _normalise_position(item.get("post_position"))
                    if position is not None:
                        return position
    return None


def _recorded_end_observation(records: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    records = list(records)
    position = _last_recorded_position(records)
    if position is None:
        return None
    latest = records[-1] if records else {}
    return {
        "sequence": latest.get("post_sequence"),
        "timestamp_utc": latest.get("timestamp_utc"),
        "map_id": latest.get("map_id"),
        "location": None,
        "position": position,
        "source": "action_log_fallback",
    }


def _horizontal_distance(first: dict[str, float] | None, second: dict[str, float] | None) -> float | None:
    if first is None or second is None:
        return None
    return math.hypot(second["x"] - first["x"], second["y"] - first["y"]) / 100.0


def _vertical_change(first: dict[str, float] | None, second: dict[str, float] | None) -> float | None:
    if first is None or second is None:
        return None
    return (second["z"] - first["z"]) / 100.0


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _efficiency_ratio(displacement_m: float | None, cumulative_m: float | None) -> float | None:
    if displacement_m is None or cumulative_m is None or cumulative_m <= 0.0:
        return None
    return max(0.0, min(1.0, displacement_m / cumulative_m))


def benchmark_action_metrics(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return route-specific counters on top of shared replay metrics."""
    actions = list(records)
    replay = replay_navigation_metrics(actions)
    by_action: dict[str, int] = {}
    for item in actions:
        name = str(item.get("action") or "unknown")
        by_action[name] = by_action.get(name, 0) + 1
    planner_actions = [item for item in actions if item.get("predictive_plan") is not None or item.get("action") == "travel_planned_chunk"]
    planner_distance = planner_requested_distance = 0.0
    for item in planner_actions:
        plan = item.get("predictive_plan")
        if not isinstance(plan, dict):
            continue
        motion = plan.get("motion_plan")
        if isinstance(motion, dict):
            planner_distance += _number(motion.get("planned_distance_m"))
        planner_requested_distance += _number(plan.get("requested_distance_m"))
    interaction_actions = [item for item in actions if item.get("action") == "interact_with_physical_chadley"]
    camera_looks = [item for item in actions if item.get("action") == "look"]
    camera_surveys = [item for item in actions if item.get("action") == "survey_camera"]
    stall_diagnoses = [item for item in actions if item.get("action") in {"diagnose_navigation_stall", "diagnose_forward_pulses"}]
    cumulative = replay.get("total_horizontal_distance_m")
    return {**replay, "cumulative_horizontal_distance_m": cumulative, "action_counts": dict(sorted(by_action.items())), "planner_action_count": len(planner_actions), "planner_requested_distance_m": round(planner_requested_distance, 4), "planner_planned_distance_m": round(planner_distance, 4), "camera_look_count": len(camera_looks), "camera_survey_count": len(camera_surveys), "stall_diagnosis_count": len(stall_diagnoses), "input_rejection_count": sum(1 for item in actions if item.get("ok") is False), "chadley_interaction_attempt_count": len(interaction_actions), "chadley_interaction_success_count": sum(1 for item in interaction_actions if item.get("ok") is True)}


class ChadleyBenchmarkStore:
    """Persist one active benchmark and append immutable completed records."""

    def __init__(self, root: Path, action_log: Path | None = None, screenshot_dir: Path | None = None, now: Any = _now_utc) -> None:
        self.root = Path(root)
        self.active_path = self.root / "chadley-active.json"
        self.history_path = self.root / "chadley-history.jsonl"
        self.action_log = Path(action_log) if action_log is not None else None
        self.screenshot_dir = Path(screenshot_dir) if screenshot_dir is not None else None
        self._now = now

    def _read_active(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.active_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise BenchmarkError(f"cannot read active Chadley benchmark: {exc}") from exc
        return value if isinstance(value, dict) and value.get("status") == "active" else None

    def _write_active(self, value: dict[str, Any]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.active_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as exc:
            raise BenchmarkError(f"cannot write active Chadley benchmark: {exc}") from exc

    def _read_actions(self) -> list[dict[str, Any]]:
        if self.action_log is None:
            return []
        records: list[dict[str, Any]] = []
        try:
            with self.action_log.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise BenchmarkError(f"cannot read Chadley action log: {exc}") from exc
        return records

    def _screenshot_count(self) -> int:
        if self.screenshot_dir is None or not self.screenshot_dir.exists():
            return 0
        try:
            return sum(1 for item in self.screenshot_dir.iterdir() if item.is_file())
        except OSError as exc:
            raise BenchmarkError(f"cannot inspect screenshot evidence: {exc}") from exc

    @staticmethod
    def _records_since(active: dict[str, Any], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        start_count = int(active.get("start_action_count") or 0)
        if start_count <= len(records):
            return records[start_count:]
        started = _timestamp(active.get("started_at_utc"))
        if started is None:
            return []
        return [item for item in records if (_timestamp(item.get("timestamp_utc")) or datetime.min.replace(tzinfo=timezone.utc)) >= started]

    def _metrics(self, active: dict[str, Any], records: list[dict[str, Any]], now: datetime, end_state: Any = None, screenshot_count: int | None = None) -> dict[str, Any]:
        started = _timestamp(active.get("started_at_utc")) or now
        elapsed = max(0.0, (now - started).total_seconds())
        start_observation = active.get("start_observation") or {}
        run_records = self._records_since(active, records)
        end_observation = _observation_summary(end_state) or _recorded_end_observation(run_records)
        start_position = start_observation.get("position") if isinstance(start_observation, dict) else None
        end_position = end_observation.get("position") if isinstance(end_observation, dict) else None
        displacement = _horizontal_distance(start_position, end_position)
        goal_position = _normalise_position(active.get("goal_position"))
        cumulative = benchmark_action_metrics(run_records).get("cumulative_horizontal_distance_m")
        screenshot_count = self._screenshot_count() if screenshot_count is None else screenshot_count
        screenshot_delta = max(0, screenshot_count - int(active.get("start_screenshot_count") or 0))
        metrics = benchmark_action_metrics(run_records)
        metrics.update({
            "elapsed_wall_seconds": round(elapsed, 4),
            "start_to_end_horizontal_displacement_m": round(displacement, 4) if displacement is not None else None,
            "start_to_end_vertical_change_m": round(_vertical_change(start_position, end_position) or 0.0, 4) if start_position is not None and end_position is not None else None,
            "cumulative_horizontal_distance_m": round(cumulative, 4) if cumulative is not None else None,
            "path_efficiency_ratio": round(_efficiency_ratio(displacement, cumulative), 4) if _efficiency_ratio(displacement, cumulative) is not None else None,
            "distance_to_goal_m": round(_horizontal_distance(goal_position, end_position), 4) if goal_position is not None and end_position is not None else None,
            "distance_metric_basis": "horizontal_world_distance_m",
            "end_observation_source": end_observation.get("source", "telemetry") if isinstance(end_observation, dict) else None,
            "screenshot_evidence_count": screenshot_delta,
            "start_action_count": int(active.get("start_action_count") or 0),
            "run_action_count": len(run_records),
            "end_action_count": len(records),
            "phase_timing": active.get("phase_timing"),
        })
        return metrics

    def start(self, route_id: str, state: Any = None, benchmark_id: str | None = None, goal_id: str | None = None, goal_position: dict[str, float] | None = None, setup: dict[str, Any] | None = None, runtime_readiness_handoff_at: datetime | str | None = None) -> dict[str, Any]:
        from .recording import BenchmarkPhaseTiming

        if not route_id.strip():
            raise BenchmarkError("route_id cannot be empty")
        if self._read_active() is not None:
            raise BenchmarkError("a Chadley benchmark is already active")
        normalised_goal = _normalise_position(goal_position)
        started = self._now()
        records = self._read_actions()
        phase_timing = BenchmarkPhaseTiming().mark("benchmark_start", started)
        if runtime_readiness_handoff_at is not None:
            try:
                phase_timing.mark("runtime_readiness_handoff", runtime_readiness_handoff_at)
            except ValueError as exc:
                raise BenchmarkError(str(exc)) from exc
        record = {"benchmark_id": benchmark_id or f"chadley-{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}", "route_id": route_id.strip(), "status": "active", "started_at_utc": _iso(started), "start_action_count": len(records), "start_screenshot_count": self._screenshot_count(), "start_observation": _observation_summary(state), "action_log_path": str(self.action_log) if self.action_log is not None else None, "screenshot_dir": str(self.screenshot_dir) if self.screenshot_dir is not None else None, "goal_id": goal_id, "goal_position": normalised_goal, "setup": dict(setup or {}), "phase_timing": phase_timing.to_dict()}
        self._write_active(record)
        return {"ok": True, "benchmark": record, "metrics": self._metrics(record, records, started, state)}

    def mark_phase(self, phase: str, at: datetime | str | None = None) -> dict[str, Any]:
        """Persist one ordered, read-only benchmark phase boundary."""

        from .recording import BenchmarkPhaseTiming

        active = self._read_active()
        if active is None:
            raise BenchmarkError("no active Chadley benchmark")
        timing = BenchmarkPhaseTiming.from_dict(active.get("phase_timing"))
        try:
            timing.mark(phase, self._now() if at is None else at)
        except ValueError as exc:
            raise BenchmarkError(str(exc)) from exc
        active["phase_timing"] = timing.to_dict()
        self._write_active(active)
        return {"ok": True, "active": True, "benchmark": active, "phase_timing": active["phase_timing"]}

    def status(self, state: Any = None) -> dict[str, Any]:
        active = self._read_active()
        if active is None:
            return {"ok": True, "active": False, "benchmark": None}
        records = self._read_actions()
        return {"ok": True, "active": True, "benchmark": active, "metrics": self._metrics(active, records, self._now(), state)}

    def discard(self) -> dict[str, Any]:
        """Remove an active run without appending it to benchmark history.

        A benchmark attempt can be useful for live diagnostics while still
        being intentionally ineligible for comparison.  Discarding removes
        only the run marker; it never promotes route observations, screenshots,
        headings, or final metrics into the durable benchmark history.
        """
        active = self._read_active()
        if active is None:
            raise BenchmarkError("no active Chadley benchmark")
        try:
            self.active_path.unlink()
        except OSError as exc:
            raise BenchmarkError(f"cannot discard active Chadley benchmark: {exc}") from exc
        return {"ok": True, "active": False, "discarded": True}

    def finish(self, outcome: str = "completed", state: Any = None, note: str | None = None) -> dict[str, Any]:
        from .recording import BenchmarkPhaseTiming

        active = self._read_active()
        if active is None:
            raise BenchmarkError("no active Chadley benchmark")
        finished = self._now()
        records = self._read_actions()
        run_records = self._records_since(active, records)
        end_observation = _observation_summary(state) or _recorded_end_observation(run_records)
        phase_timing = BenchmarkPhaseTiming.from_dict(active.get("phase_timing"))
        try:
            phase_timing.mark("completion", finished)
        except ValueError as exc:
            raise BenchmarkError(str(exc)) from exc
        timing_payload = phase_timing.to_dict()
        completed = {
            "benchmark_id": active.get("benchmark_id"),
            "route_id": active.get("route_id"),
            "goal_id": active.get("goal_id"),
            "status": "completed",
            "started_at_utc": active.get("started_at_utc"),
            "finished_at_utc": _iso(finished),
            "outcome": outcome.strip() or "completed",
            "setup": dict(active.get("setup") or {}),
            "phase_timing": timing_payload,
            "metrics": self._metrics({**active, "phase_timing": timing_payload}, records, finished, state, self._screenshot_count()),
        }
        if note:
            completed["note"] = note
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(completed, sort_keys=True) + "\n")
        except OSError as exc:
            raise BenchmarkError(f"cannot append Chadley benchmark history: {exc}") from exc
        self._write_active(completed)
        return {"ok": True, "active": False, "benchmark": completed}
