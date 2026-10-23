"""Queen's Blood opponents: who they are, where they stand, where to fast travel.

Reads ``ProjectKnowledge/Current/queens-blood-opponents.json``, built offline
by ``FF7R_Indexer/qb_opponent_catalogue.py`` from the game's DataTables, text
and level layout points. Each opponent carries its host actor (the NPC the
player talks to), a world position (Unreal cm, in the frame named by
``frame``), and ``travel_spot``: the fast-travel map spot to use, chosen as the
nearest spot leading into the opponent's own area (``goto``), else the
nearest spot in the same frame. ``travel_spot["key"]`` is the map-spot key
``map_spot_travel`` accepts.

Pure data access: nothing here talks to the game.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .navigation_values import MissionError

CATALOGUE_ENV = "FF7R_QB_OPPONENT_CATALOGUE"
DEFAULT_CATALOGUE = (
    Path(__file__).resolve().parents[4] / "ProjectKnowledge" / "Current" / "queens-blood-opponents.json"
)
_cache: dict[str, dict[str, Any]] = {}


def load_opponents(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """The catalogue (cached per path). ``FF7R_QB_OPPONENT_CATALOGUE`` overrides."""
    source = Path(path or os.environ.get(CATALOGUE_ENV) or DEFAULT_CATALOGUE)
    key = str(source.resolve())
    if key not in _cache:
        try:
            catalogue = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MissionError("qb_opponent_catalogue_unavailable") from exc
        if catalogue.get("schema_version") != 1 or not isinstance(catalogue.get("opponents"), list):
            raise MissionError("qb_opponent_catalogue_invalid")
        _cache[key] = catalogue
    return _cache[key]


def _host(record: dict[str, Any]) -> str:
    return (record.get("host") or {}).get("object_id") or record["id"]


def opponent(name: str, catalogue: dict[str, Any] | None = None, *,
             category: str | None = None) -> dict[str, Any]:
    """One opponent by id (``oba1110_Actor_CardGamer01``), canonical id, actor
    name (``Npc1110_CardGamer01``) or display name.

    A display name can name several rows. Debug rows are ignored, a ``_Hard``
    rematch yields to its base row, and rows sharing one host actor are one
    opponent. If different hosts remain (Regina plays in Gongaga and on the
    ferry), those reachable by fast travel win; ``category`` narrows further.
    Anything still ambiguous raises ``qb_opponent_ambiguous``.
    """
    records = (catalogue or load_opponents())["opponents"]
    text = str(name or "").strip()
    if not text:
        raise MissionError("qb_opponent_unknown")
    exact = [r for r in records if text in (r["id"], r.get("canonical_id"))]
    if exact:
        return exact[0]
    folded = text.casefold()
    matches = [r for r in records if folded in ((r.get("display_name") or "").casefold(),
                                                (r.get("actor_name") or "").casefold())
               and r.get("category") != "debug"]
    if category:
        matches = [r for r in matches if r.get("category") == category]
    if not matches:
        raise MissionError("qb_opponent_unknown")
    base = [r for r in matches if not r.get("hard_variant_of")]
    matches = base or matches
    if len({_host(r) for r in matches}) > 1:
        reachable = [r for r in matches if r.get("travel_spot")]
        matches = reachable or matches
    if len({_host(r) for r in matches}) > 1:
        raise MissionError("qb_opponent_ambiguous")
    return matches[0]


def alternate_travel_spot_for(name: str, current: str, catalogue: dict[str, Any] | None = None) -> str | None:
    """Another entrance to the same area after a disconnected approach.

    Distance alone can select an underground arrival (Zahira's shelter).
    Keep the catalogue's ordering, but never reset onto that failed floor.
    """
    record = opponent(name, catalogue)
    spots = record.get("nearest_spots") or []
    if not any(spot.get("key") == current for spot in spots):
        return None  # Preserve a caller's unrelated explicit reset destination.
    for spot in spots:
        if spot.get("same_area") is True and spot.get("key") != current:
            return spot.get("key")
    return None


def travel_spot_for(name: str, catalogue: dict[str, Any] | None = None, *,
                    category: str | None = None) -> dict[str, Any]:
    """The fast-travel spot for an opponent, with the opponent's position.

    Returns the catalogue's ``travel_spot`` (``key``, ``name``, ``goto``,
    ``distance_m``, ``arrival_xyz``, ``rule``) plus ``opponent_id``,
    ``target_position`` and ``canonical_id`` for the walk from the arrival.
    """
    record = opponent(name, catalogue, category=category)
    spot = record.get("travel_spot")
    if not spot:
        raise MissionError("qb_opponent_no_travel_spot")
    position = record.get("position") or {}
    xyz = position.get("xyz")
    return {
        **spot,
        "opponent_id": record["id"],
        "display_name": record.get("display_name"),
        "canonical_id": record.get("canonical_id"),
        "frame": record.get("frame"),
        "target_position": ({"x": xyz[0], "y": xyz[1], "z": xyz[2]} if xyz else None),
    }
