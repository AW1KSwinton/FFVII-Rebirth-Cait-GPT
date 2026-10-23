import json
from collections import defaultdict, deque
from pathlib import Path

import pytest

from ff7r_agent.detour_route import (
    DetourLink,
    DetourPolygon,
    DetourTopologyGraph,
    DetourRouteDataError,
    PolygonReference,
    _portal_from_link,
    _is_shipping_dynamic_link,
    load_shipping_detour_topology,
)
from ff7r_agent.models import Vec3


SNAPSHOT = (
    Path(__file__).parents[3]
    / "ProjectKnowledge"
    / "Evidence"
    / "2026-08-17-shipping-detour-topology-live"
    / "navigation-native-geometry.json"
)


@pytest.fixture(scope="module")
def topology():
    return load_shipping_detour_topology(SNAPSHOT)


def _identity_payload(*, tile_index_valid=None, tile_index=3, salt=1, links=None):
    polygon = {
        "index": 0,
        "vertex_indices": [0, 1, 2, 3],
        "flags": 1,
        "area": 0,
        "type": 0,
        "links": list(links or []),
    }
    tile = {
        "x": 0,
        "y": 0,
        "layer": 0,
        "vertices": [
            {"x": 0, "y": 0, "z": 0},
            {"x": 100, "y": 0, "z": 0},
            {"x": 100, "y": 100, "z": 0},
            {"x": 0, "y": 100, "z": 0},
        ],
        "polygons": [polygon],
    }
    if tile_index_valid is not None:
        tile.update({"tile_index_valid": tile_index_valid, "tile_index": tile_index, "salt": salt})
    return {"source": "ue4ss_native_detour_geometry_dump", "movement_sent": False, "coordinate_space": "unreal", "tiles": [tile]}


def _shipping_ref(tile_index, poly_index=0, salt=1):
    return (salt << (14 + 31)) | (tile_index << 31) | poly_index


def test_direct_native_pool_identity_is_authoritative():
    graph = DetourTopologyGraph.from_dict(_identity_payload(tile_index_valid=True))
    polygon = next(iter(graph.polygons.values()))
    assert polygon.reference.tile_index == 3
    assert polygon.reference.salt == 1
    assert polygon.identity_source == "native_pool"
    assert graph.snapshot_diagnostics["identity_native_pool_tiles"] == 1


def test_matching_direct_and_internal_link_identity_is_accepted():
    link = {"ref": _shipping_ref(3), "edge": 0, "next": 0xFFFFFFFF, "side": 128, "bmin": 0, "bmax": 0}
    graph = DetourTopologyGraph.from_dict(_identity_payload(tile_index_valid=True, links=[link]))
    assert len(graph.polygons) == 1
    assert graph.snapshot_diagnostics["identity_disagreements"] == 0


def test_direct_and_link_identity_disagreement_fails_closed():
    link = {"ref": _shipping_ref(4), "edge": 0, "next": 0xFFFFFFFF, "side": 128, "bmin": 0, "bmax": 0}
    graph = DetourTopologyGraph.from_dict(_identity_payload(tile_index_valid=True, tile_index=3, links=[link]))
    assert graph.polygons == {}
    assert graph.snapshot_diagnostics["identity_disagreements"] == 1


def test_invalid_direct_identity_fails_closed_without_fallback():
    link = {"ref": _shipping_ref(3), "edge": 0, "next": 0xFFFFFFFF, "side": 128, "bmin": 0, "bmax": 0}
    graph = DetourTopologyGraph.from_dict(_identity_payload(tile_index_valid=True, tile_index=1 << 14, links=[link]))
    assert graph.polygons == {}
    assert graph.snapshot_diagnostics["identity_invalid_direct_tiles"] == 1


def test_historical_capture_uses_internal_link_fallback():
    link = {"ref": _shipping_ref(3), "edge": 0, "next": 0xFFFFFFFF, "side": 128, "bmin": 0, "bmax": 0}
    graph = DetourTopologyGraph.from_dict(_identity_payload(links=[link]))
    polygon = next(iter(graph.polygons.values()))
    assert polygon.identity_source == "inferred_internal_link"


