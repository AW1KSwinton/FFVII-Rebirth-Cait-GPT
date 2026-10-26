"""Use synthetic map-query payloads and temporary files, never the bridge."""
from datetime import datetime, timedelta, timezone

import pytest

from ff7r_agent.loadout_menu import read_loadout_menu
from ff7r_agent.runtime_commands import invoke_runtime
from ff7r_agent.service import AgentService
from map_fixtures import base_sample, response
from test_map_query import MapService


def fixture(tmp_path, **changes):
    base = {**base_sample(), "source": "FF7RTelemetryExporter", "combat_state_observed": True,
            "in_combat": False, "source_sequence": 39}
    base.update(changes)
    # These widget names are synthetic diagnostic evidence, not accepted live
    # equipment-menu bindings. Both lists deliberately have the same suffix.
    rows = [{"id": "EndListBox /Temp/MenuRenderingWorld:Main.Equipment.Character.SelectList",
             "class": "Class /Script/EndGame.EndListBox", "visible": True, "user_index": 0,
             "owners": ["WidgetTree /Temp/MenuRenderingWorld:Main.Equipment.Character.WidgetTree",
                        "UserWidget /Temp/MenuRenderingWorld:Main.Equipment.Character"],
             "selected_index": 0, "selected_child_id": "Cell /Temp/MenuRenderingWorld:Main.Character.Cell_0",
             "properties": {"SyntheticSelectedPlayerType": "0"}},
            {"id": "EndListBox /Temp/MenuRenderingWorld:Main.Equipment.Inventory.SelectList",
             "class": "Class /Script/EndGame.EndListBox", "visible": True, "user_index": 0,
             "owners": ["UserWidget /Temp/MenuRenderingWorld:Main.Equipment.Inventory"],
             "selected_index": 0, "selected_child_id": "Cell /Temp/MenuRenderingWorld:Main.Inventory.Cell_0",
             "properties": {"SyntheticInventoryIndex": "0"}}]
    payload = {**response(), "status": "map_not_open", "texts_scope": "menu",
               "loadout_scope": {"observed": True, "active": True, "owner_still_attached": True,
                                 "owner_id": "SyntheticEquipmentOwner /Temp/MenuRenderingWorld:Main.Equipment"},
               "timestamp_utc": datetime.now(timezone.utc).isoformat(),
               "texts": [{"path": "Main.WidgetTree.Status.WidgetTree.Txt_Name", "text": "$synthetic_cloud"}],
               "save_lists": rows,
               "save_list_discovery": {"source_supported": True, "scan_truncated": False, "records_truncated": False}}
    return MapService(tmp_path, payload=payload, base=base)


def test_one_existing_menu_probe_keeps_full_ids_and_does_not_claim_loadout_bindings(tmp_path):
    service = fixture(tmp_path)
    result = read_loadout_menu(service, raw=True)
    assert result["ok"] and not result["input_sent"] and not result["input_authorized"]
    assert not result["loadout_available"] and not result["bindings_verified"]
    assert result["normalized_loadout"] is None and result["reason"] == "loadout_bindings_unverified"
    rows = result["selection_evidence"]
    assert len(rows) == 2 and rows[0]["list_id"] != rows[1]["list_id"]
    assert rows[0]["selected_child_id"] != rows[1]["selected_child_id"]
    assert all(row["selection_identity_observed"] for row in rows)
    assert rows[0]["properties"] == {"SyntheticSelectedPlayerType": "0"}
    assert result["evidence"]["lists"] == service.payload["save_lists"]
    assert len(service.requests) == 1 and service.requests[0].split("|")[3] == "loadoutmenu"
    assert len(result["evidence_sha256"]) == 64


@pytest.mark.parametrize("changes, reason", [
    ({"in_menu": False, "paused": False}, "loadout_menu_not_open"),
    ({"in_combat": True}, "loadout_field_menu_in_combat"),
    ({"combat_state_observed": None}, "loadout_combat_state_unobserved"),
    ({"source": "simulation"}, "loadout_telemetry_unavailable"),
    ({"running": False}, "loadout_telemetry_unavailable"),
    ({"telemetry": {"live": False}}, "loadout_telemetry_unavailable"),
    ({"source_sequence": None}, "loadout_source_sequence_unobserved"),
])
def test_idle_combat_or_unobserved_context_never_creates_a_request(tmp_path, changes, reason):
    service = fixture(tmp_path, **changes)
    service._sleeper = lambda _: pytest.fail("inactive reader polled for a reply")
    result = read_loadout_menu(service)
    assert not result["ok"] and result["reason"] == reason and service.requests == []
    assert not (tmp_path / "map-query.request").exists()


@pytest.mark.parametrize("field, value", [("selected_index", True), ("selected_child_id", None),
                                        ("user_index", 1), ("user_index", False)])
def test_missing_or_wrong_user_selection_is_not_inferred_from_an_index(tmp_path, field, value):
    service = fixture(tmp_path)
    service.payload["save_lists"][0][field] = value
    result = read_loadout_menu(service)
    assert not result["selection_evidence"][0]["selection_identity_observed"]


