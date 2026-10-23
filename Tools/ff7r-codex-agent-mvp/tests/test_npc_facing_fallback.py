import asyncio,copy,math
from datetime import datetime,timezone
import pytest
from ff7r_agent.npc_approach import face_body_toward,face_until_talk
from ff7r_agent.navigation_values import MissionError
CANON="field_object:2360-UNDRS:oba2360_Actor_CardGamer03"
TARGET={"x":55888.6875,"y":-21168.25,"z":3106.717773,"canonical_id":CANON}
class Runtime:
 def __init__(self,defect=None):self.seq=100;self.moves=[];self.strong=False;self.post=0;self.defect=defect;self.resolves=0
 async def rpc(self,op,args):
  if op=='observe_game':
   self.seq+=1
   if self.strong:self.post+=1
   yaw=3.819505 if self.strong and self.post>=4 and self.defect!='no_turn' else 12.147888
   d={'sequence':self.seq,'timestamp_utc':datetime.now(timezone.utc).isoformat(),'running':True,'map_id':'EndGame','player':{'actor_id':'CloudExact','position':{'x':55805.484375,'y':-21113.980469,'z':3194.717773},'rotation':{'yaw':yaw},'locomotion':{'grounded':True,'velocity_cm_s':{'x':0,'y':0,'z':0}}},'camera':{'rotation':{'yaw':-79.999863}}}
   if self.defect=='world':d['map_id']='Other' if self.seq>=105 else'EndGame'
   if self.defect=='stale':d['sequence']=100
   if self.defect=='far':d['player']['position']['x']=55000
   if self.defect=='air':d['player']['locomotion']['grounded']=False
   return d
  if op=='read_field_prompt':return{'talk':self.defect!='no_talk','player_position':{'x':55805.484375,'y':-21113.980469,'z':3194.717773}}
  if op=='resolve_world_target':
   self.resolves+=1
   return{'status':'located','canonical_id':CANON,'position':{'x':TARGET['x'],'y':TARGET['y'],'z':3194.717773},'live_actor':{'id':'MaryOther' if self.defect=='replacement' and self.resolves>1 else'MaryExact','actor_name':'Wrong' if self.defect=='actor' else'Npc2360_CardCollector3'}}
  if op=='move_stick':
   self.moves.append(args);self.strong=math.hypot(args['x'],args['y'])>.5;return{'ok':True}
  raise AssertionError(op)
async def no_sleep(_):pass
def test_delayed_turn_one_strong_pulse_after_three_gentle():
 r=Runtime();out=asyncio.run(face_body_toward(r.rpc,TARGET,sleep=no_sleep))
 assert out['ok'] and abs(out['heading_error_degrees'])<=40
 assert len(r.moves)==4 and all(p['milliseconds']==120 for p in r.moves)
 assert len(out['strong_fallback']['observations'])>=4
 assert out['strong_fallback']['post_sequence']>out['strong_fallback']['pre_sequence']
@pytest.mark.parametrize('defect',['no_turn','world','stale','far','air','no_talk','actor','replacement'])
def test_refusals_never_repeat_strong_pulse(defect):
 r=Runtime(defect);out=asyncio.run(face_body_toward(r.rpc,TARGET,sleep=no_sleep))
 assert not out['ok']
 assert sum(math.hypot(p['x'],p['y'])>.5 for p in r.moves)<=1
 if defect=='far':assert not r.moves
 if defect in ('world','stale','air','no_talk','actor'):
  assert len(r.moves)<=3
  assert not any(math.hypot(p['x'],p['y'])>.5 for p in r.moves)
def test_talk_with_failed_facing_is_not_success():
 r=Runtime('no_turn')
 with pytest.raises(MissionError,match='npc_body_facing_not_ready'):asyncio.run(face_until_talk(r.rpc,TARGET,sleep=no_sleep))


