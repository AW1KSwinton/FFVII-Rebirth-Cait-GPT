from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from ff7r_agent.models import GameState, ValidationError
from ff7r_agent.telemetry import JsonFileTelemetry, TelemetryError


def snapshot(timestamp: str, **extra) -> dict:
    payload = {
        "schema_version": 1,
        "sequence": 1,
        "timestamp_utc": timestamp,
        "game": {
            "running": True,
            "paused": False,
            "in_menu": False,
            "in_combat": False,
            "map_id": "EndGame",
            "location": "",
            "objective": "",
        },
        "player": {
            "name": "Cloud",
            "hp": 1,
            "max_hp": 1,
            "mp": 1,
            "max_mp": 1,
            "position": {"x": 0, "y": 0, "z": 0},
            "rotation": {"pitch": 0, "yaw": 0, "roll": 0},
        },
        "party": [],
        "target": None,
        "nearby_interactable": None,
        "nearby_enemies": [],
        "rest": {"state": "idle"},
        "telemetry": {"live": True},
    }
    payload.update(extra)
    return payload


def test_legacy_whole_second_timestamp_remains_parseable() -> None:
    state = GameState.from_dict(snapshot("2026-08-21T21:34:12Z"))
    assert state.timestamp_utc.endswith("Z")


@pytest.mark.parametrize(
    "timestamp",
    ["2026-08-21T21:34:12.347Z", "2026-08-21T21:34:12.347891Z"],
)
def test_fractional_timestamp_is_preserved(timestamp: str) -> None:
    state = GameState.from_dict(snapshot(timestamp))
    assert state.timestamp_utc == timestamp


def test_timestamp_timezone_is_mandatory() -> None:
    with pytest.raises(ValidationError):
        GameState.from_dict(snapshot("2026-08-21T21:34:12.347"))


def test_age_seconds_preserves_fractional_seconds() -> None:
    now = datetime(2026, 8, 21, 21, 34, 12, 900_000, tzinfo=timezone.utc)
    state = GameState.from_dict(snapshot("2026-08-21T21:34:12.347Z"))
    assert state.age_seconds(now) == pytest.approx(0.553)


@pytest.mark.parametrize(("age", "accepted"), [(0.499, True), (0.501, False)])
def test_route_freshness_boundary_preserves_fractional_age(age: float, accepted: bool) -> None:
    now = datetime(2026, 8, 21, 21, 34, 12, 900_000, tzinfo=timezone.utc)
    timestamp = (now - timedelta(seconds=age)).isoformat().replace("+00:00", "Z")
    state = GameState.from_dict(snapshot(timestamp))
    assert (state.age_seconds(now) <= 0.5) is accepted


def test_producer_timing_diagnostics_are_backward_compatible() -> None:
    state = GameState.from_dict(
        snapshot(
            "2026-08-21T21:34:12.347Z",
            sampled_at_utc="2026-08-21T21:34:12.347Z",
            published_at_utc="2026-08-21T21:34:12.389Z",
            producer_elapsed_ms=42.0,
            source_timestamp_utc="2026-08-21T21:34:12Z",
        )
    )
    assert state.sampled_at_utc.endswith(".347Z")
    assert state.published_at_utc.endswith(".389Z")
    assert state.producer_elapsed_ms == pytest.approx(42.0)
    assert state.source_timestamp_utc.endswith("Z")


def test_json_telemetry_accepts_current_whole_second_source_timestamp(tmp_path) -> None:
    path = tmp_path / "state.json"
    payload = snapshot(
        "2026-08-21T21:34:12.900Z",
        source_timestamp_utc="2026-08-21T21:34:12Z",
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    state = JsonFileTelemetry(path).observe()

    assert state.source_timestamp_utc == "2026-08-21T21:34:12Z"


@pytest.mark.parametrize(
    "source_timestamp",
    ["2026-08-21T21:34:09Z", "2026-08-21T21:34:16Z"],
)
def test_json_telemetry_rejects_republished_stale_or_future_source_sample(
    tmp_path, source_timestamp: str
) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            snapshot(
                "2026-08-21T21:34:12.900Z",
                source_timestamp_utc=source_timestamp,
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(TelemetryError, match="source timestamp"):
        JsonFileTelemetry(path).observe()


def test_json_telemetry_rejects_malformed_source_timestamp(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            snapshot(
                "2026-08-21T21:34:12.900Z",
                source_timestamp_utc="not-a-timestamp",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(TelemetryError, match="source timestamp"):
        JsonFileTelemetry(path).observe()


@pytest.mark.parametrize("field", ["sampled_at_utc", "published_at_utc", "source_timestamp_utc"])
def test_optional_timing_timestamps_require_timezone(field: str) -> None:
    with pytest.raises(ValidationError):
        GameState.from_dict(snapshot("2026-08-21T21:34:12.347Z", **{field: "2026-08-21T21:34:12.347"}))



def posed(sequence: int, feet_x: float) -> dict:
    """A sample whose capsule bottom is ``feet_x`` cm from the actor root."""
    payload = snapshot(datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), sequence=sequence)
    payload["player"].update(actor_id="PC0000_00_Cloud", locomotion={
        "reference": "upright_capsule_bottom", "feet_position": {"x": feet_x, "y": 0, "z": -88},
        "capsule_radius_cm": 34, "capsule_half_height_cm": 88,
        "velocity_cm_s": {"x": 0, "y": 0, "z": 0}, "grounded": True})
    return payload


def test_a_skewed_pose_sample_is_skipped_for_the_next_one(tmp_path, monkeypatch) -> None:
    # Live 2026-10-05 (Gold Saucer): one sample whose capsule disagreed with
    # its actor root stopped pursuit routes that succeeded when retried.
    from ff7r_agent import telemetry
    path = tmp_path / "state.json"
    path.write_text(json.dumps(posed(1, 40.0)), encoding="utf-8")
    monkeypatch.setattr(telemetry.time, "sleep",
                        lambda _: path.write_text(json.dumps(posed(2, 0.0)), encoding="utf-8"))
    assert JsonFileTelemetry(path).observe().sequence == 2


def test_a_persistent_pose_skew_still_fails_and_reports_its_size(tmp_path, monkeypatch) -> None:
    from ff7r_agent import telemetry
    path = tmp_path / "state.json"
    path.write_text(json.dumps(posed(1, 40.0)), encoding="utf-8")
    monkeypatch.setattr(telemetry.time, "sleep", lambda _: None)
    with pytest.raises(TelemetryError, match=r"actor root \(horizontal 40.0 cm, vertical 0.0 cm\)"):
        JsonFileTelemetry(path).observe()
