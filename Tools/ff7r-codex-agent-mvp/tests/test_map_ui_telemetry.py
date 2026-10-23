import json
from pathlib import Path

from ff7r_agent.input import SimulationInput
from ff7r_agent.service import AgentService
from ff7r_agent.telemetry import JsonFileTelemetry


def base_snapshot(sequence=7):
    return {
        "schema_version": 1,
        "sequence": sequence,
        "timestamp_utc": "2026-09-15T12:00:00.000Z",
        "game": {
            "running": True,
            "paused": True,
            "in_menu": True,
            "in_combat": False,
            "map_id": "2610-NIBLT",
            "location": "Nibelheim",
            "objective": "",
        },
        "player": None,
        "party": [],
    }


def sidecar(sequence=7, *, status="ready", map_id="2610-NIBLT", samples=2, markers=None):
    return {
        "schema_version": 1,
        "base_sequence": sequence,
        "timestamp_utc": "2026-09-15T12:00:00.000Z",
        "status": status,
        "map_id": map_id,
        "stability": {
            "samples": samples,
            "same_page": samples >= 2,
            "same_markers": samples >= 2,
        },
        "map_state": {
            "page": {
                "open": True,
                "id": "MapWidget_C_0",
                "page": "WorldMap",
                "layer": "field",
            },
            "markers": markers if markers is not None else [
                {
                    "marker_id": "MapIcon_2610-CARD_0101",
                    "runtime_id": "MapWidget_C_0.Marker_Dale",
                    "visible": True,
                    "icon_type": "queens_blood",
                    "subicon": "player",
                    "label": "Dale",
                    "target_id": "oba2610_Actor_CardGamer02",
                    "map_position": {"x": 0.25, "y": 0.50, "z": 0.0},
                }
            ],
        },
    }


def write_fixture(tmp_path: Path, *, snapshot=None, payload=None):
    state_path = tmp_path / "state.json"
    ui_path = tmp_path / "map-ui.json"
    state_path.write_text(json.dumps(snapshot or base_snapshot()), encoding="utf-8")
    ui_path.write_text(json.dumps(payload or sidecar()), encoding="utf-8")
    return state_path, ui_path


def test_valid_stable_map_ui_sidecar_is_merged(tmp_path):
    state_path, ui_path = write_fixture(tmp_path)
    state = JsonFileTelemetry(state_path, map_ui_path=ui_path).observe()
    assert state.map_state["page"]["page"] == "WorldMap"
    assert state.map_state["markers"][0]["label"] == "Dale"
    assert state.map_state["_map_ui_meta"]["source"] == "runtime_map_ui_sidecar"


def test_sequence_map_identity_and_stability_are_fail_closed(tmp_path):
    for payload in (
        sidecar(sequence=6),
        sidecar(map_id="2660-NIBLT"),
        sidecar(samples=1),
        sidecar(status="unavailable"),
    ):
        state_path, ui_path = write_fixture(tmp_path, payload=payload)
        state = JsonFileTelemetry(state_path, map_ui_path=ui_path).observe()
        assert state.map_state is None


def test_marker_action_fields_and_nonfinite_values_are_rejected(tmp_path):
    bad_marker = {
        "marker_id": "bad",
        "runtime_id": "MapWidget_C_0.Marker",
        "operation": "interact_with_entity",
    }
    state_path, ui_path = write_fixture(tmp_path, payload=sidecar(markers=[bad_marker]))
    state = JsonFileTelemetry(state_path, map_ui_path=ui_path).observe()
    assert state.map_state is None

    bad_marker = {
        "marker_id": "bad",
        "runtime_id": "MapWidget_C_0.Marker",
        "map_position": {"x": float("nan"), "y": 0.0, "z": 0.0},
    }
    state_path, ui_path = write_fixture(tmp_path, payload=sidecar(markers=[bad_marker]))
    state = JsonFileTelemetry(state_path, map_ui_path=ui_path).observe()
    assert state.map_state is None


def test_service_exposes_read_only_map_ui_operation(tmp_path):
    state_path, ui_path = write_fixture(tmp_path)
    service = AgentService(
        JsonFileTelemetry(state_path, map_ui_path=ui_path),
        SimulationInput(),
    )
    result = service.map_ui_telemetry()
    assert result["available"] is True
    assert result["source"] == "runtime_map_ui_sidecar"
    assert result["markers"][0]["icon_type"] == "queens_blood"
