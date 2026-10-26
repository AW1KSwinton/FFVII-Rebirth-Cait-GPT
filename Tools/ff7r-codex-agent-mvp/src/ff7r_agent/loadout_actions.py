"""Change gear, Materia, weapon skills and Combat Settings through the menus.

Every change goes through the game's own screens (Main Menu > Materia &
Equipment, or Combat Settings), one press at a time. Before the single
Confirm that applies it, the highlighted member, row and entry are read back
from the screen; after it, the screen shows the result and a diff of the
resident save data (loadout_save) must show the intended field changed and
nothing outside the change's own fields. The menu behavior is:

* Materia & Equipment opens on a member list; Confirm shows the member's
  overview, whose category list (weapon, armour, accessory) opens each gear
  list. Equipping the highlighted gear returns to the overview. Equipping a
  weapon over one with an auto-upgrade setting asks "Retain upgrade setting
  from the previous weapon?" (answered No unless asked otherwise).
* Taking a Materia copy another member has equipped asks "<member> has this
  equipped. Proceed anyway?", with No preselected. It is answered Yes only
  when the save data shows that copy on the member the caller named.
* Editing the skills of a weapon with an auto-upgrade setting first asks
  "Disable the automatic weapon skill upgrade function?" (Yes preselected);
  it is answered Yes only when the caller asks to disable it.
  These prompts are Com_Window_01 (Btn_00 Yes, Btn_01 No); the selected
  button's inner widget reports hovered (save_slot_save), which is read
  back before the Confirm.
* Triangle (R) on the overview is Set Materia: rows for the weapon's
  sockets, the armour's sockets and the weapon skills, moved between with
  Up/Down and along with the arrow keys. Confirm on a socket opens the
  Materia list, whose rows are the native copy array (materia_memory), so a
  copy is chosen by its save-scoped copy ID; on a weapon-skill slot it opens
  the weapon's skills, chosen by their text keys.

* Combat Settings opens on the same kind of member list. Confirm shows the
  member's shortcuts; Confirm on a ground shortcut offers Abilities, Spells,
  Items and Limit Breaks, each a list of text keys (``$M_CMD_001_atk001``
  is Steal). Air shortcuts follow the ground ones while Auto-Equip is on.
* Square (G) there is Edit Party: a frontline and a backline list of member
  portraits moved along with the arrow keys. Confirm on a frontline member,
  then on a backline one, swaps them. The portraits carry no names and show
  current stats (a Max HP weapon skill raises them; the save records keep
  base values), so each list place is bound to the save data's party lists,
  which the window shows in the same order.

The menu is left open on the screen that shows the result; ``close_menu``
closes it afterwards.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from . import loadout_save
from .field_menu import FieldMenu, FieldMenuError
from .loadout_menu import read_loadout_menu
from .map_query import request_snapshot

WAIT_SECONDS = 3.0
# The first main-menu open after a load took longer than field_menu's 3 s
# to show a readable cursor (live 2026-10-24).
MAIN_MENU_SECONDS = 8.0
POLL_SECONDS = 0.25
MAX_LIST_MOVES = 64
PARTY_KEYS = {name: f"$menu_partyname_{index:04d}_0002" for index, name in enumerate(loadout_save.MEMBER_NAMES)}
CATEGORIES = ("weapon", "armor", "accessory")
# Set Materia's rows, top to bottom.
SOCKET_ROWS = {"weapon": "Materia_List_All.Materia_List1.Materia_List_Box",
               "armor": "Materia_List_All.Materia_List2.Materia_List_Box",
               "weapon_skill": "Materia_List_All.Core_List.Core_List_Box"}
ROW_ORDER = ("weapon", "armor", "weapon_skill")
MEMBER_LIST = "MemberList.SelectList"
CATEGORY_LIST = "Equip_CategoryList.EquipCategoryList"
GEAR_LIST = "EquipList.SelectList"
CHOICE_LIST = "Materia_SelectList.SelectList"
GEAR_TITLES = {"weapon": "MemberEquipList.Equip_01.Txt_Title", "armor": "MemberEquipList.Equip_02.Txt_Title",
               "accessory": "MemberEquipList.Equip_03.Txt_Title"}
INFO_TITLE = "EquipInfo.Txt_Title"
MEMBER_TITLE = "CategoryTitle_Member.Txt_MainTitle"
ROOTS = {"equipment": ".Equip.WidgetTree.", "battle_setting": ".BattleSetting.WidgetTree.",
         "member_change": "MemberChange.WidgetTree."}
SHORTCUT_LIST = "BattleSetting_MainList.SettingList"
SHORTCUT_CATEGORIES = ("abilities", "spells", "items", "limit_breaks")
PARTY_LISTS = {"frontline": "List_1.BtnList", "backline": "List_2.BtnList"}
ARROWS = ("dialogue_left", "dialogue_right")


class LoadoutChangeError(RuntimeError):
    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


def _tail(path: str, root: str) -> str:
    at = path.rfind(root)
    rest = path[at + len(root):] if at >= 0 else path
    return rest.replace("WidgetTree.", "")


def read_view(service: Any, screen: str = "equipment") -> dict[str, Any]:
    """The open screen's texts and selected list rows, keyed by widget path
    below its root (``MemberEquipList.Equip_01.Txt_Title``); no input."""
    result = read_loadout_menu(service, raw=True, screen=screen)
    if not result.get("ok"):
        return {"open": False, "reason": result.get("reason")}
    owner = str(result["loadout_scope"]["owner_id"]).partition(" ")[2]
    evidence = result["evidence"]
    texts: dict[str, str] = {}
    for row in evidence["texts"]:
        texts.setdefault(_tail(str(row.get("path", "")), ROOTS[screen]), str(row.get("text", "")))
    lists: dict[str, dict[str, Any]] = {}
    for row in evidence["lists"]:
        name = str(row.get("id", "")).partition(" ")[2]
        if not name.startswith(owner + "."):
            continue
        count = str((row.get("properties") or {}).get("ItemCount", ""))
        index = row.get("selected_index")
        lists[name[len(owner) + 1:].replace("WidgetTree.", "")] = {
            "index": index if type(index) is int else None,
            "count": int(count) if count.isdigit() else None,
            "texts": [str(text.get("text", "")) for text in row.get("selected_texts") or [] if isinstance(text, dict)],
        }
    return {"open": True, "texts": texts, "lists": lists, "sequence": result["capture"]["source_sequence"],
            "evidence_sha256": result["evidence_sha256"]}


def _selected(view: dict[str, Any], key: str) -> dict[str, Any] | None:
    row = (view.get("lists") or {}).get(key)
    return row if row and isinstance(row.get("index"), int) and row["index"] >= 0 else None


def _summary(view: dict[str, Any]) -> dict[str, Any]:
    if not view.get("open"):
        return {"open": False, "reason": view.get("reason")}
    return {"selected": {key: {"index": row["index"], "texts": row["texts"][-2:]}
                         for key, row in view["lists"].items() if isinstance(row.get("index"), int) and row["index"] >= 0},
            "member": view["texts"].get(MEMBER_TITLE), "info": view["texts"].get(INFO_TITLE)}


# Each prompt labels its buttons with its own keys (the auto-upgrade one
# says $menu_mainmenu_weaponstatus_auto_release_yes/_no).
PROMPT_BUTTONS = {"yes": ("Btn_00", ("$menu_mainmenu_0009_0001",), "_yes"),
                  "no": ("Btn_01", ("$menu_mainmenu_0009_0002",), "_no")}


def read_prompt(service: Any) -> dict[str, Any] | None:
    """The open Yes/No prompt: its text and which button is selected; no input."""
    response, _ = request_snapshot(service, heights=False, flags=("menutexts", "savecells"), require_map=False)
    dialogs = [d for d in response.get("save_dialogs") or [] if isinstance(d, dict)]
    if len(dialogs) != 1:
        return None
    nodes = [n for n in dialogs[0].get("nodes") or [] if isinstance(n, dict) and n.get("visible") is True]
    texts = [str(n.get("text")) for n in nodes if n.get("kind") == "text" and n.get("text")]
    selected, labels = set(), {}
    for answer, (button, keys, suffix) in PROMPT_BUTTONS.items():
        inside = [n for n in nodes if f".{button}.WidgetTree." in str(n.get("full_id", ""))]
        labels[answer] = any(n.get("text") in keys or str(n.get("text", "")).endswith(suffix) for n in inside)
        if any(n.get("hovered") is True or n.get("keyboard_focus") is True for n in inside):
            selected.add(answer)
    if not all(labels.values()):
        return None
    question = next((t for t in texts if not t.startswith("$")), "")
    return {"text": question, "selected": selected.pop() if len(selected) == 1 else None}


class LoadoutMenu:
    """Presses with a screen read before each next step."""

    def __init__(self, service: Any, *, view: Callable[[str], dict[str, Any]] | None = None,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
                 field: FieldMenu | None = None,
                 prompt: Callable[[], dict[str, Any] | None] | None = None) -> None:
        self.service = service
        self.view = view or (lambda screen: read_view(service, screen))
        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic
        self.field = field or FieldMenu(service, sleep=self.sleep, clock=self.clock)
        self.prompt = prompt or (lambda: read_prompt(service))
        self.steps: list[dict[str, Any]] = []

    def setup(self, where: str) -> None:
        """The change's own screen is showing its before state."""
        self.steps.append({"setup": where})

    @property
    def presses(self) -> list[str]:
        return self.field.presses

    def press(self, button: str, why: str) -> None:
        self.field.press(button)
        self.steps.append({"press": button, "why": why})

    def wait(self, screen: str, test: Callable[[dict[str, Any]], bool], reason: str) -> dict[str, Any]:
        deadline = self.clock() + WAIT_SECONDS
        while True:
            view = self.view(screen)
            if view.get("open") and test(view):
                return view
            if self.clock() >= deadline:
                raise LoadoutChangeError(reason, screen=_summary(view))
            self.sleep(POLL_SECONDS)

    def move_to(self, screen: str, key: str, view: dict[str, Any], wanted: Callable[[dict[str, Any]], bool],
                reason: str, *, index: int | None = None,
                buttons: tuple[str, str] = ("up", "down")) -> dict[str, Any]:
        """Move the cursor of list ``key`` until its selected row passes
        ``wanted`` (or reaches ``index``). Each press must move the cursor by
        one row; a text search runs down to the end, then up to the top.
        ``buttons`` are the (back, forward) presses: arrows along a row."""
        row = _selected(view, key)
        if row is None:
            raise LoadoutChangeError(reason, list=key, screen=_summary(view))
        direction = "down"
        for _ in range(MAX_LIST_MOVES * 2):
            if (index is None and wanted(row)) or row["index"] == index:
                return view
            if index is not None:
                direction = "down" if row["index"] < index else "up"
            elif direction == "down" and row["count"] is not None and row["index"] >= row["count"] - 1:
                direction = "up"
            elif direction == "up" and row["index"] == 0:
                break
            before = row["index"]
            self.press(buttons[1] if direction == "down" else buttons[0], f"{key} {before} -> {direction}")
            step = 1 if direction == "down" else -1
            view = self.wait(screen, lambda v: (_selected(v, key) or {}).get("index") == before + step,
                             "list_cursor_did_not_move")
            row = _selected(view, key)
        raise LoadoutChangeError(reason, list=key, last=row)

    def main_menu(self) -> dict[str, Any]:
        """The main menu with a readable cursor: reused when it is open,
        otherwise opened from the field (closing any other menu first)."""
        main = self.field.read()
        if main.get("main_menu_item") is None:
            if self.field.in_menu() and not self.close():
                raise LoadoutChangeError("menu_did_not_close")
            self.press("menu", "open the main menu")
            deadline = self.clock() + MAIN_MENU_SECONDS
            while (main := self.field.read()).get("main_menu_item") is None:
                if self.clock() >= deadline:
                    raise LoadoutChangeError("main_menu_cursor_unreadable")
                self.sleep(POLL_SECONDS)
            # The first read as it opens can name a stale entry (save_slot_load).
            self.sleep(POLL_SECONDS)
            main = self.field.read()
            if main.get("main_menu_item") is None:
                raise LoadoutChangeError("main_menu_cursor_unreadable")
        return main

    def open_screen(self, entry: str, screen: str, member_key: str) -> dict[str, Any]:
        """Main Menu > ``entry`` > the member's own screen."""
        try:
            main = self.field.select_main(self.main_menu(), entry)
        except FieldMenuError as error:
            raise LoadoutChangeError(error.reason, **error.details) from error
        self.steps.append({"main_menu": main["main_menu_item"]})
        self.press("confirm", f"open {entry}")
        view = self.wait(screen, lambda v: _selected(v, MEMBER_LIST) is not None, "member_list_not_open")
        view = self.move_to(screen, MEMBER_LIST, view, lambda row: member_key in row["texts"], "member_not_listed")
        return view

    def close(self) -> bool:
        """Cancel back to the field. A Cancel that lands just after the menu
        closed is refused by the input guard; the field is then reached."""
        try:
            return self.field.close()
        except Exception as error:  # the guard's SafetyError, raised only after a press attempt
            if self.field.in_menu():
                raise LoadoutChangeError("menu_did_not_close", error=str(error)) from error
            return True

    def commit(self, screen: str, done: Callable[[dict[str, Any]], bool], reason: str, *,
               answer: Callable[[dict[str, Any]], str | None]) -> dict[str, Any]:
        """After the Confirm that applies a change: wait for ``done``, or for
        a Yes/No prompt that ``answer`` decides (None stops). The highlight is
        moved and read back before the prompt's Confirm."""
        deadline = self.clock() + WAIT_SECONDS
        while True:
            view = self.view(screen)
            if view.get("open") and done(view):
                return view
            prompt = self.prompt()
            if prompt is not None:
                wanted = answer(prompt)
                self.steps.append({"prompt": prompt["text"][:120], "answer": wanted})
                if wanted is None:
                    raise LoadoutChangeError("unexpected_prompt", prompt=prompt)
                for _ in range(3):
                    if prompt["selected"] == wanted:
                        break
                    self.press("up" if wanted == "yes" else "down", f"prompt -> {wanted}")
                    self.sleep(POLL_SECONDS)
                    prompt = self.prompt() or {"selected": None}
                if prompt.get("selected") != wanted:
                    raise LoadoutChangeError("prompt_selection_unverified", wanted=wanted, prompt=prompt)
                self.press("confirm", f"answer {wanted}")
                return self.wait(screen, done, reason)
            if self.clock() >= deadline:
                raise LoadoutChangeError(reason, screen=_summary(view))
            self.sleep(POLL_SECONDS)


