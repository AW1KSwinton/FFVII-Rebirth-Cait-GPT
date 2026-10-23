from datetime import datetime, timezone
import json
import math

import pytest

from ff7r_agent.models import GameState


def snapshot(*, player_x=0.0, player_y=0.0, yaw=0.0, bench_position=True):
    bench = {
        "id": "PersistentLevel.ValidatedBench",
        "kind": "bench",
        "distance": 5.0,
        "usable": False,
    }
    if bench_position:
        bench["position"] = {"x": 300.0, "y": 400.0, "z": 50.0}
    return {
        "schema_version": 1,
        "sequence": 1,
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "game": {"running": True},
        "player": {
            "name": "Cloud", "hp": 1, "max_hp": 1, "mp": 1, "max_mp": 1,
            "position": {"x": player_x, "y": player_y, "z": 0.0},
            "rotation": {"pitch": 0.0, "yaw": yaw, "roll": 0.0},
        },
        "nearby_interactable": bench,
    }


def test_bench_position_and_relative_vector_are_parsed():
    state = GameState.from_dict(snapshot())
    bench = state.nearby_interactable
    assert bench.position.x == 300.0
    assert bench.position.y == 400.0
    assert bench.relative.x == 3.0
    assert bench.relative.y == 4.0
    assert bench.relative.z == 0.5
    assert bench.relative.horizontal_distance == 5.0
    assert bench.relative.bearing_degrees == pytest.approx(53.130102)
    assert bench.relative.heading_error_degrees == pytest.approx(53.130102)


@pytest.mark.parametrize(
    ("yaw", "expected"),
    [(53.130102, 0.0), (-170.0, -136.869898), (180.0, -126.869898)],
)
def test_heading_error_is_normalised(yaw, expected):
    error = GameState.from_dict(snapshot(yaw=yaw)).nearby_interactable.relative.heading_error_degrees
    assert -180.0 <= error <= 180.0
    assert error == pytest.approx(expected, abs=1e-5)


def test_player_movement_changes_relative_distance_without_moving_bench():
    first = GameState.from_dict(snapshot())
    second = GameState.from_dict(snapshot(player_x=100.0, player_y=100.0))
    assert first.nearby_interactable.position == second.nearby_interactable.position
    assert first.nearby_interactable.relative.horizontal_distance == 5.0
    assert second.nearby_interactable.relative.horizontal_distance == pytest.approx(math.sqrt(13.0))


def test_missing_bench_position_preserves_detection_without_relative_data():
    bench = GameState.from_dict(snapshot(bench_position=False)).nearby_interactable
    assert bench.id == "PersistentLevel.ValidatedBench"
    assert bench.position is None
    assert bench.relative is None


@pytest.mark.parametrize("bad", [None, "bad", float("nan"), float("inf")])
def test_malformed_bench_spatial_data_isolated_from_party_telemetry(bad):
    value = snapshot()
    value["party"] = [{"name": "Cloud", "hp": 1, "max_hp": 1, "mp": 1, "max_mp": 1}]
    value["nearby_interactable"]["position"]["x"] = bad
    state = GameState.from_dict(value)
    assert state.nearby_interactable is None
    assert state.party[0].hp == 1


def test_schema_v1_without_spatial_fields_remains_readable(tmp_path):
    value = snapshot(bench_position=False)
    path = tmp_path / "state.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    state = GameState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    assert state.schema_version == 1
    assert state.nearby_interactable.relative is None
