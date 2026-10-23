from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ff7r_agent import save_slot_load, working_save_recovery as recovery
from ff7r_agent.queen_blood_runs import QueenBloodRuns, process_identity
from ff7r_agent.save_slot_save import SaveSlotWriter, SaveWriteLayout

LAYOUT = SaveWriteLayout("Slot", "Empty", ("Empty slot",), "RowButton", "PromptSlot", "Yes", "No")
DRIVER = {"driver": "ChatGPT", "model_id": "gpt-6.1-sol"}


class Environment:
    def __init__(self):
        self.ids, self.tokens = {42: "ff7rebirth_.exe"}, {42: "created-42"}

    def game_process_ids(self):
        return dict(self.ids)

    game_process_ids_verified = game_process_ids

    def game_process_identity_tokens(self, pids):
        return {p: self.tokens[p] for p in pids if p in self.ids and p in self.tokens}


class Game:
    """Only this fake game, never the writer, creates/modifies save data."""
    def __init__(self, tmp_path):
        self._environment = Environment()
        self._queen_blood_runs = QueenBloodRuns(tmp_path / "profiles")
        self._public_stream_event_dir = tmp_path / "stream" / "public-stream-events"
        self._queen_blood_profile_root = self._queen_blood_runs.root
        self.directory = tmp_path / "7656"
        self.directory.mkdir()
        for slot in (0, 8, 13, 14):
            self.path(slot).write_bytes(f"original-{slot}".encode())
            os.utime(self.path(slot), (1780000000 + slot * 60, 1780000000 + slot * 60))
        file = {"slot": 8, "steam_id": "7656", "path": str(self.path(8))}
        self._queen_blood_runs.assign(DRIVER, file)
        candidate = self._queen_blood_runs.begin_load(file, process_identity(self._environment))
        self._queen_blood_runs.complete_load(candidate, process_identity(self._environment))
        self.driver = dict(DRIVER)
        self.state, self.row, self.yes, self.now = "field", 8, False, 0.0
        self.order = [8, 9, 13, 14]
        self.writes, self.keys = [], []
        self.drop_up, self.empty_text, self.write_enabled = False, "Empty slot", True
        self.before_read = lambda: None
        self.cover_enabled = True
        self.sequence = 0

    def path(self, slot):
        return self.directory / f"ff7rebirth{slot:03}.sav"

    def _monotonic(self):
        return self.now

    def _sleeper(self, duration):
        self.now += duration

    def _ensure_game_foreground(self, **kwargs):
        pass

    def get_queens_blood_run_status(self):
        return self._queen_blood_runs.status(self.driver, process_identity(self._environment))

    def observe(self):
        return {"in_menu": self.state != "field", "telemetry": {"live": True}}

    def session_health(self):
        return {"loaded_gameplay": True, "in_menu": self.state != "field"}

    def _map_menu_key(self, button, milliseconds):
        self.keys.append(button)
        if button == "menu":
            self.state = {"field": "main", "main": "field", "submenu": "main",
                          "save_list": "submenu", "prompt": "save_list"}[self.state]
        elif self.state == "main" and button == "save_shortcut":
            self.state = "save_list"  # R opens the Save list (live 2026-10-08)
        elif self.state == "main" and button == "confirm":
            self.state = "submenu"
        elif self.state == "submenu" and button == "confirm":
            self.state = "save_list"
        elif self.state == "save_list":
            if button == "down":
                self.row = self.order[(self.order.index(self.row) + 1) % len(self.order)]
            if button == "confirm":
                self.state, self.yes = "prompt", False
        elif self.state == "prompt":
            if button == "cancel":
                self.state = "save_list"
            if button == "up" and not self.drop_up:
                self.yes = not self.yes
            if button == "confirm" and self.yes and self.write_enabled:
                self.path(self.row).write_bytes(b"progress-written-by-fake-game")
                os.utime(self.path(self.row), (1790000000, 1790000000))
                self.writes.append(self.row)
                self.state = "save_list"

    def stop(self):
        # Simulated streamer: acknowledge only an already published recovery.
        from ff7r_agent.queen_blood_profile import _read, _write, _now
        public = _read(self._public_stream_event_dir.parent / "public-working-save-recovery.json")
        if self.cover_enabled and public.get("phase") == "recovering":
            _write(self._public_stream_event_dir.parent / "working-save-recovery-cover.json",
                   {"version": 1, "recoveryId": public["recoveryId"], "covered": True, "at": _now()})
        return {"ok": True}

    def snapshot(self):
        self.before_read()
        self.sequence += 1
        def node(name, **kwargs):
            return {"name": name, "visible": True, **kwargs}
        signals = {"field": None, "main": "$help_message_0000_0009", "submenu": "$help_message_0008_0000",
                   "save_list": "$help_message_0008_0004", "prompt": "$menu_save_load_0002_0000"}
        response = {"texts_scope": "menu", "texts": [{"text": signals[self.state]}] if signals[self.state] else [],
                    "schema_version": 1, "source": "ue4ss_map_query",
                    "request_id": f"{self.sequence:032x}", "source_sequence": self.sequence}
        if self.state == "save_list":
            cells = []
            for number in self.order:
                nodes = [node("Slot", text=str(number)), node("RowButton", keyboard_focus=number == self.row)]
                if self.path(number).exists():
                    date = datetime.fromtimestamp(self.path(number).stat().st_mtime)
                    nodes += [node("Txt_Date", text=date.strftime("%m/%d/%Y")), node("Txt_Time", text=date.strftime("%I:%M:%S %p"))]
                else:
                    nodes.append(node("Empty", text=self.empty_text))
                cells.append({"nodes": nodes})
            response["save_cells"] = cells
        if self.state == "prompt":
            response["save_dialogs"] = [{"nodes": [node("PromptSlot", text=str(self.row)),
                node("Yes", keyboard_focus=self.yes), node("No", keyboard_focus=not self.yes)]}]
        return response


