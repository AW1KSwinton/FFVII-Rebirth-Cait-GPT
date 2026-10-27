from __future__ import annotations

import pytest

from ff7r_agent.input import ALLOWED_BUTTONS, DEFAULT_KEYMAP
from ff7r_agent.service import BATTLE_MENU_BUTTONS, AgentService, SafetyError


def test_the_commands_menu_key_is_the_battle_hud_glyph():
    assert "battle_command_menu" in ALLOWED_BUTTONS and DEFAULT_KEYMAP["battle_command_menu"] == "c"
    assert BATTLE_MENU_BUTTONS == {"battle_command_menu", "up", "down", "left", "right", "confirm", "cancel"}


@pytest.mark.parametrize("button", ["attack", "menu", "map", "dialogue_left"])
def test_other_buttons_are_not_battle_menu_presses(button):
    with pytest.raises(SafetyError, match="battle command menu"):
        AgentService.battle_menu_tap(object(), button)


def test_the_operation_is_registered_with_the_runtime():
    from ff7r_agent.runtime_commands import SERVICE_ALIASES

    assert SERVICE_ALIASES["battle_menu_tap"] == "battle_menu_tap"


def _menu_snapshots(monkeypatch, responses):
    import ff7r_agent.map_query as map_query

    asked = []

    def snapshot(service, *, heights, flags, require_map):
        asked.append(tuple(flags))
        return responses.pop(0), {}
    monkeypatch.setattr(map_query, "request_snapshot", snapshot)
    return asked


LIST = "EndListBox /Engine/Transient.X:BP.Command.WidgetTree.Command_Switching.WidgetTree.List_00.WidgetTree.List_00"


def _hud(entries, count, selected="Command_Cell_00_C_1"):
    texts = [{"path": "BP.Command_Cell_00.WidgetTree.Txt_Command", "text": name, "owner": f"Command_Cell_00_C_{i + 1}"}
             for i, name in enumerate(entries)]
    return {"texts_scope": "battle", "texts": texts,
            "save_lists": [{"id": LIST, "visible": True, "selected_index": 0,
                            "selected_child_id": f"Command_Cell_00_C /Engine/Transient.X:BP.{selected}", "properties": {"ItemCount": str(count)}}]}


def test_the_battle_menu_is_read_from_the_kept_hud_and_rescanned_when_cells_are_missing(monkeypatch):
    service = type("S", (), {})()
    asked = _menu_snapshots(monkeypatch, [_hud(["Abilities", "Spells", "Items"], 3)])
    menu = AgentService.read_battle_command_menu(service)
    assert asked == [("battlehud",)] and menu["entries"] == ["Abilities", "Spells", "Items"] and menu["selected"] == "Abilities"
    # A longer list than the kept cells: one forced scan, then none for a while.
    asked = _menu_snapshots(monkeypatch, [_hud(["Fire"], 4), _hud(["Fire", "Blizzard", "Thunder", "Aero"], 4),
                                          _hud(["Fire"], 4)])
    assert AgentService.read_battle_command_menu(service)["entries"] == ["Fire", "Blizzard", "Thunder", "Aero"]
    AgentService.read_battle_command_menu(service)
    assert asked == [("battlehud",), ("battlehud", "battlehudscan"), ("battlehud",)]


def test_an_exporter_without_the_battle_hud_read_gets_the_full_menu_read(monkeypatch):
    old = _hud(["Abilities"], 1)
    old.pop("texts_scope")
    asked = _menu_snapshots(monkeypatch, [{"texts": []}, old])
    assert AgentService.read_battle_command_menu(type("S", (), {})())["entries"] == ["Abilities"]
    assert asked == [("battlehud",), ("menutexts", "savecells")]
