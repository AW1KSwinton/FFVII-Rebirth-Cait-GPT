from __future__ import annotations

import pytest

from ff7r_agent.input import ALLOWED_BUTTONS, DEFAULT_KEYMAP
from ff7r_agent.service import BATTLE_MENU_BUTTONS, AgentService, SafetyError


def test_the_commands_menu_key_is_the_battle_hud_glyph():
    assert "battle_command_menu" in ALLOWED_BUTTONS and DEFAULT_KEYMAP["battle_command_menu"] == "c"
    assert BATTLE_MENU_BUTTONS == {"battle_command_menu", "up", "down", "confirm", "cancel"}


@pytest.mark.parametrize("button", ["attack", "menu", "map", "dialogue_left"])
def test_other_buttons_are_not_battle_menu_presses(button):
    with pytest.raises(SafetyError, match="battle command menu"):
        AgentService.battle_menu_tap(object(), button)


def test_the_operation_is_registered_with_the_runtime():
    from ff7r_agent.runtime_commands import SERVICE_ALIASES

    assert SERVICE_ALIASES["battle_menu_tap"] == "battle_menu_tap"
