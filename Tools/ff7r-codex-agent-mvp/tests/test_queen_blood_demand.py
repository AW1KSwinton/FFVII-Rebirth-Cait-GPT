"""Real probe request servicing: no scans while idle; fresh acknowledgements."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from ff7r_agent.runtime_commands import invoke_runtime
from ff7r_agent.telemetry import JsonFileTelemetry, TelemetryError
from ff7r_agent.service import AgentService, SafetyError
from test_queen_blood_probe_marker_sim import ProbeSim, DECK_SCREEN


@pytest.mark.parametrize("guide", ["select", "editor", "other", "tips"])
def test_released_probe_resumes_only_for_a_live_deck_guide(guide):
    from contextlib import nullcontext

    calls = []
    payload = {"probe_idle": True}
    sequence = 0

    def observe():
        nonlocal sequence
        sequence += 1
        return SimpleNamespace(sequence=sequence, queen_blood=dict(payload))

    def request():
        calls.append("request")
        payload.clear()
        payload.update(screen="deck_select", begin_match_available=True)
        return True

    service = object.__new__(AgentService)
    service.telemetry = SimpleNamespace(
        observe=observe, queen_blood_reads=lambda _: nullcontext(),
        request_queen_blood_probe=request)
    service._deck_screen_mode = lambda: guide
    service._sleeper = lambda _: None

    result = service._queen_blood_observe(stable=True)
    assert calls == (["request"] if guide in {"select", "editor"} else [])
    assert result.queen_blood == payload
    assert sequence >= 2  # Independent snapshots still required after recovery.


def test_visible_deck_does_not_hide_failed_probe_recovery():
    from contextlib import nullcontext

    service = object.__new__(AgentService)
    service.telemetry = SimpleNamespace(
        observe=lambda: SimpleNamespace(sequence=1, queen_blood=None),
        queen_blood_reads=lambda _: nullcontext(), request_queen_blood_probe=lambda: False)
    service._deck_screen_mode = lambda: "select"
    with pytest.raises(SafetyError, match="cannot resume"):
        service._queen_blood_observe(stable=True)


def test_completed_ticket_performs_no_more_object_reads_or_writes():
    sim = ProbeSim("slot_parent")
    sim.runtime.execute(DECK_SCREEN)
    state, _ = sim.api.serve("match", "first", "full", 1)
    assert state.probe_request == "first" and state.deck_screen.observed
    scans = sim.g.find_all_calls
    for sequence in range(2, 122):
        state, reason = sim.api.serve("match", "first", "full", sequence)
        assert state is None and reason == "awaiting_request"
    assert sim.g.find_all_calls == scans
    state, _ = sim.api.serve("match", "second", "full", 122)
    assert state.probe_request == "second" and state.deck_screen.observed
    # The known deck root bypasses UI/manager discovery, and census paths persist.
    assert sim.g.find_all_calls == scans


def test_cursor_acknowledgement_never_performs_global_discovery():
    sim = ProbeSim("slot_parent")
    sim.runtime.execute(DECK_SCREEN)
    sim.api.serve("match", "full", "full", 1)
    scans = sim.g.find_all_calls
    sim.g.test_top = sim.api.deck_top()
    sim.runtime.execute('test_top.__props.CardList_Player.__props.Txt_TotalNumber.__props.Text = "14/15"')
    state, _ = sim.api.serve("match", "cursor", "cursor", 2)
    assert state.probe_read == "cursor" and state.deck_screen.observed
    assert state.deck_screen.census is None and state.deck_screen.billboards is None
    assert state.deck_screen.deck.total == "14/15"
    assert sim.g.find_all_calls == scans


def test_lease_without_a_ticket_does_no_discovery_and_release_clears_it():
    sim = ProbeSim("slot_parent")
    scans = sim.g.find_all_calls
    state, reason = sim.api.serve("match", "-", "full", 1)
    assert state is None and reason == "awaiting_request"
    sim.api.serve(False, None, None, 2)
    assert sim.g.find_all_calls == scans


def test_new_pane_discovers_lazy_popup_once_without_waiting_for_periodic_scans():
    sim = ProbeSim("slot_parent", catalogue=True)
    sim.runtime.execute(DECK_SCREEN)
    sim.api.serve("match", "initial", "full", 1)
    scans = sim.g.find_all_calls
    sim.runtime.execute(r'''
      local path = "/Temp/Top.WidgetTree.Information.WidgetTree.Txt_Num_01"
      all_of.EndTextBlock = {Obj("EndTextBlock", path, {Visibility=0, Text="7"})}
      hooks["/Script/EndGame.EndNewCardDeckWindow:OnCardListEditCellSelectedIndexChanged"](
        nil, Param(0), Param(1))
    ''')
    cursor, _ = sim.api.serve("match", "cheap", "cursor", 2)
    assert cursor.deck_screen.detail.hovered is None
    assert sim.g.find_all_calls == scans
    fresh, _ = sim.api.serve("match", "pane", "full", 3)
    assert fresh.deck_screen.detail.hovered.card_id == "card_id_006"
    assert sim.g.find_all_calls == scans + 1  # only the new pane's text discovery
    again, _ = sim.api.serve("match", "cheap2", "cursor", 4)
    assert again.deck_screen.detail.hovered.card_id == "card_id_006"
    assert sim.g.find_all_calls == scans + 1


def test_refresh_requires_its_own_ack_and_does_not_accept_old_snapshot(tmp_path, monkeypatch):
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    telemetry.request_queen_blood_probe()
    telemetry.queen_blood_path.write_text(json.dumps({"queen_blood": {"probe_request": "old"}}))
    with pytest.raises(TelemetryError, match="timed out"):
        telemetry.wake_queen_blood_probe(0)
    def acknowledge(_):
        ticket = telemetry.queen_blood_request_path.read_text().split()[2]
        telemetry.queen_blood_path.write_text(json.dumps({"queen_blood": {"probe_request": ticket}}))
    monkeypatch.setattr("ff7r_agent.telemetry.time.sleep", acknowledge)
    assert telemetry.wake_queen_blood_probe(.1)


def test_consumer_contexts_restore_and_generic_reads_do_not_refresh(tmp_path, monkeypatch):
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    calls = []
    monkeypatch.setattr(telemetry, "_refresh_queen_blood", lambda mode: calls.append(mode))
    monkeypatch.setattr(telemetry, "_observe", lambda: "state")
    assert telemetry.observe() == "state" and not calls
    with telemetry.queen_blood_reads():
        telemetry.observe()
        with telemetry.queen_blood_reads("cursor"):
            telemetry.observe()
        telemetry.observe()
    telemetry.observe()
    assert calls == ["full", "cursor", "full"]


def test_runtime_scope_does_not_add_a_wake_scan_or_leak_after_failure():
    calls = []
    @contextmanager
    def reads(mode="full"):
        calls.append("enter")
        try:
            yield
        finally:
            calls.append("exit")
    def fail():
        raise ValueError("refused")
    telemetry = SimpleNamespace(queen_blood_reads=reads, wake_queen_blood_probe=lambda: calls.append("wake"))
    service = SimpleNamespace(telemetry=telemetry, observe_queens_blood=fail)
    with pytest.raises(ValueError, match="refused"):
        invoke_runtime(service, None, None, "observe_queens_blood")
    assert calls == ["enter", "exit"]


def test_async_conversation_recovery_reads_fresh_dialogue_samples(tmp_path, monkeypatch):
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    refreshes = []
    monkeypatch.setattr(telemetry, "_refresh_queen_blood",
                        lambda mode: refreshes.append(mode))
    monkeypatch.setattr(telemetry, "_observe", lambda: "live-wheel")

    def finish_conversation():
        assert telemetry.request_queen_blood_dialogue()
        assert telemetry.observe() == "live-wheel"
        assert telemetry.observe() == "live-wheel"
        return {"ok": True, "outcome": "declined"}

    service = SimpleNamespace(telemetry=telemetry, finish_conversation=finish_conversation)
    result = invoke_runtime(service, None, None, "run_finish_conversation")

    assert result["outcome"] == "declined"
    assert refreshes == ["full", "full"]
    telemetry.observe()
    assert refreshes == ["full", "full"]  # Generic reads stay idle afterwards.
