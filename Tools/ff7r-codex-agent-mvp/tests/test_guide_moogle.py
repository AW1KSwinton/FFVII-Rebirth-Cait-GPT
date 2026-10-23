"""Gold Saucer Guide Moogles, against a fake of the menu as seen live (2026-10-04)."""
from __future__ import annotations

import pytest

from ff7r_agent import field_prompt, guide_moogle, shop
from ff7r_agent.guide_moogle import ZONES, nearest_zone, read_menu, worth_guide_moogle, zone
from ff7r_agent.navigation_values import MissionError

P = "/Engine/Transient.GameEngine_1:BP_EndGameInstance."


class FakeMoogle:
    """A Guide Moogle in Fantasyworld: Talk opens its list on the current
    zone; up and down move the highlight (no wrap); confirm travels."""

    def __init__(self, here="Fantasyworld", lose=()):
        self.here = zone(here).key
        self.open = False
        self.cursor = [z.key for z in ZONES].index(self.here)
        self.presses, self.lose, self.travelled, self.talks = [], list(lose), None, 0

    def talk_to_npc(self, settle_seconds):
        self.talks += 1
        self.open = True

    def tap(self, button, milliseconds):
        self.presses.append(button)
        if self.lose and self.lose[0] == len(self.presses):
            self.lose.pop(0)
            return
        if button == "down":
            self.cursor = min(self.cursor + 1, len(ZONES) - 1)
        elif button == "up":
            self.cursor = max(self.cursor - 1, 0)
        elif button == "confirm":
            self.travelled, self.open = ZONES[self.cursor].key, False
        elif button == "cancel":
            self.open = False

    def texts(self):
        rows = [(P + "SaveIcon.WidgetTree.Txt_Info", "$menu_save_load_0005_0000")]
        if self.open:
            menu = P + "FastTravel.WidgetTree.FastTravel_Menu.WidgetTree."
            rows += [(menu + "Txt_Title", guide_moogle.MENU_TITLE_KEY),
                     (menu + "Txt_LocaName", ZONES[self.cursor].key),
                     (menu + "Txt_CurrentName", self.here)]
            # The rows come back in no particular order.
            rows += [(P + "FastTravel_Cell.WidgetTree.Txt_Name", z.key) for z in reversed(ZONES)]
        return [{"path": path, "text": text} for path, text in rows]


@pytest.fixture
def moogle(monkeypatch):
    monkeypatch.setattr(shop, "read_screen_texts", lambda service, contains=None: {"texts": service.texts()})
    monkeypatch.setattr(field_prompt, "read_field_prompt",
                        lambda service: {"talk": True, "commands": ["$level_action_talk"]})
    return FakeMoogle


def test_ghost_square_is_three_rows_below_fantasyworld(moogle):
    service = moogle()
    result = guide_moogle.use(service, "Ghost Square", execute=True, sleep=lambda _: None)
    assert service.presses == ["down", "down", "down", "confirm"]
    assert service.travelled == zone("Ghost Square").key
    assert result["state"] == "travelling" and result["from"] == "Fantasyworld"
    assert result["arrival"] == {"x": 53719.0, "y": 4485.0, "z": 54867.0}


def test_a_zone_above_is_reached_pressing_up(moogle):
    service = moogle()
    guide_moogle.use(service, "welcome area", execute=True, sleep=lambda _: None)
    assert service.presses == ["up"] * 6 + ["confirm"]


def test_a_dropped_press_is_corrected_before_confirming(moogle):
    service = moogle(lose=[2])
    result = guide_moogle.use(service, "Ghost Square", execute=True, sleep=lambda _: None)
    assert service.travelled == zone("Ghost Square").key and result["presses"] == 4


def test_the_current_zone_closes_the_menu_without_travelling(moogle):
    service = moogle(here="Ghost Square")
    result = guide_moogle.use(service, "Ghost Square", execute=True, sleep=lambda _: None)
    assert result["state"] == "already_there" and service.presses == ["cancel"] and service.travelled is None


def test_without_execute_nothing_is_pressed(moogle):
    service = moogle()
    assert guide_moogle.use(service, "Ghost Square", sleep=lambda _: None)["state"] == "would_talk"
    assert service.talks == 0 and service.presses == []


def test_no_talk_prompt_is_refused(moogle, monkeypatch):
    monkeypatch.setattr(field_prompt, "read_field_prompt", lambda service: {"talk": False, "commands": []})
    with pytest.raises(MissionError, match="guide_moogle_not_offered"):
        guide_moogle.use(moogle(), "Ghost Square", execute=True, sleep=lambda _: None)


def test_an_unknown_zone_is_refused():
    with pytest.raises(MissionError, match="guide_moogle_unknown_destination"):
        zone("Midgar")


def test_the_menu_reads_rows_in_menu_order_whatever_order_they_come_in():
    service = FakeMoogle()
    service.open = True
    menu = read_menu(service.texts())
    assert menu["open"] and menu["rows"] == [z.key for z in ZONES]
    assert menu["selected"] == menu["current"] == zone("Fantasyworld").key
    assert not read_menu([])["open"]


def test_a_long_walk_across_the_gold_saucer_rides_a_moogle():
    lidrehl = {"x": 52700.0, "y": 3458.0, "z": 54775.0}  # Ghost Square
    fantasyworld = {"x": 72932.0, "y": -16432.0, "z": 48819.0}
    assert worth_guide_moogle(fantasyworld, lidrehl) == zone("Ghost Square")
    # Already in Ghost Square, or nowhere near the Gold Saucer: walk.
    assert worth_guide_moogle({"x": 53718.0, "y": 4484.0, "z": 54861.0}, lidrehl) is None
    assert worth_guide_moogle({"x": 0.0, "y": 0.0, "z": 0.0}, lidrehl) is None
    assert nearest_zone(lidrehl)[0].name == "Ghost Square"
