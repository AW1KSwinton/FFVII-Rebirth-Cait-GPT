from __future__ import annotations

import json
from pathlib import Path

import pytest

from ff7r_agent.queen_blood_campaign import (
    MAX_LOSS_RETRIES,
    MOORE_TWINS_ID,
    ZOGAN_ID,
    QueenBloodCampaign,
    classify_initiation_failure,
)

ASSIGNMENT = {
    "driver": "ChatGPT",
    "model_id": "gpt-6.1-sol",
    "slot": 15,
    "steam_id": "00000000000000000",
    "profile_id": "ChatGPT-gpt-6.1-sol-00000000000000000-slot015",
}


def _opponent(status: dict, opponent_id: str) -> dict:
    return next(item for item in status["opponents"] if item["id"] == opponent_id)


def _append_finish(
    root: Path,
    selector: str,
    won: bool,
    *,
    at: str,
    source: str | None = "finish_queens_blood_match",
    mode: str | None = "live",
    saved: bool = True,
) -> None:
    path = root / "profiles" / ASSIGNMENT["profile_id"] / "opponents.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {"schema_version": 1, "profile_id": ASSIGNMENT["profile_id"], "opponents": {}}
    entry = data["opponents"].setdefault(selector.lower(), {"card_ids": [], "matches": []})
    match = {
        "at": at,
        "won": won,
        "score": {"player": 9 if won else 4, "opponent": 4 if won else 9},
        "moves": "agent",
        "elapsed_seconds": 60.0,
    }
    if source is not None:
        match["source"] = source
    if mode is not None:
        match["mode"] = mode
    if won and saved:
        match["saved_at"] = at
    entry["matches"].append(match)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _win_all_before(root: Path, opponent_id: str, *, at: str = "2026-10-07T09:00:00Z") -> None:
    """Saved live wins for every opponent earlier in the fixed order."""
    from ff7r_agent.queen_blood_campaign import REGIONAL_OPPONENT_IDS
    for earlier in REGIONAL_OPPONENT_IDS[:REGIONAL_OPPONENT_IDS.index(opponent_id)]:
        members = ([earlier + "_Younger", earlier] if earlier == MOORE_TWINS_ID else [earlier])
        for member in members:
            _append_finish(root, member, True, at=at)


def test_campaign_initialises_the_30_regional_opponents_beside_model_runs(tmp_path: Path) -> None:
    (tmp_path / "model-runs.json").write_text('{"version": 1, "runs": {}}', encoding="utf-8")
    director = QueenBloodCampaign(tmp_path)

    status = director.status(ASSIGNMENT)
    step = director.next_step(ASSIGNMENT)

    assert status["campaign_file"] == str(tmp_path / "campaigns.json")
    assert status["opponent_count"] == 30
    assert status["required_match_count"] == 31
    assert status["summary"] == {
        "pending": 30, "won": 0, "lost": 0, "locked": 0, "blocked": 0, "retry_exhausted": 0,
    }
    assert step["opponent"]["name"] == "Ned"
    assert step["target_id"] == "field_object:1110-KALMT:oba1110_Actor_CardGamer01"
    assert step["travel_spot"]["key"] == "$map1110"


def test_non_finish_profile_records_do_not_change_campaign_results(tmp_path: Path) -> None:
    director = QueenBloodCampaign(tmp_path)
    ned = "field_object:1110-KALMT:oba1110_Actor_CardGamer01"
    _append_finish(
        tmp_path,
        ned,
        True,
        at="2026-10-07T11:55:00Z",
        source="play_queens_blood_match",
    )

    state = _opponent(director.status(ASSIGNMENT), "oba1110_Actor_CardGamer01")
    assert state["state"] == "pending"
    assert state["attempts"] == 0
    assert state["wins"] == 0


def test_records_without_a_live_mode_are_not_credited(tmp_path: Path) -> None:
    # Pre-2026-10-08 records (no mode) and rehearsal/test matches never
    # reached the run's save slot, so they cannot advance the campaign.
    director = QueenBloodCampaign(tmp_path)
    ned = "field_object:1110-KALMT:oba1110_Actor_CardGamer01"
    _append_finish(tmp_path, ned, True, at="2026-10-07T11:58:00Z", source=None, mode=None)
    _append_finish(tmp_path, ned, True, at="2026-10-07T11:59:00Z", mode="rehearsal")
    _append_finish(tmp_path, ned, False, at="2026-10-07T12:00:00Z", mode="test")

    state = _opponent(director.status(ASSIGNMENT), "oba1110_Actor_CardGamer01")
    assert state["state"] == "pending"
    assert state["attempts"] == 0 and state["wins"] == 0
    assert director.next_step(ASSIGNMENT)["opponent"]["name"] == "Ned"


