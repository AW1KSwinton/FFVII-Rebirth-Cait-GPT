import copy,json,hashlib
from pathlib import Path
import pytest
from test_working_save import game,calibration_game,CALIBRATION
from ff7r_agent.save_slot_save import SaveSlotWriter,SaveCalibrationResolver
from ff7r_agent.save_selection import CONTRACT
from ff7r_agent.save_cancel_review import REVIEWED_SOURCE

TEMPLATE=json.loads((Path(__file__).parent/'fixtures/save-selection-live-derived.json').read_text())['frames'][1]
ROW={'slot_node':'Txt_ListNum','row_button_node':'Button_00','row_selection':CONTRACT}
PROMPT='Com_Window_01_C /Temp/Untitled_1.MenuRenderingWorld:Com_Window_01_C_777'

def prepare(game,tmp_path):
 calibration_game(game)
 old=game.snapshot;seq=[100]
 def snapshot():
  d=old();seq[0]+=1
  d.update(request_id=f'{seq[0]:032x}',source_sequence=seq[0],schema_version=1,source='ue4ss_map_query')
  if game.state=='save_list':
   base=copy.deepcopy(TEMPLATE);entry=base['save_lists'][0]
   cell=next(c for c in base['save_cells'] if c['full_id']==entry['selected_child_id'])
   fake=next(c for c in d['save_cells'] if c['nodes'][0]['text']==str(game.row))
   cell['nodes']=copy.deepcopy(fake['nodes'])
   cell['nodes'][0]['name']='Txt_ListNum';cell['nodes'][1]['name']='Button_00'
   cell['nodes'] += [{'name':f'pad{i}','visible':False} for i in range(51-len(cell['nodes']))]
   cell['node_walk']={'node_limit':128,'truncated':False,'visited':51}
   d.update(save_cells=[cell],save_lists=base['save_lists'],save_list_discovery=base['save_list_discovery'])
  if game.state=='prompt':
   d['save_dialogs']=[{'id':PROMPT,'class':'Com_Window_01_C','node_walk':{'node_limit':128,'visited':1,'truncated':True},'nodes':[{'name':'Txt','class':'EndTextBlock','visible':True,'text':'Save your progress to this slot?'}]}]
  return d
 game.snapshot=snapshot
 result=SaveSlotWriter(game).run(15,True,calibrate_prompt=ROW)
 assert result['state']=='save_calibration_prompt_observed'
 path=game._queen_blood_runs.root/'save-operation-lock.json';lock=json.loads(path.read_bytes());game.keys.clear()
 key=game._map_menu_key
 def cancel(button,ms):
  if game.state=='prompt' and button=='cancel':game.keys.append(button);game.state='save_list'
  else:key(button,ms)
 game._map_menu_key=cancel
 args={'expected_token':lock['token'],'expected_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'cancel_review':{'row_contract':ROW,'artifacts':{}}}
 return path,args,result

def test_durable_cancel_dryrun_readonly(game,tmp_path):
 path,args,result=prepare(game,tmp_path);raw=path.read_bytes()
 report=SaveCalibrationResolver(game).run(**args)
 assert report['ok'] and not game.keys and path.read_bytes()==raw
 assert json.loads(raw)['final_row_contract']['response']['request_id']==result['final_row_contract_evidence']['request_id']

def test_single_cancel_and_exact_raw_archive(game,tmp_path):
 path,args,_=prepare(game,tmp_path);raw=path.read_bytes()
 report=SaveCalibrationResolver(game).run(**args,execute=True)
 assert report['ok'] and report['state']=='save_calibration_cancelled',report.get('decision')
 assert game.keys[0]=='cancel' and game.keys.count('cancel')==1 and 'confirm' not in game.keys
 assert not path.exists() and not game.writes
 import base64
 archived=json.loads(Path(report['archive']).read_text())
 assert base64.b64decode(archived['lock_raw_base64'])==raw

@pytest.mark.parametrize('defect',['wrong_prompt','duplicate_prompt','wrong_row','same_frame','process','protected','cancel_ignored'])
def test_refusal_keeps_lock_no_yes_or_retry(game,tmp_path,defect):
 path,args,_=prepare(game,tmp_path);snap=game.snapshot
 def corrupt():
  d=snap()
  if defect=='wrong_prompt' and game.state=='prompt':d['save_dialogs'][0]['nodes'][0]['text']='Load this slot?'
  if defect=='duplicate_prompt' and game.state=='prompt':d['save_dialogs']*=2
  if defect=='wrong_row' and game.state=='save_list':d['save_cells'][0]['nodes'][0]['text']='14'
  if defect=='same_frame':d['request_id']='a'*32;d['source_sequence']=101
  return d
 game.snapshot=corrupt
 if defect=='process':game._environment.tokens[42]='changed'
 if defect=='protected':game.path(14).write_bytes(b'changed')
 if defect=='cancel_ignored':game._map_menu_key=lambda button,ms:game.keys.append(button)
 report=SaveCalibrationResolver(game).run(**args,execute=True)
 assert not report['ok'] and path.exists() and not game.writes and 'confirm' not in game.keys
 assert game.keys.count('cancel')<=1


def test_legacy_source_attestation_and_byte_preservation(game,tmp_path):
 path,args,result=prepare(game,tmp_path);lock=json.loads(path.read_bytes())
 lock.pop('final_row_contract');lock.pop('observed_calibration_prompt');path.write_text(json.dumps(lock))
 args['expected_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
 run=lock['review_binding']['initial'];runtime={'runtime':{'structuredContent':{'source_commit_sha':REVIEWED_SOURCE,'git_commit_sha':REVIEWED_SOURCE,'git_dirty':False,'runtime_profile_verification':{'ok':True}}},'run':{'structuredContent':{'result':run}}}
 start={'ok':True,'job_id':'synthetic-save','started_at':1,'worker_generation':1,'worker_pid':123,'operation':'save_game_progress'}
 final={**start,'state':'completed','response':{'result':result}}
 artifacts={}
 for key,data in [('runtime',runtime),('prompt',result['raw_save_menu']),('preflight',{'structuredContent':{'result':run}}),('start',{'structuredContent':start}),('final',{'structuredContent':final})]:
  p=tmp_path/(key+'.json');p.write_text(json.dumps(data));artifacts[key]={'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
 args['cancel_review']['artifacts']=artifacts
 report=SaveCalibrationResolver(game).run(**args,execute=True)
 assert report['ok'] and 'not final frame' in report['proof_limit'],report.get('decision')
 archive=json.loads(Path(report['archive']).read_text())
 assert set(archive['cancel_review_artifacts'])==set(artifacts)


def test_unreviewed_legacy_lock_cannot_use_cancel_mode(game,tmp_path):
 path,args,_=prepare(game,tmp_path);lock=json.loads(path.read_bytes());lock.pop('final_row_contract');path.write_text(json.dumps(lock));args['expected_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
 report=SaveCalibrationResolver(game).run(**args,execute=True)
 assert not report['ok'] and report['decision']=='save_cancel_durable_proof_missing' and not game.keys


@pytest.mark.parametrize('which',['target','protected'])
def test_file_change_during_final_prompt_read_sends_no_cancel(game,tmp_path,which):
 path,args,_=prepare(game,tmp_path);snap=game.snapshot;count=[0]
 def changed():
  d=snap();count[0]+=1
  if count[0]==2:game.path(15 if which=='target' else 14).write_bytes(b'changed-during-final-ui-read')
  return d
 game.snapshot=changed
 report=SaveCalibrationResolver(game).run(**args,execute=True)
 assert not report['ok'] and path.exists() and not game.keys


@pytest.mark.parametrize('defect',['post_confirm_input','wrong_source','wrong_job','wrong_preflight','artifact_hash'])
def test_legacy_review_does_not_adopt_ambiguous_evidence(game,tmp_path,defect):
 path,args,result=prepare(game,tmp_path);lock=json.loads(path.read_bytes());lock.pop('final_row_contract');path.write_text(json.dumps(lock));args['expected_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
 run=copy.deepcopy(lock['review_binding']['initial']);identity={'source_commit_sha':REVIEWED_SOURCE,'git_commit_sha':REVIEWED_SOURCE,'git_dirty':False,'runtime_profile_verification':{'ok':True}}
 start={'ok':True,'job_id':'synthetic-save','started_at':1,'worker_generation':1,'worker_pid':123,'operation':'save_game_progress'};final={**start,'state':'completed','response':{'result':copy.deepcopy(result)}}
 if defect=='post_confirm_input':final['response']['result']['presses'].append({'button':'up','why':'extra','t':100})
 if defect=='wrong_source':identity['source_commit_sha']='0'*40
 if defect=='wrong_job':final['job_id']='other'
 if defect=='wrong_preflight':run['driver']['model_id']='other'
 data={'runtime':{'runtime':{'structuredContent':identity},'run':{'structuredContent':{'result':lock['review_binding']['initial']}}},'preflight':{'structuredContent':{'result':run}},'start':{'structuredContent':start},'final':{'structuredContent':final},'prompt':result['raw_save_menu']}
 artifacts={}
 for key,value in data.items():
  p=tmp_path/(key+'.json');p.write_text(json.dumps(value));artifacts[key]={'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
 if defect=='artifact_hash':artifacts['final']['sha256']='0'*64
 args['cancel_review']['artifacts']=artifacts
 report=SaveCalibrationResolver(game).run(**args,execute=True)
 assert not report['ok'] and path.exists() and not game.keys


def test_replacement_selected15_cell_same_list_retains_lock_without_cleanup(game,tmp_path):
 path,args,_=prepare(game,tmp_path);snap=game.snapshot
 def replaced():
  d=snap()
  if game.state=='save_list':
   cell=d['save_cells'][0];old=cell['full_id'];new=old.rsplit('_',1)[0]+'_99999'
   cell['full_id']=new;cell['id']=new.split(' ',1)[1]
   entry=d['save_lists'][0];entry['selected_child_id']=new;entry['selected_save_cell_full_id']=new;entry['selected_save_cell_id']=cell['id']
  return d
 game.snapshot=replaced
 report=SaveCalibrationResolver(game).run(**args,execute=True)
 assert not report['ok'] and report['decision']=='save_cancel_original_cell_changed'
 assert path.exists() and game.keys==['cancel'] and not game.writes


def test_uppercase_exact_hash_dryrun_is_readonly(game,tmp_path):
 path,args,_=prepare(game,tmp_path);raw=path.read_bytes()
 args['expected_sha256']=args['expected_sha256'].upper()
 report=SaveCalibrationResolver(game).run(**args)
 assert report['ok'] and not game.keys and path.read_bytes()==raw

@pytest.mark.parametrize('bad',[None,123,'a'*63,'a'*65,'g'*64,' '+'a'*64])
def test_malformed_hash_refuses_before_input(game,tmp_path,bad):
 path,args,_=prepare(game,tmp_path);raw=path.read_bytes()
 args['expected_sha256']=bad
 report=SaveCalibrationResolver(game).run(**args,execute=True)
 assert report['decision']=='save_resolution_contract_invalid'
 assert not game.keys and path.read_bytes()==raw


def test_deeper_live_derived_prompt_cancel_dryrun(game,tmp_path):
 path,args,_=prepare(game,tmp_path);raw=path.read_bytes();snapshot=game.snapshot
 dialog=json.loads((Path(__file__).parent/'fixtures/save-prompt-8e-live-derived.json').read_text())['dialog']
 def deeper():
  d=snapshot()
  if game.state=='prompt':d['save_dialogs']=[copy.deepcopy(dialog)]
  return d
 game.snapshot=deeper
 report=SaveCalibrationResolver(game).run(**args)
 assert report['ok'],report
 assert not game.keys and path.read_bytes()==raw

@pytest.mark.parametrize('defect',['root_wrong','nested_question','duplicate_root'])
def test_deeper_prompt_identity_refusals(game,tmp_path,defect):
 path,args,_=prepare(game,tmp_path);raw=path.read_bytes();snapshot=game.snapshot
 dialog=json.loads((Path(__file__).parent/'fixtures/save-prompt-8e-live-derived.json').read_text())['dialog']
 root=next(n for n in dialog['nodes'] if n.get('text')=='Save your progress to this slot?')
 if defect=='root_wrong':root['full_id']=root['full_id'].replace('_777.','_778.')
 elif defect=='nested_question':root['text']='Load this slot?';next(n for n in dialog['nodes'] if '.Btn_00.WidgetTree.Txt' in n.get('full_id',''))['text']='Save your progress to this slot?'
 else:dialog['nodes'].append(copy.deepcopy(root))
 def deeper():
  d=snapshot()
  if game.state=='prompt':d['save_dialogs']=[copy.deepcopy(dialog)]
  return d
 game.snapshot=deeper
 report=SaveCalibrationResolver(game).run(**args,execute=True)
 assert report['decision']=='save_cancel_prompt_unverified'
 assert not game.keys and path.read_bytes()==raw
