from datetime import datetime, timezone

import pytest

from ff7r_agent import live_objective, map_poi
from ff7r_agent.navigation_values import MissionError
from ff7r_agent.objective_catalogue import active_main_step, main_story_scope, objective_references, read_rows
from ff7r_agent.world_target_resolver import resolve_objective_reference


def row(name, **values):
    return {'values': {'Name': name, **{k:str(v) for k,v in values.items()}}, 'arrays': {},
            'source': {'path':'fixture.csv', 'source_row_number':2, 'row_index':'0', 'sha256':'flags'}}


def catalogue():
    root = row('chapter12', StoryType=1, StoryProgressFlagID='root')
    a = row('step_a', ParentID='chapter12', StoryType=2, StoryProgressFlagID='a', GroupIndex=10,
            StoryIndex=1, CompleteStatus=0, ItemName='$same_title')
    done = row('step_a_done', ParentID='chapter12', StoryType=2, StoryProgressFlagID='done', GroupIndex=10,
               StoryIndex=2, CompleteStatus=1)
    b = row('step_b', ParentID='chapter12', StoryType=2, StoryProgressFlagID='b', GroupIndex=20,
            StoryIndex=1, CompleteStatus=0, ItemName='$same_title')
    for step in (a, b):
        step['arrays']['MapIconInfoID_Array'] = [{'position':1,'source_column':'1','value':'marker'}]
    marker = row('marker', ObjectID='point', AffiliationName='2350-GOLDS', LocationId='EndGame')
    marker['arrays']['IconType_Array'] = [{'position':1,'source_column':'1__7','value':'2'}]
    chapter = row('entry12', RefMainChapterID='Chapt_0012')
    chapter['arrays'] = {'ResidentWorkIDList_Array':[{'position':1,'value':'rswChapterProgress'}],
                         'ResidentWorkValueList_Array':[{'position':1,'value':'12010'}]}
    flag = row('root', ChapterID='Chapt_0012')
    tables = {'StoryResident':[root,a,done,b], 'Chapter':[chapter], 'MapIconInfo':[marker], 'StoryFlag':[flag]}
    return {'tables':tables,'indexes':{k:{r['values']['Name']:r for r in v} for k,v in tables.items()}}


def test_flattened_arrays_keep_their_owner_and_duplicate_column_provenance(tmp_path):
    path = tmp_path/'Story.csv'
    path.write_text('Index,Name,MapIconInfoID_Array,1,2,Other_Array,1\n0,A,,main,other,,flag\n')
    parsed = read_rows(path)[0]
    assert parsed['arrays']['MapIconInfoID_Array'][0]['value'] == 'main'
    assert parsed['arrays']['Other_Array'] == [{'position':1,'source_column':'1__2','value':'flag'}]
    assert parsed['source']['source_row_number'] == 2


def test_chapter_progress_uses_declared_work_assignment_and_ref_main_chapter():
    data = catalogue()
    root, steps, _ = main_story_scope(data,12010)
    assert root['values']['Name'] == 'chapter12' and len(steps)==3
    with pytest.raises(MissionError,match='unmapped_or_ambiguous'):
        main_story_scope(data,12011)
    other = row('other',RefMainChapterID='Chapt_0013')
    other['arrays'] = data['tables']['Chapter'][0]['arrays']
    data['tables']['Chapter'].append(other)
    with pytest.raises(MissionError,match='unmapped_or_ambiguous'):
        main_story_scope(data,12010)


def test_a_completed_step_does_not_hide_the_new_step_and_ambiguous_groups_are_refused():
    root, steps, _ = main_story_scope(catalogue(),12010)
    flags = {'root':True,'a':True,'done':False,'b':False}
    assert active_main_step(root,steps,flags)['values']['Name']=='step_a'
    with pytest.raises(MissionError,match='objective_active_ambiguous'):
        active_main_step(root,steps,{**flags,'b':True})
    assert active_main_step(root,steps,{**flags,'done':True,'b':True})['values']['Name']=='step_b'
    with pytest.raises(MissionError,match='objective_none_active'):
        active_main_step(root,steps,{**flags,'done':True})
    with pytest.raises(MissionError,match='flag_evidence_incomplete'):
        active_main_step(root,steps,{'root':True,'a':True})


def test_multiple_primary_markers_are_not_reduced_to_the_first_one():
    data = catalogue()
    step = data['tables']['StoryResident'][1]
    assert objective_references(data,step)[0]['primary'] is True
    step['arrays']['MapIconInfoID_Array'].append({'position':2,'source_column':'2','value':'marker'})
    with pytest.raises(MissionError,match='primary_target_ambiguous'):
        objective_references(data,step)


def setup_pipeline(monkeypatch, *, changed=False, identity_changed=False, macro=False):
    data, calls = catalogue(), []
    monkeypatch.setattr(live_objective,'load_catalogue',lambda:data)
    def progress(service, flags, **kwargs):
        calls.append(flags)
        last = len(calls)==3
        return {'ok':True,'chapter_progress':12010,'identity':{'world_id':'EndGame','map_id':'EndGame',
                'process_token':'different' if identity_changed and last else 'game-1'},
                'player_actor_id':'Cloud_1','source_sequence':100+len(calls),
                'timestamp_utc':datetime.now(timezone.utc).isoformat(),'flag_catalog_sha256':'flags',
                'flags':{'root':True,'a':True,'done':changed and last,'b':changed and last}}
    monkeypatch.setattr(live_objective,'read_objective_progress',progress)
    destination = {'source':'paired getters','authority':'runtime','status':'located',
                   'position':{'x':1.,'y':2.,'z':3.},'map_id':'EndGame'}
    if macro:
        destination.update(status='macro',position=None,field='2350-GOLDS')
    monkeypatch.setattr(live_objective,'resolve_objective_reference',lambda *_:destination)
    return calls