def test_missing_identity_remains_unresolved():
    graph = DetourTopologyGraph.from_dict(_identity_payload())
    assert graph.polygons == {}
    assert graph.snapshot_diagnostics["identity_unresolved_tiles"] == 1


def test_shipping_dynamic_link_is_valid_but_never_an_ordinary_portal():
    dynamic = {"ref": _shipping_ref(3), "edge": 0xFF, "next": 0xFFFFFFFF, "side": 0x50, "bmin": 0, "bmax": 255}
    assert _is_shipping_dynamic_link(dynamic["edge"], dynamic["side"])
    graph = DetourTopologyGraph.from_dict(_identity_payload(tile_index_valid=True, links=[dynamic]))
    assert graph.snapshot_diagnostics["shipping_dynamic_links_valid"] == 1
    assert graph.snapshot_diagnostics["shipping_dynamic_links_route_semantics_unresolved"] == 1
    assert graph.snapshot_diagnostics["adjacency_edge_count"] == 0
    assert _is_shipping_dynamic_link(0xFF, 0xD0)
    assert not _is_shipping_dynamic_link(0xFF, 0x20)
    assert not _is_shipping_dynamic_link(0x01, 0x50)


def test_attempt027_edge_zero_and_edge_one_links_are_valid_unresolved_records():
    links = [
        {"ref": _shipping_ref(3, 1), "edge": 1, "next": 0xFFFFFFFF, "side": 0x60, "bmin": 0, "bmax": 0},
        {"ref": _shipping_ref(3, 2), "edge": 0, "next": 0xFFFFFFFF, "side": 0xF0, "bmin": 0, "bmax": 0},
        {"ref": _shipping_ref(3, 3), "edge": 0, "next": 0xFFFFFFFF, "side": 0xD0, "bmin": 0, "bmax": 0},
    ]
    assert _is_shipping_dynamic_link(1, 0x60, 0, 0)
    assert _is_shipping_dynamic_link(0, 0xF0, 0, 0)
    assert _is_shipping_dynamic_link(0, 0xD0, 0, 0)
    graph = DetourTopologyGraph.from_dict(_identity_payload(tile_index_valid=True, links=links))
    assert graph.snapshot_diagnostics["shipping_dynamic_links_valid"] == 3
    assert graph.snapshot_diagnostics["shipping_dynamic_edge_one_valid"] == 1
    assert graph.snapshot_diagnostics["shipping_dynamic_edge_zero_valid"] == 2
    assert graph.snapshot_diagnostics["serialized_shipping_dynamic_links_valid"] == 3
    assert graph.snapshot_diagnostics["serialized_shipping_dynamic_links_by_subtype"] == {
        "edge_one": 1,
        "edge_zero": 2,
    }
    assert graph.snapshot_diagnostics["adjacency_edge_count"] == 0


def test_edge_zero_and_edge_one_malformed_shipping_records_fail_closed():
    with pytest.raises(DetourRouteDataError, match="invalid_shipping_dynamic_subtype"):
        DetourLink.from_dict({"ref": _shipping_ref(3), "edge": 0, "next": 0xFFFFFFFF, "side": 0xD0, "bmin": 1, "bmax": 0})
    with pytest.raises(DetourRouteDataError, match="invalid_shipping_dynamic_subtype"):
        DetourLink.from_dict({"ref": _shipping_ref(3), "edge": 1, "next": 0xFFFFFFFF, "side": 0x60, "bmin": 0, "bmax": 1})
    assert not _is_shipping_dynamic_link(0, 0x40, 0, 0)
    assert not _is_shipping_dynamic_link(1, 0x50, 0, 0)


def test_preserved_attempt009_dynamic_samples_accept_observed_50_and_d0_only():
    samples = json.loads(
        (SNAPSHOT).read_text(encoding="utf-8")
    )["candidate_a0_link_diagnostic_samples"]
    observed_sides = {int(sample["side"]) for sample in samples if int(sample["edge"]) == 0xFF}
    assert 0x50 in observed_sides
    assert 0xD0 in observed_sides
    assert all(_is_shipping_dynamic_link(int(sample["edge"]), int(sample["side"])) for sample in samples)
    assert not _is_shipping_dynamic_link(0xFF, 0x20)
    assert not _is_shipping_dynamic_link(0xFE, 0x50)


