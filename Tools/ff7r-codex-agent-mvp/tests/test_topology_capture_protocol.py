from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

import pytest

from ff7r_agent.live_route_adapter import (
    AdapterError,
    JsonTopologyProvider,
    RenderedGameplayEvidence,
)
from ff7r_agent.topology_cache import admit_topology_cache

from test_live_route_adapter import make_graph, make_observation


class Clock:
    def __init__(self) -> None:
        self.value = time.time()

    def __call__(self) -> float:
        return self.value


def render() -> RenderedGameplayEvidence:
    return RenderedGameplayEvidence(
        ready=True,
        reason="rendered_gameplay_confirmed",
        classifier_label="gameplay",
        metrics={"whole_nonblack": 1.0},
        capture={"ok": True, "pid": 123},
    )


def make_provider(root: Path, *, clock: Clock | None = None, process_alive=None) -> JsonTopologyProvider:
    active_clock = clock or Clock()
    return JsonTopologyProvider(
        root / "geometry.json",
        probe_path=root / "probe.json",
        enable_path=root / "geometry.enable",
        readiness_path=root / "geometry.ready",
        timing_path=root / "timing.json",
        status_path=root / "status.json",
        clock=active_clock,
        sleeper=lambda seconds: setattr(active_clock, "value", active_clock.value + seconds),
        max_capture_wait_seconds=0.5,
        capture_poll_seconds=0.1,
        process_alive=process_alive,
    )


def write_matching_outputs(root: Path, request_id: str, *, wrong: str | None = None) -> None:
    output_id = wrong or request_id
    payload = {
        "request_id": output_id,
        "source": "ue4ss_native_detour_geometry_dump",
        "status": "complete",
        "movement_sent": False,
        "coordinate_space": "recast",
        "player_position": {"x": 0.0, "y": 0.0, "z": 0.0},
    }
    probe = {
        "request_id": output_id,
        "world": "EndGame",
        "executable_identity": {"sha256": make_observation().executable_sha256},
        "native_geometry_requested": True,
        "native_geometry_marker_consumed": True,
        "native_geometry_dump_written": True,
    }
    timing = {
        "request_id": output_id,
        "probe_attempt": 1,
        "geometry_armed": True,
        "geometry_dump_written": True,
    }
    root.joinpath("geometry.json").write_text(json.dumps(payload), encoding="utf-8")
    root.joinpath("probe.json").write_text(json.dumps(probe), encoding="utf-8")
    root.joinpath("timing.json").write_text(json.dumps(timing), encoding="utf-8")
    root.joinpath("status.json").write_text(
        json.dumps({"request_id": request_id, "state": "completed"}),
        encoding="utf-8",
    )


def test_unique_request_ids_and_identical_marker_payloads():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first = make_provider(root)
        first.arm_capture(make_observation(), render())
        first_id = first.request_id
        enable = json.loads(root.joinpath("geometry.enable").read_text(encoding="utf-8"))
        ready = json.loads(root.joinpath("geometry.ready").read_text(encoding="utf-8"))
        assert first_id and enable == ready
        assert enable["request_id"] == first_id
        assert enable["purpose"] == "static_topology_cache_seed"
        first.capture_status = {"state": "completed", "request_id": first_id}
        first.disarm_capture()

        second = make_provider(root)
        second.arm_capture(make_observation(), render())
        assert second.request_id != first_id


def test_timeout_before_ack_is_distinct_and_cleans_unaccepted_markers():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        provider = make_provider(root)
        provider.arm_capture(make_observation(), render())
        with pytest.raises(AdapterError, match="capture_request_not_accepted"):
            provider.load_current(make_observation())
        assert not root.joinpath("geometry.enable").exists()
        assert not root.joinpath("geometry.ready").exists()


def test_timeout_after_native_ack_is_in_progress_and_preserves_identity_markers():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        clock = Clock()
        provider = make_provider(root, clock=clock)
        provider.arm_capture(make_observation(), render())
        root.joinpath("status.json").write_text(
            json.dumps({"request_id": provider.request_id, "state": "accepted"}),
            encoding="utf-8",
        )
        clock.value += 1.0
        with pytest.raises(AdapterError, match="capture_in_progress_timeout"):
            provider.load_current(make_observation())
        assert root.joinpath("geometry.enable").exists()
        assert root.joinpath("geometry.ready").exists()


@pytest.mark.parametrize("wrong_output", ["geometry", "probe", "timing"])
def test_wrong_id_in_any_required_output_is_rejected(wrong_output: str):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        provider = make_provider(root)
        provider.arm_capture(make_observation(), render())
        wrong = "wrong-request-id"
        write_matching_outputs(root, provider.request_id, wrong=wrong if wrong_output == "geometry" else None)
        if wrong_output == "probe":
            value = json.loads(root.joinpath("probe.json").read_text(encoding="utf-8"))
            value["request_id"] = wrong
            root.joinpath("probe.json").write_text(json.dumps(value), encoding="utf-8")
        if wrong_output == "timing":
            value = json.loads(root.joinpath("timing.json").read_text(encoding="utf-8"))
            value["request_id"] = wrong
            root.joinpath("timing.json").write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(AdapterError, match="capture_output_request_id_mismatch"):
            provider.load_current(make_observation())