@pytest.fixture
def game(tmp_path, monkeypatch):
    game = Game(tmp_path)
    monkeypatch.setattr(save_slot_load, "menu_snapshot", lambda service, **kwargs: (service.snapshot(), {}))
    monkeypatch.setenv("FF7R_SAVE_MENU_LAYOUT", str(tmp_path / "no-load-layout.json"))
    monkeypatch.delenv("FF7R_SAVE_WRITE_LAYOUT", raising=False)
    return game


def write(game, slot=None, execute=True):
    return SaveSlotWriter(game, write_layout=LAYOUT).run(slot, execute)


def test_owned_save_is_backed_up_written_verified_and_receipted(game):
    protected = {n: game.path(n).read_bytes() for n in (0, 13, 14)}
    result = write(game)
    assert result["ok"] and result["saved"] and result["closed"]
    assert game.writes == [8] and Path(result["backup"]).read_bytes() == b"original-8"
    assert result["file_proof"]["sha256"] and game.get_queens_blood_run_status()["run_loaded"]
    assert game.get_queens_blood_run_status()["loaded_save"]["source"] == "verified_save_menu"
    assert protected == {n: game.path(n).read_bytes() for n in protected}


def test_explicit_empty_slot_becomes_working_save_and_retains_previous_ownership(game):
    previous_profile = game.get_queens_blood_run_status()["profile_id"]
    result = write(game, 9)
    assert result["ok"] and result["new_slot"] and game.writes == [9]
    assert game.path(8).read_bytes() == b"original-8"
    assert game.get_queens_blood_run_status()["assignment"]["slot"] == 9
    assert game.get_queens_blood_run_status()["profile_id"] == previous_profile
    assert game._queen_blood_runs.owner("7656", 8) == "ChatGPT:gpt-6.1-sol"
    with pytest.raises(ValueError, match="another model"):
        game._queen_blood_runs.assign({"driver": "Claude", "model_id": "claude-test"},
                                     {"slot": 8, "steam_id": "7656", "path": str(game.path(8))})


@pytest.mark.parametrize("slot", [0, 14, 13, True, "8", -1, 1000])
def test_reserved_or_foreign_occupied_slots_refused_before_any_input(game, slot):
    result = write(game, slot)
    assert not result["ok"] and not game.keys and not game.writes


def test_assigned_slot_above_15_is_saved(game):
    # #262: a live run assigned to slot 16 could not save its win.
    game.path(16).write_bytes(b"original-16")
    os.utime(game.path(16), (1780000000 + 16 * 60, 1780000000 + 16 * 60))
    game._queen_blood_runs.runs_path.unlink()
    file = {"slot": 16, "steam_id": "7656", "path": str(game.path(16))}
    game._queen_blood_runs.assign(DRIVER, file)
    candidate = game._queen_blood_runs.begin_load(file, process_identity(game._environment))
    game._queen_blood_runs.complete_load(candidate, process_identity(game._environment))
    game.order, game.row = [16, 8, 9, 13, 14], 16
    result = write(game)
    assert result["ok"] and result["saved"] and result["slot"] == 16
    assert game.writes == [16] and Path(result["backup"]).read_bytes() == b"original-16"
    assert game.path(8).read_bytes() == b"original-8"


def test_unvalidated_layout_has_no_input_even_in_dry_run(game, monkeypatch, tmp_path):
    monkeypatch.setenv("FF7R_SAVE_WRITE_LAYOUT", str(tmp_path / "missing-layout.json"))
    result = SaveSlotWriter(game).run(execute=False)
    assert result["decision"] == "save_write_layout_unvalidated" and not game.keys


def test_repository_layout_is_the_live_calibrated_default(monkeypatch):
    # Live 2026-10-08: Btn_00 is Yes (No preselected) and the prompt names no slot.
    from ff7r_agent.save_slot_save import DEFAULT_SAVE_WRITE_LAYOUT
    monkeypatch.delenv("FF7R_SAVE_WRITE_LAYOUT", raising=False)
    layout = SaveWriteLayout.configured()
    assert DEFAULT_SAVE_WRITE_LAYOUT.is_file()
    assert (layout.yes_node, layout.no_node, layout.prompt_slot_node) == ("Btn_00", "Btn_01", "")
    assert layout.yes_text == "$menu_save_load_0002_0001" and layout.row_selection["widget_name"] == "SaveData_ScrollBox"


def test_dry_run_never_opens_or_confirms_save_prompt(game):
    result = write(game, 9, execute=False)
    assert result["ok"] and result["closed"] and not game.path(9).exists() and not game.writes
    assert not any(p["why"].startswith("open_save_prompt") for p in result["presses"])


def test_missing_explicit_empty_proof_refuses_new_slot(game):
    game.empty_text = "Unknown row"
    assert write(game, 9)["decision"] == "save_empty_row_unverified"
    assert not game.writes


def test_dropped_yes_selection_cannot_confirm_no_or_retry_blindly(game):
    game.drop_up = True
    result = write(game)
    assert result["decision"] == "save_yes_not_verified" and not game.writes
    assert sum(p["why"] == "save_prompt:verified_yes" for p in result["presses"]) == 0


