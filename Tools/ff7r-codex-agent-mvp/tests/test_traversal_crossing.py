"""The walk across an off-mesh link: a fake character, a fake clock."""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from ff7r_agent import traversal_crossing as tc
from ff7r_agent.models import Vec3

START = Vec3(0.0, 0.0, 0.0)
END = Vec3(340.0, 0.0, -10.0)
TARGET = tc.CrossingTarget(
    link_class="step_over", link_ref=7, from_ref=1, to_ref=2, start=START, end=END,
)


class World:
    """A character that walks where it is pushed, sampled every ``dt`` seconds."""

    def __init__(self, *, speed_cm_s=400.0, dt=0.12, blocked_x=None, ready=lambda step: [],
                 lose_telemetry_at=None):
        self.now = 0.0
        self.position = START
        self.sequence = 1
        self.speed = speed_cm_s
        self.dt = dt
        self.blocked_x = blocked_x
        self.ready = ready
        self.lose_telemetry_at = lose_telemetry_at
        self.pushes = []
        self.events = []
        self.steps = 0
        self._heading = None

    def observation(self):
        return SimpleNamespace(position=self.position, sequence=self.sequence, monotonic_t=self.now, grounded=True)

    def push(self, heading, observation):
        self.pushes.append(heading)
        self._heading = heading

    def neutralize(self):
        self._heading = None

    def next_observation(self, after):
        self.steps += 1
        if self.lose_telemetry_at is not None and self.steps >= self.lose_telemetry_at:
            raise RuntimeError("telemetry_stale")
        self.now += self.dt
        if self._heading is not None:
            dx = math.cos(math.radians(self._heading)) * self.speed * self.dt
            dy = math.sin(math.radians(self._heading)) * self.speed * self.dt
            x = self.position.x + dx
            if self.blocked_x is not None:
                x = min(x, self.blocked_x)
            self.position = Vec3(x, self.position.y + dy, self.position.z + (END.z - START.z) * (dx / 340.0))
        self.sequence = after + 1
        return self.observation()

    def crossing(self):
        return tc.TraversalCrossing(
            monotonic=lambda: self.now,
            next_observation=self.next_observation,
            readiness=lambda observation: self.ready(self.steps),
            push=self.push,
            neutralize=self.neutralize,
            record=self.events.append,
        )


def test_a_character_pushed_toward_the_landing_crosses_and_the_result_says_so():
    world = World()
    outcome = world.crossing().cross(TARGET, world.observation())
    assert outcome.crossed and outcome.reason == tc.REASON_CROSSED
    assert outcome.evidence["commands"] > 0
    assert outcome.evidence["distance_to_end_m"] <= tc.CROSSING_LANDING_RADIUS_M
    assert all(abs(heading) < 1e-6 for heading in world.pushes)
    assert [event["event"] for event in world.events] == ["traversal_crossing_started", "traversal_departure_settling", "traversal_departure_aligned",
                                                       "traversal_crossing_finished"]
    assert world.events[0]["target"]["link_class"] == "step_over"


def test_a_lateral_start_is_aligned_before_attempting_the_crossing():
    world = World(speed_cm_s=100.0)
    world.position = Vec3(0.0, 70.0, 0.0)
    attempts = []

    def cross_push(heading, observation):
        attempts.append(observation.position)
        # Model the beam refusing an angled entry outside its departure.
        assert abs(observation.position.y) <= tc.CROSSING_DEPARTURE_RADIUS_M * 100.0
        world.push(heading, observation)

    crossing = tc.TraversalCrossing(monotonic=lambda: world.now, next_observation=world.next_observation,
                                  readiness=lambda _: [], push=cross_push, align_push=world.push,
                                  neutralize=world.neutralize,
                                  record=world.events.append)
    outcome = crossing.cross(TARGET, world.observation())
    assert outcome.crossed and outcome.evidence["departure_aligned"]
    assert attempts
    aligned = next(event for event in world.events if event["event"] == "traversal_departure_aligned")
    assert aligned["distance_to_start_m"] <= tc.CROSSING_DEPARTURE_RADIUS_M


def test_it_reaches_the_departure_before_aiming_at_the_landing():
    world = World()
    world.position = Vec3(0.0, -200.0, 0.0)
    world.crossing().cross(TARGET, world.observation())
    assert world.pushes[0] == pytest.approx(90.0)
    assert any(event["event"] == "traversal_departure_aligned" for event in world.events)


