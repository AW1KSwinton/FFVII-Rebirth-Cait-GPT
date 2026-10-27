"""Virtualized synthetic inventory; no bridge or real game input."""
import pytest

from ff7r_agent.loadout_scan import scan_loadout_choices


class Menu:
    def __init__(self, index=2, count=4):
        self.index, self.count = index, count
        self.now, self.reads = 0, 0
        self.presses = []
        self.transform = lambda value: value
        self.tap_effect = None

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def tap(self, button, milliseconds):
        assert button in ("up", "down") and milliseconds == 120
        self.presses.append(button)
        if self.tap_effect:
            return self.tap_effect(button)
        else:
            self.index += -1 if button == "up" else 1
            return {"ok": True}

    def read(self):
        self.reads += 1
        owner = "Equip_C /Engine/Transient.Equip_C_42"
        value = {"ok": True, "capture": {"source_sequence": self.reads, "source": "synthetic",
                  "map_id": "synthetic-map"}, "loadout_scope": {"owner_id": owner},
                 "selection": {"list_id": "EndVirtualScrollBox /Engine/Transient.Equip_C_42.List",
                  "selection_identity_observed": True, "properties": {"ItemCount": str(self.count)},
                  "selected_index": self.index,
                  # Deliberately reuse widgets for different logical copies.
                  "selected_child_id": f"Item_Cell_C /Temp/Row{self.index % 2}",
                  "selected_texts": [{"path": "Item_Cell.WidgetTree.Txt_Title", "text": f"Materia {self.index}",
                                      "rendered": True},
                                     {"path": "Item_Cell.WidgetTree.Txt_Ap_Vol", "text": str(self.index * 100),
                                      "rendered": True}]}, "evidence": {"texts": []}}
        return self.transform(value)

    def scan(self, **kwargs):
        return scan_loadout_choices(self, read=self.read, clock=self.clock, sleep=self.sleep, **kwargs)


def test_plan_does_not_move_cursor():
    menu = Menu()
    result = menu.scan()
    assert result["ok"] and not result["execute"] and not result["input_sent"]
    assert result["cursor_moves_at_most"] == 5 and menu.presses == [] and menu.reads == 1


def test_virtualized_rows_are_collected_by_observed_index_and_ap_without_equipping():
    menu = Menu()
    result = menu.scan(execute=True)
    assert result["ok"] and result["complete"] and not result["persistent_copy_ids"]
    assert [r["index"] for r in result["choices"]] == [0, 1, 2, 3]
    assert [r["texts"][1]["text"] for r in result["choices"]] == ["0", "100", "200", "300"]
    assert len({r["widget_id_at_capture"] for r in result["choices"]}) == 2
    assert set(menu.presses) == {"up", "down"} and len(menu.presses) == 5
    assert not result["loadout_changed"] and menu.index == 3


@pytest.mark.parametrize("kwargs", [{"execute": "yes"}, {"stage": []}, {"stage": "equipment_category"},
                                    {"max_choices": True}, {"max_choices": 65}, {"max_choices": 0},
                                    {"timeout_seconds": float("nan")}, {"timeout_seconds": True},
                                    {"timeout_seconds": 121}])
def test_invalid_scan_does_not_read_or_move(kwargs):
    menu = Menu()
    with pytest.raises(ValueError):
        menu.scan(**kwargs)
    assert menu.reads == 0 and menu.presses == []


@pytest.mark.parametrize("failure, reason", [
    ("exit", "loadout_menu_not_open"), ("owner", "loadout_scan_context_changed"),
    ("count", "loadout_choice_count_changed"), ("sequence", "loadout_scan_capture_not_advanced"),
    ("timeout", "map_query_timeout"),
])
def test_interruption_keeps_partial_evidence_and_sends_no_further_input(failure, reason):
    menu = Menu()

    def transform(value):
        if menu.reads > 1:
            if failure in ("exit", "timeout"):
                return {"ok": False, "reason": "loadout_menu_not_open" if failure == "exit" else "map_query_timeout"}
            if failure == "owner":
                value["loadout_scope"]["owner_id"] = "Equip_C /Engine/Transient.Replaced"
            if failure == "count":
                value["selection"]["properties"]["ItemCount"] = "5"
            if failure == "sequence":
                value["capture"]["source_sequence"] = 1
        return value
    menu.transform = transform
    result = menu.scan(execute=True)
    assert not result["ok"] and not result["complete"] and result["reason"] == reason
    assert len(result["choices"]) == 1 and len(menu.presses) == 1 and menu.reads == 2


