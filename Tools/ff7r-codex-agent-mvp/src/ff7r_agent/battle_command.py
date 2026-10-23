"""The battle command menu: read it and issue an ATB command by name.

Live 2026-10-20 (Mt. Nibel, Classic mode, the Velociwing fight): the battle
HUD's Commands Menu key (C) opened Tactical Mode with the list "Commands"
(Abilities, Spells, Items). The screen texts give the list title
(``Command_Switching.WidgetTree.List_00.WidgetTree.Txt_Title``), each entry
(``Command_Cell_00.WidgetTree.Txt_Command``, hidden cells read ``--``) and the
highlighted entry's help (``Command_Help.WidgetTree.Txt_Help``). The
highlight itself is the ``EndVirtualScrollBox`` ``Command_Switching...
List_00.WidgetTree.List_00`` selected index, read by the save-list
discovery of the ``savecells`` snapshot. Up/down (arrow keys) move it one
entry, Confirm opens a submenu (Items: Mega-Potion, Ether, Phoenix Down),
Cancel goes back one level and closes the menu from the top. Each list
remembers its highlight (back from Items, "Commands" showed Items).

The text order is not the drawn order (with Limit ready, "Commands" drew
Abilities, Spells, Items, Limit but the texts came Abilities, Spells, Limit,
Items) and reused cells keep old text (the Fire target list still held
"Cure"). So the highlighted entry is read from the list's selected cell
itself: the exporter names each text's owning widget instance, and the
cursor moves until that cell reads the wanted entry.
After a spell or item the same list shows its targets (title "Fire":
Velociwing, Kyuvildun; help "Select a target."), chosen like any entry.
"""
from __future__ import annotations

from typing import Any, Callable

LIST_SUFFIX = ".Command_Switching.WidgetTree.List_00.WidgetTree.List_00"
TITLE_SUFFIX = ".Command_Switching.WidgetTree.List_00.WidgetTree.Txt_Title"
ENTRY_SUFFIX = "Command_Cell_00.WidgetTree.Txt_Command"
HELP_SUFFIX = ".Command_Help.WidgetTree.Txt_Help"
# The enemy's telegraphed attack name, shown only while it winds up (live:
# "Charge", "Backflip"; absent between attacks).
ENEMY_SKILL_SUFFIX = ".SkillName_Enemy.WidgetTree.Txt_SkillName"
# The help line of a target list (live: after Fire, Braver, Mega-Potion).
TARGET_HELP = "Select a target."
EMPTY_ENTRIES = {"", "--", " "}
MAX_PATH = 4
MAX_PRESSES = 24
SETTLE_SECONDS = 0.35


def _owner(object_id: Any) -> str | None:
    """``Command_Cell_00_C_2147481433`` from a selected child's full id."""
    if not isinstance(object_id, str) or not object_id:
        return None
    return object_id.rsplit(".", 1)[-1].rsplit(":", 1)[-1] or None


def parse_command_menu(texts: list[dict[str, Any]], lists: list[dict[str, Any]]) -> dict[str, Any]:
    """The command menu from one snapshot: open, title, entries, the
    highlighted index and the highlighted entry read from its own cell."""
    record = next((item for item in lists if isinstance(item, dict)
                   and str(item.get("id", "")).endswith(LIST_SUFFIX) and item.get("visible")), None)
    selected = record.get("selected_index") if record else None
    open_ = isinstance(selected, int) and not isinstance(selected, bool) and selected >= 0
    rows = [(str(t.get("path", "")), str(t.get("text", "")), t.get("owner")) for t in texts if isinstance(t, dict)]
    title = next((text for path, text, _ in rows if path.endswith(TITLE_SUFFIX)), None)
    enemy_skill = next((text.strip() for path, text, _ in rows
                        if path.endswith(ENEMY_SKILL_SUFFIX) and text.strip()), None)
    help_text = next((text for path, text, _ in rows if path.endswith(HELP_SUFFIX)), None)
    cells = [(text, owner) for path, text, owner in rows
             if path.endswith(ENTRY_SUFFIX) and text.strip() not in EMPTY_ENTRIES]
    count = None
    try:
        count = int(((record or {}).get("properties") or {}).get("ItemCount"))
    except (TypeError, ValueError):
        pass
    # The highlighted entry is the text of the list's selected cell itself.
    selected_owner = _owner(record.get("selected_child_id")) if record else None
    highlighted = next((text for text, owner in cells if owner and owner == selected_owner), None)
    entries = [text for text, _ in cells]
    if count is not None and 0 <= count <= len(entries):
        entries = entries[:count]  # informative: reused cells keep old text
    return {"open": open_, "title": title if open_ else None, "entries": entries if open_ else [],
            "all_entries": [text for text, _ in cells] if open_ else [], "item_count": count if open_ else None,
            "selected_index": selected if open_ else None, "selected": highlighted if open_ else None,
            "help": help_text.strip() if open_ and isinstance(help_text, str) else None,
            "enemy_skill": enemy_skill}