def _undirected_components(topology):
    adjacency = defaultdict(set)
    for source, portals in topology.adjacency.items():
        for portal in portals:
            adjacency[source].add(portal.to_reference)
            adjacency[portal.to_reference].add(source)
    seen = set()
    components = []
    for reference in sorted(topology.polygons):
        if reference in seen:
            continue
        queue = deque([reference])
        seen.add(reference)
        component = []
        while queue:
            current = queue.popleft()
            component.append(current)
            for neighbor in sorted(adjacency[current]):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        components.append(component)
    return sorted(components, key=lambda value: (-len(value), value[0]))


def test_snapshot_builds_identity_bound_graph_without_memory_addresses(topology):
    diagnostics = topology.snapshot_diagnostics
    assert diagnostics["snapshot_status"] == "partial"
    assert diagnostics["identity_resolved_tile_records"] == 768
    assert diagnostics["identity_unresolved_tile_records"] == 171
    assert diagnostics["resolved_polygon_count"] == 3831
    assert diagnostics["adjacency_edge_count"] == 6151
    assert diagnostics["portal_rejections"] == 0
    assert diagnostics["coordinate_space"] == "recast"
    assert all("0x" not in str(reference) for reference in topology.polygons)


def test_same_tile_route_uses_proven_link_and_portal(topology):
    start_ref = 35506494636032
    end_ref = 35506494636033
    assert topology.polygons[start_ref].reference.tile_index == topology.polygons[end_ref].reference.tile_index

    plan = topology.plan(
        topology.polygons[start_ref].centroid,
        topology.polygons[end_ref].centroid,
    )

    assert plan.status == "complete"
    assert plan.reason_code == "route_plan_complete"
    assert plan.polygon_corridor == (start_ref, end_ref)
    assert plan.validation["tile_crossings"] == 0
    assert len(plan.portals) == 3
    assert plan.portals[1].from_reference == start_ref
    assert plan.portals[1].to_reference == end_ref
    assert plan.validation["transition_portals_valid"] is True
    assert plan.movement_sent is False


def test_representative_long_route_crosses_multiple_tiles(topology):
    # Fixed refs are deliberately selected from the preserved raw evidence,
    # not synthesized from a preferred/human route.
    start_ref = 37218039103488
    end_ref = 37565931454465
    plan = topology.plan(
        topology.polygons[start_ref].centroid,
        topology.polygons[end_ref].centroid,
    )

    assert plan.status == "complete"
    assert plan.start_ref == start_ref
    assert plan.end_ref == end_ref
    assert len(plan.polygon_corridor) >= 10
    assert plan.validation["tile_crossings"] >= 1
    assert len(plan.waypoints) >= 2
    assert plan.total_estimated_path_length_m is not None
    assert plan.total_estimated_path_length_m > 0.0
    assert plan.to_dict()["source"] == "shipping_detour"

    for first, second, portal in zip(
        plan.polygon_corridor,
        plan.polygon_corridor[1:],
        plan.portals[1:-1],
    ):
        assert portal.from_reference == first
        assert portal.to_reference == second
        assert portal.link_reference in {
            link.reference for link in topology.polygons[first].links
        }
        assert all(
            all(isinstance(value, (float, int)) and value == value for value in (point.x, point.y, point.z))
            for point in (portal.left, portal.right)
        )

    assert plan.waypoints[0] == plan.resolved_start
    assert plan.waypoints[-1] == plan.resolved_end
    assert plan.validation["waypoints_finite"] is True
    assert plan.validation["movement_sent"] is False


def test_route_plan_is_machine_readable_and_has_no_control_fields(topology):
    plan = topology.plan(
        topology.polygons[37218039103488].centroid,
        topology.polygons[37565931454465].centroid,
    )
    encoded = json.loads(plan.to_json())

    assert encoded["schema_version"] == 1
    assert encoded["source"] == "shipping_detour"
    assert encoded["executable_sha256"] == "37D952FABBFA2B710F8AE6490997C9755E4E57B4FD545078C9BAAF46C9B233D7"
    assert encoded["movement_sent"] is False
    assert encoded["status"] == "complete"
    assert encoded["reason_code"] == "route_plan_complete"
    assert encoded["polygon_corridor"]
    assert encoded["portals"]
    assert encoded["waypoints"]
    assert "input" not in encoded
    assert "mcp" not in encoded


