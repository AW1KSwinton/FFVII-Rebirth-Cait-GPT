"""Offline #361 replay; no native capture or live input is used.

Source: https://example.invalid/private-source-reference (body).
Recorded: target/initial feet, 36.9 s ARRIVED, 0.843 m, Talk, -88.5 deg.
Synthetic: actor IDs, inventory/source metadata, all sampling/settling timing,
the arrival's direction (only its distance was published), and menu/ride.
The referenced raw legs were unavailable; these are not native fixtures.
"""
from __future__ import annotations

import asyncio
import copy
import math
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ff7r_agent import field_prompt, guide_moogle, npc_approach, shop
from ff7r_agent.navigation_values import MissionError
from ff7r_agent.service import AgentService, SafetyError


CANONICAL = "field_object:2350-GOLDS:oba2350_guideMoogle_battleSQ@893,-313"
TARGET = {"x": 89349.0, "y": -31292.0, "z": 46432.0}
START = {"x": 92423.0, "y": -33758.0, "z": 46436.0}
ROBOT = "BlueprintGeneratedClass /Game/NonPlayer/NP0140_00_GoldSaucerRobot_Standard" + guide_moogle.GUIDE_MOOGLE_CLASS


class Replay:
    def __init__(self):
        self.sequence, self.seconds = 10, 0.0
        self.feet, self.yaw = dict(START), -91.5
        self.events, self.lease = [], {"active": False}
        self.arrived, self.menu_open, self.zone = False, False, guide_moogle.zone("Welcome Area")
        self.cursor, self.before_use, self.gate_failure = 0, None, None
        self.gates, self.gate_fail_on = 0, 1
        self.mutate_scan, self.mutate_prompt = lambda _: None, lambda _: None

    def sample(self):
        self.sequence += 1
        position = {**self.feet, "z": self.feet["z"] + 88}
        return {"sequence": self.sequence, "source_sequence": self.sequence * 2,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(), "source": "file",
                "running": True, "map_id": "EndGame", "paused": False, "in_menu": False,
                "in_combat": False, "loading": False, "telemetry": {"live": True, "age_seconds": 0.01},
                "player": {"actor_id": "synthetic-cloud", "position": position,
                           "rotation": {"yaw": self.yaw}, "locomotion": {"feet_position": dict(self.feet),
                           "grounded": True, "velocity_cm_s": {"x": 0, "y": 0, "z": 0}}},
                "camera": {"rotation": {"yaw": 0}}}

    def scan(self):
        sample = self.sample()
        position = {**TARGET, "z": TARGET["z"] + 88}
        result = {"ok": True, "request_id": f"synthetic-{self.sequence}", "sample": sample,
                  "snapshot": {"source": "ue4ss_character_inventory", "complete": True,
                               "request_id": f"synthetic-{self.sequence}",
                               "source_sequence": sample["source_sequence"], "map_id": "EndGame",
                               "player_actor_id": "synthetic-cloud", "characters": [
                                   {"id": "synthetic-moogle", "class": ROBOT, "is_player": False,
                                    "position": position, "distance": math.hypot(
                                        self.feet["x"] - TARGET["x"], self.feet["y"] - TARGET["y"]) / 100},
                                   {"id": "synthetic-party", "is_player": True, "position": dict(self.feet)}]}}
        if self.arrived:
            self.mutate_scan(result)
        return result

    def prompt(self):
        sample = self.sample()
        result = {"talk": self.arrived, "commands": [field_prompt.TALK_KEY] if self.arrived else [],
                  "source_sequence": sample["source_sequence"], "player_position": sample["player"]["position"]}
        if self.arrived:
            self.mutate_prompt(result)
        return result

    async def sleep(self, seconds):
        self.seconds += seconds

    async def rpc(self, operation, arguments):
        self.events.append((operation, copy.deepcopy(arguments)))
        if operation == "observe_game":
            return self.sample()
        if operation == "read_field_prompt":
            return self.prompt()
        if operation == "scan_character_inventory":
            return self.scan()
        if operation == "resolve_world_target":
            candidates = [{"chara_spec_name": "NP0140_00_GoldSaucerRobot_Standard",
                           "display_names": ["Guide Moogle"],
                           "canonical_id": CANONICAL.split("@")[0],
                           "object_id": "oba2350_guideMoogle_battleSQ"},
                          {"chara_spec_name": "NP0140_00_GoldSaucerRobot_Standard",
                           "display_names": ["Guide Moogle"], "object_id": "oba2350_guideMoogle_station"}]
            return {"status": "ambiguous", "identity": {"candidates": candidates}}
        if operation == "plan_npc_approach":
            return {"library": {}, "approach": {"position": {**TARGET, "x": TARGET["x"] + 200}}}
        if operation == "get_control_session_status":
            return dict(self.lease)
        if operation == "open_control_session":
            self.lease = {"active": True, "client_id": arguments["client_id"]}
            return dict(self.lease)
        if operation in {"stop_all_input", "close_control_session"}:
            self.lease["active"] = False
            return {"ok": True}
        if operation == "use_guide_moogle":
            if self.before_use is not None:
                self.before_use()
            try:
                return AgentService.use_guide_moogle(self.service, **arguments)
            except SafetyError as exc:
                # Model the ordinary bridge refusal envelope, not a live RPC.
                return {"ok": False, "error": str(exc), "error_type": "SafetyError"}
        if operation in {"refresh_lifecycle_attachment", "get_runtime_identity", "get_bootstrap_verification",
                         "prewarm_navigation", "wait_for_settled_gameplay"}:
            return {"ok": True}
        # Old heading recovery is allowed only in the explicit pre-fix replay.
        if operation == "move_stick":
            return {"ok": True}
        raise AssertionError(f"unexpected RPC {operation}")

    def install(self, monkeypatch):
        replay = self

        class Route:
            def __init__(self, rpc, output, identity_check, config, client_id):
                assert config.pursuit and config.camera_follow_enabled

            async def run(self):
                replay.events.append(("synthetic_route", {}))
                replay.arrived = True
                replay.feet = {**TARGET, "x": TARGET["x"] + 84.3}
                replay.seconds += 36.9
                return {"ok": True, "state": "ARRIVED"}

        def gate():
            self.events.append(("input_gate", {}))
            self.gates += 1
            if self.gate_failure is not None and self.gates >= self.gate_fail_on:
                raise SafetyError(self.gate_failure)
            sample = self.sample()
            return SimpleNamespace(to_dict=lambda: sample, age_seconds=lambda: 0.01)

        def interact(milliseconds):
            self.events.append(("interaction_pulse", {"milliseconds": milliseconds}))
            self.menu_open = True

        def tap(button, milliseconds):
            self.events.append(("menu_pulse", {"button": button}))
            if button == "down":
                self.cursor += 1
            elif button == "up":
                self.cursor -= 1
            elif button == "confirm":
                self.zone = guide_moogle.ZONES[self.cursor]
                self.feet = dict(zip(("x", "y", "z"), self.zone.arrival))
                self.menu_open = False

        def texts(service, **_):
            if not self.menu_open:
                return {"texts": []}
            menu = "X.FastTravel_Menu.WidgetTree."
            return {"texts": [{"path": menu + "Txt_Title", "text": guide_moogle.MENU_TITLE_KEY},
                              {"path": menu + "Txt_LocaName", "text": guide_moogle.ZONES[self.cursor].key},
                              {"path": menu + "Txt_CurrentName", "text": guide_moogle.zone("Welcome Area").key}]
                             + [{"path": f"X{i}.FastTravel_Cell.WidgetTree.Txt_Name", "text": z.key}
                                for i, z in enumerate(guide_moogle.ZONES)]}

        self.service = SimpleNamespace(
            input=SimpleNamespace(is_real=True, use_interaction=interact, release_all=lambda: None),
            _action_lock=threading.Lock(), _assert_real_movement_allowed=gate,
            _ensure_game_foreground=lambda: self.events.append(("foreground_check", {})),
            _monotonic=lambda: self.seconds, _sleeper=lambda seconds: setattr(self, "seconds", self.seconds + seconds),
            scan_character_inventory=self.scan, observe=self.sample, tap=tap,
            _record_action=lambda **record: self.events.append(("record_action", record)))
        self.service.talk_to_npc = lambda **arguments: AgentService.talk_to_npc(self.service, **arguments)
        monkeypatch.setattr(npc_approach, "NavigationMission", Route)
        monkeypatch.setattr(field_prompt, "read_field_prompt", lambda _: self.prompt())
        monkeypatch.setattr(shop, "read_screen_texts", texts)

    def run(self, tmp_path, *, ride=True, identity_check=None):
        options = dict(output=tmp_path, identity_check=identity_check, pursuit=True,
                       clock=lambda: self.seconds, sleep=self.sleep,
                       model_id="offline-test-model", reasoning_effort="test", client_id="synthetic-caller")
        if ride:
            return asyncio.run(npc_approach.ride_guide_moogle(self.rpc, "Ghost Square", **options))
        return asyncio.run(npc_approach.approach_npc(self.rpc, "Guide Moogle", execute=True, **options))


