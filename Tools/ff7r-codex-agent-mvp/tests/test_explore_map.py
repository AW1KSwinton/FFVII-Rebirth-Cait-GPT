from __future__ import annotations

import math

import pytest

from explore_fixtures import CELL_CM, build_graph, corridor_world, decode_png, pixel
from ff7r_agent import explore_map as em
from ff7r_agent.models import Vec3


PLAYER = Vec3(250.0, 250.0, 0.0)


@pytest.fixture(scope="module")
def graph():
    return corridor_world()


@pytest.fixture(scope="module")
def analysis(graph):
    return em.analyse_view(graph, PLAYER, 0.0, radius_m=60.0)


@pytest.fixture(scope="module")
def local_analysis(graph):
    return em.analyse_view(graph, PLAYER, 0.0, centre=Vec3(2500.0, 750.0, 0.0), radius_m=40.0)


def test_transform_round_trips_between_world_and_pixels():
    transform = em.make_transform(1000.0, -2000.0, 60.0, 8.0)
    assert (transform.width, transform.height) == (960, 960)
    assert transform.left_cm == 1000.0 - 6000.0 and transform.top_cm == -2000.0 - 6000.0
    for world in ((1000.0, -2000.0), (-4321.5, 3999.0), (6999.0, 3999.9)):
        px, py = transform.to_pixel(*world)
        assert transform.to_world(px, py) == pytest.approx(world)
    # the centre of the view is the centre of the image
    assert transform.to_pixel(1000.0, -2000.0) == pytest.approx((480.0, 480.0))
    # one metre east is px_per_m pixels right; +Y runs down the image
    assert transform.to_pixel(1100.0, -2000.0)[0] - 480.0 == pytest.approx(8.0)
    assert transform.to_pixel(1000.0, -1900.0)[1] - 480.0 == pytest.approx(8.0)
    document = transform.to_dict()
    assert document["top_left_m"] == {"x": -50.0, "y": -80.0}
    assert document["bottom_right_m"] == {"x": 70.0, "y": 40.0}
    assert "centimetres" in document["units"]
    # the documented formula reproduces the method
    px, py = 123.0, 456.0
    assert transform.to_world(px, py) == pytest.approx(
        (document["left_cm"] + px / document["px_per_m"] * 100, document["top_cm"] + py / document["px_per_m"] * 100))
    assert em.ViewTransform.from_dict(document) == transform


@pytest.mark.parametrize("bad", [{}, {"left_cm": 0, "top_cm": 0}, {"left_cm": 0, "top_cm": 0, "px_per_m": 0},
                                 {"left_cm": "x", "top_cm": 0, "px_per_m": 4},
                                 {"left_cm": float("inf"), "top_cm": 0, "px_per_m": 4}])
def test_a_malformed_transform_is_rejected(bad):
    with pytest.raises(ValueError):
        em.ViewTransform.from_dict(bad)


def test_pixel_scale_is_capped_and_grid_follows_the_radius():
    assert em.choose_px_per_m(60.0) == 8.0
    assert em.choose_px_per_m(250.0) == pytest.approx(960 / 500.0)
    assert em.choose_px_per_m(10.0, requested=16.0) == 16.0
    assert em.choose_px_per_m(250.0, requested=16.0) == pytest.approx(em.MAX_IMAGE_PIXELS / 500.0)
    assert [em.grid_step_m(radius) for radius in (10, 30, 60, 150, 250)] == [2, 5, 10, 25, 50]


def test_the_player_stands_on_a_navmesh_island_the_planner_can_walk(graph, analysis):
    assert analysis.start_reference is not None
    assert analysis.island_polygons == 30
    reachable = em.reachable_references(graph, analysis.start_reference, analysis.labels)
    assert len(reachable) == 30
    # island B and the stacked floor are polygons of their own islands
    owners = {analysis.labels[ref] for ref in graph.polygons}
    assert len(owners) == 3
    assert em.route_start_ok(analysis.start_resolution)


def test_geodesic_cost_grows_along_the_corridor(graph, analysis):
    costs = em.geodesic_costs(graph, analysis.start_reference)
    assert len(costs) == 30
    by_x = sorted((graph.polygons[ref].centroid.x, cost) for ref, cost in costs.items()
                  if graph.polygons[ref].centroid.y == 250.0)
    assert [x for x, _ in by_x] == [250.0 + CELL_CM * k for k in range(10)]
    assert [cost for _, cost in by_x] == sorted(cost for _, cost in by_x)
    assert by_x[-1][1] == pytest.approx(9 * CELL_CM, rel=0.02)
    assert not set(costs) & {ref for ref, label in analysis.labels.items() if label != analysis.start_label}


