"""Card effects from the probe's generated Queen's Blood catalogue.

Hand cards arrive from the probe with their effect fields already joined, but
board cells only carry the card identity. This reads the same generated
``card_catalogue.lua`` (built by ``build_card_catalogue.py`` from the
CardGame DataTables) so any card on the board -- including the opponent's --
can be scored with its effect. Only the fields the rules model needs are
parsed. A missing or unreadable catalogue yields no effects, never an error.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from .queen_blood import QueenBloodCard

CATALOGUE_ENV = "FF7R_QUEEN_BLOOD_CATALOGUE"
DEFAULT_CATALOGUE = (
    Path(__file__).resolve().parents[3]
    / "ff7r-telemetry-exporter" / "QueenBlood" / "Scripts" / "card_catalogue.lua"
)

_CARD_ID = re.compile(r'\bcard_id = "([^"]+)"')
_NAME = re.compile(r'\bname = "([^"]+)"')
_COST = re.compile(r"\bcost = (-?\d+)")
_POWER = re.compile(r"\bpower = (-?\d+)")
_COPIES = re.compile(r"\bcopies = (-?\d+)")
_UNIQUE_ID = re.compile(r"\bunique_id = (-?\d+)")
_KEY_ITEM = re.compile(r'\bkey_item = "([^"]+)"')
_TYPE_NAME = re.compile(r'\beffect_type_name = "([^"]+)"')
_TARGET_NAME = re.compile(r'\beffect_target_name = "([^"]+)"')
_INT_PARAMETERS = re.compile(r"\beffect_int_parameters = \{([^}]*)\}")
_INFLUENCE = re.compile(r"(?<!effect_)\binfluence_offsets = ")
_EFFECT_INFLUENCE = re.compile(r"\beffect_influence_offsets = ")
_TEXT_PARAMETERS = re.compile(r"\beffect_text_parameters = \{([^}]*)\}")
_QUOTED = re.compile(r'"([^"]*)"')
_OFFSET = re.compile(r"row = (-?\d+), column = (-?\d+)")


def _offsets(line: str, pattern: re.Pattern[str]) -> tuple[tuple[int, int], ...]:
    match = pattern.search(line)
    if match is None:
        return ()
    rest = line[match.end():]
    if not rest.startswith("{ {"):
        return ()  # "{}" or nil
    body = rest[: rest.find("} }") + 1]
    return tuple((int(row), int(column)) for row, column in _OFFSET.findall(body))


def _int(pattern: re.Pattern[str], line: str) -> int | None:
    match = pattern.search(line)
    return int(match.group(1)) if match else None


def _card(line: str, card_id: str) -> QueenBloodCard:
    parameters = _INT_PARAMETERS.search(line)
    values: tuple[int, ...] = ()
    if parameters is not None:
        try:
            values = tuple(int(item) for item in parameters.group(1).split(",") if item.strip())
        except ValueError:
            values = ()
    text_parameters = _TEXT_PARAMETERS.search(line)
    texts = tuple(_QUOTED.findall(text_parameters.group(1))) if text_parameters else ()
    name = _NAME.search(line)
    type_name = _TYPE_NAME.search(line)
    target_name = _TARGET_NAME.search(line)
    return QueenBloodCard(
        hand_index=0,
        card_id=card_id,
        name=name.group(1) if name else None,
        cost=_int(_COST, line),
        power=_int(_POWER, line),
        influence_offsets=_offsets(line, _INFLUENCE),
        effect_influence_offsets=_offsets(line, _EFFECT_INFLUENCE),
        effect_int_parameters=values,
        effect_text_parameters=texts,
        effect_type_name=type_name.group(1) if type_name else None,
        effect_target_name=target_name.group(1) if target_name else None,
    )


def parse_catalogue(text: str) -> dict[str, QueenBloodCard]:
    cards: dict[str, QueenBloodCard] = {}
    for line in text.splitlines():
        card = _CARD_ID.search(line)
        if card is not None and card.group(1) not in cards:
            cards[card.group(1)] = _card(line, card.group(1))
    return cards


@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, QueenBloodCard]:
    try:
        return parse_catalogue(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}


def catalogue_card(card_id: str | None) -> QueenBloodCard | None:
    """The catalogue's card (cost, power, influence, effect) for an identity."""
    if not card_id:
        return None
    path = os.environ.get(CATALOGUE_ENV) or str(DEFAULT_CATALOGUE)
    return _load(path).get(card_id)


card_effect = catalogue_card


@lru_cache(maxsize=4)
def _load_extras(path: str) -> dict[str, dict[str, int | str | None]]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    extras: dict[str, dict[str, int | str | None]] = {}
    for line in text.splitlines():
        card = _CARD_ID.search(line)
        if card is not None and card.group(1) not in extras:
            key_item = _KEY_ITEM.search(line)
            extras[card.group(1)] = {"copies": _int(_COPIES, line), "unique_id": _int(_UNIQUE_ID, line),
                                    "key_item": key_item.group(1) if key_item else None}
    return extras


def catalogue_copies(card_id: str | None) -> int | None:
    """Copies owned once a card is obtained (CardGameCardInfo.Num: 2 standard, 1 legendary)."""
    if not card_id:
        return None
    path = os.environ.get(CATALOGUE_ENV) or str(DEFAULT_CATALOGUE)
    return (_load_extras(path).get(card_id) or {}).get("copies")


def catalogue_unique_id(card_id: str | None) -> int | None:
    """The game's card number (CardGameCardInfo.UniqueId), which card lists sort by."""
    if not card_id:
        return None
    path = os.environ.get(CATALOGUE_ENV) or str(DEFAULT_CATALOGUE)
    return (_load_extras(path).get(card_id) or {}).get("unique_id")


def card_id_for_key_item(key_item: str) -> str | None:
    """Join native item-row identities through CardGameCardInfo.KeyItem."""
    path = os.environ.get(CATALOGUE_ENV) or str(DEFAULT_CATALOGUE)
    matches = [card_id for card_id, entry in _load_extras(path).items()
               if entry.get("key_item") == key_item and re.fullmatch(r"card_id_\d{3}", card_id)]
    return matches[0] if len(matches) == 1 else None