@pytest.mark.parametrize("ride", [True, False])
def test_issue_361_zone_ride_and_named_moogle_admit_actual_talk_without_turning(tmp_path, monkeypatch, ride):
    replay = Replay()
    replay.install(monkeypatch)
    result = replay.run(tmp_path, ride=ride)
    assert result["ok"], result
    operations = [op for op, _ in replay.events]
    assert "move_stick" not in operations and "align_camera_to_bearing" not in operations
    if ride:
        admission = result["menu"]["talk_admission"]
        assert replay.zone.name == "Ghost Square"
        assert result["arrival_position"] == dict(zip(("x", "y", "z"), replay.zone.arrival))
        assert operations.count("interaction_pulse") == 1 and operations.count("input_gate") == 2
        assert operations.index("foreground_check") < operations.index("interaction_pulse")
        assert result["walk"]["phases_seconds"]["route"] == 36.9
    else:
        assert result["state"] == "talk_offered" and "interaction_pulse" not in operations
        admission = result["face"]["facing"]["admission"]
    assert admission["distance_m"] == 0.843 and admission["target"]["canonical_id"] == CANONICAL
    assert admission["actor_id"] == "synthetic-moogle" and admission["prompt"]["commands"] == [field_prompt.TALK_KEY]


def test_issue_361_heading_replay_still_refuses_without_scoped_moogle_proof():
    replay = Replay()
    replay.arrived, replay.feet = True, {**TARGET, "x": TARGET["x"] + 84.3}
    with pytest.raises(MissionError, match="npc_body_facing_not_ready") as refused:
        asyncio.run(npc_approach.face_until_talk(replay.rpc, {**TARGET, "canonical_id": CANONICAL}, sleep=replay.sleep))
    assert refused.value.response["facing"]["heading_error_degrees"] == -88.5
    assert refused.value.response["facing"]["taps"] == 3