@pytest.mark.parametrize("flags", [64, 192])
def test_explore_uses_permitted_directed_traversal_links(flags):
    from ff7r_agent.detour_route import DetourTopologyGraph
    fixtures = pytest.importorskip("offmesh_fixtures", reason="traversal integration requires PR #370")
    connection, pads_payload, ref = fixtures.connection, fixtures.pads_payload, fixtures.ref

    graph = DetourTopologyGraph.from_dict(pads_payload([
        connection((350.0, 200.0, 0.0), (650.0, 200.0, 0.0), flags=flags)]))
    start, goal = Vec3(100.0, 200.0, 0.0), Vec3(850.0, 200.0, 0.0)
    assert graph.plan(start, goal).status == "complete"
    assert ref(1) in em.reachable_references(graph, ref(0))
    assert ref(0) not in em.reachable_references(graph, ref(1))
    view = em.analyse_view(graph, start, 0.0, radius_m=10.0)
    assert ref(1) in view.geodesic
    assert any(c["reachable"] and c["position"]["x"] > 600.0 for c in view.candidates)
    target = em.snap_target(graph, goal, z_given=True, player=start,
                            reachable=em.reachable_references(graph, ref(0)), labels=view.labels)
    assert target["status"] == "ok"
    reverse = em.snap_target(graph, start, z_given=True, player=goal,
                             reachable=em.reachable_references(graph, ref(1)), labels=view.labels)
    assert reverse["reason"] == "target_on_unreachable_island"


def test_explore_offers_a_drop_the_planner_permits():
    """Drops are routed since 2026-10-21; the land below is reachable."""
    from ff7r_agent.detour_route import DetourTopologyGraph
    fixtures = pytest.importorskip("offmesh_fixtures", reason="traversal integration requires PR #370")
    connection, pads_payload, ref = fixtures.connection, fixtures.pads_payload, fixtures.ref

    graph = DetourTopologyGraph.from_dict(pads_payload([
        connection((350.0, 200.0, 0.0), (650.0, 200.0, -200.0))], rise_cm=(0.0, -200.0)))
    assert ref(1) in em.reachable_references(graph, ref(0))
    assert ref(1) in em.geodesic_costs(graph, ref(0))


@pytest.mark.parametrize("rise_cm", [200.0])
def test_explore_does_not_offer_unpermitted_vertical_traversals(rise_cm):
    from ff7r_agent.detour_route import DetourTopologyGraph
    fixtures = pytest.importorskip("offmesh_fixtures", reason="traversal integration requires PR #370")
    connection, pads_payload, ref = fixtures.connection, fixtures.pads_payload, fixtures.ref

    graph = DetourTopologyGraph.from_dict(pads_payload([
        connection((350.0, 200.0, 0.0), (650.0, 200.0, rise_cm))], rise_cm=(0.0, rise_cm)))
    assert ref(1) not in em.reachable_references(graph, ref(0))
    assert ref(1) not in em.geodesic_costs(graph, ref(0))


def test_directed_planner_portals_override_adjacency_without_the_traversal_dependency(monkeypatch):
    from types import SimpleNamespace

    graph = corridor_world()
    labels = em.island_labels(graph)
    start = em.locate_player(graph, PLAYER).selected_reference
    other = em.locate_player(graph, Vec3(250.0, 3250.0, 0.0)).selected_reference
    ordinary = {ref: tuple(portals) for ref, portals in graph.adjacency.items()}
    bridge = SimpleNamespace(to_reference=other, left=PLAYER, right=Vec3(250.0, 3250.0, 0.0))
    reverse = SimpleNamespace(to_reference=start, left=bridge.right, right=bridge.left, source_edge=-1)
    graph.adjacency[other] = (*ordinary.get(other, ()), reverse)
    # This test exercises Explore's portal contract, not either planner's
    # internal corridor representation. Real traversal planning is tested above.
    monkeypatch.setattr(em, "PLAN_REFINEMENT_SECONDS", 0.0)
    monkeypatch.setattr(graph, "outgoing_portals",
                        lambda ref, policy=None: (*ordinary.get(ref, ()), bridge) if ref == start else ordinary.get(ref, ()),
                        raising=False)
    reached = em.reachable_references(graph, start)
    assert other in reached and start not in em.reachable_references(graph, other)
    assert set(em.geodesic_costs(graph, start)) == reached
    view = em.analyse_view(graph, PLAYER, 0.0, radius_m=40.0, pois=[
        {"kind": "npc", "label": "Across step", "position": {"x": 250.0, "y": 3250.0, "z": 88.0}}])
    assert next(c for c in view.pois if c["label"] == "Across step")["reachable"]
    assert next(i for i in view.islands if i["id"] == labels[other])["reachable"]
    assert not any(c["kind"] == "other_island" and c["island"] == labels[other] for c in view.candidates)