def _member(member: str) -> tuple[int, str]:
    name = str(member).strip().casefold().replace(" ", "_")
    if name not in PARTY_KEYS:
        raise LoadoutChangeError("unknown_party_member", member=member, known=list(PARTY_KEYS))
    return loadout_save.MEMBER_NAMES.index(name), PARTY_KEYS[name]


def _verified_process(service: Any) -> dict[str, str]:
    loaded = service.get_queens_blood_run_status()
    process = (loaded.get("loaded_save") or {}).get("process")
    if not loaded.get("loaded_save_verified") or not process:
        raise LoadoutChangeError("loadout_verified_save_required")
    return process


def _snapshot(process: dict[str, str]) -> loadout_save.SaveSnapshot:
    try:
        return loadout_save.capture(process)
    except (OSError, ValueError) as error:
        raise LoadoutChangeError("loadout_save_unreadable", error=str(error)) from error


def _open_overview(menu: LoadoutMenu, member_key: str) -> dict[str, Any]:
    """The member's equipment overview: reuse it when it is already open."""
    view = menu.view("equipment")
    if view.get("open") and view["texts"].get(MEMBER_TITLE) == member_key and _selected(view, CATEGORY_LIST):
        return view
    if view.get("open") and not menu.close():
        raise LoadoutChangeError("menu_did_not_close")
    menu.open_screen("materia_equipment", "equipment", member_key)
    menu.press("confirm", "open the member's overview")
    return menu.wait("equipment", lambda v: _selected(v, CATEGORY_LIST) is not None
                     and v["texts"].get(MEMBER_TITLE) == member_key, "equipment_overview_not_open")


