from __future__ import annotations

"""Validated Queen's Blood telemetry and bounded UI-turn planning.

The game-side probe owns object discovery.  This module deliberately consumes
only the small, JSON-safe contract emitted by that probe; it does not depend on
UE4SS objects or guess card-game state from screenshots.
"""

from dataclasses import dataclass
import math
from typing import Any


class QueenBloodError(ValueError):
    """Raised when a card-game snapshot cannot safely drive an action."""


QUEEN_BLOOD_SCHEMA_VERSION = 1
QUEEN_BLOOD_PHASES = frozenset({
    "inactive",
    "entering",
    "dealing",
    "player_turn",
    "opponent_turn",
    "resolving",
    "result",
    "unknown",
})
QUEEN_BLOOD_OWNERS = frozenset({"empty", "neutral", "player", "opponent", "contested", "unknown"})
QUEEN_BLOOD_PREMATCH_SCREENS = frozenset({
    "prematch", "pre_match", "match_menu", "begin_match", "start_match", "deck_select",
})
BOARD_ROWS = 3
BOARD_COLUMNS = 5
# CardGame_Test_C authors a 15-widget CardFull pool for the hand.
MAX_HAND_SIZE = 15
MAX_PAWN_RANK = 3
UNIDENTIFIED_CARD_PREFIX = "unidentified_hand_slot_"
MAX_LEGAL_MOVES = 128
MAX_INPUT_STEPS = 32
MULLIGAN_HAND_SIZE = 5
MULLIGAN_MAX_CARD_COUNT = MULLIGAN_HAND_SIZE


def identified_card_id(value: Any) -> bool:
    """A slot placeholder is occupancy evidence, never a card identity."""
    return isinstance(value, str) and bool(value.strip()) and not value.strip().startswith(UNIDENTIFIED_CARD_PREFIX)


def _cell_coordinate(value: Any) -> int | None:
    # The probe's JSON numbers are Lua numbers, so 0 arrives as 0.0.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if float(value).is_integer() else None


def match_identity_gaps(payload: dict[str, Any]) -> list[str]:
    """Report incomplete live match reads without filling from an older board.

    Prematch/mulligan/result screens have different availability contracts.
    Only the ordinary board phases require all 15 cells and fresh identities
    and powers for occupied cells. Hand positions may include token cards.
    """
    if payload.get("phase") not in {"player_turn", "opponent_turn", "resolving"}:
        return []
    board = payload.get("board")
    cells = board.get("cells") if isinstance(board, dict) else None
    gaps = []
    positions = set()
    for cell in cells if isinstance(cells, list) else []:
        if not isinstance(cell, dict):
            continue
        row, column = _cell_coordinate(cell.get("row")), _cell_coordinate(cell.get("column"))
        if row is not None and column is not None and 0 <= row < 3 and 0 <= column < 5:
            positions.add((row, column))
        if cell.get("card_present", cell.get("card_id") is not None):
            if not identified_card_id(cell.get("card_id")):
                gaps.append(f"board[{row},{column}].card_id")
            if cell.get("power") is None:
                gaps.append(f"board[{row},{column}].power")
    if len(positions) != BOARD_ROWS * BOARD_COLUMNS:
        gaps.append("board.cells")
    for card in payload.get("hand") or []:
        if not isinstance(card, dict) or not identified_card_id(card.get("card_id")):
            index = card.get("hand_index") if isinstance(card, dict) else None
            gaps.append(f"hand[{index}].card_id")
    return gaps


