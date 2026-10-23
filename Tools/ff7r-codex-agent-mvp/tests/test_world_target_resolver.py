import json
from threading import Lock

import pytest

from ff7r_agent.navigation_values import MissionError
from ff7r_agent.world_target_resolver import (
    locate_identity,
    load_identity_index,
    resolve_identity,
    resolve_world_target,
)


def record(
    canonical_id,
    *,
    field_id="2610-NIBLT",
    object_id="oba2610_Actor_Test",
    actor_name="Npc2610_Test",
    chara_id="NP0001_00",
    chara_spec_name="CharaSpec_NP0001_00_Test",
    display_names=None,
    planner_character_names=None,
    placement=True,
):
    return {
        "canonical_id": canonical_id,
        "target_type": "character",
        "field_id": field_id,
        "object_id": object_id,
        "actor_name": actor_name,
        "chara_id": chara_id,
        "chara_spec_name": chara_spec_name,
        "display_names": display_names or [],
        "localization_keys": [],
        "planner_character_names": planner_character_names or [],
        "placement": (
            {"initialize_transform": f"obl_{object_id}_Init"} if placement else None
        ),
    }


def index_with(*records):
    return {"schema_version": 1, "records": list(records)}


class FakeInventoryService:
    def __init__(self, tmp_path, actors):
        self.telemetry = type("Telemetry", (), {"path": tmp_path / "state.json"})()
        self._lock = Lock()
        self._sequence = 20
        self.actors = actors

    def observe(self):
        self._sequence += 1
        state = {
            "sequence": self._sequence,
            "map_id": "EndGame",
            "player": {"actor_id": "Cloud"},
            "source": "simulation",
        }
        request = self.telemetry.path.with_name("entity-poi.request")
        if request.exists():
            request_id, _, _, _, mode = request.read_text(encoding="ascii").split("|")
            assert mode == "character_inventory"
            payload = {
                "source": "ue4ss_character_inventory",
                "request_id": request_id,
                "source_sequence": self._sequence,
                "map_id": "EndGame",
                "player_actor_id": "Cloud",
                "complete": True,
                "characters": self.actors,
            }
            path = request.with_name(f"character-inventory.{self._sequence % 2}.json")
            path.write_text(json.dumps(payload), encoding="utf-8")
        return state


def test_localized_display_name_resolves_and_locates_by_actor_name(tmp_path):
    dale = record(
        "field_object:2610-NIBLT:oba2610_Actor_CardGamer02",
        object_id="oba2610_Actor_CardGamer02",
        actor_name="Npc2610_CardGamer02",
        chara_id="NP0114_00",
        chara_spec_name="CharaSpec_NP0114_00_BlackMantle_Standard",
        display_names=["Dale"],
    )
    index = index_with(dale)
    identity = resolve_identity("dale", index)
    assert identity["status"] == "resolved"
    assert identity["selection_reason"] == "unique_localized_display_name"

    service = FakeInventoryService(
        tmp_path,
        [
            {
                "id": "NP0114_00_BlackMantle_Standard_C /Game/EndGame:PersistentLevel.Dale_1",
                "class": "BlueprintGeneratedClass /Game/NP0114_00_BlackMantle_Standard.NP0114_00_BlackMantle_Standard_C",
                "actor_name": "Npc2610_CardGamer02",
                "chara_id": "NP0114_00",
                "chara_spec": "CharaSpec_NP0114_00_BlackMantle_Standard",
                "is_player": False,
                "position": {"x": 100.0, "y": 200.0, "z": 300.0},
            }
        ],
    )
    result = locate_identity(service, identity)
    assert result["ok"] is True
    assert result["status"] == "located"
    assert result["source"] == "live_actor"
    assert result["coordinate_source"] == "K2_GetActorLocation"
    assert result["location_quality"] == "authoritative_live"
    assert result["position"] == {"x": 100.0, "y": 200.0, "z": 300.0}
    assert result["match_evidence"][0]["field"] == "actor_name"