def test_a_character_that_is_already_on_the_landing_needs_no_push():
    world = World()
    world.position = Vec3(300.0, 0.0, -10.0)
    outcome = world.crossing().cross(TARGET, world.observation())
    assert outcome.crossed and outcome.evidence["commands"] == 0 and world.pushes == []


def test_passing_the_landing_between_two_samples_still_counts():
    # 12 m/s and 0.2 s samples: 2.4 m a step, wider than the landing disc.
    world = World(speed_cm_s=1200.0, dt=0.2)
    world.position = Vec3(100.0, 0.0, -10.0)
    outcome = world.crossing().cross(TARGET, world.observation())
    assert outcome.crossed or outcome.reason == tc.REASON_NOT_CROSSED
    landed, detail = tc.has_landed(TARGET, Vec3(440.0, 30.0, -10.0))
    assert landed and detail["along_link"] > 1.0
    assert not tc.has_landed(TARGET, Vec3(440.0, 400.0, -10.0))[0]


def test_the_wrong_height_is_not_a_landing():
    assert not tc.has_landed(TARGET, Vec3(340.0, 0.0, 400.0))[0]


def test_a_character_stopped_by_the_link_fails_with_the_specific_outcome_and_evidence():
    world = World(blocked_x=150.0)
    outcome = world.crossing().cross(TARGET, world.observation())
    assert not outcome.crossed and outcome.reason == tc.REASON_NOT_CROSSED
    evidence = outcome.evidence
    assert evidence["detail"] == "no_progress"
    assert evidence["position_last"]["x"] == 150.0
    assert evidence["distance_to_end_m"] == pytest.approx(1.9)
    assert evidence["closest_to_end_m"] == pytest.approx(1.9)
    assert evidence["progress_toward_end_m"] == pytest.approx(1.5)
    assert evidence["target"]["horizontal_length_m"] == pytest.approx(3.4)
    assert 0 < len(evidence["recent_samples"]) <= tc.CROSSING_RECENT_SAMPLES
    assert evidence["elapsed_seconds"] >= tc.CROSSING_STALL_WINDOW_SECONDS
    finished = world.events[-1]
    assert finished["event"] == "traversal_crossing_finished" and finished["crossed"] is False


def test_a_slow_crossing_is_cut_off_at_the_time_allowed():
    # Creeping forward fast enough to count as progress, too slowly to arrive.
    world = World(speed_cm_s=20.0, dt=0.2)
    outcome = world.crossing().cross(TARGET, world.observation())
    assert not outcome.crossed and outcome.reason == tc.REASON_NOT_CROSSED
    assert outcome.evidence["detail"] == "timeout"
    assert outcome.evidence["timeout_seconds"] == TARGET.timeout_seconds()
    assert TARGET.timeout_seconds() == tc.CROSSING_TIMEOUT_BASE_SECONDS + tc.CROSSING_TIMEOUT_PER_METRE_SECONDS * 3.4




class CoastingWorld(World):
    """A character that keeps walking for a few samples after the stick is released."""

    def __init__(self, *, coast_steps, **kwargs):
        super().__init__(**kwargs)
        self.coast_steps = coast_steps
        self._coast = 0
        self._last_heading = None

    def push(self, heading, observation):
        super().push(heading, observation)
        self._last_heading = heading

    def neutralize(self):
        super().neutralize()
        self._coast = self.coast_steps

    def next_observation(self, after):
        if self._heading is None and self._coast > 0:
            self._coast -= 1
            self._heading = self._last_heading
            observation = super().next_observation(after)
            self._heading = None
            return observation
        return super().next_observation(after)


def test_momentum_past_the_departure_point_still_settles_there():
    # Live: stops 0.1 m out drifted past 0.2 m; each push back overshot again.
    world = CoastingWorld(coast_steps=2, speed_cm_s=150.0, dt=0.12)
    world.position = Vec3(-100.0, 0.0, 0.0)
    outcome = world.crossing().cross(TARGET, world.observation())
    assert outcome.crossed and outcome.evidence["departure_aligned"] is True
    aligned = next(event for event in world.events if event["event"] == "traversal_departure_aligned")
    assert tc.CROSSING_DEPARTURE_RADIUS_M < aligned["distance_to_start_m"] <= tc.CROSSING_DEPARTURE_SETTLED_RADIUS_M
    assert sum(event["event"] == "traversal_departure_settling" for event in world.events) == 1

