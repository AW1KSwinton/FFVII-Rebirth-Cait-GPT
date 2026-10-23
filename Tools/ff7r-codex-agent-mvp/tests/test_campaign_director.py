from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import uuid

import pytest

from ff7r_agent.campaign_director import ALIASES, CampaignDirector, CampaignError, TWINS, YOUNGER
from ff7r_agent.qb_opponents import load_opponents, opponent
from ff7r_agent.queen_blood_runs import QueenBloodRuns
from ff7r_agent.runtime_commands import invoke_runtime
from ff7r_agent.service import AgentService


PROFILE = "ChatGPT-gpt-6.1-sol-00000000000000000-slot013"
DRIVER = {"driver": "ChatGPT", "model_id": "gpt-6.1-sol", "effort": "high",
          "run_id": "00000000-0000-0000-0000-000000000001"}
NED, NENE, ZAHIRA = [f"oba1110_Actor_CardGamer0{n}" for n in (1, 2, 3)]
ZOGAN = "oba1310_Actor_CardGamer03"


def create(tmp_path, opponents=None, **kwargs):
    store = CampaignDirector(tmp_path / "queen-blood")
    result = store.create(PROFILE, DRIVER["driver"], DRIVER["model_id"], opponents=opponents,
                          include_existing_history=True, **kwargs)
    return store, result["plan"]["campaign_id"]


def history(tmp_path, identifier=NED, result="win", *, at="2026-10-07T10:00:00Z", mode="live",
            profile=PROFILE, driver=None, name=None):
    score = {"player": 10 if result == "win" else 0, "opponent": 10 if result == "loss" else 0}
    if result == "draw":
        score = {"player": 10, "opponent": 10}
    record = {"version": 2, "at": at, "opponent": name or opponent(identifier).get("display_name"),
              "opponent_id": identifier, "driver": DRIVER if driver is None else driver,
              "result": result, "score": score, "margin": score["player"] - score["opponent"],
              "lanes": [[score["player"], score["opponent"]], [0, 0], [0, 0]], "duration_seconds": 60,
              "cards_played": 0, "opponent_cards_played": 0, "player_cards": [], "opponent_cards": [],
              "deck": None, "save_profile": profile}
    folder = tmp_path / ("qb-match-history" if mode == "live" else "qb-match-history-rehearsal")
    folder.mkdir(exist_ok=True)
    path = folder / f"{int(datetime.fromisoformat(at.replace('Z', '+00:00')).timestamp() * 1000)}-{uuid.uuid4()}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def action(store, *, identifier=NED, ok=False, reason="harness_failure", at="2026-10-07T09:00:00Z",
           operation="initiate_queens_blood_match", **kwargs):
    return store.record_action(PROFILE, DRIVER["driver"], DRIVER["model_id"], "live",
                              {"timestamp_utc": at, "action": operation, "opponent": identifier,
                               "ok": ok, "reason": reason, **kwargs})


def state(store, campaign_id, identifier):
    return next(item for item in store.status(campaign_id)["objectives"] if item["opponent_id"] == identifier)


def test_default_plan_matches_all_30_reviewed_regional_entries_and_aliases(tmp_path):
    store, campaign_id = create(tmp_path)
    plan = store.status(campaign_id)["plan"]
    catalogue = json.loads((Path(__file__).parent / "fixtures/regional-campaign.json").read_text(encoding="utf-8"))
    regional = catalogue["regional_ids"]
    assert len(plan["objectives"]) == len(regional) == 30
    assert [item["opponent_id"] for item in plan["objectives"]] == regional
    aliases = catalogue["aliases"]
    assert ALIASES == dict(aliases)
    assert sum(len(item["required_matches"]) for item in plan["objectives"]) == 31


