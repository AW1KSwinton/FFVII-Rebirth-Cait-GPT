from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ff7r_agent.runtime_commands import SERVICE_ALIASES, invoke_runtime
from ff7r_agent.service import AgentService
from ff7r_agent.telemetry import JsonFileTelemetry, TelemetryError, _queen_blood_screen_shown


UI_OPERATIONS = [
    "observe_queens_blood", "mulligan_cards", "select_queens_blood_cards",
    "play_queens_blood_turn", "play_queens_blood", "play_queens_blood_match",
    "open_queens_blood_deck_editor",
    "probe_queens_blood_card_cells", "read_queens_blood_cards",
    "scan_queens_blood_collection", "edit_queens_blood_deck",
    "pass_queens_blood_turn", "finish_queens_blood_match",
    "place_queens_blood_card", "initiate_queens_blood_match",
    "run_initiate_queens_blood_match",
]


def service_for_operation(telemetry, operation):
    method = SERVICE_ALIASES.get(operation, "initiate_queens_blood_match")
    return SimpleNamespace(telemetry=telemetry, **{method: lambda **kwargs: {"ok": True}})


def test_request_writes_an_expiry_and_is_not_rewritten_until_half_elapsed(tmp_path, monkeypatch):
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    assert telemetry.request_queen_blood_probe(120)
    request = tmp_path / "queen_blood.request"
    expires = int(request.read_text(encoding="ascii"))
    assert time.time() + 100 < expires <= time.time() + 121
    before = (request.read_bytes(), request.stat().st_mtime_ns)
    assert telemetry.request_queen_blood_probe(120)
    assert (request.read_bytes(), request.stat().st_mtime_ns) == before
    monkeypatch.setattr("ff7r_agent.telemetry.time.time", lambda: expires - 59)
    assert telemetry.request_queen_blood_probe(120)
    assert int(request.read_text(encoding="ascii")) == expires + 61


def write_shown_probe_snapshot(telemetry):
    telemetry.queen_blood_path.write_text(json.dumps({
        "schema_version": 1, "base_sequence": 7,
        "queen_blood": {"schema_version": 1, "active": True, "phase": "result", "match_screen": True,
                        "cursor": {"area": "unknown"}, "hand": [], "legal_moves": [],
                        "board": {"rows": 3, "columns": 5, "cells": []}},
    }), encoding="utf-8")


@pytest.mark.parametrize("reader_kind", ["same_worker", "existing_peer", "restarted_worker"])
def test_released_request_cannot_be_renewed_by_a_lingering_result_snapshot(tmp_path, reader_kind, monkeypatch):
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    write_shown_probe_snapshot(telemetry)
    assert telemetry.request_queen_blood_probe()
    reader = telemetry if reader_kind == "same_worker" else JsonFileTelemetry(telemetry.path)
    if reader_kind == "existing_peer":
        assert reader.request_queen_blood_probe()
    assert telemetry.release_queen_blood_probe()
    if reader_kind == "restarted_worker":
        reader = JsonFileTelemetry(telemetry.path)
    request = telemetry.queen_blood_request_path
    before = (request.read_bytes(), request.stat().st_mtime_ns)
    assert before[0].strip() == b"0"

    assert reader._read_queen_blood({"sequence": 7})["phase"] == "result"
    assert (request.read_bytes(), request.stat().st_mtime_ns) == before
    # A lingering result cannot recreate the lease, even through a UI read.
    assert not reader.wake_queen_blood_probe(0.1)
    assert (request.read_bytes(), request.stat().st_mtime_ns) == before
    assert reader.request_queen_blood_probe()  # a later confirmed Yes
    def acknowledge(_):
        ticket = request.read_text(encoding="ascii").split()[2]
        reader.queen_blood_path.write_text(json.dumps({"queen_blood": {"probe_request": ticket}}))
    monkeypatch.setattr("ff7r_agent.telemetry.time.sleep", acknowledge)
    assert reader.wake_queen_blood_probe(0.1)
    assert reader._queen_blood_probe_request_expiry() > time.time()


