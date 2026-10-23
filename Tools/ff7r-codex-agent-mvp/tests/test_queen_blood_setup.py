"""Synthetic setup transitions; no input, telemetry, or service processes."""
import asyncio
from copy import deepcopy
import math
import threading

import pytest

from ff7r_agent import queen_blood_setup as setup

CARDS = ["card_id_000"] * 2 + ["card_id_002"] * 2 + ["card_id_006"] * 2 + ["card_id_007"] * 2 + ["card_id_008"] * 2 + ["card_id_010"] * 2 + ["card_id_024"] * 2 + ["card_id_004"]
LINES = {
    "opponent": {"text": "I'll play Zhijun with this deck.", "tone": "calm"},
    "approach": {"text": "Let's get close enough to talk.", "tone": "neutral"},
    "interaction": {"text": "I'll ask him for a game.", "tone": "neutral"},
    "deck": {"text": "Mu and Wolf should help build strength across the lanes.", "tone": "confident"},
    "begin": {"text": "The deck is ready; let's see our opening hand.", "tone": "calm"},
    "edit": {"text": "I'll add Mu to strengthen our early cards.", "tone": "neutral"},
    "travel": {"text": "I'll head to the town first.", "tone": "neutral"},
    "arrival_approach": {"text": "From here I'll walk to his table.", "tone": "neutral"},
}


class FakeRpc:
    def __init__(self):
        self.calls = []
        self.lease = {"active": False}
        self.run = {"ok": True, "loaded_save_verified": True, "run_loaded": True, "profile_id": "profile",
            "driver": {"model_id": "test-model", "effort": "xhigh"},
            "assignment": {"model_id": "test-model", "slot": 15},
            "loaded_save": {"load_id": "receipt", "slot": 15, "process": {"pid": "123", "token": "creation"}}}
        self.deck = list(CARDS)
        self.edit_result = None
        self.speech = []

    async def __call__(self, op, args):
        self.calls.append((op, deepcopy(args)))
        if op in {"get_runtime_identity", "get_bootstrap_verification"}:
            return {"ok": True}
        if op == "get_lifecycle_status":
            return {"run_context": {"pid": 123, "run_id": "game-run", "executable_sha256": "sha"}}
        if op == "resolve_world_target":
            # The live resolver refuses a target that is not loaded here (#321).
            return {"ok": False, "status": "known_but_not_currently_instantiated",
                "selection_reason": "no_matching_live_end_character_base_actor"}
        if op == "get_queens_blood_run_status":
            return deepcopy(self.run)
        if op == "get_control_session_status":
            return deepcopy(self.lease)
        if op == "open_control_session":
            self.lease = {"active": True, "client_id": args["client_id"], "game_pid": args["game_pid"],
                "recording_driver": {"modelId": args["model_id"], "effort": args["reasoning_effort"]}}
            return self.lease
        if op in {"close_control_session", "stop_all_input"}:
            self.lease = {"active": False}
            return {"ok": True}
        if op == "say_on_stream":
            return {"ok": True, "result": self.speech.pop(0) if self.speech else {"ok": True, "previous_delivery": {"status": "delivered"}}}
        if op == "prepare_queens_blood_deck":
            return {"ok": True, "candidate": "prepared"}
        if op == "revalidate_queens_blood_prepared_deck":
            return {"ok": True}
        if op == "run_initiate_queens_blood_match":
            return {"ok": True, "outcome": "deck_select_ready" if args.get("stop_at_deck_select") else "mulligan_ready"}
        if op == "read_queens_blood_cards":
            return {"ok": True, "cards": [{"card_id": c} for c in self.deck]}
        if op == "edit_queens_blood_deck":
            return self.edit_result or {"ok": True, "after": list(CARDS), "changed": self.deck != CARDS}
        if op == "observe_queens_blood":
            return {"sequence": 123, "queen_blood": {"screen": "mulligan", "hand": [{"card_id": c} for c in CARDS[:5]]}}
        if op == "capture_game_window":
            return {"ok": True, "image_path": "capture"}
        raise AssertionError(op)


async def nearby(rpc, name, **kwargs):
    assert kwargs["pursuit"] is True
    return {"ok": True, "state": "talk_offered"}


def run(rpc, tmp_path, **kwargs):
    values = dict(target_id="field_object:2410-GONGV:oba2410_Actor_CardGamer02", cards=CARDS,
        model_id="test-model", reasoning_effort="xhigh", client_id="setup-test", expected_ref="codex/test",
        expected_commit="a" * 40, repository_root=tmp_path, narration=deepcopy(LINES), output=tmp_path,
        allow_travel=False, approach=nearby, identity_checker=lambda *a: lambda *a: {"ok": True})
    values.update(kwargs)
    return asyncio.run(setup.setup_match(rpc, **values))


