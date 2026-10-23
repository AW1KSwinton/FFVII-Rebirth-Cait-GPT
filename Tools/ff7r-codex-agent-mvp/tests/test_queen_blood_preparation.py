from copy import deepcopy

import pytest

from ff7r_agent.queen_blood_preparation import prepare_deck, preparation_context, revalidate_deck
from ff7r_agent.queen_blood_profile import QueenBloodProfile
from ff7r_agent.service import AgentService, SafetyError
from test_agent import FakeRealInput
from test_queen_blood_runs import DRIVER, Processes, save


CARDS = [f"card_id_{n:03}" for n in range(1, 16)]


def snapshots():
    return ({"profile_id": "verified-profile", "loaded_save_verified": True,
             "loaded_save": {"slot": 13, "process": {"pid": "42", "token": "creation-1"}, "load_id": "load-1"}},
            {"profile_id": "verified-profile", "scanned_at": "2026-10-07T14:00:00Z", "revision": 1,
             "cards": {c: {"owned": 2} for c in CARDS}})


def prepare(run, inventory, cards=None):
    return prepare_deck(run, inventory, cards=cards or CARDS, opponent="Ned", rationale="Keep low-cost openers.")


def test_candidate_preserves_duplicates_and_never_authorizes_input():
    run, inventory = snapshots()
    cards = CARDS[:-1] + [CARDS[0]]
    candidate = prepare_deck(run, inventory, cards=cards, opponent="Ned", rationale="Two openers.", fallback=CARDS)
    assert candidate["cards"].count(CARDS[0]) == 2
    assert not candidate["input_sent"] and candidate["fallback"] == CARDS
    checked = revalidate_deck(candidate, run, inventory, opponent="Ned")
    assert checked["cards"] == cards and checked["requires_deck_select_verification"]
    assert not checked["input_sent"]


@pytest.mark.parametrize("cards", [CARDS[:-1], CARDS + [CARDS[0]], [CARDS[0]] * 15, [None] * 15])
def test_incomplete_unknown_or_unowned_decks_are_rejected(cards):
    run, inventory = snapshots()
    with pytest.raises(ValueError):
        prepare(run, inventory, cards)


@pytest.mark.parametrize("change", ["profile", "reload", "restart", "rescan", "reward", "deck"])
def test_candidate_is_discarded_when_its_context_changes(change):
    run, inventory = snapshots()
    candidate = prepare(run, inventory)
    if change == "profile":
        run["profile_id"] = inventory["profile_id"] = "another-profile"
    elif change == "reload":
        run["loaded_save"]["load_id"] = "load-2"
    elif change == "restart":
        run["loaded_save"]["process"]["token"] = "creation-2"
    elif change in {"rescan", "reward"}:
        inventory["revision"] += 1
    else:
        inventory["known_deck"] = {"cards": list(reversed(CARDS))}
    with pytest.raises(ValueError, match="stale"):
        revalidate_deck(candidate, run, inventory, opponent="Ned")


def test_a_different_opponent_and_mutated_unowned_cards_are_rejected():
    run, inventory = snapshots()
    candidate = prepare(run, inventory)
    with pytest.raises(ValueError, match="opponent"):
        revalidate_deck(candidate, run, inventory, opponent="Riana")
    candidate["cards"][0] = "unknown-card"
    with pytest.raises(ValueError, match="owned"):
        revalidate_deck(candidate, run, inventory, opponent="Ned")


def test_snapshot_requires_a_verified_load_and_scanned_matching_profile():
    run, inventory = snapshots()
    for invalid_run, invalid_inventory in [({**run, "loaded_save_verified": False}, inventory),
                                            (run, {**inventory, "profile_id": "wrong"}),
                                            (run, {**inventory, "scanned_at": None})]:
        with pytest.raises(ValueError, match="verified"):
            prepare(invalid_run, invalid_inventory)