def test_external_file_change_between_prompt_and_yes_blocks_write(game):
    def change():
        if game.state == "prompt":
            game.path(8).write_bytes(b"changed-by-someone-else")
    game.before_read = change
    assert write(game)["decision"] == "save_target_changed" and not game.writes


def test_changed_process_before_confirmation_refuses_write(game):
    game.before_read = lambda: game._environment.tokens.update({42: "reused"}) if game.state == "prompt" else None
    assert write(game)["decision"] == "save_identity_changed" and not game.writes


def test_final_snapshot_cannot_hide_a_disk_change_after_the_last_precheck(game):
    game.before_read = lambda: game.path(8).write_bytes(b"changed-on-final-read") if game.state == "prompt" and game.yes else None
    assert write(game)["decision"] == "save_target_changed" and not game.writes


@pytest.mark.parametrize("defect", ["load_list", "duplicate_row", "no_row_focus", "unknown_prompt_slot", "both_buttons_focused", "missing_dialog"])
def test_ambiguous_ui_never_reaches_irreversible_confirmation(game, defect):
    snapshot = game.snapshot
    def corrupt():
        response = snapshot()
        if game.state == "save_list":
            if defect == "load_list":
                response["texts"] = [{"text": "$help_message_0008_0005"}]
            if defect == "duplicate_row":
                response["save_cells"].append(response["save_cells"][0])
            if defect == "no_row_focus":
                for cell in response["save_cells"]:
                    cell["nodes"][1]["keyboard_focus"] = False
        if game.state == "prompt":
            if defect == "unknown_prompt_slot":
                response["save_dialogs"][0]["nodes"][0]["text"] = "13"
            if defect == "both_buttons_focused":
                for node in response["save_dialogs"][0]["nodes"][1:]:
                    node["keyboard_focus"] = True
            if defect == "missing_dialog":
                response.pop("save_dialogs")
        return response
    game.snapshot = corrupt
    result = write(game)
    assert not result["ok"] and not game.writes
    assert not any(p["why"] == "save_prompt:verified_yes" for p in result["presses"])


def test_process_loss_after_yes_leaves_uncertain_save_lock(game):
    press = game._map_menu_key
    def crash(button, milliseconds):
        final = game.state == "prompt" and game.yes and button == "confirm"
        press(button, milliseconds)
        if final:
            game._environment.ids = {}
    game._map_menu_key = crash
    result = write(game)
    assert result["decision"] == "game_exited_or_changed_during_save" and result["requires_review"]
    assert game._queen_blood_runs.root.joinpath("save-operation-lock.json").exists()


def test_another_manual_file_change_cannot_be_reported_as_success(game):
    press = game._map_menu_key
    def wrong_write(button, milliseconds):
        final = game.state == "prompt" and game.yes and button == "confirm"
        press(button, milliseconds)
        if final:
            game.path(13).write_bytes(b"unexpected-foreign-write")
    game._map_menu_key = wrong_write
    result = write(game)
    assert result["decision"] == "another_manual_save_changed" and result["requires_review"]
    assert not result["ok"] and not result["saved"]


def test_unverified_write_retains_lock_and_prevents_later_allocation(game):
    game.write_enabled = False
    result = write(game)
    assert result["state"] == "save_uncertain" and result["requires_review"]
    assert (game._queen_blood_runs.root / "save-operation-lock.json").exists()
    assert not write(game, 9)["ok"] and not game.writes


def test_new_model_can_allocate_empty_slot_from_verified_fresh(game):
    game._queen_blood_runs.runs_path.unlink()
    file = {"slot": 14, "steam_id": "7656", "path": str(game.path(14))}
    candidate = game._queen_blood_runs.begin_load(file, process_identity(game._environment))
    game._queen_blood_runs.complete_load(candidate, process_identity(game._environment))
    assert write(game, 9)["ok"]
    assert game.path(14).read_bytes() == b"original-14"


def fake_startup(game, *, reload_ok=True, close_ok=True, wrong_slot=False):
    calls = []
    def close(**kwargs):
        calls.append(("close", kwargs))
        return {"ok": close_ok}
    def start(**kwargs):
        calls.append(("start", kwargs))
        if reload_ok:
            game._environment.ids, game._environment.tokens = {43: "ff7rebirth_.exe"}, {43: "new-43"}
            file = {"slot": 13 if wrong_slot else 8, "steam_id": "7656", "path": str(game.path(13 if wrong_slot else 8))}
            candidate = game._queen_blood_runs.begin_load(file, process_identity(game._environment))
            game._queen_blood_runs.complete_load(candidate, process_identity(game._environment))
        return {"ok": reload_ok}
    return SimpleNamespace(service=game, lifecycle=SimpleNamespace(close_game=close),
                           _session_close=lambda **kwargs: {"closed": True}, start_and_load_save_slot=start,
                           _monotonic=game._monotonic, _sleep=game._sleeper), calls


def test_recovery_requires_process_loss_and_can_run_only_once(game):
    armed = recovery.arm(game)
    startup, calls = fake_startup(game)
    assert recovery.recover(startup, recovery_id=armed["recovery_id"])["decision"] == "recovery_requires_observed_process_loss"
    assert not calls
    game._environment.ids = {}
    result = recovery.recover(startup, recovery_id=armed["recovery_id"])
    assert result["ok"] and result["state"] == "recovered" and not result["actions_replayed"]
    assert calls == [("close", {"timeout_seconds": 30.0, "force": False}), ("start", {"model_run": True})]
    assert recovery.recover(startup, recovery_id=armed["recovery_id"])["decision"] == "recovery_not_armed_or_already_attempted"
    public = json.loads((game._public_stream_event_dir.parent / "public-working-save-recovery.json").read_text())
    assert set(public) == {"version", "recoveryId", "phase", "at"} and public["phase"] == "ready"