def _gear(view: dict[str, Any]) -> dict[str, str | None]:
    return {category: view["texts"].get(path) for category, path in GEAR_TITLES.items()}


def change_equipment(menu: LoadoutMenu, process: dict[str, str], member: str, category: str,
                     item: str, retain_upgrade_setting: bool = False) -> dict[str, Any]:
    """Equip gear ``item`` (its text key, e.g. ``$Item_W_TSW_0101``).
    ``retain_upgrade_setting`` answers the weapon's carry-over prompt."""
    index, member_key = _member(member)
    if category not in CATEGORIES:
        raise LoadoutChangeError("unknown_equipment_category", category=category, known=list(CATEGORIES))
    item_key = item if str(item).startswith("$") else "$" + str(item)
    view = _open_overview(menu, member_key)
    before_gear = _gear(view)
    if before_gear[category] == item_key:
        raise LoadoutChangeError("already_equipped", gear=before_gear)
    menu.setup("equipment overview")
    view = menu.move_to("equipment", CATEGORY_LIST, view, lambda row: False, "category_not_reached",
                        index=CATEGORIES.index(category))
    menu.press("confirm", f"open the {category} list")
    view = menu.wait("equipment", lambda v: _selected(v, GEAR_LIST) is not None, "gear_list_not_open")
    view = menu.move_to("equipment", GEAR_LIST, view, lambda row: item_key in row["texts"], "item_not_listed")
    before = _snapshot(process)
    menu.press("confirm", f"equip {item_key}")
    view = menu.commit("equipment", lambda v: _selected(v, CATEGORY_LIST) is not None
                       and _selected(v, GEAR_LIST) is None, "gear_list_did_not_close",
                       answer=lambda p: (("yes" if retain_upgrade_setting else "no")
                                         if category == "weapon" else None))
    after = _snapshot(process)
    after_gear = _gear(view)
    changes = loadout_save.diff(before, after)
    name = loadout_save.MEMBER_NAMES[index]
    own = {f"member.{name}.{category}"}
    allowed = own | {f"member.{name}.{row}_materia.{slot}" for row in ("weapon", "armor") for slot in range(8)}
    if category == "weapon":
        # A weapon's own upgrade setting lives in its copy record.
        allowed |= {c["field"] for c in changes if c["field"].startswith(
            (f"weapon.{before.member(index)['weapon']}.", f"weapon.{after.member(index)['weapon']}."))
            and ".skill." not in c["field"]} if retain_upgrade_setting else set()
    native = [change for change in changes if change["field"] in own]
    return _outcome("equipment", changes, allowed,
                    ui_ok=after_gear[category] == item_key and all(
                        after_gear[c] == before_gear[c] for c in CATEGORIES if c != category),
                    native_ok=bool(native),
                    member=member_key, category=category, item=item_key,
                    before={"gear": before_gear, "copy": before.member(index)[category]},
                    after={"gear": after_gear, "copy": after.member(index)[category]})