def defect(replay, name):
    def scan(value):
        snapshot, sample = value["snapshot"], value["sample"]
        target = snapshot["characters"][0]
        if name == "replacement":
            target["id"] = "another-moogle"
        elif name == "wrong_class":
            target["class"] = "BlueprintGeneratedClass X.Shopkeeper_C"
        elif name == "moved_target":
            target["position"]["x"] += 11
        elif name == "duplicate":
            snapshot["characters"].append(copy.deepcopy(target))
        elif name == "nearby_npc":
            snapshot["characters"].append({"id": "shopkeeper", "is_player": False,
                                          "position": dict(sample["player"]["position"])})
        elif name == "incomplete":
            snapshot["complete"] = False
        elif name == "stale_inventory":
            snapshot["source_sequence"] = 0
        elif name == "wrong_source":
            snapshot["source"] = "synthetic_unknown"
        elif name == "wrong_request":
            snapshot["request_id"] = "older-request"
        elif name == "target_is_player":
            target["is_player"] = True
        elif name == "nonfinite":
            target["position"]["x"] = float("nan")
        elif name == "stale_sample":
            sample["telemetry"]["age_seconds"] = 10
        elif name == "simulation":
            sample["source"] = "simulation"
        elif name == "wrong_map":
            sample["map_id"] = "Other"
        elif name == "wrong_player":
            sample["player"]["actor_id"] = "Other"
        elif name == "out_of_range":
            sample["player"]["position"]["x"] += 600
        elif name == "height":
            sample["player"]["position"]["z"] += 300
        elif name == "outside_close_range":
            sample["player"]["position"]["x"] += 100

    def prompt(value):
        if name == "wrong_command":
            value["commands"] = [field_prompt.DOOR_KEY]
        elif name == "lost_talk":
            value["talk"] = False
        elif name == "stale_prompt":
            value["source_sequence"] = 0
        elif name == "out_of_range":
            value["player_position"]["x"] += 600
        elif name == "height":
            value["player_position"]["z"] += 300
        elif name == "outside_close_range":
            value["player_position"]["x"] += 100
    replay.mutate_scan, replay.mutate_prompt = scan, prompt


