"""Wheel-gated Queen's Blood dialogue against a scripted, laggy NPC."""
from __future__ import annotations

import pytest

from ff7r_agent.service import AgentService, SafetyError
from test_agent import FakeArm, FakeEnvironment, FakeRealInput
from test_queens_blood_match import MatchTelemetry, _queen_blood, _state


class ScriptedTurner:
    """Chatter first (no wheel), then the challenge wheel on the next Talk.

    Each Talk opens a conversation that plays for a few samples on its own;
    the second conversation ends on the Yes/No wheel, and Confirm while the
    wheel is shown (after Left) opens the deck screen.
    """

    def __init__(self, backend: FakeRealInput, *, chatter_talks: int = 1) -> None:
        self.backend = backend
        self.seen = 0
        self.sequence = 100
        self.talks = 1  # the caller has already sent the first Talk
        self.chatter_talks = chatter_talks
        self.dialogue_samples = 3
        self.wheel = False
        self.selected_yes = False
        self.deck = False
        self.presses_during_lines = 0

    def observe(self):
        for action in self.backend.actions[self.seen:]:
            if action[0] == "interaction":
                self.talks += 1
                self.dialogue_samples = 3
            elif action[0] == "tap" and action[1] == "queen_blood_left" and self.wheel:
                self.selected_yes = True
            elif action[0] == "tap" and action[1] == "confirm":
                if self.wheel and self.selected_yes:
                    self.deck = True
                elif not self.wheel:
                    self.presses_during_lines += 1
        self.seen = len(self.backend.actions)
        self.sequence += 1
        if self.deck:
            return _state(self.sequence, map_id=None, include_target=False, queen_blood=_queen_blood(
                screen="deck_select", begin_match_available=True, match_screen=True))
        if self.dialogue_samples > 0:
            self.dialogue_samples -= 1
            return _state(self.sequence, map_id=None, include_target=False,
                          queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False}))
        if self.talks > self.chatter_talks:
            self.wheel = True
            return _state(self.sequence, map_id=None, include_target=False, queen_blood=_queen_blood(
                talk_menu={"observed": True, "shown": True,
                           "cursor": {"angle": 280.0 if self.selected_yes else 100.0},
                           "answers": [{"index": 1, "shown": True}, {"index": 2, "shown": True}]}))
        return _state(self.sequence, include_target=False,
                      queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False}))


