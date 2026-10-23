from ff7r_agent.queen_blood_commentary import commentary_cues, exchange_baseline, projection
from ff7r_agent.queen_blood import QueenBloodState, reconcile_lane_totals


def board(cells, lanes=None, match_id="match-1", phase="player_turn"):
    raw = {"rows": 3, "columns": 5, "cells": cells}
    if lanes is not None:
        raw["lanes"] = lanes
    return {"schema_version": 1, "active": True, "phase": phase, "turn": 4, "match_id": match_id,
            "cursor": {"area": "hand", "hand_index": 0, "row": 0, "column": 0},
            "hand": [], "legal_moves": [], "board": raw}


def card(row, column, owner, card_id, power):
    return {"row": row, "column": column, "owner": owner, "card_id": card_id, "power": power, "card_present": True}


def pawn(row, column, owner, rank=1):
    return {"row": row, "column": column, "owner": owner, "rank": rank, "card_present": False}


def kinds(result):
    return [cue["kind"] for cue in result["cues"]]


def test_the_opponent_taking_a_lane_with_a_named_card_is_notable():
    before = board([card(0, 0, "player", "card_id_001", 3), pawn(0, 1, "player")])
    after = board([card(0, 0, "player", "card_id_001", 3), card(0, 4, "opponent", "card_id_010", 5),
                   pawn(0, 1, "opponent")])
    result = commentary_cues(before, after)
    assert result["notable"] is True
    played = result["cues"][0]
    assert played == {"kind": "opponent_played", "card_id": "card_id_010", "card": "Elphadunk",
                      "lane": "top", "column": 4, "power": 5}
    assert "tiles_taken" in kinds(result)
    lead = next(cue for cue in result["cues"] if cue["kind"] == "lane_lead_changed")
    assert lead["lane"] == "top" and lead["from"] == "player" and lead["to"] == "opponent"
    assert "match_lead_changed" in kinds(result)
    assert result["projection"]["opponent_score"] == 5 and result["projection"]["leader"] == "opponent"


def test_a_destroyed_card_is_reported_by_name():
    before = board([card(1, 1, "player", "card_id_005", 2)])
    after = board([pawn(1, 1, "player")])
    result = commentary_cues(before, after)
    assert {"kind": "player_card_lost", "card_id": "card_id_005", "card": "Toxirat",
            "lane": "middle", "column": 1} in result["cues"]
    assert result["notable"] is True


def test_a_quiet_exchange_is_not_notable_but_close_lanes_are_flagged():
    before = board([card(2, 0, "player", "card_id_001", 3), card(2, 4, "opponent", "card_id_005", 1)])
    after = board([card(2, 0, "player", "card_id_001", 3), card(2, 4, "opponent", "card_id_005", 1),
                   card(2, 3, "opponent", "card_id_001", 1)])
    result = commentary_cues(before, after)
    assert result["notable"] is False
    assert kinds(result) == ["opponent_played", "close_lane"]
    # Power in a lane the opponent is losing scores nothing: the projection holds.
    assert result["projection_change"] == 0 and result["projection"]["margin"] == 3


def test_the_cards_win_over_a_lagging_game_lane_total():
    # Live 2026-10-12 against Michaela (#329): the middle lane read 7-6 while
    # its cards summed 7-8, the projection said 11-9 and the match was lost
    # 4-17 as the cards said.
    cells = [card(0, 0, "player", "card_id_001", 2), card(0, 4, "opponent", "card_id_005", 4),
             card(0, 3, "opponent", "card_id_005", 5),
             card(1, 0, "player", "card_id_001", 1), card(1, 1, "player", "card_id_002", 3),
             card(1, 2, "player", "card_id_002", 3), card(1, 3, "opponent", "card_id_010", 5),
             card(1, 4, "opponent", "card_id_001", 3),
             card(2, 0, "player", "card_id_001", 2), card(2, 4, "opponent", "card_id_001", 1)]
    lanes = [{"row": 0, "player_power": 2, "opponent_power": 9}, {"row": 1, "player_power": 7, "opponent_power": 6},
             {"row": 2, "player_power": 4, "opponent_power": 1}]
    result = projection(QueenBloodState.from_dict(board(cells, lanes=lanes)))
    assert [(lane["player_power"], lane["opponent_power"]) for lane in result["lanes"]] == [(2, 9), (7, 8), (2, 1)]
    assert (result["player_score"], result["opponent_score"], result["leader"]) == (2, 17, "opponent")


