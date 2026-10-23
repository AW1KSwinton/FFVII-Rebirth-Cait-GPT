from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from ff7r_agent.local_navigation import LocalNavigationExecutor, LocalNavigationLimits
from ff7r_agent.input import SimulationInput
from ff7r_agent.models import Character, GameState
from ff7r_agent.models import Rotation
from ff7r_agent.models import Vec3
from ff7r_agent.service import AgentService


@dataclass
class Position:
    x: float
    y: float
    z: float = 0.0


@dataclass
class Player:
    position: Position


@dataclass
class State:
    sequence: int
    player: Player
    map_id: str = "test-map"
    running: bool = True
    paused: bool = False
    in_menu: bool = False
    in_combat: bool = False
    target: object | None = None
    source: str = "simulation"


class ScriptedDriver:
    def __init__(self, *, steps: int = 5, step_cm: float = 100.0):
        self.state = State(1, Player(Position(0.0, 0.0)))
        self.steps = steps
        self.step_cm = step_cm
        self.execute_calls = []
        self.neutralise_calls = 0
        self.capture_calls = 0
        self.mcp_calls = 0

    def observe(self):
        return self.state

    def safety_reason(self, state, map_id):
        if not state.running:
            return "game_process_lost"
        if state.map_id != map_id:
            return "gameplay_state_changed"
        return None

    def execute_segment(self, state, cycle_index, maximum_ms, maximum_distance_m):
        self.execute_calls.append((cycle_index, maximum_ms, maximum_distance_m))
        if len(self.execute_calls) <= self.steps:
            self.state = State(
                self.state.sequence + 1,
                Player(Position(self.state.player.position.x + self.step_cm, 0.0)),
            )
        return {"ok": True, "decision": "segment_verified", "movement": {"ok": True}}

    def neutralise(self):
        self.neutralise_calls += 1

    def capture_terminal(self):
        self.capture_calls += 1
        return {"ok": True, "frame": "terminal"}


def make_executor(driver=None, clock=None):
    return LocalNavigationExecutor(driver or ScriptedDriver(), clock=clock or __import__("time").monotonic)


def test_one_invocation_executes_multiple_local_cycles_without_mcp():
    driver = ScriptedDriver(steps=5)
    result = make_executor(driver).run(
        target_position={"x": 1000.0, "y": 0.0, "z": 0.0},
        route_bearing_degrees=0.0,
        limits=LocalNavigationLimits(
            maximum_seconds=8.0,
            maximum_distance_m=12.0,
            maximum_cycles=5,
            maximum_segment_distance_m=2.0,
        ),
    )

    assert result["terminal_outcome"] == "maximum_cycles_reached"
    assert result["cycles_completed"] == 5
    assert result["movement_segments"] == 5
    assert result["useful_cycles_per_invocation"] == 5
    assert result["cumulative_distance_m"] == pytest.approx(5.0)
    assert len(driver.execute_calls) == 5
    assert driver.mcp_calls == 0
    assert driver.capture_calls == 0
    assert driver.neutralise_calls == 1


def test_goal_reached_stops_immediately_and_neutralises():
    driver = ScriptedDriver(steps=5)
    result = make_executor(driver).run(
        target_position={"x": 220.0, "y": 0.0, "z": 0.0},
        route_bearing_degrees=0.0,
        limits=LocalNavigationLimits(maximum_cycles=8),
    )

    assert result["terminal_outcome"] == "goal_reached"
    assert result["ok"] is True
    assert result["cycles_completed"] == 1
    assert driver.neutralise_calls == 1


def test_bare_target_without_route_returns_route_required_without_movement():
    driver = ScriptedDriver()
    result = make_executor(driver).run(
        target_position={"x": 1000.0, "y": 0.0, "z": 0.0},
        limits=LocalNavigationLimits(),
    )

    assert result["terminal_outcome"] == "route_required"
    assert driver.execute_calls == []
    assert driver.capture_calls == 1


def test_blocked_corridor_captures_at_most_one_terminal_frame():
    class BlockedDriver(ScriptedDriver):
        def execute_segment(self, *args):
            self.execute_calls.append(args[1])
            return {
                "ok": False,
                "decision": "blocked",
                "visual_evidence": {"ok": True, "image_path": "already-captured.bmp"},
            }

    driver = BlockedDriver()
    result = make_executor(driver).run(
        target_position={"x": 1000.0, "y": 0.0, "z": 0.0},
        route_bearing_degrees=0.0,
    )

    assert result["terminal_outcome"] == "blocked"
    assert result["cycles_completed"] == 1
    assert result["screenshot"] is None
    assert driver.capture_calls == 0


