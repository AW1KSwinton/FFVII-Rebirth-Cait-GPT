from __future__ import annotations

from ff7r_agent.queen_blood import QueenBloodState
from ff7r_agent.queen_blood_ai import BoardModel, choose_action, choose_mulligan

CROSS = [{"row": -1, "column": 0}, {"row": 0, "column": -1}, {"row": 0, "column": 1}, {"row": 1, "column": 0}]


def _state(pawns: dict, hand: list[dict], cards: dict | None = None, lanes=None) -> QueenBloodState:
    cells = []
    for row in range(3):
        for column in range(5):
            owner, rank = pawns.get((row, column), ("empty", 0))
            cell = {"row": row, "column": column, "owner": owner, "rank": rank}
            if cards and (row, column) in cards:
                owner, power = cards[(row, column)]
                cell.update(owner=owner, rank=0, card_present=True, card_id="card_id_x", power=power)
            cells.append(cell)
    return QueenBloodState.from_dict({
        "schema_version": 1, "active": True, "phase": "player_turn",
        "cursor": {"area": "hand", "hand_index": 0},
        "hand": hand, "legal_moves": [],
        "board": {"rows": 3, "columns": 5, "cells": cells,
                  "lanes": lanes or [{"row": r, "player_power": 0, "opponent_power": 0} for r in range(3)]},
    })


START = {(r, 0): ("player", 1) for r in range(3)} | {(r, 4): ("opponent", 1) for r in range(3)}
OFFICER = {"hand_index": 0, "card_id": "card_id_000", "name": "Security Officer",
           "cost": 1, "power": 1, "influence_offsets": CROSS, "playable": True}


def test_unknown_board_owner_does_not_credit_the_opponent() -> None:
    from dataclasses import replace

    state = _state(START, [], cards={(0, 0): ("unknown", 5), (0, 4): ("opponent", 2)})
    model = BoardModel.from_state(replace(state, lanes=()))
    assert model.player_lanes == [0, 0, 0]
    assert model.opponent_lanes == [2, 0, 0]


def test_influence_matches_the_live_security_officer_placement() -> None:
    # Live 2026-09-24: Security Officer on (1,0) -> (0,0) and (2,0) rank 2,
    # (1,1) gains a rank-1 pawn, and lane 1 player power becomes 1.
    model = BoardModel.from_state(_state(START, [OFFICER])).place(
        _state(START, [OFFICER]).hand[0], 1, 0
    )
    assert (model.cells[(0, 0)].owner, model.cells[(0, 0)].rank) == ("player", 2)
    assert (model.cells[(2, 0)].owner, model.cells[(2, 0)].rank) == ("player", 2)
    assert (model.cells[(1, 1)].owner, model.cells[(1, 1)].rank) == ("player", 1)
    assert model.cells[(1, 0)].card is True
    assert model.player_lanes == [0, 1, 0]


def test_influence_flips_enemy_pawns_and_skips_cards() -> None:
    pawns = dict(START) | {(1, 1): ("opponent", 2)}
    state = _state(pawns, [OFFICER], cards={(0, 0): ("opponent", 3)})
    model = BoardModel.from_state(state).place(state.hand[0], 1, 0)
    assert (model.cells[(1, 1)].owner, model.cells[(1, 1)].rank) == ("player", 2)
    assert model.cells[(0, 0)].card is True  # a card cell is not influenced


def test_legal_cells_respect_cost_and_occupancy() -> None:
    pricey = dict(OFFICER, hand_index=1, cost=2)
    pawns = dict(START) | {(0, 0): ("player", 2)}
    state = _state(pawns, [OFFICER, pricey], cards={(2, 0): ("player", 1)})
    model = BoardModel.from_state(state)
    assert model.legal_cells(state.hand[0]) == [(0, 0), (1, 0)]
    assert model.legal_cells(state.hand[1]) == [(0, 0)]


def test_choose_action_places_when_possible_and_passes_when_not() -> None:
    action = choose_action(_state(START, [OFFICER]))
    assert action.kind == "place" and action.hand_index == 0 and (action.row, action.column) in {
        (0, 0), (1, 0), (2, 0)
    }
    stuck = choose_action(_state(START, [dict(OFFICER, cost=3)]))
    assert stuck.kind == "pass"


def test_choose_action_passes_to_close_out_a_lead_after_an_opponent_pass() -> None:
    state = _state(START, [OFFICER], cards={(0, 1): ("player", 5), (0, 3): ("opponent", 1)})
    assert choose_action(state, opponent_passed=True).kind == "pass"
    assert choose_action(state, opponent_passed=False).kind == "place"


def test_mulligan_replaces_only_cost_three_cards() -> None:
    hand = [dict(OFFICER, hand_index=i, cost=c) for i, c in enumerate([1, 3, 2, 3, 1])]
    assert choose_mulligan(_state(START, hand)) == [1, 3]