def _set_materia(menu: LoadoutMenu, member_key: str, row: str, slot: int) -> dict[str, Any]:
    """Set Materia with the cursor on socket ``slot`` of ``row``."""
    view = _open_overview(menu, member_key)
    if not any(_selected(view, key) for key in SOCKET_ROWS.values()):
        menu.press("menu_triangle", "Set Materia")
        view = menu.wait("equipment", lambda v: any(_selected(v, key) for key in SOCKET_ROWS.values()),
                         "set_materia_not_open")
    for _ in range(len(ROW_ORDER) * 2):
        current = next(name for name, key in SOCKET_ROWS.items() if _selected(view, key))
        if current == row:
            break
        direction = "down" if ROW_ORDER.index(current) < ROW_ORDER.index(row) else "up"
        menu.press(direction, f"socket row {current} -> {direction}")
        view = menu.wait("equipment", lambda v, c=current: any(
            _selected(v, key) for name, key in SOCKET_ROWS.items() if name != c), "socket_row_did_not_move")
    key = SOCKET_ROWS[row]
    target = _selected(view, key)
    if target is None:
        raise LoadoutChangeError("socket_row_not_reached", row=row, screen=_summary(view))
    if target["count"] is not None and not 0 <= slot < target["count"]:
        raise LoadoutChangeError("socket_not_available", row=row, slot=slot, sockets=target["count"])
    for _ in range(MAX_LIST_MOVES):
        target = _selected(view, key)
        if target is None:
            raise LoadoutChangeError("socket_row_lost", row=row, screen=_summary(view))
        if target["index"] == slot:
            menu.setup(f"{row} socket {slot}")
            return view
        before = target["index"]
        direction = "dialogue_right" if before < slot else "dialogue_left"
        menu.press(direction, f"{row} socket {before} -> {direction}")
        step = 1 if before < slot else -1
        view = menu.wait("equipment", lambda v: (_selected(v, key) or {}).get("index") == before + step,
                         "socket_cursor_did_not_move")
    raise LoadoutChangeError("socket_not_reached", row=row, slot=slot)


