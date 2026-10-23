"""The field (party) menu: read it, and cast a spell or use an item from it.

Live 2026-10-21 (Mt. Nibel, Cloud on 1 HP after a fight): there was no way
to heal outside a fight. The main menu's Spells screen picks a caster, a
spell and a target (the owner's route); Inventory > Items picks an item and
a target. Every move is checked on the screen before the next press:

* main menu: the footer help names the highlighted entry;
* Spells: the spell list's title names the highlighted caster, the info
  panel the highlighted spell (``Magic...EquipInfo.Txt_Title``) and its MP;
* Items: the info panel names the highlighted item;
* target list (both): ``Recover_List`` reports its selected row, one row per
  party member in party order.

The first cast or use measures what it restores; the rest needed to reach
the requested HP or MP follow back to back, then the menu is closed.
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Mapping

from .map_query import request_snapshot
from .save_slot_load import MAIN_MENU_HELP, MAIN_MENU_ORDER, _meaning

MENU_FLAGS = ("menutexts", "savecells")

# Text keys, live 2026-10-21. Raw keys ("$...") are accepted too.
PARTY_KEYS = {"cloud": "$menu_partyname_0000_0002", "sephiroth": "$menu_partyname_0008_0002"}
SPELL_KEYS = {"cure": "$M_MAG_001_heal001", "fire": "$M_MAG_004_atk001", "blizzard": "$M_MAG_005_atk001"}
ITEM_KEYS = {"mega-potion": "$item_mpotion", "ether": "$item_atel", "phoenix down": "$item_phenxtal"}
ITEMS_CATEGORY_HELP = "$help_message_0001_0000"
RECOVER_CELL = "Recover_Cell.WidgetTree.MemberList_CellContent.WidgetTree."

PRESS_MS = 120
WAIT_SECONDS = 3.0
POLL_SECONDS = 0.25
EFFECT_SECONDS = 2.0
MAX_MOVES = 12
MAX_USES = 10
CLOSE_PRESSES = 6
# How long a Cancel may take to close a menu level.
CLOSE_SETTLE_SECONDS = 1.5


class FieldMenuError(RuntimeError):
    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


def _short(path: str) -> str:
    return path.split("BP_EndGameInstance.")[-1].split("MenuRenderingWorld:")[-1]


def _number(text: Any) -> int | None:
    digits = str(text or "").replace(",", "").strip()
    return int(digits) if digits.isdigit() else None


def _key(name: Any, table: Mapping[str, str], kind: str) -> str:
    text = str(name or "").strip()
    if text.startswith("$"):
        return text
    key = table.get(text.casefold())
    if key is None:
        raise FieldMenuError(f"unknown_{kind}", name=text, known=sorted(table))
    return key


def parse_field_menu(texts: list[tuple[str, str]], lists: list[dict[str, Any]]) -> dict[str, Any]:
    """What the menu shows, from one snapshot's texts and lists."""
    help_items = sorted({meaning for path, text in texts if path.startswith("Foot.")
                         and (meaning := _meaning(MAIN_MENU_HELP, text)) is not None})
    first: dict[str, str] = {}
    for path, text in texts:
        first.setdefault(path, text)
    party = []
    for index in range(4):
        status = f"Status.WidgetTree.Status_0{index}.WidgetTree."
        name = first.get(status + "Txt_Name")
        if name:
            party.append({"slot": index, "name_key": name, "hp": _number(first.get(status + "Txt_HP_Num")),
                          "mp": _number(first.get(status + "Txt_MP_Num"))})
    # Target rows share one path and come in no fixed order, but each row's
    # texts come together (live 2026-10-21: max HP paired by position landed
    # on the wrong member): group them, then pair by the row's name.
    cells: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for path, text in texts:
        if not path.startswith(RECOVER_CELL):
            continue
        field = path[len(RECOVER_CELL):]
        if field in current:
            cells.append(current)
            current = {}
        current[field] = text
    if current:
        cells.append(current)
    by_name = {cell.get("Txt_Title"): cell for cell in cells if cell.get("Txt_Title")}
    for member in party:
        cell = by_name.get(member["name_key"], {})
        member["max_hp"] = _number(cell.get("Txt_Hp_Total"))
        member["max_mp"] = _number(cell.get("Txt_Mp_Total"))

    def selected(owner: str) -> int | None:
        for item in lists:
            index = item.get("selected_index")
            if item.get("visible") and owner in (item.get("owners") or []) and isinstance(index, int) and index >= 0:
                return index
        return None

    return {
        "main_menu_item": help_items[0] if len(help_items) == 1 else None,
        "footer_help": first.get("Foot.WidgetTree.Txt_Help"),
        "spell_caster_key": first.get("Magic.WidgetTree.Magic_SelectList.WidgetTree.Txt_Title"),
        "spell_highlighted": first.get("Magic.WidgetTree.EquipInfo.WidgetTree.Txt_Title"),
        "spell_mp_cost": _number(first.get("Magic.WidgetTree.EquipInfo.WidgetTree.Magic_Guide.WidgetTree.Txt_MpCost")),
        "spell_list_index": selected("Magic_SelectList"),
        "spells": [text for path, text in texts if path == "Item_Cell.WidgetTree.Txt_Title" and text.startswith("$M_MAG_")],
        "item_highlighted": first.get("Item.WidgetTree.EquipInfo.WidgetTree.Txt_Title"),
        "items": [text for path, text in texts if path == "Item_Cell.WidgetTree.Txt_Title" and text.startswith("$item_")],
        "target_index": selected("Recover_List"),
        "party": party,
        "lists": [item for item in lists if item.get("visible")],
    }