def test_world_point_resolution_uses_footprint_and_vertical_layer(topology):
    high_ref = 40119289511938
    low_ref = 40104257126400
    high = topology.polygons[high_ref].centroid
    low = topology.polygons[low_ref].centroid
    assert abs(high.x - low.x) < 100.0
    assert abs(high.y - low.y) < 100.0
    assert abs(high.z - low.z) > 500.0

    high_resolution = topology.resolve_point(high)
    low_resolution = topology.resolve_point(low)

    assert high_resolution.ok
    assert low_resolution.ok
    assert high_resolution.selected_reference == high_ref
    assert low_resolution.selected_reference == low_ref
    assert high_resolution.reason_code == "point_inside_polygon"
    assert low_resolution.reason_code == "point_inside_polygon"


def test_recovery_localization_control_is_unambiguous_inside_authorized_topology():
    """The first live recovery position remains a strict interior binding."""
    point = Vec3(-25996.0, -227054.03125, 5368.852450359104)
    fixture = DetourTopologyGraph(
        {
            72350871584774: _synthetic_polygon(
                72350871584774,
                (
                    Vec3(-26050.0, -227100.0, 5368.852450359104),
                    Vec3(-25900.0, -227100.0, 5368.852450359104),
                    Vec3(-25900.0, -227000.0, 5368.852450359104),
                    Vec3(-26050.0, -227000.0, 5368.852450359104),
                ),
            )
        },
        {},
        snapshot_diagnostics={"fixture": "recorded_first_recovery_interior"},
    )

    resolution = fixture.resolve_point(
        point,
        max_snap_distance_m=0.75,
        max_vertical_separation_m=1.0,
    )
    candidates = fixture.resolve_point_candidates(
        point,
        max_snap_distance_m=0.75,
        max_vertical_separation_m=1.0,
    )

    assert resolution.reason_code == "point_inside_polygon"
    assert resolution.selected_reference == 72350871584774
    assert resolution.horizontal_distance_m == 0.0
    assert resolution.vertical_distance_m == 0.0
    assert [candidate.reference for candidate in candidates] == [72350871584774]
    assert candidates[0].contained is True


def test_recovery_localization_corner_ambiguity_remains_fail_closed():
    """The second live recovery predecessor is not safely bindable by snapping."""
    point = Vec3(-26340.4375, -226786.125, 5497.19043)
    fixture = DetourTopologyGraph(
        {
            72355166552066: _synthetic_polygon(
                72355166552066,
                (
                    Vec3(-26385.0, -226695.0, 5410.0),
                    Vec3(-26385.0, -226650.0, 5400.0),
                    Vec3(-26370.0, -226605.0, 5400.0),
                    Vec3(-26370.0, -226770.0, 5410.0),
                ),
            ),
            72355166552069: _synthetic_polygon(
                72355166552069,
                (
                    Vec3(-26460.0, -226725.0, 5470.0),
                    Vec3(-26385.0, -226695.0, 5410.0),
                    Vec3(-26370.0, -226770.0, 5410.0),
                    Vec3(-26460.0, -226875.0, 5470.0),
                ),
            ),
        },
        {},
        snapshot_diagnostics={"fixture": "recorded_second_recovery_shared_corner"},
    )

    indexed = fixture.resolve_point(
        point,
        max_snap_distance_m=0.75,
        max_vertical_separation_m=1.0,
    )
    full_scan = fixture.resolve_point_full_scan(
        point,
        max_snap_distance_m=0.75,
        max_vertical_separation_m=1.0,
    )
    candidates = fixture.resolve_point_candidates(
        point,
        max_snap_distance_m=0.75,
        max_vertical_separation_m=1.0,
    )

    assert indexed.status == full_scan.status
    assert indexed.reason_code == full_scan.reason_code
    assert indexed.selected_reference == full_scan.selected_reference
    assert indexed.closest_point == full_scan.closest_point
    assert indexed.horizontal_distance_m == full_scan.horizontal_distance_m
    assert indexed.vertical_distance_m == full_scan.vertical_distance_m
    assert indexed.candidate_count == full_scan.candidate_count
    assert indexed.status == "resolved"
    assert indexed.reason_code == "point_snapped_to_polygon"
    assert indexed.selected_reference == 72355166552066
    assert indexed.horizontal_distance_m == pytest.approx(0.3367427848)
    assert indexed.vertical_distance_m == pytest.approx(0.8719043)
    assert len(candidates) == 2
    assert [candidate.reference for candidate in candidates] == [
        72355166552066,
        72355166552069,
    ]
    assert all(candidate.contained is False for candidate in candidates)
    assert all(candidate.horizontal_distance_m == pytest.approx(0.3367427848) for candidate in candidates)
    assert all(candidate.vertical_distance_m == pytest.approx(0.8719043) for candidate in candidates)


