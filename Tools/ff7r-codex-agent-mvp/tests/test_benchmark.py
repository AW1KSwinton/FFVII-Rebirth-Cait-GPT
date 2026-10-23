import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ff7r_agent.benchmark import ChadleyBenchmarkStore, benchmark_action_metrics
from ff7r_agent.recording import BENCHMARK_PHASES
from ff7r_agent.models import Vec3


def _state(sequence: int, position: Vec3):
    return SimpleNamespace(sequence=sequence, timestamp_utc=f"2026-08-12T00:00:{sequence:02d}Z", map_id="EndGame", location="Nibelheim Upper Square", player=SimpleNamespace(position=position))


def test_benchmark_action_metrics_count_planner_and_camera_events():
    result = benchmark_action_metrics([
        {"action": "travel_direction_chunk", "horizontal_displacement": 4.0, "predictive_plan": {"requested_distance_m": 5.0, "motion_plan": {"planned_distance_m": 3.5}}},
        {"action": "survey_camera"}, {"action": "look"}, {"action": "interact_with_physical_chadley", "ok": True}, {"action": "travel_direction_chunk", "ok": False},
    ])
    assert result["planner_action_count"] == 1
    assert result["planner_planned_distance_m"] == 3.5
    assert result["camera_survey_count"] == 1
    assert result["chadley_interaction_success_count"] == 1
    assert result["input_rejection_count"] == 1


def test_benchmark_lifecycle_persists_metrics(tmp_path: Path):
    action_log = tmp_path / "input-actions.jsonl"
    screenshot_dir = tmp_path / "screenshots"
    screenshot_dir.mkdir()
    (screenshot_dir / "before.bmp").write_bytes(b"before")
    current = [datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)]
    store = ChadleyBenchmarkStore(tmp_path / "benchmarks", action_log=action_log, screenshot_dir=screenshot_dir, now=lambda: current[0])
    assert store.start("route", _state(1, Vec3(0, 0, 0)), "run-1")["benchmark"]["benchmark_id"] == "run-1"
    action_log.write_text("\n".join([
        json.dumps({"action": "travel_direction_chunk", "direction": "forward", "horizontal_displacement": 2.5, "actual_hold_ms": 1000}),
        json.dumps({"action": "travel_vector_chunk", "direction": "forward_left", "horizontal_displacement": 3.0, "actual_hold_ms": 1500}),
    ]) + "\n", encoding="utf-8")
    (screenshot_dir / "after.bmp").write_bytes(b"after")
    current[0] += timedelta(seconds=12.5)
    status = store.status(_state(2, Vec3(250, 0, 0)))
    assert status["metrics"]["elapsed_wall_seconds"] == 12.5
    assert status["metrics"]["total_horizontal_distance_m"] == 5.5
    assert status["metrics"]["screenshot_evidence_count"] == 1
    finished = store.finish("completed", _state(3, Vec3(300, 0, 25)), "baseline")
    assert finished["benchmark"]["metrics"]["movement_action_count"] == 2
    assert finished["benchmark"]["metrics"]["start_to_end_vertical_change_m"] == 0.25
    assert finished["benchmark"]["metrics"]["cumulative_horizontal_distance_m"] == 5.5
    assert finished["benchmark"]["metrics"]["path_efficiency_ratio"] == 0.5455
    assert finished["benchmark"]["metrics"]["distance_to_goal_m"] is None
    assert "action_log_path" not in finished["benchmark"]
    assert "screenshot_dir" not in finished["benchmark"]
    assert "start_observation" not in finished["benchmark"]
    assert "end_observation" not in finished["benchmark"]


