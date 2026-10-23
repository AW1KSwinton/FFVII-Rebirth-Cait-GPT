"""Queen's Blood card vendors: who sells which cards, where, for how much.

Reads ``ProjectKnowledge/Current/queens-blood-card-vendors.json``, built
offline by ``FF7R_Indexer/qb_card_vendor_catalogue.py`` from the game's shop,
item, map icon and layout data. Each vendor lists its stock (single cards or
booster packs, each pack holding two to four fixed cards; price and currency;
the story flag that puts the row on sale) and the actors behind its counter,
with their actor names, positions and nearest fast-travel spots.

Pure data access: nothing here talks to the game.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .navigation_values import MissionError

CATALOGUE_ENV = "FF7R_QB_CARD_VENDOR_CATALOGUE"
DEFAULT_CATALOGUE = (
    Path(__file__).resolve().parents[4] / "ProjectKnowledge" / "Current" / "queens-blood-card-vendors.json"
)
_cache: dict[str, dict[str, Any]] = {}


def load_vendors(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """The catalogue (cached per path). ``FF7R_QB_CARD_VENDOR_CATALOGUE`` overrides."""
    source = Path(path or os.environ.get(CATALOGUE_ENV) or DEFAULT_CATALOGUE)
    key = str(source.resolve())
    if key not in _cache:
        try:
            catalogue = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MissionError("qb_card_vendor_catalogue_unavailable") from exc
        if catalogue.get("schema_version") != 1 or not isinstance(catalogue.get("vendors"), list):
            raise MissionError("qb_card_vendor_catalogue_invalid")
        _cache[key] = catalogue
    return _cache[key]


def vendors_for(area: str | None = None, *, owned: dict[str, int] | None = None,
                catalogue: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Vendors (optionally those with a host in ``area``: a field id such as
    ``1110-KALMT`` or its name, e.g. "Kalm"), each stock row marked with how
    many of its cards are already owned when ``owned`` (card id -> count) is
    given. Whether a row is on sale yet depends on its ``view_flag`` story
    flag, which only the game knows."""
    wanted = (area or "").strip().casefold()
    result = []
    for vendor in (catalogue or load_vendors())["vendors"]:
        hosts = vendor["hosts"]
        if wanted:
            hosts = [host for host in hosts
                     if wanted in {(host.get("field") or "").casefold(), (host.get("field_name") or "").casefold()}]
            if not hosts:
                continue
        stock = []
        for entry in vendor["stock"]:
            row = {key: entry[key] for key in ("item_id", "kind", "name", "price", "currency", "view_flag")}
            row["cards"] = [dict(card) for card in entry["cards"]]
            if owned is not None:
                for card in row["cards"]:
                    card["owned"] = int(owned.get(card["card_id"], 0))
                row["new_cards"] = sum(1 for card in row["cards"] if not card["owned"])
            stock.append(row)
        result.append({
            "shop_id": vendor["shop_id"],
            "shop_name": vendor["shop_name"],
            "stock": stock,
            "hosts": [{key: host.get(key) for key in ("actor_name", "object_id", "field", "field_name", "frame",
                                                      "position", "travel_spot")} for host in hosts],
        })
    return result


def where_to_buy(card_id: str, catalogue: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every shop row that sells ``card_id`` (singly or in a pack)."""
    record = (catalogue or load_vendors())["cards"].get(str(card_id).strip())
    return [dict(item) for item in record["sold_by"]] if record else []


def shop_stock_entry(shop_id: str | None, item: str,
                     catalogue: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The stock row of ``shop_id`` named by item id or (case-insensitive) name."""
    wanted = str(item or "").strip().casefold()
    for vendor in (catalogue or load_vendors())["vendors"]:
        if vendor["shop_id"] != shop_id:
            continue
        for entry in vendor["stock"]:
            if wanted in {entry["item_id"].casefold(), entry["name"].casefold()}:
                return {key: entry[key] for key in ("item_id", "kind", "name", "price", "currency", "view_flag", "cards")}
    return None


def vendor_host(name: str, catalogue: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The host behind a counter, named by its actor name (``Npc1110_ItemShop``),
    object id (``oba1110_ItemShop``), canonical id (``field_object:...``, as
    the resolver names a vendor not yet loaded) or shop id
    (``KALMT_ITEM_000``, first host)."""
    wanted = str(name or "").strip().casefold()
    if not wanted:
        return None
    for vendor in (catalogue or load_vendors())["vendors"]:
        for host in vendor["hosts"]:
            if wanted in {(host.get("actor_name") or "").casefold(), (host.get("object_id") or "").casefold(),
                          (host.get("canonical_id") or "").casefold()} - {""}:
                return {"shop_id": vendor["shop_id"], **host}
        if wanted == vendor["shop_id"].casefold() and vendor["hosts"]:
            return {"shop_id": vendor["shop_id"], **vendor["hosts"][0]}
    return None
