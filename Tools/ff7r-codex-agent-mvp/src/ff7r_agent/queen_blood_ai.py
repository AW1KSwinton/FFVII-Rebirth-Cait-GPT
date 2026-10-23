"""Queen's Blood move selection from harness telemetry.

DEPRECATED for play (owner, 2026-09-29): matches are played with the
agent's own decisions, not this engine. Kept for reference; the operations
that use it refuse unless ``allow_automated_decisions`` is passed.

A deliberately small, deterministic player. It models the rules that decide
most positions -- pawn influence, placement cost and lane power -- and scores
each legal placement by the position it leaves. The game's own lane totals
and card powers are re-read before every decision.

Rules modelled (player's view, columns increase toward the opponent):

* A card may be placed on an empty cell the player owns whose pawn rank is
  at least the card's cost (the game's highlight is still required by the
  input layer before anything is sent).
* Placing adds the card's power to its row's lane total.
* For each influence offset of the card: a cell holding a card is unchanged;
  an opponent-owned pawn cell becomes the player's at its current rank; any
  other cell gains one pawn rank for the player (maximum 3).
* A lane scores for whichever side has more power in it (ties score nothing);
  the match is won on the sum of lane scores. The victor also receives the
  lane-win score bonus of each of its cards still in that lane (e.g. Posh
  Chocobo, +3), and while a round-end score-rule card is in play the loser's
  lane power is added to the victor's (effects from the generated catalogue,
  so opponent cards count too).
* Card abilities from the generated catalogue, for both sides:
  - power changes on the ability's tiles (effect_influence_offsets, filtered
    to allied / enemy / both) when played, and while in play: auras also
    reach cards placed on their tiles later and end when the source leaves;
  - reactions to cards being played (card_set_power_change) or destroyed
    (card_destroyed_power_change), count-based power
    (card_count_power_change), the first time a card is enhanced or
    enfeebled (triggered_power_change), and a card's own destruction
    (self_destroyed_effect / self_destroyed_add_card);
  - cards added to the hand (add_card_to_hand), which the look-ahead may
    play; replacement cards (sacrifice_*) that go onto an allied card and
    use its power;
  - lane-win score bonuses and the round-end score rule.
  A card at 0 power is destroyed; its tile reverts to a rank-1 pawn of its
  owner. Not modelled: card spawns into empty positions, extra board
  positions (additional_flag_effect, Fat Chocobo-style layouts) and the
  flag overwrite rule.

The chosen move maximises a blend of the position it leaves and the
position after the opponent's strongest reply. The opponent's hand is hidden:
replies are drawn from the opponent cards seen so far (their catalogue
influence, power and abilities), topped up with generic card profiles until
enough of the deck is known. After that reply the
player's best follow-up from the rest of the hand is also counted, so moves
that leave the remaining cards somewhere to go are preferred (live, both
lost matches ended with the player forced to pass on a full hand).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache

from .queen_blood_catalogue import card_effect
from .queen_blood import (
    BOARD_COLUMNS,
    BOARD_ROWS,
    MAX_PAWN_RANK,
    QueenBloodCard,
    QueenBloodState,
)

# Weights for the position value, tuned with queen_blood_sim against Turner
# (card_ai_easy01, which weighs the player's pawns at -2): held pawn cells
# dominate current lane points, because every card needs a pawn cell and
# losing the board strands the hand. Validated on a fresh seed over 200
# simulated matches: 60.5% wins (mean margin +1.7) versus 8% with the
# earlier lane-first weights (LANE_POINT 3.0, PAWN_CELL 0.8, REPLY 0.6).
LANE_POINT_WEIGHT = 0.75
LANE_MARGIN_WEIGHT = 0.3
LANE_MARGIN_CAP = 8
PAWN_CELL_WEIGHT = 5.0
PAWN_RANK_WEIGHT = 0.25
FORWARD_WEIGHT = 0.1
# Unknown opponent hand: (cost, power) profiles with a cross-shaped influence.
OPPONENT_PROFILES = ((1, 2), (2, 3), (3, 5))
CROSS_OFFSETS = ((-1, 0), (0, -1), (0, 1), (1, 0))
# Weight of the position after the opponent's best reply versus right after
# our move; below 1 because the profiles over-estimate a real hand.
REPLY_WEIGHT = 0.85
# Per remaining hand card that still has a legal cell after the reply.
MOBILITY_WEIGHT = 0.6
# Below this many distinct known opponent cards the generic profiles are
# still searched as well.
KNOWN_OPPONENT_CARDS_ENOUGH = 4
SACRIFICE_TYPES = frozenset({"sacrifice_for_allied_effect", "sacrifice_for_enemy_effect"})
# Value of a card an ability adds to a hand (beyond what the follow-up ply
# finds for it).
ADDED_CARD_WEIGHT = 0.5
ADDED_CARD_HAND_INDEX = 1000
LANE_WIN_BONUS_TYPE = "lane_win_score_bonus"
ROUND_END_SCORE_RULE_TYPE = "round_end_score_rule"


@dataclass(frozen=True)
class OpponentObjective:
    """An NPC's CardGameAIParam evaluation, from the NPC's side ("Enemy" in
    the game's naming): weights on each side's total lane power and pawn
    ranks. card_ai_easy01 (Turner) is PlayerPowerQT -1, EnemyPowerQT 2,
    PlayerFlagQT -2, EnemyFlagQT 1."""

    own_power: float = 2.0
    player_power: float = -1.0
    own_pawns: float = 1.0
    player_pawns: float = -2.0

    def score(self, model: "BoardModel") -> float:
        own_pawns = player_pawns = 0
        for cell in model.cells.values():
            if cell.card:
                continue
            if cell.owner == "opponent":
                own_pawns += cell.rank
            elif cell.owner == "player":
                player_pawns += cell.rank
        return (
            self.own_power * sum(model.opponent_lanes)
            + self.player_power * sum(model.player_lanes)
            + self.own_pawns * own_pawns
            + self.player_pawns * player_pawns
        )


CARD_AI_EASY01 = OpponentObjective()
# Share of the predicted-reply outcome (the move the NPC's own objective
# picks) versus the worst-case reply when the NPC's objective is known.
# Simulated, predicting the NPC's choice (from its whole deck, not its
# actual hand) won 49% against 60.5% for assuming the worst reply, so live
# play passes no objective.
PREDICTED_REPLY_SHARE = 1.0


def _score_effect(type_name: str | None, parameters: tuple[int, ...]) -> tuple[int, bool]:
    """(lane-win score bonus, round-end score rule) for one card's effect."""
    if type_name == LANE_WIN_BONUS_TYPE and parameters:
        return max(0, int(parameters[0])), False
    return 0, type_name == ROUND_END_SCORE_RULE_TYPE


def card_score_effect(card_id: str | None) -> tuple[int, bool]:
    """(lane-win score bonus, round-end score rule) for a catalogued card."""
    ref = card_effect(card_id)
    if ref is None:
        return 0, False
    return _score_effect(ref.effect_type_name, tuple(ref.effect_int_parameters))


@dataclass(frozen=True)
class PlannedAction:
    kind: str  # "place" or "pass"
    hand_index: int | None = None
    row: int | None = None
    column: int | None = None
    card_id: str | None = None
    name: str | None = None
    value: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "hand_index": self.hand_index,
            "row": self.row,
            "column": self.column,
            "card_id": self.card_id,
            "name": self.name,
            "value": round(self.value, 3),
            "reason": self.reason,
        }


