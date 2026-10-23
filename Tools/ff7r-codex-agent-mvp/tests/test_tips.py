import json
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ff7r_agent.input import SimulationInput
from ff7r_agent.service import AgentService, SafetyError
from ff7r_agent.tips import (
    TIPS_BACKOFF_SECONDS, TIPS_CLOSE_MS, TIPS_OPEN_SETTLE_SECONDS, TIPS_PAUSE_DEBOUNCE_SECONDS,
    TIPS_PRESSES_PER_TIP, TIPS_RECHECK_SECONDS,
    read_game_flags, read_tips_panel, tips_shown,
)

BASE = "/Engine/Transient.GameEngine_2147482600:BP_EndGameInstance."
CLOSE_HINT = "<button=pause-menu>/<button=cancel>\xa0Close"
MAIN_MENU = ("$tutorial_Fld_01_0070", "$tutorial_Fld_01_0070_00")
MOVEMENT = ("$tutorial_Fld_01_0020", "$tutorial_Fld_01_0020_00")
INTRO = {"kind": "cinema", "player_class": "EndCinemaSequencePlayer", "sequence": "EV_COMON_2112", "status": 1}


def tip_texts(key, body, buttons=CLOSE_HINT):
    """The tips widget's texts as read live 2026-10-13 (read_screen_texts)."""
    return [(BASE + "BattleTips.WidgetTree.Txt_Btn2", "$menu_field_tips_scale_up"),
            (BASE + "BattleTips.WidgetTree.Txt_Title", key),
            (BASE + "BattleTips.WidgetTree.Txt_Btn", buttons),
            (BASE + "BattleTips.WidgetTree.Txt_Main", body)]


# Listed before, while and after a tip: a loaded but hidden full-screen tutorial.
HIDDEN_TUTORIAL = [(BASE + "BattleTutorial.WidgetTree.Txt_Title", "$menu_tutorial_ui_0000_0001"),
                   (BASE + "Command.WidgetTree.Command_Base.WidgetTree.Txt_Command", "$menu_commandmenu_0003_0000")]


class Game:
    """A fake game that is both the telemetry and the screen: a paused field
    with queued tips, closed one at a time by Cancel."""

    def __init__(self, tmp_path, tips=(MAIN_MENU,)):
        self.path = tmp_path / "state.json"
        self.tips = list(tips)
        self.buttons = CLOSE_HINT
        self.title_override = None
        self.paused = bool(tips)
        self.in_menu = False
        self.in_combat = False
        self.loading = False
        self.interaction = None
        self.queen_blood = None
        self.ignore_cancel = False
        self.ignore_presses = 0  # the next N presses land while the tip is still opening
        self.refuse = None
        self.pressed = []
        self.pressed_at = []
        self.clock = lambda: 0.0
        self.snapshots = 0
        self.observed = 0
        self.write_state()

    def write_state(self):
        game = {"running": True, "paused": self.paused, "in_menu": self.in_menu, "in_combat": self.in_combat}
        self.path.write_text(json.dumps({"game": game}), encoding="utf-8")

    def observe(self):
        self.observed += 1
        return SimpleNamespace(
            running=True, paused=self.paused, in_menu=self.in_menu, in_combat=self.in_combat, target=None,
            loading=self.loading, interaction=self.interaction, queen_blood=self.queen_blood,
            age_seconds=lambda: 0.0)

    def texts(self):
        self.snapshots += 1
        shown = list(HIDDEN_TUTORIAL)
        if self.tips:
            key, body = self.tips[0]
            shown += tip_texts(self.title_override or key, body, self.buttons)
        return shown

    def tap(self, button, milliseconds):
        if self.refuse is not None:
            raise SafetyError(self.refuse)
        self.pressed.append((button, milliseconds))
        self.pressed_at.append(self.clock())
        if self.ignore_presses:
            self.ignore_presses -= 1
            return {"ok": True, "button": button}
        if button == "cancel" and self.tips and not self.ignore_cancel:
            self.tips.pop(0)
            if not self.tips:
                self.paused = False
            self.write_state()
        return {"ok": True, "button": button}


