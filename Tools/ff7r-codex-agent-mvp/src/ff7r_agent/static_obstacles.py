"""Scoped static-world obstacle learning and Detour route overlays.

The shipping Detour graph is deliberately never mutated here.  This module
stores bounded, scoped evidence learned by the route follower and applies it
as a read-only overlay when a fresh route is planned.  Dynamic actors have a
separate contract in :mod:`dynamic_obstacles` and are not converted into this
representation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import heapq
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .detour_route import DT_POLYTYPE_GROUND, DetourRoutePlan, DetourTopologyGraph, _distance_m, _closest_point_on_polygon
from .dynamic_obstacles import DynamicObstacleStaticEvidence, StaticLearningDynamicState
from .models import Vec3
from .offmesh_links import GIMMICK_LINK_CLASSES, locate_traversal_waypoints, with_waypoint_indices


STATIC_OBSTACLE_SCHEMA = "ff7r-static-obstacle-overlay-v1"
STATIC_OBSTACLE_ARTIFACT_SCHEMA = "ff7r-static-obstacle-companion-v1"
STATIC_OBSTACLE_QUERY_SOURCE = "ue4ss_line_trace_multi_height_capsule"
_NAVIGATION_BINDING_KEY = "_ff7r_navigation_binding"
DEFAULT_CLOUD_CLEARANCE_M = 0.65
DEFAULT_PROBABLE_RADIUS_M = 0.55
DEFAULT_UNCERTAINTY_MARGIN_M = 0.25
DEFAULT_NO_PROGRESS_DISPLACEMENT_M = 0.10
DEFAULT_NO_PROGRESS_ROUTE_DELTA_M = 0.05
DEFAULT_MAX_OBSTACLES = 32
DEFAULT_MAX_EVIDENCE = 128
DEFAULT_MAX_EVIDENCE_AGE_SECONDS = 900.0
DEFAULT_LOCAL_EDGE_CLEARANCE_M = 0.20
_LOCAL_REJOIN_MAX_SEGMENTS = 2
_LOCAL_REJOIN_FRACTIONS = (0.1, 0.5, 0.9)
DEFAULT_MAX_SESSION_SCOPES = 8
DEFAULT_MAX_CONFIRMATION_RECORDS = 128
DEFAULT_MAX_CONFIRMATION_SESSIONS = 8
DEFAULT_BARRIER_MERGE_DISTANCE_M = 4.0
DEFAULT_BARRIER_MIN_SPAN_M = 2.0
DEFAULT_BARRIER_MAX_RESIDUAL_M = 0.40
DEFAULT_BARRIER_MAX_SAMPLES = 16


class StaticObstacleError(ValueError):
    """Base class for invalid or fail-closed static-obstacle evidence."""


class StaticObstacleScopeError(StaticObstacleError):
    """Evidence does not belong to the current topology/session scope."""


def navigation_sequence_is_bound(
    navigation_payload: Mapping[str, Any],
    *,
    observed_sequence: Any = None,
    sample_sequence: Any = None,
) -> bool:
    """Accept exact samples or the consumer-issued trusted timestamp binding.

    The timestamp binding is inserted by :class:`JsonFileTelemetry` only
    after the recognized UE4SS producer has passed freshness and identity
    checks. A sidecar cannot opt into this path by publishing the marker.
    """

    binding = navigation_payload.get(_NAVIGATION_BINDING_KEY)
    if observed_sequence is None and isinstance(binding, Mapping):
        observed_sequence = binding.get("observed_sequence")
    if observed_sequence is None:
        observed_sequence = navigation_payload.get("base_sequence")
    try:
        payload_sequence = int(navigation_payload["base_sequence"])
        observed = int(observed_sequence)
        sample = payload_sequence if sample_sequence is None else int(sample_sequence)
    except (KeyError, TypeError, ValueError):
        return False
    if payload_sequence == observed and sample == observed:
        return True
    if not isinstance(binding, Mapping):
        return False
    try:
        bound_observed = int(binding["observed_sequence"])
        bound_sidecar = int(binding["sidecar_sequence"])
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        binding.get("mode") == "trusted_timestamp"
        and binding.get("source") == STATIC_OBSTACLE_QUERY_SOURCE
        and bound_observed == observed
        and bound_sidecar == payload_sequence
        and sample == payload_sequence
    )


class StaticObstaclePlanningError(StaticObstacleError):
    """An obstacle overlay cannot produce a validated route."""


class ObstacleEvidenceState(str, Enum):
    UNKNOWN = "unknown"
    CANDIDATE = "candidate"
    READY = "ready"
    STALE = "stale"


@dataclass(frozen=True)
class StaticObstacleScope:
    session_id: str
    world_id: str
    map_id: str
    topology_fingerprint: str

    def __post_init__(self) -> None:
        if not all(str(value).strip() for value in (
            self.session_id, self.world_id, self.map_id, self.topology_fingerprint,
        )):
            raise StaticObstacleScopeError("static_obstacle_scope_incomplete")

    def to_dict(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "world_id": self.world_id,
            "map_id": self.map_id,
            "topology_fingerprint": self.topology_fingerprint,
        }


@dataclass(frozen=True)
class StaticObstacleDurableIdentity:
    """Stable identity for a topology-associated companion layer.

    A runtime session is deliberately not part of this identity.  It is the
    key that permits a confirmed static-world observation to be reused only
    with the same executable/map/topology content, while session-local
    candidates remain ephemeral.
    """

    build_id: str
    map_id: str
    topology_fingerprint: str
    topology_schema: str = "shipping_detour-v1"
    content_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all(str(value).strip() for value in (
            self.build_id, self.map_id, self.topology_fingerprint, self.topology_schema,
        )):
            raise StaticObstacleScopeError("static_obstacle_durable_identity_incomplete")
        if any(not str(value).strip() for value in self.content_hashes):
            raise StaticObstacleScopeError("static_obstacle_durable_identity_hash_invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_id": self.build_id,
            "map_id": self.map_id,
            "topology_fingerprint": self.topology_fingerprint,
            "topology_schema": self.topology_schema,
            "content_hashes": list(self.content_hashes),
        }

    def key(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest().upper()


@dataclass(frozen=True)
class StaticObstaclePromotionEvidence:
    """Independent evidence that permits one-session durable promotion."""

    kind: str
    reason: str
    evidence_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"offline_static", "static_runtime_identity"}:
            raise StaticObstacleError("static_obstacle_promotion_evidence_kind_invalid")
        if not self.reason.strip():
            raise StaticObstacleError("static_obstacle_promotion_reason_missing")

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "evidence_id": self.evidence_id,
        }


def _obstacle_from_dict(value: Mapping[str, Any], *, default_persistence: str = "durable") -> "StaticObstacle":
    center = value.get("center")
    if not isinstance(center, Mapping):
        raise StaticObstacleError("static_obstacle_artifact_center_missing")
    try:
        return StaticObstacle(
            obstacle_id=str(value["obstacle_id"]),
            center=Vec3(float(center["x"]), float(center["y"]), float(center["z"])),
            radius_m=float(value["radius_m"]),
            cloud_clearance_m=float(value.get("cloud_clearance_m", DEFAULT_CLOUD_CLEARANCE_M)),
            uncertainty_margin_m=float(value.get("uncertainty_margin_m", DEFAULT_UNCERTAINTY_MARGIN_M)),
            shape=str(value.get("shape", "circle")),
            half_length_m=float(value.get("half_length_m", 0.0)),
            heading_degrees=float(value.get("heading_degrees", 0.0)),
            confidence=float(value["confidence"]),
            evidence_count=int(value["evidence_count"]),
            first_sequence=int(value.get("first_sequence", 0)),
            last_sequence=int(value.get("last_sequence", 0)),
            first_t=float(value.get("first_t", 0.0)),
            last_t=float(value.get("last_t", 0.0)),
            provenance=tuple(str(item) for item in value.get("provenance", ())),
            persistence=str(value.get("persistence", default_persistence)),
            probe_bearing_degrees=(
                float(value["probe_bearing_degrees"])
                if value.get("probe_bearing_degrees") is not None else None
            ),
            route_bearing_degrees=(
                float(value["route_bearing_degrees"])
                if value.get("route_bearing_degrees") is not None else None
            ),
            promotion_reason=(
                str(value["promotion_reason"])
                if value.get("promotion_reason") is not None else None
            ),
            promotion_evidence=tuple(str(item) for item in value.get("promotion_evidence", ())),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StaticObstacleError("static_obstacle_artifact_obstacle_invalid") from exc


def _durable_identity_from_dict(value: Mapping[str, Any]) -> StaticObstacleDurableIdentity:
    if not isinstance(value, Mapping):
        raise StaticObstacleError("static_obstacle_artifact_identity_invalid")
    try:
        return StaticObstacleDurableIdentity(
            build_id=str(value["build_id"]),
            map_id=str(value["map_id"]),
            topology_fingerprint=str(value["topology_fingerprint"]),
            topology_schema=str(value.get("topology_schema", "shipping_detour-v1")),
            content_hashes=tuple(str(item) for item in value.get("content_hashes", ())),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StaticObstacleError("static_obstacle_artifact_identity_invalid") from exc


@dataclass(frozen=True)
class StaticObstacle:
    """A bounded 2D obstacle footprint in Unreal centimetres/metres."""

    obstacle_id: str
    center: Vec3
    radius_m: float
    cloud_clearance_m: float = DEFAULT_CLOUD_CLEARANCE_M
    uncertainty_margin_m: float = DEFAULT_UNCERTAINTY_MARGIN_M
    shape: str = "circle"
    half_length_m: float = 0.0
    heading_degrees: float = 0.0
    confidence: float = 0.5
    evidence_count: int = 0
    first_sequence: int = 0
    last_sequence: int = 0
    first_t: float = 0.0
    last_t: float = 0.0
    provenance: tuple[str, ...] = ("movement_no_progress",)
    persistence: str = "session"
    probe_bearing_degrees: float | None = None
    route_bearing_degrees: float | None = None
    promotion_reason: str | None = None
    promotion_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = (
            self.center.x, self.center.y, self.center.z, self.radius_m,
            self.cloud_clearance_m, self.uncertainty_margin_m,
            self.half_length_m, self.heading_degrees, self.confidence,
            self.first_t, self.last_t,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise StaticObstacleError("static_obstacle_non_finite")
        if self.radius_m <= 0.0 or self.cloud_clearance_m < 0.0 or self.uncertainty_margin_m < 0.0:
            raise StaticObstacleError("static_obstacle_geometry_invalid")
        if self.shape not in {"circle", "capsule"} or self.half_length_m < 0.0:
            raise StaticObstacleError("static_obstacle_shape_invalid")
        if not 0.0 <= self.confidence <= 1.0 or int(self.evidence_count) < 1:
            raise StaticObstacleError("static_obstacle_confidence_invalid")
        if self.persistence not in {"session", "durable"}:
            raise StaticObstacleError("static_obstacle_persistence_invalid")
        for name, value in (
            ("probe_bearing_degrees", self.probe_bearing_degrees),
            ("route_bearing_degrees", self.route_bearing_degrees),
        ):
            if value is not None and not math.isfinite(float(value)):
                raise StaticObstacleError(f"static_obstacle_{name}_invalid")
        if any(not str(item).strip() for item in self.promotion_evidence):
            raise StaticObstacleError("static_obstacle_promotion_evidence_invalid")
        if int(self.first_sequence) < 0 or int(self.last_sequence) < int(self.first_sequence):
            raise StaticObstacleError("static_obstacle_sequence_invalid")
        if int(self.last_sequence) < 0:
            raise StaticObstacleError("static_obstacle_sequence_invalid")

    @property
    def effective_radius_m(self) -> float:
        """Radius including Cloud clearance and uncertainty margin."""

        return self.radius_m + self.cloud_clearance_m + self.uncertainty_margin_m

    def axis_endpoints(self) -> tuple[Vec3, Vec3]:
        if self.shape != "capsule" or self.half_length_m <= 0.0:
            return self.center, self.center
        radians = math.radians(self.heading_degrees)
        dx = math.cos(radians) * self.half_length_m * 100.0
        dy = math.sin(radians) * self.half_length_m * 100.0
        return (
            Vec3(self.center.x - dx, self.center.y - dy, self.center.z),
            Vec3(self.center.x + dx, self.center.y + dy, self.center.z),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "obstacle_id": self.obstacle_id,
            "center": {"x": self.center.x, "y": self.center.y, "z": self.center.z},
            "radius_m": self.radius_m,
            "cloud_clearance_m": self.cloud_clearance_m,
            "uncertainty_margin_m": self.uncertainty_margin_m,
            "effective_radius_m": self.effective_radius_m,
            "shape": self.shape,
            "half_length_m": self.half_length_m,
            "heading_degrees": self.heading_degrees,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "first_t": self.first_t,
            "last_t": self.last_t,
            "provenance": list(self.provenance),
            "persistence": self.persistence,
            "probe_bearing_degrees": self.probe_bearing_degrees,
            "route_bearing_degrees": self.route_bearing_degrees,
            "promotion_reason": self.promotion_reason,
            "promotion_evidence": list(self.promotion_evidence),
        }


@dataclass(frozen=True)
class StaticObstacleSnapshot:
    scope: StaticObstacleScope | None
    state: ObstacleEvidenceState
    generation: int
    obstacles: tuple[StaticObstacle, ...]
    candidate_count: int
    evidence_count: int
    reason_code: str
    stale_obstacle_ids: tuple[str, ...] = ()

    @property
    def planning_authorized(self) -> bool:
        return self.scope is not None and self.state in {
            ObstacleEvidenceState.READY,
            ObstacleEvidenceState.UNKNOWN,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": STATIC_OBSTACLE_SCHEMA,
            "scope": self.scope.to_dict() if self.scope else None,
            "state": self.state.value,
            "generation": self.generation,
            "obstacles": [item.to_dict() for item in self.obstacles],
            "candidate_count": self.candidate_count,
            "evidence_count": self.evidence_count,
            "reason_code": self.reason_code,
            "stale_obstacle_ids": list(self.stale_obstacle_ids),
        }

    def digest(self) -> str:
        # Candidate/evidence counters are diagnostic learning state.  They do
        # not affect the authorized planning overlay until an obstacle is
        # admitted, so a first unconfirmed no-progress sample must not revoke
        # the current route proof underneath the controller.
        encoded = json.dumps({
            "schema": STATIC_OBSTACLE_SCHEMA,
            "scope": self.scope.to_dict() if self.scope else None,
            "state": self.state.value,
            "generation": self.generation,
            "obstacles": [item.to_dict() for item in self.obstacles],
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest().upper()


@dataclass(frozen=True)
class StaticObstacleLearningResult:
    state: ObstacleEvidenceState
    reason_code: str
    obstacle: StaticObstacle | None
    evidence_count: int
    generation: int


@dataclass
class _Accumulator:
    center: Vec3
    count: int
    sample_sequences: set[int]
    first_sequence: int
    last_sequence: int
    first_t: float
    last_t: float
    confidence: float
    query_confirmed: bool
    provenance: set[str]
    samples: list[Vec3]
    route_bearing_degrees: float | None = None
    probe_bearing_degrees: float | None = None
    dynamic_states: set[str] | None = None


class StaticObstacleStore:
    """Bounded session/durable storage for static-world obstacle knowledge.

    The store is intentionally independent of the Detour graph.  A service
    may retain one instance across adapter invocations, and an optional JSON
    companion artifact provides durable promotion without making a runtime
    session's candidate state globally visible.
    """

    def __init__(
        self,
        *,
        artifact_path: str | Path | None = None,
        artifact_root: str | Path | None = None,
        max_durable_obstacles: int = DEFAULT_MAX_OBSTACLES,
        max_session_obstacles: int = DEFAULT_MAX_OBSTACLES,
        minimum_durable_sessions: int = 2,
        max_session_scopes: int = DEFAULT_MAX_SESSION_SCOPES,
        max_confirmation_records: int = DEFAULT_MAX_CONFIRMATION_RECORDS,
        max_confirmation_sessions: int = DEFAULT_MAX_CONFIRMATION_SESSIONS,
    ) -> None:
        if int(max_durable_obstacles) <= 0 or int(max_session_obstacles) <= 0:
            raise ValueError("static obstacle store bounds must be positive")
        if int(minimum_durable_sessions) <= 0:
            raise ValueError("minimum_durable_sessions must be positive")
        if int(max_session_scopes) <= 0 or int(max_confirmation_records) <= 0:
            raise ValueError("static obstacle historical bounds must be positive")
        if int(max_confirmation_sessions) < int(minimum_durable_sessions):
            raise ValueError("max_confirmation_sessions must cover minimum_durable_sessions")
        if artifact_path is not None and artifact_root is not None:
            raise ValueError("artifact_path and artifact_root are mutually exclusive")
        self._artifact_override = Path(artifact_path) if artifact_path is not None else None
        self.artifact_root = Path(artifact_root) if artifact_root is not None else None
        self.artifact_path = self._artifact_override
        self.max_durable_obstacles = int(max_durable_obstacles)
        self.max_session_obstacles = int(max_session_obstacles)
        self.minimum_durable_sessions = int(minimum_durable_sessions)
        self.max_session_scopes = int(max_session_scopes)
        self.max_confirmation_records = int(max_confirmation_records)
        self.max_confirmation_sessions = int(max_confirmation_sessions)
        self._durable: dict[tuple[str, str], tuple[StaticObstacleDurableIdentity, StaticObstacle]] = {}
        self._sessions: dict[str, dict[str, StaticObstacle]] = {}
        self._confirmations: dict[tuple[str, str], set[str]] = {}
        self._confirmation_identities: dict[tuple[str, str], StaticObstacleDurableIdentity] = {}
        self._confirmation_representatives: dict[tuple[str, str], StaticObstacle] = {}
        self._session_last_seen: dict[str, float] = {}
        self._confirmation_last_seen: dict[tuple[str, str], int] = {}
        self._next_confirmation_generation = 0
        self._confirmation_ordering_valid = True
        self._confirmation_promotion_enabled = True
        self.load_error: str | None = None
        self._load_artifact()

    @staticmethod
    def _scope_key(scope: StaticObstacleScope) -> str:
        return json.dumps(scope.to_dict(), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _record_key(identity: StaticObstacleDurableIdentity, obstacle_id: str) -> tuple[str, str]:
        return identity.key(), str(obstacle_id)

    def load_for_scope(
        self,
        scope: StaticObstacleScope,
        identity: StaticObstacleDurableIdentity | None,
    ) -> tuple[StaticObstacle, ...]:
        scope_key = self._scope_key(scope)
        self._session_last_seen.setdefault(scope_key, 0.0)
        self._prune_historical_state()
        session_values = tuple(self._sessions.get(scope_key, {}).values())
        durable_values: tuple[StaticObstacle, ...] = ()
        if identity is not None:
            durable_values = tuple(
                obstacle
                for (identity_key, _), (stored_identity, obstacle) in self._durable.items()
                if identity_key == identity.key() and stored_identity == identity
            )
        merged = {item.obstacle_id: item for item in (*durable_values, *session_values)}
        return tuple(sorted(merged.values(), key=lambda item: item.obstacle_id))

    def bind_identity(self, identity: StaticObstacleDurableIdentity | None) -> None:
        """Select the identity-specific companion without touching topology."""

        if self._artifact_override is not None or self.artifact_root is None or identity is None:
            return
        target = self.artifact_root / f"{identity.key()}.json"
        if self.artifact_path == target:
            return
        self.artifact_path = target
        self._durable.clear()
        self._confirmations.clear()
        self._confirmation_identities.clear()
        self._confirmation_representatives.clear()
        self._confirmation_last_seen.clear()
        self._next_confirmation_generation = 0
        self._confirmation_ordering_valid = True
        self._confirmation_promotion_enabled = True
        self.load_error = None
        self._load_artifact()

    def record(
        self,
        *,
        scope: StaticObstacleScope,
        identity: StaticObstacleDurableIdentity | None,
        obstacle: StaticObstacle,
        promotion_evidence: StaticObstaclePromotionEvidence | None = None,
    ) -> StaticObstacle:
        session_key = self._scope_key(scope)
        session_layer = self._sessions.setdefault(session_key, {})
        session_layer[obstacle.obstacle_id] = replace(obstacle, persistence="session")
        self._session_last_seen[session_key] = max(
            float(obstacle.last_t), self._session_last_seen.get(session_key, float("-inf"))
        )
        self._trim_session(session_key)
        self._prune_historical_state()
        if identity is None:
            return session_layer[obstacle.obstacle_id]
        record_key = self._record_key(identity, obstacle.obstacle_id)
        existing = self._durable.get(record_key)
        if existing is not None and existing[0] == identity:
            return existing[1]
        sessions = self._confirmations.setdefault(record_key, set())
        new_independent_session = scope.session_id not in sessions
        sessions.add(scope.session_id)
        self._confirmation_identities[record_key] = identity
        self._confirmation_representatives[record_key] = replace(obstacle, persistence="session")
        if new_independent_session and self._confirmation_ordering_valid:
            self._next_confirmation_generation += 1
            self._confirmation_last_seen[record_key] = self._next_confirmation_generation
        independent_session_count = len(sessions)
        if len(sessions) > self.max_confirmation_sessions:
            self._confirmations[record_key] = set(sorted(sessions)[-self.max_confirmation_sessions:])
            sessions = self._confirmations[record_key]
        self._prune_historical_state()
        multi_session = independent_session_count >= self.minimum_durable_sessions
        independently_static = promotion_evidence is not None
        if (
            (not multi_session and not independently_static)
            or not self._confirmation_promotion_enabled
        ):
            # Confirmation-only state is deliberately persisted separately from
            # planning geometry.  It becomes useful after a restart or an
            # identity switch, but never appears in load_for_scope().
            self._persist_artifact()
            return session_layer[obstacle.obstacle_id]
        reason = (
            promotion_evidence.reason
            if promotion_evidence is not None and not multi_session
            else "independent_session_confirmations"
        )
        evidence_values = set(obstacle.promotion_evidence)
        if promotion_evidence is not None:
            evidence_values.add(promotion_evidence.kind)
            if promotion_evidence.evidence_id:
                evidence_values.add(promotion_evidence.evidence_id)
        durable = replace(
            obstacle,
            persistence="durable",
            provenance=tuple(sorted(set(obstacle.provenance) | {"topology_companion_promoted"})),
            promotion_reason=reason,
            promotion_evidence=tuple(sorted(evidence_values)),
        )
        self._durable[record_key] = (identity, durable)
        self._trim_durable()
        self._persist_artifact()
        return durable

    def remove_session_obstacles(self, scope: StaticObstacleScope, obstacle_ids: Iterable[str]) -> None:
        """Remove superseded local geometry after a deterministic barrier merge."""

        layer = self._sessions.get(self._scope_key(scope))
        if layer is None:
            return
        for obstacle_id in obstacle_ids:
            layer.pop(str(obstacle_id), None)

    def durable_obstacles(self, identity: StaticObstacleDurableIdentity) -> tuple[StaticObstacle, ...]:
        return tuple(
            obstacle
            for (identity_key, _), (stored_identity, obstacle) in self._durable.items()
            if identity_key == identity.key() and stored_identity == identity
        )

    def artifact_dict(self) -> dict[str, Any]:
        records = []
        for (identity_key, obstacle_id), (identity, obstacle) in sorted(self._durable.items()):
            records.append({
                "identity_key": identity_key,
                "obstacle_id": obstacle_id,
                "identity": identity.to_dict(),
                "obstacle": obstacle.to_dict(),
                "confirmation_sessions": sorted(self._confirmations.get((identity_key, obstacle_id), set())),
                "promotion_reason": obstacle.promotion_reason,
                "promotion_evidence": list(obstacle.promotion_evidence),
            })
        confirmation_ledger = []
        for (identity_key, obstacle_id), sessions in sorted(self._confirmations.items()):
            identity = self._confirmation_identities.get((identity_key, obstacle_id))
            representative = self._confirmation_representatives.get((identity_key, obstacle_id))
            if identity is None or representative is None:
                continue
            confirmation_ledger.append({
                "identity_key": identity_key,
                "obstacle_id": obstacle_id,
                "identity": identity.to_dict(),
                # The representative is evidence, not planning geometry.  It
                # is serialized with an ordinary persistence value so legacy
                # obstacle parsing remains strict; the enclosing ledger entry
                # is the authority that makes it confirmation-only.
                "obstacle": replace(representative, persistence="session").to_dict(),
                "confirmation_sessions": sorted(sessions),
                "last_seen_generation": int(self._confirmation_last_seen.get((identity_key, obstacle_id), 0)),
                "confirmation_only": True,
            })
        return {
            "schema": STATIC_OBSTACLE_ARTIFACT_SCHEMA,
            "records": records,
            "confirmation_ledger": confirmation_ledger,
            "next_confirmation_generation": int(self._next_confirmation_generation),
        }

    def _load_artifact(self) -> None:
        if self.artifact_path is None or not self.artifact_path.is_file():
            return
        try:
            payload = json.loads(self.artifact_path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping) or payload.get("schema") != STATIC_OBSTACLE_ARTIFACT_SCHEMA:
                raise StaticObstacleError("static_obstacle_artifact_schema_invalid")
            records = payload.get("records")
            if not isinstance(records, list):
                raise StaticObstacleError("static_obstacle_artifact_records_invalid")
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, StaticObstacleError) as exc:
            # An unreadable companion layer cannot authorize durable geometry;
            # the current session may still learn fresh evidence safely.
            self.load_error = type(exc).__name__
            self._durable.clear()
            self._confirmations.clear()
            self._confirmation_identities.clear()
            self._confirmation_representatives.clear()
            self._confirmation_last_seen.clear()
            self._next_confirmation_generation = 0
            self._confirmation_ordering_valid = False
            self._confirmation_promotion_enabled = False
            return

        ordering_metadata_present = "next_confirmation_generation" in payload
        ordering_invalid = False
        if ordering_metadata_present:
            raw_generation = payload.get("next_confirmation_generation")
            if isinstance(raw_generation, bool) or not isinstance(raw_generation, int) or raw_generation < 0:
                ordering_invalid = True
                self._next_confirmation_generation = 0
            else:
                self._next_confirmation_generation = int(raw_generation)
        else:
            # Legacy artifacts used process-local monotonic timestamps.  Do
            # not compare those values after restart; a stable key order below
            # gives legacy records a deterministic migration order instead.
            self._next_confirmation_generation = 0
        legacy_confirmation_keys: set[tuple[str, str]] = set()
        try:
            for record in records[: self.max_durable_obstacles]:
                if not isinstance(record, Mapping) or not isinstance(record.get("identity"), Mapping):
                    raise StaticObstacleError("static_obstacle_artifact_record_invalid")
                identity = _durable_identity_from_dict(record["identity"])
                obstacle = _obstacle_from_dict(record.get("obstacle") or {})
                key = self._record_key(identity, obstacle.obstacle_id)
                if str(record.get("identity_key") or key[0]) != key[0] or str(record.get("obstacle_id") or obstacle.obstacle_id) != obstacle.obstacle_id:
                    raise StaticObstacleError("static_obstacle_artifact_record_key_invalid")
                self._durable[key] = (identity, replace(obstacle, persistence="durable"))
                self._confirmations[key] = {
                    str(item) for item in record.get("confirmation_sessions", ())
                    if str(item).strip()
                }
                if self._confirmations[key]:
                    legacy_confirmation_keys.add(key)
                self._confirmation_identities[key] = identity
                self._confirmation_representatives[key] = replace(obstacle, persistence="session")
        except (KeyError, TypeError, ValueError, StaticObstacleError) as exc:
            self.load_error = type(exc).__name__
            self._durable.clear()
            self._confirmations.clear()
            self._confirmation_identities.clear()
            self._confirmation_representatives.clear()
            self._confirmation_last_seen.clear()
            self._next_confirmation_generation = 0
            self._confirmation_ordering_valid = False
            self._confirmation_promotion_enabled = False
            return

        # Confirmation-only records are intentionally parsed separately from
        # durable planning records.  A corrupt ledger must not discard valid
        # durable geometry, but it must disable further promotion until a
        # valid artifact is written/reloaded.
        ledger = payload.get("confirmation_ledger", [])
        if not isinstance(ledger, list):
            self.load_error = "confirmation_ledger_invalid"
            ordering_invalid = True
            self._confirmation_promotion_enabled = False
            ledger = []
        missing_generation_keys: set[tuple[str, str]] = set()
        loaded_ledger_keys: set[tuple[str, str]] = set()
        seen_generations: set[int] = set()
        generation_missing = object()
        # Parse the complete ledger before pruning so a smaller bound after a
        # restart retains the highest persisted generations, not the first
        # lexicographic entries in the JSON array.
        for entry in ledger:
            try:
                if not isinstance(entry, Mapping) or entry.get("confirmation_only") is not True:
                    raise StaticObstacleError("confirmation_ledger_entry_invalid")
                identity = _durable_identity_from_dict(entry.get("identity") or {})
                obstacle = _obstacle_from_dict(entry.get("obstacle") or {}, default_persistence="session")
                key = self._record_key(identity, obstacle.obstacle_id)
                if str(entry.get("identity_key") or "") != key[0] or str(entry.get("obstacle_id") or "") != obstacle.obstacle_id:
                    raise StaticObstacleError("confirmation_ledger_key_invalid")
                sessions = entry.get("confirmation_sessions")
                if not isinstance(sessions, list):
                    raise StaticObstacleError("confirmation_ledger_sessions_invalid")
                session_ids = {str(item) for item in sessions if str(item).strip()}
                if not session_ids:
                    raise StaticObstacleError("confirmation_ledger_sessions_empty")
                self._confirmations.setdefault(key, set()).update(session_ids)
                self._confirmations[key] = set(sorted(self._confirmations[key])[-self.max_confirmation_sessions:])
                self._confirmation_identities[key] = identity
                self._confirmation_representatives[key] = replace(obstacle, persistence="session")
                loaded_ledger_keys.add(key)
                raw_entry_generation = entry.get("last_seen_generation", generation_missing)
                if raw_entry_generation is generation_missing:
                    missing_generation_keys.add(key)
                elif (
                    isinstance(raw_entry_generation, bool)
                    or not isinstance(raw_entry_generation, int)
                    or raw_entry_generation <= 0
                ):
                    ordering_invalid = True
                else:
                    entry_generation = int(raw_entry_generation)
                    if entry_generation in seen_generations:
                        ordering_invalid = True
                    seen_generations.add(entry_generation)
                    self._confirmation_last_seen[key] = entry_generation
                    if ordering_metadata_present and entry_generation > self._next_confirmation_generation:
                        ordering_invalid = True
            except (KeyError, TypeError, ValueError, StaticObstacleError):
                self.load_error = "confirmation_ledger_invalid"
                ordering_invalid = True
                self._confirmation_promotion_enabled = False

        all_confirmation_keys = set(self._confirmations)
        if ordering_metadata_present:
            if missing_generation_keys or legacy_confirmation_keys - loaded_ledger_keys:
                ordering_invalid = True
        elif seen_generations:
            # Mixed old/new metadata cannot be ordered safely.  Keep the
            # records available for diagnostics, but disable promotion and
            # recency-based pruning until a valid artifact is produced.
            ordering_invalid = True
        else:
            # Deterministic migration for legacy files: key order is stable
            # across restart and topology switching, while no old monotonic
            # timestamp is treated as cross-process recency.
            for key in sorted(all_confirmation_keys):
                self._next_confirmation_generation += 1
                self._confirmation_last_seen[key] = self._next_confirmation_generation

        if ordering_invalid:
            self.load_error = self.load_error or "confirmation_ledger_ordering_invalid"
            self._confirmation_ordering_valid = False
            self._confirmation_promotion_enabled = False
        else:
            self._confirmation_ordering_valid = True
        self._prune_historical_state()
        if self._confirmation_ordering_valid:
            # Persist deterministic pruning/migration so the companion itself
            # remains bounded and restart-stable, not only the in-memory store.
            self._persist_artifact()

    def _persist_artifact(self) -> None:
        if self.artifact_path is None or not self._confirmation_ordering_valid:
            return
        try:
            self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.artifact_path.with_suffix(self.artifact_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self.artifact_dict(), sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(self.artifact_path)
        except OSError:
            # Runtime use remains session-safe; durable promotion is not
            # claimed if the configured companion location is unavailable.
            self.load_error = "static_obstacle_artifact_persist_failed"

    def _trim_session(self, session_key: str) -> None:
        layer = self._sessions.get(session_key, {})
        ordered = sorted(layer.values(), key=lambda item: (-item.confidence, -item.last_sequence, item.obstacle_id))
        self._sessions[session_key] = {item.obstacle_id: item for item in ordered[: self.max_session_obstacles]}

    def _prune_historical_state(self) -> None:
        if len(self._sessions) > self.max_session_scopes:
            ordered = sorted(
                self._sessions,
                key=lambda key: (self._session_last_seen.get(key, float("-inf")), key),
                reverse=True,
            )
            retained = set(ordered[: self.max_session_scopes])
            removed = set(self._sessions) - retained
            for key in removed:
                self._sessions.pop(key, None)
                self._session_last_seen.pop(key, None)
        if len(self._confirmations) > self.max_confirmation_records:
            if self._confirmation_ordering_valid:
                ordered = sorted(
                    self._confirmations,
                    key=lambda key: (self._confirmation_last_seen.get(key, 0), key),
                    reverse=True,
                )
            else:
                # Corrupt ordering metadata must not drive a recency claim.
                # Retain a deterministic bounded key slice and keep promotion
                # disabled until a valid companion is loaded.
                ordered = sorted(self._confirmations)
            retained = set(ordered[: self.max_confirmation_records])
            for key in set(self._confirmations) - retained:
                self._confirmations.pop(key, None)
                self._confirmation_identities.pop(key, None)
                self._confirmation_representatives.pop(key, None)
                self._confirmation_last_seen.pop(key, None)

    def _trim_durable(self) -> None:
        if len(self._durable) <= self.max_durable_obstacles:
            return
        ordered = sorted(
            self._durable.items(),
            key=lambda item: (-item[1][1].confidence, -item[1][1].evidence_count, item[0]),
        )
        self._durable = dict(ordered[: self.max_durable_obstacles])


def _finite_point(point: Vec3) -> bool:
    return all(math.isfinite(float(value)) for value in (point.x, point.y, point.z))


def _distance_xy_m(first: Vec3, second: Vec3) -> float:
    return math.hypot(first.x - second.x, first.y - second.y) / 100.0


def _distance_point_to_segment_m(point: Vec3, first: Vec3, second: Vec3) -> tuple[float, float]:
    dx = second.x - first.x
    dy = second.y - first.y
    denominator = dx * dx + dy * dy
    if denominator <= 1e-9:
        return _distance_xy_m(point, first), 0.0
    amount = ((point.x - first.x) * dx + (point.y - first.y) * dy) / denominator
    amount = max(0.0, min(1.0, amount))
    closest = Vec3(first.x + amount * dx, first.y + amount * dy, first.z + amount * (second.z - first.z))
    return _distance_xy_m(point, closest), amount


def _segment_distance_m(first: Vec3, second: Vec3, third: Vec3, fourth: Vec3) -> float:
    """Bounded 2D segment distance using endpoint projections/intersection."""

    def cross(ax: float, ay: float, bx: float, by: float) -> float:
        return ax * by - ay * bx

    rx, ry = second.x - first.x, second.y - first.y
    sx, sy = fourth.x - third.x, fourth.y - third.y
    denominator = cross(rx, ry, sx, sy)
    qx, qy = third.x - first.x, third.y - first.y
    if abs(denominator) > 1e-9:
        t = cross(qx, qy, sx, sy) / denominator
        u = cross(qx, qy, rx, ry) / denominator
        if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
            return 0.0
    return min(
        _distance_point_to_segment_m(first, third, fourth)[0],
        _distance_point_to_segment_m(second, third, fourth)[0],
        _distance_point_to_segment_m(third, first, second)[0],
        _distance_point_to_segment_m(fourth, first, second)[0],
    )


# A route-sweep contact is where Cloud's body touched something, so it bears
# only on route points at that height: from a little below his feet to just
# over his head. Live 2026-10-06 at Cosmo Canyon the climb to Ka'dina doubles
# back over itself; contacts on the lower flight, 1.9-2.9 m down, walled off
# the flight above, and no route could reach her.
CONTACT_BELOW_FLOOR_M = 0.5
CONTACT_ABOVE_FLOOR_M = 2.2


def _contact_on_another_level(first: Vec3, second: Vec3, obstacle: StaticObstacle) -> bool:
    """A sweep contact above or below the route where it passes (z in cm).

    A contact recorded without a height (z 0) is not judged by it."""
    if "route_sweep_contact" not in obstacle.provenance or obstacle.center.z == 0.0:
        return False
    _, amount = _distance_point_to_segment_m(obstacle.center, first, second)
    floor = first.z + amount * (second.z - first.z)
    rise_m = (obstacle.center.z - floor) / 100.0
    return not -CONTACT_BELOW_FLOOR_M <= rise_m <= CONTACT_ABOVE_FLOOR_M


def obstacle_segment_clearance_m(first: Vec3, second: Vec3, obstacle: StaticObstacle) -> float:
    axis_first, axis_second = obstacle.axis_endpoints()
    clearance = _segment_distance_m(first, second, axis_first, axis_second) - obstacle.effective_radius_m
    if clearance <= 0.0 and _contact_on_another_level(first, second, obstacle):
        return math.inf
    return clearance


def route_obstacle_hits(route: Sequence[Vec3], obstacle: StaticObstacle) -> tuple[int, ...]:
    if len(route) < 2:
        return tuple()
    return tuple(
        index for index, (first, second) in enumerate(zip(route, route[1:]))
        if obstacle_segment_clearance_m(first, second, obstacle) <= 0.0
    )


def _point_at_distance(route: Sequence[Vec3], distance_m: float) -> Vec3:
    if not route:
        raise StaticObstaclePlanningError("route_geometry_missing")
    if len(route) == 1:
        return route[0]
    remaining = max(0.0, float(distance_m))
    for first, second in zip(route, route[1:]):
        length = _distance_xy_m(first, second)
        if length <= 1e-9:
            continue
        if remaining <= length:
            amount = remaining / length
            return Vec3(
                first.x + amount * (second.x - first.x),
                first.y + amount * (second.y - first.y),
                first.z + amount * (second.z - first.z),
            )
        remaining -= length
    return route[-1]


def _route_projection(route: Sequence[Vec3], point: Vec3) -> tuple[float, int, float]:
    best: tuple[float, int, float, float] | None = None
    along = 0.0
    for index, (first, second) in enumerate(zip(route, route[1:])):
        distance, amount = _distance_point_to_segment_m(point, first, second)
        segment_length = _distance_xy_m(first, second)
        candidate = (distance, index, amount, along + segment_length * amount)
        if best is None or candidate < best:
            best = candidate
        along += segment_length
    if best is None:
        return 0.0, 0, 0.0
    return best[3], best[1], best[2]


def _point_in_convex_polygon(point: Vec3, polygon: Any) -> bool:
    vertices = tuple(getattr(polygon, "vertices", ()))
    if len(vertices) < 3:
        return False
    signs: list[float] = []
    for first, second in zip(vertices, vertices[1:] + vertices[:1]):
        cross = (second.x - first.x) * (point.y - first.y) - (second.y - first.y) * (point.x - first.x)
        if abs(cross) > 1e-5:
            signs.append(cross)
    return not signs or all(value >= -1e-5 for value in signs) or all(value <= 1e-5 for value in signs)


def _polygon_edge_clearance_m(point: Vec3, polygon: Any) -> float:
    vertices = tuple(getattr(polygon, "vertices", ()))
    if len(vertices) < 2:
        return 0.0
    return min(
        _distance_point_to_segment_m(point, first, second)[0]
        for first, second in zip(vertices, vertices[1:] + vertices[:1])
    )


def _segment_intersection_parameter(first: Vec3, second: Vec3, third: Vec3, fourth: Vec3) -> float | None:
    """Return the first segment parameter for a proper 2D intersection."""

    def cross(ax: float, ay: float, bx: float, by: float) -> float:
        return ax * by - ay * bx

    rx, ry = second.x - first.x, second.y - first.y
    sx, sy = fourth.x - third.x, fourth.y - third.y
    denominator = cross(rx, ry, sx, sy)
    if abs(denominator) <= 1.0e-9:
        return None
    qx, qy = third.x - first.x, third.y - first.y
    t = cross(qx, qy, sx, sy) / denominator
    u = cross(qx, qy, rx, ry) / denominator
    if -1.0e-7 <= t <= 1.0 + 1.0e-7 and -1.0e-7 <= u <= 1.0 + 1.0e-7:
        return max(0.0, min(1.0, t))
    return None


def _portal_interval_parameters(
    edge_first: Vec3,
    edge_second: Vec3,
    portal: Any,
    tolerance_m: float,
) -> tuple[float, float] | None:
    """Return the clipped portal's ordered parameters on its canonical edge.

    The caller selects the edge from ``DetourPortal.source_edge`` or
    ``target_edge``.  This helper only projects the exact ``left``/``right``
    interval and applies the same tolerance used while constructing the
    canonical Detour portal.
    """

    first_distance, first_parameter = _distance_point_to_segment_m(portal.left, edge_first, edge_second)
    second_distance, second_parameter = _distance_point_to_segment_m(portal.right, edge_first, edge_second)
    if first_distance > tolerance_m or second_distance > tolerance_m:
        return None
    return min(first_parameter, second_parameter), max(first_parameter, second_parameter)


def _canonical_portal_edge_index(polygon: Any, portal: Any, field_name: str) -> int | None:
    """Return a portal's canonical edge index, without selecting a fallback edge."""

    raw_index = getattr(portal, field_name, None)
    if isinstance(raw_index, bool):
        return None
    try:
        edge_index = int(raw_index)
    except (TypeError, ValueError, OverflowError):
        return None
    if edge_index < 0 or edge_index >= len(tuple(getattr(polygon, "vertices", ()))):
        return None
    return edge_index


