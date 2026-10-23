from __future__ import annotations

import math
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ff7r_agent.detour_route import (
    CorridorPointResolution,
    DetourPolygon,
    DetourTopologyGraph,
    PointResolution,
    PointResolutionCandidate,
    PolygonReference,
)
from ff7r_agent.live_route_adapter import (
    AdapterState,
    AdapterError,
    CorridorDivergenceClass,
    AgentServiceTelemetrySource,
    AgentServiceRenderEvidenceSource,
    DEFAULT_MAX_CAPTURE_WAIT_SECONDS,
    IndependentJsonValidator,
    LIVE_EXECUTABLE_SHA256,
    JsonTopologyProvider,
    LiveAdapterConfig,
    LiveObservation,
    LiveRouteAdapter,
    PlanAuthorization,
    RenderedGameplayEvidence,
    TopologySession,
    world_heading_to_camera_stick,
)
from ff7r_agent.models import CameraState, Character, GameState, Rotation, Vec3
from ff7r_agent.dynamic_obstacles import DynamicObstacleSnapshot
from ff7r_agent.service import AgentService
from ff7r_agent.static_obstacles import NavigationSidecarLookahead, ObstacleEvidenceState


class FakeClock:
    def __init__(self, value: float = 10.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeTelemetry:
    def __init__(self, observations):
        self.observations = list(observations)
        self.index = 0

    def observe(self):
        value = self.observations[min(self.index, len(self.observations) - 1)]
        self.index += 1
        return value


class FakeSettlingTelemetry(FakeTelemetry):
    def __init__(self, observations, result=None, error=None):
        super().__init__(observations)
        self.result = result or {"ok": True, "readiness": "settled_loaded_active_gameplay"}
        self.error = error
        self.settle_calls = []

    def wait_for_settled_gameplay(self, timeout_seconds, max_telemetry_age_seconds=None):
        self.settle_calls.append((timeout_seconds, max_telemetry_age_seconds))
        if self.error is not None:
            raise self.error
        return self.result


class TimedSettlingTelemetry(FakeSettlingTelemetry):
    def __init__(self, observations, clock, wait_duration, *, honor_timeout=True, result=None, error=None):
        super().__init__(observations, result=result, error=error)
        self.clock = clock
        self.wait_duration = wait_duration
        self.honor_timeout = honor_timeout

    def wait_for_settled_gameplay(self, timeout_seconds, max_telemetry_age_seconds=None):
        self.settle_calls.append((timeout_seconds, max_telemetry_age_seconds))
        duration = min(self.wait_duration, timeout_seconds) if self.honor_timeout else self.wait_duration
        self.clock.value += duration
        if self.error is not None:
            raise self.error
        return self.result


class FakeIdentityTelemetry(FakeTelemetry):
    def __init__(self, observations, result):
        super().__init__(observations)
        self.result = result
        self.identity_calls = 0

    def verify_executable_identity(self, expected_sha256):
        self.identity_calls += 1
        self.expected_sha256 = expected_sha256
        return self.result

    def observe(self):
        value = super().observe()
        if self.identity_calls and self.result.get("verified") is True and not value.executable_valid:
            return replace(
                value,
                executable_valid=True,
                executable_sha256=str(self.result.get("sha256") or "").upper(),
            )
        return value


class FakeActuator:
    def __init__(self, *, fail=False):
        self.commands = []
        self.neutralize_count = 0
        self.neutral = True
        self.fail = fail

    def command(self, x, y, milliseconds):
        if self.fail:
            raise RuntimeError("actuator boom")
        self.neutral = False
        self.commands.append({"x": x, "y": y, "milliseconds": milliseconds})
        self.neutral = True
        return {"accepted": True}

    def neutralize(self):
        self.neutralize_count += 1
        self.neutral = True
        return {"neutral": True}

    def is_neutral(self):
        return self.neutral


class FakeValidator:
    def __init__(self, result=None):
        self.result = result or {"valid": True, "errors": [], "warnings": []}
        self.plans = []

    def validate(self, plan):
        self.plans.append(plan)
        return self.result


class FakeFollower:
    def __init__(self, plan, mode="FOLLOWING"):
        self.plan = plan
        self.mode = mode
        self.state = "ROUTE_READY"
        self.current_waypoint_index = 0
        self.active_waypoint = None
        self.route = type("Route", (), {})()

    def on_telemetry(self, sample):
        position = sample["position"]
        state = self.mode
        if self.mode == "ARRIVED_ON_FIRST" and position["x"] >= 350.0:
            state = "ARRIVED"
        suggested_speed_band = "stop" if self.mode == "FOLLOWING_STOP" else "normal"
        self.state = state
        return [{
            "state": state,
            "waypoint_index": self.current_waypoint_index,
            "distance_m": 4.0,
            "distance_to_final_destination_m": 4.0,
            "cross_track_error_m": 0.0,
            "route_progress_fraction": 0.0,
            "desired_heading_deg": 0.0,
            "heading_error_deg": 0.0,
            "suggested_speed_band": suggested_speed_band,
        }]

    def process_external_event(self, event_name, t):
        self.state = "REPLAN_REQUIRED"
        return {"state": self.state, "event": "plan_invalidated", "invalidation_event": event_name}


class FakeTopologyProvider:
    def __init__(self, topology):
        self.topology = topology
        self.load_calls = 0
        self.arm_calls = 0

    def arm_capture(self, observation, render_evidence):
        self.arm_calls += 1

    def load_current(self, observation):
        self.load_calls += 1
        return self.topology


class FakeRenderEvidence:
    def __init__(self, ready=True, reason="rendered_gameplay_confirmed"):
        self.evidence = RenderedGameplayEvidence(
            ready=ready,
            reason=reason,
            captured_at_utc="2026-08-18T00:00:00Z",
            classifier_label="gameplay" if ready else "black",
            metrics={"whole_nonblack": 1.0 if ready else 0.0},
            capture={"ok": True, "pid": 123},
        )
        self.calls = 0

    def observe(self, observation):
        self.calls += 1
        return self.evidence


class FakeCaptureService:
    def __init__(self, capture):
        self.capture = capture

    def capture_game_window(self):
        return self.capture


class _ProductionFreshnessInput:
    is_real = True

    def backend_status(self):
        return {"stick_state": "neutral", "last_stick": {"x": 0.0, "y": 0.0}}


class _ProductionFreshnessEnvironment:
    def game_process_exists(self):
        return True

    def game_is_foreground(self):
        return True

    def game_process_ids(self):
        return {123: "ff7rebirth_.exe"}


class _ProductionFreshnessLease:
    def status(self):
        return {"active": True, "seconds_remaining": 120}


class _ProductionFreshnessLifecycle:
    def status(self):
        return {
            "run_context": {
                "run_id": "run-1",
                "pid": 123,
                "executable_sha256": LIVE_EXECUTABLE_SHA256,
            }
        }


class _ScriptedAgeGameState(GameState):
    def __init__(self, *, scripted_age_seconds, **kwargs):
        super().__init__(**kwargs)
        object.__setattr__(self, "_scripted_age_seconds", float(scripted_age_seconds))

    def age_seconds(self, now=None):
        return self._scripted_age_seconds


class _ProductionFreshnessTelemetry:
    def __init__(self, states):
        self.states = list(states)
        self.index = 0
        self.observed_sequences = []

    def observe(self):
        state = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        self.observed_sequences.append(int(state.sequence))
        return state


def _production_freshness_state(sequence, age_seconds, *, position=None):
    position = position or Vec3(0.0, 0.0, 0.0)
    player = Character(
        "Cloud",
        100,
        100,
        10,
        10,
        position=position,
        rotation=Rotation(0.0, 0.0, 0.0),
        controller_state=1,
        actor_id="cloud-1",
    )
    return _ScriptedAgeGameState(
        scripted_age_seconds=age_seconds,
        schema_version=1,
        sequence=sequence,
        timestamp_utc="2026-08-27T00:00:00Z",
        running=True,
        paused=False,
        in_menu=False,
        in_combat=False,
        location="",
        objective="",
        player=player,
        map_id="EndGame",
        source="test",
        camera=CameraState(rotation=Rotation(0.0, 0.0, 0.0)),
    )


def make_production_freshness_adapter(tail_states, *, clock=None):
    clock = clock or FakeClock(100.0)
    states = [
        _production_freshness_state(sequence, 0.1)
        for sequence in (1, 2, 3)
    ] + list(tail_states)
    state_telemetry = _ProductionFreshnessTelemetry(states)
    service = AgentService(
        state_telemetry,
        _ProductionFreshnessInput(),
        max_telemetry_age=2.0,
        environment=_ProductionFreshnessEnvironment(),
        session_lease=_ProductionFreshnessLease(),
        monotonic=clock,
        sleeper=lambda delay: setattr(clock, "value", clock.value + delay),
        settled_gameplay_samples=3,
    )
    service.wait_for_settled_gameplay(
        timeout_seconds=1.0,
        max_telemetry_age_seconds=0.5,
    )
    source = AgentServiceTelemetrySource(
        service,
        _ProductionFreshnessLifecycle(),
        monotonic=clock,
    )
    adapter, _, actuator, _ = make_adapter(
        telemetry=source,
        clock=clock,
    )
    return adapter, service, state_telemetry, actuator, clock


class _AgeState:
    timestamp_utc = "2026-08-18T00:00:00Z"
    sequence = 12
    map_id = "EndGame"
    in_combat = False
    in_menu = False
    paused = False
    nearby_interactable = None
    nearby_actors = ()
    camera_rotation = SimpleNamespace(yaw=15.0)
    player = SimpleNamespace(
        position=Vec3(1.0, 2.0, 3.0),
        rotation=SimpleNamespace(yaw=20.0),
        actor_id="cloud-1",
    )

    def __init__(self, age: float):
        self._age = age

    def age_seconds(self):
        return self._age


class _AgeService:
    def __init__(self, state):
        self.telemetry = SimpleNamespace(observe=lambda: state)
        self._environment = SimpleNamespace(game_process_ids=lambda: {123: "ff7rebirth_.exe"})

    def session_health(self, state=None):
        return {
            "process_exists": True,
            "gameplay_ready": True,
            "target_clear": True,
            "diagnosis": "gameplay_ready",
            "health_sample_sequence": getattr(state, "sequence", 12),
            "loading_evidence": False,
        }

    def input_status(self, state=None):
        return {
            "backend_status": {"stick_state": "neutral"},
            "control_session": {"active": True},
        }


class _AgeLifecycle:
    def status(self):
        return {
            "run_context": {
                "run_id": "run-age",
                "pid": 123,
                "executable_sha256": "37D952FABBFA2B710F8AE6490997C9755E4E57B4FD545078C9BAAF46C9B233D7",
            }
        }


def make_graph():
    ref = PolygonReference.make(1, 1, 0)
    polygon = DetourPolygon(
        reference=ref,
        tile_x=1,
        tile_y=1,
        layer=0,
        vertices=(
            Vec3(-1000.0, -1000.0, 0.0),
            Vec3(1000.0, -1000.0, 0.0),
            Vec3(1000.0, 1000.0, 0.0),
            Vec3(-1000.0, 1000.0, 0.0),
        ),
        flags=1,
        area=0,
        poly_type=0,
        links=(),
        identity_source="test",
    )
    return DetourTopologyGraph({ref.value: polygon}, {}, snapshot_diagnostics={}), ref.value


def make_observation(**changes):
    values = {
        "monotonic_t": 10.0,
        "timestamp_utc": "2026-08-18T00:00:00Z",
        "sequence": 1,
        "position": Vec3(0.0, 0.0, 0.0),
        "actor_yaw": 0.0,
        "camera_yaw": 0.0,
        "actor_id": "cloud-1",
        "session_id": "run-1",
        "world_id": "EndGame",
        "map_id": "EndGame",
        "process_valid": True,
        "executable_valid": True,
        "controllable": True,
        "loading": False,
        "in_menu": False,
        "in_cutscene": False,
        "in_combat": False,
        "in_interaction": False,
        "lease_active": True,
        "actuator_neutral": True,
        "telemetry_age_seconds": 0.0,
        "topology_session_id": "run-1",
        "process_id": 123,
        "executable_sha256": "37D952FABBFA2B710F8AE6490997C9755E4E57B4FD545078C9BAAF46C9B233D7",
        "telemetry_gameplay_ready": True,
    }
    values.update(changes)
    return LiveObservation(**values)


def make_telemetry_backed_capture(
    *,
    raw_label="unknown",
    whole_nonblack=0.884,
    telemetry_age_seconds=0.13217,
    map_id="EndGame",
    telemetry_override="fresh_loaded_gameplay",
):
    return {
        "ok": True,
        "pid": 123,
        "hwnd": 456,
        "captured_at_utc": "2026-08-27T14:55:13.225853Z",
        "telemetry_sequence": 25336,
        "map_id": map_id,
        "telemetry_age_seconds": telemetry_age_seconds,
        "startup_frame": {
            "label": "gameplay",
            "render_classifier_label": raw_label,
            "telemetry_override": telemetry_override,
            "metrics": {"whole_nonblack": whole_nonblack},
        },
    }


def make_adapter(*, observations=None, validator=None, follower_mode="FOLLOWING", actuator=None, clock=None, render=None, telemetry=None, sleeper=None, config=None, route_target=None):
    graph, ref = make_graph()
    topology = TopologySession(
        graph=graph,
        artifact_path="test-topology.json",
        fingerprint="topology-fingerprint",
        session_id="run-1",
        world_id="EndGame",
        captured_at_utc="2026-08-18T00:00:00Z",
        current_session=True,
        movement_sent=False,
    )
    clock = clock or FakeClock()
    if telemetry is None:
        if observations is None:
            observations = [
                make_observation(),
                make_observation(sequence=2),
                make_observation(sequence=3),
                make_observation(sequence=4),
            ]
        elif len(observations) == 1 and observations[0].telemetry_age_seconds <= 0.5:
            observations = list(observations) + [replace(observations[0], sequence=observations[0].sequence + 1)]
        telemetry = FakeTelemetry(observations)
    actuator = actuator or FakeActuator()
    validator = validator or FakeValidator()
    follower_factory = lambda plan: FakeFollower(plan, follower_mode)
    topology_provider = FakeTopologyProvider(topology)
    adapter = LiveRouteAdapter(
        telemetry,
        topology_provider,
        validator,
        follower_factory,
        actuator,
        render_evidence=render or FakeRenderEvidence(),
        config=config or LiveAdapterConfig(max_cycles=4, post_arrival_samples=0),
        route_target=route_target,
        monotonic=clock,
        sleeper=sleeper or (lambda delay: setattr(clock, "value", clock.value + delay)),
    )
    return adapter, telemetry, actuator, ref


def route_test_position(adapter, fraction=0.5, perpendicular_m=0.0):
    start = adapter.authorization.resolved_start
    end = adapter.authorization.resolved_end
    dx = end.x - start.x
    dy = end.y - start.y
    length = math.hypot(dx, dy)
    nx, ny = -dy / length, dx / length
    return Vec3(
        start.x + fraction * dx + perpendicular_m * 100.0 * nx,
        start.y + fraction * dy + perpendicular_m * 100.0 * ny,
        start.z + fraction * (end.z - start.z),
    )


class LiveRouteAdapterTests(unittest.TestCase):
    def test_precommand_capsule_hit_is_deferred_until_sustained_pulse_evidence(self):
        def observation(sequence):
            return make_observation(
                sequence=sequence,
                raw={
                    "navigation": {
                        "schema_version": 1,
                        "source": "ue4ss_line_trace_multi_height_capsule",
                        "base_sequence": sequence,
                        "directions": {
                            "forward": {
                                "sample_sequence": sequence,
                                "clear_distance_m": 1.0,
                                "capsule_probe": {
                                    "available": True,
                                    "fresh": True,
                                    "sample_sequence": sequence,
                                    "blocking_hit": True,
                                    "clear_distance_m": 1.0,
                                    "radius_m": 0.42,
                                },
                            }
                        },
                    }
                },
            )

        adapter, _, actuator, _ = make_adapter(
            observations=[observation(index) for index in range(1, 8)],
        )
        adapter.lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=2, maximum_attempts_per_plan=2)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        old_hash = adapter.authorization.route_plan_sha256

        result = adapter.run(dry_run=False, max_cycles=1)

        self.assertEqual(len(actuator.commands), 1)
        self.assertTrue(any(item.get("event") == "static_obstacle_precommand_blocker_deferred" for item in result["timeline"]))
        self.assertFalse(any(item.get("event") == "static_obstacle_replanned_and_reauthorised" for item in result["timeline"]))
        self.assertEqual(adapter.authorization.route_plan_sha256, old_hash)
        budget = adapter.lookahead._budgets[old_hash]
        self.assertLessEqual(budget.used_queries, 1)

    def test_later_route_lookahead_is_triggered_once_after_distance_and_remains_bounded(self):
        adapter, _, _, _ = make_adapter()
        self.assertTrue(adapter.preflight()["movement_authorized"])
        # The direct per-plan budget remains available to callers that disable
        # live rolling renewal.
        adapter.config = replace(adapter.config, static_lookahead_budget_window_seconds=None)
        adapter.lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=2, maximum_attempts_per_plan=2)
        steering = {"control_band": "normal", "desired_world_heading_deg": 0.0}
        initial = make_observation(sequence=10, monotonic_t=10.0, position=Vec3(0.0, 0.0, 0.0))
        later = make_observation(sequence=11, monotonic_t=11.0, position=Vec3(900.0, 0.0, 0.0))
        still_later = make_observation(sequence=12, monotonic_t=12.0, position=Vec3(950.0, 0.0, 0.0))
        self.assertEqual(adapter._precommand_static_obstacle_gate(initial, steering), "no_verified_blocker")
        self.assertEqual(adapter._precommand_static_obstacle_gate(later, steering), "no_verified_blocker")
        self.assertEqual(adapter._precommand_static_obstacle_gate(still_later, steering), "lookahead_budget_exhausted")
        status = adapter.lookahead.budget_status(adapter.authorization.route_plan_sha256)
        self.assertEqual(status["used_queries"], 0)
        self.assertEqual(status["used_attempts"], 2)
        events = [item for item in adapter.timeline if item.get("event") == "static_obstacle_precommand_lookahead"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["trigger"], "travelled_distance_threshold")

    def test_live_lookahead_renews_on_long_route_and_inspects_later_fresh_obstacle(self):
        def observation(sequence, position, *, blocked=False):
            return make_observation(
                sequence=sequence,
                monotonic_t=float(sequence),
                position=position,
                raw={"navigation": {
                    "schema_version": 1,
                    "source": "ue4ss_line_trace_multi_height_capsule",
                    "base_sequence": sequence,
                    "directions": {"forward": {
                        "sample_sequence": sequence,
                        "clear_distance_m": 1.0 if blocked else 5.0,
                        "capsule_probe": {
                            "available": True,
                            "fresh": True,
                            "sample_sequence": sequence,
                            "blocking_hit": blocked,
                            "clear_distance_m": 1.0 if blocked else 5.0,
                            "radius_m": 0.42,
                        },
                    }},
                }},
            )

        adapter, _, _, _ = make_adapter()
        self.assertTrue(adapter.preflight()["movement_authorized"])
        adapter.lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=2, maximum_attempts_per_plan=2)
        steering = {"control_band": "normal", "desired_world_heading_deg": 0.0}
        route_start = Vec3(0.0, 0.0, 0.0)
        self.assertEqual(adapter._precommand_static_obstacle_gate(
            observation(10, route_start), steering), "no_verified_blocker")
        self.assertEqual(adapter._precommand_static_obstacle_gate(
            observation(11, Vec3(900.0, 0.0, 0.0)), steering), "no_verified_blocker")
        self.assertEqual(adapter._precommand_static_obstacle_gate(
            observation(12, Vec3(1800.0, 0.0, 0.0), blocked=True), steering), "no_verified_blocker")

        later = [item for item in adapter.timeline
                 if item.get("event") == "static_obstacle_precommand_lookahead"][-1]
        self.assertEqual(later["sequence"], 12)
        self.assertTrue(later["budget_window_renewed"])
        self.assertEqual(later["reason"], "lookahead_capsule_blocked")
        self.assertEqual(later["decision"], ObstacleEvidenceState.READY.value)
        self.assertEqual(adapter.lookahead.budget_status(adapter.authorization.route_plan_sha256)["used_queries"], 1)

    def test_live_lookahead_rolling_budget_rejects_replayed_sidecar_sequence(self):
        def observation(observed_sequence, sidecar_sequence, position, *, blocked=False,
                        source="ue4ss_line_trace_multi_height_capsule"):
            return make_observation(
                sequence=observed_sequence,
                monotonic_t=float(observed_sequence),
                position=position,
                raw={"navigation": {
                    "schema_version": 1,
                    "source": source,
                    "base_sequence": sidecar_sequence,
                    "_ff7r_navigation_binding": {
                        "mode": "trusted_timestamp",
                        "source": source,
                        "observed_sequence": observed_sequence,
                        "sidecar_sequence": sidecar_sequence,
                    },
                    "directions": {"forward": {
                        "sample_sequence": sidecar_sequence,
                        "clear_distance_m": 1.0 if blocked else 5.0,
                        "capsule_probe": {
                            "available": True,
                            "fresh": True,
                            "sample_sequence": sidecar_sequence,
                            "blocking_hit": blocked,
                            "clear_distance_m": 1.0 if blocked else 5.0,
                            "radius_m": 0.42,
                        },
                    }},
                }},
            )

        adapter, _, _, _ = make_adapter()
        self.assertTrue(adapter.preflight()["movement_authorized"])
        adapter.lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=2, maximum_attempts_per_plan=2)
        steering = {"control_band": "normal", "desired_world_heading_deg": 0.0}
        self.assertEqual(adapter._precommand_static_obstacle_gate(
            observation(10, 10, Vec3(0.0, 0.0, 0.0)), steering), "no_verified_blocker")
        self.assertEqual(adapter._precommand_static_obstacle_gate(
            observation(11, 11, Vec3(900.0, 0.0, 0.0)), steering), "no_verified_blocker")
        self.assertEqual(adapter._precommand_static_obstacle_gate(
            observation(12, 11, Vec3(1800.0, 0.0, 0.0), blocked=True), steering), "no_verified_blocker")

        replay = [item for item in adapter.timeline
                  if item.get("event") == "static_obstacle_precommand_lookahead"][-1]
        self.assertEqual(replay["sequence"], 12)
        self.assertTrue(replay["budget_window_renewed"])
        self.assertEqual(replay["reason"], "lookahead_sidecar_sequence_reused")
        self.assertEqual(replay["budget"]["used_queries"], 0)

        self.assertEqual(adapter._precommand_static_obstacle_gate(
            observation(13, 12, Vec3(2700.0, 0.0, 0.0), blocked=True), steering), "no_verified_blocker")
        later = [item for item in adapter.timeline
                 if item.get("event") == "static_obstacle_precommand_lookahead"][-1]
        self.assertEqual(later["reason"], "lookahead_capsule_blocked")
        self.assertEqual(adapter.lookahead.budget_status(adapter.authorization.route_plan_sha256)["used_queries"], 1)

        self.assertEqual(adapter._precommand_static_obstacle_gate(
            observation(14, 999, Vec3(3600.0, 0.0, 0.0), source="untrusted_source"), steering),
            "no_verified_blocker")
        untrusted = [item for item in adapter.timeline
                     if item.get("event") == "static_obstacle_precommand_lookahead"][-1]
        self.assertEqual(untrusted["reason"], "lookahead_source_untrusted")
        self.assertEqual(adapter._precommand_static_obstacle_gate(
            observation(15, 13, Vec3(4500.0, 0.0, 0.0), blocked=True), steering),
            "no_verified_blocker")
        recovered = [item for item in adapter.timeline
                     if item.get("event") == "static_obstacle_precommand_lookahead"][-1]
        self.assertEqual(recovered["reason"], "lookahead_capsule_blocked")

    def test_later_route_lookahead_can_use_valid_sample_after_unusable_publication(self):
        def valid_observation(sequence, position):
            return make_observation(
                sequence=sequence,
                position=position,
                raw={
                    "navigation": {
                        "schema_version": 1,
                        "source": "ue4ss_line_trace_multi_height_capsule",
                        "base_sequence": sequence,
                        "directions": {
                            "forward": {
                                "sample_sequence": sequence,
                                "clear_distance_m": 5.0,
                                "capsule_probe": {
                                    "available": True,
                                    "fresh": True,
                                    "sample_sequence": sequence,
                                    "blocking_hit": False,
                                    "clear_distance_m": 5.0,
                                    "radius_m": 0.42,
                                },
                            },
                        },
                    },
                },
            )

        adapter, _, _, _ = make_adapter()
        self.assertTrue(adapter.preflight()["movement_authorized"])
        adapter.lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=1, maximum_attempts_per_plan=3)
        steering = {"control_band": "normal", "desired_world_heading_deg": 0.0}
        initial = make_observation(sequence=10, monotonic_t=10.0, position=Vec3(0.0, 0.0, 0.0), raw=None)
        later = valid_observation(11, Vec3(900.0, 0.0, 0.0))
        self.assertEqual(adapter._precommand_static_obstacle_gate(initial, steering), "no_verified_blocker")
        self.assertEqual(adapter._precommand_static_obstacle_gate(later, steering), "no_verified_blocker")
        status = adapter.lookahead.budget_status(adapter.authorization.route_plan_sha256)
        self.assertEqual(status["used_attempts"], 2)
        self.assertEqual(status["used_usable_queries"], 1)

    def test_heading_trigger_distinguishes_zero_none_and_wraparound(self):
        def gate(previous_heading, current_heading):
            adapter, _, _, _ = make_adapter()
            self.assertTrue(adapter.preflight()["movement_authorized"])
            adapter.lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=1, maximum_attempts_per_plan=2)
            anchor = make_observation(sequence=10, monotonic_t=10.0, position=Vec3(0.0, 0.0, 0.0))
            adapter._static_precommand_lookahead_checked = True
            adapter._static_lookahead_last_position = anchor.position
            adapter._static_lookahead_last_t = anchor.monotonic_t
            adapter._static_lookahead_last_heading = previous_heading
            adapter._static_lookahead_last_polygon_ref = adapter._route_local_corridor_ref or adapter._last_resolved_polygon_ref
            current = make_observation(sequence=11, monotonic_t=11.0, position=anchor.position)
            return adapter, adapter._precommand_static_obstacle_gate(
                current,
                {"control_band": "normal", "desired_world_heading_deg": current_heading},
            )

        turned, turned_result = gate(0.0, 90.0)
        assert turned_result == "no_verified_blocker"
        assert turned.timeline[-1]["trigger"] == "heading_change"

        _, within_threshold = gate(0.0, 30.0)
        assert within_threshold == "lookahead_not_due"

        _, wrapped = gate(350.0, 10.0)
        assert wrapped == "lookahead_not_due"

        _, none_previous = gate(None, 90.0)
        assert none_previous == "lookahead_not_due"

    def test_unverified_no_progress_is_retained_without_static_replan(self):
        observations = [make_observation(sequence=index) for index in range(1, 9)]
        adapter, _, actuator, _ = make_adapter(observations=observations)
        self.assertTrue(adapter.preflight()["movement_authorized"])

        result = adapter.run(dry_run=False, max_cycles=4)

        learning_events = [item for item in result["timeline"] if item.get("event") == "static_obstacle_learning"]
        # The default test adapter uses 100 ms pulses; these are below the
        # measured game-recognition floor and cannot become blocker evidence.
        self.assertEqual(learning_events, [])
        self.assertFalse(any(item.get("event") == "static_obstacle_settled_confirmation" for item in result["timeline"]))
        self.assertFalse(any(item.get("event") == "static_obstacle_replanned_and_reauthorised" for item in result["timeline"]))
        self.assertEqual(len([item for item in result["timeline"] if item.get("event") == "static_obstacle_short_pulse_not_evidence"]), 3)
        self.assertEqual(len(actuator.commands), 4)

    def test_static_obstacle_replan_gets_new_authorization_and_validator_proof(self):
        adapter, telemetry, actuator, _ = make_adapter()
        first_report = adapter.preflight()
        self.assertTrue(first_report["movement_authorized"])
        scope = adapter.static_obstacles.scope
        proof = adapter.authorization_proof
        self.assertIsNotNone(scope)
        self.assertIsNotNone(proof)
        learning_kwargs = dict(
            scope=scope,
            route_plan_sha256=proof.route_plan_sha256,
            route=proof.waypoints,
            position_before=adapter.authorization.resolved_start,
            position_after=adapter.authorization.resolved_start,
            along_route_before_m=0.0,
            along_route_after_m=0.0,
            commanded_bearing_degrees=0.0,
            command_duration_ms=250,
            telemetry_fresh=True,
        )
        adapter.static_obstacles.learn_from_movement(sequence=1, sampled_t=1.0, **learning_kwargs)
        adapter.static_obstacles.learn_from_movement(sequence=2, sampled_t=2.0, **learning_kwargs)
        learning = adapter.static_obstacles.learn_from_movement(sequence=3, sampled_t=3.0, **learning_kwargs)
        self.assertEqual(learning.state, ObstacleEvidenceState.READY)
        adapter._static_pending_discovery = {
            "obstacle": learning.obstacle.to_dict(),
            "reason_code": learning.reason_code,
            "sequence": 2,
            "route_plan_sha256": proof.route_plan_sha256,
        }
        old_plan_hash = adapter.authorization.route_plan_sha256
        trigger = adapter._last_observation
        self.assertIsNotNone(trigger)

        fresh = adapter._replan_for_static_obstacle(trigger)

        self.assertIsNotNone(fresh)
        self.assertNotEqual(adapter.authorization.route_plan_sha256, old_plan_hash)
        self.assertEqual(adapter.authorization_proof.route_plan_sha256, adapter.authorization.route_plan_sha256)
        self.assertEqual(
            adapter.authorization_proof.static_obstacle_evidence_sha256,
            adapter._static_obstacle_digest(),
        )
        self.assertEqual(len(adapter.validator.plans), 2)
        self.assertTrue(any(item.get("event") == "static_obstacle_replanned_and_reauthorised" for item in adapter.timeline))
        self.assertEqual(len(actuator.commands), 0)

    def test_cached_topology_route_uses_target_planner_and_generic_handoff(self):
        target = Vec3(500.0, 0.0, 0.0)
        adapter, _, _, ref = make_adapter(
            config=LiveAdapterConfig(
                route_mode="cached_topology",
                max_cycles=2,
                post_arrival_samples=0,
            ),
            route_target=target,
        )

        report = adapter.preflight()

        self.assertTrue(report["movement_authorized"])
        self.assertEqual(report["route_mode"], "cached_topology")
        self.assertEqual(adapter.route_plan["start_ref"], ref)
        self.assertEqual(adapter.route_plan["end_ref"], ref)
        self.assertEqual(adapter.route_plan["requested_end"], {"x": 500.0, "y": 0.0, "z": 0.0})
        self.assertEqual(adapter.route_plan["validation"]["tile_crossings"], 0)
        self.assertEqual(adapter.validator.plans[0], adapter.route_plan)
        self.assertTrue(any(item.get("event") == "cached_topology_route_planned" for item in adapter.timeline))

    @staticmethod
    def _route144_boundary_samples(adapter):
        source = 41873783652362
        target = 41839423913998
        previous = Vec3(-26159.992188, -225627.890625, 5492.62207)
        current = Vec3(-26219.814453, -225570.1875, 5505.842773)
        destination = Vec3(-25502.0, -225560.0, 5382.0)
        portal = {
            "from_ref": source,
            "to_ref": target,
            "link_ref": target,
            "source_edge": 2,
            "target_edge": 3,
            "left": {"x": -26220.0, "y": -225570.0, "z": 5420.0},
            "right": {"x": -25502.352941176472, "y": -225570.0, "z": 5381.725490196079},
            "side": 6,
        }
        adapter.route_plan = {
            "polygon_corridor": [source, target],
            "portals": [portal],
            "waypoints": [
                {"x": previous.x, "y": previous.y, "z": previous.z},
                {"x": current.x, "y": current.y, "z": current.z},
                {"x": destination.x, "y": destination.y, "z": destination.z},
            ],
        }
        adapter.authorization = PlanAuthorization(
            session_id="route-144-replay",
            world_id="EndGame",
            map_id="EndGame",
            topology_fingerprint="route-144-replay-topology",
            route_plan_sha256="route-144-replay-plan",
            start_ref=source,
            end_ref=target,
            resolved_start=previous,
            resolved_end=destination,
            created_monotonic=0.0,
            route_mode="ordinary_multi_tile",
        )
        adapter._route_local_corridor_index = 0
        adapter._route_local_corridor_ref = source
        adapter._route_local_last_position = previous
        adapter._route_local_last_sequence = 61063
        adapter._last_route_along_m = 0.0

        def candidate(reference, point, *, contained, horizontal, vertical, index):
            return PointResolutionCandidate(
                reference=reference,
                closest_point=point,
                horizontal_distance_m=horizontal,
                vertical_distance_m=vertical,
                contained=contained,
                ranking=(0.0 if contained else 1.0, vertical * 100.0, horizontal * 100.0),
                corridor_index=index,
            )

        previous_global = PointResolution(
            "resolved", "point_inside_polygon", previous, source, previous, 0.0, 0.7878811164, 2713,
            {"contained_candidate": True},
        )
        previous_corridor = CorridorPointResolution(
            "resolved", "inside_authorized_corridor", source, 0,
            (
                candidate(source, previous, contained=True, horizontal=0.0, vertical=0.7878811164, index=0),
                candidate(target, Vec3(previous.x, -225570.0, 5416.6662), contained=False, horizontal=0.5789, vertical=0.7596, index=1),
            ),
            previous_global,
        )
        current_global = PointResolution(
            "resolved", "point_inside_polygon", current, target, current, 0.0, 0.8585843831, 2731,
            {"contained_candidate": True},
        )
        current_corridor = CorridorPointResolution(
            "ambiguous", "authorized_shared_boundary_or_overlap", None, None,
            (
                candidate(target, current, contained=True, horizontal=0.0, vertical=0.8585843831, index=1),
                candidate(source, current, contained=True, horizontal=0.0, vertical=0.8586227410, index=0),
            ),
            current_global,
        )
        previous_sample = {
            "t": 100.0,
            "sequence": 61063,
            "position": {"x": previous.x, "y": previous.y, "z": previous.z},
            "corridor_resolution": previous_corridor,
            "metrics": adapter._authorized_route_metrics(previous),
        }
        current_sample = {
            "t": 100.5,
            "sequence": 61068,
            "position": {"x": current.x, "y": current.y, "z": current.z},
            "corridor_resolution": current_corridor,
            "metrics": adapter._authorized_route_metrics(current),
        }
        return previous_sample, current_sample, portal, source, target

    def test_route144_boundary_replay_proves_old_gate_would_stop_but_new_classifier_accepts(self):
        adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(route_mode="ordinary_multi_tile"))
        previous, current, portal, source, target = self._route144_boundary_samples(adapter)

        self.assertEqual(current["corridor_resolution"].status, "ambiguous")
        self.assertEqual(current["corridor_resolution"].reason_code, "authorized_shared_boundary_or_overlap")
        # This is the exact pre-fix gate that stopped route-144.
        self.assertTrue(current["corridor_resolution"].status != "resolved")

        assessment = adapter._route_local_sample_assessment(previous, current)

        self.assertTrue(assessment["safe"])
        self.assertEqual(assessment["reason_code"], "authorized_portal_transition_proven")
        self.assertEqual(assessment["accepted_index"], 1)
        transition = assessment["transition"]
        self.assertEqual(transition["event"], "authorized_portal_transition_proven")
        self.assertEqual(transition["from_ref"], source)
        self.assertEqual(transition["to_ref"], target)
        self.assertEqual(transition["previous_sequence"], 61063)
        self.assertEqual(transition["sequence"], 61068)
        self.assertTrue(transition["portal_crossing"]["intersects"])
        self.assertTrue(transition["portal_crossing"]["vertical_consistent"])
        self.assertEqual(transition["portal"]["side"], 6)

    def test_boundary_without_portal_intersection_remains_fail_closed(self):
        adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(route_mode="ordinary_multi_tile"))
        previous, current, portal, _, _ = self._route144_boundary_samples(adapter)
        current["position"] = {"x": -26219.814453, "y": -225580.1875, "z": 5505.842773}
        current["metrics"] = adapter._authorized_route_metrics(
            Vec3(current["position"]["x"], current["position"]["y"], current["position"]["z"])
        )
        assessment = adapter._route_local_sample_assessment(previous, current)
        self.assertFalse(assessment["safe"])
        self.assertEqual(assessment["reason_code"], "authorized_boundary_ambiguity_unproven")

    def test_recorded_narrow_portal_landing_uses_bounded_tolerance(self):
        previous = Vec3(-25345.382812, -223357.859375, 5473.446777)
        current = Vec3(-25368.027344, -223325.171875, 5454.77002)
        portal = {
            "left": {"x": -25380.0, "y": -223410.0, "z": 5460.0},
            "right": {"x": -25365.0, "y": -223350.0, "z": 5460.0},
        }

        strict = LiveRouteAdapter._portal_crossing_evidence(
            previous, current, portal, vertical_bound_m=1.0, tolerance_cm=5.0,
        )
        tolerant = LiveRouteAdapter._portal_crossing_evidence(
            previous, current, portal, vertical_bound_m=1.0, tolerance_cm=20.0,
        )

        self.assertFalse(strict["intersects"])
        self.assertTrue(tolerant["intersects"])
        self.assertEqual(tolerant["method"], "endpoint_tolerance")
        self.assertTrue(tolerant["vertical_consistent"])

    def test_recorded_transition_ignores_nearby_unoccupied_polygon(self):
        data = json.loads((Path(__file__).parent / "fixtures" / "nearby-polygon-transition.json").read_text(encoding="utf-8"))
        adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(route_mode="cached_topology"))
        plan = data["route_plan"]
        adapter.route_plan = plan
        adapter.authorization = PlanAuthorization(
            session_id="recorded-transition", world_id="EndGame", map_id="EndGame",
            topology_fingerprint="recorded-topology", route_plan_sha256="recorded-plan",
            start_ref=plan["start_ref"], end_ref=plan["end_ref"],
            resolved_start=Vec3(**plan["resolved_start"]), resolved_end=Vec3(**plan["resolved_end"]),
            created_monotonic=0.0, route_mode="cached_topology",
        )
        adapter._route_local_corridor_index = data["accepted_index"]
        adapter._route_local_corridor_ref = plan["polygon_corridor"][data["accepted_index"]]

        def sample(value):
            raw = value["corridor_resolution"]
            global_raw = raw["global_resolution"]
            global_resolution = PointResolution(
                global_raw["status"], global_raw["reason_code"], Vec3(**global_raw["requested_position"]),
                global_raw["selected_ref"], Vec3(**global_raw["closest_point"]),
                global_raw["horizontal_distance_m"], global_raw["vertical_distance_m"],
                global_raw["candidate_count"], global_raw["diagnostics"],
            )
            resolution = CorridorPointResolution(
                raw["status"], raw["reason_code"], raw["selected_ref"], raw["corridor_index"],
                tuple(PointResolutionCandidate(
                    item["ref"], Vec3(**item["closest_point"]), item["horizontal_distance_m"],
                    item["vertical_distance_m"], item["contained"], tuple(item["ranking"]), item["corridor_index"],
                ) for item in raw["candidates"]), global_resolution,
            )
            return {**value, "corridor_resolution": resolution,
                    "metrics": adapter._authorized_route_metrics(Vec3(**value["position"]))}

        previous, current = sample(data["previous"]), sample(data["current"])
        self.assertEqual(len(current["corridor_resolution"].candidates), 3)
        assessment = adapter._route_local_sample_assessment(previous, current)
        self.assertTrue(assessment["safe"], assessment)
        self.assertEqual(assessment["accepted_index"], 3)
        self.assertTrue(assessment["transition"]["portal_crossing"]["intersects"])
        # The same third candidate must remain blocking if it contains Cloud.
        resolution = current["corridor_resolution"]
        candidates = list(resolution.candidates)
        candidates[-1] = replace(candidates[-1], contained=True, horizontal_distance_m=0.0)
        current["corridor_resolution"] = replace(resolution, candidates=tuple(candidates))
        self.assertFalse(adapter._route_local_sample_assessment(previous, current)["safe"])

    def _recorded_skip_samples(self):
        data = json.loads((Path(__file__).parent / "fixtures" / "nearby-polygon-transition.json").read_text(encoding="utf-8"))
        adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(route_mode="cached_topology"))
        plan = data["route_plan"]
        adapter.route_plan = plan
        adapter.authorization = PlanAuthorization(
            session_id="recorded-transition", world_id="EndGame", map_id="EndGame",
            topology_fingerprint="recorded-topology", route_plan_sha256="recorded-plan",
            start_ref=plan["start_ref"], end_ref=plan["end_ref"],
            resolved_start=Vec3(**plan["resolved_start"]), resolved_end=Vec3(**plan["resolved_end"]),
            created_monotonic=0.0, route_mode="cached_topology",
        )
        # The accepted index lags two polygons behind Cloud (live at Kalm),
        # so the recorded crossing is a skip, not a single portal transition.
        adapter._route_local_corridor_index = data["accepted_index"] - 1
        adapter._route_local_corridor_ref = plan["polygon_corridor"][data["accepted_index"] - 1]

        def sample(value):
            raw = value["corridor_resolution"]
            global_raw = raw["global_resolution"]
            global_resolution = PointResolution(
                global_raw["status"], global_raw["reason_code"], Vec3(**global_raw["requested_position"]),
                global_raw["selected_ref"], Vec3(**global_raw["closest_point"]),
                global_raw["horizontal_distance_m"], global_raw["vertical_distance_m"],
                global_raw["candidate_count"], global_raw["diagnostics"],
            )
            resolution = CorridorPointResolution(
                raw["status"], raw["reason_code"], raw["selected_ref"], raw["corridor_index"],
                tuple(PointResolutionCandidate(
                    item["ref"], Vec3(**item["closest_point"]), item["horizontal_distance_m"],
                    item["vertical_distance_m"], item["contained"], tuple(item["ranking"]), item["corridor_index"],
                ) for item in raw["candidates"]), global_resolution,
            )
            return {**value, "corridor_resolution": resolution,
                    "metrics": adapter._authorized_route_metrics(Vec3(**value["position"]))}

        return adapter, sample(data["previous"]), sample(data["current"])

    def test_a_bounded_forward_skip_along_the_corridor_is_accepted(self):
        adapter, previous, current = self._recorded_skip_samples()
        assessment = adapter._route_local_sample_assessment(previous, current)
        self.assertTrue(assessment["safe"], assessment)
        self.assertEqual(assessment["reason_code"], "authorized_forward_corridor_skip")
        self.assertEqual(assessment["accepted_index"], adapter._route_local_corridor_index + 2)

    def test_a_skip_after_a_move_beyond_the_bound_fails_closed(self):
        # Fast-follow speed moved 2.5-3.4 m between samples (accepted); a move
        # past FORWARD_SKIP_MAX_MOVE_M is not accepted as a skip.
        adapter, previous, current = self._recorded_skip_samples()
        far = dict(previous["position"])
        far["x"] = current["position"]["x"] - 500.0
        far["y"] = current["position"]["y"]
        self.assertFalse(adapter._route_local_sample_assessment({**previous, "position": far}, current)["safe"])

    def test_a_skip_whose_progress_outruns_the_movement_fails_closed(self):
        # A folded route: a short move must not count as a long way along it.
        adapter, previous, current = self._recorded_skip_samples()
        current["metrics"] = {**current["metrics"], "along_route_m": current["metrics"]["along_route_m"] + 5.0}
        self.assertFalse(adapter._route_local_sample_assessment(previous, current)["safe"])

    def test_a_skip_with_backward_progress_fails_closed(self):
        adapter, previous, current = self._recorded_skip_samples()
        current["metrics"] = {**current["metrics"], "along_route_m": previous["metrics"]["along_route_m"] - 1.0}
        self.assertFalse(adapter._route_local_sample_assessment(previous, current)["safe"])

    def test_boundary_transition_ignores_noncontaining_proximity_candidates(self):
        adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(route_mode="ordinary_multi_tile"))
        previous, current, _, _, _ = self._route144_boundary_samples(adapter)
        resolution = current["corridor_resolution"]
        nearby = replace(resolution.candidates[0], reference=999, contained=False,
                         horizontal_distance_m=0.35, corridor_index=2)
        current["corridor_resolution"] = replace(resolution, candidates=(*resolution.candidates, nearby))
        assessment = adapter._route_local_sample_assessment(previous, current)
        self.assertTrue(assessment["safe"], assessment)
        adapter._commit_route_local_sample(current, assessment)
        continuity = adapter._route_local_sample_assessment(current, current)
        self.assertTrue(continuity["safe"], continuity)
        self.assertEqual(continuity["accepted_index"], 1)

    def test_route144_third_candidate_remains_fail_closed(self):
        adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(route_mode="ordinary_multi_tile"))
        previous, current, _, _, _ = self._route144_boundary_samples(adapter)
        resolution = current["corridor_resolution"]
        third = PointResolutionCandidate(
            reference=999,
            closest_point=Vec3(-26219.814453, -225570.1875, 5505.842773),
            horizontal_distance_m=0.1,
            vertical_distance_m=0.8,
            contained=True,
            ranking=(0.0, 80.0, 10.0),
            corridor_index=None,
        )
        current["corridor_resolution"] = CorridorPointResolution(
            resolution.status,
            resolution.reason_code,
            resolution.selected_reference,
            resolution.corridor_index,
            (*resolution.candidates, third),
            resolution.global_resolution,
        )
        assessment = adapter._route_local_sample_assessment(previous, current)
        self.assertFalse(assessment["safe"])
        self.assertEqual(assessment["reason_code"], "authorized_boundary_ambiguity_unproven")

    def test_proven_transition_keeps_route_local_target_through_global_source_tiebreak(self):
        adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(route_mode="ordinary_multi_tile"))
        previous, current, _, source, target = self._route144_boundary_samples(adapter)
        assessment = adapter._route_local_sample_assessment(previous, current)
        self.assertTrue(assessment["safe"])
        adapter._commit_route_local_sample(current, assessment)
        source_candidate = current["corridor_resolution"].candidates[1]
        target_candidate = current["corridor_resolution"].candidates[0]
        current["corridor_resolution"] = CorridorPointResolution(
            "ambiguous", "authorized_shared_boundary_or_overlap", None, None,
            (source_candidate, target_candidate),
            PointResolution(
                "resolved", "point_inside_polygon", Vec3(-26219.5, -225570.1, 5505.8),
                source, Vec3(-26219.5, -225570.1, 5419.98), 0.0, 0.858, 2731,
                {"contained_candidate": True},
            ),
        )
        current["metrics"] = dict(current["metrics"])
        current["metrics"]["along_route_m"] = float(current["metrics"]["along_route_m"]) + 0.01
        continuity = adapter._route_local_sample_assessment(current, current)
        self.assertTrue(continuity["safe"])
        self.assertEqual(continuity["reason_code"], "authorized_corridor_overlap_continuity")
        self.assertEqual(adapter._route_local_corridor_ref, target)

    def test_post_command_boundary_ambiguity_gets_one_neutral_fresh_retry(self):
        clock = FakeClock(10.0)
        first = make_observation(sequence=10, monotonic_t=10.0)
        second = make_observation(sequence=11, monotonic_t=10.0)
        adapter, telemetry, actuator, _ = make_adapter(
            observations=[second],
            clock=clock,
            config=LiveAdapterConfig(route_mode="ordinary_multi_tile"),
        )
        adapter.validator_result = {"valid": True, "errors": [], "warnings": []}
        adapter._last_corridor_sample = {"sequence": 9}
        ambiguous_sample = {"sequence": 10}
        safe_sample = {"sequence": 11}
        with patch.object(adapter, "_check_preconditions", return_value=[]), \
             patch.object(adapter, "_same_authorization", return_value=True), \
             patch.object(adapter, "_verify_authorization_proof", return_value=True), \
             patch.object(adapter, "_route_polygon_sample", side_effect=[ambiguous_sample, safe_sample]), \
             patch.object(adapter, "_route_local_sample_assessment", side_effect=[
                 {"safe": False, "reason_code": "authorized_boundary_ambiguity_unproven"},
                 {"safe": True, "reason_code": "authorized_portal_transition_proven"},
             ]):
            result = adapter._ensure_fresh_following_observation(first)
        self.assertEqual(result.sequence, 11)
        self.assertEqual(telemetry.index, 1)
        self.assertTrue(actuator.is_neutral())
        self.assertIs(adapter._active_post_command_route_sample, safe_sample)
        retries = [item for item in adapter.timeline if item.get("event") == "post_command_boundary_ambiguity_retry"]
        self.assertEqual(len(retries), 1)
        self.assertFalse(retries[0]["movement_command_sent"])

    def test_post_command_persistent_boundary_ambiguity_retries_only_once_then_replans(self):
        clock = FakeClock(10.0)
        first = make_observation(sequence=20, monotonic_t=10.0)
        second = make_observation(sequence=21, monotonic_t=10.0)
        adapter, telemetry, actuator, _ = make_adapter(
            observations=[second],
            clock=clock,
            config=LiveAdapterConfig(route_mode="ordinary_multi_tile"),
        )
        adapter.validator_result = {"valid": True, "errors": [], "warnings": []}
        adapter._last_corridor_sample = {"sequence": 19}
        with patch.object(adapter, "_check_preconditions", return_value=[]), \
             patch.object(adapter, "_same_authorization", return_value=True), \
             patch.object(adapter, "_verify_authorization_proof", return_value=True), \
             patch.object(adapter, "_route_polygon_sample", side_effect=[{"sequence": 20}, {"sequence": 21}]), \
             patch.object(adapter, "_route_local_sample_assessment", side_effect=[
                 {"safe": False, "reason_code": "authorized_boundary_ambiguity_unproven"},
                 {"safe": False, "reason_code": "authorized_boundary_ambiguity_unproven"},
             ]):
            with self.assertRaisesRegex(
                AdapterError,
                "post_command_corridor_reacquisition_failed:authorized_boundary_ambiguity_unproven",
            ):
                adapter._ensure_fresh_following_observation(first)
        self.assertEqual(telemetry.index, 1)
        self.assertTrue(actuator.is_neutral())
        self.assertEqual(sum(
            item.get("event") == "post_command_boundary_ambiguity_retry"
            for item in adapter.timeline
        ), 1)

    def test_post_command_membership_departure_still_replans_without_retry(self):
        clock = FakeClock(10.0)
        first = make_observation(sequence=30, monotonic_t=10.0)
        second = make_observation(sequence=31, monotonic_t=10.0)
        adapter, telemetry, _, _ = make_adapter(
            observations=[second],
            clock=clock,
            config=LiveAdapterConfig(route_mode="ordinary_multi_tile"),
        )
        adapter.validator_result = {"valid": True, "errors": [], "warnings": []}
        adapter._last_corridor_sample = {"sequence": 29}
        with patch.object(adapter, "_check_preconditions", return_value=[]), \
             patch.object(adapter, "_same_authorization", return_value=True), \
             patch.object(adapter, "_verify_authorization_proof", return_value=True), \
             patch.object(adapter, "_route_polygon_sample", return_value={"sequence": 30}), \
             patch.object(adapter, "_route_local_sample_assessment", return_value={
                 "safe": False, "reason_code": "corridor_membership_not_authorized"
             }):
            with self.assertRaisesRegex(
                AdapterError,
                "post_command_corridor_reacquisition_failed:corridor_membership_not_authorized",
            ):
                adapter._ensure_fresh_following_observation(first)
        self.assertEqual(telemetry.index, 0)
        self.assertFalse(any(
            item.get("event") == "post_command_boundary_ambiguity_retry"
            for item in adapter.timeline
        ))

    def test_route144_post_command_path_accepts_boundary_proof(self):
        adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(route_mode="ordinary_multi_tile"))
        previous, current, _, _, _ = self._route144_boundary_samples(adapter)
        adapter.topology_session = adapter.topology.topology
        adapter._last_corridor_sample = previous
        adapter._last_resolved_polygon_ref = previous["corridor_resolution"].selected_reference
        adapter._pending_command_evidence = {
            "sequence": 61063,
            "position_before": previous["position"],
            "command_completed_t": 100.0,
            "distance_to_final_before_m": 3.9,
            "along_route_before_m": previous["metrics"]["along_route_m"],
            "route_progress_before": 0.5,
            "cross_track_before_m": previous["metrics"]["cross_track_m"],
            "control_band": "approach",
            "magnitude": 0.5,
            "command_duration_ms": 250,
            "suggested_speed_band": "slow",
        }
        with patch.object(adapter, "_route_polygon_sample", return_value=current):
            adapter._record_post_command_observation(
                make_observation(
                    sequence=61068,
                    monotonic_t=100.5,
                    position=Vec3(
                        current["position"]["x"],
                        current["position"]["y"],
                        current["position"]["z"],
                    ),
                )
            )
        self.assertTrue(any(
            item.get("event") == "authorized_portal_transition_proven"
            for item in adapter.timeline
        ))
        self.assertEqual(adapter._route_local_corridor_index, 1)

    def test_corridor_divergence_classification_keeps_portal_and_unsafe_cases_distinct(self):
        adapter, _, _, _ = make_adapter()
        self.assertTrue(adapter.preflight()["movement_authorized"])
        pending = {
            "position_before": {"x": 0.0, "y": 0.0, "z": 0.0},
            "desired_world_heading_deg": 0.0,
        }
        current = {
            "position": {"x": 0.0, "y": 100.0, "z": 0.0},
            "t": 11.0,
        }

        portal = adapter._classify_corridor_divergence(
            {"safe": True, "reason_code": "authorized_portal_transition_proven"},
            current,
            pending,
        )
        bounded = adapter._classify_corridor_divergence(
            {"safe": True, "reason_code": "inside_authorized_corridor"},
            current,
            pending,
        )
        probable = adapter._classify_corridor_divergence(
            {"safe": False, "reason_code": "corridor_membership_not_authorized"},
            current,
            pending,
        )
        unexplained = adapter._classify_corridor_divergence(
            {"safe": False, "reason_code": "corridor_membership_not_authorized"},
            {"position": {"x": 0.0, "y": 100.0, "z": 0.0}, "t": 11.0},
            {"position_before": {"x": 0.0, "y": 0.0, "z": 0.0}},
        )

        self.assertEqual(portal["classification"], CorridorDivergenceClass.AUTHORIZED_PORTAL_TRANSITION.value)
        self.assertEqual(bounded["classification"], CorridorDivergenceClass.BOUNDED_CORRIDOR_REACQUISITION.value)
        self.assertEqual(probable["classification"], CorridorDivergenceClass.PROBABLE_COLLISION_DIVERGENCE.value)
        self.assertEqual(unexplained["classification"], CorridorDivergenceClass.UNEXPLAINED_UNAUTHORIZED_DIVERGENCE.value)

    def test_provisional_static_handoff_uses_neutral_fresh_capsule_confirmation(self):
        def blocked(sequence):
            return make_observation(
                sequence=sequence,
                monotonic_t=10.0 + float(sequence),
                dynamic_obstacles=DynamicObstacleSnapshot(
                    base_sequence=sequence,
                    sampled_at_ms=sequence * 1000,
                    observations=(),
                    source_identity="test-dynamic-snapshot",
                    complete=True,
                ),
                raw={
                    "navigation": {
                        "schema_version": 1,
                        "source": "ue4ss_line_trace_multi_height_capsule",
                        "base_sequence": sequence,
                        "directions": {
                            "forward": {
                                "sample_sequence": sequence,
                                "clear_distance_m": 1.0,
                                "capsule_probe": {
                                    "available": True,
                                    "fresh": True,
                                    "sample_sequence": sequence,
                                    "blocking_hit": True,
                                    "clear_distance_m": 1.0,
                                    "radius_m": 0.42,
                                },
                            },
                        },
                    },
                },
            )

        adapter, telemetry, actuator, _ = make_adapter(
            observations=[make_observation(sequence=1), blocked(2), blocked(3)],
        )
        preflight = adapter.preflight()
        self.assertTrue(preflight["movement_authorized"], preflight)
        adapter.lookahead = NavigationSidecarLookahead(maximum_queries_per_plan=2, maximum_attempts_per_plan=2)
        adapter._last_observation = make_observation(sequence=1)
        pending = {
            "position_before": {"x": 0.0, "y": 0.0, "z": 0.0},
            "desired_world_heading_deg": 0.0,
            "command_duration_ms": 250,
        }
        confirmed = adapter._confirm_static_obstacle_after_divergence(
            blocked(2),
            pending,
            {"classification": CorridorDivergenceClass.PROVISIONAL_STATIC_HANDOFF.value},
        )

        self.assertTrue(confirmed)
        self.assertEqual(len(actuator.commands), 0)
        self.assertTrue(actuator.is_neutral())
        self.assertEqual(telemetry.index, 3)
        self.assertIsNotNone(adapter._static_pending_discovery)
        self.assertEqual(
            adapter._static_pending_discovery["reason_code"],
            "static_obstacle_confirmed_after_corridor_divergence",
        )
        confirmation_events = [
            item for item in adapter.timeline
            if item.get("event") == "corridor_divergence_confirmation_sample"
        ]
        self.assertEqual([item["sequence"] for item in confirmation_events], [2, 3])
        self.assertTrue(all(item["movement_command_sent"] is False for item in confirmation_events))
    def _selector_plan(self, ref):
        start = Vec3(0.0, 0.0, 0.0)
        end = Vec3(50.0, 0.0, 0.0)
        resolution = make_graph()[0].resolve_point(end, max_snap_distance_m=0.75, max_vertical_separation_m=1.0)
        plan = {
            "schema_version": 1,
            "source": "shipping_detour",
            "executable_sha256": "37D952FABBFA2B710F8AE6490997C9755E4E57B4FD545078C9BAAF46C9B233D7",
            "status": "complete",
            "reason_code": "route_plan_complete",
            "requested_start": {"x": start.x, "y": start.y, "z": start.z},
            "requested_end": {"x": end.x, "y": end.y, "z": end.z},
            "resolved_start": {"x": start.x, "y": start.y, "z": start.z},
            "resolved_end": {"x": end.x, "y": end.y, "z": end.z},
            "start_ref": ref,
            "end_ref": ref,
            "polygon_corridor": [ref, ref],
            "portals": [],
            "waypoints": [
                {"x": start.x, "y": start.y, "z": start.z},
                {"x": end.x, "y": end.y, "z": end.z},
            ],
            "total_estimated_path_length_m": 0.5,
            "start_resolution": resolution.to_dict(),
            "end_resolution": resolution.to_dict(),
            "validation": {"tile_crossings": 0},
            "diagnostics": {},
            "movement_sent": False,
        }
        return end, resolution, SimpleNamespace(end_ref=ref, to_dict=lambda: plan), plan

    def _assert_selector_plan_is_bound(self, route_mode, selector_name):
        adapter, _, _, ref = make_adapter(config=LiveAdapterConfig(route_mode=route_mode, max_cycles=2, post_arrival_samples=0))
        destination, resolution, plan_object, plan_mapping = self._selector_plan(ref)
        selector_result = (destination, resolution, plan_object, [{"end_ref": ref, "selected": True, "rejection": None}])
        validator = adapter.validator
        graph = adapter.topology.topology.graph
        with patch(f"ff7r_agent.live_route_adapter.{selector_name}", return_value=selector_result):
            with patch.object(graph, "plan", side_effect=AssertionError("unrestricted replan")):
                with patch.object(adapter, "_validate_first_route", return_value=None):
                    report = adapter.preflight()
        self.assertTrue(report["movement_authorized"])
        self.assertIs(validator.plans[0], adapter.route_plan)
        self.assertEqual(report["selected_route_plan_sha256"], report["adapter_route_plan_sha256"])
        self.assertEqual(report["adapter_route_plan_sha256"], report["independent_validator_route_plan_sha256"])
        self.assertEqual(adapter.route_plan, plan_mapping)
        self.assertEqual(adapter.route_plan["requested_start"], {"x": 0.0, "y": 0.0, "z": 0.0})

    def test_multi_tile_selector_plan_is_authoritative_and_no_second_plan(self):
        self._assert_selector_plan_is_bound("ordinary_multi_tile", "select_ordinary_multi_tile_destination")

    def test_multi_polygon_selector_plan_is_authoritative_and_no_second_plan(self):
        self._assert_selector_plan_is_bound("ordinary_multi_polygon", "select_ordinary_multi_polygon_destination")

    def test_route49_two_crossing_selected_plan_cannot_be_replaced_by_unrestricted_alternate(self):
        adapter, _, _, _ = make_adapter(
            config=LiveAdapterConfig(route_mode="ordinary_multi_tile", max_cycles=2, post_arrival_samples=0)
        )
        start_ref = 41873783652362
        end_ref = 72355166552086
        corridor = [
            41873783652362, 41873783652361, 41873783652356,
            72350871584773, 72350871584772, 72355166552066,
            72355166552069, 72355166552068, 72355166552067,
            72355166552075, 72355166552072, 72355166552093,
            72355166552088, 72355166552087, 72355166552090,
            72355166552092, 72355166552091, 72355166552086,
        ]
        destination = Vec3(50.0, 0.0, 0.0)
        start_resolution = SimpleNamespace(
            selected_reference=start_ref,
            ok=True,
            reason_code="point_inside_polygon",
            horizontal_distance_m=0.0,
            vertical_distance_m=0.0,
            closest_point=Vec3(0.0, 0.0, 0.0),
            to_dict=lambda: {"selected_ref": start_ref, "horizontal_distance_m": 0.0, "vertical_distance_m": 0.0},
        )
        end_resolution = SimpleNamespace(
            selected_reference=end_ref,
            ok=True,
            reason_code="point_inside_polygon",
            horizontal_distance_m=0.0,
            vertical_distance_m=0.0,
            closest_point=destination,
            to_dict=lambda: {"selected_ref": end_ref, "horizontal_distance_m": 0.0, "vertical_distance_m": 0.0},
        )
        selected_plan = {
            "schema_version": 1,
            "source": "shipping_detour",
            "executable_sha256": "37D952FABBFA2B710F8AE6490997C9755E4E57B4FD545078C9BAAF46C9B233D7",
            "status": "complete",
            "reason_code": "route_plan_complete",
            "requested_start": {"x": 0.0, "y": 0.0, "z": 0.0},
            "requested_end": {"x": 50.0, "y": 0.0, "z": 0.0},
            "resolved_start": {"x": 0.0, "y": 0.0, "z": 0.0},
            "resolved_end": {"x": 50.0, "y": 0.0, "z": 0.0},
            "start_ref": start_ref,
            "end_ref": end_ref,
            "polygon_corridor": corridor,
            "portals": [],
            "waypoints": [{"x": 0.0, "y": 0.0, "z": 0.0}, {"x": 50.0, "y": 0.0, "z": 0.0}],
            "total_estimated_path_length_m": 33.257514,
            "start_resolution": {"selected_ref": start_ref, "horizontal_distance_m": 0.0, "vertical_distance_m": 0.0},
            "end_resolution": {"selected_ref": end_ref, "horizontal_distance_m": 0.0, "vertical_distance_m": 0.0},
            "validation": {"tile_crossings": 2},
            "diagnostics": {"route49_replay": "constrained_candidate"},
            "movement_sent": False,
        }
        plan_object = SimpleNamespace(end_ref=end_ref, to_dict=lambda: selected_plan)
        selector_result = (destination, end_resolution, plan_object, [{"end_ref": end_ref, "selected": True, "rejection": None}])
        graph = adapter.topology.topology.graph
        with patch("ff7r_agent.live_route_adapter.select_ordinary_multi_tile_destination", return_value=selector_result):
            with patch.object(graph, "resolve_point", return_value=start_resolution):
                with patch.object(graph, "plan", side_effect=AssertionError("unrestricted four-crossing alternate must not run")):
                    with patch.object(adapter, "_validate_first_route", return_value=None):
                        report = adapter.preflight()
        self.assertTrue(report["movement_authorized"])
        self.assertEqual(adapter.route_plan["polygon_corridor"], corridor)
        self.assertEqual(adapter.route_plan["validation"]["tile_crossings"], 2)
        self.assertEqual(report["selected_route_plan_sha256"], report["adapter_route_plan_sha256"])
        self.assertEqual(report["adapter_route_plan_sha256"], report["independent_validator_route_plan_sha256"])

    def test_selected_plan_mutation_fails_closed_with_identity_mismatch(self):
        adapter, _, _, ref = make_adapter(config=LiveAdapterConfig(route_mode="ordinary_multi_tile"))
        destination, resolution, plan_object, _ = self._selector_plan(ref)
        with patch("ff7r_agent.live_route_adapter.select_ordinary_multi_tile_destination", return_value=(destination, resolution, plan_object, [])):
            selected = adapter._choose_destination(adapter.topology.topology, make_observation(), ref)
        selected.plan["diagnostics"] = {"mutated": True}
        adapter.start_resolution = adapter.topology.topology.graph.resolve_point(make_observation().position)
        with self.assertRaisesRegex(AdapterError, "selected_route_plan_identity_mismatch"):
            adapter._bind_selected_route_plan(selected, adapter.topology.topology, make_observation(), ref)

    def test_preflight_resets_selected_plan_before_new_selection(self):
        adapter, _, _, ref = make_adapter(config=LiveAdapterConfig(route_mode="ordinary_multi_tile", max_cycles=2, post_arrival_samples=0))
        first = self._selector_plan(ref)
        second = self._selector_plan(ref)
        with patch("ff7r_agent.live_route_adapter.select_ordinary_multi_tile_destination", side_effect=[
            (first[0], first[1], first[2], [{"end_ref": ref, "selected": True, "rejection": None}]),
            (second[0], second[1], second[2], [{"end_ref": ref, "selected": True, "rejection": None}]),
        ]):
            with patch.object(adapter, "_validate_first_route", return_value=None):
                first_report = adapter.preflight()
                first_selection_id = adapter._selected_route.selection_id
                second_report = adapter.preflight()
                second_selection_id = adapter._selected_route.selection_id
        self.assertTrue(first_report["movement_authorized"])
        self.assertTrue(second_report["movement_authorized"])
        self.assertNotEqual(first_selection_id, second_selection_id)

    def test_generic_simulator_config_retains_legacy_defaults_outside_live_boundary(self):
        config = LiveAdapterConfig()
        self.assertEqual(config.normal_stick_magnitude, 0.35)
        self.assertEqual(config.control_period_seconds, 0.1)
        self.assertEqual(config.waypoint_vertical_tolerance_m, 1.5)

    def test_measured_feet_drive_navigation_and_airborne_readiness(self):
        player = Character.from_dict(json.loads(
            (Path(__file__).parent / "fixtures/measured-capsule-pose.json").read_text(encoding="utf-8")))
        state = _AgeState(0.1)
        state.player = player
        source = AgentServiceTelemetrySource(_AgeService(state), _AgeLifecycle(), monotonic=lambda: 500.0)
        observation = source.observe()
        self.assertEqual(observation.position, player.locomotion.feet_position)
        self.assertEqual(observation.actor_origin, player.position)
        self.assertEqual(observation.position_reference, "upright_capsule_bottom")
        self.assertAlmostEqual(observation.actor_origin.z - observation.position.z, 88.0)
        self.assertTrue(observation.controllable)
        self.assertTrue(observation.grounded)
        state.player = replace(player, locomotion=replace(player.locomotion, grounded=False))
        airborne = source.observe()
        self.assertFalse(airborne.controllable)
        self.assertTrue(airborne.telemetry_gameplay_ready)
        self.assertFalse(airborne.grounded)

    def test_authorized_route_rejects_changed_position_reference(self):
        adapter, _, _, _ = make_adapter()
        self.assertTrue(adapter.preflight()["movement_authorized"])
        adapter.telemetry = FakeTelemetry([replace(make_observation(sequence=10),
            position_reference="upright_capsule_bottom")])
        with self.assertRaisesRegex(AdapterError, "position_reference_changed"):
            adapter._require_observation()

    def test_agent_service_source_preserves_underlying_telemetry_age(self):
        observation = AgentServiceTelemetrySource(
            _AgeService(_AgeState(1.25)),
            _AgeLifecycle(),
            monotonic=lambda: 500.0,
        ).observe()
        self.assertEqual(observation.monotonic_t, 500.0)
        self.assertEqual(observation.telemetry_age_seconds, 1.25)
        self.assertEqual(observation.sequence, 12)
        self.assertEqual(observation.timestamp_utc, "2026-08-18T00:00:00Z")

    def test_agent_service_source_publishes_typed_dynamic_snapshot_at_adapter_boundary(self):
        observation = AgentServiceTelemetrySource(
            _AgeService(_AgeState(0.1)),
            _AgeLifecycle(),
            monotonic=lambda: 500.0,
        ).observe()
        self.assertIsInstance(observation.dynamic_obstacles, DynamicObstacleSnapshot)
        self.assertEqual(observation.dynamic_obstacles.source_identity, "agent_service.telemetry.nearby_actors")
        self.assertNotIn("dynamic_obstacle_evidence", observation.raw)

    def test_attached_identity_is_verified_only_after_render_gate(self):
        expected = "37D952FABBFA2B710F8AE6490997C9755E4E57B4FD545078C9BAAF46C9B233D7"
        initial = make_observation(executable_valid=False, executable_sha256=None)
        telemetry = FakeIdentityTelemetry(
            [initial, replace(initial, sequence=2)],
            {"verified": True, "sha256": expected},
        )
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry)
        report = adapter.preflight()
        self.assertTrue(report["movement_authorized"])
        self.assertEqual(telemetry.identity_calls, 1)
        self.assertEqual(report["observation"]["executable_sha256"], expected)
        self.assertEqual(actuator.commands, [])

    def test_attached_identity_mismatch_refuses_before_topology(self):
        telemetry = FakeIdentityTelemetry(
            [make_observation(executable_valid=False, executable_sha256=None)],
            {"verified": False, "sha256": "0" * 64},
        )
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry)
        report = adapter.preflight()
        self.assertEqual(report["reason"], "executable_identity_invalid")
        self.assertIn("executable_identity_invalid", report["failures"])
        self.assertEqual(adapter.topology.arm_calls, 0)
        self.assertEqual(actuator.commands, [])

    def test_attached_identity_is_not_hashed_before_positive_render(self):
        telemetry = FakeIdentityTelemetry(
            [make_observation(executable_valid=False, executable_sha256=None)],
            {"verified": True, "sha256": "37D952FABBFA2B710F8AE6490997C9755E4E57B4FD545078C9BAAF46C9B233D7"},
        )
        adapter, _, actuator, _ = make_adapter(
            telemetry=telemetry,
            render=FakeRenderEvidence(False, "render_classifier_non_gameplay"),
        )
        report = adapter.preflight()
        self.assertEqual(report["reason"], "gameplay_readiness_disagreement")
        self.assertEqual(telemetry.identity_calls, 0)
        self.assertEqual(actuator.commands, [])

    def test_unknown_bright_render_never_authorizes_gameplay(self):
        source = AgentServiceRenderEvidenceSource(FakeCaptureService({
            "ok": True,
            "pid": 123,
            "captured_at_utc": "2026-08-18T00:00:00Z",
            "startup_frame": {
                "label": "unknown",
                "metrics": {"whole_nonblack": 1.0},
            },
        }))
        for _ in range(2):
            evidence = source.observe(make_observation())
            self.assertFalse(evidence.ready)
            self.assertEqual(evidence.reason, "render_classifier_non_gameplay")
            self.assertEqual(evidence.classifier_label, "unknown")

    def test_gameplay_label_with_black_frame_never_authorizes(self):
        source = AgentServiceRenderEvidenceSource(FakeCaptureService({
            "ok": True,
            "pid": 123,
            "startup_frame": {"label": "gameplay", "metrics": {"whole_nonblack": 0.0}},
        }))
        evidence = source.observe(make_observation())
        self.assertFalse(evidence.ready)
        self.assertEqual(evidence.reason, "render_black_or_loading")

    def test_gameplay_label_with_sufficient_render_and_bound_pid_authorizes(self):
        source = AgentServiceRenderEvidenceSource(FakeCaptureService({
            "ok": True,
            "pid": 123,
            "startup_frame": {"label": "gameplay", "metrics": {"whole_nonblack": 0.75}},
        }))
        evidence = source.observe(make_observation())
        self.assertTrue(evidence.ready)
        self.assertEqual(evidence.reason, "rendered_gameplay_confirmed")

    def test_gameplay_label_with_wrong_pid_never_authorizes(self):
        source = AgentServiceRenderEvidenceSource(FakeCaptureService({
            "ok": True,
            "pid": 999,
            "startup_frame": {"label": "gameplay", "metrics": {"whole_nonblack": 0.75}},
        }))
        evidence = source.observe(make_observation())
        self.assertFalse(evidence.ready)
        self.assertEqual(evidence.reason, "render_process_identity_mismatch")

    def test_telemetry_override_without_bound_capture_proof_stays_fail_closed(self):
        for raw_label, metrics in (("unknown", {"whole_nonblack": 1.0}), ("black", {"whole_nonblack": 1.0})):
            source = AgentServiceRenderEvidenceSource(FakeCaptureService({
                "ok": True,
                "pid": 123,
                "startup_frame": {
                    "label": "gameplay",
                    "render_classifier_label": raw_label,
                    "telemetry_override": "fresh_loaded_gameplay",
                    "metrics": metrics,
                },
            }))
            evidence = source.observe(make_observation())
            self.assertFalse(evidence.ready)
            self.assertEqual(evidence.classifier_label, raw_label)

    def test_fresh_loaded_gameplay_override_arms_only_after_both_gates(self):
        """A fully bound service override resolves the known raw false negative."""
        render = AgentServiceRenderEvidenceSource(FakeCaptureService({
            "ok": True,
            "pid": 123,
            "hwnd": 456,
            "telemetry_sequence": 25336,
            "map_id": "EndGame",
            "telemetry_age_seconds": 0.13217,
            "captured_at_utc": "2026-08-27T14:55:13.225853Z",
            "startup_frame": {
                "label": "gameplay",
                "render_classifier_label": "unknown",
                "telemetry_override": "fresh_loaded_gameplay",
                "metrics": {"whole_nonblack": 0.884},
            },
        }))
        adapter, _, actuator, _ = make_adapter(render=render)

        report = adapter.preflight()

        self.assertTrue(report["observation"]["telemetry_gameplay_ready"])
        self.assertTrue(report["movement_authorized"])
        self.assertEqual(report["render_confirmation"]["reason"], "telemetry_backed_gameplay_confirmed")
        self.assertEqual(report["render_confirmation"]["classifier_label"], "unknown")
        self.assertEqual(
            report["render_confirmation"]["capture"]["startup_frame"]["label"],
            "gameplay",
        )
        self.assertEqual(
            report["render_confirmation"]["capture"]["startup_frame"]["telemetry_override"],
            "fresh_loaded_gameplay",
        )
        self.assertEqual(adapter.topology.arm_calls, 1)
        self.assertEqual(adapter.topology.load_calls, 1)
        self.assertEqual(actuator.commands, [])

    def test_fresh_loaded_gameplay_override_bridges_lazy_attached_identity(self):
        """Render proof must precede the deliberately lazy executable hash gate."""
        expected = LIVE_EXECUTABLE_SHA256
        initial = make_observation(executable_valid=False, executable_sha256=None)
        telemetry = FakeIdentityTelemetry(
            [initial, replace(initial, sequence=2)],
            {"verified": True, "sha256": expected},
        )
        render = AgentServiceRenderEvidenceSource(FakeCaptureService(
            make_telemetry_backed_capture(),
        ))
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry, render=render)

        report = adapter.preflight()

        self.assertTrue(report["movement_authorized"])
        self.assertEqual(report["render_confirmation"]["reason"], "telemetry_backed_gameplay_confirmed")
        self.assertEqual(telemetry.identity_calls, 1)
        self.assertEqual(report["observation"]["executable_sha256"], expected)
        self.assertEqual(adapter.topology.arm_calls, 1)
        self.assertEqual(adapter.topology.load_calls, 1)
        self.assertEqual(actuator.commands, [])

    def test_fresh_loaded_override_stays_fail_closed_for_unsafe_telemetry(self):
        for changes in (
            {"loading": True, "controllable": False},
            {"in_menu": True},
            {"in_cutscene": True},
            {"in_combat": True, "controllable": False},
            {"process_valid": False},
        ):
            with self.subTest(changes=changes):
                source = AgentServiceRenderEvidenceSource(FakeCaptureService(
                    make_telemetry_backed_capture(),
                ))
                evidence = source.observe(make_observation(
                    executable_valid=False,
                    executable_sha256=None,
                    **changes,
                ))

                self.assertFalse(evidence.ready)
                self.assertEqual(evidence.reason, "render_classifier_non_gameplay")

    def test_telemetry_backed_unknown_render_rejects_stale_proof(self):
        cases = (
            (make_telemetry_backed_capture(telemetry_age_seconds=2.0001), make_observation()),
            (make_telemetry_backed_capture(), make_observation(telemetry_age_seconds=0.5001)),
        )
        for capture, observation in cases:
            with self.subTest(
                capture_age=capture["telemetry_age_seconds"],
                observation_age=observation.telemetry_age_seconds,
            ):
                source = AgentServiceRenderEvidenceSource(FakeCaptureService(capture))
                evidence = source.observe(observation)
                self.assertFalse(evidence.ready)
                self.assertEqual(evidence.reason, "render_classifier_non_gameplay")

    def test_telemetry_backed_render_accepts_a_capture_a_second_behind_on_a_busy_machine(self):
        # Live 2026-09-30 at Gongaga: the capture's telemetry was 1.09 s old.
        source = AgentServiceRenderEvidenceSource(
            FakeCaptureService(make_telemetry_backed_capture(telemetry_age_seconds=1.09)))
        self.assertTrue(source.observe(make_observation()).ready)

    def test_telemetry_backed_unknown_render_rejects_map_or_render_proof_disagreement(self):
        cases = (
            {"map_id": "DifferentMap"},
            {"telemetry_override": "other_override"},
            {"raw_label": "black"},
            {"raw_label": "main_menu"},
            {"raw_label": "title_prompt"},
            {"whole_nonblack": 0.0},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                source = AgentServiceRenderEvidenceSource(FakeCaptureService(
                    make_telemetry_backed_capture(**changes),
                ))
                evidence = source.observe(make_observation())
                self.assertFalse(evidence.ready)
                self.assertEqual(evidence.classifier_label, changes.get("raw_label", "unknown"))
                self.assertEqual(evidence.reason, "render_classifier_non_gameplay")

    def test_telemetry_render_disagreement_is_fail_closed_before_topology(self):
        render = FakeRenderEvidence(False, "render_black_or_loading")
        adapter, _, actuator, _ = make_adapter(render=render)
        report = adapter.preflight()
        self.assertEqual(report["reason"], "gameplay_readiness_disagreement")
        self.assertIn("gameplay_readiness_disagreement", report["failures"])
        self.assertFalse(report["movement_authorized"])
        self.assertIsNone(report["topology"])
        self.assertEqual(adapter.topology.load_calls, 0)
        self.assertEqual(adapter.topology.arm_calls, 0)
        self.assertEqual(actuator.commands, [])

    def test_render_confirmation_arms_topology_only_after_both_gates(self):
        adapter, _, _, _ = make_adapter()
        report = adapter.preflight()
        self.assertTrue(report["movement_authorized"])
        self.assertEqual(adapter.topology.arm_calls, 1)
        self.assertEqual(adapter.topology.load_calls, 1)

    def test_post_render_reacquires_settled_gameplay_after_cache_expiry(self):
        telemetry = FakeSettlingTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=3),
        ])
        adapter, _, _, _ = make_adapter(telemetry=telemetry)

        report = adapter.preflight()

        self.assertTrue(report["movement_authorized"])
        self.assertEqual(telemetry.settle_calls, [(30.0, 0.5)])
        self.assertEqual(adapter.topology.arm_calls, 1)
        self.assertTrue(any(
            event.get("event") == "post_render_settled_gameplay_reacquired"
            for event in report["timeline"]
        ))

    def test_post_render_settling_failure_remains_fail_closed_before_topology(self):
        telemetry = FakeSettlingTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2, telemetry_gameplay_ready=False, loading=True, controllable=False),
        ], error=RuntimeError("still loading"))
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry)

        report = adapter.preflight()

        self.assertEqual(report["reason"], "telemetry_gameplay_not_ready")
        self.assertFalse(report["movement_authorized"])
        self.assertEqual(adapter.topology.arm_calls, 0)
        self.assertEqual(adapter.topology.load_calls, 0)
        self.assertEqual(actuator.commands, [])

    def test_unknown_bright_disagreement_stops_before_topology_and_validator(self):
        render = FakeRenderEvidence(False, "render_classifier_non_gameplay")
        adapter, _, actuator, _ = make_adapter(render=render)
        report = adapter.preflight()
        self.assertEqual(report["reason"], "gameplay_readiness_disagreement")
        self.assertFalse(report["movement_authorized"])
        self.assertIsNone(report["topology"])
        self.assertEqual(adapter.topology.arm_calls, 0)
        self.assertEqual(adapter.topology.load_calls, 0)
        self.assertEqual(actuator.commands, [])

    def test_independent_validator_stays_lazy_before_topology_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            validator = IndependentJsonValidator(Path(directory) / "snapshot.json")
            self.assertEqual(validator.timing, {})

    def test_unexpected_validator_file_error_is_reported_with_stage_and_artifact(self):
        class FailingBindingValidator(FakeValidator):
            def bind_topology_session(self, topology):
                raise FileNotFoundError("[Errno 2] missing validator snapshot: 'legacy.json'")

        adapter, _, _, _ = make_adapter(validator=FailingBindingValidator())

        report = adapter.preflight()

        self.assertFalse(report["movement_authorized"])
        self.assertEqual(report["reason"], "preflight_exception:FileNotFoundError")
        self.assertEqual(report["exception_diagnostics"]["exception_type"], "FileNotFoundError")
        self.assertIn("legacy.json", report["exception_diagnostics"]["message"])
        self.assertEqual(report["exception_diagnostics"]["stage"], "validator_topology_binding")
        self.assertEqual(report["exception_diagnostics"]["artifact_path"], "test-topology.json")

    def test_post_command_timing_reports_separate_bounded_stages(self):
        clock = FakeClock()
        adapter, _, actuator, _ = make_adapter(clock=clock)

        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(len(actuator.commands), 2)
        timing_events = [
            item for item in result["timeline"]
            if item.get("event") == "post_command_stage_timing"
            and item.get("outcome") == "pre_command_authorized"
        ]
        self.assertEqual(len(timing_events), 1)
        stages = timing_events[0]["stages_ms"]
        for name in (
            "telemetry_parse_read_ms",
            "effective_age_computation_ms",
            "precondition_verification_ms",
            "readiness_verification_ms",
            "session_lease_identity_ms",
            "authorization_identity_ms",
            "static_plan_validation_ms",
            "independent_validator_related_checks_ms",
            "controller_decision_ms",
            "controller_steering_ms",
            "final_freshness_gate_ms",
        ):
            self.assertIn(name, stages)
            self.assertGreaterEqual(stages[name], 0.0)
        self.assertGreaterEqual(timing_events[0]["wall_clock_total_ms"], 0.0)


