import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "navigation_overhead_breakdown.py"
spec = importlib.util.spec_from_file_location("navigation_overhead_breakdown", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)


def _obs(seq, settled, stamp):
    return {
        "monotonic_t": stamp,
        "timestamp_utc": "2026-09-12T13:00:00.000Z",
        "sequence": seq,
        "controllable": settled,
        "actuator_neutral": True,
        "raw": {
            "health": {
                "game_running": True,
                "paused": False,
                "in_menu": False,
                "loaded_gameplay": True,
                "player_spatial": True,
                "gameplay_settled": settled,
                "telemetry_live": True,
            }
        },
    }


def test_readiness_episode_pairing_and_advancement():
    timeline = [
        {"event": "follower_paused_for_readiness", "t": 10.0},
        {"event": "readiness_loss_triggered", "t": 10.01, "trigger_sequence": 7, "failures": ["not_controllable"], "observation": _obs(7, False, 10.0)},
        {"event": "readiness_recovery_settled_wait_completed", "t": 10.20, "result": {"sequence": 9}},
        {"event": "readiness_recovery_sample", "t": 10.25, "observation": _obs(10, True, 10.25)},
        {"event": "readiness_reacquired_route_state", "t": 10.25, "observation": _obs(10, True, 10.25)},
    ]
    rows = mod.readiness_episodes(timeline)
    assert len(rows) == 1
    assert rows[0].duration_seconds == 0.25
    assert rows[0].last_valid_sequence == 7
    assert rows[0].next_fresh_sequence == 10
    assert rows[0].telemetry_advancement == "advanced"
    assert "settled samples" in rows[0].cause


def test_adapter_envelope_unwraps_nested_response():
    payload = {"started_at": 123.0, "response": {"result": {"run": {"timeline": []}}}}
    result = mod._unwrap_adapter(payload)
    assert "run" in result
    assert mod._adapter_started_epoch(payload) == 123.0


def test_interval_sum_is_non_negative_and_exact():
    row = mod.Interval("x", 1.0, 3.25, "unknown", "fixture", "no")
    assert row.seconds == 2.25


@pytest.mark.parametrize("times, detailed", [
    ((81, 85, 90), True),
    ((180, 185, 190), False),  # Git checkout times, after the recorded run.
    ((85, 81, 90), False),  # Copied files with inconsistent ordering.
])
def test_outside_tail_requires_recorded_window_timestamps(tmp_path, monkeypatch, times, detailed):
    wall = tmp_path / "wall-clock.json"
    wall.write_text(json.dumps({"start_utc": "1970-01-01T00:00:00Z",
                                "end_utc": "1970-01-01T00:01:40Z"}))
    for filename, stamp in zip(("114-get_live_route_adapter_status.json",
                               "115-get_live_route_adapter_status.json",
                               "117-close_control_session.json"), times):
        path = tmp_path / filename
        path.write_text("{}")
        os.utime(path, (stamp, stamp))
    timeline = [{"event": name, "t": stamp} for name, stamp in (
        ("actuator_neutralized", 0), ("topology_source_decision", 1),
        ("topology_source_selected", 2), ("validator_topology_bound", 3),
        ("route_plan_identity_verified", 4), ("movement_setpoint_issued", 5))]
    monkeypatch.setattr(mod, "_anchor_offset", lambda _: 0)
    monkeypatch.setattr(mod, "_load_budget_summary", lambda _: {
        "attempts": [{"window_end_monotonic": 80}]})
    intervals = mod.outside_intervals({}, timeline, wall, tmp_path)
    assert sum(item.seconds for item in intervals) == pytest.approx(25)
    assert all(0 <= item.start_monotonic < item.end_monotonic <= 100 for item in intervals)
    assert any(item.label == "NavigationMission poll sleep" for item in intervals) is detailed


def test_markdown_does_not_invent_readiness_episodes(tmp_path):
    wall = tmp_path / "wall-clock.json"
    wall.write_text(json.dumps({"elapsed_seconds": 10}))
    report = mod._markdown(tmp_path / "adapter.json", wall, {},
                           {"adapter_attempt_seconds": 5}, [], [], 0)
    assert "62 recovered" not in report
    assert "All 62 triggers" not in report
    assert "0 recovered" in report
