"""Navigate to a live entity using the existing cached-topology mission client."""
from dataclasses import dataclass

from .entity_interaction import (
    FINAL_OFFSET, MAX_TARGET_MOVEMENT, STAGING_RADIUS, offset_goal, ready,
    require_stable, resolve_entity, spatial, target_motion, validate_sample, world_identity,
)
from .navigation_mission import (
    MissionConfig, MissionError, NavigationMission, lifecycle_binding, route_metrics,
)


@dataclass(frozen=True)
class EntityMissionConfig(MissionConfig):
    target_id: str | None = None
    target_kind: str | None = None

    def __post_init__(self):
        super().__post_init__()
        if type(self.execute) is not bool:
            raise ValueError("execute must be Boolean")
        if not (self.target_id and self.target_id.strip() or self.target_kind and self.target_kind.strip()):
            raise ValueError("target_id or target_kind is required")
        if self.target_position is not None:
            raise ValueError("entity goals must come from live telemetry")
        if self.controller_mode != "pulse" or self.sprint:
            raise ValueError("entity missions use the proven pulse route policy")


class EntityInteractionMission(NavigationMission):
    mission_kind = "navigate_entity_and_interact"

    async def cleanup_control_session(self, report):
        # A user stop can close our lease while the last RPC is completing.
        # Never neutralize or close a replacement session owned by another task.
        if self.session_owned or self.session_borrowed:
            try:
                lease = await self.call("get_control_session_status", cleanup=True)
            except Exception as exc:
                report.update(ok=False, state="CLEANUP_FAILED")
                return {"session_reconciliation": {"ok": False, "error": str(exc)}}
            if not lease.get("active") or lease.get("client_id") != self.session_client_id:
                self.session_owned = self.session_borrowed = False
                report.update(ok=False, state="STOPPED", reason="entity_cleanup_session_changed")
                return {"session_reconciliation": {"owned": False, "active": lease.get("active")}}
        return await super().cleanup_control_session(report)

    def positioning_goal(self, sample, target, radius):
        if not self.config.topology_cache_manifest:
            return offset_goal(sample, target, radius)
        from .entity_staging import load_staging_graph, select_position
        if not hasattr(self, "staging_graph"):
            self.staging_graph = load_staging_graph(self.config.topology_cache_manifest, sample["map_id"])
        return select_position(sample, target, radius, self.staging_graph)

    async def resolve_mission_goal(self, start, report):
        validate_sample(start)
        self.anchor = resolve_entity(start, self.config.target_id, self.config.target_kind)
        report.update(target=self.anchor, goal_id=None, interaction_attempted=False, interaction_sent=False)
        self.last_sample = start
        relative = spatial(start, self.anchor)
        self.needs_staging = relative["distance_m"] > STAGING_RADIUS
        position = self.positioning_goal(start, self.anchor, STAGING_RADIUS if self.needs_staging else FINAL_OFFSET)
        return {"label": self.anchor["kind"], "map_id": start["map_id"], "position": position}

    async def check_binding(self, binding):
        if lifecycle_binding(await self.call("get_lifecycle_status")) != binding:
            raise MissionError("entity_game_process_changed")
        lease = await self.call("get_control_session_status")
        if not lease.get("active") or lease.get("client_id") != self.session_client_id:
            self.session_owned = self.session_borrowed = False
            raise MissionError("entity_control_session_changed")

    async def reacquire(self, start, binding, report):
        await self.check_binding(binding)
        await self.call("wait_for_settled_gameplay", {
            "timeout_seconds": min(3.0, self.remaining()), "max_telemetry_age_seconds": 0.5})
        # This checks neutral pose stability as well as the service readiness proof.
        samples, targets = [], []
        for _ in range(2):
            await self.sleep(0.2)
            sample = await self.call("observe_game")
            validate_sample(sample, self.last_sample["sequence"], baseline=start)
            target = resolve_entity(sample, self.anchor["id"], self.anchor["kind"])
            target_motion(target, self.anchor, MAX_TARGET_MOVEMENT)
            if samples:
                require_stable(samples[-1], sample, targets[-1], target)
            samples.append(sample)
            targets.append(target)
            self.last_sample = sample
        report.setdefault("reacquisitions", []).append({"samples": samples, "target": targets[-1],
                                                        "relative": spatial(samples[-1], targets[-1])})
        return samples[-1], targets[-1]

    async def navigate_objective(self, arguments, report, goal, start, binding):
        attempts = list(getattr(self, "prior_route_attempts", []))

        async def route(position, phase):
            await self.check_binding(binding)
            remaining_cycles = self.config.max_cycles - report.get("metrics", {}).get("movement_cycles", 0)
            if remaining_cycles <= 0:
                raise MissionError("entity_movement_budget_exhausted")
            values = {**arguments, "route_target_position": position,
                      "max_cycles": min(32, remaining_cycles) if phase == "final" else remaining_cycles,
                      "cached_topology_wall_clock_seconds": min(60 if phase == "final" else 600, self.remaining())}
            # All phases retain the accepted controller policy and independent
            # validator; a relative bearing alone never authorizes a movement.
            if not self.config.execute:
                values["max_cycles"] = 1
            report.setdefault("positioning", []).append({"phase": phase, "goal": position, "max_cycles": values["max_cycles"]})
            self.progress(f"Planning {phase} route to observed {self.anchor['kind']}.")
            result = await self.run_route_job(values, report)
            preflight, run = result.get("preflight") or {}, result.get("run") or {}
            attempts.extend(result.get("route_attempts") or [{"preflight": preflight, "run": run}])
            report["metrics"] = route_metrics({"route_attempts": attempts})
            report.update(preflight_state=preflight.get("state"), route_state=run.get("state"))
            if not preflight.get("movement_authorized"):
                raise MissionError(str(preflight.get("reason") or "entity_route_refused"))
            if not self.config.execute:
                if run.get("movement_cycles") != 0 or run.get("reason") != "control_cycle_bound_reached":
                    raise MissionError("entity_dry_run_failed")
            elif run.get("state") != "ARRIVED":
                raise MissionError(str(run.get("reason") or "entity_staging_not_arrived"))

        if not self.config.execute:
            await route(goal["position"], "staging" if self.needs_staging else "final")
            report.update(ok=True, state="PREFLIGHT_PASSED")
            return
        if self.needs_staging:
            await route(goal["position"], "staging")
        # Reacquire after staging, pin the original ID even for kind selectors,
        # and rebuild each small route from the current target-relative vector.
        for correction in range(4):
            sample, target = await self.reacquire(start, binding, report)
            if ready(sample, target):
                break
            if correction == 3:
                raise MissionError("entity_final_positioning_bound_reached")
            if spatial(sample, target)["distance_m"] > STAGING_RADIUS + MAX_TARGET_MOVEMENT + 0.75:
                raise MissionError("entity_outside_final_approach_envelope")
            await route(self.positioning_goal(sample, target, FINAL_OFFSET), "final")
        await self.check_binding(binding)
        # Mark uncertain before dispatch and never replay this RPC, even when
        # transport fails after the worker has consumed its durable token.
        report["interaction_attempted"] = True
        report["interaction_sent"] = None  # Unknown until a worker response arrives.
        try:
            result = await self.call("interact_with_entity", {
                "target_id": target["id"], "target_kind": target["kind"], "expected_position": target["position"],
                "expected_map_id": start["map_id"], "expected_actor_id": start["player"]["actor_id"],
                "expected_world": world_identity(start), "after_sequence": sample["sequence"],
                "client_id": self.session_client_id, "game_pid": binding["pid"],
                "timeout_seconds": min(10.0, self.remaining())})
        except MissionError as exc:
            if isinstance(exc.response, dict) and "interaction_attempted" in exc.response:
                report["interaction"] = exc.response
                report["interaction_attempted"] = exc.response["interaction_attempted"]
                report["interaction_sent"] = exc.response.get("interaction_sent") is True
            raise
        report["interaction"] = result
        report["interaction_sent"] = result.get("interaction_sent") is True
        if result.get("state") != "INTERACTION_VERIFIED" or not result.get("verification", {}).get("signals"):
            raise MissionError("interaction_unverified")
        report.update(ok=True, state="INTERACTION_VERIFIED", verification=result["verification"])
