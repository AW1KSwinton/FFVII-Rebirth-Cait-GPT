"""Controller identity changes, not commanded-member labels, prove control."""
from copy import deepcopy

import pytest

from ff7r_agent.party_control import switch_combat_character


class Battle:
    def __init__(self):
        self.index, self.now, self.sequence = 0, 0, 0
        self.presses, self.menu_reads = [], 0
        self.transform = lambda value: value
        self.tap_effect = None
        self.menu = {"open": False, "party_status": [{"name": "Cloud", "hp": 100}]}

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def read(self):
        self.sequence += 1
        value = {"ok": True, "available": True, "identity": {
            "process_token": "synthetic-process", "map_id": "map", "world_id": "world"},
            "combat": {"in_battle": True, "paused": False, "party_complete": True,
                       "source_sequence": self.sequence, "controlled_actor_id": ["Cloud_1", "Barret_1", "Red_1"][self.index],
                       "party": [{"actor_id": name, "player_type": kind, "hp": 100}
                                 for name, kind in [("Cloud_1", 0), ("Barret_1", 1), ("Red_1", 4)]]}}
        return self.transform(value)

    def read_menu(self):
        self.menu_reads += 1
        return deepcopy(self.menu)

    def battle_menu_tap(self, button, milliseconds):
        assert button == "up" and milliseconds == 120
        self.presses.append(button)
        if self.tap_effect:
            return self.tap_effect()
        self.index = (self.index + 1) % 3
        return {"ok": True}

    def switch(self, member="Barret", execute=True):
        return switch_combat_character(self, member, execute, read=self.read, read_menu=self.read_menu,
                                        clock=self.clock, sleep=self.sleep)


def test_changes_actual_controller_and_verifies_identity():
    battle = Battle()
    result = battle.switch()
    assert result["ok"] and result["outcome"] == "switched"
    assert result["before_controlled_actor_id"] == "Cloud_1" and result["controlled_actor_id"] == "Barret_1"
    assert result["transitions"] == [{"source_sequence": 2, "controlled_actor_id": "Barret_1"}]
    assert result["input_sent"] and len(battle.presses) == 1


def test_cycles_by_observed_control_without_assuming_party_order():
    battle = Battle()
    result = battle.switch("Red XIII")
    assert result["ok"] and result["controlled_actor_id"] == "Red_1" and len(battle.presses) == 2


def test_already_controlled_and_plan_send_nothing():
    battle = Battle()
    assert battle.switch("Cloud")["outcome"] == "already_controlled"
    result = battle.switch("Barret", execute=False)
    assert result["ok"] and result["outcome"] == "planned" and not result["input_sent"]
    assert battle.presses == [] and battle.menu_reads == 0


@pytest.mark.parametrize("member, execute", [("", True), (None, True), ("Cloud\n", True), ("Cloud", "yes")])
def test_invalid_request_never_reads_or_inputs(member, execute):
    battle = Battle()
    with pytest.raises(ValueError):
        battle.switch(member, execute)
    assert battle.sequence == 0 and battle.presses == []


@pytest.mark.parametrize("change, reason", [
    ("inactive", "combat_not_active"), ("paused", "combat_paused_or_unobserved"),
    ("incomplete", "combat_party_unobserved"), ("dead", "combat_character_unavailable"),
    ("identity", "combat_identity_unobserved"),
])
def test_unavailable_or_unobserved_context_refuses_input(change, reason):
    battle = Battle()

    def transform(value):
        state = value["combat"]
        if change == "inactive": state["in_battle"] = False
        if change == "paused": state["paused"] = True
        if change == "incomplete": state["party_complete"] = False
        if change == "dead": state["party"][1]["hp"] = 0
        if change == "identity": value["identity"]["world_id"] = None
        return value
    battle.transform = transform
    result = battle.switch()
    assert not result["ok"] and result["outcome"] == reason and battle.presses == []


def test_missing_member_and_command_menu_refuse_before_input():
    battle = Battle()
    assert battle.switch("Sephiroth")["outcome"] == "combat_character_unavailable"
    battle.menu["open"] = True
    result = battle.switch()
    assert not result["ok"] and result["outcome"] == "close_battle_command_menu_first" and battle.presses == []


def test_commanded_member_change_is_not_controller_change_or_permission_to_retry():
    battle = Battle()
    battle.tap_effect = lambda: {"ok": True, "commanded_member": "Barret"}
    result = battle.switch()
    assert not result["ok"] and result["outcome"] == "combat_controller_unchanged"
    assert result["controlled_actor_id"] == "Cloud_1" and len(battle.presses) == 1 and battle.now <= 2.5


@pytest.mark.parametrize("change, reason", [("process", "combat_control_context_changed"),
                                          ("roster", "combat_control_context_changed"),
                                          ("sequence", "combat_control_capture_not_advanced"),
                                          ("dead", "combat_character_unavailable")])
def test_changed_context_after_input_stops_without_further_input(change, reason):
    battle = Battle()

    def transform(value):
        if battle.presses:
            if change == "process": value["identity"]["process_token"] = "another-process"
            if change == "roster": value["combat"]["party"].pop()
            if change == "sequence": value["combat"]["source_sequence"] = 1
            if change == "dead": value["combat"]["party"][1]["hp"] = 0
        return value
    battle.transform = transform
    result = battle.switch()
    assert not result["ok"] and result["outcome"] == reason and len(battle.presses) == 1


@pytest.mark.parametrize("receipt, reason, sent", [
    ({"ok": False}, "combat_control_input_refused", False),
    (None, "combat_control_input_unverified", None),
])
def test_refusal_or_missing_receipt_is_not_replayed(receipt, reason, sent):
    battle = Battle()
    battle.tap_effect = lambda: receipt
    result = battle.switch()
    assert not result["ok"] and result["outcome"] == reason and result["input_sent"] is sent
    assert len(battle.presses) == 1 and battle.sequence == 1


def test_exception_after_input_is_preserved_as_uncertain():
    battle = Battle()

    def landed():
        battle.index = 1
        raise RuntimeError("synthetic failure after sending input")
    battle.tap_effect = landed
    result = battle.switch()
    assert not result["ok"] and result["outcome"] == "combat_control_interrupted"
    assert result["input_sent"] is None and result["input_outcome_unknown"] and len(battle.presses) == 1


def test_expired_wait_and_missing_hud_state_never_input():
    battle = Battle()
    battle.transform = lambda value: setattr(battle, "now", 8) or value
    assert battle.switch()["outcome"] == "combat_control_wait_expired" and battle.presses == []
    battle = Battle()
    battle.menu["party_status"] = []
    assert battle.switch()["outcome"] == "combat_menu_state_unobserved" and battle.presses == []


def test_locked_member_cycles_back_and_stops():
    battle = Battle()
    battle.tap_effect = lambda: setattr(battle, "index", 2 if battle.index == 0 else 0) or {"ok": True}
    result = battle.switch()
    assert not result["ok"] and result["outcome"] == "combat_character_unreachable" and len(battle.presses) == 2


def test_registered_operation_forwards_only_public_arguments(monkeypatch):
    from ff7r_agent.runtime_commands import invoke_runtime
    from ff7r_agent.service import AgentService
    seen = []
    monkeypatch.setattr("ff7r_agent.party_control.switch_combat_character",
                        lambda service, **kwargs: seen.append(kwargs) or {"ok": True, "outcome": "planned"})
    result = invoke_runtime(AgentService.__new__(AgentService), None, None,
                            "switch_combat_character", {"member": "Barret", "execute": False})
    assert result["ok"] and seen == [{"member": "Barret", "execute": False}]
