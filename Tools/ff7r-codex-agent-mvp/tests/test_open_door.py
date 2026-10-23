import threading
from types import SimpleNamespace

import pytest

from ff7r_agent import field_prompt
from ff7r_agent.models import Vec3
from ff7r_agent.service import AgentService, SafetyError, _door_walk_finished


def door_service(xs):
    """A service whose Cloud is at x = xs[i] on the i-th telemetry sample, 0.2 s apart."""
    pressed = []
    clock = {"t": 0.0}
    feet = iter(xs)

    def observe():
        return SimpleNamespace(player=SimpleNamespace(locomotion=SimpleNamespace(feet_position=Vec3(next(feet), 0.0, 0.0))))

    def sleeper(seconds):
        clock["t"] += seconds

    service = SimpleNamespace(
        input=SimpleNamespace(is_real=True, use_interaction=lambda ms: pressed.append(ms) or {"key": "f"},
                              release_all=lambda: None),
        telemetry=SimpleNamespace(observe=observe),
        _action_lock=threading.Lock(), _assert_real_movement_allowed=lambda: None,
        _ensure_game_foreground=lambda: None, _monotonic=lambda: clock["t"],
        _sleeper=sleeper, _record_action=lambda **values: None, _last_action_finished=0.0)
    return service, pressed


def test_open_door_is_refused_without_a_door_prompt(monkeypatch):
    service, pressed = door_service([])
    monkeypatch.setattr(field_prompt, "read_field_prompt",
                        lambda _: {"commands": ["$level_action_talk"], "talk": True, "door": False})
    with pytest.raises(SafetyError, match="no door prompt"):
        AgentService.open_door(service, observe_seconds=1.0)
    assert pressed == []


def test_open_door_returns_once_cloud_has_walked_through_and_stopped(monkeypatch):
    # Live shape: still while the door swings, then a walk through, then still.
    xs = [0.0] * 15 + [10.0 * step for step in range(1, 9)] + [80.0] * 20
    service, pressed = door_service(xs)
    prompts = iter([{"commands": ["$level_action_door"], "door": True, "talk": False},
                    {"commands": [], "door": False, "talk": False, "player_position": {"x": 80.0}}])
    monkeypatch.setattr(field_prompt, "read_field_prompt", lambda _: next(prompts))
    result = AgentService.open_door(service, observe_seconds=10.0)
    assert pressed == [300] and result["prompt_cleared"] and result["outcome"] == "walked_through"
    # 23 samples to come through, then 0.6 s still: well before the 10 s cap.
    assert result["seconds"] == pytest.approx(0.2 * 26, abs=0.25)
    assert result["samples"][-1]["position"]["x"] == 80.0


def test_standing_still_while_the_door_swings_is_not_the_end():
    swinging = [{"t": 0.2 * index, "position": {"x": 0.0, "y": 0.0}} for index in range(20)]
    assert not _door_walk_finished(swinging)


def test_standing_still_is_seen_when_samples_do_not_land_exactly_on_the_window():
    # Live: 0.21 s apart, Cloud through (171 cm) and still for 5 s.
    samples = [{"t": round(0.21 * index, 2), "position": {"x": min(171.0, 8.0 * index), "y": 0.0}} for index in range(30)]
    assert _door_walk_finished(samples)
    assert not _door_walk_finished(samples[:23])  # still for only 0.42 s


def test_a_terrain_action_is_pressed_only_while_the_game_offers_it(monkeypatch):
    service, pressed = door_service([])
    taps = []
    service.input.tap = lambda button, ms: taps.append((button, ms))
    monkeypatch.setattr(field_prompt, "read_field_prompt",
                        lambda _: {"commands": [], "terrain_action": False, "player_position": {"x": 0.0}})
    with pytest.raises(SafetyError, match="no Terrain Action prompt"):
        AgentService.terrain_action(service)
    prompts = iter([{"commands": ["$level_sys_show_parkour_input"], "terrain_action": True, "player_position": {"x": 0.0}},
                    {"commands": [], "terrain_action": False, "player_position": {"x": 150.0}}])
    monkeypatch.setattr(field_prompt, "read_field_prompt", lambda _: next(prompts))
    result = AgentService.terrain_action(service, milliseconds=150, settle_seconds=0.0)
    assert taps == [("dodge", 150)] and result["to"] == {"x": 150.0}
