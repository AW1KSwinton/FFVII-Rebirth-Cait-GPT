import math
import pytest
from dataclasses import dataclass
from ff7r_agent.camera_follow import *

@dataclass(frozen=True)
class P: x: float; y: float; z: float=0.0

def enabled(**kw):
    if kw.get("camera_follow_deadband_deg") == 0 and "camera_follow_settle_hysteresis_deg" not in kw:
        kw["camera_follow_settle_hysteresis_deg"] = 0
    return CameraFollowController(CameraFollowConfig(camera_follow_enabled=True, **kw))


def test_camera_faces_the_npc_before_arrival_instead_of_the_final_route_heading():
    c = enabled(camera_follow_arrival_target=(1000, 200, 0))
    route = [P(0, 0), P(1000, 0)]
    far = c.command(route=route, position=P(100, 0), camera_yaw_deg=0)
    assert not far["arrival_focus"] and far["target_yaw_deg"] == 0
    closing = c.command(route=route, position=P(700, 0), camera_yaw_deg=0)
    assert closing["arrival_focus"] and closing["camera_input_x"] > 0
    assert closing["target_yaw_deg"] == pytest.approx(90.0)
    assert closing["remaining_route_m"] == pytest.approx(3.0)


def test_arrival_focus_uses_remaining_path_not_proximity_on_a_folded_route():
    c = enabled(camera_follow_arrival_target=(0, 300, 0))
    route = [P(0, 0), P(1000, 0), P(1000, 1000), P(0, 1000), P(0, 100)]
    near_final_but_early = c.command(route=route, position=P(0, 0), camera_yaw_deg=0)
    assert not near_final_but_early["arrival_focus"]
    assert near_final_but_early["remaining_route_m"] == 39.0
    assert near_final_but_early["target_yaw_deg"] == 0
    c.command(route=route, position=P(1000, 900), camera_yaw_deg=0)
    c.command(route=route, position=P(100, 1000), camera_yaw_deg=0)
    closing = c.command(route=route, position=P(0, 400), camera_yaw_deg=-90)
    assert closing["arrival_focus"] and closing["target_yaw_deg"] == 90


def test_large_arrival_turn_starts_earlier_at_speed_and_does_not_switch_back_mid_turn():
    c = enabled(camera_follow_arrival_target=(1000, 200, 0))
    route = [P(0, 0), P(1000, 0)]
    # Delayed native sweeps need more time than the requested rate alone.
    # At 6 m/s the view must start changing before the final five metres.
    first = c.command(route=route, position=P(100, 0), camera_yaw_deg=0, speed_m_s=6)
    assert first["arrival_focus"] and first["target_yaw_deg"] == 90
    later = c.command(route=route, position=P(200, 0), camera_yaw_deg=60, speed_m_s=6)
    assert later["arrival_focus"] and later["target_yaw_deg"] == 90


def test_large_arrival_turn_finishes_while_walking_with_gaps_between_native_sweeps():
    c = enabled(camera_follow_arrival_target=(4000, -200, 0))
    route = [P(0, 0), P(4000, 0)]
    yaw = 35.0
    focused_at = None
    # Reproduce the live Kalm turn: 125 degrees, accelerating out of a
    # corner, and only 30 degrees/s delivered after scheduling gaps.
    for x in range(1000, 4000, 300):
        command = c.command(route=route, position=P(x, 0), camera_yaw_deg=yaw,
                            speed_m_s=2.0 if x == 1000 else 6.0)
        if command["arrival_focus"]:
            focused_at = x if focused_at is None else focused_at
            error = shortest_yaw_error_deg(command["target_yaw_deg"], yaw)
            yaw += max(-15.0, min(15.0, error))
    assert focused_at == 1000
    assert abs(shortest_yaw_error_deg(-90, yaw)) <= 8


def test_pursuit_progress_can_supply_the_remaining_path_after_a_skipped_corner():
    c = enabled(camera_follow_arrival_target=(0, 300, 0))
    route = [P(0, 0), P(1000, 0), P(1000, 1000), P(0, 1000), P(0, 100)]
    result = c.command(route=route, position=P(0, 400), camera_yaw_deg=0,
                       remaining_route_m=3.0)
    assert result["arrival_focus"] and result["target_yaw_deg"] == 90
    assert result["remaining_route_m"] == 3.0


def test_arrival_framing_does_not_use_the_loose_travel_camera_deadband():
    c = enabled(camera_follow_deadband_deg=30, camera_follow_arrival_target=(1000, 200, 0))
    result = c.command(route=[P(0, 0), P(1000, 0)], position=P(700, 0), camera_yaw_deg=70)
    assert result["arrival_focus"] and result["camera_input_x"] > 0


@pytest.mark.parametrize("target", [(float("nan"), 0, 0), (0, 0), (0, 0, float("inf"))])
def test_arrival_camera_target_must_be_finite(target):
    with pytest.raises(ValueError):
        enabled(camera_follow_arrival_target=target)

def test_zero_error_no_input():
    c=enabled(); r=[P(0,0),P(1000,0)]
    assert c.command(route=r, position=P(0,0), camera_yaw_deg=0)["camera_input_x"]==0

def test_deadband():
    c=enabled(camera_follow_deadband_deg=10); r=[P(0,0),P(1000,100)]
    assert c.command(route=r, position=P(0,0), camera_yaw_deg=0)["camera_input_x"]==0

