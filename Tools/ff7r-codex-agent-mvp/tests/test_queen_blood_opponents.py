from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

import pytest

from ff7r_agent.queen_blood_catalogue import catalogue_card
from ff7r_agent.queen_blood_opponents import OPPONENTS, STARTER_DECK, find_opponent, opponent_deck

RESIDENT = Path(r"C:\CaitGPT\DataTableIndex\Input\Resident")


def test_every_deck_has_fifteen_catalogued_cards() -> None:
    for record in (*OPPONENTS, None):
        deck = record.deck if record is not None else STARTER_DECK
        assert len(deck) == 15
        assert all(catalogue_card(card_id) is not None for card_id in deck), deck
        # At most the copies a player can own (2 standard, 1 legendary).
        assert max(Counter(deck).values()) <= 2


def test_ned_is_found_by_name_key_and_actor() -> None:
    for selector in ("Ned", " ned ", "$card_opponent_KALMT_01", "oba1110_Actor_CardGamer01",
                     "Npc1110_CardGamer01", "field_object:1110-KALMT:oba1110_Actor_CardGamer01"):
        record = find_opponent(selector)
        assert record is not None and record.key == "ned", selector
    assert find_opponent(None, "$card_opponent_KALMT_01").deck_id == "deck_en_00_0000"
    assert find_opponent("somebody") is None
    assert opponent_deck("somebody") == []


def test_neds_deck_and_reward() -> None:
    ned = find_opponent("Ned")
    names = Counter(catalogue_card(card_id).name for card_id in ned.deck)
    assert names == Counter({
        "Security Officer": 2, "Queen Bee": 2, "Levrikon": 2, "Crystalline Crab": 2, "Flan": 2,
        "J-Unit Sweeper": 1, "Mu": 2, "Elphadunk": 2,
    })
    assert ned.ai_id == "card_ai_easy01"
    assert catalogue_card(ned.reward_card).name == "Crystalline Crab"


@pytest.mark.skipif(not RESIDENT.is_dir(), reason="DataTable exports are not available")
def test_records_match_the_datatable_exports() -> None:
    with (RESIDENT / "CardGameOpponent.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    header = rows[0]
    column = {name: header.index(name) for name in ("Name", "opponent_name", "DeckID", "AIID", "Reward1")}
    opponents = {int(row[0]): row for row in rows[1:] if row and row[0].isdigit()}
    with (RESIDENT / "CardGameOpponentDeck.csv").open(encoding="utf-8-sig", newline="") as handle:
        decks = {row[1]: tuple(item for item in row[3:18] if item) for row in csv.reader(handle)}
    with (RESIDENT / "Reward.csv").open(encoding="utf-8-sig", newline="") as handle:
        rewards = {row[1]: row[3] for row in csv.reader(handle) if len(row) > 3}
    for record in OPPONENTS:
        row = opponents[record.opponent_row]
        assert row[column["Name"]] == record.canonical_id.rsplit(":", 1)[-1]
        assert row[column["opponent_name"]] == record.name_id
        assert row[column["DeckID"]] == record.deck_id
        assert row[column["AIID"]] == record.ai_id
        assert row[column["Reward1"]] == record.reward_id
        assert Counter(decks[record.deck_id]) == Counter(record.deck)
        if record.reward_card:
            assert rewards[record.reward_id] == record.reward_card.replace("card_id_", "it_key_minigamecard_")
    assert Counter(decks["deck_PC_default"]) == Counter(STARTER_DECK)
