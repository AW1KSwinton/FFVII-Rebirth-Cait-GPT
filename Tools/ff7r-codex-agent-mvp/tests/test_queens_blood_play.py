"""Verified Queen's Blood match input against a reactive fake game."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ff7r_agent.runtime_commands import invoke_runtime
from ff7r_agent.service import AgentService, SafetyError
from ff7r_agent.telemetry import JsonFileTelemetry
from test_agent import FakeArm, FakeEnvironment, FakeRealInput, StepClock
from test_queens_blood_match import _queen_blood, _state

START_PAWNS = {(0, 0): ("player", 1), (1, 0): ("player", 1), (2, 0): ("player", 1),
               (0, 4): ("opponent", 1), (1, 4): ("opponent", 1), (2, 4): ("opponent", 1)}
HAND = [
    {"hand_index": 0, "card_id": "card_id_000", "name": "Security Officer", "cost": 1, "power": 1,
     "influence_offsets": [{"row": -1, "column": 0}, {"row": 0, "column": -1},
                           {"row": 0, "column": 1}, {"row": 1, "column": 0}]},
    {"hand_index": 1, "card_id": "card_id_004", "name": "J-Unit Sweeper", "cost": 2, "power": 3},
]


class ReactiveGame:
    """A tiny card-game model that reacts to the fake backend's inputs.

    Every observe() first applies any new button presses (hand navigation,
    Confirm pick-up / place, board arrows, Cancel, pass hold), then publishes a
    fresh sequence. ``drop`` names presses the game should ignore once, to
    model lag-dropped input.
    """

    def __init__(
        self,
        backend: FakeRealInput,
        *,
        drop: set[int] | None = None,
        start: tuple[int, int] = (0, 0),
        cursor_widget: bool = True,
        pawns: dict | None = None,
        locations: bool = False,
    ) -> None:
        self.locations = locations
        self.pawns = pawns or START_PAWNS
        self.start = start
        self.cursor_widget = cursor_widget
        self.backend = backend
        self.seen = 0
        self.sequence = 10
        self.cursor = 0
        self.placing = False
        self.target = (0, 0)
        self.cards: dict[tuple[int, int], str] = {}
        self.phase = "player_turn"
        self.drop = drop or set()
        self.press_count = 0
        self.passes_in_a_row = 0

    def _legal(self, row: int, column: int) -> bool:
        owner, rank = self.pawns.get((row, column), ("empty", 0))
        card = HAND[self.cursor]
        return owner == "player" and rank >= card["cost"] and (row, column) not in self.cards

    def _apply(self, button: str, milliseconds: int) -> None:
        self.press_count += 1
        if self.press_count in self.drop:
            return
        if self.phase == "result":
            if button == "confirm":
                self.phase = "closed"
            return
        if button == "queen_blood_play" and milliseconds >= 1000 and not self.placing:
            self.passes_in_a_row += 1
            self.phase = "result" if self.passes_in_a_row >= 2 else "opponent_turn"
        elif button == "confirm" and not self.placing:
            self.placing = True
            self.target = self.start
        elif button == "confirm" and self.placing and self._legal(*self.target):
            self.cards[self.target] = HAND[self.cursor]["card_id"]
            self.placing = False
            self.passes_in_a_row = 0
            self.phase = "opponent_turn"
        elif button == "queen_blood_cancel":
            self.placing = False
        elif self.placing and button in {"up", "down"}:
            row = max(0, min(2, self.target[0] + (1 if button == "down" else -1)))
            self.target = (row, self.target[1])
        elif self.placing and button in {"queen_blood_left", "queen_blood_right"}:
            column = max(0, min(4, self.target[1] + (1 if button == "queen_blood_right" else -1)))
            self.target = (self.target[0], column)
        elif not self.placing and button in {"queen_blood_left", "queen_blood_right"}:
            step = 1 if button == "queen_blood_right" else -1
            self.cursor = max(0, min(len(HAND) - 1, self.cursor + step))

    def observe(self):
        for action in self.backend.actions[self.seen:]:
            if action[0] == "tap":
                self._apply(action[1], action[2])
        self.seen = len(self.backend.actions)
        if self.phase == "closed":
            self.sequence += 1
            return _state(self.sequence, queen_blood=_queen_blood(active=False, phase="inactive"))
        if self.phase == "opponent_turn":
            # The fake opponent always passes; two passes in a row end it.
            self.passes_in_a_row += 1
            self.phase = "result" if self.passes_in_a_row >= 2 else "player_turn"
        self.sequence += 1
        cells = []
        for row in range(3):
            for column in range(5):
                owner, rank = self.pawns.get((row, column), ("empty", 0))
                card = (row, column) in self.cards
                if card:
                    owner, rank = "player", 0
                cell = {
                    "row": row, "column": column, "owner": owner, "rank": rank,
                    "card_present": card,
                    "highlight": not self.placing and self._legal(row, column),
                }
                if card:
                    cell.update(card_id=self.cards[(row, column)], power=1)
                cells.append(cell)
        placement = {"active": self.placing}
        if self.placing and self._legal(*self.target):
            placement.update(target_row=self.target[0], target_column=self.target[1])
        if self.placing and self.cursor_widget:
            placement.update(cursor_row=self.target[0], cursor_column=self.target[1])
        if self.placing and self.locations:
            x, y = cell_centre(*self.target)
            placement.update(actor_location={"x": x, "y": y, "z": 5.0})
        return _state(self.sequence, queen_blood=_queen_blood(
            active=True,
            phase=self.phase,
            result={"observed": True, "shown": self.phase == "result"},
            turn=1,
            screen="match",
            cursor={"area": "hand", "hand_index": self.cursor},
            hand=[dict(card, playable=True) for card in HAND],
            board={"rows": 3, "columns": 5, "cells": cells, "placement": placement,
                   # Live, the result screen reads every lane total as 0.
                   "lanes": [{"row": r, "opponent_power": 0, "player_power": 0 if self.phase == "result" else sum(
                       1 for (row, _column) in self.cards if row == r)} for r in range(3)]},
        ))


def cell_centre(row: int, column: int) -> tuple[float, float]:
    return (1000.0 - 30.0 * column, 200.0 + 40.0 * row)


def _service(game: ReactiveGame, backend: FakeRealInput) -> AgentService:
    return AgentService(
        game,
        backend,
        arm_state=FakeArm(),
        environment=FakeEnvironment(),
        minimum_action_delay=0.0,
        sleeper=lambda _seconds: None,
    )


def test_place_card_moves_the_board_cursor_in_one_burst() -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = _service(game, backend)

    result = service.place_queens_blood_card(0, 2, 0)

    assert result["verified"] is True
    assert game.cards == {(2, 0): "card_id_000"}
    assert [a[1] for a in backend.actions if a[0] == "tap"] == ["confirm", "down", "down", "confirm"]
    # Checked once after the burst, not after every press.
    assert [step["observed"] for step in result["placement_steps"]] == [[0, 0], [2, 0]]
    assert result["card_left_hand"] is False  # the fake hand never shrinks


def test_placement_failure_keeps_bounded_trace_when_held_card_becomes_unobservable() -> None:
    from dataclasses import replace

    backend = FakeRealInput()
    game = ReactiveGame(backend)
    observe = game.observe
    held_samples = 0

    def missing_after_pickup():
        nonlocal held_samples
        sample = observe()
        if game.placing:
            held_samples += 1
            if held_samples > 1:
                return replace(sample, queen_blood=None)
        return sample

    game.observe = missing_after_pickup
    service = _service(game, backend)
    service._monotonic = StepClock()
    with pytest.raises(SafetyError, match="placement ended") as caught:
        service.place_queens_blood_card(0, 1, 0)

    details = caught.value.details
    assert details["stage"] == "board_navigation"
    assert details["card_id"] == "card_id_000"
    assert details["confirm_attempted"] is False
    assert details["inputs"] == [{"button": "confirm", "reason": "pick_up_card", "attempt": 1}]
    assert details["observations"][1]["placement_active"] is True
    assert details["observations"][-1]["phase"] is None
    assert len(details["observations"]) <= 72
    assert details["last_sequence"] > details["pre_sequence"]
    json.dumps(details)  # The worker can send it without retaining native objects.
    assert game.placing and not game.cards
    assert [a[1] for a in backend.actions if a[0] == "tap"] == ["confirm"]
    assert not service._action_lock.locked()


def test_placement_failure_logs_board_input_attempt_before_backend_error(monkeypatch) -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = _service(game, backend)
    tap = service._tap_queen_blood_confirm_or_button

    def late_error(button, milliseconds):
        tap(button, milliseconds)
        if button == "down":
            raise SafetyError("board input receipt lost")

    monkeypatch.setattr(service, "_tap_queen_blood_confirm_or_button", late_error)
    with pytest.raises(SafetyError, match="receipt lost") as caught:
        service.place_queens_blood_card(0, 1, 0)

    details = caught.value.details
    assert details["stage"] == "board_navigation"
    assert details["placement_steps"][0]["observed"] == [0, 0]
    assert details["observations"][-1]["cursor_cell"] == [1, 0]
    assert [item["reason"] for item in details["inputs"]] == [
        "pick_up_card", "move_placement", "cancel_observed_held_card",
    ]
    assert details["inputs_are_attempts"] is True
    assert details["confirm_attempted"] is False
    assert not game.cards
    assert [a[1] for a in backend.actions if a[0] == "tap"] == ["confirm", "down", "queen_blood_cancel"]


def test_placement_admission_failure_reports_zero_input() -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    with pytest.raises(SafetyError, match="below card cost") as caught:
        _service(game, backend).place_queens_blood_card(1, 0, 0)

    assert caught.value.details["stage"] == "admission"
    assert caught.value.details["inputs"] == []
    assert caught.value.details["card_id"] == "card_id_004"
    assert not any(action[0] == "tap" for action in backend.actions)


def test_place_card_reconciles_delayed_board_after_confirm() -> None:
    # Issue #264: the Confirm landed, but the probe missed the board and
    # placement state for an entire bounded observation window.
    from dataclasses import replace

    backend = FakeRealInput()
    game = ReactiveGame(backend)
    observe = game.observe
    missed = 0

    def delayed():
        nonlocal missed
        state = observe()
        if game.cards and missed < 605:
            missed += 1
            return replace(state, queen_blood=None)
        return state

    game.observe = delayed
    result = _service(game, backend).place_queens_blood_card(0, 1, 0)

    assert missed == 605
    assert result["ok"] is True and result["verified"] is True
    assert result["post_state"]["target_cell"]["card_id"] == "card_id_000"
    assert [a[1] for a in backend.actions if a[0] == "tap"] == ["confirm", "down", "confirm"]


def test_place_card_does_not_claim_another_card_at_target() -> None:
    # An occupied target is not proof that the *selected* card landed.
    from copy import deepcopy
    from dataclasses import replace

    backend = FakeRealInput()
    game = ReactiveGame(backend)
    observe = game.observe

    def mismatched():
        state = observe()
        if not game.cards:
            return state
        payload = deepcopy(state.queen_blood)
        cell = next(c for c in payload["board"]["cells"] if (c["row"], c["column"]) == (1, 0))
        cell["card_id"] = "card_id_999"
        return replace(state, queen_blood=payload)

    game.observe = mismatched
    result = _service(game, backend).place_queens_blood_card(0, 1, 0)

    assert result["ok"] is False and result["verified"] is False
    assert result["outcome"] == "placement_unverified" and result["retry_safe"] is False
    assert result["post_state"]["target_cell"]["card_id"] == "card_id_999"
    assert [a[1] for a in backend.actions if a[0] == "tap"] == ["confirm", "down", "confirm"]


def test_place_card_never_retries_or_cancels_when_post_confirm_telemetry_disappears() -> None:
    from dataclasses import replace

    backend = FakeRealInput()
    game = ReactiveGame(backend)
    observe = game.observe

    def missing():
        state = observe()
        return replace(state, queen_blood=None) if game.cards else state

    game.observe = missing
    result = _service(game, backend).place_queens_blood_card(0, 1, 0)

    assert game.cards == {(1, 0): "card_id_000"}
    assert result["ok"] is False and result["outcome"] == "placement_unverified"
    assert result["post_state"]["target_cell"] is None
    assert [a[1] for a in backend.actions if a[0] == "tap"] == ["confirm", "down", "confirm"]


def test_place_card_retries_dropped_confirm_only_with_a_fresh_held_preview() -> None:
    backend = FakeRealInput()
    # Pick up, Down, first place Confirm (lost), then a verified held preview
    # makes a second Confirm safe.
    game = ReactiveGame(backend, drop={3})
    result = _service(game, backend).place_queens_blood_card(0, 1, 0)

    assert result["ok"] is True and result["verified"] is True
    assert game.cards == {(1, 0): "card_id_000"}
    assert [a[1] for a in backend.actions if a[0] == "tap"] == [
        "confirm", "down", "confirm", "confirm",
    ]


@pytest.mark.parametrize("change", ["missing_card", "different_card", "different_turn"])
def test_place_card_does_not_retry_contradictory_held_card_evidence(change: str) -> None:
    from copy import deepcopy
    from dataclasses import replace

    backend = FakeRealInput()
    game = ReactiveGame(backend, drop={2})
    observe = game.observe

    def contradictory():
        state = observe()
        if game.press_count < 2:
            return state
        payload = deepcopy(state.queen_blood)
        if change == "missing_card":
            payload["hand"] = [item for item in payload["hand"] if item["hand_index"] != 0]
        elif change == "different_card":
            payload["hand"][0]["card_id"] = "card_id_999"
        else:
            payload["turn"] += 1
        return replace(state, queen_blood=payload)

    game.observe = contradictory
    result = _service(game, backend).place_queens_blood_card(0, 0, 0)

    assert result["ok"] is result["verified"] is False
    assert result["outcome"] == "placement_unverified"
    assert game.cards == {}
    assert game.placing is True
    assert [a[1] for a in backend.actions if a[0] == "tap"] == ["confirm", "confirm"]


def test_place_card_reconciles_a_confirm_error_after_the_game_accepted_it(monkeypatch) -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = _service(game, backend)
    confirm = service._tap_queen_blood_confirm
    presses = 0

    def late_error(milliseconds):
        nonlocal presses
        presses += 1
        confirm(milliseconds)
        if presses == 2:
            raise SafetyError("confirm delivery receipt lost")

    monkeypatch.setattr(service, "_tap_queen_blood_confirm", late_error)
    result = service.place_queens_blood_card(0, 1, 0)

    assert result["ok"] is True and result["outcome"] == "card_placed"
    assert result["post_sequence"] > result["pre_sequence"]
    assert game.cards == {(1, 0): "card_id_000"}
    assert [a[1] for a in backend.actions if a[0] == "tap"] == ["confirm", "down", "confirm"]


def test_place_card_does_not_cancel_or_replay_a_confirm_with_unknown_outcome(monkeypatch) -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = _service(game, backend)
    confirm = service._tap_queen_blood_confirm
    presses = 0

    def rejected_confirm(milliseconds):
        nonlocal presses
        presses += 1
        if presses == 2:
            raise SafetyError("confirm result unavailable")
        confirm(milliseconds)

    monkeypatch.setattr(service, "_tap_queen_blood_confirm", rejected_confirm)
    result = service.place_queens_blood_card(0, 1, 0)

    assert result["ok"] is False and result["outcome"] == "placement_unverified"
    assert result["requires_fresh_observation"] is True
    assert result["post_hand_card_count"] == 1
    assert game.cards == {} and game.placing is True
    assert [a[1] for a in backend.actions if a[0] == "tap"] == ["confirm", "down"]


@pytest.mark.parametrize("changed_field,changed_value", [("card_id", None), ("owner", "opponent")])
def test_confirm_error_reconciliation_requires_identified_player_card(monkeypatch, changed_field, changed_value):
    from dataclasses import replace

    backend = FakeRealInput()
    game = ReactiveGame(backend)
    original_observe = game.observe

    def ambiguous_observe():
        observed = original_observe()
        if not game.cards:
            return observed
        payload = dict(observed.queen_blood)
        board = dict(payload["board"])
        board["cells"] = [dict(cell) for cell in board["cells"]]
        for cell in board["cells"]:
            if cell["row"] == 1 and cell["column"] == 0:
                cell[changed_field] = changed_value
        payload["board"] = board
        return replace(observed, queen_blood=payload)

    game.observe = ambiguous_observe
    service = _service(game, backend)
    confirm = service._tap_queen_blood_confirm
    presses = 0

    def late_error(milliseconds):
        nonlocal presses
        presses += 1
        confirm(milliseconds)
        if presses == 2:
            raise SafetyError("confirm delivery receipt lost")

    monkeypatch.setattr(service, "_tap_queen_blood_confirm", late_error)
    result = service.place_queens_blood_card(0, 1, 0)

    assert result["verified"] is False
    assert result["retry_safe"] is False
    assert result["requires_fresh_observation"] is True
    assert [a[1] for a in backend.actions if a[0] == "tap"] == ["confirm", "down", "confirm"]


def test_place_card_stops_on_conflicting_board_and_placement_telemetry() -> None:
    from dataclasses import replace

    backend = FakeRealInput()
    game = ReactiveGame(backend)
    original_observe = game.observe

    def conflicting_observe():
        observed = original_observe()
        if not game.cards:
            return observed
        payload = dict(observed.queen_blood)
        board = dict(payload["board"])
        board["placement"] = {"active": True, "target_row": 1, "target_column": 0,
                              "cursor_row": 1, "cursor_column": 0}
        payload["board"] = board
        return replace(observed, queen_blood=payload)

    game.observe = conflicting_observe
    service = _service(game, backend)
    service._monotonic = StepClock()
    service._last_action_finished = 0.0

    result = service.place_queens_blood_card(0, 1, 0)

    assert result["ok"] is False and result["outcome"] == "placement_unverified"
    assert game.cards == {(1, 0): "card_id_000"}
    assert [a[1] for a in backend.actions if a[0] == "tap"] == ["confirm", "down", "confirm"]


def test_place_card_recovers_from_a_dropped_board_press() -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend, drop={2})  # the first "down" is lost
    service = _service(game, backend)

    result = service.place_queens_blood_card(0, 1, 0)

    assert result["verified"] is True
    assert game.cards == {(1, 0): "card_id_000"}
    assert [a[1] for a in backend.actions if a[0] == "tap"] == ["confirm", "down", "down", "confirm"]


def test_place_card_replans_the_hand_from_where_the_cursor_is_seen() -> None:
    pawns = dict(START_PAWNS) | {(1, 0): ("player", 2)}
    backend = FakeRealInput()
    game = ReactiveGame(backend, pawns=pawns, drop={1})  # the first "right" is lost
    service = _service(game, backend)

    service.place_queens_blood_card(1, 1, 0)

    assert game.cards == {(1, 0): "card_id_004"}
    assert [a[1] for a in backend.actions if a[0] == "tap"] == [
        "queen_blood_right", "queen_blood_right", "confirm", "down", "confirm"]


def test_automated_play_is_deprecated_and_refused_without_the_override() -> None:
    # Owner, 2026-09-29: agents play with their own decisions, not queen_blood_ai.
    backend = FakeRealInput()
    service = _service(ReactiveGame(backend), backend)

    with pytest.raises(SafetyError, match="deprecated: play Queen's Blood with your own decisions"):
        service.play_queens_blood_match(timeout_seconds=60, mulligan=False)
    with pytest.raises(SafetyError, match="deprecated"):
        service.play_queens_blood_turn()
    assert backend.actions == []


def test_place_card_rejects_a_cell_the_card_cannot_afford_before_input() -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = _service(game, backend)

    with pytest.raises(SafetyError, match="rank 1 is below card cost 2"):
        service.place_queens_blood_card(1, 0, 0)
    assert backend.actions == []


def test_pass_turn_holds_the_play_key_and_verifies_the_turn_ended() -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = _service(game, backend)

    result = service.pass_queens_blood_turn()

    assert result["verified"] is True
    assert [a for a in backend.actions if a[0] == "tap"] == [("tap", "queen_blood_play", 2200)]


def test_match_loop_places_then_passes_to_close_out_the_lead() -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = _service(game, backend)

    result = service.play_queens_blood_match(timeout_seconds=60, mulligan=False, allow_automated_decisions=True)

    assert result["outcome"] == "match_finished"
    assert result["won"] is True
    assert result["score"] == {"player": 1, "opponent": 0, "source": "last_turn_board_rules_model"}
    assert result["left_result_screen"] is True
    assert game.phase == "closed"
    assert [action["kind"] for action in result["actions"]] == ["place", "pass"]
    assert result["actions"][1]["opponent_passed"] is True
    assert len(game.cards) == 1


class _Lease:
    def __init__(self, active: bool) -> None:
        self.active = active

    def status(self):
        return {"active": self.active}


@pytest.mark.parametrize(("session_active", "expected"), [(True, False), (False, True)])
def test_match_loop_treats_only_a_closed_session_stop_as_an_emergency(session_active, expected) -> None:
    # Every real action latches the stop broker; an open control session
    # re-arms the next action, while stop_all_input closes the session.
    backend = FakeRealInput()
    service = AgentService(
        ReactiveGame(backend), backend,
        arm_state=FakeArm(stopped=True), environment=FakeEnvironment(),
        session_lease=_Lease(session_active), minimum_action_delay=0.0,
        sleeper=lambda _seconds: None,
    )
    assert service._queen_blood_stop_latched() is expected


@pytest.mark.parametrize("cursor_widget", [True, False])
def test_place_card_when_the_cursor_starts_on_an_unusable_cell(cursor_widget: bool) -> None:
    # Live 2026-09-25: the placement cursor started on an occupied cell the
    # held card could not use, where the preview gives no target.
    backend = FakeRealInput()
    game = ReactiveGame(backend, start=(1, 3), cursor_widget=cursor_widget)
    service = _service(game, backend)

    result = service.place_queens_blood_card(0, 2, 0)

    assert result["verified"] is True
    assert game.cards == {(2, 0): "card_id_000"}
    buttons = [a[1] for a in backend.actions if a[0] == "tap"]
    if cursor_widget:
        assert buttons == ["confirm", "down", "queen_blood_left", "queen_blood_left",
                           "queen_blood_left", "confirm"]
    else:
        assert buttons[:9] == ["confirm", "up", "up", "up", "queen_blood_left", "queen_blood_left",
                               "queen_blood_left", "queen_blood_left", "queen_blood_left"]
        assert buttons[9:] == ["down", "down", "confirm"]


def test_place_card_crosses_unlit_cells_by_tracking_its_estimate() -> None:
    # Live 2026-09-25: the cursor moves freely over every cell, but only lit
    # cells report it; crossing unlit cells must not trigger a re-home.
    pawns = dict(START_PAWNS) | {(0, 1): ("player", 1)}
    backend = FakeRealInput()
    game = ReactiveGame(backend, start=(0, 1), cursor_widget=False, pawns=pawns)
    service = _service(game, backend)

    result = service.place_queens_blood_card(0, 2, 0)

    assert game.cards == {(2, 0): "card_id_000"}
    assert [a[1] for a in backend.actions if a[0] == "tap"] == [
        "confirm", "down", "down", "queen_blood_left", "confirm"]
    assert [step["observed"] for step in result["placement_steps"]] == [[0, 1], [2, 0]]


def test_place_card_rehomes_when_a_dropped_press_hides_the_cursor() -> None:
    pawns = dict(START_PAWNS) | {(0, 1): ("player", 1)}
    backend = FakeRealInput()
    # Press 4 is the "left": the cursor stays on unlit (2, 1) while the
    # estimate says it reached the target (2, 0).
    game = ReactiveGame(backend, start=(0, 1), cursor_widget=False, pawns=pawns, drop={4})
    service = _service(game, backend)

    service.place_queens_blood_card(0, 2, 0)

    assert game.cards == {(2, 0): "card_id_000"}
    buttons = [a[1] for a in backend.actions if a[0] == "tap"]
    assert buttons[:4] == ["confirm", "down", "down", "queen_blood_left"]
    assert buttons[4:12] == ["up"] * 3 + ["queen_blood_left"] * 5


def test_match_loop_remembers_the_opponents_cards(tmp_path) -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    opponents = tmp_path / "queen-blood-opponents.json"
    opponents.write_text('{"schema_version": 1, "opponents": {"turner": {"card_ids": ["card_id_072"]}}}',
                         encoding="utf-8")
    service = AgentService(
        game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
        queen_blood_opponents_file=opponents,
    )
    service._load_queen_blood_opponent_cards("turner")
    service._save_queen_blood_opponent_cards("turner", ["card_id_072", "card_id_016"])
    assert service._load_queen_blood_opponent_cards("turner") == ["card_id_072", "card_id_016"]
    assert service._load_queen_blood_opponent_cards("someone else") == []


class FakePadInput(FakeRealInput):
    """A backend with virtual-pad buttons; pad presses reach the fake game."""

    def __init__(self):
        super().__init__()
        self.pad_presses = []

    def tap_gamepad(self, button, milliseconds):
        self.pad_presses.append(button)
        self.actions.append(("tap", button, milliseconds))


def test_background_mode_plays_a_match_on_the_pad_without_focus() -> None:
    # Live 2026-09-25: the virtual pad reaches the game while another window
    # has focus, so no press may require (or take) the foreground.
    backend = FakePadInput()
    game = ReactiveGame(backend)
    service = AgentService(
        game, backend, arm_state=FakeArm(), environment=FakeEnvironment(foreground=False),
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
        queen_blood_background_input=True,
    )

    result = service.play_queens_blood_match(timeout_seconds=60, mulligan=False, allow_automated_decisions=True)

    assert result["outcome"] == "match_finished" and result["won"] is True
    assert result["left_result_screen"] is True
    keyboard = [a for a in backend.actions if a[0] == "tap" and a[1] not in backend.pad_presses]
    assert keyboard == []
    assert backend.pad_presses[0] == "confirm" and "queen_blood_play" in backend.pad_presses


def test_background_mode_uses_the_keyboard_while_the_game_has_focus() -> None:
    # Pad taps are polled and were lost live; focused, keys are sent instead.
    backend = FakePadInput()
    game = ReactiveGame(backend)
    service = AgentService(
        game, backend, arm_state=FakeArm(), environment=FakeEnvironment(foreground=True),
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
        queen_blood_background_input=True,
    )

    service.place_queens_blood_card(0, 1, 0)

    assert game.cards == {(1, 0): "card_id_000"}
    assert backend.pad_presses == []


def test_without_background_mode_an_unfocused_game_is_refused() -> None:
    backend = FakePadInput()
    game = ReactiveGame(backend)
    service = AgentService(
        game, backend, arm_state=FakeArm(), environment=FakeEnvironment(foreground=False),
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
    )
    with pytest.raises(SafetyError, match="foreground"):
        service.place_queens_blood_card(0, 1, 0)
    assert backend.pad_presses == []


def test_pad_play_is_sent_as_a_hold() -> None:
    # Live 2026-09-25: a 500 ms Y tap left the mulligan open; 1.5 s submitted it.
    backend = FakePadInput()
    service = AgentService(
        ReactiveGame(backend), backend, arm_state=FakeArm(),
        environment=FakeEnvironment(foreground=False), minimum_action_delay=0.0,
        sleeper=lambda _seconds: None, queen_blood_background_input=True,
    )
    service._queen_blood_press("queen_blood_play", 500)
    service._queen_blood_press("confirm", 400)
    assert backend.actions[-2:] == [("tap", "queen_blood_play", 1500), ("tap", "confirm", 400)]


def test_background_mode_refuses_keyboard_only_dialogue_controls() -> None:
    class BackgroundPadInput(FakePadInput):
        supports_background_input = True

    backend = BackgroundPadInput()
    environment = FakeEnvironment(foreground=False)
    environment.focus_game_window = lambda: pytest.fail("background input must not focus the game")
    service = AgentService(
        ReactiveGame(backend), backend, arm_state=FakeArm(), environment=environment,
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
    )

    with pytest.raises(SafetyError, match="requires keyboard input.*background mode"):
        service._queen_blood_press("confirm", 400, keyboard=True)

    service._ensure_game_foreground()
    assert backend.actions == []
    assert backend.pad_presses == []


def test_learned_geometry_locates_the_cursor_on_unusable_cells_without_homing() -> None:
    # The held card's location maps to its cell anywhere on the board, so a
    # start on an unusable cell needs no trip to the corner (live, those trips
    # were the back-and-forth seen on screen).
    backend = FakeRealInput()
    game = ReactiveGame(backend, start=(1, 3), cursor_widget=False, locations=True)
    service = _service(game, backend)
    for cell in [(0, 0), (1, 0), (2, 1)]:
        service._queen_blood_geometry.observe("hover", cell, cell_centre(*cell))

    result = service.place_queens_blood_card(0, 2, 0)

    assert game.cards == {(2, 0): "card_id_000"}
    assert [a[1] for a in backend.actions if a[0] == "tap"] == [
        "confirm", "down", "queen_blood_left", "queen_blood_left", "queen_blood_left", "confirm"]
    assert result["placement_steps"][0]["observed"] == [1, 3]


def test_held_card_over_a_confirmed_cell_teaches_the_geometry() -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend, locations=True)
    service = _service(game, backend)
    service.place_queens_blood_card(0, 2, 0)
    samples = service._queen_blood_geometry._samples["hover"]
    # (1, 0) is crossed mid-burst, between reads.
    assert set(samples) == {(0, 0), (2, 0)}


def test_match_loop_models_a_known_opponent_with_its_datatable_deck(monkeypatch) -> None:
    # A first match against Ned: nothing seen yet, so his DeckID deck
    # (deck_en_00_0000) models his replies instead of generic profiles.
    import ff7r_agent.service as service_module

    seen: list[list[str]] = []
    original = service_module.choose_queen_blood_action

    def spy(state, **kwargs):
        seen.append([card.card_id for card in kwargs.get("opponent_cards") or [] if card is not None])
        return original(state, **kwargs)

    monkeypatch.setattr(service_module, "choose_queen_blood_action", spy)
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = _service(game, backend)

    result = service.play_queens_blood_match(timeout_seconds=60, mulligan=False, opponent="Ned",
                                           allow_automated_decisions=True)

    assert seen and seen[0] == ["card_id_000", "card_id_002", "card_id_006", "card_id_014",
                                "card_id_012", "card_id_004", "card_id_008", "card_id_010"]
    assert result["opponent_deck"]["deck_id"] == "deck_en_00_0000"
    assert result["opponent_deck"]["seen_outside_deck"] == []


def test_finish_records_a_hand_played_win_for_the_overlay(tmp_path) -> None:
    # The stream plays with the agent's own moves: the result screen is
    # scored from its board and the win reaches the public stats.
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = AgentService(
        game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
        public_stream_event_dir=tmp_path / "events",
    )
    service._queen_blood_match = {"opponent": "Nene", "started": service._monotonic() - 90.0}
    service.place_queens_blood_card(0, 1, 0)
    service.pass_queens_blood_turn()  # the fake opponent passes too: result screen

    result = service.finish_queens_blood_match()

    assert result["outcome"] == "match_finished" and result["won"] is True
    assert result["score"]["player"] == 1 and result["score"]["opponent"] == 0
    assert result["opponent"] == "Nene" and result["elapsed_seconds"] >= 90
    assert result["left_result_screen"] is True and game.phase == "closed"
    records = list((tmp_path / "public-qb-results").glob("*.json"))
    assert [json.loads(item.read_text())["result"] for item in records] == ["win"]
    assert json.loads((tmp_path / "public-qb-current.json").read_text())["active"] is False


def test_finish_persists_the_result_before_closing_the_screen(tmp_path) -> None:
    # Live 2026-10-12 (#325): a slow close outlasted the bridge's 25 s request
    # timeout, the worker was killed after the game left the result screen,
    # and the win was never recorded. The result is now saved first and the
    # close is bounded inside the request timeout.
    from ff7r_agent.runtime_bridge import DEFAULT_RUNTIME_REQUEST_TIMEOUT_SECONDS

    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = AgentService(
        game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
        public_stream_event_dir=tmp_path / "events",
    )
    service._queen_blood_match = {"opponent": "Nene", "started": service._monotonic() - 90.0}
    service.place_queens_blood_card(0, 1, 0)
    service.pass_queens_blood_turn()
    seen = {}

    def worker_killed_during_close(deadline):
        seen["budget"] = deadline - service._monotonic()
        seen["results"] = [json.loads(item.read_text())["result"]
                           for item in (tmp_path / "public-qb-results").glob("*.json")]
        seen["marker_active"] = json.loads((tmp_path / "public-qb-current.json").read_text())["active"]
        seen["last_action"] = dict(service._last_action)
        seen["match"] = service._queen_blood_match
        raise RuntimeError("runtime worker request timed out")

    service._close_queen_blood_result = worker_killed_during_close
    with pytest.raises(RuntimeError):
        service.finish_queens_blood_match()

    assert seen["results"] == ["win"] and seen["marker_active"] is False
    assert seen["last_action"]["action"] == "finish_queens_blood_match" and seen["last_action"]["won"] is True
    assert seen["last_action"]["left_result_screen"] is None and seen["match"] is None
    assert 0 < seen["budget"] < DEFAULT_RUNTIME_REQUEST_TIMEOUT_SECONDS - 5


def test_finish_reports_the_close_on_the_recorded_action(tmp_path) -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    game.phase, game.cards = "result", {(1, 0): "card_id_000"}
    service = _service(game, backend)
    result = service.finish_queens_blood_match()
    assert result["left_result_screen"] is True and game.phase == "closed"
    assert service._last_action["left_result_screen"] is True
    assert service._last_action["result_close_inputs"] == result["result_close_inputs"]


@pytest.mark.parametrize("owner", ["unknown", "empty"])
def test_finish_keeps_unscorable_result_open_and_can_retry(tmp_path, owner) -> None:
    from dataclasses import replace

    backend = FakeRealInput()
    game = ReactiveGame(backend)
    game.phase, game.cards = "result", {(1, 0): "card_id_000"}
    observe = game.observe
    ownership_missing = True

    def incomplete_board():
        state = observe()
        if ownership_missing:
            payload = state.queen_blood
            for cell in payload["board"]["cells"]:
                if cell.get("card_present"):
                    cell["owner"] = owner
            return replace(state, queen_blood=payload)
        return state

    game.observe = incomplete_board
    service = AgentService(
        game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
        public_stream_event_dir=tmp_path / "events",
    )
    match = {"opponent": "Nene", "started": service._monotonic() - 90.0}
    service._queen_blood_match = match

    result = service.finish_queens_blood_match()

    assert result["ok"] is False and result["outcome"] == "result_unscored"
    assert result["score"] is None and result["won"] is None
    assert result["reason"] == "card_ownership_unavailable"
    assert result["unresolved_cells"] == [
        {"row": 1, "column": 0, "card_id": "card_id_000", "owner": owner},
    ]
    assert result["requires_fresh_observation"] is True
    assert result["left_result_screen"] is False
    assert game.phase == "result" and backend.actions == []
    assert service._queen_blood_match is match
    assert not list((tmp_path / "public-qb-results").glob("*.json"))

    ownership_missing = False
    retry = service.finish_queens_blood_match()
    assert retry["ok"] is True and retry["score"]["player"] == 1
    assert retry["score"]["opponent"] == 0 and retry["won"] is True
    assert retry["left_result_screen"] is True and game.phase == "closed"
    assert service._queen_blood_match is None
    records = list((tmp_path / "public-qb-results").glob("*.json"))
    assert len(records) == 1 and json.loads(records[0].read_text())["result"] == "win"


def test_finishing_a_match_clears_the_commentary_baseline(tmp_path) -> None:
    # The next match's first turn must not react to this match's board (#326).
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    game.phase, game.cards = "result", {(1, 0): "card_id_000"}
    service = _service(game, backend)
    service._queen_blood_commentary_baseline = {"phase": "opponent_turn", "board": {}}
    service.finish_queens_blood_match(close_result=False)
    assert service._queen_blood_commentary_baseline is None


class ReplyInPlacementReadGame(ReactiveGame):
    """The opponent's reply lands before the placement's last read (#357)."""

    REPLY = {"row": 1, "column": 3, "owner": "opponent", "rank": 0, "card_present": True,
             "card_id": "card_id_006", "power": 5, "highlight": False}

    def __init__(self, backend):
        super().__init__(backend)
        self.replied = False

    def observe(self):
        state = super().observe()
        queen_blood = state.queen_blood
        if not self.cards:
            return state
        cells = [self.REPLY if (cell["row"], cell["column"]) == (1, 3) else cell
                 for cell in queen_blood["board"]["cells"]]
        phase = queen_blood["phase"] if self.replied else "opponent_turn"
        self.replied = True
        return replace(state, queen_blood=dict(
            queen_blood, phase=phase, board=dict(queen_blood["board"], cells=cells)))


def test_a_reply_read_with_our_placement_is_reported_as_played_not_passed() -> None:
    backend = FakeRealInput()
    game = ReplyInPlacementReadGame(backend)
    service = _service(game, backend)
    service.observe_queens_blood()

    placed = service.place_queens_blood_card(0, 2, 0)
    assert placed["verified"] is True and placed["queen_blood"]["phase"] == "opponent_turn"
    commentary = service.observe_queens_blood()["commentary"]

    kinds = [cue["kind"] for cue in commentary["cues"]]
    assert "opponent_passed" not in kinds
    assert [cue["card_id"] for cue in commentary["cues"] if cue["kind"] == "opponent_played"] == ["card_id_006"]


def test_finish_refuses_before_the_result_screen() -> None:
    backend = FakeRealInput()
    service = _service(ReactiveGame(backend), backend)
    with pytest.raises(SafetyError, match=r"result screen is not shown \(phase="):
        service.finish_queens_blood_match()
    assert backend.actions == []


def attach_probe_request(game, tmp_path):
    probe = JsonFileTelemetry(tmp_path / "state.json")
    game.request_queen_blood_probe = probe.request_queen_blood_probe
    game.release_queen_blood_probe = probe.release_queen_blood_probe
    return probe.queen_blood_request_path


@pytest.mark.parametrize("close_result", [True, False])
def test_finish_releases_the_real_request_only_when_result_is_closed(tmp_path, close_result):
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    game.phase, game.cards = "result", {(1, 0): "card_id_000"}
    request = attach_probe_request(game, tmp_path)
    result = invoke_runtime(_service(game, backend), None, None,
                            "finish_queens_blood_match", {"close_result": close_result})

    assert result["outcome"] == "match_finished" and result["won"] is True
    assert result["probe_released"] is (True if close_result else None)
    assert (request.read_text(encoding="ascii") == "0\n") is close_result


@pytest.mark.parametrize("transition", ["stuck_result", "missing_telemetry", "invalid_telemetry", "another_card_screen"])
def test_finish_keeps_request_when_result_dismissal_is_unconfirmed(tmp_path, transition):
    from dataclasses import replace
    backend = FakeRealInput()
    game = ReactiveGame(backend, drop=set(range(1, 10)) if transition == "stuck_result" else None)
    game.phase, game.cards = "result", {(1, 0): "card_id_000"}
    request = attach_probe_request(game, tmp_path)
    observe = game.observe

    def after_close():
        state = observe()
        if game.phase == "closed":
            payload = None if transition == "missing_telemetry" else _queen_blood(active=True, phase="mulligan")
            if transition == "invalid_telemetry":
                payload = _queen_blood(schema_version=999)
            return replace(state, queen_blood=payload, in_menu=True)
        return state

    game.observe = after_close
    service = _service(game, backend)
    service._monotonic = StepClock()
    result = invoke_runtime(service, None, None, "finish_queens_blood_match")

    assert result["probe_released"] is None
    assert int(request.read_text(encoding="ascii")) > 0
    assert result["left_result_screen"] is False


@pytest.mark.parametrize("missing_payload", [True, False])
def test_finish_waits_out_missing_result_sample_before_confirm(tmp_path, missing_payload):
    from dataclasses import replace
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    game.phase, game.cards = "result", {(1, 0): "card_id_000"}
    request = attach_probe_request(game, tmp_path)
    observe = game.observe
    reads = 0

    def gap_before_confirm():
        nonlocal reads
        reads += 1
        state = observe()
        if reads in (2, 3, 4):
            payload = None if missing_payload else _queen_blood(active=False, phase="inactive")
            return replace(state, queen_blood=payload, map_id=None, player=None)
        return state

    game.observe = gap_before_confirm
    service = _service(game, backend)
    service._monotonic = StepClock()
    result = invoke_runtime(service, None, None, "finish_queens_blood_match")

    assert result["left_result_screen"] is True and result["probe_released"] is True
    assert [a[1] for a in backend.actions if a[0] == "tap"] == ["confirm"]
    assert result["result_close_inputs"][0]["sequence"] == 15
    assert result["result_exit_sequence"] >= 17
    assert request.read_text(encoding="ascii") == "0\n"


def test_finish_does_not_claim_exit_or_send_confirm_through_missing_result(tmp_path):
    from dataclasses import replace
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    game.phase, game.cards = "result", {(1, 0): "card_id_000"}
    request = attach_probe_request(game, tmp_path)
    observe = game.observe
    reads = 0

    def missing_after_result():
        nonlocal reads
        reads += 1
        state = observe()
        return state if reads == 1 else replace(state, queen_blood=None, map_id=None, player=None)

    game.observe = missing_after_result
    service = _service(game, backend)
    service._monotonic = StepClock()
    result = invoke_runtime(service, None, None, "finish_queens_blood_match")

    assert result["won"] is True
    assert result["score"]["player"] == 1 and result["score"]["opponent"] == 0
    assert result["left_result_screen"] is False and result["probe_released"] is None
    assert result["result_close_inputs"] == [] and result["result_exit_sequence"] is None
    assert not any(action[0] == "tap" for action in backend.actions)
    assert int(request.read_text(encoding="ascii")) > 0


def test_result_exit_requires_advancing_field_samples():
    from dataclasses import replace
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    game.phase = "closed"
    observe = game.observe
    game.observe = lambda: replace(observe(), sequence=99)
    service = _service(game, backend)
    service._monotonic = StepClock()
    left, _state, inputs = service._close_queen_blood_result(service._monotonic() + 5.0)
    assert left is False and inputs == []


def test_finish_releases_after_blank_transition_resolves_to_loaded_field(tmp_path):
    from dataclasses import replace
    backend = FakeRealInput(); game = ReactiveGame(backend)
    game.phase, game.cards = "result", {(1, 0): "card_id_000"}
    request = attach_probe_request(game, tmp_path)
    observe = game.observe; closed_reads = 0
    def transition():
        nonlocal closed_reads
        state = observe()
        if game.phase == "closed":
            closed_reads += 1
            return replace(state, queen_blood=None, player=None if closed_reads < 3 else state.player)
        return state
    game.observe = transition
    result = invoke_runtime(_service(game, backend), None, None, "finish_queens_blood_match")
    assert result["left_result_screen"] and result["probe_released"] is True
    assert closed_reads >= 3 and request.read_text(encoding="ascii") == "0\n"


def test_finish_reports_release_failure_without_losing_the_match_result(tmp_path):
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    game.phase, game.cards = "result", {(1, 0): "card_id_000"}
    request = attach_probe_request(game, tmp_path)
    game.release_queen_blood_probe = lambda: False
    result = invoke_runtime(_service(game, backend), None, None, "finish_queens_blood_match")

    assert result["outcome"] == "match_finished" and result["won"] is True
    assert result["left_result_screen"] is True and result["probe_released"] is False
    assert int(request.read_text(encoding="ascii")) > 0


@pytest.mark.parametrize("leave_result", [True, False])
def test_autonomous_match_releases_request_after_result_closes(tmp_path, leave_result):
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    request = attach_probe_request(game, tmp_path)
    result = invoke_runtime(_service(game, backend), None, None, "play_queens_blood_match", {
        "timeout_seconds": 60, "mulligan": False, "leave_result": leave_result,
        "allow_automated_decisions": True,
    })

    assert result["outcome"] == "match_finished"
    assert result["probe_released"] is (True if leave_result else None)
    assert (request.read_text(encoding="ascii") == "0\n") is leave_result


def test_finish_reads_past_samples_that_miss_the_result_screen(tmp_path) -> None:
    # Live 2026-10-04 (Rolf): an observation showed the Defeat screen, and
    # the finish called straight after refused it on a single sample.
    from dataclasses import replace

    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = AgentService(
        game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
        public_stream_event_dir=tmp_path / "events",
    )
    service._queen_blood_match = {"opponent": "Nene", "started": service._monotonic() - 90.0}
    service.place_queens_blood_card(0, 1, 0)
    service.pass_queens_blood_turn()
    observe, missed = game.observe, [2]

    def flickering():
        state = observe()
        if missed[0]:
            missed[0] -= 1
            return replace(state, queen_blood=None)
        return state

    game.observe = flickering
    result = service.finish_queens_blood_match()
    assert result["outcome"] == "match_finished" and result["won"] is True and missed == [0]


def test_only_catalogue_opponent_names_reach_the_overlay() -> None:
    from ff7r_agent.public_stream_events import public_opponent_name

    assert public_opponent_name("Nene") == "Nene"
    assert public_opponent_name("Turner") == "Turner"
    assert public_opponent_name("Npc9999_Anything") == "Unknown"
    assert public_opponent_name(None) == "Unknown"


def test_finish_after_a_worker_restart_takes_the_match_from_its_marker(tmp_path) -> None:
    import time as clock

    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = AgentService(
        game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
        public_stream_event_dir=tmp_path / "events",
    )
    started = clock.strftime("%Y-%m-%dT%H:%M:%S+00:00", clock.gmtime(clock.time() - 120))
    (tmp_path / "public-qb-current.json").write_text(
        json.dumps({"version": 1, "active": True, "opponent": "Nene", "at": started}), encoding="utf-8")
    service.place_queens_blood_card(0, 1, 0)
    service.pass_queens_blood_turn()

    result = service.finish_queens_blood_match()

    assert result["opponent"] == "Nene" and 110 <= result["elapsed_seconds"] <= 200
    assert len(list((tmp_path / "public-qb-results").glob("*.json"))) == 1


def test_finish_keeps_a_detailed_history_record(tmp_path) -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = AgentService(
        game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
        public_stream_event_dir=tmp_path / "events",
    )
    service._queen_blood_match = {"opponent": "Nene", "started": service._monotonic() - 60.0}
    service.place_queens_blood_card(0, 1, 0)
    service.pass_queens_blood_turn()
    service.finish_queens_blood_match()

    [path] = list((tmp_path / "qb-match-history").glob("*.json"))
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["opponent"] == "Nene" and record["result"] == "win"
    assert record["version"] == 2 and record["opponent_id"] == "oba1110_Actor_CardGamer02"
    assert record["driver"] is None  # no stream declared in this test
    assert record["margin"] == 1 and record["lanes"][1] == [1, 0]
    assert record["cards_played"] == 1
    assert record["player_cards"][0]["name"] == "Security Officer"


def test_finish_on_a_declared_stream_credits_the_declared_model(tmp_path) -> None:
    import time as clock

    stamp = lambda seconds: clock.strftime("%Y-%m-%dT%H:%M:%S.000Z", clock.gmtime(seconds))
    now = clock.time()
    run_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    (tmp_path / "public-driver.json").write_text(json.dumps({
        "version": 1, "driver": "ChatGPT", "modelId": "gpt-6-sol", "effort": "high", "runId": run_id,
        "streamStartedAt": stamp(now - 600), "declaredAt": stamp(now - 590)}), encoding="utf-8")
    (tmp_path / "public-stream-live.json").write_text(json.dumps({
        "version": 1, "streamStartedAt": stamp(now - 600), "at": stamp(now - 5)}), encoding="utf-8")
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = AgentService(
        game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
        public_stream_event_dir=tmp_path / "events",
    )
    service._queen_blood_match = {"opponent": "oba2610_Actor_CardGamer03", "started": service._monotonic() - 60.0}
    service.place_queens_blood_card(0, 1, 0)
    service.pass_queens_blood_turn()
    service.finish_queens_blood_match()

    [history] = list((tmp_path / "qb-match-history").glob("*.json"))
    record = json.loads(history.read_text(encoding="utf-8"))
    assert record["opponent"] == "Turner" and record["opponent_id"] == "oba2610_Actor_CardGamer03"
    assert record["driver"] == {"driver": "ChatGPT", "model_id": "gpt-6-sol", "effort": "high", "run_id": run_id}
    [result] = list((tmp_path / "public-qb-results").glob("*.json"))
    assert json.loads(result.read_text(encoding="utf-8"))["driver"]["modelId"] == "gpt-6-sol"


@pytest.mark.parametrize("stable_identity", [False, True])
def test_finish_during_a_rehearsal_records_only_to_the_rehearsal_folders(tmp_path, stable_identity) -> None:
    import time as clock

    stamp = lambda seconds: clock.strftime("%Y-%m-%dT%H:%M:%S.000Z", clock.gmtime(seconds))
    now = clock.time()
    run_id = "6a1e1f8c-2b7d-4c55-8a40-1e2f3a4b5c6d"
    (tmp_path / "public-driver-rehearsal.json").write_text(json.dumps({
        "version": 1, "driver": "ChatGPT", "modelId": "gpt-6-sol", "effort": "high", "runId": run_id,
        "streamStartedAt": stamp(now - 600), "declaredAt": stamp(now - 590),
        **({"recordingId": "a" * 64} if stable_identity else {})}), encoding="utf-8")
    (tmp_path / "public-stream-rehearsal.json").write_text(json.dumps({
        "version": 2 if stable_identity else 1, "at": stamp(now - 5), "recordingStartedAt": stamp(now - 600),
        **({"recordingId": "a" * 64} if stable_identity else {})}), encoding="utf-8")
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = AgentService(
        game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
        public_stream_event_dir=tmp_path / "events",
    )
    service._queen_blood_match = {"opponent": "oba2610_Actor_CardGamer03", "started": service._monotonic() - 60.0}
    service.place_queens_blood_card(0, 1, 0)
    service.pass_queens_blood_turn()
    service.finish_queens_blood_match()

    assert not (tmp_path / "qb-match-history").exists() and not (tmp_path / "public-qb-results").exists()
    [history] = list((tmp_path / "qb-match-history-rehearsal").glob("*.json"))
    assert json.loads(history.read_text(encoding="utf-8"))["driver"]["run_id"] == run_id
    [result] = list((tmp_path / "public-qb-results-rehearsal").glob("*.json"))
    assert json.loads(result.read_text(encoding="utf-8"))["driver"]["runId"] == run_id


def test_finish_profile_uses_the_harness_tracked_opponent_not_the_caller_label(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeRealInput()
    game = ReactiveGame(backend)
    service = AgentService(
        game, backend, arm_state=FakeArm(), environment=FakeEnvironment(),
        minimum_action_delay=0.0, sleeper=lambda _seconds: None,
        queen_blood_profile_root=tmp_path,
    )
    monkeypatch.setattr(
        service,
        "get_queens_blood_run_status",
        lambda: {"profile_id": "profile-a"},
    )
    service._queen_blood_match = {
        "opponent": "field_object:1110-KALMT:oba1110_Actor_CardGamer02",
        "started": service._monotonic() - 60.0,
    }
    service.place_queens_blood_card(0, 1, 0)
    service.pass_queens_blood_turn()

    result = service.finish_queens_blood_match(opponent="Ned")

    assert result["opponent"] == "Ned"
    data = json.loads(
        (tmp_path / "profiles" / "profile-a" / "opponents.json").read_text(encoding="utf-8")
    )
    tracked = data["opponents"]["field_object:1110-kalmt:oba1110_actor_cardgamer02"]
    assert "ned" not in data["opponents"]
    assert tracked["matches"][-1]["won"] is True
    assert tracked["matches"][-1]["source"] == "finish_queens_blood_match"