def test_unsaved_live_win_requires_a_save_before_the_next_opponent(tmp_path: Path) -> None:
    from ff7r_agent.queen_blood_profile import QueenBloodProfile
    director = QueenBloodCampaign(tmp_path)
    ned = "field_object:1110-KALMT:oba1110_Actor_CardGamer01"
    _append_finish(tmp_path, ned, True, at="2026-10-07T12:00:00Z", saved=False)

    status = director.status(ASSIGNMENT)
    assert status["unsaved_live_wins"] == ["Ned"]
    assert _opponent(status, "oba1110_Actor_CardGamer01")["state"] == "pending"
    step = director.next_step(ASSIGNMENT)
    assert step["opponent"] is None and step["action"] == "save_game_progress"
    assert "Ned" in step["reason"]

    profile = QueenBloodProfile(tmp_path, ASSIGNMENT["profile_id"])
    assert profile.unsaved_live_wins() == [ned.lower()]
    assert profile.mark_live_wins_saved(saved_at="2026-10-07T12:05:00Z", receipt="r1") == 1
    assert profile.mark_live_wins_saved(saved_at="2026-10-07T12:06:00Z") == 0
    saved = director.status(ASSIGNMENT)
    assert saved["unsaved_live_wins"] == []
    assert _opponent(saved, "oba1110_Actor_CardGamer01")["state"] == "won"
    assert director.next_step(ASSIGNMENT)["opponent"]["name"] == "Nene"


def test_a_save_only_covers_wins_finished_before_it(tmp_path: Path) -> None:
    from ff7r_agent.queen_blood_profile import QueenBloodProfile
    _append_finish(tmp_path, "Ned", True, at="2026-10-07T12:10:00Z", saved=False)
    profile = QueenBloodProfile(tmp_path, ASSIGNMENT["profile_id"])
    assert profile.mark_live_wins_saved(saved_at="2026-10-07T12:00:00Z") == 0
    assert profile.unsaved_live_wins() == ["ned"]


def test_results_credited_under_the_old_rule_reset_to_pending(tmp_path: Path) -> None:
    # A campaign file written before the save rule may hold rehearsal wins.
    director = QueenBloodCampaign(tmp_path)
    director.status(ASSIGNMENT)
    store = json.loads(director.path.read_text())
    store["campaigns"][ASSIGNMENT["profile_id"]]["opponents"]["oba1110_Actor_CardGamer01"]["state"] = "won"
    director.path.write_text(json.dumps(store))
    _append_finish(tmp_path, "Ned", True, at="2026-10-07T12:00:00Z", mode="rehearsal")

    assert _opponent(director.status(ASSIGNMENT), "oba1110_Actor_CardGamer01")["state"] == "pending"
    assert director.status(ASSIGNMENT)["progress"] == 0


def test_campaign_results_are_reconciled_only_from_finish_records(tmp_path: Path) -> None:
    director = QueenBloodCampaign(tmp_path)
    ned = "field_object:1110-KALMT:oba1110_Actor_CardGamer01"
    assert _opponent(director.status(ASSIGNMENT), "oba1110_Actor_CardGamer01")["state"] == "pending"

    _append_finish(tmp_path, ned, False, at="2026-10-07T12:00:00Z")
    lost = _opponent(director.status(ASSIGNMENT), "oba1110_Actor_CardGamer01")
    assert lost["state"] == "lost" and lost["attempts"] == 1 and lost["losses"] == 1

    _append_finish(tmp_path, ned, True, at="2026-10-07T12:10:00Z")
    won = _opponent(director.status(ASSIGNMENT), "oba1110_Actor_CardGamer01")
    assert won["state"] == "won" and won["attempts"] == 2 and won["wins"] == 1