def change_materia(menu: LoadoutMenu, service: Any, process: dict[str, str], member: str, row: str, slot: int,
                   copy_id: str, take_from: str | None = None) -> dict[str, Any]:
    """Put Materia copy ``copy_id`` (a save-scoped instance ID from
    read_materia_inventory) in ``row`` (weapon or armor) socket ``slot``.
    A copy another member has equipped needs ``take_from`` naming them."""
    from .materia_memory import read_materia_inventory
    index, member_key = _member(member)
    if row not in ("weapon", "armor"):
        raise LoadoutChangeError("unknown_socket_row", row=row)
    if type(slot) is not int or not 0 <= slot < loadout_save.SOCKETS:
        raise LoadoutChangeError("socket_out_of_range", slot=slot)
    wanted = int(copy_id)
    holder = _member(take_from)[0] if take_from is not None else None
    _set_materia(menu, member_key, row, slot)
    first = _snapshot(process)
    before_member = first.member(index)
    if before_member[f"{row}_materia"][slot] == wanted:
        raise LoadoutChangeError("already_in_socket", copy_id=str(wanted))
    owner = first.materia_owner(wanted)
    if owner not in (None, index) and owner != holder:
        raise LoadoutChangeError("materia_copy_equipped_elsewhere", copy_id=str(wanted),
                                 owner=loadout_save.MEMBER_NAMES[owner] if owner < len(loadout_save.MEMBER_NAMES) else owner)
    menu.press("confirm", f"open the Materia list for {row} socket {slot}")
    view = menu.wait("equipment", lambda v: _selected(v, CHOICE_LIST) is not None, "materia_list_not_open")
    inventory = read_materia_inventory(service)
    if not inventory.get("ok"):
        raise LoadoutChangeError("materia_list_unreadable", reason_detail=inventory.get("reason"))
    choice = next((m for m in inventory["materia"] if m["category"] == "standard" and m["instance_id"] == str(wanted)), None)
    if choice is None:
        raise LoadoutChangeError("materia_copy_not_listed", copy_id=str(wanted))
    view = menu.move_to("equipment", CHOICE_LIST, view, lambda r: False, "materia_copy_not_reached",
                        index=choice["menu_index"])
    shown = _selected(view, CHOICE_LIST)
    before = _snapshot(process)
    menu.press("confirm", f"set Materia copy {wanted}")
    view = menu.commit("equipment", lambda v: _selected(v, CHOICE_LIST) is None
                       and _selected(v, SOCKET_ROWS[row]) is not None, "materia_list_did_not_close",
                       answer=lambda p: "yes" if holder is not None and owner == holder else None)
    after = _snapshot(process)
    changes = loadout_save.diff(before, after)
    allowed = {f"member.{name}.{r}_materia.{s}" for name in loadout_save.MEMBER_NAMES
               for r in ("weapon", "armor") for s in range(loadout_save.SOCKETS)}
    allowed |= {f"member.{name}.summon_materia" for name in loadout_save.MEMBER_NAMES}
    # The copies that moved record their new owner.
    displaced = before_member[f"{row}_materia"][slot]
    allowed |= {f"materia.{copy}.state" for copy in (wanted, displaced) if copy >= 0}
    after_member = after.member(index)
    if holder is not None:
        details_from = {"taken_from": loadout_save.MEMBER_NAMES[holder]}
    else:
        details_from = {}
    return _outcome("materia", changes, allowed, **details_from,
                    ui_ok=bool(shown) and view["texts"].get(INFO_TITLE) == "$Item_" + choice["item_id"],
                    native_ok=after_member[f"{row}_materia"][slot] == wanted and after.materia_owner(wanted) == index,
                    member=member_key, row=row, slot=slot,
                    materia={**choice, "row_texts": (shown or {}).get("texts")},
                    before={"socket": displaced, "owner": before.materia_owner(wanted)},
                    after={"socket": after_member[f"{row}_materia"][slot], "owner": after.materia_owner(wanted),
                           "displaced_owner": after.materia_owner(displaced) if displaced >= 0 else None,
                           "info": view["texts"].get(INFO_TITLE)})


