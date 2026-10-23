import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from ff7r_agent import save_slot_load
from ff7r_agent.save_slot_load import (
    MAIN_MENU_ORDER,
    SaveMenuLayout,
    SaveMenuScreen,
    SaveSlotLoadError,
    SaveSlotLoader,
    list_save_files,
    load_layout,
    match_save_time,
    resolve_save_slot,
)

STEAM = "00000000000000000"
# Slot -> save time; the list shows them newest first (live 2026-09-28:
# 11, autosave, 08, 07, 04, 10, ...).
SAVE_TIMES = {
    11: datetime(2026, 9, 27, 22, 27, 4),
    0: datetime(2026, 9, 27, 20, 0, 0),
    8: datetime(2026, 9, 18, 22, 18, 57),
    7: datetime(2026, 9, 18, 11, 16, 47),
    4: datetime(2026, 9, 18, 10, 54, 25),
    10: datetime(2026, 9, 17, 23, 37, 52),
    9: datetime(2026, 9, 17, 22, 3, 42),
    5: datetime(2026, 9, 13, 10, 45, 23),
    6: datetime(2026, 8, 16, 14, 50, 34),
    3: datetime(2026, 7, 25, 22, 50, 11),
    1: datetime(2026, 4, 7, 18, 33, 59),
    2: datetime(2025, 1, 24, 6, 23, 59),
}
HELP = {
    "item_transmuter": "$help_message_0000_0011", "materia_equipment": "$help_message_0000_0000",
    "combat_settings": "$help_message_0000_0001", "enemy_intel": "$help_message_0000_0004",
    "upgrade_weapons": "$help_message_0000_0002", "spells": "$help_message_0000_0006",
    "inventory": "$help_message_0000_0007", "card_decks": "$help_message_0000_0012",
    "party": "$help_message_0000_0005", "manual": "$help_message_0000_0008",
    "save_load": "$help_message_0000_0009", "system": "$help_message_0000_0010",
}
ROW = "X.MainMenu_SaveLoad.WidgetTree.List.SaveDataCell.WidgetTree."
DETAIL = "X.MainMenu_SaveLoad.WidgetTree.Detail.WidgetTree.Txt_Date"
PROMPT = "X.Com_Window_01.WidgetTree.Txt_Message"


