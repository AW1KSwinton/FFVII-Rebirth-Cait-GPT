import asyncio
import copy
import json
import time
from pathlib import Path
import pytest
from ff7r_agent.map_poi import catalog_entry, merge_poi_snapshot, resolved_point
from ff7r_agent.poi_mission import PoiInteractionMission, PoiMissionConfig, acquire_poi_entity
from ff7r_agent.navigation_mission import MissionError
from test_entity_mission import EntityRuntime, snapshot


def catalog(tmp_path, rows="0,MapIcon_Test,placed_point,0,0,0,0,,town,0\n"):
    path = tmp_path / 'MapIconInfo.csv'
    path.write_text('Index,Name,ObjectID,OffsetX,OffsetY,OffsetZ,HeightFromOffset,SocketName,AffiliationName,NaviMapLayerIndex\n'+rows)
    return path


def test_catalog_uses_identity_and_source_provenance(tmp_path):
    entry = catalog_entry('MapIcon_Test', catalog(tmp_path))
    assert entry['object_id'] == 'placed_point'
    assert entry['source_row_number'] == 2 and entry['row_index'] == '0'
    assert len(entry['catalog_sha256']) == 64 and 'position' not in entry


@pytest.mark.parametrize('rows', ['', '0,MapIcon_Test,,0,0,0,0,,town,0\n',
    '0,MapIcon_Test,p,0,0,0,0,,town,0\n1,MapIcon_Test,q,0,0,0,0,,town,0\n',
    '0,MapIcon_Test,p,nan,0,0,0,,town,0\n', '0,MapIcon_Test,p,20,0,0,0,,town,0\n',
    '0,MapIcon_Test,p,0,0,0,0,socket,town,0\n'])
def test_catalog_rejects_unknown_ambiguous_and_unsupported_placements(tmp_path, rows):
    with pytest.raises(MissionError):
        catalog_entry('MapIcon_Test', catalog(tmp_path, rows))


def test_an_icon_lifted_above_its_object_still_names_the_object(tmp_path):
    # Live 2026-10-20: the Mt. Nibel air purification unit's icon, HeightFromOffset 30.
    entry = catalog_entry('MapIcon_Test', catalog(tmp_path, '0,MapIcon_Test,unit_point,0,0,0,30,,town,0' + chr(10)))
    assert entry['object_id'] == 'unit_point' and entry['icon_height_from_offset'] == 30.0
    with pytest.raises(MissionError, match='poi_offset_invalid'):
        catalog_entry('MapIcon_Test', catalog(tmp_path, '0,MapIcon_Test,p,0,0,0,nan,,town,0' + chr(10)))


def fixture():
    sample = snapshot(sequence=10)
    entry = dict(id='MapIcon_Test', object_id='point', offset_cm=dict(x=0,y=0,z=0))
    position = dict(x=600,y=0,z=65)
    observed = dict(source='ue4ss_entity_poi', request_id='request', source_sequence=7,
        map_id='EndGame', player_actor_id=sample['player']['actor_id'], player_position=sample['player']['position'],
        timestamp_utc='2026-09-09T12:00:00Z', nearby_actors=[],
        poi=dict(id=entry['id'], object_id='point', position=position, layout_position=position,
                 coordinate_source='EndFieldAPI.GetEndTargetPointLocation'))
    sample['map_state'] = dict(poi_observation=observed)
    return sample, entry, observed


def test_two_independent_getters_supply_world_coordinates():
    sample, entry, _ = fixture()
    result = resolved_point(sample, entry, 'request')
    assert result['position']['x'] == 600
    assert result['coordinate_source'].startswith('EndFieldAPI')


@pytest.mark.parametrize('mutation', [lambda s,o:o.update(request_id='old'),
    lambda s,o:o['poi'].update(object_id='other'), lambda s,o:o['poi'].update(position=dict(x=0,y=0,z=0)),
    lambda s,o:o['poi'].update(layout_position=dict(x=620,y=0,z=65)),
    lambda s,o:o['poi'].update(position=dict(x=float('nan'),y=0,z=65)),
    lambda s,o:s['telemetry'].update(age_seconds=2)])
