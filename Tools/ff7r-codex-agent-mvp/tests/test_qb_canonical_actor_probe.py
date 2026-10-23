from types import SimpleNamespace
import pytest
from ff7r_agent.service import AgentService, SafetyError

CANONICAL = "field_object:2350-GOLDS:oba2350_Actor_Gentleman01"
NAME = "Npc2350_Gentleman01"

def service(monkeypatch, actor_name=NAME, *, distance=100, current_sequence=2, current_map="EndGame"):
    def state(seq, map_id):
        sample = {"map_id": map_id, "player": {"actor_id": "Cloud", "position": {"x": 0, "y": 0, "z": 0}, "rotation": {"yaw": 0}}}
        return SimpleNamespace(sequence=seq, map_id=map_id, player=SimpleNamespace(actor_id="Cloud"), to_dict=lambda: sample)
    before=state(1,"EndGame");after=state(current_sequence,current_map)
    s=AgentService.__new__(AgentService);s.telemetry=SimpleNamespace(observe=lambda:after);s._monotonic=lambda:100
    seen=[]
    def scan(_, selector, seconds):
        seen.append((selector,seconds))
        return {"snapshot":{"complete":True,"target":{"id":"actor-exact","actor_name":actor_name,"is_player":False,"position":{"x":distance,"y":0,"z":0}}}}
    monkeypatch.setattr("ff7r_agent.character_locator.scan_character_target",scan)
    return s,before,seen

@pytest.mark.parametrize("target,expected",[(CANONICAL,NAME),(NAME,NAME)])
def test_catalogue_canonical_normalizes_without_guessing(monkeypatch,target,expected):
    s,before,seen=service(monkeypatch)
    target_result,current=s._resolve_queens_blood_target(before,target,"npc",100)
    assert seen==[("actor_name:"+expected,10)]
    assert target_result["id"]=="actor-exact" and current.sequence==2

@pytest.mark.parametrize("kwargs",[{"actor_name":"Other"},{"distance":10000},{"current_sequence":1},{"current_map":"OtherMap"}])
def test_identity_spatial_and_freshness_refusals_remain(monkeypatch,kwargs):
    s,before,seen=service(monkeypatch,**kwargs)
    with pytest.raises(SafetyError):s._resolve_queens_blood_target(before,CANONICAL,"npc",100)
    assert seen==[("actor_name:"+NAME,10)]

def test_unknown_canonical_is_not_remapped(monkeypatch):
    target="field_object:2350-GOLDS:Unknown"
    s,before,seen=service(monkeypatch)
    with pytest.raises(SafetyError):s._resolve_queens_blood_target(before,target,"npc",100)
    assert seen==[("actor_name:"+target,10)]