def _closed_edge_segments(
    edge_first: Vec3,
    edge_second: Vec3,
    authorized_portals: Sequence[Any],
    *,
    portal_tolerance_m: float,
) -> tuple[tuple[Vec3, Vec3], ...]:
    """Split an edge and return only its non-portal boundary portions."""

    intervals = [
        interval
        for portal in authorized_portals
        for interval in (_portal_interval_parameters(edge_first, edge_second, portal, portal_tolerance_m),)
        if interval is not None and interval[1] - interval[0] > 1.0e-7
    ]
    if not intervals:
        return ((edge_first, edge_second),)
    cuts = sorted({0.0, 1.0, *(value for interval in intervals for value in interval)})
    closed: list[tuple[Vec3, Vec3]] = []
    for start, end in zip(cuts, cuts[1:]):
        if end - start <= 1.0e-7:
            continue
        midpoint = (start + end) / 2.0
        if any(portal_start - 1.0e-7 <= midpoint <= portal_end + 1.0e-7 for portal_start, portal_end in intervals):
            continue
        first = Vec3(
            edge_first.x + (edge_second.x - edge_first.x) * start,
            edge_first.y + (edge_second.y - edge_first.y) * start,
            edge_first.z + (edge_second.z - edge_first.z) * start,
        )
        second = Vec3(
            edge_first.x + (edge_second.x - edge_first.x) * end,
            edge_first.y + (edge_second.y - edge_first.y) * end,
            edge_first.z + (edge_second.z - edge_first.z) * end,
        )
        closed.append((first, second))
    return tuple(closed)


