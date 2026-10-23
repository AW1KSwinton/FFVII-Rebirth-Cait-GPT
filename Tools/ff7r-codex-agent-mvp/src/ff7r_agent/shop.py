"""Shops: reading a shop's screen and buying from it.

Live 2026-09-30 at Kalm's item shop (``KALMT_ITEM_000``), the exporter's
text scan read the whole shop:

* the shop's id (``Shop_Menu.Txt_ShopName`` = ``$KALMT_ITEM_000``), the menu
  title (``$menu_shop_0000_0000`` on Buy / Sell, ``$menu_shop_0007_0000``
  while buying) and the list's tab (``$menu_shop_0011_0009`` = Cards);
* the highlighted row's item id in the detail panel's title
  (``$it_key_minigamecardPack_000``): the cursor;
* Cloud's gil (``Shop_Foot.Txt_Giru``, "182,134") and the quantity's total
  (``Txt_TotalFee``: 0 until a row is picked, then the price);
* the "Buy the ... for 500 gil?" Yes / No window (``Com_Window_01.Txt``,
  No highlighted when it opens) and, once bought, "Obtained Card 018:
  Flan." lines in the message log.

Q / E (``left`` / ``right`` in the keymap) switch tabs, up / down move the
cursor, Confirm picks a row, opens the Yes / No window and answers it, and a
right-click (the shop's Cancel; Backspace does nothing there) backs out. A
lost press on Yes answers No, which buys nothing: the purchase is checked
by the gil it took.

Input goes through the service's guarded shop press.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .map_query import request_snapshot
from .navigation_values import MissionError

SHOP_NAME_PATH = "Shop.WidgetTree.Shop_Menu.WidgetTree.Txt_ShopName"
MENU_TITLE_PATH = "Shop.WidgetTree.Shop_Menu.WidgetTree.Txt_Title"
TAB_PATH = "Shop_Menu.WidgetTree.Shop_List_Item.WidgetTree.Txt_Tab"
FOCUSED_PATH = "Shop.WidgetTree.Shop_ItemInfo.WidgetTree.EquipInfo.WidgetTree.Txt_Title"
GIL_PATH = "Shop.WidgetTree.Shop_Foot.WidgetTree.Txt_Giru"
TOTAL_PATH = "Shop_Menu.WidgetTree.Shop_List_Item.WidgetTree.Txt_TotalFee"
CONFIRM_PATH = "Com_Window_01.WidgetTree.Txt"
MESSAGE_PATH = "MessageLogs_Cell.WidgetTree.Message"

ROOT_TITLE = "menu_shop_0000_0000"
BUYING_TITLE = "menu_shop_0007_0000"
CARDS_TAB = "menu_shop_0011_0009"

SHOP_BUTTONS = frozenset({"confirm", "cancel", "up", "down", "left", "right"})
MAX_TAB_PRESSES = 10
MAX_CURSOR_PRESSES = 60
MAX_CLOSE_PRESSES = 5
PURCHASE_SETTLE_SECONDS = 1.0
_PACK_NUMBER = re.compile(r"minigamecardPack_(\d+)$")


def _key(text: str) -> str:
    return text[1:] if text.startswith("$") else text


def _number(text: str | None) -> int | None:
    digits = (text or "").replace(",", "").strip()
    return int(digits) if digits.isdigit() else None


def _values(texts: list[dict[str, str]], suffix: str) -> list[str]:
    return [item["text"] for item in texts if item["path"].endswith(suffix)]


def _single(texts: list[dict[str, str]], suffix: str) -> str | None:
    values = {value for value in _values(texts, suffix) if value.strip()}
    return values.pop() if len(values) == 1 else None


@dataclass
class ShopScreen:
    shop: str | None
    title: str | None
    tab: str | None
    focused: str | None
    gil: int | None
    total: int | None
    confirm_text: str | None
    messages: list[str] = field(default_factory=list)

    @property
    def open(self) -> bool:
        return self.shop is not None

    @property
    def buying(self) -> bool:
        return self.title == BUYING_TITLE

    def summary(self) -> dict[str, Any]:
        return {**asdict(self), "open": self.open, "buying": self.buying}


def shop_name(texts: list[dict[str, str]]) -> str | None:
    """The open shop's id (``KALMT_ITEM_000``), or None with no shop shown."""
    for item in texts:
        if item["path"].endswith(SHOP_NAME_PATH) and item["text"].startswith("$"):
            return item["text"][1:]
    return None


def parse_shop(texts: list[dict[str, str]]) -> ShopScreen:
    title = _single(texts, MENU_TITLE_PATH)
    tab = _single(texts, TAB_PATH)
    focused = _single(texts, FOCUSED_PATH)
    return ShopScreen(
        shop=shop_name(texts),
        title=_key(title) if title else None,
        tab=_key(tab) if tab else None,
        focused=_key(focused) if focused else None,
        gil=_number(_single(texts, GIL_PATH)),
        total=_number(_single(texts, TOTAL_PATH)),
        confirm_text=_single(texts, CONFIRM_PATH),
        messages=_values(texts, MESSAGE_PATH),
    )


def read_screen_texts(service: Any, contains: str | None = None) -> dict[str, Any]:
    """Every visible text with its widget path (optionally only paths or
    texts containing ``contains``); no input."""
    response, base = request_snapshot(service, heights=False, flags=("alltexts",), require_map=False)
    wanted = (contains or "").casefold()
    texts = [{"path": str(item.get("path", "")), "text": str(item.get("text", ""))}
             for item in response.get("texts") or [] if isinstance(item, dict)]
    if wanted:
        texts = [item for item in texts if wanted in item["path"].casefold() or wanted in item["text"].casefold()]
    return {
        "texts": texts,
        "count": len(texts),
        "in_menu": base.get("in_menu"),
        "paused": base.get("paused"),
        "source_sequence": response.get("source_sequence"),
    }


