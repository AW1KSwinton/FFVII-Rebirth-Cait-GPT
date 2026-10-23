"""Resolve a map POI, stage through the existing route, then acquire a live entity."""
from dataclasses import dataclass
from .entity_mission import EntityInteractionMission
from .entity_interaction import (STAGING_RADIUS, offset_goal, resolve_entity, validate_sample, require_stable)
from .map_poi import resolved_point
from .navigation_mission import MissionConfig, MissionError, horizontal_distance, route_metrics

@dataclass(frozen=True)
class PoiMissionConfig(MissionConfig):
    poi_id: str = ""
    target_kind: str | None = None
    actor_name: str | None = None
    target_id: str | None = None

    def __post_init__(self):
        super().__post_init__()
        if not self.poi_id or self.target_position is not None or self.target_id is not None:
            raise ValueError("poi_id is required and cannot be combined with other goals")
        if self.controller_mode != "pulse" or self.sprint:
            raise ValueError("POI interaction missions require the proven pulse controller")


def acquire_poi_entity(sample, poi, *, kind=None, actor_name=None):
    candidates = []
    for feed in (sample.get("nearby_actors", []), sample.get("nearby_interactables", [])):
        for item in feed:
            if kind and item.get("kind") != kind:
                continue
            if actor_name and (item.get("name") or item.get("label")) != actor_name:
                continue
            target = resolve_entity(sample, item["id"], kind)
            if (horizontal_distance(target["position"], poi["position"]) <= 10
                and abs(target["position"]["z"]-poi["position"]["z"]) <= 300):
                if target not in candidates:
                    candidates.append(target)
    if len(candidates) != 1:
        raise MissionError("poi_entity_missing_or_ambiguous")
    return candidates[0]


class PoiInteractionMission(EntityInteractionMission):
    mission_kind = "navigate_map_poi_and_interact"
    poi = None

    async def resolve_mission_goal(self, start, report):
        result = await self.call("observe_map_poi", {"poi_id": self.config.poi_id,
                                                    "seconds": min(1500, int(self.remaining())+10)})
        self.poi = result["poi"]
        # The observation RPC may take time. Retain its fresh sample for the
        # relative staging vector; process/world admission remains unchanged.
        sample = result["sample"]
        validate_sample(sample, baseline=start)
        report.update(poi=self.poi, interaction_attempted=False, interaction_sent=False)
        self.poi_needs_staging = horizontal_distance(sample["player"]["position"], self.poi["position"]) > STAGING_RADIUS
        position = self.positioning_goal(sample, self.poi, STAGING_RADIUS) if self.poi_needs_staging else self.poi["position"]
        return {"label": self.config.poi_id, "map_id": start["map_id"], "position": position}

    async def navigate_objective(self, arguments, report, goal, start, binding):
        self.prior_route_attempts = []
        if self.poi_needs_staging or not self.config.execute:
            await self.check_binding(binding)
            result = await self.run_route_job(arguments, report)
            preflight, run = result.get("preflight") or {}, result.get("run") or {}
            self.prior_route_attempts = list(result.get("route_attempts") or [{"preflight": preflight, "run": run}])
            report["metrics"] = route_metrics({"route_attempts": self.prior_route_attempts})
            report["poi_staging"] = {"goal": goal, "preflight": preflight.get("state"), "state": run.get("state")}
            if not preflight.get("movement_authorized"):
                raise MissionError(str(preflight.get("reason") or "poi_route_refused"))
            if not self.config.execute:
                if run.get("movement_cycles") != 0 or run.get("reason") != "control_cycle_bound_reached":
                    raise MissionError("poi_dry_run_failed")
                report.update(ok=True, state="PREFLIGHT_PASSED")
                return
            if run.get("state") != "ARRIVED":
                raise MissionError(str(run.get("reason") or "poi_staging_not_arrived"))
        await self.check_binding(binding)
        await self.call("wait_for_settled_gameplay", {"timeout_seconds": min(4, self.remaining()),
                                                    "max_telemetry_age_seconds": 0.5})
        previous, target_before = None, None
        for _ in range(2):
            await self.sleep(0.2)
            sample = await self.call("observe_game")
            validate_sample(sample, previous["sequence"] if previous else -1, baseline=start)
            current_poi = resolved_point(sample, self.poi, self.poi["request_id"])
            if (horizontal_distance(current_poi["position"], self.poi["position"]) > 0.1
                or abs(current_poi["position"]["z"]-self.poi["position"]["z"]) > 10):
                raise MissionError("poi_changed_during_navigation")
            target = acquire_poi_entity(sample, self.poi, kind=self.config.target_kind,
                                        actor_name=self.config.actor_name)
            if previous:
                require_stable(previous, sample, target_before, target)
            previous, target_before = sample, target
        self.anchor, self.last_sample = target, sample
        report["target"] = target
        report["poi_to_entity_distance_m"] = horizontal_distance(target["position"], self.poi["position"])
        # Same bounded entity approach as a direct live target selection.
        self.needs_staging = horizontal_distance(sample["player"]["position"], target["position"]) > STAGING_RADIUS
        entity_goal = {"label": target["kind"], "map_id": sample["map_id"],
                       "position": self.positioning_goal(sample, target, STAGING_RADIUS)}
        await super().navigate_objective(arguments, report, entity_goal, start, binding)

    async def cleanup_control_session(self, report):
        cleanup = await super().cleanup_control_session(report)
        if self.poi:
            try:
                cleanup["poi_observation"] = await self.call("release_map_poi", {"request_id": self.poi["request_id"]}, cleanup=True)
            except Exception as exc:
                cleanup["poi_observation"] = {"ok": False, "error": str(exc)}
                report.update(ok=False, state="CLEANUP_FAILED")
        return cleanup
