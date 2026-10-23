from copy import deepcopy
from types import SimpleNamespace
import pytest
from ff7r_agent.service import SafetyError
from test_queens_blood_match import _service, MatchTelemetry, _state, _queen_blood
from test_agent import FakeRealInput

TARGET='field_object:2500-CSMO:oba2500_Actor_CardGamer02'


def fixture(monkeypatch):
    state=_state(1,queen_blood=_queen_blood(screen='deck_select',begin_match_available=True,match_screen=True))
    backend=FakeRealInput(); service=_service(MatchTelemetry([state]),backend)
    run={'driver':{'driver':'ChatGPT','model_id':'gpt-6.1-sol','effort':'xhigh','run_id':'declared'},
         'assignment':{'slot':15,'steam_id':'7656','profile_id':'assigned15'},
         'loaded_save':{'slot':4,'steam_id':'7656','load_id':'verified4','process':{'pid':'4242','token':'token'}},
         'loaded_save_verified':True,'profile_id':None,'run_loaded':False}
    session={'active':True,'owner_matches':True,'game_pid':4242,'recording_driver':{'modelId':'gpt-6.1-sol','effort':'xhigh'}}
    service._queen_blood_runs=object()
    service.get_queens_blood_run_status=lambda:deepcopy(run)
    service._session_lease=SimpleNamespace(status=lambda:dict(session))
    service._assert_action_allowed=lambda **kwargs:state
    service._ensure_game_foreground=lambda:None
    service._queen_blood_input_reachable=lambda:True
    service.queen_blood_profile=lambda:None
    campaign=[];service._record_campaign_action=lambda action:campaign.append(action)
    return service,backend,run,session,campaign


def test_one_off_stops_at_deck_without_begin_assignment_or_campaign_mutation(monkeypatch):
    service,backend,run,session,campaign=fixture(monkeypatch);before=deepcopy(run)
    result=service.initiate_queens_blood_match(target_id=TARGET,stop_at_deck_select=True,one_off_visit=True)
    assert result['outcome']=='deck_select_ready' and result['one_off_visit'] is True
    assert result['visited_save']['slot']==4 and run==before and not campaign
    assert not backend.actions and service._one_off_visit_binding is None
    with pytest.raises(SafetyError,match='assigned save'):
        service.initiate_queens_blood_match(target_id=TARGET)
    assert not backend.actions


@pytest.mark.parametrize('args', [{},{'decline':True,'stop_at_deck_select':True},{'stop_at_deck_select':True,'target_id':None}])
def test_one_off_rejects_begin_ambiguous_or_unnamed_request(monkeypatch,args):
    service,backend,*_=fixture(monkeypatch)
    with pytest.raises(SafetyError,match='Begin is forbidden'):
        service.initiate_queens_blood_match(**{'target_id':TARGET,'one_off_visit':True,**args})
    assert not backend.actions


def test_one_off_decline_from_open_deck_never_begins(monkeypatch):
    service,backend,*_=fixture(monkeypatch)
    with pytest.raises(SafetyError,match='open deck screen'):
        service.initiate_queens_blood_match(target_id=TARGET,decline=True,one_off_visit=True)
    assert not backend.actions


@pytest.mark.parametrize('field,value',[('loaded_save_verified',False),('profile_id','assigned15'),('run_loaded',True)])
def test_one_off_requires_verified_nonassigned_receipt(monkeypatch,field,value):
    service,backend,run,*_=fixture(monkeypatch);run[field]=value
    with pytest.raises(SafetyError):service.initiate_queens_blood_match(target_id=TARGET,stop_at_deck_select=True,one_off_visit=True)
    assert not backend.actions


@pytest.mark.parametrize('field,value',[('owner_matches',False),('active',False),('recording_driver',None),('game_pid',9999)])
def test_one_off_requires_owned_exact_driver_and_game(monkeypatch,field,value):
    service,backend,run,session,_=fixture(monkeypatch);session[field]=value
    with pytest.raises(SafetyError):service.initiate_queens_blood_match(target_id=TARGET,stop_at_deck_select=True,one_off_visit=True)
    assert not backend.actions


def test_one_off_changed_receipt_refuses_next_confirmation(monkeypatch):
    service,backend,run,*_=fixture(monkeypatch)
    service._one_off_visit_binding=service._one_off_visit_receipt();run['loaded_save']['load_id']='human-reload'
    with pytest.raises(SafetyError,match='changed'):service._queen_blood_press('confirm',400)
    assert not backend.actions



def test_one_off_decline_records_only_private_visit_evidence(monkeypatch,tmp_path):
    service,backend,run,session,campaign=fixture(monkeypatch);before=deepcopy(run)
    service._public_stream_event_dir=tmp_path/'events'
    service._public_stream_event_dir.mkdir()
    state=service.telemetry.observe()
    service._queen_blood_prematch_visible=lambda sample:False
    service._queen_blood_dialogue_or_transition_active=lambda sample:True
    service._finish_open_conversation=lambda *args,**kwargs:(state,True)
    result=service.initiate_queens_blood_match(target_id=TARGET,decline=True,one_off_visit=True)
    assert result['outcome']=='declined' and result['one_off_visit'] is True
    assert service._last_action['visited_save']['slot']==4
    assert run==before and not campaign and not list(tmp_path.rglob('*.json'))
    assert not backend.actions


@pytest.mark.parametrize('change', ['effort','run_id','assignment'])
def test_one_off_changed_declaration_or_assignment_stops_confirmation(monkeypatch,change):
    service,backend,run,*_=fixture(monkeypatch)
    service._one_off_visit_binding=service._one_off_visit_receipt()
    if change=='assignment':run['assignment']['slot']=16
    else:run['driver'][change]='high' if change=='effort' else 'new-run'
    with pytest.raises(SafetyError):service._queen_blood_press('confirm',400)
    assert not backend.actions
