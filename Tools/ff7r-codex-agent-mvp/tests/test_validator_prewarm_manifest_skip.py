from __future__ import annotations

from types import SimpleNamespace

from ff7r_agent import live_route_adapter, validator_prewarm


def test_missing_default_manifest_skips_without_resources(monkeypatch, tmp_path):
    missing = tmp_path / "missing-manifest.json"
    monkeypatch.setattr(
        live_route_adapter,
        "DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH",
        missing,
    )
    service = SimpleNamespace()

    status = validator_prewarm.prepare_default_validator_prewarm(service)

    assert status["state"] == "skipped"
    assert status["reason"] == "default_manifest_missing"
    assert not hasattr(service, "_validated_navigation_resources")
