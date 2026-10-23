#!/usr/bin/env python3
"""Movement-free structured objective -> target -> Detour route replay.

All quest state and target IDs are explicitly synthetic. The navmesh uses the
native export schema and the production planner. This cannot load a save,
inspect a live quest or send input; it demonstrates the integration seam only.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ff7r_agent.detour_route import DetourTopologyGraph
from ff7r_agent.models import Vec3
from ff7r_agent.objective_resolver import objective_key, resolve_active_objective
from navigation_perf import build_fingerprint_payload


def replay():
    identity = {"world_id": "offline-fixture", "map_id": "offline-grid", "process_token": "no-game-process"}
    graph = DetourTopologyGraph.from_dict(build_fingerprint_payload(20, 20, 100.0))
    targets = {"fixture-A": {"x": 950.0, "y": 50.0, "z": 0.0},
               "fixture-B": {"x": 950.0, "y": 950.0, "z": 0.0}}
    def resolve(reference):
        return {"status": "located", "position": targets[reference["target_id"]], "map_id": identity["map_id"],
                "source": "synthetic placement lookup", "authority": "offline fixture only"}
    now = datetime.now(timezone.utc)
    start = Vec3(50.0, 50.0, 0.0)
    steps = []
    previous = None
    for sequence, target in enumerate(targets, 1):
        snapshot = {"schema_version": 1, "identity": identity, "observed_at": now.isoformat(), "sequence": sequence,
                    "observed": True, "source": "synthetic objective replay", "authority": "offline fixture only",
                    "objectives": [{"state": "active", "quest_id": "fixture-quest", "objective_id": "fixture-objective",
                                    "step_id": f"step-{sequence}", "targets": [{"target_type": "poi", "target_id": target,
                                                                               "source": "synthetic task row"}]}]}
        began = time.perf_counter()
        destination = resolve_active_objective(snapshot, current_identity=identity, resolve_target=resolve, now=now)
        if not destination["ok"]:
            raise AssertionError(destination)
        key = objective_key(destination)
        if previous == key:
            raise AssertionError("same-quest step transition was missed")
        position = destination["position"]
        plan = graph.plan(start, Vec3(position["x"], position["y"], position["z"]))
        if plan.status != "complete" or plan.movement_sent:
            raise AssertionError("fixture route did not complete without input")
        steps.append({"destination": destination, "transition_detected": previous is not None,
                      "route": plan.to_dict(), "resolve_and_plan_ms": (time.perf_counter() - began) * 1000})
        start = plan.resolved_end
        previous = key
    stale = {**snapshot, "identity": {**identity, "process_token": "different-process"}}
    refused = resolve_active_objective(stale, current_identity=identity, resolve_target=lambda _: (_ for _ in ()).throw(
        AssertionError("must refuse before target lookup")), now=now)
    if refused["ok"]:
        raise AssertionError("identity mismatch accepted")
    return {"schema_version": 1, "offline_fixture_only": True, "live_objective_source_verified": False,
            "input_sent": False, "rendered_frames_used": 0, "steps": steps, "stale_identity_refusal": refused,
            "llm_token_usage": None, "live_reliability_comparison": None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = replay()
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"steps": len(result["steps"]), "input_sent": False,
                      "routes": [step["route"]["status"] for step in result["steps"]]}))
