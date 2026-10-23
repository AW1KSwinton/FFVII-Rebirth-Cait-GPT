"""The field interaction prompt, read from its text widget.

Live 2026-09-26 the prompt shown next to a talkable NPC ("Y Talk") was the
visible text block ``FieldAction_Command.Txt_Command`` = ``$level_action_talk``
(other actions use other keys, e.g. ``$level_sys_buggy_call``). The map
query's all-texts scan reads it in the field; hidden prompts are not listed.
"""
from __future__ import annotations

from typing import Any

from .map_query import request_snapshot

TALK_KEY = "$level_action_talk"
# "Y Open" in front of a door gimmick (live 2026-09-29 at Nibelheim's
# BG0007_00_Door2Bar_Standard, shown from 1.2 m).
DOOR_KEY = "$level_action_door"
# "Terrain Action" (vault, climb, squeeze through), on the dodge button.
TERRAIN_ACTION_KEY = "$level_sys_show_parkour_input"
COMMAND_PATH = "FieldAction_Command.WidgetTree.Txt_Command"


def read_field_prompt(service: Any) -> dict[str, Any]:
    # The exporter's `fieldprompt` read walks only the prompt widgets; an
    # exporter without it leaves `field_prompt` out, and the all-texts scan
    # (1.5-3.5 s a read live 2026-10-05) answers instead.
    response, base = request_snapshot(service, heights=False, flags=("fieldprompt",))
    prompt = response.get("field_prompt")
    if isinstance(prompt, dict) and isinstance(prompt.get("commands"), list):
        commands = [str(item) for item in prompt["commands"]]
    else:
        response, base = request_snapshot(service, heights=False, flags=("alltexts",))
        commands = [str(item.get("text", "")) for item in response.get("texts") or []
                    if isinstance(item, dict) and str(item.get("path", "")).endswith(COMMAND_PATH)]
    return {
        "commands": commands,
        "talk": TALK_KEY in commands,
        "door": DOOR_KEY in commands,
        "terrain_action": TERRAIN_ACTION_KEY in commands,
        "source_sequence": response.get("source_sequence"),
        "player_position": (base.get("player") or {}).get("position"),
    }
