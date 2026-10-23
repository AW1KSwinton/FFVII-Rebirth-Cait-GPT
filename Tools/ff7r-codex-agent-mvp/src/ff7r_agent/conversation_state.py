"""Shared, conservative checks for dialogue and field telemetry state."""
from __future__ import annotations

from typing import Any


_ACTIVE_DIALOGUE_STATES = frozenset({"interacting", "dialogue", "menu"})


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def has_player_position(snapshot: Any) -> bool:
    player = _get(snapshot, "player")
    position = _get(player, "position")
    if position is None:
        return False
    coordinates = (
        position.get(axis) if isinstance(position, dict) else getattr(position, axis, None)
        for axis in ("x", "y", "z")
    )
    return all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in coordinates)


def has_field_telemetry(snapshot: Any) -> bool:
    return _get(snapshot, "map_id") is not None and has_player_position(snapshot)


def dialogue_active(snapshot: Any) -> bool:
    """Explicit evidence of an open conversation: the interaction state or a shown QB wheel."""
    interaction = _get(snapshot, "interaction")
    interaction_state = str(_get(interaction, "state", "")).strip().casefold()
    if interaction_state in _ACTIVE_DIALOGUE_STATES:
        return True
    talk_menu = _get(_get(snapshot, "queen_blood"), "talk_menu")
    return _get(talk_menu, "observed") is True and _get(talk_menu, "shown") is True


def field_telemetry_blank(snapshot: Any) -> bool:
    """The generic world feed is explicitly blank (no map, no player position).

    Conversations blank it, but so do cutscenes, menus, transitions and a
    stopped producer, so this is never reported as a dialogue. It never
    authorizes input; it only stops field actions until the feed returns.
    """
    # Distinguish an explicitly blank world feed from snapshots that simply
    # omit map metadata (as several offline adapters do).
    if not isinstance(snapshot, dict) or "map_id" not in snapshot:
        return False
    if snapshot.get("map_id") is not None or has_player_position(snapshot):
        return False
    if snapshot.get("loading") or snapshot.get("in_combat") or snapshot.get("target") is not None:
        return False
    queen_blood = _get(snapshot, "queen_blood")
    if _get(queen_blood, "active") is True or _get(queen_blood, "match_screen") is True:
        return False
    if _get(queen_blood, "begin_match_available") is True:
        return False
    return True


def field_blocker(snapshot: Any) -> str | None:
    """Why a field action (route, fast travel) must not start, or None.

    ``dialogue_active`` needs explicit dialogue evidence; a blank feed alone
    is ``field_telemetry_unavailable`` so it is not mistaken for a dialogue.
    """
    if dialogue_active(snapshot):
        return "dialogue_active"
    if field_telemetry_blank(snapshot):
        return "field_telemetry_unavailable"
    return None


FIELD_BLOCKERS = ("dialogue_active", "field_telemetry_unavailable")
