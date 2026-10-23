from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from ff7r_agent import recording
from ff7r_agent.models import Vec3
from ff7r_agent.recording import BENCHMARK_PHASES, BenchmarkPhaseTiming, HumanTraversalRecorder, HumanTraversalSampler, RecordingError


def _state(sequence: int, seconds: int, position: Vec3):
    return SimpleNamespace(
        sequence=sequence,
        timestamp_utc=f"2026-08-12T00:00:{seconds:02d}Z",
        map_id="EndGame",
        location="Nibelheim Upper Square",
        running=True,
        paused=False,
        in_menu=False,
        player=SimpleNamespace(
            position=position,
            rotation=SimpleNamespace(pitch=1.0, yaw=25.0, roll=0.0),
        ),
        camera=SimpleNamespace(
            rotation=SimpleNamespace(pitch=2.0, yaw=30.0, roll=0.0),
        ),
    )


@pytest.fixture(autouse=True)
def recording_locks_are_clean_after_each_test(tmp_path: Path):
    yield
    assert not list(tmp_path.rglob("human-*.lock"))


def _lock_payload(*, pid: int, token: str | None, lock_id: str = "test-lock") -> str:
    return json.dumps({
        "version": 2,
        "pid": pid,
        "process_creation_token": token,
        "lock_id": lock_id,
    })


def test_human_recording_persists_samples_and_route_metrics(tmp_path: Path):
    current = [datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)]
    recorder = HumanTraversalRecorder(tmp_path / "human", now=lambda: current[0])
    started = recorder.start("route", _state(1, 0, Vec3(0, 0, 0)), "human-1", "goal", {"x": 500, "y": 0, "z": 0})
    assert started["recording"]["raw_input_capture"] == "not_available_from_current_backend"
    current[0] += timedelta(seconds=2)
    sampled = recorder.sample(_state(2, 2, Vec3(100, 0, 0)), note="first landmark")
    assert sampled["sample"]["incremental_horizontal_distance_m"] == 1.0
    assert sampled["sample"]["speed_mps"] == 0.5
    current[0] += timedelta(seconds=1)
    finished = recorder.finish("completed")
    metrics = finished["recording"]["metrics"]
    assert metrics["elapsed_wall_seconds"] == 3.0
    assert metrics["time_to_first_movement_seconds"] == 2.0
    assert metrics["active_movement_window_seconds"] == 0.0
    assert metrics["movement_sample_count"] == 1
    assert metrics["sample_count"] == 1
    assert metrics["cumulative_horizontal_distance_m"] == 1.0
    assert metrics["start_to_end_horizontal_displacement_m"] == 1.0
    assert metrics["path_efficiency_ratio"] == 1.0
    assert metrics["distance_to_goal_m"] == 4.0
    assert metrics["sampling_quality"] == "continuous"
    assert (tmp_path / "human" / "human-1-samples.jsonl").is_file()


def test_human_recording_marks_large_sampling_gap(tmp_path: Path):
    current = [datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)]
    recorder = HumanTraversalRecorder(tmp_path / "human", now=lambda: current[0])
    recorder.start("route", _state(1, 0, Vec3(0, 0, 0)), "human-gap")
    current[0] += timedelta(seconds=2)
    recorder.sample(_state(2, 2, Vec3(100, 0, 0)))
    current[0] += timedelta(seconds=18)
    recorder.sample(_state(3, 20, Vec3(200, 0, 0)))
    finished = recorder.finish("completed")
    metrics = finished["recording"]["metrics"]
    assert metrics["max_sampling_gap_seconds"] == 18.0
    assert metrics["sampling_quality"] == "partial"


def test_human_recording_separates_total_time_from_movement_window(tmp_path: Path):
    current = [datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)]
    recorder = HumanTraversalRecorder(tmp_path / "human", now=lambda: current[0])
    recorder.start("route", _state(1, 0, Vec3(0, 0, 0)), "human-window")
    current[0] += timedelta(seconds=10)
    recorder.sample(_state(2, 10, Vec3(100, 0, 0)))
    current[0] += timedelta(seconds=4)
    recorder.sample(_state(3, 14, Vec3(200, 0, 0)))
    current[0] += timedelta(seconds=6)
    finished = recorder.finish("completed")
    metrics = finished["recording"]["metrics"]
    assert metrics["elapsed_wall_seconds"] == 20.0
    assert metrics["time_to_first_movement_seconds"] == 10.0
    assert metrics["active_movement_window_seconds"] == 4.0
    assert metrics["movement_sample_count"] == 2


def test_human_sampler_stops_after_recording_is_finished(tmp_path: Path):
    current = [datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)]
    recorder = HumanTraversalRecorder(tmp_path / "human", now=lambda: current[0])
    recorder.start("route", _state(1, 0, Vec3(0, 0, 0)), "human-sampler")

    def finish_after_sample(_seconds: float) -> None:
        recorder.finish("completed")

    sampler = HumanTraversalSampler(
        recorder,
        lambda: _state(2, 2, Vec3(100, 0, 0)),
        sleeper=finish_after_sample,
    )
    result = sampler.run()
    assert result["samples_written"] == 1
    assert result["errors"] == []
    assert result["stopped_reason"] == "recording_inactive"


def test_human_sampler_rejects_duplicate_sampler(tmp_path: Path):
    recorder = HumanTraversalRecorder(tmp_path / "human")
    with recorder.sampler_guard():
        sampler = HumanTraversalSampler(recorder, lambda: _state(1, 0, Vec3(0, 0, 0)))
        try:
            sampler.run()
        except RecordingError as exc:
            assert "already running" in str(exc)
        else:  # pragma: no cover - the guard must reject the nested sampler
            raise AssertionError("duplicate sampler was not rejected")


