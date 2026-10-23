from __future__ import annotations

import pytest

from ff7r_agent import dialogue_choice
from ff7r_agent.dialogue_choice import DialogueChoiceError, match_answer, step_toward, wheel_from
from ff7r_agent.service import AgentService

ROOT = "/Engine/Transient.GameEngine_2147482600:BP_EndGameInstance.TalkMenu_00.WidgetTree."


def live_probe(angle: float = 99.999992, shown: bool = True) -> dict:
    """The wheel at Cloud's door, Nibelheim, read live 2026-10-14: all six
    cells report shown, only Answer2 and Answer5 have text, and the cursor
    (the wheel centre) points at 100 degrees, on "Thanks." (right)."""
    cells = [(-30, 235), (-80, 310), (-60, 385), (60, 265), (80, 340), (30, 415)]
    return {
        "observed": True, "shown": shown, "cursor_shown": True, "in_viewport_ex": shown,
        "cursor": {"left": 0.0, "x": 0.0, "angle": angle, "y": 0.0, "top": 350.0},
        "answers": [{"left": float(left), "x": 0.0, "shown": True, "index": float(index), "angle": 0.0,
                     "y": 0.0, "top": float(top)} for index, (left, top) in enumerate(cells, 1)],
    }


LIVE_TEXTS = [
    {"path": ROOT + "Answer2.WidgetTree.Txt_00", "text": "On second thought..."},
    {"path": ROOT + "Answer5.WidgetTree.Txt_00", "text": "Thanks."},
    {"path": ROOT + "Txt_Question", "text": "\"Sorry... We won't push you.\""},
]


def test_live_wheel_lists_only_answers_with_text_and_finds_the_highlight() -> None:
    wheel = wheel_from(live_probe(), LIVE_TEXTS)
    assert wheel["shown"] is True
    assert wheel["question"] == "\"Sorry... We won't push you.\""
    assert [(a["index"], a["text"]) for a in wheel["answers"]] == [(2, "On second thought..."), (5, "Thanks.")]
    assert wheel["highlighted"] == 5


def test_cursor_on_the_left_answer_highlights_it() -> None:
    # The Queen's Blood wheel's Yes (left) reads about 280 degrees live.
    assert wheel_from(live_probe(angle=280.0), LIVE_TEXTS)["highlighted"] == 2


@pytest.mark.parametrize("angle, reason", [(None, "cursor_angle_unknown"), (190.0, "cursor_points_at_no_answer"),
                                           (10.0, "cursor_between_answers")])
def test_an_unverified_cursor_never_names_a_highlight(angle, reason) -> None:
    probe = live_probe()
    probe["cursor"]["angle"] = angle
    if reason == "cursor_between_answers":
        texts = LIVE_TEXTS + [{"path": ROOT + "Answer1.WidgetTree.Txt_00", "text": "Maybe."},
                              {"path": ROOT + "Answer4.WidgetTree.Txt_00", "text": "Later."}]
    else:
        texts = LIVE_TEXTS
    wheel = wheel_from(probe, texts)
    assert wheel["highlighted"] is None
    assert wheel["highlight_reason"] == reason


def test_a_hidden_wheel_keeps_old_texts_but_offers_nothing() -> None:
    wheel = wheel_from(live_probe(shown=False), LIVE_TEXTS)
    assert wheel["shown"] is False and wheel["question"] is None and wheel["highlighted"] is None


def test_answers_match_by_normalised_text_or_index() -> None:
    wheel = wheel_from(live_probe(), LIVE_TEXTS)
    assert match_answer(wheel, "on second thought")["index"] == 2
    assert match_answer(wheel, "THANKS")["index"] == 5
    assert match_answer(wheel, index=5)["text"] == "Thanks."
    with pytest.raises(DialogueChoiceError, match="not_found"):
        match_answer(wheel, "Go on.")
    with pytest.raises(DialogueChoiceError, match="ambiguous"):
        match_answer(wheel_from(live_probe(), LIVE_TEXTS + [
            {"path": ROOT + "Answer4.WidgetTree.Txt_00", "text": "Thanks anyway."}]), "thank")