@dataclass
class _Cell:
    owner: str
    rank: int
    card: bool
    # Static power: base plus every applied change (auras included), before
    # any count-based bonus, which is derived from the board on read.
    power: int = 0
    score_bonus: int = 0
    score_rule: bool = False
    card_ref: QueenBloodCard | None = None
    base_power: int = 0
    triggered: bool = False


def _relation_matches(target: str | None, source_owner: str, other_owner: str) -> bool:
    """Whether a card owned by ``other_owner`` is a target of an effect of a
    card owned by ``source_owner`` with the catalogue ``target`` filter."""
    ally = source_owner == other_owner
    if target == "allied_cards":
        return ally
    if target == "enemy_cards":
        return not ally
    return True


def _effect_type(cell: _Cell) -> str | None:
    return cell.card_ref.effect_type_name if cell.card and cell.card_ref is not None else None


def _params(cell: _Cell) -> tuple[int, ...]:
    return tuple(cell.card_ref.effect_int_parameters) if cell.card_ref is not None else ()


@dataclass
class BoardModel:
    cells: dict[tuple[int, int], _Cell] = field(default_factory=dict)
    player_lanes: list[int] = field(default_factory=lambda: [0] * BOARD_ROWS)
    opponent_lanes: list[int] = field(default_factory=lambda: [0] * BOARD_ROWS)
    # Lane power telemetry reports beyond the modelled cards, per side/row.
    lane_offsets: dict[str, list[int]] = field(
        default_factory=lambda: {"player": [0] * BOARD_ROWS, "opponent": [0] * BOARD_ROWS}
    )
    # While-in-play links: (source cell, target cell, delta applied).
    auras: list[tuple[tuple[int, int], tuple[int, int], int]] = field(default_factory=list)
    # Card ids added to each side's hand by abilities since the position.
    hand_additions: dict[str, list[str]] = field(
        default_factory=lambda: {"player": [], "opponent": []}
    )

    @classmethod
    def from_state(cls, state: QueenBloodState) -> "BoardModel":
        model = cls()
        for row in range(BOARD_ROWS):
            for column in range(BOARD_COLUMNS):
                model.cells[(row, column)] = _Cell("empty", 0, False)
        displayed: dict[tuple[int, int], int] = {}
        for cell in state.board:
            owner = cell.owner if cell.owner in {"player", "opponent"} else "empty"
            key = (cell.row, cell.column)
            if not cell.card_present:
                model.cells[key] = _Cell(owner, cell.rank or 0, False)
                continue
            ref = card_effect(cell.card_id)
            power = max(0, cell.power if cell.power is not None else (ref.power if ref and ref.power else 0))
            base = ref.power if ref is not None and ref.power is not None else power
            bonus, rule = (
                _score_effect(ref.effect_type_name, tuple(ref.effect_int_parameters))
                if ref is not None else (0, False)
            )
            model.cells[key] = _Cell(owner, 0, True, power, bonus, rule, ref, base)
            displayed[key] = power
        # Telemetry power already includes count bonuses: keep the static part.
        for key, power in displayed.items():
            model.cells[key].power = power - model._count_bonus(key)
        # Existing auras are already in the displayed powers; record the links
        # so they reach cards placed later and are undone if the source dies.
        for key, cell in model.cells.items():
            if _effect_type(cell) == "while_in_play_effect" and _params(cell):
                for target in model._tiles(key, cell.owner, cell.card_ref.effect_influence_offsets):
                    other = model.cells[target]
                    if other.card and _relation_matches(cell.card_ref.effect_target_name, cell.owner, other.owner):
                        model.auras.append((key, target, _params(cell)[0]))
            if _effect_type(cell) == "triggered_power_change":
                cell.triggered = model._trigger_condition(key)
        model._recompute_lanes()
        # The game's lane totals can lag behind the card powers in the same
        # snapshot (#329): they correct the model only while a placed card's
        # power is unread and its catalogue power stands in for it.
        if state.card_lane_powers() is not None:
            return model
        for row in range(BOARD_ROWS):
            lane = next((item for item in state.lanes if item.row == row), None)
            if lane is None:
                continue
            if lane.player_power is not None:
                model.lane_offsets["player"][row] = lane.player_power - model.player_lanes[row]
            if lane.opponent_power is not None:
                model.lane_offsets["opponent"][row] = lane.opponent_power - model.opponent_lanes[row]
        model._recompute_lanes()
        return model

    def copy(self) -> "BoardModel":
        return BoardModel(
            {
                key: _Cell(
                    cell.owner, cell.rank, cell.card, cell.power, cell.score_bonus,
                    cell.score_rule, cell.card_ref, cell.base_power, cell.triggered,
                )
                for key, cell in self.cells.items()
            },
            list(self.player_lanes),
            list(self.opponent_lanes),
            {side: list(values) for side, values in self.lane_offsets.items()},
            list(self.auras),
            {side: list(values) for side, values in self.hand_additions.items()},
        )

    # --- rules helpers -------------------------------------------------

    def _tiles(self, key: tuple[int, int], owner: str, offsets) -> list[tuple[int, int]]:
        mirror = 1 if owner == "player" else -1
        tiles = []
        for d_row, d_column in offsets or ():
            target = (key[0] + d_row, key[1] + mirror * d_column)
            if target in self.cells:
                tiles.append(target)
        return tiles

    def _status_counts(self, key: tuple[int, int], owner: str, target: str | None) -> tuple[int, int]:
        enhanced = enfeebled = 0
        for other_key, other in self.cells.items():
            if other_key == key or not other.card or not _relation_matches(target, owner, other.owner):
                continue
            if other.power > other.base_power:
                enhanced += 1
            elif other.power < other.base_power:
                enfeebled += 1
        return enhanced, enfeebled

    def _count_bonus(self, key: tuple[int, int]) -> int:
        """card_count_power_change: +parameter[0] power per other enhanced and
        +parameter[1] per other enfeebled card of the target side (in game,
        Valron (0, 2) reads "Raise power by 2 for each other enfeebled allied
        card")."""
        cell = self.cells[key]
        if _effect_type(cell) != "card_count_power_change":
            return 0
        params = _params(cell)
        per_enhanced = params[0] if len(params) > 0 else 0
        per_enfeebled = params[1] if len(params) > 1 else 0
        enhanced, enfeebled = self._status_counts(key, cell.owner, cell.card_ref.effect_target_name)
        return per_enhanced * enhanced + per_enfeebled * enfeebled

    def effective_power(self, key: tuple[int, int]) -> int:
        cell = self.cells[key]
        return cell.power + self._count_bonus(key) if cell.card else 0

    def _recompute_lanes(self) -> None:
        for side, lanes in (("player", self.player_lanes), ("opponent", self.opponent_lanes)):
            for row in range(BOARD_ROWS):
                lanes[row] = self.lane_offsets[side][row]
        for key, cell in self.cells.items():
            if not cell.card or cell.owner not in {"player", "opponent"}:
                continue
            lanes = self.player_lanes if cell.owner == "player" else self.opponent_lanes
            lanes[key[0]] += max(0, self.effective_power(key))

    def _change_tiles(
        self, source: tuple[int, int], owner: str, offsets, delta: int, target: str | None,
        *, link: bool = False,
    ) -> None:
        for tile in self._tiles(source, owner, offsets):
            other = self.cells[tile]
            if not other.card or tile == source or not _relation_matches(target, owner, other.owner):
                continue
            if delta <= -99:
                other.power = -99  # destroy
            else:
                other.power += delta
            if link:
                self.auras.append((source, tile, delta))

    def _trigger_condition(self, key: tuple[int, int]) -> bool:
        """triggered_power_change: (threshold, 0, delta) fires once power
        reaches the threshold (Cloud: "first reaches 7"); (0, b, 0, delta)
        fires the first time the card is enfeebled."""
        cell = self.cells[key]
        params = _params(cell)
        if not params:
            return False
        power = self.effective_power(key)
        if params[0] > 0:
            return power >= params[0] and power > cell.base_power
        return power < cell.base_power

    def _add_to_hand(self, side: str, card: QueenBloodCard) -> None:
        params = tuple(card.effect_int_parameters)
        for index, card_id in enumerate(card.effect_text_parameters or ()):
            count = params[index] if index < len(params) else 1
            self.hand_additions[side].extend([card_id] * max(0, count))

    def _remove_auras_of(self, key: tuple[int, int]) -> None:
        kept = []
        for source, target, delta in self.auras:
            if source == key:
                other = self.cells[target]
                if other.card and delta > -99:
                    other.power -= delta
            elif target != key:
                kept.append((source, target, delta))
        self.auras = kept

    def _destroy(self, key: tuple[int, int]) -> None:
        cell = self.cells[key]
        ref, owner = cell.card_ref, cell.owner
        self._remove_auras_of(key)
        cell.card = False
        cell.power = 0
        cell.base_power = 0
        cell.card_ref = None
        cell.score_bonus = 0
        cell.score_rule = False
        cell.triggered = False
        cell.rank = 1  # the tile reverts to a rank-1 pawn of the card's owner
        if ref is not None:
            params = tuple(ref.effect_int_parameters)
            if ref.effect_type_name == "self_destroyed_effect" and params:
                self._change_tiles(key, owner, ref.effect_influence_offsets, params[0], ref.effect_target_name)
            elif ref.effect_type_name == "self_destroyed_add_card":
                self._add_to_hand(owner, ref)
        for watcher_key, watcher in self.cells.items():
            if _effect_type(watcher) == "card_destroyed_power_change" and _params(watcher):
                if _relation_matches(watcher.card_ref.effect_target_name, watcher.owner, owner):
                    watcher.power += _params(watcher)[0]

    def _resolve(self) -> None:
        """Destroy cards at 0 power and fire first-time triggers until stable."""
        for _ in range(20):
            changed = False
            for key, cell in self.cells.items():
                if cell.card and self.effective_power(key) <= 0:
                    self._destroy(key)
                    changed = True
            for key, cell in self.cells.items():
                if _effect_type(cell) == "triggered_power_change" and not cell.triggered:
                    if self._trigger_condition(key):
                        cell.triggered = True
                        ref = cell.card_ref
                        self._change_tiles(
                            key, cell.owner, ref.effect_influence_offsets,
                            _params(cell)[-1], ref.effect_target_name,
                        )
                        changed = True
            if not changed:
                return

    # --- placement -----------------------------------------------------

    def legal_cells(self, card: QueenBloodCard, side: str = "player") -> list[tuple[int, int]]:
        if card.effect_type_name in SACRIFICE_TYPES:
            # Replacement cards go onto one of the side's own cards.
            return sorted(key for key, cell in self.cells.items() if cell.card and cell.owner == side)
        cost = card.cost if card.cost is not None else 1
        return sorted(
            key for key, cell in self.cells.items()
            if cell.owner == side and not cell.card and cell.rank >= cost
        )

    def place(self, card: QueenBloodCard, row: int, column: int, side: str = "player") -> "BoardModel":
        after = self.copy()
        after._play(card, side, (row, column))
        after._resolve()
        after._recompute_lanes()
        return after

    def place_opponent(self, power: int, row: int, column: int) -> "BoardModel":
        return self.place(_generic_card(power), row, column, side="opponent")

    def _play(self, card: QueenBloodCard, side: str, key: tuple[int, int]) -> None:
        other = "opponent" if side == "player" else "player"
        cell = self.cells[key]
        effect = card.effect_type_name
        params = tuple(card.effect_int_parameters)
        replaced_power = 0
        if effect in SACRIFICE_TYPES and cell.card and cell.owner == side:
            replaced_power = max(0, self.effective_power(key))
            self._remove_auras_of(key)
        cell.card = True
        cell.owner = side
        cell.rank = 0
        cell.card_ref = card
        cell.base_power = max(0, card.power or 0)
        cell.power = cell.base_power
        cell.triggered = False
        cell.score_bonus, cell.score_rule = _score_effect(effect, params)
        # Auras already covering this tile reach the new card.
        for source_key, source in self.cells.items():
            if source_key == key or _effect_type(source) != "while_in_play_effect" or not _params(source):
                continue
            ref = source.card_ref
            if key in self._tiles(source_key, source.owner, ref.effect_influence_offsets) and (
                _relation_matches(ref.effect_target_name, source.owner, side)
            ):
                cell.power += _params(source)[0]
                self.auras.append((source_key, key, _params(source)[0]))
        # Pawn influence.
        for tile in self._tiles(key, side, card.influence_offsets):
            pawn = self.cells[tile]
            if pawn.card:
                continue
            if pawn.owner == other:
                pawn.owner = side
            else:
                pawn.owner = side
                pawn.rank = min(MAX_PAWN_RANK, pawn.rank + 1)
        # The card's own ability.
        if effect == "immediate_effect" and params:
            self._change_tiles(key, side, card.effect_influence_offsets, params[0], card.effect_target_name)
        elif effect == "while_in_play_effect" and params:
            self._change_tiles(
                key, side, card.effect_influence_offsets, params[0], card.effect_target_name, link=True,
            )
        elif effect == "add_card_to_hand":
            self._add_to_hand(side, card)
        elif effect in SACRIFICE_TYPES and replaced_power:
            sign = 1 if effect == "sacrifice_for_allied_effect" else -1
            self._change_tiles(
                key, side, card.effect_influence_offsets, sign * replaced_power, card.effect_target_name,
            )
        # Cards that react to a card being played.
        for watcher_key, watcher in self.cells.items():
            if watcher_key == key or _effect_type(watcher) != "card_set_power_change" or not _params(watcher):
                continue
            if _relation_matches(watcher.card_ref.effect_target_name, watcher.owner, side):
                watcher.power += _params(watcher)[0]

    def opponent_replies(self, known: tuple[QueenBloodCard, ...] = ()) -> list["BoardModel"]:
        """Every opponent placement from this position: each known opponent
        card, plus the generic profiles while few cards are known."""
        replies = []
        for card in known:
            for row, column in self.legal_cells(card, "opponent"):
                replies.append(self.place(card, row, column, side="opponent"))
        if len(known) < KNOWN_OPPONENT_CARDS_ENOUGH:
            for cost, power in OPPONENT_PROFILES:
                for (row, column), cell in self.cells.items():
                    if cell.owner != "opponent" or cell.card or cell.rank < cost:
                        continue
                    replies.append(self.place_opponent(power, row, column))
        return replies

    def opponent_best_reply(self, known: tuple[QueenBloodCard, ...] = ()) -> "BoardModel":
        """The position after the opponent's strongest placement."""
        best = self
        best_value = self.value()
        for after in self.opponent_replies(known):
            value = after.value()
            if value < best_value:
                best, best_value = after, value
        return best

    def best_follow_up(self, hand: list[QueenBloodCard]) -> float:
        """Value after the player's best next placement from ``hand``, plus a
        bonus per hand card that still has somewhere to go."""
        best = self.value()
        playable = 0
        hand = list(hand) + self._added_cards("player")
        for card in hand:
            cells = self.legal_cells(card)
            if cells:
                playable += 1
            for row, column in cells:
                best = max(best, self.place(card, row, column).value())
        return best + MOBILITY_WEIGHT * playable

    def reply_then_follow_up(
        self,
        hand: list[QueenBloodCard],
        known: tuple[QueenBloodCard, ...] = (),
        objective: OpponentObjective | None = None,
    ) -> float:
        """The opponent's reply judged by the player's best answer.

        Without the opponent's objective the reply is the one worst for the
        player. With it, the reply the opponent's own evaluation prefers is
        predicted and blended with the worst case by PREDICTED_REPLY_SHARE.
        """
        replies = self.opponent_replies(known)
        if not replies:
            return self.best_follow_up(hand)
        if objective is None or PREDICTED_REPLY_SHARE <= 0.0:
            return min(reply.best_follow_up(hand) for reply in replies)
        predicted = max(replies, key=objective.score).best_follow_up(hand)
        if PREDICTED_REPLY_SHARE >= 1.0:
            return predicted
        worst = min(reply.best_follow_up(hand) for reply in replies)
        return PREDICTED_REPLY_SHARE * predicted + (1.0 - PREDICTED_REPLY_SHARE) * worst

    def _added_cards(self, side: str) -> list[QueenBloodCard]:
        """Cards abilities added to ``side``'s hand, playable from now on."""
        cards = []
        for index, card_id in enumerate(self.hand_additions[side]):
            card = card_effect(card_id)
            if card is not None:
                cards.append(replace(card, hand_index=ADDED_CARD_HAND_INDEX + index))
        return cards

    def lane_points(self) -> tuple[int, int]:
        """Match score if the round ended now, card score effects included."""
        score_rule = any(cell.card and cell.score_rule for cell in self.cells.values())
        mine = theirs = 0
        for row, (player, opponent) in enumerate(zip(self.player_lanes, self.opponent_lanes)):
            if player == opponent:
                continue
            victor = "player" if player > opponent else "opponent"
            points = max(player, opponent) + (min(player, opponent) if score_rule else 0)
            points += sum(
                cell.score_bonus for (cell_row, _), cell in self.cells.items()
                if cell_row == row and cell.card and cell.owner == victor
            )
            if victor == "player":
                mine += points
            else:
                theirs += points
        return mine, theirs

    def value(self) -> float:
        mine, theirs = self.lane_points()
        margin = sum(
            max(-LANE_MARGIN_CAP, min(LANE_MARGIN_CAP, player - opponent))
            for player, opponent in zip(self.player_lanes, self.opponent_lanes)
        )
        territory = 0.0
        for (_, column), cell in self.cells.items():
            if cell.card or cell.owner == "empty":
                continue
            weight = PAWN_CELL_WEIGHT + PAWN_RANK_WEIGHT * (cell.rank - 1)
            if cell.owner == "player":
                territory += weight + FORWARD_WEIGHT * column
            else:
                territory -= weight + FORWARD_WEIGHT * (BOARD_COLUMNS - 1 - column)
        cards = ADDED_CARD_WEIGHT * (
            len(self.hand_additions["player"]) - len(self.hand_additions["opponent"])
        )
        return LANE_POINT_WEIGHT * (mine - theirs) + LANE_MARGIN_WEIGHT * margin + territory + cards