def test_restart_recovers_wins_idempotently_without_mutating_history(tmp_path):
    store, campaign_id = create(tmp_path, [NED, NENE])
    path = history(tmp_path)
    original = path.read_bytes()
    assert store.reconcile(campaign_id, tmp_path)["imported"] == 1
    restarted = CampaignDirector(store.path.parent)
    assert restarted.reconcile(campaign_id, tmp_path)["imported"] == 0
    status = restarted.status(campaign_id)
    assert status["completed_opponents"] == ["Ned"]
    assert status["next_step"]["opponent"] == "Nene"
    assert path.read_bytes() == original
    assert restarted.create(PROFILE, DRIVER["driver"], DRIVER["model_id"], opponents=[NED, NENE],
                            include_existing_history=True)["resumed"]


def test_twins_preserve_first_win_and_never_offer_zogan_early(tmp_path):
    store, campaign_id = create(tmp_path, [TWINS, ZOGAN])
    history(tmp_path, TWINS)  # older shared-host history: Devin was offered first
    store.reconcile(campaign_id, tmp_path)
    assert store.status(campaign_id)["next_step"]["member"] == "Darren"
    assert state(store, campaign_id, TWINS)["state"] == "pending"
    history(tmp_path, TWINS, "loss", at="2026-10-07T10:01:00Z")
    store.reconcile(campaign_id, tmp_path)
    step = CampaignDirector(store.path.parent).next_step(campaign_id)["next_step"]
    assert step["member"] == "Darren" and step["attempts"] == 1
    history(tmp_path, TWINS, at="2026-10-07T10:02:00Z")
    store.reconcile(campaign_id, tmp_path)
    twin = state(store, campaign_id, TWINS)
    assert twin["completed"] and [item["wins"] for item in twin["required_matches"]] == [1, 1]
    assert store.next_step(campaign_id)["next_step"]["opponent"] == "Zogan"


def test_explicit_younger_repeated_win_cannot_complete_darren(tmp_path):
    store, campaign_id = create(tmp_path, [TWINS, ZOGAN])
    history(tmp_path, YOUNGER)
    history(tmp_path, YOUNGER, at="2026-10-07T10:01:00Z")
    store.reconcile(campaign_id, tmp_path)
    assert store.next_step(campaign_id)["next_step"]["member"] == "Darren"


def test_losses_are_deferred_and_each_required_match_has_a_retry_bound(tmp_path):
    store, campaign_id = create(tmp_path, [NED, NENE], max_attempts=2)
    history(tmp_path, NED, "loss")
    store.reconcile(campaign_id, tmp_path)
    assert state(store, campaign_id, NED)["state"] == "lost"
    assert store.next_step(campaign_id)["next_step"]["opponent"] == "Nene"
    history(tmp_path, NENE, at="2026-10-07T10:01:00Z")
    store.reconcile(campaign_id, tmp_path)
    step = store.next_step(campaign_id)["next_step"]
    assert step["opponent"] == "Ned" and step["reason"] == "retry_loss_after_other_opponents"
    history(tmp_path, NED, "loss", at="2026-10-07T10:02:00Z")
    store.reconcile(campaign_id, tmp_path)
    status = store.status(campaign_id)
    assert status["next_step"] is None and status["reason"] == "attempt_limit_reached"
    assert status["state"] == "pending"


def test_region_grouping_preserves_the_moore_twins_dependency(tmp_path):
    catalogue = json.loads(json.dumps(load_opponents()))
    identifiers = [NED, NENE, ZAHIRA]
    for row in catalogue["opponents"]:
        if row["id"] in identifiers:
            row["region"] = {"key": "region-b" if row["id"] == NENE else "region-a", "name": "A"}
    store, campaign_id = create(tmp_path, identifiers, catalogue=catalogue)
    history(tmp_path, NED)
    store.reconcile(campaign_id, tmp_path)
    assert store.next_step(campaign_id)["next_step"]["opponent_id"] == ZAHIRA