def _segment_near_polygon_surface(first, second, polygon, max_vertical_m):
    """Bound height above the traversed triangulated surface, not distant vertices.

    Surface and motion heights are affine within each fan triangle. Their
    maximum separation lies at an interval endpoint or a crossed fan edge.
    Checking all such breakpoints also catches non-planar interior ridges.
    """
    parameters = [0.0, 1.0]
    vertices = tuple(polygon.vertices)
    if len(vertices) < 3 or not all(_finite_point(v) for v in vertices):
        return False
    for vertex in vertices[2:-1]:
        parameter = _segment_intersection_parameter(first, second, vertices[0], vertex)
        if parameter is not None and 0 < parameter < 1:
            parameters.append(parameter)
    for amount in parameters:
        point = Vec3(first.x + (second.x-first.x)*amount,
                     first.y + (second.y-first.y)*amount,
                     first.z + (second.z-first.z)*amount)
        _, contained, horizontal_cm, vertical_cm = _closest_point_on_polygon(point, polygon)
        if not contained or horizontal_cm > 1e-3 or not math.isfinite(vertical_cm) or vertical_cm / 100 > max_vertical_m:
            return False
    return True


def _segment_authorized_corridor(
    first: Vec3,
    second: Vec3,
    *,
    graph: DetourTopologyGraph,
    corridor_refs: Sequence[int],
    portals: Sequence[Any],
    minimum_edge_clearance_m: float,
    max_vertical_separation_m: float,
) -> bool:
    """Prove one inserted segment stays in the ordered corridor.

    Convexity proves containment within each interval, but every interval is
    still checked for non-portal edge clearance.  A boundary crossing is
    accepted only when the ordered Detour portal transition proves it.
    """

    if not corridor_refs or not _finite_point(first) or not _finite_point(second):
        return False
    try:
        portal_tolerance_cm = float(graph.portal_tolerance_cm)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(portal_tolerance_cm) or portal_tolerance_cm < 0.0:
        return False
    portal_tolerance_m = portal_tolerance_cm / 100.0
    corridor_polygons = [graph.polygons[reference] for reference in corridor_refs if reference in graph.polygons]
    if len(corridor_polygons) != len(corridor_refs):
        return False
    if abs(first.z - second.z) / 100.0 > max_vertical_separation_m:
        return False
    crossings: list[tuple[float, Any]] = []
    for portal in portals:
        parameter = _segment_intersection_parameter(first, second, portal.left, portal.right)
        if parameter is not None and 1.0e-7 < parameter < 1.0 - 1.0e-7:
            crossings.append((parameter, portal))
    crossings.sort(key=lambda item: (item[0], int(item[1].from_reference), int(item[1].to_reference)))
    boundaries = [0.0, *(item[0] for item in crossings), 1.0]
    interval_refs: list[int] = []
    for start_t, end_t in zip(boundaries, boundaries[1:]):
        midpoint_t = (start_t + end_t) / 2.0
        midpoint = Vec3(
            first.x + (second.x - first.x) * midpoint_t,
            first.y + (second.y - first.y) * midpoint_t,
            first.z + (second.z - first.z) * midpoint_t,
        )
        containing = [
            reference for reference, polygon in zip(corridor_refs, corridor_polygons)
            if _point_in_convex_polygon(midpoint, polygon)
        ]
        if not containing:
            return False
        interval_refs.append(containing[0])
        interval_first = Vec3(
            first.x + (second.x - first.x) * start_t,
            first.y + (second.y - first.y) * start_t,
            first.z + (second.z - first.z) * start_t,
        )
        interval_second = Vec3(
            first.x + (second.x - first.x) * end_t,
            first.y + (second.y - first.y) * end_t,
            first.z + (second.z - first.z) * end_t,
        )
        polygon = graph.polygons[interval_refs[-1]]
        # Distant vertices on a sloping polygon do not determine the height
        # of this short traversed interval. Prove local surface proximity at
        # every piecewise-linear breakpoint under the same vertical bound.
        if not _segment_near_polygon_surface(interval_first, interval_second, polygon, max_vertical_separation_m):
            return False
        if not _point_in_convex_polygon(interval_first, polygon) or not _point_in_convex_polygon(interval_second, polygon):
            return False
        canonical_portal_edges: dict[int, list[Any]] = {}
        for crossing_t, portal in crossings:
            # A source polygon may exempt the outgoing edge only for the
            # directed transition it authorizes; the following polygon may
            # exempt its incoming edge for the same portal.  The edge index
            # is canonical graph data; do not rediscover it by scanning
            # geometry with a tighter, independent tolerance.
            if abs(crossing_t - end_t) <= 1.0e-6 and int(portal.from_reference) == int(interval_refs[-1]):
                edge_index = _canonical_portal_edge_index(polygon, portal, "source_edge")
                if edge_index is None:
                    return False
                canonical_portal_edges.setdefault(edge_index, []).append(portal)
            if abs(crossing_t - start_t) <= 1.0e-6 and int(portal.to_reference) == int(interval_refs[-1]):
                edge_index = _canonical_portal_edge_index(polygon, portal, "target_edge")
                if edge_index is None:
                    return False
                canonical_portal_edges.setdefault(edge_index, []).append(portal)
        vertices = tuple(getattr(polygon, "vertices", ()))
        for edge_index, matching_portals in canonical_portal_edges.items():
            edge_first, edge_second = vertices[edge_index], vertices[(edge_index + 1) % len(vertices)]
            for portal in matching_portals:
                interval = _portal_interval_parameters(
                    edge_first,
                    edge_second,
                    portal,
                    portal_tolerance_m,
                )
                if interval is None or interval[1] - interval[0] <= 1.0e-7:
                    return False
        for edge_index, (edge_first, edge_second) in enumerate(zip(vertices, vertices[1:] + vertices[:1])):
            matching_portals = canonical_portal_edges.get(edge_index, ())
            for closed_edge_first, closed_edge_second in _closed_edge_segments(
                edge_first,
                edge_second,
                matching_portals,
                portal_tolerance_m=portal_tolerance_m,
            ):
                if _segment_distance_m(
                    interval_first,
                    interval_second,
                    closed_edge_first,
                    closed_edge_second,
                ) < minimum_edge_clearance_m:
                    return False
    for (previous_ref, next_ref), (crossing_t, portal) in zip(zip(interval_refs, interval_refs[1:]), crossings):
        if int(portal.from_reference) != int(previous_ref) or int(portal.to_reference) != int(next_ref):
            return False
    return bool(interval_refs)