def test_campaigns_are_isolated_by_model_run_profile(tmp_path: Path) -> None:
    director = QueenBloodCampaign(tmp_path)
    other = {
        **ASSIGNMENT,
        "model_id": "gpt-6.2-sol",
        "slot": 16,
        "profile_id": "ChatGPT-gpt-6.2-sol-00000000000000000-slot016",
    }
    assert director.record_locked(ASSIGNMENT, "Ned", {"error": "npc target is unavailable"})

    assert _opponent(director.status(ASSIGNMENT), "oba1110_Actor_CardGamer01")["state"] == "locked"
    assert _opponent(director.status(other), "oba1110_Actor_CardGamer01")["state"] == "pending"
    store = json.loads((tmp_path / "campaigns.json").read_text(encoding="utf-8"))
    assert set(store["campaigns"]) == {ASSIGNMENT["profile_id"], other["profile_id"]}



def test_moore_twins_requires_one_finish_win_for_each_twin_before_zogan(tmp_path: Path) -> None:
    director = QueenBloodCampaign(tmp_path)
    younger = "field_object:1310-JNONV:oba1310_Actor_CardGamer02_Younger"
    older = "field_object:1310-JNONV:oba1310_Actor_CardGamer02"
    _win_all_before(tmp_path, MOORE_TWINS_ID)

    first_twin = director.next_step(ASSIGNMENT)
    assert first_twin["opponent"]["id"] == MOORE_TWINS_ID
    assert first_twin["target_id"] == younger

    _append_finish(tmp_path, younger, True, at="2026-10-07T13:00:00Z")
    first = director.next_step(ASSIGNMENT)
    state = _opponent(director.status(ASSIGNMENT), MOORE_TWINS_ID)
    assert state["state"] == "pending" and state["wins"] == 1 and state["required_matches"] == 2
    assert first["opponent"]["id"] == MOORE_TWINS_ID
    assert first["target_id"] == older
    assert "match 2 of 2" in first["reason"]

    # Replaying the already-won twin cannot satisfy the second required match.
    _append_finish(tmp_path, younger, True, at="2026-10-07T13:05:00Z")
    still_pending = director.next_step(ASSIGNMENT)
    assert still_pending["target_id"] == older
    assert _opponent(director.status(ASSIGNMENT), MOORE_TWINS_ID)["wins"] == 1

    _append_finish(tmp_path, older, True, at="2026-10-07T13:10:00Z")
    second = director.next_step(ASSIGNMENT)
    assert _opponent(director.status(ASSIGNMENT), MOORE_TWINS_ID)["state"] == "won"
    assert second["opponent"]["id"] == ZOGAN_ID


def test_losses_are_retried_immediately_and_stop_after_the_bound(tmp_path: Path) -> None:
    director = QueenBloodCampaign(tmp_path)
    ned = "field_object:1110-KALMT:oba1110_Actor_CardGamer01"
    for losses in range(1, MAX_LOSS_RETRIES + 1):
        _append_finish(tmp_path, ned, False, at=f"2026-10-07T14:0{losses}:00Z")
        # Resume from disk, with pending opponents still in the same region.
        retry = QueenBloodCampaign(tmp_path).next_step(ASSIGNMENT)
        assert retry["opponent"]["name"] == "Ned"
        assert retry["opponent"]["attempts"] == losses
        assert retry["target_id"] == ned
        # The route is always returned: a retry may follow a reload.
        assert retry["travel_spot"]["key"] == "$map1110"
        assert retry["input_sent"] is False
        assert retry["reason"] == (
            f"retry immediately after a loss; {MAX_LOSS_RETRIES - losses + 1} attempt(s) remain"
        )

    _append_finish(tmp_path, ned, False, at="2026-10-07T14:10:00Z")
    state = _opponent(director.status(ASSIGNMENT), "oba1110_Actor_CardGamer01")
    assert state["attempts"] == MAX_LOSS_RETRIES + 1 and state["retry_exhausted"] is True
    next_opponent = director.next_step(ASSIGNMENT)
    assert next_opponent["opponent"]["name"] == "Nene"
    assert next_opponent["travel_spot"]["key"] == "$map1110"