def test_working_save_recovery_releases_model_startup_lease(game, monkeypatch, tmp_path):
    from ff7r_agent.safety import SessionLease
    from ff7r_agent.startup import StartupCoordinator

    game.driver.update(effort="xhigh", run_id="recovery-test-run")
    armed = recovery.arm(game)
    game._environment.ids = {}
    fake, calls = fake_startup(game)
    lease = SessionLease(runtime_dir=tmp_path / "lease", clock=game._monotonic)
    agent = StartupCoordinator(fake.lifecycle, game, session_status=lease.status,
                               session_open=lease.open, session_heartbeat=lease.heartbeat,
                               session_close=lease.close, sleeper=game._sleeper,
                               monotonic=game._monotonic)
    phases = []
    fake.lifecycle.mark_startup_phase = phases.append
    file = {"slot": 8, "steam_id": "7656", "path": str(game.path(8))}
    monkeypatch.setattr(save_slot_load, "resolve_save_slot", lambda *args, **kwargs: {"slot": 8, "file": file})

    def start(**kwargs):
        game._environment.ids, game._environment.tokens = {43: "ff7rebirth_.exe"}, {43: "new-43"}
        return {"ok": True, "phase": "main_menu", "session": agent._ensure_session(43, 1800)}

    def load(**kwargs):
        assert kwargs == {"slot": 8, "execute": True, "steam_id": "7656"}
        assert lease.status()["active"] and lease.status()["client_id"] == "model-startup:recovery-test-run"
        candidate = game._queen_blood_runs.begin_load(file, process_identity(game._environment))
        game._queen_blood_runs.complete_load(candidate, process_identity(game._environment))
        return {"ok": True, "phase": "gameplay_ready"}

    def title_load(service, *, before_input, **kwargs):
        assert service is game
        before_input()
        return load(**kwargs)

    agent.start_game_to_main_menu = start
    monkeypatch.setattr(save_slot_load, "load_title_save_slot", title_load)
    game.load_save_slot = lambda **kwargs: pytest.fail("recovery must load from the title")
    result = recovery.recover(agent, recovery_id=armed["recovery_id"])

    assert result["ok"], result.get("decision")
    assert result["state"] == "recovered" and not result["actions_replayed"]
    assert result["startup"]["control_session_release"]["closed"] and lease.status() is None
    assert result["startup"]["load_path"] == "title"
    assert phases == ["gameplay_ready"]
    assert game.get_queens_blood_run_status()["loaded_save_verified"]
    assert game.path(8).read_bytes() == b"original-8" and not game.writes
    assert lease.open(game_pid=43, client_id="next-pursuit")["active"]


@pytest.mark.parametrize("failure", ["close", "reload", "wrong_slot", "changed_file", "changed_driver", "save_lock"])
def test_recovery_failures_hold_brb_without_replaying_actions(game, failure):
    armed = recovery.arm(game)
    startup, calls = fake_startup(game, close_ok=failure != "close", reload_ok=failure != "reload", wrong_slot=failure == "wrong_slot")
    game._environment.ids = {}
    if failure == "changed_file":
        game.path(8).write_bytes(b"changed-after-arm")
    if failure == "changed_driver":
        game.driver = {"driver": "Claude", "model_id": "claude-test"}
    if failure == "save_lock":
        (game._queen_blood_runs.root / "save-operation-lock.json").write_text("{}")
    result = recovery.recover(startup, recovery_id=armed["recovery_id"])
    assert not result["ok"] and result["requires_review"] and not result["actions_replayed"]
    assert json.loads((game._public_stream_event_dir.parent / "public-working-save-recovery.json").read_text())["phase"] == "failed"


def test_recovery_rejects_pid_reuse_and_replacement_game_without_closing_it(game):
    armed = recovery.arm(game)
    startup, calls = fake_startup(game)
    game._environment.tokens[42] = "reused-token"
    assert not recovery.recover(startup, recovery_id=armed["recovery_id"])["ok"] and not calls
    game._environment.ids = {55: "ff7rebirth_.exe"}
    with pytest.raises(ValueError, match="replacement_game_present"):
        recovery.process_lost(game._environment, {"pid": "42", "token": "created-42"})


def test_intentional_exit_disarms_recovery(game):
    armed = recovery.arm(game)
    recovery.disarm(game)
    game._environment.ids = {}
    startup, calls = fake_startup(game)
    assert recovery.recover(startup, recovery_id=armed["recovery_id"])["decision"] == "recovery_not_armed_or_already_attempted"
    assert not calls


def test_recovery_will_not_restart_until_obs_confirms_brb(game):
    armed = recovery.arm(game)
    game.cover_enabled = False
    game._environment.ids = {}
    startup, calls = fake_startup(game)
    result = recovery.recover(startup, recovery_id=armed["recovery_id"])
    assert result["decision"] == "recovery_stream_cover_unverified" and not calls


def test_failed_process_enumeration_is_never_crash_evidence(game):
    def unavailable():
        raise OSError("probe failed")
    game._environment.game_process_ids_verified = unavailable
    with pytest.raises(OSError):
        recovery.process_lost(game._environment, {"pid": "42", "token": "created-42"})