class StaticObstacleOverlay:
    """Bounded learner whose state is explicitly bound to one route session."""

    def __init__(
        self,
        *,
        max_obstacles: int = DEFAULT_MAX_OBSTACLES,
        max_evidence: int = DEFAULT_MAX_EVIDENCE,
        minimum_confirmations: int = 2,
        maximum_evidence_age_seconds: float = DEFAULT_MAX_EVIDENCE_AGE_SECONDS,
        probable_radius_m: float = DEFAULT_PROBABLE_RADIUS_M,
        cloud_clearance_m: float = DEFAULT_CLOUD_CLEARANCE_M,
        uncertainty_margin_m: float = DEFAULT_UNCERTAINTY_MARGIN_M,
        no_progress_displacement_m: float = DEFAULT_NO_PROGRESS_DISPLACEMENT_M,
        no_progress_route_delta_m: float = DEFAULT_NO_PROGRESS_ROUTE_DELTA_M,
        discovery_distance_m: float = 2.0,
        merge_distance_m: float = 1.25,
        barrier_merge_distance_m: float = DEFAULT_BARRIER_MERGE_DISTANCE_M,
        barrier_min_span_m: float = DEFAULT_BARRIER_MIN_SPAN_M,
        barrier_max_residual_m: float = DEFAULT_BARRIER_MAX_RESIDUAL_M,
        barrier_max_samples: int = DEFAULT_BARRIER_MAX_SAMPLES,
        store: StaticObstacleStore | None = None,
    ) -> None:
        if int(max_obstacles) <= 0 or int(max_evidence) <= 0 or int(minimum_confirmations) <= 0:
            raise ValueError("static obstacle bounds must be positive")
        if not math.isfinite(float(maximum_evidence_age_seconds)) or maximum_evidence_age_seconds <= 0.0:
            raise ValueError("maximum_evidence_age_seconds must be positive")
        if int(barrier_max_samples) < 3:
            raise ValueError("barrier_max_samples must be at least three")
        for value in (
            probable_radius_m, cloud_clearance_m, uncertainty_margin_m,
            no_progress_displacement_m, no_progress_route_delta_m,
            discovery_distance_m, merge_distance_m, barrier_merge_distance_m,
            barrier_min_span_m, barrier_max_residual_m,
        ):
            if not math.isfinite(float(value)) or float(value) <= 0.0:
                raise ValueError("static obstacle geometry/configuration must be positive")
        self.max_obstacles = int(max_obstacles)
        self.max_evidence = int(max_evidence)
        self.minimum_confirmations = int(minimum_confirmations)
        self.maximum_evidence_age_seconds = float(maximum_evidence_age_seconds)
        self.probable_radius_m = float(probable_radius_m)
        self.cloud_clearance_m = float(cloud_clearance_m)
        self.uncertainty_margin_m = float(uncertainty_margin_m)
        self.no_progress_displacement_m = float(no_progress_displacement_m)
        self.no_progress_route_delta_m = float(no_progress_route_delta_m)
        self.discovery_distance_m = float(discovery_distance_m)
        self.merge_distance_m = float(merge_distance_m)
        self.barrier_merge_distance_m = float(barrier_merge_distance_m)
        self.barrier_min_span_m = float(barrier_min_span_m)
        self.barrier_max_residual_m = float(barrier_max_residual_m)
        self.barrier_max_samples = int(barrier_max_samples)
        self.store = store
        self.scope: StaticObstacleScope | None = None
        self.durable_identity: StaticObstacleDurableIdentity | None = None
        self._obstacles: dict[str, StaticObstacle] = {}
        self._accumulators: dict[str, _Accumulator] = {}
        self._evidence_total = 0
        self.generation = 0

    def bind_scope(
        self,
        scope: StaticObstacleScope,
        *,
        durable_identity: StaticObstacleDurableIdentity | None = None,
    ) -> StaticObstacleSnapshot:
        if self.scope != scope or self.durable_identity != durable_identity:
            self.scope = scope
            self.durable_identity = durable_identity
            self._obstacles.clear()
            self._accumulators.clear()
            self._evidence_total = 0
            self.generation += 1
            if self.store is not None:
                self.store.bind_identity(durable_identity)
                for obstacle in self.store.load_for_scope(scope, durable_identity):
                    self._obstacles[obstacle.obstacle_id] = obstacle
        return self.snapshot()

    def capsule_hit_intersects_segment(
        self, first: Vec3, second: Vec3, *, probe_bearing_degrees: float,
        blocking_distance_m: float, capsule_radius_m: float,
    ) -> bool | None:
        """Assess the measured sweep's modeled footprint without learning it.

        A fan ray beside the intended movement is evidence about that ray;
        it must not stop every direction in the fan. This uses the same contact
        distance, radii and clearance as the obstacle learner below.
        """
        values = (probe_bearing_degrees, blocking_distance_m, capsule_radius_m)
        if (not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)
            or blocking_distance_m < 0 or capsule_radius_m < 0):
            return None
        distance_cm = (min(50.0, blocking_distance_m) + capsule_radius_m + self.probable_radius_m) * 100.0
        radians = math.radians(probe_bearing_degrees)
        center = Vec3(first.x + math.cos(radians) * distance_cm,
                      first.y + math.sin(radians) * distance_cm, first.z)
        effective_radius = self.probable_radius_m + self.cloud_clearance_m + 0.10
        return _distance_point_to_segment_m(center, first, second)[0] <= effective_radius

    def clear(self, reason_code: str = "explicit_clear") -> StaticObstacleSnapshot:
        self._obstacles.clear()
        self._accumulators.clear()
        self._evidence_total = 0
        self.generation += 1
        return self.snapshot(reason_code=reason_code)

    def admit_obstacle(
        self,
        *,
        scope: StaticObstacleScope,
        obstacle: StaticObstacle,
        reason_code: str = "bounded_external_confirmation",
        promotion_evidence: StaticObstaclePromotionEvidence | None = None,
    ) -> StaticObstacleSnapshot:
        """Admit already-shaped evidence into the current bounded scope.

        Runtime callers normally use :meth:`learn_from_movement`.  This
        narrow seam is also useful for a trusted, bounded collision query and
        deterministic geometry fixtures; it never mutates the Detour graph.
        """
        if self.scope != scope:
            raise StaticObstacleScopeError("static_obstacle_scope_mismatch")
        if not isinstance(obstacle, StaticObstacle):
            raise StaticObstacleError("static_obstacle_type_invalid")
        admitted = replace(obstacle, persistence="session")
        if self.store is not None:
            admitted = self.store.record(
                scope=scope,
                identity=self.durable_identity,
                obstacle=admitted,
                promotion_evidence=promotion_evidence,
            )
        self._obstacles[admitted.obstacle_id] = admitted
        self._evidence_total = min(self.max_evidence, self._evidence_total + obstacle.evidence_count)
        self.generation += 1
        self._trim_obstacles()
        return self.snapshot(reason_code=reason_code)

    def discard_obstacle(self, obstacle_id: str) -> bool:
        """Forget one obstacle (a stall guess the collision sweep overruled)."""
        if obstacle_id not in self._obstacles:
            return False
        self._obstacles.pop(obstacle_id, None)
        if self.store is not None and self.scope is not None:
            self.store.remove_session_obstacles(self.scope, {obstacle_id})
        self.generation += 1
        return True

    def snapshot(self, *, now_t: float | None = None, reason_code: str | None = None) -> StaticObstacleSnapshot:
        self._prune_accumulators(now_t)
        state = ObstacleEvidenceState.UNKNOWN if self.scope is None else ObstacleEvidenceState.READY
        reason = reason_code or ("static_obstacle_scope_missing" if self.scope is None else "no_static_obstacles")
        stale_ids: tuple[str, ...] = ()
        usable = tuple(sorted(self._obstacles.values(), key=lambda item: item.obstacle_id))
        if self.scope is not None and now_t is not None:
            stale_ids = tuple(sorted(
                obstacle.obstacle_id
                for obstacle in usable
                if obstacle.persistence != "durable"
                and float(now_t) - obstacle.last_t > self.maximum_evidence_age_seconds
            ))
            usable = tuple(item for item in usable if item.obstacle_id not in stale_ids)
            if stale_ids and not usable:
                state = ObstacleEvidenceState.STALE
                reason = "stale_static_obstacle_evidence"
        if self.scope is not None and usable and state != ObstacleEvidenceState.STALE:
            reason = "static_obstacles_ready"
        return StaticObstacleSnapshot(
            scope=self.scope,
            state=state,
            generation=self.generation,
            obstacles=usable,
            candidate_count=len(self._accumulators),
            evidence_count=self._evidence_total,
            reason_code=reason,
            stale_obstacle_ids=stale_ids,
        )

    def planning_snapshot(self, *, now_t: float | None = None) -> StaticObstacleSnapshot:
        snapshot = self.snapshot(now_t=now_t)
        if snapshot.scope is None:
            raise StaticObstacleScopeError(snapshot.reason_code)
        if snapshot.state == ObstacleEvidenceState.STALE:
            raise StaticObstaclePlanningError(snapshot.reason_code)
        return snapshot

    def reset_movement_candidates(self, *, scope: StaticObstacleScope) -> int:
        """Progress breaks a run of unverified no-progress evidence.

        Previously committed obstacles and independent capsule evidence remain
        intact. Only provisional movement-only sample accumulators are reset.
        """
        if self.scope != scope:
            raise StaticObstacleScopeError("static_obstacle_scope_mismatch")
        keys = [key for key, item in self._accumulators.items() if not item.query_confirmed]
        removed = 0
        for key in keys:
            removed += self._accumulators.pop(key).count
        self._evidence_total = max(0, self._evidence_total - removed)
        return removed

    def learn_from_movement(
        self,
        *,
        scope: StaticObstacleScope,
        route_plan_sha256: str,
        route: Sequence[Vec3],
        position_before: Vec3,
        position_after: Vec3,
        along_route_before_m: float | None,
        along_route_after_m: float | None,
        commanded_bearing_degrees: float | None,
        command_duration_ms: int,
        sequence: int,
        sampled_t: float,
        telemetry_fresh: bool = True,
        query_status: str | None = None,
        query_provenance: str | None = None,
        query_verified: bool = False,
        blocking_distance_m: float | None = None,
        capsule_radius_m: float | None = None,
        probe_world_bearing_degrees: float | None = None,
        dynamic_evidence: DynamicObstacleStaticEvidence | None = None,
        require_independent_confirmation: bool = False,
        defer_movement_commit: bool = False,
    ) -> StaticObstacleLearningResult:
        if self.scope != scope:
            return StaticObstacleLearningResult(ObstacleEvidenceState.UNKNOWN, "static_obstacle_scope_mismatch", None, 0, self.generation)
        if not telemetry_fresh:
            return StaticObstacleLearningResult(ObstacleEvidenceState.UNKNOWN, "static_obstacle_telemetry_not_fresh", None, 0, self.generation)
        if not route_plan_sha256 or len(route) < 2 or not _finite_point(position_before) or not _finite_point(position_after):
            return StaticObstacleLearningResult(ObstacleEvidenceState.UNKNOWN, "static_obstacle_learning_input_incomplete", None, 0, self.generation)
        if int(command_duration_ms) <= 0 or int(sequence) < 0 or not math.isfinite(float(sampled_t)):
            return StaticObstacleLearningResult(ObstacleEvidenceState.UNKNOWN, "static_obstacle_command_evidence_invalid", None, 0, self.generation)
        if dynamic_evidence is not None and not isinstance(dynamic_evidence, DynamicObstacleStaticEvidence):
            return StaticObstacleLearningResult(ObstacleEvidenceState.UNKNOWN, "dynamic_obstacle_evidence_type_invalid", None, 0, self.generation)
        dynamic_state = dynamic_evidence.state if dynamic_evidence is not None else StaticLearningDynamicState.ABSENT
        if dynamic_state == StaticLearningDynamicState.BLOCKING:
            return StaticObstacleLearningResult(ObstacleEvidenceState.UNKNOWN, "dynamic_actor_evidence_not_static", None, 0, self.generation)
        dynamic_ambiguous = dynamic_state != StaticLearningDynamicState.CLEAR_COMPLETE
        displacement = _distance_xy_m(position_before, position_after)
        if along_route_before_m is None or along_route_after_m is None:
            route_delta = 0.0
        else:
            route_delta = float(along_route_after_m) - float(along_route_before_m)
        if not math.isfinite(route_delta):
            return StaticObstacleLearningResult(ObstacleEvidenceState.UNKNOWN, "static_obstacle_route_progress_invalid", None, 0, self.generation)
        query = str(query_status or "").lower()
        query_blocked = query == "blocked" and bool(query_verified)
        if displacement > self.no_progress_displacement_m or route_delta > self.no_progress_route_delta_m:
            self.reset_movement_candidates(scope=scope)
            return StaticObstacleLearningResult(ObstacleEvidenceState.UNKNOWN, "movement_progress_observed", None, 0, self.generation)
        if query and query not in {"blocked", "clear", "unknown"}:
            return StaticObstacleLearningResult(ObstacleEvidenceState.UNKNOWN, "static_obstacle_query_uncertain", None, 0, self.generation)
        if query == "clear" and not query_verified:
            return StaticObstacleLearningResult(ObstacleEvidenceState.UNKNOWN, "static_obstacle_query_uncertain", None, 0, self.generation)
        if query == "clear":
            return StaticObstacleLearningResult(ObstacleEvidenceState.UNKNOWN, "lookahead_clear_no_blocker_learned", None, 0, self.generation)

        along = float(along_route_before_m) if along_route_before_m is not None else _route_projection(route, position_before)[0]
        center = _point_at_distance(route, along + self.discovery_distance_m)
        route_bearing = (
            float(commanded_bearing_degrees)
            if commanded_bearing_degrees is not None and math.isfinite(float(commanded_bearing_degrees))
            else None
        )
        probe_bearing = (
            float(probe_world_bearing_degrees)
            if probe_world_bearing_degrees is not None and math.isfinite(float(probe_world_bearing_degrees))
            else None
        )
        if query_blocked:
            try:
                blocking_distance = float(blocking_distance_m)
                capsule_radius = float(capsule_radius_m)
            except (TypeError, ValueError, OverflowError):
                return StaticObstacleLearningResult(
                    ObstacleEvidenceState.UNKNOWN,
                    "static_obstacle_capsule_geometry_invalid",
                    None,
                    0,
                    self.generation,
                )
            if (
                not math.isfinite(blocking_distance)
                or blocking_distance < 0.0
                or capsule_radius <= 0.0
                or not math.isfinite(capsule_radius)
                or probe_bearing is None
                or not math.isfinite(float(probe_bearing))
            ):
                return StaticObstacleLearningResult(
                    ObstacleEvidenceState.UNKNOWN,
                    "static_obstacle_capsule_geometry_invalid",
                    None,
                    0,
                    self.generation,
                )
        if (
            query_blocked
            and blocking_distance_m is not None
            and math.isfinite(float(blocking_distance_m))
            and float(blocking_distance_m) >= 0.0
            and probe_bearing is not None
        ):
            radians = math.radians(probe_bearing)
            # Capsule sweep distance ends at contact; recover the obstacle
            # centre by adding the physical sweep and modeled obstacle radii.
            distance_cm = (
                min(50.0, blocking_distance)
                + capsule_radius
                + self.probable_radius_m
            ) * 100.0
            center = Vec3(
                position_before.x + math.cos(radians) * distance_cm,
                position_before.y + math.sin(radians) * distance_cm,
                position_before.z,
            )
        key = self._matching_key(center)
        provenance = {"movement_no_progress", f"dynamic_evidence_{dynamic_state.value}"}
        if query_blocked:
            provenance.add(query_provenance or "bounded_lookahead_blocked")
        accumulator = self._accumulators.get(key)
        if accumulator is None:
            accumulator = _Accumulator(
                center, 0, set(), int(sequence), int(sequence), float(sampled_t), float(sampled_t),
                0.0, False, set(), [], route_bearing, probe_bearing, set(),
            )
            self._accumulators[key] = accumulator
        elif int(sequence) in accumulator.sample_sequences:
            return StaticObstacleLearningResult(
                ObstacleEvidenceState.CANDIDATE,
                "static_obstacle_duplicate_evidence",
                None,
                accumulator.count,
                self.generation,
            )
        accumulator.count += 1
        accumulator.sample_sequences.add(int(sequence))
        accumulator.last_sequence = int(sequence)
        accumulator.last_t = float(sampled_t)
        accumulator.center = Vec3(
            (accumulator.center.x + center.x) / 2.0,
            (accumulator.center.y + center.y) / 2.0,
            (accumulator.center.z + center.z) / 2.0,
        )
        accumulator.samples.append(center)
        if len(accumulator.samples) > self.barrier_max_samples:
            del accumulator.samples[: len(accumulator.samples) - self.barrier_max_samples]
        if route_bearing is not None:
            accumulator.route_bearing_degrees = route_bearing
        if probe_bearing is not None:
            accumulator.probe_bearing_degrees = probe_bearing
        if accumulator.dynamic_states is None:
            accumulator.dynamic_states = set()
        accumulator.dynamic_states.add(dynamic_state.value)
        accumulator.query_confirmed = accumulator.query_confirmed or query_blocked
        accumulator.confidence = min(0.99, max(accumulator.confidence, 0.55 + 0.18 * accumulator.count + (0.18 if query_blocked else 0.0)))
        accumulator.provenance.update(provenance)
        self._evidence_total += 1
        if self._evidence_total > self.max_evidence:
            self._trim_evidence()
        required_confirmations = max(self.minimum_confirmations, 3 if dynamic_ambiguous else self.minimum_confirmations)
        confirmed = (
            accumulator.count >= required_confirmations
            if require_independent_confirmation
            else accumulator.query_confirmed or accumulator.count >= required_confirmations
        )
        if not confirmed:
            return StaticObstacleLearningResult(ObstacleEvidenceState.CANDIDATE, "static_obstacle_candidate_recorded", None, accumulator.count, self.generation)
        obstacle_id = self._obstacle_id(accumulator.center)
        obstacle = StaticObstacle(
            obstacle_id=obstacle_id,
            center=accumulator.center,
            radius_m=self.probable_radius_m,
            cloud_clearance_m=self.cloud_clearance_m,
            uncertainty_margin_m=0.10 if accumulator.query_confirmed else self.uncertainty_margin_m,
            confidence=accumulator.confidence,
            evidence_count=accumulator.count,
            first_sequence=accumulator.first_sequence,
            last_sequence=accumulator.last_sequence,
            first_t=accumulator.first_t,
            last_t=accumulator.last_t,
            provenance=tuple(sorted(accumulator.provenance)),
            route_bearing_degrees=accumulator.route_bearing_degrees,
            probe_bearing_degrees=accumulator.probe_bearing_degrees,
        )
        if query_blocked:
            obstacle = replace(obstacle, confidence=max(obstacle.confidence, 0.96), provenance=tuple(sorted(set(obstacle.provenance) | {"runtime_capsule_blocked"})))
        if defer_movement_commit and not accumulator.query_confirmed:
            return StaticObstacleLearningResult(ObstacleEvidenceState.CANDIDATE,
                "static_obstacle_pending_settled_confirmation", obstacle, accumulator.count, self.generation)
        if self.store is not None:
            obstacle = self.store.record(
                scope=scope,
                identity=self.durable_identity,
                obstacle=obstacle,
            )
        self._obstacles[obstacle_id] = obstacle
        merged_obstacle = self._merge_aligned_runtime_obstacles(scope, obstacle)
        self.generation += 1
        self._trim_obstacles()
        return StaticObstacleLearningResult(
            ObstacleEvidenceState.READY,
            "static_obstacle_confirmed",
            merged_obstacle or self._obstacles.get(obstacle_id),
            accumulator.count,
            self.generation,
        )

    def _matching_key(self, center: Vec3) -> str:
        for key, accumulator in self._accumulators.items():
            if _distance_xy_m(center, accumulator.center) <= self.merge_distance_m:
                return key
        return f"cell:{round(center.x / 50.0)}:{round(center.y / 50.0)}"

    def _obstacle_id(self, center: Vec3) -> str:
        # The spatial key is intentionally independent of runtime session so
        # two confirmed sessions can promote the same topology-cell record.
        payload = {"cell": [round(center.x / 50.0), round(center.y / 50.0)]}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "static-" + hashlib.sha256(encoded).hexdigest()[:16]

    def _prune_accumulators(self, now_t: float | None) -> None:
        if now_t is None:
            return
        expired = [
            key for key, accumulator in self._accumulators.items()
            if float(now_t) - accumulator.last_t > self.maximum_evidence_age_seconds
        ]
        for key in expired:
            accumulator = self._accumulators.pop(key)
            self._evidence_total = max(0, self._evidence_total - accumulator.count)

    def _trim_evidence(self) -> None:
        if self._evidence_total <= self.max_evidence:
            return
        ordered = sorted(self._accumulators.items(), key=lambda item: (-item[1].last_sequence, item[0]))
        while self._evidence_total > self.max_evidence and ordered:
            key, accumulator = ordered.pop()
            self._evidence_total -= accumulator.count
            self._accumulators.pop(key, None)

    def _trim_obstacles(self) -> None:
        if len(self._obstacles) <= self.max_obstacles:
            return
        ordered = sorted(self._obstacles.values(), key=lambda item: (-item.confidence, -item.evidence_count, -item.last_sequence, item.obstacle_id))
        self._obstacles = {item.obstacle_id: item for item in ordered[:self.max_obstacles]}

    def _merge_aligned_runtime_obstacles(self, scope: StaticObstacleScope, newest: StaticObstacle) -> StaticObstacle | None:
        """Merge three or more aligned runtime contacts into one capsule.

        A two-point coincidence is deliberately insufficient: it would turn
        two nearby trees into a wall.  The bounded collinearity and span
        checks provide a small deterministic occupancy-barrier equivalent
        without modifying Detour polygons.
        """

        candidates = [
            item for item in self._obstacles.values()
            if item.persistence == "session"
            and _distance_xy_m(item.center, newest.center) <= self.barrier_merge_distance_m
        ]
        if len(candidates) < 3:
            return None
        farthest = max(
            ((left, right, _distance_xy_m(left.center, right.center)) for left in candidates for right in candidates if left.obstacle_id < right.obstacle_id),
            key=lambda item: (item[2], item[0].obstacle_id, item[1].obstacle_id),
            default=None,
        )
        if farthest is None or farthest[2] < self.barrier_min_span_m:
            return None
        left, right, span = farthest
        dx = (right.center.x - left.center.x) / 100.0
        dy = (right.center.y - left.center.y) / 100.0
        length = math.hypot(dx, dy)
        if length <= 1.0e-9:
            return None
        axis_x, axis_y = dx / length, dy / length
        midpoint = Vec3(
            (left.center.x + right.center.x) / 2.0,
            (left.center.y + right.center.y) / 2.0,
            (left.center.z + right.center.z) / 2.0,
        )
        aligned = [
            item for item in candidates
            if abs(
                -axis_y * ((item.center.x - midpoint.x) / 100.0)
                + axis_x * ((item.center.y - midpoint.y) / 100.0)
            ) <= self.barrier_max_residual_m
        ]
        if len(aligned) < 3:
            return None
        min_projection = min(
            axis_x * ((item.center.x - midpoint.x) / 100.0)
            + axis_y * ((item.center.y - midpoint.y) / 100.0)
            for item in aligned
        )
        max_projection = max(
            axis_x * ((item.center.x - midpoint.x) / 100.0)
            + axis_y * ((item.center.y - midpoint.y) / 100.0)
            for item in aligned
        )
        span = max_projection - min_projection
        if span < self.barrier_min_span_m:
            return None
        center = Vec3(
            midpoint.x + (min_projection + max_projection) * 50.0 * axis_x,
            midpoint.y + (min_projection + max_projection) * 50.0 * axis_y,
            midpoint.z,
        )
        obstacle_id = self._obstacle_id(center) + "-barrier"
        merged_provenance = set().union(*(set(item.provenance) for item in aligned))
        merged_provenance.update({"runtime_barrier_merge", "aligned_runtime_contacts"})
        merged = StaticObstacle(
            obstacle_id=obstacle_id,
            center=center,
            radius_m=max(item.radius_m for item in aligned),
            cloud_clearance_m=max(item.cloud_clearance_m for item in aligned),
            uncertainty_margin_m=max(item.uncertainty_margin_m for item in aligned),
            shape="capsule",
            half_length_m=span / 2.0,
            heading_degrees=math.degrees(math.atan2(axis_y, axis_x)),
            confidence=min(0.99, max(item.confidence for item in aligned)),
            evidence_count=sum(item.evidence_count for item in aligned),
            first_sequence=min(item.first_sequence for item in aligned),
            last_sequence=max(item.last_sequence for item in aligned),
            first_t=min(item.first_t for item in aligned),
            last_t=max(item.last_t for item in aligned),
            provenance=tuple(sorted(merged_provenance)),
            route_bearing_degrees=newest.route_bearing_degrees,
            probe_bearing_degrees=newest.probe_bearing_degrees,
        )
        old_ids = {item.obstacle_id for item in aligned}
        if self.store is not None:
            self.store.remove_session_obstacles(scope, old_ids)
            merged = self.store.record(scope=scope, identity=self.durable_identity, obstacle=merged)
        for obstacle_id in old_ids:
            self._obstacles.pop(obstacle_id, None)
        self._obstacles[merged.obstacle_id] = merged
        return merged


