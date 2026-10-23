"""Offline replays of the wasted restarts and missing timings in issue #196."""

import asyncio
import json

import pytest

from ff7r_agent import npc_approach
from ff7r_agent.navigation_values import MissionError
from test_npc_approach import fake_control_lease


class ReplayClock:
    def __init__(self):
        self.seconds = 0.0

    def __call__(self):
        return self.seconds

    async def sleep(self, seconds):
        self.seconds += seconds


@pytest.mark.parametrize("returns", [True, False])
def test_arrival_waits_for_physical_feet_without_guessing_or_sending_input(returns):
    clock = ReplayClock()
    calls = []
    feet = {"x": 100, "y": 200, "z": 300}

    async def rpc(operation, arguments):
        calls.append(operation)
        return {"map_id": "EndGame", "player": {"position": {**feet, "z": 388},
                "locomotion": {"feet_position": feet} if returns and len(calls) > 1 else None}}

    task = npc_approach.wait_for_field(rpc, sleep=clock.sleep, clock=clock,
                                     seconds=2, require_locomotion=True)
    if returns:
        observed = asyncio.run(task)
        assert observed["player"]["locomotion"]["feet_position"] == feet
        assert clock.seconds == 1
    else:
        with pytest.raises(MissionError, match="field_locomotion_unavailable"):
            asyncio.run(task)
        assert clock.seconds == 2
    assert set(calls) == {"observe_game"}


class ApproachReplay:
    """All observations, input acknowledgements and elapsed time are simulated."""

    def __init__(self):
        self.clock = ReplayClock()
        self.feet = {"x": 5000.0, "y": 0.0, "z": 0.0}
        self.calls = []
        self.routes = []
        self.steps = []
        self.travels = []

    async def rpc(self, operation, arguments):
        self.calls.append(operation)
        if operation == "resolve_world_target":
            self.clock.seconds += 2.0
            return {"status": "located", "canonical_id": "field_object:offline-npc",
                    "position": {"x": 0.0, "y": 0.0, "z": 88.0}}
        if operation == "observe_game":
            return {"map_id": "EndGame", "player": {
                "position": dict(self.feet), "rotation": {"yaw": 180.0},
                "locomotion": {"feet_position": dict(self.feet), "grounded": True}},
                "camera": {"rotation": {"yaw": 180.0}}}
        if operation == "plan_npc_approach":
            self.clock.seconds += 3.0
            return {"library": {}, "approach": {"position": {"x": 200.0, "y": 0.0, "z": 0.0}}}
        if operation == "read_field_prompt":
            talk = self.feet["x"] == 200.0
            return {"commands": [npc_approach.TALK_KEY] if talk else [], "talk": talk,
                    "player_position": dict(self.feet)}
        return {"ok": True}

    def install(self, monkeypatch, route_result, *, step_moves=False):
        replay = self

        class Mission:
            def __init__(self, rpc, output, identity_check, config, client_id=None):
                self.output = output

            async def run(self):
                replay.routes.append(self.output)
                reason, seconds = route_result(replay)
                replay.clock.seconds += seconds
                if isinstance(reason, Exception):
                    raise reason
                if reason is None:
                    replay.feet["x"] = 200.0
                return {"ok": reason is None, "state": "ARRIVED" if reason is None else "STOPPED",
                        "reason": reason, "metrics": {"movement_cycles": 1 if reason is None else 0}}

        async def extricate(rpc, target, sleep=None):
            replay.steps.append(dict(replay.feet))
            replay.clock.seconds += 2.0
            if step_moves:
                replay.feet["x"] -= 100.0
            return {"moved": step_moves, "moved_m": 1.0 if step_moves else 0.0}

        async def travel(rpc, spot, sleep=None, clock=None):
            replay.travels.append(spot)
            replay.clock.seconds += 10.0
            return {"state": "arrived", "arrival_position": dict(replay.feet)}

        monkeypatch.setattr(npc_approach, "NavigationMission", Mission)
        monkeypatch.setattr(npc_approach, "extricate", extricate)
        monkeypatch.setattr(npc_approach, "travel_to_spot", travel)

    def run(self, output, **options):
        return asyncio.run(npc_approach.approach_npc(
            fake_control_lease(self.rpc), "Offline NPC", output=output, identity_check=None, execute=True,
            clock=self.clock, sleep=self.clock.sleep,
            model_id="gpt-6.1-sol", reasoning_effort="xhigh", **options))