def test_save_as_refreshes_armed_recovery_and_preserves_run_ledger(game):
    from ff7r_agent.service import AgentService
    armed = recovery.arm(game)
    # The real service wrapper is exercised with our fake UI and files.
    game.arm_working_save_recovery = lambda: recovery.arm(game)
    # Saving the run's slot is only allowed during a live stream.
    game._queen_blood_record_mode = lambda: "live"
    game.queen_blood_profile = lambda: None
    from ff7r_agent import save_slot_save
    original = save_slot_save.SaveSlotWriter
    save_slot_save.SaveSlotWriter = lambda service: original(service, write_layout=LAYOUT)
    try:
        result = AgentService.save_game_progress(game, slot=9, execute=True)
    finally:
        save_slot_save.SaveSlotWriter = original
    assert result["ok"] and result["recovery_target"]["recovery_id"] == armed["recovery_id"]
    record = json.loads(recovery.receipt_path(game).read_text())
    assert record["file"]["slot"] == 9 and record["assignment"]["profile_slot"] == 8


@pytest.mark.parametrize("phase", ["recovering", "failed"])
def test_save_refresh_cannot_clear_cover_published_by_the_loss_observer(game, phase):
    armed = recovery.arm(game)
    recovery.publish(game, armed["recovery_id"], phase)
    assert write(game, 9)["saved"]
    refreshed = recovery.arm(game)
    assert refreshed["recovery_id"] == armed["recovery_id"]
    assert json.loads(recovery.receipt_path(game).read_text())["file"]["slot"] == 9
    public = json.loads((game._public_stream_event_dir.parent / "public-working-save-recovery.json").read_text())
    assert public["phase"] == phase


def test_row_confirmation_can_write_without_a_prompt_and_retains_uncertainty(game):
    press = game._map_menu_key

    def direct_save(button, milliseconds):
        if game.state == "save_list" and button == "confirm":
            game.path(game.row).write_bytes(b"immediate-save-without-confirmation-dialog")
            game.writes.append(game.row)
            return
        press(button, milliseconds)

    game._map_menu_key = direct_save
    result = write(game, 9)
    assert result["state"] == "save_uncertain" and result["requires_review"]
    assert not result["ok"] and not result["saved"]
    assert game.writes == [9]
    lock = json.loads((game._queen_blood_runs.root / "save-operation-lock.json").read_text())
    assert lock["phase"] == "opening_save_prompt" and lock["retain"] is True
    assert game._queen_blood_runs.assignment(DRIVER)["slot"] == 8
    assert not write(game, 9)["ok"]


def test_last_row_snapshot_cannot_hide_a_change_before_row_confirmation(game):
    reads = [0]

    def change_on_final_row_read():
        if game.state == "save_list":
            reads[0] += 1
            if reads[0] == 3:  # open list, verify selected row, before_save_prompt
                game.path(8).write_bytes(b"changed-after-last-row-precheck")

    game.before_read = change_on_final_row_read
    result = write(game)
    assert result["decision"] == "save_target_changed" and not game.writes
    assert not any(p["why"] == "open_save_prompt" for p in result["presses"])


def test_occupied_only_layout_does_not_require_fabricated_empty_evidence(game, tmp_path, monkeypatch):
    layout = tmp_path / "occupied-only-layout.json"
    layout.write_text(json.dumps({"slot_node": "Slot", "row_button_node": "RowButton",
        "prompt_slot_node": "PromptSlot", "yes_node": "Yes", "no_node": "No"}))
    monkeypatch.setenv("FF7R_SAVE_WRITE_LAYOUT", str(layout))
    configured = SaveWriteLayout.configured()
    assert configured.empty_node is None and not configured.empty_texts
    result = SaveSlotWriter(game, write_layout=configured).run(execute=True)
    assert result["saved"] and game.writes == [8]
    refused = SaveSlotWriter(game, write_layout=configured).run(slot=9, execute=True)
    assert refused["decision"] == "save_empty_row_contract_unvalidated" and game.writes == [8]
    assert not any(p["why"] == "open_save_prompt" for p in refused["presses"])


@pytest.mark.parametrize("empty", [{"empty_node": "Empty"}, {"empty_texts": ["Empty slot"]},
                                    {"empty_node": "Empty", "empty_texts": []}])
def test_partial_empty_contract_is_rejected_before_input(game, tmp_path, monkeypatch, empty):
    layout = tmp_path / "partial-layout.json"
    layout.write_text(json.dumps({"slot_node": "Slot", "row_button_node": "RowButton",
        "prompt_slot_node": "PromptSlot", "yes_node": "Yes", "no_node": "No", **empty}))
    monkeypatch.setenv("FF7R_SAVE_WRITE_LAYOUT", str(layout))
    result = SaveSlotWriter(game).run(execute=True)
    assert result["decision"] == "save_write_layout_invalid" and not game.keys


CALIBRATION = {"slot_node": "Slot", "row_button_node": "RowButton"}


def calibration_game(game):
    game._queen_blood_runs = QueenBloodRuns(game.directory.parent / "calibration-profiles")
    game.path(15).write_bytes(b"owned-15")
    os.utime(game.path(15), (1780000900, 1780000900))
    file = {"slot": 15, "steam_id": "7656", "path": str(game.path(15))}
    game._queen_blood_runs.assign(DRIVER, file)
    candidate = game._queen_blood_runs.begin_load(file, process_identity(game._environment))
    game._queen_blood_runs.complete_load(candidate, process_identity(game._environment))
    game.order, game.row = [15, 14, 8], 15
    game.capture_game_window = lambda: {"ok": True, "path": "offline-fixture.png"}
    return game