def test_locked_opponents_revisit_only_after_new_progress(tmp_path):
    store, campaign_id = create(tmp_path, [NED, NENE, ZAHIRA])
    action(store, reason="no_match_prompt: conversation ended without challenge")
    assert state(store, campaign_id, NED)["state"] == "locked"
    assert store.next_step(campaign_id)["next_step"]["opponent_id"] == NENE
    history(tmp_path, NENE, "loss")
    store.reconcile(campaign_id, tmp_path)
    assert not state(store, campaign_id, NED)["locked_retry_ready"]
    history(tmp_path, ZAHIRA, at="2026-10-07T10:01:00Z")
    store.reconcile(campaign_id, tmp_path)
    step = store.next_step(campaign_id)["next_step"]
    assert step["opponent_id"] == NED and step["reason"] == "revisit_locked_after_progress"
    action(store, reason="no_match_prompt", at="2026-10-07T10:02:00Z")
    assert not state(store, campaign_id, NED)["locked_retry_ready"]


def test_history_import_order_does_not_unlock_an_opponent_without_later_progress(tmp_path):
    store, campaign_id = create(tmp_path, [NED, NENE])
    action(store, reason="npc_absent", at="2026-10-07T11:00:00Z")
    history(tmp_path, NENE, at="2026-10-07T10:00:00Z")
    store.reconcile(campaign_id, tmp_path)
    assert not state(store, campaign_id, NED)["locked_retry_ready"]
    assert store.status(campaign_id)["next_step"] is None


@pytest.mark.parametrize("status", ["known_but_not_currently_instantiated", "known_but_no_runtime_location"])
def test_lookup_of_an_unloaded_npc_is_not_failure_evidence(tmp_path, status):
    store, campaign_id = create(tmp_path, [NED, NENE])
    action(store, operation="resolve_world_target", reason=status, status=status)
    ned = state(store, campaign_id, NED)
    assert ned["state"] == "pending" and ned["failure_evidence"] is None
    assert store.next_step(campaign_id)["next_step"]["opponent_id"] == NED
    action(store, reason="no_match_prompt", at="2026-10-07T10:00:00Z")
    assert state(store, campaign_id, NED)["state"] == "locked"


def test_harness_failure_is_blocked_with_evidence_and_not_a_loss(tmp_path):
    store, campaign_id = create(tmp_path, [NED])
    action(store, reason="conversation_not_observed", artifact_path="C:/evidence/talk.png")
    failed = state(CampaignDirector(store.path.parent), campaign_id, NED)
    assert failed["state"] == "blocked" and failed["attempts"] == 0
    assert failed["failure_evidence"]["action"]["artifact_path"] == "C:/evidence/talk.png"
    assert store.status(campaign_id)["reason"] == "harness_recovery_required"
    action(store, ok=True, reason="", outcome="mulligan_ready", at="2026-10-07T10:00:00Z")
    assert state(store, campaign_id, NED)["state"] == "pending"


def test_action_success_or_model_claim_cannot_mark_an_opponent_won(tmp_path):
    store, campaign_id = create(tmp_path, [NED])
    action(store, ok=True, operation="finish_queens_blood_match", won=True, state="completed")
    assert state(store, campaign_id, NED)["state"] == "pending"


def test_modes_models_slots_and_reasoning_efforts_have_the_expected_scope(tmp_path):
    store, campaign_id = create(tmp_path, [NED])
    history(tmp_path, mode="rehearsal")
    history(tmp_path, profile=PROFILE.replace("013", "015"))
    history(tmp_path, driver={**DRIVER, "model_id": "gpt-6-sol"})
    history(tmp_path, driver={**DRIVER, "driver": "Claude"})
    store.reconcile(campaign_id, tmp_path)
    assert not state(store, campaign_id, NED)["completed"]
    history(tmp_path, driver={**DRIVER, "effort": "low", "run_id": str(uuid.uuid4())})
    store.reconcile(campaign_id, tmp_path)
    assert state(store, campaign_id, NED)["completed"]


def test_invalid_records_are_reported_and_modified_evidence_is_preserved(tmp_path):
    store, campaign_id = create(tmp_path, [NED])
    path = history(tmp_path)
    value = json.loads(path.read_text())
    value["result"] = "loss"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert len(store.reconcile(campaign_id, tmp_path)["rejected"]) == 1
    value["result"] = "win"
    path.write_text(json.dumps(value), encoding="utf-8")
    store.reconcile(campaign_id, tmp_path)
    value["duration_seconds"] = 65
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CampaignError, match="evidence changed"):
        store.reconcile(campaign_id, tmp_path)
    assert state(store, campaign_id, NED)["required_matches"][0]["evidence"][0]["record"]["duration_seconds"] == 60