def _copy_plan_with_waypoints(
    plan: DetourRoutePlan,
    waypoints: Sequence[Vec3],
    *,
    resolution: str,
    obstacle_ids: Sequence[str],
) -> DetourRoutePlan:
    waypoint_tuple = tuple(waypoints)
    if getattr(plan, "traversal_segments", ()):
        plan = replace(plan, traversal_segments=with_waypoint_indices(plan.traversal_segments, waypoint_tuple))
    validation = dict(plan.validation)
    validation.update({
        "waypoints_finite": all(_finite_point(point) for point in waypoint_tuple),
        "waypoint_sequence_nonempty": bool(waypoint_tuple),
        "waypoint_count": len(waypoint_tuple),
        "waypoint_start_matches_resolved": bool(waypoint_tuple) and _distance_xy_m(waypoint_tuple[0], plan.resolved_start or waypoint_tuple[0]) <= 0.02,
        "waypoint_end_matches_resolved": bool(waypoint_tuple) and _distance_xy_m(waypoint_tuple[-1], plan.resolved_end or waypoint_tuple[-1]) <= 0.02,
        "static_obstacle_overlay_validated": True,
        "static_obstacle_ids": list(obstacle_ids),
    })
    length_m = sum(_distance_m(first, second) for first, second in zip(waypoint_tuple, waypoint_tuple[1:]))
    diagnostics = dict(plan.diagnostics)
    diagnostics.update({
        "static_obstacle_resolution": resolution,
        "static_obstacle_ids": list(obstacle_ids),
    })
    return replace(plan, waypoints=waypoint_tuple, total_estimated_path_length_m=length_m, validation=validation, diagnostics=diagnostics)


