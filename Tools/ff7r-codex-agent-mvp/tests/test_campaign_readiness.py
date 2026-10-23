import json

from ff7r_agent.campaign_director import CampaignDirector
from ff7r_agent.campaign_readiness import DEFAULT_READINESS, load_readiness, readiness_advisory


def test_checked_in_matrix_covers_regional_members_and_sources():
    value = json.loads(DEFAULT_READINESS.read_text(encoding="utf-8"))
    index = load_readiness()
    assert index["available"] is True
    rows = index["opponents"]
    assert len(rows) == 30
    gauntlet = DEFAULT_READINESS.parents[2] / "src" / "queens-blood-gauntlet.ts"
    ids = [json.loads(line.strip().rstrip(","))["id"] for line in gauntlet.read_text(encoding="utf-8").splitlines()
           if '"benchmark":"regional"' in line]
    assert list(rows) == ids
    assert sum(row["live_reach"]["talk_offered"] is True for row in rows.values()) == value["summary"]["live_talk_proven"]
    gaps = {row["opponent_id"] for row in rows.values() if row["unproven_checks"]}
    visits = [identifier for group in value["minimal_live_checks"]["groups"] for identifier in group["opponent_ids"]]
    assert len(visits) == len(set(visits)) == 18
    assert set(visits) == gaps
    for row in rows.values():
        assert row["campaign_authority"] is False
        assert row["availability"]["evaluated_on_active_save"] is False
        for fact in row["match_events"] + row["approach_observations"]:
            assert fact["source"] in value["sources"]
        plan = row["offline_approach"]
        if plan["status"] == "planned":
            assert plan["artifact_hash_verified"] is True
            assert plan["endpoint_resolution"]["reason_code"] == "point_inside_polygon"
    twins = readiness_advisory(index, "oba1310_Actor_CardGamer02_Younger")
    assert {event["member"] for event in twins["historical_results"]} == {"Devin", "Darren"}


def test_historical_wins_cannot_complete_or_unlock_new_run(tmp_path):
    store = CampaignDirector(tmp_path)
    plan = store.create("readiness-new-slot", "ChatGPT", "gpt-6.1-sol")["plan"]
    status = store.status(plan["campaign_id"])
    assert status["completed_opponents"] == []
    assert all(row["state"] == "pending" for row in status["objectives"])
    assert status["receipt_count"] == 0
    step = store.next_step(plan["campaign_id"])["next_step"]
    assert step["opponent"] == "Ned"
    assert step["readiness"]["classification"] == "match_recorded"
    assert step["readiness"]["campaign_authority"] is False
    assert status["readiness_index"]["available"] is True


def test_optional_matrix_invalid_stale_or_absent(tmp_path):
    path = tmp_path / "readiness.json"
    assert load_readiness(path)["available"] is False
    path.write_text("[]", encoding="utf-8")
    assert load_readiness(path)["available"] is False
    value = json.loads(DEFAULT_READINESS.read_text(encoding="utf-8"))
    value["catalogue_digest"] = "stale"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert load_readiness(path)["reason"] == "readiness_catalogue_changed"
    value = json.loads(DEFAULT_READINESS.read_text(encoding="utf-8"))
    value["opponents"].append(value["opponents"][0])
    path.write_text(json.dumps(value), encoding="utf-8")
    assert load_readiness(path)["available"] is False


def test_dustbowl_advice_keeps_default_failure_and_older_floor_plan():
    index = load_readiness()
    for identifier in ("oba2360_Actor_CardGamer01", "oba2360_Actor_CardGamer02", "oba2360_Actor_CardGamer03", "oba2360_Actor_Wise"):
        advice = readiness_advisory(index, identifier)
        assert advice["classification"] == "unproven"
        assert advice["offline_approach"]["status"] == "no_offline_route"
        alternatives = advice["offline_approach"]["alternative_captures"]
        assert alternatives and all(item["artifact_hash_verified"] for item in alternatives)
        assert advice["live_reach"]["reached_live"] is None


def test_unrecorded_visible_loss_and_unknown_gate_are_retained():
    index = load_readiness()
    rolf = readiness_advisory(index, "oba2260_Actor_CardGamer02")
    assert rolf["classification"] == "played_unrecorded"
    assert rolf["historical_results"][0]["score"] == {"player": 6, "opponent": 13}
    assert rolf["historical_results"][0]["finish_record_verified"] is False
    navalan = readiness_advisory(index, "oba2260_Actor_CardGamer01")
    assert navalan["classification"] == "challenge_locked"
    assert navalan["availability"]["required_rank"] is None
