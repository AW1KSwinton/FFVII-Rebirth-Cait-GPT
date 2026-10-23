from __future__ import annotations

from types import SimpleNamespace

import pytest

from ff7r_agent import field_menu as fm

CLOUD, SEPHIROTH = fm.PARTY_KEYS["cloud"], fm.PARTY_KEYS["sephiroth"]
CURE, FIRE, BLIZZARD = fm.SPELL_KEYS["cure"], fm.SPELL_KEYS["fire"], fm.SPELL_KEYS["blizzard"]
MEGA, ETHER, PHOENIX = fm.ITEM_KEYS["mega-potion"], fm.ITEM_KEYS["ether"], fm.ITEM_KEYS["phoenix down"]
# The flashback's main menu, live 2026-10-21 (no transmuter, weapons or card decks).
MAIN = ["materia_equipment", "combat_settings", "enemy_intel", "spells", "inventory", "party",
        "manual", "save_load", "system"]


class Game:
    """The field menu as seen live on 2026-10-21 (Cloud 1/3736 HP after a fight)."""

    def __init__(self, cloud_hp=1, cloud_mp=25, cure_heal=1037):
        self.screen = "field"
        self.main = 0
        self.caster = 0
        self.spell = 0
        self.item = 0
        self.target = 0
        self.cure_heal = cure_heal
        self.party = [{"key": CLOUD, "hp": cloud_hp, "max_hp": 3736, "mp": cloud_mp, "max_mp": 53,
                       "spells": [CURE, FIRE, BLIZZARD]},
                      {"key": SEPHIROTH, "hp": 3911, "max_hp": 4259, "mp": 60, "max_mp": 60, "spells": [FIRE]}]
        self.items = [[MEGA, 9], [ETHER, 5], [PHOENIX, 5]]
        self.taps = []
        self.lag = 0  # reads for which a closed menu still reads as open
        self._stale = 0
        self.telemetry = SimpleNamespace(observe=self._observe)

    def _observe(self):
        if self._stale > 0:
            self._stale -= 1
            return SimpleNamespace(in_menu=True, paused=True)
        return SimpleNamespace(in_menu=self.screen != "field", paused=self.screen != "field")

    # The service's tap.
    def tap(self, button, milliseconds):
        self.taps.append(button)
        screen = self.screen
        moves = {"down": 1, "up": -1}
        if screen == "field" and button == "menu":
            self.screen = "main"
        elif button == "cancel":
            back = {"main": "field", "caster": "main", "spell": "caster", "target_spell": "spell",
                    "category": "main", "items": "category", "target_item": "items",
                    "field": "main"}  # Cancel in the field opens the menu
            self.screen = back.get(screen, screen)
            if self.screen == "field":
                self._stale = self.lag
        elif button in moves:
            step = moves[button]
            if screen == "main":
                self.main = (self.main + step) % len(MAIN)
            elif screen == "caster":
                self.caster = (self.caster + step) % len(self.party)
            elif screen == "spell":
                self.spell = (self.spell + step) % len(self.party[self.caster]["spells"])
            elif screen == "items":
                self.item = (self.item + step) % len(self.items)
            elif screen.startswith("target"):
                self.target = (self.target + step) % len(self.party)
        elif button == "confirm":
            if screen == "main" and MAIN[self.main] == "spells":
                self.screen, self.caster = "caster", 0
            elif screen == "main" and MAIN[self.main] == "inventory":
                self.screen = "category"
            elif screen == "caster":
                self.screen, self.spell = "spell", 0
            elif screen == "spell":
                self.screen, self.target = "target_spell", 0
            elif screen == "category":
                self.screen, self.item = "items", 0
            elif screen == "items":
                self.screen, self.target = "target_item", 0
            elif screen == "target_spell":
                caster, target = self.party[self.caster], self.party[self.target]
                if self.party[self.caster]["spells"][self.spell] == CURE and caster["mp"] >= 6:
                    caster["mp"] -= 6
                    target["hp"] = min(target["max_hp"], target["hp"] + self.cure_heal)
            elif screen == "target_item":
                key, count = self.items[self.item]
                target = self.party[self.target]
                if count > 0:
                    self.items[self.item][1] -= 1
                    if key == ETHER:
                        target["mp"] = min(target["max_mp"], target["mp"] + 20)
                    elif key == MEGA:
                        target["hp"] = min(target["max_hp"], target["hp"] + 1500)

    def read(self):
        screen = self.screen
        caster = self.party[self.caster]
        spell = caster["spells"][self.spell] if screen in ("spell", "target_spell") else None
        return {
            "main_menu_item": MAIN[self.main] if screen == "main" else None,
            "footer_help": fm.ITEMS_CATEGORY_HELP if screen == "category" else ("x" if screen != "field" else None),
            "spell_caster_key": caster["key"] if screen in ("caster", "spell", "target_spell") else None,
            "spell_highlighted": spell,
            "spell_mp_cost": 6 if spell == CURE else None,
            "spell_list_index": self.spell if screen == "spell" else None,
            "item_highlighted": self.items[self.item][0] if screen in ("items", "target_item") else None,
            "target_index": self.target if screen.startswith("target") else None,
            "party": [{"slot": index, "name_key": member["key"], "hp": member["hp"], "mp": member["mp"],
                       "max_hp": member["max_hp"], "max_mp": member["max_mp"]}
                      for index, member in enumerate(self.party)],
        }