def test_indexed_point_resolution_matches_full_scan_safety_decision(topology):
    """The live spatial prefilter must preserve the full resolver decision."""
    references = [
        40119289511938,
        40104257126400,
        37218039103488,
        37565931454465,
    ]
    points = [topology.polygons[reference].centroid for reference in references]
    points.append(Vec3(0.0, 0.0, 0.0))

    for point in points:
        indexed = topology.resolve_point(point)
        full_scan = topology.resolve_point_full_scan(point)
        assert indexed.status == full_scan.status
        assert indexed.reason_code == full_scan.reason_code
        assert indexed.selected_reference == full_scan.selected_reference
        assert indexed.closest_point == full_scan.closest_point
        assert indexed.horizontal_distance_m == full_scan.horizontal_distance_m
        assert indexed.vertical_distance_m == full_scan.vertical_distance_m
        assert indexed.candidate_count <= full_scan.candidate_count
        assert indexed.diagnostics.get("candidate_count_scope") in {
            "indexed_spatial_superset",
            "full_scan",
        }
        assert full_scan.diagnostics.get("candidate_count_scope") == "full_scan"


def test_invalid_world_point_fails_closed(topology):
    plan = topology.plan(Vec3(0.0, 0.0, 0.0), topology.polygons[37218039103488].centroid)

    assert plan.status == "failed"
    assert plan.reason_code == "start_polygon_unresolved"
    assert plan.movement_sent is False
    assert plan.polygon_corridor == ()


def test_graph_has_a_real_multi_tile_component(topology):
    components = _undirected_components(topology)
    candidates = [
        component for component in components
        if len(component) >= 10
        and len({topology.polygons[ref].reference.tile_index for ref in component}) >= 3
    ]
    assert candidates
    assert max(len(component) for component in candidates) >= 30


def _synthetic_polygon(reference, vertices, links=()):
    return DetourPolygon(
        reference=PolygonReference.from_value(reference),
        tile_x=0,
        tile_y=0,
        layer=0,
        vertices=tuple(vertices),
        flags=1,
        area=1,
        poly_type=0,
        links=tuple(links),
        identity_source="test",
    )


def test_external_portal_uses_clipped_source_edge_not_full_shared_edge():
    source = _synthetic_polygon(
        1,
        (Vec3(0, 0, 0), Vec3(100, 0, 0), Vec3(100, 100, 0), Vec3(0, 100, 0)),
    )
    target = _synthetic_polygon(
        2,
        (Vec3(100, 0, 0), Vec3(200, 0, 0), Vec3(200, 100, 0), Vec3(100, 100, 0)),
    )
    link = DetourLink(reference=2, edge=1, next_index=0xFFFFFFFF, side=2, bmin=64, bmax=192)

    portal = _portal_from_link(source, target, link, tolerance_cm=1.0)

    assert portal is not None
    assert portal.target_edge == 3  # consistency only; source edge defines endpoints
    actual = {(round(portal.left.x, 6), round(portal.left.y, 6)), (round(portal.right.x, 6), round(portal.right.y, 6))}
    expected = {(100.0, 64.0 / 255.0 * 100.0), (100.0, 192.0 / 255.0 * 100.0)}
    assert actual == {(round(x, 6), round(y, 6)) for x, y in expected}
    assert actual != {(100.0, 0.0), (100.0, 100.0)}


