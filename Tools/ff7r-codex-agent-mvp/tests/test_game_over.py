from __future__ import annotations

from types import SimpleNamespace

from ff7r_agent.game_over import read_game_over, retry_last_battle

ROOT = "/Engine/Transient.GameEngine_2147482600:BP_EndGameInstance."
# Live 2026-10-20, Mt. Nibel: Cloud fell to the Velociwings.
GAME_OVER = [
    (ROOT + "Com_Btn_00.WidgetTree.Txt", "$menu_CheckPoint_MenuName"),
    (ROOT + "Com_Btn_00.WidgetTree.Txt", "$menu_gameover_0001_0000"),
    (ROOT + "GameOver.WidgetTree.Txt_GameOver", "$menu_gameover_0000_0000"),
    (ROOT + "Com_Btn_00.WidgetTree.Txt", "$menu_gameover_0001_0001"),
]


PROMPT = [
    (ROOT + "Com_Window_01.WidgetTree.Btn_01.WidgetTree.Txt", "$menu_pause_0003_0002"),
    (ROOT + "Com_Window_01.WidgetTree.Btn_00.WidgetTree.Txt", "$menu_pause_0003_0001"),
    (ROOT + "Com_Window_01.WidgetTree.Txt", "Resume playing from just before the last battle?"),
]


class Game:
    def __init__(self, shown=True, paused=True, in_menu=False, closes=True):
        self.shown, self.paused, self.in_menu, self.closes = shown, paused, in_menu, closes
        self.prompt = False
        self.pressed = []
        self.clock = 0.0
        self.telemetry = SimpleNamespace(observe=lambda: SimpleNamespace(paused=self.paused, in_menu=self.in_menu))

    def _deck_screen_texts(self):
        return (list(GAME_OVER) + (PROMPT if self.prompt else [])) if self.shown else []

    def _monotonic(self):
        return self.clock

    def _sleeper(self, seconds):
        self.clock += seconds

    def tap(self, button, milliseconds):
        self.pressed.append(button)
        if not self.prompt:
            self.prompt = True  # live: Retry opens the resume prompt first
        elif self.closes:
            self.shown = False


def test_the_live_game_over_screen_lists_its_retry_options():
    assert read_game_over(GAME_OVER) == {"title": "$menu_gameover_0000_0000",
                                         "options": ["retry_last_battle", "retry_checkpoint"]}
    assert read_game_over(GAME_OVER[:2]) is None


def test_retry_confirms_the_preselected_last_battle_and_checks_it_closed():
    game = Game()
    assert retry_last_battle(game, False, game.tap)["outcome"] == "dry_run" and game.pressed == []
    result = retry_last_battle(game, True, game.tap)
    assert result["ok"] and result["outcome"] == "retrying_last_battle" and game.pressed == ["confirm", "confirm"]


def test_a_resume_prompt_already_up_is_confirmed_once():
    game = Game()
    game.prompt = True
    assert retry_last_battle(game, False, game.tap)["resume_prompt"] is True
    result = retry_last_battle(game, True, game.tap)
    assert result["ok"] and game.pressed == ["confirm"]


def test_nothing_is_pressed_unless_the_screen_holds_the_game():
    for game, outcome in ((Game(shown=False), "game_over_not_shown"),
                          (Game(paused=False), "game_over_not_holding_the_game"),
                          (Game(in_menu=True), "game_over_not_holding_the_game")):
        result = retry_last_battle(game, True, game.tap)
        assert result["outcome"] == outcome and result["input_sent"] is False and game.pressed == []


def test_a_screen_that_stays_is_reported():
    game = Game(closes=False)
    result = retry_last_battle(game, True, game.tap)
    assert result["ok"] is False and result["outcome"] == "game_over_still_shown"
    assert game.pressed == ["confirm", "confirm"] and result["step"] == "resume_prompt"


def test_the_operation_is_registered_with_the_runtime():
    from ff7r_agent.runtime_commands import SERVICE_ALIASES

    assert SERVICE_ALIASES["retry_after_game_over"] == "retry_after_game_over"