def test_corrupt_or_future_store_cannot_be_reset_by_create(tmp_path):
    store, campaign_id = create(tmp_path, [NED])
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA user_version=2")
    original = store.path.read_bytes()
    with pytest.raises(CampaignError, match="version"):
        store.create(PROFILE, DRIVER["driver"], DRIVER["model_id"])
    assert store.path.read_bytes() == original


def test_concurrent_history_reconciliation_keeps_one_receipt(tmp_path):
    store, campaign_id = create(tmp_path, [NED])
    history(tmp_path)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _: CampaignDirector(store.path.parent).reconcile(campaign_id, tmp_path), range(3)))
    assert sum(result["imported"] for result in results) == 1
    assert store.status(campaign_id)["receipt_count"] == 1


def test_stats_archive_between_twin_matches_preserves_both_separate_wins(tmp_path):
    store, campaign_id = create(tmp_path, [TWINS, ZOGAN])
    path = history(tmp_path, TWINS)
    store.reconcile(campaign_id, tmp_path)
    archive = tmp_path / "fixture-archive"
    archive.mkdir()
    path.replace(archive / path.name)
    history(tmp_path, TWINS, at="2026-10-07T10:01:00Z")
    restarted = CampaignDirector(store.path.parent)
    restarted.reconcile(campaign_id, tmp_path)
    assert [item["wins"] for item in state(restarted, campaign_id, TWINS)["required_matches"]] == [1, 1]
    assert restarted.next_step(campaign_id)["next_step"]["opponent"] == "Zogan"


def test_dialogue_failure_requires_capture_and_recovery_before_routing_on(tmp_path):
    store, campaign_id = create(tmp_path, [NED, NENE])
    action(store, operation="travel_to_map_spot", reason="dialogue_active")
    assert store.next_step(campaign_id)["next_step"] is None
    assert store.status(campaign_id)["reason"] == "capture_game_window_required"
    action(store, operation="capture_game_window", reason="capture_unavailable", at="2026-10-07T09:01:00Z")
    assert store.status(campaign_id)["reason"] == "capture_game_window_required"
    action(store, operation="capture_game_window", ok=True, reason="", path="C:/fixture/dialogue.png",
           at="2026-10-07T09:02:00Z")
    status = store.status(campaign_id)
    assert status["next_step"] is None and status["reason"] == "harness_recovery_required"
    assert state(store, campaign_id, NED)["failure_evidence"]["screenshots"][0]["path"] == "C:/fixture/dialogue.png"
    action(store, operation="finish_conversation", ok=True, reason="", at="2026-10-07T09:03:00Z")
    assert store.next_step(campaign_id)["next_step"]["opponent"] == "Ned"


def test_conflicting_plan_creation_preserves_the_original(tmp_path):
    store, campaign_id = create(tmp_path, [NED, NENE])
    original = store.path.read_bytes()
    with pytest.raises(CampaignError, match="different campaign"):
        store.create(PROFILE, DRIVER["driver"], DRIVER["model_id"], opponents=[NENE, NED], include_existing_history=True)
    assert store.path.read_bytes() == original
    assert store.status(campaign_id)["next_step"]["opponent"] == "Ned"


def test_visits_complete_only_after_an_actual_decline(tmp_path):
    store, campaign_id = create(tmp_path, [NED, NENE], allow_battles=False)
    action(store, ok=False, reason="no_match_prompt")
    assert state(store, campaign_id, NED)["state"] == "locked"
    action(store, ok=True, reason="", outcome="declined", at="2026-10-07T09:01:00Z")
    assert state(store, campaign_id, NED)["completed"]
    assert store.next_step(campaign_id)["next_step"]["allow_battles"] is False