def test_calibration_prompt_has_one_row_confirm_and_retained_lock(game):
    calibration_game(game)
    result = SaveSlotWriter(game).run(15, True, calibrate_prompt=CALIBRATION)
    assert result["state"] == "save_calibration_prompt_observed"
    assert result["requires_review"] and not result["saved"]
    assert result["raw_save_evidence"]["save_dialogs"] and result["capture"]["ok"]
    assert game.state == "prompt" and not game.yes and not game.writes
    assert game.keys == ["menu", "save_shortcut", "confirm"]
    lock = json.loads((game._queen_blood_runs.root / "save-operation-lock.json").read_text())
    assert lock["retain"] and lock["slot"] == 15
    assert Path(result["backup"]).read_bytes() == b"owned-15"


def test_calibration_preflight_does_not_confirm_row(game):
    calibration_game(game)
    result = SaveSlotWriter(game).run(15, False, calibrate_prompt=CALIBRATION)
    assert result["ok"] and result["state"] == "dry_run_highlighted"
    assert not game.writes and not any(p.get("stage") == "open_save_prompt" for p in result["presses"])


@pytest.mark.parametrize("slot", [0, 8, 14, 9])
def test_calibration_refuses_any_other_slot_before_input(game, slot):
    calibration_game(game)
    result = SaveSlotWriter(game).run(slot, True, calibrate_prompt=CALIBRATION)
    assert not result["ok"] and not game.keys


def test_calibration_immediate_write_verified_without_more_input(game):
    calibration_game(game)
    original = game._map_menu_key
    def key(button, milliseconds):
        if game.state == "save_list" and button == "confirm":
            lock = json.loads((game._queen_blood_runs.root / "save-operation-lock.json").read_text())
            assert lock["retain"] and Path(lock["backup"]).read_bytes() == b"owned-15"
            game.keys.append(button)
            game.path(15).write_bytes(b"immediate-save")
            os.utime(game.path(15), (1790000000, 1790000000))
            game.writes.append(15)
        else:
            original(button, milliseconds)
    game._map_menu_key = key
    result = SaveSlotWriter(game).run(15, True, calibrate_prompt=CALIBRATION)
    assert result["saved"] and result["receipt"] and not result["requires_review"]
    assert game.keys == ["menu", "save_shortcut", "confirm"]
    assert game.state == "save_list" and game.writes == [15]


@pytest.mark.parametrize("contract", [{}, {"slot_node": "Slot"},
    {"slot_node": "Slot", "row_button_node": "Slot"},
    {"slot_node": "Slot", "row_button_node": "RowButton", "yes_node": "Yes"}])
def test_calibration_bad_contract_refused_without_input(game, contract):
    calibration_game(game)
    result = SaveSlotWriter(game).run(15, True, calibrate_prompt=contract)
    assert result["decision"] == "save_calibration_row_contract_invalid" and not game.keys


def test_calibration_no_prompt_or_write_retains_lock_without_retry(game):
    calibration_game(game)
    original = game._map_menu_key
    def key(button, milliseconds):
        if game.state == "save_list" and button == "confirm":
            game.keys.append(button)
        else:
            original(button, milliseconds)
    game._map_menu_key = key
    result = SaveSlotWriter(game).run(15, True, calibrate_prompt=CALIBRATION)
    assert result["decision"] == "save_write_unverified" and result["requires_review"]
    assert result["state"] == "save_uncertain" and game.path(15).read_bytes() == b"owned-15"
    assert game.keys == ["menu", "save_shortcut", "confirm"]
    assert (game._queen_blood_runs.root / "save-operation-lock.json").exists()


def test_calibration_capture_failure_retains_lock_and_does_not_press_yes(game):
    calibration_game(game)
    game.capture_game_window = lambda: {"ok": False}
    result = SaveSlotWriter(game).run(15, True, calibrate_prompt=CALIBRATION)
    assert result["decision"] == "save_calibration_capture_failed" and result["requires_review"]
    assert result["raw_save_menu"]["save_dialogs"] and not game.writes
    assert game.keys == ["menu", "save_shortcut", "confirm"]


@pytest.mark.parametrize("slot", [8, 9])
def test_verified_save_preserves_campaign_and_refreshes_speech_binding(game, slot):
    from ff7r_agent.queen_blood_campaign import QueenBloodCampaign
    from ff7r_agent.queen_blood_speech_receipt import binding, publish, consume_locked
    game.driver["run_id"] = "declared-test-run"
    campaign = QueenBloodCampaign(game._queen_blood_runs.root)
    before = game.get_queens_blood_run_status()
    assert campaign.record_blocked(before["assignment"], "Ned", {"reason": "preserved-test-evidence"})
    prior = campaign.status(before["assignment"])
    context = binding(before, "live")
    assert context is not None
    publish(game._public_stream_event_dir, "I'll hold the lead in this lane.", "calm",
            {"ok": True, "queued_at": "2026-10-08T00:00:00Z", "rehearsal": False}, context, 100.0)
    result = write(game, slot)
    assert result["saved"]
    after = game.get_queens_blood_run_status()
    fresh = binding(after, "live")
    assert fresh is not None
    assert fresh["loaded_save"]["load_id"] != context["loaded_save"]["load_id"]
    assert consume_locked(game._public_stream_event_dir, fresh, now=101.0) is None
    current = campaign.status(after["assignment"])
    assert current["progress"] == prior["progress"]
    assert current["opponents"] == prior["opponents"]
    assert current["run"]["slot"] == 8


