"""Walking across an off-mesh traversal link: approach, face, push, verify.

The planner puts a link's departure and landing points into the route as two
consecutive waypoints.  A step-over has no portal to cross and no polygon
between them, so the closed-loop followers hand that one leg to this
controller. It first releases at the departure and proves two fresh samples
within 0.2 m at no more than 0.25 m/s, then aims along the
link toward the landing, keeps pushing (the adapter sprints across ledges
and low walls, which the game then takes by itself), and
reads every fresh sample:

* landed: within ``CROSSING_LANDING_RADIUS_M`` of the landing point (or past
  it along the link), on its level, controllable, and accepted by the
  adapter's landing-polygon proof;
* ``traversal_link_not_crossed``: the time allowed ran out, or nothing
  moved closer for ``CROSSING_STALL_WINDOW_S``.  The result carries the
  evidence (points, positions, distances, the last samples) so the failure
  can be read as what it is, not as a generic stall;
* ``traversal_readiness_lost`` / ``traversal_telemetry_stall``: the game left
  gameplay or stopped reporting.

A gimmick (a ladder, a crack, a crawl space; gimmick_links.py) is walked
into, not settled at: the game takes the character in when it reaches the
trigger, then moves it along while neither grounded nor controllable.  So
the controller pushes straight on from the departure, holds through the
whole action while it makes progress, and allows for the climb in its time.

It sends nothing itself: the adapter supplies the observation source, the
readiness check and the actuator, so this module stays free of the live
wiring and can be exercised with a fake character.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .models import Vec3
from .offmesh_links import GIMMICK_LINK_CLASSES


CROSSING_LANDING_RADIUS_M = 1.0
CROSSING_LANDING_VERTICAL_M = 1.5
# Past the landing point along the link, this close to its line, still lands.
CROSSING_PASSED_LATERAL_M = 1.5
CROSSING_TIMEOUT_BASE_SECONDS = 4.0
CROSSING_TIMEOUT_PER_METRE_SECONDS = 1.5
CROSSING_STALL_WINDOW_SECONDS = 2.5
CROSSING_MIN_PROGRESS_M = 0.35
# A vault can briefly take the controls away; menus, combat and the like do not.
CROSSING_NOT_CONTROLLABLE_GRACE_SECONDS = 1.5
CROSSING_TELEMETRY_STALL_SECONDS = 0.6
CROSSING_RECENT_SAMPLES = 12
CROSSING_DEPARTURE_RADIUS_M = 0.2
CROSSING_DEPARTURE_VERTICAL_M = 0.5
CROSSING_DEPARTURE_SPEED_M_S = 0.25
# Once settling has begun, walking momentum may carry the character this
# far from the departure point and still count (live: three stops 0.1 m
# out drifted past 0.2 m, and each push back overshot again).
CROSSING_DEPARTURE_SETTLED_RADIUS_M = 0.5

# Inside a gimmick the game has the controls; that is not a fault while
# the character keeps moving (the stall window still applies).
GIMMICK_TOLERATED_FAILURES = frozenset({"not_controllable", "interaction_active"})
# Close enough to the departure to push on into the trigger without settling
# (live at the Kalm crack 0.65 m to one side walked along the wall instead).
GIMMICK_ENGAGE_RADIUS_M = 0.3
GIMMICK_ENGAGE_VERTICAL_M = 1.0
# The game taking the character (not grounded) this near the departure means
# the action has started.
GIMMICK_TAKEN_RADIUS_M = 1.5
# Released by the game on the far floor this near the landing point is
# landed: the point is the far trigger's centre, and walking on to it enters
# the gimmick again (live at the Kalm crack, 1.2 m short, he went back in).
GIMMICK_LANDING_RADIUS_M = 3.0
# Entering a crack or mounting a ladder makes little progress for a moment.
GIMMICK_STALL_WINDOW_SECONDS = 5.0

# Steps and drops start at a ledge's edge, which Cloud's body may never get
# within CROSSING_DEPARTURE_RADIUS_M of (live at the Mt. Nibel cave drops the
# half-stick alignment circled the point and stalled). From this near, on the
# departure polygon, the sprint goes straight at the landing.
LEDGE_LINK_CLASSES = frozenset({"step_down", "step_up", "drop"})
LEDGE_ENGAGE_RADIUS_M = 1.0
LEDGE_ENGAGE_VERTICAL_M = 0.5
# Where a step or drop counts as landed (has_landed).
LEDGE_LANDING_RADIUS_M = 4.0
LEDGE_LANDING_VERTICAL_M = 2.5
LEDGE_PASSED_LATERAL_M = 3.0
LEDGE_PASSED_MAX_M = 6.0
LEDGE_SHORT_MAX_M = 1.0

REASON_CROSSED = "crossed"
REASON_NOT_CROSSED = "traversal_link_not_crossed"
REASON_READINESS_LOST = "traversal_readiness_lost"
REASON_TELEMETRY_STALL = "traversal_telemetry_stall"


@dataclass(frozen=True)
class CrossingTarget:
    """One link to cross, as the plan describes it."""

    link_class: str
    link_ref: int
    from_ref: int
    to_ref: int
    start: Vec3
    end: Vec3
    direction: str = "start_to_end"

    @property
    def horizontal_length_m(self) -> float:
        return math.hypot(self.end.x - self.start.x, self.end.y - self.start.y) / 100.0

    @property
    def vertical_delta_m(self) -> float:
        return (self.end.z - self.start.z) / 100.0

    @property
    def gimmick(self) -> bool:
        return self.link_class in GIMMICK_LINK_CLASSES

    @property
    def ledge(self) -> bool:
        """A step or drop: sprinted off from near the departure, no stop at it."""
        return self.link_class in LEDGE_LINK_CLASSES

    def timeout_seconds(self) -> float:
        # A ladder is mostly height: allow for its whole length.
        length = (math.hypot(self.horizontal_length_m, self.vertical_delta_m)
                  if self.gimmick else self.horizontal_length_m)
        return CROSSING_TIMEOUT_BASE_SECONDS + CROSSING_TIMEOUT_PER_METRE_SECONDS * length

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_class": self.link_class,
            "link_ref": self.link_ref,
            "from_ref": self.from_ref,
            "to_ref": self.to_ref,
            "direction": self.direction,
            "start": {"x": self.start.x, "y": self.start.y, "z": self.start.z},
            "end": {"x": self.end.x, "y": self.end.y, "z": self.end.z},
            "horizontal_length_m": self.horizontal_length_m,
            "vertical_delta_m": self.vertical_delta_m,
        }


@dataclass
class CrossingOutcome:
    crossed: bool
    reason: str
    observation: Any
    evidence: dict[str, Any] = field(default_factory=dict)


def _horizontal_m(first: Vec3, second: Vec3) -> float:
    return math.hypot(second.x - first.x, second.y - first.y) / 100.0


def along_and_lateral_m(start: Vec3, end: Vec3, point: Vec3) -> tuple[float, float]:
    """Fraction of the way from ``start`` to ``end`` and distance off the line."""
    dx, dy = end.x - start.x, end.y - start.y
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-9:
        return 0.0, _horizontal_m(start, point)
    t = ((point.x - start.x) * dx + (point.y - start.y) * dy) / length_sq
    lateral_cm = abs((point.x - start.x) * dy - (point.y - start.y) * dx) / math.sqrt(length_sq)
    return t, lateral_cm / 100.0


def has_landed(target: CrossingTarget, position: Vec3) -> tuple[bool, dict[str, float]]:
    """Whether ``position`` is on the landing: near the point, or past it on the line.

    A step or drop is sprinted off, and the sprint carries on past the
    point: live on the Chadley route Cloud came down 2 m beyond it and 1.6 m
    lower, and was turned round and pushed back up to it. Its landing is
    wider; the adapter's landing-polygon proof still has to agree."""
    distance = _horizontal_m(position, target.end)
    vertical = abs(position.z - target.end.z) / 100.0
    along, lateral = along_and_lateral_m(target.start, target.end, position)
    if target.ledge:
        # Near the point but never short of it: on a short link the departure
        # itself lies within the radius.
        short_m = (1.0 - along) * target.horizontal_length_m
        near = distance <= LEDGE_LANDING_RADIUS_M and short_m <= LEDGE_SHORT_MAX_M
        passed = along >= 1.0 and lateral <= LEDGE_PASSED_LATERAL_M and distance <= LEDGE_PASSED_MAX_M
        detail = {"distance_to_end_m": distance, "vertical_to_end_m": vertical, "along_link": along,
                  "lateral_m": lateral}
        return (near or passed) and vertical <= LEDGE_LANDING_VERTICAL_M, detail
    near = distance <= CROSSING_LANDING_RADIUS_M
    passed = along >= 1.0 and lateral <= CROSSING_PASSED_LATERAL_M and distance <= 2.0 * CROSSING_LANDING_RADIUS_M
    detail = {
        "distance_to_end_m": distance,
        "vertical_to_end_m": vertical,
        "along_link": along,
        "lateral_m": lateral,
    }
    return (near or passed) and vertical <= CROSSING_LANDING_VERTICAL_M, detail