def test_explore_accepts_the_real_girder_target_on_the_traversal_planner():
    from ff7r_agent.detour_route import DetourTopologyGraph
    fixtures = pytest.importorskip("offmesh_fixtures", reason="traversal integration requires PR #370")

    graph = DetourTopologyGraph.from_dict(fixtures.girder_payload())
    player = Vec3(*fixtures.GIRDER_START)
    target = Vec3(4592.0, 67343.0, -105.0)  # the actual Explore live refusal
    assert graph.plan(player, target).status == "complete"
    start = em.locate_player(graph, player).selected_reference
    reached = em.reachable_references(graph, start)
    view = em.analyse_view(graph, player, 0.0, radius_m=10.0, pois=[
        {"kind": "landmark", "label": "Girder far landing", "position": {"x": target.x, "y": target.y, "z": target.z}}])
    assert next(c for c in view.pois if c["label"] == "Girder far landing")["reachable"]
    snapped = em.snap_target(graph, target, z_given=True, player=player, reachable=reached, labels=view.labels)
    assert snapped["status"] == "ok" and snapped["reference"] in view.geodesic
    assert graph.plan(player, snapped["point"]).status == "complete"


def test_older_planners_still_use_adjacency(monkeypatch):
    graph = corridor_world()
    monkeypatch.setattr(graph, "outgoing_portals", None, raising=False)
    start = em.locate_player(graph, PLAYER).selected_reference
    labels = em.island_labels(graph)
    expected = {ref for ref, label in labels.items() if label == labels[start]}
    assert em.reachable_references(graph, start) == expected
    assert set(em.geodesic_costs(graph, start)) == expected


def test_candidate_markers_stay_inside_the_rendered_view(graph):
    view = em.analyse_view(graph, PLAYER, 0.0, radius_m=14.0)
    transform = em.make_transform(PLAYER.x, PLAYER.y, 14.0, 8.0)
    assert view.candidates
    assert all(transform.contains(c["position"]["x"], c["position"]["y"]) for c in view.candidates)


def test_far_ends_of_the_island_are_offered_and_reachable(analysis):
    far = [c for c in analysis.candidates if c["kind"] == "far_end"]
    assert far and all(c["reachable"] for c in far)
    assert max(c["position"]["x"] for c in far) == pytest.approx(4750.0)
    farthest = max(far, key=lambda c: c["distance_m"])
    # the far corner (4750, 1250) from the player at (250, 250)
    assert farthest["distance_m"] == pytest.approx(math.hypot(45.0, 10.0), abs=0.1)
    # the planner's funnelled path: near the straight line, never shorter, and shorter than the polygon-hop estimate
    assert farthest["path_source"] == "planner"
    assert farthest["distance_m"] <= farthest["path_m"] <= farthest["distance_m"] * 1.1
    assert farthest["height_vs_player_m"] == 0.0
    # candidates are numbered 1..n without gaps, and carry the aiming data
    assert [c["id"] for c in analysis.candidates] == list(range(1, len(analysis.candidates) + 1))
    for candidate in analysis.candidates:
        assert candidate["position_m"]["x"] == pytest.approx(candidate["position"]["x"] / 100.0, abs=0.01)
        assert -180.0 <= candidate["relative_to_facing_deg"] <= 180.0


def test_other_islands_are_listed_as_not_reachable_with_their_nearest_point(analysis):
    islands = [c for c in analysis.candidates if c["kind"] == "other_island"]
    assert len(islands) == 2
    assert all(not c["reachable"] and c["path_m"] is None for c in islands)
    by_size = {c["island_polygons"]: c for c in islands}
    assert set(by_size) == {9, 6}
    # island B starts 30 m north (+y) of the corridor's first cell; its nearest point is its near edge
    assert by_size[9]["position"]["y"] == pytest.approx(6 * CELL_CM + 50.0, abs=60.0)
    # the stacked floor is above the corridor: 5 m higher, nearest to the player at its low x end
    assert by_size[6]["height_vs_player_m"] == pytest.approx(5.0, abs=0.1)
    assert analysis.islands[0]["reachable"] is True and len(analysis.islands) == 3