def test_terminal_status_stranded_in_tmp_completes_the_capture():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        provider = make_provider(root)
        provider.arm_capture(make_observation(), render())
        write_matching_outputs(root, provider.request_id)
        root.joinpath("status.json").write_text(
            json.dumps({"request_id": provider.request_id, "state": "geometry_written"}),
            encoding="utf-8",
        )
        root.joinpath("status.json.tmp").write_text(
            json.dumps({"request_id": provider.request_id, "state": "completed"}),
            encoding="utf-8",
        )
        graph, _ = make_graph()
        with patch("ff7r_agent.live_route_adapter.load_shipping_detour_topology", return_value=graph):
            topology = provider.load_current(make_observation())
        assert topology.payload_summary["capture_status"]["state"] == "completed"


@pytest.mark.parametrize(
    "stranded",
    [
        {"request_id": "older-request", "state": "completed"},
        {"state": "completed"},
        "{\"request_id\": \"partial",
    ],
)
def test_stranded_tmp_for_another_request_or_partial_is_ignored(stranded):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        provider = make_provider(root)
        provider.arm_capture(make_observation(), render())
        write_matching_outputs(root, provider.request_id)
        root.joinpath("status.json").write_text(
            json.dumps({"request_id": provider.request_id, "state": "geometry_written"}),
            encoding="utf-8",
        )
        root.joinpath("status.json.tmp").write_text(
            stranded if isinstance(stranded, str) else json.dumps(stranded),
            encoding="utf-8",
        )
        with pytest.raises(AdapterError, match="capture_in_progress_timeout"):
            provider.load_current(make_observation())


def test_missing_completion_never_admits_outputs():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        provider = make_provider(root)
        provider.arm_capture(make_observation(), render())
        write_matching_outputs(root, provider.request_id)
        root.joinpath("status.json").write_text(
            json.dumps({"request_id": provider.request_id, "state": "probe_written"}),
            encoding="utf-8",
        )
        with pytest.raises(AdapterError, match="capture_in_progress_timeout"):
            provider.load_current(make_observation())


def test_partial_native_artifact_is_not_admitted_even_with_completed_protocol():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        provider = make_provider(root)
        provider.arm_capture(make_observation(), render())
        write_matching_outputs(root, provider.request_id)
        payload = json.loads(root.joinpath("geometry.json").read_text(encoding="utf-8"))
        payload["status"] = "partial"
        root.joinpath("geometry.json").write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(AdapterError, match="capture_output_incomplete"):
            provider.load_current(make_observation())


def test_native_failure_is_surface_as_native_failure():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        provider = make_provider(root)
        provider.arm_capture(make_observation(), render())
        root.joinpath("status.json").write_text(
            json.dumps({
                "request_id": provider.request_id,
                "state": "failed",
                "error": "capture_native_failed",
            }),
            encoding="utf-8",
        )
        with pytest.raises(AdapterError, match="capture_native_failed"):
            provider.load_current(make_observation())


def test_game_exit_during_capture_is_distinct():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        provider = make_provider(root, process_alive=lambda: False)
        provider.arm_capture(make_observation(), render())
        with pytest.raises(AdapterError, match="capture_game_exited"):
            provider.load_current(make_observation())


def test_matching_completed_outputs_are_consumed_once_and_neutral_markers_clear():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        provider = make_provider(root)
        provider.arm_capture(make_observation(), render())
        write_matching_outputs(root, provider.request_id)
        graph, _ = make_graph()
        with patch("ff7r_agent.live_route_adapter.load_shipping_detour_topology", return_value=graph):
            topology = provider.load_current(make_observation())
        assert topology.payload_summary["capture_request_id"] == provider.request_id
        assert topology.payload_summary["capture_status"]["state"] == "completed"
        assert not root.joinpath("geometry.enable").exists()
        assert not root.joinpath("geometry.ready").exists()


def test_cache_admission_is_atomic_and_points_to_matching_artifact():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        artifact = root / "geometry.json"
        artifact.write_text(json.dumps({"source": "complete"}), encoding="utf-8")
        # The full positive manifest contract is covered by test_topology_cache;
        # this test ensures the publication helper does not publish the manifest
        # before the fingerprinted artifact exists.
        manifest = {
            "static_topology_fingerprint": "A" * 64,
            "raw_artifact_sha256": "B" * 64,
            "validation_result": {"valid": True, "errors": [], "warnings": []},
        }
        with pytest.raises(Exception, match="cache_source_artifact_hash_mismatch"):
            admit_topology_cache(artifact, root / "manifest.json", manifest)