@pytest.mark.parametrize("request_contents", [None, "0\n", "1\n", "invalid\n"])
def test_shown_snapshot_does_not_start_an_unrequested_probe(tmp_path, request_contents):
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    write_shown_probe_snapshot(telemetry)
    request = telemetry.queen_blood_request_path
    if request_contents is not None:
        request.write_text(request_contents, encoding="ascii")
    before = (request.read_bytes(), request.stat().st_mtime_ns) if request.exists() else None
    assert telemetry._read_queen_blood({"sequence": 7}) is not None
    after = (request.read_bytes(), request.stat().st_mtime_ns) if request.exists() else None
    assert after == before


def test_shown_snapshot_does_not_renew_or_request_a_sample(tmp_path, monkeypatch):
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    write_shown_probe_snapshot(telemetry)
    monkeypatch.setattr("ff7r_agent.telemetry.time.time", lambda: 1000)
    assert telemetry.request_queen_blood_probe()
    monkeypatch.setattr("ff7r_agent.telemetry.time.time", lambda: 1061)
    assert telemetry._read_queen_blood({"sequence": 7}) is not None
    assert telemetry.queen_blood_request_path.read_text(encoding="ascii") == "1120\n"


def test_failed_release_preserves_the_live_request_and_reports_failure(tmp_path, monkeypatch):
    from pathlib import Path
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    assert telemetry.request_queen_blood_probe()
    request = telemetry.queen_blood_request_path
    before = request.read_bytes()

    def sharing_lock(*args):
        raise PermissionError("request marker is locked")

    monkeypatch.setattr(Path, "replace", sharing_lock)
    assert not telemetry.release_queen_blood_probe()
    assert request.read_bytes() == before


def test_a_brief_sharing_violation_is_retried_without_leaving_temporary_files(tmp_path, monkeypatch):
    # Live 2026-10-12 (#328): one ticket write in many failed while the game
    # was reading the marker, and that aborted a challenge.
    from pathlib import Path
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    real_replace = Path.replace
    failures = [PermissionError("[WinError 5] Access is denied")] * 2

    def flaky(self, target):
        if failures:
            raise failures.pop()
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    assert telemetry.request_queen_blood_dialogue()
    assert telemetry.queen_blood_request_path.read_text(encoding="ascii").split()[1] == "dialogue"
    assert not failures and not list(tmp_path.glob("*.tmp"))


def test_a_failed_ticket_write_names_its_cause(tmp_path, monkeypatch):
    from pathlib import Path
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    assert telemetry.request_queen_blood_probe()
    monkeypatch.setattr("ff7r_agent.telemetry.QUEEN_BLOOD_REQUEST_WRITE_SECONDS", 0.0)

    def in_use(self, target):
        raise PermissionError("[WinError 32] in use")

    monkeypatch.setattr(Path, "replace", in_use)
    with pytest.raises(TelemetryError, match=r"cannot request a fresh Queen's Blood sample "
                                             r"\(PermissionError: \[WinError 32\] in use\)"):
        telemetry.wake_queen_blood_probe(0.1)
    assert not list(tmp_path.glob("*.tmp"))


def test_only_a_shown_card_game_screen_keeps_the_probe_requested():
    idle = {"active": False, "phase": "inactive", "match_screen": False, "probe_idle": True,
            "talk_menu": {"observed": False}}
    assert not _queen_blood_screen_shown(idle)
    assert _queen_blood_screen_shown({**idle, "talk_menu": {"observed": True}})
    assert _queen_blood_screen_shown({**idle, "deck_screen": {"observed": True}})
    assert _queen_blood_screen_shown({**idle, "match_screen": True})


@pytest.mark.parametrize("operation", UI_OPERATIONS[:-2])
def test_ui_operations_request_the_probe_when_wake_is_unavailable(operation):
    requests = []
    telemetry = SimpleNamespace(request_queen_blood_probe=lambda: requests.append(1))
    service = service_for_operation(telemetry, operation)
    assert invoke_runtime(service, None, None, operation) == {"ok": True}
    assert requests == [1]


