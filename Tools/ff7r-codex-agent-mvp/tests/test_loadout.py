"""Synthetic normalized loadouts; no live state or game timings are implied."""
from copy import deepcopy

import pytest

from ff7r_agent.loadout import compare_loadout_readback, preview_loadout_change, snapshot_sha256
from ff7r_agent.runtime_commands import invoke_runtime
from ff7r_agent.service import AgentService


def socket(identity, category="weapon", materia=None, link=None, accepts=None):
    return {"slot_id": identity, "category": category, "materia_id": materia,
            "linked_to": link, "accepts": accepts or ["magic", "support"],
            "editable": True, "removable": True}


def equipment(identity, category, slots, allowed=None):
    return {"instance_id": identity, "item_id": "item_" + identity, "category": category,
            "allowed_characters": allowed or ["cloud", "tifa"], "selectable": True, "slots": slots}


def materia(identity, item_id="fire", kind="magic", ap=0, level=1):
    return {"instance_id": identity, "item_id": item_id, "kind": kind, "ap": ap, "level": level,
            "allowed_characters": ["cloud", "tifa"], "selectable": True}


def snapshot():
    cloud_weapon = [socket("w0", link="w1"), socket("w1", link="w0")]
    tifa_weapon = [socket("w0"), socket("w1")]
    armor = [socket("a0", "armor")]
    cloud = {"character_id": "cloud", "editable": True,
             "equipment": {"weapon": "sword", "armor": "bangle", "accessory": None},
             "slots": deepcopy(cloud_weapon + armor)}
    cloud["slots"][0]["materia_id"] = "fire_copy_a"
    cloud["slots"][1]["materia_id"] = "support_copy"
    tifa = {"character_id": "tifa", "editable": True,
            "equipment": {"weapon": "gloves", "armor": "bracelet", "accessory": None},
            "slots": deepcopy(tifa_weapon + armor)}
    tifa["slots"][0]["materia_id"] = "fire_copy_b"
    return {"schema_version": 1, "inventory_complete": True, "loadouts_complete": True,
            "context": {"process_token": "synthetic-process", "save_id": "synthetic-save",
                        "world_id": "synthetic-world", "source_sequence": 12, "in_combat": False},
            "free_materia_inventory_slots": 2, "characters": [cloud, tifa],
            "equipment": [equipment("sword", "weapon", cloud_weapon, ["cloud"]),
                          equipment("gloves", "weapon", tifa_weapon, ["tifa"]),
                          equipment("bangle", "armor", armor), equipment("bracelet", "armor", armor),
                          equipment("new_sword", "weapon", [socket("w0", accepts=["magic"])], ["cloud"]),
                          equipment("ring", "accessory", [])],
            "materia": [materia("fire_copy_a", ap=10), materia("fire_copy_b", ap=900, level=3),
                        materia("support_copy", "elemental", "support"), materia("lightning_copy", "lightning"),
                        materia("summon_copy", "ifrit", "summon")]}


def address(character="cloud", slot="w0"):
    return {"character_id": character, "slot_id": slot}


def preview(change, state=None):
    state = snapshot() if state is None else state
    original = deepcopy(state)
    result = preview_loadout_change(state, change, snapshot_sha256(state))
    assert state == original  # Previews, including refusals, never mutate caller data.
    assert not result["input_sent"] and not result["input_authorized"]
    assert not result["execution_supported"] and not result["native_receipt"]
    return result


def test_equip_an_owned_copy_into_an_empty_slot_preserves_every_other_member():
    before = snapshot()
    result = preview({"operation": "equip_materia", "destination": address(slot="a0"), "materia_id": "lightning_copy"}, before)
    assert result["ok"] and result["configuration_changed"]
    after = result["proposed_state"]
    assert after["characters"][0]["slots"][2]["materia_id"] == "lightning_copy"
    assert after["characters"][1] == before["characters"][1]
    assert after["free_materia_inventory_slots"] == 3


def test_move_between_characters_uses_explicit_slot_identity_and_preserves_links():
    before = snapshot()
    result = preview({"operation": "move_materia", "source": address(), "destination": address("tifa", "a0")}, before)
    assert result["ok"]
    cloud, tifa = result["proposed_state"]["characters"]
    assert cloud["slots"][0]["materia_id"] is None and tifa["slots"][2]["materia_id"] == "fire_copy_a"
    assert cloud["slots"][0]["linked_to"] == "w1" and cloud["slots"][1]["linked_to"] == "w0"
    assert result["displaced_materia"] == []