class Harness:
    def __init__(self, tmp_path, tips=(MAIN_MENU,), mode="always", **service_options):
        self.game = Game(tmp_path, tips)
        self.clock = 100.0
        self.game.clock = lambda: self.clock
        self.on_sleep = None
        self.service = AgentService(self.game, SimulationInput(), sleeper=self.sleep,
                                    monotonic=lambda: self.clock, **service_options)
        self.service.tap = self.game.tap
        self.service._deck_screen_texts = self.game.texts
        self.service._tips.mode = mode  # no thread: the tests step the watcher
        self.tips = self.service._tips

    def sleep(self, seconds):
        self.clock += seconds
        if self.on_sleep is not None:
            self.on_sleep()

    def tick(self, advance=0.0):
        self.clock += advance
        self.game.write_state()  # a fresh modification time, as the exporter keeps it
        return self.tips.watch_once()

    def paused_long_enough(self):
        self.tick()
        return self.tick(TIPS_PAUSE_DEBOUNCE_SECONDS + 0.1)


def test_the_tips_panel_is_read_from_the_live_texts_and_the_hidden_tutorial_is_not_one():
    live = HIDDEN_TUTORIAL + tip_texts(*MAIN_MENU)
    panel = read_tips_panel(live)
    assert (panel.key, panel.body, panel.cancel_closes) == (MAIN_MENU[0], MAIN_MENU[1], True)
    assert tips_shown(live) is True
    # Live 2026-10-13: BattleTutorial.Txt_Title was listed with no tip open.
    assert read_tips_panel(HIDDEN_TUTORIAL) is None and tips_shown(HIDDEN_TUTORIAL) is False
    assert read_tips_panel(tip_texts("Enfeebling Abilities", "text")) is None  # a Queen's Blood tip
    assert read_tips_panel(tip_texts(*MAIN_MENU, buttons="<button=confirm> Next")).cancel_closes is False
    assert read_tips_panel([text for text in tip_texts(*MAIN_MENU) if not text[0].endswith("Txt_Btn")]
                           ).cancel_closes is False


def test_game_flags_come_from_the_state_file_alone(tmp_path):
    path = tmp_path / "state.json"
    assert read_game_flags(path) == {"observed": False}
    path.write_text(json.dumps({"game": {"running": True, "paused": True}}), encoding="utf-8")
    flags = read_game_flags(path)
    assert flags == {"observed": True, "running": True, "paused": True, "in_menu": False, "in_combat": False}
    modified = path.stat().st_mtime
    assert read_game_flags(path, now=modified + 5.0) == {"observed": False}  # stale: nothing is known
    path.write_text("{", encoding="utf-8")
    assert read_game_flags(path) == {"observed": False}


def test_a_single_tip_is_closed_with_one_cancel(tmp_path):
    world = Harness(tmp_path)
    assert world.tick() is None and world.game.snapshots == 0  # the pause has only just begun
    result = world.tick(TIPS_PAUSE_DEBOUNCE_SECONDS + 0.1)
    assert result["ok"] is True and result["closed"] == [MAIN_MENU[0]] and result["presses"] == 1
    assert world.game.pressed == [("cancel", TIPS_CLOSE_MS)]
    status = world.service.tips_autoclose_status()
    assert status["closed_total"] == 1 and status["recent"][-1]["closed"] == [MAIN_MENU[0]]
    assert status["recent"][-1]["source"] == "watch" and status["recent"][-1]["at"]
    # The pause ended with the tip: nothing more is read or pressed.
    snapshots = world.game.snapshots
    for _ in range(20):
        assert world.tick(0.25) is None
    assert world.game.snapshots == snapshots and len(world.game.pressed) == 1


def test_queued_tips_are_closed_in_order_one_cancel_each(tmp_path):
    # Live 2026-10-13: "Main Menu" ($tutorial_Fld_01_0070), then "Movement
    # Options" ($tutorial_Fld_01_0020) behind it.
    world = Harness(tmp_path, tips=(MAIN_MENU, MOVEMENT))
    result = world.paused_long_enough()
    assert result["ok"] is True and result["closed"] == [MAIN_MENU[0], MOVEMENT[0]]
    assert [button for button, _ in world.game.pressed] == ["cancel", "cancel"]
    assert world.tips.closed_total == 2 and world.game.paused is False
    recorded = world.service.tips_autoclose_status()["recent"]
    assert len(recorded) == 1 and recorded[0]["closed"] == [MAIN_MENU[0], MOVEMENT[0]]


