"""Per-save Queen's Blood inventory, opponent records and the collection scan."""
from __future__ import annotations

import json
import os

import pytest

from ff7r_agent.queen_blood_profile import QueenBloodProfile, detect_profile_id
from ff7r_agent.service import AgentService
from ff7r_agent.telemetry import SimulationTelemetry
from test_agent import FakeArm, FakeEnvironment, FakeRealInput
from test_queens_blood_match import _queen_blood, _state


@pytest.fixture(autouse=True)
def fixture_profile(monkeypatch):
    # Deck/collection simulations inject their fixture inventory. Runtime
    # loaded-save/model resolution is exercised in test_queen_blood_runs.
    monkeypatch.setattr(AgentService, "get_queens_blood_run_status",
                        lambda self: {"profile_id": os.environ.get("FF7R_QUEEN_BLOOD_PROFILE")})


def test_profile_is_the_most_recently_written_save_slot(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FF7R_QUEEN_BLOOD_PROFILE", raising=False)
    steam = tmp_path / "76561198000000000"
    steam.mkdir()
    old = steam / "ff7rebirth001.sav"
    new = steam / "ff7rebirth002.sav"
    old.write_bytes(b"a")
    new.write_bytes(b"b")
    (steam / "ff7rebirthcommon.sav").write_bytes(b"c")  # not a playthrough slot
    os.utime(old, (1, 1))
    assert detect_profile_id(tmp_path) == "76561198000000000-slot002"
    aliases = tmp_path / "slot-aliases.json"
    aliases.write_text('{"76561198000000000-slot002": "main playthrough"}', encoding="utf-8")
    assert detect_profile_id(tmp_path, aliases) == "main_playthrough"
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "new game+ run")
    assert detect_profile_id(tmp_path) == "new_game_run"


def test_the_autosave_only_counts_without_a_manual_slot(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FF7R_QUEEN_BLOOD_PROFILE", raising=False)
    steam = tmp_path / "76561198000000000"
    steam.mkdir()
    autosave = steam / "ff7rebirth000.sav"
    autosave.write_bytes(b"a")
    assert detect_profile_id(tmp_path) == "76561198000000000-slot000"
    manual = steam / "ff7rebirth011.sav"
    manual.write_bytes(b"b")
    os.utime(manual, (1, 1))  # the autosave is newer, as after a fast travel
    assert detect_profile_id(tmp_path) == "76561198000000000-slot011"


def test_inventory_stores_ids_and_catalogue_copies_only(tmp_path) -> None:
    profile = QueenBloodProfile(tmp_path, "save-a")
    profile.record_collection(["card_id_000", "card_id_117", "card_id_000"], source="test")
    cards = profile.inventory()["cards"]
    assert {card_id: entry["owned"] for card_id, entry in cards.items()} == {
        "card_id_000": 2, "card_id_117": 1}  # standard 2 copies, legendary 1
    assert "name" not in cards["card_id_117"]  # details come from the catalogue
    joined = {card["card_id"]: card["name"] for card in profile.owned_cards()}
    assert joined == {"card_id_000": "Security Officer", "card_id_117": "Magic Pot"}
    assert QueenBloodProfile(tmp_path, "save-b").inventory()["cards"] == {}


def test_inventory_labels_missing_catalogue_copy_count_as_assumed(tmp_path, monkeypatch):
    monkeypatch.setattr("ff7r_agent.queen_blood_profile.catalogue_copies", lambda _: None)
    profile = QueenBloodProfile(tmp_path, "save-a")
    scanned = profile.record_collection(["card_id_000"], source="test")
    assert scanned["cards"]["card_id_000"] == {"owned": 1, "count_source": "assumed_single_copy"}
    reward = profile.add_card("card_id_117", source="test_reward")
    assert reward["cards"]["card_id_117"]["count_source"] == "assumed_single_copy"


def test_opponent_record_accumulates_cards_and_results(tmp_path) -> None:
    profile = QueenBloodProfile(tmp_path, "save-a")
    profile.update_opponent("Turner", name_id="$card_opponent_NIBLT_03", card_ids=["card_id_072"])
    profile.update_opponent("turner", card_ids=["card_id_016", "card_id_072"], match={"won": True})
    record = profile.update_opponent("Turner", match={"won": False})
    assert record["name_id"] == "$card_opponent_NIBLT_03"
    assert record["card_ids"] == ["card_id_072", "card_id_016"]
    assert record["record"] == {"played": 2, "won": 1}


class FakeEditor:
    """A deck-editor grid: two rows, one lag-doubled Right press."""

    GRID = [["card_id_000", "card_id_002", "card_id_006"], ["card_id_117"]]

    def __init__(self, backend, *, double_press: int | None = None, details: bool = True,
                 wrap: bool = False) -> None:
        self.wrap = wrap
        self.details = details
        self.stale = "card_id_117"
        self.backend = backend
        self.seen = 0
        self.sequence = 10
        self.cursor = (0, 1)
        self.presses = 0
        self.double_press = double_press

    def _move(self, button: str) -> None:
        row, index = self.cursor
        if button == "queen_blood_right":
            self.cursor = (row, min(index + 1, len(self.GRID[row]) - 1))
        elif button == "queen_blood_left":
            self.cursor = (row, max(index - 1, 0))
        elif button == "down" and row + 1 < len(self.GRID):
            self.cursor = (row + 1, min(index, len(self.GRID[row + 1]) - 1))
        elif button == "down" and self.wrap:
            self.cursor = (0, min(index, len(self.GRID[0]) - 1))  # live: Down wraps to the top
        elif button == "up" and row > 0:
            self.cursor = (row - 1, min(index, len(self.GRID[row - 1]) - 1))

    def observe(self):
        for action in self.backend.actions[self.seen:]:
            if action[0] == "tap" and action[1] == "queen_blood_details":
                self.details = not self.details
                continue
            if action[0] == "tap":
                self.presses += 1
                self._move(action[1])
                if self.presses == self.double_press:
                    self._move(action[1])
        self.seen = len(self.backend.actions)
        self.sequence += 1
        row, index = self.cursor
        return _state(self.sequence, map_id=None, include_target=False, queen_blood=_queen_blood(
            screen="deck_select", begin_match_available=True, match_screen=True,
            deck_screen={"observed": True, "shown": True, "windows": {"CardDeckList": True},
                         "editor": {"cursors": {"Edit": {"row": row, "index": index}}},
                         "detail": {"hovered": {"card_id": self.GRID[row][index] if self.details else self.stale}}}))


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


@pytest.mark.parametrize(("double_press", "details", "wrap"),
                         [(None, True, False), (5, True, False), (None, False, False), (None, True, True)])
def test_collection_scan_reads_every_cell_and_revisits_skips(tmp_path, monkeypatch, double_press, details, wrap) -> None:
    # details=False: the popup is off and its text is stale (live 2026-09-26);
    # the scan must notice the hovered card not following and press F4.
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "scan-test")
    backend = FakeRealInput()
    clock = _Clock()
    service = AgentService(FakeEditor(backend, double_press=double_press, details=details, wrap=wrap), backend, arm_state=FakeArm(),
                           environment=FakeEnvironment(), minimum_action_delay=0.0,
                           sleeper=clock.sleep, monotonic=clock.monotonic,
                           queen_blood_profile_root=tmp_path)

    result = service.scan_queens_blood_collection()

    assert [(c["row"], c["index"], c["card_id"]) for c in result["cells"]] == [
        (0, 0, "card_id_000"), (0, 1, "card_id_002"), (0, 2, "card_id_006"), (1, 0, "card_id_117")]
    owned = QueenBloodProfile(tmp_path, "scan-test").inventory()["cards"]
    assert sorted(owned) == ["card_id_000", "card_id_002", "card_id_006", "card_id_117"]
    assert result["inventory"] == owned
    assert owned["card_id_000"] == {"owned": 2, "count_source": "catalogue_copies"}
    assert owned["card_id_117"] == {"owned": 1, "count_source": "catalogue_copies"}