class JsonTopologyProviderTests(unittest.TestCase):
    def _render(self):
        return RenderedGameplayEvidence(
            ready=True,
            reason="rendered_gameplay_confirmed",
            classifier_label="gameplay",
            metrics={"whole_nonblack": 1.0},
            capture={"ok": True, "pid": 123},
        )

    def test_preexisting_artifact_times_out_and_markers_are_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / name for name in ("geometry.json", "probe.json", "enable", "ready", "timing.json", "status.json")}
            paths["geometry.json"].write_text("stale", encoding="utf-8")
            paths["probe.json"].write_text(json.dumps({"native_geometry_dump_written": True}), encoding="utf-8")
            paths["timing.json"].write_text(json.dumps({"probe_attempt": 4}), encoding="utf-8")
            clock = FakeClock(time.time())
            provider = JsonTopologyProvider(
                paths["geometry.json"],
                probe_path=paths["probe.json"],
                enable_path=paths["enable"],
                readiness_path=paths["ready"],
                timing_path=paths["timing.json"],
                status_path=paths["status.json"],
                clock=clock,
                sleeper=lambda seconds: setattr(clock, "value", clock.value + seconds),
                max_capture_wait_seconds=1.0,
                capture_poll_seconds=0.25,
            )
            provider.arm_capture(make_observation(), self._render())
            self.assertTrue(paths["enable"].is_file())
            self.assertTrue(paths["ready"].is_file())
            with self.assertRaisesRegex(AdapterError, "capture_request_not_accepted"):
                provider.load_current(make_observation())
            self.assertFalse(paths["enable"].exists())
            self.assertFalse(paths["ready"].exists())

    def test_measured_native_duration_fits_default_bounded_wait(self):
        self.assertGreaterEqual(DEFAULT_MAX_CAPTURE_WAIT_SECONDS, 62.800912)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / name for name in ("geometry.json", "probe.json", "enable", "ready", "timing.json", "status.json")}
            paths["geometry.json"].write_text(json.dumps({"source": "stale"}), encoding="utf-8")
            paths["probe.json"].write_text(json.dumps({"native_geometry_dump_written": False}), encoding="utf-8")
            paths["timing.json"].write_text(json.dumps({"probe_attempt": 10}), encoding="utf-8")
            clock = FakeClock(time.time())
            observation = make_observation()
            fresh_payload = {
                "source": "ue4ss_native_detour_geometry_dump",
                "status": "complete",
                "movement_sent": False,
                "coordinate_space": "recast",
                "player_position": {"x": 0.0, "y": 0.0, "z": 0.0},
            }
            fresh_probe = {
                "request_id": "PLACEHOLDER",
                "world": observation.world_id,
                "executable_identity": {"sha256": observation.executable_sha256},
                "native_geometry_requested": True,
                "native_geometry_marker_consumed": True,
                "native_geometry_dump_written": True,
            }
            fresh_timing = {
                "request_id": "PLACEHOLDER",
                "probe_attempt": 11,
                "geometry_armed": True,
                "geometry_dump_written": True,
            }

            def native_write(_seconds):
                fresh_payload["request_id"] = request_id
                fresh_probe["request_id"] = request_id
                fresh_timing["request_id"] = request_id
                paths["geometry.json"].write_text(json.dumps(fresh_payload), encoding="utf-8")
                paths["probe.json"].write_text(json.dumps(fresh_probe), encoding="utf-8")
                paths["timing.json"].write_text(json.dumps(fresh_timing), encoding="utf-8")
                paths["status.json"].write_text(json.dumps({"request_id": request_id, "state": "completed"}), encoding="utf-8")
                # Model the measured 62.8 s native operation without sleeping.
                clock.value += 62.800912

            provider = JsonTopologyProvider(
                paths["geometry.json"],
                probe_path=paths["probe.json"],
                enable_path=paths["enable"],
                readiness_path=paths["ready"],
                timing_path=paths["timing.json"],
                status_path=paths["status.json"],
                clock=clock,
                sleeper=native_write,
                max_capture_age_seconds=120.0,
                capture_poll_seconds=0.25,
            )
            graph, _ = make_graph()
            provider.arm_capture(observation, self._render())
            request_id = provider.request_id
            with patch("ff7r_agent.live_route_adapter.load_shipping_detour_topology", return_value=graph):
                topology = provider.load_current(observation)
            self.assertTrue(topology.current_session)
            self.assertEqual(
                topology.timings["python_topology_capture_wait_budget_seconds"],
                DEFAULT_MAX_CAPTURE_WAIT_SECONDS,
            )
            self.assertFalse(paths["enable"].exists())
            self.assertFalse(paths["ready"].exists())

    def test_new_native_result_is_required_before_graph_reconstruction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / name for name in ("geometry.json", "probe.json", "enable", "ready", "timing.json", "status.json")}
            stale_payload = {"source": "stale"}
            paths["geometry.json"].write_text(json.dumps(stale_payload), encoding="utf-8")
            paths["probe.json"].write_text(json.dumps({"native_geometry_dump_written": False}), encoding="utf-8")
            paths["timing.json"].write_text(json.dumps({"probe_attempt": 4}), encoding="utf-8")
            clock = FakeClock(time.time())
            observation = make_observation()
            fresh_payload = {
                "source": "ue4ss_native_detour_geometry_dump",
                "status": "complete",
                "movement_sent": False,
                "coordinate_space": "recast",
                "player_position": {"x": 0.0, "y": 0.0, "z": 0.0},
            }
            fresh_probe = {
                "request_id": "PLACEHOLDER",
                "world": observation.world_id,
                "executable_identity": {"sha256": observation.executable_sha256},
                "native_geometry_requested": True,
                "native_geometry_marker_consumed": True,
                "native_geometry_dump_written": True,
            }
            fresh_timing = {
                "request_id": "PLACEHOLDER",
                "probe_attempt": 5,
                "geometry_armed": True,
                "geometry_dump_written": True,
            }

            def native_write(_seconds):
                fresh_payload["request_id"] = request_id
                fresh_probe["request_id"] = request_id
                fresh_timing["request_id"] = request_id
                paths["geometry.json"].write_text(json.dumps(fresh_payload), encoding="utf-8")
                paths["probe.json"].write_text(json.dumps(fresh_probe), encoding="utf-8")
                paths["timing.json"].write_text(json.dumps(fresh_timing), encoding="utf-8")
                paths["status.json"].write_text(json.dumps({"request_id": request_id, "state": "completed"}), encoding="utf-8")
                clock.value += 0.25

            provider = JsonTopologyProvider(
                paths["geometry.json"],
                probe_path=paths["probe.json"],
                enable_path=paths["enable"],
                readiness_path=paths["ready"],
                timing_path=paths["timing.json"],
                status_path=paths["status.json"],
                clock=clock,
                sleeper=native_write,
                max_capture_wait_seconds=1.0,
                capture_poll_seconds=0.25,
            )
            graph, _ = make_graph()
            provider.arm_capture(observation, self._render())
            request_id = provider.request_id
            with patch("ff7r_agent.live_route_adapter.load_shipping_detour_topology", return_value=graph) as load_graph:
                topology = provider.load_current(observation)
            load_graph.assert_called_once()
            self.assertTrue(topology.current_session)
            self.assertIn("python_topology_capture_wait_ms", topology.timings)
            self.assertFalse(paths["enable"].exists())
            self.assertFalse(paths["ready"].exists())

    def test_new_native_result_accepts_process_local_attempt_counter_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / name for name in ("geometry.json", "probe.json", "enable", "ready", "timing.json", "status.json")}
            paths["geometry.json"].write_text(json.dumps({"source": "stale"}), encoding="utf-8")
            paths["probe.json"].write_text(json.dumps({"native_geometry_dump_written": False}), encoding="utf-8")
            paths["timing.json"].write_text(json.dumps({"probe_attempt": 1}), encoding="utf-8")
            clock = FakeClock(time.time())
            observation = make_observation()
            fresh_payload = {
                "source": "ue4ss_native_detour_geometry_dump",
                "status": "complete",
                "movement_sent": False,
                "coordinate_space": "recast",
                "player_position": {"x": 0.0, "y": 0.0, "z": 0.0},
            }
            fresh_probe = {
                "request_id": "PLACEHOLDER",
                "world": observation.world_id,
                "executable_identity": {"sha256": observation.executable_sha256},
                "native_geometry_requested": True,
                "native_geometry_marker_consumed": True,
                "native_geometry_dump_written": True,
            }
            fresh_timing = {
                "request_id": "PLACEHOLDER",
                "probe_attempt": 1,
                "geometry_armed": True,
                "geometry_dump_written": True,
            }

            def native_write(_seconds):
                fresh_payload["request_id"] = request_id
                fresh_probe["request_id"] = request_id
                fresh_timing["request_id"] = request_id
                paths["geometry.json"].write_text(json.dumps(fresh_payload), encoding="utf-8")
                paths["probe.json"].write_text(json.dumps(fresh_probe), encoding="utf-8")
                paths["timing.json"].write_text(json.dumps(fresh_timing), encoding="utf-8")
                paths["status.json"].write_text(json.dumps({"request_id": request_id, "state": "completed"}), encoding="utf-8")
                clock.value += 0.25

            provider = JsonTopologyProvider(
                paths["geometry.json"],
                probe_path=paths["probe.json"],
                enable_path=paths["enable"],
                readiness_path=paths["ready"],
                timing_path=paths["timing.json"],
                status_path=paths["status.json"],
                clock=clock,
                sleeper=native_write,
                max_capture_wait_seconds=1.0,
                capture_poll_seconds=0.25,
            )
            graph, _ = make_graph()
            provider.arm_capture(observation, self._render())
            request_id = provider.request_id
            with patch("ff7r_agent.live_route_adapter.load_shipping_detour_topology", return_value=graph):
                topology = provider.load_current(observation)
            self.assertTrue(topology.current_session)
            self.assertFalse(paths["enable"].exists())
            self.assertFalse(paths["ready"].exists())

    def test_no_movement_when_disarmed(self):
        adapter, _, actuator, _ = make_adapter(observations=[make_observation(lease_active=False)])
        preflight = adapter.preflight()
        self.assertFalse(preflight["movement_authorized"])
        with self.assertRaises(Exception):
            adapter.run(dry_run=False)
        self.assertEqual(actuator.commands, [])

    def test_no_movement_before_validator_acceptance(self):
        validator = FakeValidator({"valid": False, "errors": ["bad"], "warnings": []})
        adapter, _, actuator, _ = make_adapter(validator=validator)
        preflight = adapter.preflight()
        self.assertEqual(preflight["state"], AdapterState.DISARMED.value)
        self.assertEqual(actuator.commands, [])

    def test_validator_error_no_movement(self):
        self.test_no_movement_before_validator_acceptance()

    def test_stale_telemetry_neutral(self):
        adapter, _, actuator, _ = make_adapter(observations=[make_observation(telemetry_age_seconds=9.0)])
        report = adapter.preflight()
        self.assertIn("telemetry_stale", report["failures"])
        self.assertEqual(actuator.commands, [])

    def test_preflight_waits_for_fresh_advancing_source_sample(self):
        stale = make_observation(sequence=4, telemetry_age_seconds=1.25)
        fresh = make_observation(sequence=5, telemetry_age_seconds=0.25)
        post_render = make_observation(sequence=6, telemetry_age_seconds=0.20)
        clock = FakeClock()
        adapter, _, actuator, _ = make_adapter(
            observations=[stale, fresh, post_render],
            clock=clock,
        )
        report = adapter.preflight()
        self.assertTrue(report["movement_authorized"])
        self.assertEqual(report["observation"]["sequence"], 6)
        self.assertEqual(actuator.commands, [])
        events = [item for item in report["timeline"] if item.get("event") == "fresh_telemetry_observation_acquired"]
        self.assertEqual([item["sequence"] for item in events], [5])

    def test_post_render_requires_advancing_fresh_source_sample(self):
        first = make_observation(sequence=10)
        repeated = make_observation(sequence=10)
        advanced = make_observation(sequence=11)
        adapter, _, actuator, _ = make_adapter(observations=[first, repeated, advanced])
        report = adapter.preflight()
        self.assertTrue(report["movement_authorized"])
        self.assertEqual(report["observation"]["sequence"], 11)
        self.assertEqual(actuator.commands, [])

    def test_source_age_at_limit_is_accepted(self):
        adapter, _, actuator, _ = make_adapter(observations=[make_observation(telemetry_age_seconds=0.5)])
        report = adapter.preflight()
        self.assertTrue(report["movement_authorized"])
        self.assertEqual(report["observation"]["telemetry_age_seconds"], 0.5)
        self.assertEqual(actuator.commands, [])

    def test_repeated_sequence_cannot_send_a_second_control_cycle(self):
        sample = make_observation(sequence=7)
        adapter, _, actuator, _ = make_adapter(
            observations=[sample, make_observation(sequence=8), make_observation(sequence=9), make_observation(sequence=9)]
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=True, max_cycles=3)
        self.assertEqual(result["reason"], "telemetry_advancement_timeout")
        self.assertEqual(result["movement_cycles"], 0)
        self.assertEqual(actuator.commands, [])
        cycles = [item for item in result["timeline"] if item.get("event") == "control_cycle"]
        self.assertEqual(len(cycles), 1)
        self.assertFalse(any(item.get("event") == "readiness_loss_triggered" for item in result["timeline"]))
        self.assertTrue(any(item.get("event") == "waiting_for_next_telemetry" for item in result["timeline"]))

    def test_advancing_sequence_allows_the_next_control_cycle(self):
        first = make_observation(sequence=7)
        second = make_observation(sequence=8)
        third = make_observation(sequence=9)
        adapter, _, actuator, _ = make_adapter(observations=[first, second, third])
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=True, max_cycles=1)
        self.assertNotEqual(result["reason"], "telemetry_not_advancing")
        self.assertEqual(len([item for item in result["timeline"] if item.get("event") == "control_cycle"]), 1)
        self.assertEqual(actuator.commands, [])

    def test_stale_telemetry_during_following_neutralizes_immediately(self):
        first = make_observation(sequence=7)
        post_render = make_observation(sequence=8)
        stale = make_observation(sequence=9, telemetry_age_seconds=1.25)
        adapter, _, actuator, _ = make_adapter(observations=[first, post_render, stale])
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=1)
        self.assertEqual(result["state"], AdapterState.STOPPED.value)
        self.assertEqual(result["reason"], "telemetry_stale")
        self.assertEqual(actuator.commands, [])

    def test_non_finite_telemetry_neutral(self):
        adapter, _, actuator, _ = make_adapter(observations=[make_observation(position=Vec3(math.nan, 0.0, 0.0))])
        report = adapter.preflight()
        self.assertIn("telemetry_non_finite", report["failures"])
        self.assertEqual(actuator.commands, [])

    def test_invalidation_reasons_neutral(self):
        for reason in ("topology_changed", "plan_stale", "polygon_ref_invalid", "world_changed"):
            adapter, _, actuator, _ = make_adapter()
            self.assertTrue(adapter.preflight()["movement_authorized"])
            result = adapter.invalidate_plan(reason)
            self.assertEqual(result["state"], AdapterState.REPLAN_REQUIRED.value)
            self.assertEqual(actuator.commands, [])

    def test_follower_terminal_states_neutral(self):
        for mode in ("STALLED", "DEVIATED", "REPLAN_REQUIRED", "FAILED", "ARRIVED"):
            adapter, _, actuator, _ = make_adapter(follower_mode=mode)
            self.assertTrue(adapter.preflight()["movement_authorized"])
            result = adapter.run(dry_run=False, max_cycles=1)
            self.assertEqual(actuator.commands, [])
        self.assertNotEqual(result["state"], AdapterState.FOLLOWING.value)

    def test_final_pre_movement_gate_reacquires_fresh_state(self):
        adapter, _, actuator, _ = make_adapter(observations=[
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4),
        ])
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=1)
        self.assertEqual(len(actuator.commands), 1)
        self.assertEqual(result["movement_cycles"], 1)
        gate = next(item for item in result["timeline"] if item.get("event") == "final_pre_movement_gate_passed")
        self.assertEqual(gate["sequence"], 3)
        imminent = next(item for item in result["timeline"] if item.get("event") == "non_neutral_command_imminent")
        self.assertLessEqual(imminent["telemetry_age_seconds"], 0.5)
        self.assertLessEqual(imminent["effective_telemetry_age_seconds"], 0.5)

    def test_effective_pre_command_age_reacquires_after_gate_work(self):
        clock = FakeClock()
        adapter, _, _, _ = make_adapter(clock=clock, observations=[
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4),
        ])
        self.assertTrue(adapter.preflight()["movement_authorized"])
        gated = adapter._final_pre_movement_gate()
        clock.value = gated.monotonic_t + 0.8
        self.assertGreater(adapter._effective_telemetry_age_seconds(gated), 0.5)
        adapter.telemetry.observations[3] = replace(
            adapter.telemetry.observations[3],
            monotonic_t=clock.value,
        )
        fresh = adapter._ensure_fresh_command_observation(gated)
        self.assertEqual(fresh.sequence, 4)
        self.assertEqual(fresh.position, gated.position)
        reacquired = next(
            event for event in adapter.timeline
            if event.get("event") == "final_command_freshness_reacquired"
        )
        self.assertEqual(reacquired["previous_sequence"], 3)
        self.assertEqual(reacquired["sequence"], 4)
        self.assertLessEqual(reacquired["effective_telemetry_age_seconds"], 0.5)

    def test_stale_effective_age_never_sends_non_neutral_command(self):
        clock = FakeClock()
        adapter, _, actuator, _ = make_adapter(clock=clock)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        original_follower_event = adapter._follower_event

        def delayed_follower_event(observation):
            clock.value += 0.8
            return original_follower_event(observation)

        adapter._follower_event = delayed_follower_event
        result = adapter.run(dry_run=False, max_cycles=1)
        self.assertEqual(result["reason"], "telemetry_stale_immediately_before_command")
        self.assertEqual(actuator.commands, [])
        stale = next(
            event for event in result["timeline"]
            if event.get("event") == "telemetry_stale_immediately_before_command"
        )
        self.assertGreater(stale["effective_telemetry_age_seconds"], 0.5)

    def test_route31_like_final_command_edge_reacquires_unchanged_fresh_sample(self):
        """A near-limit sample must be replaced after local gate work, not aged in place."""
        clock = FakeClock()
        adapter, _, actuator, _ = make_adapter(
            clock=clock,
            observations=[
                make_observation(sequence=1),
                make_observation(sequence=2),
                make_observation(sequence=3, telemetry_age_seconds=0.4432094),
                make_observation(sequence=4, monotonic_t=10.07828),
            ],
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])
        original_follower_event = adapter._follower_event

        def delayed_final_gate_follower_event(observation):
            if observation.sequence == 3:
                clock.value += 0.07828
            return original_follower_event(observation)

        adapter._follower_event = delayed_final_gate_follower_event
        result = adapter.run(dry_run=False, max_cycles=1)

        self.assertEqual(result["movement_cycles"], 1)
        self.assertEqual(len(actuator.commands), 1)
        self.assertFalse(any(
            item.get("event") == "telemetry_stale_immediately_before_command"
            for item in result["timeline"]
        ))
        imminent = [
            item for item in result["timeline"]
            if item.get("event") == "non_neutral_command_imminent"
        ]
        self.assertEqual([item["sequence"] for item in imminent], [4])
        self.assertLessEqual(imminent[0]["effective_telemetry_age_seconds"], 0.5)
        self.assertTrue(any(
            item.get("event") == "final_command_freshness_reacquired"
            and item.get("sequence") == 4
            for item in result["timeline"]
        ))

    def test_late_steering_hitch_recovers_residual_motion_before_resuming(self):
        clock = FakeClock()
        adapter, _, actuator, _ = make_adapter(
            clock=clock,
            observations=[
                make_observation(sequence=1),
                make_observation(sequence=2),
                make_observation(sequence=3),
                make_observation(sequence=4, monotonic_t=10.0, position=Vec3(20.0, 0.0, 0.0)),
                make_observation(sequence=5, monotonic_t=10.8, telemetry_gameplay_ready=False,
                                 controllable=False, position=Vec3(25.0, 0.0, 0.0)),
                make_observation(sequence=6, monotonic_t=10.9, position=Vec3(30.0, 0.0, 0.0)),
                make_observation(sequence=7, monotonic_t=11.0, position=Vec3(35.0, 0.0, 0.0)),
            ],
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])
        original_steering = adapter._steering

        def slow_second_cycle(observation, event):
            result = original_steering(observation, event)
            if observation.sequence == 4:
                clock.value += 0.8
            return result

        adapter._steering = slow_second_cycle
        result = adapter.run(dry_run=False, max_cycles=3)

        commands = [item for item in result["timeline"]
                    if item.get("event") == "non_neutral_command_imminent"]
        self.assertEqual([item["sequence"] for item in commands], [3, 7])
        self.assertEqual(len(actuator.commands), 2)
        self.assertTrue(any(
            item.get("event") == "readiness_loss_triggered"
            and item.get("trigger_sequence") == 4
            for item in result["timeline"]
        ))
        self.assertTrue(any(
            item.get("event") == "readiness_recovery_sample"
            and item.get("observation", {}).get("sequence") == 6
            for item in result["timeline"]
        ))
        self.assertTrue(all(item["sequence"] != 4 and item["sequence"] != 5 for item in commands))
        self.assertTrue(adapter.actuator.is_neutral())

    def test_late_steering_hitch_identity_change_stops_without_reusing_sample(self):
        clock = FakeClock()
        adapter, _, actuator, _ = make_adapter(
            clock=clock,
            observations=[
                make_observation(sequence=1),
                make_observation(sequence=2),
                make_observation(sequence=3),
                make_observation(sequence=4, monotonic_t=10.0, position=Vec3(20.0, 0.0, 0.0)),
                make_observation(sequence=5, monotonic_t=10.8, session_id="other-session",
                                 position=Vec3(25.0, 0.0, 0.0)),
            ],
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])
        original_steering = adapter._steering

        def slow_second_cycle(observation, event):
            result = original_steering(observation, event)
            if observation.sequence == 4:
                clock.value += 0.8
            return result

        adapter._steering = slow_second_cycle
        result = adapter.run(dry_run=False, max_cycles=3)

        commands = [item for item in result["timeline"]
                    if item.get("event") == "non_neutral_command_imminent"]
        self.assertEqual([item["sequence"] for item in commands], [3])
        self.assertEqual(len(actuator.commands), 1)
        self.assertIn("session_changed", result["reason"])
        self.assertFalse(any(item.get("event") == "readiness_reacquired_route_state"
                             for item in result["timeline"]))
        self.assertTrue(adapter.actuator.is_neutral())

    def test_post_command_effective_staleness_reacquires_while_neutral(self):
        clock = FakeClock()
        adapter, telemetry, actuator, _ = make_adapter(
            clock=clock,
            observations=[
                make_observation(sequence=1),
                make_observation(sequence=2),
                make_observation(sequence=3),
                make_observation(sequence=4, monotonic_t=10.0),
                make_observation(sequence=5, monotonic_t=10.8, position=Vec3(1.0, 0.0, 0.0)),
            ],
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])
        original_follower_event = adapter._follower_event

        def delayed_post_command_follower_event(observation):
            if observation.sequence == 4:
                clock.value += 0.8
            return original_follower_event(observation)

        adapter._follower_event = delayed_post_command_follower_event
        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(result["movement_cycles"], 2)
        self.assertEqual(len(actuator.commands), 2)
        commands = [
            item for item in result["timeline"]
            if item.get("event") == "non_neutral_command_imminent"
        ]
        self.assertEqual([item["sequence"] for item in commands], [3, 5])
        self.assertTrue(any(
            item.get("event") == "post_command_freshness_reacquired"
            and item.get("previous_sequence") == 4
            and item.get("sequence") == 5
            for item in result["timeline"]
        ))
        self.assertTrue(all(
            item.get("actuator_neutral") is True
            for item in result["timeline"]
            if item.get("event") == "post_command_freshness_reacquired"
        ))
        self.assertEqual(telemetry.index, 5)

    def test_following_cycle_allows_residual_motion_when_preloop_sample_is_effectively_stale(self):
        """A post-pulse sample may move before the next command gate reacquires it."""
        clock = FakeClock()
        adapter, telemetry, actuator, _ = make_adapter(
            clock=clock,
            observations=[
                make_observation(sequence=1),
                make_observation(sequence=2),
                make_observation(sequence=3),
                # The source timestamp is already outside the effective age
                # bound when the next following cycle begins, even though the
                # telemetry-reported age is still fresh.
                make_observation(sequence=4, monotonic_t=10.0),
                make_observation(sequence=5, monotonic_t=10.8, position=Vec3(1.0, 0.0, 0.0)),
            ],
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])

        original_observe = telemetry.observe
        def delayed_observe():
            observation = original_observe()
            if observation.sequence == 4:
                clock.value += 0.8
            return observation
        telemetry.observe = delayed_observe

        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(result["movement_cycles"], 2)
        self.assertEqual(len(actuator.commands), 2)
        imminent = [
            item for item in result["timeline"]
            if item.get("event") == "non_neutral_command_imminent"
        ]
        self.assertEqual([item["sequence"] for item in imminent], [3, 5])
        self.assertTrue(any(
            item.get("event") == "post_command_freshness_reacquired"
            and item.get("previous_sequence") == 4
            and item.get("sequence") == 5
            for item in result["timeline"]
        ))
        self.assertEqual(telemetry.index, 5)
        self.assertTrue(adapter.actuator.is_neutral())

    def test_post_command_reacquires_again_if_corridor_validation_consumes_freshness(self):
        """A fresh post-pulse sample must remain fresh through route checks."""
        clock = FakeClock()
        adapter, telemetry, actuator, _ = make_adapter(
            clock=clock,
            observations=[
                make_observation(sequence=1),
                make_observation(sequence=2),
                make_observation(sequence=3),
                make_observation(sequence=4, monotonic_t=10.0),
                make_observation(sequence=5, monotonic_t=10.8, position=Vec3(1.0, 0.0, 0.0)),
                make_observation(sequence=6, monotonic_t=11.35, position=Vec3(1.2, 0.0, 0.0)),
            ],
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])
        adapter.config = replace(adapter.config, route_mode="ordinary_multi_tile")
        adapter.authorization = replace(adapter.authorization, route_mode="ordinary_multi_tile")
        adapter.authorization_proof = replace(adapter.authorization_proof, route_mode="ordinary_multi_tile")
        adapter._route_polygon_sample = lambda position, **kwargs: {
            "position": {
                "x": position.x,
                "y": position.y,
                "z": position.z,
            },
            "global_resolution": SimpleNamespace(
                selected_reference=1,
                to_dict=lambda: {},
            ),
            "corridor_resolution": SimpleNamespace(to_dict=lambda: {}),
            "metrics": {
                "cross_track_m": 0.0,
                "vertical_error_m": 0.0,
                "along_route_m": 0.0,
                "route_length_m": 4.0,
            },
        }
        adapter._route_local_sample_assessment = lambda previous, current: {
            "safe": True,
            "reason_code": "inside_authorized_corridor",
            "accepted_index": 0,
            "accepted_ref": adapter.authorization.start_ref,
        }
        original_follower_event = adapter._follower_event

        def delayed_post_command_follower_event(observation):
            if observation.sequence == 4:
                clock.value += 0.8
            return original_follower_event(observation)

        adapter._follower_event = delayed_post_command_follower_event

        original_route_sample = adapter._route_polygon_sample

        def slow_route_sample(position, **kwargs):
            # Sequence 5 is fresh when acquired, but the expensive corridor
            # check consumes enough of the same effective-age budget to force
            # one more neutral advancing-sample reacquisition.
            if position == Vec3(1.0, 0.0, 0.0):
                clock.value += 0.6
            return original_route_sample(position)

        adapter._route_polygon_sample = slow_route_sample
        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(result["movement_cycles"], 2)
        self.assertEqual(len(actuator.commands), 2)
        commands = [
            item for item in result["timeline"]
            if item.get("event") == "non_neutral_command_imminent"
        ]
        self.assertEqual([item["sequence"] for item in commands], [3, 6])
        self.assertTrue(any(
            item.get("event") == "post_command_freshness_validation_expired"
            and item.get("sequence") == 5
            for item in result["timeline"]
        ))
        reacquired = [
            item for item in result["timeline"]
            if item.get("event") == "post_command_freshness_reacquired"
        ]
        self.assertEqual(reacquired[-1]["sequence"], 6)
        self.assertEqual(telemetry.index, 6)
        self.assertTrue(adapter.actuator.is_neutral())

    def test_post_command_stale_replacement_never_authorizes_second_command(self):
        clock = FakeClock()
        adapter, _, actuator, _ = make_adapter(
            clock=clock,
            observations=[
                make_observation(sequence=1),
                make_observation(sequence=2),
                make_observation(sequence=3),
                make_observation(sequence=4, monotonic_t=10.0),
                make_observation(sequence=5, monotonic_t=10.0),
            ],
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])
        original_follower_event = adapter._follower_event

        def delayed_post_command_follower_event(observation):
            if observation.sequence == 4:
                clock.value += 0.8
            return original_follower_event(observation)

        adapter._follower_event = delayed_post_command_follower_event
        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(result["movement_cycles"], 1)
        self.assertEqual(len(actuator.commands), 1)
        self.assertFalse(any(
            item.get("event") == "post_command_freshness_reacquired"
            for item in result["timeline"]
        ))
        self.assertFalse(any(
            item.get("event") == "non_neutral_command_imminent"
            and item.get("sequence") == 5
            for item in result["timeline"]
        ))
        self.assertTrue(adapter.actuator.is_neutral())

    def test_post_command_reacquisition_rejects_session_change(self):
        clock = FakeClock()
        adapter, _, actuator, _ = make_adapter(
            clock=clock,
            observations=[
                make_observation(sequence=1),
                make_observation(sequence=2),
                make_observation(sequence=3),
                make_observation(sequence=4, monotonic_t=10.0),
                make_observation(sequence=5, monotonic_t=10.8, session_id="other-session"),
            ],
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])
        original_follower_event = adapter._follower_event

        def delayed_post_command_follower_event(observation):
            if observation.sequence == 4:
                clock.value += 0.8
            return original_follower_event(observation)

        adapter._follower_event = delayed_post_command_follower_event
        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(result["movement_cycles"], 1)
        self.assertEqual(len(actuator.commands), 1)
        self.assertFalse(any(
            item.get("event") == "non_neutral_command_imminent"
            and item.get("sequence") == 5
            for item in result["timeline"]
        ))
        self.assertTrue(adapter.actuator.is_neutral())

    def test_post_command_reacquisition_rejects_readiness_loss(self):
        clock = FakeClock()
        adapter, _, actuator, _ = make_adapter(
            clock=clock,
            observations=[
                make_observation(sequence=1),
                make_observation(sequence=2),
                make_observation(sequence=3),
                make_observation(sequence=4, monotonic_t=10.0),
                make_observation(
                    sequence=5,
                    monotonic_t=10.8,
                    telemetry_gameplay_ready=False,
                    controllable=False,
                ),
            ],
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])
        original_follower_event = adapter._follower_event

        def delayed_post_command_follower_event(observation):
            if observation.sequence == 4:
                clock.value += 0.8
            return original_follower_event(observation)

        adapter._follower_event = delayed_post_command_follower_event
        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(result["movement_cycles"], 1)
        self.assertEqual(len(actuator.commands), 1)
        self.assertTrue(adapter.actuator.is_neutral())
        self.assertFalse(any(
            item.get("event") == "non_neutral_command_imminent"
            and item.get("sequence") == 5
            for item in result["timeline"]
        ))

    def test_production_shaped_post_command_readiness_recovery_after_freshness_expiry(self):
        """A strict freshness expiry must re-enter the real settled-proof gate."""
        tail = [
            _production_freshness_state(4, 0.1),
            _production_freshness_state(5, 0.1),
            _production_freshness_state(6, 0.1),
            _production_freshness_state(7, 0.19238),
            # This repeated stale publication invalidates the service's
            # strict settled proof before the next fresh source sample.
            _production_freshness_state(7, 0.6),
            _production_freshness_state(8, 0.1),
            _production_freshness_state(9, 0.1),
            _production_freshness_state(10, 0.1),
            _production_freshness_state(11, 0.1),
            _production_freshness_state(12, 0.1),
        ]
        adapter, service, state_telemetry, actuator, clock = make_production_freshness_adapter(tail)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        original_follower_event = adapter._follower_event

        def delayed_post_command_follower_event(observation):
            if observation.sequence == 7:
                clock.value += 0.31
            return original_follower_event(observation)

        adapter._follower_event = delayed_post_command_follower_event
        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(result["movement_cycles"], 2)
        self.assertEqual(len(actuator.commands), 2)
        self.assertTrue(any(
            item.get("event") == "readiness_loss_triggered"
            for item in result["timeline"]
        ))
        expired = next(
            item for item in result["timeline"]
            if item.get("event") == "post_command_telemetry_freshness_expired"
        )
        self.assertAlmostEqual(
            expired["effective_telemetry_age_seconds"],
            0.50238,
            places=5,
        )
        self.assertTrue(any(
            item.get("event") == "readiness_recovery_settled_wait_completed"
            and item.get("successful") is True
            for item in result["timeline"]
        ))
        self.assertTrue(any(
            item.get("event") == "readiness_reacquired_route_state"
            and item.get("observation", {}).get("sequence") == 12
            for item in result["timeline"]
        ))
        commands = [
            item for item in result["timeline"]
            if item.get("event") == "non_neutral_command_imminent"
        ]
        self.assertEqual([item["sequence"] for item in commands], [6, 12])
        self.assertTrue(service.session_health(
            _production_freshness_state(13, 0.1)
        )["gameplay_ready"])
        self.assertEqual(
            state_telemetry.observed_sequences,
            [1, 2, 3, 4, 5, 6, 7, 7, 8, 9, 10, 11, 12],
        )
        self.assertTrue(adapter.actuator.is_neutral())

    def test_production_shaped_genuine_staleness_remains_fail_closed(self):
        """A genuinely stale source cannot be admitted by readiness recovery."""
        tail = [
            _production_freshness_state(4, 0.1),
            _production_freshness_state(5, 0.1),
            _production_freshness_state(6, 0.1),
            _production_freshness_state(7, 0.19238),
            _production_freshness_state(7, 0.6),
        ]
        adapter, _, state_telemetry, actuator, clock = make_production_freshness_adapter(tail)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        original_follower_event = adapter._follower_event

        def delayed_post_command_follower_event(observation):
            if observation.sequence == 7:
                clock.value += 0.31
            return original_follower_event(observation)

        adapter._follower_event = delayed_post_command_follower_event
        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(result["movement_cycles"], 1)
        self.assertEqual(len(actuator.commands), 1)
        self.assertEqual(result["state"], AdapterState.STOPPED.value)
        self.assertEqual(result["reason"], "telemetry_stale")
        self.assertFalse(any(
            item.get("event") == "non_neutral_command_imminent"
            and item.get("sequence") == 7
            for item in result["timeline"]
        ))
        self.assertTrue(any(
            sequence == 7 for sequence in state_telemetry.observed_sequences
        ))
        self.assertTrue(adapter.actuator.is_neutral())

    def test_post_command_reacquisition_requires_an_advancing_sample(self):
        clock = FakeClock()
        adapter, _, actuator, _ = make_adapter(
            clock=clock,
            observations=[
                make_observation(sequence=1),
                make_observation(sequence=2),
                make_observation(sequence=3),
                make_observation(sequence=4, monotonic_t=10.0),
                make_observation(sequence=4, monotonic_t=10.0),
            ],
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])
        original_follower_event = adapter._follower_event

        def delayed_post_command_follower_event(observation):
            if observation.sequence == 4:
                clock.value += 0.8
            return original_follower_event(observation)

        adapter._follower_event = delayed_post_command_follower_event
        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(result["movement_cycles"], 1)
        self.assertEqual(len(actuator.commands), 1)
        self.assertIn(result["reason"], {"telemetry_stale", "telemetry_not_advancing"})
        self.assertTrue(adapter.actuator.is_neutral())

    def test_post_command_reacquisition_requires_confirmed_neutral_actuator(self):
        clock = FakeClock()
        adapter, _, actuator, _ = make_adapter(
            clock=clock,
            observations=[
                make_observation(sequence=1),
                make_observation(sequence=2),
                make_observation(sequence=3),
                make_observation(sequence=4, monotonic_t=10.0),
                make_observation(sequence=5, monotonic_t=10.8),
            ],
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])
        clock.value = 10.8
        with patch.object(actuator, "is_neutral", return_value=False):
            with self.assertRaisesRegex(AdapterError, "actuator_neutrality_unconfirmed"):
                adapter._ensure_fresh_following_observation(make_observation(sequence=4, monotonic_t=10.0))
        self.assertEqual(actuator.commands, [])

    def test_following_waits_for_fresh_sample_after_first_command(self):
        adapter, _, actuator, _ = make_adapter(observations=[
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_age_seconds=1.25),
            make_observation(sequence=5, telemetry_age_seconds=0.20),
        ])
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=2)
        self.assertEqual(result["movement_cycles"], 2)
        self.assertEqual(len(actuator.commands), 2)
        self.assertFalse(any(item.get("event") == "readiness_loss_triggered" for item in result["timeline"]))
        fresh_next = [item for item in result["timeline"] if item.get("event") == "fresh_next_telemetry_acquired"]
        self.assertEqual(fresh_next[-1]["sequence"], 5)
        self.assertEqual(fresh_next[-1]["after_sequence"], 3)
        resumed = [item for item in result["timeline"] if item.get("event") == "non_neutral_command_imminent"][-1]
        self.assertEqual(resumed["sequence"], 5)

    def test_post_pulse_waits_neutral_for_advancing_sequence_without_readiness_recovery(self):
        adapter, _, actuator, _ = make_adapter(observations=[
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=3),
            make_observation(sequence=3),
            make_observation(sequence=3),
            make_observation(sequence=4),
        ])
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=2)
        self.assertEqual(result["movement_cycles"], 2)
        self.assertEqual(len(actuator.commands), 2)
        commands = [item for item in result["timeline"] if item.get("event") == "non_neutral_command_imminent"]
        self.assertEqual([item["sequence"] for item in commands], [3, 4])
        self.assertFalse(any(item.get("event") == "readiness_loss_triggered" for item in result["timeline"]))
        waiting = [item for item in result["timeline"] if item.get("event") == "waiting_for_next_telemetry"]
        acquired = [item for item in result["timeline"] if item.get("event") == "fresh_next_telemetry_acquired"]
        self.assertEqual(waiting[-1]["after_sequence"], 3)
        self.assertEqual(waiting[-1]["actuator_neutral"], True)
        self.assertEqual(acquired[-1]["sequence"], 4)
        self.assertEqual(acquired[-1]["after_sequence"], 3)

    def test_stale_advancing_sample_cannot_authorize_a_second_command(self):
        adapter, _, actuator, _ = make_adapter(observations=[
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_age_seconds=1.25),
        ])
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=2)
        self.assertEqual(result["movement_cycles"], 1)
        self.assertEqual(len(actuator.commands), 1)
        self.assertTrue(any(item.get("event") == "telemetry_advancement_timeout" for item in result["timeline"]))
        self.assertFalse(any(
            item.get("event") == "non_neutral_command_imminent" and item.get("sequence") == 4
            for item in result["timeline"]
        ))

    def test_live_command_evidence_records_band_magnitude_and_duration(self):
        adapter, _, actuator, _ = make_adapter()
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=1)
        self.assertEqual(len(actuator.commands), 1)
        imminent = next(item for item in result["timeline"] if item.get("event") == "non_neutral_command_imminent")
        cycle = next(item for item in result["timeline"] if item.get("event") == "control_cycle")
        completed = next(item for item in result["timeline"] if item.get("event") == "movement_pulse_completed")
        self.assertEqual(imminent["suggested_speed_band"], "normal")
        self.assertAlmostEqual(imminent["magnitude"], 0.35, places=6)
        self.assertEqual(imminent["command_duration_ms"], 100)
        self.assertEqual(cycle["command_duration_ms"], 100)
        self.assertEqual(completed["command_duration_ms"], 100)
        self.assertTrue(completed["actuator_neutral"])

    def test_nonterminal_stop_band_waits_without_calling_zero_duration_input(self):
        adapter, _, actuator, _ = make_adapter(
            follower_mode="FOLLOWING_STOP",
            observations=[
                make_observation(sequence=1),
                make_observation(sequence=2),
                make_observation(sequence=3),
                make_observation(sequence=4),
            ],
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=2)
        self.assertEqual(actuator.commands, [])
        self.assertEqual(result["movement_cycles"], 0)
        suppressed = [
            item for item in result["timeline"]
            if item.get("event") == "movement_command_suppressed"
        ]
        self.assertEqual(len(suppressed), 2)
        self.assertTrue(all(item["movement_command_sent"] is False for item in suppressed))
        self.assertTrue(all(item["actuator_neutral"] is True for item in suppressed))

    def test_near_goal_policy_uses_effective_floor_and_bounded_duration(self):
        config = LiveAdapterConfig(
            max_cycles=4,
            post_arrival_samples=0,
            control_period_seconds=0.3,
            normal_stick_magnitude=0.6,
            slow_stick_magnitude=0.2,
            minimum_effective_stick_magnitude=0.5,
            approach_stick_magnitude=0.5,
            final_stick_magnitude=0.5,
            approach_control_period_seconds=0.25,
            final_control_period_seconds=0.25,
        )
        adapter, _, _, _ = make_adapter(config=config)
        observation = make_observation()

        _, _, normal = adapter._steering(observation, {
            "desired_heading_deg": 0.0,
            "suggested_speed_band": "normal",
            "distance_m": 4.0,
            "distance_to_final_destination_m": 10.0,
            "heading_error_deg": 0.0,
        })
        self.assertEqual(normal["control_band"], "normal")
        self.assertAlmostEqual(normal["magnitude"], 0.6, places=6)
        self.assertEqual(normal["command_duration_ms"], 300)

        _, _, approach = adapter._steering(observation, {
            "desired_heading_deg": 0.0,
            "suggested_speed_band": "slow",
            "distance_m": 1.8,
            "distance_to_final_destination_m": 10.0,
            "heading_error_deg": 0.0,
        })
        self.assertEqual(approach["control_band"], "approach")
        self.assertAlmostEqual(approach["magnitude"], 0.5, places=6)
        self.assertEqual(approach["command_duration_ms"], 250)
        self.assertIn("follower_distance_slow", approach["control_selection_reasons"])
        self.assertNotIn("adapter_final_distance_slow", approach["control_selection_reasons"])
        self.assertGreaterEqual(approach["magnitude"], config.minimum_effective_stick_magnitude)

        _, _, final = adapter._steering(observation, {
            "desired_heading_deg": 0.0,
            "suggested_speed_band": "slow",
            "distance_m": 0.8,
            "distance_to_final_destination_m": 0.9,
            "heading_error_deg": 0.0,
        })
        self.assertEqual(final["control_band"], "final")
        self.assertAlmostEqual(final["magnitude"], 0.5, places=6)
        self.assertEqual(final["command_duration_ms"], 250)
        self.assertIn("final_control_distance", final["control_selection_reasons"])
        self.assertGreaterEqual(final["magnitude"], config.minimum_effective_stick_magnitude)

    def test_residual_speed_selects_neutral_braking_before_final_hold(self):
        adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(
            control_period_seconds=0.3,
            approach_control_period_seconds=0.25,
            final_control_period_seconds=0.25,
        ))
        _, _, steering = adapter._steering(make_observation(velocity_cm_s=Vec3(400.0, 0.0, 0.0)), {
            "desired_heading_deg": 0.0,
            "suggested_speed_band": "slow",
            "distance_m": 1.8,
            "distance_to_final_destination_m": 1.8,
            "heading_error_deg": 0.0,
        })
        assert steering["control_band"] == "braking"
        assert steering["magnitude"] == 0.0
        assert steering["command_duration_ms"] == 250
        assert "arrival_residual_braking" in steering["control_selection_reasons"]

    def test_continuous_residual_speed_selects_neutral_braking_before_final_hold(self):
        adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(
            controller_mode="continuous",
            control_period_seconds=0.3,
            approach_control_period_seconds=0.25,
            final_control_period_seconds=0.25,
        ))
        _, _, steering = adapter._steering(make_observation(velocity_cm_s=Vec3(400.0, 0.0, 0.0)), {
            "desired_heading_deg": 0.0,
            "suggested_speed_band": "slow",
            "distance_m": 1.8,
            "distance_to_final_destination_m": 1.8,
            "heading_error_deg": 0.0,
        })
        assert steering["control_band"] == "braking"
        assert steering["magnitude"] == 0.0
        assert steering["command_duration_ms"] == 250
        assert "arrival_residual_braking" in steering["control_selection_reasons"]


    def test_large_heading_error_remains_bounded_near_goal(self):
        config = LiveAdapterConfig(
            max_cycles=4,
            post_arrival_samples=0,
            normal_stick_magnitude=0.6,
            slow_stick_magnitude=0.2,
            approach_stick_magnitude=0.5,
            final_stick_magnitude=0.5,
        )
        adapter, _, _, _ = make_adapter(config=config)
        _, _, steering = adapter._steering(make_observation(), {
            "desired_heading_deg": 120.0,
            "suggested_speed_band": "slow",
            "distance_m": 4.0,
            "distance_to_final_destination_m": 1.4,
            "heading_error_deg": 120.0,
        })
        self.assertEqual(steering["control_band"], "approach")
        self.assertIn("follower_heading_slow", steering["control_selection_reasons"])
        self.assertNotEqual(steering["magnitude"], config.normal_stick_magnitude)
        self.assertEqual(steering["command_duration_ms"], 250)

    def test_post_command_evidence_records_observed_displacement(self):
        observations = [
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, position=Vec3(10.0, 0.0, 0.0)),
        ]
        adapter, _, actuator, _ = make_adapter(observations=observations)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=2)
        self.assertEqual(len(actuator.commands), 2)
        post = next(item for item in result["timeline"] if item.get("event") == "post_command_observation")
        self.assertEqual(post["command_sequence"], 3)
        self.assertEqual(post["sequence"], 4)
        self.assertAlmostEqual(post["observed_displacement_m"], 0.1, places=6)
        self.assertEqual(post["command_duration_ms"], 100)
        self.assertTrue(post["actuator_neutral"])

    def test_temporary_loading_neutralizes_reacquires_route_state_and_resumes(self):
        telemetry = FakeSettlingTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=5),
        ])
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        telemetry.observations[4] = replace(
            telemetry.observations[4],
            position=route_test_position(adapter),
        )
        result = adapter.run(dry_run=False, max_cycles=2)
        self.assertEqual(len(actuator.commands), 2)
        self.assertAlmostEqual(telemetry.settle_calls[0][0], 2.95, places=6)
        self.assertTrue(any(item.get("event") == "readiness_loss_triggered" for item in result["timeline"]))
        self.assertTrue(any(item.get("event") == "readiness_reacquired_route_state" for item in result["timeline"]))
        imminent = [item for item in result["timeline"] if item.get("event") == "non_neutral_command_imminent"]
        self.assertEqual([item["sequence"] for item in imminent], [3, 5])
        timeline = result["timeline"]
        trigger_index = next(index for index, item in enumerate(timeline) if item.get("event") == "readiness_loss_triggered")
        reacquired_index = next(index for index, item in enumerate(timeline) if item.get("event") == "readiness_reacquired_route_state")
        self.assertFalse(any(
            item.get("event") == "non_neutral_command_imminent"
            for item in timeline[trigger_index:reacquired_index + 1]
        ))

    def test_recovery_settle_reserves_budget_for_fresh_advancing_sample(self):
        clock = FakeClock()
        telemetry = TimedSettlingTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=5),
        ], clock, wait_duration=2.95)
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry, clock=clock)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        telemetry.observations[4] = replace(
            telemetry.observations[4],
            position=route_test_position(adapter),
            monotonic_t=clock.value + 2.95,
        )

        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(len(actuator.commands), 2)
        self.assertLess(telemetry.settle_calls[0][0], adapter.config.readiness_recovery_timeout_seconds)
        settled = next(item for item in result["timeline"] if item.get("event") == "readiness_recovery_settled_wait_completed")
        self.assertTrue(settled["successful"])
        recovery = next(item for item in result["timeline"] if item.get("event") == "readiness_recovery_sample")
        self.assertEqual(recovery["observation"]["sequence"], 5)
        resumed = [item for item in result["timeline"] if item.get("event") == "non_neutral_command_imminent"]
        self.assertEqual([item["sequence"] for item in resumed], [3, 5])
        trigger_index = next(index for index, item in enumerate(result["timeline"])
                             if item.get("event") == "readiness_loss_triggered")
        reacquired_index = next(index for index, item in enumerate(result["timeline"])
                                if item.get("event") == "readiness_reacquired_route_state")
        self.assertFalse(any(
            item.get("event") == "non_neutral_command_imminent"
            for item in result["timeline"][trigger_index:reacquired_index + 1]
        ))

    def test_recovery_success_after_settle_consumes_deadline_still_requires_sample(self):
        clock = FakeClock()
        telemetry = TimedSettlingTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=5),
        ], clock, wait_duration=3.0, honor_timeout=False)
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry, clock=clock)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        telemetry.observations[4] = replace(
            telemetry.observations[4],
            position=route_test_position(adapter),
            monotonic_t=clock.value + 3.0,
        )

        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(clock.value, 13.0)
        self.assertEqual(len(actuator.commands), 2)
        self.assertTrue(any(
            item.get("event") == "readiness_recovery_sample"
            and item["observation"]["sequence"] == 5
            for item in result["timeline"]
        ))
        self.assertNotEqual(result["reason"], "readiness_recovery_timeout")

    def test_recovery_settled_success_without_fresh_advancing_sample_stays_neutral(self):
        clock = FakeClock()
        telemetry = TimedSettlingTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
        ], clock, wait_duration=2.95)
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry, clock=clock)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(result["reason"], "readiness_recovery_post_settle_observation_unavailable")
        self.assertEqual(len(actuator.commands), 1)
        self.assertTrue(any(
            item.get("event") == "readiness_recovery_failed"
            and item.get("post_settle_observation_failure") == "telemetry_not_advancing"
            for item in result["timeline"]
        ))
        trigger_index = next(index for index, item in enumerate(result["timeline"])
                             if item.get("event") == "readiness_loss_triggered")
        self.assertFalse(any(
            item.get("event") == "non_neutral_command_imminent"
            for item in result["timeline"][trigger_index + 1:]
        ))

    def test_residual_recovery_route_below_initial_minimum_is_validated(self):
        telemetry = FakeTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=5),
        ])
        validator = FakeValidator()
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry, validator=validator)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        telemetry.observations[4] = replace(
            telemetry.observations[4],
            position=route_test_position(adapter, fraction=0.35, perpendicular_m=0.8),
        )

        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(len(actuator.commands), 2)
        replan = next(item for item in result["timeline"]
                      if item.get("event") == "readiness_replanned_and_independently_validated")
        self.assertLess(
            math.hypot(
                replan["position"]["x"] - adapter.authorization.resolved_end.x,
                replan["position"]["y"] - adapter.authorization.resolved_end.y,
            ) / 100.0,
            3.0,
        )
        self.assertEqual(len(validator.plans), 2)
        self.assertEqual(adapter.topology.load_calls, 1)

    def test_reacquired_position_inside_final_arrival_radius_does_not_move(self):
        telemetry = FakeTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=5),
        ])
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        telemetry.observations[4] = replace(
            telemetry.observations[4],
            position=route_test_position(adapter, fraction=1.0),
        )

        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(result["state"], AdapterState.ARRIVED.value)
        self.assertEqual(result["movement_cycles"], 1)
        self.assertEqual(len(actuator.commands), 1)
        self.assertTrue(any(
            item.get("event") == "readiness_reacquired_inside_final_arrival_radius"
            for item in result["timeline"]
        ))

    def test_arrival_settling_uses_actor_origin_velocity_when_available(self):
        clock = FakeClock()
        previous = make_observation(
            sequence=1,
            grounded=True,
            velocity_cm_s=Vec3(40.0, 0.0, 0.0),
        )
        current = make_observation(
            sequence=2,
            position=Vec3(20.0, 0.0, 0.0),
            grounded=True,
            velocity_cm_s=Vec3(40.0, 0.0, 0.0),
        )
        settled = make_observation(
            sequence=3,
            position=Vec3(20.0, 0.0, 0.0),
            grounded=True,
            velocity_cm_s=Vec3(10.0, 0.0, 0.0),
        )
        telemetry = FakeTelemetry([settled])
        adapter, _, _, _ = make_adapter(
            telemetry=telemetry,
            clock=clock,
            config=LiveAdapterConfig(max_cycles=2, post_arrival_samples=1),
        )

        result = adapter._settle_preflight_pose(
            previous,
            current,
            require_lease=True,
            phase="arrival",
        )

        assert result is settled
        assert any(
            item.get("event") == "arrival_pose_settling"
            and item.get("stable") is True
            for item in adapter.timeline
        )


    def test_final_arrival_and_post_confirmation_use_horizontal_vertical_envelope(self):
        adapter, telemetry, _, _ = make_adapter(
            config=LiveAdapterConfig(max_cycles=2, post_arrival_samples=1)
        )
        self.assertTrue(adapter.preflight()["movement_authorized"])
        end = adapter.authorization.resolved_end
        accepted = Vec3(end.x, end.y, end.z + 140.0)
        outside_horizontal = Vec3(end.x + 76.0, end.y, end.z + 140.0)
        excessive_vertical = Vec3(end.x, end.y, end.z + 151.0)

        self.assertTrue(adapter._within_final_arrival_tolerance(accepted, end))
        self.assertFalse(adapter._within_final_arrival_tolerance(outside_horizontal, end))
        self.assertFalse(adapter._within_final_arrival_tolerance(excessive_vertical, end))

        adapter._set_state(AdapterState.ARRIVED, "test_arrival")
        telemetry.observations = [make_observation(position=accepted, sequence=adapter._last_observation.sequence + 1)]
        telemetry.index = 0
        adapter._confirm_post_arrival()
        self.assertEqual(adapter.state, AdapterState.ARRIVED)
        self.assertTrue(adapter.timeline[-1]["confirmed"])

        adapter._set_state(AdapterState.ARRIVED, "test_arrival")
        telemetry.observations = [make_observation(position=outside_horizontal, sequence=adapter._last_observation.sequence + 1)]
        telemetry.index = 0
        adapter._confirm_post_arrival()
        self.assertEqual(adapter.state, AdapterState.FAILED)
        self.assertFalse(adapter.timeline[-1]["confirmed"])

    def test_arrival_requires_advancing_samples_and_unchanged_identity(self):
        adapter, telemetry, actuator, _ = make_adapter(config=LiveAdapterConfig(post_arrival_samples=2))
        self.assertTrue(adapter.preflight()["movement_authorized"])
        end = adapter.authorization.resolved_end
        prior = adapter._last_observation.sequence
        telemetry.observations = [make_observation(position=end, sequence=seq)
                                  for seq in [prior, prior + 1, prior + 1, prior + 2]]
        telemetry.index = 0
        adapter._set_state(AdapterState.ARRIVED, "test_arrival")
        adapter._confirm_post_arrival()
        self.assertEqual([s["sequence"] for s in adapter.timeline[-1]["samples"]], [prior + 1, prior + 2])
        self.assertTrue(adapter.timeline[-1]["confirmed"])
        telemetry.observations = [replace(make_observation(position=end, sequence=prior + 3), session_id="other-run")]
        telemetry.index = 0
        adapter._confirm_post_arrival()
        self.assertEqual(adapter.state, AdapterState.FAILED)
        self.assertIn("session_changed", adapter.reason)

    def test_recovery_route_over_maximum_distance_is_rejected(self):
        telemetry = FakeTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=5),
        ])
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        telemetry.observations[4] = replace(
            telemetry.observations[4],
            position=route_test_position(adapter, fraction=0.0, perpendicular_m=7.0),
        )

        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(result["state"], AdapterState.REPLAN_REQUIRED.value)
        self.assertEqual(len(actuator.commands), 1)
        self.assertTrue(any(
            item.get("event") == "readiness_replan_failed"
            and item.get("reason") == "recovery_route_distance_exceeds_bound"
            for item in result["timeline"]
        ))

    def test_recovery_start_resolution_bounds_are_retained(self):
        for field_name, value, expected_reason in (
            ("horizontal_distance_m", 0.76, "live_start_snap_exceeds_bound"),
            ("vertical_distance_m", 1.01, "live_start_vertical_separation_exceeds_bound"),
        ):
            with self.subTest(field_name=field_name):
                telemetry = FakeTelemetry([
                    make_observation(sequence=1),
                    make_observation(sequence=2),
                    make_observation(sequence=3),
                    make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
                    make_observation(sequence=5),
                ])
                adapter, _, actuator, _ = make_adapter(telemetry=telemetry)
                self.assertTrue(adapter.preflight()["movement_authorized"])
                telemetry.observations[4] = replace(
                    telemetry.observations[4],
                    position=route_test_position(adapter, fraction=0.5, perpendicular_m=0.8),
                )
                bad_plan = dict(adapter.route_plan)
                bad_resolution = dict(bad_plan["start_resolution"])
                bad_resolution[field_name] = value
                bad_plan["start_resolution"] = bad_resolution
                with patch.object(adapter.topology_session.graph, "plan", return_value=bad_plan):
                    result = adapter.run(dry_run=False, max_cycles=2)
                self.assertEqual(result["state"], AdapterState.REPLAN_REQUIRED.value)
                self.assertEqual(len(actuator.commands), 1)
                self.assertTrue(any(
                    item.get("event") == "readiness_replan_failed"
                    and item.get("reason") == expected_reason
                    for item in result["timeline"]
                ))

    def test_recovery_route_transition_constraints_are_retained(self):
        for field_name in ("polygon_corridor", "portals", "validation"):
            with self.subTest(field_name=field_name):
                telemetry = FakeTelemetry([
                    make_observation(sequence=1),
                    make_observation(sequence=2),
                    make_observation(sequence=3),
                    make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
                    make_observation(sequence=5),
                ])
                adapter, _, actuator, _ = make_adapter(telemetry=telemetry)
                self.assertTrue(adapter.preflight()["movement_authorized"])
                telemetry.observations[4] = replace(
                    telemetry.observations[4],
                    position=route_test_position(adapter, fraction=0.5, perpendicular_m=0.8),
                )
                bad_plan = dict(adapter.route_plan)
                if field_name == "polygon_corridor":
                    bad_plan[field_name] = [1, 2]
                elif field_name == "portals":
                    bad_plan[field_name] = [dict(bad_plan[field_name][0]), dict(bad_plan[field_name][1])]
                    bad_plan[field_name][0]["side"] = 1
                else:
                    bad_plan[field_name] = dict(bad_plan[field_name])
                    bad_plan[field_name]["tile_crossings"] = 1
                with patch.object(adapter.topology_session.graph, "plan", return_value=bad_plan):
                    result = adapter.run(dry_run=False, max_cycles=2)
                self.assertEqual(result["state"], AdapterState.REPLAN_REQUIRED.value)
                self.assertEqual(len(actuator.commands), 1)
                self.assertTrue(any(item.get("event") == "readiness_replan_failed" for item in result["timeline"]))

    def test_recovery_independent_validator_rejection_never_resumes(self):
        telemetry = FakeTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=5),
        ])
        validator = FakeValidator()
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry, validator=validator)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        validator.result = {"valid": False, "errors": ["recovery rejected"], "warnings": []}
        telemetry.observations[4] = replace(
            telemetry.observations[4],
            position=route_test_position(adapter, fraction=0.35, perpendicular_m=0.8),
        )

        result = adapter.run(dry_run=False, max_cycles=2)

        self.assertEqual(result["state"], AdapterState.REPLAN_REQUIRED.value)
        self.assertEqual(len(actuator.commands), 1)
        self.assertTrue(any(
            item.get("event") == "readiness_replan_failed"
            and item.get("reason") == "independent_validator_rejected_recovery_route"
            for item in result["timeline"]
        ))

    def test_temporary_not_controllable_neutralizes_then_resumes_only_after_fresh_telemetry(self):
        telemetry = FakeTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=False, controllable=False),
            make_observation(sequence=5, telemetry_age_seconds=0.2),
        ])
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        telemetry.observations[4] = replace(
            telemetry.observations[4],
            position=route_test_position(adapter, fraction=0.25),
        )
        result = adapter.run(dry_run=False, max_cycles=2)
        self.assertEqual(len(actuator.commands), 2)
        samples = [item for item in result["timeline"] if item.get("event") == "readiness_recovery_sample"]
        self.assertEqual(samples[-1]["observation"]["sequence"], 5)
        resumed = [item for item in result["timeline"] if item.get("event") == "non_neutral_command_imminent"][-1]
        self.assertEqual(resumed["sequence"], 5)
        self.assertLessEqual(resumed["telemetry_age_seconds"], 0.5)

    def test_passive_displacement_replans_against_cached_topology_and_revalidates(self):
        telemetry = FakeTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=5),
        ])
        validator = FakeValidator()
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry, validator=validator)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        telemetry.observations[4] = replace(
            telemetry.observations[4],
            position=route_test_position(adapter, fraction=0.5, perpendicular_m=4.0),
        )
        result = adapter.run(dry_run=False, max_cycles=2)
        self.assertEqual(len(actuator.commands), 2)
        self.assertTrue(any(
            item.get("event") == "readiness_replanned_and_independently_validated"
            for item in result["timeline"]
        ))
        self.assertEqual(len(validator.plans), 2)
        self.assertEqual(adapter.topology_session.fingerprint, "topology-fingerprint")

    def test_world_change_during_readiness_recovery_never_resumes(self):
        telemetry = FakeTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=5, world_id="Other", map_id="Other"),
        ])
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=2)
        self.assertEqual(result["reason"], "world_changed")
        self.assertEqual(len(actuator.commands), 1)
        self.assertFalse(any(item.get("event") == "readiness_reacquired_route_state" for item in result["timeline"]))

    def test_session_change_during_readiness_recovery_never_resumes(self):
        telemetry = FakeTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=5, session_id="other-session"),
        ])
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=2)
        self.assertEqual(result["reason"], "session_changed")
        self.assertEqual(len(actuator.commands), 1)

    def test_readiness_recovery_timeout_never_sends_another_command(self):
        telemetry = FakeTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3),
            make_observation(sequence=4, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=5, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=6, telemetry_gameplay_ready=False, loading=True, controllable=False),
        ])
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        adapter.config = replace(adapter.config, readiness_recovery_timeout_seconds=0.2)
        result = adapter.run(dry_run=False, max_cycles=2)
        self.assertEqual(result["reason"], "readiness_recovery_timeout")
        self.assertEqual(len(actuator.commands), 1)
        trigger_index = next(index for index, item in enumerate(result["timeline"]) if item.get("event") == "readiness_loss_triggered")
        self.assertFalse(any(
            item.get("event") == "non_neutral_command_imminent"
            for item in result["timeline"][trigger_index + 1:]
        ))

    def test_final_pre_movement_gate_waits_out_transient_settling(self):
        telemetry = FakeSettlingTelemetry([
            make_observation(sequence=1),
            make_observation(sequence=2),
            make_observation(sequence=3, telemetry_gameplay_ready=False, loading=True, controllable=False),
            make_observation(sequence=4),
        ])
        adapter, _, actuator, _ = make_adapter(telemetry=telemetry)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=1)
        self.assertEqual(len(actuator.commands), 1)
        self.assertEqual(telemetry.settle_calls, [(30.0, 0.5)])
        self.assertTrue(any(
            event.get("event") == "final_pre_movement_settled_gameplay_wait_completed"
            for event in result["timeline"]
        ))

    def test_exception_during_loop_finally_neutral(self):
        actuator = FakeActuator(fail=True)
        adapter, _, actuator, _ = make_adapter(actuator=actuator)
        self.assertTrue(adapter.preflight()["movement_authorized"])
        with self.assertRaises(RuntimeError):
            adapter.run(dry_run=False, max_cycles=1)
        self.assertGreaterEqual(actuator.neutralize_count, 2)

    def test_movement_lease_loss_neutral(self):
        adapter, _, actuator, _ = make_adapter(observations=[
            make_observation(),
            make_observation(sequence=2),
            make_observation(sequence=3, lease_active=False),
        ])
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=1)
        self.assertEqual(result["state"], AdapterState.STOPPED.value)
        self.assertEqual(actuator.commands, [])

    def test_camera_relative_world_heading_conversion(self):
        diagonal = math.sqrt(0.5)
        cases = (
            (0.0, 0.0, 0.0, 1.0),
            (45.0, 0.0, diagonal, diagonal),
            (-45.0, 0.0, -diagonal, diagonal),
            (90.0, 0.0, 1.0, 0.0),
            (-90.0, 0.0, -1.0, 0.0),
            (135.0, 0.0, diagonal, -diagonal),
            (-135.0, 0.0, -diagonal, -diagonal),
            (180.0, 0.0, 0.0, -1.0),
            (179.999, -179.999, -0.0000349066, 0.9999999994),
            (-179.999, 179.999, 0.0000349066, 0.9999999994),
        )
        for desired, camera, expected_x, expected_y in cases:
            with self.subTest(desired=desired, camera=camera):
                x, y = world_heading_to_camera_stick(desired, camera, 1.0)
                self.assertAlmostEqual(x, expected_x, places=5)
                self.assertAlmostEqual(y, expected_y, places=5)
                self.assertAlmostEqual(math.hypot(x, y), 1.0, places=6)

    def test_stick_magnitude_clamping(self):
        x, y = world_heading_to_camera_stick(45.0, 0.0, 1.0)
        self.assertLessEqual(math.hypot(x, y), 1.0 + 1e-9)
        self.assertAlmostEqual(math.hypot(x, y), 1.0, places=6)

    def test_dry_run_calculates_but_sends_neutral(self):
        adapter, _, actuator, _ = make_adapter()
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=True, max_cycles=1)
        self.assertEqual(actuator.commands, [])
        cycle = next(item for item in result["timeline"] if item.get("event") == "control_cycle")
        self.assertFalse(cycle["actual_command_sent"])
        self.assertTrue(math.isfinite(cycle["actuator"]["magnitude"]))

    def test_only_exact_authorized_plan_identity_may_move(self):
        adapter, _, actuator, _ = make_adapter()
        self.assertTrue(adapter.preflight()["movement_authorized"])
        adapter.route_plan["requested_end"] = {"x": 999.0, "y": 0.0, "z": 0.0}
        result = adapter.run(dry_run=False, max_cycles=1)
        self.assertEqual(result["state"], AdapterState.REPLAN_REQUIRED.value)
        self.assertEqual(actuator.commands, [])

    def test_no_sprint_right_stick_or_action_buttons(self):
        adapter, _, actuator, _ = make_adapter()
        self.assertTrue(adapter.preflight()["movement_authorized"])
        adapter.run(dry_run=False, max_cycles=1)
        self.assertEqual(len(actuator.commands), 1)
        self.assertEqual(set(actuator.commands[0]), {"x", "y", "milliseconds"})
        self.assertLessEqual(actuator.commands[0]["milliseconds"], 1500)

    def test_world_change_stops_before_command(self):
        adapter, _, actuator, _ = make_adapter(observations=[
            make_observation(),
            make_observation(sequence=2),
            make_observation(sequence=3, world_id="Other", map_id="Other"),
        ])
        self.assertTrue(adapter.preflight()["movement_authorized"])
        result = adapter.run(dry_run=False, max_cycles=1)
        self.assertEqual(result["state"], AdapterState.STOPPED.value)
        self.assertEqual(actuator.commands, [])