def test_external_full_width_portal_uses_entire_source_edge():
    source = _synthetic_polygon(
        1,
        (Vec3(0, 0, 0), Vec3(100, 0, 0), Vec3(100, 100, 0), Vec3(0, 100, 0)),
    )
    target = _synthetic_polygon(
        2,
        (Vec3(100, 0, 0), Vec3(200, 0, 0), Vec3(200, 100, 0), Vec3(100, 100, 0)),
    )
    link = DetourLink(reference=2, edge=1, next_index=0xFFFFFFFF, side=2, bmin=0, bmax=255)

    portal = _portal_from_link(source, target, link, tolerance_cm=1.0)

    assert portal is not None
    assert {(portal.left.x, portal.left.y), (portal.right.x, portal.right.y)} == {
        (100, 0),
        (100, 100),
    }


def test_invalid_source_edge_or_portal_bounds_fail_closed():
    source = _synthetic_polygon(
        1,
        (Vec3(0, 0, 0), Vec3(100, 0, 0), Vec3(100, 100, 0), Vec3(0, 100, 0)),
    )
    target = _synthetic_polygon(
        2,
        (Vec3(100, 0, 0), Vec3(200, 0, 0), Vec3(200, 100, 0), Vec3(100, 100, 0)),
    )
    assert _portal_from_link(
        source,
        target,
        DetourLink(reference=2, edge=9, next_index=0xFFFFFFFF, side=2, bmin=0, bmax=255),
        tolerance_cm=1.0,
    ) is None
    assert _portal_from_link(
        source,
        target,
        DetourLink(reference=2, edge=1, next_index=0xFFFFFFFF, side=2, bmin=200, bmax=100),
        tolerance_cm=1.0,
    ) is None
    assert _portal_from_link(
        source,
        target,
        DetourLink(reference=2, edge=1, next_index=0xFFFFFFFF, side=1, bmin=0, bmax=255),
        tolerance_cm=1.0,
    ) is None


def test_same_polygon_snapped_endpoints_use_resolved_positions():
    polygon = _synthetic_polygon(
        1,
        (Vec3(0, 0, 0), Vec3(100, 0, 0), Vec3(100, 100, 0), Vec3(0, 100, 0)),
    )
    graph = DetourTopologyGraph({1: polygon}, {}, snapshot_diagnostics={})
    requested_start = Vec3(120, 50, 0)
    requested_end = Vec3(-20, 50, 0)

    plan = graph.plan(requested_start, requested_end)

    assert plan.status == "complete"
    assert plan.requested_start == requested_start
    assert plan.requested_end == requested_end
    assert plan.waypoints[0] == plan.resolved_start == Vec3(100, 50, 0)
    assert plan.waypoints[-1] == plan.resolved_end == Vec3(0, 50, 0)
    assert plan.waypoints != (requested_start, requested_end)


def test_endpoint_mismatch_cannot_return_complete(monkeypatch):
    source_link = DetourLink(reference=2, edge=1, next_index=0xFFFFFFFF, side=128, bmin=0, bmax=0)
    source = _synthetic_polygon(
        1,
        (Vec3(0, 0, 0), Vec3(100, 0, 0), Vec3(100, 100, 0), Vec3(0, 100, 0)),
        (source_link,),
    )
    target = _synthetic_polygon(
        2,
        (Vec3(100, 0, 0), Vec3(200, 0, 0), Vec3(200, 100, 0), Vec3(100, 100, 0)),
    )
    portal = _portal_from_link(source, target, source_link, tolerance_cm=1.0)
    assert portal is not None
    graph = DetourTopologyGraph({1: source, 2: target}, {1: (portal,)}, snapshot_diagnostics={})
    monkeypatch.setattr(graph, "_funnel", lambda portals: (Vec3(999, 999, 0), Vec3(1000, 1000, 0)))

    plan = graph.plan(Vec3(50, 50, 0), Vec3(150, 50, 0))

    assert plan.status == "failed"
    assert plan.reason_code == "waypoint_endpoint_validation_failed"
    assert plan.validation["waypoint_start_matches_resolved"] is False
    assert plan.validation["waypoint_end_matches_resolved"] is False