def test_swap_distinguishes_two_copies_of_the_same_materia_with_different_ap():
    result = preview({"operation": "swap_materia", "source": address(), "destination": address("tifa")})
    assert result["ok"]
    cloud, tifa = result["proposed_state"]["characters"]
    assert cloud["slots"][0]["materia_id"] == "fire_copy_b" and tifa["slots"][0]["materia_id"] == "fire_copy_a"
    owned = {m["instance_id"]: m for m in result["proposed_state"]["materia"]}
    assert owned["fire_copy_a"]["ap"] == 10 and owned["fire_copy_b"]["ap"] == 900


@pytest.mark.parametrize("free, reason", [(0, "inventory_capacity_blocked"), (None, "inventory_capacity_unobserved")])
def test_unequip_needs_observed_inventory_room(free, reason):
    state = snapshot()
    state["free_materia_inventory_slots"] = free
    assert preview({"operation": "unequip_materia", "source": address()}, state)["reason"] == reason


def test_unequip_keeps_the_owned_instance_and_all_its_progress():
    state = snapshot()
    result = preview({"operation": "unequip_materia", "source": address()}, state)
    assert result["ok"] and result["displaced_materia"] == ["fire_copy_a"]
    assert result["proposed_state"]["materia"] == state["materia"]
    assert result["proposed_state"]["free_materia_inventory_slots"] == 1


@pytest.mark.parametrize("change, reason", [
    ({"operation": "equip_materia", "destination": address(), "materia_id": "lightning_copy"}, "destination_occupied"),
    ({"operation": "equip_materia", "destination": address(slot="a0"), "materia_id": "fire_copy_a"}, "materia_already_equipped_use_move_or_swap"),
    ({"operation": "equip_materia", "destination": address(slot="a0"), "materia_id": "not_owned"}, "materia_unavailable"),
    ({"operation": "equip_materia", "destination": address(slot="a0"), "materia_id": "summon_copy"}, "materia_slot_incompatible"),
    ({"operation": "move_materia", "source": address(), "destination": address("tifa")}, "destination_occupied_use_swap"),
    ({"operation": "move_materia", "source": address(), "destination": address()}, "same_slot"),
    ({"operation": "swap_materia", "source": address(), "destination": address("tifa", "a0")}, "destination_empty_use_move"),
    ({"operation": "unequip_materia", "source": address(slot="a0")}, "source_empty"),
    ({"operation": "unequip_materia", "source": address(slot="missing")}, "slot_unavailable"),
    ({"operation": "unequip_materia", "source": address("missing")}, "character_unavailable"),
    ({"operation": "switch_character"}, "operation_unsupported"),
    ({"operation": "unequip_materia", "source": address(), "execute": True}, "change_arguments_invalid"),
])
def test_invalid_or_unavailable_choices_are_refused(change, reason):
    assert preview(change)["reason"] == reason


@pytest.mark.parametrize("field, value, reason", [("editable", False, "slot_locked_or_unobserved"),
                                                  ("editable", None, "slot_locked_or_unobserved"),
                                                  ("removable", False, "removal_locked_or_unobserved")])
def test_locked_or_unobserved_slot_permissions_are_not_guessed(field, value, reason):
    state = snapshot()
    state["characters"][0]["slots"][0][field] = value
    assert preview({"operation": "unequip_materia", "source": address()}, state)["reason"] == reason


@pytest.mark.parametrize("editable", [None, False])
def test_story_locked_or_unobserved_characters_are_refused(editable):
    state = snapshot()
    state["characters"][0]["editable"] = editable
    assert preview({"operation": "unequip_materia", "source": address()}, state)["reason"] == "character_locked_or_unobserved"


def test_missing_item_restrictions_and_wrong_character_are_refused():
    state = snapshot()
    state["materia"][3]["allowed_characters"] = None
    change = {"operation": "equip_materia", "destination": address(slot="a0"), "materia_id": "lightning_copy"}
    assert preview(change, state)["reason"] == "item_restrictions_unobserved"
    state["materia"][3]["allowed_characters"] = ["tifa"]
    assert preview(change, state)["reason"] == "item_character_incompatible"


