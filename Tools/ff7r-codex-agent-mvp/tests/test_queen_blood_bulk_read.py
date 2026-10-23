import copy
from types import SimpleNamespace
from threading import Lock

import pytest

from ff7r_agent import queen_blood_memory
from ff7r_agent.service import AgentService, SafetyError
from ff7r_agent.queen_blood_catalogue import card_id_for_key_item


def agent(monkeypatch):
    service = object.__new__(AgentService)
    service.input = SimpleNamespace(is_real=True)
    service.telemetry = SimpleNamespace(queen_blood_path="queen_blood.json", observe=lambda: {})
    service._action_lock = Lock()
    service._monotonic = lambda: 1.0
    service._record_action = lambda **_: None
    service._deck_screen_mode = lambda: "select"
    service._deck_screen = lambda _: {"shown": True, "observed": True}
    service._deck_total = lambda _: 3
    service.queen_blood_profile = lambda: object()
    loaded = {"loaded_save_verified": True, "profile_id": "slot15-model",
              "loaded_save": {"slot": 15, "process": {"pid": "7", "token": "8"}}}
    service.get_queens_blood_run_status = lambda: copy.deepcopy(loaded)
    receipt = {"ok": True, "controller": "current", "controller_address": "0x10000",
               "top_path": "top", "top_address": "0x20000", "guard": {"cursors": {}, "filters": []}}
    service._queen_blood_array_source = lambda _: copy.deepcopy(receipt)
    monkeypatch.setattr(queen_blood_memory, "read_card_arrays", lambda *_: {
        "ok": True, "source": "native_card_arrays", "arrays": {
            "Player": {"card_ids": ["card_id_000", "card_id_000", "card_id_002"]},
            "Edit": {"card_ids": ["card_id_000", "card_id_002"]}}})
    return service, loaded, receipt


def test_observation_identifies_visible_deck_when_native_probe_is_missing(monkeypatch):
    service, _, _ = agent(monkeypatch)
    service._queen_blood_observe = lambda **_: SimpleNamespace(sequence=123, queen_blood=None)
    service._queen_blood_inferred_turn_one_payload = lambda _: None
    service._queen_blood_inferred_mulligan_payload = lambda _: None

    result = service.observe_queens_blood()

    assert result == {
        "ok": False, "available": False, "sequence": 123,
        "reason": "queen_blood_deck_screen_visible_probe_unavailable",
        "deck_screen_mode": "select",
    }


def test_visible_deck_without_native_sample_refuses_bulk_read_without_input(monkeypatch):
    service, _, _ = agent(monkeypatch)
    service._deck_screen = lambda _: {}
    service._deck_observe = lambda **_: {}

    with pytest.raises(SafetyError, match="visible in the menu guide, but the native deck probe"):
        service.read_queens_blood_cards("Player")
    assert not service._action_lock.locked()


def test_open_editor_refreshes_full_probe_and_refuses_unobserved_visible_deck(monkeypatch):
    service, _, _ = agent(monkeypatch)
    service.input.release_all = lambda: None
    samples = []
    service._deck_observe = lambda **options: samples.append(options) or {}
    service._deck_screen = lambda _: {}

    with pytest.raises(SafetyError, match="visible in the menu guide, but the native deck probe"):
        service.open_queens_blood_deck_editor()

    assert samples == [{"full": True, "stable": True}]
    assert not service._action_lock.locked()


def test_bulk_public_read_keeps_duplicates_positions_and_sends_no_input(monkeypatch):
    service, _, _ = agent(monkeypatch)
    result = service.read_queens_blood_cards()
    assert result["ok"] and result["input_sent"] is False and result["population_calls"] == 0
    assert [(card["position"], card["card_id"]) for card in result["cards"]] == [
        (0, "card_id_000"), (1, "card_id_000"), (2, "card_id_002")]
    assert not service._action_lock.locked()


@pytest.mark.parametrize("guide", [
    "<button=square> Jump to Card  <button=R3> Copy Another Deck",
    "<button=R3>\u00a0Copy\u00a0Another\u00a0Deck  <button=options>\u00a0Craft\u00a0Prebuilt\u00a0Deck  "
    "<button=L1>/<button=R1>\u00a0View\u00a0Deck  <button=touch>\u00a0Toggle\u00a0Details  <button=cancel>\u00a0Back",
])
def test_both_editor_guides_allow_collection_reads_but_refuse_begin_match(monkeypatch, guide):
    service, _, _ = agent(monkeypatch)
    service.telemetry.path = "state.sample"
    del service._deck_screen_mode  # Exercise the real guide classifier.
    service._deck_screen_texts = lambda: [
        ("X.MainMenu_Card_Top.WidgetTree.Txt_Guide", guide)]

    result = service.read_queens_blood_cards("Edit")
    assert result["input_sent"] is False and result["population_calls"] == 0
    assert [card["card_id"] for card in result["cards"]] == ["card_id_000", "card_id_002"]
    with pytest.raises(SafetyError, match="deck select is not shown \\(editor\\)"):
        service._assert_deck_select_for_begin()
    with pytest.raises(SafetyError, match="Player card reads require select"):
        service.read_queens_blood_cards("Player")