def test_rejected_movement_unverified_result_never_continues_and_neutralises():
    class RejectedDriver(ScriptedDriver):
        def execute_segment(self, state, cycle_index, maximum_ms, maximum_distance_m):
            self.execute_calls.append((cycle_index, maximum_ms, maximum_distance_m))
            self.state = State(
                self.state.sequence + 1,
                Player(Position(self.state.player.position.x + 100.0, 0.0)),
            )
            return {
                "ok": False,
                "replan_required": True,
                "decision": "movement_unverified_replan",
                "reason": "post-movement verification rejected the segment",
                "movement": {"ok": True, "displacement_m": 1.0},
            }

    driver = RejectedDriver(steps=5)
    result = make_executor(driver).run(
        target_position={"x": 5000.0, "y": 0.0, "z": 0.0},
        route_bearing_degrees=0.0,
        limits=LocalNavigationLimits(maximum_cycles=5),
    )

    assert result["terminal_outcome"] == "blocked"
    assert result["coordinator_decision"] == "movement_unverified_replan"
    assert result["coordinator_reason"] == "post-movement verification rejected the segment"
    assert result["terminal_reason"] == (
        "movement_unverified_replan: post-movement verification rejected the segment"
    )
    assert result["cycles_completed"] == 1
    assert result["movement_segments"] == 0
    assert result["horizontal_displacement_m"] == pytest.approx(1.0)
    assert len(driver.execute_calls) == 1
    assert driver.neutralise_calls == 1


def test_replan_required_with_positive_displacement_stops_without_second_segment():
    class ReplanDriver(ScriptedDriver):
        def execute_segment(self, state, cycle_index, maximum_ms, maximum_distance_m):
            self.execute_calls.append((cycle_index, maximum_ms, maximum_distance_m))
            self.state = State(
                self.state.sequence + 1,
                Player(Position(self.state.player.position.x + 125.0, 0.0)),
            )
            return {
                "replan_required": True,
                "decision": "post_movement_obstacle_replan",
                "reason": "fresh corridor evidence changed",
                "movement": {"ok": True},
            }

    driver = ReplanDriver(steps=5)
    result = make_executor(driver).run(
        target_position={"x": 5000.0, "y": 0.0, "z": 0.0},
        route_bearing_degrees=0.0,
        limits=LocalNavigationLimits(maximum_cycles=5),
    )

    assert result["terminal_outcome"] == "blocked"
    assert result["coordinator_decision"] == "post_movement_obstacle_replan"
    assert result["cycles_completed"] == 1
    assert len(driver.execute_calls) == 1
    assert driver.neutralise_calls == 1


def test_time_budget_is_enforced_before_an_unbounded_loop():
    now = [0.0]

    def clock():
        return now[0]

    class SlowDriver(ScriptedDriver):
        def execute_segment(self, *args):
            result = super().execute_segment(*args)
            now[0] += 0.75
            return result

    driver = SlowDriver(steps=20)
    result = make_executor(driver, clock=clock).run(
        target_position={"x": 5000.0, "y": 0.0, "z": 0.0},
        route_bearing_degrees=0.0,
        limits=LocalNavigationLimits(
            maximum_seconds=1.0,
            maximum_cycles=20,
        ),
    )

    assert result["terminal_outcome"] == "time_budget_exhausted"
    assert result["cycles_completed"] == 2


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("menu_opened", "menu_opened"),
        ("combat_started", "combat_started"),
        ("telemetry_stale", "telemetry_stale"),
        ("foreground_lost", "foreground_lost"),
        ("game_process_lost", "game_process_lost"),
        ("control_session_lost", "control_session_lost"),
        ("emergency_stop", "emergency_stop"),
    ],
)
def test_safety_reason_stops_before_movement(reason, expected):
    class UnsafeDriver(ScriptedDriver):
        def safety_reason(self, state, map_id):
            return reason

    driver = UnsafeDriver()
    result = make_executor(driver).run(
        target_position={"x": 1000.0, "y": 0.0, "z": 0.0},
        route_bearing_degrees=0.0,
    )

    assert result["terminal_outcome"] == expected
    assert driver.execute_calls == []
    assert driver.neutralise_calls == 1


def test_backend_exception_fails_closed_and_neutralises():
    class FailingDriver(ScriptedDriver):
        def execute_segment(self, *args):
            raise RuntimeError("actuator failed")

    driver = FailingDriver()
    result = make_executor(driver).run(
        target_position={"x": 1000.0, "y": 0.0, "z": 0.0},
        route_bearing_degrees=0.0,
    )

    assert result["terminal_outcome"] == "backend_error"
    assert "actuator failed" in result["terminal_reason"]
    assert driver.neutralise_calls == 1


def test_limits_reject_unbounded_requests():
    with pytest.raises(ValueError, match="maximum_seconds"):
        LocalNavigationLimits(maximum_seconds=31.0).validate()
    with pytest.raises(ValueError, match="maximum_segment_distance"):
        LocalNavigationLimits(maximum_segment_distance_m=3.1).validate()