class _Clock:
    """Simulated time: sleeping advances it, so grace periods cost nothing."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


def _service(game, backend) -> AgentService:
    clock = _Clock()
    return AgentService(game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
                        minimum_action_delay=0.0, sleeper=clock.sleep, monotonic=clock.monotonic)


def test_dialogue_talks_again_after_chatter_and_confirms_only_on_the_wheel() -> None:
    backend = FakeRealInput()
    game = ScriptedTurner(backend)
    service = _service(game, backend)
    requests = []
    def request_match():
        assert game.wheel and game.selected_yes
        assert backend.actions[-1][:2] == ("tap", "confirm")
        requests.append("match")
        return True
    game.request_queen_blood_probe = request_match
    inputs: list[dict] = []

    state = service._run_queen_blood_dialogue(game.observe(), 1e12, inputs, [])

    assert service._queen_blood_prematch_visible(state)
    assert game.presses_during_lines == 0
    assert requests == ["match"]
    assert [i["reason"] for i in inputs] == ["talk_again", "dialogue_wheel_yes"]
    assert [a[1] for a in backend.actions if a[0] in {"tap", "interaction"}] == [
        1000, "queen_blood_left", "confirm"]


def test_dialogue_gives_up_when_every_talk_is_chatter() -> None:
    backend = FakeRealInput()
    game = ScriptedTurner(backend, chatter_talks=99)
    service = _service(game, backend)
    requests = []
    game.request_queen_blood_probe = lambda: requests.append("match")
    with pytest.raises(SafetyError, match="ended without the match prompt"):
        service._run_queen_blood_dialogue(game.observe(), 1e12, [], [])
    assert game.presses_during_lines == 0
    assert requests == []
    assert sum(1 for a in backend.actions if a[0] == "interaction") == 3


def test_failed_challenge_releases_the_dialogue_request(monkeypatch) -> None:
    backend = FakeRealInput()
    telemetry = MatchTelemetry([
        _state(1, queen_blood=_queen_blood()),
        _state(2, map_id=None, queen_blood=_queen_blood(
            talk_menu={"observed": True, "shown": True, "cursor": {"angle": 100.0}})),
    ])
    requests = []
    telemetry.request_queen_blood_dialogue = lambda: requests.append("dialogue")
    telemetry.request_queen_blood_probe = lambda: requests.append("match")
    telemetry.release_queen_blood_probe = lambda: requests.append("release")
    service = _service(telemetry, backend)
    def fail(*args, **_kwargs):
        raise SafetyError("the dialogue wheel did not accept Yes")
    monkeypatch.setattr(service, "_run_queen_blood_dialogue", fail)
    with pytest.raises(SafetyError, match="did not accept Yes"):
        service.initiate_queens_blood_match(target_id="qb-npc")
    assert requests == ["dialogue", "release"]


def test_initiation_timeout_reports_live_dialogue_for_review_with_capture(monkeypatch) -> None:
    backend = FakeRealInput()
    telemetry = MatchTelemetry([
        _state(1, queen_blood=_queen_blood()),
        _state(2, map_id=None, include_target=False,
               interaction={"state": "dialogue", "target_id": "qb-npc"},
               queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False})),
    ])
    requests = []
    telemetry.request_queen_blood_dialogue = lambda: requests.append("dialogue")
    telemetry.release_queen_blood_probe = lambda: requests.append("release")
    service = _service(telemetry, backend)

    def timeout(*_args, **_kwargs):
        raise SafetyError("the dialogue wheel did not appear before the timeout")

    monkeypatch.setattr(service, "_run_queen_blood_dialogue", timeout)
    monkeypatch.setattr(service, "_capture_game_window_under_action",
                        lambda backend="window": {"ok": True, "path": "fresh-frame.bmp"})

    result = service.initiate_queens_blood_match(target_id="qb-npc")

    assert result["ok"] is False
    assert result["outcome"] == "dialogue_pending_review"
    assert result["prompt_state"] == "dialogue_or_transition_active"
    assert result["telemetry_evidence"]["wheel_observed"] is True
    assert result["telemetry_evidence"]["wheel_shown"] is False
    assert result["visual_evidence"]["path"] == "fresh-frame.bmp"
    assert result["visual_evidence"]["interpretation"] == "captured_only_not_semantically_classified"
    assert result["recovery_required"] is True
    assert result["campaign_state"] is None
    assert requests == ["dialogue", "release"]
    assert [action[0] for action in backend.actions if action[0] in {"interaction", "tap"}] == ["interaction"]


def test_initiation_timeout_does_not_claim_prompt_when_feed_is_back_on_field(monkeypatch) -> None:
    backend = FakeRealInput()
    telemetry = MatchTelemetry([
        _state(1, queen_blood=_queen_blood()),
        _state(2, map_id=None, include_target=False,
               interaction={"state": "dialogue", "target_id": "qb-npc"},
               queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False})),
    ])
    telemetry.request_queen_blood_dialogue = lambda: True
    service = _service(telemetry, backend)

    def timeout(*_args, **_kwargs):
        telemetry.states = [_state(3, include_target=False,
                                   queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False}))]
        telemetry.index = 0
        raise SafetyError("the dialogue wheel did not appear before the timeout")

    monkeypatch.setattr(service, "_run_queen_blood_dialogue", timeout)
    monkeypatch.setattr(service, "_capture_game_window_under_action",
                        lambda backend="window": {"ok": True, "path": "field-frame.bmp"})

    result = service.initiate_queens_blood_match(target_id="qb-npc")

    assert result["ok"] is False
    assert result["outcome"] == "dialogue_timeout"
    assert result["prompt_state"] == "unverified"
    assert result["campaign_state"] == "blocked"
    assert result["visual_evidence"]["path"] == "field-frame.bmp"
    assert [action[0] for action in backend.actions if action[0] in {"interaction", "tap"}] == ["interaction"]


def test_initiation_timeout_detects_deck_select_that_appeared_at_deadline(monkeypatch) -> None:
    backend = FakeRealInput()
    telemetry = MatchTelemetry([
        _state(1, queen_blood=_queen_blood()),
        _state(2, map_id=None, include_target=False,
               interaction={"state": "dialogue", "target_id": "qb-npc"},
               queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False})),
    ])
    telemetry.request_queen_blood_dialogue = lambda: True
    service = _service(telemetry, backend)

    def timeout(*_args, **_kwargs):
        telemetry.states = [_state(3, map_id=None, include_target=False,
                                   queen_blood=_queen_blood(screen="deck_select",
                                                             begin_match_available=True,
                                                             match_screen=True))]
        telemetry.index = 0
        raise SafetyError("the dialogue wheel did not appear before the timeout")

    monkeypatch.setattr(service, "_run_queen_blood_dialogue", timeout)
    monkeypatch.setattr(service, "_capture_game_window_under_action",
                        lambda backend="window": {"ok": True, "path": "deck-frame.bmp"})

    result = service.initiate_queens_blood_match(target_id="qb-npc", stop_at_deck_select=True)

    assert result["ok"] is False  # Do not claim the full initiation succeeded.
    assert result["outcome"] == "deck_select_pending_review"
    assert result["prompt_state"] == "deck_select_visible"
    assert result["campaign_state"] is None
    assert result["visual_evidence"]["path"] == "deck-frame.bmp"
    assert [action[0] for action in backend.actions if action[0] in {"interaction", "tap"}] == ["interaction"]


def test_unverified_yes_cannot_confirm_or_start_full_scans(monkeypatch) -> None:
    backend = FakeRealInput()
    wheel = _state(1, queen_blood=_queen_blood(
        talk_menu={"observed": True, "shown": True, "cursor": {}}))
    telemetry = MatchTelemetry([wheel])
    requests = []
    telemetry.request_queen_blood_dialogue = lambda: True
    telemetry.request_queen_blood_probe = lambda: requests.append("match")
    service = _service(telemetry, backend)
    monkeypatch.setattr(service, "_wait_for_wheel_yes", lambda deadline: wheel)
    with pytest.raises(SafetyError, match="Yes selection is unverified"):
        service._run_queen_blood_dialogue(wheel, 1e12, [], [])
    assert backend.actions == [] and requests == []


def test_fresh_verified_yes_confirms_before_an_intermittent_wheel_read() -> None:
    backend = FakeRealInput()
    wheel = _state(1, map_id=None, include_target=False, queen_blood=_queen_blood(
        talk_menu={"observed": True, "shown": True, "cursor": {"angle": 280.0}}))
    missing = _state(2, map_id=None, include_target=False, queen_blood=_queen_blood(
        talk_menu={"observed": False}))
    deck = _state(3, map_id=None, include_target=False, queen_blood=_queen_blood(
        screen="deck_select", begin_match_available=True, match_screen=True))
    telemetry = MatchTelemetry([wheel, missing, deck])
    telemetry.request_queen_blood_dialogue = lambda: True
    requests = []
    telemetry.request_queen_blood_probe = lambda: requests.append("match") or True
    service = _service(telemetry, backend)
    inputs = []
    state = service._run_queen_blood_dialogue(telemetry.observe(), 1e12, inputs, [])
    assert service._queen_blood_prematch_visible(state)
    assert [action[:2] for action in backend.actions] == [("tap", "confirm")]
    assert requests == ["match"] and len(inputs) == 1
    assert inputs[0]["cursor"]["angle"] == 280.0


def test_a_confirmed_yes_whose_deck_screen_never_appears_says_so() -> None:
    # Live 2026-10-12 (#328): Yes was accepted and the game showed deck
    # select, but the probe never reported it, and the timeout claimed the
    # wheel had never appeared.
    backend = FakeRealInput()
    wheel = _state(1, map_id=None, include_target=False, queen_blood=_queen_blood(
        talk_menu={"observed": True, "shown": True, "cursor": {"angle": 280.0}}))
    transition = _state(2, map_id=None, include_target=False, queen_blood=_queen_blood(
        talk_menu={"observed": True, "shown": False}))
    telemetry = MatchTelemetry([wheel, transition])
    telemetry.request_queen_blood_dialogue = lambda: True
    telemetry.request_queen_blood_probe = lambda: True
    service = _service(telemetry, backend)
    with pytest.raises(SafetyError, match="^Yes was confirmed but the deck screen did not appear"):
        service._run_queen_blood_dialogue(telemetry.observe(), 1e12, [], [])
    assert [action[:2] for action in backend.actions] == [("tap", "confirm")]


def test_initiation_after_a_confirmed_yes_never_asks_for_another_confirm(monkeypatch) -> None:
    backend = FakeRealInput()
    telemetry = MatchTelemetry([
        _state(1, queen_blood=_queen_blood()),
        _state(2, map_id=None, include_target=False,
               interaction={"state": "dialogue", "target_id": "qb-npc"},
               queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False})),
    ])
    telemetry.request_queen_blood_dialogue = lambda: True
    service = _service(telemetry, backend)

    def timeout(*_args, **_kwargs):
        raise SafetyError("Yes was confirmed but the deck screen did not appear before the timeout")

    monkeypatch.setattr(service, "_run_queen_blood_dialogue", timeout)
    monkeypatch.setattr(service, "_capture_game_window_under_action",
                        lambda backend="window": {"ok": True, "path": "after-yes.bmp"})

    result = service.initiate_queens_blood_match(target_id="qb-npc", stop_at_deck_select=True)

    assert result["ok"] is False
    assert result["outcome"] == "dialogue_pending_review"
    assert result["campaign_state"] is None
    assert result["next_step"].startswith("Yes was confirmed")
    assert "do not send another Talk or Confirm" in result["next_step"]
    assert result["visual_evidence"]["path"] == "after-yes.bmp"


def test_initiation_hands_off_a_shown_wheel_without_generic_dialogue() -> None:
    backend = FakeRealInput()
    def missing(sequence):
        return _state(sequence, map_id=None, include_target=False,
                      queen_blood=_queen_blood(talk_menu={"observed": False}))
    wheel = _state(3, map_id=None, include_target=False, queen_blood=_queen_blood(
        talk_menu={"observed": True, "shown": True, "cursor": {"angle": 280.0}}))
    deck = _state(5, map_id=None, include_target=False, queen_blood=_queen_blood(
        screen="deck_select", begin_match_available=True, match_screen=True))
    telemetry = MatchTelemetry([missing(1), missing(2), wheel, missing(4), deck])
    requests = []
    telemetry.request_queen_blood_dialogue = lambda: requests.append("dialogue") or True
    telemetry.request_queen_blood_probe = lambda: requests.append("match") or True
    result = _service(telemetry, backend).initiate_queens_blood_match(
        target_id="qb-npc", stop_at_deck_select=True, timeout_seconds=30)
    assert result["ok"] and result["outcome"] == "deck_select_ready"
    assert requests == ["dialogue", "match"]
    assert [action[:2] for action in backend.actions] == [("tap", "confirm")]
    assert result["inputs"][0]["reason"] == "dialogue_wheel_yes"
    assert result["inputs"][0]["cursor"]["angle"] == 280.0


def test_initiation_advances_subtitles_before_the_wheel_without_a_grace_wait(monkeypatch) -> None:
    """Live 2026-10-12 a 30 s grace wait ran first and no line was skipped."""
    import ff7r_agent.npc_dialogue as npc_dialogue

    backend = FakeRealInput()
    def subtitles(sequence):
        return _state(sequence, map_id=None, include_target=False,
                      queen_blood=_queen_blood(talk_menu={"observed": False}))
    wheel_index = 12
    states = [_state(1, queen_blood=_queen_blood())]
    states += [subtitles(sequence) for sequence in range(2, wheel_index + 1)]
    states += [
        _state(wheel_index + 1, map_id=None, include_target=False, queen_blood=_queen_blood(
            talk_menu={"observed": True, "shown": True, "cursor": {"angle": 280.0}})),
        subtitles(wheel_index + 2),
        _state(wheel_index + 3, map_id=None, include_target=False, queen_blood=_queen_blood(
            screen="deck_select", begin_match_available=True, match_screen=True)),
    ]
    telemetry = MatchTelemetry(states)
    telemetry.request_queen_blood_dialogue = lambda: True
    telemetry.request_queen_blood_probe = lambda: True
    advances: list[int] = []

    class Forwarder:
        def __init__(self, _service, _enabled, _trace, _inputs) -> None:
            self.enabled = self.seen_dialogue = False

        def talk_started(self) -> None:
            self.enabled = True

        def present(self) -> bool:
            # A fresh read found an ordinary fast-forwardable subtitle.
            self.seen_dialogue = True
            return True

        def advance(self, _deadline) -> bool:
            if len(advances) >= 3:
                return False
            advances.append(telemetry.index)
            return True

        def wheel_seen(self) -> None: ...
        def choice_handled(self) -> None: ...
        def stop(self, _reason) -> None: ...

    monkeypatch.setattr(npc_dialogue, "DialogueForwarder", Forwarder)
    result = _service(telemetry, backend).initiate_queens_blood_match(
        target_id="qb-npc", stop_at_deck_select=True, timeout_seconds=90)

    assert result["ok"] and result["outcome"] == "deck_select_ready"
    assert len(advances) == 3
    assert all(index < wheel_index for index in advances)
    assert advances[0] <= 5
    assert [i.get("reason") for i in result["inputs"]][-1] == "dialogue_wheel_yes"


def test_decline_mode_selects_verified_no_and_returns_to_settled_field() -> None:
    telemetry = MatchTelemetry([
        _state(1, queen_blood={"active": False, "phase": "inactive"}),
        _state(2, map_id=None, interaction={"state": "dialogue", "target_id": "qb-npc"},
               include_target=False, queen_blood=_queen_blood(
                   talk_menu={"observed": True, "shown": False})),
        _state(3, map_id=None, interaction={"state": "dialogue", "target_id": "qb-npc"},
               include_target=False, queen_blood=_queen_blood(
                   talk_menu={"observed": True, "shown": True,
                              "cursor": {"angle": 280.0}, "answers": []})),
        _state(4, map_id=None, interaction={"state": "dialogue", "target_id": "qb-npc"},
               include_target=False, queen_blood=_queen_blood(
                   talk_menu={"observed": True, "shown": True,
                              "cursor": {"angle": 280.0}, "answers": []})),
        _state(5, map_id=None, interaction={"state": "dialogue", "target_id": "qb-npc"},
               include_target=False, queen_blood=_queen_blood(
                   talk_menu={"observed": True, "shown": True,
                              "cursor": {"angle": 280.0}, "answers": []})),
        _state(6, map_id=None, interaction={"state": "dialogue", "target_id": "qb-npc"},
               include_target=False, queen_blood=_queen_blood(
                   talk_menu={"observed": True, "shown": True,
                              "cursor": {"angle": 100.0}, "answers": []})),
        _state(7, map_id="EndGame", include_target=False,
               queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False})),
        _state(8, map_id="EndGame", include_target=False,
               queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False})),
        _state(9, map_id="EndGame", include_target=False,
               queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False})),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)
    requests = []
    telemetry.request_queen_blood_dialogue = lambda: requests.append("dialogue")
    telemetry.request_queen_blood_probe = lambda: requests.append("match")
    telemetry.release_queen_blood_probe = lambda: requests.append("release")

    result = service.initiate_queens_blood_match(target_id="qb-npc", decline=True)

    assert result["outcome"] == "declined"
    assert requests == ["dialogue", "release"]
    assert result["map_id"] == "EndGame"
    assert result["settled_position"] == {"x": 0, "y": 0, "z": 0}
    assert [action[:2] for action in backend.actions if action[0] in {"interaction", "tap"}] == [
        ("interaction", 1000), ("tap", "queen_blood_right"), ("tap", "confirm")
    ]
    assert all(action[1] != "queen_blood_begin_match" for action in backend.actions if action[0] == "tap")


def test_finish_conversation_waits_for_lines_when_no_wheel_appears() -> None:
    telemetry = MatchTelemetry([
        _state(1, map_id=None, interaction={"state": "dialogue"}, include_target=False,
               queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False})),
        _state(2, map_id=None, interaction={"state": "dialogue"}, include_target=False,
               queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False})),
        _state(3, map_id="EndGame", include_target=False,
               queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False})),
        _state(4, map_id="EndGame", include_target=False,
               queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False})),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.finish_conversation(timeout_seconds=10)

    assert result["outcome"] == "finished"
    assert result["map_id"] == "EndGame"
    assert result["settled_position"] == {"x": 0, "y": 0, "z": 0}
    assert backend.actions == []


class ScriptedDecliner:
    """A Talk that opens late, plays lines, then shows the wheel with Yes highlighted.

    ``delay`` field samples pass after Talk before the conversation shows (live,
    up to ~18 s); Right moves the cursor to No; a Confirm on No closes the
    wheel only after ``linger`` more samples (the probe samples it about once
    a second), or never when ``linger`` is None.
    """

    def __init__(self, backend: FakeRealInput, *, delay: int = 0, lines: int = 2,
                 wheel: bool = True, linger: int | None = 0) -> None:
        self.backend = backend
        self.seen = 0
        self.sequence = 0
        self.talked = False
        self.delay, self.lines, self.has_wheel, self.linger = delay, lines, wheel, linger
        self.on_no = False
        self.declined_at: int | None = None
        self.confirms = 0
        self.done = False

    def observe(self):
        for action in self.backend.actions[self.seen:]:
            if action[0] == "interaction":
                self.talked = True
            elif action[0] == "tap" and action[1] == "queen_blood_right":
                self.on_no = True
            elif action[0] == "tap" and action[1] == "confirm":
                self.confirms += 1
                if self.on_no and self.declined_at is None:
                    self.declined_at = self.sequence
        self.seen = len(self.backend.actions)
        self.sequence += 1
        field = _state(self.sequence, map_id="EndGame", include_target=False,
                       queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False}))
        if not self.talked:
            return _state(self.sequence, queen_blood={"active": False, "phase": "inactive"})
        if self.done:
            return field
        if self.delay > 0:
            self.delay -= 1
            return _state(self.sequence, queen_blood={"active": False, "phase": "inactive"})
        if self.lines > 0:
            self.lines -= 1
            return _state(self.sequence, map_id=None, interaction={"state": "dialogue", "target_id": "qb-npc"},
                          include_target=False, queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False}))
        if not self.has_wheel:
            self.done = True
            return field
        if self.declined_at is not None and self.linger is not None \
                and self.sequence - self.declined_at > self.linger:
            self.done = True
            return field
        return _state(self.sequence, map_id=None, interaction={"state": "dialogue", "target_id": "qb-npc"},
                      include_target=False, queen_blood=_queen_blood(talk_menu={
                          "observed": True, "shown": True,
                          "cursor": {"angle": 100.0 if self.on_no else 280.0}, "answers": []}))


def test_decline_waits_for_a_conversation_that_opens_late() -> None:
    backend = FakeRealInput()
    game = ScriptedDecliner(backend, delay=30)  # about ten seconds of field samples after Talk

    result = _service(game, backend).initiate_queens_blood_match(target_id="qb-npc", decline=True)

    assert result["outcome"] == "declined"
    assert [action[:2] for action in backend.actions if action[0] in {"interaction", "tap"}] == [
        ("interaction", 1000), ("tap", "queen_blood_right"), ("tap", "confirm")
    ]


def test_decline_reuses_a_fresh_no_proof_before_native_rediscovery() -> None:
    # Synthetic timing: the next consumer read loses the widget until input.
    # An already acquired No cursor remains sufficient, as it is for Yes.
    class OneSampleNo(ScriptedDecliner):
        def observe(self):
            if self.sequence and not any(a[:2] == ("tap", "confirm") for a in self.backend.actions):
                self.sequence += 1
                return _state(self.sequence, map_id=None, include_target=False,
                    queen_blood=_queen_blood(talk_menu={"observed": False}))
            return super().observe()

    backend = FakeRealInput()
    game = OneSampleNo(backend, lines=0)
    game.talked = game.on_no = True
    result = _service(game, backend).finish_conversation(timeout_seconds=10)
    assert result["outcome"] == "declined"
    assert [(a[0], a[1]) for a in backend.actions] == [("tap", "confirm")]
    assert result["inputs"][0]["cursor"]["angle"] == 100.0


@pytest.mark.parametrize("angle", [None, True, float("nan"), float("inf")])
def test_decline_never_confirms_an_unknown_or_nonfinite_cursor(angle) -> None:
    telemetry = MatchTelemetry([_state(1, map_id=None, include_target=False,
        queen_blood=_queen_blood(talk_menu={"observed": True, "shown": True,
                                           "cursor": {"angle": angle}}))])
    backend = FakeRealInput()
    with pytest.raises(SafetyError):
        _service(telemetry, backend).finish_conversation(timeout_seconds=10)
    assert not any(a[:2] == ("tap", "confirm") for a in backend.actions)


def test_decline_accepts_fresh_no_after_an_intermittent_wheel_lookup() -> None:
    # Synthetic rediscovery: the selection reader misses the widget; the
    # next fresh sample recovers it already on No. Never discard that proof.
    backend = FakeRealInput()
    game = ScriptedDecliner(backend, lines=0)
    game.talked = game.on_no = True
    service = _service(game, backend)
    service._wait_for_wheel_no = lambda _, initial=None: None
    initial = _state(1, map_id=None, include_target=False,
        queen_blood=_queen_blood(talk_menu={"observed": True, "shown": True,
                                           "cursor": {"angle": None}}))
    inputs = []
    _, declined = service._finish_open_conversation(initial, 1010, inputs, [])
    assert declined and game.confirms == 1
    assert [(a[0], a[1]) for a in backend.actions] == [("tap", "confirm")]
    assert inputs[0]["cursor"]["angle"] == 100.0


def test_decline_does_not_confirm_hidden_cursor_during_wheel_rediscovery() -> None:
    # Synthetic timing: eight consumer reads lose the wheel after Right,
    # while its retained cursor still reads No. Only fresh shown No permits
    # Confirm. Rediscovery needs no repeated Right presses or subtitle reads.
    class Rediscovery(ScriptedDecliner):
        missing = 8
        last_shown = True
        confirmed_while_hidden = False

        def observe(self):
            if any(a[:2] == ("tap", "confirm") for a in self.backend.actions[self.seen:]):
                self.confirmed_while_hidden = not self.last_shown
            state = super().observe()
            if self.on_no and not self.confirms and self.missing:
                self.missing -= 1
                state = _state(self.sequence, map_id=None, include_target=False,
                    queen_blood=_queen_blood(talk_menu={"observed": True, "shown": False,
                                                       "cursor": {"angle": 100.0}}))
            self.last_shown = bool((state.queen_blood or {}).get("talk_menu", {}).get("shown"))
            return state

    backend = FakeRealInput()
    game = Rediscovery(backend, lines=0)
    game.talked = True
    service = _service(game, backend)
    initial = _state(1, map_id=None, include_target=False,
        queen_blood=_queen_blood(talk_menu={"observed": True, "shown": True,
                                           "cursor": {"angle": 280.0}}))
    inputs = []
    _, declined = service._finish_open_conversation(initial, 1020, inputs, [])
    assert declined and not game.confirmed_while_hidden
    assert [(a[0], a[1]) for a in backend.actions] == [("tap", "queen_blood_right"), ("tap", "confirm")]


def test_recovery_refreshes_a_prior_no_sample_before_reusing_cursor_evidence() -> None:
    class PriorNo(ScriptedDecliner):
        requested = False

        def request_queen_blood_dialogue(self):
            self.requested = True
            return True

        def observe(self):
            if not self.requested:
                self.sequence += 1
                return _state(self.sequence, map_id=None, include_target=False,
                    queen_blood=_queen_blood(talk_menu={"observed": True, "shown": True,
                                                        "cursor": {"angle": 100.0}}))
            return super().observe()

    backend = FakeRealInput()
    game = PriorNo(backend, lines=0)
    game.talked = True  # The actual open wheel is on Yes, despite the prior No sample.
    result = _service(game, backend).finish_conversation(timeout_seconds=10)
    assert result["outcome"] == "declined"
    assert [a[:2] for a in backend.actions] == [("tap", "queen_blood_right"), ("tap", "confirm")]


def test_decline_confirms_no_at_most_three_times_while_the_wheel_lingers() -> None:
    backend = FakeRealInput()
    game = ScriptedDecliner(backend, linger=12)

    result = _service(game, backend).initiate_queens_blood_match(target_id="qb-npc", decline=True)

    assert result["outcome"] == "declined"
    assert game.confirms == 1  # the response window outlasts a wheel that is slow to close


def test_decline_gives_up_after_three_unaccepted_confirms() -> None:
    backend = FakeRealInput()
    game = ScriptedDecliner(backend, linger=None)

    with pytest.raises(SafetyError, match="did not accept No"):
        _service(game, backend).initiate_queens_blood_match(target_id="qb-npc", decline=True)
    assert game.confirms == 3


def test_decline_reports_a_conversation_that_never_opens() -> None:
    backend = FakeRealInput()
    game = ScriptedDecliner(backend, delay=10_000)

    with pytest.raises(SafetyError, match="conversation_not_observed"):
        _service(game, backend).initiate_queens_blood_match(target_id="qb-npc", decline=True)
    assert [action[0] for action in backend.actions if action[0] in {"interaction", "tap"}] == ["interaction"]


def test_decline_without_a_match_prompt_is_not_a_successful_visit() -> None:
    backend = FakeRealInput()
    game = ScriptedDecliner(backend, wheel=False)

    with pytest.raises(SafetyError, match="no_match_prompt"):
        _service(game, backend).initiate_queens_blood_match(target_id="qb-npc", decline=True)
    assert game.confirms == 0


class _FocusEnvironment(FakeEnvironment):
    def __init__(self, foreground: bool, handle: int | None) -> None:
        super().__init__(foreground=foreground)
        self.handle = handle

    def foreground_window_handle(self):
        return self.handle


def test_focus_is_only_borrowed_from_another_window() -> None:
    backend = FakeRealInput()
    other = AgentService(ScriptedTurner(backend), backend, arm_state=FakeArm(),
                         environment=_FocusEnvironment(False, 4242), minimum_action_delay=0.0)
    assert other._foreground_to_restore() == 4242
    game_in_front = AgentService(ScriptedTurner(backend), backend, arm_state=FakeArm(),
                                 environment=_FocusEnvironment(True, 777), minimum_action_delay=0.0)
    assert game_in_front._foreground_to_restore() is None