def test_the_games_lane_totals_count_only_while_a_card_power_is_unread():
    lanes = [{"row": 0, "player_power": 9, "opponent_power": 2}, {"row": 1, "player_power": 0, "opponent_power": 0},
             {"row": 2, "player_power": 1, "opponent_power": 4}]
    state = QueenBloodState.from_dict(board([card(0, 0, "player", "card_id_001", None)], lanes=lanes))
    result = projection(state)
    assert [lane["leader"] for lane in result["lanes"]] == ["player", "tied", "opponent"]
    assert (result["player_score"], result["opponent_score"], result["margin"]) == (9, 4, 5)


def test_the_projection_adds_lane_win_bonuses_for_the_lane_victor_only():
    # Posh Chocobo (card_id_116, +3) leads the top lane; Tifa (card_id_102,
    # +5) is in the middle lane the opponent wins, so it adds nothing.
    cells = [card(0, 0, "player", "card_id_116", 2), card(0, 4, "opponent", "card_id_001", 1),
             card(1, 0, "player", "card_id_102", 1), card(1, 4, "opponent", "card_id_001", 4)]
    result = projection(QueenBloodState.from_dict(board(cells)))
    assert [(lane["points"], lane.get("score_bonus")) for lane in result["lanes"]] == [(5, 3), (4, None), (0, None)]
    assert (result["player_score"], result["opponent_score"], result["leader"]) == (5, 4, "player")


def test_the_projection_applies_the_round_end_score_rule():
    # Ultimate Party Animal (card_id_158) on the board: each lane's victor
    # also scores the loser's power there.
    cells = [card(0, 0, "player", "card_id_158", 1), card(0, 4, "opponent", "card_id_001", 3),
             card(1, 0, "player", "card_id_001", 6), card(1, 4, "opponent", "card_id_001", 2)]
    result = projection(QueenBloodState.from_dict(board(cells)))
    assert [lane["points"] for lane in result["lanes"]] == [4, 8, 0]
    assert (result["player_score"], result["opponent_score"]) == (8, 4)


def test_reported_lane_totals_are_the_card_sums_and_flag_a_lagging_game_total():
    # Live 2026-10-12 (#329): the game's middle-lane total read 7-6 while its
    # cards summed 7-8.
    cells = [card(1, 0, "player", "card_id_001", 7), card(1, 3, "opponent", "card_id_010", 5),
             card(1, 4, "opponent", "card_id_001", 3), card(0, 0, "player", "card_id_001", 2)]
    lanes = [{"row": 0, "player_power": 2, "opponent_power": 0}, {"row": 1, "player_power": 7, "opponent_power": 6}]
    payload = board(cells, lanes=lanes)
    reconciled = reconcile_lane_totals(payload)
    assert reconciled["board"]["lanes"] == [
        {"row": 0, "player_power": 2, "opponent_power": 0},
        {"row": 1, "player_power": 7, "opponent_power": 8,
         "displayed_player_power": 7, "displayed_opponent_power": 6, "mismatch": True},
        {"row": 2, "player_power": 0, "opponent_power": 0},
    ]
    assert payload["board"]["lanes"] == lanes  # The raw payload is not changed.
    unread = board([card(1, 0, "player", "card_id_001", None)], lanes=lanes)
    assert reconcile_lane_totals(unread) is unread


def test_nothing_to_compare_across_matches_or_without_a_board():
    after = board([card(0, 0, "opponent", "card_id_001", 3)])
    assert commentary_cues(None, after) is None
    assert commentary_cues(board([], match_id="match-0"), after) is None
    assert commentary_cues(board([]), {"schema_version": 1}) is None