def test_step_moves_along_the_axis_that_separates_the_answers() -> None:
    wheel = wheel_from(live_probe(), LIVE_TEXTS)
    right, left = wheel["answers"][1], wheel["answers"][0]
    assert step_toward(right, left) == "dialogue_left"
    assert step_toward(left, right) == "dialogue_right"


class FakeWheelService:
    """Just what choose_dialogue_option uses; the wheel moves with the arrows."""

    _minimum_action_delay = 0.0

    def __init__(self, angle: float) -> None:
        self.angle = angle
        self.closed = False
        self.taps: list[str] = []
        self.actions: list[dict] = []
        self.clock = 0.0
        self._last_action_finished = 0.0

    def _sleeper(self, seconds: float) -> None:
        self.clock += seconds

    def _monotonic(self) -> float:
        return self.clock

    def tap(self, button: str, milliseconds: int) -> dict:
        self.taps.append(button)
        if button == "dialogue_left":
            self.angle = 280.0
        elif button == "dialogue_right":
            self.angle = 100.0
        elif button == "confirm":
            self.closed = True
        return {"ok": True}

    def _record_action(self, **values) -> None:
        self.actions.append(values)

    def wheel(self) -> dict:
        wheel = wheel_from(live_probe(angle=self.angle, shown=not self.closed), LIVE_TEXTS)
        return wheel


@pytest.fixture
def fake(monkeypatch):
    service = FakeWheelService(angle=100.0)
    monkeypatch.setattr(dialogue_choice, "read_wheel", lambda svc: svc.wheel())
    return service


def test_choose_moves_verifies_and_confirms(fake) -> None:
    result = AgentService.choose_dialogue_option(fake, answer="On second thought", execute=True)
    assert result["ok"] is True and result["outcome"] == "chosen"
    assert fake.taps == ["dialogue_left", "confirm"]
    assert fake.actions[0]["answer"] == "On second thought..."


def test_choose_confirms_at_once_when_already_highlighted(fake) -> None:
    result = AgentService.choose_dialogue_option(fake, answer="Thanks.", execute=True)
    assert result["outcome"] == "chosen" and fake.taps == ["confirm"]


def test_choose_without_execute_sends_nothing(fake) -> None:
    result = AgentService.choose_dialogue_option(fake, index=2)
    assert result["outcome"] == "would_choose" and result["target"]["index"] == 2 and fake.taps == []


def test_choose_refuses_an_unknown_answer_without_input(fake) -> None:
    result = AgentService.choose_dialogue_option(fake, answer="Go on.", execute=True)
    assert result["ok"] is False and result["decision"] == "dialogue_answer_not_found" and fake.taps == []


def test_choose_never_confirms_an_unverified_highlight(fake, monkeypatch) -> None:
    fake.angle = 190.0
    result = AgentService.choose_dialogue_option(fake, answer="Thanks.", execute=True)
    assert result["outcome"] == "highlight_unverified" and fake.taps == []


class FlickeringProbe:
    """Live 2026-10-20: reads of an open wheel alternated shown / empty."""

    def __init__(self, pattern):
        self.pattern = list(pattern)
        self.reads = 0
        self.telemetry = type("T", (), {"request_queen_blood_dialogue": staticmethod(lambda: True)})()

    def _queen_blood_observe(self):
        self.reads += 1
        return self.pattern[min(self.reads - 1, len(self.pattern) - 1)]

    def _queen_blood_talk_wheel(self, sample):
        return live_probe() if sample else {"observed": True, "shown": False, "answers": []}

    def _sleeper(self, seconds):
        pass


def test_one_empty_read_of_an_open_wheel_is_read_again(monkeypatch) -> None:
    monkeypatch.setattr(dialogue_choice, "read_screen_texts", lambda svc, contains: {"texts": LIVE_TEXTS})
    service = FlickeringProbe([False, True])
    wheel = dialogue_choice.read_wheel(service)
    assert wheel["shown"] and wheel["reads"] == 2 and len(wheel["answers"]) == 2
    closed = dialogue_choice.read_wheel(FlickeringProbe([False]))
    assert closed["shown"] is False and closed["reads"] == dialogue_choice.WHEEL_READS
