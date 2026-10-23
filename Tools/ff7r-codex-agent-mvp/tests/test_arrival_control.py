from dataclasses import replace
from types import SimpleNamespace

import pytest

from ff7r_agent.live_route_adapter import AdapterState, LiveAdapterConfig
from ff7r_agent.models import Vec3
from ff7r_agent.runtime_commands import invoke_runtime
from test_live_route_adapter import make_adapter, make_observation


@pytest.mark.parametrize("distance,speed,age,expected_magnitude,expected_duration", [(3.03, 0.0, 0.05, 1.0, 300), (2.07, 4.1, 0.28, 0.0, 250), (1.30, 6.0, 0.29, 0.0, 250)])
def test_recorded_short_route_conditions_brake_before_full_pulse(distance, speed, age, expected_magnitude, expected_duration):
    adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(normal_stick_magnitude=1.0, control_period_seconds=0.3))
    observation = make_observation(telemetry_age_seconds=age, velocity_cm_s=Vec3(speed * 100, 0, 0))
    x, y, steering = adapter._steering(observation, {
        "desired_heading_deg": 0, "suggested_speed_band": "normal",
        "distance_to_final_destination_m": distance, "distance_m": distance,
    })
    assert steering["magnitude"] == expected_magnitude
    assert steering["command_duration_ms"] == expected_duration
    assert adapter.config.final_arrival_radius_m == 0.75
    if expected_magnitude == 0.0:
        assert x == 0 and y == 0
    else:
        assert x == 0 and y > 0


@pytest.mark.parametrize("outside", [False, True])
def test_arrival_waits_for_residual_motion_then_checks_original_radius(outside):
    adapter, telemetry, actuator, _ = make_adapter(config=LiveAdapterConfig(post_arrival_samples=2))
    assert adapter.preflight()["movement_authorized"]
    end = adapter.authorization.resolved_end
    first = adapter._last_observation.sequence
    def pose(seq, dx, speed):
        return make_observation(sequence=first + seq, position=Vec3(end.x + dx, end.y, end.z),
            position_reference="upright_capsule_bottom", grounded=True, velocity_cm_s=Vec3(speed, 0, 0))
    adapter._last_observation = pose(0, -30, 600)
    final = 76 if outside else 35
    telemetry.observations = [pose(1, -20, 600), pose(2, -20, 600),
        pose(3, final, 0), pose(4, final, 0), pose(5, final, 0)]
    telemetry.index = 0
    adapter._set_state(AdapterState.ARRIVED, "candidate_arrival")
    adapter._confirm_post_arrival()
    assert not actuator.commands
    assert adapter.timeline[-1]["confirmed"] is (not outside)
    settling = [e for e in adapter.timeline if e["event"] == "arrival_pose_settling"]
    assert not settling[0]["stable"]
    assert not settling[1]["stable"]  # Repeated pose with high velocity is not settled.
    assert adapter.config.final_arrival_radius_m == 0.75
    if outside:
        assert adapter.reason == "post_arrival_position_left_final_radius"


@pytest.mark.parametrize("ready,arguments", [(False, {}), (True, {"expected_sha256": "fake"})])
def test_identity_hashing_requires_readiness_and_rejects_overrides(ready, arguments):
    lifecycle = SimpleNamespace(verify_executable_identity=lambda _: pytest.fail("must not hash"))
    service = SimpleNamespace(session_health=lambda: {"gameplay_ready": ready})
    with pytest.raises(ValueError):
        invoke_runtime(service, lifecycle, None, "verify_executable_identity", arguments)