def change_weapon_skill(menu: LoadoutMenu, process: dict[str, str], member: str, slot: int,
                        skill: str, disable_auto_upgrade: bool = False) -> dict[str, Any]:
    """Set weapon skill ``skill`` (its text key, e.g.
    ``$menu_WeaponUpgrade_Core_name_MPUP_003``) in the equipped weapon's
    skill ``slot``. ``disable_auto_upgrade`` answers Yes to turning off the
    weapon's automatic skill upgrades, which manual edits require."""
    index, member_key = _member(member)
    skill_key = skill if str(skill).startswith("$") else "$" + str(skill)
    if type(slot) is not int or not 0 <= slot < loadout_save.SKILL_SLOTS:
        raise LoadoutChangeError("skill_slot_out_of_range", slot=slot)
    view = _set_materia(menu, member_key, "weapon_skill", slot)
    before_info = view["texts"].get(INFO_TITLE)
    if before_info == skill_key:
        raise LoadoutChangeError("skill_already_set", skill=skill_key)
    first = _snapshot(process)
    menu.press("confirm", f"open the weapon skills for slot {slot}")
    view = menu.commit("equipment", lambda v: _selected(v, CHOICE_LIST) is not None, "skill_list_not_open",
                       answer=lambda p: "yes" if disable_auto_upgrade else None)
    view = menu.move_to("equipment", CHOICE_LIST, view, lambda r: skill_key in r["texts"], "skill_not_listed")
    before = _snapshot(process)
    # Turning auto-upgrade off writes only the weapon's own record.
    auto_changes = [c for c in loadout_save.diff(first, before)
                    if not c["field"].startswith(f"weapon.{first.member(index)['weapon']}.")]
    if auto_changes:
        raise LoadoutChangeError("unexpected_save_changes_opening_skills", changes=auto_changes)
    weapon = before.member(index)["weapon"]
    before_skills = before.weapon_skills(weapon)
    menu.press("confirm", f"set weapon skill {skill_key}")
    view = menu.wait("equipment", lambda v: _selected(v, CHOICE_LIST) is None
                     and _selected(v, SOCKET_ROWS["weapon_skill"]) is not None, "skill_list_did_not_close")
    after = _snapshot(process)
    after_skills = after.weapon_skills(weapon)
    changes = loadout_save.diff(before, after)
    allowed = {f"weapon.{weapon}.skill.{s}" for s in range(loadout_save.SKILL_SLOTS)}
    changes = loadout_save.diff(first, after)
    if disable_auto_upgrade:
        allowed |= {c["field"] for c in changes if c["field"].startswith(f"weapon.{weapon}.+")}
    return _outcome("weapon_skill", changes, allowed,
                    ui_ok=view["texts"].get(INFO_TITLE) == skill_key,
                    native_ok=after_skills[slot] != before_skills[slot] and after.member(index)["weapon"] == weapon,
                    member=member_key, slot=slot, skill=skill_key, weapon_copy=weapon,
                    before={"info": before_info, "skills": before_skills},
                    after={"info": view["texts"].get(INFO_TITLE), "skills": after_skills})


