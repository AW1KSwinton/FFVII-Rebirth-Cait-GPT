from __future__ import annotations

import pytest

from ff7r_agent.live_route_adapter import LiveAdapterConfig
from ff7r_agent.models import Vec3

from test_continuous_control import StreamingActuator
from test_live_route_adapter import make_adapter, make_observation


def pursuit_adapter(observations, follower_mode="ARRIVED_ON_FIRST"):
    actuator = StreamingActuator()
    config = LiveAdapterConfig(controller_mode='continuous', continuous_fast_follow=True, pursuit=True,
        post_arrival_samples=0, max_cycles=4, readiness_recovery_timeout_seconds=.2,
        preflight_telemetry_wait_seconds=.2)
    adapter, telemetry, _, _ = make_adapter(actuator=actuator, config=config, observations=observations,
                                            follower_mode=follower_mode)
    assert adapter.preflight()['movement_authorized']
    return adapter, actuator


def walk(xs, y=0.0):
    return [make_observation(sequence=i + 1, position=Vec3(x, y, 0.0), velocity_cm_s=Vec3(400.0, 0.0, 0.0))
            for i, x in enumerate(xs)]


def test_pursuit_config_requires_continuous_mode():
    with pytest.raises(ValueError):
        LiveAdapterConfig(controller_mode='pulse', pursuit=True)
    assert LiveAdapterConfig().pursuit is False


def test_pursuit_streams_renewed_holds_without_releasing_the_stick_and_arrives():
    adapter, actuator = pursuit_adapter(walk([0, 0, 50, 100, 150, 200, 250, 300, 360, 360, 360, 360]))
    result = adapter.run(dry_run=False)
    assert result['state'] == 'ARRIVED', result['reason']
    assert len(actuator.updates) >= 3
    sequences = [update['sequence'] for update in actuator.updates]
    assert sequences == sorted(set(sequences))
    # Holds are renewed back to back: no neutralization between them.
    assert len({update['neutralizations'] for update in actuator.updates}) == 1
    events = [item['event'] for item in result['timeline']]
    assert 'pursuit_started' in events and 'pursuit_arrived' in events


def test_pursuit_hands_over_when_cloud_is_off_the_route():
    # Starts on the route, then 5 m to its side: past the 3 m cross-track bound.
    samples = walk([0, 0, 50]) + [make_observation(sequence=4 + i, position=Vec3(100.0, 500.0, 0.0),
                                                   velocity_cm_s=Vec3(400.0, 0.0, 0.0)) for i in range(10)]
    adapter, actuator = pursuit_adapter(samples, follower_mode="FOLLOWING")
    adapter.run(dry_run=False, max_cycles=1)
    handed = [item for item in adapter.timeline if item.get('event') == 'pursuit_handed_over']
    assert handed and handed[0]['reason'] == 'off_route'


def test_pursuit_movement_counts_as_meaningful_route_progress():
    from ff7r_agent.runtime_commands import _route_result_has_meaningful_progress
    adapter, actuator = pursuit_adapter(walk([0, 0, 50, 100, 150, 200, 250, 300, 360, 360, 360, 360]))
    result = adapter.run(dry_run=False)
    assert _route_result_has_meaningful_progress(result)
