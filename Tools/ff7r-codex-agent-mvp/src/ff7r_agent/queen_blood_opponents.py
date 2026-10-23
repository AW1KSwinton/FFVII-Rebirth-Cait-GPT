"""Queen's Blood NPC opponents from the game's CardGame DataTables.

Each record is one ``CardGameOpponent`` row with the deck its ``DeckID``
names in ``CardGameOpponentDeck``, read from the resident exports under
``C:\\CaitGPT\\DataTableIndex\\Input\\Resident``. Display names come
from the character identity index
(``DataTableIndex\\Output\\Identity\\characters.json``).

Live, Turner played exactly the cards of his ``DeckID`` deck (row 125,
``deck_en_09_0002``), not the first ``deck_Array`` entry, so ``DeckID`` is
taken as the deck of a first match. The ``deck_Array``/``ai_Array`` rank
progression is kept for reference; which rank selects which entry is not
confirmed.

The match loop uses these decks to model an opponent's replies before any of
its cards have been seen (a first match), instead of generic profiles.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OpponentRecord:
    key: str                      # display name, lower case
    display_name: str
    canonical_id: str             # character identity index
    opponent_row: int             # CardGameOpponent Index
    name_id: str                  # CardGameOpponent opponent_name
    deck_id: str                  # CardGameOpponent DeckID
    deck: tuple[str, ...]         # CardGameOpponentDeck cardID_Array for deck_id
    ai_id: str                    # CardGameOpponent AIID
    reward_id: str | None         # CardGameOpponent Reward1 (Reward table row)
    reward_card: str | None       # the Reward row's first item, as a card id
    rank_decks: tuple[str, ...] = ()
    rank_ais: tuple[str, ...] = ()


def _deck(*counts: tuple[str, int]) -> tuple[str, ...]:
    return tuple(card for card, count in counts for _ in range(count))


OPPONENTS: tuple[OpponentRecord, ...] = (
    OpponentRecord(
        key="ned", display_name="Ned",
        canonical_id="field_object:1110-KALMT:oba1110_Actor_CardGamer01",
        opponent_row=2, name_id="$card_opponent_KALMT_01",
        # CardGameOpponentDeck row 26.
        deck_id="deck_en_00_0000",
        deck=_deck(("card_id_000", 2), ("card_id_002", 2), ("card_id_006", 2), ("card_id_014", 2),
                   ("card_id_012", 2), ("card_id_004", 1), ("card_id_008", 2), ("card_id_010", 2)),
        ai_id="card_ai_easy01",
        # Reward row 1248: it_key_minigamecard_014 (Crystalline Crab).
        reward_id="rwdCARD_Normal_001", reward_card="card_id_014",
        rank_decks=("deck_en_00_0000", "deck_en_00_0010", "deck_en_00_0020", "deck_en_00_0030"),
        rank_ais=("card_ai_easy02", "card_ai_Normal01", "card_ai_Hard02_sacrifice", "card_ai_Hard02_sacrifice"),
    ),
    OpponentRecord(
        key="nene", display_name="Nene",
        canonical_id="field_object:1110-KALMT:oba1110_Actor_CardGamer02",
        opponent_row=3, name_id="$card_opponent_KALMT_02",
        # CardGameOpponentDeck row 27.
        deck_id="deck_en_00_0001",
        deck=_deck(("card_id_000", 2), ("card_id_002", 2), ("card_id_013", 2), ("card_id_007", 2),
                   ("card_id_012", 1), ("card_id_003", 2), ("card_id_008", 2), ("card_id_011", 2)),
        ai_id="card_ai_easy01",
        # Reward row 1249: it_key_minigamecard_013 (Cactuar).
        reward_id="rwdCARD_Normal_002", reward_card="card_id_013",
        rank_decks=("deck_en_00_0001", "deck_en_00_0011", "deck_en_00_0021", "deck_en_00_0031"),
        rank_ais=("card_ai_easy02", "card_ai_Normal01", "card_ai_Hard02_sacrifice", "card_ai_Hard02_sacrifice"),
    ),
    OpponentRecord(
        key="zahira", display_name="Zahira",
        canonical_id="field_object:1110-KALMT:oba1110_Actor_CardGamer03",
        opponent_row=4, name_id="$card_opponent_KALMT_03",
        # CardGameOpponentDeck row 28.
        deck_id="deck_en_00_0002",
        deck=_deck(("card_id_000", 2), ("card_id_006", 2), ("card_id_007", 2), ("card_id_009", 2),
                   ("card_id_002", 1), ("card_id_001", 1), ("card_id_010", 1), ("card_id_011", 2),
                   ("card_id_015", 2)),
        ai_id="card_ai_easy01",
        # Reward row 1250: it_key_minigamecard_015 (Screamer).
        reward_id="rwdCARD_Normal_003", reward_card="card_id_015",
        rank_decks=("deck_en_00_0002", "deck_en_00_0012", "deck_en_00_0022", "deck_en_00_0032"),
        rank_ais=("card_ai_easy02", "card_ai_Normal01", "card_ai_Hard03_sacrifice", "card_ai_Hard03_sacrifice"),
    ),
    OpponentRecord(
        key="turner", display_name="Turner",
        canonical_id="field_object:2610-NIBLT:oba2610_Actor_CardGamer03",
        opponent_row=30, name_id="$card_opponent_NIBLT_03",
        # CardGameOpponentDeck row 125; confirmed live (every card seen on
        # Turner's side is in it).
        deck_id="deck_en_09_0002",
        deck=_deck(("card_id_000", 2), ("card_id_006", 2), ("card_id_007", 2), ("card_id_025", 2),
                   ("card_id_001", 2), ("card_id_011", 2), ("card_id_016", 1), ("card_id_072", 2)),
        ai_id="card_ai_easy01",
        # Reward row 1276: it_key_minigamecard_072 (Valron), which the
        # proof-of-concept save (a Turner victory) owns.
        reward_id="rwdCARD_Normal_030", reward_card="card_id_072",
        rank_decks=("deck_en_09_0012", "deck_en_09_0022", "deck_en_09_0032", "deck_en_09_0042"),
        rank_ais=("card_ai_Normal01", "card_ai_Hard03", "card_ai_Hard03_sacrifice", "card_ai_Hard03_sacrifice"),
    ),
)

# The starter deck, CardGameOpponentDeck row 0 (deck_PC_default, preset
# CardGamePresetDeck_0). The proof-of-concept save's scanned inventory is
# exactly these cards plus five later rewards/purchases.
STARTER_DECK: tuple[str, ...] = _deck(
    ("card_id_000", 2), ("card_id_004", 2), ("card_id_006", 2), ("card_id_007", 2),
    ("card_id_008", 2), ("card_id_010", 2), ("card_id_002", 2), ("card_id_117", 1),
)


def _normalise(value: str) -> str:
    return str(value).strip().lower()


def find_opponent(*selectors: str | None) -> OpponentRecord | None:
    """The opponent a display name, name key, canonical or actor id names."""
    for selector in selectors:
        if not selector:
            continue
        wanted = _normalise(selector)
        for record in OPPONENTS:
            actor = record.canonical_id.rsplit(":", 1)[-1]
            if wanted in {
                record.key, _normalise(record.name_id), _normalise(record.canonical_id),
                _normalise(actor), _normalise(actor.replace("oba", "Npc", 1).replace("_Actor", "")),
            }:
                return record
    return None


def opponent_deck(*selectors: str | None) -> list[str]:
    """The opponent's first-match deck card ids (empty when unknown)."""
    record = find_opponent(*selectors)
    return list(record.deck) if record is not None else []