def test_fixed_order_ignores_location_and_later_losses(tmp_path: Path) -> None:
    # Owner decision 2026-10-08: always the catalogue order, Kalm first; a
    # loss to a later opponent (Riana, Costa del Sol) does not jump the queue.
    director = QueenBloodCampaign(tmp_path)
    riana = "field_object:2110-COSTT:oba2110_Actor_CardGamer03"
    _append_finish(tmp_path, riana, False, at="2026-10-08T12:00:00Z")
    director.record_available(ASSIGNMENT, "Riana")
    director.record_action(ASSIGNMENT, {"action": "finish_queens_blood_match",
                                      "opponent": "Ned", "ok": True, "won": True})

    step = director.next_step(ASSIGNMENT)
    assert step["opponent"]["name"] == "Ned"
    assert step["reason"] == "next opponent in the fixed campaign order"


def test_a_loss_is_retried_before_moving_on_in_the_fixed_order(tmp_path: Path) -> None:
    director = QueenBloodCampaign(tmp_path)
    _append_finish(tmp_path, "Ned", True, at="2026-10-08T11:00:00Z")
    _append_finish(tmp_path, "Nene", False, at="2026-10-08T12:00:00Z")
    # A newer win against a later opponent does not skip the Nene retry.
    _append_finish(tmp_path, "Isabelle", True, at="2026-10-08T12:05:00Z")
    # Ignore a loss from an operation that cannot confirm campaign results.
    _append_finish(tmp_path, "Ned", False, at="2026-10-08T12:10:00Z",
                   source="play_queens_blood_match")

    step = director.next_step(ASSIGNMENT)
    assert step["opponent"]["name"] == "Nene"
    assert "retry immediately after a loss" in step["reason"]
    assert step["travel_spot"] is not None


@pytest.mark.parametrize("failure_state", ["locked", "blocked"])
def test_latest_loss_does_not_override_harness_failure(tmp_path: Path, failure_state: str) -> None:
    director = QueenBloodCampaign(tmp_path)
    _append_finish(tmp_path, "Ned", False, at="2026-10-08T12:00:00Z")
    record_failure = director.record_locked if failure_state == "locked" else director.record_blocked
    record_failure(ASSIGNMENT, "Ned", {"error": "NPC unavailable or telemetry stale"})

    assert director.next_step(ASSIGNMENT)["opponent"]["name"] == "Nene"
    assert _opponent(director.status(ASSIGNMENT), "oba1110_Actor_CardGamer01")["state"] == failure_state


def test_latest_loss_still_requires_dialogue_recovery(tmp_path: Path) -> None:
    director = QueenBloodCampaign(tmp_path)
    _append_finish(tmp_path, "Ned", False, at="2026-10-08T12:00:00Z")
    director.record_action(ASSIGNMENT, {"action": "travel_to_map_spot", "opponent": "Nene",
        "ok": False, "reason": "dialogue_active", "timestamp_utc": "2026-10-08T12:01:00Z"})

    step = director.next_step(ASSIGNMENT)
    assert step["opponent"] is None
    assert step["reason"] == "capture_game_window_required"


@pytest.mark.parametrize("finished_id,retry_id", [
    (MOORE_TWINS_ID + "_Younger", MOORE_TWINS_ID),
    (MOORE_TWINS_ID, MOORE_TWINS_ID + "_Younger"),
])
def test_moore_twins_retry_unfinished_twin_and_preserve_the_other_win(
    tmp_path: Path, finished_id: str, retry_id: str,
) -> None:
    director = QueenBloodCampaign(tmp_path)
    _win_all_before(tmp_path, MOORE_TWINS_ID)
    _append_finish(tmp_path, finished_id, True, at="2026-10-08T12:00:00Z")
    target_id = f"field_object:1310-JNONV:{retry_id}"
    for losses in range(1, MAX_LOSS_RETRIES + 1):
        _append_finish(tmp_path, retry_id, False, at=f"2026-10-08T12:0{losses}:00Z")
        retry = director.next_step(ASSIGNMENT)
        assert retry["opponent"]["id"] == MOORE_TWINS_ID
        assert retry["opponent"]["wins"] == 1
        assert retry["target_id"] == target_id
        assert retry["travel_spot"] is not None
        assert "retry immediately after a loss" in retry["reason"]
        assert f"{MAX_LOSS_RETRIES - losses + 1} attempt(s) remain" in retry["reason"]
        assert "match 2 of 2" in retry["reason"]

    _append_finish(tmp_path, retry_id, False, at="2026-10-08T12:10:00Z")
    state = _opponent(director.status(ASSIGNMENT), MOORE_TWINS_ID)
    assert state["wins"] == 1 and state["retry_exhausted"] is True
    assert director.next_step(ASSIGNMENT)["opponent"]["id"] not in {MOORE_TWINS_ID, ZOGAN_ID}

    _append_finish(tmp_path, retry_id, True, at="2026-10-08T12:15:00Z")
    assert _opponent(director.status(ASSIGNMENT), MOORE_TWINS_ID)["wins"] == 2
    assert director.next_step(ASSIGNMENT)["opponent"]["id"] == ZOGAN_ID