@pytest.mark.parametrize("change", [
    {"score": {"player": True, "opponent": 0}}, {"margin": True},
    {"version": 1}, {"duration_seconds": float("nan")}, {"opponent_id": None},
    {"cards_played": True}, {"save_profile": None},
])
def test_incomplete_or_invalid_completion_facts_never_award_a_win(tmp_path, change):
    store, campaign_id = create(tmp_path, [NED])
    path = history(tmp_path)
    value = json.loads(path.read_text())
    value.update(change)
    path.write_text(json.dumps(value), encoding="utf-8")
    assert store.reconcile(campaign_id, tmp_path)["rejected"]
    assert state(store, campaign_id, NED)["state"] == "pending"


class NoGame:
    is_real = False
    def game_process_ids(self):
        return {}
    def game_process_identity_tokens(self, pids):
        return {}
    def wake_queen_blood_probe(self):
        raise AssertionError("campaign operations must not wake the probe")
    def __getattr__(self, name):
        raise AssertionError(f"campaign planning accessed the game: {name}")


class Processes:
    def game_process_ids(self):
        return {42: "ff7rebirth_.exe"}
    def game_process_identity_tokens(self, pids):
        return {42: "creation-1"}


def service_fixture(tmp_path, monkeypatch):
    import ff7r_agent.service as service_module
    monkeypatch.setattr(service_module, "match_record_target",
                        lambda _: (DRIVER, "public-qb-results", "qb-match-history"))
    root = tmp_path / "queen-blood"
    service = AgentService(NoGame(), NoGame(), environment=NoGame(), queen_blood_profile_root=root,
                           public_stream_event_dir=tmp_path / "events")
    file = tmp_path / "00000000000000000" / "ff7rebirth013.sav"
    file.parent.mkdir()
    file.write_bytes(b"offline fixture")
    save = {"path": str(file), "slot": 13, "steam_id": file.parent.name}
    service._queen_blood_runs.assign(DRIVER, save)
    return service, save


def test_mcp_operations_use_pr209_assignment_offline_and_survive_worker_restart(tmp_path, monkeypatch):
    service, _ = service_fixture(tmp_path, monkeypatch)
    status = invoke_runtime(service, None, None, "get_campaign_status", {})
    assert status["profile_id"] == PROFILE and len(status["opponents"]) == 30
    assert Path(status["campaign_file"]).parent == service._queen_blood_runs.runs_path.parent
    history(tmp_path)
    next_step = invoke_runtime(service, None, None, "get_campaign_next_step", {})
    assert next_step["input_sent"] is False and next_step["opponent"]["name"] == "Ned"  # Raw public history cannot bypass finish records.
    restarted = AgentService(NoGame(), NoGame(), environment=NoGame(), queen_blood_profile_root=service._queen_blood_profile_root,
                             public_stream_event_dir=tmp_path / "events")
    assert restarted.get_campaign_next_step()["profile_id"] == status["profile_id"]


def test_internal_hooks_record_lookup_failure_moves_and_speech_only_for_verified_save(tmp_path, monkeypatch):
    import ff7r_agent.service as service_module

    service, save = service_fixture(tmp_path, monkeypatch)
    # This case verifies the manual live receipt path. Production code now
    # correctly bypasses speech requirements when no fresh live marker exists.
    monkeypatch.setattr(service_module, "stream_is_live", lambda _state: True)
    service.get_campaign_next_step()
    service._environment = Processes()
    process = {"pid": "42", "token": "creation-1"}
    service._queen_blood_runs.complete_load(service._queen_blood_runs.begin_load(save, process), process)
    service.record_campaign_runtime_outcome("resolve_world_target", {"name": NED},
                                            {"ok": False, "status": "known_but_not_currently_instantiated"})
    status = service.get_campaign_status()
    # Not loaded here is no lock evidence (#322); the receipt is still kept.
    assert status["opponents"][0]["state"] == "pending"
    assert status["last_action"]["action"]["action"] == "resolve_world_target"
    service._queen_blood_match = {"opponent": NENE}
    from ff7r_agent.queen_blood_speech_receipt import binding, publish
    import time
    line = "I keep the middle lane open for the next card."
    publish(service._public_stream_event_dir, line, "calm", {"ok": True, "queued_at": "2026-10-08T00:00:00Z", "rehearsal": False},
            binding(service.get_queens_blood_run_status(), "live"), time.time())
    service._claim_campaign_speech_before_input()
    service._record_action(action="place_queens_blood_card", ok=True, row=1, column=0, hand_index=0)
    receipt = service.get_campaign_status()["last_action"]
    assert receipt["speech"]["response"]["ok"] and receipt["action"]["row"] == 1
    assert service._campaign_speech is None