def test_locate_identity_prefers_bounded_exact_target_probe(tmp_path, monkeypatch):
    target = record(
        "field_object:2610-NIBLT:oba2610_Actor_CardGamer03",
        object_id="oba2610_Actor_CardGamer03",
        actor_name="Npc2610_CardGamer03",
        display_names=["Turner"],
    )
    identity = resolve_identity("Turner", index_with(target))
    calls = []

    def fast_target_scan(service, selector, *, seconds):
        calls.append((selector, seconds))
        return {
            "ok": True,
            "sample": {"map_id": "EndGame", "sequence": 21},
            "snapshot": {
                "complete": True,
                "target": {
                    "id": "TurnerActor",
                    "class": "BlueprintGeneratedClass Turner",
                    "actor_name": "Npc2610_CardGamer03",
                    "chara_id": "NP0001_00",
                    "chara_spec": "CharaSpec_NP0001_00_Test",
                    "is_player": False,
                    "position": {"x": 100.0, "y": 200.0, "z": 300.0},
                },
            },
        }

    monkeypatch.setattr(
        "ff7r_agent.world_target_resolver.scan_character_target",
        fast_target_scan,
    )
    result = locate_identity(FakeInventoryService(tmp_path, []), identity, seconds=10)

    assert result["ok"] is True
    assert result["position"] == {"x": 100.0, "y": 200.0, "z": 300.0}
    assert calls == [("actor_name:Npc2610_CardGamer03", 10)]


def test_blank_runtime_name_can_match_static_chara_spec_class(tmp_path):
    snaps = record(
        "field_object:2610-NIBLT:oba2610_Actor_Snaps",
        object_id="oba2610_Actor_Snaps",
        actor_name=None,
        chara_id=None,
        chara_spec_name="CharaSpec_MM2054_00_UniqueQstTravelCameraman",
        display_names=["Snaps"],
    )
    identity = resolve_identity(
        "Snaps",
        index_with(
            snaps,
            record(
                "field_object:2610-NIBLT:oba2610_Actor_Other",
                object_id="oba2610_Actor_Other",
                chara_spec_name="CharaSpec_NP0002_00_Other",
                display_names=["Other"],
            ),
        ),
    )
    service = FakeInventoryService(
        tmp_path,
        [
            {
                "id": "MM2054_00_UniqueQstTravelCameraman_C /Game/EndGame:PersistentLevel.Snaps_1",
                "class": "BlueprintGeneratedClass /Game/MM2054_00_UniqueQstTravelCameraman.MM2054_00_UniqueQstTravelCameraman_C",
                "actor_name": "",
                "chara_id": "",
                "chara_spec": "",
                "is_player": False,
                "position": {"x": 1.0, "y": 2.0, "z": 3.0},
            }
        ],
    )
    result = locate_identity(service, identity)
    assert result["ok"] is True
    assert result["position"]["z"] == 3.0
    assert {item["field"] for item in result["match_evidence"]} == {"class_chara_spec"}
    evidence = result["match_evidence"][0]
    assert evidence["static_identity_field"] == "chara_spec_name"
    assert evidence["static_candidate_count"] == 1
    assert evidence["static_canonical_ids"] == [snaps["canonical_id"]]
    assert evidence["static_unique_to_record"] is True


def test_shared_chara_spec_does_not_select_only_loaded_runtime_actor(tmp_path):
    shared_spec = "CharaSpec_NP0001_00_Shared"
    requested = record(
        "field_object:A:requested",
        object_id="oba_A_Requested",
        actor_name=None,
        chara_id=None,
        chara_spec_name=shared_spec,
        display_names=["Requested NPC"],
    )
    alternate = record(
        "field_object:B:alternate",
        object_id="oba_B_Alternate",
        actor_name=None,
        chara_id=None,
        chara_spec_name=shared_spec,
        display_names=["Alternate NPC"],
    )
    identity = resolve_identity("Requested NPC", index_with(requested, alternate))
    assert identity["status"] == "resolved"
    assert identity["candidates"][0]["static_identity_evidence"]["chara_spec_name"]["candidate_count"] == 2

    service = FakeInventoryService(
        tmp_path,
        [
            {
                "id": "SharedActor_C /Game/EndGame:PersistentLevel.SharedActor_1",
                "class": "BlueprintGeneratedClass /Game/Shared_NP0001_00_Shared.Shared_NP0001_00_Shared_C",
                "actor_name": "",
                "chara_id": "",
                "chara_spec": "",
                "is_player": False,
                "position": {"x": 10.0, "y": 20.0, "z": 30.0},
            }
        ],
    )
    result = locate_identity(service, identity)
    assert result["ok"] is False
    assert result["status"] == "live_actor_identity_ambiguous"
    assert result["selection_reason"] == "live_actor_matches_only_non_unique_static_identity_evidence"
    assert result["candidate_count"] == 1
    assert result["blocked_match_evidence"][0][0]["static_candidate_count"] == 2


