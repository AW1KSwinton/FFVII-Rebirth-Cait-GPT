"""#366 raw QB projections; field envelopes, repeats and timing are synthetic.

The source receipts are sparse observations, not a continuous recording.
Reactive pickup/cursor frames below are explicitly synthetic; raw final board
and hand evidence is kept separate and never used to infer unseen identities.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from ff7r_agent.queen_blood import match_identity_gaps
from ff7r_agent.qb_replay import ReplayState
from ff7r_agent.service import AgentService, SafetyError
from test_agent import FakeArm, FakeEnvironment, FakeRealInput
from test_queens_blood_match import _state


@pytest.fixture
def evidence():
    path = Path(__file__).parent / "fixtures/qb-recording/issue-366.json"
    return json.loads(path.read_text(encoding="utf-8"))


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.epoch = datetime.now(timezone.utc)

    def sleep(self, seconds):
        self.seconds += seconds

    def now(self):
        return self.epoch + timedelta(seconds=self.seconds)


class Samples:
    def __init__(self, payloads, clock):
        self.payloads = payloads
        self.clock = clock
        self.reads = 0

    @contextmanager
    def queen_blood_reads(self, mode="full"):
        yield  # Fake explicit consumer context, no probe or input backend.

    def observe(self):
        payload = deepcopy(self.payloads[self.reads])
        self.reads += 1
        state = replace(_state(self.reads, queen_blood=payload), timestamp_utc=self.clock.now().isoformat())
        return clock_state(state, self.clock)


def clock_state(state, clock):
    result = ReplayState(**state.__dict__)
    object.__setattr__(result, "_replay_clock", clock)
    return result


def service_for(telemetry, clock, backend=None):
    return AgentService(telemetry, backend or FakeRealInput(), arm_state=FakeArm(),
                        environment=FakeEnvironment(), minimum_action_delay=0,
                        monotonic=lambda: clock.seconds, sleeper=clock.sleep,
                        wall_clock=clock.now)


def test_preserved_incomplete_board_is_not_an_identified_snapshot(evidence):
    unknown = evidence["moore_player_incomplete"]["queen_blood"]
    gaps = match_identity_gaps(unknown)
    assert len([item for item in gaps if item.endswith("card_id")]) == 12
    assert len([item for item in gaps if item.endswith("power")]) == 12
    assert match_identity_gaps(evidence["moore_after"]["queen_blood"]) == []
    receipt = evidence["nene_receipt"]
    assert receipt["card_id"] == "unidentified_hand_slot_0"
    assert receipt["post_state"]["target_cell"]["card_id"] == "card_id_116"
    assert evidence["moore_receipt"]["post_state"]["placement_active"] is True


def test_verification_waits_for_two_complete_snapshots_without_merging(evidence):
    clock = Clock()
    unknown = evidence["moore_player_incomplete"]["queen_blood"]
    complete = evidence["nene_fresh"]["queen_blood"]
    # Repeated frames are synthetic. These two different match positions
    # must never be combined into an invented board.
    telemetry = Samples([unknown] * 4 + [complete] * 2, clock)
    service = service_for(telemetry, clock)
    result = service.observe_queens_blood()
    assert result["ok"] and telemetry.reads == 6
    assert result["queen_blood"]["hand"] == complete["hand"]
    assert result["queen_blood"]["board"]["cells"] == complete["board"]["cells"]
    assert not service.input.actions
    assert all("card_id" not in cell for cell in unknown["board"]["cells"] if cell["card_present"])


def test_equal_incomplete_snapshots_refuse_with_missing_field_evidence(evidence):
    clock = Clock()
    telemetry = Samples([evidence["moore_player_incomplete"]["queen_blood"]] * 8, clock)
    service = service_for(telemetry, clock)
    with pytest.raises(SafetyError, match="identities incomplete") as caught:
        service.observe_queens_blood()
    assert telemetry.reads == 8 and not service.input.actions
    assert caught.value.details["complete_samples"] == 0
    assert len(caught.value.details["incomplete_samples"]) == 8


def test_missing_sample_breaks_the_verification_pair(evidence):
    clock = Clock()
    complete = evidence["moore_after"]["queen_blood"]
    unknown = deepcopy(complete)
    for cell in unknown["board"]["cells"]:
        if cell["card_present"]:
            cell.pop("card_id")
    telemetry = Samples([complete, unknown, complete, unknown, complete, unknown, complete], clock)
    service = service_for(telemetry, clock)
    with pytest.raises(SafetyError, match="identities incomplete"):
        service.observe_queens_blood()
    assert telemetry.reads == 7 and not service.input.actions


@pytest.mark.parametrize("change", ["card_id", "power", "owner", "rank", "occupancy", "hand", "cursor", "lane", "placement", "phase", "turn", "result", "legal_moves"])
def test_genuine_changes_still_exhaust_four_complete_verification_samples(evidence, change):
    clock = Clock()
    original = evidence["moore_after"]["queen_blood"]
    changed = deepcopy(original)
    board = changed["board"]
    occupied = next(cell for cell in board["cells"] if cell["card_present"])
    if change in {"card_id", "power", "owner", "rank"}:
        occupied[change] = {"card_id": "card_id_007", "power": 99, "owner": "opponent", "rank": 3}[change]
    elif change == "occupancy":
        occupied["card_present"] = False
    elif change == "hand":
        changed["hand"][0]["card_id"] = "card_id_004"
    elif change == "cursor":
        changed["cursor"]["hand_index"] = 1
    elif change == "lane":
        board["lanes"][0]["player_power"] = 99
    elif change == "placement":
        board["placement"]["active"] = True
    elif change == "phase":
        changed["phase"] = "opponent_turn"
    elif change == "turn":
        changed["turn"] += 1
    elif change == "result":
        changed["result"]["shown"] = True
    elif change == "legal_moves":
        changed["legal_moves"] = [{"hand_index": 0, "row": 2, "column": 0}]
    telemetry = Samples([original, changed, original, changed], clock)
    service = service_for(telemetry, clock)
    with pytest.raises(SafetyError, match="state changed across four"):
        service.observe_queens_blood()
    assert telemetry.reads == 4 and not service.input.actions


def test_repeated_native_ack_cannot_verify_one_frame(evidence):
    clock = Clock()
    payload = deepcopy(evidence["moore_after"]["queen_blood"])
    payload["probe_request"] = "same-ticket"
    telemetry = Samples([payload] * 4, clock)
    with pytest.raises(SafetyError, match="state changed across four"):
        service_for(telemetry, clock).observe_queens_blood()


def test_unidentified_hand_slot_never_authorizes_pickup(evidence):
    clock = Clock()
    payload = deepcopy(evidence["nene_before"]["queen_blood"])
    # The Nene receipt proves an unidentified admission, not its full raw
    # admission snapshot. This loss of just that field is synthetic.
    payload["hand"][0].pop("card_id")
    telemetry = Samples([payload] * 8, clock)
    service = service_for(telemetry, clock)
    with pytest.raises(SafetyError, match="identities incomplete") as caught:
        service.place_queens_blood_card(0, 0, 0)
    assert len(caught.value.details["verification"]["incomplete_samples"]) == 8
    assert not any(action[0] == "tap" for action in service.input.actions)


class PlacementReplay(Samples):
    """Synthetic pickup and delayed timing around the unmodified Moore boards."""
    def __init__(self, evidence, backend, clock, recovery):
        super().__init__([], clock)
        self.evidence, self.backend, self.recovery = evidence, backend, recovery
        self.confirm_at = None

    def observe(self):
        self.clock.sleep(0.1)  # Synthetic producer/read cadence.
        confirms = sum(action[:2] == ("tap", "confirm") for action in self.backend.actions)
        key = "moore_before"
        if confirms >= 2:
            if self.confirm_at is None:
                self.confirm_at = self.clock.seconds
            key = "moore_incomplete"
            if self.recovery != "never" and self.clock.seconds - self.confirm_at >= 10:
                key = "moore_after"
        payload = deepcopy(self.evidence[key]["queen_blood"])
        if confirms == 1:
            payload["board"]["placement"] = {"active": True, "target_row": 1, "target_column": 0,
                                              "cursor_row": 1, "cursor_column": 0}
        if key == "moore_after" and self.recovery == "wrong_card":
            next(cell for cell in payload["board"]["cells"] if (cell["row"], cell["column"]) == (1, 0))["card_id"] = "card_id_007"
        if key == "moore_after" and self.recovery == "unknown_hand":
            payload["hand"][0].pop("card_id")
        self.reads += 1
        state = replace(_state(self.reads, queen_blood=payload), timestamp_utc=self.clock.now().isoformat())
        if key == "moore_after" and self.recovery == "stale":
            state = replace(state, timestamp_utc=(self.clock.now() - timedelta(seconds=10)).isoformat())
        if key == "moore_after" and self.recovery == "source_changed":
            state = replace(state, source="simulation")
        if key == "moore_after" and self.recovery == "stopped":
            state = replace(state, running=False)
        return clock_state(state, self.clock)


@pytest.mark.parametrize("recovery", ["landed", "never", "wrong_card", "stale", "unknown_hand", "source_changed", "stopped"])
def test_post_confirm_reconciliation_uses_only_fresh_exact_card_proof(evidence, recovery):
    clock, backend = Clock(), FakeRealInput()
    telemetry = PlacementReplay(evidence, backend, clock, recovery)
    service = service_for(telemetry, clock, backend)
    result = service.place_queens_blood_card(0, 1, 0, timeout_seconds=35)
    assert result["ok"] is (recovery in {"landed", "unknown_hand"})
    assert [action[1] for action in backend.actions if action[0] == "tap"] == ["confirm", "confirm"]
    assert result["pre_hand_card_count"] == 2
    assert result["post_hand_card_count"] == (None if recovery == "unknown_hand" else 1)
    assert result["card_left_hand"] is (None if recovery == "unknown_hand" else True)
    if recovery in {"landed", "unknown_hand"}:
        assert result["outcome"] == "card_placed"
        assert result["post_state"]["target_cell"]["card_id"] == "card_id_000"
        assert result["post_state"]["placement_active"] is False
    else:
        assert result["outcome"] == "placement_unverified"
        assert result["retry_safe"] is False and result["requires_fresh_observation"]
    assert clock.seconds < 35 and not service._action_lock.locked()


def test_probe_float_coordinates_are_board_positions():
    # The live sidecar encodes every Lua number as a float (row 0.000000).
    cells = [{"row": float(row), "column": float(column), "owner": "empty", "card_present": False}
             for row in range(3) for column in range(5)]
    payload = {"phase": "player_turn", "board": {"cells": cells},
               "hand": [{"hand_index": 0.0, "card_id": "card_id_000"}]}
    assert match_identity_gaps(payload) == []
    cells[0]["row"] = 0.5
    assert match_identity_gaps(payload) == ["board.cells"]
