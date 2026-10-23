import json
import os
import time

from ff7r_agent.cutscene import read_cutscene_state

SIGN = {"kind": "level", "player_class": "LevelSequencePlayer", "sequence": "LS_Sign", "status": 1}
INTRO = {"kind": "cinema", "player_class": "EndCinemaSequencePlayer", "sequence": "EV_COMON_2112", "status": 1}


def write(path, playing, age=0.0, now=1000.0, **fields):
    payload = {"schema_version": 4, "sequence": 7, "leader": True, "tracked": 3, "anim_cutscene": False,
               "view_target": "CameraActor", "playing": playing, **fields}
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(path, (now - age, now - age))


def test_a_playing_cinema_is_a_cutscene_and_ambient_sequences_are_not(tmp_path):
    path = tmp_path / "cutscene.state"
    write(path, [SIGN])
    state = read_cutscene_state(path, now=1000.0)
    assert state["observed"] is True and state["active"] is False and state["playing"] == [SIGN]

    # Live 2026-10-05: the combat simulator's intro, with Cloud held by the event.
    write(path, [INTRO, SIGN], leader=False, anim_cutscene=None, view_target="")
    state = read_cutscene_state(path, now=1000.0)
    assert state["active"] is True and state["paused"] is False and state["scenes"] == [INTRO]
    assert state["leader"] is False

    write(path, [{**INTRO, "status": 6}])
    assert read_cutscene_state(path, now=1000.0)["paused"] is True


def test_a_stale_missing_or_old_state_says_nothing_is_known(tmp_path):
    path = tmp_path / "cutscene.state"
    missing = read_cutscene_state(path, now=1000.0)
    assert missing["observed"] is False and missing["active"] is None
    write(path, [], age=5.0)
    stale = read_cutscene_state(path, now=1000.0)
    assert stale == {"observed": False, "active": None, "reason": "cutscene_state_stale", "age_seconds": 5.0}
    path.write_text(json.dumps({"schema_version": 3, "anim_cutscene": False}), encoding="utf-8")
    assert read_cutscene_state(path)["reason"] == "cutscene_state_schema_unsupported"


class _Telemetry:
    def __init__(self, path):
        self.path = path
        self.paused = False

        self.in_menu = False

    def observe(self):
        return type("State", (), {"paused": self.paused, "in_menu": self.in_menu})()


def _skipper(tmp_path, pauses: bool, stops: bool):
    from ff7r_agent.service import AgentService
    from ff7r_agent.input import SimulationInput

    telemetry = _Telemetry(tmp_path / "state.json")
    state = tmp_path / "cutscene.state"
    write(state, [INTRO], now=time.time(), leader=False)
    service = AgentService(telemetry, SimulationInput(), sleeper=lambda _seconds: None)
    pressed = []
    dialog = {"open": False}
    # The Paused dialog is recognised by its texts (Pause_00 title, Com_Btn Skip).
    service._cinema_pause_dialog_open = lambda full=False: dialog["open"]

    def tap(button, milliseconds):
        pressed.append(button)
        if button == "menu" and pauses:
            dialog["open"] = True
        if button == "confirm" and stops:
            write(state, [], now=time.time())
        return {"ok": True, "button": button}

    service.tap = tap
    return service, pressed


def test_a_skippable_scene_is_skipped_through_the_paused_dialog(tmp_path):
    service, pressed = _skipper(tmp_path, pauses=True, stops=True)
    result = service.skip_cutscene()
    assert result["ok"] is True and result["skipped"] == ["EV_COMON_2112"]
    assert pressed == ["menu", "down", "confirm"]


def test_a_scene_that_does_not_pause_is_left_alone_after_menu(tmp_path):
    # Live: the simulator's VR transition ignores Menu; Down and Confirm would
    # then land on whatever is behind it.
    service, pressed = _skipper(tmp_path, pauses=False, stops=False)
    result = service.skip_cutscene(timeout_seconds=1.0)
    assert result["ok"] is False and result["reason"] == "not_skippable"
    assert pressed == ["menu"]


def test_a_scene_that_ends_while_waiting_for_the_dialog_is_not_called_unskippable(tmp_path):
    # Live: a refused Menu waited the whole 3 s after the VR transition ended.
    service, pressed = _skipper(tmp_path, pauses=False, stops=False)
    write(tmp_path / "cutscene.state", [], now=time.time())
    result = service._skip_cutscene_locked(tmp_path / "cutscene.state", ["EV_COMON_2112"], 1.0)
    assert result["reason"] == "scene_ended" and pressed == ["menu"]


def test_a_skip_that_does_not_stop_the_scene_is_reported(tmp_path):
    service, pressed = _skipper(tmp_path, pauses=True, stops=False)
    result = service.skip_cutscene(timeout_seconds=1.0)
    assert result["ok"] is False and result["reason"] == "skip_not_confirmed"


def test_nothing_is_pressed_without_a_cutscene(tmp_path):
    service, pressed = _skipper(tmp_path, pauses=True, stops=True)
    write(tmp_path / "cutscene.state", [SIGN], now=time.time())
    assert service.skip_cutscene()["reason"] == "no_cutscene" and pressed == []


def test_a_scene_ending_as_menu_lands_closes_the_main_menu_instead_of_confirming(tmp_path):
    # Menu after the scene ended opens the main menu, which also pauses; Down
    # and Confirm would pick one of its items.
    service, pressed = _skipper(tmp_path, pauses=False, stops=True)
    service.telemetry.in_menu = True
    result = service.skip_cutscene(timeout_seconds=1.0)
    assert result["reason"] == "scene_ended" and pressed == ["menu", "cancel"]


