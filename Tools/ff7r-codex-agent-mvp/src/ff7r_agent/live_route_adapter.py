"""Fail-closed live adapter for an approved ordinary-ground RoutePlan.

The adapter owns live observation, authorization, safety and actuation wiring.
It deliberately does not implement route generation, independent validation,
or follower state transitions.  Those responsibilities stay with the
existing Detour planner, validator and standalone RouteFollower.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
import copy
import hashlib
import importlib.util
import json
import math
import threading
import os
from pathlib import Path
import sys
import time
import uuid
from typing import Any, Callable, Mapping, Protocol, Sequence

from .detour_route import (
    DT_POLYTYPE_GROUND,
    DetourRoutePlan,
    DetourTopologyGraph,
    load_shipping_detour_topology,
)
from .continuous_control import movement_lease_seconds, FAST_FOLLOW_MAX_HEADING_ERROR_DEG, SPRINT_SPEED_BOUND_M_S, SPRINT_BRAKING_MARGIN_M, SPRINT_SETTLE_TIMEOUT_SECONDS
from .camera_follow import CameraFollowConfig, CameraFollowController, shortest_yaw_error_deg
from .camera_settling import (
    CameraYawSettling, CAMERA_DELTA_PER_DEGREE, CAMERA_SETTLE_DURATION_SECONDS,
    CAMERA_SETTLE_TIMEOUT_SECONDS,
)
from .models import Vec3
from .gimmick_links import attach_gimmick_links
from .offmesh_links import (
    DISABLED_TRAVERSAL_POLICY,
    GIMMICK_LINK_CLASSES,
    PORTAL_KIND_TRAVERSAL,
    TraversalPolicy,
    locate_traversal_waypoints,
    report_path_for,
    segment_points,
    unverified_classes,
    write_traversal_report,
)
from .safety import game_process_snapshot
from .dynamic_gates import (
    GATE_SPRINT_CUTOFF_M,
    GATE_WAIT_DISTANCE_M,
    SEALED_REPLAN_REASONS,
    GateRegistry,
    obstacle_id_for,
    plan_gates,
    plan_through_gates,
    wait_for_gate,
)
from .dynamic_obstacles import (
    DynamicObstacleSnapshot,
    assess_static_learning_dynamic_evidence,
    snapshot_from_game_state,
)
from .static_obstacles import (
    NavigationSidecarLookahead,
    ObstacleEvidenceState,
    StaticObstacle,
    StaticObstacleOverlay,
    StaticObstacleDurableIdentity,
    StaticObstaclePlanningError,
    StaticObstacleScope,
    StaticObstacleScopeError,
    plan_with_static_obstacles,
    obstacle_segment_clearance_m,
    _segment_authorized_corridor,
)
from .ordinary_multi_polygon import (
    OrdinaryMultiPolygonRouteError,
    select_ordinary_multi_polygon_destination,
    validate_ordinary_multi_polygon_route,
)
from .ordinary_multi_tile import (
    DEFAULT_MULTI_TILE_ABSOLUTE_MAX_CYCLES,
    DEFAULT_MULTI_TILE_MAX_CROSSINGS,
    DEFAULT_MULTI_TILE_MAX_DISTANCE_M,
    DEFAULT_MULTI_TILE_MIN_DISTANCE_M,
    OrdinaryMultiTileRouteError,
    derive_multi_tile_cycle_budget,
    select_ordinary_multi_tile_destination,
    validate_ordinary_multi_tile_route,
)
from .traversal_crossing import (
    CROSSING_TELEMETRY_STALL_SECONDS,
    LEDGE_LINK_CLASSES,
    CrossingTarget,
    TraversalCrossing,
)
from .topology_cache import (
    PARTIAL_CORRIDOR_SCOPE_SCHEMA,
    TILE_EXCLUDED_PARTIAL_SCHEMA,
    TOPOLOGY_PARSER_SCHEMA_IDENTITY,
    STATIC_TOPOLOGY_FINGERPRINT_SCHEMA,
    TOPOLOGY_CACHE_SCHEMA_VERSION,
    TopologyCacheError,
    corridor_portal_identity,
    cached_static_fingerprint,
    native_probe_identities_match,
    resolve_manifest_artifact,
    select_library_manifest,
    sha256_file,
    static_topology_fingerprint,
    tile_excluded_partial,
    topology_proof_locality_miss,
    topology_parser_schema_compatible,
    validator_identity,
    verify_validated_corridor_scope,
)


LIVE_EXECUTABLE_SHA256 = (
    "37D952FABBFA2B710F8AE6490997C9755E4E57B4FD545078C9BAAF46C9B233D7"
)
DEFAULT_TOPOLOGY_PATH = Path(
    r"C:\CaitGPT\Telemetry\navigation-native-geometry.json"
)
DEFAULT_TOPOLOGY_PROBE_PATH = Path(
    r"C:\CaitGPT\Telemetry\navigation-native-probe.json"
)
DEFAULT_TOPOLOGY_ENABLE_PATH = Path(
    r"C:\CaitGPT\Telemetry\navigation-native-geometry.enable"
)
DEFAULT_TOPOLOGY_READY_PATH = Path(
    r"C:\CaitGPT\Telemetry\navigation-native-geometry.ready"
)
DEFAULT_TOPOLOGY_TIMING_PATH = Path(
    r"C:\CaitGPT\Telemetry\navigation-native-probe-timing.json"
)
DEFAULT_TOPOLOGY_STATUS_PATH = Path(
    r"C:\CaitGPT\Telemetry\navigation-native-geometry-status.json"
)
DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH = Path(
    r"C:\CaitGPT\Telemetry\TopologyCache\manifest.json"
)
DEFAULT_TOPOLOGY_PROBE_DLL_PATH = Path(
    r"D:\Games\FINAL FANTASY VII REBIRTH\End\Binaries\Win64\ue4ss\Mods\FF7RNavigationNativeProbe\dlls\main.dll"
)
# The independent validator whose identity every admitted capture records.
DEFAULT_VALIDATOR_SOURCE_PATH = (
    Path(__file__).resolve().parents[4] / "Tools" / "ff7r-navigation-validation" / "navigation_validation.py"
)


def current_validator_source_sha256(path: str | Path | None = None) -> str | None:
    """The validator identity a route will demand of its capture, or None when unreadable."""
    source = Path(path) if path is not None else DEFAULT_VALIDATOR_SOURCE_PATH
    try:
        return validator_identity(source).upper() if source.is_file() else None
    except TopologyCacheError:
        return None


# Planning graphs kept built ahead (prewarm_graph): the capture Cloud is
# heading into and the one before. A route waits this long for a build in
# progress before building its own; live, cold builds took 9-11 s at
# Cameron's, Isabelle's and Michaela's (#196).
MAX_PREWARMED_GRAPHS = 2
GRAPH_PREWARM_WAIT_SECONDS = 60.0
DEFAULT_TELEMETRY_PATH = Path(r"C:\CaitGPT\Telemetry\state.json")
DEFAULT_MAX_TELEMETRY_AGE_SECONDS = 0.5
# The telemetry a render capture was taken against may be older than the
# live observation's: on a loaded machine the window capture lands about a
# second after the sample (live 2026-09-30 at Gongaga, 1.09 s, and a dim
# scene the brightness heuristic called the main menu refused every route).
# The observation itself is still held to the live bound.
RENDER_CAPTURE_MAX_TELEMETRY_AGE_SECONDS = 2.0
DEFAULT_PREFLIGHT_TELEMETRY_WAIT_SECONDS = 3.0
_TELEMETRY_BACKED_GAMEPLAY_OVERRIDE = "fresh_loaded_gameplay"
# Cloud's capsule half-height: an actor-origin target is this far above his feet.
ROUTE_TARGET_ACTOR_ORIGIN_ABOVE_FEET_CM = 88.0
_TELEMETRY_BACKED_GAMEPLAY_REASON = "telemetry_backed_gameplay_confirmed"
DEFAULT_TELEMETRY_POLL_SECONDS = 0.05
# The native probe is one-shot and read-only. Recent live evidence measured
# 92.6 s for BuildReport plus 78.8 s for extraction/serialization on the
# target laptop, so retain a bounded 240 s wait rather than timing out before
# the single native request can publish its artifact.
DEFAULT_MAX_CAPTURE_WAIT_SECONDS = 240.0
DEFAULT_CAPTURE_POLL_SECONDS = 0.25
DEFAULT_CONTROL_PERIOD_SECONDS = 0.1
DEFAULT_NORMAL_STICK_MAGNITUDE = 0.35
DEFAULT_SLOW_STICK_MAGNITUDE = 0.20
# The 0.20/300 ms slow pulse was below the measured effective locomotion
# floor in the preserved live route.  Keep it as a legacy/configuration field
# for compatibility, but never use it for an authorized near-goal command.
DEFAULT_MINIMUM_EFFECTIVE_STICK_MAGNITUDE = 0.50
DEFAULT_APPROACH_STICK_MAGNITUDE = 0.50
DEFAULT_FINAL_STICK_MAGNITUDE = 0.50
DEFAULT_APPROACH_CONTROL_PERIOD_SECONDS = 0.25
# The game does not reliably register a shorter final hold as locomotion.
# Keep final pulses at the same 250 ms floor as approach pulses so a
# no-progress sample represents a sustained movement attempt before any
# obstacle inference is considered.
DEFAULT_FINAL_CONTROL_PERIOD_SECONDS = 0.25
DEFAULT_FINAL_CONTROL_DISTANCE_M = 1.0
CAMERA_FOLLOW_MOUSE_SCALE_PIXELS = 90.0 * CAMERA_DELTA_PER_DEGREE * 0.75
NON_SPRINT_SPEED_BOUND_M_S = 8.0
# Below this measured horizontal speed the momentum ray of a fast-follow hold
# has no meaningful direction; the commanded-heading proof still applies.
FAST_FOLLOW_MOMENTUM_MIN_SPEED_CM_S = 50.0
MIN_COMMAND_DURATION_MS = 100
# A sub-250 ms hold is not sufficient evidence that the game accepted a
# movement command. Such samples remain diagnostic and cannot promote a static
# blocker or trigger a route replan.
MIN_STATIC_OBSTACLE_COMMAND_DURATION_MS = 250
MAX_COMMAND_DURATION_MS = 300
DEFAULT_READINESS_RECOVERY_TIMEOUT_SECONDS = 3.0
DEFAULT_READINESS_REACQUIRE_MAX_CROSS_TRACK_M = 0.75
DEFAULT_COLLISION_DIVERGENCE_MIN_DISPLACEMENT_M = 0.25
DEFAULT_COLLISION_DIVERGENCE_MIN_LATERAL_M = 0.25
DEFAULT_COLLISION_DIVERGENCE_MIN_BEARING_ERROR_DEGREES = 45.0

# Live telemetry samples are discrete snapshots taken after a movement hold.
# At narrow navmesh portals, a legitimate adjacent-polygon landing can be a
# few centimetres beside the serialized portal endpoint even though the
# landing point is inside the authorized destination polygon. Keep this
# tolerance bounded and use it only for the adjacent transition proof below.
DEFAULT_PORTAL_LANDING_TOLERANCE_CM = 20.0
DEFAULT_NARROW_PORTAL_MAX_SPAN_CM = 200.0
DEFAULT_POST_DIVERGENCE_CONFIRMATION_SAMPLES = 2
DEFAULT_POST_DIVERGENCE_CONFIRMATION_TIMEOUT_SECONDS = 1.0
# A sprint->pulse handoff may resume before a full stationary settle only after
# a fresh post-neutral sample proves very low residual speed and the complete
# conservative drift envelope remains inside the already-authorized corridor.
# The 0.6 m/s cap bounds 0.5 s of residual travel to 0.30 m; the additional
# 0.10 m margin below keeps the geometric proof conservative.
FAST_FOLLOW_HOLD_AGE_BUDGET_SECONDS = 0.75
# Pursuit following (LiveAdapterConfig.pursuit): one renewed hold per sample,
# released by the broker if renewals stop; sparse progress supervision.
PURSUIT_HOLD_SECONDS = 0.45
PURSUIT_TELEMETRY_STALL_SECONDS = 0.6
PURSUIT_MAX_CROSS_TRACK_M = 3.0
PURSUIT_PROGRESS_WINDOW_SECONDS = 1.5
PURSUIT_MIN_PROGRESS_M = 0.5
PURSUIT_WARMUP_COMMANDS = 10
PURSUIT_MIN_LOOKAHEAD_M = 2.0
PURSUIT_MAX_LOOKAHEAD_M = 5.0
PURSUIT_SPRINT_MIN_REMAINING_M = 10.0
PURSUIT_SPRINT_MIN_STRAIGHT_M = 8.0
PURSUIT_SLOW_DISTANCE_M = 3.0
PURSUIT_FINAL_MAGNITUDE = 0.5
PURSUIT_CAMERA_PERIOD_SECONDS = 0.3
PURSUIT_CONTROL_GRACE_SECONDS = 0.8
PURSUIT_SEARCH_SEGMENTS = 8
# Off-mesh traversal links (offmesh_links.py, traversal_crossing.py): the
# character walks the last stretch before a link and settles at it, then
# crosses it (at a sprint for SPRINT_CROSSING_CLASSES); the crossing starts
# within this of the link's departure point.
TRAVERSAL_START_RADIUS_M = 0.8
TRAVERSAL_START_VERTICAL_M = 1.5
TRAVERSAL_APPROACH_WALK_M = 4.0
# The route follower hands over to the crossing this far out. Its pulses
# carry the character up to ~1.8 m, and the game vaults on touching the
# obstacle ~0.4 m before the departure point (live: 2.2 m short of a
# link one sample, airborne over it the next, with no crossing begun).
TRAVERSAL_FOLLOWER_HANDOFF_M = 3.0
TRAVERSAL_HOLD_SECONDS = PURSUIT_HOLD_SECONDS
# Links crossed at a sprint. Sprinting, the game vaults a low wall, steps up a
# ledge or hops down one by itself, without the Terrain Action prompt (owner,
# 2026-10-21; live, a Mt. Nibel cave ledge that a walk only stopped at). A
# ladder, crack or crawl space is walked into, and a long span is not jumped.
SPRINT_CROSSING_CLASSES = frozenset({"step_over", "step_down", "step_up", "drop", "climb"})
# Links a crossing failed on in this process (the newest few), left out of
# later plans: some navmesh links are moves only enemies make.
FAILED_TRAVERSAL_LINKS_MAX = 64
FAILED_TRAVERSAL_LINKS: dict[int, None] = {}


def record_failed_traversal_link(link_reference: int) -> None:
    FAILED_TRAVERSAL_LINKS.pop(int(link_reference), None)
    FAILED_TRAVERSAL_LINKS[int(link_reference)] = None
    while len(FAILED_TRAVERSAL_LINKS) > FAILED_TRAVERSAL_LINKS_MAX:
        FAILED_TRAVERSAL_LINKS.pop(next(iter(FAILED_TRAVERSAL_LINKS)))
TRAVERSAL_PULSE_MS = 250
# The sidecar lookahead reads a low girder as a blocker, which the character
# walks over: it is not consulted this close to a link.
TRAVERSAL_LOOKAHEAD_EXEMPT_M = 6.0
# A gimmick may set the character down a few polygons past its landing
# point (a ladder's top); stepping back onto it could climb down again.
GIMMICK_LANDING_CORRIDOR_AHEAD = 12
# A gimmick's trigger can sit on a polygon seam (live at the Kalm crack the
# departure was 6 cm into the next polygon of the same floor): its departure
# is proven by being on that floor, near the point; the game's trigger takes
# it from there and the landing is proven exactly.
GIMMICK_DEPARTURE_SNAP_M = 0.5
GIMMICK_DEPARTURE_VERTICAL_M = 1.0
SPRINT_MIN_STRAIGHT_RUN_M = 6.0
SPRINT_MAX_TURN_DEG = 25.0
SPRINT_PULSE_HANDOFF_MAX_SPEED_M_S = 0.60
SPRINT_PULSE_HANDOFF_DRIFT_HORIZON_SECONDS = 0.50
SPRINT_PULSE_HANDOFF_DRIFT_MARGIN_M = 0.10
DEFAULT_FINAL_ARRIVAL_RADIUS_M = 0.75
# Cloud telemetry is reported from the actor/capsule origin while route
# waypoints are floor/navmesh points. Keep the horizontal arrival radius tight
# but allow the observed floor-to-origin offset on every waypoint.
DEFAULT_WAYPOINT_VERTICAL_TOLERANCE_M = 1.5
ALLOWED_GROUND_SIDES = frozenset({0, 2, 4, 6, 128})
READINESS_FAILURES = frozenset({
    "loading_or_streaming",
    "not_controllable",
    "menu_or_pause",
    "cutscene_active",
    "combat_active",
    "interaction_active",
    "telemetry_gameplay_not_ready",
    "telemetry_stale",
    "telemetry_not_advancing",
})
CORRIDOR_ROUTE_MODES = frozenset({
    "ordinary_multi_polygon",
    "ordinary_multi_tile",
    "cached_topology",
})


def _sanitize_exception_message(exc: BaseException) -> str:
    """Keep fail-closed diagnostics useful without returning a raw traceback."""
    message = " ".join(str(exc).split())
    return message[:1024]


class AdapterState(str, Enum):
    DISARMED = "DISARMED"
    OBSERVING = "OBSERVING"
    TOPOLOGY_READY = "TOPOLOGY_READY"
    PLAN_READY = "PLAN_READY"
    PLAN_VALIDATED = "PLAN_VALIDATED"
    FOLLOWING = "FOLLOWING"
    PAUSED_FOR_READINESS = "PAUSED_FOR_READINESS"
    ARRIVED = "ARRIVED"
    STOPPED = "STOPPED"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    FAILED = "FAILED"


class CorridorDivergenceClass(str, Enum):
    """Safety classification for a post-command corridor mismatch."""

    AUTHORIZED_PORTAL_TRANSITION = "authorized_portal_transition"
    BOUNDED_CORRIDOR_REACQUISITION = "bounded_corridor_reacquisition"
    PROBABLE_COLLISION_DIVERGENCE = "probable_collision_divergence"
    PROVISIONAL_STATIC_HANDOFF = "provisional_static_handoff"
    UNEXPLAINED_UNAUTHORIZED_DIVERGENCE = "unexplained_unauthorized_divergence"


# A moment without Cloud's pose (a traversal animation) is waited out.
POSE_GAP_RETRIES = 8
POSE_GAP_RETRY_SECONDS = 0.15

class AdapterError(RuntimeError):
    """A fail-closed refusal or live adapter failure."""


class _PostCommandReadinessError(AdapterError):
    """The post-pulse sample needs the normal bounded readiness recovery."""

    def __init__(self, failures: Sequence[str]) -> None:
        self.failures = tuple(str(failure) for failure in failures)
        super().__init__(
            "post_command_freshness_reacquisition_failed:"
            + ";".join(self.failures)
        )


@dataclass(frozen=True)
class LiveObservation:
    """Normalized live state required by the authorization gate.

    A producer must explicitly supply the safety fields.  Missing fields are
    represented as ``False``/``None`` and therefore cannot authorize motion.
    """

    monotonic_t: float
    timestamp_utc: str
    sequence: int
    position: Vec3
    actor_yaw: float
    camera_yaw: float | None
    actor_id: str
    session_id: str
    world_id: str
    map_id: str
    process_valid: bool
    executable_valid: bool
    controllable: bool
    loading: bool
    in_menu: bool
    in_cutscene: bool
    in_combat: bool
    in_interaction: bool
    lease_active: bool
    actuator_neutral: bool
    telemetry_age_seconds: float
    topology_session_id: str | None = None
    process_id: int | None = None
    executable_sha256: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    telemetry_gameplay_ready: bool = False
    dynamic_obstacles: DynamicObstacleSnapshot | None = field(default=None, repr=False, compare=False)
    position_reference: str = "actor_origin"
    actor_origin: Vec3 | None = None
    grounded: bool | None = None
    velocity_cm_s: Vec3 | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "monotonic_t": self.monotonic_t,
            "timestamp_utc": self.timestamp_utc,
            "sequence": self.sequence,
            "position": _point_dict(self.position),
            "position_reference": self.position_reference,
            "actor_origin": _point_dict(self.actor_origin) if self.actor_origin else None,
            "grounded": self.grounded,
            "velocity_cm_s": _point_dict(self.velocity_cm_s) if self.velocity_cm_s else None,
            "actor_yaw": self.actor_yaw,
            "camera_yaw": self.camera_yaw,
            "actor_id": self.actor_id,
            "session_id": self.session_id,
            "world_id": self.world_id,
            "map_id": self.map_id,
            "process_valid": self.process_valid,
            "executable_valid": self.executable_valid,
            "controllable": self.controllable,
            "loading": self.loading,
            "in_menu": self.in_menu,
            "in_cutscene": self.in_cutscene,
            "in_combat": self.in_combat,
            "in_interaction": self.in_interaction,
            "lease_active": self.lease_active,
            "actuator_neutral": self.actuator_neutral,
            "telemetry_age_seconds": self.telemetry_age_seconds,
            "topology_session_id": self.topology_session_id,
            "process_id": self.process_id,
            "executable_sha256": self.executable_sha256,
            "telemetry_gameplay_ready": self.telemetry_gameplay_ready,
            "dynamic_obstacles": (
                {
                    "source_identity": self.dynamic_obstacles.source_identity,
                    "base_sequence": self.dynamic_obstacles.base_sequence,
                    "sampled_at_ms": self.dynamic_obstacles.sampled_at_ms,
                    "complete": self.dynamic_obstacles.complete,
                    "observed_actor_count": self.dynamic_obstacles.observed_actor_count,
                    "accepted_actor_count": self.dynamic_obstacles.accepted_actor_count,
                    "rejected_actor_count": self.dynamic_obstacles.rejected_actor_count,
                    "rejection_reason_codes": list(self.dynamic_obstacles.rejection_reason_codes),
                }
                if self.dynamic_obstacles is not None else None
            ),
        }


@dataclass(frozen=True)
class TopologySession:
    graph: DetourTopologyGraph
    artifact_path: str
    fingerprint: str
    session_id: str
    world_id: str
    captured_at_utc: str | None
    current_session: bool
    movement_sent: bool
    payload_summary: Mapping[str, Any] = field(default_factory=dict)
    timings: Mapping[str, float] = field(default_factory=dict)
    static_fingerprint: str | None = None
    source_decision: str = "native_capture_required"
    artifact_sha256: str | None = None
    map_id: str | None = None
    scope_identity: str | None = None
    scope_process_id: int | None = None
    scope_polygon_refs: tuple[int, ...] = ()
    scope_transition_signatures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_path": self.artifact_path,
            "fingerprint": self.fingerprint,
            "static_fingerprint": self.static_fingerprint or self.fingerprint,
            "source_decision": self.source_decision,
            "artifact_sha256": self.artifact_sha256,
            "map_id": self.map_id,
            "scope_identity": self.scope_identity,
            "scope_process_id": self.scope_process_id,
            "scope_polygon_count": len(self.scope_polygon_refs),
            "scope_transition_count": len(self.scope_transition_signatures),
            "session_id": self.session_id,
            "world_id": self.world_id,
            "captured_at_utc": self.captured_at_utc,
            "current_session": self.current_session,
            "movement_sent": self.movement_sent,
            "payload_summary": dict(self.payload_summary),
            "snapshot_diagnostics": dict(self.graph.snapshot_diagnostics),
            "timings": dict(self.timings),
        }


@dataclass(frozen=True)
class RenderedGameplayEvidence:
    """Evidence from the existing foreground-window render boundary."""

    ready: bool
    reason: str
    captured_at_utc: str | None = None
    classifier_label: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)
    capture: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "reason": self.reason,
            "captured_at_utc": self.captured_at_utc,
            "classifier_label": self.classifier_label,
            "metrics": dict(self.metrics),
            "capture": dict(self.capture),
        }


@dataclass(frozen=True)
class PlanAuthorization:
    session_id: str
    world_id: str
    map_id: str
    topology_fingerprint: str
    route_plan_sha256: str
    start_ref: int
    end_ref: int
    resolved_start: Vec3
    resolved_end: Vec3
    created_monotonic: float
    route_mode: str = "same_polygon"
    scope_identity: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "world_id": self.world_id,
            "map_id": self.map_id,
            "topology_fingerprint": self.topology_fingerprint,
            "route_plan_sha256": self.route_plan_sha256,
            "start_ref": self.start_ref,
            "end_ref": self.end_ref,
            "resolved_start": _point_dict(self.resolved_start),
            "resolved_end": _point_dict(self.resolved_end),
            "created_monotonic": self.created_monotonic,
            "route_mode": self.route_mode,
            "scope_identity": self.scope_identity,
        }


@dataclass(frozen=True)
class RouteAuthorizationProof:
    """Immutable static proof produced before the live follower starts.

    The graph/route/validator facts in this object are not re-derived from
    every telemetry sample.  Live checks still revalidate the mutable sample
    against these identities and route-local geometry; any identity mismatch
    fails closed.
    """

    route_plan_sha256: str
    topology_session_id: str
    topology_fingerprint: str
    session_id: str
    world_id: str
    map_id: str
    scope_identity: str | None
    route_mode: str
    start_ref: int
    end_ref: int
    corridor_refs: tuple[int, ...]
    waypoints: tuple[Vec3, ...]
    cumulative_waypoint_m: tuple[float, ...]
    validator_result_sha256: str
    validated_portal_evidence_sha256: str
    static_safety_evidence_sha256: str
    static_obstacle_evidence_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_plan_sha256": self.route_plan_sha256,
            "topology_session_id": self.topology_session_id,
            "topology_fingerprint": self.topology_fingerprint,
            "session_id": self.session_id,
            "world_id": self.world_id,
            "map_id": self.map_id,
            "scope_identity": self.scope_identity,
            "route_mode": self.route_mode,
            "start_ref": self.start_ref,
            "end_ref": self.end_ref,
            "corridor_refs": list(self.corridor_refs),
            "waypoints": [_point_dict(point) for point in self.waypoints],
            "cumulative_waypoint_m": list(self.cumulative_waypoint_m),
            "validator_result_sha256": self.validator_result_sha256,
            "validated_portal_evidence_sha256": self.validated_portal_evidence_sha256,
            "static_safety_evidence_sha256": self.static_safety_evidence_sha256,
            "static_obstacle_evidence_sha256": self.static_obstacle_evidence_sha256,
        }


@dataclass(frozen=True)
class SelectedRoute:
    """One selector-owned destination and its already-validated route plan."""

    destination: Vec3
    resolution: Any
    plan: Mapping[str, Any]
    candidates: tuple[Mapping[str, Any], ...]
    selection_id: str
    topology_session_id: str
    topology_fingerprint: str
    plan_sha256: str


# Sweep, re-plan, sweep again: bounded so a route that cannot be cleared
# fails before movement.
MAX_ROUTE_AUDIT_ROUNDS = 5


def capture_proof_locality_miss(payload: Mapping[str, Any]) -> bool:
    """A complete (or tile-excluded partial) capture whose topology proof
    only missed Cloud's locality: see topology_proof_locality_miss."""
    if not isinstance(payload, Mapping):
        return False
    exported = payload.get("status") == "complete" or tile_excluded_partial(payload) is not None
    return exported and topology_proof_locality_miss(payload)
# The navmesh is eroded by the agent radius, so Cloud can stand just past a
# polygon edge: live by Nene in Kalm 2026-09-30, 2 cm outside,
# and the capture made there was thrown away. A start that close counts as
# on the polygon it snaps to.
START_EDGE_TOLERANCE_M = 0.10
CAPTURE_TARGET_REACH_M = 4.0


def start_on_navmesh(resolution: Any) -> bool:
    """Whether a start resolution puts Cloud on the navmesh."""
    if not resolution.ok:
        return False
    if resolution.reason_code == "point_inside_polygon":
        return True
    distance = resolution.horizontal_distance_m
    return (resolution.reason_code == "point_snapped_to_polygon"
            and distance is not None and distance <= START_EDGE_TOLERANCE_M)


NEUTRAL_GAP_STALL_PAUSE_SECONDS = 1.5
# A sample landing a few polygons further along the authorized corridor
# (live at Kalm: polygons under a metre deep crossed two or three at a time
# between samples) is accepted when it is bounded and consistent: see
# LiveRouteAdapter._bounded_forward_corridor_skip.
MAX_FORWARD_CORRIDOR_SKIP = 8
# Live on the Nibelheim-to-Chadley route at fast-follow speed, 7 of 9 route
# restarts were forward skips of 2-4 authorized polygons after moving
# 2.52-3.35 m between samples; 4.5 m covers ~6 m/s with a slow sample.
FORWARD_SKIP_MAX_MOVE_M = 4.5
FORWARD_SKIP_ALONG_SLACK_M = 1.0
# Smooth camera-follow: while Cloud moves, the camera turns toward the route
# lookahead a little each command (a share of the error, rate-limited) in an
# eased mouse sweep, instead of stopping him to snap it round.
SMOOTH_CAMERA_GAIN = 0.4
SMOOTH_CAMERA_MAX_DEG_PER_S = 60.0
SMOOTH_CAMERA_DEADBAND_DEG = 2.0
# After a failed camera settle, how long to wait for Cloud to be
# controllable again before the route stops.
CAMERA_FALLBACK_CONTROL_WAIT_SECONDS = 4.0
# A stall is looked at before anything is learned from it: live at Kalm the
# stall learner turned Aerith and Tifa, standing on the stairs to Nene, into
# static obstacles that closed the stairs and sent Cloud on a 100 m detour.
STALL_SWEEP_AHEAD_CM = 250.0
STALL_CHARACTER_WAIT_SECONDS = 1.5
STALL_SPOT_RADIUS_CM = 200.0
STALL_GUESSES_DISCARDED_PER_SPOT = 2
# Some blockers collide with Cloud but answer no engine query (live: the
# Golden Plumes wagon at Kalm). A stall with nothing seen becomes a small
# obstacle just ahead of Cloud, and the route is re-planned around it.
INVISIBLE_BLOCKER_AHEAD_CM = 60.0
INVISIBLE_BLOCKER_RADIUS_M = 0.5
MAX_INVISIBLE_BLOCKERS_PER_ROUTE = 3
ROUTE_TARGET_OBSTACLE_MARGIN_M = 0.15
# Automatic doors and gates (dynamic_gates.py): a route stops short of a gate
# and waits for it; these are the stops that end the route for good.
GATE_STOP_REASONS = frozenset({"gate_did_not_open", "gate_state_unverified"})
# Actor obstacles remembered from earlier sweeps are swept across again before
# a route is planned round them (a door shut then may be open now); no more
# than this many, and how far past the obstacle each sweep reaches.
MAX_REVERIFIED_OBSTACLES = 3
REVERIFY_REACH_CM = 60.0


@dataclass(frozen=True)
class LiveAdapterConfig:
    controller_mode: str = "pulse"
    sprint: bool = False
    # Opt-in continuous refinement (feature trial, not live-validated): every
    # supervised hold must also prove the measured-momentum ray, and an
    # intermediate waypoint's follower-distance slow band may be held at
    # normal magnitude only when that exact supervised proof passes.
    continuous_fast_follow: bool = False
    route_mode: str = "same_polygon"
    # Capture admission only: route modes to try, on the topology already
    # loaded, when ``route_mode`` finds no evidence route from where Cloud
    # stands. Live at Kalm neither a same-polygon, a same-tile 10-15 m nor a
    # simple 30-50 m destination fitted among the town's small polygons, yet
    # the planner routed to Ned; ``cached_topology`` uses the adapter's
    # ``route_target`` as the evidence route. Any validated route evidences
    # the graph.
    admission_route_fallback: tuple[str, ...] = ()
    # Capture-only evidence routes may use the proven navmesh projection of
    # a target within 4 m of reachable floor. Ordinary
    # live route targets still have to be inside their polygon.
    admission_target_edge_projection: bool = False
    max_telemetry_age_seconds: float = DEFAULT_MAX_TELEMETRY_AGE_SECONDS
    preflight_telemetry_wait_seconds: float = DEFAULT_PREFLIGHT_TELEMETRY_WAIT_SECONDS
    settled_gameplay_timeout_seconds: float = 30.0
    readiness_recovery_timeout_seconds: float = DEFAULT_READINESS_RECOVERY_TIMEOUT_SECONDS
    readiness_reacquire_max_cross_track_m: float = DEFAULT_READINESS_REACQUIRE_MAX_CROSS_TRACK_M
    final_arrival_radius_m: float = DEFAULT_FINAL_ARRIVAL_RADIUS_M
    waypoint_vertical_tolerance_m: float = DEFAULT_WAYPOINT_VERTICAL_TOLERANCE_M
    telemetry_poll_seconds: float = DEFAULT_TELEMETRY_POLL_SECONDS
    control_period_seconds: float = DEFAULT_CONTROL_PERIOD_SECONDS
    start_max_snap_m: float = 0.75
    start_max_vertical_m: float = 1.0
    destination_min_distance_m: float = 3.0
    destination_max_distance_m: float = 6.0
    multi_polygon_min_distance_m: float = 10.0
    multi_polygon_max_distance_m: float = 15.0
    multi_tile_min_distance_m: float = DEFAULT_MULTI_TILE_MIN_DISTANCE_M
    multi_tile_max_distance_m: float = DEFAULT_MULTI_TILE_MAX_DISTANCE_M
    multi_tile_max_crossings: int = DEFAULT_MULTI_TILE_MAX_CROSSINGS
    multi_tile_absolute_max_cycles: int = DEFAULT_MULTI_TILE_ABSOLUTE_MAX_CYCLES
    cached_topology_max_expansions: int = 500_000
    cached_topology_max_waypoints: int = 512
    cached_topology_absolute_max_cycles: int = 2_400
    cached_topology_wall_clock_seconds: float = 600.0
    destination_edge_clearance_m: float = 0.5
    normal_stick_magnitude: float = DEFAULT_NORMAL_STICK_MAGNITUDE
    slow_stick_magnitude: float = DEFAULT_SLOW_STICK_MAGNITUDE
    minimum_effective_stick_magnitude: float = DEFAULT_MINIMUM_EFFECTIVE_STICK_MAGNITUDE
    approach_stick_magnitude: float = DEFAULT_APPROACH_STICK_MAGNITUDE
    final_stick_magnitude: float = DEFAULT_FINAL_STICK_MAGNITUDE
    final_slow_distance_m: float = 1.5
    final_control_distance_m: float = DEFAULT_FINAL_CONTROL_DISTANCE_M
    approach_control_period_seconds: float = DEFAULT_APPROACH_CONTROL_PERIOD_SECONDS
    final_control_period_seconds: float = DEFAULT_FINAL_CONTROL_PERIOD_SECONDS
    max_cycles: int = 300
    multi_polygon_wall_clock_seconds: float = 180.0
    post_arrival_samples: int = 2
    require_camera_yaw: bool = True
    # Camera-follow remains opt-in and lives with the existing live-route
    # configuration surface.
    camera_follow_enabled: bool = False
    camera_follow_lookahead_m: float = 5.0
    camera_follow_sprint_lookahead_m: float = 9.0
    camera_follow_deadband_deg: float = 10.0
    camera_follow_max_input: float = 0.35
    camera_follow_gain: float = 1.0 / 90.0
    camera_follow_settle_hysteresis_deg: float = 3.0
    camera_follow_arrival_target: tuple[float, float, float] | None = None
    camera_follow_arrival_distance_m: float = 5.0
    # Turn the camera smoothly while moving instead of stopping to snap it.
    camera_follow_smooth: bool = False
    # Stream the stick along the route (pure pursuit) with sparse supervision,
    # handing back to the careful controller on anything unexpected.
    pursuit: bool = False
    portal_corner_inset_m: float = 0.5
    static_lookahead_distance_threshold_m: float = 8.0
    static_lookahead_interval_seconds: float = 5.0
    static_lookahead_heading_change_degrees: float = 45.0
    static_lookahead_budget_window_seconds: float | None = 2.0
    routing_objective: str = "distance"
    # Plan across short off-mesh traversal links (offmesh_links.py) and walk
    # them; off, a route never uses one.
    traversal_links: bool = True
    # The scanned traversal gimmicks a route may cross (gimmick_links.py);
    # None plans on the capture's own links only.
    gimmick_evidence_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.routing_objective, str) or self.routing_objective not in {"distance", "travel_time"}:
            raise ValueError("routing_objective must be distance or travel_time")
        if type(self.sprint) is not bool or (self.sprint and self.controller_mode != "continuous"):
            raise ValueError("sprint requires continuous mode and a boolean flag")
        if type(self.continuous_fast_follow) is not bool or (
            self.continuous_fast_follow and (self.controller_mode != "continuous" or self.sprint)
        ):
            raise ValueError("continuous_fast_follow requires non-sprint continuous mode and a boolean flag")
        if type(self.camera_follow_enabled) is not bool:
            raise ValueError("camera_follow_enabled must be a boolean")
        if type(self.camera_follow_smooth) is not bool:
            raise ValueError("camera_follow_smooth must be a boolean")
        if type(self.pursuit) is not bool or (self.pursuit and self.controller_mode != "continuous"):
            raise ValueError("pursuit requires continuous mode and a boolean flag")
        if type(self.traversal_links) is not bool:
            raise ValueError("traversal_links must be a boolean")
        if self.gimmick_evidence_path is not None:
            object.__setattr__(self, "gimmick_evidence_path", Path(self.gimmick_evidence_path))
        CameraFollowConfig(
            camera_follow_enabled=self.camera_follow_enabled,
            camera_follow_lookahead_m=self.camera_follow_lookahead_m,
            camera_follow_sprint_lookahead_m=self.camera_follow_sprint_lookahead_m,
            camera_follow_deadband_deg=self.camera_follow_deadband_deg,
            camera_follow_max_input=self.camera_follow_max_input,
            camera_follow_gain=self.camera_follow_gain,
            camera_follow_settle_hysteresis_deg=self.camera_follow_settle_hysteresis_deg,
            camera_follow_arrival_target=self.camera_follow_arrival_target,
            camera_follow_arrival_distance_m=self.camera_follow_arrival_distance_m,
        ).validate()
        if self.controller_mode not in {"pulse", "continuous"}:
            raise ValueError("controller_mode must be pulse or continuous")
        if not math.isfinite(self.portal_corner_inset_m) or self.portal_corner_inset_m < 0:
            raise ValueError("portal corner inset must be finite and non-negative")
        positive = (
            "max_telemetry_age_seconds",
            "preflight_telemetry_wait_seconds",
            "settled_gameplay_timeout_seconds",
            "readiness_recovery_timeout_seconds",
            "readiness_reacquire_max_cross_track_m",
            "final_arrival_radius_m",
            "waypoint_vertical_tolerance_m",
            "telemetry_poll_seconds",
            "control_period_seconds",
            "start_max_snap_m",
            "start_max_vertical_m",
            "destination_min_distance_m",
            "destination_max_distance_m",
            "multi_polygon_min_distance_m",
            "multi_polygon_max_distance_m",
            "multi_tile_min_distance_m",
            "multi_tile_max_distance_m",
            "destination_edge_clearance_m",
            "final_slow_distance_m",
            "final_control_distance_m",
            "approach_control_period_seconds",
            "final_control_period_seconds",
            "multi_polygon_wall_clock_seconds",
            "cached_topology_wall_clock_seconds",
            "static_lookahead_distance_threshold_m",
            "static_lookahead_interval_seconds",
            "static_lookahead_heading_change_degrees",
        )
        for field_name in positive:
            value = float(getattr(self, field_name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{field_name} must be finite and positive")
        if self.static_lookahead_budget_window_seconds is not None:
            try:
                window_seconds = float(self.static_lookahead_budget_window_seconds)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("static_lookahead_budget_window_seconds must be finite and positive") from exc
            if not math.isfinite(window_seconds) or window_seconds <= 0.0:
                raise ValueError("static_lookahead_budget_window_seconds must be finite and positive")
        if self.destination_max_distance_m < self.destination_min_distance_m:
            raise ValueError("destination distance bounds are inverted")
        if self.multi_polygon_max_distance_m < self.multi_polygon_min_distance_m:
            raise ValueError("multi-polygon distance bounds are inverted")
        if self.multi_tile_max_distance_m < self.multi_tile_min_distance_m:
            raise ValueError("multi-tile distance bounds are inverted")
        if int(self.multi_tile_max_crossings) <= 0 or int(self.multi_tile_absolute_max_cycles) <= 0:
            raise ValueError("multi-tile bounds must be positive")
        if (
            int(self.cached_topology_max_expansions) <= 0
            or int(self.cached_topology_max_waypoints) <= 0
            or int(self.cached_topology_absolute_max_cycles) <= 0
        ):
            raise ValueError("cached-topology route bounds must be positive")
        if self.route_mode not in {"same_polygon", "ordinary_multi_polygon", "ordinary_multi_tile", "cached_topology"}:
            raise ValueError("route_mode must be same_polygon, ordinary_multi_polygon, ordinary_multi_tile or cached_topology")
        if not isinstance(self.admission_route_fallback, tuple) or any(
            mode not in {"same_polygon", "ordinary_multi_polygon", "ordinary_multi_tile", "cached_topology"}
            for mode in self.admission_route_fallback
        ):
            raise ValueError("admission_route_fallback must be a tuple of route modes")
        for field_name in (
            "normal_stick_magnitude",
            "slow_stick_magnitude",
            "minimum_effective_stick_magnitude",
            "approach_stick_magnitude",
            "final_stick_magnitude",
        ):
            value = float(getattr(self, field_name))
            if not math.isfinite(value) or not 0.0 < value <= 1.0:
                raise ValueError(f"{field_name} must be in (0, 1]")
        if self.final_control_distance_m >= self.final_slow_distance_m:
            raise ValueError("final control distance must be below final slow distance")
        if self.approach_stick_magnitude < self.minimum_effective_stick_magnitude:
            raise ValueError("approach stick magnitude is below effective movement floor")
        if self.final_stick_magnitude < self.minimum_effective_stick_magnitude:
            raise ValueError("final stick magnitude is below effective movement floor")
        if int(self.max_cycles) <= 0 or int(self.post_arrival_samples) < 0:
            raise ValueError("cycle and post-arrival bounds must be non-negative")


class TelemetrySource(Protocol):
    def observe(self) -> LiveObservation: ...


class TopologyProvider(Protocol):
    def load_current(self, observation: LiveObservation) -> TopologySession: ...


class RenderEvidenceSource(Protocol):
    def observe(self, observation: LiveObservation) -> RenderedGameplayEvidence: ...


class Validator(Protocol):
    def validate(self, plan: Mapping[str, Any]) -> Mapping[str, Any]: ...


class Follower(Protocol):
    state: Any
    current_waypoint_index: int
    active_waypoint: Any
    route: Any

    def on_telemetry(self, sample: Mapping[str, Any]) -> list[dict[str, Any]]: ...

    def process_external_event(self, event_name: str, t: float) -> dict[str, Any]: ...

    def pause_for_safety(self, t: float) -> Mapping[str, Any] | None: ...

    def resume_after_safety_pause(self, t: float) -> Mapping[str, Any] | None: ...

    def progress_accounting(self, t: float | None = None) -> Mapping[str, Any] | None: ...


class Actuator(Protocol):
    def command(self, x: float, y: float, milliseconds: int) -> Mapping[str, Any] | None: ...

    def neutralize(self) -> Mapping[str, Any] | None: ...

    def is_neutral(self) -> bool | None: ...


class CameraActuator(Protocol):
    """Presentation-only horizontal camera input.

    The command is a bounded, instantaneous relative-mouse delta. It never
    owns the movement stick or a movement lease.
    """

    def command_horizontal(self, delta: int) -> Mapping[str, Any] | None: ...

    def neutralize(self) -> Mapping[str, Any] | None: ...


class ExistingHorizontalCameraActuator:
    """Use the reviewed keyboard/mouse horizontal camera path.

    WindowsHybridGamepadInput keeps analogue left-stick movement on its
    virtual X360 device and exposes this bounded mouse event separately. A
    relative mouse event has no held state, so neutralization is an explicit
    no-op status operation rather than a speculative right-stick release.
    """

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def command_horizontal(self, delta: int) -> Mapping[str, Any] | None:
        callback = getattr(self.backend, "calibrate_camera_horizontal", None)
        if not callable(callback):
            raise AdapterError("horizontal_camera_actuator_unavailable")
        return callback(int(delta))

    def stream_horizontal(self, delta: int, milliseconds: int,
                          should_stop: Callable[[], bool] | None = None) -> Mapping[str, Any] | None:
        callback = getattr(self.backend, "smooth_camera_horizontal", None)
        if not callable(callback):
            raise AdapterError("smooth_camera_actuator_unavailable")
        return callback(int(delta), int(milliseconds), should_stop)

    def neutralize(self) -> Mapping[str, Any] | None:
        return {
            "backend": type(self.backend).__name__,
            "camera_input": "neutral",
            "held": False,
        }


class ExistingAnalogueActuator:
    """Adapter over the reviewed repository analogue backend.

    ``backend.move`` bounds and neutralizes pulses; ``supervised_move`` uses
    the same stable broker for expiring holds renewed by validated samples.
    Both modes share the backend release path.
    """

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def command(self, x: float, y: float, milliseconds: int) -> Mapping[str, Any] | None:
        self.backend.move(float(x), float(y), int(milliseconds))
        return {
            "backend": type(self.backend).__name__,
            "x": float(x),
            "y": float(y),
            "milliseconds": int(milliseconds),
        }

    def supervised_command(self, x, y, *, deadline, stream_id, sequence, sprint=False):
        return self.backend.supervised_move(x, y, deadline=deadline, stream_id=stream_id, sequence=sequence, sprint=sprint)

    def neutralize(self) -> Mapping[str, Any] | None:
        self.backend.release_all()
        return self.status()

    def status(self) -> Mapping[str, Any]:
        callback = getattr(self.backend, "backend_status", None)
        if callable(callback):
            return callback()
        return {"backend": type(self.backend).__name__}

    def is_neutral(self) -> bool | None:
        status = self.status()
        if "stick_state" in status:
            return status.get("stick_state") == "neutral"
        last_stick = status.get("last_stick")
        if isinstance(last_stick, Mapping):
            return float(last_stick.get("x", 1.0)) == 0.0 and float(last_stick.get("y", 1.0)) == 0.0
        return None


class JsonTopologyProvider:
    """Load a fresh read-only shipping Detour capture for the current session.

    The native geometry probe remains the capture mechanism.  This provider
    owns a one-shot, render-gated geometry request, then reads only the new
    artifact and its matching native report/timing evidence.
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_TOPOLOGY_PATH,
        *,
        probe_path: str | Path = DEFAULT_TOPOLOGY_PROBE_PATH,
        enable_path: str | Path = DEFAULT_TOPOLOGY_ENABLE_PATH,
        readiness_path: str | Path = DEFAULT_TOPOLOGY_READY_PATH,
        timing_path: str | Path = DEFAULT_TOPOLOGY_TIMING_PATH,
        status_path: str | Path = DEFAULT_TOPOLOGY_STATUS_PATH,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        max_capture_age_seconds: float = 30.0,
        max_capture_wait_seconds: float = DEFAULT_MAX_CAPTURE_WAIT_SECONDS,
        capture_poll_seconds: float = DEFAULT_CAPTURE_POLL_SECONDS,
        max_pose_delta_m: float = 0.75,
        expected_executable_sha256: str = LIVE_EXECUTABLE_SHA256,
        expected_native_probe_sha256: str | None = None,
        process_alive: Callable[[], bool] | None = None,
        capture_request_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.probe_path = Path(probe_path)
        self.enable_path = Path(enable_path)
        self.readiness_path = Path(readiness_path)
        self.timing_path = Path(timing_path)
        self.status_path = Path(status_path)
        self.clock = clock
        self.sleeper = sleeper
        self.max_capture_age_seconds = float(max_capture_age_seconds)
        self.max_capture_wait_seconds = float(max_capture_wait_seconds)
        self.capture_poll_seconds = float(capture_poll_seconds)
        self.max_pose_delta_m = float(max_pose_delta_m)
        self.expected_executable_sha256 = expected_executable_sha256.upper()
        self.expected_native_probe_sha256 = (
            str(expected_native_probe_sha256).upper()
            if expected_native_probe_sha256 is not None else None
        )
        self.process_alive = process_alive
        self.capture_request_id = str(capture_request_id or "") or None
        self._capture_armed = False
        self._capture_arm_evidence: RenderedGameplayEvidence | None = None
        self._capture_baseline: dict[str, Mapping[str, Any]] = {}
        self._capture_armed_at: float | None = None
        self.request_id: str | None = None
        self.capture_accepted = False
        self.capture_status: Mapping[str, Any] | None = None
        self.capture_status_timeline: list[dict[str, Any]] = []

        if not math.isfinite(self.max_capture_wait_seconds) or self.max_capture_wait_seconds <= 0.0:
            raise ValueError("max_capture_wait_seconds must be finite and positive")
        if not math.isfinite(self.capture_poll_seconds) or self.capture_poll_seconds <= 0.0:
            raise ValueError("capture_poll_seconds must be finite and positive")

    def arm_capture(
        self,
        observation: LiveObservation,
        render_evidence: RenderedGameplayEvidence,
    ) -> None:
        """Arm one fresh native geometry capture after both readiness gates."""
        if (
            observation.telemetry_gameplay_ready is not True
            or observation.loading
            or observation.controllable is not True
            or not _render_evidence_confirms_gameplay(observation, render_evidence)
        ):
            raise AdapterError("topology_capture_requires_settled_render")
        previous = self._read_json_optional(self.status_path)
        previous_state = str(previous.get("state") or "")
        previous_id = str(previous.get("request_id") or "")
        if previous_id and previous_state in {
            "accepted",
            "build_report_started",
            "geometry_extracting",
            "geometry_serializing",
            "geometry_written",
            "probe_written",
        }:
            raise AdapterError("capture_previous_request_in_progress")
        self._clear_capture_markers()
        self.request_id = self.capture_request_id or str(uuid.uuid4())
        self.capture_accepted = False
        self.capture_status = None
        self.capture_status_timeline = []
        self._capture_baseline = {
            "artifact": _file_identity(self.path),
            "probe": _file_identity(self.probe_path),
            "timing": _file_identity(self.timing_path),
            "status": _file_identity(self.status_path),
        }
        baseline_timing = self._read_json_optional(self.timing_path)
        self._capture_baseline["timing"] = {
            **self._capture_baseline["timing"],
            "probe_attempt": baseline_timing.get("probe_attempt", 0),
        }
        self._capture_armed_at = self.clock()
        try:
            marker = json.dumps({
                "schema_version": 2,
                "request_id": self.request_id,
                "requested_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "session_id": observation.session_id,
                "world_id": observation.world_id,
                "map_id": observation.map_id,
                "executable_sha256": observation.executable_sha256 or self.expected_executable_sha256,
                "native_probe_sha256": self.expected_native_probe_sha256 or "",
                "purpose": "static_topology_cache_seed",
                "telemetry_gameplay_ready": True,
                "rendered_gameplay_confirmed": True,
                "rendered_gameplay_proof": render_evidence.to_dict(),
                "armed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }, sort_keys=True)
            # The enable marker is created first. The readiness token is
            # published last so the native probe can never see a half-armed
            # request as an expensive geometry grant.
            _atomic_write_text(self.enable_path, marker)
            _atomic_write_text(self.readiness_path, marker)
        except OSError as exc:
            self._clear_capture_markers()
            raise AdapterError("topology_capture_readiness_marker_write_failed") from exc
        self._capture_armed = True
        self._capture_arm_evidence = render_evidence

    def disarm_capture(self) -> None:
        """Disarm only before native acceptance; preserve an acknowledged job."""
        self._capture_armed = False
        self._capture_arm_evidence = None
        self._capture_baseline = {}
        self._capture_armed_at = None
        status = self.capture_status or self._read_json_optional(self.status_path)
        state = str(status.get("state") or "")
        if not self.capture_accepted or state in {"completed", "failed"}:
            self._clear_capture_markers()

    def _clear_capture_markers(self) -> None:
        errors: list[OSError] = []
        for path in (self.enable_path, self.readiness_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(exc)
        if errors:
            raise AdapterError("topology_capture_marker_clear_failed") from errors[0]

    def _wait_for_fresh_capture(
        self,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
        if not self.request_id:
            raise AdapterError("capture_request_id_missing")
        deadline = self.clock() + self.max_capture_wait_seconds
        while self.clock() <= deadline:
            status = self._capture_status_sample()
            if status is not None:
                state = str(status.get("state") or "")
                if state in {
                    "accepted",
                    "build_report_started",
                    "geometry_extracting",
                    "geometry_serializing",
                    "geometry_written",
                    "probe_written",
                }:
                    self.capture_accepted = True
                elif state == "failed":
                    error = str(status.get("error") or "capture_native_failed")
                    raise AdapterError(error)
                elif state == "completed":
                    self.capture_accepted = True
                    artifact = _file_identity(self.path)
                    probe = _file_identity(self.probe_path)
                    timing = _file_identity(self.timing_path)
                    if not all(item.get("exists") for item in (artifact, probe, timing)):
                        raise AdapterError("capture_output_incomplete")
                    artifact_value = self._read_json_file(self.path, "topology_artifact_unreadable")
                    probe_value = self._read_json_file(
                        self.probe_path, "topology_probe_report_missing_or_unreadable"
                    )
                    timing_value = self._read_json_file(
                        self.timing_path, "topology_probe_timing_missing_or_unreadable"
                    )
                    if any(
                        str(value.get("request_id") or "") != self.request_id
                        for value in (artifact_value, probe_value, timing_value)
                    ):
                        raise AdapterError("capture_output_request_id_mismatch")
                    return artifact, probe_value, timing_value, status
            if self.process_alive is not None:
                try:
                    if not self.process_alive():
                        raise AdapterError("capture_game_exited")
                except AdapterError:
                    raise
                except Exception:
                    # An unavailable liveness probe is not evidence of exit;
                    # the native request/status identity remains authoritative.
                    pass
            remaining = deadline - self.clock()
            if remaining <= 0.0:
                break
            self.sleeper(min(self.capture_poll_seconds, remaining))
        if self.capture_accepted:
            raise AdapterError("capture_in_progress_timeout")
        raise AdapterError("capture_request_not_accepted")

    def _new_file_identity(
        self,
        key: str,
        path: Path,
        baseline: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any] | None:
        current_stat = _file_stat_identity(path)
        before = baseline.get(key, {})
        if not current_stat.get("exists") or not _file_stat_is_new(current_stat, before):
            return None
        current = _file_identity(path)
        if not current.get("exists"):
            return None
        if self._capture_armed_at is not None:
            # File timestamps and the wall clock can differ by a small amount
            # across Windows filesystems; allow one second of skew while still
            # excluding artifacts written before this arm.
            if int(current.get("mtime_ns", 0)) < int((self._capture_armed_at - 1.0) * 1_000_000_000):
                return None
        # A new mtime/size is sufficient to detect an atomic native rewrite;
        # the digest is retained to distinguish an unchanged stale artifact
        # when a filesystem preserves coarse timestamps.
        if current.get("mtime_ns") <= int(before.get("mtime_ns", -1)) and current.get("sha256") == before.get("sha256"):
            return None
        return current

    @staticmethod
    def _read_json_file(path: Path, error: str) -> Mapping[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AdapterError(error) from exc
        if not isinstance(value, Mapping):
            raise AdapterError("topology_probe_report_not_object")
        return value

    @staticmethod
    def _read_json_optional(path: Path) -> Mapping[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, Mapping) else {}

    def _capture_status_sample(self) -> Mapping[str, Any] | None:
        value = self._read_json_optional(self.status_path)
        if str(value.get("state") or "") not in {"completed", "failed"}:
            # A native rename that lost to this poller's own read once left
            # the terminal status stranded in the .tmp (live 2026-10-01 at
            # Gongaga). Accept it only when it parses whole, names this
            # request and is terminal.
            stranded = self._read_json_optional(
                self.status_path.with_name(self.status_path.name + ".tmp")
            )
            if (
                str(stranded.get("request_id") or "") == self.request_id
                and str(stranded.get("state") or "") in {"completed", "failed"}
            ):
                value = stranded
        if not value or str(value.get("request_id") or "") != self.request_id:
            return None
        if not self.capture_status_timeline or value != self.capture_status_timeline[-1]:
            self.capture_status_timeline.append(dict(value))
        self.capture_status = dict(value)
        return value

    def load_current(self, observation: LiveObservation) -> TopologySession:
        if not self._capture_armed:
            raise AdapterError("topology_capture_not_armed_after_render_confirmation")
        try:
            return self._load_current(observation)
        finally:
            # Never leave a request or readiness token for the next session,
            # including a bounded wait timeout or a parse/validation error.
            self.disarm_capture()

    def _load_current(self, observation: LiveObservation) -> TopologySession:
        total_started = time.perf_counter()
        capture_wait_started = time.perf_counter()
        artifact_identity, probe, timing, capture_status = self._wait_for_fresh_capture()
        capture_wait_ms = (time.perf_counter() - capture_wait_started) * 1000.0
        stat = self.path.stat()
        age = self.clock() - stat.st_mtime
        if not math.isfinite(age) or age < -2.0 or age > self.max_capture_age_seconds:
            raise AdapterError("topology_artifact_not_fresh_for_current_session")
        read_started = time.perf_counter()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AdapterError("topology_artifact_unreadable") from exc
        if not isinstance(payload, Mapping):
            raise AdapterError("topology_artifact_not_object")
        if payload.get("request_id") != self.request_id:
            raise AdapterError("capture_output_request_id_mismatch")
        if probe.get("request_id") != self.request_id or timing.get("request_id") != self.request_id:
            raise AdapterError("capture_output_request_id_mismatch")
        if capture_status.get("request_id") != self.request_id:
            raise AdapterError("capture_output_request_id_mismatch")
        if payload.get("status") != "complete" and tile_excluded_partial(payload) is None:
            raise AdapterError("capture_output_incomplete")
        read_parse_ms = (time.perf_counter() - read_started) * 1000.0
        if payload.get("source") != "ue4ss_native_detour_geometry_dump":
            raise AdapterError("topology_artifact_source_not_proven")
        if payload.get("movement_sent") is not False:
            raise AdapterError("topology_artifact_not_read_only")
        if payload.get("coordinate_space") not in {"recast", "unreal"}:
            raise AdapterError("topology_artifact_coordinate_space_unknown")
        captured_position = payload.get("player_position")
        if not isinstance(captured_position, Mapping):
            proof = payload.get("topology_proof")
            captured_position = proof.get("player_position") if isinstance(proof, Mapping) else None
        try:
            captured_pose = _parse_point(captured_position)
        except AdapterError as exc:
            raise AdapterError("topology_capture_player_pose_missing") from exc
        capture_reference = observation.actor_origin or observation.position
        if _distance_m(captured_pose, capture_reference) > self.max_pose_delta_m:
            raise AdapterError("topology_capture_pose_not_current")

        probe_identity = probe.get("executable_identity") if probe else None
        probe_hash = str((probe_identity or {}).get("sha256") or "").upper()
        if probe_hash != self.expected_executable_sha256:
            raise AdapterError("topology_probe_executable_identity_invalid")
        if (
            self.expected_native_probe_sha256
            and str(probe.get("native_probe_sha256") or "").upper() != self.expected_native_probe_sha256
        ):
            raise AdapterError("topology_probe_native_build_identity_invalid")
        probe_world = str(probe.get("world") or "")
        if observation.world_id not in probe_world and observation.map_id not in probe_world:
            raise AdapterError("topology_probe_world_mismatch")
        if probe.get("native_geometry_requested") is not True:
            raise AdapterError("topology_probe_geometry_not_requested")
        if probe.get("native_geometry_marker_consumed") is not True:
            raise AdapterError("topology_probe_geometry_marker_not_consumed")
        if probe.get("native_geometry_dump_written") is not True:
            raise AdapterError("topology_probe_geometry_not_written")

        graph_started = time.perf_counter()
        try:
            graph = load_shipping_detour_topology(self.path)
        except (OSError, ValueError) as exc:
            raise AdapterError("topology_graph_load_failed") from exc
        graph_ms = (time.perf_counter() - graph_started) * 1000.0
        # The off-mesh links this capture offers, and the islands they bridge,
        # beside the capture (best effort; a route never waits on it).
        write_traversal_report(graph, self.path)
        fingerprint_started = time.perf_counter()
        fingerprint = hashlib.sha256(self.path.read_bytes()).hexdigest()
        fingerprint_ms = (time.perf_counter() - fingerprint_started) * 1000.0
        total_ms = (time.perf_counter() - total_started) * 1000.0
        try:
            static_fingerprint = (
                static_topology_fingerprint(payload)
                if isinstance(payload.get("tiles"), list) and payload.get("tiles")
                else None
            )
        except TopologyCacheError:
            # Native capture remains an explicit legacy acquisition path. A
            # malformed cache fingerprint must not change its existing graph
            # and movement safety behavior.
            static_fingerprint = None
        # The capture has no durable session id in the legacy probe schema.
        # Bind it to this live process/world only after the freshness and world
        # checks above; stale artifacts therefore cannot self-authorize.
        session_id = observation.session_id
        captured_at = _utc_from_mtime(stat.st_mtime)
        return TopologySession(
            graph=graph,
            artifact_path=str(self.path),
            fingerprint=fingerprint,
            session_id=session_id,
            world_id=observation.world_id,
            captured_at_utc=captured_at,
            current_session=True,
            movement_sent=payload.get("movement_sent") is True,
            payload_summary={
                "status": payload.get("status"),
                "reason_code": payload.get("reason_code"),
                "artifact_age_seconds": age,
                "captured_player_position": _point_dict(captured_pose),
                "pose_delta_m": _distance_m(captured_pose, capture_reference),
                "probe_world": probe_world,
                "probe_sha256": probe_hash,
                "native_probe_attempt": timing.get("probe_attempt"),
                "native_geometry_marker_consumed": probe.get("native_geometry_marker_consumed"),
                "native_timing_geometry_armed": timing.get("geometry_armed"),
                "capture_request_id": self.request_id,
                "capture_status": dict(capture_status),
                "capture_status_timeline": list(self.capture_status_timeline),
                "capture_artifact_identity": dict(artifact_identity),
                "capture_baseline_identity": {
                    key: dict(value) for key, value in self._capture_baseline.items()
                },
            },
            timings={
                "python_topology_capture_wait_ms": capture_wait_ms,
                "python_topology_capture_wait_budget_seconds": self.max_capture_wait_seconds,
                "python_topology_read_parse_ms": read_parse_ms,
                "python_topology_load_graph_ms": graph_ms,
                "python_topology_fingerprint_ms": fingerprint_ms,
                "python_topology_load_total_ms": total_ms,
            },
            static_fingerprint=static_fingerprint,
            source_decision="native_capture_required",
            artifact_sha256=fingerprint,
        )


class ValidatedTopologyCacheProvider:
    """Load a positive static-topology admission without native capture.

    A successful cache load creates a new ``TopologySession`` bound to the
    current observation.  The cached graph itself never carries an old PID,
    lifecycle session, capture pose or movement lease.
    """

    source_decisions = frozenset({
        "validated_cache_hit",
        "validated_cache_miss",
        "cache_invalid",
        "native_capture_required",
    })

    def __init__(
        self,
        manifest_path: str | Path = DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH,
        *,
        allow_scoped_partial: bool = False,
        expected_executable_sha256: str = LIVE_EXECUTABLE_SHA256,
        expected_native_probe_sha256: str | None = None,
        native_probe_path: str | Path | None = DEFAULT_TOPOLOGY_PROBE_DLL_PATH,
        expected_parser_schema_identity: str | None = None,
        parser_source_path: str | Path | None = None,
        expected_validator_source_sha256: str | None = None,
        validator_source_path: str | Path | None = None,
        expected_coordinate_transform_identity: str = "x=-recast.x,y=-recast.z,z=recast.y",
        max_current_snap_m: float = 0.75,
        max_current_vertical_m: float = 1.0,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if type(allow_scoped_partial) is not bool:
            raise ValueError("allow_scoped_partial must be a strict boolean")
        self.manifest_path = Path(manifest_path)
        self.configured_manifest_path = self.manifest_path
        # Set by the caller before each objective so the library can prefer a
        # capture that also covers the destination.
        self.selection_target: Vec3 | None = None
        self.library_decision: dict[str, Any] | None = None
        # Graphs built ahead of the first route (prewarm_graph), as futures
        # keyed by the artifact's raw SHA-256; building one took 10.3 s at
        # Kalm.
        self._graph_memo: dict[str, Future] = {}
        self._graph_memo_lock = threading.Lock()
        self.allow_scoped_partial = allow_scoped_partial
        self.expected_executable_sha256 = str(expected_executable_sha256).upper()
        self.expected_native_probe_sha256 = (
            str(expected_native_probe_sha256).upper()
            if expected_native_probe_sha256 is not None else None
        )
        self.native_probe_path = Path(native_probe_path) if native_probe_path is not None else None
        self.parser_source_path = (
            Path(parser_source_path)
            if parser_source_path is not None
            else Path(__file__).resolve().parent / "detour_route.py"
        )
        self.expected_parser_schema_identity = (
            str(expected_parser_schema_identity)
            if expected_parser_schema_identity is not None
            else TOPOLOGY_PARSER_SCHEMA_IDENTITY
        )
        self.expected_validator_source_sha256 = (
            str(expected_validator_source_sha256).upper()
            if expected_validator_source_sha256 is not None else None
        )
        self.validator_source_path = (
            Path(validator_source_path)
            if validator_source_path is not None
            else DEFAULT_VALIDATOR_SOURCE_PATH
        )
        self.expected_coordinate_transform_identity = expected_coordinate_transform_identity
        self.max_current_snap_m = float(max_current_snap_m)
        self.max_current_vertical_m = float(max_current_vertical_m)
        if (
            not math.isfinite(self.max_current_snap_m)
            or self.max_current_snap_m <= 0.0
            or not math.isfinite(self.max_current_vertical_m)
            or self.max_current_vertical_m <= 0.0
        ):
            raise ValueError("current topology resolution bounds must be finite and positive")
        self.clock = clock
        self.source_decision = "validated_cache_miss"
        self.decision_reason = "cache_manifest_missing"
        self.last_error: str | None = None
        self._cached_state: dict[str, Any] | None = None
        self._cache_invalidated = False

    def _reject(self, decision: str, reason: str) -> None:
        self.source_decision = decision
        self.decision_reason = reason
        self.last_error = reason
        raise AdapterError(reason)

    def _invalidate_cached_state(self, reason: str) -> None:
        """Fail closed after an admitted identity or artifact changes."""
        self._cached_state = None
        self._cache_invalidated = True
        self._reject("cache_invalid", reason)

    @staticmethod
    def _read_json(path: Path, reason: str) -> Mapping[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AdapterError(reason) from exc
        if not isinstance(value, Mapping):
            raise AdapterError(reason)
        return value

    @staticmethod
    def _read_bytes_with_digest(path: Path, reason: str) -> tuple[bytes, str]:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise AdapterError(reason) from exc
        return raw, hashlib.sha256(raw).hexdigest().upper()

    @staticmethod
    def _decode_json_bytes(raw: bytes, reason: str) -> Mapping[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError(reason) from exc
        if not isinstance(value, Mapping):
            raise AdapterError(reason)
        return value

    @staticmethod
    def _read_json_with_digest(path: Path, reason: str) -> tuple[Mapping[str, Any], str]:
        raw, digest = ValidatedTopologyCacheProvider._read_bytes_with_digest(path, reason)
        return ValidatedTopologyCacheProvider._decode_json_bytes(raw, reason), digest

    @staticmethod
    def _path_stat_identity(path: Path | None) -> Mapping[str, Any] | None:
        if path is None:
            return None
        return _strict_path_stat_identity(path)

    def _parser_source_identity(self) -> str | None:
        if not self.parser_source_path.is_file():
            return None
        try:
            return sha256_file(self.parser_source_path)
        except TopologyCacheError:
            return None

    def _validator_source_file_identity(self) -> str | None:
        if not self.validator_source_path.is_file():
            return None
        try:
            return validator_identity(self.validator_source_path)
        except TopologyCacheError:
            return None

    def _build_cache_key(
        self,
        *,
        manifest: Mapping[str, Any],
        manifest_sha256: str,
        artifact: Path,
        raw_sha256: str,
        static_fingerprint: str,
        native_probe_sha256: str,
        validator_source_sha256: str,
    ) -> dict[str, Any]:
        return {
            "manifest_path": self._path_stat_identity(self.manifest_path),
            "manifest_sha256": manifest_sha256,
            "artifact_path": self._path_stat_identity(artifact),
            "raw_artifact_sha256": raw_sha256,
            "static_topology_fingerprint": static_fingerprint,
            "cache_schema_version": manifest.get("cache_schema_version"),
            "fingerprint_schema": manifest.get("fingerprint_schema"),
            "executable_sha256": self.expected_executable_sha256,
            "native_probe_sha256": native_probe_sha256,
            "native_probe_path": self._path_stat_identity(self.native_probe_path),
            "parser_schema_identity": self.expected_parser_schema_identity,
            "parser_source_sha256": self._parser_source_identity(),
            "parser_source_path": self._path_stat_identity(self.parser_source_path),
            "validator_source_sha256": validator_source_sha256,
            "validator_source_file_sha256": self._validator_source_file_identity(),
            "validator_source_path": self._path_stat_identity(self.validator_source_path),
            "world_id": manifest.get("world_id"),
            "map_id": manifest.get("map_id"),
            "coordinate_space": manifest.get("coordinate_space"),
            "coordinate_transform_identity": manifest.get("coordinate_transform_identity"),
            "capture_request_id": manifest.get("capture_request_id"),
            "corridor_scope_identity": manifest.get("corridor_scope_identity"),
            "scoped_partial_opt_in": self.allow_scoped_partial,
        }

    @staticmethod
    def _scope_transition_signature(portal: Any) -> str:
        value = portal.to_dict() if callable(getattr(portal, "to_dict", None)) else portal
        return corridor_portal_identity(value)

    @classmethod
    def _restrict_graph_to_corridor_scope(
        cls,
        graph: DetourTopologyGraph,
        scope: Mapping[str, Any],
    ) -> tuple[DetourTopologyGraph, tuple[int, ...], tuple[str, ...]]:
        refs_raw = scope.get("polygon_refs")
        transitions_raw = scope.get("transitions")
        if not isinstance(refs_raw, list) or not isinstance(transitions_raw, list):
            raise AdapterError("cache_partial_scope_shape_invalid")
        try:
            refs = tuple(sorted({int(value) for value in refs_raw}))
        except (TypeError, ValueError) as exc:
            raise AdapterError("cache_partial_scope_polygon_refs_invalid") from exc
        if len(refs) != len(refs_raw) or not refs:
            raise AdapterError("cache_partial_scope_polygon_refs_invalid")
        allowed = set(refs)
        if any(ref not in graph.polygons or graph.polygons[ref].poly_type != DT_POLYTYPE_GROUND for ref in refs):
            raise AdapterError("cache_partial_scope_reference_not_resolved_ground")
        signatures: set[str] = set()
        for item in transitions_raw:
            if not isinstance(item, Mapping):
                raise AdapterError("cache_partial_scope_transition_invalid")
            try:
                signature = corridor_portal_identity(item)
                from_ref, to_ref, link_ref = int(item["from_ref"]), int(item["to_ref"]), int(item["link_ref"])
            except (KeyError, TypeError, ValueError, TopologyCacheError) as exc:
                raise AdapterError("cache_partial_scope_transition_invalid") from exc
            if from_ref not in allowed or to_ref not in allowed or link_ref != to_ref:
                raise AdapterError("cache_partial_scope_transition_outside_scope")
            signatures.add(signature)
        transitions = tuple(sorted(signatures))
        adjacency: dict[int, tuple[Any, ...]] = {}
        for source_ref in refs:
            selected = tuple(
                portal
                for portal in graph.adjacency.get(source_ref, ())
                if cls._scope_transition_signature(portal) in signatures
                and portal.to_reference in allowed
            )
            if selected:
                adjacency[source_ref] = selected
        found_signatures = {
            cls._scope_transition_signature(portal)
            for portals in adjacency.values()
            for portal in portals
        }
        if found_signatures != signatures:
            raise AdapterError("cache_partial_scope_transition_not_in_graph")
        restricted_graph = DetourTopologyGraph(
            {ref: graph.polygons[ref] for ref in refs},
            adjacency,
            snapshot_diagnostics={
                **dict(graph.snapshot_diagnostics),
                "partial_corridor_scope_applied": True,
                "partial_corridor_scope_polygon_count": len(refs),
                "partial_corridor_scope_transition_count": len(transitions),
            },
            portal_tolerance_cm=graph.portal_tolerance_cm,
        )
        return restricted_graph, refs, transitions

    def _load_cached_current(self, observation: LiveObservation) -> TopologySession:
        cached = self._cached_state
        if cached is None:
            raise AssertionError("cached topology state missing")
        started = self.clock()
        manifest_path_stat = self._path_stat_identity(self.manifest_path)
        if not manifest_path_stat or manifest_path_stat.get("exists") is not True:
            self._invalidate_cached_state("cache_manifest_missing_after_admission")
        try:
            manifest, manifest_sha256 = self._read_json_with_digest(
                self.manifest_path,
                "cache_manifest_unreadable",
            )
        except AdapterError as exc:
            self._invalidate_cached_state(str(exc))
        if manifest_path_stat != cached["cache_key"].get("manifest_path"):
            self._invalidate_cached_state("cache_manifest_stat_changed")
        if manifest_sha256 != cached["cache_key"].get("manifest_sha256"):
            self._invalidate_cached_state("cache_manifest_identity_changed")

        artifact = resolve_manifest_artifact(self.manifest_path, manifest)
        artifact_stat = self._path_stat_identity(artifact)
        previous_artifact_stat = cached["cache_key"].get("artifact_path")
        if not artifact_stat or artifact_stat.get("exists") is not True:
            self._invalidate_cached_state("cache_artifact_missing_after_admission")
        if cached.get("movement_sent") is not False:
            self._invalidate_cached_state("cache_movement_free_provenance_missing")
        if artifact_stat != previous_artifact_stat:
            try:
                current_raw = sha256_file(artifact)
            except TopologyCacheError:
                self._invalidate_cached_state("cache_artifact_unreadable_after_admission")
            if current_raw != cached["cache_key"].get("raw_artifact_sha256"):
                self._invalidate_cached_state("cache_artifact_hash_mismatch")
            self._invalidate_cached_state("cache_artifact_stat_changed")

        if manifest.get("world_id") != observation.world_id:
            self._invalidate_cached_state("cache_world_mismatch")
        if manifest.get("map_id") != observation.map_id:
            self._invalidate_cached_state("cache_map_mismatch")
        if cached.get("scope_identity") is not None and (
            type(observation.process_id) is not int
            or observation.process_id != cached.get("scope_process_id")
        ):
            self._invalidate_cached_state("cache_partial_scope_process_identity_mismatch")
        if str(observation.executable_sha256 or "").upper() != self.expected_executable_sha256:
            self._invalidate_cached_state("cache_current_executable_identity_mismatch")
        if not topology_parser_schema_compatible(
            str(manifest.get("parser_schema_identity") or ""),
            self.expected_parser_schema_identity,
        ):
            self._invalidate_cached_state("cache_parser_schema_identity_mismatch")
        if self.expected_parser_schema_identity != cached["cache_key"].get("parser_schema_identity"):
            self._invalidate_cached_state("cache_parser_identity_changed")

        if self._path_stat_identity(self.native_probe_path) != cached["cache_key"].get("native_probe_path"):
            self._invalidate_cached_state("cache_native_probe_path_stat_changed")
        if self._path_stat_identity(self.validator_source_path) != cached["cache_key"].get("validator_source_path"):
            self._invalidate_cached_state("cache_validator_path_stat_changed")
        if self._path_stat_identity(self.parser_source_path) != cached["cache_key"].get("parser_source_path"):
            self._invalidate_cached_state("cache_parser_path_stat_changed")
        current_parser_sha256 = self._parser_source_identity()
        if current_parser_sha256 != cached["cache_key"].get("parser_source_sha256"):
            self._invalidate_cached_state("cache_parser_identity_changed")
        current_probe_sha256 = (
            self.expected_native_probe_sha256
            or str(cached["cache_key"].get("native_probe_sha256") or "")
        ).upper()
        current_validator_sha256 = (
            self.expected_validator_source_sha256
            or str(cached["cache_key"].get("validator_source_sha256") or "")
        ).upper()
        if not native_probe_identities_match(manifest.get("native_probe_sha256"), current_probe_sha256):
            self._invalidate_cached_state("cache_native_probe_identity_mismatch")
        if str(manifest.get("validator_source_sha256") or "").upper() != current_validator_sha256:
            self._invalidate_cached_state("cache_validator_identity_mismatch")
        if self._validator_source_file_identity() != cached["cache_key"].get("validator_source_file_sha256"):
            self._invalidate_cached_state("cache_validator_source_identity_changed")

        current_resolution = cached["graph"].resolve_point(
            observation.position,
            max_snap_distance_m=self.max_current_snap_m,
            max_vertical_separation_m=self.max_current_vertical_m,
        )
        if not current_resolution.ok:
            self._invalidate_cached_state("cache_current_cloud_unresolvable")
        self.source_decision = "validated_cache_hit"
        self.decision_reason = "immutable_cache_reused_and_live_binding_revalidated"
        self.last_error = None
        elapsed_ms = (self.clock() - started) * 1000.0
        summary = dict(cached["payload_summary"])
        # Cold-admission timings describe the original load, not this reuse.
        summary.pop("python_topology_cache_admission_phases_ms", None)
        summary.update({
            "source_decision": self.source_decision,
            "decision_reason": self.decision_reason,
            "live_binding_session_id": observation.session_id,
            "live_binding_world_id": observation.world_id,
            "live_binding_map_id": observation.map_id,
            "current_cloud_resolution": current_resolution.to_dict(),
            "cache_reuse": True,
        })
        return TopologySession(
            graph=cached["graph"],
            artifact_path=str(artifact),
            fingerprint=cached["fingerprint"],
            static_fingerprint=cached["fingerprint"],
            session_id=observation.session_id,
            world_id=observation.world_id,
            map_id=observation.map_id,
            captured_at_utc=cached["captured_at_utc"],
            current_session=True,
            movement_sent=False,
            payload_summary=summary,
            timings={"python_topology_cache_reuse_ms": elapsed_ms},
            source_decision=self.source_decision,
            artifact_sha256=cached["raw_sha256"],
            scope_identity=cached.get("scope_identity"),
            scope_process_id=cached.get("scope_process_id"),
            scope_polygon_refs=tuple(cached.get("scope_polygon_refs") or ()),
            scope_transition_signatures=tuple(cached.get("scope_transition_signatures") or ()),
        )

    def _current_probe_identity(self) -> str:
        if self.expected_native_probe_sha256:
            return self.expected_native_probe_sha256
        if self.native_probe_path is None or not self.native_probe_path.is_file():
            raise TopologyCacheError("native_probe_identity_unavailable")
        return sha256_file(self.native_probe_path)

    def _selection_validator_identity(self) -> str | None:
        """The validator identity library selection must match; None (no filter) when unreadable,
        leaving load_current to refuse with its own reason."""
        try:
            return self._current_validator_identity().upper()
        except TopologyCacheError:
            return None

    def _current_validator_identity(self) -> str:
        if self.expected_validator_source_sha256:
            return self.expected_validator_source_sha256
        if not self.validator_source_path.is_file():
            raise TopologyCacheError("validator_source_identity_unavailable")
        return validator_identity(self.validator_source_path)

    def prewarm_graph(self, position: tuple[float, ...] | None = None,
                      world_id: str = "EndGame", map_id: str = "EndGame",
                      target: tuple[float, ...] | None = None) -> dict[str, Any]:
        """Build the graph the first route will need, before it is asked for.

        Picks the library capture covering ``position`` (or the configured
        slot), checks its bytes against the manifest and memoizes the graph.
        Nothing here authorizes anything: load_current still verifies every
        identity and only reuses a graph built from the same bytes. A route
        that asks while the build runs waits for it rather than building
        the same graph again.
        """
        selected = None
        if position is not None:
            selected, _ = select_library_manifest(
                self.configured_manifest_path.parent, world_id=world_id, map_id=map_id,
                executable_sha256=self.expected_executable_sha256, position=position, target=target,
                validator_source_sha256=self._selection_validator_identity())
        manifest_path = selected or self.configured_manifest_path
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_sha = str(manifest.get("raw_artifact_sha256") or "").upper()
        cached = self._cached_state
        with self._graph_memo_lock:
            if (manifest_path == self.manifest_path and isinstance(cached, dict)
                    and str(cached.get("raw_sha256") or "").upper() == expected_sha):
                return {"state": "current", "manifest": manifest_path.name}
            if expected_sha in self._graph_memo:
                pending = self._graph_memo[expected_sha]
                return {"state": "ready" if pending.done() else "building", "manifest": manifest_path.name}
            future: Future = Future()
            self._graph_memo[expected_sha] = future
            while len(self._graph_memo) > MAX_PREWARMED_GRAPHS:
                self._graph_memo.pop(next(iter(self._graph_memo)))
        started = time.perf_counter()
        try:
            raw, raw_sha = self._read_bytes_with_digest(
                resolve_manifest_artifact(manifest_path, manifest), "cache_artifact_unreadable")
            if raw_sha != expected_sha:
                raise TopologyCacheError("cache_artifact_hash_mismatch")
            payload = self._decode_json_bytes(raw, "cache_artifact_unreadable")
            cached_static_fingerprint(manifest_path.parent, raw_sha, lambda: static_topology_fingerprint(payload))
            graph = DetourTopologyGraph.from_dict(payload)
        except Exception as exc:
            future.set_exception(exc)
            with self._graph_memo_lock:
                if self._graph_memo.get(expected_sha) is future:
                    del self._graph_memo[expected_sha]
            if "cache_artifact_hash_mismatch" in str(exc):
                return {"state": "skipped", "reason": "cache_artifact_hash_mismatch"}
            raise
        future.set_result(graph)
        return {"state": "ready", "manifest": manifest_path.name, "polygons": len(graph.polygons),
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0)}

    def prewarmed_graphs(self, wait_seconds: float = 0.0) -> dict[str, Any]:
        """The graphs prewarm_graph has built, by raw SHA-256, waiting up to
        ``wait_seconds`` for builds still running."""
        with self._graph_memo_lock:
            pending = dict(self._graph_memo)
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        graphs = {}
        for raw_sha, future in pending.items():
            try:
                graphs[raw_sha] = future.result(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                continue
        return graphs

    def graph_prewarm_pending(self) -> bool:
        """Whether prewarm_graph is still building a graph."""
        with self._graph_memo_lock:
            return any(not future.done() for future in self._graph_memo.values())

    def _select_manifest(self, observation: LiveObservation) -> None:
        """Pick the library capture covering Cloud; keep the slot without one."""
        target = self.selection_target
        try:
            selected, decision = select_library_manifest(
                self.configured_manifest_path.parent,
                world_id=observation.world_id,
                map_id=observation.map_id,
                executable_sha256=str(observation.executable_sha256 or self.expected_executable_sha256),
                position=(observation.position.x, observation.position.y, observation.position.z),
                target=(target.x, target.y, target.z) if target is not None else None,
                native_probe_sha256=self._current_probe_identity(),
                validator_source_sha256=self._selection_validator_identity(),
            )
        except TopologyCacheError as exc:
            self._reject("cache_invalid", str(exc))
        self.library_decision = decision
        if selected is None and decision.get("reason") == "topology_library_no_capture_covers_current_position":
            self._reject("validated_cache_miss", decision["reason"])
        chosen = selected or self.configured_manifest_path
        if chosen != self.manifest_path:
            self.manifest_path = chosen
            self._cached_state = None

    def load_current(self, observation: LiveObservation) -> TopologySession:
        started = self.clock()
        if self._cache_invalidated:
            self._reject("cache_invalid", "validated_topology_cache_previous_identity_change")
        self._select_manifest(observation)
        if self._cached_state is not None:
            return self._load_cached_current(observation)
        if not self.manifest_path.is_file():
            self._reject("validated_cache_miss", "validated_topology_cache_manifest_missing")
        phases_ms: dict[str, float] = {}
        phase_started = started

        def lap(name: str) -> None:
            nonlocal phase_started
            now = self.clock()
            phases_ms[name] = round((now - phase_started) * 1000.0, 3)
            phase_started = now

        lap("manifest_selection")
        try:
            manifest, manifest_sha256 = self._read_json_with_digest(
                self.manifest_path,
                "cache_manifest_unreadable",
            )
            lap("manifest_read_hash")
            if manifest.get("cache_schema_version") != TOPOLOGY_CACHE_SCHEMA_VERSION:
                self._reject("cache_invalid", "cache_schema_incompatible")
            if manifest.get("fingerprint_schema") != STATIC_TOPOLOGY_FINGERPRINT_SCHEMA:
                self._reject("cache_invalid", "cache_fingerprint_schema_incompatible")
            if not topology_parser_schema_compatible(
                str(manifest.get("parser_schema_identity") or ""),
                self.expected_parser_schema_identity,
            ):
                self._reject("cache_invalid", "cache_parser_schema_identity_mismatch")
            artifact = resolve_manifest_artifact(self.manifest_path, manifest)
            if not artifact.is_file():
                self._reject("cache_invalid", "cache_artifact_missing")
            # Hash and retain exactly the bytes that will be decoded below. This
            # removes a second full artifact filesystem pass while strengthening
            # the admission binding: the graph is built from the same byte
            # snapshot whose SHA-256 was compared with the immutable manifest.
            raw_artifact, raw_sha = self._read_bytes_with_digest(
                artifact, "cache_artifact_unreadable"
            )
            if raw_sha != str(manifest.get("raw_artifact_sha256") or "").upper():
                self._reject("cache_invalid", "cache_artifact_hash_mismatch")
            lap("raw_artifact_read_hash")
            if str(manifest.get("executable_sha256") or "").upper() != self.expected_executable_sha256:
                self._reject("cache_invalid", "cache_executable_identity_mismatch")
            observed_executable = str(observation.executable_sha256 or "").upper()
            if observed_executable != self.expected_executable_sha256:
                self._reject("cache_invalid", "cache_current_executable_identity_mismatch")
            if str(manifest.get("world_id") or "") != observation.world_id:
                self._reject("cache_invalid", "cache_world_mismatch")
            if str(manifest.get("map_id") or "") != observation.map_id:
                self._reject("cache_invalid", "cache_map_mismatch")
            if str(manifest.get("coordinate_space") or "") != "recast":
                self._reject("cache_invalid", "cache_coordinate_space_mismatch")
            if str(manifest.get("coordinate_transform_identity") or "") != self.expected_coordinate_transform_identity:
                self._reject("cache_invalid", "cache_coordinate_transform_mismatch")
            if not native_probe_identities_match(manifest.get("native_probe_sha256"), self._current_probe_identity()):
                self._reject("cache_invalid", "cache_native_probe_identity_mismatch")
            if str(manifest.get("validator_source_sha256") or "").upper() != self._current_validator_identity():
                self._reject("cache_invalid", "cache_validator_identity_mismatch")
            validation = manifest.get("validation_result")
            if (
                not isinstance(validation, Mapping)
                or validation.get("valid") is not True
                or list(validation.get("errors") or [])
                or list(validation.get("warnings") or [])
            ):
                self._reject("cache_invalid", "cache_independent_validation_not_positive")
            provenance = manifest.get("provenance")
            capture = manifest.get("capture_provenance")
            if (
                not isinstance(provenance, Mapping)
                or provenance.get("independent_route_validation") is not True
                or not isinstance(provenance.get("capture_request_id"), str)
                or not provenance.get("capture_request_id")
                or str(manifest.get("capture_request_id") or "") != str(provenance.get("capture_request_id"))
                or not isinstance(capture, Mapping)
            ):
                self._reject("cache_invalid", "cache_provenance_not_positive")
            lap("identity_and_provenance")
            payload = self._decode_json_bytes(raw_artifact, "cache_artifact_unreadable")
            if capture.get("topology_proof_status") != "proved" and not capture_proof_locality_miss(payload):
                self._reject("cache_invalid", "cache_provenance_not_positive")
            if payload.get("source") != "ue4ss_native_detour_geometry_dump" or payload.get("movement_sent") is not False:
                self._reject("cache_invalid", "cache_artifact_not_read_only_geometry")
            lap("json_decode_and_capture_proof")
            fingerprint = cached_static_fingerprint(
                self.manifest_path.parent, raw_sha, lambda: static_topology_fingerprint(payload))
            if fingerprint != str(manifest.get("static_topology_fingerprint") or "").upper():
                self._reject("cache_invalid", "cache_static_fingerprint_mismatch")
            lap("static_semantic_fingerprint")
            with self._graph_memo_lock:
                pending = self._graph_memo.pop(raw_sha, None)
            graph = None
            if pending is not None:
                try:
                    graph = pending.result(timeout=GRAPH_PREWARM_WAIT_SECONDS)
                except Exception:
                    graph = None
            if graph is None:
                try:
                    graph = DetourTopologyGraph.from_dict(payload)
                except (OSError, ValueError) as exc:
                    self._reject("cache_invalid", "cache_graph_load_failed")
                    raise AssertionError("unreachable") from exc
            if not graph.polygons:
                self._reject("cache_invalid", "cache_graph_empty")
            if graph.off_mesh_links and not report_path_for(artifact).exists():
                write_traversal_report(graph, artifact)
            lap("planner_graph_and_spatial_index")
            scope_identity: str | None = None
            scope_polygon_refs: tuple[int, ...] = ()
            scope_transition_signatures: tuple[str, ...] = ()
            if payload.get("status") == "complete":
                if manifest.get("admission_mode") == PARTIAL_CORRIDOR_SCOPE_SCHEMA or manifest.get("corridor_scope") is not None:
                    self._reject("cache_invalid", "cache_complete_artifact_has_partial_scope")
            elif (
                payload.get("status") == "partial"
                and manifest.get("admission_mode") == TILE_EXCLUDED_PARTIAL_SCHEMA
            ):
                if tile_excluded_partial(payload) is None or manifest.get("corridor_scope") is not None:
                    self._reject("cache_invalid", "cache_tile_excluded_partial_not_positive")
            elif payload.get("status") == "partial":
                if not self.allow_scoped_partial:
                    self._reject("cache_invalid", "cache_scoped_partial_requires_feature_opt_in")
                if manifest.get("admission_mode") != PARTIAL_CORRIDOR_SCOPE_SCHEMA:
                    self._reject("cache_invalid", "cache_partial_scope_admission_missing")
                proof = payload.get("topology_proof")
                proof_status = proof.get("status") if isinstance(proof, Mapping) else None
                scope_process_id = capture.get("process_id") if isinstance(capture, Mapping) else None
                if (
                    type(scope_process_id) is not int
                    or scope_process_id <= 0
                    or type(observation.process_id) is not int
                    or observation.process_id != scope_process_id
                ):
                    self._reject("cache_invalid", "cache_partial_scope_process_identity_mismatch")
                if (
                    proof_status != "proved"
                    or capture.get("native_status") != "partial"
                    or capture.get("topology_proof_status") != "proved"
                    or str(capture.get("capture_request_id") or "") != str(manifest.get("capture_request_id") or "")
                    or payload.get("request_id") != manifest.get("capture_request_id")
                    or payload.get("capture_session_id") != capture.get("capture_session_id")
                    or payload.get("capture_world_id") != manifest.get("world_id")
                    or payload.get("capture_map_id") != manifest.get("map_id")
                    or str(payload.get("native_probe_sha256") or "").upper() != str(manifest.get("native_probe_sha256") or "").upper()
                    or payload.get("capture_purpose") != "static_topology_cache_seed"
                    or str(capture.get("executable_sha256") or "").upper() != self.expected_executable_sha256
                    or str(capture.get("native_probe_sha256") or "").upper() != str(manifest.get("native_probe_sha256") or "").upper()
                ):
                    self._reject("cache_invalid", "cache_partial_scope_proof_not_positive")
                try:
                    scope = verify_validated_corridor_scope(
                        graph,
                        manifest.get("corridor_scope"),
                        raw_artifact_sha256=raw_sha,
                        static_fingerprint=fingerprint,
                        native_status=str(payload.get("status") or ""),
                        topology_proof_status=str(proof_status or ""),
                        executable_sha256=str(manifest.get("executable_sha256") or "").upper(),
                        native_probe_sha256=str(manifest.get("native_probe_sha256") or "").upper(),
                        capture_request_id=str(manifest.get("capture_request_id") or ""),
                        capture_session_id=str(capture.get("capture_session_id") or ""),
                        process_id=scope_process_id,
                        world_id=str(manifest.get("world_id") or ""),
                        map_id=str(manifest.get("map_id") or ""),
                        validator_source_sha256=str(manifest.get("validator_source_sha256") or "").upper(),
                    )
                except TopologyCacheError as exc:
                    self._reject("cache_invalid", str(exc))
                scope_identity = str(scope.get("scope_identity") or "").upper()
                if scope_identity != str(manifest.get("corridor_scope_identity") or "").upper():
                    self._reject("cache_invalid", "cache_partial_scope_identity_mismatch")
                try:
                    graph, scope_polygon_refs, scope_transition_signatures = self._restrict_graph_to_corridor_scope(
                        graph,
                        scope,
                    )
                except AdapterError as exc:
                    self._reject("cache_invalid", str(exc))
            else:
                self._reject("cache_invalid", "cache_artifact_not_complete")
            lap("admission_scope")
            current_resolution = graph.resolve_point(
                observation.position,
                max_snap_distance_m=self.max_current_snap_m,
                max_vertical_separation_m=self.max_current_vertical_m,
            )
            if not current_resolution.ok:
                self._reject("cache_invalid", "cache_current_cloud_unresolvable")
            lap("first_point_resolution")
            self.source_decision = "validated_cache_hit"
            self.decision_reason = "manifest_and_graph_validation_passed"
            self.last_error = None
            elapsed_ms = (self.clock() - started) * 1000.0
            stat = artifact.stat()
            cache_key = self._build_cache_key(
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                artifact=artifact,
                raw_sha256=raw_sha,
                static_fingerprint=fingerprint,
                native_probe_sha256=str(manifest.get("native_probe_sha256") or "").upper(),
                validator_source_sha256=str(manifest.get("validator_source_sha256") or "").upper(),
            )
            lap("cache_key_binding")
            payload_summary = {
                "python_topology_cache_admission_phases_ms": phases_ms,
                "source_decision": self.source_decision,
                "decision_reason": self.decision_reason,
                "cache_manifest_path": str(self.manifest_path),
                "topology_library": self.library_decision,
                "cache_manifest_identity": cache_key,
                "capture_pose_is_provenance_only": True,
                "artifact_mtime_utc": _utc_from_mtime(stat.st_mtime),
                "live_binding_session_id": observation.session_id,
                "live_binding_world_id": observation.world_id,
                "live_binding_map_id": observation.map_id,
                "current_cloud_resolution": current_resolution.to_dict(),
                "corridor_scope_identity": scope_identity,
                "corridor_scope_polygon_count": len(scope_polygon_refs),
                "corridor_scope_transition_count": len(scope_transition_signatures),
                "corridor_scope_process_id": scope_process_id if scope_identity is not None else None,
            }
            self._cached_state = {
                "cache_key": cache_key,
                "graph": graph,
                "fingerprint": fingerprint,
                "raw_sha256": raw_sha,
                "movement_sent": False,
                "captured_at_utc": str(manifest.get("created_at_utc") or "") or None,
                "payload_summary": payload_summary,
                "scope_identity": scope_identity,
                "scope_process_id": scope_process_id if scope_identity is not None else None,
                "scope_polygon_refs": scope_polygon_refs,
                "scope_transition_signatures": scope_transition_signatures,
            }
            return TopologySession(
                graph=graph,
                artifact_path=str(artifact),
                fingerprint=fingerprint,
                static_fingerprint=fingerprint,
                session_id=observation.session_id,
                world_id=observation.world_id,
                map_id=observation.map_id,
                captured_at_utc=str(manifest.get("created_at_utc") or "") or None,
                current_session=True,
                movement_sent=False,
                payload_summary=payload_summary,
                timings={"python_topology_cache_load_ms": elapsed_ms},
                source_decision=self.source_decision,
                artifact_sha256=raw_sha,
                scope_identity=scope_identity,
                scope_process_id=scope_process_id if scope_identity is not None else None,
                scope_polygon_refs=scope_polygon_refs,
                scope_transition_signatures=scope_transition_signatures,
            )
        except TopologyCacheError as exc:
            self._reject("cache_invalid", str(exc))
        except AdapterError:
            raise
        except Exception as exc:
            self._reject("cache_invalid", f"cache_unexpected_error:{type(exc).__name__}")
        raise AssertionError("unreachable")
class AgentServiceTelemetrySource:
    """Bridge existing AgentService telemetry/readiness/lease surfaces."""

    def __init__(self, service: Any, lifecycle: Any, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.service = service
        self.lifecycle = lifecycle
        self.monotonic = monotonic

    def verify_executable_identity(self, expected_sha256: str = LIVE_EXECUTABLE_SHA256) -> Mapping[str, Any]:
        """Lazily verify and persist the bound executable identity.

        Attached sessions intentionally start with no executable hash.  The
        adapter calls this only after fresh settled telemetry and positive raw
        rendered-gameplay evidence, so startup polling never hashes the large
        game executable repeatedly.
        """
        verifier = getattr(self.lifecycle, "verify_executable_identity", None)
        if not callable(verifier):
            return {
                "verified": False,
                "reason": "executable_identity_verifier_unavailable",
                "sha256": None,
            }
        result = verifier(expected_sha256.upper())
        return dict(result) if isinstance(result, Mapping) else {
            "verified": False,
            "reason": "executable_identity_verifier_invalid",
            "sha256": None,
        }

    def wait_for_settled_gameplay(
        self,
        timeout_seconds: float = 30.0,
        max_telemetry_age_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        """Re-establish the service's advancing settled-gameplay gate.

        Render capture and lazy executable verification can outlast the
        service's short settled-readiness cache.  This explicit, read-only
        handoff lets the adapter reacquire genuine advancing samples before
        topology capture instead of treating an expired cache as gameplay.
        """
        result = self.service.wait_for_settled_gameplay(
            float(timeout_seconds),
            max_telemetry_age_seconds=max_telemetry_age_seconds,
        )
        return dict(result) if isinstance(result, Mapping) else {"result": result}

    def observe(self) -> LiveObservation:
        # Live 2026-10-20 (Mt. Nibel cave mouth): stepping over a plank left
        # one sample without Cloud's pose, which stopped every walk there;
        # the next samples had it. A pose missing for longer still stops.
        for attempt in range(POSE_GAP_RETRIES + 1):
            try:
                with game_process_snapshot():
                    return self._observe_current()
            except AdapterError as exc:
                if str(exc) != "cloud_pose_or_actor_yaw_missing" or attempt == POSE_GAP_RETRIES:
                    raise
                time.sleep(POSE_GAP_RETRY_SECONDS)
        raise AdapterError("cloud_pose_or_actor_yaw_missing")

    def _cutscene_playing(self) -> bool:
        """A cinema plays (the exporter's cutscene.state). Unknown is not a
        cutscene: an exporter without cutscene detection must not block every
        route."""
        reader = getattr(self.service, "cutscene_state", None)
        if reader is None:
            return False
        try:
            return reader().get("active") is True
        except Exception:
            return False

    def _observe_current(self) -> LiveObservation:
        state = self.service.telemetry.observe()
        health = self.service.session_health(state=state)
        if health.get("health_sample_sequence") != int(state.sequence):
            raise AdapterError("telemetry_health_sequence_mismatch")
        input_status = self.service.input_status(state=state)
        lifecycle_status = self.lifecycle.status()
        player = state.player
        if player is None or player.position is None or player.rotation is None:
            raise AdapterError("cloud_pose_or_actor_yaw_missing")
        camera_rotation = state.camera_rotation
        telemetry_age_seconds = float(state.age_seconds())
        process_ids = self.service._environment.game_process_ids()
        run_context = lifecycle_status.get("run_context") or {}
        run_id = str(run_context.get("run_id") or "")
        if not run_id:
            raise AdapterError("live_session_identity_missing")
        try:
            bound_process_id = int(run_context.get("pid"))
        except (TypeError, ValueError):
            bound_process_id = None
        process_id = bound_process_id if bound_process_id in process_ids else None
        executable_hash = str(run_context.get("executable_sha256") or "").upper()
        backend_status = input_status.get("backend_status") or {}
        session = input_status.get("control_session") or {}
        dynamic_now_ms: int | None = None
        try:
            dynamic_timestamp = datetime.fromisoformat(
                str(getattr(state, "sampled_at_utc", None) or state.timestamp_utc).replace("Z", "+00:00")
            )
            dynamic_now_ms = int(dynamic_timestamp.timestamp() * 1000.0 + max(0.0, telemetry_age_seconds) * 1000.0)
        except (TypeError, ValueError, OverflowError):
            dynamic_now_ms = None
        dynamic_snapshot = snapshot_from_game_state(
            state,
            source_identity="agent_service.telemetry.nearby_actors",
            now_ms=dynamic_now_ms,
        )
        locomotion = getattr(player, "locomotion", None)
        navigation_position = locomotion.feet_position if locomotion is not None else player.position
        return LiveObservation(
            monotonic_t=self.monotonic(),
            timestamp_utc=state.timestamp_utc,
            sequence=int(state.sequence),
            position=navigation_position,
            position_reference=locomotion.reference if locomotion is not None else "actor_origin",
            actor_origin=player.position,
            grounded=locomotion.grounded if locomotion is not None else None,
            velocity_cm_s=locomotion.velocity_cm_s if locomotion is not None else None,
            actor_yaw=float(player.rotation.yaw),
            camera_yaw=(float(camera_rotation.yaw) if camera_rotation is not None else None),
            actor_id=str(player.actor_id or ""),
            session_id=run_id,
            world_id=str(state.map_id or ""),
            map_id=str(state.map_id or ""),
            # The reviewed runtime commonly sees both the game executable and
            # its launcher.  Require the lifecycle-bound game PID to be among
            # the allow-listed processes instead of incorrectly rejecting that
            # known two-process identity.
            process_valid=health.get("process_exists") is True and process_id is not None,
            executable_valid=executable_hash == LIVE_EXECUTABLE_SHA256,
            controllable=bool(
                health.get("gameplay_ready") is True
                and health.get("target_clear") is True
                and not state.in_combat
                and (locomotion is None or locomotion.grounded is not False)
            ),
            # gameplay_settling means the readiness proof is being
            # established/re-established.  The current telemetry producer
            # exposes no independent loading/streaming flag, so do not turn
            # that proof state into synthetic loading evidence.
            loading=health.get("loading_evidence") is True,
            in_menu=bool(state.in_menu or state.paused),
            in_cutscene=self._cutscene_playing(),
            in_combat=bool(state.in_combat),
            in_interaction=bool(state.nearby_interactable and state.nearby_interactable.usable),
            lease_active=session.get("active") is True,
            actuator_neutral=backend_status.get("stick_state") == "neutral",
            telemetry_age_seconds=telemetry_age_seconds,
            topology_session_id=run_id,
            process_id=process_id,
            executable_sha256=executable_hash or None,
            raw={
                "health": health,
                "input_status": input_status,
                "navigation": copy.deepcopy(getattr(state, "navigation", None)),
                # This is observational context only.  Static learning does
                # not infer a blocker from actor presence; callers with a
                # proven dynamic classification must pass it explicitly to
                # the separate dynamic-obstacle contract.
                "nearby_actors": [asdict(item) for item in getattr(state, "nearby_actors", ())],
            },
            telemetry_gameplay_ready=health.get("gameplay_ready") is True,
            dynamic_obstacles=dynamic_snapshot,
        )


class AgentServiceRenderEvidenceSource:
    """Use capture plus a strictly bound raw or telemetry-backed render proof."""

    _MIN_WHOLE_NONBLACK = 0.30

    def __init__(self, service: Any) -> None:
        self.service = service

    def observe(self, observation: LiveObservation) -> RenderedGameplayEvidence:
        try:
            capture = self.service.capture_game_window()
        except Exception as exc:
            return RenderedGameplayEvidence(
                ready=False,
                reason="render_capture_failed",
                capture={"ok": False, "error": str(exc)},
            )
        if not isinstance(capture, Mapping) or capture.get("ok") is not True:
            return RenderedGameplayEvidence(
                ready=False,
                reason="render_capture_not_confirmed",
                capture=dict(capture) if isinstance(capture, Mapping) else {"value": repr(capture)},
            )
        frame = capture.get("startup_frame")
        if not isinstance(frame, Mapping):
            return RenderedGameplayEvidence(
                ready=False,
                reason="render_boundary_classifier_missing",
                capture=dict(capture),
            )
        classifier_label = frame.get("render_classifier_label")
        if classifier_label is None and "telemetry_override" not in frame:
            classifier_label = frame.get("label")
        classifier_label = str(classifier_label) if classifier_label is not None else None
        metrics = frame.get("metrics")
        metrics = dict(metrics) if isinstance(metrics, Mapping) else {}
        try:
            whole_nonblack = float(metrics.get("whole_nonblack"))
        except (TypeError, ValueError):
            whole_nonblack = math.nan
        pid = capture.get("pid")
        if not _same_process_id(pid, observation.process_id):
            return RenderedGameplayEvidence(
                ready=False,
                reason="render_process_identity_mismatch",
                captured_at_utc=str(capture.get("captured_at_utc") or "") or None,
                classifier_label=classifier_label,
                metrics=metrics,
                capture=dict(capture),
            )
        try:
            capture_sequence = int(capture.get("telemetry_sequence"))
        except (TypeError, ValueError):
            capture_sequence = None
        if capture_sequence is not None and capture_sequence < getattr(observation, "sequence", 0):
            return RenderedGameplayEvidence(ready=False, reason="render_sequence_regressed",
                classifier_label=classifier_label, metrics=metrics, capture=dict(capture))
        telemetry_candidate = RenderedGameplayEvidence(
            ready=True,
            reason=_TELEMETRY_BACKED_GAMEPLAY_REASON,
            captured_at_utc=str(capture.get("captured_at_utc") or "") or None,
            classifier_label=classifier_label,
            metrics=metrics,
            capture=dict(capture),
        )
        if _telemetry_backed_render_evidence_confirms_gameplay(
            observation,
            telemetry_candidate,
        ):
            return telemetry_candidate
        if classifier_label != "gameplay":
            reason = "render_classifier_non_gameplay"
        elif not math.isfinite(whole_nonblack) or whole_nonblack < self._MIN_WHOLE_NONBLACK:
            reason = "render_black_or_loading"
        else:
            reason = "rendered_gameplay_confirmed"
        return RenderedGameplayEvidence(
            ready=reason == "rendered_gameplay_confirmed",
            reason=reason,
            captured_at_utc=str(capture.get("captured_at_utc") or "") or None,
            classifier_label=classifier_label,
            metrics=metrics,
            capture=dict(capture),
        )


class IndependentJsonValidator:
    """Lazy wrapper over the standalone validator; never imports the planner."""

    def __init__(
        self,
        snapshot_path: str | Path | None = None,
        corpus_path: str | Path | None = None,
        *,
        require_topology_binding: bool = False,
    ) -> None:
        self.snapshot_path = Path(snapshot_path) if snapshot_path is not None else None
        self.corpus_path = Path(corpus_path) if corpus_path is not None else None
        self.require_topology_binding = bool(require_topology_binding)
        self._validator: Any | None = None
        self.timing: dict[str, float] = {}
        self._bound_topology_session_id: str | None = None
        self._bound_topology_evidence: dict[str, Any] = {}
        self._bound_artifact_stat_identity: Mapping[str, Any] | None = None
        self._bound_validator_source_path: Path | None = None
        self._bound_validator_source_stat_identity: Mapping[str, Any] | None = None
        self._bound_validator_source_sha256: str | None = None
        self._validator_cache_key: tuple[Any, ...] | None = None
        self._gimmick_module: Any | None = None
        self._gimmick_evidence: Any | None = None

    @property
    def binding_evidence(self) -> Mapping[str, Any]:
        return dict(self._bound_topology_evidence)

    @staticmethod
    def _artifact_payload(path: Path) -> Mapping[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AdapterError("validator_topology_artifact_missing") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise AdapterError("validator_topology_artifact_unreadable") from exc
        if not isinstance(value, Mapping):
            raise AdapterError("validator_topology_artifact_not_object")
        return value

    @classmethod
    def _artifact_evidence(cls, path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise AdapterError("validator_topology_artifact_missing")
        payload = cls._artifact_payload(path)
        try:
            raw_sha256 = sha256_file(path)
        except OSError as exc:
            raise AdapterError("validator_topology_artifact_missing") from exc
        static_fingerprint: str | None = None
        if isinstance(payload.get("tiles"), list) and payload.get("tiles"):
            try:
                static_fingerprint = static_topology_fingerprint(payload)
            except TopologyCacheError as exc:
                raise AdapterError("validator_topology_fingerprint_unavailable") from exc
        return {
            "artifact_path": str(path.resolve()),
            "raw_artifact_sha256": raw_sha256,
            "static_topology_fingerprint": static_fingerprint,
            "world_id": payload.get("world_id") or payload.get("world"),
            "map_id": payload.get("map_id") or payload.get("map"),
        }

    def bind_topology_session(self, topology: TopologySession) -> Mapping[str, Any]:
        """Bind this validator to the immutable artifact used by the planner.

        The standalone validator remains algorithmically independent.  Only its
        evidence input is bound here, after the planner's topology provider has
        selected and verified the live topology session.
        """
        path = Path(topology.artifact_path)
        expected_raw = str(topology.artifact_sha256 or "").upper() or None
        summary = topology.payload_summary if isinstance(topology.payload_summary, Mapping) else {}
        cache_identity = summary.get("cache_manifest_identity")
        if not isinstance(cache_identity, Mapping):
            cache_identity = {}
        capture_identity = summary.get("capture_artifact_identity")
        if not isinstance(capture_identity, Mapping):
            capture_identity = {}
        expected_raw = expected_raw or str(
            cache_identity.get("raw_artifact_sha256")
            or capture_identity.get("sha256")
            or ""
        ).upper() or None
        expected_static = str(topology.static_fingerprint or "").upper() or None
        artifact_stat_identity = _path_stat_identity_for_validator(path)
        cached_artifact_path = cache_identity.get("artifact_path")
        evidence: dict[str, Any]
        if (
            isinstance(cached_artifact_path, Mapping)
            and cached_artifact_path == artifact_stat_identity
            and str(cache_identity.get("raw_artifact_sha256") or "").upper() == (expected_raw or "")
            and str(cache_identity.get("static_topology_fingerprint") or "").upper() == (expected_static or "")
        ):
            evidence = {
                "artifact_path": str(path.resolve()),
                "raw_artifact_sha256": str(cache_identity.get("raw_artifact_sha256")).upper(),
                "static_topology_fingerprint": str(cache_identity.get("static_topology_fingerprint")).upper(),
                "world_id": cache_identity.get("world_id"),
                "map_id": cache_identity.get("map_id"),
            }
        else:
            evidence = self._artifact_evidence(path)
        actual_raw = str(evidence["raw_artifact_sha256"]).upper()
        if expected_raw is not None and actual_raw != expected_raw:
            raise AdapterError("validator_topology_artifact_hash_mismatch")
        actual_static = str(evidence.get("static_topology_fingerprint") or "").upper() or None
        if expected_static is not None and actual_static != expected_static:
            raise AdapterError("validator_topology_fingerprint_mismatch")
        for key in ("world_id", "map_id"):
            artifact_value = str(evidence.get(key) or "")
            session_value = str(getattr(topology, key, "") or "")
            if artifact_value and session_value and artifact_value != session_value:
                raise AdapterError(f"validator_topology_{key}_mismatch")
        validation_path = self._validation_source_path()
        validator_source_stat_identity = _path_stat_identity_for_validator(validation_path)
        try:
            validator_source_sha256 = (
                validator_identity(validation_path)
                if validation_path.is_file()
                else None
            )
        except TopologyCacheError as exc:
            raise AdapterError("independent_validator_module_unavailable") from exc
        validator_cache_key = (
            str(path.resolve()),
            artifact_stat_identity,
            actual_raw,
            actual_static,
            str(validation_path.resolve()),
            validator_source_stat_identity,
            validator_source_sha256,
        )
        reused = self._validator is not None and self._validator_cache_key == validator_cache_key
        if not reused:
            self.close()
        self.snapshot_path = path
        self.timing = {}
        self._bound_topology_session_id = topology.session_id
        self._bound_artifact_stat_identity = artifact_stat_identity
        self._bound_validator_source_path = validation_path
        self._bound_validator_source_stat_identity = validator_source_stat_identity
        self._bound_validator_source_sha256 = validator_source_sha256
        self._validator_cache_key = validator_cache_key
        self._bound_topology_evidence = {
            **evidence,
            "session_id": topology.session_id,
            "world_id": topology.world_id,
            "map_id": getattr(topology, "map_id", None),
            "expected_raw_artifact_sha256": expected_raw,
            "expected_static_topology_fingerprint": expected_static,
            "validator_source_sha256": validator_source_sha256,
            "topology_fingerprint": topology.fingerprint,
            "topology_source_decision": topology.source_decision,
            "validator_construction_reused": reused,
        }
        return dict(self._bound_topology_evidence)

    @staticmethod
    def _validation_source_path() -> Path:
        root = Path(__file__).resolve().parents[4]
        return root / "Tools" / "ff7r-navigation-validation" / "navigation_validation.py"

    @classmethod
    def _gimmick_source_path(cls) -> Path:
        return cls._validation_source_path().with_name("gimmick_validation.py")

    def bind_gimmick_evidence(self, path: str | Path | None, *, expected_sha256: str | None = None) -> Mapping[str, Any]:
        """Read the traversal-gimmick evidence with the independent gimmick
        validator (gimmick_validation.py), and require the very bytes the
        planner read.  None unbinds: a gimmick crossing then fails."""
        self._gimmick_evidence = None
        if path is None:
            return {"bound": False}
        source = self._gimmick_source_path()
        if self._gimmick_module is None:
            spec = importlib.util.spec_from_file_location("ff7r_independent_gimmick_validation", source)
            if spec is None or spec.loader is None:
                raise AdapterError("independent_gimmick_validator_unavailable")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._gimmick_module = module
        try:
            evidence = self._gimmick_module.GimmickEvidence(path)
        except (OSError, ValueError) as exc:
            raise AdapterError("gimmick_evidence_unreadable") from exc
        if expected_sha256 is not None and evidence.sha256 != str(expected_sha256).upper():
            raise AdapterError("gimmick_evidence_changed_since_planning")
        self._gimmick_evidence = evidence
        return {"bound": True, "path": str(path), "sha256": evidence.sha256,
                "gimmicks": len(evidence.gimmicks), "validator_source": str(source)}

    def _verify_bound_artifact(self) -> None:
        if self._bound_topology_session_id is None:
            return
        if self.snapshot_path is None:
            raise AdapterError("validator_topology_artifact_missing")
        current_stat_identity = _path_stat_identity_for_validator(self.snapshot_path)
        if current_stat_identity != self._bound_artifact_stat_identity:
            raise AdapterError("validator_topology_artifact_changed")
        if self._bound_artifact_stat_identity is not None:
            return
        evidence = self._artifact_evidence(self.snapshot_path)
        expected_raw = self._bound_topology_evidence.get("expected_raw_artifact_sha256")
        if expected_raw and str(evidence["raw_artifact_sha256"]).upper() != str(expected_raw).upper():
            raise AdapterError("validator_topology_artifact_hash_mismatch")
        expected_static = self._bound_topology_evidence.get("expected_static_topology_fingerprint")
        actual_static = evidence.get("static_topology_fingerprint")
        if expected_static and str(actual_static or "").upper() != str(expected_static).upper():
            raise AdapterError("validator_topology_fingerprint_mismatch")

    def _verify_validator_source(self) -> None:
        path = self._bound_validator_source_path or self._validation_source_path()
        current_stat_identity = _path_stat_identity_for_validator(path)
        if current_stat_identity != self._bound_validator_source_stat_identity:
            raise AdapterError("independent_validator_source_changed")
        if self._bound_validator_source_sha256 is None:
            return
        try:
            current_sha256 = validator_identity(path)
        except TopologyCacheError as exc:
            raise AdapterError("independent_validator_module_unavailable") from exc
        if current_sha256 != self._bound_validator_source_sha256:
            raise AdapterError("independent_validator_source_changed")

    def _load(self) -> Any:
        if self._validator is not None:
            self._verify_bound_artifact()
            self._verify_validator_source()
            return self._validator
        if self.require_topology_binding and self._bound_topology_session_id is None:
            raise AdapterError("validator_topology_session_unbound")
        if self.snapshot_path is None:
            raise AdapterError("validator_topology_artifact_missing")
        self._verify_bound_artifact()
        started = time.perf_counter()
        validation_path = self._validation_source_path()
        self._validator = self._construct_validator(validation_path)
        self.timing["python_validator_construction_ms"] = (time.perf_counter() - started) * 1000.0
        return self._validator

    def _construct_validator(self, validation_path: Path) -> Any:
        spec = importlib.util.spec_from_file_location("ff7r_independent_navigation_validation", validation_path)
        if spec is None or spec.loader is None:
            raise AdapterError("independent_validator_module_unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        analyzer = module.TopologyAnalyzer(self.snapshot_path)
        return module.RoutePlanValidator(analyzer)

    def close(self) -> None:
        validator, self._validator = self._validator, None
        close = getattr(validator, "close", None)
        if callable(close):
            close()

    def validate(self, plan: Mapping[str, Any]) -> Mapping[str, Any]:
        validator = self._load()
        started = time.perf_counter()
        segments = plan.get("traversal_segments") or [] if isinstance(plan, Mapping) else []
        if any(isinstance(segment, Mapping) and segment.get("gimmick_id") is not None for segment in segments):
            # Each walking leg goes to the pinned validator unchanged; the
            # gimmick crossings to the independent gimmick validator.
            if self._gimmick_module is None:
                result = {"valid": False, "errors": ["route crosses traversal gimmicks but none are bound"],
                          "warnings": []}
            else:
                result = self._gimmick_module.validate_gimmick_route(
                    json.loads(json.dumps(plan)), validator.validate, self._gimmick_evidence)
        else:
            result = validator.validate(plan)
        self.timing["python_validator_validation_ms"] = (time.perf_counter() - started) * 1000.0
        if not isinstance(result, Mapping):
            raise AdapterError("independent_validator_returned_non_object")
        return result


def load_route_follower_factory(module_path: str | Path | None = None) -> Callable[[Mapping[str, Any]], Any]:
    """Load the exact standalone RouteFollower without copying its schema."""

    candidate = Path(module_path) if module_path else None
    if candidate is None:
        root = Path(__file__).resolve().parents[4]
        candidate = root / "Tools" / "ff7r-route-following-simulator" / "route_following_simulator" / "simulator.py"
    if not candidate.is_file():
        raise AdapterError(f"route_follower_module_unavailable: {candidate}")
    spec = importlib.util.spec_from_file_location("ff7r_exact_route_follower", candidate)
    if spec is None or spec.loader is None:
        raise AdapterError("route_follower_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    # ``simulator.py`` declares dataclasses.  Register the exact module before
    # execution so dataclasses can resolve ``cls.__module__`` correctly when
    # this standalone file is loaded without importing its package.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    follower_type = getattr(module, "RouteFollower", None)
    if follower_type is None:
        raise AdapterError("route_follower_class_unavailable")
    return follower_type


class LiveRouteAdapter:
    """Join fresh live state, the accepted plan, follower and safe actuator."""

    def __init__(
        self,
        telemetry: TelemetrySource,
        topology: TopologyProvider,
        validator: Validator,
        follower_factory: Callable[[Mapping[str, Any]], Follower],
        actuator: Actuator,
        *,
        camera_actuator: CameraActuator | None = None,
        render_evidence: RenderEvidenceSource | None = None,
        config: LiveAdapterConfig | None = None,
        route_target: Vec3 | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        utc_now: Callable[[], str] | None = None,
        wall_clock: Callable[[], float] | None = None,
        static_obstacles: StaticObstacleOverlay | None = None,
        lookahead: NavigationSidecarLookahead | None = None,
        route_collision_audit: Callable[[list[dict[str, float]]], Mapping[str, Any]] | None = None,
        blocker_memory: Any | None = None,
        gate_registry: GateRegistry | None = None,
    ) -> None:
        # Blockers no query sees, carried between route attempts (RecentBlockers).
        self.blocker_memory = blocker_memory
        # Automatic doors and gates found by sweeps, carried between route
        # attempts like the blockers above (dynamic_gates.GateRegistry).
        self.gate_registry = gate_registry if gate_registry is not None else GateRegistry(clock=monotonic)
        self.telemetry = telemetry
        # Sweeps a planned route with the game's collision (route_sweep.py);
        # cached-topology routes are re-planned around what it finds.
        self.route_collision_audit = route_collision_audit
        self.topology = topology
        self.validator = validator
        self.follower_factory = follower_factory
        self.actuator = actuator
        self.camera_actuator = camera_actuator
        self.render_evidence = render_evidence
        self.config = config or LiveAdapterConfig()
        self.route_target = route_target
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.utc_now = utc_now or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        self.state = AdapterState.DISARMED
        self.reason = "movement_authorization_default_false"
        self.topology_session: TopologySession | None = None
        self.route_plan: Mapping[str, Any] | None = None
        self.validator_result: Mapping[str, Any] | None = None
        self.start_resolution: Any | None = None
        self.destination_resolution: Any | None = None
        self.render_confirmation: RenderedGameplayEvidence | None = None
        self.follower: Follower | None = None
        self.authorization: PlanAuthorization | None = None
        self.authorization_proof: RouteAuthorizationProof | None = None
        self.timeline: list[dict[str, Any]] = []
        self._last_observation: LiveObservation | None = None
        self._last_sample_attempt: LiveObservation | None = None
        self._last_command_sequence: int | None = None
        self._last_command_completed_t: float | None = None
        self._pending_command_evidence: dict[str, Any] | None = None
        self._last_post_command_observation_sequence: int | None = None
        self._attempt_movement_cycles = 0
        self._preflight_stage = "not_started"
        self.preflight_exception: dict[str, Any] | None = None
        self._multi_polygon_candidates: list[dict[str, Any]] = []
        self._multi_polygon_route_evidence: dict[str, Any] | None = None
        self._selected_route: SelectedRoute | None = None
        self._selected_route_plan_sha256: str | None = None
        self._adapter_route_plan_sha256: str | None = None
        self._independent_validator_route_plan_sha256: str | None = None
        self._last_resolved_polygon_ref: int | None = None
        self._last_corridor_sample: dict[str, Any] | None = None
        # The unrestricted resolver is intentionally retained for diagnostics,
        # but it is not a temporal route-transition state machine.  Keep the
        # last accepted corridor position separately so an overlapping portal
        # sample cannot regress the authorized route to a global tie-break.
        self._route_local_corridor_index: int | None = None
        self._route_local_corridor_ref: int | None = None
        self._route_local_last_position: Vec3 | None = None
        self._route_local_last_sequence: int | None = None
        self._last_route_along_m: float | None = None
        self._run_started_t: float | None = None
        self.wall_clock = wall_clock or time.perf_counter
        self.static_obstacles = static_obstacles or StaticObstacleOverlay()
        # The last overlay plan, keyed by everything it was planned from.
        self._static_overlay_plan_memo: tuple[tuple[Any, ...], Any, DetourRoutePlan] | None = None
        self.lookahead = lookahead
        self._static_no_progress_streak = 0
        self._static_last_evidence_command: tuple[str, int] | None = None
        self._stream_id = uuid.uuid4().hex
        self._sprint_pending_settle = False
        self._sprint_brake_started_t: float | None = None
        self._sprint_brake_previous: LiveObservation | None = None
        self._stream_hold_active = False
        self._static_pending_learning_kwargs: dict[str, Any] | None = None
        self._static_pending_discovery: dict[str, Any] | None = None
        self._static_last_learning: dict[str, Any] | None = None
        self._static_precommand_lookahead_checked = False
        self._static_lookahead_last_position: Vec3 | None = None
        self._static_lookahead_last_t: float | None = None
        self._static_lookahead_last_heading: float | None = None
        self._static_lookahead_last_polygon_ref: int | None = None
        self._route_sample_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._active_post_command_timing: dict[str, Any] | None = None
        self._active_post_command_route_sample: dict[str, Any] | None = None
        self.camera_follow = CameraFollowController(CameraFollowConfig(
            camera_follow_enabled=self.config.camera_follow_enabled,
            camera_follow_lookahead_m=self.config.camera_follow_lookahead_m,
            camera_follow_sprint_lookahead_m=self.config.camera_follow_sprint_lookahead_m,
            camera_follow_deadband_deg=self.config.camera_follow_deadband_deg,
            camera_follow_max_input=self.config.camera_follow_max_input,
            camera_follow_gain=self.config.camera_follow_gain,
            camera_follow_settle_hysteresis_deg=self.config.camera_follow_settle_hysteresis_deg,
            camera_follow_arrival_target=self.config.camera_follow_arrival_target,
            camera_follow_arrival_distance_m=self.config.camera_follow_arrival_distance_m,
        ))
        self._camera_follow_runtime_disabled = False
        self._camera_pixels_per_degree = CAMERA_DELTA_PER_DEGREE
        self._camera_stream_thread: threading.Thread | None = None
        self._camera_stream_stop = threading.Event()
        self._last_camera_result: dict[str, Any] = self.camera_follow.neutral("disabled")
        self._last_camera_timeline_key: tuple[Any, ...] | None = None
        # The route's off-mesh crossings (see _locate_route_traversals).
        self._route_traversals: list[dict[str, Any]] = []

    @property
    def movement_authorized(self) -> bool:
        return self.state == AdapterState.PLAN_VALIDATED and self.authorization is not None

    def _set_state(self, state: AdapterState, reason: str) -> None:
        self.state = state
        self.reason = reason

    def _record_camera_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(result)
        normalized.setdefault("active", False)
        normalized.setdefault("camera_input_x", 0.0)
        normalized.setdefault("camera_command_delta_px", 0)
        normalized.setdefault("actual_command_sent", False)
        self._last_camera_result = normalized
        key = (
            bool(normalized.get("active")),
            str(normalized.get("reason") or ""),
            round(float(normalized.get("camera_input_x") or 0.0), 4),
            int(normalized.get("camera_command_delta_px") or 0),
            bool(normalized.get("arrival_focus")),
        )
        if key != self._last_camera_timeline_key:
            self.timeline.append({
                "event": "camera_follow",
                "t": self.monotonic(),
                "enabled": self.config.camera_follow_enabled,
                "active": bool(normalized.get("active")),
                "suppressed": not bool(normalized.get("active")),
                "suppression_reason": normalized.get("reason"),
                "target_yaw_deg": normalized.get("target_yaw_deg"),
                "camera_yaw_deg": normalized.get("camera_yaw_deg"),
                "yaw_error_deg": normalized.get("yaw_error_deg"),
                "arrival_focus": bool(normalized.get("arrival_focus")),
                "remaining_route_m": normalized.get("remaining_route_m"),
                "camera_input_x": normalized.get("camera_input_x"),
                "camera_command_delta_px": normalized.get("camera_command_delta_px"),
                "lookahead_m": normalized.get("lookahead_m"),
                "sprint": normalized.get("sprint"),
                "actual_command_sent": normalized.get("actual_command_sent"),
            })
            self._last_camera_timeline_key = key
        return normalized

    def _smooth_camera_turn(self, observation: LiveObservation, duration_seconds: float, *, sprint: bool,
                            remaining_route_m: float | None = None) -> float:
        """Degrees to turn the camera during the coming command, or 0."""
        if (not self.config.camera_follow_enabled or not self.config.camera_follow_smooth
                or self._camera_follow_runtime_disabled or self.authorization_proof is None
                or observation.camera_yaw is None
                or not callable(getattr(self.camera_actuator, "stream_horizontal", None))):
            return 0.0
        try:
            result = dict(self.camera_follow.command(
                route=self.authorization_proof.waypoints, position=observation.position,
                camera_yaw_deg=observation.camera_yaw, sprint=bool(sprint), authority=True,
                remaining_route_m=remaining_route_m,
                speed_m_s=math.hypot(observation.velocity_cm_s.x, observation.velocity_cm_s.y) / 100.0
                if observation.velocity_cm_s is not None else 0.0))
        except Exception:
            return 0.0
        self._record_camera_result(result)
        error = result.get("yaw_error_deg")
        if not isinstance(error, (int, float)) or not math.isfinite(error) or abs(error) <= SMOOTH_CAMERA_DEADBAND_DEG:
            return 0.0
        limit = SMOOTH_CAMERA_MAX_DEG_PER_S * max(0.0, float(duration_seconds))
        gain = 1.0 if result.get("arrival_focus") else SMOOTH_CAMERA_GAIN
        return max(-limit, min(limit, float(error) * gain))

    def _start_camera_stream(self, turn_degrees: float, milliseconds: int) -> None:
        """Sweep the camera by ``turn_degrees`` alongside the stick command."""
        # A sweep still running is superseded, not waited for: waiting held
        # up a supervised stick deadline until it expired (live, fast follow:
        # supervised_deadline_invalid_or_expired).
        self._finish_camera_stream(stop=True, timeout=0.05)
        delta = max(-500, min(500, int(round(turn_degrees * self._camera_pixels_per_degree))))
        if delta == 0:
            return
        # A superseded thread may still be inside the native send when the
        # short join expires. Keep its cancellation latched permanently.
        self._camera_stream_stop = threading.Event()
        stop = self._camera_stream_stop
        record: dict[str, Any] = {"event": "camera_follow_smooth_turn", "t": self.monotonic(),
                                  "turn_degrees": turn_degrees, "delta_px": delta, "milliseconds": milliseconds}

        def run() -> None:
            try:
                record["result"] = self.camera_actuator.stream_horizontal(delta, milliseconds, stop.is_set)
            except Exception as exc:
                record["error"] = str(exc)
                self._camera_follow_runtime_disabled = True

        self.timeline.append(record)
        self._camera_stream_thread = threading.Thread(target=run, name="camera-follow-smooth", daemon=True)
        self._camera_stream_thread.start()

    def _finish_camera_stream(self, stop: bool = False, timeout: float = 1.5) -> None:
        thread = self._camera_stream_thread
        if thread is None:
            return
        if stop:
            self._camera_stream_stop.set()
        thread.join(timeout=timeout)
        self._camera_stream_thread = None

    def _neutralize_camera(self, reason: str) -> dict[str, Any]:
        self._finish_camera_stream(stop=True)
        self.camera_follow.neutral(reason)
        result: dict[str, Any] = {
            "active": False,
            "camera_input_x": 0.0,
            "camera_command_delta_px": 0,
            "actual_command_sent": False,
            "reason": str(reason),
        }
        callback = getattr(self.camera_actuator, "neutralize", None)
        if callable(callback):
            try:
                result["backend_result"] = callback()
            except Exception as exc:
                result["neutralization_error"] = str(exc)
                self._camera_follow_runtime_disabled = True
        return self._record_camera_result(result)

    def _camera_follow_for_cycle(self, observation: LiveObservation, event: Mapping[str, Any], *, sprint: bool, dry_run: bool) -> dict[str, Any]:
        def suppress(reason: str) -> dict[str, Any]:
            return self._neutralize_camera(reason)
        if not self.config.camera_follow_enabled:
            return suppress("disabled")
        if dry_run:
            return suppress("dry_run")
        if self._camera_follow_runtime_disabled:
            return suppress("camera_actuator_failed")
        if self.camera_actuator is None or not callable(getattr(self.camera_actuator, "command_horizontal", None)):
            return suppress("camera_actuator_unavailable")
        if self.authorization_proof is None:
            return suppress("route_authorization_missing")
        effective_age = self._effective_telemetry_age_seconds(observation)
        if not math.isfinite(effective_age) or effective_age > self.config.max_telemetry_age_seconds:
            return suppress("telemetry_stale")
        if self._last_command_sequence is not None and observation.sequence <= self._last_command_sequence:
            return suppress("telemetry_not_advancing")
        failures = self._check_preconditions(observation)
        if failures:
            return suppress(failures[0])
        if not self._same_authorization(observation):
            return suppress("route_authorization_changed")
        if self.state not in {AdapterState.PLAN_VALIDATED, AdapterState.FOLLOWING}:
            return suppress("route_follower_not_active")
        if str(event.get("state") or "") != "FOLLOWING":
            return suppress("route_follower_not_moving")
        if str(event.get("suggested_speed_band") or "stop") == "stop":
            return suppress("route_follower_stopped")
        if not observation.lease_active:
            return suppress("movement_lease_expired")
        try:
            result = dict(self.camera_follow.command(
                route=self.authorization_proof.waypoints,
                position=observation.position,
                camera_yaw_deg=observation.camera_yaw,
                sprint=bool(sprint),
                authority=True,
            ))
            result["sprint"] = bool(sprint)
            input_x = float(result.get("camera_input_x") or 0.0)
            if input_x == 0.0:
                result.update(camera_command_delta_px=0, actual_command_sent=False)
                return self._record_camera_result(result)
            delta = max(-500, min(500, int(round(max(-1.0, min(1.0, input_x)) * 90.0 * self._camera_pixels_per_degree * 0.75))))
            if delta == 0:
                result.update(active=False, camera_input_x=0.0, camera_command_delta_px=0,
                              actual_command_sent=False, reason="camera_command_below_one_pixel")
                return self._record_camera_result(result)
            # Camera-relative movement must be released before changing its
            # reference frame. This happens only for a material camera command,
            # not for ordinary continuous steering inside the deadband.
            if not self._neutralize("camera_yaw_adjustment"):
                raise AdapterError("camera_adjustment_requires_neutral_movement")
            self._pause_follower_for_readiness(self.monotonic(), "camera_adjustment")
            result["camera_command_attempted"] = True
            result["command_started_t"] = self.monotonic()
            try:
                result["backend_result"] = self.camera_actuator.command_horizontal(delta)
            except Exception as exc:
                self._camera_follow_runtime_disabled = True
                result.update(active=False, camera_input_x=0.0, camera_command_delta_px=0,
                              actual_command_sent=False, reason="camera_actuator_failed",
                              camera_error=str(exc))
                return self._record_camera_result(result)
            result.update(camera_command_delta_px=delta, actual_command_sent=True)
            return self._record_camera_result(result)
        except AdapterError:
            raise
        except Exception as exc:
            self._camera_follow_runtime_disabled = True
            return self._record_camera_result({
                "active": False, "camera_input_x": 0.0, "camera_command_delta_px": 0,
                "actual_command_sent": False, "reason": "camera_follow_failed",
                "camera_error": str(exc), "sprint": bool(sprint),
            })

    def _coordinate_camera_follow(
        self, observation: LiveObservation, event: Mapping[str, Any], *, dry_run: bool,
    ) -> LiveObservation:
        result = self._camera_follow_for_cycle(
            observation, event, sprint=self.config.sprint, dry_run=dry_run,
        )
        if not result.get("camera_command_attempted"):
            return observation
        started = float(result["command_started_t"])
        deadline = started + CAMERA_SETTLE_TIMEOUT_SECONDS
        settling = CameraYawSettling(float(observation.camera_yaw), observation.sequence)
        previous = observation
        required_after_sequence = observation.sequence
        readiness_recovery_attempts = 0
        samples = []

        def recover_readiness(current: LiveObservation, failures: Sequence[str]) -> bool:
            """Rebuild settled gameplay proof while camera movement is neutral."""

            nonlocal required_after_sequence, readiness_recovery_attempts
            settle = getattr(self.telemetry, "wait_for_settled_gameplay", None)
            remaining = max(0.0, deadline - self.monotonic())
            can_retry = (
                callable(settle)
                and readiness_recovery_attempts < 3
                and remaining >= 1.0 + self.config.telemetry_poll_seconds
            )
            if not can_retry:
                return False
            if self.actuator.is_neutral() is not True:
                raise AdapterError("camera_settle_movement_not_neutral")
            readiness_recovery_attempts += 1
            self._pause_follower_for_readiness(self.monotonic(), "camera_settle_readiness")
            post_settle_reserve = max(
                self.config.telemetry_poll_seconds * 2.0,
                CAMERA_SETTLE_DURATION_SECONDS + self.config.telemetry_poll_seconds,
            )
            timeout = min(
                self.config.settled_gameplay_timeout_seconds,
                max(0.0, remaining - post_settle_reserve),
            )
            event: dict[str, Any] = {
                "event": "camera_settle_readiness_recovery",
                "t": self.monotonic(),
                "sequence": current.sequence,
                "attempt": readiness_recovery_attempts,
                "failures": list(failures),
                "timeout_seconds": timeout,
                "movement_command_sent": False,
            }
            try:
                settled_result = settle(
                    timeout,
                    max_telemetry_age_seconds=self.config.max_telemetry_age_seconds,
                )
                event["result"] = settled_result
                if isinstance(settled_result, Mapping) and settled_result.get("ok") is True:
                    try:
                        result_sequence = settled_result.get("sequence")
                        if result_sequence is not None:
                            required_after_sequence = max(
                                required_after_sequence, int(result_sequence),
                            )
                    except (TypeError, ValueError):
                        pass
            except Exception as exc:
                event["error"] = _sanitize_exception_message(exc)
            self.timeline.append(event)
            return True

        try:
            while self.monotonic() < deadline:
                try:
                    current = self._require_observation(
                        wait_for_fresh=True, after_sequence=required_after_sequence,
                        timeout_seconds=max(0.0, deadline - self.monotonic()),
                        require_effective_fresh=True,
                    )
                except AdapterError as exc:
                    if str(exc) in {"telemetry_stale", "telemetry_not_advancing"}:
                        if recover_readiness(previous, [str(exc)]):
                            continue
                    raise
                failures = self._check_preconditions(current)
                if failures:
                    if recover_readiness(current, failures):
                        previous = current
                        required_after_sequence = current.sequence
                        continue
                    raise AdapterError("camera_settle_" + ";".join(failures))
                if not self._same_authorization(current):
                    raise AdapterError("camera_settle_authorization_changed")
                if self.actuator.is_neutral() is not True:
                    raise AdapterError("camera_settle_movement_not_neutral")
                # Any residual movement is still subject to the ordinary
                # corridor/portal/obstacle assessment, before another command.
                self._record_post_command_observation(current)
                samples.append({"sequence": current.sequence, "t": self.monotonic(),
                    "yaw": current.camera_yaw, "position": _point_dict(current.position),
                    "telemetry_age_seconds": self._effective_telemetry_age_seconds(current)})
                if settling.observe(yaw=float(current.camera_yaw), sequence=current.sequence,
                                    now=self.monotonic()):
                    change = shortest_yaw_error_deg(current.camera_yaw, observation.camera_yaw)
                    delta = result.get("camera_command_delta_px")
                    if delta and abs(change) >= 0.05:
                        # Same observed-response bound as bearing alignment.
                        self._camera_pixels_per_degree = max(3.0, min(30.0, abs(delta / change)))
                    self.timeline.append({"event": "camera_yaw_settled", "t": self.monotonic(),
                        "duration_seconds": self.monotonic() - started,
                        "pre_sequence": observation.sequence, "post_sequence": current.sequence,
                        "pre_yaw": observation.camera_yaw, "post_yaw": current.camera_yaw,
                        "observed_change_degrees": change, "samples": samples,
                        "movement_neutral": True, "movement_recomputation_required": True})
                    self._resume_follower_after_readiness(current.monotonic_t, "camera_adjustment")
                    return current
                previous = current
                required_after_sequence = current.sequence
                self.sleeper(self.config.telemetry_poll_seconds)
            raise AdapterError("camera_yaw_did_not_settle")
        except AdapterError as exc:
            self._camera_follow_runtime_disabled = True
            self.timeline.append({"event": "camera_yaw_settle_failed", "t": self.monotonic(),
                "duration_seconds": self.monotonic() - started, "samples": samples,
                "error": str(exc), "camera_follow_disabled_for_route": True,
                "movement_command_sent": False})
            # A camera that would not settle only costs the camera: live, one
            # not-controllable sample ended a route to Ned here. Follow on
            # without camera-follow; genuine safety failures still stop.
            if str(exc) in {"camera_settle_authorization_changed", "camera_settle_movement_not_neutral"}:
                raise
            # Continue only from a sample that passes every precondition, as
            # a settled camera would have. Control can take a few seconds to
            # come back (live: Cloud not controllable while the game carried
            # him up steps, for longer than the settle window), so wait for it
            # with movement neutral; otherwise the original error stands.
            control_deadline = self.monotonic() + CAMERA_FALLBACK_CONTROL_WAIT_SECONDS
            waited_after = None
            while True:
                try:
                    current = self._require_observation(
                        wait_for_fresh=True, after_sequence=waited_after, require_effective_fresh=True)
                except AdapterError:
                    raise exc
                if not self._same_authorization(current):
                    raise exc
                if not self._check_preconditions(current):
                    break
                if self.monotonic() >= control_deadline or self.actuator.is_neutral() is not True:
                    raise exc
                waited_after = current.sequence
                self.sleeper(self.config.telemetry_poll_seconds)
            self.timeline.append({"event": "camera_follow_fallback_control_regained", "t": self.monotonic(),
                                  "sequence": current.sequence, "movement_command_sent": False})
            self._resume_follower_after_readiness(current.monotonic_t, "camera_adjustment")
            return current

    def _neutralize(self, reason: str) -> bool:
        self._neutralize_camera(reason)
        self._stream_hold_active = False
        try:
            result = self.actuator.neutralize()
            neutral = self.actuator.is_neutral()
            self.timeline.append({
                "event": "actuator_neutralized",
                "t": self.monotonic(),
                "reason": reason,
                "result": result,
                "confirmed": neutral is True,
            })
            return neutral is True
        except Exception as exc:
            self.timeline.append({
                "event": "actuator_neutralization_failed",
                "t": self.monotonic(),
                "reason": reason,
                "error": str(exc),
            })
            self._set_state(AdapterState.FAILED, "actuator_neutralization_failed")
            raise AdapterError("actuator_neutralization_failed") from exc

    def _require_observation(
        self,
        *,
        wait_for_fresh: bool = False,
        after_sequence: int | None = None,
        timeout_seconds: float | None = None,
        require_effective_fresh: bool = False,
        timing: dict[str, float] | None = None,
    ) -> LiveObservation:
        deadline = (
            self.monotonic()
            + (self.config.preflight_telemetry_wait_seconds if timeout_seconds is None else float(timeout_seconds))
            if wait_for_fresh
            else None
        )
        waited = False
        wait_started_t = self.monotonic() if wait_for_fresh else None
        while True:
            observation = self._timed_stage(
                timing,
                "telemetry_parse_read_ms",
                self.telemetry.observe,
            )
            if not isinstance(observation, LiveObservation):
                raise AdapterError("telemetry_source_returned_invalid_observation")
            self._last_sample_attempt = observation
            try:
                age = float(observation.telemetry_age_seconds)
            except (TypeError, ValueError):
                age = math.nan
            fresh = math.isfinite(age) and 0.0 <= age <= self.config.max_telemetry_age_seconds
            if require_effective_fresh:
                effective_age = self._effective_telemetry_age_seconds(observation)
                fresh = fresh and math.isfinite(effective_age) and effective_age <= self.config.max_telemetry_age_seconds
            advancing = after_sequence is None or observation.sequence > after_sequence
            if fresh and advancing:
                break
            if not wait_for_fresh:
                # Preserve the exact rejected sample for the caller's
                # fail-closed evidence before raising; it must never silently
                # disappear behind a later fresh sample.
                self._last_observation = observation
                raise AdapterError("telemetry_stale" if not fresh else "telemetry_not_advancing")
            waited = True
            remaining = float(deadline - self.monotonic()) if deadline is not None else 0.0
            if remaining <= 0.0:
                raise AdapterError("telemetry_stale" if not fresh else "telemetry_not_advancing")
            self.sleeper(min(self.config.telemetry_poll_seconds, remaining))
        if waited:
            self.timeline.append({
                "event": "fresh_next_telemetry_acquired" if after_sequence is not None else "fresh_telemetry_observation_acquired",
                "t": observation.monotonic_t,
                "sequence": observation.sequence,
                "telemetry_age_seconds": observation.telemetry_age_seconds,
                "effective_telemetry_age_seconds": self._effective_telemetry_age_seconds(observation),
                "after_sequence": after_sequence,
                "wait_duration_seconds": max(0.0, self.monotonic() - float(wait_started_t)),
                "position_after": _point_dict(observation.position),
                "position_before": (
                    self._pending_command_evidence.get("position_before")
                    if after_sequence is not None and self._pending_command_evidence
                    else None
                ),
                "time_since_command_completion_seconds": (
                    max(0.0, self.monotonic() - self._last_command_completed_t)
                    if after_sequence is not None and self._last_command_completed_t is not None
                    else None
                ),
            })
        values = (
            observation.position.x,
            observation.position.y,
            observation.position.z,
            observation.actor_yaw,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise AdapterError("telemetry_non_finite")
        if not observation.actor_id or not observation.session_id or not observation.world_id or not observation.map_id:
            raise AdapterError("telemetry_identity_incomplete")
        if self.config.require_camera_yaw and (
            observation.camera_yaw is None or not math.isfinite(float(observation.camera_yaw))
        ):
            raise AdapterError("camera_yaw_telemetry_missing")
        if self._last_observation is not None:
            if observation.monotonic_t < self._last_observation.monotonic_t:
                raise AdapterError("telemetry_timestamp_regressed")
            if observation.actor_id != self._last_observation.actor_id:
                raise AdapterError("actor_identity_changed")
            if observation.position_reference != self._last_observation.position_reference:
                raise AdapterError("position_reference_changed")
            if observation.session_id != self._last_observation.session_id:
                raise AdapterError("session_changed")
            if observation.world_id != self._last_observation.world_id or observation.map_id != self._last_observation.map_id:
                raise AdapterError("world_changed")
        self._last_observation = observation
        return observation

    def _check_preconditions(self, observation: LiveObservation, *, require_lease: bool = True,
                             allow_owned_hold: bool = False) -> list[str]:
        failures: list[str] = []
        checks = (
            ("game_process_invalid", observation.process_valid),
            ("executable_identity_invalid", observation.executable_valid),
            ("cloud_actor_unresolved", bool(observation.actor_id)),
            ("world_identity_missing", bool(observation.world_id and observation.map_id)),
            ("loading_or_streaming", not observation.loading),
            ("not_controllable", observation.controllable),
            ("menu_or_pause", not observation.in_menu),
            ("cutscene_active", not observation.in_cutscene),
            ("combat_active", not observation.in_combat),
            ("interaction_active", not observation.in_interaction),
            ("movement_lease_missing", observation.lease_active if require_lease else True),
            ("actuator_not_neutral", observation.actuator_neutral or (
                (self.config.controller_mode == "continuous" or allow_owned_hold) and self._stream_hold_active
                and self.state == AdapterState.FOLLOWING)),
        )
        failures.extend(name for name, passed in checks if not passed)
        return failures

    def _resolve_topology(self, observation: LiveObservation) -> TopologySession:
        if self.render_confirmation is None or not _render_evidence_confirms_gameplay(
            observation,
            self.render_confirmation,
            max_telemetry_age_seconds=self.config.max_telemetry_age_seconds,
        ):
            raise AdapterError("topology_capture_requires_render_confirmation")
        arm_capture = getattr(self.topology, "arm_capture", None)
        if callable(arm_capture):
            try:
                arm_capture(observation, self.render_confirmation)
                event = {
                    "event": "topology_capture_armed_after_render_confirmation",
                    "t": self.monotonic(),
                }
                capture_budget = getattr(self.topology, "max_capture_wait_seconds", None)
                if isinstance(capture_budget, (int, float)) and math.isfinite(float(capture_budget)):
                    event["max_capture_wait_seconds"] = float(capture_budget)
                self.timeline.append(event)
                topology = self.topology.load_current(observation)
            finally:
                disarm_capture = getattr(self.topology, "disarm_capture", None)
                if callable(disarm_capture):
                    disarm_capture()
        else:
            decision = getattr(self.topology, "source_decision", None)
            if decision is not None:
                self.timeline.append({
                    "event": "topology_source_decision",
                    "t": self.monotonic(),
                    "decision": str(decision),
                    "reason": getattr(self.topology, "decision_reason", None),
                })
            topology = self.topology.load_current(observation)
        if not isinstance(topology, TopologySession) or not topology.current_session:
            raise AdapterError("current_session_topology_unavailable")
        if topology.movement_sent:
            raise AdapterError("topology_capture_movement_not_read_only")
        if topology.session_id != observation.session_id or topology.world_id != observation.world_id:
            raise AdapterError("topology_session_or_world_mismatch")
        if topology.source_decision in {"validated_cache_hit", "validated_cache_miss", "cache_invalid"}:
            self.timeline.append({
                "event": "topology_source_selected",
                "t": self.monotonic(),
                "decision": topology.source_decision,
                "reason": getattr(self.topology, "decision_reason", None),
                "static_fingerprint": topology.static_fingerprint or topology.fingerprint,
            })
        return topology

    def _bind_static_obstacle_scope(
        self,
        topology: TopologySession,
        observation: LiveObservation,
    ) -> None:
        scope = StaticObstacleScope(
            session_id=topology.session_id,
            world_id=topology.world_id,
            map_id=observation.map_id,
            topology_fingerprint=topology.fingerprint,
        )
        summary = topology.payload_summary if isinstance(topology.payload_summary, Mapping) else {}
        cache_identity = summary.get("cache_manifest_identity") if isinstance(summary.get("cache_manifest_identity"), Mapping) else {}
        content_hash = str(topology.artifact_sha256 or cache_identity.get("raw_artifact_sha256") or "").upper()
        durable_identity = StaticObstacleDurableIdentity(
            build_id=str(observation.executable_sha256 or "unverified-build").upper(),
            map_id=observation.map_id,
            topology_fingerprint=str(topology.static_fingerprint or topology.fingerprint).upper(),
            topology_schema=str((topology.graph.snapshot_diagnostics or {}).get("schema_version") or "shipping_detour-v1"),
            content_hashes=(content_hash,) if content_hash else (),
        )
        snapshot = self.static_obstacles.bind_scope(scope, durable_identity=durable_identity)
        self.timeline.append({
            "event": "static_obstacle_scope_bound",
            "t": self.monotonic(),
            "scope": scope.to_dict(),
            "generation": snapshot.generation,
            "cleared_obstacles": snapshot.state == ObstacleEvidenceState.READY and not snapshot.obstacles,
            "obstacle_count": len(snapshot.obstacles),
            "durable_identity": durable_identity.to_dict(),
            "durable_identity_key": durable_identity.key(),
        })

    def _plan_with_static_overlay(
        self,
        topology: TopologySession,
        start: Vec3,
        end: Vec3,
        **kwargs: Any,
    ) -> DetourRoutePlan:
        if self.config.route_mode == "cached_topology":
            kwargs.setdefault("portal_corner_inset_m", self.config.portal_corner_inset_m)
        if self.config.route_mode != "cached_topology" or not getattr(self.config, "traversal_links", True):
            # Only a cached-topology route follows the corridor out of the
            # graph's polygons; the others stay on ordinary portals.
            kwargs.setdefault("traversal_policy", DISABLED_TRAVERSAL_POLICY)
        elif FAILED_TRAVERSAL_LINKS and isinstance(getattr(topology.graph, "traversal_policy", None), TraversalPolicy):
            # Not the links a crossing has already failed on.
            kwargs.setdefault("traversal_policy", replace(
                topology.graph.traversal_policy,
                excluded_links=topology.graph.traversal_policy.excluded_links | frozenset(FAILED_TRAVERSAL_LINKS)))
        kwargs.setdefault("routing_objective", self.config.routing_objective)
        now_t = self.monotonic()
        key = self._static_overlay_plan_key(topology, start, end, kwargs, now_t)
        # Doors and gates found by sweeps are not in the overlay: the plan goes
        # through them at a cost (plan_through_gates), so the plan depends on them.
        registry = getattr(self, "gate_registry", None)
        gates = registry.gates(_gate_map_id(topology)) if registry is not None else ()
        if key is not None and gates:
            key = key[:-1] + (registry.digest(_gate_map_id(topology)), key[-1])
        memo = getattr(self, "_static_overlay_plan_memo", None)
        if key is not None and memo is not None and memo[0] == key and memo[1] is topology.graph:
            # The preflight re-plans the plan the collision audit has just
            # accepted, from the same start, target and overlay (live at
            # Kalm: 32 s repeating a grid detour). The planner is a pure
            # function of these, so the earlier plan is that plan.
            self.timeline.append({
                "event": "static_overlay_plan_reused",
                "t": self.monotonic(),
                "static_obstacle_digest": key[-1],
            })
            return memo[2]
        def plan_on(overlay: Any) -> DetourRoutePlan:
            return plan_with_static_obstacles(topology.graph, start, end, overlay, now_t=now_t, **kwargs)

        try:
            if gates:
                result = plan_through_gates(plan_on, self.static_obstacles, gates,
                                            penalty_m=registry.penalty_m, t=now_t)
            else:
                result = plan_on(self.static_obstacles)
        except (StaticObstaclePlanningError, StaticObstacleScopeError) as exc:
            raise AdapterError(str(exc)) from exc
        if key is not None and isinstance(result, DetourRoutePlan):
            self._static_overlay_plan_memo = (key, topology.graph, result)
        return result

    def _static_overlay_plan_key(
        self,
        topology: TopologySession,
        start: Vec3,
        end: Vec3,
        kwargs: Mapping[str, Any],
        now_t: float,
    ) -> tuple[Any, ...] | None:
        """What an overlay plan depends on, or None when it cannot be keyed.

        The graph is immutable and compared by identity; the overlay enters
        through its planning snapshot digest (scope, generation, obstacles).
        Keyword arguments left at None take the planner's None defaults.
        """
        options = tuple(sorted(
            (name, json.dumps(value.to_dict(), sort_keys=True) if isinstance(value, TraversalPolicy) else value)
            for name, value in kwargs.items() if value is not None
        ))
        if not all(isinstance(value, (bool, int, float, str)) for _, value in options):
            return None
        try:
            digest = self.static_obstacles.planning_snapshot(now_t=now_t).digest()
        except (StaticObstaclePlanningError, StaticObstacleScopeError, AttributeError):
            return None
        if not isinstance(digest, str) or not digest:
            return None
        return (
            topology.session_id,
            topology.fingerprint,
            (float(start.x), float(start.y), float(start.z)),
            (float(end.x), float(end.y), float(end.z)),
            options,
            digest,
        )

    def _static_obstacle_digest(self) -> str:
        try:
            return self.static_obstacles.planning_snapshot(now_t=self.monotonic()).digest()
        except (StaticObstaclePlanningError, StaticObstacleScopeError):
            return ""

    def _reconcile_gates(self, map_id: str, sweep: Mapping[str, Any], points: Sequence[Mapping[str, float]],
                         *, only: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Stop keeping as walls, and register as gates, actors a sweep shows moved or gone.

        Anything that moved between sweeps is dynamic: it must not stay in the
        overlay as a static obstacle for the rest of the session.
        """
        registry = self.gate_registry
        changes = registry.reconcile(map_id, sweep, points, only=only)
        for change in changes:
            sighting = registry.sighting(map_id, change["actor"])
            if sighting is None:
                continue
            self.static_obstacles.discard_obstacle(sighting.obstacle_id)
            gate = registry.add_gate(map_id, sighting, [change["change"]], {"change": change})
            self.timeline.append({"event": "dynamic_gate_registered", "t": self.monotonic(), "gate_id": gate.gate_id,
                                  "actor": gate.actor, "signals": list(gate.signals), "change": change["change"]})
        return changes

    def _reverify_actor_obstacles(self, topology: TopologySession) -> None:
        """Sweep across remembered actor obstacles again before planning round them.

        A door seen shut and kept as a wall may be open now (live in the
        prologue: it opened as Zack walked up), and a wall that no longer
        stands is a gate, not an obstacle. At most MAX_REVERIFIED_OBSTACLES
        sweeps, none when nothing was learned from an actor's bounds.
        """
        audit, registry = self.route_collision_audit, self.gate_registry
        if audit is None or registry is None:
            return
        try:
            known = self.static_obstacles.planning_snapshot(now_t=self.monotonic()).obstacles
        except (StaticObstaclePlanningError, StaticObstacleScopeError):
            return
        map_id = _gate_map_id(topology)
        checked = 0
        for obstacle in known:
            sighting = registry.sighting_for_obstacle(map_id, obstacle.obstacle_id)
            if sighting is None or "route_sweep_actor_bounds" not in obstacle.provenance:
                continue
            if checked >= MAX_REVERIFIED_OBSTACLES:
                break
            # Across the footprint, square to its long axis, floor from the navmesh.
            radians = math.radians(obstacle.heading_degrees + 90.0)
            reach = (obstacle.effective_radius_m * 100.0) + REVERIFY_REACH_CM
            ends = []
            for sign in (-1.0, 1.0):
                end = Vec3(obstacle.center.x + sign * math.cos(radians) * reach,
                           obstacle.center.y + sign * math.sin(radians) * reach, obstacle.center.z)
                floor = topology.graph.resolve_point(end, max_snap_distance_m=1.0, max_vertical_separation_m=3.0)
                if not floor.ok or floor.closest_point is None:
                    break
                ends.append(floor.closest_point)
            if len(ends) != 2:
                continue
            checked += 1
            points = [_point_dict(point) for point in ends]
            try:
                sweep = audit(points)
            except Exception as exc:
                self.timeline.append({"event": "obstacle_reverify_failed", "t": self.monotonic(),
                                      "obstacle_id": obstacle.obstacle_id, "error": str(exc)})
                continue
            changes = self._reconcile_gates(map_id, sweep, points, only=[sighting.actor])
            self.timeline.append({"event": "obstacle_reverified", "t": self.monotonic(),
                                  "obstacle_id": obstacle.obstacle_id, "actor": sighting.actor,
                                  "changes": [change["change"] for change in changes]})

    def _promote_sealed_gates(self, map_id: str, hits: Sequence[Mapping[str, Any]], plan: Any,
                              failure: BaseException, admitted: dict[str, Any]) -> list[Any]:
        """Gates among obstacles whose admission left the planner no route.

        Called when a re-plan fails with the obstacles just admitted as walls.
        Those that are thin leaves sealing a narrow neck the swept route
        crossed are gates after all (dynamic_gates.neck_evidence): they come
        out of the overlay and the caller plans again through them.
        """
        if not any(reason in str(failure) for reason in SEALED_REPLAN_REASONS):
            return []
        topology = getattr(self, "topology_session", None)
        gates = self.gate_registry.promote_sealing(map_id, hits, plan=plan, graph=getattr(topology, "graph", None))
        for gate in gates:
            self.static_obstacles.discard_obstacle(obstacle_id_for(gate.actor))
            # Its own leaf read as a ledge or spot is no wall either (DynamicGate.covers).
            for key in [key for key, item in admitted.items()
                        if item.obstacle_id == obstacle_id_for(gate.actor)
                        or (not key.startswith("actor:") and gate.covers(_point_dict(item.center)))]:
                self.static_obstacles.discard_obstacle(admitted.pop(key).obstacle_id)
            self.timeline.append({"event": "dynamic_gate_registered", "t": self.monotonic(), "gate_id": gate.gate_id,
                                  "actor": gate.actor, "signals": list(gate.signals), "change": "sealed_neck",
                                  "replan_failure": str(failure)})
        return gates

    def _attach_traversal_gimmicks(self, topology: TopologySession) -> None:
        """Set the scanned gimmicks on the route's graph and bind the
        validator to the very evidence the planner used."""
        path = self.config.gimmick_evidence_path if self.config.traversal_links else None
        if path is None and not any(link.gimmick_id for link in topology.graph.off_mesh_links):
            return
        try:
            attached = attach_gimmick_links(topology.graph, path)
        except (OSError, ValueError) as exc:
            # Unreadable evidence plans on the capture alone, as before.
            topology.graph.set_gimmick_links(())
            attached = {"attached": False, "reason": "evidence_unreadable", "error": str(exc)[:200], "sha256": None}
        binder = getattr(self.validator, "bind_gimmick_evidence", None)
        binding = None
        if callable(binder):
            binding = binder(path if attached.get("sha256") else None, expected_sha256=attached.get("sha256"))
        self.timeline.append({"event": "traversal_gimmicks_attached", "t": self.monotonic(),
                              **attached, "validator_binding": binding})

    def _bind_validator_to_topology(self, topology: TopologySession) -> None:
        binder = getattr(self.validator, "bind_topology_session", None)
        if not callable(binder):
            if getattr(self.validator, "require_topology_binding", False):
                raise AdapterError("validator_topology_session_unbound")
            return
        evidence = binder(topology)
        self.timeline.append({
            "event": "validator_topology_bound",
            "t": self.monotonic(),
            "artifact_path": str(topology.artifact_path),
            "artifact_sha256": topology.artifact_sha256,
            "static_fingerprint": topology.static_fingerprint or topology.fingerprint,
            "world_id": topology.world_id,
            "session_id": topology.session_id,
            "binding": dict(evidence) if isinstance(evidence, Mapping) else evidence,
        })

    def _choose_destination(
        self,
        topology: TopologySession,
        observation: LiveObservation,
        start_ref: int,
    ) -> SelectedRoute | tuple[Vec3, Any]:
        def selected_route(destination: Vec3, resolution: Any, plan: DetourRoutePlan, candidates: list[dict[str, Any]]) -> SelectedRoute:
            plan_mapping = plan.to_dict()
            return SelectedRoute(
                destination=destination,
                resolution=resolution,
                plan=plan_mapping,
                candidates=tuple(copy.deepcopy(candidates)),
                selection_id=uuid.uuid4().hex,
                topology_session_id=topology.session_id,
                topology_fingerprint=topology.fingerprint,
                plan_sha256=_plan_hash(plan_mapping),
            )

        if self.config.route_mode == "cached_topology":
            if self.route_target is None:
                raise AdapterError("cached_topology_route_target_missing")
            resolution = self._resolve_route_target(topology)
            if self.config.admission_target_edge_projection:
                resolution = self._project_capture_admission_target(topology, resolution, observation.position)
            if not resolution.ok or resolution.reason_code != "point_inside_polygon":
                raise AdapterError("cached_topology_route_target_not_inside_topology")
            def plan_route() -> DetourRoutePlan:
                result = self._plan_with_static_overlay(
                    topology,
                    observation.position,
                    self.route_target,
                    max_expansions=self.config.cached_topology_max_expansions,
                    max_waypoints=self.config.cached_topology_max_waypoints,
                    max_snap_distance_m=self.config.start_max_snap_m,
                    max_vertical_separation_m=self.config.start_max_vertical_m,
                )
                if not isinstance(result, DetourRoutePlan):
                    raise AdapterError("planner_returned_invalid_route_plan")
                if result.status != "complete":
                    raise AdapterError(f"cached_topology_route_plan_{result.reason_code}")
                return result

            self._reverify_actor_obstacles(topology)
            self._clear_target_of_known_obstacles(topology, observation.position)
            planner_result = self._audit_route_collisions(observation, plan_route(), plan_route)
            # Moving the target clear of an obstacle (before planning or in the
            # audit) can move it into another polygon; the destination binding
            # must describe the target actually planned to (live:
            # selected_route_plan_end_ref_mismatch on every route to Ned).
            resolution = self._resolve_route_target(topology)
            if not resolution.ok or resolution.reason_code != "point_inside_polygon":
                raise AdapterError("cached_topology_route_target_not_inside_topology")
            selected = selected_route(
                self.route_target,
                resolution,
                planner_result,
                [{
                    "selected": True,
                    "route_mode": self.config.route_mode,
                    "target_position": _point_dict(self.route_target),
                    "end_ref": planner_result.end_ref,
                    "route_length_m": planner_result.total_estimated_path_length_m,
                    "tile_crossings": planner_result.validation.get("tile_crossings"),
                }],
            )
            self.timeline.append({
                "event": "cached_topology_route_planned",
                "t": self.monotonic(),
                "route_mode": self.config.route_mode,
                "target_position": _point_dict(self.route_target),
                "start_ref": planner_result.start_ref,
                "end_ref": planner_result.end_ref,
                "route_length_m": planner_result.total_estimated_path_length_m,
                "waypoint_count": len(planner_result.waypoints),
                "tile_crossings": planner_result.validation.get("tile_crossings"),
                "route_plan_sha256": selected.plan_sha256,
            })
            return selected

        if self.config.route_mode == "ordinary_multi_polygon":
            try:
                def overlay_planner(candidate_start: Vec3, candidate_end: Vec3, **planner_kwargs: Any) -> DetourRoutePlan:
                    planner_kwargs.pop("max_tile_crossings", None)
                    return self._plan_with_static_overlay(
                        topology,
                        candidate_start,
                        candidate_end,
                        max_tile_crossings=0,
                        **planner_kwargs,
                    )
                destination, resolution, plan, candidates = select_ordinary_multi_polygon_destination(
                    topology.graph,
                    observation.position,
                    start_ref,
                    max_start_snap_m=self.config.start_max_snap_m,
                    max_start_vertical_m=self.config.start_max_vertical_m,
                    endpoint_edge_clearance_m=self.config.destination_edge_clearance_m,
                    min_route_distance_m=self.config.multi_polygon_min_distance_m,
                    max_route_distance_m=self.config.multi_polygon_max_distance_m,
                    require_zero_tile_crossings=True,
                    planner=overlay_planner,
                )
            except OrdinaryMultiPolygonRouteError as exc:
                raise AdapterError(str(exc)) from exc
            selected_plan_sha256 = _plan_hash(plan.to_dict())
            for candidate in candidates:
                if candidate.get("selected"):
                    candidate["selected_route_plan_sha256"] = selected_plan_sha256
            selected = selected_route(destination, resolution, plan, candidates)
            self._multi_polygon_candidates = [dict(item) for item in selected.candidates]
            self.timeline.append({
                "event": "ordinary_multi_polygon_candidates_considered",
                "t": self.monotonic(),
                "route_mode": self.config.route_mode,
                "candidate_routes": copy.deepcopy(self._multi_polygon_candidates),
                "selected_end_ref": int(plan.end_ref) if plan.end_ref is not None else None,
                "selected_route_plan_sha256": selected.plan_sha256,
                "selected_route_selection_id": selected.selection_id,
                "selection_reason": "fewest_polygon_transitions_then_closest_to_window_midpoint_then_reference",
                "distance_window_m": [
                    self.config.multi_polygon_min_distance_m,
                    self.config.multi_polygon_max_distance_m,
                ],
                "require_zero_tile_crossings": True,
            })
            return selected
        if self.config.route_mode == "ordinary_multi_tile":
            try:
                def overlay_planner(candidate_start: Vec3, candidate_end: Vec3, **planner_kwargs: Any) -> DetourRoutePlan:
                    planner_kwargs.pop("max_tile_crossings", None)
                    return self._plan_with_static_overlay(
                        topology,
                        candidate_start,
                        candidate_end,
                        max_tile_crossings=self.config.multi_tile_max_crossings,
                        **planner_kwargs,
                    )
                destination, resolution, plan, candidates = select_ordinary_multi_tile_destination(
                    topology.graph,
                    observation.position,
                    start_ref,
                    max_start_snap_m=self.config.start_max_snap_m,
                    max_start_vertical_m=self.config.start_max_vertical_m,
                    endpoint_edge_clearance_m=self.config.destination_edge_clearance_m,
                    min_route_distance_m=self.config.multi_tile_min_distance_m,
                    max_route_distance_m=self.config.multi_tile_max_distance_m,
                    max_tile_crossings=self.config.multi_tile_max_crossings,
                    planner=overlay_planner,
                )
            except OrdinaryMultiTileRouteError as exc:
                raise AdapterError(str(exc)) from exc
            selected_plan_sha256 = _plan_hash(plan.to_dict())
            for candidate in candidates:
                if candidate.get("selected"):
                    candidate["selected_route_plan_sha256"] = selected_plan_sha256
            selected = selected_route(destination, resolution, plan, candidates)
            self._multi_polygon_candidates = [dict(item) for item in selected.candidates]
            self.timeline.append({
                "event": "ordinary_multi_tile_candidates_considered",
                "t": self.monotonic(),
                "route_mode": self.config.route_mode,
                "candidate_routes": copy.deepcopy(self._multi_polygon_candidates),
                "selected_end_ref": int(plan.end_ref) if plan.end_ref is not None else None,
                "selected_route_plan_sha256": selected.plan_sha256,
                "selected_route_selection_id": selected.selection_id,
                "selection_reason": "fewest_tile_crossings_then_fewest_polygons_then_closest_to_40m_then_shortest_then_ref",
                "distance_window_m": [
                    self.config.multi_tile_min_distance_m,
                    self.config.multi_tile_max_distance_m,
                ],
                "max_tile_crossings": self.config.multi_tile_max_crossings,
            })
            return selected
        polygon = topology.graph.polygons.get(start_ref)
        if polygon is None or polygon.poly_type != DT_POLYTYPE_GROUND:
            raise AdapterError("start_polygon_not_ordinary_ground")
        start = observation.position
        centroid = polygon.centroid
        dx, dy = centroid.x - start.x, centroid.y - start.y
        base_angle = math.atan2(dy, dx) if math.hypot(dx, dy) > 1e-6 else 0.0
        angles = [base_angle + math.radians(step) for step in (0, 30, -30, 60, -60, 90, -90, 120, -120, 180)]
        distances = (4.0, 3.5, 4.5, 3.0, 5.0, 5.5, 6.0)
        for angle in angles:
            for distance_m in distances:
                requested = Vec3(
                    start.x + math.cos(angle) * distance_m * 100.0,
                    start.y + math.sin(angle) * distance_m * 100.0,
                    start.z,
                )
                resolution = topology.graph.resolve_point(
                    requested,
                    max_snap_distance_m=self.config.start_max_snap_m,
                    max_vertical_separation_m=self.config.start_max_vertical_m,
                )
                if not resolution.ok or resolution.selected_reference != start_ref:
                    continue
                if resolution.reason_code != "point_inside_polygon" or resolution.closest_point is None:
                    continue
                if _polygon_edge_clearance_m(resolution.closest_point, polygon) < self.config.destination_edge_clearance_m:
                    continue
                horizontal = _horizontal_distance_m(start, resolution.closest_point)
                if self.config.destination_min_distance_m <= horizontal <= self.config.destination_max_distance_m:
                    return requested, resolution
        raise AdapterError("no_defensible_same_polygon_destination")

    def _audit_route_collisions(
        self, observation: LiveObservation, plan: DetourRoutePlan, replan: Callable[[], DetourRoutePlan],
    ) -> DetourRoutePlan:
        """Sweep the plan with the game's collision and re-plan around what it finds.

        The navmesh omits individual props (live at Kalm: cafÃ© table sets,
        Ned's card table). Each fixed hit on such an actor becomes an overlay
        obstacle from its bounds; one hit again after a re-plan is grown. The
        loop ends when a sweep finds nothing new, and fails before any
        movement if it cannot.

        Automatic doors and gates are not walls (dynamic_gates.py): an actor
        whose name says door or gate, whose bounds moved or that vanished from
        where an earlier sweep saw it, or a thin leaf that seals a narrow neck
        the planner can otherwise not pass, is registered as a gate. It never
        enters the overlay; the next plan goes through it at a cost and
        carries it as a gate waypoint for the follower to wait at.
        """
        audit = self.route_collision_audit
        if audit is None:
            return plan
        from .route_sweep import (GROWTH_M, MAX_GROWN_RADIUS_M, classify_hit, obstacle_for_hit, obstacle_key,
                                  unknown_obstacle_hits)

        scope = getattr(self.static_obstacles, "scope", None)
        registry = getattr(self, "gate_registry", None)
        topology = getattr(self, "topology_session", None)
        map_id = _gate_map_id(topology) or str(getattr(observation, "map_id", "") or "")
        graph = getattr(topology, "graph", None)
        admitted: dict[str, Any] = {}
        rounds: list[dict[str, Any]] = []
        for _ in range(MAX_ROUTE_AUDIT_ROUNDS):
            points = [{"x": p.x, "y": p.y, "z": p.z} for p in plan.waypoints]
            try:
                sweep = audit(points)
            except Exception as exc:
                self.timeline.append({"event": "route_collision_audit_failed", "t": self.monotonic(),
                                      "error": str(exc), "rounds": rounds})
                raise AdapterError(f"route_collision_audit_failed:{exc}") from exc
            # A crossing's own leg is where the game's collision (a low girder)
            # is meant to be walked over: its hits are not obstacles.
            crossing_legs = _traversal_leg_indices(plan)
            hits = [hit for hit in unknown_obstacle_hits(sweep)
                    if hit.get("segment") not in crossing_legs and not _gimmick_contact(plan, hit)]
            gate_changes: list[dict[str, Any]] = []
            gates: tuple[Any, ...] = ()
            new_gates: tuple[Any, ...] = ()
            absorbed: tuple[tuple[str, Any], ...] = ()
            if registry is not None:
                gate_changes = self._reconcile_gates(map_id, sweep, points)
                triage = registry.triage(map_id, hits, plan=plan, graph=graph)
                hits, gates, new_gates = list(triage.walls), triage.gates, triage.new_gates
                absorbed = triage.absorbed
                for gate in gates:
                    # A gate is never a wall, even one admitted before it was known.
                    self.static_obstacles.discard_obstacle(obstacle_id_for(gate.actor))
                for _, hit in absorbed:
                    # Nor is the gate's own leaf read as a ledge or spot.
                    earlier = admitted.pop(str(obstacle_key(hit)), None)
                    if earlier is not None:
                        self.static_obstacles.discard_obstacle(earlier.obstacle_id)
            changed = []
            changed_hits = []
            for hit in hits:
                key = str(obstacle_key(hit))
                previous = admitted.get(key)
                if previous is None:
                    obstacle = obstacle_for_hit(hit, sequence=observation.sequence, t=self.monotonic())
                elif previous.radius_m + GROWTH_M <= MAX_GROWN_RADIUS_M:
                    obstacle = obstacle_for_hit(hit, sequence=observation.sequence, t=self.monotonic(),
                                                radius_m=previous.radius_m + GROWTH_M)
                else:
                    continue
                if any(item.obstacle_id == obstacle.obstacle_id for item in changed):
                    continue
                self.static_obstacles.admit_obstacle(scope=scope, obstacle=obstacle,
                                                     reason_code="route_sweep_contact")
                admitted[key] = obstacle
                changed.append(obstacle)
                changed_hits.append(hit)
            rounds.append({
                "waypoints": len(points),
                "route_length_m": plan.total_estimated_path_length_m,
                "hits": sum(len(segment.get("hits") or []) for segment in sweep.get("segments") or []),
                "unknown_obstacles": [{"key": obstacle_key(hit), "actor": hit.get("actor"),
                                       "kind": hit.get("kind"), "segment": hit.get("segment"),
                                       "impact": hit.get("impact"), "distance_cm": hit.get("distance_cm")}
                                      for hit in hits],
                "gates": [{"gate_id": gate.gate_id, "actor": gate.actor, "signals": list(gate.signals),
                           "new": gate in new_gates} for gate in gates],
                "gate_changes": gate_changes,
                "absorbed_by_gates": [{"gate_id": gate_id, "key": obstacle_key(hit), "kind": hit.get("kind"),
                                       "impact": hit.get("impact"), "rise_cm": hit.get("rise_cm")}
                                      for gate_id, hit in absorbed],
                "admitted_or_grown": [item.obstacle_id for item in changed],
                # Doors on the route are opened, not routed round.
                "doors": [{"segment": segment.get("index"), "actor": hit.get("actor"),
                           "bounds": hit.get("actor_bounds")}
                          for segment in sweep.get("segments") or [] for hit in segment.get("hits") or []
                          if classify_hit(hit) == "door"],
                "query": sweep.get("query"),
            })
            if not hits and not new_gates:
                self.timeline.append({"event": "route_collision_audit", "t": self.monotonic(),
                                      "outcome": "clear", "rounds": rounds})
                return plan
            if hits and not changed:
                last_hits = hits  # swept on the plan that stands
                break
            self._move_target_clear_of_obstacles(plan)
            try:
                plan = replan()
            except AdapterError as exc:
                # Walls that left no route: a thin leaf sealing a narrow neck
                # the route crossed is a gate after all. Plan again through it.
                promoted = (self._promote_sealed_gates(map_id, changed_hits, plan, exc, admitted)
                            if registry is not None and changed_hits else [])
                if not promoted:
                    # Keep what the sweeps found: the refusal alone does not say what sealed the route.
                    self.timeline.append({"event": "route_collision_audit", "t": self.monotonic(),
                                          "outcome": "no_route", "error": str(exc), "rounds": rounds})
                    raise
                rounds[-1]["promoted_gates"] = [gate.gate_id for gate in promoted]
                plan = replan()
        else:
            # The rounds ran out after a re-plan nobody has swept yet.
            try:
                crossing_legs = _traversal_leg_indices(plan)
                last_hits = [
                    hit for hit in unknown_obstacle_hits(audit([{"x": p.x, "y": p.y, "z": p.z} for p in plan.waypoints]))
                    if hit.get("segment") not in crossing_legs and not _gimmick_contact(plan, hit)
                ]
            except Exception:
                last_hits = None
            if last_hits is not None:
                rounds.append({"waypoints": len(plan.waypoints), "route_length_m": plan.total_estimated_path_length_m,
                               "unknown_obstacles": [{"key": obstacle_key(hit), "actor": hit.get("actor"),
                                                      "kind": hit.get("kind")} for hit in last_hits]})
        self.timeline.append({"event": "route_collision_audit", "t": self.monotonic(),
                              "outcome": "unresolved", "rounds": rounds})
        raise AdapterError("route_collision_audit_unresolved")

    def _clear_target_of_known_obstacles(self, topology: TopologySession, start: Vec3) -> None:
        """Move a target standing in an already known obstacle before planning.

        The collision audit moves the target once it learns the obstacle,
        but a recovery starts a fresh adapter with the original target while
        the overlay keeps what the audit learned (live: Ned inside his padded
        card table), and no plan to it can exist. The target moves back
        along the bare navmesh route instead, as the audit would.
        """
        if self.route_collision_audit is None or self.route_target is None:
            return
        try:
            known = self.static_obstacles.planning_snapshot(now_t=self.monotonic()).obstacles
        except (StaticObstaclePlanningError, StaticObstacleScopeError):
            return
        if not known:
            return
        bare = topology.graph.plan(
            start,
            self.route_target,
            max_expansions=self.config.cached_topology_max_expansions,
            max_waypoints=self.config.cached_topology_max_waypoints,
            max_snap_distance_m=self.config.start_max_snap_m,
            max_vertical_separation_m=self.config.start_max_vertical_m,
            portal_corner_inset_m=self.config.portal_corner_inset_m,
            routing_objective=self.config.routing_objective,
        )
        if bare.status == "complete":
            self._move_target_clear_of_obstacles(bare)

    def _move_target_clear_of_obstacles(self, plan: DetourRoutePlan) -> None:
        """Stop short of an obstacle the target stands in or against.

        An NPC seated at a table (live: Ned at his card table) is inside the
        table's footprint once it is padded for Cloud's clearance, and no
        route can end there. The target moves back along the route to the
        first point clear of every obstacle; facing and talking cover the
        rest.
        """
        target = self.route_target
        if target is None:
            return
        obstacles = tuple(self.static_obstacles.planning_snapshot().obstacles)

        def clear(point: Vec3) -> bool:
            return all(obstacle_segment_clearance_m(point, point, item) > ROUTE_TARGET_OBSTACLE_MARGIN_M
                       for item in obstacles)

        if clear(target):
            return
        points = list(plan.waypoints)
        walked = 0.0
        for index in range(len(points) - 1, 0, -1):
            first, second = points[index - 1], points[index]
            length = math.hypot(second.x - first.x, second.y - first.y)
            steps = max(1, int(length / 10.0))
            for step in range(1, steps + 1):
                t = 1.0 - step / steps
                point = Vec3(first.x + (second.x - first.x) * t, first.y + (second.y - first.y) * t,
                             first.z + (second.z - first.z) * t)
                if clear(point):
                    moved = walked + length * step / steps
                    self.route_target = point
                    self.timeline.append({
                        "event": "route_target_moved_clear_of_obstacle",
                        "t": self.monotonic(),
                        "original_target": _point_dict(target),
                        "target": _point_dict(point),
                        "moved_m": moved / 100.0,
                    })
                    return
            walked += length
        raise AdapterError("route_target_enclosed_by_obstacles")

    def _resolve_route_target(self, topology: TopologySession) -> Any:
        """Resolve the route target, at feet height if not inside as given.

        Missions give targets at Cloud's actor-origin height, 88 cm above his
        feet, which leaves about 12 cm of the 1 m vertical tolerance: live at
        Neil's table (#174) the floor was 1.05 m below and the target snapped
        to a neighbouring polygon, refusing every route. A target inside a
        polygon at feet height is routed to there, and the timeline says so.
        """
        resolution = topology.graph.resolve_point(
            self.route_target,
            max_snap_distance_m=self.config.start_max_snap_m,
            max_vertical_separation_m=self.config.start_max_vertical_m,
        )
        if resolution.ok and resolution.reason_code == "point_inside_polygon":
            return resolution
        lowered = Vec3(self.route_target.x, self.route_target.y,
                       self.route_target.z - ROUTE_TARGET_ACTOR_ORIGIN_ABOVE_FEET_CM)
        at_feet = topology.graph.resolve_point(
            lowered,
            max_snap_distance_m=self.config.start_max_snap_m,
            max_vertical_separation_m=self.config.start_max_vertical_m,
        )
        if not at_feet.ok or at_feet.reason_code != "point_inside_polygon":
            return resolution
        self.timeline.append({
            "event": "route_target_resolved_at_feet_height",
            "t": self.monotonic(),
            "original_target": _point_dict(self.route_target),
            "target": _point_dict(lowered),
            "original_reason_code": resolution.reason_code,
            "end_ref": at_feet.selected_reference,
            "movement_command_sent": False,
        })
        self.route_target = lowered
        return at_feet

    def _project_capture_admission_target(self, topology: TopologySession, resolution: Any,
                                          start: Vec3) -> Any:
        """Use reachable floor near an off-mesh NPC for movement-free admission.

        The nearest polygon may be an isolated counter platform. A nearby
        linked floor within 4 m can evidence the capture instead. Start/floor
        bounds, collision audits and independent route validation still apply.
        This endpoint is never used to move Cloud to the NPC.
        """
        if resolution.reason_code == "point_inside_polygon":
            return resolution
        candidate = topology.graph.resolve_point(
            self.route_target, max_snap_distance_m=CAPTURE_TARGET_REACH_M,
            max_vertical_separation_m=self.config.start_max_vertical_m)
        if not candidate.ok or candidate.closest_point is None:
            return resolution
        start_resolution = topology.graph.resolve_point(
            start, max_snap_distance_m=self.config.start_max_snap_m,
            max_vertical_separation_m=self.config.start_max_vertical_m)
        if not start_on_navmesh(start_resolution):
            return resolution
        point = candidate.closest_point
        projected = topology.graph.resolve_point(
            point,
            max_snap_distance_m=self.config.start_max_snap_m,
            max_vertical_separation_m=self.config.start_max_vertical_m,
        )
        if (not projected.ok or projected.reason_code != "point_inside_polygon"
                or projected.selected_reference != candidate.selected_reference):
            raise AdapterError("capture_admission_target_projection_not_proven")
        direct = topology.graph.plan(start, point, start_resolution_override=start_resolution,
                                     end_resolution_override=projected)
        if direct.status != "complete":
            point = topology.graph.nearest_reachable_point(
                start, self.route_target, max_horizontal_m=CAPTURE_TARGET_REACH_M,
                max_vertical_m=self.config.start_max_vertical_m, start_resolution=start_resolution)
            if point is None:
                return resolution
            projected = topology.graph.resolve_point(
                point, max_snap_distance_m=self.config.start_max_snap_m,
                max_vertical_separation_m=self.config.start_max_vertical_m)
            if not projected.ok or projected.reason_code != "point_inside_polygon":
                raise AdapterError("capture_admission_target_projection_not_proven")
        original_target = self.route_target
        self.route_target = point
        self.timeline.append({
            "event": "capture_admission_target_projected",
            "t": self.monotonic(),
            "original_target": _point_dict(original_target),
            "target": _point_dict(self.route_target),
            "horizontal_distance_m": _horizontal_distance_m(original_target, point),
            "vertical_distance_m": abs(original_target.z - point.z) / 100.0,
            "max_horizontal_distance_m": CAPTURE_TARGET_REACH_M,
            "end_ref": projected.selected_reference,
            "movement_command_sent": False,
        })
        return projected

    def _choose_admission_destination(
        self,
        topology: TopologySession,
        observation: LiveObservation,
        start_ref: int,
    ) -> Any:
        """Choose the route, falling back through ``admission_route_fallback``."""
        original = self.config
        try:
            return self._choose_destination(topology, observation, start_ref)
        except AdapterError as first:
            if not original.admission_route_fallback:
                raise
            failures = [{"route_mode": original.route_mode, "error": str(first)}]
            last_failure = first
            for mode in original.admission_route_fallback:
                if mode == original.route_mode:
                    continue
                self.config = replace(original, route_mode=mode)
                try:
                    selection = self._choose_destination(topology, observation, start_ref)
                except AdapterError as exc:
                    last_failure = exc
                    failures.append({"route_mode": mode, "error": str(exc)})
                    continue
                self.timeline.append({
                    "event": "admission_route_mode_fallback",
                    "t": self.monotonic(),
                    "route_mode": mode,
                    "failed_modes": failures,
                })
                return selection
            self.config = original
            self.timeline.append({
                "event": "admission_route_modes_failed",
                "t": self.monotonic(),
                "failed_modes": failures,
            })
            raise last_failure

    def _bind_selected_route_plan(
        self,
        selected: SelectedRoute,
        topology: TopologySession,
        observation: LiveObservation,
        start_ref: int,
    ) -> Mapping[str, Any]:
        """Bind the current selector result without regenerating its path."""

        if selected.topology_session_id != topology.session_id or selected.topology_fingerprint != topology.fingerprint:
            raise AdapterError("selected_route_plan_topology_or_session_mismatch")
        plan = selected.plan
        actual_sha = _plan_hash(plan)
        if actual_sha != selected.plan_sha256:
            raise AdapterError("selected_route_plan_identity_mismatch")
        if plan.get("status") != "complete" or plan.get("movement_sent") is not False:
            raise AdapterError("selected_route_plan_not_movement_free_complete")
        if int(plan.get("start_ref", -1)) != int(start_ref):
            raise AdapterError("selected_route_plan_start_ref_mismatch")
        destination_ref = getattr(selected.resolution, "selected_reference", None)
        if destination_ref is None or int(plan.get("end_ref", -1)) != int(destination_ref):
            raise AdapterError("selected_route_plan_end_ref_mismatch")
        requested_start = _parse_point(plan.get("requested_start"))
        requested_end = _parse_point(plan.get("requested_end"))
        if _distance_m(requested_start, observation.position) > 1e-3:
            raise AdapterError("selected_route_plan_start_observation_mismatch")
        if _distance_m(requested_end, selected.destination) > 1e-3:
            raise AdapterError("selected_route_plan_destination_mismatch")
        start_resolution = plan.get("start_resolution")
        if not isinstance(start_resolution, Mapping):
            raise AdapterError("selected_route_plan_start_resolution_missing")
        if int(start_resolution.get("selected_ref", -1)) != int(start_ref):
            raise AdapterError("selected_route_plan_start_resolution_mismatch")
        if float(start_resolution.get("horizontal_distance_m", math.inf)) > self.config.start_max_snap_m:
            raise AdapterError("selected_route_plan_start_snap_exceeds_bound")
        if float(start_resolution.get("vertical_distance_m", math.inf)) > self.config.start_max_vertical_m:
            raise AdapterError("selected_route_plan_start_vertical_exceeds_bound")
        if self.start_resolution is None or self.start_resolution.selected_reference != int(start_ref):
            raise AdapterError("selected_route_plan_current_start_mismatch")
        self._selected_route_plan_sha256 = selected.plan_sha256
        self._adapter_route_plan_sha256 = actual_sha
        self.timeline.append({
            "event": "selected_route_plan_bound",
            "t": self.monotonic(),
            "selection_id": selected.selection_id,
            "selected_route_plan_sha256": selected.plan_sha256,
            "adapter_route_plan_sha256": actual_sha,
            "start_ref": int(plan["start_ref"]),
            "end_ref": int(plan["end_ref"]),
            "polygon_corridor": list(plan.get("polygon_corridor") or []),
            "route_length_m": plan.get("total_estimated_path_length_m"),
            "tile_crossings": (plan.get("validation") or {}).get("tile_crossings"),
        })
        return plan

    def _validate_same_polygon_route(
        self,
        plan: Mapping[str, Any],
        topology: TopologySession,
        start: LiveObservation,
    ) -> tuple[Vec3, Vec3]:
        """Validate route invariants shared by initial and recovery routes."""

        if plan.get("status") != "complete" or plan.get("reason_code") != "route_plan_complete":
            raise AdapterError("planner_did_not_return_complete_route")
        if plan.get("movement_sent") is not False:
            raise AdapterError("planner_route_not_movement_free")
        corridor = plan.get("polygon_corridor")
        portals = plan.get("portals")
        if not isinstance(corridor, list) or len(corridor) != 1:
            raise AdapterError("first_route_not_same_polygon")
        if not isinstance(portals, list) or len(portals) != 2:
            raise AdapterError("first_route_has_unexpected_transitions")
        ref = int(corridor[0])
        polygon = topology.graph.polygons.get(ref)
        if polygon is None or polygon.poly_type != DT_POLYTYPE_GROUND:
            raise AdapterError("first_route_polygon_not_ordinary_ground")
        for portal in portals:
            if not isinstance(portal, Mapping) or int(portal.get("side", -1)) not in ALLOWED_GROUND_SIDES:
                raise AdapterError("first_route_contains_special_transition")
        validation = plan.get("validation")
        if not isinstance(validation, Mapping) or validation.get("tile_crossings") != 0:
            raise AdapterError("first_route_has_tile_crossing")
        start_resolution = plan.get("start_resolution")
        end_resolution = plan.get("end_resolution")
        if not isinstance(start_resolution, Mapping) or not isinstance(end_resolution, Mapping):
            raise AdapterError("route_resolution_evidence_missing")
        if start_resolution.get("reason_code") != "point_inside_polygon" or end_resolution.get("reason_code") != "point_inside_polygon":
            raise AdapterError("route_endpoint_not_inside_polygon")
        if float(start_resolution.get("horizontal_distance_m", math.inf)) > self.config.start_max_snap_m:
            raise AdapterError("live_start_snap_exceeds_bound")
        if float(start_resolution.get("vertical_distance_m", math.inf)) > self.config.start_max_vertical_m:
            raise AdapterError("live_start_vertical_separation_exceeds_bound")
        resolved_start = _parse_point(plan.get("resolved_start"))
        resolved_end = _parse_point(plan.get("resolved_end"))
        if not all(_is_finite_point(point) for point in plan.get("waypoints", [])):
            raise AdapterError("route_waypoints_non_finite")
        if not start.actor_id:
            raise AdapterError("live_cloud_actor_missing")
        return resolved_start, resolved_end

    def _validate_cached_topology_route(
        self,
        plan: Mapping[str, Any],
        topology: TopologySession,
        start: LiveObservation,
        *,
        original_destination: Vec3 | None = None,
    ) -> tuple[Vec3, Vec3]:
        """Validate an arbitrary movement-free route against this graph session."""

        if plan.get("status") != "complete" or plan.get("reason_code") != "route_plan_complete":
            raise AdapterError("cached_topology_planner_did_not_return_complete_route")
        if plan.get("movement_sent") is not False:
            raise AdapterError("cached_topology_route_not_movement_free")
        corridor = plan.get("polygon_corridor")
        portals = plan.get("portals")
        if not isinstance(corridor, list) or not corridor:
            raise AdapterError("cached_topology_corridor_missing")
        if not isinstance(portals, list) or len(portals) != len(corridor) + 1:
            raise AdapterError("cached_topology_portal_count_mismatch")
        corridor_refs: list[int] = []
        for raw_ref in corridor:
            if not isinstance(raw_ref, int) or isinstance(raw_ref, bool):
                raise AdapterError("cached_topology_corridor_reference_invalid")
            polygon = topology.graph.polygons.get(int(raw_ref))
            if polygon is None or polygon.poly_type != DT_POLYTYPE_GROUND:
                raise AdapterError("cached_topology_corridor_not_ordinary_ground")
            if topology.scope_identity is not None and int(raw_ref) not in set(topology.scope_polygon_refs):
                raise AdapterError("cached_topology_corridor_outside_partial_scope")
            corridor_refs.append(int(raw_ref))
        for index, raw_portal in enumerate(portals):
            if not isinstance(raw_portal, Mapping):
                raise AdapterError("cached_topology_portal_invalid")
            try:
                from_ref = int(raw_portal["from_ref"])
                to_ref = int(raw_portal["to_ref"])
                side = int(raw_portal["side"])
            except (KeyError, TypeError, ValueError) as exc:
                raise AdapterError("cached_topology_portal_invalid") from exc
            if raw_portal.get("kind") == PORTAL_KIND_TRAVERSAL:
                if index == 0 or index == len(portals) - 1:
                    raise AdapterError("cached_topology_portal_invalid")
                self._verify_traversal_portal(topology, raw_portal, corridor_refs[index - 1], corridor_refs[index])
                continue
            if side not in ALLOWED_GROUND_SIDES:
                raise AdapterError("cached_topology_special_transition")
            if index == 0:
                expected_from = expected_to = corridor_refs[0]
            elif index == len(portals) - 1:
                expected_from = expected_to = corridor_refs[-1]
            else:
                expected_from = corridor_refs[index - 1]
                expected_to = corridor_refs[index]
                source_polygon = topology.graph.polygons[expected_from]
                link_ref = raw_portal.get("link_ref")
                source_edge = raw_portal.get("source_edge")
                if not isinstance(link_ref, int) or isinstance(link_ref, bool):
                    raise AdapterError("cached_topology_portal_link_missing")
                if not isinstance(source_edge, int) or isinstance(source_edge, bool):
                    raise AdapterError("cached_topology_portal_edge_missing")
                if topology.scope_identity is not None:
                    try:
                        scope_signature = corridor_portal_identity(raw_portal)
                    except TopologyCacheError as exc:
                        raise AdapterError("cached_topology_portal_scope_identity_invalid") from exc
                    if scope_signature not in topology.scope_transition_signatures:
                        raise AdapterError("cached_topology_transition_outside_partial_scope")
                if not any(
                    link.reference == expected_to
                    and link.reference == link_ref
                    and link.edge == source_edge
                    for link in source_polygon.links
                ):
                    raise AdapterError("cached_topology_portal_link_unproven")
            if from_ref != expected_from or to_ref != expected_to:
                raise AdapterError("cached_topology_portal_corridor_mismatch")
        validation = plan.get("validation")
        if not isinstance(validation, Mapping):
            raise AdapterError("cached_topology_validation_evidence_missing")
        for key in (
            "corridor_refs_present",
            "transition_portals_valid",
            "portal_links_proven",
            "waypoints_finite",
            "waypoint_sequence_nonempty",
            "waypoint_start_matches_resolved",
            "waypoint_end_matches_resolved",
        ):
            if validation.get(key) is not True:
                raise AdapterError(f"cached_topology_validation_failed:{key}")
        waypoints = plan.get("waypoints")
        if not isinstance(waypoints, list) or not waypoints:
            raise AdapterError("cached_topology_waypoints_missing")
        try:
            parsed_waypoints = tuple(_parse_point(point) for point in waypoints)
            resolved_start = _parse_point(plan.get("resolved_start"))
            resolved_end = _parse_point(plan.get("resolved_end"))
        except (KeyError, TypeError, ValueError) as exc:
            raise AdapterError("cached_topology_waypoints_non_finite") from exc
        if not all(_is_finite_point(point) for point in parsed_waypoints):
            raise AdapterError("cached_topology_waypoints_non_finite")
        if _horizontal_distance_m(resolved_start, start.position) > self.config.start_max_snap_m:
            raise AdapterError("cached_topology_start_snap_exceeds_bound")
        if abs(resolved_start.z - start.position.z) / 100.0 > self.config.start_max_vertical_m:
            raise AdapterError("cached_topology_start_vertical_exceeds_bound")
        if self.start_resolution is None or int(plan.get("start_ref", -1)) != int(self.start_resolution.selected_reference):
            raise AdapterError("cached_topology_start_ref_mismatch")
        start_resolution = plan.get("start_resolution")
        end_resolution = plan.get("end_resolution")
        if not isinstance(start_resolution, Mapping) or not isinstance(end_resolution, Mapping):
            raise AdapterError("cached_topology_endpoint_resolution_missing")
        if int(start_resolution.get("selected_ref", -1)) != int(plan.get("start_ref", -2)):
            raise AdapterError("cached_topology_start_resolution_mismatch")
        if int(end_resolution.get("selected_ref", -1)) != int(plan.get("end_ref", -2)):
            raise AdapterError("cached_topology_end_resolution_mismatch")
        for label, resolution in (("start", start_resolution), ("end", end_resolution)):
            if resolution.get("reason_code") != "point_inside_polygon":
                raise AdapterError(f"cached_topology_{label}_not_inside_polygon")
            if float(resolution.get("horizontal_distance_m", math.inf)) > self.config.start_max_snap_m:
                raise AdapterError(f"cached_topology_{label}_snap_exceeds_bound")
            if float(resolution.get("vertical_distance_m", math.inf)) > self.config.start_max_vertical_m:
                raise AdapterError(f"cached_topology_{label}_vertical_exceeds_bound")
        if original_destination is not None and _distance_m(resolved_end, original_destination) > 1e-3:
            raise AdapterError("cached_topology_recovery_destination_changed")
        return resolved_start, resolved_end

    def _validate_first_route(self, plan: Mapping[str, Any], topology: TopologySession, start: LiveObservation) -> None:
        """Apply the initial bounded destination-selection policy."""

        if self.config.route_mode == "cached_topology":
            self._validate_cached_topology_route(plan, topology, start)
            return

        if self.config.route_mode == "ordinary_multi_polygon":
            try:
                _, _, evidence = validate_ordinary_multi_polygon_route(
                    plan,
                    topology.graph,
                    start.position,
                    start_ref=int(self.start_resolution.selected_reference) if self.start_resolution is not None else int(plan["start_ref"]),
                    max_start_snap_m=self.config.start_max_snap_m,
                    max_start_vertical_m=self.config.start_max_vertical_m,
                    endpoint_edge_clearance_m=self.config.destination_edge_clearance_m,
                    min_route_distance_m=self.config.multi_polygon_min_distance_m,
                    max_route_distance_m=self.config.multi_polygon_max_distance_m,
                    require_zero_tile_crossings=True,
                )
            except OrdinaryMultiPolygonRouteError as exc:
                raise AdapterError(str(exc)) from exc
            self._multi_polygon_route_evidence = dict(evidence)
            self.timeline.append({
                "event": "ordinary_multi_polygon_route_invariants_passed",
                "t": self.monotonic(),
                "route_evidence": copy.deepcopy(self._multi_polygon_route_evidence),
            })
            return
        if self.config.route_mode == "ordinary_multi_tile":
            try:
                _, _, evidence = validate_ordinary_multi_tile_route(
                    plan,
                    topology.graph,
                    start.position,
                    start_ref=int(self.start_resolution.selected_reference) if self.start_resolution is not None else int(plan["start_ref"]),
                    max_start_snap_m=self.config.start_max_snap_m,
                    max_start_vertical_m=self.config.start_max_vertical_m,
                    endpoint_edge_clearance_m=self.config.destination_edge_clearance_m,
                    min_route_distance_m=self.config.multi_tile_min_distance_m,
                    max_route_distance_m=self.config.multi_tile_max_distance_m,
                    max_tile_crossings=self.config.multi_tile_max_crossings,
                )
            except OrdinaryMultiTileRouteError as exc:
                raise AdapterError(str(exc)) from exc
            self._multi_polygon_route_evidence = dict(evidence)
            self.timeline.append({
                "event": "ordinary_multi_tile_route_invariants_passed",
                "t": self.monotonic(),
                "route_evidence": copy.deepcopy(self._multi_polygon_route_evidence),
                "cycle_budget": derive_multi_tile_cycle_budget(
                    float(evidence["geodesic_length_m"]),
                    absolute_max_cycles=self.config.multi_tile_absolute_max_cycles,
                ),
            })
            return

        resolved_start, resolved_end = self._validate_same_polygon_route(plan, topology, start)
        distance = _horizontal_distance_m(resolved_start, resolved_end)
        if not self.config.destination_min_distance_m <= distance <= self.config.destination_max_distance_m:
            raise AdapterError("first_route_distance_outside_bound")

    def _validate_recovery_route(
        self,
        plan: Mapping[str, Any],
        topology: TopologySession,
        start: LiveObservation,
        original_destination: Vec3,
    ) -> tuple[Vec3, Vec3]:
        """Validate a cached-topology residual route without an artificial minimum."""

        if self.config.route_mode == "cached_topology":
            return self._validate_cached_topology_route(
                plan,
                topology,
                start,
                original_destination=original_destination,
            )
        if self.config.route_mode == "ordinary_multi_polygon":
            try:
                resolved_start, resolved_end, evidence = validate_ordinary_multi_polygon_route(
                    plan,
                    topology.graph,
                    start.position,
                    start_ref=int(self.start_resolution.selected_reference) if self.start_resolution is not None else int(plan["start_ref"]),
                    max_start_snap_m=self.config.start_max_snap_m,
                    max_start_vertical_m=self.config.start_max_vertical_m,
                    endpoint_edge_clearance_m=self.config.destination_edge_clearance_m,
                    # This is a residual route to an already-authorized
                    # destination, so the initial destination-window minimum
                    # is not reapplied.  The zero-crossing policy remains.
                    min_route_distance_m=0.0,
                    max_route_distance_m=self.config.multi_polygon_max_distance_m,
                    require_zero_tile_crossings=True,
                )
            except OrdinaryMultiPolygonRouteError as exc:
                raise AdapterError(str(exc)) from exc
            self._multi_polygon_route_evidence = dict(evidence)
            if _distance_m(resolved_end, original_destination) > 1e-3:
                raise AdapterError("recovery_destination_changed")
            return resolved_start, resolved_end
        if self.config.route_mode == "ordinary_multi_tile":
            try:
                resolved_start, resolved_end, evidence = validate_ordinary_multi_tile_route(
                    plan,
                    topology.graph,
                    start.position,
                    start_ref=int(self.start_resolution.selected_reference) if self.start_resolution is not None else int(plan["start_ref"]),
                    max_start_snap_m=self.config.start_max_snap_m,
                    max_start_vertical_m=self.config.start_max_vertical_m,
                    endpoint_edge_clearance_m=self.config.destination_edge_clearance_m,
                    min_route_distance_m=0.0,
                    max_route_distance_m=self.config.multi_tile_max_distance_m,
                    max_tile_crossings=self.config.multi_tile_max_crossings,
                )
            except OrdinaryMultiTileRouteError as exc:
                raise AdapterError(str(exc)) from exc
            self._multi_polygon_route_evidence = dict(evidence)
            if _distance_m(resolved_end, original_destination) > 1e-3:
                raise AdapterError("recovery_destination_changed")
            return resolved_start, resolved_end
        resolved_start, resolved_end = self._validate_same_polygon_route(plan, topology, start)
        distance = _horizontal_distance_m(resolved_start, resolved_end)
        if distance > self.config.destination_max_distance_m:
            raise AdapterError("recovery_route_distance_exceeds_bound")
        if not all(
            math.isclose(getattr(resolved_end, axis), getattr(original_destination, axis), rel_tol=0.0, abs_tol=1e-6)
            for axis in ("x", "y", "z")
        ):
            raise AdapterError("recovery_destination_changed")
        return resolved_start, resolved_end

    def _validate_plan_identity(self, plan: Mapping[str, Any], topology: TopologySession, observation: LiveObservation) -> PlanAuthorization:
        start = _parse_point(plan.get("resolved_start"))
        end = _parse_point(plan.get("resolved_end"))
        start_ref, end_ref = int(plan["start_ref"]), int(plan["end_ref"])
        digest = _plan_hash(plan)
        return PlanAuthorization(
            session_id=observation.session_id,
            world_id=observation.world_id,
            map_id=observation.map_id,
            topology_fingerprint=topology.fingerprint,
            route_plan_sha256=digest,
            start_ref=start_ref,
            end_ref=end_ref,
            resolved_start=start,
            resolved_end=end,
            created_monotonic=self.monotonic(),
            route_mode=self.config.route_mode,
            scope_identity=topology.scope_identity,
        )

    def _settle_preflight_pose(self, previous: LiveObservation, current: LiveObservation,
                               *, require_lease: bool, phase: str = "preflight") -> LiveObservation:
        """Bind a new route after measured traversal/deceleration has settled.

        A neutral controller and a grounded flag do not imply a stationary
        character: scripted terrain traversal can continue with zero reported
        velocity. Use advancing physical poses instead of a velocity guess.
        """
        # Arrival confirmation must account for residual motion even when
        # telemetry reports the actor origin rather than capsule-bottom pose.
        # Preflight/replan settling keeps the historical capsule-bottom gate;
        # arrival settling additionally uses a published velocity sample when
        # one exists. If velocity is unavailable, the existing position-only
        # arrival proof remains unchanged rather than inventing a speed estimate.
        if (
            current.position_reference != "upright_capsule_bottom"
            and phase != "arrival"
        ):
            return current
        if (
            phase == "arrival"
            and previous.velocity_cm_s is None
            and current.velocity_cm_s is None
        ):
            return current
        deadline = self.monotonic() + self.config.readiness_recovery_timeout_seconds
        settle_attempts = 0
        while True:
            failures = self._check_preconditions(current, require_lease=require_lease)
            if not current.telemetry_gameplay_ready:
                failures.append("telemetry_gameplay_not_ready")
            if current.process_id != previous.process_id:
                raise AdapterError("game_process_changed")
            if failures and not self._is_readiness_only_failure(failures):
                raise AdapterError(phase + "_pose_not_ready:" + ";".join(failures))
            displacement = _distance_m(previous.position, current.position)
            stable = (not failures and previous.telemetry_gameplay_ready and previous.controllable
                      and current.grounded is True and previous.grounded is True
                      and current.sequence > previous.sequence and displacement <= 0.05)
            if phase == "arrival":
                # Repeated poses alone can precede delayed residual motion.
                # Require the published physical velocity to be settled too.
                stable = stable and all(
                    sample.velocity_cm_s is not None
                    and math.isfinite(sample.velocity_cm_s.x)
                    and math.isfinite(sample.velocity_cm_s.y)
                    and math.hypot(sample.velocity_cm_s.x, sample.velocity_cm_s.y) <= 50.0
                    for sample in (previous, current)
                )
            self.timeline.append({"event": phase + "_pose_settling", "t": self.monotonic(),
                "sequence": current.sequence, "previous_sequence": previous.sequence,
                "position": _point_dict(current.position), "grounded": current.grounded,
                "displacement_m": displacement, "stable": stable, "failures": failures,
                "velocity_cm_s": _point_dict(current.velocity_cm_s) if current.velocity_cm_s else None,
                "movement_command_sent": False})
            if stable:
                return current
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise AdapterError(phase + "_pose_did_not_settle")
            # Readiness can be invalidated while waiting for residual motion.
            # Rebuild its proof without leaving neutral or resetting this deadline.
            # Reserve the service's minimum one-second slice plus a fresh sample.
            settle = getattr(self.telemetry, "wait_for_settled_gameplay", None)
            if (failures and callable(settle) and settle_attempts < 3
                    and remaining >= 1.0 + self.config.telemetry_poll_seconds):
                settle_attempts += 1
                timeout = min(self.config.settled_gameplay_timeout_seconds,
                              remaining - self.config.telemetry_poll_seconds)
                event = {"event": phase + "_pose_readiness_reacquire", "t": self.monotonic(),
                         "sequence": current.sequence, "attempt": settle_attempts,
                         "timeout_seconds": timeout, "movement_command_sent": False}
                try:
                    event["result"] = settle(timeout,
                        max_telemetry_age_seconds=self.config.max_telemetry_age_seconds)
                except Exception as exc:
                    event["error"] = _sanitize_exception_message(exc)
                self.timeline.append(event)
            previous = current
            current = self._require_observation(wait_for_fresh=True,
                after_sequence=current.sequence, timeout_seconds=max(0.0, deadline - self.monotonic()))

    def _capture_preflight_render(self, observation: LiveObservation, *, require_lease: bool) -> LiveObservation:
        """Reacquire stale capture evidence locally; never reinterpret a refused frame."""
        deadline = self.monotonic() + self.config.readiness_recovery_timeout_seconds
        for attempt in range(3):
            if self.render_evidence is None:
                self.render_confirmation = RenderedGameplayEvidence(ready=False, reason="render_evidence_source_missing")
                break
            try:
                self.render_confirmation = self.render_evidence.observe(observation)
            except Exception as exc:
                self.render_confirmation = RenderedGameplayEvidence(ready=False,
                    reason="render_evidence_exception", capture={"error": str(exc)})
            if _render_evidence_confirms_gameplay(observation, self.render_confirmation,
                    max_telemetry_age_seconds=self.config.max_telemetry_age_seconds):
                break
            evidence = self.render_confirmation
            capture = evidence.capture if isinstance(evidence.capture, Mapping) else {}
            try:
                age = float(capture.get("telemetry_age_seconds"))
            except (TypeError, ValueError):
                break
            if (attempt == 2 or not math.isfinite(age)
                    or age <= self.config.max_telemetry_age_seconds
                    or evidence.reason != "render_classifier_non_gameplay"
                    or not _same_process_id(capture.get("pid"), observation.process_id)
                    or capture.get("map_id") != observation.map_id
                    or self._check_preconditions(observation, require_lease=require_lease)):
                break
            remaining = deadline - self.monotonic()
            if remaining < 1.0 + self.config.telemetry_poll_seconds:
                break
            self.timeline.append({"event": "stale_preflight_render_reacquire", "attempt": attempt + 1,
                "sequence": observation.sequence, "capture": dict(capture), "movement_command_sent": False})
            settle = getattr(self.telemetry, "wait_for_settled_gameplay", None)
            if callable(settle):
                try:
                    settle(remaining - self.config.telemetry_poll_seconds,
                           max_telemetry_age_seconds=self.config.max_telemetry_age_seconds)
                except Exception as exc:
                    self.timeline.append({"event": "stale_render_readiness_wait_failed", "error": str(exc)})
                    break
            previous = observation
            observation = self._require_observation(wait_for_fresh=True,
                after_sequence=observation.sequence, timeout_seconds=max(0.0, deadline - self.monotonic()))
            if observation.process_id != previous.process_id:
                raise AdapterError("game_process_changed")
        return observation

    def preflight(self, *, require_lease: bool = True) -> dict[str, Any]:
        """Run every movement gate, leaving the actuator neutral."""
        self._set_state(AdapterState.OBSERVING, "preflight_started")
        self.timeline = []
        self._last_observation = None
        self._last_command_sequence = None
        self.topology_session = None
        self.route_plan = None
        self.validator_result = None
        self.start_resolution = None
        self.destination_resolution = None
        self.follower = None
        self.authorization = None
        self.authorization_proof = None
        self.render_confirmation = None
        self._multi_polygon_candidates = []
        self._multi_polygon_route_evidence = None
        self._selected_route = None
        self._selected_route_plan_sha256 = None
        self._adapter_route_plan_sha256 = None
        self._independent_validator_route_plan_sha256 = None
        self._route_traversals = []
        self._last_resolved_polygon_ref = None
        self._last_corridor_sample = None
        self._route_local_corridor_index = None
        self._route_local_corridor_ref = None
        self._route_local_last_position = None
        self._route_local_last_sequence = None
        self._last_route_along_m = None
        self._run_started_t = None
        self._route_sample_cache = {}
        self._active_post_command_timing = None
        self._active_post_command_route_sample = None
        self._static_no_progress_streak = 0
        self._static_last_evidence_command = None
        self._static_pending_discovery = None
        self._static_pending_learning_kwargs = None
        self._static_last_learning = None
        self._preflight_stage = "preflight_started"
        self.preflight_exception = None
        try:
            self._preflight_stage = "preflight_neutral_proof"
            disarm_capture = getattr(self.topology, "disarm_capture", None)
            if callable(disarm_capture):
                disarm_capture()
                self.timeline.append({
                    "event": "topology_capture_disarmed_before_readiness_check",
                    "t": self.monotonic(),
                })
            if not self._neutralize("preflight_neutral_proof"):
                raise AdapterError("actuator_neutrality_unconfirmed")
            self._preflight_stage = "initial_observation"
            observation = self._require_observation(wait_for_fresh=True)
            neutral_observation = observation
            failures: list[str] = []
            if observation.telemetry_gameplay_ready is not True:
                failures.append("telemetry_gameplay_not_ready")
            # Once telemetry claims gameplay readiness, always obtain the
            # independent raw render-boundary result. This keeps the exact
            # disagreement classification deterministic even if another
            # later movement precondition (for example, lease state) also
            # fails.
            if observation.telemetry_gameplay_ready is True:
                observation = self._capture_preflight_render(observation, require_lease=require_lease)
                if not _render_evidence_confirms_gameplay(
                    observation,
                    self.render_confirmation,
                    max_telemetry_age_seconds=self.config.max_telemetry_age_seconds,
                ):
                    self._set_state(AdapterState.DISARMED, "gameplay_readiness_disagreement")
                    self.reason = "gameplay_readiness_disagreement"
                    return self.preflight_report(["gameplay_readiness_disagreement"])
            if not observation.executable_valid:
                identity_verifier = getattr(self.telemetry, "verify_executable_identity", None)
                expected_executable_sha256 = str(
                    getattr(self.topology, "expected_executable_sha256", LIVE_EXECUTABLE_SHA256)
                ).upper()
                try:
                    identity = identity_verifier(expected_executable_sha256) if callable(identity_verifier) else {
                        "verified": False,
                        "reason": "executable_identity_verifier_unavailable",
                        "sha256": None,
                    }
                except Exception:
                    identity = {
                        "verified": False,
                        "reason": "executable_identity_unavailable",
                        "sha256": None,
                    }
                if not isinstance(identity, Mapping) or identity.get("verified") is not True:
                    self._neutralize("executable_identity_invalid")
                    self._set_state(AdapterState.DISARMED, "executable_identity_invalid")
                    return self.preflight_report(["executable_identity_invalid"])
                verified_hash = str(identity.get("sha256") or "").upper()
                if verified_hash != expected_executable_sha256:
                    self._neutralize("executable_identity_invalid")
                    self._set_state(AdapterState.DISARMED, "executable_identity_invalid")
                    return self.preflight_report(["executable_identity_invalid"])
                observation = replace(
                    observation,
                    executable_valid=True,
                    executable_sha256=verified_hash,
                )
                self._last_observation = observation
            # Render capture and lazy executable hashing can take long enough
            # to invalidate the sample used for those gates. Require a fresh,
            # advancing source sample before topology capture/planning. The
            # closed-loop path never authorizes a command from stale
            # telemetry: a transient during following crosses the neutral
            # readiness-recovery boundary below.
            self._preflight_stage = "final_readiness_observation"
            observation = self._require_observation(
                wait_for_fresh=True,
                after_sequence=observation.sequence,
            )
            if observation.telemetry_gameplay_ready is not True:
                settle = getattr(self.telemetry, "wait_for_settled_gameplay", None)
                if callable(settle):
                    try:
                        settled_result = settle(
                            self.config.settled_gameplay_timeout_seconds,
                            max_telemetry_age_seconds=self.config.max_telemetry_age_seconds,
                        )
                    except Exception as exc:
                        self.timeline.append({
                            "event": "post_render_settled_gameplay_reacquire_failed",
                            "t": self.monotonic(),
                            "sequence": observation.sequence,
                            "error": str(exc),
                        })
                    else:
                        self.timeline.append({
                            "event": "post_render_settled_gameplay_reacquired",
                            "t": self.monotonic(),
                            "sequence": observation.sequence,
                            "result": settled_result,
                        })
                        observation = self._require_observation(
                            wait_for_fresh=True,
                            after_sequence=observation.sequence,
                        )
                if observation.telemetry_gameplay_ready is not True:
                    self.timeline.append({
                        "event": "preflight_gameplay_readiness_rejected",
                        "t": self.monotonic(),
                        "observation": observation.to_dict(),
                    })
                    self._set_state(AdapterState.DISARMED, "telemetry_gameplay_not_ready")
                    self.reason = "telemetry_gameplay_not_ready"
                    return self.preflight_report([self.reason])
            failures.extend(self._check_preconditions(observation, require_lease=require_lease))
            if failures:
                self._set_state(AdapterState.DISARMED, "precondition_failed")
                self.reason = ";".join(failures)
                return self.preflight_report(failures)
            self._preflight_stage = "neutral_pose_settling"
            observation = self._settle_preflight_pose(neutral_observation, observation, require_lease=require_lease)
            self.timeline.append({
                "event": "preflight_dynamic_actor_snapshot",
                "t": self.monotonic(),
                "sequence": observation.sequence,
                "source_identity": (
                    observation.dynamic_obstacles.source_identity
                    if observation.dynamic_obstacles is not None else None
                ),
                "complete": (
                    observation.dynamic_obstacles.complete
                    if observation.dynamic_obstacles is not None else False
                ),
                "actors": [
                    {
                        "actor_id": item.actor_id,
                        "actor_class": item.actor_class,
                        "actor_category": item.actor_category,
                        "position_m": {"x": item.actor_position_m.x, "y": item.actor_position_m.y, "z": item.actor_position_m.z},
                        "distance_m": item.horizontal_distance_m,
                    }
                    for item in (observation.dynamic_obstacles.observations if observation.dynamic_obstacles is not None else ())
                    if item.horizontal_distance_m <= 2.0
                ],
                "movement_command_sent": False,
            })
            self._preflight_stage = "topology_resolution"
            topology = self._resolve_topology(observation)
            self.topology_session = topology
            self._attach_traversal_gimmicks(topology)
            self._bind_static_obstacle_scope(topology, observation)
            self._readmit_remembered_blockers()
            self._set_state(AdapterState.TOPOLOGY_READY, "current_session_topology_ready")
            start_resolution = topology.graph.resolve_point(
                observation.position,
                max_snap_distance_m=self.config.start_max_snap_m,
                max_vertical_separation_m=self.config.start_max_vertical_m,
            )
            self.start_resolution = start_resolution
            self.timeline.append({
                "event": "preflight_localization", "t": self.monotonic(),
                "sequence": observation.sequence,
                "position": _point_dict(observation.position),
                "session_id": observation.session_id, "world_id": observation.world_id,
                "map_id": observation.map_id, "resolution": start_resolution.to_dict(),
            })
            if not start_on_navmesh(start_resolution):
                raise AdapterError("live_start_not_inside_current_topology")
            if start_resolution.horizontal_distance_m is None or start_resolution.vertical_distance_m is None:
                raise AdapterError("live_start_resolution_incomplete")
            self._last_resolved_polygon_ref = int(start_resolution.selected_reference)
            self._preflight_stage = "route_planning"
            selection = self._choose_admission_destination(
                topology, observation, int(start_resolution.selected_reference)
            )
            if self.config.route_mode in CORRIDOR_ROUTE_MODES:
                if not isinstance(selection, SelectedRoute):
                    raise AdapterError("selected_route_plan_missing")
                self._selected_route = selection
                destination = selection.destination
                self.destination_resolution = selection.resolution
                plan = self._bind_selected_route_plan(
                    selection,
                    topology,
                    observation,
                    int(start_resolution.selected_reference),
                )
                if self.static_obstacles.planning_snapshot(now_t=self.monotonic()).obstacles:
                    overlay_plan = self._plan_with_static_overlay(
                        topology,
                        observation.position,
                        destination,
                        max_expansions=self.config.cached_topology_max_expansions,
                        max_waypoints=self.config.cached_topology_max_waypoints,
                        max_tile_crossings=(
                            0
                            if self.config.route_mode == "ordinary_multi_polygon"
                            else self.config.multi_tile_max_crossings
                            if self.config.route_mode == "ordinary_multi_tile"
                            else None
                        ),
                        max_snap_distance_m=self.config.start_max_snap_m,
                        max_vertical_separation_m=self.config.start_max_vertical_m,
                    )
                    if overlay_plan.status != "complete":
                        raise AdapterError(f"static_obstacle_route_{overlay_plan.reason_code}")
                    plan = overlay_plan.to_dict()
                    self._selected_route = replace(
                        selection,
                        plan=plan,
                        plan_sha256=_plan_hash(plan),
                    )
                    self._selected_route_plan_sha256 = _plan_hash(plan)
                    self._adapter_route_plan_sha256 = _plan_hash(plan)
            else:
                destination, destination_resolution = selection
                self.destination_resolution = destination_resolution
                planner_result = self._plan_with_static_overlay(
                    topology,
                    observation.position,
                    destination,
                    max_snap_distance_m=self.config.start_max_snap_m,
                    max_vertical_separation_m=self.config.start_max_vertical_m,
                )
                if isinstance(planner_result, DetourRoutePlan):
                    plan = planner_result.to_dict()
                elif isinstance(planner_result, Mapping):
                    plan = planner_result
                else:
                    raise AdapterError("planner_returned_invalid_route_plan")
            self.route_plan = plan
            self._adapter_route_plan_sha256 = _plan_hash(plan)
            self._adopt_route_traversals(plan)
            if (
                self._selected_route_plan_sha256 is not None
                and self._adapter_route_plan_sha256 != self._selected_route_plan_sha256
            ):
                raise AdapterError("selected_route_plan_identity_mismatch")
            self._report_route_gates(plan)
            self._set_state(AdapterState.PLAN_READY, "planner_route_ready")
            self._validate_first_route(plan, topology, observation)
            self._preflight_stage = "validator_topology_binding"
            self._bind_validator_to_topology(topology)
            self._preflight_stage = "independent_validation"
            validation = self.validator.validate(plan)
            self._independent_validator_route_plan_sha256 = _plan_hash(plan)
            if (
                self._selected_route_plan_sha256 is not None
                and (
                    self._adapter_route_plan_sha256 != self._selected_route_plan_sha256
                    or self._independent_validator_route_plan_sha256 != self._selected_route_plan_sha256
                )
            ):
                raise AdapterError("selected_route_plan_identity_mismatch")
            self.validator_result = validation
            self.timeline.append({
                "event": "route_plan_identity_verified",
                "t": self.monotonic(),
                "selected_route_plan_sha256": self._selected_route_plan_sha256,
                "adapter_route_plan_sha256": self._adapter_route_plan_sha256,
                "independent_validator_route_plan_sha256": self._independent_validator_route_plan_sha256,
                "plan_identity_equal": (
                    self._selected_route_plan_sha256 is None
                    or self._selected_route_plan_sha256 == self._adapter_route_plan_sha256 == self._independent_validator_route_plan_sha256
                ),
            })
            if validation.get("valid") is not True or validation.get("errors") != [] or validation.get("warnings") != []:
                self._set_state(AdapterState.DISARMED, "independent_validator_rejected_route")
                self.reason = "validator_rejected_route"
                return self.preflight_report([self.reason])
            if self.config.route_mode in CORRIDOR_ROUTE_MODES:
                for candidate in self._multi_polygon_candidates:
                    if candidate.get("selected"):
                        candidate["independent_validator"] = {
                            "valid": validation.get("valid"),
                            "errors": list(validation.get("errors") or []),
                            "warnings": list(validation.get("warnings") or []),
                        }
                        break
            self.follower = self.follower_factory(plan)
            self._set_state(AdapterState.PLAN_VALIDATED, "independent_validator_pass_and_follower_accept")
            self.authorization = self._validate_plan_identity(plan, topology, observation)
            self.authorization_proof = self._build_authorization_proof(
                plan,
                topology,
                observation,
                validation,
            )
            self._lookahead_reset_for_plan()
            self.timeline.append({
                "event": "immutable_route_authorization_proof_created",
                "t": self.monotonic(),
                "proof": self.authorization_proof.to_dict(),
            })
            return self.preflight_report([])
        except AdapterError as exc:
            disarm_capture = getattr(self.topology, "disarm_capture", None)
            if callable(disarm_capture):
                try:
                    disarm_capture()
                except AdapterError:
                    pass
            self._set_state(AdapterState.DISARMED, str(exc))
            self.preflight_exception = {
                "exception_type": type(exc).__name__,
                "message": _sanitize_exception_message(exc),
                "stage": self._preflight_stage,
                "artifact_path": (
                    self.topology_session.artifact_path
                    if self.topology_session is not None else None
                ),
            }
            return self.preflight_report([str(exc)])
        except Exception as exc:
            # Any unexpected construction/serialization failure is still a
            # refusal.  It must not escape as an ambiguous partially armed
            # state, and no live command is reachable before this method
            # returns PLAN_VALIDATED.
            try:
                self._neutralize("preflight_exception")
            except AdapterError:
                pass
            self.preflight_exception = {
                "exception_type": type(exc).__name__,
                "message": _sanitize_exception_message(exc),
                "stage": self._preflight_stage,
                "artifact_path": (
                    self.topology_session.artifact_path
                    if self.topology_session is not None else None
                ),
            }
            self.timeline.append({
                "event": "preflight_exception",
                "t": self.monotonic(),
                **self.preflight_exception,
            })
            self._set_state(AdapterState.FAILED, f"preflight_exception:{type(exc).__name__}")
            return self.preflight_report([self.reason])

    def _report_route_gates(self, plan: Mapping[str, Any]) -> None:
        """Say before any movement which automatic doors or gates the route goes through."""
        gates = plan_gates(plan)
        if gates:
            self.timeline.append({
                "event": "route_gates_reported", "t": self.monotonic(), "gate_count": len(gates),
                "gates": [{key: gate.get(key) for key in ("gate_id", "actor", "actor_class", "reason", "signals",
                                                          "along_m", "waypoint_index", "position", "evidence")}
                          for gate in gates],
                "planning": (plan.get("diagnostics") or {}).get("dynamic_gate_planning"),
                "movement_command_sent": False,
            })

    def preflight_report(self, failures: Sequence[str]) -> dict[str, Any]:
        observation = self._last_observation
        return {
            # Automatic doors and gates the planned route goes through, known
            # before movement (each is also a waypoint in the plan's diagnostics).
            "dynamic_gates": plan_gates(self.route_plan),
            "state": self.state.value,
            "reason": self.reason,
            "route_mode": self.config.route_mode,
            "route_target": _point_dict(self.route_target) if self.route_target is not None else None,
            "movement_authorized": self.movement_authorized,
            "failures": list(failures),
            "observation": observation.to_dict() if observation else None,
            "topology": self.topology_session.to_dict() if self.topology_session else None,
            "route_plan": copy.deepcopy(self.route_plan) if self.route_plan is not None else None,
            "validator": copy.deepcopy(self.validator_result) if self.validator_result is not None else None,
            "validator_timing": dict(getattr(self.validator, "timing", {}) or {}),
            "render_confirmation": self.render_confirmation.to_dict() if self.render_confirmation else None,
            "start_resolution": self.start_resolution.to_dict() if self.start_resolution is not None else None,
            "destination_resolution": self.destination_resolution.to_dict() if self.destination_resolution is not None else None,
            "authorization": self.authorization.to_dict() if self.authorization else None,
            "authorization_proof": self.authorization_proof.to_dict() if self.authorization_proof else None,
            "static_obstacles": self.static_obstacles.snapshot(now_t=self.monotonic()).to_dict(),
            "selected_route_plan_sha256": self._selected_route_plan_sha256,
            "adapter_route_plan_sha256": self._adapter_route_plan_sha256,
            "independent_validator_route_plan_sha256": self._independent_validator_route_plan_sha256,
            "exception_diagnostics": copy.deepcopy(self.preflight_exception),
            "multi_polygon_candidates": copy.deepcopy(self._multi_polygon_candidates),
            "multi_polygon_route_evidence": copy.deepcopy(self._multi_polygon_route_evidence),
            "validator_binding": dict(getattr(self.validator, "binding_evidence", {}) or {}),
            "traversal": self._traversal_summary(),
            "timeline": list(self.timeline),
        }

    def _same_authorization(self, observation: LiveObservation) -> bool:
        auth = self.authorization
        topology = self.topology_session
        plan = self.route_plan
        proof = self.authorization_proof
        if auth is None or topology is None or plan is None or proof is None:
            return False
        return (
            observation.session_id == auth.session_id
            and observation.world_id == auth.world_id
            and observation.map_id == auth.map_id
            and topology.fingerprint == auth.topology_fingerprint
            and topology.scope_identity == auth.scope_identity
            and _plan_hash(plan) == auth.route_plan_sha256
            and topology.session_id == observation.session_id
            and topology.world_id == observation.world_id
            and auth.route_mode == self.config.route_mode
            and proof.route_plan_sha256 == auth.route_plan_sha256 == _plan_hash(plan)
            and proof.topology_session_id == topology.session_id
            and proof.topology_fingerprint == topology.fingerprint == auth.topology_fingerprint
            and proof.scope_identity == topology.scope_identity == auth.scope_identity
            and proof.session_id == observation.session_id
            and proof.world_id == observation.world_id
            and proof.map_id == observation.map_id
            and proof.route_mode == self.config.route_mode
            and proof.validator_result_sha256 == _plan_hash(self.validator_result or {})
            and proof.static_obstacle_evidence_sha256 == self._static_obstacle_digest()
        )

    def _build_authorization_proof(
        self,
        plan: Mapping[str, Any],
        topology: TopologySession,
        observation: LiveObservation,
        validation: Mapping[str, Any],
    ) -> RouteAuthorizationProof:
        raw_waypoints = plan.get("waypoints")
        if not isinstance(raw_waypoints, list) or not raw_waypoints:
            raise AdapterError("route_waypoints_non_finite")
        try:
            waypoints = tuple(_parse_point(value) for value in raw_waypoints)
        except (KeyError, TypeError, ValueError) as exc:
            raise AdapterError("route_waypoints_non_finite") from exc
        if not all(_is_finite_point(point) for point in waypoints):
            raise AdapterError("route_waypoints_non_finite")
        cumulative = [0.0]
        for first, second in zip(waypoints, waypoints[1:]):
            cumulative.append(cumulative[-1] + _distance_m(first, second))
        corridor_raw = plan.get("polygon_corridor") or []
        corridor = tuple(
            int(value)
            for value in corridor_raw
            if isinstance(value, int) and not isinstance(value, bool)
        )
        return RouteAuthorizationProof(
            route_plan_sha256=_plan_hash(plan),
            topology_session_id=topology.session_id,
            topology_fingerprint=topology.fingerprint,
            session_id=observation.session_id,
            world_id=observation.world_id,
            map_id=observation.map_id,
            scope_identity=topology.scope_identity,
            route_mode=self.config.route_mode,
            start_ref=int(plan["start_ref"]),
            end_ref=int(plan["end_ref"]),
            corridor_refs=corridor,
            waypoints=waypoints,
            cumulative_waypoint_m=tuple(cumulative),
            validator_result_sha256=_plan_hash(validation),
            validated_portal_evidence_sha256=_plan_hash({"portals": plan.get("portals", [])}),
            static_safety_evidence_sha256=_plan_hash({
                "start_resolution": plan.get("start_resolution"),
                "end_resolution": plan.get("end_resolution"),
                "tile_crossings": validation.get("tile_crossings"),
                "route_length_m": plan.get("total_estimated_path_length_m"),
                "endpoint_edge_clearance_m": validation.get("endpoint_edge_clearance_m"),
                "waypoints_finite": validation.get("waypoints_finite"),
            }),
            static_obstacle_evidence_sha256=self._static_obstacle_digest(),
        )

    def _verify_authorization_proof(self, observation: LiveObservation) -> bool:
        """Verify only immutable proof identity plus current session identity."""
        proof = self.authorization_proof
        topology = self.topology_session
        plan = self.route_plan
        if proof is None or topology is None or plan is None:
            return False
        if proof.route_plan_sha256 != _plan_hash(plan):
            return False
        if proof.topology_session_id != topology.session_id:
            return False
        if proof.topology_fingerprint != topology.fingerprint:
            return False
        if proof.scope_identity != topology.scope_identity:
            return False
        if proof.session_id != observation.session_id or proof.world_id != observation.world_id or proof.map_id != observation.map_id:
            return False
        if proof.validator_result_sha256 != _plan_hash(self.validator_result or {}):
            return False
        if proof.static_obstacle_evidence_sha256 != self._static_obstacle_digest():
            return False
        return True

    def _timed_stage(
        self,
        timing: dict[str, float] | None,
        name: str,
        callback: Callable[[], Any],
    ) -> Any:
        if timing is None:
            return callback()
        started = self.wall_clock()
        try:
            return callback()
        finally:
            elapsed_ms = max(0.0, (self.wall_clock() - started) * 1000.0)
            timing[name] = float(timing.get(name, 0.0)) + elapsed_ms

    def _emit_post_command_timing(
        self,
        sequence: int,
        timing: Mapping[str, Any],
        *,
        outcome: str,
    ) -> None:
        self.timeline.append({
            "event": "post_command_stage_timing",
            "sequence": int(sequence),
            "outcome": outcome,
            "wall_clock_total_ms": sum(
                float(value) for value in timing.values()
                if isinstance(value, (int, float))
            ),
            "stages_ms": dict(timing),
        })

    def _effective_telemetry_age_seconds(
        self,
        observation: LiveObservation,
        *,
        now: float | None = None,
    ) -> float:
        """Return the observation age at the instant it may authorize input.

        ``telemetry_age_seconds`` is measured when the observation is built.
        The movement gate can perform local validation work afterwards, so the
        sample's age must include the monotonic time spent in this process.
        This preserves the configured 0.5-second bound instead of allowing a
        sample that was fresh at capture time to become stale before input.
        """
        try:
            observed_age = float(observation.telemetry_age_seconds)
            captured_t = float(observation.monotonic_t)
        except (TypeError, ValueError):
            return math.inf
        current_t = self.monotonic() if now is None else float(now)
        elapsed = max(0.0, current_t - captured_t)
        return observed_age + elapsed

    def _ensure_fresh_command_observation(self, observation: LiveObservation) -> LiveObservation:
        """Reacquire neutral, unchanged live state if pre-command work ran long.

        The expensive final route-start checks already established the exact
        authorized polygon.  If that observation is no longer fresh by the
        time the control cycle is about to begin, obtain a newer advancing
        sample while neutral.  Any uncommanded position change is fail-closed;
        it must not silently reuse the old route-start resolution.
        """
        effective_age = self._effective_telemetry_age_seconds(observation)
        if math.isfinite(effective_age) and effective_age <= self.config.max_telemetry_age_seconds:
            return observation

        self._neutralize("final_command_telemetry_freshness_expired")
        self.timeline.append({
            "event": "final_command_telemetry_freshness_expired",
            "t": self.monotonic(),
            "sequence": observation.sequence,
            "telemetry_age_seconds_at_observation": observation.telemetry_age_seconds,
            "observation_monotonic_t": observation.monotonic_t,
            "effective_telemetry_age_seconds": effective_age,
            "max_telemetry_age_seconds": self.config.max_telemetry_age_seconds,
            "actuator_neutral": self.actuator.is_neutral(),
        })
        fresh = self._require_observation(
            wait_for_fresh=True,
            after_sequence=observation.sequence,
        )
        if fresh.position != observation.position:
            raise AdapterError("movement_start_changed_during_final_freshness_reacquisition")
        failures = self._check_preconditions(fresh)
        if failures or not self._same_authorization(fresh):
            raise AdapterError(
                "final_command_freshness_reacquisition_failed:"
                + ";".join(failures or ["topology_or_route_authorization_invalid"])
            )
        fresh_age = self._effective_telemetry_age_seconds(fresh)
        if not math.isfinite(fresh_age) or fresh_age > self.config.max_telemetry_age_seconds:
            raise AdapterError("telemetry_stale_after_final_freshness_reacquisition")
        self.timeline.append({
            "event": "final_command_freshness_reacquired",
            "t": self.monotonic(),
            "sequence": fresh.sequence,
            "previous_sequence": observation.sequence,
            "telemetry_age_seconds": fresh.telemetry_age_seconds,
            "effective_telemetry_age_seconds": fresh_age,
            "position": _point_dict(fresh.position),
            "position_unchanged": True,
            "actuator_neutral": self.actuator.is_neutral(),
        })
        return fresh

    def _ensure_fresh_following_observation(self, observation: LiveObservation) -> LiveObservation:
        """Reacquire a post-pulse sample if neutral processing made it stale.

        A post-command sample is allowed to contain movement, so this gate must
        not apply the pre-command unchanged-position rule.  It still requires
        a newer effectively-fresh sequence, a neutral actuator, unchanged
        session/world/topology authorization, and valid route-local corridor
        evidence before another command can be considered.  The route-local
        checks can themselves be expensive on the full cached graph, so the
        effective-age check is repeated after those checks.  If that work
        consumes the freshness budget, reacquire again within the same bounded
        wait rather than authorizing from a sample that was fresh only when it
        was first read.
        """
        deadline = self.monotonic() + self.config.preflight_telemetry_wait_seconds
        current = observation
        initial_sequence = observation.sequence
        reacquisition_count = 0
        boundary_ambiguity_retry_count = 0
        timing: dict[str, float] = self._active_post_command_timing or {}

        def finish(outcome: str) -> None:
            self._active_post_command_timing = timing
            if outcome != "fresh_sample_authorized_for_evaluation":
                self._emit_post_command_timing(current.sequence, timing, outcome=outcome)

        while True:
            effective_age = self._timed_stage(
                timing,
                "effective_age_computation_ms",
                lambda: self._effective_telemetry_age_seconds(current),
            )
            if not math.isfinite(effective_age) or effective_age > self.config.max_telemetry_age_seconds:
                reason = (
                    "post_command_telemetry_freshness_expired"
                    if reacquisition_count == 0
                    else "post_command_freshness_validation_expired"
                )
                if not self._neutralize(reason):
                    finish("actuator_neutrality_unconfirmed")
                    raise AdapterError("actuator_neutrality_unconfirmed")
                self.timeline.append({
                    "event": reason,
                    "t": self.monotonic(),
                    "sequence": current.sequence,
                    "telemetry_age_seconds": current.telemetry_age_seconds,
                    "effective_telemetry_age_seconds": effective_age,
                    "max_telemetry_age_seconds": self.config.max_telemetry_age_seconds,
                    "actuator_neutral": self.actuator.is_neutral(),
                    "reacquisition_count": reacquisition_count,
                    "position": _point_dict(current.position),
                })
                remaining = deadline - self.monotonic()
                if remaining <= 0.0:
                    finish("freshness_timeout")
                    raise AdapterError("telemetry_stale_after_post_command_freshness_reacquisition")
                try:
                    current = self._require_observation(
                        wait_for_fresh=True,
                        after_sequence=current.sequence,
                        timeout_seconds=remaining,
                        require_effective_fresh=True,
                        timing=timing,
                    )
                except AdapterError:
                    finish("freshness_reacquisition_failed")
                    raise
                reacquisition_count += 1
                continue

            failures = self._timed_stage(
                timing,
                "precondition_verification_ms",
                lambda: self._check_preconditions(current),
            )
            self._timed_stage(
                timing,
                "readiness_verification_ms",
                lambda: current.telemetry_gameplay_ready is True and not current.loading and current.controllable,
            )
            self._timed_stage(
                timing,
                "session_lease_identity_ms",
                lambda: bool(current.process_valid and current.executable_valid and current.lease_active and current.actuator_neutral),
            )
            if current.telemetry_gameplay_ready is not True:
                failures.append("telemetry_gameplay_not_ready")
            authorization_ok = self._timed_stage(
                timing,
                "authorization_identity_ms",
                lambda: self._same_authorization(current),
            )
            proof_ok = self._timed_stage(
                timing,
                "static_plan_validation_ms",
                lambda: self._verify_authorization_proof(current),
            )
            independent_ok = self._timed_stage(
                timing,
                "independent_validator_related_checks_ms",
                lambda: (
                    isinstance(self.validator_result, Mapping)
                    and self.validator_result.get("valid") is True
                    and self.validator_result.get("errors") == []
                    and self.validator_result.get("warnings") == []
                ),
            )
            if failures or not authorization_ok or not proof_ok or not independent_ok:
                if (
                    failures
                    and authorization_ok
                    and proof_ok
                    and independent_ok
                    and self._is_readiness_only_failure(failures)
                ):
                    finish("readiness_recovery_required")
                    raise _PostCommandReadinessError(failures)
                finish("authorization_failed")
                raise AdapterError(
                    "post_command_freshness_reacquisition_failed:"
                    + ";".join(failures or ["topology_or_route_authorization_invalid"])
                )
            if self.config.route_mode in CORRIDOR_ROUTE_MODES:
                route_sample = self._route_polygon_sample(
                    current.position,
                    sequence=current.sequence,
                    timing=timing,
                )
                assessment = self._timed_stage(
                    timing,
                    "route_local_transition_classification_ms",
                    lambda: self._route_local_sample_assessment(self._last_corridor_sample, route_sample),
                )
                if not assessment.get("safe"):
                    reason = str(assessment.get("reason_code") or "corridor_membership_not_authorized")
                    # A shared-boundary ambiguity can be a one-sample localization
                    # condition while Cloud crosses an already-authorized portal.
                    # Permit exactly one later fresh sample to resolve it while
                    # neutral. All other corridor failures, and a second ambiguous
                    # sample, retain the existing immediate fail-closed replan.
                    if (
                        reason == "authorized_boundary_ambiguity_unproven"
                        and boundary_ambiguity_retry_count == 0
                    ):
                        if not self._neutralize("post_command_boundary_ambiguity_reacquisition"):
                            finish("actuator_neutrality_unconfirmed")
                            raise AdapterError("actuator_neutrality_unconfirmed")
                        retry_timeout = min(
                            max(0.0, deadline - self.monotonic()),
                            max(0.0, self.config.max_telemetry_age_seconds),
                        )
                        self.timeline.append({
                            "event": "post_command_boundary_ambiguity_retry",
                            "t": self.monotonic(),
                            "sequence": current.sequence,
                            "reason_code": reason,
                            "timeout_seconds": retry_timeout,
                            "movement_command_sent": False,
                        })
                        if retry_timeout > 0.0:
                            try:
                                current = self._require_observation(
                                    wait_for_fresh=True,
                                    after_sequence=current.sequence,
                                    timeout_seconds=retry_timeout,
                                    require_effective_fresh=True,
                                    timing=timing,
                                )
                            except AdapterError as exc:
                                self.timeline.append({
                                    "event": "post_command_boundary_ambiguity_retry_failed",
                                    "t": self.monotonic(),
                                    "sequence": current.sequence,
                                    "reason_code": reason,
                                    "error": _sanitize_exception_message(exc),
                                    "movement_command_sent": False,
                                })
                            else:
                                boundary_ambiguity_retry_count += 1
                                reacquisition_count += 1
                                continue
                    self.timeline.append({
                        "event": "post_command_corridor_reacquisition_rejected",
                        "t": current.monotonic_t,
                        "sequence": current.sequence,
                        "reason_code": reason,
                        "route_local_assessment": dict(assessment),
                        "position": _point_dict(current.position),
                    })
                    finish("corridor_reacquisition_failed")
                    raise AdapterError("post_command_corridor_reacquisition_failed:" + reason)
                self._active_post_command_route_sample = route_sample
            else:
                self._active_post_command_route_sample = None

            validated_age = self._timed_stage(
                timing,
                "final_freshness_gate_ms",
                lambda: self._effective_telemetry_age_seconds(current),
            )
            if not math.isfinite(validated_age) or validated_age > self.config.max_telemetry_age_seconds:
                # The graph/corridor validation above can consume most of the
                # movement freshness window.  Loop back through the same
                # bounded neutral reacquisition rather than treating the
                # validation-start age as authorization evidence.
                continue

            if current.sequence != initial_sequence or reacquisition_count:
                self.timeline.append({
                    "event": "post_command_freshness_reacquired",
                    "t": current.monotonic_t,
                    "sequence": current.sequence,
                    "previous_sequence": observation.sequence,
                    "telemetry_age_seconds": current.telemetry_age_seconds,
                    "effective_telemetry_age_seconds": validated_age,
                    "position": _point_dict(current.position),
                    "actuator_neutral": self.actuator.is_neutral(),
                    "reacquisition_count": reacquisition_count,
                })
            finish("fresh_sample_authorized_for_evaluation")
            return current

    def _final_pre_movement_gate(self) -> LiveObservation:
        """Reacquire fresh live state after all expensive planning work.

        Topology capture, graph loading and independent validation can take
        minutes.  The observation that authorized those operations is not
        allowed to authorize the first controller command as well.  This gate
        is intentionally the final operation before the control loop and
        verifies the live start still corresponds to the authorized route.
        """
        previous = self._last_observation
        if previous is None:
            raise AdapterError("final_pre_movement_observation_missing")
        previous_sequence = previous.sequence
        if not self._neutralize("final_pre_movement_neutral_proof"):
            raise AdapterError("actuator_neutrality_unconfirmed")
        observation = self._require_observation(
            wait_for_fresh=True,
            after_sequence=previous_sequence,
        )
        if observation.telemetry_gameplay_ready is not True:
            settle = getattr(self.telemetry, "wait_for_settled_gameplay", None)
            if callable(settle):
                try:
                    settled_result = settle(
                        self.config.settled_gameplay_timeout_seconds,
                        max_telemetry_age_seconds=self.config.max_telemetry_age_seconds,
                    )
                except Exception as exc:
                    self.timeline.append({
                        "event": "final_pre_movement_settled_gameplay_wait_failed",
                        "t": self.monotonic(),
                        "sequence": observation.sequence,
                        "error": str(exc),
                    })
                else:
                    self.timeline.append({
                        "event": "final_pre_movement_settled_gameplay_wait_completed",
                        "t": self.monotonic(),
                        "sequence": observation.sequence,
                        "result": settled_result,
                    })
                    observation = self._require_observation(
                        wait_for_fresh=True,
                        after_sequence=observation.sequence,
                    )
        failures: list[str] = []
        if observation.telemetry_gameplay_ready is not True:
            failures.append("telemetry_gameplay_not_ready")
        failures.extend(self._check_preconditions(observation))
        authorization_invalid = not self._same_authorization(observation)
        if authorization_invalid:
            failures.append("topology_or_route_authorization_invalid")

        auth = self.authorization
        topology = self.topology_session
        route_start_horizontal_m: float | None = None
        route_start_vertical_m: float | None = None
        resolved_start_ref: int | None = None
        if auth is None or topology is None:
            failures.append("topology_or_route_authorization_missing")
        else:
            route_start_horizontal_m = _horizontal_distance_m(
                observation.position,
                auth.resolved_start,
            )
            route_start_vertical_m = abs(observation.position.z - auth.resolved_start.z) / 100.0
            if route_start_horizontal_m > self.config.start_max_snap_m:
                failures.append("route_start_horizontal_mismatch")
            if route_start_vertical_m > self.config.start_max_vertical_m:
                failures.append("route_start_vertical_mismatch")
            try:
                resolution = topology.graph.resolve_point(
                    observation.position,
                    max_snap_distance_m=self.config.start_max_snap_m,
                    max_vertical_separation_m=self.config.start_max_vertical_m,
                )
                if resolution.selected_reference is not None:
                    resolved_start_ref = int(resolution.selected_reference)
                if not resolution.ok or resolution.selected_reference != auth.start_ref:
                    failures.append("route_start_polygon_mismatch")
            except Exception:
                failures.append("route_start_polygon_unresolvable")

        gate_event = {
            "event": "final_pre_movement_gate_failed" if failures else "final_pre_movement_gate_passed",
            "t": self.monotonic(),
            "sequence": observation.sequence,
            "telemetry_age_seconds": observation.telemetry_age_seconds,
            "effective_telemetry_age_seconds_at_gate": self._effective_telemetry_age_seconds(observation),
            "telemetry_gameplay_ready": observation.telemetry_gameplay_ready,
            "position": _point_dict(observation.position),
            "camera_yaw": observation.camera_yaw,
            "lease_active": observation.lease_active,
            "actuator_neutral": observation.actuator_neutral,
            "route_start_horizontal_m": route_start_horizontal_m,
            "route_start_vertical_m": route_start_vertical_m,
            "authorized_start_ref": auth.start_ref if auth is not None else None,
            "resolved_start_ref": resolved_start_ref,
            "failures": list(failures),
        }
        if failures:
            self.timeline.append(gate_event)
            if authorization_invalid and failures == ["topology_or_route_authorization_invalid"]:
                raise AdapterError("plan_identity_changed")
            raise AdapterError("final_pre_movement_gate_failed:" + ";".join(failures))
        self._last_corridor_sample = self._route_polygon_sample(
            observation.position,
            sequence=observation.sequence,
        )
        if self._last_corridor_sample is not None:
            gate_event["corridor_resolution"] = self._last_corridor_sample["corridor_resolution"].to_dict()
            corridor = tuple(
                int(value)
                for value in (self.route_plan or {}).get("polygon_corridor", [])
                if isinstance(value, int) and not isinstance(value, bool)
            )
            start_ref = int(auth.start_ref) if auth is not None else None
            if start_ref is not None and start_ref in corridor:
                self._route_local_corridor_index = corridor.index(start_ref)
                self._route_local_corridor_ref = start_ref
                self._route_local_last_position = observation.position
                self._route_local_last_sequence = observation.sequence
                gate_event["route_local_corridor_state"] = {
                    "accepted_index": self._route_local_corridor_index,
                    "accepted_ref": self._route_local_corridor_ref,
                    "sequence": observation.sequence,
                    "source": "authorized_start_resolution",
                }
        self.timeline.append(gate_event)
        return observation

    # -- off-mesh traversal links -------------------------------------------

    def _locate_route_traversals(self, plan: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        """The plan's off-mesh crossings, each with its two waypoints.

        Raises before any movement when a declared crossing cannot be found as
        two consecutive waypoints (an obstacle detour moved one), does not
        match a traversal portal of the corridor, or the adapter may not
        cross links at all.
        """
        if not isinstance(plan, Mapping):
            return []
        segments = plan.get("traversal_segments") or []
        portals = plan.get("portals")
        corridor = plan.get("polygon_corridor")
        positions = [
            index for index, portal in enumerate(portals[1:-1] if isinstance(portals, list) else [])
            if isinstance(portal, Mapping) and portal.get("kind") == PORTAL_KIND_TRAVERSAL
        ]
        if not segments and not positions:
            return []
        if not self.config.traversal_links:
            raise AdapterError("traversal_links_disabled")
        if not isinstance(segments, list) or len(segments) != len(positions) or not isinstance(corridor, list):
            raise AdapterError("traversal_segments_portal_mismatch")
        try:
            waypoints = [_parse_point(value) for value in plan.get("waypoints") or []]
            located = locate_traversal_waypoints(waypoints, segments)
        except ValueError as exc:
            raise AdapterError(str(exc)) from exc
        entries: list[dict[str, Any]] = []
        for (segment, index), position in zip(located, positions):
            try:
                from_ref, to_ref = int(segment["from_ref"]), int(segment["to_ref"])
                start, end = segment_points(segment)
            except (KeyError, TypeError, ValueError) as exc:
                raise AdapterError("traversal_segment_invalid") from exc
            if (from_ref, to_ref) != (corridor[position], corridor[position + 1]):
                raise AdapterError("traversal_segment_corridor_mismatch")
            landing_refs = [to_ref]
            # A ladder ends on, and a sprinted step or drop carries on to, the
            # polygons just past its landing: they count as landed too.
            if segment.get("link_class") in GIMMICK_LINK_CLASSES or segment.get("link_class") in LEDGE_LINK_CLASSES:
                following = [later for later in positions if later > position]
                stop = min([position + 2 + GIMMICK_LANDING_CORRIDOR_AHEAD]
                           + [later + 1 for later in following[:1]])
                landing_refs += [int(ref) for ref in corridor[position + 2:stop]]
            entries.append({
                "order": len(entries),
                "segment": dict(segment),
                "start_index": index,
                "end_index": index + 1,
                "corridor_index": position,
                "from_ref": from_ref,
                "to_ref": to_ref,
                "start": start,
                "end": end,
                "landing_refs": tuple(landing_refs),
                "state": "pending",
            })
        return entries

    def _adopt_route_traversals(self, plan: Mapping[str, Any]) -> None:
        """Take the plan's crossings and say, before any movement, which link
        classes it depends on that no live crossing has verified."""
        self._route_traversals = self._locate_route_traversals(plan)
        summary = self._traversal_summary()
        if summary is not None:
            self.timeline.append({"event": "route_uses_traversal_links", "t": self.monotonic(), **summary})

    def _traversal_summary(self) -> dict[str, Any] | None:
        if not self._route_traversals:
            return None
        segments = [item["segment"] for item in self._route_traversals]
        unverified = unverified_classes(segments)
        return {
            "count": len(segments),
            "links": [
                {
                    "order": item["order"],
                    "link_class": item["segment"].get("link_class"),
                    "link_ref": item["segment"].get("link_ref"),
                    "direction": item["segment"].get("direction"),
                    "start": _point_dict(item["start"]),
                    "end": _point_dict(item["end"]),
                    "horizontal_length_m": item["segment"].get("horizontal_length_m"),
                    "vertical_delta_m": item["segment"].get("vertical_delta_m"),
                    "live_verified": item["segment"].get("live_verified"),
                    "state": item["state"],
                }
                for item in self._route_traversals
            ],
            "unverified_classes": unverified,
            "route_depends_on_unverified_traversal": bool(unverified),
        }

    def _verify_traversal_portal(
        self,
        topology: TopologySession,
        raw_portal: Mapping[str, Any],
        expected_from: int,
        expected_to: int,
    ) -> None:
        """A traversal portal must be a crossing the graph's policy allows."""
        if not self.config.traversal_links:
            raise AdapterError("cached_topology_traversal_links_disabled")
        if topology.scope_identity is not None:
            raise AdapterError("cached_topology_traversal_outside_partial_scope")
        graph = topology.graph
        try:
            link_ref = int(raw_portal["link_ref"])
            from_ref, to_ref = int(raw_portal["from_ref"]), int(raw_portal["to_ref"])
            reverse = raw_portal.get("direction") == "end_to_start"
            left, right = _parse_point(raw_portal["left"]), _parse_point(raw_portal["right"])
            link_class = str(raw_portal.get("traversal_class"))
        except (KeyError, TypeError, ValueError) as exc:
            raise AdapterError("cached_topology_traversal_portal_invalid") from exc
        link = graph.off_mesh_link(link_ref)
        if link is None:
            raise AdapterError("cached_topology_traversal_link_unproven")
        crossing_from, crossing_to, departure, landing = link.crossing(reverse)
        if (
            (from_ref, to_ref) != (expected_from, expected_to)
            or (crossing_from, crossing_to) != (from_ref, to_ref)
            or _distance_m(departure, left) > 1e-3
            or _distance_m(landing, right) > 1e-3
            or link.link_class != link_class
        ):
            raise AdapterError("cached_topology_traversal_link_unproven")
        if not graph.traversal_policy.permits(link.link_class, reverse):
            raise AdapterError("cached_topology_traversal_class_not_permitted")

    def _traversal_link_near(self, position: Vec3) -> bool:
        return any(
            item["state"] == "pending"
            and _horizontal_distance_m(position, item["start"]) <= TRAVERSAL_LOOKAHEAD_EXEMPT_M
            for item in self._route_traversals
        )

    @staticmethod
    def _at_traversal_start(position: Vec3, item: Mapping[str, Any]) -> bool:
        return (
            _horizontal_distance_m(position, item["start"]) <= TRAVERSAL_START_RADIUS_M
            and abs(position.z - item["start"].z) / 100.0 <= TRAVERSAL_START_VERTICAL_M
        )

    def _pending_traversal_for(
        self, event: Mapping[str, Any], observation: LiveObservation,
    ) -> dict[str, Any] | None:
        """The crossing the follower has just reached the start of, if any."""
        item = next((entry for entry in self._route_traversals if entry["state"] == "pending"), None)
        index = event.get("waypoint_index")
        if (
            item is None
            or not isinstance(index, int)
            or index not in {item["start_index"], item["end_index"]}
            or str(event.get("state")) not in {"FOLLOWING", "WAYPOINT_REACHED"}
            or _horizontal_distance_m(observation.position, item["start"]) > TRAVERSAL_FOLLOWER_HANDOFF_M
        ):
            return None
        return item

    def _traversal_push(self, heading_degrees: float, observation: LiveObservation, *, magnitude: float = 1.0,
                        sprint: bool = False) -> None:
        """Hold the stick toward a world heading, at walking pace unless
        ``sprint`` (a sprint takes ledges and low walls by itself)."""
        if observation.camera_yaw is None:
            raise AdapterError("steering_heading_or_camera_yaw_missing")
        x, y = world_heading_to_camera_stick(heading_degrees, float(observation.camera_yaw), magnitude)
        supervised = getattr(self.actuator, "supervised_command", None)
        # Keep movement held across the animation even when the route's
        # ordinary legs use pulses. Releasing each pulse can abort a vault.
        if callable(supervised):
            result = supervised(
                x, y, deadline=self.monotonic() + TRAVERSAL_HOLD_SECONDS,
                stream_id=self._stream_id, sequence=int(observation.sequence), sprint=sprint,
            )
            self._stream_hold_active = True
        else:
            raise AdapterError("supervised_actuator_unavailable")
        self._attempt_movement_cycles += 1
        self.timeline.append({
            "event": "traversal_command", "t": self.monotonic(), "sequence": observation.sequence,
            "heading_degrees": heading_degrees, "camera_yaw": observation.camera_yaw,
            "actor_yaw": observation.actor_yaw, "position": _point_dict(observation.position),
            "magnitude": magnitude, "stick_x": x, "stick_y": y, "sprint": sprint,
            "backend_result": result,
        })

    def _traversal_polygon_proven(self, observation: LiveObservation, reference: int, *, departure: bool = False) -> bool:
        sample = self._route_polygon_sample(observation.position, sequence=observation.sequence)
        if sample is None:
            return False
        resolution = sample["corridor_resolution"]
        # The departure can be on a navmesh seam. Permit at most 5 cm of
        # source-side snap there, still globally resolved to that exact source
        # polygon. A landing always requires containment.
        proven = {int(candidate.reference) for candidate in resolution.candidates
                  if candidate.contained or (departure and candidate.horizontal_distance_m <= 0.05
                                             and candidate.vertical_distance_m <= 0.5)}
        global_ref = resolution.global_resolution.selected_reference
        return int(reference) in proven and (
            global_ref == int(reference)
            or (global_ref is None and resolution.reason_code == "authorized_shared_boundary_or_overlap")
        )

    def _gimmick_departure_proven(self, observation: LiveObservation, item: Mapping[str, Any]) -> bool:
        graph = self.topology_session.graph if self.topology_session is not None else None
        if graph is None:
            return False
        resolution = graph.resolve_point_if_resolvable(
            observation.position, max_snap_distance_m=GIMMICK_DEPARTURE_SNAP_M,
            max_vertical_separation_m=GIMMICK_DEPARTURE_VERTICAL_M)
        labels = graph.ground_islands()
        return (resolution.ok and resolution.selected_reference is not None
                and labels.get(int(resolution.selected_reference)) == labels.get(int(item["from_ref"])))

    def _traversal_landing_index(self, observation: LiveObservation, item: Mapping[str, Any]) -> int | None:
        """Which of the crossing's landing polygons holds the sample, as an
        offset past the link's own landing polygon; None if none does."""
        for offset, reference in enumerate(item.get("landing_refs") or (item["to_ref"],)):
            if self._traversal_polygon_proven(observation, int(reference)):
                return offset
        return None

    def _cross_traversal(
        self, observation: LiveObservation, item: dict[str, Any], *, source: str,
    ) -> tuple[bool, LiveObservation]:
        """Walk one off-mesh link; stop with a specific outcome if it is not crossed.

        Returns whether the character landed, and the last observation.  On
        failure the stick is neutral, the adapter is STOPPED with the outcome
        as its reason (``traversal_link_not_crossed`` when the time allowed
        ran out or nothing moved closer) and the timeline carries the evidence.
        """
        segment = item["segment"]
        target = CrossingTarget(
            link_class=str(segment.get("link_class")),
            link_ref=int(segment["link_ref"]),
            from_ref=item["from_ref"],
            to_ref=item["to_ref"],
            start=item["start"],
            end=item["end"],
            direction=str(segment.get("direction") or "start_to_end"),
        )
        self.timeline.append({"event": "traversal_crossing_requested", "t": self.monotonic(),
                              "source": source, "order": item["order"]})
        self._set_state(AdapterState.FOLLOWING, "traversal_crossing")
        crossing = TraversalCrossing(
            monotonic=self.monotonic,
            next_observation=lambda after: self._require_observation(
                wait_for_fresh=True, after_sequence=after, timeout_seconds=CROSSING_TELEMETRY_STALL_SECONDS),
            readiness=lambda sample: self._check_preconditions(sample, allow_owned_hold=True),
            push=lambda heading, sample: self._traversal_push(
                heading, sample, sprint=target.link_class in SPRINT_CROSSING_CLASSES),
            align_push=lambda heading, sample: self._traversal_push(heading, sample, magnitude=0.5),
            neutralize=lambda: self._neutralize("traversal_departure_settling"),
            departure_ready=(
                (lambda sample: self._gimmick_departure_proven(sample, item))
                if segment.get("link_class") in GIMMICK_LINK_CLASSES
                else (lambda sample: self._traversal_polygon_proven(sample, item["from_ref"], departure=True))),
            landing_ready=lambda sample: self._traversal_landing_index(sample, item) is not None,
            record=self.timeline.append,
        )
        outcome = crossing.cross(target, observation)
        last = outcome.observation
        self._pending_command_evidence = None
        self._last_command_sequence = last.sequence
        if outcome.crossed:
            item["state"] = "crossed"
            if source != "pursuit":
                if self._neutralize("traversal_crossed"):
                    last = replace(last, actuator_neutral=True)
            return True, last
        item["state"] = "failed"
        record_failed_traversal_link(target.link_ref)
        self._neutralize(outcome.reason)
        self._stream_hold_active = False
        self.timeline.append({"event": outcome.reason, "t": self.monotonic(), "source": source,
                              "order": item["order"], "evidence": outcome.evidence})
        self._set_state(AdapterState.STOPPED, outcome.reason)
        return False, last

    def _commit_traversal_landing(self, observation: LiveObservation, item: Mapping[str, Any]) -> bool:
        """Move the route-local corridor state across a crossed link.

        The crossing leaves the authorized polygons for a moment, so the
        corridor state machine cannot follow it sample by sample; on landing
        the sample must belong to the link's landing polygon, and the state
        resumes there.  Otherwise the route stops.
        """
        sample = self._route_polygon_sample(observation.position, sequence=observation.sequence)
        resolution = sample["corridor_resolution"] if sample is not None else None
        landing_offset = self._traversal_landing_index(observation, item)
        landing_refs = item.get("landing_refs") or (item["to_ref"],)
        landing_ref = int(landing_refs[landing_offset if landing_offset is not None else 0])
        if (self._check_preconditions(observation, allow_owned_hold=True) or observation.grounded is not True
                or landing_offset is None):
            self._neutralize("traversal_landing_polygon_unproven")
            self.timeline.append({
                "event": "traversal_landing_polygon_unproven", "t": self.monotonic(),
                "sequence": observation.sequence, "landing_ref": landing_ref,
                "corridor_resolution": resolution.to_dict() if resolution is not None else None,
                "order": item["order"],
            })
            self._set_state(AdapterState.STOPPED, "traversal_landing_polygon_unproven")
            return False
        commit = getattr(self.follower, "commit_traversal_landing", None)
        try:
            if not callable(commit):
                raise AdapterError("traversal_follower_resume_unavailable")
            resumed = commit(item["start_index"], item["end_index"], {
                "t": observation.monotonic_t, "position": _point_dict(observation.position),
                "heading": observation.actor_yaw,
            })
        except (ValueError, AdapterError) as exc:
            self._neutralize("traversal_follower_resume_unproven")
            self.timeline.append({"event": "traversal_follower_resume_unproven", "t": self.monotonic(),
                                  "sequence": observation.sequence, "error": str(exc), "order": item["order"]})
            self._set_state(AdapterState.STOPPED, "traversal_follower_resume_unproven")
            return False
        global_ref = resolution.global_resolution.selected_reference
        self._route_local_corridor_index = int(item["corridor_index"]) + 1 + landing_offset
        self._route_local_corridor_ref = landing_ref
        self._route_local_last_position = observation.position
        self._route_local_last_sequence = int(observation.sequence)
        # The follower just reset progress at this exact sample. Do not let
        # the generic neutral-gap pause shift that timestamp into the future.
        self.__dict__["_follower_last_sample"] = (self.follower, observation.monotonic_t)
        self._last_corridor_sample = sample
        if global_ref is not None:
            self._last_resolved_polygon_ref = int(global_ref)
        metrics = sample.get("metrics") or {}
        if metrics.get("along_route_m") is not None:
            self._last_route_along_m = float(metrics["along_route_m"])
        self.timeline.append({
            "event": "traversal_landing_committed", "t": self.monotonic(), "sequence": observation.sequence,
            "order": item["order"], "accepted_index": self._route_local_corridor_index,
            "accepted_ref": landing_ref,
            "follower_resume": resumed,
        })
        return True

    def _follower_event(self, observation: LiveObservation) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self.follower is None:
            raise AdapterError("route_follower_missing")
        # A long gap between samples with the stick neutral is the adapter
        # busy (re-planning, auditing), not Cloud stuck: live at Kalm a 9 s
        # re-plan read as a stall, and the assumed blocker walled off the
        # route. The gap is excluded from stall time like a safety pause.
        previous = self.__dict__.get("_follower_last_sample")
        if (previous is not None and previous[0] is self.follower
                and observation.monotonic_t - previous[1] > NEUTRAL_GAP_STALL_PAUSE_SECONDS
                and self.actuator.is_neutral() is True):
            pause = getattr(self.follower, "pause_for_safety", None)
            resume = getattr(self.follower, "resume_after_safety_pause", None)
            if callable(pause) and callable(resume):
                pause(previous[1])
                resume(observation.monotonic_t)
                self.timeline.append({"event": "follower_neutral_gap_excluded", "t": observation.monotonic_t,
                                      "gap_seconds": observation.monotonic_t - previous[1],
                                      "sequence": observation.sequence})
        self.__dict__["_follower_last_sample"] = (self.follower, observation.monotonic_t)
        sample = {
            "t": observation.monotonic_t,
            "position": _point_dict(observation.position),
            "heading": observation.actor_yaw,
        }
        events = self.follower.on_telemetry(sample)
        latest = events[-1] if events else {"state": str(getattr(self.follower.state, "value", self.follower.state))}
        return events, latest

    def _route_polygon_sample(
        self,
        position: Vec3,
        *,
        sequence: int | None = None,
        timing: dict[str, float] | None = None,
    ) -> dict[str, Any] | None:
        """Capture global and corridor-bounded resolution for one route point.

        The graph resolutions are immutable for a given position/sequence and
        route plan.  Cache only those static geometric results; route metrics
        are recomputed against the current follower waypoint state.
        """
        topology = self.topology_session
        plan = self.route_plan
        if topology is None or not isinstance(plan, Mapping):
            return None
        corridor = plan.get("polygon_corridor")
        if self.config.route_mode not in CORRIDOR_ROUTE_MODES or not isinstance(corridor, list):
            return None
        corridor_refs = tuple(int(value) for value in corridor if isinstance(value, int) and not isinstance(value, bool))
        if not corridor_refs:
            return None
        proof = self.authorization_proof
        plan_sha = proof.route_plan_sha256 if proof is not None else _plan_hash(plan)
        cache_key = (
            int(sequence) if sequence is not None else None,
            float(position.x),
            float(position.y),
            float(position.z),
            plan_sha,
        )
        cached = self._route_sample_cache.get(cache_key) if sequence is not None else None
        if cached is None:
            global_resolution = self._timed_stage(
                timing,
                "current_position_topology_resolution_ms",
                lambda: topology.graph.resolve_point(
                    position,
                    max_snap_distance_m=self.config.start_max_snap_m,
                    max_vertical_separation_m=self.config.start_max_vertical_m,
                ),
            )
            corridor_resolution = self._timed_stage(
                timing,
                "corridor_resolution_ms",
                lambda: topology.graph.resolve_point_in_corridor(
                    position,
                    corridor_refs,
                    max_snap_distance_m=self.config.start_max_snap_m,
                    max_vertical_separation_m=self.config.start_max_vertical_m,
                    global_resolution=global_resolution,
                ),
            )
            cached = {
                "global_resolution": global_resolution,
                "corridor_resolution": corridor_resolution,
            }
            if sequence is not None:
                self._route_sample_cache[cache_key] = cached
        else:
            if timing is not None:
                timing["route_sample_cache_lookup_ms"] = 0.0
            global_resolution = cached["global_resolution"]
            corridor_resolution = cached["corridor_resolution"]
        metrics = self._timed_stage(
            timing,
            "progress_cross_track_calculation_ms",
            lambda: self._authorized_route_metrics(position),
        )
        return {
            "position": _point_dict(position),
            "global_resolution": global_resolution,
            "corridor_resolution": corridor_resolution,
            "metrics": dict(metrics) if metrics is not None else None,
        }

    @staticmethod
    def _portal_for_transition(
        plan: Mapping[str, Any],
        from_ref: int,
        to_ref: int,
    ) -> Mapping[str, Any] | None:
        portals = plan.get("portals")
        if not isinstance(portals, list):
            return None
        for portal in portals:
            if (
                isinstance(portal, Mapping)
                and int(portal.get("from_ref", -1)) == from_ref
                and int(portal.get("to_ref", -1)) == to_ref
            ):
                return portal
        return None

    def _route_corridor_refs(self) -> tuple[int, ...]:
        if not isinstance(self.route_plan, Mapping):
            return tuple()
        corridor = self.route_plan.get("polygon_corridor")
        if not isinstance(corridor, (list, tuple)):
            return tuple()
        return tuple(
            int(value)
            for value in corridor
            if isinstance(value, int) and not isinstance(value, bool)
        )

    @staticmethod
    def _portal_crossing_evidence(
        previous_position: Vec3,
        current_position: Vec3,
        portal: Mapping[str, Any],
        *,
        vertical_bound_m: float,
        tolerance_cm: float = 5.0,
    ) -> dict[str, Any]:
        """Check the observed movement segment against one exact portal."""
        left = _parse_point(portal.get("left"))
        right = _parse_point(portal.get("right"))
        movement = _segment_portal_intersection_xy(
            previous_position,
            current_position,
            left,
            right,
            tolerance_cm=tolerance_cm,
        )
        evidence: dict[str, Any] = {
            "portal_left": _point_dict(left),
            "portal_right": _point_dict(right),
            "segment_start": _point_dict(previous_position),
            "segment_end": _point_dict(current_position),
            "intersects": movement is not None,
        }
        if movement is None:
            evidence["reason"] = "movement_segment_misses_authorized_portal"
            return evidence
        segment_t, portal_u, intersection, method = movement
        portal_z = left.z + portal_u * (right.z - left.z)
        segment_z = previous_position.z + segment_t * (current_position.z - previous_position.z)
        vertical_error_m = abs(segment_z - portal_z) / 100.0
        evidence.update({
            "method": method,
            "segment_t": segment_t,
            "portal_u": portal_u,
            "intersection_xy": {"x": intersection[0], "y": intersection[1]},
            "segment_z": segment_z,
            "portal_z": portal_z,
            "vertical_error_m": vertical_error_m,
            "vertical_consistent": vertical_error_m <= vertical_bound_m,
        })
        return evidence

    def _authorized_transition_evidence(
        self,
        previous_sample: Mapping[str, Any] | None,
        current_sample: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Prove one forward corridor hop from consecutive route samples.

        The current point may be a legitimate shared-boundary sample.  In
        that case the corridor resolver deliberately remains ambiguous and
        this method requires the temporal source state, the exact consecutive
        portal, and the observed movement segment before accepting the hop.
        """
        if previous_sample is None or not isinstance(self.route_plan, Mapping):
            return None
        previous_resolution = previous_sample.get("corridor_resolution")
        current_resolution = current_sample.get("corridor_resolution")
        if previous_resolution is None or current_resolution is None:
            return None
        if previous_resolution.status != "resolved":
            return None
        current_is_boundary = (
            current_resolution.status == "ambiguous"
            and current_resolution.reason_code == "authorized_shared_boundary_or_overlap"
        )
        if current_resolution.status != "resolved" and not current_is_boundary:
            return None
        corridor = self._route_corridor_refs()
        if not corridor:
            return None
        from_index = self._route_local_corridor_index
        if from_index is None:
            from_index = previous_resolution.corridor_index
        if from_index is None or from_index < 0 or from_index + 1 >= len(corridor):
            return None
        from_ref = corridor[from_index]
        if previous_resolution.selected_reference != from_ref:
            return None
        if previous_resolution.global_resolution.selected_reference != from_ref:
            return None
        to_index = from_index + 1
        to_ref = corridor[to_index]
        current_candidates = tuple(current_resolution.candidates)
        current_by_ref = {int(candidate.reference): candidate for candidate in current_candidates}
        # The resolver includes nearby snap candidates as diagnostics even
        # when one polygon uniquely contains the sample. Only containment can
        # introduce a competing surface here; proximity alone is not ambiguity.
        containing_refs = {
            reference for reference, candidate in current_by_ref.items() if candidate.contained
        }
        if any(reference not in {from_ref, to_ref} for reference in containing_refs):
            return None
        if current_resolution.status == "resolved":
            if current_resolution.selected_reference != to_ref:
                return None
        else:
            # A boundary proof must not hide a third containing polygon or a
            # global non-corridor winner. Nearby unoccupied polygons remain
            # diagnostic; both consecutive polygons must contain the sample.
            if current_resolution.global_resolution.selected_reference not in {from_ref, to_ref}:
                return None
            if containing_refs != {from_ref, to_ref}:
                return None
            if not all(bool(current_by_ref[ref].contained) for ref in (from_ref, to_ref)):
                return None
        if (
            from_ref is None
            or to_ref is None
            or from_index is None
            or to_index is None
            or to_index != from_index + 1
        ):
            return None
        portal = self._portal_for_transition(self.route_plan, int(from_ref), int(to_ref))
        if portal is None:
            return None
        previous_metrics = previous_sample.get("metrics") or {}
        current_metrics = current_sample.get("metrics") or {}
        if not isinstance(previous_metrics, Mapping) or not isinstance(current_metrics, Mapping):
            return None
        if any(
            float(metrics.get("cross_track_m", math.inf)) > self.config.readiness_reacquire_max_cross_track_m
            for metrics in (previous_metrics, current_metrics)
        ):
            return None
        previous_along = float(previous_metrics.get("along_route_m", -math.inf))
        current_along = float(current_metrics.get("along_route_m", -math.inf))
        # Two forward poses can project to the same clamped waypoint while
        # crossing a real portal. The directed geometric crossing below is
        # still mandatory; only backward projected progress is disallowed.
        if current_along < previous_along:
            return None
        if current_metrics.get("vertical_error_m") is None or float(current_metrics["vertical_error_m"]) > self.config.start_max_vertical_m:
            return None
        portal_left = _parse_point(portal.get("left"))
        portal_right = _parse_point(portal.get("right"))
        portal_span_cm = math.hypot(
            portal_right.x - portal_left.x,
            portal_right.y - portal_left.y,
        )
        portal_tolerance_cm = (
            DEFAULT_PORTAL_LANDING_TOLERANCE_CM
            if portal_span_cm <= DEFAULT_NARROW_PORTAL_MAX_SPAN_CM
            else 5.0
        )
        crossing = self._portal_crossing_evidence(
            _parse_point(previous_sample["position"]),
            _parse_point(current_sample["position"]),
            portal,
            vertical_bound_m=self.config.start_max_vertical_m,
            tolerance_cm=portal_tolerance_cm,
        )
        if crossing.get("intersects") is not True or crossing.get("vertical_consistent") is not True:
            return None
        crossing_metrics = self._authorized_route_metrics(
            Vec3(
                float(crossing["intersection_xy"]["x"]),
                float(crossing["intersection_xy"]["y"]),
                float(crossing["segment_z"]),
            )
        )
        if crossing_metrics is None:
            return None
        portal_along = float(crossing_metrics["along_route_m"])
        if previous_along > portal_along + self.config.readiness_reacquire_max_cross_track_m:
            return None
        if current_along < portal_along - self.config.readiness_reacquire_max_cross_track_m:
            return None
        return {
            "event": "authorized_portal_transition_proven",
            "t": current_sample.get("t"),
            "sequence": current_sample.get("sequence"),
            "previous_sequence": previous_sample.get("sequence"),
            "from_ref": from_ref,
            "to_ref": to_ref,
            "from_corridor_index": from_index,
            "to_corridor_index": to_index,
            "portal": dict(portal),
            "portal_crossing": crossing,
            "portal_route_metrics": dict(crossing_metrics),
            "boundary_sample": current_resolution.status == "ambiguous",
            "global_selected_ref": current_resolution.global_resolution.selected_reference,
            "corridor_candidate_refs": [int(candidate.reference) for candidate in current_candidates],
            "corridor_containing_refs": sorted(containing_refs),
            "route_metrics_before": dict(previous_metrics),
            "route_metrics_after": dict(current_metrics),
        }

    def _bounded_forward_corridor_skip(self, previous, current, local_index: int):
        """Accept Cloud found a few polygons further along the corridor.

        The exact portal-by-portal proof fails when the accepted index lags or
        a sample lands on a boundary; live at Kalm this restarted routes ten
        times in four runs with Cloud squarely on the route. Accepted only
        when Cloud is contained by exactly one polygon, 2 to
        MAX_FORWARD_CORRIDOR_SKIP further along the authorized corridor, both
        resolutions agree on it, he moved at most FORWARD_SKIP_MAX_MOVE_M, and
        route progress grew by no more than that movement (a folded route
        cannot pass as progress). Cross-track and vertical bounds were
        already checked by the caller.
        """
        if previous is None or current is None:
            return None
        resolution = current.get("corridor_resolution")
        corridor = self._route_corridor_refs()
        target = getattr(resolution, "corridor_index", None)
        if resolution is None or resolution.status != "resolved" or target is None:
            return None
        target = int(target)
        if not 2 <= target - local_index <= MAX_FORWARD_CORRIDOR_SKIP or target >= len(corridor):
            return None
        if (resolution.selected_reference != corridor[target]
                or resolution.global_resolution.selected_reference != corridor[target]
                or {int(c.reference) for c in resolution.candidates if c.contained} != {corridor[target]}):
            return None
        try:
            first, last = _parse_point(previous["position"]), _parse_point(current["position"])
        except (KeyError, TypeError, ValueError, AdapterError):
            return None
        moved_m = math.hypot(last.x - first.x, last.y - first.y) / 100.0
        before = float((previous.get("metrics") or {}).get("along_route_m", math.nan))
        after = float((current.get("metrics") or {}).get("along_route_m", math.nan))
        if (not math.isfinite(before) or not math.isfinite(after) or moved_m > FORWARD_SKIP_MAX_MOVE_M
                or after < before - 0.05 or after - before > moved_m + FORWARD_SKIP_ALONG_SLACK_M):
            return None
        return {"event": "authorized_forward_corridor_skip", "t": current.get("t"),
                "sequence": current.get("sequence"), "previous_sequence": previous.get("sequence"),
                "from_corridor_index": local_index, "to_corridor_index": target,
                "from_ref": corridor[local_index], "to_ref": corridor[target],
                "moved_m": moved_m, "along_route_delta_m": after - before}

    def _authorized_multi_portal_evidence(self, previous, current):
        """Prove a bounded observed segment across several tiny ground polygons.

        This does not authorize unobserved steering or an off-mesh shortcut.
        Both ends must resolve uniquely, every exact portal must be crossed in
        order, and each convex interval must remain inside its ground polygon.
        """
        if previous is None or self.topology_session is None:
            return None
        before, after = previous.get("corridor_resolution"), current.get("corridor_resolution")
        corridor = self._route_corridor_refs()
        start, end = self._route_local_corridor_index, getattr(after, "corridor_index", None)
        if (start is None or end is None or not 2 <= end - start <= 8
            or before is None or before.status != "resolved" or after.status != "resolved"
            or not 0 <= start < end < len(corridor)):
            return None
        if (before.selected_reference != corridor[start]
            or before.global_resolution.selected_reference != corridor[start]
            or after.selected_reference != corridor[end]
            or after.global_resolution.selected_reference != corridor[end]
            or {int(c.reference) for c in after.candidates if c.contained} != {corridor[end]}):
            return None
        first, last = _parse_point(previous["position"]), _parse_point(current["position"])
        if math.hypot(last.x - first.x, last.y - first.y) > 200.0:
            return None
        prior, present = previous.get("metrics") or {}, current.get("metrics") or {}
        if (float(present.get("along_route_m", -math.inf)) < float(prior.get("along_route_m", math.inf))
            or any(float(m.get("cross_track_m", math.inf)) > self.config.readiness_reacquire_max_cross_track_m
                   or float(m.get("vertical_error_m", math.inf)) > self.config.start_max_vertical_m
                   for m in (prior, present))):
            return None
        graph = self.topology_session.graph
        portals, crossings = [], []
        last_t = 0.0
        for source, target in zip(corridor[start:end], corridor[start + 1:end + 1]):
            serialized = self._portal_for_transition(self.route_plan, source, target)
            canonical = next((p for p in graph.adjacency.get(source, ())
                              if p.to_reference == target and p.to_dict() == serialized), None)
            if canonical is None:
                return None
            crossing = self._portal_crossing_evidence(first, last, serialized,
                vertical_bound_m=self.config.start_max_vertical_m, tolerance_cm=0.0)
            if (not crossing.get("intersects") or not crossing.get("vertical_consistent")
                or not last_t < crossing["segment_t"] < 1.0):
                return None
            last_t = crossing["segment_t"]
            portals.append(canonical)
            crossings.append(crossing)
        if not _segment_authorized_corridor(first, last, graph=graph,
            corridor_refs=corridor[start:end + 1], portals=portals,
            minimum_edge_clearance_m=0.0,
            max_vertical_separation_m=self.config.start_max_vertical_m):
            return None
        return {"event": "authorized_multi_portal_transition_proven",
            "t": current.get("t"), "sequence": current.get("sequence"),
            "previous_sequence": previous.get("sequence"),
            "from_ref": corridor[start], "to_ref": corridor[end],
            "from_corridor_index": start, "to_corridor_index": end,
            "crossings": crossings, "interval_containment_proven": True}

    def _classify_corridor_divergence(
        self,
        assessment: Mapping[str, Any] | None,
        current_sample: Mapping[str, Any] | None,
        pending: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Classify a corridor result without relaxing its authorization gate."""
        safe = isinstance(assessment, Mapping) and assessment.get("safe") is True
        reason = str(assessment.get("reason_code") or "") if isinstance(assessment, Mapping) else ""
        if safe:
            classification = (
                CorridorDivergenceClass.AUTHORIZED_PORTAL_TRANSITION
                if reason in {"authorized_portal_transition_proven", "authorized_multi_portal_transition_proven"}
                else CorridorDivergenceClass.BOUNDED_CORRIDOR_REACQUISITION
            )
            return {
                "classification": classification.value,
                "reason_code": reason,
                "candidate_count": 0,
                "movement": {},
            }

        movement: dict[str, Any] = {}
        if isinstance(current_sample, Mapping) and isinstance(pending, Mapping):
            try:
                before = _parse_point(pending["position_before"])
                after = _parse_point(current_sample["position"])
                dx_m = (after.x - before.x) / 100.0
                dy_m = (after.y - before.y) / 100.0
                displacement_m = math.hypot(dx_m, dy_m)
                movement.update({
                    "dx_m": dx_m,
                    "dy_m": dy_m,
                    "displacement_m": displacement_m,
                })
                bearing = pending.get("desired_world_heading_deg")
                if bearing is not None and math.isfinite(float(bearing)) and displacement_m > 1.0e-6:
                    radians = math.radians(float(bearing))
                    along_m = dx_m * math.cos(radians) + dy_m * math.sin(radians)
                    lateral_signed_m = -dx_m * math.sin(radians) + dy_m * math.cos(radians)
                    actual_bearing = math.degrees(math.atan2(dy_m, dx_m)) % 360.0
                    bearing_error = abs((actual_bearing - float(bearing) + 180.0) % 360.0 - 180.0)
                    movement.update({
                        "requested_bearing_degrees": float(bearing),
                        "actual_bearing_degrees": actual_bearing,
                        "bearing_disagreement_degrees": bearing_error,
                        "along_m": along_m,
                        "lateral_signed_m": lateral_signed_m,
                        "lateral_m": abs(lateral_signed_m),
                    })
            except (KeyError, TypeError, ValueError):
                movement = {"invalid": True}

        snapshot = self.static_obstacles.snapshot(
            now_t=(current_sample.get("t") if isinstance(current_sample, Mapping) else None)
        )
        candidate_count = int(snapshot.candidate_count)
        strong_lateral = (
            movement.get("displacement_m", 0.0) >= DEFAULT_COLLISION_DIVERGENCE_MIN_DISPLACEMENT_M
            and movement.get("lateral_m", 0.0) >= DEFAULT_COLLISION_DIVERGENCE_MIN_LATERAL_M
            and movement.get("bearing_disagreement_degrees", 0.0) >= DEFAULT_COLLISION_DIVERGENCE_MIN_BEARING_ERROR_DEGREES
        )
        provisional_handoff = (
            candidate_count > 0
            and reason in {
                "global_non_corridor_with_authorized_candidate",
                "corridor_membership_not_authorized",
            }
        )
        classification = (
            CorridorDivergenceClass.PROBABLE_COLLISION_DIVERGENCE
            if strong_lateral
            else CorridorDivergenceClass.PROVISIONAL_STATIC_HANDOFF
            if provisional_handoff
            else CorridorDivergenceClass.UNEXPLAINED_UNAUTHORIZED_DIVERGENCE
        )
        return {
            "classification": classification.value,
            "reason_code": reason,
            "candidate_count": candidate_count,
            "movement": movement,
            "strong_lateral_displacement": strong_lateral,
            "static_snapshot_generation": snapshot.generation,
        }

    def _route_local_sample_assessment(
        self,
        previous_sample: Mapping[str, Any] | None,
        current_sample: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Classify one observation against the temporal authorized corridor.

        Global polygon resolution remains diagnostic.  This state machine is
        the only path that can accept a multi-polygon/multi-tile live sample.
        It permits forward portal crossings with exact geometric proof, then keeps the
        accepted corridor index monotonic through a known overlap.  Every
        other ambiguity, skip, backwards movement, or departure fails closed.
        """
        if current_sample is None:
            return {"safe": False, "reason_code": "corridor_sample_missing", "failure_class": "departure"}
        resolution = current_sample.get("corridor_resolution")
        metrics = current_sample.get("metrics") or {}
        if resolution is None or not isinstance(metrics, Mapping):
            return {"safe": False, "reason_code": "corridor_resolution_missing", "failure_class": "departure"}
        candidates = tuple(resolution.candidates)
        candidate_refs = {int(candidate.reference) for candidate in candidates}
        global_ref = resolution.global_resolution.selected_reference
        if not candidates:
            return {
                "safe": False,
                "reason_code": "no_authorized_corridor_candidate",
                "failure_class": "departure",
                "corridor_resolution": resolution.to_dict(),
            }
        if global_ref not in candidate_refs:
            return {
                "safe": False,
                "reason_code": "global_non_corridor_with_authorized_candidate",
                "failure_class": "departure",
                "corridor_resolution": resolution.to_dict(),
            }
        if float(metrics.get("cross_track_m", math.inf)) > self.config.readiness_reacquire_max_cross_track_m:
            return {
                "safe": False,
                "reason_code": "route_cross_track_exceeds_bound",
                "failure_class": "departure",
                "corridor_resolution": resolution.to_dict(),
            }
        if float(metrics.get("vertical_error_m", math.inf)) > self.config.start_max_vertical_m:
            return {
                "safe": False,
                "reason_code": "route_vertical_error_exceeds_bound",
                "failure_class": "departure",
                "corridor_resolution": resolution.to_dict(),
            }
        corridor = self._route_corridor_refs()
        local_index = self._route_local_corridor_index
        local_ref = self._route_local_corridor_ref
        if local_index is None and local_ref in corridor:
            local_index = corridor.index(local_ref)
        if local_index is None and resolution.status == "resolved" and resolution.corridor_index is not None:
            local_index = int(resolution.corridor_index)
            local_ref = corridor[local_index] if 0 <= local_index < len(corridor) else None
        if local_index is None or local_index < 0 or local_index >= len(corridor):
            return {"safe": False, "reason_code": "route_local_corridor_state_missing", "failure_class": "departure"}
        local_ref = corridor[local_index]

        transition = self._authorized_transition_evidence(previous_sample, current_sample)
        if transition is not None:
            return {
                "safe": True,
                "reason_code": "authorized_portal_transition_proven",
                "failure_class": "none",
                "accepted_index": local_index + 1,
                "accepted_ref": corridor[local_index + 1],
                "transition": transition,
                "corridor_resolution": resolution.to_dict(),
            }

        multi_transition = self._authorized_multi_portal_evidence(previous_sample, current_sample)
        if multi_transition is not None:
            return {"safe": True, "reason_code": "authorized_multi_portal_transition_proven",
                "failure_class": "none", "accepted_index": multi_transition["to_corridor_index"],
                "accepted_ref": multi_transition["to_ref"], "transition": multi_transition,
                "corridor_resolution": resolution.to_dict()}

        forward_skip = self._bounded_forward_corridor_skip(previous_sample, current_sample, local_index)
        if forward_skip is not None:
            return {"safe": True, "reason_code": "authorized_forward_corridor_skip",
                "failure_class": "none", "accepted_index": forward_skip["to_corridor_index"],
                "accepted_ref": forward_skip["to_ref"], "forward_skip": forward_skip,
                "corridor_resolution": resolution.to_dict()}

        selected_index = resolution.corridor_index
        if resolution.status == "resolved" and resolution.selected_reference == local_ref:
            return {
                "safe": True,
                "reason_code": "inside_authorized_corridor",
                "failure_class": "none",
                "accepted_index": local_index,
                "accepted_ref": local_ref,
                "corridor_resolution": resolution.to_dict(),
            }

        # Once a forward hop has been proven, a later global tie-break may
        # still select the source polygon while the point remains in the
        # shared overlap.  Preserve the monotonic route-local target only
        # when the point still has the adjacent authorized candidate and the
        # route progress has not moved backwards.
        if local_index > 0 and local_index < len(corridor):
            overlap_refs = {corridor[local_index - 1], local_ref}
            containing_refs = {int(candidate.reference) for candidate in candidates if candidate.contained}
            if (
                overlap_refs.issubset(candidate_refs)
                and containing_refs.issubset(overlap_refs)
                and global_ref in overlap_refs
            ):
                previous_along = self._last_route_along_m
                current_along = float(metrics.get("along_route_m", -math.inf))
                if previous_along is None or current_along >= previous_along - self.config.readiness_reacquire_max_cross_track_m:
                    return {
                        "safe": True,
                        "reason_code": "authorized_corridor_overlap_continuity",
                        "failure_class": "none",
                        "accepted_index": local_index,
                        "accepted_ref": local_ref,
                        "corridor_resolution": resolution.to_dict(),
                    }

        if resolution.reason_code in {"authorized_shared_boundary_or_overlap", "authorized_snap_ambiguous"}:
            reason_code = "authorized_boundary_ambiguity_unproven"
            failure_class = "ambiguity"
        elif selected_index is not None and int(selected_index) > local_index + 1:
            reason_code = "corridor_transition_skipped"
            failure_class = "departure"
        elif selected_index is not None and int(selected_index) < local_index:
            reason_code = "corridor_transition_backward"
            failure_class = "departure"
        else:
            reason_code = "corridor_membership_not_authorized"
            failure_class = "departure"
        return {
            "safe": False,
            "reason_code": reason_code,
            "failure_class": failure_class,
            "corridor_resolution": resolution.to_dict(),
            "accepted_index": local_index,
            "accepted_ref": local_ref,
        }

    def _commit_route_local_sample(
        self,
        sample: Mapping[str, Any] | None,
        assessment: Mapping[str, Any],
    ) -> None:
        if not assessment.get("safe") or sample is None:
            return
        accepted_index = assessment.get("accepted_index")
        accepted_ref = assessment.get("accepted_ref")
        if accepted_index is not None:
            self._route_local_corridor_index = int(accepted_index)
        if accepted_ref is not None:
            self._route_local_corridor_ref = int(accepted_ref)
        try:
            self._route_local_last_position = _parse_point(sample["position"])
        except (KeyError, TypeError, ValueError):
            self._route_local_last_position = None
        sequence = sample.get("sequence")
        self._route_local_last_sequence = int(sequence) if sequence is not None else None

    def _learn_static_obstacle_from_post_command(
        self,
        observation: LiveObservation,
        pending: Mapping[str, Any],
        metrics: Mapping[str, Any],
        displacement_m: float,
    ) -> None:
        """Convert repeated authorized no-progress into scoped evidence."""
        before_along = pending.get("along_route_before_m")
        after_along = metrics.get("along_route_m") if isinstance(metrics, Mapping) else None
        try:
            route_delta = (
                float(after_along) - float(before_along)
                if before_along is not None and after_along is not None
                else 0.0
            )
        except (TypeError, ValueError):
            route_delta = math.nan
        command_duration_ms = int(pending.get("command_duration_ms", 0))
        command_sequence = pending.get("sequence")
        if 0 < command_duration_ms < MIN_STATIC_OBSTACLE_COMMAND_DURATION_MS:
            self.timeline.append({
                "event": "static_obstacle_short_pulse_not_evidence",
                "t": observation.monotonic_t,
                "sequence": observation.sequence,
                "command_sequence": command_sequence,
                "command_duration_ms": command_duration_ms,
                "minimum_sustained_duration_ms": MIN_STATIC_OBSTACLE_COMMAND_DURATION_MS,
                "reason": "movement_hold_below_gameplay_recognition_floor",
                "movement_command_sent": False,
            })
            return
        no_progress = (
            math.isfinite(displacement_m)
            and displacement_m <= self.static_obstacles.no_progress_displacement_m
            and math.isfinite(route_delta)
            and route_delta <= self.static_obstacles.no_progress_route_delta_m
            and command_duration_ms >= MIN_STATIC_OBSTACLE_COMMAND_DURATION_MS
            and float(pending.get("magnitude", 0.0)) > 0.0
        )
        proof = self.authorization_proof
        command_key = (
            (proof.route_plan_sha256, command_sequence)
            if proof is not None and type(command_sequence) is int and command_sequence >= 0
            else None
        )
        previous_command = self._static_last_evidence_command
        duplicate_command = (
            command_key is not None and previous_command is not None
            and command_key[0] == previous_command[0]
            and command_key[1] <= previous_command[1]
        )
        if not no_progress:
            # Later advancing feedback from the SAME input may finally show
            # movement. Process that progress before suppressing duplicates.
            self._static_no_progress_streak = 0
            measured_progress = (
                math.isfinite(displacement_m)
                and displacement_m > self.static_obstacles.no_progress_displacement_m
            ) or (math.isfinite(route_delta) and route_delta > self.static_obstacles.no_progress_route_delta_m)
            scope = self.static_obstacles.scope
            if measured_progress and scope is not None:
                if command_key is not None and not duplicate_command:
                    self._static_last_evidence_command = command_key
                removed = self.static_obstacles.reset_movement_candidates(scope=scope)
                if removed:
                    self.timeline.append({"event": "static_obstacle_candidates_reset_for_progress",
                        "t": observation.monotonic_t, "sequence": observation.sequence,
                        "discarded_movement_samples": removed, "displacement_m": displacement_m,
                        "route_delta_m": route_delta, "committed_obstacles_preserved": True})
            return
        if command_key is None or duplicate_command:
            self.timeline.append({
                "event": "static_obstacle_command_feedback_not_independent",
                "t": observation.monotonic_t, "sequence": observation.sequence,
                "command_sequence": command_sequence,
                "reason": "duplicate_command" if duplicate_command else "command_identity_missing",
                "movement_command_sent": False,
            })
            return
        self._static_last_evidence_command = command_key
        self._static_no_progress_streak += 1
        query_status: str | None = None
        query_reason: str | None = None
        query_verified = False
        blocking_distance_m: float | None = None
        capsule_radius_m: float | None = None
        probe_world_bearing_degrees: float | None = None
        if self.lookahead is not None and self.authorization is not None:
            desired = pending.get("desired_world_heading_deg")
            if desired is not None:
                decision = self.lookahead.inspect(
                    observation.raw,
                    sequence=observation.sequence,
                    route_plan_sha256=self.authorization.route_plan_sha256,
                    desired_world_bearing_degrees=float(desired),
                    camera_world_yaw_degrees=observation.camera_yaw,
                    horizon_m=3.0,
                    force=True,
                )
                query_reason = decision.reason_code
                if decision.state == ObstacleEvidenceState.READY:
                    query_status = "blocked" if decision.reason_code == "lookahead_capsule_blocked" else "clear"
                    query_verified = decision.query_verified is True
                    blocking_distance_m = decision.clear_distance_m
                    capsule_radius_m = decision.capsule_radius_m
                    probe_world_bearing_degrees = decision.probe_world_bearing_degrees
                self.timeline.append({
                    "event": "static_obstacle_lookahead",
                    "t": observation.monotonic_t,
                    "sequence": observation.sequence,
                    "decision": decision.state.value,
                    "reason": decision.reason_code,
                    "source": decision.source,
                    "clear_distance_m": decision.clear_distance_m,
                    "bounded": True,
                })
        proof = self.authorization_proof
        scope = self.static_obstacles.scope
        if proof is None or scope is None:
            return
        dynamic_evidence = self._dynamic_static_learning_evidence(
            observation,
            pending.get("desired_world_heading_deg"),
        )
        learning_kwargs = dict(
            scope=scope,
            route_plan_sha256=proof.route_plan_sha256,
            route=proof.waypoints,
            position_before=_parse_point(pending["position_before"]),
            position_after=observation.position,
            along_route_before_m=before_along,
            along_route_after_m=after_along,
            commanded_bearing_degrees=pending.get("desired_world_heading_deg"),
            command_duration_ms=command_duration_ms,
            sequence=observation.sequence,
            sampled_t=observation.monotonic_t,
            telemetry_fresh=True,
            query_status=query_status,
            query_provenance=query_reason,
            query_verified=query_verified,
            capsule_radius_m=capsule_radius_m,
            blocking_distance_m=blocking_distance_m,
            probe_world_bearing_degrees=probe_world_bearing_degrees,
            dynamic_evidence=dynamic_evidence,
        )
        result = self.static_obstacles.learn_from_movement(**learning_kwargs, defer_movement_commit=True)
        verified_blocker = query_verified is True and query_status == "blocked"
        provisional = (
            result.reason_code == "static_obstacle_pending_settled_confirmation"
            and verified_blocker
        )
        # Movement-only no-progress is diagnostic evidence, not proof that a
        # static obstacle occupies the corridor. The current UE4SS line trace
        # can report `lookahead_line_only_not_walkability_proof` for an
        # open-looking route. Keep the candidate for forensics and let the
        # normal readiness/stall path fail closed, but do not invalidate a
        # validated route without an independent capsule hit.
        self._static_pending_learning_kwargs = learning_kwargs if provisional else None
        self._static_last_learning = {
            "state": result.state.value,
            "reason_code": result.reason_code,
            "evidence_count": result.evidence_count,
            "generation": result.generation,
            "obstacle": result.obstacle.to_dict() if result.obstacle is not None else None,
            "dynamic_evidence": dynamic_evidence.to_dict(),
        }
        self.timeline.append({
            "event": "static_obstacle_learning",
            "t": observation.monotonic_t,
            "sequence": observation.sequence,
            "command_sequence": command_sequence,
            "no_progress_streak": self._static_no_progress_streak,
            **self._static_last_learning,
        })
        if not verified_blocker and result.reason_code in {
            "static_obstacle_candidate_recorded",
            "static_obstacle_pending_settled_confirmation",
        }:
            self.timeline.append({
                "event": "static_obstacle_candidate_deferred_unverified",
                "t": observation.monotonic_t,
                "sequence": observation.sequence,
                "command_sequence": command_sequence,
                "reason": query_reason or "lookahead_not_verified",
                "query_status": query_status,
                "query_verified": query_verified,
                "movement_command_sent": False,
            })
        if verified_blocker and (result.state == ObstacleEvidenceState.READY or provisional) and result.obstacle is not None:
            self._static_pending_discovery = {
                "provisional_movement_only": provisional,
                "obstacle": result.obstacle.to_dict(),
                "reason_code": result.reason_code,
                "sequence": observation.sequence,
                "route_plan_sha256": proof.route_plan_sha256,
            }

    def _learn_verified_lookahead_blocker(
        self,
        observation: LiveObservation,
        steering: Mapping[str, Any],
        decision: Any,
    ) -> bool:
        """Refuse precommand promotion until a sustained pulse is observed.

        A capsule hit is useful lookahead evidence, but it is not proof that
        Cloud is blocked. The game may not have registered the input yet, and
        a sidecar hit can describe nearby geometry that the authorized route
        can still traverse. Static obstacle admission belongs to the
        post-command path, which requires an observed 250 ms movement attempt.
        """
        self.timeline.append({
            "event": "static_obstacle_precommand_blocker_deferred",
            "t": observation.monotonic_t,
            "sequence": observation.sequence,
            "reason": "requires_sustained_movement_evidence",
            "command_duration_ms": int(steering.get("command_duration_ms") or 0),
            "minimum_sustained_duration_ms": MIN_STATIC_OBSTACLE_COMMAND_DURATION_MS,
            "clear_distance_m": decision.clear_distance_m,
            "probe_world_bearing_degrees": decision.probe_world_bearing_degrees,
            "movement_command_sent": False,
        })
        return False

    def _learn_post_divergence_capsule_sample(
        self,
        observation: LiveObservation,
        pending: Mapping[str, Any],
        decision: Any,
    ) -> Any:
        """Record one stationary capsule sample without authorizing motion."""
        proof = self.authorization_proof
        scope = self.static_obstacles.scope
        if proof is None or scope is None or decision.query_verified is not True:
            return None
        metrics = self._authorized_route_metrics(observation.position) or {}
        result = self.static_obstacles.learn_from_movement(
            scope=scope,
            route_plan_sha256=proof.route_plan_sha256,
            route=proof.waypoints,
            position_before=observation.position,
            position_after=observation.position,
            along_route_before_m=metrics.get("along_route_m"),
            along_route_after_m=metrics.get("along_route_m"),
            commanded_bearing_degrees=pending.get("desired_world_heading_deg"),
            command_duration_ms=max(MIN_COMMAND_DURATION_MS, int(pending.get("command_duration_ms") or MIN_COMMAND_DURATION_MS)),
            sequence=observation.sequence,
            sampled_t=observation.monotonic_t,
            telemetry_fresh=True,
            query_status="blocked",
            capsule_radius_m=decision.capsule_radius_m,
            query_provenance="post_divergence_capsule_confirmation",
            query_verified=True,
            blocking_distance_m=decision.clear_distance_m,
            probe_world_bearing_degrees=decision.probe_world_bearing_degrees,
            dynamic_evidence=self._dynamic_static_learning_evidence(
                observation,
                pending.get("desired_world_heading_deg"),
            ),
            require_independent_confirmation=True,
        )
        self._static_last_learning = {
            "state": result.state.value,
            "reason_code": result.reason_code,
            "evidence_count": result.evidence_count,
            "generation": result.generation,
            "obstacle": result.obstacle.to_dict() if result.obstacle is not None else None,
            "source": "post_divergence_capsule_confirmation",
            "sequence": observation.sequence,
        }
        self.timeline.append({
            "event": "static_obstacle_learning",
            "t": observation.monotonic_t,
            "sequence": observation.sequence,
            "no_progress_streak": self._static_no_progress_streak,
            **self._static_last_learning,
            "movement_command_sent": False,
        })
        return result

    def _confirm_static_obstacle_after_divergence(
        self,
        observation: LiveObservation,
        pending: Mapping[str, Any],
        classification: Mapping[str, Any],
    ) -> bool:
        """Use a small fresh, neutral-only window to corroborate a blocker."""
        if classification.get("classification") not in {
            CorridorDivergenceClass.PROBABLE_COLLISION_DIVERGENCE.value,
            CorridorDivergenceClass.PROVISIONAL_STATIC_HANDOFF.value,
        }:
            return False
        if self.lookahead is None or self.authorization_proof is None:
            self.timeline.append({
                "event": "corridor_divergence_confirmation_unavailable",
                "t": observation.monotonic_t,
                "sequence": observation.sequence,
                "reason": "capsule_lookahead_unavailable",
                "movement_command_sent": False,
            })
            return False
        self._neutralize("corridor_divergence_confirmation_started")
        before_digest = self._static_obstacle_digest()
        self.timeline.append({
            "event": "corridor_divergence_confirmation_started",
            "t": observation.monotonic_t,
            "sequence": observation.sequence,
            "classification": classification.get("classification"),
            "reason_code": classification.get("reason_code"),
            "samples_bound": DEFAULT_POST_DIVERGENCE_CONFIRMATION_SAMPLES,
            "movement_command_sent": False,
            "actuator_neutral": self.actuator.is_neutral(),
        })
        current = observation
        seen_sequences: set[int] = set()
        confirmation_results: list[dict[str, Any]] = []
        for sample_index in range(DEFAULT_POST_DIVERGENCE_CONFIRMATION_SAMPLES):
            if current.sequence in seen_sequences:
                break
            seen_sequences.add(current.sequence)
            desired = pending.get("desired_world_heading_deg")
            if desired is None or not math.isfinite(float(desired)):
                confirmation_results.append({
                    "sequence": current.sequence,
                    "reason": "requested_bearing_missing",
                    "movement_command_sent": False,
                })
                break
            decision = self.lookahead.inspect(
                current.raw,
                sequence=current.sequence,
                route_plan_sha256=self.authorization_proof.route_plan_sha256,
                desired_world_bearing_degrees=float(desired),
                camera_world_yaw_degrees=current.camera_yaw,
                horizon_m=3.0,
                force=True,
            )
            confirmation_results.append({
                "sequence": current.sequence,
                "decision": decision.state.value,
                "reason": decision.reason_code,
                "source": decision.source,
                "clear_distance_m": decision.clear_distance_m,
                "query_verified": decision.query_verified,
                "probe_world_bearing_degrees": decision.probe_world_bearing_degrees,
                "movement_command_sent": False,
            })
            self.timeline.append({
                "event": "corridor_divergence_confirmation_sample",
                "t": current.monotonic_t,
                "sequence": current.sequence,
                "sample_index": sample_index,
                "decision": decision.state.value,
                "reason": decision.reason_code,
                "source": decision.source,
                "clear_distance_m": decision.clear_distance_m,
                "query_verified": decision.query_verified,
                "movement_command_sent": False,
                "actuator_neutral": self.actuator.is_neutral(),
            })
            result = None
            if decision.reason_code == "lookahead_capsule_blocked" and decision.query_verified is True:
                result = self._learn_post_divergence_capsule_sample(current, pending, decision)
                if result is not None and result.state == ObstacleEvidenceState.READY and result.obstacle is not None:
                    after_digest = self._static_obstacle_digest()
                    self._static_pending_discovery = {
                        "obstacle": result.obstacle.to_dict(),
                        "reason_code": "static_obstacle_confirmed_after_corridor_divergence",
                        "sequence": current.sequence,
                        "route_plan_sha256": self.authorization_proof.route_plan_sha256,
                        "classification": classification.get("classification"),
                        "obstacle_digest_before": before_digest,
                        "obstacle_digest_after": after_digest,
                        "confirmation_samples": confirmation_results,
                    }
                    self.timeline.append({
                        "event": "static_obstacle_confirmed_after_corridor_divergence",
                        "t": current.monotonic_t,
                        "sequence": current.sequence,
                        "obstacle": result.obstacle.to_dict(),
                        "obstacle_digest_before": before_digest,
                        "obstacle_digest_after": after_digest,
                        "confirmation_samples": confirmation_results,
                        "movement_command_sent": False,
                    })
                    return True
            if sample_index + 1 >= DEFAULT_POST_DIVERGENCE_CONFIRMATION_SAMPLES:
                break
            try:
                current = self._require_observation(
                    wait_for_fresh=True,
                    after_sequence=current.sequence,
                    timeout_seconds=DEFAULT_POST_DIVERGENCE_CONFIRMATION_TIMEOUT_SECONDS,
                    require_effective_fresh=True,
                )
            except AdapterError as exc:
                confirmation_results.append({
                    "reason": str(exc),
                    "movement_command_sent": False,
                })
                break
            failures = self._check_preconditions(current)
            if failures or not self._same_authorization(current):
                confirmation_results.append({
                    "sequence": current.sequence,
                    "reason": "confirmation_precondition_failed",
                    "failures": failures,
                    "movement_command_sent": False,
                })
                break
        self.timeline.append({
            "event": "corridor_divergence_confirmation_finished",
            "t": self.monotonic(),
            "sequence": current.sequence,
            "confirmed": False,
            "samples": confirmation_results,
            "movement_command_sent": False,
            "actuator_neutral": self.actuator.is_neutral(),
        })
        return False

    def _dynamic_static_learning_evidence(
        self,
        observation: LiveObservation,
        intended_bearing_degrees: Any,
    ) -> Any:
        """Adapt the production typed actor snapshot at the route boundary."""

        try:
            bearing = float(intended_bearing_degrees)
        except (TypeError, ValueError):
            bearing = None
        cloud_position_m = Vec3(
            observation.position.x / 100.0,
            observation.position.y / 100.0,
            observation.position.z / 100.0,
        )
        from .dynamic_obstacles import Point3M

        return assess_static_learning_dynamic_evidence(
            observation.dynamic_obstacles,
            cloud_position_m=Point3M(cloud_position_m.x, cloud_position_m.y, cloud_position_m.z),
            intended_bearing_degrees=bearing,
            expected_sequence=observation.sequence,
            max_age_seconds=max(0.1, self.config.max_telemetry_age_seconds),
        )

    def _precommand_static_obstacle_gate(
        self,
        observation: LiveObservation,
        steering: Mapping[str, Any],
    ) -> str:
        """Run bounded short lookahead on explicit low-rate route triggers."""
        if self.lookahead is None or self.authorization is None:
            return "not_checked"
        if str(steering.get("control_band") or "stop") == "stop":
            return "not_needed"
        if self._traversal_link_near(observation.position):
            return "traversal_link_ahead"
        budget_window_renewed = False
        if self.config.static_lookahead_budget_window_seconds is not None:
            budget_window_renewed = self.lookahead.renew_budget_window(
                self.authorization.route_plan_sha256,
                now_t=observation.monotonic_t,
                interval_seconds=self.config.static_lookahead_budget_window_seconds,
            )
        trigger = "initial_plan_area"
        if self._static_precommand_lookahead_checked:
            budget = self.lookahead.budget_status(self.authorization.route_plan_sha256)
            if bool(budget.get("exhausted")):
                return "lookahead_budget_exhausted"
            if self._static_lookahead_last_position is None or self._static_lookahead_last_t is None:
                return "lookahead_not_due"
            distance_since_query_m = math.hypot(
                observation.position.x - self._static_lookahead_last_position.x,
                observation.position.y - self._static_lookahead_last_position.y,
            ) / 100.0
            heading = float(steering.get("desired_world_heading_deg"))
            previous_heading = (
                heading
                if self._static_lookahead_last_heading is None
                else float(self._static_lookahead_last_heading)
            )
            heading_delta = abs((heading - previous_heading + 180.0) % 360.0 - 180.0)
            polygon_ref = self._route_local_corridor_ref or self._last_resolved_polygon_ref
            if distance_since_query_m < self.config.static_lookahead_distance_threshold_m and (
                float(observation.monotonic_t) - self._static_lookahead_last_t
            ) < self.config.static_lookahead_interval_seconds and (
                polygon_ref == self._static_lookahead_last_polygon_ref
            ) and heading_delta < self.config.static_lookahead_heading_change_degrees:
                return "lookahead_not_due"
            if distance_since_query_m >= self.config.static_lookahead_distance_threshold_m:
                trigger = "travelled_distance_threshold"
            elif polygon_ref != self._static_lookahead_last_polygon_ref:
                trigger = "entered_new_polygon"
            elif heading_delta >= self.config.static_lookahead_heading_change_degrees:
                trigger = "heading_change"
            else:
                trigger = "bounded_interval_elapsed"
        self._static_precommand_lookahead_checked = True
        decision = self.lookahead.inspect(
            observation.raw,
            sequence=observation.sequence,
            route_plan_sha256=self.authorization.route_plan_sha256,
            desired_world_bearing_degrees=float(steering["desired_world_heading_deg"]),
            camera_world_yaw_degrees=observation.camera_yaw,
            horizon_m=3.0,
            force=True,
        )
        self._static_lookahead_last_position = observation.position
        self._static_lookahead_last_t = observation.monotonic_t
        self._static_lookahead_last_heading = float(steering["desired_world_heading_deg"])
        self._static_lookahead_last_polygon_ref = self._route_local_corridor_ref or self._last_resolved_polygon_ref
        self.timeline.append({
            "event": "static_obstacle_precommand_lookahead",
            "t": observation.monotonic_t,
            "sequence": observation.sequence,
            "decision": decision.state.value,
            "reason": decision.reason_code,
            "source": decision.source,
            "clear_distance_m": decision.clear_distance_m,
            "selected_direction": decision.selected_direction,
            "probe_relative_yaw_degrees": decision.probe_relative_yaw_degrees,
            "probe_world_bearing_degrees": decision.probe_world_bearing_degrees,
            "trigger": trigger,
            "budget": self.lookahead.budget_status(self.authorization.route_plan_sha256),
            "budget_window_renewed": budget_window_renewed,
            "bounded": True,
            "critical_path": "low_rate_triggered_sidecar_not_control_pulse",
        })
        if decision.reason_code != "lookahead_capsule_blocked" or decision.query_verified is not True:
            return "no_verified_blocker"
        heading = math.radians(float(steering["desired_world_heading_deg"]))
        segment_end = Vec3(observation.position.x + math.cos(heading) * 300.0,
                           observation.position.y + math.sin(heading) * 300.0, observation.position.z)
        intersects = self.static_obstacles.capsule_hit_intersects_segment(
            observation.position, segment_end,
            probe_bearing_degrees=decision.probe_world_bearing_degrees,
            blocking_distance_m=decision.clear_distance_m,
            capsule_radius_m=decision.capsule_radius_m)
        if intersects is False:
            self.timeline.append({"event": "capsule_hit_outside_command_segment",
                "sequence": observation.sequence, "probe_world_bearing_degrees": decision.probe_world_bearing_degrees,
                "command_world_bearing_degrees": steering["desired_world_heading_deg"],
                "clear_distance_m": decision.clear_distance_m, "movement_command_sent": False})
            return "no_verified_blocker"
        return "blocked" if self._learn_verified_lookahead_blocker(observation, steering, decision) else "no_verified_blocker"

    def _record_post_command_observation(
        self,
        observation: LiveObservation,
        event: Mapping[str, Any] | None = None,
    ) -> None:
        """Record the first fresh observation after the preceding pulse.

        The adapter never treats an expected displacement as proof of motion;
        this diagnostic is deliberately derived from the observed position.
        It is emitted before any next non-neutral command can be selected.
        """
        pending = self._pending_command_evidence
        if not pending or observation.sequence <= int(pending["sequence"]):
            return
        if self._last_post_command_observation_sequence == observation.sequence:
            return
        if observation.grounded is False:
            # An automatic leap is not a ground-surface sample. Preserve it
            # before the readiness pause without mutating corridor authority
            # or teaching the obstacle learner a fictitious collision.
            self.timeline.append({"event": "post_command_airborne_observation",
                "t": observation.monotonic_t, "sequence": observation.sequence,
                "command_sequence": pending["sequence"],
                "position_before": pending["position_before"],
                "position_after": _point_dict(observation.position),
                "observation": self._observation_evidence(observation),
                "ground_membership_deferred_until_landing": True})
            self._last_post_command_observation_sequence = observation.sequence
            return
        before = _parse_point(pending["position_before"])
        dx = observation.position.x - before.x
        dy = observation.position.y - before.y
        dz = observation.position.z - before.z
        displacement_m = math.sqrt(dx * dx + dy * dy + dz * dz) / 100.0
        polygon_resolution: Mapping[str, Any] | None = None
        current_polygon_ref: int | None = None
        route_sample: dict[str, Any] | None = None
        corridor_resolution = None
        if self.topology_session is not None:
            try:
                if self.config.route_mode in CORRIDOR_ROUTE_MODES:
                    route_sample = self._route_polygon_sample(
                        observation.position,
                        sequence=observation.sequence,
                    )
                    resolution = route_sample["global_resolution"] if route_sample is not None else None
                    corridor_resolution = route_sample["corridor_resolution"] if route_sample is not None else None
                else:
                    resolution = self.topology_session.graph.resolve_point(
                        observation.position,
                        max_snap_distance_m=self.config.start_max_snap_m,
                        max_vertical_separation_m=self.config.start_max_vertical_m,
                    )
                if resolution is None:
                    raise AdapterError("polygon_resolution_unavailable")
                polygon_resolution = resolution.to_dict()
                if resolution.selected_reference is not None:
                    current_polygon_ref = int(resolution.selected_reference)
            except Exception as exc:
                polygon_resolution = {"status": "unresolved", "reason_code": "resolution_exception"}
                self.timeline.append({
                    "event": "global_polygon_resolution_failed",
                    "t": observation.monotonic_t,
                    "sequence": observation.sequence,
                    "error": str(exc),
                })
        metrics = (
            route_sample.get("metrics")
            if isinstance(route_sample, Mapping) and isinstance(route_sample.get("metrics"), Mapping)
            else self._authorized_route_metrics(observation.position)
            or {}
        )
        previous_polygon_ref = self._last_resolved_polygon_ref
        if current_polygon_ref is not None:
            self._last_resolved_polygon_ref = current_polygon_ref
            if (
                self.config.route_mode in CORRIDOR_ROUTE_MODES
                and previous_polygon_ref is not None
                and current_polygon_ref != previous_polygon_ref
            ):
                self.timeline.append({
                    "event": "global_polygon_resolution_changed",
                    "t": observation.monotonic_t,
                    "sequence": observation.sequence,
                    "from_ref": previous_polygon_ref,
                    "to_ref": current_polygon_ref,
                    "position": _point_dict(observation.position),
                    "route_metrics": dict(metrics),
                    "corridor_resolution": corridor_resolution.to_dict() if corridor_resolution is not None else None,
                })
        route_local_assessment = (
            self._route_local_sample_assessment(self._last_corridor_sample, route_sample)
            if route_sample is not None
            else None
        )
        authorized_transition = (
            route_local_assessment.get("transition")
            if isinstance(route_local_assessment, Mapping)
            else None
        )
        if authorized_transition is not None:
            self.timeline.append(authorized_transition)
        if (
            isinstance(route_local_assessment, Mapping)
            and route_local_assessment.get("safe") is True
            and metrics.get("along_route_m") is not None
        ):
            self._last_route_along_m = float(metrics["along_route_m"])
        accounting = self._follower_progress_accounting(observation.monotonic_t)
        before_anchor = pending.get("last_meaningful_progress_t_before")
        after_anchor = accounting.get("last_meaningful_progress_t") if accounting else None
        meaningful_progress_anchor_reset = (
            before_anchor is not None
            and after_anchor is not None
            and float(after_anchor) > float(before_anchor)
        )
        self.timeline.append({
            "event": "post_command_observation",
            "t": observation.monotonic_t,
            "command_sequence": pending["sequence"],
            "sequence": observation.sequence,
            "telemetry_age_seconds": observation.telemetry_age_seconds,
            "time_since_command_completion_seconds": (
                max(0.0, observation.monotonic_t - float(pending["command_completed_t"]))
            ),
            "position_before": pending["position_before"],
            "position_after": _point_dict(observation.position),
            "observed_displacement_m": displacement_m,
            "distance_to_final_before_m": pending.get("distance_to_final_before_m"),
            "distance_to_final_after_m": _distance_m(observation.position, self.authorization.resolved_end)
            if self.authorization is not None else None,
            "along_route_displacement_m": (
                float(metrics.get("along_route_m", 0.0)) - float(pending.get("along_route_before_m", 0.0))
            ) if metrics else None,
            "route_progress_before": pending.get("route_progress_before"),
            "route_progress_after": (
                float(metrics.get("along_route_m", 0.0)) / float(metrics.get("route_length_m"))
                if metrics and float(metrics.get("route_length_m", 0.0)) > 0.0 else None
            ),
            "cross_track_before_m": pending.get("cross_track_before_m"),
            "cross_track_after_m": metrics.get("cross_track_m") if metrics else None,
            "remaining_route_distance_after_m": metrics.get("remaining_route_distance_m") if metrics else None,
            "route_segment_index_after": metrics.get("segment_index") if metrics else None,
            "current_polygon_ref": current_polygon_ref,
            "polygon_resolution": dict(polygon_resolution) if polygon_resolution is not None else None,
            "corridor_resolution": corridor_resolution.to_dict() if corridor_resolution is not None else None,
            "route_local_corridor_state": {
                "assessment": dict(route_local_assessment) if route_local_assessment is not None else None,
                "accepted_index": self._route_local_corridor_index,
                "accepted_ref": self._route_local_corridor_ref,
            },
            "active_stall_elapsed_before_seconds": pending.get("active_stall_elapsed_before_seconds"),
            "active_stall_elapsed_after_seconds": (
                accounting.get("active_stall_elapsed_seconds") if accounting else None
            ),
            "last_meaningful_progress_t_before": before_anchor,
            "last_meaningful_progress_t_after": after_anchor,
            "meaningful_progress_anchor_reset": meaningful_progress_anchor_reset,
            "control_band": pending.get("control_band"),
            "magnitude": pending.get("magnitude"),
            "command_duration_ms": pending.get("command_duration_ms"),
            "suggested_speed_band": pending.get("suggested_speed_band"),
            "actuator_neutral": self.actuator.is_neutral(),
            "follower_state": event.get("state") if event else None,
        })
        if (
            route_sample is None
            or (
                isinstance(route_local_assessment, Mapping)
                and route_local_assessment.get("safe") is True
            )
        ):
            self._learn_static_obstacle_from_post_command(
                observation,
                pending,
                metrics,
                displacement_m,
            )
        self._last_post_command_observation_sequence = observation.sequence
        if route_sample is not None:
            current_metrics = route_sample.get("metrics") or {}
            corridor_membership_safe = bool(
                isinstance(route_local_assessment, Mapping)
                and route_local_assessment.get("safe") is True
            )
            if corridor_membership_safe:
                self._commit_route_local_sample(route_sample, route_local_assessment)
                self._last_corridor_sample = route_sample
            if (
                not corridor_membership_safe
                or not isinstance(current_metrics, Mapping)
                or float(current_metrics.get("cross_track_m", math.inf)) > self.config.readiness_reacquire_max_cross_track_m
                or float(current_metrics.get("vertical_error_m", math.inf)) > self.config.start_max_vertical_m
            ):
                assessment_reason = (
                    route_local_assessment.get("reason_code")
                    if isinstance(route_local_assessment, Mapping)
                    else "corridor_resolution_unavailable"
                )
                ambiguity_unproven = (
                    isinstance(route_local_assessment, Mapping)
                    and route_local_assessment.get("failure_class") == "ambiguity"
                )
                if ambiguity_unproven:
                    self.timeline.append({
                        "event": "authorized_corridor_ambiguity_unproven",
                        "t": observation.monotonic_t,
                        "sequence": observation.sequence,
                        "reason": assessment_reason,
                        "global_polygon_resolution": polygon_resolution,
                        "corridor_resolution": corridor_resolution.to_dict() if corridor_resolution is not None else None,
                        "route_metrics": dict(current_metrics) if isinstance(current_metrics, Mapping) else None,
                    })
                failure_reason = (
                    "multi_tile_corridor_boundary_ambiguity_unproven"
                    if ambiguity_unproven and self.config.route_mode == "ordinary_multi_tile"
                    else (
                        "multi_polygon_corridor_boundary_ambiguity_unproven"
                        if ambiguity_unproven
                        else (
                            "multi_tile_corridor_departure"
                            if self.config.route_mode == "ordinary_multi_tile"
                            else "multi_polygon_corridor_departure"
                        )
                    )
                )
                divergence = self._classify_corridor_divergence(
                    route_local_assessment,
                    route_sample,
                    pending,
                )
                self.timeline.append({
                    "event": "corridor_divergence_classified",
                    "t": observation.monotonic_t,
                    "sequence": observation.sequence,
                    **divergence,
                    "movement_command_sent": False,
                })
                if self._confirm_static_obstacle_after_divergence(
                    observation,
                    pending,
                    divergence,
                ):
                    return
                self.timeline.append({
                    "event": (
                        "multi_tile_corridor_resolution_failed"
                        if self.config.route_mode == "ordinary_multi_tile"
                        else "multi_polygon_corridor_resolution_failed"
                    ),
                    "t": observation.monotonic_t,
                    "sequence": observation.sequence,
                    "reason": failure_reason,
                    "route_local_reason": assessment_reason,
                    "global_polygon_resolution": polygon_resolution,
                    "corridor_resolution": corridor_resolution.to_dict() if corridor_resolution is not None else None,
                    "route_metrics": dict(current_metrics) if isinstance(current_metrics, Mapping) else None,
                })
                raise AdapterError(failure_reason)

    def _follower_progress_accounting(self, t: float | None = None) -> Mapping[str, Any] | None:
        if self.follower is None:
            return None
        callback = getattr(self.follower, "progress_accounting", None)
        if not callable(callback):
            return None
        try:
            result = callback(t)
        except TypeError:
            result = callback()
        return dict(result) if isinstance(result, Mapping) else None

    def _pause_follower_for_readiness(self, t: float, reason: str = "readiness") -> None:
        accounting = None
        callback = getattr(self.follower, "pause_for_safety", None) if self.follower is not None else None
        if callable(callback):
            result = callback(float(t))
            accounting = dict(result) if isinstance(result, Mapping) else None
        self.timeline.append({
            "event": f"follower_paused_for_{reason}",
            "t": float(t),
            "accounting": accounting or self._follower_progress_accounting(t),
        })

    def _resume_follower_after_readiness(self, t: float, reason: str = "readiness") -> Mapping[str, Any] | None:
        callback = getattr(self.follower, "resume_after_safety_pause", None) if self.follower is not None else None
        result = callback(float(t)) if callable(callback) else None
        accounting = dict(result) if isinstance(result, Mapping) else self._follower_progress_accounting(t)
        self.timeline.append({
            "event": f"follower_resumed_after_{reason}",
            "t": float(t),
            "accounting": accounting,
        })
        return accounting

    @staticmethod
    def _observation_evidence(observation: LiveObservation) -> dict[str, Any]:
        evidence = observation.to_dict()
        if observation.raw:
            evidence["raw"] = copy.deepcopy(dict(observation.raw))
        return evidence

    @staticmethod
    def _is_readiness_only_failure(failures: Sequence[str]) -> bool:
        return bool(failures) and set(failures).issubset(READINESS_FAILURES)

    def _authorized_route_metrics(self, position: Vec3) -> dict[str, float] | None:
        """Measure against the authorized waypoint polyline, not its chord."""

        authorization = self.authorization
        plan = self.route_plan
        if authorization is None or not isinstance(plan, Mapping):
            return None
        proof = self.authorization_proof
        if proof is not None and proof.route_plan_sha256 == _plan_hash(plan):
            waypoints = list(proof.waypoints)
            cumulative = list(proof.cumulative_waypoint_m)
        else:
            raw_waypoints = plan.get("waypoints")
            if not isinstance(raw_waypoints, list):
                return None
            try:
                waypoints = [_parse_point(value) for value in raw_waypoints]
            except (KeyError, TypeError, ValueError):
                return None
            if not waypoints:
                return None
            cumulative = [0.0]
            for first, second in zip(waypoints, waypoints[1:]):
                cumulative.append(cumulative[-1] + _distance_m(first, second))
        if len(waypoints) == 1:
            return {
                "cross_track_m": _horizontal_distance_m(position, waypoints[0]),
                "route_length_m": 0.0,
                "along_route_m": 0.0,
                "remaining_route_distance_m": _distance_m(position, waypoints[0]),
                "vertical_error_m": abs(position.z - waypoints[0].z) / 100.0,
                "segment_index": 0.0,
                "current_waypoint_index": 0.0,
            }
        route_length_m = cumulative[-1]
        active_index = int(getattr(self.follower, "current_waypoint_index", 0) or 0)
        active_index = max(0, min(len(waypoints) - 2, active_index))
        segment_indices = range(
            max(0, active_index - 1),
            min(len(waypoints) - 1, active_index + 2),
        )
        candidates: list[tuple[float, int, float, float, float, float]] = []
        for segment_index in segment_indices:
            first = waypoints[segment_index]
            second = waypoints[segment_index + 1]
            dx = second.x - first.x
            dy = second.y - first.y
            denominator = dx * dx + dy * dy
            if denominator <= 1e-9:
                amount = 0.0
            else:
                amount = (
                    (position.x - first.x) * dx
                    + (position.y - first.y) * dy
                ) / denominator
                amount = max(0.0, min(1.0, amount))
            closest_x = first.x + amount * dx
            closest_y = first.y + amount * dy
            closest_z = first.z + amount * (second.z - first.z)
            cross_track_m = math.hypot(position.x - closest_x, position.y - closest_y) / 100.0
            along_route_m = cumulative[segment_index] + _distance_m(first, second) * amount
            candidates.append((
                cross_track_m,
                abs(segment_index - active_index),
                segment_index,
                along_route_m,
                closest_z,
                amount,
            ))
        if not candidates:
            return None
        cross_track_m, _, segment_index, along_route_m, closest_z, _ = min(
            candidates,
            key=lambda item: (item[0], item[1], item[2]),
        )
        return {
            "cross_track_m": cross_track_m,
            "route_length_m": route_length_m,
            "along_route_m": along_route_m,
            "remaining_route_distance_m": max(0.0, route_length_m - along_route_m),
            "vertical_error_m": abs(position.z - closest_z) / 100.0,
            "segment_index": float(segment_index),
            "current_waypoint_index": float(active_index),
            "nearby_segment_start_m": cumulative[segment_index],
            "nearby_segment_end_m": cumulative[segment_index + 1],
        }

    def _replan_for_readiness_displacement(
        self,
        observation: LiveObservation,
        metrics: Mapping[str, float],
    ) -> bool:
        """Replan from a passively displaced position without recapturing topology."""

        topology = self.topology_session
        authorization = self.authorization
        if topology is None or authorization is None:
            self.timeline.append({
                "event": "readiness_replan_failed",
                "t": self.monotonic(),
                "reason": "topology_or_authorization_missing",
            })
            return False
        try:
            planner_kwargs: dict[str, Any] = {
                "max_snap_distance_m": self.config.start_max_snap_m,
                "max_vertical_separation_m": self.config.start_max_vertical_m,
            }
            if self.config.route_mode == "ordinary_multi_polygon":
                planner_kwargs["max_tile_crossings"] = 0
            elif self.config.route_mode == "ordinary_multi_tile":
                planner_kwargs["max_tile_crossings"] = self.config.multi_tile_max_crossings
            planner_result = self._plan_with_static_overlay(
                topology,
                observation.position,
                authorization.resolved_end,
                **planner_kwargs,
            )
            if isinstance(planner_result, DetourRoutePlan):
                plan = planner_result.to_dict()
            elif isinstance(planner_result, Mapping):
                plan = planner_result
            else:
                raise AdapterError("planner_returned_invalid_route_plan")
            start_resolution = topology.graph.resolve_point(
                observation.position,
                max_snap_distance_m=self.config.start_max_snap_m,
                max_vertical_separation_m=self.config.start_max_vertical_m,
            )
            if not start_resolution.ok:
                raise AdapterError("readiness_replan_start_unresolved")
            self.start_resolution = start_resolution
            self._validate_recovery_route(
                plan,
                topology,
                observation,
                authorization.resolved_end,
            )
            validation = self.validator.validate(plan)
            if (
                validation.get("valid") is not True
                or validation.get("errors") != []
                or validation.get("warnings") != []
            ):
                raise AdapterError("independent_validator_rejected_recovery_route")
            self._locate_route_traversals(plan)
            follower = self.follower_factory(plan)
            new_authorization = self._validate_plan_identity(plan, topology, observation)
            start_resolution = topology.graph.resolve_point(
                observation.position,
                max_snap_distance_m=self.config.start_max_snap_m,
                max_vertical_separation_m=self.config.start_max_vertical_m,
            )
            destination_resolution = topology.graph.resolve_point(
                authorization.resolved_end,
                max_snap_distance_m=self.config.start_max_snap_m,
                max_vertical_separation_m=self.config.start_max_vertical_m,
            )
        except (AdapterError, KeyError, TypeError, ValueError) as exc:
            self.timeline.append({
                "event": "readiness_replan_failed",
                "t": self.monotonic(),
                "reason": str(exc),
                "position": _point_dict(observation.position),
                "route_metrics": dict(metrics),
            })
            return False
        self.route_plan = plan
        self.validator_result = validation
        self.start_resolution = start_resolution
        self.destination_resolution = destination_resolution
        self.follower = follower
        self.authorization = new_authorization
        self.authorization_proof = self._build_authorization_proof(
            plan,
            topology,
            observation,
            validation,
        )
        self._reset_feedback_after_replan(observation)
        self._set_state(AdapterState.PLAN_VALIDATED, "readiness_replan_validated")
        self.timeline.append({
            "event": "readiness_replanned_and_independently_validated",
            "t": self.monotonic(),
            "position": _point_dict(observation.position),
            "route_metrics": dict(metrics),
            "route_plan_sha256": new_authorization.route_plan_sha256,
            "validator": copy.deepcopy(validation),
            "topology_fingerprint": topology.fingerprint,
        })
        return True

    def _replan_around_invisible_blocker(self, observation: LiveObservation, event: Mapping[str, Any]) -> LiveObservation | None:
        """Re-plan around a stall no collision query explains.

        Returns the fresh observation of the new route, or None to let the
        stall end the route as before.
        """
        if self.route_collision_audit is None or self.config.route_mode != "cached_topology":
            return None
        count = self.__dict__.get("_invisible_blockers", 0)
        if count >= MAX_INVISIBLE_BLOCKERS_PER_ROUTE:
            return None
        heading = event.get("desired_heading_deg")
        if not isinstance(heading, (int, float)) or not math.isfinite(float(heading)):
            heading = observation.actor_yaw
        radians = math.radians(float(heading))
        start = observation.position
        center = Vec3(start.x + math.cos(radians) * INVISIBLE_BLOCKER_AHEAD_CM,
                      start.y + math.sin(radians) * INVISIBLE_BLOCKER_AHEAD_CM, start.z)
        from .route_sweep import SWEEP_CLOUD_CLEARANCE_M, SWEEP_UNCERTAINTY_MARGIN_M
        obstacle = StaticObstacle(
            obstacle_id=f"stall-{observation.sequence}", center=center, radius_m=INVISIBLE_BLOCKER_RADIUS_M,
            cloud_clearance_m=SWEEP_CLOUD_CLEARANCE_M, uncertainty_margin_m=SWEEP_UNCERTAINTY_MARGIN_M,
            confidence=0.7, evidence_count=1, first_sequence=observation.sequence,
            last_sequence=observation.sequence, first_t=observation.monotonic_t, last_t=observation.monotonic_t,
            provenance=("stall_without_collision_evidence",),
        )
        scope = getattr(self.static_obstacles, "scope", None)
        try:
            self.static_obstacles.admit_obstacle(scope=scope, obstacle=obstacle,
                                                 reason_code="stall_without_collision_evidence")
        except Exception as exc:
            self.timeline.append({"event": "invisible_blocker_not_admitted", "t": self.monotonic(), "error": str(exc)})
            return None
        self.__dict__["_invisible_blockers"] = count + 1
        if self.blocker_memory is not None:
            self.blocker_memory.remember(obstacle)
        self._static_pending_discovery = {
            "obstacle": obstacle.to_dict(),
            "reason_code": "stall_without_collision_evidence",
            "sequence": observation.sequence,
            "provisional_movement_only": False,
        }
        self.timeline.append({"event": "invisible_blocker_assumed", "t": self.monotonic(),
                              "sequence": observation.sequence, "center": _point_dict(center),
                              "heading_degrees": float(heading), "count": count + 1})
        return self._replan_for_static_obstacle(observation)

    def _readmit_remembered_blockers(self) -> None:
        if self.blocker_memory is None:
            return
        scope = getattr(self.static_obstacles, "scope", None)
        readmitted = []
        for obstacle in self.blocker_memory.active():
            try:
                self.static_obstacles.admit_obstacle(scope=scope, obstacle=obstacle,
                                                     reason_code="remembered_invisible_blocker")
                readmitted.append(obstacle.obstacle_id)
            except Exception:
                continue
        if readmitted:
            self.timeline.append({"event": "remembered_blockers_readmitted", "t": self.monotonic(),
                                  "obstacle_ids": readmitted})

    def _review_stall_guess(self, observation: LiveObservation, discovery: Any) -> str | None:
        """Ask the game's collision what stopped Cloud before believing a guess.

        The stall learner places a padded circle where progress stopped. With
        a collision sweep available, the guess is discarded: a character in
        the way is waited for, and anything fixed is found precisely by the
        re-plan's own audit sweep. A third stall at the same spot keeps the
        guess, so a stall the sweep cannot explain does not loop forever.
        """
        if self.route_collision_audit is None or self.config.route_mode != "cached_topology":
            return
        obstacle = discovery.get("obstacle") if isinstance(discovery, Mapping) else None
        if not isinstance(obstacle, Mapping) or str(obstacle.get("obstacle_id") or "").startswith(("sweep-", "stall-")):
            return
        center = obstacle.get("center") or {}
        try:
            cx, cy = float(center["x"]), float(center["y"])
        except (KeyError, TypeError, ValueError):
            return
        spots = self.__dict__.setdefault("_stall_spots", [])
        repeats = sum(1 for x, y in spots if math.hypot(x - cx, y - cy) <= STALL_SPOT_RADIUS_CM)
        spots.append((cx, cy))
        record: dict[str, Any] = {"event": "stall_reviewed", "t": self.monotonic(),
                                  "obstacle_id": obstacle.get("obstacle_id"), "repeats": repeats}
        if repeats >= STALL_GUESSES_DISCARDED_PER_SPOT:
            record["decision"] = "kept_guess_repeated_stall"
            self.timeline.append(record)
            return
        start = observation.position
        dx, dy = cx - start.x, cy - start.y
        length = math.hypot(dx, dy) or 1.0
        reach = max(length, STALL_SWEEP_AHEAD_CM)
        ahead = {"x": start.x + dx / length * reach, "y": start.y + dy / length * reach, "z": start.z}
        try:
            sweep = self.route_collision_audit([{"x": start.x, "y": start.y, "z": start.z}, ahead])
            hits = [{"segment": segment.get("index"), **hit}
                    for segment in sweep.get("segments") or [] for hit in segment.get("hits") or []]
            kinds = [hit.get("kind") for hit in hits]
        except Exception as exc:
            record.update(decision="kept_guess_sweep_failed", error=str(exc))
            self.timeline.append(record)
            return
        blocking = [hit for hit in hits
                    if hit.get("kind") in {"party", "npc", "door", "vehicle", "fixed", "ledge", "drop"}]
        blockers = [hit.get("kind") for hit in blocking]
        record["hits_ahead"] = kinds
        self.static_obstacles.discard_obstacle(str(obstacle.get("obstacle_id")))
        if blockers and blockers[0] == "door":
            record["decision"] = "door_ahead"
        elif blockers and blockers[0] == "npc" and repeats >= 1 and blocking[0].get("impact"):
            # Stalled on the same person again: they are not moving (live at
            # Costa del Sol: a bar patron), so route round them after all.
            from .route_sweep import obstacle_for_hit
            person = obstacle_for_hit(blocking[0], sequence=observation.sequence, t=self.monotonic())
            self.static_obstacles.admit_obstacle(scope=self.static_obstacles.scope, obstacle=person,
                                                 reason_code="route_sweep_contact")
            record.update(decision="routed_round_character", actor=blocking[0].get("actor"),
                          obstacle_id=person.obstacle_id)
        elif blockers and blockers[0] in {"party", "npc"}:
            record["decision"] = "discarded_guess_waited_for_character"
            self.sleeper(STALL_CHARACTER_WAIT_SECONDS)
        else:
            record["decision"] = "discarded_guess_for_audit"
        self.timeline.append(record)
        return record["decision"]

    def _replan_for_static_obstacle(
        self,
        observation: LiveObservation,
    ) -> LiveObservation | None:
        """Replan from fresh telemetry after learning a static blocker."""
        topology = self.topology_session
        authorization = self.authorization
        if topology is None or authorization is None:
            self._set_state(AdapterState.REPLAN_REQUIRED, "static_obstacle_authorization_missing")
            return None
        destination = authorization.resolved_end
        discovery = copy.deepcopy(self._static_pending_discovery)
        old_digest = (
            discovery.get("obstacle_digest_before")
            if isinstance(discovery, Mapping)
            else None
        ) or self._static_obstacle_digest()
        new_digest = (
            discovery.get("obstacle_digest_after")
            if isinstance(discovery, Mapping)
            else None
        ) or self._static_obstacle_digest()
        self.timeline.append({
            "event": ("movement_paused_for_stall_verification" if discovery and discovery.get("provisional_movement_only")
                      else "movement_authorization_invalidated_for_static_obstacle"),
            "t": self.monotonic(),
            "previous_route_plan_sha256": authorization.route_plan_sha256,
            "obstacle_digest_before": old_digest,
            "obstacle_digest_after": new_digest,
            "reason": "confirmed_static_obstacle",
            "movement_command_sent": False,
            "actuator_neutral": self.actuator.is_neutral(),
        })
        self._neutralize("static_obstacle_discovered_replan")
        if self._review_stall_guess(observation, discovery) == "door_ahead":
            # The route runs through a closed door: the caller opens it at the
            # prompt and routes on from inside.
            self._set_state(AdapterState.STOPPED, "door_ahead")
            return None
        try:
            fresh = self._require_observation(
                wait_for_fresh=True,
                after_sequence=observation.sequence,
                require_effective_fresh=True,
            )
            # Obstacle learning revokes the old movement authority. Settle
            # advancing measured feet and rebuild any invalidated readiness
            # proof within the existing preflight budget before replacing it.
            fresh = self._settle_preflight_pose(observation, fresh, require_lease=True)
            failures = self._check_preconditions(fresh)
            if failures:
                raise AdapterError("static_obstacle_replan_precondition_failed:" + ";".join(failures))
            if (
                fresh.session_id != authorization.session_id
                or fresh.world_id != authorization.world_id
                or fresh.map_id != authorization.map_id
            ):
                raise AdapterError("static_obstacle_replan_identity_changed")
            if discovery and discovery.get("provisional_movement_only"):
                pending_kwargs = self._static_pending_learning_kwargs
                if pending_kwargs is None:
                    raise AdapterError("provisional_obstacle_evidence_missing")
                metrics = self._authorized_route_metrics(fresh.position) or {}
                confirmed = self.static_obstacles.learn_from_movement(**{
                    **pending_kwargs, "position_after": fresh.position,
                    "along_route_after_m": metrics.get("along_route_m"),
                    "sequence": fresh.sequence, "sampled_t": fresh.monotonic_t,
                    "dynamic_evidence": self._dynamic_static_learning_evidence(fresh,
                        pending_kwargs.get("commanded_bearing_degrees")),
                })
                self.timeline.append({"event": "static_obstacle_settled_confirmation",
                    "t": fresh.monotonic_t, "sequence": fresh.sequence,
                    "reason": confirmed.reason_code, "state": confirmed.state.value,
                    "displacement_m": _horizontal_distance_m(pending_kwargs["position_before"], fresh.position),
                    "movement_command_sent": False})
                if confirmed.state != ObstacleEvidenceState.READY and confirmed.reason_code != "movement_progress_observed":
                    raise AdapterError("static_obstacle_settled_confirmation_unproven")
                self._static_pending_learning_kwargs = None
            start_resolution = topology.graph.resolve_point(
                fresh.position,
                max_snap_distance_m=self.config.start_max_snap_m,
                max_vertical_separation_m=self.config.start_max_vertical_m,
            )
            if not start_resolution.ok or start_resolution.reason_code != "point_inside_polygon":
                raise AdapterError("static_obstacle_replan_start_unresolved")
            self.start_resolution = start_resolution
            route_planner_kwargs: dict[str, Any] = {
                "max_expansions": self.config.cached_topology_max_expansions,
                "max_waypoints": self.config.cached_topology_max_waypoints,
                "max_snap_distance_m": self.config.start_max_snap_m,
                "max_vertical_separation_m": self.config.start_max_vertical_m,
            }
            if self.config.route_mode == "ordinary_multi_polygon":
                route_planner_kwargs["max_tile_crossings"] = 0
            elif self.config.route_mode == "ordinary_multi_tile":
                route_planner_kwargs["max_tile_crossings"] = self.config.multi_tile_max_crossings
            # The authorized destination, unless the collision audit moves the
            # target clear of an obstacle (then it plans to the moved target).
            # Using the unsnapped route target instead failed the recovery
            # check live (cached_topology_recovery_destination_changed).
            target_before_audit = self.route_target

            def recovery_end() -> Vec3:
                if self.route_target is not None and self.route_target is not target_before_audit:
                    return self.route_target
                return destination

            def recovery_plan() -> DetourRoutePlan:
                result = self._plan_with_static_overlay(topology, fresh.position, recovery_end(),
                                                        **route_planner_kwargs)
                if result.status != "complete":
                    raise AdapterError(f"static_obstacle_replan_{result.reason_code}")
                return result

            planner_result = recovery_plan()
            if self.config.route_mode == "cached_topology":
                # Check the recovery route against the game's collision from
                # where Cloud actually is, as the first route was.
                planner_result = self._audit_route_collisions(fresh, planner_result, recovery_plan)
                if self.route_target is not target_before_audit and planner_result.resolved_end is not None:
                    # The audit moved the target on purpose (logged as
                    # route_target_moved_clear_of_obstacle); the recovery
                    # check then holds the route to that new end.
                    destination = planner_result.resolved_end
            plan = planner_result.to_dict()
            self._validate_recovery_route(plan, topology, fresh, destination)
            self._bind_validator_to_topology(topology)
            validation = self.validator.validate(plan)
            if validation.get("valid") is not True or validation.get("errors") != [] or validation.get("warnings") != []:
                raise AdapterError("independent_validator_rejected_static_obstacle_recovery_route")
            self._locate_route_traversals(plan)
            follower = self.follower_factory(plan)
            new_authorization = self._validate_plan_identity(plan, topology, fresh)
            destination_resolution = topology.graph.resolve_point(
                destination,
                max_snap_distance_m=self.config.start_max_snap_m,
                max_vertical_separation_m=self.config.start_max_vertical_m,
            )
            proof = self._build_authorization_proof(plan, topology, fresh, validation)
        except (AdapterError, KeyError, TypeError, ValueError) as exc:
            self.timeline.append({
                "event": "static_obstacle_replan_failed",
                "t": self.monotonic(),
                "reason": str(exc),
                "obstacle_discovery": discovery,
            })
            self._neutralize("static_obstacle_replan_failed")
            self._set_state(AdapterState.REPLAN_REQUIRED, "static_obstacle_replan_failed")
            return None
        self.route_plan = plan
        self.validator_result = validation
        self.destination_resolution = destination_resolution
        self.follower = follower
        self.authorization = new_authorization
        self.authorization_proof = proof
        self._reset_feedback_after_replan(fresh)
        self._set_state(AdapterState.PLAN_VALIDATED, "static_obstacle_replan_validated")
        self.timeline.append({
            "event": "static_obstacle_replanned_and_reauthorised",
            "t": fresh.monotonic_t,
            "sequence": fresh.sequence,
            "obstacle_discovery": discovery,
            "route_plan_sha256": new_authorization.route_plan_sha256,
            "previous_route_plan_sha256": authorization.route_plan_sha256,
            "validator": copy.deepcopy(validation),
            "static_obstacle_evidence_sha256": proof.static_obstacle_evidence_sha256,
            "obstacle_digest_before": old_digest,
            "obstacle_digest_after": self._static_obstacle_digest(),
            "movement_command_sent": False,
            "zero_movement_between_invalidation_and_reauthorization": True,
        })
        return fresh

    def _reset_feedback_after_replan(self, observation: LiveObservation) -> None:
        """Discard feedback and progress owned by the replaced corridor."""
        self._route_traversals = self._locate_route_traversals(self.route_plan)
        self._selected_route = None
        self._selected_route_plan_sha256 = None
        self._adapter_route_plan_sha256 = _plan_hash(self.route_plan)
        self._independent_validator_route_plan_sha256 = _plan_hash(self.route_plan)
        self._pending_command_evidence = None
        self._last_command_sequence = observation.sequence
        self._last_post_command_observation_sequence = observation.sequence
        self._static_no_progress_streak = 0
        self._static_last_evidence_command = None
        self._static_pending_discovery = None
        self._static_pending_learning_kwargs = None
        self._static_last_learning = None
        self._route_local_corridor_index = 0
        self._route_local_corridor_ref = self.authorization.start_ref
        self._route_local_last_position = observation.position
        self._route_local_last_sequence = observation.sequence
        self._last_route_along_m = 0.0
        self._last_corridor_sample = self._route_polygon_sample(observation.position, sequence=observation.sequence)
        self._lookahead_reset_for_plan()

    def _lookahead_reset_for_plan(self) -> None:
        self._static_precommand_lookahead_checked = False
        self._static_lookahead_last_position = None
        self._static_lookahead_last_t = None
        self._static_lookahead_last_heading = None
        self._static_lookahead_last_polygon_ref = None
        if self.lookahead is not None and self.authorization is not None:
            self.lookahead.reset_plan(self.authorization.route_plan_sha256)

    def _recover_from_readiness_loss(
        self,
        *,
        trigger: LiveObservation | None,
        failures: Sequence[str],
        trigger_error: str | None = None,
        trigger_event_name: str = "readiness_loss_triggered",
    ) -> tuple[LiveObservation, list[dict[str, Any]], dict[str, Any]] | None:
        """Pause neutral, then reacquire or replan using the cached topology."""

        trigger_sequence = trigger.sequence if trigger is not None else self._last_command_sequence
        airborne_recovery = trigger is not None and trigger.grounded is False
        landing_sample: LiveObservation | None = None
        deadline = self.monotonic() + self.config.readiness_recovery_timeout_seconds
        after_sequence = trigger_sequence
        settled_wait_attempted = False
        settled_wait_attempts = 0
        settled_wait_succeeded = False
        post_settle_observation_attempted = False
        post_settle_observation_failure: str | None = None
        self._set_state(AdapterState.PAUSED_FOR_READINESS, "readiness_loss")
        pause_t = (
            trigger.monotonic_t
            if trigger is not None
            else self._last_command_completed_t
            if self._last_command_completed_t is not None
            else self.monotonic()
        )
        self._pause_follower_for_readiness(pause_t)
        trigger_event: dict[str, Any] = {
            "event": trigger_event_name,
            "t": self.monotonic(),
            "route_state": AdapterState.FOLLOWING.value,
            "failures": list(failures),
            "trigger_error": trigger_error,
            "trigger_sequence": trigger_sequence,
        }
        if trigger is not None:
            trigger_event["observation"] = self._observation_evidence(trigger)
        elif self._last_observation is not None:
            trigger_event["observation"] = self._observation_evidence(self._last_observation)
        self.timeline.append(trigger_event)
        settle = getattr(self.telemetry, "wait_for_settled_gameplay", None)

        def wait_for_settled_gameplay(sequence: int | None) -> None:
            """Use a bounded settle slice and preserve a fresh-sample slice."""

            nonlocal after_sequence, settled_wait_attempted, settled_wait_succeeded, settled_wait_attempts
            settled_wait_attempted = True
            settled_wait_attempts += 1
            # A successful settle call is not itself a telemetry observation.
            # Keep one polling interval available for the required advancing
            # post-settle sample.  If a source nevertheless consumes the whole
            # deadline, the caller makes one immediate, non-waiting observation
            # attempt rather than falsely treating the successful settle as a
            # generic timeout.
            post_settle_reserve = min(
                self.config.telemetry_poll_seconds,
                self.config.readiness_recovery_timeout_seconds,
            )
            settle_timeout = min(
                self.config.settled_gameplay_timeout_seconds,
                max(0.0, deadline - self.monotonic() - post_settle_reserve),
            )
            try:
                settled_result = settle(
                    settle_timeout,
                    max_telemetry_age_seconds=self.config.max_telemetry_age_seconds,
                )
            except Exception as exc:
                self.timeline.append({
                    "event": "readiness_recovery_settled_wait_failed",
                    "t": self.monotonic(),
                    "sequence": sequence,
                    "timeout_seconds": settle_timeout,
                    "error": str(exc),
                })
                return
            settled_ok = isinstance(settled_result, Mapping) and settled_result.get("ok") is True
            if settled_ok:
                settled_wait_succeeded = True
                result_sequence = settled_result.get("sequence")
                try:
                    if result_sequence is not None:
                        after_sequence = max(after_sequence or int(result_sequence), int(result_sequence))
                except (TypeError, ValueError):
                    pass
            self.timeline.append({
                "event": "readiness_recovery_settled_wait_completed",
                "t": self.monotonic(),
                "sequence": sequence,
                "timeout_seconds": settle_timeout,
                "successful": settled_ok,
                "result": settled_result,
            })

        if callable(settle):
            wait_for_settled_gameplay(trigger.sequence if trigger is not None else after_sequence)
        while self.monotonic() < deadline or (settled_wait_succeeded and not post_settle_observation_attempted):
            remaining = max(0.0, deadline - self.monotonic())
            if settled_wait_succeeded:
                post_settle_observation_attempted = True
            try:
                observation = self._require_observation(
                    wait_for_fresh=True,
                    after_sequence=after_sequence,
                    timeout_seconds=remaining,
                )
            except AdapterError as exc:
                self.timeline.append({
                    "event": "readiness_recovery_observation_failed",
                    "t": self.monotonic(),
                    "after_sequence": after_sequence,
                    "error": str(exc),
                })
                if settled_wait_succeeded and post_settle_observation_attempted:
                    post_settle_observation_failure = str(exc)
                if str(exc) in {
                    "actor_identity_changed",
                    "session_changed",
                    "world_changed",
                    "telemetry_identity_incomplete",
                    "camera_yaw_telemetry_missing",
                    "telemetry_non_finite",
                }:
                    self._neutralize("readiness_recovery_identity_failure")
                    self._set_state(AdapterState.STOPPED, str(exc))
                    return None
                break
            after_sequence = observation.sequence
            current_failures = self._check_preconditions(observation)
            if observation.telemetry_gameplay_ready is not True:
                current_failures.append("telemetry_gameplay_not_ready")
            self.timeline.append({
                "event": "readiness_recovery_sample",
                "t": observation.monotonic_t,
                "observation": self._observation_evidence(observation),
                "failures": list(current_failures),
            })
            if current_failures:
                landing_sample = None
                if not self._is_readiness_only_failure(current_failures):
                    self._neutralize("readiness_recovery_identity_or_lease_failure")
                    self._set_state(AdapterState.STOPPED, ";".join(current_failures))
                    return None
                settle = getattr(self.telemetry, "wait_for_settled_gameplay", None)
                # A successful proof can be invalidated again by a later
                # stale observation. Fresh samples alone do not rebuild that
                # proof. Retry its actual multi-sample gate within the same
                # deadline, reserving the service's minimum one-second slice.
                can_retry_settle = (settled_wait_attempts < 3 and
                    deadline - self.monotonic() >= 1.0 + self.config.telemetry_poll_seconds)
                if callable(settle) and (not settled_wait_attempted or can_retry_settle):
                    wait_for_settled_gameplay(observation.sequence)
                if self.monotonic() < deadline:
                    self.sleeper(min(self.config.telemetry_poll_seconds, deadline - self.monotonic()))
                continue
            if airborne_recovery:
                stable_landing = (observation.grounded is True and landing_sample is not None
                    and _distance_m(observation.position, landing_sample.position) <= 0.15)
                self.timeline.append({"event": "airborne_landing_sample", "sequence": observation.sequence,
                    "grounded": observation.grounded, "stable": stable_landing,
                    "position": _point_dict(observation.position), "movement_command_sent": False})
                landing_sample = observation if observation.grounded is True else None
                if not stable_landing:
                    if self.monotonic() < deadline:
                        self.sleeper(min(self.config.telemetry_poll_seconds, deadline - self.monotonic()))
                    continue
            if not self._same_authorization(observation):
                self._neutralize("readiness_recovery_plan_identity_failure")
                self._set_state(AdapterState.REPLAN_REQUIRED, "plan_identity_changed")
                return None
            topology = self.topology_session
            authorization = self.authorization
            if topology is None or authorization is None:
                self._neutralize("readiness_recovery_authorization_missing")
                self._set_state(AdapterState.STOPPED, "topology_or_route_authorization_missing")
                return None
            final_distance_m = _distance_m(observation.position, authorization.resolved_end)
            if self._within_final_arrival_tolerance(observation.position, authorization.resolved_end):
                self._resume_follower_after_readiness(observation.monotonic_t)
                events, follower_event = self._follower_event(observation)
                arrival_event = dict(follower_event)
                arrival_event.update({
                    "event": "destination_reached_after_readiness",
                    "state": "ARRIVED",
                    "distance_to_final_destination_m": final_distance_m,
                })
                self._set_state(AdapterState.ARRIVED, "readiness_reacquired_inside_final_arrival_radius")
                self.timeline.append({
                    "event": "readiness_reacquired_inside_final_arrival_radius",
                    "t": observation.monotonic_t,
                    "observation": self._observation_evidence(observation),
                    "distance_to_final_destination_m": final_distance_m,
                    "topology_fingerprint": topology.fingerprint,
                    "route_plan_sha256": authorization.route_plan_sha256,
                })
                return observation, events, arrival_event
            resolution = topology.graph.resolve_point(
                observation.position,
                max_snap_distance_m=self.config.start_max_snap_m,
                max_vertical_separation_m=self.config.start_max_vertical_m,
            )
            metrics = self._authorized_route_metrics(observation.position)
            if self.config.route_mode in CORRIDOR_ROUTE_MODES:
                corridor_refs = tuple(
                    int(value)
                    for value in (self.route_plan or {}).get("polygon_corridor", [])
                    if isinstance(value, int)
                )
                corridor_resolution = topology.graph.resolve_point_in_corridor(
                    observation.position,
                    corridor_refs,
                    max_snap_distance_m=self.config.start_max_snap_m,
                    max_vertical_separation_m=self.config.start_max_vertical_m,
                    global_resolution=resolution,
                )
                route_sample = {
                    "position": _point_dict(observation.position),
                    "global_resolution": resolution,
                    "corridor_resolution": corridor_resolution,
                    "metrics": dict(metrics) if isinstance(metrics, Mapping) else None,
                    "sequence": observation.sequence,
                    "t": observation.monotonic_t,
                }
                route_local_assessment = self._route_local_sample_assessment(
                    self._last_corridor_sample,
                    route_sample,
                )
                prior_along = self._last_route_along_m
                route_safe = (
                    route_local_assessment.get("safe") is True
                    and metrics is not None
                    and metrics["cross_track_m"] <= self.config.readiness_reacquire_max_cross_track_m
                    and metrics["along_route_m"] >= -self.config.readiness_reacquire_max_cross_track_m
                    and metrics["along_route_m"] <= metrics["route_length_m"] + self.config.readiness_reacquire_max_cross_track_m
                    and metrics["vertical_error_m"] <= self.config.start_max_vertical_m
                    and (
                        prior_along is None
                        or (
                            metrics["along_route_m"] >= prior_along - self.config.readiness_reacquire_max_cross_track_m
                            and metrics["along_route_m"] <= prior_along + 2.0
                        )
                    )
                )
                if route_safe:
                    transition = route_local_assessment.get("transition")
                    if transition is not None:
                        self.timeline.append(transition)
                    self._commit_route_local_sample(route_sample, route_local_assessment)
                    self._last_corridor_sample = route_sample
                    self._last_route_along_m = float(metrics["along_route_m"])
            else:
                route_local_assessment = None
                route_safe = (
                    resolution.ok
                    and resolution.selected_reference == authorization.start_ref
                    and metrics is not None
                    and metrics["cross_track_m"] <= self.config.readiness_reacquire_max_cross_track_m
                    and metrics["along_route_m"] >= -self.config.readiness_reacquire_max_cross_track_m
                    and metrics["along_route_m"] <= metrics["route_length_m"] + self.config.readiness_reacquire_max_cross_track_m
                    and metrics["vertical_error_m"] <= self.config.start_max_vertical_m
                )
            if not route_safe:
                if self.config.route_mode in {"ordinary_multi_polygon", "ordinary_multi_tile"}:
                    recovery_reason = (
                        "multi_tile_replan_required"
                        if self.config.route_mode == "ordinary_multi_tile"
                        else "multi_polygon_replan_required"
                    )
                    self._neutralize(recovery_reason)
                    self._set_state(AdapterState.REPLAN_REQUIRED, recovery_reason)
                    self.timeline.append({
                        "event": (
                            "readiness_recovery_multi_tile_replan_required"
                            if self.config.route_mode == "ordinary_multi_tile"
                            else "readiness_recovery_multi_polygon_replan_required"
                        ),
                        "t": observation.monotonic_t,
                        "observation": self._observation_evidence(observation),
                        "resolved_polygon_ref": resolution.selected_reference,
                        "authorized_corridor_refs": sorted(corridor_refs),
                        "corridor_resolution": corridor_resolution.to_dict(),
                        "route_local_assessment": dict(route_local_assessment),
                        "route_metrics": dict(metrics) if metrics is not None else None,
                        "previous_route_along_m": self._last_route_along_m,
                    })
                    return None
                if metrics is None:
                    self._neutralize("readiness_recovery_route_unresolvable")
                    self._set_state(AdapterState.REPLAN_REQUIRED, "readiness_recovery_replan_required")
                    return None
                if not self._replan_for_readiness_displacement(observation, metrics):
                    self._neutralize("readiness_recovery_replan_failed")
                    self._set_state(AdapterState.REPLAN_REQUIRED, "readiness_recovery_replan_required")
                    return None
            self._resume_follower_after_readiness(observation.monotonic_t)
            events, event = self._follower_event(observation)
            state = str(event.get("state") or "")
            if state in {"DEVIATED", "REPLAN_REQUIRED", "FAILED", "STALLED"}:
                self._neutralize("readiness_recovery_follower_unsafe")
                # A stall read straight after a readiness pause is the pause
                # itself (live: brief not-controllable flickers near Kalm's
                # NPCs ended a route to Ned); plan afresh rather than stop.
                self._set_state(
                    AdapterState.REPLAN_REQUIRED if state in {"DEVIATED", "REPLAN_REQUIRED", "STALLED"}
                    else AdapterState.STOPPED,
                    f"readiness_recovery_follower_{state.lower()}",
                )
                return None
            self._set_state(AdapterState.FOLLOWING, "readiness_recovered")
            self.timeline.append({
                "event": "readiness_reacquired_route_state",
                "t": observation.monotonic_t,
                "observation": self._observation_evidence(observation),
                "route_metrics": dict(metrics) if metrics is not None else None,
                "follower_state": state,
                "topology_fingerprint": self.topology_session.fingerprint if self.topology_session else None,
                "route_plan_sha256": self.authorization.route_plan_sha256 if self.authorization else None,
            })
            return observation, events, event
        failure_reason = (
            "readiness_recovery_post_settle_observation_unavailable"
            if settled_wait_succeeded and post_settle_observation_failure is not None
            else "readiness_recovery_timeout"
        )
        self._neutralize(failure_reason)
        self._set_state(AdapterState.STOPPED, failure_reason)
        self.timeline.append({
            "event": "readiness_recovery_failed",
            "t": self.monotonic(),
            "reason": failure_reason,
            "last_sequence": after_sequence,
            "post_settle_observation_failure": post_settle_observation_failure,
        })
        return None

    def _supervised_deadline(self, observation: LiveObservation, steering: Mapping[str, Any], event: Mapping[str, Any]):
        """Bound held travel by sample freshness, a turn, and proven corridor geometry."""
        if self.config.controller_mode != "continuous":
            return None
        speed_bound = SPRINT_SPEED_BOUND_M_S if self.config.sprint else NON_SPRINT_SPEED_BOUND_M_S
        if self.config.sprint and (observation.velocity_cm_s is None or not all(
                math.isfinite(v) for v in (observation.velocity_cm_s.x, observation.velocity_cm_s.y, observation.velocity_cm_s.z))):
            raise AdapterError("sprint_velocity_unavailable")
        if observation.velocity_cm_s is not None and math.hypot(
                observation.velocity_cm_s.x, observation.velocity_cm_s.y) / 100.0 > speed_bound:
            raise AdapterError("continuous_speed_envelope_exceeded")
        if steering["control_band"] == "final":
            return None
        if not callable(getattr(self.actuator, "supervised_command", None)):
            raise AdapterError("supervised_actuator_unavailable")
        started = self.monotonic()
        age = self._effective_telemetry_age_seconds(observation)
        distance = float(event.get("distance_m") or 0.0)
        braking_margin = SPRINT_BRAKING_MARGIN_M if self.config.sprint else 0.75
        sprint_candidate = self._sprint_eligible(observation, steering, event)
        if self.config.sprint:
            # Sprint keeps the measured turn-distance/braking envelope. Ordinary
            # continuous steering can use the stronger exact corridor proof below
            # instead of treating every nearby waypoint as a mandatory pulse.
            # The turn is where the route bends, not the next waypoint: live at
            # Costa del Sol waypoints ~5 m apart left no usable lease, and 117
            # of 189 commands fell back to pulses, stopping Cloud between them.
            if self.authorization_proof is not None:
                distance = max(distance, straight_run_ahead_m(self.authorization_proof.waypoints,
                                                              observation.position))
            duration = movement_lease_seconds(effective_age=age,
                freshness_limit=self.config.max_telemetry_age_seconds,
                distance_to_turn_m=distance, speed_bound_m_s=speed_bound,
                braking_margin_m=braking_margin)
            if sprint_candidate:
                duration = min(duration, 0.3)
        else:
            if not math.isfinite(age) or age < 0.0:
                return None
            budget = self.config.max_telemetry_age_seconds
            if self.config.continuous_fast_follow:
                # A fast-follow hold must match the 0.3 s pulse, which the 0.5 s
                # freshness cap allowed only below 0.175 s of sample age: live
                # on the Chadley route with OBS recording (median age 0.18 s)
                # 247 of 309 commands fell back to stop-go pulses. The ray proof
                # below covers speed x (age + hold), so the hold may reach
                # further while samples past the cap are still refused.
                budget = max(budget, FAST_FOLLOW_HOLD_AGE_BUDGET_SECONDS)
            duration = max(0.0, min(0.4, budget - age - 0.025))
        if duration < 0.04 or self.topology_session is None or self.authorization_proof is None:
            return None
        graph = self.topology_session.graph
        corridor = self._route_corridor_refs()
        index = max(0, (self._route_local_corridor_index or 0) - 1)
        refs = corridor[index:index + 16]
        portals = []
        for source, target in zip(refs, refs[1:]):
            serialized = self._portal_for_transition(self.route_plan, source, target)
            canonical = next((p for p in graph.adjacency.get(source, ())
                              if p.to_reference == target and p.to_dict() == serialized), None)
            if canonical is None:
                return None
            portals.append(canonical)
        obstacles = self.static_obstacles.planning_snapshot(now_t=started).obstacles
        heading = math.radians(float(steering["desired_world_heading_deg"]))
        headings = [heading]
        minimum_hold = 0.04
        if self.config.continuous_fast_follow:
            # A hold must be at least as long as the pulse it replaces. Live
            # (Kalm, ned19) 73 of 106 holds were 0.08-0.2 s: 45 expired
            # before renewal, giving a 53% stick duty cycle against 81% for
            # the proven 300 ms pulse, and holds that short from rest barely
            # moved Cloud. When only a shorter hold is proven, the band's
            # ordinary pulse is sent instead, exactly as the default does.
            try:
                pulse_ms = float(steering.get("command_duration_ms") or 0.0)
            except (TypeError, ValueError):
                pulse_ms = 0.0
            if not math.isfinite(pulse_ms) or pulse_ms <= 0.0:
                pulse_ms = self.config.control_period_seconds * 1000.0
            minimum_hold = max(minimum_hold, pulse_ms / 1000.0)
            if duration < minimum_hold:
                return None
            # Full-stick holds carry more momentum into a turn. The measured
            # velocity ray must satisfy the same horizon/corridor/obstacle
            # proof as the commanded heading; without a measured velocity the
            # existing pulse path is used instead of a hold.
            velocity = observation.velocity_cm_s
            if velocity is None or not all(math.isfinite(float(v)) for v in (velocity.x, velocity.y, velocity.z)):
                return None
            if math.hypot(velocity.x, velocity.y) >= FAST_FOLLOW_MOMENTUM_MIN_SPEED_CM_S:
                headings.append(math.atan2(velocity.y, velocity.x))

        def ray_proven(ray_heading: float, horizon: float) -> bool:
            target = Vec3(observation.position.x + math.cos(ray_heading)*horizon*100,
                          observation.position.y + math.sin(ray_heading)*horizon*100, observation.position.z)
            if any(obstacle_segment_clearance_m(observation.position, target, item) <= 0 for item in obstacles):
                return False
            return _segment_authorized_corridor(observation.position, target, graph=graph,
                corridor_refs=refs, portals=portals, minimum_edge_clearance_m=0.35,
                max_vertical_separation_m=self.config.start_max_vertical_m)

        for factor in (1.0, 0.5, 0.25):
            hold = duration * factor
            if hold < minimum_hold:
                continue
            horizon = speed_bound * (age + hold) + braking_margin
            if all(ray_proven(ray_heading, horizon) for ray_heading in headings):
                result = {"deadline": started + hold, "lease_seconds": hold,
                    "horizon_m": horizon, "speed_bound_m_s": speed_bound,
                    "effective_sample_age_seconds": age, "corridor_proven": True,
                    "braking_margin_m": braking_margin,
                    "sprint": sprint_candidate and hold >= 0.1}
                if self.config.continuous_fast_follow:
                    result["momentum_ray_proven"] = len(headings) > 1
                    result["minimum_hold_seconds"] = minimum_hold
                return result
        return None

    def _fast_follow_waypoint_carry(self, observation: LiveObservation, steering: Mapping[str, Any], event: Mapping[str, Any]):
        """Hold normal travel through an intermediate waypoint's slow band.

        Cached-topology plans commonly place a waypoint every 1-2 m, so the
        follower's 2 m distance slow band otherwise selects the 0.5 approach
        magnitude for much of a route. Only that single reason is carried, and
        only when the normal-magnitude hold passes the exact supervised
        corridor, momentum and obstacle proof; otherwise the approach band
        stands unchanged. Final-waypoint, heading-slow, adapter-final,
        braking and final bands are never carried.
        """
        # Sprint travel carries too: live at Costa del Sol 57 of 189 sprint-trial
        # commands slowed to the 0.5 approach stick at intermediate waypoints.
        if (not (self.config.continuous_fast_follow or self.config.sprint)
                or self.config.controller_mode != "continuous"):
            return None
        if (steering.get("control_band") != "approach"
                or list(steering.get("control_selection_reasons") or ()) != ["follower_distance_slow"]):
            return None
        waypoints = (self.route_plan or {}).get("waypoints") or ()
        index = event.get("waypoint_index")
        if type(index) is not int or not 0 <= index < len(waypoints) - 1:
            return None
        final_distance = event.get("distance_to_final_destination_m")
        if (not isinstance(final_distance, (int, float)) or not math.isfinite(final_distance)
                or final_distance <= float(steering["arrival_stopping_horizon_m"])):
            return None
        heading_error = event.get("heading_error_deg")
        if (not isinstance(heading_error, (int, float)) or not math.isfinite(heading_error)
                or abs(heading_error) > FAST_FOLLOW_MAX_HEADING_ERROR_DEG):
            return None
        x, y = world_heading_to_camera_stick(
            float(steering["desired_world_heading_deg"]), observation.camera_yaw, self.config.normal_stick_magnitude,
        )
        carried = {
            **steering,
            "magnitude": math.hypot(x, y),
            "control_band": "normal",
            "command_duration_ms": self._bounded_command_duration_ms(self.config.control_period_seconds),
            "control_selection_reasons": ["follower_distance_slow", "fast_follow_waypoint_carry"],
        }
        supervision = self._supervised_deadline(observation, carried, event)
        if supervision is None:
            return None
        return x, y, carried, supervision

    def _sprint_eligible(self, observation, steering, event):
        heading_error = event.get("heading_error_deg")
        if not (self.config.sprint and steering["control_band"] == "normal"
                and observation.grounded is True and observation.velocity_cm_s is not None
                and isinstance(heading_error, (int, float)) and math.isfinite(heading_error)
                and abs(heading_error) <= 15.0
                and float(event.get("distance_to_final_destination_m") or 0.0) >= 8.0):
            return False
        # The follower's target point is often under 6 m away on a town route
        # (live at Costa del Sol: 18 waypoints over 88 m, no sprint at all),
        # so also accept a straight run of route ahead across waypoints.
        if float(event.get("distance_m") or 0.0) >= SPRINT_MIN_STRAIGHT_RUN_M:
            return True
        waypoints = self.authorization_proof.waypoints if self.authorization_proof is not None else ()
        return straight_run_ahead_m(waypoints, observation.position) >= SPRINT_MIN_STRAIGHT_RUN_M

    def _sprint_pulse_handoff_evidence(self, observation: LiveObservation) -> dict[str, Any]:
        """Prove a bounded low-speed residual-motion envelope before pulses.

        This is deliberately evaluated only after the sprint input has already
        been neutralized and on a later advancing telemetry sample. It does
        not reinterpret the pre-neutral observation as braking evidence.
        """
        velocity = observation.velocity_cm_s
        if observation.grounded is not True:
            return {"proven": False, "reason": "not_grounded"}
        if velocity is None or not all(math.isfinite(float(value)) for value in
                (velocity.x, velocity.y, velocity.z)):
            return {"proven": False, "reason": "velocity_unavailable"}
        horizontal_cm_s = math.hypot(velocity.x, velocity.y)
        speed_m_s = horizontal_cm_s / 100.0
        if speed_m_s > SPRINT_PULSE_HANDOFF_MAX_SPEED_M_S:
            return {"proven": False, "reason": "residual_speed_above_handoff_bound",
                    "horizontal_speed_m_s": speed_m_s}
        if self.actuator.is_neutral() is not True:
            return {"proven": False, "reason": "actuator_neutrality_unconfirmed",
                    "horizontal_speed_m_s": speed_m_s}
        age = self._effective_telemetry_age_seconds(observation)
        if not math.isfinite(age) or age > self.config.max_telemetry_age_seconds:
            return {"proven": False, "reason": "telemetry_stale",
                    "horizontal_speed_m_s": speed_m_s}
        if self.topology_session is None or self.authorization_proof is None:
            return {"proven": False, "reason": "route_authorization_missing",
                    "horizontal_speed_m_s": speed_m_s}
        graph = self.topology_session.graph
        corridor = self._route_corridor_refs()
        index = max(0, (self._route_local_corridor_index or 0) - 1)
        refs = corridor[index:index + 16]
        if not refs:
            return {"proven": False, "reason": "authorized_corridor_missing",
                    "horizontal_speed_m_s": speed_m_s}
        portals = []
        for source, target in zip(refs, refs[1:]):
            serialized = self._portal_for_transition(self.route_plan, source, target)
            canonical = next((portal for portal in graph.adjacency.get(source, ())
                              if portal.to_reference == target and portal.to_dict() == serialized), None)
            if canonical is None:
                return {"proven": False, "reason": "authorized_portal_mismatch",
                        "horizontal_speed_m_s": speed_m_s}
            portals.append(canonical)
        drift_bound_m = (
            speed_m_s * SPRINT_PULSE_HANDOFF_DRIFT_HORIZON_SECONDS
            + SPRINT_PULSE_HANDOFF_DRIFT_MARGIN_M
        )
        if horizontal_cm_s <= 1.0e-9:
            target = observation.position
        else:
            target = Vec3(
                observation.position.x + velocity.x / horizontal_cm_s * drift_bound_m * 100.0,
                observation.position.y + velocity.y / horizontal_cm_s * drift_bound_m * 100.0,
                observation.position.z,
            )
        obstacles = self.static_obstacles.planning_snapshot(now_t=self.monotonic()).obstacles
        if any(obstacle_segment_clearance_m(observation.position, target, item) <= 0
               for item in obstacles):
            return {"proven": False, "reason": "residual_drift_obstacle_blocked",
                    "horizontal_speed_m_s": speed_m_s, "drift_bound_m": drift_bound_m}
        if not _segment_authorized_corridor(
            observation.position,
            target,
            graph=graph,
            corridor_refs=refs,
            portals=portals,
            minimum_edge_clearance_m=0.35,
            max_vertical_separation_m=self.config.start_max_vertical_m,
        ):
            return {"proven": False, "reason": "residual_drift_corridor_unproven",
                    "horizontal_speed_m_s": speed_m_s, "drift_bound_m": drift_bound_m}
        return {
            "proven": True,
            "reason": "low_speed_residual_drift_corridor_proven",
            "horizontal_speed_m_s": speed_m_s,
            "drift_bound_m": drift_bound_m,
            "drift_horizon_seconds": SPRINT_PULSE_HANDOFF_DRIFT_HORIZON_SECONDS,
            "drift_margin_m": SPRINT_PULSE_HANDOFF_DRIFT_MARGIN_M,
            "corridor_refs": list(refs),
            "target": _point_dict(target),
        }

    def _sprint_braking_required(self, observation, supervision):
        """Prove measured deceleration before returning from sprint to pulses."""
        if not self._sprint_pending_settle:
            return False
        if supervision is not None and self._sprint_brake_started_t is None:
            return False
        now = self.monotonic()
        if self._sprint_brake_started_t is None:
            if not self._neutralize("sprint_to_pulse_braking"):
                raise AdapterError("sprint_brake_neutral_unconfirmed")
            self._pause_follower_for_readiness(observation.monotonic_t, "sprint_braking")
            self._sprint_brake_started_t = now
            self._sprint_brake_previous = None
            return True
        elapsed = now - self._sprint_brake_started_t
        if elapsed > SPRINT_SETTLE_TIMEOUT_SECONDS:
            raise AdapterError("sprint_braking_unproven")
        previous = self._sprint_brake_previous
        velocity = observation.velocity_cm_s
        slow = (observation.grounded is True and velocity is not None
            and math.hypot(velocity.x, velocity.y) <= 50.0)
        stable = (slow and previous is not None and observation.sequence > previous.sequence
            and math.hypot(observation.position.x - previous.position.x,
                           observation.position.y - previous.position.y) <= 5.0
            and elapsed >= 0.15)
        handoff = (
            self._sprint_pulse_handoff_evidence(observation)
            if elapsed >= 0.15 else {"proven": False, "reason": "minimum_braking_interval_not_met"}
        )
        handoff_proven = handoff.get("proven") is True
        self._sprint_brake_previous = observation if slow else None
        self.timeline.append({"event": "sprint_braking_sample", "t": now,
            "sequence": observation.sequence, "stable": stable,
            "low_speed_handoff_proven": handoff_proven,
            "low_speed_handoff": handoff, "movement_command_sent": False,
            "elapsed_seconds": elapsed,
            "grounded": observation.grounded,
            "horizontal_speed_m_s": math.hypot(velocity.x, velocity.y) / 100.0 if velocity is not None else None,
            "position": _point_dict(observation.position)})
        if stable or handoff_proven:
            self._resume_follower_after_readiness(observation.monotonic_t, "sprint_braking")
            self._sprint_pending_settle = False
            self._sprint_brake_started_t = None
            self._sprint_brake_previous = None
            if handoff_proven:
                self.timeline.append({
                    "event": "sprint_low_speed_pulse_handoff_proven",
                    "t": now,
                    "sequence": observation.sequence,
                    "movement_command_sent": False,
                    **handoff,
                })
        # Consume the release sample without input. The next command starts
        # with newly checked readiness, identity, geometry and telemetry.
        return True

    @staticmethod
    def _bounded_command_duration_ms(seconds: float) -> int:
        milliseconds = int(round(float(seconds) * 1000.0))
        return max(MIN_COMMAND_DURATION_MS, min(MAX_COMMAND_DURATION_MS, milliseconds))

    def _steering(self, observation: LiveObservation, event: Mapping[str, Any]) -> tuple[float, float, dict[str, Any]]:
        desired = event.get("desired_heading_deg")
        if desired is None or observation.camera_yaw is None:
            raise AdapterError("steering_heading_or_camera_yaw_missing")
        desired = float(desired)
        camera = float(observation.camera_yaw)
        relative = _normalize_degrees(desired - camera)
        speed_band = str(event.get("suggested_speed_band") or "stop")
        distance = event.get("distance_to_final_destination_m")
        final_distance = float(distance) if distance is not None else math.inf
        active_distance = event.get("distance_m")
        active_distance = float(active_distance) if active_distance is not None else math.inf
        heading_error = event.get("heading_error_deg")
        heading_error = float(heading_error) if heading_error is not None else math.nan
        follower_distance_slow = speed_band == "slow" and active_distance <= 2.0
        follower_heading_slow = speed_band == "slow" and math.isfinite(heading_error) and abs(heading_error) >= 110.0
        adapter_final_distance_slow = final_distance <= self.config.final_slow_distance_m
        # A full-speed pulse must leave room for motion during both telemetry
        # age and the pulse, followed by the existing neutral braking margin.
        # The old distance-only 1.5 m switch could select a 300 ms full pulse
        # while Cloud was already travelling at 6 m/s near a 3 m destination.
        # This is an earlier control switch, never a larger arrival envelope.
        stopping_horizon_m = (
            NON_SPRINT_SPEED_BOUND_M_S * (
                self.config.max_telemetry_age_seconds + self.config.control_period_seconds
            ) + 0.75 + self.config.final_arrival_radius_m
        )
        # Continuous travel bounds each held lease by remaining distance, but
        # it still needs a neutral handoff once the actor is moving inside
        # the residual-arrival envelope.
        horizontal_speed_m_s = 0.0
        if observation.velocity_cm_s is not None:
            horizontal_speed_m_s = math.hypot(
                float(observation.velocity_cm_s.x),
                float(observation.velocity_cm_s.y),
            ) / 100.0
        # A stationary character needs an activation pulse before the
        # braking envelope is useful. Applying the 150 ms final pulse from
        # rest can turn a clear short route into in-place rotations; the
        # resulting no-progress sample must not be mistaken for a world
        # obstacle. Once measured motion exists, retain the bounded horizon.
        # A moving actor needs a neutral braking interval before the final
        # 250 ms movement hold.  Releasing the stick is the only measured
        # braking action available to the gamepad bridge; sending another
        # positive pulse inside this envelope creates residual travel that can
        # leave the original 0.75 m arrival proof after the first sample.
        residual_braking_horizon_m = (
            horizontal_speed_m_s * self.config.approach_control_period_seconds
            + 0.75 + self.config.final_arrival_radius_m
        )
        arrival_braking = (
            self.config.controller_mode in {"pulse", "continuous"}
            and horizontal_speed_m_s >= 1.0
            and final_distance > self.config.final_arrival_radius_m
            and final_distance <= residual_braking_horizon_m
        )
        # A stationary character still needs an activation pulse. Once motion
        # is measured, residual braking takes precedence over the old broad
        # stopping horizon so the short-route controller does not feed another
        # positive pulse into an already moving actor.
        arrival_horizon = False
        selection_reasons: list[str] = []
        if speed_band == "stop" or (distance is not None and float(distance) <= 0.0):
            magnitude = 0.0
            control_band = "stop"
            command_duration_ms = 0
            selection_reasons.append("follower_stop")
        elif arrival_braking:
            magnitude = 0.0
            control_band = "braking"
            command_duration_ms = self._bounded_command_duration_ms(self.config.approach_control_period_seconds)
            selection_reasons.append("arrival_residual_braking")
        elif final_distance <= self.config.final_control_distance_m or arrival_horizon:
            magnitude = self.config.final_stick_magnitude
            control_band = "final"
            command_duration_ms = self._bounded_command_duration_ms(self.config.final_control_period_seconds)
            if follower_distance_slow:
                selection_reasons.append("follower_distance_slow")
            if follower_heading_slow:
                selection_reasons.append("follower_heading_slow")
            if adapter_final_distance_slow:
                selection_reasons.append("adapter_final_distance_slow")
            if final_distance <= self.config.final_control_distance_m:
                selection_reasons.append("final_control_distance")
            if arrival_horizon:
                selection_reasons.append("arrival_stopping_horizon")
        elif speed_band == "slow" or adapter_final_distance_slow:
            magnitude = self.config.approach_stick_magnitude
            control_band = "approach"
            command_duration_ms = self._bounded_command_duration_ms(self.config.approach_control_period_seconds)
            if follower_distance_slow:
                selection_reasons.append("follower_distance_slow")
            if follower_heading_slow:
                selection_reasons.append("follower_heading_slow")
            if adapter_final_distance_slow:
                selection_reasons.append("adapter_final_distance_slow")
        elif speed_band == "normal":
            magnitude = self.config.normal_stick_magnitude
            control_band = "normal"
            command_duration_ms = self._bounded_command_duration_ms(self.config.control_period_seconds)
            selection_reasons.append("follower_normal")
        else:
            magnitude = 0.0
            control_band = "stop"
            command_duration_ms = 0
            selection_reasons.append("unrecognized_speed_band")
        x, y = world_heading_to_camera_stick(desired, camera, magnitude)
        return x, y, {
            "desired_world_heading_deg": desired,
            "camera_world_yaw_deg": camera,
            "desired_stick_angle_deg": relative,
            "magnitude": math.hypot(x, y),
            "speed_band": speed_band,
            "control_band": control_band,
            "control_selection_reasons": selection_reasons,
            "arrival_stopping_horizon_m": stopping_horizon_m,
            "arrival_residual_braking_horizon_m": residual_braking_horizon_m,
            "horizontal_speed_m_s": horizontal_speed_m_s,
            "command_duration_ms": command_duration_ms,
            "minimum_effective_stick_magnitude": self.config.minimum_effective_stick_magnitude,
            "legacy_slow_stick_magnitude": self.config.slow_stick_magnitude,
            # No formal displacement model is used for authorization.  The
            # actual post-pulse observation remains the source of truth.
            "expected_displacement_range_m": None,
            "expected_displacement_basis": "historical_effective_pulse_only",
            "actuator_convention": "camera_relative_x_right_y_forward_unreal_yaw",
        }

    def invalidate_plan(self, reason: str) -> dict[str, Any]:
        """Stop immediately for a reviewed external plan invalidation."""
        allowed = {"topology_changed", "plan_stale", "polygon_ref_invalid", "world_changed"}
        if reason not in allowed:
            raise AdapterError("invalid_plan_invalidation")
        self._neutralize(reason)
        if self.follower is not None:
            self.follower.process_external_event(reason, self.monotonic())
        self._set_state(AdapterState.REPLAN_REQUIRED, reason)
        return {"state": self.state.value, "reason": self.reason, "movement_authorized": False}

    def _record_cycle(self, observation: LiveObservation, event: Mapping[str, Any], command: Mapping[str, Any] | None, sent: bool, safety: str) -> None:
        accounting = self._follower_progress_accounting(observation.monotonic_t)
        route_metrics = self._authorized_route_metrics(observation.position) or {}
        current_polygon_ref = self._last_resolved_polygon_ref
        self.timeline.append({
            "event": "control_cycle",
            "monotonic_t": observation.monotonic_t,
            "position": _point_dict(observation.position),
            "actor_yaw": observation.actor_yaw,
            "camera_yaw": observation.camera_yaw,
            "waypoint_index": event.get("waypoint_index"),
            "distance_to_waypoint_m": event.get("distance_m"),
            "distance_to_final_m": event.get("distance_to_final_destination_m"),
            "cross_track_error_m": event.get("cross_track_error_m"),
            "route_progress": event.get("route_progress_fraction"),
            "route_metrics": route_metrics,
            "current_polygon_ref": current_polygon_ref,
            "follower_state": event.get("state"),
            "desired_heading_deg": event.get("desired_heading_deg"),
            "heading_error_deg": event.get("heading_error_deg"),
            "actuator": command,
            "camera_follow": dict(self._last_camera_result),
            "actual_command_sent": sent,
            "safety": safety,
            "command_duration_ms": command.get("milliseconds") if isinstance(command, Mapping) else None,
            "last_meaningful_progress_t": accounting.get("last_meaningful_progress_t") if accounting else None,
            "active_stall_elapsed_seconds": accounting.get("active_stall_elapsed_seconds") if accounting else event.get("elapsed_since_meaningful_progress_s"),
            "safety_pause_total_seconds": accounting.get("safety_pause_total_seconds") if accounting else None,
        })

    def run(self, *, dry_run: bool, max_cycles: int | None = None) -> dict[str, Any]:
        """Run the closed-loop follower; dry-run always keeps the actuator neutral."""
        if self.route_plan is None or self.follower is None or self.authorization is None:
            raise AdapterError("movement_pipeline_not_preflighted")
        if not dry_run and not self.movement_authorized:
            raise AdapterError("movement_not_authorized")
        cycles = int(max_cycles or self.config.max_cycles)
        if self.config.route_mode == "ordinary_multi_polygon":
            cycles = min(cycles, 100)
        elif self.config.route_mode == "ordinary_multi_tile":
            route_length = float((self.route_plan or {}).get("total_estimated_path_length_m", 0.0))
            derived_budget = derive_multi_tile_cycle_budget(
                route_length,
                absolute_max_cycles=self.config.multi_tile_absolute_max_cycles,
            )
            requested_budget = int(max_cycles) if max_cycles is not None else self.config.multi_tile_absolute_max_cycles
            cycles = min(requested_budget, derived_budget)
            self.timeline.append({
                "event": "ordinary_multi_tile_cycle_budget_selected",
                "t": self.monotonic(),
                "route_length_m": route_length,
                "requested_budget": requested_budget,
                "derived_budget": derived_budget,
                "selected_budget": cycles,
                "minimum_cycles": 40,
                "absolute_max_cycles": self.config.multi_tile_absolute_max_cycles,
                "formula": "ceil(route_length_m * 6) + 12, bounded by [40, absolute_max_cycles]",
            })
        elif self.config.route_mode == "cached_topology":
            route_length = float((self.route_plan or {}).get("total_estimated_path_length_m", 0.0))
            derived_budget = max(40, int(math.ceil(route_length * 6.0)) + 12)
            requested_budget = int(max_cycles) if max_cycles is not None else self.config.cached_topology_absolute_max_cycles
            cycles = min(requested_budget, derived_budget, self.config.cached_topology_absolute_max_cycles)
            self.timeline.append({
                "event": "cached_topology_cycle_budget_selected",
                "t": self.monotonic(),
                "route_length_m": route_length,
                "requested_budget": requested_budget,
                "derived_budget": derived_budget,
                "selected_budget": cycles,
                "absolute_max_cycles": self.config.cached_topology_absolute_max_cycles,
                "formula": "max(40, ceil(route_length_m * 6) + 12), bounded by cached_topology_absolute_max_cycles",
            })
        self._run_started_t = self.monotonic()
        try:
            first_observation = self._final_pre_movement_gate()
            if self.config.pursuit and not dry_run:
                if self._run_pursuit(first_observation):
                    return {
                        "state": self.state.value,
                        "reason": self.reason,
                        "movement_authorized": self.movement_authorized,
                        "dry_run": dry_run,
                        "movement_cycles": self._attempt_movement_cycles,
                        "timeline": list(self.timeline),
                        "route_plan_sha256": self.authorization.route_plan_sha256,
                    }
                if self.state == AdapterState.STOPPED and self.reason in GATE_STOP_REASONS:
                    # A gate on the route stayed shut: the route ends here with
                    # that outcome; the careful controller must not push at it.
                    return {
                        "state": self.state.value,
                        "reason": self.reason,
                        "movement_authorized": self.movement_authorized,
                        "dry_run": dry_run,
                        "movement_cycles": self._attempt_movement_cycles,
                        "timeline": list(self.timeline),
                        "route_plan_sha256": self.authorization.route_plan_sha256,
                    }
                # Handed over: the careful controller continues from here.
                first_observation = self._final_pre_movement_gate()
            for cycle_index in range(cycles):
                if (
                    self.config.route_mode in {"ordinary_multi_polygon", "ordinary_multi_tile", "cached_topology"}
                    and self._run_started_t is not None
                    and self.monotonic() - self._run_started_t > (
                        self.config.cached_topology_wall_clock_seconds
                        if self.config.route_mode == "cached_topology"
                        else self.config.multi_polygon_wall_clock_seconds
                    )
                ):
                    wall_clock_reason = (
                        "multi_tile_wall_clock_bound_reached"
                        if self.config.route_mode == "ordinary_multi_tile"
                        else "cached_topology_wall_clock_bound_reached"
                        if self.config.route_mode == "cached_topology"
                        else "multi_polygon_wall_clock_bound_reached"
                    )
                    self._neutralize(wall_clock_reason)
                    self._set_state(AdapterState.STOPPED, wall_clock_reason)
                    self.timeline.append({
                        "event": wall_clock_reason,
                        "t": self.monotonic(),
                        "wall_clock_seconds": self.monotonic() - self._run_started_t,
                        "max_wall_clock_seconds": (
                            self.config.cached_topology_wall_clock_seconds
                            if self.config.route_mode == "cached_topology"
                            else self.config.multi_polygon_wall_clock_seconds
                        ),
                    })
                    break
                # Use the observation that just passed the final gate for the
                # first control cycle.  Re-observing here creates a needless
                # race against the 0.5 s freshness bound immediately before
                # the first command.
                if cycle_index == 0:
                    observation = first_observation
                    events = None
                    event = None
                    self._active_post_command_timing = None
                    self._active_post_command_route_sample = None
                else:
                    if self._last_command_sequence is None:
                        raise AdapterError("post_command_sequence_missing")
                    after_sequence = self._last_command_sequence
                    self._active_post_command_timing = {}
                    self._active_post_command_route_sample = None
                    self.timeline.append({
                        "event": "waiting_for_next_telemetry",
                        "t": self.monotonic(),
                        "after_sequence": after_sequence,
                        "actuator_neutral": self.actuator.is_neutral(),
                        "command_completed_t": self._last_command_completed_t,
                        "position_before": (
                            self._pending_command_evidence.get("position_before")
                            if self._pending_command_evidence else None
                        ),
                    })
                    try:
                        observation = self._require_observation(
                            wait_for_fresh=True,
                            after_sequence=after_sequence,
                            timeout_seconds=self.config.preflight_telemetry_wait_seconds,
                            timing=self._active_post_command_timing,
                        )
                    except AdapterError as exc:
                        if str(exc) not in {"telemetry_stale", "telemetry_not_advancing"}:
                            raise
                        last_sample = self._last_sample_attempt
                        self.timeline.append({
                            "event": "telemetry_advancement_timeout",
                            "t": self.monotonic(),
                            "after_sequence": after_sequence,
                            "last_observed_sequence": last_sample.sequence if last_sample is not None else None,
                            "last_observed_age_seconds": last_sample.telemetry_age_seconds if last_sample is not None else None,
                            "wait_duration_seconds": max(
                                0.0,
                                self.monotonic() - (self._last_command_completed_t or self.monotonic()),
                            ),
                            "error": str(exc),
                        })
                        self._neutralize("telemetry_advancement_timeout")
                        if dry_run:
                            self._set_state(AdapterState.STOPPED, "telemetry_advancement_timeout")
                            break
                        recovered = self._recover_from_readiness_loss(
                            trigger=None,
                            failures=["telemetry_advancement_timeout"],
                            trigger_error="telemetry_advancement_timeout",
                            trigger_event_name="telemetry_advancement_timeout_recovery_started",
                        )
                        if recovered is None:
                            break
                        observation, events, event = recovered
                    else:
                        events = None
                        event = None
                if not dry_run and cycle_index == 0:
                    # The initial command gate must reject any uncommanded
                    # movement while the route authorization is being
                    # established. Later following samples may legitimately
                    # contain actor-origin residual motion from the preceding
                    # pulse; _ensure_fresh_following_observation below is the
                    # continuation gate that validates and reacquires those
                    # samples without treating residual motion as a new start.
                    observation = self._ensure_fresh_command_observation(observation)
                if self._last_command_sequence is not None and observation.sequence <= self._last_command_sequence:
                    self._neutralize("telemetry_not_advancing")
                    self._set_state(AdapterState.STOPPED, "telemetry_not_advancing")
                    self.timeline.append({
                        "event": "telemetry_not_advancing",
                        "monotonic_t": observation.monotonic_t,
                        "sequence": observation.sequence,
                        "previous_command_sequence": self._last_command_sequence,
                    })
                    break
                failures = self._check_preconditions(observation)
                authorization_ok_before_post_record = self._same_authorization(observation)
                if failures or not authorization_ok_before_post_record:
                    # Preserve the first fresh post-pulse sample even when it
                    # is the sample that triggers a readiness/identity stop.
                    self._record_post_command_observation(observation)
                    authorization_ok = self._same_authorization(observation)
                    static_replan_ready = (
                        not dry_run
                        and self.state in {AdapterState.FOLLOWING, AdapterState.PLAN_VALIDATED}
                        and authorization_ok_before_post_record
                        and self._static_pending_discovery is not None
                        and self._is_readiness_only_failure(failures)
                    )
                    if static_replan_ready:
                        self.timeline.append({
                            "event": "static_obstacle_replan_intercepted_readiness_failure",
                            "t": observation.monotonic_t,
                            "sequence": observation.sequence,
                            "failures": list(failures),
                            "authorization_valid_before_post_record": True,
                            "authorization_valid_after_post_record": authorization_ok,
                            "movement_command_sent": False,
                        })
                        replanned_observation = self._replan_for_static_obstacle(observation)
                        if replanned_observation is None:
                            break
                        observation = replanned_observation
                        events, event = self._follower_event(observation)
                    elif (
                        not dry_run
                        and self.state in {AdapterState.FOLLOWING, AdapterState.PLAN_VALIDATED}
                        and authorization_ok
                        and self._is_readiness_only_failure(failures)
                    ):
                        self._neutralize("readiness_loss_during_following")
                        recovered = self._recover_from_readiness_loss(
                            trigger=observation,
                            failures=failures,
                        )
                        if recovered is None:
                            break
                        observation, events, event = recovered
                    else:
                        self._neutralize("live_precondition_or_plan_identity_failure")
                        self._set_state(AdapterState.STOPPED if failures else AdapterState.REPLAN_REQUIRED, ";".join(failures) or "plan_identity_changed")
                        break
                if self._sprint_brake_started_t is not None:
                    # Readiness recovery or a replan can replace/resume the
                    # follower while input is still intentionally neutral.
                    self._pause_follower_for_readiness(observation.monotonic_t, "sprint_braking")
                if events is None or event is None:
                    timing = self._active_post_command_timing
                    if timing is not None:
                        events, event = self._timed_stage(
                            timing,
                            "controller_decision_ms",
                            lambda: self._follower_event(observation),
                        )
                    else:
                        events, event = self._follower_event(observation)
                if not dry_run and self._last_command_sequence is not None:
                    try:
                        post_command_observation = self._ensure_fresh_following_observation(observation)
                    except _PostCommandReadinessError as exc:
                        trigger = self._last_sample_attempt or observation
                        recovered = self._recover_from_readiness_loss(
                            trigger=trigger,
                            failures=exc.failures,
                            trigger_error=str(exc),
                        )
                        if recovered is None:
                            break
                        observation, events, event = recovered
                    else:
                        if post_command_observation.sequence != observation.sequence:
                            observation = post_command_observation
                            timing = self._active_post_command_timing
                            if timing is not None:
                                events, event = self._timed_stage(
                                    timing,
                                    "controller_decision_reacquired_sample_ms",
                                    lambda: self._follower_event(observation),
                                )
                            else:
                                events, event = self._follower_event(observation)
                try:
                    self._record_post_command_observation(observation, event)
                except AdapterError:
                    timing = self._active_post_command_timing
                    if timing is not None:
                        self._emit_post_command_timing(
                            observation.sequence,
                            timing,
                            outcome="post_command_observation_rejected",
                        )
                    raise
                if not dry_run and self._static_pending_discovery is not None:
                    replanned_observation = self._replan_for_static_obstacle(observation)
                    if replanned_observation is None:
                        break
                    observation = replanned_observation
                    events, event = self._follower_event(observation)
                # Smooth camera-follow turns the camera during the commands
                # themselves; only the snap mode stops Cloud to turn it.
                coordinated = (observation if self.config.camera_follow_smooth
                               else self._coordinate_camera_follow(observation, event, dry_run=dry_run))
                if coordinated.sequence != observation.sequence:
                    observation = coordinated
                    events, event = self._follower_event(observation)
                if not dry_run:
                    crossing = self._pending_traversal_for(event, observation)
                    if crossing is not None:
                        # The follower has reached an off-mesh link's departure
                        # point: walk the link, prove the landing, go on.
                        crossed, observation = self._cross_traversal(observation, crossing, source="controller")
                        if not crossed or not self._commit_traversal_landing(observation, crossing):
                            break
                        self._pending_command_evidence = None
                        events, event = self._follower_event(observation)
                state = str(event.get("state") or "")
                if state == "STALLED" and not dry_run:
                    replanned = self._replan_around_invisible_blocker(observation, event)
                    if replanned is not None:
                        observation = replanned
                        events, event = self._follower_event(observation)
                        state = str(event.get("state") or "")
                if state in {"ARRIVED", "STALLED", "DEVIATED", "REPLAN_REQUIRED", "FAILED"}:
                    timing = self._active_post_command_timing
                    if timing is not None:
                        self._emit_post_command_timing(
                            observation.sequence,
                            timing,
                            outcome=f"follower_{state.lower()}_before_next_command",
                        )
                    self._neutralize("follower_terminal_or_unsafe_state")
                    target_state = AdapterState.ARRIVED if state == "ARRIVED" else AdapterState.REPLAN_REQUIRED if state in {"DEVIATED", "REPLAN_REQUIRED"} else AdapterState.STOPPED
                    self._set_state(target_state, f"follower_{state.lower()}")
                    self._record_cycle(observation, event, None, False, self.reason)
                    if state == "ARRIVED" and not dry_run:
                        self._confirm_post_arrival()
                    break
                timing = self._active_post_command_timing
                if timing is not None:
                    x, y, steering = self._timed_stage(
                        timing,
                        "controller_steering_ms",
                        lambda: self._steering(observation, event),
                    )
                else:
                    x, y, steering = self._steering(observation, event)
                # A non-terminal follower stop band has no movement pulse to send.
                # Wait for advancing telemetry instead of passing duration 0 to the
                # input backend, which would turn a waypoint transition into an
                # invalid command and break the live route.
                command_duration_ms = int(steering.get("command_duration_ms", 0))
                if command_duration_ms <= 0:
                    self._neutralize("follower_stop_wait")
                    self.timeline.append({
                        "event": "movement_command_suppressed",
                        "t": observation.monotonic_t,
                        "sequence": observation.sequence,
                        "reason": "follower_stop_band_without_terminal_state",
                        "follower_state": event.get("state"),
                        "suggested_speed_band": steering.get("speed_band"),
                        "actuator_neutral": self.actuator.is_neutral(),
                        "movement_command_sent": False,
                    })
                    self._record_cycle(
                        observation,
                        event,
                        None,
                        False,
                        "FOLLOWER_STOP_WAIT",
                    )
                    self._pending_command_evidence = None
                    self._last_command_sequence = observation.sequence
                    self._last_command_completed_t = self.monotonic()
                    continue
                command = {"x": x, "y": y, **steering}
                if dry_run:
                    self._neutralize("dry_run_cycle")
                    self._record_cycle(observation, event, command, False, "DRY_RUN")
                else:
                    if self.actuator.is_neutral() is not True and not (
                        self.config.controller_mode == "continuous" and self._stream_hold_active):
                        raise AdapterError("actuator_not_neutral_before_first_command")
                    effective_age = self._timed_stage(
                        timing,
                        "final_command_freshness_gate_ms",
                        lambda: self._effective_telemetry_age_seconds(observation),
                    )
                    if not math.isfinite(effective_age) or effective_age > self.config.max_telemetry_age_seconds:
                        try:
                            # After a movement command, inertia during a frame
                            # hitch is expected. Revalidate the corridor instead
                            # of imposing the stationary route-start invariant.
                            refreshed_observation = (
                                self._ensure_fresh_command_observation(observation)
                                if cycle_index == 0 else
                                self._ensure_fresh_following_observation(observation)
                            )
                        except _PostCommandReadinessError as exc:
                            self._neutralize("readiness_loss_at_final_command_gate")
                            recovered = self._recover_from_readiness_loss(
                                trigger=observation, failures=exc.failures,
                                trigger_error=str(exc))
                            if recovered is None:
                                break
                            observation, events, event = recovered
                            # Recompute steering and clearance on the next
                            # cycle; no command from the interrupted sample.
                            continue
                        except AdapterError as exc:
                            # Preserve the established terminal diagnostic
                            # when no effectively-fresh replacement exists.
                            # Identity, authorization and other safety errors
                            # retain their more specific fail-closed reason.
                            if str(exc) != "telemetry_stale_after_final_freshness_reacquisition":
                                raise
                            if timing is not None:
                                self._emit_post_command_timing(
                                    observation.sequence,
                                    timing,
                                    outcome="final_command_freshness_expired",
                                )
                            self._neutralize("telemetry_stale_immediately_before_command")
                            self.timeline.append({
                                "event": "telemetry_stale_immediately_before_command",
                                "t": self.monotonic(),
                                "sequence": observation.sequence,
                                "telemetry_age_seconds": observation.telemetry_age_seconds,
                                "effective_telemetry_age_seconds": effective_age,
                                "max_telemetry_age_seconds": self.config.max_telemetry_age_seconds,
                                "actuator_neutral": self.actuator.is_neutral(),
                            })
                            self._set_state(AdapterState.STOPPED, "telemetry_stale_immediately_before_command")
                            break
                        observation = refreshed_observation
                        timing = self._active_post_command_timing
                        if timing is not None:
                            events, event = self._timed_stage(
                                timing,
                                "controller_decision_reacquired_final_sample_ms",
                                lambda: self._follower_event(observation),
                            )
                        else:
                            events, event = self._follower_event(observation)
                        state = str(event.get("state") or "")
                        if state == "STALLED" and not dry_run:
                            replanned = self._replan_around_invisible_blocker(observation, event)
                            if replanned is not None:
                                observation = replanned
                                events, event = self._follower_event(observation)
                                state = str(event.get("state") or "")
                        if state in {"ARRIVED", "STALLED", "DEVIATED", "REPLAN_REQUIRED", "FAILED"}:
                            if timing is not None:
                                self._emit_post_command_timing(
                                    observation.sequence,
                                    timing,
                                    outcome=f"follower_{state.lower()}_before_next_command",
                                )
                            self._neutralize("follower_terminal_or_unsafe_state")
                            target_state = AdapterState.ARRIVED if state == "ARRIVED" else AdapterState.REPLAN_REQUIRED if state in {"DEVIATED", "REPLAN_REQUIRED"} else AdapterState.STOPPED
                            self._set_state(target_state, f"follower_{state.lower()}")
                            self._record_cycle(observation, event, None, False, self.reason)
                            if state == "ARRIVED" and not dry_run:
                                self._confirm_post_arrival()
                            break
                        if timing is not None:
                            x, y, steering = self._timed_stage(
                                timing,
                                "controller_steering_reacquired_final_sample_ms",
                                lambda: self._steering(observation, event),
                            )
                        else:
                            x, y, steering = self._steering(observation, event)
                        command = {"x": x, "y": y, **steering}
                        effective_age = self._timed_stage(
                            timing,
                            "final_command_freshness_gate_ms",
                            lambda: self._effective_telemetry_age_seconds(observation),
                        )
                        if not math.isfinite(effective_age) or effective_age > self.config.max_telemetry_age_seconds:
                            if timing is not None:
                                self._emit_post_command_timing(
                                    observation.sequence,
                                    timing,
                                    outcome="final_command_freshness_expired",
                                )
                            self._neutralize("telemetry_stale_immediately_before_command")
                            self.timeline.append({
                                "event": "telemetry_stale_immediately_before_command",
                                "t": self.monotonic(),
                                "sequence": observation.sequence,
                                "telemetry_age_seconds": observation.telemetry_age_seconds,
                                "effective_telemetry_age_seconds": effective_age,
                                "max_telemetry_age_seconds": self.config.max_telemetry_age_seconds,
                                "actuator_neutral": self.actuator.is_neutral(),
                            })
                            self._set_state(AdapterState.STOPPED, "telemetry_stale_immediately_before_command")
                            break
                    lookahead_gate = self._precommand_static_obstacle_gate(observation, steering)
                    if lookahead_gate == "blocked":
                        # The attempted pulse is never sent.  Replanning has
                        # its own neutral/fresh-observation/validator proof
                        # and the next loop iteration starts from that new
                        # authorization.
                        replanned_observation = self._replan_for_static_obstacle(observation)
                        if replanned_observation is None:
                            break
                        self.timeline.append({
                            "event": "static_obstacle_precommand_pulse_suppressed",
                            "t": replanned_observation.monotonic_t,
                            "sequence": replanned_observation.sequence,
                            "reason": "verified_capsule_blocker",
                            "movement_command_sent": False,
                        })
                        observation = replanned_observation
                        continue
                    carried = self._fast_follow_waypoint_carry(observation, steering, event)
                    if carried is not None:
                        # Already proven by the same supervised deadline for
                        # the carried full-magnitude steering.
                        x, y, steering, supervision = carried
                        command = {"x": x, "y": y, **steering}
                    else:
                        supervision = self._supervised_deadline(observation, steering, event)
                    if self._sprint_braking_required(observation, supervision):
                        self._last_command_sequence = observation.sequence
                        continue
                    sprint = bool(supervision and supervision.get("sprint"))
                    command = {**command, "camera_follow": dict(self._last_camera_result)}
                    if sprint:
                        x, y = world_heading_to_camera_stick(steering["desired_world_heading_deg"], observation.camera_yaw, 1.0)
                        steering = {**steering, "magnitude": 1.0}
                        command = {**command, "x": x, "y": y, **steering, "sprint": True}
                    camera_turn_degrees = 0.0
                    if not dry_run and self.config.camera_follow_smooth:
                        duration_seconds = (float(supervision["lease_seconds"]) if supervision is not None
                                            else int(steering["command_duration_ms"]) / 1000.0)
                        camera_turn_degrees = self._smooth_camera_turn(observation, duration_seconds, sprint=sprint)
                        if camera_turn_degrees:
                            # The stick is camera-relative: aim it against the
                            # camera halfway through the turn, so the average
                            # world heading is still the one proven above.
                            x, y = world_heading_to_camera_stick(
                                steering["desired_world_heading_deg"],
                                float(observation.camera_yaw) + camera_turn_degrees / 2.0,
                                min(1.0, math.hypot(x, y)))
                            command = {**command, "x": x, "y": y, "camera_turn_degrees": camera_turn_degrees}
                    self.timeline.append({
                        "event": "non_neutral_command_imminent",
                        "sprint": sprint,
                        "t": self.monotonic(),
                        "sequence": observation.sequence,
                        "telemetry_age_seconds": observation.telemetry_age_seconds,
                        "effective_telemetry_age_seconds": effective_age,
                        "position": _point_dict(observation.position),
                        "camera_yaw": observation.camera_yaw,
                        "actor_yaw": observation.actor_yaw,
                        "velocity_cm_s": _point_dict(observation.velocity_cm_s) if observation.velocity_cm_s is not None else None,
                        "horizontal_speed_m_s": (
                            math.hypot(observation.velocity_cm_s.x, observation.velocity_cm_s.y) / 100.0
                            if observation.velocity_cm_s is not None else None
                        ),
                        "desired_heading_deg": steering["desired_world_heading_deg"],
                        "heading_error_deg": event.get("heading_error_deg"),
                        "suggested_speed_band": steering["speed_band"],
                        "control_band": steering["control_band"],
                        "control_selection_reasons": steering["control_selection_reasons"],
                        "stick_x": x,
                        "stick_y": y,
                        "magnitude": steering["magnitude"],
                        "command_duration_ms": steering["command_duration_ms"],
                        "minimum_effective_stick_magnitude": steering["minimum_effective_stick_magnitude"],
                        "expected_displacement_range_m": steering["expected_displacement_range_m"],
                        "distance_to_final_before_m": event.get("distance_to_final_destination_m"),
                        "route_progress_before": event.get("route_progress_fraction"),
                        "cross_track_before_m": event.get("cross_track_error_m"),
                        "route_metrics_before": self._authorized_route_metrics(observation.position),
                        "current_polygon_ref": self._last_resolved_polygon_ref,
                        "active_stall_elapsed_before_seconds": (
                            self._follower_progress_accounting(observation.monotonic_t) or {}
                        ).get("active_stall_elapsed_seconds"),
                        "last_meaningful_progress_t_before": (
                            self._follower_progress_accounting(observation.monotonic_t) or {}
                        ).get("last_meaningful_progress_t"),
                        "position_before": _point_dict(observation.position),
                        "route_plan_sha256": self.authorization.route_plan_sha256,
                    })
                    if timing is not None:
                        self._emit_post_command_timing(
                            observation.sequence,
                            timing,
                            outcome="pre_command_authorized",
                        )
                    command_duration_ms = int(steering["command_duration_ms"])
                    # A pulse blocks for its length, so its sweep starts first;
                    # a supervised hold is sent first, its deadline being live.
                    if camera_turn_degrees and supervision is None:
                        self._start_camera_stream(camera_turn_degrees, int(steering["command_duration_ms"]))
                    command_started_t = self.monotonic()
                    if supervision is not None:
                        try:
                            result = self.actuator.supervised_command(x, y, deadline=supervision["deadline"],
                                stream_id=self._stream_id, sequence=observation.sequence, sprint=sprint)
                        except Exception as exc:
                            # The lease ran out before the broker took it (a
                            # busy CPU). The broker refused it, so nothing
                            # moved; send this cycle's ordinary pulse rather
                            # than abort an otherwise reachable route (#169).
                            if "supervised_deadline_invalid_or_expired" not in str(exc):
                                raise
                            self.timeline.append({
                                "event": "supervised_lease_expired_pulse_fallback", "t": self.monotonic(),
                                "sequence": observation.sequence, "error": str(exc)[:200],
                            })
                            supervision = None
                            if camera_turn_degrees:
                                self._start_camera_stream(camera_turn_degrees, int(steering["command_duration_ms"]))
                    if supervision is not None:
                        command_duration_ms = int(supervision["lease_seconds"] * 1000)
                        if camera_turn_degrees:
                            self._start_camera_stream(camera_turn_degrees, int(supervision["lease_seconds"] * 1000))
                        self._stream_hold_active = True
                        self._sprint_pending_settle = self._sprint_pending_settle or sprint
                    else:
                        if self._stream_hold_active:
                            self._neutralize("continuous_to_pulse_transition")
                            self._stream_hold_active = False
                        result = self.actuator.command(x, y, command_duration_ms)
                    command_completed_t = self.monotonic()
                    command = {
                        **command,
                        "milliseconds": command_duration_ms,
                        "backend_result": result,
                    }
                    neutral_confirmed = (False if supervision is not None
                        else self._neutralize("movement_pulse_completed"))
                    self._last_command_completed_t = command_completed_t
                    self._pending_command_evidence = {
                        "sequence": observation.sequence,
                        "position_before": _point_dict(observation.position),
                        "command_started_t": command_started_t,
                        "command_completed_t": command_completed_t,
                        "command_duration_ms": command_duration_ms,
                        "supervision": supervision,
                        "stick_x": x,
                        "stick_y": y,
                        "magnitude": steering["magnitude"],
                        "suggested_speed_band": steering["speed_band"],
                        "control_band": steering["control_band"],
                        "control_selection_reasons": list(steering["control_selection_reasons"]),
                        "desired_world_heading_deg": steering["desired_world_heading_deg"],
                        "camera_world_yaw_degrees": observation.camera_yaw,
                        "distance_to_final_before_m": event.get("distance_to_final_destination_m"),
                        "along_route_before_m": (
                            self._authorized_route_metrics(observation.position) or {}
                        ).get("along_route_m"),
                        "route_progress_before": event.get("route_progress_fraction"),
                        "cross_track_before_m": event.get("cross_track_error_m"),
                        "active_stall_elapsed_before_seconds": (
                            self._follower_progress_accounting(observation.monotonic_t) or {}
                        ).get("active_stall_elapsed_seconds"),
                        "last_meaningful_progress_t_before": (
                            self._follower_progress_accounting(observation.monotonic_t) or {}
                        ).get("last_meaningful_progress_t"),
                    }
                    self.timeline.append({
                        "event": "movement_setpoint_issued" if supervision is not None else "movement_pulse_completed",
                        "supervision": supervision,
                        "t": command_completed_t,
                        "sequence": observation.sequence,
                        "command_duration_ms": command_duration_ms,
                        "command_wall_duration_seconds": max(0.0, command_completed_t - command_started_t),
                        "position_before": _point_dict(observation.position),
                        "actuator_neutral": neutral_confirmed,
                    })
                    self._attempt_movement_cycles += 1
                    self._set_state(AdapterState.FOLLOWING, "closed_loop_following")
                    self._record_cycle(observation, event, command, True, "AUTHORIZED")
                self._last_command_sequence = observation.sequence
            else:
                self._neutralize("control_cycle_bound_reached")
                self._set_state(AdapterState.STOPPED, "control_cycle_bound_reached")
        except KeyboardInterrupt:
            self._neutralize("task_cancelled")
            self._set_state(AdapterState.STOPPED, "task_cancelled")
        except AdapterError as exc:
            # Expected safety/integrity failures stop the attempt cleanly.  The
            # actuator is neutralized before exposing the terminal state; the
            # caller should receive a structured fail-closed result rather than
            # having to infer whether a command could have remained active.
            self._neutralize("adapter_safety_failure")
            reason = str(exc)
            replan_reasons = {"plan_identity_changed"}
            target = AdapterState.REPLAN_REQUIRED if reason in replan_reasons else AdapterState.STOPPED
            self._set_state(target, reason)
        except Exception as exc:
            try:
                self._neutralize("adapter_exception")
            finally:
                self._set_state(AdapterState.FAILED, f"adapter_exception:{type(exc).__name__}")
            raise
        finally:
            if dry_run or self.config.controller_mode == "continuous":
                self._neutralize("dry_run_finally" if dry_run else "continuous_controller_finally")
                self._stream_hold_active = False
        return {
            "state": self.state.value,
            "reason": self.reason,
            "movement_authorized": self.movement_authorized,
            "dry_run": dry_run,
            "movement_cycles": self._attempt_movement_cycles,
            "timeline": list(self.timeline),
            "route_plan_sha256": self.authorization.route_plan_sha256,
        }

    def _run_pursuit(self, first_observation: LiveObservation) -> bool:
        """Stream the stick along the planned route; True once arrived.

        Pure pursuit: every new telemetry sample renews one supervised hold
        (the broker releases the stick if renewals stop) aimed at a point a
        speed-dependent distance ahead on the route, with sprint on straight
        runs. Per-command corridor proofs are replaced by sparse supervision:
        route progress, cross-track and readiness. Anything unexpected
        neutralizes and hands the route back to the careful controller
        (returns False). Live, that controller's stop-go pulses averaged
        0.85-1.5 m/s on the Nibelheim-to-Chadley route against a human's ~7.
        """
        points = [Vec3(float(p.x), float(p.y), float(p.z)) for p in self.authorization_proof.waypoints]
        if len(points) < 2:
            return False
        lengths = [_horizontal_distance_m(a, b) for a, b in zip(points, points[1:])]
        starts = [0.0]
        for length in lengths[:-1]:
            starts.append(starts[-1] + length)
        total = starts[-1] + lengths[-1]

        def project(position: Vec3, near_index: int) -> tuple[int, float, float]:
            best = (near_index, math.inf, 0.0)
            for index in range(max(0, near_index - 1), min(len(lengths), near_index + PURSUIT_SEARCH_SEGMENTS)):
                a, b = points[index], points[index + 1]
                dx, dy = b.x - a.x, b.y - a.y
                length_sq = dx * dx + dy * dy
                t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((position.x - a.x) * dx + (position.y - a.y) * dy) / length_sq))
                cross = math.hypot(a.x + t * dx - position.x, a.y + t * dy - position.y) / 100.0
                if cross < best[1]:
                    best = (index, cross, starts[index] + t * lengths[index])
            return best

        def point_at(along_m: float) -> Vec3:
            along_m = max(0.0, min(total, along_m))
            for index, start in enumerate(starts):
                if along_m <= start + lengths[index] or index == len(lengths) - 1:
                    t = 0.0 if lengths[index] == 0 else (along_m - start) / lengths[index]
                    a, b = points[index], points[index + 1]
                    return Vec3(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z + (b.z - a.z) * t)
            return points[-1]

        def hand_over(reason: str, **detail: Any) -> bool:
            self._neutralize("pursuit_" + reason)
            self.timeline.append({"event": "pursuit_handed_over", "t": self.monotonic(), "reason": reason,
                                  "commands": commands, **detail})
            return False

        # Automatic doors and gates the plan goes through (its gate waypoints):
        # stop short of each and wait for it to open (wait_at_gate).
        gate_stops = ([dict(gate, cleared=False) for gate in plan_gates(self.route_plan)]
                      if self.route_collision_audit is not None else [])

        def wait_at_gate(gate: dict[str, Any], current: LiveObservation, along_m: float):
            """Release the stick short of a gate and wait for it to open.

            Sends no input: it re-sweeps just the gate's footprint (a few
            seconds at most) and keeps watching readiness. Returns
            ("go", fresh observation), ("stop", None) with the route stopped
            for good, or ("handover", reason).
            """
            self._neutralize("pursuit_gate_wait")
            started = self.monotonic()
            self.timeline.append({
                "event": "gate_wait_started", "t": started, "gate_id": gate.get("gate_id"),
                "actor": gate.get("actor"), "reason": gate.get("reason"),
                "distance_to_gate_m": round(float(gate["along_m"]) - along_m, 2), "movement_command_sent": False})
            latest = [current]
            prompt = [bool(current.in_interaction)]

            def interrupted() -> str | None:
                try:
                    sample = self._require_observation(wait_for_fresh=True, after_sequence=latest[0].sequence,
                                                       timeout_seconds=PURSUIT_TELEMETRY_STALL_SECONDS)
                except AdapterError as exc:
                    return "telemetry_stall:" + str(exc)
                latest[0] = sample
                # A prompt beside the gate does not end the wait: it is evidence
                # (the gate may want the player), and no input is sent here.
                prompt[0] = prompt[0] or bool(sample.in_interaction)
                failures = [name for name in self._check_preconditions(sample) if name != "interaction_active"]
                return ";".join(failures) or None

            result = wait_for_gate(gate, sweep=self.route_collision_audit, now=self.monotonic,
                                   sleep=self.sleeper, interrupted=interrupted)
            if result.outcome == "opened":
                gate["cleared"] = True
                self.timeline.append({"event": "gate_opened", "t": self.monotonic(), "gate_id": gate.get("gate_id"),
                                      "actor": gate.get("actor"), "wait": result.to_dict(),
                                      "movement_command_sent": False})
                try:
                    return "go", self._require_observation(
                        wait_for_fresh=True, after_sequence=latest[0].sequence,
                        timeout_seconds=PURSUIT_TELEMETRY_STALL_SECONDS)
                except AdapterError as exc:
                    return "handover", "telemetry_stall:" + str(exc)
            if result.outcome == "interrupted":
                return "handover", "gate_wait_interrupted:" + str(result.interruption)
            # Still shut (or no sweep could say): stop here with the evidence,
            # not a generic stall; the careful controller does not push at it.
            self._neutralize("pursuit_" + result.outcome)
            sample = latest[0]
            nearby = [{key: item.get(key) for key in ("id", "kind", "name", "distance", "prompt", "usable", "opened")}
                      for item in (sample.raw or {}).get("nearby_actors") or []
                      if isinstance(item, Mapping) and (item.get("prompt") or item.get("opened") is False)][:3]
            self.timeline.append({
                "event": "gate_did_not_open" if result.outcome == "gate_did_not_open" else "gate_state_unverified",
                "t": self.monotonic(), "gate": {key: gate.get(key) for key in (
                    "gate_id", "actor", "actor_class", "reason", "signals", "bounds", "evidence", "along_m")},
                "wait": result.to_dict(), "position": _point_dict(sample.position),
                "distance_to_gate_m": round(float(gate["along_m"]) - along_m, 2),
                "interaction_prompt_visible": prompt[0], "nearby_prompts": nearby,
                "movement_command_sent": False})
            self._set_state(AdapterState.STOPPED, result.outcome)
            return "stop", None

        observation = first_observation
        segment, commands = 0, 0
        last_sample_t = self.monotonic()
        progress_window: list[tuple[float, float]] = []
        next_camera_t = 0.0
        airborne_since: float | None = None
        self._set_state(AdapterState.FOLLOWING, "pursuit_following")
        self.timeline.append({"event": "pursuit_started", "t": last_sample_t, "route_length_m": total})
        while True:
            if self.monotonic() - (self._run_started_t or last_sample_t) > self.config.cached_topology_wall_clock_seconds:
                return hand_over("wall_clock_bound")
            failures = self._check_preconditions(observation)
            if list(failures) == ["not_controllable"]:
                # A moment airborne (a step at sprint speed) or the settled-
                # gameplay proof flickering while running (live: grounded,
                # gameplay_ready false, 3 hand-overs in one run): keep
                # steering, as a player keeps holding the stick. Menu, pause
                # and combat are separate failures and hand over at once.
                airborne_since = airborne_since if airborne_since is not None else self.monotonic()
                if self.monotonic() - airborne_since <= PURSUIT_CONTROL_GRACE_SECONDS:
                    failures = []
            else:
                airborne_since = None
            if failures:
                return hand_over("readiness", failures=list(failures), grounded=observation.grounded,
                                 gameplay_ready=observation.telemetry_gameplay_ready,
                                 controllable=observation.controllable)
            # Not the route follower: it advances one waypoint per sample by
            # design, and at sprint speed Cloud passes several (1-2 m apart),
            # so it read him as deviating (live: replan after 1.5 s at
            # 8 m/s). Pursuit tracks its own projection onto the route and
            # uses the adapter's final-arrival envelope.
            if self._within_final_arrival_tolerance(observation.position, self.authorization.resolved_end):
                self._neutralize("pursuit_arrived")
                self._set_state(AdapterState.ARRIVED, "pursuit_arrived")
                self.timeline.append({"event": "pursuit_arrived", "t": self.monotonic(), "commands": commands})
                self._confirm_post_arrival()
                return self.state == AdapterState.ARRIVED
            segment, cross_track_m, along_m = project(observation.position, segment)
            if cross_track_m > PURSUIT_MAX_CROSS_TRACK_M:
                return hand_over("off_route", cross_track_m=cross_track_m)
            while gate_stops and along_m > float(gate_stops[0]["exit_m"]) + 0.5:
                gate_stops.pop(0)  # behind us now
            if (gate_stops and not gate_stops[0]["cleared"]
                    and float(gate_stops[0]["along_m"]) - along_m <= GATE_WAIT_DISTANCE_M):
                status, detail = wait_at_gate(gate_stops[0], observation, along_m)
                if status == "stop":
                    return False
                if status == "handover":
                    return hand_over(str(detail))
                observation = detail
                last_sample_t = self.monotonic()
                progress_window.clear()  # standing still at the gate is not a stall
                continue
            traversal = next((item for item in self._route_traversals if item["state"] == "pending"), None)
            if traversal is not None and (
                segment >= traversal["start_index"]
                or self._at_traversal_start(observation.position, traversal)
            ):
                # An off-mesh link: its own walk across, then the route goes on.
                crossed, observation = self._cross_traversal(observation, traversal, source="pursuit")
                if not crossed:
                    return True
                segment = traversal["end_index"]
                progress_window.clear()
                last_sample_t = self.monotonic()
                continue
            now = self.monotonic()
            progress_window.append((now, along_m))
            while progress_window and now - progress_window[0][0] > PURSUIT_PROGRESS_WINDOW_SECONDS:
                progress_window.pop(0)
            if (commands >= PURSUIT_WARMUP_COMMANDS and progress_window
                    and now - progress_window[0][0] >= PURSUIT_PROGRESS_WINDOW_SECONDS * 0.9
                    and along_m - progress_window[0][1] < PURSUIT_MIN_PROGRESS_M):
                return hand_over("no_progress", along_m=along_m)
            remaining_m = total - along_m
            velocity = observation.velocity_cm_s
            speed = math.hypot(velocity.x, velocity.y) / 100.0 if velocity is not None else 0.0
            lookahead = max(PURSUIT_MIN_LOOKAHEAD_M, min(PURSUIT_MAX_LOOKAHEAD_M, 1.5 + 0.4 * speed))
            aim_along = along_m + lookahead
            link_start_m = None
            if traversal is not None:
                # Aim at the link's departure point, not past it, and walk the
                # last stretch up to it.
                link_start_m = starts[traversal["start_index"]] - along_m
                aim_along = min(aim_along, starts[traversal["start_index"]])
            aim = point_at(aim_along)
            heading = math.degrees(math.atan2(aim.y - observation.position.y, aim.x - observation.position.x))
            straight = straight_run_ahead_m(points, observation.position)
            sprint = (remaining_m > PURSUIT_SPRINT_MIN_REMAINING_M and straight >= PURSUIT_SPRINT_MIN_STRAIGHT_M
                      and (link_start_m is None or link_start_m > TRAVERSAL_APPROACH_WALK_M)
                      # No sprinting up to a gate that may be opening.
                      and not (gate_stops and not gate_stops[0]["cleared"]
                               and float(gate_stops[0]["along_m"]) - along_m <= GATE_SPRINT_CUTOFF_M))
            magnitude = 1.0 if remaining_m > PURSUIT_SLOW_DISTANCE_M else PURSUIT_FINAL_MAGNITUDE
            sprint = sprint and magnitude >= 1.0
            if observation.camera_yaw is None:
                return hand_over("camera_yaw_missing")
            x, y = world_heading_to_camera_stick(heading, float(observation.camera_yaw), magnitude)
            if sprint:
                # The broker takes sprint only with a full stick.
                norm = math.hypot(x, y) or 1.0
                x, y = x / norm, y / norm
            deadline = now + PURSUIT_HOLD_SECONDS
            self.actuator.supervised_command(x, y, deadline=deadline, stream_id=self._stream_id,
                                             sequence=int(observation.sequence), sprint=sprint)
            self._stream_hold_active = True
            self._last_command_sequence = observation.sequence
            self._attempt_movement_cycles += 1
            commands += 1
            self.timeline.append({"event": "pursuit_command", "t": now, "sequence": observation.sequence,
                                  "along_m": round(along_m, 2), "cross_track_m": round(cross_track_m, 2),
                                  "route_progress": along_m / total if total else 0.0,
                                  "distance_to_final_m": _horizontal_distance_m(observation.position,
                                                                                self.authorization.resolved_end),
                                  "speed_m_s": round(speed, 2), "sprint": sprint, "magnitude": magnitude,
                                  "age_s": round(float(observation.telemetry_age_seconds), 3)})
            if self.config.camera_follow_smooth and now >= next_camera_t:
                turn = self._smooth_camera_turn(observation, PURSUIT_CAMERA_PERIOD_SECONDS, sprint=sprint,
                                                remaining_route_m=remaining_m)
                if turn:
                    self._start_camera_stream(turn, int(PURSUIT_CAMERA_PERIOD_SECONDS * 1000))
                next_camera_t = now + PURSUIT_CAMERA_PERIOD_SECONDS
            # Wait for the next fresh sample; the hold lapses by itself if none
            # comes before its deadline.
            try:
                observation = self._require_observation(
                    wait_for_fresh=True, after_sequence=observation.sequence,
                    timeout_seconds=PURSUIT_TELEMETRY_STALL_SECONDS)
            except AdapterError as exc:
                return hand_over("telemetry_stall", error=str(exc))
            last_sample_t = self.monotonic()

    def _confirm_post_arrival(self) -> None:
        self._neutralize_camera("arrival_confirmation")
        if self.authorization is None:
            return
        end = self.authorization.resolved_end
        samples: list[dict[str, Any]] = []
        after_sequence = self._last_observation.sequence if self._last_observation else self._last_command_sequence
        arrival_previous = self._last_observation
        try:
            for _ in range(self.config.post_arrival_samples):
                observation = self._require_observation(
                    wait_for_fresh=True, after_sequence=after_sequence,
                    require_effective_fresh=True,
                )
                # Retain the previous sample before _require_observation
                # updates _last_observation.
                if arrival_previous is not None:
                    observation = self._settle_preflight_pose(
                        arrival_previous, observation, require_lease=True, phase="arrival",
                    )
                arrival_previous = observation
                after_sequence = observation.sequence
                failures = self._check_preconditions(observation)
                if failures or not self._same_authorization(observation):
                    raise AdapterError(";".join(failures) or "arrival_authorization_changed")
                samples.append({
                    "sequence": observation.sequence,
                    "monotonic_t": observation.monotonic_t,
                    "position": _point_dict(observation.position),
                    "distance_to_final_m": _distance_m(observation.position, end),
                    "telemetry_age_seconds": observation.telemetry_age_seconds,
                    "session_id": observation.session_id,
                    "process_id": observation.process_id,
                })
                if not self._within_final_arrival_tolerance(observation.position, end):
                    self._set_state(AdapterState.FAILED, "post_arrival_position_left_final_radius")
                    break
        except AdapterError as exc:
            self._set_state(AdapterState.FAILED, "post_arrival_confirmation_failed:" + str(exc))
        self.timeline.append({"event": "post_arrival_confirmation", "samples": samples,
                              "confirmed": self.state == AdapterState.ARRIVED,
                              "required_advancing_samples": self.config.post_arrival_samples})

    def _within_final_arrival_tolerance(self, position: Vec3, target: Vec3) -> bool:
        """Apply the same horizontal/vertical arrival envelope as the follower."""

        return (
            _horizontal_distance_m(position, target) <= self.config.final_arrival_radius_m
            and abs(position.z - target.z) / 100.0 <= self.config.waypoint_vertical_tolerance_m
        )


def _point_dict(point: Vec3) -> dict[str, float]:
    return {"x": float(point.x), "y": float(point.y), "z": float(point.z)}


# The gimmick's own body (a ladder's frame beside its mount point) is not in
# the navmesh, so the walk onto its departure or off its landing can clip it
# (live at the Kalm inn cellar the leg to the ladder's foot crossed the frame
# 1 m from it). The crossing controller walks those last metres itself.
GIMMICK_CONTACT_EXEMPT_M = 1.5


def _gimmick_contact(plan: Any, hit: Mapping[str, Any]) -> bool:
    """Whether a collision hit is a gimmick's own body at the walk onto or off it."""
    segments = getattr(plan, "traversal_segments", ())
    location = hit.get("location")
    if not segments or not isinstance(location, Mapping) or not isinstance(hit.get("segment"), int):
        return False
    try:
        point = Vec3(float(location["x"]), float(location["y"]), float(location["z"]))
        located = locate_traversal_waypoints(plan.waypoints, segments)
    except (KeyError, TypeError, ValueError):
        return False
    for segment, index in located:
        if segment.get("link_class") not in GIMMICK_LINK_CLASSES:
            continue
        ends = {index - 1: plan.waypoints[index], index + 1: plan.waypoints[index + 1]}
        end = ends.get(int(hit["segment"]))
        if end is not None and _horizontal_distance_m(point, end) <= GIMMICK_CONTACT_EXEMPT_M:
            return True
    return False


def _traversal_leg_indices(plan: Any) -> set[int]:
    """The route legs (by index) that are off-mesh crossings."""
    segments = getattr(plan, "traversal_segments", ())
    if not segments:
        return set()
    try:
        return {index for _, index in locate_traversal_waypoints(plan.waypoints, segments)}
    except ValueError:
        return set()


def _gate_map_id(topology: Any) -> str:
    """The map a topology (or None) is of, which keys the gate registry."""
    return str(getattr(topology, "map_id", None) or getattr(topology, "world_id", None) or "")


def _parse_point(value: Any) -> Vec3:
    if not isinstance(value, Mapping):
        raise AdapterError("route_point_missing")
    try:
        point = Vec3(float(value["x"]), float(value["y"]), float(value["z"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise AdapterError("route_point_invalid") from exc
    if not _is_finite_point(point):
        raise AdapterError("route_point_non_finite")
    return point


def _is_finite_point(point: Any) -> bool:
    if isinstance(point, Vec3):
        values = (point.x, point.y, point.z)
    elif isinstance(point, Mapping):
        try:
            values = (point["x"], point["y"], point["z"])
        except (KeyError, TypeError):
            return False
    else:
        return False
    try:
        return all(math.isfinite(float(value)) for value in values)
    except (TypeError, ValueError):
        return False


def _distance_m(first: Vec3, second: Vec3) -> float:
    return math.sqrt((second.x - first.x) ** 2 + (second.y - first.y) ** 2 + (second.z - first.z) ** 2) / 100.0


def _horizontal_distance_m(first: Vec3, second: Vec3) -> float:
    return math.hypot(second.x - first.x, second.y - first.y) / 100.0


def straight_run_ahead_m(waypoints, position, max_turn_deg: float = SPRINT_MAX_TURN_DEG) -> float:
    """Horizontal length of route ahead of ``position`` before it turns.

    The position is projected onto the nearest route segment; from there the
    route is followed while each segment stays within ``max_turn_deg`` of the
    first one's heading.
    """
    points = [(float(p.x), float(p.y)) for p in waypoints]
    if len(points) < 2 or position is None:
        return 0.0
    px, py = float(position.x), float(position.y)
    best_index, best_t, best_d = 0, 0.0, math.inf
    for index in range(len(points) - 1):
        (ax, ay), (bx, by) = points[index], points[index + 1]
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
        d = math.hypot(ax + t * dx - px, ay + t * dy - py)
        if d < best_d:
            best_index, best_t, best_d = index, t, d
    heading = None
    total_cm = 0.0
    for index in range(best_index, len(points) - 1):
        (ax, ay), (bx, by) = points[index], points[index + 1]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length == 0:
            continue
        segment_heading = math.degrees(math.atan2(dy, dx))
        if heading is None:
            heading = segment_heading
        elif abs((segment_heading - heading + 180.0) % 360.0 - 180.0) > max_turn_deg:
            break
        total_cm += length * (1.0 - best_t) if index == best_index else length
    return total_cm / 100.0


def _segment_portal_intersection_xy(
    segment_start: Vec3,
    segment_end: Vec3,
    portal_start: Vec3,
    portal_end: Vec3,
    *,
    tolerance_cm: float = 5.0,
) -> tuple[float, float, tuple[float, float], str] | None:
    """Return a bounded XY segment/portal crossing, if one is proven.

    Exact proper/endpoint intersections are preferred.  A small endpoint
    tolerance handles telemetry landing just beside a portal without turning
    a near miss into a crossing: the closest pair must involve an endpoint of
    one of the two observed segments and remain within the explicit bound.
    """
    ax, ay = segment_start.x, segment_start.y
    bx, by = segment_end.x, segment_end.y
    cx, cy = portal_start.x, portal_start.y
    dx, dy = portal_end.x, portal_end.y
    r_x, r_y = bx - ax, by - ay
    s_x, s_y = dx - cx, dy - cy
    denominator = r_x * s_y - r_y * s_x
    if abs(denominator) > 1e-9:
        q_x, q_y = cx - ax, cy - ay
        segment_t = (q_x * s_y - q_y * s_x) / denominator
        portal_u = (q_x * r_y - q_y * r_x) / denominator
        if -1e-9 <= segment_t <= 1.0 + 1e-9 and -1e-9 <= portal_u <= 1.0 + 1e-9:
            segment_t = max(0.0, min(1.0, segment_t))
            portal_u = max(0.0, min(1.0, portal_u))
            return (
                segment_t,
                portal_u,
                (ax + segment_t * r_x, ay + segment_t * r_y),
                "exact_xy_intersection",
            )

    def closest_point_on_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> tuple[float, float, float]:
        vx, vy = x2 - x1, y2 - y1
        length_sq = vx * vx + vy * vy
        if length_sq <= 1e-9:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / length_sq))
        qx, qy = x1 + t * vx, y1 + t * vy
        return t, qx, qy

    endpoint_candidates = (
        ("segment", 0.0, *closest_point_on_segment(ax, ay, cx, cy, dx, dy)),
        ("segment", 1.0, *closest_point_on_segment(bx, by, cx, cy, dx, dy)),
        ("portal", 0.0, *closest_point_on_segment(cx, cy, ax, ay, bx, by)),
        ("portal", 1.0, *closest_point_on_segment(dx, dy, ax, ay, bx, by)),
    )
    best: tuple[float, float, float, tuple[float, float]] | None = None
    tolerance_sq = max(0.0, float(tolerance_cm)) ** 2
    for endpoint_kind, endpoint_parameter, projected_parameter, qx, qy in endpoint_candidates:
        if endpoint_kind == "segment":
            segment_t = endpoint_parameter
            portal_u = projected_parameter
            px, py = (ax, ay) if segment_t == 0.0 else (bx, by)
        else:
            portal_u = endpoint_parameter
            segment_t = projected_parameter
            px, py = (cx, cy) if portal_u == 0.0 else (dx, dy)
        distance_sq = (px - qx) ** 2 + (py - qy) ** 2
        if distance_sq <= tolerance_sq:
            candidate = (distance_sq, segment_t, portal_u, (qx, qy))
            if best is None or candidate < best:
                best = candidate
    if best is None:
        return None
    _, segment_t, portal_u, intersection = best
    return segment_t, portal_u, intersection, "endpoint_tolerance"


def _polygon_edge_clearance_m(point: Vec3, polygon: Any) -> float:
    best = math.inf
    vertices = tuple(polygon.vertices)
    for first, second in zip(vertices, vertices[1:] + vertices[:1]):
        dx, dy = second.x - first.x, second.y - first.y
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-9:
            distance = math.hypot(point.x - first.x, point.y - first.y)
        else:
            t = max(0.0, min(1.0, ((point.x - first.x) * dx + (point.y - first.y) * dy) / length_sq))
            distance = math.hypot(point.x - (first.x + t * dx), point.y - (first.y + t * dy))
        best = min(best, distance / 100.0)
    return best


def _normalize_degrees(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0


def _clamp_stick(x: float, y: float) -> tuple[float, float]:
    magnitude = math.hypot(x, y)
    if magnitude <= 1.0:
        return x, y
    return x / magnitude, y / magnitude


def world_heading_to_camera_stick(
    desired_world_heading_deg: float,
    camera_world_yaw_deg: float,
    magnitude: float,
) -> tuple[float, float]:
    """Convert Unreal world yaw to the existing camera-relative left stick.

    The verified backend convention is +X/right and +Y/forward in camera
    space.  Unreal yaw is +X=0 degrees, +Y=+90 degrees, positive to the right.
    """
    values = (desired_world_heading_deg, camera_world_yaw_deg, magnitude)
    if not all(math.isfinite(float(value)) for value in values):
        raise AdapterError("steering_non_finite")
    if not 0.0 <= float(magnitude) <= 1.0:
        raise AdapterError("steering_magnitude_out_of_bounds")
    relative = _normalize_degrees(float(desired_world_heading_deg) - float(camera_world_yaw_deg))
    x = math.sin(math.radians(relative)) * float(magnitude)
    y = math.cos(math.radians(relative)) * float(magnitude)
    return _clamp_stick(x, y)


def _plan_hash(plan: Mapping[str, Any]) -> str:
    encoded = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc_from_mtime(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _same_process_id(value: Any, expected: int | None) -> bool:
    if expected is None or value is None:
        return False
    try:
        return int(value) == int(expected)
    except (TypeError, ValueError):
        return False


def _render_evidence_confirms_gameplay(
    observation: LiveObservation,
    evidence: RenderedGameplayEvidence,
    *,
    max_telemetry_age_seconds: float = DEFAULT_MAX_TELEMETRY_AGE_SECONDS,
) -> bool:
    """Return true only for a bound raw or telemetry-backed gameplay proof.

    A fresh loaded-gameplay telemetry override may resolve the known raw
    classifier false-negative, but only for an explicitly unknown frame with
    all process, map, sequence, freshness, loaded-state, and nonblack checks.
    Semantic menu/title evidence remains non-authorizing; brightness-only title
    or menu candidates additionally require measured grounded locomotion.
    """
    if evidence.ready is not True:
        return False
    if evidence.capture.get("ok") is not True:
        return False
    if not _same_process_id(evidence.capture.get("pid"), observation.process_id):
        return False
    try:
        whole_nonblack = float(evidence.metrics.get("whole_nonblack"))
    except (TypeError, ValueError):
        return False
    if evidence.classifier_label == "gameplay":
        return math.isfinite(whole_nonblack) and whole_nonblack >= AgentServiceRenderEvidenceSource._MIN_WHOLE_NONBLACK
    return _telemetry_backed_render_evidence_confirms_gameplay(
        observation,
        evidence,
        max_telemetry_age_seconds=max_telemetry_age_seconds,
    )


# A frame darker than the usual .30 gate still shows the world when it has
# these: lit samples, luminance variation and edges (see the function below).
DIM_FRAME_LABELS = frozenset({"main_menu", "title_prompt", "unknown"})
DIM_FRAME_MIN_NONBLACK = 0.015
DIM_FRAME_MIN_STDDEV = 0.02
DIM_FRAME_MIN_EDGES = 0.006


def _render_whole_nonblack(evidence: RenderedGameplayEvidence) -> float:
    try:
        return float(evidence.metrics.get("whole_nonblack"))
    except (TypeError, ValueError):
        return math.nan


def _bound_render_has_nonblack_structure(evidence: RenderedGameplayEvidence) -> bool:
    """The independently bound frame must show the world, not a loading screen.

    The classifier recognises the loading screen by its widget, so darkness
    alone is not treated as loading. whole_nonblack measures samples brighter
    than RGB20, not literal black. Keep the normal .30 gate; only regional
    menu/title/unknown brightness candidates (dark scenery) may be darker, and
    then only a blank or flat frame is refused. The caller still verifies loaded,
    grounded, current process/map/sequence/age and non-menu telemetry.
    """
    try:
        if evidence.classifier_label == "loading":
            return False
        bright = float(evidence.metrics.get("whole_nonblack"))
        if not math.isfinite(bright) or not 0 <= bright <= 1:
            return False
        if bright >= AgentServiceRenderEvidenceSource._MIN_WHOLE_NONBLACK:
            return True
        frame = evidence.capture.get("startup_frame")
        if (evidence.classifier_label not in DIM_FRAME_LABELS
                or not isinstance(frame, Mapping)
                or frame.get("classification_basis") != "regional_brightness_heuristic"):
            return False
        variation = float(evidence.metrics.get("luminance_stddev"))
        edges = float(evidence.metrics.get("edge_fraction"))
        if not (math.isfinite(variation) and math.isfinite(edges) and variation <= 1 and edges <= 1):
            return False
        # Live 2026-10-14 the lamp-lit cellar under the Kalm inn measured
        # nonblack .13, stddev .055, edges .029; live 2026-10-21 a Mt. Nibel
        # cave above the second gas yard .020, .026, .007 (classifier: unknown).
        # A fade to black has almost no lit samples and no variation.
        return (bright >= DIM_FRAME_MIN_NONBLACK and variation >= DIM_FRAME_MIN_STDDEV
                and edges >= DIM_FRAME_MIN_EDGES)
    except (TypeError, ValueError, OverflowError):
        return False


def _telemetry_backed_render_evidence_confirms_gameplay(
    observation: LiveObservation,
    evidence: RenderedGameplayEvidence,
    *,
    max_telemetry_age_seconds: float = DEFAULT_MAX_TELEMETRY_AGE_SECONDS,
) -> bool:
    """Validate the explicit service proof for a raw unknown loaded frame."""
    if (
        evidence.ready is not True
        or evidence.reason != _TELEMETRY_BACKED_GAMEPLAY_REASON
    ):
        return False
    capture = evidence.capture
    frame = capture.get("startup_frame")
    if not isinstance(frame, Mapping):
        return False
    if (
        frame.get("label") != "gameplay"
        or frame.get("telemetry_override") != _TELEMETRY_BACKED_GAMEPLAY_OVERRIDE
    ):
        return False
    raw_label = frame.get("render_classifier_label")
    if raw_label != evidence.classifier_label:
        return False
    if raw_label != "unknown":
        # Regional brightness is an uncertain menu candidate: shaded scenery
        # produces the same pattern. Require synchronized measured grounded
        # locomotion in addition to all the loaded-state/capture gates below.
        if not (raw_label in {"main_menu", "title_prompt"}
            and frame.get("classification_basis") == "regional_brightness_heuristic"
            and getattr(observation, "position_reference", None) == "upright_capsule_bottom"
            and getattr(observation, "grounded", None) is True):
            return False
    elif _render_whole_nonblack(evidence) < AgentServiceRenderEvidenceSource._MIN_WHOLE_NONBLACK and not (
            getattr(observation, "position_reference", None) == "upright_capsule_bottom"
            and getattr(observation, "grounded", None) is True):
        # A dim unknown frame counts only with Cloud measured standing.
        return False
    # Attached sessions intentionally begin hashless.  The adapter performs
    # executable identity verification immediately after this render gate and
    # before topology/planning, so this proof must not make identity a hidden
    # prerequisite or it can never bridge the supported lazy-identity path.
    if not (
        getattr(observation, "telemetry_gameplay_ready", False) is True
        and getattr(observation, "process_valid", False) is True
        and getattr(observation, "controllable", False) is True
        and getattr(observation, "loading", False) is False
        and getattr(observation, "in_menu", False) is False
        and getattr(observation, "in_cutscene", False) is False
        and getattr(observation, "in_combat", False) is False
    ):
        return False
    capture_map_id = capture.get("map_id")
    observation_map_id = getattr(observation, "map_id", None)
    if not capture_map_id or not observation_map_id or str(capture_map_id) != str(observation_map_id):
        return False
    try:
        capture_sequence = int(capture.get("telemetry_sequence"))
        capture_age = float(capture.get("telemetry_age_seconds"))
        observation_age = float(getattr(observation, "telemetry_age_seconds", None))
        max_age = float(max_telemetry_age_seconds)
        hwnd = int(capture.get("hwnd"))
        whole_nonblack = float(evidence.metrics.get("whole_nonblack"))
    except (TypeError, ValueError, OverflowError):
        return False
    if capture_sequence < 0 or hwnd <= 0:
        return False
    if (
        not math.isfinite(max_age)
        or max_age <= 0.0
        or not math.isfinite(capture_age)
        or not math.isfinite(observation_age)
        or not math.isfinite(whole_nonblack)
        or capture_age < 0.0
        or observation_age < 0.0
        or not _bound_render_has_nonblack_structure(evidence)
        or capture_age > max(max_age, RENDER_CAPTURE_MAX_TELEMETRY_AGE_SECONDS)
        or observation_age > max_age
    ):
        return False
    return True


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _file_stat_identity(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False, "mtime_ns": None, "size": None}
    except OSError:
        return {"exists": False, "mtime_ns": None, "size": None}
    return {"exists": True, "mtime_ns": int(stat.st_mtime_ns), "size": int(stat.st_size)}


def _path_stat_identity_for_validator(path: Path) -> dict[str, Any]:
    return _strict_path_stat_identity(path)


def _strict_path_stat_identity(path: Path) -> dict[str, Any]:
    identity = {
        "path": str(path.resolve()),
        **_file_stat_identity(path),
    }
    if not identity.get("exists"):
        return identity
    try:
        stat = path.stat()
    except OSError:
        identity["exists"] = False
        return identity
    identity.update({
        "ctime_ns": int(getattr(stat, "st_ctime_ns", 0)),
        "inode": int(getattr(stat, "st_ino", 0)),
        "device": int(getattr(stat, "st_dev", 0)),
    })
    return identity


def _file_identity(path: Path) -> dict[str, Any]:
    identity = _file_stat_identity(path)
    if not identity.get("exists"):
        identity["sha256"] = None
        return identity
    try:
        identity["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        identity["exists"] = False
        identity["sha256"] = None
    return identity


def _file_stat_is_new(current: Mapping[str, Any], before: Mapping[str, Any]) -> bool:
    if current.get("exists") is not True:
        return False
    if before.get("exists") is not True:
        return True
    return (
        current.get("mtime_ns") != before.get("mtime_ns")
        or current.get("size") != before.get("size")
    )