def _same_polygon_visibility_detour(
    graph: DetourTopologyGraph,
    plan: DetourRoutePlan,
    obstacles: Sequence[StaticObstacle],
    *,
    max_waypoints: int,
    minimum_edge_clearance_m: float,
    max_vertical_separation_m: float,
) -> DetourRoutePlan | None:
    """Prove a bounded bypass when a route stays inside one ground polygon."""
    corridor_refs = tuple(
        int(reference) for reference in plan.polygon_corridor
        if reference in graph.polygons and graph.polygons[reference].poly_type == DT_POLYTYPE_GROUND
    )
    if len(corridor_refs) != 1 or len(plan.waypoints) < 2 or not obstacles:
        return None
    polygon = graph.polygons[corridor_refs[0]]
    start, end = plan.waypoints[0], plan.waypoints[-1]
    if any(obstacle_segment_clearance_m(point, point, obstacle) <= 0.0 for point in (start, end) for obstacle in obstacles):
        return None
    nodes: list[Vec3] = [start, end]
    node_limit = min(128, max(2, int(max_waypoints)))
    ring_count = 32
    for obstacle in obstacles:
        if obstacle.shape != "circle":
            return None
        radius_cm = (obstacle.effective_radius_m + max(0.15, minimum_edge_clearance_m)) * 100.0 / math.cos(math.pi / ring_count)
        for index in range(ring_count):
            angle = 2.0 * math.pi * index / ring_count
            raw = Vec3(obstacle.center.x + math.cos(angle) * radius_cm, obstacle.center.y + math.sin(angle) * radius_cm, obstacle.center.z)
            closest, contained, horizontal_cm, vertical_cm = _closest_point_on_polygon(raw, polygon)
            if (not contained or horizontal_cm > 1.0e-3 or not math.isfinite(vertical_cm)
                or vertical_cm / 100.0 > max_vertical_separation_m
                or _polygon_edge_clearance_m(closest, polygon) < minimum_edge_clearance_m
                or any(obstacle_segment_clearance_m(closest, closest, item) <= 0.0 for item in obstacles)):
                continue
            if not any(_distance_xy_m(closest, existing) < 0.01 for existing in nodes):
                if len(nodes) >= node_limit:
                    break
                nodes.append(closest)
    if len(nodes) < 3:
        return None

    def authorized(first: Vec3, second: Vec3) -> bool:
        if _distance_xy_m(first, second) <= 1.0e-6:
            return False
        if any(obstacle_segment_clearance_m(first, second, item) <= 0.0 for item in obstacles):
            return False
        return _segment_authorized_corridor(
            first, second, graph=graph, corridor_refs=corridor_refs, portals=(),
            minimum_edge_clearance_m=minimum_edge_clearance_m,
            max_vertical_separation_m=max_vertical_separation_m,
        )

    costs: dict[int, float] = {0: 0.0}
    previous: dict[int, int] = {}
    queue: list[tuple[float, int]] = [(0.0, 0)]
    checks = 0
    while queue:
        cost, index = heapq.heappop(queue)
        if cost > costs.get(index, math.inf):
            continue
        if index == 1:
            break
        for target in range(len(nodes)):
            if target == index:
                continue
            checks += 1
            if checks > 8192:
                return None
            step = _distance_xy_m(nodes[index], nodes[target])
            if step <= 1.0e-6 or cost + step >= costs.get(target, math.inf):
                continue
            if not authorized(nodes[index], nodes[target]):
                continue
            costs[target] = cost + step
            previous[target] = index
            heapq.heappush(queue, (cost + step, target))
    if 1 not in previous:
        return None
    indices = [1]
    while indices[-1] != 0:
        indices.append(previous[indices[-1]])
    waypoints = tuple(nodes[index] for index in reversed(indices))
    if len(waypoints) > max_waypoints or any(route_obstacle_hits(waypoints, obstacle) for obstacle in obstacles):
        return None
    return _copy_plan_with_waypoints(plan, waypoints, resolution="same_polygon_visibility_detour", obstacle_ids=tuple(sorted(item.obstacle_id for item in obstacles)))

def _local_detour(
    graph: DetourTopologyGraph,
    plan: DetourRoutePlan,
    obstacle: StaticObstacle,
    *,
    obstacles: Sequence[StaticObstacle] = (),
    max_waypoints: int,
    minimum_edge_clearance_m: float,
    max_vertical_separation_m: float = 20.0,
) -> DetourRoutePlan | None:
    if not plan.polygon_corridor or len(plan.waypoints) < 2:
        return None
    if getattr(plan, "traversal_segments", ()):
        # An off-mesh crossing is no portal edge to prove a bypass against:
        # a route that makes one is re-planned instead.
        return None
    corridor_refs = tuple(
        int(reference) for reference in plan.polygon_corridor
        if reference in graph.polygons and graph.polygons[reference].poly_type == DT_POLYTYPE_GROUND
    )
    polygons = tuple(graph.polygons[reference] for reference in corridor_refs)
    if not polygons:
        return None
    hits = route_obstacle_hits(plan.waypoints, obstacle)
    if not hits:
        return None
    index = hits[0]
    first, second = plan.waypoints[index], plan.waypoints[index + 1]
    dx, dy = second.x - first.x, second.y - first.y
    length_cm = math.hypot(dx, dy)
    if length_cm <= 1e-6:
        return None
    tx, ty = dx / length_cm, dy / length_cm
    nx, ny = -ty, tx
    projection_distance_m = _route_projection(plan.waypoints, obstacle.center)[0]
    projection = _point_at_distance(plan.waypoints, projection_distance_m)
    radius = obstacle.effective_radius_m + max(0.15, minimum_edge_clearance_m)
    candidates: list[tuple[float, int, int, int, int, int, tuple[Vec3, ...]]] = []
    applicable_obstacles = tuple(obstacles) or (obstacle,)
    # An off-axis hit only requires clearance from its own footprint. A
    # fixed full-radius offset from the route can leave a narrow corridor
    # despite a small, fully contained bypass on the obstacle's far side.
    lateral_center_cm = ((obstacle.center.x - projection.x) * nx
                         + (obstacle.center.y - projection.y) * ny)
    for side_order, (sign, centered) in enumerate(((1.0, True), (-1.0, True), (1.0, False), (-1.0, False))):
        lead_cm = radius * 100.0
        lateral_cm = sign * lead_cm + (lateral_center_cm if centered else 0.0)
        entry = Vec3(
            projection.x - tx * lead_cm + nx * lateral_cm,
            projection.y - ty * lead_cm + ny * lateral_cm,
            projection.z,
        )
        exit = Vec3(
            projection.x + tx * lead_cm + nx * lateral_cm,
            projection.y + ty * lead_cm + ny * lateral_cm,
            projection.z,
        )
        candidate = tuple(plan.waypoints[:index + 1] + (entry, exit) + plan.waypoints[index + 1:])
        if len(candidate) > max_waypoints:
            continue
        inserted_segments = (
            (first, entry),
            (entry, exit),
            (exit, second),
        )
        corridor_authorized = all(
            _segment_authorized_corridor(
                segment_first,
                segment_second,
                graph=graph,
                corridor_refs=corridor_refs,
                portals=plan.portals[1:-1],
                minimum_edge_clearance_m=minimum_edge_clearance_m,
                max_vertical_separation_m=max_vertical_separation_m,
            )
            for segment_first, segment_second in inserted_segments
        )
        candidate_clear = not any(
            route_obstacle_hits(candidate, item)
            for item in applicable_obstacles
        )
        if corridor_authorized and candidate_clear:
            length = sum(_distance_xy_m(left, right) for left, right in zip(candidate, candidate[1:]))
            candidates.append((length, side_order, 0, index, index, 0, candidate))
            continue
        if corridor_authorized:
            continue
        # A funnel waypoint can be a corner of an ordered portal.  Keep the
        # original, already-authorized approach/rejoin portions of the base
        # route and search only a small deterministic window for an interior
        # point where the new bypass can reconnect.  Every newly introduced
        # segment remains subject to the same corridor, directed-portal, edge,
        # vertical, and obstacle proofs; this is not a global route search.
        if not _segment_authorized_corridor(
            entry,
            exit,
            graph=graph,
            corridor_refs=corridor_refs,
            portals=plan.portals[1:-1],
            minimum_edge_clearance_m=minimum_edge_clearance_m,
            max_vertical_separation_m=max_vertical_separation_m,
        ):
            continue
        last_rejoin_segment = min(
            len(plan.waypoints) - 2,
            index + _LOCAL_REJOIN_MAX_SEGMENTS,
        )
        departure_options: list[tuple[int, int, Vec3]] = []
        for departure_order, departure_fraction in enumerate(_LOCAL_REJOIN_FRACTIONS):
            departure = Vec3(
                first.x + (entry.x - first.x) * departure_fraction,
                first.y + (entry.y - first.y) * departure_fraction,
                first.z + (entry.z - first.z) * departure_fraction,
            )
            # A departure from the hit waypoint is new geometry and must pass
            # the same proof as the other inserted segments.
            if _segment_authorized_corridor(
                first,
                departure,
                graph=graph,
                corridor_refs=corridor_refs,
                portals=plan.portals[1:-1],
                minimum_edge_clearance_m=minimum_edge_clearance_m,
                max_vertical_separation_m=max_vertical_separation_m,
            ) and _segment_authorized_corridor(
                departure,
                entry,
                graph=graph,
                corridor_refs=corridor_refs,
                portals=plan.portals[1:-1],
                minimum_edge_clearance_m=minimum_edge_clearance_m,
                max_vertical_separation_m=max_vertical_separation_m,
            ):
                departure_options.append((index, departure_order, departure))
        # If the funnel hit waypoint is a portal endpoint, the strict proof
        # intentionally rejects a new segment beginning on the closed edge.
        # A point on an earlier or the hit base segment keeps that exact,
        # graph-produced route portion and introduces only a connector from the base route.
        for departure_segment_index in range(
            max(0, index - _LOCAL_REJOIN_MAX_SEGMENTS),
            index + 1,
        ):
            base_first = plan.waypoints[departure_segment_index]
            base_second = plan.waypoints[departure_segment_index + 1]
            for departure_order, departure_fraction in enumerate(_LOCAL_REJOIN_FRACTIONS):
                departure = Vec3(
                    base_first.x + (base_second.x - base_first.x) * departure_fraction,
                    base_first.y + (base_second.y - base_first.y) * departure_fraction,
                    base_first.z + (base_second.z - base_first.z) * departure_fraction,
                )
                if _segment_authorized_corridor(
                    departure,
                    entry,
                    graph=graph,
                    corridor_refs=corridor_refs,
                    portals=plan.portals[1:-1],
                    minimum_edge_clearance_m=minimum_edge_clearance_m,
                    max_vertical_separation_m=max_vertical_separation_m,
                ):
                    departure_options.append((
                        departure_segment_index,
                        departure_order,
                        departure,
                    ))
        for departure_segment_index, departure_order, departure in departure_options:
            for rejoin_segment_index in range(index, last_rejoin_segment + 1):
                rejoin_first = plan.waypoints[rejoin_segment_index]
                rejoin_second = plan.waypoints[rejoin_segment_index + 1]
                for fraction_order, rejoin_fraction in enumerate(_LOCAL_REJOIN_FRACTIONS):
                    rejoin = Vec3(
                        rejoin_first.x + (rejoin_second.x - rejoin_first.x) * rejoin_fraction,
                        rejoin_first.y + (rejoin_second.y - rejoin_first.y) * rejoin_fraction,
                        rejoin_first.z + (rejoin_second.z - rejoin_first.z) * rejoin_fraction,
                    )
                    if not _segment_authorized_corridor(
                        exit,
                        rejoin,
                        graph=graph,
                        corridor_refs=corridor_refs,
                        portals=plan.portals[1:-1],
                        minimum_edge_clearance_m=minimum_edge_clearance_m,
                        max_vertical_separation_m=max_vertical_separation_m,
                    ):
                        continue
                    rejoined_candidate = tuple(
                        plan.waypoints[:departure_segment_index + 1]
                        + (departure, entry, exit, rejoin)
                        + plan.waypoints[rejoin_segment_index + 1:]
                    )
                    if len(rejoined_candidate) > max_waypoints:
                        continue
                    if any(
                        route_obstacle_hits(rejoined_candidate, item)
                        for item in applicable_obstacles
                    ):
                        continue
                    length = sum(
                        _distance_xy_m(left, right)
                        for left, right in zip(rejoined_candidate, rejoined_candidate[1:])
                    )
                    candidates.append((
                        length,
                        side_order,
                        1,
                        departure_segment_index,
                        rejoin_segment_index,
                        fraction_order + departure_order,
                        rejoined_candidate,
                    ))
    if not candidates:
        return None
    _, _, _, _, _, _, waypoints = min(candidates, key=lambda item: item[:6])
    return _copy_plan_with_waypoints(
        plan,
        waypoints,
        resolution="corridor_local_detour",
        obstacle_ids=tuple(sorted(item.obstacle_id for item in applicable_obstacles)),
    )


def _portal_hits_obstacle(portal: Any, obstacle: StaticObstacle) -> bool:
    # A gimmick's crossing passes through its own body by design (live: the
    # Kalm inn-cellar ladder's frame blocked its own climb).
    if getattr(portal, "traversal_class", None) in GIMMICK_LINK_CLASSES:
        return False
    return obstacle_segment_clearance_m(portal.left, portal.right, obstacle) <= 0.0


# An obstacle centred this near a gimmick's departure or landing is the
# gimmick itself (as the route adapter's collision audit judges a hit).
GIMMICK_BODY_RADIUS_M = 1.5


def _walked_hits(plan: Any, obstacle: StaticObstacle) -> tuple[int, ...]:
    """``route_obstacle_hits`` on the legs walked, not those crossing a gimmick."""
    waypoints = tuple(plan.waypoints)
    segments = [segment for segment in getattr(plan, "traversal_segments", ())
                if segment.get("link_class") in GIMMICK_LINK_CLASSES]
    if not segments:
        return route_obstacle_hits(waypoints, obstacle)
    try:
        located = [index for _, index in locate_traversal_waypoints(waypoints, segments)]
    except ValueError:
        return route_obstacle_hits(waypoints, obstacle)
    exempt = set(located)
    for index in located:
        # On the legs onto and off a gimmick, an obstacle at its end is its
        # own body (a ladder's frame stands over the floor at its top too).
        for leg, end in ((index - 1, waypoints[index]), (index + 1, waypoints[index + 1])):
            if _distance_xy_m(obstacle.center, end) <= GIMMICK_BODY_RADIUS_M:
                exempt.add(leg)
    return tuple(index for index in route_obstacle_hits(waypoints, obstacle) if index not in exempt)



def _free_circle_portal_intervals(portal: Any, obstacles: Sequence[StaticObstacle]) -> list[tuple[float, float]]:
    """Subtract inflated circle footprints from a canonical portal parameter range."""
    dx, dy = portal.right.x - portal.left.x, portal.right.y - portal.left.y
    denominator = dx * dx + dy * dy
    if denominator <= 1e-8:
        return [(0.0, 1.0)] if all(
            _distance_xy_m(portal.left, item.center) > item.effective_radius_m + 0.05
            for item in obstacles) else []
    intervals = [(0.0, 1.0)]
    for item in obstacles:
        ox, oy = portal.left.x - item.center.x, portal.left.y - item.center.y
        b = 2.0 * (ox * dx + oy * dy)
        c = ox * ox + oy * oy - ((item.effective_radius_m + 0.05) * 100.0) ** 2
        discriminant = b * b - 4.0 * denominator * c
        if discriminant <= 0.0:
            continue
        root = math.sqrt(discriminant)
        low, high = (-b - root) / (2.0 * denominator), (-b + root) / (2.0 * denominator)
        remaining = []
        for first, last in intervals:
            if high <= first or low >= last:
                remaining.append((first, last))
                continue
            if low > first:
                remaining.append((first, low))
            if high < last:
                remaining.append((high, last))
        intervals = remaining
    # Unrelated narrow portals retain their original topology. Only clipped
    # slivers below one centimetre are excluded from this candidate search.
    if intervals == [(0.0, 1.0)]:
        return intervals
    return [(a, b) for a, b in intervals if (b - a) * math.sqrt(denominator) >= 1.0]