def test_truncation_and_older_producer_fields_remain_visible_and_never_become_complete_loadouts(tmp_path):
    service = fixture(tmp_path)
    service.payload["save_list_discovery"]["records_truncated"] = True
    result = read_loadout_menu(service)
    assert result["list_discovery"]["records_truncated"] and not result["loadout_available"]
    service.payload.pop("save_list_discovery")
    result = read_loadout_menu(service)
    assert result["list_discovery"] is None and result["normalized_loadout"] is None


def test_unproven_text_scope_refuses_without_a_broad_fallback_scan(tmp_path):
    service = fixture(tmp_path)
    service.payload.pop("texts_scope")
    assert read_loadout_menu(service)["reason"] == "loadout_menu_scope_unobserved"
    assert len(service.requests) == 1


@pytest.mark.parametrize("field", ["observed", "active", "owner_still_attached", "owner_id"])
def test_lost_or_unidentified_owner_stops_after_one_request(tmp_path, field):
    service = fixture(tmp_path)
    service.payload["loadout_scope"][field] = None
    result = read_loadout_menu(service, raw=True)
    assert result["reason"] == "loadout_menu_scope_inactive" and not result["input_sent"]
    assert len(service.requests) == 1


@pytest.mark.parametrize("field, value, reason", [
    ("source_sequence", 39, "loadout_capture_not_advanced"),
    ("timestamp_utc", (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(), "loadout_capture_stale"),
    ("timestamp_utc", "2026-10-23T12:00:00", "loadout_capture_stale"),
    ("timestamp_utc", None, "loadout_capture_timestamp_invalid"),
])
def test_old_or_unbound_evidence_is_not_presented_as_a_current_capture(tmp_path, field, value, reason):
    service = fixture(tmp_path)
    service.payload[field] = value
    assert read_loadout_menu(service)["reason"] == reason


def test_registered_evidence_reader_has_no_input_methods(tmp_path):
    fixture_service = fixture(tmp_path)
    service = AgentService.__new__(AgentService)
    service.__dict__.update(fixture_service.__dict__)
    service.observe = fixture_service.observe
    service._sleeper = fixture_service._sleeper
    result = invoke_runtime(service, None, None, "read_loadout_menu", {"raw": True})
    assert result["ok"] and not result["input_sent"]


def test_invalid_raw_argument_does_not_observe_or_request(tmp_path):
    service = fixture(tmp_path)
    service.observe = lambda: pytest.fail("invalid arguments triggered a read")
    with pytest.raises(ValueError, match="raw must be a boolean"):
        read_loadout_menu(service, raw="yes")


@pytest.mark.parametrize("kwargs", [
    {"stage": "unknown"}, {"stage": []}, {"wait_seconds": True},
    {"wait_seconds": float("nan")}, {"wait_seconds": float("inf")},
    {"wait_seconds": -1}, {"wait_seconds": 4}, {"wait_seconds": 1},
])
def test_invalid_selection_wait_does_not_read(tmp_path, kwargs):
    service = fixture(tmp_path)
    service.observe = lambda: pytest.fail("invalid wait triggered a read")
    with pytest.raises(ValueError):
        read_loadout_menu(service, **kwargs)


def selection_capture(index=0, owner="Equip_C /Engine/Transient.Equip_C_42"):
    path = owner.partition(" ")[2]
    return {"ok": True, "loadout_scope": {"owner_id": owner},
            "list_discovery": {"scan_truncated": False, "records_truncated": False},
            "selection_evidence": [{
                "list_id": "EndVirtualScrollBox " + path + ".WidgetTree.EquipList.WidgetTree.SelectList",
                "selection_identity_observed": index >= 0, "selected_index": index,
                "selected_child_id": "Item_Cell_C /Temp/SelectedRow" if index >= 0 else None}]}


def wait_service():
    class Service:
        now = 0
        sleeps = []

        def _monotonic(self):
            return self.now

        def _sleeper(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds
    return Service()


def test_waits_for_cursor_after_transition_then_stops(monkeypatch):
    service = wait_service()
    captures, results = [], [selection_capture(-1), selection_capture()]

    def capture(service, raw, **kwargs):
        captures.append(kwargs["timeout_seconds"])
        return results.pop(0)
    monkeypatch.setattr("ff7r_agent.loadout_menu._capture_loadout_menu", capture)
    result = read_loadout_menu(service, stage="equipment_inventory", wait_seconds=3)
    assert result["ok"] and result["selection_wait"] == {
        "stage": "equipment_inventory", "attempts": 2, "ready": True}
    assert result["selection"]["selected_index"] == 0
    assert captures == [2, 2] and service.sleeps == [0.5]


@pytest.mark.parametrize("second, reason", [
    ({"ok": False, "reason": "loadout_menu_not_open"}, "loadout_menu_not_open"),
    ({"ok": False, "reason": "map_query_timeout"}, "map_query_timeout"),
    (selection_capture(owner="Equip_C /Engine/Transient.Equip_C_43"), "loadout_owner_changed"),
])
def test_wait_stops_on_exit_failed_capture_or_replaced_menu(monkeypatch, second, reason):
    service = wait_service()
    results = [selection_capture(-1), second]
    monkeypatch.setattr("ff7r_agent.loadout_menu._capture_loadout_menu", lambda *args, **kwargs: results.pop(0))
    result = read_loadout_menu(service, stage="equipment_inventory", wait_seconds=3)
    assert not result["ok"] and result["reason"] == reason and len(service.sleeps) == 1
    assert results == []


def test_wait_expires_without_idle_polling(monkeypatch):
    service = wait_service()
    calls = []
    monkeypatch.setattr("ff7r_agent.loadout_menu._capture_loadout_menu",
                        lambda *args, **kwargs: calls.append(kwargs) or selection_capture(-1))
    result = read_loadout_menu(service, stage="equipment_inventory", wait_seconds=3)
    assert not result["ok"] and result["reason"] == "loadout_selection_not_ready"
    assert len(calls) == 6 and service.now == 2.5
    assert all(0.5 <= call["timeout_seconds"] <= 2 for call in calls)


def test_no_wait_does_not_sleep_or_accept_other_owner_selector(monkeypatch):
    service = wait_service()
    result = selection_capture()
    result["selection_evidence"][0]["list_id"] = result["selection_evidence"][0]["list_id"].replace("42.", "43.")
    monkeypatch.setattr("ff7r_agent.loadout_menu._capture_loadout_menu", lambda *args, **kwargs: result)
    result = read_loadout_menu(service, stage="equipment_inventory")
    assert not result["ok"] and result["reason"] == "loadout_selection_not_ready"
    assert service.sleeps == []


@pytest.mark.parametrize("change, reason", [("truncated", "loadout_selection_incomplete"),
                                          ("ambiguous", "loadout_selection_ambiguous")])
def test_wait_refuses_incomplete_or_ambiguous_selection(monkeypatch, change, reason):
    service = wait_service()
    result = selection_capture()
    if change == "truncated":
        result["list_discovery"]["records_truncated"] = True
    else:
        result["selection_evidence"] *= 2
    monkeypatch.setattr("ff7r_agent.loadout_menu._capture_loadout_menu", lambda *args, **kwargs: result)
    result = read_loadout_menu(service, stage="equipment_inventory", wait_seconds=3)
    assert not result["ok"] and result["reason"] == reason and service.sleeps == []


def test_wait_outside_menu_never_requests_a_snapshot(tmp_path):
    service = fixture(tmp_path, in_menu=False, paused=False)
    service._sleeper = lambda _: pytest.fail("closed-menu wait slept")
    result = read_loadout_menu(service, stage="member", wait_seconds=3)
    assert not result["ok"] and result["reason"] == "loadout_menu_not_open" and not service.requests


def test_property_diagnostic_is_explicit_and_single_capture(tmp_path):
    service = fixture(tmp_path)
    service.payload["save_lists"][0]["selected_properties"] = {"SyntheticCopyIndex": "7"}
    result = read_loadout_menu(service, raw=True, inspect_properties=True)
    assert result["ok"] and not result["input_authorized"] and result["normalized_loadout"] is None
    assert result["selection_evidence"][0]["selected_properties"] == {"SyntheticCopyIndex": "7"}
    assert len(service.requests) == 1
    assert service.requests[0].split("|")[3] == "loadoutmenu,loadoutinspect"


@pytest.mark.parametrize("kwargs", [{"inspect_properties": "yes"},
                                   {"inspect_properties": True, "stage": "member", "wait_seconds": 1}])
def test_property_diagnostic_rejects_polling_or_invalid_type(tmp_path, kwargs):
    service = fixture(tmp_path)
    service.observe = lambda: pytest.fail("invalid property diagnostic read the game")
    with pytest.raises(ValueError):
        read_loadout_menu(service, **kwargs)


def test_choice_capture_is_explicit_single_request_and_not_normalized_ownership(tmp_path):
    service = fixture(tmp_path)
    choices = {"complete": False, "limit": 64, "rows": [], "item_count": 100}
    service.payload["save_lists"][0]["choices"] = choices
    result = read_loadout_menu(service, include_choices=True)
    assert result["selection_evidence"][0]["choices"] == choices
    assert not result["loadout_available"] and result["normalized_loadout"] is None
    assert len(service.requests) == 1 and service.requests[0].split("|")[3] == "loadoutmenu,loadoutchoices"


@pytest.mark.parametrize("kwargs", [{"include_choices": "yes"},
                                   {"include_choices": True, "stage": "materia_inventory", "wait_seconds": 1}])
def test_choice_capture_cannot_create_a_polling_loop(tmp_path, kwargs):
    service = fixture(tmp_path)
    service.observe = lambda: pytest.fail("invalid choice capture observed the game")
    with pytest.raises(ValueError):
        read_loadout_menu(service, **kwargs)
