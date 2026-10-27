from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace

import pytest

from ff7r_agent.models import Character, GameState, ObservedActor, Rotation, Vec3
from ff7r_agent.service import AgentService, SafetyError
from test_agent import FakeArm, FakeEnvironment, FakeRealInput


def _queen_blood(**overrides: object) -> dict:
    payload = {
        "schema_version": 1,
        "active": False,
        "phase": "inactive",
        "turn": 0,
        "match_id": None,
        "screen": None,
        "begin_match_available": False,
        "match_screen": False,
        "cursor": {"area": "unknown", "hand_index": None, "row": None, "column": None},
        "hand": [],
        "legal_moves": [],
        "board": {"rows": 3, "columns": 5, "cells": []},
    }
    payload.update(overrides)
    return payload


def _state(
    sequence: int,
    *,
    interaction: dict | None = None,
    queen_blood: dict | None = None,
    include_target: bool = True,
    map_id: str | None = "EndGame",
) -> GameState:
    return GameState(
        schema_version=1,
        sequence=sequence,
        timestamp_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        running=True,
        paused=False,
        in_menu=False,
        in_combat=False,
        map_id=map_id,
        location="Gold Saucer",
        objective="",
        player=Character(
            "Cloud", 100, 100, 10, 10,
            position=Vec3(0, 0, 0),
            rotation=Rotation(0, 0, 0),
            actor_id="CloudActor",
        ),
        nearby_actors=(
            ObservedActor(
                id="qb-npc",
                distance=1.0,
                position=Vec3(100, 0, 0),
                kind="npc",
                name="Queen's Blood NPC",
                usable=True,
                visible=True,
                prompt="Talk",
                opened=False,
            ),
        ) if include_target else (),
        source="test",
        interaction=interaction,
        queen_blood=(
            None
            if queen_blood is None
            else {**_queen_blood(), **queen_blood}
        ),
        nearby_entities_complete=True,
    )


class MatchTelemetry:
    def __init__(self, states: list[GameState]) -> None:
        self.states = states
        self.index = 0

    def observe(self) -> GameState:
        state = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        return state


def _service(
    telemetry: MatchTelemetry,
    backend: FakeRealInput,
    *,
    settled_gameplay_samples: int = 1,
) -> AgentService:
    return AgentService(
        telemetry,
        backend,
        arm_state=FakeArm(),
        environment=FakeEnvironment(),
        minimum_action_delay=0.0,
        settled_gameplay_samples=settled_gameplay_samples,
        sleeper=lambda _seconds: None,
    )


def test_paused_deck_select_can_resume_but_other_pauses_still_refuse():
    prematch = _state(1, queen_blood={"screen": "deck_select", "begin_match_available": True})
    prematch = replace(prematch, paused=True)
    service = _service(MatchTelemetry([prematch]), FakeRealInput())
    assert service._assert_action_allowed(
        allow_queen_blood_dialogue_transition=True) is prematch
    with pytest.raises(SafetyError, match="game is paused"):
        service._assert_action_allowed()

    other_menu = replace(prematch, queen_blood=_queen_blood())
    service.telemetry = MatchTelemetry([other_menu])
    with pytest.raises(SafetyError, match="game is paused"):
        service._assert_action_allowed(allow_queen_blood_dialogue_transition=True)


def test_initiate_queens_blood_match_interacts_confirms_and_starts() -> None:
    prematch = {
        "screen": "pre_match",
        "begin_match_available": True,
    }
    started = {
        "active": False,
        "phase": "inactive",
        "screen": "deck_select",
        "begin_match_available": True,
        "match_screen": True,
        "cursor": {"area": "hand", "hand_index": 0, "row": 0, "column": 0},
        "hand": [{"hand_index": 0, "card_id": "card-0", "name": "Card 0"}],
        "legal_moves": [],
    }
    telemetry = MatchTelemetry([
        _state(1, queen_blood={"active": False, "phase": "inactive"}),
        _state(2, interaction={"state": "dialogue", "target_id": "qb-npc"}),
        _state(3, interaction={"state": "dialogue", "target_id": "qb-npc"}),
        _state(4, interaction={"state": "dialogue", "target_id": "qb-npc"}),
        _state(5, interaction={"state": "menu"}, queen_blood=prematch),
        _state(6, interaction={"state": "menu"}, queen_blood=prematch),
        _state(7, interaction={"state": "menu"}, queen_blood=started),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    probe_requests = []
    def request_before_talk():
        assert backend.actions == []
        probe_requests.append(True)
    telemetry.request_queen_blood_dialogue = request_before_talk

    result = service.initiate_queens_blood_match(
        target_id="qb-npc",
        dialogue_confirmations=3,
    )

    assert result["ok"] is True
    assert result["outcome"] == "mulligan_ready"
    assert result["interaction_sent"] is True
    assert probe_requests == [True]
    handoff = next(item for item in result["trace"] if item.get("event") == "npc_interaction_handoff")
    assert handoff["input_sent"] is True
    assert set(handoff["phases_ms"]) == {"foreground", "readiness_and_ownership", "target_resolution_and_facing",
                                          "affordance_and_competitors", "final_foreground"}
    assert handoff["first_input_after_start_ms"] >= 0
    assert result["dialogue_confirmations_sent"] == 1
    assert result["queen_blood"]["phase"] == "dealing"
    assert result["queen_blood"]["screen"] == "mulligan"
    assert result["state_inference"] == "identified_hand_after_begin_match"
    observed = service.observe_queens_blood()
    assert observed["queen_blood"]["screen"] == "mulligan"
    assert observed["queen_blood"]["state_inference"] == {
        "state": "mulligan",
        "basis": "identified_hand_after_begin_match",
        "raw_screen": "deck_select",
        "sequence": 7,
    }
    assert [item[:2] for item in backend.actions] == [
        ("interaction", 1000),
        ("tap", "queen_blood_left"),
        ("tap", "confirm"),
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
    ]
    assert service._arm.stopped is True


def test_observe_queens_blood_can_restore_inference_after_worker_restart() -> None:
    raw = _queen_blood(
        active=False,
        phase="inactive",
        screen="deck_select",
        begin_match_available=True,
        match_screen=True,
        cursor={"area": "hand", "hand_index": 0},
        hand=[{"hand_index": 0, "card_id": "card-0", "name": "Card 0"}],
    )
    telemetry = MatchTelemetry([_state(9, queen_blood=raw), _state(10, queen_blood=raw)])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    before_recovery = service.observe_queens_blood()
    recovered = service.observe_queens_blood(infer_current_hand_is_mulligan=True)

    assert before_recovery["queen_blood"]["screen"] == "deck_select"
    assert recovered["queen_blood"]["screen"] == "mulligan"
    assert recovered["queen_blood"]["state_inference"] == {
        "state": "mulligan",
        "basis": "identified_hand_recovery",
        "raw_screen": "deck_select",
        "sequence": 10,
    }
    assert backend.actions == []


def test_initiate_queens_blood_match_returns_when_mulligan_hand_is_ready() -> None:
    prematch = _queen_blood(
        screen="deck_select",
        begin_match_available=True,
        match_screen=True,
    )
    mulligan = _queen_blood(
        active=True,
        phase="dealing",
        screen="mulligan",
        hand=[{"hand_index": index, "card_id": f"card-{index}"} for index in range(5)],
    )
    telemetry = MatchTelemetry([
        _state(1, queen_blood=prematch),
        _state(2, queen_blood=prematch),
        _state(3, queen_blood=mulligan),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.initiate_queens_blood_match(dialogue_confirmations=1)

    assert result["ok"] is True
    assert result["outcome"] == "mulligan_ready"
    assert result["queen_blood"]["screen"] == "mulligan"
    assert len(result["queen_blood"]["hand"]) == 5
    assert [item[:2] for item in backend.actions] == [
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
    ]


def test_mulligan_cards_verifies_replacements_at_turn_one() -> None:
    opening_hand = [
        {
            "hand_index": index,
            "card_id": f"opening-{index}",
            "name": f"Opening {index}",
            "mulligan_selected": False,
        }
        for index in range(5)
    ]
    selected_0 = [dict(card, mulligan_selected=index == 0) for index, card in enumerate(opening_hand)]
    selected_01 = [dict(card, mulligan_selected=index in {0, 1}) for index, card in enumerate(opening_hand)]
    selected_012 = [dict(card, mulligan_selected=index in {0, 1, 2}) for index, card in enumerate(opening_hand)]
    replacement_hand = [
        {"hand_index": 0, "card_id": "replacement-0", "name": "Replacement 0"},
        {"hand_index": 1, "card_id": "replacement-1", "name": "Replacement 1"},
        {"hand_index": 2, "card_id": "replacement-2", "name": "Replacement 2"},
        opening_hand[3],
        opening_hand[4],
    ]
    telemetry = MatchTelemetry([
        _state(
            1,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 0},
                hand=opening_hand,
            ),
        ),
        _state(
            2,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 0},
                hand=opening_hand,
            ),
        ),
        _state(
            3,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 0},
                hand=opening_hand,
            ),
        ),
        _state(
            4,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 0},
                hand=selected_0,
            ),
        ),
        _state(
            5,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 0},
                hand=selected_0,
            ),
        ),
        _state(
            6,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 1},
                hand=selected_0,
            ),
        ),
        _state(
            7,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 1},
                hand=selected_01,
            ),
        ),
        _state(
            8,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 1},
                hand=selected_01,
            ),
        ),
        _state(
            9,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 2},
                hand=selected_01,
            ),
        ),
        _state(
            10,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 2},
                hand=selected_012,
            ),
        ),
        _state(
            11,
            queen_blood=_queen_blood(
                active=True,
                phase="player_turn",
                turn=1,
                screen="match",
                cursor={"area": "hand", "hand_index": 0},
                hand=replacement_hand,
            ),
        ),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.mulligan_cards((0, 1, 2), milliseconds=25)

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["selected_indices"] == [0, 1, 2]
    assert result["changed_indices"] == [0, 1, 2]
    assert result["unchanged_indices"] == [3, 4]
    assert result["missing_name_indices"] == []
    assert result["queen_blood"]["phase"] == "player_turn"
    assert [item[1] for item in backend.actions if item[0] == "tap" and item[1] != "confirm"] == [
        "queen_blood_right", "queen_blood_right",
        "queen_blood_play",
    ]
    assert [item[:2] for item in backend.actions if item[:2] == ("tap", "confirm")] == [
        ("tap", "confirm"),
        ("tap", "confirm"),
        ("tap", "confirm"),
    ]
    assert [item[2] for item in backend.actions if item[:2] == ("tap", "confirm")] == [500, 500, 500]
    assert result["selection_trace"][-1]["observed_indices"] == [0, 1, 2]
    assert backend.release_count >= 1
    assert service._arm.stopped is True


