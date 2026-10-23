import math

import pytest

from ff7r_agent import qb_opponents
from ff7r_agent.navigation_values import MissionError
from ff7r_agent.qb_opponents import load_opponents, opponent, travel_spot_for

KALM = {"key": "$map1110", "name": "Kalm", "goto": "1110-KALMT", "same_area": True,
        "distance_m": 64.3, "arrival_xyz": [58003.7, -97280.9, 10883.2], "rule": "nearest_same_area"}
GONGAGA = {"key": "$map2410", "name": "Gongaga", "goto": "2410-GONGV", "same_area": True,
           "distance_m": 109.3, "arrival_xyz": [0.0, 0.0, 0.0], "rule": "nearest_same_area"}


def _record(identifier, name, host, category="regional_player", spot=None, hard_of=None, xyz=None):
    return {"id": identifier, "display_name": name, "canonical_id": f"field_object:X:{host}",
            "host": {"object_id": host}, "category": category, "hard_variant_of": hard_of,
            "travel_spot": spot, "frame": "0001-WORLD",
            "position": {"xyz": xyz} if xyz else None}


CATALOGUE = {"schema_version": 1, "opponents": [
    _record("oba1110_Actor_CardGamer01", "Ned", "oba1110_Actor_CardGamer01", spot=KALM,
            xyz=[61394.453, -102668.43, 11798.452]),
    _record("oba1110_Actor_QuestCardGamer01", "Vash", "oba1110_BarOwner_Quest02", "quest", KALM),
    _record("oba1110_Actor_QuestCardGamer01_Hard", "Vash", "oba1110_BarOwner_Quest02", "quest", KALM,
            hard_of="oba1110_Actor_QuestCardGamer01"),
    _record("oba2410_Actor_CardGamer01", "Regina", "oba2410_Actor_CardGamer01", spot=GONGAGA),
    _record("oba1360_CardGamePlayer09", "Regina", "oba1360_CardGamePlayer09", "ferry_tournament"),
    _record("opy1360_Tifa", "Tifa", "opy1360_Tifa", "ferry_tournament"),
    _record("opy9999_TwinA", "Twin", "hostA", spot=KALM),
    _record("opy9999_TwinB", "Twin", "hostB", spot=KALM),
    _record("oba9964_Actor_CardGamer02", "Zogan", "oba9964", "debug", KALM),
]}


def test_opponent_by_id_canonical_id_and_display_name() -> None:
    assert opponent("oba1110_Actor_CardGamer01", CATALOGUE)["display_name"] == "Ned"
    assert opponent("field_object:X:oba1110_Actor_CardGamer01", CATALOGUE)["display_name"] == "Ned"
    assert opponent("  ned ", CATALOGUE)["id"] == "oba1110_Actor_CardGamer01"


def test_hard_rematch_yields_to_its_base_row() -> None:
    assert opponent("Vash", CATALOGUE)["id"] == "oba1110_Actor_QuestCardGamer01"


def test_a_shared_name_prefers_the_host_reachable_by_fast_travel() -> None:
    assert opponent("Regina", CATALOGUE)["id"] == "oba2410_Actor_CardGamer01"
    assert opponent("Regina", CATALOGUE, category="ferry_tournament")["id"] == "oba1360_CardGamePlayer09"


def test_distinct_reachable_hosts_are_ambiguous_and_debug_rows_are_ignored() -> None:
    with pytest.raises(MissionError, match="qb_opponent_ambiguous"):
        opponent("Twin", CATALOGUE)
    with pytest.raises(MissionError, match="qb_opponent_unknown"):
        opponent("Zogan", CATALOGUE)
    with pytest.raises(MissionError, match="qb_opponent_unknown"):
        opponent("", CATALOGUE)


def test_travel_spot_for_returns_the_spot_and_the_walk_target() -> None:
    spot = travel_spot_for("Ned", CATALOGUE)
    assert spot["key"] == "$map1110" and spot["opponent_id"] == "oba1110_Actor_CardGamer01"
    assert spot["target_position"] == {"x": 61394.453, "y": -102668.43, "z": 11798.452}
    with pytest.raises(MissionError, match="qb_opponent_no_travel_spot"):
        travel_spot_for("Tifa", CATALOGUE)


def test_disconnected_shelter_has_a_same_area_entrance_fallback():
    assert qb_opponents.alternate_travel_spot_for("Zahira", "$map1110_FastTravel1") == "$map1110"
    assert qb_opponents.alternate_travel_spot_for("Zahira", "$map1110") == "$map1110_FastTravel1"
    catalogue = {"opponents": [{**CATALOGUE["opponents"][0], "nearest_spots": [
        {"key": "$map1110", "same_area": True}, {"key": "other-region", "same_area": False}]}]}
    assert qb_opponents.alternate_travel_spot_for("Ned", "$map1110", catalogue) is None


def test_catalogue_errors_are_mission_errors(tmp_path) -> None:
    with pytest.raises(MissionError, match="qb_opponent_catalogue_unavailable"):
        load_opponents(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": 2, "opponents": []}', encoding="utf-8")
    with pytest.raises(MissionError, match="qb_opponent_catalogue_invalid"):
        load_opponents(bad)


@pytest.mark.skipif(not qb_opponents.DEFAULT_CATALOGUE.is_file(), reason="generated catalogue not present")
def test_the_generated_catalogue_places_ned_where_he_was_seen_live() -> None:
    catalogue = load_opponents(qb_opponents.DEFAULT_CATALOGUE)
    ned = opponent("Ned", catalogue)
    assert ned["canonical_id"] == "field_object:1110-KALMT:oba1110_Actor_CardGamer01"
    # Live 2026-09 observation of Ned: (61395, -102669, 11798).
    assert math.dist(ned["position"]["xyz"], (61395, -102669, 11798)) < 5
    spot = travel_spot_for("Ned", catalogue)
    assert spot["key"] == "$map1110"
    # Live Kalm arrival (58003.7, -97280.9, 10974.5); the warp point sits on the floor.
    assert math.dist(spot["arrival_xyz"][:2], (58003.7, -97280.9)) < 5
    assert catalogue["summary"]["with_travel_spot"] >= 50


def test_an_opponent_is_found_by_its_actor_name():
    # Live 2026-10-04: the reachability run passed actor names, which the
    # catalogue knew but did not match, so no position or spot was found.
    from ff7r_agent.qb_opponents import opponent, travel_spot_for

    assert opponent("Npc2110_CardGamer03")["display_name"] == "Riana"
    assert opponent("npc2110_cardgamer03")["id"] == "oba2110_Actor_CardGamer03"
    assert travel_spot_for("Npc2110_CardGamer03")