def test_unassigned_run_cannot_create_a_campaign(tmp_path, monkeypatch):
    import ff7r_agent.service as service_module
    monkeypatch.setattr(service_module, "match_record_target", lambda _: (DRIVER, "results", "qb-match-history"))
    service = AgentService(NoGame(), NoGame(), environment=NoGame(), queen_blood_profile_root=tmp_path / "queen-blood",
                           public_stream_event_dir=tmp_path / "events")
    from ff7r_agent.service import SafetyError
    with pytest.raises(SafetyError, match="assign"):
        service.get_campaign_status()
    assert not service._queen_blood_campaign.path.exists()


def test_nested_navigation_failure_and_successful_recovery_are_recorded(tmp_path, monkeypatch):
    service, save = service_fixture(tmp_path, monkeypatch)
    service.get_campaign_next_step()
    service._environment = Processes()
    process = {"pid": "42", "token": "creation-1"}
    service._queen_blood_runs.complete_load(service._queen_blood_runs.begin_load(save, process), process)
    failed = {"navigation_objective": {"state": "NAVIGATION_OBJECTIVE_ABORTED"},
              "run": {"reason": "validator_failure", "evidence_path": "C:/fixture/route.json"}}
    service.record_campaign_runtime_outcome("run_live_route_adapter", {"target_id": NED}, failed)
    status = service.get_campaign_status()
    assert status["opponents"][0]["state"] == "blocked"
    assert status["opponents"][0]["evidence"][-1]["action"]["run"]["evidence_path"] == "C:/fixture/route.json"
    success = {"navigation_objective": {"state": "NAVIGATION_OBJECTIVE_COMPLETED"}, "run": {"reason": "arrived"}}
    service.record_campaign_runtime_outcome("run_live_route_adapter", {"target_id": NED}, success)
    assert service.get_campaign_status()["opponents"][0]["state"] == "pending"


def test_wrong_loaded_slot_cannot_supply_campaign_action_evidence(tmp_path, monkeypatch):
    service, _ = service_fixture(tmp_path, monkeypatch)
    status = service.get_campaign_next_step()
    service._environment = Processes()
    # No verified load receipt for this working slot; the selected run alone is insufficient.
    service.record_campaign_runtime_outcome("resolve_world_target", {"name": NED},
                                            {"ok": False, "status": "known_but_not_currently_instantiated"})
    assert service.get_campaign_status()["receipt_count"] == 0
    assert status["opponent"]["name"] == "Ned"


def test_finish_match_writer_is_the_authoritative_source_for_the_director(tmp_path, monkeypatch):
    from test_agent import FakeArm, FakeEnvironment, FakeRealInput
    from test_queens_blood_play import ReactiveGame
    import ff7r_agent.service as service_module
    from ff7r_agent.queen_blood_profile import QueenBloodProfile

    monkeypatch.setattr(service_module, "match_record_target", lambda _: (DRIVER, "public-qb-results", "qb-match-history"))
    store, campaign_id = create(tmp_path, [NED])
    backend = FakeRealInput()
    service = AgentService(ReactiveGame(backend), backend, arm_state=FakeArm(), environment=FakeEnvironment(),
                           minimum_action_delay=0, sleeper=lambda _: None, public_stream_event_dir=tmp_path / "events")
    monkeypatch.setattr(service, "queen_blood_profile", lambda: QueenBloodProfile(store.path.parent, PROFILE))
    service._queen_blood_match = {"opponent": NED, "started": service._monotonic() - 60}
    service.place_queens_blood_card(0, 1, 0)
    service.pass_queens_blood_turn()
    assert state(store, campaign_id, NED)["state"] == "pending"
    result = service.finish_queens_blood_match()
    assert result["outcome"] == "match_finished" and result["won"]
    assert store.reconcile(campaign_id, tmp_path)["imported"] == 1
    assert state(store, campaign_id, NED)["state"] == "won"