def assert_accounted(report):
    phases = report["phases_seconds"]
    assert sum(seconds for phase, seconds in phases.items() if phase != "total") == pytest.approx(
        phases["total"], abs=0.03)


def test_regina_failure_replay_resets_after_one_unusable_restart(tmp_path, monkeypatch):
    replay = ApproachReplay()

    def result(runtime):
        if runtime.travels:
            return None, 58.0
        if len(runtime.routes) == 1:
            return "pursuit_telemetry_stall", 48.0
        return "final_pre_movement_gate_failed:cache_current_cloud_unresolvable", 21.0

    replay.install(monkeypatch, result)
    report = replay.run(tmp_path, reset_spot="Gongaga")

    assert report["ok"]
    assert [(path.parent.name, path.name) for path in replay.routes] == [
        (tmp_path.name, "route-1"), (tmp_path.name, "route-2"), ("after-reset", "route-1")]
    assert len(replay.steps) == 1 and replay.travels == ["Gongaga"]
    assert "cache_current_cloud_unresolvable" in report["before_reset"]["reason"]
    assert [route["seconds"] for route in report["before_reset"]["routes"]] == [48.0, 21.0]
    assert report["reset"]["seconds"] == 10.0  # Does not include the second approach.
    assert report["phases_seconds"]["route"] == 129.0  # 48 + 21 + step 2 + 58.
    assert report["phases_seconds"]["total"] == 149.5
    assert report["phases_seconds"]["total"] == replay.clock.seconds
    assert report["before_reset"]["phases_seconds"]["total"] == 76.0
    after = json.loads((tmp_path / "after-reset" / "result.json").read_text(encoding="utf-8"))
    assert after["phases_seconds"]["total"] == 63.5
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == report
    assert_accounted(report)
    assert_accounted(after)


@pytest.mark.parametrize("reason", ["live_start_not_inside_current_topology", "cache_current_cloud_unresolvable"])
def test_unusable_start_without_a_clear_step_does_not_retry_or_face(tmp_path, monkeypatch, reason):
    replay = ApproachReplay()
    replay.install(monkeypatch, lambda _: (reason, 21.0))
    report = replay.run(tmp_path)

    assert not report["ok"] and "npc_approach_start_unresolvable" in report["reason"]
    assert len(replay.routes) == 1 and len(replay.steps) == 1
    assert "align_camera_to_bearing" not in replay.calls and "face_failed" not in report
    assert "reapproach" not in report
    assert report["phases_seconds"]["route"] == 23.0
    assert_accounted(report)


@pytest.mark.parametrize("reason", ["live_start_not_inside_current_topology", "cache_current_cloud_unresolvable"])
def test_a_successful_step_allows_a_fresh_validated_route(tmp_path, monkeypatch, reason):
    replay = ApproachReplay()
    replay.install(monkeypatch, lambda runtime: (reason if len(runtime.routes) == 1 else None, 21.0),
                   step_moves=True)
    report = replay.run(tmp_path)

    assert report["ok"] and len(replay.routes) == 2 and len(replay.steps) == 1
    assert report["start_steps"][0]["moved"]
    assert replay.travels == []
    assert_accounted(report)


def test_unusable_start_recovery_remains_bounded_after_movement(tmp_path, monkeypatch):
    replay = ApproachReplay()
    replay.install(monkeypatch, lambda _: ("cache_current_cloud_unresolvable", 21.0), step_moves=True)
    report = replay.run(tmp_path)

    assert not report["ok"] and len(replay.steps) == npc_approach.MAX_START_STEPS
    assert len(replay.routes) == npc_approach.MAX_START_STEPS + 1
    assert "reapproach" not in report
    assert_accounted(report)