def shown(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    meridiem = "p.m." if moment.hour >= 12 else "a.m."
    return f"{moment:%m/%d/%Y} {hour}:{moment:%M:%S} {meridiem}"


@pytest.fixture
def save_root(tmp_path):
    folder = tmp_path / "Steam" / STEAM
    folder.mkdir(parents=True)
    for slot, moment in SAVE_TIMES.items():
        path = folder / f"ff7rebirth{slot:03d}.sav"
        path.write_bytes(b"x")
        stamp = moment.timestamp()
        os.utime(path, (stamp, stamp))
    (folder / "ff7rebirthcommon.sav").write_bytes(b"x")
    return tmp_path / "Steam"


@pytest.fixture
def aliases(tmp_path):
    path = tmp_path / "slot-aliases.json"
    path.write_text(json.dumps({
        f"{STEAM}-slot000": "proof_of_concept",
        f"{STEAM}-slot008": "proof_of_concept",
        f"{STEAM}-slot011": "queens_blood_campaign",
    }), encoding="utf-8")
    return path


class FakeGame:
    """The main menu, Save / Load and Load list as seen live on 2026-09-28."""

    def __init__(self, *, menu_cursor: int = 0, order=None, detail: bool = True,
                 help_text: bool = True, marker: bool = False, prompt_kind: str = "load",
                 drop: set[int] | None = None, double: set[int] | None = None,
                 load_samples: int = 3, never_arrive: bool = False, menu_flag_lag: int = 0,
                 stale_open_reads: int = 0, hover_rows: bool = False, hover_extra: int | None = None) -> None:
        self.state = "field"
        # Reads of the opening menu that still name Item Transmuter.
        self.stale_open_reads = stale_open_reads
        # Row widgets as the savecells query reports them (live 2026-10-05):
        # the selected row's button is hovered; hover_extra hovers another.
        self.hover_rows = hover_rows
        self.hover_extra = hover_extra
        # Telemetry keeps saying "in menu" for this many reads after it closes.
        self.menu_flag_lag = menu_flag_lag
        self.lag_left = 0
        self.menu_cursor = menu_cursor
        self.submenu_cursor = 0  # Save
        self.order = list(order or sorted(SAVE_TIMES, key=lambda slot: -SAVE_TIMES[slot].timestamp()))
        self.row = 0
        self.top = 0
        self.window = 5
        self.detail = detail
        self.help_text = help_text
        self.marker = marker
        self.prompt_kind = prompt_kind
        self.prompt_yes = False
        self.drop = drop or set()
        self.double = double or set()
        self.presses: list[str] = []
        self.loaded_slot = None
        self.loading = 0
        self.load_samples = load_samples
        self.never_arrive = never_arrive
        self.position = {"x": 0.0, "y": 0.0, "z": 0.0}

    # Input -------------------------------------------------------------

    def press(self, button: str) -> None:
        index = len(self.presses)
        self.presses.append(button)
        if index in self.drop:
            return
        for _ in range(2 if index in self.double else 1):
            self._apply(button)

    def _move_row(self, step: int) -> None:
        self.row = max(0, min(len(self.order) - 1, self.row + step))
        if self.row < self.top:
            self.top = self.row
        elif self.row >= self.top + self.window:
            self.top = self.row - self.window + 1

    def _apply(self, button: str) -> None:
        state = self.state
        if state == "field":
            if button == "menu":
                self.state = "main"
        elif state == "main":
            if button == "menu":
                self.state = "field"
                self.lag_left = self.menu_flag_lag
            elif button in ("up", "down"):
                self.menu_cursor = (self.menu_cursor + (1 if button == "down" else -1)) % len(MAIN_MENU_ORDER)
            elif button == "confirm" and MAIN_MENU_ORDER[self.menu_cursor] == "save_load":
                self.state, self.submenu_cursor = "submenu", 0
        elif state == "submenu":
            if button == "menu":
                self.state = "main"
            elif button in ("up", "down"):
                self.submenu_cursor = 1 - self.submenu_cursor
            elif button == "confirm":
                self.state = "load_list" if self.submenu_cursor == 1 else "save_list"
                self.row, self.top = 0, 0
        elif state in ("load_list", "save_list"):
            if button == "menu":
                self.state = "submenu"
            elif button in ("up", "down"):
                self._move_row(1 if button == "down" else -1)
            elif button == "confirm":
                self.state, self.prompt_yes = "prompt", False
        elif state == "prompt":
            if button == "up":
                self.prompt_yes = not self.prompt_yes  # two entries, wrapping
            elif button == "menu":
                self.state = "load_list"
            elif button == "confirm":
                if self.prompt_yes and self.prompt_kind == "load":
                    self.state, self.loading = "loading", self.load_samples
                    self.loaded_slot = self.order[self.row]
                else:
                    self.state = "load_list"

    # Reading -----------------------------------------------------------

    @property
    def in_menu(self) -> bool:
        if self.state == "field" and self.lag_left > 0:
            self.lag_left -= 1
            return True
        return self.state in ("main", "submenu", "load_list", "save_list", "prompt")

    def response(self) -> dict:
        texts = []
        if self.state in ("main", "submenu", "load_list", "save_list", "prompt"):
            texts.append({"path": "X.MainMenu_Base.WidgetTree.Txt_Title", "text": "$menu_mainmenu_0000_0000"})
            texts.append({"path": "X.Party.WidgetTree.Txt_Level", "text": "38"})
        if self.state == "main" and self.help_text:
            shown_item = MAIN_MENU_ORDER[self.menu_cursor]
            if self.stale_open_reads > 0:
                self.stale_open_reads -= 1
                shown_item = MAIN_MENU_ORDER[0]
            texts.append({"path": "X.Foot.WidgetTree.Txt_Help", "text": HELP[shown_item]})
        if self.state == "submenu" and self.help_text:
            texts.append({"path": "X.Foot.WidgetTree.Txt_Help",
                          "text": ["$help_message_0008_0000", "$help_message_0008_0001"][self.submenu_cursor]})
        if self.state in ("load_list", "save_list", "prompt"):
            if self.help_text:
                texts.append({"path": "X.Foot.WidgetTree.Txt_Help", "text": "$help_message_0008_0005"
                              if self.state != "save_list" else "$help_message_0008_0004"})
            for index in range(self.top, min(len(self.order), self.top + self.window)):
                slot = self.order[index]
                cell = ROW.replace("SaveDataCell", "SaveDataCellSelected") if (
                    self.marker and index == self.row) else ROW
                if slot:
                    texts.append({"path": cell + "Txt_SlotNo", "text": f"{slot:02d}"})
                texts.append({"path": cell + "Txt_Date", "text": shown(SAVE_TIMES[slot])})
                texts.append({"path": cell + "Txt_Level", "text": "40"})
                texts.append({"path": cell + "Txt_Kind", "text": "$menu_save_load_0001_0001" if slot
                              else "$menu_save_load_0001_0000"})
            if self.detail:
                texts.append({"path": DETAIL, "text": shown(SAVE_TIMES[self.order[self.row]])})
        response = {"status": "map_not_open", "texts": texts, "source_sequence": 1}
        if self.hover_rows and self.state == "load_list":
            response["save_cells"] = [
                {"nodes": [
                    {"name": "Button_00", "class": "EndButton",
                     "hovered": index == self.row or index == self.hover_extra},
                    {"name": "Txt_Date", "class": "EndTextBlock", "text": SAVE_TIMES[slot].strftime("%m/%d/%Y")},
                    {"name": "Txt_Time", "class": "EndTextBlock",
                     "text": SAVE_TIMES[slot].strftime("%I:%M:%S ") + ("a.m." if SAVE_TIMES[slot].hour < 12 else "p.m.")},
                ]}
                for index in range(self.top, min(len(self.order), self.top + self.window))
                for slot in [self.order[index]]]
        if self.state == "prompt":
            texts.append({"path": PROMPT, "text": "$menu_save_load_0003_0000" if self.prompt_kind == "load"
                          else "$menu_save_load_0002_0000"})
        return response

    def health(self) -> dict:
        loading = self.state == "loading"
        if loading:
            if self.loading > 0:
                self.loading -= 1
            elif not self.never_arrive:
                self.state = "field"
                self.position = {"x": 50000.0, "y": 0.0, "z": 0.0}
                loading = False
        return {
            "telemetry_live": True,
            "game_running": True,
            "in_menu": self.in_menu,
            "paused": self.in_menu,
            "in_combat": False,
            "target_clear": True,
            "map_id": None if loading else "map-a",
            "player_spatial": not loading,
            "loaded_gameplay": not loading and not self.in_menu,
            "diagnosis": "player_not_loaded" if loading else "ready",
        }


class FakeService:
    def __init__(self, game: FakeGame) -> None:
        self.game = game
        self.now = 0.0
        self.settled = 0
        self.focused = 0

    def observe(self) -> dict:
        return {"in_menu": self.game.in_menu, "map_id": "map-a",
                "player": {"position": dict(self.game.position)}, "telemetry": {"live": True}}

    def session_health(self) -> dict:
        return self.game.health()

    def _sleeper(self, seconds: float) -> None:
        self.now += seconds

    def _monotonic(self) -> float:
        return self.now

    def _ensure_game_foreground(self, force: bool = False) -> None:
        self.focused += 1

    def _map_menu_key(self, button: str, milliseconds: int) -> None:
        assert milliseconds == (save_slot_load.MENU_OPEN_KEY_MS if button == "menu" and self.game.state == "field" else save_slot_load.KEY_MS)
        self.game.press(button)

    def wait_for_settled_gameplay(self, timeout: float) -> dict:
        self.settled += 1
        return {"ok": True, "readiness": "settled_loaded_active_gameplay"}


@pytest.fixture
def snapshot(monkeypatch):
    holder = {}

    def fake(service, heights, flags, require_map=True):
        assert require_map is False
        assert heights is False
        holder.setdefault("flags", []).append(flags)
        response = holder["game"].response()
        if "menutexts" in flags and not holder.get("old_exporter"):
            response = {**response, "texts_scope": "menu"}
        return response, {}

    monkeypatch.setattr(save_slot_load, "request_snapshot", fake)
    return holder


def run(snapshot, game, save_root, aliases, slot, execute=True, layout=None):
    snapshot["game"] = game
    service = FakeService(game)
    loader = SaveSlotLoader(service, layout=layout or SaveMenuLayout(), save_root=save_root,
                            aliases_path=aliases)
    return loader.run(slot, execute=execute), service


class TitleGame(FakeGame):
    """Synthetic title selection with the same SaveData rows as the field."""

    def __init__(self, *, title_cursor=0, title_proof=True, **kwargs):
        super().__init__(**kwargs)
        self.state = "title"
        self.title_cursor = title_cursor
        self.title_proof = title_proof
        self.title_order = ["$menu_title_0001_0000", "$menu_title_0001_0001",
                            "$menu_title_0001_0002", "$menu_title_0001_0004"]
        self.wrong_slot_loads = []

    @property
    def in_menu(self):
        return self.state == "title" or super().in_menu

    def _apply(self, button):
        if self.state == "title":
            if button == "down":
                self.title_cursor = (self.title_cursor + 1) % len(self.title_order)
            elif button == "confirm":
                assert self.title_order[self.title_cursor] == "$menu_title_0001_0002", "Continue/New Game was selected"
                self.state = "load_list"
            return
        if button in {"cancel", "menu"} and self.state in {"load_list", "save_list", "prompt"}:
            self.state = "load_list" if self.state == "prompt" else "title"
            return
        return super()._apply(button)

    def health(self):
        result = super().health()
        if self.state != "field":
            result.update(map_id=None, player_spatial=False, loaded_gameplay=False)
        result["process_exists"] = True
        return result

    def response(self):
        response = super().response()
        if self.state == "title" and self.title_proof:
            selected = f"TitleCell_C /Engine/Transient.Title.Entry_{self.title_cursor}"
            owner = "StartMenu_C /Engine/Transient.StartMenu_C_1"
            nodes = [{"full_id": selected, "visible": True},
                     {"text": self.title_order[self.title_cursor], "visible": True}]
            response.update(save_list_discovery={"scan_truncated": False, "records_truncated": False},
                            title_lists=[{"visible": True, "title_owner_visible": True, "title_owner_id": owner,
                                          "owners": [owner], "user_index": 0, "selected_index": self.title_cursor,
                                          "selected_child_id": selected, "selected_nodes": nodes,
                                          "selected_node_walk": {"node_limit": 128, "visited": len(nodes), "truncated": False}}])
        return response


def run_title(snapshot, game, save_root, aliases, slot=8, execute=True):
    snapshot["game"] = game
    service = FakeService(game)
    candidate = []
    service.begin_save_load = lambda file: candidate.append(file) or dict(file)
    service.complete_save_load = lambda file: {"verified": True, **file}
    loader = SaveSlotLoader(service, layout=SaveMenuLayout(), save_root=save_root, aliases_path=aliases,
                            from_title=True)
    return loader.run(slot, execute), service, candidate


@pytest.mark.parametrize("cursor", [0, 1, 2, 3])
def test_title_load_selects_assigned_row_without_continue(snapshot, save_root, aliases, cursor):
    game = TitleGame(title_cursor=cursor, detail=False, hover_rows=True)
    result, service, candidates = run_title(snapshot, game, save_root, aliases, slot=2)
    assert result["ok"] and game.loaded_slot == 2 and service.settled == 1
    assert result["loaded_save"]["verified"] and result["loaded_save"]["slot"] == 2
    assert result["loaded_save"]["steam_id"] == STEAM
    assert len(candidates) == 1 and candidates[0]["slot"] == 2
    assert not game.wrong_slot_loads and "menu" not in game.presses
    assert result["confirm"]["load_evidence"] == "title_load_ui_closed"
    assert all(flags == save_slot_load.TITLE_SNAPSHOT_FLAGS for flags in snapshot["flags"])


def test_title_dry_run_returns_to_title_without_loading(snapshot, save_root, aliases):
    game = TitleGame()
    result, _, candidates = run_title(snapshot, game, save_root, aliases, execute=False)
    assert result["ok"] and result["state"] == "dry_run_highlighted"
    assert game.state == "title" and game.loaded_slot is None and not candidates


def test_title_dry_run_stops_at_the_title_cursor_despite_a_stale_load_list(snapshot, save_root, aliases):
    """Live 2026-10-20: the closed Load list kept its rows and signal, so the
    close loop kept pressing Cancel; at the title that opens "Quit the game?"
    with Yes preselected."""
    class StaleListTitleGame(TitleGame):
        stale = False
        quit_prompts = 0

        def _apply(self, button):
            if button == "cancel" and self.state == "title":
                self.quit_prompts += 1
                return
            if button == "cancel" and self.state == "load_list":
                self.stale = True
            return super()._apply(button)

        def response(self):
            title = super().response()
            if not (self.stale and self.state == "title"):
                return title
            self.state = "load_list"
            try:
                stale = super().response()
            finally:
                self.state = "title"
            return {**stale, "title_lists": title.get("title_lists", []),
                    "save_list_discovery": title.get("save_list_discovery")}

    game = StaleListTitleGame()
    result, _, candidates = run_title(snapshot, game, save_root, aliases, execute=False)
    assert result["ok"] and result["state"] == "dry_run_highlighted"
    assert game.quit_prompts == 0 and game.presses.count("cancel") == 1
    assert game.state == "title" and game.loaded_slot is None and not candidates


def test_title_row_loads_without_prompt_and_fingerprints_before_confirm(snapshot, save_root, aliases):
    class ImmediateTitleGame(TitleGame):
        def _apply(self, button):
            if self.state == "load_list" and button == "confirm":
                assert candidates and candidates[0]["slot"] == self.order[self.row]
                self.loaded_slot = self.order[self.row]
                self.state, self.loading = "loading", self.load_samples
                return
            super()._apply(button)

    game = ImmediateTitleGame(title_cursor=2, detail=False, hover_rows=True)
    snapshot["game"] = game
    service = FakeService(game)
    candidates = []
    service.begin_save_load = lambda file: candidates.append(file) or dict(file)
    service.complete_save_load = lambda file: {"verified": True, **file}
    result = SaveSlotLoader(service, layout=SaveMenuLayout(), save_root=save_root,
                            aliases_path=aliases, from_title=True).run(11, True)
    assert result["ok"] and game.loaded_slot == 11 and service.settled == 1
    assert result["loaded_save"]["verified"] and len(candidates) == 1
    assert result["confirm"] == {"attempts": 1, "load_evidence": "title_load_ui_closed", "prompt_shown": False}
    assert game.presses == ["confirm", "confirm"]  # No Yes input into gameplay.


def test_hidden_checking_text_does_not_prove_title_load_or_allow_more_input(snapshot, save_root, aliases):
    class HiddenCheckingTitle(TitleGame):
        def response(self):
            response = super().response()
            response["texts"].append({"path": "X.SaveIcon.Txt_Info", "text": "$menu_save_load_0005_0006"})
            return response

    game = HiddenCheckingTitle(title_cursor=2, drop={1})  # Lose only the row confirm.
    result, _, candidates = run_title(snapshot, game, save_root, aliases, slot=11)
    assert not result["ok"] and result["decision"] == "load_prompt_not_shown"
    assert not result["fallback_allowed"] and game.loaded_slot is None
    assert game.presses == ["confirm", "confirm"] and len(candidates) == 1


def test_title_missing_selection_sends_nothing_and_allows_verified_startup_fallback(snapshot, save_root, aliases):
    game = TitleGame(title_proof=False)
    result, _, candidates = run_title(snapshot, game, save_root, aliases)
    assert not result["ok"] and result["decision"] == "title_menu_selection_unverified"
    assert result["fallback_allowed"] and not game.presses and not candidates


def test_title_unreadable_row_backs_out_before_fallback(snapshot, save_root, aliases):
    game = TitleGame(detail=False)
    result, _, candidates = run_title(snapshot, game, save_root, aliases)
    assert not result["ok"] and result["decision"] == "load_list_highlight_unreadable"
    assert result["fallback_allowed"] and result["closed"] and game.state == "title" and not candidates


def test_title_missing_player_alone_never_proves_a_load(snapshot, save_root, aliases):
    game = TitleGame(title_cursor=2, drop={3, 6})  # Drop final Yes both times.
    result, _, _ = run_title(snapshot, game, save_root, aliases, slot=11)
    assert not result["ok"] and result["decision"] == "load_prompt_still_shown"
    assert not result["fallback_allowed"] and game.loaded_slot is None
    assert game.state == "prompt"  # No cleanup input after a possible load.


def test_title_unverified_receipt_never_falls_back(snapshot, save_root, aliases):
    game = TitleGame()
    snapshot["game"] = game
    service = FakeService(game)
    service.complete_save_load = lambda file: {"verified": False}
    loader = SaveSlotLoader(service, layout=SaveMenuLayout(), save_root=save_root, aliases_path=aliases, from_title=True)
    result = loader.run(8, True)
    assert not result["ok"] and result["decision"] == "loaded_save_receipt_unverified"
    assert not result["fallback_allowed"] and game.loaded_slot == 8 and game.state == "field"


@pytest.mark.parametrize("mutation", [None, "file", "process"])
def test_title_load_uses_existing_fingerprint_and_process_receipt(snapshot, save_root, aliases, tmp_path, mutation):
    from ff7r_agent.queen_blood_runs import QueenBloodRuns
    game = TitleGame()
    snapshot["game"] = game
    service = FakeService(game)
    runs = QueenBloodRuns(tmp_path / "run-receipts")
    process = {"pid": "42", "token": "created-42"}
    driver = {"driver": "ChatGPT", "model_id": "test-model"}
    file = next(item.summary() for item in list_save_files(save_root) if item.slot == 8)
    runs.assign(driver, file)
    before = {item.path: Path(item.path).read_bytes() for item in list_save_files(save_root)}
    service.begin_save_load = lambda target: runs.begin_load(target, dict(process))
    def complete(candidate):
        if mutation == "file":
            Path(file["path"]).write_bytes(b"changed-by-test")
        elif mutation == "process":
            process["token"] = "replacement-process"
        return runs.complete_load(candidate, process)
    service.complete_save_load = complete
    loader = SaveSlotLoader(service, layout=SaveMenuLayout(), save_root=save_root, aliases_path=aliases, from_title=True)
    result = loader.run(8, True)
    if mutation is None:
        assert result["ok"] and result["loaded_save"]["process"] == process
        assert runs.status(driver, process)["run_loaded"]
        assert before == {item.path: Path(item.path).read_bytes() for item in list_save_files(save_root)}
    else:
        assert not result["ok"] and result["decision"] == "loaded_save_receipt_unverified"
        assert not result["fallback_allowed"]


@pytest.mark.parametrize("mutation", ["duplicate", "truncated", "hidden", "unlinked", "root_mismatch", "bad_discovery", "bad_walk", "whole_list"])
def test_title_selection_rejects_ambiguous_or_incomplete_evidence(save_root, mutation):
    response = TitleGame(title_cursor=2).response()
    entry = response["title_lists"][0]
    if mutation == "duplicate":
        response["title_lists"].append(dict(entry))
    elif mutation == "truncated":
        entry["selected_node_walk"]["truncated"] = True
    elif mutation == "hidden":
        entry["title_owner_visible"] = False
    elif mutation == "unlinked":
        entry["owners"] = []
    elif mutation == "root_mismatch":
        entry["selected_child_id"] = "Another Cell"
    elif mutation == "bad_discovery":
        response["save_list_discovery"] = []
    elif mutation == "bad_walk":
        entry["selected_node_walk"] = []
    elif mutation == "whole_list":
        entry["selected_nodes"][0]["text"] = "Continue"
    assert SaveMenuScreen(response, list_save_files(save_root)).title_selection is None


def test_load_row_reuses_reviewed_virtual_scroll_selected_child_proof():
    from ff7r_agent.save_selection import CONTRACT, selected_cell
    response = json.loads((Path(__file__).parent / "fixtures/save-selection-live-derived.json").read_text())["frames"][1]
    cell, _ = selected_cell(response, CONTRACT)
    texts = {node["name"]: node["text"] for node in cell["nodes"] if node.get("text")}
    stamp = save_slot_load._date_candidates(f"{texts['Txt_Date']} {texts['Txt_Time']}")[0].timestamp()
    slot = int(texts["Txt_ListNum"])
    save = save_slot_load.SaveFile(slot, "synthetic.sav", stamp, STEAM)
    screen = SaveMenuScreen(response, [save])
    assert screen.highlighted_slot == slot and screen.highlight_basis == "selected_child"


def test_unverified_receipt_is_failure_even_after_successful_gameplay_arrival(snapshot, save_root, aliases):
    game = FakeGame()
    snapshot["game"] = game
    service = FakeService(game)
    service.complete_save_load = lambda candidate: {"verified": False, "reason": "process_changed"}
    report = SaveSlotLoader(service, layout=SaveMenuLayout(), save_root=save_root, aliases_path=aliases).run(8, execute=True)
    assert game.loaded_slot == 8 and not report["ok"]
    assert report["decision"] == "loaded_save_receipt_unverified"


def test_account_pin_ignores_another_accounts_newer_save(tmp_path):
    for account, stamp in (("7656", 1780000000), ("7777", 1790000000)):
        directory = tmp_path / account
        directory.mkdir()
        path = directory / "ff7rebirth008.sav"
        path.write_bytes(b"account save")
        os.utime(path, (stamp, stamp))
    assert save_slot_load.resolve_save_slot(8, save_root=tmp_path)["file"]["steam_id"] == "7777"
    target = save_slot_load.resolve_save_slot(8, save_root=tmp_path, steam_id="7656")
    assert target["file"]["steam_id"] == "7656"


def test_loads_slot_from_an_arbitrary_main_menu_cursor_through_the_wrap(snapshot, save_root, aliases):
    game = FakeGame(menu_cursor=1)  # Materia & Equipment
    report, service = run(snapshot, game, save_root, aliases, 8)

    assert report["ok"] is True and report["state"] == "loaded", report.get("decision")
    assert game.loaded_slot == 8
    # Materia -> Item Transmuter -> (wrap) System -> Save / Load, not 9 downs.
    assert game.presses[:4] == ["menu", "up", "up", "up"]
    # Save / Load, Load, then two rows down (11, autosave, 08), prompt, Yes.
    assert game.presses[4:] == ["confirm", "down", "confirm", "down", "down", "confirm", "up", "confirm"]
    assert report["confirm"] == {"attempts": 1, "load_evidence": "player_not_loaded"}
    assert report["position_changed"] is True and service.settled == 1
    assert report["highlighted"]["highlight_basis"] == "detail_occurrence"
    assert report["highlighted"]["slot_number_path"] == ROW + "Txt_SlotNo"


def test_opens_on_item_transmuter_by_default_and_goes_up_twice(snapshot, save_root, aliases):
    game = FakeGame()
    report, _ = run(snapshot, game, save_root, aliases, 11)

    assert report["ok"] is True
    assert game.presses == ["menu", "up", "up", "confirm", "down", "confirm", "confirm", "up", "confirm"]


def test_main_menu_moves_are_pressed_together_then_verified_once(snapshot, save_root, aliases, monkeypatch):
    # A read after every press made menu navigation slow (owner, 2026-10-05).
    game = FakeGame()
    log: list[str] = []
    press = game.press
    monkeypatch.setattr(game, "press", lambda button: (log.append(button), press(button))[1])
    response = game.response
    monkeypatch.setattr(game, "response", lambda: (log.append("read"), response())[1])
    report, _ = run(snapshot, game, save_root, aliases, 11)

    assert report["ok"] is True
    first_up = log.index("up")
    assert log[first_up:first_up + 3] == ["up", "up", "read"]


def test_a_stale_first_read_of_the_opening_menu_is_not_acted_on(snapshot, save_root, aliases):
    # Live 2026-10-05: the opening menu first read as Item Transmuter while
    # the cursor was still on Save / Load, and two batched presses hit Party.
    game = FakeGame(menu_cursor=MAIN_MENU_ORDER.index("save_load"), stale_open_reads=1)
    report, _ = run(snapshot, game, save_root, aliases, 11)

    assert report["ok"] is True
    assert game.presses[:2] == ["menu", "confirm"]


def test_menu_reads_use_the_menu_scan_and_fall_back_to_all_texts(snapshot, save_root, aliases):
    # The all-texts scan froze the open menu at every read (live 2026-10-05).
    report, _ = run(snapshot, FakeGame(), save_root, aliases, 11)
    assert report["ok"] is True and set(snapshot["flags"]) <= {("menutexts",), ("menutexts", "savecells")}
    snapshot["flags"] = []
    snapshot["old_exporter"] = True
    report, _ = run(snapshot, FakeGame(), save_root, aliases, 11)
    assert report["ok"] is True and ("alltexts",) in snapshot["flags"]


def test_finds_a_row_below_the_visible_window(snapshot, save_root, aliases):
    game = FakeGame()
    report, _ = run(snapshot, game, save_root, aliases, 2)  # the oldest save, row 11

    assert report["ok"] is True and game.loaded_slot == 2
    assert game.presses.count("down") == 1 + 11 and game.top > 0


def test_dry_run_highlights_the_row_and_closes_every_menu(snapshot, save_root, aliases):
    game = FakeGame(menu_cursor=5)
    report, _ = run(snapshot, game, save_root, aliases, "queens_blood_campaign", execute=False)

    assert report["state"] == "dry_run_highlighted" and report["slot"] == 11
    assert report["highlighted"]["highlighted_slot"] == 11
    assert game.state == "field" and game.loaded_slot is None
    assert game.presses.count("confirm") == 2  # Save / Load and Load only


def test_retries_when_the_default_no_was_confirmed(snapshot, save_root, aliases):
    game = FakeGame()
    # Presses: menu, up, up, confirm, down, confirm, down, down, confirm(row), up(lost), confirm(No)
    game.drop = {9}
    report, _ = run(snapshot, game, save_root, aliases, 8)

    assert report["ok"] is True and game.loaded_slot == 8
    assert report["confirm"]["attempts"] == 2
    assert game.presses[8:] == ["confirm", "up", "confirm", "confirm", "up", "confirm"]


def test_an_overshooting_press_is_corrected_from_the_highlight(snapshot, save_root, aliases):
    game = FakeGame()
    game.double = {6}  # the first row Down moves two rows (11 -> 08)
    report, _ = run(snapshot, game, save_root, aliases, 0)  # autosave, between them

    assert report["ok"] is True and game.loaded_slot == 0
    assert game.presses[6:8] == ["down", "up"]


def test_a_stalled_main_menu_press_is_retried_once(snapshot, save_root, aliases):
    game = FakeGame(menu_cursor=0)
    game.drop = {1}
    report, _ = run(snapshot, game, save_root, aliases, 11)

    assert report["ok"] is True
    assert game.presses[:4] == ["menu", "up", "up", "up"]


def test_highlight_can_be_named_by_the_layout_override(snapshot, save_root, aliases):
    game = FakeGame(detail=False, marker=True)
    layout = SaveMenuLayout(highlight_path_pattern=r"SaveDataCellSelected")
    report, _ = run(snapshot, game, save_root, aliases, 7, layout=layout)

    assert report["ok"] is True and game.loaded_slot == 7
    assert report["highlighted"]["highlight_basis"] == "highlight_path_pattern"


def test_refuses_to_confirm_without_a_readable_highlight(snapshot, save_root, aliases):
    game = FakeGame(detail=False)
    report, _ = run(snapshot, game, save_root, aliases, 8)

    assert report["ok"] is False and report["decision"] == "load_list_highlight_unreadable"
    assert game.loaded_slot is None and "prompt" not in game.state
    # Nothing after opening the list except backing out of the menus.
    assert game.presses[6:] == ["menu", "menu", "menu"] and game.state == "field"
    assert any(item["text"] == shown(SAVE_TIMES[11]) for item in report["last_texts"])


def test_backing_out_waits_for_the_menu_flag_instead_of_reopening_the_menu(snapshot, save_root, aliases):
    # Live 2026-10-05: the save UI had gone but telemetry still said "in
    # menu", so a fourth press reopened the main menu and a fifth closed it.
    game = FakeGame(detail=False, menu_flag_lag=3)
    report, _ = run(snapshot, game, save_root, aliases, 8)

    assert report["decision"] == "load_list_highlight_unreadable"
    assert game.presses[6:] == ["menu", "menu", "menu"] and game.state == "field"


def test_the_hovered_row_names_the_highlight_when_nothing_else_does(snapshot, save_root, aliases):
    # #153, live 2026-10-05: no detail panel repeats the selected save, and
    # the hovered row button was the only difference.
    game = FakeGame(detail=False, hover_rows=True)
    report, _ = run(snapshot, game, save_root, aliases, 8)

    assert report["ok"] is True and game.loaded_slot == 8
    assert report["highlighted"]["highlight_basis"] == "hovered_row"
    # The re-check before confirming read the same row (live it read none).
    assert report["confirm"]["attempts"] == 1


def test_two_hovered_rows_still_refuse_to_confirm(snapshot, save_root, aliases):
    game = FakeGame(detail=False, hover_rows=True, hover_extra=3)
    report, _ = run(snapshot, game, save_root, aliases, 8)

    assert report["ok"] is False and report["decision"] == "load_list_highlight_unreadable"
    assert game.loaded_slot is None


def test_refuses_to_move_without_a_readable_main_menu_cursor(snapshot, save_root, aliases):
    game = FakeGame(help_text=False)
    report, _ = run(snapshot, game, save_root, aliases, 8)

    assert report["decision"] == "main_menu_cursor_unreadable"
    assert game.presses == ["menu", "menu"] and game.state == "field"


def test_declines_a_save_prompt_and_never_selects_yes(snapshot, save_root, aliases):
    game = FakeGame(prompt_kind="save")
    report, _ = run(snapshot, game, save_root, aliases, 11)

    assert report["decision"] == "save_screen_detected"
    # menu, up, up, Save / Load, down, Load, the row: then only backing out.
    assert game.presses[6] == "confirm" and game.presses[7] == "menu"
    assert "up" not in game.presses[6:]
    assert game.loaded_slot is None


def test_stops_when_the_list_order_differs_from_the_files(snapshot, save_root, aliases):
    order = sorted(SAVE_TIMES, key=lambda slot: -SAVE_TIMES[slot].timestamp())
    order[1], order[3] = order[3], order[1]  # 11, 07, 08, autosave, ...
    game = FakeGame(order=order)
    report, _ = run(snapshot, game, save_root, aliases, 0)

    assert report["decision"] == "load_list_order_mismatch"
    assert game.loaded_slot is None and game.state == "field"


def test_a_missing_slot_fails_before_any_input(snapshot, save_root, aliases):
    game = FakeGame()
    report, _ = run(snapshot, game, save_root, aliases, 12)

    assert report["decision"] == "save_slot_missing" and game.presses == []


def test_a_load_that_never_settles_is_not_backed_out_of(snapshot, save_root, aliases):
    game = FakeGame(never_arrive=True)
    report, _ = run(snapshot, game, save_root, aliases, 11)

    assert report["decision"] == "load_arrival_timeout"
    assert game.presses[-1] == "confirm" and game.state == "loading"


def test_game_exit_during_load_stops_immediately_without_menu_recovery(snapshot, save_root, aliases):
    game = FakeGame(never_arrive=True)
    health = game.health
    game.health = lambda: {**health(), "process_exists": game.state != "loading"}
    report, service = run(snapshot, game, save_root, aliases, 11)
    assert report["decision"] == "game_exited_during_load"
    assert service.now < save_slot_load.ARRIVAL_WAIT_SECONDS
    assert game.presses[-1] == "confirm" and game.state == "loading"


def test_receipt_hooks_only_run_for_a_confirmed_successful_load(snapshot, save_root, aliases, monkeypatch):
    events = []
    monkeypatch.setattr(FakeService, "begin_save_load", lambda self, file: events.append(("begin", file["slot"])) or file,
                        raising=False)
    monkeypatch.setattr(FakeService, "complete_save_load", lambda self, candidate: events.append(("complete", candidate["slot"])) or {"verified": True, "slot": candidate["slot"]},
                        raising=False)
    report, _ = run(snapshot, FakeGame(), save_root, aliases, 8, execute=False)
    assert report["ok"] and not events
    report, _ = run(snapshot, FakeGame(), save_root, aliases, 8)
    assert events == [("begin", 8), ("complete", 8)] and report["loaded_save"]["verified"]


def test_combat_blocks_the_load(snapshot, save_root, aliases):
    game = FakeGame()
    original = game.health
    game.health = lambda: {**original(), "in_combat": True}
    report, _ = run(snapshot, game, save_root, aliases, 11)

    assert report["decision"] == "load_blocked_in_combat" and game.presses == []


def test_an_open_menu_is_closed_before_starting(snapshot, save_root, aliases):
    game = FakeGame()
    game.state = "submenu"
    report, _ = run(snapshot, game, save_root, aliases, 11)

    assert report["ok"] is True
    assert game.presses[:3] == ["menu", "menu", "menu"]


# ---------------------------------------------------------------------------
# Slots, aliases and dates


def test_save_files_are_listed_newest_first(save_root):
    files = list_save_files(save_root)
    assert [item.slot for item in files][:6] == [11, 0, 8, 7, 4, 10]


@pytest.mark.parametrize(("requested", "slot"), [
    (8, 8), ("8", 8), ("008", 8), ("slot011", 11), (f"{STEAM}-slot004", 4),
    ("autosave", 0), ("queens_blood_campaign", 11), ("Proof_Of_Concept", 8),
])
def test_resolves_slot_numbers_and_profile_aliases(save_root, aliases, requested, slot):
    assert resolve_save_slot(requested, save_root=save_root, aliases_path=aliases)["slot"] == slot


def test_alias_naming_two_manual_slots_is_ambiguous(save_root, tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({f"{STEAM}-slot004": "run", f"{STEAM}-slot007": "run"}), encoding="utf-8")
    with pytest.raises(SaveSlotLoadError, match="save_slot_alias_ambiguous"):
        resolve_save_slot("run", save_root=save_root, aliases_path=path)
    with pytest.raises(SaveSlotLoadError, match="save_slot_unknown"):
        resolve_save_slot("nobody", save_root=save_root, aliases_path=path)
    with pytest.raises(SaveSlotLoadError, match="save_slot_invalid"):
        resolve_save_slot(True, save_root=save_root, aliases_path=path)


def test_row_dates_match_file_times(save_root):
    files = list_save_files(save_root)
    assert match_save_time("09/18/2026 10:18:57 p.m.", files, 5.0) == ([8], "date_time")
    assert match_save_time("09/18/2026 22:19:00", files, 5.0) == ([8], "date_time")
    assert match_save_time("10:18:57\xa0p.m.", files, 5.0) == ([8], "clock")
    assert match_save_time("09/18/2026 10:30:00 p.m.", files, 5.0) == ([], "date_time")
    assert match_save_time("Playtime 112:03:45", files, 5.0) == ([], None)


def test_screen_reads_resolved_english_text(save_root):
    files = list_save_files(save_root)
    screen = SaveMenuScreen({"texts": [
        {"path": "X.Foot.Txt_Help", "text": "Save and load game data."},
        {"path": PROMPT, "text": "Load the data in this slot?"},
    ]}, files)
    assert screen.main_menu_item == "save_load" and screen.load_prompt_shown


def test_two_main_menu_help_texts_leave_the_cursor_unknown(save_root):
    screen = SaveMenuScreen({"texts": [
        {"path": "X.Foot.Txt_Help", "text": HELP["system"]},
        {"path": "Y.Foot.Txt_Help", "text": HELP["manual"]},
    ]}, list_save_files(save_root))
    assert screen.main_menu_item is None


def test_layout_override_file(tmp_path):
    path = tmp_path / "layout.json"
    path.write_text(json.dumps({"highlight_path_pattern": "Cursor", "date_tolerance_seconds": 2}), encoding="utf-8")
    layout = load_layout(path)
    assert layout.highlight_path_pattern == "Cursor" and layout.date_tolerance_seconds == 2.0
    assert layout.main_menu_order == MAIN_MENU_ORDER and layout.source == str(path)
    path.write_text(json.dumps({"main_menu_order": ["system"]}), encoding="utf-8")
    with pytest.raises(SaveSlotLoadError, match="save_menu_layout_invalid"):
        load_layout(path)
    assert load_layout(tmp_path / "absent.json") == SaveMenuLayout()


def test_read_save_menu_reports_files_and_the_parsed_screen(snapshot, save_root, monkeypatch, tmp_path):
    monkeypatch.setattr(save_slot_load, "default_save_root", lambda: save_root)
    monkeypatch.setenv("FF7R_SAVE_MENU_LAYOUT", str(tmp_path / "none.json"))
    game = FakeGame()
    game.state = "load_list"
    snapshot["game"] = game

    result = save_slot_load.read_save_menu(FakeService(game))

    assert result["ok"] is True and result["saves"][0]["slot"] == 11
    assert result["screen"]["rows"] == [11, 0, 8, 7, 4]
    assert result["screen"]["highlighted_slot"] == 11
    assert result["texts"] and game.presses == []


def test_an_input_refusal_is_reported_with_its_evidence(snapshot, save_root, aliases):
    game = FakeGame()
    snapshot["game"] = game
    service = FakeService(game)
    original = service._map_menu_key

    def refuse(button, milliseconds):
        if len(game.presses) == 3:
            raise RuntimeError("FFVII Rebirth is not the foreground window")
        original(button, milliseconds)

    service._map_menu_key = refuse
    report = SaveSlotLoader(service, layout=SaveMenuLayout(), save_root=save_root,
                            aliases_path=aliases).run(11, execute=True)

    assert report["ok"] is False and report["decision"] == "load_save_slot_error"
    assert report["details"]["error"] == "FFVII Rebirth is not the foreground window"
    assert [press["button"] for press in report["presses"]] == ["menu", "up", "up"]


def test_menu_open_uses_supported_hold_without_lengthening_cursor_taps(snapshot, save_root, aliases):
    game = FakeGame()
    snapshot["game"] = game
    service = FakeService(game)
    sent = []
    def tap(button, milliseconds):
        sent.append((button, milliseconds))
        if button == "menu" and game.state == "field" and milliseconds < 500:
            return  # Reproduce the observed missed short opening pulse.
        game.press(button)
    service._map_menu_key = tap
    loader = SaveSlotLoader(service, layout=SaveMenuLayout(), save_root=save_root, aliases_path=aliases)
    report = loader.run(8, execute=True)
    assert report["ok"] is True and game.loaded_slot == 8
    assert sent[0] == ("menu", 500)
    assert all(ms == 60 for button, ms in sent if button != "menu")


def test_open_menu_waits_for_delayed_cursor_without_second_toggle(snapshot, save_root, aliases):
    game = FakeGame()
    snapshot["game"] = game
    service = FakeService(game)
    loader = SaveSlotLoader(service, layout=SaveMenuLayout(), save_root=save_root, aliases_path=aliases)
    read = loader.read
    def delayed(stage):
        if service.now < 1.5:
            return SaveMenuScreen({}, [])
        return read(stage)
    loader.read = delayed
    screen = loader._open_main_menu()
    assert screen.main_menu_item == "item_transmuter"
    assert game.presses == ["menu"]
    assert service.now >= 1.5


def test_open_unreadable_menu_never_toggles_or_confirms_again(snapshot, save_root, aliases):
    game = FakeGame(help_text=False)
    snapshot["game"] = game
    service = FakeService(game)
    loader = SaveSlotLoader(service, layout=SaveMenuLayout(), save_root=save_root, aliases_path=aliases)
    with pytest.raises(SaveSlotLoadError, match="main_menu_cursor_unreadable"):
        loader._open_main_menu()
    assert game.presses == ["menu"] and game.loaded_slot is None
    assert 8 <= service.now < 10
