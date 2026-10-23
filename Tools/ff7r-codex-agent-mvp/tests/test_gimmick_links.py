"""Scanned traversal gimmicks as routable links, and their independent check."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from ff7r_agent import gimmick_links as gl
from ff7r_agent.detour_route import DetourTopologyGraph
from ff7r_agent.models import Vec3
from offmesh_fixtures import pads_payload, ref

# Two 4 m floors 2 m apart (x 0..400 and 600..1000), joined by nothing.
A_MIDDLE, B_MIDDLE = Vec3(100.0, 200.0, 0.0), Vec3(850.0, 200.0, 0.0)
CRACK_ID = "SampleCrack00_C /Game/Level/Test:PersistentLevel.Crack"
CRACK_ROW = {"id": CRACK_ID, "kind": "crack", "class": "SampleCrack00_C",
             "enter": {"x": 350.0, "y": 200.0, "z": 0.0}, "exit": {"x": 650.0, "y": 200.0, "z": 0.0},
             "rotation": {"pitch": 0, "yaw": 0, "roll": 0}}
VALIDATION_DIR = Path(__file__).resolve().parents[2] / "ff7r-navigation-validation"


def graph() -> DetourTopologyGraph:
    return DetourTopologyGraph.from_dict(pads_payload([]))


def evidence(tmp_path, rows=(CRACK_ROW,)) -> Path:
    path = tmp_path / "TraversalGimmicks" / "evidence.json"
    gl.record_scan(path, list(rows), map_id="Test")
    return path


def test_scan_rows_with_both_ends_become_routable_gimmicks():
    rows = [
        CRACK_ROW,
        {"id": "ladder", "kind": "ladder", "lower": {"x": 0, "y": 0, "z": 0}, "upper": {"x": 0, "y": 0, "z": 800}},
        {"id": "no-exit", "kind": "crouch", "enter": {"x": 0, "y": 0, "z": 0}},
        {"id": "pipe", "kind": "climbing", "upper": {"x": 0, "y": 0, "z": 400}},
    ]
    gimmicks = gl.gimmicks_from_scan(rows)
    assert [(item.gimmick_id, item.link_class) for item in gimmicks] == [(CRACK_ID, "crack"), ("ladder", "ladder")]
    assert gimmicks[1].second == Vec3(0.0, 0.0, 800.0)


def test_a_rescan_keeps_landings_observed_live_unless_the_actor_moved(tmp_path):
    path = evidence(tmp_path)
    gl.record_observed_landing(path, CRACK_ID, gl.DIRECTION_FORWARD, Vec3(700.0, 210.0, 0.0))
    gl.record_scan(path, [CRACK_ROW, {**CRACK_ROW, "id": "other"}])
    known = {item.gimmick_id: item for item in gl.load_evidence(path)}
    assert set(known) == {CRACK_ID, "other"}
    assert known[CRACK_ID].ends(gl.DIRECTION_FORWARD) == (Vec3(350.0, 200.0, 0.0), Vec3(700.0, 210.0, 0.0))
    assert known[CRACK_ID].ends(gl.DIRECTION_REVERSE)[1] == Vec3(350.0, 200.0, 0.0)
    gl.record_scan(path, [{**CRACK_ROW, "exit": {"x": 660.0, "y": 200.0, "z": 0.0}}])
    assert gl.load_evidence(path)[0].observed_landings == {}


def test_a_route_crosses_a_scanned_gimmick_each_way_and_names_it(tmp_path):
    topology = graph()
    assert topology.plan(A_MIDDLE, B_MIDDLE).status == "failed"
    attached = gl.attach_gimmick_links(topology, evidence(tmp_path))
    assert attached["links"] == 2 and attached["sha256"]
    plan = topology.plan(A_MIDDLE, B_MIDDLE)
    assert plan.status == "complete", plan.reason_code
    (segment,) = plan.traversal_segments
    assert segment["link_class"] == "crack" and segment["live_verified"] is True
    assert segment["gimmick_id"] == CRACK_ID and segment["gimmick_direction"] == gl.DIRECTION_FORWARD
    assert segment["link_ref"] == gl.link_reference(CRACK_ID, gl.DIRECTION_FORWARD) < 0
    assert (segment["from_ref"], segment["to_ref"]) == (ref(0), ref(1))
    back = topology.plan(B_MIDDLE, A_MIDDLE)
    assert back.status == "complete" and back.traversal_segments[0]["gimmick_direction"] == gl.DIRECTION_REVERSE


def test_reattaching_replaces_the_gimmick_links_of_a_shared_graph(tmp_path):
    topology = graph()
    path = evidence(tmp_path)
    gl.attach_gimmick_links(topology, path)
    gl.record_observed_landing(path, CRACK_ID, gl.DIRECTION_FORWARD, Vec3(800.0, 100.0, 0.0))
    gl.attach_gimmick_links(topology, path)
    assert sum(1 for link in topology.off_mesh_links if link.gimmick_id) == 2
    segment = topology.plan(A_MIDDLE, B_MIDDLE).traversal_segments[0]
    assert (segment["end"]["x"], segment["end"]["y"]) == (800.0, 100.0)
    gl.attach_gimmick_links(topology, None)
    assert topology.plan(A_MIDDLE, B_MIDDLE).status == "failed"


def test_an_end_the_navmesh_does_not_reach_is_left_out(tmp_path):
    topology = graph()
    far = {**CRACK_ROW, "exit": {"x": 650.0, "y": 200.0, "z": 900.0}}
    attached = gl.attach_gimmick_links(topology, evidence(tmp_path, [far]))
    assert attached["links"] == 0 and attached["end_not_on_mesh"] == 2


# -- the independent check -----------------------------------------------------


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, VALIDATION_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checkers(tmp_path_factory):
    nv = _load("ff7r_test_gimmick_navigation_validation", "navigation_validation.py")
    gv = _load("ff7r_test_gimmick_validation", "gimmick_validation.py")
    path = tmp_path_factory.mktemp("oracle") / "pads.json"
    path.write_text(json.dumps(pads_payload([])), encoding="utf-8")
    return nv.RoutePlanValidator(nv.TopologyAnalyzer(path)), gv


@pytest.fixture()
def crossing_plan(tmp_path):
    topology = graph()
    path = evidence(tmp_path)
    gl.attach_gimmick_links(topology, path)
    plan = topology.plan(A_MIDDLE, B_MIDDLE).to_dict()
    plan["executable_sha256"] = "0" * 64
    return plan, path


def test_the_pinned_validator_checks_each_leg_and_the_crossing_its_evidence(checkers, crossing_plan):
    oracle, gv = checkers
    plan, path = crossing_plan
    # The pinned oracle knows no such connection, and stays unchanged.
    assert oracle.validate(plan)["valid"] is False
    result = gv.validate_gimmick_route(plan, oracle.validate, gv.GimmickEvidence(path))
    assert result["valid"] is True, result["errors"]
    assert result["gimmick_route"]["legs"] == 2 and result["gimmick_route"]["crossings"] == 1


def _tamper(plan, change):
    plan = copy.deepcopy(plan)
    segment = plan["traversal_segments"][0]
    portal = next(item for item in plan["portals"] if item.get("kind") == "traversal")
    if change == "unknown_gimmick":
        segment["gimmick_id"] = "elsewhere"
    elif change == "class":
        segment["link_class"] = portal["traversal_class"] = "ladder"
    elif change == "reference":
        segment["link_ref"] = portal["link_ref"] = segment["link_ref"] - 1
    elif change == "far_end":
        for point in (segment["end"], portal["right"]):
            point["y"] += 300.0
        index = segment["end_waypoint_index"]
        plan["waypoints"][index]["y"] += 300.0
    elif change == "length":
        plan["total_estimated_path_length_m"] += 1.0
    return plan


@pytest.mark.parametrize("change", ["unknown_gimmick", "class", "reference", "far_end", "length"])
def test_a_crossing_that_does_not_match_its_evidence_is_rejected(checkers, crossing_plan, change):
    oracle, gv = checkers
    plan, path = crossing_plan
    result = gv.validate_gimmick_route(_tamper(plan, change), oracle.validate, gv.GimmickEvidence(path))
    assert result["valid"] is False and result["errors"]


def test_a_gimmick_route_without_bound_evidence_is_rejected(checkers, crossing_plan):
    oracle, gv = checkers
    result = gv.validate_gimmick_route(crossing_plan[0], oracle.validate, None)
    assert result["valid"] is False


def test_a_gimmicks_own_body_beside_its_ends_is_not_an_obstacle():
    from types import SimpleNamespace
    from ff7r_agent.live_route_adapter import _gimmick_contact

    # Live at the Kalm cellar: the leg onto the ladder's foot clipped its frame.
    waypoints = (Vec3(0, 0, 0), Vec3(300, 0, 0), Vec3(400, 0, 0), Vec3(420, 0, 800), Vec3(700, 0, 800))
    segment = {"link_class": "ladder", "start": {"x": 400.0, "y": 0.0, "z": 0.0},
               "end": {"x": 420.0, "y": 0.0, "z": 800.0}}
    plan = SimpleNamespace(traversal_segments=(segment,), waypoints=waypoints)

    def hit(segment_index, x, z=0.0):
        return {"segment": segment_index, "kind": "fixed", "location": {"x": x, "y": 0.0, "z": z}}

    assert _gimmick_contact(plan, hit(1, 330.0))          # walking onto the foot
    assert _gimmick_contact(plan, hit(3, 500.0, 800.0))   # stepping off the top
    assert not _gimmick_contact(plan, hit(1, 200.0))      # 2 m short: a real obstacle
    assert not _gimmick_contact(plan, hit(0, 100.0))      # an earlier leg
    vault = SimpleNamespace(traversal_segments=({**segment, "link_class": "step_over"},), waypoints=waypoints)
    assert not _gimmick_contact(vault, hit(1, 330.0))


def test_a_crossing_starts_in_front_of_its_trigger_along_the_gimmick(tmp_path):
    topology = graph()
    gl.attach_gimmick_links(topology, evidence(tmp_path))
    there = topology.plan(A_MIDDLE, B_MIDDLE).traversal_segments[0]
    back = topology.plan(B_MIDDLE, A_MIDDLE).traversal_segments[0]
    assert there["start"]["x"] == pytest.approx(350.0 - gl.LEAD_IN_CM)
    assert back["start"]["x"] == pytest.approx(650.0 + gl.LEAD_IN_CM)


def test_the_obstacle_overlay_never_blocks_a_gimmick_with_its_own_body():
    from types import SimpleNamespace
    from ff7r_agent.static_obstacles import StaticObstacle, _portal_hits_obstacle, _walked_hits

    def obstacle(x, y):
        return StaticObstacle(obstacle_id=f"o{x}", center=Vec3(x, y, 0.0), radius_m=0.3, cloud_clearance_m=0.45,
                              uncertainty_margin_m=0.1, confidence=0.9, evidence_count=1, first_sequence=1,
                              last_sequence=1, first_t=0.0, last_t=0.0, provenance=("test",))

    waypoints = (Vec3(0, 0, 0), Vec3(300, 0, 0), Vec3(400, 0, 0), Vec3(420, 0, 800), Vec3(700, 0, 800))
    segment = {"link_class": "ladder", "start": {"x": 400.0, "y": 0.0, "z": 0.0},
               "end": {"x": 420.0, "y": 0.0, "z": 800.0}}
    plan = SimpleNamespace(traversal_segments=(segment,), waypoints=waypoints)
    frame = obstacle(430.0, 0.0)  # the ladder: on its own leg and both ends
    assert _walked_hits(plan, frame) == ()
    elsewhere = obstacle(220.0, 0.0)  # 1.8 m short of the departure: a real obstacle
    assert _walked_hits(plan, elsewhere) == (0, 1)
    portal = SimpleNamespace(left=Vec3(400, 0, 0), right=Vec3(420, 0, 800), traversal_class="ladder")
    assert not _portal_hits_obstacle(portal, frame)
    assert _portal_hits_obstacle(SimpleNamespace(left=portal.left, right=portal.right, traversal_class=None), frame)
