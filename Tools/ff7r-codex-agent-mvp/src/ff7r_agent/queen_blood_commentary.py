"""What happened since our last move, for narration that reacts (#182).

The playing agent narrates every Queen's Blood move. Left to the board alone
it described its own actions; these cues say what changed between the board
just after our move and the board at our next turn, so a line can react to
the opponent, a lane changing hands, a swing, or the match's prospects.

A lane is won by the side with more power there and scores that side's lane
power; a tied lane scores nothing. The victor also scores the lane-win bonus
of each of its cards in the lane (Posh Chocobo +3), and while a round-end
score-rule card is on the board the loser's lane power goes to the victor
too. The projection applies those rules to the board as it stands, as the
finish scorer does.

Pure: no game input, no telemetry reads.
"""
from __future__ import annotations

from typing import Any

from .queen_blood import BOARD_ROWS, QueenBloodError, QueenBloodState
from .queen_blood_ai import card_score_effect
from .queen_blood_catalogue import catalogue_card

LANE_NAMES = ("top", "middle", "bottom")
# A lane's margin moving this far in one exchange is worth a reaction.
SWING_POWER = 4
NOTABLE_SWING_POWER = 6
CLOSE_LANE_POWER = 1


def _cells(state: QueenBloodState) -> dict[tuple[int, int], Any]:
    return {(cell.row, cell.column): cell for cell in state.board}


def _same_card(previous: Any, cell: Any) -> bool:
    """The same side's card still holds the cell. A present card whose id is
    momentarily unreadable is the card that was there: live 2026-10-12 one
    read without ids reported our Security Officer destroyed (#326)."""
    return (previous is not None and cell is not None and previous.card_present and cell.card_present
            and previous.owner == cell.owner
            and (previous.card_id == cell.card_id or previous.card_id is None or cell.card_id is None))


def _lane_powers(state: QueenBloodState) -> dict[int, tuple[int, int]]:
    """Each lane's (player, opponent) power: the sum of the cards on the
    board, which the match is scored from. The game's lane totals can lag
    behind the cards (#329), so they count only while a placed card's power
    is unread."""
    summed = state.card_lane_powers()
    if summed is not None:
        return dict(enumerate(summed))
    shown = {lane.row: lane for lane in state.lanes}
    powers = {}
    for row in range(BOARD_ROWS):
        lane = shown.get(row)
        if lane is not None and lane.player_power is not None and lane.opponent_power is not None:
            powers[row] = (lane.player_power, lane.opponent_power)
            continue
        player = sum(cell.power or 0 for cell in state.board
                     if cell.row == row and cell.card_present and cell.owner == "player")
        opponent = sum(cell.power or 0 for cell in state.board
                       if cell.row == row and cell.card_present and cell.owner == "opponent")
        powers[row] = (player, opponent)
    return powers


def _leader(player: int, opponent: int) -> str:
    if player > opponent:
        return "player"
    if opponent > player:
        return "opponent"
    return "tied"


def projection(state: QueenBloodState) -> dict[str, Any]:
    """The score if the match ended now, lane by lane, card score effects
    included."""
    effects = {(cell.row, cell.column): (cell.owner, *card_score_effect(cell.card_id))
               for cell in state.board if cell.card_present and cell.owner in {"player", "opponent"}}
    score_rule = any(rule for _, _, rule in effects.values())
    lanes = []
    player_total = opponent_total = 0
    for row, (player, opponent) in sorted(_lane_powers(state).items()):
        leader = _leader(player, opponent)
        points = bonus = 0
        if leader != "tied":
            bonus = sum(effect_bonus for (cell_row, _), (owner, effect_bonus, _) in effects.items()
                        if cell_row == row and owner == leader)
            points = max(player, opponent) + (min(player, opponent) if score_rule else 0) + bonus
        if leader == "player":
            player_total += points
        elif leader == "opponent":
            opponent_total += points
        lane = {"lane": LANE_NAMES[row], "player_power": player, "opponent_power": opponent,
                "leader": leader, "margin": player - opponent, "points": points}
        if bonus:
            lane["score_bonus"] = bonus
        lanes.append(lane)
    return {"lanes": lanes, "player_score": player_total, "opponent_score": opponent_total,
            "leader": _leader(player_total, opponent_total), "margin": player_total - opponent_total}


def _name(card_id: str | None) -> str | None:
    card = catalogue_card(card_id)
    return getattr(card, "name", None) if card is not None else None


