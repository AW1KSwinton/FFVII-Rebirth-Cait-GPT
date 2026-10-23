"""Offline Queen's Blood matches for measuring the decision engine.

DEPRECATED for play (owner, 2026-09-29): agents decide their own decks and
moves; do not use this simulator to choose them. Kept for reference.

Plays the harness's own ``queen_blood_ai.decide`` against a model of an NPC
opponent on the same rules model (``BoardModel``), so changes to the player's
evaluation can be compared over hundreds of matches instead of a few live
ones. The opponent (``--opponent``: any record in queen_blood_opponents,
e.g. Turner or Ned) plays its DataTable deck with the objective of its
``CardGameAIParam`` row (``--opponent-ai`` overrides it; easy01 and easy02
are modelled). For ``card_ai_easy01`` (Turner, Ned), from the AI's side
("Enemy" in the game's naming):

    2 * own lane power - 1 * player lane power
    + 1 * own pawn ranks - 2 * player pawn ranks

with one turn of player prediction (``PlayerPredictionTurn = 1``): it
assumes the player's best reply from the player's actual hand.

Rules simulated: every card ability BoardModel models (see queen_blood_ai),
cards added to a hand by abilities joining that hand, 5-card opening hands
(the player may mulligan through ``choose_mulligan``), one draw at the start of every turn while the deck
lasts, a random first player, and the round ending on two consecutive
passes.
"""
from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import dataclass, field, replace

from . import queen_blood_ai as ai
from .queen_blood import BOARD_COLUMNS, BOARD_ROWS, QueenBloodCard
from .queen_blood_catalogue import catalogue_card
from .queen_blood_opponents import STARTER_DECK, find_opponent

# Reconstructed from cards drawn and played in live matches (2026-09-25).
PLAYER_DECK = (
    ["card_id_000"] * 2 + ["card_id_007"] * 2 + ["card_id_010"] * 2 + ["card_id_006"] * 2
    + ["card_id_002"] * 2 + ["card_id_116", "card_id_118", "card_id_024", "card_id_117", "card_id_004"]
)
# CardGameOpponentDeck row 125, deck_en_09_0002: Turner (CardGameOpponent
# row 30, oba2610_Actor_CardGamer03).
TURNER_DECK = (
    ["card_id_000"] * 2 + ["card_id_006"] * 2 + ["card_id_007"] * 2 + ["card_id_025"] * 2
    + ["card_id_001"] * 2 + ["card_id_011"] * 2 + ["card_id_016"] + ["card_id_072"] * 2
)
# CardGameOpponentDeck row 0, deck_PC_default: the starter deck a fresh save
# owns (see queen_blood_opponents.STARTER_DECK).
STARTER_PLAYER_DECK = list(STARTER_DECK)


@dataclass(frozen=True)
class OpponentWeights:
    own_power: float = 2.0
    player_power: float = -1.0
    own_pawns: float = 1.0
    player_pawns: float = -2.0
    predict_player: bool = True


EASY01 = OpponentWeights()
# CardGameAIParam card_ai_easy02: EnemyPowerQT 3, PlayerPowerQT 1,
# EnemyFlagQT 1, PlayerFlagQT 0, PlayerPredictionTurn 0. Its special and
# card-conservation terms (EnemySpecialQT, PlayerSpecialQT,
# EnemyCardConservationTwoRoundQT, SpecialPrediction) are not modelled.
EASY02 = OpponentWeights(own_power=3.0, player_power=1.0, own_pawns=1.0, player_pawns=0.0,
                         predict_player=False)
AI_WEIGHTS = {"card_ai_easy01": EASY01, "card_ai_easy02": EASY02}
# What the player's look-ahead assumes about the opponent (None: worst case).
PLAYER_MODEL_OF_OPPONENT: ai.OpponentObjective | None = None


def start_board() -> ai.BoardModel:
    model = ai.BoardModel()
    for row in range(BOARD_ROWS):
        for column in range(BOARD_COLUMNS):
            model.cells[(row, column)] = ai._Cell("empty", 0, False)
        model.cells[(row, 0)] = ai._Cell("player", 1, False)
        model.cells[(row, BOARD_COLUMNS - 1)] = ai._Cell("opponent", 1, False)
    return model


def _pawn_ranks(model: ai.BoardModel, side: str) -> int:
    return sum(cell.rank for cell in model.cells.values() if cell.owner == side and not cell.card)