def test_the_walk_up_to_the_departure_point_adds_to_the_time_allowed():
    # The route follower hands over a few metres short of the link.
    world = World(speed_cm_s=20.0, dt=0.2)
    world.position = Vec3(-250.0, 0.0, 0.0)
    outcome = world.crossing().cross(TARGET, world.observation())
    expected = TARGET.timeout_seconds() + tc.CROSSING_TIMEOUT_PER_METRE_SECONDS * 2.5
    assert world.events[0]["timeout_seconds"] == pytest.approx(expected)
    assert outcome.evidence["timeout_seconds"] == pytest.approx(expected)

def test_losing_the_controls_for_a_moment_is_tolerated_but_a_menu_is_not():
    vault = World(ready=lambda step: ["not_controllable"] if 2 <= step <= 6 else [])
    assert vault.crossing().cross(TARGET, vault.observation()).crossed
    menu = World(ready=lambda step: ["menu_or_pause"] if step >= 2 else [])
    outcome = menu.crossing().cross(TARGET, menu.observation())
    assert not outcome.crossed and outcome.reason == tc.REASON_READINESS_LOST
    assert outcome.evidence["failures"] == ["menu_or_pause"]
    long_loss = World(speed_cm_s=100.0, ready=lambda step: ["not_controllable"] if step >= 2 else [])
    assert long_loss.crossing().cross(TARGET, long_loss.observation()).reason == tc.REASON_READINESS_LOST


def test_telemetry_that_stops_ends_the_crossing_with_that_reason():
    world = World(lose_telemetry_at=3)
    outcome = world.crossing().cross(TARGET, world.observation())
    assert not outcome.crossed and outcome.reason == tc.REASON_TELEMETRY_STALL
    assert "telemetry_stale" in outcome.evidence["error"]


@pytest.mark.parametrize("failure", ["menu_or_pause", "combat_active", "movement_lease_missing"])
def test_landing_proximity_does_not_override_a_readiness_failure(failure):
    world = World(ready=lambda _: [failure])
    world.position = END
    result = world.crossing().cross(TARGET, world.observation())
    assert not result.crossed and result.reason == tc.REASON_READINESS_LOST
    assert result.evidence["failures"] == [failure] and not world.pushes


def test_an_animation_inside_the_landing_radius_is_not_an_early_success():
    world = World(speed_cm_s=100.0, ready=lambda step: ["not_controllable"] if 20 <= step <= 26 else [])
    result = world.crossing().cross(TARGET, world.observation())
    assert result.crossed and world.steps > 26


def test_the_crossing_keeps_holding_until_the_exact_landing_is_proven():
    world = World()
    crossing = world.crossing()
    crossing.landing_ready = lambda observation: observation.position.x >= END.x
    result = crossing.cross(TARGET, world.observation())
    assert result.crossed and result.observation.position.x >= END.x


def test_unknown_ground_state_cannot_be_claimed_as_a_landing():
    world = World()
    world.position = END
    observation = world.observation()
    observation.grounded = None
    result = world.crossing().cross(TARGET, observation)
    assert not result.crossed and result.reason == tc.REASON_READINESS_LOST
    assert result.evidence["failures"] == ["traversal_ground_state_unavailable"]


def test_alignment_requires_a_fresh_stationary_sample_after_releasing_the_approach():
    world = World(speed_cm_s=100.0)
    world.position = Vec3(0.0, 70.0, 0.0)
    stops = []
    crossing = world.crossing()
    def stop():
        stops.append(world.sequence)
        world.neutralize()
    crossing.neutralize = stop
    result = crossing.cross(TARGET, world.observation())
    aligned = next(event for event in world.events if event["event"] == "traversal_departure_aligned")
    assert result.crossed and stops
    assert aligned["sequence"] > stops[-1]
    assert aligned["horizontal_speed_m_s"] <= tc.CROSSING_DEPARTURE_SPEED_M_S


def test_a_failed_push_is_not_swallowed():
    world = World()

    def refuse(heading, observation):
        raise RuntimeError("camera_yaw_missing")

    crossing = tc.TraversalCrossing(
        monotonic=lambda: world.now, next_observation=world.next_observation,
        readiness=lambda observation: [], push=refuse, neutralize=world.neutralize,
    )
    try:
        crossing.cross(TARGET, world.observation())
    except RuntimeError as exc:
        assert "camera_yaw_missing" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("the push error was swallowed")


