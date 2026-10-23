from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ff7r_agent.live_route_adapter import (
    TopologySession,
    _path_stat_identity_for_validator,
)
from ff7r_agent.validator_prewarm import build_prewarmed_validator_class


def _write_fake_validator(path: Path, *, marker: str = "first") -> None:
    path.write_text(
        "class TopologyAnalyzer:\n"
        "    def __init__(self, snapshot_path):\n"
        "        self.snapshot_path = snapshot_path\n"
        f"        self.marker = {marker!r}\n"
        "\n"
        "class RoutePlanValidator:\n"
        "    def __init__(self, analyzer):\n"
        "        self.analyzer = analyzer\n"
        "    def validate(self, plan):\n"
        "        return {'valid': True, 'marker': self.analyzer.marker}\n",
        encoding="utf-8",
    )


def _write_manifest(tmp_path: Path, source_path: Path) -> tuple[Path, Path, str, str]:
    artifact_path = tmp_path / "topology.json"
    artifact_path.write_text(
        json.dumps({
            "tiles": [{
                "tile_index_valid": True,
                "tile_index": 0,
                "salt": 1,
                "x": 0,
                "y": 0,
                "layer": 0,
                "lookup_index": 0,
                "vertices": [],
                "polygons": [],
                "off_mesh_connections": [],
            }]
        }),
        encoding="utf-8",
    )
    raw_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest().upper()
    static_fingerprint = "A" * 64
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest().upper()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "source_artifact_path": artifact_path.name,
            "raw_artifact_sha256": raw_sha,
            "static_topology_fingerprint": static_fingerprint,
            "validator_source_sha256": source_sha,
        }),
        encoding="utf-8",
    )
    return manifest_path, artifact_path, raw_sha, static_fingerprint


def _session(artifact_path: Path, raw_sha: str, static_fingerprint: str) -> TopologySession:
    return TopologySession(
        graph=object(),
        artifact_path=str(artifact_path),
        fingerprint=static_fingerprint,
        static_fingerprint=static_fingerprint,
        artifact_sha256=raw_sha,
        session_id="session-a",
        world_id="world-a",
        map_id="map-a",
        captured_at_utc=None,
        current_session=True,
        movement_sent=False,
        payload_summary={
            "cache_manifest_identity": {
                "artifact_path": _path_stat_identity_for_validator(artifact_path),
                "raw_artifact_sha256": raw_sha,
                "static_topology_fingerprint": static_fingerprint,
                "world_id": "world-a",
                "map_id": "map-a",
            }
        },
    )


def test_prewarmed_validator_is_reused_only_after_matching_topology_bind(tmp_path, monkeypatch):
    source_path = tmp_path / "independent_validator.py"
    _write_fake_validator(source_path)
    manifest_path, artifact_path, raw_sha, static_fingerprint = _write_manifest(
        tmp_path, source_path
    )

    validator_type = build_prewarmed_validator_class()
    monkeypatch.setattr(
        validator_type,
        "_validation_source_path",
        staticmethod(lambda: source_path),
    )
    validator = validator_type(None, None, require_topology_binding=True)

    warm = validator.prewarm_from_manifest(manifest_path)
    assert warm["state"] == "ready"
    assert validator._validator is not None
    assert validator._bound_topology_session_id is None

    binding = validator.bind_topology_session(
        _session(artifact_path, raw_sha, static_fingerprint)
    )
    assert binding["validator_construction_reused"] is True
    assert validator.validate({}) == {"valid": True, "marker": "first"}


def test_prewarm_rejects_validator_source_identity_mismatch(tmp_path, monkeypatch):
    source_path = tmp_path / "independent_validator.py"
    _write_fake_validator(source_path)
    manifest_path, _, _, _ = _write_manifest(tmp_path, source_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["validator_source_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    validator_type = build_prewarmed_validator_class()
    monkeypatch.setattr(
        validator_type,
        "_validation_source_path",
        staticmethod(lambda: source_path),
    )
    validator = validator_type(None, None, require_topology_binding=True)

    warm = validator.prewarm_from_manifest(manifest_path)
    assert warm["state"] == "failed"
    assert "validator_source_identity_mismatch" in warm["detail"]
    assert validator._validator is None
    assert validator._validator_cache_key is None


def test_changed_validator_source_cannot_reuse_warmed_object(tmp_path, monkeypatch):
    source_path = tmp_path / "independent_validator.py"
    _write_fake_validator(source_path, marker="old")
    manifest_path, artifact_path, raw_sha, static_fingerprint = _write_manifest(
        tmp_path, source_path
    )

    validator_type = build_prewarmed_validator_class()
    monkeypatch.setattr(
        validator_type,
        "_validation_source_path",
        staticmethod(lambda: source_path),
    )
    validator = validator_type(None, None, require_topology_binding=True)
    assert validator.prewarm_from_manifest(manifest_path)["state"] == "ready"

    _write_fake_validator(source_path, marker="new-source-longer")
    binding = validator.bind_topology_session(
        _session(artifact_path, raw_sha, static_fingerprint)
    )
    assert binding["validator_construction_reused"] is False
    assert validator._validator is None


def test_a_validator_built_ahead_in_the_warm_pool_is_taken_by_the_route_that_binds_it(tmp_path, monkeypatch):
    # Live at Kalm: the route validator took 74 s to build with the game
    # running, and every route stood still for it.
    from ff7r_agent import validator_process

    source_path = tmp_path / "independent_validator.py"
    _write_fake_validator(source_path)
    manifest_path, artifact_path, raw_sha, static_fingerprint = _write_manifest(tmp_path, source_path)
    validator_type = build_prewarmed_validator_class()
    monkeypatch.setattr(validator_type, "_validation_source_path", staticmethod(lambda: source_path))
    validator = validator_type(None, None, require_topology_binding=True, isolated=True)
    built = []
    real = validator_process.ValidatorProcess

    class Counting(real):
        def __init__(self, *args, **kwargs):
            built.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(validator_process, "ValidatorProcess", Counting)
    try:
        assert validator.prewarm_artifact(manifest_path)["state"] == "ready"
        assert validator.prewarm_artifact(manifest_path)["state"] == "ready"  # not built twice
        binding = validator.bind_topology_session(_session(artifact_path, raw_sha, static_fingerprint))
        assert binding["validator_construction_reused"] is False
        assert validator.validate({}) == {"valid": True, "marker": "first"}
        assert built == [1] and validator._warm_pool == {}
    finally:
        validator.close()
