"""The initiation fast path finds a host the name scan cannot."""
from __future__ import annotations

from ff7r_agent.service import AgentService
import pytest
from copy import deepcopy

NENE = {
    "is_player": False,
    "position": {"x": 63612.25, "y": -100782.9375, "z": 12226.47},
    "id": "MG2005_00_UniqueCardGameKalm02_C /Game/Level/Game/EndGame.EndGame:PersistentLevel.MG2005_00_UniqueCardGameKalm02_C_1",
    "class": "BlueprintGeneratedClass /Game/BluePrint/Character/Mob/MG2005_00_UniqueCardGameKalm02.MG2005_00_UniqueCardGameKalm02_C",
    "actor_name": "",
}
CLOUD = {"is_player": True, "position": {"x": 63612.2, "y": -100992.9, "z": 12246.8},
         "id": "PC0000_00_Cloud_Standard_C", "class": "PC0000_00_Cloud_Standard_C"}


def test_a_nameless_card_game_actor_on_the_catalogue_position_is_the_host() -> None:
    # Live 2026-09-29: the probe reported Nene with no actor name.
    snapshot = {"complete": False, "nearby": [CLOUD, NENE]}
    assert AgentService._queen_blood_host_nearby(snapshot, "Nene") == NENE


def test_no_host_is_taken_away_from_the_opponents_position_or_class() -> None:
    assert AgentService._queen_blood_host_nearby({"nearby": [CLOUD, NENE]}, "Ned") is None
    villager = dict(NENE, id="NPC_Villager_C", **{"class": "NPC_Villager_C"})
    assert AgentService._queen_blood_host_nearby({"nearby": [villager]}, "Nene") is None


OLDER_TWIN = {
    "is_player": False, "actor_name": "", "class": "MB2006_00_UniqueCardGameUnderJunon01_C",
    "id": "MB2006_00_UniqueCardGameUnderJunon01_C_2147475760",
    "position": {"x": -73847.125, "y": 81643.25, "z": 4156.547852},
}
YOUNGER_TWIN = {
    **OLDER_TWIN, "id": "MB2006_00_UniqueCardGameUnderJunon01_C_2147475754",
    "position": {"x": -73800.25, "y": 81685.34375, "z": 4156.547363},
}


def test_observed_twins_resolve_by_exact_resident_host_placement() -> None:
    # Both live twins fit the 150 cm envelope; only Darren matches the
    # catalogue's unambiguous resident placement, within 0.02 cm.
    for actors in ([YOUNGER_TWIN, OLDER_TWIN], [OLDER_TWIN, YOUNGER_TWIN]):
        assert AgentService._queen_blood_host_nearby({"complete": False, "nearby": actors}, "oba1310_Actor_CardGamer02") == OLDER_TWIN


@pytest.mark.parametrize("position", [
    {"x": -73847.0, "y": 81643.2, "z": 4156.0},
    {"x": -73817.0, "y": 81643.2, "z": 4156.0},
])
def test_twins_remain_ambiguous_without_separated_placement_evidence(position) -> None:
    nearby = [OLDER_TWIN, {**YOUNGER_TWIN, "position": position}]
    assert AgentService._queen_blood_host_nearby({"nearby": nearby}, "oba1310_Actor_CardGamer02") is None


@pytest.mark.parametrize("override", [
    {"id": OLDER_TWIN["id"]},
    {"id": ""},
    {"actor_name": "DifferentHost"},
    {"class": "DifferentCardGame_C"},
    {"class": ""},
])
def test_exact_placement_does_not_override_conflicting_actor_identity(override) -> None:
    nearby = [OLDER_TWIN, {**YOUNGER_TWIN, **override}]
    assert AgentService._queen_blood_host_nearby({"nearby": nearby}, "oba1310_Actor_CardGamer02") is None


@pytest.mark.parametrize("z", [4300.0, float("nan")])
def test_exact_placement_rejects_wrong_floor_or_invalid_position(z) -> None:
    nearby = [{**OLDER_TWIN, "position": {**OLDER_TWIN["position"], "z": z}}, YOUNGER_TWIN]
    assert AgentService._queen_blood_host_nearby({"nearby": nearby}, "oba1310_Actor_CardGamer02") is None


@pytest.mark.parametrize("override", [
    {"ambiguous": True}, {"duplicates": 1}, {"table_source": {"row": "DifferentHost"}},
])
def test_exact_placement_requires_unambiguous_host_resident_evidence(monkeypatch, override) -> None:
    from ff7r_agent.qb_opponents import opponent
    record = deepcopy(opponent("oba1310_Actor_CardGamer02"))
    record["position_candidates"][0].update(override)
    monkeypatch.setattr("ff7r_agent.qb_opponents.opponent", lambda _: record)
    assert AgentService._queen_blood_host_nearby({"nearby": [OLDER_TWIN, YOUNGER_TWIN]}, record["id"]) is None


def test_exact_placement_refuses_a_third_matching_actor() -> None:
    third = {**YOUNGER_TWIN, "id": "MB2006_00_UniqueCardGameUnderJunon01_C_3"}
    assert AgentService._queen_blood_host_nearby({"nearby": [OLDER_TWIN, YOUNGER_TWIN, third]}, "oba1310_Actor_CardGamer02") is None


def test_exact_placement_ignores_player_distance_rank() -> None:
    older = {**OLDER_TWIN, "distance": 100.0}
    younger = {**YOUNGER_TWIN, "distance": 0.0}
    assert AgentService._queen_blood_host_nearby({"nearby": [younger, older]}, "oba1310_Actor_CardGamer02") == older