def test_nearby_noop_setup_stops_at_verified_hand_without_travel_or_mulligan_decision(tmp_path):
    rpc = FakeRpc()
    result = run(rpc, tmp_path)
    assert result["ok"] and result["outcome"] == "mulligan_ready"
    assert result["deck"] == CARDS
    assert [s["key"] for s in result["speech"]] == ["opponent", "approach", "interaction", "deck", "begin"]
    assert sum(op == "observe_queens_blood" for op, _ in rpc.calls) == 1
    assert not any(op in setup.ASYNC_CHILDREN or op == "mulligan_cards" for op, _ in rpc.calls)


@pytest.mark.parametrize("change,reason", [
    ({"cards": CARDS[:14]}, "exact_15"), ({"timeout_seconds": "900"}, "timeout"),
    ({"timeout_seconds": math.nan}, "timeout"), ({"timeout_seconds": True}, "timeout"),
    ({"narration": {**LINES, "deck": {"text": "debug telemetry", "tone": "calm"}}}, "narration"),
])
def test_invalid_plan_cannot_send_input(tmp_path, change, reason):
    rpc = FakeRpc()
    result = run(rpc, tmp_path, **change)
    assert not result["ok"] and reason in result["reason"]
    assert not any(op in {"open_control_session", "run_initiate_queens_blood_match", "edit_queens_blood_deck"} for op, _ in rpc.calls)


@pytest.mark.parametrize("field", ["slot", "driver", "foreign"])
def test_rejects_wrong_save_driver_or_unattributable_session_before_approach(tmp_path, field):
    rpc = FakeRpc()
    if field == "slot": rpc.run["loaded_save"]["slot"] = 13
    if field == "driver": rpc.run["driver"]["model_id"] = "other-model"
    # A session for another model or game is not borrowed.
    if field == "foreign": rpc.lease = {"active": True, "client_id": "other-client"}
    result = run(rpc, tmp_path)
    assert not result["ok"] and result["stage"] == "preflight"
    assert not any(op in {"stop_all_input", "close_control_session", "run_initiate_queens_blood_match"} for op, _ in rpc.calls)


def test_a_session_open_for_this_model_and_game_is_borrowed_and_left_open(tmp_path):
    rpc = FakeRpc()
    rpc.lease = {"active": True, "client_id": "agent-session", "game_pid": 123,
                 "recording_driver": {"modelId": "test-model", "effort": "xhigh"}}
    result = run(rpc, tmp_path)
    assert result["ok"] and result["outcome"] == "mulligan_ready"
    assert not any(op in {"open_control_session", "close_control_session", "stop_all_input"} for op, _ in rpc.calls)
    assert rpc.lease["active"] and rpc.lease["client_id"] == "agent-session"


def test_failed_exact_deck_verification_never_begins_match(tmp_path):
    rpc = FakeRpc(); rpc.edit_result = {"ok": True, "after": CARDS[:14] + ["card_id_117"]}
    result = run(rpc, tmp_path)
    assert not result["ok"] and result["stage"] == "deck"
    assert "verification_failed" in result["reason"]
    assert len([args for op, args in rpc.calls if op == "run_initiate_queens_blood_match"]) == 1
    assert any(op == "stop_all_input" for op, _ in rpc.calls)


def test_stale_swap_narration_is_suppressed(tmp_path):
    rpc = FakeRpc(); rpc.deck[0] = "card_id_117"
    result = run(rpc, tmp_path)
    assert not result["ok"] and "edit_plan_changed" in result["reason"]
    assert "edit" not in [s["key"] for s in result["speech"]]
    assert not any(op == "edit_queens_blood_deck" for op, _ in rpc.calls)


def test_substantial_edit_uses_verified_original_for_swap_commentary(tmp_path):
    rpc = FakeRpc(); rpc.deck[0] = "card_id_117"
    lines = deepcopy(LINES); lines["edit_from"] = list(rpc.deck)
    result = run(rpc, tmp_path, narration=lines)
    assert result["ok"] and result["deck"] == CARDS
    assert "edit" in [s["key"] for s in result["speech"]]


@pytest.mark.parametrize("receipt", [{"ok": False, "reason": "not_live"},
    {"ok": True, "previous_delivery": {"status": "failed"}},
    {"ok": False, "reason": "rate_limited", "retry_after_seconds": 40}])
def test_speech_refusals_stop_before_next_action(tmp_path, receipt):
    rpc = FakeRpc(); rpc.speech = [receipt]
    result = run(rpc, tmp_path)
    assert not result["ok"] and len(result["speech"]) == 1
    assert not any(op == "run_initiate_queens_blood_match" for op, _ in rpc.calls)


def test_rate_limit_wait_then_rechecks_loaded_save_before_retry(tmp_path):
    rpc = FakeRpc(); rpc.speech = [{"ok": False, "reason": "rate_limited", "retry_after_seconds": .01}]
    result = run(rpc, tmp_path)
    assert result["ok"] and len(result["speech"]) == 6
    calls = [op for op, _ in rpc.calls]; first = calls.index("say_on_stream")
    assert calls[first + 1] == "get_queens_blood_run_status"