def test_naming_the_record_by_canonical_id_selects_the_one_shared_class_actor(tmp_path):
    # Live: Chadley's model is shared by eleven records; at the ranch the one
    # loaded actor was refused until the caller named the ranch record.
    shared_spec = "CharaSpec_NP0001_00_Shared"
    requested = record("field_object:A:requested", object_id="oba_A_Requested", actor_name=None, chara_id=None,
                       chara_spec_name=shared_spec, display_names=["Requested NPC"])
    alternate = record("field_object:B:alternate", object_id="oba_B_Alternate", actor_name=None, chara_id=None,
                       chara_spec_name=shared_spec, display_names=["Alternate NPC"])
    actor = {
        "id": "SharedActor_C /Game/EndGame:PersistentLevel.SharedActor_1",
        "class": "BlueprintGeneratedClass /Game/Shared_NP0001_00_Shared.Shared_NP0001_00_Shared_C",
        "actor_name": "", "chara_id": "", "chara_spec": "", "is_player": False,
        "position": {"x": 10.0, "y": 20.0, "z": 30.0},
    }
    index = index_with(requested, alternate)
    by_id = locate_identity(FakeInventoryService(tmp_path, [actor]), resolve_identity("field_object:A:requested", index))
    assert by_id["ok"] is True and by_id["status"] == "located"
    assert by_id["selection_reason"] == "caller_selected_canonical_id_unique_live_match"
    assert by_id["position"] == {"x": 10.0, "y": 20.0, "z": 30.0}
    # Two such actors, or a display name rather than the id, still fail closed.
    two = locate_identity(FakeInventoryService(tmp_path, [actor, {**actor, "id": actor["id"] + "_2"}]),
                          resolve_identity("field_object:A:requested", index))
    assert two["ok"] is False
    by_name = locate_identity(FakeInventoryService(tmp_path, [actor]), resolve_identity("Requested NPC", index))
    assert by_name["status"] == "live_actor_identity_ambiguous"


def test_duplicate_display_names_remain_ambiguous_and_do_not_scan(tmp_path):
    index = index_with(
        record("field_object:A:one", field_id="A", display_names=["Merchant"]),
        record("field_object:B:two", field_id="B", display_names=["Merchant"]),
    )
    identity = resolve_identity("Merchant", index)
    assert identity["status"] == "ambiguous"
    assert identity["candidate_count"] == 2
    result = locate_identity(FakeInventoryService(tmp_path, []), identity)
    assert result["status"] == "ambiguous"
    assert "selection_reason" in result


def test_planner_event_name_is_not_silent_display_name(tmp_path):
    index = index_with(
        record(
            "field_object:A:one",
            planner_character_names=["Supervisor"],
            display_names=[],
        )
    )
    identity = resolve_identity("Supervisor", index)
    assert identity["status"] == "identity_label_only"
    assert identity["ok"] is False
    result = locate_identity(FakeInventoryService(tmp_path, []), identity)
    assert result["status"] == "identity_label_only"


def test_known_identity_without_live_actor_is_explicit_and_not_a_coordinate(tmp_path):
    identity = resolve_identity(
        "Christine",
        index_with(
            record(
                "field_object:2600-NIBLA:oba2600_Actor_Christine",
                object_id="oba2600_Actor_Christine",
                actor_name="Npc2600_Christine",
                display_names=["Christine"],
            )
        ),
    )
    result = locate_identity(FakeInventoryService(tmp_path, []), identity)
    assert result["ok"] is False
    assert result["status"] == "known_but_not_currently_instantiated"
    assert result["location_source"] is None
    assert "position" not in result


def test_resolve_world_target_loads_explicit_index_and_runs_live_lookup(tmp_path):
    target_index = tmp_path / "characters.json"
    target_index.write_text(
        json.dumps(
            index_with(
                record(
                    "field_object:A:one",
                    actor_name="Npc_A_Target",
                    display_names=["Target"],
                )
            )
        ),
        encoding="utf-8",
    )
    service = FakeInventoryService(
        tmp_path,
        [
            {
                "id": "TargetActor",
                "class": "Target",
                "actor_name": "Npc_A_Target",
                "chara_id": "NP0001_00",
                "chara_spec": "CharaSpec_NP0001_00_Test",
                "is_player": False,
                "position": {"x": 9.0, "y": 8.0, "z": 7.0},
            }
        ],
    )
    result = resolve_world_target(service, "Target", identity_index_path=target_index)
    assert result["ok"] is True
    assert result["canonical_id"] == "field_object:A:one"
    assert result["map_id"] == "EndGame"


def test_index_loading_fails_closed_when_missing(tmp_path):
    with pytest.raises(MissionError, match="identity_index_unavailable"):
        load_identity_index(tmp_path / "missing.json")
