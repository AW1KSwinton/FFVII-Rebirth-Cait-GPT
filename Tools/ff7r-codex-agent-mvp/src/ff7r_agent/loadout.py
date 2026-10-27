"""Offline loadout previews for #410. No controller, getters or game writes.

This normalized contract is not yet produced by live menu bindings. Copy IDs,
slot topology and restrictions must come from observations, never UI indexes
or assumptions about a character. A preview is not an execution permission.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


CATEGORIES = ("weapon", "armor", "accessory")
MAX_BYTES = 512 * 1024
MAX_CHARACTERS = 32
MAX_ITEMS = 2048
MAX_SLOTS = 32


class LoadoutError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise LoadoutError(reason)


def _id(value: Any) -> str:
    _require(isinstance(value, str) and 0 < len(value) <= 256
             and value == value.strip() and not any(ord(c) < 32 for c in value), "identity_invalid")
    return value


def _boolean(value: Any) -> None:
    _require(value is None or type(value) is bool, "restriction_invalid")


def _ids(value: Any, limit: int = MAX_CHARACTERS) -> None:
    _require(isinstance(value, list) and len(value) <= limit, "identity_list_invalid")
    _require(len({_id(v) for v in value}) == len(value), "identity_duplicated")


def _clone(value: Any) -> Any:
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        _require(len(text.encode("utf-8")) <= MAX_BYTES, "snapshot_too_large")
        return json.loads(text)
    except (TypeError, ValueError, RecursionError) as error:
        if isinstance(error, LoadoutError):
            raise
        raise LoadoutError("snapshot_not_json") from error


def snapshot_sha256(snapshot: dict[str, Any]) -> str:
    text = json.dumps(_clone(snapshot), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _index(rows: Any, key: str, limit: int) -> dict[str, dict[str, Any]]:
    _require(isinstance(rows, list) and len(rows) <= limit, "collection_invalid")
    result = {}
    for row in rows:
        _require(isinstance(row, dict), "record_invalid")
        identity = _id(row.get(key))
        _require(identity not in result, "identity_duplicated")
        result[identity] = row
    return result


def _slots(rows: Any) -> dict[str, dict[str, Any]]:
    slots = _index(rows, "slot_id", MAX_SLOTS)
    for slot in slots.values():
        _require({"slot_id", "category", "accepts", "editable", "removable", "linked_to", "materia_id"}
                 <= slot.keys(), "slot_unobserved")
        _require(slot.get("category") in CATEGORIES, "slot_category_invalid")
        _ids(slot.get("accepts"), 32)
        for key in ("editable", "removable"):
            _boolean(slot.get(key))
        if slot.get("materia_id") is not None:
            _id(slot["materia_id"])
        link = slot.get("linked_to")
        if link is not None:
            _id(link)
            _require(link in slots and link != slot["slot_id"], "slot_link_invalid")
            other = slots[link]
            _require(other.get("linked_to") == slot["slot_id"]
                     and other.get("category") == slot["category"], "slot_link_invalid")
    return slots


class LoadoutState:
    """A complete provided configuration; nullable permissions stay unknown."""

    def __init__(self, value: Any) -> None:
        self.data = _clone(value)
        _require(isinstance(self.data, dict) and type(self.data.get("schema_version")) is int
                 and self.data["schema_version"] == 1, "snapshot_schema_unsupported")
        context = self.data.get("context")
        _require(isinstance(context, dict), "context_missing")
        for name in ("process_token", "save_id", "world_id"):
            _id(context.get(name))
        _require(type(context.get("source_sequence")) is int and 0 <= context["source_sequence"] < 2**63,
                 "sequence_invalid")
        _require(type(context.get("in_combat")) is bool, "combat_state_unobserved")
        _require(self.data.get("inventory_complete") is True, "inventory_incomplete")
        _require(self.data.get("loadouts_complete") is True, "loadouts_incomplete")
        free = self.data.get("free_materia_inventory_slots")
        _require(free is None or (type(free) is int and 0 <= free <= MAX_ITEMS), "inventory_capacity_invalid")
        self.characters = _index(self.data.get("characters"), "character_id", MAX_CHARACTERS)
        self.equipment = _index(self.data.get("equipment"), "instance_id", MAX_ITEMS)
        self.materia = _index(self.data.get("materia"), "instance_id", MAX_ITEMS)
        _require(not (self.equipment.keys() & self.materia.keys()), "identity_duplicated")
        for item in (*self.equipment.values(), *self.materia.values()):
            _id(item.get("item_id"))
            if item.get("allowed_characters") is not None:
                _ids(item["allowed_characters"])
            _boolean(item.get("selectable"))
        for item in self.equipment.values():
            _require(item.get("category") in CATEGORIES, "equipment_category_invalid")
            if item.get("slots") is not None:
                slots = _slots(item["slots"])
                _require(all(s["category"] == item["category"] and s.get("materia_id") is None
                             for s in slots.values()), "equipment_layout_invalid")
        for item in self.materia.values():
            _id(item.get("kind"))
            for field in ("ap", "level"):
                value = item.get(field)
                _require(value is None or (type(value) is int and 0 <= value <= 1_000_000_000),
                         "materia_progress_invalid")
        seen_gear, seen_materia = set(), set()
        for member in self.characters.values():
            _boolean(member.get("editable"))
            gear = member.get("equipment")
            _require(isinstance(gear, dict) and set(gear) == set(CATEGORIES), "equipment_unobserved")
            for category, identity in gear.items():
                if identity is not None:
                    _id(identity)
                    _require(identity in self.equipment and identity not in seen_gear, "equipment_identity_conflict")
                    _require(self.equipment[identity]["category"] == category, "equipment_category_conflict")
                    seen_gear.add(identity)
            slots = _slots(member.get("slots"))
            for category, identity in gear.items():
                if identity is not None and self.equipment[identity].get("slots") is not None:
                    def topology(rows: Any) -> dict[str, Any]:
                        return {s["slot_id"]: (set(s["accepts"]), s["linked_to"])
                                for s in rows if s["category"] == category}
                    _require(topology(slots.values()) == topology(self.equipment[identity]["slots"]),
                             "equipment_layout_conflict")
            for slot in slots.values():
                _require(gear[slot["category"]] is not None, "slot_without_equipment")
                identity = slot.get("materia_id")
                if identity is not None:
                    _require(identity in self.materia and identity not in seen_materia, "materia_identity_conflict")
                    _require(self.materia[identity]["kind"] in slot["accepts"], "materia_slot_incompatible")
                    seen_materia.add(identity)

    def member(self, identity: str) -> dict[str, Any]:
        _require(identity in self.characters, "character_unavailable")
        member = self.characters[identity]
        _require(member.get("editable") is True, "character_locked_or_unobserved")
        return member

    def slot(self, address: Any, *, removing: bool = False) -> dict[str, Any]:
        _require(isinstance(address, dict) and set(address) == {"character_id", "slot_id"}, "slot_address_invalid")
        member = self.member(_id(address["character_id"]))
        slots = _slots(member["slots"])
        _require(_id(address["slot_id"]) in slots, "slot_unavailable")
        slot = slots[address["slot_id"]]
        _require(slot.get("editable") is True, "slot_locked_or_unobserved")
        if removing:
            _require(slot.get("removable") is True, "removal_locked_or_unobserved")
        return slot

    def place(self, slot: dict[str, Any], identity: str | None, character: str) -> None:
        if identity is not None:
            _require(identity in self.materia, "materia_unavailable")
            item = self.materia[identity]
            self.eligible(item, character)
            _require(item["kind"] in slot["accepts"], "materia_slot_incompatible")
        slot["materia_id"] = identity

    @staticmethod
    def eligible(item: dict[str, Any], character: str) -> None:
        _require(item.get("selectable") is True, "item_unavailable_or_unobserved")
        allowed = item.get("allowed_characters")
        _require(isinstance(allowed, list), "item_restrictions_unobserved")
        _require(character in allowed, "item_character_incompatible")

    def assigned(self) -> set[str]:
        return {slot["materia_id"] for c in self.characters.values() for slot in c["slots"]
                if slot.get("materia_id") is not None}

    def configuration(self) -> dict[str, Any]:
        # Selection/enabled flags may change as menus open/close. They are
        # preconditions, not evidence of the resulting equipped configuration.
        return {"characters": {key: {"equipment": c["equipment"], "slots": {
                    s["slot_id"]: {k: s.get(k) for k in ("category", "accepts", "linked_to", "materia_id")}
                    for s in c["slots"]}} for key, c in self.characters.items()},
                "materia": {key: {k: item.get(k) for k in ("item_id", "kind", "ap", "level")}
                            for key, item in self.materia.items()},
                "equipment": {key: {k: item.get(k) for k in ("item_id", "category")}
                              for key, item in self.equipment.items()}}


def _materia_change(state: LoadoutState, change: dict[str, Any]) -> None:
    operation = change["operation"]
    if operation == "equip_materia":
        _require(set(change) == {"operation", "destination", "materia_id"}, "change_arguments_invalid")
        destination = state.slot(change["destination"])
        identity = _id(change["materia_id"])
        _require(destination.get("materia_id") is None, "destination_occupied")
        _require(identity not in state.assigned(), "materia_already_equipped_use_move_or_swap")
        state.place(destination, identity, change["destination"]["character_id"])
    elif operation == "unequip_materia":
        _require(set(change) == {"operation", "source"}, "change_arguments_invalid")
        source = state.slot(change["source"], removing=True)
        _require(source.get("materia_id") is not None, "source_empty")
        source["materia_id"] = None
    else:
        _require(set(change) == {"operation", "source", "destination"}, "change_arguments_invalid")
        _require(change["source"] != change["destination"], "same_slot")
        source = state.slot(change["source"], removing=True)
        destination = state.slot(change["destination"], removing=operation == "swap_materia")
        left, right = source.get("materia_id"), destination.get("materia_id")
        _require(left is not None, "source_empty")
        if operation == "move_materia":
            _require(right is None, "destination_occupied_use_swap")
        else:
            _require(right is not None, "destination_empty_use_move")
        state.place(destination, left, change["destination"]["character_id"])
        state.place(source, right, change["source"]["character_id"])


def _equipment_change(state: LoadoutState, change: dict[str, Any]) -> None:
    _require({"operation", "character_id", "equipment_id"} <= change.keys()
             and not (change.keys() - {"operation", "character_id", "equipment_id", "materia_assignments", "allow_displaced_materia"}),
             "change_arguments_invalid")
    _require(type(change.get("allow_displaced_materia", False)) is bool, "change_arguments_invalid")
    member = state.member(_id(change["character_id"]))
    identity = _id(change["equipment_id"])
    _require(identity in state.equipment, "equipment_unavailable")
    item = state.equipment[identity]
    state.eligible(item, change["character_id"])
    category = item["category"]
    if member["equipment"][category] == identity:
        _require("materia_assignments" not in change, "already_equipped_use_materia_change")
        return
    _require(not any(identity in c["equipment"].values() for c in state.characters.values()), "equipment_in_use")
    _require(item.get("slots") is not None, "equipment_slot_layout_unobserved")
    old_slots = [s for s in member["slots"] if s["category"] == category]
    new_slots = _clone(item["slots"])
    old_ids = {s["materia_id"] for s in old_slots if s.get("materia_id") is not None}
    assignments = change.get("materia_assignments")
    if assignments is None:
        _require(not old_ids, "explicit_materia_layout_required")
        assignments = {s["slot_id"]: None for s in new_slots}
    _require(isinstance(assignments, dict) and set(assignments) == {s["slot_id"] for s in new_slots}, "materia_layout_invalid")
    elsewhere = state.assigned() - old_ids
    for slot in old_slots:
        _require(slot.get("editable") is True and slot.get("removable") is True, "slot_locked_or_unobserved")
    for slot in new_slots:
        value = assignments[slot["slot_id"]]
        if value is not None:
            _id(value)
            _require(slot.get("editable") is True, "slot_locked_or_unobserved")
        _require(value is None or value not in elsewhere, "materia_already_equipped_use_move_or_swap")
        state.place(slot, value, change["character_id"])
    displaced = old_ids - {s["materia_id"] for s in new_slots}
    _require(not displaced or change.get("allow_displaced_materia") is True, "materia_displacement_requires_consent")
    member["equipment"][category] = identity
    member["slots"] = [s for s in member["slots"] if s["category"] != category] + new_slots


def preview_loadout_change(snapshot: dict[str, Any], change: dict[str, Any],
                           expected_snapshot_sha256: str) -> dict[str, Any]:
    """Compute a desired configuration from provided data, without input."""
    common = {"input_sent": False, "input_authorized": False, "execution_supported": False,
              "validation": "offline_preview_only", "native_receipt": False}
    try:
        state = LoadoutState(snapshot)
        fingerprint = snapshot_sha256(state.data)
        _require(type(expected_snapshot_sha256) is str and expected_snapshot_sha256 == fingerprint, "snapshot_changed")
        _require(state.data["context"]["in_combat"] is False, "field_loadout_edit_in_combat")
        change = _clone(change)
        _require(isinstance(change, dict), "change_arguments_invalid")
        operation = change.get("operation")
        _require(operation in ("equip_materia", "unequip_materia", "move_materia", "swap_materia", "equip_equipment"), "operation_unsupported")
        before = state.configuration()
        assigned_before = state.assigned()
        if operation == "equip_equipment":
            _equipment_change(state, change)
        else:
            _materia_change(state, change)
        # Global uniqueness and slot topology are checked again after the edit.
        state = LoadoutState(state.data)
        delta = len(assigned_before) - len(state.assigned())
        free = state.data.get("free_materia_inventory_slots")
        if delta > 0:
            _require(free is not None, "inventory_capacity_unobserved")
            _require(free >= delta, "inventory_capacity_blocked")
        if free is not None:
            _require(0 <= free - delta <= MAX_ITEMS, "inventory_capacity_invalid")
            state.data["free_materia_inventory_slots"] = free - delta
        return {**common, "ok": True, "before_sha256": fingerprint, "change": change,
                "proposed_state": state.data, "expected_configuration": state.configuration(),
                "configuration_changed": before != state.configuration(),
                "displaced_materia": sorted(assigned_before - state.assigned())}
    except LoadoutError as error:
        return {**common, "ok": False, "reason": error.reason, "proposed_state": None}


def compare_loadout_readback(preview: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare provided states, not a live action receipt or a retry decision."""
    result = {"input_sent": False, "input_authorized": False, "native_receipt": False,
              "validation": "offline_state_comparison", "matches_requested": False}
    try:
        _require(isinstance(preview, dict) and preview.get("ok") is True, "preview_unavailable")
        wanted = LoadoutState(preview.get("proposed_state"))
        actual = LoadoutState(after)
        left, right = wanted.data["context"], actual.data["context"]
        _require(all(left[k] == right[k] for k in ("process_token", "save_id", "world_id")), "readback_identity_changed")
        _require(right["source_sequence"] > left["source_sequence"], "readback_not_newer")
        _require(right["in_combat"] is False, "readback_in_combat")
        matches = wanted.configuration() == actual.configuration()
        return {**result, "ok": matches, "matches_requested": matches,
                "reason": "configuration_matches" if matches else "configuration_mismatch"}
    except LoadoutError as error:
        return {**result, "ok": False, "reason": error.reason}