def test_mulligan_cards_can_submit_the_unchanged_hand() -> None:
    opening_hand = [
        {
            "hand_index": index,
            "card_id": f"opening-{index}",
            "name": f"Opening {index}",
            "mulligan_selected": False,
        }
        for index in range(5)
    ]
    mulligan = _queen_blood(
        active=True,
        phase="dealing",
        screen="mulligan",
        cursor={"area": "hand", "hand_index": 0},
        hand=opening_hand,
    )
    turn_one = _queen_blood(
        active=True,
        phase="player_turn",
        turn=1,
        screen="match",
        hand=opening_hand,
    )
    telemetry = MatchTelemetry([
        _state(1, queen_blood=mulligan),
        _state(2, queen_blood=turn_one),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.mulligan_cards((), milliseconds=25)

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["selection_verified"] is True
    assert result["selected_indices"] == []
    assert result["unchanged_indices"] == [0, 1, 2, 3, 4]
    assert result["selection_trace"] == [{
        "expected_indices": [],
        "observed_indices": [],
        "outcome": "no_mulligan_markers_verified",
    }]
    assert [item[:2] for item in backend.actions] == [("tap", "queen_blood_play")]


def test_mulligan_cards_retries_confirm_after_fresh_marker_stays_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opening_hand = [
        {
            "hand_index": index,
            "card_id": f"opening-{index}",
            "name": f"Opening {index}",
            "mulligan_selected": False,
        }
        for index in range(5)
    ]
    selected_hand = [dict(card, mulligan_selected=index == 0) for index, card in enumerate(opening_hand)]
    replacement_hand = [
        {"hand_index": 0, "card_id": "replacement-0", "name": "Replacement 0"},
        *opening_hand[1:],
    ]

    def mulligan_state(hand: list[dict]) -> dict:
        return _queen_blood(
            active=True,
            phase="dealing",
            screen="mulligan",
            cursor={"area": "hand", "hand_index": 0},
            hand=hand,
        )

    telemetry = MatchTelemetry([
        _state(1, queen_blood=mulligan_state(opening_hand)),
        _state(2, queen_blood=mulligan_state(opening_hand)),
        _state(3, queen_blood=mulligan_state(opening_hand)),
        _state(4, queen_blood=mulligan_state(opening_hand)),
        _state(5, queen_blood=mulligan_state(selected_hand)),
        _state(6, queen_blood=_queen_blood(
            active=True,
            phase="player_turn",
            turn=1,
            screen="match",
            hand=replacement_hand,
        )),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)
    marker_waits = iter([
        (_state(20, queen_blood=mulligan_state(opening_hand)), False),
        (_state(21, queen_blood=mulligan_state(selected_hand)), True),
    ])

    def wait_for_marker(*_args: object, **_kwargs: object) -> tuple[GameState, bool]:
        return next(marker_waits)

    monkeypatch.setattr(service, "_wait_for_queen_blood_mulligan_marker", wait_for_marker)

    result = service.mulligan_cards(
        (0,), milliseconds=25, max_selection_retries=5, submit_play=False
    )

    assert result["verified"] is True
    assert result["selected_indices"] == [0]
    assert result["max_selection_retries"] == 5
    assert result["max_selection_attempts"] == 6
    assert [item for item in backend.actions if item[:2] == ("tap", "confirm")] == [
        ("tap", "confirm", 500),
        ("tap", "confirm", 500),
    ]
    assert result["selection_trace"][0]["observed_indices"] == []
    assert result["selection_trace"][0]["outcome"] == "marker_still_false_after_wait"
    assert result["selection_trace"][1]["observed_indices"] == [0]


def test_mulligan_cards_uses_gamepad_a_after_all_confirm_attempts_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opening_hand = [
        {
            "hand_index": index,
            "card_id": f"opening-{index}",
            "name": f"Opening {index}",
            "mulligan_selected": False,
        }
        for index in range(5)
    ]
    selected_hand = [
        dict(card, mulligan_selected=index == 0)
        for index, card in enumerate(opening_hand)
    ]
    replacement_hand = [
        {"hand_index": 0, "card_id": "replacement-0", "name": "Replacement 0"},
        *opening_hand[1:],
    ]

    def mulligan_state(hand: list[dict]) -> dict:
        return _queen_blood(
            active=True,
            phase="dealing",
            screen="mulligan",
            cursor={"area": "hand", "hand_index": 0},
            hand=hand,
        )

    states = [_state(1, queen_blood=mulligan_state(opening_hand))]
    states.extend(
        _state(sequence, queen_blood=mulligan_state(opening_hand))
        for sequence in range(2, 14)
    )
    states.append(_state(14, queen_blood=mulligan_state(selected_hand)))
    states.append(_state(15, queen_blood=_queen_blood(
        active=True,
        phase="player_turn",
        turn=1,
        screen="match",
        hand=replacement_hand,
    )))

    class GamepadFallbackInput(FakeRealInput):
        def press_gamepad_a(self, milliseconds: int) -> dict:
            self.actions.append(("gamepad_a", milliseconds))
            return {"ok": True, "button": "x360_a", "milliseconds": milliseconds}

    backend = GamepadFallbackInput()
    service = _service(MatchTelemetry(states), backend)
    marker_waits = iter([
        (_state(20 + index, queen_blood=mulligan_state(opening_hand)), False)
        for index in range(6)
    ] + [(_state(40, queen_blood=mulligan_state(selected_hand)), True)])

    def wait_for_marker(*_args: object, **_kwargs: object) -> tuple[GameState, bool]:
        return next(marker_waits)

    monkeypatch.setattr(service, "_wait_for_queen_blood_mulligan_marker", wait_for_marker)

    result = service.mulligan_cards(
        (0,), milliseconds=25, max_selection_retries=5, submit_play=False
    )

    assert result["verified"] is True
    assert [item for item in backend.actions if item[:2] == ("tap", "confirm")] == [
        ("tap", "confirm", 500),
    ] * 6
    assert [item for item in backend.actions if item[0] == "gamepad_a"] == [
        ("gamepad_a", 500),
    ]
    assert result["inputs"][-1]["reason"] == "mulligan_click_fallback"
    assert result["selection_trace"][-1]["outcome"] == "mark_verified_gamepad_fallback"


def test_mulligan_cards_can_restore_inference_without_resending_begin_match() -> None:
    opening_hand = [
        {
            "hand_index": index,
            "card_id": f"opening-{index}",
            "name": f"Opening {index}",
            "mulligan_selected": False,
        }
        for index in range(5)
    ]
    selected_hand = [dict(card, mulligan_selected=index == 0) for index, card in enumerate(opening_hand)]
    replacement_hand = [
        {"hand_index": 0, "card_id": "replacement-0", "name": "Replacement 0"},
        *opening_hand[1:],
    ]
    telemetry = MatchTelemetry([
        _state(1, queen_blood=_queen_blood(
            active=False,
            phase="inactive",
            screen="deck_select",
            begin_match_available=True,
            match_screen=True,
            cursor={"area": "hand", "hand_index": 0},
            hand=opening_hand,
        )),
        _state(2, queen_blood=_queen_blood(
            active=False,
            phase="inactive",
            screen="deck_select",
            begin_match_available=True,
            match_screen=True,
            cursor={"area": "hand", "hand_index": 0},
            hand=opening_hand,
        )),
        _state(3, queen_blood=_queen_blood(
            active=False,
            phase="inactive",
            screen="deck_select",
            begin_match_available=True,
            match_screen=True,
            cursor={"area": "hand", "hand_index": 0},
            hand=selected_hand,
        )),
        _state(4, queen_blood=_queen_blood(
            active=True,
            phase="player_turn",
            turn=1,
            screen="match",
            hand=replacement_hand,
        )),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.mulligan_cards(
        (0,), milliseconds=25, infer_current_hand_is_mulligan=True
    )

    assert result["verified"] is True
    assert result["state_inference"] == "identified_hand_recovery"
    assert [item for item in backend.actions if item[0] == "tap" and item[1] != "confirm"] == [
        ("tap", "queen_blood_play", 25),
    ]
    assert [item for item in backend.actions if item[:2] == ("tap", "confirm")] == [
        ("tap", "confirm", 500),
    ]


def test_mulligan_cards_retries_a_transient_missing_qb_sidecar_before_begin() -> None:
    opening_hand = [
        {
            "hand_index": index,
            "card_id": f"opening-{index}",
            "name": f"Opening {index}",
            "mulligan_selected": False,
        }
        for index in range(5)
    ]
    selected_hand = [
        dict(card, mulligan_selected=index == 1)
        for index, card in enumerate(opening_hand)
    ]
    replacement_hand = [
        opening_hand[0],
        {"hand_index": 1, "card_id": "replacement-1", "name": "Replacement 1"},
        opening_hand[2],
        opening_hand[3],
        opening_hand[4],
    ]
    prematch = _queen_blood(
        screen="deck_select",
        begin_match_available=True,
        match_screen=True,
    )
    mulligan = _queen_blood(
        active=True,
        phase="dealing",
        screen="mulligan",
        cursor={"area": "hand", "hand_index": 0},
        hand=opening_hand,
    )
    mulligan_hovered = dict(mulligan, cursor={"area": "hand", "hand_index": 1})
    mulligan_selected = dict(
        mulligan_hovered,
        hand=selected_hand,
    )
    turn_one = _queen_blood(
        active=True,
        phase="player_turn",
        screen="match",
        turn=1,
        cursor={"area": "hand", "hand_index": 0},
        hand=replacement_hand,
    )
    telemetry = MatchTelemetry([
        _state(1),
        _state(2, queen_blood=prematch),
        _state(3, queen_blood=prematch),
        _state(4, queen_blood=mulligan),
        _state(5, queen_blood=mulligan),
        _state(6, queen_blood=mulligan_hovered),
        _state(7, queen_blood=mulligan_selected),
        _state(8, queen_blood=turn_one),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.mulligan_cards(
        (1,), milliseconds=25, begin_if_needed=True, selection_milliseconds=200
    )

    assert result["ok"] is True
    assert result["selected_indices"] == [1]
    assert result["selection_trace"][-1]["observed_indices"] == [1]
    assert [item[1] for item in backend.actions if item[0] == "tap"] == [
        "queen_blood_begin_match",
        "confirm",
        "queen_blood_right",
        "confirm",
        "queen_blood_play",
    ]


def test_select_queens_blood_cards_reuses_verified_cursor_navigation() -> None:
    hand = [
        {"hand_index": index, "card_id": f"card-{index}", "name": f"Card {index}"}
        for index in range(5)
    ]
    telemetry = MatchTelemetry([
        _state(1, queen_blood=_queen_blood(
            active=True,
            phase="player_turn",
            screen="match",
            cursor={"area": "hand", "hand_index": 0},
            hand=hand,
        )),
        _state(2, queen_blood=_queen_blood(
            active=True,
            phase="player_turn",
            screen="match",
            cursor={"area": "hand", "hand_index": 0},
            hand=hand,
        )),
        _state(3, queen_blood=_queen_blood(
            active=True,
            phase="player_turn",
            screen="match",
            cursor={"area": "hand", "hand_index": 1},
            hand=hand,
        )),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.select_queens_blood_cards((1,))

    assert result["ok"] is True
    assert result["selected_indices"] == [1]
    assert result["outcome"] == "cards_hover_verified"
    assert result["selection_trace"][-1]["cursor_after_navigation"] == 1
    assert backend.actions == [("tap", "queen_blood_right", 200)]


def test_mulligan_cards_rejects_a_non_mulligan_hand() -> None:
    telemetry = MatchTelemetry([
        _state(
            1,
            queen_blood=_queen_blood(
                active=True,
                phase="player_turn",
                hand=[{"hand_index": index, "card_id": f"card-{index}"} for index in range(5)],
            ),
        ),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    with pytest.raises(SafetyError, match="mulligan hand is not ready"):
        service.mulligan_cards((0, 1, 2))
    assert [item for item in backend.actions if item[0] == "tap" and item[1] != "confirm"] == []


def test_mulligan_cards_selects_only_exact_named_cards() -> None:
    opening_hand = [
        {
            "hand_index": 0,
            "card_id": "security-officer",
            "name": "Security Officer",
            "mulligan_selected": False,
        },
        {
            "hand_index": 1,
            "card_id": "levrikon-1",
            "name": "Levrikon",
            "mulligan_selected": False,
        },
        {
            "hand_index": 2,
            "card_id": "cactuar",
            "name": "Cactuar",
            "mulligan_selected": False,
        },
        {
            "hand_index": 3,
            "card_id": "levrikon-2",
            "name": "Levrikon",
            "mulligan_selected": False,
        },
        {
            "hand_index": 4,
            "card_id": "tonberry",
            "name": "Tonberry",
            "mulligan_selected": False,
        },
    ]
    selected_1 = [
        dict(card, mulligan_selected=index == 1)
        for index, card in enumerate(opening_hand)
    ]
    selected_13 = [
        dict(card, mulligan_selected=index in {1, 3})
        for index, card in enumerate(opening_hand)
    ]
    replacement_hand = [
        opening_hand[0],
        {"hand_index": 1, "card_id": "replacement-1", "name": "Replacement 1"},
        opening_hand[2],
        {"hand_index": 3, "card_id": "replacement-3", "name": "Replacement 3"},
        opening_hand[4],
    ]
    telemetry = MatchTelemetry([
        _state(
            1,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 0},
                hand=opening_hand,
            ),
        ),
        _state(
            2,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 0},
                hand=opening_hand,
            ),
        ),
        _state(
            3,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 0},
                hand=opening_hand,
            ),
        ),
        _state(
            4,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 1},
                hand=opening_hand,
            ),
        ),
        _state(
            5,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 1},
                hand=selected_1,
            ),
        ),
        _state(
            6,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 1},
                hand=selected_1,
            ),
        ),
        _state(
            7,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 3},
                hand=selected_1,
            ),
        ),
        _state(
            8,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                cursor={"area": "hand", "hand_index": 3},
                hand=selected_13,
            ),
        ),
        _state(
            9,
            queen_blood=_queen_blood(
                active=True,
                phase="player_turn",
                turn=1,
                screen="match",
                cursor={"area": "hand", "hand_index": 0},
                hand=replacement_hand,
            ),
        ),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.mulligan_cards(only_card_name="levrikon", milliseconds=25)

    assert result["ok"] is True
    assert result["performed"] is True
    assert result["only_card_name"] == "levrikon"
    assert result["selected_indices"] == [1, 3]
    assert result["changed_indices"] == [1, 3]
    assert result["unchanged_indices"] == [0, 2, 4]
    assert [item[1] for item in backend.actions if item[0] == "tap" and item[1] != "confirm"] == [
        "queen_blood_right", "queen_blood_right", "queen_blood_right",
        "queen_blood_play",
    ]
    assert [item[:2] for item in backend.actions if item[:2] == ("tap", "confirm")] == [
        ("tap", "confirm"),
        ("tap", "confirm"),
    ]
    assert [item[2] for item in backend.actions if item[:2] == ("tap", "confirm")] == [500, 500]
    assert result["selection_trace"][-1]["observed_indices"] == [1, 3]


