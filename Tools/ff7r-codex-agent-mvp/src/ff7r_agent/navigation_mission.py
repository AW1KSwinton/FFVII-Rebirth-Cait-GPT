"""Run a landmark objective through the existing singleton HTTP controller.

This is a client, never a second AgentService, broker, or movement backend.
Planning, independent validation, pulse bounds and control-loop safety remain
in LiveRouteAdapter. Human recordings are not route inputs.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid
from typing import Any, Awaitable, Callable

from .navigation_values import MissionError, finite_position, horizontal_distance
from .continuous_control import CONTINUOUS_PROFILE, FAST_FOLLOW_CAMERA_DEADBAND_DEG, FAST_FOLLOW_PROFILE, SPRINT_PROFILE
from .controller_policy import LIVE_CONTROLLER_POLICY_INPUT_FIELDS, NORMAL_06_TRIAL_PROFILE, NORMAL_08_TRIAL_PROFILE, PROVEN_LIVE_CONTROLLER_POLICY

CHADLEY_GOAL = "nibelheim-upper-square-chadley-20260811"
RPC = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
# The first status polls come quickly so a refused preflight or a dry run is
# collected without waiting a whole interval; after them, the configured
# poll_seconds holds.
POLL_RAMP_SECONDS = (0.25, 0.5, 1.0)
# How long one approach may reuse a full identity verification. Live, the
# longest route so far took 216 s; five minutes covers the next route while
# staying well inside the 600 s mission budget.
IDENTITY_REUSE_TTL_SECONDS = 300.0
# get_runtime_identity fields that pin the bridge, the worker, the loaded
# checkout and the deployment profile. A restart, worker reload, commit,
# dirty tree or profile change alters at least one of them.
IDENTITY_REUSE_FIELDS = (
    "pid", "worker_generation", "worker_pid", "source_root", "package_import_root", "loaded_package_path",
    "repository_root", "git_commit_sha", "source_commit_sha", "git_dirty", "git_dirty_paths",
    "deployment_profile", "expected_feature_ref", "expected_feature_commit", "bridge_identity",
    "bridge_source_binding", "runtime_profile_verification")
WORKER_REUSE_FIELDS = (
    "pid", "worker_generation", "worker_pid", "source_root", "loaded_package_path", "git_commit_sha",
    "source_commit_sha", "git_dirty", "git_dirty_paths", "deployment_profile")
BRIDGE_STATUS_REUSE_FIELDS = ("generation", "worker_pid", "started_at", "last_restart_at", "source_binding")
# Without these an identity pins nothing and is never reused.
IDENTITY_REUSE_REQUIRED = ("pid", "worker_pid", "git_commit_sha")


def unwrap(value: dict[str, Any]) -> dict[str, Any]:
    """Reject both transport errors and wrapped worker/service refusals."""
    if not isinstance(value, dict):
        raise MissionError("invalid_runtime_response")
    if value.get("ok") is False:
        raise MissionError(str(value.get("error") or value.get("decision") or value.get("reason") or "runtime_refused"), response=value)
    if "id" in value and "result" in value:
        return unwrap(value["result"])
    return value


def input_recovery_refused(exc: BaseException) -> bool:
    """A failed input/identity gate is not a recoverable route obstacle."""
    response = getattr(exc, "response", None)
    refused = isinstance(response, dict) and (response.get("recovery_refused") is True
        or response.get("error_type") or response.get("error") or response.get("decision"))
    return (not isinstance(exc, MissionError) or bool(refused)
            or str(exc).startswith(("runtime_identity_", "feature-validation ",
                "game_process_", "bound_game_process_", "player_identity_",
                "control_session_", "capture_process_", "executable_identity_",
                "loaded_gameplay_", "arrival_identity_", "arrival_game_process_",
                "arrival_capture_process_", "arrival_correction_session_",
                "arrival_telemetry_", "arrival_sequence_", "arrival_not_gameplay")))


def resolve_goal(context: dict[str, Any], goal_id: str) -> dict[str, Any]:
    matches = [item for item in context.get("known_landmarks", []) if item.get("landmark_id") == goal_id]
    if len(matches) != 1:
        raise MissionError("landmark_missing_or_ambiguous")
    goal = dict(matches[0])
    if not context.get("map_id") or goal.get("map_id") != context["map_id"]:
        raise MissionError("landmark_map_mismatch")
    goal["position"] = finite_position(goal.get("position"))
    return goal


def route_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """Count commands across route attempts, deduplicating repeated feedback."""
    attempts = result.get("route_attempts") or [{"run": result.get("run") or {}, "preflight": result.get("preflight") or {}}]
    observed, cycles, events, pulses = {}, 0, [], []
    for index, attempt in enumerate(attempts):
        run = attempt.get("run") or {}
        cycles += int(run.get("movement_cycles") or 0)
        timeline = run.get("timeline") or []
        events.extend(timeline)
        pulses.extend(e for e in timeline if e.get("event") in {"movement_pulse_completed", "movement_setpoint_issued"})
        for event in timeline:
            if event.get("event") == "post_command_observation":
                # The controller can reacquire feedback for the same command.
                # Keep its latest advancing endpoint, not a second distance.
                key = (index, int(event["command_sequence"]))
                old = observed.get(key)
                if old is None or int(event.get("sequence", -1)) > int(old.get("sequence", -1)):
                    observed[key] = event
    distances = [horizontal_distance(finite_position(e["position_before"]), finite_position(e["position_after"])) for e in observed.values()]
    gaps = [float(b["t"])-float(a["t"]) for a,b in zip(pulses,pulses[1:]) if "t" in a and "t" in b]
    first_plan = (attempts[0].get("preflight") or {}).get("route_plan") or {}
    return {
        "route_attempts": len(attempts), "movement_cycles": cycles,
        "supervised_setpoints": sum(e.get("event") == "movement_setpoint_issued" for e in pulses),
        "completed_pulses": sum(e.get("event") == "movement_pulse_completed" for e in pulses),
        "sprint_setpoints": sum(bool((e.get("supervision") or {}).get("sprint")) for e in pulses),
        "sprint_braking_samples": sum(e.get("event") == "sprint_braking_sample" for e in events),
        "observed_movement_cycles": len(observed),
        "cumulative_horizontal_distance_m": sum(distances),
        "distance_sampling_complete": len(observed) == cycles,
        "mean_command_gap_seconds": sum(gaps)/len(gaps) if gaps else None,
        "maximum_command_gap_seconds": max(gaps, default=None),
        "maximum_cross_track_m": max((float(e.get("route_metrics", {}).get("cross_track_m", 0)) for e in events if isinstance(e.get("route_metrics"), dict)), default=0.0),
        "planned_distance_m": first_plan.get("total_estimated_path_length_m"),
        "route_plan_sha256": (result.get("run") or {}).get("route_plan_sha256"),
    }


def lifecycle_binding(value: dict[str, Any]) -> dict[str, Any]:
    context = value.get("run_context") or {}
    if not isinstance(context.get("pid"), int) or context["pid"] <= 0 or not context.get("run_id"):
        raise MissionError("bound_game_process_missing")
    return {key: context.get(key) for key in ("pid", "run_id", "executable_sha256")}


def verify_arrival(observations: list[dict[str, Any]], goal: dict[str, Any], start: dict[str, Any]) -> dict[str, Any]:
    """Require two advancing live samples at the goal in the same actor/map."""
    if len(observations) < 2:
        raise MissionError("arrival_samples_missing")
    previous = -1
    distances = []
    for state in observations:
        if not state.get("telemetry", {}).get("live") or not state.get("running"):
            raise MissionError("arrival_telemetry_stale")
        if any(state.get(key) for key in ("paused", "in_menu", "in_combat")) or (state.get("player", {}).get("locomotion") or {}).get("grounded") is False:
            raise MissionError("arrival_not_gameplay")
        if state.get("map_id") != goal["map_id"] or state.get("player", {}).get("actor_id") != start.get("player", {}).get("actor_id"):
            raise MissionError("arrival_identity_changed")
        sequence = int(state.get("sequence", -1))
        if sequence <= previous or sequence <= int(start.get("sequence", -1)):
            raise MissionError("arrival_sequence_not_advancing")
        previous = sequence
        position = finite_position(state.get("player", {}).get("position"))
        distance = horizontal_distance(position, goal["position"])
        vertical = abs(position["z"] - goal["position"]["z"]) / 100.0
        distances.append({"sequence": sequence, "horizontal_distance_m": distance, "vertical_distance_m": vertical})
    # Validate identity/readiness for every sample before classifying a purely
    # geometric miss as eligible for a bounded final correction.
    if any(d["horizontal_distance_m"] > PROVEN_LIVE_CONTROLLER_POLICY["final_arrival_radius_m"]
           or d["vertical_distance_m"] > 1.5 for d in distances):
        raise MissionError("arrival_outside_goal_tolerance")
    return {"confirmed": True, "samples": distances}


def identity_fingerprint(identity: Any) -> str | None:
    """SHA-256 of the identity fields that pin bridge, worker and checkout."""
    if not isinstance(identity, dict) or any(identity.get(key) is None for key in IDENTITY_REUSE_REQUIRED):
        return None
    worker = identity.get("worker_identity") if isinstance(identity.get("worker_identity"), dict) else {}
    status = identity.get("bridge_status") if isinstance(identity.get("bridge_status"), dict) else {}
    projection = {key: identity.get(key) for key in IDENTITY_REUSE_FIELDS}
    projection["worker_identity"] = {key: worker.get(key) for key in WORKER_REUSE_FIELDS}
    projection["bridge_status"] = {key: status.get(key) for key in BRIDGE_STATUS_REUSE_FIELDS}
    encoded = json.dumps(projection, sort_keys=True, allow_nan=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class VerifiedIdentityCache:
    """Carry one approach's runtime verification across its consecutive routes.

    Callable like the identity check it wraps. The first mission verifies in
    full (``get_bootstrap_verification`` and the check). A later mission
    within ``ttl_seconds`` skips both only when its own fresh
    ``get_runtime_identity`` pins the same bridge, worker, checkout and
    profile, and its own fresh lifecycle binds the same game run. Any
    mismatch forgets the verification, so the next mission verifies in full.
    """

    def __init__(self, check: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]], *,
                 ttl_seconds: float = IDENTITY_REUSE_TTL_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be finite and positive")
        self.check, self.ttl_seconds, self.clock = check, float(ttl_seconds), clock
        self._verified: dict[str, Any] | None = None

    def __call__(self, identity: dict[str, Any], bootstrap: dict[str, Any]) -> dict[str, Any]:
        return self.check(identity, bootstrap)

    def remember(self, identity: dict[str, Any], binding: dict[str, Any], checked: dict[str, Any], source: str) -> None:
        fingerprint = identity_fingerprint(identity)
        if not isinstance(checked, dict) or checked.get("ok") is not True or fingerprint is None:
            self._verified = None
            return
        self._verified = {"fingerprint": fingerprint, "binding": dict(binding), "at": self.clock(),
                          "source": source, "checked": checked}

    def reuse(self, identity: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any] | None:
        verified = self._verified
        if verified is None:
            return None
        age = self.clock() - verified["at"]
        fingerprint = identity_fingerprint(identity)
        if not 0 <= age <= self.ttl_seconds or fingerprint != verified["fingerprint"] or binding != verified["binding"]:
            self._verified = None
            return None
        return {"ok": True, "reused": True, "verified_by": verified["source"],
                "verified_age_seconds": round(age, 3), "ttl_seconds": self.ttl_seconds,
                "identity_fingerprint_sha256": fingerprint, "game_binding": dict(binding),
                "verification": verified["checked"]}


@dataclass(frozen=True)
class MissionConfig:
    goal_id: str = CHADLEY_GOAL
    execute: bool = False
    timeout_seconds: float = 900.0
    max_cycles: int = 2400
    normal_stick_magnitude: float = 1.0
    controller_mode: str = "pulse"
    sprint: bool = False
    camera_follow_enabled: bool = False
    # Turn the camera smoothly while moving instead of stopping to snap it.
    camera_follow_smooth: bool = False
    camera_follow_arrival_target: tuple[float, float, float] | None = None
    poll_seconds: float = 2.0
    # NPC handoffs use fresh lifecycle/arrival proofs; keep optional visual
    # evidence on standalone missions without delaying each NPC conversation.
    capture_evidence: bool = True
    target_position: tuple[float, float, float] | None = None
    target_map_id: str | None = None
    topology_cache_manifest: str | None = None
    map_target_id: str | None = None
    # Feature-only full-stick continuous travel (continuous_fast_follow_trial).
    fast_follow: bool = False
    # Feature-only streamed pure-pursuit following (see LiveAdapterConfig.pursuit).
    pursuit: bool = False
    # Caller-reported attribution for a newly opened lease; never inferred.
    model_id: str | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if type(self.capture_evidence) is not bool:
            raise ValueError("capture_evidence must be boolean")
        if (self.model_id is None) != (self.reasoning_effort is None):
            raise ValueError("model_id and reasoning_effort must be supplied together")
        if self.model_id is not None and any(not isinstance(v, str) or not v.strip()
                                             for v in (self.model_id, self.reasoning_effort)):
            raise ValueError("model_id and reasoning_effort must be nonempty strings")
        if type(self.camera_follow_enabled) is not bool:
            raise ValueError("camera_follow_enabled must be boolean")
        if type(self.camera_follow_smooth) is not bool or (self.camera_follow_smooth and not self.camera_follow_enabled):
            raise ValueError("camera_follow_smooth must be boolean and needs camera_follow_enabled")
        if self.camera_follow_arrival_target is not None:
            if (not self.camera_follow_enabled or len(self.camera_follow_arrival_target) != 3
                    or not all(math.isfinite(v) for v in self.camera_follow_arrival_target)):
                raise ValueError("camera arrival target needs camera follow and three finite coordinates")
        if type(self.sprint) is not bool or (self.sprint and self.controller_mode != "continuous"):
            raise ValueError("sprint requires continuous mode and a boolean flag")
        if self.controller_mode not in {"pulse", "continuous"}:
            raise ValueError("controller_mode must be pulse or continuous")
        if type(self.fast_follow) is not bool:
            raise ValueError("fast_follow must be boolean")
        if self.fast_follow and (self.controller_mode != "continuous" or self.sprint or self.normal_stick_magnitude != 1.0):
            raise ValueError("fast_follow requires continuous mode at normal_stick_magnitude 1.0 without sprint")
        if self.controller_mode == "continuous" and not self.fast_follow and self.normal_stick_magnitude != 0.8:
            raise ValueError("continuous trial requires normal_stick_magnitude 0.8")
        if self.normal_stick_magnitude not in (0.6, 0.8, 1.0):
            raise ValueError("normal_stick_magnitude must be 0.6, 0.8, or 1.0")
        if self.target_position is not None:
            if len(self.target_position) != 3 or not all(math.isfinite(v) for v in self.target_position):
                raise ValueError("target_position requires three finite coordinates")
            if not self.target_map_id:
                raise ValueError("target_map_id is required for a coordinate goal")
        if self.map_target_id is not None and (self.target_position is not None or self.target_map_id):
            raise ValueError("map_target_id cannot be combined with a coordinate goal")
        if not self.goal_id.strip():
            raise ValueError("goal_id is required")
        if not math.isfinite(self.timeout_seconds) or not 10 <= self.timeout_seconds <= 1500:
            raise ValueError("timeout_seconds must be between 10 and 1500")
        if not 1 <= self.max_cycles <= 2400:
            raise ValueError("max_cycles must be between 1 and 2400")
        if not math.isfinite(self.poll_seconds) or not 0.05 <= self.poll_seconds <= 10:
            raise ValueError("poll_seconds must be between 0.05 and 10")


class NavigationMission:
    mission_kind = "landmark_navigation_mission"

    def __init__(self, rpc: RPC, output: Path, *, identity_check: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]], config: MissionConfig = MissionConfig(), clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, progress: Callable[[str], None] = print, client_id: str | None = None) -> None:
        self.rpc, self.output, self.identity_check = rpc, Path(output), identity_check
        self.config, self.clock, self.sleep, self.progress = config, clock, sleep, progress
        self.sequence = 0
        self.session_owned = False
        self.session_pending = False
        # A control session is open for the life of the game and is not this
        # mission's to close: closing it latches the stop, and the next
        # action, the cutscene watcher included, is then refused (#193). One
        # already open is borrowed and left open. A caller holding it under
        # ``client_id`` (an NPC approach across its routes) has lent it
        # (adopted); any other client's is borrowed. Only a session this
        # mission opened itself is closed at the end.
        self.session_adopted = False
        self.session_borrowed = False
        self.client_id = client_id or uuid.uuid4().hex
        # The client the session in use is held under: this mission's own
        # only when it opened the session or was lent it.
        self.session_client_id = self.client_id
        self.deadline = math.inf
        self.job_id: str | None = None
        self.job_pending = False
        self.digests: dict[str, str] = {}

    def save(self, name: str, value: Any) -> None:
        # Bytes identical to json.dump(indent=2) through a text-mode stream,
        # but encoded in one pass: streaming a multi-megabyte final route
        # status took ~2.8 s live. Hashing the written bytes here spares
        # sha256.json a read-back of every file.
        data = (json.dumps(value, indent=2, allow_nan=False) + "\n").replace("\n", os.linesep).encode("utf-8")
        with (self.output / name).open("xb") as stream:
            stream.write(data)
        self.digests[name] = hashlib.sha256(data).hexdigest()

    def remaining(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise MissionError("mission_time_budget_exhausted")
        return remaining

    async def call(self, operation: str, arguments: dict[str, Any] | None = None, *, cleanup: bool = False) -> dict[str, Any]:
        timeout = 30.0 if cleanup else min(30.0, self.remaining())
        self.sequence += 1
        try:
            value = await asyncio.wait_for(self.rpc(operation, arguments or {}), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise MissionError("cleanup_rpc_timeout" if cleanup else "mission_rpc_timeout:"+operation) from exc
        self.save(f"{self.sequence:03d}-{operation}.json", value)
        # A timed-out mutation is never replayed. Ownership is reconciled in
        # finally, even if its response arrived after the logical deadline.
        if not cleanup:
            self.remaining()
        return unwrap(value)

    async def run_route_job(self, arguments: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        """Submit once, poll locally, and retain uncertain-start cleanup semantics."""
        self.job_pending = True
        job = await self.call("start_live_route_adapter", arguments)
        self.job_id = job["job_id"]
        report["job_id"] = self.job_id
        report.setdefault("job_ids", []).append(self.job_id)
        report["movement_attempted"] = self.config.execute
        last_progress = self.clock()
        polls = 0
        while job.get("state") == "running":
            interval = self.config.poll_seconds
            if polls < len(POLL_RAMP_SECONDS):
                interval = min(interval, POLL_RAMP_SECONDS[polls])
            polls += 1
            await self.sleep(min(interval, self.remaining()))
            job = await self.call("get_live_route_adapter_status", {"job_id": self.job_id})
            if self.clock() - last_progress >= 30:
                elapsed = self.config.timeout_seconds - self.remaining()
                self.progress(f"Navigation job {self.job_id} is running ({elapsed:.0f}s elapsed).")
                last_progress = self.clock()
        self.job_pending = False
        if job.get("state") != "completed":
            raise MissionError(f"navigation_job_{job.get('state')}")
        return unwrap(job.get("response", {}))

    async def run(self) -> dict[str, Any]:
        # Exclusive output directory: never overwrite another run's evidence.
        self.output.mkdir(parents=True, exist_ok=False)
        started = self.clock()
        self.deadline = started + self.config.timeout_seconds
        report: dict[str, Any] = {"schema_version": 1, "kind": self.mission_kind, "goal_id": self.config.goal_id, "dry_run": not self.config.execute, "ok": False, "state": "FAILED", "movement_attempted": False, "started_at_utc": datetime.now(timezone.utc).isoformat(), "output_directory": str(self.output.resolve())}
        navigation_entered = False
        try:
            # A live operation first: it lets a bridge left on an older commit
            # rebind to this checkout before its identity is read (live
            # 2026-09-30: a commit made between routes failed the next one as
            # runtime_identity_mismatch), and binds the game process that is
            # actually running (live 2026-10-05: a restarted game kept the
            # previous day's PID until the window capture disagreed).
            await self.call("refresh_lifecycle_attachment")
            identity = await self.call("get_runtime_identity")
            lifecycle = await self.call("get_lifecycle_status")
            health = await self.call("get_session_health")
            start = await self.call("observe_game")
            checked = await self.verify_identity(identity, lifecycle, report)
            if not checked.get("ok"):
                raise MissionError("runtime_identity_mismatch")
            if self.config.normal_stick_magnitude in (0.6, 0.8) and identity.get("deployment_profile") != "FEATURE_VALIDATION":
                raise MissionError("live_controller_trial_requires_feature_runtime")
            if not health.get("process_exists"):
                raise MissionError("loaded_gameplay_required")
            # Bootstrap and identity inspection can outlive a telemetry sample.
            # Rebuild the existing multi-sample proof locally before opening a
            # control session instead of requiring another LLM submission.
            if self.remaining() < 1.0:
                raise MissionError("mission_time_budget_exhausted")
            readiness = await self.call("wait_for_settled_gameplay", {
                "timeout_seconds": min(10.0, self.remaining()), "max_telemetry_age_seconds": 0.5})
            if readiness.get("ok") is not True:
                raise MissionError("loaded_gameplay_readiness_not_established")
            health = await self.call("get_session_health")
            start = await self.call("observe_game")
            if not health.get("process_exists") or not health.get("telemetry_live") or not start.get("running"):
                raise MissionError("loaded_gameplay_required")
            if lifecycle_binding(await self.call("get_lifecycle_status")) != lifecycle_binding(lifecycle):
                raise MissionError("game_process_changed_during_setup")
            if not start.get("map_id") or not start.get("player", {}).get("actor_id"):
                raise MissionError("player_identity_missing")
            self.remaining()
            binding = lifecycle_binding(lifecycle)
            report["game_binding"] = binding
            report["start"] = start
            report["runtime_identity"] = {key: identity.get(key) for key in ("git_commit_sha", "source_root", "worker_generation", "worker_pid")}
            goal = await self.resolve_mission_goal(start, report)
            report["goal"] = goal
            start_position = finite_position(start.get("player", {}).get("position"))
            report["start_distance_to_goal_m"] = horizontal_distance(start_position, goal["position"])
            # An open session is used, never replaced or closed: it is the
            # agent's for the life of the game, and it can belong to a
            # different task even though all tasks use the same runtime owner
            # string. It must be for this game.
            lease = await self.call("get_control_session_status")
            if lease.get("active"):
                if lease.get("game_pid") not in (None, binding["pid"]):
                    raise MissionError("control_session_game_mismatch")
                self.session_client_id = lease.get("client_id")
                if lease.get("client_id") == self.client_id:
                    self.session_owned = self.session_adopted = True
                    report["control_session"] = "adopted"
                else:
                    self.session_borrowed = True
                    report["control_session"] = "borrowed"
            else:
                await self.call("close_control_session")
                self.session_pending = True
                # `seconds` is ignored: the session lasts as long as the game.
                session = await self.call("open_control_session", {"seconds": 1800, "game_pid": binding["pid"], "client_id": self.client_id,
                    "model_id": self.config.model_id, "reasoning_effort": self.config.reasoning_effort})
                self.session_owned = session.get("client_id") == self.client_id
                self.session_pending = False
                if not self.session_owned:
                    raise MissionError("control_session_identity_mismatch")
                if not session.get("active"):
                    raise MissionError("control_session_not_open")
                report["control_session"] = "opened"
            if self.config.capture_evidence:
                report["before_capture"] = await self.call("capture_game_window")
                if report["before_capture"].get("pid") != binding["pid"]:
                    raise MissionError("capture_process_mismatch")
            if binding["executable_sha256"] is None:
                # Attached processes acquire their executable digest lazily.
                # Establish that proof before movement and bind to it, rather
                # than later mistaking its enrichment for a process restart.
                verified = await self.call("verify_executable_identity")
                if verified.get("verified") is not True or not verified.get("sha256"):
                    raise MissionError("executable_identity_not_verified")
                bound = lifecycle_binding(await self.call("get_lifecycle_status"))
                if any(bound[key] != binding[key] for key in ("pid", "run_id")):
                    raise MissionError("game_process_changed_during_identity_verification")
                if bound["executable_sha256"] != verified["sha256"]:
                    raise MissionError("executable_identity_binding_mismatch")
                binding = bound
                report["game_binding"] = binding
            policy = {field: PROVEN_LIVE_CONTROLLER_POLICY[field] for field in LIVE_CONTROLLER_POLICY_INPUT_FIELDS}
            arguments = {"dry_run": not self.config.execute, "route_mode": "cached_topology", "route_target_position": goal["position"], "max_cycles": self.config.max_cycles if self.config.execute else 1, "cached_topology_wall_clock_seconds": min(600.0, self.remaining()), **policy}
            policy["normal_stick_magnitude"] = self.config.normal_stick_magnitude
            arguments.update(policy)
            if self.config.normal_stick_magnitude == 0.6:
                arguments["controller_policy_profile"] = NORMAL_06_TRIAL_PROFILE
            elif self.config.normal_stick_magnitude == 0.8:
                arguments["controller_policy_profile"] = NORMAL_08_TRIAL_PROFILE
            arguments["controller_mode"] = self.config.controller_mode
            if self.config.controller_mode == "continuous":
                arguments["controller_policy_profile"] = SPRINT_PROFILE if self.config.sprint else FAST_FOLLOW_PROFILE if self.config.fast_follow else CONTINUOUS_PROFILE
            arguments["sprint"] = self.config.sprint
            arguments["camera_follow_enabled"] = self.config.camera_follow_enabled
            if self.config.camera_follow_arrival_target is not None:
                arguments["camera_follow_arrival_target"] = self.config.camera_follow_arrival_target
            if self.config.camera_follow_smooth:
                # Sent only when opted in, so default routes keep their exact
                # argument set.
                arguments["camera_follow_smooth"] = True
            if self.config.pursuit:
                arguments["pursuit"] = True
            if self.config.fast_follow:
                # Sent only when opted in, so default routes keep their exact
                # argument set.
                arguments["continuous_fast_follow"] = True
                if self.config.camera_follow_enabled:
                    arguments["camera_follow_deadband_deg"] = FAST_FOLLOW_CAMERA_DEADBAND_DEG
            report["controller_policy"] = {**policy, "sprint": self.config.sprint, "sprint_binding": "held_rt" if self.config.sprint else None, "sprint_stick_magnitude": 1.0 if self.config.sprint else None, "controller_mode": self.config.controller_mode, "fast_follow": self.config.fast_follow, "profile": arguments.get("controller_policy_profile", "proven")}
            if self.config.topology_cache_manifest:
                arguments["topology_cache_manifest_path"] = self.config.topology_cache_manifest
            navigation_entered = True
            await self.navigate_objective(arguments, report, goal, start, binding)
        except (Exception, asyncio.CancelledError) as exc:
            report.update(ok=False, state="STOPPED", reason=str(exc) or type(exc).__name__,
                          recovery_refused=not navigation_entered or input_recovery_refused(exc))
        finally:
            cleanup = await self.cleanup_control_session(report)
            report["cleanup"] = cleanup
            if report.get("state") == "CLEANUP_FAILED":
                report["recovery_refused"] = True
            report["elapsed_seconds"] = self.clock() - started
            self.save("result.json", report)
            hashes = {path.name: self.digests.get(path.name) or hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in sorted(self.output.glob("*.json"))}
            self.save("sha256.json", hashes)
        return report

    async def verify_identity(self, identity: dict[str, Any], lifecycle: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        """Check the runtime identity, or reuse this approach's recent check.

        Reuse needs a ``VerifiedIdentityCache`` identity check and is decided
        on this mission's own fresh identity and lifecycle reads. Otherwise
        the bootstrap verification and the full check run as always.
        """
        cache = self.identity_check if isinstance(self.identity_check, VerifiedIdentityCache) else None
        try:
            binding = lifecycle_binding(lifecycle)
        except MissionError:
            binding = None  # the setup gates below refuse this game
        checked = cache.reuse(identity, binding) if cache is not None and binding is not None else None
        report["identity_verification"] = "full" if checked is None else "reused"
        if checked is None:
            bootstrap = await self.call("get_bootstrap_verification")
            checked = self.identity_check(identity, bootstrap)
            if cache is not None and binding is not None:
                cache.remember(identity, binding, checked, str(self.output))
        self.save("identity-check.json", checked)
        return checked

    async def cleanup_control_session(self, report):
        cleanup = {}
        if self.session_pending:
            try:
                lease = await self.call("get_control_session_status", cleanup=True)
                self.session_owned = lease.get("client_id") == self.client_id
                cleanup["session_reconciliation"] = {"owned": self.session_owned, "active": lease.get("active")}
            except Exception as exc:
                cleanup["session_reconciliation"] = {"ok": False, "error": str(exc)}
                report.update(ok=False, state="CLEANUP_FAILED")
        if self.session_owned or self.session_borrowed:
            # A session this mission opened is closed with it. One it was
            # lent or merely borrowed goes back open: the mission's routes
            # end with input released, so nothing is stopped unless input
            # may still be in flight, and only then is it an emergency stop,
            # which closes the session and says so.
            lent = self.session_adopted or self.session_borrowed
            operations = []
            if not lent or self.input_may_be_in_flight(report):
                operations.append(("stop_all_input", None))
            if not lent:
                operations.append(("close_control_session", {"client_id": self.client_id}))
            cleanup["session"] = "left_open" if lent and not operations else "closed"
            for operation, arguments in operations:
                try:
                    cleanup[operation] = await self.call(operation, arguments, cleanup=True)
                except Exception as exc:
                    cleanup[operation] = {"ok": False, "error": str(exc)}
                    report.update(ok=False, state="CLEANUP_FAILED")
        return cleanup

    def input_may_be_in_flight(self, report) -> bool:
        """A route job not known to have finished, or a request that timed out."""
        reason = str(report.get("reason") or "")
        return bool(self.job_pending or reason.startswith(("mission_rpc_timeout", "cleanup_rpc_timeout"))
                    or (report.get("interaction_attempted") and report.get("interaction_sent") is None))

    async def resolve_mission_goal(self, start, report):
        if self.config.map_target_id is not None:
            target = (await self.call("get_map_target", {"target_id": self.config.map_target_id}))["target"]
            if target.get("map_id") != start["map_id"]:
                raise MissionError("map_target_map_mismatch")
            if target.get("position", {}).get("z") is None:
                raise MissionError("map_target_z_unresolved")
            report.update(goal_id=None, map_target=target)
            return {"landmark_id": None, "label": target.get("label") or "map target",
                    "map_id": target["map_id"], "position": finite_position(target["position"])}
        if self.config.target_position is None:
            goal = resolve_goal(await self.call("get_navigation_context"), self.config.goal_id)
        else:
            if self.config.target_map_id != start["map_id"]:
                raise MissionError("target_map_mismatch")
            goal = {"landmark_id": None, "label": "coordinate goal", "map_id": self.config.target_map_id,
                    "position": dict(zip(("x", "y", "z"), self.config.target_position))}
            report["goal_id"] = None
        return goal

    async def navigate_objective(self, arguments, report, goal, start, binding):
        self.progress(f"Planning navmesh route to {goal.get('label', self.config.goal_id)}.")
        # Never replay a start whose response was lost. Cleanup stops the
        # worker if a network error leaves the outcome uncertain.
        result = await self.run_route_job(arguments, report)
        preflight = result.get("preflight") or {}
        run = result.get("run") or {}
        report["metrics"] = route_metrics(result)
        report["preflight_state"] = preflight.get("state")
        report["route_state"] = run.get("state")
        if not preflight.get("movement_authorized"):
            raise MissionError(str(preflight.get("reason") or "route_preflight_refused"))
        if not self.config.execute:
            if int(run.get("movement_cycles", 0)) != 0:
                raise MissionError("dry_run_sent_movement")
            if run.get("reason") != "control_cycle_bound_reached":
                raise MissionError(str(run.get("reason") or "dry_run_failed"))
            report.update(ok=True, state="PREFLIGHT_PASSED")
        else:
            if run.get("state") != "ARRIVED":
                raise MissionError(str(run.get("reason") or "navigation_not_arrived"))
            attempts = list(result.get("route_attempts") or [{"run": run, "preflight": preflight}])
            for correction_index in range(2):
                samples = []
                for _ in range(2):
                    await self.sleep(0.3)
                    samples.append(await self.call("observe_game"))
                end_binding = lifecycle_binding(await self.call("get_lifecycle_status"))
                if end_binding != binding:
                    raise MissionError("arrival_game_process_changed")
                try:
                    report["arrival"] = verify_arrival(samples, goal, start)
                except MissionError as exc:
                    if correction_index or str(exc) != "arrival_outside_goal_tolerance":
                        raise
                    positions = [finite_position(sample["player"]["position"]) for sample in samples]
                    radius = PROVEN_LIVE_CONTROLLER_POLICY["final_arrival_radius_m"]
                    if any(horizontal_distance(position, goal["position"]) > 2 * radius
                           or abs(position["z"] - goal["position"]["z"]) / 100.0 > 1.5
                           for position in positions):
                        raise
                    remaining_cycles = self.config.max_cycles - report["metrics"]["movement_cycles"]
                    if remaining_cycles <= 0 or self.remaining() < 1.0:
                        raise
                    lease = await self.call("get_control_session_status")
                    if not lease.get("active") or lease.get("client_id") != self.session_client_id:
                        self.session_owned = self.session_borrowed = False
                        raise MissionError("arrival_correction_session_changed")
                    correction_arguments = {**arguments, "max_cycles": min(32, remaining_cycles),
                        "cached_topology_wall_clock_seconds": min(60.0, self.remaining())}
                    report["arrival_correction"] = {"attempts": 1,
                        "reason": "post_arrival_position_drift", "max_cycles": correction_arguments["max_cycles"],
                        "distance_before_m": horizontal_distance(positions[-1], goal["position"])}
                    self.progress("Correcting a small post-arrival drift through a fresh validated route.")
                    result = await self.run_route_job(correction_arguments, report)
                    preflight, run = result.get("preflight") or {}, result.get("run") or {}
                    attempts.extend(result.get("route_attempts") or [{"run": run, "preflight": preflight}])
                    report["metrics"] = route_metrics({"route_attempts": attempts, "run": run})
                    report["preflight_state"], report["route_state"] = preflight.get("state"), run.get("state")
                    if not preflight.get("movement_authorized"):
                        raise MissionError(str(preflight.get("reason") or "arrival_correction_preflight_refused"))
                    if run.get("state") != "ARRIVED":
                        raise MissionError(str(run.get("reason") or "arrival_correction_not_arrived"))
                    continue
                report["end"] = samples[-1]
                break
            if self.config.capture_evidence:
                report["after_capture"] = await self.call("capture_game_window")
                if report["after_capture"].get("pid") != binding["pid"]:
                    raise MissionError("arrival_capture_process_changed")
            report.update(ok=True, state="ARRIVED")


def canonical_identity_check(manifest_path: Path) -> Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]:
    from .doctor import build_report

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    def check(identity: dict[str, Any], bootstrap: dict[str, Any]) -> dict[str, Any]:
        return build_report(repository_root=Path(manifest["canonical_source"]["repository_root"]), manifest_path=manifest_path, runtime_python=Path(manifest["installed_editable_runtime"]["python"]), runtime_identity=identity.get("worker_identity") or {}, connected_identity=identity, ownership_inventory=bootstrap.get("runtime_ownership", {}).get("inventory"))

    return check


def feature_identity_check(repository_root: Path, expected_ref: str, expected_commit: str):
    from .runtime_profile import validate_checkout, verify_runtime_identity
    def check(identity, bootstrap):
        contract = validate_checkout(profile="FEATURE_VALIDATION", repository_root=repository_root,
            expected_commit_sha=expected_commit, expected_ref=expected_ref)
        # After the bridge rebinds to a newer commit, its own fields keep the
        # binding it started with and the process that runs operations is
        # reported as active_runtime_identity: verify that one (live
        # 2026-10-05, every route after a rebind was refused as
        # runtime_identity_mismatch until the server was restarted by hand).
        active = identity.get("active_runtime_identity") or identity
        checks = [verify_runtime_identity(value, contract=contract, worker_identity=value.get("worker_identity"))
                  for value in (active, bootstrap.get("runtime_identity") or {})]
        return {"ok": bootstrap.get("ok") is True and all(item["ok"] for item in checks),
                "profile": "FEATURE_VALIDATION", "checks": checks}
    return check


async def run_http_mission(config: MissionConfig, output: Path, manifest_path: Path, *, identity_check=None, mission_class=NavigationMission) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client("http://127.0.0.1:8765/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def rpc(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
                direct = operation in {"get_runtime_identity", "get_bootstrap_verification"}
                response = await session.call_tool(operation if direct else "agent_call", {} if direct else {"operation": operation, "arguments": arguments})
                if response.isError:
                    raise MissionError(str(response.content))
                value = response.structuredContent
                if value is None:
                    value = json.loads(next(item.text for item in response.content if item.type == "text"))
                return value

            return await mission_class(rpc, output, identity_check=identity_check or canonical_identity_check(manifest_path), config=config).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", help="Exact client-reported model for a new control lease.")
    parser.add_argument("--reasoning-effort", help="Exact client-reported effort; requires --model-id.")
    parser.add_argument("--goal-id", default=CHADLEY_GOAL)
    parser.add_argument("--poi-id", help="Resolve a MapIconInfo placement and interact with its live entity.")
    parser.add_argument("--actor-name", help="Exact runtime semantic actor name for POI handoff.")
    parser.add_argument("--target-id", help="Resolve an exact live entity and interact once.")
    parser.add_argument("--target-kind", help="Resolve one unambiguous live kind and interact once.")
    parser.add_argument("--execute", action="store_true", help="Send bounded movement; default only validates the route.")
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--max-cycles", type=int, default=2400)
    parser.add_argument("--normal-stick-magnitude", type=float, choices=(0.6, 0.8, 1.0), default=1.0, help="Normal travel intensity; default 1.0. The 0.6 and 0.8 settings are feature-only trials. Approach/final controls stay at 0.5.")
    parser.add_argument("--pursuit", action=argparse.BooleanOptionalAction, default=None,
                        help="Streamed pure-pursuit following, with fast follow as its fallback. On by default "
                             "unless --sprint, a 0.6/0.8 magnitude, --controller-mode or an entity/POI target "
                             "is given; --no-pursuit selects the pulse controller.")
    parser.add_argument("--smooth-camera", action="store_true",
                        help="With --camera-follow, turn the camera while moving instead of stopping to snap it.")
    parser.add_argument("--camera-follow", action="store_true", help="Follow travel with bounded, measured camera-yaw coordination.")
    parser.add_argument("--sprint", action="store_true", help="Feature-only held-RT sprint on proven straight continuous segments; requires --controller-mode continuous and normal magnitude 0.8.")
    parser.add_argument("--controller-mode", choices=("pulse", "continuous"), default="pulse", help="Continuous 0.8 trial holds movement between fresh validated samples with a broker watchdog.")
    parser.add_argument("--fast-follow", action="store_true", help="Full-stick continuous travel (continuous_fast_follow_trial) without pursuit; implies --controller-mode continuous at magnitude 1.0.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).resolve().parents[4] / "ProjectKnowledge" / "Current" / "canonical-project-state.json")
    parser.add_argument("--target-position", type=float, nargs=3, metavar=("X", "Y", "Z"))
    parser.add_argument("--target-map-id")
    parser.add_argument("--map-target-id", help="Navigate to a map target from observe_map_pins or map_point_to_world.")
    parser.add_argument("--topology-cache-manifest", type=Path)
    parser.add_argument("--feature-ref", help="Opt into an exact clean feature checkout; requires --expected-commit.")
    parser.add_argument("--expected-commit")
    parser.add_argument("--production-test-context", type=Path,
                        help="Explicit JSON feature-test context; emits one model-neutral development event after the verified route result.")
    parser.add_argument("--production-state-dir", type=Path,
                        help="Existing OBS monitor state directory (defaults to FF7R_PRODUCTION_STATE_DIR / FF7R_PUBLIC_STREAM_EVENT_DIR).")
    parser.add_argument("--repository-root", type=Path, default=Path(__file__).resolve().parents[4])
    args = parser.parse_args(argv)
    if bool(args.model_id) != bool(args.reasoning_effort):
        parser.error("--model-id and --reasoning-effort must be supplied together")
    if args.model_id is not None and (not args.model_id.strip() or not args.reasoning_effort.strip()):
        parser.error("model and effort must be nonempty")
    if bool(args.feature_ref) != bool(args.expected_commit):
        parser.error("--feature-ref and --expected-commit must be provided together")
    if args.production_test_context is not None and not args.feature_ref:
        parser.error("--production-test-context requires --feature-ref and --expected-commit")
    from .production_events import (emit_development_report, read_recording_snapshot,
                                    state_dir_from_env)
    production_state = args.production_state_dir or state_dir_from_env()
    if args.production_test_context is not None and production_state is None:
        parser.error("--production-test-context requires --production-state-dir or an existing state-dir environment pointer")
    production_start = read_recording_snapshot(production_state) if args.production_test_context else None
    if args.normal_stick_magnitude in (0.6, 0.8) and not args.feature_ref:
        parser.error("the 0.6 and 0.8 intensity trials require --feature-ref and --expected-commit")
    if args.sprint and args.controller_mode != "continuous":
        parser.error("--sprint requires --controller-mode continuous")
    entity_target = args.poi_id or args.target_id is not None or args.target_kind is not None
    controller_chosen = any(arg.startswith("--controller-mode") for arg in (argv if argv is not None else sys.argv[1:]))
    if args.pursuit is None:
        args.pursuit = not (args.sprint or args.normal_stick_magnitude != 1.0 or controller_chosen or entity_target)
    if args.pursuit:
        if args.sprint or args.normal_stick_magnitude != 1.0:
            parser.error("--pursuit excludes --sprint and needs --normal-stick-magnitude 1.0")
        args.fast_follow = True
    if args.fast_follow:
        if args.sprint or args.normal_stick_magnitude != 1.0:
            parser.error("--fast-follow requires no --sprint and --normal-stick-magnitude 1.0")
        args.controller_mode = "continuous"
    elif args.controller_mode == "continuous" and (not args.feature_ref or args.normal_stick_magnitude != 0.8):
        parser.error("continuous trial requires an exact feature commit and --normal-stick-magnitude 0.8")
    mission_class, config_type, entity_options = NavigationMission, MissionConfig, {}
    if args.actor_name and not args.poi_id:
        parser.error("--actor-name requires --poi-id")
    if args.map_target_id and (args.poi_id or args.target_id or args.target_kind or args.target_position
                               or args.target_map_id or args.goal_id != CHADLEY_GOAL):
        parser.error("--map-target-id cannot be combined with other goals")
    if args.map_target_id:
        entity_options = {"map_target_id": args.map_target_id}
    if args.poi_id:
        from .poi_mission import PoiMissionConfig, PoiInteractionMission
        if args.target_id or args.target_position or args.target_map_id or args.goal_id != CHADLEY_GOAL:
            parser.error("POI selection cannot be combined with other goals")
        mission_class, config_type = PoiInteractionMission, PoiMissionConfig
        entity_options = {"poi_id": args.poi_id, "target_kind": args.target_kind, "actor_name": args.actor_name}
    elif args.target_id is not None or args.target_kind is not None:
        from .entity_mission import EntityInteractionMission, EntityMissionConfig
        if args.target_position or args.target_map_id or args.goal_id != CHADLEY_GOAL:
            parser.error("entity selectors cannot be combined with coordinate or landmark goals")
        if args.controller_mode != "pulse" or args.sprint:
            parser.error("entity missions require the pulse controller")
        mission_class, config_type = EntityInteractionMission, EntityMissionConfig
        entity_options = {"target_id": args.target_id, "target_kind": args.target_kind}
    check = feature_identity_check(args.repository_root, args.feature_ref, args.expected_commit) if args.feature_ref else None
    output = args.output or Path(os.environ.get("LOCALAPPDATA", ".")) / "ff7r-agent" / "navigation-missions" / datetime.now(timezone.utc).strftime("mission-%Y%m%dT%H%M%S.%fZ")
    result = asyncio.run(run_http_mission(config_type(**entity_options, model_id=args.model_id, reasoning_effort=args.reasoning_effort, goal_id=args.goal_id, execute=args.execute, timeout_seconds=args.timeout_seconds, max_cycles=args.max_cycles, sprint=args.sprint, camera_follow_enabled=args.camera_follow, camera_follow_smooth=args.camera_follow and args.smooth_camera, controller_mode=args.controller_mode, normal_stick_magnitude=args.normal_stick_magnitude, fast_follow=args.fast_follow, pursuit=args.pursuit, target_position=tuple(args.target_position) if args.target_position else None, target_map_id=args.target_map_id, topology_cache_manifest=str(args.topology_cache_manifest.resolve()) if args.topology_cache_manifest else None), output, args.manifest, identity_check=check, mission_class=mission_class))
    if args.production_test_context is not None:
        result["output_directory"] = str(output.resolve())
        production_end = read_recording_snapshot(production_state)
        result["production_event_status"] = emit_development_report(
            state_dir=production_state, context_path=args.production_test_context, report=result,
            event_type="navigation_mission_outcome", start_snapshot=production_start,
            end_snapshot=production_end)
    print(json.dumps({key: result.get(key) for key in ("ok", "state", "reason", "metrics", "output_directory",
                                                       "production_event_status")}, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