def test_waking_the_probe_requires_its_matching_ticket(tmp_path, monkeypatch):
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    assert telemetry.request_queen_blood_probe()
    sidecar = tmp_path / "queen_blood.json"
    sidecar.write_text('{"queen_blood": {"probe_idle": true}}', encoding="utf-8")
    with pytest.raises(TelemetryError, match="timed out"):
        telemetry.wake_queen_blood_probe(0)
    assert (tmp_path / "queen_blood.request").exists()
    sidecar.write_text('{"queen_blood": {"screen": "deck_select"}}', encoding="utf-8")
    with pytest.raises(TelemetryError, match="timed out"):
        telemetry.wake_queen_blood_probe(0)
    def acknowledge(_):
        ticket = telemetry.queen_blood_request_path.read_text(encoding="ascii").split()[2]
        sidecar.write_text(json.dumps({"queen_blood": {"probe_request": ticket}}))
    monkeypatch.setattr("ff7r_agent.telemetry.time.sleep", acknowledge)
    assert telemetry.wake_queen_blood_probe(0.3)


@pytest.mark.parametrize("operation", UI_OPERATIONS[:-2])
def test_ui_operations_wake_the_probe_when_they_can(operation):
    calls = []
    telemetry = SimpleNamespace(request_queen_blood_probe=lambda: calls.append("request"),
                                wake_queen_blood_probe=lambda: calls.append("wake"))
    service = service_for_operation(telemetry, operation)
    assert invoke_runtime(service, None, None, operation) == {"ok": True}
    assert calls == ["wake"]


@pytest.mark.parametrize("operation", ["initiate_queens_blood_match", "run_initiate_queens_blood_match"])
def test_missing_field_npc_never_wakes_card_ui_discovery(operation):
    from test_queens_blood_match import _state
    calls = []
    state = _state(1, include_target=False)
    telemetry = SimpleNamespace(observe=lambda: state,
        request_queen_blood_probe=lambda: calls.append("request"),
        wake_queen_blood_probe=lambda: calls.append("wake"))
    service = service_for_operation(telemetry, operation)
    def missing(**kwargs):
        raise RuntimeError("NPC target unavailable")
    service.initiate_queens_blood_match = missing
    with pytest.raises(RuntimeError, match="NPC target unavailable"):
        invoke_runtime(service, None, None, operation)
    assert calls == []


@pytest.mark.parametrize("change", [{"paused": True}, {"in_menu": True},
                                   {"map_id": None}, {"player": None},
                                   {"queen_blood": {"deck_screen": {"observed": True}}}])
def test_initiation_never_starts_full_scans_before_yes_even_without_field_feed(change):
    from test_queens_blood_match import _state
    calls = []
    state = replace(_state(1), **change)
    telemetry = SimpleNamespace(observe=lambda: state,
        wake_queen_blood_probe=lambda: calls.append("wake"))
    service = service_for_operation(telemetry, "run_initiate_queens_blood_match")
    assert invoke_runtime(service, None, None, "run_initiate_queens_blood_match") == {"ok": True}
    assert calls == []


def test_dialogue_request_never_promotes_itself_through_a_ui_read(tmp_path, monkeypatch):
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    write_shown_probe_snapshot(telemetry)
    monkeypatch.setattr("ff7r_agent.telemetry.time.time", lambda: 1000)
    assert telemetry.request_queen_blood_dialogue()
    marker = telemetry.queen_blood_request_path
    assert marker.read_text(encoding="ascii") == "1120 dialogue\n"
    monkeypatch.setattr("ff7r_agent.telemetry.time.time", lambda: 1061)
    assert telemetry._read_queen_blood({"sequence": 7}) is not None
    assert not telemetry.wake_queen_blood_probe(0.1)
    assert marker.read_text(encoding="ascii") == "1120 dialogue\n"
    assert telemetry.request_queen_blood_probe()  # confirmed Yes switches scope
    assert marker.read_text(encoding="ascii") == "1181\n"