class TraversalCrossing:
    """The closed loop that walks one link; the adapter supplies the wiring.

    ``next_observation(after_sequence)`` returns the next fresh sample or
    raises; ``readiness(observation)`` lists why the character cannot be
    driven (empty if it can); ``push(heading_degrees, observation)`` holds the
    stick toward a world heading (sprinting is the adapter's choice).
    ``neutralize`` releases the
    approach so its momentum cannot carry a position-only alignment into the
    crossing. The adapter supplies source and landing polygon predicates.
    """

    def __init__(
        self,
        *,
        monotonic: Callable[[], float],
        next_observation: Callable[[int], Any],
        readiness: Callable[[Any], Sequence[str]],
        push: Callable[[float, Any], None],
        align_push: Callable[[float, Any], None] | None = None,
        neutralize: Callable[[], None] | None = None,
        departure_ready: Callable[[Any], bool] | None = None,
        landing_ready: Callable[[Any], bool] | None = None,
        record: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.monotonic = monotonic
        self.next_observation = next_observation
        self.readiness = readiness
        self.push = push
        self.align_push = align_push or push
        self.neutralize = neutralize or (lambda: None)
        self.departure_ready = departure_ready or (lambda _: True)
        self.landing_ready = landing_ready or (lambda _: True)
        self.record = record or (lambda event: None)

    def cross(self, target: CrossingTarget, observation: Any) -> CrossingOutcome:
        started = self.monotonic()
        # The walk up to the departure point is part of the crossing too.
        timeout = target.timeout_seconds() + CROSSING_TIMEOUT_PER_METRE_SECONDS * _horizontal_m(
            observation.position, target.start)
        samples: deque[dict[str, float]] = deque(maxlen=CROSSING_RECENT_SAMPLES)
        first_position = observation.position
        commands = 0
        closest = math.inf
        window: deque[tuple[float, float]] = deque()
        not_controllable_since: float | None = None
        aligned = False
        settling = False
        settled_samples = 0
        previous: tuple[float, Vec3] | None = None
        last_crossing_heading: float | None = None
        gimmick = target.gimmick
        stall_window = GIMMICK_STALL_WINDOW_SECONDS if gimmick else CROSSING_STALL_WINDOW_SECONDS
        # Inside a gimmick hold the link's own direction: re-aiming at the far
        # point from beside the opening walks along the wall instead.
        link_heading = math.degrees(math.atan2(target.end.y - target.start.y, target.end.x - target.start.x))
        self.record({"event": "traversal_crossing_started", "t": started, "timeout_seconds": timeout,
                     "sequence": observation.sequence, "target": target.to_dict()})

        def outcome(crossed: bool, reason: str, current: Any, **detail: Any) -> CrossingOutcome:
            position = current.position
            landed, geometry = has_landed(target, position)
            evidence = {
                "target": target.to_dict(),
                "outcome": reason,
                "elapsed_seconds": self.monotonic() - started,
                "timeout_seconds": timeout,
                "commands": commands,
                "departure_aligned": aligned,
                "position_first": {"x": first_position.x, "y": first_position.y, "z": first_position.z},
                "position_last": {"x": position.x, "y": position.y, "z": position.z},
                "closest_to_end_m": closest if math.isfinite(closest) else None,
                "progress_toward_end_m": (
                    _horizontal_m(first_position, target.end) - geometry["distance_to_end_m"]
                ),
                **geometry,
                "recent_samples": list(samples),
                **detail,
            }
            self.record({"event": "traversal_crossing_finished", "t": self.monotonic(),
                         "crossed": crossed, "reason": reason, "sequence": current.sequence,
                         "evidence": evidence})
            return CrossingOutcome(crossed, reason, current, evidence)

        while True:
            now = self.monotonic()
            position = observation.position
            landed, geometry = has_landed(target, position)
            closest = min(closest, geometry["distance_to_end_m"])
            samples.append({
                "t": now, "x": position.x, "y": position.y, "z": position.z,
                "distance_to_end_m": geometry["distance_to_end_m"],
                "sequence": observation.sequence,
                "grounded": getattr(observation, "grounded", None),
                "controllable": getattr(observation, "controllable", None),
            })
            failures = list(self.readiness(observation))
            grounded = getattr(observation, "grounded", None)
            if grounded is None:
                failures.append("traversal_ground_state_unavailable")
            elif grounded is False and not failures:
                failures = ["not_controllable"]
            controls_available = not failures and grounded is True
            if gimmick and aligned and failures and set(failures) <= GIMMICK_TOLERATED_FAILURES:
                failures = []
            if failures == ["not_controllable"]:
                not_controllable_since = not_controllable_since if not_controllable_since is not None else now
                if now - not_controllable_since <= CROSSING_NOT_CONTROLLABLE_GRACE_SECONDS:
                    failures = []
            else:
                not_controllable_since = None
            if failures:
                return outcome(False, REASON_READINESS_LOST, observation, failures=failures)
            if gimmick and aligned:
                landed = (geometry["distance_to_end_m"] <= GIMMICK_LANDING_RADIUS_M
                          and geometry["vertical_to_end_m"] <= CROSSING_LANDING_VERTICAL_M)
            # Proximity during a vault, or a snapped source-side candidate,
            # does not prove a landing. Keep the hold until controls and the
            # adapter's exact landing-polygon proof agree.
            if landed and controls_available and self.landing_ready(observation):
                return outcome(True, REASON_CROSSED, observation)
            if now - started > timeout:
                return outcome(False, REASON_NOT_CROSSED, observation, detail="timeout")
            distance_to_start = _horizontal_m(position, target.start)
            vertical_to_start = abs(position.z - target.start.z) / 100.0
            if gimmick and not aligned:
                # Walk straight into the trigger: no stop at the departure.
                engaged = (distance_to_start <= GIMMICK_ENGAGE_RADIUS_M
                           and vertical_to_start <= GIMMICK_ENGAGE_VERTICAL_M
                           and controls_available and self.departure_ready(observation))
                taken = (distance_to_start <= GIMMICK_TAKEN_RADIUS_M
                         and vertical_to_start <= GIMMICK_ENGAGE_VERTICAL_M and grounded is False)
                if engaged or taken:
                    aligned = True
                    window.clear()
                    self.record({"event": "traversal_gimmick_engaged", "t": now,
                                 "sequence": observation.sequence, "distance_to_start_m": distance_to_start,
                                 "taken_by_game": taken and not engaged,
                                 "position": {"x": position.x, "y": position.y, "z": position.z}})
            if target.ledge and not aligned and controls_available:
                if (distance_to_start <= LEDGE_ENGAGE_RADIUS_M and vertical_to_start <= LEDGE_ENGAGE_VERTICAL_M
                        and self.departure_ready(observation)):
                    aligned = True
                    window.clear()
                    self.record({"event": "traversal_ledge_engaged", "t": now,
                                 "sequence": observation.sequence, "distance_to_start_m": distance_to_start,
                                 "position": {"x": position.x, "y": position.y, "z": position.z}})
            if not aligned and not target.ledge:
                departure_proven = self.departure_ready(observation)
                if (distance_to_start <= CROSSING_DEPARTURE_RADIUS_M and controls_available
                        and (abs(position.z - target.start.z) / 100.0 > CROSSING_DEPARTURE_VERTICAL_M
                             or not departure_proven)):
                    return outcome(False, REASON_READINESS_LOST, observation,
                                   failures=["traversal_departure_polygon_unproven"])
                at_departure = (
                    distance_to_start <= CROSSING_DEPARTURE_RADIUS_M
                    and abs(position.z - target.start.z) / 100.0 <= CROSSING_DEPARTURE_VERTICAL_M
                    and controls_available and departure_proven
                )
                speed = math.inf
                if previous is not None and now > previous[0]:
                    speed = _horizontal_m(previous[1], position) / (now - previous[0])
                    velocity = getattr(observation, "velocity_cm_s", None)
                    if velocity is not None:
                        speed = max(speed, math.hypot(velocity.x, velocity.y) / 100.0)
                near_departure = (
                    distance_to_start <= CROSSING_DEPARTURE_SETTLED_RADIUS_M
                    and abs(position.z - target.start.z) / 100.0 <= CROSSING_DEPARTURE_VERTICAL_M
                    and controls_available and departure_proven
                )
                settled_samples = (settled_samples + 1
                                   if near_departure and settling and speed <= CROSSING_DEPARTURE_SPEED_M_S else 0)
                if settled_samples >= 2:
                    aligned = True
                    window.clear()
                    self.record({"event": "traversal_departure_aligned", "t": now,
                                 "sequence": observation.sequence, "distance_to_start_m": distance_to_start,
                                 "horizontal_speed_m_s": speed,
                                 "position": {"x": position.x, "y": position.y, "z": position.z}})
                elif at_departure:
                    if not settling:
                        self.neutralize()
                        self.record({"event": "traversal_departure_settling", "t": now,
                                     "sequence": observation.sequence, "distance_to_start_m": distance_to_start})
                    settling = True
                elif not near_departure:
                    settling = False
            distance_to_goal = geometry["distance_to_end_m"] if aligned else distance_to_start
            if gimmick and aligned:
                # A climb makes its progress in height (live: the Kalm inn
                # ladder stopped 5 m up on horizontal "no progress").
                distance_to_goal = math.hypot(geometry["distance_to_end_m"], geometry["vertical_to_end_m"])
            window.append((now, distance_to_goal))
            while len(window) > 1 and now - window[1][0] >= stall_window:
                window.popleft()
            if (
                now - window[0][0] >= stall_window
                and window[0][1] - distance_to_goal < CROSSING_MIN_PROGRESS_M
            ):
                return outcome(False, REASON_NOT_CROSSED, observation, detail="no_progress")
            aim = target.end if aligned else target.start
            heading = math.degrees(math.atan2(aim.y - position.y, aim.x - position.x))
            if gimmick and aligned:
                heading = link_heading
            if aligned:
                # The animation can move the feet backwards or sideways;
                # retain the entering world heading until controls return.
                if not controls_available and last_crossing_heading is not None:
                    heading = last_crossing_heading
                self.push(heading, observation)
                last_crossing_heading = heading
                commands += 1
            elif (gimmick or target.ledge) and controls_available:
                # Full pace onto the departure; slowing to settle is for vaults.
                self.push(heading, observation)
                commands += 1
            elif not settling and controls_available:
                self.align_push(heading, observation)
                commands += 1
            elif not controls_available:
                self.neutralize()
            previous = (now, position)
            try:
                observation = self.next_observation(int(observation.sequence))
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed silently
                return outcome(False, REASON_TELEMETRY_STALL, observation, error=str(exc))
