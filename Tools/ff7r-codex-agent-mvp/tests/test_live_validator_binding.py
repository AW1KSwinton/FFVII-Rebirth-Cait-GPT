from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ff7r_agent.live_route_adapter import AdapterError, IndependentJsonValidator
from ff7r_agent.detour_route import load_shipping_detour_topology
from ff7r_agent.models import Vec3
from ff7r_agent.topology_cache import sha256_file, static_topology_fingerprint
from test_topology_cache import topology_payload


def _write_artifact(path: Path, payload: dict | None = None) -> tuple[dict, str, str]:
    value = payload or topology_payload()
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return value, sha256_file(path), static_topology_fingerprint(value)


def _session(path: Path, raw_sha: str, static_fingerprint: str) -> SimpleNamespace:
    return SimpleNamespace(
        artifact_path=str(path),
        artifact_sha256=raw_sha,
        static_fingerprint=static_fingerprint,
        fingerprint=static_fingerprint,
        session_id="session-1",
        world_id="EndGame",
        map_id="EndGame",
        source_decision="validated_cache_hit",
        payload_summary={},
    )


def test_cache_binding_overrides_bogus_caller_snapshot_and_records_identity(tmp_path: Path) -> None:
    artifact, raw_sha, static = _write_artifact(tmp_path / "cache.json")
    bogus = tmp_path / "legacy.json"
    validator = IndependentJsonValidator(bogus, require_topology_binding=True)

    evidence = validator.bind_topology_session(_session(tmp_path / "cache.json", raw_sha, static))

    artifact_path = tmp_path / "cache.json"
    assert validator.snapshot_path == artifact_path
    assert evidence["raw_artifact_sha256"] == raw_sha
    assert evidence["static_topology_fingerprint"] == static
    assert validator._load().analyzer.snapshot_path == artifact_path.resolve()


def test_unbound_validator_cannot_validate(tmp_path: Path) -> None:
    validator = IndependentJsonValidator(None, require_topology_binding=True)
    with pytest.raises(AdapterError, match="validator_topology_session_unbound"):
        validator.validate({})


def test_missing_bound_artifact_fails_closed(tmp_path: Path) -> None:
    validator = IndependentJsonValidator(None, require_topology_binding=True)
    with pytest.raises(AdapterError, match="validator_topology_artifact_missing"):
        validator.bind_topology_session(_session(tmp_path / "missing.json", "A" * 64, "B" * 64))


def test_bound_raw_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    _, raw_sha, static = _write_artifact(tmp_path / "cache.json")
    validator = IndependentJsonValidator(None, require_topology_binding=True)
    with pytest.raises(AdapterError, match="validator_topology_artifact_hash_mismatch"):
        validator.bind_topology_session(_session(tmp_path / "cache.json", "A" * 64, static))
    assert raw_sha != "A" * 64


def test_bound_static_fingerprint_mismatch_fails_closed(tmp_path: Path) -> None:
    _, raw_sha, static = _write_artifact(tmp_path / "cache.json")
    validator = IndependentJsonValidator(None, require_topology_binding=True)
    with pytest.raises(AdapterError, match="validator_topology_fingerprint_mismatch"):
        validator.bind_topology_session(_session(tmp_path / "cache.json", raw_sha, "B" * 64))
    assert static != "B" * 64


def test_rebinding_clears_and_reconstructs_analyzer(tmp_path: Path) -> None:
    first_payload, first_raw, first_static = _write_artifact(tmp_path / "first.json")
    second_payload = topology_payload()
    second_payload["tiles"][0]["vertices"][0]["x"] += 1.0
    _, second_raw, second_static = _write_artifact(tmp_path / "second.json", second_payload)
    validator = IndependentJsonValidator(None, require_topology_binding=True)

    validator.bind_topology_session(_session(tmp_path / "first.json", first_raw, first_static))
    first_analyzer = validator._load().analyzer
    validator.bind_topology_session(_session(tmp_path / "second.json", second_raw, second_static))

    assert validator._validator is None
    second_analyzer = validator._load().analyzer
    assert second_analyzer is not first_analyzer
    assert validator.snapshot_path == (tmp_path / "second.json")
    assert first_payload != second_payload


def test_bound_cache_artifact_supports_independent_same_polygon_validation(tmp_path: Path) -> None:
    _, raw_sha, static = _write_artifact(tmp_path / "cache.json")
    graph = load_shipping_detour_topology(tmp_path / "cache.json")
    plan = graph.plan(Vec3(0.0, 0.0, 0.0), Vec3(100.0, 0.0, 0.0)).to_dict()
    validator = IndependentJsonValidator(None, require_topology_binding=True)
    validator.bind_topology_session(_session(tmp_path / "cache.json", raw_sha, static))

    result = validator.validate(plan)

    assert result["valid"] is True
    assert result["errors"] == []
    assert result["warnings"] == []
