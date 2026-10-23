"""Queen's Blood card vendor lookups over the built catalogue."""
from __future__ import annotations

import pytest

from ff7r_agent.qb_card_vendors import DEFAULT_CATALOGUE, load_vendors, vendors_for, where_to_buy

pytestmark = pytest.mark.skipif(not DEFAULT_CATALOGUE.is_file(), reason="card vendor catalogue not built")


def test_vendors_in_an_area_by_field_id_or_name() -> None:
    by_id = vendors_for("1110-KALMT")
    by_name = vendors_for("Kalm")
    assert [vendor["shop_id"] for vendor in by_id] == ["KALMT_ITEM_000"]
    assert by_name == by_id
    host = by_id[0]["hosts"][0]
    assert host["actor_name"] == "Npc1110_ItemShop" and host["travel_spot"]


def test_stock_marks_the_cards_already_owned() -> None:
    [kalm] = vendors_for("Kalm", owned={"card_id_001": 2})
    pack = next(row for row in kalm["stock"] if row["item_id"] == "it_key_minigamecardPack_000")
    owned = {card["card_id"]: card["owned"] for card in pack["cards"]}
    assert owned["card_id_001"] == 2
    assert pack["new_cards"] == len(pack["cards"]) - 1


def test_where_a_card_is_sold() -> None:
    catalogue = load_vendors()
    card_id = next(iter(catalogue["cards"]))
    places = where_to_buy(card_id)
    assert places and all({"shop_id", "item_id", "price", "currency"} <= set(place) for place in places)
    assert where_to_buy("card_id_999") == []


def test_a_shop_row_is_found_by_item_id_or_name():
    from ff7r_agent.qb_card_vendors import shop_stock_entry

    by_id = shop_stock_entry("KALMT_ITEM_000", "it_key_minigamecardPack_001")
    assert by_id["name"] == "QB Booster Pack: Ruffians" and by_id["price"] == 500
    assert shop_stock_entry("KALMT_ITEM_000", "qb booster pack: ruffians") == by_id
    assert shop_stock_entry("GS_SHOP_SQUARE1", "it_key_minigamecardPack_001") is None


def test_a_vendor_host_is_found_by_canonical_id() -> None:
    # The resolver names a vendor that is not loaded yet by canonical id.
    from ff7r_agent.qb_card_vendors import vendor_host

    host = vendor_host("field_object:2350-GOLDS:oba2350_shop_chocoboSQ")
    assert host["shop_id"] == "GS_SHOP_SQUARE1" and host["position"]["yaw_deg"] == -5.6
    assert vendor_host("") is None
