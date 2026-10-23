"""Read-only deck preparation; a candidate never authorizes game input."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from typing import Any


def snapshot_binding(run: dict[str, Any], inventory: dict[str, Any]) -> str:
    loaded = run.get("loaded_save")
    if (run.get("loaded_save_verified") is not True or not run.get("profile_id")
            or not isinstance(loaded, dict) or not loaded.get("load_id")
            or inventory.get("profile_id") != run["profile_id"] or not inventory.get("scanned_at")):
        raise ValueError("a verified loaded-save receipt and scanned ownership profile are required")
    snapshot = {"profile_id": run["profile_id"], "loaded_save": loaded, "inventory": inventory}
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def validate_cards(cards: Any, inventory: dict[str, Any]) -> list[str]:
    if not isinstance(cards, list) or len(cards) != 15 or any(not isinstance(c, str) or not c for c in cards):
        raise ValueError("a candidate requires exactly 15 card IDs, including duplicate copies")
    entries = inventory.get("cards")
    if not isinstance(entries, dict):
        raise ValueError("ownership is unavailable")
    for card, wanted in Counter(cards).items():
        entry = entries.get(card)
        owned = entry.get("owned") if isinstance(entry, dict) else None
        if type(owned) is not int or owned < wanted:
            raise ValueError(f"not enough verified owned copies of {card}")
    return list(cards)


def prepare_deck(run: dict[str, Any], inventory: dict[str, Any], *, cards: list[str],
                 opponent: str, rationale: str, fallback: list[str] | None = None,
                 expected_snapshot_sha256: str | None = None) -> dict[str, Any]:
    binding = snapshot_binding(run, inventory)
    if expected_snapshot_sha256 is not None and expected_snapshot_sha256 != binding:
        raise ValueError("the deck planning context is stale; refresh it before choosing cards")
    if not isinstance(opponent, str) or not opponent.strip() or not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("the opponent and tactical rationale are required")
    target = validate_cards(cards, inventory)
    alternative = validate_cards(fallback, inventory) if fallback is not None else None
    return {"version": 1, "opponent": opponent.strip(), "rationale": rationale.strip(),
            "cards": target, "fallback": alternative, "snapshot_sha256": binding,
            "profile_id": run["profile_id"], "known_deck": deepcopy(inventory.get("known_deck")),
            "input_sent": False, "requires_deck_select_verification": True}


def preparation_context(run: dict[str, Any], inventory: dict[str, Any], *,
                        opponent: str, history: dict[str, Any] | None = None) -> dict[str, Any]:
    """Give the agent its planning inputs before it has chosen any cards."""
    from .queen_blood_catalogue import catalogue_card
    from .queen_blood_opponents import find_opponent
    if not isinstance(opponent, str) or not opponent.strip():
        raise ValueError("the opponent is required")
    binding = snapshot_binding(run, inventory)
    record = find_opponent(opponent)
    if record is None:
        from .qb_opponents import opponent as resolve_opponent
        from .navigation_values import MissionError
        try:
            target = resolve_opponent(opponent)
            record = find_opponent(target.get("canonical_id"), target.get("display_name"))
        except MissionError:
            pass  # An unknown opponent remains unknown, never an invented deck.
    owned_cards = []
    for card_id, entry in sorted(inventory.get("cards", {}).items()):
        owned = entry.get("owned") if isinstance(entry, dict) else None
        if type(owned) is not int or owned <= 0:
            continue
        card = catalogue_card(card_id)
        owned_cards.append({"card_id": card_id, "owned": owned,
            "count_source": entry.get("count_source"),
            **({key: deepcopy(getattr(card, key)) for key in (
                "name", "cost", "power", "influence_offsets", "effect_type_name",
                "effect_target_name", "effect_int_parameters", "effect_text_parameters",
                "effect_influence_offsets")} if card else {"catalogue_available": False})})
    return {"version": 1, "opponent": opponent.strip(), "profile_id": run["profile_id"],
            "loaded_slot": run["loaded_save"]["slot"], "snapshot_sha256": binding,
            "scanned_at": inventory["scanned_at"], "owned_cards": owned_cards,
            "known_deck": deepcopy(inventory.get("known_deck")),
            "opponent_info": {"name": record.display_name if record else None,
                "first_match_deck": list(record.deck) if record else None,
                "deck_source": "CardGameOpponent.DeckID; rematches may differ" if record else None,
                "history": deepcopy(history or {})},
            "input_sent": False, "read_only": True,
            "requires_deck_select_verification": True}


def revalidate_deck(candidate: dict[str, Any], run: dict[str, Any], inventory: dict[str, Any],
                    *, opponent: str) -> dict[str, Any]:
    if (not isinstance(candidate, dict) or candidate.get("version") != 1
            or candidate.get("opponent") != opponent.strip()
            or candidate.get("snapshot_sha256") != snapshot_binding(run, inventory)):
        raise ValueError("the prepared deck is stale or belongs to another opponent")
    validate_cards(candidate.get("cards"), inventory)
    if candidate.get("fallback") is not None:
        validate_cards(candidate["fallback"], inventory)
    return {"ok": True, "cards": list(candidate["cards"]), "opponent": opponent.strip(),
            "input_sent": False, "requires_deck_select_verification": True}


def prepare_runtime_snapshot(*, root, state_dir, environment, arguments: dict[str, Any],
                             context_only: bool = False) -> dict[str, Any]:
    """Supervisor-only preparation from durable files and process identity.

    No service, worker, UI/probe, public-state publication or input is involved.
    A second read rejects a save/declaration/inventory change during preparation.
    The returned candidate still needs normal deck-select revalidation.
    """
    from . import public_stream_events
    from .queen_blood_runs import QueenBloodRuns, process_identity
    from .queen_blood_profile import QueenBloodProfile

    if state_dir is None:
        raise ValueError("a verified driver declaration is required for deck preparation")
    supported = {"opponent"} if context_only else {
        "cards", "opponent", "rationale", "fallback", "expected_snapshot_sha256"}
    unexpected = set(arguments) - supported
    if unexpected:
        raise ValueError("unsupported deck preparation arguments: " + ", ".join(sorted(unexpected)))
    required = {"opponent"} if context_only else {"cards", "opponent", "rationale"}
    if not required <= set(arguments):
        raise ValueError("required deck preparation arguments: " + ", ".join(sorted(required)))
    if not isinstance(arguments.get("opponent"), str) or not arguments["opponent"].strip():
        raise ValueError("the opponent is required")
    runs = QueenBloodRuns(root)

    def read():
        driver, _, mode = public_stream_events.match_record_target(state_dir)
        run = runs.status(driver, process_identity(environment))
        assigned = run.get("assignment")
        if (run.get("run_loaded") is not True or not isinstance(assigned, dict)
                or run.get("profile_id") != assigned.get("profile_id")):
            raise ValueError("deck preparation requires the model's verified loaded working save")
        inventory = QueenBloodProfile(root, run["profile_id"]).inventory()
        return run, inventory, mode

    run, inventory, mode = read()
    if context_only:
        profile = QueenBloodProfile(root, run["profile_id"])
        history = profile.opponent(arguments["opponent"])
        # Keep history compact; full match records stay in the existing profile.
        history = {"name_id": history.get("name_id"), "card_ids": history.get("card_ids", []),
                   "last_matches": [{key: match.get(key) for key in ("at", "won", "score", "mode")}
                                    for match in history.get("matches", [])[-3:]]}
        candidate = preparation_context(run, inventory, opponent=arguments["opponent"], history=history)
    else:
        candidate = prepare_deck(run, inventory, **arguments)
    after_run, after_inventory, after_mode = read()
    if (mode != after_mode or run != after_run or
            candidate["snapshot_sha256"] != snapshot_binding(after_run, after_inventory)):
        raise ValueError("the deck preparation snapshot changed while it was being read")
    return candidate