def test_mulligan_cards_named_selector_does_not_play_when_name_is_absent() -> None:
    opening_hand = [
        {
            "hand_index": index,
            "card_id": f"card-{index}",
            "name": f"Card {index}",
            "mulligan_selected": False,
        }
        for index in range(5)
    ]
    telemetry = MatchTelemetry([
        _state(
            1,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                hand=opening_hand,
            ),
        ),
        _state(
            2,
            queen_blood=_queen_blood(
                active=True,
                phase="dealing",
                screen="mulligan",
                hand=opening_hand,
            ),
        ),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.mulligan_cards(only_card_name="Levrikon", milliseconds=25)

    assert result["ok"] is True
    assert result["outcome"] == "no_matching_cards"
    assert result["performed"] is False
    assert result["selected_indices"] == []
    assert [item for item in backend.actions if item[0] in {"tap", "mouse_click"}] == []


def test_mulligan_cards_waits_for_the_cursor_to_arrive() -> None:
    # The cursor is seen passing slot 1 on its way to 2: that is not a miss
    # (the burst was planned from a fresh sample), so nothing is re-sent.
    opening_hand = [
        {
            "hand_index": index,
            "card_id": f"opening-{index}",
            "name": f"Opening {index}",
            "mulligan_selected": False,
        }
        for index in range(5)
    ]
    selected_2 = [
        dict(card, mulligan_selected=index == 2)
        for index, card in enumerate(opening_hand)
    ]
    replacement_hand = [
        opening_hand[0],
        opening_hand[1],
        {"hand_index": 2, "card_id": "replacement-2", "name": "Replacement 2"},
        opening_hand[3],
        opening_hand[4],
    ]
    telemetry = MatchTelemetry([
        _state(1, queen_blood=_queen_blood(
            active=True, phase="dealing", screen="mulligan",
            cursor={"area": "hand", "hand_index": 0}, hand=opening_hand,
        )),
        _state(2, queen_blood=_queen_blood(
            active=True, phase="dealing", screen="mulligan",
            cursor={"area": "hand", "hand_index": 0}, hand=opening_hand,
        )),
        _state(3, queen_blood=_queen_blood(
            active=True, phase="dealing", screen="mulligan",
            cursor={"area": "hand", "hand_index": 0}, hand=opening_hand,
        )),
        _state(4, queen_blood=_queen_blood(
            active=True, phase="dealing", screen="mulligan",
            cursor={"area": "hand", "hand_index": 1}, hand=opening_hand,
        )),
        _state(5, queen_blood=_queen_blood(
            active=True, phase="dealing", screen="mulligan",
            cursor={"area": "hand", "hand_index": 1}, hand=opening_hand,
        )),
        _state(6, queen_blood=_queen_blood(
            active=True, phase="dealing", screen="mulligan",
            cursor={"area": "hand", "hand_index": 2}, hand=opening_hand,
        )),
        _state(7, queen_blood=_queen_blood(
            active=True, phase="dealing", screen="mulligan",
            cursor={"area": "hand", "hand_index": 2}, hand=selected_2,
        )),
        _state(8, queen_blood=_queen_blood(
            active=True, phase="player_turn", turn=1, screen="match",
            cursor={"area": "hand", "hand_index": 0}, hand=replacement_hand,
        )),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.mulligan_cards(
        (2,), milliseconds=25, max_selection_retries=2, selection_milliseconds=200
    )

    assert result["ok"] is True
    assert result["max_selection_retries"] == 2
    assert result["max_selection_attempts"] == 3
    assert [item[1] for item in backend.actions if item[0] == "tap" and item[1] != "confirm"] == [
        "queen_blood_right", "queen_blood_right",
        "queen_blood_play",
    ]
    assert [item[2] for item in backend.actions if item[:2] == ("tap", "queen_blood_right")] == [
        200, 200,
    ]
    assert [item[2] for item in backend.actions if item[:2] == ("tap", "queen_blood_play")] == [25]
    assert [item[:2] for item in backend.actions if item[:2] == ("tap", "confirm")] == [
        ("tap", "confirm"),
    ]
    assert [item[2] for item in backend.actions if item[:2] == ("tap", "confirm")] == [500]
    assert len(result["selection_trace"]) == 1
    assert result["selection_trace"][-1]["cursor_before"] == 0
    assert result["selection_trace"][-1]["cursor_after_navigation"] == 2
    assert result["selection_trace"][-1]["outcome"] == "mark_verified"
    assert result["selected_indices"] == [2]
    assert result["selection_trace"][-1]["expected_indices"] == [2]
    assert result["selection_trace"][-1]["observed_indices"] == [2]


def test_mulligan_verifies_the_kept_cards_after_the_game_reorders_the_hand() -> None:
    # Live 2026-09-29: slot 0 was swapped and the game re-sorted the hand,
    # the replacement arriving in slot 4; slot-by-slot checks called it
    # unverified.
    opening = [
        {"hand_index": index, "card_id": f"opening-{index}", "name": f"Opening {index}",
         "mulligan_selected": False}
        for index in range(5)
    ]
    marked = [dict(card, mulligan_selected=card["hand_index"] == 0) for card in opening]
    reordered = [
        dict(card, hand_index=index) for index, card in enumerate(opening[1:])
    ] + [{"hand_index": 4, "card_id": "replacement", "name": "Replacement"}]
    mulligan = dict(active=True, phase="dealing", screen="mulligan",
                    cursor={"area": "hand", "hand_index": 0})
    telemetry = MatchTelemetry([
        _state(1, queen_blood=_queen_blood(**mulligan, hand=opening)),
        _state(2, queen_blood=_queen_blood(**mulligan, hand=opening)),
        _state(3, queen_blood=_queen_blood(**mulligan, hand=marked)),
        _state(4, queen_blood=_queen_blood(
            active=True, phase="player_turn", turn=1, screen="match",
            cursor={"area": "hand", "hand_index": 0}, hand=reordered,
        )),
    ])
    service = _service(telemetry, FakeRealInput())

    result = service.mulligan_cards((0,), milliseconds=25)

    assert result["outcome"] == "turn_one_hand_verified"
    assert result["kept_cards_present"] is True


def test_mulligan_cards_waits_without_input_until_hover_identity_recovers() -> None:
    hand = [{"hand_index": i, "card_id": f"opening-{i}", "mulligan_selected": False} for i in range(5)]
    unknown = _state(1, queen_blood=_queen_blood(active=True, phase="dealing", screen="mulligan", hand=hand))
    known = _state(2, queen_blood=dict(unknown.queen_blood, cursor={"area": "hand", "hand_index": 0}))
    backend = FakeRealInput();service = _service(MatchTelemetry([unknown, known]), backend)
    trace = [];inputs = []
    result, _ = service._select_queens_blood_card_slot(0, action_deadline=service._monotonic() + 10,
        max_selection_attempts=1, selection_milliseconds=25, trace=trace, selection_trace=[], inputs=inputs)
    assert result.sequence == 2
    assert inputs == []
    assert not [item for item in backend.actions if item[0] in {"tap", "mouse_click"}]
    assert trace[0]["stage"] == "cursor_not_ready"


@pytest.mark.parametrize("change", ["identity", "phase", "turn", "marker"])
def test_cursor_wait_still_refuses_changed_mulligan_state(change):
    hand = [{"hand_index": i, "card_id": f"opening-{i}", "mulligan_selected": False} for i in range(5)]
    unknown = _state(1, queen_blood=_queen_blood(active=True, phase="dealing", screen="mulligan", hand=hand))
    payload = dict(unknown.queen_blood, cursor={"area": "hand", "hand_index": 0},
                   hand=[dict(card) for card in hand])
    if change == "identity":
        payload["hand"][0]["card_id"] = "other-card"
    elif change == "marker":
        payload["hand"][0]["mulligan_selected"] = True
    else:
        payload[change] = "player_turn" if change == "phase" else 1
    backend = FakeRealInput()
    service = _service(MatchTelemetry([unknown, _state(2, queen_blood=payload)]), backend)

    with pytest.raises(SafetyError, match="hand changed") as caught:
        service._select_queens_blood_card_slot(0, action_deadline=service._monotonic() + 10,
            max_selection_attempts=1, selection_milliseconds=25, trace=[], selection_trace=[], inputs=[])

    assert caught.value.details["stage"] == "admission"
    assert caught.value.details["inputs"] == []
    assert caught.value.details["action"] == "select_queens_blood_card_slot"
    assert not [a for a in backend.actions if a[0] in {"tap", "mouse_click"}]


@pytest.mark.parametrize("outer_attempt,inner_attempt", [(False, False), (True, False), (False, True)])
def test_mulligan_wrapper_preserves_admission_proof_and_either_attempt_ledger(
    outer_attempt, inner_attempt, monkeypatch,
):
    hand = [{"hand_index": i, "card_id": f"opening-{i}", "name": f"Opening {i}",
             "mulligan_selected": False} for i in range(5)]
    state = _state(1, queen_blood=_queen_blood(active=True, phase="dealing", screen="mulligan",
                   cursor={"area": "hand", "hand_index": 0}, hand=hand))
    backend = FakeRealInput()
    service = _service(MatchTelemetry([state]), backend)
    attempt = {"button": "confirm"}
    source_trace = [{"sequence": 1, "stage": "cursor_not_ready"}]

    def fail_selection(*args, **kwargs):
        if outer_attempt:
            kwargs["inputs"].append(attempt)
        if outer_attempt or inner_attempt:
            backend.tap("confirm", 25)
        raise SafetyError("cursor admission failed", details={
            "action": "select_queens_blood_card_slot", "stage": "admission",
            "inputs": [attempt] if inner_attempt else [], "trace": source_trace,
        })

    monkeypatch.setattr(service, "_select_queens_blood_card_slot", fail_selection)
    with pytest.raises(SafetyError, match="cursor admission failed") as caught:
        service.mulligan_cards((0,), submit_play=False)

    details = caught.value.details
    assert details["action"] == "select_queens_blood_card_slot"
    assert details["stage"] == "admission"
    assert details["trace"] == source_trace
    assert bool(details["inputs"]) is (outer_attempt or inner_attempt)
    assert not service._action_lock.locked()


def test_mulligan_cards_does_not_input_without_hover_identity() -> None:
    opening_hand = [
        {
            "hand_index": index,
            "card_id": f"opening-{index}",
            "name": f"Opening {index}",
            "mulligan_selected": False,
        }
        for index in range(5)
    ]
    telemetry = MatchTelemetry([
        _state(1, queen_blood=_queen_blood(
            active=True, phase="dealing", screen="mulligan", hand=opening_hand,
        )),
        _state(2, queen_blood=_queen_blood(
            active=True, phase="dealing", screen="mulligan", hand=opening_hand,
        )),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    with pytest.raises(SafetyError, match="cursor telemetry did not identify"):
        service.mulligan_cards((2,), milliseconds=25)
    assert [item for item in backend.actions if item[0] in {"tap", "mouse_click"}] == []


def test_initiate_queens_blood_match_retries_begin_when_deck_select_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prematch = _queen_blood(
        screen="deck_select",
        begin_match_available=True,
        match_screen=True,
    )
    mulligan = _queen_blood(
        active=True,
        phase="dealing",
        screen="mulligan",
        hand=[{"hand_index": index, "card_id": f"card-{index}"} for index in range(5)],
    )
    telemetry = MatchTelemetry([_state(1, queen_blood=prematch)])
    backend = FakeRealInput()
    service = _service(telemetry, backend)
    samples = iter([
        _state(2, queen_blood=prematch),
        _state(3, queen_blood=mulligan),
    ])

    def next_sample(*_args: object, **_kwargs: object) -> GameState:
        return next(samples)

    monkeypatch.setattr(service, "_wait_for_queen_blood_sample", next_sample)

    result = service.initiate_queens_blood_match(dialogue_confirmations=1)

    assert result["ok"] is True
    assert result["outcome"] == "mulligan_ready"
    begin_inputs = [
        item for item in result["inputs"]
        if item.get("button") == "queen_blood_begin_match"
    ]
    assert [item["attempt"] for item in begin_inputs] == [1, 2]
    assert [item[:2] for item in backend.actions] == [
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
    ]


def test_initiate_queens_blood_match_checks_deck_select_before_every_begin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Live 2026-10-06: Begin Match pressed in the open deck editor is Craft
    # Prebuilt Deck plus Yes, which overwrote the deck. No F3 there.
    prematch = _queen_blood(
        screen="deck_select",
        begin_match_available=True,
        match_screen=True,
    )
    telemetry = MatchTelemetry([_state(1, queen_blood=prematch)])
    backend = FakeRealInput()
    service = _service(telemetry, backend)
    modes = iter(["select", "editor"])
    monkeypatch.setattr(service, "_deck_screen_mode", lambda: next(modes))
    monkeypatch.setattr(service, "_wait_for_queen_blood_sample",
                        lambda *_args, **_kwargs: _state(2, queen_blood=prematch))

    with pytest.raises(SafetyError, match="deck select is not shown"):
        service.initiate_queens_blood_match(dialogue_confirmations=1)

    assert [item[1] for item in backend.actions if item[0] == "tap"] == [
        "queen_blood_begin_match", "confirm",
    ]


def test_initiate_queens_blood_match_waits_twenty_seconds_for_each_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prematch = _queen_blood(
        screen="deck_select",
        begin_match_available=True,
        match_screen=True,
    )
    telemetry = MatchTelemetry([_state(1, queen_blood=prematch)])
    backend = FakeRealInput()
    service = _service(telemetry, backend)
    samples = iter([
        _state(2, queen_blood=prematch),
        _state(3, queen_blood=prematch),
        _state(4, queen_blood=prematch),
    ])
    wait_windows: list[float] = []

    def next_sample(
        _previous_sequence: int,
        deadline: float,
        _predicate: object,
        _trace: list[dict[str, object]],
        **_kwargs: object,
    ) -> GameState:
        wait_windows.append(deadline - service._monotonic())
        return next(samples)

    monkeypatch.setattr(service, "_wait_for_queen_blood_sample", next_sample)

    with pytest.raises(SafetyError, match="3 Begin Match attempts"):
        service.initiate_queens_blood_match(dialogue_confirmations=1)

    assert len(wait_windows) == 3
    assert all(19.0 <= window <= 20.0 for window in wait_windows)
    assert [item[1] for item in backend.actions if item[0] == "tap"] == [
        "queen_blood_begin_match", "confirm",
        "queen_blood_begin_match", "confirm",
        "queen_blood_begin_match", "confirm",
    ]


def test_initiate_queens_blood_match_stops_when_sidecar_is_missing_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prematch = _queen_blood(
        screen="deck_select",
        begin_match_available=True,
        match_screen=True,
    )
    telemetry = MatchTelemetry([_state(1, queen_blood=prematch)])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    def next_sample(*_args: object, **_kwargs: object) -> GameState:
        return _state(2, queen_blood=None)

    monkeypatch.setattr(service, "_wait_for_queen_blood_sample", next_sample)

    with pytest.raises(SafetyError, match="sidecar is no longer fresh"):
        service.initiate_queens_blood_match(dialogue_confirmations=1)

    assert [item[:2] for item in backend.actions] == [
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
    ]


def test_initiate_queens_blood_match_confirms_modal_after_begin_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prematch = _queen_blood(
        screen="deck_select",
        begin_match_available=True,
        match_screen=True,
    )
    started = _queen_blood(
        active=True,
        phase="player_turn",
        screen="mulligan",
        match_screen=True,
        hand=[{"hand_index": index, "card_id": f"card-{index}"} for index in range(5)],
    )
    telemetry = MatchTelemetry([_state(1, queen_blood=prematch)])
    backend = FakeRealInput()
    service = _service(telemetry, backend)
    samples = iter([
        _state(2, queen_blood=prematch),
        _state(3, queen_blood=prematch),
        _state(4, queen_blood=started),
    ])

    def next_sample(*_args: object, **_kwargs: object) -> GameState:
        return next(samples)

    monkeypatch.setattr(service, "_wait_for_queen_blood_sample", next_sample)

    result = service.initiate_queens_blood_match(dialogue_confirmations=1)

    assert result["ok"] is True
    assert result["outcome"] == "mulligan_ready"
    assert any(
        item.get("reason") == "start_match_default_yes_after_begin_attempt"
        for item in result["inputs"]
    )
    confirm_inputs = [
        item for item in result["inputs"]
        if item.get("reason") == "start_match_default_yes_after_begin_attempt"
    ]
    assert [item["attempt"] for item in confirm_inputs] == [1, 2, 3]
    assert [item[:2] for item in backend.actions] == [
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
    ]


def test_initiate_queens_blood_match_waits_for_mulligan_after_partial_deal() -> None:
    prematch = _queen_blood(
        screen="deck_select",
        begin_match_available=True,
        match_screen=True,
    )
    partial_deal = _queen_blood(
        hand=[{"hand_index": index, "card_id": f"card-{index}"} for index in range(4)],
    )
    mulligan = _queen_blood(
        active=True,
        phase="dealing",
        screen="mulligan",
        hand=[{"hand_index": index, "card_id": f"card-{index}"} for index in range(5)],
    )
    telemetry = MatchTelemetry([
        _state(1, queen_blood=prematch),
        _state(2, queen_blood=partial_deal),
        _state(3, queen_blood=mulligan),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.initiate_queens_blood_match(dialogue_confirmations=1)

    assert result["ok"] is True
    assert result["outcome"] == "mulligan_ready"
    assert len(result["queen_blood"]["hand"]) == 4
    assert result["queen_blood"]["screen"] == "mulligan"
    assert [item[:2] for item in backend.actions] == [
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
    ]


def test_initiate_queens_blood_match_allows_existing_prematch_after_world_telemetry_blanks() -> None:
    prematch = _queen_blood(
        screen="deck_select",
        begin_match_available=True,
        match_screen=True,
    )
    mulligan = _queen_blood(
        active=True,
        phase="dealing",
        screen="mulligan",
        hand=[{"hand_index": index, "card_id": f"card-{index}"} for index in range(5)],
    )
    telemetry = MatchTelemetry([
        _state(1, queen_blood=prematch, map_id=None),
        _state(2, queen_blood=mulligan, map_id=None),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend, settled_gameplay_samples=2)

    result = service.initiate_queens_blood_match(
        target_id="Turner",
        dialogue_confirmations=1,
    )

    assert result["ok"] is True
    assert result["outcome"] == "mulligan_ready"
    assert result["queen_blood"]["screen"] == "mulligan"
    assert len(result["queen_blood"]["hand"]) == 5
    assert [item[:2] for item in backend.actions] == [
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
    ]


def test_initiate_queens_blood_match_refuses_f3_without_prematch_signal() -> None:
    telemetry = MatchTelemetry([
        _state(1),
        _state(2, interaction={"state": "dialogue", "target_id": "qb-npc"}),
        _state(3, interaction={"state": "dialogue", "target_id": "qb-npc"}),
        _state(4, interaction={"state": "dialogue", "target_id": "qb-npc"}),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    with pytest.raises(SafetyError, match="match screen was not observed"):
        service.initiate_queens_blood_match(
            target_id="qb-npc",
            dialogue_confirmations=1,
        )

    assert not any(item[0] == "tap" and item[1] == "queen_blood_begin_match" for item in backend.actions)
    assert service._arm.stopped is True


def test_initiate_queens_blood_match_waits_for_delayed_prematch_ui() -> None:
    prematch = {
        "screen": "pre_match",
        "begin_match_available": True,
    }
    started = {
        "active": True,
        "phase": "player_turn",
        "match_id": "match-delayed",
        "cursor": {"area": "hand", "hand_index": 0, "row": 0, "column": 0},
        "hand": [{"hand_index": 0, "card_id": "cactuar"}],
        "legal_moves": [{"hand_index": 0, "card_id": "cactuar", "row": 0, "column": 0}],
    }
    telemetry = MatchTelemetry([
        _state(1, queen_blood={"active": False, "phase": "inactive"}),
        _state(2, map_id=None, queen_blood={"active": False, "phase": "inactive"}),
        _state(3, map_id=None, queen_blood={"active": False, "phase": "inactive"}),
        _state(4, map_id=None, queen_blood={"active": False, "phase": "inactive"}),
        _state(5, map_id=None, queen_blood=prematch),
        _state(6, map_id=None, queen_blood=prematch),
        _state(7, map_id=None, queen_blood=started),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.initiate_queens_blood_match(
        target_id="qb-npc",
        dialogue_confirmations=1,
    )

    assert result["ok"] is True
    assert result["dialogue_confirmations_sent"] == 0
    assert any(item[0] == "tap" and item[1] == "queen_blood_begin_match" for item in backend.actions)


def test_initiate_queens_blood_match_sends_interaction_once_when_dialogue_is_not_observed() -> None:
    # Live 2026-09-25: a retried interaction key landed on the rematch
    # prompt and declined it, so the key is sent once and the dialogue is
    # awaited without input.
    prematch = {
        "screen": "pre_match",
        "begin_match_available": True,
    }
    started = {
        "active": True,
        "phase": "player_turn",
        "match_id": "match-retried",
        "cursor": {"area": "hand", "hand_index": 0, "row": 0, "column": 0},
        "hand": [{"hand_index": 0, "card_id": "cactuar"}],
        "legal_moves": [{"hand_index": 0, "card_id": "cactuar", "row": 0, "column": 0}],
    }
    telemetry = MatchTelemetry([
        _state(1),
        _state(2),
        *[_state(3) for _ in range(601)],
        _state(4, interaction={"state": "dialogue", "target_id": "qb-npc"}),
        _state(5, interaction={"state": "dialogue", "target_id": "qb-npc"}),
        _state(6, interaction={"state": "menu"}, queen_blood=prematch),
        _state(7, interaction={"state": "menu"}, queen_blood=prematch),
        _state(8, interaction={"state": "menu"}, queen_blood=started),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.initiate_queens_blood_match(
        target_id="qb-npc",
        dialogue_confirmations=2,
    )

    assert result["ok"] is True
    assert result["interaction_sent"] is True
    # Without an observed dialogue the default confirmations carry it on.
    assert result["dialogue_confirmations_sent"] == 2
    assert [item[:2] for item in backend.actions] == [
        ("interaction", 1000),
        ("tap", "queen_blood_left"),
        ("tap", "confirm"),
        ("tap", "queen_blood_left"),
        ("tap", "confirm"),
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
    ]


def test_initiate_queens_blood_match_resolves_named_live_card_gamer(monkeypatch: pytest.MonkeyPatch) -> None:
    prematch = {
        "screen": "pre_match",
        "begin_match_available": True,
    }
    started = {
        "active": True,
        "phase": "player_turn",
        "match_id": "match-turner",
        "cursor": {"area": "hand", "hand_index": 0, "row": 0, "column": 0},
        "hand": [{"hand_index": 0, "card_id": "cactuar"}],
        "legal_moves": [{"hand_index": 0, "card_id": "cactuar", "row": 0, "column": 0}],
    }
    telemetry = MatchTelemetry([
        _state(1, include_target=False),
        _state(2, include_target=False),
        _state(3, include_target=False),
        _state(4, include_target=False),
        _state(5, queen_blood=prematch, include_target=False),
        _state(6, queen_blood=prematch, include_target=False),
        _state(7, queen_blood=started, include_target=False),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    def resolve_turner(name: str, *, seconds: int = 60, identity_index_path: str | None = None) -> dict:
        assert name == "Turner"
        assert seconds >= 10
        return {
            "ok": True,
            "status": "located",
            "canonical_id": "field_object:2610-NIBLT:oba2610_Actor_CardGamer03",
            "identity": {
                "candidates": [{"actor_name": "Npc2610_CardGamer03"}],
            },
            "position": {"x": 100.0, "y": 0.0, "z": 0.0},
            "live_actor": {
                "id": "turner-live",
                "actor_name": "Npc2610_CardGamer03",
                "is_player": False,
            },
        }

    monkeypatch.setattr(service, "resolve_world_target", resolve_turner)

    result = service.initiate_queens_blood_match(
        target_id="Turner",
        dialogue_confirmations=3,
    )

    assert result["ok"] is True
    assert result["target"]["id"] == "turner-live"
    assert result["target"]["canonical_id"] == "field_object:2610-NIBLT:oba2610_Actor_CardGamer03"
    assert result["interaction_sent"] is True
    assert [item[:2] for item in backend.actions] == [
        ("interaction", 1000),
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
    ]


def test_initiate_queens_blood_match_retries_target_resolution_until_npc_is_instantiated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prematch = {"screen": "pre_match", "begin_match_available": True}
    started = {
        "active": True,
        "phase": "player_turn",
        "match_id": "match-turner-retry",
        "cursor": {"area": "hand", "hand_index": 0, "row": 0, "column": 0},
        "hand": [{"hand_index": 0, "card_id": "cactuar"}],
        "legal_moves": [{"hand_index": 0, "card_id": "cactuar", "row": 0, "column": 0}],
    }
    telemetry = MatchTelemetry([
        _state(1, include_target=False),
        _state(2, include_target=False),
        _state(3, include_target=False),
        _state(4, queen_blood=prematch, include_target=False),
        _state(5, queen_blood=started, include_target=False),
        _state(6, queen_blood=started, include_target=False),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)
    scan_calls: list[int] = []
    resolutions = iter([
        {"status": "known_but_not_currently_instantiated"},
        {
            "status": "located",
            "canonical_id": "field_object:turner",
            "identity": {"candidates": [{"actor_name": "Npc_CardGamer_Turner"}]},
            "position": {"x": 0.0, "y": 0.0, "z": 0.0},
            "live_actor": {
                "id": "turner-live",
                "actor_name": "Npc_CardGamer_Turner",
                "is_player": False,
            },
        },
    ])

    def resolve_turner(name: str, *, seconds: int = 60, identity_index_path: str | None = None) -> dict:
        assert name == "Turner"
        scan_calls.append(seconds)
        return next(resolutions)

    monkeypatch.setattr(service, "resolve_world_target", resolve_turner)

    result = service.initiate_queens_blood_match(target_id="Turner", dialogue_confirmations=1)

    assert result["ok"] is True
    assert result["target"]["id"] == "turner-live"
    assert scan_calls[0] >= 10
    assert scan_calls[1] >= 10
    assert [item[:2] for item in backend.actions] == [
        ("interaction", 1000),
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
    ]


def test_initiate_queens_blood_match_resumes_explicit_named_dialogue_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prematch = {
        "screen": "pre_match",
        "begin_match_available": True,
    }
    started = {
        "active": True,
        "phase": "player_turn",
        "match_id": "match-resumed",
        "cursor": {"area": "hand", "hand_index": 0, "row": 0, "column": 0},
        "hand": [{"hand_index": 0, "card_id": "cactuar"}],
        "legal_moves": [{"hand_index": 0, "card_id": "cactuar", "row": 0, "column": 0}],
    }
    telemetry = MatchTelemetry([
        _state(1, include_target=False, map_id=None),
        _state(2, include_target=False, map_id=None),
        _state(3, queen_blood=prematch, include_target=False, map_id=None),
        _state(4, queen_blood=prematch, include_target=False, map_id=None),
        _state(5, queen_blood=started, include_target=False, map_id=None),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    def should_not_reenter_world_settling(**kwargs: object) -> GameState:
        assert kwargs["allow_queen_blood_dialogue_transition"] is True
        return telemetry.observe()

    monkeypatch.setattr(service, "_assert_action_allowed", should_not_reenter_world_settling)

    result = service.initiate_queens_blood_match(
        target_id="Turner",
        dialogue_confirmations=1,
    )

    assert result["ok"] is True
    assert result["interaction_sent"] is False
    assert result["target"] is None
    assert [item[:2] for item in backend.actions] == [
        ("tap", "queen_blood_begin_match"),
        ("tap", "confirm"),
    ]


def test_queen_blood_confirm_allows_stale_explicit_prematch_signal() -> None:
    telemetry = MatchTelemetry([
        _state(
            1,
            queen_blood={
                "screen": "deck_select",
                "match_screen": True,
                "begin_match_available": True,
            },
        ),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)
    service.max_telemetry_age = 0.0

    state = service._assert_real_menu_input_allowed(
        "confirm",
        500,
        allow_queen_blood_dialogue_transition=True,
    )

    assert state.sequence == 1


def test_non_queens_blood_confirm_still_rejects_stale_telemetry() -> None:
    telemetry = MatchTelemetry([_state(1)])
    backend = FakeRealInput()
    service = _service(telemetry, backend)
    service.max_telemetry_age = 0.0

    with pytest.raises(SafetyError, match="telemetry is stale"):
        service._assert_real_menu_input_allowed("confirm", 500)


def _cursor_state(sequence: int, hand_index: int) -> GameState:
    return _state(sequence, queen_blood=_queen_blood(
        active=True,
        phase="dealing",
        cursor={"area": "hand", "hand_index": hand_index, "row": None, "column": None},
    ))


@pytest.mark.parametrize(("settle_seconds", "expected_cursor"), [(0.0, 2), (1.5, 3)])
def test_settled_cursor_waits_out_a_queued_navigation_step(
    settle_seconds: float,
    expected_cursor: int,
) -> None:
    # Live 2026-09-24: the cursor was sampled on the target (2) before a
    # queued Right moved it to 3, and the mark landed on slot 3.
    clock = {"now": 100.0}
    states = [_cursor_state(10, 0), _cursor_state(11, 2)] + [
        _cursor_state(12 + offset, 3) for offset in range(5)
    ]
    service = AgentService(
        MatchTelemetry(states),
        FakeRealInput(),
        arm_state=FakeArm(),
        environment=FakeEnvironment(),
        minimum_action_delay=0.0,
        monotonic=lambda: clock["now"],
        sleeper=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
        queen_blood_cursor_settle_seconds=settle_seconds,
    )

    settled = service._wait_for_settled_queen_blood_cursor(10, 1000.0, [])

    assert service._queen_blood_cursor_or_none(settled) == expected_cursor
    if settle_seconds:
        assert clock["now"] - 100.0 >= settle_seconds


def _resume_hand(marked: set[int]) -> dict:
    return _queen_blood(
        active=True,
        phase="dealing",
        screen="mulligan",
        cursor={"area": "hand", "hand_index": 0},
        hand=[
            {
                "hand_index": index,
                "card_id": f"opening-{index}",
                "name": f"Opening {index}",
                "mulligan_selected": index in marked,
            }
            for index in range(5)
        ],
    )


def test_mulligan_cards_resumes_when_requested_cards_are_already_marked() -> None:
    telemetry = MatchTelemetry([_state(1 + offset, queen_blood=_resume_hand({0, 2})) for offset in range(6)])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.mulligan_cards((0, 2), milliseconds=25, submit_play=False)

    assert result["verified"] is True
    assert result["selected_indices"] == [0, 2]
    assert [item for item in backend.actions if item[0] == "tap"] == []
    assert [record["outcome"] for record in result["selection_trace"]] == [
        "already_marked_verified", "already_marked_verified",
    ]


def test_mulligan_cards_refuses_unrequested_existing_markers_before_input() -> None:
    telemetry = MatchTelemetry([_state(1 + offset, queen_blood=_resume_hand({1})) for offset in range(6)])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    with pytest.raises(SafetyError, match=r"unrequested cards marked: \[1\]"):
        service.mulligan_cards((0,), milliseconds=25, submit_play=False)

    assert [item for item in backend.actions if item[0] in {"tap", "mouse_click"}] == []


def test_initiate_confirms_an_already_open_begin_popup_without_f3() -> None:
    # A previous Confirm was dropped: the probe reports the Begin Match
    # popup still open, so only Yes is sent.
    prematch = {
        "screen": "pre_match",
        "begin_match_available": True,
        "popups": {"begin_confirm": True, "mulligan_confirm": False},
    }
    started = {
        "active": False,
        "phase": "inactive",
        "screen": "deck_select",
        "begin_match_available": True,
        "match_screen": True,
        "cursor": {"area": "hand", "hand_index": 0, "row": 0, "column": 0},
        "hand": [{"hand_index": 0, "card_id": "card-0", "name": "Card 0"}],
        "legal_moves": [],
    }
    telemetry = MatchTelemetry([
        _state(1, queen_blood={"active": False, "phase": "inactive"}),
        _state(2, interaction={"state": "dialogue", "target_id": "qb-npc"}),
        _state(3, interaction={"state": "dialogue", "target_id": "qb-npc"}),
        _state(4, interaction={"state": "dialogue", "target_id": "qb-npc"}),
        _state(5, interaction={"state": "menu"}, queen_blood=prematch),
        _state(6, interaction={"state": "menu"}, queen_blood=prematch),
        _state(7, interaction={"state": "menu"}, queen_blood=started),
    ])
    backend = FakeRealInput()
    service = _service(telemetry, backend)

    result = service.initiate_queens_blood_match(
        target_id="qb-npc",
        dialogue_confirmations=3,
    )

    assert result["ok"] is True
    assert result["outcome"] == "mulligan_ready"
    assert result["interaction_sent"] is True
    assert result["dialogue_confirmations_sent"] == 1
    assert result["queen_blood"]["phase"] == "dealing"
    assert result["queen_blood"]["screen"] == "mulligan"
    assert result["state_inference"] == "identified_hand_after_begin_match"
    observed = service.observe_queens_blood()
    assert observed["queen_blood"]["screen"] == "mulligan"
    assert observed["queen_blood"]["state_inference"] == {
        "state": "mulligan",
        "basis": "identified_hand_after_begin_match",
        "raw_screen": "deck_select",
        "sequence": 7,
    }
    assert [item[:2] for item in backend.actions] == [
        ("interaction", 1000),
        ("tap", "queen_blood_left"),
        ("tap", "confirm"),
        ("tap", "confirm"),
    ]
    assert service._arm.stopped is True


def test_mulligan_waits_for_the_swapped_in_card_to_be_named() -> None:
    # The probe withholds a reused widget's identity until its next read, so
    # the first turn-one sample can hold an unnamed card (live 2026-09-30).
    opening = [
        {"hand_index": index, "card_id": f"opening-{index}", "name": f"Opening {index}",
         "mulligan_selected": False}
        for index in range(5)
    ]
    marked = [dict(card, mulligan_selected=card["hand_index"] == 0) for card in opening]
    unnamed = [{"hand_index": 0}] + opening[1:]
    named = [{"hand_index": 0, "card_id": "replacement", "name": "Replacement"}] + opening[1:]
    mulligan = dict(active=True, phase="dealing", screen="mulligan", cursor={"area": "hand", "hand_index": 0})
    turn = dict(active=True, phase="player_turn", turn=1, screen="match", cursor={"area": "hand", "hand_index": 0})
    telemetry = MatchTelemetry([
        _state(1, queen_blood=_queen_blood(**mulligan, hand=opening)),
        _state(2, queen_blood=_queen_blood(**mulligan, hand=opening)),
        _state(3, queen_blood=_queen_blood(**mulligan, hand=marked)),
        _state(4, queen_blood=_queen_blood(**turn, hand=unnamed)),
        _state(5, queen_blood=_queen_blood(**turn, hand=named)),
    ])
    service = _service(telemetry, FakeRealInput())

    result = service.mulligan_cards((0,), milliseconds=25)

    assert result["outcome"] == "turn_one_hand_verified"


def test_observe_at_our_turn_says_what_changed_since_our_last_move() -> None:
    # #182: narration reacts to the opponent's move, not only our own.
    def turn(phase: str, cells: list[dict]) -> dict:
        return {"schema_version": 1, "active": True, "phase": phase, "turn": 3, "match_id": "match-1",
                "cursor": {"area": "hand", "hand_index": 0, "row": 0, "column": 0},
                "hand": [], "legal_moves": [], "board": {"rows": 3, "columns": 5, "cells": cells}}

    ours = {"row": 0, "column": 0, "owner": "player", "card_id": "card_id_001", "power": 3, "card_present": True}
    theirs = {"row": 0, "column": 4, "owner": "opponent", "card_id": "card_id_010", "power": 5, "card_present": True}
    after_our_move = turn("opponent_turn", [ours])
    service = _service(MatchTelemetry([_state(9, queen_blood=turn("player_turn", [ours, theirs]))]), FakeRealInput())
    assert "commentary" not in service.observe_queens_blood()  # nothing to compare yet

    service._record_action(action="place_queens_blood_card", ok=True, verified=True, queen_blood=after_our_move)
    commentary = service.observe_queens_blood()["commentary"]
    assert commentary["notable"] is True
    assert commentary["cues"][0]["kind"] == "opponent_played" and commentary["cues"][0]["card"] == "Elphadunk"
    assert commentary["projection"]["leader"] == "opponent"


def test_observe_reports_card_sums_when_the_games_lane_total_lags() -> None:
    # Live 2026-10-12 (#329): the middle lane read 7-6 while its cards summed
    # 7-8, so the agent and its narration thought a lost lane was won.
    cells = [{"row": 1, "column": 0, "owner": "player", "card_id": "card_id_001", "power": 7, "card_present": True},
             {"row": 1, "column": 3, "owner": "opponent", "card_id": "card_id_010", "power": 5, "card_present": True},
             {"row": 1, "column": 4, "owner": "opponent", "card_id": "card_id_001", "power": 3, "card_present": True}]
    lanes = [{"row": 0, "player_power": 0, "opponent_power": 0}, {"row": 1, "player_power": 7, "opponent_power": 6},
             {"row": 2, "player_power": 0, "opponent_power": 0}]
    payload = {"schema_version": 1, "active": True, "phase": "player_turn", "turn": 6, "match_id": "match-1",
               "cursor": {"area": "hand", "hand_index": 0, "row": 0, "column": 0}, "hand": [], "legal_moves": [],
               "board": {"rows": 3, "columns": 5, "cells": cells, "lanes": lanes}}
    service = _service(MatchTelemetry([_state(9, queen_blood=payload)]), FakeRealInput())
    service._record_action(action="place_queens_blood_card", ok=True, verified=True,
                           queen_blood=dict(payload, phase="opponent_turn"))

    result = service.observe_queens_blood()

    middle = result["queen_blood"]["board"]["lanes"][1]
    assert (middle["player_power"], middle["opponent_power"], middle["mismatch"]) == (7, 8, True)
    assert result["lane_totals_mismatch"] == ["middle"]
    projection = result["commentary"]["projection"]
    assert (projection["opponent_score"], projection["leader"]) == (8, "opponent")


@pytest.mark.parametrize("queen_blood", [None, {"probe_idle": True}])
def test_a_match_button_outside_a_match_points_to_the_plain_button(queen_blood):
    # Live 2026-10-12 (#343): Confirm on the Defeat screen was refused as
    # "telemetry is unavailable", with no hint that tap_button closes it.
    backend = FakeRealInput()
    service = _service(MatchTelemetry([_state(1, queen_blood=queen_blood)]), backend)
    with pytest.raises(SafetyError, match=r'tap_button\(button="confirm"\)'):
        service.tap_queens_blood_button("confirm")
    assert backend.actions == []


def _lagging_lane_payload(opponent_total: int, phase: str = "player_turn") -> dict:
    cells = [{"row": 1, "column": 0, "owner": "player", "card_id": "card_id_001", "power": 7, "card_present": True},
             {"row": 1, "column": 3, "owner": "opponent", "card_id": "card_id_010", "power": 5, "card_present": True},
             {"row": 1, "column": 4, "owner": "opponent", "card_id": "card_id_001", "power": 3, "card_present": True}]
    lanes = [{"row": 0, "player_power": 0, "opponent_power": 0},
             {"row": 1, "player_power": 7, "opponent_power": opponent_total},
             {"row": 2, "player_power": 0, "opponent_power": 0}]
    return {"schema_version": 1, "active": True, "phase": phase, "turn": 6, "match_id": "match-1",
            "cursor": {"area": "hand", "hand_index": 0, "row": 0, "column": 0}, "hand": [], "legal_moves": [],
            "board": {"rows": 3, "columns": 5, "cells": cells, "lanes": lanes}}


def test_observe_waits_for_lane_totals_to_reach_the_cards() -> None:
    # Live 2026-10-12 (#344): lane totals reached the cards one to four
    # seconds after a placement or effect, while the game spawned its score
    # tokens.
    lagging = _lagging_lane_payload(6)
    settled = _lagging_lane_payload(8)
    states = [_state(9, queen_blood=lagging)] * 2 + [_state(10, queen_blood=lagging), _state(11, queen_blood=settled)]
    service = _service(MatchTelemetry(states), FakeRealInput())

    result = service.observe_queens_blood()

    assert result["lane_settle"]["settled"] is True
    assert result["sequence"] == 11
    assert "lane_totals_mismatch" not in result
    assert result["queen_blood"]["board"]["lanes"][1]["opponent_power"] == 8


def test_a_lane_total_that_never_settles_is_a_mismatch_waited_on_once(monkeypatch) -> None:
    from ff7r_agent import service as service_module
    monkeypatch.setattr(service_module, "QUEEN_BLOOD_LANE_SETTLE_SECONDS", 0.0)
    service = _service(MatchTelemetry([_state(9, queen_blood=_lagging_lane_payload(6))]), FakeRealInput())

    first = service.observe_queens_blood()
    again = service.observe_queens_blood()

    middle = first["queen_blood"]["board"]["lanes"][1]
    assert (middle["opponent_power"], middle["displayed_opponent_power"], middle.get("mismatch")) == (8, 6, True)
    assert first["lane_totals_mismatch"] == ["middle"]
    assert first["lane_settle"]["settled"] is False
    assert again["lane_settle"] == {"settled": False, "waited_seconds": 0.0, "already_waited": True}


def test_a_lane_total_off_turn_is_settling_not_a_mismatch() -> None:
    service = _service(MatchTelemetry([_state(9, queen_blood=_lagging_lane_payload(6, phase="opponent_turn"))]),
                       FakeRealInput())

    result = service.observe_queens_blood()

    middle = result["queen_blood"]["board"]["lanes"][1]
    assert (middle.get("settling"), middle.get("mismatch")) == (True, None)
    assert "lane_totals_mismatch" not in result and "lane_settle" not in result
