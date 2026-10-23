"""Load a chosen save slot through the title or in-game Load list.

Live 2026-09-28 (game running, keyboard focus on the game):

- Esc opens the main menu. Its 12 items, top to bottom, are Item Transmuter,
  Materia & Equipment, Combat Settings, Enemy Intel, Upgrade Weapons, Spells,
  Inventory, Card Decks, Party, Manual, Save / Load and System; the list
  wraps, and the cursor usually opens on Item Transmuter but not always.
- 200 ms holds auto-repeated (extra steps, even a wrong net direction);
  60 ms taps about 0.7 s apart were reliable.
- Enter on Save / Load opens Save, Load; Down + Enter opens the Load list.
- The Load list has one row per save, newest first: the slot number (none on
  the autosave row, "Autosave Data"), the save date and time ("09/18/2026
  10:18:57 p.m."), chapter, location, level and playtime.
- Enter on a row asks "Load the data in this slot?" with No preselected; Up
  selects Yes. After Yes the telemetry read player_not_loaded, then
  not_ready, then settled gameplay at the saved place within about 20 s.

The menus are read through the map query's all-texts scan (every visible text
block with its widget path, usually as an unresolved localisation key). The
keys below come from ``Resident_TxtRes`` (US); their English text is accepted
too. What is *not* known yet, because no menu text dump exists, is which text
marks the highlighted row, so:

- the main menu and the Save / Load choice are read from the footer help text,
  which describes the highlighted item (``$help_message_0000_0009`` = "Save
  and load game data." for Save / Load) -- an assumption to confirm live;
- a Load-list row is identified by its date text alone, matched to the
  modification time of ``ff7rebirthNNN.sav`` (live, slot 008's row read
  "09/18/2026 10:18:57 p.m." and the file's mtime was 22:18:57 local), so row
  texts never need to be grouped by widget path;
- the highlighted row is a date shown a second time outside the rows (a detail
  panel) or at a path matching ``highlight_path_pattern`` from the layout
  override file. When neither exists nothing is confirmed: the operation stops
  with the texts it saw, so the right widget can be named in the override.

Every press is followed by a read that must show its effect. Nothing is
confirmed unless the highlighted row is the requested slot and the dialog is
the load prompt (never the save prompt, which would overwrite a slot).
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .map_query import request_snapshot
from .navigation_values import MissionError
from .queen_blood_profile import SAVE_SUBPATH, documents_folder

LAYOUT_ENV = "FF7R_SAVE_MENU_LAYOUT"
SNAPSHOT_FLAGS = ("alltexts",)
# The pause menus' texts alone: one object scan on the game thread instead
# of eight, which froze the open menu at every read (live 2026-10-05).
MENU_SNAPSHOT_FLAGS = ("menutexts",)
# On the Load list, also the rows' widgets: the selection is drawn only (#153).
LOAD_LIST_SNAPSHOT_FLAGS = ("menutexts", "savecells")
TITLE_SNAPSHOT_FLAGS = (*LOAD_LIST_SNAPSHOT_FLAGS, "startmenu")


def menu_snapshot(service: Any, rows: bool = False, *, title: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """The menu texts, or every text from an exporter without `menutexts`."""
    response, base = request_snapshot(service, heights=False,
                                      flags=TITLE_SNAPSHOT_FLAGS if title else LOAD_LIST_SNAPSHOT_FLAGS if rows else MENU_SNAPSHOT_FLAGS,
                                      require_map=False)
    if response.get("texts_scope") != "menu":
        response, base = request_snapshot(service, heights=False,
                                          flags=(*SNAPSHOT_FLAGS, "savecells", "startmenu") if title else SNAPSHOT_FLAGS,
                                          require_map=False)
    return response, base
# The exporter lists at most this many texts; a full list may have lost rows.
TEXT_CAP = 400
KEY_MS = 60
# Menu opening is a toggle, not cursor movement; short taps were missed live.
MENU_OPEN_KEY_MS = 500
# Between presses; each step is then verified by reading the screen.
PRESS_GAP_SECONDS = 0.25
FOCUS_TELEMETRY_WAIT_SECONDS = 5.0
POLL_SECONDS = 0.2
CHANGE_WAIT_SECONDS = 4.0
MENU_OPEN_WAIT_SECONDS = 8.0
MENU_OPEN_ATTEMPTS = 2
# Reads for the opening menu's cursor to repeat itself.
MENU_SETTLE_READS = 4
FIELD_WAIT_SECONDS = 30.0
MAX_MENU_CLOSES = 5
MENU_CLOSE_SETTLE_SECONDS = 0.8
# How long "in menu" may outlast the menu's screen as it closes.
MENU_FLAG_CLEAR_SECONDS = 2.0
MAX_STALLS = 1
MAX_SUBMENU_PRESSES = 4
LOAD_CONFIRM_ATTEMPTS = 2
LOAD_START_WAIT_SECONDS = 15.0
LOAD_START_STALE_SECONDS = 3.0
ARRIVAL_WAIT_SECONDS = 120.0
ARRIVAL_MOVED_CM = 100.0
MAX_SCREEN_RECORDS = 40

MAIN_MENU_ORDER = (
    "item_transmuter", "materia_equipment", "combat_settings", "enemy_intel",
    "upgrade_weapons", "spells", "inventory", "card_decks", "party", "manual",
    "save_load", "system",
)
SAVE_LOAD_ITEM = "save_load"

# Footer help per main-menu item ($help_message_0000_*), keyed by the text
# key and by its English text.
_MAIN_MENU_HELP = (
    ("$help_message_0000_0011", "Transmute crude materials into practical items.", "item_transmuter"),
    ("$help_message_0000_0000", "Change equipment or set materia.", "materia_equipment"),
    ("$help_message_0000_0001",
     "Change settings related to combat, such as party members and command shortcuts.", "combat_settings"),
    ("$help_message_0000_0004", "View information about the enemies you have encountered.", "enemy_intel"),
    ("$help_message_0000_0002", "View equipped weapons and adjust skills.", "upgrade_weapons"),
    ("$help_message_0000_0006", "View the spells you have learned.", "spells"),
    ("$help_message_0000_0007", "View the items in your inventory.", "inventory"),
    ("$help_message_0000_0012", "Adjust your Queen's Blood card decks.", "card_decks"),
    ("$help_message_0000_0005", "View the data for your party members.", "party"),
    ("$help_message_0000_0008", "Check here for help playing the game.", "manual"),
    ("$help_message_0000_0009", "Save and load game data.", "save_load"),
    ("$help_message_0000_0010", "View various system options and change gameplay settings.", "system"),
)
_SAVE_LOAD_HELP = (
    ("$help_message_0008_0000", "Save your current game progress.", "save"),
    ("$help_message_0008_0001", "Load your saved data and resume playing.", "load"),
)
_SIGNALS = (
    # Footer help inside the slot lists: which list is open.
    ("$help_message_0008_0005", "Select a save slot to load.", "load_list"),
    ("$help_message_0008_0004", "Select a save slot for your data.", "save_list"),
    # Com_Window prompts.
    ("$menu_save_load_0003_0000", "Load the data in this slot?", "load_prompt"),
    ("$menu_save_load_0002_0000", "Save your progress to this slot?", "save_prompt"),
    ("$menu_save_load_0005_0001", "Checking saved data...", "checking"),
    ("$menu_save_load_0005_0006", "Checking saved data...", "checking"),
    ("$menu_save_load_0005_0005", "Failed to load data.", "load_failed"),
    # Row labels.
    ("$menu_save_load_0001_0000", "Autosave Data", "autosave_label"),
    ("$menu_save_load_0001_0001", "Saved Data", "saved_label"),
)

_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")
_DATE_TIME = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})(?:\s*([ap])\.?\s*m\b\.?)?", re.I)
_CLOCK = re.compile(r"\b(\d{1,2}):(\d{2}):(\d{2})\s*([ap])\.?\s*m\b\.?", re.I)
_SLOT_NUMBER = re.compile(r"^0*(\d{1,3})$")
_DIGITS = re.compile(r"\d+")
_SLOT_FILE = re.compile(r"^ff7rebirth(\d{3})$")


def _normalise(text: Any) -> str:
    value = str(text or "").replace("\xa0", " ").replace(" ", " ")
    return _SPACE.sub(" ", _TAG.sub("", value)).strip()


def _lookup(table: Iterable[tuple[str, str, str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, english, meaning in table:
        result[key] = meaning
        result[_normalise(english).casefold()] = meaning
    return result


MAIN_MENU_HELP = _lookup(_MAIN_MENU_HELP)
SAVE_LOAD_HELP = _lookup(_SAVE_LOAD_HELP)
SIGNALS = _lookup(_SIGNALS)


def _meaning(table: dict[str, str], text: str) -> str | None:
    return table.get(text) or table.get(_normalise(text).casefold())


class SaveSlotLoadError(RuntimeError):
    def __init__(self, code: str, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


# ---------------------------------------------------------------------------
# Layout assumptions, adjustable without a code change


@dataclass(frozen=True)
class SaveMenuLayout:
    """Widget assumptions a live test may need to correct.

    Override any field with a JSON object in ``$FF7R_SAVE_MENU_LAYOUT`` or
    ``%LOCALAPPDATA%/ff7r-agent/save-menu-layout.json``.
    """

    main_menu_order: tuple[str, ...] = MAIN_MENU_ORDER
    # A date text at a path matching this marks the highlighted row.
    highlight_path_pattern: str | None = None
    # Only date texts at paths matching this count (e.g. "SaveLoad").
    row_path_pattern: str | None = None
    date_tolerance_seconds: float = 5.0
    # Additional localisation keys/labels captured by the live validator.
    # Resident_TxtRes US, Analysis/TextUSLegacy-20260927.
    title_load_texts: tuple[str, ...] = ("$menu_title_0001_0002", "Load Game")
    source: str | None = field(default=None, compare=False)


def default_layout_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", ".")) / "ff7r-cait-gpt" / "save-menu-layout.json"


def load_layout(path: str | os.PathLike[str] | None = None) -> SaveMenuLayout:
    source = Path(path or os.environ.get(LAYOUT_ENV) or default_layout_path())
    if not source.is_file():
        return SaveMenuLayout()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SaveSlotLoadError("save_menu_layout_invalid", path=str(source), error=str(exc)) from exc
    if not isinstance(data, dict):
        raise SaveSlotLoadError("save_menu_layout_invalid", path=str(source))
    values: dict[str, Any] = {"source": str(source)}
    labels = data.get("title_load_texts")
    if labels is not None:
        if not isinstance(labels, list) or not labels or any(not isinstance(s, str) or not s.strip() for s in labels):
            raise SaveSlotLoadError("save_menu_layout_invalid", path=str(source), field="title_load_texts")
        values["title_load_texts"] = tuple(labels)
    order = data.get("main_menu_order")
    if order is not None:
        if (not isinstance(order, list) or SAVE_LOAD_ITEM not in order
                or not all(isinstance(item, str) for item in order) or len(set(order)) != len(order)):
            raise SaveSlotLoadError("save_menu_layout_invalid", path=str(source), field="main_menu_order")
        values["main_menu_order"] = tuple(order)
    for name in ("highlight_path_pattern", "row_path_pattern"):
        pattern = data.get(name)
        if pattern is not None:
            try:
                re.compile(str(pattern))
            except re.error as exc:
                raise SaveSlotLoadError("save_menu_layout_invalid", path=str(source), field=name) from exc
            values[name] = str(pattern)
    tolerance = data.get("date_tolerance_seconds")
    if tolerance is not None:
        if not isinstance(tolerance, (int, float)) or not 0 <= float(tolerance) <= 120:
            raise SaveSlotLoadError("save_menu_layout_invalid", path=str(source), field="date_tolerance_seconds")
        values["date_tolerance_seconds"] = float(tolerance)
    return replace(SaveMenuLayout(), **values)


# ---------------------------------------------------------------------------
# Save files


@dataclass(frozen=True)
class SaveFile:
    slot: int
    path: str
    mtime: float
    steam_id: str

    @property
    def saved_at(self) -> datetime:
        return datetime.fromtimestamp(self.mtime)

    def summary(self) -> dict[str, Any]:
        return {"slot": self.slot, "autosave": self.slot == 0, "path": self.path,
                "saved_at": self.saved_at.isoformat(timespec="seconds"), "steam_id": self.steam_id}


def default_save_root() -> Path:
    return documents_folder() / SAVE_SUBPATH


def list_save_files(save_root: str | os.PathLike[str] | None = None, *, steam_id: str | None = None) -> list[SaveFile]:
    """The pinned account's slots, or the account that saved last; newest first."""
    root = Path(save_root) if save_root is not None else default_save_root()
    try:
        paths = [path for path in root.glob("*/ff7rebirth[0-9][0-9][0-9].sav") if path.is_file()]
    except OSError:
        return []
    files = []
    for path in paths:
        match = _SLOT_FILE.match(path.stem)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if match:
            files.append(SaveFile(int(match.group(1)), str(path), mtime, path.parent.name))
    if not files:
        return []
    account = steam_id if steam_id is not None else max(files, key=lambda item: item.mtime).steam_id
    return sorted((item for item in files if item.steam_id == account), key=lambda item: -item.mtime)