@pytest.mark.parametrize('macro',[False,True])
def test_live_pipeline_returns_a_rechecked_exact_or_macro_destination_without_input(monkeypatch,macro):
    calls = setup_pipeline(monkeypatch,macro=macro)
    result = live_objective.get_active_objective(object())
    assert result['ok'] and result['destination_type']==('macro' if macro else 'exact')
    assert result['objective_key']==('chapter12','chapter12:10','step_a')
    assert result['input_sent'] is False and result['input_authorized'] is False
    assert len(calls)==3


@pytest.mark.parametrize('options,reason', [({'changed':True},'objective_changed_during_resolution'),
                                         ({'identity_changed':True},'objective_identity_changed')])
def test_a_transition_or_process_replacement_during_target_resolution_returns_no_destination(monkeypatch,options,reason):
    setup_pipeline(monkeypatch,**options)
    result = live_objective.get_active_objective(object())
    assert not result['ok'] and result['reason']==reason and result['position'] is None


def test_unsupported_activity_does_not_silently_return_a_main_story_goal(monkeypatch):
    monkeypatch.setattr(live_objective,'read_objective_progress',lambda *_:pytest.fail('unexpected read'))
    assert live_objective.get_active_objective(object(),'world_intel')['reason']=='objective_activity_type_unsupported'


def test_slow_progress_reads_do_not_start_a_target_lookup_outside_the_rpc_budget(monkeypatch):
    calls = setup_pipeline(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(live_objective.time, 'monotonic', lambda:clock[0])
    reader = live_objective.read_objective_progress
    def slow(*args, **kwargs):
        result = reader(*args, **kwargs)
        clock[0] += 6
        return result
    monkeypatch.setattr(live_objective, 'read_objective_progress', slow)
    monkeypatch.setattr(live_objective, 'resolve_objective_reference', lambda *_:pytest.fail('late lookup'))
    result = live_objective.get_active_objective(object())
    assert result['reason']=='objective_resolution_timeout' and result['position'] is None
    assert len(calls)==2


def test_marker_resolution_releases_its_read_lease_and_rejects_changed_static_identity(monkeypatch):
    released=[]
    monkeypatch.setattr(map_poi,'observe_poi',lambda *_args,**_kw:{'poi':{'request_id':'lease','object_id':'point',
        'catalog_sha256':'sha','position':{'x':1,'y':2,'z':3},'map_id':'EndGame','coordinate_source':'getter'}})
    monkeypatch.setattr(map_poi,'release_poi',lambda _,request:released.append(request))
    reference={'target_type':'poi','target_id':'marker','object_id':'point','marker_source':{'sha256':'sha'}}
    assert resolve_objective_reference(object(),reference)['status']=='located'
    assert released==['lease']
    with pytest.raises(MissionError,match='objective_target_catalog_changed'):
        resolve_objective_reference(object(),{**reference,'object_id':'changed'})
    assert released==['lease','lease']


def test_missing_exact_target_uses_its_declared_field_and_never_guessed_coordinates(monkeypatch):
    def unavailable(*args,**kwargs): raise MissionError('poi_observation_timeout')
    monkeypatch.setattr(map_poi,'observe_poi',unavailable)
    reference={'target_type':'poi','target_id':'marker','field':'2350-GOLDS'}
    result=resolve_objective_reference(object(),reference)
    assert result['status']=='macro' and result['position'] is None and result['field']=='2350-GOLDS'
    with pytest.raises(MissionError,match='objective_macro_destination_unavailable'):
        resolve_objective_reference(object(),{**reference,'field':'0001-WORLD'})


def test_progress_between_two_initializers_of_one_chapter_belongs_to_it():
    # Live 2026-10-20 (Mt. Nibel, the gas): the counter read 1045, between
    # initializers 1040 and 1050 of the same chapter.
    data = catalogue()
    later = row('entry12_030', RefMainChapterID='Chapt_0012')
    later['arrays'] = {'ResidentWorkIDList_Array':[{'position':1,'value':'rswChapterProgress'}],
                       'ResidentWorkValueList_Array':[{'position':1,'value':'12030'}]}
    data['tables']['Chapter'].append(later)
    root, _, evidence = main_story_scope(data, 12020)
    assert root['values']['Name'] == 'chapter12' and len(evidence) == 2
    later['values']['RefMainChapterID'] = 'Chapt_0013'
    with pytest.raises(MissionError, match='unmapped_or_ambiguous'):
        main_story_scope(data, 12020)


def test_a_sole_marker_of_another_icon_type_is_the_steps_place():
    data = catalogue()
    data['indexes']['MapIconInfo']['marker']['arrays']['IconType_Array'] = [{'position':1,'source_column':'1__7','value':'11'}]
    step = data['indexes']['StoryResident']['step_a']
    references = objective_references(data, step)
    assert references[0]['primary'] is True and references[0]['primary_basis'] == 'sole_marker'
    lift = row('lift', ObjectID='lift_point')
    lift['arrays']['IconType_Array'] = [{'position':1,'source_column':'1__7','value':'11'}]
    data['indexes']['MapIconInfo']['lift'] = lift
    step['arrays']['MapIconInfoID_Array'].append({'position':2,'source_column':'2','value':'lift'})
    with pytest.raises(MissionError, match='primary_target_ambiguous_or_unsupported'):
        objective_references(data, step)