def _snapshot(service: Any) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    response, _ = request_snapshot(service, heights=False, flags=MENU_FLAGS, require_map=False)
    texts = [(_short(str(item.get("path", ""))), str(item.get("text", "")))
             for item in response.get("texts") or [] if isinstance(item, dict)]
    lists = []
    for item in response.get("save_lists") or []:
        if isinstance(item, dict):
            lists.append({"id": str(item.get("id", "")).split(".")[-1], "visible": item.get("visible"),
                          "selected_index": item.get("selected_index"),
                          "owners": [str(owner).split(".")[-1] for owner in item.get("owners") or []][:3]})
    return texts, lists


def read_field_menu(service: Any, raw: bool = False) -> dict[str, Any]:
    texts, lists = _snapshot(service)
    result = {"ok": True, "input_sent": False, **parse_field_menu(texts, lists)}
    if raw:
        result["texts"] = [{"path": path, "text": text} for path, text in texts]
        result["all_lists"] = lists
    return result


class FieldMenu:
    """Presses and verified reads through the field menu."""

    def __init__(self, service: Any, *, read: Callable[[], dict[str, Any]] | None = None,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None) -> None:
        self.service = service
        self.read = read or (lambda: parse_field_menu(*_snapshot(service)))
        self.sleep = sleep or time.sleep
        self.clock = clock or time.monotonic
        self.presses: list[str] = []

    def press(self, button: str) -> None:
        self.service.tap(button, PRESS_MS)
        self.presses.append(button)

    def wait_for(self, test: Callable[[dict[str, Any]], bool], reason: str) -> dict[str, Any]:
        deadline = self.clock() + WAIT_SECONDS
        while True:
            screen = self.read()
            if test(screen):
                return screen
            if self.clock() >= deadline:
                raise FieldMenuError(reason, screen={k: screen.get(k) for k in (
                    "main_menu_item", "footer_help", "spell_caster_key", "spell_highlighted",
                    "item_highlighted", "target_index")})
            self.sleep(POLL_SECONDS)

    def open_main_menu(self) -> dict[str, Any]:
        self.press("menu")
        screen = self.wait_for(lambda s: s["main_menu_item"] is not None, "main_menu_cursor_unreadable")
        # The first read as it opens can name a stale entry (save_slot_load).
        self.sleep(POLL_SECONDS)
        return self.wait_for(lambda s: s["main_menu_item"] is not None, "main_menu_cursor_unreadable")

    def move_until(self, screen: dict[str, Any], field: str, wanted: Any, reason: str,
                   direction: str = "down") -> dict[str, Any]:
        for _ in range(MAX_MOVES):
            if screen.get(field) == wanted:
                return screen
            before = screen.get(field)
            self.press(direction)
            screen = self.wait_for(lambda s, b=before: s.get(field) not in (None, b), reason)
        if screen.get(field) == wanted:
            return screen
        raise FieldMenuError(reason, wanted=wanted, highlighted=screen.get(field))

    def select_main(self, screen: dict[str, Any], item: str) -> dict[str, Any]:
        current = screen["main_menu_item"]
        order = list(MAIN_MENU_ORDER)
        ahead = (order.index(item) - order.index(current)) % len(order) if current in order else 0
        direction = "down" if ahead <= len(order) - ahead else "up"
        return self.move_until(screen, "main_menu_item", item, "main_menu_target_not_reached", direction)

    def select_target(self, screen: dict[str, Any], slot: int) -> dict[str, Any]:
        for _ in range(MAX_MOVES):
            index = screen["target_index"]
            if index == slot:
                return screen
            self.press("down" if index < slot else "up")
            screen = self.wait_for(lambda s, b=index: s["target_index"] not in (None, b), "target_cursor_stalled")
        raise FieldMenuError("target_not_reached", slot=slot, index=screen["target_index"])

    def in_menu(self) -> bool:
        state = self.service.telemetry.observe()
        return bool(getattr(state, "in_menu", False) or getattr(state, "paused", False))

    def close(self) -> bool:
        """Cancel back to the field, one press per level. Each press waits for
        the menu flag to settle first: live a Cancel sent while the closed
        menu still read as open landed in the field and reopened it."""
        for _ in range(CLOSE_PRESSES):
            if not self.in_menu():
                return True
            self.press("cancel")
            deadline = self.clock() + CLOSE_SETTLE_SECONDS
            while self.clock() < deadline and self.in_menu():
                self.sleep(POLL_SECONDS)
            if not self.in_menu():
                self.sleep(POLL_SECONDS)  # a second look: the flag can flicker
                if not self.in_menu():
                    return True
        return not self.in_menu()