def test_corridor_resolution_index_preserves_adversarial_semantics():
    first_ref = _shipping_ref(3, 10)
    second_ref = _shipping_ref(3, 11)
    overlap_ref = _shipping_ref(3, 12)
    non_corridor_ref = _shipping_ref(3, 13)
    first = _synthetic_polygon(first_ref, (
        Vec3(0.0, 0.0, 0.0), Vec3(100.0, 0.0, 0.0),
        Vec3(100.0, 100.0, 0.0), Vec3(0.0, 100.0, 0.0),
    ))
    second = _synthetic_polygon(second_ref, (
        Vec3(100.0, 0.0, 0.0), Vec3(200.0, 0.0, 0.0),
        Vec3(200.0, 100.0, 0.0), Vec3(100.0, 100.0, 0.0),
    ))
    overlap = _synthetic_polygon(overlap_ref, (
        Vec3(50.0, 0.0, 0.0), Vec3(150.0, 0.0, 0.0),
        Vec3(150.0, 100.0, 0.0), Vec3(50.0, 100.0, 0.0),
    ))
    # Same XY footprint as the first corridor polygon, but 10 cm lower. A point
    # on this lower polygon is still XY-contained by the authorized polygon,
    # while the unrestricted resolver correctly prefers the zero-vertical-error
    # non-corridor polygon. This exercises the existing fail-closed ambiguity.
    non_corridor = _synthetic_polygon(non_corridor_ref, (
        Vec3(0.0, 0.0, -10.0), Vec3(100.0, 0.0, -10.0),
        Vec3(100.0, 100.0, -10.0), Vec3(0.0, 100.0, -10.0),
    ))
    graph = DetourTopologyGraph(
        {p.reference.value: p for p in (first, second, overlap, non_corridor)},
        {},
        snapshot_diagnostics={"fixture": "corridor_index_adversarial"},
    )

    boundary = graph.resolve_point_in_corridor(
        Vec3(100.0, 50.0, 0.0), (first_ref, second_ref),
        max_snap_distance_m=0.75, max_vertical_separation_m=1.0,
    )
    assert boundary.status == "ambiguous"
    assert boundary.reason_code == "authorized_shared_boundary_or_overlap"
    assert {candidate.reference for candidate in boundary.candidates} == {first_ref, second_ref}

    overlap_result = graph.resolve_point_in_corridor(
        Vec3(75.0, 50.0, 0.0), (first_ref, overlap_ref),
        max_snap_distance_m=0.75, max_vertical_separation_m=1.0,
    )
    assert overlap_result.status == "ambiguous"
    assert overlap_result.reason_code == "authorized_shared_boundary_or_overlap"

    global_non_corridor = graph.resolve_point(
        Vec3(50.0, 50.0, -10.0),
        max_snap_distance_m=0.75, max_vertical_separation_m=1.0,
    )
    assert global_non_corridor.selected_reference == non_corridor_ref
    ambiguous_contained = graph.resolve_point_in_corridor(
        Vec3(50.0, 50.0, -10.0), (first_ref,),
        max_snap_distance_m=0.75, max_vertical_separation_m=1.0,
        global_resolution=global_non_corridor,
    )
    assert ambiguous_contained.status == "ambiguous"
    assert ambiguous_contained.reason_code == "global_non_corridor_with_authorized_candidate"

    at_radius = graph.resolve_point_in_corridor(
        Vec3(175.0, 50.0, 0.0), (first_ref,),
        max_snap_distance_m=0.75, max_vertical_separation_m=1.0,
    )
    assert at_radius.status == "resolved"
    assert at_radius.reason_code == "within_authorized_snap"
    outside_radius = graph.resolve_point_in_corridor(
        Vec3(175.01, 50.0, 0.0), (first_ref,),
        max_snap_distance_m=0.75, max_vertical_separation_m=1.0,
    )
    assert outside_radius.status == "unresolved"
    assert outside_radius.reason_code == "no_authorized_corridor_candidate"