@pytest.mark.parametrize("guide", ["View Deck", "Craft Prebuilt Deck", ""])
def test_partial_collection_guide_cannot_authorize_a_bulk_read(monkeypatch, guide):
    service, _, _ = agent(monkeypatch)
    service.telemetry.path = "state.sample"
    del service._deck_screen_mode
    service._deck_screen_texts = lambda: [
        ("X.MainMenu_Card_Top.WidgetTree.Txt_Guide", guide)]
    service._queen_blood_array_source = lambda _: pytest.fail("unknown menu must not request cards")
    with pytest.raises(SafetyError, match="observed other"):
        service.read_queens_blood_cards("Edit")


@pytest.mark.parametrize("change", ["cursor", "root", "save", "mode"])
def test_changed_ui_or_loaded_save_refuses_instead_of_falling_back_to_input(monkeypatch, change):
    service, loaded, receipt = agent(monkeypatch)
    requests = 0

    def source(_):
        nonlocal requests
        requests += 1
        value = copy.deepcopy(receipt)
        if requests == 2:
            if change == "cursor":
                value["guard"]["cursors"]["Player"] = {"index": 1}
            elif change == "root":
                value["controller_address"] = "0x30000"
            elif change == "save":
                loaded["loaded_save"]["slot"] = 13
            elif change == "mode":
                service._deck_screen_mode = lambda: "editor"
        return value

    service._queen_blood_array_source = source
    with pytest.raises(SafetyError, match="changed during"):
        service.read_queens_blood_cards()
    assert not service._action_lock.locked()


def test_unsupported_build_public_read_is_an_explicit_no_input_refusal(monkeypatch):
    service, _, _ = agent(monkeypatch)

    def unsupported(*_):
        raise ValueError("unsupported game build")

    monkeypatch.setattr(queen_blood_memory, "read_card_arrays", unsupported)
    result = service.read_queens_blood_cards()
    assert result == {"ok": False, "input_sent": False, "reason": "unsupported game build"}


def test_deck_reader_uses_live_bulk_array_without_popup_or_cursor_navigation(monkeypatch):
    service, _, _ = agent(monkeypatch)
    assert service._read_deck_list_fast() == ["card_id_000", "card_id_000", "card_id_002"]


@pytest.mark.parametrize("failure", ["bulk_card_source_timeout", "unsupported game build"])
def test_real_deck_reader_refuses_failed_bulk_read_before_any_cursor_input(monkeypatch, failure):
    service, _, _ = agent(monkeypatch)
    service._deck_observe = lambda **_: pytest.fail("must not start a serial card scan")
    service._read_queen_blood_arrays = lambda _: None
    service._bulk_card_fallback_reason = failure
    with pytest.raises(SafetyError, match=failure + ".*cursor fallback is disabled"):
        service._read_deck_list_fast()


def test_bulk_collection_scan_records_only_the_loaded_profile(monkeypatch):
    service, _, _ = agent(monkeypatch)
    recorded = []
    service._deck_screen_mode = lambda: "editor"
    def record(cards, **kwargs):
        recorded.append((cards, kwargs))
        return {"cards": {card: {"owned": 2, "count_source": "catalogue_copies"} for card in cards}}
    service.queen_blood_profile = lambda: SimpleNamespace(
        profile_id="slot15-model", record_collection=record)
    service.input.release_all = lambda: None
    result = service.scan_queens_blood_collection()
    assert result["input_sent"] is False and result["distinct_cards"] == 2
    assert recorded == [(["card_id_000", "card_id_002"], {"source": "native_card_arrays"})]
    assert result["inventory"] == {
        "card_id_000": {"owned": 2, "count_source": "catalogue_copies"},
        "card_id_002": {"owned": 2, "count_source": "catalogue_copies"},
    }


CLOSE_DECK = ["card_id_000", "card_id_000", "card_id_002", "card_id_002",
              "card_id_006", "card_id_006", "card_id_007", "card_id_007",
              "card_id_024", "card_id_116", "card_id_004", "card_id_010",
              "card_id_010", "card_id_118", "card_id_117"]


def closing_agent(monkeypatch, *, mode="editor"):
    service, _, _ = agent(monkeypatch)
    state = {"mode": mode, "cards": list(CLOSE_DECK), "keys": [], "reads": 0}
    service._deck_screen_mode = lambda: state["mode"]
    service.input.release_all = lambda: None
    service._read_deck_list_stable = lambda: list(state["cards"])
    service._sleeper = lambda _: None

    def bulk(area):
        assert area == "Edit"
        state["reads"] += 1
        return {"arrays": {"Player": {"card_ids": list(state["cards"])}},
                "slot": 15, "profile_id": "slot15-model", "controller": "live-editor", "guard": {}}

    def key(button):
        state["keys"].append(button)
        state["mode"] = "select"

    service._read_queen_blood_arrays = bulk
    service._deck_key = key
    return service, state