def test_the_hidden_tutorial_widget_alone_never_triggers_a_close(tmp_path):
    world = Harness(tmp_path, tips=())
    world.game.paused = True  # paused with no tip: a shop, a map, a prompt
    world.game.write_state()
    assert world.paused_long_enough() is None
    assert world.game.snapshots == 1 and world.game.pressed == []
    assert world.tips.status()["last_look"]["outcome"] == "no_tips"
    # One more look in case the panel's text is late, then the pause is left alone.
    assert world.tick(TIPS_RECHECK_SECONDS + 0.1) is None
    assert world.game.snapshots == 2
    for _ in range(40):
        world.tick(0.5)
    assert world.game.snapshots == 2 and world.game.pressed == []
    assert world.service.tips_autoclose_status()["recent"] == []
    # A new pause is looked at afresh.
    world.game.paused = False
    world.tick(1.0)
    world.game.paused = True
    world.paused_long_enough()
    assert world.game.snapshots == 3


def test_a_late_panel_is_closed_on_the_second_look(tmp_path):
    world = Harness(tmp_path, tips=())
    world.game.paused = True
    assert world.paused_long_enough() is None and world.game.snapshots == 1
    world.game.tips = [MAIN_MENU]
    result = world.tick(TIPS_RECHECK_SECONDS + 0.1)
    assert result["ok"] is True and result["closed"] == [MAIN_MENU[0]]


def test_no_snapshot_is_taken_unless_the_game_stays_paused_outside_a_menu(tmp_path):
    world = Harness(tmp_path, tips=(MAIN_MENU,))
    world.game.paused = False  # a tip is queued but the game is running: nothing to look for
    for _ in range(40):
        assert world.tick(0.25) is None
    assert world.game.snapshots == 0 and world.game.observed == 0
    world.game.paused = True
    world.game.in_menu = True  # the Main Menu pauses too
    for _ in range(40):
        assert world.tick(0.25) is None
    assert world.game.snapshots == 0 and world.game.observed == 0
    world.game.in_menu = False
    # A pause that ends within the debounce costs nothing either.
    assert world.tick() is None
    world.game.paused = False
    assert world.tick(TIPS_PAUSE_DEBOUNCE_SECONDS - 0.1) is None
    assert world.game.snapshots == 0 and world.game.observed == 0 and world.game.pressed == []
    # An unreadable or stale state file says nothing is known.
    world.game.paused = True
    world.game.write_state()
    os.utime(world.game.path, (time.time() - 60, time.time() - 60))
    world.clock += 1.0
    assert world.tips.watch_once() is None and world.tips.watch_once() is None
    assert world.game.snapshots == 0


@pytest.mark.parametrize("blocker", ["dialogue", "queen_blood", "match", "loading", "cutscene", "stale"])
def test_flows_that_own_the_screen_are_not_looked_into(tmp_path, blocker):
    world = Harness(tmp_path)
    if blocker == "dialogue":
        world.game.interaction = {"state": "dialogue"}
    elif blocker == "queen_blood":
        world.game.queen_blood = {"active": True, "phase": "player_turn"}
    elif blocker == "match":
        world.service._queen_blood_match = {"opponent": "x"}
    elif blocker == "loading":
        world.game.loading = True
    elif blocker == "cutscene":
        payload = {"schema_version": 4, "sequence": 1, "playing": [INTRO]}
        (tmp_path / "cutscene.state").write_text(json.dumps(payload), encoding="utf-8")
    else:
        world.game.observe = lambda: SimpleNamespace(
            running=True, paused=True, in_menu=False, in_combat=False, target=None, age_seconds=lambda: 9.0)
    assert world.paused_long_enough() is None
    assert world.game.snapshots == 0 and world.game.pressed == []
    assert world.tips.status()["last_look"]["outcome"].startswith("blocked: ")
    # Bounded: the pause is looked at twice, then left alone.
    world.game.observed = 0
    world.tick(TIPS_RECHECK_SECONDS + 0.1)
    for _ in range(20):
        world.tick(1.0)
    assert world.game.snapshots == 0 and world.game.observed <= 1


def test_the_watcher_leaves_the_controls_to_a_running_action(tmp_path):
    world = Harness(tmp_path)
    world.tick()
    world.clock += TIPS_PAUSE_DEBOUNCE_SECONDS + 0.1
    with world.service._action_lock:
        assert world.tick() is None and world.tick(1.0) is None
        assert world.service.close_tips()["reason"] == "busy"
    assert world.game.snapshots == 0 and world.game.observed == 0 and world.game.pressed == []
    # The look was not used up: the next free step closes the tip.
    assert world.tick(1.0)["closed"] == [MAIN_MENU[0]]