def menu(game):
    return fm.FieldMenu(game, read=game.read, sleep=lambda seconds: None, clock=iter(range(10_000)).__next__)


def test_cure_heals_cloud_to_full_with_casts_back_to_back():
    """Live: each Cure from Cloud healed 1,037; 1 HP to full took four."""
    game = Game()
    result = fm.cast_field_spell(game, "Cure", menu=menu(game))
    assert result["ok"] and result["uses"] == 4 and result["per_use"] == 1037
    assert result["from"] == 1 and result["to"] == 3736 and game.party[0]["mp"] == 1
    assert result["menu_closed"] and game.screen == "field"
    # Three moves down to Spells, then caster, spell and target already highlighted.
    assert game.taps[:5] == ["menu", "down", "down", "down", "confirm"]


def test_a_cure_stops_at_the_hp_asked_for():
    game = Game()
    result = fm.cast_field_spell(game, "cure", until_hp=2000, menu=menu(game))
    assert result["ok"] and result["uses"] == 2 and game.party[0]["hp"] == 2075


def test_casts_never_exceed_the_casters_mp():
    game = Game(cloud_mp=13)
    result = fm.cast_field_spell(game, "cure", menu=menu(game))
    assert result["uses"] == 2 and result["reason"] == "stopped_short" and not result["ok"]


def test_cure_on_sephiroth_moves_the_target_cursor():
    game = Game()
    game.party[1]["hp"] = 2000
    result = fm.cast_field_spell(game, "cure", target="sephiroth", menu=menu(game))
    assert result["ok"] and game.party[1]["hp"] == 4259 and game.party[0]["hp"] == 1


def test_a_spell_with_no_field_use_or_no_mp_is_refused_before_casting():
    game = Game()
    assert fm.cast_field_spell(game, "fire", menu=menu(game))["reason"] == "spell_not_castable_here"
    game = Game(cloud_mp=1)
    result = fm.cast_field_spell(game, "cure", menu=menu(game))
    assert result["reason"] == "not_enough_mp" and game.party[0]["hp"] == 1 and game.screen == "field"


def test_unknown_names_are_refused_without_input():
    game = Game()
    with pytest.raises(fm.FieldMenuError, match="unknown_spell"):
        fm.cast_field_spell(game, "Curaga", menu=menu(game))
    assert game.taps == []


def test_ethers_restore_mp_to_the_amount_asked_for():
    """Live: two Ethers took Cloud from 1 to 41 MP (20 each)."""
    game = Game(cloud_mp=1)
    result = fm.use_field_item(game, "Ether", until_mp=41, menu=menu(game))
    assert result["ok"] and result["uses"] == 2 and game.party[0]["mp"] == 41
    assert game.items[1][1] == 3 and game.screen == "field"