def test_intermediate_weak_turn_heading_is_not_accepted_before_it_settles():
 # Synthetic delayed animation: the first turn briefly looks ready, then
 # settles facing away. The second turn supplies the actual stable heading.
 class DelayedWeak(Runtime):
  reads_after_move=0
  async def rpc(self,op,args):
   value=await super().rpc(op,args)
   if op=='move_stick':self.reads_after_move=0
   if op=='observe_game' and self.moves:
    self.reads_after_move+=1
    value['player']['rotation']['yaw']=(0 if len(self.moves)==1 and self.reads_after_move<=2
                                      else 12.147888 if len(self.moves)==1 else 3.819505)
   return value
 r=DelayedWeak();out=asyncio.run(face_body_toward(r.rpc,TARGET,sleep=no_sleep))
 assert out['ok'] and len(r.moves)==2 and 'strong_fallback' not in out
 assert all(len(samples)>=4 for samples in out['settling'])


@pytest.mark.parametrize('defect',['moving','rotating','changed_actor','unknown_velocity','nonfinite_heading'])
def test_an_unsettled_weak_turn_never_reports_ready_or_sends_another_tap(defect):
 class Unsettled(Runtime):
  async def rpc(self,op,args):
   value=await super().rpc(op,args)
   if op=='observe_game' and self.moves:
    if defect=='moving':value['player']['locomotion']['velocity_cm_s']['x']=100
    if defect=='rotating':value['player']['rotation']['yaw']=self.seq*3
    if defect=='changed_actor':value['player']['actor_id']='OtherCloud'
    if defect=='unknown_velocity':value['player']['locomotion']['velocity_cm_s']=None
    if defect=='nonfinite_heading':value['player']['rotation']['yaw']=float('nan')
   return value
 r=Unsettled();out=asyncio.run(face_body_toward(r.rpc,TARGET,sleep=no_sleep))
 assert not out['ok'] and out['reason']=='npc_body_pose_not_settled' and len(r.moves)==1


NENE='field_object:1110-KALMT:oba1110_Actor_CardGamer02'
class ClassOnlyNene(Runtime):
 # Synthetic coordinates; the identity proof reproduces Nene's live shape.
 async def rpc(self,op,args):
  value=await super().rpc(op,args)
  if op=='resolve_world_target':
   spec='MG2005_00_UniqueCardGameKalm02'
   value.update(canonical_id=NENE,
    identity={'candidates':[{'canonical_id':NENE,'actor_name':'Npc1110_CardGamer02','chara_spec_name':spec}]},
    match_evidence=[{'field':'class_chara_spec','value':spec,'static_canonical_ids':[NENE],
                    'static_candidate_count':1,'static_unique_to_record':True}])
   value['live_actor'].update(actor_name='',id='NeneExact',
                             **{'class':f'BlueprintGeneratedClass /Game/Mob/{spec}.{spec}_C'})
   if self.defect=='ambiguous_class':value['match_evidence'][0]['static_canonical_ids'].append(CANON)
   if self.defect=='wrong_class':value['live_actor']['class']='OtherClass'
   if self.defect=='conflicting_name':value['live_actor']['actor_name']='OtherNPC'
   if self.defect=='missing_proof':value['match_evidence']=[]
   if self.defect=='not_unique':value['match_evidence'][0]['static_unique_to_record']=False
   if self.defect=='replaced_class_actor' and self.resolves>1:value['live_actor']['id']='NeneReplacement'
  return value

def test_blank_actor_name_uses_the_resolvers_unique_class_identity_for_one_strong_turn():
 r=ClassOnlyNene();out=asyncio.run(face_body_toward(r.rpc,{**TARGET,'canonical_id':NENE},sleep=no_sleep))
 assert out['ok'] and len(r.moves)==4 and out['strong_fallback']['actor_id']=='NeneExact'

@pytest.mark.parametrize('defect',['ambiguous_class','wrong_class','conflicting_name','missing_proof','not_unique','replaced_class_actor'])
def test_class_recovery_keeps_ambiguity_conflict_and_actor_replacement_checks(defect):
 r=ClassOnlyNene(defect);out=asyncio.run(face_body_toward(r.rpc,{**TARGET,'canonical_id':NENE},sleep=no_sleep))
 assert not out['ok']
 assert sum(math.hypot(p['x'],p['y'])>.5 for p in r.moves)==int(defect=='replaced_class_actor')
