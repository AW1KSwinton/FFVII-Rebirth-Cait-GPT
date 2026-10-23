from __future__ import annotations

from ff7r_agent.battle_command import parse_command_menu, use_command

ROOT = "/Engine/Transient.GameEngine_2147482600:BP_EndGameInstance."
LIST_ID = ("EndVirtualScrollBox /Engine/Transient.GameEngine_2147482600:BP_EndGameInstance_C_2147482461."
           "Command_C_2147481452.WidgetTree.Command_Switching.WidgetTree.List_00.WidgetTree.List_00")
# Live 2026-10-20, Mt. Nibel (the Velociwing fight).
MENUS = {
    "Commands": (["Abilities", "Spells", "Items"], ["Use a powerful ability.", "Cast magic.", "Use an item."]),
    "Items": (["Mega-Potion", "Ether", "Phoenix Down"], ["Restores 1,500 HP.", "Restores MP.", "Revives."]),
    "Abilities": (["Braver"], ["Leap and strike."]),
    "Braver": (["Velociwing A", "Velociwing B"], ["Select a target.", "Select a target."]),
    "Spells": (["Fire", "Blizzard", "Cure"], ["Fire.", "Ice.", "Heal."]),
    "Fire": (["Velociwing A", "Velociwing B"], ["Select a target.", "Select a target."]),
    "Commands4": (["Abilities", "Spells", "Items", "Limit"], ["a", "s", "i", "l"]),
}


CELL = "Command_Cell_00_C_21474814%02d"


def snapshot(title, selected, order=None):
    """Texts in snapshot order (``order`` of drawn indexes), each owned by its cell."""
    entries, helps = MENUS[title]
    order = list(range(len(entries))) if order is None else order

    def text(path, value, owner=None):
        return {"path": path, "text": value, "owner": owner}

    texts = [text(ROOT + "Command.WidgetTree.Command_Switching.WidgetTree.List_00.WidgetTree.Txt_Title", title),
             text(ROOT + "Command.WidgetTree.Command_Help.WidgetTree.Txt_Help", helps[selected]),
             text(ROOT + "Command_Cell_00.WidgetTree.Txt_Command", "--", CELL % 90)]
    texts += [text(ROOT + "Command_Cell_00.WidgetTree.Txt_Command", entries[i], CELL % i) for i in order]
    texts.append(text(ROOT + "Command_Cell_00.WidgetTree.Txt_Command", "Cure", CELL % 91))  # a reused hidden cell
    lists = [{"id": LIST_ID, "visible": True, "selected_index": selected,
              "selected_child_id": "Command_Cell_00_C /Engine/Transient.GameEngine_2147482600:"
                                   "BP_EndGameInstance_C_2147482461." + CELL % selected,
              "properties": {"ItemCount": str(len(entries)), "DesiredCount": "6"}},
             {"id": "EndVirtualScrollBox x.ShortCut_List.WidgetTree.List_00", "visible": True, "selected_index": -1}]
    return texts, lists


def test_the_live_menu_is_read_with_its_native_highlight():
    menu = parse_command_menu(*snapshot("Items", 0))
    assert {k: menu[k] for k in ("open", "title", "entries", "selected_index", "selected", "help")} == {
        "open": True, "title": "Items", "entries": ["Mega-Potion", "Ether", "Phoenix Down"],
        "selected_index": 0, "selected": "Mega-Potion", "help": "Restores 1,500 HP."}
    # Live: with Limit ready the texts came out of drawn order.
    menu = parse_command_menu(*snapshot("Commands4", 2, order=[0, 1, 3, 2]))
    assert menu["selected"] == "Items"
    texts, lists = snapshot("Commands", 1)
    lists[0]["selected_index"] = -1
    assert parse_command_menu(texts, lists)["open"] is False


class Menu:
    """The command menu as seen live: lists remember their highlight."""

    def __init__(self, atb=True):
        self.stack = []
        self.memory = {"Commands": 2, "Items": 0, "Abilities": 0, "Spells": 0, "Fire": 1, "Braver": 1}
        self.atb = atb
        self.braver_targets = False
        self.taps = []
        self.used = []

    def read(self):
        if not self.stack:
            return parse_command_menu([], [])
        title = self.stack[-1]
        return parse_command_menu(*snapshot(title, self.memory[title]))

    def tap(self, button):
        self.taps.append(button)
        if button == "battle_command_menu":
            self.stack = ["Commands"]
            return
        title = self.stack[-1]
        count = len(MENUS[title][0])
        if button == "down":
            self.memory[title] = min(count - 1, self.memory[title] + 1)
        elif button == "up":
            self.memory[title] = max(0, self.memory[title] - 1)
        elif button == "cancel":
            self.stack.pop()
        elif button == "confirm":
            entry = MENUS[title][0][self.memory[title]]
            if entry in MENUS and title not in ("Fire", "Braver") and (entry != "Braver" or self.braver_targets):
                self.stack.append(entry)
            elif self.atb:
                self.used.append(entry)
                self.stack = []