# A gimmick (crack, crawl space, ladder) is walked into, then the game holds
# the character for longer than a vault's grace while moving it along.
CRACK = tc.CrossingTarget(link_class="crack", link_ref=-5, from_ref=1, to_ref=2,
                          start=START, end=Vec3(600.0, 0.0, 0.0))


class GimmickWorld(World):
    """Inside 50..550 cm the game has the controls: not grounded, not controllable."""

    def observation(self):
        inside = 50.0 <= self.position.x <= 550.0
        return SimpleNamespace(position=self.position, sequence=self.sequence, monotonic_t=self.now,
                               grounded=not inside)

    def next_observation(self, after):
        self.steps += 1
        self.now += self.dt
        if self._heading is not None:
            self.position = Vec3(self.position.x + math.cos(math.radians(self._heading)) * self.speed * self.dt,
                                 self.position.y, self.position.z)
        self.sequence = after + 1
        return self.observation()

    def crossing(self):
        return tc.TraversalCrossing(
            monotonic=lambda: self.now,
            next_observation=self.next_observation,
            readiness=lambda observation: [] if observation.grounded else ["not_controllable"],
            push=self.push,
            align_push=lambda heading, observation: self.events.append({"event": "slow_align"}) or self.push(heading, observation),
            neutralize=self.neutralize,
            record=self.events.append,
        )


def test_a_gimmick_is_walked_into_and_held_through_while_the_game_has_the_controls():
    world = GimmickWorld(speed_cm_s=100.0)
    world.position = Vec3(-250.0, 0.0, 0.0)
    outcome = world.crossing().cross(CRACK, world.observation())
    assert outcome.crossed, outcome.evidence
    names = [event["event"] for event in world.events]
    assert "traversal_gimmick_engaged" in names
    assert "traversal_departure_settling" not in names and "slow_align" not in names
    # Five seconds inside, well past a vault's 1.5 s grace.
    assert outcome.evidence["elapsed_seconds"] > 5.0


def test_a_gimmick_still_stops_when_the_game_leaves_gameplay():
    world = GimmickWorld(speed_cm_s=100.0)
    crossing = world.crossing()
    crossing.readiness = lambda observation: ["menu_or_pause"] if world.position.x > 200 else []
    outcome = crossing.cross(CRACK, world.observation())
    assert not outcome.crossed and outcome.reason == tc.REASON_READINESS_LOST


def test_a_ladder_is_allowed_its_climb_in_its_time():
    ladder = tc.CrossingTarget(link_class="ladder", link_ref=-6, from_ref=1, to_ref=2,
                               start=START, end=Vec3(130.0, 0.0, 810.0))
    assert ladder.gimmick
    assert ladder.timeout_seconds() == pytest.approx(
        tc.CROSSING_TIMEOUT_BASE_SECONDS + tc.CROSSING_TIMEOUT_PER_METRE_SECONDS * math.hypot(1.3, 8.1))
    assert not TARGET.gimmick


class SidewaysWorld(GimmickWorld):
    """Moves in 2D along the held heading; the gimmick releases at x 550."""

    def next_observation(self, after):
        self.steps += 1
        self.now += self.dt
        if self._heading is not None:
            step = self.speed * self.dt
            self.position = Vec3(self.position.x + math.cos(math.radians(self._heading)) * step,
                                 self.position.y + math.sin(math.radians(self._heading)) * step, self.position.z)
        self.sequence = after + 1
        return self.observation()


def test_a_gimmick_is_entered_from_its_departure_not_from_beside_it():
    # Live at the Kalm crack: 0.65 m to one side, heading for the far point,
    # walked along the wall instead of into the opening.
    world = SidewaysWorld(speed_cm_s=100.0)
    world.position = Vec3(-80.0, 65.0, 0.0)
    outcome = world.crossing().cross(CRACK, world.observation())
    assert outcome.crossed, outcome.evidence
    engaged = next(event for event in world.events if event["event"] == "traversal_gimmick_engaged")
    assert engaged["distance_to_start_m"] <= tc.GIMMICK_ENGAGE_RADIUS_M
    # Inside, the link's own direction is held.
    after = world.pushes[[e["event"] for e in world.events].index("traversal_gimmick_engaged") - 1:]
    assert all(abs(heading) < 1e-6 for heading in after[-5:])


