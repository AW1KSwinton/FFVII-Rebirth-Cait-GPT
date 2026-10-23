import pytest

from ff7r_agent import combat_simulator, field_prompt
from ff7r_agent.combat_simulator import ROWS, SIMULATOR_ROW, enter, leave, read_screen
from ff7r_agent.navigation_values import MissionError

BASE = "/Engine/Transient.GameEngine_1:BP_EndGameInstance."


class Game:
    """Chadley as seen live 2026-10-06: a greeting that takes no input, his
    shop-screen menu (cursor on Develop Materia), scenes, then the simulator."""

    def __init__(self, greeting_reads=3, scene_polls=2, drop_first_press=False, talk=True):
        self.screen = "field"
        self.greeting = greeting_reads
        self.scene_polls = scene_polls
        self.drop = drop_first_press
        self.talk = talk
        self.row = 0
        self.taps = []
        self.skips = 0

    # The service calls the module makes.
    def talk_to_npc(self, settle_seconds=1.5):
        self.taps.append("talk")
        self.screen = "greeting"

    def _press_simulator_button(self, button, milliseconds):
        self.taps.append(button)
        if self.screen == "menu":
            if button in ("down", "up"):
                if self.drop:
                    self.drop = False
                    return
                self.row = max(0, min(len(ROWS) - 1, self.row + (1 if button == "down" else -1)))
            elif button == "confirm" and ROWS[self.row] == SIMULATOR_ROW:
                self.screen = "scene"
            elif button == "cancel":
                self.screen = "field"
        elif self.screen == "simulator" and button == "cancel":
            self.screen = "menu"

    def cutscene_state(self):
        if self.screen != "scene":
            return {"observed": True, "active": False, "scenes": []}
        self.scene_polls -= 1
        if self.scene_polls < 0:
            self.screen = "simulator"
            return {"observed": True, "active": False, "scenes": []}
        return {"observed": True, "active": True, "scenes": [{"sequence": "EV_COMON_2112"}]}

    def skip_known_cutscene(self, state):
        self.skips += 1
        return {"ok": False, "reason": "not_skippable", "scenes": ["EV_COMON_2112"]}

    # What the exporter's menu-text read would show.
    def texts(self):
        if self.screen == "greeting":
            self.greeting -= 1
            if self.greeting < 0:
                self.screen = "menu"
            return [{"path": BASE + "Subtitle00.WidgetTree.Txt_Lines", "text": "Why hello, Cloud."}]
        if self.screen == "menu":
            return [{"path": BASE + "Shop.WidgetTree.Shop_Menu.WidgetTree.Txt_ShopName", "text": "$CHADLEY_SHOP_GONGAGA"},
                    {"path": BASE + "Shop.WidgetTree.Shop_Foot.WidgetTree.Txt_Help", "text": ROWS[self.row]}]
        if self.screen == "simulator":
            return [{"path": BASE + "VR_Top.WidgetTree.InfoWindow.WidgetTree.Txt_MemberNum",
                     "text": "$menu_Chadrey_VR_SystemWord_MemberCount"}]
        return []


@pytest.fixture
def game(monkeypatch):
    made = {}

    def make(**kwargs):
        made["game"] = Game(**kwargs)
        return made["game"]

    monkeypatch.setattr(combat_simulator, "screen_texts", lambda service: service.texts())
    monkeypatch.setattr(field_prompt, "read_field_prompt",
                        lambda service: {"talk": service.talk, "commands": ["$level_action_talk"] if service.talk else []})
    return make


def run(fn, game, **kwargs):
    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += seconds

    return fn(game, sleep=sleep, clock=lambda: clock["now"], **kwargs)


def test_entering_waits_out_the_greeting_then_picks_the_simulator_by_its_help_text(game):
    chadley = game()
    result = run(enter, chadley, execute=True)
    assert result["ok"] is True and result["state"] == "in_simulator" and result["talked"] is True
    # Nothing is pressed during the greeting (live, presses then were dropped).
    assert chadley.taps == ["talk", "down", "confirm"]
    assert result["scenes"] == ["EV_COMON_2112"] and chadley.skips == 2


def test_a_dropped_press_is_made_up_before_confirming(game):
    chadley = game(drop_first_press=True)
    result = run(enter, chadley, execute=True)
    assert result["ok"] is True and chadley.taps == ["talk", "down", "down", "confirm"]


def test_scenes_are_left_to_play_when_skipping_is_off(game):
    chadley = game()
    assert run(enter, chadley, execute=True, skip_scenes=False)["ok"] is True and chadley.skips == 0


def test_a_dry_run_presses_nothing(game):
    chadley = game()
    assert run(enter, chadley)["state"] == "would_talk" and chadley.taps == []


def test_no_talk_prompt_means_no_chadley(game):
    chadley = game(talk=False)
    with pytest.raises(MissionError, match="chadley_not_offered"):
        run(enter, chadley, execute=True)
    assert chadley.taps == []


def test_a_menu_that_never_opens_is_reported_without_presses(game):
    chadley = game(greeting_reads=10_000)
    with pytest.raises(MissionError, match="chadley_menu_not_opened"):
        run(enter, chadley, execute=True)
    assert chadley.taps == ["talk"]


def test_leaving_backs_out_to_the_field(game):
    chadley = game()
    run(enter, chadley, execute=True)
    result = run(leave, chadley)
    assert result == {"ok": True, "left_simulator": True, "closed_menu": True, "seconds": result["seconds"]}
    assert chadley.taps[-2:] == ["cancel", "cancel"] and chadley.screen == "field"


def test_only_chadleys_menu_and_the_simulator_are_recognised():
    other_shop = [{"path": BASE + "Shop.WidgetTree.Shop_Menu.WidgetTree.Txt_ShopName", "text": "$KALMT_ITEM_000"}]
    assert read_screen(other_shop) == {"menu": False, "shop": None, "row": None, "simulator": False}
    gold_saucer_vr = [{"path": BASE + "VR_Top.WidgetTree.InfoWindow.WidgetTree.Txt_Turn", "text": "1"}]
    assert read_screen(gold_saucer_vr)["simulator"] is False