@pytest.mark.parametrize("mismatch", ["model", "save", "process"])
def test_authoritative_action_refuses_unverified_binding(tmp_path, monkeypatch, mismatch):
    service, save = service_fixture(tmp_path, monkeypatch)
    service._environment = Processes()
    process = {"pid": "42", "token": "creation-1"}
    service._queen_blood_runs.complete_load(service._queen_blood_runs.begin_load(save, process), process)
    service.get_campaign_next_step()
    if mismatch == "model":
        import ff7r_agent.service as module
        monkeypatch.setattr(module, "match_record_target", lambda _: ({**DRIVER, "model_id": "different-model"}, "results", "qb-match-history"))
    elif mismatch == "save":
        service._queen_blood_runs.invalidate("human loaded another save")
    else:
        service._environment = NoGame()
    service._record_campaign_action({"action": "place_queens_blood_card", "ok": True, "opponent": NED})
    original = service._queen_blood_runs.assignment(DRIVER)
    assert service._queen_blood_campaign.status(original)["receipt_count"] == 0


def test_action_mode_speech_and_explicit_opponent_survive_restart(tmp_path, monkeypatch):
    service, save = service_fixture(tmp_path, monkeypatch)
    service._environment = Processes()
    process = {"pid": "42", "token": "creation-1"}
    service._queen_blood_runs.complete_load(service._queen_blood_runs.begin_load(save, process), process)
    service.get_campaign_next_step()
    service._campaign_speech = {"text": "Keep the middle open.", "response": {"ok": True, "rehearsal": True}}
    service._record_campaign_action({"action": "place_queens_blood_card", "opponent": NED, "ok": True})
    receipt = service.get_campaign_status()["last_action"]
    assert receipt["speech"] is None and receipt["mode"] == "live"
    restarted = AgentService(NoGame(), NoGame(), environment=Processes(), queen_blood_profile_root=service._queen_blood_profile_root,
                             public_stream_event_dir=tmp_path / "events")
    restarted._record_campaign_action({"action": "pass_queens_blood_turn", "opponent": NED, "ok": True})
    status = restarted.get_campaign_status()
    assert status["receipt_count"] == 2 and status["last_action"]["opponent_id"] == NED
    assert status["progress"] == 0


def test_live_recording_refuses_missing_verified_working_save(tmp_path, monkeypatch):
    from ff7r_agent.service import SafetyError
    service, _ = service_fixture(tmp_path, monkeypatch)
    service.input.is_real = True
    with pytest.raises(SafetyError, match="verified loaded"):
        service.start_queens_blood_recording("refused")
    assert not service._qb_recorder.status()["enabled"]


def test_generic_runtime_route_cannot_reuse_old_match_or_last_action(tmp_path, monkeypatch):
    service, save = service_fixture(tmp_path, monkeypatch)
    service.get_campaign_next_step()
    service._environment = Processes()
    process = {"pid": "42", "token": "creation-1"}
    service._queen_blood_runs.complete_load(service._queen_blood_runs.begin_load(save, process), process)
    service._queen_blood_match = {"opponent": NENE}
    service._last_action = {"action": "finish_queens_blood_match", "opponent": NENE, "ok": True}
    before = service.get_campaign_status()
    service.record_campaign_runtime_outcome("run_live_route_adapter", {},
        {"navigation_objective": {"state": "NAVIGATION_OBJECTIVE_ABORTED"},
         "run": {"reason": "cache_current_cloud_unresolvable"}})
    after = service.get_campaign_status()
    assert after["opponents"] == before["opponents"]
    assert after["last_action"] == before["last_action"]
    assert after["receipt_count"] == before["receipt_count"]