def test_transient_route_failure_still_retries_without_an_unneeded_step(tmp_path, monkeypatch):
    replay = ApproachReplay()
    replay.install(monkeypatch, lambda runtime: (
        "pursuit_telemetry_stall" if len(runtime.routes) == 1 else None, 10.0))
    report = replay.run(tmp_path)

    assert report["ok"] and len(replay.routes) == 2
    assert replay.steps == [] and replay.travels == []
    assert_accounted(report)


def test_unusable_start_does_not_reset_twice_if_recovery_still_fails(tmp_path, monkeypatch):
    replay = ApproachReplay()
    replay.install(monkeypatch, lambda _: ("cache_current_cloud_unresolvable", 21.0))
    report = replay.run(tmp_path, reset_spot="Gongaga")

    assert not report["ok"] and len(replay.routes) == 2 and len(replay.steps) == 2
    assert replay.travels == ["Gongaga"]
    assert report["before_reset"]["reason"] == report["reason"]
    assert report["phases_seconds"]["total"] == 66.0
    assert_accounted(report)


def test_a_refused_route_already_in_talk_range_still_checks_the_prompt(tmp_path, monkeypatch):
    replay = ApproachReplay()

    def result(runtime):
        runtime.feet["x"] = 200.0
        return "cache_current_cloud_unresolvable", 21.0

    replay.install(monkeypatch, result)
    report = replay.run(tmp_path)

    assert report["ok"] and report["state"] == "talk_offered"
    assert len(replay.routes) == 1 and replay.steps == [] and replay.travels == []
    assert_accounted(report)


def test_face_failure_and_reapproach_have_separate_timings(tmp_path, monkeypatch):
    replay = ApproachReplay()
    replay.install(monkeypatch, lambda _: (None, 21.0))
    faces = []

    async def face(rpc, target, sleep=None):
        faces.append(target)
        replay.clock.seconds += 3.0
        if len(faces) == 1:
            raise MissionError("npc_talk_prompt_not_offered")
        return {"ok": True}

    monkeypatch.setattr(npc_approach, "face_until_talk", face)
    report = replay.run(tmp_path)

    assert report["ok"] and len(replay.routes) == 2
    assert report["reapproach"]["seconds"] == 21.0
    assert report["phases_seconds"]["reapproach"] == 21.0
    assert report["phases_seconds"]["face"] == 6.0
    assert_accounted(report)


def test_route_exception_time_is_kept_in_the_failed_phase(tmp_path, monkeypatch):
    replay = ApproachReplay()
    replay.install(monkeypatch, lambda _: (MissionError("route_refused"), 21.0))
    report = replay.run(tmp_path)

    assert not report["ok"] and report["reason"] == "route_refused"
    assert report["phases_seconds"]["route"] == 21.0
    assert report["phases_seconds"]["total"] == 26.0
    assert_accounted(report)


@pytest.mark.parametrize("arrival", ["refused", "position_missing"])
def test_failed_reset_time_is_included_without_a_second_attempt(tmp_path, monkeypatch, arrival):
    replay = ApproachReplay()
    replay.install(monkeypatch, lambda _: ("cache_current_cloud_unresolvable", 21.0))

    async def failed_travel(rpc, spot, sleep=None, clock=None):
        replay.clock.seconds += 7.0
        if arrival == "refused":
            raise MissionError("fast_travel_unavailable")
        return {"state": "arrived", "arrival_position": None}

    monkeypatch.setattr(npc_approach, "travel_to_spot", failed_travel)
    report = replay.run(tmp_path, reset_spot="Gongaga")

    assert not report["ok"] and len(replay.routes) == 1
    assert report["reset"]["seconds"] == 7.0
    assert report["phases_seconds"]["total"] == 35.0
    assert_accounted(report)