def names_match(entry: str | None, name: str) -> bool:
    """An entry is the name, or the name with a one-letter tag (two enemies
    of a kind are listed "Velociwing A" and "Velociwing B")."""
    if not isinstance(entry, str):
        return False
    have, wanted = entry.strip().casefold(), name.strip().casefold()
    return have == wanted or (len(have) == len(wanted) + 2 and have.startswith(wanted + " ") and have[-1].isalpha())


def use_command(read: Callable[[], dict[str, Any]], tap: Callable[[str], Any],
                sleep: Callable[[float], None], path: list[str], execute: bool,
                target: str | None = None, hold_on_enemy_skill: bool = False) -> dict[str, Any]:
    """Open the menu, walk ``path`` (e.g. ["Items", "Mega-Potion"]) by name,
    verifying the highlight before each Confirm, and report how it ended.
    When a target list follows the last entry, ``target`` is chosen in it by
    name, or without one the target the game already highlights (its lock)."""
    presses: list[str] = []
    steps: list[dict[str, Any]] = []

    def press(button: str) -> dict[str, Any]:
        tap(button)
        presses.append(button)
        sleep(SETTLE_SECONDS)
        return read()

    def report(ok: bool, outcome: str, menu: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {"ok": ok, "outcome": outcome, "execute": execute, "path": path, "steps": steps,
                "menu": menu, "presses": presses, "input_sent": bool(presses), **extra}

    def close(menu: dict[str, Any]) -> dict[str, Any]:
        for _ in range(MAX_PATH + 1):
            if not menu["open"]:
                break
            menu = press("cancel")
        return menu

    menu = read()
    if not execute:
        return report(True, "dry_run", menu)
    if hold_on_enemy_skill and menu.get("enemy_skill"):
        # An attack landing mid-cast knocks Cloud out of it and still spends
        # the ATB (owner, 2026-10-20): wait for it to pass and try again.
        return report(False, "enemy_attack_incoming", menu, enemy_skill=menu["enemy_skill"])
    if not menu["open"]:
        menu = press("battle_command_menu")
        if not menu["open"]:
            return report(False, "command_menu_not_opened", menu)
    names = list(path)
    depth = -1
    while depth + 1 < len(names):
        depth += 1
        name = names[depth]
        if not any(names_match(entry, name) for entry in menu["all_entries"]):
            return report(False, "entry_not_found", close(menu), wanted=name, entries=menu["entries"],
                          title=menu["title"])
        if menu["selected"] is None:
            # The exporter names the selected cell's text (2026-10-20); an
            # older one cannot verify the highlight, so nothing is chosen.
            return report(False, "highlight_unverified", close(menu), wanted=name)
        # Move one way until the selected cell reads the name; at the end of
        # the list (the highlight stops) turn back. Bounded by a full pass.
        direction, moves = "down", 0
        limit = 2 * max(menu["item_count"] or len(menu["all_entries"]), 1) + 2
        while not names_match(menu["selected"], name):
            if moves >= limit or len(presses) >= MAX_PRESSES:
                return report(False, "entry_not_reached", close(menu), wanted=name)
            before = menu["selected_index"]
            menu = press(direction)
            moves += 1
            if not menu["open"]:
                return report(False, "menu_closed_early", menu, after=name)
            if menu["selected_index"] == before:
                if direction == "up":
                    return report(False, "entry_not_reached", close(menu), wanted=name)
                direction = "up"
        index = menu["selected_index"]
        title_before = menu["title"]
        steps.append({"title": title_before, "entry": menu["selected"], "index": index, "help": menu["help"]})
        menu = press("confirm")
        last = depth == len(names) - 1
        if not menu["open"]:
            if last:
                # The list closed: the command (or a target prompt the list
                # does not show) is on screen; the caller checks the effect.
                return report(True, "menu_closed_after_command", menu)
            return report(False, "menu_closed_early", menu, after=name)
        if menu["title"] == title_before and menu["selected_index"] == index:
            # Nothing happened: the entry is unavailable (not enough ATB or
            # MP, or nothing to use it on).
            return report(False, "command_not_accepted", close(menu), wanted=name, help=steps[-1]["help"])
        if last and menu["help"] == TARGET_HELP:
            if target:
                names.append(target)
                continue
            # No target named: the one the game highlights is its lock-on.
            if menu["selected"] is None:
                return report(False, "highlight_unverified", close(menu), wanted="target")
            steps.append({"title": menu["title"], "entry": menu["selected"], "index": menu["selected_index"],
                          "help": menu["help"]})
            menu = press("confirm")
            if not menu["open"]:
                return report(True, "menu_closed_after_command", menu, target=steps[-1]["entry"])
            return report(False, "menu_still_open", menu, note="the target list stayed open")
        if last:
            return report(False, "menu_still_open", menu, note="a further choice is shown")
    return report(False, "menu_still_open", menu)
