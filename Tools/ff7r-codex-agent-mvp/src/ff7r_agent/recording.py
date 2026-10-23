"""Durable, pose-based recordings of a human traversal.

The recorder observes telemetry; it never sends input.  A caller records
samples at meaningful intervals (normally every 1--2 seconds and at route
landmarks), producing a reusable route/control baseline without pretending
that the current Windows input backend can see arbitrary human key presses.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .benchmark import (
    _efficiency_ratio,
    _horizontal_distance,
    _normalise_position,
    _observation_summary,
    _timestamp,
    _vertical_change,
    _iso,
)
from .runtime_ownership import owner_is_alive, process_identity_token


_LOCK_SCHEMA_VERSION = 2
_LOCK_READ_ATTEMPTS = 3
_LOCK_READ_RETRY_SECONDS = 0.01
_MAX_WINDOWS_PID = (1 << 32) - 1


class RecordingError(RuntimeError):
    """Raised when a human traversal recording cannot be persisted."""


def _valid_lock_pid(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        pid = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pid <= 0 or pid > _MAX_WINDOWS_PID:
        return None
    return pid


def _decode_lock_record(raw: str) -> dict[str, Any] | None:
    """Decode current metadata or the pre-v2 decimal-PID format.

    Unknown or incomplete metadata is deliberately not treated as stale. A
    lock that cannot be identified is still a lock, so callers fail closed.
    """

    value = raw.strip()
    if not value:
        return None
    if value.isdecimal():
        pid = _valid_lock_pid(value)
        if pid is None:
            return None
        return {
            "version": 1,
            "pid": pid,
            "process_creation_token": None,
            "lock_id": None,
        }
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != _LOCK_SCHEMA_VERSION:
        return None
    pid = _valid_lock_pid(payload.get("pid"))
    lock_id = payload.get("lock_id")
    token = payload.get("process_creation_token")
    if pid is None or not isinstance(lock_id, str) or not lock_id:
        return None
    if token is not None and not isinstance(token, str):
        return None
    return {
        "version": _LOCK_SCHEMA_VERSION,
        "pid": pid,
        "process_creation_token": token,
        "lock_id": lock_id,
    }


def _read_lock_record(lock_path: Path) -> dict[str, Any] | None:
    """Read a lock record without interpreting an invalid record as stale."""

    for attempt in range(_LOCK_READ_ATTEMPTS):
        try:
            raw = lock_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise RecordingError(f"cannot read recorder lock: {exc}") from exc
        record = _decode_lock_record(raw)
        if record is not None:
            return record
        if attempt + 1 < _LOCK_READ_ATTEMPTS:
            # A creator may have won O_EXCL and still be flushing its small
            # metadata record. Give that bounded write a chance to complete;
            # persistent corruption remains a fail-closed error.
            time.sleep(_LOCK_READ_RETRY_SECONDS)
    return None


def _current_process_owns_lock(record: dict[str, Any]) -> bool:
    if record.get("pid") != os.getpid():
        return False
    # A legacy PID-only lock cannot distinguish this process from a previous
    # process after PID reuse, so retain the conservative live-owner result.
    recorded_token = record.get("process_creation_token")
    if recorded_token is None:
        return True
    try:
        current_token = process_identity_token(os.getpid())
    except Exception:
        # Failure to prove identity must never turn a possibly-live lock into
        # a stale one.
        return True
    return current_token is None or str(current_token) == str(recorded_token)


def _write_lock_record(descriptor: int, record: dict[str, Any]) -> None:
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("recorder lock write made no progress")
        remaining = remaining[written:]
    os.fsync(descriptor)


def _remove_owned_lock(lock_path: Path, lock_id: str) -> None:
    try:
        record = _read_lock_record(lock_path)
    except FileNotFoundError:
        return
    except RecordingError:
        # Never delete a lock whose ownership metadata is no longer readable.
        return
    if record is None or record.get("lock_id") != lock_id:
        return
    try:
        lock_path.unlink()
    except (FileNotFoundError, OSError):
        pass


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


BENCHMARK_PHASES = (
    "benchmark_start",
    "runtime_readiness_handoff",
    "route_attempt_start",
    "topology_load",
    "route_planning",
    "independent_validation",
    "authorization",
    "first_movement",
    "completion",
)


class BenchmarkPhaseTiming:
    """Record ordered benchmark phase boundaries for durable diagnostics."""

    def __init__(self, timestamps: dict[str, datetime | str] | None = None) -> None:
        self._timestamps: dict[str, datetime] = {}
        supplied = timestamps or {}
        for phase in BENCHMARK_PHASES:
            if phase in supplied:
                self.mark(phase, supplied[phase])

    @staticmethod
    def _timestamp(value: datetime | str) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = _timestamp(value)
        else:
            parsed = None
        if parsed is None:
            raise ValueError("phase timestamp must be an ISO-8601 timestamp")
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    def mark(self, phase: str, at: datetime | str) -> "BenchmarkPhaseTiming":
        """Record one phase boundary, rejecting phase or timestamp regression."""

        if phase not in BENCHMARK_PHASES:
            raise ValueError(f"unknown benchmark phase: {phase}")
        if phase in self._timestamps:
            return self
        phase_index = BENCHMARK_PHASES.index(phase)
        recorded_indexes = [BENCHMARK_PHASES.index(item) for item in self._timestamps]
        if recorded_indexes and phase_index < max(recorded_indexes):
            later = BENCHMARK_PHASES[max(recorded_indexes)]
            raise ValueError(f"benchmark phase {phase} follows already-recorded phase {later}")
        timestamp = self._timestamp(at)
        if self._timestamps:
            previous = self._timestamps[BENCHMARK_PHASES[max(recorded_indexes)]]
            if timestamp < previous:
                raise ValueError(f"benchmark phase {phase} timestamp precedes the previous phase")
        self._timestamps[phase] = timestamp
        return self

    @classmethod
    def from_dict(cls, value: Any) -> "BenchmarkPhaseTiming":
        """Rehydrate known timing fields while ignoring future fields."""

        if isinstance(value, cls):
            return cls(value.to_dict().get("timestamps_utc"))
        if not isinstance(value, dict):
            return cls()
        timestamps = value.get("timestamps_utc")
        return cls(timestamps if isinstance(timestamps, dict) else None)

    def to_dict(self) -> dict[str, dict[str, str | float]]:
        timestamps: dict[str, str] = {}
        durations: dict[str, float] = {}
        previous: datetime | None = None
        for phase in BENCHMARK_PHASES:
            timestamp = self._timestamps.get(phase)
            if timestamp is None:
                continue
            timestamps[phase] = _iso(timestamp)
            durations[phase] = round(max(0.0, (timestamp - previous).total_seconds()), 4) if previous is not None else 0.0
            previous = timestamp
        return {"timestamps_utc": timestamps, "durations_seconds": durations}


def _rotation_dict(value: Any) -> dict[str, float] | None:
    if value is None:
        return None
    result: dict[str, float] = {}
    for axis in ("pitch", "yaw", "roll"):
        try:
            number = float(getattr(value, axis))
        except (AttributeError, TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        result[axis] = number
    return result


def _human_observation(state: Any) -> dict[str, Any] | None:
    summary = _observation_summary(state)
    if summary is None:
        return None
    player = getattr(state, "player", None)
    camera = getattr(state, "camera", None)
    summary.update({
        "running": getattr(state, "running", None),
        "paused": getattr(state, "paused", None),
        "in_menu": getattr(state, "in_menu", None),
        "player_rotation": _rotation_dict(getattr(player, "rotation", None)),
        "camera_rotation": _rotation_dict(getattr(camera, "rotation", None)),
    })
    return summary


class HumanTraversalRecorder:
    """Persist design-time human evidence, never a runtime route input.

    The recorder owns benchmark capture only. Navigation code must not load
    these files, and no human recording is copied into the known-world route
    store automatically.
    """

    def __init__(self, root: Path, action_log: Path | None = None, now: Any = _now_utc) -> None:
        self.root = Path(root)
        self.active_path = self.root / "human-active.json"
        self.history_path = self.root / "human-history.jsonl"
        self.lock_path = self.root / "human-recording.lock"
        self.sampler_lock_path = self.root / "human-sampler.lock"
        self.action_log = Path(action_log) if action_log is not None else None
        self._now = now

    @contextmanager
    def _file_guard(self, lock_path: Path, busy_message: str) -> Iterator[None]:
        """Acquire a recoverable exclusive lock across helper processes.

        Windows liveness is checked through the handle-based runtime
        ownership helper. In particular, this path never calls ``os.kill``:
        Python's Windows implementation maps non-console signals, including
        the POSIX-style signal 0 used by the old code, to ``TerminateProcess``.
        """
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                descriptor = os.open(
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                try:
                    owner = _read_lock_record(lock_path)
                except FileNotFoundError:
                    # The previous owner recovered its stale lock between
                    # O_EXCL and the metadata read. Retry the acquisition.
                    continue
                if owner is None:
                    raise RecordingError(f"{busy_message}; lock metadata is corrupt")
                if _current_process_owns_lock(owner):
                    raise RecordingError(f"{busy_message}; lock is owned by the current process")
                try:
                    alive = owner_is_alive(owner["pid"], owner.get("process_creation_token"))
                except Exception as exc:
                    raise RecordingError(f"{busy_message}; cannot verify lock owner") from exc
                if alive or attempt:
                    raise RecordingError(busy_message)
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise RecordingError(f"cannot recover recorder lock: {exc}") from exc
            else:
                lock_id = uuid.uuid4().hex
                try:
                    record = {
                        "version": _LOCK_SCHEMA_VERSION,
                        "pid": os.getpid(),
                        "process_creation_token": process_identity_token(os.getpid()),
                        "lock_id": lock_id,
                    }
                    _write_lock_record(descriptor, record)
                except Exception as exc:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                    if isinstance(exc, RecordingError):
                        raise
                    raise RecordingError(f"cannot write recorder lock: {exc}") from exc
                try:
                    os.close(descriptor)
                except OSError as exc:
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                    raise RecordingError(f"cannot close recorder lock: {exc}") from exc
                break
        else:  # pragma: no cover - the loop always either breaks or raises
            raise RecordingError("cannot acquire human recorder lock")
        try:
            yield
        finally:
            _remove_owned_lock(lock_path, lock_id)

    @contextmanager
    def _mutation_guard(self) -> Iterator[None]:
        """Serialize recorder writers across MCP/CLI helper processes."""
        with self._file_guard(self.lock_path, "human recording is busy"):
            yield

    @contextmanager
    def sampler_guard(self) -> Iterator[None]:
        """Allow only one background sampler for an active recording."""
        with self._file_guard(self.sampler_lock_path, "human recording sampler is already running"):
            yield

    def _read_active(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.active_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise RecordingError(f"cannot read active human recording: {exc}") from exc
        return value if isinstance(value, dict) and value.get("status") == "active" else None

    def _write_active(self, value: dict[str, Any]) -> None:
        temporary = self.active_path.with_name(
            f"{self.active_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temporary, self.active_path)
        except OSError as exc:
            raise RecordingError(f"cannot write active human recording: {exc}") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _action_count(self) -> int:
        if self.action_log is None:
            return 0
        try:
            with self.action_log.open("r", encoding="utf-8") as stream:
                return sum(1 for _ in stream)
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise RecordingError(f"cannot read human recording action log: {exc}") from exc

    @staticmethod
    def _metrics(recording: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        start = recording.get("start_observation") or {}
        end = recording.get("last_sample") or start
        start_position = start.get("position") if isinstance(start, dict) else None
        end_position = end.get("position") if isinstance(end, dict) else None
        displacement = _horizontal_distance(start_position, end_position)
        cumulative = float(recording.get("cumulative_horizontal_distance_m") or 0.0)
        goal = _normalise_position(recording.get("goal_position"))
        started = _timestamp(recording.get("started_at_utc"))
        elapsed = recording.get("elapsed_wall_seconds")
        if elapsed is None:
            current = now or _now_utc()
            elapsed = max(0.0, (current - started).total_seconds()) if started is not None else None
        first_movement = _timestamp(recording.get("first_movement_at_utc"))
        last_movement = _timestamp(recording.get("last_movement_at_utc"))
        time_to_first_movement = (
            max(0.0, (first_movement - started).total_seconds())
            if first_movement is not None and started is not None else None
        )
        active_movement_window = (
            max(0.0, (last_movement - first_movement).total_seconds())
            if first_movement is not None and last_movement is not None else None
        )
        return {
            "elapsed_wall_seconds": round(float(elapsed), 4) if elapsed is not None else None,
            "time_to_first_movement_seconds": round(time_to_first_movement, 4) if time_to_first_movement is not None else None,
            "active_movement_window_seconds": round(active_movement_window, 4) if active_movement_window is not None else None,
            "movement_sample_count": int(recording.get("movement_sample_count") or 0),
            "sample_count": int(recording.get("sample_count") or 0),
            "start_to_end_horizontal_displacement_m": round(displacement, 4) if displacement is not None else None,
            "start_to_end_vertical_change_m": round(_vertical_change(start_position, end_position) or 0.0, 4) if start_position is not None and end_position is not None else None,
            "cumulative_horizontal_distance_m": round(cumulative, 4),
            "path_efficiency_ratio": round(_efficiency_ratio(displacement, cumulative), 4) if _efficiency_ratio(displacement, cumulative) is not None else None,
            "max_sampling_gap_seconds": round(float(recording.get("max_sampling_gap_seconds") or 0.0), 4),
            "sampling_quality": (
                "endpoint_only"
                if int(recording.get("sample_count") or 0) == 0
                or float(recording.get("max_sampling_gap_seconds") or 0.0) > 60.0
                else "partial"
                if float(recording.get("max_sampling_gap_seconds") or 0.0) > 10.0
                else "continuous"
            ),
            "distance_to_goal_m": round(_horizontal_distance(goal, end_position), 4) if goal is not None and end_position is not None else None,
            "distance_metric_basis": "horizontal_world_distance_m",
            "max_observed_speed_mps": round(float(recording.get("max_observed_speed_mps") or 0.0), 4),
            "paused_sample_count": int(recording.get("paused_sample_count") or 0),
            "menu_sample_count": int(recording.get("menu_sample_count") or 0),
            "harness_action_count_delta": max(0, int(recording.get("last_action_count") or 0) - int(recording.get("start_action_count") or 0)),
        }

    def start(self, route_id: str, state: Any, recording_id: str | None = None, goal_id: str | None = None, goal_position: dict[str, float] | None = None) -> dict[str, Any]:
        with self._mutation_guard():
            if not route_id.strip():
                raise RecordingError("route_id cannot be empty")
            if self._read_active() is not None:
                raise RecordingError("a human traversal recording is already active")
            observation = _human_observation(state)
            if observation is None or observation.get("position") is None:
                raise RecordingError("human recording requires player position telemetry")
            started = self._now()
            record_id = recording_id or f"human-{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
            sample_path = self.root / f"{record_id}-samples.jsonl"
            record = {
                "recording_id": record_id,
                "design_time_only": True,
                "route_id": route_id.strip(),
                "status": "active",
                "started_at_utc": _iso(started),
                "start_observation": observation,
                "last_sample": observation,
                "sample_path": str(sample_path),
                "action_log_path": str(self.action_log) if self.action_log is not None else None,
                "start_action_count": self._action_count(),
                "last_action_count": self._action_count(),
                "sample_count": 0,
                "cumulative_horizontal_distance_m": 0.0,
                "max_observed_speed_mps": 0.0,
                "max_sampling_gap_seconds": 0.0,
                "movement_sample_count": 0,
                "first_movement_at_utc": None,
                "last_movement_at_utc": None,
                "paused_sample_count": 0,
                "menu_sample_count": 0,
                "goal_id": goal_id,
                "goal_position": _normalise_position(goal_position),
                "raw_input_capture": "not_available_from_current_backend",
            }
            self._write_active(record)
            return {"ok": True, "recording": record, "metrics": self._metrics(record)}

    def sample(self, state: Any, screenshot: dict[str, Any] | None = None, note: str | None = None) -> dict[str, Any]:
        with self._mutation_guard():
            active = self._read_active()
            if active is None:
                raise RecordingError("no active human traversal recording")
            observation = _human_observation(state)
            if observation is None or observation.get("position") is None:
                raise RecordingError("human recording sample requires player position telemetry")
            previous = active.get("last_sample") or active.get("start_observation") or {}
            previous_sample_count = int(active.get("sample_count") or 0)
            previous_position = previous.get("position") if isinstance(previous, dict) else None
            current_position = observation.get("position")
            increment = _horizontal_distance(previous_position, current_position) or 0.0
            previous_time = _timestamp(previous.get("timestamp_utc")) if isinstance(previous, dict) else None
            current_time = _timestamp(observation.get("timestamp_utc"))
            elapsed = (current_time - previous_time).total_seconds() if current_time and previous_time else 0.0
            speed = increment / elapsed if elapsed > 0.0 else 0.0
            movement_observed = increment >= 0.25
            sample = {
                **observation,
                "sample_index": int(active.get("sample_count") or 0) + 1,
                "incremental_horizontal_distance_m": round(increment, 4),
                "elapsed_since_previous_sample_s": round(max(0.0, elapsed), 4),
                "speed_mps": round(speed, 4),
                "harness_action_count": self._action_count(),
            }
            if screenshot:
                sample["screenshot"] = screenshot
            if note:
                sample["note"] = note
            try:
                Path(active["sample_path"]).parent.mkdir(parents=True, exist_ok=True)
                with Path(active["sample_path"]).open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(sample, sort_keys=True) + "\n")
            except OSError as exc:
                raise RecordingError(f"cannot append human recording sample: {exc}") from exc
            active["last_sample"] = sample
            active["last_action_count"] = sample["harness_action_count"]
            active["sample_count"] = sample["sample_index"]
            active["cumulative_horizontal_distance_m"] = round(float(active.get("cumulative_horizontal_distance_m") or 0.0) + increment, 6)
            active["max_observed_speed_mps"] = max(float(active.get("max_observed_speed_mps") or 0.0), speed)
            if movement_observed:
                active["movement_sample_count"] = int(active.get("movement_sample_count") or 0) + 1
                active["first_movement_at_utc"] = active.get("first_movement_at_utc") or observation.get("timestamp_utc")
                active["last_movement_at_utc"] = observation.get("timestamp_utc")
            # Do not treat the time between `start` and the sampler's first
            # observation as a route gap. Only gaps between actual samples
            # indicate that the movement polyline may be missing evidence.
            if previous_sample_count > 0:
                active["max_sampling_gap_seconds"] = max(
                    float(active.get("max_sampling_gap_seconds") or 0.0),
                    max(0.0, elapsed),
                )
            active["paused_sample_count"] = int(active.get("paused_sample_count") or 0) + int(bool(observation.get("paused")))
            active["menu_sample_count"] = int(active.get("menu_sample_count") or 0) + int(bool(observation.get("in_menu")))
            self._write_active(active)
            return {"ok": True, "recording": active, "sample": sample, "metrics": self._metrics(active)}

    def status(self) -> dict[str, Any]:
        active = self._read_active()
        if active is None:
            return {"ok": True, "active": False, "recording": None}
        return {"ok": True, "active": True, "recording": active, "metrics": self._metrics(active)}

    def finish(self, outcome: str = "completed", note: str | None = None) -> dict[str, Any]:
        with self._mutation_guard():
            active = self._read_active()
            if active is None:
                raise RecordingError("no active human traversal recording")
            finished = self._now()
            started = _timestamp(active.get("started_at_utc")) or finished
            completed = {
                **active,
                "status": "completed",
                "finished_at_utc": _iso(finished),
                "outcome": outcome.strip() or "completed",
                "elapsed_wall_seconds": round(max(0.0, (finished - started).total_seconds()), 4),
            }
            if note:
                completed["note"] = note
            completed["metrics"] = self._metrics(completed)
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with self.history_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(completed, sort_keys=True) + "\n")
            except OSError as exc:
                raise RecordingError(f"cannot append human recording history: {exc}") from exc
            self._write_active(completed)
            return {"ok": True, "active": False, "recording": completed}


class HumanTraversalSampler:
    """Single-process, low-rate pose sampler for an active human recording."""

    def __init__(
        self,
        recorder: HumanTraversalRecorder,
        state_reader: Callable[[], Any],
        interval_seconds: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.recorder = recorder
        self.state_reader = state_reader
        self.interval_seconds = float(interval_seconds)
        self.sleeper = sleeper
        if not 0.25 <= self.interval_seconds <= 30.0:
            raise RecordingError("sampler interval must be between 0.25 and 30 seconds")

    def run(self) -> dict[str, Any]:
        started = _now_utc()
        samples = 0
        errors: list[str] = []
        last_error: str | None = None
        stopped_reason = "recording_inactive"
        with self.recorder.sampler_guard():
            while True:
                try:
                    status = self.recorder.status()
                except Exception as exc:  # transient replacement/read errors are recoverable
                    last_error = str(exc)
                    if len(errors) < 10:
                        errors.append(last_error)
                    self.sleeper(self.interval_seconds)
                    continue
                if not status.get("active"):
                    break
                try:
                    self.recorder.sample(
                        self.state_reader(),
                        note="background_pose_sample",
                    )
                    samples += 1
                    last_error = None
                except Exception as exc:  # telemetry/file races must not stop recording
                    last_error = str(exc)
                    if len(errors) < 10:
                        errors.append(last_error)
                self.sleeper(self.interval_seconds)
        return {
            "ok": True,
            "started_at_utc": _iso(started),
            "finished_at_utc": _iso(_now_utc()),
            "samples_written": samples,
            "errors": errors,
            "last_error": last_error,
            "stopped_reason": stopped_reason,
        }