def opponent_objective(model: ai.BoardModel, weights: OpponentWeights) -> float:
    return (
        weights.own_power * sum(model.opponent_lanes)
        + weights.player_power * sum(model.player_lanes)
        + weights.own_pawns * _pawn_ranks(model, "opponent")
        + weights.player_pawns * _pawn_ranks(model, "player")
    )


def _opponent_legal(model: ai.BoardModel, card: QueenBloodCard) -> list[tuple[int, int]]:
    return model.legal_cells(card, "opponent")


def opponent_decide(
    model: ai.BoardModel,
    hand: list[QueenBloodCard],
    player_hand: list[QueenBloodCard],
    weights: OpponentWeights,
    player_passed: bool,
) -> tuple[QueenBloodCard, int, int] | None:
    mine, theirs = model.lane_points()
    if player_passed and theirs > mine:
        return None
    best = None
    best_value = None
    for card in hand:
        for row, column in _opponent_legal(model, card):
            after = model.place(card, row, column, side="opponent")
            value = opponent_objective(after, weights)
            if weights.predict_player:
                replies = [
                    opponent_objective(after.place(reply, r, c), weights)
                    for reply in player_hand for r, c in after.legal_cells(reply)
                ]
                if replies:
                    value = min(replies)
            if best_value is None or value > best_value:
                best, best_value = (card, row, column), value
    return best


@dataclass
class MatchResult:
    player_score: int
    opponent_score: int
    player_first: bool
    turns: int
    player_cards_played: int
    opponent_cards_played: int
    decide_seconds: float = 0.0

    @property
    def won(self) -> bool:
        return self.player_score > self.opponent_score


def _cards(ids: list[str], start_index: int = 0) -> list[QueenBloodCard]:
    cards = []
    for offset, card_id in enumerate(ids):
        card = catalogue_card(card_id)
        if card is None:
            raise ValueError(f"card {card_id} is not in the catalogue")
        cards.append(replace(card, hand_index=start_index + offset))
    return cards


def _collect_added_cards(
    model: ai.BoardModel, player_hand: list[QueenBloodCard], opponent_hand: list[QueenBloodCard],
) -> ai.BoardModel:
    """Move cards abilities added (e.g. Moogle Trio's) into the hands."""
    if not model.hand_additions["player"] and not model.hand_additions["opponent"]:
        return model
    model = model.copy()
    for side, hand in (("player", player_hand), ("opponent", opponent_hand)):
        start = max((card.hand_index for card in player_hand + opponent_hand), default=0) + 1
        hand.extend(_cards(model.hand_additions[side], start_index=start))
        model.hand_additions[side] = []
    return model


def play_match(
    rng: random.Random,
    *,
    player_deck: list[str] = PLAYER_DECK,
    opponent_deck: list[str] = TURNER_DECK,
    weights: OpponentWeights = EASY01,
    known_opponent_cards: bool = True,
    max_turns: int = 60,
) -> MatchResult:
    player_pile = _cards(list(player_deck))
    opponent_pile = _cards(list(opponent_deck), start_index=100)
    rng.shuffle(player_pile)
    rng.shuffle(opponent_pile)
    player_hand, player_pile = player_pile[:5], player_pile[5:]
    opponent_hand, opponent_pile = opponent_pile[:5], opponent_pile[5:]
    # Player mulligan: replaced cards go back into the deck.
    for card in ai.mulligan_cards(player_hand):
        player_hand.remove(card)
        player_pile.append(card)
    rng.shuffle(player_pile)
    while len(player_hand) < 5 and player_pile:
        player_hand.append(player_pile.pop())
    known = tuple(_cards(sorted(set(opponent_deck)), start_index=200)) if known_opponent_cards else ()

    model = start_board()
    player_first = rng.random() < 0.5
    player_to_move = player_first
    passes_in_a_row = 0
    last_player_pass = last_opponent_pass = False
    played = {"player": 0, "opponent": 0}
    decide_seconds = 0.0
    turns = 0
    while passes_in_a_row < 2 and turns < max_turns:
        turns += 1
        if player_to_move:
            if player_pile:
                player_hand.append(player_pile.pop())
            started = time.perf_counter()
            plan = ai.decide(
                model, player_hand, opponent_passed=last_opponent_pass, opponent_cards=known,
                opponent_objective=PLAYER_MODEL_OF_OPPONENT,
            )
            decide_seconds += time.perf_counter() - started
            if plan.kind == "place":
                card = next(c for c in player_hand if c.hand_index == plan.hand_index)
                model = model.place(card, plan.row, plan.column)
                player_hand.remove(card)
                model = _collect_added_cards(model, player_hand, opponent_hand)
                played["player"] += 1
                passes_in_a_row = 0
                last_player_pass = False
            else:
                passes_in_a_row += 1
                last_player_pass = True
        else:
            if opponent_pile:
                opponent_hand.append(opponent_pile.pop())
            move = opponent_decide(model, opponent_hand, player_hand, weights, last_player_pass)
            if move is not None:
                card, row, column = move
                model = model.place(card, row, column, side="opponent")
                opponent_hand.remove(card)
                model = _collect_added_cards(model, player_hand, opponent_hand)
                played["opponent"] += 1
                passes_in_a_row = 0
                last_opponent_pass = False
            else:
                passes_in_a_row += 1
                last_opponent_pass = True
        player_to_move = not player_to_move
    mine, theirs = model.lane_points()
    return MatchResult(mine, theirs, player_first, turns, played["player"], played["opponent"], decide_seconds)


