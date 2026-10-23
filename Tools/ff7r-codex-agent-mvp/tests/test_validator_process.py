from pathlib import Path
import hashlib
import time

import pytest

from ff7r_agent.validator_process import ValidatorProcess
from ff7r_agent.validator_prewarm import build_prewarmed_validator_class
from test_validator_prewarm import _write_fake_validator, _write_manifest, _session


def make_proxy(tmp_path, *, wrong_source=False, wrong_artifact=False):
    source = tmp_path / "validator.py"
    _write_fake_validator(source)
    manifest, artifact, raw, fingerprint = _write_manifest(tmp_path, source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    proxy = ValidatorProcess(source, artifact, source_sha="0" * 64 if wrong_source else source_sha,
                             artifact_sha="0" * 64 if wrong_artifact else raw)
    return proxy


def test_isolated_exact_validator_returns_result_and_closes_on_eof(tmp_path):
    proxy = make_proxy(tmp_path)
    try:
        proxy.wait_ready()
        assert proxy.validate({}) == {"valid": True, "marker": "first"}
        assert proxy.validate({}) == {"valid": True, "marker": "first"}
    finally:
        proxy.close()
    assert proxy.process.poll() is not None
    with pytest.raises(RuntimeError, match="closed"):
        proxy.validate({})


@pytest.mark.parametrize("field", ["wrong_source", "wrong_artifact"])
def test_isolation_rejects_identity_mismatch(tmp_path, field):
    proxy = make_proxy(tmp_path, **{field: True})
    with pytest.raises(RuntimeError, match="unavailable"):
        proxy.wait_ready()
    assert proxy.process.poll() is not None


def test_helper_owner_eof_interrupts_expensive_construction(tmp_path):
    source = tmp_path / "validator.py"
    source.write_text("import time\ntime.sleep(30)\n")
    artifact = tmp_path / "topology.json"
    artifact.write_text("{}")
    proxy = ValidatorProcess(source, artifact,
        source_sha=hashlib.sha256(source.read_bytes()).hexdigest(),
        artifact_sha=hashlib.sha256(artifact.read_bytes()).hexdigest())
    time.sleep(.2)
    started = time.monotonic()
    proxy.close()
    assert time.monotonic() - started < 3
    assert proxy.process.poll() is not None


def test_isolated_prewarm_keeps_binding_and_retires_changed_identity(tmp_path, monkeypatch):
    source = tmp_path / "validator.py"
    _write_fake_validator(source)
    manifest, artifact, raw, fingerprint = _write_manifest(tmp_path, source)
    cls = build_prewarmed_validator_class()
    monkeypatch.setattr(cls, "_validation_source_path", staticmethod(lambda: source))
    validator = cls(None, None, require_topology_binding=True, isolated=True)
    try:
        assert validator.prewarm_from_manifest(manifest)["state"] == "ready"
        original = validator._validator
        assert original.process.poll() is None
        assert validator.bind_topology_session(_session(artifact, raw, fingerprint))["validator_construction_reused"]
        assert validator.validate({})["marker"] == "first"
        _write_fake_validator(source, marker="changed-source")
        assert not validator.bind_topology_session(_session(artifact, raw, fingerprint))["validator_construction_reused"]
        assert original.process.poll() is not None
        assert validator.validate({})["marker"] == "changed-source"
    finally:
        validator.close()
