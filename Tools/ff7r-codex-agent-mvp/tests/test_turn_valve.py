from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from ff7r_agent import field_prompt, shop
from ff7r_agent.input import DEFAULT_KEYMAP, SimulationInput
from ff7r_agent.service import VALVE_HOLD_TEXT, VALVE_TURN_BINDINGS, AgentService, SafetyError

# Live 2026-10-22, Mt. Nibel reactor: after the grab the valve shows "Hold Ctrl" and "Hold Shift".
GRABBED = {"commands": [], "player_position": {"x": 1.0, "y": 2.0, "z": 3.0}}


class RealInput:
    is_real = True

    def __init__(self):
        self.holds = []

    def hold_bindings(self, bindings, milliseconds):
        self.holds.append((list(bindings), milliseconds))

    def release_all(self):
        pass


def service_with(prompts, monkeypatch, markers=(2, 0), in_combat=False):
    service = AgentService.__new__(AgentService)
    service.input = RealInput()
    service._action_lock = threading.Lock()
    service._last_action_finished = 0.0
    service._monotonic = lambda: 0.0
    service._assert_real_movement_allowed = lambda allow_swimming=False: SimpleNamespace(in_combat=in_combat)
    service._ensure_game_foreground = lambda: None
    service._record_action = lambda **kw: None
    queue = list(prompts)
    monkeypatch.setattr(field_prompt, "read_field_prompt", lambda svc: queue.pop(0))
    counts = list(markers)

    def texts(svc, contains):
        assert contains == VALVE_HOLD_TEXT
        return {"count": counts.pop(0)}
    monkeypatch.setattr(shop, "read_screen_texts", texts)
    return service


def test_the_valve_keys_are_ctrl_and_shift():
    assert [DEFAULT_KEYMAP[binding] for binding in VALVE_TURN_BINDINGS] == ["ctrl", "shift"]


def test_a_grabbed_valve_is_turned_with_both_keys_held(monkeypatch):
    service = service_with([GRABBED, GRABBED], monkeypatch)
    result = AgentService.turn_valve(service, 4000)
    assert result["ok"] and result["commands_after"] == []
    assert (result["hold_markers_before"], result["hold_markers_after"]) == (2, 0)
    assert service.input.holds == [(["grab", "sprint"], 4000)]


def test_alternate_holds_each_handle_in_turn(monkeypatch):
    service = service_with([GRABBED, GRABBED], monkeypatch)
    result = AgentService.turn_valve(service, 1000, handles="alternate", cycles=2)
    assert service.input.holds == [(["grab"], 1000), (["sprint"], 1000)] * 2
    assert result["handles"] == "alternate"


def test_one_handle_can_be_held_alone(monkeypatch):
    service = service_with([GRABBED, GRABBED], monkeypatch)
    AgentService.turn_valve(service, 2000, handles="sprint")
    assert service.input.holds == [(["sprint"], 2000)]


def test_unknown_patterns_are_refused(monkeypatch):
    service = service_with([GRABBED], monkeypatch)
    with pytest.raises(SafetyError, match="handles"):
        AgentService.turn_valve(service, 2000, handles="stick")
    with pytest.raises(SafetyError, match="cycles"):
        AgentService.turn_valve(service, 2000, cycles=11)


def test_the_holds_end_inside_the_request_timeout(monkeypatch):
    service = service_with([GRABBED], monkeypatch)
    with pytest.raises(SafetyError, match="add up"):
        AgentService.turn_valve(service, 8000, cycles=5)
    assert service.input.holds == []


def test_no_turn_without_the_valve_hold_markers(monkeypatch):
    service = service_with([GRABBED], monkeypatch, markers=(0,))
    with pytest.raises(SafetyError, match="grab it first"):
        AgentService.turn_valve(service, 3000)
    assert service.input.holds == []


def test_no_turn_once_cloud_has_let_go(monkeypatch):
    service = service_with([{"commands": ["$level_sys_valve_catch"], "player_position": None}], monkeypatch)
    with pytest.raises(SafetyError, match="not held"):
        AgentService.turn_valve(service, 3000)
    assert service.input.holds == []


def test_no_turn_in_a_fight(monkeypatch):
    service = service_with([GRABBED], monkeypatch, in_combat=True)
    with pytest.raises(SafetyError, match="fight"):
        AgentService.turn_valve(service, 3000)
    assert service.input.holds == []


def test_the_hold_is_bounded(monkeypatch):
    service = service_with([GRABBED], monkeypatch)
    for milliseconds in (400, 10001, 1.5):
        with pytest.raises(SafetyError, match="milliseconds"):
            AgentService.turn_valve(service, milliseconds)


def test_the_simulation_backend_records_the_hold():
    backend = SimulationInput()
    backend.hold_bindings(["grab", "sprint"], 3000)
    assert backend.actions[-1].action == "hold_bindings"


def test_the_operation_is_registered_with_the_runtime():
    from ff7r_agent.runtime_commands import SERVICE_ALIASES

    assert SERVICE_ALIASES["turn_valve"] == "turn_valve"
