"""Buying from a shop, against a fake of Kalm's item shop as seen live."""
from __future__ import annotations

import pytest

from ff7r_agent import shop
from ff7r_agent.navigation_values import MissionError

P = "/Engine/Transient.GameEngine_1:BP_EndGameInstance."
PACKS = [f"it_key_minigamecardPack_{number:03d}" for number in range(4)]


class FakeShop:
    """Buy / Sell menu, an All tab and a Cards tab, the quantity step, the
    Yes / No window (opening on No) and the gil a purchase takes."""

    def __init__(self, gil=1000, sold_out=(), lose=()):
        self.open, self.buying, self.tab = True, False, 0
        self.tabs = [("menu_shop_0011_0000", ["item_potion"] + PACKS), (shop.CARDS_TAB, list(PACKS))]
        self.cursor, self.stage, self.yes = 0, "list", False
        self.gil, self.sold_out, self.messages = gil, set(sold_out), []
        self.presses, self.lose = [], list(lose)

    def focused(self):
        return self.tabs[self.tab][1][self.cursor]

    def _press_shop_button(self, button, shop_id):
        assert shop_id == "KALMT_ITEM_000"
        self.presses.append(button)
        if self.lose and self.lose[0] == len(self.presses):
            self.lose.pop(0)
            return {"ok": True}
        if not self.buying:
            if button == "confirm":
                self.buying = True
            elif button == "cancel":
                self.open = False
            return {"ok": True}
        if self.stage == "list":
            items = self.tabs[self.tab][1]
            if button == "right":
                self.tab, self.cursor = (self.tab + 1) % len(self.tabs), 0
            elif button == "down":
                self.cursor = min(self.cursor + 1, len(items) - 1)
            elif button == "up":
                self.cursor = max(self.cursor - 1, 0)
            elif button == "confirm" and self.focused() not in self.sold_out:
                self.stage = "quantity"
            elif button == "cancel":
                self.buying = False
        elif self.stage == "quantity":
            self.stage = {"confirm": "window", "cancel": "list"}.get(button, "quantity")
            self.yes = False
        elif self.stage == "window":
            if button == "up":
                self.yes = True
            elif button == "confirm":
                if self.yes:
                    self.gil -= 500
                    self.sold_out.add(self.focused())
                    self.messages.append("Obtained Card 018: Flan.")
                self.stage = "list"
            elif button == "cancel":
                self.stage = "quantity"
        return {"ok": True}

    def texts(self):
        if not self.open:
            return []
        rows = [
            (P + shop.SHOP_NAME_PATH, "$KALMT_ITEM_000"),
            (P + shop.MENU_TITLE_PATH, "$" + (shop.BUYING_TITLE if self.buying else shop.ROOT_TITLE)),
            (P + shop.GIL_PATH, f"{self.gil:,}"),
        ] + [(P + shop.MESSAGE_PATH, line) for line in self.messages]
        if self.buying:
            rows += [(P + "Shop.WidgetTree." + shop.TAB_PATH, "$" + self.tabs[self.tab][0]),
                     (P + shop.FOCUSED_PATH, "$" + self.focused()),
                     (P + "Shop.WidgetTree." + shop.TOTAL_PATH, "500" if self.stage != "list" else "0")]
        if self.stage == "window":
            rows.append((P + shop.CONFIRM_PATH, "Buy the QB Booster Pack: Ruffians for 500 gil?"))
        return [{"path": path, "text": text} for path, text in rows]


@pytest.fixture
def fake(monkeypatch):
    shops = []

    def make(**kwargs):
        store = FakeShop(**kwargs)
        shops.append(store)
        return store

    monkeypatch.setattr(shop, "read_screen_texts", lambda service, contains=None: {"texts": service.texts()})
    return make


def test_a_pack_is_bought_from_the_buy_menu_and_checked_by_the_gil_it_took(fake):
    store = fake()
    result = shop.buy_item(store, PACKS[1], price=500, name="QB Booster Pack: Ruffians", sleep=lambda _: None)
    assert result["ok"] and result["paid"] == 500 and store.gil == 500
    assert result["obtained"] == ["Obtained Card 018: Flan."]
    # Buy, Cards tab, one row down, quantity, Yes / No window, up to Yes, Confirm.
    assert store.presses == ["confirm", "right", "down", "confirm", "confirm", "up", "confirm"]


def test_a_lost_press_on_yes_buys_nothing_and_says_so(fake):
    store = fake(lose=[6])  # the "up" to Yes
    with pytest.raises(MissionError, match="shop_purchase_not_made"):
        shop.buy_item(store, PACKS[1], price=500, sleep=lambda _: None)
    assert store.gil == 1000


def test_a_sold_out_pack_is_refused_before_any_window(fake):
    store = fake(sold_out={PACKS[0]})
    with pytest.raises(MissionError, match="shop_item_not_purchasable"):
        shop.buy_item(store, PACKS[0], price=500, sleep=lambda _: None)
    assert "up" not in store.presses


def test_not_enough_gil_is_refused_before_confirming(fake):
    store = fake(gil=100)
    with pytest.raises(MissionError, match="shop_not_enough_gil"):
        shop.buy_item(store, PACKS[2], price=500, sleep=lambda _: None)
    assert store.presses.count("confirm") == 1  # only into the buy list


def test_an_unlisted_item_is_reported_after_searching_both_ways(fake):
    store = fake()
    with pytest.raises(MissionError, match="shop_item_not_listed"):
        shop.buy_item(store, "it_key_minigamecardPack_099", sleep=lambda _: None)


def test_the_shop_is_closed_with_cancel(fake):
    store = fake()
    store.buying = True
    assert shop.close_shop(store) == {"ok": True, "presses": 2}
    assert not store.open


def test_the_open_shop_is_read_from_its_texts(fake):
    store = fake(gil=182134)
    screen = shop.parse_shop(store.texts())
    assert screen.shop == "KALMT_ITEM_000" and screen.gil == 182134 and not screen.buying