def test_close_unchanged_editor_preserves_duplicate_copies(monkeypatch):
    service, state = closing_agent(monkeypatch)
    result = service.close_queens_blood_deck_editor(CLOSE_DECK)
    assert result["deck_unchanged"] and result["cards"] == CLOSE_DECK
    assert result["outcome"] == "editor_closed" and result["input_sent"] is True
    assert state["reads"] == 2 and state["keys"] == ["queen_blood_cancel"]
    assert not service._action_lock.locked()


@pytest.mark.parametrize("draft", [CLOSE_DECK[:-1], CLOSE_DECK[:-1] + ["card_id_000"]])
def test_close_refuses_incomplete_or_changed_draft_before_escape(monkeypatch, draft):
    service, state = closing_agent(monkeypatch)
    state["cards"] = draft
    with pytest.raises(SafetyError, match="unsaved deck changes"):
        service.close_queens_blood_deck_editor(CLOSE_DECK)
    assert state["keys"] == [] and not service._action_lock.locked()


def test_close_refuses_a_changed_second_read_without_input(monkeypatch):
    service, state = closing_agent(monkeypatch)
    read = service._read_queen_blood_arrays

    def changed(area):
        bulk = read(area)
        if state["reads"] == 2:
            bulk["slot"] = 13
        return bulk

    service._read_queen_blood_arrays = changed
    with pytest.raises(SafetyError, match="loaded save changed"):
        service.close_queens_blood_deck_editor(CLOSE_DECK)
    assert state["keys"] == []


def test_close_already_selected_deck_is_input_free(monkeypatch):
    service, state = closing_agent(monkeypatch, mode="select")
    result = service.close_queens_blood_deck_editor(CLOSE_DECK)
    assert result["outcome"] == "already_closed" and result["input_sent"] is False
    assert state["keys"] == [] and state["reads"] == 0


def test_close_refuses_unverified_screen_and_missing_bulk_without_input(monkeypatch):
    service, state = closing_agent(monkeypatch, mode="other")
    with pytest.raises(SafetyError, match="verified deck screen"):
        service.close_queens_blood_deck_editor(CLOSE_DECK)
    state["mode"] = "editor"
    service._read_queen_blood_arrays = lambda _: None
    with pytest.raises(SafetyError, match="draft cannot be verified"):
        service.close_queens_blood_deck_editor(CLOSE_DECK)
    assert state["keys"] == []


def test_close_stops_after_one_escape_if_post_close_deck_changed(monkeypatch):
    service, state = closing_agent(monkeypatch)
    service._read_deck_list_stable = lambda: CLOSE_DECK[:-1] + ["card_id_000"]
    with pytest.raises(SafetyError, match="deck changed after closing"):
        service.close_queens_blood_deck_editor(CLOSE_DECK)
    assert state["keys"] == ["queen_blood_cancel"]


def test_filtered_collection_is_not_persisted_as_full_ownership(monkeypatch):
    service, _, receipt = agent(monkeypatch)
    service._deck_screen_mode = lambda: "editor"
    receipt["guard"]["filters"] = [{"group": 1, "index": 1}]
    service.input.release_all = lambda: None
    with pytest.raises(SafetyError, match="unfiltered"):
        service.scan_queens_blood_collection()


def test_unverified_save_refuses_before_request_or_memory_access(monkeypatch):
    service, loaded, _ = agent(monkeypatch)
    loaded["loaded_save_verified"] = False
    service._queen_blood_array_source = lambda _: pytest.fail("unverified save must not request a source")
    with pytest.raises(SafetyError, match="verified loaded save"):
        service.read_queens_blood_cards()


def test_native_item_rows_use_authored_catalogue_join_without_numeric_guessing():
    assert card_id_for_key_item("it_key_minigamecard_112") == "card_id_112"
    assert card_id_for_key_item("it_key_minigamecard_149") is None
    assert card_id_for_key_item("unrelated_000") is None


def test_source_request_uses_canonical_lf_bytes_on_windows_and_cleans_ticket(tmp_path):
    import json
    service = object.__new__(AgentService)
    service.telemetry = SimpleNamespace(queen_blood_path=tmp_path / "queen_blood.json")
    clock = [0.0]
    service._monotonic = lambda: clock[0]

    def consume(seconds):
        clock[0] += seconds
        request = (tmp_path / "queen_blood_cells.request").read_bytes()
        assert request.startswith(b"v2\t") and request.endswith(b"\t0\n")
        assert b"\r" not in request  # CRLF leaves a noncanonical last field in Lua
        ticket = request.split(b"\t")[1].decode()
        (tmp_path / "queen_blood_cells.json").write_text(json.dumps({"ticket": ticket, "ok": True}))

    service._sleeper = consume
    assert service._queen_blood_array_source("Player")["ok"]
    assert not (tmp_path / "queen_blood_cells.request").exists()
