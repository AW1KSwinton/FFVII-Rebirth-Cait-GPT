import json
import time
from threading import Lock

import pytest

from ff7r_agent.character_locator import (
    scan_actor_inventory,
    scan_character_candidates,
    scan_character_inventory,
    scan_character_target,
    scan_traversal_gimmicks,
    _validate_snapshot,
)
from ff7r_agent.navigation_values import MissionError


class FakeCharacterScanService:
    def __init__(self, tmp_path):
        self.telemetry = type("Telemetry", (), {"path": tmp_path / "state.json"})()
        self._lock = Lock()
        self._sequence = 10
        self._clock_value = 0.0
        self._clock = lambda: self._clock_value
        self._sleeper = self._advance_clock
        self._publish_sidecar = True
        self._delay_inventory_until = None

    def _advance_clock(self, seconds):
        self._clock_value += seconds

    def observe(self):
        self._sequence += 1
        state = {
            "sequence": self._sequence,
            "map_id": "EndGame",
            "player": {"actor_id": "Cloud"},
            "source": "simulation",
        }
        request_path = self.telemetry.path.with_name("entity-poi.request")
        if not self._publish_sidecar:
            return state
        if request_path.exists():
            request_id, _, _, selector, mode = request_path.read_text().split("|")
            if (
                mode == "character_inventory"
                and self._delay_inventory_until is not None
                and self._clock_value < self._delay_inventory_until
            ):
                return state
            if mode == "character_candidates":
                payload = {
                    "source": "ue4ss_character_candidates",
                    "request_id": request_id,
                    "source_sequence": self._sequence,
                    "map_id": "EndGame",
                    "player_actor_id": "Cloud",
                    "complete": True,
                    "characters": [{"id": "Npc", "position": {"x": 1, "y": 2, "z": 3}}],
                }
                path = request_path.with_name(f"character-candidates.{self._sequence % 2}.json")
            elif mode == "character_inventory":
                payload = {
                    "source": "ue4ss_character_inventory",
                    "request_id": request_id,
                    "source_sequence": self._sequence,
                    "map_id": "EndGame",
                    "player_actor_id": "Cloud",
                    "complete": True,
                    "characters": [{"id": "Dale", "chara_id": "NP0114_00", "chara_spec": "CharaSpec_NP0114_00_BlackMantle_Standard"}],
                }
                path = request_path.with_name(f"character-inventory.{self._sequence % 2}.json")
            elif mode == "actor_inventory":
                payload = {
                    "source": "ue4ss_actor_inventory",
                    "request_id": request_id,
                    "source_sequence": self._sequence,
                    "map_id": "EndGame",
                    "player_actor_id": "Cloud",
                    "complete": True,
                    "actors": [{"id": "Chadley", "kind": "field_action", "position": {"x": 7, "y": 8, "z": 9}}],
                    "scan_counts": {"EndCharacterBase": 1, "EndFieldActionActorBase": 1, "End2FieldActionActorBase": 0},
                }
                path = request_path.with_name(f"actor-inventory.{self._sequence % 2}.json")
            elif mode == "traversal_gimmicks":
                payload = {
                    "source": "ue4ss_traversal_gimmicks",
                    "request_id": request_id,
                    "source_sequence": self._sequence,
                    "map_id": "EndGame",
                    "player_actor_id": "Cloud",
                    "complete": True,
                    "gimmicks": [{
                        "id": "BP_Climb_C /Game/Map.Map:PersistentLevel.PipeSide",
                        "kind": "climbing",
                        "position": {"x": 1, "y": 2, "z": 3},
                        "upper": {"x": 1, "y": 2, "z": 400},
                        "points": [{"class": "EndFieldClimbingFinishPointSetting", "position": {"x": 1, "y": 2, "z": 380}}],
                    }],
                    "scan_counts": {"EndFieldActionActorLadder": 0, "EndFieldActionClimbingActor": 1},
                }
                path = request_path.with_name(f"traversal-gimmicks.{self._sequence % 2}.json")
            else:
                payload = {
                    "source": "ue4ss_character_target",
                    "request_id": request_id,
                    "source_sequence": self._sequence,
                    "map_id": "EndGame",
                    "player_actor_id": "Cloud",
                    "complete": True,
                    "selector": selector,
                    "target": {"id": selector, "position": {"x": 4, "y": 5, "z": 6}},
                    "nearby": [],
                }
                path = request_path.with_name(f"character-target.{self._sequence % 2}.json")
            path.write_text(json.dumps(payload), encoding="utf-8")
        return state