class FakeDeckScreen(SimulationTelemetry):
    """Deck select + editor: deck list (Player), owned grid (Edit), strip.

    Mirrors what was seen live on 2026-09-26: R opens the editor, Esc
    closes it, Up from the grid's first row enters the strip (Confirm
    deletes one copy), Confirm on a grid card adds one, and each area has
    its own details popup.
    """

    # Live 2026-09-29: every owned card, by cost then card number, 11 to a row.
    GRID = [["card_id_000", "card_id_002", "card_id_007", "card_id_008", "card_id_117"]]
    OWNED = {"card_id_000": 2, "card_id_007": 2, "card_id_008": 2, "card_id_117": 1, "card_id_002": 10}

    def __init__(self, backend, deck) -> None:
        self.backend = backend
        self.deck = list(deck)
        self.seen = 0
        self.sequence = 10
        self.editor = False
        self.details = True
        self.area = "Player"
        self.cursors = {"Player": (0, 0)}
        self.sequences = {"Player": 0}
        self.event = 0
        self.list_total = len(self.deck)
        self.saved = list(self.deck)
        self.dialog = None  # live: the "not fifteen cards" dialog's highlighted option

    def _order(self, card):
        return [c for row in self.GRID for c in row].index(card)

    @staticmethod
    def _total_text(count):
        # Live: a short deck's count is drawn red with rich-text markup.
        return f"{count}/15" if count == 15 else f"<color=red>{count}</color>/15"

    def _strip(self):
        return sorted(dict.fromkeys(self.deck), key=self._order)

    def _move(self, area, position):
        self.event += 1
        self.cursors[area] = position
        self.area = area
        self.sequences[area] = self.event

    def _press(self, button):
        if getattr(self, "tips", False):
            if button in {"cancel", "queen_blood_cancel"}:
                self.tips = False
            return  # live: the panel takes the deck screens' input
        if button == "queen_blood_details":
            self.details = not self.details
            return
        if self.dialog is not None:
            options = ["save", "quit_without_saving", "continue"]
            if button in {"up", "down"}:
                index = options.index(self.dialog) + (1 if button == "down" else -1)
                self.dialog = options[max(0, min(2, index))]
            elif button == "confirm":
                if self.dialog == "quit_without_saving":
                    self.deck = list(self.saved)
                if self.dialog != "continue":
                    self.editor = False
                    self.area = "Player"
                    self.list_total = len(self.deck)
                self.dialog = None
            return
        if not self.editor:
            if button == "queen_blood_edit_deck":
                self.editor = True
                self.saved = list(self.deck)
                # Live 2026-10-06: opening sends no cursor event; the grid
                # keeps its last reported cursor (if any) until it moves.
                self.cursors.setdefault("Edit", (0, 0))
                self.area = "Edit"
                return
            row, index = self.cursors["Player"]
            flat = row * 5 + index
            step = {"queen_blood_right": 1, "queen_blood_left": -1, "down": 5, "up": -5}.get(button, 0)
            if 0 <= flat + step < len(self.deck):
                self._move("Player", divmod(flat + step, 5))
            return
        if button == "queen_blood_cancel":
            if len(self.deck) != 15:
                self.dialog = "continue"  # the game refuses to close on it
                return
            self.editor = False
            self.area = "Player"
            self.list_total = len(self.deck)
            return
        if self.area == "Strip":
            index = self.cursors["Strip"][1]
            strip = self._strip()
            if button == "confirm":
                self.deck.remove(strip[index])
            elif button == "down":
                self._move("Edit", (0, min(index, len(self.GRID[0]) - 1)))
            elif button in {"queen_blood_left", "queen_blood_right"}:
                step = 1 if button == "queen_blood_right" else -1
                self._move("Strip", (0, max(0, min(len(strip) - 1, index + step))))
            return
        row, index = self.cursors["Edit"]
        if button == "confirm":
            card = self.GRID[row][index]
            if self.deck.count(card) < self.OWNED[card] and len(self.deck) < 15:
                self.deck.append(card)
        elif button == "up" and row == 0:
            self._move("Strip", (0, min(index, len(self._strip()) - 1)))
        elif button == "up":
            self._move("Edit", (row - 1, min(index, len(self.GRID[row - 1]) - 1)))
        elif button == "down" and row + 1 < len(self.GRID):
            self._move("Edit", (row + 1, min(index, len(self.GRID[row + 1]) - 1)))
        elif button in {"queen_blood_left", "queen_blood_right"}:
            step = 1 if button == "queen_blood_right" else -1
            self._move("Edit", (row, max(0, min(len(self.GRID[row]) - 1, index + step))))

    def observe(self):
        for action in self.backend.actions[self.seen:]:
            if action[0] == "tap":
                self._press(action[1])
        self.seen = len(self.backend.actions)
        self.sequence += 1
        cursors = {area: {"row": pos[0], "index": pos[1], "sequence": self.sequences.get(area, 0)}
                   for area, pos in self.cursors.items()}
        row, index = self.cursors["Player"]
        resolved = []
        if self.deck:
            player_card = self.deck[min(row * 5 + index, len(self.deck) - 1)]
            resolved.append({"card_id": player_card, "popup": "Information", "popup_shown": self.details,
                             "path": "Top.WidgetTree.CardList_Player.WidgetTree.Information.WidgetTree"})
        if self.editor and "Edit" in self.cursors:
            r, i = self.cursors["Edit"]
            resolved.append({"card_id": self.GRID[r][i], "popup": "MainMenu_Card_CardInformation_C_2147474949",
                             "path": "X.MainMenu_Card_CardInformation_C_2147474949.WidgetTree",
                             "popup_shown": self.details and self.area == "Edit"})
        if self.editor and "Strip" in self.cursors:
            strip = self._strip()
            resolved.append({"card_id": strip[min(self.cursors["Strip"][1], len(strip) - 1)],
                             "popup": "MainMenu_Card_CardInformation_C_2147474947",
                             "path": "X.MainMenu_Card_CardInformation_C_2147474947.WidgetTree",
                             "popup_shown": self.details and self.area == "Strip"})
        return _state(self.sequence, map_id=None, include_target=False, queen_blood=_queen_blood(
            screen="deck_select", begin_match_available=True, match_screen=True,
            # Like live: the deck-select total only updates when the editor is closed.
            deck_screen={"observed": True, "shown": True,
                         "windows": {"CardDeckList": self.editor or getattr(self, "stale_visibility", False)},
                         "deck": {"total": self._total_text(self.list_total)},
                         "editor": {"cursors": cursors},
                         "detail": {"resolved": resolved, "totals": [
                             {"text": self._total_text(len(self.deck)), "shown": True,
                              "path": "Top.WidgetTree.CardDeckList.WidgetTree.Txt_TotalNumber"}]}}))