def test_accessory_change_preserves_all_equipment_materia_and_other_characters():
    before = snapshot()
    result = preview({"operation": "equip_equipment", "character_id": "cloud", "equipment_id": "ring"}, before)
    assert result["ok"] and result["displaced_materia"] == []
    after = result["proposed_state"]
    assert after["characters"][0]["equipment"]["accessory"] == "ring"
    assert after["characters"][0]["slots"] == before["characters"][0]["slots"]
    assert after["characters"][1] == before["characters"][1]


def sword_change(**extra):
    return {"operation": "equip_equipment", "character_id": "cloud", "equipment_id": "new_sword", **extra}


def test_weapon_change_does_not_assume_that_materia_survives_a_different_layout():
    assert preview(sword_change())["reason"] == "explicit_materia_layout_required"
    assert preview(sword_change(materia_assignments={"w0": "fire_copy_a"}))["reason"] == "materia_displacement_requires_consent"
    result = preview(sword_change(materia_assignments={"w0": "fire_copy_a"}, allow_displaced_materia=True))
    assert result["ok"] and result["displaced_materia"] == ["support_copy"]
    assert len(result["proposed_state"]["characters"][0]["slots"]) == 2  # one weapon + one armor slot
    assert result["proposed_state"]["characters"][0]["slots"][1]["linked_to"] is None


def test_weapon_change_checks_displacement_capacity_and_cannot_steal_an_equipped_copy():
    state = snapshot()
    state["free_materia_inventory_slots"] = 0
    change = sword_change(materia_assignments={"w0": "fire_copy_a"}, allow_displaced_materia=True)
    assert preview(change, state)["reason"] == "inventory_capacity_blocked"
    change["materia_assignments"]["w0"] = "fire_copy_b"
    assert preview(change, state)["reason"] == "materia_already_equipped_use_move_or_swap"


def test_unobserved_new_equipment_layout_is_not_assumed_empty():
    state = snapshot()
    state["equipment"][-2]["slots"] = None
    assert preview(sword_change(), state)["reason"] == "equipment_slot_layout_unobserved"


def test_armour_change_uses_observed_links_and_does_not_touch_weapon_slots():
    state = snapshot()
    state["equipment"].append(equipment("new_armor", "armor", [
        socket("a0", "armor", link="a1"), socket("a1", "armor", link="a0")]))
    change = {"operation": "equip_equipment", "character_id": "cloud", "equipment_id": "new_armor",
              "materia_assignments": {"a0": "lightning_copy", "a1": None}}
    result = preview(change, state)
    assert result["ok"]
    member = result["proposed_state"]["characters"][0]
    assert member["slots"][:2] == state["characters"][0]["slots"][:2]
    assert member["slots"][2]["linked_to"] == "a1" and member["slots"][3]["linked_to"] == "a0"


def test_locked_new_equipment_socket_cannot_be_filled_even_if_item_is_selectable():
    state = snapshot()
    state["equipment"][-2]["slots"][0]["editable"] = False
    change = sword_change(materia_assignments={"w0": "fire_copy_a"}, allow_displaced_materia=True)
    assert preview(change, state)["reason"] == "slot_locked_or_unobserved"


def test_current_equipment_descriptor_and_observed_slot_topology_must_agree():
    state = snapshot()
    state["equipment"][0]["slots"] = [socket("w0")]
    assert preview({"operation": "unequip_materia", "source": address()}, state)["reason"] == "equipment_layout_conflict"


def test_empty_inventory_and_same_asset_name_never_create_or_choose_a_copy():
    state = snapshot()
    state["materia"] = []
    for member in state["characters"]:
        for slot in member["slots"]:
            slot["materia_id"] = None
    change = {"operation": "equip_materia", "destination": address(), "materia_id": "fire"}
    assert preview(change, state)["reason"] == "materia_unavailable"
    assert preview({**change, "destination": address(slot="a0")})["reason"] == "materia_unavailable"


