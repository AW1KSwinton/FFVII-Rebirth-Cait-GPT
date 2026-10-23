"""Chadley's combat simulator: entering it from beside Chadley, and leaving.

Live 2026-10-06 at Gongaga's Chadley:

* Talking plays his greeting first: voiced subtitle lines for 3 s to over
  20 s (longer when he announces a newly unlocked fight). His menu takes no
  input until it shows; presses during the greeting are dropped.
* The menu is a shop screen. ``Shop_Menu.Txt_ShopName`` reads
  ``$CHADLEY_SHOP_GONGAGA`` only while it shows, and ``Shop_Foot.Txt_Help``
  is the highlighted row's help text, one per row: Develop Materia, Use
  Combat Simulator, Ask about World Intel, top to bottom. The cursor opens on
  Develop Materia.
* Confirming Use Combat Simulator plays Chadley's intro (``EV_COMON_2110``,
  skippable, only on the first entry after a game load) and the VR
  transition (``EV_COMON_2112``, not skippable), then the simulator's screen,
  whose ``VR_Top`` info window holds ``$menu_Chadrey_VR_SystemWord_MemberCount``.
* Cancel on the simulator's screen goes straight back to his menu; Cancel
  there closes it.
* Neither screen sets the game's menu or pause flag, and after a simulator
  battle the arena counts as loaded gameplay, so presses go through the
  service's simulator press, made only right after the screen is read.

Texts are read with the exporter's ``menutexts`` scan (0.2-0.4 s, also
during the greeting); every-text reads took 7-9 s a read in conversation.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .navigation_values import MissionError

SHOP_NAME_SUFFIX = "Shop_Menu.WidgetTree.Txt_ShopName"
HELP_SUFFIX = "Shop.WidgetTree.Shop_Foot.WidgetTree.Txt_Help"
CHADLEY_SHOP_PREFIX = "$CHADLEY_SHOP_"
# Menu order, top to bottom, by each row's help text.
ROWS = (
    "$menu_chadleyshop_help_materia_development",
    "$menu_chadleyshop_help_battle_simulator",
    "$menu_chadleyshop_help_about_worldreport",
)
SIMULATOR_ROW = ROWS[1]
SIMULATOR_TEXT = "$menu_Chadrey_VR_SystemWord_MemberCount"
SNAPSHOT_FLAGS = ("menutexts",)
FALLBACK_FLAGS = ("alltexts",)

PRESS_MS = 100
PRESS_SETTLE_SECONDS = 0.35
POLL_SECONDS = 0.3
# The greeting ran past 20 s live.
MENU_WAIT_SECONDS = 40.0
LEAVE_WAIT_SECONDS = 8.0


def read_screen(texts: list[dict[str, Any]]) -> dict[str, Any]:
    """Chadley's menu and the simulator from screen texts: the shop id
    (``CHADLEY_SHOP_GONGAGA``) while his menu shows, the highlighted row's
    help key, and whether the simulator's screen shows."""
    shop = row = None
    simulator = False
    for item in texts:
        path, text = str(item.get("path", "")), str(item.get("text", ""))
        if path.endswith(SHOP_NAME_SUFFIX) and text.startswith(CHADLEY_SHOP_PREFIX):
            shop = text[1:]
        elif path.endswith(HELP_SUFFIX) and text in ROWS:
            row = text
        elif ".VR_Top." in path and text == SIMULATOR_TEXT:
            simulator = True
    return {"menu": shop is not None, "shop": shop, "row": row, "simulator": simulator}


def screen_texts(service: Any) -> list[dict[str, Any]]:
    """The menu texts, or every text from an exporter without ``menutexts``."""
    from .map_query import request_snapshot
    response, _ = request_snapshot(service, heights=False, flags=SNAPSHOT_FLAGS, require_map=False)
    if response.get("texts_scope") != "menu":
        response, _ = request_snapshot(service, heights=False, flags=FALLBACK_FLAGS, require_map=False)
    return [item for item in response.get("texts") or [] if isinstance(item, dict)]


def presses_to(row: str | None) -> tuple[str, int]:
    """Direction and count from the highlighted row to Use Combat Simulator."""
    if row not in ROWS:
        raise MissionError("chadley_menu_highlight_unreadable")
    steps = ROWS.index(SIMULATOR_ROW) - ROWS.index(row)
    return ("down" if steps >= 0 else "up"), abs(steps)


