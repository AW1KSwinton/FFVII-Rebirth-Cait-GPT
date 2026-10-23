from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import ff7r_agent.service as service_module
from ff7r_agent.service import AgentService, SafetyError


def sample(sequence, z, *, grounded=False, in_combat=False, x=-35281.8, state=11):
    # Live 2026-10-20, the Mt. Nibel cave wall: hanging, not grounded, state 11.
    return SimpleNamespace(
        sequence=sequence, in_combat=in_combat,
        player=SimpleNamespace(position=SimpleNamespace(x=x, y=-238454.8, z=z),
                               locomotion=SimpleNamespace(grounded=grounded), controller_state=state))


class RealInput:
    is_real = True

    def __init__(self):
        self.moves = []

    def move(self, x, y, milliseconds):
        self.moves.append((x, y, milliseconds))

    def release_all(self):
        pass


def service_with(samples, monkeypatch):
    monkeypatch.setattr(service_module.time, "sleep", lambda seconds: None)
    service = AgentService.__new__(AgentService)
    service.input = RealInput()
    service._action_lock = threading.Lock()
    service._last_action_finished = 0.0
    service._monotonic = lambda: 0.0
    service._record_action = lambda **kw: None
    queue = list(samples)
    service._assert_real_movement_allowed = lambda **gates: queue.pop(0) if gates == {"allow_swimming": True} else None
    service.telemetry = SimpleNamespace(observe=lambda: queue.pop(0))
    return service


def test_a_hanging_climber_is_moved_up_the_wall(monkeypatch):
    service = service_with([sample(1, 3244.0), sample(2, 3244.0), sample(3, 3424.0)], monkeypatch)
    result = AgentService.climb_move(service, 0.0, 1.0, 800)
    assert result["ok"] and result["climbed_m"] == 1.8 and result["grounded_after"] is False
    assert service.input.moves == [(0.0, 1.0, 800)]


def test_on_the_ground_climb_move_is_refused(monkeypatch):
    service = service_with([sample(1, 3000.0, grounded=True)], monkeypatch)
    with pytest.raises(SafetyError, match="on the ground"):
        AgentService.climb_move(service, 0.0, 1.0, 800)
    assert service.input.moves == []


def test_unreported_ground_contact_needs_the_climbing_state(monkeypatch):
    service = service_with([sample(1, 3529.0, grounded=None), sample(2, 3529.0, grounded=None),
                            sample(3, 3700.0, grounded=None)], monkeypatch)
    assert AgentService.climb_move(service, 0.0, 1.0, 800)["climbed_m"] == 1.71
    service = service_with([sample(1, 3529.0, grounded=None, state=1)], monkeypatch)
    with pytest.raises(SafetyError, match="not climbing"):
        AgentService.climb_move(service, 0.0, 1.0, 800)


def test_a_fall_or_jump_is_never_steered(monkeypatch):
    service = service_with([sample(1, 3244.0), sample(2, 3180.0)], monkeypatch)
    with pytest.raises(SafetyError, match="through the air"):
        AgentService.climb_move(service, 0.0, 1.0, 800)
    assert service.input.moves == []


def test_refused_in_a_fight_and_without_a_direction(monkeypatch):
    service = service_with([sample(1, 3244.0, in_combat=True)], monkeypatch)
    with pytest.raises(SafetyError, match="fight"):
        AgentService.climb_move(service, 0.0, 1.0, 800)
    with pytest.raises(SafetyError, match="direction"):
        AgentService.climb_move(service, 0.0, 0.0, 800)


def test_the_operation_is_registered_with_the_runtime():
    from ff7r_agent.runtime_commands import SERVICE_ALIASES

    assert SERVICE_ALIASES["climb_move"] == "climb_move"