def _finite_int(value: Any, name: str, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise QueenBloodError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise QueenBloodError(f"{name} must be an integer") from exc
    if result < minimum or (maximum is not None and result > maximum):
        bound = f" between {minimum} and {maximum}" if maximum is not None else f" >= {minimum}"
        raise QueenBloodError(f"{name} must be{bound}")
    return result


def _optional_int(value: Any, name: str, minimum: int = 0, maximum: int | None = None) -> int | None:
    return None if value is None else _finite_int(value, name, minimum, maximum)


def _text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise QueenBloodError(f"{name} must be non-empty text")
    return value


def _optional_bool(value: Any, name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise QueenBloodError(f"{name} must be Boolean when present")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _optional_float(value: Any, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise QueenBloodError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise QueenBloodError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise QueenBloodError(f"{name} must be finite")
    return result


def _text_tuple(value: Any, name: str, maximum: int = 8) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > maximum:
        raise QueenBloodError(f"{name} must be a bounded array")
    return tuple(_text(item, f"{name} item") for item in value)


def _int_tuple(value: Any, name: str, maximum: int = 8) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > maximum:
        raise QueenBloodError(f"{name} must be a bounded array")
    return tuple(
        _finite_int(item, f"{name} item", -1000000000, 1000000000)
        for item in value
    )


def _offset_tuple(value: Any, name: str, maximum: int = 25) -> tuple[tuple[int, int], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > maximum:
        raise QueenBloodError(f"{name} must be a bounded array")
    offsets: list[tuple[int, int]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise QueenBloodError(f"{name} item {index} must be an object")
        offsets.append(
            (
                _finite_int(item.get("row"), f"{name} item {index} row", -2, 2),
                _finite_int(item.get("column"), f"{name} item {index} column", -2, 2),
            )
        )
    if len(set(offsets)) != len(offsets):
        raise QueenBloodError(f"{name} must not contain duplicate offsets")
    return tuple(offsets)


def _position(value: Any) -> tuple[int, int]:
    if isinstance(value, dict):
        row = value.get("row")
        column = value.get("column", value.get("col"))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        row, column = value
    else:
        raise QueenBloodError("board position must contain row and column")
    return (
        _finite_int(row, "row", 0, BOARD_ROWS - 1),
        _finite_int(column, "column", 0, BOARD_COLUMNS - 1),
    )


def _optional_location(value: Any, label: str) -> tuple[float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise QueenBloodError(f"{label} must be an object")
    try:
        point = tuple(float(value[axis]) for axis in ("x", "y", "z"))
    except (KeyError, TypeError, ValueError) as exc:
        raise QueenBloodError(f"{label} needs numeric x, y and z") from exc
    if not all(math.isfinite(item) for item in point):
        raise QueenBloodError(f"{label} must be finite")
    return point  # type: ignore[return-value]


@dataclass(frozen=True)
class QueenBloodCell:
    row: int
    column: int
    owner: str = "empty"
    card_id: str | None = None
    power: int | None = None
    control: int | None = None
    # Pawn rank (1-3) on an unoccupied cell; 0 when a card occupies it.
    rank: int | None = None
    card_present: bool = False
    # The game lights the cells the focused hand card may be placed on.
    highlight: bool = False
    # World location of the placed card's actor (the cell centre), if any.
    location: tuple[float, float, float] | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "QueenBloodCell":
        if not isinstance(value, dict):
            raise QueenBloodError("board cell must be an object")
        row, column = _position(value)
        owner = str(value.get("owner", value.get("side", "empty"))).lower()
        if owner not in QUEEN_BLOOD_OWNERS:
            raise QueenBloodError(f"unknown board owner: {owner}")
        card_id = value.get("card_id", value.get("id"))
        if card_id is not None:
            card_id = _text(card_id, "card_id")
        card_present = value.get("card_present", card_id is not None)
        highlight = value.get("highlight", False)
        if not isinstance(card_present, bool) or not isinstance(highlight, bool):
            raise QueenBloodError("board cell card_present/highlight must be Boolean")
        return cls(
            row,
            column,
            owner,
            card_id,
            _optional_int(value.get("power"), "power", -99, 99),
            _optional_int(value.get("control"), "control"),
            _optional_int(value.get("rank"), "rank", 0, MAX_PAWN_RANK),
            card_present,
            highlight,
            _optional_location(value.get("location"), "cell location"),
        )


@dataclass(frozen=True)
class QueenBloodCard:
    hand_index: int
    card_id: str
    name: str | None = None
    mulligan_selected: bool | None = None
    cost: int | None = None
    power: int | None = None
    legal_positions: tuple[tuple[int, int], ...] = ()
    influence: str | None = None
    note_id: str | None = None
    effect_id: str | None = None
    effect_text_id: str | None = None
    effect_influence: str | None = None
    effect_constant: float | None = None
    effect_target: int | None = None
    effect_type: int | None = None
    effect_int_parameters: tuple[int, ...] = ()
    effect_text_parameters: tuple[str, ...] = ()
    effect_reference_text_id: str | None = None
    influence_square_count: int | None = None
    influence_offsets: tuple[tuple[int, int], ...] = ()
    effect_influence_square_count: int | None = None
    effect_influence_offsets: tuple[tuple[int, int], ...] = ()
    effect_text: str | None = None
    effect_text_source: str | None = None
    effect_type_name: str | None = None
    effect_target_name: str | None = None
    effect_text_label_references: tuple[str, ...] = ()
    effect_heading_types: tuple[int, ...] = ()
    effect_parameter_references: tuple[int, ...] = ()
    effect_reference_counts: tuple[int, ...] = ()
    effect_filter_tags: tuple[int, ...] = ()
    effect_reference_count0: int | None = None
    # Hand actor bChooseableVisible: the card has at least one legal cell.
    playable: bool | None = None
    # CardFull pool widget the card is drawn on (not its displayed position).
    card_index: int | None = None

    @property
    def identified(self) -> bool:
        return not self.card_id.startswith(UNIDENTIFIED_CARD_PREFIX)

    @classmethod
    def from_dict(cls, value: Any, fallback_index: int) -> "QueenBloodCard":
        if not isinstance(value, dict):
            raise QueenBloodError("hand card must be an object")
        index = _finite_int(value.get("hand_index", value.get("index", fallback_index)), "hand_index", 0, MAX_HAND_SIZE - 1)
        raw_id = value.get("card_id", value.get("id"))
        if raw_id is None or (isinstance(raw_id, str) and not raw_id.strip()):
            # A card the probe could not identify (live 2026-09-25: the two
            # cards Moogle Trio adds to the hand). It keeps its hand slot so
            # navigation stays correct; the AI does not play it.
            card_id = f"{UNIDENTIFIED_CARD_PREFIX}{index}"
        else:
            card_id = _text(raw_id, "card_id")
        name = value.get("name")
        if name is not None:
            name = _text(name, "card name")
        mulligan_selected = value.get("mulligan_selected")
        if mulligan_selected is not None and not isinstance(mulligan_selected, bool):
            raise QueenBloodError("mulligan_selected must be Boolean when present")
        raw_positions = value.get("legal_positions", value.get("placements", value.get("positions", [])))
        if not isinstance(raw_positions, list):
            raise QueenBloodError("card legal_positions must be an array")
        positions = tuple(_position(item) for item in raw_positions)
        influence_square_count = _optional_int(
            value.get("influence_square_count"), "influence_square_count", 0, 25
        )
        influence_offsets = _offset_tuple(value.get("influence_offsets"), "influence_offsets")
        effect_influence_square_count = _optional_int(
            value.get("effect_influence_square_count"),
            "effect_influence_square_count",
            0,
            25,
        )
        effect_influence_offsets = _offset_tuple(
            value.get("effect_influence_offsets"), "effect_influence_offsets"
        )
        if influence_square_count is not None and influence_square_count != len(influence_offsets):
            raise QueenBloodError("influence_square_count does not match influence_offsets")
        if (
            effect_influence_square_count is not None
            and effect_influence_square_count != len(effect_influence_offsets)
        ):
            raise QueenBloodError(
                "effect_influence_square_count does not match effect_influence_offsets"
            )
        return cls(
            index,
            card_id,
            name,
            mulligan_selected,
            _optional_int(value.get("cost"), "cost"),
            _optional_int(value.get("power"), "power"),
            positions,
            _optional_text(value.get("influence"), "influence"),
            _optional_text(value.get("note_id"), "note_id"),
            _optional_text(value.get("effect_id"), "effect_id"),
            _optional_text(value.get("effect_text_id"), "effect_text_id"),
            _optional_text(value.get("effect_influence"), "effect_influence"),
            _optional_float(value.get("effect_constant"), "effect_constant"),
            _optional_int(value.get("effect_target"), "effect_target", -128, 127),
            _optional_int(value.get("effect_type"), "effect_type", -128, 127),
            _int_tuple(value.get("effect_int_parameters"), "effect_int_parameters"),
            _text_tuple(value.get("effect_text_parameters"), "effect_text_parameters"),
            _optional_text(value.get("effect_reference_text_id"), "effect_reference_text_id"),
            influence_square_count,
            influence_offsets,
            effect_influence_square_count,
            effect_influence_offsets,
            _optional_text(value.get("effect_text"), "effect_text"),
            _optional_text(value.get("effect_text_source"), "effect_text_source"),
            _optional_text(value.get("effect_type_name"), "effect_type_name"),
            _optional_text(value.get("effect_target_name"), "effect_target_name"),
            _text_tuple(
                value.get("effect_text_label_references"),
                "effect_text_label_references",
            ),
            _int_tuple(value.get("effect_heading_types"), "effect_heading_types"),
            _int_tuple(
                value.get("effect_parameter_references"),
                "effect_parameter_references",
            ),
            _int_tuple(value.get("effect_reference_counts"), "effect_reference_counts"),
            _int_tuple(value.get("effect_filter_tags"), "effect_filter_tags"),
            _optional_int(
                value.get("effect_reference_count0"),
                "effect_reference_count0",
                0,
                255,
            ),
            _optional_bool(value.get("playable"), "playable"),
            _optional_int(value.get("card_index"), "card_index", -1, 64),
        )


@dataclass(frozen=True)
class QueenBloodMove:
    hand_index: int
    row: int
    column: int
    card_id: str | None = None
    score: float | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "QueenBloodMove":
        if not isinstance(value, dict):
            raise QueenBloodError("legal move must be an object")
        if value.get("legal", True) is not True:
            raise QueenBloodError("move is not legal")
        row, column = _position(value.get("position", value))
        hand_index = _finite_int(
            value.get("hand_index", value.get("card_index", value.get("card_slot"))),
            "hand_index",
            0,
            MAX_HAND_SIZE - 1,
        )
        card_id = value.get("card_id", value.get("id"))
        if card_id is not None:
            card_id = _text(card_id, "card_id")
        score = value.get("score", value.get("priority"))
        if score is not None:
            try:
                score = float(score)
            except (TypeError, ValueError) as exc:
                raise QueenBloodError("move score must be numeric") from exc
            if not math.isfinite(score):
                raise QueenBloodError("move score must be finite")
        return cls(hand_index, row, column, card_id, score)

    def key(self) -> tuple[int, int, int]:
        return self.hand_index, self.row, self.column


@dataclass(frozen=True)
class QueenBloodCursor:
    area: str
    hand_index: int | None = None
    row: int | None = None
    column: int | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "QueenBloodCursor":
        if not isinstance(value, dict):
            raise QueenBloodError("cursor must be an object")
        area = str(value.get("area", value.get("focus", "unknown"))).lower()
        if area not in {"hand", "board", "unknown"}:
            raise QueenBloodError(f"unknown cursor area: {area}")
        hand_index = _optional_int(value.get("hand_index", value.get("index")), "cursor hand_index", 0, MAX_HAND_SIZE - 1)
        row = _optional_int(value.get("row"), "cursor row", 0, BOARD_ROWS - 1)
        column = _optional_int(value.get("column", value.get("col")), "cursor column", 0, BOARD_COLUMNS - 1)
        return cls(area, hand_index, row, column)


@dataclass(frozen=True)
class QueenBloodLane:
    """One row's power totals as the game shows them (manager token counts)."""
    row: int
    player_power: int | None = None
    opponent_power: int | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "QueenBloodLane":
        if not isinstance(value, dict):
            raise QueenBloodError("board lane must be an object")
        return cls(
            _finite_int(value.get("row"), "lane row", 0, BOARD_ROWS - 1),
            _optional_int(value.get("player_power"), "lane player_power", 0, 999),
            _optional_int(value.get("opponent_power"), "lane opponent_power", 0, 999),
        )


@dataclass(frozen=True)
class QueenBloodPlacement:
    """A picked-up card being positioned on the board, and its target cell."""
    active: bool = False
    # Target from the game's placement preview (only on cells the card may use).
    target_row: int | None = None
    target_column: int | None = None
    # The cell under the placement cursor from the CardFrameCursor widgets,
    # observable on any cell.
    cursor_row: int | None = None
    cursor_column: int | None = None
    # World location of the held card, which hovers over the cursor cell.
    actor_location: tuple[float, float, float] | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "QueenBloodPlacement":
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise QueenBloodError("board placement must be an object")
        active = value.get("active", False)
        if not isinstance(active, bool):
            raise QueenBloodError("placement active must be Boolean")
        return cls(
            active,
            _optional_int(value.get("target_row"), "placement target_row", 0, BOARD_ROWS - 1),
            _optional_int(value.get("target_column"), "placement target_column", 0, BOARD_COLUMNS - 1),
            _optional_int(value.get("cursor_row"), "placement cursor_row", 0, BOARD_ROWS - 1),
            _optional_int(value.get("cursor_column"), "placement cursor_column", 0, BOARD_COLUMNS - 1),
            _optional_location(value.get("actor_location"), "placement actor_location"),
        )

    @property
    def target(self) -> tuple[int, int] | None:
        if self.target_row is None or self.target_column is None:
            return None
        return self.target_row, self.target_column

    @property
    def position(self) -> tuple[int, int] | None:
        """Best observed cursor cell: the preview target, else the cursor widget."""
        if self.target is not None:
            return self.target
        if self.cursor_row is None or self.cursor_column is None:
            return None
        return self.cursor_row, self.cursor_column


@dataclass(frozen=True)
class QueenBloodState:
    active: bool
    phase: str
    turn: int
    match_id: str | None
    cursor: QueenBloodCursor | None
    hand: tuple[QueenBloodCard, ...]
    legal_moves: tuple[QueenBloodMove, ...]
    board: tuple[QueenBloodCell, ...]
    player_score: int | None = None
    opponent_score: int | None = None
    winner: str | None = None
    screen: str | None = None
    begin_match_available: bool = False
    match_screen: bool = False
    lanes: tuple[QueenBloodLane, ...] = ()
    placement: QueenBloodPlacement = QueenBloodPlacement()

    def cell(self, row: int, column: int) -> QueenBloodCell | None:
        for item in self.board:
            if item.row == row and item.column == column:
                return item
        return None

    def card_lane_powers(self) -> tuple[tuple[int, int], ...] | None:
        """Each lane's (player, opponent) power summed from the cards on the
        board, or None without a board or while a placed card's power is
        unread.

        The game's own lane totals (``lanes``, its token counters) can lag
        behind the card powers in the same snapshot: live 2026-10-12 the
        middle lane read 7-6 while its cards summed 7-8, and the match was
        lost 4-17 as the cards said (#329)."""
        if not self.board:
            return None
        powers = [[0, 0] for _ in range(BOARD_ROWS)]
        for cell in self.board:
            if not cell.card_present or cell.owner not in {"player", "opponent"}:
                continue
            if cell.power is None:
                return None
            powers[cell.row][0 if cell.owner == "player" else 1] += cell.power
        return tuple((player, opponent) for player, opponent in powers)

    @classmethod
    def from_dict(cls, value: Any) -> "QueenBloodState":
        if not isinstance(value, dict):
            raise QueenBloodError("queen_blood telemetry must be an object")
        missing = sorted(
            key for key in ("schema_version", "active", "phase", "cursor", "hand", "legal_moves", "board")
            if key not in value
        )
        if missing:
            raise QueenBloodError(f"queen_blood telemetry is missing fields: {', '.join(missing)}")
        schema_version = value.get("schema_version")
        if _finite_int(schema_version, "queen_blood schema_version", 1, QUEEN_BLOOD_SCHEMA_VERSION) != QUEEN_BLOOD_SCHEMA_VERSION:
            raise QueenBloodError("unsupported queen_blood schema_version")
        active = value.get("active", False)
        if not isinstance(active, bool):
            raise QueenBloodError("queen_blood active must be Boolean")
        phase = str(value.get("phase", "inactive" if not active else "unknown")).lower()
        if phase not in QUEEN_BLOOD_PHASES:
            raise QueenBloodError(f"unknown queen_blood phase: {phase}")
        if not active and phase not in {"inactive", "unknown", "result"}:
            raise QueenBloodError("inactive queen_blood telemetry has an active phase")
        match_id = value.get("match_id", value.get("game_id"))
        if match_id is not None:
            match_id = _text(match_id, "match_id")
        screen = value.get("screen", value.get("ui_state"))
        if screen is not None:
            screen = _text(screen, "screen").strip().lower()
        begin_match_available = value.get("begin_match_available", False)
        match_screen = value.get("match_screen", False)
        if not isinstance(begin_match_available, bool):
            raise QueenBloodError("begin_match_available must be Boolean")
        if not isinstance(match_screen, bool):
            raise QueenBloodError("match_screen must be Boolean")
        raw_hand = value.get("hand", [])
        if not isinstance(raw_hand, list) or len(raw_hand) > MAX_HAND_SIZE:
            raise QueenBloodError("queen_blood hand must be a bounded array")
        hand = tuple(QueenBloodCard.from_dict(item, index) for index, item in enumerate(raw_hand))
        indices = [card.hand_index for card in hand]
        if len(indices) != len(set(indices)):
            raise QueenBloodError("queen_blood hand indices must be unique")
        raw_moves = value.get("legal_moves", value.get("moves", []))
        if not isinstance(raw_moves, list) or len(raw_moves) > MAX_LEGAL_MOVES:
            raise QueenBloodError("queen_blood legal_moves must be a bounded array")
        moves = tuple(QueenBloodMove.from_dict(item) for item in raw_moves)
        if len({move.key() for move in moves}) != len(moves):
            raise QueenBloodError("queen_blood legal moves must be unique")
        raw_board = value.get("board", {})
        raw_cells = raw_board.get("cells", []) if isinstance(raw_board, dict) else raw_board
        if not isinstance(raw_cells, list):
            raise QueenBloodError("queen_blood board cells must be an array")
        if isinstance(raw_board, dict):
            rows = _finite_int(raw_board.get("rows", BOARD_ROWS), "board rows", BOARD_ROWS, BOARD_ROWS)
            columns = _finite_int(raw_board.get("columns", BOARD_COLUMNS), "board columns", BOARD_COLUMNS, BOARD_COLUMNS)
            if rows != BOARD_ROWS or columns != BOARD_COLUMNS:
                raise QueenBloodError("queen_blood board dimensions must be 3x5")
        board = tuple(QueenBloodCell.from_dict(item) for item in raw_cells)
        raw_lanes = raw_board.get("lanes", []) if isinstance(raw_board, dict) else []
        if not isinstance(raw_lanes, list) or len(raw_lanes) > BOARD_ROWS:
            raise QueenBloodError("queen_blood board lanes must be a bounded array")
        lanes = tuple(QueenBloodLane.from_dict(item) for item in raw_lanes)
        placement = QueenBloodPlacement.from_dict(
            raw_board.get("placement") if isinstance(raw_board, dict) else None
        )
        board_keys = [(cell.row, cell.column) for cell in board]
        if len(board_keys) != len(set(board_keys)):
            raise QueenBloodError("queen_blood board cells must be unique")
        cursor = QueenBloodCursor.from_dict(value["cursor"]) if value.get("cursor") is not None else None
        result = value.get("result")
        winner = value.get("winner", result.get("winner") if isinstance(result, dict) else None)
        if winner is not None:
            winner = _text(winner, "winner").lower()
        return cls(
            active,
            phase,
            _finite_int(value.get("turn", 0), "turn"),
            match_id,
            cursor,
            hand,
            moves,
            board,
            _optional_int(value.get("player_score"), "player_score"),
            _optional_int(value.get("opponent_score"), "opponent_score"),
            winner,
            screen,
            begin_match_available,
            match_screen,
            lanes,
            placement,
        )

    def choose_move(self) -> QueenBloodMove:
        if not self.active:
            raise QueenBloodError("Queen's Blood is not active")
        if self.phase != "player_turn":
            raise QueenBloodError(f"Queen's Blood is not awaiting the player: {self.phase}")
        if not self.legal_moves:
            raise QueenBloodError("no legal Queen's Blood move was exported")
        hand_by_index = {card.hand_index: card for card in self.hand}

        def rank(move: QueenBloodMove) -> tuple[float, int, int, int, int]:
            card = hand_by_index.get(move.hand_index)
            explicit_score = move.score if move.score is not None else float("-inf")
            power = card.power if card and card.power is not None else 0
            cost = card.cost if card and card.cost is not None else 0
            center_bias = -abs(move.column - (BOARD_COLUMNS - 1) / 2.0)
            return explicit_score, power - cost, int(center_bias * 100), -move.hand_index, -move.row

        return max(self.legal_moves, key=rank)


@dataclass(frozen=True)
class QueenBloodInputStep:
    button: str
    reason: str
    hand_index: int | None = None


def _axis_steps(current: int, target: int, negative: str, positive: str, reason: str) -> list[QueenBloodInputStep]:
    button = positive if target > current else negative
    return [QueenBloodInputStep(button, reason) for _ in range(abs(target - current))]


def plan_queen_blood_card_navigation(
    current_cursor_index: int,
    target_index: int,
    hand_size: int = MULLIGAN_HAND_SIZE,
) -> tuple[QueenBloodInputStep, ...]:
    """Plan bounded hand navigation from an observed cursor slot.

    The caller supplies the cursor slot from fresh telemetry for each attempt;
    this helper emits only directional hand-navigation input. It deliberately
    emits no mulligan marker, card-play confirm, or board-placement input.
    """
    # The hand holds five cards at the mulligan but grows during a match
    # (live 2026-09-25: Moogle Trio added two cards, making seven).
    if not isinstance(hand_size, int) or not MULLIGAN_HAND_SIZE <= hand_size <= MAX_HAND_SIZE:
        raise QueenBloodError("hand size is outside the supported range")
    if not isinstance(current_cursor_index, int) or isinstance(current_cursor_index, bool):
        raise QueenBloodError("mulligan current cursor index must be an integer")
    if not 0 <= current_cursor_index < hand_size:
        raise QueenBloodError(f"current cursor index is outside the {hand_size}-card hand")
    if not isinstance(target_index, int) or isinstance(target_index, bool):
        raise QueenBloodError("mulligan target index must be an integer")
    if not 0 <= target_index < hand_size:
        raise QueenBloodError(f"target index is outside the {hand_size}-card hand")
    steps = _axis_steps(
        current_cursor_index,
        target_index,
        "queen_blood_left",
        "queen_blood_right",
        "hover_mulligan_card",
    )
    if len(steps) > MAX_INPUT_STEPS:
        raise QueenBloodError("mulligan navigation exceeds the bounded step limit")
    return tuple(steps)


def plan_queen_blood_mulligan_navigation(
    current_cursor_index: int,
    target_index: int,
) -> tuple[QueenBloodInputStep, ...]:
    """Compatibility wrapper for the generic Queen's Blood card planner."""
    return plan_queen_blood_card_navigation(current_cursor_index, target_index)


def plan_queen_blood_input(state: QueenBloodState, move: QueenBloodMove) -> tuple[QueenBloodInputStep, ...]:
    """Translate one validated move into bounded directional UI input."""
    if state.phase != "player_turn":
        raise QueenBloodError("cannot plan input outside player_turn")
    if move.key() not in {item.key() for item in state.legal_moves}:
        raise QueenBloodError("requested move is not in the exported legal move set")
    card = next((item for item in state.hand if item.hand_index == move.hand_index), None)
    if card is None:
        raise QueenBloodError("legal move references a card missing from the exported hand")
    if move.card_id is not None and move.card_id != card.card_id:
        raise QueenBloodError("legal move card_id does not match the exported hand")
    if card.legal_positions and (move.row, move.column) not in card.legal_positions:
        raise QueenBloodError("legal move position is not in the card placement set")
    if state.cursor is None:
        raise QueenBloodError("card-game cursor telemetry is required before input")
    if state.cursor.area == "unknown":
        raise QueenBloodError("card-game cursor area is unresolved")
    steps: list[QueenBloodInputStep] = []
    if state.cursor.area == "hand":
        if state.cursor.hand_index is None:
            raise QueenBloodError("hand cursor index is unresolved")
        steps.extend(_axis_steps(state.cursor.hand_index, move.hand_index, "left", "right", "select_card"))
        steps.append(QueenBloodInputStep("confirm", "select_card"))
        board_row = state.cursor.row if state.cursor.row is not None else 0
        board_column = state.cursor.column if state.cursor.column is not None else 0
    else:
        board_row = state.cursor.row
        board_column = state.cursor.column
        if board_row is None or board_column is None:
            raise QueenBloodError("board cursor position is unresolved")
    steps.extend(_axis_steps(board_row, move.row, "up", "down", "place_card_row"))
    steps.extend(_axis_steps(board_column, move.column, "left", "right", "place_card_column"))
    steps.append(QueenBloodInputStep("confirm", "place_card"))
    if not 1 <= len(steps) <= MAX_INPUT_STEPS:
        raise QueenBloodError("card-game input plan exceeds the bounded step limit")
    return tuple(steps)


def plan_queen_blood_mulligan_input(
    card_indices: list[int] | tuple[int, ...],
    *,
    initial_cursor_index: int = 0,
) -> tuple[QueenBloodInputStep, ...]:
    """Plan a bounded mulligan for zero through five cards.

    The game enters the mulligan screen with the hover on the first hand
    card. Arrow navigation moves between cards and the left mouse button
    toggles a card's mulligan marker. The game-specific Play binding then
    submits either the selected replacements or the unchanged opening hand.
    """
    if not 0 <= len(card_indices) <= MULLIGAN_MAX_CARD_COUNT:
        raise QueenBloodError(
            "mulligan must select between zero and five cards"
        )
    if not isinstance(initial_cursor_index, int) or isinstance(initial_cursor_index, bool):
        raise QueenBloodError("mulligan initial cursor index must be an integer")
    if not 0 <= initial_cursor_index < MULLIGAN_HAND_SIZE:
        raise QueenBloodError("mulligan initial cursor index is outside the five-card hand")
    indices = []
    for value in card_indices:
        if not isinstance(value, int) or isinstance(value, bool):
            raise QueenBloodError("mulligan card indices must be integers")
        if not 0 <= value < MULLIGAN_HAND_SIZE:
            raise QueenBloodError("mulligan card index is outside the five-card hand")
        if value in indices:
            raise QueenBloodError("mulligan card indices must be distinct")
        indices.append(value)

    steps: list[QueenBloodInputStep] = []
    current = initial_cursor_index
    for target in sorted(indices):
        steps.extend(_axis_steps(
            current,
            target,
            "queen_blood_left",
            "queen_blood_right",
            "hover_mulligan_card",
        ))
        steps.append(QueenBloodInputStep("queen_blood_mark", "mark_mulligan_card", target))
        current = target
    steps.append(QueenBloodInputStep("queen_blood_play", "submit_mulligan"))
    if not 1 <= len(steps) <= MAX_INPUT_STEPS:
        raise QueenBloodError("mulligan input plan exceeds the bounded step limit")
    return tuple(steps)


def lanes_disagree_with_cards(payload: Any) -> bool:
    """True when a game lane total differs from its card sum. Live
    2026-10-12 (#344) the totals reached the cards one to four seconds after
    a placement or effect, while the game spawned its score tokens."""
    try:
        state = QueenBloodState.from_dict(payload)
    except QueenBloodError:
        return False
    summed = state.card_lane_powers()
    if summed is None:
        return False
    shown = {lane.row: (lane.player_power, lane.opponent_power) for lane in state.lanes}
    return any(row in shown and shown[row] != powers for row, powers in enumerate(summed))


def reconcile_lane_totals(payload: dict[str, Any], *, settled: bool = True) -> dict[str, Any]:
    """A copy of a Queen's Blood payload whose lane totals are its card sums
    (#329). A game total that differs is kept as ``displayed_player_power``
    and ``displayed_opponent_power`` with ``mismatch: true``, or with
    ``settling: true`` instead when the caller has not waited for the board
    to settle (#344). Returned unchanged when the cards cannot be summed."""
    try:
        state = QueenBloodState.from_dict(payload)
    except QueenBloodError:
        return payload
    summed = state.card_lane_powers()
    board = payload.get("board")
    if summed is None or not isinstance(board, dict):
        return payload
    shown = {lane.row: lane for lane in state.lanes}
    lanes = []
    for row, (player, opponent) in enumerate(summed):
        lane: dict[str, Any] = {"row": row, "player_power": player, "opponent_power": opponent}
        game = shown.get(row)
        if game is not None and (game.player_power, game.opponent_power) != (player, opponent):
            lane.update(displayed_player_power=game.player_power,
                        displayed_opponent_power=game.opponent_power,
                        **({"mismatch": True} if settled else {"settling": True}))
        lanes.append(lane)
    return {**payload, "board": {**board, "lanes": lanes}}


def queen_blood_summary(state: QueenBloodState) -> dict[str, Any]:
    return {
        "active": state.active,
        "phase": state.phase,
        "turn": state.turn,
        "match_id": state.match_id,
        "screen": state.screen,
        "begin_match_available": state.begin_match_available,
        "match_screen": state.match_screen,
        "legal_move_count": len(state.legal_moves),
        "hand_size": len(state.hand),
        "board_cell_count": len(state.board),
        "player_score": state.player_score,
        "opponent_score": state.opponent_score,
        "winner": state.winner,
    }
