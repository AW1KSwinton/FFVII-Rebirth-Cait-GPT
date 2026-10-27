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

Live 2026-10-22 (the Materia Guardian, Cloud and Sephiroth): Tactical Mode
names the commanded member above the list ("Q < Sephiroth > E";
``Command_Switching.WidgetTree.Txt_Name`` holds a party-name key) and Q/E
switch it, so one menu commands either member. Range spells carry markup
("Thunder<icon=range>"). The status panel's ``Status_NN`` texts give each
member's name key, HP and MP.
"""
from __future__ import annotations

import re
from typing import Any, Callable

LIST_SUFFIX = ".Command_Switching.WidgetTree.List_00.WidgetTree.List_00"
TITLE_SUFFIX = ".Command_Switching.WidgetTree.List_00.WidgetTree.Txt_Title"
ENTRY_SUFFIX = "Command_Cell_00.WidgetTree.Txt_Command"
HELP_SUFFIX = ".Command_Help.WidgetTree.Txt_Help"
MEMBER_SUFFIX = ".Command_Switching.WidgetTree.Txt_Name"
STATUS_PATTERN = re.compile(r"\.Status\.WidgetTree\.Status_(\d\d)\.WidgetTree\.Txt_(Name|HP_Num|MP_Num)$")
# Party-name keys seen live; an unknown key is reported as itself.
PARTY_NAMES = {
    "$menu_partyname_0000_0002": "Cloud",
    "$menu_partyname_0001_0002": "Barret",
    "$menu_partyname_0002_0002": "Tifa",
    "$menu_partyname_0003_0002": "Aerith",
    "$menu_partyname_0004_0002": "Red XIII",
    "$menu_partyname_0005_0002": "Yuffie",
    "$menu_partyname_0008_0002": "Sephiroth",
}
MARKUP = re.compile(r"<[^>]*>")
# Q / E in Tactical Mode: the previous / next party member.
NEXT_MEMBER = "right"
MAX_MEMBERS = 4
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


def member_name(key: Any) -> str | None:
    """A party member's name from its localisation key (or the text itself)."""
    if not isinstance(key, str) or not key.strip():
        return None
    return PARTY_NAMES.get(key.strip(), key.strip())


