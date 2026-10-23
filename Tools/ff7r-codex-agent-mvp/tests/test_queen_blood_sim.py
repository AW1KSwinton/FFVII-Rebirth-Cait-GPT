from __future__ import annotations

import random

from ff7r_agent import queen_blood_sim as sim


def test_a_simulated_match_is_complete_and_deterministic_per_seed() -> None:
    first = sim.play_match(random.Random(3))
    second = sim.play_match(random.Random(3))
    assert (first.player_score, first.opponent_score, first.turns) == (
        second.player_score, second.opponent_score, second.turns)
    assert first.player_cards_played + first.opponent_cards_played > 0
    assert first.turns < 60


def test_easy01_objective_weighs_the_players_pawns_double() -> None:
    board = sim.start_board()
    base = sim.opponent_objective(board, sim.EASY01)
    board.cells[(0, 1)] = sim.ai._Cell("player", 1, False)
    assert sim.opponent_objective(board, sim.EASY01) == base - 2
    board.cells[(0, 3)] = sim.ai._Cell("opponent", 1, False)
    assert sim.opponent_objective(board, sim.EASY01) == base - 1


def test_decks_resolve_in_the_catalogue() -> None:
    assert len(sim.PLAYER_DECK) == 15 and len(sim.TURNER_DECK) == 15
    assert all(sim.catalogue_card(card_id) for card_id in set(sim.PLAYER_DECK) | set(sim.TURNER_DECK))


def test_easy02_objective_ignores_the_players_pawns() -> None:
    board = sim.start_board()
    base = sim.opponent_objective(board, sim.EASY02)
    board.cells[(0, 1)] = sim.ai._Cell("player", 1, False)
    assert sim.opponent_objective(board, sim.EASY02) == base
    assert sim.AI_WEIGHTS["card_ai_easy01"] is sim.EASY01


def test_starter_deck_plays_neds_deck() -> None:
    ned = sim.find_opponent("ned")
    result = sim.play_match(random.Random(5), player_deck=sim.STARTER_PLAYER_DECK,
                            opponent_deck=list(ned.deck))
    assert result.player_cards_played + result.opponent_cards_played > 0
    assert result.turns < 60


def test_cli_selects_the_opponent(capsys) -> None:
    assert sim.main(["--matches", "1", "--seed", "2", "--opponent", "ned", "--player-deck", "starter",
                     "--opponent-ai", "card_ai_easy02"]) == 0
    out = capsys.readouterr().out
    assert '"opponent": "ned"' in out and '"opponent_deck": "deck_en_00_0000"' in out
    assert '"opponent_ai": "card_ai_easy02"' in out