def test_the_captured_mesh_ending_is_offered_as_an_exit(analysis):
    edges = [c for c in analysis.candidates if c["kind"] == "capture_edge"]
    assert edges and all(c["reachable"] for c in edges)
    assert {"+x", "-x"} & {c["side"] for c in edges}
    assert all("start_topology_capture" in c["note"] for c in edges)


def test_known_points_of_interest_are_snapped_and_flagged_by_reachability(graph):
    pois = [{"kind": "npc", "label": "Guard", "position": {"x": 2100.0, "y": 700.0, "z": 88.0}},
            {"kind": "door", "label": "Gate", "position": {"x": 600.0, "y": 3600.0, "z": 0.0}},
            {"kind": "npc", "label": "Nowhere", "position": {"x": 99999.0, "y": 0.0, "z": 0.0}}]
    result = em.analyse_view(graph, PLAYER, 90.0, radius_m=60.0, pois=pois)
    by_label = {c.get("label"): c for c in result.candidates if c["kind"] in ("poi", "door")}
    assert set(by_label) == {"Guard", "Gate"}  # the off-mesh one far outside the view is dropped
    assert by_label["Guard"]["reachable"] and by_label["Guard"]["kind"] == "poi"
    assert by_label["Guard"]["poi_snap_m"] == 0.0 or by_label["Guard"]["poi_snap_m"] < 1.0
    assert not by_label["Gate"]["reachable"] and by_label["Gate"]["kind"] == "door"
    assert [p["label"] for p in result.pois] == [c["label"] for c in result.candidates if c["kind"] in ("poi", "door")]


def test_a_player_off_the_mesh_gets_only_other_islands(graph):
    result = em.analyse_view(graph, Vec3(-9000.0, 250.0, 0.0), None, centre=Vec3(2000.0, 500.0, 0.0), radius_m=60.0)
    assert result.start_reference is None and result.island_polygons == 0
    assert result.candidates and all(c["kind"] == "other_island" and not c["reachable"] for c in result.candidates)
    assert all(c["relative_to_facing_deg"] is None for c in result.candidates)


def test_the_png_is_valid_sized_by_the_transform_and_marks_player_and_candidates(graph, local_analysis):
    analysis = local_analysis
    transform = em.make_transform(2500.0, 750.0, 40.0, 8.0)
    png, facts = em.render_view(graph, analysis, transform, radius_m=40.0)
    width, height, rows = decode_png(png)
    assert (width, height) == (transform.width, transform.height) == (640, 640)
    assert em.decode_png_size(png) == (640, 640)
    assert facts["grid_step_m"] == 10 and facts["polygons_drawn"] > 30
    # the player is a white dot on the pixel the transform gives
    px, py = transform.to_pixel(PLAYER.x, PLAYER.y)
    assert pixel(rows, round(px), round(py)) == (255, 255, 255)
    # the facing arrow (yaw 0 = +x) is drawn to the player's right in red
    assert pixel(rows, round(px) + 30, round(py)) == (255, 80, 80)
    # a point on the corridor floor is a height colour, a point in the void is background or grid
    floor = pixel(rows, *(round(v) for v in transform.to_pixel(3000.0, 750.0)))
    assert floor not in {(18, 18, 22), (44, 44, 56)}
    void = pixel(rows, *(round(v) for v in transform.to_pixel(3000.0, 3000.0)))
    assert void in {(18, 18, 22), (44, 44, 56)} or void[0] > 200  # a label pixel at worst
    # every candidate's marker colour sits on its pixel ring or centre
    for candidate in analysis.candidates:
        cx, cy = transform.to_pixel(candidate["position"]["x"], candidate["position"]["y"])
        colour = em.KIND_COLOURS[candidate["kind"]]
        ring = [pixel(rows, round(cx) + dx, round(cy) + dy) for dx in range(-8, 9) for dy in range(-8, 9)
                if 0 <= round(cx) + dx < width and 0 <= round(cy) + dy < height]
        assert colour in ring, candidate