def test_coordinate_identity_and_freshness_are_required(mutation):
    sample, entry, observed = fixture();mutation(sample,observed)
    with pytest.raises(MissionError): resolved_point(sample,entry,'request')


def write_sidecar(tmp_path, base, observed):
    (tmp_path/'entity-poi.1.json').write_text(json.dumps(observed))
    (tmp_path/'entity-poi.request').write_text(f'request|{int(time.time())+60}|EndGame|point|MapIcon_Test')


def merger_fixture():
    sample, _, observed = fixture()
    base = dict(source_sequence=7,source_timestamp_utc=observed['timestamp_utc'],
                game=dict(map_id='EndGame'),player=copy.deepcopy(sample['player']))
    return base,observed


def test_snapshot_merge_uses_exact_source_sequence_not_output_counter(tmp_path):
    base,observed=merger_fixture();base['sequence']=999
    write_sidecar(tmp_path,base,observed);merge_poi_snapshot(base,tmp_path)
    assert base['map_state']['poi_observation']==observed
    assert base['nearby_entities_complete'] is False


@pytest.mark.parametrize('mutation', [lambda o:o.update(source_sequence=9), lambda o:o.update(source_sequence=True),
    lambda o:o.update(timestamp_utc='old'),lambda o:o.update(player_actor_id='other'),
    lambda o:o.update(map_id='past'),lambda o:o.update(request_id='expired'),
    lambda o:o.update(player_position=dict(x=900,y=0,z=65)),lambda o:o.update(nearby_actors={})])
def test_sidecar_mismatch_never_overrides_telemetry(tmp_path,mutation):
    base,observed=merger_fixture();mutation(observed)
    write_sidecar(tmp_path,base,observed);merge_poi_snapshot(base,tmp_path)
    assert 'map_state' not in base and 'nearby_actors' not in base


class PoiRuntime(EntityRuntime):
    def __init__(self,tmp_path):
        super().__init__(tmp_path)
        _,self.entry,self.observed=fixture()
        self.poi=dict(self.entry,position=copy.deepcopy(self.observed['poi']['position']),map_id='EndGame',request_id='request')
        self.telemetry.change_before=self.add_poi
        self.released=False
    def add_poi(self,state):
        state['map_state']=dict(poi_observation=copy.deepcopy(self.observed))
        state['nearby_interactables'][0]['label']='town_scholar'
    async def call(self,op,args):
        if op=='observe_map_poi':return dict(ok=True,poi=self.poi,sample=self.service.observe())
        if op=='release_map_poi':self.released=True;return dict(ok=True,released=True)
        return await super().call(op,args)


def run_poi(runtime,tmp_path,**options):
    config=PoiMissionConfig(poi_id='MapIcon_Test',target_kind='npc',actor_name='town_scholar',
        **dict(execute=options.pop('execute',True),timeout_seconds=60,**options))
    return asyncio.run(PoiInteractionMission(runtime.call,tmp_path/'mission',config=config,
        identity_check=lambda *a:dict(ok=True),clock=lambda:runtime.time,sleep=runtime.sleep,
        progress=lambda _:None).run())


def test_poi_mission_reacquires_actual_actor_then_interacts_once(tmp_path):
    runtime=PoiRuntime(tmp_path);result=run_poi(runtime,tmp_path)
    assert result['ok'], result.get('reason')
    assert [p['x'] for p in runtime.route_goals]==[200,600,875]
    assert result['poi_to_entity_distance_m']==4
    assert runtime.backend.actions==[('interaction',250)] and runtime.released
    assert not runtime.service._session_lease.status()
    assert result['state']=='INTERACTION_VERIFIED'


def test_poi_dry_run_never_moves_or_interacts_and_releases_request(tmp_path):
    runtime=PoiRuntime(tmp_path);result=run_poi(runtime,tmp_path,execute=False)
    assert result['ok'] and result['state']=='PREFLIGHT_PASSED'
    assert not runtime.backend.actions and runtime.telemetry.x==0 and runtime.released


