import threading
from types import SimpleNamespace

import pytest

from ff7r_agent import field_prompt
from ff7r_agent.models import Vec3
from ff7r_agent.service import AgentService, SafetyError

CHECK = "$level_action_check"


def field_service(states):
    """A service whose i-th telemetry sample is states[i] (0.2 s apart); the last repeats."""
    pressed = []
    clock = {"t": 0.0}
    samples = list(states)

    def observe():
        return samples.pop(0) if len(samples) > 1 else samples[0]

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


def field(x=0.0, **flags):
    values = {"paused": False, "in_menu": False, "loading": False, "map_id": "EndGame", "interaction": None}
    values.update(flags)
    return SimpleNamespace(player=SimpleNamespace(locomotion=SimpleNamespace(feet_position=Vec3(x, 0.0, 0.0))),
                           **values)


def blank():
    return SimpleNamespace(player=None, paused=False, in_menu=False, loading=False, map_id=None, interaction=None)


def prompts(monkeypatch, *shown):
    reads = iter([{"commands": list(commands)} for commands in shown])
    monkeypatch.setattr(field_prompt, "read_field_prompt", lambda _: next(reads))


@pytest.mark.parametrize("commands", [[], ["$level_action_talk"], [CHECK, "$level_action_talk"]])
def test_nothing_is_pressed_unless_exactly_that_prompt_is_shown(monkeypatch, commands):
    service, pressed = field_service([field()])
    prompts(monkeypatch, commands)
    with pytest.raises(SafetyError):
        AgentService.use_field_action(service, command="check", observe_seconds=1.0)
    assert pressed == []


@pytest.mark.parametrize("command,operation", [("talk", "talk_to_npc"), ("door", "open_door"),
                                               ("$level_sys_show_parkour_input", "terrain_action")])
def test_prompts_with_their_own_operation_are_sent_there(monkeypatch, command, operation):
    service, pressed = field_service([field()])
    with pytest.raises(SafetyError, match=operation):
        AgentService.use_field_action(service, command=command)
    assert pressed == []


def test_a_non_interact_key_is_refused():
    service, pressed = field_service([field()])
    with pytest.raises(SafetyError, match="only interact prompts"):
        AgentService.use_field_action(service, command="$level_sys_buggy_call")
    assert pressed == []


def test_a_conversation_after_the_press_is_reported(monkeypatch):
    service, pressed = field_service([field(), field(interaction={"state": "dialogue"})])
    prompts(monkeypatch, [CHECK])
    result = AgentService.use_field_action(service, command="Check", observe_seconds=4.0)
    assert pressed == [300] and result["command"] == CHECK and result["outcome"] == "dialogue"
    assert result["seconds"] == pytest.approx(0.2)


def test_a_tutorial_tip_is_reported_as_paused(monkeypatch):
    service, _ = field_service([field(), field(), field(paused=True)])
    prompts(monkeypatch, [CHECK])
    assert AgentService.use_field_action(service, command=CHECK)["outcome"] == "paused"


def test_one_blank_read_is_not_a_scene_but_a_blank_feed_is(monkeypatch):
    service, _ = field_service([field(), blank(), field(), blank(), blank(), blank()])
    prompts(monkeypatch, [CHECK])
    result = AgentService.use_field_action(service, command="check")
    assert result["outcome"] == "field_feed_blank" and result["seconds"] == pytest.approx(1.0)


def test_cloud_walking_through_and_stopping_is_reported(monkeypatch):
    service, _ = field_service([field(0.0)] * 5 + [field(20.0 * step) for step in range(1, 6)] + [field(100.0)])
    prompts(monkeypatch, [CHECK], [])
    result = AgentService.use_field_action(service, command="check", observe_seconds=10.0)
    assert result["outcome"] == "moved" and result["commands_after"] == []


@pytest.mark.parametrize("after,outcome", [([], "prompt_cleared"), ([CHECK], "no_change")])
def test_without_a_visible_change_the_prompt_decides(monkeypatch, after, outcome):
    service, _ = field_service([field()])
    prompts(monkeypatch, [CHECK], after)
    result = AgentService.use_field_action(service, command="check", observe_seconds=1.0)
    assert result["outcome"] == outcome and result["seconds"] == pytest.approx(1.0)


def test_a_hold_prompt_is_held(monkeypatch):
    """Live 2026-10-21: the Mt. Nibel wall lever's prompt read "Hold"."""
    service, pressed = field_service([field()])
    prompts(monkeypatch, ["$level_action_operation"], [])
    AgentService.use_field_action(service, command="operation", observe_seconds=1.0, hold_ms=2500)
    assert pressed == [2500]
    with pytest.raises(SafetyError, match="hold_ms"):
        AgentService.use_field_action(service, command="operation", hold_ms=9000)


def test_the_interaction_key_may_be_held_four_seconds():
    from ff7r_agent.input import validate_duration

    assert validate_duration(4000, "interaction") == 4000
    with pytest.raises(Exception):
        validate_duration(4000)