def test_axis_labels_are_drawn_in_world_metres_along_the_edges(graph, local_analysis):
    transform = em.make_transform(2500.0, 750.0, 40.0, 8.0)
    canvas_png, _ = em.render_view(graph, local_analysis, transform, radius_m=40.0)
    _, _, rows = decode_png(canvas_png)
    # grid lines every 5 m: world x = 0 m is px = (0 - left) / 100 * 8; the top edge label sits right of its line
    left_m = transform.left_cm / 100.0
    for world_m in (-20, 0, 20):
        column = round((world_m - left_m) * transform.px_per_m)
        if 0 <= column < transform.width - 40:
            band = [pixel(rows, x, y) for x in range(column + 3, column + 40) for y in range(3, 17)]
            assert (255, 255, 255) in band, world_m  # the label's glyph pixels


def test_the_glyph_font_covers_every_label_it_draws():
    canvas = em.Canvas(300, 30, (0, 0, 0))
    canvas.text(2, 2, "-12.5 X Y Z M m +9", (255, 255, 255))
    lit = sum(1 for y in range(30) for x in range(300) if canvas.pixel(x, y) == (255, 255, 255))
    assert lit > 150


def test_a_target_inside_the_floor_snaps_with_no_distance_and_stays_off_the_edge(graph, analysis):
    reachable = em.reachable_references(graph, analysis.start_reference, analysis.labels)
    inside = em.snap_target(graph, Vec3(1260.0, 760.0, 0.0), z_given=False, player=PLAYER, reachable=reachable,
                            labels=analysis.labels)
    assert inside["status"] == "ok" and inside["snap_horizontal_m"] == 0.0 and inside["contained"]
    assert inside["point"].z == pytest.approx(0.0) and inside["reference"] in reachable
    near_edge = em.snap_target(graph, Vec3(5.0, 250.0, 0.0), z_given=False, player=PLAYER, reachable=reachable,
                               labels=analysis.labels)
    assert near_edge["status"] == "ok"
    point = near_edge["point"]
    assert point.x >= em.EDGE_CLEARANCE_CM - 1e-6  # the polygon's -x wall is at x = 0
    assert near_edge["snap_horizontal_m"] == pytest.approx((point.x - 5.0) / 100.0, abs=0.02)


def test_a_target_off_the_mesh_snaps_to_the_nearest_floor_and_reports_the_distance(graph, analysis):
    reachable = em.reachable_references(graph, analysis.start_reference, analysis.labels)
    snapped = em.snap_target(graph, Vec3(-300.0, 750.0, 0.0), z_given=False, player=PLAYER, reachable=reachable,
                             labels=analysis.labels)
    assert snapped["status"] == "ok" and not snapped["contained"]
    assert snapped["snap_horizontal_m"] == pytest.approx(3.5, abs=0.1)  # 3 m to the wall plus the 0.5 m inset
    assert snapped["point"].x == pytest.approx(50.0, abs=1.0)


def test_a_target_beyond_the_snap_reach_is_refused_with_the_nearest_reachable_point(graph, analysis):
    reachable = em.reachable_references(graph, analysis.start_reference, analysis.labels)
    refused = em.snap_target(graph, Vec3(-3000.0, 750.0, 0.0), z_given=False, player=PLAYER, reachable=reachable,
                             labels=analysis.labels, max_snap_m=6.0)
    assert refused["status"] == "refused" and refused["reason"] == "target_off_navmesh"
    nearest = refused["nearest_reachable"]
    assert nearest["position"]["x"] == pytest.approx(50.0, abs=1.0)
    assert nearest["distance_m"] == pytest.approx(30.5, abs=0.2)


def test_a_target_on_another_island_is_refused_naming_the_island_and_the_nearest_reachable_point(graph, analysis):
    reachable = em.reachable_references(graph, analysis.start_reference, analysis.labels)
    refused = em.snap_target(graph, Vec3(750.0, 3750.0, 0.0), z_given=False, player=PLAYER, reachable=reachable,
                             labels=analysis.labels)
    assert refused["status"] == "refused" and refused["reason"] == "target_on_unreachable_island"
    assert refused["target_island"]["polygons"] == 9
    nearest = refused["nearest_reachable"]
    assert nearest["position"]["y"] == pytest.approx(1450.0, abs=1.0)  # the corridor's far wall, inset by 0.5 m
    assert nearest["distance_m"] == pytest.approx(23.0, abs=0.2)
    assert "door" in refused["message"]