def test_service_hosts_executor_under_one_action_lock_without_mcp_round_trips():
    class AdvancingTelemetry:
        def __init__(self):
            self.state = GameState(
                schema_version=1,
                sequence=1,
                timestamp_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                running=True,
                paused=False,
                in_menu=False,
                in_combat=False,
                location="test",
                objective="test",
                map_id="test-map",
                player=Character(
                    "Cloud", 10, 10, 1, 1,
                    position=Vec3(0.0, 0.0, 0.0),
                    rotation=Rotation(0.0, 0.0, 0.0),
                ),
                source="simulation",
            )

        def observe(self):
            return self.state

        def step(self):
            self.state = replace(
                self.state,
                sequence=self.state.sequence + 1,
                player=replace(
                    self.state.player,
                    position=Vec3(self.state.player.position.x + 100.0, 0.0, 0.0),
                ),
            )

    telemetry = AdvancingTelemetry()
    service = AgentService(telemetry, SimulationInput(), sleeper=lambda _: None)
    coordinator_calls = []

    def local_coordinator(**kwargs):
        coordinator_calls.append(kwargs)
        telemetry.step()
        return {"ok": True, "decision": "segment_verified", "movement": {"ok": True}}

    service.navigate_next_segment = local_coordinator
    result = service.run_local_navigation_episode(
        target_position={"x": 1000.0, "y": 0.0, "z": 0.0},
        route_bearing_degrees=0.0,
        maximum_cycles=3,
        maximum_segment_distance_m=2.0,
    )

    assert result["terminal_outcome"] == "maximum_cycles_reached"
    assert result["cycles_completed"] == 3
    assert len(coordinator_calls) == 3
    assert result["screenshot"] is None
    assert service._action_lock.acquire(blocking=False) is True
    service._action_lock.release()


def test_service_refreshes_target_route_leg_and_propagates_geometry_each_cycle():
    class AdvancingTelemetry:
        def __init__(self):
            self.state = GameState(
                schema_version=1,
                sequence=1,
                timestamp_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                running=True,
                paused=False,
                in_menu=False,
                in_combat=False,
                location="test",
                objective="test",
                map_id="test-map",
                player=Character(
                    "Cloud", 10, 10, 1, 1,
                    position=Vec3(0.0, 0.0, 0.0),
                    rotation=Rotation(0.0, 0.0, 0.0),
                ),
                source="simulation",
            )

        def observe(self):
            return self.state

        def step(self):
            self.state = replace(
                self.state,
                sequence=self.state.sequence + 1,
                player=replace(
                    self.state.player,
                    position=Vec3(
                        self.state.player.position.x + 100.0,
                        self.state.player.position.y,
                        0.0,
                    ),
                ),
            )

    telemetry = AdvancingTelemetry()
    service = AgentService(telemetry, SimulationInput(), sleeper=lambda _: None)
    route_resolutions = []
    coordinator_calls = []
    first_leg = {
        "bearing": 0.0,
        "route_plan": {"legs": ["first"]},
        "route_source": "known_world",
        "route_segment_start": {"x": 0.0, "y": 0.0, "z": 0.0},
        "route_segment_end": {"x": 100.0, "y": 0.0, "z": 0.0},
    }
    second_leg = {
        "bearing": 90.0,
        "route_plan": {"legs": ["second"]},
        "route_source": "known_world",
        "route_segment_start": {"x": 100.0, "y": 0.0, "z": 0.0},
        "route_segment_end": {"x": 100.0, "y": 100.0, "z": 0.0},
    }

    def resolve_route(state, target_id, explicit_bearing):
        route_resolutions.append((state.sequence, target_id, explicit_bearing))
        return first_leg if state.player.position.x < 100.0 else second_leg

    def local_coordinator(**kwargs):
        coordinator_calls.append(kwargs)
        telemetry.step()
        return {"ok": True, "decision": "segment_verified", "movement": {"ok": True}}

    service._route_leg_for_target = resolve_route
    service.navigate_next_segment = local_coordinator
    result = service.run_local_navigation_episode(
        target_id="route-target",
        target_position={"x": 1000.0, "y": 1000.0, "z": 0.0},
        maximum_cycles=2,
        maximum_segment_distance_m=2.0,
    )

    assert result["terminal_outcome"] == "maximum_cycles_reached"
    assert [call["route_bearing_degrees"] for call in coordinator_calls] == [0.0, 90.0]
    assert [call["route_segment_start"] for call in coordinator_calls] == [
        first_leg["route_segment_start"],
        second_leg["route_segment_start"],
    ]
    assert [call["route_segment_end"] for call in coordinator_calls] == [
        first_leg["route_segment_end"],
        second_leg["route_segment_end"],
    ]
    assert result["cycles"][0]["selected_bearing_degrees"] == 0.0
    assert result["cycles"][1]["selected_bearing_degrees"] == 90.0
    assert result["cycles"][1]["route_segment_end"] == second_leg["route_segment_end"]
    assert len(coordinator_calls) == 2
    assert len(route_resolutions) >= 3