GRENADIER = {"hand_index": 0, "card_id": "card_id_g", "name": "Grenadier", "cost": 1, "power": 1,
             "influence_offsets": CROSS, "playable": True,
             "effect_type_name": "immediate_effect", "effect_target_name": "enemy_cards",
             "effect_int_parameters": [-4], "effect_influence_offsets": [{"row": 0, "column": 2}]}


def test_immediate_power_effect_destroys_an_enemy_card_and_frees_its_lane() -> None:
    lanes = [{"row": 0, "player_power": 0, "opponent_power": 0},
             {"row": 1, "player_power": 0, "opponent_power": 3},
             {"row": 2, "player_power": 0, "opponent_power": 0}]
    state = _state(START, [GRENADIER], cards={(1, 2): ("opponent", 3)}, lanes=lanes)
    model = BoardModel.from_state(state).place(state.hand[0], 1, 0)
    assert model.cells[(1, 2)].card is False
    assert (model.cells[(1, 2)].owner, model.cells[(1, 2)].rank) == ("opponent", 1)
    assert model.opponent_lanes[1] == 0
    assert model.player_lanes[1] == 1


def test_effect_on_allies_only_skips_enemy_cards() -> None:
    buffer = dict(GRENADIER, effect_target_name="allied_cards", effect_int_parameters=[2])
    state = _state(START, [buffer], cards={(1, 2): ("opponent", 3)})
    model = BoardModel.from_state(state).place(state.hand[0], 1, 0)
    assert (model.cells[(1, 2)].card, model.cells[(1, 2)].power) == (True, 3)


def test_opponent_best_reply_never_improves_the_players_position() -> None:
    state = _state(START, [OFFICER])
    after = BoardModel.from_state(state).place(state.hand[0], 1, 0)
    assert after.opponent_best_reply().value() <= after.value()
    # With a rank-1 opponent pawn on (1,4) a reply is always available.
    assert after.opponent_best_reply().value() < after.value()


def test_choose_action_keeps_somewhere_to_play_the_rest_of_the_hand() -> None:
    # Only (1,0) and (1,1) are open to the player. A cost-2 card needs the
    # rank-2 cell (1,1); spending it on the cost-1 card would strand the
    # cost-2 card, so the cost-1 card goes to (1,0).
    pawns = {(1, 0): ("player", 1), (1, 1): ("player", 2), (1, 4): ("opponent", 1)}
    no_influence = dict(OFFICER, influence_offsets=[])
    heavy = dict(no_influence, hand_index=1, card_id="card_id_h", name="Heavy", cost=2, power=2)
    action = choose_action(_state(pawns, [no_influence, heavy]))
    assert action.kind == "place"
    assert (action.hand_index, action.row, action.column) in {(0, 1, 0), (1, 1, 1)}


def test_follow_up_counts_cards_left_with_a_legal_cell() -> None:
    state = _state(START, [OFFICER, dict(OFFICER, hand_index=1, cost=3)])
    model = BoardModel.from_state(state)
    assert model.best_follow_up([state.hand[1]]) == model.value()
    assert model.best_follow_up([state.hand[0]]) > model.value()


POSH = dict(OFFICER, hand_index=0, card_id="card_id_posh", name="Posh Chocobo", cost=1, power=1,
            influence_offsets=[], effect_type_name="lane_win_score_bonus",
            effect_target_name="event_or_no_board_target", effect_int_parameters=[3])


def test_lane_win_score_bonus_counts_only_for_the_lane_victor() -> None:
    # Live 2026-09-25: lanes 12-3 and 6-3 with Posh Chocobo in the first
    # scored 21 on the result screen, not 18.
    cards = {(0, 3): ("opponent", 12), (1, 1): ("player", 11), (1, 3): ("opponent", 3),
             (2, 1): ("player", 6), (2, 3): ("opponent", 3)}
    state = _state(START, [POSH], cards=cards)
    model = BoardModel.from_state(state).place(state.hand[0], 1, 0)
    assert model.lane_points() == (12 + 3 + 6, 12)
    lost = BoardModel.from_state(state).place(state.hand[0], 0, 0)
    assert lost.lane_points() == (11 + 6, 12)  # lane 0 still lost: no bonus


def test_round_end_score_rule_adds_the_losers_power() -> None:
    rule = dict(OFFICER, influence_offsets=[], power=1, effect_type_name="round_end_score_rule")
    state = _state(START, [rule], cards={(0, 1): ("player", 5), (0, 3): ("opponent", 2)})
    model = BoardModel.from_state(state).place(state.hand[0], 1, 0)
    assert model.lane_points() == (5 + 2 + 1, 0)