@pytest.mark.parametrize("operation", UI_OPERATIONS[:-2])
def test_field_ui_operations_cannot_create_a_full_scan_lease(tmp_path, operation):
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    service = service_for_operation(telemetry, operation)
    assert invoke_runtime(service, None, None, operation) == {"ok": True}
    assert not telemetry.queen_blood_request_path.exists()


def test_service_target_refusal_leaves_probe_idle_and_sends_no_input(monkeypatch):
    from test_queens_blood_match import _state, _service, MatchTelemetry, FakeRealInput
    from ff7r_agent.service import SafetyError
    telemetry = MatchTelemetry([_state(1, include_target=False)])
    calls = []
    telemetry.request_queen_blood_probe = lambda: calls.append("request")
    backend = FakeRealInput()
    service = _service(telemetry, backend)
    def missing(*args):
        raise SafetyError("NPC target unavailable")
    monkeypatch.setattr(service, "_resolve_queens_blood_target", missing)
    with pytest.raises(SafetyError, match="NPC target unavailable"):
        invoke_runtime(service, None, None, "run_initiate_queens_blood_match", {"target_id": "qb-npc"})
    assert calls == []
    assert backend.actions == []


@pytest.mark.parametrize("operation", [
    "get_queens_blood_run_status", "assign_queens_blood_run",
    "get_queens_blood_recording_status", "start_queens_blood_recording",
    "stop_queens_blood_recording", "prepare_queens_blood_deck",
    "revalidate_queens_blood_prepared_deck", "get_campaign_status",
    "get_campaign_next_step", "queens_blood_card_vendors", "observe_game",
    "tap_queens_blood_button",
])
@pytest.mark.parametrize("can_wake", [False, True])
def test_non_ui_operations_neither_wake_nor_request_the_probe(operation, can_wake):
    calls = []
    telemetry = SimpleNamespace(request_queen_blood_probe=lambda: calls.append("request"))
    if can_wake:
        telemetry.wake_queen_blood_probe = lambda: calls.append("wake")
    service = service_for_operation(telemetry, operation)
    assert invoke_runtime(service, None, None, operation) == {"ok": True}
    assert calls == []


@pytest.mark.parametrize("request_contents", [None, "0\n", "9999999999\n"])
def test_run_status_leaves_the_real_probe_request_untouched(tmp_path, request_contents):
    telemetry = JsonFileTelemetry(tmp_path / "state.json")
    # A non-idle sidecar makes an accidental wake return promptly, but the
    # status call must never consult it or write/renew the request.
    telemetry.queen_blood_path.write_text('{"queen_blood": {"probe_idle": false}}', encoding="utf-8")
    request = telemetry.queen_blood_request_path
    if request_contents is not None:
        request.write_text(request_contents, encoding="ascii")
    before = (request.read_bytes(), request.stat().st_mtime_ns) if request.exists() else None
    service = AgentService(telemetry, SimpleNamespace(), environment=SimpleNamespace(),
                           benchmark_root=tmp_path / "benchmarks",
                           human_recording_root=tmp_path / "human-recordings",
                           queen_blood_recording_root=tmp_path / "qb-recordings")

    for _ in range(2):
        result = invoke_runtime(service, None, None, "get_queens_blood_run_status")
        assert result["ok"] and result["assignment"] is None

    after = (request.read_bytes(), request.stat().st_mtime_ns) if request.exists() else None
    assert after == before
    assert not request.with_name(request.name + ".tmp").exists()


def test_unknown_queens_blood_operation_does_not_wake_the_probe():
    calls = []
    telemetry = SimpleNamespace(request_queen_blood_probe=lambda: calls.append("request"),
                                wake_queen_blood_probe=lambda: calls.append("wake"))
    with pytest.raises(ValueError, match="unsupported runtime operation"):
        invoke_runtime(SimpleNamespace(telemetry=telemetry), None, None, "unknown_queens_blood_status")
    assert calls == []