def test_an_item_can_be_used_a_set_number_of_times():
    game = Game(cloud_mp=1)
    result = fm.use_field_item(game, "ether", count=1, menu=menu(game))
    assert result["ok"] and result["uses"] == 1 and game.party[0]["mp"] == 21


def test_a_mega_potion_heals_to_full_by_default():
    game = Game(cloud_hp=500)
    result = fm.use_field_item(game, "mega-potion", menu=menu(game))
    assert result["ok"] and result["uses"] == 3 and game.party[0]["hp"] == 3736


def test_the_live_texts_are_read_into_the_screen():
    texts = [
        ("Foot.WidgetTree.Txt_Help", "$help_message_0000_0006"),
        ("Magic.WidgetTree.Magic_SelectList.WidgetTree.Txt_Title", CLOUD),
        ("Magic.WidgetTree.EquipInfo.WidgetTree.Txt_Title", CURE),
        ("Magic.WidgetTree.EquipInfo.WidgetTree.Magic_Guide.WidgetTree.Txt_MpCost", "6"),
        ("Item_Cell.WidgetTree.Txt_Title", CURE),
        ("Status.WidgetTree.Status_00.WidgetTree.Txt_Name", CLOUD),
        ("Status.WidgetTree.Status_00.WidgetTree.Txt_HP_Num", "1"),
        ("Status.WidgetTree.Status_00.WidgetTree.Txt_MP_Num", "25"),
        ("Recover_Cell.WidgetTree.MemberList_CellContent.WidgetTree.Txt_Hp_Total", "3,736"),
        ("Recover_Cell.WidgetTree.MemberList_CellContent.WidgetTree.Txt_Title", CLOUD),
    ]
    lists = [{"id": "Member_List", "visible": True, "selected_index": 0,
              "owners": ["WidgetTree", "Recover_List", "WidgetTree"]}]
    screen = fm.parse_field_menu(texts, lists)
    assert screen["main_menu_item"] == "spells" and screen["spell_caster_key"] == CLOUD
    assert screen["spell_highlighted"] == CURE and screen["spell_mp_cost"] == 6
    assert screen["target_index"] == 0
    assert screen["party"] == [{"slot": 0, "name_key": CLOUD, "hp": 1, "mp": 25, "max_hp": 3736, "max_mp": None}]


def test_the_operations_are_registered_with_the_runtime():
    from ff7r_agent.runtime_commands import SERVICE_ALIASES

    for name in ("read_field_menu", "cast_field_spell", "use_field_item"):
        assert SERVICE_ALIASES[name] == name


def test_target_rows_pair_their_totals_by_name_not_position():
    """Live 2026-10-21: Sephiroth's row texts came first, and paired by position
    Cloud's max HP read 4,259."""
    cell = fm.RECOVER_CELL
    texts = [
        ("Status.WidgetTree.Status_00.WidgetTree.Txt_Name", CLOUD),
        ("Status.WidgetTree.Status_01.WidgetTree.Txt_Name", SEPHIROTH),
        (cell + "Txt_Mp_Total", "60"), (cell + "Txt_Hp_Total", "4259"), (cell + "Txt_Title", SEPHIROTH),
        (cell + "Txt_Mp_Total", "53"), (cell + "Txt_Hp_Total", "3736"), (cell + "Txt_Title", CLOUD),
    ]
    party = fm.parse_field_menu(texts, [])["party"]
    assert [(m["max_hp"], m["max_mp"]) for m in party] == [(3736, 53), (4259, 60)]


def test_closing_waits_out_a_lagging_menu_flag_and_does_not_reopen_it():
    """Live 2026-10-21: an extra Cancel landed in the field and reopened the menu."""
    game = Game()
    game.lag = 2
    result = fm.cast_field_spell(game, "cure", menu=menu(game))
    assert result["ok"] and result["menu_closed"] and game.screen == "field"
    assert game.taps[-4:] == ["cancel"] * 4