def _fully_blocked_transitions(graph: DetourTopologyGraph, obstacles: Sequence[StaticObstacle]) -> set[tuple[int, int]]:
    """Transitions whose whole portal lies inside inflated circle footprints.

    Only a portal within an obstacle's inflated radius can lose its whole
    interval; the rest keep (0, 1) and are skipped by bounding box (the Kalm
    graph has 53,768 portals, 0.5 s per search checked one by one).
    """
    if not obstacles:
        return set()
    reach_cm = max((item.effective_radius_m + 0.05) * 100.0 for item in obstacles) + 1.0
    low_x = min(item.center.x for item in obstacles) - reach_cm
    high_x = max(item.center.x for item in obstacles) + reach_cm
    low_y = min(item.center.y for item in obstacles) - reach_cm
    high_y = max(item.center.y for item in obstacles) + reach_cm
    return {(portal.from_reference, portal.to_reference)
            for portals in graph.adjacency.values() for portal in portals
            if max(portal.left.x, portal.right.x) >= low_x and min(portal.left.x, portal.right.x) <= high_x
            and max(portal.left.y, portal.right.y) >= low_y and min(portal.left.y, portal.right.y) <= high_y
            and not _free_circle_portal_intervals(portal, obstacles)}


def _portal_contour_detour(
    graph: DetourTopologyGraph, start: Vec3, end: Vec3,
    obstacles: Sequence[StaticObstacle], *, planner_kwargs: Mapping[str, Any],
    minimum_edge_clearance_m: float,
) -> DetourRoutePlan | None:
    """Bounded local visibility search after a rectangular bypass fails.

    Whole portals are removed only when their entire usable interval is
    obstructed. The new corridor and original portal records remain immutable.
    Every added connector proves ordered containment, edge clearance, surface
    bounds and clearance from every obstacle before a plan can be returned.
    """
    if (not isinstance(graph, DetourTopologyGraph) or not obstacles or len(obstacles) > 8
            or any(item.shape != "circle" for item in obstacles)):
        return None
    blocked = _fully_blocked_transitions(graph, obstacles)
    base = graph.plan(start, end, blocked_transitions=blocked, **planner_kwargs)
    if base.status != "complete" or len(base.waypoints) < 2:
        return None
    if getattr(base, "traversal_segments", ()):
        # Connectors are proven against ordinary portal edges only.
        return None
    hits = [index for item in obstacles for index in route_obstacle_hits(base.waypoints, item)]
    if not hits:
        return _copy_plan_with_waypoints(base, base.waypoints,
            resolution="portal_contour_detour", obstacle_ids=tuple(item.obstacle_id for item in obstacles))
    first_index = min(hits)
    rejoin_index = min(len(base.waypoints) - 1, max(hits) + 2)
    local_start = base.waypoints[first_index]
    vertical_bound = float(planner_kwargs.get("max_vertical_separation_m", 20.0))
    max_waypoints = int(planner_kwargs.get("max_waypoints", 256))
    max_nodes, max_checks, local_radius_m, max_connector_m = 128, 8192, 16.0, 8.0

    def proven(a: Vec3, b: Vec3) -> bool:
        return _segment_authorized_corridor(a, b, graph=graph,
            corridor_refs=base.polygon_corridor, portals=base.portals[1:-1],
            minimum_edge_clearance_m=minimum_edge_clearance_m,
            max_vertical_separation_m=vertical_bound)

    def clear(points: Sequence[Vec3]) -> bool:
        return not any(route_obstacle_hits(points, item) for item in obstacles)

    # Rejoin inside a polygon, not on a corner that the new connector would
    # have to treat as an unproven closed-edge endpoint.
    local_end = None
    if rejoin_index == len(base.waypoints) - 1:
        candidate = base.waypoints[-1]
        if proven(candidate, candidate) and clear((candidate, candidate)):
            local_end = candidate
    else:
        for index in range(rejoin_index, min(rejoin_index + 4, len(base.waypoints) - 1)):
            a, b = base.waypoints[index:index + 2]
            for fraction in (0.1, 0.5, 0.9):
                candidate = Vec3(a.x + (b.x - a.x) * fraction,
                                 a.y + (b.y - a.y) * fraction,
                                 a.z + (b.z - a.z) * fraction)
                if proven(candidate, candidate) and clear((candidate, candidate)):
                    rejoin_index, local_end = index, candidate
                    break
            if local_end is not None:
                break
    if local_end is None or _distance_xy_m(local_start, local_end) > local_radius_m:
        return None
    nodes = [local_start, local_end]

    def add(point: Vec3) -> None:
        if len(nodes) >= max_nodes or _distance_xy_m(local_start, point) > local_radius_m:
            return
        # Only a resolved point is used, so off-mesh candidates (circle
        # points round an obstacle by a wall) skip the full polygon scan.
        resolution = graph.resolve_point_if_resolvable(point, max_snap_distance_m=0.0,
                                                        max_vertical_separation_m=vertical_bound)
        if not resolution.ok or resolution.selected_reference not in base.polygon_corridor:
            return
        point = resolution.closest_point
        if point is None or not clear((point, point)):
            return
        if not any(_distance_xy_m(point, other) < 0.01 for other in nodes):
            nodes.append(point)

    for point in base.waypoints[first_index + 1:rejoin_index]:
        add(point)
    for portal in base.portals[1:-1]:
        if min(_distance_xy_m(portal.left, local_start),
               _distance_xy_m(portal.right, local_start)) > local_radius_m:
            continue
        for a, b in _free_circle_portal_intervals(portal, obstacles):
            fraction = (a + b) / 2.0
            add(Vec3(portal.left.x + (portal.right.x - portal.left.x) * fraction,
                     portal.left.y + (portal.right.y - portal.left.y) * fraction,
                     portal.left.z + (portal.right.z - portal.left.z) * fraction))
    for item in obstacles:
        bearing = math.atan2(local_start.y - item.center.y, local_start.x - item.center.x)
        for extra_m in (0.025, 0.25):
            # Circumscribed chords remain outside the inflated footprint;
            # every actual connector is still checked independently below.
            radius_cm = (item.effective_radius_m + extra_m) * 100.0 / math.cos(math.pi / 16.0)
            for index in range(16):
                angle = bearing + 2.0 * math.pi * index / 16.0
                add(Vec3(item.center.x + math.cos(angle) * radius_cm,
                         item.center.y + math.sin(angle) * radius_cm, item.center.z))
    costs, previous, queue = {0: 0.0}, {}, [(0.0, 0)]
    checks = 0
    reached = False
    while queue:
        cost, index = heapq.heappop(queue)
        if cost > costs[index]:
            continue
        if index == 1:
            reached = True
            break
        for target, point in enumerate(nodes):
            step = _distance_xy_m(nodes[index], point)
            if index == target or step > max_connector_m or cost + step >= costs.get(target, math.inf):
                continue
            if not clear((nodes[index], point)):
                continue
            if checks >= max_checks:
                return None
            checks += 1
            if not proven(nodes[index], point):
                continue
            costs[target], previous[target] = cost + step, index
            heapq.heappush(queue, (cost + step, target))
    if not reached:
        return None
    indices = [1]
    while indices[-1] != 0:
        indices.append(previous[indices[-1]])
    path = tuple(nodes[index] for index in reversed(indices))
    points = base.waypoints[:first_index] + path + base.waypoints[rejoin_index + 1:]
    if len(points) > max_waypoints or not clear(points):
        return None
    result = _copy_plan_with_waypoints(base, points, resolution="portal_contour_detour",
        obstacle_ids=tuple(sorted(item.obstacle_id for item in obstacles)))
    return replace(result, diagnostics={**result.diagnostics, "contour_search": {
        "nodes": len(nodes), "connector_checks": checks, "max_nodes": max_nodes,
        "max_connector_checks": max_checks, "local_radius_m": local_radius_m,
        "max_connector_m": max_connector_m, "blocked_transitions": sorted(blocked),
        "first_replaced_waypoint": first_index, "connector_count": len(path) - 1}})


START_CLEARANCE_MARGIN_M = 0.10


def _clear_of_start(start: Vec3, obstacle: StaticObstacle) -> StaticObstacle:
    """Shrink, for this plan only, an obstacle whose padding covers the start.

    Cloud is standing there, so the padded footprint over-covers; left as
    is, no route can leave (live at Kalm, starting beside Nene's table set
    every re-plan failed as global_replan_unavailable). The obstacle keeps
    its axis and shrinks to just short of Cloud, unpadded.
    """
    clearance = obstacle_segment_clearance_m(start, start, obstacle)
    if clearance > 0.0:
        return obstacle
    axis_distance = clearance + obstacle.effective_radius_m
    return replace(
        obstacle,
        radius_m=max(0.05, axis_distance - START_CLEARANCE_MARGIN_M),
        cloud_clearance_m=0.0,
        uncertainty_margin_m=0.0,
    )


class RecentBlockers:
    """Blockers no collision query sees, remembered across route attempts.

    Each route attempt starts from a fresh overlay; without this, a pocket
    formed by an invisible blocker (live: the Golden Plumes wagon at Kalm,
    hemmed in by a crowd) was rediscovered stall by stall and never left.
    Kept briefly and in memory only: the world changes and restarts forget.
    """

    def __init__(self, ttl_seconds: float = 600.0, max_items: int = 16, clock: Any = None) -> None:
        import time as _time
        self.ttl_seconds = float(ttl_seconds)
        self.max_items = int(max_items)
        self.clock = clock or _time.monotonic
        self._items: list[tuple[float, StaticObstacle]] = []

    def remember(self, obstacle: StaticObstacle) -> None:
        now = self.clock()
        self._items = [(t, o) for t, o in self._items if o.obstacle_id != obstacle.obstacle_id]
        self._items.append((now, obstacle))
        self._items = self._items[-self.max_items:]

    def active(self) -> list[StaticObstacle]:
        now = self.clock()
        self._items = [(t, o) for t, o in self._items if now - t <= self.ttl_seconds]
        return [o for _, o in self._items]


def plan_with_static_obstacles(
    graph: DetourTopologyGraph,
    start: Vec3,
    end: Vec3,
    overlay: StaticObstacleOverlay,
    *,
    now_t: float | None = None,
    max_expansions: int = 100_000,
    max_snap_distance_m: float = 8.0,
    max_vertical_separation_m: float = 20.0,
    max_waypoints: int = 256,
    max_tile_crossings: int | None = None,
    minimum_edge_clearance_m: float = DEFAULT_LOCAL_EDGE_CLEARANCE_M,
    start_resolution_override: Any | None = None,
    end_resolution_override: Any | None = None,
    portal_corner_inset_m: float = 0.0,
    routing_objective: str = "distance",
    traversal_policy: Any | None = None,
) -> DetourRoutePlan:
    """Plan with an overlay while preserving the immutable topology graph."""

    snapshot = overlay.planning_snapshot(now_t=now_t)
    # Only a caller that chooses a policy passes one; planners and test
    # doubles that know nothing of off-mesh links see the old call.
    policy_kwargs = {"traversal_policy": traversal_policy} if traversal_policy is not None else {}
    base = graph.plan(
        start,
        end,
        max_expansions=max_expansions,
        max_snap_distance_m=max_snap_distance_m,
        max_vertical_separation_m=max_vertical_separation_m,
        max_waypoints=max_waypoints,
        portal_corner_inset_m=portal_corner_inset_m,
        routing_objective=routing_objective,
        max_tile_crossings=max_tile_crossings,
        start_resolution_override=start_resolution_override,
        end_resolution_override=end_resolution_override,
        **policy_kwargs,
    )
    # Test doubles and a few legacy providers expose a serialized plan rather
    # than a DetourRoutePlan object.  Preserve that provider seam; the live
    # adapter will validate the mapping independently before authorization.
    if isinstance(base, Mapping):
        return base  # type: ignore[return-value]
    if base.status != "complete" or not snapshot.obstacles:
        return base
    obstacles = tuple(_clear_of_start(start, obstacle) for obstacle in snapshot.obstacles)
    def grid_or(failed: DetourRoutePlan) -> DetourRoutePlan:
        # The strategies below keep the route's shape but cannot go round
        # obstacles inside large polygons or between several of them (live:
        # a crowd round the Golden Plumes wagon at Kalm). A grid search over
        # the navmesh floor can; it is the last resort before failing closed.
        from .grid_detour import grid_detour
        grid_plan = grid_detour(
            graph, base, obstacles,
            clearance_m=lambda point, obstacle: obstacle_segment_clearance_m(point, point, obstacle),
            route_hits=route_obstacle_hits,
            plan_kwargs={
                "max_expansions": max_expansions,
                "max_snap_distance_m": max_snap_distance_m,
                "max_vertical_separation_m": max_vertical_separation_m,
                "max_waypoints": max_waypoints,
                "portal_corner_inset_m": portal_corner_inset_m,
                "routing_objective": routing_objective,
                **policy_kwargs,
            },
        )
        if grid_plan is None:
            return failed
        ids = [obstacle.obstacle_id for obstacle in obstacles]
        return replace(
            grid_plan,
            validation={**dict(grid_plan.validation), "static_obstacle_overlay_validated": True,
                        "static_obstacle_ids": ids},
            diagnostics={**dict(grid_plan.diagnostics), "static_obstacle_resolution": "grid_detour",
                         "static_obstacle_ids": ids, "legacy_failure": failed.reason_code},
        )

    current = base
    blocked_transitions: set[tuple[int, int]] = set()
    # Inspect the base route and both replacement candidates. The final
    # candidate still needs an obstacle check when the replan budget is spent.
    for replans_used in range(3):
        hits = tuple(
            obstacle for obstacle in obstacles
            if _walked_hits(current, obstacle)
        )
        if not hits:
            if blocked_transitions:
                return replace(
                    current,
                    diagnostics={
                        **dict(current.diagnostics),
                        "static_obstacle_resolution": "global_replan",
                        "blocked_transitions": sorted(blocked_transitions),
                    },
                )
            return current
        if replans_used == 2:
            break
        if len(hits) == 1:
            portal_hit = any(
                _portal_hits_obstacle(portal, hits[0])
                for portal in current.portals[1:-1]
            )
            if not portal_hit:
                local = _local_detour(
                    graph,
                    current,
                    hits[0],
                    obstacles=obstacles,
                    max_waypoints=max_waypoints,
                    minimum_edge_clearance_m=minimum_edge_clearance_m,
                    max_vertical_separation_m=max_vertical_separation_m,
                )
                if local is not None:
                    return local
                # The bounded local-offset proof is preferred because it
                # preserves the existing route shape. If that proof cannot
                # establish a safe bypass, try the same-polygon visibility
                # graph before escalating or failing closed.
                if len(current.polygon_corridor) == 1:
                    visibility = _same_polygon_visibility_detour(
                        graph,
                        current,
                        obstacles,
                        max_waypoints=max_waypoints,
                        minimum_edge_clearance_m=minimum_edge_clearance_m,
                        max_vertical_separation_m=max_vertical_separation_m,
                    )
                    if visibility is not None:
                        return visibility
        for obstacle in hits:
            for portal in current.portals[1:-1]:
                if _portal_hits_obstacle(portal, obstacle):
                    blocked_transitions.add((int(portal.from_reference), int(portal.to_reference)))
        if not blocked_transitions:
            contour = _portal_contour_detour(graph, start, end, obstacles,
                minimum_edge_clearance_m=minimum_edge_clearance_m,
                planner_kwargs={"max_expansions": max_expansions, "max_waypoints": max_waypoints,
                    "max_snap_distance_m": max_snap_distance_m,
                    "max_vertical_separation_m": max_vertical_separation_m,
                    "portal_corner_inset_m": portal_corner_inset_m, "routing_objective": routing_objective, "max_tile_crossings": max_tile_crossings,
                    "start_resolution_override": start_resolution_override,
                    "end_resolution_override": end_resolution_override, **policy_kwargs})
            if contour is not None:
                return contour
            return grid_or(replace(
                current,
                status="failed",
                reason_code="static_obstacle_local_detour_unproven",
                waypoints=tuple(),
                total_estimated_path_length_m=None,
                validation={**dict(current.validation), "static_obstacle_overlay_validated": False},
                diagnostics={**dict(current.diagnostics), "static_obstacle_resolution": "fail_closed_no_portal_escalation"},
            ))
        current = graph.plan(
            start,
            end,
            max_expansions=max_expansions,
            max_snap_distance_m=max_snap_distance_m,
            max_vertical_separation_m=max_vertical_separation_m,
            max_waypoints=max_waypoints,
            portal_corner_inset_m=portal_corner_inset_m,
            routing_objective=routing_objective,
            max_tile_crossings=max_tile_crossings,
            start_resolution_override=start_resolution_override,
            end_resolution_override=end_resolution_override,
            blocked_transitions=blocked_transitions,
            **policy_kwargs,
        )
        if current.status != "complete":
            contour = _portal_contour_detour(graph, start, end, obstacles,
                minimum_edge_clearance_m=minimum_edge_clearance_m,
                planner_kwargs={"max_expansions": max_expansions, "max_waypoints": max_waypoints,
                    "max_snap_distance_m": max_snap_distance_m,
                    "max_vertical_separation_m": max_vertical_separation_m,
                    "portal_corner_inset_m": portal_corner_inset_m, "routing_objective": routing_objective, "max_tile_crossings": max_tile_crossings,
                    "start_resolution_override": start_resolution_override,
                    "end_resolution_override": end_resolution_override, **policy_kwargs})
            if contour is not None:
                return contour
            return grid_or(replace(
                current,
                reason_code="static_obstacle_global_replan_unavailable",
                diagnostics={**dict(current.diagnostics), "static_obstacle_resolution": "global_replan_failed", "blocked_transitions": sorted(blocked_transitions)},
            ))
    return grid_or(replace(
        current,
        status="failed",
        reason_code="static_obstacle_global_replan_still_blocked",
        waypoints=tuple(),
        total_estimated_path_length_m=None,
        validation={**dict(current.validation), "static_obstacle_overlay_validated": False},
        diagnostics={**dict(current.diagnostics), "static_obstacle_resolution": "global_replan_still_blocked", "blocked_transitions": sorted(blocked_transitions)},
    ))


