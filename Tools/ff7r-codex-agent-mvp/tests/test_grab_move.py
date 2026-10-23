from __future__ import annotations

import threading

import pytest

from ff7r_agent import field_prompt
from ff7r_agent.input import DEFAULT_KEYMAP, SimulationInput
from ff7r_agent.service import AgentService, SafetyError

# Live 2026-10-20, the Mt. Nibel air purifier: "Ctrl Grab".
PURIFIER = {"commands": ["$level_sys_airpurifier_catch"], "player_position": {"x": 19400.0, "y": -256930.0, "z": 18710.0}}


class RealInput:
    is_real = True

    def __init__(self):
        self.grabs = []

    def grab_move(self, segments, stick=False):
        self.grabs.append(list(segments))

    def release_all(self):
        pass


def service_with(prompts, monkeypatch):
    service = AgentService.__new__(AgentService)
    service.input = RealInput()
    service._action_lock = threading.Lock()
    service._last_action_finished = 0.0
    service._monotonic = lambda: 0.0
    service._assert_real_movement_allowed = lambda: None
    service._ensure_game_foreground = lambda: None
    service._record_action = lambda **kw: None
    queue = list(prompts)
    monkeypatch.setattr(field_prompt, "read_field_prompt", lambda svc: queue.pop(0))
    return service


def test_grab_is_ctrl_on_the_keyboard_profile():
    assert DEFAULT_KEYMAP["grab"] == "ctrl"


def test_a_grab_prompt_is_held_for_the_whole_pull(monkeypatch):
    moved = {**PURIFIER, "player_position": {"x": 19400.0, "y": -257130.0, "z": 18710.0}}
    service = service_with([PURIFIER, moved], monkeypatch)
    result = AgentService.grab_move(service, segments=[[0.0, 1.0, 1500], [1.0, 0.0, 800]])
    assert result["ok"] and result["command"] == "$level_sys_airpurifier_catch" and result["moved_m"] == 2.0
    assert service.input.grabs == [[(0.0, 1.0, 1500), (1.0, 0.0, 800)]]
    assert result["milliseconds"] == 2300


def test_segments_are_bounded(monkeypatch):
    service = service_with([PURIFIER], monkeypatch)
    with pytest.raises(SafetyError, match="segments"):
        AgentService.grab_move(service, segments=[[0.0, 1.0, 500]] * 9)
    with pytest.raises(SafetyError, match="direction"):
        AgentService.grab_move(service, segments=[[0.0, 0.0, 500]])


def test_no_grab_without_a_grab_prompt(monkeypatch):
    service = service_with([{"commands": ["$level_action_check"], "player_position": None}], monkeypatch)
    with pytest.raises(SafetyError, match="no grab prompt"):
        AgentService.grab_move(service, 0.0, 1.0, 1500)
    assert service.input.grabs == []


def test_the_simulation_backend_records_a_grab():
    backend = SimulationInput()
    backend.grab_move([(0.0, 1.0, 500)])
    assert backend.actions[-1].action == "grab_move"


def test_the_operation_is_registered_with_the_runtime():
    from ff7r_agent.runtime_commands import SERVICE_ALIASES

    assert SERVICE_ALIASES["grab_move"] == "grab_move"
