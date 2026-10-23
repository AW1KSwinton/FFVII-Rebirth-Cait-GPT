import json
from dataclasses import replace
from pathlib import Path
import pytest
from ff7r_agent.detour_route import DetourPolygon, DetourPortal, DetourTopologyGraph, PolygonReference
from ff7r_agent.static_obstacles import _segment_authorized_corridor, _segment_near_polygon_surface
from ff7r_agent.live_route_adapter import LiveAdapterConfig
from ff7r_agent.models import Vec3
from test_live_route_adapter import make_adapter, FakeFollower


def fixture_graph(index):
    data = json.loads((Path(__file__).parent/'fixtures/continuous-sloped-crossings.json').read_text())['crossings'][index]
    polygons = {}
    for raw in data['polygons']:
        ref = PolygonReference.from_value(raw['ref']['value'])
        polygons[ref.value] = DetourPolygon(ref, raw['tile_x'], raw['tile_y'], raw['layer'],
            tuple(Vec3(**v) for v in raw['vertices']), raw['flags'], raw['area'], raw['type'], (), raw['identity_source'])
    adjacency, portals = {}, []
    for raw in data['portals']:
        portal = DetourPortal(raw['from_ref'], raw['to_ref'], raw['link_ref'], raw['source_edge'], raw['target_edge'],
            Vec3(**raw['left']), Vec3(**raw['right']), raw['side'])
        adjacency.setdefault(portal.from_reference, []).append(portal)
        portals.append(portal)
    return data, DetourTopologyGraph(polygons, adjacency, snapshot_diagnostics={}), portals


@pytest.mark.parametrize('index', [1,2])
def test_observed_sloped_intervals_prove_local_height_and_reject_wrong_level(index):
    data, graph, portals = fixture_graph(index)
    first, last = Vec3(**data['previous']), Vec3(**data['current'])
    # This unrelated distant-vertex comparison used to reject these crossings.
    assert max(abs(v.z-first.z)/100 for p in graph.polygons.values() for v in p.vertices) > 1
    args = dict(graph=graph, corridor_refs=data['refs'], portals=portals,
        minimum_edge_clearance_m=0, max_vertical_separation_m=1)
    assert _segment_authorized_corridor(first, last, **args)
    assert not _segment_authorized_corridor(replace(first,z=first.z+200),replace(last,z=last.z+200), **args)
    assert not _segment_authorized_corridor(replace(first,x=first.x+500), last, **args)


def test_nonplanar_interior_height_cannot_hide_behind_valid_endpoints():
    _, graph, _ = fixture_graph(0)
    polygon = replace(next(iter(graph.polygons.values())), vertices=(
        Vec3(0,0,0), Vec3(1000,0,400), Vec3(1000,1000,0), Vec3(0,1000,400)))
    # Each end lies on the surface; the crossed diagonal lies 1.6m below.
    assert not _segment_near_polygon_surface(Vec3(500,100,160),Vec3(500,900,160),polygon,1)
    assert _segment_near_polygon_surface(Vec3(200,100,40),Vec3(400,100,120),polygon,1)


def test_directed_portal_crossing_can_have_equal_clamped_route_progress():
    data, graph, _ = fixture_graph(0)
    adapter, _, _, _ = make_adapter(config=LiveAdapterConfig(route_mode='cached_topology'))
    adapter.topology_session = replace(adapter.topology.topology, graph=graph)
    adapter.route_plan = {'waypoints':data['projection_waypoints'], 'polygon_corridor':data['refs'], 'portals':data['portals']}
    adapter.authorization = object()  # metrics need a bound authorization marker
    adapter.follower = FakeFollower(adapter.route_plan)
    adapter.follower.current_waypoint_index = 1
    adapter._route_local_corridor_index = 0
    adapter._route_local_corridor_ref = data['refs'][0]
    def sample(point, sequence):
        point = Vec3(**point)
        global_resolution = graph.resolve_point(point)
        return {'sequence':sequence, 'position':vars(point),'metrics':adapter._authorized_route_metrics(point),
            'corridor_resolution':graph.resolve_point_in_corridor(point,data['refs'],global_resolution=global_resolution)}
    previous, current = sample(data['previous'],1),sample(data['current'],2)
    assert previous['metrics']['along_route_m'] == current['metrics']['along_route_m'] == 0
    result = adapter._route_local_sample_assessment(previous,current)
    assert result['safe'],result
    assert result['reason_code'] == 'authorized_portal_transition_proven'
    current['metrics']['along_route_m'] = -0.1
    assert not adapter._route_local_sample_assessment(previous,current)['safe']
