from __future__ import annotations

import hashlib
import json

import pytest

from ff7r_agent.live_route_adapter import AdapterError, ValidatedTopologyCacheProvider


def test_read_bytes_with_digest_binds_decode_to_same_snapshot(tmp_path):
    path = tmp_path / "topology.json"
    expected = {"status": "complete", "tiles": [{"x": 1, "vertices": [1.0, 2.0, 3.0]}]}
    raw = json.dumps(expected, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)

    admitted_raw, digest = ValidatedTopologyCacheProvider._read_bytes_with_digest(
        path, "cache_artifact_unreadable"
    )
    path.write_text('{"status":"tampered"}', encoding="utf-8")

    assert admitted_raw == raw
    assert digest == hashlib.sha256(raw).hexdigest().upper()
    assert ValidatedTopologyCacheProvider._decode_json_bytes(
        admitted_raw, "cache_artifact_unreadable"
    ) == expected


def test_decode_json_bytes_rejects_non_object_and_invalid_json():
    with pytest.raises(AdapterError, match="cache_artifact_unreadable"):
        ValidatedTopologyCacheProvider._decode_json_bytes(b"[]", "cache_artifact_unreadable")
    with pytest.raises(AdapterError, match="cache_artifact_unreadable"):
        ValidatedTopologyCacheProvider._decode_json_bytes(b"{", "cache_artifact_unreadable")