def test_a_refused_close_is_recorded_once_and_backed_off(tmp_path):
    world = Harness(tmp_path)
    world.game.refuse = "emergency stop is latched: no control session is open; open_control_session first"
    result = world.paused_long_enough()
    assert result["ok"] is False and result["reason"] == "input_refused" and result["input_sent"] is False
    # A look for the tip and one more after it has settled.
    assert world.game.pressed == [] and world.game.snapshots == 2
    for _ in range(14):  # nothing is tried again for 15 s
        assert world.tick(1.0) is None
    assert world.game.snapshots == 2
    assert world.tick(1.5)["reason"] == "input_refused" and world.game.snapshots == 4
    for _ in range(29):  # the wait doubles: 30 s
        assert world.tick(1.0) is None
    assert world.tick(1.5)["reason"] == "input_refused" and world.game.snapshots == 6
    recent = world.service.tips_autoclose_status()["recent"]
    assert len(recent) == 1 and recent[0]["reason"] == "input_refused" and recent[0]["repeats"] == 3
    assert world.tips.status()["backoff_seconds"] == 60.0
    # Once input is allowed again the tip is closed.
    world.game.refuse = None
    assert world.tick(61.0)["closed"] == [MAIN_MENU[0]]
    assert world.game.pressed == [("cancel", TIPS_CLOSE_MS)] and world.game.tips == []


def test_a_latched_stop_is_found_before_any_snapshot_is_spent(tmp_path):
    world = Harness(tmp_path)
    world.service.input = SimpleNamespace(is_real=True, release_all=lambda: None)
    world.service._arm = SimpleNamespace(snapshot=lambda: (False, 0.0, True))
    result = world.paused_long_enough()
    assert result["reason"] == "input_refused" and "latched" in result["detail"]
    assert world.game.snapshots == 0 and world.game.pressed == []
    world.service._arm = SimpleNamespace(snapshot=lambda: (False, 0.0, False))
    world.tick(60.0)
    assert world.tips.status()["recent"][-1]["detail"] == "real input is not manually armed"
    assert world.game.snapshots == 0


def test_a_tip_that_does_not_change_after_cancel_stops_without_repeating(tmp_path):
    world = Harness(tmp_path)
    world.game.ignore_cancel = True
    result = world.paused_long_enough()
    assert result["ok"] is False and result["reason"] == "tip_not_closed" and result["closed"] == []
    # One press and one more, then it stopped.
    assert world.game.pressed == [("cancel", TIPS_CLOSE_MS)] * TIPS_PRESSES_PER_TIP
    for _ in range(int(TIPS_BACKOFF_SECONDS) - 2):  # the backoff holds, and nothing is pressed blind
        assert world.tick(1.0) is None
    assert len(world.game.pressed) == TIPS_PRESSES_PER_TIP
    # After the backoff it is confirmed afresh, tried again, and given up on after three.
    for _ in range(5):
        world.tick(TIPS_BACKOFF_SECONDS + 5.0)
    assert len(world.game.pressed) == 3 * TIPS_PRESSES_PER_TIP
    recent = world.service.tips_autoclose_status()["recent"]
    assert len(recent) == 1 and recent[0]["reason"] == "tip_not_closed" and recent[0]["repeats"] == 3
    assert world.tips.closed_total == 0


def test_each_tip_is_left_to_open_before_its_cancel(tmp_path):
    # Live 2026-10-13: a Cancel half a second into the pause, and one as the
    # next tip replaced the closed one, did nothing.
    world = Harness(tmp_path, tips=(MAIN_MENU, MOVEMENT))
    world.tick()
    paused_at = world.clock
    result = world.tick(TIPS_PAUSE_DEBOUNCE_SECONDS + 0.1)
    assert result["ok"] is True and result["closed"] == [MAIN_MENU[0], MOVEMENT[0]] and result["presses"] == 2
    first, second = world.game.pressed_at
    assert first - paused_at >= TIPS_PAUSE_DEBOUNCE_SECONDS + TIPS_OPEN_SETTLE_SECONDS
    assert second - first >= TIPS_OPEN_SETTLE_SECONDS


def test_a_cancel_the_opening_tip_ignored_is_pressed_once_more(tmp_path):
    world = Harness(tmp_path, tips=(MAIN_MENU, MOVEMENT))
    world.game.ignore_presses = 1
    result = world.paused_long_enough()
    assert result["ok"] is True and result["closed"] == [MAIN_MENU[0], MOVEMENT[0]] and result["presses"] == 3
    assert world.tips.closed_total == 2 and world.game.paused is False


