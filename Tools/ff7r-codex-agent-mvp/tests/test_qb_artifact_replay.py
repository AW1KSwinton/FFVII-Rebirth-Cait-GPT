"""Saved observations are verbatim projections; timing/field envelopes are synthetic."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from ff7r_agent.qb_replay import create_replay_service, ReplayError
from ff7r_agent.queen_blood import QueenBloodState
from ff7r_agent.service import SafetyError
from test_queen_blood_profile import _deck_service
from test_queens_blood_match import _queen_blood, _state


FIXTURE = Path(__file__).parent / "fixtures" / "qb-recording" / "regional-snapshots.json"


@pytest.fixture
def snapshots():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def replay(tmp_path, samples, *, mode="timeline"):
    epoch = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    header = {"schema_version": 1, "kind": "header", "timestamp_utc": epoch.isoformat(),
              "offset_seconds": 0, "run_id": "artifact-regression",
              "metadata": {"synthetic": "field envelope, sequence counters, timing and any repeated samples"}}
    events = [header]
    for index, (offset, qb) in enumerate(samples, 1):
        # The source evidence is QB-only. Do not pretend this envelope was live.
        state = _state(index, queen_blood=deepcopy(qb)).to_dict()
        payload = {"schema_version": 1, "sequence": index,
                   "timestamp_utc": (epoch + timedelta(seconds=offset)).isoformat(),
                   "game": {"running": True, "in_menu": False, "location": "fixture", "map_id": "EndGame"},
                   "player": state["player"], "queen_blood": deepcopy(qb)}
        events.append({"schema_version": 1, "kind": "telemetry", "event_id": index,
                       "timestamp_utc": payload["timestamp_utc"], "offset_seconds": offset,
                       "data": {"payload": payload}})
    events.append({"schema_version": 1, "kind": "summary", "offset_seconds": samples[-1][0],
                   "data": {"dropped_events": 0, "error": None}})
    path = tmp_path / "fixture.jsonl"
    path.write_text('\n'.join(json.dumps(e) for e in events) + '\n', encoding="utf-8")
    return create_replay_service(path, output_root=tmp_path / "offline", mode=mode)


def test_raw_deck_cursor_popup_visibility_and_closed_editor_gates(tmp_path, snapshots):
    service = replay(tmp_path, [(0, snapshots["deck"])])
    state = service.telemetry.observe()
    assert service._deck_total(state) == 15
    assert service._deck_active_area(service._deck_screen(state)) == "Player"
    # Live popup has a card_id but lacks popup_shown=True: it cannot authorize
    # a read. A leftover Strip/Edit cursor must not bypass the closed window.
    assert service._deck_editor_view(state, "Player") == ((0, 0), None)
    assert service._deck_editor_view(state, "Strip") == (None, None)
    assert service._deck_editor_view(state, "Edit") == (None, None)


def test_hidden_details_timeout_does_not_return_cached_card(tmp_path, snapshots):
    service = replay(tmp_path, [(0, snapshots["deck"]), (6, snapshots["deck"])])
    assert service._deck_popup_card("Player", (0, 0)) is None
    assert not service.input.actions


def test_full_deck_read_retains_live_duplicate_order_with_synthetic_cursor_events(tmp_path, snapshots):
    # The evidence has the full verified list but not all 15 cursor samples.
    # Existing reactive fake supplies explicitly synthetic intermediate events.
    service, game = _deck_service(tmp_path, snapshots["deck_cards"])
    assert service._read_deck_list_fast() == snapshots["deck_cards"]
    assert game.deck.count("card_id_000") == 2
    assert len(service._last_deck_read_timings) == 15


def test_lost_cursor_event_is_retried_without_hiding_shown_details(tmp_path, snapshots):
    service, game = _deck_service(tmp_path, snapshots["deck_cards"])
    original = game._press
    dropped = []
    def press(button):
        if button == "queen_blood_right" and not dropped:
            dropped.append(button)
            return
        original(button)
    game._press = press
    assert service._read_deck_list_fast() == snapshots["deck_cards"]
    assert dropped
    assert not any(action[:2] == ("tap", "queen_blood_details") for action in game.backend.actions)


def test_mulligan_submission_replays_opening_hand_to_player_turn(tmp_path, snapshots):
    opening, final = snapshots["mulligan_opening"], snapshots["mulligan_final"]
    assert QueenBloodState.from_dict(opening).phase == "dealing"
    service = replay(tmp_path, [(0, opening), (0.5, opening), (1, opening),
                                (2, final), (2.2, final), (2.4, final), (3, final), (4, final)])
    result = service.mulligan_cards([], begin_if_needed=False, submit_play=True,
                                    infer_current_hand_is_mulligan=True)
    assert result["verified"] is True
    assert result["queen_blood"]["phase"] == "player_turn"
    assert any(action.parameters.get("button") == "queen_blood_play" for action in service.input.actions)


def test_unknown_cursor_trace_never_invents_a_hand_index(tmp_path, snapshots):
    unknown = next(qb for qb in snapshots["mulligan_trace"] if qb["cursor"].get("area") == "unknown")
    service = replay(tmp_path, [(0, unknown)])
    queen = QueenBloodState.from_dict(service.telemetry.observe().queen_blood)
    with pytest.raises(SafetyError, match="cursor"):
        service._queen_blood_mulligan_cursor(queen)
    assert not service.input.actions


def test_match_loop_waits_opponent_transition_and_stops_on_recorded_result(tmp_path, snapshots):
    # No synthetic move selection: the legacy loop is opted in only to exercise
    # its wait/result branches. The director continues to choose no game moves.
    # Valid inactive samples are explicitly synthetic field/timing envelopes:
    # exit now needs two advancing field samples, then a settled-field check.
    inactive = _queen_blood()
    service = replay(tmp_path, [(0, snapshots["end_after"]), (0.2, snapshots["end_after"]),
                                (0.4, snapshots["result"]),
                                *[(offset, inactive) for offset in (0.6, 0.8, 1.0, 1.2, 1.4)]], mode="reads")
    result = service.play_queens_blood_match(mulligan=False, allow_automated_decisions=True)
    assert result["outcome"] == "match_finished"
    assert result["left_result_screen"] is True and result["field_settled"] is True
    assert not any(action.action == "tap" for action in service.input.actions)


def test_result_detection_scores_actual_saved_board_without_history_or_input(tmp_path, snapshots):
    assert snapshots["result"]["result"] == {"shown": True, "observed": True}
    service = replay(tmp_path, [(0, snapshots["result"])])
    result = service.finish_queens_blood_match(opponent="rolf", close_result=False)
    assert result["outcome"] == "match_finished"
    assert result["score"]["player"] == 6 and result["score"]["opponent"] == 13
    assert result["won"] is False
    assert service._public_stream_event_dir is None and service._queen_blood_profile_root is None
    assert not any(action.action == "tap" for action in service.input.actions)


def test_no_result_screen_cannot_be_promoted_to_a_finish(tmp_path, snapshots):
    service = replay(tmp_path, [(0, snapshots["turn_before"])], mode="reads")
    with pytest.raises((SafetyError, ReplayError)):
        service.finish_queens_blood_match(close_result=False)
    assert service._last_action is None