class FakeCameraActuator:
    def __init__(self, fail=False):
        self.commands = []
        self.neutralize_count = 0
        self.fail = fail

    def command_horizontal(self, delta):
        if self.fail:
            raise RuntimeError("camera boom")
        self.commands.append(int(delta))
        return {"accepted": True, "horizontal_delta": int(delta), "vertical_delta": 0}

    def neutralize(self):
        self.neutralize_count += 1
        return {"neutral": True}


def _camera_adapter(*, camera=None, observations=None, **changes):
    config = LiveAdapterConfig(camera_follow_enabled=True, max_cycles=1, post_arrival_samples=0, **changes)
    adapter, telemetry, actuator, _ = make_adapter(config=config, observations=observations)
    adapter.camera_actuator = camera or FakeCameraActuator()
    assert adapter.preflight()["movement_authorized"]
    return adapter, telemetry, actuator, adapter.camera_actuator


def test_live_camera_actuation_is_horizontal_and_follows_measured_yaw():
    adapter, _, _, camera = _camera_adapter()
    event = {"state": "FOLLOWING", "suggested_speed_band": "normal"}
    positive = adapter._camera_follow_for_cycle(make_observation(camera_yaw=-90.0), event, sprint=False, dry_run=False)
    negative = adapter._camera_follow_for_cycle(make_observation(camera_yaw=90.0), event, sprint=False, dry_run=False)
    assert camera.commands[0] > 0 and camera.commands[1] < 0
    assert positive["camera_command_delta_px"] == camera.commands[0]
    assert negative["camera_command_delta_px"] == camera.commands[1]


