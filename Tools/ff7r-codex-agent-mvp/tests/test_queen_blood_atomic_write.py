"""Offline regression tests for concurrent Queen's Blood snapshot publication."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

import pytest

from ff7r_agent import queen_blood_profile as module


def test_concurrent_publishers_own_distinct_complete_temporary_snapshots(tmp_path, monkeypatch):
    path = tmp_path / "public-model-run.json"
    barrier = Barrier(2)
    original = module.os.replace
    sources = []
    values = [{"profile": "first", "padding": "a" * 8192}, {"profile": "second", "padding": "b" * 8192}]

    def replace(source, target):
        first_attempt = source not in sources
        sources.append(source)
        assert json.loads(source.read_text(encoding="utf-8")) in values
        # Synchronize distinct publishers once. A real Windows sharing denial
        # can retry after the other publisher has already finished, so a retry
        # must not wait for a nonexistent second barrier participant.
        if first_attempt:
            barrier.wait(timeout=5)
        original(source, target)

    monkeypatch.setattr(module.os, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(module._write, path, value) for value in values]
        for job in jobs:
            job.result(timeout=10)
    assert len(set(sources)) == 2
    assert json.loads(path.read_text(encoding="utf-8")) in values
    assert sorted(item.name for item in tmp_path.iterdir()) == [path.name]


@pytest.mark.parametrize("code", [5, 32, 33])
def test_transient_windows_replace_denial_retries_same_owned_file(tmp_path, monkeypatch, code):
    path = tmp_path / "public-model-run.json"
    original = module.os.replace
    attempts, delays = [], []

    def replace(source, target):
        attempts.append(source)
        if len(attempts) < 4:
            error = PermissionError("temporary reader sharing denial")
            error.winerror = code
            raise error
        original(source, target)

    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(module.time, "sleep", delays.append)
    module._write(path, {"ok": True})
    assert len(set(attempts)) == 1 and len(attempts) == 4
    assert delays == [0.01, 0.02, 0.04]
    assert json.loads(path.read_text()) == {"ok": True}
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("code, retries", [(5, 4), (112, 1), (None, 1)])
def test_persistent_or_unrelated_failure_preserves_destination_and_cleans_only_owned_temp(tmp_path, monkeypatch, code, retries):
    path = tmp_path / "public-model-run.json"
    path.write_text('{"previous": true}')
    unrelated = tmp_path / "public-model-run.json.other.tmp"
    unrelated.write_text("another writer")
    attempts = []

    def replace(source, target):
        attempts.append(source)
        error = OSError("permanent replace failure")
        if code is not None:
            error.winerror = code
        raise error

    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    with pytest.raises(OSError, match="permanent"):
        module._write(path, {"new": True})
    assert len(attempts) == retries
    assert json.loads(path.read_text()) == {"previous": True}
    assert unrelated.read_text() == "another writer"
    assert list(tmp_path.glob("*.tmp")) == [unrelated]