def test_moore_twins_retry_the_recently_lost_member_when_both_are_unfinished(tmp_path: Path) -> None:
    director = QueenBloodCampaign(tmp_path)
    _win_all_before(tmp_path, MOORE_TWINS_ID)
    target_id = f"field_object:1310-JNONV:{MOORE_TWINS_ID}"
    _append_finish(tmp_path, target_id, False, at="2026-10-08T12:00:00Z")

    retry = director.next_step(ASSIGNMENT)
    assert retry["target_id"] == target_id
    assert "retry immediately after a loss" in retry["reason"]


def test_replaying_a_won_twin_and_losing_does_not_retry_it(tmp_path: Path) -> None:
    director = QueenBloodCampaign(tmp_path)
    _win_all_before(tmp_path, MOORE_TWINS_ID)
    _append_finish(tmp_path, MOORE_TWINS_ID, True, at="2026-10-08T12:00:00Z")
    _append_finish(tmp_path, MOORE_TWINS_ID, False, at="2026-10-08T12:05:00Z")

    step = director.next_step(ASSIGNMENT)
    state = _opponent(director.status(ASSIGNMENT), MOORE_TWINS_ID)
    assert state["wins"] == 1
    assert state["target_id"] == f"field_object:1310-JNONV:{MOORE_TWINS_ID}_Younger"
    assert step["target_id"] != f"field_object:1310-JNONV:{MOORE_TWINS_ID}"
    assert "retry immediately" not in step["reason"]


def test_locked_opponents_revisit_only_after_progress(tmp_path: Path) -> None:
    director = QueenBloodCampaign(tmp_path)
    assert director.record_locked(ASSIGNMENT, "Ned", {"error": "npc target is unavailable"})
    assert director.next_step(ASSIGNMENT)["opponent"]["name"] == "Nene"

    _append_finish(tmp_path, "field_object:1110-KALMT:oba1110_Actor_CardGamer02", True,
                   at="2026-10-07T15:00:00Z")
    _append_finish(tmp_path, "field_object:1110-KALMT:oba1110_Actor_CardGamer03", True,
                   at="2026-10-07T15:05:00Z")
    revisit = director.next_step(ASSIGNMENT)
    assert revisit["opponent"]["name"] == "Ned"
    assert "revisit after campaign progress" in revisit["reason"]


@pytest.mark.parametrize("operation", ["resolve_world_target", "plan_npc_approach"])
@pytest.mark.parametrize("status", ["known_but_not_currently_instantiated", "known_but_no_runtime_location"])
def test_lookup_of_an_unloaded_npc_neither_locks_nor_blocks_it(tmp_path: Path, operation: str, status: str) -> None:
    # Issue #322: Ned resolved from the Gold Saucer and Isabelle 137 m away
    # were both locked although neither had been approached or challenged.
    director = QueenBloodCampaign(tmp_path)
    assert director.record_action(ASSIGNMENT, {"action": operation, "opponent": "Ned", "ok": False,
        "reason": status, "status": status, "timestamp_utc": "2026-10-12T08:52:00Z"})

    status_after = director.status(ASSIGNMENT)
    ned = _opponent(status_after, "oba1110_Actor_CardGamer01")
    assert ned["state"] == "pending" and ned["locked_at_progress"] is None
    assert status_after["last_action"]["action"]["reason"] == status
    assert director.next_step(ASSIGNMENT)["opponent"]["name"] == "Ned"