def test_live_camera_deadband_and_stop_are_neutral():
    adapter, _, _, camera = _camera_adapter()
    event = {"state": "FOLLOWING", "suggested_speed_band": "normal"}
    inside = adapter._camera_follow_for_cycle(make_observation(camera_yaw=5.0), event, sprint=False, dry_run=False)
    stopped = adapter._camera_follow_for_cycle(make_observation(camera_yaw=90.0), {"state": "FOLLOWING", "suggested_speed_band": "stop"}, sprint=False, dry_run=False)
    assert inside["camera_input_x"] == 0.0 and stopped["camera_input_x"] == 0.0
    assert camera.commands == []
    assert stopped["reason"] == "route_follower_stopped"


def test_live_camera_suppresses_readiness_stale_and_expired_lease():
    event = {"state": "FOLLOWING", "suggested_speed_band": "normal"}
    cases = [
        (dict(controllable=False), "not_controllable"),
        (dict(telemetry_age_seconds=9.0), "telemetry_stale"),
        (dict(lease_active=False), "movement_lease_missing"),
    ]
    for changes, reason in cases:
        adapter, _, _, camera = _camera_adapter()
        result = adapter._camera_follow_for_cycle(make_observation(camera_yaw=90.0, **changes), event, sprint=False, dry_run=False)
        assert result["camera_input_x"] == 0.0 and result["reason"] == reason
        assert camera.commands == []