def test_occupied_slots_cannot_duplicate_a_copy_through_equipment_layout_assignment():
    state = snapshot()
    state["equipment"][-2]["slots"] = [socket("w0"), socket("w1")]
    change = sword_change(materia_assignments={"w0": "fire_copy_a", "w1": "fire_copy_a"},
                          allow_displaced_materia=True)
    assert preview(change, state)["reason"] == "materia_identity_conflict"


@pytest.mark.parametrize("mutate", [lambda c: c.update(allow_displaced_materia="true"),
                                  lambda c: c.update(materia_assignments={"w0": []})])
def test_malformed_equipment_arguments_return_refusals(mutate):
    change = sword_change()
    mutate(change)
    assert not preview(change)["ok"]


@pytest.mark.parametrize("mutate, reason", [
    (lambda s: s.update(inventory_complete=False), "inventory_incomplete"),
    (lambda s: s.update(loadouts_complete=False), "loadouts_incomplete"),
    (lambda s: s["context"].update(in_combat=True), "field_loadout_edit_in_combat"),
    (lambda s: s["context"].update(in_combat=None), "combat_state_unobserved"),
    (lambda s: s["materia"].append(deepcopy(s["materia"][0])), "identity_duplicated"),
    (lambda s: s["characters"][1]["slots"][0].update(materia_id="fire_copy_a"), "materia_identity_conflict"),
    (lambda s: s["characters"][0]["slots"][0].pop("materia_id"), "slot_unobserved"),
    (lambda s: s["characters"][0]["slots"][0].update(linked_to="w0"), "slot_link_invalid"),
    (lambda s: s["characters"][0]["slots"][1].update(linked_to=None), "slot_link_invalid"),
    (lambda s: s["characters"][0]["slots"][0].update(editable="true"), "restriction_invalid"),
])
def test_incomplete_or_conflicting_snapshots_do_not_produce_plans(mutate, reason):
    state = snapshot()
    mutate(state)
    assert preview({"operation": "unequip_materia", "source": address()}, state)["reason"] == reason


def test_stale_snapshot_hash_is_refused():
    state = snapshot()
    digest = snapshot_sha256(state)
    state["materia"][0]["ap"] += 1
    result = preview_loadout_change(state, {"operation": "unequip_materia", "source": address()}, digest)
    assert not result["ok"] and result["reason"] == "snapshot_changed"


def test_a_newer_matching_readback_is_only_an_offline_comparison_not_a_game_receipt():
    plan = preview({"operation": "swap_materia", "source": address(), "destination": address("tifa")})
    after = deepcopy(plan["proposed_state"])
    after["context"]["source_sequence"] += 1
    result = compare_loadout_readback(plan, after)
    assert result["ok"] and result["matches_requested"] and not result["native_receipt"]
    assert result["validation"] == "offline_state_comparison"


@pytest.mark.parametrize("mutate, reason", [
    (lambda s: s["context"].update(save_id="different-save"), "readback_identity_changed"),
    (lambda s: s["context"].update(process_token="reused-pid"), "readback_identity_changed"),
    (lambda s: s["context"].update(source_sequence=12), "readback_not_newer"),
    (lambda s: s["context"].update(in_combat=True), "readback_in_combat"),
    (lambda s: s["characters"][1]["equipment"].update(accessory="ring"), "configuration_mismatch"),
    (lambda s: s["materia"][0].update(ap=900), "configuration_mismatch"),
])
def test_partial_or_unintended_changes_never_become_success_or_a_retry(mutate, reason):
    plan = preview({"operation": "move_materia", "source": address(), "destination": address("tifa", "a0")})
    after = deepcopy(plan["proposed_state"])
    after["context"]["source_sequence"] += 1
    mutate(after)
    result = compare_loadout_readback(plan, after)
    assert not result["matches_requested"] and result["reason"] == reason
    assert not result["input_sent"] and "retry" not in result


def test_registered_preview_never_reads_a_service_or_constructs_an_input_backend():
    service = AgentService.__new__(AgentService)
    service.observe = lambda: pytest.fail("offline preview read game state")
    before = snapshot()
    result = invoke_runtime(service, None, None, "preview_loadout_change", {
        "snapshot": before, "change": {"operation": "equip_equipment", "character_id": "cloud", "equipment_id": "ring"},
        "expected_snapshot_sha256": snapshot_sha256(before)})
    assert result["ok"] and not result["input_sent"]
