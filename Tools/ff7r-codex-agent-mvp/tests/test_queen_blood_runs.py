from __future__ import annotations

import pytest

from ff7r_agent.queen_blood_profile import QueenBloodProfile
from ff7r_agent.queen_blood_runs import QueenBloodRuns, profile_id, process_identity
from ff7r_agent.service import AgentService
from test_agent import FakeRealInput


DRIVER = {"driver": "ChatGPT", "model_id": "gpt-6.1-sol", "effort": "xhigh",
          "run_id": "00000000-0000-0000-0000-000000000001"}
PROCESS = {"pid": "42", "token": "creation-1"}


def save(tmp_path, slot=13):
    path = tmp_path / "76561198000000000" / f"ff7rebirth{slot:03}.sav"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"saved world")
    return {"slot": slot, "steam_id": path.parent.name, "path": str(path)}


def loaded(store, file):
    assert store.complete_load(store.begin_load(file, PROCESS), PROCESS)["verified"]


def test_receipt_survives_server_restart_but_not_game_restart_or_pid_reuse(tmp_path):
    store = QueenBloodRuns(tmp_path / "profiles-root")
    file = save(tmp_path)
    loaded(store, file)
    restarted = QueenBloodRuns(store.root)
    assert restarted.loaded(PROCESS)["slot"] == 13
    assert restarted.loaded({"pid": "42", "token": "creation-2"}) is None
    assert restarted.loaded({"pid": "43", "token": "creation-1"}) is None
    assert restarted.loaded(None) is None


def test_failed_or_in_progress_load_cannot_reuse_the_old_inventory(tmp_path):
    store = QueenBloodRuns(tmp_path / "profiles-root")
    file = save(tmp_path)
    loaded(store, file)
    candidate = store.begin_load(save(tmp_path, 12), PROCESS)
    assert store.loaded(PROCESS) is None
    assert not store.complete_load(candidate, {"pid": "99", "token": "new"})["verified"]
    assert store.loaded(PROCESS) is None


def test_file_changed_during_load_is_unknown_but_later_saves_preserve_the_receipt(tmp_path):
    store = QueenBloodRuns(tmp_path / "profiles-root")
    file = save(tmp_path)
    candidate = store.begin_load(file, PROCESS)
    from pathlib import Path
    Path(file["path"]).write_bytes(b"replaced save")
    assert not store.complete_load(candidate, PROCESS)["verified"]
    loaded(store, file)
    Path(file["path"]).write_bytes(b"later progress")
    assert store.loaded(PROCESS)["slot"] == 13


def test_profiles_follow_both_the_loaded_slot_and_model_and_efforts_share(tmp_path):
    store = QueenBloodRuns(tmp_path / "profiles-root")
    file = save(tmp_path)
    loaded(store, file)
    first = store.status(DRIVER, PROCESS)["profile_id"]
    assert first == store.status({**DRIVER, "effort": "low"}, PROCESS)["profile_id"]
    assert first != store.status({**DRIVER, "model_id": "gpt-6-sol"}, PROCESS)["profile_id"]
    assert first != store.status({**DRIVER, "driver": "Claude"}, PROCESS)["profile_id"]
    loaded(store, save(tmp_path, 12))
    assert first != store.status(DRIVER, PROCESS)["profile_id"]


def test_one_assignment_per_model_and_no_two_models_share_a_slot(tmp_path):
    store = QueenBloodRuns(tmp_path / "profiles-root")
    file = save(tmp_path)
    record = store.assign(DRIVER, file)
    assert store.assign({**DRIVER, "effort": "low"}, file) == record
    with pytest.raises(ValueError, match="another model"):
        store.assign({**DRIVER, "model_id": "gpt-6-sol"}, file)
    with pytest.raises(ValueError, match="silently replaced"):
        store.assign(DRIVER, save(tmp_path, 12))
    loaded(store, save(tmp_path, 11))
    status = store.status(DRIVER, PROCESS)
    assert status["profile_id"] is None and not status["run_loaded"]
    assert status["loaded_save"]["slot"] == 11 and status["assignment"]["slot"] == 13


def test_new_slot_15_can_be_assigned_without_modifying_any_save(tmp_path):
    from pathlib import Path
    store = QueenBloodRuns(tmp_path / "profiles-root")
    file = save(tmp_path, 15)
    original = Path(file["path"]).read_bytes()
    assert store.assign(DRIVER, file)["slot"] == 15
    assert Path(file["path"]).read_bytes() == original


@pytest.mark.parametrize("slot", [0, 14])
def test_autosave_and_fresh_are_never_model_working_slots(tmp_path, slot):
    with pytest.raises(ValueError, match="reserved"):
        QueenBloodRuns(tmp_path / "profiles-root").assign(DRIVER, save(tmp_path, slot))


class Processes:
    token = "creation-1"
    def game_process_ids(self):
        return {42: "ff7rebirth_.exe"}
    def game_process_identity_tokens(self, pids):
        return {42: self.token} if self.token else {}


def test_process_creation_token_is_required():
    environment = Processes()
    assert process_identity(environment) == PROCESS
    environment.token = None
    assert process_identity(environment) is None


def test_process_identity_ignores_launcher_beside_the_actual_game():
    environment = Processes()
    environment.game_process_ids = lambda: {41: "ff7rebirth.exe", 42: "ff7rebirth_.exe"}
    assert process_identity(environment) == PROCESS
    environment.game_process_ids = lambda: {41: "ff7rebirth_.exe", 42: "ff7rebirth_.exe"}
    assert process_identity(environment) is None


def test_service_ignores_wrong_env_inventory_and_preserves_legacy_files(tmp_path, monkeypatch):
    import ff7r_agent.service as service_module
    monkeypatch.setenv("FF7R_QUEEN_BLOOD_PROFILE", "queens_blood_campaign")
    monkeypatch.setattr(service_module, "match_record_target", lambda root: (DRIVER, "rehearsal", "history"))
    root = tmp_path / "profiles-root"
    legacy = QueenBloodProfile(root, "queens_blood_campaign")
    legacy.record_collection(["card_id_117"], source="wrong save")
    original = legacy.inventory_path.read_bytes()
    environment = Processes()
    service = AgentService(None, FakeRealInput(), environment=environment,
                           queen_blood_profile_root=root, public_stream_event_dir=tmp_path / "stream" / "events")
    assert service.queen_blood_profile() is None
    file = save(tmp_path)
    candidate = service.begin_save_load(file)
    assert service.complete_save_load(candidate)["verified"]
    profile = service.queen_blood_profile()
    assert profile.profile_id == profile_id(DRIVER, file) and profile.inventory()["cards"] == {}
    profile.record_collection(["card_id_000"], source="loaded save")
    assert legacy.inventory_path.read_bytes() == original
    # A server restart recovers the verified load and the same model profile.
    restarted = AgentService(None, FakeRealInput(), environment=environment,
                             queen_blood_profile_root=root, public_stream_event_dir=tmp_path / "stream" / "events")
    assert restarted.queen_blood_profile().owned_cards()[0]["card_id"] == "card_id_000"
    environment.token = "reused-pid"
    assert restarted.queen_blood_profile() is None


def test_corrupt_registry_is_not_overwritten(tmp_path):
    store = QueenBloodRuns(tmp_path / "profiles-root")
    store.root.mkdir()
    store.runs_path.write_text('{"version": 9, "runs": {}}', encoding="utf-8")
    before = store.runs_path.read_bytes()
    with pytest.raises(ValueError, match="registry is invalid"):
        store.assign(DRIVER, save(tmp_path))
    assert store.runs_path.read_bytes() == before
