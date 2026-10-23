"""Synthetic delayed cursor repro, prompted by preserved Pietro recorder9->0."""
import pytest
from ff7r_agent.service import SafetyError
from test_queen_blood_profile import _deck_service

def setup(tmp_path):
 deck=['card_id_000']*2+['card_id_007']*2+['card_id_117']+['card_id_002']*10
 service,game=_deck_service(tmp_path,deck)
 game.editor=True;game.details=True;game._move('Strip',(0,3))
 return service,game,deck

def test_delayed_overshoot_reapproaches_and_still_checks_card_before_confirm(tmp_path,monkeypatch):
 service,game,deck=setup(tmp_path);original=service._deck_popup_card;calls=[]
 def popup(area,target,*args,**kwargs):
  calls.append(target)
  if len(calls)==1:
   game._move('Strip',(0,0));return None
  return original(area,target,*args,**kwargs)
 monkeypatch.setattr(service,'_deck_popup_card',popup)
 assert service._confirm_deck_card('Strip',(0,3),'card_id_117',15,-1)==14
 assert calls==[(0,3),(0,3)] and game.deck.count('card_id_117')==0
 assert sum(a[0]=='tap' and a[1]=='confirm' for a in game.backend.actions)==1

@pytest.mark.parametrize('defect',['readable_wrong','same_position_unreadable','other_area','count_changed','retry_unreadable'])
def test_no_blind_confirmation_or_unbounded_cursor_retry(tmp_path,monkeypatch,defect):
 service,game,deck=setup(tmp_path);calls=[];moves=[]
 original_move=service._deck_move
 def move(*args):moves.append(args);return original_move(*args)
 def popup(area,target,*args,**kwargs):
  calls.append(target)
  if defect=='readable_wrong':return 'card_id_000'
  if len(calls)==1:
   if defect!='same_position_unreadable':game._move('Strip',(0,0))
   if defect=='other_area':game._move('Edit',(0,0))
   if defect=='count_changed':game.deck.pop()
  return None
 monkeypatch.setattr(service,'_deck_popup_card',popup);monkeypatch.setattr(service,'_deck_move',move)
 with pytest.raises(SafetyError,match='not confirming'):
  service._confirm_deck_card('Strip',(0,3),'card_id_117',15,-1)
 assert not any(a[0]=='tap' and a[1]=='confirm' for a in game.backend.actions)
 assert len(moves)<=1 and len(calls)<=2
 if defect!='retry_unreadable':assert not moves



def test_recorded_target_then_wrap_is_not_stable_popup_proof():
 import json
 from pathlib import Path
 from types import SimpleNamespace
 from ff7r_agent.service import AgentService
 frames=json.loads((Path(__file__).parent/'fixtures/pietro-strip-overshoot-derived.json').read_text())['frames']
 states=[SimpleNamespace(queen_blood=f['queen_blood']) for f in frames]
 assert AgentService._deck_editor_view(states[0],'Strip')==((0,9),'card_id_117')
 assert AgentService._deck_editor_view(states[1],'Strip')==((0,0),'card_id_000')
 service=AgentService.__new__(AgentService);now=[0.0]
 service._monotonic=lambda:now[0]
 service._sleeper=lambda gap:now.__setitem__(0,now[0]+gap)
 service.telemetry=SimpleNamespace(observe=lambda:states[0] if now[0]<0.2 else states[1])
 assert service._deck_popup_card('Strip',(0,9)) is None


def test_count_change_during_popup_read_refuses_confirm_without_an_extra_count_poll(tmp_path, monkeypatch):
    service, game, _ = setup(tmp_path)
    observe = game.observe
    reads = 0

    def changing():
        nonlocal reads
        reads += 1
        if reads == 2:  # The popup read after the initial card/count acknowledgement.
            game.deck.pop()  # Keep the hovered Magic Pot; change only the total.
        return observe()

    monkeypatch.setattr(game, "observe", changing)
    with pytest.raises(SafetyError, match="count changed while checking the card"):
        service._confirm_deck_card("Strip", (0, 3), "card_id_117", 15, -1)
    assert not any(a[0] == "tap" and a[1] == "confirm" for a in game.backend.actions)
