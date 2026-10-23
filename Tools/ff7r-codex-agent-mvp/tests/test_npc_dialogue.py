"""Fresh rendered greeting evidence, and a Talk-scoped checker lifetime."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ff7r_agent import npc_dialogue as dialogue


def scene():
    nodes = [{"class": kind, "path": "/Temp/Subtitle.WidgetTree." + name,
              "rendered": True, "text": text}
             for name, kind, text in [("Img_Arrow", "EndImage", ""),
                                      ("Txt_Lines", "EndTextBlock", "Ready to play?"),
                                      ("Txt_Name", "EndTextBlock", "Mr. Cuddlesworth")]]
    nodes.append({"class": "Subtitle00_C", "path": "/Temp/Subtitle", "depth": 0, "rendered": True})
    roots = {"observed": True, "configured_complete": True, "widgets": [
        {"class": "Subtitle00_C", "path": "/Temp/Subtitle", "configured_root": True,
         "details": {"in_viewport_ex": True, "truncated": False, "nodes": nodes}},
        {"class": "TalkMenu_00_C", "configured_root": True,
         "details": {"in_viewport_ex": False}},
    ]}
    base = {"running": True, "telemetry": {"live": True}, "paused": False,
            "in_menu": False, "in_combat": False, "loading": False,
            "map_id": None, "target": None}
    cinema = {"observed": True, "active": False, "anim_cutscene": False,
              "playing": [{"kind": "dialogue"}]}
    return roots, base, cinema


def test_current_puppet_speaker_and_rendered_arrow_authorize_only_ordinary_line():
    value = dialogue.signal(*scene())
    assert value["fast_forwardable"] and value["speaker"] == "Mr. Cuddlesworth"


@pytest.mark.parametrize("fault", ["detached", "wheel", "unknown_overlay", "hidden_arrow",
                                   "hidden_line", "blank_speaker", "ambiguous", "partial_scan",
                                   "truncated_tree", "field", "paused", "combat", "cinema",
                                   "unknown_cinema", "malformed_root", "hidden_owner", "unknown_owner"])
def test_no_input_permission_from_stale_or_ambiguous_ui(fault):
    roots, base, cinema = scene()
    details = roots["widgets"][0]["details"]
    if fault == "detached": details["in_viewport_ex"] = False
    if fault == "wheel": roots["widgets"][1]["details"]["in_viewport_ex"] = True
    if fault == "unknown_overlay": roots["widgets"][1]["details"]["in_viewport_ex"] = None
    if fault == "hidden_arrow": details["nodes"][0]["rendered"] = False
    if fault == "hidden_line": details["nodes"][1]["rendered"] = None
    if fault == "blank_speaker": details["nodes"][2]["text"] = " "
    if fault == "ambiguous": roots["widgets"].append(deepcopy(roots["widgets"][0]))
    if fault == "partial_scan": roots["configured_complete"] = False
    if fault == "truncated_tree": details["truncated"] = True
    if fault == "field": base.update(map_id="EndGame", player={"position": {"x": 1}})
    if fault == "paused": base["paused"] = True
    if fault == "combat": base["in_combat"] = True
    if fault == "cinema": cinema["playing"] = [{"kind": "cinema"}]
    if fault == "unknown_cinema": cinema["observed"] = False
    if fault == "malformed_root": roots["widgets"][0]["details"] = "broken"
    if fault == "hidden_owner": details["nodes"][-1]["rendered"] = False
    if fault == "unknown_owner": details["nodes"][-1].pop("rendered")
    assert dialogue.signal(roots, base, cinema)["fast_forwardable"] is False


def forwarder(monkeypatch, values):
    clock = [100.0]
    values = iter(values)
    reader = Mock(side_effect=lambda _: next(values))
    monkeypatch.setattr(dialogue, "read_dialogue", reader)
    service = SimpleNamespace(telemetry=SimpleNamespace(path="state.json"),
                              _monotonic=lambda: clock[0],
                              _wall_clock=lambda: datetime.now(timezone.utc),
                              _environment=SimpleNamespace(foreground_window_handle=lambda: 123,
                                                           game_window_handles=lambda: [{"hwnd": 123}]),
                              _action_lock=SimpleNamespace(locked=lambda: True), input=Mock(),
                              _arm=SimpleNamespace(status=lambda: (True, 120)),
                              _ensure_game_foreground=Mock(), _assert_one_off_visit_unchanged=Mock(),
                              _assert_action_allowed=Mock(return_value=SimpleNamespace(map_id=None, player=None)),
                              _queen_blood_prematch_visible=lambda _: False)
    trace, inputs = [], []
    return dialogue.DialogueForwarder(service, True, trace, inputs), service, reader, clock


def value(sequence, present=True, **extra):
    return {"source_sequence": sequence, "request_id": str(sequence),
            "dialogue_present": present, "fast_forwardable": present,
            "reason": "ordinary_line_with_advance_arrow" if present else "subtitle_absent_or_ambiguous",
            "line": "Greeting", **extra}


def test_one_click_release_reread_then_no_polling_after_conversation_finishes(monkeypatch):
    f, service, reader, _ = forwarder(monkeypatch, [value(1), value(2, False, reason="conversation_finished")])
    assert not f.advance(200) and not f.present()
    reader.assert_not_called()
    f.talk_started()
    assert f.advance(200)
    assert reader.call_count == 2
    service.input.mouse_click_in_game_window.assert_called_once_with("right", 20, 123)
    service.input.release_all.assert_called_once()
    assert not f.enabled and f.trace[-1]["reason"] == "conversation_finished"
    for _ in range(20):
        assert not f.advance(200) and not f.present()
    assert reader.call_count == 2 and len(f.inputs) == 1


def test_line_gap_or_choice_does_not_end_conversation_or_authorize_a_click(monkeypatch):
    f, service, reader, _ = forwarder(monkeypatch, [value(1), value(2, False),
        value(3, fast_forwardable=False, reason="decision_or_menu_visible"), value(4),
        value(5, False, reason="conversation_finished")])
    f.talk_started()
    assert f.present()
    assert not f.advance(200)
    service.input.mouse_click_in_game_window.assert_not_called()
    assert f.enabled and not f.advance(200) and f.enabled
    assert f.advance(200) and not f.enabled
    assert reader.call_count == 5
    service.input.mouse_click_in_game_window.assert_called_once()


def test_initial_wait_is_bounded_without_polling_the_rest_of_the_visit(monkeypatch):
    f, _, reader, clock = forwarder(monkeypatch, [value(1, False)])
    f.talk_started()
    assert not f.present() and f.enabled
    clock[0] += dialogue.OPENING_WAIT_SECONDS
    assert not f.present() and not f.enabled
    assert reader.call_count == 1


def test_wheel_before_first_line_keeps_post_choice_checker_active(monkeypatch):
    f, _, reader, clock = forwarder(monkeypatch, [value(1), value(2, False, reason="conversation_finished")])
    f.talk_started()
    f.wheel_seen()
    clock[0] += 20
    for _ in range(20):
        assert not f.advance(200) and f.present()
    reader.assert_not_called()
    f.choice_handled()
    assert f.advance(200) and not f.enabled


def test_multiple_wheels_suspend_subtitle_requests_until_verified_choices(monkeypatch):
    f, service, reader, _ = forwarder(monkeypatch, [value(1),
        value(2, False, interactive_ui=["TalkMenu_00_C"]), value(3),
        value(4, False, interactive_ui=["TalkMenu_00_C"]), value(5),
        value(6, False, reason="conversation_finished")])
    f.talk_started()
    for expected_reads in (2, 4):
        assert f.advance(200) and f.enabled and f.waiting_for_choice
        for _ in range(20):
            assert not f.advance(200) and f.present()
        assert reader.call_count == expected_reads
        f.choice_handled()
    assert f.advance(200) and not f.enabled
    assert reader.call_count == 6 and service.input.mouse_click_in_game_window.call_count == 3
    assert len([e for e in f.trace if e["event"] == "npc_dialogue_checker_resumed"]) == 2


def test_cached_subtitle_and_wheel_text_after_field_return_end_conversation():
    roots, base, cinema = scene()
    roots["widgets"][0]["details"]["in_viewport_ex"] = False
    base.update(map_id="EndGame", player={"position": {"x": 1}})
    assert dialogue.signal(roots, base, cinema)["reason"] == "conversation_finished"


@pytest.mark.parametrize("fault", ["sequence", "stale", "unknown"])
def test_bad_read_disables_the_episode_instead_of_retrying_forever(monkeypatch, fault):
    bad = value(1)
    if fault == "stale": bad.update(fast_forwardable=False, reason="dialogue_read_stale")
    if fault == "unknown": bad.update(fast_forwardable=False, reason="dialogue_unknown")
    f, service, reader, _ = forwarder(monkeypatch, [value(1), bad])
    f.talk_started()
    assert f.present()
    assert not f.advance(200) and not f.enabled
    for _ in range(10): assert not f.advance(200)
    assert reader.call_count == 2
    service.input.mouse_click_in_game_window.assert_not_called()


def test_request_scoped_read_rejects_old_timestamp(monkeypatch):
    roots, base, cinema = scene()
    request = Mock(return_value=({"ui_roots": roots, "timestamp_utc": "2000-01-01T00:00:00Z",
                                 "source_sequence": 1}, base))
    monkeypatch.setattr(dialogue, "request_snapshot", request)
    result = dialogue.read_dialogue(SimpleNamespace(_monotonic=lambda: 1, cutscene_state=lambda: cinema))
    assert result["reason"] == "dialogue_read_stale" and not result["fast_forwardable"]
    assert not result["input_sent"] and not result["input_authorized"]
    assert request.call_args.kwargs["flags"] == ("npcdialogue",)


def test_focus_change_cannot_click_another_application(monkeypatch):
    f, service, _, _ = forwarder(monkeypatch, [value(1)])
    service._environment.foreground_window_handle = lambda: 999
    f.talk_started()
    assert not f.advance(200)
    service.input.mouse_click_in_game_window.assert_not_called()


def test_slow_preflight_precedes_the_fresh_authorizing_read(monkeypatch):
    f, service, reader, clock = forwarder(monkeypatch, [value(1), value(2, False, reason="conversation_finished")])
    def slow_context(**_):
        clock[0] += 2
        reader.assert_not_called()
        return SimpleNamespace(map_id=None, player=None)
    service._assert_action_allowed = slow_context
    f.talk_started()
    assert f.advance(200)
    service.input.mouse_click_in_game_window.assert_called_once()


def test_stop_during_ui_read_prevents_click(monkeypatch):
    f, service, _, _ = forwarder(monkeypatch, [value(1)])
    service._arm.status = lambda: (False, 0)
    f.talk_started()
    assert not f.advance(200)
    service.input.mouse_click_in_game_window.assert_not_called()


def test_generic_field_return_reads_detachment_without_sending_a_click(monkeypatch):
    f, service, reader, _ = forwarder(monkeypatch, [value(1, False, reason="conversation_finished")])
    service._assert_action_allowed.return_value = SimpleNamespace(map_id="EndGame",
        player=SimpleNamespace(position=SimpleNamespace(x=1, y=2, z=3)))
    f.talk_started()
    f.wheel_seen()
    f.choice_handled()
    assert not f.advance(200) and not f.enabled
    reader.assert_called_once()
    service.input.mouse_click_in_game_window.assert_not_called()


@pytest.mark.parametrize("ending", ["field", "menu"])
def test_generic_talk_advances_lines_without_choosing_a_menu(monkeypatch, ending):
    from ff7r_agent.service import AgentService
    ending_read = value(2, False, reason="conversation_finished") if ending == "field" else value(
        2, False, interactive_ui=["Shop_C"])
    f, service, reader, clock = forwarder(monkeypatch, [value(1), ending_read])
    service._action_lock.acquire = lambda **_: True
    service._action_lock.release = Mock()
    service.input.is_real = True
    service._assert_real_movement_allowed = Mock()
    service.observe = lambda: {"in_menu": False, "paused": False}
    service._sleeper = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    service._record_action = Mock()
    monkeypatch.setattr("ff7r_agent.field_prompt.read_field_prompt", lambda _: {"talk": True})
    result = AgentService.talk_to_npc(service, fast_forward_dialogue=True)
    service.input.use_interaction.assert_called_once()
    service.input.mouse_click_in_game_window.assert_called_once_with("right", 20, 123)
    service.input.tap.assert_not_called()
    assert reader.call_count == 2
    assert result["trace"][-1]["reason"] == (
        "conversation_finished" if ending == "field" else "interactive_ui_requires_decision")