def test_a_destroyed_card_calls_for_an_annoyed_reaction_naming_both_cards():
    # Owner 2026-10-06 (Robin): a Deathwheel destroyed our Security Officer and
    # the narration did not react at all.
    before = board([card(2, 2, "player", "card_id_000", 1), card(0, 3, "opponent", "card_id_007", 2)])
    after = board([pawn(2, 2, "player"), card(0, 3, "opponent", "card_id_007", 2),
                   card(0, 4, "opponent", "card_id_028", 1)])
    result = commentary_cues(before, after)
    assert result["reaction"]["tone"] == "frustrated"
    assert result["reaction"]["about"] == "Deathwheel destroyed your Security Officer in the bottom lane"


def test_a_countered_lane_and_a_regained_lead_have_their_own_reactions():
    before = board([card(0, 0, "player", "card_id_001", 3)])
    countered = board([card(0, 0, "player", "card_id_001", 3), card(0, 4, "opponent", "card_id_010", 5)])
    assert commentary_cues(before, countered)["reaction"]["tone"] == "concerned"  # the overall lead went too
    # Still ahead overall (top 7 against the middle's 5): only the lane changed hands.
    two_lanes = board([card(0, 0, "player", "card_id_001", 7), card(1, 0, "player", "card_id_001", 3)])
    lost_one = board([card(0, 0, "player", "card_id_001", 7), card(1, 0, "player", "card_id_001", 3),
                      card(1, 4, "opponent", "card_id_010", 5)])
    assert commentary_cues(two_lanes, lost_one)["reaction"] == {
        "tone": "frustrated", "about": "the opponent took back your middle lane with Elphadunk"}
    behind = board([card(0, 4, "opponent", "card_id_010", 5)])
    ahead = board([card(0, 4, "opponent", "card_id_010", 5), card(1, 0, "player", "card_id_010", 6)])
    assert commentary_cues(behind, ahead)["reaction"] == {"tone": "excited", "about": "you are back in the overall lead"}
    level = board([])
    assert commentary_cues(level, board([card(1, 0, "player", "card_id_010", 6)]))["reaction"]["about"] == (
        "you took the overall lead")


def test_an_opponent_who_passes_is_noticed_and_a_quiet_move_needs_no_reaction():
    still = board([card(0, 0, "player", "card_id_001", 3), card(0, 4, "opponent", "card_id_005", 1)])
    passed = commentary_cues(dict(still, phase="opponent_turn"), still)
    assert "opponent_passed" in kinds(passed) and passed["reaction"]["tone"] == "confident"
    quiet = board([card(0, 0, "player", "card_id_001", 3), card(0, 4, "opponent", "card_id_005", 1),
                   card(2, 4, "opponent", "card_id_005", 1)])
    assert commentary_cues(still, quiet)["reaction"] is None


def test_repeat_reads_of_the_same_player_turn_do_not_claim_an_opponent_pass():
    still = board([card(0, 0, "player", "card_id_001", 3)])

    result = commentary_cues(still, still)

    assert "opponent_passed" not in kinds(result)
    assert result["reaction"] is None


def test_an_advancing_turn_can_show_a_pass_when_the_opponent_phase_was_missed():
    still = board([card(0, 0, "player", "card_id_001", 3)])

    result = commentary_cues(still, dict(still, turn=still["turn"] + 1))

    assert "opponent_passed" in kinds(result)
    assert result["reaction"] == {"tone": "confident", "about": "the opponent passed"}


def test_after_our_pass_an_unchanged_board_is_not_an_opponent_pass():
    # Live 2026-10-12 (#345): two passes end the match, so a turn of ours
    # after our pass means the opponent played; their card had not landed.
    still = board([card(0, 0, "player", "card_id_001", 3), card(0, 4, "opponent", "card_id_005", 1)])
    result = commentary_cues(dict(still, phase="opponent_turn"), still, player_passed=True)
    assert "opponent_passed" not in kinds(result)
    assert result["reaction"] is None
    # After a placement the same boards still show the pass.
    placed = commentary_cues(dict(still, phase="opponent_turn"), still, player_passed=False)
    assert "opponent_passed" in kinds(placed)


