from __future__ import annotations

import contextlib

import pytest

from ff7r_agent import live_route_adapter
from ff7r_agent.live_route_adapter import AdapterError, AgentServiceTelemetrySource


def source_with(samples, monkeypatch):
    """Live 2026-10-20: stepping over a plank left one sample without Cloud's pose."""
    monkeypatch.setattr(live_route_adapter, "game_process_snapshot", contextlib.nullcontext)
    monkeypatch.setattr(live_route_adapter.time, "sleep", lambda seconds: None)
    source = AgentServiceTelemetrySource.__new__(AgentServiceTelemetrySource)
    queue = list(samples)

    def observe_current():
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    source._observe_current = observe_current
    return source


def test_a_brief_pose_gap_is_waited_out(monkeypatch):
    source = source_with([AdapterError("cloud_pose_or_actor_yaw_missing"), "observation"], monkeypatch)
    assert source.observe() == "observation"


def test_a_lasting_pose_gap_or_another_error_still_stops(monkeypatch):
    gap = [AdapterError("cloud_pose_or_actor_yaw_missing")] * (live_route_adapter.POSE_GAP_RETRIES + 1)
    with pytest.raises(AdapterError, match="cloud_pose_or_actor_yaw_missing"):
        source_with(gap, monkeypatch).observe()
    with pytest.raises(AdapterError, match="live_session_identity_missing"):
        source_with([AdapterError("live_session_identity_missing"), "observation"], monkeypatch).observe()