@pytest.mark.parametrize("anchor", [14, True, 7, "8"])
def test_campaign_rejects_unowned_or_invalid_profile_anchor(game, anchor):
    from ff7r_agent.queen_blood_campaign import QueenBloodCampaign
    assigned = game.get_queens_blood_run_status()["assignment"]
    campaign = QueenBloodCampaign(game._queen_blood_runs.root)
    with pytest.raises(ValueError, match="profile slot"):
        campaign.status({**assigned, "profile_slot": anchor})


@pytest.mark.parametrize("mode", ["rehearsal", "test"])
def test_saving_the_run_slot_requires_a_live_stream(game, mode):
    # Owner decision 2026-10-08: rehearsals and development never overwrite
    # the run's save slot; only a live stream may write it.
    from ff7r_agent.service import AgentService
    game._queen_blood_record_mode = lambda: mode
    before = {slot: game.path(slot).read_bytes() for slot in (8, 9) if game.path(slot).exists()}
    result = AgentService.save_game_progress(game, slot=8, execute=True)
    assert result["ok"] is False and result["decision"] == "save_requires_live_stream"
    assert result["input_sent"] is False and result["presses"] == []
    assert {slot: game.path(slot).read_bytes() for slot in before} == before


def test_record_mode_is_live_only_while_obs_streams(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ff7r_agent import service as service_module
    fake = SimpleNamespace(_public_stream_event_dir=tmp_path / "events")
    mode = lambda: service_module.AgentService._queen_blood_record_mode(fake)
    for live, rehearsal, expected in ((True, False, "live"), (False, True, "rehearsal"),
                                      (False, False, "test"), (True, True, "live")):
        monkeypatch.setattr(service_module, "stream_is_live", lambda _directory, value=live: value)
        monkeypatch.setattr(service_module, "rehearsal_is_live", lambda _directory, value=rehearsal: value)
        assert mode() == expected
    fake._public_stream_event_dir = None
    assert mode() == "test"



LIVE_PROMPT_LAYOUT = SaveWriteLayout("Slot", None, (), "RowButton", "", "Btn_00", "Btn_01",
                                     yes_text="$menu_save_load_0002_0001", no_text="$menu_save_load_0002_0002")


def _live_prompt(selected, *, yes_text="$menu_save_load_0002_0001", extra=()):
    """The shape captured live on 2026-10-08: no slot text, no keyboard
    focus, and the selected button's inner EndButton reporting hovered."""
    root = "Com_Window_01_C /Temp/Untitled_1.MenuRenderingWorld:Com_Window_01_C_1.WidgetTree"
    def node(name, path, **kwargs):
        return {"name": name, "full_id": f"X {root.split(' ', 1)[1]}.{path}", "visible": True, **kwargs}
    nodes = [node("Txt", "Txt", text="Save your progress to this slot?")]
    for button, text in (("Btn_00", yes_text), ("Btn_01", "$menu_save_load_0002_0002")):
        nodes += [node(button, button), node("Button_00", f"{button}.WidgetTree.Button_00",
                                             keyboard_focus=False, hovered=button == selected),
                  node("Txt", f"{button}.WidgetTree.Txt", text=text)]
    nodes += list(extra)
    return SimpleNamespace(response={"save_dialogs": [{"nodes": nodes}]}, text_cap_reached=False,
                           save_prompt_shown=True, load_prompt_shown=False, signals=["save_prompt"])


@pytest.mark.parametrize("selected,yes_selected", [("Btn_00", True), ("Btn_01", False)])
def test_live_prompt_selection_comes_from_the_hovered_inner_button(game, selected, yes_selected):
    writer = SaveSlotWriter(game, write_layout=LIVE_PROMPT_LAYOUT)
    writer.target = 8
    yes, no = writer._dialog(_live_prompt(selected))
    assert yes["keyboard_focus"] is yes_selected and no["keyboard_focus"] is not yes_selected


def test_live_prompt_refuses_ambiguous_or_relabelled_buttons(game):
    from ff7r_agent.save_slot_load import SaveSlotLoadError
    writer = SaveSlotWriter(game, write_layout=LIVE_PROMPT_LAYOUT)
    writer.target = 8
    stray = {"name": "Other", "full_id": "X /Temp/W:Com_Window_01_C_1.WidgetTree.Other", "visible": True, "hovered": True}
    with pytest.raises(SaveSlotLoadError, match="save_confirmation_selection_unverified"):
        writer._dialog(_live_prompt("Btn_00", extra=[stray]))
    with pytest.raises(SaveSlotLoadError, match="save_confirmation_button_text_changed"):
        writer._dialog(_live_prompt("Btn_00", yes_text="$menu_save_load_0002_0002"))
    no_question = _live_prompt("Btn_00")
    no_question.response["save_dialogs"][0]["nodes"][0]["text"] = "Load this save?"
    with pytest.raises(SaveSlotLoadError, match="save_prompt_unverified"):
        writer._dialog(no_question)


def test_designated_test_slot_is_written_without_changing_the_run(game, monkeypatch):
    monkeypatch.setenv("FF7R_TEST_SAVE_SLOT", "13")
    before_run = game._queen_blood_runs.assignment(DRIVER)
    protected = {n: game.path(n).read_bytes() for n in (0, 8, 14)}
    result = SaveSlotWriter(game, write_layout=LAYOUT).run(13, True, test_save=True)
    assert result["ok"] and result["saved"] and result["test_save"]
    assert result["receipt"] == {"test_save": True, "slot": 13}
    assert game.writes == [13] and Path(result["backup"]).read_bytes() == b"original-13"
    assert game._queen_blood_runs.assignment(DRIVER) == before_run
    assert {n: game.path(n).read_bytes() for n in protected} == protected


@pytest.mark.parametrize("env,slot,decision", [
    (None, 13, "save_test_slot_not_designated"),
    ("12", 13, "save_test_slot_not_designated"),
    ("14", 14, "save_test_slot_not_designated"),
    ("8", 8, "save_test_slot_owned_by_a_model"),
])
def test_test_saves_need_the_designated_unowned_slot(game, monkeypatch, env, slot, decision):
    if env is None:
        monkeypatch.delenv("FF7R_TEST_SAVE_SLOT", raising=False)
    else:
        monkeypatch.setenv("FF7R_TEST_SAVE_SLOT", env)
    result = SaveSlotWriter(game, write_layout=LAYOUT).run(slot, True, test_save=True)
    assert result["ok"] is False and result["decision"] == decision and game.writes == []


def test_prompt_probe_selects_yes_then_cancels_without_writing(game):
    game.capture_game_window = lambda: {"ok": True, "image_path": "probe.bmp"}
    before = {n: game.path(n).read_bytes() for n in (0, 8, 13, 14)}
    result = SaveSlotWriter(game, write_layout=LAYOUT).run(8, True, probe_prompt=True)
    assert result["ok"] and result["state"] == "prompt_probed" and result["saved"] is False
    assert result["capture"]["ok"] and result["closed"]
    assert "cancel" in game.keys and game.writes == []
    assert {n: game.path(n).read_bytes() for n in before} == before
    assert not (game._queen_blood_runs.root / "save-operation-lock.json").exists()


def test_test_saves_are_refused_on_a_live_stream(game, monkeypatch):
    from ff7r_agent.service import AgentService
    monkeypatch.setenv("FF7R_TEST_SAVE_SLOT", "13")
    game._queen_blood_record_mode = lambda: "live"
    result = AgentService.save_game_progress(game, slot=13, execute=True, test_save=True)
    assert result["decision"] == "save_test_slot_not_on_live_stream" and result["input_sent"] is False
    assert game.writes == []


def test_designated_story_slot_is_written_without_changing_the_run(game, monkeypatch):
    monkeypatch.setenv("FF7R_STORY_SAVE_SLOT", "13")
    before_run = game._queen_blood_runs.assignment(DRIVER)
    protected = {n: game.path(n).read_bytes() for n in (0, 8, 14)}
    result = SaveSlotWriter(game, write_layout=LAYOUT).run(13, True, story_save=True)
    assert result["ok"] and result["saved"] and result["story_save"]
    assert result["receipt"] == {"story_save": True, "slot": 13}
    assert game.writes == [13] and Path(result["backup"]).read_bytes() == b"original-13"
    assert game._queen_blood_runs.assignment(DRIVER) == before_run
    assert {n: game.path(n).read_bytes() for n in protected} == protected


@pytest.mark.parametrize("env,slot,kwargs,decision", [
    (None, 13, {}, "save_story_slot_not_designated"),
    ("12", 13, {}, "save_story_slot_not_designated"),
    ("14", 14, {}, "save_story_slot_not_designated"),
    ("8", 8, {}, "save_story_slot_owned_by_a_model"),
    ("13", 13, {"test_save": True}, "save_test_and_story_are_exclusive"),
])
def test_story_saves_need_the_designated_unowned_slot(game, monkeypatch, env, slot, kwargs, decision):
    monkeypatch.setenv("FF7R_TEST_SAVE_SLOT", "13")
    if env is None:
        monkeypatch.delenv("FF7R_STORY_SAVE_SLOT", raising=False)
    else:
        monkeypatch.setenv("FF7R_STORY_SAVE_SLOT", env)
    result = SaveSlotWriter(game, write_layout=LAYOUT).run(slot, True, story_save=True, **kwargs)
    assert result["ok"] is False and result["decision"] == decision and game.writes == []


@pytest.mark.parametrize("mode", ["offline", "rehearsal", "live"])
def test_story_saves_are_not_held_to_the_live_stream_rule(game, monkeypatch, mode):
    from ff7r_agent import save_slot_save
    from ff7r_agent.service import AgentService
    calls = []

    class Writer:
        def __init__(self, service):
            pass

        def run(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True, "saved": True, "story_save": True, "receipt": {"story_save": True, "slot": 17}}

    monkeypatch.setattr(save_slot_save, "SaveSlotWriter", Writer)
    game._queen_blood_record_mode = lambda: mode
    result = AgentService.save_game_progress(game, slot=17, execute=True, story_save=True)
    assert result["saved"] and calls and calls[0]["story_save"] is True


def test_story_saves_need_no_declared_driver(game, monkeypatch):
    # Live 2026-10-20: the offline Story Playthrough has a verified loaded
    # slot but no driver, since drivers are declared on streams only.
    monkeypatch.setenv("FF7R_STORY_SAVE_SLOT", "13")
    status = game.get_queens_blood_run_status
    game.get_queens_blood_run_status = lambda: {**status(), "driver": None}
    result = SaveSlotWriter(game, write_layout=LAYOUT).run(13, True, story_save=True)
    assert result["ok"] and result["saved"] and result["receipt"] == {"story_save": True, "slot": 13}
    refused = SaveSlotWriter(game, write_layout=LAYOUT).run(8, True)
    assert refused["decision"] == "save_requires_verified_loaded_slot_and_driver"
