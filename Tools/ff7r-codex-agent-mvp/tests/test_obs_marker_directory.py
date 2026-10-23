from pathlib import Path
import json
from datetime import datetime, timezone
from threading import Event
import time
from unittest.mock import Mock

import pytest

from ff7r_agent import input as input_module


def test_markers_go_where_the_running_monitor_reads_them(tmp_path, monkeypatch):
    # Live: the agent ran from a worktree without the bridge's .env and wrote
    # 4,392 markers to a directory no monitor read.
    state = tmp_path / "bridge-state"
    state.mkdir()
    pointer = tmp_path / "Codex" / "OBS Bridge" / "active-state-dir.txt"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(str(state) + "\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("FF7R_OBS_STATE_DIR", raising=False)
    assert input_module._obs_bridge_state_dir() == state


def test_an_explicit_state_directory_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("FF7R_OBS_STATE_DIR", str(tmp_path / "explicit"))
    assert input_module._obs_bridge_state_dir() == tmp_path / "explicit"


def test_a_stale_pointer_to_a_missing_directory_is_ignored(tmp_path, monkeypatch):
    pointer = tmp_path / "Codex" / "OBS Bridge" / "active-state-dir.txt"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(str(tmp_path / "gone"), encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("FF7R_OBS_STATE_DIR", raising=False)
    assert input_module._published_bridge_state_dir() is None


@pytest.fixture
def dispatcher_factory():
    dispatchers = []
    def create(writer, clock=time.monotonic):
        dispatcher = input_module._ObsInputMarkerDispatcher(writer, clock)
        dispatchers.append(dispatcher)
        return dispatcher
    yield create
    for dispatcher in dispatchers:
        dispatcher._queue.join()
        dispatcher._queue.put(None)
        dispatcher._thread.join(timeout=2)
        assert not dispatcher._thread.is_alive()


def test_a_blocked_marker_writer_cannot_expire_supervised_movement(monkeypatch, dispatcher_factory):
    from test_gamepad_input import make_backend
    entered, release = Event(), Event()
    def writer(occurred_at):
        entered.set()
        release.wait(timeout=3)
    dispatcher = dispatcher_factory(writer)
    monkeypatch.setattr(input_module, "_obs_marker_dispatcher", dispatcher)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("FF7R_OBS_INPUT_CLIPS", "1")
    backend, _, gamepad, _ = make_backend()
    accepted = []
    def broker(x, y, *, deadline, **kwargs):
        assert time.monotonic() < deadline
        accepted.append(kwargs["sequence"])
        return {"ok": True}
    gamepad.set_supervised = broker
    try:
        for sequence in range(100):
            backend.supervised_move(0.0, 1.0, deadline=time.monotonic() + 0.3,
                                    stream_id="pursuit", sequence=sequence)
        assert entered.wait(timeout=1)
        assert accepted == list(range(100))
        assert not release.is_set()
    finally:
        release.set()


def test_ten_hz_input_emits_at_most_one_marker_per_second(dispatcher_factory):
    now = [0.0]
    events = []
    dispatcher = dispatcher_factory(events.append, lambda: now[0])
    for tick in range(101):
        now[0] = tick / 10.0
        dispatcher.mark()
        dispatcher._queue.join()
    assert len(events) == 11  # At t=0, 1, ... 10, rather than all 101 renewals.


def test_the_last_input_of_a_burst_is_written_once_the_second_passes(dispatcher_factory):
    now = [0.0]
    events = []
    dispatcher = dispatcher_factory(events.append, lambda: now[0])
    dispatcher.mark()
    dispatcher._queue.join()
    now[0] = 0.6
    dispatcher.mark()  # within the second: remembered, not dropped
    time.sleep(0.6)
    assert len(events) == 1
    now[0] = 1.2
    deadline = time.monotonic() + 2.0
    while len(events) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(events) == 2 and events[1] > events[0]


def test_a_blocked_writer_keeps_only_one_pending_marker(dispatcher_factory):
    entered, release = Event(), Event()
    now = [0.0]
    def writer(occurred_at):
        entered.set()
        release.wait(timeout=3)
    dispatcher = dispatcher_factory(writer, lambda: now[0])
    try:
        dispatcher.mark()
        assert entered.wait(timeout=1)
        for tick in range(1, 1001):
            now[0] = float(tick)
            dispatcher.mark()
        assert dispatcher._queue.qsize() == 1
    finally:
        release.set()


def test_marker_failure_does_not_kill_the_writer(dispatcher_factory):
    now = [0.0]
    writer = Mock(side_effect=[OSError("offline"), None])
    dispatcher = dispatcher_factory(writer, lambda: now[0])
    dispatcher.mark()
    dispatcher._queue.join()
    now[0] = 1.0
    dispatcher.mark()
    dispatcher._queue.join()
    assert writer.call_count == 2 and dispatcher._thread.is_alive()


def test_file_marker_preserves_the_original_input_time(tmp_path, monkeypatch):
    monkeypatch.setattr(input_module, "_cached_obs_state_dir", lambda: tmp_path)
    occurred_at = datetime(2026, 10, 3, 12, 0, 0, 123000, tzinfo=timezone.utc)
    input_module._write_obs_marker(occurred_at)
    markers = list((tmp_path / "input-events").glob("*.json"))
    assert len(markers) == 1
    event = json.loads(markers[0].read_text())
    assert event["occurredAt"] == "2026-10-03T12:00:00.123Z" and event["id"]


def test_file_marker_includes_active_mission(tmp_path, monkeypatch):
    monkeypatch.setattr(input_module, "_cached_obs_state_dir", lambda: tmp_path)
    mission = {"id": "41cf2d0f-6fd3-419f-8022-cb1eab785caf",
               "name": "PR 211 live acceptance at Zhijun",
               "startedAt": "2026-10-07T21:58:03.568Z", "phase": "Load and deck restoration"}
    (tmp_path / "mission-active.json").write_text(json.dumps({"version": 1, "mission": mission}), encoding="utf-8")
    input_module._write_obs_marker(datetime(2026, 10, 7, 22, 23, 58, 326000, tzinfo=timezone.utc))
    event = json.loads(next((tmp_path / "input-events").glob("*.json")).read_text())
    assert event["mission"] == mission


def test_published_path_is_cached_and_refreshes_after_a_monitor_change(tmp_path, monkeypatch):
    monkeypatch.setattr(input_module, "_obs_state_cache", None)
    now = [0.0]
    monkeypatch.setattr(input_module.time, "monotonic", lambda: now[0])
    resolver = Mock(return_value=tmp_path / "first")
    monkeypatch.setattr(input_module, "_obs_bridge_state_dir", resolver)
    for _ in range(10):
        assert input_module._cached_obs_state_dir() == tmp_path / "first"
    assert resolver.call_count == 1
    now[0] = 5.0
    resolver.return_value = tmp_path / "second"
    assert input_module._cached_obs_state_dir() == tmp_path / "second"
    monkeypatch.setenv("FF7R_OBS_STATE_DIR", str(tmp_path / "explicit"))
    input_module._cached_obs_state_dir()
    assert resolver.call_count == 3


def test_disabled_clips_do_not_enqueue_markers(monkeypatch):
    dispatcher = Mock()
    monkeypatch.setattr(input_module, "_obs_marker_dispatcher", dispatcher)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("FF7R_OBS_INPUT_CLIPS", "0")
    input_module.mark_gameplay_input()
    dispatcher.mark.assert_not_called()