def test_a_reply_already_in_our_moves_read_is_still_reported_as_played():
    # Live 2026-10-12 (#357): a placement read took 16.6 s and already held
    # Nene's Levrikon, so the next turn reported a pass.
    before_move = board([pawn(2, 0, "player", 2), card(2, 4, "opponent", "card_id_014", 1)])
    placed = board([card(2, 0, "player", "card_id_001", 3), card(2, 4, "opponent", "card_id_014", 1)],
                   phase="opponent_turn")
    replied = board([card(2, 0, "player", "card_id_001", 3), card(2, 2, "opponent", "card_id_006", 5),
                     card(2, 4, "opponent", "card_id_014", 1)], phase="opponent_turn")
    now = dict(replied, phase="player_turn")

    assert exchange_baseline(before_move, placed) is placed
    assert exchange_baseline(before_move, replied) is before_move
    result = commentary_cues(exchange_baseline(before_move, replied), now)

    assert "opponent_passed" not in kinds(result)
    played = [cue for cue in result["cues"] if cue["kind"] == "opponent_played"]
    assert [(cue["card_id"], cue["lane"], cue["column"]) for cue in played] == [("card_id_006", "bottom", 2)]
    # Without a usable board from before the move, the move's read is kept.
    assert exchange_baseline(None, replied) is replied
    assert exchange_baseline(dict(before_move, match_id="match-0"), replied) is replied


def test_an_opponent_card_still_landing_does_not_show_a_pass():
    before = board([card(0, 0, "player", "card_id_001", 3), card(0, 4, "opponent", "card_id_005", 1)])
    landing = board([card(0, 0, "player", "card_id_001", 3), card(0, 4, "opponent", "card_id_005", None)])
    result = commentary_cues(dict(before, phase="opponent_turn"), landing)
    assert "opponent_passed" not in kinds(result)


def test_a_card_read_without_its_id_is_not_reported_destroyed_or_played():
    # Live 2026-10-12 (#326): one read had present cells without ids and
    # reported our Security Officer destroyed while it was still there.
    before = board([card(1, 0, "player", "card_id_000", 1), card(1, 4, "opponent", "card_id_005", 1)])
    unread = board([card(1, 0, "player", None, None), card(1, 4, "opponent", None, None)])
    result = commentary_cues(before, unread)
    assert "player_card_lost" not in kinds(result) and "opponent_played" not in kinds(result)
    assert result["reaction"] is None or result["reaction"]["tone"] != "frustrated"
    # A card that really changed hands or left is still reported.
    replaced = board([card(1, 0, "opponent", None, 2), card(1, 4, "opponent", "card_id_005", 1)])
    changed = commentary_cues(before, replaced)
    assert "player_card_lost" in kinds(changed) and "opponent_played" in kinds(changed)


def test_a_new_match_is_not_compared_with_the_previous_matchs_board():
    # Live 2026-10-12 (#326): match ids read null for both matches, so the
    # first turn against Zahira reported Ned's final board as destroyed.
    finished = dict(board([card(0, 0, "player", "card_id_010", 5), card(1, 1, "player", "card_id_024", 3)],
                          match_id=None), turn=12)
    first_turn = dict(board([pawn(0, 0, "player"), pawn(1, 0, "player")], match_id=None), turn=1)
    assert commentary_cues(finished, first_turn) is None


def test_the_result_has_a_reaction_for_one_line_after_the_match():
    from ff7r_agent.queen_blood_commentary import result_reaction

    assert result_reaction({"player": 15, "opponent": 0})["tone"] == "excited"
    assert result_reaction({"player": 9, "opponent": 7}) == {"tone": "relieved", "about": "a narrow win, 9 to 7"}
    assert result_reaction({"player": 0, "opponent": 14})["tone"] == "concerned"
    assert result_reaction({"player": 6, "opponent": 8})["tone"] == "frustrated"
    assert result_reaction({"player": 5, "opponent": 5})["tone"] == "surprised"
    assert result_reaction(None) is None
