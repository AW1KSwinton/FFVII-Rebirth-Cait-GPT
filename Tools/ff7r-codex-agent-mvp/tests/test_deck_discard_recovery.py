from types import SimpleNamespace

import pytest

from ff7r_agent.service import AgentService, SafetyError


def recovery(modes):
    clock = [0.0]
    actions = []
    current = iter(modes)
    last = modes[-1]

    def sleep(seconds):
        clock[0] += seconds

    service = SimpleNamespace(
        _deck_screen_mode=lambda: next(current, last),
        _deck_key=lambda key: actions.append(key),
        _deck_fast_key=lambda key: actions.append(key),
        _sleeper=sleep, _monotonic=lambda: clock[0],
        telemetry=SimpleNamespace(observe=lambda: pytest.fail("live recovery must use the fresh guide")),
        _deck_fresh_cursor=lambda *args: pytest.fail("closed editor cursor is not recovery evidence"),
    )
    return service, actions


def test_already_recovered_select_sends_no_more_input():
    service, actions = recovery(["select"])
    AgentService._discard_incomplete_deck(service)
    assert actions == []


def test_fresh_select_confirms_recovery_without_stale_editor_cursor():
    service, actions = recovery(["editor", "other", "select"])
    AgentService._discard_incomplete_deck(service)
    assert actions == ["queen_blood_cancel", "up", "confirm"]


@pytest.mark.parametrize("mode", ["editor", "other", "tips"])
def test_unconfirmed_close_fails_without_repeating_the_quit_inputs(mode):
    service, actions = recovery(["editor", mode])
    with pytest.raises(SafetyError, match="editor did not close"):
        AgentService._discard_incomplete_deck(service)
    assert actions == ["queen_blood_cancel", "up", "confirm"]


@pytest.mark.parametrize("mode", ["other", "tips"])
def test_unknown_initial_screen_does_not_send_quit_or_confirm(mode):
    service, actions = recovery([mode])
    with pytest.raises(SafetyError, match="verified editor"):
        AgentService._discard_incomplete_deck(service)
    assert actions == []