def _alias_slots(aliases_path: Path | None, name: str, steam_id: str) -> list[int]:
    if aliases_path is None:
        return []
    try:
        aliases = json.loads(Path(aliases_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    slots = []
    for key, value in (aliases.items() if isinstance(aliases, dict) else []):
        match = re.fullmatch(r"(.+)-slot(\d{3})", str(key))
        if (match and isinstance(value, str) and value.strip().casefold() == name.casefold()
                and match.group(1) == steam_id):
            slots.append(int(match.group(2)))
    return sorted(set(slots))


def resolve_save_slot(slot: Any, *, saves: list[SaveFile] | None = None,
                      save_root: str | os.PathLike[str] | None = None,
                      aliases_path: str | os.PathLike[str] | None = None,
                      steam_id: str | None = None) -> dict[str, Any]:
    """A slot number (8, "008", "slot008"), "autosave", or a profile alias.

    An alias from ``slot-aliases.json`` may name the autosave as well as
    manual slots (live: proof_of_concept -> 000 and 008); the manual slot
    wins, and two manual slots are ambiguous.
    """
    saves = saves if saves is not None else list_save_files(save_root, steam_id=steam_id)
    if not saves:
        raise SaveSlotLoadError("save_files_not_found", save_root=str(save_root or default_save_root()))
    steam_id = saves[0].steam_id
    alias = None
    if isinstance(slot, bool) or slot is None:
        raise SaveSlotLoadError("save_slot_invalid", requested=slot)
    if isinstance(slot, int):
        number = slot
    else:
        text = str(slot).strip()
        folded = text.casefold()
        match = re.fullmatch(r"(?:slot\s*)?0*(\d{1,3})", folded) or re.fullmatch(r".+-slot(\d{3})", folded)
        if folded in {"autosave", "auto"}:
            number = 0
        elif match:
            number = int(match.group(1))
        else:
            candidates = _alias_slots(Path(aliases_path) if aliases_path else None, text, steam_id)
            manual = [value for value in candidates if value != 0]
            if len(manual) > 1:
                raise SaveSlotLoadError("save_slot_alias_ambiguous", requested=text, slots=manual)
            if not candidates:
                raise SaveSlotLoadError("save_slot_unknown", requested=text)
            number = manual[0] if manual else candidates[0]
            alias = text
    if not 0 <= number <= 999:
        raise SaveSlotLoadError("save_slot_invalid", requested=slot)
    found = next((item for item in saves if item.slot == number), None)
    if found is None:
        raise SaveSlotLoadError("save_slot_missing", requested=slot, slot=number,
                                available=[item.slot for item in saves])
    return {"slot": number, "requested": slot, "alias": alias, "file": found.summary(),
            "latest": saves[0].slot == number}


# ---------------------------------------------------------------------------
# Reading the menus


def _date_candidates(text: str) -> list[datetime]:
    match = _DATE_TIME.search(text)
    if not match:
        return []
    first, second, year, hour, minute, second_of_minute, meridiem = match.groups()
    hour_value = int(hour)
    if meridiem:
        if not 1 <= hour_value <= 12:
            return []
        hour_value = hour_value % 12 + (12 if meridiem.lower() == "p" else 0)
    candidates = []
    # The US text shows month/day; the other order is tried for a
    # differently localised build and must still match a file time.
    for month, day in ((int(first), int(second)), (int(second), int(first))):
        try:
            value = datetime(int(year), month, day, hour_value, int(minute), int(second_of_minute))
        except ValueError:
            continue
        if value not in candidates:
            candidates.append(value)
    return candidates


def _clock_seconds(text: str) -> int | None:
    match = _CLOCK.search(text)
    if not match:
        return None
    hour, minute, second, meridiem = match.groups()
    hour_value = int(hour)
    if not 1 <= hour_value <= 12:
        return None
    hour_value = hour_value % 12 + (12 if meridiem.lower() == "p" else 0)
    return hour_value * 3600 + int(minute) * 60 + int(second)


def match_save_time(text: str, saves: list[SaveFile], tolerance: float) -> tuple[list[int], str | None]:
    """Slots whose file time matches a date text, and how it was read."""
    candidates = _date_candidates(text)
    if candidates:
        slots = [item.slot for item in saves
                 if any(abs((item.saved_at - value).total_seconds()) <= tolerance for value in candidates)]
        return slots, "date_time"
    clock = _clock_seconds(text)
    if clock is None:
        return [], None
    slots = []
    for item in saves:
        stamp = item.saved_at
        seconds = stamp.hour * 3600 + stamp.minute * 60 + stamp.second
        difference = abs(seconds - clock) % 86400
        if min(difference, 86400 - difference) <= tolerance:
            slots.append(item.slot)
    return slots, "clock"


def _shape(path: str) -> str:
    return _DIGITS.sub("#", path)


class SaveMenuScreen:
    """The main menu and Save / Load texts in one snapshot."""

    def __init__(self, response: dict[str, Any], saves: list[SaveFile],
                 layout: SaveMenuLayout | None = None) -> None:
        self.response = response
        self.saves = saves
        self.layout = layout or SaveMenuLayout()
        self.texts = [(str(item.get("path", "")), str(item.get("text", "")))
                      for item in response.get("texts") or [] if isinstance(item, dict)]
        self.signals = {meaning for _, text in self.texts
                        if (meaning := _meaning(SIGNALS, text)) is not None}
        self.main_menu_help = sorted({meaning for _, text in self.texts
                                      if (meaning := _meaning(MAIN_MENU_HELP, text)) is not None})
        self.save_load_help = sorted({meaning for _, text in self.texts
                                      if (meaning := _meaning(SAVE_LOAD_HELP, text)) is not None})
        self._read_rows()

    # Cursor positions -------------------------------------------------

    @property
    def main_menu_item(self) -> str | None:
        return self.main_menu_help[0] if len(self.main_menu_help) == 1 else None

    @property
    def save_load_choice(self) -> str | None:
        return self.save_load_help[0] if len(self.save_load_help) == 1 else None

    @property
    def load_list_open(self) -> bool:
        return "load_list" in self.signals or (bool(self.rows) and "save_list" not in self.signals)

    @property
    def save_list_open(self) -> bool:
        return "save_list" in self.signals

    @property
    def load_prompt_shown(self) -> bool:
        return "load_prompt" in self.signals

    @property
    def save_prompt_shown(self) -> bool:
        return "save_prompt" in self.signals

    @property
    def title_selection(self) -> dict[str, Any] | None:
        """Only a complete selected child linked to a visible EndStartMenu.

        The exporter proves the owner with IsA against the SDK class. Neither
        a visible Load Game label elsewhere nor a presumed menu offset counts.
        """
        discovery = self.response.get("save_list_discovery")
        if (not isinstance(discovery, dict) or discovery.get("scan_truncated") is not False
                or discovery.get("records_truncated") is not False):
            return None
        entries = self.response.get("title_lists")
        if not isinstance(entries, list) or len(entries) > 24:
            return None
        active = [item for item in entries if isinstance(item, dict) and item.get("visible") is True
                  and item.get("title_owner_visible") is True]
        if len(active) != 1:
            return None
        entry = active[0]
        nodes, walk = entry.get("selected_nodes"), entry.get("selected_node_walk")
        selected, owner = entry.get("selected_child_id"), entry.get("title_owner_id")
        if (type(entry.get("user_index")) is not int or entry["user_index"] != 0 or type(entry.get("selected_index")) is not int
                or entry["selected_index"] < 0 or not isinstance(selected, str) or not selected
                or not isinstance(owner, str) or owner not in (entry.get("owners") or [])
                or not isinstance(nodes, list) or not nodes or len(nodes) > 128
                or not isinstance(walk, dict) or walk.get("truncated") is not False
                or type(walk.get("visited")) is not int or walk["visited"] != len(nodes)
                or walk.get("node_limit") != 128
                or any(not isinstance(node, dict) for node in nodes)
                or nodes[0].get("full_id") != selected or nodes[0].get("visible") is not True):
            return None
        labels = sorted({_normalise(node["text"]) for node in nodes
                         if node.get("visible") is True and isinstance(node.get("text"), str) and node["text"].strip()})
        if not labels:
            return None
        load_labels = {_normalise(value).casefold() for value in self.layout.title_load_texts}
        # A selected list/container containing several options is not a
        # selected Load Game cell, even if that label occurs somewhere in it.
        other_options = {"$menu_title_0001_0000", "$menu_title_0001_0001", "$menu_title_0001_0004",
                         "$menu_title_0001_0005", "new game", "continue", "options", "quit"}
        if any(label.casefold() in load_labels for label in labels) and any(label.casefold() in other_options for label in labels):
            return None
        return {"id": selected, "labels": labels,
                "load_selected": any(label.casefold() in load_labels for label in labels)}

    @property
    def text_cap_reached(self) -> bool:
        return len(self.texts) >= TEXT_CAP

    # Load-list rows ------------------------------------------------------

    def _read_rows(self) -> None:
        row_filter = re.compile(self.layout.row_path_pattern) if self.layout.row_path_pattern else None
        occurrences: list[dict[str, Any]] = []
        self.unmatched_dates: list[dict[str, Any]] = []
        self.ambiguous_dates: list[dict[str, Any]] = []
        for path, raw in self.texts:
            text = _normalise(raw)
            if row_filter is not None and not row_filter.search(path):
                continue
            slots, basis = match_save_time(text, self.saves, self.layout.date_tolerance_seconds)
            if basis is None:
                continue
            if len(slots) == 1:
                occurrences.append({"slot": slots[0], "path": path, "shape": _shape(path),
                                    "text": text, "basis": basis})
            elif slots:
                self.ambiguous_dates.append({"path": path, "text": text, "slots": slots})
            else:
                self.unmatched_dates.append({"path": path, "text": text})
        self.occurrences = occurrences
        self.rows = sorted({item["slot"] for item in occurrences},
                           key=lambda slot: self._rank().get(slot, 10**6))
        self.highlighted_slot, self.highlight_basis = self._highlight()
        self.slot_number_path = self._slot_number_path()

    def _hovered_row(self) -> int | None:
        """The save whose row button the game reports hovered.

        Live 2026-10-05 the Load list marks its selection only by drawing it;
        the one row whose button is hovered is the selected one (it moved
        with the cursor), and no text or other property differs. The row's
        own date and time name the save. None unless exactly one row is
        hovered and it names exactly one save.
        """
        cells = self.response.get("save_cells")
        if not isinstance(cells, list):
            return None
        hovered = [cell for cell in cells if isinstance(cell, dict) and any(
            isinstance(node, dict) and "Button" in str(node.get("class", "")) and node.get("hovered") is True
            for node in cell.get("nodes") or [])]
        if len(hovered) != 1:
            return None
        texts = {str(node.get("name")): str(node.get("text"))
                 for node in hovered[0].get("nodes") or [] if isinstance(node, dict) and node.get("text")}
        date, clock = texts.get("Txt_Date"), texts.get("Txt_Time")
        if not date or not clock:
            return None
        slots, _ = match_save_time(f"{_normalise(date)} {_normalise(clock)}", self.saves,
                                   self.layout.date_tolerance_seconds)
        return slots[0] if len(slots) == 1 else None

    def _rank(self) -> dict[int, int]:
        return {item.slot: index for index, item in enumerate(self.saves)}

    def _highlight(self) -> tuple[int | None, str | None]:
        # The reviewed virtual-scroll contract, where the title list shares
        # the field widget. It binds a selected child directly, never index+1.
        from .save_selection import CONTRACT, selected_cell
        try:
            cell, _ = selected_cell(self.response, CONTRACT)
            texts = {node.get("name"): node.get("text") for node in cell["nodes"]
                     if node.get("visible") is True and node.get("text")}
            slots, _ = match_save_time(f"{texts.get('Txt_Date', '')} {texts.get('Txt_Time', '')}",
                                       self.saves, self.layout.date_tolerance_seconds)
            if len(slots) == 1:
                return slots[0], "selected_child"
        except SaveSlotLoadError:
            pass
        hovered = self._hovered_row()
        if hovered is not None:
            return hovered, "hovered_row"
        if not self.occurrences:
            return None, None
        if self.layout.highlight_path_pattern:
            marker = re.compile(self.layout.highlight_path_pattern)
            slots = {item["slot"] for item in self.occurrences if marker.search(item["path"])}
            return (slots.pop(), "highlight_path_pattern") if len(slots) == 1 else (None, None)
        # Rows share one widget path once instance numbers are dropped; the
        # highlighted save is the one whose date also appears elsewhere.
        shapes = Counter(item["shape"] for item in self.occurrences)
        row_shape, count = shapes.most_common(1)[0]
        row_slots = {item["slot"] for item in self.occurrences if item["shape"] == row_shape}
        if count < 2 or len(row_slots) < 2:
            return None, None
        focused = {item["slot"] for item in self.occurrences if item["shape"] != row_shape}
        return (focused.pop(), "detail_occurrence") if len(focused) == 1 else (None, None)

    def _slot_number_path(self) -> str | None:
        """The widget whose numbers are exactly the visible manual slots."""
        expected = {slot for slot in self.rows if slot != 0}
        if not expected:
            return None
        groups: dict[str, set[int]] = {}
        for path, raw in self.texts:
            match = _SLOT_NUMBER.match(_normalise(raw))
            if match:
                groups.setdefault(_shape(path), set()).add(int(match.group(1)))
        paths = sorted(shape for shape, values in groups.items() if values == expected)
        return paths[0] if len(paths) == 1 else None

    def summary(self) -> dict[str, Any]:
        return {
            "main_menu_item": self.main_menu_item,
            "title_selection": self.title_selection,
            "main_menu_help": self.main_menu_help,
            "save_load_choice": self.save_load_choice,
            "signals": sorted(self.signals),
            "load_list_open": self.load_list_open,
            "rows": self.rows,
            "highlighted_slot": self.highlighted_slot,
            "highlight_basis": self.highlight_basis,
            "slot_number_path": self.slot_number_path,
            "unmatched_dates": self.unmatched_dates[:10],
            "ambiguous_dates": self.ambiguous_dates[:10],
            "text_count": len(self.texts),
            "text_cap_reached": self.text_cap_reached,
        }


# ---------------------------------------------------------------------------
# Driving the menus


def _position(sample: dict[str, Any]) -> dict[str, Any] | None:
    position = (sample.get("player") or {}).get("position")
    return dict(position) if isinstance(position, dict) else None


def _distance_cm(first: Any, second: Any) -> float | None:
    try:
        return sum((float(second[axis]) - float(first[axis])) ** 2 for axis in ("x", "y", "z")) ** 0.5
    except (KeyError, TypeError, ValueError):
        return None


class SaveSlotLoader:
    def __init__(self, service: Any, *, layout: SaveMenuLayout | None = None,
                 save_root: str | os.PathLike[str] | None = None,
                 aliases_path: str | os.PathLike[str] | None = None,
                 steam_id: str | None = None, from_title: bool = False,
                 before_input: Callable[[], None] | None = None) -> None:
        self.service = service
        self.layout = layout
        self.save_root = save_root
        self.aliases_path = aliases_path
        self.steam_id = steam_id
        self.from_title = from_title
        self.before_input = before_input
        self.load_confirmation_sent = False
        self.sleep: Callable[[float], None] = getattr(service, "_sleeper", time.sleep)
        self.clock: Callable[[], float] = getattr(service, "_monotonic", time.monotonic)
        self.started = self.clock()
        self.saves: list[SaveFile] = []
        self.presses: list[dict[str, Any]] = []
        self.screens: list[dict[str, Any]] = []
        self.last_texts: list[dict[str, str]] = []
        self.menu_touched = False
        self.load_started = False
        # Set when a load may be in progress: then nothing more is pressed.
        self.uncertain = False

    # Reading -----------------------------------------------------------

    def read(self, stage: str) -> SaveMenuScreen:
        try:
            # Row widgets wherever the Load list may be up, including the
            # re-check before confirming (live 2026-10-05 it read no
            # highlight there and refused as load_list_highlight_moved).
            response, _ = menu_snapshot(self.service, rows=stage not in ("main_menu", "save_load"), title=self.from_title)
        except MissionError as exc:
            raise SaveSlotLoadError("save_menu_unreadable", stage=stage, cause=str(exc)) from exc
        screen = SaveMenuScreen(response, self.saves, self.layout)
        self.last_texts = [{"path": path, "text": text} for path, text in screen.texts]
        if len(self.screens) < MAX_SCREEN_RECORDS:
            self.screens.append({"stage": stage, "t": round(self.clock() - self.started, 2), **screen.summary()})
        return screen

    def wait_for(self, predicate: Callable[[SaveMenuScreen], bool], timeout: float,
                 stage: str) -> SaveMenuScreen | None:
        deadline = self.clock() + timeout
        screen = None
        while True:
            try:
                screen = self.read(stage)
            except SaveSlotLoadError:
                screen = None
            if screen is not None and predicate(screen):
                return screen
            if self.clock() >= deadline:
                return screen
            self.sleep(POLL_SECONDS)

    # Input -------------------------------------------------------------

    def press(self, button: str, why: str, *, milliseconds: int = KEY_MS) -> None:
        if self.before_input is not None:
            self.before_input()
        self.service._map_menu_key(button, milliseconds)
        self.presses.append({"button": button, "why": why, "t": round(self.clock() - self.started, 2)})
        self.sleep(PRESS_GAP_SECONDS)

    def in_menu(self) -> bool:
        return bool(self.service.observe().get("in_menu"))

    def close_menus(self, why: str) -> bool:
        if not self.in_menu():
            return True
        for _ in range(MAX_MENU_CLOSES):
            self.press("menu", why)
            self.sleep(MENU_CLOSE_SETTLE_SECONDS)
            if self.menu_flag_cleared():
                return True
        return not self.in_menu()

    @staticmethod
    def _save_ui_visible(screen: SaveMenuScreen) -> bool:
        return bool(
            screen.main_menu_item is not None
            or screen.save_load_choice is not None
            or screen.signals.intersection({"load_list", "save_list", "load_prompt", "save_prompt"})
        )

    def menu_flag_cleared(self) -> bool:
        """Whether telemetry stops saying "in menu" within the settle bound."""
        deadline = self.clock() + MENU_FLAG_CLEAR_SECONDS
        while self.in_menu():
            if self.clock() >= deadline:
                return False
            self.sleep(POLL_SECONDS)
        return True

    def close_save_ui(self, why: str) -> bool:
        """Back out of save/load UI, verifying each state until the field returns."""
        for _ in range(MAX_MENU_CLOSES + 2):
            screen = self.read(f"{why}_verify")
            # The flag lags the screen as the menu closes: another press then
            # reopened the main menu (live 2026-10-05).
            if not self._save_ui_visible(screen) and self.menu_flag_cleared():
                return True
            self.press("menu", why)
            self.sleep(MENU_CLOSE_SETTLE_SECONDS)
        return not self._save_ui_visible(self.read(f"{why}_final_verify")) and not self.in_menu()

    # Steps -------------------------------------------------------------

    def close_title_ui(self, why: str) -> bool:
        for _ in range(MAX_MENU_CLOSES):
            screen = self.read("title_menu")
            # A readable title cursor means the title menu has focus. The
            # closed Load list keeps its rows and load_list signal (live
            # 2026-10-20), and a Cancel here opens "Quit the game?" with Yes
            # preselected, so never press it while the cursor is shown.
            if (screen.title_selection is not None and screen.main_menu_item is None
                    and screen.save_load_choice is None):
                return True
            if not self._save_ui_visible(screen) and not screen.rows:
                return False  # Unknown/blank UI never authorizes a cancel.
            self.press("cancel", why)
            self.sleep(MENU_CLOSE_SETTLE_SECONDS)
        return False

    def _open_title_load_list(self) -> SaveMenuScreen:
        screen = self.read("title_menu")
        for _ in range(12):
            selected = screen.title_selection
            if selected is None or self._save_ui_visible(screen) or screen.rows:
                raise SaveSlotLoadError("title_menu_selection_unverified")
            if selected["load_selected"]:
                again = self.read("title_menu")
                if again.title_selection != selected or self._save_ui_visible(again) or again.rows:
                    raise SaveSlotLoadError("title_menu_selection_unsettled")
                self.menu_touched = True
                self.uncertain = True
                self.press("confirm", "title_menu:load_game")
                opened = self.wait_for(lambda s: s.save_list_open or (s.load_list_open and bool(s.rows)),
                                       MENU_OPEN_WAIT_SECONDS, "load_list")
                if opened is not None and (self._save_ui_visible(opened) or opened.rows or opened.title_selection is not None):
                    self.uncertain = False
                if opened is not None and opened.save_list_open:
                    raise SaveSlotLoadError("save_list_opened_instead")
                if opened is None or not opened.load_list_open or not opened.rows:
                    raise SaveSlotLoadError("title_load_list_unverified")
                return opened
            self.menu_touched = True
            self.press("down", "title_menu:seek_load_game")
            screen = self.wait_for(lambda s: s.title_selection is not None and s.title_selection != selected,
                                   CHANGE_WAIT_SECONDS, "title_menu")
            if screen is None or screen.title_selection == selected:
                raise SaveSlotLoadError("title_menu_cursor_stalled")
        raise SaveSlotLoadError("title_load_game_not_found")

    def _focus(self) -> None:
        focus = getattr(self.service, "_ensure_game_foreground", None)
        if callable(focus):
            focus(force=True)
            deadline = self.clock() + FOCUS_TELEMETRY_WAIT_SECONDS
            while (not (self.service.observe().get("telemetry") or {}).get("live")
                   and self.clock() < deadline):
                self.sleep(0.25)

    def _require_field(self) -> dict[str, Any]:
        health = self.service.session_health()
        if health.get("in_combat") or health.get("target_clear") is False:
            raise SaveSlotLoadError("load_blocked_in_combat", health=health)
        if health.get("in_menu") or health.get("paused"):
            self.menu_touched = True
            if not self.close_menus("close_open_menu"):
                raise SaveSlotLoadError("open_menu_did_not_close")
        deadline = self.clock() + FIELD_WAIT_SECONDS
        while True:
            health = self.service.session_health()
            if health.get("loaded_gameplay") and not health.get("in_menu"):
                return health
            if self.clock() >= deadline:
                raise SaveSlotLoadError("load_requires_field_gameplay", health=health)
            self.sleep(0.5)

    def _open_main_menu(self) -> SaveMenuScreen:
        screen = None
        for attempt in range(MENU_OPEN_ATTEMPTS):
            self.menu_touched = True
            self.press("menu", "open_main_menu", milliseconds=MENU_OPEN_KEY_MS)
            screen = self.wait_for(lambda s: s.main_menu_item is not None, MENU_OPEN_WAIT_SECONDS, "main_menu")
            if screen is not None and screen.main_menu_item is not None:
                return self._settled_main_menu(screen)
            if self.in_menu():
                break  # open, but its cursor cannot be read: no blind presses
        raise SaveSlotLoadError("main_menu_cursor_unreadable")

    def _settled_main_menu(self, screen: SaveMenuScreen) -> SaveMenuScreen:
        """The cursor once two reads agree: moves are now pressed together.

        Live 2026-10-05 the first read as the menu opened named Item
        Transmuter while the cursor was on Save / Load, where it had been
        left, so two presses meant for Save / Load went on to Party.
        """
        for _ in range(MENU_SETTLE_READS):
            again = self.read("main_menu")
            if again.main_menu_item is not None and again.main_menu_item == screen.main_menu_item:
                return again
            if again.main_menu_item is not None:
                screen = again
            self.sleep(POLL_SECONDS)
        raise SaveSlotLoadError("main_menu_cursor_unsettled", item=screen.main_menu_item)

    def _select_main_item(self, screen: SaveMenuScreen, target: str) -> SaveMenuScreen:
        order = list(self.layout.main_menu_order)
        size = len(order)
        stalls = 0
        for _ in range(2 * size + 2):
            item = screen.main_menu_item
            if item == target:
                return screen
            if item is None:
                raise SaveSlotLoadError("main_menu_cursor_unreadable")
            if item not in order:
                raise SaveSlotLoadError("main_menu_item_unknown", item=item)
            ahead = (order.index(target) - order.index(item)) % size
            button = "down" if ahead <= size - ahead else "up"
            # Every press to the target, then one verifying read: a read per
            # press made menu navigation slow (the owner, 2026-10-05). A
            # dropped press is corrected on the next pass.
            for _ in range(min(ahead, size - ahead)):
                self.press(button, f"main_menu:{item}->{target}")
            moved = self.wait_for(lambda s, before=item: s.main_menu_item not in (None, before),
                                  CHANGE_WAIT_SECONDS, "main_menu")
            if moved is None or moved.main_menu_item in (None, item):
                stalls += 1
                if stalls > MAX_STALLS:
                    raise SaveSlotLoadError("main_menu_cursor_stalled", item=item)
                if moved is None or moved.main_menu_item is None:
                    raise SaveSlotLoadError("main_menu_cursor_unreadable")
            screen = moved
        raise SaveSlotLoadError("main_menu_target_not_reached", target=target)

    def _open_load_list(self) -> SaveMenuScreen:
        self.press("confirm", "open_save_load")
        screen = self.wait_for(lambda s: s.save_load_choice is not None, MENU_OPEN_WAIT_SECONDS, "save_load")
        if screen is None or screen.save_load_choice is None:
            raise SaveSlotLoadError("save_load_submenu_unreadable")
        for _ in range(MAX_SUBMENU_PRESSES):
            if screen.save_load_choice == "load":
                break
            self.press("down", "save_load:save->load")
            moved = self.wait_for(lambda s: s.save_load_choice == "load", CHANGE_WAIT_SECONDS, "save_load")
            if moved is None or moved.save_load_choice is None:
                raise SaveSlotLoadError("save_load_submenu_unreadable")
            screen = moved
        if screen.save_load_choice != "load":
            raise SaveSlotLoadError("save_load_cursor_not_on_load")
        self.press("confirm", "open_load_list")
        screen = self.wait_for(lambda s: s.save_list_open or (s.load_list_open and bool(s.rows)),
                               MENU_OPEN_WAIT_SECONDS, "load_list")
        if screen is not None and screen.save_list_open:
            raise SaveSlotLoadError("save_list_opened_instead")
        if screen is None or not screen.load_list_open:
            raise SaveSlotLoadError("load_list_did_not_open")
        if not screen.rows:
            raise SaveSlotLoadError("load_list_rows_unreadable")
        return screen

    def _highlight(self, screen: SaveMenuScreen, target: int) -> SaveMenuScreen:
        rank = {item.slot: index for index, item in enumerate(self.saves)}
        stalls = 0
        visited: list[int] = []
        for _ in range(len(self.saves) + 4):
            current = screen.highlighted_slot
            if current is None:
                raise SaveSlotLoadError("load_list_highlight_unreadable", rows=screen.rows)
            if current == target:
                return screen
            if current in visited[:-1]:
                # Back on a row already left: the list is not ordered like
                # the files (newest first), so the target's side is unknown.
                raise SaveSlotLoadError("load_list_order_mismatch", visited=visited + [current])
            if not visited or visited[-1] != current:
                visited.append(current)
            button = "down" if rank[target] > rank[current] else "up"
            self.press(button, f"load_list:{current}->{target}")
            moved = self.wait_for(lambda s, before=current: s.highlighted_slot not in (None, before),
                                  CHANGE_WAIT_SECONDS, "load_list")
            after = moved.highlighted_slot if moved is not None else None
            if after is None:
                raise SaveSlotLoadError("load_list_highlight_unreadable",
                                        rows=moved.rows if moved is not None else [])
            if after == current:
                stalls += 1
                if stalls > MAX_STALLS:
                    raise SaveSlotLoadError("load_list_cursor_stalled", slot=current)
            elif (rank[after] - rank[current]) * (1 if button == "down" else -1) < 0:
                # A wrap or an order unlike the files': stop rather than guess.
                raise SaveSlotLoadError("load_list_order_mismatch", before=current, after=after, pressed=button)
            screen = moved
        raise SaveSlotLoadError("load_list_target_not_reached", target=target)

    def _load_evidence(self, before: dict[str, Any], stale_since: list[float | None]) -> str | None:
        health = self.service.session_health()
        if health.get("process_exists") is False:
            self.uncertain = True  # never try to close menus in an exited game
            raise SaveSlotLoadError("game_exited_during_load", health=health)
        if health.get("game_running") is False:
            return "game_not_running"
        if health.get("telemetry_live") is False:
            if stale_since[0] is None:
                stale_since[0] = self.clock()
            elif self.clock() - stale_since[0] >= LOAD_START_STALE_SECONDS:
                return "telemetry_stale"
            return None
        stale_since[0] = None
        if not health.get("player_spatial") or not health.get("map_id"):
            return str(health.get("diagnosis") or "player_not_loaded")
        if before.get("map_id") and health.get("map_id") != before.get("map_id"):
            return "map_changed"
        return None

    def _wait_load_start(self, before: dict[str, Any]) -> str | None:
        deadline = self.clock() + LOAD_START_WAIT_SECONDS
        stale_since: list[float | None] = [None]
        missing_ui_reads = 0
        while True:
            if self.from_title:
                health = self.service.session_health()
                if health.get("process_exists") is False:
                    self.uncertain = True
                    raise SaveSlotLoadError("game_exited_during_load", health=health)
                # An absent player is the title screen's normal state. Prove
                # the confirmed UI left, or that gameplay actually arrived.
                screen = self.read("title_load_transition")
                missing = not self._save_ui_visible(screen) and not screen.rows and screen.title_selection is None
                missing_ui_reads = missing_ui_reads + 1 if missing else 0
                # SaveIcon's hidden checking label is present even in gameplay.
                # Require the verified title/load UI to disappear twice.
                evidence = "title_load_ui_closed" if missing_ui_reads >= 2 else None
            else:
                evidence = self._load_evidence(before, stale_since)
            if evidence is not None:
                self.load_started = True
                return evidence
            if self.clock() >= deadline:
                return None
            self.sleep(0.5)

    def _confirm_load(self, target: int, before: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(1, LOAD_CONFIRM_ATTEMPTS + 1):
            screen = self.read("before_load_prompt")
            if screen.save_prompt_shown or screen.save_list_open:
                raise SaveSlotLoadError("save_screen_detected")
            if screen.load_prompt_shown:
                # Not opened by this attempt (or its text lingers): unsafe.
                raise SaveSlotLoadError("load_prompt_state_ambiguous")
            if not screen.load_list_open or screen.highlighted_slot != target:
                raise SaveSlotLoadError("load_list_highlight_moved", highlighted=screen.highlighted_slot)
            if self.from_title:
                # Live title loading starts immediately on the selected row,
                # without the field's unsaved-progress confirmation. Fingerprint
                # the verified file/process BEFORE that potentially loading press.
                begin = getattr(self.service, "begin_save_load", None)
                if callable(begin) and getattr(self, "loaded_save_candidate", None) is None:
                    file = next(item.summary() for item in self.saves if item.slot == target)
                    self.loaded_save_candidate = begin(file)
                self.load_confirmation_sent = True
                self.uncertain = True
            self.press("confirm", f"open_load_prompt:{target}")
            prompt = self.wait_for(lambda s: s.load_prompt_shown or s.save_prompt_shown,
                                   CHANGE_WAIT_SECONDS, "load_prompt")
            if prompt is not None and prompt.save_prompt_shown:
                self.press("menu", "decline_save_prompt")
                raise SaveSlotLoadError("save_screen_detected")
            if prompt is None or not prompt.load_prompt_shown:
                if self.from_title:
                    evidence = self._wait_load_start(before)
                    if evidence is not None:
                        return {"attempts": attempt, "load_evidence": evidence, "prompt_shown": False}
                raise SaveSlotLoadError("load_prompt_not_shown")
            if self.from_title:
                self.uncertain = False
            # No is preselected; Up moves to Yes.
            self.press("up", "load_prompt:no->yes")
            begin = getattr(self.service, "begin_save_load", None)
            if callable(begin) and (not self.from_title or getattr(self, "loaded_save_candidate", None) is None):
                file = next(item.summary() for item in self.saves if item.slot == target)
                self.loaded_save_candidate = begin(file)
            self.load_confirmation_sent = True
            self.press("confirm", "load_prompt:yes")
            evidence = self._wait_load_start(before)
            if evidence is not None:
                return {"attempts": attempt, "load_evidence": evidence}
            try:
                after = self.read("after_load_prompt")
            except SaveSlotLoadError:
                after = None
            if after is None or "checking" in after.signals or not after.load_list_open:
                # Possibly loading after all: wait again, and never press
                # anything into a load in progress.
                evidence = self._wait_load_start(before)
                if evidence is not None:
                    return {"attempts": attempt, "load_evidence": evidence}
                self.uncertain = True
                raise SaveSlotLoadError("load_state_unknown")
            if after.load_prompt_shown:
                raise SaveSlotLoadError("load_prompt_still_shown")
            if after.highlighted_slot != target:
                raise SaveSlotLoadError("load_list_highlight_moved", highlighted=after.highlighted_slot)
            # The prompt closed on the target row without a load: No was
            # confirmed (Up was lost or wrapped). Retry from the row.
        raise SaveSlotLoadError("load_not_started", attempts=LOAD_CONFIRM_ATTEMPTS)

    def _await_arrival(self) -> dict[str, Any]:
        deadline = self.clock() + ARRIVAL_WAIT_SECONDS
        while True:
            health = self.service.session_health()
            if health.get("process_exists") is False:
                raise SaveSlotLoadError("game_exited_during_load", health=health)
            if health.get("loaded_gameplay") and not health.get("in_menu"):
                break
            if self.clock() >= deadline:
                raise SaveSlotLoadError("load_arrival_timeout", health=health)
            self.sleep(1.0)
        try:
            return self.service.wait_for_settled_gameplay(ARRIVAL_WAIT_SECONDS)
        except Exception as exc:
            raise SaveSlotLoadError("load_settle_failed", cause=str(exc)) from exc

    # Operation ---------------------------------------------------------

    def run(self, slot: Any, execute: bool = False) -> dict[str, Any]:
        report: dict[str, Any] = {"requested": slot, "execute": bool(execute),
                                  "presses": self.presses, "screens": self.screens}
        try:
            if self.layout is None:
                self.layout = load_layout()
            report["layout"] = {"source": self.layout.source,
                                "highlight_path_pattern": self.layout.highlight_path_pattern,
                                "row_path_pattern": self.layout.row_path_pattern}
            self.saves = list_save_files(self.save_root, steam_id=self.steam_id)
            target = resolve_save_slot(slot, saves=self.saves, save_root=self.save_root,
                                       aliases_path=self.aliases_path)
            report.update(slot=target["slot"], alias=target["alias"], save_file=target["file"],
                          saves=[item.summary() for item in self.saves])
            self._focus()
            health = self.service.session_health() if self.from_title else self._require_field()
            start = self.service.observe()
            before = {"map_id": start.get("map_id") or health.get("map_id"), "position": _position(start)}
            report["start"] = before
            if self.from_title:
                screen = self._open_title_load_list()
            else:
                screen = self._open_main_menu()
                screen = self._select_main_item(screen, SAVE_LOAD_ITEM)
                screen = self._open_load_list()
            # The autosave can have been rewritten since the files were
            # listed; the list shows the files as they are now.
            self.saves = list_save_files(self.save_root, steam_id=self.steam_id)
            screen = self.read("load_list")
            screen = self._highlight(screen, target["slot"])
            report["highlighted"] = screen.summary()
            if not execute:
                if not (self.close_title_ui("dry_run_close") if self.from_title else self.close_menus("dry_run_close")):
                    raise SaveSlotLoadError("menus_did_not_close")
                report["closed"] = True
                report.update(ok=True, state="dry_run_highlighted", phase="dry_run_highlighted")
                return report
            report["confirm"] = self._confirm_load(target["slot"], before)
            report["settled"] = self._await_arrival()
            arrival = self.service.observe()
            report["arrival"] = {"map_id": arrival.get("map_id"), "position": _position(arrival)}
            moved = _distance_cm(before.get("position"), report["arrival"]["position"])
            report["moved_cm"] = moved
            # A save made where Cloud stands loads without moving him; the
            # load itself was shown by player_not_loaded (or a map change).
            report["position_changed"] = moved is not None and moved > ARRIVAL_MOVED_CM
            complete = getattr(self.service, "complete_save_load", None)
            if callable(complete):
                report["loaded_save"] = complete(getattr(self, "loaded_save_candidate", None))
                if not report["loaded_save"].get("verified"):
                    raise SaveSlotLoadError("loaded_save_receipt_unverified", receipt=report["loaded_save"])
            report.update(ok=True, state="loaded", phase="gameplay_ready")
            return report
        except Exception as exc:
            # Input refusals (focus lost, session) end the operation the same
            # way: with its evidence and the menus closed where safe.
            if isinstance(exc, SaveSlotLoadError):
                report.update(decision=exc.code, details=exc.details)
            else:
                report.update(decision="load_save_slot_error", details={
                    "error_type": type(exc).__name__, "error": str(exc)})
            report.update(ok=False, state="failed", phase="load_failed", last_texts=self.last_texts)
            if self.menu_touched and not self.load_started and not self.uncertain and not (self.from_title and self.load_confirmation_sent):
                try:
                    report["closed"] = (self.close_title_ui("close_after_failure") if self.from_title
                                        else self.close_save_ui("close_after_failure"))
                except Exception as close_exc:  # never mask the first failure
                    report["close_error"] = str(close_exc)
            if self.from_title:
                # Startup may use Continue/field loading only before any load
                # confirmation and after a positively verified title return.
                recoverable = report["decision"] in {
                    "title_menu_selection_unverified", "title_menu_selection_unsettled", "title_menu_cursor_stalled",
                    "title_load_game_not_found", "title_load_list_unverified", "load_list_highlight_unreadable",
                    "load_list_order_mismatch", "load_list_target_not_reached", "load_list_cursor_stalled",
                    "save_menu_unreadable", "load_list_highlight_moved", "load_prompt_not_shown",
                }
                report["fallback_allowed"] = (recoverable and not self.load_confirmation_sent and not self.load_started
                                              and not self.uncertain and (not self.menu_touched or report.get("closed") is True))
            return report


def load_save_slot(service: Any, slot: Any, execute: bool = False, steam_id: str | None = None) -> dict[str, Any]:
    return SaveSlotLoader(service, aliases_path=aliases_path_for(service), steam_id=steam_id).run(slot, execute)


def load_title_save_slot(service: Any, slot: Any, execute: bool = False, steam_id: str | None = None,
                         before_input: Callable[[], None] | None = None) -> dict[str, Any]:
    return SaveSlotLoader(service, aliases_path=aliases_path_for(service), steam_id=steam_id,
                          from_title=True, before_input=before_input).run(slot, execute)


def aliases_path_for(service: Any) -> Path:
    root = getattr(service, "_queen_blood_profile_root", None)
    if root is None:
        root = Path(os.environ.get("LOCALAPPDATA", ".")) / "ff7r-cait-gpt" / "queen-blood"
    return Path(root) / "slot-aliases.json"


def read_save_menu(service: Any, include_texts: bool = True) -> dict[str, Any]:
    """The save files and what one snapshot reads of the menus; no input."""
    try:
        layout = load_layout()
    except SaveSlotLoadError as exc:
        return {"ok": False, "decision": exc.code, "details": exc.details}
    saves = list_save_files()
    try:
        response, _ = menu_snapshot(service, rows=True, title=True)
    except MissionError as exc:
        return {"ok": False, "decision": "save_menu_unreadable", "cause": str(exc),
                "saves": [item.summary() for item in saves]}
    screen = SaveMenuScreen(response, saves, layout)
    result: dict[str, Any] = {
        "ok": True,
        "saves": [item.summary() for item in saves],
        "screen": screen.summary(),
        "occurrences": screen.occurrences,
        "layout_source": layout.source,
        "source_sequence": response.get("source_sequence"),
    }
    if include_texts:
        result["texts"] = [{"path": path, "text": text} for path, text in screen.texts]
        result["save_cells"] = response.get("save_cells", [])
        result["save_dialogs"] = response.get("save_dialogs", [])
        result["save_lists"] = response.get("save_lists", [])
        result["title_lists"] = response.get("title_lists", [])
        result["save_list_discovery"] = response.get("save_list_discovery")
        result["frame"] = {key: response.get(key) for key in ("request_id", "schema_version", "source", "source_sequence")}
    return result