def read_shop(service: Any) -> ShopScreen:
    return parse_shop(read_screen_texts(service)["texts"])


def _first_direction(focused: str | None, item_id: str) -> str:
    """Packs are listed in item-id order: head toward the wanted number."""
    here = _PACK_NUMBER.search(focused or "")
    there = _PACK_NUMBER.search(item_id)
    if here and there and int(there.group(1)) < int(here.group(1)):
        return "up"
    return "down"


def _press(service: Any, button: str, screen: ShopScreen) -> ShopScreen:
    """Press on the shop ``screen`` was just read from, then read it again."""
    service._press_shop_button(button, screen.shop)
    return read_shop(service)


def open_buy_list(service: Any, screen: ShopScreen, trace: list[dict[str, Any]]) -> ShopScreen:
    """From the Buy / Sell menu into the buying list (the cursor opens on Buy)."""
    if screen.buying:
        return screen
    for attempt in range(2):
        if attempt:
            # Not the buying list: back out and move up to Buy.
            screen = _press(service, "cancel", screen)
            screen = _press(service, "up", screen)
        screen = _press(service, "confirm", screen)
        trace.append({"step": "open_buy_list", "title": screen.title})
        if screen.buying:
            return screen
    raise MissionError("shop_buy_list_not_opened", response={"screen": screen.summary()})


def select_tab(service: Any, screen: ShopScreen, tab: str, trace: list[dict[str, Any]]) -> ShopScreen:
    """Cycle the tabs to ``tab``; stays put if the shop has no such tab."""
    start = screen.tab
    for _ in range(MAX_TAB_PRESSES):
        if screen.tab == tab:
            return screen
        screen = _press(service, "right", screen)
        trace.append({"step": "tab", "tab": screen.tab})
        if screen.tab == start:
            break
    return screen


def focus_item(service: Any, screen: ShopScreen, item_id: str, trace: list[dict[str, Any]]) -> ShopScreen:
    """Move the cursor until the detail panel names ``item_id``."""
    direction = _first_direction(screen.focused, item_id)
    turned = False
    stuck = 0
    for _ in range(MAX_CURSOR_PRESSES):
        if screen.focused == item_id:
            return screen
        previous = screen.focused
        screen = _press(service, direction, screen)
        trace.append({"step": "cursor", "button": direction, "focused": screen.focused})
        if screen.focused != previous:
            stuck = 0
            continue
        # Twice without moving is the end of the list (once may be a lost press).
        stuck += 1
        if stuck < 2:
            continue
        if turned:
            break
        direction, turned, stuck = ("up" if direction == "down" else "down"), True, 0
    raise MissionError("shop_item_not_listed", response={"item_id": item_id, "screen": screen.summary()})


def buy_item(service: Any, item_id: str, *, price: int | None = None, name: str | None = None,
             sleep: Any = None) -> dict[str, Any]:
    """Buy one ``item_id`` from the open shop and check the gil it took."""
    trace: list[dict[str, Any]] = []
    screen = read_shop(service)
    if not screen.open:
        raise MissionError("shop_not_open")
    screen = open_buy_list(service, screen, trace)
    if item_id.startswith("it_key_minigamecard"):
        screen = select_tab(service, screen, CARDS_TAB, trace)
    screen = focus_item(service, screen, item_id, trace)
    gil_before = screen.gil
    if gil_before is not None and price is not None and gil_before < price:
        raise MissionError("shop_not_enough_gil", response={"gil": gil_before, "price": price})
    screen = _press(service, "confirm", screen)
    trace.append({"step": "quantity", "total": screen.total})
    if not screen.total:
        # No quantity to choose: sold out, or not affordable.
        raise MissionError("shop_item_not_purchasable", response={"item_id": item_id, "screen": screen.summary(),
                                                                  "trace": trace})
    total = screen.total
    if price is not None and total != price:
        _press(service, "cancel", screen)
        raise MissionError("shop_price_mismatch", response={"expected": price, "total": total})
    screen = _press(service, "confirm", screen)
    trace.append({"step": "confirm_window", "text": screen.confirm_text})
    if not screen.confirm_text or (name and name.casefold() not in screen.confirm_text.casefold()):
        _press(service, "cancel", screen)
        raise MissionError("shop_confirmation_not_shown", response={"screen": screen.summary(), "trace": trace})
    messages_before = set(screen.messages)
    # The window opens on No: up to Yes, then Confirm.
    service._press_shop_button("up", screen.shop)
    service._press_shop_button("confirm", screen.shop)
    (sleep or service._sleeper)(PURCHASE_SETTLE_SECONDS)
    screen = read_shop(service)
    obtained = [line for line in screen.messages if line not in messages_before]
    trace.append({"step": "bought", "gil": screen.gil, "confirm_text": screen.confirm_text})
    paid = gil_before - screen.gil if gil_before is not None and screen.gil is not None else None
    if paid != total:
        raise MissionError("shop_purchase_not_made", response={"gil_before": gil_before, "gil_after": screen.gil,
                                                               "total": total, "trace": trace})
    return {"ok": True, "shop": screen.shop, "item_id": item_id, "paid": paid, "gil": screen.gil,
            "obtained": obtained, "trace": trace}


def close_shop(service: Any) -> dict[str, Any]:
    """Back out of the shop with Cancel until it is gone."""
    presses = 0
    screen = read_shop(service)
    while screen.open and presses < MAX_CLOSE_PRESSES:
        screen = _press(service, "cancel", screen)
        presses += 1
    if screen.open:
        raise MissionError("shop_not_closed", response={"screen": screen.summary()})
    return {"ok": True, "presses": presses}