def test_disabled_camera_follow_preserves_movement_command():
    observations = [make_observation(sequence=i, camera_yaw=90.0) for i in range(1, 9)]
    baseline, _, baseline_actuator, _ = make_adapter(observations=observations, config=LiveAdapterConfig(max_cycles=1, post_arrival_samples=0))
    baseline.preflight()
    baseline.run(dry_run=False, max_cycles=1)
    disabled, _, disabled_actuator, _ = make_adapter(observations=observations, config=LiveAdapterConfig(camera_follow_enabled=False, max_cycles=1, post_arrival_samples=0))
    disabled.camera_actuator = FakeCameraActuator()
    disabled.preflight()
    disabled.run(dry_run=False, max_cycles=1)
    assert baseline_actuator.commands == disabled_actuator.commands
    assert disabled.camera_actuator.commands == []


def test_uncertain_camera_failure_never_reuses_old_movement_vector():
    adapter, _, actuator, _ = _camera_adapter(
        camera=FakeCameraActuator(fail=True),
        observations=[make_observation(sequence=i, camera_yaw=90.0) for i in range(1, 9)],
    )
    result = adapter.run(dry_run=False, max_cycles=1)
    assert actuator.commands == []
    assert actuator.is_neutral()
    assert adapter._camera_follow_runtime_disabled
    assert any(item.get("suppression_reason") == "camera_actuator_failed" for item in result["timeline"] if item.get("event") == "camera_follow")
    assert result["state"] == AdapterState.STOPPED.value