def test_corridor_resolution_index_preserves_overflow_and_duplicate_index_semantics():
    huge_ref = _shipping_ref(3, 20)
    huge = _synthetic_polygon(huge_ref, (
        Vec3(0.0, 0.0, 0.0), Vec3(10000.0, 0.0, 0.0),
        Vec3(10000.0, 10000.0, 0.0), Vec3(0.0, 10000.0, 0.0),
    ))
    graph = DetourTopologyGraph(
        {huge_ref: huge}, {}, snapshot_diagnostics={"fixture": "corridor_index_overflow"}
    )
    assert huge_ref in graph._point_resolution_index_overflow
    result = graph.resolve_point_in_corridor(
        Vec3(5000.0, 5000.0, 0.0), (huge_ref, huge_ref),
        max_snap_distance_m=0.75, max_vertical_separation_m=1.0,
    )
    assert result.status == "resolved"
    assert result.selected_reference == huge_ref
    assert result.corridor_index == 1
    assert len(result.candidates) == 1


def _refuse_full_scan(graph, monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("full polygon scan for a point nothing can resolve")
    monkeypatch.setattr(graph, "resolve_point_full_scan", refuse)


def test_resolvable_only_resolution_matches_resolve_point_whenever_it_resolves(topology):
    """Callers that use only resolved points get exactly the same ones."""
    points = []
    for reference in sorted(topology.polygons)[:12]:
        polygon = topology.polygons[reference]
        points.append(polygon.centroid)
        vertex = polygon.vertices[0]
        points.append(Vec3(vertex.x + 3.0, vertex.y - 3.0, vertex.z + 20.0))
        points.append(Vec3(vertex.x - 400.0, vertex.y + 400.0, vertex.z))
    points.append(Vec3(0.0, 0.0, 0.0))
    resolved = 0
    for point in points:
        for snap_m in (0.0, 0.05, 0.75):
            expected = topology.resolve_point(point, max_snap_distance_m=snap_m, max_vertical_separation_m=1.5)
            actual = topology.resolve_point_if_resolvable(point, max_snap_distance_m=snap_m,
                                                          max_vertical_separation_m=1.5)
            assert actual.ok == expected.ok
            if expected.ok:
                resolved += 1
                assert actual == expected
    assert resolved > 12


def test_resolvable_only_resolution_skips_the_full_scan_off_the_mesh(topology, monkeypatch):
    """Live at Kalm the scan named the nearest of 23,851 polygons, 0.8 s a
    time, for contour nodes that could never resolve."""
    point = Vec3(0.0, 0.0, 0.0)
    assert topology.resolve_point(point, max_snap_distance_m=0.0).diagnostics["candidate_count_scope"] == "full_scan"
    _refuse_full_scan(topology, monkeypatch)
    result = topology.resolve_point_if_resolvable(point, max_snap_distance_m=0.0)
    assert not result.ok
    assert result.reason_code == "point_outside_navmesh"
    assert result.selected_reference is None


def test_resolvable_only_resolution_keeps_points_contained_within_tolerance(monkeypatch):
    """Containment has a barycentric tolerance, so a point just outside a
    polygon's bounding box, in a cell the index leaves empty, still resolves
    through the full scan; one beyond the tolerance skips it."""
    # 60 index cells, so indexed rather than overflow; its left edge is a
    # cell boundary, so x = -2 falls in an empty cell.
    rectangle = _synthetic_polygon(
        7,
        (Vec3(0.0, 0.0, 0.0), Vec3(2490.0, 0.0, 0.0), Vec3(2490.0, 1490.0, 0.0), Vec3(0.0, 1490.0, 0.0)),
    )
    graph = DetourTopologyGraph({7: rectangle}, {}, snapshot_diagnostics={})
    assert graph._point_resolution_index_overflow == ()
    inside_tolerance = Vec3(-2.0, 1000.0, 0.0)
    assert graph._point_resolution_references(inside_tolerance, 0.0) == ()
    expected = graph.resolve_point(inside_tolerance, max_snap_distance_m=0.0)
    assert expected.reason_code == "point_inside_polygon" and expected.selected_reference == 7
    assert graph.resolve_point_if_resolvable(inside_tolerance, max_snap_distance_m=0.0) == expected

    _refuse_full_scan(graph, monkeypatch)
    beyond = graph.resolve_point_if_resolvable(Vec3(-10.0, 1000.0, 0.0), max_snap_distance_m=0.0)
    assert not beyond.ok
