"""Offline dynamic-character obstacle tracking and route conflict geometry.

This module deliberately has no movement, runtime, route-adapter, or topology
dependencies.  It consumes immutable observations and returns evidence about a
route corridor.  A positive conflict is useful as a conservative stop/avoid
signal; a clear result is only clearance-authorizable when the observation is
fresh and has sufficient velocity evidence.

All geometry in this module is expressed in metres.  The current Rebirth
``ObservedActor.position`` values are Unreal centimetres, so the explicit
adapter at the bottom performs that conversion without changing the existing
telemetry models.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
from typing import Iterable, Sequence

from .models import Character, GameState, ObservedActor


_EPSILON = 1.0e-9


class ObstacleEvidenceState(str, Enum):
    """Evidence state of a tracked actor at a caller-supplied time."""

    CURRENT = "current"
    STALE = "stale"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class ConflictClassification(str, Enum):
    """Geometry result; UNKNOWN is deliberately distinct from clear."""

    CONFLICT = "conflict"
    NO_CONFLICT = "no_conflict"
    UNKNOWN = "unknown"


def _finite(value: float) -> bool:
    return math.isfinite(value)


def _require_finite(name: str, value: float) -> None:
    if not _finite(value):
        raise ValueError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class Point3M:
    """An immutable world-space point/vector in metres."""

    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        for name, value in (("x", self.x), ("y", self.y), ("z", self.z)):
            _require_finite(name, value)

    def __add__(self, other: "Point3M") -> "Point3M":
        return Point3M(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: "Point3M") -> "Point3M":
        return Point3M(self.x - other.x, self.y - other.y, self.z - other.z)

    def scale(self, factor: float) -> "Point3M":
        _require_finite("factor", factor)
        return Point3M(self.x * factor, self.y * factor, self.z * factor)

    def horizontal_length(self) -> float:
        return math.hypot(self.x, self.y)

    def length(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)


@dataclass(frozen=True, slots=True)
class DynamicObstacleObservation:
    """One immutable actor sample bound to one base telemetry sequence.

    ``radius`` is intentionally absent.  Position is measured evidence;
    obstacle geometry is supplied separately through :class:`ObstacleGeometry`.
    ``sampled_at_ms`` is a UTC epoch timestamp in milliseconds.  The existing
    sidecar only publishes second-precision ``timestamp_utc`` today; a future
    producer should provide the millisecond value directly.
    """

    actor_id: str
    base_sequence: int
    sampled_at_ms: int
    cloud_position_m: Point3M
    actor_position_m: Point3M
    relative_position_m: Point3M
    horizontal_distance_m: float
    vertical_separation_m: float
    velocity_mps: Point3M | None = None
    velocity_confidence: float = 0.0
    velocity_sample_count: int = 0
    actor_class: str | None = None
    actor_category: str | None = None
    observation_age_seconds: float = 0.0
    source_identity: str = ""

    def __post_init__(self) -> None:
        if not self.actor_id:
            raise ValueError("actor_id must not be empty")
        if not isinstance(self.base_sequence, int) or self.base_sequence < 0:
            raise ValueError("base_sequence must be a non-negative integer")
        if not isinstance(self.sampled_at_ms, int) or self.sampled_at_ms < 0:
            raise ValueError("sampled_at_ms must be a non-negative integer")
        if not self.source_identity:
            raise ValueError("source_identity must not be empty")
        for name, value in (
            ("horizontal_distance_m", self.horizontal_distance_m),
            ("vertical_separation_m", self.vertical_separation_m),
            ("velocity_confidence", self.velocity_confidence),
            ("observation_age_seconds", self.observation_age_seconds),
        ):
            _require_finite(name, value)
        if self.horizontal_distance_m < 0 or self.vertical_separation_m < 0:
            raise ValueError("distances must be non-negative")
        if not 0.0 <= self.velocity_confidence <= 1.0:
            raise ValueError("velocity_confidence must be between 0 and 1")
        if not isinstance(self.velocity_sample_count, int) or self.velocity_sample_count < 0:
            raise ValueError("velocity_sample_count must be a non-negative integer")
        if self.observation_age_seconds < 0:
            raise ValueError("observation_age_seconds must be non-negative")
        derived = self.actor_position_m - self.cloud_position_m
        if (
            abs(derived.x - self.relative_position_m.x) > 1.0e-5
            or abs(derived.y - self.relative_position_m.y) > 1.0e-5
            or abs(derived.z - self.relative_position_m.z) > 1.0e-5
        ):
            raise ValueError("relative_position_m must equal actor_position_m - cloud_position_m")
        if abs(self.horizontal_distance_m - derived.horizontal_length()) > 1.0e-5:
            raise ValueError("horizontal_distance_m does not match relative position")
        if abs(self.vertical_separation_m - abs(derived.z)) > 1.0e-5:
            raise ValueError("vertical_separation_m does not match relative position")
        if self.velocity_mps is None:
            if self.velocity_confidence != 0.0 or self.velocity_sample_count != 0:
                raise ValueError("velocity evidence requires a velocity estimate")


@dataclass(frozen=True, slots=True)
class DynamicObstacleSnapshot:
    """A complete actor snapshot from one producer publication."""

    base_sequence: int
    sampled_at_ms: int
    observations: tuple[DynamicObstacleObservation, ...]
    source_identity: str
    # ``False`` is the safe default: a producer must explicitly prove that its
    # actor set is complete before a clear route result can be authorizable.
    complete: bool = False
    observed_actor_count: int = 0
    accepted_actor_count: int = 0
    rejected_actor_count: int = 0
    rejection_reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.base_sequence, int) or self.base_sequence < 0:
            raise ValueError("snapshot base_sequence must be non-negative")
        if not isinstance(self.sampled_at_ms, int) or self.sampled_at_ms < 0:
            raise ValueError("snapshot sampled_at_ms must be non-negative")
        if not self.source_identity:
            raise ValueError("snapshot source_identity must not be empty")
        if not isinstance(self.observations, tuple):
            raise ValueError("snapshot observations must be a tuple")
        if not isinstance(self.complete, bool):
            raise ValueError("snapshot complete must be Boolean")
        for name, value in (
            ("observed_actor_count", self.observed_actor_count),
            ("accepted_actor_count", self.accepted_actor_count),
            ("rejected_actor_count", self.rejected_actor_count),
        ):
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"snapshot {name} must be a non-negative integer")
        if self.accepted_actor_count != len(self.observations):
            raise ValueError("accepted_actor_count must match observations")
        if self.observed_actor_count != self.accepted_actor_count + self.rejected_actor_count:
            raise ValueError("snapshot actor counts do not reconcile")
        if not isinstance(self.rejection_reason_codes, tuple):
            raise ValueError("rejection_reason_codes must be a tuple")
        if any(not isinstance(reason, str) or not reason for reason in self.rejection_reason_codes):
            raise ValueError("snapshot rejection reasons must be non-empty strings")
        if tuple(sorted(set(self.rejection_reason_codes))) != self.rejection_reason_codes:
            raise ValueError("snapshot rejection reasons must be deterministic and unique")
        if self.complete and (
            self.rejected_actor_count != 0
            or self.observed_actor_count != self.accepted_actor_count
            or self.rejection_reason_codes
        ):
            raise ValueError("complete snapshot cannot contain rejected or unaccounted actors")
        if self.rejected_actor_count and not self.rejection_reason_codes:
            raise ValueError("rejected actors require rejection evidence")
        seen: set[str] = set()
        for observation in self.observations:
            if observation.base_sequence != self.base_sequence:
                raise ValueError("observation sequence does not match snapshot")
            if observation.sampled_at_ms != self.sampled_at_ms:
                raise ValueError("observation timestamp does not match snapshot")
            if observation.source_identity != self.source_identity:
                raise ValueError("observation source identity does not match snapshot")
            if observation.actor_id in seen:
                raise ValueError("snapshot contains duplicate actor IDs")
            seen.add(observation.actor_id)


class StaticLearningDynamicState(str, Enum):
    """Typed dynamic evidence state consumed by static-world learning."""

    ABSENT = "absent"
    BLOCKING = "blocking_dynamic"
    CLEAR_COMPLETE = "clear_complete"
    INCOMPLETE = "incomplete"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DynamicObstacleStaticEvidence:
    """A bounded, snapshot-bound answer for one static-learning attempt.

    This is deliberately derived from :class:`DynamicObstacleSnapshot`; it is
    not a second actor-classification dictionary.  A positive current actor
    conflict blocks static promotion, while a clear answer is authorizable
    only when the producer proved completeness.  Missing, incomplete and
    stale evidence remain explicit so callers can require stronger repeated
    static confirmation without treating absence as proof of static identity.
    """

    state: StaticLearningDynamicState
    reason_code: str
    source_identity: str | None = None
    base_sequence: int | None = None
    sampled_at_ms: int | None = None
    blocking_actor_ids: tuple[str, ...] = ()
    snapshot_complete: bool = False
    age_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, StaticLearningDynamicState):
            raise ValueError("dynamic static-learning state must be typed")
        if not self.reason_code:
            raise ValueError("dynamic static-learning reason must not be empty")
        if tuple(sorted(set(self.blocking_actor_ids))) != self.blocking_actor_ids:
            raise ValueError("blocking actor IDs must be sorted and unique")
        if self.base_sequence is not None and (not isinstance(self.base_sequence, int) or self.base_sequence < 0):
            raise ValueError("dynamic evidence sequence is invalid")
        if self.sampled_at_ms is not None and (not isinstance(self.sampled_at_ms, int) or self.sampled_at_ms < 0):
            raise ValueError("dynamic evidence timestamp is invalid")
        if self.age_seconds is not None and (not math.isfinite(self.age_seconds) or self.age_seconds < 0.0):
            raise ValueError("dynamic evidence age is invalid")
        if self.state == StaticLearningDynamicState.BLOCKING and not self.blocking_actor_ids:
            raise ValueError("blocking dynamic evidence requires actor IDs")

    @property
    def blocks_static_persistence(self) -> bool:
        return self.state == StaticLearningDynamicState.BLOCKING

    @property
    def completeness_proven(self) -> bool:
        return self.state == StaticLearningDynamicState.CLEAR_COMPLETE

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "reason_code": self.reason_code,
            "source_identity": self.source_identity,
            "base_sequence": self.base_sequence,
            "sampled_at_ms": self.sampled_at_ms,
            "blocking_actor_ids": list(self.blocking_actor_ids),
            "snapshot_complete": self.snapshot_complete,
            "age_seconds": self.age_seconds,
            "blocks_static_persistence": self.blocks_static_persistence,
            "completeness_proven": self.completeness_proven,
        }


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    """Safety bounds for offline tracking.

    These defaults are engineering guardrails, not discovered Rebirth
    constants.  They must be validated against future high-rate telemetry
    before they can support a live controller.
    """

    max_observation_age_seconds: float = 0.75
    disappearance_expiry_seconds: float = 1.50
    minimum_velocity_interval_seconds: float = 0.05
    maximum_velocity_interval_seconds: float = 2.50
    maximum_believable_velocity_mps: float = 8.0
    maximum_prediction_horizon_seconds: float = 3.0

    def __post_init__(self) -> None:
        for name, value in (
            ("max_observation_age_seconds", self.max_observation_age_seconds),
            ("disappearance_expiry_seconds", self.disappearance_expiry_seconds),
            ("minimum_velocity_interval_seconds", self.minimum_velocity_interval_seconds),
            ("maximum_velocity_interval_seconds", self.maximum_velocity_interval_seconds),
            ("maximum_believable_velocity_mps", self.maximum_believable_velocity_mps),
            ("maximum_prediction_horizon_seconds", self.maximum_prediction_horizon_seconds),
        ):
            _require_finite(name, value)
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.disappearance_expiry_seconds < self.max_observation_age_seconds:
            raise ValueError("disappearance expiry must not precede stale age")
        if self.maximum_velocity_interval_seconds < self.minimum_velocity_interval_seconds:
            raise ValueError("maximum velocity interval must not precede minimum interval")


@dataclass(frozen=True, slots=True)
class TrackerRejection:
    actor_id: str | None
    base_sequence: int | None
    reason_code: str


@dataclass(frozen=True, slots=True)
class TrackedObstacle:
    """A point-in-time view of one tracker record."""

    actor_id: str
    observation: DynamicObstacleObservation
    evidence_state: ObstacleEvidenceState
    age_seconds: float
    missing_snapshot_count: int
    velocity_evidence: str

    @property
    def clearance_authorizable(self) -> bool:
        """Whether a clear result may be used as evidence, not a command."""

        return (
            self.evidence_state == ObstacleEvidenceState.CURRENT
            and self.observation.velocity_mps is not None
            and self.observation.velocity_confidence > 0.0
        )


@dataclass(frozen=True, slots=True)
class TrackerUpdate:
    accepted_actor_ids: tuple[str, ...]
    rejected: tuple[TrackerRejection, ...]
    snapshot_accepted: bool
    reason_code: str
    tracks: tuple[TrackedObstacle, ...]


@dataclass(slots=True)
class _TrackRecord:
    observation: DynamicObstacleObservation
    missing_snapshot_count: int = 0
    velocity_evidence: str = "none"


class DynamicObstacleTracker:
    """Deterministic, bounded tracker for successive actor snapshots."""

    def __init__(self, config: TrackerConfig | None = None) -> None:
        self.config = config or TrackerConfig()
        self._tracks: dict[str, _TrackRecord] = {}
        self._last_snapshot_sequence: int | None = None
        self._last_snapshot_time_ms: int | None = None

    def update(self, snapshot: DynamicObstacleSnapshot, *, now_ms: int | None = None) -> TrackerUpdate:
        """Accept one strictly newer snapshot and derive bounded velocity evidence."""

        if self._last_snapshot_sequence is not None:
            if snapshot.base_sequence <= self._last_snapshot_sequence:
                return self._rejected_snapshot(
                    snapshot,
                    "duplicate_snapshot" if snapshot.base_sequence == self._last_snapshot_sequence
                    else "out_of_order_snapshot",
                    now_ms,
                )
            if snapshot.sampled_at_ms < (self._last_snapshot_time_ms or 0):
                return self._rejected_snapshot(snapshot, "out_of_order_timestamp", now_ms)

        current_ids = {item.actor_id for item in snapshot.observations}
        for actor_id, record in self._tracks.items():
            if actor_id not in current_ids:
                record.missing_snapshot_count += 1

        accepted: list[str] = []
        rejected: list[TrackerRejection] = []
        for observation in snapshot.observations:
            previous = self._tracks.get(observation.actor_id)
            if previous is not None:
                previous_observation = previous.observation
                if observation.base_sequence <= previous_observation.base_sequence:
                    rejected.append(TrackerRejection(
                        observation.actor_id,
                        observation.base_sequence,
                        "duplicate_or_out_of_order_actor_sample",
                    ))
                    continue
                if observation.sampled_at_ms < previous_observation.sampled_at_ms:
                    rejected.append(TrackerRejection(
                        observation.actor_id,
                        observation.base_sequence,
                        "out_of_order_actor_timestamp",
                    ))
                    continue
                observation, velocity_evidence = self._derive_velocity(previous, observation)
            else:
                velocity_evidence = "insufficient_samples"
            self._tracks[observation.actor_id] = _TrackRecord(
                observation=observation,
                missing_snapshot_count=0,
                velocity_evidence=velocity_evidence,
            )
            accepted.append(observation.actor_id)

        self._last_snapshot_sequence = snapshot.base_sequence
        self._last_snapshot_time_ms = snapshot.sampled_at_ms
        return TrackerUpdate(
            accepted_actor_ids=tuple(accepted),
            rejected=tuple(rejected),
            snapshot_accepted=True,
            reason_code="accepted",
            tracks=self.tracks(now_ms=now_ms if now_ms is not None else snapshot.sampled_at_ms),
        )

    def tracks(self, *, now_ms: int) -> tuple[TrackedObstacle, ...]:
        """Return bounded evidence states; stale/expired records are retained only as diagnostics."""

        if not isinstance(now_ms, int) or now_ms < 0:
            raise ValueError("now_ms must be a non-negative integer")
        result: list[TrackedObstacle] = []
        for actor_id in sorted(self._tracks):
            record = self._tracks[actor_id]
            age = (now_ms - record.observation.sampled_at_ms) / 1000.0
            if age < 0:
                state = ObstacleEvidenceState.UNKNOWN
                bounded_age = 0.0
            elif age > self.config.disappearance_expiry_seconds:
                state = ObstacleEvidenceState.EXPIRED
                bounded_age = age
            elif age > self.config.max_observation_age_seconds:
                state = ObstacleEvidenceState.STALE
                bounded_age = age
            else:
                state = ObstacleEvidenceState.CURRENT
                bounded_age = age
            observation = replace(record.observation, observation_age_seconds=bounded_age)
            result.append(TrackedObstacle(
                actor_id=actor_id,
                observation=observation,
                evidence_state=state,
                age_seconds=bounded_age,
                missing_snapshot_count=record.missing_snapshot_count,
                velocity_evidence=record.velocity_evidence,
            ))
        return tuple(result)

    def current_obstacles(self, *, now_ms: int) -> tuple[TrackedObstacle, ...]:
        """Return only fresh records; stale and expired actors cannot be current truth."""

        return tuple(
            item for item in self.tracks(now_ms=now_ms)
            if item.evidence_state == ObstacleEvidenceState.CURRENT
        )

    def _derive_velocity(
        self,
        previous: _TrackRecord,
        observation: DynamicObstacleObservation,
    ) -> tuple[DynamicObstacleObservation, str]:
        previous_observation = previous.observation
        interval = (observation.sampled_at_ms - previous_observation.sampled_at_ms) / 1000.0
        if interval < self.config.minimum_velocity_interval_seconds:
            return replace(observation), "insufficient_time_separation"
        if interval > self.config.maximum_velocity_interval_seconds:
            return replace(observation), "velocity_gap_too_large"
        displacement = observation.actor_position_m - previous_observation.actor_position_m
        velocity = displacement.scale(1.0 / interval)
        speed = velocity.length()
        if speed > self.config.maximum_believable_velocity_mps:
            return replace(observation), "impossible_velocity"
        sample_count = previous_observation.velocity_sample_count + 1
        confidence = min(1.0, 0.5 + 0.25 * max(0, sample_count - 1))
        return replace(
            observation,
            velocity_mps=velocity,
            velocity_confidence=confidence,
            velocity_sample_count=sample_count,
        ), "derived_velocity"

    def _rejected_snapshot(
        self,
        snapshot: DynamicObstacleSnapshot,
        reason_code: str,
        now_ms: int | None,
    ) -> TrackerUpdate:
        return TrackerUpdate(
            accepted_actor_ids=(),
            rejected=tuple(
                TrackerRejection(item.actor_id, snapshot.base_sequence, reason_code)
                for item in snapshot.observations
            ) or (TrackerRejection(None, snapshot.base_sequence, reason_code),),
            snapshot_accepted=False,
            reason_code=reason_code,
            tracks=self.tracks(now_ms=now_ms if now_ms is not None else snapshot.sampled_at_ms),
        )


@dataclass(frozen=True, slots=True)
class ObstacleGeometry:
    """Explicit obstacle geometry, kept separate from measured position."""

    radius_m: float
    radius_source: str
    additional_clearance_m: float = 0.0

    def __post_init__(self) -> None:
        _require_finite("radius_m", self.radius_m)
        _require_finite("additional_clearance_m", self.additional_clearance_m)
        if self.radius_m <= 0 or self.additional_clearance_m < 0:
            raise ValueError("radius must be positive and additional clearance non-negative")
        if not self.radius_source:
            raise ValueError("radius_source must identify configuration provenance")

    @property
    def effective_radius_m(self) -> float:
        return self.radius_m + self.additional_clearance_m


@dataclass(frozen=True, slots=True)
class RouteConflictAnalysis:
    actor_id: str
    classification: ConflictClassification
    reason_code: str
    nearest_segment_index: int | None
    along_route_m: float | None
    lateral_distance_m: float | None
    longitudinal_distance_ahead_m: float | None
    predicted_closest_approach_m: float | None
    time_to_conflict_seconds: float | None
    vertical_separation_m: float | None
    observation_age_seconds: float
    velocity_confidence: float
    effective_radius_m: float
    authorizable: bool


@dataclass(frozen=True, slots=True)
class ObstacleSetAnalysis:
    classification: ConflictClassification
    reason_code: str
    selected: RouteConflictAnalysis | None
    analyses: tuple[RouteConflictAnalysis, ...]
    authorizable: bool


@dataclass(frozen=True, slots=True)
class _RouteSegment:
    index: int
    start: Point3M
    end: Point3M
    cumulative_start_m: float
    length_m: float
    direction: Point3M

    @property
    def cumulative_end_m(self) -> float:
        return self.cumulative_start_m + self.length_m


def observation_from_observed_actor(
    actor: ObservedActor,
    cloud: Character,
    *,
    base_sequence: int,
    sampled_at_ms: int,
    source_identity: str,
    observation_age_seconds: float = 0.0,
    actor_class: str | None = None,
    actor_category: str | None = None,
) -> DynamicObstacleObservation:
    """Adapt existing ``ObservedActor``/``Character`` interfaces into metres.

    The actor ``kind`` remains the raw category.  ``actor_class`` records the
    conservative party/NPC classification used by static-learning checks; an
    unrecognised actor remains ``unknown`` and therefore cannot be treated as
    safe clearance evidence.
    """

    if actor.position is None or cloud.position is None:
        raise ValueError("actor and Cloud must both have world positions")
    actor_position = Point3M(actor.position.x / 100.0, actor.position.y / 100.0, actor.position.z / 100.0)
    cloud_position = Point3M(cloud.position.x / 100.0, cloud.position.y / 100.0, cloud.position.z / 100.0)
    relative = actor_position - cloud_position
    return DynamicObstacleObservation(
        actor_id=actor.id,
        base_sequence=base_sequence,
        sampled_at_ms=sampled_at_ms,
        cloud_position_m=cloud_position,
        actor_position_m=actor_position,
        relative_position_m=relative,
        horizontal_distance_m=relative.horizontal_length(),
        vertical_separation_m=abs(relative.z),
        actor_class=actor_class,
        actor_category=actor_category,
        observation_age_seconds=observation_age_seconds,
        source_identity=source_identity,
    )


_PARTY_KIND_LABELS = frozenset({"party", "companion", "ally", "player"})
_NPC_KIND_LABELS = frozenset({"npc", "civilian", "townsfolk", "character", "human"})
_STATIC_LEARNING_ACTOR_RADIUS_M = 2.0


def _identity_token(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().casefold()
    return text or None


def _party_identity_tokens(state: GameState) -> set[str]:
    tokens: set[str] = set()
    for member in tuple(getattr(state, "party", ())):
        for value in (getattr(member, "name", None), getattr(member, "actor_id", None)):
            token = _identity_token(value)
            if token is not None:
                tokens.add(token)
    return tokens


def _actor_classification(actor: ObservedActor, party_tokens: set[str]) -> str:
    actor_tokens = {
        token for token in (
            _identity_token(getattr(actor, "id", None)),
            _identity_token(getattr(actor, "name", None)),
        ) if token is not None
    }
    if actor_tokens & party_tokens:
        return "party"
    kind = _identity_token(getattr(actor, "kind", None))
    if kind in _PARTY_KIND_LABELS:
        return "party"
    if kind in _NPC_KIND_LABELS:
        return "npc"
    return "unknown"


def snapshot_from_game_state(
    state: GameState,
    *,
    source_identity: str = "surroundings.json",
    now_ms: int | None = None,
) -> DynamicObstacleSnapshot:
    """Build a snapshot from the existing immutable ``GameState`` interfaces.

    Actors without a finite world position are omitted from the represented
    observations, but their presence is retained in the rejection counts and
    reason codes. The current ``GameState`` interface does not prove that its
    actor list is complete, so this adapter always returns ``complete=False``.
    """

    if state.player is None or state.player.position is None:
        sampled_at_ms = _timestamp_to_ms(getattr(state, "timestamp_utc"))
        nearby_actors = tuple(getattr(state, "nearby_actors", ()))
        observed_count = len(nearby_actors)
        reasons = {"cloud_position_unavailable", "producer_completeness_unproven"}
        return DynamicObstacleSnapshot(
            base_sequence=state.sequence,
            sampled_at_ms=sampled_at_ms,
            observations=(),
            source_identity=source_identity,
            complete=False,
            observed_actor_count=observed_count,
            accepted_actor_count=0,
            rejected_actor_count=observed_count,
            rejection_reason_codes=tuple(sorted(reasons)),
        )
    sampled_at_ms = _timestamp_to_ms(getattr(state, "sampled_at_utc", None) or getattr(state, "timestamp_utc"))
    if now_ms is None:
        now_ms = sampled_at_ms
    age_seconds = max(0.0, (now_ms - sampled_at_ms) / 1000.0)
    observations: list[DynamicObstacleObservation] = []
    rejection_reasons = {"producer_completeness_unproven"}
    party_tokens = _party_identity_tokens(state)
    nearby_actors = list(getattr(state, "nearby_actors", ()))
    # Some telemetry producers expose party members in ``party`` but omit them
    # from the nearby-actor sidecar. Add only members with a measured position;
    # this is observational context and never creates topology edges.
    existing_tokens = {
        token for actor in nearby_actors
        for token in (
            _identity_token(getattr(actor, "id", None)),
            _identity_token(getattr(actor, "name", None)),
        ) if token is not None
    }
    for member in tuple(getattr(state, "party", ())):
        if getattr(member, "position", None) is None:
            continue
        member_tokens = {
            token for token in (
                _identity_token(getattr(member, "actor_id", None)),
                _identity_token(getattr(member, "name", None)),
            ) if token is not None
        }
        if member_tokens & existing_tokens:
            continue
        member_id = getattr(member, "actor_id", None) or f"party:{getattr(member, 'name', 'unknown')}"
        nearby_actors.append(ObservedActor(
            id=str(member_id), name=getattr(member, "name", None), kind="party",
            position=member.position,
        ))
        existing_tokens.update(member_tokens)
    for actor in nearby_actors:
        if actor.position is None:
            rejection_reasons.add("actor_position_unavailable")
            continue
        try:
            observations.append(observation_from_observed_actor(
                actor,
                state.player,
                base_sequence=state.sequence,
                sampled_at_ms=sampled_at_ms,
                source_identity=source_identity,
                observation_age_seconds=age_seconds,
                actor_class=_actor_classification(actor, party_tokens),
                actor_category=actor.kind,
            ))
        except (TypeError, ValueError):
            rejection_reasons.add("actor_position_invalid")
            continue
    observed_count = len(nearby_actors)
    accepted_count = len(observations)
    return DynamicObstacleSnapshot(
        base_sequence=state.sequence,
        sampled_at_ms=sampled_at_ms,
        observations=tuple(observations),
        source_identity=source_identity,
        complete=False,
        observed_actor_count=observed_count,
        accepted_actor_count=accepted_count,
        rejected_actor_count=observed_count - accepted_count,
        rejection_reason_codes=tuple(sorted(rejection_reasons)),
    )


def assess_static_learning_dynamic_evidence(
    snapshot: DynamicObstacleSnapshot | None,
    *,
    cloud_position_m: Point3M | None,
    intended_bearing_degrees: float | None,
    horizon_m: float = 3.0,
    lateral_clearance_m: float = 0.85,
    vertical_clearance_m: float = 1.25,
    max_age_seconds: float = 0.75,
    max_actor_distance_m: float = _STATIC_LEARNING_ACTOR_RADIUS_M,
    expected_sequence: int | None = None,
    now_ms: int | None = None,
) -> DynamicObstacleStaticEvidence:
    """Classify one typed actor snapshot for static-learning purposes.

    The geometry is intentionally a small conservative envelope around the
    commanded direction.  It is not a replacement for the existing dynamic
    route-conflict analyzer; it only answers whether current typed actor
    evidence positively occupies the short movement probe, proves a complete
    clear actor set, or remains uncertain.
    """

    if snapshot is None:
        return DynamicObstacleStaticEvidence(
            StaticLearningDynamicState.ABSENT,
            "dynamic_obstacle_telemetry_absent",
        )
    for name, value in (
        ("horizon_m", horizon_m),
        ("lateral_clearance_m", lateral_clearance_m),
        ("vertical_clearance_m", vertical_clearance_m),
        ("max_age_seconds", max_age_seconds),
        ("max_actor_distance_m", max_actor_distance_m),
    ):
        _require_finite(name, float(value))
        if float(value) <= 0.0:
            raise ValueError(f"{name} must be positive")
    if cloud_position_m is None or intended_bearing_degrees is None:
        return DynamicObstacleStaticEvidence(
            StaticLearningDynamicState.UNKNOWN,
            "dynamic_obstacle_learning_geometry_missing",
            source_identity=snapshot.source_identity,
            base_sequence=snapshot.base_sequence,
            sampled_at_ms=snapshot.sampled_at_ms,
            snapshot_complete=snapshot.complete,
        )
    _require_finite("intended_bearing_degrees", float(intended_bearing_degrees))
    age_seconds = max(
        (float(item.observation_age_seconds) for item in snapshot.observations),
        default=0.0,
    )
    if now_ms is not None:
        if not isinstance(now_ms, int) or now_ms < 0:
            raise ValueError("now_ms must be a non-negative integer")
        age_seconds = max(age_seconds, (now_ms - snapshot.sampled_at_ms) / 1000.0)
    common = {
        "source_identity": snapshot.source_identity,
        "base_sequence": snapshot.base_sequence,
        "sampled_at_ms": snapshot.sampled_at_ms,
        "snapshot_complete": snapshot.complete,
        "age_seconds": max(0.0, age_seconds),
    }
    if expected_sequence is not None and snapshot.base_sequence != int(expected_sequence):
        return DynamicObstacleStaticEvidence(
            StaticLearningDynamicState.STALE,
            "dynamic_obstacle_sequence_mismatch",
            **common,
        )
    if age_seconds < 0.0 or age_seconds > float(max_age_seconds):
        return DynamicObstacleStaticEvidence(
            StaticLearningDynamicState.STALE,
            "dynamic_obstacle_snapshot_stale",
            **common,
        )
    radians = math.radians(float(intended_bearing_degrees))
    direction_x, direction_y = math.cos(radians), math.sin(radians)
    blocking: list[str] = []
    for observation in snapshot.observations:
        # Party members are pass-through gameplay actors for this check. Their
        # positions are retained in evidence, but they must never be promoted
        # to static geometry. Other actors within the bounded 2 m observation
        # radius remain conservative dynamic blockers until a route-aware
        # detour policy can prove a safe bypass.
        if observation.actor_class == "party":
            continue
        if observation.horizontal_distance_m > float(max_actor_distance_m):
            continue
        relative = observation.actor_position_m - cloud_position_m
        longitudinal = relative.x * direction_x + relative.y * direction_y
        lateral = abs(-direction_y * relative.x + direction_x * relative.y)
        if (
            -float(lateral_clearance_m) <= longitudinal <= float(horizon_m) + float(lateral_clearance_m)
            and lateral <= float(lateral_clearance_m)
            and observation.vertical_separation_m <= float(vertical_clearance_m)
        ):
            blocking.append(observation.actor_id)
    if blocking:
        return DynamicObstacleStaticEvidence(
            StaticLearningDynamicState.BLOCKING,
            "current_dynamic_actor_in_intended_movement_envelope",
            blocking_actor_ids=tuple(sorted(blocking)),
            **common,
        )
    if snapshot.complete and snapshot.rejected_actor_count == 0:
        return DynamicObstacleStaticEvidence(
            StaticLearningDynamicState.CLEAR_COMPLETE,
            "complete_actor_snapshot_proves_no_relevant_dynamic_blocker",
            **common,
        )
    return DynamicObstacleStaticEvidence(
        StaticLearningDynamicState.INCOMPLETE,
        "dynamic_obstacle_snapshot_completeness_unproven",
        **common,
    )


def _timestamp_to_ms(timestamp: str) -> int:
    from datetime import datetime

    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return int(parsed.timestamp() * 1000.0)


def _route_segments(route: Sequence[Point3M]) -> tuple[_RouteSegment, ...]:
    if len(route) < 2:
        raise ValueError("route polyline requires at least two points")
    segments: list[_RouteSegment] = []
    cumulative = 0.0
    for index, (start, end) in enumerate(zip(route, route[1:])):
        delta = end - start
        length = math.hypot(delta.x, delta.y)
        if length <= _EPSILON:
            raise ValueError("route contains a zero-length horizontal segment")
        direction = delta.scale(1.0 / length)
        segments.append(_RouteSegment(index, start, end, cumulative, length, direction))
        cumulative += length
    return tuple(segments)


def _point_on_segment(segment: _RouteSegment, fraction: float) -> Point3M:
    fraction = max(0.0, min(1.0, fraction))
    return segment.start + (segment.end - segment.start).scale(fraction)


def _project_xy(point: Point3M, segment: _RouteSegment) -> tuple[float, Point3M, float]:
    delta = segment.end - segment.start
    denominator = segment.length_m * segment.length_m
    fraction = ((point.x - segment.start.x) * delta.x + (point.y - segment.start.y) * delta.y) / denominator
    fraction = max(0.0, min(1.0, fraction))
    projected = _point_on_segment(segment, fraction)
    horizontal = math.hypot(point.x - projected.x, point.y - projected.y)
    return horizontal, projected, segment.cumulative_start_m + fraction * segment.length_m


def _nearest_route_projection(
    point: Point3M,
    segments: Sequence[_RouteSegment],
) -> tuple[int, Point3M, float, float]:
    best: tuple[float, int, Point3M, float] | None = None
    for segment in segments:
        lateral, projected, along = _project_xy(point, segment)
        candidate = (lateral, segment.index, projected, along)
        if best is None or lateral < best[0] - _EPSILON:
            best = candidate
    if best is None:
        raise ValueError("route has no segments")
    return best[1], best[2], best[3], best[0]


def _quadratic_interval(
    r0x: float,
    r0y: float,
    rvx: float,
    rvy: float,
    radius: float,
    start: float,
    end: float,
) -> tuple[float, float] | None:
    """Return the interval where ``|r0 + rv*t| <= radius``."""

    a = rvx * rvx + rvy * rvy
    b = 2.0 * (r0x * rvx + r0y * rvy)
    c = r0x * r0x + r0y * r0y - radius * radius
    if a <= _EPSILON:
        return (start, end) if c <= _EPSILON else None
    discriminant = b * b - 4.0 * a * c
    if discriminant < 0.0:
        return None
    root = math.sqrt(max(0.0, discriminant))
    low = (-b - root) / (2.0 * a)
    high = (-b + root) / (2.0 * a)
    low = max(start, low)
    high = min(end, high)
    return (low, high) if low <= high + _EPSILON else None


def _linear_band_interval(
    value_at_start: float,
    rate: float,
    band: float,
    start: float,
    end: float,
) -> tuple[float, float] | None:
    """Return the interval where ``abs(value_at_start + rate*t) <= band``."""

    if abs(rate) <= _EPSILON:
        return (start, end) if abs(value_at_start) <= band + _EPSILON else None
    first = (-band - value_at_start) / rate
    second = (band - value_at_start) / rate
    low = max(start, min(first, second))
    high = min(end, max(first, second))
    return (low, high) if low <= high + _EPSILON else None


def analyze_route_obstacle(
    cloud_position_m: Point3M,
    route: Sequence[Point3M],
    *,
    route_progress_m: float,
    obstacle: DynamicObstacleObservation,
    geometry: ObstacleGeometry,
    cloud_speed_mps: float,
    horizon_seconds: float,
    vertical_clearance_m: float = 1.0,
    max_observation_age_seconds: float = 0.75,
    behind_tolerance_m: float = 0.25,
) -> RouteConflictAnalysis:
    """Analyze one actor against the future Cloud trajectory on a route polyline.

    Each route segment is evaluated over the time interval in which Cloud would
    traverse it.  Actor motion is constant-velocity only within the bounded
    horizon; no position is extrapolated beyond that horizon.  The nearest
    point on the whole polyline is used for behind/boundary decisions, so a
    later bend is never discarded merely because the first segment is clear.
    """

    _require_finite("route_progress_m", route_progress_m)
    _require_finite("cloud_speed_mps", cloud_speed_mps)
    _require_finite("horizon_seconds", horizon_seconds)
    _require_finite("vertical_clearance_m", vertical_clearance_m)
    _require_finite("max_observation_age_seconds", max_observation_age_seconds)
    _require_finite("behind_tolerance_m", behind_tolerance_m)
    if route_progress_m < 0 or cloud_speed_mps < 0 or horizon_seconds <= 0:
        raise ValueError("route progress, Cloud speed, and horizon have invalid bounds")
    if vertical_clearance_m < 0 or max_observation_age_seconds < 0 or behind_tolerance_m < 0:
        raise ValueError("clearance and tolerances must be non-negative")

    segments = _route_segments(route)
    nearest_index, nearest_point, nearest_along, current_lateral = _nearest_route_projection(
        obstacle.actor_position_m, segments
    )
    current_vertical = abs(obstacle.actor_position_m.z - nearest_point.z)
    longitudinal = nearest_along - route_progress_m
    effective_radius = geometry.effective_radius_m

    if obstacle.observation_age_seconds > max_observation_age_seconds:
        return _analysis(
            obstacle,
            ConflictClassification.UNKNOWN,
            "stale_observation",
            nearest_index,
            nearest_along,
            current_lateral,
            longitudinal,
            None,
            None,
            current_vertical,
            effective_radius,
            False,
        )
    if nearest_along < route_progress_m - behind_tolerance_m:
        return _analysis(
            obstacle,
            ConflictClassification.NO_CONFLICT,
            "behind_route_progress",
            nearest_index,
            nearest_along,
            current_lateral,
            longitudinal,
            None,
            None,
            current_vertical,
            effective_radius,
            obstacle.velocity_mps is not None,
        )

    velocity = obstacle.velocity_mps or Point3M(0.0, 0.0, 0.0)
    # The caller supplies the short horizon explicitly.  Tracker-level
    # prediction uses its own configured maximum; geometry does not silently
    # rewrite a scenario's requested horizon.
    horizon = horizon_seconds
    route_end = segments[-1].cumulative_end_m
    future_end = min(route_end, route_progress_m + cloud_speed_mps * horizon)
    closest_distance = math.inf
    closest_time: float | None = None
    closest_vertical: float | None = None
    conflict_time: float | None = None
    had_future_interval = False

    for segment in segments:
        if segment.cumulative_end_m < route_progress_m - _EPSILON:
            continue
        if segment.cumulative_start_m > future_end + _EPSILON:
            break
        if cloud_speed_mps <= _EPSILON:
            if route_progress_m < segment.cumulative_start_m - _EPSILON or route_progress_m > segment.cumulative_end_m + _EPSILON:
                continue
            time_start, time_end = 0.0, horizon
            route_start_along = route_progress_m
        else:
            time_start = max(0.0, (segment.cumulative_start_m - route_progress_m) / cloud_speed_mps)
            time_end = min(horizon, (segment.cumulative_end_m - route_progress_m) / cloud_speed_mps)
            route_start_along = route_progress_m + cloud_speed_mps * time_start
        if time_end < time_start - _EPSILON:
            continue
        had_future_interval = True

        route_fraction = (route_start_along - segment.cumulative_start_m) / segment.length_m
        route_at_start = _point_on_segment(segment, route_fraction)
        route_velocity = segment.direction.scale(cloud_speed_mps)
        obstacle_at_start = obstacle.actor_position_m + velocity.scale(time_start)
        relative_at_start = obstacle_at_start - route_at_start
        relative_velocity = velocity - route_velocity
        local_end = time_end - time_start

        candidate_local_time = 0.0
        relative_speed_sq = relative_velocity.x * relative_velocity.x + relative_velocity.y * relative_velocity.y
        if relative_speed_sq > _EPSILON:
            candidate_local_time = max(
                0.0,
                min(
                    local_end,
                    -(
                        relative_at_start.x * relative_velocity.x
                        + relative_at_start.y * relative_velocity.y
                    ) / relative_speed_sq,
                ),
            )
        candidate_relative = relative_at_start + relative_velocity.scale(candidate_local_time)
        candidate_distance = math.hypot(candidate_relative.x, candidate_relative.y)
        candidate_time = time_start + candidate_local_time
        if candidate_distance < closest_distance - _EPSILON:
            closest_distance = candidate_distance
            closest_time = candidate_time
            closest_vertical = abs(candidate_relative.z)

        horizontal_interval = _quadratic_interval(
            relative_at_start.x,
            relative_at_start.y,
            relative_velocity.x,
            relative_velocity.y,
            effective_radius,
            0.0,
            local_end,
        )
        if horizontal_interval is None:
            continue
        vertical_interval = _linear_band_interval(
            relative_at_start.z,
            relative_velocity.z,
            vertical_clearance_m,
            0.0,
            local_end,
        )
        if vertical_interval is None:
            continue
        local_conflict = max(horizontal_interval[0], vertical_interval[0])
        if local_conflict <= min(horizontal_interval[1], vertical_interval[1]) + _EPSILON:
            absolute_conflict = time_start + local_conflict
            if conflict_time is None or absolute_conflict < conflict_time:
                conflict_time = absolute_conflict

    if conflict_time is not None:
        return _analysis(
            obstacle,
            ConflictClassification.CONFLICT,
            "predicted_route_corridor_conflict",
            nearest_index,
            nearest_along,
            current_lateral,
            longitudinal,
            closest_distance if math.isfinite(closest_distance) else current_lateral,
            conflict_time,
            closest_vertical if closest_vertical is not None else current_vertical,
            effective_radius,
            True,
        )
    if not had_future_interval:
        reason = "outside_near_future_horizon"
    elif math.isinf(closest_distance):
        reason = "no_route_time_interval"
    elif current_vertical > vertical_clearance_m and closest_vertical is not None and closest_vertical > vertical_clearance_m:
        reason = "vertical_separation"
    elif closest_distance > effective_radius:
        reason = "lateral_clearance"
    else:
        reason = "no_conflict_within_horizon"
    return _analysis(
        obstacle,
        ConflictClassification.NO_CONFLICT,
        reason if obstacle.velocity_mps is not None else f"{reason}_velocity_unknown",
        nearest_index,
        nearest_along,
        current_lateral,
        longitudinal,
        closest_distance if math.isfinite(closest_distance) else None,
        None,
        closest_vertical if closest_vertical is not None else current_vertical,
        effective_radius,
        obstacle.velocity_mps is not None and obstacle.velocity_confidence > 0.0,
    )


def _analysis(
    obstacle: DynamicObstacleObservation,
    classification: ConflictClassification,
    reason_code: str,
    nearest_segment_index: int | None,
    along_route_m: float | None,
    lateral_distance_m: float | None,
    longitudinal_distance_ahead_m: float | None,
    predicted_closest_approach_m: float | None,
    time_to_conflict_seconds: float | None,
    vertical_separation_m: float | None,
    effective_radius_m: float,
    authorizable: bool,
) -> RouteConflictAnalysis:
    return RouteConflictAnalysis(
        actor_id=obstacle.actor_id,
        classification=classification,
        reason_code=reason_code,
        nearest_segment_index=nearest_segment_index,
        along_route_m=along_route_m,
        lateral_distance_m=lateral_distance_m,
        longitudinal_distance_ahead_m=longitudinal_distance_ahead_m,
        predicted_closest_approach_m=predicted_closest_approach_m,
        time_to_conflict_seconds=time_to_conflict_seconds,
        vertical_separation_m=vertical_separation_m,
        observation_age_seconds=obstacle.observation_age_seconds,
        velocity_confidence=obstacle.velocity_confidence,
        effective_radius_m=effective_radius_m,
        authorizable=authorizable,
    )


def analyze_route_obstacles(
    cloud_position_m: Point3M,
    route: Sequence[Point3M],
    *,
    route_progress_m: float,
    obstacles: Iterable[tuple[DynamicObstacleObservation, ObstacleGeometry]],
    cloud_speed_mps: float,
    horizon_seconds: float,
    vertical_clearance_m: float = 1.0,
    max_observation_age_seconds: float = 0.75,
    behind_tolerance_m: float = 0.25,
    snapshot: DynamicObstacleSnapshot | None = None,
) -> ObstacleSetAnalysis:
    """Analyze actors and fail closed when the actor set is not complete.

    ``snapshot`` is optional for compatibility with low-level geometry calls,
    but omitting it means completeness is unproven. Such a call may surface a
    definite conflict, but a clear result is never clearance-authorizable.
    When supplied, the pair IDs must exactly match the snapshot observations;
    the function will not combine observations from another set or source.
    """

    pairs = sorted(obstacles, key=lambda item: item[0].actor_id)
    if snapshot is not None:
        snapshot_ids = tuple(sorted(item.actor_id for item in snapshot.observations))
        pair_ids = tuple(item[0].actor_id for item in pairs)
        if pair_ids != snapshot_ids:
            raise ValueError("obstacle observations do not match snapshot evidence")
        if any(
            observation.base_sequence != snapshot.base_sequence
            or observation.sampled_at_ms != snapshot.sampled_at_ms
            or observation.source_identity != snapshot.source_identity
            for observation, _geometry in pairs
        ):
            raise ValueError("obstacle observations do not match snapshot identity evidence")
    completeness_proven = snapshot is not None and snapshot.complete
    if not pairs:
        if completeness_proven:
            return ObstacleSetAnalysis(
                classification=ConflictClassification.NO_CONFLICT,
                reason_code="complete_no_relevant_actors",
                selected=None,
                analyses=(),
                authorizable=True,
            )
        return ObstacleSetAnalysis(
            classification=ConflictClassification.UNKNOWN,
            reason_code="incomplete_obstacle_snapshot" if snapshot is not None else "no_obstacle_data",
            selected=None,
            analyses=(),
            authorizable=False,
        )
    analyses = tuple(
        analyze_route_obstacle(
            cloud_position_m,
            route,
            route_progress_m=route_progress_m,
            obstacle=observation,
            geometry=geometry,
            cloud_speed_mps=cloud_speed_mps,
            horizon_seconds=horizon_seconds,
            vertical_clearance_m=vertical_clearance_m,
            max_observation_age_seconds=max_observation_age_seconds,
            behind_tolerance_m=behind_tolerance_m,
        )
        for observation, geometry in pairs
    )
    conflicts = [item for item in analyses if item.classification == ConflictClassification.CONFLICT]
    if conflicts:
        selected = min(
            conflicts,
            key=lambda item: (
                item.time_to_conflict_seconds if item.time_to_conflict_seconds is not None else math.inf,
                item.longitudinal_distance_ahead_m if item.longitudinal_distance_ahead_m is not None else math.inf,
                item.actor_id,
            ),
        )
        return ObstacleSetAnalysis(
            classification=ConflictClassification.CONFLICT,
            reason_code="nearest_relevant_conflict",
            selected=selected,
            analyses=analyses,
            authorizable=selected.authorizable,
        )
    unknown = [item for item in analyses if item.classification == ConflictClassification.UNKNOWN]
    if unknown:
        return ObstacleSetAnalysis(
            classification=ConflictClassification.UNKNOWN,
            reason_code="unknown_obstacle_evidence",
            selected=min(unknown, key=lambda item: item.actor_id),
            analyses=analyses,
            authorizable=False,
        )
    if not completeness_proven:
        return ObstacleSetAnalysis(
            classification=ConflictClassification.UNKNOWN,
            reason_code="incomplete_obstacle_snapshot",
            selected=min(analyses, key=lambda item: (abs(item.lateral_distance_m or math.inf), item.actor_id)),
            analyses=analyses,
            authorizable=False,
        )
    return ObstacleSetAnalysis(
        classification=ConflictClassification.NO_CONFLICT,
        reason_code="all_obstacles_clear_within_horizon",
        selected=min(analyses, key=lambda item: (abs(item.lateral_distance_m or math.inf), item.actor_id)),
        analyses=analyses,
        authorizable=all(item.authorizable for item in analyses),
    )


def predict_obstacle_position(
    observation: DynamicObstacleObservation,
    seconds: float,
    *,
    maximum_horizon_seconds: float = 3.0,
) -> Point3M:
    """Bounded constant-velocity prediction; never extrapolate indefinitely."""

    _require_finite("seconds", seconds)
    _require_finite("maximum_horizon_seconds", maximum_horizon_seconds)
    if seconds < 0 or seconds > maximum_horizon_seconds:
        raise ValueError("prediction horizon exceeds configured bound")
    if observation.velocity_mps is None:
        return observation.actor_position_m
    return observation.actor_position_m + observation.velocity_mps.scale(seconds)
