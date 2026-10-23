import copy,json
from pathlib import Path
from types import SimpleNamespace
import pytest
from ff7r_agent.save_selection import CONTRACT, selected_cell
from ff7r_agent.save_slot_save import SaveSlotWriter,SaveWriteLayout
from ff7r_agent.save_slot_load import SaveSlotLoadError

FRAMES=json.loads((Path(__file__).parent/'fixtures/save-selection-live-derived.json').read_text())['frames']

def writer():
 w=SaveSlotWriter.__new__(SaveSlotWriter)
 w.write_layout=SaveWriteLayout('Txt_ListNum',None,(),'Button_00','','','',CONTRACT)
 return w

def screen(d):return SimpleNamespace(response=d,text_cap_reached=False,load_prompt_shown=False,signals=[])

@pytest.mark.parametrize('index,slot',[(0,16),(1,15),(2,11),(3,15)])
def test_live_derived_exact_link_ignores_false_focus(index,slot):
 row=writer()._row(screen(FRAMES[index]))
 assert row['slot']==slot and len(row['nodes'])==51
 assert row['button']['keyboard_focus'] is False
 assert row['selection']['selected_index']+1 != slot

@pytest.mark.parametrize('mutation', ['cap','visited','class','owner','world','user','child','duplicate','discovery','numeric','hover'])
def test_refusals(mutation):
 d=copy.deepcopy(FRAMES[1]);cell,entry=selected_cell(d,CONTRACT)
 if mutation=='cap':cell['node_walk']['truncated']=True
 elif mutation=='visited':cell['node_walk']['visited']=50
 elif mutation=='class':entry['class']='Class /Script/EndGame.EndListBox'
 elif mutation=='owner':entry['owners'][1]='Other_C unrelated'
 elif mutation=='world':entry['id']=entry['id'].replace('/Temp/','/Game/')
 elif mutation=='user':entry['user_index']=1
 elif mutation=='child':entry['selected_child_id']='SaveData_Cell_C wrong'
 elif mutation=='duplicate':d['save_lists'].append(copy.deepcopy(entry))
 elif mutation=='discovery':d['save_list_discovery']['scan_truncated']=True
 elif mutation=='numeric':next(n for n in cell['nodes'] if n['name']=='Txt_ListNum')['text']='15 saved'
 elif mutation=='hover':d['save_lists']=[];next(n for n in cell['nodes'] if n['name']=='Button_00')['hovered']=True
 with pytest.raises(SaveSlotLoadError):writer()._row(screen(d))


def test_fresh_frame_refuses_replay_and_nonadvancing_sequence(monkeypatch):
 from ff7r_agent.save_slot_load import SaveSlotLoader
 w=writer();w._save_frame=None;w.last_save_evidence={}
 d=copy.deepcopy(FRAMES[1])
 monkeypatch.setattr(SaveSlotLoader,'read',lambda *args:screen(d))
 w.read('first')
 with pytest.raises(SaveSlotLoadError,match='save_frame_stale_or_invalid'):w.read('replay')
 d['request_id']='f'*32
 with pytest.raises(SaveSlotLoadError,match='save_frame_stale_or_invalid'):w.read('same_sequence')
 d['source_sequence']+=1
 w.read('advanced')


def test_final_row_read_occurs_after_file_guards_before_only_confirm(game,monkeypatch):
 from test_working_save import calibration_game,CALIBRATION
 calibration_game(game);events=[]
 old_guard=SaveSlotWriter._guard;old_read=SaveSlotWriter.read;old_press=SaveSlotWriter.press
 def guard(self):events.append('file_guard');return old_guard(self)
 def read(self,stage):events.append(stage);return old_read(self,stage)
 def press(self,key,why,**kwargs):events.append(why);return old_press(self,key,why,**kwargs)
 monkeypatch.setattr(SaveSlotWriter,'_guard',guard)
 monkeypatch.setattr(SaveSlotWriter,'read',read)
 monkeypatch.setattr(SaveSlotWriter,'press',press)
 result=SaveSlotWriter(game).run(15,True,calibrate_prompt=CALIBRATION)
 assert result['state']=='save_calibration_prompt_observed'
 i=events.index('final_save_row_after_file_guards');j=events.index('open_save_prompt')
 assert events[i-1]=='file_guard' and i<j and 'file_guard' not in events[i+1:j]
 assert not any('verified_yes' in event for event in events)

from test_working_save import game


def test_final_row_changed_during_file_guard_sends_no_row_confirm(game,monkeypatch):
 from test_working_save import calibration_game,CALIBRATION
 calibration_game(game)
 old_guard=SaveSlotWriter._guard
 def guard(self):
  old_guard(self)
  if self.screens and self.screens[-1]['stage']=='before_save_prompt':game.row=14
 monkeypatch.setattr(SaveSlotWriter,'_guard',guard)
 result=SaveSlotWriter(game).run(15,True,calibrate_prompt=CALIBRATION)
 assert not result['ok'] and result['decision']=='save_selected_row_changed'
 assert not any(p['why']=='open_save_prompt' for p in result['presses'])


def test_load_ui_never_authorizes_correct_cell():
 s=screen(FRAMES[1]);s.signals=['load_list']
 with pytest.raises(SaveSlotLoadError,match='save_screen_ambiguous'):writer()._row(s)


def test_malformed_discovery_structured_refusal():
 d=copy.deepcopy(FRAMES[1]);d['save_list_discovery']['classes']=[0]
 with pytest.raises(SaveSlotLoadError,match='save_list_discovery_incomplete'):selected_cell(d,CONTRACT)