def test_route_failure_after_an_unloaded_lookup_still_blocks(tmp_path: Path) -> None:
    director = QueenBloodCampaign(tmp_path)
    director.record_action(ASSIGNMENT, {"action": "resolve_world_target", "opponent": "Ned", "ok": False,
        "reason": "known_but_not_currently_instantiated", "timestamp_utc": "2026-10-12T08:52:00Z"})
    director.record_action(ASSIGNMENT, {"action": "run_live_route_adapter", "opponent": "Ned", "ok": False,
        "reason": "stuck", "timestamp_utc": "2026-10-12T08:53:00Z"})

    assert _opponent(director.status(ASSIGNMENT), "oba1110_Actor_CardGamer01")["state"] == "blocked"


def test_blocked_opponent_keeps_harness_evidence_and_is_not_routed(tmp_path: Path) -> None:
    director = QueenBloodCampaign(tmp_path)
    evidence = {"operation": "initiate_queens_blood_match", "error": "Queen's Blood telemetry is stale",
                "trace": list(range(20))}
    assert director.record_blocked(ASSIGNMENT, "Ned", evidence)

    state = _opponent(director.status(ASSIGNMENT), "oba1110_Actor_CardGamer01")
    assert state["state"] == "blocked"
    assert state["evidence"][-1]["error"] == evidence["error"]
    assert state["evidence"][-1]["trace"] == list(range(8, 20))
    assert director.next_step(ASSIGNMENT)["opponent"]["name"] == "Nene"


@pytest.mark.parametrize(("message", "expected"), [
    ("no_match_prompt: the conversation ended without the Queen's Blood prompt", "locked"),
    ("Queen's Blood NPC target is unavailable: known_but_not_currently_instantiated", "locked"),
    ("Queen's Blood telemetry is stale", "blocked"),
    ("Queen's Blood telemetry did not advance after Begin Match", "blocked"),
    ("Queen's Blood NPC target resolution exceeded the action deadline", "blocked"),
    ("Queen's Blood hand identities did not appear after 3 Begin Match attempts", "blocked"),
    ("real input is not manually armed", None),
])
def test_initiation_failure_classification(message: str, expected: str | None) -> None:
    assert classify_initiation_failure(message) == expected


def test_owner_override_unbound_dax_route_never_blocks_suggested_kadina(tmp_path):
    director = QueenBloodCampaign(tmp_path)
    director.status(ASSIGNMENT)
    store = json.loads(director.path.read_text())
    campaign = store["campaigns"][ASSIGNMENT["profile_id"]]
    campaign["selected_opponent"] = "oba2500_Actor_CardGamer02"
    director.path.write_text(json.dumps(store))
    before = director.status(ASSIGNMENT)
    accepted = director.record_action(ASSIGNMENT, {"action": "run_live_route_adapter", "ok": False,
        "reason": "cache_current_cloud_unresolvable", "timestamp_utc": "2026-10-08T00:29:27.170327Z",
        "arguments": {"route_target_position": {"x": 55217.621094, "y": -21741.910156, "z": 3189.329102}}})
    assert accepted is False
    after = director.status(ASSIGNMENT)
    assert after["opponents"] == before["opponents"]
    assert after["last_action"] == before["last_action"]
    assert after["receipt_count"] == before["receipt_count"]
    assert director.record_action(ASSIGNMENT, {"action": "run_live_route_adapter", "ok": False,
        "reason": "cache_current_cloud_unresolvable", "target_id": "field_object:2360-UNDRS:oba2360_Actor_CardGamer01"})
    assert _opponent(director.status(ASSIGNMENT), "oba2360_Actor_CardGamer01")["state"] == "blocked"
    assert _opponent(director.status(ASSIGNMENT), "oba2500_Actor_CardGamer02")["state"] == "pending"
    _append_finish(tmp_path, "Dax", True, at="2026-10-08T00:42:00Z")
    assert _opponent(director.status(ASSIGNMENT), "oba2360_Actor_CardGamer01")["state"] == "won"


def test_other_opponent_capture_does_not_clear_required_screenshot(tmp_path):
    director = QueenBloodCampaign(tmp_path)
    director.record_action(ASSIGNMENT, {"action": "travel_to_map_spot", "opponent": "Ned",
        "ok": False, "reason": "dialogue_active", "timestamp_utc": "2026-10-08T00:00:00Z"})
    director.record_action(ASSIGNMENT, {"action": "capture_game_window", "opponent": "Dax",
        "ok": True, "timestamp_utc": "2026-10-08T00:01:00Z"})
    assert director.next_step(ASSIGNMENT)["reason"] == "capture_game_window_required"
