"""Match provenance with fake telemetry/input and explicitly synthetic timing."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from ff7r_agent.queen_blood import QueenBloodState
from ff7r_agent.queen_blood_deck import MatchDeck
from ff7r_agent.queen_blood_profile import QueenBloodProfile
from ff7r_agent.service import AgentService, SafetyError
from test_agent import FakeArm, FakeEnvironment, FakeRealInput, StepClock
from test_queens_blood_match import MatchTelemetry, _queen_blood, _state

DECK = ["card_id_000", "card_id_000", "card_id_001", "card_id_001", "card_id_004",
        "card_id_004", "card_id_006", "card_id_006", "card_id_007", "card_id_007",
        "card_id_008", "card_id_008", "card_id_011", "card_id_077", "card_id_077"]
VERIFICATION = {"source": "deck_select_native_card_arrays", "profile_id": "offline", "slot": 13}
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "issue-356-nene-deck.json").read_text())


def _payload(hand=(), board=(), *, phase="player_turn", **overrides):
    placement = overrides.pop("placement", None)
    return _queen_blood(active=True, phase=phase,
                        hand=[{"hand_index": i, "card_id": card} for i, card in enumerate(hand)],
                        board={"cells": [{"owner": "player", "card_present": True, **cell}
                                         for cell in board], "placement": placement}, **overrides)


def _evidence(cards=DECK):
    return MatchDeck(list(cards), verification=VERIFICATION)


def _observe(evidence, hand=(), board=(), **kwargs):
    evidence.observe(QueenBloodState.from_dict(_payload(hand, board, **kwargs)))
    return evidence.record()


def _start_states(hand=None):
    prematch = _queen_blood(screen="deck_select", begin_match_available=True, match_screen=True,
                           deck_screen={"observed": True, "shown": True})
    mulligan = _payload(hand or DECK[0:5], phase="dealing", screen="mulligan")
    return [_state(1, queen_blood=prematch), _state(2, queen_blood=prematch),
            _state(3, queen_blood=mulligan)]


def _service(tmp_path, monkeypatch):
    backend = FakeRealInput()
    service = AgentService(MatchTelemetry(_start_states()), backend, arm_state=FakeArm(),
                           environment=FakeEnvironment(), monotonic=StepClock(),
                           minimum_action_delay=0.0, sleeper=lambda _seconds: None,
                           public_stream_event_dir=tmp_path / "events",
                           queen_blood_profile_root=tmp_path / "profiles-root")
    run = {"profile_id": "offline", "loaded_save_verified": True, "run_loaded": True,
           "assignment": None, "loaded_save": {"slot": 13, "process": {"pid": "fake", "token": "fake"}}}
    monkeypatch.setattr(service, "get_queens_blood_run_status", lambda: deepcopy(run))
    profile = QueenBloodProfile(tmp_path / "profiles-root", "offline")
    profile.record_deck(FIXTURE["deck"], source="previous_match")
    return service, backend, profile, run


def _bulk(cards=DECK, **overrides):
    return {"ok": True, "read_only": True, "source": "native_card_arrays", "controller": "fake-root",
            "profile_id": "offline", "slot": 13, "guard": "fake-guard",
            "arrays": {"Player": {"card_ids": list(cards)}}, **overrides}


def _finish(service, board=None, hand=()):
    if board is None:
        board = [{"card_id": "card_id_000", "row": 0, "column": 0, "power": 1}]
    service.telemetry = MatchTelemetry([_state(50, queen_blood=_payload(hand, board, phase="result"))])
    return service.finish_queens_blood_match(close_result=False)


def _resume(service):
    # Initiation deliberately latches stop. Model an authorized re-arm of the
    # fake backend between independent transactions; keep production gates.
    service._arm.reset_stop()
    service._arm.armed = True
    service._arm.remaining = 60.0
    service._stopped = False
    service.telemetry = MatchTelemetry(_start_states())


def _declare(tmp_path, mode):
    now = datetime.now(timezone.utc)
    started = (now - timedelta(minutes=10)).isoformat()
    driver = {"version": 1, "driver": "ChatGPT", "modelId": "gpt-6.1-sol", "effort": "max",
              "runId": "6a1e1f8c-2b7d-4c55-8a40-1e2f3a4b5c6d", "streamStartedAt": started,
              "declaredAt": (now - timedelta(minutes=9)).isoformat()}
    if mode == "rehearsal":
        driver["recordingId"] = "a" * 64
        marker = {"version": 2, "at": now.isoformat(), "recordingStartedAt": started,
                  "recordingId": "a" * 64}
    else:
        marker = {"version": 1, "at": now.isoformat(), "streamStartedAt": started}
    (tmp_path / f"public-stream-{mode}.json").write_text(json.dumps(marker))
    name = "public-driver-rehearsal.json" if mode == "rehearsal" else "public-driver.json"
    (tmp_path / name).write_text(json.dumps(driver))


def test_raw_nene_board_contradicts_stale_deck_without_reconstructing_it():
    result = _observe(_evidence(FIXTURE["deck"]), board=FIXTURE["player_cards"], phase="result")
    assert result["deck"] is None
    assert result["deck_provenance"]["status"] == "contradicted"
    assert result["deck_provenance"]["contradictions"] == [
        {"card_id": "card_id_077", "verified_copies": 0, "observed_copies": 1}]


@pytest.mark.parametrize("cards", [None, DECK[:14], ["unknown"] * 15])
def test_incomplete_or_missing_verification_never_becomes_a_deck(cards):
    evidence = MatchDeck(cards, verification=VERIFICATION)
    result = _observe(evidence, DECK)
    assert result["deck"] is None and result["deck_provenance"]["status"] == "unverified"
    assert MatchDeck(DECK).record()["deck"] is None


def test_repeated_reads_and_mulligan_redraw_do_not_count_as_additional_copies():
    evidence = _evidence()
    for hand in (["card_id_000"] * 2, ["card_id_001"] * 2, ["card_id_000"] * 2) * 4:
        result = _observe(evidence, hand, phase="dealing", screen="mulligan")
    assert result["deck"] == DECK
    assert result["deck_provenance"]["observed_minimum_copies"] == {"card_id_000": 2, "card_id_001": 2}


@pytest.mark.parametrize("hand,board", [(["card_id_077"] * 3, []),
    (["card_id_077"] * 2, [{"card_id": "card_id_077", "row": 0, "column": 0}]),
    ([], [{"card_id": "card_id_077", "row": i, "column": 0} for i in range(3)])])
def test_copy_excess_in_hand_board_or_both_invalidates_verification(hand, board):
    result = _observe(_evidence(), hand, board)
    assert result["deck"] is None
    assert result["deck_provenance"]["contradictions"] == [
        {"card_id": "card_id_077", "verified_copies": 2, "observed_copies": 3}]


def test_opponent_cards_and_unknown_hand_slots_are_not_our_deck_evidence():
    result = _observe(_evidence(), [None],
        [{"owner": "opponent", "card_id": "card_id_024", "row": 0, "column": 4}])
    assert result["deck"] == DECK
    assert result["deck_provenance"]["observed_minimum_copies"] == {}


def test_held_preview_and_synthetic_inactive_screen_do_not_add_copies():
    evidence = _evidence()
    _observe(evidence, ["card_id_077"] * 2,
             [{"card_id": "card_id_077", "row": 0, "column": 0}], placement={"active": True})
    evidence.observe(QueenBloodState.from_dict(_queen_blood(hand=[{"card_id": "card_id_024"}])))
    assert evidence.record()["deck"] == DECK


def test_consumed_copies_survive_destruction_and_duplicate_action_records():
    evidence = _evidence()
    for sequence in (10, 20):
        action = {"ok": True, "verified": True, "card_left_hand": True, "card_id": "card_id_077",
                  "pre_sequence": sequence, "post_sequence": sequence + 1, "row": 0, "column": 0}
        evidence.placement(action)
        evidence.placement(action)
    assert evidence.record()["deck"] == DECK  # two confirmed copies, repeated reports ignored
    result = _observe(evidence, ["card_id_077"])  # played cards now destroyed
    assert result["deck"] is None
    assert result["deck_provenance"]["contradictions"][0]["observed_copies"] == 3


@pytest.mark.parametrize("changes", [{"ok": False}, {"verified": False}, {"card_left_hand": False}])
def test_ambiguous_attempts_never_count_as_consumed_cards(changes):
    evidence = _evidence()
    evidence.placement({"ok": True, "verified": True, "card_left_hand": True,
                        "card_id": "card_id_024", **changes})
    assert evidence.record()["deck"] == DECK


def test_hand_contradiction_is_sticky_after_that_card_disappears():
    evidence = _evidence()
    _observe(evidence, ["card_id_024"])
    result = _observe(evidence, ["card_id_000"])
    assert result["deck"] is None and result["deck_provenance"]["status"] == "contradicted"


@pytest.mark.parametrize("mode", ["live", "rehearsal"])
@pytest.mark.parametrize("verified", [False, True])
def test_start_and_finish_write_only_the_current_match_deck(tmp_path, monkeypatch, mode, verified):
    service, backend, profile, _run = _service(tmp_path, monkeypatch)
    _declare(tmp_path, mode)
    reads = []

    def read(area):
        reads.append(area)
        return _bulk() if verified else None

    monkeypatch.setattr(service, "_read_queen_blood_arrays", read)
    opponent = "oba1110_Actor_CardGamer02"
    started = service.initiate_queens_blood_match(dialogue_confirmations=1, target_id=opponent)
    assert started["outcome"] == "mulligan_ready"
    assert started["deck"] == (DECK if verified else None)
    match_state = service._queen_blood_match
    assert match_state is not None and match_state["story_id"]
    assert match_state["deck_evidence"].record()["deck_provenance"] == started["deck_provenance"]
    assert [action[:2] for action in backend.actions] == [
        ("tap", "queen_blood_begin_match"), ("tap", "confirm")]
    assert reads == ["Player"] * (2 if verified else 1)
    # A subsequent inventory change cannot replace the match's evidence.
    profile.record_deck(["card_id_001"] * 15, source="unrelated_later_verification")
    finished = _finish(service)
    assert finished["deck"] == started["deck"]
    assert finished["deck_provenance"] == started["deck_provenance"]
    suffix = "-rehearsal" if mode == "rehearsal" else ""
    [history] = (tmp_path / f"qb-match-history{suffix}").glob("*.json")
    record = json.loads(history.read_text())
    assert record["deck"] == finished["deck"]
    from ff7r_agent.campaign_director import _history_record
    assert _history_record(record)["deck"] == finished["deck"]
    assert record["result"] == "win" and record["score"] == {"player": 1, "opponent": 0}
    assert Counter(record["deck"] or ()) == (Counter(DECK) if verified else Counter())
    assert len(list((tmp_path / f"public-qb-results{suffix}").glob("*.json"))) == 1
    if mode == "rehearsal":
        assert not (tmp_path / "qb-match-history").exists() and not (tmp_path / "public-qb-results").exists()
    assert service._queen_blood_match is None
    match = profile.opponent(opponent)["matches"][-1]
    assert match["won"] is True and match["mode"] == mode


def test_direct_to_mulligan_with_nene_cards_does_not_reuse_known_profile(tmp_path, monkeypatch):
    service, _backend, profile, _run = _service(tmp_path, monkeypatch)
    service.telemetry = MatchTelemetry(_start_states(["card_id_077", "card_id_077", "card_id_011",
                                                      "card_id_001", "card_id_001"]))
    started = service.initiate_queens_blood_match(dialogue_confirmations=1)
    assert started["state_inference"] == "identified_hand_after_begin_match"
    assert started["deck"] is None
    finished = _finish(service, FIXTURE["player_cards"])
    assert finished["deck"] is None and finished["deck_provenance"]["status"] == "unverified"
    assert profile.known_deck() == FIXTURE["deck"]


@pytest.mark.parametrize("second", [_bulk(DECK[::-1]), None, _bulk(DECK[:14])])
def test_copy_order_is_irrelevant_but_partial_or_missing_reads_are_unverified(tmp_path, monkeypatch, second):
    service, backend, _profile, _run = _service(tmp_path, monkeypatch)
    first = _bulk(DECK[:14]) if second is not None and len(second["arrays"]["Player"]["card_ids"]) == 14 else _bulk()
    reads = iter([first, second])
    monkeypatch.setattr(service, "_read_queen_blood_arrays", lambda _area: next(reads))
    evidence = service._match_deck_before_begin(_start_states()[0])
    assert evidence.record()["deck"] == (DECK[::-1] if second == _bulk(DECK[::-1]) else None)
    assert not backend.actions


@pytest.mark.parametrize("second", [_bulk(["card_id_001"] * 15), _bulk(guard="new-guard"),
                                    _bulk(slot=14), _bulk(controller="new-root")])
def test_changed_deck_or_source_stops_before_begin(tmp_path, monkeypatch, second):
    service, backend, _profile, _run = _service(tmp_path, monkeypatch)
    reads = iter([_bulk(), second])
    monkeypatch.setattr(service, "_read_queen_blood_arrays", lambda _area: next(reads))
    with pytest.raises(SafetyError, match="changed across match deck verification"):
        service.initiate_queens_blood_match(dialogue_confirmations=1)
    assert not backend.actions


def test_source_safety_refusal_is_not_swallowed(tmp_path, monkeypatch):
    service, backend, _profile, _run = _service(tmp_path, monkeypatch)

    def refuse(_area):
        raise SafetyError("the loaded save or deck screen changed during the bulk read")

    monkeypatch.setattr(service, "_read_queen_blood_arrays", refuse)
    with pytest.raises(SafetyError, match="loaded save or deck screen changed"):
        service.initiate_queens_blood_match(dialogue_confirmations=1)
    assert not backend.actions


def test_deck_select_stop_cannot_carry_verification_into_changed_match(tmp_path, monkeypatch):
    service, backend, profile, _run = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_read_queen_blood_arrays", lambda _area: pytest.fail("stop must not read/start"))
    stopped = service.initiate_queens_blood_match(stop_at_deck_select=True)
    assert stopped["outcome"] == "deck_select_ready" and not backend.actions
    changed = DECK[:13] + ["card_id_024", "card_id_024"]
    profile.record_deck(DECK, source="prior_explicit_read")
    monkeypatch.setattr(service, "_read_queen_blood_arrays", lambda _area: _bulk(changed))
    _resume(service)
    started = service.initiate_queens_blood_match(dialogue_confirmations=1)
    assert started["deck"] == changed


def test_begin_retry_reverifies_instead_of_carrying_first_attempt_deck(tmp_path, monkeypatch):
    service, _backend, _profile, _run = _service(tmp_path, monkeypatch)
    changed = DECK[:13] + ["card_id_024", "card_id_024"]
    reads = iter([_bulk(), _bulk(), _bulk(changed), _bulk(changed)])
    monkeypatch.setattr(service, "_read_queen_blood_arrays", lambda _area: next(reads))
    samples = iter([_start_states()[1], _start_states()[2]])
    monkeypatch.setattr(service, "_wait_for_queen_blood_sample", lambda *_args, **_kwargs: next(samples))
    started = service.initiate_queens_blood_match(dialogue_confirmations=1)
    assert started["deck"] == changed
    assert [item["attempt"] for item in started["inputs"] if item["button"] == "queen_blood_begin_match"] == [1, 2]


def test_contradicted_match_does_not_poison_or_supply_the_next_match(tmp_path, monkeypatch):
    service, _backend, _profile, _run = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_read_queen_blood_arrays", lambda _area: _bulk())
    service.initiate_queens_blood_match(dialogue_confirmations=1)
    service.telemetry = MatchTelemetry([_state(40, queen_blood=_payload(["card_id_024"]))])
    service.observe_queens_blood()
    assert _finish(service)["deck_provenance"]["status"] == "contradicted"
    _resume(service)
    assert service.initiate_queens_blood_match(dialogue_confirmations=1)["deck"] == DECK
    assert _finish(service)["deck"] == DECK
    monkeypatch.setattr(service, "_read_queen_blood_arrays", lambda _area: None)
    _resume(service)
    assert service.initiate_queens_blood_match(dialogue_confirmations=1)["deck"] is None
    assert _finish(service)["deck"] is None
    histories = [json.loads(path.read_text()) for path in (tmp_path / "qb-match-history").glob("*.json")]
    assert len(histories) == 3
    assert sum(item["deck"] is None for item in histories) == 2
    assert [item["deck"] for item in histories if item["deck"] is not None] == [DECK]


def test_worker_restart_marker_cannot_supply_a_verified_deck(tmp_path, monkeypatch):
    service, _backend, _profile, _run = _service(tmp_path, monkeypatch)
    (tmp_path / "public-qb-current.json").write_text(json.dumps({
        "version": 1, "active": True, "opponent": "Nene", "at": datetime.now(timezone.utc).isoformat()}))
    finished = _finish(service)
    assert finished["opponent"] == "Nene"
    assert finished["deck"] is None and finished["deck_provenance"]["status"] == "unverified"


def test_verified_stale_deck_is_invalidated_by_the_initial_mulligan(tmp_path, monkeypatch):
    service, _backend, _profile, _run = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_read_queen_blood_arrays", lambda _area: _bulk(FIXTURE["deck"]))
    service.telemetry = MatchTelemetry(_start_states(["card_id_077", "card_id_077", "card_id_011",
                                                      "card_id_001", "card_id_001"]))
    started = service.initiate_queens_blood_match(dialogue_confirmations=1)
    assert started["deck"] is None and started["deck_provenance"]["status"] == "contradicted"
    assert started["deck_provenance"]["contradictions"] == [
        {"card_id": "card_id_011", "verified_copies": 0, "observed_copies": 1},
        {"card_id": "card_id_077", "verified_copies": 0, "observed_copies": 2}]
    assert _finish(service)["deck_provenance"]["status"] == "contradicted"


def test_unscored_finish_keeps_same_match_verification_for_retry(tmp_path, monkeypatch):
    service, _backend, _profile, _run = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_read_queen_blood_arrays", lambda _area: _bulk())
    service.initiate_queens_blood_match(dialogue_confirmations=1)
    match = service._queen_blood_match
    unscored = _finish(service, [{"owner": "unknown", "card_id": "card_id_000", "row": 0, "column": 0}])
    assert unscored["ok"] is False and unscored["deck"] == DECK
    assert service._queen_blood_match is match
    assert not (tmp_path / "qb-match-history").exists()
    assert _finish(service)["deck"] == DECK and service._queen_blood_match is None


def test_process_receipt_change_between_reads_stops_without_begin(tmp_path, monkeypatch):
    service, backend, _profile, run = _service(tmp_path, monkeypatch)
    reads = []

    def read(_area):
        reads.append(1)
        if len(reads) == 2:
            run["loaded_save"]["process"]["token"] = "other-process"
        return _bulk()

    monkeypatch.setattr(service, "_read_queen_blood_arrays", read)
    with pytest.raises(SafetyError, match="changed across match deck verification"):
        service.initiate_queens_blood_match(dialogue_confirmations=1)
    assert not backend.actions


def test_unobserved_deck_screen_continues_with_null_without_bulk_input(tmp_path, monkeypatch):
    service, backend, _profile, _run = _service(tmp_path, monkeypatch)
    states = _start_states()
    for state in states[:2]:
        state.queen_blood["deck_screen"] = {"shown": True, "observed": False}
    service.telemetry = MatchTelemetry(states)
    monkeypatch.setattr(service, "_read_queen_blood_arrays", lambda _area: pytest.fail("no native deck receipt"))
    started = service.initiate_queens_blood_match(dialogue_confirmations=1)
    assert started["deck"] is None and started["outcome"] == "mulligan_ready"
    assert [action[:2] for action in backend.actions] == [
        ("tap", "queen_blood_begin_match"), ("tap", "confirm")]


def test_provenance_records_are_snapshots_not_mutable_match_state():
    cards = list(DECK)
    verification = {**VERIFICATION, "guard": {"filters": []}}
    evidence = MatchDeck(cards, verification=verification)
    cards[0] = "card_id_024"
    verification["guard"]["filters"].append("outside_change")
    record = evidence.record()
    record["deck"][0] = "card_id_024"
    record["deck_provenance"]["verification"]["source"] = "other"
    record["deck_provenance"]["verification"]["guard"]["filters"].append("response_change")
    _observe(evidence, ["card_id_000"])
    fresh = evidence.record()
    assert fresh["deck"] == DECK
    assert fresh["deck_provenance"]["verification"] == {**VERIFICATION, "guard": {"filters": []}}
    assert record["deck_provenance"]["observed_minimum_copies"] == {}


def test_already_open_begin_popup_has_no_match_deck_verification(tmp_path, monkeypatch):
    service, backend, _profile, _run = _service(tmp_path, monkeypatch)
    states = _start_states()
    for state in states[:2]:
        state.queen_blood["popups"] = {"begin_confirm": True}
    service.telemetry = MatchTelemetry(states)
    monkeypatch.setattr(service, "_read_queen_blood_arrays", lambda _area: pytest.fail("popup is not deck select"))
    started = service.initiate_queens_blood_match(dialogue_confirmations=1)
    assert started["deck"] is None
    assert [action[:2] for action in backend.actions] == [("tap", "confirm")]


def test_begin_transition_read_failure_keeps_the_verified_deck_with_the_match(tmp_path, monkeypatch):
    # Live: Confirm started the match, then the probe ticket timed out, so
    # the job failed after Begin and the finish record lost the deck.
    from ff7r_agent.telemetry import TelemetryError
    service, _backend, _profile, _run = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "_read_queen_blood_arrays", lambda _area: _bulk())

    def timed_out(*_args, **_kwargs):
        raise TelemetryError("fresh Queen's Blood sample request timed out")

    monkeypatch.setattr(service, "_wait_for_queen_blood_sample", timed_out)
    with pytest.raises(TelemetryError):
        service.initiate_queens_blood_match(dialogue_confirmations=1, target_id="oba2360_Actor_CardGamer02")
    finished = _finish(service)
    assert finished["deck"] == DECK and finished["deck_provenance"]["status"] == "verified"
    assert finished["opponent"]