def test_stale_recording_lock_is_safely_recovered(tmp_path: Path):
    recorder = HumanTraversalRecorder(tmp_path / "human")
    recorder.root.mkdir()
    recorder.sampler_lock_path.write_text(
        _lock_payload(pid=2**31 - 1, token="not-a-live-process"),
        encoding="utf-8",
    )

    with recorder.sampler_guard():
        assert recorder.sampler_lock_path.exists()

    assert not recorder.sampler_lock_path.exists()


def test_legacy_decimal_stale_recording_lock_is_recovered(tmp_path: Path):
    recorder = HumanTraversalRecorder(tmp_path / "human")
    recorder.root.mkdir()
    recorder.sampler_lock_path.write_text(str(2**31 - 1), encoding="ascii")

    with recorder.sampler_guard():
        pass

    assert not recorder.sampler_lock_path.exists()


def test_live_foreign_recording_lock_fails_closed(tmp_path: Path):
    recorder = HumanTraversalRecorder(tmp_path / "human")
    recorder.root.mkdir()
    foreign_pid = os.getppid()
    assert foreign_pid != os.getpid()
    recorder.sampler_lock_path.write_text(
        _lock_payload(
            pid=foreign_pid,
            token=recording.process_identity_token(foreign_pid),
        ),
        encoding="utf-8",
    )

    with pytest.raises(RecordingError, match="already running"):
        with recorder.sampler_guard():
            pass

    assert recorder.sampler_lock_path.exists()
    recorder.sampler_lock_path.unlink()


def test_current_process_recording_lock_is_rejected_without_signal_or_deadlock(monkeypatch, tmp_path: Path):
    recorder = HumanTraversalRecorder(tmp_path / "human")
    recorder.root.mkdir()
    recorder.sampler_lock_path.write_text(
        _lock_payload(
            pid=os.getpid(),
            token=recording.process_identity_token(os.getpid()),
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(recording.os, "kill", lambda *args: pytest.fail("recording lock must not call os.kill"))

    started = time.monotonic()
    with pytest.raises(RecordingError, match="current process"):
        with recorder.sampler_guard():
            pass
    assert time.monotonic() - started < 0.5
    assert recorder.sampler_lock_path.exists()
    recorder.sampler_lock_path.unlink()


def test_malformed_recording_lock_is_deterministic_and_preserved(tmp_path: Path):
    recorder = HumanTraversalRecorder(tmp_path / "human")
    recorder.root.mkdir()
    recorder.sampler_lock_path.write_text("not-lock-metadata", encoding="utf-8")

    with pytest.raises(RecordingError, match="metadata is corrupt"):
        with recorder.sampler_guard():
            pass

    assert recorder.sampler_lock_path.read_text(encoding="utf-8") == "not-lock-metadata"
    recorder.sampler_lock_path.unlink()


def test_lock_cleanup_does_not_remove_a_replaced_owner_record(tmp_path: Path):
    recorder = HumanTraversalRecorder(tmp_path / "human")
    foreign_pid = os.getppid()
    with recorder.sampler_guard():
        recorder.sampler_lock_path.write_text(
            _lock_payload(
                pid=foreign_pid,
                token=recording.process_identity_token(foreign_pid),
                lock_id="replacement-owner",
            ),
            encoding="utf-8",
        )

    assert recorder.sampler_lock_path.exists()
    recorder.sampler_lock_path.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows creation identity is required for PID-reuse coverage")
def test_pid_reuse_recording_lock_is_recovered_from_identity_mismatch(tmp_path: Path):
    recorder = HumanTraversalRecorder(tmp_path / "human")
    recorder.root.mkdir()
    foreign_pid = os.getppid()
    token = recording.process_identity_token(foreign_pid)
    assert token is not None
    recorder.sampler_lock_path.write_text(
        _lock_payload(pid=foreign_pid, token=f"{token}-previous-process"),
        encoding="utf-8",
    )

    with recorder.sampler_guard():
        pass

    assert not recorder.sampler_lock_path.exists()


def test_benchmark_phase_timing_orders_and_round_trips_serialization():
    base = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    offsets = [0.0, 1.25, 2.0, 5.0, 7.5, 8.75, 10.0, 14.0, 20.0]
    timing = BenchmarkPhaseTiming()
    for phase, offset in zip(BENCHMARK_PHASES, offsets):
        timing.mark(phase, base + timedelta(seconds=offset))

    payload = timing.to_dict()
    assert list(payload["timestamps_utc"]) == list(BENCHMARK_PHASES)
    assert payload["durations_seconds"] == {
        "benchmark_start": 0.0,
        "runtime_readiness_handoff": 1.25,
        "route_attempt_start": 0.75,
        "topology_load": 3.0,
        "route_planning": 2.5,
        "independent_validation": 1.25,
        "authorization": 1.25,
        "first_movement": 4.0,
        "completion": 6.0,
    }
    assert BenchmarkPhaseTiming.from_dict(json.loads(json.dumps(payload, sort_keys=True))).to_dict() == payload


def test_benchmark_phase_timing_rejects_phase_regression_and_keeps_duplicate_idempotent():
    base = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    timing = BenchmarkPhaseTiming().mark("benchmark_start", base)
    timing.mark("topology_load", base + timedelta(seconds=2))
    with pytest.raises(ValueError, match="follows already-recorded"):
        timing.mark("route_attempt_start", base + timedelta(seconds=3))
    assert timing.mark("topology_load", base + timedelta(seconds=4)).to_dict()["timestamps_utc"]["topology_load"] == "2026-08-12T00:00:02Z"