def test_camera_is_neutral_during_arrival_confirmation():
    adapter, _, _, camera = _camera_adapter()
    adapter._neutralize_camera("arrival_confirmation")
    assert camera.neutralize_count >= 1
    assert adapter._last_camera_result["camera_input_x"] == 0.0

if __name__ == "__main__":
    unittest.main()


def test_a_long_neutral_gap_is_not_counted_as_a_stall():
    # Live at Kalm: a 9 s re-plan with the stick neutral read as a stall.
    from ff7r_agent.live_route_adapter import load_route_follower_factory
    adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(post_arrival_samples=0))
    assert adapter.preflight()["movement_authorized"]
    adapter.follower = load_route_follower_factory()(adapter.route_plan)
    start = make_observation(sequence=50, monotonic_t=100.0)
    later = make_observation(sequence=51, monotonic_t=110.0)
    adapter._follower_event(start)
    _, event = adapter._follower_event(later)
    assert event.get("state") != "STALLED"
    assert any(e["event"] == "follower_neutral_gap_excluded" for e in adapter.timeline)
    accounting = adapter._follower_progress_accounting(110.0)
    assert accounting["active_stall_elapsed_seconds"] < 1.0


def test_a_target_moved_into_another_polygon_is_bound_to_its_new_polygon():
    # Live: moving Ned's target clear of his table put it in the next
    # polygon, and every route failed selected_route_plan_end_ref_mismatch.
    from test_ordinary_multi_polygon import _multi_graph

    graph, refs = _multi_graph()
    start = Vec3(300.0, 250.0, 0.0)
    observations = [make_observation(sequence=index, position=start) for index in range(1, 6)]
    adapter, _, _, _ = make_adapter(
        config=LiveAdapterConfig(route_mode="cached_topology", max_cycles=2, post_arrival_samples=0),
        route_target=Vec3(1500.0, 250.0, 0.0), observations=observations)
    adapter.topology.topology = replace(adapter.topology.topology, graph=graph)
    adapter.route_collision_audit = lambda points: {"segments": []}

    def move_target(topology, position):
        adapter.route_target = Vec3(900.0, 250.0, 0.0)

    adapter._clear_target_of_known_obstacles = move_target
    report = adapter.preflight()
    assert report["movement_authorized"], report
    assert adapter.route_plan["end_ref"] == refs[1].value