def test_stuck_handoff_time_and_persisted_report_are_accounted(tmp_path, monkeypatch):
    replay = ApproachReplay()
    replay.install(monkeypatch, lambda _: ("cache_current_cloud_unresolvable", 21.0))

    async def handoff(rpc, target, reset_spot):
        replay.clock.seconds += 3.0
        return {"options": ["manual_escape"], "position": dict(replay.feet)}

    monkeypatch.setattr(npc_approach, "stuck_report", handoff)
    report = replay.run(tmp_path, hand_off_when_stuck=True)

    assert report["state"] == "stuck" and not report["ok"]
    assert report["phases_seconds"]["overhead"] == 3.0
    assert report["phases_seconds"]["total"] == 31.0
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == report
    assert_accounted(report)


@pytest.mark.parametrize("fails", [False, True])
def test_initial_travel_is_separate_from_resolution_even_when_interrupted(tmp_path, monkeypatch, fails):
    replay = ApproachReplay()
    replay.feet["x"] = (npc_approach.TRAVEL_FIRST_M + 10.0) * 100.0
    replay.install(monkeypatch, lambda _: (None, 21.0))

    async def initial_travel(rpc, spot, sleep=None, clock=None):
        replay.clock.seconds += 7.0
        if fails:
            raise MissionError("fast_travel_unavailable")
        replay.feet["x"] = 5000.0
        return {"state": "arrived", "arrival_position": dict(replay.feet)}

    monkeypatch.setattr(npc_approach, "travel_to_spot", initial_travel)
    # Test the attempt directly: the wrapper may legitimately reset after failure.
    report = asyncio.run(npc_approach._approach_once(
        replay.rpc, "Offline NPC", output=tmp_path, identity_check=None,
        execute=True, stop_distance_m=2.0, camera_follow=False,
        travel_spot="Gongaga", clock=replay.clock, sleep=replay.clock.sleep))

    assert report["ok"] is not fails
    assert report["phases_seconds"]["resolve"] == 2.0
    assert report["phases_seconds"]["travel_first"] == 7.0
    if not fails:
        assert report["phases_seconds"]["target_refresh"] == 2.0
    assert_accounted(report)


@pytest.mark.parametrize("fails", [False, True])
def test_moogle_travel_has_its_own_phase_even_after_a_failed_ride(tmp_path, monkeypatch, fails):
    from types import SimpleNamespace

    replay = ApproachReplay()
    replay.install(monkeypatch, lambda _: (None, 21.0))
    original_rpc = replay.rpc

    async def rpc(operation, arguments):
        result = await original_rpc(operation, arguments)
        if operation == "resolve_world_target":
            result["canonical_id"] = npc_approach.GOLD_SAUCER_FIELD + ":offline-npc"
        return result

    replay.rpc = rpc
    monkeypatch.setattr(npc_approach, "worth_guide_moogle", lambda *_: SimpleNamespace(name="Offline Square"))

    async def ride(*args, **kwargs):
        replay.clock.seconds += 7.0
        if fails:
            raise MissionError("moogle_unavailable")
        return {"state": "arrived"}

    monkeypatch.setattr(npc_approach, "travel_by_guide_moogle", ride)
    report = replay.run(tmp_path)

    assert report["ok"]
    assert report["phases_seconds"]["resolve"] == 2.0
    assert report["phases_seconds"]["guide_moogle"] == 7.0
    assert_accounted(report)


def test_failed_initial_travel_is_not_replayed_as_a_reset(tmp_path, monkeypatch):
    replay = ApproachReplay()
    replay.feet["x"] = (npc_approach.TRAVEL_FIRST_M + 10.0) * 100.0
    replay.install(monkeypatch, lambda _: (None, 21.0))
    travels = []

    async def uncertain(rpc, spot, sleep=None, clock=None):
        travels.append(spot)
        replay.clock.seconds += 26.0
        raise MissionError("runtime worker request timed out; request was not retried")

    monkeypatch.setattr(npc_approach, "travel_to_spot", uncertain)
    report = replay.run(tmp_path, reset_spot="Kalm")
    assert not report["ok"] and report["reason"].startswith("npc_initial_travel_failed:")
    assert travels == ["Kalm"] and not replay.routes and "reset" not in report
    assert report["phases_seconds"]["travel_first"] == 26.0
    assert_accounted(report)
