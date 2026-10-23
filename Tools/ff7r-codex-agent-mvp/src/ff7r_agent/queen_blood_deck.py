"""Match-local deck evidence, never inferred from inventory or observed cards."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any

from .queen_blood import QueenBloodState
from .queen_blood_catalogue import catalogue_card


class MatchDeck:
    def __init__(self, cards: list[str] | None = None, *, verification: dict[str, Any] | None = None):
        valid = (isinstance(cards, list) and len(cards) == 15 and verification
                 and all(isinstance(card, str) and catalogue_card(card) is not None for card in cards))
        self._cards = tuple(cards) if valid else None
        self.verification = deepcopy(verification) if self._cards is not None else None
        self._observed: Counter[str] = Counter()
        self._played: Counter[str] = Counter()
        self._placements: set[tuple[Any, ...]] = set()

    def observe(self, state: QueenBloodState) -> None:
        if not state.active or state.phase not in {"dealing", "player_turn", "opponent_turn", "result"}:
            return
        hand = Counter(card.card_id for card in state.hand if card.identified)
        board = Counter(cell.card_id for cell in state.board
                        if cell.card_present and cell.owner == "player" and cell.card_id)
        # Each snapshot is a lower bound, not a new draw. A mulligan can return
        # and redraw the same copy; a held-card preview is not a placement.
        counts = hand | board | self._played
        if not state.placement.active:
            counts |= hand + (board | self._played)
        self._observed |= counts

    def placement(self, action: dict[str, Any]) -> None:
        # Count consumed copies only after the normal placement gates prove
        # both commitment and departure from the hand. Ambiguous attempts and
        # repeated records cannot inflate the count.
        if (action.get("ok") is not True or action.get("verified") is not True
                or action.get("card_left_hand") is not True):
            return
        card = action.get("card_id")
        key = tuple(action.get(field) for field in
                    ("pre_sequence", "post_sequence", "row", "column", "card_id"))
        if not isinstance(card, str) or key in self._placements:
            return
        self._placements.add(key)
        self._played[card] += 1
        self._observed |= self._played

    def record(self) -> dict[str, Any]:
        expected = Counter(self._cards or ())
        contradictions = [{"card_id": card, "verified_copies": expected[card], "observed_copies": count}
                          for card, count in sorted(self._observed.items())
                          if self._cards is not None and count > expected[card]]
        status = ("unverified" if self._cards is None else
                  "contradicted" if contradictions else "verified")
        return {
            "deck": list(self._cards) if status == "verified" else None,
            "deck_provenance": {"status": status,
                                "verification": deepcopy(self.verification),
                                "observed_minimum_copies": dict(sorted(self._observed.items())),
                                "contradictions": contradictions},
        }