def _member(screen: Mapping[str, Any], key: str) -> dict[str, Any]:
    for member in screen.get("party") or []:
        if member["name_key"] == key:
            return member
    raise FieldMenuError("party_member_not_shown", member=key)


def _apply(menu: FieldMenu, screen: dict[str, Any], target_key: str, stat: str, goal: int | None,
           limit: int, exact: int | None = None) -> dict[str, Any]:
    """Confirm once, measure, then confirm as often as reaches ``goal``
    (default: full), or ``exact`` times in all."""
    before = _member(screen, target_key)
    start = before[stat]
    cap = before.get(f"max_{stat}")
    if exact is None or goal is not None:
        goal = cap if goal is None else (min(goal, cap) if cap else goal)
    if exact is None and goal is not None and start is not None and start >= goal:
        return {"uses": 0, "from": start, "to": start, "per_use": None, "reason": "already_there"}
    menu.press("confirm")
    menu.sleep(EFFECT_SECONDS)
    screen = menu.read()
    moved = _member(screen, target_key)
    if exact is not None and goal is None:
        # A set number of uses names no stat: the one the first use raised.
        rises = {name: (moved[name] or 0) - (before[name] or 0) for name in ("hp", "mp")}
        stat = max(rises, key=rises.get)
        start = before[stat]
    after = moved[stat]
    per_use = (after - start) if after is not None and start is not None else None
    if not per_use or per_use <= 0:
        return {"uses": 1, "from": start, "to": after, "per_use": per_use, "reason": "nothing_restored"}
    uses = 1
    if exact is not None:
        more = exact - 1
    else:
        more = 0 if goal is None else math.ceil(max(0, goal - after) / per_use)
    more = min(more, limit - 1)
    for _ in range(more):
        menu.press("confirm")
        menu.sleep(EFFECT_SECONDS)
        uses += 1
    screen = menu.read()
    final = _member(screen, target_key)[stat]
    if exact is not None:
        reached = uses == exact
    else:
        reached = goal is not None and final is not None and final >= goal
    return {"uses": uses, "stat": stat, "from": start, "to": final, "per_use": per_use, "goal": goal,
            "reason": "goal_reached" if reached else "stopped_short"}