def test_an_item_is_used_by_name_from_a_remembered_highlight():
    menu = Menu()
    result = use_command(menu.read, menu.tap, lambda s: None, ["Items", "Mega-Potion"], True)
    assert result["ok"] and result["outcome"] == "menu_closed_after_command" and menu.used == ["Mega-Potion"]
    assert menu.taps == ["battle_command_menu", "confirm", "confirm"]  # Items was still highlighted
    menu = Menu()
    result = use_command(menu.read, menu.tap, lambda s: None, ["Abilities", "Braver"], True)
    # Down first; at the end of the list the highlight stops and it turns back.
    assert result["ok"] and menu.taps == ["battle_command_menu", "down", "up", "up", "confirm", "confirm"]
    assert [step["entry"] for step in result["steps"]] == ["Abilities", "Braver"]


def test_an_unavailable_command_is_reported_and_the_menu_closed():
    menu = Menu(atb=False)
    result = use_command(menu.read, menu.tap, lambda s: None, ["Items", "Mega-Potion"], True)
    assert result["ok"] is False and result["outcome"] == "command_not_accepted"
    assert menu.stack == [] and menu.used == []


def test_an_unknown_entry_presses_nothing_more_and_backs_out():
    menu = Menu()
    result = use_command(menu.read, menu.tap, lambda s: None, ["Items", "Hi-Potion"], True)
    assert result["outcome"] == "entry_not_found" and result["entries"] == ["Mega-Potion", "Ether", "Phoenix Down"]
    assert menu.stack == [] and menu.used == []


def test_without_execute_nothing_is_pressed():
    menu = Menu()
    result = use_command(menu.read, menu.tap, lambda s: None, ["Items", "Mega-Potion"], False)
    assert result["outcome"] == "dry_run" and menu.taps == []


def test_the_operations_are_registered_with_the_runtime():
    from ff7r_agent.runtime_commands import SERVICE_ALIASES

    assert SERVICE_ALIASES["use_battle_command"] == "use_battle_command"
    assert SERVICE_ALIASES["read_battle_command_menu"] == "read_battle_command_menu"


def test_a_spell_is_cast_on_a_named_target_and_stale_cells_are_ignored():
    texts, lists = snapshot("Fire", 1)
    assert parse_command_menu(texts, lists)["entries"] == ["Velociwing A", "Velociwing B"]
    lists[0]["selected_child_id"] = None
    assert parse_command_menu(texts, lists)["selected"] is None  # no cell, no verified highlight
    menu = Menu()
    result = use_command(menu.read, menu.tap, lambda s: None, ["Spells", "Fire", "Velociwing"], True)
    # "Velociwing" names either lettered target; the highlighted one is taken.
    assert result["ok"] and menu.used == ["Velociwing B"]
    assert menu.taps == ["battle_command_menu", "down", "up", "confirm", "confirm", "confirm"]


def test_the_highlight_is_followed_by_its_cell_whatever_the_text_order():
    state = {"sel": 0, "taps": []}

    def read():
        return parse_command_menu(*snapshot("Commands4", state["sel"], order=[0, 1, 3, 2]))

    def tap(button):
        state["taps"].append(button)
        if button == "down":
            state["sel"] = min(3, state["sel"] + 1)
        elif button == "up":
            state["sel"] = max(0, state["sel"] - 1)
        elif button == "confirm":
            state["confirmed"] = MENUS["Commands4"][0][state["sel"]]

    use_command(read, tap, lambda s: None, ["Limit"], True)
    assert state["confirmed"] == "Limit" and state["taps"][:4] == ["down", "down", "down", "confirm"]
    state.update(sel=3, taps=[])
    use_command(read, tap, lambda s: None, ["Spells"], True)
    assert state["confirmed"] == "Spells" and state["taps"][:4] == ["down", "up", "up", "confirm"]


def test_a_target_list_after_the_last_entry_takes_the_named_target_or_the_lock():
    menu = Menu()
    menu.braver_targets = True
    result = use_command(menu.read, menu.tap, lambda s: None, ["Abilities", "Braver"], True)
    assert result["ok"] and result["target"] == "Velociwing B" and menu.used == ["Velociwing B"]
    menu = Menu()
    menu.braver_targets = True
    result = use_command(menu.read, menu.tap, lambda s: None, ["Abilities", "Braver"], True, target="Velociwing A")
    assert result["ok"] and menu.used == ["Velociwing A"]
    assert [step["entry"] for step in result["steps"]] == ["Abilities", "Braver", "Velociwing A"]



def test_a_telegraphed_enemy_attack_holds_the_command():
    menu = Menu()
    skill = {"path": ROOT + "SkillName_Enemy.WidgetTree.Txt_SkillName", "text": "Charge", "owner": None}
    read = lambda: parse_command_menu([skill], [])
    result = use_command(read, menu.tap, lambda s: None, ["Spells", "Fire"], True, hold_on_enemy_skill=True)
    assert result["outcome"] == "enemy_attack_incoming" and result["enemy_skill"] == "Charge" and menu.taps == []
    result = use_command(menu.read, menu.tap, lambda s: None, ["Items", "Mega-Potion"], True, hold_on_enemy_skill=True)
    assert result["ok"] and menu.used == ["Mega-Potion"]