def test_changed_run_after_approach_cannot_open_interaction(tmp_path):
    rpc = FakeRpc()
    async def changed(*args, **kwargs):
        rpc.run["loaded_save"]["load_id"] = "human-loaded"
        return {"ok": True, "state": "talk_offered"}
    result = run(rpc, tmp_path, approach=changed)
    assert not result["ok"] and "loaded_save_changed" in result["reason"]
    assert not any(op == "run_initiate_queens_blood_match" for op, _ in rpc.calls)


def test_forbidden_travel_stops_before_dispatch(tmp_path):
    rpc = FakeRpc()
    async def travelling(local, *args, **kwargs):
        await local("travel_to_map_spot_async", {"spot": "kalm", "execute": True})
    result = run(rpc, tmp_path, approach=travelling)
    assert not result["ok"] and "travel_not_authorized" in result["reason"]
    assert not any(op == "travel_to_map_spot_async" for op, _ in rpc.calls)


def test_lazy_executable_verification_does_not_look_like_a_restart(tmp_path):
    rpc = FakeRpc(); original = rpc.__call__; calls = 0
    async def local(op, args):
        nonlocal calls
        if op == "get_lifecycle_status":
            calls += 1
            return {"run_context": {"pid": 123, "run_id": "attached", "executable_sha256": None if calls == 1 else "sha"}}
        return await original(op, args)
    assert run(local, tmp_path)["ok"]


@pytest.mark.parametrize("defect", [None, "foreign", "far", "facing", "menu", "stale"])
def test_nearby_shortcut_requires_fresh_identified_facing_talk_target(defect):
    resolved = {"status": "located", "canonical_id": "target", "location_quality": "authoritative_live",
        "live_actor": {"id": "actor", "is_player": False}, "position": {"x": 200, "y": 0, "z": 0}, "map_id": "map"}
    state = {"running": True, "telemetry": {"live": True}, "map_id": "map", "sequence": 10, "age_seconds": .1,
        "player": {"position": {"x": 0, "y": 0, "z": 0}, "rotation": {"yaw": 0}}}
    if defect == "foreign": resolved["canonical_id"] = "other"
    if defect == "far": resolved["position"]["x"] = 600
    if defect == "facing": state["player"]["rotation"]["yaw"] = 180
    if defect == "menu": state["in_menu"] = True
    if defect == "stale": state["age_seconds"] = 10
    async def rpc(op, args):
        return resolved if op == "resolve_world_target" else state if op == "observe_game" else {"talk": True, "source_sequence": 11}
    result = asyncio.run(setup.already_at_target(rpc, "target"))
    assert bool(result) == (defect is None)


def test_unloaded_target_is_not_nearby_so_the_approach_can_travel(tmp_path):
    rpc = FakeRpc(); seen = {}
    async def approach(local, name, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "state": "talk_offered"}
    result = run(rpc, tmp_path, approach=approach)
    assert result["ok"] and "approach" in result["completed_stages"]
    assert "reset_spot" in seen


def test_nearby_check_still_stops_on_an_identity_refusal():
    async def rpc(op, args):
        return {"ok": False, "error": "runtime_identity_changed", "recovery_refused": True}
    with pytest.raises(setup.MissionError, match="runtime_identity_changed"):
        asyncio.run(setup.already_at_target(rpc, "target"))


def test_single_child_and_polling_do_not_repeat_work():
    gate = threading.Event(); calls = []
    def invoke(op, args):
        calls.append(op); gate.wait(2); return {"ok": True}
    async def scenario():
        rpc = setup.SetupRpc(invoke, {})
        try:
            job = await rpc("travel_to_map_spot_async", {})
            for _ in range(10):
                assert (await rpc("get_travel_to_map_spot_status", {"job_id": job["job_id"]}))["state"] == "running"
            with pytest.raises(Exception, match="input_in_progress"):
                await rpc("start_live_route_adapter", {})
            assert calls == ["travel_to_map_spot"]
        finally:
            gate.set(); rpc.close()
    asyncio.run(scenario())


def test_watchdog_stops_input_and_closes_lease_before_returning(monkeypatch, tmp_path):
    # Synthetic accelerated wall clock: production's minimum is 30 seconds.
    rpc = FakeRpc(); original = rpc.__class__.__call__; wait_for = asyncio.wait_for
    async def blocked(self, op, args):
        if op == "edit_queens_blood_deck":
            await asyncio.sleep(10)
        return await original(self, op, args)
    monkeypatch.setattr(FakeRpc, "__call__", blocked)
    monkeypatch.setattr(setup.asyncio, "wait_for", lambda task, timeout: wait_for(task, timeout=.02))
    result = run(rpc, tmp_path)
    assert not result["ok"] and result["reason"] == "match_setup_deadline_exceeded"
    ops = [op for op, _ in rpc.calls]
    assert ops.index("stop_all_input") < ops.index("capture_game_window")
    assert rpc.lease["active"] is False