def test_positive_negative_and_wrap():
    c=enabled(camera_follow_deadband_deg=0)
    assert c.command(route=[P(0,0),P(0,1000)], position=P(0,0), camera_yaw_deg=0)["camera_input_x"]>0
    c=enabled(camera_follow_deadband_deg=0)
    assert c.command(route=[P(0,0),P(0,-1000)], position=P(0,0), camera_yaw_deg=0)["camera_input_x"]<0
    assert math.isclose(shortest_yaw_error_deg(179,-179),-2)
    assert math.isclose(shortest_yaw_error_deg(-179,179),2)

def test_clamp():
    c=enabled(camera_follow_deadband_deg=0,camera_follow_max_input=.2)
    out=c.command(route=[P(0,0),P(-1000,0)], position=P(0,0), camera_yaw_deg=0)
    assert abs(out["camera_input_x"])==.2

def test_lookahead_turns_toward_future_segment():
    route=[P(0,0),P(100,0),P(100,1000)]
    x,y=route_lookahead_target(route,P(0,0),5)
    assert x==100 and y>0
    yaw=target_yaw_for_route(route,P(0,0),5)
    assert 45 < yaw < 90

def test_neutral_when_stopped_or_disabled():
    assert enabled().neutral("route_stopped")["camera_input_x"]==0
    c=CameraFollowController()
    assert c.command(route=[P(0,0),P(100,0)], position=P(0,0), camera_yaw_deg=0)["camera_input_x"]==0

def test_sprint_uses_farther_lookahead():
    c=enabled(camera_follow_lookahead_m=3,camera_follow_sprint_lookahead_m=9)
    r=[P(0,0),P(400,0),P(400,1000)]
    normal=c.command(route=r,position=P(0,0),camera_yaw_deg=0,sprint=False)
    c=enabled(camera_follow_lookahead_m=3,camera_follow_sprint_lookahead_m=9)
    sprint=c.command(route=r,position=P(0,0),camera_yaw_deg=0,sprint=True)
    assert normal["lookahead_m"]==3 and sprint["lookahead_m"]==9
    assert abs(sprint["target_yaw_deg"]) > abs(normal["target_yaw_deg"])


def test_wraparound_command_is_small_and_signed():
    c = enabled(camera_follow_deadband_deg=0)
    route = [P(0, 0), P(-1000, 17)]  # target yaw is about +179 degrees
    out = c.command(route=route, position=P(0, 0), camera_yaw_deg=-179)
    assert out["camera_input_x"] < 0
    assert abs(out["camera_input_x"]) < 0.1
    assert abs(out["yaw_error_deg"]) < 5


def test_dense_straight_route_does_not_oscillate_sign():
    c = enabled()
    route = [P(i * 100, (1 if i % 2 else -1) * 2) for i in range(30)]
    camera = 0.0
    signs = []
    for i in range(40):
        position = P(i * 65.0, 0.0)
        out = c.command(route=route, position=position, camera_yaw_deg=camera)
        value = float(out["camera_input_x"])
        if value:
            signs.append(1 if value > 0 else -1)
            camera += value * 14.0
    assert not any(a != b for a, b in zip(signs, signs[1:]))


def test_appropriate_bend_changes_target_gradually():
    c = enabled(camera_follow_lookahead_m=3.0)
    route = [P(0, 0), P(500, 0), P(1000, 500)]
    targets = [target_yaw_for_route(route, P(x, 0), 3.0) for x in (0, 200, 400, 500)]
    assert targets == sorted(targets)
    assert targets[-1] > targets[0]


def test_progress_anchor_does_not_jump_to_later_crossing_segment():
    c = enabled(camera_follow_deadband_deg=0, camera_follow_lookahead_m=2.0)
    route = [
        P(0, 0),
        P(1000, 0),
        P(1000, 1000),
        P(500, 1000),
        P(500, -1000),  # Later leg crosses the first segment at x=500.
        P(1500, -1000),
    ]

    # The later vertical leg is geometrically closer here, but progress is
    # still halfway along the first segment, so camera-follow must stay on it.
    first = c.command(route=route, position=P(500, 10), camera_yaw_deg=0)
    assert first["route_segment_index"] == 0
    assert abs(first["target_yaw_deg"]) < 10

    near_corner = c.command(route=route, position=P(900, 0), camera_yaw_deg=0)
    assert near_corner["route_segment_index"] == 0

    after_corner = c.command(route=route, position=P(1000, 200), camera_yaw_deg=0)
    assert after_corner["route_segment_index"] >= 1


def test_neutralization_preserves_route_progress_anchor():
    c = enabled(camera_follow_deadband_deg=0, camera_follow_lookahead_m=2.0)
    route = [
        P(0, 0),
        P(1000, 0),
        P(1000, 1000),
        P(500, 1000),
        P(500, -1000),
        P(1500, -1000),
    ]

    c.command(route=route, position=P(1000, 200), camera_yaw_deg=0)
    c.command(route=route, position=P(800, 1000), camera_yaw_deg=0)
    later = c.command(route=route, position=P(500, 500), camera_yaw_deg=0)
    assert later["route_segment_index"] >= 3

    c.neutral("movement_pulse_completed")
    after_neutral = c.command(route=route, position=P(500, 10), camera_yaw_deg=0)
    assert after_neutral["route_segment_index"] >= 3