def exchange_baseline(pre_move: dict[str, Any] | None, post_move: dict[str, Any] | None) -> dict[str, Any] | None:
    """The board to compare our next turn with: the read our move returned,
    unless the opponent's reply had already landed in it. Then the board
    before our move is used, so the reply still counts and the exchange
    includes our own move. Live 2026-10-12 a placement read took 16.6 s
    and held Nene's Levrikon, and the commentary reported a pass (#357)."""
    if not isinstance(pre_move, dict) or not isinstance(post_move, dict):
        return post_move
    try:
        old, new = QueenBloodState.from_dict(pre_move), QueenBloodState.from_dict(post_move)
    except QueenBloodError:
        return post_move
    if old.match_id != new.match_id or not old.board:
        return post_move
    old_cells = _cells(old)
    replied = any(cell.owner == "opponent" and cell.card_present and not _same_card(old_cells.get(key), cell)
                  for key, cell in _cells(new).items())
    return pre_move if replied else post_move


def commentary_cues(before: dict[str, Any] | None, after: dict[str, Any],
                    player_passed: bool = False) -> dict[str, Any] | None:
    """Cues for the narration at our turn, comparing the board just after our
    last move (``before``) with the board now (``after``); None when there is
    nothing to compare (no earlier board, another match, unreadable boards).
    ``player_passed`` says our last move was a pass."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    try:
        old, new = QueenBloodState.from_dict(before), QueenBloodState.from_dict(after)
    except QueenBloodError:
        return None
    # Match ids can read null for every match, so a turn counter that went
    # back means a new match: live 2026-10-12 the first turn against Zahira
    # was compared with Ned's final board (#326).
    if old.match_id != new.match_id or new.turn < old.turn or not new.board:
        return None
    cues: list[dict[str, Any]] = []
    old_cells, new_cells = _cells(old), _cells(new)

    for key, cell in sorted(new_cells.items()):
        previous = old_cells.get(key)
        if cell.owner == "opponent" and cell.card_present and not _same_card(previous, cell):
            cues.append({"kind": "opponent_played", "card_id": cell.card_id, "card": _name(cell.card_id),
                         "lane": LANE_NAMES[cell.row], "column": cell.column, "power": cell.power})
    lost = [previous for key, previous in sorted(old_cells.items())
            if previous.owner == "player" and previous.card_present
            and not _same_card(previous, new_cells.get(key))]
    for previous in lost:
        cues.append({"kind": "player_card_lost", "card_id": previous.card_id, "card": _name(previous.card_id),
                     "lane": LANE_NAMES[previous.row], "column": previous.column})
    taken = sum(1 for key, cell in new_cells.items()
                if cell.owner == "opponent" and not cell.card_present
                and old_cells.get(key) is not None and old_cells[key].owner == "player")
    if taken:
        cues.append({"kind": "tiles_taken", "count": taken})

    old_lanes, new_lanes = _lane_powers(old), _lane_powers(new)
    for row in range(BOARD_ROWS):
        (old_player, old_opponent), (new_player, new_opponent) = old_lanes[row], new_lanes[row]
        before_leader, now_leader = _leader(old_player, old_opponent), _leader(new_player, new_opponent)
        swing = (new_player - new_opponent) - (old_player - old_opponent)
        if before_leader != now_leader:
            cues.append({"kind": "lane_lead_changed", "lane": LANE_NAMES[row], "from": before_leader,
                         "to": now_leader, "player_power": new_player, "opponent_power": new_opponent})
        elif abs(swing) >= SWING_POWER:
            cues.append({"kind": "lane_swing", "lane": LANE_NAMES[row], "swing": swing,
                         "player_power": new_player, "opponent_power": new_opponent})
        if (new_player or new_opponent) and abs(new_player - new_opponent) <= CLOSE_LANE_POWER:
            cues.append({"kind": "close_lane", "lane": LANE_NAMES[row],
                         "player_power": new_player, "opponent_power": new_opponent})

    was, now = projection(old), projection(new)
    if was["leader"] != now["leader"]:
        cues.append({"kind": "match_lead_changed", "from": was["leader"], "to": now["leader"],
                     "player_score": now["player_score"], "opponent_score": now["opponent_score"]})
    opponent_cards = lambda state: sorted((key, cell.card_id) for key, cell in _cells(state).items()
                                          if cell.owner == "opponent" and cell.card_present)
    # An unchanged board alone can be a repeat read or a dropped player input.
    # Only infer a pass after evidence that an opponent turn has elapsed.
    opponent_turn_elapsed = new.phase == "player_turn" and (
        old.phase == "opponent_turn" or new.turn > old.turn
    )
    # Two passes in a row end the match, so after our pass a turn of ours
    # means the opponent played: an unchanged board was read before their
    # card landed. Live 2026-10-12 (Michaela) that read claimed a pass twice
    # (#345). A present opponent card with an unread id or power is still
    # landing, so it proves nothing either.
    settled = all(cell.card_id is not None and cell.power is not None
                  for cell in new.board if cell.owner == "opponent" and cell.card_present)
    if (opponent_turn_elapsed and not player_passed and settled
            and opponent_cards(old) == opponent_cards(new)
            and not any(cue["kind"] == "opponent_played" for cue in cues)):
        cues.append({"kind": "opponent_passed"})
    notable = any(
        cue["kind"] in {"player_card_lost", "lane_lead_changed", "match_lead_changed"}
        or (cue["kind"] == "lane_swing" and abs(cue["swing"]) >= NOTABLE_SWING_POWER)
        for cue in cues)
    return {"notable": notable, "cues": cues, "projection": now,
            "projection_change": now["margin"] - was["margin"], "reaction": reaction(cues)}


# How a player would feel about what just happened, from the biggest event:
# the narrating agent opens its line with it in that tone. Owner 2026-10-06
# (Robin): a Deathwheel destroying our card, a counter and a won match drew
# no reaction at all; a player would be annoyed, then pleased.
def reaction(cues: list[dict[str, Any]]) -> dict[str, str] | None:
    """The reaction to open the next line with, as a tone and what it is
    about; None when nothing calls for one."""
    def first(kind: str, **match: Any) -> dict[str, Any] | None:
        return next((cue for cue in cues if cue["kind"] == kind
                     and all(cue.get(key) == value for key, value in match.items())), None)

    played = [cue.get("card") or "their card" for cue in cues if cue["kind"] == "opponent_played"]
    lost = first("player_card_lost")
    if lost is not None:
        by = f"{played[0]} " if played else "the opponent "
        return {"tone": "frustrated", "about": f"{by}destroyed your {lost.get('card') or 'card'} in the {lost['lane']} lane"}
    if first("match_lead_changed", to="opponent") is not None:
        return {"tone": "concerned", "about": "the opponent took the overall lead"}
    countered = first("lane_lead_changed", **{"from": "player"})
    if countered is not None:
        by = f" with {played[0]}" if played else ""
        return {"tone": "frustrated", "about": f"the opponent took back your {countered['lane']} lane{by}"}
    taken = first("match_lead_changed", to="player")
    if taken is not None:
        # "Back" only after being behind (live at Ka'dina: from a tie).
        about = "you are back in the overall lead" if taken["from"] == "opponent" else "you took the overall lead"
        return {"tone": "excited", "about": about}
    swing = next((cue for cue in cues if cue["kind"] == "lane_swing" and abs(cue["swing"]) >= NOTABLE_SWING_POWER), None)
    if swing is not None:
        if swing["swing"] > 0:
            return {"tone": "excited", "about": f"the {swing['lane']} lane swung your way"}
        return {"tone": "tense", "about": f"the opponent piled into the {swing['lane']} lane"}
    if first("opponent_passed") is not None:
        return {"tone": "confident", "about": "the opponent passed"}
    return None


def result_reaction(score: dict[str, Any] | None) -> dict[str, str] | None:
    """The reaction to the final score, for one line after the match."""
    if not isinstance(score, dict) or score.get("player") is None or score.get("opponent") is None:
        return None
    margin = int(score["player"]) - int(score["opponent"])
    if margin == 0:
        return {"tone": "surprised", "about": "the match ended in a draw"}
    if margin > 0:
        if margin <= 5:
            return {"tone": "relieved", "about": f"a narrow win, {score['player']} to {score['opponent']}"}
        return {"tone": "excited", "about": f"a win, {score['player']} to {score['opponent']}"}
    if margin >= -5:
        return {"tone": "frustrated", "about": f"a narrow loss, {score['player']} to {score['opponent']}"}
    return {"tone": "concerned", "about": f"a loss, {score['player']} to {score['opponent']}"}
