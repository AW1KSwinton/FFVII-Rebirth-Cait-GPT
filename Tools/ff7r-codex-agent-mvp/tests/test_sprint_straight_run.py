from __future__ import annotations

import pytest

from ff7r_agent.live_route_adapter import straight_run_ahead_m
from ff7r_agent.models import Vec3


def line(*points):
    return tuple(Vec3(x * 100.0, y * 100.0, 0.0) for x, y in points)


def test_straight_run_spans_closely_spaced_waypoints():
    # Live at Costa del Sol waypoints were ~5 m apart; sprint needed 6 m to the next one.
    route = line((0, 0), (5, 0), (10, 0.3), (15, 0), (20, 0))
    assert straight_run_ahead_m(route, Vec3(100.0, 0.0, 0.0)) == pytest.approx(19.0, abs=0.05)


def test_straight_run_stops_at_a_turn():
    route = line((0, 0), (5, 0), (10, 0), (10, 5), (10, 10))
    assert straight_run_ahead_m(route, Vec3(0.0, 0.0, 0.0)) == pytest.approx(10.0)
    assert straight_run_ahead_m(route, Vec3(1000.0, 200.0, 0.0)) == pytest.approx(8.0)


def test_no_route_means_no_run():
    assert straight_run_ahead_m((), Vec3(0.0, 0.0, 0.0)) == 0.0