@pytest.mark.parametrize("name", ["replacement", "wrong_class", "moved_target", "duplicate", "nearby_npc",
                                 "incomplete", "stale_inventory", "stale_sample", "simulation", "wrong_map",
                                 "wrong_player", "wrong_command", "lost_talk", "stale_prompt", "out_of_range",
                                 "height", "outside_close_range", "wrong_source", "wrong_request",
                                 "target_is_player", "nonfinite"])
def test_worker_rechecks_target_prompt_and_range_before_any_talk_or_menu_input(tmp_path, monkeypatch, name):
    replay = Replay()
    replay.install(monkeypatch)
    replay.before_use = lambda: defect(replay, name)
    result = replay.run(tmp_path)
    assert not result["ok"], result
    assert not any(op in {"interaction_pulse", "menu_pulse"} for op, _ in replay.events)


def test_outside_close_range_retains_the_fresh_body_facing_requirement(tmp_path, monkeypatch):
    replay = Replay()
    replay.install(monkeypatch)
    def moved_but_facing():
        replay.feet["x"] += 100
        replay.yaw = 180
    replay.before_use = moved_but_facing
    result = replay.run(tmp_path)
    assert result["ok"] and result["menu"]["talk_admission"]["distance_m"] == 1.843


@pytest.mark.parametrize("change", ["map", "player", "drift", "heading", "stale"])
def test_last_input_gate_still_requires_the_same_settled_live_player(tmp_path, monkeypatch, change):
    replay = Replay()
    replay.install(monkeypatch)
    if change == "heading":
        def outside_close_range():
            replay.feet["x"] += 100
            replay.yaw = 180
        replay.before_use = outside_close_range
    normal_gate = replay.service._assert_real_movement_allowed
    def final_change():
        state = normal_gate()
        if replay.gates == 2:
            value = state.to_dict()
            if change == "map":
                value["map_id"] = "Other"
            elif change == "player":
                value["player"]["actor_id"] = "Other"
            elif change == "drift":
                value["player"]["position"]["x"] += 20
            elif change == "heading":
                value["player"]["rotation"]["yaw"] = 0
            state = SimpleNamespace(to_dict=lambda: value, age_seconds=lambda: 10 if change == "stale" else 0.01)
        return state
    replay.service._assert_real_movement_allowed = final_change
    result = replay.run(tmp_path)
    assert not result["ok"], result
    assert not any(op in {"interaction_pulse", "menu_pulse"} for op, _ in replay.events)


def test_ambiguous_talk_during_approach_stops_without_reapproach_or_reset(tmp_path, monkeypatch):
    replay = Replay()
    replay.install(monkeypatch)
    defect(replay, "nearby_npc")
    result = replay.run(tmp_path, ride=False)
    assert not result["ok"] and result["recovery_refused"]
    assert result["reason"] == "guide_moogle_talk_prompt_ambiguous"
    assert sum(op == "synthetic_route" for op, _ in replay.events) == 1
    assert not any(op in {"move_stick", "use_guide_moogle", "interaction_pulse"} for op, _ in replay.events)


@pytest.mark.parametrize("gate_number", [1, 2])
@pytest.mark.parametrize("failure", ["input stop is latched", "speech_delivery_failing", "FFVII Rebirth process was not found"])
def test_existing_input_gate_refusals_still_precede_the_moogle_talk(tmp_path, monkeypatch, failure, gate_number):
    replay = Replay()
    replay.install(monkeypatch)
    replay.gate_failure = failure
    replay.gate_fail_on = gate_number
    assert not replay.run(tmp_path)["ok"]
    assert not any(op in {"interaction_pulse", "menu_pulse"} for op, _ in replay.events)


@pytest.mark.parametrize("change_before_use", [False, True])
def test_runtime_identity_is_required_before_route_and_again_before_ride(tmp_path, monkeypatch, change_before_use):
    replay = Replay()
    replay.install(monkeypatch)
    checks = []
    def check(identity, bootstrap):
        checks.append(identity)
        return {"ok": change_before_use and len(checks) == 1}
    result = replay.run(tmp_path, identity_check=check)
    assert not result["ok"] and "runtime_identity_mismatch" in result["reason"]
    assert len(checks) == (2 if change_before_use else 1)
    assert not any(op in {"use_guide_moogle", "interaction_pulse", "menu_pulse"} for op, _ in replay.events)
    assert sum(op == "synthetic_route" for op, _ in replay.events) == int(change_before_use)