def _wait(read: Callable[[], dict[str, Any]], done: Callable[[dict[str, Any]], bool], seconds: float,
          sleep: Callable[[float], None], clock: Callable[[], float]) -> dict[str, Any]:
    deadline = clock() + seconds
    while True:
        screen = read()
        if done(screen) or clock() >= deadline:
            return screen
        sleep(POLL_SECONDS)


def enter(service: Any, *, execute: bool = False, skip_scenes: bool = True, timeout_seconds: float = 60.0,
          sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Beside Chadley (Talk shown) or with his menu open: talk, wait for his
    menu, highlight Use Combat Simulator, check it and confirm, then wait for
    the simulator's screen. Scenes on the way are skipped where they allow it
    (``skip_scenes``); one that does not is waited out."""
    from . import field_prompt
    started = clock()

    def read() -> dict[str, Any]:
        return read_screen(screen_texts(service))

    screen = read()
    if screen["simulator"]:
        return {"ok": True, "state": "already_in_simulator", "seconds": 0.0}
    talked = False
    if not screen["menu"]:
        prompt = field_prompt.read_field_prompt(service)
        if not prompt.get("talk"):
            raise MissionError(f"chadley_not_offered:{prompt.get('commands')}")
        if not execute:
            return {"ok": True, "execute": False, "state": "would_talk"}
        service.talk_to_npc(settle_seconds=0.0)
        talked = True
        screen = _wait(read, lambda now: now["menu"], MENU_WAIT_SECONDS, sleep, clock)
        if not screen["menu"]:
            raise MissionError("chadley_menu_not_opened")
    elif not execute:
        return {"ok": True, "execute": False, "state": "would_select", "screen": screen}
    menu_seconds = round(clock() - started, 2)
    presses = 0
    for _ in range(2):  # one correction if a press was dropped
        direction, count = presses_to(screen["row"])
        for _ in range(count):
            service._press_simulator_button(direction, PRESS_MS)
            presses += 1
            sleep(PRESS_SETTLE_SECONDS)
        if count:
            screen = read()
        if screen["row"] == SIMULATOR_ROW:
            break
    else:
        raise MissionError(f"chadley_menu_highlight_wrong:{screen['row']}")
    service._press_simulator_button("confirm", PRESS_MS)
    confirmed = clock()
    scenes: list[str] = []
    skips: list[dict[str, Any]] = []
    deadline = confirmed + float(timeout_seconds)
    while clock() < deadline:
        cutscene = service.cutscene_state()
        if cutscene.get("active"):
            playing = sorted(item.get("sequence") for item in cutscene.get("scenes") or [])
            scenes.extend(scene for scene in playing if scene not in scenes)
            if skip_scenes:
                skipped = service.skip_known_cutscene(cutscene)
                if skipped is not None:
                    skips.append({key: skipped.get(key) for key in ("ok", "reason", "skipped", "scenes", "seconds")
                                  if skipped.get(key) is not None})
            sleep(POLL_SECONDS)
            continue
        if read()["simulator"]:
            return {"ok": True, "state": "in_simulator", "talked": talked, "menu_seconds": menu_seconds,
                    "presses": presses, "scenes": scenes, "skips": skips,
                    "after_confirm_seconds": round(clock() - confirmed, 2),
                    "seconds": round(clock() - started, 2)}
        sleep(POLL_SECONDS)
    raise MissionError("combat_simulator_not_opened")


def leave(service: Any, *, close_menu: bool = True, sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """From the simulator's screen back to Chadley's menu, and with
    ``close_menu`` out of it to the field."""
    def read() -> dict[str, Any]:
        return read_screen(screen_texts(service))

    started = clock()
    screen = read()
    left = False
    if screen["simulator"]:
        service._press_simulator_button("cancel", PRESS_MS)
        screen = _wait(read, lambda now: not now["simulator"], LEAVE_WAIT_SECONDS, sleep, clock)
        if screen["simulator"]:
            raise MissionError("combat_simulator_not_left")
        left = True
    closed = False
    if close_menu:
        if not screen["menu"]:
            screen = _wait(read, lambda now: now["menu"], LEAVE_WAIT_SECONDS if left else 0.0, sleep, clock)
        if screen["menu"]:
            service._press_simulator_button("cancel", PRESS_MS)
            screen = _wait(read, lambda now: not now["menu"], LEAVE_WAIT_SECONDS, sleep, clock)
            if screen["menu"]:
                raise MissionError("chadley_menu_not_closed")
            closed = True
    return {"ok": True, "left_simulator": left, "closed_menu": closed, "seconds": round(clock() - started, 2)}