def test_benchmark_uses_action_log_endpoint_when_telemetry_is_gone(tmp_path: Path):
    action_log = tmp_path / "input-actions.jsonl"
    current = [datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)]
    store = ChadleyBenchmarkStore(tmp_path / "benchmarks", action_log=action_log, now=lambda: current[0])
    store.start("route", _state(1, Vec3(0, 0, 0)), "run-fallback", "goal", {"x": 1000, "y": 0, "z": 0})
    action_log.write_text(json.dumps({
        "action": "travel_direction_chunk",
        "direction": "forward",
        "horizontal_displacement": 6.0,
        "post_position": {"x": 600, "y": 0, "z": 0},
        "post_sequence": 20,
        "timestamp_utc": "2026-08-12T00:00:04Z",
    }) + "\n", encoding="utf-8")
    current[0] += timedelta(seconds=4)
    result = store.finish("blocked")
    metrics = result["benchmark"]["metrics"]
    assert metrics["start_to_end_horizontal_displacement_m"] == 6.0
    assert metrics["cumulative_horizontal_distance_m"] == 6.0
    assert metrics["path_efficiency_ratio"] == 1.0
    assert metrics["distance_to_goal_m"] == 4.0
    assert metrics["end_observation_source"] == "action_log_fallback"


def test_benchmark_discard_removes_active_run_without_history(tmp_path: Path):
    current = [datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)]
    store = ChadleyBenchmarkStore(tmp_path / "benchmarks", now=lambda: current[0])
    store.start("route", _state(1, Vec3(0, 0, 0)), "discard-me")

    result = store.discard()

    assert result == {"ok": True, "active": False, "discarded": True}
    assert not (tmp_path / "benchmarks" / "chadley-active.json").exists()
    assert not (tmp_path / "benchmarks" / "chadley-history.jsonl").exists()
    assert store.status()["active"] is False


def test_benchmark_history_keeps_setup_summary_only(tmp_path: Path):
    current = [datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)]
    store = ChadleyBenchmarkStore(tmp_path / "benchmarks", now=lambda: current[0])
    store.start("route", _state(1, Vec3(0, 0, 0)), "setup-run", setup={
        "input_backend": "WindowsKeyboardInput",
        "telemetry_backend": "JsonFileTelemetry",
        "recorded_routes_enabled": False,
    })
    current[0] += timedelta(seconds=5)

    finished = store.finish("abandoned", _state(2, Vec3(100, 0, 0)))
    summary = finished["benchmark"]

    assert summary["setup"]["input_backend"] == "WindowsKeyboardInput"
    assert summary["setup"]["recorded_routes_enabled"] is False
    assert "start_observation" not in summary
    assert "end_observation" not in summary


def test_benchmark_phase_timing_persists_without_changing_lifecycle_shape(tmp_path: Path):
    base = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    current = [base]
    store = ChadleyBenchmarkStore(tmp_path / "benchmarks", now=lambda: current[0])
    started = store.start(
        "route",
        _state(1, Vec3(0, 0, 0)),
        "timed-run",
        runtime_readiness_handoff_at=base + timedelta(seconds=1),
    )
    assert started["benchmark"]["benchmark_id"] == "timed-run"
    assert started["benchmark"]["route_id"] == "route"
    assert started["metrics"]["phase_timing"]["timestamps_utc"] == {
        "benchmark_start": "2026-08-12T00:00:00Z",
        "runtime_readiness_handoff": "2026-08-12T00:00:01Z",
    }

    for phase, offset in (
        ("route_attempt_start", 2.0),
        ("topology_load", 5.0),
        ("route_planning", 7.5),
        ("independent_validation", 8.75),
        ("authorization", 10.0),
        ("first_movement", 14.0),
    ):
        store.mark_phase(phase, base + timedelta(seconds=offset))
    current[0] = base + timedelta(seconds=20)
    finished = store.finish("completed", _state(2, Vec3(100, 0, 0)))

    timing = finished["benchmark"]["phase_timing"]
    assert list(timing["timestamps_utc"]) == list(BENCHMARK_PHASES)
    assert timing["durations_seconds"]["completion"] == 6.0
    assert finished["benchmark"]["metrics"]["phase_timing"] == timing
    history = json.loads((tmp_path / "benchmarks" / "chadley-history.jsonl").read_text(encoding="utf-8"))
    assert history["benchmark_id"] == "timed-run"
    assert history["phase_timing"] == timing