class MissingTargetScanService(FakeCharacterScanService):
    def observe(self):
        state = super().observe()
        request_path = self.telemetry.path.with_name("entity-poi.request")
        if request_path.exists():
            request_id, _, _, selector, mode = request_path.read_text().split("|")
            if mode == "character_target":
                payload = {
                    "source": "ue4ss_character_target",
                    "request_id": request_id,
                    "source_sequence": self._sequence,
                    "map_id": "EndGame",
                    "player_actor_id": "Cloud",
                    "complete": False,
                    "reason": "character_target_not_found",
                    "selector": selector,
                    "target": None,
                    "nearby": [],
                    "lookup": {
                        "actor_name_api": {
                            "status": "null_default",
                            "id": "",
                        }
                    },
                }
                path = request_path.with_name(f"character-target.{self._sequence % 2}.json")
                path.write_text(json.dumps(payload), encoding="utf-8")
        return state


class DelayedCharacterScanService(FakeCharacterScanService):
    """Publish once before the next observe sample advances past its sequence."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self._published = False

    def observe(self):
        self._sequence += 1
        source_sequence = self._sequence
        state_sequence = source_sequence
        request_path = self.telemetry.path.with_name("entity-poi.request")
        if request_path.exists() and not self._published:
            request_id, _, _, _, mode = request_path.read_text().split("|")
            assert mode == "character_candidates"
            payload = {
                "source": "ue4ss_character_candidates",
                "request_id": request_id,
                "source_sequence": source_sequence,
                "map_id": "EndGame",
                "player_actor_id": "Cloud",
                "complete": True,
                "characters": [{"id": "Npc", "position": {"x": 1, "y": 2, "z": 3}}],
            }
            path = request_path.with_name(f"character-candidates.{source_sequence % 2}.json")
            path.write_text(json.dumps(payload), encoding="utf-8")
            self._published = True
            state_sequence += 1
        return {
            "sequence": state_sequence,
            "map_id": "EndGame",
            "player": {"actor_id": "Cloud"},
            "source": "simulation",
        }


class DelayedDetailedCharacterScanService(FakeCharacterScanService):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self._delay_inventory_until = 4.5


class OffsetSequenceCharacterScanService(FakeCharacterScanService):
    """Model the live exporter: output and Lua/source counters differ."""

    def observe(self):
        state = super().observe()
        state["source_sequence"] = self._sequence
        state["sequence"] = self._sequence + 100
        return state


class NeverPublishingCharacterScanService(FakeCharacterScanService):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self._publish_sidecar = False


def test_candidate_scan_is_request_owned_and_released(tmp_path):
    service = FakeCharacterScanService(tmp_path)
    result = scan_character_candidates(service)
    assert result["ok"]
    assert result["snapshot"]["characters"][0]["id"] == "Npc"
    assert not (tmp_path / "entity-poi.request").exists()


def test_candidate_scan_accepts_sidecar_published_before_next_state(tmp_path):
    service = DelayedCharacterScanService(tmp_path)
    result = scan_character_candidates(service)
    assert result["ok"]
    assert result["snapshot"]["source_sequence"] < result["sample"]["sequence"]
    assert result["snapshot"]["characters"][0]["id"] == "Npc"
    assert not (tmp_path / "entity-poi.request").exists()


def test_detailed_inventory_accepts_sidecar_after_four_seconds(tmp_path):
    service = DelayedDetailedCharacterScanService(tmp_path)

    result = scan_character_inventory(service, seconds=10)

    assert result["ok"]
    assert service._clock_value > 4.0
    assert service._clock_value < 10.0
    assert result["snapshot"]["characters"][0]["chara_id"] == "NP0114_00"
    assert not (tmp_path / "entity-poi.request").exists()


def test_inventory_uses_source_sequence_when_output_counter_is_offset(tmp_path):
    service = OffsetSequenceCharacterScanService(tmp_path)

    result = scan_character_inventory(service)

    assert result["ok"]
    assert result["snapshot"]["source_sequence"] == result["sample"]["source_sequence"]
    assert result["snapshot"]["source_sequence"] < result["sample"]["sequence"]
    assert not (tmp_path / "entity-poi.request").exists()


def test_scan_times_out_and_releases_owned_request_without_sidecar(tmp_path):
    service = NeverPublishingCharacterScanService(tmp_path)

    with pytest.raises(MissionError, match="character_scan_timeout"):
        scan_character_candidates(service, seconds=10)

    assert service._clock_value >= 4.0
    assert service._clock_value < 5.0
    assert not (tmp_path / "entity-poi.request").exists()


def test_character_inventory_scan_returns_detailed_identity(tmp_path):
    service = FakeCharacterScanService(tmp_path)
    result = scan_character_inventory(service)
    assert result["ok"]
    character = result["snapshot"]["characters"][0]
    assert character["chara_id"] == "NP0114_00"
    assert character["chara_spec"].endswith("BlackMantle_Standard")
    assert not (tmp_path / "entity-poi.request").exists()


def test_actor_inventory_scan_is_request_owned_and_released(tmp_path):
    service = FakeCharacterScanService(tmp_path)
    result = scan_actor_inventory(service)
    assert result["ok"]
    assert result["snapshot"]["actors"][0]["kind"] == "field_action"
    assert result["snapshot"]["actors"][0]["position"]["x"] == 7
    assert not (tmp_path / "entity-poi.request").exists()

def test_traversal_gimmick_scan_returns_climb_points_and_releases(tmp_path):
    service = FakeCharacterScanService(tmp_path)
    result = scan_traversal_gimmicks(service)
    assert result["ok"]
    gimmick = result["snapshot"]["gimmicks"][0]
    assert gimmick["kind"] == "climbing"
    assert gimmick["upper"]["z"] == 400
    assert gimmick["points"][0]["position"]["z"] == 380
    assert not (tmp_path / "entity-poi.request").exists()


def test_traversal_gimmick_snapshot_accepts_an_empty_lua_table():
    state = {"map_id": "EndGame", "player": {"actor_id": "Cloud"}}
    payload = {"source": "ue4ss_traversal_gimmicks", "request_id": "r", "source_sequence": 5,
               "map_id": "EndGame", "player_actor_id": "Cloud", "complete": True, "gimmicks": {}}
    validated = _validate_snapshot(payload, source="ue4ss_traversal_gimmicks", request_id="r",
                                   sequence=5, state_sequence=5, state=state)
    assert validated["gimmicks"] == []


def test_target_scan_accepts_full_identity_and_releases_request(tmp_path):
    service = FakeCharacterScanService(tmp_path)
    selector = "Npc World:PersistentLevel.Npc"
    result = scan_character_target(service, selector)
    assert result["snapshot"]["target"]["id"] == selector
    assert not (tmp_path / "entity-poi.request").exists()


def test_target_scan_returns_not_found_snapshot_with_lookup_evidence(tmp_path):
    service = MissingTargetScanService(tmp_path)
    result = scan_character_target(service, "actor_name:Missing")
    snapshot = result["snapshot"]
    assert result["ok"]
    assert snapshot["complete"] is False
    assert snapshot["target"] is None
    assert snapshot["reason"] == "character_target_not_found"
    assert snapshot["lookup"]["actor_name_api"]["status"] == "null_default"
    assert not (tmp_path / "entity-poi.request").exists()


def test_snapshot_rejects_sidecar_newer_than_current_state():
    payload = {
        "source": "ue4ss_character_target",
        "request_id": "request",
        "source_sequence": 11,
        "map_id": "EndGame",
        "player_actor_id": "Cloud",
        "complete": False,
        "target": None,
        "nearby": [],
    }
    state = {
        "sequence": 10,
        "source_sequence": 10,
        "map_id": "EndGame",
        "player": {"actor_id": "Cloud"},
    }
    assert _validate_snapshot(
        payload,
        source="ue4ss_character_target",
        request_id="request",
        sequence=11,
        state_sequence=10,
        state=state,
    ) is None


def test_snapshot_normalizes_empty_lua_lookup_table():
    payload = {
        "source": "ue4ss_character_target",
        "request_id": "request",
        "source_sequence": 10,
        "map_id": "EndGame",
        "player_actor_id": "Cloud",
        "complete": True,
        "target": {"id": "Dale"},
        "nearby": [],
        "lookup": [],
    }
    state = {
        "sequence": 10,
        "map_id": "EndGame",
        "player": {"actor_id": "Cloud"},
    }
    result = _validate_snapshot(
        payload,
        source="ue4ss_character_target",
        request_id="request",
        sequence=10,
        state_sequence=10,
        state=state,
    )
    assert result is not None
    assert result["lookup"] == {}


def test_scan_rejects_an_active_request_and_invalid_selector(tmp_path):
    service = FakeCharacterScanService(tmp_path)
    (tmp_path / "entity-poi.request").write_text(
        f"other|{int(time.time()) + 60}|*|unused|character_candidates",
        encoding="ascii",
    )
    with pytest.raises(MissionError, match="request_busy"):
        scan_character_candidates(service)
    (tmp_path / "entity-poi.request").unlink()
    with pytest.raises(MissionError, match="selector_invalid"):
        scan_character_target(service, "bad|selector")