def test_a_tip_closed_while_it_settles_is_not_pressed(tmp_path):
    # Cancel in the field opens the pause menu: a tip the player closed
    # first is never pressed after.
    world = Harness(tmp_path)

    def player_closes():
        world.game.tips = []
        world.game.paused = False

    world.on_sleep = player_closes
    result = world.paused_long_enough()
    assert result is None and world.game.pressed == []
    assert world.tips.status()["last_look"]["outcome"] == "no_tips"


def test_a_tip_that_does_not_name_cancel_is_not_pressed(tmp_path):
    world = Harness(tmp_path)
    world.game.buttons = "<button=confirm>\xa0Next"
    result = world.paused_long_enough()
    assert result["reason"] == "no_cancel_hint" and world.game.pressed == []
    snapshots = world.game.snapshots
    for _ in range(20):
        world.tick(1.0)
    assert world.game.snapshots == snapshots  # the paged variant is left alone for the whole pause


def test_a_panel_with_another_title_is_left_to_its_own_flow(tmp_path):
    world = Harness(tmp_path)
    world.game.title_override = "Enfeebling Abilities"  # a Queen's Blood tip
    result = world.paused_long_enough()
    assert result["reason"] == "tips_unrecognised" and world.game.pressed == []
    snapshots = world.game.snapshots
    for _ in range(20):
        world.tick(1.0)
    assert world.game.snapshots == snapshots


def test_close_tips_on_demand_and_its_limits(tmp_path):
    world = Harness(tmp_path, tips=(MAIN_MENU, MOVEMENT), mode="off")
    with pytest.raises(SafetyError):
        world.service.close_tips(0)
    with pytest.raises(SafetyError):
        world.service.close_tips(9)
    with pytest.raises(SafetyError):
        world.service.close_tips("all")
    result = world.service.close_tips(max_tips=1)
    assert result["ok"] is False and result["reason"] == "tip_limit" and result["closed"] == [MAIN_MENU[0]]
    assert world.game.pressed == [("cancel", TIPS_CLOSE_MS)]
    result = world.service.close_tips()
    assert result["ok"] is True and result["closed"] == [MOVEMENT[0]]
    result = world.service.close_tips()
    assert result["ok"] is False and result["reason"] == "no_tips" and result["input_sent"] is False
    assert len(world.game.pressed) == 2
    assert [entry["source"] for entry in world.service.tips_autoclose_status()["recent"]] == [
        "manual", "manual", "manual"]


def test_the_mode_decides_when_the_watcher_acts(tmp_path):
    with pytest.raises(SafetyError):
        Harness(tmp_path).service.set_tips_autoclose("sometimes")
    world = Harness(tmp_path, mode="off")
    assert world.paused_long_enough() is None and world.game.snapshots == 0
    assert world.service.tips_autoclose_status()["mode"] == "off"
    world.tips.mode = "stream"
    assert world.tips.status()["applies_now"] is False  # no stream event directory: never live
    assert world.tick(10.0) is None and world.game.snapshots == 0
    # Live while OBS streams; a rehearsal counts too.
    events = tmp_path / "events"
    world = Harness(tmp_path, mode="stream", public_stream_event_dir=events)
    assert world.tips.status()["applies_now"] is False
    beat = {"version": 1, "streamStartedAt": datetime.now(timezone.utc).isoformat(),
            "at": datetime.now(timezone.utc).isoformat()}
    (tmp_path / "public-stream-live.json").write_text(json.dumps(beat), encoding="utf-8")
    assert world.tips.status()["applies_now"] is True
    assert world.paused_long_enough()["closed"] == [MAIN_MENU[0]]


def test_the_watcher_thread_starts_with_a_mode_and_stops(tmp_path):
    world = Harness(tmp_path, tips=(), mode="off")
    world.game.paused = False
    world.game.write_state()
    assert world.service.set_tips_autoclose("off")["watching"] is False
    status = world.service.set_tips_autoclose("always")
    assert status["mode"] == "always" and status["watching"] is True
    world.tips.stop()
    world.tips._thread.join(timeout=2.0)
    assert not world.tips._thread.is_alive()