def _new_selected(view: dict[str, Any], seen: set[str]) -> str | None:
    """The one selected list not in ``seen`` (a popup that just opened)."""
    fresh = [key for key in view.get("lists") or {} if key not in seen and _selected(view, key)]
    return fresh[0] if len(fresh) == 1 else None


def _open_combat_settings(menu: LoadoutMenu, member_key: str) -> dict[str, Any]:
    """Combat Settings' member list with ``member_key`` highlighted."""
    view = menu.view("battle_setting")
    if not (view.get("open") and _selected(view, MEMBER_LIST)):
        if view.get("open") and not menu.close():
            raise LoadoutChangeError("menu_did_not_close")
        return menu.open_screen("combat_settings", "battle_setting", member_key)
    return menu.move_to("battle_setting", MEMBER_LIST, view, lambda row: member_key in row["texts"],
                        "member_not_listed")


def change_shortcut(menu: LoadoutMenu, process: dict[str, str], member: str, slot: int, entry: str,
                    category: str = "abilities") -> dict[str, Any]:
    """Set ground shortcut ``slot`` (1-4) to ``entry`` (its text key, e.g.
    ``$M_CMD_001_atk001``) from ``category``."""
    _, member_key = _member(member)
    entry_key = entry if str(entry).startswith("$") else "$" + str(entry)
    if type(slot) is not int or not 1 <= slot <= 4:
        raise LoadoutChangeError("shortcut_slot_out_of_range", slot=slot)
    if category not in SHORTCUT_CATEGORIES:
        raise LoadoutChangeError("unknown_shortcut_category", category=category, known=list(SHORTCUT_CATEGORIES))
    _open_combat_settings(menu, member_key)
    menu.press("confirm", "open the member's shortcuts")
    view = menu.wait("battle_setting", lambda v: _selected(v, SHORTCUT_LIST) is not None
                     and v["texts"].get(MEMBER_TITLE) == member_key, "shortcuts_not_open")
    view = menu.move_to("battle_setting", SHORTCUT_LIST, view, lambda r: False, "shortcut_slot_not_reached",
                        index=slot - 1)
    before_row = _selected(view, SHORTCUT_LIST)["texts"]
    menu.setup(f"shortcut {slot}")
    seen = {key for key in view["lists"] if _selected(view, key)}
    menu.press("confirm", f"choose shortcut {slot}")
    view = menu.wait("battle_setting", lambda v: _new_selected(v, seen) is not None, "shortcut_categories_not_open")
    popup = _new_selected(view, seen)
    view = menu.move_to("battle_setting", popup, view, lambda r: False, "shortcut_category_not_reached",
                        index=SHORTCUT_CATEGORIES.index(category))
    seen.add(popup)
    menu.press("confirm", f"open {category}")
    view = menu.wait("battle_setting", lambda v: _new_selected(v, seen) is not None, "shortcut_entries_not_open")
    entries = _new_selected(view, seen)
    view = menu.move_to("battle_setting", entries, view, lambda r: entry_key in r["texts"],
                        "shortcut_entry_not_listed")
    chosen = _selected(view, entries)["texts"]
    before = _snapshot(process)
    menu.press("confirm", f"set {entry_key}")
    view = menu.wait("battle_setting", lambda v: (_selected(v, SHORTCUT_LIST) or {}).get("index") == slot - 1
                     and _selected(v, entries) is None and _selected(v, popup) is None,
                     "shortcut_entries_did_not_close")
    after = _snapshot(process)
    after_row = _selected(view, SHORTCUT_LIST)["texts"]
    changes = loadout_save.diff(before, after)
    return _outcome("shortcut", changes, {"combat_settings.shortcuts"},
                    ui_ok=bool(after_row) and after_row != before_row,
                    native_ok=any(c["field"] == "combat_settings.shortcuts" for c in changes),
                    member=member_key, slot=slot, category=category, entry=entry_key,
                    before={"row": before_row}, after={"row": after_row, "chosen_row": chosen})