def test_the_board_model_scores_the_cards_not_a_lagging_game_lane_total() -> None:
    # Live 2026-10-12 against Michaela (#329): the middle lane read 7-6 while
    # its cards summed 7-8. The finish scorer builds this model from the last
    # board, so the game's totals must not shift the lanes.
    lanes = [{"row": 0, "player_power": 0, "opponent_power": 0},
             {"row": 1, "player_power": 7, "opponent_power": 6},
             {"row": 2, "player_power": 0, "opponent_power": 0}]
    state = _state(START, [OFFICER], cards={(1, 1): ("player", 7), (1, 3): ("opponent", 8)}, lanes=lanes)
    model = BoardModel.from_state(state)
    assert (model.player_lanes[1], model.opponent_lanes[1]) == (7, 8)
    assert model.lane_points() == (0, 8)


def test_the_game_lane_total_corrects_the_model_while_a_card_power_is_unread() -> None:
    # An unread power falls back to the catalogue's (none for card_id_x);
    # the game's lane total then stands in for the cards.
    lanes = [{"row": 0, "player_power": 6, "opponent_power": 2},
             {"row": 1, "player_power": 0, "opponent_power": 0},
             {"row": 2, "player_power": 0, "opponent_power": 0}]
    state = _state(START, [OFFICER], cards={(0, 1): ("player", None), (0, 3): ("opponent", 2)}, lanes=lanes)
    model = BoardModel.from_state(state)
    assert (model.player_lanes[0], model.opponent_lanes[0]) == (6, 2)


def test_board_cards_take_score_effects_from_the_catalogue(monkeypatch, tmp_path) -> None:
    catalogue = tmp_path / "card_catalogue.lua"
    catalogue.write_text(
        '    ["Card_x"] = { card_id = "card_id_x", name = "X", effect_int_parameters = {5}, '
        'effect_type_name = "lane_win_score_bonus" },\n', encoding="utf-8")
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_CATALOGUE", str(catalogue))
    lanes = [{"row": 0, "player_power": 0, "opponent_power": 3},
             {"row": 1, "player_power": 0, "opponent_power": 0},
             {"row": 2, "player_power": 0, "opponent_power": 0}]
    state = _state(START, [OFFICER], cards={(0, 3): ("opponent", 3)}, lanes=lanes)
    assert BoardModel.from_state(state).lane_points() == (0, 8)


def test_the_generated_catalogue_knows_posh_chocobo() -> None:
    from ff7r_agent.queen_blood_catalogue import DEFAULT_CATALOGUE, parse_catalogue
    effects = parse_catalogue(DEFAULT_CATALOGUE.read_text(encoding="utf-8"))
    posh = [e for e in effects.values() if e.effect_type_name == "lane_win_score_bonus"]
    assert sorted(e.effect_int_parameters[0] for e in posh) == [3, 5, 10, 10]


def test_known_opponent_card_replies_use_its_real_influence_and_ability() -> None:
    from ff7r_agent.queen_blood import QueenBloodCard
    # A Grenadier-like opponent card on (1,4) lowers the player card two
    # tiles in front of it -- (1,2) -- by 4, destroying a 3-power card.
    grenadier = QueenBloodCard(
        hand_index=0, card_id="card_id_016", name="Grenadier", cost=1, power=1,
        effect_type_name="immediate_effect", effect_target_name="enemy_cards",
        effect_int_parameters=(-4,), effect_influence_offsets=((0, 2),),
    )
    pawns = dict(START) | {(1, 3): ("empty", 0)}
    lanes = [{"row": 0, "player_power": 0, "opponent_power": 0},
             {"row": 1, "player_power": 3, "opponent_power": 0},
             {"row": 2, "player_power": 0, "opponent_power": 0}]
    state = _state(pawns, [OFFICER], cards={(1, 2): ("player", 3)}, lanes=lanes)
    model = BoardModel.from_state(state)
    reply = model.place(grenadier, 1, 4, side="opponent")
    assert reply.cells[(1, 2)].card is False
    assert reply.player_lanes[1] == 0 and reply.opponent_lanes[1] == 1
    assert model.opponent_best_reply((grenadier,)).value() <= reply.value()


def test_unidentified_hand_cards_parse_and_are_not_played() -> None:
    # Live 2026-09-25: Moogle Trio added two cards the probe could not
    # identify (no card_id, name, cost or power) and the state stopped parsing.
    unknown = {"hand_index": 1, "card_id": None, "playable": True}
    state = _state(START, [OFFICER, unknown])
    assert [card.identified for card in state.hand] == [True, False]
    action = choose_action(state)
    assert action.kind == "place" and action.hand_index == 0
    only_unknown = _state(START, [dict(unknown, hand_index=0)])
    assert choose_action(only_unknown).kind == "pass"


def test_card_navigation_reaches_slots_beyond_five_in_a_grown_hand() -> None:
    from ff7r_agent.queen_blood import plan_queen_blood_card_navigation
    steps = plan_queen_blood_card_navigation(0, 6, 7)
    assert [step.button for step in steps] == ["queen_blood_right"] * 6
    import pytest
    from ff7r_agent.queen_blood import QueenBloodError
    with pytest.raises(QueenBloodError, match="outside the 7-card hand"):
        plan_queen_blood_card_navigation(0, 7, 7)