def _deck_profile(tmp_path):
    profile = QueenBloodProfile(tmp_path, "deck-test")
    profile.record_collection([c for row in FakeDeckScreen.GRID for c in row], source="test")
    data = profile.inventory()
    data["cards"]["card_id_002"]["owned"] = 10  # a filler card with many copies
    profile.inventory_path.write_text(json.dumps(data), encoding="utf-8")
    return profile


def test_edit_deck_swaps_a_card_through_strip_and_grid(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    deck = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    target = list(deck)
    target.remove("card_id_007")
    target.append("card_id_008")
    backend = FakeRealInput()
    game = FakeDeckScreen(backend, deck)
    clock = _Clock()
    service = AgentService(game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
                           minimum_action_delay=0.0, sleeper=clock.sleep, monotonic=clock.monotonic,
                           queen_blood_profile_root=tmp_path)

    result = service.edit_queens_blood_deck(target)

    assert sorted(game.deck) == sorted(target)
    assert result["steps"] == [
        {"removed": "card_id_007", "strip_index": 2, "verified": True, "total_after": 14},
        {"added": "card_id_008", "grid": [0, 3], "verified": True, "total_after": 15}]
    assert game.editor is False  # left with Esc


def test_an_emptied_deck_is_read_as_empty_and_filled(tmp_path, monkeypatch) -> None:
    # Live 2026-09-29 the deck was emptied by hand ("0/15"); nothing to read.
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    target = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    backend = FakeRealInput()
    game = FakeDeckScreen(backend, [])
    clock = _Clock()
    service = AgentService(game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
                           minimum_action_delay=0.0, sleeper=clock.sleep, monotonic=clock.monotonic,
                           queen_blood_profile_root=tmp_path)

    result = service.edit_queens_blood_deck(target)

    assert sorted(game.deck) == sorted(target)
    assert all("added" in step for step in result["steps"]) and len(result["steps"]) == 15
    assert game.editor is False


def test_edit_deck_refuses_cards_that_are_not_owned(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    QueenBloodProfile(tmp_path, "deck-test").record_collection(["card_id_000"], source="test")
    backend = FakeRealInput()
    service = AgentService(FakeDeckScreen(backend, ["card_id_000"] * 15), backend, arm_state=FakeArm(),
                           environment=FakeEnvironment(), minimum_action_delay=0.0,
                           queen_blood_profile_root=tmp_path)
    with pytest.raises(Exception, match="not enough owned copies"):
        service.edit_queens_blood_deck(["card_id_000"] * 15)
    assert backend.actions == []


def _deck_service(tmp_path, deck):
    backend = FakeRealInput()
    game = FakeDeckScreen(backend, deck)
    clock = _Clock()
    service = AgentService(game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
                           minimum_action_delay=0.0, sleeper=clock.sleep, monotonic=clock.monotonic,
                           queen_blood_profile_root=tmp_path)
    return service, game


def test_fresh_cursor_waits_for_the_return_key_event(tmp_path):
    backend = FakeRealInput()
    game = FakeDeckScreen(backend, ["card_id_000", "card_id_007"])
    clock = _Clock()
    press, observe = game._press, game.observe
    pending = []

    def delayed_left(button):
        if button == "queen_blood_left":
            pending.append(clock.monotonic() + 0.35)
        else:
            press(button)

    def sampling():
        if pending and clock.monotonic() >= pending[0]:
            pending.pop(0)
            press("queen_blood_left")
        return observe()

    game._press = delayed_left
    game.observe = sampling
    service = AgentService(game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
                           minimum_action_delay=0.0, sleeper=clock.sleep, monotonic=clock.monotonic)

    service._deck_fresh_cursor("Player")

    assert not pending and service._deck_cursor("Player") == (0, 0)


def test_cursor_refresh_retries_short_presses_with_a_longer_hold(tmp_path):
    service, game = _deck_service(tmp_path, ["card_id_000", "card_id_007"])
    tap = game.backend.tap
    durations = []

    def dropped_short_press(button, milliseconds):
        durations.append(milliseconds)
        if milliseconds >= 150:
            tap(button, milliseconds)

    game.backend.tap = dropped_short_press
    service._deck_fresh_cursor("Player")
    assert 80 in durations and 150 in durations
    assert service._deck_cursor("Player") == (0, 0)


def test_the_deck_on_screen_is_read_first_even_when_one_is_remembered(tmp_path, monkeypatch) -> None:
    # Live 2026-10-06 a remembered deck was not the one on screen (it had
    # been overwritten): the deck is always read before planning edits.
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    profile = _deck_profile(tmp_path)
    deck = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    profile.record_deck(["card_id_002"] * 15, source="an overwritten deck")
    target = list(deck)
    target.remove("card_id_007")
    target.remove("card_id_007")
    target += ["card_id_008", "card_id_008"]
    service, game = _deck_service(tmp_path, deck)

    result = service.edit_queens_blood_deck(target)

    assert sorted(game.deck) == sorted(target) and result["current_from"] == "read"
    assert result["before"] == deck and result["attempts"] == 1
    # Both copies go with two Confirms on the one strip entry; both come back from one grid cell.
    assert result["steps"] == [
        {"removed": "card_id_007", "strip_index": 2, "verified": True, "total_after": 14},
        {"removed": "card_id_007", "strip_index": 2, "verified": True, "total_after": 13},
        {"added": "card_id_008", "grid": [0, 3], "verified": True, "total_after": 14},
        {"added": "card_id_008", "grid": [0, 3], "verified": True, "total_after": 15}]
    assert QueenBloodProfile(tmp_path, "deck-test").known_deck() == result["after"]


def test_a_stale_remembered_deck_does_not_plan_the_edit(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    profile = _deck_profile(tmp_path)
    actual = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    stale = ["card_id_000"] * 2 + ["card_id_008"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    profile.record_deck(stale, source="an older save")
    target = ["card_id_000"] * 2 + ["card_id_007", "card_id_008"] + ["card_id_117"] + ["card_id_002"] * 10
    service, game = _deck_service(tmp_path, actual)
    game.stale_visibility = True  # Live: an archetype stays visible after the real editor closes.

    result = service.edit_queens_blood_deck(target)

    assert sorted(game.deck) == sorted(target) and result["attempts"] == 1


def test_a_wrong_deck_read_and_failed_confirm_restores_the_saved_deck(tmp_path, monkeypatch) -> None:
    # Live 2026-09-29: the save's deck was older than the one remembered; the
    # edit removed a card that was not surplus, and Esc met "This deck cannot
    # be used ... Finish building anyway?".
    # Two equally misread starting samples stand in for the stale plan.
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    actual = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    stale = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 9 + ["card_id_008"]
    target = ["card_id_000"] * 2 + ["card_id_007", "card_id_008"] + ["card_id_117"] + ["card_id_002"] * 10
    service, game = _deck_service(tmp_path, actual)
    reads = iter([stale, stale])
    read = service._read_deck_list_fast
    monkeypatch.setattr(service, "_read_deck_list_fast", lambda: next(reads, None) or read())

    with pytest.raises(Exception, match="did not change the deck count"):
        service.edit_queens_blood_deck(target)

    # The stale plan drops the wrong card and cannot add a 11th filler: 14
    # cards. The unobserved Confirm is never blindly replayed; the saved
    # deck is restored and the caller can retry from a fresh screen read.
    assert game.deck == actual
    assert game.dialog is None and game.editor is False


def test_discrepant_starting_deck_read_is_rechecked_before_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    actual = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    stale = actual[:-1] + ["card_id_008"]
    target = actual.copy(); target.remove("card_id_007"); target.append("card_id_008")
    service, game = _deck_service(tmp_path, actual)
    original = service._read_deck_list_fast
    samples = iter([stale])
    monkeypatch.setattr(service, "_read_deck_list_fast", lambda: next(samples, None) or original())
    result = service.edit_queens_blood_deck(target)
    assert result["before"] == actual and sorted(game.deck) == sorted(target)
    assert [step.get("removed") for step in result["steps"] if "removed" in step] == ["card_id_007"]


def test_edit_fills_the_fifteenth_card_in_a_short_final_grid_row(tmp_path, monkeypatch):
    # The live edit stopped at 14/15: Down at (0, 8) could not enter row 1.
    grid = sorted([f"card_id_{n:03}" for n in [0, 2, 6, 7, 13, 14, 24, 112, 116, 4, 8, 10, 118, 117]],
                  key=AgentService._deck_order_key)
    monkeypatch.setattr(FakeDeckScreen, "GRID", [grid[:11], grid[11:]])
    monkeypatch.setattr(FakeDeckScreen, "OWNED", {card: 2 for card in grid})
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    profile = _deck_profile(tmp_path)
    before = [grid[0]] * 2 + [grid[1]] * 2 + [grid[2]] * 2 + [grid[3]] * 2 + [grid[4]] * 2 + [grid[5]] * 2 + [grid[6]] * 2 + [grid[7]]
    target = before[:-2] + [grid[8], grid[11]]
    profile.record_deck(before, source="test")
    service, game = _deck_service(tmp_path, before)
    original = game._press
    def press(button):
        if game.editor and game.area == "Edit" and button == "down":
            row, column = game.cursors["Edit"]
            if row + 1 < len(game.GRID) and column >= len(game.GRID[row + 1]):
                return
        original(button)
    game._press = press
    result = service.edit_queens_blood_deck(target)
    assert sorted(game.deck) == sorted(target) and len(game.deck) == 15
    assert result["steps"][-1]["grid"] == [1, 0]
    assert not game.editor


def test_open_editor_ignores_cursors_retained_after_close(tmp_path, monkeypatch):
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    service, game = _deck_service(tmp_path, ["card_id_002"] * 15)
    game._move("Edit", (0, 3))
    game.editor = False
    game.area = "Player"
    game.stale_visibility = True
    result = service.open_queens_blood_deck_editor()
    assert result["ok"] and game.editor


def test_initial_deck_popup_waits_past_an_intermediate_card_after_a_cursor_burst(tmp_path, monkeypatch):
    service, _ = _deck_service(tmp_path, ["card_id_002"] * 15)
    started = service._monotonic()
    def view(state, area):
        card = "card_id_008" if service._monotonic() - started >= 0.5 else "card_id_007"
        return (1, 0), card
    monkeypatch.setattr(service, "_deck_editor_view", view)
    assert service._deck_popup_card("Player", (1, 0)) == "card_id_008"


def test_adjacent_deck_popup_returns_as_soon_as_the_card_changes(tmp_path, monkeypatch):
    service, _ = _deck_service(tmp_path, ["card_id_002"] * 15)
    started = service._monotonic()
    monkeypatch.setattr(service, "_deck_editor_view", lambda state, area: ((0, 1), "card_id_008"))
    assert service._deck_popup_card("Player", (0, 1), previous="card_id_000") == "card_id_008"
    assert service._monotonic() - started < 0.1


def test_add_fifteenth_card_from_three_cell_row_starting_at_column_eight(tmp_path, monkeypatch):
    grid = sorted([f"card_id_{n:03}" for n in [0, 2, 6, 7, 13, 14, 24, 112, 116, 4, 8, 10, 118, 117]],
                  key=AgentService._deck_order_key)
    monkeypatch.setattr(FakeDeckScreen, "GRID", [grid[:11], grid[11:]])
    monkeypatch.setattr(FakeDeckScreen, "OWNED", {card: 2 for card in grid})
    before = [card for card in grid[:7] for _ in range(2)]
    service, game = _deck_service(tmp_path, before)
    game.editor = True
    game._move("Edit", (0, 8))
    service._deck_move("Edit", (1, 2))
    service._deck_fast_key("confirm")
    game.observe()
    taps = [action[1] for action in game.backend.actions if action[0] == "tap"]
    assert taps == ["queen_blood_left"] * 6 + ["down", "confirm"]
    assert len(game.deck) == 15 and game.deck[-1] == grid[13]
    # Returning to column eight must first leave the short final row.
    service._deck_move("Edit", (0, 8))
    assert game.observe().queen_blood["deck_screen"]["editor"]["cursors"]["Edit"]["index"] == 8


def test_failed_edit_restores_the_saved_deck_instead_of_leaving_14_cards(tmp_path, monkeypatch):
    from ff7r_agent.service import SafetyError
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    profile = _deck_profile(tmp_path)
    before = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    profile.record_deck(before, source="test")
    target = before.copy(); target.remove("card_id_007"); target.append("card_id_008")
    service, game = _deck_service(tmp_path, before)
    move = service._deck_move
    game.stale_visibility = True
    def fail_at_add(area, position):
        if area == "Edit" and position == (0, 3):
            raise SafetyError("cursor stalled")
        return move(area, position)
    monkeypatch.setattr(service, "_deck_move", fail_at_add)
    with pytest.raises(SafetyError, match="cursor stalled"):
        service.edit_queens_blood_deck(target)
    assert game.deck == before and not game.editor and game.dialog is None


def _guided(service, game, monkeypatch, tmp_path):
    """A live text reader for the fake: the deck screens' main guide."""
    game.path = tmp_path
    def texts():
        game.observe()
        guide = ("<button=square> Jump to Card  <button=R3> Copy Another Deck" if game.editor
                 else "<button=L1>/<button=R1> Change Deck  <button=touch> Toggle Details")
        shown = [("X.MainMenu_Card_TopNPC.WidgetTree.MainMenu_Card_Top.WidgetTree.Txt_Guide", guide)]
        if getattr(game, "tips", False):
            shown.append(("X:BP_EndGameInstance.BattleTips.WidgetTree.Txt_Btn", "<button=cancel> Close"))
        return shown
    monkeypatch.setattr(service, "_deck_screen_texts", texts)


def test_begin_match_is_refused_unless_deck_select_is_shown(tmp_path, monkeypatch):
    # Live 2026-10-06: F3 + Confirm in the open editor is Craft Prebuilt
    # Deck, which overwrote Deck #1 with the Starter Deck.
    from ff7r_agent.service import SafetyError
    service, game = _deck_service(tmp_path, ["card_id_002"] * 15)
    assert service._deck_screen_mode() is None  # no live reader: unguarded
    _guided(service, game, monkeypatch, tmp_path)
    service._assert_deck_select_for_begin()
    game.editor = True
    with pytest.raises(SafetyError, match="deck select is not shown"):
        service._assert_deck_select_for_begin()
    monkeypatch.setattr(service, "_deck_screen_texts", lambda: [])
    with pytest.raises(SafetyError, match="deck select is not shown"):
        service._assert_deck_select_for_begin()


def test_a_strip_confirm_on_the_wrong_card_is_refused_and_the_editor_left(tmp_path, monkeypatch):
    from ff7r_agent.service import SafetyError
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    before = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    target = before.copy(); target.remove("card_id_007"); target.append("card_id_008")
    service, game = _deck_service(tmp_path, before)
    _guided(service, game, monkeypatch, tmp_path)
    move = service._deck_move
    def off_by_one(area, position):
        move(area, position)
        if area == "Strip":  # the cursor lands one entry right of the target
            game._move("Strip", (0, position[1] + 1))
    monkeypatch.setattr(service, "_deck_move", off_by_one)
    with pytest.raises(SafetyError, match="not confirming") as raised:
        service.edit_queens_blood_deck(target)
    assert game.deck == before and not game.editor and game.dialog is None
    assert any("Deck after the failed edit" in note for note in raised.value.__notes__)


def test_a_grid_card_not_where_the_inventory_says_is_found_before_confirming(tmp_path, monkeypatch):
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    # A card won since the last scan shifts the grid one cell right.
    monkeypatch.setattr(FakeDeckScreen, "GRID",
                        [["card_id_000", "card_id_001", "card_id_002", "card_id_007", "card_id_008", "card_id_117"]])
    monkeypatch.setattr(FakeDeckScreen, "OWNED", {**FakeDeckScreen.OWNED, "card_id_001": 1})
    before = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    target = before.copy(); target.remove("card_id_007"); target.append("card_id_008")
    service, game = _deck_service(tmp_path, before)

    result = service.edit_queens_blood_deck(target)

    assert sorted(game.deck) == sorted(target)
    assert result["steps"][-1] == {"added": "card_id_008", "grid": [0, 4],
                                  "verified": True, "total_after": 15}


def test_a_failed_edit_on_a_full_deck_still_leaves_the_editor(tmp_path, monkeypatch):
    from ff7r_agent.service import SafetyError
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    before = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    target = before.copy(); target.remove("card_id_007"); target.append("card_id_008")
    service, game = _deck_service(tmp_path, before)
    _guided(service, game, monkeypatch, tmp_path)
    fresh = service._deck_fresh_cursor
    def stopped(area):
        if game.editor:
            raise SafetyError("cursor events stopped")
        fresh(area)
    monkeypatch.setattr(service, "_deck_fresh_cursor", stopped)
    with pytest.raises(SafetyError, match="cursor events stopped|cursor did not enter") as raised:
        service.edit_queens_blood_deck(target)
    assert not game.editor and game.dialog is None and sorted(game.deck) == sorted(before)
    service._assert_deck_select_for_begin()  # Begin Match is safe again
    assert not any("Begin Match is refused" in note for note in raised.value.__notes__)


def test_a_lost_down_after_the_strip_removals_is_pressed_again(tmp_path, monkeypatch):
    # Live 2026-10-06 the Down out of the strip after the last removal was
    # lost and the grid moves went to the strip.
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    before = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    target = before.copy(); target.remove("card_id_007"); target.append("card_id_008")
    service, game = _deck_service(tmp_path, before)
    original = game._press
    lost = []
    def press(button):
        if game.editor and game.area == "Strip" and button == "down" and not lost:
            lost.append(button)
            return
        original(button)
    game._press = press

    result = service.edit_queens_blood_deck(target)

    assert lost and sorted(game.deck) == sorted(target) and not game.editor
    assert result["steps"][-1] == {"added": "card_id_008", "grid": [0, 3],
                                  "verified": True, "total_after": 15}


def test_verified_edit_reads_twice_at_each_boundary_and_checks_every_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    before = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    target = ["card_id_008" if card == "card_id_007" else card for card in before]
    service, game = _deck_service(tmp_path, before)
    read, check = service._read_deck_list_fast, service._assert_deck_cursor_card
    reads, checks = [], []
    def reading():
        reads.append(True)
        return read()
    def checking(area, position, card, **kwargs):
        checks.append((area, card))
        return check(area, position, card, **kwargs)
    monkeypatch.setattr(service, "_read_deck_list_fast", reading)
    monkeypatch.setattr(service, "_assert_deck_cursor_card", checking)
    result = service.edit_queens_blood_deck(target)
    assert len(reads) == 4 and sorted(game.deck) == sorted(result["after"]) == sorted(target)
    assert checks == [("Strip", "card_id_007")] * 2 + [("Edit", "card_id_008")] * 2
    assert result["verification"] == "two_reads_after_edit_batch"


def test_dropped_confirm_is_not_reported_as_a_successful_edit(tmp_path, monkeypatch):
    from ff7r_agent.service import SafetyError
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    before = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    target = before.copy(); target.remove("card_id_007"); target.append("card_id_008")
    service, game = _deck_service(tmp_path, before)
    original = game._press
    def press(button):
        if button == "confirm" and game.area == "Edit" and game.dialog is None:
            return
        original(button)
    game._press = press
    with pytest.raises(SafetyError, match="did not change the deck count"):
        service.edit_queens_blood_deck(target)
    assert game.deck == before and not game.editor


def test_first_read_retries_a_missing_cursor_event_before_toggling_details(tmp_path, monkeypatch):
    service, game = _deck_service(tmp_path, ["card_id_002"] * 15)
    original = game._press
    lost = []
    def press(button):
        if not lost and button == "queen_blood_right":
            lost.append(button)
            return
        original(button)
    game._press = press
    assert service._read_deck_list_fast() == game.deck
    assert lost and not any(a[0] == "tap" and a[1] == "queen_blood_details" for a in game.backend.actions)


def test_popup_that_leaves_the_requested_cell_cannot_authorize_confirm(tmp_path, monkeypatch):
    service, _ = _deck_service(tmp_path, ["card_id_002"] * 15)
    started = service._monotonic()
    def view(state, area):
        return ((0, 0), "card_id_002") if service._monotonic() == started else ((0, 1), "card_id_007")
    monkeypatch.setattr(service, "_deck_editor_view", view)
    assert service._deck_popup_card("Player", (0, 0)) is None


def test_a_lost_details_toggle_is_retried_only_while_the_popup_is_hidden(tmp_path):
    service, game = _deck_service(tmp_path, ["card_id_002"] * 15)
    game.details = False
    original = game._press
    lost = []
    def press(button):
        if button == "queen_blood_details" and not lost:
            lost.append(button)
            return
        original(button)
    game._press = press
    assert service._read_deck_list_fast() == game.deck
    toggles = [a for a in game.backend.actions if a[0] == "tap" and a[1] == "queen_blood_details"]
    assert len(toggles) == 2 and game.details


def test_a_deck_move_is_refused_while_another_area_holds_the_cursor(tmp_path):
    from ff7r_agent.service import SafetyError
    service, game = _deck_service(tmp_path, ["card_id_002"] * 15)
    game.editor = True
    game._move("Edit", (0, 2))
    game._move("Strip", (0, 0))
    with pytest.raises(SafetyError, match="in the Strip, not the Edit"):
        service._deck_move("Edit", (0, 3))
    assert [action for action in game.backend.actions if action[0] == "tap"] == []


def test_a_deck_edit_will_not_start_with_the_editor_open(tmp_path, monkeypatch):
    from ff7r_agent.service import SafetyError
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    before = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    service, game = _deck_service(tmp_path, before)
    _guided(service, game, monkeypatch, tmp_path)
    game.editor = True
    with pytest.raises(SafetyError, match="starts on deck select"):
        service.edit_queens_blood_deck(before)
    assert [action for action in game.backend.actions if action[0] == "tap"] == []


def test_the_grid_lists_a_cost_zero_card_after_every_costed_card():
    # Live 2026-10-06 the owned grid (rank ascending) ended with Magic Pot
    # (cost 3) and then Insectoid Chimera (cost 0).
    owned = ["card_id_056", "card_id_117", "card_id_118", "card_id_000", "card_id_010", "card_id_112"]
    assert sorted(owned, key=AgentService._deck_order_key) == [
        "card_id_000", "card_id_112", "card_id_010", "card_id_118", "card_id_117", "card_id_056"]


def test_deck_lists_sort_a_cost_by_the_game_card_number_not_the_card_id():
    # Live 2026-10-12 the strip listed Riot Trooper (card_id_001, number 2),
    # Grenadier (card_id_016, number 3), Elphadunk (card_id_010, number 11).
    deck = ["card_id_075", "card_id_010", "card_id_016", "card_id_026", "card_id_001",
            "card_id_116", "card_id_019", "card_id_006", "card_id_000"]
    assert sorted(deck, key=AgentService._deck_order_key) == [
        "card_id_000", "card_id_006", "card_id_019", "card_id_116",
        "card_id_001", "card_id_016", "card_id_010", "card_id_026", "card_id_075"]


def test_a_tips_panel_over_deck_select_is_closed_before_editing(tmp_path, monkeypatch):
    # Live 2026-10-06 "Enfeebling Abilities" covered deck select after a win
    # and took its input; the deck read could not start.
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    before = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    target = before.copy(); target.remove("card_id_007"); target.append("card_id_008")
    service, game = _deck_service(tmp_path, before)
    _guided(service, game, monkeypatch, tmp_path)
    game.tips = True

    service.edit_queens_blood_deck(target)

    assert sorted(game.deck) == sorted(target) and not game.tips
    assert ("tap", "cancel", 400) in [action[:3] for action in game.backend.actions]


def test_begin_match_closes_a_tips_panel_and_refuses_if_it_stays(tmp_path, monkeypatch):
    from ff7r_agent.service import SafetyError
    service, game = _deck_service(tmp_path, ["card_id_002"] * 15)
    _guided(service, game, monkeypatch, tmp_path)
    game.tips = True
    service._assert_deck_select_for_begin()
    assert not game.tips
    game.tips = True
    game._press = lambda button: None  # a panel that will not close
    with pytest.raises(SafetyError, match=r"not shown \(tips\)"):
        service._assert_deck_select_for_begin()


def test_each_editor_area_reads_its_own_details_popup():
    # Live 2026-10-06 both editor popups read as shown; the strip's card
    # check took the grid's stale Fleetwing while the strip was on a Wolf.
    def popup(number, card, shown):
        name = f"MainMenu_Card_CardInformation_C_{number}"
        return {"popup": name, "path": f"X.{name}.WidgetTree", "card_id": card, "popup_shown": shown}
    both = {"detail": {"resolved": [popup(2147474949, "card_id_024", True), popup(2147474947, "card_id_007", True)]}}
    assert AgentService._deck_area_hovered(both, "Strip") == "card_id_007"
    assert AgentService._deck_area_hovered(both, "Edit") == "card_id_024"
    grid_only = {"detail": {"resolved": [popup(2147474949, "card_id_024", True), popup(2147474947, "card_id_007", None)]}}
    assert AgentService._deck_area_hovered(grid_only, "Strip") is None


def test_native_final_read_does_not_nudge_player_after_editor_close(tmp_path, monkeypatch):
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    before = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    target = ["card_id_008" if c == "card_id_007" else c for c in before]
    service, game = _deck_service(tmp_path, before)
    fresh = service._deck_fresh_cursor
    refreshed = []
    def refresh(area):
        refreshed.append(area)
        assert area != "Player", "native read must not nudge the closed editor list"
        return fresh(area)
    monkeypatch.setattr(service, "_deck_fresh_cursor", refresh)
    monkeypatch.setattr(service, "_read_queen_blood_arrays", lambda area: {"seconds": 0.1, "arrays": {"Player": {"card_ids": list(game.deck)}}})
    result = service.edit_queens_blood_deck(target)
    assert not game.editor and sorted(game.deck) == sorted(target) == sorted(result["after"])
    assert "Edit" in refreshed and "Strip" in refreshed
    assert result["verification"] == "two_reads_after_edit_batch"
    timing = result["attempt_timings"][0]
    assert set(timing["edit_phases_seconds"]) == {"open_and_details", "removals", "additions", "close_and_select"}
    assert timing["edit_seconds"] >= sum(timing["edit_phases_seconds"].values()) - 0.01


def _grid_unlike_the_profile(tmp_path, monkeypatch):
    # #341: the live grid differed from the profile's cost/number layout, and
    # each card was found by walking the grid (about 50 s an addition).
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "deck-test")
    _deck_profile(tmp_path)
    monkeypatch.setattr(FakeDeckScreen, "GRID", [["card_id_000", "card_id_002", "card_id_005",
                                                  "card_id_007", "card_id_008", "card_id_117"]])
    monkeypatch.setattr(FakeDeckScreen, "OWNED", {**FakeDeckScreen.OWNED, "card_id_005": 2})
    before = ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_117"] + ["card_id_002"] * 10
    target = ["card_id_008" if c == "card_id_007" else c for c in before]
    service, game = _deck_service(tmp_path, before)
    searched = []
    find = service._deck_find
    def walk(area, card_id, *args, **kwargs):
        searched.append(card_id)
        return find(area, card_id, *args, **kwargs)
    monkeypatch.setattr(service, "_deck_find", walk)
    return service, game, target, searched


def test_additions_go_straight_to_the_native_grid_cell(tmp_path, monkeypatch):
    service, game, target, searched = _grid_unlike_the_profile(tmp_path, monkeypatch)
    def arrays(area):
        return {"seconds": 0.1, "arrays": {"Player": {"card_ids": list(game.deck)},
                                           "Edit": {"card_ids": [c for row in game.GRID for c in row]}}}
    monkeypatch.setattr(service, "_read_queen_blood_arrays", arrays)
    result = service.edit_queens_blood_deck(target)
    assert sorted(game.deck) == sorted(target) and not game.editor
    assert searched == []
    added = [step for step in result["attempt_timings"][0]["step_timings"] if "added" in step]
    assert [(step["added"], step["grid"], step["located"]) for step in added] == [
        ("card_id_008", [0, 4], "native_grid")] * 2
    assert all(step["seconds"] >= 0 for step in result["attempt_timings"][0]["step_timings"])


def test_without_a_native_grid_a_mislaid_card_is_still_found_by_walking(tmp_path, monkeypatch):
    service, game, target, searched = _grid_unlike_the_profile(tmp_path, monkeypatch)
    result = service.edit_queens_blood_deck(target)
    assert sorted(game.deck) == sorted(target) and not game.editor
    assert searched == ["card_id_008"]
    added = [step for step in result["attempt_timings"][0]["step_timings"] if "added" in step]
    assert [(step["grid"], step["located"]) for step in added] == [([0, 4], "grid_search")] * 2