def test_the_watcher_loop_backs_off_on_repeated_errors_instead_of_spinning(tmp_path):
    world = Harness(tmp_path)
    delays = []

    class Stopper:
        def wait(self, delay):
            delays.append(delay)
            return len(delays) > 6

    def broken():
        raise OSError("state file is locked")

    world.tips._stop = Stopper()
    world.tips.watch_once = broken
    world.tips._run()
    assert delays == [0.25, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    recent = world.service.tips_autoclose_status()["recent"]
    assert len(recent) == 1 and recent[0]["reason"] == "watch_error" and recent[0]["repeats"] == 6


def test_the_operations_are_registered_with_the_runtime():
    from ff7r_agent.runtime_commands import SERVICE_ALIASES

    assert SERVICE_ALIASES["close_tips"] == "close_tips"
    assert SERVICE_ALIASES["set_tips_autoclose"] == "set_tips_autoclose"
    assert SERVICE_ALIASES["get_tips_autoclose_status"] == "tips_autoclose_status"


def test_a_battle_tutorial_in_the_tips_panel_is_a_tip():
    # Live 2026-10-20, Cloud's first fight on Mt. Nibel ("Fighting as Cloud").
    root = "/Engine/Transient.GameEngine_2147482600:BP_EndGameInstance.BattleTips.WidgetTree."
    panel = read_tips_panel([
        (root + "Txt_Title", "$menu_Tutorial_End2Btl_tuto0010_01"),
        (root + "Txt_Main", "$menu_Tutorial_End2Btl_tuto0010_00"),
        (root + "Txt_Btn", "<button=pause-menu>\ufffdPause  <button=cancel>\ufffdClose"),
    ])
    assert panel is not None and panel.key == "$menu_Tutorial_End2Btl_tuto0010_01" and panel.cancel_closes


BATTLE_TIPS = (("$menu_Tutorial_End2Btl_tuto0010_01", "$menu_Tutorial_End2Btl_tuto0010_00"),
               ("$menu_Tutorial_End2Btl_tuto0150_01", "$menu_Tutorial_End2Btl_tuto0150_00"))


def battle_harness(tmp_path):
    """Live 2026-10-20: Escape closes a battle tip and opens the battle pause
    menu (Resume selected); Confirm resumes and the next tip shows."""
    harness = Harness(tmp_path, tips=BATTLE_TIPS)
    game = harness.game
    game.paused = False  # battle tips show over a running battle
    game.pause_menu = False
    texts, tap = game.texts, game.tap

    def battle_texts():
        shown = [] if game.pause_menu else texts()
        if game.pause_menu:
            shown.append((BASE + "Pause_00.WidgetTree.Txt_Pause", "$menu_pause_0000_0000"))
        return shown

    def battle_tap(button, milliseconds):
        if button == "confirm" and game.pause_menu:
            game.pressed.append((button, milliseconds))
            game.pause_menu = False
            return {"ok": True}
        result = tap(button, milliseconds)
        if button == "cancel":
            game.pause_menu = True
        return result

    game.texts = battle_texts
    game.tap = battle_tap
    harness.service.tap = battle_tap
    harness.service._tap_battle_tip_button = battle_tap
    harness.service._deck_screen_texts = battle_texts
    return harness


def test_closing_battle_tips_resumes_the_pause_menu_each_one_opens(tmp_path):
    harness = battle_harness(tmp_path)
    result = harness.service.close_tips()
    assert result["ok"] and result["closed"] == [key for key, _ in BATTLE_TIPS]
    assert [button for button, _ in harness.game.pressed] == ["cancel", "confirm", "cancel", "confirm"]
    assert harness.game.pause_menu is False and not harness.game.tips


def test_a_battle_tip_that_pauses_the_fight_is_closed_by_the_watcher(tmp_path):
    # Live 2026-10-20, Mt. Nibel: "Targeting" paused the battle outside any
    # menu and stayed until close_tips was called.
    harness = battle_harness(tmp_path)
    harness.game.paused = True
    harness.game.in_combat = True
    result = harness.paused_long_enough()
    assert result["ok"] and result["closed"] == [key for key, _ in BATTLE_TIPS]
    assert [button for button, _ in harness.game.pressed] == ["cancel", "confirm", "cancel", "confirm"]


def test_a_field_tip_is_left_alone_in_combat(tmp_path):
    world = Harness(tmp_path, tips=(MAIN_MENU,))
    world.game.in_combat = True
    result = world.paused_long_enough()
    assert result["reason"] == "tips_unrecognised" and result["input_sent"] is False
    assert world.game.pressed == []
    for _ in range(10):
        assert world.tick(1.0) is None  # not looked at again in this pause
