"""The Game Over screen: read it and retry from the last battle.

Live 2026-10-20 (Mt. Nibel, the Velociwing fight, Classic mode on Easy):
Cloud fell and the Game Over screen held the game paused with ``in_menu``
clear. The screen texts listed ``BP_EndGameInstance.GameOver.WidgetTree.``
``Txt_GameOver`` (``$menu_gameover_0000_0000``) and its ``Com_Btn_00``
options: ``$menu_gameover_0001_0000`` (Retry from Last Battle) and
``_0001`` (Retry from Checkpoint), above Return to Title Screen. Retry from
Last Battle was highlighted when the screen opened. The text reader does not
report the highlight, so only that preselected default is confirmed; the
other options are not navigated to. Confirming it opens a ``Com_Window_01``
prompt ("Resume playing from just before the last battle?") with
``Btn_00`` Yes (``$menu_pause_0003_0001``, highlighted) and ``Btn_01`` No
(``$menu_pause_0003_0002``); its Yes is confirmed in turn.
"""
from __future__ import annotations

from typing import Any, Callable

GAME_OVER_TITLE_SUFFIX = ".GameOver.WidgetTree.Txt_GameOver"
GAME_OVER_TITLE_KEY = "$menu_gameover_0000_0000"
GAME_OVER_BUTTON_SUFFIX = ".Com_Btn_00.WidgetTree.Txt"
GAME_OVER_OPTIONS = {
    "$menu_gameover_0001_0000": "retry_last_battle",
    "$menu_gameover_0001_0001": "retry_checkpoint",
    "$menu_gameover_0001_0002": "return_to_title",
}
PROMPT_YES_SUFFIX = ".Com_Window_01.WidgetTree.Btn_00.WidgetTree.Txt"
PROMPT_YES_KEY = "$menu_pause_0003_0001"
PROMPT_NO_SUFFIX = ".Com_Window_01.WidgetTree.Btn_01.WidgetTree.Txt"
PROMPT_NO_KEY = "$menu_pause_0003_0002"
CONFIRM_MS = 120
CLOSE_WAIT_SECONDS = 10.0
POLL_SECONDS = 0.5


def read_game_over(texts: list[tuple[str, str]]) -> dict[str, Any] | None:
    """The Game Over screen in the screen texts, or None: its options in order."""
    if not any(path.endswith(GAME_OVER_TITLE_SUFFIX) and text == GAME_OVER_TITLE_KEY for path, text in texts):
        return None
    options = [GAME_OVER_OPTIONS[text] for path, text in texts
               if path.endswith(GAME_OVER_BUTTON_SUFFIX) and text in GAME_OVER_OPTIONS]
    return {"title": GAME_OVER_TITLE_KEY, "options": options}


def resume_prompt_shown(texts: list[tuple[str, str]]) -> bool:
    """The Yes/No prompt that asks to resume from just before the last battle."""
    return (any(path.endswith(PROMPT_YES_SUFFIX) and text == PROMPT_YES_KEY for path, text in texts)
            and any(path.endswith(PROMPT_NO_SUFFIX) and text == PROMPT_NO_KEY for path, text in texts))


def retry_last_battle(service: Any, execute: bool, tap: Callable[[str, int], Any]) -> dict[str, Any]:
    """Confirm the preselected Retry from Last Battle and check the screen closed."""
    presses: list[str] = []

    def report(ok: bool, outcome: str, screen: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
        return {"ok": ok, "outcome": outcome, "execute": execute, "screen": screen,
                "presses": presses, "input_sent": bool(presses), **extra}

    state = service.telemetry.observe()
    texts = service._deck_screen_texts()
    screen = read_game_over(texts)
    if screen is None:
        return report(False, "game_over_not_shown", None)
    # The widget may stay loaded once hidden: the screen also holds the game
    # paused outside any menu.
    if not state.paused or state.in_menu:
        return report(False, "game_over_not_holding_the_game", screen,
                      paused=bool(state.paused), in_menu=bool(state.in_menu))
    if "retry_last_battle" not in screen["options"]:
        return report(False, "retry_option_not_shown", screen)
    # Either the options (Retry highlighted) or the resume prompt (Yes
    # highlighted) is up; each is confirmed once.
    prompt = resume_prompt_shown(texts)
    if not execute:
        return report(True, "dry_run", screen, resume_prompt=prompt)
    for step in (("resume_prompt",) if prompt else ("retry_option", "resume_prompt")):
        tap("confirm", CONFIRM_MS)
        presses.append("confirm")
        deadline = service._monotonic() + CLOSE_WAIT_SECONDS
        while True:
            service._sleeper(POLL_SECONDS)
            texts = service._deck_screen_texts()
            if read_game_over(texts) is None or not service.telemetry.observe().paused:
                return report(True, "retrying_last_battle", screen)
            if step == "retry_option" and resume_prompt_shown(texts):
                break
            if service._monotonic() >= deadline:
                return report(False, "game_over_still_shown", screen, step=step)
    return report(False, "game_over_still_shown", screen, step="resume_prompt")