def test_stalled_cursor_has_a_bounded_wait_and_no_input_replay():
    menu = Menu()
    menu.tap_effect = lambda button: {"ok": True}
    result = menu.scan(execute=True)
    assert not result["ok"] and result["reason"] == "loadout_cursor_stalled"
    assert len(menu.presses) == 1 and menu.reads <= 7 and menu.now <= 3


def test_exception_after_a_landed_tap_is_not_retried():
    menu = Menu()

    def landed(button):
        menu.index -= 1
        raise RuntimeError("synthetic transport failure after input")
    menu.tap_effect = landed
    result = menu.scan(execute=True)
    assert not result["ok"] and result["reason"] == "loadout_scan_interrupted"
    assert menu.index == 1 and len(menu.presses) == 1 and len(result["choices"]) == 1
    assert result["input_sent"] is None and result["input_outcome_unknown"]


def test_unexpected_cursor_jump_stops_without_guessing():
    menu = Menu()
    menu.tap_effect = lambda button: setattr(menu, "index", 0) or {"ok": True}
    result = menu.scan(execute=True)
    assert not result["ok"] and result["reason"] == "loadout_cursor_unexpected" and len(menu.presses) == 1


def test_changed_row_data_on_revisit_invalidates_scan():
    menu = Menu()

    def changed(value):
        if menu.reads >= 4:
            value["selection"]["selected_texts"][0]["text"] = "different inventory"
        return value
    menu.transform = changed
    result = menu.scan(execute=True)
    assert not result["ok"] and result["reason"] == "loadout_choices_changed"
    assert len(menu.presses) == 3 and len(result["choices"]) == 3


def test_expired_scan_and_oversized_list_never_start_input():
    menu = Menu(count=65)
    result = menu.scan(execute=True)
    assert not result["ok"] and result["reason"] == "loadout_choice_budget" and menu.presses == []
    menu = Menu()
    menu.transform = lambda value: setattr(menu, "now", 2) or value
    result = menu.scan(execute=True, timeout_seconds=1)
    assert not result["ok"] and result["reason"] == "loadout_scan_expired" and menu.presses == []


def test_existing_async_field_menu_action_dispatches_scan(monkeypatch):
    from ff7r_agent.service import AgentService
    seen = []
    monkeypatch.setattr("ff7r_agent.loadout_scan.scan_loadout_choices",
                        lambda service, **kwargs: seen.append(kwargs) or {"ok": True, "input_sent": False})
    service = AgentService.__new__(AgentService)
    result = service.run_field_menu("scan_loadout_choices", execute=False, stage="materia_inventory")
    assert result["ok"] and seen == [{"execute": False, "stage": "materia_inventory"}]


@pytest.mark.parametrize("receipt, reason, sent", [
    ({"ok": False, "reason": "synthetic_refusal"}, "loadout_input_refused", False),
    (None, "loadout_input_unverified", None),
])
def test_refused_or_unverified_input_is_not_replayed(receipt, reason, sent):
    menu = Menu()
    menu.tap_effect = lambda button: receipt
    result = menu.scan(execute=True)
    assert not result["ok"] and result["reason"] == reason and result["input_sent"] is sent
    assert len(menu.presses) == 1 and menu.reads == 1


def test_rpc_rejects_private_dependency_injection_arguments():
    from ff7r_agent.service import AgentService, SafetyError
    service = AgentService.__new__(AgentService)
    with pytest.raises(SafetyError, match="unsupported loadout scan arguments"):
        service.run_field_menu("scan_loadout_choices", read="arbitrary")