def run(
    matches: int,
    seed: int = 1,
    player_deck: list[str] = PLAYER_DECK,
    opponent_deck: list[str] = TURNER_DECK,
    weights: OpponentWeights = EASY01,
) -> dict:
    rng = random.Random(seed)
    results = [
        play_match(rng, player_deck=player_deck, opponent_deck=opponent_deck, weights=weights)
        for _ in range(matches)
    ]
    wins = sum(r.won for r in results)
    draws = sum(r.player_score == r.opponent_score for r in results)
    return {
        "matches": matches,
        "wins": wins,
        "draws": draws,
        "win_rate": round(wins / matches, 3),
        "mean_margin": round(sum(r.player_score - r.opponent_score for r in results) / matches, 2),
        "mean_player_cards": round(sum(r.player_cards_played for r in results) / matches, 2),
        "mean_opponent_cards": round(sum(r.opponent_cards_played for r in results) / matches, 2),
        "win_rate_first": round(
            sum(r.won for r in results if r.player_first) / max(1, sum(r.player_first for r in results)), 3
        ),
        "mean_decide_seconds": round(sum(r.decide_seconds for r in results) / matches, 2),
    }


def main(argv: list[str] | None = None) -> int:
    global PLAYER_MODEL_OF_OPPONENT
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--matches", type=int, default=50)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--set", action="append", default=[],
                        help="override a queen_blood_ai constant, e.g. --set PAWN_CELL_WEIGHT=1.2")
    parser.add_argument("--player-deck", default="current",
                        help="'current', 'starter', 'turner', or comma-separated card ids (15)")
    parser.add_argument("--opponent", default="turner",
                        help="an opponent in queen_blood_opponents (turner, ned, nene, zahira)")
    parser.add_argument("--opponent-ai", default=None, choices=sorted(AI_WEIGHTS),
                        help="the opponent's AI objective (default: its CardGameOpponent AIID)")
    parser.add_argument("--predict-opponent", action="store_true",
                        help="the player's look-ahead predicts the opponent with card_ai_easy01")
    args = parser.parse_args(argv)
    if args.predict_opponent:
        PLAYER_MODEL_OF_OPPONENT = ai.CARD_AI_EASY01
    for item in args.set:
        name, value = item.split("=", 1)
        if not hasattr(ai, name):
            raise SystemExit(f"unknown queen_blood_ai constant: {name}")
        setattr(ai, name, type(getattr(ai, name))(value))
    if args.player_deck == "current":
        deck = list(PLAYER_DECK)
    elif args.player_deck == "starter":
        deck = list(STARTER_PLAYER_DECK)
    elif args.player_deck == "turner":
        deck = list(TURNER_DECK)
    else:
        deck = [item.strip() for item in args.player_deck.split(",") if item.strip()]
    opponent = find_opponent(args.opponent)
    if opponent is None:
        raise SystemExit(f"unknown opponent: {args.opponent}")
    ai_id = args.opponent_ai or opponent.ai_id
    if ai_id not in AI_WEIGHTS:
        raise SystemExit(f"no simulator model for {ai_id}")
    print(json.dumps({"overrides": args.set, "predict_opponent": args.predict_opponent,
                      "deck": args.player_deck, "opponent": opponent.key, "opponent_deck": opponent.deck_id,
                      "opponent_ai": ai_id,
                      **run(args.matches, args.seed, deck, list(opponent.deck), AI_WEIGHTS[ai_id])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
