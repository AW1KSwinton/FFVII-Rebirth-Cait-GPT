"""Menu-driven loadout changes against a simulated menu and save region.

Synthetic data only: the layouts mirror the live 2026-10-24 discoveries
documented in loadout_save, but no value here is a live capture.
"""
from __future__ import annotations

import struct

import pytest

from ff7r_agent import loadout_actions, loadout_save
from ff7r_agent.loadout_actions import LoadoutMenu, change_loadout
from ff7r_agent.loadout_save import SaveSnapshot

CLOUD = "$menu_partyname_0000_0002"


def _region(**ints: int) -> bytearray:
    region = bytearray(loadout_save.REGION_SIZE)
    for offset, value in ints.items():
        struct.pack_into("<i", region, int(offset, 16), value)
    return region


def _put(region: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<i", region, offset, value)


def _cloud(field: int) -> int:
    return loadout_save.MEMBER_RECORDS + field


class FakeField:
    """FieldMenu's surface: presses go to the simulated game."""

    def __init__(self, game: "FakeGame") -> None:
        self.game = game
        self.presses: list[str] = []

    def press(self, button: str) -> None:
        self.presses.append(button)
        self.game.press(button)

    def in_menu(self) -> bool:
        return self.game.screen != "field"

    def read(self) -> dict:
        return {"main_menu_item": "materia_equipment" if self.game.screen == "main" else None}

    def select_main(self, screen: dict, item: str) -> dict:
        assert item == "materia_equipment"
        return {"main_menu_item": item}

    def close(self) -> bool:
        self.game.screen = "field"
        return True


class FakeGame:
    def __init__(self) -> None:
        self.screen = "field"
        self.region = _region()
        for slot in range(8):
            _put(self.region, _cloud(loadout_save.WEAPON_SOCKETS) + 4 * slot, -1)
            _put(self.region, _cloud(loadout_save.ARMOR_SOCKETS) + 4 * slot, -1)
        _put(self.region, _cloud(0x9C), 300001)
        _put(self.region, _cloud(loadout_save.WEAPON_SOCKETS), 600029)
        self.weapon = loadout_save.WEAPON_RECORDS + 1 * loadout_save.WEAPON_SIZE
        _put(self.region, self.weapon + 0x18, 300001)
        _put(self.region, self.weapon + loadout_save.SKILL_SLOTS_OFFSET, 87055443)
        self.gear = {"weapon": "$Item_W_TSW_0001", "armor": "$Item_E_ARM_10000", "accessory": "$Item_E_ACC_0003"}
        self.weapons = ["$Item_W_TSW_0001", "$Item_W_TSW_0101", "$Item_W_TSW_0106"]
        self.copies = {"$Item_W_TSW_0001": 300001, "$Item_W_TSW_0101": 300009, "$Item_W_TSW_0106": 300010}
        self.skills = ["$menu_WeaponUpgrade_Core_name_W_TSW_0001_00", "$menu_WeaponUpgrade_Core_name_HPUP_200",
                       "$menu_WeaponUpgrade_Core_name_MPUP_003"]
        self.skill_ids = {self.skills[0]: 11, self.skills[1]: 87055443, self.skills[2]: 565974986}
        self.slot_skill = self.skills[1]
        self.materia = [("600010", "M_MAG_001"), ("600029", "M_SUP_001"), ("600043", "M_SUP_007")]
        for copy, _ in self.materia:
            record = loadout_save.RECORD_OFFSET + (int(copy) - 600000) * 32
            _put(self.region, record, int(copy))
            _put(self.region, record + 0x10, 0x1001 if copy != "600029" else 0x0001)
        self.cursor = {}
        self.extra_write = None
        self.skip_owner = False
        self.prompting = None  # (question, selected, apply) while a Yes/No prompt is open

    def prompt(self):
        if not self.prompting:
            return None
        return {"text": self.prompting[0], "selected": self.prompting[1]}

    # -- input --------------------------------------------------------------
    def press(self, button: str) -> None:
        c = self.cursor
        if self.prompting:
            question, selected, apply = self.prompting
            if button in ("up", "down"):
                self.prompting = (question, "yes" if button == "up" else "no", apply)
            elif button == "confirm":
                self.prompting = None
                if selected == "yes":
                    apply()
                self.screen = "sockets"
            return
        if button == "menu":
            self.screen = "main"
        elif self.screen == "main" and button == "confirm":
            self.screen, c["member"] = "members", 0
        elif self.screen == "members" and button == "confirm":
            self.screen, c["category"] = "overview", 0
        elif self.screen == "overview" and button in ("up", "down"):
            c["category"] = max(0, min(2, c["category"] + (1 if button == "down" else -1)))
        elif self.screen == "overview" and button == "confirm":
            self.screen = "gear"
            c["gear"] = self.weapons.index(self.gear["weapon"])
        elif self.screen == "gear" and button in ("up", "down"):
            c["gear"] = max(0, min(len(self.weapons) - 1, c["gear"] + (1 if button == "down" else -1)))
        elif self.screen == "gear" and button == "confirm":
            item = self.weapons[c["gear"]]
            self.gear["weapon"] = item
            _put(self.region, _cloud(0x9C), self.copies[item])
            if self.extra_write:
                _put(self.region, *self.extra_write)
            self.screen = "overview"
        elif self.screen == "overview" and button == "menu_triangle":
            self.screen, c["row"], c["slot"] = "sockets", "weapon", 0
        elif self.screen == "sockets" and button in ("up", "down"):
            order = loadout_actions.ROW_ORDER
            c["row"] = order[max(0, min(2, order.index(c["row"]) + (1 if button == "down" else -1)))]
            c["slot"] = 0
        elif self.screen == "sockets" and button in ("dialogue_left", "dialogue_right"):
            c["slot"] = max(0, min(3, c["slot"] + (1 if button == "dialogue_right" else -1)))
        elif self.screen == "sockets" and button == "confirm":
            self.screen = "choices"
            c["choice"] = 1 if c["row"] == "weapon_skill" else 0
        elif self.screen == "choices" and button in ("up", "down"):
            size = len(self.skills) if c["row"] == "weapon_skill" else len(self.materia)
            c["choice"] = max(0, min(size - 1, c["choice"] + (1 if button == "down" else -1)))
        elif self.screen == "choices" and button == "confirm":
            if c["row"] == "weapon_skill":
                self.slot_skill = self.skills[c["choice"]]
                _put(self.region, self.weapon + loadout_save.SKILL_SLOTS_OFFSET + 4 * c["slot"],
                     self.skill_ids[self.slot_skill])
            else:
                copy = int(self.materia[c["choice"]][0])
                slot = c["slot"]

                def apply(copy=copy, slot=slot):
                    _put(self.region, _cloud(loadout_save.WEAPON_SOCKETS) + 4 * slot, copy)
                    if not self.skip_owner:
                        _put(self.region, loadout_save.RECORD_OFFSET + (copy - 600000) * 32 + 0x10, 0x0001)
                record = loadout_save.RECORD_OFFSET + (copy - 600000) * 32 + 0x11
                if self.region[record] not in (0x10, 0):
                    self.prompting = ("Tifa has this equipped. Proceed anyway?", "no", apply)
                    return
                apply()
            self.screen = "sockets"
        _put(self.region, loadout_save.CLOCK_OFFSET, len(self.cursor) + 1 + struct.unpack_from(
            "<i", self.region, loadout_save.CLOCK_OFFSET)[0])

    # -- reads --------------------------------------------------------------
    def view(self, screen: str) -> dict:
        if self.screen in ("field", "main"):
            return {"open": False, "reason": "loadout_menu_not_open"}
        c = self.cursor
        texts = {f"MemberEquipList.Equip_0{i + 1}.Txt_Title": self.gear[k]
                 for i, k in enumerate(("weapon", "armor", "accessory"))}
        lists = {}
        if self.screen == "members":
            lists["MemberList.SelectList"] = {"index": c["member"], "count": 7, "texts": [CLOUD]}
        if self.screen != "members":
            texts[loadout_actions.MEMBER_TITLE] = CLOUD
        if self.screen == "overview":
            lists[loadout_actions.CATEGORY_LIST] = {"index": c["category"], "count": 3, "texts": []}
        if self.screen == "gear":
            lists[loadout_actions.GEAR_LIST] = {"index": c["gear"], "count": len(self.weapons),
                                                "texts": ["$menu_itemcraft_0001_0001", self.weapons[c["gear"]]]}
        if self.screen == "sockets":
            lists[loadout_actions.SOCKET_ROWS[c["row"]]] = {"index": c["slot"], "count": 4, "texts": []}
            if c["row"] == "weapon_skill":
                texts[loadout_actions.INFO_TITLE] = self.slot_skill
            else:
                copy = struct.unpack_from("<i", self.region, _cloud(loadout_save.WEAPON_SOCKETS) + 4 * c["slot"])[0]
                item = dict(self.materia).get(str(copy))
                texts[loadout_actions.INFO_TITLE] = "$Item_" + item if item else None
        if self.screen == "choices":
            rows = self.skills if c["row"] == "weapon_skill" else [name for _, name in self.materia]
            lists[loadout_actions.CHOICE_LIST] = {"index": c["choice"], "count": len(rows),
                                                  "texts": [rows[c["choice"]]]}
        return {"open": True, "texts": texts, "lists": lists}

    def snapshot(self, process) -> SaveSnapshot:
        return SaveSnapshot(bytes(self.region), 0, "token", 0.0)


class FakeService:
    def __init__(self, game: FakeGame) -> None:
        self.game = game

    def get_queens_blood_run_status(self) -> dict:
        return {"loaded_save_verified": True, "loaded_save": {"process": {"pid": "1", "token": "token"}}}


@pytest.fixture
def rig(monkeypatch):
    game = FakeGame()
    monkeypatch.setattr(loadout_save, "capture", game.snapshot)
    monkeypatch.setattr("ff7r_agent.materia_memory.read_materia_inventory", lambda service: {
        "ok": True, "materia": [{"menu_index": i, "category": "standard", "instance_id": copy, "item_id": item,
                                 "ap_total": 0, "level": 1} for i, (copy, item) in enumerate(game.materia)]})
    menu = LoadoutMenu(FakeService(game), view=game.view, sleep=lambda _: None, field=FakeField(game),
                       prompt=game.prompt)
    return game, menu


def test_save_snapshot_decodes_member_weapon_and_names_changes():
    game = FakeGame()
    before = game.snapshot(None)
    assert before.member(0)["weapon"] == 300001
    assert before.member(0)["weapon_materia"][:2] == [600029, -1]
    assert before.weapon_skills(300001)[0] == 87055443
    _put(game.region, _cloud(0x9C), 300009)
    _put(game.region, loadout_save.CLOCK_OFFSET, 99)
    _put(game.region, 0x108C8, 16047)
    changes = loadout_save.diff(before, game.snapshot(None))
    assert [c["field"] for c in changes] == ["member.cloud.weapon", "combat_settings.shortcuts"]
    with pytest.raises(ValueError, match="loadout_weapon_record_mismatch"):
        before.weapon_skills(300002)


def test_snapshot_from_another_process_is_refused():
    one = SaveSnapshot(bytes(16), 0, "a", 0.0)
    with pytest.raises(ValueError, match="loadout_game_process_changed"):
        loadout_save.diff(one, SaveSnapshot(bytes(16), 0, "b", 0.0))


def test_equipment_change_is_verified_on_screen_and_in_the_save(rig):
    game, menu = rig
    result = change_loadout(FakeService(game), "equipment", member="cloud", execute=True, menu=menu,
                            category="weapon", item="$Item_W_TSW_0101")
    assert result["verified"] is True, result
    assert result["before"]["copy"] == 300001 and result["after"]["copy"] == 300009
    assert [c["field"] for c in result["save_changes"]] == ["member.cloud.weapon"]
    assert result["presses"] == menu.presses.__len__() and "confirm" in menu.presses


def test_unexpected_save_write_is_not_verified(rig):
    game, menu = rig
    game.extra_write = (0x50000, 7)
    result = change_loadout(FakeService(game), "equipment", member="cloud", execute=True, menu=menu,
                            category="weapon", item="$Item_W_TSW_0101")
    assert result["verified"] is False and result["outcome"] == "unexpected_save_changes"
    assert result["unexpected_save_changes"][0]["field"] == "unclassified"


def test_setup_marks_the_changes_own_screen_once(rig):
    game, menu = rig
    result = change_loadout(FakeService(game), "materia", member="cloud", execute=True, menu=menu,
                            row="weapon", slot=1, copy_id="600043")
    assert result["verified"] is True
    assert {"setup": "weapon socket 1"} in result["steps"]


def test_item_not_listed_stops_before_confirm(rig):
    game, menu = rig
    result = change_loadout(FakeService(game), "equipment", member="cloud", execute=True, menu=menu,
                            category="weapon", item="$Item_W_TSW_9999")
    assert result["outcome"] == "item_not_listed"
    assert menu.presses.count("confirm") == 3  # main menu, member and category, never the item
    assert game.gear["weapon"] == "$Item_W_TSW_0001"


def test_materia_copy_is_chosen_by_native_copy_id(rig):
    game, menu = rig
    result = change_loadout(FakeService(game), "materia", member="cloud", execute=True, menu=menu,
                            row="weapon", slot=1, copy_id="600043")
    assert result["verified"] is True, result
    assert result["after"]["socket"] == 600043 and result["before"]["socket"] == -1
    assert result["before"]["owner"] is None and result["after"]["owner"] == 0
    assert [c["field"] for c in result["save_changes"]] == ["member.cloud.weapon_materia.1", "materia.600043.state"]
    assert "dialogue_right" in menu.presses and result["materia"]["item_id"] == "M_SUP_007"


def test_materia_from_another_member_needs_the_named_holder(rig):
    game, menu = rig
    _put(game.region, loadout_save.RECORD_OFFSET + 43 * 32 + 0x10, 0x0201)  # 600043 held by Tifa (2)
    refused = change_loadout(FakeService(game), "materia", member="cloud", execute=True, menu=menu,
                             row="weapon", slot=1, copy_id="600043")
    assert refused["outcome"] == "materia_copy_equipped_elsewhere" and "confirm" not in menu.presses[-1:]
    menu.close()
    result = change_loadout(FakeService(game), "materia", member="cloud", execute=True, menu=menu,
                            row="weapon", slot=1, copy_id="600043", take_from="tifa")
    assert result["verified"] is True, result
    assert result["taken_from"] == "tifa" and {"prompt": "Tifa has this equipped. Proceed anyway?",
                                                "answer": "yes"} in result["steps"]
    assert menu.presses[-2:] == ["up", "confirm"]


def test_materia_owner_must_follow_the_copy(rig):
    game, menu = rig
    game.skip_owner = True
    result = change_loadout(FakeService(game), "materia", member="cloud", execute=True, menu=menu,
                            row="weapon", slot=1, copy_id="600043")
    assert result["verified"] is False and result["outcome"] == "save_readback_mismatch"


def test_weapon_skill_change_is_read_from_the_weapon_record(rig):
    game, menu = rig
    result = change_loadout(FakeService(game), "weapon_skill", member="cloud", execute=True, menu=menu,
                            slot=0, skill="$menu_WeaponUpgrade_Core_name_MPUP_003")
    assert result["verified"] is True, result
    assert result["before"]["skills"][0] == 87055443 and result["after"]["skills"][0] == 565974986
    assert [c["field"] for c in result["save_changes"]] == ["weapon.300001.skill.0"]


def test_plan_only_reads_the_save_without_input(rig):
    game, menu = rig
    result = change_loadout(FakeService(game), "equipment", member="cloud", execute=False, menu=menu)
    assert result["input_sent"] is False and result["loadout"]["weapon"] == 300001
    assert menu.presses == []


def test_a_refused_press_is_reported_not_raised(rig):
    game, menu = rig

    def refuse(button):
        raise RuntimeError("menu input blocked during loaded gameplay")
    menu.field.press = refuse
    result = change_loadout(FakeService(game), "equipment", member="cloud", execute=True, menu=menu,
                            category="weapon", item="$Item_W_TSW_0101")
    assert result["verified"] is False and result["outcome"] == "input_refused"


def test_unknown_member_is_refused(rig):
    game, menu = rig
    with pytest.raises(loadout_actions.LoadoutChangeError, match="unknown_party_member"):
        change_loadout(FakeService(game), "equipment", member="vincent", execute=True, menu=menu)


class FakeCombatSettings:
    """Combat Settings: member list, ground shortcuts, a category popup and
    entry list, and Edit Party's frontline/backline portraits."""

    POPUP, ENTRIES = "BattleSetting_List3.SelectList_1", "BattleSetting_List3.SelectList"

    def __init__(self) -> None:
        self.screen = "field"
        self.region = _region()
        self.hp = [4831, 5229, 4077, 3633, 4785, 3988, 4342]
        for member, hp in enumerate(self.hp):
            _put(self.region, loadout_save.MEMBER_RECORDS + member * 256 + loadout_save.MAX_HP, hp)
        self.party = {"frontline": [0, 1, 4], "backline": [2, 3, 5]}
        self.shortcuts = ["Braver", "Focused Thrust", "Triple Slash", ""]
        self.entries = ["$PC0000_AtkBurst01", "$M_CMD_001_atk001", "$M_CMD_001_atk002"]
        self.c = {}
        self._write_party()

    def _write_party(self) -> None:
        for line, start in (("frontline", loadout_save.FRONTLINE), ("backline", loadout_save.BACKLINE)):
            values = self.party[line] + [loadout_save.PARTY_EMPTY] * (loadout_save.PARTY_SLOTS - len(self.party[line]))
            self.region[start:start + loadout_save.PARTY_SLOTS] = bytes(values)

    def press(self, button: str) -> None:
        c, step = self.c, {"down": 1, "up": -1, "dialogue_right": 1, "dialogue_left": -1}.get(button)
        if button == "menu":
            self.screen = "main"
        elif self.screen == "main" and button == "confirm":
            self.screen, c["member"] = "members", 0
        elif self.screen == "members" and step:
            c["member"] = max(0, min(6, c["member"] + step))
        elif self.screen == "members" and button == "confirm":
            self.screen, c["slot"] = "shortcuts", 0
        elif self.screen == "members" and button == "menu_square":
            self.screen, c["line"], c["place"] = "party", "frontline", 0
        elif self.screen == "shortcuts" and step:
            c["slot"] = max(0, min(3, c["slot"] + step))
        elif self.screen == "shortcuts" and button == "confirm":
            self.screen, c["category"] = "popup", 0
        elif self.screen == "popup" and step:
            c["category"] = max(0, min(3, c["category"] + step))
        elif self.screen == "popup" and button == "confirm":
            self.screen, c["entry"] = "entries", 0
        elif self.screen == "entries" and step:
            c["entry"] = max(0, min(len(self.entries) - 1, c["entry"] + step))
        elif self.screen == "entries" and button == "confirm":
            self.shortcuts[c["slot"]] = self.entries[c["entry"]]
            _put(self.region, 0x108C8, 16047)
            self.screen = "shortcuts"
        elif self.screen == "party" and step:
            c["place"] = max(0, min(2, c["place"] + step))
        elif self.screen == "party" and button == "confirm":
            if c["line"] == "frontline":
                c["picked"], c["line"], c["place"] = c["place"], "backline", 0
            else:
                front, back = self.party["frontline"], self.party["backline"]
                front[c["picked"]], back[c["place"]] = back[c["place"]], front[c["picked"]]
                self._write_party()
                c["line"], c["place"] = "frontline", c["picked"]

    def view(self, screen: str) -> dict:
        c = self.c
        if self.screen in ("field", "main") or (screen == "member_change") != (self.screen == "party"):
            return {"open": False, "reason": "loadout_menu_inactive"}
        lists, texts = {}, {}
        if self.screen == "members":
            member = (0, 1, 4, 2, 3, 5, 6)[c["member"]]
            lists["MemberList.SelectList"] = {"index": c["member"], "count": 7,
                                              "texts": [f"$menu_partyname_{member:04d}_0002"]}
        if self.screen in ("shortcuts", "popup", "entries"):
            texts[loadout_actions.MEMBER_TITLE] = CLOUD
            lists[loadout_actions.SHORTCUT_LIST] = {"index": c["slot"], "count": 4,
                                                    "texts": [self.shortcuts[c["slot"]]]}
        if self.screen in ("popup", "entries"):
            lists[self.POPUP] = {"index": c["category"], "count": 4, "texts": [f"$menu_mainmenu_0007_000{c['category'] + 1}"]}
        if self.screen == "entries":
            lists[self.ENTRIES] = {"index": c["entry"], "count": len(self.entries), "texts": [self.entries[c["entry"]]]}
        if self.screen == "party":
            member = self.party[c["line"]][c["place"]]
            lists[loadout_actions.PARTY_LISTS[c["line"]]] = {"index": c["place"], "count": 3,
                                                             "texts": ["50", str(self.hp[member]), "255"]}
        return {"open": True, "texts": texts, "lists": lists}

    def snapshot(self, process) -> SaveSnapshot:
        return SaveSnapshot(bytes(self.region), 0, "token", 0.0)


@pytest.fixture
def combat(monkeypatch):
    game = FakeCombatSettings()
    monkeypatch.setattr(loadout_save, "capture", game.snapshot)
    field = FakeField(game)
    field.read = lambda: {"main_menu_item": "combat_settings" if game.screen == "main" else None}
    field.select_main = lambda screen, item: {"main_menu_item": item}
    menu = LoadoutMenu(FakeService(game), view=game.view, sleep=lambda _: None, field=field)
    return game, menu


def test_shortcut_is_set_through_its_category_list(combat):
    game, menu = combat
    result = change_loadout(FakeService(game), "shortcut", member="cloud", execute=True, menu=menu,
                            slot=4, entry="$M_CMD_001_atk001")
    assert result["verified"] is True, result
    assert game.shortcuts[3] == "$M_CMD_001_atk001"
    assert [c["field"] for c in result["save_changes"]] == ["combat_settings.shortcuts"]


def test_party_swap_binds_each_place_to_the_saved_party(combat):
    game, menu = combat
    result = change_loadout(FakeService(game), "party", member="red_xiii", execute=True, menu=menu, member_in="tifa")
    assert result["verified"] is True, result
    assert result["after"]["party"] == {"frontline": [0, 1, 2], "backline": [4, 3, 5]}
    assert "menu_square" in menu.presses and menu.presses.count("dialogue_right") == 2


def test_party_member_must_be_placed(combat):
    game, menu = combat
    result = change_loadout(FakeService(game), "party", member="tifa", execute=True, menu=menu, member_in="aerith")
    assert result["outcome"] == "party_members_not_placed" and menu.presses == []