def cast_field_spell(service: Any, spell: str, caster: str = "cloud", target: str = "cloud",
                     until_hp: int | None = None, max_casts: int = MAX_USES, menu: FieldMenu | None = None) -> dict[str, Any]:
    spell_key = _key(spell, SPELL_KEYS, "spell")
    caster_key, target_key = _key(caster, PARTY_KEYS, "party_member"), _key(target, PARTY_KEYS, "party_member")
    menu = menu or FieldMenu(service)
    try:
        screen = menu.select_main(menu.open_main_menu(), "spells")
        menu.press("confirm")
        screen = menu.wait_for(lambda s: s["spell_caster_key"] is not None, "spells_screen_not_open")
        screen = menu.move_until(screen, "spell_caster_key", caster_key, "caster_not_reached")
        menu.press("confirm")
        screen = menu.wait_for(lambda s: s["spell_list_index"] is not None and s["spell_highlighted"], "spell_list_not_focused")
        screen = menu.move_until(screen, "spell_highlighted", spell_key, "spell_not_reached")
        cost = screen["spell_mp_cost"]
        if cost is None:
            raise FieldMenuError("spell_not_castable_here", spell=spell_key)
        mana = _member(screen, caster_key)["mp"]
        if mana is not None and mana < cost:
            raise FieldMenuError("not_enough_mp", mp=mana, cost=cost)
        menu.press("confirm")
        screen = menu.wait_for(lambda s: s["target_index"] is not None, "target_list_not_open")
        screen = menu.select_target(screen, _member(screen, target_key)["slot"])
        limit = max(1, min(int(max_casts), (mana // cost) if mana is not None else int(max_casts)))
        outcome = _apply(menu, screen, target_key, "hp", until_hp, limit)
        result = {"ok": outcome["reason"] in ("goal_reached", "already_there"), "action": "cast_field_spell",
                  "spell": spell_key, "caster": caster_key, "target": target_key, "mp_cost": cost, **outcome}
    except FieldMenuError as error:
        result = {"ok": False, "action": "cast_field_spell", "reason": error.reason, **error.details}
    result["menu_closed"] = menu.close()
    result["presses"] = len(menu.presses)
    result["input_sent"] = bool(menu.presses)
    return result


def use_field_item(service: Any, item: str, target: str = "cloud", until_hp: int | None = None,
                   until_mp: int | None = None, count: int | None = None, menu: FieldMenu | None = None) -> dict[str, Any]:
    item_key = _key(item, ITEM_KEYS, "item")
    target_key = _key(target, PARTY_KEYS, "party_member")
    if until_hp is not None and until_mp is not None:
        raise FieldMenuError("give until_hp or until_mp, not both")
    menu = menu or FieldMenu(service)
    try:
        screen = menu.select_main(menu.open_main_menu(), "inventory")
        menu.press("confirm")
        screen = menu.wait_for(lambda s: s["footer_help"] is not None and s["main_menu_item"] is None,
                               "inventory_not_open")
        if screen["footer_help"] != ITEMS_CATEGORY_HELP:
            screen = menu.move_until(screen, "footer_help", ITEMS_CATEGORY_HELP, "items_category_not_reached", "up")
        menu.press("confirm")
        screen = menu.wait_for(lambda s: s["item_highlighted"] is not None, "item_list_not_open")
        screen = menu.move_until(screen, "item_highlighted", item_key, "item_not_reached")
        menu.press("confirm")
        screen = menu.wait_for(lambda s: s["target_index"] is not None, "target_list_not_open")
        screen = menu.select_target(screen, _member(screen, target_key)["slot"])
        stat = "mp" if until_mp is not None else "hp"  # a set count takes the stat its first use raises
        goal = until_mp if until_mp is not None else until_hp
        exact = None if count is None else max(1, min(int(count), MAX_USES))
        outcome = _apply(menu, screen, target_key, stat, goal, MAX_USES, exact=exact)
        result = {"ok": outcome["reason"] in ("goal_reached", "already_there"), "action": "use_field_item",
                  "item": item_key, "target": target_key, **outcome}
    except FieldMenuError as error:
        result = {"ok": False, "action": "use_field_item", "reason": error.reason, **error.details}
    result["menu_closed"] = menu.close()
    result["presses"] = len(menu.presses)
    result["input_sent"] = bool(menu.presses)
    return result
