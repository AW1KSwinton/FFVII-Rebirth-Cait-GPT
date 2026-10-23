"""Route geometry regressions independent of any game location."""
from dataclasses import replace
import math

import pytest

from ff7r_agent.detour_route import DetourPortal, DetourTopologyGraph
from ff7r_agent.models import Vec3


def portal(x, low, high, ref=1):
    return DetourPortal(ref, ref + 1, ref + 1, 1, 3,
                        Vec3(x, high, 0), Vec3(x, low, 0), 128)


def endpoints(start, gates, end):
    return (DetourTopologyGraph._degenerate_portal(1, start), *gates,
            DetourTopologyGraph._degenerate_portal(len(gates) + 1, end))


@pytest.mark.parametrize("angle", [0, 37, 90, 180, 271])
@pytest.mark.parametrize("offset", [0, 17000])
def test_straight_corridor_does_not_emit_alternating_corners(angle, offset):
    start, end = Vec3(0, 0, 0), Vec3(1000, 0, 0)
    gates = endpoints(start, [portal(x, -100, 100, i + 1)
                             for i, x in enumerate(range(100, 1000, 100))], end)
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))

    def transform(p):
        return Vec3(c * p.x - s * p.y + offset, s * p.x + c * p.y - offset, p.z)

    gates = tuple(replace(p, left=transform(p.left), right=transform(p.right)) for p in gates)
    assert DetourTopologyGraph._funnel(gates) == (transform(start), transform(end))


@pytest.mark.parametrize("mirror", [1, -1])
def test_tightening_a_funnel_only_emits_the_obstructing_side(mirror):
    start, corner, end = Vec3(0, 0, 0), Vec3(100, 100, 0), Vec3(200, 250, 0)
    gates = endpoints(start, [portal(100, -100, 100), portal(150, 150, 250, 2)], end)
    if mirror < 0:
        def flip(p):
            return Vec3(p.x, -p.y, p.z)
        gates = tuple(replace(p, left=flip(p.right), right=flip(p.left)) for p in gates)
        start, corner, end = flip(start), flip(corner), flip(end)
    assert DetourTopologyGraph._funnel(gates) == (start, corner, end)


def test_insetting_is_bounded_and_does_not_change_portal_evidence():
    gates = endpoints(Vec3(0, 0, 0), [portal(100, -10, 10)], Vec3(200, 0, 0))
    adjusted = DetourTopologyGraph._inset_funnel_portals(gates, 0.5)
    assert adjusted[0] == gates[0] and adjusted[-1] == gates[-1]
    assert adjusted[1].left == Vec3(100, 5, 0)
    assert adjusted[1].right == Vec3(100, -5, 0)
    assert gates[1].left == Vec3(100, 10, 0)
    assert DetourTopologyGraph._inset_funnel_portals(gates, 0) == gates


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_inset_rejected(value):
    with pytest.raises(ValueError):
        DetourTopologyGraph._inset_funnel_portals((), value)