def change_party(menu: LoadoutMenu, process: dict[str, str], member_out: str, member_in: str) -> dict[str, Any]:
    """Swap frontline ``member_out`` for backline ``member_in`` in Combat
    Settings > Edit Party."""
    out_index, out_key = _member(member_out)
    in_index, in_key = _member(member_in)
    snapshot = _snapshot(process)
    party = snapshot.party()
    if out_index not in party["frontline"] or in_index not in party["backline"]:
        raise LoadoutChangeError("party_members_not_placed", party=party)
    _open_combat_settings(menu, out_key)
    menu.press("menu_square", "Edit Party")
    front, back = PARTY_LISTS["frontline"], PARTY_LISTS["backline"]
    view = menu.wait("member_change", lambda v: _selected(v, front) is not None, "party_window_not_open")
    menu.setup("party window")

    shown: dict[str, list[str]] = {}

    def pick(view: dict[str, Any], key: str, line: str, place: int, why: str) -> dict[str, Any]:
        row = _selected(view, key)
        if row["count"] is not None and row["count"] != len(party[line]):
            raise LoadoutChangeError("party_window_disagrees_with_save", list=key, shown=row["count"], party=party)
        view = menu.move_to("member_change", key, view, lambda r: False, "party_place_not_reached",
                            index=place, buttons=ARROWS)
        shown[line] = _selected(view, key)["texts"]
        menu.press("confirm", why)
        return view

    view = pick(view, front, "frontline", party["frontline"].index(out_index), f"pick {out_key}")
    view = menu.wait("member_change", lambda v: _selected(v, back) is not None, "backline_not_focused")
    before = _snapshot(process)
    view = pick(view, back, "backline", party["backline"].index(in_index), f"swap in {in_key}")
    view = menu.wait("member_change", lambda v: _selected(v, front) is not None, "party_swap_did_not_return")
    after = _snapshot(process)
    expected = {"frontline": [in_index if m == out_index else m for m in party["frontline"]],
                "backline": [out_index if m == in_index else m for m in party["backline"]]}
    changes = loadout_save.diff(before, after)
    returned = _selected(view, front)
    # The cursor returns to the swapped place, now showing the incoming member.
    return _outcome("party", changes, {"combat_settings.party"},
                    ui_ok=returned["index"] == party["frontline"].index(out_index)
                    and returned["texts"] == shown["backline"],
                    native_ok=after.party() == expected,
                    member=out_key, member_in=in_key,
                    before={"party": party, "picked_rows": shown}, after={"party": after.party(), "row": returned["texts"]})


def _outcome(kind: str, changes: list[dict[str, Any]], allowed: set[str], *, ui_ok: bool, native_ok: bool,
             **details: Any) -> dict[str, Any]:
    unexpected = [change for change in changes if change["field"] not in allowed]
    verified = ui_ok and native_ok and not unexpected
    reason = ("verified" if verified else "unexpected_save_changes" if unexpected
              else "screen_readback_mismatch" if not ui_ok else "save_readback_mismatch")
    return {"ok": verified, "verified": verified, "outcome": reason, "change": kind, **details,
            "save_changes": changes, "unexpected_save_changes": unexpected,
            "screen_verified": ui_ok, "save_verified": native_ok}


def change_loadout(service: Any, change: str, *, member: str = "cloud", execute: bool = False,
                   close_menu: bool = False, menu: LoadoutMenu | None = None,
                   **arguments: Any) -> dict[str, Any]:
    """One change, verified on screen and in the save data.

    ``change``: ``equipment`` (category, item), ``materia`` (row, slot,
    copy_id), ``weapon_skill`` (slot, skill), ``shortcut`` (slot 1-4, entry,
    category) or ``party`` (``member`` out of the frontline, member_in). ``execute=False`` returns the
    member's current loadout from the save data and sends no input.
    """
    process = _verified_process(service)
    index, member_key = _member(member)
    if not execute:
        snapshot = _snapshot(process)
        loadout = snapshot.member(index)
        try:
            loadout["weapon_skills"] = snapshot.weapon_skills(loadout["weapon"])
        except ValueError as error:
            loadout["weapon_skills_error"] = str(error)
        return {"ok": True, "input_sent": False, "execute": False, "change": change, "member": member_key,
                "loadout": loadout, "party": snapshot.party()}
    menu = menu or LoadoutMenu(service)
    try:
        if change == "equipment":
            result = change_equipment(menu, process, member, **arguments)
        elif change == "materia":
            result = change_materia(menu, service, process, member, **arguments)
        elif change == "weapon_skill":
            result = change_weapon_skill(menu, process, member, **arguments)
        elif change == "shortcut":
            result = change_shortcut(menu, process, member, **arguments)
        elif change == "party":
            result = change_party(menu, process, member, **arguments)
        else:
            raise LoadoutChangeError("unknown_change", change=change,
                                     known=["equipment", "materia", "weapon_skill", "shortcut", "party"])
    except LoadoutChangeError as error:
        result = {"ok": False, "verified": False, "outcome": error.reason, "change": change, **error.details}
    except TypeError as error:
        result = {"ok": False, "verified": False, "outcome": "invalid_arguments", "change": change, "error": str(error)}
    except Exception as error:  # e.g. the input guard refused a press: report it, never leave it unrecorded
        result = {"ok": False, "verified": False, "outcome": "input_refused", "change": change,
                  "error": f"{type(error).__name__}: {error}"}
    if close_menu:
        result["menu_closed"] = menu.close()
    result.update(input_sent=bool(menu.presses), presses=len(menu.presses), steps=menu.steps[-40:], execute=True)
    return result