def test_routes_see_a_playing_cinema_and_never_block_on_an_unknown_state():
    # #158: in_cutscene was hard-coded False, so cutscene_active never fired.
    from types import SimpleNamespace
    from ff7r_agent.live_route_adapter import AgentServiceTelemetrySource

    def runtime(state):
        adapter = object.__new__(AgentServiceTelemetrySource)
        adapter.service = SimpleNamespace(cutscene_state=lambda: state)
        return adapter

    assert runtime({"observed": True, "active": True})._cutscene_playing() is True
    assert runtime({"observed": True, "active": False})._cutscene_playing() is False
    assert runtime({"observed": False, "active": None})._cutscene_playing() is False
    adapter = object.__new__(AgentServiceTelemetrySource)
    adapter.service = SimpleNamespace()  # an older service without cutscene detection
    assert adapter._cutscene_playing() is False


def test_the_watcher_skips_only_when_its_mode_applies(tmp_path):
    service, pressed = _skipper(tmp_path, pauses=True, stops=True)
    playing = {"active": True, "scenes": [INTRO]}
    assert service._cutscene_watch_once(playing) is None  # off by default in a plain service
    service._cutscene_autoskip = "stream"
    assert service._cutscene_watch_once(playing) is None  # no stream event directory, so never live
    service._cutscene_autoskip = "always"
    assert service._cutscene_watch_once(playing)["ok"] is True
    assert pressed == ["menu", "down", "confirm"]
    assert service.cutscene_autoskip_status()["recent"][-1]["skipped"] == ["EV_COMON_2112"]


def test_the_watcher_waits_out_an_unskippable_scene_instead_of_mashing(tmp_path):
    # One more try a moment later, in case Skip comes only after the
    # opening; after a second refusal the scene is waited out.
    service, pressed = _skipper(tmp_path, pauses=False, stops=False)
    clock = {"now": 100.0}
    service._monotonic = lambda: clock["now"]
    service._sleeper = lambda seconds: clock.update(now=clock["now"] + seconds)
    service._cutscene_autoskip = "always"
    playing = {"active": True, "scenes": [INTRO]}
    assert service._cutscene_watch_once(playing)["reason"] == "not_skippable"
    assert service._cutscene_watch_once(playing) is None and pressed == ["menu"]
    assert service.cutscene_autoskip_status()["refused_once"] == ["EV_COMON_2112"]
    clock["now"] += 2.5
    assert service._cutscene_watch_once(playing)["reason"] == "not_skippable"
    clock["now"] += 10.0
    assert service._cutscene_watch_once(playing) is None and pressed == ["menu", "menu"]
    status = service.cutscene_autoskip_status()
    assert status["unskippable"] == ["EV_COMON_2112"] and status["refused_once"] == []


def test_the_watcher_leaves_the_controls_to_a_running_action(tmp_path):
    service, pressed = _skipper(tmp_path, pauses=True, stops=True)
    service._cutscene_autoskip = "always"
    with service._action_lock:
        assert service._cutscene_watch_once({"active": True, "scenes": [INTRO]}) is None
    assert pressed == []


def test_a_skip_is_a_public_event_and_modes_are_checked():
    import pytest
    from ff7r_agent.public_stream_events import public_event_for_action
    from ff7r_agent.service import AgentService, SafetyError
    from ff7r_agent.input import SimulationInput
    from ff7r_agent.telemetry import SimulationTelemetry

    assert public_event_for_action({"ok": True, "action": "skip_cutscene"}) == "cutscene_skipped"
    assert public_event_for_action({"ok": False, "action": "skip_cutscene"}) is None
    with pytest.raises(SafetyError):
        AgentService(SimulationTelemetry(), SimulationInput()).set_cutscene_autoskip("sometimes")


def test_the_paused_dialog_is_recognised_by_its_title_and_skip_button(tmp_path, monkeypatch):
    from ff7r_agent import map_query
    from ff7r_agent.service import AgentService
    from ff7r_agent.input import SimulationInput

    service = AgentService(_Telemetry(tmp_path / "state.json"), SimulationInput())
    base = "/Engine/Transient.GameEngine_1:BP_EndGameInstance."
    screens = {
        "paused": [{"path": base + "Pause_00.WidgetTree.Txt_Pause", "text": "$menu_pause_0000_0000"},
                   {"path": base + "Com_Btn_00.WidgetTree.Txt", "text": "$menu_pause_0002_0000"},
                   {"path": base + "Com_Btn_00.WidgetTree.Txt", "text": "$menu_pause_0002_0001"}],
        # Live: the cinema's skip hint uses other keys and is not the dialog.
        "hint_only": [{"path": base + "Cinema_Skip_Btn.WidgetTree.Txt_Skip", "text": "$menu_Cinema_0000_0000"}],
    }
    shown = {"now": "paused"}
    reads = []

    def snapshot(*args, flags=(), **kwargs):
        reads.append(tuple(flags))
        return {"texts": screens[shown["now"]], "texts_scope": "menu" if "menutexts" in flags else None}, {}

    monkeypatch.setattr(map_query, "request_snapshot", snapshot)
    assert service._cinema_pause_dialog_open() is True
    shown["now"] = "hint_only"
    assert service._cinema_pause_dialog_open() is False
    assert service._cinema_pause_dialog_open(full=True) is False
    # The quick menu-text read (0.2-0.4 s) first; every text only when asked.
    assert reads == [("menutexts",), ("menutexts",), ("alltexts",)]