def test_released_on_the_far_floor_short_of_its_trigger_is_landed():
    # The far point is the far trigger's centre: walking on to it re-enters.
    long_crack = tc.CrossingTarget(link_class="crack", link_ref=-5, from_ref=1, to_ref=2,
                                   start=START, end=Vec3(780.0, 0.0, 0.0))
    world = SidewaysWorld(speed_cm_s=100.0)
    outcome = world.crossing().cross(long_crack, world.observation())
    assert outcome.crossed, outcome.evidence
    assert 1.0 < outcome.evidence["distance_to_end_m"] <= tc.GIMMICK_LANDING_RADIUS_M


class LadderWorld(GimmickWorld):
    """Climbs 60 cm/s straight up while held, then steps off at the top."""

    def observation(self):
        climbing = self.position.z < 800.0 and self.position.x >= 0.0 and self.steps > 0
        return SimpleNamespace(position=self.position, sequence=self.sequence, monotonic_t=self.now,
                               grounded=not climbing)

    def next_observation(self, after):
        self.steps += 1
        self.now += self.dt
        if self._heading is not None:
            if self.position.z < 800.0:
                self.position = Vec3(self.position.x, self.position.y, self.position.z + 60.0 * self.dt)
            else:
                self.position = Vec3(self.position.x + 100.0 * self.dt, self.position.y, 800.0)
        self.sequence = after + 1
        return self.observation()


def test_a_ladder_climb_is_progress_though_it_moves_nowhere_across():
    ladder = tc.CrossingTarget(link_class="ladder", link_ref=-6, from_ref=1, to_ref=2,
                               start=START, end=Vec3(120.0, 0.0, 800.0))
    world = LadderWorld()
    outcome = world.crossing().cross(ladder, world.observation())
    assert outcome.crossed, outcome.evidence
    assert outcome.evidence["elapsed_seconds"] > tc.GIMMICK_STALL_WINDOW_SECONDS


def drop_target():
    return tc.CrossingTarget(link_class="drop", link_ref=8, from_ref=1, to_ref=2,
                             start=START, end=Vec3(340.0, 0.0, -10.0))


def test_a_drop_is_sprinted_off_from_near_its_ledge_without_stopping():
    """Live 2026-10-21: the Mt. Nibel drops' departure points sit at the edge."""
    world = World()
    world.position = Vec3(-70.0, 30.0, 0.0)  # 0.76 m short of the edge, off to one side
    outcome = world.crossing().cross(drop_target(), world.observation())
    assert outcome.crossed and outcome.evidence["departure_aligned"]
    events = [event["event"] for event in world.events]
    assert "traversal_ledge_engaged" in events and "traversal_departure_settling" not in events
    # Straight at the landing from where Cloud stood, never back to the point itself.
    assert world.pushes[0] == pytest.approx(math.degrees(math.atan2(-30.0, 410.0)), abs=0.5)


def test_a_ledge_further_than_a_metre_is_walked_up_to_first():
    world = World()
    world.position = Vec3(-300.0, 0.0, 0.0)
    outcome = world.crossing().cross(drop_target(), world.observation())
    assert outcome.crossed
    engaged = next(event for event in world.events if event["event"] == "traversal_ledge_engaged")
    assert engaged["distance_to_start_m"] <= tc.LEDGE_ENGAGE_RADIUS_M


def test_a_drop_landed_past_its_point_is_not_pushed_back():
    """Live 2026-10-21 (Chadley route): the sprint off a 3.6 m drop came down 2 m
    beyond the landing point and 1.6 m lower, and was pushed back up to it."""
    target = tc.CrossingTarget(link_class="drop", link_ref=9, from_ref=1, to_ref=2,
                               start=Vec3(-24525.0, -219082.0, 5405.0), end=Vec3(-25305.0, -218595.0, 5050.0))
    overshoot = Vec3(-25590.0, -218350.0, 4885.0)  # 3.8 m on, 1.65 m lower, on the line
    assert tc.has_landed(target, overshoot)[0]
    walked = tc.CrossingTarget(link_class="step_over", link_ref=9, from_ref=1, to_ref=2,
                               start=target.start, end=target.end)
    assert not tc.has_landed(walked, overshoot)[0]
    # Off to one side, or short of the point at the ledge, is still no landing.
    assert not tc.has_landed(target, Vec3(-25305.0, -219300.0, 5050.0))[0]
    assert not tc.has_landed(target, Vec3(-25050.0, -218750.0, 5050.0))[0]  # 3 m short