def test_a_complete_capture_that_only_missed_the_proof_locality_is_accepted():
    from ff7r_agent.live_route_adapter import capture_proof_locality_miss

    payload = {"status": "complete", "shipping_binary_layout_proven": True,
               "topology_proof": {"reason_code": "no_nearby_two_hop_cross_tile_link", "status": "not_proven",
                                  "cross_tile_links": 35104, "link_read_failures": 0}}
    assert capture_proof_locality_miss(payload)
    for change in ({"status": "partial"}, {"shipping_binary_layout_proven": False}):
        assert not capture_proof_locality_miss({**payload, **change})
    for proof in ({"reason_code": "cross_tile_link_mismatch"}, {"link_read_failures": 3}, {"cross_tile_links": 0}):
        assert not capture_proof_locality_miss({**payload, "topology_proof": {**payload["topology_proof"], **proof}})


def test_a_tile_excluded_capture_that_only_missed_the_proof_locality_is_accepted():
    # Live by Nene in Kalm 2026-09-30: one tile of 3,307 left out
    # and the proof found no cross-tile link two hops from Cloud.
    from ff7r_agent.live_route_adapter import capture_proof_locality_miss
    from ff7r_agent.topology_cache import tile_excluded_partial

    payload = {"status": "partial", "reason_code": "geometry_stage_rejections", "shipping_binary_layout_proven": True,
               "truncated": False, "lookup_chain_complete": True, "polygon_read_failures": 0,
               "link_read_failures": 0, "rejected_pointers": 0, "cyclic_pointers": 0,
               "valid_unique_tiles": 3307, "exported_unique_tiles": 3306, "tiles_with_incomplete_link_data": 1,
               "tiles": [{}] * 3306,
               "topology_proof": {"reason_code": "no_nearby_two_hop_cross_tile_link", "status": "not_proven",
                                  "cross_tile_links": 54122, "link_read_failures": 0}}
    assert tile_excluded_partial(payload)["excluded_tiles"] == 1
    assert capture_proof_locality_miss(payload)
    unproven = {**payload, "topology_proof": {**payload["topology_proof"], "reason_code": "other"}}
    assert tile_excluded_partial(unproven) is None
    assert not capture_proof_locality_miss(unproven)