def test_preparation_runs_while_input_lock_is_held_without_touching_game(tmp_path, monkeypatch):
    import ff7r_agent.service as service_module
    monkeypatch.setattr(service_module, "match_record_target", lambda root: (DRIVER, "rehearsal", "history"))
    root = tmp_path / "profiles"
    service = AgentService(None, FakeRealInput(), environment=Processes(),
                           queen_blood_profile_root=root, public_stream_event_dir=tmp_path / "stream" / "events")
    assert service.complete_save_load(service.begin_save_load(save(tmp_path)))["verified"]
    profile = service.queen_blood_profile()
    profile.record_collection(CARDS, source="offline replay")
    before = deepcopy(profile.inventory())
    with service._action_lock:
        candidate = service.prepare_queens_blood_deck(CARDS, "Ned", "Keep low-cost openers.")
        assert service.revalidate_queens_blood_prepared_deck(candidate, "Ned")["ok"]
    assert profile.inventory() == before
    assert service.input.actions == []
    profile.record_collection(CARDS, source="same-second rescan")
    with pytest.raises(SafetyError, match="stale"):
        service.revalidate_queens_blood_prepared_deck(candidate, "Ned")


def test_reloading_the_same_save_creates_a_distinct_receipt(tmp_path):
    from ff7r_agent.queen_blood_runs import QueenBloodRuns
    store = QueenBloodRuns(tmp_path / "profiles")
    file = save(tmp_path)
    process = {"pid": "42", "token": "creation-1"}
    first = store.complete_load(store.begin_load(file, process), process)
    second = store.complete_load(store.begin_load(file, process), process)
    assert first["load_id"] != second["load_id"]


def test_context_supplies_owned_card_tactics_and_preserves_the_verified_deck():
    run, inventory = snapshots()
    inventory["known_deck"] = {"cards": CARDS, "verified_at": "fixture-time"}
    context = preparation_context(run, inventory, opponent="Ned")
    card = next(c for c in context["owned_cards"] if c["card_id"] == "card_id_004")
    assert card["name"] == "J-Unit Sweeper" and card["cost"] == 2
    assert card["owned"] == 2 and card["influence_offsets"]
    assert context["opponent_info"]["name"] == "Ned"
    assert len(context["opponent_info"]["first_match_deck"]) == 15
    assert context["read_only"] and not context["input_sent"]
    context["known_deck"]["cards"][0] = "changed"
    assert inventory["known_deck"]["cards"][0] == CARDS[0]


def test_unknown_opponent_or_card_does_not_invent_tactical_data():
    run, inventory = snapshots()
    inventory["cards"] = {"not-a-card": {"owned": 1}, "unowned": {"owned": 0}}
    context = preparation_context(run, inventory, opponent="not-an-opponent")
    assert context["opponent_info"]["first_match_deck"] is None
    assert context["owned_cards"] == [{"card_id": "not-a-card", "owned": 1,
                                       "count_source": None, "catalogue_available": False}]


@pytest.mark.parametrize("change", ["profile", "reload", "restart", "rescan", "reward", "deck"])
def test_a_context_change_during_reasoning_refuses_to_rebind_the_plan(change):
    run, inventory = snapshots()
    context = preparation_context(run, inventory, opponent="Ned")
    expected = context["snapshot_sha256"]
    assert prepare_deck(run, inventory, cards=CARDS, opponent="Ned", rationale="Openers.",
                        expected_snapshot_sha256=expected)["snapshot_sha256"] == expected
    if change == "profile":
        run["profile_id"] = inventory["profile_id"] = "new-profile"
    elif change == "reload":
        run["loaded_save"]["load_id"] = "new-load"
    elif change == "restart":
        run["loaded_save"]["process"]["token"] = "new-process"
    elif change == "deck":
        inventory["known_deck"] = {"cards": list(reversed(CARDS))}
    else:
        inventory["revision"] += 1
    with pytest.raises(ValueError, match="planning context is stale"):
        prepare_deck(run, inventory, cards=CARDS, opponent="Ned", rationale="Openers.",
                     expected_snapshot_sha256=expected)
