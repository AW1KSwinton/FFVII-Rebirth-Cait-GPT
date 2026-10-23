"""Bounded destination selection on existing cached topology; never sends input."""
import hashlib
import json
import math
from pathlib import Path
from .detour_route import DetourTopologyGraph
from .entity_interaction import offset_goal
from .models import Vec3
from .navigation_mission import MissionError


def load_staging_graph(manifest_path, map_id):
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf8"))
    artifact = (path.parent / manifest["source_artifact_path"]).resolve()
    if (not artifact.is_relative_to(path.parent.resolve()) or manifest.get("map_id") != map_id
        or manifest.get("admitted_atomically") is not True or artifact.stat().st_size > 256 * 1024 * 1024):
        raise MissionError("entity_staging_cache_invalid")
    data = artifact.read_bytes()
    if hashlib.sha256(data).hexdigest().upper() != manifest.get("admitted_artifact_sha256"):
        raise MissionError("entity_staging_cache_changed")
    return DetourTopologyGraph.from_dict(json.loads(data))


def select_position(sample, target, radius, graph):
    """Try at most 24 points at the requested target-relative radius.

    This only selects a goal hint. The ordinary adapter independently validates
    cache/session identity, reachability, corridor, obstacles and every input.
    """
    desired = offset_goal(sample, target, radius)
    origin = target["position"]
    base_angle = math.atan2(desired["y"]-origin["y"], desired["x"]-origin["x"])
    angles = [0] + [direction * n for n in range(1, 12) for direction in (1, -1)] + [12]
    start = Vec3(**sample["player"]["position"])
    for step in angles:
        angle = base_angle + step * math.pi / 12
        point = Vec3(origin["x"]+radius*100*math.cos(angle),
                     origin["y"]+radius*100*math.sin(angle), origin["z"])
        resolved = graph.resolve_point(point, max_snap_distance_m=0.75, max_vertical_separation_m=1.0)
        if resolved.ok and resolved.reason_code == "point_inside_polygon":
            plan = graph.plan(start, point, max_expansions=100000, max_waypoints=256,
                              max_snap_distance_m=0.75, max_vertical_separation_m=1.0)
            if plan.status != "complete":
                continue
            return {"x":point.x,"y":point.y,"z":point.z}
    raise MissionError("entity_no_walkable_staging_position")
