from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
for path in (SRC, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ff7r_agent.continuous_control import movement_lease_seconds  # noqa: E402
from ff7r_agent.live_route_adapter import (  # noqa: E402
    _segment_authorized_corridor,
    obstacle_segment_clearance_m,
)
from ff7r_agent.models import Vec3  # noqa: E402
from test_continuous_control import stream_adapter  # noqa: E402
from test_live_route_adapter import make_observation  # noqa: E402


def _prove_deadline(adapter, observation, steering, *, duration):
    if duration < 0.04 or adapter.topology_session is None or adapter.authorization_proof is None:
        return None
    speed_bound = 8.0
    age = adapter._effective_telemetry_age_seconds(observation)
    graph = adapter.topology_session.graph
    corridor = adapter._route_corridor_refs()
    index = max(0, (adapter._route_local_corridor_index or 0) - 1)
    refs = corridor[index:index + 16]
    portals = []
    for source, target_ref in zip(refs, refs[1:]):
        serialized = adapter._portal_for_transition(adapter.route_plan, source, target_ref)
        canonical = next((p for p in graph.adjacency.get(source, ())
                          if p.to_reference == target_ref and p.to_dict() == serialized), None)
        if canonical is None:
            return None
        portals.append(canonical)
    obstacles = adapter.static_obstacles.planning_snapshot(now_t=adapter.monotonic()).obstacles
    heading = math.radians(float(steering["desired_world_heading_deg"]))
    for factor in (1.0, 0.5, 0.25):
        hold = duration * factor
        if hold < 0.04:
            continue
        horizon = speed_bound * (age + hold) + 0.75
        target = Vec3(
            observation.position.x + math.cos(heading) * horizon * 100.0,
            observation.position.y + math.sin(heading) * horizon * 100.0,
            observation.position.z,
        )
        if any(obstacle_segment_clearance_m(observation.position, target, item) <= 0 for item in obstacles):
            continue
        if _segment_authorized_corridor(
            observation.position,
            target,
            graph=graph,
            corridor_refs=refs,
            portals=portals,
            minimum_edge_clearance_m=0.35,
            max_vertical_separation_m=adapter.config.start_max_vertical_m,
        ):
            return {
                "lease_seconds": hold,
                "horizon_m": horizon,
                "corridor_proven": True,
            }
    return None


def legacy_deadline(adapter, observation, steering, event):
    """Reference the pre-change non-sprint lease calculation."""
    age = adapter._effective_telemetry_age_seconds(observation)
    duration = movement_lease_seconds(
        effective_age=age,
        freshness_limit=adapter.config.max_telemetry_age_seconds,
        distance_to_turn_m=float(event.get("distance_m") or 0.0),
        speed_bound_m_s=8.0,
        braking_margin_m=0.75,
    )
    return _prove_deadline(adapter, observation, steering, duration=duration)


def main() -> None:
    adapter, _, _ = stream_adapter()
    steering = {"control_band": "normal", "desired_world_heading_deg": 0.0}
    distances = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 4.0, 8.0)
    positions_cm = (0.0, 400.0, 700.0, 900.0, 990.0)
    rows = []
    legacy_eligible = 0
    production_eligible = 0
    for x in positions_cm:
        for distance in distances:
            observation = make_observation(position=Vec3(x, 0.0, 0.0))
            event = {"distance_m": distance}
            legacy = legacy_deadline(adapter, observation, steering, event)
            production = adapter._supervised_deadline(observation, steering, event)
            legacy_eligible += legacy is not None
            production_eligible += production is not None
            rows.append({
                "x_m": x / 100.0,
                "active_waypoint_distance_m": distance,
                "legacy_supervised": legacy is not None,
                "production_supervised": production is not None,
                "legacy_lease_s": legacy.get("lease_seconds") if legacy else None,
                "production_lease_s": production.get("lease_seconds") if production else None,
            })

    near_edge = [row for row in rows if row["x_m"] == 9.9]
    assert near_edge and not any(row["production_supervised"] for row in near_edge)
    assert production_eligible >= legacy_eligible
    gained = production_eligible - legacy_eligible
    assert gained > 0
    result = {
        "samples": len(rows),
        "legacy_supervised_samples": legacy_eligible,
        "production_supervised_samples": production_eligible,
        "newly_supervised_samples": gained,
        "relative_eligibility_increase": (
            production_eligible / legacy_eligible - 1.0 if legacy_eligible else None
        ),
        "rows": rows,
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