def parse_party_status(texts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each filled status panel slot: name, HP and MP as the HUD shows them."""
    slots: dict[str, dict[str, Any]] = {}
    for item in texts:
        if not isinstance(item, dict):
            continue
        match = STATUS_PATTERN.search(str(item.get("path", "")))
        if not match:
            continue
        slot, field = match.groups()
        value = str(item.get("text", "")).strip()
        row = slots.setdefault(slot, {"slot": int(slot)})
        if field == "Name":
            row["name"] = member_name(value)
        elif value.replace(",", "").isdigit():
            row["hp" if field == "HP_Num" else "mp"] = int(value.replace(",", ""))
    return [row for _, row in sorted(slots.items()) if row.get("name") and "hp" in row]


def parse_command_menu(texts: list[dict[str, Any]], lists: list[dict[str, Any]]) -> dict[str, Any]:
    """The command menu from one snapshot: open, title, entries, the
    highlighted index and the highlighted entry read from its own cell, and
    the party member it commands."""
    record = next((item for item in lists if isinstance(item, dict)
                   and str(item.get("id", "")).endswith(LIST_SUFFIX) and item.get("visible")), None)
    selected = record.get("selected_index") if record else None
    open_ = isinstance(selected, int) and not isinstance(selected, bool) and selected >= 0
    rows = [(str(t.get("path", "")), str(t.get("text", "")), t.get("owner")) for t in texts if isinstance(t, dict)]
    title = next((text for path, text, _ in rows if path.endswith(TITLE_SUFFIX)), None)
    enemy_skill = next((text.strip() for path, text, _ in rows
                        if path.endswith(ENEMY_SKILL_SUFFIX) and text.strip()), None)
    help_text = next((text for path, text, _ in rows if path.endswith(HELP_SUFFIX)), None)
    member = next((member_name(text) for path, text, _ in rows if path.endswith(MEMBER_SUFFIX)), None)
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
            "member": member if open_ else None, "enemy_skill": enemy_skill}


def is_target_list(menu: dict[str, Any], enemies: tuple[str, ...] = ()) -> bool:
    """A target list: its help line starts "Select a target" (a range spell
    adds a marker to it, live 2026-10-22: Blizzard, Thunder and Aero were
    backed out of where Fire's exact line matched), or it lists an enemy."""
    help_text = MARKUP.sub("", str(menu.get("help") or "")).strip()
    return (help_text.startswith(TARGET_HELP.rstrip("."))
            or any(names_match(entry, enemy) for entry in menu.get("all_entries") or [] for enemy in enemies))


def names_match(entry: str | None, name: str) -> bool:
    """An entry is the name, or the name with a one-letter tag (two enemies
    of a kind are listed "Velociwing A" and "Velociwing B"). Icon markup
    ("Thunder<icon=range>") is not part of the name."""
    if not isinstance(entry, str):
        return False
    have, wanted = MARKUP.sub("", entry).strip().casefold(), MARKUP.sub("", name).strip().casefold()
    return have == wanted or (len(have) == len(wanted) + 2 and have.startswith(wanted + " ") and have[-1].isalpha())


def use_command(read: Callable[[], dict[str, Any]], tap: Callable[[str], Any],
                sleep: Callable[[float], None], path: list[str], execute: bool,
                target: str | None = None, hold_on_enemy_skill: bool = False,
                member: str | None = None, take_highlighted: bool = False,
                enemies: tuple[str, ...] = (), prefer_child_target: bool = False) -> dict[str, Any]:
    """Open the menu, walk ``path`` (e.g. ["Items", "Mega-Potion"]) by name,
    verifying the highlight before each Confirm, and report how it ended.
    When a target list follows the last entry, ``target`` is chosen in it by
    name, or without one the target the game already highlights (its lock).
    ``member`` first switches Tactical Mode to that party member (Q/E),
    verified by the name it shows. ``take_highlighted`` takes the highlighted
    entry of a further list after the path (e.g. ["Limit"] then its Limit
    Break) instead of stopping there. ``enemies`` (names) also mark a list
    that shows them as the target list."""
    presses: list[str] = []
    steps: list[dict[str, Any]] = []
    top_entries: list[str] = []

    def press(button: str) -> dict[str, Any]:
        tap(button)
        presses.append(button)
        sleep(SETTLE_SECONDS)
        return read()

    def report(ok: bool, outcome: str, menu: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {"ok": ok, "outcome": outcome, "execute": execute, "path": path, "steps": steps,
                "menu": menu, "presses": presses, "input_sent": bool(presses), "member": member,
                "top_entries": list(top_entries), **extra}

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
    if member is not None:
        # Each press of E shows the next member; a full lap without it means it is absent.
        switches = 0
        while not names_match(menu.get("member"), member):
            if switches >= MAX_MEMBERS:
                return report(False, "member_not_reached", close(menu), wanted=member)
            before = menu.get("member")
            menu = press(NEXT_MEMBER)
            switches += 1
            if not menu["open"]:
                return report(False, "menu_closed_early", menu, after="member switch")
            if menu.get("member") == before:
                return report(False, "member_not_reached", close(menu), wanted=member)
    # The top-level list this member offers (it shows "Limit" once a Limit is ready).
    top_entries.extend(menu["entries"])
    names = list(path)
    targeted = False
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
        if last and not targeted and is_target_list(menu, enemies):
            targeted = True
            if target is None and prefer_child_target:
                children = [entry for entry in menu["entries"] if "<button=child-target>" in entry]
                if children:
                    target = children[0]
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
        if last and take_highlighted and depth + 1 < MAX_PATH and menu["selected"] is not None:
            # A list the caller does not name (the Limit Breaks): take the
            # entry the game highlights, verified by its own cell, and go on.
            names.append(menu["selected"])
            continue
        if last:
            return report(False, "menu_still_open", menu, note="a further choice is shown")
    return report(False, "menu_still_open", menu)