@dataclass
class LookaheadBudget:
    """Separate bounded inspection attempts from usable exact probes."""

    maximum_attempts: int = 4
    used_attempts: int = 0
    maximum_usable_queries: int = 1
    used_usable_queries: int = 0

    @property
    def maximum_queries(self) -> int:
        """Compatibility name for the usable-query budget."""

        return int(self.maximum_usable_queries)

    @property
    def used_queries(self) -> int:
        """Compatibility name for consumed usable exact-sequence probes."""

        return int(self.used_usable_queries)

    def consume_attempt(self) -> bool:
        if self.used_attempts >= int(self.maximum_attempts):
            return False
        self.used_attempts += 1
        return True

    def consume_usable(self) -> bool:
        if self.used_usable_queries >= int(self.maximum_usable_queries):
            return False
        self.used_usable_queries += 1
        return True

    @property
    def attempts_exhausted(self) -> bool:
        return self.used_attempts >= int(self.maximum_attempts)

    @property
    def usable_exhausted(self) -> bool:
        return self.used_usable_queries >= int(self.maximum_usable_queries)

    @property
    def exhausted(self) -> bool:
        return self.attempts_exhausted or self.usable_exhausted


@dataclass(frozen=True)
class LookaheadDecision:
    state: ObstacleEvidenceState
    reason_code: str
    clear_distance_m: float | None = None
    source: str | None = None
    sequence: int | None = None
    query_verified: bool = False
    selected_direction: str | None = None
    probe_relative_yaw_degrees: float | None = None
    probe_world_bearing_degrees: float | None = None
    capsule_radius_m: float | None = None


class NavigationSidecarLookahead:
    """Consume one exact-sequence navigation sidecar sample at most once."""

    def __init__(self, *, maximum_queries_per_plan: int = 1, maximum_attempts_per_plan: int | None = None) -> None:
        if int(maximum_queries_per_plan) <= 0:
            raise ValueError("maximum_queries_per_plan must be positive")
        if maximum_attempts_per_plan is not None and int(maximum_attempts_per_plan) <= 0:
            raise ValueError("maximum_attempts_per_plan must be positive")
        self.maximum_queries_per_plan = int(maximum_queries_per_plan)
        self.maximum_attempts_per_plan = int(
            maximum_attempts_per_plan
            if maximum_attempts_per_plan is not None
            else max(4, self.maximum_queries_per_plan + 2)
        )
        if self.maximum_attempts_per_plan < self.maximum_queries_per_plan:
            raise ValueError("maximum_attempts_per_plan must cover maximum_queries_per_plan")
        self._budgets: dict[str, LookaheadBudget] = {}
        self._rolling_window_starts: dict[str, float] = {}
        self._rolling_plans: set[str] = set()
        self._last_sidecar_sequences: dict[str, int] = {}

    def reset_plan(self, route_plan_sha256: str) -> None:
        key = str(route_plan_sha256)
        self._budgets[key] = LookaheadBudget(
            maximum_attempts=self.maximum_attempts_per_plan,
            maximum_usable_queries=self.maximum_queries_per_plan,
        )
        self._rolling_window_starts.pop(key, None)
        self._rolling_plans.discard(key)
        self._last_sidecar_sequences.pop(key, None)

    def renew_budget_window(
        self,
        route_plan_sha256: str,
        *,
        now_t: float,
        interval_seconds: float,
    ) -> bool:
        """Renew a live route's budget after a bounded interval.

        Direct users retain the established per-plan budget unless they call
        this method. The highest inspected producer sequence survives window
        renewals so a fresh base sample cannot make a replayed sidecar usable.
        """
        now = float(now_t)
        interval = float(interval_seconds)
        if not math.isfinite(now) or not math.isfinite(interval) or interval <= 0.0:
            raise ValueError("rolling lookahead window values must be finite and positive")
        key = str(route_plan_sha256)
        self._rolling_plans.add(key)
        started = self._rolling_window_starts.get(key)
        if started is None:
            self._rolling_window_starts[key] = now
            return False
        if now < started or now - started < interval:
            return False
        self._budgets[key] = LookaheadBudget(
            maximum_attempts=self.maximum_attempts_per_plan,
            maximum_usable_queries=self.maximum_queries_per_plan,
        )
        self._rolling_window_starts[key] = now
        return True

    def budget_status(self, route_plan_sha256: str) -> dict[str, int | bool]:
        budget = self._budgets.get(str(route_plan_sha256))
        if budget is None:
            return {
                "used_attempts": 0,
                "maximum_attempts": self.maximum_attempts_per_plan,
                "used_usable_queries": 0,
                "maximum_usable_queries": self.maximum_queries_per_plan,
                "used_queries": 0,
                "maximum_queries": self.maximum_queries_per_plan,
                "attempts_exhausted": False,
                "usable_exhausted": False,
                "exhausted": False,
            }
        return {
            "used_attempts": int(budget.used_attempts),
            "maximum_attempts": int(budget.maximum_attempts),
            "used_usable_queries": int(budget.used_usable_queries),
            "maximum_usable_queries": int(budget.maximum_usable_queries),
            "used_queries": int(budget.used_queries),
            "maximum_queries": int(budget.maximum_queries),
            "attempts_exhausted": budget.attempts_exhausted,
            "usable_exhausted": budget.usable_exhausted,
            "exhausted": budget.exhausted,
        }

    def inspect(
        self,
        raw: Mapping[str, Any] | None,
        *,
        sequence: int,
        route_plan_sha256: str,
        desired_world_bearing_degrees: float,
        camera_world_yaw_degrees: float | None,
        horizon_m: float,
        force: bool = False,
        expected_source: str = STATIC_OBSTACLE_QUERY_SOURCE,
    ) -> LookaheadDecision:
        budget = self._budgets.setdefault(
            str(route_plan_sha256),
            LookaheadBudget(
                maximum_attempts=self.maximum_attempts_per_plan,
                maximum_usable_queries=self.maximum_queries_per_plan,
            ),
        )
        if budget.usable_exhausted:
            return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_budget_exhausted", sequence=sequence)
        if not budget.consume_attempt():
            return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_budget_exhausted", sequence=sequence)
        if not isinstance(raw, Mapping):
            return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_navigation_payload_missing", sequence=sequence)
        navigation = raw.get("navigation") if isinstance(raw.get("navigation"), Mapping) else raw
        if not isinstance(navigation, Mapping):
            return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_navigation_payload_missing", sequence=sequence)
        key = str(route_plan_sha256)
        sidecar_sequence: int | None = None
        if key in self._rolling_plans:
            sidecar_sequence = navigation.get("base_sequence")
            try:
                sidecar_sequence = int(sidecar_sequence)
            except (TypeError, ValueError, OverflowError):
                return LookaheadDecision(
                    ObstacleEvidenceState.UNKNOWN,
                    "lookahead_sidecar_sequence_invalid",
                    sequence=sequence,
                )
        try:
            if int(navigation.get("schema_version", -1)) != 1:
                return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_schema_invalid", sequence=sequence)
        except (TypeError, ValueError):
            return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_schema_invalid", sequence=sequence)
        source = str(navigation.get("source") or "")
        if source != str(expected_source):
            return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_source_untrusted", source=source, sequence=sequence)
        try:
            if not navigation_sequence_is_bound(navigation, observed_sequence=sequence):
                return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_sequence_mismatch", sequence=sequence)
        except (TypeError, ValueError):
            return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_sequence_invalid", sequence=sequence)
        if key in self._rolling_plans:
            previous_sequence = self._last_sidecar_sequences.get(key)
            if previous_sequence is not None and sidecar_sequence <= previous_sequence:
                return LookaheadDecision(
                    ObstacleEvidenceState.UNKNOWN,
                    "lookahead_sidecar_sequence_reused",
                    source=source,
                    sequence=sequence,
                )
            # Advance the replay guard only after source identity and the
            # consumer's observed-to-sidecar binding have passed.
            self._last_sidecar_sequences[key] = sidecar_sequence
        directions = navigation.get("directions")
        if not isinstance(directions, Mapping):
            return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_directions_missing", sequence=sequence)
        names = ("forward", "right", "backward", "left")
        if camera_world_yaw_degrees is None or not math.isfinite(float(camera_world_yaw_degrees)):
            return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_camera_yaw_missing", sequence=sequence)
        offsets = {"forward": 0.0, "right": 90.0, "backward": 180.0, "left": -90.0}
        selected = min(
            names,
            key=lambda name: abs(
                (float(desired_world_bearing_degrees)
                 - (float(camera_world_yaw_degrees) + offsets[name])
                 + 180.0) % 360.0 - 180.0
            ),
        )
        probe_relative_yaw = float(offsets[selected])
        probe_world_bearing = (float(camera_world_yaw_degrees) + probe_relative_yaw) % 360.0
        evidence = directions.get(selected)
        if not isinstance(evidence, Mapping):
                return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_direction_missing", source=source, sequence=sequence)
        try:
            if not navigation_sequence_is_bound(
                navigation,
                observed_sequence=sequence,
                sample_sequence=evidence.get("sample_sequence"),
            ):
                return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_direction_sequence_mismatch", source=source, sequence=sequence)
        except (TypeError, ValueError):
                return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_direction_sequence_invalid", source=source, sequence=sequence)
        try:
            clear_distance = float(evidence.get("clear_distance_m"))
        except (TypeError, ValueError):
            clear_distance = None
        if clear_distance is None or not math.isfinite(clear_distance):
            return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_clearance_unknown", source=source, sequence=sequence)
        capsule = evidence.get("capsule_probe")
        if isinstance(capsule, Mapping) and capsule.get("available") is True and capsule.get("fresh") is True:
            try:
                capsule_sequence = int(capsule.get("sample_sequence", -1))
                capsule_distance = float(capsule.get("clear_distance_m"))
            except (TypeError, ValueError):
                return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_capsule_invalid", source=source, sequence=sequence)
            if (
                not navigation_sequence_is_bound(
                    navigation,
                    observed_sequence=sequence,
                    sample_sequence=capsule_sequence,
                )
                or not math.isfinite(capsule_distance)
            ):
                return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_capsule_sequence_mismatch", source=source, sequence=sequence)
            if not budget.consume_usable():
                return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_budget_exhausted", source=source, sequence=sequence)
            radius_values: list[float] = []
            for raw_radius in (capsule.get("radius_m"), navigation.get("capsule_radius_m")):
                if raw_radius is None:
                    continue
                try:
                    radius = float(raw_radius)
                except (TypeError, ValueError, OverflowError):
                    return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_capsule_geometry_invalid", source=source, sequence=sequence)
                if not math.isfinite(radius) or radius <= 0.0:
                    return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_capsule_geometry_invalid", source=source, sequence=sequence)
                radius_values.append(radius)
            capsule_radius = radius_values[0] if radius_values else None
            if len(radius_values) == 2 and abs(radius_values[0] - radius_values[1]) > 1e-6:
                return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_capsule_geometry_invalid", source=source, sequence=sequence)
            blocking_hit = bool(capsule.get("blocking_hit", capsule.get("hit", False)))
            if blocking_hit and capsule_radius is None:
                return LookaheadDecision(ObstacleEvidenceState.UNKNOWN, "lookahead_capsule_geometry_invalid", source=source, sequence=sequence)
            if blocking_hit and capsule_distance <= float(horizon_m):
                return LookaheadDecision(
                    ObstacleEvidenceState.READY, "lookahead_capsule_blocked", capsule_distance, source, sequence, True,
                    selected, probe_relative_yaw, probe_world_bearing, capsule_radius,
                )
            if not blocking_hit:
                return LookaheadDecision(
                    ObstacleEvidenceState.READY, "lookahead_capsule_clear", min(clear_distance, capsule_distance), source, sequence, True,
                    selected, probe_relative_yaw, probe_world_bearing, capsule_radius,
                )
        if bool(evidence.get("hit", False)) or bool(evidence.get("blocking_hit", False)):
            if clear_distance <= float(horizon_m):
                return LookaheadDecision(
                    ObstacleEvidenceState.CANDIDATE, "lookahead_line_blocked_not_capsule_proof", clear_distance, source, sequence, False,
                    selected, probe_relative_yaw, probe_world_bearing,
                )
            return LookaheadDecision(
                ObstacleEvidenceState.UNKNOWN, "lookahead_line_hit_beyond_horizon", clear_distance, str(navigation.get("source") or ""), sequence,
                False, selected, probe_relative_yaw, probe_world_bearing,
            )
        if force:
            return LookaheadDecision(
                ObstacleEvidenceState.UNKNOWN, "lookahead_line_only_not_walkability_proof", clear_distance, str(navigation.get("source") or ""), sequence,
                False, selected, probe_relative_yaw, probe_world_bearing,
            )
        return LookaheadDecision(
            ObstacleEvidenceState.UNKNOWN, "lookahead_clear_not_capsule_proven", clear_distance, str(navigation.get("source") or ""), sequence,
            False, selected, probe_relative_yaw, probe_world_bearing,
        )