def test_a_start_just_past_a_polygon_edge_is_on_the_navmesh():
    # Live by Nene in Kalm 2026-09-30: Cloud stood 2 cm outside.
    from types import SimpleNamespace
    from ff7r_agent.live_route_adapter import start_on_navmesh

    def resolution(reason, distance, ok=True):
        return SimpleNamespace(ok=ok, reason_code=reason, horizontal_distance_m=distance)

    assert start_on_navmesh(resolution("point_inside_polygon", 0.0))
    assert start_on_navmesh(resolution("point_snapped_to_polygon", 0.02))
    assert not start_on_navmesh(resolution("point_snapped_to_polygon", 0.4))
    assert not start_on_navmesh(resolution("point_snapped_to_polygon", None))
    assert not start_on_navmesh(resolution("point_inside_polygon", 0.0, ok=False))


def _capture_edge_target_adapter(target, *, projection=True, validator=None):
    from test_ordinary_multi_polygon import _multi_graph

    graph, refs = _multi_graph()
    observations = [make_observation(sequence=i, position=Vec3(300.0, 250.0, 0.0))
                    for i in range(1, 6)]
    adapter, telemetry, actuator, clock = make_adapter(
        config=LiveAdapterConfig(route_mode="cached_topology",
                                 admission_target_edge_projection=projection),
        route_target=target, observations=observations, validator=validator)
    adapter.topology.topology = replace(adapter.topology.topology, graph=graph)
    return adapter, actuator, refs


def test_capture_admission_projects_catalogue_target_just_outside_mesh():
    # Michaela's live catalogue position was 3.4 cm outside a linked polygon.
    original = Vec3(1500.0, -3.4, 0.0)
    adapter, actuator, refs = _capture_edge_target_adapter(original)
    report = adapter.preflight(require_lease=False)
    assert report["movement_authorized"], report
    assert adapter.route_target == Vec3(1500.0, 0.0, 0.0)
    assert adapter.route_plan["end_ref"] == refs[2].value
    assert adapter.validator.plans[-1]["requested_end"] == {"x": 1500.0, "y": 0.0, "z": 0.0}
    evidence = next(e for e in adapter.timeline if e["event"] == "capture_admission_target_projected")
    assert evidence["original_target"] == {"x": original.x, "y": original.y, "z": original.z}
    assert math.isclose(evidence["horizontal_distance_m"], 0.034)
    assert actuator.commands == []
    assert actuator.is_neutral()


def test_live_routes_still_reject_catalogue_targets_outside_mesh():
    adapter, actuator, _ = _capture_edge_target_adapter(Vec3(1500.0, -3.4, 0.0), projection=False)
    report = adapter.preflight()
    assert not report["movement_authorized"]
    assert report["reason"] == "cached_topology_route_target_not_inside_topology"
    assert actuator.commands == []


def test_capture_projection_does_not_accept_distant_or_wrong_floor_targets():
    for target in (Vec3(1500.0, -400.1, 0.0), Vec3(1500.0, -3.4, 101.0)):
        adapter, actuator, _ = _capture_edge_target_adapter(target)
        report = adapter.preflight(require_lease=False)
        assert not report["movement_authorized"], target
        assert adapter.route_target == target
        assert actuator.commands == []


def test_capture_admission_accepts_an_npc_19cm_off_mesh_without_relaxing_clouds_start():
    adapter, actuator, _ = _capture_edge_target_adapter(Vec3(1500.0, -18.8, 0.0))
    report = adapter.preflight(require_lease=False)
    assert report["movement_authorized"], report
    assert adapter.route_target == Vec3(1500.0, 0.0, 0.0)
    assert adapter.config.start_max_snap_m == 0.75
    assert actuator.commands == []


def test_capture_admission_uses_linked_floor_instead_of_the_isolated_npc_polygon():
    from test_ordinary_multi_polygon import _multi_graph
    graph, refs = _multi_graph(remove_second_portal=True)
    original = Vec3(1201.0, -1.0, 0.0)
    adapter, actuator, _ = _capture_edge_target_adapter(original)
    adapter.topology.topology = replace(adapter.topology.topology, graph=graph)
    report = adapter.preflight(require_lease=False)
    assert report["movement_authorized"], report
    assert adapter.route_target == Vec3(900.0, 250.0, 0.0)
    assert adapter.route_plan["end_ref"] == refs[1].value
    assert actuator.commands == []


def test_capture_projection_still_requires_independent_route_validation():
    validator = FakeValidator({"valid": False, "errors": ["blocked_route"], "warnings": []})
    adapter, actuator, _ = _capture_edge_target_adapter(Vec3(1500.0, -3.4, 0.0), validator=validator)
    report = adapter.preflight(require_lease=False)
    assert not report["movement_authorized"]
    assert validator.plans
    assert actuator.commands == []


def test_a_route_target_snapped_at_actor_height_is_routed_to_at_feet_height():
    # Live at Neil's table (#174): the floor was 1.05 m below the actor-origin
    # target, which snapped to a neighbouring polygon and refused every route.
    from types import SimpleNamespace
    from ff7r_agent.live_route_adapter import LiveRouteAdapter
    from ff7r_agent.models import Vec3

    def resolve_point(point, max_snap_distance_m, max_vertical_separation_m):
        inside = point.z < 100.0
        return SimpleNamespace(ok=True, reason_code="point_inside_polygon" if inside else "point_snapped_to_polygon",
                               selected_reference=268 if inside else 270)

    topology = SimpleNamespace(graph=SimpleNamespace(resolve_point=resolve_point))
    adapter = SimpleNamespace(route_target=Vec3(10.0, 20.0, 150.0), timeline=[], monotonic=lambda: 1.0,
                              config=SimpleNamespace(start_max_snap_m=0.75, start_max_vertical_m=1.0))
    resolution = LiveRouteAdapter._resolve_route_target(adapter, topology)
    assert resolution.reason_code == "point_inside_polygon" and resolution.selected_reference == 268
    assert adapter.route_target == Vec3(10.0, 20.0, 62.0)
    assert adapter.timeline[-1]["event"] == "route_target_resolved_at_feet_height"

    # Inside as given: unchanged. Inside at neither height: the original refusal stands.
    adapter.route_target, adapter.timeline = Vec3(1.0, 1.0, 50.0), []
    assert LiveRouteAdapter._resolve_route_target(adapter, topology).selected_reference == 268
    assert adapter.route_target == Vec3(1.0, 1.0, 50.0) and adapter.timeline == []
    adapter.route_target = Vec3(1.0, 1.0, 500.0)
    assert LiveRouteAdapter._resolve_route_target(adapter, topology).reason_code == "point_snapped_to_polygon"
    assert adapter.route_target == Vec3(1.0, 1.0, 500.0)
