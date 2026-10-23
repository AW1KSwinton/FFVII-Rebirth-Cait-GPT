"""Issue #235: an input attempt must not report an unobserved turn as done."""
from dataclasses import replace

import pytest

from ff7r_agent.service import SafetyError
from test_agent import FakeRealInput
from test_queens_blood_play import ReactiveGame, _service


def test_dropped_pass_reports_failure_and_allows_an_explicit_retry(tmp_path):
    backend = FakeRealInput()
    game = ReactiveGame(backend, drop={1})
    service = _service(game, backend)
    events = tmp_path / "events"
    service._public_stream_event_dir = events
    # A previous exchange must not supply a stale cue for the failed pass.
    old_board = game.observe().queen_blood
    service._queen_blood_commentary_baseline = dict(old_board, phase="opponent_turn")

    result = service.pass_queens_blood_turn(timeout_seconds=1)

    assert result["ok"] is False
    assert result["verified"] is False
    assert result["outcome"] == result["reason"] == "pass_not_observed"
    assert result["post_sequence"] > result["pre_sequence"]
    assert result["queen_blood"]["phase"] == "player_turn"
    assert result["queen_blood"]["board"] == old_board["board"]
    assert service._last_action["ok"] is False
    assert service._last_action["outcome"] == "pass_not_observed"
    assert service._queen_blood_commentary_baseline is None
    assert "commentary" not in service.observe_queens_blood()
    assert not events.exists()
    assert [a for a in backend.actions if a[0] == "tap"] == [
        ("tap", "queen_blood_play", 2200),
    ]
    assert backend.release_count >= 2
    assert not service._action_lock.locked()

    retry = service.pass_queens_blood_turn(timeout_seconds=1)

    assert retry["ok"] is retry["verified"] is True
    assert retry["outcome"] == "turn_passed"
    assert "reason" not in retry
    assert len(list(events.glob("*.json"))) == 1
    assert len([a for a in backend.actions if a[0] == "tap"]) == 2


class PassEvidenceGame(ReactiveGame):
    """Control only the telemetry after the hold, without invoking the game."""

    def __init__(self, backend, *, phase, advancing=True, available=True):
        super().__init__(backend, drop={1})
        self.post_phase = phase
        self.advancing = advancing
        self.available = available
        self.pre_sequence = 0

    def observe(self):
        state = super().observe()
        if not any(action[0] == "tap" for action in self.backend.actions):
            self.pre_sequence = state.sequence
            return state
        payload = dict(state.queen_blood, phase=self.post_phase,
                       active=self.post_phase != "inactive") if self.available else None
        return replace(state, sequence=state.sequence if self.advancing else self.pre_sequence,
                       queen_blood=payload)


@pytest.mark.parametrize("phase", ["opponent_turn", "result", "inactive"])
def test_pass_accepts_fresh_turn_end_evidence(phase):
    backend = FakeRealInput()
    service = _service(PassEvidenceGame(backend, phase=phase), backend)

    result = service.pass_queens_blood_turn(timeout_seconds=1)

    assert result["ok"] is result["verified"] is True
    assert result["outcome"] == "turn_passed"


@pytest.mark.parametrize("phase", ["unknown", "dealing", "deck_select", "opponent_turn", "result"])
def test_pass_rejects_unknown_phases_and_nonadvancing_turn_end_evidence(phase):
    backend = FakeRealInput()
    advancing = phase not in {"opponent_turn", "result"}
    service = _service(PassEvidenceGame(backend, phase=phase, advancing=advancing), backend)

    result = service.pass_queens_blood_turn(timeout_seconds=1)

    assert result["ok"] is result["verified"] is False
    assert result["outcome"] == result["reason"] == "pass_not_observed"
    assert service._queen_blood_commentary_baseline is None


def test_pass_without_post_telemetry_clears_the_previous_commentary_baseline():
    backend = FakeRealInput()
    game = PassEvidenceGame(backend, phase="unknown", available=False)
    service = _service(game, backend)
    service._queen_blood_commentary_baseline = game.observe().queen_blood

    result = service.pass_queens_blood_turn(timeout_seconds=1)

    assert result["ok"] is result["verified"] is False
    assert result["queen_blood"] is None
    assert result["reason"] == "pass_not_observed"
    assert service._queen_blood_commentary_baseline is None


def test_dropped_placement_confirms_report_unverified_without_cancel():
    backend = FakeRealInput()
    game = ReactiveGame(backend, drop={2, 3})
    service = _service(game, backend)

    result = service.place_queens_blood_card(0, 0, 0)

    assert result["ok"] is result["verified"] is False
    assert result["outcome"] == "placement_unverified"
    assert result["retry_safe"] is False
    assert game.cards == {}
    assert game.placing is True
    assert [a[1] for a in backend.actions if a[0] == "tap"] == [
        "confirm", "confirm", "confirm",
    ]
    assert service._queen_blood_commentary_baseline is None
    assert not service._action_lock.locked()
