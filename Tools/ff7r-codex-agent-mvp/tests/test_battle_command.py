from __future__ import annotations

import pytest

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


# Live 2026-10-22, the Materia Guardian: the fight starts with Sephiroth in Tactical Mode; E shows Cloud.
MEMBER_KEYS = ["$menu_partyname_0008_0002", "$menu_partyname_0000_0002"]
MENUS["Limit"] = (["Cross-Slash"], ["A Limit Break."])


class PartyMenu(Menu):
    """Tactical Mode naming the commanded member; Q/E (left/right) switch it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.member = 0
        self.commanded = []
        self.memory.update({"Commands4": 3, "Limit": 0})

    def read(self):
        if not self.stack:
            return parse_command_menu([], [])
        texts, lists = snapshot(self.stack[-1], self.memory[self.stack[-1]])
        texts.append({"path": ROOT + "Command.WidgetTree.Command_Switching.WidgetTree.Txt_Name",
                      "text": MEMBER_KEYS[self.member], "owner": None})
        return parse_command_menu(texts, lists)

    def tap(self, button):
        if button in ("left", "right"):
            self.taps.append(button)
            self.member = (self.member + (1 if button == "right" else -1)) % len(MEMBER_KEYS)
            self.stack = [self.stack[0]]
            return
        used = len(self.used)
        super().tap(button)
        if len(self.used) > used:
            self.commanded.append(MEMBER_KEYS[self.member])


def test_tactical_mode_switches_to_the_named_member_before_choosing():
    menu = PartyMenu()
    result = use_command(menu.read, menu.tap, lambda s: None, ["Items", "Mega-Potion"], True, member="Cloud")
    assert result["ok"] and result["member"] == "Cloud" and menu.used == ["Mega-Potion"]
    assert menu.taps[:2] == ["battle_command_menu", "right"] and menu.commanded == ["$menu_partyname_0000_0002"]
    assert result["top_entries"] == ["Abilities", "Spells", "Items"]
    menu = PartyMenu()
    result = use_command(menu.read, menu.tap, lambda s: None, ["Items", "Mega-Potion"], True, member="Sephiroth")
    assert result["ok"] and "right" not in menu.taps  # already commanded


def test_an_absent_member_is_reported_after_a_bounded_lap_and_the_menu_closed():
    menu = PartyMenu()
    result = use_command(menu.read, menu.tap, lambda s: None, ["Items", "Mega-Potion"], True, member="Tifa")
    assert result["outcome"] == "member_not_reached" and menu.taps.count("right") == 4
    assert menu.stack == [] and menu.used == []


def test_titan_party_switches_to_barret_and_aerith_from_live_name_keys(monkeypatch):
    # Slot 13, Full Might Titan, 2026-10-23: the HUD returns these keys,
    # rather than the displayed names. Previously only Cloud/Sephiroth resolved.
    monkeypatch.setitem(globals(), "MEMBER_KEYS", [
        "$menu_partyname_0000_0002", "$menu_partyname_0001_0002", "$menu_partyname_0003_0002",
    ])
    for member, key, switches in (("Barret", MEMBER_KEYS[1], 1), ("Aerith", MEMBER_KEYS[2], 2)):
        menu = PartyMenu()
        result = use_command(menu.read, menu.tap, lambda s: None, ["Items", "Mega-Potion"], True, member=member)
        assert result["ok"] and result["member"] == member
        assert menu.taps.count("right") == switches
        assert menu.commanded == [key] and menu.used == ["Mega-Potion"]


@pytest.mark.parametrize("member,index", [("Tifa", 0), ("Yuffie", 1), ("Red XIII", 2)])
def test_phoenix_party_reaches_each_member_from_native_name_keys(monkeypatch, member, index):
    monkeypatch.setitem(globals(), "MEMBER_KEYS", [
        "$menu_partyname_0002_0002", "$menu_partyname_0005_0002", "$menu_partyname_0004_0002",
    ])
    menu = PartyMenu()
    result = use_command(menu.read, menu.tap, lambda s: None, ["Items", "Mega-Potion"], True, member=member)
    assert result["ok"] and menu.used == ["Mega-Potion"]
    assert menu.taps.count("right") == index and menu.commanded == [MEMBER_KEYS[index]]


def test_phoenix_party_switches_caster_and_selects_red_xiii_as_healing_target(monkeypatch):
    monkeypatch.setitem(globals(), "MEMBER_KEYS", [
        "$menu_partyname_0002_0002", "$menu_partyname_0005_0002", "$menu_partyname_0004_0002",
    ])
    monkeypatch.setitem(MENUS, "Cure", (["Tifa", "Yuffie", "Red XIII"], ["Select a target."] * 3))
    menu = PartyMenu()
    menu.memory["Cure"] = 0
    result = use_command(menu.read, menu.tap, lambda s: None, ["Spells", "Cure"], True,
                         member="Yuffie", target="Red XIII")
    assert result["ok"] and menu.used == ["Red XIII"]
    assert menu.commanded == [MEMBER_KEYS[1]]


def test_range_spell_markup_is_not_part_of_the_name():
    from ff7r_agent.battle_command import names_match

    assert names_match("Thunder<icon=range>", "Thunder") and names_match("Thunder", "Thunder<icon=range>")
    assert not names_match("Thundara<icon=range>", "Thunder")


def test_the_status_panel_gives_each_members_hp_and_mp():
    from ff7r_agent.battle_command import parse_party_status

    status = ROOT + "Status.WidgetTree.Status_%s.WidgetTree.Txt_%s"
    texts = [{"path": status % ("00", "Name"), "text": "$menu_partyname_0000_0002"},
             {"path": status % ("00", "HP_Num"), "text": "3014"}, {"path": status % ("00", "MP_Num"), "text": "42"},
             {"path": status % ("01", "Name"), "text": "$menu_partyname_0008_0002"},
             {"path": status % ("01", "HP_Num"), "text": "3,599"}, {"path": status % ("01", "MP_Num"), "text": "60"},
             {"path": status % ("02", "HP"), "text": "HP"}]  # an empty slot
    assert parse_party_status(texts) == [{"slot": 0, "name": "Cloud", "hp": 3014, "mp": 42},
                                         {"slot": 1, "name": "Sephiroth", "hp": 3599, "mp": 60}]


def test_a_limit_break_is_taken_from_its_highlight_only_when_asked():
    menu = PartyMenu()
    menu.tap = lambda button, tap=menu.tap: (tap(button), menu.stack.__setitem__(0, "Commands4"))[0] \
        if button == "battle_command_menu" else tap(button)
    result = use_command(menu.read, menu.tap, lambda s: None, ["Limit"], True)
    assert result["outcome"] == "menu_still_open" and menu.used == []
    menu.stack = []
    result = use_command(menu.read, menu.tap, lambda s: None, ["Limit"], True, take_highlighted=True)
    assert result["ok"] and menu.used == ["Cross-Slash"]
    assert [step["entry"] for step in result["steps"]] == ["Limit", "Cross-Slash"]


def test_a_range_spells_target_list_is_recognised_by_its_marked_help_or_its_enemy(monkeypatch):
    # Live 2026-10-22: Fire's exact "Select a target." matched; Blizzard's marked line did not.
    monkeypatch.setitem(MENUS, "Blizzard", (["Materia Guardian"], ["<icon=range>Select a target. Range: area"]))
    menu = Menu()
    menu.memory["Blizzard"] = 0
    result = use_command(menu.read, menu.tap, lambda s: None, ["Spells", "Blizzard"], True)
    assert result["ok"] and menu.used == ["Materia Guardian"]
    monkeypatch.setitem(MENUS, "Blizzard", (["Materia Guardian"], ["Choose."]))
    menu = Menu()
    menu.memory["Blizzard"] = 0
    assert use_command(menu.read, menu.tap, lambda s: None, ["Spells", "Blizzard"], True)["outcome"] == "menu_still_open"
    menu = Menu()
    menu.memory["Blizzard"] = 0
    result = use_command(menu.read, menu.tap, lambda s: None, ["Spells", "Blizzard"], True, enemies=("Materia Guardian",))
    assert result["ok"] and menu.used == ["Materia Guardian"]


def test_magnified_spell_targets_titan_from_the_live_summon_actor_identity(monkeypatch):
    from ff7r_agent.combat_autopilot import enemy_name

    # Slot 13, Full Might Titan, 2026-10-23: this Magnify target help does
    # not start "Select a target". The SU actor must resolve to its HUD name.
    titan = enemy_name({
        "actor_id": "SU2000_00_Titan_Standard_C /Game/Level/Game/EndGame.EndGame:PersistentLevel.SU2000_00_Titan_Standard_C_2147469747",
        "chara_id": "SU2000_01",
    })
    monkeypatch.setitem(MENUS, "Fire", (["Titan"], ["Cast magic on up to three enemies within range."]))
    menu = Menu()
    menu.memory["Fire"] = 0
    result = use_command(menu.read, menu.tap, lambda s: None, ["Spells", "Fire"], True, enemies=(titan,))
    assert titan == "Titan" and result["ok"] and menu.used == ["Titan"]


def test_autopilot_can_prefer_the_live_child_target_without_overriding_explicit_targets(monkeypatch):
    targets = ["Titan", "<button=child-target>Earthen Aegis"]
    monkeypatch.setitem(MENUS, "Braver", (targets, ["Select a target."] * 2))
    menu = Menu()
    menu.braver_targets, menu.memory["Braver"] = True, 0
    result = use_command(menu.read, menu.tap, lambda s: None, ["Abilities", "Braver"], True,
                         prefer_child_target=True)
    assert result["ok"] and menu.used == [targets[1]]
    menu = Menu()
    menu.braver_targets, menu.memory["Braver"] = True, 0
    result = use_command(menu.read, menu.tap, lambda s: None, ["Abilities", "Braver"], True,
                         target="Titan", prefer_child_target=True)
    assert result["ok"] and menu.used == ["Titan"]