@pytest.mark.parametrize('failure',['missing','ambiguous','moved_poi','stale','wrong_actor'])
def test_poi_handoff_failures_never_interact(tmp_path,failure):
    runtime=PoiRuntime(tmp_path)
    def after_route(r):
        def change(s):
            r.add_poi(s)
            if failure=='missing':s['nearby_interactables']=[]
            if failure=='ambiguous':
                other=copy.deepcopy(s['nearby_interactables'][0]);other['id']='duplicate';s['nearby_interactables'].append(other)
            if failure=='moved_poi':
                s['map_state']['poi_observation']['poi']['position']['x']+=100
                s['map_state']['poi_observation']['poi']['layout_position']['x']+=100
            if failure=='stale':s['timestamp_utc']='2000-01-01T00:00:00+00:00'
            if failure=='wrong_actor':s['nearby_interactables'][0]['label']='other'
        r.telemetry.change_before=change
    runtime.mutate_route=after_route
    result=run_poi(runtime,tmp_path)
    assert not result['ok'] and not runtime.backend.actions and runtime.released


def test_read_only_warmup_restarts_freshness_proof_and_keeps_request_owned(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from threading import Lock
    from ff7r_agent import map_poi
    sample, entry, observed = fixture()
    calls = []
    def observe():
        state = copy.deepcopy(sample)
        state['sequence'] = len(calls) + 20
        calls.append(state['sequence'])
        if len(calls) == 3:
            state['telemetry']['age_seconds'] = 2
        request = tmp_path / 'entity-poi.request'
        if request.exists():
            state['map_state']['poi_observation']['request_id'] = request.read_text().split('|')[0]
        return state
    service = SimpleNamespace(telemetry=SimpleNamespace(path=tmp_path/'state.json'),
        observe=observe, _sleeper=lambda _:None, _lock=Lock())
    monkeypatch.setattr(map_poi, 'catalog_entry', lambda _:entry)
    result = map_poi.observe_poi(service, entry['id'])
    assert result['ok'] and len(calls) == 5
    request = tmp_path/'entity-poi.request'
    original = request.read_text()
    with pytest.raises(MissionError, match='request_busy'):
        map_poi.observe_poi(service, entry['id'])
    assert request.read_text() == original
    assert not map_poi.release_poi(service, 'another-client')['released']
    assert map_poi.release_poi(service, result['poi']['request_id'])['released']
    assert not request.exists()


def test_staging_rotates_around_target_when_direct_goal_is_not_walkable():
    import math
    from types import SimpleNamespace
    from ff7r_agent.entity_staging import select_position
    sample, entry, observed = fixture()
    target = dict(position=observed['poi']['position'])
    checked = []
    def resolve(point, **bounds):
        checked.append(point)
        return SimpleNamespace(ok=len(checked)==3, reason_code='point_inside_polygon')
    result = select_position(sample,target,4,SimpleNamespace(resolve_point=resolve,
        plan=lambda *a,**k:SimpleNamespace(status="complete")))
    assert len(checked)==3
    assert math.hypot(result['x']-600,result['y']) == pytest.approx(400)
    assert result['z']==65


def test_staging_has_a_finite_bound_and_refuses_snapping_to_an_unproven_floor():
    from types import SimpleNamespace
    from ff7r_agent.entity_staging import select_position
    sample, entry, observed = fixture()
    checked=[]
    def resolve(point, **bounds):
        checked.append(bounds)
        return SimpleNamespace(ok=True, reason_code='point_snapped_to_polygon')
    with pytest.raises(MissionError, match='no_walkable'):
        select_position(sample,dict(position=observed['poi']['position']),4,SimpleNamespace(resolve_point=resolve))
    assert len(checked)==24
    assert all(b['max_vertical_separation_m']==1 for b in checked)


def test_staging_skips_isolated_polygon_before_requesting_live_route():
    from types import SimpleNamespace
    from ff7r_agent.entity_staging import select_position
    sample, entry, observed=fixture()
    planned=[]
    def plan(start,end,**bounds):
        planned.append(end)
        return SimpleNamespace(status='complete' if len(planned)==2 else 'failed')
    graph=SimpleNamespace(resolve_point=lambda *a,**k:SimpleNamespace(ok=True,reason_code='point_inside_polygon'),plan=plan)
    result=select_position(sample,dict(position=observed['poi']['position']),4,graph)
    assert len(planned)==2 and result['y']!=0