def test_stacked_floors_are_told_apart_by_the_given_height_or_the_players(graph, analysis):
    reachable = em.reachable_references(graph, analysis.start_reference, analysis.labels)
    over_corridor = Vec3(1750.0, 250.0, 500.0)
    # no height given: the player's floor wins over the one 5 m above, which is another island
    no_height = em.snap_target(graph, over_corridor, z_given=False, player=PLAYER, reachable=reachable,
                               labels=analysis.labels)
    assert no_height["status"] == "ok" and no_height["point"].z == 0.0
    # the upper floor's height asked for: that floor is not reachable
    upper = em.snap_target(graph, over_corridor, z_given=True, player=PLAYER, reachable=reachable,
                           labels=analysis.labels)
    assert upper["status"] == "refused" and upper["reason"] == "target_on_unreachable_island"
    assert upper["target_island"]["polygons"] == 6
    # the ground's height asked for: fine
    ground = em.snap_target(graph, Vec3(1750.0, 250.0, 20.0), z_given=True, player=PLAYER, reachable=reachable,
                            labels=analysis.labels)
    assert ground["status"] == "ok" and ground["snap_vertical_m"] == pytest.approx(0.2, abs=0.01)


def test_the_distance_of_the_nearest_reachable_point_is_horizontal_and_exact(graph, analysis):
    reachable = em.reachable_references(graph, analysis.start_reference, analysis.labels)
    target = Vec3(2500.0, 3750.0, 0.0)
    nearest = em.nearest_reachable_point(graph, reachable, target)
    assert nearest["distance_m"] == pytest.approx(math.hypot(
        nearest["position"]["x"] - target.x, nearest["position"]["y"] - target.y) / 100.0, abs=0.01)
    assert em.nearest_reachable_point(graph, set(), target) is None


def test_an_explicit_height_does_not_select_a_reachable_floor_below_an_unreachable_one():
    graph = build_graph([{"cells": [(0, 0)], "z": 0.0}, {"cells": [(0, 0)], "z": 200.0}])
    labels = em.island_labels(graph)
    start = em.locate_player(graph, PLAYER).selected_reference
    reachable = em.reachable_references(graph, start)
    target = Vec3(250.0, 250.0, 200.0)
    explicit = em.snap_target(graph, target, z_given=True, player=PLAYER, reachable=reachable, labels=labels)
    assert explicit["reason"] == "target_on_unreachable_island"
    assert explicit["target_island"]["id"] != labels[start]
    implicit = em.snap_target(graph, target, z_given=False, player=PLAYER, reachable=reachable, labels=labels)
    assert implicit["status"] == "ok" and implicit["point"].z == 0.0


def test_nearest_reachable_suggestions_break_horizontal_ties_using_height():
    graph = build_graph([{"cells": [(0, 0)], "z": 0.0}, {"cells": [(0, 0)], "z": 200.0}])
    point = em.nearest_reachable_point(graph, set(graph.polygons), Vec3(250.0, 250.0, 200.0))
    assert point["position"]["z"] == 200.0
    lower = em.nearest_reachable_point(graph, set(graph.polygons), Vec3(250.0, 250.0, 200.0), reference_z=0.0)
    assert lower["position"]["z"] == 0.0


def test_path_lengths_fall_back_to_the_polygon_estimate_past_the_time_budget(graph, monkeypatch):
    monkeypatch.setattr(em, "PLAN_REFINEMENT_SECONDS", 0.0)
    late = em.analyse_view(graph, PLAYER, 0.0, radius_m=60.0)
    far = [c for c in late.candidates if c["kind"] == "far_end"]
    assert far and all(c["path_source"] == "estimate" for c in far)
    corner = max(far, key=lambda c: c["distance_m"])
    assert corner["path_m"] > corner["distance_m"]  # hops between centres: longer than the straight line
    other = [c for c in late.candidates if c["kind"] == "other_island"]
    assert all(c["path_m"] is None and c["path_source"] is None for c in other)


def test_the_height_ramp_spans_the_players_floors_not_a_far_plateau(graph, local_analysis):
    transform = em.make_transform(2500.0, 750.0, 40.0, 8.0)
    _, facts = em.render_view(graph, local_analysis, transform, radius_m=40.0)
    # the player's island is flat at z = 0 (the stacked floor 5 m up is another island): 1 m margin, 3 m minimum span
    assert (facts["z_min_cm"], facts["z_max_cm"]) == (-100.0, 200.0)
    # with the player off the mesh every floor in view sets the range
    off = em.analyse_view(graph, Vec3(-9000.0, 250.0, 0.0), None, centre=Vec3(2500.0, 750.0, 0.0), radius_m=40.0)
    _, facts_off = em.render_view(graph, off, transform, radius_m=40.0)
    assert (facts_off["z_min_cm"], facts_off["z_max_cm"]) == (0.0, 500.0)