@lru_cache(maxsize=16)
def _generic_card(power: int) -> QueenBloodCard:
    return QueenBloodCard(
        hand_index=0, card_id=f"generic_power_{power}", power=power, influence_offsets=CROSS_OFFSETS,
    )


def choose_action(
    state: QueenBloodState,
    *,
    opponent_passed: bool = False,
    opponent_cards: tuple[QueenBloodCard, ...] | list[QueenBloodCard] = (),
    opponent_objective: OpponentObjective | None = None,
) -> PlannedAction:
    """Pick the placement that leaves the best position, or pass.

    Passing is chosen when nothing is playable, or when the opponent has just
    passed and the player already leads on lane points (passing then ends the
    match as a win).
    """
    hand = [card for card in state.hand if card.playable is not False and card.identified]
    return decide(
        BoardModel.from_state(state), hand,
        opponent_passed=opponent_passed, opponent_cards=opponent_cards,
        opponent_objective=opponent_objective,
    )


def decide(
    model: BoardModel,
    hand: list[QueenBloodCard],
    *,
    opponent_passed: bool = False,
    opponent_cards: tuple[QueenBloodCard, ...] | list[QueenBloodCard] = (),
    opponent_objective: OpponentObjective | None = None,
) -> PlannedAction:
    """The decision on a board model; shared by live play and simulation."""
    known = _distinct_cards(opponent_cards)
    mine, theirs = model.lane_points()
    if opponent_passed and mine > theirs:
        return PlannedAction(
            "pass", value=model.value(),
            reason=f"opponent passed and the player leads {mine}-{theirs}",
        )
    best: PlannedAction | None = None
    for card in hand:
        rest = [item for item in hand if item.hand_index != card.hand_index]
        for row, column in model.legal_cells(card):
            after = model.place(card, row, column)
            value = (
                (1.0 - REPLY_WEIGHT) * after.value()
                + REPLY_WEIGHT * after.reply_then_follow_up(rest, known, opponent_objective)
                + 0.01 * (card.power or 0)
            )
            if best is None or value > best.value:
                best = PlannedAction(
                    "place", card.hand_index, row, column, card.card_id, card.name, value,
                    reason="best position value",
                )
    if best is None:
        return PlannedAction("pass", value=model.value(), reason="no playable card has a legal cell")
    return best


def _distinct_cards(cards) -> tuple[QueenBloodCard, ...]:
    seen: dict[str, QueenBloodCard] = {}
    for card in cards or ():
        if card is not None and card.card_id not in seen:
            seen[card.card_id] = card
    return tuple(seen.values())


def choose_mulligan(state: QueenBloodState) -> list[int]:
    """Opening-hand replacements: cards that need a rank-3 cell to play."""
    return sorted(card.hand_index for card in mulligan_cards(state.hand))


def mulligan_cards(hand) -> list[QueenBloodCard]:
    return [card for card in hand if (card.cost or 0) >= 3]
